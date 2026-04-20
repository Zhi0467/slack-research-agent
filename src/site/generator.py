"""
Website generator — data collection, Jinja2 rendering, and static export.

Independent of ``src/loop/`` — reads data files directly from ``.agent/``
and ``docs/``.  No imports from ``src.loop``.

Usage (one-shot export for testing):
    python3 -m src.site.generator --once --export-dir /tmp/test-site
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import argparse
import copy
import html as html_mod
import subprocess
import sys
import tempfile
import time

try:
    from jinja2 import Environment, FileSystemLoader
except ImportError:
    Environment = None  # type: ignore[assignment,misc]
    FileSystemLoader = None  # type: ignore[assignment,misc]

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parents[2]
AGENT_DIR = BASE_DIR / ".agent"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"
SHOWCASE_SOURCE_DIR = BASE_DIR / "projects" / "agent-monitor-web"
SHOWCASE_COPY_DIRS = ("showcase", "tokenizers", "assets")

# External showcase links rendered as the Launchpad nav band.
SITE_LINKS: List[Dict[str, str]] = [
    {"label": "Repo", "href": "https://github.com/Zhi0467/slack-research-agent", "desc": "Project repository"},
    {"label": "Showcase", "href": "showcase/", "desc": "Browser tools and demos"},
]

# Backlog parsing paths and regexes
BACKLOG_FILE = BASE_DIR / "docs" / "dev" / "BACKLOG.md"
ROADMAP_JSON_FILE = BASE_DIR / "docs" / "dev" / "roadmap.json"
BACKLOG_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
BACKLOG_ROW_RE = re.compile(
    r"^\|\s*(?P<id>(?:AGENT|FIX)-\d+)\s*\|"
    r"\s*(?P<created>[^|\n]*)\|"
    r"\s*(?P<priority>[^|\n]*)\|"
    r"\s*(?P<status>[^|\n]*)\|"
    r"\s*(?P<task>[^|\n]*)\|"
    r"\s*(?P<context>[^|\n]*)\|"
    r"\s*(?P<done_when>[^|\n]*)\|",
    re.MULTILINE,
)
BACKLOG_COMPLETED_ROW_RE = re.compile(
    r"^\|\s*(?P<id>(?:AGENT|FIX)-\d+)\s*\|"
    r"\s*(?P<created>[^|\n]*)\|"
    r"\s*(?P<completed>[^|\n]*)\|"
    r"\s*(?P<summary>[^|\n]*)\|",
    re.MULTILINE,
)
BACKLOG_PLAN_LINK_RE = re.compile(r"\[Plan\]\(([^)]+)\)")
BACKLOG_ISSUE_LINK_RE = re.compile(r"\[Issue\]\(([^)]+)\)")
BACKLOG_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")

# Text processing regexes
WHITESPACE_RE = re.compile(r"\s+")
URL_RE = re.compile(r"https?://\S+")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://[^)]+)\)")
TASK_MESSAGE_BLOCK_RE = re.compile(
    r"- Message:\n```(?:text)?\n(.*?)\n```", re.DOTALL
)
TASK_HUMAN_CONTEXT_BLOCK_RE = re.compile(
    r"\| role: human\]\n(.+?)(?=\n\n\[|\Z)", re.DOTALL
)
TASK_SLACK_MENTION_RE = re.compile(r"<@[^>]+>")
TASK_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")

# Identity redaction regexes
TASK_USER_ID_LINE_RE = re.compile(
    r"^(\s*- User ID:\s*).*$", re.MULTILINE
)
TASK_USER_NAME_LINE_RE = re.compile(
    r"^(\s*- User Name:\s*).*$", re.MULTILINE
)
TASK_JSON_USER_ID_RE = re.compile(r'("user_id"\s*:\s*")[^"]*(")')
TASK_JSON_USER_NAME_RE = re.compile(r'("user_name"\s*:\s*")[^"]*(")')
TASK_CONTEXT_USER_FIELD_RE = re.compile(r"(\|\s*user:\s*)[^\s\|]+")

# Config file — may be overridden by LOOP_CONFIG_FILE env var.
_SUPERVISOR_DEFAULT_RE = re.compile(
    r'^\s*:\s*"\$\{([A-Z0-9_]+):=(.*)\}"\s*$'
)


# ---------------------------------------------------------------------------
# Jinja2 environment
# ---------------------------------------------------------------------------
def make_jinja_env() -> Any:
    """Create a Jinja2 Environment bound to the templates directory."""
    if Environment is None:
        raise RuntimeError(
            "jinja2 is not installed — run: pip install jinja2"
        )
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,  # HTML templates handle their own escaping
        keep_trailing_newline=True,
    )


# ---------------------------------------------------------------------------
# Utility functions (extracted from dashboard.py)
# ---------------------------------------------------------------------------
def read_json(path: str | Path) -> Any:
    """Read and parse a JSON file; return *None* on any error."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def read_text(path: str | Path) -> Optional[str]:
    """Read a text file; return *None* on any error."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except Exception:
        return None


def atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via temp-file rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def json_for_script(value: Any) -> str:
    """Serialize *value* to JSON safe for embedding in ``<script>`` blocks."""
    return (
        json.dumps(value, default=str, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def parse_int(value: Any) -> Optional[int]:
    """Parse *value* as int, returning *None* on failure."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def parse_bool(value: str) -> bool:
    """Match supervisor ``parse_bool`` semantics."""
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------
def resolve_config_file() -> Path:
    """Return the supervisor config file path."""
    env = os.environ.get("LOOP_CONFIG_FILE")
    if env:
        return Path(env)
    return BASE_DIR / "src" / "config" / "supervisor_loop.conf"


def read_supervisor_default(name: str) -> Optional[str]:
    """Read a default value from ``supervisor_loop.conf``."""
    cfg_file = resolve_config_file()
    if not cfg_file.exists():
        return None
    try:
        lines = cfg_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in lines:
        match = _SUPERVISOR_DEFAULT_RE.match(line)
        if not match:
            continue
        key, raw = match.group(1), match.group(2).strip()
        if key != name:
            continue
        nested = re.fullmatch(r"\$\{([A-Z0-9_]+):-(.*)\}", raw)
        if nested:
            return os.environ.get(nested.group(1), nested.group(2))
        if "$(hostname)" in raw:
            return raw.replace("$(hostname)", socket.gethostname())
        return raw
    return None


def read_positive_interval(*names: str) -> Optional[int]:
    """Read the first positive integer from env or config for *names*."""
    for name in names:
        val = parse_int(os.environ.get(name))
        if val and val > 0:
            return val
    for name in names:
        val = parse_int(read_supervisor_default(name))
        if val and val > 0:
            return val
    return None


# ---------------------------------------------------------------------------
# Text processing and redaction (extracted from dashboard.py)
# ---------------------------------------------------------------------------
def redact_identity_text(text: str) -> str:
    """Remove Slack user IDs, names, and mentions from *text*."""
    raw = str(text or "")
    if not raw:
        return ""
    redacted = TASK_SLACK_MENTION_RE.sub("[user]", raw)
    redacted = TASK_USER_ID_LINE_RE.sub(r"\1[redacted]", redacted)
    redacted = TASK_USER_NAME_LINE_RE.sub(r"\1[redacted]", redacted)
    redacted = TASK_JSON_USER_ID_RE.sub(r'\1[redacted]\2', redacted)
    redacted = TASK_JSON_USER_NAME_RE.sub(r'\1[redacted]\2', redacted)
    redacted = TASK_CONTEXT_USER_FIELD_RE.sub(r"\1[redacted]", redacted)
    return redacted


def summarize_task_description(text: str, limit: int = 220) -> str:
    """Extract a one-sentence summary from task text."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    raw = MARKDOWN_LINK_RE.sub(r"\1", raw)
    raw = TASK_SLACK_MENTION_RE.sub("", raw)
    raw = raw.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    raw = raw.replace("`", "")
    raw = URL_RE.sub("[link]", raw)
    raw = WHITESPACE_RE.sub(" ", raw).strip(" -:;,.")
    if not raw:
        return ""
    parts = [
        part.strip()
        for part in TASK_SENTENCE_SPLIT_RE.split(raw)
        if part.strip()
    ]
    sentence = parts[0] if parts else raw
    if sentence:
        sentence = sentence[0].upper() + sentence[1:]
    if len(sentence) > limit:
        sentence = sentence[: max(limit - 1, 0)].rstrip() + "…"
    if sentence and sentence[-1] not in ".!?…":
        sentence += "."
    return sentence


def looks_like_task_metadata(text: str) -> bool:
    """Return True if *text* looks like boilerplate task metadata."""
    lowered = str(text or "").lower()
    return (
        "## mention (mention_ts=" in lowered
        or "- thread id:" in lowered
        or "[context update:" in lowered
    )


def extract_task_objective_candidate(task_text: str) -> str:
    """Pull the first meaningful sentence from raw task text."""
    raw = str(task_text or "")
    if not raw.strip():
        return ""

    for match in TASK_MESSAGE_BLOCK_RE.finditer(raw):
        candidate = (match.group(1) or "").strip()
        if candidate and candidate != "[no text]":
            return candidate

    for match in TASK_HUMAN_CONTEXT_BLOCK_RE.finditer(raw):
        candidate = (match.group(1) or "").strip()
        if candidate and candidate != "[empty message]":
            return candidate

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if (
            stripped.startswith("#")
            or stripped.startswith("- ")
            or lowered.startswith("[context update:")
            or lowered.startswith("```")
        ):
            continue
        return stripped
    return ""


def derive_task_description(task: dict, mention_text: str) -> str:
    """Derive a short public-facing description for a task."""
    explicit = summarize_task_description(task.get("task_description") or "")
    if explicit and not looks_like_task_metadata(explicit):
        return explicit

    mention_candidate = extract_task_objective_candidate(mention_text)
    mention_desc = summarize_task_description(mention_candidate)
    if mention_desc:
        return mention_desc

    summary_desc = summarize_task_description(task.get("summary") or "")
    if summary_desc and not looks_like_task_metadata(summary_desc):
        return summary_desc

    return "Resolve the objective requested in this task thread."


def _contains_any(text: str, keywords: tuple) -> bool:
    lowered = str(text or "").lower()
    return any(keyword in lowered for keyword in keywords)


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------
REDACTED_SECRET = "[REDACTED_SECRET]"
SECRET_PATTERNS = [
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]+`")


def redact_secrets(text: str) -> str:
    """Replace known secret patterns with a placeholder."""
    out = text
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(REDACTED_SECRET, out)
    return out


def compact_public_text(text: str, limit: int = 160) -> str:
    """Clean and truncate text for public display."""
    if not isinstance(text, str):
        return ""
    cleaned = CODE_BLOCK_RE.sub(" [code block] ", text)
    cleaned = INLINE_CODE_RE.sub(" [code] ", cleaned)
    cleaned = URL_RE.sub("[link]", cleaned)
    cleaned = WHITESPACE_RE.sub(" ", cleaned).strip()
    cleaned = redact_secrets(cleaned)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(limit - 1, 0)] + "…"


def deep_redact(value: Any) -> Any:
    """Recursively redact secrets from strings in nested structures."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [deep_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: deep_redact(val) for key, val in value.items()}
    return value


