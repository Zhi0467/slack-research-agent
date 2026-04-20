#!/usr/bin/env python3
"""Tests for Murphy init/bootstrap flows."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.loop import bootstrap  # noqa: E402
from src.loop.bootstrap import bootstrap_repo  # noqa: E402


def _make_repo_root(root: Path) -> None:
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts/run.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'murphy-test'\n", encoding="utf-8")
    slack_mcp = root / "mcp/slack-mcp-server/build/slack-mcp-server"
    slack_mcp.parent.mkdir(parents=True, exist_ok=True)
    slack_mcp.write_text("", encoding="utf-8")


class TestBootstrapRepo(unittest.TestCase):
    def test_creates_manifest_and_local_configs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            results = bootstrap_repo(
                root,
                template_root=REPO_ROOT,
                slack_app_name="Test Murphy",
                slack_app_description="Test description",
                default_channel_id="C1234567890",
                chatgpt_project="Test Project",
            )

            self.assertIn(("created", "config.toml"), results)
            self.assertIn(("created", ".env"), results)
            self.assertIn(("created", ".codex/config.toml"), results)
            self.assertIn(("created", "src/config/claude_mcp.json"), results)
            self.assertIn(("created", "slack-app-manifest.json"), results)
            self.assertIn(("created", ".agent/memory/memory.md"), results)
            self.assertIn(("created", ".agent/memory/long_term_goals.md"), results)

            canonical_text = (root / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[slack]", canonical_text)
            self.assertIn('app_name = "Test Murphy"', canonical_text)

            env_text = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("DEFAULT_CHANNEL_ID=C1234567890", env_text)
            self.assertIn("AGENT_NAME=Murphy", env_text)

            codex_text = (root / ".codex/config.toml").read_text(encoding="utf-8")
            self.assertIn('CHATGPT_DEFAULT_PROJECT = "Test Project"', codex_text)
            self.assertIn('command = ""', codex_text)
            self.assertIn((root / "mcp/slack-mcp-server/build/slack-mcp-server").as_posix(), codex_text)

            claude_text = (root / "src/config/claude_mcp.json").read_text(encoding="utf-8")
            self.assertIn((root / "mcp/slack-mcp-server/build/slack-mcp-server").as_posix(), claude_text)

            manifest = json.loads((root / "slack-app-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["display_information"]["name"], "Test Murphy")
            self.assertEqual(manifest["display_information"]["description"], "Test description")
            self.assertIn("chat:write", manifest["oauth_config"]["scopes"]["user"])
            self.assertIn("search:read", manifest["oauth_config"]["scopes"]["user"])
            self.assertIn("files:write", manifest["oauth_config"]["scopes"]["user"])
            self.assertIn("reactions:write", manifest["oauth_config"]["scopes"]["user"])

            memory_text = (root / ".agent/memory/memory.md").read_text(encoding="utf-8")
            goals_text = (root / ".agent/memory/long_term_goals.md").read_text(encoding="utf-8")
            self.assertIn("# Curated Memory", memory_text)
            self.assertIn("# Long-Term Goals", goals_text)

    def test_agent_name_written_into_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bootstrap_repo(
                root,
                template_root=REPO_ROOT,
                agent_name="Terry",
            )
            env_text = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("AGENT_NAME=Terry", env_text)

    def test_default_agent_name_written_into_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            bootstrap_repo(root, template_root=REPO_ROOT)
            env_text = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("AGENT_NAME=Murphy", env_text)

    def test_skips_existing_files_without_force(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text("KEEP_ME=true\n", encoding="utf-8")

            results = bootstrap_repo(root, template_root=REPO_ROOT)

            self.assertIn(("skipped", ".env"), results)
            self.assertEqual(env_path.read_text(encoding="utf-8"), "KEEP_ME=true\n")

    def test_force_overwrites_existing_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            env_path = root / ".env"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text("OLD=true\n", encoding="utf-8")

            results = bootstrap_repo(root, template_root=REPO_ROOT, force=True, default_channel_id="C999")

            self.assertIn(("updated", ".env"), results)
            self.assertIn("DEFAULT_CHANNEL_ID=C999", env_path.read_text(encoding="utf-8"))


class TestInitCommand(unittest.TestCase):
    def test_non_interactive_happy_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make_repo_root(root)

            with patch("src.loop.cli.canonical.shutil.which", return_value="/usr/bin/fake"):
                rc = bootstrap.main(
                    [
                        "--repo-root",
                        str(root),
                        "--non-interactive",
                        "--slack-user-token",
                        "xoxp-test-token",
                        "--default-channel-id",
                        "C123456",
                        "--slack-app-name",
                        "Murphy Agent",
                    ]
                )

            self.assertEqual(rc, 0)
            self.assertTrue((root / "config.toml").exists())
            env_text = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("SLACK_USER_TOKEN=xoxp-test-token", env_text)
            self.assertIn("DEFAULT_CHANNEL_ID=C123456", env_text)

    def test_single_word_slack_app_name_defaults_agent_name(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make_repo_root(root)

            with patch("src.loop.cli.canonical.shutil.which", return_value="/usr/bin/fake"):
                rc = bootstrap.main(
                    [
                        "--repo-root",
                        str(root),
                        "--non-interactive",
                        "--slack-user-token",
                        "xoxp-test-token",
                        "--default-channel-id",
                        "C123456",
                        "--slack-app-name",
                        "Terry",
                    ]
                )

            self.assertEqual(rc, 0)
            env_text = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("AGENT_NAME=Terry", env_text)

    def test_interactive_happy_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _make_repo_root(root)
            answers = [
                "",  # slack app name
                "",  # slack app description
                "",  # agent name
                "xoxp-interactive",
                "C777",
                "",  # explicit agent user id
                "",  # max workers
                "",  # chatgpt project
                "",  # worker model
                "",  # worker reasoning effort
                "",  # consult command
                "",  # consult args
                "",  # developer-review backend
                "",  # developer-review command
                "",  # enable tribune
                "",  # enable dashboard export
            ]

            with patch("builtins.input", side_effect=answers), patch(
                "src.loop.cli.canonical.shutil.which", return_value="/usr/bin/fake"
            ):
                rc = bootstrap.main(["--repo-root", str(root)])

            self.assertEqual(rc, 0)
            env_text = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("SLACK_USER_TOKEN=xoxp-interactive", env_text)
            self.assertIn("DEFAULT_CHANNEL_ID=C777", env_text)


if __name__ == "__main__":
    unittest.main()
