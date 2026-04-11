You are resuming your prior session on this task. All your previous context
(files read, reasoning, decisions) is preserved.

## Thread

Full conversation history: `{{THREAD_FILE_PATH}}`
Read this file to understand the current state of the conversation — it may
have evolved significantly since your original dispatch.

New messages since your last session:
{{NEW_THREAD_MESSAGES}}

## Task State
{{TASK_STATE_UPDATES}}

{{WAKEUP_CONTEXT}}

{{LOOP_CONTEXT}}

{{SLOT_OVERRIDES}}

Continue from where you left off. If significant time has passed, re-read
files before modifying them — other workers may have merged changes.

Write the outcome file to `{{DISPATCH_OUTCOME_PATH}}` per the schema in `docs/schemas.md`.