# ---------------------------------------------------------------------------
# Task text resolution and enrichment
# ---------------------------------------------------------------------------
def resolve_task_text(task: dict) -> str:
    """Resolve the full mention text for a task (inline or from file)."""
    if not isinstance(task, dict):
        return ""
    text = task.get("mention_text")
    if isinstance(text, str) and text.strip():
        return text
    mention_text_file = task.get("mention_text_file")
    if mention_text_file:
        data = read_json(mention_text_file)
        if isinstance(data, dict) and "messages" in data:
            parts = []
            for msg in data.get("messages") or []:
                t = str(msg.get("text") or "")
                if t and msg.get("source") != "context_snapshot":
                    parts.append(t)
            return "\n\n".join(parts) if parts else ""
        return read_text(mention_text_file) or ""
    return ""


# ---------------------------------------------------------------------------
# Public-facing text derivation
# ---------------------------------------------------------------------------
BACKLOG_PUBLIC_RULES: List[Dict[str, Any]] = [
    {
        "keywords": ("secret", "token", "credential", "auth", "exposure"),
        "title": "Strengthen secret protection.",
        "summary": "Close gaps that could expose sensitive data in review or delivery paths.",
    },
    {
        "keywords": ("tribune", "review", "reviewer", "handoff", "identity"),
        "title": "Tighten the review path.",
        "summary": "Keep the second-pass review flow stable, legible, and trustworthy.",
    },
    {
        "keywords": ("worktree", "symlink", "git", "merge", "branch", "submodule"),
        "title": "Stabilize shared code handoffs.",
        "summary": "Reduce friction when multiple work lanes touch the same codebase.",
    },
    {
        "keywords": ("session", "resume", "redispatch", "thread", "context"),
        "title": "Preserve work continuity.",
        "summary": "Carry active work across sessions without losing useful context.",
    },
    {
        "keywords": ("dashboard", "roadmap", "backlog", "showcase", "publisher", "site"),
        "title": "Sharpen the public site.",
        "summary": "Keep the live routes current, coherent, and easy to read.",
    },
    {
        "keywords": ("slack", "dispatch", "prompt", "communication", "profile"),
        "title": "Refine public-facing communication.",
        "summary": "Make the system easier to understand from the outside.",
    },
    {
        "keywords": ("gpu", "drive", "backup", "runtime", "log", "watchdog"),
        "title": "Harden runtime infrastructure.",
        "summary": "Improve the system layer behind delivery and ongoing operation.",
    },
    {
        "keywords": ("skill", "workflow", "overleaf", "paper", "visualization"),
        "title": "Package a reusable workflow.",
        "summary": "Turn a repeated internal process into a clearer repeatable path.",
    },
]


def derive_public_task_story(task: dict, mention_text: str) -> Tuple[str, str]:
    """Return a (title, summary) pair suitable for public display."""
    objective = derive_task_description(task, mention_text)
    summary = str(task.get("summary") or "")
    lowered = f"{objective} {summary}".lower()
    task_type = str(task.get("task_type") or "").lower()

    if _contains_any(
        lowered,
        ("dashboard", "roadmap", "showcase", "website", "site", "frontend",
         "tokenizer", "tokenizers", "res publica"),
    ):
        if "dashboard" in lowered and "roadmap" in lowered:
            return ("Signal-board redesign", "Retuning the public monitor and roadmap.")
        if _contains_any(lowered, ("tokenizer", "tokenizers", "showcase", "res publica")):
            return ("Browser artifact update", "Refining a public browser tool or exhibit.")
        return ("Site update", "Tightening a public route, tool, or interaction.")

    if _contains_any(
        lowered,
        ("research", "paper", "proof", "theorem", "benchmark", "experiment",
         "architecture comparison", "dataset", "model", "loss", "optimizer",
         "auc", "evaluation", "report"),
    ):
        return ("Research packet", "Developing an analysis and shaping it into a readable result.")

    if task_type == "maintenance" or _contains_any(
        lowered,
        ("maintenance", "developer review", "tribune", "slack", "github",
         "repo", "repos", "dispatch", "branch", "merge", "pull request",
         "ci", "auth", "credential", "token"),
    ):
        return ("Infrastructure upkeep", "Stabilizing the systems behind delivery and collaboration.")

    if task_type == "development":
        return ("Build cycle", "Implementing a scoped capability from the active build board.")

    if _contains_any(lowered, ("feature", "fix", "bug", "improve")):
        return ("Build step", "Turning a scoped request into a verified improvement.")

    return ("Live work", "Advancing the active request.")


def derive_public_backlog_copy(
    title: str, detail: str, *, queue: str = "", completed: bool = False,
) -> Tuple[str, str]:
    """Return a (title, summary) pair for a backlog item's public display."""
    combined = f"{title} {detail}".lower()
    for rule in BACKLOG_PUBLIC_RULES:
        if _contains_any(combined, rule["keywords"]):
            return rule["title"], rule["summary"]
    if completed:
        return ("Completed system upgrade.", "A scoped improvement landed and is now part of the system.")
    if str(queue).lower() == "fix":
        return ("Open reliability fix.", "A quality or stability issue is queued for cleanup.")
    if str(queue).lower() == "feature":
        return ("Open capability build.", "A scoped improvement is queued for the next build pass.")
    return ("Open work item.", "A scoped change is queued for follow-through.")


