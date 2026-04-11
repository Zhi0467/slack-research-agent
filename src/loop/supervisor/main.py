#!/usr/bin/env python3
"""Entrypoint and compatibility exports for the supervisor loop."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .config import Config
from .runtime import Supervisor
from .utils import (
    CHANNEL_ID_RE,
    THREAD_TS_RE,
    TRANSIENT_PATTERN,
    iso_from_ts_floor,
    load_dotenv,
    now_ts,
    parse_bool,
    parse_conf_defaults,
    resolve_default_expr,
    timestamp_utc,
    ts_gt,
    ts_to_int,
)

DEFAULT_LOOP_CONFIG_FILE = Path("src/config/supervisor_loop.conf")


def resolve_loop_config_file() -> Path:
    configured = os.environ.get("LOOP_CONFIG_FILE")
    if configured:
        return Path(configured)
    return DEFAULT_LOOP_CONFIG_FILE


def check_deps(cfg: Config) -> None:
    required = [cfg.worker_cmd[0]] if cfg.worker_cmd else ["codex"]
    missing = [b for b in required if shutil.which(b) is None]
    if missing:
        raise RuntimeError(f"Missing required dependency: {', '.join(missing)}")
    optional: list[tuple[str, str, str]] = []
    if cfg.dev_review_cmd:
        optional.append((cfg.dev_review_cmd[0], "developer review", "dev_review_cmd"))
    if (cfg.tribune_max_review_rounds > 0 or cfg.tribune_maint_rounds > 0) and cfg.tribune_cmd:
        optional.append((cfg.tribune_cmd[0], "Tribune review", "tribune_cmd"))
    warned: set[str] = set()
    for binary, feature, attr in optional:
        if binary in warned or binary in required:
            continue
        warned.add(binary)
        if shutil.which(binary) is None:
            print(
                f"WARNING: optional dependency '{binary}' not found; disabling {feature}.",
                file=sys.stderr,
            )
            setattr(cfg, attr, [])
            if attr == "tribune_cmd":
                cfg.tribune_max_review_rounds = 0
                cfg.tribune_maint_rounds = 0


def main() -> int:
    os.chdir(Path(__file__).resolve().parents[3])
    load_dotenv(Path(".env"))

    loop_config_file = resolve_loop_config_file()
    if not loop_config_file.exists():
        print(f"Missing loop config file: {loop_config_file}", file=sys.stderr)
        return 1

    cfg = Config(loop_config_file)
    try:
        check_deps(cfg)
    except Exception as e:
        print(str(e), file=sys.stderr)
        return 1

    # Dashboard publisher is a standalone process since AGENT-044.
    print("NOTE: Dashboard publisher runs separately — start it with: ./scripts/dashboard.sh")

    sup = Supervisor(cfg)
    return sup.run()


__all__ = [
    "CHANNEL_ID_RE",
    "Config",
    "DEFAULT_LOOP_CONFIG_FILE",
    "Supervisor",
    "THREAD_TS_RE",
    "TRANSIENT_PATTERN",
    "check_deps",
    "iso_from_ts_floor",
    "load_dotenv",
    "main",
    "now_ts",
    "parse_bool",
    "parse_conf_defaults",
    "resolve_default_expr",
    "resolve_loop_config_file",
    "timestamp_utc",
    "ts_gt",
    "ts_to_int",
]


if __name__ == "__main__":
    sys.exit(main())
