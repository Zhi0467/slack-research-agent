Git branching (parallel dispatch):
You are working in a git worktree on branch `{{BRANCH_NAME}}`.

To stage files, always use the staging helper from the worktree root (handles symlinked paths reliably).
Do NOT run this from inside a symlinked directory like `.agent/` — it will be rejected:

    python3 {{REPO_ROOT}}/scripts/worktree_stage <files>
    git commit -m "your message"

For submodule pointer updates:

    python3 {{REPO_ROOT}}/scripts/worktree_stage projects/<slug>

Do NOT use `git add` directly — it silently fails for some paths in worktrees.
Do NOT use `git -C {{REPO_ROOT}}` for staging or commits — that targets the main repo on the `main` branch, not your worktree branch.

Before writing the dispatch outcome file, merge your branch into the main repo:

    git -C {{REPO_ROOT}} merge {{BRANCH_NAME}}

If the merge fails with a lock error (another worker is merging), wait 5 seconds and retry (up to 3 times).
If the merge has conflicts, resolve them by editing files under `{{REPO_ROOT}}/`,
then `git -C {{REPO_ROOT}} add <files>` and `git -C {{REPO_ROOT}} commit --no-edit`.