def enrich_bucket_tasks(bucket: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Enrich a bucket of tasks with descriptions and redacted fields."""
    out: Dict[str, Any] = {}
    for key, task in (bucket or {}).items():
        if not isinstance(task, dict):
            continue
        row = dict(task)
        mention_text = redact_identity_text(resolve_task_text(row))
        row["task_description"] = redact_identity_text(
            derive_task_description(row, mention_text)
        )
        row["summary"] = redact_identity_text(str(row.get("summary") or ""))
        public_title, public_summary = derive_public_task_story(row, mention_text)
        row["public_title"] = public_title
        row["public_summary"] = public_summary
        row["mention_text"] = "(hidden in monitor payload)"
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        row["source"] = {
            "user_id": "[redacted]",
            "user_name": "[redacted]",
            "time_iso": source.get("time_iso"),
        }
        out[key] = row
    return out


def public_task_view(task: dict) -> Dict[str, Any]:
    """Build a public-safe view of a single task."""
    if not isinstance(task, dict):
        return {}
    source = task.get("source") if isinstance(task.get("source"), dict) else {}
    objective_text = derive_task_description(task, str(task.get("mention_text") or ""))
    summary_source = str(task.get("summary") or "")
    if not summary_source or looks_like_task_metadata(summary_source):
        summary_source = objective_text
    task_description = (
        compact_public_text(objective_text, limit=180)
        or "Task objective summary unavailable."
    )
    summary_preview = (
        compact_public_text(summary_source, limit=160)
        or "(task details redacted for public view)"
    )
    return {
        "mention_ts": task.get("mention_ts"),
        "thread_ts": task.get("thread_ts"),
        "status": task.get("status"),
        "task_description": task_description,
        "summary": summary_preview,
        "summary_preview": summary_preview,
        "mention_text": "(hidden in public snapshot)",
        "created_ts": task.get("created_ts"),
        "last_update_ts": task.get("last_update_ts"),
        "task_type": task.get("task_type"),
        "source": {
            "user_name": "[redacted]",
            "time_iso": source.get("time_iso"),
        },
    }


def sanitize_public_status(payload: dict) -> dict:
    """Remove sensitive fields from a full status payload for public use."""
    safe = copy.deepcopy(payload)
    system = safe.get("system") if isinstance(safe.get("system"), dict) else {}
    if system:
        system.pop("repo_path", None)
        system.pop("agent_dir", None)
        system["hostname"] = "redacted-host"
        system.pop("local_ips", None)
        system.pop("dashboard_pid", None)

    tasks = safe.get("tasks") if isinstance(safe.get("tasks"), dict) else {}
    safe_tasks: Dict[str, Any] = {}
    for bucket_name in ("queued", "active", "incomplete", "finished"):
        rows = tasks.get(bucket_name) if isinstance(tasks.get(bucket_name), dict) else {}
        safe_tasks[bucket_name] = {
            k: public_task_view(v) for k, v in rows.items()
        }
    safe["tasks"] = safe_tasks

    gpu = safe.get("gpu") if isinstance(safe.get("gpu"), dict) else {}
    if gpu:
        original_alias = str(gpu.get("node_alias") or "").strip()
        gpu["node_alias"] = "redacted-node"
        note = str(gpu.get("note") or "").strip()
        if original_alias and note:
            note = note.replace(original_alias, "redacted-node")
        if gpu.get("status") == "unavailable":
            note = "Remote GPU data unavailable for this snapshot."
        if note:
            gpu["note"] = note

    safe["visibility"] = build_visibility_info("public_snapshot")
    return deep_redact(safe)


# ---------------------------------------------------------------------------
# Roadmap public-facing copy
# ---------------------------------------------------------------------------
ROADMAP_VISION_FALLBACK = (
    "An autonomous research system that can investigate, build, write, "
    "and publish on a steady public cadence."
)

ROADMAP_THEME_PUBLIC_COPY: Dict[str, Tuple[str, str]] = {
    "Supervisor & Task Lifecycle": (
        "Core Platform",
        "The foundation that keeps task flow, state, and upkeep reliable.",
    ),
    "Multi-Agent Parallel Dispatch": (
        "Concurrent Builds",
        "Multiple active build streams with clean isolation and safe merges.",
    ),
    "Worker Resilience": (
        "Reliability",
        "Keeping active work alive through failures, hangs, and restarts.",
    ),
    "Research & Consultation": (
        "Research Delivery",
        "From assignment through analysis to delivered research artifacts.",
    ),
    "Quality Gates & Review": (
        "Quality Control",
        "Independent review and release checks before work goes public.",
    ),
    "Agent Communication & Identity": (
        "Interface",
        "How the public surface reads, responds, and stays coherent.",
    ),
    "Observability & Dashboard": (
        "Public Site",
        "The live site, release map, and supporting public telemetry.",
    ),
}

ROADMAP_GOAL_PUBLIC_COPY: Dict[str, Tuple[str, str]] = {
    "Bootstrap & dispatch loop": (
        "Dispatch Engine",
        "Task routing, typed state, and fast restart for the orchestration layer.",
    ),
    "Maintenance system": (
        "Maintenance Loop",
        "Routine upkeep and deeper review passes for the system.",
    ),
    "Dispatch optimization": (
        "Loop Speed",
        "Faster polling, cleaner completion gates, and leaner runtime behavior.",
    ),
    "Queue & lifecycle polish": (
        "Queue Reliability",
        "Safer delivery gates, cleaner drains, and tighter lifecycle behavior.",
    ),
    "Parallel infrastructure": (
        "Parallel Runtime",
        "Concurrent work lanes with isolation, coordinated writes, and merge safety.",
    ),
    "Multi-worker project collaboration": (
        "Shared Project Coordination",
        "Rules for multiple active lanes touching the same project at once.",
    ),
    "Watchdog & recovery": (
        "Failure Recovery",
        "Startup guards, retry logic, hang detection, and safer recovery.",
    ),
    "Session persistence": (
        "Session Continuity",
        "Carry active work across new sessions with less restart friction.",
    ),
    "Consult MCP integration": (
        "External Reasoning",
        "Specialist consultation with durable history and reusable context.",
    ),
    "Research delivery": (
        "Artifact Delivery",
        "Visualization, project sub-sessions, writing, and async execution.",
    ),
    "Thread management at scale": (
        "Long-Run Threads",
        "Keep long-running work readable and continuous as it grows.",
    ),
    "Tribune role": (
        "Independent Review",
        "A second reviewer that pressure-tests quality before delivery.",
    ),
    "Iterative review skill": (
        "Multi-Pass Review",
        "Structured review passes that pressure-test a release from different angles.",
    ),
    "Tribune integration hardening": (
        "Review Reliability",
        "Keeping the review handoff stable, trustworthy, and easy to follow.",
    ),
    "Multi-role deliberation": (
        "Multi-Perspective Deliberation",
        "Broader discussion formats that compare several reviewer viewpoints.",
    ),
    "Natural communication": (
        "Public-Facing Voice",
        "Cleaner peer-style communication and clearer completion signals.",
    ),
    "User awareness": (
        "Audience Context",
        "Remembering who the site is speaking to without losing clarity.",
    ),
    "Skill library": (
        "Reusable Skills",
        "Portable capabilities that can be reused across tasks and surfaces.",
    ),
    "Behavioral contracts": (
        "Role Boundaries",
        "Cleaner boundaries across the major roles in the system.",
    ),
    "Public dashboard": (
        "Live Signal Board",
        "Public site with live status, backlog, and showcase routes.",
    ),
    "Strategic roadmap": (
        "Release Map",
        "A public roadmap for the next releases and longer arcs.",
    ),
    "Ecosystem awareness": (
        "Ecosystem Scan",
        "Daily scouting of relevant agent-system developments.",
    ),
    "State validation": (
        "State Integrity",
        "Validation around runtime state files and public data.",
    ),
    "Cloud backup": (
        "Artifact Continuity",
        "Off-repo retention for runtime artifacts that should stay available.",
    ),
}


def summarize_checkpoint_rollup(milestones: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize milestone completion for a roadmap goal."""
    rows = [row for row in (milestones or []) if isinstance(row, dict)]
    total = len(rows)
    done = sum(1 for row in rows if str(row.get("status") or "") == "done")
    active = sum(1 for row in rows if str(row.get("status") or "") == "in_progress")
    queued = max(total - done - active, 0)
    if total == 0:
        note = "No milestones yet."
    elif active:
        note = f"{active} in motion, {queued} queued."
    elif done == total:
        note = "All milestones locked."
    else:
        note = f"{queued} queued."
    return {
        "checkpoint_total": total,
        "checkpoint_done": done,
        "checkpoint_active": active,
        "checkpoint_note": note,
    }


def publicize_roadmap_data(roadmap_data: dict) -> dict:
    """Convert internal roadmap data to public-facing copy."""
    safe = copy.deepcopy(roadmap_data or {})
    vision = summarize_task_description(safe.get("vision") or "", limit=220)
    if vision:
        vision = (
            vision.replace("agent", "system")
            .replace(
                "independently conducts, writes up, and delivers high-quality "
                "research while maintaining its own infrastructure",
                "investigates, builds, writes, and ships work on a steady "
                "public cadence",
            )
        )
    safe["vision"] = vision or ROADMAP_VISION_FALLBACK
    safe["last_updated"] = str(safe.get("last_updated") or "")

    public_themes = []
    for theme in safe.get("themes") or []:
        if not isinstance(theme, dict):
            continue
        theme_name = str(theme.get("name") or "")
        public_theme_name, public_theme_desc = ROADMAP_THEME_PUBLIC_COPY.get(
            theme_name,
            (theme_name or "Untitled lane",
             summarize_task_description(theme.get("description") or "", limit=180)),
        )
        public_theme = dict(theme)
        public_theme["name"] = public_theme_name
        public_theme["description"] = public_theme_desc

        public_goals = []
        for goal in theme.get("goals") or []:
            if not isinstance(goal, dict):
                continue
            goal_name = str(goal.get("name") or "")
            public_goal_name, public_goal_desc = ROADMAP_GOAL_PUBLIC_COPY.get(
                goal_name,
                (goal_name or "Untitled goal",
                 summarize_task_description(goal.get("description") or "", limit=180)),
            )
            public_goal = dict(goal)
            public_goal["name"] = public_goal_name
            public_goal["description"] = public_goal_desc
            public_goal.update(
                summarize_checkpoint_rollup(goal.get("milestones") or [])
            )
            public_goals.append(public_goal)

        public_theme["goals"] = public_goals
        public_themes.append(public_theme)

    safe["themes"] = public_themes
    return safe


# ---------------------------------------------------------------------------
# GPU monitoring (stateless — no threading or caching)
# ---------------------------------------------------------------------------
DEFAULT_LOCAL_WRITE_INTERVAL_SEC = 2


def default_gpu_status(enabled: bool, node_alias: str) -> Dict[str, Any]:
    """Return a default GPU status dict."""
    return {
        "enabled": bool(enabled),
        "node_alias": node_alias,
        "status": "disabled" if not enabled else "pending",
        "checked_at_utc": None,
        "cache_age_sec": None,
        "note": (
            "GPU monitor disabled for this run." if not enabled
            else "GPU monitor enabled; waiting for first poll."
        ),
        "gpus": [],
    }


def run_remote_command(
    node_alias: str, remote_command: str, timeout_sec: int,
) -> Dict[str, Any]:
    """Run a command on a remote host via SSH."""
    cmd = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=4",
        node_alias, remote_command,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout_sec, check=False,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "error": None,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "returncode": None,
            "stdout": "", "stderr": "",
            "error": f"timed out after {timeout_sec}s",
        }
    except Exception as exc:
        return {
            "ok": False, "returncode": None,
            "stdout": "", "stderr": "",
            "error": str(exc),
        }


