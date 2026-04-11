#!/usr/bin/env python3
"""Validate integrity of .agent/ JSON data files.

Checks parsability, schema compliance, cross-references between state.json
and task files, and optionally verifies conversation completeness against
Slack threads.

Incremental by default: skips files unchanged since the last successful run.
Use --full to force a complete scan.

Called by developer review (maintenance phase 1) as a checklist item.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ── Paths ──────────────────────────────────────────────────────────────

AGENT_DIR = Path(".agent")
RUNTIME_DIR = AGENT_DIR / "runtime"
STATE_FILE = RUNTIME_DIR / "state.json"
TASKS_DIR = AGENT_DIR / "tasks"
OUTCOMES_DIR = RUNTIME_DIR / "outcomes"
PROJECTS_DIR = AGENT_DIR / "projects"
LAST_VALIDATION_TS = RUNTIME_DIR / "last_validation_ts"

STATE_BUCKETS = ("queued_tasks", "active_tasks", "incomplete_tasks", "finished_tasks")
TASK_REQUIRED_KEYS = {"task_id", "thread_ts", "channel_id", "messages"}
MSG_REQUIRED_KEYS = {"ts", "role", "text"}
OUTCOME_REQUIRED_KEYS = {"mention_ts", "status"}
SLACK_THREAD_CAP = 20  # max threads to verify per run

# ── Helpers ────────────────────────────────────────────────────────────


def read_json_safe(path: Path) -> Tuple[Any, str | None]:
    """Return (data, None) on success or (None, error_message) on failure."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    except Exception as e:
        return None, f"read error: {e}"


def changed_since(path: Path, since: float) -> bool:
    """True if file was modified after *since* (epoch float)."""
    try:
        return path.stat().st_mtime > since
    except OSError:
        return True  # missing → treat as changed


