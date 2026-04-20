## Research-task workflow

### Default sequence

1. **Read provided materials** — project docs, local PDFs, prior reports, `.agent/projects/<slug>.json`
2. **Draft a plan** — scope the investigation, identify subquestions
3. **Call the Consult MCP** (optional) — only when local analysis is insufficient, or when you are explicitly asked to use the Consult MCP or just `consult.ask` in general.
4. **Implement** — code, experiments, derivations
5. **Test and validate** — verify results before reporting
6. **Update persistent state** — memory, long-term goals, and project docs (`roadmap.md`, `docs/`, `AGENTS.md` — see `project-docs` skill for what goes where)
7. **Report findings** — Slack thread reply, PDF delivery for substantive content
8. **Iterate** — refine based on results or human feedback

### Materials-first rule

- Always read provided materials (papers, PDFs, project docs) before external consults or code changes.
- External consult prompts must include explicit domain context from the materials — not generic requests. In-project terminology (e.g., SDFT, SDPO) may differ from general usage; read first, then ask.
- When tooling supports it, upload project PDFs via `consult.ask(file_paths=[...])`. If upload is unavailable, use other available tools or skills to process PDFs.
- For full consult usage guidelines (when to consult, mode selection, constraints): see `docs/mcp-integrations.md`.

### Project folder creation

When a new research investigation is initiated (not a one-off factual question), create a `projects/<slug>/` folder and register it as a submodule **early in the task** — before delivering the first write-up. See the `project-docs` skill for the full setup (AGENTS.md pointer file, roadmap.md, docs/ tree).

Use descriptive slugs: `heavy-tail-optimizer-generalization`, not `ht-opt-gen`.

### PDF generation

Write LaTeX source and compile with `pdflatex` (MacTeX is installed at `/Library/TeX/texbin/`). This produces properly typeset mathematical content. Alternatively, use `consult.ask` to generate PDFs when the content originates from a consult session.