# Unified Murphy CLI Implementation Handoff

Created: 2026-04-20 00:26 UTC

Context:

- The user explicitly asked to implement `docs/dev/plans/2026-04-19-unified-cli-roadmap.md`.
- The worker contract for this repo forbids direct edits to root agent code (`src/`, `scripts/`, `docs/`, root files, `.codex/`, `.claude/`).
- This file is therefore the concrete developer-review handoff for implementation.

This handoff assumes `2026-04-19-unified-cli-roadmap.md` remains the product-level source of truth. The goal here is to make the implementation slice-by-slice and file-by-file enough that the developer phase can execute without another discovery pass.

## Current Code Reality

As of this handoff:

- `pyproject.toml` exposes `murphy-agent`, `murphy-dashboard`, and `murphy-init`. There is no top-level `murphy` entrypoint yet.
- `src/loop/bootstrap.py` owns the existing `murphy-init` flow and directly writes:
  - `.env`
  - `.codex/config.toml`
  - `src/config/claude_mcp.json`
  - `slack-app-manifest.json`
  - `.agent/memory/memory.md`
  - `.agent/memory/long_term_goals.md`
- `src/loop/supervisor/runtime.py` already supports hot restart via `SIGHUP` and writes `.agent/runtime/heartbeat.json`.
- `scripts/run.sh` already resolves repo root and launches `scripts/supervisor_loop.sh`.
- `src/loop/supervisor/config.py` still treats env + `src/config/supervisor_loop.conf` as the runtime source of truth.

## Recommended Module Shape

Implement the roadmap by splitting responsibilities instead of continuing to grow `src/loop/bootstrap.py`.

Recommended new modules:

- `src/loop/cli.py`
  - top-level `murphy` parser and dispatch
- `src/loop/cli_lifecycle.py`
  - `start`, `restart`, `status`, `logs`
- `src/loop/local_config.py`
  - canonical `.murphy/config.toml` model
  - import-from-existing logic
  - projection/render helpers
  - doctor/validation helpers

Recommended refactor target:

- keep `src/loop/bootstrap.py`, but shrink it into init-specific orchestration and shared rendering calls into `local_config.py`

This keeps three separations clear:

- CLI routing
- local control-plane config
- runtime supervisor config

## Slice 1: Shared `murphy` Entrypoint and Lifecycle Commands

This is the first shipping slice from the roadmap and does not require the canonical config migration.

Required changes:

1. Add `murphy = "src.loop.cli:main"` to `pyproject.toml`.
2. Keep `murphy-init = "src.loop.bootstrap:main"` as the compatibility alias for one release cycle.
3. Add `src/loop/cli.py` with subcommands:
   - `init`
   - `start`
   - `restart`
   - `status`
4. Wire `murphy init` to call the existing bootstrap flow first, before the later config redesign lands.
5. Leave `murphy logs` for the same module if cheap, otherwise add it immediately after `status`.

### `murphy start`

Implement in `src/loop/cli_lifecycle.py`.

Behavior:

- resolve repo root from:
  - explicit `--repo-root`
  - otherwise upward search for markers:
    - `pyproject.toml`
    - `AGENTS.md`
    - `scripts/run.sh`
- verify `tmux` exists using `shutil.which("tmux")`
- default session name: `supervisor`
- detect duplicate session with `tmux has-session -t <name>`
- start detached session with repo-root cwd and `./scripts/run.sh`
- support `--attach`
- support `--run-once` by appending `--run-once` to `scripts/run.sh`

Implementation note:

- do not shell out via `shell=True`
- use `subprocess.run([...], cwd=repo_root, check=False, capture_output=True, text=True)`
- report precise failure causes:
  - repo root not found
  - `tmux` missing
  - session already exists
  - run command launch failed

### `murphy restart`

Implement in `src/loop/cli_lifecycle.py`.

Behavior:

- locate heartbeat at `<repo_root>/.agent/runtime/heartbeat.json`
- parse JSON
- require integer `pid`
- send `signal.SIGHUP`
- optionally poll heartbeat once or twice to show that `last_updated_utc` changed

