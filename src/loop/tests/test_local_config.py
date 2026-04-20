#!/usr/bin/env python3
"""Tests for canonical Murphy local config and projections."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

from src.loop.cli.canonical import (  # noqa: E402
    CanonicalConfig,
    build_projection_map,
    existing_canonical_path,
    import_existing_install,
    render_claude_config,
    render_codex_config,
    render_env,
    render_manifest,
)


class TestCanonicalConfig(unittest.TestCase):
    def test_render_env(self):
        cfg = CanonicalConfig()
        cfg.slack.user_token = "xoxp-token"
        cfg.slack.default_channel_id = "C123"
        cfg.agent.name = "Terry"
        cfg.runtime.max_concurrent_workers = 2
        cfg.dashboard.export_enabled = True
        text = render_env(cfg)
        self.assertIn("SLACK_USER_TOKEN=xoxp-token", text)
        self.assertIn("DEFAULT_CHANNEL_ID=C123", text)
        self.assertIn("AGENT_NAME=Terry", text)
        self.assertIn("MAX_CONCURRENT_WORKERS=2", text)
        self.assertIn("DASHBOARD_EXPORT_ENABLED=true", text)

    def test_render_codex_config(self):
        cfg = CanonicalConfig()
        cfg.slack.user_token = "xoxp-token"
        cfg.worker.chatgpt_project = "Project X"
        cfg.consult.command = "/usr/bin/consult"
        cfg.consult.args = ["--mode", "prod"]
        text = render_codex_config(cfg, REPO_ROOT)
        self.assertIn('CHATGPT_DEFAULT_PROJECT = "Project X"', text)
        self.assertIn('command = "/usr/bin/consult"', text)
        self.assertIn('args = ["--mode", "prod"]', text)
        self.assertIn('SLACK_MCP_XOXP_TOKEN = "xoxp-token"', text)

    def test_render_claude_config(self):
        cfg = CanonicalConfig()
        cfg.slack.user_token = "xoxp-token"
        text = render_claude_config(cfg, REPO_ROOT)
        payload = json.loads(text)
        self.assertEqual(
            payload["mcpServers"]["slack"]["env"]["SLACK_MCP_XOXP_TOKEN"],
            "xoxp-token",
        )
        self.assertIn("slack-mcp-server", payload["mcpServers"]["slack"]["command"])

    def test_render_manifest(self):
        cfg = CanonicalConfig()
        cfg.slack.app_name = "Test Murphy"
        cfg.slack.app_description = "Test description"
        payload = json.loads(render_manifest(cfg))
        self.assertEqual(payload["display_information"]["name"], "Test Murphy")
        self.assertEqual(payload["display_information"]["description"], "Test description")
        self.assertIn("chat:write", payload["oauth_config"]["scopes"]["user"])

    def test_import_existing_install(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".codex").mkdir(parents=True, exist_ok=True)
            (root / "src/config").mkdir(parents=True, exist_ok=True)
            (root / ".env").write_text(
                "\n".join(
                    [
                        "SLACK_USER_TOKEN=xoxp-env",
                        "DEFAULT_CHANNEL_ID=C123",
                        "AGENT_NAME=Terry",
                        "MAX_CONCURRENT_WORKERS=3",
                        'WORKER_CMD="codex exec --yolo -"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (root / ".codex/config.toml").write_text(
                "\n".join(
                    [
                        'model = "gpt-5.4-mini"',
                        'model_reasoning_effort = "medium"',
                        "",
                        "[mcp_servers.consult]",
                        'command = "/usr/bin/consult"',
                        'args = ["--mode", "prod"]',
                        "",
                        "[mcp_servers.consult.env]",
                        'CHATGPT_DEFAULT_PROJECT = "Legacy Project"',
                        "",
                        "[mcp_servers.slack.env]",
                        'SLACK_MCP_XOXP_TOKEN = "xoxp-env"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            (root / "src/config/claude_mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "slack": {
                                "env": {
                                    "SLACK_MCP_XOXP_TOKEN": "xoxp-env",
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "slack-app-manifest.json").write_text(
                json.dumps(
                    {
                        "display_information": {
                            "name": "Legacy Murphy",
                            "description": "Legacy description",
                        }
                    }
                ),
                encoding="utf-8",
            )

            imported = import_existing_install(root)
            self.assertFalse(imported.conflicts)
            self.assertEqual(imported.config.slack.user_token, "xoxp-env")
            self.assertEqual(imported.config.slack.default_channel_id, "C123")
            self.assertEqual(imported.config.agent.name, "Terry")
            self.assertEqual(imported.config.runtime.max_concurrent_workers, 3)
            self.assertEqual(imported.config.worker.model, "gpt-5.4-mini")
            self.assertEqual(imported.config.worker.chatgpt_project, "Legacy Project")
            self.assertEqual(imported.config.consult.command, "/usr/bin/consult")
            self.assertEqual(imported.config.consult.args, ["--mode", "prod"])
            self.assertEqual(imported.config.slack.app_name, "Legacy Murphy")

    def test_import_existing_conflict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / ".codex").mkdir(parents=True, exist_ok=True)
            (root / "src/config").mkdir(parents=True, exist_ok=True)
            (root / ".env").write_text("SLACK_USER_TOKEN=xoxp-env\n", encoding="utf-8")
            (root / ".codex/config.toml").write_text(
                "[mcp_servers.slack.env]\nSLACK_MCP_XOXP_TOKEN = \"xoxp-codex\"\n",
                encoding="utf-8",
            )
            (root / "src/config/claude_mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "slack": {
                                "env": {
                                    "SLACK_MCP_XOXP_TOKEN": "xoxp-claude",
                                }
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            imported = import_existing_install(root)
            self.assertEqual(len(imported.conflicts), 1)
            self.assertEqual(imported.conflicts[0].key, "slack.user_token")

    def test_projection_map_uses_manifest_path(self):
        cfg = CanonicalConfig()
        cfg.files.manifest_path = "tmp/custom-manifest.json"
        mapping = build_projection_map(cfg, REPO_ROOT)
        self.assertIn(REPO_ROOT / "tmp/custom-manifest.json", mapping)

    def test_existing_canonical_path_falls_back_to_legacy_location(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            legacy = root / ".murphy/config.toml"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.write_text("[agent]\nname = \"Murphy\"\n", encoding="utf-8")
            self.assertEqual(existing_canonical_path(root), legacy)


if __name__ == "__main__":
    unittest.main()
