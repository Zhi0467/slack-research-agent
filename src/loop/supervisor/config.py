#!/usr/bin/env python3
"""Configuration model for the supervisor loop."""

from __future__ import annotations

import os
import shlex
import socket
from pathlib import Path
from typing import Mapping, Optional

from .utils import parse_bool, parse_conf_defaults


class Config:
    def __init__(
        self,
        loop_config_file: Path,
        env: Optional[Mapping[str, str]] = None,
    ) -> None:
        defaults = parse_conf_defaults(loop_config_file)
        env_map: Mapping[str, str] = env if env is not None else os.environ

        def get(name: str, fallback: str) -> str:
            return env_map.get(name, defaults.get(name, fallback))

        self.session_minutes = int(get("SESSION_MINUTES", "360"))
        self.sleep_normal = int(get("SLEEP_NORMAL", "120"))
        self.poll_interval = int(get("POLL_INTERVAL", "5"))
        self.waiting_refresh_interval = int(get("WAITING_REFRESH_INTERVAL", "30"))
        self.pending_check_initial = int(get("PENDING_CHECK_INITIAL", "120"))
        self.pending_check_multiplier = int(get("PENDING_CHECK_MULTIPLIER", "2"))
        self.pending_check_max = int(get("PENDING_CHECK_MAX", "600"))
        self.failure_sleep_sec = int(get("FAILURE_SLEEP_SEC", "120"))

        self.max_transient_retries = int(get("MAX_TRANSIENT_RETRIES", "3"))
        self.transient_retry_initial = int(get("TRANSIENT_RETRY_INITIAL", "15"))
        self.transient_retry_multiplier = int(get("TRANSIENT_RETRY_MULTIPLIER", "2"))
        self.transient_retry_max = int(get("TRANSIENT_RETRY_MAX", "300"))

        # Auth failure backoff (e.g. expired OAuth token)
        self.auth_max_retries = int(get("AUTH_MAX_RETRIES", "5"))
        self.auth_retry_initial = int(get("AUTH_RETRY_INITIAL", "60"))
        self.auth_retry_multiplier = int(get("AUTH_RETRY_MULTIPLIER", "2"))
        self.auth_retry_max = int(get("AUTH_RETRY_MAX", "600"))

        self.mention_poll_limit = int(get("MENTION_POLL_LIMIT", "10"))
        self.mention_max_pages = int(get("MENTION_MAX_PAGES", "25"))
        self.finished_ttl_days = int(get("FINISHED_TTL_DAYS", "30"))
        self.max_incomplete_retention = int(get("MAX_INCOMPLETE_RETENTION", "259200"))
        self.reflect_interval_sec = int(get("REFLECT_INTERVAL_SEC", "86400"))
        self.maintenance_hour = int(get("MAINTENANCE_HOUR", "4"))

        self.waiting_human_reply_limit = int(get("WAITING_HUMAN_REPLY_LIMIT", "100"))
        self.completion_gate = get("COMPLETION_GATE", "high")
        self.prompt_memory_total_char_limit = int(get("PROMPT_MEMORY_TOTAL_CHAR_LIMIT", "20000"))
        self.worker_id = get("WORKER_ID", f"{socket.gethostname()}-agent")
        self.agent_name = get("AGENT_NAME", "Murphy").strip() or "Murphy"
        self.agent_user_id = get("AGENT_USER_ID", "").strip()
        self.run_once = parse_bool(get("RUN_ONCE", "false"))
        self.default_channel_id = get("DEFAULT_CHANNEL_ID", "")
        self.max_concurrent_workers = int(get("MAX_CONCURRENT_WORKERS", "1"))

        # Worker CLI commands (parsed as shell tokens)
        self.worker_cmd = shlex.split(get("WORKER_CMD", "codex exec --yolo --ephemeral --skip-git-repo-check -"))
        self.dev_review_cmd = shlex.split(get("DEV_REVIEW_CMD", "claude -p --dangerously-skip-permissions --mcp-config src/config/claude_mcp.json"))

        # Tribune (independent quality reviewer, Gemini CLI)
        self.tribune_cmd = shlex.split(get("TRIBUNE_CMD", "gemini -m gemini-3.1-pro-preview -p '' -y --output-format text"))
        self.tribune_fallback_models = [
            m.strip() for m in get("TRIBUNE_FALLBACK_MODELS", "gemini-3-flash,gemini-2.5-flash").split(",") if m.strip()
        ]
        self.tribune_max_review_rounds = int(get("TRIBUNE_MAX_REVIEW_ROUNDS", "0"))
        self.tribune_max_reviews_per_thread = int(get("TRIBUNE_MAX_REVIEWS_PER_THREAD", "4"))
        self.tribune_maint_rounds = int(get("TRIBUNE_MAINT_ROUNDS", "0"))
        if not self.tribune_cmd:
            self.tribune_max_review_rounds = 0
            self.tribune_maint_rounds = 0

        # Slack API retry/backoff
        self.slack_api_max_retries = int(get("SLACK_API_MAX_RETRIES", "3"))
        self.slack_api_retry_initial_sec = float(get("SLACK_API_RETRY_INITIAL_SEC", "1.0"))
        self.slack_api_retry_multiplier = float(get("SLACK_API_RETRY_MULTIPLIER", "2.0"))
        self.slack_api_retry_max_sec = float(get("SLACK_API_RETRY_MAX_SEC", "30.0"))
        self.slack_api_timeout_sec = int(get("SLACK_API_TIMEOUT_SEC", "60"))

        # Loop mode (continuous iteration)
        self.loop_max_duration_sec = int(get("LOOP_MAX_DURATION_SEC", "18000"))
        self.loop_iteration_delay_sec = int(get("LOOP_ITERATION_DELAY_SEC", "180"))

        # Worker watchdog
        self.mcp_startup_check_sec = int(get("MCP_STARTUP_CHECK_SEC", "30"))
        self.max_watchdog_retries = int(get("MAX_WATCHDOG_RETRIES", "2"))
        self.worker_idle_timeout_sec = int(get("WORKER_IDLE_TIMEOUT_SEC", "900"))
        self.max_consecutive_exit_failures = int(get("MAX_CONSECUTIVE_EXIT_FAILURES", "3"))
        self.worker_tool_timeout_sec = int(get("WORKER_TOOL_TIMEOUT_SEC", "14400"))

        # Thread continuation (AGENT-057)
        self.thread_continuation_threshold = int(get("THREAD_CONTINUATION_THRESHOLD", "80"))

        # Session resume (AGENT-025)
        self.session_resume_enabled = parse_bool(get("SESSION_RESUME_ENABLED", "true"))
        self.max_session_resumes = int(get("MAX_SESSION_RESUMES", "5"))

        # Merge-failure enforcement (plan 09)
        self.merge_failure_blocks_done = parse_bool(get("MERGE_FAILURE_BLOCKS_DONE", "false"))

        self.state_file = Path(get("STATE_FILE", ".agent/runtime/state.json"))
        # repo_root derived from state_file: .agent/runtime/state.json → ../../.. = repo root
        self.repo_root = self.state_file.parent.parent.parent
        self.runner_log = Path(get("RUNNER_LOG", ".agent/runtime/logs/runner.log"))
        self.heartbeat_file = Path(get("HEARTBEAT_FILE", ".agent/runtime/heartbeat.json"))
        self.last_session_log = Path(get("LAST_SESSION_LOG", ".agent/runtime/logs/last_session.log"))
        self.session_template = Path(get("SESSION_TEMPLATE", "src/prompts/session.md"))
        self.reflect_template = Path(get("REFLECT_TEMPLATE", "src/prompts/maintenance_reflect.md"))
        self.developer_review_template = Path(get("DEVELOPER_REVIEW_TEMPLATE", "src/prompts/developer_review.md"))
        self.tribune_review_template = Path(get("TRIBUNE_REVIEW_TEMPLATE", "src/prompts/tribune_review.md"))
        self.tribune_maintenance_template = Path(get("TRIBUNE_MAINTENANCE_TEMPLATE", "src/prompts/tribune_maintenance.md"))

        self.runtime_prompt_file = Path(get("RUNTIME_PROMPT_FILE", ".agent/runtime/dispatch/prompt.md"))
        self.dispatch_task_file = Path(get("DISPATCH_TASK_FILE", ".agent/runtime/dispatch/task.json"))
        self.dispatch_outcome_file = Path(get("DISPATCH_OUTCOME_FILE", ".agent/runtime/dispatch/outcome.json"))
        self.outcomes_dir = Path(get("OUTCOMES_DIR", ".agent/runtime/outcomes"))
        self.dispatch_dir = Path(get("DISPATCH_DIR", ".agent/runtime/dispatch"))
        self.worktree_dir = Path(get("WORKTREE_DIR", ".agent/runtime/worktrees"))
        self.tasks_dir = Path(get("TASKS_DIR", ".agent/tasks"))
        self.memory_file = Path(get("MEMORY_FILE", ".agent/memory/memory.md"))
        self.long_term_goals_file = Path(get("LONG_TERM_GOALS_FILE", ".agent/memory/long_term_goals.md"))
        self.memory_daily_dir = Path(get("MEMORY_DAILY_DIR", ".agent/memory/daily"))
        self.user_directory_file = Path(get("USER_DIRECTORY_FILE", ".agent/memory/user_directory.json"))
        self.user_profiles_dir = Path(get("USER_PROFILES_DIR", ".agent/user_profiles"))
        self.user_profile_char_limit = int(get("USER_PROFILE_CHAR_LIMIT", "2000"))
        self.projects_dir = Path(get("PROJECTS_DIR", ".agent/projects"))
        self.consult_history_dir = Path(get("CONSULT_HISTORY_DIR", ".agent/runtime/consult_history"))
        self.jobs_dir = Path(get("JOBS_DIR", ".agent/runtime/jobs"))

        # Bounded thread-context packing for worker prompts
        self.thread_context_max_chars = int(get("THREAD_CONTEXT_MAX_CHARS", "200000"))
        self.thread_context_max_messages = int(get("THREAD_CONTEXT_MAX_MESSAGES", "100"))
        self.thread_context_preserve_objective_chars = int(get("THREAD_CONTEXT_PRESERVE_OBJECTIVE_CHARS", "1000"))

        # Dashboard config removed — publisher runs as a standalone process
        # (scripts/dashboard.sh). Config values remain in supervisor_loop.conf
        # and are read by the dashboard module directly.