Failure cases:

- heartbeat missing: print “supervisor does not appear to be running; use `murphy start`”
- invalid JSON or missing pid: clear error and nonzero exit
- `ProcessLookupError`: stale heartbeat, suggest `murphy start`

Do not:

- recreate tmux
- kill the process
- invent a separate restart mechanism

### `murphy status`

Implement in `src/loop/cli_lifecycle.py`.

Behavior:

- read heartbeat if present
- print:
  - repo root
  - heartbeat path
  - status
  - pid
  - loop count
  - last updated timestamp
  - max workers
  - active worker count if present
- if heartbeat is missing, return nonzero with a concise “not running” message

Keep output operator-oriented and text-first. No table formatting required.

### Optional `murphy logs`

If added in slice 1:

- default target: `.agent/runtime/logs/runner.log`
- support `--last-session` to read `.agent/runtime/logs/last_session.log`
- support `--tail <n>` for simple operator inspection

## Slice 2: Canonical Config Model

This is the largest refactor and should land before the full `murphy init` overhaul.

Add `.murphy/config.toml` as the canonical local config file.

Recommended top-level structure:

```toml
[agent]
name = "Murphy"
default_channel_id = "C1234567890"
slack_user_id = "U1234567890"

[slack]
user_token = "xoxp-..."
app_name = "Murphy Agent"
app_description = "Self-hosted Slack supervisor for long-running AI work"

[workers]
max_concurrent = 2
backend = "codex"

[workers.codex]
command = "codex exec --yolo --ephemeral --skip-git-repo-check -"
chatgpt_project = "Murphy"

[developer_review]
backend = "claude"

[developer_review.claude]
command = "claude -p --dangerously-skip-permissions --mcp-config src/config/claude_mcp.json"

[tribune]
enabled = false
command = "gemini -m gemini-3.1-pro-preview -p '' -y --output-format text"
fallback_models = ["gemini-3-flash", "gemini-2.5-flash"]
```

The exact TOML schema can move, but these values need stable typed ownership:

- agent/public identity
- Slack credentials and manifest fields
- worker backend selection and backend config
- developer-review backend selection and backend config
- concurrency
- optional Tribune settings

Implementation note:

- Python 3.11 has `tomllib`, but this repo supports Python 3.9. Use a small dependency such as `tomli` for reads and emit TOML directly, or vendor a minimal writer. Avoid a broad dependency jump just to write TOML.

### Projection Helpers

Move these concerns out of `bootstrap.py` into `local_config.py`:

- render `.env`
- render `.codex/config.toml`
- render `src/config/claude_mcp.json`
- render `slack-app-manifest.json`
- initialize `.agent/memory/memory.md` if absent
- initialize `.agent/memory/long_term_goals.md` if absent

The projection contract should be:

- canonical config is the only source of local intent
- generated files are derived artifacts
- `--force` controls destructive overwrites
- migration/import is explicit when conflicts are detected

### Import Existing Installs

Implement import logic before changing init UX.

Read from:

- `.env`
- `.codex/config.toml`
- `src/config/claude_mcp.json`

Importable values include:

- `AGENT_NAME`
- `DEFAULT_CHANNEL_ID`
- `MAX_CONCURRENT_WORKERS`
- `WORKER_CMD`
- `DEV_REVIEW_CMD`
- Slack token from `.codex/config.toml`
- manifest-ish defaults if discoverable, otherwise fallback to current defaults

Conflict handling:

- if values disagree across local files, do not silently choose one
- print a conflict summary
- require explicit confirmation in init, or a `--force` / `--prefer` style flag in non-interactive mode

## Slice 3: `murphy init` Overhaul

Once `.murphy/config.toml` exists, rework `src/loop/bootstrap.py` into the onboarding command behind both:

- `murphy init`
- `murphy-init`

Target behavior:

- interactive by default
- non-interactive when flags provide all required inputs
- writes canonical config first
- regenerates projections second
- runs doctor third

Prompt/order for interactive mode:

