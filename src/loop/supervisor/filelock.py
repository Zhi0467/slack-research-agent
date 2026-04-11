"""Advisory file locking for shared .agent/ files.

Provides ``agent_file_lock`` (context manager) and ``locked_append`` for
coordinating concurrent writes from multiple worker processes and the
supervisor.  Uses ``fcntl.flock`` (POSIX advisory locks) — the same
mechanism already protecting ``state.json``.

Lock files live beside targets: ``<name>.lock`` in the same directory.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover
    fcntl = None


@contextmanager
def agent_file_lock(target_path: Path) -> Iterator[None]:
    """Advisory exclusive lock for any shared file.

    Creates ``<target_path>.lock`` beside the target.  Tries non-blocking
    first; on contention falls back to a blocking wait (with an optional
    stderr hint so workers know they are waiting).
    """
    lock_path = target_path.parent / (target_path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lockf:
        if fcntl is not None:
            try:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print(
                    f"filelock: waiting for lock on {target_path.name} ...",
                    file=sys.stderr,
                )
                fcntl.flock(lockf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lockf.fileno(), fcntl.LOCK_UN)


def locked_append(target_path: Path, text: str) -> None:
    """Append *text* to *target_path* under advisory lock.

    Ensures trailing newline so consecutive appends never merge lines.
    Creates parent directories if needed.
    """
    with agent_file_lock(target_path):
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with target_path.open("a", encoding="utf-8") as f:
            f.write(text if text.endswith("\n") else text + "\n")
