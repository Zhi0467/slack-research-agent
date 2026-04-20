"""Shared helpers for Murphy CLI commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


class RepoRootNotFoundError(RuntimeError):
    """Raised when a Murphy repo root cannot be resolved."""


def is_repo_root(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (path / "scripts/run.sh").is_file()


def resolve_repo_root(explicit: Optional[str] = None, *, start: Optional[Path] = None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if not is_repo_root(candidate):
            raise RepoRootNotFoundError(
                f"repo root does not look like a Murphy checkout: {candidate}"
            )
        return candidate

    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if is_repo_root(candidate):
            return candidate
    raise RepoRootNotFoundError(
        "could not find Murphy repo root from the current directory; pass --repo-root"
    )


def resolve_repo_root_from_args(args: argparse.Namespace) -> Optional[Path]:
    try:
        return resolve_repo_root(getattr(args, "repo_root", None))
    except RepoRootNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return None
