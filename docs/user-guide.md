# Murphy — User Guide

## What is Murphy?

Murphy is an autonomous research assistant that lives in Slack. You interact with it by **@mentioning** it in a channel, and it works on your request asynchronously — reading papers, writing code, running experiments, producing typeset PDFs, and reporting back in-thread.

## Getting Started

Give Murphy a task by @mentioning it in any channel it's joined:

```
@Murphy Read this paper and summarize the key contributions
@Murphy Implement the algorithm from Section 3 and run it on CIFAR-10
@Murphy Prove that this loss function is convex under these assumptions
```

Murphy will reply in the same thread with progress updates as she works, then post the final result when done.

## What Murphy Can Do

| Capability | Example |
|---|---|
| **Research & literature review** | "Survey recent work on KL-regularized RLHF" |
| **Mathematical proofs & derivations** | "Prove Theorem 2 from the attached paper" |
| **Code implementation** | "Implement SGD with heavy-tail noise injection" |
| **GPU experiments** | "Train this model on the GPU node with these hyperparameters" |
| **PDF delivery** | Substantive research content is auto-compiled to typeset LaTeX PDFs |
| **File/PDF analysis** | Attach a file to your message — Murphy can read and analyze it |
| **Consult Athena** | "Ask Athena to verify this proof" — invokes an external expert |

## How to Interact

- **Reply in-thread** to give follow-up instructions, corrections, or clarifications. Murphy picks up new replies and continues working.
- **Attach files** (PDFs, code, data) directly in your message — Murphy will download and process them.
- **Be specific** — clear instructions get better results. "Implement X" is better than "look into X."
- **Ask for what you want** — Murphy defaults to autonomy, but will ask for clarification when requirements are genuinely ambiguous.

## Special Commands

These commands are included in your @mention message:

| Command | What it does |
|---|---|
| `@Murphy !loop-3h` | Run the task repeatedly for 3 hours (supports `Xh` or `Xm`) |
| `@Murphy !stop` | Cancel loop mode on the current task thread |

**Loop mode** is useful for iterative tasks where Murphy should keep working until a goal is met or time runs out. Example:

```
@Murphy fix the failing tests and make CI green !loop-3h
```

## Task Lifecycle

Each task moves through these stages:

1. **Queued** — Murphy saw your @mention and added it to its queue.
2. **Active** — Murphy is working on it.
3. **Done** — Results posted in-thread.
4. **Waiting for human** — Murphy needs your input. Reply in the thread to unblock it.
5. **In progress** — Murphy has more work to do and will continue autonomously.

When Murphy marks a task as "waiting for human," simply reply in the same thread. Murphy will detect your reply and resume work automatically.

If a task is marked **done**, plain replies in the thread won't reactivate it. To reopen a completed task, **@mention Murphy again** in the same thread with your follow-up request.

## Tips for Best Results

- **One task per thread.** Start a new thread for each new task.
- **Murphy has long-term memory.** It remembers prior tasks and active research projects across sessions, so you can reference earlier work by name (e.g., "continue the heavy-tail experiments").
- **PDFs for research, text for quick answers.** Short factual answers come as Slack messages; substantive write-ups (proofs, literature reviews, experiment reports) arrive as typeset PDF attachments.
- **Corrections work.** If Murphy gets something wrong, reply in-thread with the correction and it will adjust.
- **You can attach files.** PDFs, code files, and data can be attached directly to your Slack message for Murphy to analyze.
- **Murphy works asynchronously.** You don't need to wait — send your request and check back later. Murphy will post results in the thread when ready.

## Athena — External Expert

If you configure a consult MCP server, Murphy can use an external expert called **Athena** for problems that benefit from a second opinion — particularly mathematical proofs, research planning, and domain-specific reasoning.

You can explicitly request Athena's involvement:

```
@Murphy Ask Athena to verify this convergence proof
@Murphy Have Athena review my draft and identify logical gaps
@Murphy Consult Athena on the theoretical implications of these results
```

When that integration is enabled, Murphy can also invoke Athena on its own when it judges that external expertise would improve the result.

## FAQ

**Q: How long does Murphy take?**
Tasks vary widely — a quick factual answer may take a minute, while a research deep-dive with experiments can take hours. Murphy works as fast as it can and posts results when ready.

**Q: Can I give Murphy multiple tasks at once?**
Yes. Send separate @mentions (ideally in separate threads). Murphy queues them and works through them in order, potentially dispatching multiple workers in parallel.

**Q: What if Murphy gets stuck or produces wrong results?**
Reply in the thread with corrections or additional context. Murphy will pick up your reply and adjust. For persistent issues, you can ask it to try a different approach.

**Q: Can Murphy access external websites or APIs?**
Murphy primarily works with local tools, files, and its configured integrations (for example Slack, an optional consult server, or a GPU node). It cannot browse arbitrary websites, but it can process files and documents you provide.

**Q: How does Murphy handle sensitive data?**
Murphy operates within its configured environment and does not share data externally beyond its integrated services. Avoid sharing credentials or secrets in Slack messages.
