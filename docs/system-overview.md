# Murphy: System Overview

Murphy is an autonomous research agent that operates continuously on a local workstation or server. It receives tasks from a human collaborator via Slack, works on them independently, and reports back. The system can run 24/7, handle multiple tasks concurrently, maintain its own memory across sessions, and perform routine self-maintenance.

## How It Works

A human sends a Slack message mentioning Murphy. A Python supervisor process polls Slack, picks up the mention, and dispatches an AI worker session to handle it. The worker reads the task, does the work (research, code, analysis), and communicates results back through Slack. When the worker finishes, the supervisor reads its outcome, updates the task state, and moves on to the next task.

Tasks can be anything the collaborator needs: literature reviews, code implementation, data analysis, paper writing, system administration, or multi-step research projects. The worker operates autonomously — it reads files, runs commands, uses configured external services, writes reports, and delivers results — but it follows a strict behavioral contract that governs how it communicates, what it can modify, and how it reports completion.

## Three Roles

The system's governance is inspired by the Roman Republic, which achieved stability through the separation of powers between competing institutions. Three distinct AI roles operate the system, each with different authority and constraints:

### Worker — *The Executor*

The Worker (named Murphy, powered by OpenAI Codex) handles all task execution. It receives a task, does the work, and delivers results to the human via Slack. It can read and write files, run shell commands, use configured external services, and send progress updates directly to Slack. Final responses may be routed through Tribune review before delivery.

The Worker is governed by a behavioral contract that defines how it should communicate (like a peer researcher, not a status-reporting bot), when to ask for help versus proceed independently, and how to classify task completion. It cannot modify the system's own source code or change its own behavioral rules.

### Developer — *The Maintainer*

The Developer (powered by Anthropic Claude) maintains the system infrastructure. It audits source code for bugs, runs the test suite, fixes issues, implements new features, and keeps the system healthy. It has full write access to the codebase — the only role that can modify system source code.

The Developer cannot change the Worker's behavioral contract (it can only suggest changes for human approval) and cannot execute user tasks. Its domain is the mechanism, not the mission.

### Tribune — *The Reviewer*

The Tribune (powered by Google Gemini) is an independent quality reviewer. It reviews the Worker's output before it reaches the human, checking for accuracy, completeness, and communication quality. It also audits the Developer's code changes during maintenance.

The Tribune holds veto power over delivery — it can block a Worker's draft from reaching the human and send the Worker back to revise. It can also run validation scripts to check file integrity. But it cannot execute tasks, modify code, or post to Slack. When it finds concerns in the Developer's code changes, those are flagged for the next review round.

This separation is deliberate: the role that does the work should not be the same role that evaluates the work. And the role that maintains the infrastructure should be checked by someone who isn't also writing the code.

## Task Lifecycle

1. **Human sends a Slack message** mentioning Murphy with a task request.

2. **Supervisor picks it up** within seconds, queues it, and dispatches a Worker.

3. **Worker executes the task** — sends progress updates to Slack along the way, writes deliverables to disk, and produces a draft of the final response.

4. **Tribune reviews the draft** — reads the task thread, examines the draft, spot-checks claims against actual files. If approved, the supervisor posts the draft to Slack. If issues are found, the Worker is sent back to revise with specific feedback (configurable number of revision rounds).

5. **Human sees the response** in Slack. If they reply with follow-up questions or feedback, the supervisor detects the reply and re-dispatches the Worker to continue.

Up to eight Workers can run concurrently, each in an isolated git worktree. The supervisor handles task selection, worker lifecycle, and state persistence.

## Daily Maintenance

Once per day, the system runs a maintenance cycle. The number of phases is configurable; the default is two phases (reflect + developer review), with optional Tribune rounds:

1. **Worker reflects** — Murphy reviews its own recent work, updates its memory and goals, checks project documentation, and posts a self-assessment.

2. **Developer audits** — Claude reviews recent code changes, runs tests, fixes bugs, implements planned improvements, and commits fixes.

3. **Tribune reviews both** (optional) — Gemini audits the Worker's output quality AND the Developer's code changes, checking for correctness, design philosophy compliance, and behavioral contract adherence.

With multiple Tribune rounds configured, the Developer and Tribune iterate: the Tribune flags issues, the Developer addresses them, the Tribune re-reviews, until quality converges.

## Design Principles

**Behavior through contract, not enforcement code.** The Worker's behavior is primarily controlled through written instructions (its behavioral contract), not runtime guardrails. When the agent does something wrong, the first question is "what's missing from the contract?" — not "what code can prevent this?"

**No role has final authority to approve its own work.** Each role self-reflects — the Worker reviews its own output in maintenance, the Developer runs tests on its own fixes. But self-assessment is advisory, not final. Every role's work is also reviewed by a different role with different incentives: the Tribune reviews the Worker's output, the Tribune audits the Developer's code, and the human ratifies contract changes proposed by either.

**Productive tension.** The Worker wants autonomy; the Tribune wants quality gates. The Tribune wants behavioral changes; the Developer wants system stability. These tensions are features, not bugs — they surface real problems. The human resolves genuine conflicts.

**Fail-open.** If the Tribune is unavailable or errors out, the Worker's draft is posted as-is. If the Developer review fails, the system continues operating. Quality gates improve output but never block the system from functioning.

**Simple and robust over clever.** One task with a phase integer beats two chained tasks. A single boolean flag beats a state machine. When a design gets complicated, step back and find the simpler structure.

## Key Infrastructure

- **Supervisor** — A single-threaded Python process that orchestrates everything: polls Slack, manages task queues, dispatches workers, reconciles outcomes, and exports a monitoring dashboard.

- **State** — Task queue state lives in a JSON file with file-level locking. Task history, outcomes, and project data are stored as separate JSON files on disk. No database.

- **Memory** — The Worker maintains durable memory (facts, constraints, user preferences) and episodic daily memory across sessions. Memory is injected into each dispatch prompt.

- **MCP Servers** — The roles access external services through Model Context Protocol servers: Slack for communication, plus any optional integrations you configure such as an external consultation service (Consult MCP). The Tribune has read-only Slack access.

- **Dashboard** — A static HTML dashboard published to GitHub Pages shows live system status, active workers, and task history.

## The Roman Analogy

The three-role design draws directly from the Roman Republic's separation of powers, as analyzed by the historian Polybius:

- The **Consul** (Worker) held executive power — the authority to command and act. Constrained by term limits and tribune oversight.
- The **Censor** (Developer) maintained standards and public infrastructure. Reviewed the conduct of officials but couldn't command armies.
- The **Tribune** (Tribune) held veto power — could block any action but couldn't initiate one. Protected the interests of the people against institutional overreach.

No single office could govern alone. The system's stability came not from harmony but from mutual dependence and mutual constraint. The same principle applies here: the system works because each role needs the other two, and each checks the other two.
