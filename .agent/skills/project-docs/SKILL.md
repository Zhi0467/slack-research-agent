---
name: "project-docs"
description: "Use when initializing a new project OR updating an existing project's documentation. Covers creating projects/<slug>/ with AGENTS.md pointer file, docs/ tree for durable knowledge, roadmap.md with milestone gates, and backlog.md for open tasks. Also use after significant work to update docs/, roadmap gates, backlog, or AGENTS.md. Triggers on: 'start a new project', 'initialize project', 'update project docs', 'update roadmap', 'update backlog', or when project docs are stale or missing."
---

# Project Initialization and Doc Maintenance

## New Project Setup

1. **Create `projects/<slug>/`** and register as a git submodule:
   ```bash
   mkdir -p projects/<slug>
   scripts/register_submodules.sh projects/<slug>
   ```
   Use descriptive slugs: `heavy-tail-optimizer-generalization`, not `ht-opt-gen`.

2. **Create `AGENTS.md`** as a pointer file (not a monolith):
   - Brief description of what the project is and its current focus
   - Pointers to `roadmap.md`, `backlog.md`, and `docs/` for details
   - Project-specific sub-session instructions (coding conventions, test commands, key constraints)
   - Keep it short — sub-sessions load this automatically

3. **Create `roadmap.md`** with milestone-based structure:
   - 2-5 milestones with clear, testable success criteria
   - "Current Status" section at the top for quick orientation
   - Gate conditions that must pass before advancing
   - Each milestone gets a chronological **activity log** section as work progresses (see Documentation Standards below)

4. **Create `backlog.md`** for open tasks and work items:
   - Open tasks, TODOs, known issues, and planned work
   - Format is up to the project (checklist, table, prose — whatever fits)
   - Separate from `roadmap.md` which tracks milestones and activity history

5. **Create `README.md`** (for Murphy-owned projects, not upstream forks):
   - What this project is — scope, research question, or goal
   - Current state — high-level status (point to `roadmap.md` for details)
   - Where to find things — key artifacts, outputs, documentation pointers
   - Beyond these requirements, add whatever serves the project

5. **Create `docs/` tree** for durable project knowledge:
   - `docs/README.md` — index of what each doc covers
   - Start minimal, grow organically as the project develops

6. **Update coordination artifacts** — root-repo project summary, memory, long-term goals.

## Updating Existing Project Docs

Use this after significant project work (implementation sessions, research findings, milestone completion).

**When to update:** milestone gates passed, new design decisions made, significant findings, or sub-session instructions changed.

**When to skip:** trivial one-file changes, no new knowledge produced, read-only tasks.

Steps:
1. **Update `roadmap.md`** — mark completed gates, advance current milestone, add activity log entries
1b. **Update `backlog.md`** — mark completed items, add new tasks or issues discovered during work
2. **Update or add `docs/` entries** — capture new findings, design decisions, specs, or references as durable docs
3. **Update `docs/README.md` index** — keep it current with any new docs added
4. **Update `AGENTS.md`** if the project focus or sub-session instructions have changed
5. **Prune stale content** — remove outdated docs, update inaccurate references

### What goes in `docs/` vs elsewhere

| Content | Location |
|---------|----------|
| Durable knowledge (design decisions, specs, references) | `docs/<topic>.md` |
| Milestone tracking and activity logs | `roadmap.md` |
| Open tasks, TODOs, work items | `backlog.md` |
| Sub-session instructions and project overview | `AGENTS.md` |
| Generated artifacts, PDFs, plots | `outputs/` |
| Ephemeral task context, thread history | Root-repo `.agent/` (not in project) |

## Documentation Standards

### `roadmap.md` — the activity record

`roadmap.md` must include:
- **Milestones** with success/gate criteria
- **Chronological activity log entries** under each milestone — dated (UTC), factual, concise. These entries are append-only and record what happened: experiments run, results obtained, approach changes, decisions made, consultations held.
- **Athena consultation entries** must include: mode, number of turns if multi-turn, brief summary of the question and key insight, and a pointer to the full conversation record (`.agent/runtime/consult_history/{task_id}.jsonl`).
- A `Last updated` timestamp that stays current.

Beyond these requirements, organize `roadmap.md` however best fits the project.

### `README.md` (project root) — the project overview

For Murphy-owned projects (not upstream forks where the README belongs to the original repo):
- **What this project is** — scope, research question, or goal
- **Current state** — high-level status, not a chronological log (that's `roadmap.md`)
- **Where to find things** — key artifacts, outputs, documentation pointers

For forked repos, Murphy's project documentation lives in `AGENTS.md`, `roadmap.md`, and `docs/` — the upstream README is left intact.

Beyond these requirements, add whatever serves the project.

## Example: Pointer-file pattern

The root repo itself follows this pattern — `AGENTS.md` is a short pointer file, `docs/` holds all detailed knowledge:

```
AGENTS.md                              # ~90 lines: behavior rules + pointers
docs/
  protocols/
    slack-protocols.md                 # messaging rules, ACK, outcome status
    maintenance-protocols.md           # maintenance task rules
  workflows/
    research-workflow.md               # research task sequence
    git-workflow.md                    # commit rules, submodule management
    project-workflow.md                # project sub-session pattern
    remote-gpu-workflow.md             # GPU node constraints
  mcp-integrations.md                  # external tool usage
  persistent-files.md                  # file layout and artifacts
  schemas.md                           # data schemas
  operations.md                        # system operations guide
```

`AGENTS.md` tells the reader *what to do* and *where to look*. `docs/` holds the *how* and *why*.

### Example project AGENTS.md

```markdown
# Project: <name>

<1-2 sentence description and current focus.>

## Key Docs
- `roadmap.md` — milestones, current status, gate conditions, activity logs
- `backlog.md` — open tasks, work items, known issues
- `docs/` — references, design decisions, specs (see `docs/README.md`)

## Sub-Session Instructions
- Install: `uv sync`
- Test: `pytest tests/ -v`
- Coding style: <brief conventions>
- Commit style: short imperative messages
- Do NOT communicate on Slack — parent worker handles all Slack I/O

## Context Loading
Read the docs relevant to your task before starting:
- New to this project? → `roadmap.md` then `docs/README.md`
- Implementing a milestone? → `roadmap.md` for gate conditions
- Need domain context? → `docs/<relevant-topic>.md`
```

Keep AGENTS.md under ~40 lines. If you're writing more, move content to `docs/`.

## Project Structure

```
projects/<slug>/
  README.md            # project overview: scope, state, artifact locations (Murphy-owned projects)
  AGENTS.md            # pointer file: what this is, where to look, sub-session instructions
  roadmap.md           # milestones with gate conditions and chronological activity logs
  backlog.md           # open tasks, work items, known issues
  docs/
    README.md          # index of docs
    <topic>.md         # durable knowledge files
  src/                 # source code (structure varies)
  outputs/             # generated artifacts, reports, PDFs
```

## Remote Policy

- Default to private repos unless the human explicitly requests public
- Create GitHub remote at init if the project will have PRs or collaboration
- If deferring: document the reason and trigger condition in `roadmap.md`

## Implementation Work

After initialization, use the project sub-session workflow for implementation work — see `docs/workflows/project-workflow.md` for how to spawn sub-sessions, review changes, and commit. The sub-session auto-loads `AGENTS.md` from the project directory.

## Gotchas

- `AGENTS.md` is a pointer file — keep it short. Put detailed knowledge in `docs/`
- Do not regenerate scaffold if the project already exists — update instead
- `docs/` grows with the project. Don't front-load docs you don't have yet
- After init, use `docs/workflows/project-workflow.md` for implementation work
