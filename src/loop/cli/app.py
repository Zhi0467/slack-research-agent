"""Top-level unified Murphy CLI."""

from __future__ import annotations

import argparse
import json
import sys

from src import __version__ as PACKAGE_VERSION

from src.loop import bootstrap

from .canonical import (
    canonical_path,
    doctor_config,
    dump_canonical_toml,
    effective_runtime_view,
    existing_canonical_path,
    format_doctor_findings,
    format_import_conflicts,
    format_projection_results,
    import_existing_install,
    load_canonical,
    save_canonical,
    sync_projections,
)
from .common import resolve_repo_root_from_args
from .lifecycle import add_lifecycle_subcommands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="murphy",
        description="Unified setup, config, and lifecycle CLI for Murphy.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"murphy-agent {PACKAGE_VERSION}",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Onboard a Murphy checkout.")
    bootstrap.add_init_arguments(init_parser)
    init_parser.set_defaults(func=bootstrap.run_init)

    config_parser = subparsers.add_parser("config", help="Inspect and manage canonical local config.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")

    show_parser = config_subparsers.add_parser("show", help="Show canonical config.")
    show_parser.add_argument("--repo-root", help="Explicit Murphy repo root.")
    show_parser.add_argument(
        "--effective",
        action="store_true",
        help="Show effective runtime settings after defaults are applied.",
    )
    show_parser.set_defaults(func=command_config_show)

    set_parser = config_subparsers.add_parser("set", help="Set a canonical config value.")
    set_parser.add_argument("--repo-root", help="Explicit Murphy repo root.")
    set_parser.add_argument("key", help="Dotted canonical config key, e.g. worker.model.")
    set_parser.add_argument("value", help="New value.")
    set_parser.set_defaults(func=command_config_set)

    unset_parser = config_subparsers.add_parser("unset", help="Reset a canonical config value.")
    unset_parser.add_argument("--repo-root", help="Explicit Murphy repo root.")
    unset_parser.add_argument("key", help="Dotted canonical config key to reset.")
    unset_parser.set_defaults(func=command_config_unset)

    doctor_parser = config_subparsers.add_parser("doctor", help="Validate the local setup.")
    doctor_parser.add_argument("--repo-root", help="Explicit Murphy repo root.")
    doctor_parser.set_defaults(func=command_config_doctor)

    sync_parser = config_subparsers.add_parser("sync", help="Regenerate projections from canonical config.")
    sync_parser.add_argument("--repo-root", help="Explicit Murphy repo root.")
    sync_parser.add_argument("--force", action="store_true", help="Overwrite generated files.")
    sync_parser.add_argument(
        "--import-existing",
        action="store_true",
        help="If canonical config is missing, import local files into a new root config.toml first.",
    )
    sync_parser.add_argument(
        "--prefer",
        choices=("env", "codex", "claude"),
        help="Preferred source when importing conflicting existing files.",
    )
    sync_parser.set_defaults(func=command_config_sync)

    add_lifecycle_subcommands(subparsers)
    return parser


def _load_cfg(repo_root, *, allow_import: bool = False, prefer: str | None = None):
    cfg_path = existing_canonical_path(repo_root)
    if cfg_path is not None:
        cfg, warnings = load_canonical(cfg_path)
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
        return cfg
    if allow_import:
        imported = import_existing_install(repo_root, prefer=prefer)
        if imported.conflicts and not prefer:
            print("Existing local files disagree. Re-run with --prefer env|codex|claude.")
            print(format_import_conflicts(imported.conflicts))
            return None
        if imported.conflicts and prefer:
            print(format_import_conflicts(imported.conflicts))
        save_canonical(imported.config, cfg_path)
        return imported.config
    print(
        f"canonical config not found at {canonical_path(repo_root)}. Run `murphy init` or `murphy config sync --import-existing`.",
        file=sys.stderr,
    )
    return None


def command_config_show(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root_from_args(args)
    if repo_root is None:
        return 1
    cfg = _load_cfg(repo_root)
    if cfg is None:
        return 1
    if args.effective:
        print(json.dumps(effective_runtime_view(cfg, repo_root), indent=2))
    else:
        print(dump_canonical_toml(cfg), end="")
    return 0


def command_config_set(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root_from_args(args)
    if repo_root is None:
        return 1
    cfg = _load_cfg(repo_root)
    if cfg is None:
        return 1
    try:
        cfg.set(args.key, args.value)
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_canonical(cfg, canonical_path(repo_root))
    results = sync_projections(cfg, repo_root, force=True)
    print(format_projection_results(results))
    return 0


def command_config_unset(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root_from_args(args)
    if repo_root is None:
        return 1
    cfg = _load_cfg(repo_root)
    if cfg is None:
        return 1
    try:
        cfg.unset(args.key)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    save_canonical(cfg, canonical_path(repo_root))
    results = sync_projections(cfg, repo_root, force=True)
    print(format_projection_results(results))
    return 0


def command_config_doctor(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root_from_args(args)
    if repo_root is None:
        return 1
    cfg = _load_cfg(repo_root)
    if cfg is None:
        return 1
    findings = doctor_config(cfg, repo_root)
    if findings:
        print(format_doctor_findings(findings))
    else:
        print("No issues found.")
    return 1 if any(f.level == "error" for f in findings) else 0


def command_config_sync(args: argparse.Namespace) -> int:
    repo_root = resolve_repo_root_from_args(args)
    if repo_root is None:
        return 1
    cfg = _load_cfg(repo_root, allow_import=args.import_existing, prefer=args.prefer)
    if cfg is None:
        return 1
    results = sync_projections(cfg, repo_root, force=args.force)
    print(format_projection_results(results))
    skipped = any(result.status == "skipped" for result in results)
    return 1 if skipped else 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 1
    return args.func(args)
