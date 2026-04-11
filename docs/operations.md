# Operations Guide

Detailed operational procedures for the supervisor, dashboard, and maintenance.

## Running the Supervisor

```bash
./scripts/run.sh
RUN_ONCE=true ./scripts/run.sh
SESSION_MINUTES=60 SLEEP_NORMAL=30 ./scripts/run.sh
MAX_CONCURRENT_WORKERS=2 ./scripts/run.sh
```

Defaults live in `src/config/supervisor_loop.conf` and can be overridden via environment variables.

## Parallel Dispatch

When `MAX_CONCURRENT_WORKERS >= 2`, the supervisor dispatches multiple workers concurrently. Each worker runs in its own git worktree under `.agent/runtime/worktrees/worker-N/`.

Key behaviors:

- default is serial
- maintenance drains active workers first
- each worker writes its own session log
- outcomes are keyed by task, not worker slot

## Dashboard

The local dashboard server works out of the box:

```bash
python3 -m src.loop.monitor.dashboard
```

Static export is disabled by default. To enable it, set `DASHBOARD_EXPORT_ENABLED=true` and choose an export directory such as `dashboard-export`, then run:

```bash
./scripts/dashboard.sh
```

Useful commands:

```bash
python3 -m src.loop.monitor.dashboard --once --export-static-dir dashboard-export
python3 -m src.loop.monitor.dashboard 9000
```

## Maintenance

The default maintenance cycle is two phases:

1. reflect
2. developer review

Tribune maintenance rounds are optional and disabled by default.

## Hot Restart

```bash
kill -HUP $(jq -r .pid .agent/runtime/heartbeat.json)
```

Or from Slack: `@agent !restart`

## Tests

```bash
python3 -m pytest src/loop/tests/test_supervisor_loop.py -v
```
