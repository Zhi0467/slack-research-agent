#!/usr/bin/env python3
"""Supervisor runtime implementation."""

from __future__ import annotations

import http.client
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Config
from .filelock import agent_file_lock
from .job_store import AckRequest, JobStore
from .maintenance import MaintenanceManager
from .shell_adapter import ShellAdapter
from .utils import (
    AUTH_FAILURE_PATTERN,
    CAPACITY_PATTERN,
    CHANNEL_ID_RE,
    THREAD_TS_RE,
    TRANSIENT_PATTERN,
    capture_codex_session_id,
    format_waiting_human_context_messages,
    iso_from_ts_floor,
    now_ts,
    short_ts_format,
    system_prompt_hash,
    timestamp_utc,
    ts_gt,
    ts_to_int,
)

def _swap_model_in_cmd(cmd: list[str], model: str) -> list[str]:
    """Replace or insert ``-m <model>`` in a CLI command.

    Handles ``-m <model>``, ``--model <model>``, and ``--model=<model>``.
    A bare trailing ``-m`` or ``--model`` (no following value) is repaired
    in-place so no dangling flag remains in argv.
    """
    cmd = list(cmd)
    for i, arg in enumerate(cmd):
        if arg in ("-m", "--model") and i + 1 < len(cmd):
            cmd[i + 1] = model
            return cmd
        if arg.startswith("--model="):
            cmd[i] = f"--model={model}"
            return cmd
        # Bare trailing -m/--model without a following value: repair in-place.
        if arg in ("-m", "--model"):
            cmd[i] = "-m"
            cmd.insert(i + 1, model)
            return cmd
    cmd.insert(1, "-m")
    cmd.insert(2, model)
    return cmd