def parse_gpu_rows(raw_text: str) -> List[Dict[str, Any]]:
    """Parse nvidia-smi CSV output into GPU row dicts."""
    rows: List[Dict[str, Any]] = []
    for line in (raw_text or "").splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        rows.append({
            "index": parse_int(parts[0]),
            "name": parts[1],
            "utilization_gpu_pct": parse_int(parts[2]),
            "memory_used_mb": parse_int(parts[3]),
            "memory_total_mb": parse_int(parts[4]),
            "temperature_c": parse_int(parts[5]),
        })
    return rows


def collect_remote_gpu_status(
    node_alias: str, timeout_sec: int,
) -> Dict[str, Any]:
    """Collect GPU status from a remote node via SSH + nvidia-smi."""
    result = default_gpu_status(enabled=True, node_alias=node_alias)
    result["checked_at_utc"] = utc_now_iso()

    gpu_cmd = (
        "nvidia-smi --query-gpu=index,name,utilization.gpu,"
        "memory.used,memory.total,temperature.gpu "
        "--format=csv,noheader,nounits"
    )
    gpu_resp = run_remote_command(
        node_alias=node_alias, remote_command=gpu_cmd, timeout_sec=timeout_sec,
    )
    if not gpu_resp["ok"]:
        detail = (
            gpu_resp["error"]
            or (gpu_resp["stderr"] or gpu_resp["stdout"]).strip()
        )
        result["status"] = "unavailable"
        result["note"] = f"GPU query failed: {detail or 'no details'}"
        return result

    result["gpus"] = parse_gpu_rows(gpu_resp["stdout"])
    result["status"] = "ok" if result["gpus"] else "unavailable"
    if not result["gpus"]:
        result["note"] = "nvidia-smi returned no GPU rows."
    else:
        result["note"] = f"{len(result['gpus'])} GPU(s) reporting."
    return result


# ---------------------------------------------------------------------------
# Polling info
# ---------------------------------------------------------------------------
def resolve_supervisor_poll_interval_sec() -> int:
    """Return the supervisor poll interval from config."""
    return (
        read_positive_interval("SLEEP_NORMAL")
        or DEFAULT_LOCAL_WRITE_INTERVAL_SEC
    )


def build_polling_info() -> Dict[str, int]:
    """Build polling interval info dict."""
    poll_interval = resolve_supervisor_poll_interval_sec()
    return {
        "supervisor_poll_interval_sec": poll_interval,
        "frontend_poll_interval_sec": poll_interval,
    }


