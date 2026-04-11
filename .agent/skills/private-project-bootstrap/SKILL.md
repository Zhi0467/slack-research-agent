---
name: "private-project-bootstrap"
description: "Use when creating a new Murphy-owned private GitHub repo for a project, including repo creation, collaborator invite, feature branch setup, PR creation, and root coordination artifact registration. Triggers on: create new project repo, bootstrap project, set up private repo, share repo with collaborator."
---

# Private Project Repo Bootstrap with Collaborator Invite

## Procedure

1. **Create local project repo:**
   ```bash
   mkdir -p projects/<slug>
   cd projects/<slug>
   git init -b main
   ```
   Use descriptive slugs (e.g., `heavy-tail-optimizer-generalization`, not `ht-opt-gen`).

2. **Create GitHub repo (private by default):**
   ```bash
   gh repo create murphytheagent/<slug> --private --source=projects/<slug> --remote=origin --push
   ```

3. **Resolve collaborator identity.** If only an email was provided, resolve the GitHub account first:
   ```bash
   gh api search/users -q '.items[0].login' -f q="<email> in:email"
   ```
   Do not guess GitHub usernames from email addresses.

4. **Push bootstrap commit.** Ensure `main` has at least one commit so the default branch anchor exists.

5. **Create feature branch** for implementation work:
   ```bash
   git checkout -b feat/<description>
   # ... add implementation
   git add . && git commit -m "Initial implementation"
   git push -u origin feat/<description>
   ```

6. **Open a PR** against `main` and add an `@codex` comment for review.

7. **Send collaborator invite** (read access):
   ```bash
   gh api repos/murphytheagent/<slug>/collaborators/<login> -X PUT -f permission=pull
   ```

8. **Register in root coordination artifacts.** Workers cannot register submodules — file a developer request in `docs/dev/issues/` for submodule registration. Update:
   - `projects/<slug>.md` — project tracking doc
   - `.agent/projects/<slug>.json` — project metadata
   - `.agent/memory/long_term_goals.md` — if the project represents a tracked goal

## Gotchas

- Workers cannot edit `.gitmodules` or register submodules from parallel worktrees. File a developer issue for this step.
- Always create the repo as private first. Public visibility is a deliberate later decision.
- Push a minimal bootstrap commit to `main` before creating feature branches — avoids orphan branch confusion.
- The `gh repo create` command with `--source` handles both `git init` and initial push in one step.
