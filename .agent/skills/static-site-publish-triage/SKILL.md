---
name: "static-site-publish-triage"
description: "Use when a hosted static site (GitHub Pages or similar) looks wrong or stale despite local fixes appearing correct. Covers diagnosing publish-state mismatches where local preview is fixed but live site is stale. Triggers on: site still looks wrong, GitHub Pages stale, live site not updated, publish mismatch, deployment not reflecting changes."
---

# Static Site Publish Mismatch Triage

## Procedure

1. **Reproduce locally.** Render the local route from the working tree and capture the current state (screenshot or specific markers like page title, labels, visible text).

2. **Reproduce on the live site.** Open the public hosted URL in a fresh browser session. Compare concrete markers against local state.

3. **If local and live differ, inspect the project repo branch state:**
   - Current checked-out branch
   - Host publishing branch (e.g., GitHub Pages source branch)
   - Any open PR carrying the intended fix
   - Whether the fix exists only on a feature branch

4. **Treat this as a delivery-state bug first, not a content bug.** The most common cause is the fix existing on a feature branch while the publishing branch (usually `main`) still has old content.

5. **Move the fix onto the publishing branch** using the repo's preferred delivery path:
   - Merge the PR, or
   - Cherry-pick the fix to the publishing branch
   - Return the checkout to the publishing branch if needed

6. **Verify the live site.** Re-open the public URL in a fresh browser session AND fetch raw HTML to confirm the hosted route now serves the new build. Do not trust cached pages.

7. **Record the root cause** and the publish-branch commit in the task report. This prevents future sessions from misreading a local preview as live evidence.

## Key Principle

A local working-tree preview is NOT evidence that the live site is fixed. Always verify the public URL after any fix. The publishing branch is the source of truth, not the local checkout.

## Common Root Causes

- Project left on a feature branch while GitHub Pages serves from `main`
- PR merged but checkout not returned to `main`, so publisher keeps serving old snapshot
- Publisher process running stale code (restart needed after exporter changes)
- CDN/browser cache serving old content (use incognito + raw fetch to verify)
