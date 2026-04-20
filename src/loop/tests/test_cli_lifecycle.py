#!/usr/bin/env python3
"""Tests for unified Murphy lifecycle commands."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.loop.cli.app import main as cli_main  # noqa: E402


def _make_repo_root(root: Path) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts/run.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'murphy-test'\n", encoding="utf-8")


class TestLifecycleCLI(unittest.TestCase):
    def test_start_with_explicit_repo_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make_repo_root(root)

            with patch("src.loop.cli.lifecycle.shutil.which", return_value="/usr/bin/tmux"), patch(
                "src.loop.cli.lifecycle.subprocess.run",
                side_effect=[
                    CompletedProcess(args=["tmux"], returncode=1, stdout="", stderr=""),
                    CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
                ],
            ) as mock_run:
                rc = cli_main(["start", "--repo-root", str(root)])

            self.assertEqual(rc, 0)
            self.assertEqual(mock_run.call_args_list[0].args[0][:3], ["tmux", "has-session", "-t"])
            self.assertEqual(mock_run.call_args_list[1].args[0][:3], ["tmux", "new-session", "-d"])

    def test_start_refuses_duplicate_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make_repo_root(root)
            out = io.StringIO()
            with redirect_stdout(out), patch(
                "src.loop.cli.lifecycle.shutil.which", return_value="/usr/bin/tmux"
            ), patch(
                "src.loop.cli.lifecycle.subprocess.run",
                return_value=CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
            ):
                rc = cli_main(["start", "--repo-root", str(root)])

            self.assertEqual(rc, 1)
            self.assertIn("already exists", out.getvalue())

    def test_restart_missing_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make_repo_root(root)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli_main(["restart", "--repo-root", str(root)])

            self.assertEqual(rc, 1)
            self.assertIn("does not appear to be running", out.getvalue())

    def test_restart_sends_sighup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make_repo_root(root)
            heartbeat = root / ".agent/runtime/heartbeat.json"
            heartbeat.parent.mkdir(parents=True, exist_ok=True)
            heartbeat.write_text(
                json.dumps({"pid": 12345, "last_updated_utc": "2026-04-19T00:00:00Z"}),
                encoding="utf-8",
            )
            with patch("src.loop.cli.lifecycle.os.kill") as mock_kill:
                rc = cli_main(["restart", "--repo-root", str(root), "--wait-seconds", "0"])

            self.assertEqual(rc, 0)
            mock_kill.assert_called_once()
            self.assertEqual(mock_kill.call_args.args[0], 12345)

    def test_status_reads_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make_repo_root(root)
            heartbeat = root / ".agent/runtime/heartbeat.json"
            heartbeat.parent.mkdir(parents=True, exist_ok=True)
            heartbeat.write_text(
                json.dumps(
                    {
                        "status": "sleeping",
                        "pid": 12345,
                        "loop_count": 7,
                        "last_updated_utc": "2026-04-19T00:00:00Z",
                        "max_workers": 2,
                        "active_workers": [{"slot_id": "w1"}],
                    }
                ),
                encoding="utf-8",
            )
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cli_main(["status", "--repo-root", str(root)])

            self.assertEqual(rc, 0)
            text = out.getvalue()
            self.assertIn("status: sleeping", text)
            self.assertIn("pid: 12345", text)
            self.assertIn("loop count: 7", text)
            self.assertIn("active workers: 1", text)


if __name__ == "__main__":
    unittest.main()
