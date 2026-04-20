# Unified Murphy CLI Roadmap

Created: 2026-04-19 23:54 UTC

Supersedes the exploratory direction in:

- `2026-04-19-config-cli.md`
- `2026-04-19-cli-start-restart.md`
- `2026-04-19-init-owns-config-phase.md`

Those earlier notes are useful background, but this file is the single source of truth for CLI direction going forward.

## Decisions

### 1. Who owns configuration?

`murphy init` owns first-run setup and the full configuration phase.

`murphy config` exists, but as a day-2 inspection and adjustment surface over the same canonical config. It does not compete with init; it complements it.

Concretely:

- `murphy init`
  - interactive first-run onboarding
  - can also run non-interactively from flags
  - writes canonical config
  - renders generated config files
  - runs validation/doctor checks at the end
- `murphy config`
  - day-2 edits and inspection
  - reads/writes the same canonical config
  - exposes `show`, `set`, `unset`, `doctor`, and `sync`

This resolves the earlier contradiction:

- Plan 1 was right that day-2 config needs a dedicated surface.
- Plan 3 was right that init must own end-to-end setup.

The correct model is not “pick one.” It is “init for onboarding, config for later operations, both backed by one config model.”

### 2. Canonical config path

Use `.murphy/config.toml`.

Reasoning:

- `.agent/` is already defined and documented as runtime state plus tracked skills.
- `.agent/*` is broadly gitignored except the skills subtree.
- `.agent/` is symlinked/shared across worktrees as part of runtime machinery.
- a user-owned canonical config file is conceptually different from runtime state and should not live under the runtime namespace if avoidable.

Implication:

- `.murphy/` should become the home for user-owned local control-plane config.
- `.agent/` remains runtime state, memory, tasks, logs, and skills.

### 3. Where does validation live?

Validation lives in one shared implementation, surfaced in two places:

- automatically at the end of `murphy init`
- explicitly via `murphy config doctor`

There should not be two different doctor implementations.

`murphy init` should call the same doctor logic after writing config so first-run setup fails early and clearly.

### 4. Does `murphy status` ship now or later?

It ships in the first lifecycle slice together with `murphy start` and `murphy restart`.

Reasoning:

- `murphy restart` is much easier to trust if there is a companion probe.
- `status` is low-complexity because the runtime already writes `.agent/runtime/heartbeat.json`.

## Command Model

### Setup and config

- `murphy init`
- `murphy config show`
- `murphy config set <key> <value>`
- `murphy config unset <key>`
- `murphy config doctor`
- `murphy config sync`

### Lifecycle

- `murphy start`
- `murphy restart`
- `murphy status`
- `murphy logs`

### Compatibility

- `murphy-init` remains as a compatibility alias to `murphy init`

Alias lifetime:

- keep it for one release cycle after `murphy` is introduced
- document it as deprecated immediately
- remove it only after docs, tests, and examples have migrated

The recently added `--agent-name` behavior and flags should carry forward unchanged under `murphy init`.

## Ownership of Data

### Canonical source of truth

`/.murphy/config.toml`

This should store structured local intent such as:

- agent name
- Slack token
- default channel ID
- optional explicit agent user ID override
- worker backend
- worker backend settings
- developer-review backend settings
- max concurrent workers
- Tribune settings
- dashboard settings
- consult MCP settings

### Generated projections

These become projections from canonical config plus repo-local path discovery:

- `.env`
- `.codex/config.toml`
- `src/config/claude_mcp.json`
- `slack-app-manifest.json`

The projections remain user-visible, but are no longer the primary edit surface.

## `murphy init`

`murphy init` should gather the normal first-run inputs end-to-end:

- agent name
- Slack app name and description
- Slack token
- default channel ID
- optional explicit agent user ID override
- max workers
- worker backend
- worker backend settings
- developer-review backend selection/settings
- optional Tribune settings
- optional consult MCP settings
- optional dashboard settings

It should support both:

- interactive mode
- non-interactive flags for automation

It should finish by:

1. writing `.murphy/config.toml`
2. rendering all generated config files
3. running doctor/validation
4. printing any remaining manual steps

## `murphy config`

`murphy config` is the explicit day-2 surface over the same data.

