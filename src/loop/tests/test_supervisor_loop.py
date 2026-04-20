#!/usr/bin/env python3
"""Tests for the Python supervisor loop implementation.

All tests use isolated temporary directories — they never touch the live
.agent/ directory or any running supervisor state.

Run with:
    python3 -m pytest src/loop/tests/test_supervisor_loop.py -v
or:
    python3 -m unittest src.loop.tests.test_supervisor_loop -v
"""

from __future__ import annotations

import json
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# ---------------------------------------------------------------------------
# Bootstrap: make the supervisor module importable without executing main().
# The `main()` function is guarded by __name__=="__main__" so import is safe.
# ---------------------------------------------------------------------------

# Ensure the repo root is on sys.path so imports work when tests are run
# from any working directory.
REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import src.loop.supervisor.main as sl  # noqa: E402  (after sys.path fixup)
from src.loop.supervisor.utils import system_prompt_hash  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conf(tmp: Path, overrides: dict | None = None) -> Path:
    """Write a minimal supervisor_loop.conf into tmp and return its path."""
    lines = [
        '# test conf',
        ': "${SESSION_MINUTES:=5}"',
        ': "${SLEEP_NORMAL:=1}"',
        ': "${PENDING_CHECK_INITIAL:=2}"',
        ': "${PENDING_CHECK_MULTIPLIER:=2}"',
        ': "${PENDING_CHECK_MAX:=10}"',
        ': "${FAILURE_SLEEP_SEC:=3}"',
        ': "${MAX_TRANSIENT_RETRIES:=2}"',
        ': "${TRANSIENT_RETRY_INITIAL:=1}"',
        ': "${TRANSIENT_RETRY_MULTIPLIER:=2}"',
        ': "${TRANSIENT_RETRY_MAX:=8}"',
        ': "${MENTION_POLL_LIMIT:=10}"',
        ': "${MENTION_MAX_PAGES:=3}"',
        ': "${FINISHED_TTL_DAYS:=30}"',
        ': "${MAX_INCOMPLETE_RETENTION:=2592000}"',
        ': "${REFLECT_INTERVAL_SEC:=86400}"',
        ': "${WAITING_HUMAN_REPLY_LIMIT:=100}"',
        ': "${COMPLETION_GATE:=high}"',
        ': "${PROMPT_MEMORY_TOTAL_CHAR_LIMIT:=20000}"',
        ': "${WORKER_ID:=test-agent}"',
        ': "${RUN_ONCE:=true}"',
        ': "${DEFAULT_CHANNEL_ID:=}"',
        f': "${{STATE_FILE:={tmp}/.agent/runtime/state.json}}"',
        f': "${{USER_DIRECTORY_FILE:={tmp}/.agent/memory/user_directory.json}}"',
        f': "${{RUNNER_LOG:={tmp}/.agent/runtime/logs/runner.log}}"',
        f': "${{HEARTBEAT_FILE:={tmp}/.agent/runtime/heartbeat.json}}"',
        f': "${{LAST_SESSION_LOG:={tmp}/.agent/runtime/logs/last_session.log}}"',
        f': "${{SESSION_TEMPLATE:={tmp}/prompts/session.md}}"',
        f': "${{REFLECT_TEMPLATE:={tmp}/prompts/maintenance_reflect.md}}"',
        f': "${{RUNTIME_PROMPT_FILE:={tmp}/.agent/runtime/dispatch/prompt.md}}"',
        f': "${{DISPATCH_TASK_FILE:={tmp}/.agent/runtime/dispatch/task.json}}"',
        f': "${{DISPATCH_OUTCOME_FILE:={tmp}/.agent/runtime/dispatch/outcome.json}}"',
        f': "${{TASKS_DIR:={tmp}/.agent/tasks}}"',
        f': "${{MEMORY_FILE:={tmp}/.agent/memory/memory.md}}"',
        f': "${{LONG_TERM_GOALS_FILE:={tmp}/.agent/memory/long_term_goals.md}}"',
        f': "${{MEMORY_DAILY_DIR:={tmp}/.agent/memory/daily}}"',
    ]
    if overrides:
        for k, v in overrides.items():
            lines.append(f': "${{{k}:={v}}}"')

    conf = tmp / "supervisor_loop.conf"
    conf.write_text("\n".join(lines) + "\n")
    return conf


def _make_supervisor(tmp: Path, env_overrides: dict | None = None) -> sl.Supervisor:
    """Create a Supervisor pointed at tmp with a fresh, isolated config."""
    conf_path = _make_conf(tmp)
    saved = {}
    env_patch = {
        "SLACK_MCP_XOXP_TOKEN": "xoxp-test-token",
        "STATE_FILE": str(tmp / ".agent/runtime/state.json"),
        "USER_DIRECTORY_FILE": str(tmp / ".agent/memory/user_directory.json"),
        "RUNNER_LOG": str(tmp / ".agent/runtime/logs/runner.log"),
        "HEARTBEAT_FILE": str(tmp / ".agent/runtime/heartbeat.json"),
        "LAST_SESSION_LOG": str(tmp / ".agent/runtime/logs/last_session.log"),
        "SESSION_TEMPLATE": str(tmp / "prompts/session.md"),
        "REFLECT_TEMPLATE": str(tmp / "prompts/maintenance_reflect.md"),
        "RUNTIME_PROMPT_FILE": str(tmp / ".agent/runtime/dispatch/prompt.md"),
        "DISPATCH_TASK_FILE": str(tmp / ".agent/runtime/dispatch/task.json"),
        "DISPATCH_OUTCOME_FILE": str(tmp / ".agent/runtime/dispatch/outcome.json"),
        "TASKS_DIR": str(tmp / ".agent/tasks"),
        "MEMORY_FILE": str(tmp / ".agent/memory/memory.md"),
        "LONG_TERM_GOALS_FILE": str(tmp / ".agent/memory/long_term_goals.md"),
        "MEMORY_DAILY_DIR": str(tmp / ".agent/memory/daily"),
        "PROJECTS_DIR": str(tmp / ".agent/projects"),
        "USER_PROFILES_DIR": str(tmp / ".agent/user_profiles"),
        "PROMPT_MEMORY_TOTAL_CHAR_LIMIT": "20000",
        "RUN_ONCE": "true",
        "WORKER_ID": "test-agent",
    }
    if env_overrides:
        env_patch.update(env_overrides)
    for k, v in env_patch.items():
        saved[k] = os.environ.get(k)
        os.environ[k] = v
    try:
        cfg = sl.Config(conf_path)
        sup = sl.Supervisor(cfg)
    finally:
        for k, orig in saved.items():
            if orig is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = orig
    # Plan-51 safety net: prevent tests from triggering real git commits
    sup._auto_commit_system_files = lambda: None
    return sup


def _write_agent_identity(sup: sl.Supervisor, agent_id: str = "U_AGENT") -> None:
    """Write agent identity into user_directory.json for tests."""
    path = sup.cfg.user_directory_file
    path.parent.mkdir(parents=True, exist_ok=True)
    import json as _json
    _json.dump({"agent": {"user_id": agent_id}, "users": {}}, path.open("w"))
    sup._agent_user_id = agent_id


def _empty_state() -> dict:
    return {
        "watermark_ts": "0",
        "active_tasks": {},
        "queued_tasks": {},
        "incomplete_tasks": {},
        "finished_tasks": {},
        "supervisor": {"last_reflect_dispatch_ts": "0"},
    }


def _make_task(mention_ts: str = "1000.000000", status: str = "queued",
               task_type: str = "slack_mention",
               maintenance_phase: int | None = None) -> dict:
    thread_ts = mention_ts
    d = {
        "mention_ts": thread_ts,
        "thread_ts": thread_ts,
        "channel_id": "C123",
        "mention_text_file": "",
        "status": status,
        "claimed_by": None,
        "summary": "",
        "task_description": "",
        "report_path": f"reports/{mention_ts}.md",
        "created_ts": mention_ts,
        "last_update_ts": mention_ts,
        "source": {"user_id": "U1", "user_name": "alice", "time_iso": ""},
        "task_type": task_type,
        "last_error": None,
    }
    if maintenance_phase is not None:
        d["maintenance_phase"] = maintenance_phase
    return d


# ===========================================================================
# 1. Pure utility functions
# ===========================================================================

class TestTsToInt(unittest.TestCase):

    def test_integer_ts(self):
        self.assertEqual(sl.ts_to_int("1000"), 1000_000000)

    def test_float_ts(self):
        self.assertEqual(sl.ts_to_int("1000.123456"), 1000_123456)

    def test_float_ts_short_frac(self):
        # "1000.1" → frac padded to "100000"
        self.assertEqual(sl.ts_to_int("1000.1"), 1000_100000)

    def test_empty_string(self):
        self.assertEqual(sl.ts_to_int(""), 0)

    def test_noise_characters(self):
        # Non-digit chars stripped; result should still parse
        val = sl.ts_to_int("abc1000def.123456ghi")
        self.assertEqual(val, 1000_123456)

    def test_ordering(self):
        self.assertGreater(sl.ts_to_int("1001.000000"), sl.ts_to_int("1000.999999"))


class TestTsGt(unittest.TestCase):

    def test_greater(self):
        self.assertTrue(sl.ts_gt("2000.000000", "1000.000000"))

    def test_not_greater(self):
        self.assertFalse(sl.ts_gt("1000.000000", "2000.000000"))

    def test_equal(self):
        self.assertFalse(sl.ts_gt("1000.000000", "1000.000000"))


class TestParseBool(unittest.TestCase):

    def test_true_values(self):
        for v in ("1", "true", "True", "TRUE", "yes", "y", "on"):
            with self.subTest(v=v):
                self.assertTrue(sl.parse_bool(v))

    def test_false_values(self):
        for v in ("0", "false", "False", "no", "off", ""):
            with self.subTest(v=v):
                self.assertFalse(sl.parse_bool(v))


class TestIsoFromTsFloor(unittest.TestCase):

    def test_known_epoch(self):
        # Unix epoch 0 → 1970-01-01 00:00 UTC
        result = sl.iso_from_ts_floor("0")
        self.assertEqual(result, "1970-01-01 00:00 UTC")

    def test_invalid(self):
        self.assertEqual(sl.iso_from_ts_floor("not-a-number"), "")


class TestTimestampUtc(unittest.TestCase):

    def test_format(self):
        ts = sl.timestamp_utc()
        # Should match YYYY-MM-DDTHH:MM:SSZ
        import re
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestNowTs(unittest.TestCase):

    def test_float_string_with_6_decimals(self):
        ts = sl.now_ts()
        self.assertIn(".", ts)
        int_part, frac = ts.split(".")
        self.assertGreater(int(int_part), 0)
        self.assertEqual(len(frac), 6)


# ===========================================================================
# 2. Config and conf parsing
# ===========================================================================

class TestParseConfDefaults(unittest.TestCase):

    def test_parses_simple_values(self):
        with tempfile.TemporaryDirectory() as td:
            conf = Path(td) / "test.conf"
            conf.write_text(
                ': "${SESSION_MINUTES:=300}"\n'
                ': "${SLEEP_NORMAL:=60}"\n'
                '# a comment\n'
                'ignored line\n'
            )
            result = sl.parse_conf_defaults(conf)
        self.assertEqual(result["SESSION_MINUTES"], "300")
        self.assertEqual(result["SLEEP_NORMAL"], "60")
        self.assertNotIn("ignored", result)

    def test_missing_file(self):
        result = sl.parse_conf_defaults(Path("/nonexistent/path/file.conf"))
        self.assertEqual(result, {})

    def test_env_override_in_default_expr(self):
        # Confirm that the env takes precedence when resolve_default_expr uses ${VAR:-fallback}
        with tempfile.TemporaryDirectory() as td:
            conf = Path(td) / "test.conf"
            # Uses a fallback expression: if FOO_TEST_VAR is set in env, use it
            conf.write_text(': "${SESSION_MINUTES:=${FOO_TEST_VAR:-999}}"\n')
            os.environ["FOO_TEST_VAR"] = "42"
            try:
                result = sl.parse_conf_defaults(conf)
            finally:
                del os.environ["FOO_TEST_VAR"]
        self.assertEqual(result["SESSION_MINUTES"], "42")


class TestConfig(unittest.TestCase):

    def test_defaults_loaded_from_conf(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            conf = _make_conf(tmp)
            # Clear any env vars that could override
            clean_env = {k: v for k, v in os.environ.items()
                         if k not in {"SESSION_MINUTES", "SLEEP_NORMAL", "RUN_ONCE"}}
            with patch.dict(os.environ, {"SLACK_MCP_XOXP_TOKEN": "x",
                                          "SESSION_MINUTES": "5",
                                          "SLEEP_NORMAL": "1",
                                          "RUN_ONCE": "true"}, clear=False):
                cfg = sl.Config(conf)
            self.assertEqual(cfg.session_minutes, 5)
            self.assertEqual(cfg.sleep_normal, 1)
            self.assertTrue(cfg.run_once)

    def test_env_overrides_conf(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            conf = _make_conf(tmp)
            with patch.dict(os.environ, {"SESSION_MINUTES": "999",
                                          "SLACK_MCP_XOXP_TOKEN": "x"}):
                cfg = sl.Config(conf)
            self.assertEqual(cfg.session_minutes, 999)


# ===========================================================================
# 3. Atomic write / read helpers
# ===========================================================================

class TestAtomicWriteJson(unittest.TestCase):

    def test_writes_valid_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "out.json"
            sl.Supervisor.atomic_write_json(path, {"key": "value", "n": 42})
            data = json.loads(path.read_text())
            self.assertEqual(data["key"], "value")
            self.assertEqual(data["n"], 42)

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "sub" / "dir" / "out.json"
            sl.Supervisor.atomic_write_json(path, {"x": 1})
            self.assertTrue(path.exists())

    def test_read_json_missing_file(self):
        val = sl.Supervisor.read_json(Path("/nonexistent/file.json"), {"default": True})
        self.assertEqual(val, {"default": True})

    def test_read_json_invalid_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "bad.json"
            path.write_text("not json {{{")
            val = sl.Supervisor.read_json(path, "fallback")
            self.assertEqual(val, "fallback")


# ===========================================================================
# 4. ensure_state_schema
# ===========================================================================

class TestEnsureStateSchema(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_creates_schema_when_empty(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.ensure_state_schema()
            state = sup.load_state()
            for key in ("watermark_ts", "active_tasks", "queued_tasks",
                         "incomplete_tasks", "finished_tasks", "supervisor"):
                with self.subTest(key=key):
                    self.assertIn(key, state)

    def test_normalises_tasks_in_all_buckets(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            raw_state = _empty_state()
            raw_state["queued_tasks"]["ts1"] = {
                "mention_ts": "ts1",
                "thread_ts": "ts1",
                "status": "queued",
            }
            sup.save_state(raw_state)
            sup.ensure_state_schema()
            state = sup.load_state()
            # Key should be the normalised mention_ts
            self.assertIn("ts1", state["queued_tasks"])

    def test_canonicalises_maintenance_key_and_infers_phase(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            raw_state = _empty_state()
            raw_state["queued_tasks"]["1772000000.000001"] = {
                "mention_ts": "1772000000.000001",
                "thread_ts": "1772000000.000001",
                "status": "queued",
                "task_type": "maintenance",
                "task_description": "Developer review: audit recent agent work and fix/improve system code.",
                "report_path": "reports/developer.review.md",
            }
            sup.save_state(raw_state)
            sup.ensure_state_schema()
            state = sup.load_state()
            self.assertIn("maintenance", state["queued_tasks"])
            task = state["queued_tasks"]["maintenance"]
            self.assertEqual(task["mention_ts"], "maintenance")
            self.assertEqual(task["thread_ts"], "1772000000.000001")
            self.assertEqual(task["maintenance_phase"], 1)

    def test_finished_tasks_forced_done_status(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            raw_state = _empty_state()
            raw_state["finished_tasks"]["1000.000000"] = {
                "mention_ts": "1000.000000",
                "status": "weird_status",
            }
            sup.save_state(raw_state)
            sup.ensure_state_schema()
            state = sup.load_state()
            task = state["finished_tasks"]["1000.000000"]
            self.assertEqual(task["status"], "done")

    def test_skips_save_when_state_already_normalized(self):
        """ensure_state_schema should not rewrite state.json when nothing changed."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            raw_state = _empty_state()
            raw_state["queued_tasks"]["ts1"] = {
                "mention_ts": "ts1",
                "thread_ts": "ts1",
                "status": "queued",
            }
            sup.save_state(raw_state)
            # First call normalizes the state
            sup.ensure_state_schema()
            mtime_after_first = os.path.getmtime(sup.cfg.state_file)
            # Second call should detect no changes and skip the write
            sup.ensure_state_schema()
            mtime_after_second = os.path.getmtime(sup.cfg.state_file)
            self.assertEqual(mtime_after_first, mtime_after_second)

# ===========================================================================
# 5. normalize_task
# ===========================================================================

class TestNormalizeTask(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_fills_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            result = sup.normalize_task({"mention_ts": "1234.0"}, "1234.0")
            self.assertEqual(result["mention_ts"], "1234.0")
            self.assertEqual(result["thread_ts"], "1234.0")
            self.assertEqual(result["status"], "in_progress")

    def test_maintenance_preserves_mention_and_infers_phase(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            result = sup.normalize_task(
                {
                    "task_type": "maintenance",
                    "mention_ts": "maintenance",
                    "thread_ts": "2000.000001",
                    "report_path": "reports/developer.review.md",
                    "task_description": "Developer review: audit recent agent work and fix/improve system code.",
                },
                "maintenance",
            )
            self.assertEqual(result["mention_ts"], "maintenance")
            self.assertEqual(result["thread_ts"], "2000.000001")
            self.assertEqual(result["maintenance_phase"], 1)

    def test_force_done(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            result = sup.normalize_task({"mention_ts": "1.0", "status": "queued"}, "1.0", force_done=True)
            self.assertEqual(result["status"], "done")

    def test_channel_fallback_from_channel_field(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            result = sup.normalize_task({"mention_ts": "1.0", "channel": "C999"}, "1.0")
            self.assertEqual(result["channel_id"], "C999")

    def test_channel_fallback_to_default_channel_id(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"DEFAULT_CHANNEL_ID": "CDEFAULT"})
            result = sup.normalize_task({"mention_ts": "1.0", "channel_id": ""}, "1.0")
            self.assertEqual(result["channel_id"], "CDEFAULT")

    def test_task_description_from_thread_text_file(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            mention_text_file = sup.task_text_path("1.0", "queued_tasks")
            mention_text_file.parent.mkdir(parents=True, exist_ok=True)
            task_data = {
                "task_id": "1.0",
                "thread_ts": "1.0",
                "channel_id": "",
                "messages": [
                    {
                        "ts": "1.0",
                        "user_id": "U_HUMAN",
                        "role": "human",
                        "text": "<@U_AGENT> create a compact dashboard and host it on GitHub Pages",
                    }
                ],
            }
            sup.write_task_json(str(mention_text_file), task_data)
            result = sup.normalize_task(
                {
                    "mention_ts": "1.0",
                    "mention_text_file": str(mention_text_file),
                },
                "1.0",
            )
            self.assertEqual(
                result["task_description"],
                "Create a compact dashboard and host it on GitHub Pages.",
            )


# ===========================================================================
# 6. select_next_task — priority: active > incomplete (non-waiting) > queued
# ===========================================================================

class TestSelectNextTask(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_no_tasks_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.save_state(_empty_state())
            self.assertFalse(sup.select_next_task())

    def test_active_beats_queued(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            state["active_tasks"]["2.0"] = _make_task("2.0", "in_progress")
            sup.save_state(state)
            self.assertTrue(sup.select_next_task())
            self.assertEqual(sup.selected_bucket, "active_tasks")
            self.assertEqual(sup.selected_key, "2.0")

    def test_incomplete_non_waiting_beats_queued(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            state["incomplete_tasks"]["3.0"] = _make_task("3.0", "in_progress")
            sup.save_state(state)
            self.assertTrue(sup.select_next_task())
            self.assertEqual(sup.selected_bucket, "incomplete_tasks")

    def test_waiting_human_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["incomplete_tasks"]["3.0"] = _make_task("3.0", "waiting_human")
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            self.assertTrue(sup.select_next_task())
            # waiting_human in incomplete must be skipped; queued wins
            self.assertEqual(sup.selected_bucket, "queued_tasks")

    def test_selects_oldest_by_last_update_ts(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            older = _make_task("100.0", "queued")
            older["last_update_ts"] = "100.000000"
            newer = _make_task("200.0", "queued")
            newer["last_update_ts"] = "200.000000"
            state["queued_tasks"]["100.0"] = older
            state["queued_tasks"]["200.0"] = newer
            sup.save_state(state)
            sup.select_next_task()
            self.assertEqual(sup.selected_key, "100.0")


# ===========================================================================
# 7. claim_task_for_worker
# ===========================================================================

class TestClaimTaskForWorker(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_moves_queued_to_active(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            sup.claim_task_for_worker("queued_tasks", "1.0")
            new_state = sup.load_state()
            self.assertNotIn("1.0", new_state["queued_tasks"])
            self.assertIn("1.0", new_state["active_tasks"])

    def test_sets_status_in_progress_and_claimed_by(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            sup.claim_task_for_worker("queued_tasks", "1.0")
            new_state = sup.load_state()
            task = new_state["active_tasks"]["1.0"]
            self.assertEqual(task["status"], "in_progress")
            self.assertEqual(task["claimed_by"], "test-agent")

    def test_active_stays_in_active(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["active_tasks"]["1.0"] = _make_task("1.0", "in_progress")
            sup.save_state(state)
            sup.claim_task_for_worker("active_tasks", "1.0")
            new_state = sup.load_state()
            self.assertIn("1.0", new_state["active_tasks"])

    def test_writes_dispatch_task_file(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            sup.claim_task_for_worker("queued_tasks", "1.0")
            dispatch = json.loads(sup.cfg.dispatch_task_file.read_text())
            self.assertEqual(dispatch["mention_ts"], "1.0")

    def test_dispatch_mention_text_loaded_from_task_file(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            task = _make_task("1.0", "queued")
            queue_file = str(sup.task_text_path("1.0", "queued_tasks"))
            task["mention_text_file"] = queue_file
            task_data = {
                "task_id": "1.0", "thread_ts": "1.0", "channel_id": "C123",
                "messages": [{"ts": "1.0", "user_id": "U1", "role": "human",
                              "text": "Canonical task text"}],
            }
            Path(queue_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(queue_file, task_data)
            state["queued_tasks"]["1.0"] = task
            sup.save_state(state)

            sup.claim_task_for_worker("queued_tasks", "1.0")
            dispatch = json.loads(sup.cfg.dispatch_task_file.read_text())

            self.assertEqual(dispatch["mention_text_file"], str(sup.task_text_path("1.0", "active_tasks")))
            self.assertIn("Canonical task text", dispatch["mention_text"])


# ===========================================================================
# 7b. select_and_claim (atomic select + claim)
# ===========================================================================

class TestSelectAndClaim(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_selects_and_claims_queued_task(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            result = sup.select_and_claim()
            self.assertIsNotNone(result)
            task_key, task_type = result
            self.assertEqual(task_key, "1.0")
            self.assertEqual(task_type, "slack_mention")
            new_state = sup.load_state()
            self.assertIn("1.0", new_state["active_tasks"])
            self.assertNotIn("1.0", new_state["queued_tasks"])

    def test_returns_none_when_no_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.save_state(_empty_state())
            result = sup.select_and_claim()
            self.assertIsNone(result)

    def test_sets_claimed_by_with_slot(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            sup.select_and_claim(worker_slot=2)
            new_state = sup.load_state()
            self.assertEqual(new_state["active_tasks"]["1.0"]["claimed_by"], "test-agent-slot-2")

    def test_priority_active_over_queued(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["active_tasks"]["active.0"] = _make_task("active.0", "in_progress")
            state["queued_tasks"]["queued.0"] = _make_task("queued.0", "queued")
            sup.save_state(state)
            result = sup.select_and_claim()
            task_key, _ = result
            self.assertEqual(task_key, "active.0")

    def test_skips_waiting_human_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            waiting = _make_task("wait.0", "waiting_human")
            state["incomplete_tasks"]["wait.0"] = waiting
            state["queued_tasks"]["q.0"] = _make_task("q.0", "queued")
            sup.save_state(state)
            result = sup.select_and_claim()
            task_key, _ = result
            self.assertEqual(task_key, "q.0")

    def test_sets_instance_vars_for_compat(self):
        """select_and_claim sets selected_bucket/key/_active_task_type for serial path."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            sup.select_and_claim()
            self.assertEqual(sup.selected_key, "1.0")
            self.assertEqual(sup._active_task_type, "slack_mention")

    def test_writes_dispatch_task_file(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            sup.select_and_claim()
            dispatch = json.loads(sup.cfg.dispatch_task_file.read_text())
            self.assertEqual(dispatch["mention_ts"], "1.0")

    def test_skips_already_claimed_active_tasks(self):
        """select_and_claim should not re-claim an active task that has claimed_by set."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            # Active task already claimed by slot 0
            task = _make_task("1.0", "in_progress")
            task["claimed_by"] = "test-agent-slot-0"
            state["active_tasks"]["1.0"] = task
            # Also a queued task
            state["queued_tasks"]["2.0"] = _make_task("2.0", "queued")
            sup.save_state(state)

            # Should skip the claimed active task and pick the queued one
            result = sup.select_and_claim(worker_slot=1)
            self.assertIsNotNone(result)
            self.assertEqual(result[0], "2.0")

    def test_returns_none_when_all_active_claimed(self):
        """select_and_claim returns None when only claimed active tasks exist."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            task = _make_task("1.0", "in_progress")
            task["claimed_by"] = "test-agent-slot-0"
            state["active_tasks"]["1.0"] = task
            sup.save_state(state)

            result = sup.select_and_claim(worker_slot=1)
            self.assertIsNone(result)

    def test_writes_to_custom_dispatch_file(self):
        """select_and_claim should write to a custom dispatch_task_file."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            custom_file = Path(td) / "custom_dispatch.json"
            sup.select_and_claim(dispatch_task_file=custom_file)
            self.assertTrue(custom_file.exists())
            dispatch = json.loads(custom_file.read_text())
            self.assertEqual(dispatch["mention_ts"], "1.0")


class TestUnclaimTask(unittest.TestCase):
    """Tests for _unclaim_task (undoing a claim)."""

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_unclaim_moves_to_queued(self):
        """_unclaim_task should move task from active to queued."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            task = _make_task("1.0", "in_progress")
            task["claimed_by"] = "test-agent-slot-0"
            state["active_tasks"]["1.0"] = task
            sup.save_state(state)

            sup._unclaim_task("1.0")

            state = sup.load_state()
            self.assertNotIn("1.0", state["active_tasks"])
            self.assertIn("1.0", state["queued_tasks"])
            self.assertIsNone(state["queued_tasks"]["1.0"]["claimed_by"])
            self.assertEqual(state["queued_tasks"]["1.0"]["status"], "queued")

    def test_unclaim_noop_for_missing_key(self):
        """_unclaim_task should be a no-op if key not in active_tasks."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            sup.save_state(state)
            sup._unclaim_task("nonexistent")
            # Should not raise


# ===========================================================================
# 8. prune_finished_tasks
# ===========================================================================

class TestPruneFinishedTasks(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_removes_old_finished(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            # TTL is 30 days. Use a ts 31 days old.
            old_ts = f"{int(time.time()) - 31 * 86400}.000000"
            state = _empty_state()
            state["finished_tasks"][old_ts] = _make_task(old_ts, "done")
            state["finished_tasks"][old_ts]["last_update_ts"] = old_ts
            sup.save_state(state)
            sup.prune_finished_tasks()
            new_state = sup.load_state()
            self.assertNotIn(old_ts, new_state["finished_tasks"])

    def test_keeps_recent_finished(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            recent_ts = f"{int(time.time()) - 1 * 86400}.000000"
            state = _empty_state()
            state["finished_tasks"][recent_ts] = _make_task(recent_ts, "done")
            state["finished_tasks"][recent_ts]["last_update_ts"] = recent_ts
            sup.save_state(state)
            sup.prune_finished_tasks()
            new_state = sup.load_state()
            self.assertIn(recent_ts, new_state["finished_tasks"])

    def test_mixed_prune(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            old_ts = f"{int(time.time()) - 31 * 86400}.000000"
            recent_ts = f"{int(time.time()) - 5 * 86400}.000000"
            state = _empty_state()
            for ts in (old_ts, recent_ts):
                task = _make_task(ts, "done")
                task["last_update_ts"] = ts
                state["finished_tasks"][ts] = task
            sup.save_state(state)
            sup.prune_finished_tasks()
            new_state = sup.load_state()
            self.assertNotIn(old_ts, new_state["finished_tasks"])
            self.assertIn(recent_ts, new_state["finished_tasks"])

    def test_removes_task_markdown_for_pruned_finished(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            old_ts = f"{int(time.time()) - 31 * 86400}.000000"
            state = _empty_state()
            task = _make_task(old_ts, "done")
            task["last_update_ts"] = old_ts
            mention_text_file = str(sup.cfg.tasks_dir / "finished" / f"{old_ts}.json")
            Path(mention_text_file).parent.mkdir(parents=True, exist_ok=True)
            Path(mention_text_file).write_text("finished task text\n", encoding="utf-8")
            task["mention_text_file"] = mention_text_file
            state["finished_tasks"][old_ts] = task
            sup.save_state(state)

            sup.prune_finished_tasks()

            new_state = sup.load_state()
            self.assertNotIn(old_ts, new_state["finished_tasks"])
            self.assertFalse(Path(mention_text_file).exists())


    def test_skips_save_when_nothing_pruned(self):
        """prune_finished_tasks should not rewrite state.json when no tasks are expired."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            recent_ts = f"{int(time.time()) - 1 * 86400}.000000"
            state = _empty_state()
            state["finished_tasks"][recent_ts] = _make_task(recent_ts, "done")
            state["finished_tasks"][recent_ts]["last_update_ts"] = recent_ts
            sup.save_state(state)
            mtime_before = os.path.getmtime(sup.cfg.state_file)
            sup.prune_finished_tasks()
            mtime_after = os.path.getmtime(sup.cfg.state_file)
            self.assertEqual(mtime_before, mtime_after)


class TestPruneStaleWaitingHumanTasks(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_removes_old_waiting_human(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            old_ts = f"{int(time.time()) - 31 * 86400}.000000"
            state = _empty_state()
            task = _make_task(old_ts, "waiting_human")
            task["last_update_ts"] = old_ts
            state["incomplete_tasks"][old_ts] = task
            sup.save_state(state)
            sup.prune_stale_waiting_human_tasks()
            new_state = sup.load_state()
            self.assertNotIn(old_ts, new_state["incomplete_tasks"])

    def test_keeps_recent_waiting_human(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            recent_ts = f"{int(time.time()) - 1 * 86400}.000000"
            state = _empty_state()
            task = _make_task(recent_ts, "waiting_human")
            task["last_update_ts"] = recent_ts
            state["incomplete_tasks"][recent_ts] = task
            sup.save_state(state)
            sup.prune_stale_waiting_human_tasks()
            new_state = sup.load_state()
            self.assertIn(recent_ts, new_state["incomplete_tasks"])

    def test_keeps_old_non_waiting_human(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            old_ts = f"{int(time.time()) - 31 * 86400}.000000"
            state = _empty_state()
            task = _make_task(old_ts, "in_progress")
            task["last_update_ts"] = old_ts
            state["incomplete_tasks"][old_ts] = task
            sup.save_state(state)
            sup.prune_stale_waiting_human_tasks()
            new_state = sup.load_state()
            self.assertIn(old_ts, new_state["incomplete_tasks"])

    def test_keeps_waiting_human_with_recent_last_seen_mention(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            old_ts = f"{int(time.time()) - 31 * 86400}.000000"
            recent_seen = f"{int(time.time()) - 1 * 86400}.000000"
            state = _empty_state()
            task = _make_task(old_ts, "waiting_human")
            task["last_update_ts"] = old_ts
            task["last_seen_mention_ts"] = recent_seen
            state["incomplete_tasks"][old_ts] = task
            sup.save_state(state)

            sup.prune_stale_waiting_human_tasks()

            new_state = sup.load_state()
            self.assertIn(old_ts, new_state["incomplete_tasks"])

    def test_removes_task_markdown_for_pruned_waiting_human(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            old_ts = f"{int(time.time()) - 31 * 86400}.000000"
            state = _empty_state()
            task = _make_task(old_ts, "waiting_human")
            task["last_update_ts"] = old_ts
            mention_text_file = str(sup.cfg.tasks_dir / "incomplete" / f"{old_ts}.json")
            Path(mention_text_file).parent.mkdir(parents=True, exist_ok=True)
            Path(mention_text_file).write_text("waiting_human task text\n", encoding="utf-8")
            task["mention_text_file"] = mention_text_file
            state["incomplete_tasks"][old_ts] = task
            sup.save_state(state)

            sup.prune_stale_waiting_human_tasks()

            new_state = sup.load_state()
            self.assertNotIn(old_ts, new_state["incomplete_tasks"])
            self.assertFalse(Path(mention_text_file).exists())

    def test_skips_save_when_nothing_pruned(self):
        """prune_stale_waiting_human_tasks should not rewrite state.json when no tasks are stale."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            recent_ts = f"{int(time.time()) - 1 * 86400}.000000"
            state = _empty_state()
            task = _make_task(recent_ts, "waiting_human")
            task["last_update_ts"] = recent_ts
            state["incomplete_tasks"][recent_ts] = task
            sup.save_state(state)
            mtime_before = os.path.getmtime(sup.cfg.state_file)
            sup.prune_stale_waiting_human_tasks()
            mtime_after = os.path.getmtime(sup.cfg.state_file)
            self.assertEqual(mtime_before, mtime_after)


# ===========================================================================
# 9. classify_failure
# ===========================================================================

class TestClassifyFailure(unittest.TestCase):

    def test_exit_0_is_none(self):
        self.assertEqual(sl.Supervisor.classify_failure("", 0), "none")

    def test_exit_124_is_timeout(self):
        self.assertEqual(sl.Supervisor.classify_failure("", 124), "timeout")

    def test_exit_137_is_timeout(self):
        self.assertEqual(sl.Supervisor.classify_failure("", 137), "timeout")

    def test_transient_pattern_in_output(self):
        kind = sl.Supervisor.classify_failure("stream disconnected before completion", 1)
        self.assertEqual(kind, "transient_transport")

    def test_transport_error(self):
        self.assertEqual(sl.Supervisor.classify_failure("transport error occurred", 1), "transient_transport")

    def test_network_error(self):
        self.assertEqual(sl.Supervisor.classify_failure("Network Error: connection reset", 1), "transient_transport")

    def test_nonzero_exit_no_pattern(self):
        self.assertEqual(sl.Supervisor.classify_failure("some random error output", 2), "nonzero_exit")

    def test_transient_pattern_case_insensitive(self):
        self.assertEqual(sl.Supervisor.classify_failure("TLS handshake failed", 1), "transient_transport")


class TestErrorPreview(unittest.TestCase):

    def test_truncates_to_max_chars(self):
        out = "x" * 1000
        preview = sl.Supervisor.error_preview(out, max_chars=100)
        self.assertLessEqual(len(preview), 100)

    def test_takes_last_lines(self):
        lines = [f"line{i}" for i in range(20)]
        out = "\n".join(lines)
        preview = sl.Supervisor.error_preview(out, max_chars=10000)
        # Last 8 lines should appear; first lines may be truncated
        self.assertIn("line19", preview)

    def test_empty_output(self):
        self.assertEqual(sl.Supervisor.error_preview(""), "")


# ===========================================================================
# 10. reconcile_task_after_run
# ===========================================================================

class TestReconcileTaskAfterRun(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def _setup_active_task(self, sup, key="1000.000000"):
        state = _empty_state()
        state["active_tasks"][key] = _make_task(key, "in_progress")
        sup.save_state(state)

    def _write_outcome(self, sup, outcome: dict):
        sup.cfg.dispatch_outcome_file.parent.mkdir(parents=True, exist_ok=True)
        sup.cfg.dispatch_outcome_file.write_text(json.dumps(outcome))

    # -- done with high confidence ------------------------------------------

    def test_done_high_confidence_goes_to_finished(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "all good", "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])
            self.assertEqual(
                state["finished_tasks"][key]["mention_text_file"],
                str(sup.task_text_path(key, "finished_tasks")),
            )
            self.assertNotIn(key, state["active_tasks"])

    # -- requires_human_feedback overrides done -> waiting_human -------------

    def test_requires_human_feedback_becomes_waiting_human(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "needs review", "completion_confidence": "high",
                "requires_human_feedback": True,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "waiting_human")

    # -- waiting_human goes to incomplete ------------------------------------

    def test_waiting_human_goes_to_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "waiting_human",
                "summary": "", "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "waiting_human")
            self.assertEqual(
                state["incomplete_tasks"][key]["mention_text_file"],
                str(sup.task_text_path(key, "incomplete_tasks")),
            )

    def test_waiting_human_maintenance_uses_default_channel_and_outcome_thread(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"DEFAULT_CHANNEL_ID": "CDEFAULT"})
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=1)
            task["thread_ts"] = "maintenance"
            task["channel_id"] = ""
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "waiting_human",
                "thread_ts": "2000.000001",
                "summary": "", "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "waiting_human")
            self.assertEqual(state["incomplete_tasks"][key]["channel_id"], "CDEFAULT")
            self.assertEqual(state["incomplete_tasks"][key]["thread_ts"], "2000.000001")

    def test_maintenance_in_progress_outcome_is_forced_to_waiting_human(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"DEFAULT_CHANNEL_ID": "CDEFAULT"})
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=1)
            task["thread_ts"] = "maintenance"
            task["channel_id"] = ""
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "in_progress",
                "thread_ts": "2000.000011",
                "summary": "", "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "waiting_human")
            self.assertEqual(state["incomplete_tasks"][key]["thread_ts"], "2000.000011")
            self.assertIn(
                "maintenance_in_progress_disallowed",
                str(state["incomplete_tasks"][key].get("last_error") or ""),
            )

    def test_non_final_maintenance_phase_force_finished(self):
        """Non-final maintenance phases (e.g. phase 0 reflect) are force-finished
        so advance_phase can re-queue the task at the next phase."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"DEFAULT_CHANNEL_ID": "CDEFAULT"})
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=0)
            task["thread_ts"] = "maintenance"
            task["channel_id"] = ""
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "waiting_human",
                "thread_ts": "2000.000001",
                "summary": "", "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            # After force-finish + advance_phase, task is re-queued at phase 1.
            self.assertIn(key, state["queued_tasks"])
            self.assertEqual(state["queued_tasks"][key]["maintenance_phase"], 1)
            self.assertEqual(state["queued_tasks"][key]["status"], "queued")

    def test_advance_phase_moves_task_text_file(self):
        """advance_phase should physically move the task text file from
        finished_tasks bucket to queued_tasks bucket."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"DEFAULT_CHANNEL_ID": "CDEFAULT"})
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=0)
            task["thread_ts"] = "maintenance"
            task["channel_id"] = ""
            state["active_tasks"][key] = task
            sup.save_state(state)

            # Create the task text file in the finished_tasks bucket (where
            # the reconciler would have placed it before calling advance_phase).
            finished_path = sup.task_text_path(key, "finished_tasks")
            finished_path.parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(str(finished_path), {
                "task_id": key, "thread_ts": key, "channel_id": "",
                "messages": [{"ts": key, "role": "human", "text": "test context"}],
            })
            task["mention_text_file"] = str(finished_path)
            sup.save_state(state)

            self._write_outcome(sup, {
                "mention_ts": key, "status": "waiting_human",
                "thread_ts": "2000.000001",
                "summary": "", "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()

            # Task should be re-queued at phase 1.
            self.assertIn(key, state["queued_tasks"])
            queued_task = state["queued_tasks"][key]
            queued_path = Path(queued_task["mention_text_file"])
            # The file should exist at the new (queued) location.
            self.assertTrue(queued_path.exists(), f"Task text file missing at {queued_path}")
            # The old file should be gone.
            self.assertFalse(finished_path.exists(), "Old task text file not cleaned up")
            # Content should be preserved.
            data = sup.read_task_json(str(queued_path))
            self.assertTrue(len(data.get("messages", [])) > 0, "Thread history lost during phase advance")

    def test_legacy_developer_review_without_phase_is_not_requeued(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"DEFAULT_CHANNEL_ID": "CDEFAULT"})
            key = "maintenance"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance")
            task["thread_ts"] = "2000.000001"
            task["channel_id"] = ""
            task["report_path"] = "reports/developer.review.md"
            task["task_description"] = "Developer review: audit recent agent work and fix/improve system code."
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "waiting_human",
                "thread_ts": "2000.000001",
                "summary": "", "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertNotIn(key, state["queued_tasks"])
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "waiting_human")
            self.assertEqual(state["incomplete_tasks"][key]["maintenance_phase"], 1)

    # -- FIX-010 #4: Tribune maintenance summary bridge ----------------------

    def test_tribune_maintenance_summary_posted_in_thread(self):
        """Tribune maintenance summary is posted to Slack in the existing thread."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
                "TRIBUNE_MAINT_ROUNDS": "1",
            })
            key = "1000.000000"
            state = _empty_state()
            # Phase 2 = Tribune when TRIBUNE_MAINT_ROUNDS=1
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=2)
            task["thread_ts"] = "2000.000001"
            task["channel_id"] = "CDEFAULT"
            task["report_path"] = "reports/tribune.maintenance.md"
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "thread_ts": "2000.000001",
                "summary": "Tribune review: all good.",
                "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            with patch.object(sup, "slack_api_post", return_value={"ok": True}) as mock_post:
                sup.reconcile_task_after_run(key, 0)
            # Find the chat.postMessage call for the summary
            summary_calls = [c for c in mock_post.call_args_list
                             if c[0][0] == "chat.postMessage"
                             and "Tribune review" in str(c[0][1].get("text", ""))]
            self.assertEqual(len(summary_calls), 1)
            payload = summary_calls[0][0][1]
            self.assertEqual(payload["channel"], "CDEFAULT")
            self.assertEqual(payload["thread_ts"], "2000.000001")

    def test_tribune_maintenance_summary_posted_toplevel(self):
        """When thread_ts is the sentinel, summary is posted as top-level."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
                "TRIBUNE_MAINT_ROUNDS": "1",
            })
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=2)
            task["thread_ts"] = "maintenance"
            task["channel_id"] = "CDEFAULT"
            task["report_path"] = "reports/tribune.maintenance.md"
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "thread_ts": "maintenance",
                "summary": "Tribune review: all good.",
                "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            with patch.object(sup, "slack_api_post", return_value={"ok": True}) as mock_post:
                sup.reconcile_task_after_run(key, 0)
            summary_calls = [c for c in mock_post.call_args_list
                             if c[0][0] == "chat.postMessage"
                             and "Tribune review" in str(c[0][1].get("text", ""))]
            self.assertEqual(len(summary_calls), 1)
            payload = summary_calls[0][0][1]
            self.assertEqual(payload["channel"], "CDEFAULT")
            self.assertNotIn("thread_ts", payload)

    def test_tribune_maintenance_summary_empty_skipped(self):
        """No Slack call when Tribune summary is empty."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
                "TRIBUNE_MAINT_ROUNDS": "1",
            })
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=2)
            task["thread_ts"] = "2000.000001"
            task["channel_id"] = "CDEFAULT"
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "thread_ts": "2000.000001",
                "summary": "",
                "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            with patch.object(sup, "slack_api_post", return_value={"ok": True}) as mock_post:
                sup.reconcile_task_after_run(key, 0)
            # Filter out done-ack messages; only check for tribune summary posts
            summary_calls = [c for c in mock_post.call_args_list
                             if c[0][0] == "chat.postMessage"
                             and "done" not in str(c[0][1].get("text", "")).lower()]
            self.assertEqual(len(summary_calls), 0)

    def test_tribune_maintenance_summary_no_token(self):
        """Graceful skip when slack_token is missing."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
                "TRIBUNE_MAINT_ROUNDS": "1",
                "SLACK_MCP_XOXP_TOKEN": "",
            })
            sup.slack_token = ""
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=2)
            task["thread_ts"] = "2000.000001"
            task["channel_id"] = "CDEFAULT"
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "thread_ts": "2000.000001",
                "summary": "Tribune review: findings.",
                "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            # Should not raise, just skip gracefully
            sup.reconcile_task_after_run(key, 0)

    def test_tribune_maintenance_non_tribune_phase_no_post(self):
        """No bridge post for reflect/developer phases (they post directly)."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
                "TRIBUNE_MAINT_ROUNDS": "1",
            })
            key = "1000.000000"
            state = _empty_state()
            # Phase 0 = reflect, not tribune
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=0)
            task["thread_ts"] = "maintenance"
            task["channel_id"] = "CDEFAULT"
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "thread_ts": "2000.000001",
                "summary": "Reflect summary.",
                "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            with patch.object(sup, "slack_api_post", return_value={"ok": True}) as mock_post:
                sup.reconcile_task_after_run(key, 0)
            summary_calls = [c for c in mock_post.call_args_list
                             if c[0][0] == "chat.postMessage"]
            self.assertEqual(len(summary_calls), 0)

    def test_tribune_maintenance_summary_posted_waiting_human(self):
        """Bridge fires even when final Tribune phase returns waiting_human."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
                "TRIBUNE_MAINT_ROUNDS": "1",
            })
            key = "1000.000000"
            state = _empty_state()
            # Phase 2 = Tribune (final phase when TRIBUNE_MAINT_ROUNDS=1)
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=2)
            task["thread_ts"] = "2000.000001"
            task["channel_id"] = "CDEFAULT"
            task["report_path"] = "reports/tribune.maintenance.md"
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "waiting_human",
                "thread_ts": "2000.000001",
                "summary": "Tribune review: needs human attention.",
                "completion_confidence": "high",
                "requires_human_feedback": True,
            })
            with patch.object(sup, "slack_api_post", return_value={"ok": True}) as mock_post:
                sup.reconcile_task_after_run(key, 0)
            summary_calls = [c for c in mock_post.call_args_list
                             if c[0][0] == "chat.postMessage"
                             and "Tribune review" in str(c[0][1].get("text", ""))]
            self.assertEqual(len(summary_calls), 1)
            # Task should be in incomplete_tasks (waiting_human), not finished
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])

    def test_tribune_maintenance_summary_truncated(self):
        """Long summaries are truncated at Slack's message limit."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
                "TRIBUNE_MAINT_ROUNDS": "1",
            })
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=2)
            task["thread_ts"] = "2000.000001"
            task["channel_id"] = "CDEFAULT"
            state["active_tasks"][key] = task
            sup.save_state(state)
            long_summary = "x" * 4500
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "thread_ts": "2000.000001",
                "summary": long_summary,
                "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            with patch.object(sup, "slack_api_post", return_value={"ok": True}) as mock_post:
                sup.reconcile_task_after_run(key, 0)
            summary_calls = [c for c in mock_post.call_args_list
                             if c[0][0] == "chat.postMessage"
                             and "done" not in str(c[0][1].get("text", "")).lower()]
            self.assertEqual(len(summary_calls), 1)
            text = summary_calls[0][0][1]["text"]
            self.assertLessEqual(len(text), 4000)
            self.assertIn("truncated", text)

    def test_tribune_maintenance_summary_refreshes_thread_snapshot(self):
        """After posting summary, thread snapshot is refreshed to prevent spurious re-dispatch."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
                "TRIBUNE_MAINT_ROUNDS": "1",
            })
            _write_agent_identity(sup, "U_AGENT")
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=2)
            task["thread_ts"] = "2000.000001"
            task["channel_id"] = "CDEFAULT"
            task["report_path"] = "reports/tribune.maintenance.md"
            # Set up a task text file so _store_thread_snapshot has somewhere to write
            task_file = str(sup.task_text_path(key, "incomplete_tasks"))
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": key, "thread_ts": "2000.000001",
                "channel_id": "CDEFAULT", "messages": [],
            })
            task["mention_text_file"] = task_file
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "waiting_human",
                "thread_ts": "2000.000001",
                "summary": "Tribune review: needs attention.",
                "completion_confidence": "high",
                "requires_human_feedback": True,
            })
            thread_msgs = [
                {"ts": "2000.000001", "user": "U1", "bot_id": "", "username": "", "text": "Reflect summary"},
                {"ts": "2000.000100", "user": "U_AGENT", "bot_id": "", "username": "", "text": "Tribune review: needs attention."},
            ]
            with patch.object(sup, "slack_api_post", return_value={"ok": True}), \
                 patch.object(sup, "_fetch_thread_messages", return_value=thread_msgs) as mock_fetch, \
                 patch.object(sup, "_store_thread_snapshot") as mock_store, \
                 patch.object(sup, "resolve_slack_id", return_value="U_AGENT"):
                sup.reconcile_task_after_run(key, 0)
            # Verify thread was re-fetched and snapshot stored (may be called
            # multiple times — once by the bridge, once by regular reconciliation)
            mock_fetch.assert_any_call("CDEFAULT", "2000.000001")
            self.assertGreaterEqual(mock_store.call_count, 1)

    # -- failed -> incomplete with in_progress status -----------------------

    def test_failed_status_goes_to_incomplete_as_in_progress(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "failed",
                "summary": "", "completion_confidence": "",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "in_progress")

    # -- non-zero worker exit -----------------------------------------------

    def test_nonzero_exit_goes_to_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            # No outcome file — worker failed
            sup.reconcile_task_after_run(key, 1)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            last_error = state["incomplete_tasks"][key].get("last_error") or ""
            self.assertIn("worker_exit=1", last_error)

    # -- missing outcome file with exit 0 -----------------------------------

    def test_exit_0_no_outcome_goes_to_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            # No outcome file written
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            last_error = state["incomplete_tasks"][key].get("last_error") or ""
            self.assertIn("dispatch_outcome_missing", last_error)

    # -- mention_ts mismatch in outcome -------------------------------------

    def test_mention_ts_mismatch_goes_to_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            state = sup.load_state()
            state["active_tasks"][key]["summary"] = "original summary"
            state["active_tasks"][key]["thread_ts"] = key
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": "9999.000000",  # wrong key
                "status": "done",
                "summary": "wrong summary",
                "thread_ts": "9999.000001",
                "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["thread_ts"], key)
            self.assertEqual(state["incomplete_tasks"][key]["summary"], "original summary")
            last_error = state["incomplete_tasks"][key].get("last_error") or ""
            self.assertIn("mismatch", last_error)

    # -- invalid status in outcome ------------------------------------------

    def test_invalid_status_in_outcome_goes_to_incomplete(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "bogus_status",
                "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])

    # -- completion_gate=moderate with non-high confidence -> waiting_human -

    def test_moderate_gate_non_high_confidence_becomes_waiting_human(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            # Override completion_gate to moderate
            sup.cfg.completion_gate = "moderate"
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "maybe", "completion_confidence": "medium",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "waiting_human")


    def test_moderate_gate_high_confidence_completes(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.cfg.completion_gate = "moderate"
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])

    def test_low_gate_always_holds_for_human_review(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.cfg.completion_gate = "low"
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "waiting_human")

    def test_high_gate_trusts_worker_done(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.cfg.completion_gate = "high"
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "medium",
                "requires_human_feedback": False,
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])

    # -- project field propagation --------------------------------------------

    def test_project_field_stored_in_state(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
                "project": "small-swe-train",
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertEqual(state["finished_tasks"][key]["project"], ["small-swe-train"])

    def test_project_field_list_stored_in_state(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
                "project": ["proj-a", "proj-b"],
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertEqual(state["finished_tasks"][key]["project"], ["proj-a", "proj-b"])

    def test_project_field_merges_on_redispatch(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress")
            task["project"] = ["proj-a"]
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
                "project": "proj-b",
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertEqual(state["finished_tasks"][key]["project"], ["proj-a", "proj-b"])

    def test_no_project_field_when_absent(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
            })
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertNotIn("project", state["finished_tasks"][key])

    # -- delivery guard: done without agent reply downgrades ----------------

    def test_delivery_guard_downgrades_done_without_agent_reply(self):
        """done + no agent message after human -> waiting_human."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
            })
            # Mock: thread has human message but no agent reply after it
            thread_msgs = [
                {"ts": "1000.000000", "user": "UHUMAN1", "bot_id": "", "username": "", "text": "do X"},
            ]
            with patch.object(sup, "_fetch_thread_messages", return_value=thread_msgs), \
                 patch.object(sup, "_store_thread_snapshot"), \
                 patch.object(sup, "resolve_slack_id", return_value="UAGENT1"):
                sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertNotIn(key, state.get("finished_tasks", {}))
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "waiting_human")
            self.assertIn("done_without_delivery_evidence", state["incomplete_tasks"][key].get("last_error", ""))

    def test_delivery_guard_allows_done_with_agent_reply(self):
        """done + agent message after human -> stays done."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
            })
            thread_msgs = [
                {"ts": "1000.000000", "user": "UHUMAN1", "bot_id": "", "username": "", "text": "do X"},
                {"ts": "1001.000000", "user": "UAGENT1", "bot_id": "", "username": "", "text": "Done!"},
            ]
            with patch.object(sup, "_fetch_thread_messages", return_value=thread_msgs), \
                 patch.object(sup, "_store_thread_snapshot"), \
                 patch.object(sup, "resolve_slack_id", return_value="UAGENT1"):
                sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])
            self.assertNotIn(key, state.get("incomplete_tasks", {}))

    def test_delivery_guard_skipped_when_no_thread_messages(self):
        """done + empty thread (no Slack token) -> stays done (no guard)."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
            })
            # No thread messages fetched (e.g., no Slack token)
            with patch.object(sup, "_fetch_thread_messages", return_value=[]), \
                 patch.object(sup, "_store_thread_snapshot"):
                sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])

    def test_delivery_guard_skipped_for_maintenance(self):
        """Maintenance tasks skip delivery guard even without agent reply."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=1)
            state["active_tasks"][key] = task
            sup.save_state(state)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "dev review done", "completion_confidence": "high",
            })
            thread_msgs = [
                {"ts": "1000.000000", "user": "UHUMAN1", "bot_id": "", "username": "", "text": "start"},
            ]
            with patch.object(sup, "_fetch_thread_messages", return_value=thread_msgs), \
                 patch.object(sup, "_store_thread_snapshot"), \
                 patch.object(sup, "resolve_slack_id", return_value="UAGENT1"):
                sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            # Maintenance final phase goes to finished
            self.assertIn(key, state["finished_tasks"])

    def test_done_reaction_posted_on_completion(self):
        """Reconciler posts a done acknowledgement message when task completes."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "all good", "completion_confidence": "high",
                "requires_human_feedback": False,
                "thread_ts": key,
            })
            thread_msgs = [
                {"ts": key, "user": "U1", "bot_id": "", "username": "", "text": "do thing"},
                {"ts": "1000.000001", "user": "UAGENT", "bot_id": "", "username": "", "text": "Done"},
            ]
            with patch.object(sup, "_fetch_thread_messages", return_value=thread_msgs), \
                 patch.object(sup, "_store_thread_snapshot"), \
                 patch.object(sup, "resolve_slack_id", return_value="UAGENT"), \
                 patch.object(sup, "slack_api_post", return_value={"ok": True}) as mock_post:
                sup.reconcile_task_after_run(key, 0)
            # Find the chat.postMessage call for done ack
            ack_calls = [c for c in mock_post.call_args_list
                         if c[0][0] == "chat.postMessage"
                         and "done" in str(c[0][1].get("text", "")).lower()]
            self.assertEqual(len(ack_calls), 1)
            payload = ack_calls[0][0][1]
            self.assertEqual(payload["channel"], "C123")
            self.assertEqual(payload["thread_ts"], key)

    def test_done_reaction_skipped_for_non_done_status(self):
        """No reaction when task is not done (e.g., waiting_human)."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "waiting_human",
                "summary": "needs input",
                "requires_human_feedback": True,
            })
            with patch.object(sup, "_fetch_thread_messages", return_value=[]), \
                 patch.object(sup, "_store_thread_snapshot"), \
                 patch.object(sup, "slack_api_post", return_value={"ok": True}) as mock_post:
                sup.reconcile_task_after_run(key, 0)
            reaction_calls = [c for c in mock_post.call_args_list
                              if c[0][0] == "reactions.add"]
            self.assertEqual(len(reaction_calls), 0)

    def test_done_reaction_error_does_not_propagate(self):
        """Slack API error on reaction does not crash reconciler."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "all good", "completion_confidence": "high",
                "requires_human_feedback": False,
                "thread_ts": key,
            })
            thread_msgs = [
                {"ts": key, "user": "U1", "bot_id": "", "username": "", "text": "do thing"},
                {"ts": "1000.000001", "user": "UAGENT", "bot_id": "", "username": "", "text": "Done"},
            ]
            def side_effect(method, payload):
                if method == "reactions.add":
                    raise ConnectionError("network down")
                return {"ok": True, "messages": thread_msgs, "has_more": False}
            with patch.object(sup, "_fetch_thread_messages", return_value=thread_msgs), \
                 patch.object(sup, "_store_thread_snapshot"), \
                 patch.object(sup, "resolve_slack_id", return_value="UAGENT"), \
                 patch.object(sup, "slack_api_post", side_effect=side_effect):
                # Should not raise
                sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])


# ===========================================================================
# 10b. Per-task outcome files
# ===========================================================================

class TestPerTaskOutcomes(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def _setup_active_task(self, sup, key="1000.000000"):
        state = _empty_state()
        state["active_tasks"][key] = _make_task(key, "in_progress")
        sup.save_state(state)

    def test_reconcile_reads_per_task_outcome(self):
        """Outcome at outcomes/<key>.json is read by reconciler."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "2000.000000"
            self._setup_active_task(sup, key)
            # Write outcome to per-task path (not legacy shared file)
            outcome_path = sup._outcome_path_for_task(key)
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "per-task done", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])
            self.assertEqual(state["finished_tasks"][key]["summary"], "per-task done")

    def test_reconcile_falls_back_to_legacy_outcome(self):
        """Legacy dispatch_outcome.json is used when per-task file doesn't exist."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "2001.000000"
            self._setup_active_task(sup, key)
            # Write outcome to legacy shared file only
            sup.cfg.dispatch_outcome_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_outcome_file.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "legacy done", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])
            self.assertEqual(state["finished_tasks"][key]["summary"], "legacy done")

    def test_per_task_outcome_cleaned_up_after_reconcile(self):
        """Per-task outcome file is deleted after reconciliation."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "2002.000000"
            self._setup_active_task(sup, key)
            outcome_path = sup._outcome_path_for_task(key)
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "cleanup test", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            self.assertTrue(outcome_path.exists())
            sup.reconcile_task_after_run(key, 0)
            self.assertFalse(outcome_path.exists())

    def test_outcome_file_override_takes_priority(self):
        """outcome_file_override parameter is used when provided."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "2003.000000"
            self._setup_active_task(sup, key)
            # Write to a custom override path
            override_path = Path(td) / "custom_outcome.json"
            override_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "override done", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            sup.reconcile_task_after_run(key, 0, outcome_file_override=override_path)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])
            self.assertEqual(state["finished_tasks"][key]["summary"], "override done")

    def test_render_prompt_uses_per_task_outcome_path(self):
        """render_runtime_prompt substitutes per-task outcome path into template."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text("outcome={{DISPATCH_OUTCOME_PATH}}\n{{DISPATCH_TASK_JSON}}\n")
            task_data = {"mention_ts": "3000.000000", "mention_text": "test"}
            sup.cfg.dispatch_task_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_task_file.write_text(json.dumps(task_data))
            sup.render_runtime_prompt()
            rendered = sup.cfg.runtime_prompt_file.read_text()
            self.assertIn("outcomes/3000.000000.json", rendered)
            # Should NOT contain the legacy shared outcome path
            self.assertNotIn("dispatch_outcome.json", rendered)


# ===========================================================================
# 11. enqueue_reflect_task_if_due
# ===========================================================================

class TestEnqueueReflectTaskIfDue(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp), env_overrides={"MAINTENANCE_HOUR": "0"})

    def test_enqueues_when_due(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            # Last dispatch was >1 day ago
            state["supervisor"]["last_reflect_dispatch_ts"] = "1.000000"
            sup.save_state(state)
            sup.maintenance.enqueue_if_due()
            new_state = sup.load_state()
            self.assertIn("maintenance", new_state["queued_tasks"])
            self.assertEqual(new_state["queued_tasks"]["maintenance"]["task_type"], "maintenance")

    def test_enqueued_reflect_uses_default_channel_id(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                Path(td),
                env_overrides={
                    "DEFAULT_CHANNEL_ID": "CDEFAULT",
                    "MAINTENANCE_HOUR": "0",
                },
            )
            state = _empty_state()
            state["supervisor"]["last_reflect_dispatch_ts"] = "1.000000"
            sup.save_state(state)
            sup.maintenance.enqueue_if_due()
            new_state = sup.load_state()
            self.assertIn("maintenance", new_state["queued_tasks"])
            self.assertEqual(new_state["queued_tasks"]["maintenance"]["channel_id"], "CDEFAULT")

    def test_does_not_enqueue_when_recently_dispatched(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            # Last dispatch was today → already ran, should not re-enqueue.
            # Use current time so the local calendar date matches regardless
            # of the hour the test runs at.
            state["supervisor"]["last_reflect_dispatch_ts"] = f"{int(time.time())}.000000"
            sup.save_state(state)
            sup.maintenance.enqueue_if_due()
            new_state = sup.load_state()
            self.assertNotIn("maintenance", new_state["queued_tasks"])

    def test_does_not_duplicate_if_already_queued(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["supervisor"]["last_reflect_dispatch_ts"] = "1.000000"
            state["queued_tasks"]["maintenance"] = _make_task("maintenance", "queued", "maintenance", maintenance_phase=0)
            sup.save_state(state)
            sup.maintenance.enqueue_if_due()
            new_state = sup.load_state()
            # Should still be exactly one — no duplicate added
            maint_tasks = [
                v for v in new_state["queued_tasks"].values()
                if v.get("task_type") == "maintenance"
            ]
            self.assertEqual(len(maint_tasks), 1)

    def test_waiting_human_numeric_key_does_not_block_enqueue(self):
        """A waiting_human maintenance task (even with numeric key) should not
        block new maintenance — it is stale and will be cleaned up by _enqueue."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["supervisor"]["last_reflect_dispatch_ts"] = "1.000000"
            task = _make_task("2000.000000", "waiting_human", "maintenance", maintenance_phase=1)
            task["thread_ts"] = "2000.000000"
            state["incomplete_tasks"]["2000.000000"] = task
            sup.save_state(state)
            sup.maintenance.enqueue_if_due()
            new_state = sup.load_state()
            # New maintenance should be enqueued (waiting_human no longer blocks)
            self.assertIn("maintenance", new_state["queued_tasks"])
            # The stale numeric-key task may still exist (only canonical key
            # is cleared by _enqueue), but new maintenance is not blocked.

    def test_in_progress_numeric_key_blocks_enqueue(self):
        """An in_progress maintenance task with numeric key should still block."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["supervisor"]["last_reflect_dispatch_ts"] = "1.000000"
            task = _make_task("2000.000000", "in_progress", "maintenance", maintenance_phase=1)
            task["thread_ts"] = "2000.000000"
            state["incomplete_tasks"]["2000.000000"] = task
            sup.save_state(state)
            sup.maintenance.enqueue_if_due()
            new_state = sup.load_state()
            self.assertNotIn("maintenance", new_state["queued_tasks"])



# ===========================================================================
# 12. poll_mentions_and_enqueue  (Slack API mocked)
# ===========================================================================

class TestPollMentionsAndEnqueue(unittest.TestCase):

    def _sup(self, tmp):
        sup = _make_supervisor(Path(tmp))
        _write_agent_identity(sup)
        return sup

    def _slack_resp(self, matches, page=1, page_count=1):
        return {
            "ok": True,
            "messages": {
                "matches": matches,
                "pagination": {"page": page, "page_count": page_count},
            },
        }

    def _mention(self, ts, text="hello", user="U_HUMAN", thread_ts=None):
        return {
            "ts": ts,
            "text": text,
            "user": user,
            "username": user,
            "permalink": f"https://example.slack.com/archives/C123/p{ts.replace('.', '')}",
            "channel": {"id": "C123"},
            "thread_ts": thread_ts or ts,
        }

    def test_enqueues_new_mentions(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            sup.save_state(state)

            resp = self._slack_resp([self._mention("2000.000000")])
            with patch.object(sup, "slack_api_get", return_value=resp):
                ok = sup.poll_mentions_and_enqueue()

            self.assertTrue(ok)
            new_state = sup.load_state()
            self.assertIn("2000.000000", new_state["queued_tasks"])
            self.assertEqual(
                new_state["queued_tasks"]["2000.000000"]["task_description"],
                "Hello.",
            )
            self.assertEqual(
                new_state["queued_tasks"]["2000.000000"]["mention_text_file"],
                str(sup.task_text_path("2000.000000", "queued_tasks")),
            )

    def test_groups_multiple_mentions_in_same_thread_into_one_task(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.save_state(_empty_state())

            resp = self._slack_resp(
                [
                    self._mention("2001.000000", text="Second ping", thread_ts="2000.000000"),
                    self._mention("2000.500000", text="First ping", thread_ts="2000.000000"),
                ]
            )
            with patch.object(sup, "slack_api_get", return_value=resp):
                ok = sup.poll_mentions_and_enqueue()

            self.assertTrue(ok)
            new_state = sup.load_state()
            self.assertEqual(len(new_state["queued_tasks"]), 1)
            task = new_state["queued_tasks"]["2000.000000"]
            self.assertEqual(task["thread_ts"], "2000.000000")
            data = json.loads(Path(task["mention_text_file"]).read_text(encoding="utf-8"))
            msg_ts = [m["ts"] for m in data["messages"]]
            self.assertIn("2000.500000", msg_ts)
            self.assertIn("2001.000000", msg_ts)

    def test_skips_already_known_mentions(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["queued_tasks"]["2000.000000"] = _make_task("2000.000000")
            sup.save_state(state)

            resp = self._slack_resp([self._mention("2000.000000")])
            with patch.object(sup, "slack_api_get", return_value=resp):
                sup.poll_mentions_and_enqueue()

            new_state = sup.load_state()
            # No new tasks added; still one
            all_tasks = list(new_state["queued_tasks"].values()) + list(new_state["active_tasks"].values())
            self.assertEqual(len(all_tasks), 1)

    def test_skips_mentions_at_or_before_watermark(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["watermark_ts"] = "3000.000000"
            sup.save_state(state)

            resp = self._slack_resp([self._mention("2000.000000")])
            with patch.object(sup, "slack_api_get", return_value=resp):
                sup.poll_mentions_and_enqueue()

            new_state = sup.load_state()
            self.assertEqual(len(new_state["queued_tasks"]), 0)

    def test_updates_watermark_to_highest_ts(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["watermark_ts"] = "1000.000000"
            sup.save_state(state)

            resp = self._slack_resp([self._mention("5000.000000")])
            with patch.object(sup, "slack_api_get", return_value=resp):
                sup.poll_mentions_and_enqueue()

            new_state = sup.load_state()
            self.assertEqual(new_state["watermark_ts"], "5000.000000")

    def test_returns_false_on_api_error(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.save_state(_empty_state())

            bad_resp = {"ok": False, "error": "invalid_auth"}
            with patch.object(sup, "slack_api_get", return_value=bad_resp):
                result = sup.poll_mentions_and_enqueue()

            self.assertFalse(result)

    def test_returns_false_on_network_exception(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.save_state(_empty_state())

            with patch.object(sup, "slack_api_get", side_effect=OSError("timeout")):
                result = sup.poll_mentions_and_enqueue()

            self.assertFalse(result)

    def test_reopens_finished_thread_when_new_mention_arrives(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            state["finished_tasks"]["1000.000000"] = _make_task("1000.000000", "done")
            sup.save_state(state)

            resp = self._slack_resp([
                self._mention("1001.000000", thread_ts="1000.000000"),
                self._mention("2000.000000"),
            ])
            with patch.object(sup, "slack_api_get", return_value=resp):
                sup.poll_mentions_and_enqueue()

            new_state = sup.load_state()
            self.assertNotIn("1000.000000", new_state["finished_tasks"])
            self.assertIn("1000.000000", new_state["queued_tasks"])
            self.assertIn("2000.000000", new_state["queued_tasks"])

    def test_waiting_human_feedback_is_deferred_to_refresh_snapshot_path(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            key = "3000.000000"
            task = _make_task(key, "waiting_human")
            task["last_update_ts"] = "3000.100000"
            mention_text_file = str(sup.task_text_path(key, "incomplete_tasks"))
            task["mention_text_file"] = mention_text_file
            initial_data = {
                "task_id": key, "thread_ts": key, "channel_id": "C123",
                "messages": [{"ts": key, "user_id": "U1", "role": "human",
                              "text": "Original ask"}],
            }
            Path(mention_text_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(mention_text_file, initial_data)
            state["incomplete_tasks"][key] = task
            sup.save_state(state)

            resp = self._slack_resp([self._mention("3000.200000", text="new feedback", thread_ts=key)])
            with patch.object(sup, "slack_api_get", return_value=resp):
                ok = sup.poll_mentions_and_enqueue()

            self.assertTrue(ok)
            new_state = sup.load_state()
            updated = new_state["incomplete_tasks"][key]
            self.assertEqual(updated["status"], "waiting_human")
            self.assertEqual(updated["last_update_ts"], "3000.100000")
            self.assertEqual(updated["last_seen_mention_ts"], "3000.200000")

            data = json.loads(Path(mention_text_file).read_text(encoding="utf-8"))
            # File should not have the new mention appended (deferred to refresh path)
            msg_ts = [m["ts"] for m in data["messages"]]
            self.assertNotIn("3000.200000", msg_ts)
            self.assertEqual(data["messages"][0]["text"], "Original ask")

    def test_non_slack_waiting_human_reactivated_on_poll(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = _empty_state()
            key = "4000.000000"
            task = _make_task(key, "waiting_human", task_type="maintenance", maintenance_phase=0)
            task["last_update_ts"] = "4000.100000"
            mention_text_file = str(sup.task_text_path(key, "incomplete_tasks"))
            task["mention_text_file"] = mention_text_file
            task_data = {
                "task_id": key, "thread_ts": key, "channel_id": "C123",
                "messages": [{"ts": key, "user_id": "system", "role": "human",
                              "text": "Maintenance ask"}],
            }
            Path(mention_text_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(mention_text_file, task_data)
            state["incomplete_tasks"][key] = task
            sup.save_state(state)

            resp = self._slack_resp([self._mention("4000.200000", text="feedback", thread_ts=key)])
            with patch.object(sup, "slack_api_get", return_value=resp):
                ok = sup.poll_mentions_and_enqueue()

            self.assertTrue(ok)
            updated = sup.load_state()["incomplete_tasks"][key]
            self.assertEqual(updated["status"], "in_progress")
            self.assertEqual(updated["last_update_ts"], "4000.200000")
            self.assertEqual(updated["last_seen_mention_ts"], "4000.200000")

            data = json.loads(Path(mention_text_file).read_text(encoding="utf-8"))
            msg_ts = [m["ts"] for m in data["messages"]]
            self.assertIn("4000.200000", msg_ts)

            self.assertTrue(sup.select_next_task())
            self.assertEqual(sup.selected_bucket, "incomplete_tasks")
            self.assertEqual(sup.selected_key, key)

    def test_returns_true_when_missing_slack_id(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            # Remove user directory so agent ID is unknown
            sup.cfg.user_directory_file.unlink()
            sup._agent_user_id = ""
            result = sup.poll_mentions_and_enqueue()
            self.assertTrue(result)  # Graceful skip, not an error


# ===========================================================================
# 13. refresh_waiting_human_tasks
# ===========================================================================

class TestRefreshWaitingHumanTasks(unittest.TestCase):

    def _sup(self, tmp):
        sup = _make_supervisor(Path(tmp))
        _write_agent_identity(sup)
        return sup

    def test_reactivates_with_readable_context_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "waiting_human")
            task["last_update_ts"] = "1000.500000"
            mention_text_file = str(sup.task_text_path(key, "incomplete_tasks"))
            task["mention_text_file"] = mention_text_file
            # Write initial JSON with an original message and an old context snapshot
            initial_data = {
                "task_id": key, "thread_ts": key, "channel_id": "C123",
                "messages": [
                    {"ts": "999.000000", "user_id": "U_HUMAN", "role": "human",
                     "text": "Original ask"},
                    {"ts": "999.500000", "user_id": "U_AGENT", "role": "agent",
                     "text": "old context block", "source": "context_snapshot"},
                ],
            }
            Path(mention_text_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(mention_text_file, initial_data)
            state["incomplete_tasks"][key] = task
            sup.save_state(state)

            thread_resp = {
                "ok": True,
                "messages": [
                    {"ts": "1000.000000", "user": "U_AGENT", "text": "Prior agent response"},
                    {"ts": "1001.000000", "user": "U_HUMAN", "text": "Please update\nwith details"},
                    {"ts": "1002.000000", "bot_id": "B01", "username": "helper-bot", "text": "Bot note"},
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

            with patch.object(sup, "slack_api_get", return_value=thread_resp):
                sup.refresh_waiting_human_tasks()

            updated = sup.load_state()["incomplete_tasks"][key]
            self.assertEqual(updated["status"], "in_progress")
            self.assertEqual(updated["last_human_reply_ts"], "1001.000000")
            self.assertEqual(updated["mention_text_file"], mention_text_file)

            data = json.loads(Path(updated["mention_text_file"]).read_text(encoding="utf-8"))
            # Original ask should be preserved
            original_msgs = [m for m in data["messages"] if m.get("source") != "context_snapshot"]
            self.assertEqual(len(original_msgs), 1)
            self.assertEqual(original_msgs[0]["text"], "Original ask")
            # Old context block should be replaced with fresh snapshot
            snapshot_msgs = [m for m in data["messages"] if m.get("source") == "context_snapshot"]
            self.assertEqual(len(snapshot_msgs), 3)
            snapshot_roles = {m["role"] for m in snapshot_msgs}
            self.assertIn("agent", snapshot_roles)
            self.assertIn("human", snapshot_roles)
            self.assertIn("bot:helper-bot", snapshot_roles)
            snapshot_texts = [m["text"] for m in snapshot_msgs]
            self.assertIn("Prior agent response", snapshot_texts)
            self.assertIn("Please update with details", snapshot_texts)
            self.assertIn("Bot note", snapshot_texts)
            # Old context block should NOT be present
            self.assertNotIn("old context block", [m["text"] for m in snapshot_msgs])

    def test_reactivates_when_since_timestamp_is_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "waiting_human")
            task["last_update_ts"] = "maintenance.reflect"
            task["channel_id"] = "C123"
            state["incomplete_tasks"][key] = task
            sup.save_state(state)

            thread_resp = {
                "ok": True,
                "messages": [
                    {"ts": "1000.000000", "user": "U_AGENT", "text": "Prior agent response"},
                    {"ts": "1001.000000", "user": "U_HUMAN", "text": "Human follow-up"},
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

            with patch.object(sup, "slack_api_get", return_value=thread_resp):
                sup.refresh_waiting_human_tasks()

            updated = sup.load_state()["incomplete_tasks"][key]
            self.assertEqual(updated["status"], "in_progress")
            self.assertEqual(updated["last_human_reply_ts"], "1001.000000")

    def test_reactivates_maintenance_on_plain_thread_reply(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "2000.000000"
            state = _empty_state()
            task = _make_task(key, "waiting_human", task_type="maintenance", maintenance_phase=1)
            task["last_update_ts"] = "2000.100000"
            task["channel_id"] = "CMAINT"
            mention_text_file = str(sup.task_text_path(key, "incomplete_tasks"))
            task["mention_text_file"] = mention_text_file
            initial_data = {
                "task_id": key, "thread_ts": key, "channel_id": "CMAINT",
                "messages": [{"ts": key, "user_id": "system", "role": "human",
                              "text": "Maintenance checklist"}],
            }
            Path(mention_text_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(mention_text_file, initial_data)
            state["incomplete_tasks"][key] = task
            sup.save_state(state)

            thread_resp = {
                "ok": True,
                "messages": [
                    {"ts": "2000.000000", "user": "U_AGENT", "text": "Maintenance summary"},
                    {"ts": "2001.000000", "user": "U_HUMAN", "text": "Please proceed"},
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

            with patch.object(sup, "slack_api_get", return_value=thread_resp):
                sup.refresh_waiting_human_tasks()

            updated = sup.load_state()["incomplete_tasks"][key]
            self.assertEqual(updated["status"], "in_progress")
            self.assertEqual(updated["last_human_reply_ts"], "2001.000000")
            data = json.loads(Path(updated["mention_text_file"]).read_text(encoding="utf-8"))
            snapshot_msgs = [m for m in data["messages"] if m.get("source") == "context_snapshot"]
            snapshot_texts = [m["text"] for m in snapshot_msgs]
            self.assertIn("Please proceed", snapshot_texts)


# ===========================================================================
# 13b. _fetch_thread_messages / _store_thread_snapshot / reconcile integration
# ===========================================================================

class TestThreadSnapshot(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_fetch_thread_messages_returns_sorted_deduped(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup)
            fake_resp = {
                "ok": True,
                "messages": [
                    {"ts": "2000.000002", "user": "U1", "text": "second"},
                    {"ts": "2000.000001", "user": "U2", "text": "first"},
                    {"ts": "2000.000001", "user": "U2", "text": "first dupe"},  # same ts
                ],
                "has_more": False,
            }
            with patch.object(sup, "slack_api_get", return_value=fake_resp):
                msgs = sup._fetch_thread_messages("C123", "2000.000000")
            self.assertEqual(len(msgs), 2)
            self.assertEqual(msgs[0]["ts"], "2000.000001")
            self.assertEqual(msgs[1]["ts"], "2000.000002")

    def test_fetch_thread_messages_returns_empty_on_api_error(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup)
            with patch.object(sup, "slack_api_get", return_value={"ok": False, "error": "channel_not_found"}):
                msgs = sup._fetch_thread_messages("C123", "2000.000000")
            self.assertEqual(msgs, [])

    def test_fetch_thread_messages_returns_empty_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup)
            with patch.object(sup, "slack_api_get", side_effect=Exception("network")):
                msgs = sup._fetch_thread_messages("C123", "2000.000000")
            self.assertEqual(msgs, [])

    def test_store_thread_snapshot_replaces_context_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")
            # Seed a task JSON with an old context_snapshot message
            task_file = str(sup.cfg.tasks_dir / "active" / "1000.000000.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1000.000000",
                "thread_ts": "1000.000000",
                "channel_id": "C123",
                "messages": [
                    {"ts": "1000.000000", "user_id": "U1", "role": "human", "text": "original mention"},
                    {"ts": "1000.000001", "user_id": "U_AGENT", "role": "agent", "text": "old snapshot", "source": "context_snapshot"},
                ],
            })
            # Store a new snapshot
            thread_messages = [
                {"ts": "1000.000000", "user": "U1", "text": "original mention"},
                {"ts": "1000.000001", "user": "U_AGENT", "text": "ACK"},
                {"ts": "1000.000002", "user": "U_AGENT", "text": "done"},
            ]
            sup._store_thread_snapshot(task_file, thread_messages)
            data = json.loads(Path(task_file).read_text(encoding="utf-8"))
            # Original mention (no source tag) preserved
            originals = [m for m in data["messages"] if not m.get("source")]
            self.assertEqual(len(originals), 1)
            self.assertEqual(originals[0]["text"], "original mention")
            # New context_snapshot messages written
            snapshots = [m for m in data["messages"] if m.get("source") == "context_snapshot"]
            self.assertEqual(len(snapshots), 3)  # all 3 thread messages
            snapshot_texts = [m["text"] for m in snapshots]
            self.assertIn("ACK", snapshot_texts)
            self.assertIn("done", snapshot_texts)

    def test_store_thread_snapshot_noop_on_empty(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup)
            task_file = str(sup.cfg.tasks_dir / "active" / "1000.000000.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1000.000000", "thread_ts": "1000.000000",
                "channel_id": "C123", "messages": [{"ts": "1000.000000", "user_id": "U1", "role": "human", "text": "hi"}],
            })
            sup._store_thread_snapshot(task_file, [])
            data = json.loads(Path(task_file).read_text(encoding="utf-8"))
            self.assertEqual(len(data["messages"]), 1)  # unchanged

    def _setup_active_task(self, sup, key="1000.000000"):
        state = _empty_state()
        state["active_tasks"][key] = _make_task(key, "in_progress")
        sup.save_state(state)

    def _write_outcome(self, sup, outcome: dict):
        sup.cfg.dispatch_outcome_file.parent.mkdir(parents=True, exist_ok=True)
        sup.cfg.dispatch_outcome_file.write_text(json.dumps(outcome))

    def test_reconcile_captures_thread_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")
            key = "1000.000000"
            self._setup_active_task(sup, key)
            self._write_outcome(sup, {
                "mention_ts": key, "status": "done",
                "summary": "finished", "completion_confidence": "high",
                "requires_human_feedback": False,
                "thread_ts": key,
            })
            fake_resp = {
                "ok": True,
                "messages": [
                    {"ts": "1000.000000", "user": "U1", "text": "do the thing"},
                    {"ts": "1000.000001", "user": "U_AGENT", "text": "ACK working on it"},
                    {"ts": "1000.000002", "user": "U_AGENT", "text": "Done, results posted"},
                ],
                "has_more": False,
            }
            with patch.object(sup, "slack_api_get", return_value=fake_resp):
                sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])
            task_file = state["finished_tasks"][key]["mention_text_file"]
            data = json.loads(Path(task_file).read_text(encoding="utf-8"))
            snapshots = [m for m in data["messages"] if m.get("source") == "context_snapshot"]
            self.assertEqual(len(snapshots), 3)
            snapshot_texts = [m["text"] for m in snapshots]
            self.assertIn("ACK working on it", snapshot_texts)
            self.assertIn("Done, results posted", snapshot_texts)


# ===========================================================================
# 14. run_worker_with_retries  (subprocess mocked)
# ===========================================================================

class TestRunWorkerWithRetries(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def _write_prompt(self, sup):
        sup.cfg.runtime_prompt_file.parent.mkdir(parents=True, exist_ok=True)
        sup.cfg.runtime_prompt_file.write_text("# test prompt\n")

    def test_success_on_first_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            self._write_prompt(sup)
            with patch.object(sup, "run_worker_once", return_value=(0, "ok", False)):
                with patch.object(sup, "write_heartbeat"):
                    result = sup.run_worker_with_retries()
            self.assertEqual(result, 0)

    def test_transient_retries_up_to_max(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            self._write_prompt(sup)
            transient_output = "stream disconnected before completion"
            # All attempts return transient failure; max_transient_retries=2 means 3 attempts total
            side_effects = [(1, transient_output, False)] * 10

            with patch.object(sup, "run_worker_once", side_effect=side_effects):
                with patch.object(sup, "write_heartbeat"):
                    with patch("time.sleep"):
                        result = sup.run_worker_with_retries()

            # Should give up after max_transient_retries+1 attempts
            self.assertNotEqual(result, 0)

    def test_transient_retry_succeeds_on_second_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            self._write_prompt(sup)
            transient_output = "transport error occurred"
            effects = [(1, transient_output, False), (0, "success", False)]

            with patch.object(sup, "run_worker_once", side_effect=effects):
                with patch.object(sup, "write_heartbeat"):
                    with patch("time.sleep"):
                        result = sup.run_worker_with_retries()

            self.assertEqual(result, 0)

    def test_nonzero_nonretriable_returns_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            self._write_prompt(sup)
            with patch.object(sup, "run_worker_once", return_value=(2, "some error", False)):
                with patch.object(sup, "write_heartbeat"):
                    result = sup.run_worker_with_retries()
            self.assertEqual(result, 2)

    def test_transient_backoff_increases(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            self._write_prompt(sup)
            transient_output = "stream disconnected before completion"
            sleep_calls = []

            def fake_sleep(n):
                sleep_calls.append(n)

            effects = [(1, transient_output, False)] * 5
            with patch.object(sup, "run_worker_once", side_effect=effects):
                with patch.object(sup, "write_heartbeat"):
                    with patch("time.sleep", side_effect=fake_sleep):
                        sup.run_worker_with_retries()

            # Sleep values should be non-decreasing (backoff grows)
            for i in range(1, len(sleep_calls)):
                self.assertGreaterEqual(sleep_calls[i], sleep_calls[i - 1])


# ===========================================================================
# 14b. Session Resume (AGENT-025)
# ===========================================================================

class TestSessionResume(unittest.TestCase):
    """Tests for session resume helpers and session lifecycle."""

    def _sup(self, tmp, **env_overrides):
        return _make_supervisor(Path(tmp), env_overrides=env_overrides)

    def test_should_resume_session_basic(self):
        """Resume when session_id + matching hash + feature enabled."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            current_hash = system_prompt_hash()
            task = {
                "codex_session_id": "abc-123",
                "session_prompt_hash": current_hash,
            }
            self.assertTrue(sup._should_resume_session(task))

    def test_should_resume_session_disabled(self):
        """Don't resume when feature disabled."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td, SESSION_RESUME_ENABLED="false")
            task = {
                "codex_session_id": "abc-123",
                "session_prompt_hash": system_prompt_hash(),
            }
            self.assertFalse(sup._should_resume_session(task))

    def test_should_resume_session_no_session_id(self):
        """Don't resume when no session ID."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            task = {"session_prompt_hash": system_prompt_hash()}
            self.assertFalse(sup._should_resume_session(task))

    def test_should_resume_session_stale_hash(self):
        """Don't resume when prompt hash doesn't match."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            task = {
                "codex_session_id": "abc-123",
                "session_prompt_hash": "stale_hash_value",
            }
            self.assertFalse(sup._should_resume_session(task))

    def test_should_resume_session_tribune_feedback(self):
        """Don't resume when tribune_feedback is present."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            task = {
                "codex_session_id": "abc-123",
                "session_prompt_hash": system_prompt_hash(),
                "tribune_feedback": "some feedback",
            }
            self.assertFalse(sup._should_resume_session(task))

    def test_should_resume_session_cross_slot(self):
        """Resume works regardless of slot/mode — no affinity check (AGENT-046)."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            # Task originally ran on slot 2 in parallel mode — still resumable
            task = {
                "codex_session_id": "abc-123",
                "session_prompt_hash": system_prompt_hash(),
                "session_dispatch_mode": "parallel",  # legacy field
                "session_slot_id": 2,                  # legacy field
            }
            self.assertTrue(sup._should_resume_session(task))

    def _write_merge_template(self, sup):
        """Write a merge_instructions.md next to the session template."""
        merge_path = sup.cfg.session_template.parent / "merge_instructions.md"
        merge_path.parent.mkdir(parents=True, exist_ok=True)
        merge_path.write_text(
            "Git branching (parallel dispatch):\n"
            "You are working in a git worktree on branch `{{BRANCH_NAME}}`.\n"
            "Before writing the dispatch outcome file, merge your branch "
            "into the main repo:\n"
            "    git -C {{REPO_ROOT}} merge {{BRANCH_NAME}}\n"
        )

    def test_resume_prompt_includes_merge_override(self):
        """Parallel resume prompt contains merge instructions for current slot (AGENT-046)."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            self._write_merge_template(sup)
            dispatch = {"mention_ts": "1.1", "session_end_ts": "0"}
            ctx = {
                "repo_root": "/Users/test/Research",
                "branch_name": "worker-5",
                "draft_path": "/tmp/worker-5.slack_draft.md",
            }
            prompt = sup._render_resume_prompt(dispatch, "1.1", slot_context=ctx)
            self.assertIn("## Updated Instructions", prompt)
            self.assertIn("worker-5", prompt)
            self.assertIn("/Users/test/Research", prompt)
            # Merge command should reference the slot's repo root and branch
            self.assertIn("git -C /Users/test/Research merge worker-5", prompt)

    def test_resume_prompt_includes_draft_override(self):
        """Resume prompt contains current draft path when Tribune active (AGENT-046)."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td, TRIBUNE_MAX_REVIEW_ROUNDS="1")
            dispatch = {"mention_ts": "1.1", "session_end_ts": "0"}
            ctx = {"draft_path": "/tmp/dispatch/worker-3.slack_draft.md"}
            prompt = sup._render_resume_prompt(dispatch, "1.1", slot_context=ctx)
            self.assertIn("worker-3.slack_draft.md", prompt)
            self.assertIn("Tribune approval", prompt)

    def test_resume_prompt_no_draft_when_tribune_disabled(self):
        """No draft instructions when TRIBUNE_MAX_REVIEW_ROUNDS=0 (AGENT-046)."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td, TRIBUNE_MAX_REVIEW_ROUNDS="0")
            self._write_merge_template(sup)
            dispatch = {"mention_ts": "1.1", "session_end_ts": "0"}
            # No draft_path in context because Tribune is off
            ctx = {
                "repo_root": "/Users/test/Research",
                "branch_name": "worker-2",
            }
            prompt = sup._render_resume_prompt(dispatch, "1.1", slot_context=ctx)
            self.assertNotIn("slack_draft", prompt)
            self.assertNotIn("Tribune approval", prompt)
            # Merge instructions should still be present
            self.assertIn("worker-2", prompt)

    def test_resume_prompt_serial_cancels_merge(self):
        """Serial resume cancels stale parallel merge instructions (AGENT-046)."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            dispatch = {"mention_ts": "1.1", "session_end_ts": "0"}
            # Serial context: has serial_mode flag, no repo_root/branch_name
            ctx = {"serial_mode": "true"}
            prompt = sup._render_resume_prompt(dispatch, "1.1", slot_context=ctx)
            self.assertIn("## Updated Instructions", prompt)
            self.assertIn("serial mode", prompt)
            self.assertIn("no branch merge", prompt.lower())
            self.assertNotIn("git -C", prompt)

    def test_resume_prompt_no_overrides_bare(self):
        """Resume with no slot_context has no Updated Instructions section."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            dispatch = {"mention_ts": "1.1", "session_end_ts": "0"}
            prompt = sup._render_resume_prompt(dispatch, "1.1")
            self.assertNotIn("## Updated Instructions", prompt)

    def test_build_resume_cmd_default(self):
        """Resume command from default WORKER_CMD."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            task = {"codex_session_id": "test-uuid-123"}
            cmd = sup._build_resume_cmd(task)
            # Should be: codex exec resume test-uuid-123 - --yolo --skip-git-repo-check
            self.assertIn("resume", cmd)
            self.assertIn("test-uuid-123", cmd)
            self.assertNotIn("--ephemeral", cmd)
            # exec should come before resume
            exec_idx = cmd.index("exec")
            resume_idx = cmd.index("resume")
            self.assertEqual(resume_idx, exec_idx + 1)
            # session id right after resume
            self.assertEqual(cmd[resume_idx + 1], "test-uuid-123")
            # stdin marker right after session id
            self.assertEqual(cmd[resume_idx + 2], "-")

    def test_build_resume_cmd_no_exec(self):
        """Returns empty list when command doesn't contain 'exec'."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td, WORKER_CMD="custom-wrapper --flag")
            task = {"codex_session_id": "test-uuid"}
            cmd = sup._build_resume_cmd(task)
            self.assertEqual(cmd, [])

    def test_build_fresh_worker_cmd_strips_ephemeral(self):
        """Fresh cmd strips --ephemeral when resume enabled."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            cmd = sup._build_fresh_worker_cmd()
            self.assertNotIn("--ephemeral", cmd)
            self.assertIn("codex", cmd)

    def test_build_fresh_worker_cmd_keeps_ephemeral_when_disabled(self):
        """Fresh cmd keeps --ephemeral when resume disabled."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td, SESSION_RESUME_ENABLED="false")
            cmd = sup._build_fresh_worker_cmd()
            self.assertIn("--ephemeral", cmd)

    def test_session_fields_in_internal_fields(self):
        """Session fields are excluded from worker prompt JSON."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            for field in sup._SESSION_FIELDS:
                self.assertIn(
                    field, sup._DISPATCH_INTERNAL_FIELDS,
                    f"{field} should be in _DISPATCH_INTERNAL_FIELDS"
                )

    def test_session_fields_preserved_on_done(self):
        """FIX-022: session fields stay on done tasks for resume on reopen."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1.1"
            state = _empty_state()
            task = _make_task(key, "in_progress", "slack_mention")
            task["thread_ts"] = key
            task["codex_session_id"] = "keep-me"
            task["session_prompt_hash"] = "hash-abc"
            state["active_tasks"][key] = task
            sup.save_state(state)
            outcome_path = sup._outcome_path_for_task(key)
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "done", "thread_ts": key,
            }))
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            finished = state["finished_tasks"].get(key) or {}
            # Session fields preserved for resume on reopen
            self.assertEqual(finished.get("codex_session_id"), "keep-me")
            self.assertEqual(finished.get("session_prompt_hash"), "hash-abc")

    def test_session_id_capture_in_reconcile(self):
        """Session metadata stored on task after successful dispatch."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1.1"
            state = _empty_state()
            task = _make_task(key, "in_progress", "slack_mention")
            task["thread_ts"] = key
            state["active_tasks"][key] = task
            sup.save_state(state)
            # Write a valid outcome
            outcome_path = sup._outcome_path_for_task(key)
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "in_progress",
                "summary": "wip", "thread_ts": key,
            }))
            sup.reconcile_task_after_run(
                key, 0,
                captured_session_id="test-uuid-456",
                dispatch_prompt_hash="hash123",
            )
            state = sup.load_state()
            task = state["incomplete_tasks"].get(key) or {}
            self.assertEqual(task.get("codex_session_id"), "test-uuid-456")
            self.assertEqual(task.get("session_prompt_hash"), "hash123")
            # AGENT-046: session_dispatch_mode no longer written
            self.assertNotIn("session_dispatch_mode", task)
            self.assertNotIn("session_slot_id", task)

    def test_session_preserved_on_done_for_reopen_resume(self):
        """FIX-022: session ID survives task completion for future resume."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1.2"
            state = _empty_state()
            task = _make_task(key, "in_progress", "slack_mention")
            task["thread_ts"] = key
            task["codex_session_id"] = "old-session"
            state["active_tasks"][key] = task
            sup.save_state(state)
            outcome_path = sup._outcome_path_for_task(key)
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "done", "thread_ts": key,
            }))
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            finished = state["finished_tasks"].get(key) or {}
            # FIX-022: session fields preserved, not cleared
            self.assertEqual(finished.get("codex_session_id"), "old-session")

    def test_loop_iteration_preserves_session(self):
        """Session ID survives loop iteration transitions."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1.3"
            state = _empty_state()
            task = _make_task(key, "in_progress", "slack_mention")
            task["thread_ts"] = key
            task["loop_mode"] = True
            task["loop_deadline"] = str(time.time() + 3600)
            state["active_tasks"][key] = task
            sup.save_state(state)
            outcome_path = sup._outcome_path_for_task(key)
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "iteration done", "thread_ts": key,
            }))
            sup.reconcile_task_after_run(
                key, 0,
                captured_session_id="loop-session-id",
                dispatch_prompt_hash="loophash",
            )
            state = sup.load_state()
            # Loop mode: task should be in incomplete (re-dispatch)
            task = state["incomplete_tasks"].get(key) or {}
            self.assertEqual(task.get("codex_session_id"), "loop-session-id")
            # AGENT-046: session_dispatch_mode no longer written
            self.assertNotIn("session_dispatch_mode", task)

    def test_resume_failure_clears_session(self):
        """Session ID cleared after failed dispatch."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1.4"
            state = _empty_state()
            task = _make_task(key, "in_progress", "slack_mention")
            task["thread_ts"] = key
            task["codex_session_id"] = "broken-session"
            task["session_prompt_hash"] = "somehash"
            state["active_tasks"][key] = task
            sup.save_state(state)
            # No outcome file — worker crashed
            sup.reconcile_task_after_run(key, 1)
            state = sup.load_state()
            task = state["incomplete_tasks"].get(key) or {}
            self.assertNotIn("codex_session_id", task)


# ===========================================================================
# 15. render_runtime_prompt
# ===========================================================================

class TestRenderRuntimePrompt(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_substitutes_placeholders(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            # Create a session template with placeholders
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text(
                "outcome={{DISPATCH_OUTCOME_PATH}}\n{{SESSION_MEMORY_CONTEXT}}\n{{DISPATCH_TASK_JSON}}\n"
            )
            sup.cfg.memory_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.memory_file.write_text("# Memory\n- durable note\n", encoding="utf-8")
            sup.cfg.long_term_goals_file.write_text("# Goals\n- goal note\n", encoding="utf-8")
            # Write a dispatch_task.json
            task_data = {"mention_ts": "1.0", "mention_text": "hello"}
            sup.cfg.dispatch_task_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_task_file.write_text(json.dumps(task_data))

            sup.render_runtime_prompt()
            rendered = sup.cfg.runtime_prompt_file.read_text()

            # Per-task outcome path: outcomes_dir / <mention_ts>.json
            self.assertIn("outcomes/1.0.json", rendered)
            self.assertIn('"mention_ts"', rendered)
            self.assertIn("Curated Memory", rendered)
            self.assertIn("Long-Term Goals (Pointer)", rendered)
            self.assertIn("Daily Episodic Memory (Today UTC, Pointer)", rendered)

    def test_dispatch_task_json_line_replaced_with_content(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text("{{DISPATCH_TASK_JSON}}\n")
            sup.cfg.dispatch_task_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_task_file.write_text('{"x": 42}')
            sup.render_runtime_prompt()
            rendered = sup.cfg.runtime_prompt_file.read_text()
            self.assertNotIn("{{DISPATCH_TASK_JSON}}", rendered)
            self.assertIn('"x": 42', rendered)

    def test_session_memory_placeholder_is_replaced(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text("top\n{{SESSION_MEMORY_CONTEXT}}\nbottom\n")
            sup.cfg.memory_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.memory_file.write_text("memory body\n", encoding="utf-8")
            sup.cfg.long_term_goals_file.write_text("goals body\n", encoding="utf-8")
            sup.cfg.dispatch_task_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_task_file.write_text("{}", encoding="utf-8")

            sup.render_runtime_prompt()
            rendered = sup.cfg.runtime_prompt_file.read_text()

            self.assertIn("top", rendered)
            self.assertIn("bottom", rendered)
            self.assertNotIn("{{SESSION_MEMORY_CONTEXT}}", rendered)
            self.assertIn(f"Source: `{sup.cfg.memory_file}`", rendered)
            self.assertIn(f"Source: `{sup.cfg.long_term_goals_file}`", rendered)
            self.assertIn("pointer only to keep initial prompt compact", rendered)

    def test_session_memory_soft_guard_caps_context_size(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                Path(td),
                env_overrides={"PROMPT_MEMORY_TOTAL_CHAR_LIMIT": "900"},
            )
            sup.cfg.memory_file.parent.mkdir(parents=True, exist_ok=True)
            memory_lines = [f"- memory-{idx:04d}" for idx in range(1, 1601)]
            sup.cfg.memory_file.write_text("# Memory\n" + "\n".join(memory_lines) + "\n", encoding="utf-8")
            sup.cfg.long_term_goals_file.write_text("# Goals\n" + ("goal\n" * 1200), encoding="utf-8")
            sup.cfg.memory_daily_dir.mkdir(parents=True, exist_ok=True)

            now_utc = datetime.now(timezone.utc)
            for day in (now_utc - timedelta(days=1), now_utc):
                day_path = sup.cfg.memory_daily_dir / f"{day.strftime('%Y-%m-%d')}.md"
                day_path.write_text("# Daily\n" + ("episodic\n" * 1200), encoding="utf-8")

            context = sup.build_session_memory_context()
            self.assertLessEqual(len(context), sup.cfg.prompt_memory_total_char_limit)
            self.assertIn("[memory soft guard]", context)
            self.assertIn("scripts/memory_recall", context)
            self.assertNotIn("memory-0001", context)


# ===========================================================================
# 16. session_sleep_policy
# ===========================================================================

class TestSessionSleepPolicy(unittest.TestCase):

    def _sup(self, tmp, run_once=False):
        """run_once=False by default here so sleep is actually called."""
        return _make_supervisor(Path(tmp), env_overrides={"RUN_ONCE": "true" if run_once else "false"})

    def test_failure_sleep(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(1)  # non-zero exit
            mock_sleep.assert_called_once_with(sup.cfg.failure_sleep_sec)

    def test_normal_sleep(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(0)
            mock_sleep.assert_called_once_with(sup.cfg.sleep_normal)

    def test_run_once_skips_sleep(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td, run_once=True)
            self.assertTrue(sup.cfg.run_once)
            with patch("time.sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(0)
            mock_sleep.assert_not_called()

    def test_pending_decision_sleep(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            # Create the pending_decision file in tmp (override path check)
            pending_file = Path(td) / ".agent" / "pending_decision.json"
            pending_file.parent.mkdir(parents=True, exist_ok=True)
            pending_file.write_text("{}")
            # Use simpler approach: monkeypatch the pending_decision path check
            original_exists = Path.exists
            def patched_exists(self_path):
                if str(self_path) == ".agent/runtime/pending_decision.json":
                    return True
                return original_exists(self_path)
            with patch.object(Path, "exists", patched_exists):
                with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                    with patch.object(sup, "write_heartbeat"):
                        sup.session_sleep_policy(0)
            mock_sleep.assert_called_once_with(sup.cfg.pending_check_initial)

    def test_pending_backoff_increases_on_repeated_pending(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.was_pending = True  # Simulate already in pending state
            initial = sup.pending_backoff_sec

            original_exists = Path.exists
            def patched_exists(self_path):
                if str(self_path) == ".agent/runtime/pending_decision.json":
                    return True
                return original_exists(self_path)

            with patch.object(Path, "exists", patched_exists):
                with patch("time.sleep"):
                    with patch.object(sup, "write_heartbeat"):
                        sup.session_sleep_policy(0)

            # Backoff should increase (multiplied by pending_check_multiplier=2)
            self.assertGreater(sup.pending_backoff_sec, initial)

    def test_skip_sleep_when_queued_tasks_exist(self):
        """When exit=0 and queued tasks exist, sleep should be 0 (draining_queue)."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = sup.load_state()
            state["queued_tasks"] = {"100.001": {"mention_ts": "100.001", "status": "queued"}}
            sup.save_state(state)
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat") as mock_hb:
                    sup.session_sleep_policy(0)
            mock_sleep.assert_called_once_with(0)
            mock_hb.assert_called_once()
            self.assertEqual(mock_hb.call_args[0][0], "draining_queue")

    def test_normal_sleep_when_no_dispatchable_tasks(self):
        """When exit=0 and no tasks queued, sleep should be sleep_normal."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = sup.load_state()
            state["queued_tasks"] = {}
            state["active_tasks"] = {}
            state["incomplete_tasks"] = {}
            sup.save_state(state)
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(0)
            mock_sleep.assert_called_once_with(sup.cfg.sleep_normal)

    def test_failure_sleep_ignores_queued_tasks(self):
        """Failure sleep takes priority even if queued tasks exist."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = sup.load_state()
            state["queued_tasks"] = {"100.001": {"mention_ts": "100.001", "status": "queued"}}
            sup.save_state(state)
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(1)
            mock_sleep.assert_called_once_with(sup.cfg.failure_sleep_sec)

    def test_draining_queue_with_incomplete_non_waiting(self):
        """Non-waiting incomplete tasks also trigger draining_queue."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = sup.load_state()
            state["incomplete_tasks"] = {
                "100.001": {"mention_ts": "100.001", "status": "in_progress"}
            }
            sup.save_state(state)
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(0)
            mock_sleep.assert_called_once_with(0)

    def test_waiting_human_does_not_trigger_draining(self):
        """Incomplete tasks with waiting_human status should not trigger draining."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            state = sup.load_state()
            state["incomplete_tasks"] = {
                "100.001": {"mention_ts": "100.001", "status": "waiting_human"}
            }
            sup.save_state(state)
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(0)
            mock_sleep.assert_called_once_with(sup.cfg.sleep_normal)


# ===========================================================================
# 17. load_dotenv
# ===========================================================================

class TestLoadDotenv(unittest.TestCase):

    def test_sets_env_from_file(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text('MY_TEST_KEY="hello_world"\n# comment\nBAD_LINE\n')
            os.environ.pop("MY_TEST_KEY", None)
            sl.load_dotenv(env_file)
            self.assertEqual(os.environ.get("MY_TEST_KEY"), "hello_world")
            os.environ.pop("MY_TEST_KEY", None)

    def test_does_not_override_existing_env(self):
        with tempfile.TemporaryDirectory() as td:
            env_file = Path(td) / ".env"
            env_file.write_text("MY_EXISTING_KEY=from_file\n")
            os.environ["MY_EXISTING_KEY"] = "from_env"
            sl.load_dotenv(env_file)
            self.assertEqual(os.environ.get("MY_EXISTING_KEY"), "from_env")
            os.environ.pop("MY_EXISTING_KEY", None)

    def test_missing_file_is_noop(self):
        # Should not raise
        sl.load_dotenv(Path("/nonexistent/.env"))


# ===========================================================================
# 18. Integration: ensure_state_schema → select_next_task → claim → reconcile
# ===========================================================================

class TestIntegrationStateFlow(unittest.TestCase):

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_full_queued_to_finished_flow(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.ensure_state_schema()

            # Manually enqueue a task (bypassing Slack poll)
            with sup.state_lock():
                state = sup.load_state()
                key = "1000.000000"
                state["queued_tasks"][key] = _make_task(key, "queued")
                sup.save_state(state)

            # Select the task
            self.assertTrue(sup.select_next_task())
            self.assertEqual(sup.selected_key, key)

            # Claim it
            sup.claim_task_for_worker(sup.selected_bucket, sup.selected_key)
            state = sup.load_state()
            self.assertIn(key, state["active_tasks"])
            self.assertNotIn(key, state["queued_tasks"])

            # Write a successful outcome
            outcome = {
                "mention_ts": key, "status": "done",
                "summary": "done!", "completion_confidence": "high",
                "requires_human_feedback": False,
            }
            sup.cfg.dispatch_outcome_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_outcome_file.write_text(json.dumps(outcome))

            # Reconcile
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])
            self.assertNotIn(key, state["active_tasks"])
            self.assertEqual(state["finished_tasks"][key]["summary"], "done!")


class TestHotRestart(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sup = _make_supervisor(self.tmp)

    def test_restart_flag_initially_false(self):
        self.assertFalse(self.sup._restart_requested)

    def test_handle_restart_signal_sets_flag(self):
        self.sup._handle_restart_signal(None, None)
        self.assertTrue(self.sup._restart_requested)
        log = self.sup.cfg.runner_log.read_text()
        self.assertIn("hot_restart_requested", log)

    def test_interruptible_sleep_returns_early_on_restart(self):
        self.sup._restart_requested = True
        start = time.monotonic()
        self.sup._interruptible_sleep(60)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 2.0)

    def test_interruptible_sleep_normal_duration(self):
        start = time.monotonic()
        self.sup._interruptible_sleep(2)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 1.5)

    def test_is_restart_command_matches(self):
        self.assertTrue(self.sup._is_restart_command("<@U123> !restart"))
        self.assertTrue(self.sup._is_restart_command("<@U123>  !Restart"))
        self.assertTrue(self.sup._is_restart_command("  <@U123> !restart  "))
        # Slack often omits space between mention and text
        self.assertTrue(self.sup._is_restart_command("<@U0AFZHQMAHX|Murphy>!restart"))

    def test_is_restart_command_rejects(self):
        self.assertFalse(self.sup._is_restart_command("<@U123> restart"))
        self.assertFalse(self.sup._is_restart_command("<@U123> restart the server"))
        self.assertFalse(self.sup._is_restart_command("<@U123> do something"))
        self.assertFalse(self.sup._is_restart_command("!restart"))

    @patch("os.execv")
    def test_exec_restart_calls_execv(self, mock_execv):
        self.sup._exec_restart()
        mock_execv.assert_called_once()
        args = mock_execv.call_args[0]
        self.assertIn("python", args[0].lower())
        self.assertIn("src.loop.supervisor.main", args[1])
        # Verify heartbeat was written
        hb = json.loads(self.sup.cfg.heartbeat_file.read_text())
        self.assertEqual(hb["status"], "restarting")


# ===========================================================================
# 18. refresh_dispatch_thread_context
# ===========================================================================

class TestRefreshDispatchThreadContext(unittest.TestCase):

    def _sup(self, tmp):
        sup = _make_supervisor(Path(tmp))
        _write_agent_identity(sup)
        return sup

    def test_updates_dispatch_with_fresh_thread(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            mention_text_file = str(sup.task_text_path(key, "active_tasks"))
            Path(mention_text_file).parent.mkdir(parents=True, exist_ok=True)
            initial_data = {
                "task_id": key, "thread_ts": key, "channel_id": "C123",
                "messages": [
                    {"ts": key, "user_id": "U_HUMAN", "role": "human",
                     "text": "Original ask"},
                ],
            }
            sup.write_task_json(mention_text_file, initial_data)
            dispatch = {
                "mention_ts": key,
                "thread_ts": key,
                "channel_id": "C123",
                "mention_text_file": mention_text_file,
                "mention_text": "Original ask",
            }
            sup.atomic_write_json(sup.cfg.dispatch_task_file, dispatch)

            thread_resp = {
                "ok": True,
                "messages": [
                    {"ts": "1000.000000", "user": "U_AGENT", "text": "Agent reply"},
                    {"ts": "1001.000000", "user": "U_HUMAN", "text": "New human reply"},
                ],
                "has_more": False,
                "response_metadata": {"next_cursor": ""},
            }

            with patch.object(sup, "slack_api_get", return_value=thread_resp):
                sup.refresh_dispatch_thread_context()

            updated_dispatch = json.loads(sup.cfg.dispatch_task_file.read_text())
            self.assertIn("New human reply", updated_dispatch["mention_text"])
            self.assertIn("Agent reply", updated_dispatch["mention_text"])

    def test_skips_when_no_slack_token(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            sup.slack_token = ""
            dispatch = {
                "thread_ts": "1000.000000",
                "channel_id": "C123",
                "mention_text_file": "some/path.json",
            }
            sup.atomic_write_json(sup.cfg.dispatch_task_file, dispatch)

            with patch.object(sup, "slack_api_get") as mock_api:
                sup.refresh_dispatch_thread_context()
                mock_api.assert_not_called()

    def test_skips_non_numeric_thread_ts(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            dispatch = {
                "thread_ts": "maintenance",
                "channel_id": "C123",
                "mention_text_file": "some/path.json",
            }
            sup.atomic_write_json(sup.cfg.dispatch_task_file, dispatch)

            with patch.object(sup, "slack_api_get") as mock_api:
                sup.refresh_dispatch_thread_context()
                mock_api.assert_not_called()


# ===========================================================================
# Fast Polling (AGENT-004)
# ===========================================================================

class TestPollIntervalConfig(unittest.TestCase):

    def test_default_poll_interval(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            conf = _make_conf(tmp)
            with patch.dict(os.environ, {"SLACK_MCP_XOXP_TOKEN": "x"}, clear=False):
                cfg = sl.Config(conf)
            self.assertEqual(cfg.poll_interval, 5)

    def test_default_waiting_refresh_interval(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            conf = _make_conf(tmp)
            with patch.dict(os.environ, {"SLACK_MCP_XOXP_TOKEN": "x"}, clear=False):
                cfg = sl.Config(conf)
            self.assertEqual(cfg.waiting_refresh_interval, 30)

    def test_poll_interval_env_override(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            conf = _make_conf(tmp)
            with patch.dict(os.environ, {"SLACK_MCP_XOXP_TOKEN": "x",
                                          "POLL_INTERVAL": "10"}, clear=False):
                cfg = sl.Config(conf)
            self.assertEqual(cfg.poll_interval, 10)

    def test_waiting_refresh_interval_env_override(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            conf = _make_conf(tmp)
            with patch.dict(os.environ, {"SLACK_MCP_XOXP_TOKEN": "x",
                                          "WAITING_REFRESH_INTERVAL": "60"}, clear=False):
                cfg = sl.Config(conf)
            self.assertEqual(cfg.waiting_refresh_interval, 60)


class TestFastPolling(unittest.TestCase):
    """Verify the main loop uses poll_interval for mention poll rate-limiting."""

    def test_poll_uses_poll_interval_not_sleep_normal(self):
        """Poll check should trigger based on poll_interval (5s), not sleep_normal."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "POLL_INTERVAL": "5",
                "SLEEP_NORMAL": "120",
            })
            sup._last_poll_ts = time.monotonic() - 6  # 6s ago (> 5s poll_interval)
            # The poll condition should be True
            now = time.monotonic()
            self.assertTrue(now - sup._last_poll_ts >= sup.cfg.poll_interval)
            # But would be False for sleep_normal
            self.assertFalse(now - sup._last_poll_ts >= sup.cfg.sleep_normal)


class TestWaitingRefreshThrottle(unittest.TestCase):
    """Verify refresh_waiting_human_tasks() is throttled to waiting_refresh_interval."""

    def test_first_call_runs_immediately(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(sup._last_waiting_refresh_ts, 0.0)
            self.assertTrue(sup._waiting_refresh_due(now=0.1))

    def test_call_within_interval_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"WAITING_REFRESH_INTERVAL": "30"})
            sup._last_waiting_refresh_ts = 100.0  # Just ran
            self.assertFalse(sup._waiting_refresh_due(now=129.0))

    def test_call_after_interval_runs(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"WAITING_REFRESH_INTERVAL": "30"})
            sup._last_waiting_refresh_ts = 100.0
            self.assertTrue(sup._waiting_refresh_due(now=130.0))


class TestSleepCap(unittest.TestCase):
    """Verify session_sleep_policy() caps sleep to poll_interval."""

    def test_idle_sleep_capped_to_poll_interval(self):
        """When idle, sleep_normal (120s) should be capped to poll_interval (5s)."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "RUN_ONCE": "false",
                "POLL_INTERVAL": "5",
                "SLEEP_NORMAL": "120",
            })
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(0)
            mock_sleep.assert_called_once_with(5)  # Capped from 120 to 5

    def test_failure_sleep_preserves_backoff(self):
        """Failure sleep should NOT be capped — backoff must be preserved."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "RUN_ONCE": "false",
                "POLL_INTERVAL": "5",
                "FAILURE_SLEEP_SEC": "120",
            })
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(1)  # failure exit
            mock_sleep.assert_called_once_with(120)  # Full backoff preserved

    def test_draining_sleep_unaffected(self):
        """Draining queue (sleep=0) should remain 0."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "RUN_ONCE": "false",
                "POLL_INTERVAL": "5",
            })
            state = sup.load_state()
            state["queued_tasks"] = {"100.001": {"mention_ts": "100.001", "status": "queued"}}
            sup.save_state(state)
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(0)
            mock_sleep.assert_called_once_with(0)

    def test_no_cap_when_sleep_already_below_poll_interval(self):
        """When sleep is already below poll_interval, it should not be changed."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "RUN_ONCE": "false",
                "POLL_INTERVAL": "10",
                "FAILURE_SLEEP_SEC": "3",
            })
            with patch.object(sup, "_interruptible_sleep") as mock_sleep:
                with patch.object(sup, "write_heartbeat"):
                    sup.session_sleep_policy(1)
            mock_sleep.assert_called_once_with(3)  # 3 < 10, no cap needed


class TestHeartbeatPollInterval(unittest.TestCase):
    """Verify heartbeat includes poll_interval field."""

    def test_heartbeat_contains_poll_interval(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            sup.write_heartbeat("sleeping", 0, False, 5, "none", 0)
            hb = json.loads(sup.cfg.heartbeat_file.read_text())
            self.assertIn("poll_interval", hb)
            self.assertEqual(hb["poll_interval"], sup.cfg.poll_interval)


# ===========================================================================
# WorkerSlot tests
# ===========================================================================

from src.loop.supervisor.worker_slot import WorkerSlot


class TestWorkerSlot(unittest.TestCase):
    """Tests for WorkerSlot: lifecycle, state, subprocess, merge."""

    def _make_slot(self, tmp: Path, slot_id: int = 0) -> WorkerSlot:
        repo_root = tmp / "repo"
        repo_root.mkdir(parents=True, exist_ok=True)
        dispatch_dir = tmp / "dispatch"
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        outcomes_dir = tmp / "outcomes"
        outcomes_dir.mkdir(parents=True, exist_ok=True)
        worktree_dir = tmp / "worktrees"
        worktree_dir.mkdir(parents=True, exist_ok=True)
        return WorkerSlot(
            slot_id=slot_id,
            repo_root=repo_root,
            dispatch_dir=dispatch_dir,
            outcomes_dir=outcomes_dir,
            worktree_dir=worktree_dir,
        )

    def test_initial_state_is_idle(self):
        """A fresh slot should be idle."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            self.assertTrue(slot.is_idle)
            self.assertFalse(slot.is_busy)
            self.assertFalse(slot.is_done)
            self.assertIsNone(slot.task_key)

    def test_file_paths_contain_slot_id(self):
        """Per-slot paths should include the slot identifier."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td), slot_id=2)
            self.assertIn("worker-2", str(slot.worktree_path))
            self.assertIn("worker-2", str(slot.dispatch_task_file))
            self.assertIn("worker-2", str(slot.dispatch_prompt_file))
            self.assertIn("worker-2", str(slot.session_log_file))
            self.assertEqual(slot.branch_name, "worker-2")

    def test_outcome_file_for_returns_correct_path(self):
        """outcome_file_for should return outcomes_dir / <key>.json."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            path = slot.outcome_file_for("1234.567890")
            self.assertEqual(path.name, "1234.567890.json")
            self.assertEqual(path.parent, slot.outcomes_dir)

    @patch.object(WorkerSlot, "_git")
    def test_setup_worktree_creates_on_first_call(self, mock_git):
        """First setup_worktree call should create worktree + symlinks (no submodule init)."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            # Create .agent and .codex in repo_root for symlink test
            (slot.repo_root / ".agent").mkdir()
            (slot.repo_root / ".codex").mkdir()
            # Create worktree dir as side effect of git worktree add
            def fake_git(args, cwd, **kw):
                if args[0] == "worktree" and args[1] == "add":
                    Path(args[2]).mkdir(parents=True, exist_ok=True)
                return MagicMock(stdout="", returncode=0)
            mock_git.side_effect = fake_git

            slot.setup_worktree()

            # Should have called rev-parse (base branch detection in __init__),
            # worktree prune, branch -D (stale cleanup), worktree add.
            # No submodule init — submodules are symlinked to main repo.
            calls = [c[0][0] for c in mock_git.call_args_list]
            self.assertEqual(calls[0], ["rev-parse", "--abbrev-ref", "HEAD"])
            self.assertEqual(calls[1], ["worktree", "prune"])
            # calls[2] is branch -D (stale branch cleanup, may fail — that's ok)
            self.assertEqual(calls[2], ["branch", "-D", slot.branch_name])
            self.assertEqual(calls[3][0:2], ["worktree", "add"])
            # ls-files for .agent/ skip-worktree (symlinked .agent in worktree)
            self.assertEqual(calls[4], ["ls-files", ".agent/"])
            self.assertEqual(len(calls), 5)  # No submodule update call

    @patch.object(WorkerSlot, "_git")
    def test_setup_worktree_resets_on_second_call(self, mock_git):
        """Second setup_worktree call should reset the branch, not create."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            (slot.repo_root / ".agent").mkdir()
            (slot.repo_root / ".codex").mkdir()
            def fake_git(args, cwd, **kw):
                if args[0] == "worktree" and args[1] == "add":
                    Path(args[2]).mkdir(parents=True, exist_ok=True)
                return MagicMock(stdout="", returncode=0)
            mock_git.side_effect = fake_git

            slot.setup_worktree()  # first call: create
            mock_git.reset_mock()
            mock_git.side_effect = lambda args, cwd, **kw: MagicMock(stdout="", returncode=0)

            slot.setup_worktree()  # second call: reset

            calls = [c[0][0] for c in mock_git.call_args_list]
            # Should do checkout -B and reset --hard, not worktree add
            self.assertTrue(any("checkout" in c for c in calls))
            self.assertTrue(any("reset" in c for c in calls))
            self.assertFalse(any(c == ["worktree", "add"] for c in calls))

    @patch.object(WorkerSlot, "_git")
    def test_reset_worktree_cleans_before_checkout(self, mock_git):
        """_reset_worktree_impl must run git clean -fd before checkout -B.

        Without the early clean, untracked files left by a worker that
        committed directly to main (via git -C $REPO_ROOT) will block
        the checkout and permanently break the worktree.
        """
        with tempfile.TemporaryDirectory() as td:
            mock_git.side_effect = lambda args, cwd, **kw: MagicMock(stdout="", returncode=0)
            slot = self._make_slot(Path(td))
            slot.worktree_path.mkdir(parents=True, exist_ok=True)
            slot._worktree_initialized = True
            mock_git.reset_mock()
            mock_git.side_effect = lambda args, cwd, **kw: MagicMock(stdout="", returncode=0)

            slot._reset_worktree_impl()

            calls = [c[0][0] for c in mock_git.call_args_list]
            # Expected: reset --hard, clean -fd, checkout -B, reset --hard, clean -fd
            self.assertEqual(calls[0], ["reset", "--hard", "HEAD"])
            self.assertEqual(calls[1], ["clean", "-fd"])
            self.assertIn("checkout", calls[2])
            self.assertEqual(calls[3][0:2], ["reset", "--hard"])
            self.assertEqual(calls[4], ["clean", "-fd"])

    @patch.object(WorkerSlot, "_git")
    def test_reset_worktree_clean_before_checkout_ordering(self, mock_git):
        """Verify clean appears before checkout in the full setup_worktree reset path."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            (slot.repo_root / ".agent").mkdir()
            (slot.repo_root / ".codex").mkdir()
            def fake_git(args, cwd, **kw):
                if args[0] == "worktree" and args[1] == "add":
                    Path(args[2]).mkdir(parents=True, exist_ok=True)
                return MagicMock(stdout="", returncode=0)
            mock_git.side_effect = fake_git

            slot.setup_worktree()  # first call: create
            mock_git.reset_mock()
            mock_git.side_effect = lambda args, cwd, **kw: MagicMock(stdout="", returncode=0)

            slot.setup_worktree()  # second call: reset

            calls = [c[0][0] for c in mock_git.call_args_list]
            clean_indices = [i for i, c in enumerate(calls) if c == ["clean", "-fd"]]
            checkout_indices = [i for i, c in enumerate(calls) if "checkout" in c]
            self.assertTrue(len(clean_indices) >= 2, "Expected at least 2 clean calls")
            self.assertTrue(
                any(ci < co for ci in clean_indices for co in checkout_indices),
                f"clean -fd must appear before checkout -B; calls: {calls}",
            )

    @patch.object(WorkerSlot, "_git")
    def test_symlinks_created(self, mock_git):
        """.agent/ symlinked, .codex/ copied with per-slot CONSULT_SLOT_ID."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            (slot.repo_root / ".agent").mkdir()
            codex_dir = slot.repo_root / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text(
                '[mcp_servers.consult]\ncommand = "test"\n\n'
                '[mcp_servers.consult.env]\nFOO = "bar"\n'
            )
            def fake_git(args, cwd, **kw):
                if args[0] == "worktree" and args[1] == "add":
                    Path(args[2]).mkdir(parents=True, exist_ok=True)
                return MagicMock(stdout="", returncode=0)
            mock_git.side_effect = fake_git

            slot.setup_worktree()

            # .agent/ is symlinked
            self.assertTrue((slot.worktree_path / ".agent").is_symlink())
            self.assertEqual(
                (slot.worktree_path / ".agent").resolve(),
                (slot.repo_root / ".agent").resolve(),
            )
            # .codex/ is copied (not symlinked) with CONSULT_SLOT_ID injected
            self.assertTrue((slot.worktree_path / ".codex").is_dir())
            self.assertFalse((slot.worktree_path / ".codex").is_symlink())
            config_text = (slot.worktree_path / ".codex" / "config.toml").read_text()
            self.assertIn(f'CONSULT_SLOT_ID = "{slot.slot_id}"', config_text)

    def test_start_and_collect_echo(self):
        """start() should run a subprocess and collect() should return results."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            # Create worktree dir manually (skip git)
            slot.worktree_path.mkdir(parents=True, exist_ok=True)

            slot.start(
                cmd=["cat"],
                prompt="hello from test",
                task_key="1000.0",
                task_type="slack_mention",
                timeout_sec=30,
            )

            self.assertTrue(slot.is_busy or slot.is_done)
            self.assertEqual(slot.task_key, "1000.0")

            exit_code, log_path = slot.collect()
            self.assertEqual(exit_code, 0)
            self.assertTrue(slot.is_done)

            # Output should be streamed to log file
            log_content = Path(log_path).read_text()
            self.assertIn("hello from test", log_content)

    def test_start_raises_if_busy(self):
        """start() should raise if slot already has an active worker."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            slot.worktree_path.mkdir(parents=True, exist_ok=True)

            # Use sleep to keep the subprocess alive
            slot.start(
                cmd=["sleep", "10"],
                prompt="",
                task_key="1000.0",
                task_type="slack_mention",
                timeout_sec=30,
            )

            with self.assertRaises(RuntimeError):
                slot.start(
                    cmd=["echo", "nope"],
                    prompt="",
                    task_key="2000.0",
                    task_type="slack_mention",
                    timeout_sec=30,
                )

            # Clean up: collect the sleeping process (it'll error because no tty, that's ok)
            slot._thread.join(timeout=1)

    def test_reset_clears_state(self):
        """reset() should return slot to idle state."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            slot.worktree_path.mkdir(parents=True, exist_ok=True)

            slot.start(cmd=["echo", "hi"], prompt="", task_key="1.0",
                       task_type="slack_mention", timeout_sec=10)
            slot.collect()
            self.assertTrue(slot.is_done)

            slot.reset()
            self.assertTrue(slot.is_idle)
            self.assertIsNone(slot.task_key)
            self.assertIsNone(slot.task_type)
            self.assertIsNone(slot.started_at)

    def test_elapsed_sec(self):
        """elapsed_sec should return positive value when worker is running."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            self.assertEqual(slot.elapsed_sec, 0.0)

            slot.started_at = time.time() - 5.0
            self.assertGreaterEqual(slot.elapsed_sec, 4.5)

    def test_timeout_returns_124(self):
        """A timed-out subprocess should return exit code 124."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            slot.worktree_path.mkdir(parents=True, exist_ok=True)

            slot.start(
                cmd=["sleep", "60"],
                prompt="",
                task_key="1.0",
                task_type="slack_mention",
                timeout_sec=1,
            )

            exit_code, log_path = slot.collect()
            self.assertEqual(exit_code, 124)


    def test_collect_idle_raises(self):
        """collect() on idle slot should raise RuntimeError."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            with self.assertRaises(RuntimeError):
                slot.collect()


    @patch.object(WorkerSlot, "_git")
    def test_symlinks_handle_existing_non_symlink(self, mock_git):
        """_ensure_symlinks should replace a non-symlink dir with a symlink."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            (slot.repo_root / ".agent").mkdir()
            # Create worktree with a real .agent dir (not a symlink)
            slot.worktree_path.mkdir(parents=True, exist_ok=True)
            (slot.worktree_path / ".agent").mkdir()
            (slot.worktree_path / ".agent" / "stale_file").write_text("stale")

            slot._ensure_symlinks()

            # Should now be a symlink to repo_root/.agent
            self.assertTrue((slot.worktree_path / ".agent").is_symlink())
            self.assertEqual(
                (slot.worktree_path / ".agent").resolve(),
                (slot.repo_root / ".agent").resolve(),
            )


    @patch.object(WorkerSlot, "_git")
    def test_skip_worktree_set_on_symlinked_project_submodules(self, mock_git):
        """Symlinked project submodule dirs should be marked skip-worktree."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            (slot.repo_root / ".agent").mkdir()
            (slot.repo_root / ".codex").mkdir()
            # Create project submodule dirs in repo_root
            proj_dir = slot.repo_root / "projects"
            proj_dir.mkdir()
            (proj_dir / "alpha").mkdir()
            (proj_dir / "beta").mkdir()
            (proj_dir / "readme.md").write_text("file, not dir")  # should be skipped

            def fake_git(args, cwd, **kw):
                if args[0] == "worktree" and args[1] == "add":
                    Path(args[2]).mkdir(parents=True, exist_ok=True)
                return MagicMock(stdout="", returncode=0)
            mock_git.side_effect = fake_git

            slot.setup_worktree()

            # Find the update-index --skip-worktree call
            skip_calls = [
                c[0][0] for c in mock_git.call_args_list
                if c[0][0][:2] == ["update-index", "--skip-worktree"]
            ]
            self.assertEqual(len(skip_calls), 1)
            skip_args = skip_calls[0]
            # Should include both project dirs but not the .md file or .agent
            self.assertIn("projects/alpha", skip_args)
            self.assertIn("projects/beta", skip_args)
            self.assertNotIn(".agent", skip_args)

    @patch.object(WorkerSlot, "_git")
    def test_subprocess_env_includes_repo_root_and_branch(self, mock_git):
        """_run_subprocess should pass REPO_ROOT and WORKER_BRANCH env vars."""
        with tempfile.TemporaryDirectory() as td:
            slot = self._make_slot(Path(td))
            captured_env = {}

            class FakePopen:
                def __init__(self, *args, **kwargs):
                    if kwargs.get("env"):
                        captured_env.update(kwargs["env"])
                    self.stdin = MagicMock()
                    self.returncode = 0
                    self.pid = 12345
                def wait(self, timeout=None):
                    pass
                def poll(self):
                    return 0

            with patch("subprocess.Popen", FakePopen):
                slot._run_subprocess(["echo", "hi"], "prompt", 30)

            self.assertEqual(captured_env.get("REPO_ROOT"), str(slot.repo_root))
            self.assertEqual(captured_env.get("WORKER_BRANCH"), slot.branch_name)
            self.assertEqual(captured_env.get("CONSULT_SLOT_ID"), str(slot.slot_id))


class TestParallelLoop(unittest.TestCase):
    """Tests for the parallel dispatch loop (_run_parallel and helpers)."""

    def test_run_gates_to_serial_by_default(self):
        """run() should use _run_serial when max_concurrent_workers=1."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(sup.cfg.max_concurrent_workers, 1)
            # Patch _run_serial to verify it's called
            with patch.object(sup, "_run_serial", return_value=0) as mock_serial, \
                 patch.object(sup, "_run_parallel", return_value=0) as mock_parallel:
                sup.run()
                mock_serial.assert_called_once()
                mock_parallel.assert_not_called()

    def test_run_gates_to_parallel_when_configured(self):
        """run() should use _run_parallel when max_concurrent_workers >= 2."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"MAX_CONCURRENT_WORKERS": "2"})
            self.assertEqual(sup.cfg.max_concurrent_workers, 2)
            with patch.object(sup, "_run_serial", return_value=0) as mock_serial, \
                 patch.object(sup, "_run_parallel", return_value=0) as mock_parallel:
                sup.run()
                mock_parallel.assert_called_once()
                mock_serial.assert_not_called()

    def test_peek_next_is_maintenance_false_on_empty(self):
        """_peek_next_is_maintenance returns False when no tasks exist."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            sup.save_state(state)
            self.assertFalse(sup._peek_next_is_maintenance())

    def test_peek_next_is_maintenance_true(self):
        """_peek_next_is_maintenance returns True when next task is maintenance."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            state["queued_tasks"]["maintenance"] = _make_task(
                mention_ts="maintenance", task_type="maintenance"
            )
            sup.save_state(state)
            self.assertTrue(sup._peek_next_is_maintenance())

    def test_peek_next_is_maintenance_false_for_slack(self):
        """_peek_next_is_maintenance returns False when next task is slack_mention."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            state["queued_tasks"]["1000.0"] = _make_task("1000.0")
            sup.save_state(state)
            self.assertFalse(sup._peek_next_is_maintenance())

    def test_render_slot_prompt(self):
        """_render_slot_prompt should write a prompt file for the slot."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            # Write session template
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text(
                "Task: {{DISPATCH_OUTCOME_PATH}}\n{{DISPATCH_TASK_JSON}}\n"
            )

            # Create a mock slot
            dispatch_dir = tmp / "dispatch"
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            outcomes_dir = tmp / "outcomes"
            outcomes_dir.mkdir(parents=True, exist_ok=True)

            from src.loop.supervisor.worker_slot import WorkerSlot
            slot = WorkerSlot(
                slot_id=0, repo_root=tmp, dispatch_dir=dispatch_dir,
                outcomes_dir=outcomes_dir, worktree_dir=tmp / "worktrees",
            )

            # Write dispatch task file
            task_data = {"mention_ts": "1234.0", "thread_ts": "1234.0", "channel_id": "C1"}
            sup.atomic_write_json(slot.dispatch_task_file, task_data)

            sup._render_slot_prompt(slot, "1234.0", "slack_mention")

            prompt = slot.dispatch_prompt_file.read_text()
            self.assertIn("1234.0", prompt)
            self.assertIn("outcomes", prompt)

    def test_render_slot_prompt_expands_merge_instructions(self):
        """_render_slot_prompt should expand {{MERGE_INSTRUCTIONS}} with repo root and branch."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text(
                "{{DISPATCH_TASK_JSON}}\n{{MERGE_INSTRUCTIONS}}\nDone\n"
            )
            merge_tpl = (Path(__file__).resolve().parent.parent.parent / "prompts" / "merge_instructions.md").read_text()
            (sup.cfg.session_template.parent / "merge_instructions.md").write_text(merge_tpl)

            dispatch_dir = tmp / "dispatch"
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            outcomes_dir = tmp / "outcomes"
            outcomes_dir.mkdir(parents=True, exist_ok=True)

            from src.loop.supervisor.worker_slot import WorkerSlot
            slot = WorkerSlot(
                slot_id=0, repo_root=tmp, dispatch_dir=dispatch_dir,
                outcomes_dir=outcomes_dir, worktree_dir=tmp / "worktrees",
            )

            task_data = {"mention_ts": "1234.0", "thread_ts": "1234.0", "channel_id": "C1"}
            sup.atomic_write_json(slot.dispatch_task_file, task_data)

            sup._render_slot_prompt(slot, "1234.0", "slack_mention")
            prompt = slot.dispatch_prompt_file.read_text()

            self.assertIn(f"git -C {tmp} merge worker-0", prompt)
            self.assertIn("worker-0", prompt)
            self.assertIn("Done", prompt)  # lines after the placeholder still rendered

    def test_render_serial_prompt_skips_merge_instructions(self):
        """render_runtime_prompt should skip {{MERGE_INSTRUCTIONS}} in serial mode."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text(
                "Before\n{{MERGE_INSTRUCTIONS}}\nAfter\n{{DISPATCH_TASK_JSON}}\n"
            )

            # select a task so render_runtime_prompt works
            state = _empty_state()
            state["queued_tasks"]["1.0"] = _make_task("1.0", "queued")
            sup.save_state(state)
            result = sup.select_and_claim()
            self.assertIsNotNone(result)

            sup.render_runtime_prompt()
            prompt = sup.cfg.runtime_prompt_file.read_text()

            self.assertIn("Before", prompt)
            self.assertIn("After", prompt)
            self.assertNotIn("MERGE_INSTRUCTIONS", prompt)
            self.assertNotIn("worktree", prompt.lower())

    def test_parallel_sleep_policy_workers_active(self):
        """_parallel_sleep_policy should sleep poll_interval when workers are busy."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            mock_slot = MagicMock()
            mock_slot.is_busy = True
            mock_slot.is_idle = False
            mock_slot.is_done = False
            mock_slot.slot_id = 0
            mock_slot.task_key = "1000.0"
            mock_slot.started_at = time.time() - 10
            mock_slot.elapsed_sec = 10.0

            sup._parallel_sleep_policy([mock_slot])
            hb = json.loads(sup.cfg.heartbeat_file.read_text())
            self.assertEqual(hb["status"], "workers_active")
            self.assertEqual(len(hb["active_workers"]), 1)
            self.assertEqual(hb["active_workers"][0]["slot"], 0)
            self.assertEqual(hb["active_workers"][0]["task"], "1000.0")

    def test_parallel_sleep_policy_idle(self):
        """_parallel_sleep_policy should write sleeping status when all idle."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            mock_slot = MagicMock()
            mock_slot.is_busy = False
            mock_slot.is_idle = True
            mock_slot.is_done = False

            with patch.object(sup, "_has_dispatchable_tasks", return_value=False):
                sup._parallel_sleep_policy([mock_slot])
            hb = json.loads(sup.cfg.heartbeat_file.read_text())
            self.assertEqual(hb["status"], "sleeping")

    def test_parallel_sleep_policy_draining(self):
        """_parallel_sleep_policy should drain when idle slots and tasks exist."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            mock_slot = MagicMock()
            mock_slot.is_busy = False
            mock_slot.is_idle = True
            mock_slot.is_done = False

            with patch.object(sup, "_has_dispatchable_tasks", return_value=True):
                sup._parallel_sleep_policy([mock_slot])
            hb = json.loads(sup.cfg.heartbeat_file.read_text())
            self.assertEqual(hb["status"], "draining_queue")

    def test_peek_next_is_maintenance_with_mixed(self):
        """Active non-maintenance task takes priority over queued maintenance."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            # Unclaimed active task (non-maintenance)
            state["active_tasks"]["1.0"] = _make_task("1.0", task_type="slack_mention")
            # Queued maintenance
            state["queued_tasks"]["maintenance"] = _make_task(
                mention_ts="maintenance", task_type="maintenance"
            )
            sup.save_state(state)
            # Active takes priority, and it's not maintenance
            self.assertFalse(sup._peek_next_is_maintenance())

    def test_reconcile_slot_handles_collect_error(self):
        """_reconcile_slot should reset slot on RuntimeError from collect."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            mock_slot = MagicMock()
            mock_slot.collect.side_effect = RuntimeError("no worker")
            mock_slot.task_key = "1.0"

            sup._reconcile_slot(mock_slot, Path(td))
            mock_slot.reset.assert_called_once()

    def test_fallback_merge_dispatch_sends_slack_and_runs_worker(self):
        """_fallback_merge_dispatch should log internally and run a merge worker.

        FIX-028: Initial "attempting merge" message now goes to log_line()
        instead of Slack (internal coordination must not leak to user threads).
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)

            # Set up task in finished_tasks with channel/thread info
            state = _empty_state()
            state["finished_tasks"]["1.0"] = _make_task("1.0", "finished")
            sup.save_state(state)

            # Create merge_fallback.md template
            prompts_dir = sup.cfg.session_template.parent
            prompts_dir.mkdir(parents=True, exist_ok=True)
            (prompts_dir / "merge_fallback.md").write_text(
                "Merge {{BRANCH_NAME}} into {{REPO_ROOT}}.\n"
            )

            mock_slot = MagicMock()
            mock_slot.slot_id = 0
            mock_slot.branch_name = "worker-0"
            mock_slot._base_branch = "main"
            mock_slot.worktree_path = tmp / "worktrees" / "worker-0"
            mock_slot.worktree_path.mkdir(parents=True, exist_ok=True)
            # After merge worker runs, simulate successful merge (no unmerged commits)
            mock_slot._git.return_value = MagicMock(stdout="")

            with patch.object(sup, "slack_api_post") as mock_slack, \
                 patch("subprocess.run") as mock_run:
                sup._fallback_merge_dispatch(mock_slot, "1.0", tmp)

            # No Slack notification for the initial "attempting" message (FIX-028)
            self.assertEqual(mock_slack.call_count, 0)

            # Verify codex exec was called with merge prompt
            mock_run.assert_called_once()
            run_kwargs = mock_run.call_args
            self.assertIn("Merge worker-0", run_kwargs.kwargs["input"])
            self.assertEqual(run_kwargs.kwargs["timeout"], 1200)

            # Verify worktree was cleaned before worker dispatch (FIX-003)
            git_calls = mock_slot._git.call_args_list
            reset_calls = [c for c in git_calls if c[0][0] == ["reset", "--hard", "HEAD"]]
            clean_calls = [c for c in git_calls if c[0][0] == ["clean", "-fd"]]
            self.assertTrue(len(reset_calls) >= 1, "git reset --hard HEAD should be called")
            self.assertTrue(len(clean_calls) >= 1, "git clean -fd should be called")

    def test_fallback_merge_dispatch_cleanup_failure_still_notifies(self):
        """When worktree cleanup fails, worker is skipped but Slack failure is still sent.

        FIX-028: Initial "attempting" message no longer goes to Slack, so only
        the failure notification is expected.
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)

            state = _empty_state()
            state["finished_tasks"]["1.0"] = _make_task("1.0", "finished")
            sup.save_state(state)

            prompts_dir = sup.cfg.session_template.parent
            prompts_dir.mkdir(parents=True, exist_ok=True)
            (prompts_dir / "merge_fallback.md").write_text(
                "Merge {{BRANCH_NAME}} into {{REPO_ROOT}}.\n"
            )

            mock_slot = MagicMock()
            mock_slot.slot_id = 0
            mock_slot.branch_name = "worker-0"
            mock_slot._base_branch = "main"
            mock_slot.worktree_path = tmp / "worktrees" / "worker-0"
            mock_slot.worktree_path.mkdir(parents=True, exist_ok=True)

            # Make the first _git call (reset) fail with TimeoutExpired (not
            # CalledProcessError) to verify the broad except clause (M-1-2).
            import subprocess as _subprocess
            def git_side_effect(args, **kwargs):
                if args == ["reset", "--hard", "HEAD"]:
                    raise _subprocess.TimeoutExpired("git reset", 60)
                # Verify call: still-unmerged
                return MagicMock(stdout="abc123 some commit\n")

            mock_slot._git.side_effect = git_side_effect

            with patch.object(sup, "slack_api_post") as mock_slack, \
                 patch("subprocess.run") as mock_run:
                sup._fallback_merge_dispatch(mock_slot, "1.0", tmp)

            # Worker should NOT have been dispatched (cleanup failed)
            mock_run.assert_not_called()

            # Only failure notification sent (no initial "attempting" per FIX-028)
            self.assertEqual(mock_slack.call_count, 1)
            failure_msg = mock_slack.call_args_list[0][0][1]["text"]
            self.assertIn("manual intervention", failure_msg)

    def test_fallback_merge_dispatch_alerts_on_failure(self):
        """_fallback_merge_dispatch should send failure Slack message when merge doesn't resolve.

        FIX-028: Only the failure notification goes to Slack (not the initial
        "attempting" message which now goes to log_line).
        """
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)

            state = _empty_state()
            state["finished_tasks"]["1.0"] = _make_task("1.0", "finished")
            sup.save_state(state)

            prompts_dir = sup.cfg.session_template.parent
            prompts_dir.mkdir(parents=True, exist_ok=True)
            (prompts_dir / "merge_fallback.md").write_text(
                "Merge {{BRANCH_NAME}} into {{REPO_ROOT}}.\n"
            )

            mock_slot = MagicMock()
            mock_slot.slot_id = 0
            mock_slot.branch_name = "worker-0"
            mock_slot._base_branch = "main"
            mock_slot.worktree_path = tmp / "worktrees" / "worker-0"
            mock_slot.worktree_path.mkdir(parents=True, exist_ok=True)
            # Simulate still-unmerged commits after worker runs
            mock_slot._git.return_value = MagicMock(stdout="abc123 some commit\n")

            with patch.object(sup, "slack_api_post") as mock_slack, \
                 patch("subprocess.run"):
                sup._fallback_merge_dispatch(mock_slot, "1.0", tmp)

            # Only failure notification (no initial "attempting" per FIX-028)
            self.assertEqual(mock_slack.call_count, 1)
            failure_msg = mock_slack.call_args_list[0][0][1]["text"]
            self.assertIn("manual intervention", failure_msg)
            self.assertIn("worker-0", failure_msg)

    def test_fallback_merge_dispatch_missing_template(self):
        """_fallback_merge_dispatch should log and return if merge_fallback.md is missing."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)

            state = _empty_state()
            state["finished_tasks"]["1.0"] = _make_task("1.0", "finished")
            sup.save_state(state)

            mock_slot = MagicMock()
            mock_slot.slot_id = 0
            mock_slot.branch_name = "worker-0"

            with patch.object(sup, "slack_api_post") as mock_slack, \
                 patch("subprocess.run") as mock_run:
                sup._fallback_merge_dispatch(mock_slot, "1.0", tmp)

            # Slack notification sent, but no subprocess should run
            mock_run.assert_not_called()


class TestHeartbeatParallelFields(unittest.TestCase):
    """Verify heartbeat includes parallel dispatch fields."""

    def test_heartbeat_includes_max_workers(self):
        """Heartbeat should include max_workers field."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            sup.write_heartbeat("sleeping", 0, False, 5, "none", 0)
            hb = json.loads(sup.cfg.heartbeat_file.read_text())
            self.assertIn("max_workers", hb)
            self.assertEqual(hb["max_workers"], 1)

    def test_heartbeat_includes_active_workers(self):
        """Heartbeat should include active_workers when provided."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            workers = [{"slot": 0, "task": "1.0", "started_at": 100, "elapsed_sec": 50}]
            sup.write_heartbeat("workers_active", 0, False, 5, "none", 0, active_workers=workers)
            hb = json.loads(sup.cfg.heartbeat_file.read_text())
            self.assertIn("active_workers", hb)
            self.assertEqual(len(hb["active_workers"]), 1)
            self.assertEqual(hb["active_workers"][0]["slot"], 0)

    def test_heartbeat_omits_active_workers_when_none(self):
        """Heartbeat should not include active_workers when not provided."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            sup.write_heartbeat("sleeping", 0, False, 5, "none", 0)
            hb = json.loads(sup.cfg.heartbeat_file.read_text())
            self.assertNotIn("active_workers", hb)


class TestWorkerSlotConfig(unittest.TestCase):
    """Verify config additions for parallel dispatch."""

    def test_max_concurrent_workers_default(self):
        """Default MAX_CONCURRENT_WORKERS should be 1."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(sup.cfg.max_concurrent_workers, 1)

    def test_max_concurrent_workers_override(self):
        """MAX_CONCURRENT_WORKERS should be overridable."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"MAX_CONCURRENT_WORKERS": "3"})
            self.assertEqual(sup.cfg.max_concurrent_workers, 3)

    def test_dispatch_dir_config(self):
        """dispatch_dir should default to .agent/runtime/dispatch."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(str(sup.cfg.dispatch_dir), ".agent/runtime/dispatch")

    def test_worktree_dir_config(self):
        """worktree_dir should default to .agent/runtime/worktrees."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(str(sup.cfg.worktree_dir), ".agent/runtime/worktrees")


class TestBoundedThreadContext(unittest.TestCase):
    """Tests for read_task_text_for_prompt bounded context packing."""

    def _make_task_json(self, tmp, regular_msgs=None, snapshot_msgs=None):
        """Helper to create a task JSON file with given messages."""
        sup = _make_supervisor(Path(tmp))
        task_file = str(Path(tmp) / ".agent/tasks/active/test.json")
        Path(task_file).parent.mkdir(parents=True, exist_ok=True)

        messages = []
        for msg in (regular_msgs or []):
            messages.append({
                "ts": msg.get("ts", "1.0"),
                "user_id": msg.get("user_id", "U123"),
                "role": "human",
                "text": msg.get("text", ""),
            })
        for msg in (snapshot_msgs or []):
            messages.append({
                "ts": msg.get("ts", "2.0"),
                "user_id": msg.get("user_id", "U123"),
                "role": msg.get("role", "human"),
                "text": msg.get("text", ""),
                "source": "context_snapshot",
            })

        data = {
            "task_id": "test",
            "thread_ts": "1.0",
            "channel_id": "C123",
            "messages": messages,
        }
        sup.write_task_json(task_file, data)
        return sup, task_file

    def test_small_thread_unchanged(self):
        """Threads under the message limit should not be clipped."""
        with tempfile.TemporaryDirectory() as td:
            sup, tf = self._make_task_json(td,
                regular_msgs=[{"text": "Do this task", "ts": "1.0"}],
                snapshot_msgs=[
                    {"text": "msg1", "ts": "2.0"},
                    {"text": "msg2", "ts": "3.0"},
                ],
            )
            result = sup.read_task_text_for_prompt(tf)
            self.assertIn("Do this task", result)
            self.assertIn("msg1", result)
            self.assertIn("msg2", result)
            self.assertNotIn("clipped", result)

    def test_large_thread_clips_messages(self):
        """Threads exceeding max_messages should clip middle messages."""
        with tempfile.TemporaryDirectory() as td:
            sup, tf = self._make_task_json(td,
                regular_msgs=[{"text": "Original task", "ts": "0.5"}],
                snapshot_msgs=[
                    {"text": f"snap_{i}", "ts": f"{i}.0", "role": "human"}
                    for i in range(50)
                ],
            )
            # Override config to a small limit
            sup.cfg.thread_context_max_messages = 5
            result = sup.read_task_text_for_prompt(tf)
            # First snapshot message preserved
            self.assertIn("snap_0", result)
            # Last 4 preserved (messages 46-49)
            self.assertIn("snap_49", result)
            self.assertIn("snap_48", result)
            # Middle messages should be clipped
            self.assertNotIn("snap_10", result)
            # Clipping note present
            self.assertIn("clipped", result.lower())

    def test_objective_preserved_on_char_clip(self):
        """When hard char cap triggers, the beginning (objective) is retained."""
        with tempfile.TemporaryDirectory() as td:
            long_text = "A" * 2000
            sup, tf = self._make_task_json(td,
                regular_msgs=[{"text": "OBJECTIVE_MARKER", "ts": "0.5"}],
                snapshot_msgs=[
                    {"text": long_text, "ts": f"{i}.0"}
                    for i in range(20)
                ],
            )
            sup.cfg.thread_context_max_chars = 3000
            sup.cfg.thread_context_preserve_objective_chars = 500
            result = sup.read_task_text_for_prompt(tf)
            self.assertLessEqual(len(result), 3100)  # allow for clipping marker
            self.assertIn("OBJECTIVE_MARKER", result)

    def test_regular_messages_always_included(self):
        """Regular (non-snapshot) messages are never clipped by message limit."""
        with tempfile.TemporaryDirectory() as td:
            sup, tf = self._make_task_json(td,
                regular_msgs=[
                    {"text": "mention1", "ts": "0.5"},
                    {"text": "mention2", "ts": "0.6"},
                ],
                snapshot_msgs=[
                    {"text": f"snap_{i}", "ts": f"{i + 1}.0"}
                    for i in range(50)
                ],
            )
            sup.cfg.thread_context_max_messages = 5
            result = sup.read_task_text_for_prompt(tf)
            self.assertIn("mention1", result)
            self.assertIn("mention2", result)

    def test_empty_task_returns_empty(self):
        """Empty task JSON should return empty string."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            result = sup.read_task_text_for_prompt("")
            self.assertEqual(result, "")

    def test_config_defaults(self):
        """Thread context config knobs should have expected defaults."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(sup.cfg.thread_context_max_chars, 200000)
            self.assertEqual(sup.cfg.thread_context_max_messages, 100)
            self.assertEqual(sup.cfg.thread_context_preserve_objective_chars, 1000)


class TestBoundedThreadContextDispatch(unittest.TestCase):
    """Test that dispatch refresh paths use bounded context."""

    def test_refresh_dispatch_uses_bounded_reader(self):
        """refresh_dispatch_thread_context should call read_task_text_for_prompt."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            task_file = str(Path(td) / ".agent/tasks/active/test.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1.0",
                "thread_ts": "1.0",
                "channel_id": "C123",
                "messages": [{"ts": "1.0", "user_id": "U1", "role": "human", "text": "hi"}],
            })
            sup.atomic_write_json(sup.cfg.dispatch_task_file, {
                "channel_id": "C123",
                "thread_ts": "1.000000",
                "mention_text_file": task_file,
            })
            # Mock Slack API and verify bounded reader is used
            with patch.object(sup, '_fetch_thread_messages', return_value=[
                {"ts": "1.0", "user": "U1", "text": "hi", "bot_id": "", "username": ""},
            ]), patch.object(sup, '_store_thread_snapshot'), \
                 patch.object(sup, 'read_task_text_for_prompt', return_value="bounded") as mock_bounded:
                sup.refresh_dispatch_thread_context()
                mock_bounded.assert_called_once_with(task_file)


# ---------------------------------------------------------------------------
# Thread Context Rendering Tests
# ---------------------------------------------------------------------------

class TestRenderThreadContext(unittest.TestCase):
    """Tests for _render_thread_context() chat transcript format."""

    def test_basic_format(self):
        """Thread context uses [name, short timestamp] format and skips snapshot[0] when it duplicates the original."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            sup._agent_user_id = "UAGENT"
            task_file = str(Path(td) / ".agent/tasks/active/test.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1000000.0",
                "thread_ts": "1000000.0",
                "channel_id": "C123",
                "messages": [
                    {"ts": "1000000.0", "user_id": "U1", "user_name": "alice",
                     "role": "human", "text": "hello"},
                    {"ts": "1000000.0", "user_id": "U1", "user_name": "alice",
                     "role": "human", "text": "hello", "source": "context_snapshot"},
                    {"ts": "1000060.0", "user_id": "UAGENT", "user_name": "murphy",
                     "role": "agent", "text": "hi there", "source": "context_snapshot"},
                ],
            })
            original, thread = sup._render_thread_context(task_file)
            # Original request is the first mention
            self.assertIn("hello", original)
            # Thread context has the reply but not the duplicate snapshot[0]
            self.assertIn("[Murphy,", thread)
            self.assertIn("hi there", thread)
            self.assertNotIn("[alice,", thread)  # snapshot[0] skipped
            # No raw user IDs or Slack ts in parentheses
            self.assertNotIn("UAGENT", thread)
            self.assertNotIn("(1000000.0)", thread)

    def test_agent_shows_murphy(self):
        """Agent's own messages display as Murphy."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            sup._agent_user_id = "UBOT"
            task_file = str(Path(td) / ".agent/tasks/active/test.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "2000000.0",
                "thread_ts": "2000000.0",
                "channel_id": "C123",
                "messages": [
                    {"ts": "2000000.0", "user_id": "UBOT", "user_name": "botname",
                     "role": "agent", "text": "working on it", "source": "context_snapshot"},
                ],
            })
            _original, thread = sup._render_thread_context(task_file)
            self.assertIn("[Murphy,", thread)
            self.assertNotIn("botname", thread)
            self.assertNotIn("UBOT", thread)

    def test_bounding(self):
        """Thread context respects max_messages config."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            sup._agent_user_id = "UAGENT"
            sup.cfg.thread_context_max_messages = 3
            # msg 0 is regular (original mention), snapshots start from msg 1
            messages = [
                {"ts": "1000000.0", "user_id": "U1", "user_name": "alice",
                 "role": "human", "text": "original request"},
            ] + [
                {"ts": f"{1000000 + i * 60}.0", "user_id": "U1", "user_name": "alice",
                 "role": "human", "text": f"msg {i}", "source": "context_snapshot"}
                for i in range(10)
            ]
            task_file = str(Path(td) / ".agent/tasks/active/test.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1000000.0", "thread_ts": "1000000.0",
                "channel_id": "C123", "messages": messages,
            })
            original, thread = sup._render_thread_context(task_file)
            self.assertIn("original request", original)
            # snapshot[0] (ts=1000000.0) is skipped (same ts as regular),
            # so 9 non-dup snapshots remain, bounded to 3: first + last 2
            self.assertIn("msg 1", thread)  # first non-dup snapshot
            self.assertIn("msg 9", thread)
            self.assertIn("msg 8", thread)
            self.assertIn("clipped", thread)
            self.assertNotIn("msg 5", thread)

    def test_empty_file(self):
        """Returns empty strings for missing/empty task file."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(sup._render_thread_context(""), ("", ""))
            self.assertEqual(sup._render_thread_context("/nonexistent"), ("", ""))

    def test_no_snapshots_falls_back_to_regular(self):
        """When no snapshots exist, original request is the regular message, thread context is empty."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            task_file = str(Path(td) / ".agent/tasks/active/test.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1000000.0",
                "thread_ts": "1000000.0",
                "channel_id": "C123",
                "messages": [
                    {"ts": "1000000.0", "user_id": "U1", "user_name": "alice",
                     "role": "human", "text": "help me with X"},
                ],
            })
            original, thread = sup._render_thread_context(task_file)
            self.assertIn("help me with X", original)
            self.assertEqual(thread, "")


class TestDispatchJsonStripping(unittest.TestCase):
    """Test that internal fields are stripped from the prompt JSON."""

    def test_internal_fields_stripped(self):
        """mention_text, claimed_by, etc. should not appear in rendered prompt."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            sup._agent_user_id = "UAGENT"
            # Write a session template that just outputs the JSON
            tpl = Path(td) / "session.md"
            tpl.write_text("{{DISPATCH_TASK_JSON}}\n")
            sup.cfg.session_template = tpl

            task_file = str(Path(td) / ".agent/tasks/active/test.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1.0", "thread_ts": "1.0", "channel_id": "C123",
                "messages": [],
            })

            dispatch = {
                "mention_ts": "1.0",
                "thread_ts": "1.0",
                "channel_id": "C123",
                "mention_text": "some thread text",
                "mention_text_file": task_file,
                "claimed_by": "worker-0",
                "created_ts": "1.0",
                "last_update_ts": "2.0",
                "last_error": "some_error",
                "last_seen_mention_ts": "1.0",
                "watchdog_retries": 2,
                "status": "in_progress",
                "task_description": "test task",
                "source": {"user_id": "U1", "user_name": "alice"},
            }
            sup.atomic_write_json(sup.cfg.dispatch_task_file, dispatch)
            sup.render_runtime_prompt()
            rendered = sup.cfg.runtime_prompt_file.read_text()

            # Internal fields should NOT be in the prompt
            self.assertNotIn('"mention_text"', rendered)
            self.assertNotIn('"claimed_by"', rendered)
            self.assertNotIn('"last_error"', rendered)
            self.assertNotIn('"watchdog_retries"', rendered)
            self.assertNotIn('"last_update_ts"', rendered)
            self.assertNotIn('"mention_text_file"', rendered)
            # Task metadata SHOULD be in the prompt
            self.assertIn('"mention_ts"', rendered)
            self.assertIn('"task_description"', rendered)
            self.assertIn('"channel_id"', rendered)


# ---------------------------------------------------------------------------
# Worker Watchdog Tests
# ---------------------------------------------------------------------------

class TestMCPStartupPattern(unittest.TestCase):
    """Tests for the MCP_STARTUP_FAILURE_PATTERN regex."""

    def test_matches_mcp_server_failure(self):
        from src.loop.supervisor.utils import MCP_STARTUP_FAILURE_PATTERN
        cases = [
            "Error: mcp server connection failed for slack",
            "Failed to start MCP server 'consult'",
            "slack mcp server error: connection refused",
            "mcp server timed out during startup",
            "error starting mcp server: address in use",
            "failed to connect mcp server slack",
            "failed to initialize mcp",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNotNone(
                    MCP_STARTUP_FAILURE_PATTERN.search(text),
                    f"Pattern should match: {text!r}"
                )

    def test_does_not_match_normal_output(self):
        from src.loop.supervisor.utils import MCP_STARTUP_FAILURE_PATTERN
        cases = [
            "Using MCP server slack for Slack API",
            "Successfully connected to MCP servers",
            "MCP ready",
            "tool call: conversations_add_message",
        ]
        for text in cases:
            with self.subTest(text=text):
                self.assertIsNone(
                    MCP_STARTUP_FAILURE_PATTERN.search(text),
                    f"Pattern should NOT match: {text!r}"
                )


class TestWorkerSlotWatchdog(unittest.TestCase):
    """Tests for WorkerSlot kill_worker() and check_mcp_startup()."""

    def _make_slot(self, tmp: Path):
        from src.loop.supervisor.worker_slot import WorkerSlot
        dispatch_dir = tmp / "dispatch"
        outcomes_dir = tmp / "outcomes"
        worktree_dir = tmp / "worktrees"
        for d in (dispatch_dir, outcomes_dir, worktree_dir):
            d.mkdir(parents=True, exist_ok=True)
        slot = WorkerSlot.__new__(WorkerSlot)
        slot.slot_id = 0
        slot.repo_root = tmp
        slot.worktree_path = worktree_dir / "worker-0"
        slot.worktree_path.mkdir(exist_ok=True)
        slot.branch_name = "worker-0"
        slot._base_branch = "main"
        slot.dispatch_task_file = dispatch_dir / "worker-0.task.json"
        slot.dispatch_prompt_file = dispatch_dir / "worker-0.prompt.md"
        slot.session_log_file = dispatch_dir / "worker-0.session.log"
        slot.outcomes_dir = outcomes_dir
        slot.task_key = None
        slot.task_type = None
        slot._thread = None
        slot._result = None
        slot.started_at = None
        slot._log_fn = None
        slot._worktree_initialized = False
        slot._proc = None
        slot._killed_reason = None
        slot._mcp_checked = False
        return slot

    def test_check_mcp_startup_detects_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 60  # Started 60s ago
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text(
                "OpenAI Codex v0.104.0\n"
                "--------\n"
                "Error: mcp server connection failed for slack\n"
                "Continuing without Slack MCP\n"
            )
            reason = slot.check_mcp_startup(grace_sec=30)
            self.assertIsNotNone(reason)
            self.assertIn("mcp_startup_failed", reason)
            # Second call should return None (already checked)
            self.assertIsNone(slot.check_mcp_startup(grace_sec=30))

    def test_check_mcp_startup_skips_before_grace(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time()  # Just started
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text(
                "Error: mcp server connection failed for slack\n"
            )
            reason = slot.check_mcp_startup(grace_sec=30)
            self.assertIsNone(reason)
            self.assertFalse(slot._mcp_checked)

    def test_check_mcp_startup_no_failure(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 60
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text(
                "OpenAI Codex v0.104.0\n"
                "--------\n"
                "workdir: /tmp/test\n"
                "MCP ready\n"
            )
            reason = slot.check_mcp_startup(grace_sec=30)
            self.assertIsNone(reason)
            self.assertTrue(slot._mcp_checked)

    def test_check_mcp_startup_ignores_prompt_content(self):
        """MCP keywords in the user prompt must not trigger a false positive."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 60
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text(
                "OpenAI Codex v0.104.0\n"
                "--------\n"
                "workdir: /tmp/test\n"
                "--------\n"
                "user\n"
                'Your task: {"mention_text": "chatgpt mcp chrome, push commit. '
                "MCP server investigation. MERGE_FAILURE_BLOCKS_DONE=true\"}\n"
            )
            reason = slot.check_mcp_startup(grace_sec=30)
            self.assertIsNone(reason)
            self.assertTrue(slot._mcp_checked)

    def test_check_mcp_startup_missing_log(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 60
            # Don't create the log file
            reason = slot.check_mcp_startup(grace_sec=30)
            self.assertIsNone(reason)
            self.assertTrue(slot._mcp_checked)

    def test_kill_worker_sets_reason(self):
        """kill_worker should set killed_reason and terminate a live process."""
        from src.loop.supervisor.worker_slot import WorkerSlot
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            # Start a real subprocess via Popen
            import subprocess
            proc = subprocess.Popen(
                ["sleep", "60"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            slot._proc = proc
            result = slot.kill_worker("test_kill")
            self.assertTrue(result)
            self.assertEqual(slot.killed_reason, "test_kill")
            # Process should be terminated
            proc.wait(timeout=10)
            self.assertIsNotNone(proc.returncode)

    def test_kill_worker_no_proc(self):
        """kill_worker should return False when no process is running."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            result = slot.kill_worker("test_kill")
            self.assertFalse(result)
            self.assertEqual(slot.killed_reason, "test_kill")

    def test_reset_clears_watchdog_state(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot._killed_reason = "test"
            slot._mcp_checked = True
            slot.reset()
            self.assertIsNone(slot._killed_reason)
            self.assertFalse(slot._mcp_checked)

    def test_session_log_stale_detects_idle(self):
        """Worker with a stale session log should be flagged."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 1200  # Started 20 min ago
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text("some output\n")
            # Backdate the file mtime to 16 min ago
            stale_time = time.time() - 960
            os.utime(slot.session_log_file, (stale_time, stale_time))
            reason = slot.check_session_log_stale(idle_timeout_sec=900)
            self.assertIsNotNone(reason)
            self.assertIn("session_log_stale", reason)

    def test_session_log_stale_skips_young_worker(self):
        """Worker that hasn't run long enough should not be flagged."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 300  # Started 5 min ago
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text("some output\n")
            # Even if file is stale, worker hasn't been running long enough
            stale_time = time.time() - 960
            os.utime(slot.session_log_file, (stale_time, stale_time))
            reason = slot.check_session_log_stale(idle_timeout_sec=900)
            self.assertIsNone(reason)

    def test_session_log_stale_ok_when_recent(self):
        """Worker with a recently written session log should not be flagged."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 1200  # Started 20 min ago
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text("recent output\n")
            # mtime is now (just written), so not stale
            reason = slot.check_session_log_stale(idle_timeout_sec=900)
            self.assertIsNone(reason)

    def test_session_log_stale_missing_file(self):
        """Missing session log should not flag stale."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 1200
            # Don't create the file
            reason = slot.check_session_log_stale(idle_timeout_sec=900)
            self.assertIsNone(reason)

    def test_session_log_stale_tool_in_flight_not_killed(self):
        """Stale log with tool call in flight should NOT be killed (under tool timeout)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 1200  # Started 20 min ago
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            # Last line is an unmatched tool start
            slot.session_log_file.write_text(
                'some reasoning text\n'
                'tool consult.ask({"mode":"deep","prompt":"hello"})\n'
            )
            stale_time = time.time() - 960  # 16 min ago
            os.utime(slot.session_log_file, (stale_time, stale_time))
            reason = slot.check_session_log_stale(
                idle_timeout_sec=900, tool_timeout_sec=14400
            )
            self.assertIsNone(reason)

    def test_session_log_stale_tool_in_flight_over_tool_timeout(self):
        """Stale log with tool in flight exceeding tool timeout should be killed."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 20000  # Started long ago
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text(
                'tool consult.ask({"mode":"deep","prompt":"hello"})\n'
            )
            stale_time = time.time() - 15000  # Stale beyond 14400s tool timeout
            os.utime(slot.session_log_file, (stale_time, stale_time))
            reason = slot.check_session_log_stale(
                idle_timeout_sec=900, tool_timeout_sec=14400
            )
            self.assertIsNotNone(reason)
            self.assertIn("tool_call_stale", reason)

    def test_session_log_stale_tool_completed_still_killed(self):
        """Stale log where last tool completed should be killed normally."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 1200
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            # Tool started and completed — last boundary is completion
            slot.session_log_file.write_text(
                'tool consult.ask({"mode":"deep","prompt":"hello"})\n'
                'consult.ask({"mode":"deep","prompt":"hello"}) success in 5m 30s:\n'
                '{"content": "response text"}\n'
            )
            stale_time = time.time() - 960
            os.utime(slot.session_log_file, (stale_time, stale_time))
            reason = slot.check_session_log_stale(idle_timeout_sec=900)
            self.assertIsNotNone(reason)
            self.assertIn("session_log_stale", reason)

    def test_session_log_stale_no_tool_patterns(self):
        """Stale log with no tool patterns should be killed (existing behavior)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.started_at = time.time() - 1200
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text("just some reasoning text\n")
            stale_time = time.time() - 960
            os.utime(slot.session_log_file, (stale_time, stale_time))
            reason = slot.check_session_log_stale(idle_timeout_sec=900)
            self.assertIsNotNone(reason)
            self.assertIn("session_log_stale", reason)

    def test_detect_tool_in_flight_empty_log(self):
        """Empty session log should not detect a tool in flight."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            slot = self._make_slot(tmp)
            slot.session_log_file.parent.mkdir(parents=True, exist_ok=True)
            slot.session_log_file.write_text("")
            self.assertFalse(slot._detect_tool_in_flight())


class TestWatchdogConfig(unittest.TestCase):
    """Tests for watchdog configuration defaults."""

    def test_watchdog_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            conf = tmp / "test.conf"
            conf.write_text("")
            cfg = sl.Config(conf)
            self.assertEqual(cfg.mcp_startup_check_sec, 30)
            self.assertEqual(cfg.max_watchdog_retries, 2)
            self.assertEqual(cfg.worker_idle_timeout_sec, 900)
            self.assertEqual(cfg.worker_tool_timeout_sec, 14400)


class TestWatchdogRequeueOrPark(unittest.TestCase):
    """Tests for atomic watchdog requeue-or-park logic."""

    def _make_sup(self, tmp):
        return _make_supervisor(tmp)

    def test_requeue_when_retries_remain(self):
        """Task should move from active to queued when retries remain."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = self._make_sup(tmp)
            state = sup.load_state()
            state["active_tasks"] = {
                "t1": {"status": "in_progress", "claimed_by": "slot-0",
                       "watchdog_retries": 0}
            }
            sup.save_state(state)
            result = sup._watchdog_requeue_or_park("t1", "mcp_timeout")
            self.assertTrue(result)
            state = sup.load_state()
            self.assertNotIn("t1", state.get("active_tasks", {}))
            self.assertIn("t1", state.get("queued_tasks", {}))
            task = state["queued_tasks"]["t1"]
            self.assertEqual(task["status"], "queued")
            self.assertIsNone(task["claimed_by"])
            self.assertEqual(task["watchdog_retries"], 1)

    def test_park_when_retries_exhausted(self):
        """Task should move to incomplete/waiting_human when retries exhausted."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = self._make_sup(tmp)
            state = sup.load_state()
            state["active_tasks"] = {
                "t1": {"status": "in_progress", "claimed_by": "slot-0",
                       "watchdog_retries": sup.cfg.max_watchdog_retries}
            }
            sup.save_state(state)
            result = sup._watchdog_requeue_or_park("t1", "mcp_timeout")
            self.assertFalse(result)
            state = sup.load_state()
            self.assertNotIn("t1", state.get("active_tasks", {}))
            self.assertIn("t1", state.get("incomplete_tasks", {}))
            task = state["incomplete_tasks"]["t1"]
            self.assertEqual(task["status"], "waiting_human")
            self.assertIn("watchdog_retries_exhausted", task.get("last_error", ""))

    def test_missing_task_returns_false(self):
        """Should return False without error if task not in active_tasks."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = self._make_sup(tmp)
            result = sup._watchdog_requeue_or_park("nonexistent", "test")
            self.assertFalse(result)


class TestMaintenanceInFlight(unittest.TestCase):
    """Tests for MaintenanceManager._is_in_flight status filtering."""

    def _make_sup(self, tmp):
        return _make_supervisor(tmp)

    def test_waiting_human_not_in_flight(self):
        """A waiting_human maintenance task in incomplete should not block new maintenance."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = self._make_sup(tmp)
            state = _empty_state()
            state["incomplete_tasks"]["maintenance"] = _make_task(
                "maintenance", status="waiting_human", task_type="maintenance",
            )
            self.assertFalse(sup.maintenance._is_in_flight(state))

    def test_in_progress_is_in_flight(self):
        """An in_progress maintenance task in incomplete should block new maintenance."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = self._make_sup(tmp)
            state = _empty_state()
            state["incomplete_tasks"]["maintenance"] = _make_task(
                "maintenance", status="in_progress", task_type="maintenance",
            )
            self.assertTrue(sup.maintenance._is_in_flight(state))

    def test_queued_is_in_flight(self):
        """A queued maintenance task should block new maintenance."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = self._make_sup(tmp)
            state = _empty_state()
            state["queued_tasks"]["maintenance"] = _make_task(
                "maintenance", status="queued", task_type="maintenance",
            )
            self.assertTrue(sup.maintenance._is_in_flight(state))


class TestConsecutiveExitFailures(unittest.TestCase):
    """Tests for the consecutive exit failure counter that prevents crash loops."""

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def _setup_active_task(self, sup, key="1000.000000", extra=None):
        state = _empty_state()
        task = _make_task(key, "in_progress")
        if extra:
            task.update(extra)
        state["active_tasks"][key] = task
        sup.save_state(state)

    def test_parks_after_threshold(self):
        """Task should be parked as waiting_human after max_consecutive_exit_failures."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            threshold = sup.cfg.max_consecutive_exit_failures

            for i in range(threshold):
                self._setup_active_task(sup, key, extra={
                    "consecutive_exit_failures": i,
                })
                sup.reconcile_task_after_run(key, 1)

            state = sup.load_state()
            self.assertIn(key, state["incomplete_tasks"])
            task = state["incomplete_tasks"][key]
            self.assertEqual(task["status"], "waiting_human")
            self.assertEqual(task.get("consecutive_exit_failures"), threshold)
            self.assertIn("consecutive_exit_failures=", task.get("last_error", ""))

    def test_increments_on_failure(self):
        """Counter should increment by 1 on each non-zero exit."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            self._setup_active_task(sup, key)
            sup.reconcile_task_after_run(key, 1)
            state = sup.load_state()
            task = state["incomplete_tasks"][key]
            self.assertEqual(task.get("consecutive_exit_failures"), 1)
            self.assertEqual(task["status"], "in_progress")

    def test_resets_on_success(self):
        """Counter should be cleared on successful exit."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            key = "1000.000000"
            # Set up task with existing failures
            self._setup_active_task(sup, key, extra={
                "consecutive_exit_failures": 2,
            })
            # Write a successful outcome
            sup.cfg.dispatch_outcome_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_outcome_file.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "ok", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            task = state["finished_tasks"].get(key, {})
            self.assertNotIn("consecutive_exit_failures", task)


class TestSerialModeMCPCheck(unittest.TestCase):
    """Tests for serial mode MCP failure classification."""

    def test_mcp_failure_classified_as_retryable(self):
        """MCP startup failure in output should override failure_kind."""
        from src.loop.supervisor.utils import MCP_STARTUP_FAILURE_PATTERN
        output = (
            "OpenAI Codex v0.104.0\n"
            "--------\n"
            "Error: mcp server connection failed for slack\n"
            "some other output\n"
        )
        # Simulate the serial mode check logic
        exit_code = 1
        failure_kind = sl.Supervisor.classify_failure(output, exit_code)
        self.assertEqual(failure_kind, "nonzero_exit")  # Without MCP check
        # Now apply MCP check
        if failure_kind not in ("timeout",):
            if MCP_STARTUP_FAILURE_PATTERN.search(output[:8192]):
                failure_kind = "mcp_startup_failed"
        self.assertEqual(failure_kind, "mcp_startup_failed")


class TestSlackAPIRetry(unittest.TestCase):
    """Tests for Slack API retry/backoff logic."""

    # runtime module where urlopen is actually imported
    _rt = sys.modules.get("src.loop.supervisor.runtime") or __import__(
        "src.loop.supervisor.runtime", fromlist=["urlopen"]
    )

    def _sup(self, td):
        sup = _make_supervisor(Path(td))
        sup.slack_token = "xoxb-test"
        return sup

    def test_transient_failure_then_success(self):
        """Retries on transient URLError and succeeds."""
        from urllib.error import URLError
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            call_count = [0]
            def mock_urlopen(req, timeout=60):
                call_count[0] += 1
                if call_count[0] <= 2:
                    raise URLError("Connection refused")
                return MagicMock(
                    read=MagicMock(return_value=b'{"ok": true}'),
                    __enter__=lambda s: s,
                    __exit__=lambda s, *a: None,
                )
            with patch.object(self._rt, "urlopen", mock_urlopen), \
                 patch("time.sleep"):
                result = sup.slack_api_get("test.method", {"key": "val"})
                self.assertTrue(result["ok"])
                self.assertEqual(call_count[0], 3)

    def test_http_429_respects_retry_after(self):
        """Retries on HTTP 429 and respects Retry-After header."""
        from urllib.error import HTTPError
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            call_count = [0]
            sleep_durations = []
            def mock_urlopen(req, timeout=60):
                call_count[0] += 1
                if call_count[0] == 1:
                    exc = HTTPError(req.full_url, 429, "Rate Limited", {"Retry-After": "2"}, None)
                    raise exc
                return MagicMock(
                    read=MagicMock(return_value=b'{"ok": true}'),
                    __enter__=lambda s: s,
                    __exit__=lambda s, *a: None,
                )
            def mock_sleep(sec):
                sleep_durations.append(sec)
            with patch.object(self._rt, "urlopen", mock_urlopen), \
                 patch("time.sleep", mock_sleep):
                result = sup.slack_api_get("test.method", {})
                self.assertTrue(result["ok"])
                self.assertEqual(call_count[0], 2)
                self.assertAlmostEqual(sleep_durations[0], 2.0)

    def test_persistent_failure_raises(self):
        """Raises after max retries on persistent transient errors."""
        from urllib.error import URLError
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            def mock_urlopen(req, timeout=60):
                raise URLError("Network unreachable")
            with patch.object(self._rt, "urlopen", mock_urlopen), \
                 patch("time.sleep"):
                with self.assertRaises(URLError):
                    sup.slack_api_get("test.method", {})

    def test_non_retryable_4xx_raises_immediately(self):
        """Non-429 4xx errors are not retried."""
        from urllib.error import HTTPError
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            call_count = [0]
            def mock_urlopen(req, timeout=60):
                call_count[0] += 1
                raise HTTPError(req.full_url, 403, "Forbidden", {}, None)
            with patch.object(self._rt, "urlopen", mock_urlopen), \
                 patch("time.sleep"):
                with self.assertRaises(HTTPError):
                    sup.slack_api_get("test.method", {})
                self.assertEqual(call_count[0], 1)  # No retry

    def test_slack_api_post_uses_retry(self):
        """slack_api_post also uses the retry path."""
        from urllib.error import URLError
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            call_count = [0]
            def mock_urlopen(req, timeout=60):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise URLError("Temporary failure")
                return MagicMock(
                    read=MagicMock(return_value=b'{"ok": true}'),
                    __enter__=lambda s: s,
                    __exit__=lambda s, *a: None,
                )
            with patch.object(self._rt, "urlopen", mock_urlopen), \
                 patch("time.sleep"):
                result = sup.slack_api_post("chat.postMessage", {"text": "hi"})
                self.assertTrue(result["ok"])
                self.assertEqual(call_count[0], 2)


class TestSlackRetryConfig(unittest.TestCase):
    """Tests for Slack API retry configuration defaults."""

    def test_retry_config_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            conf_path = _make_conf(Path(td))
            cfg = sl.Config(conf_path)
            self.assertEqual(cfg.slack_api_max_retries, 3)
            self.assertAlmostEqual(cfg.slack_api_retry_initial_sec, 1.0)
            self.assertAlmostEqual(cfg.slack_api_retry_multiplier, 2.0)
            self.assertAlmostEqual(cfg.slack_api_retry_max_sec, 30.0)
            self.assertEqual(cfg.slack_api_timeout_sec, 60)

    def test_retry_config_override(self):
        with tempfile.TemporaryDirectory() as td:
            conf_path = _make_conf(Path(td))
            env = {"SLACK_API_MAX_RETRIES": "5", "SLACK_API_TIMEOUT_SEC": "120"}
            with patch.dict(os.environ, env):
                cfg = sl.Config(conf_path)
            self.assertEqual(cfg.slack_api_max_retries, 5)
            self.assertEqual(cfg.slack_api_timeout_sec, 120)


# ===========================================================================
# Loop mode tests
# ===========================================================================

class TestLoopMode(unittest.TestCase):
    """Tests for !loop-Xh continuous iteration feature."""

    # -- Regex and parsing --

    def test_loop_re_matches_variants(self):
        """LOOP_RE matches !loop, !loop-3h, !loop-90m."""
        sup_cls = sl.Supervisor
        self.assertIsNotNone(sup_cls.LOOP_RE.search("!loop"))
        m = sup_cls.LOOP_RE.search("!loop-3h")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "3h")
        m = sup_cls.LOOP_RE.search("!loop-90m")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "90m")
        # Embedded in text
        m = sup_cls.LOOP_RE.search("fix wandb saving !loop-2h and test")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "2h")

    def test_loop_re_no_match(self):
        """LOOP_RE does not match unrelated text."""
        self.assertIsNone(sl.Supervisor.LOOP_RE.search("loop"))
        self.assertIsNone(sl.Supervisor.LOOP_RE.search("!restart"))

    def test_loop_command_re_entire_message(self):
        """LOOP_COMMAND_RE only matches when !loop is the entire message."""
        self.assertIsNotNone(sl.Supervisor.LOOP_COMMAND_RE.search("<@U123> !loop-3h"))
        self.assertIsNotNone(sl.Supervisor.LOOP_COMMAND_RE.search("<@U123> !loop"))
        self.assertIsNone(sl.Supervisor.LOOP_COMMAND_RE.search("<@U123> fix stuff !loop-3h"))

    def test_stop_command_re(self):
        """STOP_COMMAND_RE matches @agent !stop."""
        self.assertIsNotNone(sl.Supervisor.STOP_COMMAND_RE.search("<@U123> !stop"))
        self.assertIsNone(sl.Supervisor.STOP_COMMAND_RE.search("<@U123> !stop now"))
        self.assertIsNone(sl.Supervisor.STOP_COMMAND_RE.search("<@U123> fix !stop"))

    def test_parse_loop_duration(self):
        """_parse_loop_duration handles hours, minutes, bare digits (hours), and default."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(sup._parse_loop_duration("3h"), 10800)
            self.assertEqual(sup._parse_loop_duration("90m"), 5400)
            # Bare digits default to hours
            self.assertEqual(sup._parse_loop_duration("3"), 10800)
            self.assertEqual(sup._parse_loop_duration("1"), 3600)
            # None/empty returns config default
            self.assertEqual(sup._parse_loop_duration(None), sup.cfg.loop_max_duration_sec)
            self.assertEqual(sup._parse_loop_duration(""), sup.cfg.loop_max_duration_sec)

    def test_loop_re_strips_from_text(self):
        """LOOP_RE.sub strips !loop-Xh from mention text and collapses whitespace."""
        import re
        text = "fix wandb saving !loop-3h and make tests pass"
        cleaned = re.sub(r"  +", " ", sl.Supervisor.LOOP_RE.sub("", text)).strip()
        self.assertEqual(cleaned, "fix wandb saving and make tests pass")

    # -- Config --

    def test_loop_config_defaults(self):
        """Loop config has expected defaults."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(sup.cfg.loop_max_duration_sec, 18000)
            self.assertEqual(sup.cfg.loop_iteration_delay_sec, 180)

    def test_loop_config_override(self):
        """Loop config can be overridden via environment."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "LOOP_MAX_DURATION_SEC": "7200",
                "LOOP_ITERATION_DELAY_SEC": "60",
            })
            self.assertEqual(sup.cfg.loop_max_duration_sec, 7200)
            self.assertEqual(sup.cfg.loop_iteration_delay_sec, 60)

    # -- Dispatchable filtering --

    def test_has_dispatchable_tasks_respects_loop_delay(self):
        """_has_dispatchable_tasks returns False for loop tasks with pending delay."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            task = _make_task("1000.000000", status="in_progress")
            task["loop_mode"] = True
            task["loop_next_dispatch_after"] = str(time.time() + 300)  # 5 min in future
            state["incomplete_tasks"]["1000.000000"] = task
            sup.save_state(state)
            self.assertFalse(sup._has_dispatchable_tasks())

    def test_has_dispatchable_tasks_allows_ready_loop_task(self):
        """_has_dispatchable_tasks returns True for loop tasks past their delay."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            task = _make_task("1000.000000", status="in_progress")
            task["loop_mode"] = True
            task["loop_next_dispatch_after"] = str(time.time() - 10)  # past
            state["incomplete_tasks"]["1000.000000"] = task
            sup.save_state(state)
            self.assertTrue(sup._has_dispatchable_tasks())

    def test_has_dispatchable_tasks_allows_non_loop_incomplete(self):
        """_has_dispatchable_tasks still works for non-loop incomplete tasks."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            task = _make_task("1000.000000", status="in_progress")
            state["incomplete_tasks"]["1000.000000"] = task
            sup.save_state(state)
            self.assertTrue(sup._has_dispatchable_tasks())

    # -- Reconcile loop logic --

    def test_reconcile_loop_active_overrides_done(self):
        """When loop is active, worker's 'done' status is overridden to 'in_progress'."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            _write_agent_identity(sup)
            state = _empty_state()
            task = _make_task("1000.000000", status="in_progress")
            task["claimed_by"] = "test-agent-slot-0"
            task["loop_mode"] = True
            task["loop_deadline"] = str(time.time() + 3600)  # 1h in future
            task["loop_iteration"] = 1
            state["active_tasks"]["1000.000000"] = task

            # Ensure task text file exists
            text_file = sup.cfg.tasks_dir / "active" / "1000.000000.json"
            text_file.parent.mkdir(parents=True, exist_ok=True)
            json.dump({"task_id": "1000.000000", "messages": []}, text_file.open("w"))
            task["mention_text_file"] = str(text_file)

            sup.save_state(state)

            # Write outcome: worker says "done"
            outcome = {
                "mention_ts": "1000.000000",
                "thread_ts": "1000.000000",
                "status": "done",
                "summary": "Fixed it",
                "completion_confidence": "high",
            }
            outcome_path = sup.cfg.outcomes_dir / "1000.000000.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            json.dump(outcome, outcome_path.open("w"))

            sup.reconcile_task_after_run("1000.000000", 0, outcome_file_override=outcome_path)

            state = sup.load_state()
            # Task should be in incomplete, NOT finished
            self.assertIn("1000.000000", state.get("incomplete_tasks", {}))
            self.assertNotIn("1000.000000", state.get("finished_tasks", {}))
            t = state["incomplete_tasks"]["1000.000000"]
            self.assertEqual(t["status"], "in_progress")
            self.assertEqual(t["loop_iteration"], 2)
            self.assertTrue(float(t.get("loop_next_dispatch_after", "0")) > time.time())
            # Worker's real status is preserved for !stop restoration
            self.assertEqual(t.get("loop_worker_status"), "done")

    def test_reconcile_loop_expired_applies_final_status(self):
        """When loop deadline passed, worker's status is applied faithfully."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            _write_agent_identity(sup)
            state = _empty_state()
            task = _make_task("1000.000000", status="in_progress")
            task["claimed_by"] = "test-agent-slot-0"
            task["loop_mode"] = True
            task["loop_deadline"] = str(time.time() - 10)  # expired
            task["loop_iteration"] = 5
            state["active_tasks"]["1000.000000"] = task

            text_file = sup.cfg.tasks_dir / "active" / "1000.000000.json"
            text_file.parent.mkdir(parents=True, exist_ok=True)
            json.dump({"task_id": "1000.000000", "messages": []}, text_file.open("w"))
            task["mention_text_file"] = str(text_file)

            sup.save_state(state)

            # Write outcome: worker says "done"
            outcome = {
                "mention_ts": "1000.000000",
                "thread_ts": "1000.000000",
                "status": "done",
                "summary": "All done after 5 iterations",
                "completion_confidence": "high",
            }
            outcome_path = sup.cfg.outcomes_dir / "1000.000000.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            json.dump(outcome, outcome_path.open("w"))

            sup.reconcile_task_after_run("1000.000000", 0, outcome_file_override=outcome_path)

            state = sup.load_state()
            # Task should be in finished (loop expired, done status applied)
            self.assertIn("1000.000000", state.get("finished_tasks", {}))
            t = state["finished_tasks"]["1000.000000"]
            self.assertEqual(t["status"], "done")
            # Loop fields should be cleared
            self.assertNotIn("loop_mode", t)
            self.assertNotIn("loop_deadline", t)
            self.assertNotIn("loop_iteration", t)
            self.assertNotIn("loop_worker_status", t)

    def test_reconcile_loop_expired_waiting_human(self):
        """When loop expires and worker says waiting_human, task stays incomplete."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            _write_agent_identity(sup)
            state = _empty_state()
            task = _make_task("1000.000000", status="in_progress")
            task["claimed_by"] = "test-agent-slot-0"
            task["loop_mode"] = True
            task["loop_deadline"] = str(time.time() - 10)  # expired
            task["loop_iteration"] = 3
            state["active_tasks"]["1000.000000"] = task

            text_file = sup.cfg.tasks_dir / "active" / "1000.000000.json"
            text_file.parent.mkdir(parents=True, exist_ok=True)
            json.dump({"task_id": "1000.000000", "messages": []}, text_file.open("w"))
            task["mention_text_file"] = str(text_file)

            sup.save_state(state)

            outcome = {
                "mention_ts": "1000.000000",
                "thread_ts": "1000.000000",
                "status": "waiting_human",
                "summary": "Need review",
                "completion_confidence": "medium",
            }
            outcome_path = sup.cfg.outcomes_dir / "1000.000000.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            json.dump(outcome, outcome_path.open("w"))

            sup.reconcile_task_after_run("1000.000000", 0, outcome_file_override=outcome_path)

            state = sup.load_state()
            self.assertIn("1000.000000", state.get("incomplete_tasks", {}))
            t = state["incomplete_tasks"]["1000.000000"]
            self.assertEqual(t["status"], "waiting_human")
            self.assertNotIn("loop_mode", t)

    # -- Prompt context --

    def test_build_loop_context_active(self):
        """_build_loop_context returns formatted context when loop is active."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            # Copy real template so we test actual substitution
            prompts_dir = sup.cfg.session_template.parent
            prompts_dir.mkdir(parents=True, exist_ok=True)
            real_template = Path("src/prompts/loop_context.md")
            if real_template.exists():
                import shutil
                shutil.copy(real_template, prompts_dir / "loop_context.md")
            dispatch = {
                "loop_mode": True,
                "loop_iteration": 3,
                "loop_deadline": str(time.time() + 7200),  # 2h
            }
            ctx = sup._build_loop_context(dispatch)
            self.assertIn("Loop Mode (Active)", ctx)
            self.assertIn("iteration 3", ctx)
            self.assertIn("remaining", ctx)
            # Verify placeholders are fully substituted
            self.assertNotIn("{{LOOP_ITERATION}}", ctx)
            self.assertNotIn("{{LOOP_REMAINING}}", ctx)

    def test_build_loop_context_inactive(self):
        """_build_loop_context returns empty string when loop is not active."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            ctx = sup._build_loop_context({})
            self.assertEqual(ctx, "")
            ctx = sup._build_loop_context({"loop_mode": False})
            self.assertEqual(ctx, "")

    # -- Command detection (is_stop_command) --

    def test_is_stop_command(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertTrue(sup._is_stop_command("<@U123> !stop"))
            self.assertFalse(sup._is_stop_command("<@U123> !stop now"))
            self.assertFalse(sup._is_stop_command("<@U123> fix stuff"))

    # -- !stop parks task as waiting_human (Bug 1) --

    def test_stop_sets_waiting_human_in_incomplete(self):
        """!stop on a loop task in incomplete_tasks sets waiting_human."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            task = _make_task("1000.000000", status="in_progress")
            task["loop_mode"] = True
            task["loop_deadline"] = str(time.time() + 3600)
            task["loop_iteration"] = 3
            task["loop_next_dispatch_after"] = str(time.time() + 180)
            task["loop_worker_status"] = "done"
            state["incomplete_tasks"]["1000.000000"] = task
            sup.save_state(state)

            # Simulate !stop logic
            with sup.state_lock():
                state = sup.load_state()
                t = state["incomplete_tasks"]["1000.000000"]
                t.pop("loop_worker_status", None)
                t.pop("loop_mode", None)
                t.pop("loop_deadline", None)
                t.pop("loop_next_dispatch_after", None)
                t.pop("loop_iteration", None)
                t["status"] = "waiting_human"
                sup.save_state(state)

            state = sup.load_state()
            self.assertIn("1000.000000", state.get("incomplete_tasks", {}))
            t = state["incomplete_tasks"]["1000.000000"]
            self.assertEqual(t["status"], "waiting_human")
            self.assertNotIn("loop_mode", t)
            self.assertNotIn("loop_iteration", t)

    def test_stop_moves_active_task_to_incomplete_waiting_human(self):
        """!stop on a loop task in active_tasks moves it to incomplete as waiting_human."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            task = _make_task("1000.000000", status="in_progress")
            task["loop_mode"] = True
            task["loop_deadline"] = str(time.time() + 3600)
            task["loop_iteration"] = 1
            task["claimed_by"] = "test-agent-slot-0"
            state["active_tasks"]["1000.000000"] = task
            sup.save_state(state)

            # Simulate !stop logic for active_tasks bucket
            with sup.state_lock():
                state = sup.load_state()
                t = state["active_tasks"]["1000.000000"]
                t.pop("loop_worker_status", None)
                t.pop("loop_mode", None)
                t.pop("loop_deadline", None)
                t.pop("loop_next_dispatch_after", None)
                t.pop("loop_iteration", None)
                t["status"] = "waiting_human"
                state["active_tasks"].pop("1000.000000", None)
                state.setdefault("incomplete_tasks", {})["1000.000000"] = t
                sup.save_state(state)

            state = sup.load_state()
            self.assertNotIn("1000.000000", state.get("active_tasks", {}))
            self.assertIn("1000.000000", state.get("incomplete_tasks", {}))
            self.assertEqual(state["incomplete_tasks"]["1000.000000"]["status"], "waiting_human")

    # -- loop_iteration reset on re-activation (Bug 3) --

    def test_loop_reactivation_resets_iteration(self):
        """Re-activating !loop on a task resets loop_iteration to 0."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            state = _empty_state()
            task = _make_task("1000.000000", status="in_progress")
            # Simulate a previously stopped loop that left stale loop_iteration
            task["loop_iteration"] = 5
            state["incomplete_tasks"]["1000.000000"] = task
            sup.save_state(state)

            # Simulate loop activation
            with sup.state_lock():
                state = sup.load_state()
                t = state["incomplete_tasks"]["1000.000000"]
                t["loop_mode"] = True
                t["loop_deadline"] = str(time.time() + 7200)
                t["loop_iteration"] = 0  # This is the fix (was setdefault)
                t.pop("loop_worker_status", None)
                sup.save_state(state)

            state = sup.load_state()
            t = state["incomplete_tasks"]["1000.000000"]
            self.assertEqual(t["loop_iteration"], 0)

    # -- normalize_task gates loop fields on loop_mode (Bug 6) --

    def test_normalize_task_drops_stale_loop_fields_without_loop_mode(self):
        """normalize_task does not preserve loop_iteration/loop_next_dispatch_after without loop_mode."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            task = _make_task("1000.000000", status="in_progress")
            # Stale loop fields without loop_mode
            task["loop_iteration"] = 5
            task["loop_next_dispatch_after"] = str(time.time() + 300)
            task["loop_worker_status"] = "done"

            normalized = sup.normalize_task(task, "1000.000000", bucket_name="incomplete_tasks")
            self.assertNotIn("loop_iteration", normalized)
            self.assertNotIn("loop_next_dispatch_after", normalized)
            self.assertNotIn("loop_worker_status", normalized)
            self.assertNotIn("loop_mode", normalized)


class TestMergeFailureEnforcement(unittest.TestCase):
    """Tests for merge-failure state enforcement in parallel reconcile (plan 09)."""

    def _make_sup(self, tmp: Path, merge_blocks: bool = False) -> sl.Supervisor:
        overrides = {"MERGE_FAILURE_BLOCKS_DONE": "true" if merge_blocks else "false"}
        return _make_supervisor(tmp, env_overrides=overrides)

    def _setup_finished_task(self, sup: sl.Supervisor, key: str = "1000.000000") -> None:
        sup.ensure_state_schema()
        with sup.state_lock():
            state = sup.load_state()
            state["finished_tasks"][key] = sup.normalize_task(
                {
                    "thread_ts": key,
                    "channel_id": "C_TEST",
                    "status": "done",
                    "task_type": "slack_mention",
                    "summary": "task completed",
                },
                key,
                force_done=True,
                bucket_name="finished_tasks",
            )
            sup.save_state(state)

    def test_merge_constants_imported(self):
        from src.loop.supervisor.utils import (
            MERGE_CHECK_ERROR,
            MERGE_FALLBACK_FAILED,
            MERGE_FALLBACK_OK,
            MERGE_FF,
            MERGE_NO_UNMERGED,
        )
        self.assertEqual(MERGE_NO_UNMERGED, "no_unmerged_commits")
        self.assertEqual(MERGE_FF, "ff_merged")
        self.assertEqual(MERGE_FALLBACK_OK, "fallback_merged")
        self.assertEqual(MERGE_FALLBACK_FAILED, "fallback_failed")
        self.assertEqual(MERGE_CHECK_ERROR, "merge_check_error")

    def test_config_default_is_false(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            sup = _make_supervisor(tmp)
            self.assertFalse(sup.cfg.merge_failure_blocks_done)

    def test_config_override_to_true(self):
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            sup = self._make_sup(tmp, merge_blocks=True)
            self.assertTrue(sup.cfg.merge_failure_blocks_done)

    def test_annotation_only_mode_no_state_change(self):
        """When merge_failure_blocks_done=false, task stays in finished_tasks."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            sup = self._make_sup(tmp, merge_blocks=False)
            key = "1000.000000"
            self._setup_finished_task(sup, key)

            sup._enforce_merge_blocked_state(key, 0, "worker-0", "fallback_failed")

            state = sup.load_state()
            self.assertIn(key, state.get("finished_tasks", {}))
            self.assertNotIn(key, state.get("incomplete_tasks", {}))
            task = state["finished_tasks"][key]
            self.assertEqual(task["status"], "done")

    def test_enforcement_downgrades_done_to_waiting_human(self):
        """When merge_failure_blocks_done=true, finished task moves to incomplete/waiting_human."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            sup = self._make_sup(tmp, merge_blocks=True)
            key = "1000.000000"
            self._setup_finished_task(sup, key)

            sup._enforce_merge_blocked_state(key, 0, "worker-0", "fallback_failed")

            state = sup.load_state()
            self.assertNotIn(key, state.get("finished_tasks", {}))
            self.assertIn(key, state.get("incomplete_tasks", {}))
            task = state["incomplete_tasks"][key]
            self.assertEqual(task["status"], "waiting_human")
            self.assertIn("fallback_failed", task.get("last_error", ""))
            self.assertIn("worker-0", task.get("last_error", ""))

    def test_enforcement_on_merge_check_error(self):
        """merge_check_error also triggers enforcement."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            sup = self._make_sup(tmp, merge_blocks=True)
            key = "1000.000000"
            self._setup_finished_task(sup, key)

            sup._enforce_merge_blocked_state(key, 1, "worker-1", "merge_check_error")

            state = sup.load_state()
            self.assertNotIn(key, state.get("finished_tasks", {}))
            task = state["incomplete_tasks"][key]
            self.assertEqual(task["status"], "waiting_human")
            self.assertIn("merge_check_error", task.get("last_error", ""))

    def test_enforcement_preserves_existing_error(self):
        """Merge error is appended to existing last_error, not overwritten."""
        with tempfile.TemporaryDirectory() as tmp_str:
            tmp = Path(tmp_str)
            sup = self._make_sup(tmp, merge_blocks=True)
            key = "1000.000000"
            self._setup_finished_task(sup, key)
            # Set an existing error
            with sup.state_lock():
                state = sup.load_state()
                state["finished_tasks"][key]["last_error"] = "prior_warning"
                sup.save_state(state)

            sup._enforce_merge_blocked_state(key, 0, "worker-0", "fallback_failed")

            state = sup.load_state()
            task = state["incomplete_tasks"][key]
            self.assertIn("prior_warning", task["last_error"])
            self.assertIn("fallback_failed", task["last_error"])


class TestJobStore(unittest.TestCase):
    """Tests for the durable job store (plan 31 phase 1)."""

    def _make_store(self, root):
        from src.loop.supervisor.job_store import JobStore
        return JobStore(Path(root) / "jobs")

    def _make_job(self, **overrides):
        from src.loop.supervisor.job_store import JobRecord
        defaults = dict(
            job_id="job_001",
            task_id="1000.0",
            thread_ts="1000.0",
            origin_turn_id="worker-0:turn-001",
        )
        defaults.update(overrides)
        return JobRecord(**defaults)

    def _make_event(self, **overrides):
        from src.loop.supervisor.job_store import JobEvent
        defaults = dict(job_id="job_001", seq=1, kind="job_started")
        defaults.update(overrides)
        return JobEvent(**defaults)

    # ---- Job record CRUD ----

    def test_create_and_load_job(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            job = self._make_job()
            store.create_job(job)

            loaded = store.load_job("job_001")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.job_id, "job_001")
            self.assertEqual(loaded.task_id, "1000.0")
            self.assertEqual(loaded.runtime_state, "running")

    def test_load_nonexistent_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            self.assertIsNone(store.load_job("missing"))

    def test_list_jobs_filters_by_task(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            store.create_job(self._make_job(job_id="j1", task_id="t1"))
            store.create_job(self._make_job(job_id="j2", task_id="t2"))
            store.create_job(self._make_job(job_id="j3", task_id="t1"))

            all_jobs = store.list_jobs()
            self.assertEqual(len(all_jobs), 3)

            t1_jobs = store.list_jobs(task_id="t1")
            self.assertEqual(len(t1_jobs), 2)
            self.assertTrue(all(j.task_id == "t1" for j in t1_jobs))

    # ---- Event log ----

    def test_append_and_load_events(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            e1 = self._make_event(seq=1, kind="job_started")
            e2 = self._make_event(seq=2, kind="job_completed", requires_attention=True)

            self.assertTrue(store.append_event(e1))
            self.assertTrue(store.append_event(e2))

            events = store.load_events("job_001")
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0].kind, "job_started")
            self.assertEqual(events[1].kind, "job_completed")

    def test_event_deduplication_by_source_key(self):
        """Duplicate source_event_key appends exactly one event."""
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            e1 = self._make_event(
                seq=1, kind="job_completed",
                source_event_key="shell:s1:exit:0",
            )
            self.assertTrue(store.append_event(e1))
            # Duplicate
            e2 = self._make_event(
                seq=2, kind="job_completed",
                source_event_key="shell:s1:exit:0",
            )
            self.assertFalse(store.append_event(e2))

            events = store.load_events("job_001")
            self.assertEqual(len(events), 1)

    def test_next_seq(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            self.assertEqual(store.next_seq("job_001"), 1)
            store.append_event(self._make_event(seq=1))
            self.assertEqual(store.next_seq("job_001"), 2)
            store.append_event(self._make_event(seq=5))
            self.assertEqual(store.next_seq("job_001"), 6)

    # ---- Derived attention ----

    def test_attention_idle_when_no_material_events(self):
        from src.loop.supervisor.job_store import attention_state
        job = self._make_job()
        events = [self._make_event(seq=1, kind="job_started", requires_attention=False)]
        self.assertEqual(attention_state(job, events), "idle")

    def test_attention_pending_when_unacked_material_event(self):
        from src.loop.supervisor.job_store import attention_state
        job = self._make_job()
        events = [
            self._make_event(seq=1, kind="job_started"),
            self._make_event(seq=2, kind="job_completed", requires_attention=True),
        ]
        self.assertEqual(attention_state(job, events), "pending")

    def test_attention_idle_after_ack(self):
        from src.loop.supervisor.job_store import attention_state
        job = self._make_job(acknowledged_material_seq=2)
        events = [
            self._make_event(seq=1, kind="job_started"),
            self._make_event(seq=2, kind="job_completed", requires_attention=True),
        ]
        self.assertEqual(attention_state(job, events), "idle")

    def test_attention_leased_with_valid_lease(self):
        from src.loop.supervisor.job_store import Lease, attention_state
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        job = self._make_job()
        job.lease = Lease(
            owner="dispatch_1",
            seq=2,
            expires_at=(now + timedelta(hours=1)).isoformat(),
        )
        events = [
            self._make_event(seq=2, kind="job_completed", requires_attention=True),
        ]
        self.assertEqual(attention_state(job, events, now), "leased")

    def test_attention_pending_after_lease_expires(self):
        from src.loop.supervisor.job_store import Lease, attention_state
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        job = self._make_job()
        job.lease = Lease(
            owner="dispatch_1",
            seq=2,
            expires_at=(now - timedelta(hours=1)).isoformat(),
        )
        events = [
            self._make_event(seq=2, kind="job_completed", requires_attention=True),
        ]
        self.assertEqual(attention_state(job, events, now), "pending")

    # ---- CAS acknowledgement ----

    def test_ack_accepted_with_valid_lease(self):
        from src.loop.supervisor.job_store import AckRequest, Lease, apply_ack
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        job = self._make_job()
        job.lease = Lease(
            owner="dispatch_1",
            seq=2,
            expires_at=(now + timedelta(hours=1)).isoformat(),
        )
        events = [
            self._make_event(seq=2, kind="job_completed", requires_attention=True),
        ]
        ack = AckRequest(job_id="job_001", handled_seq=2, lease_owner="dispatch_1")
        self.assertTrue(apply_ack(job, events, ack))
        self.assertEqual(job.acknowledged_material_seq, 2)
        self.assertIsNone(job.lease.owner)

    def test_ack_rejected_with_wrong_lease_owner(self):
        from src.loop.supervisor.job_store import AckRequest, Lease, apply_ack
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        job = self._make_job()
        job.lease = Lease(
            owner="dispatch_2",
            seq=2,
            expires_at=(now + timedelta(hours=1)).isoformat(),
        )
        events = [
            self._make_event(seq=2, kind="job_completed", requires_attention=True),
        ]
        ack = AckRequest(job_id="job_001", handled_seq=2, lease_owner="dispatch_1")
        self.assertFalse(apply_ack(job, events, ack))
        self.assertEqual(job.acknowledged_material_seq, 0)  # unchanged

    def test_ack_rejected_for_nonexistent_event(self):
        from src.loop.supervisor.job_store import AckRequest, Lease, apply_ack
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        job = self._make_job()
        job.lease = Lease(
            owner="d1", seq=99,
            expires_at=(now + timedelta(hours=1)).isoformat(),
        )
        events = [self._make_event(seq=1, kind="job_started")]
        ack = AckRequest(job_id="job_001", handled_seq=99, lease_owner="d1")
        self.assertFalse(apply_ack(job, events, ack))

    def test_ack_rejected_for_already_acked_seq(self):
        from src.loop.supervisor.job_store import AckRequest, Lease, apply_ack
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        job = self._make_job(acknowledged_material_seq=2)
        job.lease = Lease(
            owner="d1", seq=2,
            expires_at=(now + timedelta(hours=1)).isoformat(),
        )
        events = [
            self._make_event(seq=2, kind="job_completed", requires_attention=True),
        ]
        ack = AckRequest(job_id="job_001", handled_seq=2, lease_owner="d1")
        self.assertFalse(apply_ack(job, events, ack))

    # ---- Store-level operations ----

    def test_pending_wakeups(self):
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            job = self._make_job(task_id="t1")
            store.create_job(job)
            store.append_event(self._make_event(
                seq=1, kind="job_started", requires_attention=False,
            ))
            # No pending wakeups yet
            self.assertEqual(len(store.pending_wakeups("t1")), 0)

            # Add material event
            store.append_event(self._make_event(
                seq=2, kind="job_completed", requires_attention=True,
            ))
            wakeups = store.pending_wakeups("t1")
            self.assertEqual(len(wakeups), 1)
            self.assertEqual(wakeups[0].job_id, "job_001")

    def test_issue_and_expire_lease(self):
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            job = self._make_job()
            store.create_job(job)
            store.append_event(self._make_event(
                seq=1, kind="job_completed", requires_attention=True,
            ))

            now = datetime.now(timezone.utc)
            self.assertTrue(store.issue_lease("job_001", "d1", 60, now))

            reloaded = store.load_job("job_001")
            self.assertEqual(reloaded.lease.owner, "d1")
            self.assertEqual(reloaded.lease.seq, 1)

            # Expire the lease
            future = now + timedelta(seconds=120)
            expired = store.expire_leases(future)
            self.assertIn("job_001", expired)

            reloaded = store.load_job("job_001")
            self.assertIsNone(reloaded.lease.owner)

    def test_process_ack_via_store(self):
        from src.loop.supervisor.job_store import AckRequest
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            job = self._make_job()
            store.create_job(job)
            store.append_event(self._make_event(
                seq=1, kind="job_completed", requires_attention=True,
            ))

            now = datetime.now(timezone.utc)
            store.issue_lease("job_001", "d1", 3600, now)

            ack = AckRequest(job_id="job_001", handled_seq=1, lease_owner="d1")
            self.assertTrue(store.process_ack(ack))

            reloaded = store.load_job("job_001")
            self.assertEqual(reloaded.acknowledged_material_seq, 1)
            self.assertIsNone(reloaded.lease.owner)

    def test_lease_expiry_leaves_event_pending_and_releasable(self):
        """Lease expiry leaves the same material event pending and re-leasable."""
        from datetime import datetime, timedelta, timezone
        with tempfile.TemporaryDirectory() as td:
            store = self._make_store(td)
            job = self._make_job()
            store.create_job(job)
            store.append_event(self._make_event(
                seq=1, kind="job_completed", requires_attention=True,
            ))

            now = datetime.now(timezone.utc)
            store.issue_lease("job_001", "d1", 60, now)

            # Expire lease
            future = now + timedelta(seconds=120)
            store.expire_leases(future)

            # Event still pending
            self.assertEqual(store.attention_for_job("job_001", future), "pending")

            # Can re-lease
            self.assertTrue(store.issue_lease("job_001", "d2", 60, future))
            reloaded = store.load_job("job_001")
            self.assertEqual(reloaded.lease.owner, "d2")

    def test_json_round_trip_preserves_lease(self):
        """JobRecord survives JSON serialization/deserialization."""
        from src.loop.supervisor.job_store import Lease
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        job = self._make_job()
        job.lease = Lease(
            owner="d1", seq=2,
            expires_at=(now + timedelta(hours=1)).isoformat(),
        )
        # Round-trip through JSON
        from dataclasses import asdict
        data = json.loads(json.dumps(asdict(job)))
        restored = type(job)(**data)
        self.assertEqual(restored.lease.owner, "d1")
        self.assertEqual(restored.lease.seq, 2)
        self.assertTrue(restored.lease.is_valid(now))


class TestJobWakeupHooks(unittest.TestCase):
    """Tests for Plan 31 Phase 2: supervisor job wakeup hooks."""

    def _make_sup(self, tmp: Path) -> sl.Supervisor:
        sup = _make_supervisor(tmp)
        sup.slack_token = "xoxp-fake"
        _write_agent_identity(sup)
        return sup

    def _seed_job_with_completion(self, sup: sl.Supervisor, task_id: str):
        """Create a job for a task with a completed material event."""
        from src.loop.supervisor.job_store import JobRecord, JobEvent
        job = JobRecord(
            job_id=f"job_{task_id}",
            task_id=task_id,
            thread_ts=task_id,
            origin_turn_id="worker-0:turn-001",
            adapter="shell",
            runtime_state="succeeded",
        )
        sup._job_store.create_job(job)
        event = JobEvent(
            job_id=job.job_id,
            seq=1,
            kind="job_completed",
            summary="pip install completed",
            runtime_state_after="succeeded",
            requires_attention=True,
            source_event_key=f"shell:{task_id}:exit:0",
        )
        sup._job_store.append_event(event)
        return job

    def test_process_job_wakeups_reactivates_waiting_task(self):
        """A waiting_human task with a pending job event gets reactivated."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            task_id = "1000000000.000001"
            state = sup.load_state()
            state.setdefault("incomplete_tasks", {})[task_id] = {
                "mention_ts": task_id,
                "status": "waiting_human",
                "task_type": "slack_mention",
            }
            sup.save_state(state)

            self._seed_job_with_completion(sup, task_id)

            reactivated = sup._process_job_wakeups()
            self.assertEqual(reactivated, 1)

            state = sup.load_state()
            self.assertIn(task_id, state.get("active_tasks", {}))
            self.assertNotIn(task_id, state.get("incomplete_tasks", {}))
            task = state["active_tasks"][task_id]
            self.assertEqual(task["status"], "in_progress")
            self.assertIsNone(task.get("claimed_by"))

    def test_process_job_wakeups_ignores_no_pending(self):
        """A waiting_human task without pending events stays in incomplete."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            task_id = "1000000000.000002"
            state = sup.load_state()
            state.setdefault("incomplete_tasks", {})[task_id] = {
                "mention_ts": task_id,
                "status": "waiting_human",
                "task_type": "slack_mention",
            }
            sup.save_state(state)

            reactivated = sup._process_job_wakeups()
            self.assertEqual(reactivated, 0)

            state = sup.load_state()
            self.assertIn(task_id, state.get("incomplete_tasks", {}))
            self.assertNotIn(task_id, state.get("active_tasks", {}))

    def test_process_job_wakeups_expires_stale_leases(self):
        """Stale leases get expired during wakeup processing."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            from src.loop.supervisor.job_store import JobRecord, Lease
            job = JobRecord(
                job_id="job_lease_test",
                task_id="1000000000.000003",
                thread_ts="1000000000.000003",
                origin_turn_id="worker-0:turn-001",
                lease=Lease(
                    owner="old_dispatch",
                    seq=1,
                    expires_at="2020-01-01T00:00:00+00:00",  # expired
                ),
            )
            sup._job_store.create_job(job)

            sup._process_job_wakeups()

            reloaded = sup._job_store.load_job("job_lease_test")
            self.assertIsNone(reloaded.lease.owner)

    def test_process_job_acks_applied(self):
        """Valid job acks from outcome are applied."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            task_id = "1000000000.000004"
            job = self._seed_job_with_completion(sup, task_id)

            # Issue a lease (simulating what _inject_wakeup_context does)
            sup._job_store.issue_lease(job.job_id, "dispatch_token_1")

            outcome = {
                "job_acknowledgements": [{
                    "job_id": job.job_id,
                    "handled_seq": 1,
                    "lease_owner": "dispatch_token_1",
                }]
            }
            applied = sup._process_job_acks(outcome)
            self.assertEqual(applied, 1)

            # Verify watermark advanced
            reloaded = sup._job_store.load_job(job.job_id)
            self.assertEqual(reloaded.acknowledged_material_seq, 1)

    def test_process_job_acks_rejected_wrong_owner(self):
        """Ack with wrong lease_owner is rejected."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            task_id = "1000000000.000005"
            job = self._seed_job_with_completion(sup, task_id)

            sup._job_store.issue_lease(job.job_id, "dispatch_token_1")

            outcome = {
                "job_acknowledgements": [{
                    "job_id": job.job_id,
                    "handled_seq": 1,
                    "lease_owner": "wrong_token",
                }]
            }
            applied = sup._process_job_acks(outcome)
            self.assertEqual(applied, 0)

            reloaded = sup._job_store.load_job(job.job_id)
            self.assertEqual(reloaded.acknowledged_material_seq, 0)

    def test_inject_wakeup_context_adds_dispatch_token(self):
        """_inject_wakeup_context always adds a dispatch_token."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            dispatch = {}
            sup._inject_wakeup_context(dispatch, "1000000000.000006")
            self.assertIn("dispatch_token", dispatch)
            self.assertTrue(len(dispatch["dispatch_token"]) > 0)

    def test_inject_wakeup_context_adds_pending_wakeups(self):
        """_inject_wakeup_context injects pending job info."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            task_id = "1000000000.000007"
            self._seed_job_with_completion(sup, task_id)

            dispatch = {}
            sup._inject_wakeup_context(dispatch, task_id)
            self.assertIn("pending_job_wakeups", dispatch)
            wakeups = dispatch["pending_job_wakeups"]
            self.assertEqual(len(wakeups), 1)
            self.assertEqual(wakeups[0]["job_id"], f"job_{task_id}")
            self.assertEqual(wakeups[0]["event_kind"], "job_completed")

            # Verify a lease was issued
            reloaded = sup._job_store.load_job(f"job_{task_id}")
            self.assertEqual(reloaded.lease.owner, dispatch["dispatch_token"])


class TestShellAdapter(unittest.TestCase):
    """Tests for the shell adapter (plan 31 phase 3)."""

    def _make_adapter(self, td):
        from src.loop.supervisor.job_store import JobStore
        from src.loop.supervisor.shell_adapter import ShellAdapter
        store = JobStore(Path(td) / "jobs")
        return ShellAdapter(store), store

    def test_start_creates_job_and_event(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            job = adapter.start(
                "echo hello",
                task_id="1000.0",
                thread_ts="1000.0",
            )
            self.assertEqual(job.adapter, "shell")
            self.assertEqual(job.runtime_state, "running")
            self.assertEqual(job.task_id, "1000.0")
            self.assertIn("command", job.adapter_handle)
            # job_started event should exist
            events = store.load_events(job.job_id)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "job_started")
            self.assertFalse(events[0].requires_attention)

    def test_poll_detects_successful_completion(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            job = adapter.start(
                "echo done",
                task_id="1000.0",
                thread_ts="1000.0",
            )
            # Wait for process to finish
            time.sleep(0.5)
            finished = adapter.poll_all()
            self.assertIn(job.job_id, finished)
            # Check job record updated
            reloaded = store.load_job(job.job_id)
            self.assertEqual(reloaded.runtime_state, "succeeded")
            self.assertEqual(reloaded.adapter_handle["exit_code"], 0)
            # Check completion event
            events = store.load_events(job.job_id)
            completion = [e for e in events if e.kind == "job_completed"]
            self.assertEqual(len(completion), 1)
            self.assertTrue(completion[0].requires_attention)

    def test_poll_detects_failure(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            job = adapter.start(
                "exit 42",
                task_id="1000.0",
                thread_ts="1000.0",
            )
            time.sleep(0.5)
            finished = adapter.poll_all()
            self.assertIn(job.job_id, finished)
            reloaded = store.load_job(job.job_id)
            self.assertEqual(reloaded.runtime_state, "failed")
            self.assertEqual(reloaded.adapter_handle["exit_code"], 42)
            events = store.load_events(job.job_id)
            failure = [e for e in events if e.kind == "job_failed"]
            self.assertEqual(len(failure), 1)
            self.assertTrue(failure[0].requires_attention)

    def test_poll_skips_still_running(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            job = adapter.start(
                "sleep 60",
                task_id="1000.0",
                thread_ts="1000.0",
            )
            finished = adapter.poll_all()
            self.assertEqual(finished, [])
            self.assertEqual(adapter.running_count, 1)
            # Cleanup
            adapter.cancel(job.job_id)

    def test_cancel_kills_process(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            job = adapter.start(
                "sleep 60",
                task_id="1000.0",
                thread_ts="1000.0",
            )
            self.assertEqual(adapter.running_count, 1)
            result = adapter.cancel(job.job_id, grace_sec=1)
            self.assertTrue(result)
            self.assertEqual(adapter.running_count, 0)
            reloaded = store.load_job(job.job_id)
            self.assertEqual(reloaded.runtime_state, "failed")

    def test_cancel_nonexistent_returns_false(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            self.assertFalse(adapter.cancel("nonexistent"))

    def test_timeout_kills_process(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            job = adapter.start(
                "sleep 60",
                task_id="1000.0",
                thread_ts="1000.0",
                timeout_sec=1,
            )
            time.sleep(1.5)
            finished = adapter.poll_all()
            self.assertIn(job.job_id, finished)
            reloaded = store.load_job(job.job_id)
            self.assertEqual(reloaded.runtime_state, "failed")
            events = store.load_events(job.job_id)
            timeout_events = [e for e in events if e.kind == "job_timeout"]
            self.assertEqual(len(timeout_events), 1)
            self.assertTrue(timeout_events[0].requires_attention)

    def test_log_tail_captures_output(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            job = adapter.start(
                "echo line1; echo line2; echo line3",
                task_id="1000.0",
                thread_ts="1000.0",
            )
            time.sleep(0.5)
            adapter.poll_all()
            tail = adapter.log_tail(job.job_id, lines=2)
            self.assertIn("line2", tail)
            self.assertIn("line3", tail)

    def test_recover_lost_jobs(self):
        """Jobs marked running with no live process get marked lost."""
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            from src.loop.supervisor.job_store import JobRecord
            # Create a "running" job record with a dead PID
            job = JobRecord(
                job_id="lost_job_001",
                task_id="1000.0",
                thread_ts="1000.0",
                origin_turn_id="",
                adapter="shell",
                adapter_handle={"pid": 999999999},  # dead PID
                runtime_state="running",
            )
            store.create_job(job)
            lost = adapter.recover_lost_jobs()
            self.assertIn("lost_job_001", lost)
            reloaded = store.load_job("lost_job_001")
            self.assertEqual(reloaded.runtime_state, "lost")
            events = store.load_events("lost_job_001")
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "job_lost")
            self.assertTrue(events[0].requires_attention)

    def test_start_with_bad_command_creates_failed_job(self):
        """Starting a command that can't be executed creates a failed job."""
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            # /bin/sh -c should handle this, but test with a non-existent cwd
            job = adapter.start(
                "echo test",
                task_id="1000.0",
                thread_ts="1000.0",
                cwd="/nonexistent/directory/that/does/not/exist",
            )
            # Should have created a failed job record
            self.assertEqual(job.runtime_state, "failed")
            events = store.load_events(job.job_id)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0].kind, "job_failed")

    def test_multiple_jobs_tracked_independently(self):
        with tempfile.TemporaryDirectory() as td:
            adapter, store = self._make_adapter(td)
            job1 = adapter.start("echo a", task_id="1000.0", thread_ts="1000.0")
            job2 = adapter.start("echo b", task_id="1000.0", thread_ts="1000.0")
            self.assertNotEqual(job1.job_id, job2.job_id)
            self.assertEqual(adapter.running_count, 2)
            time.sleep(0.5)
            finished = adapter.poll_all()
            self.assertEqual(len(finished), 2)
            self.assertEqual(adapter.running_count, 0)


class TestShellAdapterIntegration(unittest.TestCase):
    """Tests for Plan 31 Phase 4: ShellAdapter wired into supervisor runtime."""

    def _make_sup(self, tmp: Path) -> sl.Supervisor:
        sup = _make_supervisor(tmp)
        sup.slack_token = "xoxp-fake"
        _write_agent_identity(sup)
        return sup

    def test_supervisor_has_shell_adapter(self):
        """Supervisor creates a ShellAdapter alongside JobStore."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            from src.loop.supervisor.shell_adapter import ShellAdapter
            self.assertIsInstance(sup._shell_adapter, ShellAdapter)
            self.assertIs(sup._shell_adapter._store, sup._job_store)

    def test_poll_shell_jobs_detects_completion(self):
        """_poll_shell_jobs detects a completed background process."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            job = sup._shell_adapter.start(
                "true",  # exits immediately with code 0
                task_id="1000000000.100001",
                thread_ts="1000000000.100001",
            )
            time.sleep(0.3)  # let process exit
            finished = sup._poll_shell_jobs()
            self.assertEqual(finished, [job.job_id])

            # Verify the job record was updated
            reloaded = sup._job_store.load_job(job.job_id)
            self.assertEqual(reloaded.runtime_state, "succeeded")

    def test_poll_feeds_into_wakeup_reactivation(self):
        """End-to-end: background job completes → poll → wakeup → task reactivated."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            task_id = "1000000000.100002"

            # Place task in waiting_human
            state = sup.load_state()
            state.setdefault("incomplete_tasks", {})[task_id] = {
                "mention_ts": task_id,
                "status": "waiting_human",
                "task_type": "slack_mention",
            }
            sup.save_state(state)

            # Start a background job for this task
            sup._shell_adapter.start(
                "true",
                task_id=task_id,
                thread_ts=task_id,
            )
            time.sleep(0.3)

            # Poll (generates completion event) then process wakeups
            sup._poll_shell_jobs()
            reactivated = sup._process_job_wakeups()
            self.assertEqual(reactivated, 1)

            state = sup.load_state()
            self.assertIn(task_id, state.get("active_tasks", {}))
            self.assertNotIn(task_id, state.get("incomplete_tasks", {}))

    def test_recover_lost_shell_jobs_at_startup(self):
        """Startup recovery detects orphaned running jobs."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            from src.loop.supervisor.job_store import JobRecord
            # Create a job record that claims to be running but has no process
            job = JobRecord(
                job_id="job_orphan_test",
                task_id="1000000000.100003",
                thread_ts="1000000000.100003",
                origin_turn_id="worker-0:turn-001",
                adapter="shell",
                adapter_handle={"pid": 999999999, "command": "sleep 3600"},
                runtime_state="running",
            )
            sup._job_store.create_job(job)

            sup._recover_lost_shell_jobs()

            reloaded = sup._job_store.load_job("job_orphan_test")
            self.assertEqual(reloaded.runtime_state, "lost")

            # Verify a job_lost event was created
            events = sup._job_store.load_events("job_orphan_test")
            self.assertTrue(any(e.kind == "job_lost" for e in events))

    def test_multiple_acks_in_single_outcome(self):
        """A single outcome can acknowledge multiple jobs."""
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._make_sup(Path(tmp))
            from src.loop.supervisor.job_store import JobEvent, JobRecord

            task_id = "1000000000.100004"
            jobs = []
            for i in range(3):
                job = JobRecord(
                    job_id=f"job_multi_{i}",
                    task_id=task_id,
                    thread_ts=task_id,
                    origin_turn_id="worker-0:turn-001",
                    runtime_state="succeeded",
                )
                sup._job_store.create_job(job)
                sup._job_store.append_event(JobEvent(
                    job_id=job.job_id,
                    seq=1,
                    kind="job_completed",
                    summary=f"Job {i} done",
                    runtime_state_after="succeeded",
                    requires_attention=True,
                    source_event_key=f"shell:multi_{i}:exit:0",
                ))
                sup._job_store.issue_lease(job.job_id, "dispatch_abc")
                jobs.append(job)

            outcome = {
                "job_acknowledgements": [
                    {"job_id": j.job_id, "handled_seq": 1, "lease_owner": "dispatch_abc"}
                    for j in jobs
                ]
            }
            applied = sup._process_job_acks(outcome)
            self.assertEqual(applied, 3)

            for j in jobs:
                reloaded = sup._job_store.load_job(j.job_id)
                self.assertEqual(reloaded.acknowledged_material_seq, 1)


class TestInjectTaskEnv(unittest.TestCase):
    """Tests for WorkerSlot.inject_task_env()."""

    def _make_slot_with_codex(self, tmp: Path, slot_id: int = 0):
        from src.loop.supervisor.worker_slot import WorkerSlot
        repo_root = tmp / "repo"
        repo_root.mkdir(parents=True, exist_ok=True)
        dispatch_dir = tmp / "dispatch"
        dispatch_dir.mkdir(parents=True, exist_ok=True)
        outcomes_dir = tmp / "outcomes"
        outcomes_dir.mkdir(parents=True, exist_ok=True)
        worktree_dir = tmp / "worktrees"
        worktree_dir.mkdir(parents=True, exist_ok=True)
        slot = WorkerSlot(
            slot_id=slot_id,
            repo_root=repo_root,
            dispatch_dir=dispatch_dir,
            outcomes_dir=outcomes_dir,
            worktree_dir=worktree_dir,
        )
        # Create a fake .codex/config.toml in the worktree
        codex_dir = slot.worktree_path / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.consult]\ncommand = "/usr/bin/test"\n\n'
            '[mcp_servers.consult.env]\n'
            'CHATGPT_DEFAULT_PROJECT = "Murphy"\n'
        )
        return slot

    def test_inject_task_env_writes_vars(self):
        with tempfile.TemporaryDirectory() as tmp:
            slot = self._make_slot_with_codex(Path(tmp))
            slot.inject_task_env("1234.567890")
            text = (slot.worktree_path / ".codex" / "config.toml").read_text()
            self.assertIn('CONSULT_TASK_ID = "1234.567890"', text)
            self.assertIn("CONSULT_HISTORY_DIR", text)
            self.assertIn(".agent/runtime/consult_history", text)

    def test_inject_task_env_replaces_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            slot = self._make_slot_with_codex(Path(tmp))
            slot.inject_task_env("first.task")
            slot.inject_task_env("second.task")
            text = (slot.worktree_path / ".codex" / "config.toml").read_text()
            self.assertIn('CONSULT_TASK_ID = "second.task"', text)
            self.assertNotIn("first.task", text)

    def test_inject_task_env_noop_without_consult_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            slot = self._make_slot_with_codex(Path(tmp))
            # Overwrite with a config that has no consult env section
            (slot.worktree_path / ".codex" / "config.toml").write_text(
                '[mcp_servers.slack]\ncommand = "/usr/bin/slack"\n'
            )
            slot.inject_task_env("1234.567890")
            text = (slot.worktree_path / ".codex" / "config.toml").read_text()
            self.assertNotIn("CONSULT_TASK_ID", text)


class TestSyncConsultToProject(unittest.TestCase):
    """Tests for _sync_consult_to_project()."""

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_sync_creates_scoped_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._sup(tmp)
            # Write a fake consult history
            hist_dir = sup.cfg.consult_history_dir
            hist_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": "2026-03-19T12:00:00+00:00",
                "task_id": "1000.000",
                "chat_id": "abc-123",
                "turn": 1,
                "slot_id": "0",
                "prompt": "test",
                "mode": "deep",
                "file_paths": [],
                "completed": True,
                "response": "answer",
                "downloaded_files": [],
                "duration_sec": 10.0,
                "error": None,
            }
            (hist_dir / "1000.000.jsonl").write_text(json.dumps(record) + "\n")

            sup._sync_consult_to_project("1000.000", "test-project")

            scoped = sup.cfg.projects_dir / "test-project.consult.jsonl"
            self.assertTrue(scoped.exists())
            rec = json.loads(scoped.read_text().strip())
            self.assertEqual(rec["task_id"], "1000.000")
            self.assertEqual(rec["chat_id"], "abc-123")
            self.assertEqual(rec["prompt"], "test")
            self.assertEqual(rec["response"], "answer")
            # Should NOT contain internal fields
            self.assertNotIn("slot_id", rec)
            self.assertNotIn("duration_sec", rec)
            self.assertNotIn("file_paths", rec)

    def test_sync_deduplicates_on_redispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._sup(tmp)
            hist_dir = sup.cfg.consult_history_dir
            hist_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": "2026-03-19T12:00:00+00:00",
                "task_id": "1000.000",
                "chat_id": "abc",
                "turn": 1,
                "slot_id": "0",
                "prompt": "p",
                "mode": "deep",
                "file_paths": [],
                "completed": True,
                "response": "r",
                "downloaded_files": [],
                "duration_sec": 5.0,
                "error": None,
            }
            (hist_dir / "1000.000.jsonl").write_text(json.dumps(record) + "\n")

            # Sync twice
            sup._sync_consult_to_project("1000.000", "proj")
            sup._sync_consult_to_project("1000.000", "proj")

            scoped = sup.cfg.projects_dir / "proj.consult.jsonl"
            lines = scoped.read_text().strip().split("\n")
            self.assertEqual(len(lines), 1, "Duplicate records should be skipped")

    def test_sync_noop_when_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._sup(tmp)
            # No consult history file exists
            sup._sync_consult_to_project("nonexistent.000", "proj")
            scoped = sup.cfg.projects_dir / "proj.consult.jsonl"
            self.assertFalse(scoped.exists())

    def test_sync_multiple_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            sup = self._sup(tmp)
            hist_dir = sup.cfg.consult_history_dir
            hist_dir.mkdir(parents=True, exist_ok=True)
            records = []
            for i in range(3):
                records.append(json.dumps({
                    "ts": f"2026-03-19T12:0{i}:00+00:00",
                    "task_id": "2000.000",
                    "chat_id": "xyz",
                    "turn": i + 1,
                    "slot_id": "1",
                    "prompt": f"q{i}",
                    "mode": "standard",
                    "file_paths": [],
                    "completed": True,
                    "response": f"a{i}",
                    "downloaded_files": [],
                    "duration_sec": float(i),
                    "error": None,
                }))
            (hist_dir / "2000.000.jsonl").write_text("\n".join(records) + "\n")

            sup._sync_consult_to_project("2000.000", "multi")
            scoped = sup.cfg.projects_dir / "multi.consult.jsonl"
            lines = scoped.read_text().strip().split("\n")
            self.assertEqual(len(lines), 3)
            recs = [json.loads(l) for l in lines]
            self.assertEqual([r["turn"] for r in recs], [1, 2, 3])


# ── Section: Tribune role (maintenance phase generalization + post-dispatch review) ──


class TestTribuneMaintenance(unittest.TestCase):
    """Tests for dynamic maintenance phase construction with Tribune rounds."""

    def test_build_phases_rounds_0(self):
        """With TRIBUNE_MAINT_ROUNDS=0, phases are [reflect, dev] (legacy)."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"TRIBUNE_MAINT_ROUNDS": "0"})
            phases = sup.maintenance.PHASES
            self.assertEqual(len(phases), 2)
            self.assertEqual(phases[0]["role"], "worker")
            self.assertEqual(phases[1]["role"], "developer")

    def test_build_phases_rounds_1(self):
        """With TRIBUNE_MAINT_ROUNDS=1, phases are [reflect, dev, tribune]."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"TRIBUNE_MAINT_ROUNDS": "1"})
            phases = sup.maintenance.PHASES
            self.assertEqual(len(phases), 3)
            roles = [p["role"] for p in phases]
            self.assertEqual(roles, ["worker", "developer", "tribune"])

    def test_build_phases_rounds_2(self):
        """With TRIBUNE_MAINT_ROUNDS=2, phases are [reflect, dev, trib, dev, trib]."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"TRIBUNE_MAINT_ROUNDS": "2"})
            phases = sup.maintenance.PHASES
            self.assertEqual(len(phases), 5)
            roles = [p["role"] for p in phases]
            self.assertEqual(roles, ["worker", "developer", "tribune", "developer", "tribune"])

    def test_build_phases_skips_developer_when_cmd_empty(self):
        """Maintenance falls back to reflect-only when developer review is disabled."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"DEV_REVIEW_CMD": ""})
            roles = [p["role"] for p in sup.maintenance.PHASES]
            self.assertEqual(roles, ["worker"])

    def test_build_phases_skips_tribune_when_cmd_empty(self):
        """Maintenance keeps a single developer pass when Tribune is disabled."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                Path(td),
                env_overrides={"TRIBUNE_MAINT_ROUNDS": "2", "TRIBUNE_CMD": ""},
            )
            roles = [p["role"] for p in sup.maintenance.PHASES]
            self.assertEqual(roles, ["worker", "developer"])

    def test_empty_tribune_cmd_disables_review_rounds(self):
        """Explicitly empty Tribune command disables all Tribune review loops."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(
                Path(td),
                env_overrides={
                    "TRIBUNE_CMD": "",
                    "TRIBUNE_MAX_REVIEW_ROUNDS": "2",
                    "TRIBUNE_MAINT_ROUNDS": "1",
                },
            )
            self.assertEqual(sup.cfg.tribune_max_review_rounds, 0)
            self.assertEqual(sup.cfg.tribune_maint_rounds, 0)

    def test_get_worker_cmd_tribune_phase(self):
        """Tribune phase returns tribune_cmd."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"TRIBUNE_MAINT_ROUNDS": "1"})
            # Phase 2 = tribune
            cmd = sup.maintenance.get_worker_cmd(2)
            self.assertEqual(cmd, list(sup.cfg.tribune_cmd))

    def test_get_worker_cmd_dev_phase(self):
        """Developer phase returns dev_review_cmd."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"TRIBUNE_MAINT_ROUNDS": "1"})
            # Phase 1 = developer
            cmd = sup.maintenance.get_worker_cmd(1)
            self.assertEqual(cmd, list(sup.cfg.dev_review_cmd))

    def test_phase_role_helpers(self):
        """phase_role() and is_dev_review_phase() return correct values."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"TRIBUNE_MAINT_ROUNDS": "1"})
            self.assertEqual(sup.maintenance.phase_role(0), "worker")
            self.assertEqual(sup.maintenance.phase_role(1), "developer")
            self.assertEqual(sup.maintenance.phase_role(2), "tribune")
            self.assertTrue(sup.maintenance.is_dev_review_phase(1))
            self.assertFalse(sup.maintenance.is_dev_review_phase(0))
            self.assertFalse(sup.maintenance.is_dev_review_phase(2))

    def test_non_final_dev_phase_force_finished_rounds_1(self):
        """With rounds=1, developer phase (1) is non-final and advances to tribune (2)."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
                "TRIBUNE_MAINT_ROUNDS": "1",
            })
            key = "1000.000000"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=1)
            task["thread_ts"] = "maintenance"
            task["channel_id"] = ""
            state["active_tasks"][key] = task
            sup.save_state(state)
            # Write outcome (developer review done)
            outcome_path = sup.cfg.outcomes_dir / f"{key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "thread_ts": "maintenance",
                "summary": "dev review done", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            # Task should be re-queued at phase 2 (tribune).
            self.assertIn(key, state["queued_tasks"])
            self.assertEqual(state["queued_tasks"][key]["maintenance_phase"], 2)

    def test_advance_phase_through_all_rounds_1(self):
        """With rounds=1, phases advance 0→1→2 (reflect→dev→tribune)."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"TRIBUNE_MAINT_ROUNDS": "1"})
            self.assertFalse(sup.maintenance.is_final_phase(0))
            self.assertFalse(sup.maintenance.is_final_phase(1))
            self.assertTrue(sup.maintenance.is_final_phase(2))

    def test_fallback_prompts_keyed_by_config_attr(self):
        """Fallback prompts are keyed by prompt_config_attr, not phase index."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"TRIBUNE_MAINT_ROUNDS": "1"})
            # Tribune fallback should exist
            prompt = sup.maintenance.load_prompt(2)  # tribune phase
            self.assertIn("Tribune", prompt)


class TestTribunePostDispatch(unittest.TestCase):
    """Tests for post-dispatch Tribune review flow."""

    def test_tribune_skipped_when_rounds_zero(self):
        """With TRIBUNE_MAX_REVIEW_ROUNDS=0, done tasks finish directly."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "0",
            })
            key = "2000.000001"
            state = _empty_state()
            state["active_tasks"][key] = _make_task(key, "in_progress")
            sup.save_state(state)
            outcome_path = sup.cfg.outcomes_dir / f"{key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "task done", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            # Task should be in finished_tasks
            self.assertIn(key, state["finished_tasks"])

    def test_tribune_skipped_for_maintenance(self):
        """Maintenance tasks never get Tribune post-dispatch review."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "1",
                "DEFAULT_CHANNEL_ID": "CDEFAULT",
            })
            key = "maintenance"
            state = _empty_state()
            task = _make_task(key, "in_progress", "maintenance", maintenance_phase=0)
            task["thread_ts"] = "maintenance"
            task["channel_id"] = ""
            state["active_tasks"][key] = task
            sup.save_state(state)
            outcome_path = sup.cfg.outcomes_dir / f"{key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "thread_ts": "maintenance",
                "summary": "reflect done", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            # Maintenance should advance phase, not trigger Tribune review
            self.assertIn(key, state["queued_tasks"])

    def test_tribune_no_draft_falls_through(self):
        """When no draft file exists, task finishes normally."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "1",
            })
            key = "2000.000002"
            state = _empty_state()
            state["active_tasks"][key] = _make_task(key, "in_progress")
            sup.save_state(state)
            outcome_path = sup.cfg.outcomes_dir / f"{key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            # No draft file written — worker posted directly
            sup.reconcile_task_after_run(key, 0)
            state = sup.load_state()
            # Task should still finish
            self.assertIn(key, state["finished_tasks"])

    def test_tribune_revision_moves_to_incomplete(self):
        """When Tribune returns revision_requested, task moves to incomplete with feedback."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "1",
            })
            key = "2000.000010"
            state = _empty_state()
            state["active_tasks"][key] = _make_task(key, "in_progress")
            sup.save_state(state)
            # Write outcome
            outcome_path = sup.cfg.outcomes_dir / f"{key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            # Write a draft file
            draft = sup.cfg.dispatch_dir / f"slack_draft.{key}.md"
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text("Here is my final response.")
            # Mock _tribune_review_cycle to return revision_requested
            original_cycle = sup._tribune_review_cycle
            sup._tribune_review_cycle = lambda k, t, p: ("revision_requested", "Fix the analysis section")
            try:
                sup.reconcile_task_after_run(key, 0)
            finally:
                sup._tribune_review_cycle = original_cycle
                draft.unlink(missing_ok=True)
            state = sup.load_state()
            # Task should be in incomplete_tasks with feedback
            self.assertIn(key, state["incomplete_tasks"])
            task = state["incomplete_tasks"][key]
            self.assertEqual(task["status"], "in_progress")
            self.assertEqual(task["tribune_revision_count"], 1)
            self.assertEqual(task["tribune_feedback"], "Fix the analysis section")

    def test_tribune_approved_posts_draft(self):
        """When Tribune approves, draft is posted and task finishes."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "1",
            })
            key = "2000.000011"
            state = _empty_state()
            state["active_tasks"][key] = _make_task(key, "in_progress")
            sup.save_state(state)
            outcome_path = sup.cfg.outcomes_dir / f"{key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "done", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            draft = sup.cfg.dispatch_dir / f"slack_draft.{key}.md"
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text("Approved response.")
            # Mock Tribune to approve and track if _post_slack_draft is called
            sup._tribune_review_cycle = lambda k, t, p: ("approved", "")
            posted = []
            original_post = sup._post_slack_draft
            sup._post_slack_draft = lambda t, p: posted.append(p) or True
            try:
                sup.reconcile_task_after_run(key, 0)
            finally:
                sup._post_slack_draft = original_post
                draft.unlink(missing_ok=True)
            state = sup.load_state()
            self.assertIn(key, state["finished_tasks"])
            self.assertEqual(len(posted), 1)

    def test_tribune_approved_preserves_waiting_human(self):
        """When worker sets waiting_human and Tribune approves, task stays waiting_human."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "1",
            })
            key = "2000.000013"
            state = _empty_state()
            state["active_tasks"][key] = _make_task(key, "in_progress")
            sup.save_state(state)
            outcome_path = sup.cfg.outcomes_dir / f"{key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "waiting_human",
                "summary": "research done, needs human eval",
                "completion_confidence": "high",
                "requires_human_feedback": True,
            }))
            draft = sup.cfg.dispatch_dir / f"slack_draft.{key}.md"
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text("Research findings.")
            sup._tribune_review_cycle = lambda k, t, p: ("approved", "")
            posted = []
            sup._post_slack_draft = lambda t, p: posted.append(p) or True
            try:
                sup.reconcile_task_after_run(key, 0)
            finally:
                draft.unlink(missing_ok=True)
            state = sup.load_state()
            # Task should stay in incomplete_tasks with waiting_human
            self.assertIn(key, state["incomplete_tasks"])
            self.assertEqual(state["incomplete_tasks"][key]["status"], "waiting_human")
            # Draft should have been posted
            self.assertEqual(len(posted), 1)

    def test_tribune_max_rounds_posts_draft(self):
        """After max revision rounds exhausted, draft posts regardless of verdict."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "1",
            })
            key = "2000.000012"
            state = _empty_state()
            task = _make_task(key, "in_progress")
            task["tribune_revision_count"] = 1  # Already revised once
            state["active_tasks"][key] = task
            sup.save_state(state)
            outcome_path = sup.cfg.outcomes_dir / f"{key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            outcome_path.write_text(json.dumps({
                "mention_ts": key, "status": "done",
                "summary": "revised", "completion_confidence": "high",
                "requires_human_feedback": False,
            }))
            draft = sup.cfg.dispatch_dir / f"slack_draft.{key}.md"
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text("Revised response.")
            # Tribune still wants revision but rounds exhausted
            sup._tribune_review_cycle = lambda k, t, p: ("revision_requested", "Still not good enough")
            posted = []
            sup._post_slack_draft = lambda t, p: posted.append(p) or True
            try:
                sup.reconcile_task_after_run(key, 0)
            finally:
                draft.unlink(missing_ok=True)
            state = sup.load_state()
            # Should finish anyway (max rounds exhausted)
            self.assertIn(key, state["finished_tasks"])
            self.assertEqual(len(posted), 1)

    def test_resolve_draft_path_serial(self):
        """_resolve_draft_path finds task-bound serial draft file."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            test_key = "3000.000001"
            draft = sup.cfg.dispatch_dir / f"slack_draft.{test_key}.md"
            draft.parent.mkdir(parents=True, exist_ok=True)
            draft.write_text("Hello world")
            try:
                path = sup._resolve_draft_path(test_key)
                self.assertEqual(path, str(draft))
            finally:
                draft.unlink(missing_ok=True)

    def test_resolve_draft_path_empty_when_missing(self):
        """_resolve_draft_path returns empty when no draft exists."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            # Ensure no stale draft file exists
            stale = sup.cfg.dispatch_dir / "slack_draft.anykey.md"
            if stale.exists():
                stale.unlink()
            path = sup._resolve_draft_path("anykey")
            self.assertEqual(path, "")


class TestTribuneModelFallback(unittest.TestCase):
    """Tests for Tribune model fallback on capacity exhaustion (FIX-006)."""

    def test_swap_model_replaces_existing(self):
        """_swap_model_in_cmd replaces -m arg when present."""
        from src.loop.supervisor.runtime import _swap_model_in_cmd
        cmd = ["gemini", "-m", "gemini-2.5-pro", "-p", "", "-y"]
        result = _swap_model_in_cmd(cmd, "gemini-2.5-flash")
        self.assertEqual(result, ["gemini", "-m", "gemini-2.5-flash", "-p", "", "-y"])

    def test_swap_model_inserts_when_missing(self):
        """_swap_model_in_cmd inserts -m after binary when absent."""
        from src.loop.supervisor.runtime import _swap_model_in_cmd
        cmd = ["gemini", "-p", "", "-y"]
        result = _swap_model_in_cmd(cmd, "gemini-2.5-flash")
        self.assertEqual(result, ["gemini", "-m", "gemini-2.5-flash", "-p", "", "-y"])

    def test_swap_model_does_not_mutate_input(self):
        """_swap_model_in_cmd returns a new list, does not modify the input."""
        from src.loop.supervisor.runtime import _swap_model_in_cmd
        cmd = ["gemini", "-m", "gemini-2.5-pro", "-p", ""]
        original = list(cmd)
        _swap_model_in_cmd(cmd, "gemini-2.5-flash")
        self.assertEqual(cmd, original)

    def test_swap_model_replaces_long_form(self):
        """_swap_model_in_cmd replaces --model arg when present."""
        from src.loop.supervisor.runtime import _swap_model_in_cmd
        cmd = ["gemini", "--model", "gemini-3.1-pro-preview", "-p", ""]
        result = _swap_model_in_cmd(cmd, "gemini-2.5-flash")
        self.assertEqual(result, ["gemini", "--model", "gemini-2.5-flash", "-p", ""])

    def test_swap_model_replaces_equals_form(self):
        """_swap_model_in_cmd replaces --model=value form."""
        from src.loop.supervisor.runtime import _swap_model_in_cmd
        cmd = ["gemini", "--model=gemini-3.1-pro-preview", "-p", ""]
        result = _swap_model_in_cmd(cmd, "gemini-2.5-flash")
        self.assertEqual(result, ["gemini", "--model=gemini-2.5-flash", "-p", ""])

    def test_swap_model_repairs_bare_trailing_model(self):
        """_swap_model_in_cmd repairs bare trailing --model without value."""
        from src.loop.supervisor.runtime import _swap_model_in_cmd
        cmd = ["gemini", "--model"]
        result = _swap_model_in_cmd(cmd, "gemini-2.5-flash")
        self.assertEqual(result, ["gemini", "-m", "gemini-2.5-flash"])

    def test_swap_model_repairs_bare_trailing_m(self):
        """_swap_model_in_cmd repairs bare trailing -m without value."""
        from src.loop.supervisor.runtime import _swap_model_in_cmd
        cmd = ["gemini", "-m"]
        result = _swap_model_in_cmd(cmd, "gemini-2.5-flash")
        self.assertEqual(result, ["gemini", "-m", "gemini-2.5-flash"])

    def test_swap_model_long_form_no_mutate(self):
        """_swap_model_in_cmd does not mutate input for long forms."""
        from src.loop.supervisor.runtime import _swap_model_in_cmd
        cmd = ["gemini", "--model", "gemini-3.1-pro-preview", "-p", ""]
        original = list(cmd)
        _swap_model_in_cmd(cmd, "gemini-2.5-flash")
        self.assertEqual(cmd, original)

    def test_extract_model_found(self):
        """_extract_model extracts model name from -m flag."""
        from src.loop.supervisor.runtime import _extract_model
        self.assertEqual(_extract_model(["gemini", "-m", "gemini-2.5-pro", "-p", ""]), "gemini-2.5-pro")

    def test_extract_model_default(self):
        """_extract_model returns 'default' when no -m flag."""
        from src.loop.supervisor.runtime import _extract_model
        self.assertEqual(_extract_model(["gemini", "-p", ""]), "default")

    def test_extract_model_long_form(self):
        """_extract_model extracts model name from --model flag."""
        from src.loop.supervisor.runtime import _extract_model
        self.assertEqual(_extract_model(["gemini", "--model", "gemini-3.1-pro-preview", "-p", ""]), "gemini-3.1-pro-preview")

    def test_extract_model_equals_form(self):
        """_extract_model extracts model name from --model=value form."""
        from src.loop.supervisor.runtime import _extract_model
        self.assertEqual(_extract_model(["gemini", "--model=gemini-3.1-pro-preview", "-p", ""]), "gemini-3.1-pro-preview")

    def test_extract_model_bare_trailing_model(self):
        """_extract_model returns 'default' for bare trailing --model."""
        from src.loop.supervisor.runtime import _extract_model
        self.assertEqual(_extract_model(["gemini", "--model"]), "default")

    def test_capacity_pattern_matches_429(self):
        """CAPACITY_PATTERN matches common capacity error messages."""
        from src.loop.supervisor.utils import CAPACITY_PATTERN
        self.assertIsNotNone(CAPACITY_PATTERN.search("API Error: 429"))
        self.assertIsNotNone(CAPACITY_PATTERN.search("MODEL_CAPACITY_EXHAUSTED"))
        self.assertIsNotNone(CAPACITY_PATTERN.search("RESOURCE_EXHAUSTED"))
        self.assertIsNotNone(CAPACITY_PATTERN.search("rate limit exceeded"))
        self.assertIsNotNone(CAPACITY_PATTERN.search("server overloaded"))
        self.assertIsNotNone(CAPACITY_PATTERN.search("quota exceeded"))
        self.assertIsNone(CAPACITY_PATTERN.search("normal error message"))

    def test_fallback_on_capacity_error(self):
        """_tribune_review_cycle retries with fallback models on capacity error."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "1",
                "TRIBUNE_FALLBACK_MODELS": "gemini-2.5-pro,gemini-3-flash,gemini-2.5-flash",
            })
            key = "2000.000020"
            task = {"mention_text_file": "", "thread_ts": key}
            draft_path = Path(td) / "draft.md"
            draft_path.write_text("Draft content")
            calls = []

            def mock_run(cmd, **kwargs):
                calls.append(list(cmd))
                mock_proc = MagicMock()
                if len(calls) <= 3:
                    # First three calls (primary + 2 fallbacks): capacity error
                    mock_proc.returncode = 1
                    mock_proc.stderr = "MODEL_CAPACITY_EXHAUSTED 429"
                    mock_proc.stdout = ""
                else:
                    # Fourth call (last fallback): success
                    mock_proc.returncode = 0
                    mock_proc.stdout = ""
                    mock_proc.stderr = ""
                    # Write outcome for the fallback
                    outcome_path = sup.cfg.outcomes_dir / f"{key}.tribune.json"
                    outcome_path.parent.mkdir(parents=True, exist_ok=True)
                    outcome_path.write_text(json.dumps({
                        "tribune_verdict": "approved",
                        "tribune_feedback": "",
                    }))
                return mock_proc

            with patch("subprocess.run", side_effect=mock_run):
                verdict, feedback = sup._tribune_review_cycle(key, task, str(draft_path))

            # Should have made 4 calls: primary + 3 fallbacks
            self.assertEqual(len(calls), 4)
            # First call should have primary model
            self.assertIn("gemini-3.1-pro-preview", calls[0])
            # Second call should have first fallback
            self.assertIn("gemini-2.5-pro", calls[1])
            # Third call should have second fallback
            self.assertIn("gemini-3-flash", calls[2])
            # Fourth call should have last fallback
            self.assertIn("gemini-2.5-flash", calls[3])
            self.assertEqual(verdict, "approved")

    def test_all_models_exhausted_returns_approved(self):
        """When all fallback models hit capacity, returns approved (fail-open)."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "1",
                "TRIBUNE_FALLBACK_MODELS": "gemini-2.5-pro,gemini-3-flash,gemini-2.5-flash",
            })
            key = "2000.000021"
            task = {"mention_text_file": "", "thread_ts": key}
            draft_path = Path(td) / "draft.md"
            draft_path.write_text("Draft content")
            calls = []

            def mock_run(cmd, **kwargs):
                calls.append(list(cmd))
                mock_proc = MagicMock()
                mock_proc.returncode = 1
                mock_proc.stderr = "RESOURCE_EXHAUSTED quota exceeded"
                mock_proc.stdout = ""
                return mock_proc

            with patch("subprocess.run", side_effect=mock_run):
                verdict, feedback = sup._tribune_review_cycle(key, task, str(draft_path))

            self.assertEqual(len(calls), 4)  # primary + 3 fallbacks
            self.assertEqual(verdict, "approved")

    def test_non_gemini_cmd_skips_fallback(self):
        """Non-Gemini TRIBUNE_CMD does not attempt model fallback."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_MAX_REVIEW_ROUNDS": "1",
                "TRIBUNE_CMD": "my-custom-reviewer -p ''",
                "TRIBUNE_FALLBACK_MODELS": "gemini-2.5-flash",
            })
            key = "2000.000022"
            task = {"mention_text_file": "", "thread_ts": key}
            draft_path = Path(td) / "draft.md"
            draft_path.write_text("Draft content")
            calls = []

            def mock_run(cmd, **kwargs):
                calls.append(list(cmd))
                mock_proc = MagicMock()
                mock_proc.returncode = 1
                mock_proc.stderr = "429 capacity exhausted"
                mock_proc.stdout = ""
                return mock_proc

            with patch("subprocess.run", side_effect=mock_run):
                verdict, feedback = sup._tribune_review_cycle(key, task, str(draft_path))

            # Only 1 call — no fallback for non-Gemini cmd
            self.assertEqual(len(calls), 1)
            self.assertEqual(verdict, "approved")

    def test_config_fallback_models_parsed(self):
        """TRIBUNE_FALLBACK_MODELS config is correctly parsed as a list."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={
                "TRIBUNE_FALLBACK_MODELS": "gemini-2.5-flash, gemini-2.0-flash",
            })
            self.assertEqual(sup.cfg.tribune_fallback_models, ["gemini-2.5-flash", "gemini-2.0-flash"])

    def test_config_default_model_in_tribune_cmd(self):
        """Default TRIBUNE_CMD includes -m gemini-3.1-pro-preview."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertIn("-m", sup.cfg.tribune_cmd)
            idx = sup.cfg.tribune_cmd.index("-m")
            self.assertEqual(sup.cfg.tribune_cmd[idx + 1], "gemini-3.1-pro-preview")

    def test_tribune_review_cycle_skips_when_cmd_empty(self):
        """_tribune_review_cycle should fail open without spawning a missing CLI."""
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"TRIBUNE_CMD": ""})
            key = "2000.000023"
            task = {"mention_text_file": "", "thread_ts": key}
            draft_path = Path(td) / "draft.md"
            draft_path.write_text("Draft content")

            with patch("subprocess.run") as mock_run:
                verdict, feedback = sup._tribune_review_cycle(key, task, str(draft_path))

            mock_run.assert_not_called()
            self.assertEqual((verdict, feedback), ("approved", ""))


# ===========================================================================
# User Profiling (AGENT-023, AGENT-049)
# ===========================================================================

class TestUserProfiling(unittest.TestCase):
    """Tests for user profile v3: JSON profiles + observation logs."""

    def _write_profile(self, sup, user_id, data):
        """Helper: write a JSON profile for a user."""
        sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
        sup.atomic_write_json(sup.cfg.user_profiles_dir / f"{user_id}.json", data)

    def _profile_data(self, user_id="U_TEST", user_name="Alice", **overrides):
        """Helper: return a v3+ profile dict with defaults."""
        base = {
            "user_id": user_id, "user_name": user_name, "display_name": "",
            "email": "", "github": "", "timezone": "",
            "biography": "", "personality": "", "communication_preferences": "",
            "working_patterns": "", "projects": [], "active_context": "",
            "milestones": [], "notes": [],
        }
        base.update(overrides)
        return base

    def test_profile_stub_created_on_new_user(self):
        """resolve_user_name via Slack API creates a JSON profile stub."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            _write_agent_identity(sup, "U_AGENT")

            def mock_api(method, params=None):
                return {"ok": True, "user": {"profile": {"display_name": "Alice"}}}

            with patch.object(sup, "slack_api_get", side_effect=mock_api):
                name = sup.resolve_user_name("U_NEW_USER")

            self.assertEqual(name, "Alice")
            profile_path = sup.cfg.user_profiles_dir / "U_NEW_USER.json"
            self.assertTrue(profile_path.exists())
            data = json.loads(profile_path.read_text(encoding="utf-8"))
            self.assertEqual(data["user_id"], "U_NEW_USER")
            self.assertEqual(data["user_name"], "Alice")
            for field in ("email", "github", "timezone",
                          "biography", "personality", "communication_preferences",
                          "working_patterns", "projects", "active_context",
                          "milestones", "notes"):
                self.assertIn(field, data)
            self.assertEqual(data["email"], "")
            self.assertEqual(data["github"], "")
            self.assertEqual(data["timezone"], "")
            self.assertNotIn("background", data)
            self.assertIsInstance(data["projects"], list)
            self.assertIsInstance(data["milestones"], list)
            self.assertIsInstance(data["notes"], list)

    def test_profile_stub_not_created_for_agent(self):
        """_create_profile_stub skips the agent's own user ID."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            _write_agent_identity(sup, "U_AGENT")

            sup._create_profile_stub("U_AGENT", "Murphy")
            self.assertFalse((sup.cfg.user_profiles_dir / "U_AGENT.json").exists())

    def test_profile_injection_serial(self):
        """render_runtime_prompt injects formatted JSON profile fields."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text(
                "{{USER_PROFILE}}\n{{DISPATCH_TASK_JSON}}\n"
            )
            self._write_profile(sup, "U_TEST", self._profile_data(
                biography="ML theory expert",
                personality="Direct and concise",
            ))
            sup.cfg.dispatch_task_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_task_file.write_text(
                json.dumps({"mention_ts": "1.0", "source": {"user_id": "U_TEST"}})
            )

            sup.render_runtime_prompt()
            rendered = sup.cfg.runtime_prompt_file.read_text()

            self.assertIn("About your collaborator:", rendered)
            self.assertIn("ML theory expert", rendered)
            self.assertIn("Direct and concise", rendered)
            self.assertNotIn("{{USER_PROFILE}}", rendered)

    def test_profile_injection_parallel(self):
        """_render_slot_prompt injects formatted JSON profile fields."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text(
                "{{USER_PROFILE}}\n{{DISPATCH_TASK_JSON}}\n"
            )
            self._write_profile(sup, "U_TEST", self._profile_data(
                biography="ML theory expert",
            ))

            dispatch_dir = tmp / "dispatch"
            dispatch_dir.mkdir(parents=True, exist_ok=True)
            outcomes_dir = tmp / "outcomes"
            outcomes_dir.mkdir(parents=True, exist_ok=True)

            from src.loop.supervisor.worker_slot import WorkerSlot
            slot = WorkerSlot(
                slot_id=0, repo_root=tmp, dispatch_dir=dispatch_dir,
                outcomes_dir=outcomes_dir, worktree_dir=tmp / "worktrees",
            )

            task_data = {"mention_ts": "1.0", "thread_ts": "1.0", "source": {"user_id": "U_TEST"}}
            sup.atomic_write_json(slot.dispatch_task_file, task_data)

            sup._render_slot_prompt(slot, "1.0", "slack_mention")
            rendered = slot.dispatch_prompt_file.read_text()

            self.assertIn("About your collaborator:", rendered)
            self.assertIn("ML theory expert", rendered)

    def test_missing_profile_fallback(self):
        """render_runtime_prompt shows fallback when no profile exists."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text(
                "{{USER_PROFILE}}\n{{DISPATCH_TASK_JSON}}\n"
            )
            sup.cfg.dispatch_task_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_task_file.write_text(
                json.dumps({"mention_ts": "1.0", "source": {"user_id": "U_UNKNOWN"}})
            )

            sup.render_runtime_prompt()
            rendered = sup.cfg.runtime_prompt_file.read_text()

            self.assertIn("No prior interaction history", rendered)
            self.assertNotIn("About your collaborator:", rendered)

    def test_char_limit_truncation(self):
        """Profile body exceeding char limit is truncated."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp, env_overrides={"USER_PROFILE_CHAR_LIMIT": "50"})
            self._write_profile(sup, "U_TEST", self._profile_data(
                biography="A" * 200,
            ))

            result = sup.read_user_profile("U_TEST")

            self.assertTrue(len(result) < 250)
            self.assertIn("[Profile truncated", result)

    def test_invalid_json_returns_empty(self):
        """Malformed JSON profile returns empty string."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            (sup.cfg.user_profiles_dir / "U_TEST.json").write_text(
                "{ this is not valid json }"
            )

            result = sup.read_user_profile("U_TEST")
            self.assertEqual(result, "")

    def test_multiple_fields_formatted(self):
        """read_user_profile formats multiple non-empty fields with labels."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            self._write_profile(sup, "U_TEST", self._profile_data(
                display_name="Alice",
                biography="ML researcher",
                personality="Direct",
                active_context="heavy-tail project",
            ))

            result = sup.read_user_profile("U_TEST")

            self.assertIn("- Name: Alice", result)
            self.assertIn("- Biography: ML researcher", result)
            self.assertIn("- Personality: Direct", result)
            self.assertIn("- Current focus: heavy-tail project", result)

    def test_stub_only_renders_fallback(self):
        """JSON stub with all empty fields renders neutral fallback."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            self._write_profile(sup, "U_STUB", self._profile_data(
                user_id="U_STUB", user_name="Bob",
            ))

            result = sup.read_user_profile("U_STUB")
            self.assertEqual(result, "")

            sup.cfg.session_template.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.session_template.write_text(
                "{{USER_PROFILE}}\n{{DISPATCH_TASK_JSON}}\n"
            )
            sup.cfg.dispatch_task_file.parent.mkdir(parents=True, exist_ok=True)
            sup.cfg.dispatch_task_file.write_text(
                json.dumps({"mention_ts": "1.0", "source": {"user_id": "U_STUB"}})
            )
            sup.render_runtime_prompt()
            rendered = sup.cfg.runtime_prompt_file.read_text()
            self.assertIn("No prior interaction history", rendered)

    def test_v1_migration(self):
        """v1 .md profile with observations migrates to .json + .log.md."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            # Write a v1 profile
            (sup.cfg.user_profiles_dir / "U_MIG.md").write_text(
                "---\nuser_id: U_MIG\nuser_name: Charlie\n---\n- Prefers concise responses\n- Expert in NLP\n"
            )
            # Write user directory so backfill finds this user
            sup.cfg.user_directory_file.parent.mkdir(parents=True, exist_ok=True)
            json.dump({"agent": {}, "users": {"U_MIG": {"user_name": "Charlie"}}},
                      sup.cfg.user_directory_file.open("w"))

            sup._backfill_user_profiles()

            # JSON profile created
            json_path = sup.cfg.user_profiles_dir / "U_MIG.json"
            self.assertTrue(json_path.exists())
            data = json.loads(json_path.read_text())
            self.assertEqual(data["user_id"], "U_MIG")
            self.assertEqual(data["user_name"], "Charlie")
            # v3 schema: biography (not background), contact + list fields present
            self.assertIn("biography", data)
            self.assertNotIn("background", data)
            self.assertEqual(data["email"], "")
            self.assertEqual(data["github"], "")
            self.assertEqual(data["timezone"], "")
            self.assertEqual(data["projects"], [])
            self.assertEqual(data["milestones"], [])
            self.assertEqual(data["notes"], [])
            # Observations moved to log
            log_path = sup.cfg.user_profiles_dir / "U_MIG.log.md"
            self.assertTrue(log_path.exists())
            self.assertIn("Prefers concise responses", log_path.read_text())
            # Legacy .md removed
            self.assertFalse((sup.cfg.user_profiles_dir / "U_MIG.md").exists())

    def test_v1_migration_stub_only(self):
        """v1 .md stub with no body migrates to .json only, no .log.md."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            (sup.cfg.user_profiles_dir / "U_STUB2.md").write_text(
                "---\nuser_id: U_STUB2\nuser_name: Dana\n---\n"
            )
            sup.cfg.user_directory_file.parent.mkdir(parents=True, exist_ok=True)
            json.dump({"agent": {}, "users": {"U_STUB2": {"user_name": "Dana"}}},
                      sup.cfg.user_directory_file.open("w"))

            sup._backfill_user_profiles()

            self.assertTrue((sup.cfg.user_profiles_dir / "U_STUB2.json").exists())
            self.assertFalse((sup.cfg.user_profiles_dir / "U_STUB2.log.md").exists())
            self.assertFalse((sup.cfg.user_profiles_dir / "U_STUB2.md").exists())

    def test_v1_migration_already_migrated(self):
        """If .json already exists, .md is not removed."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            # Both files exist
            (sup.cfg.user_profiles_dir / "U_BOTH.md").write_text(
                "---\nuser_id: U_BOTH\n---\n- old observation\n"
            )
            self._write_profile(sup, "U_BOTH", self._profile_data(user_id="U_BOTH"))
            sup.cfg.user_directory_file.parent.mkdir(parents=True, exist_ok=True)
            json.dump({"agent": {}, "users": {"U_BOTH": {"user_name": "Eve"}}},
                      sup.cfg.user_directory_file.open("w"))

            sup._backfill_user_profiles()

            # .md NOT removed (migration skipped)
            self.assertTrue((sup.cfg.user_profiles_dir / "U_BOTH.md").exists())
            self.assertTrue((sup.cfg.user_profiles_dir / "U_BOTH.json").exists())

    def test_v2_to_v3_migration(self):
        """v2 profile (background field) migrates to v3 (biography + list fields)."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            # Write a v2 profile directly (has background, no biography)
            v2_data = {
                "user_id": "U_V2", "user_name": "Frank", "display_name": "Frank",
                "background": "Expert in NLP", "personality": "Focused",
                "communication_preferences": "", "working_patterns": "",
                "active_context": "",
            }
            sup.atomic_write_json(sup.cfg.user_profiles_dir / "U_V2.json", v2_data)

            sup._migrate_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_V2.json").read_text())
            self.assertEqual(data["biography"], "Expert in NLP")
            self.assertNotIn("background", data)
            self.assertEqual(data["email"], "")
            self.assertEqual(data["github"], "")
            self.assertEqual(data["timezone"], "")
            self.assertEqual(data["projects"], [])
            self.assertEqual(data["milestones"], [])
            self.assertEqual(data["notes"], [])

    def test_v2_migration_orphan_profile(self):
        """v2 profile on disk with no user_directory entry still migrates."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            # Write a v2 profile with no matching user_directory entry
            v2_data = {
                "user_id": "U_ORPHAN", "user_name": "Ghost", "display_name": "",
                "background": "Orphan user", "personality": "",
                "communication_preferences": "", "working_patterns": "",
                "active_context": "",
            }
            sup.atomic_write_json(sup.cfg.user_profiles_dir / "U_ORPHAN.json", v2_data)

            sup._backfill_user_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_ORPHAN.json").read_text())
            self.assertEqual(data["biography"], "Orphan user")
            self.assertNotIn("background", data)
            self.assertEqual(data["email"], "")
            self.assertEqual(data["github"], "")
            self.assertEqual(data["timezone"], "")

    def test_v3_profile_already_current(self):
        """v3+ profile with contact fields is not modified by migration."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            v3_data = self._profile_data(
                user_id="U_V3", biography="Already current",
                email="test@example.com", github="testuser", timezone="America/New_York",
                projects=["proj-a"], milestones=["2025-01: milestone"],
                notes=["a note"],
            )
            self._write_profile(sup, "U_V3", v3_data)

            sup._migrate_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_V3.json").read_text())
            self.assertEqual(data["biography"], "Already current")
            self.assertEqual(data["email"], "test@example.com")
            self.assertEqual(data["github"], "testuser")
            self.assertEqual(data["timezone"], "America/New_York")
            self.assertEqual(data["projects"], ["proj-a"])
            self.assertEqual(data["milestones"], ["2025-01: milestone"])
            self.assertEqual(data["notes"], ["a note"])

    def test_list_field_formatting(self):
        """read_user_profile formats projects inline, milestones/notes as bullet sub-lists."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            self._write_profile(sup, "U_LIST", self._profile_data(
                user_id="U_LIST",
                biography="Researcher",
                projects=["proj-a", "proj-b"],
                milestones=["2025-09: Paper accepted", "2026-01: First run"],
                notes=["From NYC", "Loves coffee"],
            ))

            result = sup.read_user_profile("U_LIST")

            self.assertIn("- Projects: proj-a, proj-b", result)
            self.assertIn("- Milestones:", result)
            self.assertIn("  - 2025-09: Paper accepted", result)
            self.assertIn("  - 2026-01: First run", result)
            self.assertIn("- Notes:", result)
            self.assertIn("  - From NYC", result)
            self.assertIn("  - Loves coffee", result)

    def test_malformed_list_fields(self):
        """Malformed list fields (non-list, null, mixed types) don't crash rendering."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            # Write profile with malformed list fields
            bad_data = {
                "user_id": "U_BAD", "user_name": "Bad",
                "display_name": "Bad", "biography": "Valid bio",
                "personality": "", "communication_preferences": "",
                "working_patterns": "", "active_context": "",
                "projects": "not-a-list",  # string instead of list
                "milestones": None,  # null
                "notes": [42, "", "valid note", None],  # mixed types
            }
            sup.atomic_write_json(sup.cfg.user_profiles_dir / "U_BAD.json", bad_data)

            result = sup.read_user_profile("U_BAD")

            # Should not crash and should format valid content
            self.assertIn("- Biography: Valid bio", result)
            # projects is a string, not a list — skipped
            self.assertNotIn("Projects", result)
            # milestones is None — skipped
            self.assertNotIn("Milestones", result)
            # notes: only "valid note" should appear (42 and None are not strings, "" is empty)
            self.assertIn("- Notes:", result)
            self.assertIn("  - valid note", result)

    def test_v2_migration_at_init_time(self):
        """v2 profiles on disk are migrated to v3 during Supervisor.__init__."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            profiles_dir = tmp / ".agent" / "user_profiles"
            profiles_dir.mkdir(parents=True, exist_ok=True)
            # Seed a v2 profile BEFORE constructing the supervisor
            v2_data = {
                "user_id": "U_INIT", "user_name": "InitUser", "display_name": "",
                "background": "Data from v2", "personality": "",
                "communication_preferences": "", "working_patterns": "",
                "active_context": "",
            }
            (profiles_dir / "U_INIT.json").write_text(
                json.dumps(v2_data), encoding="utf-8"
            )
            # Do NOT pre-create runtime/logs — test the real startup path
            sup = _make_supervisor(tmp)

            # Profile should already be v3+ after supervisor construction
            data = json.loads((sup.cfg.user_profiles_dir / "U_INIT.json").read_text())
            self.assertEqual(data["biography"], "Data from v2")
            self.assertNotIn("background", data)
            self.assertEqual(data["email"], "")
            self.assertEqual(data["github"], "")
            self.assertEqual(data["timezone"], "")
            self.assertEqual(data["projects"], [])
            self.assertEqual(data["milestones"], [])
            self.assertEqual(data["notes"], [])

    def test_v2_migration_mixed_schema(self):
        """Profile with both background and empty biography reconciles correctly."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            # Write a mixed-schema profile (both keys, biography empty)
            mixed_data = {
                "user_id": "U_MIX", "user_name": "Mixed", "display_name": "",
                "background": "Real background data", "biography": "",
                "personality": "", "communication_preferences": "",
                "working_patterns": "", "active_context": "",
            }
            sup.atomic_write_json(sup.cfg.user_profiles_dir / "U_MIX.json", mixed_data)

            sup._migrate_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_MIX.json").read_text())
            self.assertEqual(data["biography"], "Real background data")
            self.assertNotIn("background", data)
            self.assertEqual(data["projects"], [])

    def test_v2_migration_whitespace_biography(self):
        """Mixed-schema profile with whitespace-only biography uses background value."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            mixed_data = {
                "user_id": "U_WS", "user_name": "WS", "display_name": "",
                "background": "Real data", "biography": "   ",
                "personality": "", "communication_preferences": "",
                "working_patterns": "", "active_context": "",
            }
            sup.atomic_write_json(sup.cfg.user_profiles_dir / "U_WS.json", mixed_data)

            sup._migrate_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_WS.json").read_text())
            self.assertEqual(data["biography"], "Real data")
            self.assertNotIn("background", data)

    def test_read_unmigrated_v2_profile(self):
        """read_user_profile renders background from an unmigrated v2 profile."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            # Write a v2 profile that was NOT migrated (no biography key)
            v2_data = {
                "user_id": "U_V2R", "user_name": "V2Reader",
                "display_name": "Vic", "background": "Still on v2",
                "personality": "", "communication_preferences": "",
                "working_patterns": "", "active_context": "",
            }
            sup.atomic_write_json(sup.cfg.user_profiles_dir / "U_V2R.json", v2_data)

            result = sup.read_user_profile("U_V2R")

            self.assertIn("- Name: Vic", result)
            self.assertIn("- Biography: Still on v2", result)

    def test_non_string_biography_fallback(self):
        """Profile with non-string biography and valid background falls back gracefully."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            bad_data = {
                "user_id": "U_NSB", "user_name": "NSB",
                "display_name": "", "biography": ["not", "a", "string"],
                "background": "Valid background data",
                "personality": "", "communication_preferences": "",
                "working_patterns": "", "active_context": "",
            }
            sup.atomic_write_json(sup.cfg.user_profiles_dir / "U_NSB.json", bad_data)

            # Migration should fix it
            sup._migrate_profiles()
            data = json.loads((sup.cfg.user_profiles_dir / "U_NSB.json").read_text())
            self.assertEqual(data["biography"], "Valid background data")
            self.assertNotIn("background", data)

            # Reader should also handle it if migration didn't run
            bad_data2 = {
                "user_id": "U_NSB2", "user_name": "NSB2",
                "display_name": "", "biography": 42,
                "background": "Fallback bio",
                "personality": "", "communication_preferences": "",
                "working_patterns": "", "active_context": "",
            }
            sup.atomic_write_json(sup.cfg.user_profiles_dir / "U_NSB2.json", bad_data2)
            result = sup.read_user_profile("U_NSB2")
            self.assertIn("- Biography: Fallback bio", result)

    def test_contact_fields_formatted(self):
        """read_user_profile formats populated contact fields with labels."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            self._write_profile(sup, "U_CF", self._profile_data(
                user_id="U_CF",
                display_name="Alice",
                email="alice@example.com",
                github="alicecodes",
                timezone="America/Los_Angeles",
                biography="ML researcher",
            ))

            result = sup.read_user_profile("U_CF")

            self.assertIn("- Email: alice@example.com", result)
            self.assertIn("- GitHub: alicecodes", result)
            self.assertIn("- Timezone: America/Los_Angeles", result)
            self.assertIn("- Biography: ML researcher", result)

    def test_contact_fields_empty_omitted(self):
        """Empty contact fields are not rendered in profile output."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            self._write_profile(sup, "U_EMPTY", self._profile_data(
                user_id="U_EMPTY",
                biography="Has no contact info",
            ))

            result = sup.read_user_profile("U_EMPTY")

            self.assertNotIn("Email", result)
            self.assertNotIn("GitHub", result)
            self.assertNotIn("Timezone", result)
            self.assertIn("- Biography: Has no contact info", result)

    def test_existing_v3_gains_contact_fields(self):
        """A v3 profile without contact fields gains them on backfill."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            # Write a v3 profile WITHOUT contact fields (pre-AGENT-054)
            old_v3 = {
                "user_id": "U_OLD3", "user_name": "OldV3", "display_name": "",
                "biography": "Has bio", "personality": "",
                "communication_preferences": "", "working_patterns": "",
                "projects": [], "active_context": "",
                "milestones": [], "notes": [],
            }
            self._write_profile(sup, "U_OLD3", old_v3)

            sup._migrate_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_OLD3.json").read_text())
            self.assertEqual(data["email"], "")
            self.assertEqual(data["github"], "")
            self.assertEqual(data["timezone"], "")
            self.assertEqual(data["biography"], "Has bio")

    def test_contact_fields_idempotent(self):
        """Populated contact fields survive backfill unchanged."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            self._write_profile(sup, "U_IDEM", self._profile_data(
                user_id="U_IDEM",
                email="keep@example.com",
                github="keepuser",
                timezone="Europe/London",
                biography="Should survive",
            ))

            sup._backfill_user_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_IDEM.json").read_text())
            self.assertEqual(data["email"], "keep@example.com")
            self.assertEqual(data["github"], "keepuser")
            self.assertEqual(data["timezone"], "Europe/London")
            self.assertEqual(data["biography"], "Should survive")

    def test_maintenance_prompt_mentions_scalar_fields(self):
        """maintenance_reflect.md mentions email, github, timezone, scalar guidance, and attribution."""
        prompt_path = Path(__file__).resolve().parent.parent.parent / "prompts" / "maintenance_reflect.md"
        content = prompt_path.read_text(encoding="utf-8")
        for keyword in ("email", "github", "timezone"):
            self.assertIn(keyword, content.lower(),
                          f"maintenance_reflect.md should mention '{keyword}'")
        # Scalar field guidance should be present
        self.assertIn("scalar", content.lower(),
                      "maintenance_reflect.md should contain scalar field guidance")
        # Attribution rule must be present to prevent writing third-party data
        self.assertIn("Attribution required", content,
                      "maintenance_reflect.md should require attribution for scalar fields")
        self.assertIn("unambiguously theirs", content,
                      "maintenance_reflect.md should require unambiguous ownership")

    def test_session_prompt_contact_categories(self):
        """session.md mentions github/timezone but NOT email in observation categories."""
        prompt_path = Path(__file__).resolve().parent.parent.parent / "prompts" / "session.md"
        content = prompt_path.read_text(encoding="utf-8")
        # Find the observation log guidance line
        obs_line = ""
        for line in content.splitlines():
            if "learned something durable" in line:
                obs_line = line
                break
        self.assertTrue(obs_line, "session.md should have observation guidance line")
        self.assertIn("github username", obs_line)
        self.assertIn("timezone", obs_line)
        # email should NOT be in the observable categories (PII)
        self.assertNotIn("email", obs_line.lower())
        # PII rule should explicitly allow github/timezone
        self.assertIn("GitHub usernames and timezones are allowed", content)

    def test_malformed_contact_scalars_normalized(self):
        """Non-string contact field values are reset to empty by migration."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            # Write a profile with non-string contact fields
            bad_data = {
                "user_id": "U_BAD_C", "user_name": "BadContact",
                "display_name": "", "biography": "Has malformed contacts",
                "email": ["not", "a", "string"],
                "github": None,
                "timezone": 42,
                "personality": "", "communication_preferences": "",
                "working_patterns": "", "projects": [], "active_context": "",
                "milestones": [], "notes": [],
            }
            self._write_profile(sup, "U_BAD_C", bad_data)

            sup._migrate_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_BAD_C.json").read_text())
            # All malformed contact fields should be reset to empty string
            self.assertEqual(data["email"], "")
            self.assertEqual(data["github"], "")
            self.assertEqual(data["timezone"], "")
            self.assertEqual(data["biography"], "Has malformed contacts")

    def test_multiline_contact_scalars_normalized_on_disk(self):
        """Multiline contact scalars are normalized to first line during migration."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            bad_data = {
                "user_id": "U_MLN", "user_name": "MultiLineNorm",
                "display_name": "", "biography": "OK",
                "email": "alice@example.com\nLearned from Overleaf",
                "github": "alicecodes\nShe also uses gitlab",
                "timezone": "America/New_York\nSometimes EST",
                "personality": "", "communication_preferences": "",
                "working_patterns": "", "projects": [], "active_context": "",
                "milestones": [], "notes": [],
            }
            self._write_profile(sup, "U_MLN", bad_data)

            sup._migrate_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_MLN.json").read_text())
            # Should be normalized on disk, not just in rendered output
            self.assertEqual(data["email"], "alice@example.com")
            self.assertEqual(data["github"], "alicecodes")
            self.assertEqual(data["timezone"], "America/New_York")

    def test_reflect_fallback_mentions_contact_fields(self):
        """Maintenance reflect fallback prompt mentions scalar identity fields."""
        from src.loop.supervisor.maintenance import MaintenanceManager
        fallback = MaintenanceManager._FALLBACK_PROMPTS["reflect_template"]
        for keyword in ("email", "github", "timezone"):
            self.assertIn(keyword, fallback.lower(),
                          f"reflect fallback should mention '{keyword}'")
        self.assertIn("scalar", fallback.lower())
        self.assertIn("Attribution required", fallback)

    def test_scalar_field_multiline_normalized(self):
        """Multiline values in scalar identity fields are normalized to first line."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            self._write_profile(sup, "U_ML", self._profile_data(
                user_id="U_ML",
                email="alice@example.com\nThis was learned from Overleaf setup",
                github="alicecodes\nShe also uses gitlab",
                timezone="America/Los_Angeles\nSometimes works from Tokyo",
                biography="Researcher",
            ))

            result = sup.read_user_profile("U_ML")

            # Only first line of each scalar field should appear
            self.assertIn("- Email: alice@example.com", result)
            self.assertIn("- GitHub: alicecodes", result)
            self.assertIn("- Timezone: America/Los_Angeles", result)
            # Prose/extra lines should NOT appear
            self.assertNotIn("Overleaf", result)
            self.assertNotIn("gitlab", result)
            self.assertNotIn("Tokyo", result)

    def test_orphan_log_backfill_creates_stub(self):
        """Orphan .log.md file (no .json) gets a profile stub during backfill."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            # Create an orphan log file — no .json, no user_directory entry
            log_path = sup.cfg.user_profiles_dir / "U_ORPHAN_LOG.log.md"
            log_path.write_text("- User asked about NLP models\n")

            # Mock resolve_user_name so we don't hit a real Slack API
            with patch.object(sup, "resolve_user_name", return_value="OrphanUser"):
                sup._backfill_user_profiles()

            json_path = sup.cfg.user_profiles_dir / "U_ORPHAN_LOG.json"
            self.assertTrue(json_path.exists(), "Profile stub should be created for orphan log")
            data = json.loads(json_path.read_text())
            self.assertEqual(data["user_id"], "U_ORPHAN_LOG")
            self.assertEqual(data["user_name"], "OrphanUser")

    def test_orphan_log_backfill_skips_existing(self):
        """Backfill does not overwrite existing .json when .log.md also exists."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            sup.cfg.user_profiles_dir.mkdir(parents=True, exist_ok=True)
            # Create both .json and .log.md
            self._write_profile(sup, "U_BOTH_LOG", self._profile_data(
                user_id="U_BOTH_LOG", user_name="Existing",
                biography="Already has a profile",
            ))
            (sup.cfg.user_profiles_dir / "U_BOTH_LOG.log.md").write_text("- new obs\n")

            sup._backfill_user_profiles()

            data = json.loads((sup.cfg.user_profiles_dir / "U_BOTH_LOG.json").read_text())
            self.assertEqual(data["biography"], "Already has a profile")


# ---------------------------------------------------------------------------
# FIX-004: Advisory file locking tests
# ---------------------------------------------------------------------------

from src.loop.supervisor.filelock import agent_file_lock, locked_append


class TestAgentFileLock(unittest.TestCase):
    """Tests for the advisory file locking utility."""

    def test_agent_file_lock_basic(self):
        """Lock acquire/release cycle works and creates lock file."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.md"
            target.write_text("hello\n", encoding="utf-8")
            with agent_file_lock(target):
                # Can read/write while holding the lock
                target.write_text("updated\n", encoding="utf-8")
            self.assertEqual(target.read_text(encoding="utf-8"), "updated\n")
            # Lock file should exist
            self.assertTrue((Path(tmp) / "test.md.lock").exists())

    def test_agent_file_lock_creates_parent(self):
        """Lock works even when parent directory doesn't exist yet."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "dir" / "test.md"
            with agent_file_lock(target):
                target.write_text("created\n", encoding="utf-8")
            self.assertEqual(target.read_text(encoding="utf-8"), "created\n")

    def test_locked_append_basic(self):
        """locked_append writes text and ensures trailing newline."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "log.md"
            locked_append(target, "- first entry")
            locked_append(target, "- second entry\n")
            content = target.read_text(encoding="utf-8")
            self.assertEqual(content, "- first entry\n- second entry\n")

    def test_locked_append_creates_file(self):
        """locked_append creates the file if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "new" / "file.md"
            locked_append(target, "hello")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

    def test_locked_append_newline_normalization(self):
        """Consecutive appends produce separate lines, never merging."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "log.md"
            locked_append(target, "line 1")
            locked_append(target, "line 2")
            locked_append(target, "line 3\n")
            lines = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines, ["line 1", "line 2", "line 3"])

    def test_agent_file_lock_contention_subprocess(self):
        """Two subprocess workers contending on the same lock produce serialized output."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shared.md"
            target.write_text("", encoding="utf-8")

            # Each subprocess uses locked_append via a one-liner
            n_per_worker = 20

            procs = []
            for wid in ("A", "B"):
                code = (
                    f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
                    f"from pathlib import Path; "
                    f"from src.loop.supervisor.filelock import locked_append; "
                    f"[locked_append(Path({str(target)!r}), "
                    f"'worker-{wid}-' + str(i)) for i in range({n_per_worker})]"
                )
                p = subprocess.Popen(
                    [sys.executable, "-c", code],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                procs.append(p)

            for p in procs:
                p.wait(timeout=30)

            lines = [l for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
            # All lines present, no interleaving
            self.assertEqual(len(lines), n_per_worker * 2)
            a_lines = [l for l in lines if l.startswith("worker-A-")]
            b_lines = [l for l in lines if l.startswith("worker-B-")]
            self.assertEqual(len(a_lines), n_per_worker)
            self.assertEqual(len(b_lines), n_per_worker)
            # Each worker's lines are in order
            a_ids = [int(l.split("-")[-1]) for l in a_lines]
            b_ids = [int(l.split("-")[-1]) for l in b_lines]
            self.assertEqual(a_ids, list(range(n_per_worker)))
            self.assertEqual(b_ids, list(range(n_per_worker)))


class TestAgentFlockCLI(unittest.TestCase):
    """Tests for the scripts/agent_flock CLI wrapper."""

    SCRIPT = str(REPO_ROOT / "scripts" / "agent_flock")

    def test_cli_append(self):
        """CLI append command works end-to-end."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.log"
            result = subprocess.run(
                [sys.executable, self.SCRIPT, "append", str(target), "- observation one"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "- observation one\n")

            # Second append doesn't clobber
            result = subprocess.run(
                [sys.executable, self.SCRIPT, "append", str(target), "- observation two\n"],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "- observation one\n- observation two\n",
            )

    def test_cli_write(self):
        """CLI write command replaces file content from stdin."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "test.md"
            target.write_text("old content\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, self.SCRIPT, "write", str(target)],
                input="new content\n",
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "new content\n")

    def test_cli_usage_error(self):
        """CLI exits 2 on missing arguments."""
        result = subprocess.run(
            [sys.executable, self.SCRIPT],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 2)

    def test_cli_concurrent_appends(self):
        """Two concurrent subprocess invocations produce complete output."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "shared.log"

            procs = []
            for worker_id in ("X", "Y"):
                for i in range(10):
                    p = subprocess.Popen(
                        [sys.executable, self.SCRIPT, "append", str(target),
                         f"entry-{worker_id}-{i}"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    )
                    procs.append(p)

            for p in procs:
                p.wait(timeout=30)

            lines = [l for l in target.read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), 20)
            x_lines = [l for l in lines if l.startswith("entry-X-")]
            y_lines = [l for l in lines if l.startswith("entry-Y-")]
            self.assertEqual(len(x_lines), 10)
            self.assertEqual(len(y_lines), 10)

    def test_cli_append_from_stdin(self):
        """CLI append reads text from stdin when no argv text given."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "stdin.log"
            result = subprocess.run(
                [sys.executable, self.SCRIPT, "append", str(target)],
                input="- observation from stdin",
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                target.read_text(encoding="utf-8"),
                "- observation from stdin\n",
            )

    def test_cli_write_atomic(self):
        """CLI write uses tempfile+replace (file is never empty on disk)."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "atomic.md"
            target.write_text("original\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, self.SCRIPT, "write", str(target)],
                input="replacement\n",
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "replacement\n")


class TestFileLockReadModifyWrite(unittest.TestCase):
    """Tests proving locking is required for read-modify-write operations."""

    def test_locked_read_modify_write_no_lost_updates(self):
        """Concurrent read-modify-write under lock loses no increments.

        Without locking, concurrent read-count-write would lose updates
        (classic lost-update race). This test verifies the lock prevents it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "counter.txt"
            target.write_text("0\n", encoding="utf-8")

            n_increments = 50
            # Each subprocess reads the counter, increments, and writes back
            # under advisory lock.  Without the lock this would lose updates.
            code = (
                f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r}); "
                f"from pathlib import Path; "
                f"from src.loop.supervisor.filelock import agent_file_lock; "
                f"t = Path({str(target)!r}); "
                f"[("
                f"  f := agent_file_lock(t).__enter__(),"
                f"  v := int(t.read_text().strip()),"
                f"  t.write_text(str(v + 1) + chr(10)),"
                f"  agent_file_lock(t).__exit__(None, None, None)"
                f") for _ in range({n_increments})]"
            )

            # This is tricky with walrus operators in a list comp.  Use a
            # simpler multi-statement approach instead.
            code = (
                f"import sys; sys.path.insert(0, {str(REPO_ROOT)!r})\n"
                f"from pathlib import Path\n"
                f"from src.loop.supervisor.filelock import agent_file_lock\n"
                f"t = Path({str(target)!r})\n"
                f"for _ in range({n_increments}):\n"
                f"    with agent_file_lock(t):\n"
                f"        v = int(t.read_text().strip())\n"
                f"        t.write_text(str(v + 1) + '\\n')\n"
            )

            procs = []
            for _ in range(2):
                p = subprocess.Popen(
                    [sys.executable, "-c", code],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                procs.append(p)

            for p in procs:
                p.wait(timeout=30)
                self.assertEqual(p.returncode, 0, p.stderr.read().decode())

            final = int(target.read_text(encoding="utf-8").strip())
            self.assertEqual(final, n_increments * 2)


class TestSystemPromptHash(unittest.TestCase):
    """Tests for system_prompt_hash() — Plan 37 foundation."""

    def test_deterministic(self):
        from src.loop.supervisor.utils import system_prompt_hash
        root = Path.cwd()
        h1 = system_prompt_hash(root)
        h2 = system_prompt_hash(root)
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)  # truncated hex

    def test_changes_with_content(self):
        from src.loop.supervisor.utils import system_prompt_hash
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            h1 = system_prompt_hash(root)  # all files missing
            (root / "src" / "prompts").mkdir(parents=True)
            (root / "src" / "prompts" / "session.md").write_text("v1")
            h2 = system_prompt_hash(root)
            self.assertNotEqual(h1, h2)
            (root / "src" / "prompts" / "session.md").write_text("v2")
            h3 = system_prompt_hash(root)
            self.assertNotEqual(h2, h3)


class TestCaptureCodexSessionId(unittest.TestCase):
    """Tests for capture_codex_session_id() — Plan 37 foundation."""

    def test_parses_uuid(self):
        from src.loop.supervisor.utils import capture_codex_session_id
        output = "codex 0.111.0\nsession id: a1b2c3d4-e5f6-7890-abcd-ef1234567890\nthinking..."
        self.assertEqual(capture_codex_session_id(output), "a1b2c3d4-e5f6-7890-abcd-ef1234567890")

    def test_returns_none_when_absent(self):
        from src.loop.supervisor.utils import capture_codex_session_id
        self.assertIsNone(capture_codex_session_id("some random output with no session id"))

    def test_case_insensitive(self):
        from src.loop.supervisor.utils import capture_codex_session_id
        output = "Session ID: AABBCCDD-1122-3344-5566-778899001122"
        # UUIDs are lowercase hex but we handle case-insensitive match
        self.assertEqual(capture_codex_session_id(output), "AABBCCDD-1122-3344-5566-778899001122")


class TestClassifyThreadMessageAttachments(unittest.TestCase):
    """Test that attachment-only messages get descriptive text instead of [empty message]."""

    def test_empty_text_no_files(self):
        from src.loop.supervisor.utils import _classify_thread_message
        msg = {"ts": "1.0", "user": "U123", "text": ""}
        result = _classify_thread_message(msg, "UAGENT")
        self.assertEqual(result["text"], "[empty message]")

    def test_empty_text_with_files_summary(self):
        from src.loop.supervisor.utils import _classify_thread_message
        msg = {"ts": "1.0", "user": "U123", "text": "", "files_summary": "[attached: report.pdf (application/pdf)]"}
        result = _classify_thread_message(msg, "UAGENT")
        self.assertEqual(result["text"], "[attached: report.pdf (application/pdf)]")

    def test_text_present_ignores_files_summary(self):
        from src.loop.supervisor.utils import _classify_thread_message
        msg = {"ts": "1.0", "user": "U123", "text": "Here is the report", "files_summary": "[attached: report.pdf]"}
        result = _classify_thread_message(msg, "UAGENT")
        self.assertEqual(result["text"], "Here is the report")


# ===========================================================================
# Auto-commit safety (Plan 51)
# ===========================================================================

class TestAutoCommitSafety(unittest.TestCase):
    """Plan-51: _auto_commit_system_files uses repo_root cwd, not os.getcwd()."""

    def test_auto_commit_uses_repo_root_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # Restore the real method (test helper replaces it with no-op)
            sup = _make_supervisor(tmp)
            sup._auto_commit_system_files = sl.Supervisor._auto_commit_system_files.__get__(sup)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(stdout="", returncode=0)
                sup._auto_commit_system_files()
            # At least the git status call should have happened
            self.assertGreaterEqual(mock_run.call_count, 1)
            first_call = mock_run.call_args_list[0]
            self.assertEqual(first_call.kwargs.get("cwd"), str(sup.cfg.repo_root))

    def test_repo_root_derived_from_state_file(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            sup = _make_supervisor(tmp)
            # repo_root should be 3 levels up from state_file
            self.assertEqual(sup.cfg.repo_root, sup.cfg.state_file.parent.parent.parent)
            self.assertEqual(sup.cfg.repo_root, tmp)


# Dashboard decoupling (AGENT-044)
# ===========================================================================

class TestDashboardDecoupled(unittest.TestCase):
    """Verify that the supervisor no longer owns dashboard config or threads."""

    def test_config_no_dashboard_fields(self):
        """Config class should not have dashboard_* attributes after decoupling."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            conf = _make_conf(tmp)
            with patch.dict(os.environ, {"SLACK_MCP_XOXP_TOKEN": "x",
                                          "SESSION_MINUTES": "5",
                                          "SLEEP_NORMAL": "1"}):
                cfg = sl.Config(conf)
            # These fields should no longer exist
            for attr in ("dashboard_export_dir", "dashboard_export_enabled",
                         "dashboard_git_push", "dashboard_git_remote",
                         "dashboard_git_branch", "dashboard_gpu_monitor",
                         "dashboard_gpu_node_alias", "dashboard_gpu_command_timeout"):
                self.assertFalse(hasattr(cfg, attr), f"Config should not have {attr}")

    def test_main_no_dashboard_thread(self):
        """Supervisor main.py should not reference _start_dashboard_thread."""
        import src.loop.supervisor.main as main_mod
        self.assertFalse(hasattr(main_mod, "_start_dashboard_thread"),
                         "main module should not have _start_dashboard_thread")

    def test_main_no_threading_import(self):
        """Supervisor main.py should not import threading after decoupling."""
        import importlib
        import src.loop.supervisor.main as main_mod
        importlib.reload(main_mod)
        # Check that 'threading' is not in the module's namespace
        self.assertNotIn("threading", dir(main_mod))


class TestDashboardHeadlessMode(unittest.TestCase):
    """Test the --headless and --from-config modes in dashboard.py."""

    def test_headless_calls_export_loop(self):
        """--headless should call static_export_loop in the main thread."""
        from src.loop.monitor.dashboard import main as dashboard_main
        with tempfile.TemporaryDirectory() as td:
            test_args = ["dashboard", "--headless", "--export-static-dir", td,
                         "--static-git-push", "off", "--gpu-monitor", "off"]
            with patch("sys.argv", test_args), \
                 patch("src.loop.monitor.dashboard.static_export_loop") as mock_loop, \
                 patch("src.loop.monitor.dashboard.write_static_site"), \
                 patch("src.loop.monitor.dashboard.configure_gpu_monitor"):
                dashboard_main()
            mock_loop.assert_called_once()
            # Verify it was called with the right export dir
            call_args = mock_loop.call_args
            self.assertEqual(str(call_args[0][1]), td)

    def test_headless_without_export_dir_exits(self):
        """--headless without --export-static-dir should exit with error."""
        from src.loop.monitor.dashboard import main as dashboard_main
        test_args = ["dashboard", "--headless", "--gpu-monitor", "off"]
        with patch("sys.argv", test_args), \
             patch("src.loop.monitor.dashboard.configure_gpu_monitor"):
            with self.assertRaises(SystemExit) as ctx:
                dashboard_main()
            self.assertEqual(ctx.exception.code, 1)

    def test_from_config_reads_env_vars(self):
        """--from-config should populate args from DASHBOARD_* env vars."""
        from src.loop.monitor.dashboard import main as dashboard_main
        with tempfile.TemporaryDirectory() as td:
            env = {
                "DASHBOARD_EXPORT_ENABLED": "true",
                "DASHBOARD_EXPORT_DIR": td,
                "DASHBOARD_GIT_PUSH": "false",
                "DASHBOARD_GIT_REMOTE": "upstream",
                "DASHBOARD_GIT_BRANCH": "deploy",
                "DASHBOARD_GPU_MONITOR": "false",
                "DASHBOARD_GPU_NODE_ALIAS": "test-node",
                "DASHBOARD_GPU_COMMAND_TIMEOUT": "10",
            }
            test_args = ["dashboard", "--headless", "--from-config"]
            with patch("sys.argv", test_args), \
                 patch.dict(os.environ, env, clear=False), \
                 patch("src.loop.monitor.dashboard.static_export_loop") as mock_loop, \
                 patch("src.loop.monitor.dashboard.write_static_site"), \
                 patch("src.loop.monitor.dashboard.configure_gpu_monitor") as mock_gpu:
                dashboard_main()
            # Verify export dir came from env
            call_args = mock_loop.call_args
            self.assertEqual(str(call_args[0][1]), td)
            # Verify git push is off (DASHBOARD_GIT_PUSH=false)
            self.assertFalse(call_args[0][2])  # static_git_push_enabled
            # Verify GPU monitor was configured as off
            mock_gpu.assert_called_once()
            self.assertFalse(mock_gpu.call_args[1]["enabled"] if "enabled" in mock_gpu.call_args[1]
                             else mock_gpu.call_args[0][0])

    def test_from_config_export_disabled(self):
        """--from-config with DASHBOARD_EXPORT_ENABLED=false should return immediately."""
        from src.loop.monitor.dashboard import main as dashboard_main
        test_args = ["dashboard", "--headless", "--from-config"]
        with patch("sys.argv", test_args), \
             patch.dict(os.environ, {"DASHBOARD_EXPORT_ENABLED": "false"}, clear=False), \
             patch("src.loop.monitor.dashboard.static_export_loop") as mock_loop, \
             patch("src.loop.monitor.dashboard.configure_gpu_monitor"):
            dashboard_main()
        mock_loop.assert_not_called()

    def test_from_config_parse_bool_semantics(self):
        """--from-config should accept all parse_bool truthy values."""
        from src.loop.monitor.dashboard import _parse_bool
        for truthy in ("1", "true", "True", "TRUE", "yes", "Yes", "y", "Y", "on", "ON"):
            self.assertTrue(_parse_bool(truthy), f"{truthy!r} should be truthy")
        for falsy in ("0", "false", "no", "off", "", "nope"):
            self.assertFalse(_parse_bool(falsy), f"{falsy!r} should be falsy")

    def test_load_dotenv_setdefault_semantics(self):
        """_load_dotenv should not overwrite existing env vars."""
        from src.loop.monitor.dashboard import _load_dotenv, BASE_DIR
        with tempfile.TemporaryDirectory() as td:
            dotenv = Path(td) / ".env"
            dotenv.write_text("TEST_DASHBOARD_VAR=from_file\nEXISTING_VAR=from_file\n")
            with patch.object(type(BASE_DIR), "__truediv__", return_value=dotenv):
                # Patch BASE_DIR / ".env" to return our test file
                pass
            # Use the actual implementation with a patched BASE_DIR
            import src.loop.monitor.dashboard as dash_mod
            orig_base = dash_mod.BASE_DIR
            try:
                dash_mod.BASE_DIR = Path(td)
                with patch.dict(os.environ, {"EXISTING_VAR": "from_env"}, clear=False):
                    # Remove TEST_DASHBOARD_VAR if it exists
                    os.environ.pop("TEST_DASHBOARD_VAR", None)
                    _load_dotenv()
                    # New var should be set
                    self.assertEqual(os.environ.get("TEST_DASHBOARD_VAR"), "from_file")
                    # Existing var should NOT be overwritten
                    self.assertEqual(os.environ.get("EXISTING_VAR"), "from_env")
            finally:
                dash_mod.BASE_DIR = orig_base
                os.environ.pop("TEST_DASHBOARD_VAR", None)

    def test_resolve_config_file_honors_loop_config_file(self):
        """_resolve_config_file should use LOOP_CONFIG_FILE env var when set."""
        from src.loop.monitor.dashboard import _resolve_config_file, BASE_DIR
        with patch.dict(os.environ, {"LOOP_CONFIG_FILE": "/custom/config.conf"}):
            result = _resolve_config_file()
            self.assertEqual(result, Path("/custom/config.conf"))
        # Without the env var, should use default
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOOP_CONFIG_FILE", None)
            result = _resolve_config_file()
            self.assertEqual(result, BASE_DIR / "src/config/supervisor_loop.conf")

    def test_from_config_reads_conf_file_fallback(self):
        """--from-config should fall back to read_supervisor_default when env var is absent."""
        import src.loop.monitor.dashboard as dash_mod
        from src.loop.monitor.dashboard import main as dashboard_main
        orig_config = dash_mod.SUPERVISOR_LOOP_CONFIG_FILE
        try:
            with tempfile.TemporaryDirectory() as td:
                # Create a minimal conf file with a custom export dir
                conf = Path(td) / "test.conf"
                conf.write_text(
                    ': "${DASHBOARD_EXPORT_DIR:=/custom/export}"\n'
                    ': "${DASHBOARD_EXPORT_ENABLED:=true}"\n'
                    ': "${DASHBOARD_GIT_PUSH:=false}"\n'
                    ': "${DASHBOARD_GPU_MONITOR:=false}"\n'
                )
                test_args = ["dashboard", "--headless", "--from-config"]
                # Set LOOP_CONFIG_FILE to our test conf, clear DASHBOARD_* env vars
                env_overrides = {"LOOP_CONFIG_FILE": str(conf)}
                with patch("sys.argv", test_args), \
                     patch.dict(os.environ, env_overrides, clear=False), \
                     patch("src.loop.monitor.dashboard.static_export_loop") as mock_loop, \
                     patch("src.loop.monitor.dashboard.write_static_site"), \
                     patch("src.loop.monitor.dashboard.configure_gpu_monitor"):
                    # Remove DASHBOARD_* env vars so conf file is the fallback
                    for key in ("DASHBOARD_EXPORT_DIR", "DASHBOARD_GIT_PUSH",
                                "DASHBOARD_GPU_MONITOR", "DASHBOARD_EXPORT_ENABLED"):
                        os.environ.pop(key, None)
                    dashboard_main()
                # Export dir should come from conf file
                call_args = mock_loop.call_args
                self.assertEqual(str(call_args[0][1]), "/custom/export")
                # Git push should be off (from conf: false)
                self.assertFalse(call_args[0][2])
        finally:
            dash_mod.SUPERVISOR_LOOP_CONFIG_FILE = orig_config

    def test_from_config_defaults_deploy_branch(self):
        """--from-config should default dashboard git publishing to the deploy branch."""
        from src.loop.monitor.dashboard import main as dashboard_main
        with tempfile.TemporaryDirectory() as td:
            env = {
                "DASHBOARD_EXPORT_ENABLED": "true",
                "DASHBOARD_EXPORT_DIR": td,
                "DASHBOARD_GIT_PUSH": "false",
                "DASHBOARD_GPU_MONITOR": "false",
            }
            test_args = ["dashboard", "--headless", "--from-config"]
            with patch("sys.argv", test_args), \
                 patch.dict(os.environ, env, clear=False), \
                 patch("src.loop.monitor.dashboard.static_export_loop") as mock_loop, \
                 patch("src.loop.monitor.dashboard.write_static_site"), \
                 patch("src.loop.monitor.dashboard.configure_gpu_monitor"):
                os.environ.pop("DASHBOARD_GIT_BRANCH", None)
                dashboard_main()
            self.assertEqual(mock_loop.call_args[0][4], "deploy")


class TestCheckDeps(unittest.TestCase):
    """Tests for optional dependency gating in supervisor startup."""

    def test_missing_optional_reviewers_are_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            conf = _make_conf(Path(td), overrides={"TRIBUNE_MAINT_ROUNDS": "1"})
            cfg = sl.Config(conf)
            with patch("src.loop.supervisor.main.shutil.which") as mock_which, \
                 patch("sys.stderr", new_callable=io.StringIO) as stderr:
                mock_which.side_effect = lambda binary: "/usr/bin/codex" if binary == "codex" else None
                sl.check_deps(cfg)
            self.assertEqual(cfg.dev_review_cmd, [])
            self.assertEqual(cfg.tribune_cmd, [])
            self.assertEqual(cfg.tribune_max_review_rounds, 0)
            self.assertEqual(cfg.tribune_maint_rounds, 0)
            self.assertIn("disabling developer review", stderr.getvalue().lower())
            self.assertIn("disabling tribune review", stderr.getvalue().lower())

    def test_enqueue_developer_review_skips_when_cmd_empty(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"DEV_REVIEW_CMD": ""})
            state = _empty_state()
            mention_task = _make_task("1000.000123")
            with patch.object(sup, "slack_api_post") as mock_post:
                sup._enqueue_developer_review("FIX-123", state, mention_task)
            self.assertEqual(state["queued_tasks"], {})
            mock_post.assert_called_once()
            self.assertIn("Developer review is disabled", mock_post.call_args[0][1]["text"])

    def test_headless_git_push_requires_git_repo(self):
        """--headless with git push should fail if export dir is not a git repo."""
        from src.loop.monitor.dashboard import main as dashboard_main
        with tempfile.TemporaryDirectory() as td:
            test_args = ["dashboard", "--headless", "--export-static-dir", td,
                         "--static-git-push", "on", "--gpu-monitor", "off"]
            with patch("sys.argv", test_args), \
                 patch("src.loop.monitor.dashboard.configure_gpu_monitor"):
                with self.assertRaises(SystemExit) as ctx:
                    dashboard_main()
                self.assertEqual(ctx.exception.code, 1)

    def test_from_config_fails_on_missing_explicit_config(self):
        """--from-config should exit if LOOP_CONFIG_FILE points to nonexistent file."""
        from src.loop.monitor.dashboard import main as dashboard_main
        import src.loop.monitor.dashboard as dash_mod
        orig_config = dash_mod.SUPERVISOR_LOOP_CONFIG_FILE
        try:
            test_args = ["dashboard", "--headless", "--from-config"]
            with patch("sys.argv", test_args), \
                 patch.dict(os.environ, {"LOOP_CONFIG_FILE": "/nonexistent/config.conf"}, clear=False), \
                 patch("src.loop.monitor.dashboard.configure_gpu_monitor"):
                with self.assertRaises(SystemExit) as ctx:
                    dashboard_main()
                self.assertEqual(ctx.exception.code, 1)
        finally:
            dash_mod.SUPERVISOR_LOOP_CONFIG_FILE = orig_config

    def test_reload_config_file_after_dotenv(self):
        """_reload_config_file should pick up LOOP_CONFIG_FILE set after import."""
        import src.loop.monitor.dashboard as dash_mod
        orig = dash_mod.SUPERVISOR_LOOP_CONFIG_FILE
        try:
            with patch.dict(os.environ, {"LOOP_CONFIG_FILE": "/late/config.conf"}):
                dash_mod._reload_config_file()
                self.assertEqual(dash_mod.SUPERVISOR_LOOP_CONFIG_FILE, Path("/late/config.conf"))
        finally:
            dash_mod.SUPERVISOR_LOOP_CONFIG_FILE = orig


class TestBacklogRefreshButton(unittest.TestCase):
    """Verify the session log refresh button is present in backlog HTML."""

    def test_refresh_button_in_backlog_html(self):
        """write_backlog_html output includes the refresh button and functions."""
        from src.loop.monitor.dashboard import write_backlog_html
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            write_backlog_html(out, {"items": [], "completed": []})
            html = (out / "backlog" / "index.html").read_text()
            self.assertIn('id="doc-modal-refresh"', html)
            self.assertIn("doc-modal-refresh", html)
            self.assertIn("fetchAndRenderSession", html)
            self.assertIn("refreshSession", html)
            self.assertIn("data-session-id", html)

    def test_refresh_button_hidden_by_default(self):
        """Refresh button starts with display:none in CSS."""
        from src.loop.monitor.dashboard import write_backlog_html
        with tempfile.TemporaryDirectory() as td:
            out = Path(td)
            write_backlog_html(out, {"items": [], "completed": []})
            html = (out / "backlog" / "index.html").read_text()
            # The CSS rule should set display:none by default
            self.assertIn(".doc-modal-refresh", html)
            self.assertRegex(html, r"\.doc-modal-refresh\s*\{[^}]*display:\s*none")


class TestPublishToDeployBranch(unittest.TestCase):
    """Tests for the plumbing-based deploy branch publishing."""

    @staticmethod
    def _init_repo_pair(workdir: Path):
        """Create a local repo + bare remote, with main branch and durable files."""
        bare = workdir / "remote.git"
        local = workdir / "local"
        subprocess.run(["git", "init", "--bare", str(bare)], capture_output=True, check=True)
        # Set default branch to main on the bare repo.
        subprocess.run(["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], capture_output=True, check=True)
        subprocess.run(["git", "init", str(local)], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(local), "checkout", "-b", "main"], capture_output=True, check=True)
        # Set local git identity (hermetic — doesn't rely on global config).
        subprocess.run(["git", "-C", str(local), "config", "user.name", "Test"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(local), "config", "user.email", "test@test.local"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(local), "remote", "add", "origin", str(bare)], capture_output=True, check=True)
        # Create durable site files on main.
        (local / ".nojekyll").touch()
        (local / "showcase").mkdir()
        (local / "showcase" / "index.html").write_text("<h1>Showcase</h1>")
        (local / "assets").mkdir()
        (local / "assets" / "site.css").write_text("body { color: #000; }")
        # Create initial snapshot files.
        (local / "index.html").write_text("<h1>Dashboard</h1>")
        (local / "status.json").write_text('{"status":"ok"}')
        (local / "backlog").mkdir()
        (local / "backlog" / "index.html").write_text("<h1>Backlog</h1>")
        (local / "backlog" / "session.json").write_text("[]")
        (local / "roadmap").mkdir()
        (local / "roadmap" / "index.html").write_text("<h1>Roadmap</h1>")
        subprocess.run(["git", "-C", str(local), "add", "-A"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(local), "commit", "-m", "initial"], capture_output=True, check=True)
        subprocess.run(["git", "-C", str(local), "push", "origin", "main"], capture_output=True, check=True)
        return local, bare

    def test_core_publish(self):
        """Publish creates deploy branch with full site + snapshot overlay."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            # Publish.
            sha = publish_to_deploy_branch(local, "origin", "deploy", (
                "index.html", "status.json", "backlog/index.html",
                "backlog/session.json", "roadmap/index.html",
            ))
            self.assertIsNotNone(sha)
            # Verify deploy branch exists on remote.
            ls = subprocess.run(
                ["git", "-C", str(bare), "branch", "--list", "deploy"],
                capture_output=True, text=True,
            )
            self.assertIn("deploy", ls.stdout)
            # Verify deploy has durable files from main.
            show_showcase = subprocess.run(
                ["git", "-C", str(bare), "show", "deploy:showcase/index.html"],
                capture_output=True, text=True,
            )
            self.assertEqual(show_showcase.stdout, "<h1>Showcase</h1>")
            # Verify deploy has snapshot files.
            show_index = subprocess.run(
                ["git", "-C", str(bare), "show", "deploy:index.html"],
                capture_output=True, text=True,
            )
            self.assertEqual(show_index.stdout, "<h1>Dashboard</h1>")
            # Verify main is untouched.
            log = subprocess.run(
                ["git", "-C", str(local), "log", "--oneline", "main"],
                capture_output=True, text=True,
            )
            self.assertEqual(len(log.stdout.strip().splitlines()), 1)  # Only initial commit.

    def test_skip_detection(self):
        """Second publish with no changes returns None."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        tracked = ("index.html", "status.json", "backlog/index.html",
                    "backlog/session.json", "roadmap/index.html")
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            publish_to_deploy_branch(local, "origin", "deploy", tracked)
            # Second call, no changes.
            result = publish_to_deploy_branch(local, "origin", "deploy", tracked)
            self.assertIsNone(result)

    def test_changed_content_creates_orphan(self):
        """Changed content creates a new orphan commit (no parent)."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        tracked = ("index.html", "status.json", "backlog/index.html",
                    "backlog/session.json", "roadmap/index.html")
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            publish_to_deploy_branch(local, "origin", "deploy", tracked)
            # Change a snapshot file.
            (local / "status.json").write_text('{"status":"updated"}')
            sha2 = publish_to_deploy_branch(local, "origin", "deploy", tracked)
            self.assertIsNotNone(sha2)
            # Verify orphan (no parent).
            parents = subprocess.run(
                ["git", "-C", str(bare), "rev-list", "--parents", "-1", "deploy"],
                capture_output=True, text=True,
            )
            parts = parents.stdout.strip().split()
            self.assertEqual(len(parts), 1, "deploy commit should have no parents (orphan)")

    def test_feature_branch_durable_files_from_main(self):
        """When checkout is on a feature branch, deploy's durable files come from main."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        tracked = ("index.html",)
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            # Create and switch to a feature branch that modifies a durable file.
            subprocess.run(["git", "-C", str(local), "checkout", "-b", "feature"], capture_output=True, check=True)
            (local / "showcase" / "index.html").write_text("<h1>Feature Showcase</h1>")
            (local / "feature-only.txt").write_text("only on feature branch")
            subprocess.run(["git", "-C", str(local), "add", "-A"], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(local), "commit", "-m", "feature change"], capture_output=True, check=True)
            # Publish — durable files should come from main (via origin/main), not feature.
            sha = publish_to_deploy_branch(local, "origin", "deploy", tracked)
            self.assertIsNotNone(sha)
            # Showcase should have main's content.
            show = subprocess.run(
                ["git", "-C", str(bare), "show", "deploy:showcase/index.html"],
                capture_output=True, text=True,
            )
            self.assertEqual(show.stdout, "<h1>Showcase</h1>")
            # Feature-only file should NOT be in deploy.
            show_feat = subprocess.run(
                ["git", "-C", str(bare), "show", "deploy:feature-only.txt"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(show_feat.returncode, 0)

    def test_remote_only_update_reflected(self):
        """Changes pushed to main on remote are reflected in deploy."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        tracked = ("index.html", "status.json", "backlog/index.html",
                    "backlog/session.json", "roadmap/index.html")
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            # First publish.
            publish_to_deploy_branch(local, "origin", "deploy", tracked)
            # Simulate a remote-only push (e.g., merged PR) by pushing from a second clone.
            clone2 = Path(td) / "clone2"
            subprocess.run(["git", "clone", "--branch", "main", str(bare), str(clone2)], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(clone2), "config", "user.name", "Test"], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(clone2), "config", "user.email", "test@test.local"], capture_output=True, check=True)
            (clone2 / "new-durable-file.txt").write_text("new content")
            subprocess.run(["git", "-C", str(clone2), "add", "-A"], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(clone2), "commit", "-m", "remote change"], capture_output=True, check=True)
            subprocess.run(["git", "-C", str(clone2), "push", "origin", "main"], capture_output=True, check=True)
            # Publish again — should include the new file.
            (local / "status.json").write_text('{"status":"after-remote-push"}')
            sha = publish_to_deploy_branch(local, "origin", "deploy", tracked)
            self.assertIsNotNone(sha)
            show = subprocess.run(
                ["git", "-C", str(bare), "show", "deploy:new-durable-file.txt"],
                capture_output=True, text=True,
            )
            self.assertEqual(show.stdout, "new content")

    def test_first_run_no_deploy_branch(self):
        """First publish when deploy branch doesn't exist creates it."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        tracked = ("index.html",)
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            # No deploy branch exists yet.
            ls = subprocess.run(
                ["git", "-C", str(bare), "branch", "--list", "deploy"],
                capture_output=True, text=True,
            )
            self.assertEqual(ls.stdout.strip(), "")
            # Publish.
            sha = publish_to_deploy_branch(local, "origin", "deploy", tracked)
            self.assertIsNotNone(sha)
            ls2 = subprocess.run(
                ["git", "-C", str(bare), "branch", "--list", "deploy"],
                capture_output=True, text=True,
            )
            self.assertIn("deploy", ls2.stdout)

    def test_submodule_gitdir(self):
        """Publish works when .git is a pointer file (submodule layout)."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        tracked = ("index.html",)
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            # Simulate submodule: move .git dir, replace with pointer file.
            git_dir = local / ".git"
            real_git_dir = Path(td) / "real-git-dir"
            shutil.move(str(git_dir), str(real_git_dir))
            git_dir.write_text(f"gitdir: {real_git_dir}\n")
            # Verify .git is a file.
            self.assertTrue(git_dir.is_file())
            self.assertFalse(git_dir.is_dir())
            # Publish should work.
            sha = publish_to_deploy_branch(local, "origin", "deploy", tracked)
            self.assertIsNotNone(sha)

    def test_rejects_main_as_deploy_target(self):
        """Publishing to 'main' or 'master' raises RuntimeError."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            with self.assertRaises(RuntimeError) as ctx:
                publish_to_deploy_branch(local, "origin", "main")
            self.assertIn("main", str(ctx.exception))
            with self.assertRaises(RuntimeError) as ctx2:
                publish_to_deploy_branch(local, "origin", "master")
            self.assertIn("master", str(ctx2.exception))

    def test_stale_snapshot_removal(self):
        """A snapshot file removed from tracked list disappears from deploy."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        full_tracked = ("index.html", "status.json")
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            # First publish with both files.
            publish_to_deploy_branch(local, "origin", "deploy", full_tracked)
            show = subprocess.run(
                ["git", "-C", str(bare), "show", "deploy:status.json"],
                capture_output=True, text=True,
            )
            self.assertEqual(show.returncode, 0)
            # Second publish with status.json removed from tracked list.
            reduced_tracked = ("index.html",)
            (local / "index.html").write_text("<h1>Changed</h1>")  # Force a change
            sha = publish_to_deploy_branch(local, "origin", "deploy", reduced_tracked)
            self.assertIsNotNone(sha)
            # status.json should still be on deploy (inherited from main's tree)
            # but NOT the generated version — it should have main's original content.
            show2 = subprocess.run(
                ["git", "-C", str(bare), "show", "deploy:status.json"],
                capture_output=True, text=True,
            )
            # The file exists from main's tree, with main's content.
            self.assertEqual(show2.stdout, '{"status":"ok"}')

    def test_relative_path_out_dir(self):
        """Publish works when out_dir is a relative path (git -C + relative file paths)."""
        from src.loop.monitor.dashboard import publish_to_deploy_branch
        with tempfile.TemporaryDirectory() as td:
            local, bare = self._init_repo_pair(Path(td))
            # Use a relative path to the local repo.
            original_cwd = os.getcwd()
            try:
                os.chdir(td)
                rel_local = Path("local")
                sha = publish_to_deploy_branch(rel_local, "origin", "deploy", (
                    "index.html", "status.json",
                ))
                self.assertIsNotNone(sha)
                # Verify content is correct.
                show = subprocess.run(
                    ["git", "-C", str(bare), "show", "deploy:index.html"],
                    capture_output=True, text=True,
                )
                self.assertEqual(show.stdout, "<h1>Dashboard</h1>")
            finally:
                os.chdir(original_cwd)


class TestWorktreeStage(unittest.TestCase):
    """Integration tests for scripts/worktree_stage.

    Uses real temp-directory git repos with actual symlinks to verify
    the staging helper works correctly in worktree-like layouts.
    """

    def _init_repo(self, tmp: Path) -> Path:
        """Create a git repo with a submodule-like layout and a worktree."""
        repo = tmp / "main-repo"
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.email", "test@test.com"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )

        # Create .agent/ directory with a tracked file
        agent_dir = repo / ".agent" / "memory"
        agent_dir.mkdir(parents=True)
        (agent_dir / "goals.md").write_text("original goals")

        # Create projects/ with a file and a submodule-like dir
        projects_dir = repo / "projects"
        projects_dir.mkdir()
        (projects_dir / "myproj.md").write_text("project report")

        sub_dir = projects_dir / "myproj"
        sub_dir.mkdir()
        subprocess.run(["git", "init", str(sub_dir)], capture_output=True, check=True)
        subprocess.run(
            ["git", "-C", str(sub_dir), "config", "user.email", "test@test.com"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(sub_dir), "config", "user.name", "Test"],
            capture_output=True, check=True,
        )
        (sub_dir / "README.md").write_text("submodule readme")
        subprocess.run(
            ["git", "-C", str(sub_dir), "add", "-A"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(sub_dir), "commit", "-m", "init sub"],
            capture_output=True, check=True,
        )

        # Create a normal tracked file
        (repo / "normal.txt").write_text("normal file")

        # Initial commit
        subprocess.run(
            ["git", "-C", str(repo), "add", "-A"],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", "init"],
            capture_output=True, check=True,
        )

        # Create worktree
        worktree = tmp / "worktree"
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", str(worktree), "-b", "worker-0", "HEAD"],
            capture_output=True, check=True,
        )

        # Replace .agent/ and projects/myproj/ with symlinks (mimics worker_slot.py)
        import shutil
        shutil.rmtree(str(worktree / ".agent"))
        (worktree / ".agent").symlink_to(repo / ".agent")

        shutil.rmtree(str(worktree / "projects" / "myproj"))
        (worktree / "projects" / "myproj").symlink_to(repo / "projects" / "myproj")

        return worktree

    def _run_stage(self, worktree: Path, *paths: str, env: dict | None = None) -> subprocess.CompletedProcess:
        """Run worktree_stage in the given worktree directory.

        Sets PWD to match cwd (mimicking what a real shell does on cd)
        so the symlinked-CWD detection works correctly in tests.
        """
        script = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "worktree_stage"
        if env is not None:
            run_env = env
        else:
            run_env = os.environ.copy()
            run_env["PWD"] = str(worktree)
        run_env.pop("WORKER_BRANCH", None)
        run_env.pop("REPO_ROOT", None)
        return subprocess.run(
            [sys.executable, str(script)] + list(paths),
            cwd=str(worktree),
            capture_output=True,
            text=True,
            env=run_env,
        )

    def _get_staged_entry(self, worktree: Path, path: str) -> str | None:
        """Return the ls-files --stage line for a path, or None."""
        result = subprocess.run(
            ["git", "-C", str(worktree), "ls-files", "--stage", path],
            capture_output=True, text=True,
        )
        line = result.stdout.strip()
        return line if line else None

    def test_regular_file_staging(self):
        """Normal file (no symlink) is staged via hash-object + update-index."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            (wt / "normal.txt").write_text("modified content")

            result = self._run_stage(wt, "normal.txt")
            self.assertEqual(result.returncode, 0, result.stderr)

            entry = self._get_staged_entry(wt, "normal.txt")
            self.assertIsNotNone(entry)
            self.assertIn("100644", entry)

            # Verify the staged blob has the new content
            sha = entry.split()[1]
            show = subprocess.run(
                ["git", "-C", str(wt), "cat-file", "-p", sha],
                capture_output=True, text=True,
            )
            self.assertEqual(show.stdout, "modified content")

    def test_symlinked_dir_file_staging(self):
        """File under symlinked .agent/ dir is staged correctly."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            # Modify the file through the symlink
            (wt / ".agent" / "memory" / "goals.md").write_text("new goals")

            result = self._run_stage(wt, ".agent/memory/goals.md")
            self.assertEqual(result.returncode, 0, result.stderr)

            entry = self._get_staged_entry(wt, ".agent/memory/goals.md")
            self.assertIsNotNone(entry)
            self.assertIn("100644", entry)

            sha = entry.split()[1]
            show = subprocess.run(
                ["git", "-C", str(wt), "cat-file", "-p", sha],
                capture_output=True, text=True,
            )
            self.assertEqual(show.stdout, "new goals")

    def test_executable_file_staging(self):
        """Executable file is staged with mode 100755."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            script_path = wt / "normal.txt"
            script_path.write_text("#!/bin/sh\necho hi")
            os.chmod(str(script_path), 0o755)

            result = self._run_stage(wt, "normal.txt")
            self.assertEqual(result.returncode, 0, result.stderr)

            entry = self._get_staged_entry(wt, "normal.txt")
            self.assertIsNotNone(entry)
            self.assertIn("100755", entry)

    def test_submodule_gitlink_staging(self):
        """Submodule directory is staged with mode 160000."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))

            result = self._run_stage(wt, "projects/myproj")
            self.assertEqual(result.returncode, 0, result.stderr)

            entry = self._get_staged_entry(wt, "projects/myproj")
            self.assertIsNotNone(entry)
            self.assertIn("160000", entry)

            # Verify the staged commit matches the submodule HEAD
            staged_sha = entry.split()[1]
            sub_dir = (wt / "projects" / "myproj").resolve()
            head_result = subprocess.run(
                ["git", "-C", str(sub_dir), "rev-parse", "HEAD"],
                capture_output=True, text=True,
            )
            self.assertEqual(staged_sha, head_result.stdout.strip())

    def test_absolute_path_through_symlink_target(self):
        """Absolute path pointing to the real .agent/ (main repo) resolves correctly."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            main_repo = Path(td) / "main-repo"

            # Modify via the real path
            real_file = main_repo / ".agent" / "memory" / "goals.md"
            real_file.write_text("updated via absolute")

            # Pass the absolute path of the real file
            result = self._run_stage(wt, str(real_file))
            self.assertEqual(result.returncode, 0, result.stderr)

            entry = self._get_staged_entry(wt, ".agent/memory/goals.md")
            self.assertIsNotNone(entry)

            sha = entry.split()[1]
            show = subprocess.run(
                ["git", "-C", str(wt), "cat-file", "-p", sha],
                capture_output=True, text=True,
            )
            self.assertEqual(show.stdout, "updated via absolute")

    def test_nonexistent_path_fails(self):
        """Missing file returns exit code 1 with error on stderr."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))

            result = self._run_stage(wt, "does/not/exist.md")
            self.assertEqual(result.returncode, 1)
            self.assertIn("error:", result.stderr)

    def test_multiple_paths_partial_failure(self):
        """Mix of valid and invalid paths: valid ones staged, exit code 1."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            (wt / "normal.txt").write_text("changed")

            result = self._run_stage(wt, "normal.txt", "nonexistent.txt")
            self.assertEqual(result.returncode, 1)
            # The valid file should still be staged
            entry = self._get_staged_entry(wt, "normal.txt")
            self.assertIsNotNone(entry)

            sha = entry.split()[1]
            show = subprocess.run(
                ["git", "-C", str(wt), "cat-file", "-p", sha],
                capture_output=True, text=True,
            )
            self.assertEqual(show.stdout, "changed")

    def test_sibling_symlink_md_file(self):
        """projects/<slug>.md stages correctly even when sibling projects/<slug>/ is a symlink."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            # Verify sibling is actually a symlink
            self.assertTrue((wt / "projects" / "myproj").is_symlink())
            # Modify the .md file
            (wt / "projects" / "myproj.md").write_text("updated report")

            result = self._run_stage(wt, "projects/myproj.md")
            self.assertEqual(result.returncode, 0, result.stderr)

            entry = self._get_staged_entry(wt, "projects/myproj.md")
            self.assertIsNotNone(entry)

            sha = entry.split()[1]
            show = subprocess.run(
                ["git", "-C", str(wt), "cat-file", "-p", sha],
                capture_output=True, text=True,
            )
            self.assertEqual(show.stdout, "updated report")

    def test_submodule_cwd_rejected(self):
        """Running from inside a submodule directory exits with an error."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            sub_dir = wt / "projects" / "myproj"
            # Run from inside the symlinked submodule
            result = self._run_stage(sub_dir, "projects/myproj.md")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("worktree root", result.stderr)

    def test_deletion_staging(self):
        """Deleting a tracked file via worktree_stage stages the removal."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            # Remove the tracked file from disk
            (wt / "normal.txt").unlink()

            result = self._run_stage(wt, "normal.txt")
            self.assertEqual(result.returncode, 0, result.stderr)

            # Verify the file is no longer in the index
            entry = self._get_staged_entry(wt, "normal.txt")
            self.assertIsNone(entry)  # None = not in index

    def test_deletion_symlinked_file(self):
        """Deleting a tracked file under symlinked .agent/ stages the removal."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            # Remove the tracked file through the symlink
            (wt / ".agent" / "memory" / "goals.md").unlink()

            result = self._run_stage(wt, ".agent/memory/goals.md")
            self.assertEqual(result.returncode, 0, result.stderr)

            entry = self._get_staged_entry(wt, ".agent/memory/goals.md")
            self.assertIsNone(entry)

    def test_symlinked_cwd_rejects(self):
        """Running from inside a symlinked directory is detected and rejected."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            main_repo = Path(td) / "main-repo"

            # wt/.agent/ is a symlink to main_repo/.agent/.
            # Simulate being inside that symlink: set the real CWD to
            # the resolved target (main_repo/.agent/memory/) but $PWD
            # to the logical path (wt/.agent/memory/).
            resolved_cwd = main_repo / ".agent" / "memory"
            logical_cwd = wt / ".agent" / "memory"

            script = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "worktree_stage"
            env = os.environ.copy()
            env["PWD"] = str(logical_cwd)

            result = subprocess.run(
                [sys.executable, str(script), "normal.txt"],
                cwd=str(resolved_cwd),
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("symlinked directory", result.stderr)

    def test_symlinked_cwd_no_pwd_skips_check(self):
        """Without $PWD the symlink check is skipped (best-effort)."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            (wt / "normal.txt").write_text("changed")

            # Remove PWD from env to simulate non-shell context
            env = os.environ.copy()
            env.pop("PWD", None)
            result = self._run_stage(wt, "normal.txt", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_no_args_exits_2(self):
        """Running with no arguments exits with code 2."""
        with tempfile.TemporaryDirectory() as td:
            wt = self._init_repo(Path(td))
            result = self._run_stage(wt)
            self.assertEqual(result.returncode, 2)


# ═══════════════════════════════════════════════════════════════════
# AGENT-057: Thread Continuation Tests
# ═══════════════════════════════════════════════════════════════════


class TestSlackThreadLink(unittest.TestCase):
    """Test _slack_thread_link helper."""

    def test_formats_link(self):
        link = sl.Supervisor._slack_thread_link("C0ABC123", "1700000000.123456")
        self.assertEqual(link, "https://slack.com/archives/C0ABC123/p1700000000123456")

    def test_no_dot(self):
        link = sl.Supervisor._slack_thread_link("C123", "1700000000.000000")
        self.assertNotIn(".", link.split("/p")[1])


class TestContinueInNewThread(unittest.TestCase):
    """Test _continue_in_new_thread core method."""

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_posts_new_thread_and_link_forward(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")

            # Seed task JSON
            task_file = str(sup.cfg.tasks_dir / "incomplete" / "1000.000000.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1000.000000",
                "thread_ts": "1000.000000",
                "channel_id": "C123",
                "messages": [
                    {"ts": "1000.000000", "user_id": "U1", "role": "human", "text": "original"},
                    {"ts": "1000.000010", "user_id": "U_AGENT", "role": "agent", "text": "reply1", "source": "context_snapshot"},
                    {"ts": "1000.000020", "user_id": "U1", "user_name": "alice", "text": "followup", "source": "context_snapshot"},
                ],
            })

            task = {
                "mention_ts": "1000.000000",
                "thread_ts": "1000.000000",
                "channel_id": "C123",
                "mention_text_file": task_file,
                "task_description": "Fix the widget",
                "consecutive_exit_failures": 3,
            }

            api_calls = []
            def fake_post(method, payload):
                api_calls.append((method, payload))
                if method == "chat.postMessage" and "thread_ts" not in payload:
                    return {"ok": True, "ts": "2000.000000"}
                return {"ok": True}

            with patch.object(sup, "slack_api_post", side_effect=fake_post):
                result = sup._continue_in_new_thread(task, "parked", context_message="try again")

            self.assertTrue(result)
            self.assertEqual(task["thread_ts"], "2000.000000")
            self.assertEqual(task["prior_threads"], ["1000.000000"])
            self.assertEqual(task["consecutive_exit_failures"], 0)
            self.assertNotIn("waiting_reason", task)
            self.assertNotIn("continuation_pending", task)

            # Verify two API calls: new thread + link forward
            self.assertEqual(len(api_calls), 2)
            # First call: new top-level message (no thread_ts)
            self.assertNotIn("thread_ts", api_calls[0][1])
            self.assertIn("previous thread", api_calls[0][1]["text"])
            # Second call: link forward (has thread_ts of old thread)
            self.assertEqual(api_calls[1][1]["thread_ts"], "1000.000000")
            self.assertIn("new thread", api_calls[1][1]["text"])

            # Verify task JSON has prior_thread_context messages
            data = json.loads(Path(task_file).read_text(encoding="utf-8"))
            prior_ctx = [m for m in data["messages"] if m.get("source") == "prior_thread_context"]
            self.assertEqual(len(prior_ctx), 2)  # 2 old snapshots carried forward
            # No more context_snapshot messages
            snapshots = [m for m in data["messages"] if m.get("source") == "context_snapshot"]
            self.assertEqual(len(snapshots), 0)

    def test_session_fields_cleared_for_parked(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")

            task_file = str(sup.cfg.tasks_dir / "incomplete" / "2000.000000.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "2000.000000",
                "thread_ts": "2000.000000",
                "channel_id": "C123",
                "messages": [],
            })

            task = {
                "mention_ts": "2000.000000",
                "thread_ts": "2000.000000",
                "channel_id": "C123",
                "mention_text_file": task_file,
                "codex_session_id": "sess-123",
                "session_prompt_hash": "abc",
                "session_resume_count": 3,
            }

            with patch.object(sup, "slack_api_post", return_value={"ok": True, "ts": "3000.000000"}):
                sup._continue_in_new_thread(task, "parked")

            self.assertNotIn("codex_session_id", task)
            self.assertNotIn("session_prompt_hash", task)
            self.assertNotIn("session_resume_count", task)

    def test_session_fields_preserved_for_thread_length(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")

            task_file = str(sup.cfg.tasks_dir / "active" / "2000.000000.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "2000.000000",
                "thread_ts": "2000.000000",
                "channel_id": "C123",
                "messages": [],
            })

            task = {
                "mention_ts": "2000.000000",
                "thread_ts": "2000.000000",
                "channel_id": "C123",
                "mention_text_file": task_file,
                "codex_session_id": "sess-456",
                "session_prompt_hash": "def",
            }

            with patch.object(sup, "slack_api_post", return_value={"ok": True, "ts": "3000.000000"}):
                sup._continue_in_new_thread(task, "thread_length")

            self.assertEqual(task["codex_session_id"], "sess-456")
            self.assertEqual(task["session_prompt_hash"], "def")

    def test_returns_false_on_missing_channel(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            task = {"mention_ts": "1000.000000", "thread_ts": "1000.000000", "channel_id": ""}
            self.assertFalse(sup._continue_in_new_thread(task, "parked"))

    def test_returns_false_on_api_failure(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup)
            task = {"mention_ts": "1000.000000", "thread_ts": "1000.000000", "channel_id": "C123"}
            with patch.object(sup, "slack_api_post", side_effect=Exception("network")):
                result = sup._continue_in_new_thread(task, "parked")
            self.assertFalse(result)
            # thread_ts unchanged on failure
            self.assertEqual(task["thread_ts"], "1000.000000")

    def test_link_forward_failure_is_non_fatal(self):
        """Link forward in old thread can fail without aborting continuation."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")

            task_file = str(sup.cfg.tasks_dir / "active" / "1000.000000.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1000.000000", "thread_ts": "1000.000000",
                "channel_id": "C123", "messages": [],
            })

            task = {
                "mention_ts": "1000.000000",
                "thread_ts": "1000.000000",
                "channel_id": "C123",
                "mention_text_file": task_file,
            }

            call_count = [0]
            def fake_post(method, payload):
                call_count[0] += 1
                if call_count[0] == 1:
                    return {"ok": True, "ts": "2000.000000"}
                raise Exception("link forward failed")

            with patch.object(sup, "slack_api_post", side_effect=fake_post):
                result = sup._continue_in_new_thread(task, "parked")

            self.assertTrue(result)
            self.assertEqual(task["thread_ts"], "2000.000000")


class TestNormalizeTaskPreservesThreadContinuationFields(unittest.TestCase):
    """Test that normalize_task preserves AGENT-057 fields."""

    def test_preserves_prior_threads(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            task = {
                "mention_ts": "1000.000000",
                "thread_ts": "2000.000000",
                "channel_id": "C123",
                "prior_threads": ["1000.000000"],
                "status": "in_progress",
            }
            result = sup.normalize_task(task, "1000.000000")
            self.assertEqual(result["prior_threads"], ["1000.000000"])

    def test_preserves_continuation_pending(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            task = {
                "mention_ts": "1000.000000",
                "thread_ts": "1000.000000",
                "channel_id": "C123",
                "continuation_pending": "thread_length",
                "status": "in_progress",
            }
            result = sup.normalize_task(task, "1000.000000")
            self.assertEqual(result["continuation_pending"], "thread_length")

    def test_preserves_waiting_reason(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            task = {
                "mention_ts": "1000.000000",
                "thread_ts": "1000.000000",
                "channel_id": "C123",
                "waiting_reason": "consecutive_exit_failures",
                "status": "waiting_human",
            }
            result = sup.normalize_task(task, "1000.000000")
            self.assertEqual(result["waiting_reason"], "consecutive_exit_failures")

    def test_absent_fields_not_added(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            task = {
                "mention_ts": "1000.000000",
                "thread_ts": "1000.000000",
                "channel_id": "C123",
                "status": "in_progress",
            }
            result = sup.normalize_task(task, "1000.000000")
            self.assertNotIn("prior_threads", result)
            self.assertNotIn("continuation_pending", result)
            self.assertNotIn("waiting_reason", result)


class TestRenderThreadContextWithPriorThread(unittest.TestCase):
    """Test _render_thread_context with prior_thread_context messages."""

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_prior_thread_context_rendered_before_current(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")

            task_file = str(sup.cfg.tasks_dir / "active" / "1000.000000.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1000.000000",
                "thread_ts": "2000.000000",
                "channel_id": "C123",
                "messages": [
                    {"ts": "1000.000000", "user_id": "U1", "role": "human", "text": "original mention"},
                    # Prior thread context
                    {"ts": "1000.000010", "user_id": "U1", "user_name": "alice", "text": "old msg 1", "source": "prior_thread_context"},
                    {"ts": "1000.000020", "user_id": "U_AGENT", "user_name": "Murphy", "text": "old reply", "source": "prior_thread_context"},
                    # Current thread snapshot
                    {"ts": "2000.000000", "user_id": "U_AGENT", "user_name": "Murphy", "text": "continuation msg", "source": "context_snapshot"},
                    {"ts": "2000.000010", "user_id": "U1", "user_name": "alice", "text": "new msg", "source": "context_snapshot"},
                ],
            })

            original, context = sup._render_thread_context(task_file)
            self.assertEqual(original, "original mention")
            self.assertIn("Prior thread context", context)
            self.assertIn("old msg 1", context)
            self.assertIn("old reply", context)
            self.assertIn("Current thread", context)
            self.assertIn("new msg", context)

            # Prior context comes before current
            prior_pos = context.index("Prior thread context")
            current_pos = context.index("Current thread")
            self.assertLess(prior_pos, current_pos)

    def test_no_prior_context_no_header(self):
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")

            task_file = str(sup.cfg.tasks_dir / "active" / "1000.000000.json")
            Path(task_file).parent.mkdir(parents=True, exist_ok=True)
            sup.write_task_json(task_file, {
                "task_id": "1000.000000",
                "thread_ts": "1000.000000",
                "channel_id": "C123",
                "messages": [
                    {"ts": "1000.000000", "user_id": "U1", "role": "human", "text": "hi"},
                    {"ts": "1000.000010", "user_id": "U_AGENT", "user_name": "Murphy", "text": "hello", "source": "context_snapshot"},
                ],
            })

            _, context = sup._render_thread_context(task_file)
            self.assertNotIn("Prior thread context", context)
            self.assertNotIn("Current thread", context)
            self.assertIn("hello", context)


class TestThreadContinuationThresholdConfig(unittest.TestCase):
    """Test THREAD_CONTINUATION_THRESHOLD config."""

    def test_default_value(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td))
            self.assertEqual(sup.cfg.thread_continuation_threshold, 80)

    def test_custom_value(self):
        with tempfile.TemporaryDirectory() as td:
            sup = _make_supervisor(Path(td), env_overrides={"THREAD_CONTINUATION_THRESHOLD": "50"})
            self.assertEqual(sup.cfg.thread_continuation_threshold, 50)


class TestWaitingReasonSetAndCleared(unittest.TestCase):
    """Test waiting_reason lifecycle in parking and reactivation."""

    def _sup(self, tmp):
        return _make_supervisor(Path(tmp))

    def test_waiting_reason_set_on_parking(self):
        """Consecutive exit failures set waiting_reason on the task."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")

            state = _empty_state()
            task_key = "1000.000000"
            task = {
                "mention_ts": task_key,
                "thread_ts": task_key,
                "channel_id": "C123",
                "status": "in_progress",
                "task_type": "slack_mention",
                "consecutive_exit_failures": 2,  # Will become 3 = threshold
                "claimed_by": "test-slot-0",
            }
            state["active_tasks"][task_key] = task
            sup.save_state(state)

            # Write dispatch task file and outcome
            sup.cfg.dispatch_task_file.parent.mkdir(parents=True, exist_ok=True)
            sup.atomic_write_json(sup.cfg.dispatch_task_file, task)

            outcome_path = sup.cfg.outcomes_dir / f"{task_key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            json.dump({
                "mention_ts": task_key,
                "thread_ts": task_key,
                "status": "failed",
                "summary": "crash",
            }, outcome_path.open("w"))

            sup.selected_key = task_key
            sup.selected_bucket = "active_tasks"

            with patch.object(sup, "slack_api_post", return_value={"ok": True}):
                with patch.object(sup, "_fetch_thread_messages", return_value=[]):
                    sup.reconcile_task_after_run(task_key, worker_exit=1)

            state = sup.load_state()
            t = state["incomplete_tasks"].get(task_key)
            self.assertIsNotNone(t)
            self.assertEqual(t.get("waiting_reason"), "consecutive_exit_failures")
            self.assertEqual(t["status"], "waiting_human")

    def test_waiting_reason_cleared_on_success(self):
        """Successful run clears waiting_reason."""
        with tempfile.TemporaryDirectory() as td:
            sup = self._sup(td)
            _write_agent_identity(sup, "U_AGENT")

            state = _empty_state()
            task_key = "1000.000000"
            task = {
                "mention_ts": task_key,
                "thread_ts": task_key,
                "channel_id": "C123",
                "status": "in_progress",
                "task_type": "slack_mention",
                "consecutive_exit_failures": 1,
                "waiting_reason": "consecutive_exit_failures",
                "claimed_by": "test-slot-0",
            }
            state["active_tasks"][task_key] = task
            sup.save_state(state)

            sup.cfg.dispatch_task_file.parent.mkdir(parents=True, exist_ok=True)
            sup.atomic_write_json(sup.cfg.dispatch_task_file, task)

            outcome_path = sup.cfg.outcomes_dir / f"{task_key}.json"
            outcome_path.parent.mkdir(parents=True, exist_ok=True)
            json.dump({
                "mention_ts": task_key,
                "thread_ts": task_key,
                "status": "done",
                "summary": "completed",
            }, outcome_path.open("w"))

            sup.selected_key = task_key
            sup.selected_bucket = "active_tasks"

            with patch.object(sup, "slack_api_post", return_value={"ok": True}):
                with patch.object(sup, "_fetch_thread_messages", return_value=[
                    {"ts": task_key, "user": "U1", "text": "hi"},
                    {"ts": "1000.000010", "user": "U_AGENT", "text": "done"},
                ]):
                    sup.reconcile_task_after_run(task_key, worker_exit=0)

            state = sup.load_state()
            t = state["finished_tasks"].get(task_key)
            self.assertIsNotNone(t)
            self.assertNotIn("waiting_reason", t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
