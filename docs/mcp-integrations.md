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

If you want second opinions from an external model, point that section at a consult MCP server you control. The supervisor still preserves consult history and per-task wiring when a consult server is configured.

## Tribune

Tribune review uses Gemini CLI through `TRIBUNE_CMD`. It is optional and disabled by default in the public config.
