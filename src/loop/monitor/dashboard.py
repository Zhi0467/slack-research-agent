#!/usr/bin/env python3
"""
Agent monitoring dashboard.

HTTP server on PORT — open in any browser via port forwarding.
  GET /            serves the live dashboard HTML
  GET /api/status  returns live JSON

Usage:
    python3 -m src.loop.monitor.dashboard              # port 8765
    python3 -m src.loop.monitor.dashboard 9000         # custom port
    python3 -m src.loop.monitor.dashboard --once --export-static-dir dashboard-export
                                                       # write GitHub Pages-ready index.html + status.json
    python3 -m src.loop.monitor.dashboard --export-static-dir dashboard-export
                                                       # continuous static export (interval defaults to root SLEEP_NORMAL)
    python3 -m src.loop.monitor.dashboard --export-static-dir dashboard-export \\
      --static-git-push on --static-git-branch task/live-monitor
                                                       # optional commit/push updates for hosted Pages branch
    python3 -m src.loop.monitor.dashboard --gpu-monitor on --gpu-node-alias gpu-node
                                                       # optional low-frequency remote GPU snapshot
"""

import argparse
import copy
import html as html_mod
import http.server
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

BASE_DIR  = Path(__file__).resolve().parents[3]
AGENT_DIR = BASE_DIR / ".agent"
PROCESS_START_TS = time.time()
REDACTED_SECRET = "[REDACTED_SECRET]"
SECRET_PATTERNS = [
    re.compile(r"hf_[A-Za-z0-9]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]
WHITESPACE_RE = re.compile(r"\s+")
CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"`[^`]+`")
URL_RE = re.compile(r"https?://\S+")
MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://[^)]+)\)")
TASK_MESSAGE_BLOCK_RE = re.compile(r"- Message:\n```(?:text)?\n(.*?)\n```", re.DOTALL)
TASK_HUMAN_CONTEXT_BLOCK_RE = re.compile(r"\| role: human\]\n(.+?)(?=\n\n\[|\Z)", re.DOTALL)
TASK_SLACK_MENTION_RE = re.compile(r"<@[^>]+>")
TASK_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
TASK_USER_ID_LINE_RE = re.compile(r"^(\s*- User ID:\s*).*$", re.MULTILINE)
TASK_USER_NAME_LINE_RE = re.compile(r"^(\s*- User Name:\s*).*$", re.MULTILINE)
TASK_JSON_USER_ID_RE = re.compile(r'("user_id"\s*:\s*")[^"]*(")')
TASK_JSON_USER_NAME_RE = re.compile(r'("user_name"\s*:\s*")[^"]*(")')
TASK_CONTEXT_USER_FIELD_RE = re.compile(r"(\|\s*user:\s*)[^\s\|]+")
DEFAULT_LOCAL_WRITE_INTERVAL_SEC = 2
GPU_STATE_LOCK = threading.Lock()
GPU_MONITOR = {
    "enabled": False,
    "node_alias": "",
    "poll_interval_sec": DEFAULT_LOCAL_WRITE_INTERVAL_SEC,
    "command_timeout_sec": 4,
    "last_polled_ts": 0.0,
    "last_result": None,
}
def _resolve_config_file() -> Path:
    """Honor LOOP_CONFIG_FILE env var, fallback to default."""
    override = os.environ.get("LOOP_CONFIG_FILE")
    if override:
        p = Path(override)
        return p if p.is_absolute() else BASE_DIR / p
    return BASE_DIR / "src/config/supervisor_loop.conf"

SUPERVISOR_LOOP_CONFIG_FILE = _resolve_config_file()
SUPERVISOR_DEFAULT_RE = re.compile(r'^\s*:\s*"\$\{([A-Z0-9_]+):=(.*)\}"\s*$')
STATIC_EXPORT_TRACKED_FILES = ("index.html", "status.json", "backlog/index.html", "backlog/session.json", "roadmap/index.html")
SHOWCASE_SOURCE_DIR = BASE_DIR / "projects" / "agent-monitor-web"
SHOWCASE_COPY_DIRS = ("showcase", "tokenizers", "assets")
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
SITE_LINKS = [
    {"label": "Repo", "href": "https://github.com/Zhi0467/slack-research-agent", "desc": "Project repository"},
    {"label": "Showcase", "href": "showcase/", "desc": "Browser tools and demos"},
]
ROADMAP_THEME_PUBLIC_COPY = {
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
ROADMAP_GOAL_PUBLIC_COPY = {
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
    "Athena integration": (
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
BACKLOG_PUBLIC_RULES = [
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
ROADMAP_VISION_FALLBACK = (
    "An autonomous research system that can investigate, build, write, and publish on a steady public cadence."
)


def summarize_checkpoint_rollup(milestones):
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


def publicize_roadmap_data(roadmap_data):
    safe = copy.deepcopy(roadmap_data or {})
    vision = summarize_task_description(safe.get("vision") or "", limit=220)
    if vision:
        vision = (
            vision.replace("agent", "system")
            .replace("independently conducts, writes up, and delivers high-quality research while maintaining its own infrastructure", "investigates, builds, writes, and ships work on a steady public cadence")
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
            (theme_name or "Untitled lane", summarize_task_description(theme.get("description") or "", limit=180)),
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
                (goal_name or "Untitled goal", summarize_task_description(goal.get("description") or "", limit=180)),
            )
            public_goal = dict(goal)
            public_goal["name"] = public_goal_name
            public_goal["description"] = public_goal_desc
            public_goal.update(summarize_checkpoint_rollup(goal.get("milestones") or []))
            public_goals.append(public_goal)

        public_theme["goals"] = public_goals
        public_themes.append(public_theme)

    safe["themes"] = public_themes
    return safe


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------
# The page polls /api/status via fetch() for live updates.

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agent Monitor</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Space+Grotesk:wght@300&family=Playfair+Display:ital,wght@1,700&family=Caveat:wght@700&family=Permanent+Marker&family=Cormorant+Garamond:ital,wght@1,600&family=Barlow+Condensed:ital,wght@1,600&family=Press+Start+2P&family=Audiowide&family=Black+Ops+One&family=Monoton&family=Major+Mono+Display&family=Righteous&family=Bungee&family=Russo+One&family=Staatliches&family=Cinzel:wght@700&family=Gruppo&family=Megrim&family=Poiret+One&family=Michroma&family=Nova+Mono&family=Syncopate:wght@700&family=Vast+Shadow&family=Silkscreen&family=Notable&family=Bungee+Shade&family=Iceland&family=Share+Tech+Mono&family=VT323&family=Special+Elite&family=Coda:wght@800&family=Teko:wght@600&family=Aldrich&family=Electrolize&family=Share+Tech&family=Play:wght@700&family=Quantico:wght@700&family=Kanit:wght@600&family=Tomorrow:wght@600&family=Chakra+Petch:wght@600&family=Stint+Ultra+Expanded&family=Abril+Fatface&family=Fascinate&family=Wallpoet&family=Nosifer&family=Lacquer&family=Rubik+Glitch&display=swap');
  :root {{
    --bg-base: #000; --bg-border: rgba(56,240,255,0.5); --bg-border-strong: rgba(56,240,255,0.8);
    --text-main: #eaffff; --text-muted: #99a6d8; --text-dim: #6d79ad;
    --brand: #38f0ff; --brand-alt: #ff2fb3;
    --good: #4dffb4; --warn: #ffe66a; --bad: #ff6689;
    --stack-queued-border: rgba(47,216,255,0.5); --stack-active-border: rgba(56,255,183,0.5);
    --stack-incomplete-border: rgba(255,186,74,0.5); --stack-finished-border: rgba(255,75,193,0.4);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Rajdhani", "Avenir Next", "Segoe UI", sans-serif;
    background: #000;
    color: var(--text-main); font-size: 15px;
    min-height: 100vh; position: relative; overflow-x: hidden;
  }}

  header {{
    display: flex; align-items: center; gap: 12px;
    padding: 14px 20px; background: #000;
    border-bottom: 3px solid #38f0ff;
    flex-wrap: wrap; position: sticky; top: 0; z-index: 2;
  }}
  header h1 {{
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 18px; font-weight: 700; letter-spacing: 0.15em;
    color: #e9ffff;
    text-shadow: 3px 0 #ff2fb3, -3px 0 #38f0ff;
  }}
  #last-updated {{ margin-left: auto; color: var(--text-muted); font-size: 12px; }}

  .pill {{
    display: inline-block; padding: 3px 10px; border-radius: 0;
    border: 2px solid transparent;
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase; text-shadow: 0 0 6px rgba(255,255,255,0.24);
  }}
  .pill-running  {{ background: rgba(26,76,63,0.4); border-color: rgba(77,255,180,0.5); color: var(--good); }}
  .pill-sleeping {{ background: rgba(40,48,86,0.38); border-color: rgba(87,110,182,0.44); color: var(--text-muted); }}
  .pill-pending  {{ background: rgba(88,58,8,0.42); border-color: rgba(255,230,106,0.56); color: var(--warn); }}
  .pill-retrying {{ background: rgba(95,26,46,0.44); border-color: rgba(255,102,137,0.56); color: var(--bad); }}
  .pill-unknown  {{ background: rgba(34,38,69,0.36); border-color: rgba(98,106,162,0.38); color: #acb6dd; }}
  .pill-done     {{ background: rgba(26,76,63,0.4); border-color: rgba(77,255,180,0.5); color: var(--good); }}
  .pill-inprog   {{ background: rgba(12,70,88,0.4); border-color: rgba(56,240,255,0.58); color: var(--brand); }}
  .pill-waiting  {{ background: rgba(88,58,8,0.42); border-color: rgba(255,230,106,0.56); color: var(--warn); }}
  .pill-queued   {{ background: rgba(34,38,69,0.36); border-color: rgba(98,106,162,0.38); color: #acb6dd; }}
  .pill-failed   {{ background: rgba(95,26,46,0.44); border-color: rgba(255,102,137,0.56); color: var(--bad); }}

  main {{ padding: 16px 20px 24px; display: grid; gap: 10px; max-width: 1450px; margin: 0 auto; position: relative; z-index: 1; }}
  .card {{
    background: #000; border: 2px solid rgba(56,240,255,0.5); border-radius: 0;
    overflow: hidden;
    box-shadow: 4px 4px 0 rgba(255,47,179,0.3), -2px -2px 0 rgba(56,240,255,0.2);
    animation: riseIn 320ms ease-out both;
  }}
  .card:hover {{ box-shadow: 6px 6px 0 rgba(255,47,179,0.5), -3px -3px 0 rgba(56,240,255,0.3); border-color: rgba(56,240,255,0.8); }}
  .card-header {{
    padding: 10px 16px;
    background: rgba(56,240,255,0.06);
    border-bottom: 2px solid rgba(56,240,255,0.3);
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.12em; color: #b8f4ff;
    display: flex; align-items: center; gap: 8px;
  }}
  .card-body {{ padding: 14px 16px; }}

  .hb-grid  {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(110px,1fr)); gap: 4px; }}
  .hb-item  {{ display: flex; flex-direction: column; gap: 2px; background: #000; border: 1px solid rgba(56,240,255,0.3); border-radius: 0; padding: 6px 10px; }}
  .hb-label {{ font-size: 9px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.1em; }}
  .hb-val   {{ font-size: 14px; color: var(--text-main); font-weight: 700; }}
  .hb-note {{ margin-top: 10px; color: #9eb7ff; font-size: 12px; line-height: 1.45; }}
  .hb-note-warn {{ color: var(--warn); }}

  .system-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }}
  .sys-item {{ border: 1px solid rgba(56,240,255,0.3); border-radius: 0; background: #000; padding: 10px 12px; }}
  .sys-label {{ color: var(--text-dim); font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.7px; }}
  .sys-val {{ color: var(--text-main); font-size: 13px; font-weight: 500; margin-top: 5px; word-break: break-word; }}

  .stack-grid {{ display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }}
  .stack-col {{
    border: 2px solid rgba(72,226,255,0.35); border-radius: 0;
    background: #000; display: flex; flex-direction: column; min-height: 220px;
  }}
  .stack-head {{
    padding: 9px 10px; border-bottom: 2px solid rgba(74,239,255,0.26);
    display: flex; align-items: center; justify-content: space-between;
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 11px; color: var(--text-muted); text-transform: uppercase;
    letter-spacing: 0.08em; font-weight: 700;
  }}
  .stack-count {{ background: rgba(19,26,46,0.85); border-radius: 999px; border: 1px solid rgba(86,166,255,0.36); padding: 1px 8px; color: #dce2f3; font-size: 11px; }}
  .stack-list {{ display: grid; gap: 8px; padding: 10px; align-content: start; max-height: 300px; overflow-y: auto; }}
  .stack-item {{ border: 1px solid rgba(56,240,255,0.2); border-radius: 0; padding: 8px; background: #000; }}
  .stack-item-title {{
    font-size: 12px; line-height: 1.4; color: #d9f1ff; margin-bottom: 6px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }}
  .stack-item-meta {{ display: flex; justify-content: space-between; gap: 10px; font-size: 11px; color: #a7b4df; }}
  .stack-empty {{ color: #6578ac; font-style: italic; font-size: 12px; padding: 10px; }}
  .stack-col.stack-queued {{ border-color: var(--stack-queued-border); background: #000; }}
  .stack-col.stack-active {{ border-color: var(--stack-active-border); background: #000; }}
  .stack-col.stack-incomplete {{ border-color: var(--stack-incomplete-border); background: #000; }}
  .stack-col.stack-finished {{ border-color: var(--stack-finished-border); background: #000; }}
  .stack-col.stack-queued .stack-head {{ border-bottom-color: rgba(47,216,255,0.44); color: #dbf8ff; }}
  .stack-col.stack-active .stack-head {{ border-bottom-color: rgba(56,255,183,0.45); color: #dbffe8; }}
  .stack-col.stack-incomplete .stack-head {{ border-bottom-color: rgba(255,186,74,0.45); color: #ffeac4; }}
  .stack-col.stack-finished .stack-head {{ border-bottom-color: rgba(255,75,193,0.45); color: #ffd9f1; }}

  .task-text {{ line-height: 1.5; color: #cde9ff; max-height: 88px; overflow-y: auto; word-break: break-word; margin-bottom: 10px; white-space: pre-wrap; }}
  .task-meta {{ display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; color: var(--text-muted); }}
  .task-meta span b {{ color: #bdd8ff; font-weight: 600; }}

  .tab-bar {{ display: flex; border-bottom: 1px solid rgba(74,239,255,0.26); }}
  .tab-btn  {{ padding: 8px 16px; font-size: 12px; font-weight: 600; cursor: pointer;
               background: none; border: none; color: var(--text-dim); border-bottom: 2px solid transparent; }}
  .tab-btn:hover  {{ color: #d4e4ff; }}
  .tab-btn.active {{ color: var(--brand); border-bottom-color: var(--brand); }}
  .tab-content        {{ display: none; }}
  .tab-content.active {{ display: block; }}

  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px 10px; font-size: 11px; color: #8fa3d8;
        text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid rgba(74,239,255,0.26); }}
  td {{ padding: 9px 10px; border-bottom: 1px solid rgba(60,90,164,0.35); vertical-align: top; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: rgba(27,36,72,0.78); }}
  .cell-text {{ max-width: 420px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #d4e8ff; }}
  .cell-user {{ color: var(--text-muted); white-space: nowrap; }}
  .cell-age  {{ color: var(--text-dim); white-space: nowrap; text-align: right; }}
  .empty-row {{ color: #6f7fb2; font-style: italic; padding: 16px 10px; }}

  #refresh-btn {{
    padding: 5px 12px; border-radius: 0;
    border: 2px solid rgba(56,240,255,0.5); background: #000;
    color: #d9f6ff; font-size: 12px; font-weight: 600; cursor: pointer;
    box-shadow: 2px 2px 0 rgba(255,47,179,0.3);
  }}
  #refresh-btn:hover {{ box-shadow: 3px 3px 0 rgba(255,47,179,0.5); }}
  #refresh-btn:disabled {{ opacity: 0.7; cursor: wait; }}

  .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; }}
  .dot-green  {{ background: var(--good); box-shadow: 0 0 8px rgba(77,255,180,0.86); }}
  .dot-yellow {{ background: var(--warn); box-shadow: 0 0 8px rgba(255,230,106,0.82); }}
  .dot-red    {{ background: var(--bad); box-shadow: 0 0 8px rgba(255,102,137,0.86); }}
  .dot-grey   {{ background: #6072ab; }}
  .section-note {{ margin-top: 10px; color: #a6b6e8; font-size: 12px; line-height: 1.45; }}
  .gpu-layout {{ display: grid; gap: 10px; }}
  .gpu-grid {{ display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); }}
  .gpu-item {{ border: 1px solid rgba(56,240,255,0.3); border-radius: 0; background: #000; padding: 10px 12px; }}
  .gpu-title {{ color: #d9f4ff; font-size: 11px; font-weight: 700; letter-spacing: 0.4px; text-transform: uppercase; margin-bottom: 7px; }}
  .gpu-val {{ color: #d0e2ff; font-size: 12.5px; line-height: 1.45; }}
  .visibility-list {{ list-style: none; display: grid; gap: 6px; }}
  .visibility-list li {{ color: #c8d8ff; font-size: 12px; line-height: 1.5; }}
  .visibility-list strong {{ color: #7cf2ff; font-weight: 600; }}

  /* Launchpad: animated pill showcase link */
  @keyframes edgeShimmer {{ 0% {{ background-position: 0 0; }} 100% {{ background-position: 300% 0; }} }}
  @keyframes glitchText {{
    0%, 95%, 100% {{ transform: none; opacity: 1; }}
    96% {{ transform: translate(-2px, 1px) skewX(-1deg); opacity: 0.9; }}
    97% {{ transform: translate(2px, -1px); opacity: 1; }}
    98% {{ transform: translate(-1px, 0) skewX(0.5deg); opacity: 0.95; }}
  }}
  .launchpad {{
    display: flex; justify-content: center; align-items: center;
    padding: 18px 20px; background: #000;
    border-bottom: 2px solid rgba(56,240,255,0.2);
    position: relative; z-index: 1;
  }}
  .launchpad-link {{
    position: relative;
    width: 280px; height: 56px;
    display: inline-flex; align-items: center; justify-content: center;
    overflow: hidden;
    padding: 14px 48px; border-radius: 999px;
    border: 2px solid rgba(56,240,255,0.5);
    background: #000;
    font-family: "Space Grotesk", sans-serif;
    font-size: 20px; font-weight: 400; letter-spacing: 0.10em; text-transform: uppercase;
    color: #c4a0ff; text-decoration: none;
    text-shadow: 2px 0 #ff2fb3, -2px 0 #38f0ff;
    box-shadow: 4px 4px 0 rgba(255,47,179,0.4), -2px -2px 0 rgba(56,240,255,0.25),
                0 12px 24px rgba(56,240,255,0.15), 0 12px 48px rgba(255,47,179,0.08);
    animation: glitchText 6s infinite;
    z-index: 1;
  }}
  .launchpad-link::before {{
    content: ""; position: absolute; inset: -2px; border-radius: 999px; padding: 2px;
    background: linear-gradient(90deg, #38f0ff, #ff2fb3, #4dffb4, #38f0ff);
    background-size: 300% 100%;
    animation: edgeShimmer 3s linear infinite;
    -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
    -webkit-mask-composite: xor; mask-composite: exclude;
    pointer-events: none;
  }}
  .launchpad-link:hover {{
    color: #fff;
    border-color: rgba(56,240,255,0.8);
    box-shadow: 6px 6px 0 rgba(255,47,179,0.6), -3px -3px 0 rgba(56,240,255,0.4),
                0 16px 32px rgba(56,240,255,0.25), 0 16px 64px rgba(255,47,179,0.12);
  }}
  .launchpad-link:focus-visible {{ outline: 2px solid var(--brand); outline-offset: 4px; }}
  .launchpad-link span {{ display: inline-block; white-space: nowrap; }}

  @keyframes riseIn {{
    from {{ opacity: 0; transform: translateY(5px); }}
    to   {{ opacity: 1; transform: translateY(0); }}
  }}

  @media (max-width: 1160px) {{
    .stack-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
  }}
  @media (max-width: 760px) {{
    .launchpad {{ padding: 12px 14px; }}
    .launchpad-link {{ width: 240px; height: 48px; padding: 10px 32px; font-size: 16px; }}
    main {{ padding: 14px; gap: 12px; }}
    .hb-grid {{ gap: 4px; }}
    .stack-grid {{ grid-template-columns: 1fr; }}
    .tab-bar {{ overflow-x: auto; }}
    .tab-btn {{ flex: 1 0 auto; }}
  }}
</style>
</head>
<body>
<header>
  <h1>Agent Monitor</h1>
  <span id="status-pill" class="pill pill-unknown">—</span>
  <button id="refresh-btn">↻ Refresh</button>
  <span id="last-updated"></span>
  <a href="roadmap/" style="margin-left:auto;color:var(--text-muted);text-decoration:none;font-size:13px;border:1px solid var(--text-dim);padding:2px 10px;">Roadmap</a>
  <a href="backlog/" style="color:var(--text-muted);text-decoration:none;font-size:13px;border:1px solid var(--text-dim);padding:2px 10px;">Backlog</a>
</header>

{launchpad_html}

<main>
  <div class="card">
    <div class="card-header">Supervisor</div>
    <div class="card-body">
      <div class="hb-grid" id="hb-grid"></div>
      <div class="hb-note" id="hb-note"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-header"><span class="dot" id="active-dot"></span><span id="active-header">Active Task</span></div>
    <div class="card-body" id="active-body"></div>
  </div>

  <div class="card">
    <div class="card-header">Task Stacks</div>
    <div class="card-body"><div class="stack-grid" id="stack-grid"></div></div>
  </div>

  <div class="card">
    <div class="card-header">System Snapshot</div>
    <div class="card-body"><div class="system-grid" id="system-grid"></div></div>
  </div>

  <div class="card">
    <div class="card-header">GPU Snapshot</div>
    <div class="card-body">
      <div class="gpu-layout" id="gpu-body"></div>
      <div class="section-note" id="gpu-note"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-header">Visibility + Permissions</div>
    <div class="card-body"><ul class="visibility-list" id="visibility-list"></ul></div>
  </div>
</main>

<script>
// Data embedded at file-write time. Used for instant first render in both modes.
const INITIAL_DATA = {data_json};
const API_STATUS_PATH = {api_status_path_json};

const STATUS_STYLES = {{
  running_session:       ['pill-running',  'dot-green',  'Running'],
  workers_active:        ['pill-running',  'dot-green',  'Running'],
  draining_queue:        ['pill-running',  'dot-green',  'Draining'],
  sleeping:              ['pill-sleeping', 'dot-grey',   'Sleeping'],
  sleeping_pending:      ['pill-pending',  'dot-yellow', 'Waiting (human)'],
  sleeping_after_failure:['pill-retrying', 'dot-red',    'Failed (sleeping)'],
  retrying_transient:    ['pill-retrying', 'dot-red',    'Retrying'],
  starting:              ['pill-sleeping', 'dot-grey',   'Starting'],
  restarting:            ['pill-pending',  'dot-yellow', 'Restarting'],
}};
const STACK_STYLE_BY_BUCKET = {{
  queued: 'stack-queued',
  active: 'stack-active',
  incomplete: 'stack-incomplete',
  finished: 'stack-finished',
}};

function esc(s) {{
  return String(s ?? '')
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;')
    .replace(/'/g,'&#39;');
}}
function ageSeconds(ts) {{
  if (!ts) return null;
  const sec = Math.floor((Date.now() - new Date(ts).getTime()) / 1000);
  return sec < 0 ? 0 : sec;
}}
function age(ts) {{
  const d = ageSeconds(ts);
  if (d == null) return '—';
  if (d < 60)   return d + 's';
  if (d < 3600) return Math.floor(d/60) + 'm ' + (d%60) + 's';
  return Math.floor(d/3600) + 'h ' + Math.floor((d%3600)/60) + 'm';
}}
function taskAge(ts) {{
  if (!ts) return '—';
  const d = Math.floor((Date.now() - parseFloat(ts)*1000) / 1000);
  if (d < 0)     return '0s';
  if (d < 60)    return d + 's';
  if (d < 3600)  return Math.floor(d/60) + 'm';
  if (d < 86400) return Math.floor(d/3600) + 'h';
  return Math.floor(d/86400) + 'd';
}}
function formatBytes(v) {{
  if (v == null || Number.isNaN(v)) return '—';
  const n = Number(v);
  if (n < 1024) return n + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n / 1024;
  let idx = 0;
  while (value >= 1024 && idx < units.length - 1) {{
    value /= 1024;
    idx += 1;
  }}
  return `${{value.toFixed(value >= 10 ? 1 : 2)}} ${{units[idx]}}`;
}}
function sortTasks(items) {{
  return [...items].sort((a, b) => {{
    const aTs = parseFloat(a.last_update_ts || a.created_ts || '0');
    const bTs = parseFloat(b.last_update_ts || b.created_ts || '0');
    return bTs - aTs;
  }});
}}
function rawTaskText(t) {{
  return String(
    t.task_description ??
    t.summary_preview ??
    t.summary ??
    t.mention_text ??
    '(no text)'
  ).replace(/\s+/g, ' ').trim();
}}
function compactTaskText(t, limit=90) {{
  const raw = rawTaskText(t);
  if (raw.length <= limit) return raw;
  return raw.substring(0, Math.max(limit - 1, 0)) + '…';
}}
function statusPill(s) {{
  const m = {{done:'pill-done',in_progress:'pill-inprog',waiting_human:'pill-waiting',queued:'pill-queued',failed:'pill-failed'}};
  return `<span class="pill ${{m[s]||'pill-unknown'}}">${{esc(s)||'?'}}</span>`;
}}
function formatTime(ts) {{
  if (!ts) return '—';
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  return d.toLocaleString();
}}
function heartbeatFreshness(ageSec) {{
  if (ageSec == null) return 'unknown';
  if (ageSec <= 15) return 'fresh';
  if (ageSec <= 60) return 'delayed';
  return 'stale';
}}

function render(data) {{
  const hb    = data.heartbeat;
  const tasks = data.tasks || {{}};

  // Status pill — override to DOWN if PID is dead
  const pidDead = hb && hb.pid_alive === false;
  const effectiveStatus = pidDead ? 'down' : (hb ? hb.status : null);
  const [pillCls, dotCls, label] = (effectiveStatus === 'down')
    ? ['pill-failed', 'dot-red', 'DOWN']
    : (hb && STATUS_STYLES[hb.status]) || (hb ? ['pill-unknown','dot-grey', hb.status || '?'] : ['pill-unknown','dot-grey','?']);
  const pillEl = document.getElementById('status-pill');
  pillEl.className = 'pill ' + pillCls;
  pillEl.textContent = label;

  // Supervisor stats
  const hbGridEl = document.getElementById('hb-grid');
  const hbNoteEl = document.getElementById('hb-note');
  if (hb) {{
    const hbAgeSec = ageSeconds(hb.last_updated_utc);
    const freshness = heartbeatFreshness(hbAgeSec);
    hbGridEl.innerHTML = [
      ['Loop',             hb.loop_count ?? '—'],
      ['PID',              hb.pid != null ? (hb.pid + (hb.pid_alive === false ? ' (dead)' : '')) : '—'],
      ['Last exit',        hb.last_exit_code ?? '—'],
      ['Failure kind',     hb.last_failure_kind || '—'],
      ['Retry attempt',    hb.transient_retry_attempt ?? '—'],
      ['Backoff',          hb.pending_backoff_sec != null ? hb.pending_backoff_sec + 's' : '—'],
      ['Next sleep',       hb.next_sleep_sec != null ? hb.next_sleep_sec + 's' : '—'],
      ['Pending decision', hb.pending_decision ? '⚠ Yes' : 'No'],
      ['Heartbeat age',    hbAgeSec == null ? '—' : `${{age(hb.last_updated_utc)}} (${{freshness}})`],
      ['Max workers',      hb.max_workers ?? 1],
      ['Heartbeat updated', formatTime(hb.last_updated_utc)],
    ].map(([l, v]) =>
      `<div class="hb-item"><span class="hb-label">${{l}}</span><span class="hb-val">${{esc(v)}}</span></div>`
    ).join('');
    hbNoteEl.textContent =
      hbAgeSec == null
        ? 'Heartbeat age is unavailable because last_updated_utc is missing.'
        : `Heartbeat age = seconds since supervisor last wrote .agent/runtime/heartbeat.json (${{hbAgeSec}}s).`;
    hbNoteEl.className = 'hb-note' + ((hbAgeSec != null && hbAgeSec > 60) ? ' hb-note-warn' : '');
  }} else {{
    hbGridEl.innerHTML = '<div class="hb-item"><span class="hb-label">State</span><span class="hb-val">No heartbeat data</span></div>';
    hbNoteEl.textContent = 'Heartbeat age is the elapsed time since .agent/runtime/heartbeat.json was last refreshed by the supervisor loop.';
    hbNoteEl.className = 'hb-note';
  }}

  // Active task(s) — supports parallel workers
  const active = Object.values(tasks.active || {{}});
  const workers = (hb && hb.active_workers) || [];
  const maxWorkers = (hb && hb.max_workers) || 1;
  document.getElementById('active-dot').className = 'dot ' + (active.length ? 'dot-green' : 'dot-grey');
  document.getElementById('active-header').textContent = maxWorkers >= 2
    ? `Active Tasks (${{active.length}}/${{maxWorkers}} slots)`
    : 'Active Task';
  const abody = document.getElementById('active-body');
  if (active.length) {{
    if (maxWorkers >= 2 && workers.length > 0) {{
      // Parallel mode: show per-worker cards
      abody.innerHTML = active.map(t => {{
        const txt = compactTaskText(t, 220);
        const w = workers.find(w => w.task === t.mention_ts);
        const slotInfo = w ? `<span><b>Slot:</b> ${{w.slot}}</span><span><b>Elapsed:</b> ${{w.elapsed_sec}}s</span>` : '';
        return `
          <div style="border-left:3px solid #4caf50;padding:4px 8px;margin-bottom:6px">
            <div class="task-text">${{esc(txt)}}</div>
            <div class="task-meta">
              ${{statusPill(t.status)}}
              ${{slotInfo}}
              <span><b>Age:</b> ${{taskAge(t.created_ts)}}</span>
              <span><b>Thread:</b> ${{esc(t.thread_ts || '—')}}</span>
            </div>
          </div>`;
      }}).join('');
    }} else {{
      // Serial mode: single active task
      const t = active[0];
      const txt = compactTaskText(t, 220);
      abody.innerHTML = `
        <div class="task-text">${{esc(txt)}}</div>
        <div class="task-meta">
          ${{statusPill(t.status)}}
          <span><b>Requester:</b> [redacted]</span>
          <span><b>Age:</b> ${{taskAge(t.created_ts)}}</span>
          <span><b>Thread:</b> ${{esc(t.thread_ts || '—')}}</span>
        </div>`;
    }}
  }} else {{
    abody.innerHTML = '<p style="color:#555;font-style:italic">No active task</p>';
  }}

  // Task stacks
  const stackDefs = [
    ['queued', 'Queued'],
    ['active', 'Active'],
    ['incomplete', 'Incomplete'],
    ['finished', 'Finished'],
  ];
  const stackGrid = document.getElementById('stack-grid');
  stackGrid.innerHTML = stackDefs.map(([bucket, label]) => {{
    const items = sortTasks(Object.values(tasks[bucket] || {{}}));
    const listHtml = items.length
      ? items.slice(0, 12).map(t => `
          <div class="stack-item">
            <div class="stack-item-title">${{esc(compactTaskText(t, 88))}}</div>
            <div class="stack-item-meta">
              <span>[redacted]</span>
              <span>${{taskAge(t.created_ts)}}</span>
            </div>
          </div>
        `).join('')
      : '<div class="stack-empty">No tasks</div>';
    return `
      <section class="stack-col ${{STACK_STYLE_BY_BUCKET[bucket] || ''}}">
        <div class="stack-head">
          <span>${{esc(label)}}</span>
          <span class="stack-count">${{items.length}}</span>
        </div>
        <div class="stack-list">${{listHtml}}</div>
      </section>
    `;
  }}).join('');

  // System snapshot
  const sys = data.system || {{}};
  const sysRows = [
    ['Host',              sys.hostname || '—'],
    ['Platform',          sys.platform || '—'],
    ['Python',            sys.python_version || '—'],
    ['CPU cores',         sys.cpu_count != null ? sys.cpu_count : '—'],
    ['Load avg 1/5/15m',  sys.load_avg ? `${{sys.load_avg.one}} / ${{sys.load_avg.five}} / ${{sys.load_avg.fifteen}}` : '—'],
    ['Memory total',      formatBytes(sys.memory_total_bytes)],
    ['Disk free',         formatBytes(sys.disk_free_bytes)],
    ['Disk used',         formatBytes(sys.disk_used_bytes)],
    ['Runner log size',   formatBytes(sys.runner_log_size_bytes)],
    ['Dashboard uptime',  age(sys.dashboard_started_utc)],
    ['Dashboard PID',     sys.dashboard_pid != null ? sys.dashboard_pid : '—'],
    ['Local IPs',         Array.isArray(sys.local_ips) && sys.local_ips.length ? sys.local_ips.join(', ') : '—'],
  ];
  document.getElementById('system-grid').innerHTML = sysRows.map(([l, v]) =>
    `<div class="sys-item"><div class="sys-label">${{esc(l)}}</div><div class="sys-val">${{esc(v)}}</div></div>`
  ).join('');

  // GPU snapshot (optional)
  const gpu = data.gpu || {{}};
  const gpuBody = document.getElementById('gpu-body');
  const gpuNote = document.getElementById('gpu-note');
  if (!gpu.enabled) {{
    gpuBody.innerHTML = `
      <div class="gpu-item">
        <div class="gpu-title">Status</div>
        <div class="gpu-val">GPU monitor disabled for this run.</div>
      </div>`;
    gpuNote.textContent = 'Run with --gpu-monitor on to re-enable. Collection is cached and low-frequency.';
    gpuNote.className = 'section-note';
  }} else {{
    const gpus = Array.isArray(gpu.gpus) ? gpu.gpus : [];
    const gpuCards = gpus.length
      ? gpus.map(row => `
          <div class="gpu-item">
            <div class="gpu-title">GPU ${{esc(row.index ?? '?')}} · ${{esc(row.name || 'Unknown')}}</div>
            <div class="gpu-val">util ${{esc(row.utilization_gpu_pct != null ? row.utilization_gpu_pct + '%' : '—')}} · mem ${{esc(row.memory_used_mb != null ? row.memory_used_mb : '—')}} / ${{esc(row.memory_total_mb != null ? row.memory_total_mb : '—')}} MB · temp ${{esc(row.temperature_c != null ? row.temperature_c + '°C' : '—')}}</div>
          </div>
        `).join('')
      : '<div class="gpu-item"><div class="gpu-title">GPU Data</div><div class="gpu-val">No GPU rows returned from nvidia-smi.</div></div>';

    gpuBody.innerHTML = `<div class="gpu-grid">${{gpuCards}}</div>`;

    const noteParts = [
      `Node: ${{gpu.node_alias || '—'}}`,
      `Last check: ${{formatTime(gpu.checked_at_utc)}}`,
    ];
    if (gpu.cache_age_sec != null) {{
      noteParts.push(`cache age ${{gpu.cache_age_sec}}s`);
    }}
    if (gpu.note) {{
      noteParts.push(String(gpu.note));
    }}
    gpuNote.textContent = noteParts.join(' · ');
    gpuNote.className = 'section-note' + ((gpu.status === 'ok' || gpu.status === 'partial') ? '' : ' hb-note-warn');
  }}

  // Visibility + permissions
  const vis = data.visibility || {{}};
  const visRows = [
    ['Audience', vis.audience || '—'],
    ['Task Text', vis.task_text_access || '—'],
    ['Source-Code Exposure', vis.source_code_exposure || '—'],
  ];
  document.getElementById('visibility-list').innerHTML = visRows.map(([l, v]) =>
    `<li><strong>${{esc(l)}}:</strong> ${{esc(v)}}</li>`
  ).join('');

  // Timestamp
  const src = data.server_time_utc ? new Date(data.server_time_utc).toLocaleTimeString() : '?';
  const mode = location.protocol === 'file:' ? 'file · Live Preview' : 'http · live poll';
  document.getElementById('last-updated').textContent = `Updated ${{src}} (${{mode}})`;
}}

// Render immediately from embedded data (instant, no network round-trip)
render(INITIAL_DATA);

const refreshBtn = document.getElementById('refresh-btn');
const canPoll = Boolean(API_STATUS_PATH) && location.protocol !== 'file:';
refreshBtn.onclick = canPoll ? () => fetchAndRender(true, true) : () => location.reload();

function resolveStatusUrl() {{
  const rawPath = String(API_STATUS_PATH || '');
  if (!rawPath) return '';
  const sep = rawPath.includes('?') ? '&' : '?';
  return `${{rawPath}}${{sep}}_ts=${{Date.now()}}`;
}}

function resolvePollIntervalMs(data) {{
  const fallback = Number(data?.polling?.supervisor_poll_interval_sec ?? 2);
  const raw = Number(data?.polling?.frontend_poll_interval_sec ?? fallback);
  const sec = Number.isFinite(raw) && raw > 0
    ? raw
    : (Number.isFinite(fallback) && fallback > 0 ? fallback : 2);
  return Math.max(1000, Math.round(sec * 1000));
}}

let pollTimer = null;
let fetchInFlight = false;
function scheduleNextPoll(data) {{
  if (!canPoll) return;
  if (pollTimer) clearTimeout(pollTimer);
  pollTimer = setTimeout(() => fetchAndRender(false), resolvePollIntervalMs(data));
}}

// Over HTTP: poll /api/status for live DOM updates without page reload.
function fetchAndRender(scheduleNext=true, manual=false) {{
  if (!API_STATUS_PATH) return Promise.resolve(null);
  if (fetchInFlight) return Promise.resolve(null);
  fetchInFlight = true;
  if (manual) refreshBtn.disabled = true;
  return fetch(resolveStatusUrl(), {{
      cache: 'no-store',
      credentials: 'include',
      headers: {{
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
      }},
    }})
    .then(r => {{
      if (!r.ok) throw new Error(`HTTP ${{r.status}}`);
      return r.json();
    }})
    .then(data => {{
      render(data);
      if (scheduleNext) scheduleNextPoll(data);
      return data;
    }})
    .catch(() => {{
      // fetch poll failed (e.g. behind Cloudflare Access) — fall back to page reload
      setTimeout(() => location.reload(), resolvePollIntervalMs(INITIAL_DATA));
      return null;
    }})
    .finally(() => {{
      fetchInFlight = false;
      if (manual) refreshBtn.disabled = false;
    }});
}}

if (canPoll) {{
  scheduleNextPoll(INITIAL_DATA);
  fetchAndRender(true);
}}
</script>
<script>
// Showcase link: random font on each page load, auto-scaled to fit pill
(function() {{
  var TARGET_WIDTH = 170;
  var TARGET_HEIGHT = 28;
  var fonts = [
    {{ family: '"Space Grotesk", sans-serif', weight: '300', spacing: '0.22em', transform: 'uppercase' }},
    {{ family: '"Playfair Display", serif', weight: '700', spacing: '0.06em', transform: 'uppercase', style: 'italic' }},
    {{ family: '"Caveat", cursive', weight: '700', spacing: '0.04em', transform: 'uppercase' }},
    {{ family: '"Permanent Marker", cursive', weight: '400', spacing: '0.06em', transform: 'uppercase' }},
    {{ family: '"Cormorant Garamond", serif', weight: '600', spacing: '0.08em', transform: 'uppercase', style: 'italic' }},
    {{ family: '"Barlow Condensed", sans-serif', weight: '600', spacing: '0.14em', transform: 'uppercase', style: 'italic' }},
    {{ family: '"JetBrains Mono", monospace', weight: '700', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Press Start 2P", monospace', weight: '400', spacing: '0.06em', transform: 'uppercase' }},
    {{ family: '"Audiowide", sans-serif', weight: '400', spacing: '0.12em', transform: 'uppercase' }},
    {{ family: '"Black Ops One", sans-serif', weight: '400', spacing: '0.08em', transform: 'uppercase' }},
    {{ family: '"Monoton", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Major Mono Display", monospace', weight: '400', spacing: '0.10em', transform: 'lowercase' }},
    {{ family: '"Orbitron", sans-serif', weight: '700', spacing: '0.14em', transform: 'uppercase' }},
    {{ family: '"Rajdhani", sans-serif', weight: '700', spacing: '0.16em', transform: 'uppercase' }},
    {{ family: '"Righteous", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Bungee", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' }},
    {{ family: '"Russo One", sans-serif', weight: '400', spacing: '0.08em', transform: 'uppercase' }},
    {{ family: '"Staatliches", sans-serif', weight: '400', spacing: '0.14em', transform: 'uppercase' }},
    {{ family: '"Cinzel", serif', weight: '700', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Gruppo", sans-serif', weight: '400', spacing: '0.18em', transform: 'uppercase' }},
    {{ family: '"Megrim", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Poiret One", sans-serif', weight: '400', spacing: '0.12em', transform: 'uppercase' }},
    {{ family: '"Michroma", sans-serif', weight: '400', spacing: '0.12em', transform: 'uppercase' }},
    {{ family: '"Nova Mono", monospace', weight: '400', spacing: '0.08em', transform: 'uppercase' }},
    {{ family: '"Syncopate", sans-serif', weight: '700', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Vast Shadow", serif', weight: '400', spacing: '0.06em', transform: 'uppercase' }},
    {{ family: '"Silkscreen", sans-serif', weight: '400', spacing: '0.08em', transform: 'uppercase' }},
    {{ family: '"Notable", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' }},
    {{ family: '"Bungee Shade", sans-serif', weight: '400', spacing: '0.04em', transform: 'uppercase' }},
    {{ family: '"Iceland", sans-serif', weight: '400', spacing: '0.14em', transform: 'uppercase' }},
    {{ family: '"Share Tech Mono", monospace', weight: '400', spacing: '0.12em', transform: 'uppercase' }},
    {{ family: '"VT323", monospace', weight: '400', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Special Elite", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' }},
    {{ family: '"Coda", sans-serif', weight: '800', spacing: '0.06em', transform: 'uppercase' }},
    {{ family: '"Teko", sans-serif', weight: '600', spacing: '0.14em', transform: 'uppercase' }},
    {{ family: '"Aldrich", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Electrolize", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Share Tech", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Play", sans-serif', weight: '700', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Quantico", sans-serif', weight: '700', spacing: '0.08em', transform: 'uppercase' }},
    {{ family: '"Kanit", sans-serif', weight: '600', spacing: '0.08em', transform: 'uppercase' }},
    {{ family: '"Tomorrow", sans-serif', weight: '600', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Chakra Petch", sans-serif', weight: '600', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Stint Ultra Expanded", sans-serif', weight: '400', spacing: '0.04em', transform: 'uppercase' }},
    {{ family: '"Abril Fatface", serif', weight: '400', spacing: '0.04em', transform: 'uppercase' }},
    {{ family: '"Fascinate", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' }},
    {{ family: '"Wallpoet", sans-serif', weight: '400', spacing: '0.10em', transform: 'uppercase' }},
    {{ family: '"Nosifer", sans-serif', weight: '400', spacing: '0.04em', transform: 'uppercase' }},
    {{ family: '"Lacquer", sans-serif', weight: '400', spacing: '0.04em', transform: 'uppercase' }},
    {{ family: '"Rubik Glitch", sans-serif', weight: '400', spacing: '0.06em', transform: 'uppercase' }},
  ];
  window.addEventListener('DOMContentLoaded', function() {{
    var link = document.querySelector('.launchpad-link');
    if (!link) return;
    if (!link.querySelector('span')) {{
      var span = document.createElement('span');
      span.textContent = link.textContent;
      link.textContent = '';
      link.appendChild(span);
    }}
    var span = link.querySelector('span');
    var f = fonts[Math.floor(Math.random() * fonts.length)];
    span.style.fontFamily = f.family;
    span.style.fontWeight = f.weight;
    span.style.letterSpacing = f.spacing;
    span.style.textTransform = f.transform;
    span.style.fontStyle = f.style || 'normal';
    span.style.fontSize = '20px';
    span.style.whiteSpace = 'nowrap';
    span.style.display = 'inline-block';
    requestAnimationFrame(function() {{
      setTimeout(function() {{
        var actualW = span.offsetWidth;
        var actualH = span.offsetHeight;
        if (actualW > 0 && actualH > 0) {{
          var scaleW = TARGET_WIDTH / actualW;
          var scaleH = TARGET_HEIGHT / actualH;
          var scale = Math.min(scaleW, scaleH);
          var linkRect = link.getBoundingClientRect();
          var spanRect = span.getBoundingClientRect();
          var linkCenterY = linkRect.top + linkRect.height / 2;
          var spanCenterY = spanRect.top + spanRect.height / 2;
          var offsetY = (linkCenterY - spanCenterY) / scale;
          span.style.transform = 'scale(' + scale + ') translateY(' + offsetY + 'px)';
          span.style.transformOrigin = 'center center';
        }}
      }}, 100);
    }});
  }});
}})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Data aggregation
# ---------------------------------------------------------------------------

def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def read_text(path):
    try:
        return Path(path).read_text()
    except Exception:
        return None



def resolve_task_text(task):
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
        # Fallback: read as plain text (legacy)
        return read_text(mention_text_file) or ""
    return ""


def redact_identity_text(text: str) -> str:
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
    parts = [part.strip() for part in TASK_SENTENCE_SPLIT_RE.split(raw) if part.strip()]
    sentence = parts[0] if parts else raw
    if sentence:
        sentence = sentence[0].upper() + sentence[1:]
    if len(sentence) > limit:
        sentence = sentence[: max(limit - 1, 0)].rstrip() + "…"
    if sentence and sentence[-1] not in ".!?…":
        sentence += "."
    return sentence


def looks_like_task_metadata(text: str) -> bool:
    lowered = str(text or "").lower()
    return (
        "## mention (mention_ts=" in lowered
        or "- thread id:" in lowered
        or "[context update:" in lowered
    )


def extract_task_objective_candidate(task_text: str) -> str:
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


def _contains_any(text: str, keywords) -> bool:
    lowered = str(text or "").lower()
    return any(keyword in lowered for keyword in keywords)


def derive_public_task_story(task: dict, mention_text: str) -> tuple:
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


def derive_public_backlog_copy(title: str, detail: str, *, queue: str = "", completed: bool = False) -> tuple:
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


def enrich_bucket_tasks(bucket):
    out = {}
    for key, task in (bucket or {}).items():
        if not isinstance(task, dict):
            continue
        row = dict(task)
        mention_text = redact_identity_text(resolve_task_text(row))
        row["task_description"] = redact_identity_text(derive_task_description(row, mention_text))
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


def file_size_bytes(path):
    try:
        return Path(path).stat().st_size
    except Exception:
        return None


def sysconf_int(name):
    try:
        value = os.sysconf(name)
        if isinstance(value, int) and value > 0:
            return value
    except Exception:
        return None
    return None


def utc_now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_visibility_info(mode: str):
    if mode == "public_snapshot":
        return {
            "mode": mode,
            "audience": "Anyone with the GitHub Pages URL.",
            "task_text_access": "Thread-scoped task_description plus compact previews; requester identities are redacted.",
            "source_code_exposure": "No repository files are published by this dashboard output.",
        }
    return {
        "mode": "local_live",
        "audience": "Anyone with local machine/port access.",
        "task_text_access": "Thread-scoped task_description plus truncated previews; requester identities are redacted in the monitor payload.",
        "source_code_exposure": "Dashboard does not publish repository files by itself.",
    }


def parse_int(value):
    try:
        return int(str(value).strip())
    except Exception:
        return None


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


def _parse_bool(value: str) -> bool:
    """Match supervisor parse_bool semantics: 1/true/yes/y/on (case-insensitive)."""
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _get_config_file() -> Path:
    """Return the current config file path (re-resolves after .env loading)."""
    global SUPERVISOR_LOOP_CONFIG_FILE
    return SUPERVISOR_LOOP_CONFIG_FILE


def _reload_config_file() -> None:
    """Re-resolve SUPERVISOR_LOOP_CONFIG_FILE from env (call after _load_dotenv)."""
    global SUPERVISOR_LOOP_CONFIG_FILE
    SUPERVISOR_LOOP_CONFIG_FILE = _resolve_config_file()


def read_supervisor_default(name: str):
    cfg_file = _get_config_file()
    if not cfg_file.exists():
        return None
    try:
        lines = cfg_file.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in lines:
        match = SUPERVISOR_DEFAULT_RE.match(line)
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


def read_positive_interval_from_root(*names: str):
    for name in names:
        env_value = parse_int(os.environ.get(name))
        if env_value and env_value > 0:
            return env_value
    for name in names:
        cfg_value = parse_int(read_supervisor_default(name))
        if cfg_value and cfg_value > 0:
            return cfg_value
    return None


def resolve_supervisor_poll_interval_sec():
    return read_positive_interval_from_root("SLEEP_NORMAL") or DEFAULT_LOCAL_WRITE_INTERVAL_SEC


def build_polling_info():
    poll_interval = resolve_supervisor_poll_interval_sec()
    return {
        "supervisor_poll_interval_sec": poll_interval,
        "frontend_poll_interval_sec": poll_interval,
    }


def default_gpu_status(enabled: bool, node_alias: str):
    return {
        "enabled": bool(enabled),
        "node_alias": node_alias,
        "status": "disabled" if not enabled else "pending",
        "checked_at_utc": None,
        "cache_age_sec": None,
        "note": "GPU monitor disabled for this run." if not enabled else "GPU monitor enabled; waiting for first poll.",
        "gpus": [],
    }


def configure_gpu_monitor(enabled: bool, node_alias: str, poll_interval_sec: Optional[int], command_timeout_sec: int):
    resolved_poll_interval = poll_interval_sec if poll_interval_sec is not None else resolve_supervisor_poll_interval_sec()
    poll_interval = max(int(resolved_poll_interval), 30)
    timeout = max(int(command_timeout_sec), 2)
    with GPU_STATE_LOCK:
        GPU_MONITOR["enabled"] = bool(enabled)
        GPU_MONITOR["node_alias"] = node_alias or ""
        GPU_MONITOR["poll_interval_sec"] = poll_interval
        GPU_MONITOR["command_timeout_sec"] = timeout
        GPU_MONITOR["last_polled_ts"] = 0.0
        GPU_MONITOR["last_result"] = default_gpu_status(
            enabled=bool(enabled),
            node_alias=GPU_MONITOR["node_alias"],
        )


def run_remote_command(node_alias: str, remote_command: str, timeout_sec: int):
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=4",
        node_alias,
        remote_command,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
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
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": f"timed out after {timeout_sec}s",
        }
    except Exception as exc:
        return {
            "ok": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "error": str(exc),
        }


def parse_gpu_rows(raw_text: str):
    rows = []
    for line in (raw_text or "").splitlines():
        if not line.strip():
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        rows.append(
            {
                "index": parse_int(parts[0]),
                "name": parts[1],
                "utilization_gpu_pct": parse_int(parts[2]),
                "memory_used_mb": parse_int(parts[3]),
                "memory_total_mb": parse_int(parts[4]),
                "temperature_c": parse_int(parts[5]),
            }
        )
    return rows


def collect_remote_gpu_status(node_alias: str, timeout_sec: int):
    result = default_gpu_status(enabled=True, node_alias=node_alias)
    result["checked_at_utc"] = utc_now_iso()

    gpu_cmd = (
        "nvidia-smi --query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu "
        "--format=csv,noheader,nounits"
    )
    gpu_resp = run_remote_command(node_alias=node_alias, remote_command=gpu_cmd, timeout_sec=timeout_sec)
    if not gpu_resp["ok"]:
        detail = gpu_resp["error"] or (gpu_resp["stderr"] or gpu_resp["stdout"]).strip()
        result["status"] = "unavailable"
        result["note"] = f"GPU query failed: {detail or 'no details'}"
        return result

    result["gpus"] = parse_gpu_rows(gpu_resp["stdout"])

    if result["gpus"]:
        result["status"] = "ok"
        result["note"] = "Low-frequency cached nvidia-smi snapshot."
    else:
        result["status"] = "partial"
        result["note"] = "nvidia-smi returned no GPU rows."
    return result


def build_gpu_status():
    with GPU_STATE_LOCK:
        enabled = bool(GPU_MONITOR["enabled"])
        node_alias = str(GPU_MONITOR["node_alias"])
        poll_interval = int(GPU_MONITOR["poll_interval_sec"])
        timeout_sec = int(GPU_MONITOR["command_timeout_sec"])
        last_polled_ts = float(GPU_MONITOR["last_polled_ts"] or 0.0)
        last_result = copy.deepcopy(GPU_MONITOR["last_result"])

    now = time.time()
    if not enabled:
        base = last_result or default_gpu_status(enabled=False, node_alias=node_alias)
        base["cache_age_sec"] = None
        return base

    if last_result and last_polled_ts > 0 and (now - last_polled_ts) < poll_interval:
        last_result["cache_age_sec"] = int(now - last_polled_ts)
        return last_result

    fresh = collect_remote_gpu_status(node_alias=node_alias, timeout_sec=timeout_sec)
    fresh["cache_age_sec"] = 0
    with GPU_STATE_LOCK:
        GPU_MONITOR["last_result"] = copy.deepcopy(fresh)
        GPU_MONITOR["last_polled_ts"] = now
    return fresh


def build_system_info():
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
        "runner_log_size_bytes": file_size_bytes(AGENT_DIR / "runtime" / "logs" / "runner.log"),
        "dashboard_pid": os.getpid(),
        "dashboard_started_utc": datetime.fromtimestamp(PROCESS_START_TS, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "local_ips": local_ips(),
    }


def parse_backlog():
    """Parse docs/dev/BACKLOG.md and return dict with items and completed lists."""
    try:
        text = BACKLOG_FILE.read_text(encoding="utf-8")
    except OSError:
        return {"items": [], "completed": []}

    # Load per-item dev session mapping.
    session_map = _read_dev_session_map()

    # Protect pipes inside backtick spans so the table regex doesn't split on them.
    _BACKTICK_PIPE_RE = re.compile(r"`[^`]*`")

    def _shield_pipes(t):
        return _BACKTICK_PIPE_RE.sub(lambda m: m.group(0).replace("|", "\x00"), t)

    def _restore_pipes(t):
        return t.replace("\x00", "|")

    # Extract section bodies.
    sections = list(BACKLOG_SECTION_RE.finditer(text))
    queue_sections = []  # (queue_name, section_text)
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

    items = []
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
                    plan_content = (BACKLOG_FILE.parent / plan_match.group(1)).read_text(encoding="utf-8")
                except OSError:
                    pass
            if issue_match:
                try:
                    issue_content = (BACKLOG_FILE.parent / issue_match.group(1)).read_text(encoding="utf-8")
                except OSError:
                    pass
            item_id = m.group("id").strip()
            items.append({
                "id": item_id,
                "queue": queue_name,
                "created": m.group("created").strip(),
                "priority": m.group("priority").strip(),
                "status": m.group("status").strip(),
                "task": BACKLOG_MD_LINK_RE.sub(r"\1", _restore_pipes(m.group("task").strip())),
                "context": BACKLOG_MD_LINK_RE.sub(r"\1", ctx_raw)[:200],
                "done_when": _restore_pipes(m.group("done_when").strip())[:200],
                "has_plan": bool(plan_match),
                "has_issue": bool(issue_match),
                "plan_content": plan_content,
                "issue_content": issue_content,
                "session_id": session_map.get(item_id, ""),
            })

    completed = []
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
                    plan_content = (BACKLOG_FILE.parent / plan_match.group(1)).read_text(encoding="utf-8")
                except OSError:
                    pass
            if issue_match:
                try:
                    issue_content = (BACKLOG_FILE.parent / issue_match.group(1)).read_text(encoding="utf-8")
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


def parse_roadmap_json():
    """Load docs/dev/roadmap.json and return its contents."""
    try:
        return json.loads(ROADMAP_JSON_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def build_status():
    heartbeat = read_json(AGENT_DIR / "runtime" / "heartbeat.json")
    # Enrich heartbeat with PID liveness so consumers can distinguish
    # a genuinely sleeping supervisor from a dead one with a stale file.
    if heartbeat and isinstance(heartbeat, dict):
        hb_pid = heartbeat.get("pid")
        if isinstance(hb_pid, int) and hb_pid > 0:
            heartbeat["pid_alive"] = _pid_alive(hb_pid)
        else:
            heartbeat["pid_alive"] = False
    state     = read_json(AGENT_DIR / "runtime" / "state.json")
    system    = build_system_info()
    gpu       = build_gpu_status()

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
            "queued":     enrich_bucket_tasks(state.get("queued_tasks") or {}),
            "active":     enrich_bucket_tasks(state.get("active_tasks") or {}),
            "incomplete": enrich_bucket_tasks(state.get("incomplete_tasks") or {}),
            "finished":   enrich_bucket_tasks(finished_sorted),
        }

    return {
        "heartbeat":       heartbeat,
        "system":          system,
        "gpu":             gpu,
        "tasks":           tasks,
        "roadmap":         publicize_roadmap_data(parse_roadmap_json()),
        "backlog":         parse_backlog(),
        "polling":         build_polling_info(),
        "visibility":      build_visibility_info("local_live"),
        "server_time_utc": utc_now_iso(),
    }


def redact_secrets(text: str) -> str:
    out = text
    for pattern in SECRET_PATTERNS:
        out = pattern.sub(REDACTED_SECRET, out)
    return out


def compact_public_text(text: str, limit: int = 160) -> str:
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


def deep_redact(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, list):
        return [deep_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: deep_redact(val) for key, val in value.items()}
    return value


def public_task_view(task):
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
    summary_preview = compact_public_text(summary_source, limit=160) or "(task details redacted for public view)"
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


def sanitize_public_status(payload):
    safe = copy.deepcopy(payload)
    system = safe.get("system") if isinstance(safe.get("system"), dict) else {}
    if system:
        system.pop("repo_path", None)
        system.pop("agent_dir", None)
        system["hostname"] = "redacted-host"
        system["local_ips"] = []
        system["dashboard_pid"] = None

    tasks = safe.get("tasks") if isinstance(safe.get("tasks"), dict) else {}
    safe_tasks = {}
    for bucket in ("queued", "active", "incomplete", "finished"):
        rows = tasks.get(bucket) if isinstance(tasks.get(bucket), dict) else {}
        safe_tasks[bucket] = {
            key: public_task_view(task)
            for key, task in rows.items()
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
# HTML file writer
# ---------------------------------------------------------------------------

def atomic_write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def json_for_script(value):
    # Keep embedded JSON safe inside a classic <script> block.
    return (
        json.dumps(value, default=str, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _build_launchpad_html():
    """Build the Launchpad navigation band HTML from SITE_LINKS."""
    if not SITE_LINKS:
        return ""
    items = []
    for link in SITE_LINKS:
        label = html_mod.escape(link["label"])
        href = html_mod.escape(link["href"])
        desc = link.get("desc", "")
        title_attr = f' title="{html_mod.escape(desc)}"' if desc else ""
        items.append(f'<a class="launchpad-link" href="{href}"{title_attr}>{label}</a>')
    inner = "\n  ".join(items)
    return f'<nav class="launchpad" aria-label="Launchpad">\n  {inner}\n</nav>'


def render_html(data, api_status_path):
    data_json = json_for_script(data)
    api_status_path_json = json.dumps(api_status_path, ensure_ascii=False)
    launchpad_html = _build_launchpad_html()
    return HTML_TEMPLATE.format(
        data_json=data_json,
        api_status_path_json=api_status_path_json,
        launchpad_html=launchpad_html,
    )


def write_html(out_file: Path, api_status_path: str = "/api/status", data=None):
    payload = data or build_status()
    html = render_html(payload, api_status_path=api_status_path)
    atomic_write_text(out_file, html)


# ---------------------------------------------------------------------------
# Backlog page template
# ---------------------------------------------------------------------------

BACKLOG_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Murphy Backlog</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Playfair+Display:ital,wght@1,700&display=swap');
  :root {{
    --bg-base: #000; --bg-border: rgba(56,240,255,0.5); --bg-border-strong: rgba(56,240,255,0.8);
    --text-main: #eaffff; --text-muted: #99a6d8; --text-dim: #6d79ad;
    --brand: #38f0ff; --brand-alt: #ff2fb3;
    --good: #4dffb4; --warn: #ffe66a; --bad: #ff6689;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "Rajdhani", "Avenir Next", "Segoe UI", sans-serif;
    background:
      radial-gradient(circle at top, rgba(19, 42, 72, 0.34), transparent 38%),
      radial-gradient(circle at 85% 0%, rgba(255, 47, 179, 0.14), transparent 26%),
      #000;
    color: var(--text-main); font-size: 15px;
    min-height: 100vh; overflow-x: hidden;
  }}
  .status-dot {{ width: 6px; height: 6px; border-radius: 50%; display: inline-block; }}
  .status-dot.ok {{ background: var(--good); }}
  .status-dot.missing {{ background: var(--warn); }}
  main {{
    max-width: 1180px;
    margin: 0 auto;
    padding: 22px 20px 40px;
    display: grid;
    gap: 16px;
  }}
  .roadmap-hero {{
    display: grid;
    grid-template-columns: minmax(0, 0.96fr) minmax(320px, 1.04fr);
    gap: 16px;
  }}
  .hero-copy,
  .summary-grid,
  .settings,
  .roadmap-stage {{
    border: 1px solid rgba(56, 240, 255, 0.18);
    background:
      linear-gradient(135deg, rgba(5, 9, 24, 0.96) 0%, rgba(12, 20, 42, 0.9) 100%);
    box-shadow:
      3px 3px 0 rgba(255, 47, 179, 0.18),
      -1px -1px 0 rgba(56, 240, 255, 0.12);
  }}
  .hero-copy {{
    display: grid;
    gap: 12px;
    padding: 20px;
    align-content: start;
  }}
  .hero-kicker,
  .settings-kicker {{
    color: var(--brand);
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }}
  .hero-copy h1 {{
    font-size: clamp(34px, 5vw, 58px);
    line-height: 0.92;
    letter-spacing: 0.06em;
  }}
  .hero-copy p {{
    color: #bed2ff;
    font-size: 16px;
    line-height: 1.52;
    max-width: 620px;
  }}
  .summary-grid {{
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
    padding: 20px;
  }}
  .summary-card {{
    display: grid;
    gap: 8px;
    padding: 16px;
    border: 1px solid rgba(56, 240, 255, 0.14);
    background: rgba(6, 11, 24, 0.88);
  }}
  .summary-card span {{
    color: var(--text-muted);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }}
  .summary-card strong {{
    color: #f4fbff;
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 24px;
    font-weight: 700;
    letter-spacing: 0.08em;
  }}
  .settings {{
    display: grid;
    gap: 12px;
    padding: 18px;
  }}
  .settings-top {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }}
  .settings-top h2 {{
    font-size: 22px;
    letter-spacing: 0.04em;
  }}
  .settings-copy {{
    color: #bed2ff;
    font-size: 14px;
    line-height: 1.45;
    max-width: 700px;
  }}
  .settings-state {{
    color: var(--text-muted);
    font-size: 12px;
    line-height: 1.4;
  }}
  .settings-body {{
    display: none;
    padding: 14px 0 0;
    border-top: 1px solid rgba(56, 240, 255, 0.14);
  }}
  .settings-body.open {{ display: block; }}
  .settings-row {{ display: flex; gap: 10px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }}
  .settings-row label {{ font-size: 12px; color: var(--text-muted); min-width: 100px; }}
  .settings-row input {{
    flex: 1; min-width: 200px; background: #0a0a1a; border: 1px solid rgba(56, 240, 255, 0.18);
    color: var(--text-main); padding: 5px 8px; font-family: "JetBrains Mono", monospace;
    font-size: 12px;
  }}
  .settings-row input:focus {{ outline: none; border-color: var(--brand); }}
  .settings-btn {{
    background: none; border: 1px solid rgba(77, 255, 180, 0.36); color: var(--good);
    padding: 8px 16px; cursor: pointer; font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
    box-shadow: 3px 3px 0 rgba(77, 255, 180, 0.12);
  }}
  .settings-btn:hover {{ background: rgba(77,255,180,0.1); }}
  .settings-status {{ font-size: 11px; margin-top: 6px; }}
  .settings-status.ok {{ color: var(--good); }}
  .settings-status.missing {{ color: var(--warn); }}
  .roadmap-stage {{
    display: grid;
    gap: 14px;
    padding: 18px;
  }}
  .tab-bar {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .tab-btn {{
    background: rgba(8, 12, 28, 0.92);
    border: 1px solid rgba(56, 240, 255, 0.16);
    color: var(--text-muted);
    padding: 9px 14px;
    cursor: pointer;
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }}
  .tab-btn:hover {{ color: var(--text-main); }}
  .tab-btn.active {{
    color: var(--text-main);
    border-color: rgba(56, 240, 255, 0.58);
    box-shadow:
      4px 4px 0 rgba(255, 47, 179, 0.2),
      -1px -1px 0 rgba(56, 240, 255, 0.16);
  }}

  /* Roadmap grid */
  .roadmap-grid {{ display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }}
  .roadmap-item {{
    border: 1px solid rgba(56,240,255,0.16);
    background:
      linear-gradient(180deg, rgba(255,255,255,0.02) 0%, transparent 14%, transparent 100%),
      rgba(6, 11, 24, 0.92);
    padding: 16px; display: grid; gap: 10px;
    transition: border-color 0.15s;
  }}
  .roadmap-item:hover {{
    border-color: rgba(56,240,255,0.4);
    box-shadow:
      4px 4px 0 rgba(255, 47, 179, 0.18),
      -1px -1px 0 rgba(56, 240, 255, 0.14);
  }}
  .roadmap-item[data-priority="Critical"] {{ border-left: 3px solid var(--bad); }}
  .roadmap-item[data-priority="High"] {{ border-left: 3px solid var(--warn); }}
  .roadmap-item[data-priority="Medium"] {{ border-left: 3px solid var(--brand); }}
  .roadmap-item[data-priority="Low"] {{ border-left: 3px solid var(--text-dim); }}

  .roadmap-top {{ display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .roadmap-id {{
    font-family: "JetBrains Mono", monospace; font-size: 12px;
    color: var(--brand); font-weight: 500;
  }}
  .roadmap-task {{ font-size: 16px; color: #d9f1ff; line-height: 1.42; }}
  .roadmap-context {{ color: #9eb5df; font-size: 13px; line-height: 1.48; }}
  .roadmap-meta {{
    display: flex; gap: 10px; flex-wrap: wrap;
    font-size: 11px; color: var(--text-muted);
  }}
  .roadmap-controls {{ display: flex; gap: 6px; align-items: center; }}

  .pill {{
    display: inline-block; padding: 1px 8px; font-size: 10px;
    font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em;
    border: 1px solid; white-space: nowrap;
  }}
  .pill-open {{ color: var(--brand); border-color: rgba(56,240,255,0.4); }}
  .pill-deferred {{ color: var(--text-dim); border-color: rgba(109,121,173,0.4); }}
  .pill-in_progress {{ color: var(--good); border-color: rgba(77,255,180,0.4); }}
  .pill-priority-Critical {{ color: var(--bad); border-color: rgba(255,102,137,0.4); }}
  .pill-priority-High {{ color: var(--warn); border-color: rgba(255,230,106,0.4); }}
  .pill-aux {{ color: #d2e6ff; border-color: rgba(56,240,255,0.22); }}

  .dispatch-btn {{
    padding: 7px 12px; border: 1px solid rgba(56,240,255,0.24);
    background:
      linear-gradient(135deg, rgba(8, 12, 28, 0.96) 0%, rgba(14, 22, 42, 0.92) 100%);
    color: #d9f6ff; font-size: 11px;
    font-weight: 700; cursor: pointer; font-family: "Orbitron", "Rajdhani", sans-serif;
    letter-spacing: 0.08em; text-transform: uppercase;
    white-space: nowrap;
    box-shadow: 3px 3px 0 rgba(255,47,179,0.15);
    transition: all 0.1s;
  }}
  .dispatch-btn:hover {{
    border-color: rgba(255,47,179,0.42);
    box-shadow: 4px 4px 0 rgba(255,47,179,0.24);
  }}
  .dispatch-btn:disabled {{ opacity: 0.35; cursor: not-allowed; box-shadow: none; }}
  .dispatch-btn.success {{ border-color: var(--good); color: var(--good); }}
  .dispatch-btn.error {{ border-color: var(--bad); color: var(--bad); }}

  .empty {{ color: var(--text-dim); font-size: 13px; padding: 20px 0; text-align: center; }}

  .backlog-group-header {{
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em;
    text-transform: uppercase; color: var(--text-muted);
    border-bottom: 1px solid rgba(56,240,255,0.12);
    padding: 6px 0; margin-top: 18px;
  }}
  .backlog-group-header:first-child {{ margin-top: 0; }}
  .backlog-group-header .group-count {{
    color: var(--text-dim); font-weight: 400; margin-left: 6px;
  }}

  /* Session log viewer (rendered in modal) */
  .session-entry {{ padding: 8px 12px; border-bottom: 1px solid rgba(56,240,255,0.05); }}
  .session-entry.role-assistant {{ border-left: 2px solid var(--brand); }}
  .session-entry.role-user {{ border-left: 2px solid var(--text-dim); opacity: 0.7; }}
  .session-ts {{ font-size: 10px; color: var(--text-dim); font-family: "JetBrains Mono", monospace; }}
  .session-block {{ margin: 4px 0; }}
  .session-text {{ font-size: 13px; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }}
  .session-tool {{ font-size: 12px; }}
  .session-tool-name {{ color: var(--brand); font-weight: 600; font-family: "JetBrains Mono", monospace; }}
  .session-tool-input {{ color: var(--text-muted); font-size: 11px; white-space: pre-wrap; word-break: break-word;
    max-height: 150px; overflow-y: auto; margin-top: 2px; }}
  .session-result {{ font-size: 11px; color: var(--text-muted); white-space: pre-wrap; word-break: break-word;
    max-height: 200px; overflow-y: auto; border-left: 2px solid var(--text-dim); padding-left: 8px; margin: 4px 0; }}
  .session-thinking {{ cursor: pointer; }}
  .session-thinking-label {{ font-size: 11px; color: var(--brand-alt); font-style: italic; }}
  .session-thinking-body {{ display: none; font-size: 11px; color: var(--text-dim); white-space: pre-wrap;
    word-break: break-word; max-height: 300px; overflow-y: auto; margin-top: 2px; }}
  .session-thinking.open .session-thinking-body {{ display: block; }}
  .session-toolbar {{ display: flex; gap: 6px; padding: 6px 12px; border-bottom: 1px solid rgba(56,240,255,0.1);
    background: #0a0a1a; position: sticky; top: 0; z-index: 1; }}
  .view-session-btn {{
    padding: 2px 8px; border: 1px solid rgba(255,47,179,0.4); background: none;
    color: var(--brand-alt); font-size: 10px; font-weight: 600; cursor: pointer;
    font-family: "Rajdhani", sans-serif;
  }}
  .view-session-btn:hover {{ border-color: var(--brand-alt); }}

  /* Doc modal overlay */
  .doc-modal-overlay {{
    display: none; position: fixed; inset: 0; z-index: 900;
    background: rgba(0,0,0,0.75); backdrop-filter: blur(4px);
    justify-content: center; align-items: center; padding: 24px;
  }}
  .doc-modal-overlay.open {{ display: flex; }}
  .doc-modal {{
    background: #0a0a14; border: 1px solid rgba(56,240,255,0.25);
    max-width: 720px; width: 100%; max-height: 80vh; display: flex; flex-direction: column;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
  }}
  .doc-modal-header {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 12px 16px; border-bottom: 1px solid rgba(56,240,255,0.12);
    background: #080816;
  }}
  .doc-modal-title {{ font-size: 14px; font-weight: 700; color: var(--brand); flex: 1; }}
  .doc-modal-refresh {{
    background: none; border: 1px solid rgba(255,255,255,0.15); color: var(--text-dim);
    width: 28px; height: 28px; cursor: pointer; font-size: 16px; display: none;
    align-items: center; justify-content: center; margin-right: 6px; transition: opacity 0.15s;
  }}
  .doc-modal-refresh:hover {{ border-color: var(--brand); color: var(--brand); }}
  .doc-modal-refresh:disabled {{ opacity: 0.3; cursor: default; }}
  .doc-modal-close {{
    background: none; border: 1px solid rgba(255,255,255,0.15); color: var(--text-dim);
    width: 28px; height: 28px; cursor: pointer; font-size: 16px; display: flex;
    align-items: center; justify-content: center;
  }}
  .doc-modal-close:hover {{ border-color: var(--bad); color: var(--bad); }}
  .doc-modal-body {{
    padding: 16px; overflow-y: auto; flex: 1;
    font-size: 13px; line-height: 1.6; color: var(--text);
    white-space: pre-wrap; word-break: break-word;
    font-family: 'SF Mono', 'Fira Code', 'JetBrains Mono', monospace;
    scrollbar-width: thin;
    scrollbar-color: rgba(56,240,255,0.3) transparent;
    transition: opacity 0.15s;
  }}
  .doc-modal-body::-webkit-scrollbar {{ width: 6px; }}
  .doc-modal-body::-webkit-scrollbar-track {{ background: transparent; }}
  .doc-modal-body::-webkit-scrollbar-thumb {{ background: rgba(56,240,255,0.3); border-radius: 3px; }}
  .doc-modal-body::-webkit-scrollbar-thumb:hover {{ background: rgba(56,240,255,0.5); }}

  /* Dispatch modal overlay */
  .dispatch-modal-overlay {{
    display: none; position: fixed; inset: 0; z-index: 950;
    background: rgba(0,0,0,0.8); backdrop-filter: blur(6px);
    justify-content: center; align-items: center; padding: 24px;
  }}
  .dispatch-modal-overlay.open {{ display: flex; }}
  .dispatch-modal {{
    background: #0a0a14; border: 1px solid rgba(56,240,255,0.25);
    max-width: 520px; width: 100%; display: flex; flex-direction: column;
    box-shadow: 0 8px 32px rgba(0,0,0,0.6);
  }}
  .dispatch-modal-header {{
    display: flex; justify-content: space-between; align-items: center;
    padding: 14px 16px; border-bottom: 1px solid rgba(56,240,255,0.12);
    background: #080816;
  }}
  .dispatch-modal-title {{ font-size: 14px; font-weight: 700; color: var(--brand); }}
  .dispatch-modal-close {{
    background: none; border: 1px solid rgba(255,255,255,0.15); color: var(--text-dim);
    width: 28px; height: 28px; cursor: pointer; font-size: 16px; display: flex;
    align-items: center; justify-content: center;
  }}
  .dispatch-modal-close:hover {{ border-color: var(--bad); color: var(--bad); }}
  .dispatch-modal-body {{ padding: 16px; display: flex; flex-direction: column; gap: 14px; }}
  .dispatch-field {{ display: flex; flex-direction: column; gap: 5px; }}
  .dispatch-label {{
    font-size: 11px; font-weight: 700; color: var(--text-muted);
    text-transform: uppercase; letter-spacing: 0.06em;
  }}
  .dispatch-textarea {{
    background: #0a0a1a; border: 1px solid rgba(56,240,255,0.18);
    color: var(--text-main); padding: 8px 10px; font-family: 'JetBrains Mono', monospace;
    font-size: 12px; min-height: 72px; resize: vertical;
  }}
  .dispatch-textarea:focus {{ outline: none; border-color: var(--brand); }}
  .dispatch-textarea::placeholder {{ color: var(--text-dim); }}
  .dispatch-rounds-row {{ display: flex; gap: 16px; }}
  .dispatch-rounds-row .dispatch-field {{ flex: 1; }}
  .dispatch-number-input {{
    background: #0a0a1a; border: 1px solid rgba(56,240,255,0.18);
    color: var(--text-main); padding: 6px 10px; font-family: 'JetBrains Mono', monospace;
    font-size: 13px; width: 100%; box-sizing: border-box;
  }}
  .dispatch-number-input:focus {{ outline: none; border-color: var(--brand); }}
  .dispatch-actions {{
    display: flex; gap: 10px; justify-content: flex-end;
    padding-top: 10px; border-top: 1px solid rgba(56,240,255,0.08);
  }}
  .dispatch-cancel-btn {{
    background: none; border: 1px solid rgba(255,255,255,0.15); color: var(--text-dim);
    padding: 8px 18px; cursor: pointer; font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
  }}
  .dispatch-cancel-btn:hover {{ border-color: var(--text-muted); color: var(--text-muted); }}
  .dispatch-confirm-btn {{
    background: none; border: 1px solid rgba(77,255,180,0.36); color: var(--good);
    padding: 8px 18px; cursor: pointer; font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase;
    box-shadow: 3px 3px 0 rgba(77,255,180,0.12);
  }}
  .dispatch-confirm-btn:hover {{ background: rgba(77,255,180,0.1); }}

  .doc-btn {{
    background: none; border: 1px solid rgba(56,240,255,0.22); color: #d2e6ff;
    padding: 1px 8px; font-size: 10px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.05em; cursor: pointer; white-space: nowrap;
  }}
  .doc-btn:hover {{ border-color: var(--brand); color: var(--brand); }}

  /* Refresh button */
  .refresh-btn {{
    background: none; border: 1px solid rgba(56,240,255,0.2); color: var(--text-dim);
    padding: 4px 10px; font-size: 11px; cursor: pointer; margin-left: auto;
    font-family: inherit;
  }}
  .refresh-btn:hover {{ border-color: var(--brand); color: var(--brand); }}
  @media (max-width: 960px) {{
    .roadmap-hero {{
      grid-template-columns: 1fr;
    }}
  }}
  @media (max-width: 760px) {{
    main {{
      padding: 16px 14px 28px;
    }}
    .summary-grid,
    .roadmap-grid {{
      grid-template-columns: 1fr;
    }}
    .hero-copy,
    .summary-grid,
    .settings,
    .roadmap-stage {{
      padding: 14px;
    }}
    .tab-bar {{
      overflow-x: auto;
      padding-bottom: 2px;
      flex-wrap: nowrap;
    }}
    .tab-btn {{
      flex: 0 0 auto;
    }}
  }}
</style>
</head>
<body>
{site_header_html}
<main>
  <section class="roadmap-hero">
    <div class="hero-copy">
      <div class="hero-kicker">Murphy's planning surface</div>
      <h1>Backlog</h1>
      <p>Fixes, feature work, and completions arranged as a public planning board instead of a raw utility sheet.</p>
    </div>
    <div class="summary-grid">
      <div class="summary-card"><span>Fixes</span><strong id="fix-count">0</strong></div>
      <div class="summary-card"><span>Features</span><strong id="feature-count">0</strong></div>
      <div class="summary-card"><span>Completed</span><strong id="completed-count">0</strong></div>
      <div class="summary-card"><span>Active now</span><strong id="active-count">0</strong></div>
    </div>
  </section>
  <section class="settings">
    <div class="settings-top" onclick="toggleSettings()" style="cursor:pointer">
      <div>
        <div class="settings-kicker">Dispatch</div>
        <h2>Slack handoff</h2>
      </div>
      <div class="settings-state" id="settings-inline-status">No webhook saved on this browser yet.</div>
    </div>
    <p class="settings-copy">Save the webhook locally if you want to launch a backlog item directly from this page.</p>
    <div class="settings-body" id="settings-body">
      <div class="settings-row">
        <label>Webhook URL</label>
        <input type="text" id="cfg-webhook" placeholder="https://hooks.slack.com/services/T.../B.../XXX" />
      </div>
      <div class="settings-row">
        <button class="settings-btn" onclick="saveSettings()">Save</button>
      </div>
      <div class="settings-status" id="settings-status"></div>
    </div>
  </section>
  <section class="roadmap-stage">
    <div class="tab-bar" id="tab-bar"></div>
    <div id="roadmap-body"></div>
  </section>
</main>
<div class="doc-modal-overlay" id="doc-modal-overlay" onclick="if(event.target===this)closeDocModal()">
  <div class="doc-modal">
    <div class="doc-modal-header">
      <span class="doc-modal-title" id="doc-modal-title"></span>
      <button class="doc-modal-refresh" id="doc-modal-refresh" onclick="refreshSession()" title="Refresh">&#x21bb;</button>
      <button class="doc-modal-close" onclick="closeDocModal()">&times;</button>
    </div>
    <div class="doc-modal-body" id="doc-modal-body"></div>
  </div>
</div>
<div class="dispatch-modal-overlay" id="dispatch-modal" onclick="if(event.target===this)closeDispatchModal()">
  <div class="dispatch-modal">
    <div class="dispatch-modal-header">
      <span class="dispatch-modal-title" id="dispatch-modal-title">Dispatch</span>
      <button class="dispatch-modal-close" onclick="closeDispatchModal()">&times;</button>
    </div>
    <div class="dispatch-modal-body">
      <div class="dispatch-field">
        <label class="dispatch-label">Additional instructions (optional)</label>
        <textarea class="dispatch-textarea" id="dispatch-instructions" placeholder="e.g. Focus on error handling, skip tests for now..."></textarea>
      </div>
      <div class="dispatch-rounds-row">
        <div class="dispatch-field">
          <label class="dispatch-label">Plan review rounds</label>
          <input type="number" class="dispatch-number-input" id="dispatch-plan-rounds" min="0" max="20" value="2" />
        </div>
        <div class="dispatch-field">
          <label class="dispatch-label">Impl review rounds</label>
          <input type="number" class="dispatch-number-input" id="dispatch-impl-rounds" min="0" max="20" value="3" />
        </div>
      </div>
      <div class="dispatch-actions">
        <button class="dispatch-cancel-btn" onclick="closeDispatchModal()">Cancel</button>
        <button class="dispatch-confirm-btn" id="dispatch-confirm-btn" onclick="confirmDispatch()">Confirm dispatch</button>
      </div>
    </div>
  </div>
</div>
<script>
const BACKLOG_DATA = {backlog_json};
const COMPLETED_DATA = {completed_json};
const ACTIVE_DEV_ITEMS = {active_dev_json};

function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }}

function toggleSettings() {{
  document.getElementById('settings-body').classList.toggle('open');
}}

var AGENT_USER_ID = 'U0AFZHQMAHX';

function loadSettings() {{
  var wh = localStorage.getItem('dispatch_webhook') || '';
  document.getElementById('cfg-webhook').value = wh;
  updateSettingsStatus();
  // Auto-open settings panel if no webhook configured
  if (!wh) {{
    document.getElementById('settings-body').classList.add('open');
  }}
}}

function saveSettings() {{
  var wh = document.getElementById('cfg-webhook').value.trim();
  localStorage.setItem('dispatch_webhook', wh);
  updateSettingsStatus();
}}

function updateSettingsStatus() {{
  var el = document.getElementById('settings-status');
  var dot = document.getElementById('header-dot');
  var state = document.getElementById('dispatch-state');
  var inline = document.getElementById('settings-inline-status');
  var wh = localStorage.getItem('dispatch_webhook') || '';
  if (wh) {{
    el.textContent = '\u2713 Dispatch configured';
    el.className = 'settings-status ok';
    dot.className = 'status-dot ok';
    state.textContent = '\u2713 Ready';
    state.style.borderColor = 'rgba(77,255,180,0.5)';
    state.style.color = '#4dffb4';
    state.style.background = 'rgba(26,76,63,0.4)';
    inline.textContent = 'This browser can dispatch backlog items directly to Slack.';
  }} else {{
    el.textContent = 'Configure webhook URL to enable dispatch';
    el.className = 'settings-status missing';
    dot.className = 'status-dot missing';
    state.textContent = '\u2699 Configure';
    state.style.borderColor = 'rgba(56,240,255,0.4)';
    state.style.color = '#b8f4ff';
    state.style.background = 'rgba(56,240,255,0.08)';
    inline.textContent = 'No webhook saved on this browser yet.';
  }}
}}

function scrollToSettings() {{
  var el = document.querySelector('.settings');
  if (el) el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
}}

function canDispatch() {{
  return !!localStorage.getItem('dispatch_webhook');
}}

var _dispatchItemId = null;

function dispatchItem(itemId) {{
  if (!canDispatch()) {{ alert('Configure dispatch settings first.'); return; }}
  _dispatchItemId = itemId;
  document.getElementById('dispatch-modal-title').textContent = 'Dispatch ' + itemId;
  document.getElementById('dispatch-instructions').value = '';
  document.getElementById('dispatch-plan-rounds').value = '2';
  document.getElementById('dispatch-impl-rounds').value = '3';
  document.getElementById('dispatch-confirm-btn').disabled = false;
  document.getElementById('dispatch-confirm-btn').textContent = 'Confirm dispatch';
  document.getElementById('dispatch-modal').classList.add('open');
  document.getElementById('dispatch-instructions').focus();
}}

function closeDispatchModal() {{
  document.getElementById('dispatch-modal').classList.remove('open');
  _dispatchItemId = null;
}}

function confirmDispatch() {{
  var itemId = _dispatchItemId;
  if (!itemId) return;
  var extra = (document.getElementById('dispatch-instructions').value || '').trim();
  var planRounds = parseInt(document.getElementById('dispatch-plan-rounds').value, 10) || 2;
  var implRounds = parseInt(document.getElementById('dispatch-impl-rounds').value, 10) || 3;
  var confirmBtn = document.getElementById('dispatch-confirm-btn');
  confirmBtn.textContent = '...';
  confirmBtn.disabled = true;
  var btn = document.querySelector('[data-dispatch="' + itemId + '"]');
  if (btn) {{ btn.textContent = '...'; btn.disabled = true; }}
  var webhook = localStorage.getItem('dispatch_webhook');
  var text = '<@' + AGENT_USER_ID + '> !developer ' + itemId;
  text += ' plan-rounds:' + planRounds + ' impl-rounds:' + implRounds;
  if (extra) text += '\\n' + extra;
  fetch(webhook, {{
    method: 'POST',
    mode: 'no-cors',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ text: text }}),
  }})
  .then(function() {{
    if (btn) {{ btn.textContent = '✓ Dispatched'; btn.className = 'dispatch-btn success'; btn.disabled = true; }}
    var dispatched = JSON.parse(sessionStorage.getItem('dispatched_items') || '[]');
    if (dispatched.indexOf(itemId) < 0) dispatched.push(itemId);
    sessionStorage.setItem('dispatched_items', JSON.stringify(dispatched));
    closeDispatchModal();
  }})
  .catch(function(err) {{
    confirmBtn.textContent = 'Error — retry';
    confirmBtn.disabled = false;
    if (btn) {{ btn.textContent = '▶ Dispatch'; btn.className = 'dispatch-btn'; btn.disabled = false; }}
  }});
}}

function renderBacklog() {{
  var items = BACKLOG_DATA || [];
  var completed = COMPLETED_DATA || [];
  var fixes = items.filter(function(i) {{ return i.queue === 'fix'; }});
  var features = items.filter(function(i) {{ return i.queue === 'feature'; }});
  document.getElementById('fix-count').textContent = String(fixes.length);
  document.getElementById('feature-count').textContent = String(features.length);
  document.getElementById('completed-count').textContent = String(completed.length);
  document.getElementById('active-count').textContent = String((ACTIVE_DEV_ITEMS || []).length);
  var groups = [
    ['Fixes (' + fixes.length + ')', fixes, 'active'],
    ['Features (' + features.length + ')', features, 'active'],
    ['Completed (' + completed.length + ')', completed, 'completed'],
  ];

  var tabs = document.getElementById('tab-bar');
  var savedTab = parseInt(sessionStorage.getItem('backlog-tab') || '0');
  if (savedTab >= groups.length) savedTab = 0;
  tabs.innerHTML = groups.map(function(g, i) {{
    return '<button class="tab-btn' + (i === savedTab ? ' active' : '') + '" data-tab="' + i + '">' + g[0] + '</button>';
  }}).join('') + '<button class="refresh-btn" onclick="location.reload()">&#x21bb; Refresh</button>';

  function renderCard(item) {{
    var isActive = ACTIVE_DEV_ITEMS.indexOf(item.id) >= 0;
    var dispatched = JSON.parse(sessionStorage.getItem('dispatched_items') || '[]');
    var wasDispatched = dispatched.indexOf(item.id) >= 0;
    var statusClass = isActive ? 'pill-in_progress' : 'pill-' + item.status.replace(/ /g, '_');
    var statusText = isActive ? 'in progress' : item.status;
    var isDeferred = item.status === 'deferred';
    var priClass = (item.priority === 'Critical' || item.priority === 'High') ? ' pill-priority-' + item.priority : '';
    var badges = '';
    if (item.has_plan) badges += '<button class="doc-btn" data-doc-type="plan" data-doc-id="' + esc(item.id) + '">Plan</button>';
    if (item.has_issue) badges += '<button class="doc-btn" data-doc-type="issue" data-doc-id="' + esc(item.id) + '">Issue</button>';
    var contextHtml = item.context ? '<div class="roadmap-context">' + esc(item.context) + '</div>' : '';

    return '<div class="roadmap-item" data-priority="' + esc(item.priority) + '">' +
      '<div class="roadmap-top">' +
        '<span class="roadmap-id">' + esc(item.id) + '</span>' +
        '<div class="roadmap-controls">' +
          '<span class="pill ' + statusClass + '">' + esc(statusText) + '</span>' +
          '<button class="dispatch-btn' + (wasDispatched ? ' success' : '') + '" data-dispatch="' + esc(item.id) + '" ' +
            (isDeferred || isActive || wasDispatched ? 'disabled title="' + (isActive ? 'In progress' : wasDispatched ? 'Already dispatched' : 'Deferred') + '"' : '') +
            '>' + (isActive ? '⟳ Running' : wasDispatched ? '✓ Dispatched' : '▶ Dispatch') + '</button>' +
        '</div>' +
      '</div>' +
      '<div class="roadmap-task">' + esc(item.task) + '</div>' +
      contextHtml +
      '<div class="roadmap-meta">' +
        '<span' + priClass + '>' + esc(item.priority) + '</span>' +
        '<span>' + esc(item.created) + '</span>' +
        badges +
        (item.session_id || isActive ? '<button class="view-session-btn" data-session="' + esc(item.id) + '">View Session</button>' : '') +
      '</div>' +
    '</div>';
  }}

  function renderGroup(arr) {{
    if (!arr.length) return '<div class="empty">No items</div>';
    var byAge = function(a, b) {{ return (a.created || '').localeCompare(b.created || ''); }};
    var inProgress = arr.filter(function(i) {{ return ACTIVE_DEV_ITEMS.indexOf(i.id) >= 0 || i.status === 'in_progress'; }}).sort(byAge);
    var rest = arr.filter(function(i) {{ return ACTIVE_DEV_ITEMS.indexOf(i.id) < 0 && i.status !== 'in_progress'; }});
    var high = rest.filter(function(i) {{ return i.priority === 'Critical' || i.priority === 'High'; }}).sort(byAge);
    var medium = rest.filter(function(i) {{ return i.priority === 'Medium' && i.status !== 'deferred'; }}).sort(byAge);
    var low = rest.filter(function(i) {{ return i.priority === 'Low' && i.status !== 'deferred'; }}).sort(byAge);
    var deferred = rest.filter(function(i) {{ return i.status === 'deferred'; }}).sort(byAge);
    var tiers = [
      ['In Progress', inProgress],
      ['High Priority', high],
      ['Medium Priority', medium],
      ['Low Priority', low],
      ['Deferred', deferred],
    ];
    var html = '';
    tiers.forEach(function(tier) {{
      if (!tier[1].length) return;
      html += '<div class="backlog-group-header">' + tier[0] + '<span class="group-count">(' + tier[1].length + ')</span></div>';
      html += '<div class="roadmap-grid">' + tier[1].map(renderCard).join('') + '</div>';
    }});
    return html || '<div class="empty">No items</div>';
  }}

  function renderCompleted(arr) {{
    if (!arr.length) return '<div class="empty">No completed items</div>';
    return '<div class="roadmap-grid">' + arr.map(function(item) {{
      var badges = '';
      if (item.has_plan) badges += '<button class="doc-btn" data-doc-type="plan" data-doc-id="' + esc(item.id) + '">Plan</button>';
      if (item.has_issue) badges += '<button class="doc-btn" data-doc-type="issue" data-doc-id="' + esc(item.id) + '">Issue</button>';
      return '<div class="roadmap-item" style="border-left:3px solid var(--good);opacity:0.75">' +
        '<div class="roadmap-top">' +
          '<span class="roadmap-id">' + esc(item.id) + '</span>' +
          '<span class="pill pill-open" style="color:var(--good);border-color:rgba(77,255,180,0.4)">done</span>' +
        '</div>' +
        '<div class="roadmap-task">' + esc(item.summary) + '</div>' +
        '<div class="roadmap-meta">' +
          '<span>Created: ' + esc(item.created) + '</span>' +
          '<span>Completed: ' + esc(item.completed) + '</span>' +
          badges +
          (item.session_id ? '<button class="view-session-btn" data-session="' + esc(item.id) + '">View Session</button>' : '') +
        '</div>' +
      '</div>';
    }}).join('') + '</div>';
  }}

  var body = document.getElementById('roadmap-body');
  var initGroup = groups[savedTab];
  body.innerHTML = initGroup[2] === 'completed' ? renderCompleted(initGroup[1]) : renderGroup(initGroup[1]);

  tabs.onclick = function(e) {{
    var btn = e.target.closest('[data-tab]');
    if (!btn) return;
    var idx = parseInt(btn.dataset.tab);
    sessionStorage.setItem('backlog-tab', idx);
    tabs.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    btn.classList.add('active');
    var group = groups[idx];
    body.innerHTML = group[2] === 'completed' ? renderCompleted(group[1]) : renderGroup(group[1]);
  }};

  document.querySelector('main').addEventListener('click', function(e) {{
    var btn = e.target.closest('.dispatch-btn');
    if (btn && !btn.disabled) dispatchItem(btn.dataset.dispatch);
    var docBtn = e.target.closest('[data-doc-type]');
    if (docBtn) {{
      var docType = docBtn.dataset.docType;
      var docId = docBtn.dataset.docId;
      var allItems = (BACKLOG_DATA || []).concat(COMPLETED_DATA || []);
      var item = allItems.find(function(i) {{ return i.id === docId; }});
      if (item) {{
        var content = docType === 'plan' ? item.plan_content : item.issue_content;
        var title = docType === 'plan' ? 'Plan: ' + item.id : 'Issue: ' + item.id;
        openDocModal(title, content || 'No content available.');
      }}
    }}
    var sessBtn = e.target.closest('[data-session]');
    if (sessBtn) openSessionModal(sessBtn.dataset.session);
  }});
}}

function openDocModal(title, content, isHtml) {{
  document.getElementById('doc-modal-title').textContent = title;
  var body = document.getElementById('doc-modal-body');
  if (isHtml) {{
    body.innerHTML = content;
    body.style.fontFamily = 'inherit';
    body.style.whiteSpace = 'normal';
  }} else {{
    body.textContent = content;
    body.style.fontFamily = "'SF Mono', 'Fira Code', 'JetBrains Mono', monospace";
    body.style.whiteSpace = 'pre-wrap';
  }}
  document.getElementById('doc-modal-overlay').classList.add('open');
}}
function closeDocModal() {{
  document.getElementById('doc-modal-overlay').classList.remove('open');
  document.getElementById('doc-modal-overlay').removeAttribute('data-session-id');
  document.getElementById('doc-modal-refresh').style.display = 'none';
}}
document.addEventListener('keydown', function(e) {{
  if (e.key === 'Escape') {{ closeDocModal(); closeDispatchModal(); }}
}});

function renderSessionHtml(entries) {{
  if (!entries || !entries.length) return '<div style="padding:12px;color:var(--text-dim)">No session data</div>';
  return entries.map(function(entry) {{
    var roleClass = 'role-' + entry.role;
    var ts = entry.timestamp ? entry.timestamp.replace('T', ' ').replace(/\.\d+Z$/, 'Z') : '';
    var blocksHtml = (entry.blocks || []).map(function(b) {{
      if (b.type === 'text') {{
        return '<div class="session-block session-text">' + esc(b.content) + '</div>';
      }} else if (b.type === 'tool_use') {{
        return '<div class="session-block session-tool">' +
          '<span class="session-tool-name">⚙ ' + esc(b.tool) + '</span>' +
          '<div class="session-tool-input">' + esc(b.input_preview) + '</div>' +
        '</div>';
      }} else if (b.type === 'tool_result') {{
        return '<div class="session-block session-result">' + esc(b.content_preview) + '</div>';
      }} else if (b.type === 'thinking') {{
        return '<div class="session-block session-thinking" data-toggle-thinking="1">' +
          '<span class="session-thinking-label">▸ thinking (' + b.content.length + ' chars)</span>' +
          '<div class="session-thinking-body">' + esc(b.content) + '</div>' +
        '</div>';
      }}
      return '';
    }}).join('');
    return '<div class="session-entry ' + roleClass + '">' +
      '<div class="session-ts">' + esc(ts) + '</div>' +
      blocksHtml +
    '</div>';
  }}).join('');
}}

function fetchAndRenderSession(itemId, autoScroll) {{
  var body = document.getElementById('doc-modal-body');
  var refreshBtn = document.getElementById('doc-modal-refresh');
  var savedScroll = body.scrollTop;
  body.style.opacity = '0.4';
  body.style.pointerEvents = 'none';
  refreshBtn.disabled = true;
  fetch('sessions/' + encodeURIComponent(itemId) + '.json?_ts=' + Date.now())
    .then(function(r) {{
      if (!r.ok) throw new Error('not found');
      return r.json();
    }})
    .then(function(entries) {{
      if (document.getElementById('doc-modal-overlay').getAttribute('data-session-id') !== itemId) return;
      body.innerHTML = renderSessionHtml(entries);
      body.scrollTop = autoScroll ? body.scrollHeight : savedScroll;
    }})
    .catch(function() {{
      if (document.getElementById('doc-modal-overlay').getAttribute('data-session-id') !== itemId) return;
      if (ACTIVE_DEV_ITEMS.indexOf(itemId) >= 0) {{
        return fetch('session.json?_ts=' + Date.now())
          .then(function(r) {{ return r.json(); }})
          .then(function(entries) {{
            if (document.getElementById('doc-modal-overlay').getAttribute('data-session-id') !== itemId) return;
            body.innerHTML = renderSessionHtml(entries);
            body.scrollTop = autoScroll ? body.scrollHeight : savedScroll;
          }})
          .catch(function(err) {{
            body.innerHTML = '<div style="padding:12px;color:var(--bad)">Error: ' + esc(err.message) + '</div>';
          }});
      }} else {{
        body.innerHTML = '<div style="padding:12px;color:var(--text-dim)">No session data available.</div>';
      }}
    }})
    .finally(function() {{
      if (document.getElementById('doc-modal-overlay').getAttribute('data-session-id') !== itemId) return;
      body.style.opacity = '1';
      body.style.pointerEvents = '';
      refreshBtn.disabled = false;
    }});
}}
function openSessionModal(itemId) {{
  openDocModal('Session: ' + itemId, '<div style="padding:12px;color:var(--text-dim)">Loading...</div>', true);
  document.getElementById('doc-modal-overlay').setAttribute('data-session-id', itemId);
  document.getElementById('doc-modal-refresh').style.display = 'flex';
  fetchAndRenderSession(itemId, true);
}}
function refreshSession() {{
  var itemId = document.getElementById('doc-modal-overlay').getAttribute('data-session-id');
  if (itemId) fetchAndRenderSession(itemId, false);
}}

document.getElementById('doc-modal-overlay').addEventListener('click', function(e) {{
  var thinking = e.target.closest('[data-toggle-thinking]');
  if (thinking) thinking.classList.toggle('open');
}});

loadSettings();
renderBacklog();
</script>
</body>
</html>
"""


CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-Users-murphy-Research"
DEV_SESSION_ID_FILE = AGENT_DIR / "runtime" / "dev_session_id"
DEV_SESSION_MAP_FILE = AGENT_DIR / "runtime" / "dev_session_map.json"



def _parse_session_jsonl(session_id: str):
    """Parse a Claude Code session JSONL into a renderable log. Returns list or None."""
    session_file = CLAUDE_SESSIONS_DIR / f"{session_id}.jsonl"
    if not session_file.exists():
        return None

    entries = []
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
                    # First user message is plain text prompt
                    entries.append({
                        "role": "user",
                        "timestamp": timestamp,
                        "blocks": [{"type": "text", "content": msg[:2000]}],
                    })
                    continue
                content = msg.get("content", [])
                if not isinstance(content, list):
                    continue
                blocks = []
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
                            # tool_result content can be a list of blocks
                            text_parts = []
                            for part in content_val:
                                if isinstance(part, dict) and part.get("type") == "text":
                                    text_parts.append(str(part.get("text", "")))
                            content_val = "\n".join(text_parts)
                        blocks.append({"type": "tool_result", "content_preview": redact_secrets(str(content_val)[:1000])})
                if blocks:
                    entries.append({
                        "role": msg_type,
                        "timestamp": timestamp,
                        "blocks": blocks,
                    })
    except Exception:
        return None

    return entries if entries else None


def _read_dev_session_map():
    """Read the persistent item_id → session_id mapping."""
    try:
        return json.loads(DEV_SESSION_MAP_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _build_session_log():
    """Parse the active development session JSONL into a renderable log."""
    try:
        session_id = DEV_SESSION_ID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not session_id:
        return None
    return _parse_session_jsonl(session_id)


def _extract_active_dev_items(tasks_payload):
    """Extract backlog item IDs that have active development tasks."""
    active = set()
    if not isinstance(tasks_payload, dict):
        return active
    for bucket in ("queued", "active"):
        for task in (tasks_payload.get(bucket) or {}).values():
            if not isinstance(task, dict):
                continue
            if str(task.get("task_type") or "") == "development":
                desc = str(task.get("task_description") or "")
                # "Development: FIX-003" → "FIX-003"
                if desc.startswith("Development: "):
                    active.add(desc[len("Development: "):].rstrip("."))
    return sorted(active)



def write_backlog_html(out_dir: Path, backlog, active_dev_items=None):
    """Write the self-contained backlog page to backlog/index.html."""
    backlog_dir = out_dir / "backlog"
    backlog_dir.mkdir(parents=True, exist_ok=True)
    items = backlog.get("items", []) if isinstance(backlog, dict) else backlog
    completed = backlog.get("completed", []) if isinstance(backlog, dict) else []
    # Enrich backlog items with public-facing copy
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
    backlog_json = json_for_script(items)
    completed_json = json_for_script(completed)
    active_dev_json = json_for_script(active_dev_items or [])
    site_header_html = (
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
    html = BACKLOG_HTML_TEMPLATE.format(
        murphy_shell_css="",
        site_header_html=site_header_html,
        backlog_json=backlog_json,
        completed_json=completed_json,
        active_dev_json=active_dev_json,
    )
    atomic_write_text(backlog_dir / "index.html", html)



# ---------------------------------------------------------------------------
# Roadmap (strategic) page template
# ---------------------------------------------------------------------------

ROADMAP_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Murphy Roadmap</title>
<style>

  :root {{
    --bg: #000; --surface: #0a0a0a; --border: #1a1a2e; --text: #d9f1ff;
    --text-dim: #7b8da6; --brand: #4dc3ff; --good: #4dffb4; --warn: #ffc04d; --bad: #ff5c5c;
    --glow-brand: rgba(77,195,255,0.15); --glow-good: rgba(77,255,180,0.12); --glow-warn: rgba(255,192,77,0.12);
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Inter', 'SF Pro Display', -apple-system, sans-serif; }}

  /* --- Vision hero --- */
  .vision-hero {{
    position: relative;
    padding: 56px 32px 48px;
    max-width: 960px;
    margin: 0 auto;
    text-align: center;
    overflow: hidden;
  }}
  .vision-hero::before {{
    content: '';
    position: absolute;
    top: -60%;
    left: 50%;
    transform: translateX(-50%);
    width: 600px;
    height: 600px;
    background: radial-gradient(circle, rgba(77,195,255,0.08) 0%, transparent 70%);
    pointer-events: none;
  }}
  .vision-hero h1 {{
    font-size: 40px;
    font-weight: 300;
    letter-spacing: -0.5px;
    color: #fff;
    margin-bottom: 16px;
  }}
  .vision-hero h1 span {{
    background: linear-gradient(135deg, var(--brand), var(--good));
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    font-weight: 600;
  }}
  .vision-text {{
    color: var(--text-dim);
    font-size: 15px;
    line-height: 1.6;
    max-width: 640px;
    margin: 0 auto;
  }}
  .vision-meta {{
    margin-top: 20px;
    font-size: 12px;
    color: var(--text-dim);
    opacity: 0.5;
  }}

  /* --- Horizon flow --- */
  .horizon-flow {{
    max-width: 1100px;
    margin: 0 auto;
    padding: 0 24px 64px;
    position: relative;
  }}
  /* Vertical spine */
  .horizon-flow::before {{
    content: '';
    position: absolute;
    left: 52px;
    top: 0;
    bottom: 64px;
    width: 1px;
    background: linear-gradient(to bottom, var(--brand), var(--warn), var(--border));
    opacity: 0.4;
  }}

  .horizon-group {{
    position: relative;
    margin-bottom: 48px;
  }}
  .horizon-marker {{
    position: relative;
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 24px;
    padding-left: 36px;
  }}
  .horizon-dot {{
    width: 14px;
    height: 14px;
    border-radius: 50%;
    border: 2px solid;
    position: relative;
    flex-shrink: 0;
    box-shadow: 0 0 12px;
  }}
  .horizon-dot.now {{ border-color: var(--brand); box-shadow: 0 0 16px var(--glow-brand); background: rgba(77,195,255,0.2); }}
  .horizon-dot.next {{ border-color: var(--warn); box-shadow: 0 0 16px var(--glow-warn); background: rgba(255,192,77,0.2); }}
  .horizon-dot.later {{ border-color: var(--text-dim); box-shadow: none; background: rgba(123,141,166,0.15); }}
  .horizon-title {{
    font-size: 13px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 3px;
  }}
  .horizon-title.now {{ color: var(--brand); }}
  .horizon-title.next {{ color: var(--warn); }}
  .horizon-title.later {{ color: var(--text-dim); }}

  /* --- Theme cards --- */
  .themes-container {{
    padding-left: 68px;
    display: flex;
    flex-direction: column;
    gap: 20px;
  }}
  .theme-card {{
    background: linear-gradient(135deg, rgba(10,10,20,0.9), rgba(15,15,30,0.7));
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    transition: border-color 0.3s, box-shadow 0.3s;
  }}
  .theme-card:hover {{
    border-color: rgba(77,195,255,0.25);
    box-shadow: 0 4px 24px rgba(77,195,255,0.06);
  }}
  .theme-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 6px;
  }}
  .theme-name {{
    font-size: 17px;
    font-weight: 600;
    color: #fff;
  }}
  .theme-progress {{
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--text-dim);
  }}
  .progress-ring {{
    width: 28px;
    height: 28px;
    position: relative;
  }}
  .progress-ring svg {{
    transform: rotate(-90deg);
  }}
  .progress-ring .ring-bg {{
    fill: none;
    stroke: var(--border);
    stroke-width: 3;
  }}
  .progress-ring .ring-fill {{
    fill: none;
    stroke: var(--brand);
    stroke-width: 3;
    stroke-linecap: round;
    transition: stroke-dashoffset 0.8s ease;
  }}
  .theme-desc {{
    font-size: 13px;
    color: var(--text-dim);
    line-height: 1.5;
    margin-bottom: 16px;
  }}

  /* --- Goals --- */
  .goal-list {{
    display: flex;
    flex-direction: column;
    gap: 12px;
  }}
  .goal-item {{
    background: rgba(255,255,255,0.02);
    border-radius: 8px;
    padding: 14px 16px;
    border-left: 3px solid transparent;
    transition: background 0.2s;
  }}
  .goal-item:hover {{
    background: rgba(255,255,255,0.04);
  }}
  .goal-item.done {{ border-left-color: var(--good); }}
  .goal-item.in_progress {{ border-left-color: var(--brand); }}
  .goal-item.planned {{ border-left-color: var(--border); }}

  .goal-top {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
  }}
  .goal-name {{
    font-size: 14px;
    font-weight: 600;
    color: var(--text);
  }}
  .goal-badge {{
    font-size: 10px;
    padding: 2px 10px;
    border-radius: 10px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
  }}
  .goal-badge.done {{ background: rgba(77,255,180,0.12); color: var(--good); }}
  .goal-badge.in_progress {{ background: rgba(77,195,255,0.12); color: var(--brand); }}
  .goal-badge.planned {{ background: rgba(123,141,166,0.1); color: var(--text-dim); }}

  .goal-desc {{
    font-size: 12px;
    color: var(--text-dim);
    line-height: 1.5;
    margin-top: 6px;
  }}

  /* --- Milestones as progress chain --- */
  .milestone-chain {{
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 10px;
    align-items: center;
  }}
  .ms-node {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    font-size: 11px;
    padding: 3px 10px;
    border-radius: 12px;
    background: rgba(255,255,255,0.03);
    border: 1px solid var(--border);
    color: var(--text-dim);
    transition: all 0.2s;
  }}
  .ms-node:hover {{
    background: rgba(255,255,255,0.06);
  }}
  .ms-node.done {{
    border-color: rgba(77,255,180,0.3);
    color: var(--good);
  }}
  .ms-node.done::before {{
    content: '\2713';
    font-size: 10px;
  }}
  .ms-node.in_progress {{
    border-color: rgba(77,195,255,0.3);
    color: var(--brand);
  }}
  .ms-node.in_progress::before {{
    content: '';
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--brand);
    animation: pulse 1.5s ease infinite;
  }}
  .ms-node.planned::before {{
    content: '';
    width: 6px;
    height: 6px;
    border-radius: 50%;
    border: 1px solid var(--text-dim);
  }}
  .ms-connector {{
    width: 12px;
    height: 1px;
    background: var(--border);
    flex-shrink: 0;
  }}
  .ms-ids {{
    font-size: 9px;
    opacity: 0.5;
    margin-left: 2px;
  }}

  @keyframes pulse {{
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.4; }}
  }}

  @media (max-width: 700px) {{
    .vision-hero {{ padding: 36px 20px 32px; }}
    .vision-hero h1 {{ font-size: 28px; }}
    .horizon-flow {{ padding: 0 16px 40px; }}
    .horizon-flow::before {{ left: 28px; }}
    .horizon-marker {{ padding-left: 12px; }}
    .themes-container {{ padding-left: 44px; }}
    .theme-card {{ padding: 16px; }}
    .milestone-chain {{ gap: 4px; }}
    .ms-connector {{ width: 6px; }}
  }}
</style>
</head>
<body>
{site_header_html}
<main>
  <section class="vision-hero">
    <h1>Murphy <span>Roadmap</span></h1>
    <p class="vision-text" id="vision-text"></p>
    <p class="vision-meta" id="updated-text"></p>
  </section>
  <div class="horizon-flow" id="roadmap-content"></div>
</main>
<script>
var ROADMAP = {roadmap_json};
function esc(s) {{ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }}

(function() {{
  document.getElementById('vision-text').textContent = ROADMAP.vision || '';
  var updated = ROADMAP.last_updated || '';
  if (updated) document.getElementById('updated-text').textContent = 'Last updated ' + updated;

  var themes = ROADMAP.themes || [];
  var horizons = ['now', 'next', 'later'];
  var horizonLabels = {{now: 'Now', next: 'Next', later: 'Later'}};
  var content = document.getElementById('roadmap-content');
  var html = '';

  function goalProgress(goals) {{
    var total = 0, done = 0;
    goals.forEach(function(g) {{
      (g.milestones || []).forEach(function(m) {{
        total++;
        if (m.status === 'done') done++;
      }});
    }});
    return total > 0 ? Math.round((done / total) * 100) : 0;
  }}

  function progressRingSvg(pct) {{
    var r = 11, c = 2 * Math.PI * r;
    var offset = c - (pct / 100) * c;
    return '<div class="progress-ring"><svg width="28" height="28" viewBox="0 0 28 28">' +
      '<circle class="ring-bg" cx="14" cy="14" r="' + r + '"/>' +
      '<circle class="ring-fill" cx="14" cy="14" r="' + r + '" ' +
        'stroke-dasharray="' + c.toFixed(1) + '" stroke-dashoffset="' + offset.toFixed(1) + '"/>' +
    '</svg></div>';
  }}

  horizons.forEach(function(h) {{
    var themeCards = [];
    themes.forEach(function(theme) {{
      var goals = (theme.goals || []).filter(function(g) {{ return g.horizon === h; }});
      if (goals.length === 0) return;
      var pct = goalProgress(goals);

      var goalsHtml = goals.map(function(goal) {{
        var msNodes = (goal.milestones || []).map(function(m, i) {{
          var ids = (m.backlog_ids || []).length > 0
            ? '<span class="ms-ids">' + m.backlog_ids.map(esc).join(', ') + '</span>'
            : '';
          var connector = i > 0 ? '<span class="ms-connector"></span>' : '';
          return connector + '<span class="ms-node ' + esc(m.status) + '">' + esc(m.name) + ids + '</span>';
        }}).join('');

        return '<div class="goal-item ' + esc(goal.status) + '">' +
          '<div class="goal-top">' +
            '<span class="goal-name">' + esc(goal.name) + '</span>' +
            '<span class="goal-badge ' + esc(goal.status) + '">' + esc(goal.status.replace(/_/g, ' ')) + '</span>' +
          '</div>' +
          '<div class="goal-desc">' + esc(goal.description || '') + '</div>' +
          (msNodes ? '<div class="milestone-chain">' + msNodes + '</div>' : '') +
        '</div>';
      }}).join('');

      themeCards.push(
        '<div class="theme-card">' +
          '<div class="theme-header">' +
            '<span class="theme-name">' + esc(theme.name) + '</span>' +
            '<span class="theme-progress">' + progressRingSvg(pct) + pct + '%</span>' +
          '</div>' +
          '<div class="theme-desc">' + esc(theme.description || '') + '</div>' +
          '<div class="goal-list">' + goalsHtml + '</div>' +
        '</div>'
      );
    }});
    if (themeCards.length === 0) return;
    html += '<div class="horizon-group">' +
      '<div class="horizon-marker">' +
        '<span class="horizon-dot ' + h + '"></span>' +
        '<span class="horizon-title ' + h + '">' + horizonLabels[h] + '</span>' +
      '</div>' +
      '<div class="themes-container">' + themeCards.join('') + '</div>' +
    '</div>';
  }});

  content.innerHTML = html;
}})();
</script>
</body>
</html>
"""



def write_roadmap_html(out_dir: Path, roadmap_data):
    """Write the strategic roadmap page to roadmap/index.html."""
    roadmap_dir = out_dir / "roadmap"
    roadmap_dir.mkdir(parents=True, exist_ok=True)
    roadmap_json = json_for_script(roadmap_data)
    site_header_html = (
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
    html = ROADMAP_HTML_TEMPLATE.format(
        murphy_shell_css="",
        site_header_html=site_header_html,
        roadmap_json=roadmap_json,
    )
    atomic_write_text(roadmap_dir / "index.html", html)


def write_standalone_roadmap_html(roadmap_data):
    """Write the standalone roadmap visualization to docs/dev/roadmap.html."""
    roadmap_json = json_for_script(roadmap_data)
    # Standalone version: no dashboard nav header
    html = ROADMAP_HTML_TEMPLATE.format(
        murphy_shell_css="",
        site_header_html="",
        roadmap_json=roadmap_json,
    )
    out_path = BASE_DIR / "docs" / "dev" / "roadmap.html"
    atomic_write_text(out_path, html)


def write_static_site(out_dir: Path, *, skip_standalone: bool = False):
    payload = sanitize_public_status(build_status())
    atomic_write_text(out_dir / "status.json", json.dumps(payload, default=str, ensure_ascii=False, indent=2))
    bootstrap = {
        "heartbeat": payload.get("heartbeat"),
        "tasks": payload.get("tasks"),
        "backlog": payload.get("backlog"),
        "polling": payload.get("polling") if isinstance(payload.get("polling"), dict) else build_polling_info(),
        "visibility": payload.get("visibility") if isinstance(payload.get("visibility"), dict) else build_visibility_info("public_snapshot"),
    }
    write_html(out_file=out_dir / "index.html", api_status_path="status.json", data=bootstrap)
    backlog_data = payload.get("backlog") or {"items": [], "completed": []}
    active_dev = _extract_active_dev_items(payload.get("tasks"))
    write_backlog_html(out_dir=out_dir, backlog=backlog_data, active_dev_items=active_dev)
    # Write per-item session files for items with dev sessions.
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
    # Legacy session.json for backwards compat.
    session_log = _build_session_log()
    atomic_write_text(
        backlog_dir / "session.json",
        json.dumps(session_log or [], default=str, ensure_ascii=False, indent=2),
    )
    # Strategic roadmap page
    roadmap_data = payload.get("roadmap") or {"vision": "", "themes": [], "last_updated": ""}
    write_roadmap_html(out_dir=out_dir, roadmap_data=roadmap_data)
    if not skip_standalone:
        write_standalone_roadmap_html(roadmap_data)



def run_git(repo_dir: Path, args, timeout_sec: int = 25):
    return subprocess.run(
        ["git", "-C", str(repo_dir), *args],
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )


def _run_git_with_index(repo_dir: Path, args, index_file: str, timeout_sec: int = 25):
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


def _git_check(proc, operation: str):
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
    # Guard: refuse to force-push to main/master — this would destroy the
    # development branch. Catches stale DASHBOARD_GIT_BRANCH=main configs.
    if deploy_branch in ("main", "master"):
        raise RuntimeError(
            f"Refusing to publish to '{deploy_branch}' — this would destroy the "
            f"development branch with an orphan commit. Set DASHBOARD_GIT_BRANCH "
            f"to a dedicated deploy branch (e.g., 'deploy')."
        )

    # Verify this is a git repo (works for both normal repos and submodules
    # where .git is a pointer file).
    git_dir_proc = run_git(out_dir, ["rev-parse", "--git-dir"])
    _git_check(git_dir_proc, "rev-parse --git-dir")

    # Always fetch the latest main from the configured remote.
    fetch_proc = run_git(out_dir, ["fetch", remote, "main"], timeout_sec=60)
    _git_check(fetch_proc, f"fetch {remote} main")

    # Read main's full tree from the remote-tracking ref.
    tree_proc = run_git(out_dir, ["rev-parse", f"{remote}/main^{{tree}}"])
    _git_check(tree_proc, f"rev-parse {remote}/main^{{tree}}")
    main_tree = tree_proc.stdout.strip()

    # Create a temporary index file (system temp dir — safe for submodules
    # where .git is a pointer file, not a directory).
    fd, tmp_index = tempfile.mkstemp(prefix="deploy-idx-")
    os.close(fd)  # Close immediately to avoid fd leak in long-running loops.
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
            blob_proc = run_git(out_dir, ["hash-object", "-w", rel_path])
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
    ls_remote_proc = run_git(out_dir, ["ls-remote", remote, f"refs/heads/{deploy_branch}"])
    if ls_remote_proc.returncode == 0 and ls_remote_proc.stdout.strip():
        remote_sha = ls_remote_proc.stdout.strip().split()[0]
        # Fetch the remote commit so we can inspect its tree.
        run_git(out_dir, ["fetch", remote, remote_sha], timeout_sec=60)
        remote_tree_proc = run_git(out_dir, ["rev-parse", f"{remote_sha}^{{tree}}"])
        if remote_tree_proc.returncode == 0:
            remote_tree = remote_tree_proc.stdout.strip()
            if remote_tree == new_tree:
                return None  # No changes.

    # Create orphan commit (no parent).
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    commit_proc = run_git(
        out_dir, ["commit-tree", new_tree, "-m", f"deploy: snapshot {timestamp}"]
    )
    _git_check(commit_proc, "commit-tree")
    commit_sha = commit_proc.stdout.strip()

    # Force-push via explicit refspec (no local ref update).
    push_proc = run_git(
        out_dir,
        ["push", "--force", remote, f"{commit_sha}:refs/heads/{deploy_branch}"],
        timeout_sec=60,
    )
    _git_check(push_proc, f"push --force {remote} {deploy_branch}")

    return commit_sha


# ---------------------------------------------------------------------------
# Cloudflare Pages public site
# ---------------------------------------------------------------------------

# The template uses %%STATUS_JSON%% as a placeholder for injected status data.
PUBLIC_LANDING_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Murphy | Autonomous Research Agent</title>
<link rel="icon" href="data:,">
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@500;700&family=Rajdhani:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&family=Playfair+Display:ital,wght@1,700&display=swap');
  :root {
    --bg-base: #000; --bg-border: rgba(56,240,255,0.5); --bg-border-strong: rgba(56,240,255,0.8);
    --text-main: #eaffff; --text-muted: #99a6d8; --text-dim: #6d79ad;
    --brand: #38f0ff; --brand-alt: #ff2fb3;
    --good: #4dffb4; --warn: #ffe66a; --bad: #ff6689;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: "Rajdhani", "Avenir Next", "Segoe UI", sans-serif;
    background: #000;
    color: var(--text-main); font-size: 15px;
    min-height: 100vh; position: relative; overflow-x: hidden;
  }
  header {
    display: flex; align-items: center; gap: 12px;
    padding: 14px 20px; background: #000;
    border-bottom: 3px solid #38f0ff;
    flex-wrap: wrap; position: sticky; top: 0; z-index: 2;
  }
  header h1 {
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 18px; font-weight: 700; letter-spacing: 0.15em;
    color: #e9ffff;
    text-shadow: 3px 0 #ff2fb3, -3px 0 #38f0ff;
  }
  .nav-links { display: flex; gap: 8px; margin-left: auto; }
  .nav-link {
    display: inline-block; padding: 5px 14px;
    border: 2px solid var(--bg-border);
    background: rgba(56,240,255,0.06);
    color: var(--text-muted);
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 0.08em;
    text-transform: uppercase; text-decoration: none;
    transition: border-color 150ms, color 150ms, box-shadow 150ms;
  }
  .nav-link:hover, .nav-link.active {
    border-color: var(--bg-border-strong); color: var(--text-main);
    box-shadow: 4px 4px 0 rgba(255,47,179,0.3), -2px -2px 0 rgba(56,240,255,0.2);
  }
  .nav-link.active { background: rgba(56,240,255,0.12); }
  main {
    padding: 16px 20px 24px; display: grid; gap: 10px;
    max-width: 1450px; margin: 0 auto; position: relative; z-index: 1;
  }
  .card {
    background: #000; border: 2px solid var(--bg-border); border-radius: 0;
    overflow: hidden;
    box-shadow: 4px 4px 0 rgba(255,47,179,0.3), -2px -2px 0 rgba(56,240,255,0.2);
  }
  .card:hover {
    box-shadow: 6px 6px 0 rgba(255,47,179,0.5), -3px -3px 0 rgba(56,240,255,0.3);
    border-color: var(--bg-border-strong);
  }
  .card-header {
    padding: 10px 16px;
    background: rgba(56,240,255,0.06);
    border-bottom: 2px solid rgba(56,240,255,0.3);
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.12em; color: #b8f4ff;
    display: flex; align-items: center; gap: 8px;
  }
  .card-body { padding: 14px 16px; }
  .pill {
    display: inline-block; padding: 3px 10px; border-radius: 0;
    border: 2px solid transparent;
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: 11px; font-weight: 700; letter-spacing: 0.06em;
    text-transform: uppercase; text-shadow: 0 0 6px rgba(255,255,255,0.24);
  }
  .pill-online  { background: rgba(26,76,63,0.4); border-color: rgba(77,255,180,0.5); color: var(--good); }
  .pill-offline { background: rgba(95,26,46,0.44); border-color: rgba(255,102,137,0.56); color: var(--bad); }
  .hero-banner {
    font-family: "Orbitron", "Rajdhani", sans-serif;
    font-size: clamp(36px, 6vw, 72px);
    font-weight: 700; letter-spacing: 0.1em;
    text-transform: uppercase; text-align: center;
    padding: 40px 20px 16px;
    color: #e9ffff;
    text-shadow: 3px 0 #ff2fb3, -3px 0 #38f0ff;
    border-bottom: 2px solid rgba(56,240,255,0.3);
    background: rgba(56,240,255,0.03);
  }
  .hero-sub {
    text-align: center; padding: 16px 20px 20px;
    color: var(--text-muted); font-size: 16px; line-height: 1.6;
    max-width: 700px; margin: 0 auto;
  }
  .hb-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
    gap: 4px;
  }
  .hb-item {
    display: flex; flex-direction: column; gap: 2px;
    background: #000; border: 1px solid rgba(56,240,255,0.3);
    border-radius: 0; padding: 6px 10px;
  }
  .hb-label { font-size: 9px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.1em; }
  .hb-val { font-size: 14px; color: var(--text-main); font-weight: 700; }
  .gpu-wrap { margin-top: 10px; }
  .gpu-header { display: flex; justify-content: space-between; font-size: 12px; color: var(--text-muted); margin-bottom: 4px; }
  .gpu-track { height: 8px; background: rgba(56,240,255,0.1); border: 1px solid rgba(56,240,255,0.2); border-radius: 0; }
  .gpu-fill { height: 100%; background: linear-gradient(90deg, var(--brand) 0%, var(--brand-alt) 100%); }
  .gpu-mem { margin-top: 4px; font-size: 12px; color: var(--text-dim); }
  .arch-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; }
  .arch-item { border: 1px solid rgba(56,240,255,0.3); background: #000; padding: 12px 14px; display: grid; gap: 4px; }
  .arch-role { font-size: 9px; color: var(--brand); text-transform: uppercase; letter-spacing: 0.1em; font-family: "Orbitron", "Rajdhani", sans-serif; font-weight: 700; }
  .arch-name { font-size: 18px; font-weight: 700; color: var(--text-main); }
  .arch-desc { font-size: 13px; color: var(--text-muted); line-height: 1.5; }
  .showcase-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 4px; }
  .showcase-item {
    display: block; text-decoration: none; color: var(--text-main);
    border: 1px solid rgba(56,240,255,0.3); background: #000; padding: 12px 14px;
    transition: border-color 150ms, box-shadow 150ms;
  }
  .showcase-item:hover {
    border-color: var(--bg-border-strong);
    box-shadow: 4px 4px 0 rgba(255,47,179,0.3), -2px -2px 0 rgba(56,240,255,0.2);
  }
  .showcase-label { font-size: 9px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.1em; }
  .showcase-title { font-family: "Orbitron", "Rajdhani", sans-serif; font-size: 13px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; margin-top: 2px; }
  .showcase-desc { font-size: 13px; color: var(--text-muted); margin-top: 4px; }
  .ts-footer { margin-top: 10px; font-size: 11px; color: var(--text-dim); font-family: "JetBrains Mono", monospace; }
  .site-footer { padding: 16px 20px; text-align: center; color: var(--text-dim); font-size: 12px; border-top: 1px solid rgba(56,240,255,0.2); }
  .site-footer a { color: var(--brand); text-decoration: none; }
  @media (max-width: 768px) { .arch-grid { grid-template-columns: 1fr; } .nav-links { margin-left: 0; } }
</style>
</head>
<body>
<header>
  <h1>Murphy</h1>
  <div class="nav-links">
    <a class="nav-link active" href="./">Home</a>
    <a class="nav-link" href="./showcase/">Showcase</a>
    <a class="nav-link" href="./">Dashboard</a>
  </div>
</header>
<main>
  <div class="card">
    <div class="hero-banner">Murphy</div>
    <div class="hero-sub">Autonomous research agent. Receives tasks via Slack, conducts research, writes code, consults external experts, and delivers results — 24/7, with parallel workers and adversarial review.</div>
  </div>
  <div class="card">
    <div class="card-header">Supervisor <span class="pill pill-online" id="s-pill">Online</span></div>
    <div class="card-body">
      <div class="hb-grid">
        <div class="hb-item"><span class="hb-label">Status</span><span class="hb-val" id="s-status">--</span></div>
        <div class="hb-item"><span class="hb-label">Loop</span><span class="hb-val" id="s-loops">--</span></div>
        <div class="hb-item"><span class="hb-label">Workers</span><span class="hb-val" id="s-workers">--</span></div>
        <div class="hb-item"><span class="hb-label">Active</span><span class="hb-val" id="s-active">--</span></div>
        <div class="hb-item"><span class="hb-label">Queued</span><span class="hb-val" id="s-queued">--</span></div>
        <div class="hb-item"><span class="hb-label">Finished</span><span class="hb-val" id="s-finished">--</span></div>
      </div>
      <div class="gpu-wrap" id="gpu-section">
        <div class="gpu-header"><span id="gpu-label">GPU</span><span id="gpu-pct">--</span></div>
        <div class="gpu-track"><div class="gpu-fill" id="gpu-fill" style="width:0%"></div></div>
        <div class="gpu-mem" id="gpu-mem"></div>
      </div>
      <div class="ts-footer" id="s-ts"></div>
    </div>
  </div>
  <div class="card">
    <div class="card-header">Architecture</div>
    <div class="card-body">
      <div class="arch-grid">
        <div class="arch-item"><span class="arch-role">Execution</span><span class="arch-name">Worker</span><span class="arch-desc">Acts on tasks. Researches, writes code, generates papers, posts results to Slack.</span></div>
        <div class="arch-item"><span class="arch-role">Repair</span><span class="arch-name">Developer</span><span class="arch-desc">Audits and fixes the system. Runs daily during maintenance. Cannot modify behavioral contracts.</span></div>
        <div class="arch-item"><span class="arch-role">Review</span><span class="arch-name">Tribune</span><span class="arch-desc">Adversarial reviewer. Challenges worker output. No role can certify its own work.</span></div>
      </div>
    </div>
  </div>
  <div class="card">
    <div class="card-header">Showcase</div>
    <div class="card-body">
      <div class="showcase-grid">
        <a class="showcase-item" href="./showcase/"><span class="showcase-label">Gallery</span><div class="showcase-title">Showcase</div><div class="showcase-desc">Tools, telemetry, and experiments.</div></a>
        <a class="showcase-item" href="./tokenizers/"><span class="showcase-label">Interactive</span><div class="showcase-title">Tokenizer Lab</div><div class="showcase-desc">Compare tokenizers across models.</div></a>
        <a class="showcase-item" href="./showcase/signal-deck/"><span class="showcase-label">Telemetry</span><div class="showcase-title">Signal Deck</div><div class="showcase-desc">Production telemetry and task flow.</div></a>
        <a class="showcase-item" href="./showcase/res-publica/"><span class="showcase-label">Architecture</span><div class="showcase-title">Res Publica</div><div class="showcase-desc">Three-role separation exhibit.</div></a>
      </div>
    </div>
  </div>
</main>
<div class="site-footer"><a href="https://github.com/murphytheagent">murphytheagent</a></div>
<script>
var D = %%STATUS_JSON%%;
(function(d) {
  if (!d) return;
  var pill = document.getElementById('s-pill');
  if (d.agent_online) { pill.textContent = 'Online'; pill.className = 'pill pill-online'; }
  else { pill.textContent = 'Offline'; pill.className = 'pill pill-offline'; }
  var map = {
    's-status': d.status || '--',
    's-loops': d.loop_count != null ? d.loop_count.toLocaleString() : '--',
    's-workers': d.max_workers || '--',
    's-active': d.tasks ? d.tasks.active : '--',
    's-queued': d.tasks ? d.tasks.queued : '--',
    's-finished': d.tasks ? d.tasks.finished : '--',
  };
  for (var k in map) { var el = document.getElementById(k); if (el) el.textContent = map[k]; }
  if (d.gpu && d.gpu.count > 0) {
    document.getElementById('gpu-label').textContent = d.gpu.count + 'x ' + d.gpu.name;
    document.getElementById('gpu-pct').textContent = d.gpu.avg_utilization_pct + '% util';
    document.getElementById('gpu-fill').style.width = d.gpu.avg_utilization_pct + '%';
    document.getElementById('gpu-mem').textContent = d.gpu.used_memory_gb + ' / ' + d.gpu.total_memory_gb + ' GB VRAM';
  } else {
    document.getElementById('gpu-section').style.display = 'none';
  }
  if (d.last_updated_utc) {
    document.getElementById('s-ts').textContent = 'Last updated: ' + d.last_updated_utc;
  }
})(D);
</script>
</body>
</html>"""


def build_public_status() -> dict:
    """Build a minimal status dict safe for the public landing page."""
    full = build_status()
    hb = full.get("heartbeat", {})
    gpu_data = full.get("gpu", {})
    tasks = full.get("tasks", {})

    gpus = gpu_data.get("gpus", [])
    gpu_count = len(gpus)
    gpu_name = gpus[0]["name"].split(" Server Edition")[0] if gpus else "N/A"
    avg_util = sum(g.get("utilization_gpu_pct", 0) for g in gpus) // max(gpu_count, 1)
    total_mem = round(sum(g.get("memory_total_mb", 0) for g in gpus) / 1024, 1)
    used_mem = round(sum(g.get("memory_used_mb", 0) for g in gpus) / 1024, 1)

    return {
        "agent_online": hb.get("pid_alive", False) and hb.get("status") not in ("stopped", None),
        "status": hb.get("status", "unknown"),
        "loop_count": hb.get("loop_count", 0),
        "max_workers": hb.get("max_workers", 0),
        "last_updated_utc": hb.get("last_updated_utc", ""),
        "tasks": {
            "active": len(tasks.get("active", {})),
            "queued": len(tasks.get("queued", {})),
            "finished": len(tasks.get("finished", {})),
        },
        "gpu": {
            "count": gpu_count,
            "name": gpu_name,
            "avg_utilization_pct": avg_util,
            "total_memory_gb": total_mem,
            "used_memory_gb": used_mem,
        },
    }


def write_public_site(out_dir: Path) -> None:
    """Assemble the public landing site with injected status data."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Inject status into template
    status_json = json.dumps(build_public_status(), default=str, ensure_ascii=False)
    html = PUBLIC_LANDING_TEMPLATE.replace("%%STATUS_JSON%%", status_json)
    atomic_write_text(out_dir / "index.html", html)

    # Copy static showcase content (only if not already present)
    for dirname in SHOWCASE_COPY_DIRS:
        src = SHOWCASE_SOURCE_DIR / dirname
        dst = out_dir / dirname
        if src.is_dir() and not dst.exists():
            shutil.copytree(src, dst, dirs_exist_ok=True)


def publish_to_cloudflare_pages(out_dir: Path, project_name: str) -> bool:
    """Deploy public site to Cloudflare Pages via wrangler."""
    try:
        result = subprocess.run(
            ["wrangler", "pages", "deploy", str(out_dir),
             "--project-name", project_name, "--commit-dirty=true"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            return True
        print(f"[writer] cf-pages error: {result.stderr.strip()}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("[writer] cf-pages error: wrangler not found on PATH", file=sys.stderr)
        return False
    except Exception as exc:
        print(f"[writer] cf-pages error: {exc}", file=sys.stderr)
        return False


def static_export_loop(
    interval: int,
    static_dir: Path,
    static_git_push: bool = False,
    static_git_remote: str = "origin",
    static_git_branch: Optional[str] = None,
    cf_pages_enabled: bool = False,
    cf_pages_project: str = "",
    cf_pages_dir: Optional[Path] = None,
    cf_pages_interval: int = 900,
):
    cycle = 0
    cf_every_n = max(1, cf_pages_interval // max(interval, 1))
    while True:
        if cycle > 0:
            time.sleep(interval)
        try:
            write_static_site(static_dir)
            if static_git_push:
                commit_hash = publish_to_deploy_branch(static_dir, static_git_remote, static_git_branch or "deploy")
                if commit_hash:
                    print(f"[writer] deploy publish {commit_hash} -> {static_git_remote}:{static_git_branch or 'deploy'}")
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


def resolve_writer_interval(interval_arg: Optional[int]) -> int:
    if interval_arg is not None and interval_arg > 0:
        return interval_arg
    return resolve_supervisor_poll_interval_sec()



# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request noise

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            payload = build_status()
            html = render_html(payload, api_status_path="/api/status")
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        elif self.path == '/api/status':
            body = json.dumps(build_status(), default=str, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        else:
            # Serve static files from the export directory (backlog, roadmap, etc.)
            self._serve_static_file()

    def _serve_static_file(self):
        """Serve static files from the dashboard export directory."""
        export_dir = Path(os.environ.get("DASHBOARD_EXPORT_DIR", "dashboard-export"))
        if not export_dir.is_absolute():
            export_dir = BASE_DIR / export_dir
        # Strip query string and normalize path
        path = self.path.split("?")[0].strip("/")
        if not path or path.endswith("/"):
            path = path + "index.html"
        elif not Path(path).suffix:
            path = path + "/index.html"

        file_path = (export_dir / path).resolve()
        # Security: ensure resolved path is within export_dir
        try:
            file_path.relative_to(export_dir.resolve())
        except ValueError:
            self.send_response(403)
            self.end_headers()
            return

        if not file_path.is_file():
            self.send_response(404)
            self.end_headers()
            return

        content_types = {
            ".html": "text/html; charset=utf-8",
            ".json": "application/json",
            ".css": "text/css",
            ".js": "application/javascript",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }
        ext = file_path.suffix.lower()
        ctype = content_types.get(ext, "application/octet-stream")

        body = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            addr = info[4][0]
            if not addr.startswith("127.") and ":" not in addr:
                ips.add(addr)
    except Exception:
        pass
    return sorted(ips)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", nargs="?", type=int, default=8765,
                        help="HTTP server port (default: 8765)")
    parser.add_argument("--interval", type=int,
                        help="Static export write interval in seconds (defaults to root SLEEP_NORMAL)")
    parser.add_argument("--once", action="store_true",
                        help="Write static export once and exit (requires --export-static-dir)")
    parser.add_argument("--headless", action="store_true",
                        help="Run static export loop in the foreground without starting an HTTP server")
    parser.add_argument("--from-config", action="store_true",
                        help="Load config from .env and DASHBOARD_* env vars (supervisor-compatible semantics)")
    parser.add_argument("--export-static-dir", type=Path,
                        help="Optional static export directory (writes index.html + status.json for GitHub Pages)")
    parser.add_argument("--static-git-push", choices=("off", "on"), default=None,
                        help="When exporting static files, optionally commit+push index/status updates on each write (default: off)")
    parser.add_argument("--static-git-remote", default=None,
                        help="Git remote name for static export push mode (default: origin)")
    parser.add_argument("--static-git-branch", default=None,
                        help="Optional target branch for static export push mode (default: current branch)")
    parser.add_argument("--gpu-monitor", choices=("off", "on"), default=None,
                        help="Optional remote GPU snapshot via SSH (default: on)")
    parser.add_argument("--gpu-node-alias", default=None,
                        help="SSH alias for GPU node")
    parser.add_argument("--gpu-poll-interval-sec", type=int,
                        help="Remote GPU poll interval in seconds when enabled (default: root SLEEP_NORMAL)")
    parser.add_argument("--gpu-command-timeout-sec", type=int, default=None,
                        help="Timeout in seconds for each remote GPU command (default: 4)")
    args = parser.parse_args()

    # --from-config: load .env, re-resolve config file, populate args from
    # env vars -> supervisor_loop.conf -> hardcoded defaults (same precedence
    # as the supervisor's Config class).
    if args.from_config:
        _load_dotenv()
        _reload_config_file()  # pick up LOOP_CONFIG_FILE from .env

        # Fail fast if an explicitly-set config file doesn't exist
        explicit_config = os.environ.get("LOOP_CONFIG_FILE")
        if explicit_config and not _get_config_file().exists():
            print(f"Error: config file not found: {_get_config_file()}", file=sys.stderr)
            sys.exit(1)

        def _cfg(name: str, fallback: str) -> str:
            """Read config: env var -> supervisor_loop.conf -> hardcoded fallback."""
            val = os.environ.get(name)
            if val is not None:
                return val
            val = read_supervisor_default(name)
            if val is not None:
                return val
            return fallback

        if not _parse_bool(_cfg("DASHBOARD_EXPORT_ENABLED", "false")):
            print("Dashboard export disabled (DASHBOARD_EXPORT_ENABLED)")
            return
        if args.export_static_dir is None:
            args.export_static_dir = Path(_cfg("DASHBOARD_EXPORT_DIR", "dashboard-export"))
        if args.static_git_push is None:
            args.static_git_push = "on" if _parse_bool(_cfg("DASHBOARD_GIT_PUSH", "false")) else "off"
        if args.static_git_remote is None:
            args.static_git_remote = _cfg("DASHBOARD_GIT_REMOTE", "origin")
        if args.static_git_branch is None:
            args.static_git_branch = _cfg("DASHBOARD_GIT_BRANCH", "deploy")
        # Cloudflare Pages config
        args.cf_pages_enabled = _parse_bool(_cfg("DASHBOARD_CF_PAGES_ENABLED", "false"))
        args.cf_pages_project = _cfg("DASHBOARD_CF_PAGES_PROJECT", "")
        args.cf_pages_dir = Path(_cfg("DASHBOARD_CF_PAGES_DIR", ".agent/runtime/public-site"))
        args.cf_pages_interval = int(_cfg("DASHBOARD_CF_PAGES_INTERVAL", "900"))

        if args.gpu_monitor is None:
            args.gpu_monitor = "on" if _parse_bool(_cfg("DASHBOARD_GPU_MONITOR", "false")) else "off"
        if args.gpu_node_alias is None:
            args.gpu_node_alias = _cfg("DASHBOARD_GPU_NODE_ALIAS", "")
        if args.gpu_command_timeout_sec is None:
            args.gpu_command_timeout_sec = int(_cfg("DASHBOARD_GPU_COMMAND_TIMEOUT", "4"))

    # Apply defaults for args not set by --from-config or CLI
    if not hasattr(args, "cf_pages_enabled"):
        args.cf_pages_enabled = False
        args.cf_pages_project = ""
        args.cf_pages_dir = Path(".agent/runtime/public-site")
        args.cf_pages_interval = 900
    if args.static_git_push is None:
        args.static_git_push = "off"
    if args.static_git_remote is None:
        args.static_git_remote = "origin"
    if args.gpu_monitor is None:
        args.gpu_monitor = "off"
    if args.gpu_node_alias is None:
        args.gpu_node_alias = ""
    if args.gpu_command_timeout_sec is None:
        args.gpu_command_timeout_sec = 4

    gpu_poll_interval = args.gpu_poll_interval_sec if args.gpu_poll_interval_sec is not None else resolve_supervisor_poll_interval_sec()

    configure_gpu_monitor(
        enabled=(args.gpu_monitor == "on"),
        node_alias=args.gpu_node_alias,
        poll_interval_sec=gpu_poll_interval,
        command_timeout_sec=args.gpu_command_timeout_sec,
    )
    writer_interval = resolve_writer_interval(args.interval)
    static_git_push_enabled = args.static_git_push == "on"

    if args.gpu_monitor == "on":
        print(
            f"GPU monitor: enabled ({args.gpu_node_alias}, poll={max(gpu_poll_interval, 30)}s, "
            f"timeout={max(args.gpu_command_timeout_sec, 2)}s)"
        )
    else:
        print("GPU monitor: disabled for this run (set --gpu-monitor on to enable)")

    if args.export_static_dir:
        # Fail fast if git push is enabled but the export dir is not a git repo.
        # Check for .git as either a directory (normal repo) or file (submodule).
        git_marker = args.export_static_dir / ".git"
        if static_git_push_enabled and not (git_marker.is_dir() or git_marker.is_file()):
            print(
                f"Error: export dir is not a git repo: {args.export_static_dir} "
                f"(git push is enabled but .git is missing)",
                file=sys.stderr,
            )
            sys.exit(1)
        write_static_site(args.export_static_dir, skip_standalone=args.once)
        print(f"Static export: {args.export_static_dir / 'index.html'}")
        print(f"Static status: {args.export_static_dir / 'status.json'}")
        if static_git_push_enabled:
            try:
                deploy_branch = args.static_git_branch or "deploy"
                commit_hash = publish_to_deploy_branch(args.export_static_dir, args.static_git_remote, deploy_branch)
                if commit_hash:
                    print(f"Deploy publish: {commit_hash} -> {args.static_git_remote}:{deploy_branch}")
                else:
                    print("Deploy publish: no changes (tree unchanged)")
            except Exception as exc:
                print(f"Deploy publish error: {exc}", file=sys.stderr)

    if args.once:
        return

    # Headless mode: run static export loop in the main thread (no HTTP server)
    if args.headless:
        if not args.export_static_dir:
            print("Error: --headless requires --export-static-dir (or --from-config)", file=sys.stderr)
            sys.exit(1)
        print(f"Headless mode: static export every {writer_interval}s")
        if static_git_push_enabled:
            print("Static git push mode: enabled")
        print("Press Ctrl-C to stop.")
        try:
            static_export_loop(writer_interval, args.export_static_dir, static_git_push_enabled, args.static_git_remote, args.static_git_branch,
                               args.cf_pages_enabled, args.cf_pages_project, args.cf_pages_dir, args.cf_pages_interval)
        except KeyboardInterrupt:
            print("\nStopped.")
        return

    # Start static-export thread if configured (daemon so it dies with the main process)
    if args.export_static_dir:
        t = threading.Thread(
            target=static_export_loop,
            args=(writer_interval, args.export_static_dir, static_git_push_enabled, args.static_git_remote, args.static_git_branch,
                  args.cf_pages_enabled, args.cf_pages_project, args.cf_pages_dir, args.cf_pages_interval),
            daemon=True,
        )
        t.start()
        print(f"Static export cadence: every {writer_interval}s")
        if static_git_push_enabled:
            print("Static git push mode: enabled")
        if args.cf_pages_enabled:
            print(f"Cloudflare Pages: {args.cf_pages_project} every {args.cf_pages_interval}s")

    # Start HTTP server
    server = http.server.HTTPServer(("0.0.0.0", args.port), Handler)
    print(f"HTTP server:  http://localhost:{args.port}")
    for ip in local_ips():
        print(f"              http://{ip}:{args.port}")
    print("Open the URL above in any browser.")
    print("Press Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
