## Slack protocol
- Never mention your own Slack ID in any message.
- When sending link-heavy Slack replies (for example, paper lists), prefer plain-text messages with raw URLs and keep each title adjacent to its URL; avoid mrkdwn `<url|label>` formatting when reliable highlighting/clickability matters.
- If the user references an attached file (for example, "this file", "attached PDF") and the file content is not in message text, use Slack MCP to inspect thread attachment metadata and call `attachment_get_data` for the relevant `AttachmentIDs`. If download or parsing fails (for example, `missing_scope`, permission denied, size limit, unreadable binary), ask for help in-thread and set outcome status to `waiting_human`.
- When delivering substantive research content (proofs, literature reviews, experiment reports, write-ups, theoretical analyses), compile to a typeset PDF using LaTeX (`pdflatex`) and upload as an in-thread Slack attachment. Short factual answers, status updates, and coordination messages stay as plain Slack text.
- Artifact storage (internal bookkeeping — do NOT surface these paths in Slack messages):
  - If the work is scoped to a project repo under `projects/<slug>/`, save artifacts under that project first (for example, `projects/<slug>/outputs/<run_label>/...`) and treat that as the canonical location.
  - In project folders, use readable, descriptive names. Common domain abbreviations are fine (`sgd`, `gd`, `k4`, `lr`) but avoid opaque encodings. Never use epoch timestamps, Slack thread IDs, or compact ISO timestamps (e.g., `20260226T092303Z`) as directory names. For experiment sweep directories, prefer descriptive labels over encoded parameter grids (e.g., `dim_100_to_380/` not `etaO_0p45_etaF_0p22_steps700/`).
  - For non-project tasks, save artifacts under `deliverables/<thread_ts>/...`.
  - Update `reports/<thread_ts>.md` with what was generated and where it is stored.
- Slack replies must contain only what your collaborator needs — findings, answers, decisions requested. Do NOT include local file paths, commit hashes, submodule status, report update notices, or other internal bookkeeping. They read Slack on a phone/desktop and cannot use filesystem paths.
- Slack message discipline:
  - One message per logical update. Do not send a completion message followed by a separate file-link message for the same artifact.
  - Do not ask for confirmation unless the situation is genuinely ambiguous. If your collaborator gave a clear directive, execute it.
  - Do not re-ask a question your collaborator already answered.
  - Do not announce internal housekeeping (checkpoint commits, report updates, submodule wiring, memory writes).
  - When decisions about infrastructure (submodule remotes, repo visibility, etc.) have a reasonable default, use the default silently. Only escalate when the choice materially affects your collaborator's workflow.
- In all notes/docs you write (`reports/`, `deliverables/`, `projects/`, `.agent/memory/memory.md`, `.agent/memory/long_term_goals.md`), use explicit UTC timestamps in `YYYY-MM-DD HH:MM UTC` format. Convert Unix epoch timestamps to this format; raw epoch may be included in parentheses when needed.
- Always reply in the task's `thread_ts`. Do not start new threads unless the task requires it.
- During proactive long-term-goal work (no Slack task), send concise Slack updates when useful: asking for human intervention, reporting new ideas, or reporting issues/risks. Prefer the most relevant existing thread and throttle to avoid spam.

**Related docs:** outcome JSON schema in `docs/schemas.md` · file layout in `docs/persistent-files.md` · git rules in `docs/workflows/git-workflow.md`
