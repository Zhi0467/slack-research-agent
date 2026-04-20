"""Lifecycle commands for the unified Murphy CLI."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .common import resolve_repo_root_from_args


def add_lifecycle_subcommands(subparsers: argparse._SubParsersAction) -> None:
    start_parser = subparsers.add_parser("start", help="Start the supervisor in tmux.")
    start_parser.add_argument("--repo-root", help="Explicit Murphy repo root.")
    start_parser.add_argument("--session", default="supervisor", help="tmux session name.")
    start_parser.add_argument("--attach", action="store_true", help="Attach after starting.")
    start_parser.add_argument(
        "--run-once",
        action="store_true",
        help="Start with RUN_ONCE=true for a single supervisor cycle.",
    )
    start_parser.set_defaults(func=command_start)

    restart_parser = subparsers.add_parser("restart", help="Hot-restart the running supervisor.")
    restart_parser.add_argument("--repo-root", help="Explicit Murphy repo root.")
    restart_parser.add_argument(
        "--wait-seconds",
        type=float,
        default=1.0,
        help="Seconds to wait for the heartbeat to refresh after SIGHUP.",
    )
    restart_parser.set_defaults(func=command_restart)

    status_parser = subparsers.add_parser("status", help="Show supervisor heartbeat status.")
    status_parser.add_argument("--repo-root", help="Explicit Murphy repo root.")
    status_parser.set_defaults(func=command_status)

    logs_parser = subparsers.add_parser("logs", help="Show supervisor log output.")
    logs_parser.add_argument("--repo-root", help="Explicit Murphy repo root.")
    logs_parser.add_argument(
        "--last-session",
        action="store_true",
        help="Read .agent/runtime/logs/last_session.log instead of runner.log.",
    )
    logs_parser.add_argument(
        "--tail",
        type=int,
        default=200,
        help="Number of trailing lines to print.",
    )
    logs_parser.set_defaults(func=command_logs)


def _heartbeat_path(repo_root: Path) -> Path:
    return repo_root / ".agent/runtime/heartbeat.json"


def _load_heartbeat(repo_root: Path) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    heartbeat_path = _heartbeat_path(repo_root)
    if not heartbeat_path.exists():
        return None, "supervisor does not appear to be running; use `murphy start`"
    try:
        raw = json.loads(heartbeat_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"failed to parse heartbeat JSON: {exc}"
    if not isinstance(raw, dict):
        return None, "heartbeat payload is not a JSON object"
    return raw, None


def command_start(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root_from_args(args)
    if repo_root is None:
        return 1
    if not _tmux_available():
        print("tmux is not installed or not on PATH.")
        return 1

    session = args.session
    has_session = subprocess.run(
        ["tmux", "has-session", "-t", session],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if has_session.returncode == 0:
        print(f"tmux session '{session}' already exists; refusing to start a duplicate.")
        return 1
    if has_session.returncode not in (0, 1):
        stderr = has_session.stderr.strip() or has_session.stdout.strip()
        print(f"failed to inspect tmux session '{session}': {stderr}")
        return 1

    command = ["./scripts/run.sh"]
    if args.run_once:
        command = ["env", "RUN_ONCE=true", "./scripts/run.sh"]

    start = subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-c", str(repo_root), *command],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if start.returncode != 0:
        stderr = start.stderr.strip() or start.stdout.strip()
        print(f"failed to start supervisor session '{session}': {stderr}")
        return 1

    print(f"started tmux session '{session}' in {repo_root}")

    if args.attach:
        attach = subprocess.run(
            ["tmux", "attach-session", "-t", session],
            cwd=repo_root,
            capture_output=False,
            text=True,
            check=False,
        )
        return attach.returncode
    return 0


def command_restart(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root_from_args(args)
    if repo_root is None:
        return 1

    before, error = _load_heartbeat(repo_root)
    if error:
        print(error)
        return 1
    assert before is not None

    pid = before.get("pid")
    if not isinstance(pid, int):
        print("heartbeat is missing an integer pid.")
        return 1

    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        print("heartbeat pid is stale; use `murphy start` to launch a fresh supervisor.")
        return 1
    except PermissionError:
        print(f"permission denied while signaling pid {pid}.")
        return 1

    previous_timestamp = str(before.get("last_updated_utc") or "")
    deadline = time.time() + max(args.wait_seconds, 0.0)
    refreshed = False
    latest = before
    while time.time() <= deadline:
        latest, error = _load_heartbeat(repo_root)
        if error:
            break
        assert latest is not None
        if str(latest.get("last_updated_utc") or "") != previous_timestamp:
            refreshed = True
            break
        time.sleep(0.1)

    if refreshed:
        print(
            f"sent SIGHUP to pid {pid}; heartbeat advanced to {latest.get('last_updated_utc', '')}."
        )
    else:
        print(f"sent SIGHUP to pid {pid}; waiting for the next heartbeat refresh.")
    return 0


def command_status(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root_from_args(args)
    if repo_root is None:
        return 1

    heartbeat, error = _load_heartbeat(repo_root)
    heartbeat_path = _heartbeat_path(repo_root)
    if error:
        print(error)
        print(f"repo root: {repo_root}")
        print(f"heartbeat: {heartbeat_path}")
        return 1
    assert heartbeat is not None

    print(f"repo root: {repo_root}")
    print(f"heartbeat: {heartbeat_path}")
    print(f"status: {heartbeat.get('status', 'unknown')}")
    print(f"pid: {heartbeat.get('pid', 'unknown')}")
    print(f"loop count: {heartbeat.get('loop_count', 'unknown')}")
    print(f"last updated: {heartbeat.get('last_updated_utc', 'unknown')}")
    print(f"max workers: {heartbeat.get('max_workers', 'unknown')}")
    active_workers = heartbeat.get("active_workers")
    if isinstance(active_workers, list):
        print(f"active workers: {len(active_workers)}")
    return 0


def command_logs(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root_from_args(args)
    if repo_root is None:
        return 1

    log_path = (
        repo_root / ".agent/runtime/logs/last_session.log"
        if args.last_session
        else repo_root / ".agent/runtime/logs/runner.log"
    )
    if not log_path.exists():
        print(f"log file not found: {log_path}")
        return 1
    lines = log_path.read_text(encoding="utf-8").splitlines()
    tail_count = max(args.tail, 0)
    for line in lines[-tail_count:]:
        print(line)
    return 0


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None