# ---------------------------------------------------------------------------
# Status builders (orchestrators)
# ---------------------------------------------------------------------------
def build_status(*, gpu_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the full status payload for the monitor dashboard.

    *gpu_data* is optional — when running as a standalone static export
    the caller can pass pre-collected GPU data or ``None`` to skip.
    """
    heartbeat = read_json(AGENT_DIR / "runtime" / "heartbeat.json")
    state = read_json(AGENT_DIR / "runtime" / "state.json")
    system = build_system_info()

    tasks = None
    if state:
        finished_all = state.get("finished_tasks") or {}
        finished_sorted = dict(
            sorted(
                finished_all.items(),
                key=lambda kv: kv[1].get("last_update_ts", "0"),
                reverse=True,
            )[:50]
        )
        tasks = {
            "queued": enrich_bucket_tasks(state.get("queued_tasks") or {}),
            "active": enrich_bucket_tasks(state.get("active_tasks") or {}),
            "incomplete": enrich_bucket_tasks(state.get("incomplete_tasks") or {}),
            "finished": enrich_bucket_tasks(finished_sorted),
        }

    return {
        "heartbeat": heartbeat,
        "system": system,
        "gpu": gpu_data or default_gpu_status(enabled=False, node_alias=""),
        "tasks": tasks,
        "roadmap": publicize_roadmap_data(parse_roadmap_json()),
        "backlog": parse_backlog(),
        "polling": build_polling_info(),
        "visibility": build_visibility_info("local_live"),
        "server_time_utc": utc_now_iso(),
    }


def build_public_status(
    *, gpu_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a minimal status dict safe for the public landing page."""
    full = build_status(gpu_data=gpu_data)
    hb = full.get("heartbeat") or {}
    gpu = full.get("gpu") or {}
    task_data = full.get("tasks") or {}

    gpus = gpu.get("gpus") or []
    gpu_count = len(gpus)
    gpu_name = (
        gpus[0]["name"].split(" Server Edition")[0] if gpus else "N/A"
    )
    avg_util = (
        sum(g.get("utilization_gpu_pct", 0) for g in gpus) // max(gpu_count, 1)
    )
    total_mem = round(
        sum(g.get("memory_total_mb", 0) for g in gpus) / 1024, 1,
    )
    used_mem = round(
        sum(g.get("memory_used_mb", 0) for g in gpus) / 1024, 1,
    )

    return {
        "agent_online": hb.get("status") not in ("stopped", None),
        "status": hb.get("status", "unknown"),
        "loop_count": hb.get("loop_count", 0),
        "max_workers": hb.get("max_workers", 0),
        "last_updated_utc": hb.get("last_updated_utc", ""),
        "tasks": {
            "active": len(task_data.get("active", {})),
            "queued": len(task_data.get("queued", {})),
            "finished": len(task_data.get("finished", {})),
        },
        "gpu": {
            "count": gpu_count,
            "name": gpu_name,
            "avg_utilization_pct": avg_util,
            "total_memory_gb": total_mem,
            "used_memory_gb": used_mem,
        },
    }


# ---------------------------------------------------------------------------
# System info and utilities
# ---------------------------------------------------------------------------
def file_size_bytes(path: str | Path) -> Optional[int]:
    """Return size in bytes of *path*, or None on error."""
    try:
        return Path(path).stat().st_size
    except Exception:
        return None


def sysconf_int(name: str) -> Optional[int]:
    """Query os.sysconf(*name*), returning None on failure or non-positive."""
    try:
        value = os.sysconf(name)
        if isinstance(value, int) and value > 0:
            return value
    except Exception:
        return None
    return None


def utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_visibility_info(mode: str) -> Dict[str, str]:
    """Build a visibility/audience info dict for the given mode."""
    if mode == "public_snapshot":
        return {
            "mode": mode,
            "audience": "Anyone with the GitHub Pages URL.",
            "task_text_access": (
                "Thread-scoped task_description plus compact previews; "
                "requester identities are redacted."
            ),
            "source_code_exposure": (
                "No repository files are published by this dashboard output."
            ),
        }
    return {
        "mode": "local_live",
        "audience": "Anyone with local machine/port access.",
        "task_text_access": (
            "Thread-scoped task_description plus truncated previews; "
            "requester identities are redacted in the monitor payload."
        ),
        "source_code_exposure": (
            "Dashboard does not publish repository files by itself."
        ),
    }


def build_system_info() -> Dict[str, Any]:
    """Collect local system info (disk, CPU, memory, etc.)."""
    disk_total = disk_used = disk_free = None
    try:
        usage = shutil.disk_usage(BASE_DIR)
        disk_total, disk_used, disk_free = usage.total, usage.used, usage.free
    except Exception:
        pass

    load_avg = None
    try:
        one, five, fifteen = os.getloadavg()
        load_avg = {
            "one": round(one, 2),
            "five": round(five, 2),
            "fifteen": round(fifteen, 2),
        }
    except Exception:
        pass

    memory_total_bytes = None
    pages = sysconf_int("SC_PHYS_PAGES")
    page_size = sysconf_int("SC_PAGE_SIZE")
    if pages and page_size:
        memory_total_bytes = pages * page_size

    return {
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "python_version": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "load_avg": load_avg,
        "memory_total_bytes": memory_total_bytes,
        "disk_total_bytes": disk_total,
        "disk_used_bytes": disk_used,
        "disk_free_bytes": disk_free,
        "repo_path": str(BASE_DIR),
        "agent_dir": str(AGENT_DIR),
        "runner_log_size_bytes": file_size_bytes(
            AGENT_DIR / "runtime" / "logs" / "runner.log"
        ),
    }


# ---------------------------------------------------------------------------
# Backlog and roadmap parsers (extracted from dashboard.py)
# ---------------------------------------------------------------------------
def parse_backlog(
    session_map: Optional[Dict[str, str]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Parse ``docs/dev/BACKLOG.md`` and return items + completed lists."""
    try:
        text = BACKLOG_FILE.read_text(encoding="utf-8")
    except OSError:
        return {"items": [], "completed": []}

    if session_map is None:
        session_map = {}

    _BACKTICK_PIPE_RE = re.compile(r"`[^`]*`")

    def _shield_pipes(t: str) -> str:
        return _BACKTICK_PIPE_RE.sub(
            lambda m: m.group(0).replace("|", "\x00"), t
        )

    def _restore_pipes(t: str) -> str:
        return t.replace("\x00", "|")

    sections = list(BACKLOG_SECTION_RE.finditer(text))
    queue_sections: List[Tuple[str, str]] = []
    completed_text = ""
    for i, m in enumerate(sections):
        title = m.group(1).strip().lower()
        start = m.end()
        end = sections[i + 1].start() if i + 1 < len(sections) else len(text)
        if "fix" in title and "queue" in title:
            queue_sections.append(("fix", text[start:end]))
        elif "active" in title:
            queue_sections.append(("feature", text[start:end]))
        elif "completed" in title:
            completed_text = text[start:end]

    items: List[Dict[str, Any]] = []
    for queue_name, section_text in queue_sections:
        shielded = _shield_pipes(section_text)
        for m in BACKLOG_ROW_RE.finditer(shielded):
            ctx_raw = _restore_pipes(m.group("context").strip())
            plan_match = BACKLOG_PLAN_LINK_RE.search(ctx_raw)
            issue_match = BACKLOG_ISSUE_LINK_RE.search(ctx_raw)
            plan_content = ""
            issue_content = ""
            if plan_match:
                try:
                    plan_content = (
                        BACKLOG_FILE.parent / plan_match.group(1)
                    ).read_text(encoding="utf-8")
                except OSError:
                    pass
            if issue_match:
                try:
                    issue_content = (
                        BACKLOG_FILE.parent / issue_match.group(1)
                    ).read_text(encoding="utf-8")
                except OSError:
                    pass
            item_id = m.group("id").strip()
            items.append({
                "id": item_id,
                "queue": queue_name,
                "created": m.group("created").strip(),
                "priority": m.group("priority").strip(),
                "status": m.group("status").strip(),
                "task": BACKLOG_MD_LINK_RE.sub(
                    r"\1", _restore_pipes(m.group("task").strip())
                ),
                "context": BACKLOG_MD_LINK_RE.sub(r"\1", ctx_raw)[:200],
                "done_when": _restore_pipes(
                    m.group("done_when").strip()
                )[:200],
                "has_plan": bool(plan_match),
                "has_issue": bool(issue_match),
                "plan_content": plan_content,
                "issue_content": issue_content,
                "session_id": session_map.get(item_id, ""),
            })

    completed: List[Dict[str, Any]] = []
    if completed_text:
        shielded = _shield_pipes(completed_text)
        for m in BACKLOG_COMPLETED_ROW_RE.finditer(shielded):
            comp_id = m.group("id").strip()
            raw_summary = _restore_pipes(m.group("summary").strip())
            plan_match = BACKLOG_PLAN_LINK_RE.search(raw_summary)
            issue_match = BACKLOG_ISSUE_LINK_RE.search(raw_summary)
            plan_content = ""
            issue_content = ""
            if plan_match:
                try:
                    plan_content = (
                        BACKLOG_FILE.parent / plan_match.group(1)
                    ).read_text(encoding="utf-8")
                except OSError:
                    pass
            if issue_match:
                try:
                    issue_content = (
                        BACKLOG_FILE.parent / issue_match.group(1)
                    ).read_text(encoding="utf-8")
                except OSError:
                    pass
            completed.append({
                "id": comp_id,
                "created": m.group("created").strip(),
                "completed": m.group("completed").strip(),
                "summary": BACKLOG_MD_LINK_RE.sub(r"\1", raw_summary)[:200],
                "has_plan": bool(plan_match),
                "has_issue": bool(issue_match),
                "plan_content": plan_content,
                "issue_content": issue_content,
                "session_id": session_map.get(comp_id, ""),
            })

    return {"items": items, "completed": completed}


def parse_roadmap_json() -> Dict[str, Any]:
    """Load ``docs/dev/roadmap.json`` and return its contents."""
    try:
        return json.loads(ROADMAP_JSON_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Data collection (builds the context dict passed to templates)
# ---------------------------------------------------------------------------
def collect_site_data(
    *, gpu_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Collect all data needed to render the static site.

    Returns the full ``build_status()`` payload — the single
    data-collection entry point that templates use.
    """
    return build_status(gpu_data=gpu_data)


# ---------------------------------------------------------------------------
# Jinja2 rendering
# ---------------------------------------------------------------------------

def _get_jinja_env() -> Any:
    """Create a Jinja2 Environment with the templates directory.

    Returns None if jinja2 is not installed.
    """
    if Environment is None:
        return None
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=False,  # HTML templates manage their own escaping
        keep_trailing_newline=True,
    )


def render_template(
    template_name: str,
    context: Dict[str, Any],
) -> Optional[str]:
    """Render a Jinja2 template with the given context.

    Returns the rendered HTML string, or None if jinja2 is unavailable.
    """
    env = _get_jinja_env()
    if env is None:
        return None
    tmpl = env.get_template(template_name)
    return tmpl.render(**context)


def json_for_template(data: Any) -> str:
    """Serialize data to a JSON string safe for embedding in <script> tags.

    Produces compact (no indent) JSON with HTML-safe escaping to match the
    original ``dashboard.py`` ``json_for_script()`` output.
    """
    return (
        json.dumps(data, default=str, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def write_rendered_page(
    out_dir: Path,
    template_name: str,
    context: Dict[str, Any],
    *,
    subdir: Optional[str] = None,
    filename: str = "index.html",
) -> Optional[Path]:
    """Render a template and write it to the export directory.

    If *subdir* is given, the file is placed in ``out_dir/subdir/filename``.
    Static assets (CSS/JS) from ``src/site/static/`` matching the template
    base name are copied alongside the rendered HTML.

    Returns the path of the written file, or None if rendering failed.
    """
    html = render_template(template_name, context)
    if html is None:
        return None
    target_dir = out_dir / subdir if subdir else out_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename
    target_path.write_text(html, encoding="utf-8")

    # Process matching static assets (e.g. roadmap.css, roadmap.js for
    # roadmap.html.jinja2).  JS files are rendered through Jinja2 because
    # they may contain template variables (e.g. ``{{ data_json }}``).
    # CSS files are copied verbatim.
    base_name = template_name.split(".")[0]  # "roadmap" from "roadmap.html.jinja2"
    if STATIC_DIR.is_dir():
        for ext in ("css", "js"):
            asset = STATIC_DIR / f"{base_name}.{ext}"
            if not asset.exists():
                continue
            if ext == "js":
                env = _get_jinja_env()
                if env is not None:
                    from jinja2 import Template

                    tmpl = Template(
                        asset.read_text(encoding="utf-8"),
                        autoescape=False,
                    )
                    rendered = tmpl.render(**context)
                    (target_dir / asset.name).write_text(
                        rendered, encoding="utf-8"
                    )
                else:
                    shutil.copy2(str(asset), str(target_dir / asset.name))
            else:
                shutil.copy2(str(asset), str(target_dir / asset.name))
    return target_path


# ---------------------------------------------------------------------------
# Page-specific render functions
# ---------------------------------------------------------------------------

def _build_launchpad_html() -> str:
    """Build the Launchpad navigation band HTML from SITE_LINKS."""
    if not SITE_LINKS:
        return ""
    items = []
    for link in SITE_LINKS:
        label = html_mod.escape(link["label"])
        href = html_mod.escape(link["href"])
        desc = link.get("desc", "")
        title_attr = f' title="{html_mod.escape(desc)}"' if desc else ""
        items.append(
            f'<a class="launchpad-link" href="{href}"{title_attr}>{label}</a>'
        )
    inner = "\n  ".join(items)
    return f'<nav class="launchpad" aria-label="Launchpad">\n  {inner}\n</nav>'


_BACKLOG_SITE_HEADER = (
    '<header style="padding:14px 24px;background:#000;border-bottom:2px solid rgba(56,240,255,0.5);'
    'display:flex;align-items:center;gap:16px;">'
    '<a href="../" style="color:#38f0ff;text-decoration:none;font-family:Orbitron,sans-serif;'
    'font-size:12px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;">'
    '\u2190 Monitor</a>'
    '<span style="color:rgba(56,240,255,0.5);">\u203a</span>'
    '<span style="color:#eaffff;font-family:Orbitron,sans-serif;font-size:14px;font-weight:700;'
    'letter-spacing:0.1em;text-transform:uppercase;">Backlog</span>'
    '<span style="margin-left:auto;display:flex;align-items:center;gap:8px;">'
    '<span class="status-dot missing" id="header-dot"></span>'
    '<button onclick="toggleSettings();scrollToSettings()" id="dispatch-state" style="'
    'background:rgba(56,240,255,0.08);border:1px solid rgba(56,240,255,0.4);color:#b8f4ff;'
    'padding:4px 12px;font-family:Orbitron,sans-serif;font-size:10px;font-weight:700;'
    'letter-spacing:0.08em;text-transform:uppercase;cursor:pointer;'
    'transition:background 150ms,border-color 150ms;'
    '">&#9881; Configure</button>'
    '</span>'
    '</header>'
)

_ROADMAP_SITE_HEADER = (
    '<header style="padding:14px 24px;background:#000;border-bottom:2px solid rgba(56,240,255,0.5);'
    'display:flex;align-items:center;gap:16px;">'
    '<a href="../" style="color:#38f0ff;text-decoration:none;font-family:Orbitron,sans-serif;'
    'font-size:12px;font-weight:700;letter-spacing:0.1em;text-transform:uppercase;">'
    '\u2190 Monitor</a>'
    '<span style="color:rgba(56,240,255,0.5);">\u203a</span>'
    '<span style="color:#eaffff;font-family:Orbitron,sans-serif;font-size:14px;font-weight:700;'
    'letter-spacing:0.1em;text-transform:uppercase;">Roadmap</span>'
    '</header>'
)


def write_monitor_html(
    out_dir: Path,
    data: Dict[str, Any],
    api_status_path: str = "status.json",
) -> Optional[Path]:
    """Render the monitor page to ``out_dir/index.html``."""
    context = {
        "launchpad_html": _build_launchpad_html(),
        "data_json": json_for_template(data),
        "api_status_path_json": json.dumps(api_status_path, ensure_ascii=False),
    }
    return write_rendered_page(out_dir, "monitor.html.jinja2", context)


def write_backlog_html(
    out_dir: Path,
    backlog: Any,
    active_dev_items: Any = None,
) -> Optional[Path]:
    """Render the backlog page to ``out_dir/backlog/index.html``.

    Enriches items with public-facing copy before rendering.
    """
    items = backlog.get("items", []) if isinstance(backlog, dict) else backlog
    completed = backlog.get("completed", []) if isinstance(backlog, dict) else []
    for item in items:
        if isinstance(item, dict) and "public_title" not in item:
            title = str(item.get("title") or item.get("id") or "")
            detail = str(item.get("detail") or item.get("description") or "")
            queue = str(item.get("queue") or "")
            item["public_title"], item["public_summary"] = derive_public_backlog_copy(
                title, detail, queue=queue
            )
    for item in completed:
        if isinstance(item, dict) and "public_title" not in item:
            title = str(item.get("title") or item.get("id") or "")
            detail = str(item.get("detail") or item.get("description") or "")
            item["public_title"], item["public_summary"] = derive_public_backlog_copy(
                title, detail, completed=True
            )
    context = {
        "site_header_html": _BACKLOG_SITE_HEADER,
        "murphy_shell_css": "",
        "backlog_json": json_for_template(items),
        "completed_json": json_for_template(completed),
        "active_dev_json": json_for_template(active_dev_items or []),
    }
    return write_rendered_page(
        out_dir, "backlog.html.jinja2", context, subdir="backlog"
    )


def write_roadmap_html(
    out_dir: Path,
    roadmap_data: Dict[str, Any],
) -> Optional[Path]:
    """Render the roadmap page to ``out_dir/roadmap/index.html``."""
    context = {
        "site_header_html": _ROADMAP_SITE_HEADER,
        "murphy_shell_css": "",
        "roadmap_json": json_for_template(roadmap_data),
    }
    return write_rendered_page(
        out_dir, "roadmap.html.jinja2", context, subdir="roadmap"
    )


def write_standalone_roadmap_html(roadmap_data: Dict[str, Any]) -> Optional[Path]:
    """Write the standalone roadmap visualization to ``docs/dev/roadmap.html``."""
    html = render_template("roadmap.html.jinja2", {
        "site_header_html": "",
        "murphy_shell_css": "",
        "roadmap_json": json_for_template(roadmap_data),
    })
    if html is None:
        return None
    out_path = BASE_DIR / "docs" / "dev" / "roadmap.html"
    out_path.write_text(html, encoding="utf-8")
    # Copy static assets alongside
    roadmap_dir = out_path.parent
    if STATIC_DIR.is_dir():
        for ext in ("css", "js"):
            asset = STATIC_DIR / f"roadmap.{ext}"
            if asset.exists():
                if ext == "js":
                    env = _get_jinja_env()
                    if env is not None:
                        from jinja2 import Template

                        tmpl = Template(
                            asset.read_text(encoding="utf-8"),
                            autoescape=False,
                        )
                        rendered = tmpl.render(
                            roadmap_json=json_for_template(roadmap_data),
                        )
                        (roadmap_dir / asset.name).write_text(
                            rendered, encoding="utf-8"
                        )
                    else:
                        shutil.copy2(str(asset), str(roadmap_dir / asset.name))
                else:
                    shutil.copy2(str(asset), str(roadmap_dir / asset.name))
    return out_path


# ---------------------------------------------------------------------------
# Session log helpers (for backlog per-item dev sessions)
# ---------------------------------------------------------------------------

CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-Users-murphy-Research"
DEV_SESSION_ID_FILE = AGENT_DIR / "runtime" / "dev_session_id"
DEV_SESSION_MAP_FILE = AGENT_DIR / "runtime" / "dev_session_map.json"


def _parse_session_jsonl(session_id: str) -> Optional[List[Dict[str, Any]]]:
    """Parse a Claude Code session JSONL into a renderable log."""
    session_file = CLAUDE_SESSIONS_DIR / f"{session_id}.jsonl"
    if not session_file.exists():
        return None
    entries: List[Dict[str, Any]] = []
    try:
        with open(session_file, encoding="utf-8") as f:
            for line in f:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg_type = raw.get("type", "")
                if msg_type not in ("user", "assistant"):
                    continue
                timestamp = raw.get("timestamp", "")
                msg = raw.get("message", {})
                if isinstance(msg, str):
                    entries.append({
                        "role": "user",
                        "timestamp": timestamp,
                        "blocks": [{"type": "text", "content": msg[:2000]}],
                    })
                    continue
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                blocks: List[Dict[str, Any]] = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get("type", "")
                    if bt == "text":
                        blocks.append({"type": "text", "content": redact_secrets(str(block.get("text", "")))})
                    elif bt == "thinking":
                        blocks.append({"type": "thinking", "content": str(block.get("thinking", ""))})
                    elif bt == "tool_use":
                        inp = block.get("input", {})
                        inp_preview = json.dumps(inp, default=str, ensure_ascii=False)[:500] if isinstance(inp, dict) else str(inp)[:500]
                        blocks.append({"type": "tool_use", "tool": block.get("name", "?"), "input_preview": inp_preview})
                    elif bt == "tool_result":
                        content_val = block.get("content", "")
                        if isinstance(content_val, list):
                            text_parts = [str(part.get("text", "")) for part in content_val if isinstance(part, dict) and part.get("type") == "text"]
                            content_val = "\n".join(text_parts)
                        blocks.append({"type": "tool_result", "content_preview": redact_secrets(str(content_val)[:1000])})
                if blocks:
                    entries.append({"role": msg_type, "timestamp": timestamp, "blocks": blocks})
    except Exception:
        return None
    return entries if entries else None


def _read_dev_session_map() -> Dict[str, str]:
    """Read the persistent item_id → session_id mapping."""
    try:
        return json.loads(DEV_SESSION_MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _build_session_log() -> Optional[List[Dict[str, Any]]]:
    """Parse the active development session JSONL into a renderable log."""
    try:
        session_id = DEV_SESSION_ID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not session_id:
        return None
    return _parse_session_jsonl(session_id)


def _extract_active_dev_items(tasks_payload: Any) -> List[str]:
    """Extract backlog item IDs that have active development tasks."""
    active: set[str] = set()
    if not isinstance(tasks_payload, dict):
        return sorted(active)
    for bucket in ("queued", "active"):
        for task in (tasks_payload.get(bucket) or {}).values():
            if not isinstance(task, dict):
                continue
            if str(task.get("task_type") or "") == "development":
                desc = str(task.get("task_description") or "")
                if desc.startswith("Development: "):
                    active.add(desc[len("Development: "):].rstrip("."))
    return sorted(active)


# ---------------------------------------------------------------------------
# Top-level static site export
# ---------------------------------------------------------------------------

def write_static_site(out_dir: Path, *, skip_standalone: bool = False) -> None:
    """Export the complete static site to *out_dir*.

    This is the single entry point for static site generation:
    1. Collects data, sanitizes for public consumption
    2. Writes status.json, monitor, backlog (+ session data), roadmap pages
    3. Optionally writes standalone roadmap.html for docs/dev/
    """
    payload = sanitize_public_status(build_status())

    # status.json — raw data for client-side refresh
    atomic_write_text(
        out_dir / "status.json",
        json.dumps(payload, default=str, ensure_ascii=False, indent=2),
    )

    # Monitor page (index.html)
    bootstrap = {
        "heartbeat": payload.get("heartbeat"),
        "tasks": payload.get("tasks"),
        "backlog": payload.get("backlog"),
        "polling": payload.get("polling") if isinstance(payload.get("polling"), dict) else build_polling_info(),
        "visibility": payload.get("visibility") if isinstance(payload.get("visibility"), dict) else build_visibility_info("public_snapshot"),
    }
    write_monitor_html(out_dir, bootstrap)

    # Backlog page
    backlog_data = payload.get("backlog") or {"items": [], "completed": []}
    active_dev = _extract_active_dev_items(payload.get("tasks"))
    write_backlog_html(out_dir, backlog_data, active_dev_items=active_dev)

    # Per-item session files
    backlog_dir = out_dir / "backlog"
    backlog_dir.mkdir(parents=True, exist_ok=True)
    sessions_dir = backlog_dir / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_map = _read_dev_session_map()
    for item_id, sess_id in session_map.items():
        session_entries = _parse_session_jsonl(sess_id)
        if session_entries:
            atomic_write_text(
                sessions_dir / f"{item_id}.json",
                json.dumps(session_entries, default=str, ensure_ascii=False, indent=2),
            )
    # Legacy session.json for backwards compat
    session_log = _build_session_log()
    atomic_write_text(
        backlog_dir / "session.json",
        json.dumps(session_log or [], default=str, ensure_ascii=False, indent=2),
    )

    # Roadmap page
    roadmap_data = payload.get("roadmap") or {"vision": "", "themes": [], "last_updated": ""}
    write_roadmap_html(out_dir, roadmap_data)
    if not skip_standalone:
        write_standalone_roadmap_html(roadmap_data)


# ---------------------------------------------------------------------------
# Deploy publishing — git plumbing to push snapshots to an orphan branch
# ---------------------------------------------------------------------------

STATIC_EXPORT_TRACKED_FILES = (
    "index.html",
    "status.json",
    "backlog/index.html",
    "backlog/session.json",
    "roadmap/index.html",
)


def _run_git(repo_dir: Path, args: list, timeout_sec: int = 25):
    """Run a git command in *repo_dir* and return the CompletedProcess."""
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )


def _run_git_with_index(
    repo_dir: Path, args: list, index_file: str, timeout_sec: int = 25,
):
    """Run a git command with a custom GIT_INDEX_FILE environment variable."""
    env = {**os.environ, "GIT_INDEX_FILE": index_file}
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
        env=env,
    )


def _git_check(proc, operation: str) -> None:
    """Raise RuntimeError if a git subprocess failed."""
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"git {operation} failed: {detail or 'no details'}")


def publish_to_deploy_branch(
    out_dir: Path,
    remote: str,
    deploy_branch: str = "deploy",
    tracked_files: tuple = STATIC_EXPORT_TRACKED_FILES,
) -> Optional[str]:
    """Publish snapshot files to an orphan deploy branch using git plumbing.

    Builds a tree from the remote main branch's full content, overlays the
    generated snapshot files, and force-pushes as an orphan commit. The working
    tree and HEAD are never modified.

    Returns the commit hash on success, None if skipped (no changes).
    """
    # Guard: refuse to force-push to main/master.
    if deploy_branch in ("main", "master"):
        raise RuntimeError(
            f"Refusing to publish to '{deploy_branch}' — this would destroy the "
            f"development branch with an orphan commit. Set DASHBOARD_GIT_BRANCH "
            f"to a dedicated deploy branch (e.g., 'deploy')."
        )

    # Verify this is a git repo.
    git_dir_proc = _run_git(out_dir, ["rev-parse", "--git-dir"])
    _git_check(git_dir_proc, "rev-parse --git-dir")

    # Always fetch the latest main from the configured remote.
    fetch_proc = _run_git(out_dir, ["fetch", remote, "main"], timeout_sec=60)
    _git_check(fetch_proc, f"fetch {remote} main")

    # Read main's full tree from the remote-tracking ref.
    tree_proc = _run_git(out_dir, ["rev-parse", f"{remote}/main^{{tree}}"])
    _git_check(tree_proc, f"rev-parse {remote}/main^{{tree}}")
    main_tree = tree_proc.stdout.strip()

    # Create a temporary index file.
    fd, tmp_index = tempfile.mkstemp(prefix="deploy-idx-")
    os.close(fd)
    try:
        # Populate index from main's tree.
        _git_check(
            _run_git_with_index(out_dir, ["read-tree", main_tree], tmp_index),
            "read-tree",
        )

        # Clear stale snapshot entries before overlaying.
        _run_git_with_index(
            out_dir,
            ["rm", "--cached", "-r", "--ignore-unmatch", "--", *tracked_files],
            tmp_index,
        )

        # Also include per-item session files (backlog/sessions/) if present.
        sessions_path = out_dir / "backlog" / "sessions"
        all_overlay_files = list(tracked_files)
        if sessions_path.is_dir():
            for f in sessions_path.iterdir():
                if f.is_file():
                    all_overlay_files.append(f"backlog/sessions/{f.name}")

        # Overlay current snapshot files.
        for rel_path in all_overlay_files:
            abs_path = out_dir / rel_path
            if not abs_path.is_file():
                continue
            # Write blob to object store.
            blob_proc = _run_git(out_dir, ["hash-object", "-w", rel_path])
            _git_check(blob_proc, f"hash-object -w {rel_path}")
            blob_sha = blob_proc.stdout.strip()
            # Add to temp index.
            _git_check(
                _run_git_with_index(
                    out_dir,
                    ["update-index", "--add", "--cacheinfo", f"100644,{blob_sha},{rel_path}"],
                    tmp_index,
                ),
                f"update-index {rel_path}",
            )

        # Write tree from temp index.
        write_tree_proc = _run_git_with_index(out_dir, ["write-tree"], tmp_index)
        _git_check(write_tree_proc, "write-tree")
        new_tree = write_tree_proc.stdout.strip()
    finally:
        try:
            os.unlink(tmp_index)
        except OSError:
            pass

    # Skip detection: compare against remote deploy branch's tree.
    ls_remote_proc = _run_git(out_dir, ["ls-remote", remote, f"refs/heads/{deploy_branch}"])
    if ls_remote_proc.returncode == 0 and ls_remote_proc.stdout.strip():
        remote_sha = ls_remote_proc.stdout.strip().split()[0]
        # Fetch the remote commit so we can inspect its tree.
        _run_git(out_dir, ["fetch", remote, remote_sha], timeout_sec=60)
        remote_tree_proc = _run_git(out_dir, ["rev-parse", f"{remote_sha}^{{tree}}"])
        if remote_tree_proc.returncode == 0:
            remote_tree = remote_tree_proc.stdout.strip()
            if remote_tree == new_tree:
                return None  # No changes.

    # Create orphan commit (no parent).
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit_proc = _run_git(
        out_dir, ["commit-tree", new_tree, "-m", f"deploy: snapshot {timestamp}"]
    )
    _git_check(commit_proc, "commit-tree")
    commit_sha = commit_proc.stdout.strip()

    # Force-push via explicit refspec (no local ref update).
    push_proc = _run_git(
        out_dir,
        ["push", "--force", remote, f"{commit_sha}:refs/heads/{deploy_branch}"],
        timeout_sec=60,
    )
    _git_check(push_proc, f"push --force {remote} {deploy_branch}")

    return commit_sha


# ---------------------------------------------------------------------------
# Config helpers (ported from dashboard.py for CLI entry point)
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load .env with setdefault semantics (env vars take precedence)."""
    dotenv = BASE_DIR / ".env"
    if not dotenv.exists():
        return
    for raw in dotenv.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _reload_config_file() -> None:
    """Re-resolve the config file path from env (call after _load_dotenv)."""
    # No-op in generator — resolve_config_file() already reads env each call.
    pass


def configure_gpu_monitor(
    enabled: bool,
    node_alias: str,
    poll_interval_sec: Optional[int],
    command_timeout_sec: int,
) -> Dict[str, Any]:
    """Build a GPU monitor configuration dict.

    Unlike dashboard.py's threaded version, this returns the config dict
    instead of mutating global state — generator.py is stateless.
    """
    resolved_poll_interval = (
        poll_interval_sec if poll_interval_sec is not None
        else resolve_supervisor_poll_interval_sec()
    )
    poll_interval = max(int(resolved_poll_interval), 30)
    timeout = max(int(command_timeout_sec), 2)
    return {
        "enabled": bool(enabled),
        "node_alias": node_alias or "tianhaowang-gpu0",
        "poll_interval_sec": poll_interval,
        "command_timeout_sec": timeout,
        "last_polled_ts": 0.0,
        "last_result": default_gpu_status(
            enabled=bool(enabled),
            node_alias=node_alias or "tianhaowang-gpu0",
        ),
    }


def resolve_writer_interval(interval_arg: Optional[int]) -> int:
    """Resolve the static export write interval in seconds."""
    if interval_arg is not None and interval_arg > 0:
        return interval_arg
    return resolve_supervisor_poll_interval_sec()


def local_ips() -> List[str]:
    """Return a sorted list of non-loopback IPv4 addresses for this host."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if not addr.startswith("127.") and ":" not in addr:
                ips.add(addr)
    except Exception:
        pass
    return sorted(ips)


# ---------------------------------------------------------------------------
# Public site (landing page) assembly
# ---------------------------------------------------------------------------

def write_public_site(
    out_dir: Path,
    *,
    gpu_data: Optional[Dict[str, Any]] = None,
) -> None:
    """Assemble the public landing site with injected status data."""
    out_dir.mkdir(parents=True, exist_ok=True)

    status = build_public_status(gpu_data=gpu_data)
    status_json = json.dumps(status, default=str, ensure_ascii=False)

    html = render_template("landing.html.jinja2", {
        "status_json": status_json,
    })
    if html is None:
        raise RuntimeError(
            "Failed to render landing.html.jinja2 — is jinja2 installed?"
        )
    atomic_write_text(out_dir / "index.html", html)

    # Copy static showcase content (only if not already present)
    for dirname in SHOWCASE_COPY_DIRS:
        src = SHOWCASE_SOURCE_DIR / dirname
        dst = out_dir / dirname
        if src.is_dir() and not dst.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)


def publish_to_cloudflare_pages(
    out_dir: Path, project_name: str,
) -> bool:
    """Deploy public site to Cloudflare Pages via wrangler."""
    try:
        result = subprocess.run(
            [
                "wrangler", "pages", "deploy", str(out_dir),
                "--project-name", project_name, "--commit-dirty=true",
            ],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True
        print(
            f"[writer] cf-pages error: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return False
    except FileNotFoundError:
        print(
            "[writer] cf-pages error: wrangler not found on PATH",
            file=sys.stderr,
        )
        return False
    except Exception as exc:
        print(f"[writer] cf-pages error: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Static export loop
# ---------------------------------------------------------------------------

def static_export_loop(
    interval: int,
    static_dir: Path,
    static_git_push: bool = False,
    static_git_remote: str = "origin",
    static_git_branch: Optional[str] = None,
    cf_pages_enabled: bool = False,
    cf_pages_project: str = "murphy-agent-site",
    cf_pages_dir: Optional[Path] = None,
    cf_pages_interval: int = 900,
) -> None:
    """Continuously export the static site at a fixed interval."""
    cycle = 0
    cf_every_n = max(1, cf_pages_interval // max(interval, 1))
    while True:
        if cycle > 0:
            time.sleep(interval)
        try:
            write_static_site(static_dir)
            if static_git_push:
                commit_hash = publish_to_deploy_branch(
                    static_dir, static_git_remote,
                    static_git_branch or "deploy",
                )
                if commit_hash:
                    print(
                        f"[writer] deploy publish {commit_hash} -> "
                        f"{static_git_remote}:{static_git_branch or 'deploy'}"
                    )
        except Exception as exc:
            print(f"[writer] error: {exc}", file=sys.stderr)

        # Cloudflare Pages public site deploy
        if cf_pages_enabled and cf_pages_dir and (cycle % cf_every_n == 0):
            try:
                write_public_site(cf_pages_dir)
                if publish_to_cloudflare_pages(cf_pages_dir, cf_pages_project):
                    print(f"[writer] cf-pages deploy -> {cf_pages_project}")
            except Exception as exc:
                print(f"[writer] cf-pages error: {exc}", file=sys.stderr)

        cycle += 1


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Static site generator — data collection, Jinja2 rendering, "
            "and deploy publishing."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--interval", type=int,
        help="Static export write interval in seconds "
             "(defaults to root SLEEP_NORMAL)",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Write static export once and exit (requires --export-dir)",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run static export loop in the foreground",
    )
    parser.add_argument(
        "--from-config", action="store_true",
        help="Load config from .env and DASHBOARD_* env vars "
             "(supervisor-compatible semantics)",
    )
    parser.add_argument(
        "--export-dir", type=Path, dest="export_dir",
        help="Static export directory "
             "(writes index.html + status.json for GitHub Pages)",
    )
    # Legacy alias for backwards compat with dashboard.py invocations
    parser.add_argument(
        "--export-static-dir", type=Path, dest="export_static_dir_compat",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--static-git-push", choices=("off", "on"), default=None,
        help="Commit+push snapshot updates on each write (default: off)",
    )
    parser.add_argument(
        "--static-git-remote", default=None,
        help="Git remote name for static export push mode (default: origin)",
    )
    parser.add_argument(
        "--static-git-branch", default=None,
        help="Target branch for static export push mode (default: deploy)",
    )
    parser.add_argument(
        "--gpu-monitor", choices=("off", "on"), default=None,
        help="Remote GPU snapshot via SSH (default: on)",
    )
    parser.add_argument(
        "--gpu-node-alias", default=None,
        help="SSH alias for GPU node (default: tianhaowang-gpu0)",
    )
    parser.add_argument(
        "--gpu-poll-interval-sec", type=int,
        help="Remote GPU poll interval in seconds when enabled",
    )
    parser.add_argument(
        "--gpu-command-timeout-sec", type=int, default=None,
        help="Timeout in seconds for each remote GPU command (default: 4)",
    )
    args = parser.parse_args()

    # Merge legacy --export-static-dir into --export-dir
    if args.export_static_dir_compat and not args.export_dir:
        args.export_dir = args.export_static_dir_compat

    # --from-config: load .env, populate args from
    # env vars -> supervisor_loop.conf -> hardcoded defaults
    if args.from_config:
        _load_dotenv()
        _reload_config_file()

        # Fail fast if an explicitly-set config file doesn't exist
        explicit_config = os.environ.get("LOOP_CONFIG_FILE")
        if explicit_config and not resolve_config_file().exists():
            print(
                f"Error: config file not found: {resolve_config_file()}",
                file=sys.stderr,
            )
            sys.exit(1)

        def _cfg(name: str, fallback: str) -> str:
            """Read config: env var -> supervisor_loop.conf -> fallback."""
            val = os.environ.get(name)
            if val is not None:
                return val
            val = read_supervisor_default(name)
            if val is not None:
                return val
            return fallback

        if not parse_bool(_cfg("DASHBOARD_EXPORT_ENABLED", "true")):
            print("Dashboard export disabled (DASHBOARD_EXPORT_ENABLED)")
            return
        if args.export_dir is None:
            args.export_dir = Path(
                _cfg("DASHBOARD_EXPORT_DIR", "projects/agent-monitor-web")
            )
        if args.static_git_push is None:
            args.static_git_push = (
                "on" if parse_bool(_cfg("DASHBOARD_GIT_PUSH", "true"))
                else "off"
            )
        if args.static_git_remote is None:
            args.static_git_remote = _cfg("DASHBOARD_GIT_REMOTE", "origin")
        if args.static_git_branch is None:
            args.static_git_branch = _cfg("DASHBOARD_GIT_BRANCH", "deploy")
        # Cloudflare Pages config
        args.cf_pages_enabled = parse_bool(
            _cfg("DASHBOARD_CF_PAGES_ENABLED", "false")
        )
        args.cf_pages_project = _cfg(
            "DASHBOARD_CF_PAGES_PROJECT", "murphy-agent-site"
        )
        args.cf_pages_dir = Path(
            _cfg("DASHBOARD_CF_PAGES_DIR", ".agent/runtime/public-site")
        )
        args.cf_pages_interval = int(
            _cfg("DASHBOARD_CF_PAGES_INTERVAL", "900")
        )

        if args.gpu_monitor is None:
            args.gpu_monitor = (
                "on" if parse_bool(_cfg("DASHBOARD_GPU_MONITOR", "true"))
                else "off"
            )
        if args.gpu_node_alias is None:
            args.gpu_node_alias = _cfg(
                "DASHBOARD_GPU_NODE_ALIAS", "tianhaowang-gpu0"
            )
        if args.gpu_command_timeout_sec is None:
            args.gpu_command_timeout_sec = int(
                _cfg("DASHBOARD_GPU_COMMAND_TIMEOUT", "4")
            )

    # Apply defaults for args not set by --from-config or CLI
    if not hasattr(args, "cf_pages_enabled"):
        args.cf_pages_enabled = False
        args.cf_pages_project = "murphy-agent-site"
        args.cf_pages_dir = Path(".agent/runtime/public-site")
        args.cf_pages_interval = 900
    if args.static_git_push is None:
        args.static_git_push = "off"
    if args.static_git_remote is None:
        args.static_git_remote = "origin"
    if args.gpu_monitor is None:
        args.gpu_monitor = "on"
    if args.gpu_node_alias is None:
        args.gpu_node_alias = "tianhaowang-gpu0"
    if args.gpu_command_timeout_sec is None:
        args.gpu_command_timeout_sec = 4

    gpu_poll_interval = (
        args.gpu_poll_interval_sec
        if args.gpu_poll_interval_sec is not None
        else resolve_supervisor_poll_interval_sec()
    )

    gpu_cfg = configure_gpu_monitor(
        enabled=(args.gpu_monitor == "on"),
        node_alias=args.gpu_node_alias,
        poll_interval_sec=gpu_poll_interval,
        command_timeout_sec=args.gpu_command_timeout_sec,
    )
    writer_interval = resolve_writer_interval(args.interval)
    static_git_push_enabled = args.static_git_push == "on"

    if args.gpu_monitor == "on":
        print(
            f"GPU monitor: enabled ({args.gpu_node_alias}, "
            f"poll={gpu_cfg['poll_interval_sec']}s, "
            f"timeout={gpu_cfg['command_timeout_sec']}s)"
        )
    else:
        print(
            "GPU monitor: disabled for this run "
            "(set --gpu-monitor on to enable)"
        )

    if args.export_dir:
        # Fail fast if git push is enabled but the export dir is not a
        # git repo.
        git_marker = args.export_dir / ".git"
        if static_git_push_enabled and not (
            git_marker.is_dir() or git_marker.is_file()
        ):
            print(
                f"Error: export dir is not a git repo: {args.export_dir} "
                f"(git push is enabled but .git is missing)",
                file=sys.stderr,
            )
            sys.exit(1)
        write_static_site(args.export_dir, skip_standalone=args.once)
        print(f"Static export: {args.export_dir / 'index.html'}")
        print(f"Static status: {args.export_dir / 'status.json'}")
        if static_git_push_enabled:
            try:
                deploy_branch = args.static_git_branch or "deploy"
                commit_hash = publish_to_deploy_branch(
                    args.export_dir, args.static_git_remote, deploy_branch,
                )
                if commit_hash:
                    print(
                        f"Deploy publish: {commit_hash} -> "
                        f"{args.static_git_remote}:{deploy_branch}"
                    )
                else:
                    print("Deploy publish: no changes (tree unchanged)")
            except Exception as exc:
                print(f"Deploy publish error: {exc}", file=sys.stderr)

    if args.once:
        return

    # Headless mode: run static export loop (no HTTP server in generator)
    if args.headless or args.export_dir:
        if not args.export_dir:
            print(
                "Error: --headless requires --export-dir (or --from-config)",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Static export cadence: every {writer_interval}s")
        if static_git_push_enabled:
            print("Static git push mode: enabled")
        if args.cf_pages_enabled:
            print(
                f"Cloudflare Pages: {args.cf_pages_project} "
                f"every {args.cf_pages_interval}s"
            )
        print("Press Ctrl-C to stop.")
        try:
            static_export_loop(
                writer_interval, args.export_dir,
                static_git_push_enabled, args.static_git_remote,
                args.static_git_branch,
                args.cf_pages_enabled, args.cf_pages_project,
                args.cf_pages_dir, args.cf_pages_interval,
            )
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    # No --export-dir and no --once — nothing to do in static-only mode
    print(
        "No export directory specified. Use --export-dir or --from-config.",
        file=sys.stderr,
    )
    parser.print_usage(sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
