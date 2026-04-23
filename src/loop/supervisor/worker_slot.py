#!/usr/bin/env python3
"""WorkerSlot: one concurrent worker's execution context.

Each slot owns a git worktree, a dispatch thread, and per-slot file paths.
Used by the parallel dispatch loop (_run_parallel) when MAX_CONCURRENT_WORKERS >= 2.

Workers are responsible for merging their own branch into the main repo at
task end (via ``git -C $REPO_ROOT merge $WORKER_BRANCH``).  The supervisor
no longer performs post-task merging.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

# Maximum seconds to wait for a worker thread to finish during collect().
# Prevents indefinite blocking on hung workers during restart drain.
# Reduced from 7200 (2h) → 600 (10min) after AGENT-028 incident where a hung
# worker blocked supervisor restart-drain for 2h15m.
COLLECT_JOIN_TIMEOUT = 600


class WorkerSlot:
    """Manages one parallel worker's worktree, subprocess, and lifecycle."""

    def __init__(
        self,
        slot_id: int,
        repo_root: Path,
        dispatch_dir: Path,
        outcomes_dir: Path,
        worktree_dir: Path,
        log_fn=None,
    ) -> None:
        self.slot_id = slot_id
        self.repo_root = repo_root
        self.worktree_path = worktree_dir / f"worker-{slot_id}"
        self.branch_name = f"worker-{slot_id}"
        # Capture the base branch once at init (the branch checked out in the
        # main repo).  Workers reset to this branch tip before each dispatch.
        self._base_branch = self._get_current_branch()

        # Per-slot dispatch files
        self.dispatch_task_file = dispatch_dir / f"worker-{slot_id}.task.json"
        self.dispatch_prompt_file = dispatch_dir / f"worker-{slot_id}.prompt.md"
        self.session_log_file = dispatch_dir / f"worker-{slot_id}.session.log"
        self.outcomes_dir = outcomes_dir

        # Per-dispatch state
        self.task_key: Optional[str] = None
        self.task_type: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._result: Optional[Tuple[int, str]] = None
        self.started_at: Optional[float] = None
        self.ended_at: Optional[float] = None

        self._log_fn = log_fn
        self._worktree_initialized = False

        # Watchdog state (set in start(), read from main thread)
        self._proc: Optional[subprocess.Popen] = None
        self._killed_reason: Optional[str] = None
        self._mcp_checked: bool = False
        self._is_privileged: bool = False

    # ---- State properties ----

    @property
    def is_idle(self) -> bool:
        return self._thread is None

    @property
    def is_busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_done(self) -> bool:
        return self._thread is not None and not self._thread.is_alive()

    @property
    def elapsed_sec(self) -> float:
        if self.started_at is None:
            return 0.0
        return time.time() - self.started_at

    # ---- Worktree management ----

    def setup_worktree(self) -> None:
        """Create worktree on first call; reset branch to main on subsequent calls."""
        if not self._worktree_initialized:
            self._create_worktree()
            self._worktree_initialized = True
        else:
            self._reset_worktree()

    def _create_worktree(self) -> None:
        """Create the git worktree if it doesn't exist, symlink shared dirs."""
        # Prune stale worktree locks
        self._git(["worktree", "prune"], cwd=self.repo_root)

        if self.worktree_path.exists():
            if not (self.worktree_path / ".git").exists():
                import shutil

                self._log(
                    f"worktree_orphan_dir_nuked slot={self.slot_id} "
                    f"path={self.worktree_path}"
                )
                shutil.rmtree(self.worktree_path, ignore_errors=True)
            else:
                # Worktree already exists (e.g. from a prior crash), just reset it.
                # Use _reset_worktree_impl to avoid mutual recursion.
                self._reset_worktree_impl()
                self._ensure_symlinks()
                return

        self.worktree_path.parent.mkdir(parents=True, exist_ok=True)

        # Delete stale branch if it exists (e.g. from a previous run that
        # removed the worktree but not the branch).
        try:
            self._git(["branch", "-D", self.branch_name], cwd=self.repo_root)
        except subprocess.CalledProcessError:
            pass  # Branch doesn't exist — fine

        # Create worktree with a dedicated branch
        self._git(
            ["worktree", "add", str(self.worktree_path), "-b", self.branch_name, "HEAD"],
            cwd=self.repo_root,
        )
        self._ensure_symlinks()

    def _reset_worktree(self) -> None:
        """Force-reset the worktree branch to current main tip.

        Refreshes _base_branch so each dispatch sees commits merged by
        earlier workers (not the stale branch tip from supervisor startup).
        """
        self._base_branch = self._get_current_branch()
        if not self.worktree_path.exists():
            # Worktree directory was deleted — recreate from scratch.
            self._worktree_initialized = False
            self._create_worktree()
            self._worktree_initialized = True
            return
        self._reset_worktree_impl()
        self._ensure_symlinks()

    def _reset_worktree_impl(self) -> None:
        """Reset the worktree branch to the base branch (no existence check, no recursion).

        Uses graduated recovery:
        1. Soft reset: reset --hard + clean + checkout -B (fast path)
        2. If soft reset fails: try merging stranded branch into main
        3. If merge fails: backup branch to refs/stranded/, nuke and recreate
        """
        base = self._base_branch
        try:
            self._git(["reset", "--hard", "HEAD"], cwd=self.worktree_path)
            self._git(["clean", "-fd"], cwd=self.worktree_path)
            self._git(["checkout", "-B", self.branch_name, base], cwd=self.worktree_path)
            self._git(["reset", "--hard", base], cwd=self.worktree_path)
            self._git(["clean", "-fd"], cwd=self.worktree_path)
        except subprocess.CalledProcessError as exc:
            self._log(
                f"worktree_soft_reset_failed slot={self.slot_id} "
                f"error={exc!s:.200}"
            )
            self._recover_stranded_worktree(base)

    def _recover_stranded_worktree(self, base: str) -> None:
        """Graduated recovery for a worktree whose branch diverged from main.

        Step 1: Try merging the stranded branch into main (preserves work).
        Step 2: If merge fails, backup branch to refs/stranded/ and nuke.
        """
        # Step 1: try merging stranded branch into main.
        # Guard: verify the root is actually on the expected base branch
        # before merging.  If not, skip straight to nuke to avoid merging
        # into an unrelated branch.
        actual = self._get_current_branch()
        if actual != base:
            self._log(
                f"worktree_merge_skipped slot={self.slot_id} "
                f"root_on={actual} expected={base}"
            )
            self._backup_and_nuke_worktree(base)
            return
        try:
            self._git(
                ["merge", self.branch_name, "--no-edit"],
                cwd=self.repo_root,
            )
            self._log(
                f"worktree_merge_recovery slot={self.slot_id} "
                f"branch={self.branch_name} merged_to={base}"
            )
            # Merge succeeded — retry soft reset (should work now)
            self._git(["reset", "--hard", "HEAD"], cwd=self.worktree_path)
            self._git(["clean", "-fd"], cwd=self.worktree_path)
            self._git(["checkout", "-B", self.branch_name, base], cwd=self.worktree_path)
            self._git(["reset", "--hard", base], cwd=self.worktree_path)
            self._git(["clean", "-fd"], cwd=self.worktree_path)
            return
        except subprocess.CalledProcessError:
            # Merge failed — abort it and fall through to nuke
            try:
                self._git(["merge", "--abort"], cwd=self.repo_root)
            except subprocess.CalledProcessError:
                pass  # No merge in progress — fine

        # Step 2: backup branch and nuke worktree
        self._backup_and_nuke_worktree(base)

    def _backup_and_nuke_worktree(self, base: str) -> None:
        """Backup stranded branch to refs/stranded/, remove and recreate worktree."""
        import shutil
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_ref = f"refs/stranded/{self.branch_name}-{ts}"

        # Backup the branch to a ref so commits are never lost
        try:
            self._git(
                ["update-ref", backup_ref, self.branch_name],
                cwd=self.repo_root,
            )
            self._log(
                f"worktree_branch_backed_up slot={self.slot_id} "
                f"branch={self.branch_name} ref={backup_ref}"
            )
        except subprocess.CalledProcessError as exc:
            self._log(
                f"worktree_backup_failed slot={self.slot_id} "
                f"error={exc!s:.200}"
            )

        # Nuke the worktree
        try:
            self._git(
                ["worktree", "remove", "--force", str(self.worktree_path)],
                cwd=self.repo_root,
            )
        except subprocess.CalledProcessError:
            # Manual cleanup if git worktree remove fails
            shutil.rmtree(self.worktree_path, ignore_errors=True)
            git_wt_dir = self.repo_root / ".git" / "worktrees" / self.branch_name
            shutil.rmtree(git_wt_dir, ignore_errors=True)

        self._git(["worktree", "prune"], cwd=self.repo_root)

        # Delete the stale branch so worktree add can recreate it
        try:
            self._git(["branch", "-D", self.branch_name], cwd=self.repo_root)
        except subprocess.CalledProcessError:
            pass

        # Recreate fresh
        self.worktree_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            ["worktree", "add", str(self.worktree_path), "-b", self.branch_name, base],
            cwd=self.repo_root,
        )
        self._ensure_symlinks()
        self._log(
            f"worktree_nuked_and_recreated slot={self.slot_id} "
            f"backup={backup_ref}"
        )

    def _ensure_symlinks(self) -> None:
        """Symlink shared directories into worktree; copy .codex/ with per-slot env.

        Symlinked directories:
        - .agent/ — shared state (memory/, runtime/, tasks/, projects/)
        - projects/*/ — submodule working dirs (shares gitignored runtime
          artifacts like datasets, venvs, experiment outputs across workers)

        mcp/*/ is NOT symlinked: binaries are referenced by absolute path
        so worktree copies are unused at runtime, and symlinking them
        breaks ``git status`` in the worktree.

        Submodule dirs are safe to symlink because git reset --hard does not
        recurse into submodules.  Only directories are symlinked — regular
        files (e.g. projects/*.md) are left as worktree-local checkouts.
        """
        # Guard against catastrophic circular symlinks if worktree == repo root
        if self.worktree_path.resolve() == self.repo_root.resolve():
            self._log(
                f"CRITICAL: worktree_path == repo_root ({self.worktree_path}), "
                f"skipping symlink creation to prevent circular symlinks"
            )
            return
        import shutil

        # Collect all directories to symlink: .agent/ + submodule dirs
        symlink_pairs: list[tuple[Path, Path]] = []

        # .agent/ — shared state
        agent_src = self.repo_root / ".agent"
        if agent_src.exists():
            symlink_pairs.append((agent_src, self.worktree_path / ".agent"))

        # projects/*/ — submodule directories only (skip files).
        # Shares gitignored runtime artifacts (datasets, venvs, outputs).
        # NOTE: mcp/*/ is intentionally NOT symlinked.  MCP binaries are
        # referenced by absolute path so worktree copies are unused at
        # runtime, and symlinking them breaks `git status` in the worktree
        # ("expected submodule path … not to be a symbolic link").
        for submod_parent in ("projects",):
            parent_src = self.repo_root / submod_parent
            if not parent_src.is_dir():
                continue
            parent_dst = self.worktree_path / submod_parent
            parent_dst.mkdir(parents=True, exist_ok=True)
            for child in sorted(parent_src.iterdir()):
                if not child.is_dir():
                    continue
                symlink_pairs.append((child, parent_dst / child.name))

        for src, dst in symlink_pairs:
            if dst.exists() or dst.is_symlink():
                if dst.is_symlink() and dst.resolve() == src.resolve():
                    continue
                if dst.is_symlink():
                    dst.unlink()
                else:
                    shutil.rmtree(str(dst), ignore_errors=True)
            dst.symlink_to(src)

        # Mark symlinked entries as skip-worktree so ``git status`` in the
        # worktree doesn't report phantom deletions or reject symlinked paths.
        # Cleared by ``git reset --hard`` in _reset_worktree_impl(), but
        # re-applied here on each call.
        #
        # Two categories:
        # 1. projects/*/ submodule gitlink entries — prevents "expected
        #    submodule path … not to be a symbolic link" errors.
        # 2. .agent/ tracked files — the .agent symlink makes all tracked
        #    files under it invisible to git's worktree check, causing
        #    spurious "deleted" status for every tracked .agent/ file.
        skip_paths = [
            str(dst.relative_to(self.worktree_path))
            for _src, dst in symlink_pairs
            if str(dst.relative_to(self.worktree_path)).startswith("projects/")
        ]
        # Enumerate tracked files under .agent/ and mark them skip-worktree
        # so the symlinked .agent directory doesn't produce phantom deletions.
        if (self.worktree_path / ".agent").is_symlink():
            try:
                result = self._git(
                    ["ls-files", ".agent/"],
                    cwd=self.worktree_path,
                )
                agent_files = [
                    f for f in (result.stdout or "").strip().split("\n") if f
                ]
                skip_paths.extend(agent_files)
            except subprocess.CalledProcessError:
                pass  # Non-fatal — git status will be noisy but functional
        if skip_paths:
            try:
                self._git(
                    ["update-index", "--skip-worktree", "--"] + skip_paths,
                    cwd=self.worktree_path,
                )
            except subprocess.CalledProcessError as exc:
                log.warning(
                    "skip-worktree failed for symlinked paths in slot %d: %s",
                    self.slot_id, exc.stderr if exc.stderr else exc,
                )

        # .codex/ is COPIED (not symlinked) so we can inject per-slot
        # CONSULT_SLOT_ID into the MCP server env for tab isolation.
        codex_src = self.repo_root / ".codex"
        codex_dst = self.worktree_path / ".codex"
        if codex_src.exists():
            if codex_dst.is_symlink():
                codex_dst.unlink()
            if codex_dst.exists():
                shutil.rmtree(str(codex_dst), ignore_errors=True)
            shutil.copytree(str(codex_src), str(codex_dst))
            self._inject_slot_env(codex_dst / "config.toml")

    def _inject_slot_env(self, config_path: Path) -> None:
        """Add CONSULT_SLOT_ID to [mcp_servers.consult.env] in config.toml."""
        if not config_path.exists():
            return
        text = config_path.read_text(encoding="utf-8")
        marker = "[mcp_servers.consult.env]"
        if marker not in text:
            return
        inject_line = f'CONSULT_SLOT_ID = "{self.slot_id}"'
        if "CONSULT_SLOT_ID" in text:
            return  # Already injected
        text = text.replace(marker, f"{marker}\n{inject_line}")
        config_path.write_text(text, encoding="utf-8")

    def inject_task_env(self, task_key: str) -> None:
        """Inject per-task consult env vars into the copied .codex/config.toml.

        Called before each dispatch (after setup_worktree which copies .codex/).
        Writes CONSULT_TASK_ID and CONSULT_HISTORY_DIR so the consult MCP
        server can persist conversation history.
        """
        import re as _re

        config_path = self.worktree_path / ".codex" / "config.toml"
        if not config_path.exists():
            return
        text = config_path.read_text(encoding="utf-8")
        marker = "[mcp_servers.consult.env]"
        if marker not in text:
            return
        history_dir = str(self.repo_root / ".agent" / "runtime" / "consult_history")
        for var_name, var_value in [
            ("CONSULT_TASK_ID", task_key),
            ("CONSULT_HISTORY_DIR", history_dir),
        ]:
            line = f'{var_name} = "{var_value}"'
            if var_name in text:
                text = _re.sub(
                    rf'^{var_name}\s*=\s*"[^"]*"',
                    line,
                    text,
                    flags=_re.MULTILINE,
                )
            else:
                text = text.replace(marker, f"{marker}\n{line}")
        config_path.write_text(text, encoding="utf-8")

    def rewrite_consult_binary_path(self, binary_path: str) -> None:
        """Rewrite the consult MCP command in the copied config.toml."""
        config_path = self.worktree_path / ".codex" / "config.toml"
        if not config_path.exists():
            return
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        in_consult = False
        rewritten = False
        out: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                in_consult = stripped == "[mcp_servers.consult]"
            if in_consult and not rewritten and re.match(r'^command\s*=\s*"', stripped):
                out.append(
                    re.sub(
                        r'^(command\s*=\s*)"[^"]*"',
                        rf'\1"{binary_path}"',
                        line,
                    )
                )
                rewritten = True
            else:
                out.append(line)
        config_path.write_text("".join(out), encoding="utf-8")

    def disable_consult_mcp(self) -> None:
        """Remove the entire consult MCP section from the copied config."""
        config_path = self.worktree_path / ".codex" / "config.toml"
        if not config_path.exists():
            return
        lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
        out: list[str] = []
        in_consult = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("["):
                in_consult = (
                    stripped == "[mcp_servers.consult]"
                    or stripped.startswith("[mcp_servers.consult.")
                )
            if not in_consult:
                out.append(line)
        config_path.write_text("".join(out), encoding="utf-8")

    # ---- Subprocess dispatch ----

    def start(
        self,
        cmd: list[str],
        prompt: str,
        task_key: str,
        task_type: str,
        timeout_sec: int,
        is_privileged: bool = False,
    ) -> None:
        """Launch the worker subprocess in a background thread.

        Output is streamed to session_log_file (absorbs AGENT-003: no buffering).
        When *is_privileged* is True (developer review), the pre-commit write
        protection (AGENT_WORKER=1) is skipped.
        """
        if self._thread is not None:
            state = "busy" if self.is_busy else "done (uncollected)"
            raise RuntimeError(
                f"Slot {self.slot_id} already has a worker ({state}); "
                f"call collect() + reset() first"
            )

        self.task_key = task_key
        self.task_type = task_type
        self._result = None
        self.started_at = time.time()
        self._is_privileged = is_privileged

        # Reset watchdog state
        self._killed_reason = None
        self._mcp_checked = False

        # Ensure dispatch dir exists
        self.session_log_file.parent.mkdir(parents=True, exist_ok=True)

        self._thread = threading.Thread(
            target=self._run_subprocess,
            args=(cmd, prompt, timeout_sec),
            name=f"worker-{self.slot_id}",
            daemon=True,
        )
        self._thread.start()
        self._log(f"slot_start slot={self.slot_id} task={task_key} type={task_type}")

    def _run_subprocess(
        self, cmd: list[str], prompt: str, timeout_sec: int
    ) -> None:
        """Run the worker subprocess, streaming output to the log file.

        Uses Popen (not subprocess.run) so the main thread can call
        kill_worker() via self._proc.
        """
        exit_code = -1
        # Pass slot ID so the consult MCP server opens a dedicated Chrome tab.
        # REPO_ROOT and WORKER_BRANCH let the worker merge its branch at task end.
        env = {
            k: v for k, v in os.environ.items() if k != "CLAUDECODE"
        }
        env.update({
            "CONSULT_SLOT_ID": str(self.slot_id),
            "CONSULT_TASK_ID": self.task_key or "",
            "CONSULT_HISTORY_DIR": str(self.repo_root / ".agent" / "runtime" / "consult_history"),
            "REPO_ROOT": str(self.repo_root),
            "WORKER_BRANCH": self.branch_name,
        })
        # Write protection: AGENT_WORKER=1 blocks source edits via pre-commit hook.
        # Only developer review is privileged (can edit source).
        if not self._is_privileged:
            env["AGENT_WORKER"] = "1"
        # Disable Gemini sandbox for Tribune phases.
        if cmd and "gemini" in cmd[0]:
            env["GEMINI_SANDBOX"] = "false"
        try:
            with open(self.session_log_file, "w", encoding="utf-8") as log_fh:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    text=True,
                    cwd=str(self.worktree_path),
                    env=env,
                )
                self._proc = proc  # Expose to main thread for kill_worker()
                try:
                    if prompt:
                        proc.stdin.write(prompt)
                    proc.stdin.close()
                    proc.wait(timeout=timeout_sec)
                    exit_code = proc.returncode
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        pass  # process didn't exit after kill; proceed with code 124
                    exit_code = 124
        except Exception as exc:
            self._log(f"slot_error slot={self.slot_id} error={exc}")
            # Ensure process is cleaned up if it was started
            if self._proc is not None:
                try:
                    self._proc.kill()
                    self._proc.wait(timeout=10)
                except Exception:
                    pass
            exit_code = 1
        finally:
            self._proc = None

        self.ended_at = time.time()  # AGENT-025: subprocess exit timestamp
        self._result = (exit_code, str(self.session_log_file))
        self._log(f"slot_done slot={self.slot_id} task={self.task_key} exit={exit_code}")

    def collect(self, timeout: int = COLLECT_JOIN_TIMEOUT) -> Tuple[int, str]:
        """Wait for the worker thread to finish and return (exit_code, log_path).

        Args:
            timeout: Maximum seconds to wait for the thread. Defaults to
                COLLECT_JOIN_TIMEOUT (2 hours). Raises RuntimeError on timeout.
        """
        if self._thread is None:
            raise RuntimeError(f"Slot {self.slot_id} has no active worker to collect")
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            raise RuntimeError(
                f"Slot {self.slot_id} worker did not finish within {timeout}s"
            )
        result = self._result or (-1, str(self.session_log_file))
        return result

    def reset(self) -> None:
        """Clear per-dispatch state so the slot can be reused."""
        self.task_key = None
        self.task_type = None
        self._thread = None
        self._result = None
        self.started_at = None
        self._proc = None
        self._killed_reason = None
        self._mcp_checked = False

    # ---- Watchdog ----

    @property
    def killed_reason(self) -> Optional[str]:
        """Return the reason the worker was killed by the watchdog, if any."""
        return self._killed_reason

    def kill_worker(self, reason: str) -> bool:
        """Send SIGTERM then SIGKILL to the worker process.

        Returns True if a process was signalled, False if already exited.
        Thread-safe: POSIX kill() is safe from any thread.
        """
        self._killed_reason = reason
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return False
        self._log(
            f"watchdog_kill slot={self.slot_id} task={self.task_key} reason={reason}"
        )
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        except (OSError, ProcessLookupError):
            pass  # Process already exited
        return True

    def check_mcp_startup(self, grace_sec: int = 30) -> Optional[str]:
        """Check session log for MCP startup failure patterns.

        Called once per dispatch, after grace_sec has elapsed.
        Returns a failure reason string if detected, None otherwise.

        Only checks the session header (before the user prompt) to avoid
        false positives from task text that mentions MCP-related keywords.
        """
        if self._mcp_checked:
            return None
        if self.started_at is None or time.time() - self.started_at < grace_sec:
            return None

        self._mcp_checked = True

        try:
            with open(self.session_log_file, "r", encoding="utf-8", errors="replace") as f:
                head = f.read(8192)
        except (FileNotFoundError, OSError):
            return None

        # Only check the session header — content before the user prompt.
        # MCP initialization errors appear in the worker's startup output,
        # not inside the rendered prompt.  Checking the full head causes
        # false positives when task text contains MCP-related keywords
        # (e.g. "mcp chrome" + "MCP server" + "FAILURE" on one JSON line).
        for marker in ("\nuser\n", "\nuser ", "\n---\nuser"):
            idx = head.find(marker)
            if idx >= 0:
                head = head[:idx]
                break

        from .utils import MCP_STARTUP_FAILURE_PATTERN
        match = MCP_STARTUP_FAILURE_PATTERN.search(head)
        if match:
            return f"mcp_startup_failed: {match.group(0)[:120]}"
        return None

    # Patterns for detecting MCP tool call boundaries in session logs.
    # Codex writes "tool <ns>.<method>(...)" before a call and
    # "<ns>.<method>(...) success in ..." when it returns.
    _TOOL_START_RE = re.compile(r"^tool \w+\.\w+\(")
    _TOOL_END_RE = re.compile(r"\) success in \d|succeeded in \d")

    def _detect_tool_in_flight(self) -> bool:
        """Check if the worker is currently inside an MCP tool call.

        Reads the last 32 KB of the session log and scans lines in reverse
        for the most recent tool boundary.  Returns True if the last
        boundary is a tool *start* (no matching completion found after it).

        Fails safe: any I/O or decode error returns False (short timeout).
        """
        try:
            with open(self.session_log_file, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                read_size = min(size, 32768)
                if read_size == 0:
                    return False
                f.seek(-read_size, 2)
                tail = f.read().decode("utf-8", errors="replace")
        except (FileNotFoundError, OSError):
            return False

        for line in reversed(tail.splitlines()):
            stripped = line.strip()
            if not stripped:
                continue
            if self._TOOL_END_RE.search(stripped):
                return False  # Last tool completed; not in a call
            if self._TOOL_START_RE.match(stripped):
                return True  # Unmatched tool start; call in flight
        return False

    def check_session_log_stale(
        self,
        idle_timeout_sec: int = 900,
        tool_timeout_sec: int = 14400,
    ) -> Optional[str]:
        """Check if the session log hasn't been written to for idle_timeout_sec.

        Returns a reason string if stale, None otherwise.
        Only meaningful for busy workers that have been running long enough
        for the idle timeout to have elapsed since start.

        When a tool call is detected in flight (via session log tail),
        the longer *tool_timeout_sec* ceiling is used instead, so that
        legitimate long MCP calls (e.g. Athena consults) are not killed.
        """
        if self.started_at is None:
            return None
        elapsed = time.time() - self.started_at
        if elapsed < idle_timeout_sec:
            return None  # Worker hasn't run long enough for staleness check
        try:
            mtime = self.session_log_file.stat().st_mtime
        except (FileNotFoundError, OSError):
            return None  # File doesn't exist yet
        stale_sec = time.time() - mtime
        if stale_sec < idle_timeout_sec:
            return None  # Recently active — not stale

        # Log is stale beyond idle threshold.  Check if a tool call is in
        # flight — if so, apply the longer tool timeout instead.
        if self._detect_tool_in_flight():
            if stale_sec >= tool_timeout_sec:
                return (
                    f"tool_call_stale: no output for {int(stale_sec)}s "
                    f"(tool threshold={tool_timeout_sec}s)"
                )
            return None  # Tool in flight, under tool timeout — not stale

        return (
            f"session_log_stale: no output for {int(stale_sec)}s "
            f"(threshold={idle_timeout_sec}s)"
        )

    # ---- Outcome path ----

    def outcome_file_for(self, task_key: str) -> Path:
        """Return the per-task outcome file path."""
        return self.outcomes_dir / f"{task_key}.json"

    # ---- Helpers ----

    def _git(
        self,
        args: list[str],
        cwd: Path,
        timeout: int = 60,
    ) -> subprocess.CompletedProcess:
        """Run a git command, raising on failure."""
        full_cmd = ["git"] + args
        return subprocess.run(
            full_cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )

    def _log(self, msg: str) -> None:
        if self._log_fn:
            self._log_fn(msg)
        else:
            log.info(msg)

    def _get_current_branch(self) -> str:
        """Return the branch currently checked out in the main repo.

        Falls back to ``"main"`` when the root checkout is detached or
        on a ``worker-*`` branch (which can happen after a failed
        worktree recovery leaves the root on a stale worker branch).
        """
        try:
            result = self._git(
                ["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo_root
            )
            branch = result.stdout.strip()
            if not branch or branch == "HEAD":
                return "main"
            # Worker branches are never a valid base — fall back to main.
            if branch.startswith("worker-"):
                self._log(
                    f"base_branch_guard root_on_worker_branch={branch} "
                    f"falling_back_to=main"
                )
                return "main"
            return branch
        except Exception:
            return "main"
