## Maintenance task
If `task_type` is `maintenance`, the detailed checklist is in the task's `mention_text` field. Follow it step by step. Maintenance runs in two phases (phase 0 = self-reflection, phase 1 = developer review); the supervisor advances phases automatically.
- Start with the maintenance workflow directly and send the required maintenance summary message when done.
- Whether you set status to `done` or `waiting_human`, always send a Slack message to the task's `channel_id` summarizing how maintenance went: what was reviewed, what was cleaned or fixed, any drift or issues found, and any items needing human attention.
- Set `waiting_human` if you need clarification or found issues requiring human judgment; otherwise mark `done` and fix issues at your discretion.
- Do not set `project` in the dispatch outcome for maintenance tasks — maintenance is system-level, not project-specific.