Recommended initial subcommands:

- `show`
- `show --effective`
- `set`
- `unset`
- `doctor`
- `sync`

Notes:

- `show --effective` should display the merged runtime config after defaults from `src/config/supervisor_loop.conf` are applied.
- `sync` should regenerate projections from `.murphy/config.toml`.
- backend selection belongs to the canonical config model and is surfaced in both `init` and `config`, not duplicated as separate ad hoc logic.

## `murphy start`

`murphy start` should own the standard tmux startup flow.

Required behavior:

1. resolve repo root even when invoked from another cwd
2. verify `tmux` exists
3. start detached tmux session `supervisor` by default
4. execute `./scripts/run.sh` from repo root
5. refuse to start a duplicate if the session already exists

Recommended flags:

- `--repo-root <path>`
- `--session <name>`
- `--attach`
- `--run-once`

Repo-root resolution:

- prefer explicit `--repo-root`
- otherwise detect via marker files such as `AGENTS.md`, `pyproject.toml`, and `scripts/run.sh`
- do not assume current cwd is the repo root

## `murphy restart`

`murphy restart` should wrap the existing hot-restart implementation.

Required behavior:

1. read `.agent/runtime/heartbeat.json`
2. extract PID
3. send `SIGHUP`
4. optionally wait briefly and report restart status

It should not kill the tmux session and recreate it in the first version.

If heartbeat is missing, it should say the supervisor does not appear to be running and suggest `murphy start`.

## `murphy status`

`murphy status` should ship with start/restart in the first lifecycle slice.

Minimum output:

- whether heartbeat exists
- PID
- last known status
- last heartbeat timestamp if available

That is sufficient for restart confidence and basic operator debugging.

## Migration for Existing Installs

This is required. Existing users already have manually-edited local files from `murphy-init`.

First-run behavior after the new CLI lands:

1. If `.murphy/config.toml` exists:
   - use it as the canonical source
2. If it does not exist:
   - inspect existing local files:
     - `.env`
     - `.codex/config.toml`
     - `src/config/claude_mcp.json`
   - import what can be inferred into a new `.murphy/config.toml`
   - report what was imported vs what still needs confirmation

This import path should be available both:

- implicitly from `murphy init`
- explicitly from `murphy config sync --import-existing` or equivalent

Migration rules:

- preserve existing local values whenever possible
- do not overwrite by surprise unless `--force` is passed
- warn when files disagree and require human choice

## Sequencing

The roadmap should ship in this order:

### Slice 1: Lifecycle commands

- shared `murphy` entrypoint
- `murphy start`
- `murphy restart`
- `murphy status`
- keep `murphy-init` as-is

Reason:

- this slice is implementable without blocking on canonical config migration
- it delivers immediate operator value

### Slice 2: Canonical config model

- add `.murphy/config.toml`
- add projection/render helpers
- add import-from-existing logic
- add projection round-trip tests

### Slice 3: `murphy init` overhaul

- convert init from generator to complete onboarding flow
- write canonical config first
- render projections
- call shared doctor logic
- keep non-interactive flags

### Slice 4: `murphy config`

- `show`
- `show --effective`
- `set`
- `unset`
- `doctor`
- `sync`

This order avoids blocking lifecycle improvements on the larger config refactor.

## Test Plan

The highest-risk area is canonical-config projection and migration. It needs explicit coverage.

Required tests:

1. canonical config -> `.env` render
2. canonical config -> `.codex/config.toml` render
3. canonical config -> `src/config/claude_mcp.json` render
4. canonical config -> manifest render
5. import existing install -> canonical config
6. disagreement detection between existing local files
7. `murphy init` interactive/non-interactive happy path
8. `murphy start` from non-repo cwd with explicit repo-root
9. `murphy start` duplicate-session behavior
10. `murphy restart` missing-heartbeat behavior
11. `murphy restart` valid-heartbeat SIGHUP path
12. `murphy status` basic heartbeat parsing

## Immediate Documentation Direction

After the full config redesign lands, the default README flow should become:

```bash
murphy init
murphy start
```

Manual edits to `.env`, `.codex/config.toml`, and `src/config/claude_mcp.json` should be documented as advanced escape hatches, not the primary workflow.
