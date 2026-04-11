Maintenance checklist — Phase 0: Reflect

Your job is to review your recent work, maintain documentation and planning artifacts, and tidy state. You do NOT fix source code bugs or modify system infrastructure — that is the developer review's job (phase 1, dispatched automatically after you finish).

1) Review recent work across `reports/`, `projects/`, `deliverables/`, recent code changes, and recent Slack thread interactions. This gives you context for all subsequent steps.

2) Update all memory artifacts comprehensively:
   - `.agent/memory/memory.md` — flush durable, reusable constraints/preferences/environment facts from recent tasks. Remove stale or obsolete entries. Condense wording and trim overly specific or ephemeral items (e.g., one-off PR open/close notes).
   - `.agent/memory/long_term_goals.md` — update progress on active goals, mark completed goals, add new goals discovered from recent work.
   - `.agent/memory/daily/` — review recent days for anything that should be promoted to durable memory or goals.

3) Update user profiles from observation logs:
   - For each `.agent/user_profiles/<user_id>.log.md` that contains entries, read the log and the corresponding `.agent/user_profiles/<user_id>.json` profile.
   - Distill log observations into the appropriate JSON fields: `biography` (who they are — role, expertise, identity, like a CV summary), `personality` (actual personality traits, quirks, humor style — not just "direct and concise"), `communication_preferences` (how they want info delivered), `working_patterns` (how they use the system), `projects` (list of project slugs they work on), `active_context` (current focus and priorities), `milestones` (notable achievements and events, e.g. "2025-09: NeurIPS paper accepted"), `notes` (relationship context, personal facts, inside jokes that make interactions feel personal). Update `display_name` if you learn the user's real name.
   - **Scalar identity fields** — `email`, `github`, `timezone`: these are raw canonical values, NOT prose. Store exactly one value per field (e.g., `"email": "alice@example.com"`, `"github": "alicecodes"`, `"timezone": "America/Los_Angeles"`). Replace stale values instead of merging. Verify before writing. Do not sentence-merge these fields.
   - Merge new observations with existing field content — do not discard prior knowledge. Keep string fields concise (1-3 sentences). For list fields (`projects`, `milestones`, `notes`), append new items rather than replacing existing ones.
   - Write the updated JSON back to `.agent/user_profiles/<user_id>.json`.
   - After updating, clear the processed log file (truncate to empty).
   - Skip users where the log is empty or missing. Never fabricate observations.

4) Scalar identity scan (runs independently of step 3 — do NOT skip this even if all observation logs are empty):
   - For all known users (from `.agent/memory/user_directory.json` and on-disk `.agent/user_profiles/*.json`), scan recent task threads for scalar identity data: email addresses, GitHub usernames, and timezone mentions.
   - **Attribution required**: Only update a user's profile when the value is unambiguously theirs — e.g., the user stated it themselves ("my email is ..."), or the context clearly attributes it to them. Do not write values from third-party mentions, other users' contact info, or ambiguous references.
   - If found with clear attribution, update the corresponding `.agent/user_profiles/<user_id>.json` profile directly. This is the primary ingestion path for `email` since it is PII and excluded from observation logs.
   - Apply the same scalar field rules: raw canonical values only, replace stale values, verify before writing.

5) Review recent tasks for reusable workflow patterns and file skill candidates:
   - Scan recent task reports, daily memory, and Slack threads for multi-step procedures that were performed more than once OR represent domain expertise worth preserving.
   - Check existing skills in `~/.codex/skills/` — if the pattern is already captured, skip it.
   - For each new pattern, create a file in `docs/dev/plans/` describing the skill candidate. Include: a proposed skill name (kebab-case), when it should trigger (what kind of task), the step-by-step procedure, gotchas/constraints, and which tasks demonstrated the pattern. See `docs/workflows/skill-learning-workflow.md` for the Codex skill format the developer review will target.
   - **Quality bar:** Only file patterns that are genuinely reusable across tasks and projects, and that have appeared in at least two separate tasks. Do not file: one-off procedures, project-specific patterns, single-task behavioral observations, or narrow refinements that should be folded into an existing plan. If a candidate is a subset of an existing plan, update that plan instead of creating a new file. Fewer high-quality candidates is always better than many low-quality ones.
   - Example: if the user has asked across multiple tasks to initialize projects with a specific structure (docs/, roadmap with milestones, success criteria per stage), file a skill candidate called "project-initialization" with the full workflow and source tasks.
   - Developer review (phase 1) will implement these as actual Codex skills — do not create skills directly.

6) Update all relevant project documentation:
   - `.agent/projects/*.json` summaries — update each project's `summary` field if recent work changed the project's state. Keep summaries concise (2–4 sentences).
   - Project-level docs within `projects/<slug>/` — ensure READMEs, status docs, and progress notes reflect the actual state of the codebase and recent deliverables.
   - Verify documented progress aligns with actual artifacts and code. Flag any drift or inconsistencies.

7) Check the PRs of each project. Use `scripts/review_project.sh <slug> --base main` to review PR changes locally. If the review shows no issues, you can auto-merge. If it flags issues, resolve them yourself or flag for developer review.

8) Keep edits concise and evidence-based. Prefer local evidence and direct repository inspection; do not use `consult` MCP unless local analysis is insufficient.

9) When finished, send a Slack summary to the task's `channel_id` reporting: what was reviewed, what was cleaned or updated, project progress updates (avoid jargon), any issues found needing human attention, and any items flagged for the upcoming developer review. Mention that a developer review will be automatically dispatched next to audit code and fix system issues.

10) In dispatch outcome for maintenance tasks, never set `status` to `in_progress`; use only `done` or `waiting_human`. Do not set `project` in the outcome — maintenance is a system-level task, not project-specific work.

11) As soon as you post the first maintenance Slack message, capture its numeric Slack `ts` and set outcome `thread_ts` to that numeric value; reuse that thread for all follow-ups.
