#!/usr/bin/env python3
"""Shell adapter for async job execution.

Implements Plan 31 Phase 3: launches shell commands as background processes
with process-group management, log capture, and event generation.

The adapter is driven by the supervisor main loop — ``poll_all()`` checks
running processes and appends events to the job store when they complete
or fail.  Workers never interact with this module directly; they request
background jobs via their outcome payload and receive wakeup context on
the next dispatch.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from .job_store import (
    MATERIAL_EVENT_KINDS,
    JobEvent,
    JobRecord,
    JobStore,
)


@dataclass
class _RunningProcess:
    """In-memory handle for a background shell process."""

    job_id: str
    proc: subprocess.Popen
    log_path: Path
    log_fh: object  # open file handle for stdout/stderr


class ShellAdapter:
    """Manages background shell processes and generates job events.

    Usage::

        adapter = ShellAdapter(job_store)
        job = adapter.start("pip install -r requirements.txt",
                            task_id="1234", thread_ts="1234")
        # ... later, in the supervisor poll loop ...
        completed = adapter.poll_all()
        # completed is a list of job_ids that finished this cycle

    The adapter uses ``os.setpgrp`` so each subprocess runs in its own
    process group. ``cancel()`` sends ``SIGTERM`` to the entire group,
    then ``SIGKILL`` after a grace period.
    """

    def __init__(self, store: JobStore) -> None:
        self._store = store
        self._running: Dict[str, _RunningProcess] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        command: str,
        task_id: str,
        thread_ts: str,
        origin_turn_id: str = "",
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        timeout_sec: Optional[int] = None,
    ) -> JobRecord:
        """Launch *command* in the background and return the job record.

        The command is run via ``/bin/sh -c`` in a new process group.
        Stdout and stderr are merged into ``<logs_dir>/<job_id>.log``.
        """
        job_id = f"job_{int(time.time())}_{os.getpid()}_{len(self._running)}"
        log_path = self._store.logs_dir / f"{job_id}.log"

        log_fh = open(log_path, "w", encoding="utf-8")

        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)

        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", command],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                env=merged_env,
                start_new_session=True,  # setsid → own process group
            )
        except OSError as exc:
            log_fh.close()
            # Create a failed job record immediately
            job = JobRecord(
                job_id=job_id,
                task_id=task_id,
                thread_ts=thread_ts,
                origin_turn_id=origin_turn_id,
                adapter="shell",
                adapter_handle={"command": command},
                runtime_state="failed",
            )
            self._store.create_job(job)
            seq = self._store.next_seq(job_id)
            self._store.append_event(JobEvent(
                job_id=job_id,
                seq=seq,
                kind="job_failed",
                summary=f"Failed to start: {exc}",
                runtime_state_after="failed",
                requires_attention=True,
                source_event_key=f"shell:{job_id}:start_error",
            ))
            return job

        job = JobRecord(
            job_id=job_id,
            task_id=task_id,
            thread_ts=thread_ts,
            origin_turn_id=origin_turn_id,
            adapter="shell",
            adapter_handle={
                "command": command,
                "pid": proc.pid,
                "pgid": os.getpgid(proc.pid),
            },
            runtime_state="running",
        )
        if timeout_sec is not None:
            job.adapter_handle["timeout_sec"] = timeout_sec
            job.adapter_handle["deadline"] = (
                time.time() + timeout_sec
            )

        self._store.create_job(job)

        # Append job_started event (non-material — doesn't wake workers)
        seq = self._store.next_seq(job_id)
        self._store.append_event(JobEvent(
            job_id=job_id,
            seq=seq,
            kind="job_started",
            summary=f"Started: {command[:120]}",
            runtime_state_after="running",
            requires_attention=False,
            source_event_key=f"shell:{job_id}:started",
        ))

        self._running[job_id] = _RunningProcess(
            job_id=job_id,
            proc=proc,
            log_path=log_path,
            log_fh=log_fh,
        )
        return job

    def poll_all(self) -> list[str]:
        """Check all running processes. Returns list of job_ids that finished.

        For each completed process, appends a ``job_completed`` or
        ``job_failed`` event and updates the job record.  Also checks
        for timeout expiry.
        """
        finished: list[str] = []
        # Snapshot keys to allow dict mutation during iteration
        for job_id in list(self._running):
            rp = self._running[job_id]
            rc = rp.proc.poll()

            # Check timeout before checking exit
            job = self._store.load_job(job_id)
            if job and rc is None:
                deadline = (job.adapter_handle or {}).get("deadline")
                if deadline is not None and time.time() > deadline:
                    self._kill_process_group(rp, reason="timeout")
                    rc = rp.proc.poll()
                    if rc is None:
                        # Force kill
                        self._kill_process_group(rp, force=True)
                        try:
                            rp.proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            pass
                        rc = rp.proc.returncode or -9

                    self._finish_job(
                        job_id, rp, "failed", rc,
                        event_kind="job_timeout",
                        summary=f"Timed out after {job.adapter_handle.get('timeout_sec', '?')}s",
                    )
                    finished.append(job_id)
                    continue

            if rc is not None:
                # Process exited
                if rc == 0:
                    self._finish_job(
                        job_id, rp, "succeeded", rc,
                        event_kind="job_completed",
                        summary="Completed successfully",
                    )
                else:
                    self._finish_job(
                        job_id, rp, "failed", rc,
                        event_kind="job_failed",
                        summary=f"Exited with code {rc}",
                    )
                finished.append(job_id)

        return finished

    def cancel(self, job_id: str, grace_sec: int = 5) -> bool:
        """Cancel a running job. Returns True if the job was running.

        Sends SIGTERM to the process group, waits *grace_sec*, then
        SIGKILL if still alive.
        """
        rp = self._running.get(job_id)
        if rp is None:
            return False

        self._kill_process_group(rp)
        try:
            rp.proc.wait(timeout=grace_sec)
        except subprocess.TimeoutExpired:
            self._kill_process_group(rp, force=True)
            try:
                rp.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

        rc = rp.proc.returncode or -15
        self._finish_job(
            job_id, rp, "failed", rc,
            event_kind="job_failed",
            summary="Cancelled by supervisor",
        )
        return True

    def recover_lost_jobs(self) -> list[str]:
        """Scan job registry for jobs marked 'running' that have no live process.

        Called at supervisor startup to detect jobs lost due to crash/restart.
        Returns list of job_ids that were marked lost.
        """
        lost: list[str] = []
        for job in self._store.list_jobs():
            if job.runtime_state != "running":
                continue
            if job.job_id in self._running:
                continue
            # Check if the PID is still alive
            pid = (job.adapter_handle or {}).get("pid")
            if pid and self._pid_alive(pid):
                # Process still running but we don't have a handle.
                # This shouldn't happen in normal operation; skip it.
                continue
            # Mark as lost
            job.runtime_state = "lost"
            self._store.save_job(job)
            seq = self._store.next_seq(job.job_id)
            self._store.append_event(JobEvent(
                job_id=job.job_id,
                seq=seq,
                kind="job_lost",
                summary="Process not found after supervisor restart",
                runtime_state_after="lost",
                requires_attention=True,
                source_event_key=f"shell:{job.job_id}:lost",
            ))
            lost.append(job.job_id)
        return lost

    @property
    def running_count(self) -> int:
        """Number of currently tracked running processes."""
        return len(self._running)

    def log_tail(self, job_id: str, lines: int = 20) -> str:
        """Return the last *lines* lines from a job's log file."""
        log_path = self._store.logs_dir / f"{job_id}.log"
        if not log_path.exists():
            return ""
        try:
            content = log_path.read_text(encoding="utf-8", errors="replace")
            all_lines = content.splitlines()
            return "\n".join(all_lines[-lines:])
        except OSError:
            return ""

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _finish_job(
        self,
        job_id: str,
        rp: _RunningProcess,
        runtime_state: str,
        exit_code: int,
        event_kind: str,
        summary: str,
    ) -> None:
        """Record completion, close log, remove from running set."""
        # Close log file handle
        try:
            rp.log_fh.close()
        except Exception:
            pass

        # Get log size for cursor
        log_cursor = 0
        try:
            log_cursor = rp.log_path.stat().st_size
        except OSError:
            pass

        # Update job record
        job = self._store.load_job(job_id)
        if job:
            job.runtime_state = runtime_state
            job.adapter_handle["exit_code"] = exit_code
            job.last_log_cursor = log_cursor
            self._store.save_job(job)

        # Append completion event
        seq = self._store.next_seq(job_id)
        self._store.append_event(JobEvent(
            job_id=job_id,
            seq=seq,
            kind=event_kind,
            summary=summary,
            runtime_state_after=runtime_state,
            requires_attention=event_kind in MATERIAL_EVENT_KINDS,
            log_cursor=log_cursor,
            source_event_key=f"shell:{job_id}:exit:{exit_code}",
        ))

        # Remove from running set
        self._running.pop(job_id, None)

    def _kill_process_group(
        self, rp: _RunningProcess, force: bool = False, reason: str = ""
    ) -> None:
        """Send signal to the entire process group."""
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            pgid = os.getpgid(rp.proc.pid)
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Check if a process with *pid* exists."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
