#!/usr/bin/env python3
"""Init/onboarding command for the unified Murphy CLI."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterable, Optional

from src import __version__ as PACKAGE_VERSION

from .cli.canonical import (
    CANONICAL_CONFIG_PATH,
    CanonicalConfig,
    DEFAULT_AGENT_NAME,
    DEFAULT_CHATGPT_PROJECT,
    DEFAULT_MANIFEST_PATH,
    DEFAULT_SLACK_APP_DESCRIPTION,
    DEFAULT_SLACK_APP_NAME,
    DoctorFinding,
    canonical_path,
    doctor_config,
    dump_canonical_toml,
    existing_canonical_path,
    format_doctor_findings,
    format_import_conflicts,
    format_projection_results,
    import_existing_install,
    infer_agent_name_from_app_name,
    load_canonical,
    save_canonical,
    sync_projections,
    write_text_file,
)
from .cli.common import RepoRootNotFoundError, resolve_repo_root


def add_init_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repo root to initialize. Defaults to the current directory.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing generated files instead of skipping them.",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Do not prompt; rely only on imported state plus explicit flags.",
    )
    parser.add_argument(
        "--prefer",
        choices=("env", "codex", "claude"),
        help="Preferred source when importing conflicting existing local files.",
    )
    parser.add_argument(
        "--agent-name",
        default=None,
        help=(
            "Public-facing agent name used inside worker/reviewer prompts. "
            "If omitted and --slack-app-name is a single word, that app name "
            "becomes the default agent name."
        ),
    )
    parser.add_argument(
        "--slack-app-name",
        default=None,
        help="Display name to place in the generated Slack app manifest.",
    )
    parser.add_argument(
        "--slack-app-description",
        default=None,
        help="Description to place in the generated Slack app manifest.",
    )
    parser.add_argument(
        "--slack-user-token",
        default=None,
        help="Slack xoxp user token for the supervisor and MCP clients.",
    )
    parser.add_argument(
        "--default-channel-id",
        default=None,
        help="Optional default Slack channel ID for system messages.",
    )
    parser.add_argument(
        "--agent-user-id",
        default=None,
        help="Optional explicit Slack user ID override for the agent account.",
    )
    parser.add_argument(
        "--max-concurrent-workers",
        type=int,
        default=None,
        help="Maximum number of parallel workers.",
    )
    parser.add_argument(
        "--session-minutes",
        type=int,
        default=None,
        help="Default worker session budget in minutes.",
    )
    parser.add_argument(
        "--chatgpt-project",
        default=None,
        help="Value to place into CHATGPT_DEFAULT_PROJECT inside .codex/config.toml.",
    )
    parser.add_argument(
        "--worker-command",
        default=None,
        help="Runtime worker command written to WORKER_CMD in .env.",
    )
    parser.add_argument("--worker-model", default=None, help="Codex worker model.")
    parser.add_argument(
        "--worker-reasoning-effort",
        default=None,
        help="Codex worker reasoning effort.",
    )
    parser.add_argument("--worker-personality", default=None, help="Codex worker personality.")
    parser.add_argument("--worker-approval-policy", default=None, help="Codex approval policy.")
    parser.add_argument("--worker-sandbox-mode", default=None, help="Codex sandbox mode.")
    parser.add_argument("--worker-web-search", default=None, help="Codex web search mode.")
    parser.add_argument(
        "--consult-command",
        default=None,
        help="Optional consult MCP command for .codex/config.toml.",
    )
    parser.add_argument(
        "--consult-args",
        default=None,
        help="Comma-separated consult MCP args.",
    )
    parser.add_argument(
        "--dev-review-backend",
        choices=("claude", "none"),
        default=None,
        help="Developer-review backend.",
    )
    parser.add_argument(
        "--dev-review-command",
        default=None,
        help="Developer-review command written to DEV_REVIEW_CMD.",
    )
    parser.add_argument(
        "--tribune-enabled",
        dest="tribune_enabled",
        action="store_true",
        default=None,
        help="Enable Tribune review support.",
    )
    parser.add_argument(
        "--tribune-disabled",
        dest="tribune_enabled",
        action="store_false",
        help="Disable Tribune review support.",
    )
    parser.add_argument(
        "--tribune-review-rounds",
        type=int,
        default=None,
        help="TRIBUNE_MAX_REVIEW_ROUNDS value.",
    )
    parser.add_argument(
        "--tribune-maintenance-rounds",
        type=int,
        default=None,
        help="TRIBUNE_MAINT_ROUNDS value.",
    )
    parser.add_argument("--tribune-command", default=None, help="Tribune CLI command.")
    parser.add_argument(
        "--tribune-fallback-models",
        default=None,
        help="Comma-separated fallback models for Tribune.",
    )
    parser.add_argument(
        "--dashboard-export-enabled",
        dest="dashboard_export_enabled",
        action="store_true",
        default=None,
        help="Enable dashboard static export.",
    )
    parser.add_argument(
        "--dashboard-export-disabled",
        dest="dashboard_export_enabled",
        action="store_false",
        help="Disable dashboard static export.",
    )
    parser.add_argument(
        "--dashboard-export-dir",
        default=None,
        help="Dashboard export directory.",
    )
    parser.add_argument(
        "--manifest-path",
        default=None,
        help="Relative output path for the generated Slack app manifest.",
    )


def build_parser(prog: str = "murphy init") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Onboard a Murphy checkout and generate all local config projections.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"murphy-agent {PACKAGE_VERSION}",
    )
    add_init_arguments(parser)
    return parser


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
    slack_user_token: str = "",
) -> list[tuple[str, str]]:
    del template_root  # retained for backward compatibility with existing tests

    cfg = CanonicalConfig()
    cfg.slack.app_name = slack_app_name
    cfg.slack.app_description = slack_app_description
    cfg.slack.default_channel_id = default_channel_id
    cfg.slack.user_token = slack_user_token
    cfg.worker.chatgpt_project = chatgpt_project
    cfg.agent.name = agent_name
    cfg.files.manifest_path = str(manifest_path)

    results: list[tuple[str, str]] = []
    canonical_cfg_path = canonical_path(repo_root)
    canonical_status = write_text_file(
        canonical_cfg_path,
        dump_canonical_toml(cfg),
        force=force,
    )
    results.append((canonical_status, str(CANONICAL_CONFIG_PATH)))
    for projection in sync_projections(cfg, repo_root.resolve(), force=force):
        results.append((projection.status, projection.relative_path))
    return results


def _prompt_text(label: str, default: str, *, allow_empty: bool = True) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        raw = input(f"{label}{suffix}: ").strip()
        if raw:
            return raw
        if default or allow_empty:
            return default
        print("A value is required.")


def _prompt_bool(label: str, default: bool) -> bool:
    while True:
        suffix = "Y/n" if default else "y/N"
        raw = input(f"{label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "1", "true"}:
            return True
        if raw in {"n", "no", "0", "false"}:
            return False
        print("Enter y or n.")


def _prompt_int(label: str, default: int) -> int:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print("Enter an integer.")


def _prompt_choice(label: str, default: str, choices: Iterable[str]) -> str:
    choices_list = list(choices)
    choices_label = "/".join(choices_list)
    while True:
        raw = input(f"{label} [{default}] ({choices_label}): ").strip().lower()
        if not raw:
            return default
        if raw in choices_list:
            return raw
        print(f"Choose one of: {choices_label}")


def _resolve_import_base(repo_root: Path, prefer: Optional[str], interactive: bool) -> tuple[CanonicalConfig, list[str]]:
    cfg_path = existing_canonical_path(repo_root)
    notes: list[str] = []
    if cfg_path is not None:
        cfg, warnings = load_canonical(cfg_path)
        if cfg_path != canonical_path(repo_root):
            notes.append(f"found legacy canonical config at {cfg_path.relative_to(repo_root)}")
        notes.extend(f"warning: {warning}" for warning in warnings)
        return cfg, notes

    imported = import_existing_install(repo_root, prefer=prefer)
    if imported.conflicts and not prefer:
        if not interactive:
            print("Existing local files disagree. Re-run with --prefer env|codex|claude.")
            print(format_import_conflicts(imported.conflicts))
            raise RuntimeError("conflicting existing local files")
        print("Existing local files disagree:")
        print(format_import_conflicts(imported.conflicts))
        chosen = _prompt_choice("Prefer values from which source", "env", ("env", "codex", "claude"))
        imported = import_existing_install(repo_root, prefer=chosen)
        notes.append(f"import chose {chosen} for conflicting values")

    if imported.imported_keys:
        notes.append("imported existing local values:")
        for key in imported.imported_keys:
            notes.append(f"  - {key}")
    notes.extend(f"warning: {warning}" for warning in imported.warnings)
    return imported.config, notes


def _apply_flag_overrides(cfg: CanonicalConfig, args: argparse.Namespace) -> None:
    for attr, value in (
        ("slack.app_name", args.slack_app_name),
        ("slack.app_description", args.slack_app_description),
        ("slack.user_token", args.slack_user_token),
        ("slack.default_channel_id", args.default_channel_id),
        ("slack.agent_user_id", args.agent_user_id),
        ("runtime.max_concurrent_workers", args.max_concurrent_workers),
        ("runtime.session_minutes", args.session_minutes),
        ("worker.command", args.worker_command),
        ("worker.model", args.worker_model),
        ("worker.reasoning_effort", args.worker_reasoning_effort),
        ("worker.personality", args.worker_personality),
        ("worker.approval_policy", args.worker_approval_policy),
        ("worker.sandbox_mode", args.worker_sandbox_mode),
        ("worker.web_search", args.worker_web_search),
        ("worker.chatgpt_project", args.chatgpt_project),
        ("consult.command", args.consult_command),
        ("dev_review.command", args.dev_review_command),
        ("tribune.command", args.tribune_command),
        ("dashboard.export_dir", args.dashboard_export_dir),
        ("files.manifest_path", args.manifest_path),
    ):
        if value is not None:
            cfg.set(attr.replace("dev_review", "developer_review"), value)

    if args.consult_args is not None:
        cfg.consult.args = [part.strip() for part in args.consult_args.split(",") if part.strip()]
    if args.tribune_fallback_models is not None:
        cfg.tribune.fallback_models = [
            part.strip() for part in args.tribune_fallback_models.split(",") if part.strip()
        ]
    if args.dev_review_backend == "none":
        cfg.developer_review.enabled = False
        cfg.developer_review.backend = "none"
        cfg.developer_review.command = ""
    elif args.dev_review_backend == "claude":
        cfg.developer_review.enabled = True
        cfg.developer_review.backend = "claude"
    if args.tribune_enabled is not None:
        cfg.tribune.enabled = bool(args.tribune_enabled)
    if args.tribune_review_rounds is not None:
        cfg.tribune.review_rounds = args.tribune_review_rounds
    if args.tribune_maintenance_rounds is not None:
        cfg.tribune.maintenance_rounds = args.tribune_maintenance_rounds
    if args.dashboard_export_enabled is not None:
        cfg.dashboard.export_enabled = bool(args.dashboard_export_enabled)
    if args.agent_name is not None:
        cfg.agent.name = args.agent_name
    elif cfg.agent.name == DEFAULT_AGENT_NAME:
        inferred = infer_agent_name_from_app_name(cfg.slack.app_name)
        if inferred:
            cfg.agent.name = inferred

    if cfg.developer_review.backend == "none":
        cfg.developer_review.enabled = False
        cfg.developer_review.command = ""
    if cfg.tribune.enabled is False:
        cfg.tribune.review_rounds = 0
        cfg.tribune.maintenance_rounds = 0
    if cfg.tribune.review_rounds > 0 or cfg.tribune.maintenance_rounds > 0:
        cfg.tribune.enabled = True


def _interactive_configure(cfg: CanonicalConfig) -> CanonicalConfig:
    cfg.slack.app_name = _prompt_text("Slack app name", cfg.slack.app_name, allow_empty=False)
    cfg.slack.app_description = _prompt_text(
        "Slack app description",
        cfg.slack.app_description,
        allow_empty=False,
    )
    if cfg.agent.name == DEFAULT_AGENT_NAME:
        inferred = infer_agent_name_from_app_name(cfg.slack.app_name)
        if inferred:
            cfg.agent.name = inferred
    cfg.agent.name = _prompt_text("Agent name", cfg.agent.name, allow_empty=False)
    cfg.slack.user_token = _prompt_text("Slack user token", cfg.slack.user_token, allow_empty=False)
    cfg.slack.default_channel_id = _prompt_text(
        "Default channel ID",
        cfg.slack.default_channel_id,
        allow_empty=False,
    )
    cfg.slack.agent_user_id = _prompt_text(
        "Explicit agent user ID override",
        cfg.slack.agent_user_id,
    )
    cfg.runtime.max_concurrent_workers = _prompt_int(
        "Max concurrent workers",
        cfg.runtime.max_concurrent_workers,
    )
    cfg.worker.chatgpt_project = _prompt_text(
        "ChatGPT default project",
        cfg.worker.chatgpt_project,
        allow_empty=False,
    )
    cfg.worker.model = _prompt_text("Worker model", cfg.worker.model, allow_empty=False)
    cfg.worker.reasoning_effort = _prompt_text(
        "Worker reasoning effort",
        cfg.worker.reasoning_effort,
        allow_empty=False,
    )
    cfg.consult.command = _prompt_text("Consult command", cfg.consult.command)
    consult_args = _prompt_text("Consult args (comma-separated)", ",".join(cfg.consult.args))
    cfg.consult.args = [part.strip() for part in consult_args.split(",") if part.strip()]
    cfg.developer_review.backend = _prompt_choice(
        "Developer-review backend",
        cfg.developer_review.backend if cfg.developer_review.enabled else "none",
        ("claude", "none"),
    )
    cfg.developer_review.enabled = cfg.developer_review.backend != "none"
    if cfg.developer_review.enabled:
        cfg.developer_review.command = _prompt_text(
            "Developer-review command",
            cfg.developer_review.command,
            allow_empty=False,
        )
    else:
        cfg.developer_review.command = ""
    cfg.tribune.enabled = _prompt_bool("Enable Tribune review", cfg.tribune.enabled)
    if cfg.tribune.enabled:
        cfg.tribune.review_rounds = _prompt_int("Tribune review rounds", max(cfg.tribune.review_rounds, 1))
        cfg.tribune.maintenance_rounds = _prompt_int(
            "Tribune maintenance rounds",
            cfg.tribune.maintenance_rounds,
        )
        cfg.tribune.command = _prompt_text(
            "Tribune command",
            cfg.tribune.command,
            allow_empty=False,
        )
        fallback = _prompt_text(
            "Tribune fallback models (comma-separated)",
            ",".join(cfg.tribune.fallback_models),
            allow_empty=False,
        )
        cfg.tribune.fallback_models = [part.strip() for part in fallback.split(",") if part.strip()]
    else:
        cfg.tribune.review_rounds = 0
        cfg.tribune.maintenance_rounds = 0
    cfg.dashboard.export_enabled = _prompt_bool(
        "Enable dashboard export",
        cfg.dashboard.export_enabled,
    )
    if cfg.dashboard.export_enabled:
        cfg.dashboard.export_dir = _prompt_text(
            "Dashboard export dir",
            cfg.dashboard.export_dir,
            allow_empty=False,
        )
    return cfg


def _print_next_steps(cfg: CanonicalConfig) -> None:
    print("\nNext steps:")
    print(
        f"  1. Import {cfg.files.manifest_path} into https://api.slack.com/apps and install it under the agent's Slack account."
    )
    print("  2. Build mcp/slack-mcp-server if it is not already built.")
    print("  3. Start the supervisor with `murphy start`.")


def _has_error(findings: Iterable[DoctorFinding]) -> bool:
    return any(finding.level == "error" for finding in findings)


def run_init(args: argparse.Namespace) -> int:
    try:
        repo_root = resolve_repo_root(args.repo_root)
    except RepoRootNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        cfg, notes = _resolve_import_base(
            repo_root,
            prefer=args.prefer,
            interactive=not args.non_interactive,
        )
    except RuntimeError:
        return 1

    _apply_flag_overrides(cfg, args)
    if not args.non_interactive:
        cfg = _interactive_configure(cfg)

    save_canonical(cfg, canonical_path(repo_root))
    results = sync_projections(cfg, repo_root, force=args.force)
    findings = doctor_config(cfg, repo_root)

    if notes:
        print("\n".join(notes))
        print()
    print(format_projection_results(results))
    if findings:
        print("\nDoctor:")
        print(format_doctor_findings(findings))
    _print_next_steps(cfg)

    skipped = any(result.status == "skipped" for result in results)
    return 1 if skipped or _has_error(findings) else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser("murphy init")
    args = parser.parse_args(argv)
    return run_init(args)


if __name__ == "__main__":
    raise SystemExit(main())
