#!/usr/bin/env python3
"""Sync task conversations into per-project JSON files under .agent/projects/.

Two modes:

  --sync <task_file> --project <slug>
      Upsert a single task's conversation into .agent/projects/<slug>.json.
      Called by the supervisor after each task reconciliation.

  --full
      Scan all task JSONs in .agent/tasks/*/ for those with a "project" field,
      group by project, and rebuild all project JSONs from scratch.
      Useful for initial backfill or recovery.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


TASKS_DIR = Path(".agent/tasks")
PROJECTS_DIR = Path(".agent/projects")


def atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via tempfile + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, str(path))
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def ts_to_iso(ts_str: str) -> str:
    """Convert a Slack-style float timestamp to ISO-8601 UTC string."""
    try:
        return datetime.fromtimestamp(float(ts_str), tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return ""


def derive_last_updated(messages: List[Dict[str, Any]]) -> str:
    """Derive last_updated ISO timestamp from the most recent message ts."""
    if not messages:
        return ""
    max_ts = max((float(m.get("ts", "0") or "0") for m in messages), default=0)
    return ts_to_iso(str(max_ts)) if max_ts > 0 else ""


def derive_status_from_path(task_file: str) -> str:
    """Derive task status from its bucket directory name."""
    parts = Path(task_file).parts
    for bucket in ("finished", "incomplete", "active", "queue"):
        if bucket in parts:
            if bucket == "finished":
                return "done"
            if bucket == "queue":
                return "queued"
            return bucket  # "incomplete", "active"
    return "unknown"


def collect_users(threads: List[Dict[str, Any]]) -> List[str]:
    """Collect deduplicated user_ids from all thread conversations."""
    seen: dict[str, None] = {}
    for thread in threads:
        for msg in thread.get("conversation", []):
            uid = msg.get("user_id", "")
            if uid and uid not in seen:
                seen[uid] = None
    return list(seen.keys())


def read_project_json(slug: str, outdir: Path) -> Dict[str, Any]:
    """Read existing project JSON or return a new empty one."""
    path = outdir / f"{slug}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"project": slug, "summary": "", "users": [], "threads": []}


def read_task_json(task_file: str) -> Dict[str, Any]:
    """Read a task JSON file, returning empty dict on failure."""
    path = Path(task_file)
    if not path.exists():
        print(f"  Warning: task file not found: {task_file}", file=sys.stderr)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"  Warning: could not read {task_file}: {exc}", file=sys.stderr)
        return {}


def build_thread_entry(task_file: str, task_data: Dict[str, Any],
                       existing_summary: str = "") -> Dict[str, Any]:
    """Build a thread entry from a task file path and its parsed data.

    Preserves the existing summary when upserting (summaries are maintained
    by maintenance_reflect, not by this script).
    """
    messages = task_data.get("messages", [])
    return {
        "task_id": task_data.get("task_id", ""),
        "status": derive_status_from_path(task_file),
        "last_updated": derive_last_updated(messages),
        "summary": existing_summary,
        "conversation": messages,
    }


def sync_one(task_file: str, project_slug: str, outdir: Path) -> None:
    """Upsert a single task into a project JSON."""
    task_data = read_task_json(task_file)
    if not task_data:
        return

    project = read_project_json(project_slug, outdir)
    task_id = task_data.get("task_id", "")
    new_entry = build_thread_entry(task_file, task_data)

    # Upsert: match by task_id, preserve existing summary
    threads = project.get("threads", [])
    found = False
    for i, t in enumerate(threads):
        if task_id and t.get("task_id") == task_id:
            existing_summary = t.get("summary", "")
            new_entry = build_thread_entry(task_file, task_data, existing_summary)
            threads[i] = new_entry
            found = True
            break
    if not found:
        threads.append(new_entry)

    project["threads"] = threads
    project["users"] = collect_users(threads)

    atomic_write_json(outdir / f"{project_slug}.json", project)
    print(f"Synced: {task_file} → {project_slug} ({len(threads)} threads)")


def full_rebuild(tasks_dir: Path, outdir: Path) -> None:
    """Scan all task JSONs for project fields and rebuild all project JSONs."""
    project_threads: Dict[str, List[Dict[str, Any]]] = {}

    for bucket in ("finished", "incomplete", "active", "queue"):
        bucket_dir = tasks_dir / bucket
        if not bucket_dir.is_dir():
            continue
        for task_file in sorted(bucket_dir.glob("*.json")):
            task_data = read_task_json(str(task_file))
            if not task_data:
                continue
            slugs = task_data.get("project", [])
            if isinstance(slugs, str):
                slugs = [slugs] if slugs else []
            for slug in slugs:
                if not slug:
                    continue
                entry = build_thread_entry(str(task_file), task_data)
                project_threads.setdefault(slug, []).append(entry)

    outdir.mkdir(parents=True, exist_ok=True)
    for slug, threads in project_threads.items():
        existing = read_project_json(slug, outdir)
        # Build lookup of existing threads by task_id — preserve summaries
        # AND retain threads whose task JSON no longer exists on disk.
        old_by_id: Dict[str, Dict[str, Any]] = {}
        for t in existing.get("threads", []):
            tid = t.get("task_id", "")
            if tid:
                old_by_id[tid] = t

        # Apply existing summaries to scanned threads
        scanned_ids: set[str] = set()
        for t in threads:
            tid = t.get("task_id", "")
            if tid:
                scanned_ids.add(tid)
                if tid in old_by_id:
                    t["summary"] = old_by_id[tid].get("summary", "")

        # Preserve threads whose task file was pruned/deleted — append them
        # so project history is never silently lost by a full rebuild.
        for tid, old_thread in old_by_id.items():
            if tid not in scanned_ids:
                threads.append(old_thread)

        existing["threads"] = threads
        existing["users"] = collect_users(threads)
        atomic_write_json(outdir / f"{slug}.json", existing)
        print(f"Rebuilt: {slug} ({len(threads)} threads)")

    if not project_threads:
        print("No tasks with project tags found.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sync", metavar="TASK_FILE",
                        help="Sync a single task file into its project JSON")
    parser.add_argument("--project", metavar="SLUG",
                        help="Project slug (required with --sync)")
    parser.add_argument("--full", action="store_true",
                        help="Full rebuild: scan all task JSONs and rebuild project JSONs")
    parser.add_argument("--tasks-dir", default=str(TASKS_DIR),
                        help=f"Tasks directory (default: {TASKS_DIR})")
    parser.add_argument("--outdir", default=str(PROJECTS_DIR),
                        help=f"Output directory (default: {PROJECTS_DIR})")
    args = parser.parse_args()

    if args.sync:
        if not args.project:
            parser.error("--project is required with --sync")
        sync_one(args.sync, args.project, Path(args.outdir))
    elif args.full:
        full_rebuild(Path(args.tasks_dir), Path(args.outdir))
    else:
        parser.error("specify --sync TASK_FILE --project SLUG, or --full")


if __name__ == "__main__":
    main()