def _extract_model(cmd: list[str]) -> str:
    """Extract the model name from a CLI command.

    Handles ``-m <model>``, ``--model <model>``, and ``--model=<model>``.
    """
    for i, arg in enumerate(cmd):
        if arg in ("-m", "--model") and i + 1 < len(cmd):
            return cmd[i + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    return "default"


TASK_SLACK_MENTION_RE = re.compile(r"<@[^>]+>")
TASK_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://[^)]+)\)")
TASK_URL_RE = re.compile(r"https?://\S+")
TASK_WHITESPACE_RE = re.compile(r"\s+")
TASK_SENTENCE_SPLIT_RE = re.compile(r"(?<=\D[.!?])\s+")
TASK_DESCRIPTION_MAX_CHARS = 220

# Paths auto-committed before dispatch so worktree workers always see them.
SYSTEM_FILE_PATHS = [
    "src/",
    "scripts/",
    "docs/",
    "AGENTS.md",
    "ARCHITECTURE.md",
    "CLAUDE.md",
    ".gitignore",
    ".gitmodules",
]

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None


class Supervisor:
    TASK_BUCKET_DIRS = {
        "queued_tasks": "queue",
        "active_tasks": "active",
        "incomplete_tasks": "incomplete",
        "finished_tasks": "finished",
    }
    PROMPT_MEMORY_SECTION_CHAR_LIMIT = 6000
    PROMPT_MEMORY_TOTAL_CHAR_LIMIT_DEFAULT = 20000
    PROMPT_MEMORY_SOFT_GUARD_NOTE = (
        "[memory soft guard] Session memory context clipped to fit prompt budget. "
        'Use `scripts/memory_recall "<query>"` for full context.\n'
    )
    PROMPT_MEMORY_COLLAPSED_BODY = (
        '[omitted by prompt soft guard; use `scripts/memory_recall "<query>"` for full context]'
    )
    PROMPT_MEMORY_POINTER_BODY = (
        '[pointer only to keep initial prompt compact; use `scripts/memory_recall "<query>"` for detailed context]'
    )
    PROMPT_MEMORY_DROPPED_EARLY_NOTE = (
        "[memory soft guard] Dropped earlier long-term memory entries; "
        'use `scripts/memory_recall "<query>"` for older context.\n'
    )

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.loop_count = 0
        self.pending_backoff_sec = cfg.pending_check_initial
        self.transient_backoff_sec = cfg.transient_retry_initial
        self._auth_backoff_sec = cfg.auth_retry_initial
        self.was_pending = False
        self.last_failure_kind = "none"
        self.last_transient_retry_attempt = 0

        self.selected_bucket = ""
        self.selected_key = ""
        self._active_task_type = "slack_mention"
        self._restart_requested = False
        self._maintenance_requested = False
        self.maintenance = MaintenanceManager(self)
        self._job_store = JobStore(self.cfg.jobs_dir)
        self._shell_adapter = ShellAdapter(self._job_store)
        self._last_poll_ts: float = 0.0
        self._last_waiting_refresh_ts: float = 0.0
        self._parallel_slots: list = []
        self._install_signal_handlers()

        self.slack_token = self.resolve_slack_token()
        self._agent_user_id = ""
        self._user_name_cache: Dict[str, str] = {}
        self._seed_user_cache()

        self.cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.runner_log.parent.mkdir(parents=True, exist_ok=True)
        self._backfill_user_profiles()
        self.cfg.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.outcomes_dir.mkdir(parents=True, exist_ok=True)
        for dir_name in self.TASK_BUCKET_DIRS.values():
            (self.cfg.tasks_dir / dir_name).mkdir(parents=True, exist_ok=True)

    def _waiting_refresh_due(self, now: float | None = None) -> bool:
        """Return True when waiting-human refresh should run.

        `_last_waiting_refresh_ts == 0` means the refresh has never run and
        should not be throttled by process uptime.
        """
        if self._last_waiting_refresh_ts <= 0:
            return True
        if now is None:
            now = time.monotonic()
        return now - self._last_waiting_refresh_ts >= self.cfg.waiting_refresh_interval

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGHUP, self._handle_restart_signal)

    def _handle_restart_signal(self, signum: int, frame: Any) -> None:
        self._restart_requested = True
        self.log_line("hot_restart_requested signal=SIGHUP")

    def _exec_restart(self) -> None:
        self._guard_main_branch()
        self.log_line("hot_restart_exec reloading supervisor")
        self.write_heartbeat("restarting", 0, False, 0, self.last_failure_kind, 0)
        os.execv(sys.executable, [sys.executable, "-m", "src.loop.supervisor.main"])

    def _guard_main_branch(self) -> None:
        """Ensure the main repo is on its default branch before hot-restart.

        Worker worktree operations or developer bot commands can
        accidentally leave the main repo checked out on a worker branch.
        If ``os.execv`` runs in that state, the supervisor loads stale code.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=str(self.cfg.repo_root),
                capture_output=True, text=True, timeout=10,
            )
            branch = result.stdout.strip()
            if branch and branch != "main":
                self.log_line(
                    f"branch_guard_fix repo on '{branch}', switching to main"
                )
                subprocess.run(
                    ["git", "checkout", "main"],
                    cwd=str(self.cfg.repo_root),
                    capture_output=True, text=True, timeout=30,
                )
        except Exception as exc:
            self.log_line(f"branch_guard_error error={exc}")

    RESTART_COMMAND_RE = re.compile(
        r"^\s*<@[^>]+>\s*!restart\s*$", re.IGNORECASE
    )
    MAINTENANCE_COMMAND_RE = re.compile(
        r"^\s*<@[^>]+>\s*!maintenance\s*$", re.IGNORECASE
    )
    STOP_COMMAND_RE = re.compile(
        r"^\s*<@[^>]+>\s*!stop\s*$", re.IGNORECASE
    )
    DEV_REVIEW_COMMAND_RE = re.compile(
        r"^\s*<@[^>]+>\s*!developer\s+((?:AGENT|FIX)-\d+)\b([\s\S]*)?$", re.IGNORECASE
    )
    # Matches !loop, !loop-3h, !loop-90m anywhere in text
    LOOP_RE = re.compile(r"!loop(?:-(\d+[hm]?))?", re.IGNORECASE)
    # Pure loop command (entire message is just the loop trigger)
    LOOP_COMMAND_RE = re.compile(
        r"^\s*<@[^>]+>\s*!loop(?:-(\d+[hm]?))?\s*$", re.IGNORECASE
    )

    def _is_restart_command(self, text: str) -> bool:
        """Return True if the mention text is a supervisor restart command."""
        return bool(self.RESTART_COMMAND_RE.search(text))

    def _is_maintenance_command(self, text: str) -> bool:
        """Return True if the mention text is a manual maintenance trigger."""
        return bool(self.MAINTENANCE_COMMAND_RE.search(text))

    def _is_stop_command(self, text: str) -> bool:
        """Return True if the mention text is a loop-stop command."""
        return bool(self.STOP_COMMAND_RE.search(text))

    def _parse_dev_review_command(self, text: str):
        """If text is a !developer command, return (item_id, extra_text); else None."""
        m = self.DEV_REVIEW_COMMAND_RE.search(text)
        if not m:
            return None
        return m.group(1).upper(), (m.group(2) or "").strip()

    _REVIEW_ROUNDS_RE = re.compile(r"(?:plan-rounds|impl-rounds):\d+", re.IGNORECASE)
    _DEFAULT_PLAN_ROUNDS = 2
    _DEFAULT_IMPL_ROUNDS = 3

    def _extract_review_rounds(self, extra_text: str) -> str:
        """Extract plan-rounds:N and impl-rounds:N from extra_text.

        Returns a string like ``plan-rounds:2 impl-rounds:3`` suitable for
        insertion into the ``/iterative-review`` invocation line.  Falls back
        to defaults when values are not specified.
        """
        plan = self._DEFAULT_PLAN_ROUNDS
        impl = self._DEFAULT_IMPL_ROUNDS
        for m in self._REVIEW_ROUNDS_RE.finditer(extra_text):
            token = m.group(0)
            key, val = token.split(":")
            if key.lower() == "plan-rounds":
                plan = int(val)
            elif key.lower() == "impl-rounds":
                impl = int(val)
        return f"plan-rounds:{plan} impl-rounds:{impl}"

    def _parse_loop_duration(self, raw: str | None) -> int:
        """Convert a loop duration string to seconds.

        Accepts: "3h" (hours), "90m" (minutes), bare digits (hours),
        None/empty (default from config).
        """
        if not raw:
            return self.cfg.loop_max_duration_sec
        raw = raw.strip().lower()
        if raw.endswith("h"):
            return int(raw[:-1]) * 3600
        if raw.endswith("m"):
            return int(raw[:-1]) * 60
        return int(raw) * 3600

    def _interruptible_sleep(self, seconds: int) -> None:
        """Sleep in 1-second increments, returning early if restart is requested."""
        for _ in range(seconds):
            if self._restart_requested:
                return
            time.sleep(1)

    def log_line(self, line: str) -> None:
        with self.cfg.runner_log.open("a", encoding="utf-8") as f:
            f.write(f"[{timestamp_utc()}] {line}\n")

    def write_heartbeat(
        self,
        status: str,
        last_exit_code: int,
        pending_decision: bool,
        next_sleep_sec: int,
        last_failure_kind: str,
        transient_retry_attempt: int,
        active_workers: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        payload = {
            "last_updated_utc": timestamp_utc(),
            "status": status,
            "pid": os.getpid(),
            "loop_count": self.loop_count,
            "last_exit_code": last_exit_code,
            "last_failure_kind": last_failure_kind,
            "transient_retry_attempt": transient_retry_attempt,
            "pending_decision": pending_decision,
            "pending_backoff_sec": self.pending_backoff_sec,
            "next_sleep_sec": next_sleep_sec,
            "poll_interval": self.cfg.poll_interval,
            "max_workers": self.cfg.max_concurrent_workers,
        }
        if active_workers is not None:
            payload["active_workers"] = active_workers
        self.atomic_write_json(self.cfg.heartbeat_file, payload)

    @staticmethod
    def atomic_write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)

    @staticmethod
    def read_json(path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    @contextmanager
    def state_lock(self):
        lock_path = Path(str(self.cfg.state_file) + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("w", encoding="utf-8") as lockf:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)

    def load_state(self) -> Dict[str, Any]:
        return self.read_json(self.cfg.state_file, {})

    def save_state(self, state: Dict[str, Any]) -> None:
        self.atomic_write_json(self.cfg.state_file, state)

    def resolve_slack_token(self) -> str:
        if os.environ.get("SLACK_MCP_XOXP_TOKEN"):
            return os.environ["SLACK_MCP_XOXP_TOKEN"]
        if os.environ.get("SLACK_USER_TOKEN"):
            return os.environ["SLACK_USER_TOKEN"]

        cfg_toml = Path(".codex/config.toml")
        if cfg_toml.exists():
            for line in cfg_toml.read_text(encoding="utf-8").splitlines():
                m = re.match(r"\s*SLACK_MCP_XOXP_TOKEN\s*=\s*\"([^\"]+)\"\s*$", line)
                if m:
                    return m.group(1)
        return ""

    def _load_user_directory(self) -> Dict[str, Any]:
        """Load .agent/memory/user_directory.json. Returns empty structure on missing/invalid."""
        path = self.cfg.user_directory_file
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {"agent": {}, "users": {}}

    def _save_user_directory(self, data: Dict[str, Any]) -> None:
        """Atomically write .agent/memory/user_directory.json."""
        self.atomic_write_json(self.cfg.user_directory_file, data)

    def _seed_user_cache(self) -> None:
        """Populate in-memory user name cache from user_directory.json at startup."""
        directory = self._load_user_directory()
        for uid, info in (directory.get("users") or {}).items():
            name = (info.get("user_name") or "").strip()
            if name:
                self._user_name_cache[uid] = name
        agent = directory.get("agent") or {}
        self._agent_user_id = (agent.get("user_id") or "").strip()

    def resolve_slack_id(self) -> str:
        if self._agent_user_id:
            return self._agent_user_id
        # Try user_directory.json
        directory = self._load_user_directory()
        agent_id = ((directory.get("agent") or {}).get("user_id") or "").strip()
        if agent_id:
            self._agent_user_id = agent_id
            return agent_id
        return ""


    @staticmethod
    def _safe_task_filename(task_id: str) -> str:
        name = str(task_id or "").strip()
        name = name.replace("/", "_").replace("\\", "_")
        if not name or name in {".", ".."}:
            return "unknown_task.json"
        return f"{name}.json"

    @classmethod
    def bucket_dir_for_state_bucket(cls, bucket_name: str) -> str:
        return cls.TASK_BUCKET_DIRS.get(str(bucket_name or ""), "incomplete")

    @classmethod
    def bucket_name_for_status(cls, status: str) -> str:
        s = str(status or "")
        if s == "done":
            return "finished_tasks"
        if s == "queued":
            return "queued_tasks"
        return "incomplete_tasks"

    def task_text_path(self, task_id: str, bucket_name: str = "") -> Path:
        dir_name = self.bucket_dir_for_state_bucket(bucket_name)
        return self.cfg.tasks_dir / dir_name / self._safe_task_filename(task_id)

    # ------------------------------------------------------------------
    # Task JSON I/O
    # ------------------------------------------------------------------

    def _empty_task_data(self, task_id: str = "", thread_ts: str = "",
                         channel_id: str = "") -> Dict[str, Any]:
        return {
            "task_id": task_id,
            "thread_ts": thread_ts,
            "channel_id": channel_id,
            "messages": [],
        }

    def read_task_json(self, mention_text_file: str) -> Dict[str, Any]:
        """Read a task JSON file.  Returns empty structure on missing/invalid."""
        if not mention_text_file:
            return self._empty_task_data()
        path = Path(mention_text_file)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "messages" in data:
                    # Backfill task_id from filename if empty
                    if not data.get("task_id"):
                        data["task_id"] = path.stem
                    if not data.get("thread_ts"):
                        data["thread_ts"] = path.stem
                    return data
            except Exception:
                pass
        return self._empty_task_data()

    def write_task_json(self, mention_text_file: str, data: Dict[str, Any]) -> None:
        """Atomically write a task JSON file."""
        path = Path(mention_text_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.atomic_write_json(path, data)

    @staticmethod
    def _task_json_to_text(data: Dict[str, Any]) -> str:
        """Reconstruct a text representation from task JSON for dispatch mention_text."""
        messages = data.get("messages") or []
        if not messages:
            return ""

        regular: List[str] = []
        snapshot: List[str] = []
        for msg in messages:
            text = str(msg.get("text") or "")
            if not text:
                continue
            if msg.get("source") == "context_snapshot":
                ts = str(msg.get("ts") or "")
                user_id = str(msg.get("user_id") or "unknown")
                role = str(msg.get("role") or "unknown")
                ts_human = f"{iso_from_ts_floor(ts)} ({ts})" if ts else "(unknown)"
                snapshot.append(f"[{ts_human} | user: {user_id} | role: {role}]\n{text}")
            else:
                regular.append(text)

        parts = list(regular)
        if snapshot:
            parts.append("\n[Context update: full thread snapshot]\n\n" + "\n\n".join(snapshot))
        return "\n\n".join(parts)

    def read_task_text(self, mention_text_file: str) -> str:
        """Read task file and return text for dispatch / description extraction."""
        data = self.read_task_json(mention_text_file)
        return self._task_json_to_text(data)

    def read_task_text_for_prompt(self, mention_text_file: str) -> str:
        """Read task file and return bounded text suitable for worker prompts.

        Unlike ``read_task_text`` which returns the full reconstruction, this
        method enforces ``thread_context_max_messages`` and
        ``thread_context_max_chars`` to prevent unbounded prompt inflation.

        Preserves:
          - All regular (non-snapshot) messages (original mentions)
          - The earliest snapshot message (objective context)
          - The most recent snapshot messages up to the message cap

        Adds a clipping note when snapshot messages are dropped.
        """
        data = self.read_task_json(mention_text_file)
        messages = data.get("messages") or []
        if not messages:
            return ""

        max_msgs = self.cfg.thread_context_max_messages
        max_chars = self.cfg.thread_context_max_chars

        regular: List[str] = []
        snapshot_msgs: List[Dict[str, Any]] = []
        for msg in messages:
            text = str(msg.get("text") or "")
            if not text:
                continue
            if msg.get("source") == "context_snapshot":
                snapshot_msgs.append(msg)
            else:
                regular.append(text)

        # Format snapshot messages, preserving first + last N within budget
        snapshot_formatted: List[str] = []
        clipped_count = 0
        if snapshot_msgs:
            if len(snapshot_msgs) <= max_msgs:
                kept = snapshot_msgs
            else:
                # Keep first message (objective) + last (max_msgs - 1) messages
                kept = [snapshot_msgs[0]] + snapshot_msgs[-(max_msgs - 1):]
                clipped_count = len(snapshot_msgs) - len(kept)

            for msg in kept:
                ts = str(msg.get("ts") or "")
                user_id = str(msg.get("user_id") or "unknown")
                role = str(msg.get("role") or "unknown")
                ts_human = f"{iso_from_ts_floor(ts)} ({ts})" if ts else "(unknown)"
                snapshot_formatted.append(
                    f"[{ts_human} | user: {user_id} | role: {role}]\n{str(msg.get('text') or '')}"
                )

        parts = list(regular)
        if snapshot_formatted:
            header = "\n[Context update: full thread snapshot]\n\n"
            if clipped_count > 0:
                # Insert clipping note after first message
                snapshot_formatted.insert(
                    1, f"[... {clipped_count} earlier messages clipped for prompt budget ...]"
                )
            parts.append(header + "\n\n".join(snapshot_formatted))

        result = "\n\n".join(parts)

        # Hard character cap
        if len(result) > max_chars:
            self.log_line(
                f"thread_context_clipped original_chars={len(result)} "
                f"max_chars={max_chars} clipped_messages={clipped_count}"
            )
            # Keep the beginning (objective) up to preserve budget, then tail
            preserve = self.cfg.thread_context_preserve_objective_chars
            if preserve >= max_chars:
                result = result[:max_chars]
            else:
                tail_budget = max_chars - preserve
                head = result[:preserve]
                tail = result[-tail_budget:]
                result = (
                    head
                    + "\n[... clipped for prompt budget ...]\n"
                    + tail
                )

        return result

    # -- Fields stripped from the dispatch JSON when rendering into the prompt --
    _DISPATCH_INTERNAL_FIELDS = frozenset({
        "mention_text",           # rendered separately as {{THREAD_CONTEXT}}
        "mention_text_file",      # internal file path
        "claimed_by",             # internal scheduler state
        "created_ts",             # redundant with source.time_iso
        "last_update_ts",         # internal scheduler state
        "last_error",             # internal scheduler state
        "last_seen_mention_ts",   # internal scheduler state
        "watchdog_retries",       # internal scheduler state
        "consecutive_exit_failures",  # parallel crash-loop guard
        # Session resume fields (AGENT-025)
        "codex_session_id",       # codex session UUID for resume
        "session_prompt_hash",    # prompt hash at dispatch time
        "session_slot_id",        # slot that last ran this task
        "session_end_ts",         # last thread message ts at reconcile
        "session_dispatch_mode",  # "serial" or "parallel"
        "dispatch_prompt_hash",   # prompt hash stamped before render
        "session_task_id",        # task identity bound to session
        # AGENT-057: thread continuation internal fields
        "continuation_pending",   # flag consumed at dispatch time
        "waiting_reason",         # why task is waiting_human
    })

    def _render_thread_context(self, mention_text_file: str) -> tuple:
        """Render thread context as a chat transcript for the worker prompt.

        Returns ``(original_request, thread_context)`` where
        *original_request* is the raw first mention text and
        *thread_context* is the formatted chat transcript of subsequent
        thread messages (snapshot[0] skipped to avoid duplication).

        Format per message::

            [display_name, 2026 Mar 22 00:53 UTC]
            message text here

        Uses resolved user names (already stored in task JSON) and maps
        the agent's own user_id to "Murphy".
        """
        if not mention_text_file:
            return "", ""
        data = self.read_task_json(mention_text_file)
        messages = data.get("messages") or []
        if not messages:
            return "", ""

        agent_uid = self.resolve_slack_id()
        max_msgs = self.cfg.thread_context_max_messages
        max_chars = self.cfg.thread_context_max_chars

        regular: List[str] = []
        first_regular_ts: str = ""
        snapshot_msgs: List[Dict] = []
        prior_thread_msgs: List[Dict] = []
        for msg in messages:
            text = str(msg.get("text") or "")
            if not text:
                continue
            if msg.get("source") == "prior_thread_context":
                prior_thread_msgs.append(msg)
            elif msg.get("source") == "context_snapshot":
                snapshot_msgs.append(msg)
            else:
                regular.append(text)
                if not first_regular_ts:
                    first_regular_ts = str(msg.get("ts") or "")

        # Original request = first regular message (the @mention)
        original_request = regular[0] if regular else ""

        # Format snapshot messages, skipping only the one that duplicates the
        # original mention (same ts as first regular). Follow-up @mentions
        # that were stored as additional regulars still appear in snapshots.
        non_dup_snapshots = [
            m for m in snapshot_msgs
            if not first_regular_ts or str(m.get("ts") or "") != first_regular_ts
        ]

        formatted: List[str] = []
        clipped_count = 0
        if non_dup_snapshots:
            if len(non_dup_snapshots) <= max_msgs:
                kept = non_dup_snapshots
            else:
                kept = [non_dup_snapshots[0]] + non_dup_snapshots[-(max_msgs - 1):]
                clipped_count = len(non_dup_snapshots) - len(kept)

            for msg in kept:
                ts = str(msg.get("ts") or "")
                user_id = str(msg.get("user_id") or "unknown")
                user_name = msg.get("user_name") or user_id
                display_name = "Murphy" if user_id == agent_uid else user_name
                ts_str = short_ts_format(ts) if ts else ""
                header = f"[{display_name}, {ts_str}]" if ts_str else f"[{display_name}]"
                formatted.append(f"{header}\n{str(msg.get('text') or '')}")

        # AGENT-057: Prepend prior thread context if available
        prior_formatted: List[str] = []
        if prior_thread_msgs:
            for msg in prior_thread_msgs:
                ts = str(msg.get("ts") or "")
                user_id = str(msg.get("user_id") or "unknown")
                user_name = msg.get("user_name") or user_id
                display_name = "Murphy" if user_id == agent_uid else user_name
                ts_str = short_ts_format(ts) if ts else ""
                header = f"[{display_name}, {ts_str}]" if ts_str else f"[{display_name}]"
                prior_formatted.append(f"{header}\n{str(msg.get('text') or '')}")

        if formatted:
            if clipped_count > 0:
                formatted.insert(1, f"[... {clipped_count} earlier messages clipped ...]")

        all_parts: List[str] = []
        if prior_formatted:
            all_parts.append(
                f"[Prior thread context — {len(prior_formatted)} messages from previous thread]"
            )
            all_parts.extend(prior_formatted)
            if formatted:
                all_parts.append("[Current thread]")
        all_parts.extend(formatted)

        thread_context = "\n\n".join(all_parts)

        # Hard character cap on thread context
        if len(thread_context) > max_chars:
            preserve = self.cfg.thread_context_preserve_objective_chars
            if preserve >= max_chars:
                thread_context = thread_context[:max_chars]
            else:
                tail_budget = max_chars - preserve
                head = thread_context[:preserve]
                tail = thread_context[-tail_budget:]
                thread_context = head + "\n[... clipped for prompt budget ...]\n" + tail

        return original_request, thread_context

    def remove_task_text_file(self, mention_text_file: str) -> None:
        if not mention_text_file:
            return
        path = Path(mention_text_file)
        try:
            resolved = path.resolve()
            tasks_root = self.cfg.tasks_dir.resolve()
            resolved.relative_to(tasks_root)
        except Exception:
            return

        try:
            if path.is_file():
                path.unlink()
        except Exception:
            pass

    def ensure_task_text_file(self, task: Dict[str, Any], bucket_name: str = "", legacy_text: str = "") -> str:
        task_id = str(task.get("thread_ts") or task.get("mention_ts") or "0")
        if not bucket_name:
            bucket_name = self.bucket_name_for_status(str(task.get("status") or "in_progress"))

        desired_path = self.task_text_path(task_id, bucket_name)
        current = str(task.get("mention_text_file") or "")
        current_path = Path(current) if current else None

        if current_path and current_path != desired_path and current_path.exists():
            desired_path.parent.mkdir(parents=True, exist_ok=True)
            if not desired_path.exists():
                os.replace(current_path, desired_path)
            else:
                desired_data = self.read_task_json(str(desired_path))
                current_data = self.read_task_json(str(current_path))
                if current_data.get("messages") and not desired_data.get("messages"):
                    self.write_task_json(str(desired_path), current_data)
                try:
                    current_path.unlink()
                except Exception:
                    pass

        mention_text_file = str(desired_path)
        task["mention_text_file"] = mention_text_file

        path = desired_path
        if path.exists():
            if legacy_text:
                existing = self.read_task_json(mention_text_file)
                if not existing.get("messages"):
                    data = self._empty_task_data(task_id=task_id)
                    data["messages"].append({
                        "ts": str(task.get("mention_ts") or ""),
                        "user_id": str(((task.get("source") or {}).get("user_id") or "")),
                        "role": "human",
                        "text": legacy_text.strip(),
                    })
                    self.write_task_json(mention_text_file, data)
            return mention_text_file

        data = self._empty_task_data(task_id=task_id)
        if legacy_text:
            data["messages"].append({
                "ts": str(task.get("mention_ts") or ""),
                "user_id": str(((task.get("source") or {}).get("user_id") or "")),
                "role": "human",
                "text": legacy_text.strip(),
            })
        self.write_task_json(mention_text_file, data)
        return mention_text_file

    def append_mentions_to_task_text(self, mention_text_file: str, mentions: List[Dict[str, Any]]) -> None:
        if not mentions:
            return
        data = self.read_task_json(mention_text_file)
        existing_ts = {str(m.get("ts") or "") for m in data.get("messages", []) if m.get("ts")}

        mentions = sorted(mentions, key=lambda m: ts_to_int(str(m.get("mention_ts") or "0")))
        added = False
        for mention in mentions:
            mention_ts = str(mention.get("mention_ts") or "")
            if mention_ts in existing_ts:
                continue

            user_id = str(((mention.get("source") or {}).get("user_id") or ""))
            user_name = str(((mention.get("source") or {}).get("user_name") or ""))
            thread_ts = str(mention.get("thread_ts") or "")
            channel_id = str(mention.get("channel_id") or "")
            text = str(mention.get("mention_text") or "").strip()
            if not text:
                text = "[no text]"

            name = user_name or self.resolve_user_name(user_id)
            msg: Dict[str, Any] = {
                "ts": mention_ts,
                "user_id": user_id,
                "role": "human",
                "text": text,
            }
            if name:
                msg["user_name"] = name
            data["messages"].append(msg)
            added = True

            # Populate top-level fields from first mention if empty
            if not data.get("task_id"):
                data["task_id"] = mention_ts
            if not data.get("thread_ts"):
                data["thread_ts"] = thread_ts
            if not data.get("channel_id"):
                data["channel_id"] = channel_id

        if added:
            self.write_task_json(mention_text_file, data)

    @staticmethod
    def summarize_task_description_from_text(text: str) -> str:
        raw = str(text or "").strip()
        if not raw:
            return ""

        raw = TASK_MARKDOWN_LINK_RE.sub(r"\1", raw)
        raw = TASK_SLACK_MENTION_RE.sub("", raw)
        raw = raw.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        raw = raw.replace("`", "")
        raw = TASK_URL_RE.sub("[link]", raw)
        raw = TASK_WHITESPACE_RE.sub(" ", raw).strip(" -:;,.")
        if not raw:
            return ""

        parts = [p.strip() for p in TASK_SENTENCE_SPLIT_RE.split(raw) if p.strip()]
        sentence = parts[0] if parts else raw
        if sentence:
            sentence = sentence[0].upper() + sentence[1:]

        if len(sentence) > TASK_DESCRIPTION_MAX_CHARS:
            sentence = sentence[: TASK_DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"
        if sentence and sentence[-1] not in ".!?…":
            sentence += "."
        return sentence

    @staticmethod
    def looks_like_task_metadata(text: str) -> bool:
        lowered = str(text or "").lower()
        return (
            "## mention (mention_ts=" in lowered
            or "- thread id:" in lowered
            or "[context update:" in lowered
        )

    @staticmethod
    def first_task_objective_from_json(data: Dict[str, Any]) -> str:
        """Return the text of the first human message from a task JSON dict."""
        for msg in data.get("messages") or []:
            if msg.get("source") == "context_snapshot":
                continue
            text = str(msg.get("text") or "").strip()
            if text and text != "[no text]":
                return text
        return ""

    def derive_task_description(self, task: Dict[str, Any]) -> str:
        existing = self.summarize_task_description_from_text(str(task.get("task_description") or ""))
        if existing and not self.looks_like_task_metadata(existing):
            return existing

        # Try task JSON file — extract first human message
        mention_text_file = str(task.get("mention_text_file") or "")
        if mention_text_file:
            data = self.read_task_json(mention_text_file)
            candidate = self.first_task_objective_from_json(data)
            if candidate:
                desc = self.summarize_task_description_from_text(candidate)
                if desc:
                    return desc

        fallback = self.summarize_task_description_from_text(str(task.get("summary") or ""))
        if fallback and not self.looks_like_task_metadata(fallback):
            return fallback
        return "Resolve the objective requested in this task thread."

    def normalize_task(
        self,
        task: Dict[str, Any],
        key: str,
        force_done: bool = False,
        bucket_name: str = "",
    ) -> Dict[str, Any]:
        task_type = str(task.get("task_type") or "slack_mention")
        thread = str(task.get("thread_ts") or key or task.get("mention_ts") or task.get("last_update_ts") or "0")
        mention = str(task.get("mention_ts") or key or thread)
        status = "done" if force_done else str(task.get("status") or "in_progress")
        path_bucket = bucket_name or self.bucket_name_for_status(status)
        channel_id = str(task.get("channel_id") or task.get("channel") or "")
        if not channel_id and self.cfg.default_channel_id:
            channel_id = self.cfg.default_channel_id
        mention_text_file = str(task.get("mention_text_file") or self.task_text_path(thread, path_bucket))
        task_description = self.derive_task_description(task)
        out = {
            "mention_ts": mention,
            "thread_ts": thread,
            "channel_id": channel_id,
            "mention_text_file": mention_text_file,
            "status": status,
            "claimed_by": task.get("claimed_by"),
            "summary": str(task.get("summary") or ""),
            "task_description": task_description,
            "report_path": str(task.get("report_path") or f"reports/{thread}.md"),
            "created_ts": str(task.get("created_ts") or mention),
            "last_update_ts": str(task.get("last_update_ts") or mention),
            "source": task.get("source") or {"user_id": "", "user_name": "", "time_iso": ""},
            "task_type": task_type,
            "last_error": task.get("last_error"),
            "last_seen_mention_ts": str(task.get("last_seen_mention_ts") or task.get("last_update_ts") or mention),
            **({"project": task["project"]} if "project" in task else {}),
            **({"watchdog_retries": task["watchdog_retries"]} if "watchdog_retries" in task else {}),
            **({"consecutive_exit_failures": task["consecutive_exit_failures"]} if "consecutive_exit_failures" in task else {}),
            **({"loop_mode": task["loop_mode"]} if task.get("loop_mode") else {}),
            **({"loop_deadline": task["loop_deadline"]} if "loop_deadline" in task and task.get("loop_mode") else {}),
            **({"loop_iteration": task["loop_iteration"]} if "loop_iteration" in task and task.get("loop_mode") else {}),
            **({"loop_next_dispatch_after": task["loop_next_dispatch_after"]} if "loop_next_dispatch_after" in task and task.get("loop_mode") else {}),
            **({"loop_worker_status": task["loop_worker_status"]} if "loop_worker_status" in task and task.get("loop_mode") else {}),
            # Tribune post-dispatch review state (preserved across re-dispatches).
            **({"tribune_revision_count": task["tribune_revision_count"]} if "tribune_revision_count" in task else {}),
            **({"tribune_feedback": task["tribune_feedback"]} if "tribune_feedback" in task else {}),
            # FIX-022: session resume fields preserved across normalize for reopen resume.
            **({"codex_session_id": task["codex_session_id"]} if "codex_session_id" in task else {}),
            **({"session_prompt_hash": task["session_prompt_hash"]} if "session_prompt_hash" in task else {}),
            **({"session_end_ts": task["session_end_ts"]} if "session_end_ts" in task else {}),
            **({"dispatch_prompt_hash": task["dispatch_prompt_hash"]} if "dispatch_prompt_hash" in task else {}),
            # AGENT-057: thread continuation fields
            **({"prior_threads": task["prior_threads"]} if "prior_threads" in task else {}),
            **({"continuation_pending": task["continuation_pending"]} if "continuation_pending" in task else {}),
            **({"waiting_reason": task["waiting_reason"]} if "waiting_reason" in task else {}),
        }
        if self.maintenance.is_maintenance_task(task_type):
            out["maintenance_phase"] = self.maintenance.get_phase(task)
        elif "maintenance_phase" in task:
            out["maintenance_phase"] = task["maintenance_phase"]
        return out

    def ensure_state_schema(self) -> None:
        with self.state_lock():
            state = self.load_state()
            if not state:
                self.save_state(
                    {
                        "watermark_ts": "0",
                        "active_tasks": {},
                        "queued_tasks": {},
                        "incomplete_tasks": {},
                        "finished_tasks": {},
                        "supervisor": {"last_reflect_dispatch_ts": "0"},
                    }
                )
                return

            out: Dict[str, Any] = {
                "watermark_ts": str(state.get("watermark_ts") or "0"),
                "active_tasks": {},
                "queued_tasks": {},
                "incomplete_tasks": {},
                "finished_tasks": {},
                "supervisor": {
                    "last_reflect_dispatch_ts": str(
                        ((state.get("supervisor") or {}).get("last_reflect_dispatch_ts") or "0")
                    )
                },
            }

            for bucket in ("active_tasks", "queued_tasks", "incomplete_tasks", "finished_tasks"):
                src = state.get(bucket) or {}
                if not isinstance(src, dict):
                    src = {}
                norm = {}
                for k, v in src.items():
                    if not isinstance(v, dict):
                        continue
                    nv = self.normalize_task(v, str(k), force_done=(bucket == "finished_tasks"), bucket_name=bucket)
                    legacy_text = str(v.get("mention_text") or "")
                    self.ensure_task_text_file(nv, bucket_name=bucket, legacy_text=legacy_text)
                    # AGENT-057: use mention_ts as the stable key. After thread
                    # continuation, thread_ts changes but mention_ts stays the
                    # same — keying by thread_ts would break all subsequent
                    # lookups that use mention_ts.
                    out_key = nv["mention_ts"]
                    if self.maintenance.is_maintenance_task(str(nv.get("task_type") or "")):
                        out_key = self.maintenance.TASK_ID
                        nv["mention_ts"] = self.maintenance.TASK_ID
                    norm[out_key] = nv
                out[bucket] = norm

            if out != state:
                self.save_state(out)

    def resolve_user_name(self, user_id: str) -> str:
        """Resolve a Slack user ID to display name.

        Lookup order: in-memory cache → user_directory.json → Slack users.info API.
        New lookups are persisted to user_directory.json for future sessions.
        Only attempts lookup for IDs starting with 'U' (real Slack user IDs).
        """
        if not user_id or not user_id.startswith("U"):
            return ""
        # 1. In-memory hot cache
        cached = self._user_name_cache.get(user_id)
        if cached is not None:
            return cached
        # 2. Persistent file cache
        directory = self._load_user_directory()
        user_entry = (directory.get("users") or {}).get(user_id)
        if isinstance(user_entry, dict):
            name = (user_entry.get("user_name") or "").strip()
            if name:
                self._user_name_cache[user_id] = name
                return name
        # 3. Slack API lookup
        if not self.slack_token:
            return ""
        try:
            resp = self.slack_api_get("users.info", {"user": user_id})
            profile = (resp.get("user") or {}).get("profile") or {}
            name = (profile.get("display_name") or profile.get("real_name") or "").strip()
        except Exception as exc:
            self.log_line(f"resolve_user_name_error user={user_id} error={type(exc).__name__}: {exc}")
            return ""
        if name:
            self._user_name_cache[user_id] = name
            # Persist to file
            directory.setdefault("users", {})[user_id] = {"user_name": name}
            self._save_user_directory(directory)
            # Seed a profile stub for newly discovered users
            self._create_profile_stub(user_id, name)
        return name

    def _create_profile_stub(self, user_id: str, user_name: str) -> None:
        """Create a minimal JSON profile stub for a new user if one doesn't exist."""
        if not user_id or not user_id.startswith("U"):
            return
        if user_id == self._agent_user_id:
            return
        json_path = self.cfg.user_profiles_dir / f"{user_id}.json"
        legacy_path = self.cfg.user_profiles_dir / f"{user_id}.md"
        if json_path.exists() or legacy_path.exists():
            return
        try:
            self.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            stub = {
                "user_id": user_id,
                "user_name": user_name,
                "display_name": "",
                "email": "",
                "github": "",
                "timezone": "",
                "biography": "",
                "personality": "",
                "communication_preferences": "",
                "working_patterns": "",
                "projects": [],
                "active_context": "",
                "milestones": [],
                "notes": [],
            }
            self.atomic_write_json(json_path, stub)
        except Exception as exc:
            self.log_line(f"profile_stub_error user={user_id} error={type(exc).__name__}: {exc}")

    def _migrate_v1_profile(self, user_id: str, user_name: str) -> None:
        """Migrate a v1 .md profile to v3 JSON + log.md if needed."""
        if not user_id or not user_id.startswith("U"):
            return
        legacy_path = self.cfg.user_profiles_dir / f"{user_id}.md"
        json_path = self.cfg.user_profiles_dir / f"{user_id}.json"
        if not legacy_path.exists() or json_path.exists():
            return
        try:
            content = legacy_path.read_text(encoding="utf-8")
            fm_name = user_name
            parts = content.split("---\n", 2)
            if len(parts) == 3 and parts[0] == "":
                for line in parts[1].splitlines():
                    if line.startswith("user_name:"):
                        fm_name = line.split(":", 1)[1].strip() or fm_name
                body = parts[2].strip()
            else:
                body = content.strip()
            self.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            stub = {
                "user_id": user_id,
                "user_name": fm_name,
                "display_name": "",
                "email": "",
                "github": "",
                "timezone": "",
                "biography": "",
                "personality": "",
                "communication_preferences": "",
                "working_patterns": "",
                "projects": [],
                "active_context": "",
                "milestones": [],
                "notes": [],
            }
            self.atomic_write_json(json_path, stub)
            if body:
                log_path = self.cfg.user_profiles_dir / f"{user_id}.log.md"
                with agent_file_lock(log_path):
                    if not log_path.exists():
                        log_path.write_text(body + "\n", encoding="utf-8")
            legacy_path.unlink()
            self.log_line(f"profile_migrated user={user_id} had_observations={bool(body)}")
        except Exception as exc:
            self.log_line(f"profile_migration_error user={user_id} error={type(exc).__name__}: {exc}")

    def _migrate_profiles(self) -> None:
        """Upgrade all profiles: v2→v3 (background→biography) + add contact fields."""
        profiles_dir = self.cfg.user_profiles_dir
        if not profiles_dir.is_dir():
            return
        for json_path in profiles_dir.glob("*.json"):
            try:
                data = json.loads(json_path.read_text(encoding="utf-8"))
                changed = False
                # v2→v3: background → biography
                if "background" in data:
                    bg = data.pop("background")
                    bio = data.get("biography")
                    if not isinstance(bio, str) or not bio.strip():
                        data["biography"] = bg
                    changed = True
                # Contact field backfill (fix non-string + normalize multiline)
                for field in ("email", "github", "timezone"):
                    if field not in data or not isinstance(data[field], str):
                        data[field] = ""
                        changed = True
                    elif "\n" in data[field]:
                        data[field] = data[field].splitlines()[0].strip()
                        changed = True
                # List field backfill (from v2→v3)
                for field in ("projects", "milestones", "notes"):
                    if field not in data:
                        data[field] = []
                        changed = True
                if changed:
                    self.atomic_write_json(json_path, data)
                    self.log_line(f"profile_migrated file={json_path.name}")
            except Exception as exc:
                self.log_line(f"profile_migration_error file={json_path.name} error={type(exc).__name__}: {exc}")

    def _backfill_user_profiles(self) -> None:
        """Backfill: migrate profiles, then v1 profiles, then create stubs for all known users.

        Also scans for orphan .log.md files (created by workers for users not yet
        in user_directory.json) and bootstraps profile stubs so maintenance can
        ingest them.
        """
        self._migrate_profiles()
        directory = self._load_user_directory()
        for uid, info in (directory.get("users") or {}).items():
            name = (info.get("user_name") or "").strip() if isinstance(info, dict) else ""
            self._migrate_v1_profile(uid, name)
            self._create_profile_stub(uid, name)

        # Scan for orphan observation logs without a corresponding profile JSON.
        # Workers can append to .log.md files for users the supervisor hasn't seen yet.
        if self.cfg.user_profiles_dir.is_dir():
            for log_path in self.cfg.user_profiles_dir.glob("U*.log.md"):
                uid = log_path.name.replace(".log.md", "")
                json_path = self.cfg.user_profiles_dir / f"{uid}.json"
                if json_path.exists():
                    continue
                # Resolve the user name (may hit Slack API) and create a stub
                name = self.resolve_user_name(uid)
                if not name:
                    name = uid  # fallback so the stub is at least created
                self._create_profile_stub(uid, name)

    # Profile field labels for prompt formatting
    _PROFILE_FIELD_LABELS = {
        "display_name": "Name",
        "email": "Email",
        "github": "GitHub",
        "timezone": "Timezone",
        "biography": "Biography",
        "personality": "Personality",
        "communication_preferences": "Communication preferences",
        "working_patterns": "Working patterns",
        "projects": "Projects",
        "active_context": "Current focus",
        "milestones": "Milestones",
        "notes": "Notes",
    }

    # List fields that need special formatting
    _PROFILE_LIST_FIELDS = {"projects", "milestones", "notes"}
    # List fields formatted as comma-joined inline (vs bullet sub-lists)
    _PROFILE_INLINE_LIST_FIELDS = {"projects"}
    # Scalar identity fields: single-line canonical values only
    _PROFILE_SCALAR_FIELDS = {"email", "github", "timezone"}

    def read_user_profile(self, user_id: str) -> str:
        """Read a user profile JSON, format non-empty fields into text, respecting char limit."""
        if not user_id or not user_id.startswith("U"):
            return ""
        profile_path = self.cfg.user_profiles_dir / f"{user_id}.json"
        if not profile_path.exists():
            return ""
        try:
            data = json.loads(profile_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.log_line(f"profile_read_error user={user_id} error={type(exc).__name__}: {exc}")
            return ""
        # Backward compat: unmigrated v2 profiles or non-string biography
        bio = data.get("biography")
        if (not isinstance(bio, str) or not bio.strip()) and isinstance(data.get("background"), str) and data["background"].strip():
            data["biography"] = data["background"]
        parts = []
        for key, label in self._PROFILE_FIELD_LABELS.items():
            if key in self._PROFILE_LIST_FIELDS:
                raw = data.get(key)
                if not isinstance(raw, list):
                    continue
                items = [str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]
                if not items:
                    continue
                if key in self._PROFILE_INLINE_LIST_FIELDS:
                    parts.append(f"- {label}: {', '.join(items)}")
                else:
                    parts.append(f"- {label}:")
                    for item in items:
                        parts.append(f"  - {item}")
            else:
                val = (data.get(key) or "").strip() if isinstance(data.get(key), str) else ""
                if val:
                    # Scalar identity fields: use first line only (no multiline prose)
                    if key in self._PROFILE_SCALAR_FIELDS:
                        val = val.splitlines()[0].strip()
                    if val:
                        parts.append(f"- {label}: {val}")
        body = "\n".join(parts)
        if not body:
            return ""
        limit = self.cfg.user_profile_char_limit
        if len(body) > limit:
            self.log_line(f"profile_truncated user={user_id} len={len(body)} limit={limit}")
            body = body[:limit] + "\n\n[Profile truncated — exceeds character budget]"
        return body

    def _slack_request(self, method: str, req: Request) -> Dict[str, Any]:
        """Execute a Slack API request with retry and backoff.

        Retries on transient transport errors (URLError, timeouts,
        connection resets), HTTP 429 (rate-limited), and 5xx server
        errors.  Permanent 4xx errors (except 429) are raised
        immediately.
        """
        max_retries = self.cfg.slack_api_max_retries
        delay = self.cfg.slack_api_retry_initial_sec
        timeout = self.cfg.slack_api_timeout_sec

        for attempt in range(1 + max_retries):
            try:
                with urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
            except HTTPError as exc:
                if exc.code == 429:
                    retry_after = float(exc.headers.get("Retry-After", delay))
                    sleep_sec = min(retry_after, self.cfg.slack_api_retry_max_sec)
                    self.log_line(
                        f"slack_api_rate_limited method={method} "
                        f"retry_after_sec={sleep_sec:.1f} attempt={attempt + 1}"
                    )
                    if attempt < max_retries:
                        time.sleep(sleep_sec)
                        continue
                elif 500 <= exc.code < 600:
                    self.log_line(
                        f"slack_api_retry method={method} attempt={attempt + 1} "
                        f"reason=http_{exc.code} sleep_sec={delay:.1f}"
                    )
                    if attempt < max_retries:
                        time.sleep(delay)
                        delay = min(delay * self.cfg.slack_api_retry_multiplier,
                                    self.cfg.slack_api_retry_max_sec)
                        continue
                # Permanent 4xx or retries exhausted
                raise
            except (URLError, OSError, TimeoutError, ConnectionError, http.client.IncompleteRead) as exc:
                self.log_line(
                    f"slack_api_retry method={method} attempt={attempt + 1} "
                    f"reason={type(exc).__name__} sleep_sec={delay:.1f}"
                )
                if attempt < max_retries:
                    time.sleep(delay)
                    delay = min(delay * self.cfg.slack_api_retry_multiplier,
                                self.cfg.slack_api_retry_max_sec)
                    continue
                self.log_line(
                    f"slack_api_failure method={method} "
                    f"terminal_error={type(exc).__name__} attempts={attempt + 1}"
                )
                raise
        # Should not reach here, but satisfy type checker
        raise RuntimeError(f"slack_api_failure method={method} retries_exhausted")

    def slack_api_get(self, method: str, params: Dict[str, str]) -> Dict[str, Any]:
        query = urlencode(params)
        url = f"https://slack.com/api/{method}?{query}"
        req = Request(url, headers={"Authorization": f"Bearer {self.slack_token}"})
        return self._slack_request(method, req)

    def slack_api_post(self, method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"https://slack.com/api/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.slack_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        return self._slack_request(method, req)

    def _mark_task_done_reaction(self, channel_id: str, mention_ts: str) -> None:
        """Post a done acknowledgement in the task's Slack thread."""
        if not self.slack_token or not channel_id or not mention_ts:
            return
        try:
            resp = self.slack_api_post("chat.postMessage", {
                "channel": channel_id,
                "thread_ts": mention_ts,
                "text": ":white_check_mark: Task marked done.",
            })
            self.log_line(
                f"done_ack_posted channel={channel_id} thread={mention_ts} "
                f"ok={resp.get('ok')}"
            )
        except Exception as exc:
            self.log_line(
                f"done_ack_failed channel={channel_id} ts={mention_ts} "
                f"error={type(exc).__name__}: {exc}"
            )

    def _post_completion_fallback(
        self, channel_id: str, thread_ts: str, message: str
    ) -> None:
        """Post a fallback notification to the Slack thread when the worker
        didn't deliver a reply itself.  Best-effort — failures are logged
        but never block reconciliation."""
        if not self.slack_token or not channel_id or not thread_ts:
            return
        try:
            self.slack_api_post("chat.postMessage", {
                "channel": channel_id,
                "thread_ts": thread_ts,
                "text": f":warning: {message}",
            })
            self.log_line(
                f"completion_fallback_posted channel={channel_id} "
                f"thread={thread_ts}"
            )
        except Exception as exc:
            self.log_line(
                f"completion_fallback_failed channel={channel_id} "
                f"thread={thread_ts} error={type(exc).__name__}: {exc}"
            )

    def _has_agent_delivery(self, thread_msgs: List[Dict[str, str]]) -> bool:
        """Check if thread has an agent message after the latest human message.

        Returns True if the agent posted a substantive reply after the most
        recent human message, indicating successful delivery.  Returns True
        (optimistic) if no slack_id is available or if there are no human
        messages (nothing to respond to).
        """
        slack_id = self.resolve_slack_id()
        if not slack_id:
            return True  # Can't determine — assume delivered

        last_human_ts = ""
        last_agent_ts = ""
        for m in thread_msgs:
            user = m.get("user") or ""
            ts = m.get("ts") or ""
            if user == slack_id:
                last_agent_ts = ts
            elif user:  # Real user, not a bot
                last_human_ts = ts

        if not last_human_ts:
            return True  # No human messages to respond to

        # Agent must have replied after the latest human message
        return bool(last_agent_ts) and ts_gt(last_agent_ts, last_human_ts)

    def poll_mentions_and_enqueue(self) -> bool:
        slack_id = self.resolve_slack_id()
        if not slack_id:
            self.log_line("mention_poll_skip reason=missing_slack_id")
            return True
        if not self.slack_token:
            self.log_line("mention_poll_error reason=missing_slack_token")
            return False

        mention = f"<@{slack_id}>"

        with self.state_lock():
            state = self.load_state()
            old_wm = str(state.get("watermark_ts", "0"))

        highest = old_wm
        gathered: List[Dict[str, Any]] = []

        for page in range(1, self.cfg.mention_max_pages + 1):
            try:
                resp = self.slack_api_get(
                    "search.messages",
                    {
                        "query": mention,
                        "count": str(self.cfg.mention_poll_limit),
                        "page": str(page),
                        "sort": "timestamp",
                        "sort_dir": "desc",
                        "highlight": "false",
                    },
                )
            except Exception:
                self.log_line(f"mention_poll_error page={page} error=curl_failed")
                return False

            if not resp.get("ok"):
                self.log_line(f"mention_poll_error page={page} error={resp.get('error', 'unknown_error')}")
                return False

            matches = ((resp.get("messages") or {}).get("matches") or [])
            if matches:
                page_top = str(matches[0].get("ts") or "")
                if page_top and ts_gt(page_top, highest):
                    highest = page_top

            reached_old = False
            for m in matches:
                ts = str(m.get("ts") or "")
                if not ts:
                    continue
                if ts_to_int(ts) <= ts_to_int(old_wm):
                    reached_old = True
                    continue

                permalink = str(m.get("permalink") or "")
                thread_ts = str(m.get("thread_ts") or "")
                if not thread_ts:
                    mt = THREAD_TS_RE.search(permalink)
                    thread_ts = mt.group(1) if mt else ts

                channel_id = ""
                ch = m.get("channel")
                if isinstance(ch, dict):
                    channel_id = str(ch.get("id") or "")
                if not channel_id and permalink:
                    mc = CHANNEL_ID_RE.search(permalink)
                    channel_id = mc.group(1) if mc else ""
                if not channel_id and self.cfg.default_channel_id:
                    channel_id = self.cfg.default_channel_id

                gathered.append(
                    {
                        "mention_ts": ts,
                        "thread_ts": thread_ts,
                        "channel_id": channel_id,
                        "mention_text": str(m.get("text") or ""),
                        "status": "queued",
                        "claimed_by": None,
                        "summary": "",
                        "task_description": self.summarize_task_description_from_text(str(m.get("text") or "")),
                        "report_path": f"reports/{thread_ts}.md",
                        "created_ts": ts,
                        "last_update_ts": ts,
                        "source": {
                            "user_id": str(m.get("user") or ""),
                            "user_name": str(m.get("username") or ""),
                            "time_iso": datetime.fromtimestamp(int(ts.split(".")[0]) if ts else 0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                            if ts
                            else "",
                        },
                        "task_type": "slack_mention",
                    }
                )

            pag = (resp.get("messages") or {}).get("pagination") or {}
            has_more = int(pag.get("page", 1)) < int(pag.get("page_count", 1))
            if reached_old or not has_more:
                break

        # Check for supervisor commands (e.g. "@agent !restart") before enqueueing.
        non_command: List[Dict[str, Any]] = []
        pending_loop_activations: Dict[str, int] = {}  # thread_ts → duration_sec
        pending_stop_threads: List[str] = []
        self._pending_dev_reviews: List[tuple] = []
        for mention_task in gathered:
            text = str(mention_task.get("mention_text") or "")
            thread_ts = str(mention_task.get("thread_ts") or mention_task.get("mention_ts") or "")
            if self._is_restart_command(text):
                self._restart_requested = True
                self.log_line(
                    f"hot_restart_requested source=slack mention_ts={mention_task.get('mention_ts')}"
                )
                continue
            if self._is_maintenance_command(text):
                self._maintenance_requested = True
                self.log_line(
                    f"maintenance_requested source=slack mention_ts={mention_task.get('mention_ts')}"
                )
                continue
            if self._is_stop_command(text):
                if thread_ts:
                    pending_stop_threads.append(thread_ts)
                self.log_line(
                    f"loop_stop_requested source=slack mention_ts={mention_task.get('mention_ts')} thread_ts={thread_ts}"
                )
                continue
            dev_result = self._parse_dev_review_command(text)
            if dev_result:
                dev_item, dev_extra = dev_result
                self._pending_dev_reviews.append((dev_item, dev_extra, mention_task))
                self.log_line(
                    f"dev_review_requested source=slack item={dev_item} "
                    f"mention_ts={mention_task.get('mention_ts')}"
                )
                continue
            # Detect inline !loop-Xh in mention text (strips it from text)
            loop_match = self.LOOP_RE.search(text)
            if loop_match:
                duration_sec = self._parse_loop_duration(loop_match.group(1))
                cleaned_text = re.sub(r"  +", " ", self.LOOP_RE.sub("", text)).strip()
                mention_task["mention_text"] = cleaned_text
                if thread_ts:
                    pending_loop_activations[thread_ts] = duration_sec
                # If the entire message was just the loop command, don't enqueue
                if self.LOOP_COMMAND_RE.search(text):
                    self.log_line(
                        f"loop_requested source=slack mention_ts={mention_task.get('mention_ts')} "
                        f"thread_ts={thread_ts} duration_sec={duration_sec}"
                    )
                    continue
            non_command.append(mention_task)
        gathered = non_command

        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for mention_task in gathered:
            task_id = str(mention_task.get("thread_ts") or mention_task.get("mention_ts") or "")
            if not task_id:
                continue
            mention_task["thread_ts"] = task_id
            grouped.setdefault(task_id, []).append(mention_task)

        with self.state_lock():
            state = self.load_state()
            enq_count = 0
            update_count = 0
            reopen_count = 0

            for task_id in sorted(grouped.keys(), key=ts_to_int):
                mentions = sorted(grouped[task_id], key=lambda row: ts_to_int(str(row.get("mention_ts") or "0")))
                if not mentions:
                    continue
                first = mentions[0]
                latest = mentions[-1]

                existing_bucket = ""
                existing_task: Optional[Dict[str, Any]] = None
                existing_key = task_id
                for bucket_name in ("active_tasks", "queued_tasks", "incomplete_tasks", "finished_tasks"):
                    candidate = (state.get(bucket_name) or {}).get(task_id)
                    if isinstance(candidate, dict):
                        existing_bucket = bucket_name
                        existing_task = candidate
                        break

                # AGENT-057: after thread continuation, the task key (mention_ts)
                # differs from thread_ts. If key-based lookup failed, scan for a
                # task whose thread_ts field matches the mention's thread.
                if existing_task is None:
                    for bucket_name in ("active_tasks", "queued_tasks", "incomplete_tasks", "finished_tasks"):
                        for k, v in (state.get(bucket_name) or {}).items():
                            if isinstance(v, dict) and str(v.get("thread_ts") or "") == task_id:
                                existing_bucket = bucket_name
                                existing_task = v
                                existing_key = k
                                break
                        if existing_task:
                            break

                defer_to_waiting_refresh = False
                if existing_task is None:
                    task = self.normalize_task(
                        {
                            "thread_ts": task_id,
                            "channel_id": str(latest.get("channel_id") or ""),
                            "status": "queued",
                            "claimed_by": None,
                            "summary": "",
                            "task_description": self.summarize_task_description_from_text(
                                str(first.get("mention_text") or "")
                            ),
                            "report_path": f"reports/{task_id}.md",
                            "created_ts": str(first.get("mention_ts") or now_ts()),
                            "last_update_ts": str(latest.get("mention_ts") or now_ts()),
                            "last_seen_mention_ts": str(latest.get("mention_ts") or now_ts()),
                            "source": latest.get("source") or {"user_id": "", "user_name": "", "time_iso": ""},
                            "task_type": "slack_mention",
                        },
                        task_id,
                        bucket_name="queued_tasks",
                    )
                    state.setdefault("queued_tasks", {})[task_id] = task
                    enq_count += 1
                    destination_bucket = "queued_tasks"
                else:
                    task = self.normalize_task(existing_task, existing_key, bucket_name=existing_bucket)
                    defer_to_waiting_refresh = (
                        existing_bucket == "incomplete_tasks"
                        and str(task.get("status") or "") == "waiting_human"
                        and str(task.get("task_type") or "slack_mention") == "slack_mention"
                    )
                    reactivate_non_slack_waiting = (
                        existing_bucket == "incomplete_tasks"
                        and str(task.get("status") or "") == "waiting_human"
                        and not defer_to_waiting_refresh
                    )
                    last_update_ts = str(latest.get("mention_ts") or "")
                    if (
                        (not defer_to_waiting_refresh)
                        and last_update_ts
                        and ts_gt(last_update_ts, str(task.get("last_update_ts") or "0"))
                    ):
                        task["last_update_ts"] = last_update_ts
                    if last_update_ts and ts_gt(last_update_ts, str(task.get("last_seen_mention_ts") or "0")):
                        task["last_seen_mention_ts"] = last_update_ts
                    if reactivate_non_slack_waiting:
                        task["status"] = "in_progress"
                    if str(latest.get("channel_id") or ""):
                        task["channel_id"] = str(latest.get("channel_id") or "")
                    if latest.get("source"):
                        task["source"] = latest["source"]
                    if not str(task.get("task_description") or "").strip():
                        task["task_description"] = self.derive_task_description(task)

                    # Follow-up messages reopen as regular tasks even if
                    # the original was a development dispatch.  Explicit
                    # !developer commands are handled earlier with continue.
                    if task.get("task_type") == "development":
                        task["task_type"] = "slack_mention"
                        task["task_description"] = ""

                    destination_bucket = existing_bucket or "incomplete_tasks"
                    if existing_bucket == "finished_tasks":
                        destination_bucket = "queued_tasks"
                        task["status"] = "queued"
                        task["claimed_by"] = None
                        reopen_count += 1

                    for bucket_name in ("active_tasks", "queued_tasks", "incomplete_tasks", "finished_tasks"):
                        state.setdefault(bucket_name, {}).pop(existing_key, None)
                    state.setdefault(destination_bucket, {})[existing_key] = task
                    update_count += 1

                self.ensure_task_text_file(task, bucket_name=destination_bucket)
                if not defer_to_waiting_refresh:
                    self.append_mentions_to_task_text(str(task.get("mention_text_file") or ""), mentions)
                if not str(task.get("task_description") or "").strip():
                    task["task_description"] = self.derive_task_description(task)

            if enq_count > 0:
                self.log_line(f"mention_poll_enqueued count={enq_count}")
            if update_count > 0:
                self.log_line(f"mention_poll_updated count={update_count}")
            if reopen_count > 0:
                self.log_line(f"mention_poll_reopened count={reopen_count}")

            # Apply !stop commands — clear loop fields on matching tasks
            for stop_ts in pending_stop_threads:
                found_stop = False
                for bucket_name in ("active_tasks", "queued_tasks", "incomplete_tasks", "finished_tasks"):
                    t = (state.get(bucket_name) or {}).get(stop_ts)
                    if isinstance(t, dict) and t.get("loop_mode"):
                        iteration = t.get("loop_iteration", 0)
                        t.pop("loop_worker_status", None)
                        t.pop("loop_mode", None)
                        t.pop("loop_deadline", None)
                        t.pop("loop_next_dispatch_after", None)
                        t.pop("loop_iteration", None)
                        # Human explicitly stopped the loop — park as
                        # waiting_human so it won't auto-redispatch.
                        t["status"] = "waiting_human"
                        if bucket_name != "incomplete_tasks":
                            state.setdefault(bucket_name, {}).pop(stop_ts, None)
                            state.setdefault("incomplete_tasks", {})[stop_ts] = t
                        self.log_line(
                            f"loop_stopped thread_ts={stop_ts} iteration={iteration}"
                        )
                        found_stop = True
                        break
                # Kill the worker process if it's actively running this task.
                # The !stop state change above is already applied; the kill
                # ensures the slot frees up so the queue isn't deadlocked.
                if found_stop and hasattr(self, '_parallel_slots') and self._parallel_slots:
                    for slot in self._parallel_slots:
                        if slot.is_busy and slot.task_key == stop_ts:
                            slot.kill_worker("stop_command")
                            self.log_line(f"stop_kill slot={slot.slot_id} task={stop_ts}")
                            break
                if not found_stop:
                    self.log_line(f"loop_stop_no_match thread_ts={stop_ts}")

            # Apply !loop activations — set loop fields on matching tasks
            now_val = now_ts()
            for loop_ts, dur_sec in pending_loop_activations.items():
                for bucket_name in ("active_tasks", "queued_tasks", "incomplete_tasks", "finished_tasks"):
                    t = (state.get(bucket_name) or {}).get(loop_ts)
                    if isinstance(t, dict):
                        t["loop_mode"] = True
                        t["loop_deadline"] = str(float(now_val) + dur_sec)
                        t["loop_iteration"] = 0
                        t.pop("loop_worker_status", None)
                        # If task is finished or waiting_human, reactivate for dispatch
                        if bucket_name == "finished_tasks":
                            t["status"] = "queued"
                            t["claimed_by"] = None
                            state.setdefault(bucket_name, {}).pop(loop_ts, None)
                            state.setdefault("queued_tasks", {})[loop_ts] = t
                            bucket_name = "queued_tasks"
                        elif (
                            bucket_name == "incomplete_tasks"
                            and str(t.get("status") or "") == "waiting_human"
                        ):
                            t["status"] = "in_progress"
                        self.log_line(
                            f"loop_activated thread_ts={loop_ts} duration_sec={dur_sec} "
                            f"deadline={t['loop_deadline']} bucket={bucket_name}"
                        )
                        break

            # Process !developer commands — enqueue developer review tasks
            for dev_item_id, dev_extra, dev_mention in self._pending_dev_reviews:
                self._enqueue_developer_review(dev_item_id, state, dev_mention, extra_text=dev_extra)

            if ts_gt(highest, str(state.get("watermark_ts") or "0")):
                state["watermark_ts"] = highest

            self.save_state(state)

        return True

    # Maintenance methods (load_reflect_prompt_text, enqueue_reflect_task_if_due)
    # have been moved to maintenance.py — see self.maintenance (MaintenanceManager).

    DEV_SESSION_ID_FILE = Path(".agent/runtime/dev_session_id")
    DEV_SESSION_MAP_FILE = Path(".agent/runtime/dev_session_map.json")
    SERIAL_DISPATCH_TYPES = frozenset({"development"})

    def _write_dev_session_id(self, session_id: str, item_id: str = "") -> None:
        """Write the development task's Claude session ID for dashboard polling."""
        self.DEV_SESSION_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.DEV_SESSION_ID_FILE.write_text(session_id, encoding="utf-8")
        self.log_line(f"dev_session_id={session_id}")
        # Persist per-item session mapping for dashboard history.
        if item_id:
            try:
                existing = json.loads(self.DEV_SESSION_MAP_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            existing[item_id] = session_id
            self.DEV_SESSION_MAP_FILE.write_text(
                json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8"
            )
    DEV_REVIEW_PROMPT_FILE = Path("src/prompts/developer_backlog.md")

    def _is_serial_dispatch_type(self, task_type: str) -> bool:
        """True for task types that must run in the main worktree (serial)."""
        return (
            self.maintenance.is_maintenance_task(task_type)
            or task_type in self.SERIAL_DISPATCH_TYPES
        )

    def _enqueue_developer_review(
        self, item_id: str, state: dict, mention_task: dict, extra_text: str = ""
    ) -> None:
        """Enqueue a developer review task for a specific roadmap item.

        Called inside the state lock during poll_slack_mentions().
        Uses the real Slack mention_ts/thread_ts so the developer can
        post updates in the same thread as the !developer command.
        """
        channel_id = str(mention_task.get("channel_id") or self.cfg.default_channel_id)
        thread_ts = str(mention_task.get("thread_ts") or mention_task.get("mention_ts") or "")
        if not self.cfg.dev_review_cmd:
            self.log_line(f"dev_review_skip item={item_id} reason=command_disabled")
            if self.slack_token and channel_id and thread_ts:
                try:
                    self.slack_api_post("chat.postMessage", {
                        "channel": channel_id,
                        "thread_ts": thread_ts,
                        "text": (
                            "Developer review is disabled in this install. "
                            "Install Claude Code or set `DEV_REVIEW_CMD` to enable `!developer`."
                        ),
                    })
                except Exception as exc:
                    self.log_line(
                        f"dev_review_disabled_notice_failed item={item_id} "
                        f"error={type(exc).__name__}: {exc}"
                    )
            return

        # Use the real Slack ts as task ID so the thread is tracked.
        task_id = thread_ts
        if not task_id:
            self.log_line(f"dev_review_skip item={item_id} reason=no_thread_ts")
            return

        # Dedup: skip if already in any bucket (including finished)
        for bucket in ("queued_tasks", "active_tasks", "incomplete_tasks", "finished_tasks"):
            if task_id in (state.get(bucket) or {}):
                self.log_line(f"dev_review_skip item={item_id} reason=already_in_flight")
                return

        # Extract review round params from extra_text before building prompt
        review_rounds_args = self._extract_review_rounds(extra_text)
        extra_text = self._REVIEW_ROUNDS_RE.sub("", extra_text).strip()

        # Load the prompt template
        prompt_path = self.DEV_REVIEW_PROMPT_FILE
        try:
            prompt_text = prompt_path.read_text(encoding="utf-8")
        except OSError:
            prompt_text = f"/iterative-review {{REVIEW_ROUNDS_ARGS}}\n\nDevelopment task: implement backlog item {item_id}. Read the BACKLOG entry in docs/dev/BACKLOG.md and linked plan/issue files."
        prompt_text = prompt_text.replace("{ITEM_ID}", item_id)
        prompt_text = prompt_text.replace("{REVIEW_ROUNDS_ARGS}", review_rounds_args)
        if extra_text:
            prompt_text = prompt_text.replace(
                "{ADDITIONAL_CONTEXT}",
                f"\nIMPORTANT — Additional instructions from human (take priority over defaults):\n{extra_text}\n",
            )
        else:
            prompt_text = prompt_text.replace("{ADDITIONAL_CONTEXT}", "")

        dispatch_ts = now_ts()
        thread_ts = str(mention_task.get("thread_ts") or task_id)
        prompt_text = prompt_text.replace("{THREAD_TS}", thread_ts)
        prompt_text = prompt_text.replace("{CHANNEL_ID}", channel_id)

        task_data = self._empty_task_data(
            task_id=task_id, thread_ts=thread_ts, channel_id=channel_id,
        )
        task_data["messages"].append({
            "ts": dispatch_ts,
            "user_id": "system",
            "role": "human",
            "text": prompt_text.strip(),
        })
        mention_text_file = str(self.task_text_path(task_id, "queued_tasks"))
        self.write_task_json(mention_text_file, task_data)

        # Clear from all buckets before inserting.
        for bucket in ("queued_tasks", "active_tasks", "incomplete_tasks", "finished_tasks"):
            state.setdefault(bucket, {}).pop(task_id, None)

        state.setdefault("queued_tasks", {})[task_id] = {
            "mention_ts": task_id,
            "thread_ts": thread_ts,
            "channel_id": channel_id,
            "mention_text_file": mention_text_file,
            "status": "queued",
            "claimed_by": None,
            "summary": "",
            "task_description": f"Development: {item_id}",
            "created_ts": dispatch_ts,
            "last_update_ts": dispatch_ts,
            "source": mention_task.get("source") or {
                "user_id": "system",
                "user_name": "dashboard",
                "time_iso": timestamp_utc(),
            },
            "task_type": "development",
            "mention_text": prompt_text.strip(),
        }
        self.log_line(f"dev_review_enqueued item={item_id} task_id={task_id} thread_ts={thread_ts}")

    def prune_finished_tasks(self) -> None:
        cutoff_ts = f"{int(time.time()) - self.cfg.finished_ttl_days * 86400}.000000"
        cutoff_val = ts_to_int(cutoff_ts)
        with self.state_lock():
            state = self.load_state()
            finished = state.get("finished_tasks") or {}
            kept = {}
            for k, v in finished.items():
                ts = str((v or {}).get("last_update_ts") or (v or {}).get("created_ts") or "0")
                if ts_to_int(ts) >= cutoff_val:
                    kept[k] = v
                else:
                    self.remove_task_text_file(str((v or {}).get("mention_text_file") or ""))
            if len(kept) < len(finished):
                state["finished_tasks"] = kept
                self.save_state(state)

    def prune_stale_waiting_human_tasks(self) -> None:
        if self.cfg.max_incomplete_retention <= 0:
            return

        cutoff_ts = f"{int(time.time()) - self.cfg.max_incomplete_retention}.000000"
        cutoff_val = ts_to_int(cutoff_ts)
        with self.state_lock():
            state = self.load_state()
            incomplete = state.get("incomplete_tasks") or {}
            kept = {}
            pruned_count = 0
            for key, task in incomplete.items():
                if not isinstance(task, dict):
                    continue
                if str(task.get("status") or "") != "waiting_human":
                    kept[key] = task
                    continue
                latest_seen = max(
                    ts_to_int(str(task.get("created_ts") or "0")),
                    ts_to_int(str(task.get("last_update_ts") or "0")),
                    ts_to_int(str(task.get("last_seen_mention_ts") or "0")),
                    ts_to_int(str(task.get("last_human_reply_ts") or "0")),
                )
                if latest_seen >= cutoff_val:
                    kept[key] = task
                else:
                    pruned_count += 1
                    self.remove_task_text_file(str(task.get("mention_text_file") or ""))
            if pruned_count > 0:
                state["incomplete_tasks"] = kept
                self.save_state(state)

        if pruned_count:
            self.log_line(
                f"waiting_human_pruned count={pruned_count} retention_sec={self.cfg.max_incomplete_retention}"
            )

    def _last_agent_message_ts(self, mention_text_file: str, slack_id: str) -> str:
        """Return the ts of the last agent message in the task JSON.

        Used as the cutoff for detecting new human replies — more reliable
        than ``last_update_ts`` which is set by the reconciler and can
        post-date human replies that arrived during worker execution.
        """
        if not mention_text_file:
            return ""
        data = self.read_task_json(mention_text_file)
        if not data:
            return ""
        best_ts = ""
        best_val = 0
        for m in data.get("messages", []):
            if m.get("role") == "agent" or m.get("user_id") == slack_id:
                ts = str(m.get("ts") or "")
                ts_val = ts_to_int(ts)
                if ts_val > best_val:
                    best_val = ts_val
                    best_ts = ts
        return best_ts

    def _fetch_thread_messages(self, channel_id: str, thread_ts: str) -> List[Dict[str, str]]:
        """Fetch all messages in a Slack thread via conversations.replies.

        Returns a deduplicated, timestamp-sorted list of raw message dicts,
        or an empty list on any failure.
        """
        if not self.slack_token:
            return []
        cursor = ""
        has_more = True
        thread_messages: List[Dict[str, str]] = []

        while has_more:
            params = {
                "channel": channel_id,
                "ts": thread_ts,
                "limit": str(self.cfg.waiting_human_reply_limit),
            }
            if cursor:
                params["cursor"] = cursor
            try:
                resp = self.slack_api_get("conversations.replies", params)
            except Exception as exc:
                self.log_line(f"thread_fetch_error channel={channel_id} thread={thread_ts} error={type(exc).__name__}: {exc}")
                return []

            if not resp.get("ok"):
                self.log_line(
                    f"thread_fetch_error channel={channel_id} thread={thread_ts} error={resp.get('error', 'unknown_error')}"
                )
                return []

            for m in resp.get("messages") or []:
                ts = str(m.get("ts") or "")
                subtype = str(m.get("subtype") or "")
                if not ts:
                    continue
                if subtype not in {"", "thread_broadcast", "bot_message"}:
                    continue
                # Build a files summary for attachment-only messages so they
                # don't degrade to "[empty message]" in thread snapshots.
                files_summary = ""
                raw_files = m.get("files") or []
                if raw_files and isinstance(raw_files, list):
                    descs = []
                    for f in raw_files[:5]:
                        name = f.get("name") or f.get("title") or "file"
                        mimetype = f.get("mimetype") or ""
                        desc = name
                        if mimetype:
                            desc += f" ({mimetype})"
                        descs.append(desc)
                    files_summary = "[attached: " + ", ".join(descs) + "]"

                thread_messages.append(
                    {
                        "ts": ts,
                        "user": str(m.get("user") or ""),
                        "bot_id": str(m.get("bot_id") or ""),
                        "username": str(m.get("username") or ((m.get("bot_profile") or {}).get("name") or "")),
                        "text": str(m.get("text") or ""),
                        "files_summary": files_summary,
                    }
                )

            has_more = bool(resp.get("has_more"))
            cursor = str((resp.get("response_metadata") or {}).get("next_cursor") or "")
            if has_more and not cursor:
                has_more = False

        thread_messages = sorted(thread_messages, key=lambda x: ts_to_int(x["ts"]))
        uniq: Dict[str, Dict[str, str]] = {}
        for m in thread_messages:
            uniq[m["ts"]] = m
        return [uniq[k] for k in sorted(uniq.keys(), key=ts_to_int)]

    def _store_thread_snapshot(self, mention_text_file: str, thread_messages: List[Dict[str, str]]) -> None:
        """Replace context_snapshot messages in a task JSON with a fresh thread snapshot."""
        if not thread_messages:
            return
        slack_id = self.resolve_slack_id()
        if not slack_id:
            return

        data = self.read_task_json(mention_text_file)
        # Remove old context_snapshot messages, then add fresh ones
        data["messages"] = [
            m for m in data.get("messages", [])
            if m.get("source") != "context_snapshot"
        ]
        # Build uid->user_name map from existing messages, then
        # fall back to Slack API for unknown users
        uid_name = {
            m["user_id"]: m["user_name"]
            for m in data["messages"]
            if m.get("user_name") and m.get("user_id")
        }
        ctx_msgs = format_waiting_human_context_messages(thread_messages, slack_id)
        for cm in ctx_msgs:
            if not cm.get("user_name"):
                uid = cm.get("user_id", "")
                name = uid_name.get(uid) or self.resolve_user_name(uid)
                if name:
                    cm["user_name"] = name
                    uid_name[uid] = name
        data["messages"].extend(ctx_msgs)
        self.write_task_json(mention_text_file, data)

    # -- AGENT-057: Thread continuation ------------------------------------------

    @staticmethod
    def _slack_thread_link(channel_id: str, thread_ts: str) -> str:
        """Build a Slack archive link for a thread."""
        ts_no_dot = thread_ts.replace(".", "")
        return f"https://slack.com/archives/{channel_id}/p{ts_no_dot}"

    def _continue_in_new_thread(
        self,
        task: Dict[str, Any],
        reason: str,
        context_message: str = "",
    ) -> bool:
        """Start a new Slack thread for a task, preserving context.

        Crash-safe ordering:
        1. Post new top-level message → get new thread_ts
        2. Durably save task with new thread_ts (under state lock)
        3. Post link-forward in old thread (best-effort)

        Returns True if continuation succeeded, False on failure.
        """
        channel_id = str(task.get("channel_id") or "")
        old_thread_ts = str(task.get("thread_ts") or "")
        mention_ts = str(task.get("mention_ts") or "")
        if not channel_id or not old_thread_ts or not self.slack_token:
            return False

        old_link = self._slack_thread_link(channel_id, old_thread_ts)
        task_desc = str(task.get("task_description") or task.get("summary") or "")

        # Build new thread message
        parts = [f":thread: Continuing from <{old_link}|previous thread>"]
        if task_desc:
            parts.append(task_desc)
        if reason == "parked" and context_message:
            parts.append(f"Human replied: {context_message[:500]}")
        elif reason == "thread_length":
            parts.append("Thread exceeded message limit — starting fresh.")
        new_thread_text = "\n\n".join(parts)

        # Step 1: Post new top-level message
        try:
            resp = self.slack_api_post("chat.postMessage", {
                "channel": channel_id,
                "text": new_thread_text,
                "unfurl_links": False,
            })
            new_thread_ts = str(resp.get("ts") or "")
            if not new_thread_ts or not re.match(r"^[0-9]+\.[0-9]+$", new_thread_ts):
                self.log_line(
                    f"thread_continuation_failed reason=no_new_ts task={mention_ts}"
                )
                return False
        except Exception as exc:
            self.log_line(
                f"thread_continuation_failed reason=post_error task={mention_ts} "
                f"error={type(exc).__name__}: {exc}"
            )
            return False

        # Preserve bounded tail of old context snapshot before overwriting
        mention_text_file = str(task.get("mention_text_file") or "")
        prior_context_msgs: List[Dict[str, Any]] = []
        if mention_text_file:
            data = self.read_task_json(mention_text_file)
            old_snapshots = [
                m for m in data.get("messages", [])
                if m.get("source") == "context_snapshot"
            ]
            max_carry = self.cfg.thread_context_max_messages // 2
            tail = old_snapshots[-max_carry:] if len(old_snapshots) > max_carry else old_snapshots
            for m in tail:
                carried = dict(m)
                carried["source"] = "prior_thread_context"
                prior_context_msgs.append(carried)

        # Step 2: Durably save task state
        prior_threads = list(task.get("prior_threads") or [])
        prior_threads.append(old_thread_ts)
        task["prior_threads"] = prior_threads
        task["thread_ts"] = new_thread_ts
        task.pop("continuation_pending", None)
        task.pop("waiting_reason", None)
        task["consecutive_exit_failures"] = 0
        if reason == "parked":
            for sf in self._SESSION_FIELDS:
                task.pop(sf, None)
            task.pop("session_resume_count", None)

        # Update task JSON: keep originals, add prior context, clear old snapshots
        if mention_text_file:
            data = self.read_task_json(mention_text_file)
            data["messages"] = [
                m for m in data.get("messages", [])
                if m.get("source") not in ("context_snapshot", "prior_thread_context")
            ]
            data["messages"].extend(prior_context_msgs)
            data["thread_ts"] = new_thread_ts
            self.write_task_json(mention_text_file, data)

        self.log_line(
            f"thread_continuation_done task={mention_ts} reason={reason} "
            f"old_thread={old_thread_ts} new_thread={new_thread_ts} "
            f"prior_context_msgs={len(prior_context_msgs)}"
        )

        # Step 3: Post link-forward in old thread (best-effort)
        new_link = self._slack_thread_link(channel_id, new_thread_ts)
        try:
            self.slack_api_post("chat.postMessage", {
                "channel": channel_id,
                "thread_ts": old_thread_ts,
                "text": f":arrow_right: Continuing in a <{new_link}|new thread>.",
                "unfurl_links": False,
            })
        except Exception as exc:
            self.log_line(
                f"thread_continuation_linkforward_failed task={mention_ts} "
                f"error={type(exc).__name__}: {exc}"
            )

        return True

    def refresh_waiting_human_tasks(self) -> None:
        slack_id = self.resolve_slack_id()
        if not slack_id or not self.slack_token:
            if not self.slack_token:
                self.log_line("waiting_human_recheck_skip reason=missing_slack_token")
            return

        with self.state_lock():
            state = self.load_state()
            waiting = []
            for key, task in (state.get("incomplete_tasks") or {}).items():
                if not isinstance(task, dict):
                    continue
                if str(task.get("status") or "") != "waiting_human":
                    continue
                task_type = str(task.get("task_type") or "slack_mention")
                # Re-check maintenance threads the same way as mention threads:
                # maintenance tasks often wait on plain in-thread replies (without a new @mention).
                if task_type not in {"slack_mention"} and not self.maintenance.is_maintenance_task(task_type):
                    continue
                channel_id = str(task.get("channel_id") or "")
                thread_ts = str(task.get("thread_ts") or "")
                if not channel_id or not re.match(r"^[0-9]+\.[0-9]+$", thread_ts):
                    continue
                # Use the last agent message ts from the task JSON as the
                # cutoff, not last_update_ts.  last_update_ts is set by the
                # reconciler (via now_ts()) and can be newer than human
                # replies that arrived during or just after worker execution,
                # causing those replies to be permanently skipped.
                mention_text_file = str(task.get("mention_text_file") or "")
                since_ts = self._last_agent_message_ts(mention_text_file, slack_id)
                if not since_ts:
                    since_ts = str(task.get("last_update_ts") or task.get("created_ts") or "0")
                # AGENT-057: also track the last prior thread for watching
                prior_threads = list(task.get("prior_threads") or [])
                last_prior_thread = prior_threads[-1] if prior_threads else ""
                waiting.append(
                    {
                        "key": key,
                        "channel_id": channel_id,
                        "thread_ts": thread_ts,
                        "since_ts": since_ts,
                        "waiting_reason": str(task.get("waiting_reason") or ""),
                        "last_prior_thread": last_prior_thread,
                        "prior_thread_fetch_failures": int(task.get("prior_thread_fetch_failures") or 0),
                    }
                )

        if not waiting:
            return

        updates = []
        prior_failure_updates: List[Tuple[str, int]] = []
        for row in waiting:
            key = row["key"]
            channel_id = row["channel_id"]
            thread_ts = row["thread_ts"]
            since_ts = row["since_ts"]
            since_val = ts_to_int(since_ts)

            thread_messages = self._fetch_thread_messages(channel_id, thread_ts)

            latest_human_ts = ""
            latest_human_text = ""
            for m in (thread_messages or []):
                if ts_to_int(m["ts"]) <= since_val:
                    continue
                user = m.get("user") or ""
                if user and user != slack_id:
                    latest_human_ts = m["ts"]
                    latest_human_text = str(m.get("text") or "")

            # AGENT-057: also check last prior thread for human replies
            reply_from_prior_thread = False
            prior_thread_human_msgs: List[Dict[str, str]] = []
            if not latest_human_ts and row["last_prior_thread"]:
                prior_thread_ts = row["last_prior_thread"]
                prior_failures = row.get("prior_thread_fetch_failures", 0)
                if prior_failures >= 5:
                    self.log_line(
                        f"prior_thread_watch_abandoned key={key} "
                        f"thread={prior_thread_ts} failures={prior_failures}"
                    )
                elif re.match(r"^[0-9]+\.[0-9]+$", prior_thread_ts):
                    prior_msgs = self._fetch_thread_messages(channel_id, prior_thread_ts)
                    if not prior_msgs:
                        # Fetch failed (likely IncompleteRead) — increment failure counter
                        prior_failure_updates.append((key, prior_failures + 1))
                    else:
                        for m in prior_msgs:
                            if ts_to_int(m["ts"]) <= since_val:
                                continue
                            user = m.get("user") or ""
                            if user and user != slack_id:
                                latest_human_ts = m["ts"]
                                latest_human_text = str(m.get("text") or "")
                                reply_from_prior_thread = True
                                prior_thread_human_msgs.append(m)

            if not latest_human_ts:
                continue

            updates.append(
                {
                    "key": key,
                    "last_human_reply_ts": latest_human_ts,
                    "last_human_reply_text": latest_human_text,
                    "thread_messages": thread_messages or [],
                    "waiting_reason": row["waiting_reason"],
                    "reply_from_prior_thread": reply_from_prior_thread,
                    "prior_thread_human_msgs": prior_thread_human_msgs,
                }
            )

        # Persist prior thread fetch failure counts
        if prior_failure_updates:
            with self.state_lock():
                state = self.load_state()
                for fail_key, fail_count in prior_failure_updates:
                    task = (state.get("incomplete_tasks") or {}).get(fail_key)
                    if isinstance(task, dict):
                        task["prior_thread_fetch_failures"] = fail_count
                self.save_state(state)

        if not updates:
            return

        now_val = now_ts()
        # AGENT-057: process continuations OUTSIDE state lock (they make Slack API calls).
        # Pattern: load task under lock → release → API call → reacquire → save.
        continuation_keys = set()
        for u in updates:
            if u["waiting_reason"] == "consecutive_exit_failures":
                # Load task snapshot under lock
                with self.state_lock():
                    state = self.load_state()
                    task = (state.get("incomplete_tasks") or {}).get(u["key"])
                    if not isinstance(task, dict):
                        continue
                    mention_text_file = self.ensure_task_text_file(
                        task,
                        bucket_name="incomplete_tasks",
                        legacy_text=str(task.get("mention_text") or ""),
                    )
                    task_copy = dict(task)

                # Store thread snapshot BEFORE continuation so human replies
                # are captured in context_snapshot → carried as prior_thread_context
                if u["thread_messages"] and mention_text_file:
                    self._store_thread_snapshot(mention_text_file, u["thread_messages"])

                # Execute continuation outside lock (Slack API calls)
                if self._continue_in_new_thread(
                    task_copy, "parked", context_message=u["last_human_reply_text"]
                ):
                    # Commit state under lock
                    with self.state_lock():
                        state = self.load_state()
                        task = (state.get("incomplete_tasks") or {}).get(u["key"])
                        if not isinstance(task, dict):
                            continue
                        # Apply continuation results
                        task["thread_ts"] = task_copy["thread_ts"]
                        task["prior_threads"] = task_copy.get("prior_threads", [])
                        task.pop("continuation_pending", None)
                        task.pop("waiting_reason", None)
                        task["consecutive_exit_failures"] = 0
                        for sf in self._SESSION_FIELDS:
                            task.pop(sf, None)
                        task.pop("session_resume_count", None)
                        task["status"] = "in_progress"
                        task["last_update_ts"] = now_val
                        task["last_human_reply_ts"] = u["last_human_reply_ts"]
                        if not str(task.get("task_description") or "").strip():
                            task["task_description"] = self.derive_task_description(task)
                        task.pop("mention_text", None)
                        self.save_state(state)
                    continuation_keys.add(u["key"])

        with self.state_lock():
            state = self.load_state()
            reactivated = 0
            for u in updates:
                key = u["key"]
                if key in continuation_keys:
                    reactivated += 1
                    continue
                task = (state.get("incomplete_tasks") or {}).get(key)
                if not isinstance(task, dict):
                    continue
                mention_text_file = self.ensure_task_text_file(
                    task,
                    bucket_name="incomplete_tasks",
                    legacy_text=str(task.get("mention_text") or ""),
                )
                self._store_thread_snapshot(mention_text_file, u["thread_messages"])
                # AGENT-057: if reply came from prior thread, inject all messages
                if u.get("reply_from_prior_thread") and u.get("prior_thread_human_msgs"):
                    data = self.read_task_json(mention_text_file)
                    for pm in u["prior_thread_human_msgs"]:
                        data["messages"].append({
                            "ts": str(pm.get("ts") or ""),
                            "user_id": str(pm.get("user") or ""),
                            "user_name": str(pm.get("username") or "human (from prior thread)"),
                            "text": str(pm.get("text") or ""),
                            "source": "prior_thread_context",
                        })
                    self.write_task_json(mention_text_file, data)
                task["status"] = "in_progress"
                task["last_update_ts"] = now_val
                task["last_human_reply_ts"] = u["last_human_reply_ts"]
                task.pop("waiting_reason", None)
                if not str(task.get("task_description") or "").strip():
                    task["task_description"] = self.derive_task_description(task)
                task.pop("mention_text", None)
                reactivated += 1
            self.save_state(state)

        if reactivated:
            self.log_line(
                f"waiting_human_reactivated count={reactivated} "
                f"continuations={len(continuation_keys)}"
            )

    def _check_and_execute_continuation(self, dispatch_task_file=None) -> None:
        """AGENT-057: If the claimed task has continuation_pending, execute it now.

        Called just before refresh_dispatch_thread_context(). Reads the dispatch
        JSON, checks for continuation_pending, executes _continue_in_new_thread(),
        and updates both state.json and the dispatch JSON with the new thread_ts.
        """
        target_file = dispatch_task_file or self.cfg.dispatch_task_file
        dispatch = self.read_json(target_file, {})
        if not dispatch.get("continuation_pending"):
            return

        mention_ts = str(dispatch.get("mention_ts") or "")
        # Load task under lock, release before API calls
        with self.state_lock():
            state = self.load_state()
            task = (state.get("active_tasks") or {}).get(mention_ts)
            if not isinstance(task, dict) or not task.get("continuation_pending"):
                return
            task_copy = dict(task)

        # Execute continuation outside lock (Slack API calls)
        if not self._continue_in_new_thread(task_copy, "thread_length"):
            return

        # Commit state under lock
        with self.state_lock():
            state = self.load_state()
            task = (state.get("active_tasks") or {}).get(mention_ts)
            if not isinstance(task, dict):
                return
            task["thread_ts"] = task_copy["thread_ts"]
            task["prior_threads"] = task_copy.get("prior_threads", [])
            task.pop("continuation_pending", None)
            self.save_state(state)

        # Update dispatch JSON with new thread_ts
        dispatch["thread_ts"] = task_copy["thread_ts"]
        dispatch["prior_threads"] = task_copy.get("prior_threads", [])
        dispatch.pop("continuation_pending", None)
        self.atomic_write_json(target_file, dispatch)

    def refresh_dispatch_thread_context(self) -> None:
        """Fetch fresh Slack thread and update dispatch context before rendering.

        Called between claim_task_for_worker() and render_runtime_prompt() to
        ensure the worker always sees the latest thread messages — including
        replies that arrived after the task was last reconciled.
        """
        dispatch = self.read_json(self.cfg.dispatch_task_file, {})
        channel_id = str(dispatch.get("channel_id") or "")
        thread_ts = str(dispatch.get("thread_ts") or "")
        mention_text_file = str(dispatch.get("mention_text_file") or "")
        if not channel_id or not mention_text_file:
            return
        if not re.match(r"^[0-9]+\.[0-9]+$", thread_ts):
            return
        if not self.slack_token:
            return

        thread_msgs = self._fetch_thread_messages(channel_id, thread_ts)
        if not thread_msgs:
            return

        self._store_thread_snapshot(mention_text_file, thread_msgs)
        dispatch["mention_text"] = self.read_task_text_for_prompt(mention_text_file)
        self.atomic_write_json(self.cfg.dispatch_task_file, dispatch)

    @staticmethod
    def _by_oldest(obj: Dict[str, Dict[str, Any]]) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Return the (key, task) pair with the oldest timestamp, or None."""
        items = list(obj.items())
        if not items:
            return None

        def key_fn(item: Tuple[str, Dict[str, Any]]) -> int:
            v = item[1] if isinstance(item[1], dict) else {}
            ts = str(v.get("last_update_ts") or v.get("created_ts") or "0")
            return ts_to_int(ts)

        return sorted(items, key=key_fn)[0]

    def select_and_claim(
        self, worker_slot: int = 0, dispatch_task_file: Path | None = None
    ) -> Optional[Tuple[str, str]]:
        """Atomically select the best unclaimed task and claim it.

        Selection and claim happen under a single state_lock acquisition,
        preventing race conditions under concurrent dispatch.

        Returns (task_key, task_type) or None if no task is available.
        Also sets self.selected_bucket, self.selected_key, self._active_task_type
        for backward compatibility with the serial path.
        """
        now_val = now_ts()
        target_file = dispatch_task_file or self.cfg.dispatch_task_file

        with self.state_lock():
            state = self.load_state()

            # --- Selection: same priority as the old select_next_task ---
            selected_bucket = ""
            selected_key = ""

            # Active tasks: only select unclaimed ones (prevents dual-dispatch
            # when multiple slots call select_and_claim concurrently).
            unclaimed_active = {
                k: v
                for k, v in (state.get("active_tasks") or {}).items()
                if not (v or {}).get("claimed_by")
            }
            if unclaimed_active:
                sel = self._by_oldest(unclaimed_active)
                if sel:
                    selected_bucket, selected_key = "active_tasks", sel[0]

            if not selected_key:
                now_epoch = time.time()
                incomplete = {
                    k: v
                    for k, v in (state.get("incomplete_tasks") or {}).items()
                    if str((v or {}).get("status") or "in_progress") != "waiting_human"
                    and float((v or {}).get("loop_next_dispatch_after") or "0") <= now_epoch
                }
                if incomplete:
                    sel = self._by_oldest(incomplete)
                    if sel:
                        selected_bucket, selected_key = "incomplete_tasks", sel[0]

            if not selected_key:
                queued = state.get("queued_tasks") or {}
                if queued:
                    # Serial-dispatch tasks (maintenance, development) take priority
                    # over regular queued tasks.
                    serial_queued = {
                        k: v for k, v in queued.items()
                        if self._is_serial_dispatch_type(
                            str((v or {}).get("task_type") or "")
                        )
                    }
                    sel = self._by_oldest(serial_queued) if serial_queued else self._by_oldest(queued)
                    if sel:
                        selected_bucket, selected_key = "queued_tasks", sel[0]

            if not selected_key:
                return None

            # --- Claim: same logic as the old claim_task_for_worker ---
            src_bucket = state.get(selected_bucket) or {}
            task = src_bucket.get(selected_key)
            if not isinstance(task, dict):
                return None

            claimed = self.normalize_task(task, selected_key, bucket_name="active_tasks")
            self.ensure_task_text_file(
                claimed,
                bucket_name="active_tasks",
                legacy_text=str(task.get("mention_text") or ""),
            )
            claimed["status"] = "in_progress"
            claimed["claimed_by"] = f"{self.cfg.worker_id}-slot-{worker_slot}"
            claimed["last_update_ts"] = now_val

            task_type = str(claimed.get("task_type") or "slack_mention")

            state.setdefault("active_tasks", {})[selected_key] = claimed
            if selected_bucket != "active_tasks":
                state.setdefault(selected_bucket, {}).pop(selected_key, None)
            self.save_state(state)

            # Write dispatch task file
            dispatch = dict((state.get("active_tasks") or {}).get(selected_key) or {})
            if dispatch:
                mention_text_file = self.ensure_task_text_file(dispatch, bucket_name="active_tasks")
                dispatch["mention_text"] = self.read_task_text(mention_text_file)
            self.atomic_write_json(target_file, dispatch)

        # Set instance variables for backward compat with serial path
        self.selected_bucket = selected_bucket
        self.selected_key = selected_key
        self._active_task_type = task_type

        return (selected_key, task_type)

    def select_next_task(self) -> bool:
        """Legacy selection-only method. Used by older tests. Does NOT claim."""
        with self.state_lock():
            state = self.load_state()

        active = state.get("active_tasks") or {}
        if active:
            sel = self._by_oldest(active)
            if sel:
                self.selected_bucket, self.selected_key = "active_tasks", sel[0]
                return True

        now_epoch = time.time()
        incomplete = {
            k: v
            for k, v in (state.get("incomplete_tasks") or {}).items()
            if str((v or {}).get("status") or "in_progress") != "waiting_human"
            and float((v or {}).get("loop_next_dispatch_after") or "0") <= now_epoch
        }
        if incomplete:
            sel = self._by_oldest(incomplete)
            if sel:
                self.selected_bucket, self.selected_key = "incomplete_tasks", sel[0]
                return True

        queued = state.get("queued_tasks") or {}
        if queued:
            sel = self._by_oldest(queued)
            if sel:
                self.selected_bucket, self.selected_key = "queued_tasks", sel[0]
                return True

        return False

    def claim_task_for_worker(self, bucket: str, key: str) -> None:
        """Legacy wrapper: used by tests and the serial path's old flow."""
        now_val = now_ts()
        with self.state_lock():
            state = self.load_state()
            src_bucket = state.get(bucket) or {}
            task = src_bucket.get(key)
            if not isinstance(task, dict):
                return
            claimed = self.normalize_task(task, key, bucket_name="active_tasks")
            self.ensure_task_text_file(
                claimed,
                bucket_name="active_tasks",
                legacy_text=str(task.get("mention_text") or ""),
            )
            claimed["status"] = "in_progress"
            claimed["claimed_by"] = self.cfg.worker_id
            claimed["last_update_ts"] = now_val
            self._active_task_type = str(claimed.get("task_type") or "slack_mention")
            state.setdefault("active_tasks", {})[key] = claimed
            if bucket != "active_tasks":
                state.setdefault(bucket, {}).pop(key, None)
            self.save_state(state)

            dispatch = dict((state.get("active_tasks") or {}).get(key) or {})
            if dispatch:
                mention_text_file = self.ensure_task_text_file(dispatch, bucket_name="active_tasks")
                dispatch["mention_text"] = self.read_task_text(mention_text_file)
            self.atomic_write_json(self.cfg.dispatch_task_file, dispatch)

    def _outcome_path_for_task(self, task_key: str) -> Path:
        """Return the per-task outcome file path (absolute)."""
        return Path.cwd() / self.cfg.outcomes_dir / f"{task_key}.json"

    def _build_loop_context(self, dispatch_json: Dict[str, Any]) -> str:
        """Build loop-mode context string for the worker prompt.

        Returns empty string when loop mode is not active.
        """
        if not dispatch_json.get("loop_mode"):
            return ""
        iteration = dispatch_json.get("loop_iteration", 0)
        deadline = float(dispatch_json.get("loop_deadline") or "0")
        remaining_sec = max(0, int(deadline - time.time()))
        hours, remainder = divmod(remaining_sec, 3600)
        minutes = remainder // 60
        if hours > 0:
            remaining_str = f"~{hours}h{minutes:02d}m"
        else:
            remaining_str = f"~{minutes}m"
        template_path = self.cfg.session_template.parent / "loop_context.md"
        try:
            template = template_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            template = (
                "## Loop Mode (Active)\n\n"
                "You are in continuous loop mode (iteration {{LOOP_ITERATION}}, {{LOOP_REMAINING}} remaining).\n"
                "Keep making autonomous progress each iteration.\n"
            )
        return template.replace("{{LOOP_ITERATION}}", str(iteration)).replace("{{LOOP_REMAINING}}", remaining_str)

    def render_runtime_prompt(self) -> None:
        dispatch_json = self.read_json(self.cfg.dispatch_task_file, {})

        # Non-worker maintenance phases (developer, tribune) use standalone
        # prompts (no session.md / Murphy identity).  Each CLI auto-loads
        # its own agent doc (CLAUDE.md for developer, GEMINI.md for tribune).
        if self._active_task_type == "maintenance":
            phase = self.maintenance.get_phase(dispatch_json)
            role = self.maintenance.phase_role(phase)
            if role in ("developer", "tribune"):
                dispatch_str = json.dumps(dispatch_json, ensure_ascii=False, indent=2)
                self._render_standalone_phase_prompt(phase, dispatch_json, dispatch_str)
                return

        # Development tasks use their own prompt (loaded during enqueue).
        if self._active_task_type == "development":
            task_data = self.read_json(
                Path(dispatch_json.get("mention_text_file") or str(self.cfg.dispatch_task_file)), {}
            )
            messages = task_data.get("messages") or []
            prompt_text = messages[0].get("text", "") if messages else ""
            self.cfg.runtime_prompt_file.parent.mkdir(parents=True, exist_ok=True)
            self.cfg.runtime_prompt_file.write_text(prompt_text + "\n", encoding="utf-8")
            return

        # Inject async job wakeup context for regular tasks
        task_key = str(dispatch_json.get("mention_ts") or self.selected_key or "")
        self._inject_wakeup_context(dispatch_json, task_key)
        outcome_path = str(self._outcome_path_for_task(task_key)) if task_key else str(self.cfg.dispatch_outcome_file)

        # Build thread context as a chat transcript (separate from task JSON)
        mention_text_file = str(dispatch_json.get("mention_text_file") or "")
        original_request_str, thread_context_str = self._render_thread_context(mention_text_file)

        # Build a cleaned dispatch JSON for the prompt (strip internal fields)
        prompt_json = {
            k: v for k, v in dispatch_json.items()
            if k not in self._DISPATCH_INTERNAL_FIELDS
        }
        prompt_dispatch_str = json.dumps(prompt_json, ensure_ascii=False, indent=2)

        lines = self.cfg.session_template.read_text(encoding="utf-8").splitlines()
        memory_context_str = ""
        if any("{{SESSION_MEMORY_CONTEXT}}" in line for line in lines):
            memory_context_str = self.build_session_memory_context()
        loop_context_str = self._build_loop_context(dispatch_json)
        user_id = str((dispatch_json.get("source") or {}).get("user_id") or "")
        user_profile_body = self.read_user_profile(user_id)
        out: List[str] = []
        for line in lines:
            if line == "{{ORIGINAL_REQUEST}}":
                if original_request_str:
                    out.extend(original_request_str.splitlines())
                continue
            if line == "{{THREAD_CONTEXT}}":
                if thread_context_str:
                    out.extend(thread_context_str.splitlines())
                continue
            if line == "{{DISPATCH_TASK_JSON}}":
                out.append(prompt_dispatch_str)
                continue
            if line == "{{SESSION_MEMORY_CONTEXT}}":
                if memory_context_str:
                    out.extend(memory_context_str.splitlines())
                continue
            if line == "{{LOOP_CONTEXT}}":
                if loop_context_str:
                    out.extend(loop_context_str.splitlines())
                continue
            if line == "{{USER_PROFILE}}":
                if user_profile_body:
                    out.append("About your collaborator:")
                    out.extend(user_profile_body.splitlines())
                else:
                    out.append("No prior interaction history with this user.")
                continue
            if line == "{{MERGE_INSTRUCTIONS}}":
                continue  # Serial mode — no worktree merge needed
            if line == "{{TRIBUNE_DRAFT_INSTRUCTIONS}}":
                if (
                    self.cfg.tribune_max_review_rounds >= 1
                    and self._active_task_type != "maintenance"
                ):
                    task_key_for_draft = str(dispatch_json.get("mention_ts") or self.selected_key or "draft")
                    draft_path = str(self.cfg.dispatch_dir / f"slack_draft.{task_key_for_draft}.md")
                    out.append("")
                    out.append("## Tribune Review (Active)")
                    out.append("")
                    out.append(
                        "Your final response will be reviewed by an independent quality "
                        "reviewer (Tribune) before posting to Slack. Instead of posting "
                        "your final response via Slack MCP, write it to "
                        f"`{draft_path}` as a Markdown file. The supervisor will post it "
                        "after Tribune approval."
                    )
                    out.append("")
                    out.append(
                        "**Important:** Only the *final* response (your concluding answer/"
                        "deliverable) goes into the draft file instead of Slack. "
                        "Set your outcome status normally per AGENTS.md — the supervisor "
                        "will route the draft through Tribune review before posting."
                    )
                    # Inject Tribune feedback from prior revision round
                    tribune_feedback = dispatch_json.get("tribune_feedback")
                    if tribune_feedback:
                        revision = dispatch_json.get("tribune_revision_count", 0)
                        out.append("")
                        out.append(f"### Tribune Feedback (Revision Round {revision})")
                        out.append("")
                        out.append(
                            "The Tribune reviewed your previous draft and requested revisions:"
                        )
                        out.append("")
                        out.append(str(tribune_feedback))
                        out.append("")
                        out.append("Address this feedback in your revised draft.")
                continue
            line = line.replace("{{SLACK_ID}}", self.resolve_slack_id())
            line = line.replace("{{AGENT_NAME}}", self.cfg.agent_name)
            line = line.replace("{{DISPATCH_OUTCOME_PATH}}", outcome_path)
            out.append(line)

        self.cfg.runtime_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.runtime_prompt_file.write_text("\n".join(out) + "\n", encoding="utf-8")

    def _render_standalone_phase_prompt(
        self, phase: int, dispatch_json: Dict[str, Any], dispatch_str: str
    ) -> None:
        """Render a standalone prompt for non-worker maintenance phases.

        Used for both developer review and tribune review phases.
        Loads the phase-appropriate template and prepends it to the
        conversation context and dispatch JSON.
        """
        review_template = self.maintenance.load_prompt(phase)
        review_template = review_template.replace("{{AGENT_NAME}}", self.cfg.agent_name)
        thread_context = str(dispatch_json.get("mention_text") or "")
        task_key = str(dispatch_json.get("mention_ts") or self.selected_key or "")
        outcome_path = str(self._outcome_path_for_task(task_key)) if task_key else str(self.cfg.dispatch_outcome_file)

        prompt = (
            f"{review_template}\n\n"
            f"{thread_context}\n\n"
            f"Your task dispatch context:\n"
            f"```json\n{dispatch_str}\n```\n\n"
            f"Write the outcome file to `{outcome_path}` before exit:\n"
            f"```json\n"
            f'{{\n'
            f'  "mention_ts": "<task_id from dispatch>",\n'
            f'  "thread_ts": "<thread_ts from dispatch — reply in this existing thread>",\n'
            f'  "status": "done | waiting_human",\n'
            f'  "summary": "<short plain-text summary>",\n'
            f'  "completion_confidence": "high | medium | low",\n'
            f'  "requires_human_feedback": true | false,\n'
            f'  "error": "<optional>"\n'
            f'}}\n'
            f"```\n"
        )

        self.cfg.runtime_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        self.cfg.runtime_prompt_file.write_text(prompt, encoding="utf-8")

    @staticmethod
    def _trim_prompt_text(text: str, max_chars: int) -> str:
        content = text.rstrip()
        if len(content) <= max_chars:
            return content
        trimmed = content[-max_chars:]
        return f"[truncated to last {max_chars} chars]\n{trimmed}"

    @staticmethod
    def _hard_clip_with_prefix(content: str, max_chars: int, prefix: str = "") -> str:
        content = content.rstrip()
        if max_chars <= 0:
            return ""

        if not prefix:
            if len(content) <= max_chars:
                return content
            return content[-max_chars:]

        if len(prefix) >= max_chars:
            return prefix[:max_chars]

        tail_budget = max_chars - len(prefix)
        if len(content) <= tail_budget:
            return prefix + content
        return prefix + content[-tail_budget:]

    @staticmethod
    def _relative_path(path: Path) -> str:
        try:
            return str(path.resolve().relative_to(Path.cwd().resolve()))
        except Exception:
            return str(path)

    def _ensure_daily_memory_file(self, day: datetime) -> Path:
        self.cfg.memory_daily_dir.mkdir(parents=True, exist_ok=True)
        day_str = day.strftime("%Y-%m-%d")
        path = self.cfg.memory_daily_dir / f"{day_str}.md"
        if path.exists():
            return path

        body = (
            f"# Daily Memory — {day_str}\n\n"
            f"- [{timestamp_utc()}] Session bootstrap: add concise episodic notes for this day.\n"
        )
        path.write_text(body, encoding="utf-8")
        return path

    def _read_prompt_memory_file(self, path: Path) -> str:
        if not path.exists():
            return "[missing]"
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return "[unreadable]"
        if not text.strip():
            return "[empty]"
        return self._trim_prompt_text(text, self.PROMPT_MEMORY_SECTION_CHAR_LIMIT)

    @staticmethod
    def _trim_with_note_to_recent(text: str, max_chars: int, note: str) -> str:
        content = text.rstrip()
        if max_chars <= 0:
            return ""
        if len(content) <= max_chars:
            return content
        if not note:
            return content[-max_chars:]
        if len(note) >= max_chars:
            return note[:max_chars]
        tail_budget = max_chars - len(note)
        return note + content[-tail_budget:]

    @staticmethod
    def _render_memory_sections(sections: List[Dict[str, str]]) -> str:
        rendered_sections: List[str] = []
        for section in sections:
            rendered_sections.append(
                f"### {section['title']}\n"
                f"Source: `{section['path']}`\n"
                "```markdown\n"
                f"{section['body']}\n"
                "```"
            )
        return "\n\n".join(rendered_sections)

    def build_session_memory_context(self) -> str:
        now_utc = datetime.now(timezone.utc)
        daily_days = [
            now_utc - timedelta(days=1),
            now_utc,
        ]
        daily_paths = [self._ensure_daily_memory_file(day) for day in daily_days]

        sections: List[Dict[str, str]] = []
        sections.append(
            {
                "title": "Curated Memory",
                "path": self._relative_path(self.cfg.memory_file),
                "body": self._read_prompt_memory_file(self.cfg.memory_file),
            }
        )
        sections.append(
            {
                "title": "Long-Term Goals (Pointer)",
                "path": self._relative_path(self.cfg.long_term_goals_file),
                "body": self.PROMPT_MEMORY_POINTER_BODY,
            }
        )
        sections.append(
            {
                "title": "Daily Episodic Memory (Yesterday UTC, Pointer)",
                "path": self._relative_path(daily_paths[0]),
                "body": self.PROMPT_MEMORY_POINTER_BODY,
            }
        )
        sections.append(
            {
                "title": "Daily Episodic Memory (Today UTC, Pointer)",
                "path": self._relative_path(daily_paths[1]),
                "body": self.PROMPT_MEMORY_POINTER_BODY,
            }
        )

        total_limit = int(
            getattr(
                self.cfg,
                "prompt_memory_total_char_limit",
                self.PROMPT_MEMORY_TOTAL_CHAR_LIMIT_DEFAULT,
            )
        )
        if total_limit <= 0:
            return ""

        context = self._render_memory_sections(sections)
        if len(context) <= total_limit:
            return context

        # Soft guard: when over budget, drop earlier curated memory first and keep the most recent context.
        curated_index = 0
        curated_original = str(sections[curated_index].get("body") or "")
        keep_chars = len(curated_original)
        while len(context) > total_limit and keep_chars > 0:
            overflow = len(context) - total_limit
            keep_chars = max(0, keep_chars - overflow - 64)
            if keep_chars <= 0:
                sections[curated_index]["body"] = self.PROMPT_MEMORY_COLLAPSED_BODY
            else:
                sections[curated_index]["body"] = self._trim_with_note_to_recent(
                    curated_original,
                    keep_chars,
                    self.PROMPT_MEMORY_DROPPED_EARLY_NOTE,
                )
            context = self._render_memory_sections(sections)

        if len(context) <= total_limit:
            return context
        return self._hard_clip_with_prefix(context, total_limit, self.PROMPT_MEMORY_SOFT_GUARD_NOTE)

    # ------------------------------------------------------------------
    # Session resume helpers (AGENT-025)
    # ------------------------------------------------------------------

    def _should_resume_session(self, task: Dict[str, Any]) -> bool:
        """Check if we can resume a prior codex session for this task."""
        if not self.cfg.session_resume_enabled:
            return False
        session_id = task.get("codex_session_id")
        if not session_id:
            return False
        # Task identity check: the stored session must belong to this task.
        # Without this, a hot-restart or slot reassignment can resume a
        # session that was semantically working on a different task.
        stored_task_id = task.get("session_task_id", "")
        current_task_id = str(task.get("mention_ts") or "")
        if stored_task_id and current_task_id and stored_task_id != current_task_id:
            return False
        # Stale prompt check
        stored_hash = task.get("session_prompt_hash", "")
        if stored_hash != system_prompt_hash():
            return False
        # Tribune revision: need full prompt with feedback block
        if task.get("tribune_feedback"):
            return False
        # Context staleness: after too many resumes, force a fresh dispatch
        # with full thread context to avoid degraded context quality.
        resume_count = int(task.get("session_resume_count") or 0)
        if resume_count >= self.cfg.max_session_resumes:
            return False
        return True

    def _build_resume_cmd(self, task: Dict[str, Any]) -> list:
        """Build codex exec resume command derived from WORKER_CMD.

        Returns empty list if the command shape doesn't support resume.
        """
        session_id = task.get("codex_session_id", "")
        if not session_id:
            return []
        base = list(self.cfg.worker_cmd)
        if "--ephemeral" in base:
            base.remove("--ephemeral")
        has_stdin = base and base[-1] == "-"
        if has_stdin:
            base.pop()
        try:
            exec_idx = base.index("exec")
            insert_args = ["resume", session_id]
            if has_stdin:
                insert_args.append("-")
            for i, arg in enumerate(insert_args):
                base.insert(exec_idx + 1 + i, arg)
        except ValueError:
            return []
        return base

    def _build_fresh_worker_cmd(self) -> list:
        """Build worker command for fresh dispatch.

        When session_resume_enabled, drops --ephemeral so sessions persist.
        """
        cmd = list(self.cfg.worker_cmd)
        if self.cfg.session_resume_enabled and "--ephemeral" in cmd:
            cmd.remove("--ephemeral")
        return cmd

    def _render_resume_prompt(
        self, dispatch_json: Dict[str, Any], task_key: str,
        slot_context: Optional[Dict[str, str]] = None,
    ) -> str:
        """Render the lightweight resume prompt for session re-dispatch.

        *slot_context*, when provided, carries slot-specific overrides
        (``repo_root``, ``branch_name``, ``draft_path``) so a resumed
        session gets the current slot's merge instructions and draft
        path regardless of which slot originally ran it (AGENT-046).
        """
        template_path = self.cfg.session_template.parent / "session_resume.md"
        try:
            template = template_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Fallback inline template
            template = (
                "You are resuming your prior session on this task.\n\n"
                "## New Thread Messages\n{{NEW_THREAD_MESSAGES}}\n\n"
                "## Task State\n{{TASK_STATE_UPDATES}}\n\n"
                "{{WAKEUP_CONTEXT}}\n\n{{LOOP_CONTEXT}}\n\n"
                "{{SLOT_OVERRIDES}}\n\n"
                "Continue from where you left off.\n\n"
                "Write the outcome file to `{{DISPATCH_OUTCOME_PATH}}` "
                "per the schema in `docs/schemas.md`.\n"
            )

        # New thread messages since last session
        session_end_ts = str(dispatch_json.get("session_end_ts") or "0")
        mention_text_file = str(dispatch_json.get("mention_text_file") or "")
        new_messages_str = "No new messages since your last session."
        if mention_text_file:
            try:
                task_data = self.read_json(Path(mention_text_file), {})
                messages = task_data.get("messages") or []
                new_msgs = [
                    m for m in messages
                    if ts_gt(str(m.get("ts") or "0"), session_end_ts)
                ]
                if new_msgs:
                    lines = []
                    for m in new_msgs:
                        name = m.get("user_name") or m.get("role") or "unknown"
                        text = m.get("text") or ""
                        lines.append(f"[{name}] {text}")
                    new_messages_str = "\n".join(lines)
            except Exception:
                pass

        # Task state updates
        status = str(dispatch_json.get("status") or "")
        task_state_str = ""
        if status == "in_progress":
            prev_error = str(dispatch_json.get("last_error") or "")
            if "waiting_human" in prev_error or "reactivated" in prev_error:
                task_state_str = (
                    "Task was waiting for your collaborator's input and has "
                    "been re-activated by their reply."
                )

        # Wakeup context
        wakeup_str = ""
        self._inject_wakeup_context(dispatch_json, task_key)
        wakeups = dispatch_json.get("pending_job_wakeups")
        dispatch_token = dispatch_json.get("dispatch_token")
        if wakeups or dispatch_token:
            parts = []
            if dispatch_token:
                parts.append(f"Dispatch token: `{dispatch_token}`")
            if wakeups:
                parts.append(f"Pending job wakeups: {json.dumps(wakeups)}")
            wakeup_str = "## Async Jobs\n" + "\n".join(parts)

        # Loop context
        loop_context_str = self._build_loop_context(dispatch_json)

        # Outcome path
        outcome_path = str(
            self._outcome_path_for_task(task_key) if task_key
            else self.cfg.dispatch_outcome_file
        )

        # AGENT-046: slot-specific overrides for cross-slot resume
        slot_overrides_str = ""
        if slot_context:
            parts: list = []
            # Merge instructions (parallel only — serial skips merge)
            repo_root = slot_context.get("repo_root")
            branch_name = slot_context.get("branch_name")
            if repo_root and branch_name:
                merge_path = self.cfg.session_template.parent / "merge_instructions.md"
                if merge_path.exists():
                    merge_tpl = merge_path.read_text(encoding="utf-8").rstrip("\n")
                    merge_block = (
                        merge_tpl
                        .replace("{{REPO_ROOT}}", repo_root)
                        .replace("{{BRANCH_NAME}}", branch_name)
                    )
                    parts.append(merge_block)
            elif slot_context.get("serial_mode"):
                # Serial resume: explicitly cancel any stale parallel merge
                # instructions that may remain in the preserved session context.
                parts.append(
                    "You are running in the main repository (serial mode). "
                    "Ignore any prior worktree merge instructions — no branch "
                    "merge is needed."
                )
            # Tribune draft path (conditional on same gate as full prompt)
            draft_path = slot_context.get("draft_path")
            if draft_path:
                parts.append(
                    f"Write your final response to `{draft_path}` as a "
                    f"Markdown file. The supervisor will post it after "
                    f"Tribune approval."
                )
            if parts:
                slot_overrides_str = (
                    "## Updated Instructions\n\n" + "\n\n".join(parts)
                )

        # Thread file path for full conversation access
        thread_file_path = mention_text_file or ""

        result = template
        result = result.replace("{{THREAD_FILE_PATH}}", thread_file_path)
        result = result.replace("{{NEW_THREAD_MESSAGES}}", new_messages_str)
        result = result.replace("{{TASK_STATE_UPDATES}}", task_state_str)
        result = result.replace("{{WAKEUP_CONTEXT}}", wakeup_str)
        result = result.replace("{{LOOP_CONTEXT}}", loop_context_str)
        result = result.replace("{{SLOT_OVERRIDES}}", slot_overrides_str)
        result = result.replace("{{DISPATCH_OUTCOME_PATH}}", outcome_path)
        return result

    _SESSION_FIELDS = (
        "codex_session_id", "session_prompt_hash", "session_slot_id",
        "session_end_ts", "session_dispatch_mode", "dispatch_prompt_hash",
        "session_task_id",
    )

    def run_worker_once(
        self, disable_resume: bool = False
    ) -> Tuple[int, str, bool]:
        """Run one worker dispatch.

        Returns (exit_code, output, was_resume).
        """
        is_privileged = False  # Only developer review can edit source
        was_resume = False
        if self.maintenance.is_maintenance_task(self._active_task_type):
            dispatch = self.read_json(self.cfg.dispatch_task_file, {})
            phase = self.maintenance.get_phase(dispatch)
            cmd = self.maintenance.get_worker_cmd(phase)
            is_privileged = self.maintenance.is_dev_review_phase(phase)
            # Name maintenance sessions for identifiable --resume listings.
            _maint_role = self.maintenance.phase_role(phase)
            if _maint_role != "worker":  # claude -p phases only (codex has no --name)
                cmd.extend(["--name", f"[maintenance: {_maint_role} review]"])
        elif self._active_task_type == "development":
            if not self.cfg.dev_review_cmd:
                return 1, (
                    "Developer review is disabled in this install. "
                    "Install Claude Code or set DEV_REVIEW_CMD to enable it."
                ), False
            cmd = list(self.cfg.dev_review_cmd)
            is_privileged = True
            # Session ID: deterministic from marker + task key + timestamp.
            import uuid as _uuid
            dev_session_id = str(_uuid.uuid5(_uuid.NAMESPACE_URL, f"development:{self.selected_key}:{now_ts()}"))
            cmd.extend(["--session-id", dev_session_id])
            # Extract item ID for session naming and mapping.
            _dev_item_id = ""
            _active = self.load_state().get("active_tasks", {}).get(self.selected_key, {})
            _desc = str(_active.get("task_description") or "")
            if _desc.startswith("Development: "):
                _dev_item_id = _desc[len("Development: "):].rstrip(".")
            if _dev_item_id:
                cmd.extend(["--name", f"[development: {_dev_item_id}]"])
            self._write_dev_session_id(dev_session_id, item_id=_dev_item_id)
        else:
            # Session resume check (AGENT-025)
            dispatch = self.read_json(self.cfg.dispatch_task_file, {})
            resume_cmd = (
                self._build_resume_cmd(dispatch)
                if not disable_resume
                and self._should_resume_session(dispatch)
                else []
            )
            if resume_cmd:
                cmd = resume_cmd
                was_resume = True
            else:
                cmd = self._build_fresh_worker_cmd()
        # Use resume prompt if resuming, otherwise full rendered prompt
        if was_resume:
            dispatch = self.read_json(self.cfg.dispatch_task_file, {})
            # AGENT-046: build serial slot context (draft only, no merge;
            # serial_mode flag cancels any stale parallel merge instructions
            # from prior session context)
            serial_ctx: Dict[str, str] = {"serial_mode": "true"}
            if (
                self.cfg.tribune_max_review_rounds >= 1
                and self._active_task_type != "maintenance"
            ):
                task_key_for_draft = str(
                    dispatch.get("mention_ts")
                    or self.selected_key or "draft"
                )
                serial_ctx["draft_path"] = str(
                    self.cfg.dispatch_dir
                    / f"slack_draft.{task_key_for_draft}.md"
                )
            prompt = self._render_resume_prompt(
                dispatch, self.selected_key or "",
                slot_context=serial_ctx,
            )
        else:
            prompt = self.cfg.runtime_prompt_file.read_text(encoding="utf-8")
        timeout_sec = self.cfg.session_minutes * 60

        # Strip CLAUDECODE so nested claude -p sessions are not blocked.
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        # Mark agent workers so the pre-commit hook enforces write protection.
        # Only developer review legitimately edits source; worker and tribune
        # are write-protected.
        if not is_privileged:
            env["AGENT_WORKER"] = "1"
        # Maintenance runs in the main worktree on the main branch.
        # Tell the pre-commit hook to allow direct-to-main commits
        # (protected-path checks still apply).
        if self.maintenance.is_maintenance_task(self._active_task_type):
            env["AGENT_MAIN_WORKTREE"] = "1"
        # Disable Gemini sandbox for Tribune phases.
        if cmd and "gemini" in cmd[0]:
            env["GEMINI_SANDBOX"] = "false"

        # Consult history persistence env vars.
        env["CONSULT_TASK_ID"] = self.selected_key or ""
        env["CONSULT_HISTORY_DIR"] = str(Path.cwd() / self.cfg.consult_history_dir)

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                check=False,
                env=env,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode, output, was_resume
        except subprocess.TimeoutExpired as e:
            output = (e.stdout or "") + (e.stderr or "")
            return 124, output, was_resume

    @staticmethod
    def classify_failure(output: str, exit_code: int) -> str:
        if exit_code == 0:
            return "none"
        if exit_code in {124, 137}:
            return "timeout"
        if AUTH_FAILURE_PATTERN.search(output or ""):
            return "auth_failure"
        if TRANSIENT_PATTERN.search(output or ""):
            return "transient_transport"
        return "nonzero_exit"

    @staticmethod
    def error_preview(output: str, max_chars: int = 500) -> str:
        lines = (output or "").splitlines()
        tail = " ".join(lines[-8:])
        tail = re.sub(r"\s+", " ", tail).strip()
        return tail[:max_chars]

    def run_worker_with_retries(self) -> int:
        session_exit = 0
        transient_retry_attempt = 0
        attempt = 0
        resume_failed = False  # AGENT-025: disable resume on first failure

        while True:
            attempt += 1
            self.write_heartbeat("running_session", 0, False, 0, self.last_failure_kind, transient_retry_attempt)
            self.log_line(f"session_attempt_start loop={self.loop_count} attempt={attempt} task={self.selected_key}")

            session_exit, output, was_resume = self.run_worker_once(
                disable_resume=resume_failed,
            )
            self.cfg.last_session_log.parent.mkdir(parents=True, exist_ok=True)
            self.cfg.last_session_log.write_text(output, encoding="utf-8")

            if session_exit == 0:
                self.last_failure_kind = "none"
                self.transient_backoff_sec = self.cfg.transient_retry_initial
                self.last_transient_retry_attempt = transient_retry_attempt
                self._auth_backoff_sec = self.cfg.auth_retry_initial
                return 0

            failure_kind = self.classify_failure(output, session_exit)

            # Post-hoc MCP startup failure check (serial mode)
            if failure_kind not in ("timeout", "auth_failure"):
                from .utils import MCP_STARTUP_FAILURE_PATTERN
                if MCP_STARTUP_FAILURE_PATTERN.search((output or "")[:8192]):
                    failure_kind = "mcp_startup_failed"

            self.last_failure_kind = failure_kind
            preview = self.error_preview(output, 500)
            self.log_line(
                f"session_attempt_error loop={self.loop_count} attempt={attempt} "
                f"exit_code={session_exit} failure_kind={failure_kind} "
                f"was_resume={was_resume} preview=\"{preview}\""
            )

            # AGENT-025: if resume failed, disable it for remaining retries
            if was_resume and not resume_failed:
                resume_failed = True
                self.log_line(
                    f"resume_failed task={self.selected_key} "
                    f"falling_back_to_fresh"
                )
                # Re-render the full prompt for fresh dispatch
                self.render_runtime_prompt()
                continue

            # Auth failures: exponential backoff, then park as waiting_human
            if failure_kind == "auth_failure":
                if attempt <= self.cfg.auth_max_retries:
                    retry_sleep = self._auth_backoff_sec
                    self.write_heartbeat(
                        "retrying_auth",
                        session_exit,
                        False,
                        retry_sleep,
                        failure_kind,
                        attempt,
                    )
                    self.log_line(
                        f"auth_retry_scheduled loop={self.loop_count} attempt={attempt} "
                        f"retry_in_sec={retry_sleep} max={self.cfg.auth_max_retries}"
                    )
                    time.sleep(retry_sleep)
                    self._auth_backoff_sec = min(
                        self.cfg.auth_retry_max,
                        self._auth_backoff_sec * self.cfg.auth_retry_multiplier,
                    )
                    continue
                # Exhausted auth retries — return failure so task gets parked
                self.log_line(
                    f"auth_retries_exhausted loop={self.loop_count} attempts={attempt} "
                    f"task={self.selected_key}"
                )
                self._auth_backoff_sec = self.cfg.auth_retry_initial
                self.last_transient_retry_attempt = transient_retry_attempt
                return session_exit

            if failure_kind in ("transient_transport", "mcp_startup_failed") and attempt <= self.cfg.max_transient_retries:
                transient_retry_attempt = attempt
                retry_sleep = self.transient_backoff_sec
                self.write_heartbeat(
                    "retrying_transient",
                    session_exit,
                    False,
                    retry_sleep,
                    failure_kind,
                    transient_retry_attempt,
                )
                self.log_line(
                    f"transient_retry_scheduled loop={self.loop_count} attempt={attempt} retry_in_sec={retry_sleep}"
                )
                time.sleep(retry_sleep)
                self.transient_backoff_sec = min(
                    self.cfg.transient_retry_max,
                    self.transient_backoff_sec * self.cfg.transient_retry_multiplier,
                )
                continue

            self.last_transient_retry_attempt = transient_retry_attempt
            return session_exit

    def reconcile_task_after_run(
        self, key: str, worker_exit: int,
        outcome_file_override: Path | None = None,
        captured_session_id: Optional[str] = None,
        captured_slot_id: Optional[int] = None,
        dispatch_prompt_hash: Optional[str] = None,
        worker_exit_ts: Optional[str] = None,
    ) -> None:
        now_val = now_ts()
        status = "in_progress"
        summary = ""
        requires_human_feedback = "false"
        confidence = ""
        error = ""
        outcome_thread_ts = ""
        outcome_key_mismatch = False
        new_projects: List[str] = []

        # Per-task outcome file (primary), then legacy shared file (fallback)
        per_task_outcome = self._outcome_path_for_task(key)
        if outcome_file_override and outcome_file_override.exists():
            outcome_file = outcome_file_override
        elif per_task_outcome.exists():
            outcome_file = per_task_outcome
        else:
            outcome_file = self.cfg.dispatch_outcome_file

        if worker_exit == 0 and outcome_file.exists():
            try:
                outcome = json.loads(outcome_file.read_text(encoding="utf-8"))
                outcome_mention = str(outcome.get("mention_ts") or "")
                status = str(outcome.get("status") or "in_progress")
                summary = str(outcome.get("summary") or "")
                requires_human_feedback = str(outcome.get("requires_human_feedback", False)).lower()
                confidence = str(outcome.get("completion_confidence") or "")
                error = str(outcome.get("error") or "")
                outcome_thread_ts = str(outcome.get("thread_ts") or "")
                raw_project = outcome.get("project") or []
                if isinstance(raw_project, str):
                    raw_project = [raw_project] if raw_project else []
                new_projects = [s for s in raw_project if s]
                if outcome_mention and outcome_mention != key:
                    outcome_key_mismatch = True
                    status = "in_progress"
                    error = f"outcome_mention_mismatch expected={key} got={outcome_mention}"
                    summary = ""
                    outcome_thread_ts = ""
                    requires_human_feedback = "false"
                    confidence = ""
            except Exception as exc:
                error = f"invalid_dispatch_outcome_json: {type(exc).__name__}: {exc}"
        elif worker_exit != 0:
            error = f"worker_exit={worker_exit}"
        elif self._active_task_type == "development":
            # Development tasks run /iterative-review which commits and pushes
            # but does not write an outcome file.  Exit 0 = success.
            status = "done"
            summary = f"Development task completed ({key})"
            self.DEV_SESSION_ID_FILE.unlink(missing_ok=True)
        else:
            error = "dispatch_outcome_missing"

        # Process job acknowledgements from the outcome (Plan 31 Phase 2).
        # This runs before state mutation so acks are applied even if the
        # task itself transitions to a non-final state.
        if worker_exit == 0 and outcome_file.exists():
            try:
                outcome_for_acks = json.loads(
                    outcome_file.read_text(encoding="utf-8")
                )
                self._process_job_acks(outcome_for_acks)
            except Exception:
                pass  # Best-effort; don't block reconciliation on ack failures

        if status not in {"done", "waiting_human", "in_progress", "failed"}:
            error = (error + "; " if error else "") + f"invalid_status={status}"
            status = "in_progress"

        if status == "done" and requires_human_feedback == "true":
            status = "waiting_human"

        # Completion gate enforcement:
        #   "high"     — trust worker's done status (no extra gating)
        #   "moderate" — require completion_confidence="high" for auto-completion
        #   "low"      — always hold done tasks for human review
        if status == "done":
            gate = self.cfg.completion_gate
            if gate == "low":
                status = "waiting_human"
                if not error:
                    error = "low_gate_requires_human_review"
            elif gate == "moderate" and confidence != "high":
                status = "waiting_human"
                if not error:
                    error = "moderate_gate_requires_high_confidence"

        with self.state_lock():
            state = self.load_state()
            active = state.get("active_tasks") or {}
            task = active.get(key)
            if not isinstance(task, dict):
                return
            task_type = str(task.get("task_type") or "slack_mention")
            is_maintenance = self.maintenance.is_maintenance_task(task_type)

            # Maintenance runs should always resolve to done/waiting_human on successful exits.
            if worker_exit == 0 and is_maintenance and status == "in_progress":
                status = "waiting_human"
                error = (error + "; " if error else "") + "maintenance_in_progress_disallowed"

            # Auth failures after retry exhaustion: park as waiting_human
            # so the supervisor stops retrying until the token is refreshed.
            if self.last_failure_kind == "auth_failure" and worker_exit != 0:
                status = "waiting_human"
                error = (error + "; " if error else "") + "auth_retries_exhausted"

            # Consecutive exit failure tracking: park task after repeated rapid
            # failures to prevent crash loops in the parallel dispatch path.
            if worker_exit != 0:
                prev = int(task.get("consecutive_exit_failures") or 0)
                task["consecutive_exit_failures"] = prev + 1
                if prev + 1 >= self.cfg.max_consecutive_exit_failures:
                    status = "waiting_human"
                    task["waiting_reason"] = "consecutive_exit_failures"
                    error = (error + "; " if error else "") + (
                        f"consecutive_exit_failures={prev + 1}"
                    )
                    self._post_completion_fallback(
                        str(task.get("channel_id") or ""),
                        str(task.get("thread_ts") or ""),
                        f"Task parked after {prev + 1} consecutive failures. "
                        "Re-mention to retry.",
                    )
            elif worker_exit == 0:
                task.pop("consecutive_exit_failures", None)
                task.pop("waiting_reason", None)

            destination = "incomplete_tasks"
            final_status = status
            if status == "done":
                destination = "finished_tasks"
                final_status = "done"
            elif status == "failed":
                destination = "incomplete_tasks"
                final_status = "in_progress"

            # Non-final maintenance phases are force-finished so the
            # reconciler can advance_phase and re-queue at the next phase.
            if (
                is_maintenance
                and not self.maintenance.is_final_phase(self.maintenance.get_phase(task))
                and worker_exit == 0
                and destination != "finished_tasks"
            ):
                destination = "finished_tasks"
                final_status = "done"

            # Loop mode: override status to force re-dispatch while loop is active.
            if task.get("loop_mode") and not is_maintenance:
                loop_deadline = float(task.get("loop_deadline") or "0")
                if time.time() < loop_deadline:
                    # Loop still active — always re-dispatch regardless of worker status.
                    # Preserve the worker's real status so !stop can restore it.
                    task["loop_worker_status"] = final_status
                    destination = "incomplete_tasks"
                    final_status = "in_progress"
                    task["loop_iteration"] = (task.get("loop_iteration") or 0) + 1
                    task["loop_next_dispatch_after"] = str(
                        time.time() + self.cfg.loop_iteration_delay_sec
                    )
                    remaining = int(loop_deadline - time.time())
                    self.log_line(
                        f"loop_iteration_complete key={key} iteration={task['loop_iteration']} "
                        f"worker_status={status} remaining_sec={remaining}"
                    )
                else:
                    # Loop expired — apply final status faithfully, clear loop fields
                    iteration = task.get("loop_iteration", 0)
                    task.pop("loop_mode", None)
                    task.pop("loop_deadline", None)
                    task.pop("loop_next_dispatch_after", None)
                    task.pop("loop_iteration", None)
                    task.pop("loop_worker_status", None)
                    self.log_line(
                        f"loop_expired key={key} iteration={iteration} final_status={final_status}"
                    )

            merged = self.normalize_task(task, key, bucket_name="active_tasks")
            if not str(merged.get("channel_id") or "") and self.cfg.default_channel_id:
                merged["channel_id"] = self.cfg.default_channel_id
            if not str(merged.get("task_description") or "").strip():
                merged["task_description"] = self.derive_task_description(merged)
            if (
                (not outcome_key_mismatch)
                and outcome_thread_ts
                and re.match(r"^[0-9]+\.[0-9]+$", outcome_thread_ts)
            ):
                merged["thread_ts"] = outcome_thread_ts
            merged["status"] = final_status
            merged["last_update_ts"] = now_val
            if summary:
                merged["summary"] = summary
            if error:
                merged["last_error"] = error
            if ts_gt(now_val, str(merged.get("last_seen_mention_ts") or "0")):
                merged["last_seen_mention_ts"] = now_val

            # Merge project associations (accumulate, never remove).
            if new_projects:
                existing_proj = merged.get("project") or []
                if isinstance(existing_proj, str):
                    existing_proj = [existing_proj] if existing_proj else []
                merged["project"] = list(dict.fromkeys(existing_proj + new_projects))

            # AGENT-025: store session resume metadata
            if captured_session_id and not is_maintenance:
                prev_session_id = merged.get("codex_session_id")
                merged["codex_session_id"] = captured_session_id
                merged["session_task_id"] = key  # bind session to task identity
                merged["session_prompt_hash"] = (
                    dispatch_prompt_hash or system_prompt_hash()
                )
                # Track resume count: increment if same session (resumed),
                # reset if new session (fresh dispatch).
                if prev_session_id and prev_session_id == captured_session_id:
                    merged["session_resume_count"] = int(
                        merged.get("session_resume_count") or 0
                    ) + 1
                else:
                    merged["session_resume_count"] = 0
                # AGENT-046: stopped writing session_dispatch_mode and
                # session_slot_id — slot affinity removed. Legacy fields
                # kept in _SESSION_FIELDS/_DISPATCH_INTERNAL_FIELDS for
                # cleanup of pre-existing tasks.
                # session_end_ts set after thread snapshot below
            elif worker_exit != 0 and "codex_session_id" in merged:
                # Failed dispatch: clear session so next dispatch is fresh
                for sf in self._SESSION_FIELDS:
                    merged.pop(sf, None)

            self.ensure_task_text_file(merged, bucket_name=destination, legacy_text=str(task.get("mention_text") or ""))

            state.setdefault("active_tasks", {}).pop(key, None)
            state.setdefault("queued_tasks", {}).pop(key, None)
            state.setdefault("incomplete_tasks", {}).pop(key, None)
            state.setdefault("finished_tasks", {}).pop(key, None)
            state.setdefault(destination, {})[key] = merged

            self.save_state(state)

        # Outside state lock: capture thread snapshot so agent replies are recorded.
        mention_text_file = str(merged.get("mention_text_file") or "")
        channel_id = str(merged.get("channel_id") or "")
        thread_ts = str(merged.get("thread_ts") or "")
        thread_msgs: List[Dict[str, str]] = []
        if mention_text_file and channel_id and re.match(r"^[0-9]+\.[0-9]+$", thread_ts):
            thread_msgs = self._fetch_thread_messages(channel_id, thread_ts)
            self._store_thread_snapshot(mention_text_file, thread_msgs)

        # AGENT-057: set continuation_pending if thread exceeds threshold
        if (
            thread_msgs
            and len(thread_msgs) >= self.cfg.thread_continuation_threshold
            and destination == "incomplete_tasks"
            and not is_maintenance
        ):
            with self.state_lock():
                state = self.load_state()
                t = (state.get("incomplete_tasks") or {}).get(key)
                if isinstance(t, dict) and not t.get("continuation_pending"):
                    t["continuation_pending"] = "thread_length"
                    self.save_state(state)
                    self.log_line(
                        f"thread_continuation_pending task={key} "
                        f"thread_msgs={len(thread_msgs)} "
                        f"threshold={self.cfg.thread_continuation_threshold}"
                    )

        # AGENT-025: set session_end_ts from highest thread message ts
        if captured_session_id and thread_msgs:
            last_thread_ts = max(
                (str(m.get("ts") or "0") for m in thread_msgs), default="0"
            )
            with self.state_lock():
                state = self.load_state()
                task_in_state = None
                for bucket in ("finished_tasks", "incomplete_tasks"):
                    task_in_state = (state.get(bucket) or {}).get(key)
                    if task_in_state:
                        break
                if task_in_state and task_in_state.get("codex_session_id"):
                    task_in_state["session_end_ts"] = last_thread_ts
                    self.save_state(state)

        # Delivery guard: if task resolved to "done" but thread has no agent
        # reply after the latest human message, downgrade to waiting_human.
        # This catches cases where Slack MCP was unavailable and the worker
        # reported done without actually delivering a response.
        #
        # Skip when Tribune draft mode is active: the worker intentionally
        # writes to a draft file instead of posting to Slack, so absence of
        # an agent message in the thread is expected.
        tribune_draft_exists = bool(
            self.cfg.tribune_max_review_rounds >= 1
            and not is_maintenance
            and self._resolve_draft_path(key)
        )
        if (
            final_status == "done"
            and not is_maintenance
            and thread_msgs
            and not self._has_agent_delivery(thread_msgs)
            and not tribune_draft_exists
        ):
            downgrade_reason = "done_without_delivery_evidence"
            self.log_line(
                f"delivery_guard_downgrade task={key} reason={downgrade_reason}"
            )
            with self.state_lock():
                state = self.load_state()
                finished = state.get("finished_tasks") or {}
                task_data = finished.pop(key, None)
                if task_data:
                    task_data["status"] = "waiting_human"
                    task_data["last_error"] = (
                        (str(task_data.get("last_error") or "") + "; " if task_data.get("last_error") else "")
                        + downgrade_reason
                    )
                    task_data["last_update_ts"] = now_ts()
                    state.setdefault("incomplete_tasks", {})[key] = task_data
                    self.save_state(state)
            final_status = "waiting_human"
            destination = "incomplete_tasks"
            # Notify user that the task completed but no reply was delivered
            self._post_completion_fallback(
                channel_id, thread_ts,
                "Task completed but the worker didn't post a reply. "
                "Re-mention to retry, or check the session log.",
            )

        # Mid-session human reply guard: if a human message arrived in the
        # thread after the last agent message (i.e., during or after the
        # worker's run but before the draft is posted), the draft is stale —
        # discard it and re-dispatch so the worker can address the new message.
        _stale_draft = False
        if (
            thread_msgs
            and not is_maintenance
            and final_status in ("done", "waiting_human")
        ):
            slack_id = self.resolve_slack_id()
            _last_agent_ts = "0"
            _last_human_ts = "0"
            for m in thread_msgs:
                user = m.get("user") or ""
                if user == slack_id:
                    _last_agent_ts = m.get("ts") or "0"
                elif user:
                    _last_human_ts = m.get("ts") or "0"
            if ts_gt(_last_human_ts, _last_agent_ts):
                draft_path = self._resolve_draft_path(key)
                if draft_path and Path(draft_path).exists():
                    self.log_line(
                        f"stale_draft_detected key={key} last_human={_last_human_ts} last_agent={_last_agent_ts} — re-dispatching"
                    )
                    Path(draft_path).unlink(missing_ok=True)
                    with self.state_lock():
                        state = self.load_state()
                        task_data = (state.get(destination) or {}).pop(key, None)
                        if task_data:
                            task_data["status"] = "in_progress"
                            task_data["claimed_by"] = None
                            task_data["last_update_ts"] = now_ts()
                            state.setdefault("incomplete_tasks", {})[key] = task_data
                            self.save_state(state)
                    final_status = "in_progress"
                    destination = "incomplete_tasks"
                    _stale_draft = True

        # Tribune post-dispatch review gate.  Fires when Tribune review is
        # enabled, the task is non-maintenance, and a draft file exists —
        # regardless of whether the worker set status to "done" or
        # "waiting_human" (both are valid per AGENTS.md).
        # Per-thread limit: skip Tribune once the task has been reviewed
        # TRIBUNE_MAX_REVIEWS_PER_THREAD times across all dispatches.
        thread_review_count = int(merged.get("tribune_review_count") or 0)
        if (
            not _stale_draft
            and final_status in ("done", "waiting_human")
            and not is_maintenance
            and self.cfg.tribune_max_review_rounds >= 1
            and thread_review_count < self.cfg.tribune_max_reviews_per_thread
        ):
            draft_path = self._resolve_draft_path(key)
            revision_count = int(merged.get("tribune_revision_count") or 0)

            if draft_path and Path(draft_path).exists():
                # Increment per-thread review counter
                thread_review_count += 1
                with self.state_lock():
                    state = self.load_state()
                    for bucket in ("active_tasks", "incomplete_tasks", "finished_tasks"):
                        if key in (state.get(bucket) or {}):
                            state[bucket][key]["tribune_review_count"] = thread_review_count
                            break
                    self.save_state(state)

                self.log_line(f"tribune_review_start key={key} round={revision_count + 1} thread_review={thread_review_count}/{self.cfg.tribune_max_reviews_per_thread}")
                verdict, feedback = self._tribune_review_cycle(key, merged, draft_path)
                self.log_line(f"tribune_review_done key={key} verdict={verdict}")

                if verdict == "approved":
                    posted = self._post_slack_draft(merged, draft_path)
                    if posted:
                        # Draft delivered — preserve worker's original status.
                        # Tribune approval gates draft quality, not task lifecycle.
                        # If worker set waiting_human, task stays waiting_human
                        # so the supervisor keeps polling for human replies.
                        #
                        # Re-snapshot thread so the just-posted message is
                        # captured in the task JSON.  Without this,
                        # refresh_waiting_human_tasks sees the pre-Tribune
                        # snapshot, treats the human reply as unaddressed,
                        # and spuriously re-dispatches (causing duplicate posts).
                        if mention_text_file and channel_id and thread_ts:
                            _post_msgs = self._fetch_thread_messages(channel_id, thread_ts)
                            if _post_msgs:
                                self._store_thread_snapshot(mention_text_file, _post_msgs)
                    else:
                        # Draft posting failed — ensure task is waiting_human
                        self.log_line(f"tribune_post_failed key={key} — downgrading to waiting_human")
                        with self.state_lock():
                            state = self.load_state()
                            task_data = (state.get(destination) or {}).pop(key, None)
                            if task_data:
                                task_data["status"] = "waiting_human"
                                task_data["last_error"] = (
                                    (str(task_data.get("last_error") or "") + "; " if task_data.get("last_error") else "")
                                    + "tribune_draft_post_failed"
                                )
                                task_data["last_update_ts"] = now_ts()
                                state.setdefault("incomplete_tasks", {})[key] = task_data
                                self.save_state(state)
                        final_status = "waiting_human"
                        destination = "incomplete_tasks"
                elif verdict == "revision_requested" and revision_count < self.cfg.tribune_max_review_rounds:
                    # Move task back to incomplete for worker re-dispatch with feedback
                    with self.state_lock():
                        state = self.load_state()
                        task_data = (state.get(destination) or {}).pop(key, None)
                        if task_data:
                            task_data["tribune_revision_count"] = revision_count + 1
                            task_data["tribune_feedback"] = feedback
                            task_data["status"] = "in_progress"
                            task_data["claimed_by"] = None
                            task_data["last_update_ts"] = now_ts()
                            state.setdefault("incomplete_tasks", {})[key] = task_data
                            self.save_state(state)
                    Path(draft_path).unlink(missing_ok=True)
                    final_status = "in_progress"
                    destination = "incomplete_tasks"
                    self.log_line(f"tribune_revision_requested key={key} round={revision_count + 1}")
                else:
                    # Max rounds reached or error — post draft as-is
                    posted = self._post_slack_draft(merged, draft_path)
                    if posted:
                        # Re-snapshot (same as approved path above).
                        if mention_text_file and channel_id and thread_ts:
                            _post_msgs = self._fetch_thread_messages(channel_id, thread_ts)
                            if _post_msgs:
                                self._store_thread_snapshot(mention_text_file, _post_msgs)
                    if not posted:
                        self.log_line(f"tribune_post_failed key={key} — downgrading to waiting_human")
                        with self.state_lock():
                            state = self.load_state()
                            task_data = (state.get(destination) or {}).pop(key, None)
                            if task_data:
                                task_data["status"] = "waiting_human"
                                task_data["last_error"] = (
                                    (str(task_data.get("last_error") or "") + "; " if task_data.get("last_error") else "")
                                    + "tribune_draft_post_failed"
                                )
                                task_data["last_update_ts"] = now_ts()
                                state.setdefault("incomplete_tasks", {})[key] = task_data
                                self.save_state(state)
                        final_status = "waiting_human"
                        destination = "incomplete_tasks"
            else:
                # No draft file — worker posted directly (fallback to current behavior)
                self.log_line(f"tribune_no_draft key={key}")
        elif (
            not _stale_draft
            and final_status in ("done", "waiting_human")
            and not is_maintenance
            and thread_review_count >= self.cfg.tribune_max_reviews_per_thread
        ):
            # Per-thread Tribune limit reached — post draft as-is without review
            draft_path = self._resolve_draft_path(key)
            if draft_path and Path(draft_path).exists():
                self.log_line(f"tribune_skip_thread_limit key={key} reviews={thread_review_count}/{self.cfg.tribune_max_reviews_per_thread}")
                posted = self._post_slack_draft(merged, draft_path)
                if posted and mention_text_file and channel_id and thread_ts:
                    _post_msgs = self._fetch_thread_messages(channel_id, thread_ts)
                    if _post_msgs:
                        self._store_thread_snapshot(mention_text_file, _post_msgs)

        # Outside state lock: add a check-mark reaction to the original message
        # to signal task completion.  For maintenance, only signal on the final
        # phase (intermediate phases re-queue the same task).
        skip_done_signal = is_maintenance and not self.maintenance.is_final_phase(
            self.maintenance.get_phase(merged)
        )
        if (
            final_status == "done"
            and not skip_done_signal
            and channel_id
            and re.match(r"^[0-9]+\.[0-9]+$", key)
        ):
            self._mark_task_done_reaction(channel_id, key)

        # Outside state lock: write project tag to task JSON and sync to project JSONs.
        # Maintenance tasks review all projects but don't belong to any — skip sync.
        all_projects = merged.get("project") if isinstance(merged, dict) else []
        if isinstance(all_projects, str):
            all_projects = [all_projects] if all_projects else []
        if isinstance(all_projects, list) and all_projects and not is_maintenance:
            task_file = str(merged.get("mention_text_file") or "")
            if task_file:
                self._write_project_to_task_json(task_file, all_projects)
                for slug in all_projects:
                    self._sync_task_to_project(task_file, slug)
            # Sync consult history to per-project aggregate
            if (self.cfg.consult_history_dir / f"{key}.jsonl").exists():
                for slug in all_projects:
                    self._sync_consult_to_project(key, slug)

        # Maintenance: post Tribune summary, advance to next phase, or restart.
        if is_maintenance:
            maint_phase = self.maintenance.get_phase(merged)
            # FIX-010 #4: Tribune maintenance summary bridge.
            # Tribune has read-only Slack access, so the supervisor posts on
            # its behalf.  Fires regardless of destination — a final Tribune
            # phase returning waiting_human still needs its summary delivered.
            if self.maintenance.phase_role(maint_phase) == "tribune" and summary:
                posted = self._post_maintenance_tribune_summary(merged, summary)
                # Re-snapshot thread so refresh_waiting_human_tasks sees
                # the just-posted summary and doesn't spuriously re-dispatch.
                if posted:
                    _ch = str(merged.get("channel_id") or "")
                    _ts = str(merged.get("thread_ts") or "")
                    _tf = str(merged.get("mention_text_file") or "")
                    if _tf and _ch and re.match(r"^[0-9]+\.[0-9]+$", _ts):
                        _msgs = self._fetch_thread_messages(_ch, _ts)
                        if _msgs:
                            self._store_thread_snapshot(_tf, _msgs)
            # Advance to next phase or schedule hot restart after final.
            # Only act when the phase actually succeeded (landed in finished).
            if destination == "finished_tasks":
                if not self.maintenance.is_final_phase(maint_phase):
                    if not self.maintenance.advance_phase(key):
                        self.log_line(f"maintenance_advance_failed key={key} phase={maint_phase}")
                else:
                    self._auto_commit_system_files()
                    self._restart_requested = True
                    self.log_line("hot_restart_scheduled reason=maintenance_complete")

        # FIX-022: session fields preserved on done tasks so reopened tasks
        # can resume their prior codex session when prompt hash matches.

        # Clean up per-task outcome file after reconciliation.
        per_task_outcome = self._outcome_path_for_task(key)
        if per_task_outcome.exists():
            try:
                per_task_outcome.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Tribune post-dispatch review helpers
    # ------------------------------------------------------------------

    def _tribune_review_cycle(
        self, key: str, task: Dict[str, Any], draft_path: str
    ) -> Tuple[str, str]:
        """Run Tribune review on a worker's draft.

        Returns ``(verdict, feedback)`` where verdict is one of
        ``"approved"``, ``"revision_requested"``, or ``"error"``.
        Fails open: errors/timeouts return ``("approved", "")``.
        """
        # Load tribune review template
        tpl = self.cfg.tribune_review_template
        if tpl.exists():
            template = tpl.read_text(encoding="utf-8")
        else:
            self.log_line(
                f"tribune_template_missing key={key} path={tpl}"
            )
            return ("approved", "")

        # Read the draft
        try:
            draft_content = Path(draft_path).read_text(encoding="utf-8")
        except Exception as exc:
            self.log_line(f"tribune_draft_read_error key={key} error={exc}")
            return ("approved", "")

        # Build thread context
        mention_text_file = str(task.get("mention_text_file") or "")
        thread_context = ""
        if mention_text_file:
            thread_context = self.read_task_text_for_prompt(mention_text_file)

        outcome_path = self._outcome_path_for_task(f"{key}.tribune")
        prompt = (
            f"{template}\n\n"
            f"## Original Task Thread\n\n{thread_context}\n\n"
            f"## Worker's Draft Response\n\n{draft_content}\n\n"
            f"Write your review outcome to `{outcome_path}`:\n"
            f"```json\n"
            f'{{\n'
            f'  "mention_ts": "{key}",\n'
            f'  "thread_ts": "{task.get("thread_ts", "")}",\n'
            f'  "status": "done",\n'
            f'  "summary": "<one-line summary of your review>",\n'
            f'  "completion_confidence": "high",\n'
            f'  "requires_human_feedback": false,\n'
            f'  "tribune_verdict": "approved | revision_requested",\n'
            f'  "tribune_feedback": "<specific feedback if revision requested>"\n'
            f'}}\n'
            f"```\n"
        )

        cmd = list(self.cfg.tribune_cmd)
        if not cmd:
            self.log_line(f"tribune_review_skipped key={key} reason=command_disabled")
            return ("approved", "")
        timeout_sec = self.cfg.session_minutes * 60

        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["AGENT_WORKER"] = "1"
        if cmd and "gemini" in cmd[0]:
            env["GEMINI_SANDBOX"] = "false"

        # Model fallback chain: try primary cmd, then each fallback model.
        # Only apply model swapping when cmd is a Gemini CLI invocation.
        is_gemini = bool(cmd and "gemini" in cmd[0])
        fallback_models = list(self.cfg.tribune_fallback_models) if is_gemini else []
        models_to_try: list[str | None] = [None] + fallback_models
        proc = None
        for fallback_model in models_to_try:
            run_cmd = list(cmd)
            if fallback_model is not None:
                run_cmd = _swap_model_in_cmd(run_cmd, fallback_model)
                self.log_line(f"tribune_fallback key={key} model={fallback_model}")
            model_label = fallback_model or _extract_model(cmd)
            self.log_line(f"tribune_dispatch key={key} model={model_label}")
            try:
                proc = subprocess.run(
                    run_cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=timeout_sec,
                    check=False,
                    env=env,
                )
                if proc.returncode != 0:
                    stderr = proc.stderr or ""
                    if CAPACITY_PATTERN.search(stderr):
                        self.log_line(
                            f"tribune_capacity_error key={key} model={model_label} exit={proc.returncode}"
                        )
                        continue  # try next model in fallback chain
                    self.log_line(f"tribune_review_failed key={key} model={model_label} exit={proc.returncode}")
                    return ("approved", "")
                break  # success
            except subprocess.TimeoutExpired:
                self.log_line(f"tribune_review_timeout key={key} model={model_label}")
                return ("approved", "")
        else:
            # All models exhausted by capacity errors
            self.log_line(f"tribune_all_models_exhausted key={key}")
            return ("approved", "")

        # Read Tribune outcome
        if outcome_path.exists():
            try:
                outcome = json.loads(outcome_path.read_text(encoding="utf-8"), strict=False)
                verdict = str(outcome.get("tribune_verdict") or "approved")
                feedback = str(outcome.get("tribune_feedback") or "")
                outcome_path.unlink(missing_ok=True)
                return (verdict, feedback)
            except Exception as exc:
                self.log_line(f"tribune_outcome_parse_error key={key} error={exc}")

        return ("approved", "")

    def _post_slack_draft(self, task: Dict[str, Any], draft_path: str) -> bool:
        """Post the approved draft to Slack and clean up the draft file."""
        if not self.slack_token:
            self.log_line("tribune_post_skip reason=no_slack_token")
            return False

        channel_id = str(task.get("channel_id") or "")
        thread_ts = str(task.get("thread_ts") or "")
        if not channel_id or not thread_ts:
            self.log_line("tribune_post_skip reason=missing_channel_or_thread")
            return False

        try:
            draft_text = Path(draft_path).read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            self.log_line(f"tribune_draft_missing path={draft_path}")
            return False

        if not draft_text:
            self.log_line("tribune_post_skip reason=empty_draft — task finishes without Slack delivery")
            return False

        try:
            self.slack_api_post("chat.postMessage", {
                "channel": channel_id,
                "thread_ts": thread_ts,
                "text": draft_text,
            })
            self.log_line(f"tribune_draft_posted channel={channel_id} thread={thread_ts}")
        except Exception as exc:
            self.log_line(f"tribune_post_error error={exc}")
            return False

        try:
            Path(draft_path).unlink()
        except OSError:
            pass

        return True

    def _post_maintenance_tribune_summary(
        self, task: Dict[str, Any], summary: str
    ) -> bool:
        """Post Tribune maintenance summary to Slack (FIX-010 #4).

        Tribune has read-only Slack access, so the supervisor posts on its
        behalf.  Posts as a thread reply when ``thread_ts`` is a real Slack
        timestamp (set by phase 0 reflect), otherwise as a top-level message.
        """
        if not self.slack_token:
            self.log_line("tribune_maint_summary_skip reason=no_slack_token")
            return False

        channel_id = str(task.get("channel_id") or "")
        if not channel_id:
            self.log_line("tribune_maint_summary_skip reason=missing_channel")
            return False

        thread_ts = str(task.get("thread_ts") or "")
        is_real_thread = bool(re.match(r"^[0-9]+\.[0-9]+$", thread_ts))

        # Truncate to stay within Slack's 4000-char message limit.
        max_len = 3900
        if len(summary) > max_len:
            summary = summary[:max_len] + "\n\n_(truncated — full review in supervisor logs)_"

        payload: Dict[str, Any] = {"channel": channel_id, "text": summary}
        if is_real_thread:
            payload["thread_ts"] = thread_ts

        try:
            self.slack_api_post("chat.postMessage", payload)
            mode = f"thread={thread_ts}" if is_real_thread else "top_level"
            self.log_line(f"tribune_maint_summary_posted channel={channel_id} {mode}")
        except Exception as exc:
            self.log_line(f"tribune_maint_summary_post_failed error={exc}")
            return False

        return True

    def _resolve_draft_path(self, key: str) -> str:
        """Find the draft file for a task. Checks slot-specific and serial paths."""
        # Parallel: check slot-specific paths
        if hasattr(self, "_parallel_slots"):
            for slot in self._parallel_slots:
                if slot.task_key == key:
                    path = slot.dispatch_task_file.parent / f"worker-{slot.slot_id}.slack_draft.md"
                    if path.exists():
                        return str(path)

        # Serial: task-bound draft path only (no legacy shared path fallback
        # to prevent stale drafts from being picked up by unrelated tasks)
        serial_path = self.cfg.dispatch_dir / f"slack_draft.{key}.md"
        if serial_path.exists():
            return str(serial_path)

        return ""

    def _write_project_to_task_json(self, task_file: str, projects: List[str]) -> None:
        """Write the project list into the task JSON file."""
        try:
            path = Path(task_file)
            if not path.exists():
                return
            data = json.loads(path.read_text(encoding="utf-8"))
            data["project"] = projects
            self.write_task_json(str(path), data)
        except Exception as exc:
            self.log_line(f"project_write_failed file={task_file} error={type(exc).__name__}: {exc}")

    def _sync_task_to_project(self, task_file: str, project_slug: str) -> None:
        """Sync a single task's conversation into its project JSON."""
        try:
            subprocess.run(
                [sys.executable, "scripts/assemble_project_jsons.py",
                 "--sync", task_file, "--project", project_slug,
                 "--outdir", str(self.cfg.projects_dir)],
                timeout=30,
                capture_output=True,
            )
        except Exception as exc:
            self.log_line(f"project_sync_failed file={task_file} project={project_slug} error={type(exc).__name__}: {exc}")

    def _sync_consult_to_project(self, task_key: str, project_slug: str) -> None:
        """Append scoped consult records from a task's history into the project's consult JSONL."""
        SCOPED_FIELDS = ("ts", "task_id", "chat_id", "turn", "mode", "prompt", "response", "completed")
        try:
            src = self.cfg.consult_history_dir / f"{task_key}.jsonl"
            if not src.exists():
                return
            dst = self.cfg.projects_dir / f"{project_slug}.consult.jsonl"
            dst.parent.mkdir(parents=True, exist_ok=True)

            # Read existing task_id:ts pairs to avoid duplicates on re-dispatch
            existing_keys: set = set()
            if dst.exists():
                with open(dst, "r", encoding="utf-8") as f_existing:
                    for line in f_existing:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                            existing_keys.add(f"{rec.get('task_id', '')}:{rec.get('ts', '')}")
                        except (json.JSONDecodeError, ValueError):
                            continue

            # Read source and extract scoped fields
            new_records = []
            with open(src, "r", encoding="utf-8") as f_in:
                for line in f_in:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        key = f"{rec.get('task_id', '')}:{rec.get('ts', '')}"
                        if key in existing_keys:
                            continue
                        scoped = {k: rec.get(k, "" if k != "completed" else False) for k in SCOPED_FIELDS}
                        new_records.append(scoped)
                    except json.JSONDecodeError:
                        continue

            if new_records:
                with open(dst, "a", encoding="utf-8") as f_out:
                    for rec in new_records:
                        f_out.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:
            self.log_line(
                f"consult_project_sync_failed task={task_key} "
                f"project={project_slug} error={type(exc).__name__}: {exc}"
            )

    def _has_dispatchable_tasks(self) -> bool:
        """Check if there are tasks ready to dispatch (queued, active, or non-waiting incomplete)."""
        try:
            now = time.time()
            with self.state_lock():
                state = self.load_state()
            # Only unclaimed active tasks are dispatchable (claimed ones are
            # already assigned to a slot or stale from a previous instance).
            active = state.get("active_tasks") or {}
            if any(not (v or {}).get("claimed_by") for v in active.values()):
                return True
            incomplete = state.get("incomplete_tasks") or {}
            if any(
                str((v or {}).get("status") or "in_progress") != "waiting_human"
                and float((v or {}).get("loop_next_dispatch_after") or "0") <= now
                for v in incomplete.values()
            ):
                return True
            if state.get("queued_tasks"):
                return True
        except Exception as exc:
            self.log_line(f"dispatchable_check_error error={type(exc).__name__}: {exc}")
        return False

    def session_sleep_policy(self, session_exit: int) -> None:
        if session_exit != 0:
            sleep_for = self.cfg.failure_sleep_sec
            pending = False
            sleep_status = "sleeping_after_failure"
        elif Path(".agent/runtime/pending_decision.json").exists():
            if self.was_pending:
                self.pending_backoff_sec = min(
                    self.cfg.pending_check_max,
                    self.pending_backoff_sec * self.cfg.pending_check_multiplier,
                )
            else:
                self.pending_backoff_sec = self.cfg.pending_check_initial
            sleep_for = self.pending_backoff_sec
            pending = True
            sleep_status = "sleeping_pending"
        elif self._has_dispatchable_tasks():
            sleep_for = 0
            pending = False
            sleep_status = "draining_queue"
            self.pending_backoff_sec = self.cfg.pending_check_initial
        else:
            sleep_for = self.cfg.sleep_normal
            pending = False
            sleep_status = "sleeping"
            self.pending_backoff_sec = self.cfg.pending_check_initial

        # Cap to poll_interval for fast mention detection, but preserve
        # failure backoff to avoid rapid retries of broken workers.
        if sleep_status != "sleeping_after_failure":
            sleep_for = min(sleep_for, self.cfg.poll_interval)
        self.was_pending = pending
        self.log_line(
            f"session_end loop={self.loop_count} exit_code={session_exit} failure_kind={self.last_failure_kind} "
            f"pending_decision={str(pending).lower()} next_sleep_sec={sleep_for} "
            f"pending_backoff_sec={self.pending_backoff_sec} transient_retry_attempt={self.last_transient_retry_attempt}"
        )
        self.write_heartbeat(
            sleep_status,
            session_exit,
            pending,
            sleep_for,
            self.last_failure_kind,
            self.last_transient_retry_attempt,
        )

        if self.cfg.run_once:
            return
        self._interruptible_sleep(sleep_for)

    def remove_outcome_files(self) -> None:
        if self.cfg.dispatch_outcome_file.exists():
            self.cfg.dispatch_outcome_file.unlink()
        fallback = Path(".agent/runtime/dispatch/outcome.json")
        if self.cfg.dispatch_outcome_file != fallback and fallback.exists():
            fallback.unlink()

    def _auto_commit_system_files(self) -> None:
        """Stage and commit any uncommitted system files so worktree workers see them."""
        cwd = str(self.cfg.repo_root)
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--"] + SYSTEM_FILE_PATHS,
                capture_output=True, text=True, timeout=10, cwd=cwd,
            )
            if not result.stdout.strip():
                return
            subprocess.run(
                ["git", "add", "--"] + SYSTEM_FILE_PATHS,
                capture_output=True, text=True, timeout=10, cwd=cwd,
            )
            commit = subprocess.run(
                ["git", "commit", "-m", "[maintenance] commit uncommitted system changes"],
                capture_output=True, text=True, timeout=10, cwd=cwd,
            )
            if commit.returncode == 0:
                self.log_line("auto_commit_system_files committed")
            else:
                self.log_line(f"auto_commit_system_files noop msg={commit.stdout.strip()}")
        except Exception as exc:
            self.log_line(f"auto_commit_system_files error={exc}")

    def run(self) -> int:
        self.log_line(
            "runner_start supervisor=true "
            f"worker_id={self.cfg.worker_id} session_minutes={self.cfg.session_minutes} "
            f"sleep_normal={self.cfg.sleep_normal} poll_interval={self.cfg.poll_interval} "
            f"waiting_refresh_interval={self.cfg.waiting_refresh_interval} "
            f"pending_initial={self.cfg.pending_check_initial} "
            f"pending_multiplier={self.cfg.pending_check_multiplier} pending_max={self.cfg.pending_check_max} "
            f"max_transient_retries={self.cfg.max_transient_retries} mention_poll_limit={self.cfg.mention_poll_limit} "
            f"mention_max_pages={self.cfg.mention_max_pages} reflect_interval_sec={self.cfg.reflect_interval_sec} "
            f"waiting_human_reply_limit={self.cfg.waiting_human_reply_limit} "
            f"max_incomplete_retention={self.cfg.max_incomplete_retention} "
            f"reflect_template={self.cfg.reflect_template} "
            f"developer_review_template={self.cfg.developer_review_template} "
            f"completion_gate={self.cfg.completion_gate} "
            f"max_concurrent_workers={self.cfg.max_concurrent_workers}"
        )
        self.write_heartbeat("starting", 0, False, 0, self.last_failure_kind, 0)

        if self.cfg.max_concurrent_workers >= 2:
            return self._run_parallel()
        return self._run_serial()

    def _run_serial(self) -> int:
        """Original serial dispatch loop (MAX_CONCURRENT_WORKERS=1)."""
        self._recover_stale_active_tasks()
        self._recover_lost_shell_jobs()
        while True:
            if self._restart_requested:
                self._exec_restart()

            self.loop_count += 1
            self.last_transient_retry_attempt = 0
            self.log_line(f"session_start loop={self.loop_count}")

            session_exit = 0
            self.ensure_state_schema()

            now = time.monotonic()
            if now - self._last_poll_ts >= self.cfg.poll_interval:
                if not self.poll_mentions_and_enqueue():
                    session_exit = 1
                    self.last_failure_kind = "mention_poll_error"
                else:
                    self._last_poll_ts = now

            if session_exit == 0:
                if self._maintenance_requested:
                    self._maintenance_requested = False
                    self.maintenance.enqueue_now()
                self.maintenance.enqueue_if_due()
                self.prune_finished_tasks()
                self.prune_stale_waiting_human_tasks()
                now_wh = time.monotonic()
                if self._waiting_refresh_due(now_wh):
                    self.refresh_waiting_human_tasks()
                    self._last_waiting_refresh_ts = now_wh

            self.remove_outcome_files()
            self._poll_shell_jobs()
            self._process_job_wakeups()

            claim_result = self.select_and_claim() if session_exit == 0 else None
            if claim_result:
                task_key, _task_type = claim_result
                self._check_and_execute_continuation()
                self.refresh_dispatch_thread_context()
                # AGENT-025: stamp prompt hash before rendering
                if self.cfg.session_resume_enabled:
                    dispatch = self.read_json(self.cfg.dispatch_task_file, {})
                    dispatch["dispatch_prompt_hash"] = system_prompt_hash()
                    self.atomic_write_json(self.cfg.dispatch_task_file, dispatch)
                self.render_runtime_prompt()
                session_exit = self.run_worker_with_retries()
                # AGENT-025: capture session ID from worker output
                captured_session_id = None
                dispatch_hash = None
                if self.cfg.session_resume_enabled and session_exit == 0:
                    try:
                        log_content = self.cfg.last_session_log.read_text(encoding="utf-8")
                        captured_session_id = capture_codex_session_id(log_content)
                    except OSError:
                        pass
                    dispatch = self.read_json(self.cfg.dispatch_task_file, {})
                    dispatch_hash = str(dispatch.get("dispatch_prompt_hash") or "")
                self.reconcile_task_after_run(
                    task_key, session_exit,
                    captured_session_id=captured_session_id,
                    dispatch_prompt_hash=dispatch_hash,
                )
                if self._restart_requested:
                    self._exec_restart()
            elif session_exit == 0:
                self.cfg.last_session_log.write_text(f"[{timestamp_utc()}] idle_no_task\n", encoding="utf-8")
                self.log_line(f"session_idle loop={self.loop_count} reason=no_dispatchable_task")
                self.last_failure_kind = "none"

            self.session_sleep_policy(session_exit)

            if self.cfg.run_once:
                break

        return 0

    # ------------------------------------------------------------------
    # Parallel dispatch loop (MAX_CONCURRENT_WORKERS >= 2)
    # ------------------------------------------------------------------

    def _run_parallel(self) -> int:
        """Dispatch up to N workers concurrently using git worktrees."""
        from .worker_slot import WorkerSlot

        repo_root = Path.cwd()
        n = self.cfg.max_concurrent_workers
        slots = [
            WorkerSlot(
                slot_id=i,
                repo_root=repo_root,
                dispatch_dir=self.cfg.dispatch_dir,
                outcomes_dir=self.cfg.outcomes_dir,
                worktree_dir=self.cfg.worktree_dir,
                log_fn=self.log_line,
            )
            for i in range(n)
        ]

        # Initialize worktrees
        for slot in slots:
            try:
                slot.setup_worktree()
            except Exception as exc:
                self.log_line(f"worktree_init_failed slot={slot.slot_id} error={exc}")

        # Startup recovery: unclaim active tasks left over from a previous
        # supervisor instance.  No slots are running yet, so any claimed_by
        # values are stale.  Unclaiming lets select_and_claim re-dispatch them.
        self._recover_stale_active_tasks()
        self._recover_lost_shell_jobs()

        # Store slots reference for _dispatch_to_slot heartbeat writes
        self._parallel_slots = slots

        self.log_line(f"parallel_start slots={n}")

        while True:
            # 1. Restart: drain all workers first
            if self._restart_requested:
                for slot in slots:
                    if slot.is_busy or slot.is_done:
                        self.log_line(f"restart_drain slot={slot.slot_id}")
                        self._reconcile_slot(slot, repo_root)
                self._exec_restart()

            self.loop_count += 1
            self.ensure_state_schema()

            # 2. Poll for mentions (rate-limited)
            now = time.monotonic()
            if now - self._last_poll_ts >= self.cfg.poll_interval:
                if not self.poll_mentions_and_enqueue():
                    self.last_failure_kind = "mention_poll_error"
                else:
                    self._last_poll_ts = now

            # 3. Housekeeping
            if self._maintenance_requested:
                self._maintenance_requested = False
                self.maintenance.enqueue_now()
            self.maintenance.enqueue_if_due()
            self.prune_finished_tasks()
            self.prune_stale_waiting_human_tasks()
            now_wh = time.monotonic()
            if self._waiting_refresh_due(now_wh):
                self.refresh_waiting_human_tasks()
                self._last_waiting_refresh_ts = now_wh

            # 4. Reconcile completed workers
            for slot in slots:
                if slot.is_done:
                    self._reconcile_slot(slot, repo_root)

            # 4b. Watchdog: check busy slots for MCP startup failures
            self._watchdog_check_slots(slots)

            # 4c. Poll background shell jobs for completion
            self._poll_shell_jobs()

            # 4d. Job wakeups: expire leases, reactivate tasks
            self._process_job_wakeups()

            # 5. Maintenance exclusivity gate
            next_is_maintenance = self._peek_next_is_maintenance()
            all_idle = all(slot.is_idle for slot in slots)

            if next_is_maintenance and all_idle:
                self._dispatch_maintenance_serial()
            elif not next_is_maintenance:
                # 6. Fill free slots with tasks
                for slot in slots:
                    if slot.is_idle:
                        result = self.select_and_claim(
                            worker_slot=slot.slot_id,
                            dispatch_task_file=slot.dispatch_task_file,
                        )
                        if result:
                            task_key, task_type = result
                            if self._is_serial_dispatch_type(task_type):
                                # Serial-dispatch task selected but shouldn't run in a slot;
                                # this shouldn't happen since _peek_next_is_maintenance
                                # guards above, but handle defensively.
                                # Unclaim the task so it can be dispatched correctly.
                                self._unclaim_task(task_key)
                                self.log_line(f"serial_task_skipped_in_slot slot={slot.slot_id} type={task_type}")
                                break  # Stop filling — needs serial dispatch
                            self._dispatch_to_slot(slot, task_key, task_type)
                        else:
                            break  # No more tasks, stop trying to fill

            # 7. Sleep
            self._parallel_sleep_policy(slots)

            if self.cfg.run_once and all(s.is_idle for s in slots):
                break

        return 0

    def _dispatch_to_slot(self, slot, task_key: str, task_type: str) -> None:
        """Prepare and launch a worker in the given slot."""
        # Reset worktree to latest main
        try:
            slot.setup_worktree()
        except Exception as exc:
            self.log_line(f"worktree_reset_failed slot={slot.slot_id} error={exc}")
            self._unclaim_task(task_key)
            return

        # Inject per-task consult env vars into the copied .codex/config.toml
        slot.inject_task_env(task_key)

        # AGENT-057: execute pending thread continuation before refresh
        self._check_and_execute_continuation(dispatch_task_file=slot.dispatch_task_file)
        # Refresh thread context (reads from slot's dispatch_task_file)
        self._refresh_slot_thread_context(slot)

        # AGENT-025: stamp prompt hash before rendering
        if self.cfg.session_resume_enabled:
            dispatch = self.read_json(slot.dispatch_task_file, {})
            dispatch["dispatch_prompt_hash"] = system_prompt_hash()
            self.atomic_write_json(slot.dispatch_task_file, dispatch)

        # Determine command and write-protection level
        is_privileged = False
        is_resume = False
        if self.maintenance.is_maintenance_task(task_type):
            dispatch = self.read_json(slot.dispatch_task_file, {})
            phase = self.maintenance.get_phase(dispatch)
            cmd = self.maintenance.get_worker_cmd(phase)
            is_privileged = self.maintenance.is_dev_review_phase(phase)
        elif task_type == "development":
            cmd = list(self.cfg.dev_review_cmd)
            is_privileged = True
        else:
            # AGENT-025: session resume check
            dispatch = self.read_json(slot.dispatch_task_file, {})
            resume_cmd = (
                self._build_resume_cmd(dispatch)
                if self._should_resume_session(dispatch)
                else []
            )
            if resume_cmd:
                cmd = resume_cmd
                is_resume = True
            else:
                cmd = self._build_fresh_worker_cmd()

        # Render prompt (resume or full)
        if is_resume:
            self._render_slot_resume_prompt(slot, task_key, dispatch, task_type)
        else:
            self._render_slot_prompt(slot, task_key, task_type)

        prompt = slot.dispatch_prompt_file.read_text(encoding="utf-8")
        timeout_sec = self.cfg.session_minutes * 60

        # Delete stale outcome before dispatch (same rationale as serial path).
        self._outcome_path_for_task(task_key).unlink(missing_ok=True)

        slot.start(cmd, prompt, task_key, task_type, timeout_sec, is_privileged=is_privileged)

        # Write heartbeat immediately so the dashboard reflects the new worker.
        self._write_parallel_heartbeat(self._parallel_slots)

    def _write_parallel_heartbeat(
        self,
        slots,
        status_override: Optional[str] = None,
        sleep_for: Optional[int] = None,
    ) -> None:
        """Write heartbeat reflecting current parallel worker state.

        Called both from _dispatch_to_slot (immediate heartbeat after worker
        launch) and from _parallel_sleep_policy (end of each loop iteration).
        """
        any_busy = any(s.is_busy or s.is_done for s in slots)
        status = status_override or ("workers_active" if any_busy else "sleeping")

        if Path(".agent/runtime/pending_decision.json").exists():
            status = "sleeping_pending"

        active_workers = []
        for s in slots:
            if s.is_busy or s.is_done:
                active_workers.append({
                    "slot": s.slot_id,
                    "task": s.task_key or "",
                    "started_at": int(s.started_at or 0),
                    "elapsed_sec": int(s.elapsed_sec),
                })

        next_sleep = sleep_for if sleep_for is not None else self.cfg.poll_interval
        self.write_heartbeat(
            status, 0, False, next_sleep, self.last_failure_kind, 0,
            active_workers=active_workers if active_workers else None,
        )

    def _watchdog_check_slots(self, slots) -> None:
        """Check busy slots for MCP startup failures and session log staleness."""
        for slot in slots:
            if not slot.is_busy:
                continue
            mcp_reason = slot.check_mcp_startup(
                grace_sec=self.cfg.mcp_startup_check_sec
            )
            if mcp_reason:
                self.log_line(
                    f"watchdog_mcp_fail slot={slot.slot_id} task={slot.task_key} "
                    f"reason={mcp_reason}"
                )
                slot.kill_worker(mcp_reason)
                continue
            # AGENT-028 + AGENT-040: detect hung workers via session log
            # staleness, with tool-aware timeout for long MCP calls.
            stale_reason = slot.check_session_log_stale(
                idle_timeout_sec=self.cfg.worker_idle_timeout_sec,
                tool_timeout_sec=self.cfg.worker_tool_timeout_sec,
            )
            if stale_reason:
                kill_kind = (
                    "watchdog_tool_kill"
                    if stale_reason.startswith("tool_call_stale")
                    else "watchdog_idle_kill"
                )
                self.log_line(
                    f"{kill_kind} slot={slot.slot_id} task={slot.task_key} "
                    f"reason={stale_reason}"
                )
                slot.kill_worker(stale_reason)

    def _refresh_slot_thread_context(self, slot) -> None:
        """Like refresh_dispatch_thread_context but uses slot's dispatch file."""
        dispatch = self.read_json(slot.dispatch_task_file, {})
        channel_id = str(dispatch.get("channel_id") or "")
        thread_ts = str(dispatch.get("thread_ts") or "")
        mention_text_file = str(dispatch.get("mention_text_file") or "")
        if not channel_id or not mention_text_file:
            return
        if not re.match(r"^[0-9]+\.[0-9]+$", thread_ts):
            return
        if not self.slack_token:
            return

        thread_msgs = self._fetch_thread_messages(channel_id, thread_ts)
        if not thread_msgs:
            return

        self._store_thread_snapshot(mention_text_file, thread_msgs)
        dispatch["mention_text"] = self.read_task_text_for_prompt(mention_text_file)
        self.atomic_write_json(slot.dispatch_task_file, dispatch)

    def _render_slot_resume_prompt(
        self, slot, task_key: str, dispatch_json: Dict[str, Any],
        task_type: str = "",
    ) -> None:
        """Render a lightweight resume prompt to the slot's prompt file."""
        # AGENT-046: build parallel slot context (merge + draft)
        ctx: Dict[str, str] = {
            "repo_root": str(slot.repo_root),
            "branch_name": slot.branch_name,
        }
        if (
            self.cfg.tribune_max_review_rounds >= 1
            and not self.maintenance.is_maintenance_task(task_type)
        ):
            ctx["draft_path"] = str(
                slot.dispatch_task_file.parent
                / f"worker-{slot.slot_id}.slack_draft.md"
            )
        prompt = self._render_resume_prompt(
            dispatch_json, task_key, slot_context=ctx,
        )
        slot.dispatch_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        slot.dispatch_prompt_file.write_text(prompt, encoding="utf-8")

    def _render_slot_prompt(self, slot, task_key: str, task_type: str) -> None:
        """Render the runtime prompt to the slot's prompt file."""
        dispatch_json = self.read_json(slot.dispatch_task_file, {})
        # Inject async job wakeup context (dispatch_token + pending wakeups)
        self._inject_wakeup_context(dispatch_json, task_key)
        dispatch_str = json.dumps(dispatch_json, ensure_ascii=False, indent=2)  # full, for maintenance/dev

        # Non-worker maintenance phases (developer, tribune) use standalone prompts
        if self.maintenance.is_maintenance_task(task_type):
            phase = self.maintenance.get_phase(dispatch_json)
            role = self.maintenance.phase_role(phase)
            if role in ("developer", "tribune"):
                outcome_path = str(self._outcome_path_for_task(task_key))
                review_template = self.maintenance.load_prompt(phase)
                thread_context = str(dispatch_json.get("mention_text") or "")
                prompt = (
                    f"{review_template}\n\n"
                    f"{thread_context}\n\n"
                    f"Your task dispatch context:\n"
                    f"```json\n{dispatch_str}\n```\n\n"
                    f"Write the outcome file to `{outcome_path}` before exit:\n"
                    f"```json\n"
                    f'{{\n'
                    f'  "mention_ts": "<task_id from dispatch>",\n'
                    f'  "thread_ts": "<thread_ts from dispatch — reply in this existing thread>",\n'
                    f'  "status": "done | waiting_human",\n'
                    f'  "summary": "<short plain-text summary>",\n'
                    f'  "completion_confidence": "high | medium | low",\n'
                    f'  "requires_human_feedback": true | false,\n'
                    f'  "error": "<optional>"\n'
                    f'}}\n'
                    f"```\n"
                )
                slot.dispatch_prompt_file.parent.mkdir(parents=True, exist_ok=True)
                slot.dispatch_prompt_file.write_text(prompt, encoding="utf-8")
                return

        # Development tasks: use the prompt stored in the task's message data.
        if task_type == "development":
            task_data = self.read_json(
                Path(dispatch_json.get("mention_text_file") or str(slot.dispatch_task_file)), {}
            )
            messages = task_data.get("messages") or []
            prompt_text = messages[0].get("text", "") if messages else ""
            slot.dispatch_prompt_file.parent.mkdir(parents=True, exist_ok=True)
            slot.dispatch_prompt_file.write_text(prompt_text + "\n", encoding="utf-8")
            return

        outcome_path = str(self._outcome_path_for_task(task_key)) if task_key else str(self.cfg.dispatch_outcome_file)

        # Build thread context as a chat transcript (separate from task JSON)
        mention_text_file = str(dispatch_json.get("mention_text_file") or "")
        original_request_str, thread_context_str = self._render_thread_context(mention_text_file)

        # Build a cleaned dispatch JSON for the prompt (strip internal fields)
        prompt_json = {
            k: v for k, v in dispatch_json.items()
            if k not in self._DISPATCH_INTERNAL_FIELDS
        }
        prompt_dispatch_str = json.dumps(prompt_json, ensure_ascii=False, indent=2)

        lines = self.cfg.session_template.read_text(encoding="utf-8").splitlines()
        memory_context_str = ""
        if any("{{SESSION_MEMORY_CONTEXT}}" in line for line in lines):
            memory_context_str = self.build_session_memory_context()
        loop_context_str = self._build_loop_context(dispatch_json)
        user_id = str((dispatch_json.get("source") or {}).get("user_id") or "")
        user_profile_body = self.read_user_profile(user_id)
        out: List[str] = []
        for line in lines:
            if line == "{{ORIGINAL_REQUEST}}":
                if original_request_str:
                    out.extend(original_request_str.splitlines())
                continue
            if line == "{{THREAD_CONTEXT}}":
                if thread_context_str:
                    out.extend(thread_context_str.splitlines())
                continue
            if line == "{{DISPATCH_TASK_JSON}}":
                out.append(prompt_dispatch_str)
                continue
            if line == "{{SESSION_MEMORY_CONTEXT}}":
                if memory_context_str:
                    out.extend(memory_context_str.splitlines())
                continue
            if line == "{{LOOP_CONTEXT}}":
                if loop_context_str:
                    out.extend(loop_context_str.splitlines())
                continue
            if line == "{{USER_PROFILE}}":
                if user_profile_body:
                    out.append("About your collaborator:")
                    out.extend(user_profile_body.splitlines())
                else:
                    out.append("No prior interaction history with this user.")
                continue
            if line == "{{MERGE_INSTRUCTIONS}}":
                merge_path = self.cfg.session_template.parent / "merge_instructions.md"
                if merge_path.exists():
                    merge_tpl = merge_path.read_text(encoding="utf-8").rstrip("\n")
                    merge_block = merge_tpl.replace("{{REPO_ROOT}}", str(slot.repo_root)).replace("{{BRANCH_NAME}}", slot.branch_name)
                    out.extend(merge_block.splitlines())
                continue
            if line == "{{TRIBUNE_DRAFT_INSTRUCTIONS}}":
                if (
                    self.cfg.tribune_max_review_rounds >= 1
                    and not self.maintenance.is_maintenance_task(task_type)
                ):
                    draft_path = str(slot.dispatch_task_file.parent / f"worker-{slot.slot_id}.slack_draft.md")
                    out.append("")
                    out.append("## Tribune Review (Active)")
                    out.append("")
                    out.append(
                        "Your final response will be reviewed by an independent quality "
                        "reviewer (Tribune) before posting to Slack. Instead of posting "
                        "your final response via Slack MCP, write it to "
                        f"`{draft_path}` as a Markdown file. The supervisor will post it "
                        "after Tribune approval."
                    )
                    out.append("")
                    out.append(
                        "**Important:** Only the *final* response (your concluding answer/"
                        "deliverable) goes into the draft file instead of Slack. "
                        "Set your outcome status normally per AGENTS.md — the supervisor "
                        "will route the draft through Tribune review before posting."
                    )
                    tribune_feedback = dispatch_json.get("tribune_feedback")
                    if tribune_feedback:
                        revision = dispatch_json.get("tribune_revision_count", 0)
                        out.append("")
                        out.append(f"### Tribune Feedback (Revision Round {revision})")
                        out.append("")
                        out.append(
                            "The Tribune reviewed your previous draft and requested revisions:"
                        )
                        out.append("")
                        out.append(str(tribune_feedback))
                        out.append("")
                        out.append("Address this feedback in your revised draft.")
                continue
            line = line.replace("{{SLACK_ID}}", self.resolve_slack_id())
            line = line.replace("{{AGENT_NAME}}", self.cfg.agent_name)
            line = line.replace("{{DISPATCH_OUTCOME_PATH}}", outcome_path)
            out.append(line)

        slot.dispatch_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        slot.dispatch_prompt_file.write_text("\n".join(out) + "\n", encoding="utf-8")

    def _reconcile_slot(self, slot, repo_root: Path) -> None:
        """Collect a finished slot, reconcile the task, merge changes."""
        try:
            exit_code, log_path = slot.collect()
        except RuntimeError:
            slot.reset()
            return

        task_key = slot.task_key or ""
        self.log_line(
            f"slot_reconcile slot={slot.slot_id} task={task_key} exit={exit_code}"
        )

        # Copy session log to last_session_log for visibility
        try:
            log_content = Path(log_path).read_text(encoding="utf-8")
            self.cfg.last_session_log.write_text(log_content, encoding="utf-8")
        except Exception:
            log_content = ""

        # AGENT-025: capture session ID from worker output
        captured_session_id = None
        dispatch_hash = None
        if self.cfg.session_resume_enabled and exit_code == 0 and task_key:
            captured_session_id = capture_codex_session_id(log_content or "")
            dispatch = self.read_json(slot.dispatch_task_file, {})
            dispatch_hash = str(dispatch.get("dispatch_prompt_hash") or "")

        # Handle stop_command kills: skip outcome reading but fall through
        # to merge check so unmerged commits are preserved.  Task state was
        # already updated by the !stop handler (moved to incomplete/waiting_human).
        stop_killed = slot.killed_reason == "stop_command"
        if stop_killed:
            self.log_line(
                f"slot_stop_killed slot={slot.slot_id} task={task_key} exit={exit_code}"
            )
            # Fall through to merge check below.

        # Handle watchdog kills: re-enqueue task with retry tracking
        elif slot.killed_reason and task_key:
            self.log_line(
                f"watchdog_reconcile slot={slot.slot_id} task={task_key} "
                f"reason={slot.killed_reason} exit={exit_code}"
            )
            requeued = self._watchdog_requeue_or_park(
                task_key, slot.killed_reason or "unknown"
            )
            if requeued:
                self.log_line(
                    f"watchdog_requeue slot={slot.slot_id} task={task_key}"
                )
            else:
                self.log_line(
                    f"watchdog_retries_exhausted slot={slot.slot_id} task={task_key}"
                )
            slot.reset()
            return

        # Reconcile using per-task outcome file (skip for stop-killed slots)
        if task_key and not stop_killed:
            outcome_override = slot.outcome_file_for(task_key)
            self.reconcile_task_after_run(
                task_key, exit_code,
                outcome_file_override=outcome_override if outcome_override.exists() else None,
                captured_session_id=captured_session_id,
                captured_slot_id=slot.slot_id,
                dispatch_prompt_hash=dispatch_hash,
            )

        # Safety net: if the worker forgot to merge its branch, attempt a
        # trivial fast-forward first.  If that fails (divergence), dispatch a
        # short-lived worker to resolve the merge and alert the human via Slack.
        #
        # Merge-result tracking (plan 09): explicit result constants so we
        # can enforce task-state downgrades on merge failure.
        from .utils import (
            MERGE_CHECK_ERROR,
            MERGE_FALLBACK_FAILED,
            MERGE_FALLBACK_OK,
            MERGE_FF,
            MERGE_NO_UNMERGED,
        )

        merge_result = MERGE_NO_UNMERGED
        if task_key:
            try:
                log_result = slot._git(
                    ["log", f"{slot._base_branch}..{slot.branch_name}", "--oneline"],
                    cwd=slot.repo_root,
                )
                if log_result.stdout.strip():
                    self.log_line(
                        f"slot_unmerged_commits slot={slot.slot_id} task={task_key}"
                    )
                    # Ensure root checkout is on the base branch before
                    # merging.  The root can be left on a worker-* branch
                    # after a failed worktree recovery or manual intervention.
                    try:
                        cur = slot._git(
                            ["rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=slot.repo_root,
                        ).stdout.strip()
                        if cur != slot._base_branch:
                            self.log_line(
                                f"slot_checkout_base slot={slot.slot_id} "
                                f"from={cur} to={slot._base_branch}"
                            )
                            slot._git(
                                ["checkout", slot._base_branch],
                                cwd=slot.repo_root,
                            )
                    except subprocess.CalledProcessError as exc:
                        self.log_line(
                            f"slot_checkout_base_failed slot={slot.slot_id} "
                            f"error={exc!s:.200}"
                        )
                    # Stash dirty working tree so merge isn't blocked by
                    # unrelated modifications (symlinked .agent/, submodule
                    # pointer drift, other workers' uncommitted changes).
                    stashed = False
                    try:
                        stash_result = slot._git(
                            ["stash", "--include-untracked"],
                            cwd=slot.repo_root,
                        )
                        stashed = "No local changes" not in stash_result.stdout
                    except subprocess.CalledProcessError:
                        pass
                    try:
                        slot._git(["merge", "--ff-only", slot.branch_name],
                                  cwd=slot.repo_root)
                        merge_result = MERGE_FF
                        self.log_line(
                            f"slot_fallback_ff slot={slot.slot_id} "
                            f"task={task_key} success=true"
                        )
                    except subprocess.CalledProcessError:
                        self.log_line(
                            f"slot_fallback_ff slot={slot.slot_id} "
                            f"task={task_key} success=false"
                        )
                        self._fallback_merge_dispatch(slot, task_key, repo_root)
                    finally:
                        if stashed:
                            try:
                                slot._git(["stash", "pop"], cwd=slot.repo_root)
                            except subprocess.CalledProcessError:
                                # Stash pop conflict — drop it, the stashed
                                # changes were transient runtime state.
                                try:
                                    slot._git(["stash", "drop"], cwd=slot.repo_root)
                                except subprocess.CalledProcessError:
                                    pass
                                self.log_line(
                                    f"slot_stash_pop_conflict slot={slot.slot_id} "
                                    f"task={task_key} — dropped stash"
                                )
                    # Re-check only if ff failed and fallback was attempted
                    if merge_result != MERGE_FF:
                        try:
                            verify = slot._git(
                                ["log", f"{slot._base_branch}..{slot.branch_name}", "--oneline"],
                                cwd=slot.repo_root,
                            )
                            if verify.stdout.strip():
                                merge_result = MERGE_FALLBACK_FAILED
                            else:
                                merge_result = MERGE_FALLBACK_OK
                        except Exception:
                            merge_result = MERGE_FALLBACK_FAILED
            except Exception as exc:
                merge_result = MERGE_CHECK_ERROR
                self.log_line(
                    f"slot_merge_check_error slot={slot.slot_id} task={task_key} "
                    f"error={type(exc).__name__}: {exc}"
                )

            self.log_line(
                f"slot_merge_check slot={slot.slot_id} task={task_key} result={merge_result}"
            )

            # Enforce merge-failure state if policy is enabled
            if merge_result in (MERGE_FALLBACK_FAILED, MERGE_CHECK_ERROR):
                self._enforce_merge_blocked_state(
                    task_key, slot.slot_id, slot.branch_name, merge_result
                )

            # FIX-022: session fields preserved — reopened tasks resume sessions.

        slot.reset()

    def _fallback_merge_dispatch(self, slot, task_key: str, repo_root: Path) -> None:
        """Dispatch a short-lived worker to resolve a diverged merge.

        Called when the ff-only fallback fails because the worker's branch
        diverged from the base branch.  Sends a Slack alert and runs a
        merge-only codex session in the slot's worktree.
        """
        # Look up task data for Slack notification
        channel_id = ""
        thread_ts = ""
        with self.state_lock():
            state = self.load_state()
            for bucket in ("finished_tasks", "incomplete_tasks", "active_tasks"):
                task = state.get(bucket, {}).get(task_key)
                if task:
                    channel_id = str(task.get("channel_id") or "")
                    thread_ts = str(task.get("thread_ts") or "")
                    break

        branch = slot.branch_name
        # Log internally only — do not post to user's Slack thread.
        # Internal coordination messages (merge status) must never leak
        # into collaborator-facing threads.
        self.log_line(
            f"slot_fallback_merge slot={slot.slot_id} task={task_key} "
            f"branch={branch} — attempting automatic merge resolution"
        )

        # Load and render merge fallback prompt
        merge_tpl_path = self.cfg.session_template.parent / "merge_fallback.md"
        try:
            merge_prompt = (
                merge_tpl_path.read_text(encoding="utf-8")
                .replace("{{REPO_ROOT}}", str(repo_root))
                .replace("{{BRANCH_NAME}}", branch)
                .replace("{{AGENT_NAME}}", self.cfg.agent_name)
            )
        except FileNotFoundError:
            self.log_line(
                f"slot_fallback_merge slot={slot.slot_id} "
                f"task={task_key} error=merge_fallback.md not found"
            )
            return

        # Clean the worktree's dirty state before running the merge worker.
        # Cannot call setup_worktree() — that resets the branch pointer to the
        # base branch, which would destroy the unmerged commits we need to merge.
        # Instead, discard uncommitted changes and untracked files only.
        worktree_clean = True
        try:
            slot._git(["reset", "--hard", "HEAD"], cwd=slot.worktree_path)
            slot._git(["clean", "-fd"], cwd=slot.worktree_path)
        except Exception as exc:
            worktree_clean = False
            self.log_line(
                f"slot_fallback_merge slot={slot.slot_id} "
                f"task={task_key} error=worktree_clean_failed: {exc}"
            )

        # Run merge-only worker in the slot's worktree (skip if cleanup failed)
        if worktree_clean:
            cmd = list(self.cfg.worker_cmd)
            env = {
                k: v for k, v in os.environ.items() if k != "CLAUDECODE"
            }
            env.update({
                "REPO_ROOT": str(repo_root),
                "WORKER_BRANCH": branch,
                "AGENT_WORKER": "1",
            })
            try:
                subprocess.run(
                    cmd,
                    input=merge_prompt,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=1200,
                    check=False,
                    cwd=str(slot.worktree_path),
                    env=env,
                )
            except subprocess.TimeoutExpired:
                self.log_line(
                    f"slot_fallback_merge slot={slot.slot_id} "
                    f"task={task_key} error=timeout"
                )
            except Exception as exc:
                self.log_line(
                    f"slot_fallback_merge slot={slot.slot_id} "
                    f"task={task_key} error={exc}"
                )

        # Verify merge succeeded
        try:
            verify = slot._git(
                ["log", f"{slot._base_branch}..{branch}", "--oneline"],
                cwd=repo_root,
            )
            merged = not verify.stdout.strip()
        except Exception:
            merged = False

        self.log_line(
            f"slot_fallback_merge slot={slot.slot_id} "
            f"task={task_key} success={'true' if merged else 'false'}"
        )

        if not merged and self.slack_token and channel_id and thread_ts:
            try:
                self.slack_api_post("chat.postMessage", {
                    "channel": channel_id,
                    "thread_ts": thread_ts,
                    "text": (
                        f"Automatic merge failed — manual intervention needed. "
                        f"Branch `{branch}` has unmerged commits."
                    ),
                })
            except Exception:
                pass

    def _enforce_merge_blocked_state(
        self, task_key: str, slot_id: int, branch: str, merge_result: str
    ) -> None:
        """Downgrade a task to waiting_human when merge verification fails.

        When ``merge_failure_blocks_done`` is True, forces the task status
        to ``waiting_human`` so the human can investigate unmerged commits.
        When False (default, annotation-only), logs the event but does not
        change task state.
        """
        enforce = self.cfg.merge_failure_blocks_done
        reason = f"merge_{merge_result} branch={branch} slot={slot_id}"

        if not enforce:
            self.log_line(
                f"slot_merge_block_skipped_policy task={task_key} reason={reason}"
            )
            return

        with self.state_lock():
            state = self.load_state()
            task = None
            src_bucket = ""
            for bucket in ("finished_tasks", "incomplete_tasks", "active_tasks"):
                candidate = (state.get(bucket) or {}).get(task_key)
                if isinstance(candidate, dict):
                    task = candidate
                    src_bucket = bucket
                    break

            if task is None:
                self.log_line(
                    f"slot_merge_block_task_not_found task={task_key} reason={reason}"
                )
                return

            task["status"] = "waiting_human"
            existing_error = str(task.get("last_error") or "")
            task["last_error"] = (
                (existing_error + "; " if existing_error else "") + reason
            )
            task["last_update_ts"] = now_ts()

            if src_bucket != "incomplete_tasks":
                state.setdefault(src_bucket, {}).pop(task_key, None)
                state.setdefault("incomplete_tasks", {})[task_key] = task

            self.save_state(state)

        self.log_line(
            f"slot_merge_block_enforced task={task_key} reason={reason}"
        )

    def _recover_stale_active_tasks(self) -> None:
        """Unclaim active tasks left from a previous supervisor instance.

        Called once at the start of _run_parallel(), before any slots are
        running.  Clears ``claimed_by`` so select_and_claim can re-dispatch.
        """
        with self.state_lock():
            state = self.load_state()
            active = state.get("active_tasks") or {}
            recovered = 0
            for key, task in active.items():
                if isinstance(task, dict) and task.get("claimed_by"):
                    self.log_line(
                        f"stale_active_unclaim task={key} "
                        f"was_claimed_by={task['claimed_by']}"
                    )
                    task["claimed_by"] = None
                    recovered += 1
            if recovered:
                self.save_state(state)
                self.log_line(f"stale_active_recovered count={recovered}")

    # ---- Job wakeup hooks (Plan 31 Phase 2) ----

    def _process_job_wakeups(self) -> int:
        """Expire stale job leases and reactivate tasks with pending wakeups.

        Called before task selection so that tasks with completed async jobs
        become dispatchable.  Returns the number of tasks reactivated.
        """
        # 1. Expire stale leases
        expired = self._job_store.expire_leases()
        for jid in expired:
            self.log_line(f"job_lease_expired job_id={jid}")

        # 2. Scan incomplete (waiting_human) tasks for pending wakeups
        reactivated = 0
        with self.state_lock():
            state = self.load_state()
            waiting = dict(state.get("incomplete_tasks") or {})
            for key, task in waiting.items():
                if not isinstance(task, dict):
                    continue
                if str(task.get("status") or "") != "waiting_human":
                    continue
                pending = self._job_store.pending_wakeups(key)
                if not pending:
                    continue
                # Reactivate: move to active_tasks (unclaimed)
                task["status"] = "in_progress"
                task["claimed_by"] = None
                task["last_update_ts"] = now_ts()
                state.setdefault("active_tasks", {})[key] = task
                state.get("incomplete_tasks", {}).pop(key, None)
                reactivated += 1
                self.log_line(
                    f"job_task_reactivated task={key} "
                    f"pending_jobs={len(pending)}"
                )
            if reactivated:
                self.save_state(state)
        return reactivated

    def _process_job_acks(self, outcome: dict) -> int:
        """Process job acknowledgements from a worker outcome.

        Returns the number of acks successfully applied.
        """
        acks_raw = outcome.get("job_acknowledgements") or []
        if not acks_raw:
            return 0
        applied = 0
        for ack_data in acks_raw:
            try:
                ack = AckRequest(
                    job_id=str(ack_data.get("job_id", "")),
                    handled_seq=int(ack_data.get("handled_seq", 0)),
                    lease_owner=str(ack_data.get("lease_owner", "")),
                )
            except (TypeError, ValueError):
                self.log_line(
                    f"job_ack_invalid data={ack_data!r}"
                )
                continue
            if self._job_store.process_ack(ack):
                applied += 1
                self.log_line(
                    f"job_ack_applied job_id={ack.job_id} "
                    f"seq={ack.handled_seq}"
                )
            else:
                self.log_line(
                    f"job_ack_rejected job_id={ack.job_id} "
                    f"seq={ack.handled_seq} owner={ack.lease_owner}"
                )
        return applied

    def _inject_wakeup_context(self, dispatch_json: dict, task_key: str) -> None:
        """Inject pending job wakeup context into dispatch JSON.

        Adds ``dispatch_token`` and ``pending_job_wakeups`` to the dispatch
        payload so the worker can see and acknowledge async completions.
        """
        import uuid

        dispatch_token = str(uuid.uuid4())
        dispatch_json["dispatch_token"] = dispatch_token

        pending = self._job_store.pending_wakeups(task_key)
        if not pending:
            return

        wakeups = []
        for job in pending:
            events = self._job_store.load_events(job.job_id)
            # Issue a lease so the worker's ack can be validated
            self._job_store.issue_lease(job.job_id, dispatch_token)
            self.log_line(
                f"job_lease_issued job_id={job.job_id} owner={dispatch_token}"
            )
            # Build a compact wakeup summary for the prompt
            from .job_store import pending_material_seq

            pms = pending_material_seq(events, job.acknowledged_material_seq)
            material_event = None
            if pms is not None:
                material_event = next(
                    (e for e in events if e.seq == pms), None
                )
            wakeups.append({
                "job_id": job.job_id,
                "adapter": job.adapter,
                "runtime_state": job.runtime_state,
                "pending_seq": pms,
                "event_kind": material_event.kind if material_event else None,
                "event_summary": material_event.summary if material_event else None,
            })

        dispatch_json["pending_job_wakeups"] = wakeups
        self.log_line(
            f"job_wakeup_injected task={task_key} "
            f"dispatch_token={dispatch_token} jobs={len(wakeups)}"
        )

    # ---- Shell adapter integration (Plan 31 Phase 4) ----

    def _poll_shell_jobs(self) -> list:
        """Poll background shell jobs for completion.

        Called each loop iteration before job wakeup processing so that
        newly completed jobs generate material events before the wakeup
        scan runs.
        """
        finished = self._shell_adapter.poll_all()
        for jid in finished:
            job = self._job_store.load_job(jid)
            self.log_line(
                f"job_poll_finished job_id={jid} "
                f"state={job.runtime_state if job else 'unknown'}"
            )
        return finished

    def _recover_lost_shell_jobs(self) -> None:
        """Scan for jobs marked running that have no live process.

        Called once at supervisor startup to detect jobs orphaned by a
        previous crash or restart.
        """
        lost = self._shell_adapter.recover_lost_jobs()
        if lost:
            self.log_line(f"job_recovery_scan lost={len(lost)} ids={lost}")

    def _unclaim_task(self, task_key: str) -> None:
        """Move a task back from active_tasks to queued_tasks (undo a claim).

        Used when select_and_claim picks a task that can't be dispatched
        (e.g. maintenance selected by a parallel slot).
        """
        with self.state_lock():
            state = self.load_state()
            task = (state.get("active_tasks") or {}).pop(task_key, None)
            if isinstance(task, dict):
                task["status"] = "queued"
                task["claimed_by"] = None
                task["last_update_ts"] = now_ts()
                state.setdefault("queued_tasks", {})[task_key] = task
                self.save_state(state)
                self.log_line(f"task_unclaimed key={task_key}")

    def _park_task_waiting_human(self, task_key: str, error: str = "") -> None:
        """Move a task to incomplete_tasks with waiting_human status.

        Used when watchdog retries are exhausted to prevent infinite
        re-dispatch.  The task will only be re-dispatched when a human
        replies in the Slack thread.
        """
        with self.state_lock():
            state = self.load_state()
            task = (state.get("active_tasks") or {}).pop(task_key, None)
            if isinstance(task, dict):
                task["status"] = "waiting_human"
                task["claimed_by"] = None
                task["last_update_ts"] = now_ts()
                if error:
                    task["last_error"] = error
                state.setdefault("incomplete_tasks", {})[task_key] = task
                self.save_state(state)
                self.log_line(f"task_parked_waiting_human key={task_key} error={error}")

    def _watchdog_requeue_or_park(self, task_key: str, reason: str) -> bool:
        """Atomically check retries and either requeue or park a watchdog-killed task.

        Combines retry checking, counter increment, and task-bucket move
        under a single lock acquisition to prevent TOCTOU races where the
        task could be moved between separate lock/unlock cycles.

        Returns True if task was requeued (retries remain), False if parked
        as waiting_human (retries exhausted or task not found).
        """
        with self.state_lock():
            state = self.load_state()
            task = (state.get("active_tasks") or {}).get(task_key)
            if not isinstance(task, dict):
                self.log_line(
                    f"watchdog_task_not_found key={task_key}"
                )
                return False
            retries = int(task.get("watchdog_retries") or 0)
            if retries < self.cfg.max_watchdog_retries:
                # Retries remain — increment counter and move to queued
                task["watchdog_retries"] = retries + 1
                task["last_error"] = f"watchdog_kill (attempt {retries + 1})"
                (state.get("active_tasks") or {}).pop(task_key, None)
                task["status"] = "queued"
                task["claimed_by"] = None
                task["last_update_ts"] = now_ts()
                state.setdefault("queued_tasks", {})[task_key] = task
                self.save_state(state)
                return True
            else:
                # Retries exhausted — park as waiting_human
                error = f"watchdog_retries_exhausted: {reason}"
                (state.get("active_tasks") or {}).pop(task_key, None)
                task["status"] = "waiting_human"
                task["claimed_by"] = None
                task["last_update_ts"] = now_ts()
                task["last_error"] = error
                state.setdefault("incomplete_tasks", {})[task_key] = task
                self.save_state(state)
                self.log_line(
                    f"task_parked_waiting_human key={task_key} error={error}"
                )
                return False

    def _dispatch_maintenance_serial(self) -> None:
        """Dispatch a serial task (maintenance or development) in the main worktree.

        Uses the existing serial code path: select+claim,
        refresh context, render prompt, subprocess.run (blocking), reconcile.
        """
        claim_result = self.select_and_claim()
        if not claim_result:
            return

        task_key, task_type = claim_result
        if not self._is_serial_dispatch_type(task_type):
            # Not a serial task after all (race); unclaim so the parallel
            # fill cycle can dispatch it to a slot.
            self._unclaim_task(task_key)
            return

        self.log_line(f"maintenance_serial_dispatch task={task_key}")
        self.refresh_dispatch_thread_context()
        self.render_runtime_prompt()
        # Delete stale outcome before dispatch so the reconciler only sees
        # outcomes written by this worker.  Git-tracked or leftover outcome
        # files can reappear after git operations (stash, checkout, merge).
        self._outcome_path_for_task(task_key).unlink(missing_ok=True)
        session_exit = self.run_worker_with_retries()
        self.reconcile_task_after_run(task_key, session_exit)

    def _has_pending_serial_task(self) -> bool:
        """Check if any dispatchable task requires serial dispatch (read-only)."""
        try:
            with self.state_lock():
                state = self.load_state()
            for bucket in ("queued_tasks", "active_tasks", "incomplete_tasks"):
                for v in (state.get(bucket) or {}).values():
                    if not isinstance(v, dict):
                        continue
                    tt = str(v.get("task_type") or "")
                    if not self._is_serial_dispatch_type(tt):
                        continue
                    # Skip waiting_human in incomplete
                    if bucket == "incomplete_tasks" and str(v.get("status") or "") == "waiting_human":
                        continue
                    # Skip claimed active
                    if bucket == "active_tasks" and v.get("claimed_by"):
                        continue
                    return True
        except Exception:
            pass
        return False

    def _peek_next_is_maintenance(self) -> bool:
        """Check if the next dispatchable task needs serial dispatch (read-only).

        Returns True for maintenance and development tasks.
        Uses the same filters as select_and_claim: skip claimed active tasks
        and respect loop_next_dispatch_after on incomplete tasks.
        """
        try:
            with self.state_lock():
                state = self.load_state()

            def _check_type(task):
                return self._is_serial_dispatch_type(
                    str((task or {}).get("task_type") or "slack_mention")
                )

            # Same priority order and filters as select_and_claim
            unclaimed_active = {
                k: v
                for k, v in (state.get("active_tasks") or {}).items()
                if not (v or {}).get("claimed_by")
            }
            if unclaimed_active:
                sel = self._by_oldest(unclaimed_active)
                if sel:
                    return _check_type(sel[1])

            now_epoch = time.time()
            incomplete = {
                k: v
                for k, v in (state.get("incomplete_tasks") or {}).items()
                if str((v or {}).get("status") or "in_progress") != "waiting_human"
                and float((v or {}).get("loop_next_dispatch_after") or "0") <= now_epoch
            }
            if incomplete:
                sel = self._by_oldest(incomplete)
                if sel:
                    return _check_type(sel[1])

            queued = state.get("queued_tasks") or {}
            if queued:
                # Mirror select_and_claim: serial-dispatch tasks take priority
                serial_queued = {
                    k: v for k, v in queued.items()
                    if self._is_serial_dispatch_type(
                        str((v or {}).get("task_type") or "")
                    )
                }
                sel = self._by_oldest(serial_queued) if serial_queued else self._by_oldest(queued)
                if sel:
                    return _check_type(sel[1])
        except Exception:
            pass
        return False

    def _parallel_sleep_policy(self, slots) -> None:
        """Sleep policy for parallel mode.

        Determines sleep duration and writes heartbeat.  The draining_queue
        status overrides the default workers_active/sleeping when all
        workers are idle but dispatchable tasks remain.
        """
        any_busy = any(s.is_busy for s in slots)
        any_idle = any(s.is_idle for s in slots)

        if any_busy:
            sleep_for = self.cfg.poll_interval
        elif any_idle and self._has_dispatchable_tasks():
            sleep_for = 0
        else:
            sleep_for = self.cfg.poll_interval

        # Pending decision gate
        if Path(".agent/runtime/pending_decision.json").exists():
            sleep_for = min(sleep_for, self.cfg.poll_interval)

        # Determine status with draining override
        if not any_busy and any_idle and self._has_dispatchable_tasks():
            status_override = "draining_queue"
        else:
            status_override = None

        self._write_parallel_heartbeat(slots, status_override=status_override, sleep_for=sleep_for)

        if self.cfg.run_once:
            return
        self._interruptible_sleep(sleep_for)
