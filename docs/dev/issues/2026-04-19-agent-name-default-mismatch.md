# `murphy-init --agent-name` help text does not match behavior

Created: 2026-04-19 23:30 UTC

## What was reported

While reviewing the latest CLI changes in commit `642924a` ("Make agent name configurable via AGENT_NAME and --agent-name"), the new `murphy-init` help text was re-read to align future CLI plans with the current implementation.

## Reproduction context

Current parser help in `src/loop/bootstrap.py` says:

> "Defaults to the Slack app name when `--slack-app-name` is a single word."

But the implementation currently does this:

- `--agent-name` parser default is hardcoded to `"Murphy"`
- `bootstrap_repo(..., agent_name=DEFAULT_AGENT_NAME)` defaults to `"Murphy"`
- `_render_env(...)` only writes `AGENT_NAME=...` when the passed `agent_name` differs from `"Murphy"`
- there is no code path deriving `agent_name` from `--slack-app-name`

So a command such as:

```bash
murphy-init --slack-app-name Terry
```

still behaves as if `agent_name == "Murphy"` unless the caller also passes:

```bash
--agent-name Terry
```

## Assessment

This is a real CLI contract bug, not just a docs typo, because the parser help advertises fallback behavior that is not implemented. It is easy for operators to believe the worker/reviewer prompts will identify as the Slack app name when they will actually keep using the default `"Murphy"`.

The smallest fix is one of:

1. implement the described fallback in argument normalization before calling `bootstrap_repo`, or
2. remove the fallback claim from the `--agent-name` help text if that behavior is not desired.

This should be covered by a bootstrap CLI test that passes `--slack-app-name <single-word>` without `--agent-name` and asserts the expected `.env` output.
