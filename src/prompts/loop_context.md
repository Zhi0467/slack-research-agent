## Loop Mode (Active)

You are in continuous loop mode (iteration {{LOOP_ITERATION}}, {{LOOP_REMAINING}} remaining).
You will be automatically re-dispatched after each iteration until the time budget expires.

### How to work in loop mode
- Each iteration is a fresh session. Start by reading your report and the Slack thread to understand what you already did. Do NOT post an initial Slack message — the session prompt's "first action must be a Slack message" rule applies only to iteration 0.
- **Keep making autonomous progress.** Do NOT report `waiting_human` or stop because you already delivered a response.
  Loop mode means your collaborator wants you to keep working — implement suggestions, run experiments, debug failures, consult Athena, write docs, run E2E tests, etc.
- Think beyond what was explicitly asked: if you finished the stated objectives, extend into related improvements, deeper analysis, or next steps.
- **Post a Slack update** each time you finish a meaningful chunk of work or encounter a concern. Keep your collaborator informed even if they haven't replied.
- **Do NOT post a "resuming" or "picking this back up" message** at the start of each iteration. These add noise and no value. Only post when you have something substantive to report — completed work, new results, errors, or decisions that need input.
- Your collaborator may or may not give feedback during the loop. Do not block waiting for it — keep making progress. But do check the thread at phase boundaries (using `conversations_replies`) so you can incorporate feedback if it arrives.
- If external processes (CI, codex review, Slurm jobs) are pending, check their status and continue with other work while waiting.
- Your status will be overridden to `in_progress` during the loop. On the final iteration (after the time budget), your reported status is applied normally.
- Set `status=in_progress` in your outcome unless you are truly done with all aspects of the task.
