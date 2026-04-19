#!/usr/bin/env python3
"""Bootstrap CLI for public Murphy repo setup."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src import __version__ as PACKAGE_VERSION

DEFAULT_AGENT_NAME = "Murphy"
DEFAULT_SLACK_APP_NAME = "Murphy Agent"
DEFAULT_SLACK_APP_DESCRIPTION = "Self-hosted Slack supervisor for long-running AI work"
DEFAULT_CHATGPT_PROJECT = "Murphy"
DEFAULT_MANIFEST_PATH = Path("slack-app-manifest.json")
DEFAULT_MEMORY_BODY = "# Curated Memory\n\n- Add durable preferences, constraints, and operating notes here.\n"
DEFAULT_GOALS_BODY = "# Long-Term Goals\n\n- Add active goals and progress notes here.\n"
SLACK_MCP_COMMAND_PLACEHOLDER = "__SLACK_MCP_COMMAND__"
USER_SCOPES = [
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "im:read",
    "im:write",
    "mpim:history",
    "mpim:read",
    "mpim:write",
    "users:read",
    "chat:write",
    "search:read",
    "files:read",
    "files:write",
    "reactions:read",
    "reactions:write",
]


def _toml_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def build_slack_manifest(app_name: str, app_description: str) -> str:
    manifest = {
        "display_information": {
            "name": app_name,
            "description": app_description,
        },
        "oauth_config": {
            "scopes": {
                "user": USER_SCOPES,
            }
        },
        "settings": {
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }
    return json.dumps(manifest, indent=2) + "\n"


def _read_required(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Missing required template: {path}")
    return path.read_text(encoding="utf-8")


def _render_env(template_root: Path, default_channel_id: str, agent_name: str) -> str:
    text = _read_required(template_root / ".env.example")
    if default_channel_id:
        text = text.replace("DEFAULT_CHANNEL_ID=C0YOUR_CHANNEL_ID", f"DEFAULT_CHANNEL_ID={default_channel_id}")
    if agent_name and agent_name != DEFAULT_AGENT_NAME:
        text = text.replace("# AGENT_NAME=Murphy", f"AGENT_NAME={agent_name}")
    return text


def _repo_path(repo_root: Path, relative: str) -> str:
    return (repo_root / relative).resolve().as_posix()


def _render_codex_config(template_root: Path, repo_root: Path, chatgpt_project: str) -> str:
    text = _read_required(template_root / ".codex/config.example.toml")
    text = text.replace(
        SLACK_MCP_COMMAND_PLACEHOLDER,
        _repo_path(repo_root, "mcp/slack-mcp-server/build/slack-mcp-server"),
    )
    marker = 'CHATGPT_DEFAULT_PROJECT = "Murphy"'
    return text.replace(marker, f"CHATGPT_DEFAULT_PROJECT = {_toml_quote(chatgpt_project)}")


def _render_claude_config(template_root: Path, repo_root: Path) -> str:
    text = _read_required(template_root / "src/config/claude_mcp.example.json")
    return text.replace(
        SLACK_MCP_COMMAND_PLACEHOLDER,
        _repo_path(repo_root, "mcp/slack-mcp-server/build/slack-mcp-server"),
    )


def _write_file(path: Path, content: str, force: bool) -> str:
    existed = path.exists()
    if existed and not force:
        return "skipped"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "updated" if existed else "created"


def bootstrap_repo(
    repo_root: Path,
    *,
    template_root: Path | None = None,
    force: bool = False,
    slack_app_name: str = DEFAULT_SLACK_APP_NAME,
    slack_app_description: str = DEFAULT_SLACK_APP_DESCRIPTION,
    default_channel_id: str = "",
    chatgpt_project: str = DEFAULT_CHATGPT_PROJECT,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    agent_name: str = DEFAULT_AGENT_NAME,
) -> list[tuple[str, str]]:
    repo_root = repo_root.resolve()
    template_root = repo_root if template_root is None else template_root.resolve()

    writes = [
        (Path(".env"), _render_env(template_root, default_channel_id, agent_name)),
        (Path(".codex/config.toml"), _render_codex_config(template_root, repo_root, chatgpt_project)),
        (Path("src/config/claude_mcp.json"), _render_claude_config(template_root, repo_root)),
        (manifest_path, build_slack_manifest(slack_app_name, slack_app_description)),
        (Path(".agent/memory/memory.md"), DEFAULT_MEMORY_BODY),
        (Path(".agent/memory/long_term_goals.md"), DEFAULT_GOALS_BODY),
    ]

    results: list[tuple[str, str]] = []
    for rel_path, content in writes:
        status = _write_file(repo_root / rel_path, content, force=force)
        results.append((status, str(rel_path)))
    return results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="murphy-init",
        description="Generate the local config files and Slack app manifest for a fresh Murphy checkout.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"murphy-agent {PACKAGE_VERSION}",
    )
    parser.add_argument(
        "--repo-root",
        default=".",
        help="Repo root to initialize. Defaults to the current directory.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing generated files instead of skipping them.",
    )
    parser.add_argument(
        "--agent-name",
        default=DEFAULT_AGENT_NAME,
        help=(
            "Public-facing agent name used inside worker/reviewer prompts "
            "(sets AGENT_NAME in .env). Defaults to the Slack app name when "
            "--slack-app-name is a single word."
        ),
    )
    parser.add_argument(
        "--slack-app-name",
        default=DEFAULT_SLACK_APP_NAME,
        help="Display name to place in the generated Slack app manifest.",
    )
    parser.add_argument(
        "--slack-app-description",
        default=DEFAULT_SLACK_APP_DESCRIPTION,
        help="Description to place in the generated Slack app manifest.",
    )
    parser.add_argument(
        "--default-channel-id",
        default="",
        help="Optional default Slack channel ID to prefill into .env.",
    )
    parser.add_argument(
        "--chatgpt-project",
        default=DEFAULT_CHATGPT_PROJECT,
        help="Value to place into CHATGPT_DEFAULT_PROJECT inside .codex/config.toml.",
    )
    parser.add_argument(
        "--manifest-path",
        default=str(DEFAULT_MANIFEST_PATH),
        help="Relative output path for the generated Slack app manifest.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root)
    if not repo_root.exists():
        print(f"Repo root does not exist: {repo_root}", file=sys.stderr)
        return 1

    try:
        results = bootstrap_repo(
            repo_root,
            force=args.force,
            slack_app_name=args.slack_app_name,
            slack_app_description=args.slack_app_description,
            default_channel_id=args.default_channel_id,
            chatgpt_project=args.chatgpt_project,
            manifest_path=Path(args.manifest_path),
            agent_name=args.agent_name,
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    grouped: dict[str, list[str]] = {"created": [], "updated": [], "skipped": []}
    for status, rel_path in results:
        grouped.setdefault(status, []).append(rel_path)

    for label in ("created", "updated", "skipped"):
        items = grouped.get(label, [])
        if not items:
            continue
        print(f"{label.title()}:")
        for item in items:
            print(f"  - {item}")

    manifest_path = Path(args.manifest_path)
    print("\nNext steps:")
    print(f"  1. Import {manifest_path} into https://api.slack.com/apps and install it to your workspace.")
    print("  2. Copy the resulting xoxp user token into .env, .codex/config.toml, and src/config/claude_mcp.json.")
    print("  3. Build mcp/slack-mcp-server, authenticate the CLIs you plan to use, then run ./scripts/run.sh.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
