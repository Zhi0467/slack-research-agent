#!/usr/bin/env python3
"""Durable job store for async shell jobs.

Implements the on-disk model from plan 31:
- Job records at ``<jobs_dir>/registry/<job_id>.json``
- Append-only event logs at ``<jobs_dir>/events/<job_id>.jsonl``
- Derived attention (no mutable attention_state field)
- CAS acknowledgements to prevent stale workers from clearing newer wakeups

This module is a standalone data layer with no supervisor imports. The
supervisor hooks (selection, prompt injection, reconciliation) are wired
in Phase 2.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Lease:
    """Exclusive claim on a pending material event."""

    owner: Optional[str] = None
    seq: Optional[int] = None
    expires_at: Optional[str] = None  # ISO-8601 UTC

    def is_valid(self, now: Optional[datetime] = None) -> bool:
        if self.owner is None or self.seq is None or self.expires_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        try:
            exp = datetime.fromisoformat(self.expires_at)
        except (ValueError, TypeError):
            return False
        return now < exp


@dataclass
class JobRecord:
    """Durable job record persisted at ``registry/<job_id>.json``."""

    job_id: str
    task_id: str
    thread_ts: str
    origin_turn_id: str
    adapter: str = "shell"
    adapter_handle: Dict[str, Any] = field(default_factory=dict)
    runtime_state: str = "running"  # running | succeeded | failed | lost
    acknowledged_material_seq: int = 0
    lease: Lease = field(default_factory=Lease)
    last_log_cursor: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now_iso
        if not self.updated_at:
            self.updated_at = now_iso
        # Allow dict→Lease coercion from JSON round-trip
        if isinstance(self.lease, dict):
            self.lease = Lease(**self.lease)


@dataclass
class JobEvent:
    """Single entry in the append-only event log."""

    job_id: str
    seq: int
    kind: str  # job_started | job_completed | job_failed | ...
    ts: str = ""
    summary: str = ""
    runtime_state_after: str = ""
    requires_attention: bool = False
    log_cursor: int = 0
    source_event_key: str = ""

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat()


# Material event kinds that require worker attention.
MATERIAL_EVENT_KINDS = frozenset({
    "job_completed",
    "job_failed",
    "job_requires_input",
    "job_stalled",
    "job_timeout",
    "job_lost",
})


@dataclass
class AckRequest:
    """Worker acknowledgement from the outcome payload."""

    job_id: str
    handled_seq: int
    lease_owner: str


# ---------------------------------------------------------------------------
# Attention derivation
# ---------------------------------------------------------------------------


def pending_material_seq(
    events: List[JobEvent],
    ack_seq: int,
) -> Optional[int]:
    """Return the smallest material event seq > ack_seq, or None."""
    best: Optional[int] = None
    for ev in events:
        if ev.requires_attention and ev.seq > ack_seq:
            if best is None or ev.seq < best:
                best = ev.seq
    return best


def attention_state(
    job: JobRecord,
    events: List[JobEvent],
    now: Optional[datetime] = None,
) -> str:
    """Derive attention state: ``idle`` | ``pending`` | ``leased``.

    No mutable field — computed from the event log and ack watermark.
    """
    pms = pending_material_seq(events, job.acknowledged_material_seq)
    if pms is None:
        return "idle"
    if job.lease.is_valid(now) and job.lease.seq == pms:
        return "leased"
    return "pending"


# ---------------------------------------------------------------------------
# CAS acknowledgement
# ---------------------------------------------------------------------------


def apply_ack(
    job: JobRecord,
    events: List[JobEvent],
    ack: AckRequest,
) -> bool:
    """Apply a CAS acknowledgement.  Returns True if the ack was accepted.

    Checks (all must pass):
    1. ``handled_seq`` refers to a material event that exists.
    2. ``handled_seq > acknowledged_material_seq``.
    3. ``lease.owner == ack.lease_owner``.
    4. ``lease.seq == ack.handled_seq``.
    """
    # Check 1: event exists and is material
    event_exists = any(
        ev.seq == ack.handled_seq and ev.requires_attention for ev in events
    )
    if not event_exists:
        return False

    # Check 2: forward progress
    if ack.handled_seq <= job.acknowledged_material_seq:
        return False

    # Check 3 & 4: lease ownership
    if job.lease.owner != ack.lease_owner:
        return False
    if job.lease.seq != ack.handled_seq:
        return False

    # CAS passes — advance watermark and clear lease.
    job.acknowledged_material_seq = ack.handled_seq
    job.lease = Lease()
    job.updated_at = datetime.now(timezone.utc).isoformat()
    return True


# ---------------------------------------------------------------------------
# On-disk persistence
# ---------------------------------------------------------------------------


class JobStore:
    """File-backed store for job records and event logs.

    Layout::

        <root>/
            registry/<job_id>.json
            events/<job_id>.jsonl
            logs/<job_id>.log          (raw output, managed by adapter)
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.registry_dir = root / "registry"
        self.events_dir = root / "events"
        self.logs_dir = root / "logs"
        for d in (self.registry_dir, self.events_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- Job records ----

    def _job_path(self, job_id: str) -> Path:
        return self.registry_dir / f"{job_id}.json"

    def create_job(self, job: JobRecord) -> None:
        path = self._job_path(job.job_id)
        path.write_text(json.dumps(asdict(job), indent=2), encoding="utf-8")

    def load_job(self, job_id: str) -> Optional[JobRecord]:
        path = self._job_path(job_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return JobRecord(**data)

    def save_job(self, job: JobRecord) -> None:
        job.updated_at = datetime.now(timezone.utc).isoformat()
        self._job_path(job.job_id).write_text(
            json.dumps(asdict(job), indent=2), encoding="utf-8"
        )

    def list_jobs(self, task_id: Optional[str] = None) -> List[JobRecord]:
        """List all jobs, optionally filtered by task_id."""
        jobs: List[JobRecord] = []
        if not self.registry_dir.exists():
            return jobs
        for p in sorted(self.registry_dir.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                jr = JobRecord(**data)
                if task_id is None or jr.task_id == task_id:
                    jobs.append(jr)
            except (json.JSONDecodeError, TypeError):
                continue
        return jobs

    # ---- Event logs ----

    def _events_path(self, job_id: str) -> Path:
        return self.events_dir / f"{job_id}.jsonl"

    def append_event(self, event: JobEvent) -> bool:
        """Append an event.  Deduplicates by ``source_event_key``.

        Returns True if appended, False if duplicate.
        """
        path = self._events_path(event.job_id)
        if event.source_event_key:
            existing = self.load_events(event.job_id)
            for ex in existing:
                if ex.source_event_key == event.source_event_key:
                    return False
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event)) + "\n")
        return True

    def load_events(self, job_id: str) -> List[JobEvent]:
        path = self._events_path(job_id)
        if not path.exists():
            return []
        events: List[JobEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(JobEvent(**json.loads(line)))
            except (json.JSONDecodeError, TypeError):
                continue
        return events

    def next_seq(self, job_id: str) -> int:
        """Return the next available sequence number for a job."""
        events = self.load_events(job_id)
        if not events:
            return 1
        return max(e.seq for e in events) + 1

    # ---- Derived queries ----

    def attention_for_job(
        self, job_id: str, now: Optional[datetime] = None
    ) -> str:
        """Compute attention state for a job."""
        job = self.load_job(job_id)
        if job is None:
            return "idle"
        events = self.load_events(job_id)
        return attention_state(job, events, now)

    def pending_wakeups(
        self, task_id: str, now: Optional[datetime] = None
    ) -> List[JobRecord]:
        """Return jobs for a task that need worker attention."""
        result: List[JobRecord] = []
        for job in self.list_jobs(task_id=task_id):
            events = self.load_events(job.job_id)
            if attention_state(job, events, now) == "pending":
                result.append(job)
        return result

    # ---- Lease management ----

    def issue_lease(
        self,
        job_id: str,
        owner: str,
        duration_sec: int = 3600,
        now: Optional[datetime] = None,
    ) -> bool:
        """Issue a lease on the pending material event.

        Returns True if a lease was issued, False if no pending event.
        """
        job = self.load_job(job_id)
        if job is None:
            return False
        events = self.load_events(job_id)
        pms = pending_material_seq(events, job.acknowledged_material_seq)
        if pms is None:
            return False
        now = now or datetime.now(timezone.utc)
        job.lease = Lease(
            owner=owner,
            seq=pms,
            expires_at=(now + timedelta(seconds=duration_sec)).isoformat(),
        )
        self.save_job(job)
        return True

    def expire_leases(self, now: Optional[datetime] = None) -> List[str]:
        """Expire stale leases.  Returns list of job_ids whose leases expired."""
        now = now or datetime.now(timezone.utc)
        expired: List[str] = []
        for job in self.list_jobs():
            if job.lease.owner is not None and not job.lease.is_valid(now):
                job.lease = Lease()
                self.save_job(job)
                expired.append(job.job_id)
        return expired

    # ---- Acknowledgement ----

    def process_ack(self, ack: AckRequest) -> bool:
        """Process a worker acknowledgement with CAS semantics.

        Returns True if accepted, False if rejected (stale/duplicate/mismatch).
        """
        job = self.load_job(ack.job_id)
        if job is None:
            return False
        events = self.load_events(ack.job_id)
        if apply_ack(job, events, ack):
            self.save_job(job)
            return True
        return False
