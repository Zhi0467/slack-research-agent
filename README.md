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

## Note
- currently only codex is supported as the main worker backend.
- currecntly only MacOS is supported.
- **the Slack app must be installed under the agent's Slack app**, create an email and slack account for your Murphy, and invite it to your workspace first.
- This repository is the canonical source for the public Murphy supervisor package. The bundled Slack MCP submodule intentionally still resolves to `murphytheagent/slack-mcp-server` as an upstream dependency. If you want that MCP to come from your own fork instead, update `.gitmodules` and run `git submodule sync --recursive`.
- `git clone --recurse-submodules` is preferred, otherwise you need to `git submodule update --init --recursive` after cloning this repo.

## Prerequisites

```bash
brew install codex
brew install tmux
```
Also install `go` at https://go.dev/doc/install.

Optional:

- `gemini` CLI if you want Tribune review
- Google Chrome if your optional consult MCP server needs it
- ChatGPT Pro plan ($100/200 tier) if you want Murphy to also have the ability to consult ChatGPT Pro.
- `npm install -g @anthropic-ai/claude-code` if you need claude code as the developer.

Authenticate the CLIs before starting.

## Quick Start

```bash
git clone --recurse-submodules https://github.com/Zhi0467/slack-research-agent.git murphy-agent
cd murphy-agent

python3 -m pip install -e '.[dev]'

cd mcp/slack-mcp-server
go build -o build/slack-mcp-server ./cmd/slack-mcp-server/
cd ../..

murphy init --default-channel-id <your-default-channel-id> --agent-name <agent-name-of-your-choice>
```

If you already cloned without submodules:

```bash
git submodule update --init --recursive
```


`murphy init` writes the canonical local config to `config.toml`, then
renders the generated projections:

- `config.toml`
- `.env`
- `.codex/config.toml`
- `src/config/claude_mcp.json`
- `slack-app-manifest.json`
- `.agent/memory/memory.md`
- `.agent/memory/long_term_goals.md`

It also writes absolute repo paths into `.codex/config.toml` and
`src/config/claude_mcp.json`, so worker sessions launched from git worktrees
still resolve the shared MCP binaries correctly.

Then:

1. Import `slack-app-manifest.json` into https://api.slack.com/apps, create a Slack from this manifest using the agent's Slack account.
2. Install the app to your workspace, again under the agent's account.
3. If you did not provide the `xoxp-...` token during `murphy init`, add it with `murphy config set slack.user_token <token>` and run `murphy config sync --force`.
4. Run one cycle:

```bash
murphy start --run-once
```
If you see a message at your default channel, the bootstrap is done.

The primary day-2 config surface is:

- `config.toml`
- `murphy config show`
- `murphy config set <key> <value>`
- `murphy config unset <key>`
- `murphy config doctor`
- `murphy config sync`

The generated files remain available as advanced escape hatches:

- `.env`
- `.codex/config.toml`
- `src/config/claude_mcp.json`

Finally start the supervisor with tmux-managed lifecycle commands:

```bash
murphy start
```

Useful operator commands:

```bash
murphy status
murphy restart
murphy logs
```

### Optional consult: Let your agent use ChatGPT Pro via Chrome
`murphy init` leaves the optional `consult` MCP entry disabled by default. If
you have your own consult server, set it later with `murphy config set` or edit
`config.toml` and run `murphy config sync`.

When you use consult, assume Athena has **zero access** to your computer, local
files, repo checkout, PR state, or earlier task context unless you explicitly
provide it in the current consult. If Athena needs a local artifact, attach it
through `consult.ask(..., file_paths=[...])`; mentioning a local path in the
prompt is not enough. If Athena is helping with code review or PR work, include
the GitHub URL and enough pasted or summarized context for it to reason without
your local checkout.

The intended upstream consult server is
`https://github.com/murphytheagent/chatgpt-mcp-chrome`.

Minimal setup for that server:

```bash
git clone https://github.com/murphytheagent/chatgpt-mcp-chrome.git /path/to/chatgpt-mcp-chrome
python3 -m pip install -e /path/to/chatgpt-mcp-chrome

bash /path/to/chatgpt-mcp-chrome/scripts/launch_chrome.sh
# Sign into chatgpt.com in the Chrome window that opens the first time.

murphy config set consult.command chatgpt-mcp-chrome
murphy config unset consult.args
murphy config set worker.chatgpt_project Murphy
murphy config sync --force
```

If `chatgpt-mcp-chrome` is not on your shell `PATH`, use its absolute path
instead of the bare command name.

For `chatgpt-mcp-chrome`, `consult.args` should normally stay empty. Its
entrypoint already runs the MCP server on stdio and does not require extra CLI
flags.

The server defaults to:

- `CHATGPT_CDP_URL=http://127.0.0.1:9222`
- `CHROME_PATH=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`
- `CHROME_USER_DATA_DIR=~/Library/Application Support/Google/Chrome-Automation`

Those defaults match Murphy's current public setup on macOS. If you need a
different Chrome path, profile directory, or CDP port, the current CLI does not
have first-class keys for those env vars yet.

Murphy writes the consult server into `.codex/config.toml` under
`[mcp_servers.consult]`. Today the canonical config models only:

- `consult.command`
- `consult.args`
- `worker.chatgpt_project` for `[mcp_servers.consult.env].CHATGPT_DEFAULT_PROJECT`

At runtime Murphy also injects `CONSULT_SLOT_ID`, `CONSULT_TASK_ID`, and
`CONSULT_HISTORY_DIR` into `[mcp_servers.consult.env]` so the consult server
can preserve per-task history and parallel-slot isolation.



## Usage
1. `@<your-agent-username> task` to start a thread/task, example `@Murphy On my agentic Lean project, let's add and test a command that does the following...` or `@Murphy brief me on the current status of small swe train, and run the pilot study using our GPU node.`. You can send attachments, the agent can also send attachments back.
2. Reply to a thread to continue working like a normal Chat session, unless the agent posted a reply indicating the task is done.
3. Try re-mention in thread if the agent is taking too long to reply for a simple request.
4. Use syntax `@<your-agent-username> !loop-3h task` to enforce a minimum working time, for example, `@Murphy !loop-4h fork OpenAI parameter golf repo and try to achieve the best score, report back only the final experiment logs synthesized in a PDF.`

## Setup Notes

### Slack

Murphy operates as a Slack user. `murphy init` generates `slack-app-manifest.json` with the user-token scopes the public setup needs, including:

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

The main worker uses `.codex/config.toml`. `murphy init` rewrites the tracked
template into a local config with absolute repo paths so worktree-launched
workers do not break.

Consult is optional in the public package. The generated config leaves `[mcp_servers.consult]` disabled until you point it at a server you control.

### Claude Developer Review

Maintenance phase 1 uses `src/config/claude_mcp.json`. `murphy init` rewrites
that file the same way, and if Claude Code is not installed the maintenance
loop automatically skips that developer-review phase instead of crashing later.

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

The canonical local config lives in root `config.toml`. `murphy init` owns
first-run setup, and `murphy config` is the day-2 inspection/edit surface.

Generated files remain projections derived from the canonical config:

- `.env`
- `.codex/config.toml`
- `src/config/claude_mcp.json`
- `slack-app-manifest.json`

Main runtime defaults still live in `src/config/supervisor_loop.conf`, and the
supervisor continues to read env + defaults at runtime.

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
murphy --help
murphy init
murphy config show
murphy config doctor
murphy start
murphy start --run-once
murphy status
murphy restart
murphy logs
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