1. app name
2. app description
3. agent name
4. Slack user token
5. default channel ID
6. optional explicit agent user ID override
7. max concurrent workers
8. worker backend
9. worker backend settings
10. developer-review backend
11. developer-review backend settings
12. optional Tribune settings

Keep support for already-shipped flags:

- `--agent-name`
- `--slack-app-name`
- `--slack-app-description`
- `--default-channel-id`
- `--chatgpt-project`
- `--manifest-path`
- `--repo-root`
- `--force`

Important fix to fold in while touching init:

- `src/loop/bootstrap.py` currently advertises that `--agent-name` defaults to `--slack-app-name` when the app name is a single word, but code still hard-defaults to `"Murphy"`.
- See `docs/dev/issues/2026-04-19-agent-name-default-mismatch.md`.
- Either implement the documented fallback or remove the misleading help text. The better choice is to implement it as part of the init rewrite.

## Slice 4: `murphy config`

Add `murphy config` under `src/loop/cli.py` once the canonical config exists.

Initial subcommands:

- `murphy config show`
- `murphy config show --effective`
- `murphy config set <key> <value>`
- `murphy config unset <key>`
- `murphy config doctor`
- `murphy config sync`

Suggested semantics:

- `show`
  - dump canonical `.murphy/config.toml`
- `show --effective`
  - show merged runtime-effective settings after defaults from `src/config/supervisor_loop.conf`
- `set` / `unset`
  - operate on typed canonical keys such as:
    - `agent.name`
    - `agent.default_channel_id`
    - `workers.max_concurrent`
    - `workers.backend`
    - `workers.codex.chatgpt_project`
- `doctor`
  - validate required local state:
    - Slack token present
    - `tmux` installed
    - selected worker/developer-review binaries resolvable
    - generated projections match canonical config
- `sync`
  - regenerate projections from `.murphy/config.toml`
  - support import mode if canonical config is absent

## Runtime Integration Notes

The roadmap does not require replacing env-based runtime config immediately. Keep runtime loading incremental.

Recommended bridge:

1. canonical `.murphy/config.toml`
2. generated `.env` and `.codex/config.toml`
3. existing `Config` class continues to read env + `supervisor_loop.conf`

This avoids a high-risk simultaneous runtime rewrite.

Only after the CLI stabilizes should developer review consider whether runtime should read `.murphy/config.toml` directly.

## Test Coverage to Add

Add or expand tests in:

- `src/loop/tests/test_bootstrap_cli.py`
- `src/loop/tests/test_cli_lifecycle.py` (new)
- `src/loop/tests/test_local_config.py` (new)

Minimum cases:

1. `murphy init` delegates to current bootstrap behavior in slice 1
2. `murphy start` resolves repo root from explicit `--repo-root`
3. `murphy start` refuses duplicate tmux session
4. `murphy restart` returns a useful error when heartbeat is missing
5. `murphy restart` sends `SIGHUP` to the pid in heartbeat
6. `murphy status` parses heartbeat happy path
7. canonical config renders `.env`
8. canonical config renders `.codex/config.toml`
9. canonical config renders `src/config/claude_mcp.json`
10. canonical config renders manifest
11. import-existing creates canonical config from current files
12. import-existing surfaces disagreement instead of silently choosing
13. single-word `--slack-app-name` defaults the agent name correctly if `--agent-name` is omitted

Mocking guidance:

- mock `subprocess.run` for `tmux`
- mock `os.kill` for restart
- use temp directories with real heartbeat JSON fixtures

## Recommended Delivery Order

If developer review wants the fastest path to user-visible value, ship in two PRs:

1. PR 1
   - add `murphy`
   - add `start`, `restart`, `status`
   - keep `murphy-init` behavior otherwise unchanged
   - add lifecycle tests
2. PR 2
   - canonical `.murphy/config.toml`
   - import/projection helpers
   - `murphy init` overhaul
   - `murphy config`
   - fix `--agent-name` default mismatch

This preserves the roadmap’s sequencing while avoiding one oversized review.
