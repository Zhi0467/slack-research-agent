# Data Schemas

JSON schemas for supervisor-worker communication. Referenced from `CLAUDE.md` and `ARCHITECTURE.md`.

## Worker Outcome Schema

When modifying the supervisor or prompts, the outcome JSON contract between worker and supervisor is:

```json
{
  "mention_ts": "<task ID>",
  "thread_ts": "<Slack thread ID>",
  "status": "done | in_progress | waiting_human | failed",
  "summary": "<one-line summary>",
  "completion_confidence": "high | medium | low",
  "requires_human_feedback": true | false,
  "project": "<slug or list of slugs, optional>",
  "error": "<optional>"
}
```

**Tribune review extension** (only present in Tribune review outcomes):

```json
{
  "tribune_verdict": "approved | revision_requested",
  "tribune_feedback": "<specific, actionable feedback if revision requested>"
}
```

**Tribune task state fields** (on tasks in `.agent/runtime/state.json` during Tribune review):
- `tribune_revision_count` (int) — number of revision rounds completed
- `tribune_feedback` (str) — Tribune's feedback injected into worker re-dispatch prompt
- `slack_draft_path` (str) — path to the staging file containing the worker's draft response

The supervisor's `COMPLETION_GATE` config (default: `high`) controls completion gating. The `"high"` gate trusts the worker's done status (no extra confidence check). The `"moderate"` gate requires `completion_confidence: "high"` for auto-completion — anything lower is held as `waiting_human`.

## Task JSON Schema

Each task's thread history is stored in `.agent/tasks/<bucket>/<ts>.json`:

```json
{
  "task_id": "1771908580.861819",
  "thread_ts": "1771908580.861819",
  "channel_id": "C08M46FE74M",
  "messages": [
    {
      "ts": "1771908580.861819",
      "user_id": "U06P01KHNBS",
      "user_name": "alice",
      "role": "human",
      "text": "the original @mention text"
    },
    {
      "ts": "1771908600.123456",
      "user_id": "U0AFZHQMAHX",
      "role": "agent",
      "text": "Looking into this — will start with the recent literature.",
      "source": "context_snapshot"
    }
  ]
}
```

Messages without a `source` field are original mentions (durable, never removed). Messages with `"source": "context_snapshot"` are fetched from Slack's `conversations.replies` and are replaced wholesale on each refresh.
