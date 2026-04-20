# CLAUDE.md

Developer guide for Claude Code sessions working on the Murphy Agent codebase.

## What This Repo Is

Murphy Agent is a self-hosted Slack supervisor. The Python supervisor polls Slack, dispatches stateless worker sessions, keeps task state on disk, and optionally runs developer review, Tribune review, and dashboard export flows.

The core implementation lives in `src/loop/`.

## Running the Supervisor

```bash
./scripts/run.sh
RUN_ONCE=true ./scripts/run.sh
SESSION_MINUTES=60 SLEEP_NORMAL=30 ./scripts/run.sh
MAX_CONCURRENT_WORKERS=2 ./scripts/run.sh
```

Defaults live in `src/config/supervisor_loop.conf` and can be overridden via environment variables or `.env`.

## Running Tests

```bash
python3 -m pytest src/loop/tests/test_supervisor_loop.py -v
```

## Key Files

| File | Role |
|---|---|
| `scripts/run.sh` | Entrypoint |
| `scripts/supervisor_loop.sh` | Python launcher |
| `scripts/dashboard.sh` | Standalone dashboard publisher |
| `src/loop/supervisor/main.py` | Supervisor entrypoint |
| `src/loop/supervisor/runtime.py` | Task lifecycle and Slack reconciliation |
| `src/loop/supervisor/config.py` | Config model |
| `src/loop/supervisor/worker_slot.py` | Worktree orchestration |
| `src/loop/supervisor/maintenance.py` | Maintenance flow |
| `src/loop/supervisor/job_store.py` | Async job store |
| `src/loop/monitor/dashboard.py` | Monitoring dashboard |
| `src/site/generator.py` | Optional static site generator |
| `src/config/supervisor_loop.conf` | Tunable defaults |
| `src/config/claude_mcp.example.json` | Claude MCP template |
| `.codex/config.example.toml` | Codex MCP template |
| `src/prompts/` | Worker / maintenance / Tribune prompts |
| `src/loop/tests/test_supervisor_loop.py` | Unit tests |

## Notes

- Tribune is disabled by default. Enable it only if `gemini` is installed.
- Dashboard export is disabled by default. Local dashboard serving works without extra config.
- This public repo intentionally does not track live runtime state, reports, or internal project notes.
