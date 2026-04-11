---
name: "overleaf-git-sync"
description: "Use when a task requires syncing content with Overleaf via Git, including first-time shared project access, token generation, git transport verification, local clone bootstrap, push/pull sync, and web editor verification. Triggers on: Overleaf sync, Overleaf git, manuscript sync, paper sync with Overleaf, accept Overleaf invite, Overleaf access setup."
---

# Overleaf Git Sync and Project Bootstrap

This skill covers both first-time shared project bootstrap and steady-state Git sync with Overleaf.

## First-Time Project Bootstrap

1. **Confirm identity boundary.** Murphy must use Murphy-owned Overleaf/Gmail credentials and Murphy-owned Git token. Do not assume existing `git.overleaf.com` credentials are usable unless provenance metadata proves ownership.

2. **Handle invite acceptance.** Raw share links may only land on the Overleaf login page. The actual project-join path is in the Gmail invite email body. Use a headed browser session for Google sign-in (fresh headless OAuth can be rejected as insecure).

3. **Verify project access separately for:**
   - Web editor access (can you open and edit the project?)
   - Git transport access (does `git ls-remote` work with auth?)
   - A successful web login or token-generation page does NOT prove Git works.

4. **Mint and store a Murphy-owned Git token.** Generate from Murphy's Overleaf account settings. Store in Keychain with owner + generation metadata. Remove ambiguous duplicate tokens. On macOS, delete and re-add rather than `security add-internet-password -U` to avoid stale label metadata.

5. **Probe Git access directly.** Use authenticated `info/refs` or `git ls-remote` against the real Overleaf remote. If it returns 403, treat it as an account/project entitlement blocker — even if:
   - The project is visible on the Murphy dashboard
   - The account can generate a token
   - The editor shows web edit access

6. **Bootstrap local checkout.** Prefer:
   ```bash
   git init && git fetch <authenticated-url> master && git checkout -B master FETCH_HEAD
   ```
   when naive `git clone` leaves a bad HEAD. Use `git clone --branch master` only if it behaves cleanly.

## Steady-State Sync

1. **Pull remote changes first** before making local edits:
   ```bash
   git fetch origin master && git rebase origin/master
   ```

2. **Push local changes:**
   ```bash
   git push origin master
   ```

3. **Always verify in the web editor.** Open the project in Overleaf and confirm the exact change appears. A local commit is NOT proof that the shared draft is synced.

4. **Handle concurrent edits.** Re-fetch or rebase rather than force-pushing. Overleaf state can advance independently between local clone and push.

## Gotchas

- Account-side sharing or entitlement changes can flip the same token from 403 to 200 without token rotation. Re-probe after changes.
- Overleaf may let Murphy mint a token while the project still shows the premium Git teaser and returns 403 on transport.
- On macOS, avoid relying on the default credential-helper path during first auth challenge; use explicit auth headers or a temporary `GIT_ASKPASS` helper if needed.
- Distinguish the local project repo or mirror from the real Overleaf remote. Some project repos keep a local bare origin that is NOT the Overleaf remote.
- A minimal reversible probe edit (push, verify in web, remove, verify cleanup) is a reliable sync-verification technique.
