# MCP Integrations

MCP servers and external tools available to the worker agent.

## Slack MCP

`mcp/slack-mcp-server/` is the Slack API wrapper used by workers for all Slack I/O.

- Codex worker config: `.codex/config.toml`
- Claude developer-review config: `src/config/claude_mcp.json`

After changing Slack MCP source, rebuild the binary:

```bash
cd mcp/slack-mcp-server
go build -o build/slack-mcp-server ./cmd/slack-mcp-server/
```

## Consult MCP

The public package does not bundle a consult server. By default, `murphy-init` leaves `[mcp_servers.consult]` disabled in `.codex/config.toml`.

If you want Athena-style second opinions, point that section at a consult MCP server you control. The supervisor still preserves consult history and per-task wiring when a consult server is configured.

When you do use consult, treat Athena as having **zero access** to your computer, terminal, local files, repo checkout, PR state, or prior task context unless you explicitly provide that context in the current Athena chat.

Tools: `consult.ask(prompt, mode?, file_paths?)`, `consult.new_chat()`
- `file_paths`: absolute local file paths that the consult server will upload as attachments into the Athena chat

Important constraints:
- If a consult depends on a local artifact, attach every relied-on file via `file_paths`. Referring to `/path/to/file` in the prompt is not enough; Athena cannot open files on your computer.
- If a consult depends on repo / PR / issue / review-comment context, include the GitHub URL and enough pasted or summarized context for Athena to reason without local checkout access. If a local diff or artifact matters, attach it.
- Provide enough self-contained context in the prompt for Athena to reason correctly. Vague prompts get vague answers.
- Consult is optional second-opinion support, not an execution environment. Verify its output before using it.

Example:

```python
consult.ask(
  prompt="Audit the final delta on https://github.com/org/repo/pull/123.\nContext: <brief summary of what changed and what risks you want checked>.\nFocus on concrete correctness bugs or regressions, not style.",
  file_paths=[
    "/absolute/path/to/final.diff",
    "/absolute/path/to/any-local-artifact-mentioned-in-the-prompt",
  ],
)
```

## Tribune

Tribune review uses Gemini CLI through `TRIBUNE_CMD`. It is optional and disabled by default in the public config.
