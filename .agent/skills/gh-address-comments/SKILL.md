---
name: gh-address-comments
description: "Address pending review comments on a GitHub PR. Triggers when the user asks to respond to PR review feedback, resolve review threads, fix issues raised in PR comments, or address GitHub discussion points on a pull request."
---

# Address PR Review Comments

Fetch, triage, and fix review comments on a GitHub PR using `gh` CLI and the bundled `scripts/fetch_comments.py` helper.

## Prerequisites

Ensure `gh` is authenticated: run `gh auth status` first. If not logged in, prompt the user to run `gh auth login`.

## Workflow

1. **Fetch comments.** Run `scripts/fetch_comments.py` (relative to this skill directory) to print all review threads and comments on the PR for the current branch.

2. **Summarize and number.** Present each review thread/comment as a numbered list with a short summary of what fix would be required. Include the file path and line number for each.

3. **Ask which to address.** Ask the user which numbered comments they want fixed. Wait for their selection before proceeding.

4. **Apply fixes.** For each selected comment, make the code change, then move on to the next. After all fixes, commit with a message referencing the addressed comments.

5. **Verify.** Run `gh pr view --json reviewDecision,reviews` to confirm the comment state after pushing fixes.

## Notes

- If `gh` hits auth or rate-limit issues mid-run, prompt the user to re-authenticate with `gh auth login`, then retry.
- The script requires the current branch to have an open PR. If no PR is found, inform the user.
