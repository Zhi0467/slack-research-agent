## Paper-writing workflow

This document governs all tasks involving writing, editing, or reviewing academic papers and manuscripts. Read it before starting any paper-related work. Project-specific conventions (macro names, color choices, Overleaf remotes) live in each project's `docs/paper-writing-notes.md`.

### Prose standard: The Elements of Style

*The Elements of Style* (Strunk & White) is the default prose baseline for all manuscript work. The most relevant rules:

1. **Use the active voice.** "We prove that X holds" not "It is shown that X holds."
2. **Put statements in positive form.** Say what something *is*, not what it *is not*. Avoid tame, noncommittal language.
3. **Omit needless words.** A sentence should contain no unnecessary words, a paragraph no unnecessary sentences. Cut "the fact that," "it should be noted that," "in order to."
4. **Use parallel construction.** Express coordinate ideas in similar form so readers recognize likeness of content.
5. **Keep related words together.** Don't separate subject from verb with a long parenthetical.
6. **Place emphatic words at the end of a sentence.** The final position carries the most weight.
7. **Make the paragraph the unit of composition.** One topic per paragraph. Begin with a topic sentence.
8. **Avoid a succession of loose sentences.** Vary sentence structure to prevent monotony.
9. **Use the serial (Oxford) comma.** "Red, white, and blue."
10. **Do not join independent clauses with a comma.** Use a semicolon, conjunction, or separate sentences.
11. **Avoid bare "this" as a pronoun.** Write "This result" or "This construction," not bare "This."
12. **Eliminate intensifiers and vacuous adverbs.** Cut "very," "extremely," "quite," "rather," "clearly," "obviously," "trivially." If something is truly obvious, you are wasting words; if not, you are insulting readers.

### Mathematical writing

Curated from Halmos, Tao, and Knuth. These apply to any paper with formal mathematics.

- **Never start a sentence with a symbol.** Write "The function f is continuous" not "f is continuous." (Halmos)
- **Use descriptive terms before symbolic names.** Write "the group G" or "the vector v" to re-anchor readers. (Halmos)
- **Write so it is impossible to misunderstand** — not merely so it is possible to understand. Every sentence should have only one valid parsing. (Halmos, citing Quintilian)
- **Define terms before using them in theorems.** If a theorem requires a specialized notion (e.g., "leap profile"), introduce the definition first — in its own subsection if needed. Do not cite definitions inside theorem statements.
- **Notation should emphasize what matters and hide what doesn't.** Use asymptotic notation to conceal irrelevant constants; be precise when exact values are crucial. (Tao)
- **Organize notation by scope.** Global notation near the front; local notation close to where it appears. Introduce notation for expressions used three or more times; avoid notation for one-off appearances. (Tao)
- **Use English prose alongside symbols.** English conveys relative importance, non-triviality, and causal relationships that symbols cannot. (Tao, Halmos)
- **Design lemma statements for use, not for ease of proof.** Hypotheses should be natural and verifiable; conclusions should be manifestly useful. Don't over-split: combine lemmas that lack individual interest. (Tao)
- **Avoid dense multi-equation displays.** When a block of aligned equations introduces setup or notation, break it into prose with one or two key displays. Lead with the role each object plays, then give the notation.
- **Avoid excessive subscripts and superscripts.** Reorganize notation to minimize stacking. (Knuth)
- **Present intuition before formalism.** Explain the strategy of a proof or the purpose of a definition before the formal statement. Toy examples or special cases before general results. (Tao, Peyton Jones)
- **Connect each proof step explicitly.** Use "therefore," "it follows that," "we now show" to guide readers. Justify non-obvious steps.

### Paper structure

- **State contributions upfront.** Write your contributions list as specific, refutable claims with supporting evidence. The contributions list drives the entire paper. (Peyton Jones)
- **Tell a story.** Structure like a whiteboard explanation: problem, why it matters, why it's unsolved, your solution, evidence it works. (Peyton Jones)
- **Don't grandmother introductions.** Don't open with universally known facts. Get to the point in the first paragraph. (Shewchuk)
- **Don't tease the reader.** Include concrete results in the abstract and introduction. No mystery-novel pacing. (Lipton, Ernst)
- **Delete generic openings.** Eliminate sentences applicable to any paper in the field. Start with specificity. (Lipton)
- **Defer related work.** Present your technique first, then compare. Leading with related work suggests your work is derivative. Be generous crediting competitors. (Peyton Jones, Ernst)
- **Conclusions must add insight.** Don't repeat the abstract with different verb tenses. Include new observations, implications, open problems. A strong conclusion should be incomprehensible to someone who hasn't read the paper. (Shewchuk)
- **Each section should explain its purpose.** Begin sections by stating key milestones, their significance, and the proof or experimental strategy. (Tao)
- **Include prose between headings.** Never have a section heading immediately followed by a subsection heading with no intervening text. (Munzner)
- **Citations must be grammatically invisible.** Write "Smith et al. [17] showed" not "[17] showed." Don't use citation numbers as nouns. (Munzner)
- **Use precise terminology consistently.** Switching words confuses readers. Different terms should signal different meanings. (Ernst)

### Manuscript editing conventions

These rules govern how Murphy edits manuscripts during collaborative writing.

- **Scope discipline.** Edit only the sections explicitly requested. Do not broaden to neighboring sections or front-matter without explicit approval. Introduction edits in particular need explicit approval.
- **Review-mode attribution.** In review mode, mark all Murphy-authored manuscript spans with the project's review color. Use the project's comment macro (e.g., `\murphy{...}`) for review notes. Let the macro supply the author label — don't add a redundant prefix. Remove visible markup only if the human explicitly asks for a clean copy.
- **Comment placement.** Place each comment inside the section it addresses. A comment about Section 2 goes in Section 2, not at the end of Section 1.
- **Comment-only reviews.** When asked to review without editing in-place: preserve existing text, add clearly attributed comments, and append new prose where completion is needed. Do not silently rewrite.
- **Consult Athena for theory-heavy sections.** Run an Athena pass on related work, comparison claims, and technical framing before finalizing. Keep resulting edits within the requested scope.

### Sync and delivery

- **Push to Overleaf after every edit pass.** The shared draft must stay current. Verify the actual Overleaf remote received the diff — a local project-repo commit does not update Overleaf by itself.
- **Compile and verify.** Use `latexmk` or repeated `pdflatex` (MacTeX at `/Library/TeX/texbin/`). Check output with `pdftotext` or rendered PNGs for regressions.
- **Deliver PDFs for substantive content.** When reporting research results, proofs, or literature reviews in Slack, compile to a typeset PDF and upload as an in-thread attachment.

### Slack updates during manuscript work

- **One message per logical revision.** Don't post multiple near-duplicate progress messages restating the same scope.
- **Report the concrete result.** State what was completed or found, not that you're still working on the same thing.
- **Fold bookkeeping into substantive updates.** If nothing materially new has happened beyond compile/sync, wait and include it in the next substantive message.
