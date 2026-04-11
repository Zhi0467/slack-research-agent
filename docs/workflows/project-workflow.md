# Project Sub-Session Workflow

Use a project sub-session when a task requires multi-file implementation work inside a specific project where the project's `AGENTS.md` would add meaningful context.

## When to Use

- Multi-file code changes inside `projects/<slug>/`
- Project has its own `AGENTS.md` or `CLAUDE.md` with domain-specific guidance
- Implementation benefits from focused project context (not just root-repo instructions)

**Skip for:** simple one-file edits, read-only tasks, tasks where project docs aren't relevant.

## How to Use

### 1. Plan and Write a Focused Prompt

Create a temp file with specific, actionable instructions:

```bash
cat > /tmp/project_prompt.md << 'EOF'
[Task description with specific instructions]

Context:
- [Prior findings, constraints, what Athena said]
- [Relevant code paths or file references]

Instructions:
- [Specific changes to make]
- Commit all changes with descriptive messages
- Describe results and any issues in your final message
- Do NOT communicate on Slack — the parent worker handles all Slack I/O
EOF
```

### 2. Launch the Sub-Session

```bash
scripts/project_worker.sh <slug> /tmp/project_prompt.md [--timeout <seconds>]
```

Default timeout: 4 hours. Parent worker has a 6-hour session window.

### 3. Check Results

```bash
# Exit code: 0 = success, 1 = failure, 2 = bad args, 124 = timeout
echo $?

# Full output in the results file (path printed by script)
# Last 200 lines shown on stdout
```

### 4. After the Sub-Session

1. Quick-check changes: `git -C projects/<slug>/ log --oneline -5` and `git -C projects/<slug>/ diff HEAD~1`
2. **(Optional) Run `codex review` for quality gating** — especially useful for multi-file changes, unfamiliar codebases, or high-confidence tasks:
   ```bash
   # Review uncommitted work (before sub-session committed)
   scripts/review_project.sh <slug>

   # Review the last commit (most common after sub-session)
   scripts/review_project.sh <slug> --commit HEAD

   # Review all changes against a branch
   scripts/review_project.sh <slug> --base main
   ```
   Read the `REVIEW_FILE` path printed at the end. Fix issues or re-dispatch sub-session if needed.
3. Retry with revised prompt if sub-session failed, produced incomplete work, or review flagged issues
4. **(Optional) Update project docs** — if the sub-session produced significant findings, new design decisions, or milestone progress, update `roadmap.md`, `docs/`, and `AGENTS.md` as needed. See the `project-docs` skill for what goes where.
5. Update root-repo submodule pointer: `git add projects/<slug>`
6. Report findings to the Slack thread

## Sub-Session Capabilities

| Has Access | Does NOT Have |
|---|---|
| Project's `AGENTS.md` / `CLAUDE.md` (auto-loaded from CWD) | Slack MCP (parent handles all Slack I/O) |
| `consult` MCP (Athena) for research questions | Root-repo `.agent/` state files |
| Web search | |
| Full shell access (`--yolo` + `danger-full-access`) | |

## Prompt Guidelines

- **Be specific:** include concrete file paths, function names, test commands
- **Provide context:** prior findings, Athena recommendations, constraints
- **Set expectations:** "commit changes", "run tests", "describe results in final message"
- **No Slack:** always include "Do NOT communicate on Slack — parent handles all Slack I/O"

## Edge Cases

| Case | Handling |
|---|---|
| Project doesn't exist yet | Create project + register submodule first (existing workflow), then spawn sub-session |
| Project has no AGENTS.md | Works fine — codex loads nothing extra, parent prompt provides all context |
| Sub-session fails | Code changes persist on disk. Check exit code, read results, retry once or report failure |
| Sub-session timeout | Default 4h via `timeout` wrapper. Script exits 124. |
| Parent killed mid-sub-session | Sub-session dies (child process). Code changes on disk persist. Supervisor reconciles as incomplete. |
| Large output | Script captures full log to disk, returns only last 200 lines to parent |
