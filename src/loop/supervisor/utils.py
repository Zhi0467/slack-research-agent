#!/usr/bin/env python3
"""Shared utility helpers for the supervisor loop."""

from __future__ import annotations

import hashlib
import os
import re
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

TRANSIENT_PATTERN = re.compile(
    r"stream disconnected before completion|transport error|network error|error decoding response body|"
    r"connection (?:reset|closed)|broken pipe|unexpected eof|tls|temporarily unavailable|"
    r"API Error:\s*(?:429|500|502|503|529)|overloaded_error|api_error.*Internal server error",
    re.IGNORECASE,
)

CAPACITY_PATTERN = re.compile(
    r"429|capacity.?exhausted|resource.?exhausted|rate.?limit|overloaded|quota",
    re.IGNORECASE,
)

AUTH_FAILURE_PATTERN = re.compile(
    r"authentication_error|OAuth token has expired|401.*authentication|"
    r"Failed to authenticate.*API Error.*401",
    re.IGNORECASE,
)

MCP_STARTUP_FAILURE_PATTERN = re.compile(
    r"mcp.*(?:server|connection).*(?:fail|error|refused|timed?\s*out)|"
    r"failed.*(?:start|connect|initialize).*mcp|"
    r"(?:slack|consult).*mcp.*(?:fail|error|unavailable)|"
    r"error.*(?:starting|connecting).*mcp.*server",
    re.IGNORECASE,
)

THREAD_TS_RE = re.compile(r"thread_ts=([0-9]+\.[0-9]+)")
CHANNEL_ID_RE = re.compile(r"/archives/([A-Z0-9]+)")

# Merge-result constants for parallel reconcile (plan 09).
MERGE_NO_UNMERGED = "no_unmerged_commits"
MERGE_FF = "ff_merged"
MERGE_FALLBACK_OK = "fallback_merged"
MERGE_FALLBACK_FAILED = "fallback_failed"
MERGE_CHECK_ERROR = "merge_check_error"


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_ts() -> str:
    return f"{time.time():.6f}"


def ts_to_int(ts: str) -> int:
    if not ts:
        return 0
    if "." in ts:
        sec, frac = ts.split(".", 1)
    else:
        sec, frac = ts, "0"
    sec = re.sub(r"[^0-9]", "", sec) or "0"
    frac = re.sub(r"[^0-9]", "", frac)[:6].ljust(6, "0")
    return int(f"{sec}{frac}")


def ts_gt(a: str, b: str) -> bool:
    return ts_to_int(a) > ts_to_int(b)


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def iso_from_ts_floor(ts: str) -> str:
    try:
        sec = int(ts.split(".")[0]) if ts else 0
        return datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return ""


def short_ts_format(ts: str) -> str:
    """Short human-readable timestamp: '2026 Mar 22 00:53 UTC'."""
    if not ts:
        return ""
    try:
        sec = int(ts.split(".")[0])
        if not sec:
            return ""
        return datetime.fromtimestamp(sec, tz=timezone.utc).strftime("%Y %b %d %H:%M UTC")
    except Exception:
        return ""


def _classify_thread_message(m: Dict[str, str], slack_id: str) -> Dict[str, str]:
    """Classify a raw Slack thread message into user_id, role, role_detail, and cleaned text."""
    user = m.get("user") or ""
    role = "unknown"
    user_id = "unknown"
    if user:
        user_id = user
        role = "agent" if user == slack_id else "human"
    elif m.get("username"):
        role = "bot"
        if m.get("bot_id"):
            user_id = f"bot:{m['bot_id']}"
    elif m.get("bot_id"):
        role = "bot"
        user_id = f"bot:{m['bot_id']}"

    text = re.sub(r"\s+", " ", m.get("text") or "").strip()
    if not text:
        text = m.get("files_summary") or "[empty message]"

    role_detail = role
    username = str(m.get("username") or "").strip()
    if username and role == "bot":
        role_detail = f"bot:{username}"

    return {
        "ts": m.get("ts", ""),
        "user_id": user_id,
        "role": role_detail,
        "text": text,
    }


def format_waiting_human_context_messages(
    thread_messages: List[Dict[str, str]], slack_id: str
) -> List[Dict[str, str]]:
    """Return structured message dicts for the context snapshot (for JSON task files)."""
    result: List[Dict[str, str]] = []
    for m in thread_messages:
        classified = _classify_thread_message(m, slack_id)
        classified["source"] = "context_snapshot"
        result.append(classified)
    return result


# ---------------------------------------------------------------------------
# Plan 37 — session resume foundations
# ---------------------------------------------------------------------------

# Files whose content defines the worker behavioral contract.  A change in any
# of these files means a resumed session would run on stale instructions, so
# the supervisor should force a fresh dispatch when the hash changes.
SYSTEM_PROMPT_FILES = (
    "src/prompts/session.md",
    "src/prompts/loop_context.md",
    "src/prompts/merge_instructions.md",
    "src/prompts/session_resume.md",
    "AGENTS.md",
    "docs/protocols/slack-protocols.md",
    "docs/protocols/maintenance-protocols.md",
    "docs/workflows/git-workflow.md",
    "docs/workflows/research-workflow.md",
    "docs/workflows/remote-gpu-workflow.md",
    "docs/workflows/project-workflow.md",
    "docs/workflows/paper-writing-workflow.md",
    "docs/mcp-integrations.md",
    "docs/persistent-files.md",
    "docs/schemas.md",
)


def system_prompt_hash(root: Optional[Path] = None) -> str:
    """SHA-256 digest of the concatenated worker behavioral contract files.

    Returns the first 16 hex chars (64 bits) — enough to detect changes
    reliably without bloating task JSON.  Missing files are hashed as empty
    (so the hash still changes when a file is added or removed).
    """
    h = hashlib.sha256()
    base = root or Path.cwd()
    for rel in SYSTEM_PROMPT_FILES:
        p = base / rel
        try:
            h.update(p.read_bytes())
        except OSError:
            pass  # missing file hashed as empty
    return h.hexdigest()[:16]


# Pattern to capture the codex session ID from worker output.
# codex prints "session id: <UUID>" in its startup header.
_CODEX_SESSION_ID_RE = re.compile(r"session id:\s*([0-9a-f-]{36})", re.IGNORECASE)


def capture_codex_session_id(worker_output: str) -> Optional[str]:
    """Extract the codex session UUID from worker stdout/stderr output.

    Returns the UUID string or None if not found.
    """
    m = _CODEX_SESSION_ID_RE.search(worker_output)
    return m.group(1) if m else None


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def resolve_default_expr(expr: str) -> str:
    expr = expr.strip()

    # ${VAR:-fallback}
    m = re.fullmatch(r"\$\{([A-Z0-9_]+):-([^}]*)\}", expr)
    if m:
        var, fallback = m.group(1), m.group(2)
        return os.environ.get(var, fallback)

    # $(hostname)-agent
    if "$(hostname)" in expr:
        return expr.replace("$(hostname)", socket.gethostname())

    return expr


def parse_conf_defaults(path: Path) -> Dict[str, str]:
    defaults: Dict[str, str] = {}
    if not path.exists():
        return defaults

    pattern = re.compile(r'^\s*:\s*"\$\{([A-Z0-9_]+):=(.*)\}"\s*$')
    for line in path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if not m:
            continue
        key, raw = m.group(1), m.group(2)
        defaults[key] = resolve_default_expr(raw)
    return defaults
