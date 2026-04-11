---
name: "visualization-task"
description: "Use when a task requests a rendered visual artifact: architecture diagram, workflow chart, state machine, data plot, or any figure that must be delivered as an image file. Covers provider selection, Mermaid-first generation, QA, and delivery."
---

# Visualization Task Skill

## When to use
- Task explicitly requests a diagram, chart, plot, or figure
- Deliverable requires rendered visual output (not a text description)
- Human provides a reference image to reproduce or improve

## Provider decision tree

1. **Technical diagram** (architecture, workflow, state machine, sequence):
   - Use **Mermaid** first — portable, reviewable, deterministic
   - Render via `mmdc` (mermaid-cli) to PNG/SVG
   - Fallback: `graphviz` (`dot`) or `matplotlib`

2. **Data visualization** (plots, charts, histograms):
   - Use **matplotlib** or **seaborn** (local, no external deps beyond Python)

3. **Complex/aesthetic diagram** (detailed illustrations, custom layouts):
   - Use Athena (`consult.ask`) with explicit visual output instructions if local rendering is insufficient
   - Fallback: local render + quality note to human

## Workflow

### 1. Classify intent
- Parse what entities, relationships, and layout constraints are needed
- Identify whether a reference image was provided
- Select the generation path from the decision tree above

### 2. Generate artifact
- Write the source file (`.mmd`, `.py`, `.dot`) under `deliverables/<thread_ts>/`
- Render to PNG (preferred) or SVG
- Use deterministic seeds for reproducibility where applicable

### 3. QA checklist
Before delivering, verify:
- [ ] All requested entities and relationships are present
- [ ] Text is readable (not clipped, not overlapping)
- [ ] Layout is clean (no unnecessary crossings, balanced spacing)
- [ ] Colors and styles are consistent
- [ ] If reference image was provided: side-by-side comparison passes

### 4. Iterate (max 2 rounds)
- If QA fails, redesign and re-render (max 2 iterations)
- After 2 failed iterations: deliver best attempt with a note explaining limitations
- Never deliver a text description when a rendered image was requested

### 5. Deliver
- Upload the rendered image to the Slack thread via `mcp__slack__attachment_upload`
- Keep the source file alongside the rendered output for future edits
- Record delivery evidence in the task report

## Mermaid rendering

Install mermaid-cli if not present:
```bash
npm install -g @mermaid-js/mermaid-cli
```

Render a `.mmd` file:
```bash
mmdc -i input.mmd -o output.png -t default -b white -w 1200
```

Common diagram types:
- `graph TD` / `graph LR` — flowcharts
- `sequenceDiagram` — sequence diagrams
- `stateDiagram-v2` — state machines
- `classDiagram` — class/entity diagrams
- `erDiagram` — entity-relationship diagrams

## Matplotlib conventions
- Use `plt.savefig(path, dpi=150, bbox_inches='tight')` for clean output
- Set figure size explicitly: `plt.figure(figsize=(10, 6))`
- Use descriptive axis labels and titles
- Prefer colorblind-friendly palettes (`tab10`, `Set2`)

## Gotchas
- Mermaid subgraph labels with special characters need quoting
- Long node labels cause layout overflow — keep labels concise or use aliases
- `mmdc` requires a Chromium installation for PNG rendering (usually bundled)
- SVG output avoids the Chromium dependency but may render differently across viewers
