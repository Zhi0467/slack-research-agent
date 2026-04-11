# Murphy Agent

Murphy Agent is a self-hosted Slack supervisor for long-running AI work. It polls Slack for `@mentions`, dispatches stateless worker sessions, keeps task state on disk, and supports maintenance, parallel worktrees, optional dashboard publishing, and optional second-pass review.

This repo is maintained as a standalone public package rather than a live mirror of the private Research checkout. Internal reports, project notes, task history, and local runtime state are intentionally not tracked here.

## What You Get

- Python supervisor with disk-backed task state
- Stateless worker dispatch through Codex
- Optional developer-review phase through Claude Code
- Optional Tribune review through Gemini CLI
- Parallel workers via git worktrees
- Optional dashboard/static export tooling
- Bundled public skill pack under `.agent/skills/`
- Prompt and contract files you can customize for your own agent

## Quick Start

```bash
git clone --recurse-submodules https://github.com/murphytheagent/murphy-supervisor.git murphy-agent
cd murphy-agent

python3 -m pip install -e '.[dev]'

cd mcp/slack-mcp-server
go build -o build/slack-mcp-server ./cmd/slack-mcp-server/
cd ../..

murphy-init --default-channel-id C0123456789
```

That command generates:

- `.env`
- `.codex/config.toml`
- `src/config/claude_mcp.json`
- `slack-app-manifest.json`
- `.agent/memory/memory.md`
- `.agent/memory/long_term_goals.md`

It also writes absolute repo paths into `.codex/config.toml` and `src/config/claude_mcp.json`, so worker sessions launched from git worktrees still resolve the shared MCP binaries correctly.

`murphy-init` leaves the optional `consult` MCP entry disabled by default. If you have your own consult server, wire it into `.codex/config.toml` after bootstrap.

If you already cloned without submodules:

```bash
git submodule update --init --recursive
```

Then:

1. Import `slack-app-manifest.json` into https://api.slack.com/apps and install it to your workspace.
2. Paste the resulting `xoxp-...` user token into the generated local config files.
3. Run one cycle:

```bash
RUN_ONCE=true ./scripts/run.sh
```

Files you will usually edit after bootstrap:

- `.env`
- `.codex/config.toml`
- `src/config/claude_mcp.json`

## Prerequisites

```bash
brew install go node python@3.11
npm install -g @openai/codex
npm install -g @anthropic-ai/claude-code
```

Optional:

- `gemini` CLI if you want Tribune review
- Google Chrome if your optional consult MCP server needs it

Authenticate the CLIs you plan to use before starting.

## Setup Notes

### Slack

Murphy operates as a Slack user. `murphy-init` generates `slack-app-manifest.json` with the user-token scopes the public setup needs, including:

- `channels:history`
- `channels:read`
- `groups:history`
- `groups:read`
- `im:history`
- `im:read`
- `im:write`
- `mpim:history`
- `mpim:read`
- `mpim:write`
- `users:read`
- `chat:write`
- `search:read`
- `files:read`
- `files:write`
- `reactions:read`
- `reactions:write`

The supervisor itself uses `SLACK_USER_TOKEN` from `.env`. The worker MCP configs need the same token embedded as a literal value.

The older `slack-claude-bot` helper is private and is intentionally not part of the public bootstrap flow.

### Codex Worker

The main worker uses `.codex/config.toml`. `murphy-init` rewrites the tracked template (`.codex/config.example.toml`) into a local config with absolute repo paths so worktree-launched workers do not break.

Consult is optional in the public package. The generated config leaves `[mcp_servers.consult]` disabled until you point it at a server you control.

### Claude Developer Review

Maintenance phase 1 uses `src/config/claude_mcp.json`. `murphy-init` rewrites `src/config/claude_mcp.example.json` the same way, and if Claude Code is not installed the maintenance loop automatically skips that developer-review phase instead of crashing later.

### Tribune Review

Tribune is disabled by default in `src/config/supervisor_loop.conf`. If you want it, install Gemini CLI and set:

```bash
TRIBUNE_MAX_REVIEW_ROUNDS=1
```

### Dashboard

Dashboard export is also disabled by default. The local HTTP dashboard still works:

```bash
python3 -m src.loop.monitor.dashboard
```

If you want continuous static export, configure `DASHBOARD_*` vars in `.env` and run:

```bash
./scripts/dashboard.sh
```

## Configuration

Main defaults live in `src/config/supervisor_loop.conf` and can be overridden from `.env` or the shell.

Important variables:

- `SLACK_USER_TOKEN`
- `DEFAULT_CHANNEL_ID`
- `WORKER_CMD`
- `DEV_REVIEW_CMD`
- `MAX_CONCURRENT_WORKERS`
- `RUN_ONCE`
- `TRIBUNE_MAX_REVIEW_ROUNDS`
- `DASHBOARD_EXPORT_ENABLED`

## Runtime Layout

Runtime state is written under `.agent/`, mainly:

- `.agent/skills/` is tracked with the repo and ships the reusable public skill pack
- `.agent/runtime/state.json`
- `.agent/runtime/outcomes/`
- `.agent/runtime/dispatch/`
- `.agent/tasks/`
- `.agent/memory/`

Task outputs default to ignored runtime directories such as `reports/`, `deliverables/`, and `projects/`.

## Commands

```bash
murphy-init --help
./scripts/run.sh
RUN_ONCE=true ./scripts/run.sh
python3 -m src.loop.monitor.dashboard
./scripts/dashboard.sh
python3 -m pytest src/loop/tests/test_supervisor_loop.py -v
```

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md)
- [AGENTS.md](AGENTS.md)
- [CLAUDE.md](CLAUDE.md)
- [docs/system-overview.md](docs/system-overview.md)
- [docs/user-guide.md](docs/user-guide.md)
- [docs/operations.md](docs/operations.md)
- [docs/schemas.md](docs/schemas.md)
- [docs/mcp-integrations.md](docs/mcp-integrations.md)

## License

MIT
