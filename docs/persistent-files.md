## Persistent state

`.agent/runtime/state.json` is supervisor-owned and immutable — do not edit it.

Your task is keyed by `mention_ts` and contains the full task payload.
### Read at session start:
- `.agent/memory/memory.md` — curated durable memory (cross-session knowledge, preferences, patterns).
- `.agent/memory/long_term_goals.md` — cross-session goals with progress tracking.
- `.agent/memory/daily/YYYY-MM-DD.md` — daily episodic notes (read today + yesterday in UTC).

### Per-task files:
- `reports/<thread_ts>.md` — evolving deliverable for that task.
- `deliverables/<thread_ts>/` — large generated outputs for non-project tasks.
- `projects/<slug>/outputs/<run_label>/` — large generated outputs for project-scoped tasks.

### Project state:
- `projects/<goal_slug>.md` — evolving report for each long-term project.
- `.agent/projects/<slug>.json` — per-project conversation history and summaries. Read the relevant project JSON when you need context about prior work on a project.
- `.agent/projects/<slug>.consult.jsonl` — per-project Consult MCP history (machine-assembled from global archive). Contains prompt, response, mode, chat_id, turn for each consult call.
- `.agent/runtime/consult_history/{task_id}.jsonl` — per-task Consult MCP records (global archive). Complete log with all metadata.

### Supervisor-managed (read-only):
- `.agent/runtime/logs/runner.log` — append-only heartbeat/session log from the external loop.
- `.agent/runtime/heartbeat.json` — latest liveness snapshot from the external loop.
- `.agent/runtime/logs/last_session.log` — stdout/stderr from the most recent agent attempt for debugging.