def resolve_slack_token() -> str:
    """Resolve Slack token from env or .env file. Returns '' if unavailable."""
    for var in ("SLACK_MCP_XOXP_TOKEN", "SLACK_USER_TOKEN"):
        val = os.environ.get(var, "").strip()
        if val:
            return val
    # Fallback: parse from .env
    env_path = Path(".env")
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        m = re.search(r'SLACK_MCP_XOXP_TOKEN\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    return ""


def slack_get(token: str, method: str, params: Dict[str, str]) -> Dict[str, Any]:
    """Single Slack API GET with one retry on 429/5xx."""
    url = f"https://slack.com/api/{method}?{urlencode(params)}"
    req = Request(url, headers={"Authorization": f"Bearer {token}"})
    for attempt in range(2):
        try:
            with urlopen(req, timeout=60) as resp:
                return json.loads(resp.read())
        except HTTPError as e:
            if e.code == 429 or e.code >= 500:
                time.sleep(2.0)
                continue
            return {"ok": False, "error": f"HTTP {e.code}"}
        except (URLError, OSError) as e:
            return {"ok": False, "error": str(e)}
    return {"ok": False, "error": "retries exhausted"}


# ── Phase 1: Local validation ─────────────────────────────────────────


def validate_local(full: bool, since: float) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    checked = 0

    # 1. Parse + schema: task files
    for bucket_dir in TASKS_DIR.iterdir() if TASKS_DIR.exists() else []:
        if not bucket_dir.is_dir():
            continue
        for fp in sorted(bucket_dir.glob("*.json")):
            if not full and not changed_since(fp, since):
                continue
            checked += 1
            data, err = read_json_safe(fp)
            if err:
                errors.append(f"{fp}: {err}")
                continue
            if not isinstance(data, dict):
                errors.append(f"{fp}: root is not a dict")
                continue
            missing = TASK_REQUIRED_KEYS - set(data.keys())
            if missing:
                errors.append(f"{fp}: missing keys {missing}")
                continue
            if not isinstance(data.get("messages"), list):
                errors.append(f"{fp}: 'messages' is not a list")
                continue
            bad_msgs = 0
            for msg in data["messages"]:
                if not isinstance(msg, dict):
                    bad_msgs += 1
                    continue
                if MSG_REQUIRED_KEYS - set(msg.keys()):
                    bad_msgs += 1
            if bad_msgs:
                errors.append(f"{fp}: {bad_msgs} message(s) missing required keys")

    # 2. Parse + schema: outcome files
    if OUTCOMES_DIR.exists():
        for fp in sorted(OUTCOMES_DIR.glob("*.json")):
            if not full and not changed_since(fp, since):
                continue
            checked += 1
            data, err = read_json_safe(fp)
            if err:
                errors.append(f"{fp}: {err}")
                continue
            if not isinstance(data, dict):
                errors.append(f"{fp}: root is not a dict")
                continue
            missing = OUTCOME_REQUIRED_KEYS - set(data.keys())
            if missing:
                errors.append(f"{fp}: missing keys {missing}")

    # 3. Parse + schema: project files
    if PROJECTS_DIR.exists():
        for fp in sorted(PROJECTS_DIR.glob("*.json")):
            if not full and not changed_since(fp, since):
                continue
            checked += 1
            data, err = read_json_safe(fp)
            if err:
                errors.append(f"{fp}: {err}")
                continue
            if not isinstance(data, dict):
                errors.append(f"{fp}: root is not a dict")
                continue
            if "project" not in data:
                errors.append(f"{fp}: missing 'project' key")
            if not isinstance(data.get("threads"), list):
                errors.append(f"{fp}: 'threads' is not a list")

    # 4. state.json schema + cross-references
    state_changed = full or changed_since(STATE_FILE, since)
    if state_changed and STATE_FILE.exists():
        checked += 1
        data, err = read_json_safe(STATE_FILE)
        if err:
            errors.append(f"{STATE_FILE}: {err}")
        elif not isinstance(data, dict):
            errors.append(f"{STATE_FILE}: root is not a dict")
        else:
            # Bucket type check
            for bucket in STATE_BUCKETS:
                val = data.get(bucket)
                if val is not None and not isinstance(val, dict):
                    errors.append(f"{STATE_FILE}: '{bucket}' is not a dict")

            # Cross-references
            all_text_files: set[str] = set()
            for bucket in STATE_BUCKETS:
                for key, task in (data.get(bucket) or {}).items():
                    if not isinstance(task, dict):
                        continue
                    tf = task.get("mention_text_file", "")
                    if tf:
                        all_text_files.add(tf)
                        if not Path(tf).exists():
                            errors.append(f"state.json[{bucket}][{key}]: mention_text_file missing: {tf}")
                    if not task.get("channel_id"):
                        warnings.append(f"state.json[{bucket}][{key}]: missing channel_id")
                    if not task.get("thread_ts"):
                        warnings.append(f"state.json[{bucket}][{key}]: missing thread_ts")

            # Orphan task files
            for bucket_dir in TASKS_DIR.iterdir() if TASKS_DIR.exists() else []:
                if not bucket_dir.is_dir():
                    continue
                for fp in sorted(bucket_dir.glob("*.json")):
                    if str(fp) not in all_text_files:
                        warnings.append(f"orphan task file: {fp}")

            # Orphan outcome files
            all_task_keys: set[str] = set()
            for bucket in STATE_BUCKETS:
                all_task_keys.update((data.get(bucket) or {}).keys())
            if OUTCOMES_DIR.exists():
                for fp in sorted(OUTCOMES_DIR.glob("*.json")):
                    key = fp.stem
                    if key not in all_task_keys:
                        warnings.append(f"orphan outcome file: {fp}")

    print(f"=== Local Validation ===")
    print(f"Checked {checked} files: {len(errors)} errors, {len(warnings)} warnings")
    return errors, warnings


# ── Phase 2: Slack verification ───────────────────────────────────────


def validate_slack(
    token: str, full: bool, since: float
) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    checked = 0

    # Collect task files to verify
    candidates: List[Tuple[Path, Dict[str, Any]]] = []
    for bucket_dir in TASKS_DIR.iterdir() if TASKS_DIR.exists() else []:
        if not bucket_dir.is_dir():
            continue
        for fp in sorted(bucket_dir.glob("*.json")):
            if not full and not changed_since(fp, since):
                continue
            data, err = read_json_safe(fp)
            if err or not isinstance(data, dict):
                continue
            ch = data.get("channel_id", "")
            ts = data.get("thread_ts", "")
            msgs = data.get("messages")
            if ch and ts and isinstance(msgs, list):
                candidates.append((fp, data))

    # Cap to avoid long runs
    if len(candidates) > SLACK_THREAD_CAP:
        candidates = candidates[-SLACK_THREAD_CAP:]

    for fp, data in candidates:
        ch = data["channel_id"]
        ts = data["thread_ts"]
        local_count = len(data["messages"])

        resp = slack_get(token, "conversations.replies", {
            "channel": ch, "ts": ts, "limit": "200"
        })
        if not resp.get("ok"):
            warnings.append(f"{fp}: Slack API error: {resp.get('error', '?')}")
            time.sleep(1.0)
            continue

        slack_msgs = resp.get("messages", [])
        slack_count = len(slack_msgs)
        delta = slack_count - local_count

        if delta > 0:
            errors.append(
                f"{fp}: local has {local_count} messages, "
                f"Slack has {slack_count} ({delta} missing)"
            )

        checked += 1
        time.sleep(1.0)

    # Project JSON freshness
    if PROJECTS_DIR.exists():
        for fp in sorted(PROJECTS_DIR.glob("*.json")):
            if not full and not changed_since(fp, since):
                continue
            data, err = read_json_safe(fp)
            if err or not isinstance(data, dict):
                continue
            proj_mtime = fp.stat().st_mtime
            for thread in data.get("threads") or []:
                tid = thread.get("task_id", "")
                if not tid:
                    continue
                # Find the task file across buckets
                for bucket_dir in TASKS_DIR.iterdir() if TASKS_DIR.exists() else []:
                    tf = bucket_dir / f"{tid}.json"
                    if tf.exists() and tf.stat().st_mtime > proj_mtime:
                        warnings.append(
                            f"{fp}: stale — task {tid} was updated after project JSON"
                        )
                        break

    print(f"\n=== Slack Verification ===")
    print(f"Checked {checked} threads: {len(errors)} gaps, {len(warnings)} warnings")
    return errors, warnings


# ── Main ──────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate .agent/ JSON data integrity."
    )
    parser.add_argument("--full", action="store_true", help="Force full validation")
    parser.add_argument("--skip-slack", action="store_true", help="Skip Slack verification")
    args = parser.parse_args()

    since = 0.0
    if not args.full and LAST_VALIDATION_TS.exists():
        try:
            since = float(LAST_VALIDATION_TS.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass

    all_errors: List[str] = []
    all_warnings: List[str] = []

    # Phase 1
    errs, warns = validate_local(args.full, since)
    all_errors.extend(errs)
    all_warnings.extend(warns)

    # Phase 2
    if not args.skip_slack:
        token = resolve_slack_token()
        if token:
            errs, warns = validate_slack(token, args.full, since)
            all_errors.extend(errs)
            all_warnings.extend(warns)
        else:
            print("\nSkipping Slack verification (no token found)")

    # Summary
    print(f"\n=== Summary ===")
    print(f"Errors: {len(all_errors)}  Warnings: {len(all_warnings)}")
    for e in all_errors:
        print(f"  ERROR: {e}")
    for w in all_warnings:
        print(f"  WARN:  {w}")

    # Record timestamp on clean run
    if not all_errors:
        LAST_VALIDATION_TS.parent.mkdir(parents=True, exist_ok=True)
        LAST_VALIDATION_TS.write_text(str(time.time()), encoding="utf-8")

    return 1 if all_errors else 0


if __name__ == "__main__":
    sys.exit(main())
