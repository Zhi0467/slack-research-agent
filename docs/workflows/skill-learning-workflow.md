# Skill Learning Workflow

Murphy automatically identifies reusable workflow patterns from recurring tasks and captures them as Codex-native skills. Skills are discovered and injected by Codex at session startup — no custom matching logic needed.

## When to Create a Skill

Create a skill when you observe:
- A multi-step procedure performed in two or more separate tasks
- Domain expertise worth preserving (non-obvious workflows, gotchas, specific toolchains)
- A pattern the user has explicitly requested in a consistent way across tasks

Do NOT create a skill for:
- One-off procedures unlikely to recur
- Project-specific steps with no cross-project reuse
- Knowledge Codex already has (common CLI commands, standard library usage)

Before creating a new skill, check `.agent/skills/` for existing skills that partially cover the pattern. Update an existing skill rather than creating a duplicate.

## Codex Skill Format

Each skill is a directory under `.agent/skills/<skill-name>/`:

```
.agent/skills/<skill-name>/
├── SKILL.md          (required)
├── agents/
│   └── openai.yaml   (recommended)
├── references/       (optional — detailed docs loaded on demand)
└── scripts/          (optional — deterministic helpers)
```

### SKILL.md

The only required file. Has two parts:

**1. YAML frontmatter** — always in Codex's context (~100 words). Only two fields:

```yaml
---
name: "<skill-name>"
description: "<what the skill does AND when Codex should activate it>"
---
```

The `description` is the **primary trigger** — Codex reads it to decide whether to activate the skill for a given task. It must include both what the skill does and specific contexts/triggers for when to use it. This is the most important field to get right.

**2. Markdown body** — loaded only after the skill triggers. Contains the procedural workflow, steps, gotchas, and references. Keep under 500 lines; use `references/` for detailed content.

### agents/openai.yaml (recommended)

UI metadata for skill display:

```yaml
interface:
  display_name: "<Human-Readable Name>"
  short_description: "<Brief tagline>"
  default_prompt: "<Default prompt when skill is invoked directly>"
```

### Naming Conventions

- Lowercase letters, digits, and hyphens only
- Under 64 characters
- Prefer short, verb-led phrases: `project-initialization`, `benchmark-sweep`, `research-report`
- Namespace by tool when it improves clarity: `gh-address-comments`, `slurm-training-job`

## Storage and Discovery

Skills are version-controlled at `.agent/skills/<skill-name>/` in the root repo. Codex discovers skills from `~/.codex/skills/`, so per-skill symlinks bridge the two:

```
~/.codex/skills/<skill-name>  →  <repo-root>/.agent/skills/<skill-name>/
```

The `.system/` directory stays at `~/.codex/skills/.system/` (Codex-internal, not version-controlled).

### Adding a new skill

1. Create the skill directory at `.agent/skills/<skill-name>/`
2. Create the symlink: `ln -s $(pwd)/.agent/skills/<skill-name> ~/.codex/skills/<skill-name>`
3. Commit the skill: `git add .agent/skills/<skill-name> && git commit`
4. Update the skills inventory table below

### Removing a skill

1. Remove the symlink: `rm ~/.codex/skills/<skill-name>`
2. Remove the directory and commit: `git rm -r .agent/skills/<skill-name> && git commit`
3. Remove the entry from the skills inventory table below

## Skills Inventory

| Skill | Origin | Description |
|---|---|---|
| code-review | Murphy | Independent code review via `codex review` with CWD handling and branch sync |
| doc | Codex installer | Read, create, and edit `.docx` documents with python-docx and rendering |
| gh-address-comments | Codex installer | Address review/issue comments on GitHub PRs using gh CLI |
| gh-fix-ci | Codex installer | Debug and fix failing GitHub Actions PR checks |
| jupyter-notebook | Codex installer | Create, scaffold, and edit Jupyter notebooks from templates |
| openai-docs | Codex installer | Look up OpenAI product/API documentation with citations |
| pdf | Codex installer | Read, create, and review PDFs with Poppler and Python tools |
| playwright | Codex installer | Browser automation via playwright-cli (navigation, screenshots, extraction) |
| project-docs | Murphy | Initialize or update project docs, roadmap milestones, and AGENTS.md |
| project-pr-readiness-audit | Murphy | Audit PR merge readiness: local review, thread check, classification |
| screenshot | Codex installer | OS-level desktop/window/region screenshots |
| security-best-practices | Codex installer | Language/framework-specific security review (Python, JS/TS, Go) |
| visualization-task | Murphy | Mermaid-first diagram/chart/figure generation with QA and delivery |

## Example: Learnable Skill from Recurring Pattern

### Observed pattern

The user asked across multiple tasks to initialize projects with a specific structure: create `docs/` for repo knowledge, create `roadmap.md` with milestones, define success criteria per milestone, enforce stage gates so the agent never advances without passing checks.

### Resulting skill

`.agent/skills/project-initialization/SKILL.md`:

```markdown
---
name: "project-initialization"
description: "Use when initializing a new project, repository, or research workspace.
Covers creating documentation structure for repo knowledge management, milestone-based
roadmap with clear success boundaries, and stage-gate enforcement so the agent never
advances without passing defined checks."
---

# Project Initialization

## Workflow
1. Create `docs/` directory for centralized repo knowledge management.
2. Create `roadmap.md` at project root with milestone-based structure.
3. For each milestone:
   - Define clear, testable success criteria.
   - Break into manageable sections (3-5 per milestone).
   - Specify checks that must pass before advancing to the next stage.
4. Create initial documentation for the first milestone.

## Gotchas
- Never advance to the next milestone without verifying all success criteria.
- Keep milestones small enough to be achievable but meaningful.
- Document both the "what" and "why" for each stage boundary.
```

## Progressive Disclosure

Keep SKILL.md lean. If the skill covers multiple variants or has extensive reference material, split it:

- Core workflow and decision logic stays in SKILL.md
- Variant-specific details go in `references/<variant>.md`
- Reference files from SKILL.md so Codex knows they exist: "See `references/aws.md` for AWS-specific steps."

## Lifecycle

1. **Observation**: Workers notice recurring patterns during task execution and log skill candidates to `docs/dev/plans/` with enough context (pattern name, trigger conditions, step-by-step procedure, source tasks).
2. **Identification**: Maintenance phase 0 (reflect) reviews recent tasks, identifies additional patterns, and files skill candidates to `docs/dev/plans/`.
3. **Implementation**: Developer review (maintenance phase 1) reads skill candidates from `docs/dev/plans/`, creates the skill at `.agent/skills/<slug>/`, creates the symlink in `~/.codex/skills/`, commits to git, and deletes the plan file.
4. **Discovery**: Codex auto-discovers skills at session startup from `~/.codex/skills/` via symlinks to `.agent/skills/`.
5. **Refinement**: Skills are updated by developer review as new variations emerge or quality issues are found. All changes are version-controlled.
