#!/usr/bin/env python3
"""Maintenance cycle manager: single task, multiple phases."""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List

from .utils import now_ts, timestamp_utc

if TYPE_CHECKING:
    from .runtime import Supervisor


class MaintenanceManager:
    """Single-task maintenance cycle with sequential phases.

    Phase sequence is dynamic based on ``TRIBUNE_MAINT_ROUNDS``:

    - rounds=0 (default): [reflect, developer] — legacy, no Tribune
    - rounds=1: [reflect, developer, tribune]
    - rounds=2: [reflect, developer, tribune, developer, tribune]
    - rounds=N: reflect + N alternating developer+tribune rounds

    Developer runs before Tribune so the Tribune reviews both Worker
    output AND Developer fixes.  Rounds are incremental: Developer
    round 2 addresses issues Tribune flagged in round 1.

    After each non-final phase finishes, the reconciler calls
    ``advance_phase()`` which bumps the phase and re-queues the task.
    """

    TASK_ID = "maintenance"
    TASK_TYPE = "maintenance"

    # Static phase definitions — combined dynamically in _build_phases().
    _REFLECT_PHASE: Dict[str, Any] = {
        "description": "Run maintenance reflection checklist and summarize findings.",
        "report_path": "reports/maintenance.reflect.md",
        "prompt_config_attr": "reflect_template",
        "cmd_config_attr": "worker_cmd",
        "role": "worker",
    }
    _DEV_PHASE: Dict[str, Any] = {
        "description": "Developer review: audit recent agent work and fix/improve system code.",
        "report_path": "reports/developer.review.md",
        "prompt_config_attr": "developer_review_template",
        "cmd_config_attr": "dev_review_cmd",
        "role": "developer",
    }
    _TRIBUNE_PHASE: Dict[str, Any] = {
        "description": "Tribune review: audit output quality, behavioral compliance, and developer fixes.",
        "report_path": "reports/tribune.maintenance.md",
        "prompt_config_attr": "tribune_maintenance_template",
        "cmd_config_attr": "tribune_cmd",
        "role": "tribune",
    }

    # Inline fallback prompts keyed by prompt_config_attr.
    _FALLBACK_PROMPTS: Dict[str, str] = {
        "reflect_template": (
            "Maintenance/reflect checklist:\n"
            "1) Review recent work across reports/, projects/, deliverables/, recent code changes, and recent Slack thread interactions.\n"
            "2) Review .agent/memory/daily/ (recent days), .agent/memory/long_term_goals.md, and .agent/memory/memory.md; condense wording and trim overly specific or obsolete items.\n"
            "3) Update user profiles from observation logs. Distill into JSON fields: biography, personality, communication_preferences, working_patterns, projects, active_context, milestones, notes. Scalar identity fields (email, github, timezone): raw canonical values only, replace stale values.\n"
            "4) Scalar identity scan: for all known users, scan recent task threads for email, GitHub username, and timezone. Attribution required: only update when unambiguously theirs. This is the primary path for email (PII, excluded from observation logs).\n"
            "5) Check whether each active project's documented progress aligns with the actual codebase and artifacts; update project docs accordingly.\n"
            "6) Verify submodule hygiene for projects/* and mcp/*.\n"
            "7) Check github status of root, MCP repos, and projects; report or fix issues.\n"
            "8) Keep edits concise and evidence-based.\n"
            "9) Send a Slack summary to the task's channel_id reporting what was reviewed/fixed and any items needing human attention.\n"
        ),
        "developer_review_template": (
            "Developer review checklist:\n"
            "1) Review all recent code changes, commits, and PRs across the root repo, projects/, and mcp/.\n"
            "2) Fix any issues found: code bugs, stale config, missing docs, broken tests.\n"
            "3) Improve agent system code where warranted (prompts, scripts, supervisor logic).\n"
            "4) Commit all fixes directly with clear commit messages.\n"
            "5) Post findings summary to the task's channel_id.\n"
            "6) In dispatch outcome, never set status to in_progress; use only done or waiting_human.\n"
        ),
        "tribune_maintenance_template": (
            "Tribune maintenance review:\n"
            "1) Read Murphy's phase 0 reflect report and assess quality.\n"
            "2) Review Developer's recent commits for correctness and design philosophy compliance.\n"
            "3) Run python3 scripts/validate_agent_state.py for file integrity.\n"
            "4) Audit recent Slack interactions for communication quality.\n"
            "5) Check research deliverables for accuracy and completeness.\n"
            "6) Review behavioral contract compliance.\n"
            "7) Propose contract improvements (suggestions only).\n"
            "8) Write your summary to the outcome JSON summary field. The supervisor will post it to Slack.\n"
        ),
    }

    def __init__(self, sup: "Supervisor") -> None:
        self._sup = sup
        self.PHASES = self._build_phases()

    def _build_phases(self) -> List[Dict[str, Any]]:
        """Build the maintenance phase list based on tribune_maint_rounds.

        rounds=0: [reflect, developer] — backward compatible
        rounds=1: [reflect, developer, tribune]
        rounds=2: [reflect, dev, tribune, dev, tribune]
        """
        rounds = self._sup.cfg.tribune_maint_rounds
        has_dev = bool(self._sup.cfg.dev_review_cmd)
        has_tribune = bool(self._sup.cfg.tribune_cmd)
        phases: List[Dict[str, Any]] = [dict(self._REFLECT_PHASE)]
        if rounds == 0:
            if has_dev:
                phases.append(dict(self._DEV_PHASE))
            return phases
        if not has_dev:
            return phases
        if not has_tribune:
            phases.append(dict(self._DEV_PHASE))
            return phases
        for _ in range(rounds):
            phases.append(dict(self._DEV_PHASE))
            phases.append(dict(self._TRIBUNE_PHASE))
        return phases

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def is_maintenance_task(self, task_type: str) -> bool:
        """Return True if *task_type* is the maintenance task."""
        return task_type == self.TASK_TYPE

    def is_final_phase(self, phase: int) -> bool:
        """Return True if *phase* is the last phase in the cycle."""
        return phase >= len(self.PHASES) - 1

    @staticmethod
    def get_phase(task: Dict[str, Any]) -> int:
        """Extract maintenance phase, with legacy inference when missing.

        Legacy tasks may not carry ``maintenance_phase``. In that case infer
        phase 1 (developer review) from the task metadata, otherwise default
        to phase 0 (reflect).
        """
        raw_phase = task.get("maintenance_phase")
        try:
            return max(0, int(raw_phase))
        except (TypeError, ValueError):
            pass

        report_path = str(task.get("report_path") or "").strip().lower()
        description = str(task.get("task_description") or "").strip().lower()
        if report_path.endswith("developer.review.md") or description.startswith("developer review"):
            return 1
        return 0

    def phase_role(self, phase: int) -> str:
        """Return the role string for a given phase ('worker', 'tribune', or 'developer')."""
        phase = max(0, min(phase, len(self.PHASES) - 1))
        return self.PHASES[phase].get("role", "worker")

    def is_dev_review_phase(self, phase: int) -> bool:
        """Return True if *phase* is a developer review phase."""
        return self.phase_role(phase) == "developer"

    def get_worker_cmd(self, phase: int) -> List[str]:
        """Return the CLI command list for a given phase."""
        phase = max(0, min(phase, len(self.PHASES) - 1))
        cmd_attr = self.PHASES[phase].get("cmd_config_attr", "worker_cmd")
        return list(getattr(self._sup.cfg, cmd_attr))

    def load_prompt(self, phase: int) -> str:
        """Load the prompt template for a phase.

        Falls back to an inline prompt if the template file is missing.
        """
        phase = max(0, min(phase, len(self.PHASES) - 1))
        phase_def = self.PHASES[phase]
        template_path: Path = getattr(self._sup.cfg, phase_def["prompt_config_attr"])
        if template_path.exists():
            return template_path.read_text(encoding="utf-8")

        self._sup.log_line(
            f"maintenance_prompt_missing path={template_path} "
            f"phase={phase} using_fallback=true"
        )
        config_attr = phase_def["prompt_config_attr"]
        return self._FALLBACK_PROMPTS.get(config_attr, "")

    # ------------------------------------------------------------------
    # Enqueue logic
    # ------------------------------------------------------------------

    def enqueue_if_due(self) -> None:
        """Check the daily schedule and enqueue maintenance at phase 0 if due.

        Maintenance runs once per calendar day at/after ``MAINTENANCE_HOUR``
        (local time, default 4 = 4 AM).  The legacy ``REFLECT_INTERVAL_SEC``
        is kept as a fallback guard but the primary gate is date-based.
        """
        sup = self._sup

        with sup.state_lock():
            state = sup.load_state()

            if self._is_in_flight(state):
                return

            # Daily schedule: run once per calendar day at/after MAINTENANCE_HOUR.
            now = time.localtime()
            if now.tm_hour < sup.cfg.maintenance_hour:
                return  # too early today

            today_date = time.strftime("%Y-%m-%d", now)
            last_ts = str(((state.get("supervisor") or {}).get("last_reflect_dispatch_ts") or "0"))
            try:
                last_date = time.strftime("%Y-%m-%d", time.localtime(int(last_ts.split(".")[0])))
            except (TypeError, ValueError, OSError):
                last_date = ""
            if last_date == today_date:
                return  # already ran today

            self._enqueue(state)
            state.setdefault("supervisor", {})["last_reflect_dispatch_ts"] = now_ts()
            sup.save_state(state)

        sup.log_line(f"maintenance_enqueued task_id={self.TASK_ID}")

    def enqueue_now(self) -> None:
        """Manually trigger maintenance (bypasses the 24-hour timer).

        Called when ``@agent !maintenance`` is received via Slack.
        Still respects the dedup guard.
        """
        sup = self._sup

        with sup.state_lock():
            state = sup.load_state()

            if self._is_in_flight(state):
                sup.log_line("maintenance_manual_skip reason=already_in_flight")
                return

            self._enqueue(state)
            state.setdefault("supervisor", {})["last_reflect_dispatch_ts"] = now_ts()
            sup.save_state(state)

        sup.log_line(f"maintenance_manual_enqueued task_id={self.TASK_ID}")

    def advance_phase(self, key: str) -> bool:
        """Bump maintenance_phase and re-queue the same task.

        Called by the reconciler after a non-final phase completes.
        The task is in ``finished_tasks`` at this point (force-finished
        by the reconciler).  Returns True if the phase was advanced.
        """
        sup = self._sup

        with sup.state_lock():
            state = sup.load_state()

            task = (state.get("finished_tasks") or {}).get(key)
            if not isinstance(task, dict) or task.get("task_type") != self.TASK_TYPE:
                return False

            phase = self.get_phase(task)
            next_phase = phase + 1
            if next_phase >= len(self.PHASES):
                return False

            next_def = self.PHASES[next_phase]
            task["maintenance_phase"] = next_phase
            task["task_description"] = next_def["description"]
            task["report_path"] = next_def["report_path"]
            task["status"] = "queued"
            task["claimed_by"] = None
            task["last_update_ts"] = now_ts()

            # Move from finished to queued.
            state["finished_tasks"].pop(key, None)
            state.setdefault("queued_tasks", {})[key] = task

            # Move task text file to new bucket (ensure_task_text_file
            # handles the physical file move and path update in one step).
            sup.ensure_task_text_file(task, bucket_name="queued_tasks")

            sup.save_state(state)

        sup.log_line(f"maintenance_phase_advanced key={key} phase={next_phase}")
        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _is_in_flight(self, state: Dict[str, Any]) -> bool:
        """Return True if a maintenance task exists in any active bucket.

        A maintenance task parked as ``waiting_human`` in ``incomplete_tasks``
        is considered stale — it will never be re-dispatched automatically, so
        it must not block new maintenance from being enqueued.  When
        ``_enqueue`` runs, it clears all buckets (including ``incomplete_tasks``)
        before inserting the fresh phase-0 task, cleaning up the stale entry.
        """
        for bucket in ("queued_tasks", "active_tasks", "incomplete_tasks"):
            tasks = state.get(bucket) or {}
            for key, task in tasks.items():
                is_match = (key == self.TASK_ID) or (
                    isinstance(task, dict)
                    and task.get("task_type") == self.TASK_TYPE
                )
                if not is_match:
                    continue
                # Skip waiting_human in incomplete — not actively in flight.
                if (
                    bucket == "incomplete_tasks"
                    and isinstance(task, dict)
                    and str(task.get("status") or "") == "waiting_human"
                ):
                    continue
                return True
        return False

    def _enqueue(self, state: Dict[str, Any]) -> None:
        """Create a maintenance task at phase 0 and insert into queued_tasks.

        Caller must already hold the state lock and will call ``save_state``
        after this returns.
        """
        sup = self._sup
        task_id = self.TASK_ID
        dispatch_ts = now_ts()
        channel_id = sup.cfg.default_channel_id
        phase_def = self.PHASES[0]

        prompt_text = self.load_prompt(0)
        mention_text_file = str(sup.task_text_path(task_id, "queued_tasks"))
        task_data = sup._empty_task_data(
            task_id=task_id,
            thread_ts=task_id,
            channel_id=channel_id,
        )
        task_data["messages"].append({
            "ts": dispatch_ts,
            "user_id": "system",
            "role": "human",
            "text": prompt_text.strip(),
        })
        sup.write_task_json(mention_text_file, task_data)

        # Clear from all buckets before inserting.
        for bucket_name in ("queued_tasks", "active_tasks", "incomplete_tasks", "finished_tasks"):
            state.setdefault(bucket_name, {}).pop(task_id, None)

        state.setdefault("queued_tasks", {})[task_id] = {
            "mention_ts": task_id,
            "thread_ts": task_id,
            "channel_id": channel_id,
            "mention_text_file": mention_text_file,
            "status": "queued",
            "claimed_by": None,
            "summary": "",
            "task_description": phase_def["description"],
            "report_path": phase_def["report_path"],
            "created_ts": dispatch_ts,
            "last_update_ts": dispatch_ts,
            "source": {
                "user_id": "system",
                "user_name": "supervisor",
                "time_iso": timestamp_utc(),
            },
            "task_type": self.TASK_TYPE,
            "maintenance_phase": 0,
        }
