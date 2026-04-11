# Architecture

## What This Repo Is

This is the root coordination repository for an autonomous research agent system. The worker agent ("Murphy") receives tasks via Slack `@mention`, dispatches a stateless Claude Code worker session per task, and persists all state on disk.

## How Tasks Flow

```
Slack @mention
      ↓  (poll every POLL_INTERVAL sec via search.messages API, default 5s)
src.loop.supervisor.main
      ↓  enqueues to runtime/state.json → queued_tasks (keyed by thread_ts)
      ↓  selects oldest task: active > incomplete (non-waiting) > queued
      ↓  refreshes thread context (conversations.replies) before render
      ↓  renders src/prompts/session.md with task JSON → .agent/runtime/dispatch/prompt.md
      ↓  invokes worker CLI (codex exec for tasks, claude -p for maintenance phase 1)
Worker agent ("Murphy" — stateless Claude Code session)
      ↓  communicates progress, does work, posts result, delivers PDFs for research content
      ↓  writes .agent/runtime/outcomes/<mention_ts>.json
src.loop.supervisor.main
      ↓  reads outcome → moves task to finished_tasks or incomplete_tasks
      ↓  snapshots full Slack thread into task JSON (conversations.replies)
      ↓  syncs task conversation to .agent/projects/<slug>.json if project-tagged
      ↓  if maintenance non-final phase: advance_phase() and re-queue
      ↓  if waiting_human: re-dispatches when human replies in thread
      ↓  if more tasks queued: loops immediately (skips sleep)
```

Task state buckets in `runtime/state.json`: `queued_tasks` → `active_tasks` → `finished_tasks` or `incomplete_tasks`

## Key Files

| File | Role |
|---|---|
| `scripts/run.sh` | Entrypoint; delegates to `scripts/supervisor_loop.sh` |
| `scripts/supervisor_loop.sh` | Launches the Python supervisor (`python -m src.loop.supervisor.main`) |
| `src/loop/supervisor/main.py` | Python supervisor entrypoint; wires config, runtime, and helpers |
| `src/loop/supervisor/runtime.py` | Supervisor runtime/task lifecycle implementation |
| `src/loop/supervisor/worker_slot.py` | WorkerSlot: per-worker worktree setup, subprocess management, worktree reset (parallel dispatch) |
| `src/loop/supervisor/config.py` | Supervisor config model and default path wiring |
| `src/loop/supervisor/utils.py` | Shared supervisor utilities (timestamps, parsing, regexes, message classification) |
| `src/loop/tests/test_supervisor_loop.py` | Unit tests for the Python supervisor; safe alongside a live agent |
| `src/loop/monitor/dashboard.py` | Monitoring dashboard; GitHub Pages static export (production) or local HTTP server (dev) |
| `src/config/supervisor_loop.conf` | Tunable defaults (timeouts, retries, TTLs, paths) |
| `src/prompts/session.md` | Runtime prompt template rendered into each worker dispatch |
| `src/loop/supervisor/maintenance.py` | MaintenanceManager: single-task, multi-phase daily maintenance (phase 0=reflect, phase 1=developer review) |
| `src/prompts/maintenance_reflect.md` | Phase 0 prompt: Murphy's periodic (24h) self-reflection/hygiene checklist |
| `src/prompts/developer_review.md` | Phase 1 prompt: Claude Code developer audit of recent agent work |
| `src/config/claude_mcp.json` | MCP server config for Claude Code sessions (developer review gets Slack access) |
| `AGENTS.md` | Worker behavioral contract — pointer file; detailed rules in `docs/protocols/`, `docs/workflows/`, `docs/mcp-integrations.md` |
| `CLAUDE.md` | Developer guide (Claude Code sessions); monolithic |
| `docs/dev/BACKLOG.md` | System improvement backlog |
| `docs/dev/CHANGELOG.md` | Agent source change log (excludes project/report churn) |
| `docs/dev/issues/` | Known issues and bug reports for developer review |
| `docs/dev/plans/` | Implementation plans for developer review to advance |
| `scripts/register_submodules.sh` | Registers a directory as a git submodule |
| `scripts/reset_task_list.sh` | Clears all task queues (dev/testing utility) |
| `scripts/assemble_project_jsons.py` | Syncs conversation data from task JSONs into `.agent/projects/<slug>.json` (`--sync` for single task, `--full` for rebuild all) |
| `scripts/memory_recall` | Search memory and reports with file/line citations |
| `scripts/memory_reflect` | Extract and promote durable memory candidates from a report |
| `scripts/project_worker.sh` | Spawns codex sub-session scoped to a project directory; loads project's AGENTS.md/CLAUDE.md |
| `docs/workflows/project-workflow.md` | Project sub-session workflow: when/how to spawn, review, and commit project work |
| `.agent/runtime/state.json` | Task queue — supervisor-owned; do not edit manually |
| `.agent/memory/memory.md` | Durable agent memory, appended per task |
| `.agent/memory/long_term_goals.md` | Cross-session goals with progress tracking |
| `.agent/runtime/logs/runner.log` | Append-only heartbeat/session log |
| `.agent/runtime/heartbeat.json` | Live status snapshot (pid, loop count, exit code, sleep policy) |
| `.agent/memory/user_directory.json` | Agent identity + user ID→name cache; read by `resolve_slack_id` and `resolve_user_name` |
| `.agent/runtime/dispatch/task.json` | Current dispatch task payload passed to worker (serial mode) |
| `.agent/runtime/outcomes/<mention_ts>.json` | Per-task worker outcome JSON; supervisor reads after each dispatch |
| `.agent/runtime/logs/last_session.log` | Captured stdout/stderr from the most recent worker session (serial mode) |
| `.agent/runtime/dispatch/` | Per-worker files for parallel dispatch: `worker-N.task.json`, `worker-N.prompt.md`, `worker-N.session.log` |
| `.agent/runtime/worktrees/` | Git worktrees for parallel workers: `worker-N/` directories |
| `.agent/memory/daily/` | Daily episodic memory files (UTC-dated); used for recent-work context in prompts |
| `.agent/tasks/<bucket>/<ts>.json` | Per-task thread history files (JSON); schema in `docs/schemas.md` |
| `.agent/projects/<slug>.json` | Per-project aggregated data with full conversations; assembled by `assemble_project_jsons.py` |

## Detailed Documentation

- [Operations guide](docs/operations.md) — supervisor config, dashboard, maintenance cycle, hot restart, parallel dispatch
- [Data schemas](docs/schemas.md) — worker outcome JSON, task JSON
- [Slack protocols](docs/protocols/slack-protocols.md) — Slack messaging, behavior requirements, outcome rules
- [Maintenance protocols](docs/protocols/maintenance-protocols.md) — maintenance task rules
- [Git workflow](docs/workflows/git-workflow.md) — git guardrails, submodule management, PR workflow
- [Research workflow](docs/workflows/research-workflow.md) — research task sequence, materials-first rule
- [Project workflow](docs/workflows/project-workflow.md) — project sub-session spawning, codex review, submodule commit
- [Remote GPU workflow](docs/workflows/remote-gpu-workflow.md) — GPU node constraints
- [MCP integrations](docs/mcp-integrations.md) — Slack MCP and optional consult integrations
- [Persistent files](docs/persistent-files.md) — agent file layout and artifact storage
