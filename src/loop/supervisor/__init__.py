"""Supervisor loop public API.

Keep this module free of eager imports from ``main``.
Importing ``src.loop.supervisor.main`` here preloads that module and triggers
``runpy`` warnings when the entrypoint is executed with ``python -m``.
"""

from .config import Config
from .runtime import Supervisor
from .utils import (
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


def resolve_loop_config_file():
    from .main import resolve_loop_config_file as _resolve_loop_config_file

    return _resolve_loop_config_file()


def check_deps():
    from .main import check_deps as _check_deps

    return _check_deps()


def run() -> int:
    from .main import main

    return main()

__all__ = [
    "Config",
    "Supervisor",
    "check_deps",
    "iso_from_ts_floor",
    "load_dotenv",
    "run",
    "now_ts",
    "parse_bool",
    "parse_conf_defaults",
    "resolve_default_expr",
    "resolve_loop_config_file",
    "timestamp_utc",
    "ts_gt",
    "ts_to_int",
]
