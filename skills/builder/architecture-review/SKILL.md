---
name: architecture-review
description: Have a frontier model (Fable or Astra, at max effort) review a very large build as its architect — whether the whole system works together, whether it is the most elegant design, what should be rebuilt, and what the user has not accounted for — and turn the result into a plan Opus can act on. Use only when asked ("run an architecture review", "Fable review of the whole build", "have Astra review the system", "holistic review of everything we shipped", "review the architecture before we build it") or when /build offers it on a mission marked for one, at the initial design or after the last backlog row ships. Do NOT use for a PR or branch diff (Code or Merge Review), a plan (Plan Review), a normal-scale design (Design Review), or one task's diff (task-reviewer).
argument-hint: [design | final] [what to review]
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, SendMessage, AskUserQuestion, Skill
---

# Architecture review — a frontier architect, an Opus executor

A frontier model reviews the build **as its architect**: it reads, judges and
plans, and never edits. You (Opus) frame the review, act as its research
assistant, fact-check what comes back, and hand the result on. Two moments only:

- **design** — a design and requirements exist, no plan yet. On a mission marked
  `Architecture review: yes`, this stands in for `/build`'s Design Review.
- **final** — the mission's work has shipped; a holistic review of all of it.

It is expensive and slow on purpose. It does not run per PR, per plan, or
mid-flight.

Paths: `${CLAUDE_PLUGIN_ROOT}` is the `toolkit-build` plugin root. The wrapper is
`claude-codex-review`. The fixed reviewer instructions are
`${CLAUDE_PLUGIN_ROOT}/tools/codex-review/architecture-brief.md`; the design
lenses are `${CLAUDE_PLUGIN_ROOT}/tools/build-lens/design-review.md`.

## The procedure

0. **Resolve `<specs_dir>`:** `python3 "${CLAUDE_PLUGIN_ROOT}/lib/toolkit_config.py" specs-dir`.
   **Exit 3: ask the user where specs go.** Never guess a folder.

1. **Moment and target.** Settle `design` or `final`, the repository (or
   repositories) and the mission spec. Use `<subject>` = the mission spec's
   `subject:`.

2. **Parse the objectives.** Invoke `skill-prompt-optimizer:prompt-optimizer` with the
   `Skill` tool — explicitly, never left to trigger — on the user's request plus
   the mission spec's `## MVP` and any recorded rulings. You want the objectives
   as testable lines and the success criteria they imply. Keep the user's own
   words verbatim beside the parse: the reviewer judges against the words.

3. **Draft the brief** from `references/brief-template.md` into
   `<prefix>-brief.md`, where `<prefix>` is
   `<specs_dir>/YYYY-MM-DD-<subject>-architecture-<moment>`. Every artifact of
   this review starts with it, so a design and a final review never overwrite
   each other. **Facts and
   questions, never conclusions:** no prior review's findings, no verdicts, no
   summary of what changed since a previous round. Then run
   `claude-codex-review --check-brief <brief>`. Exit 1 → rewrite the flagged
   lines. Warnings → carry them to step 4.

4. **The user reviews the brief.** In the turn's final text message: a link to
   the brief, the parsed objectives as a short table, and any `--check-brief`
   warnings. Ask them to approve or revise. Revise and re-run step 3's check
   until they approve. **Nothing is dispatched before this approval.**

5. **The user chooses the reviewer** with `AskUserQuestion`:
   - **Astra** (`gpt-6-astra` via `claude-codex-review`, effort `max`, read-only
     sandbox; `codex_review.models.architecture` in the toolkit config replaces
     the model). Spends the Codex weekly window, not Claude usage. Its cost at
     `max` on a whole build is **unmeasured** — say so.
   - **Fable** (`toolkit-build:architecture-reviewer`, effort `max`, read-only tools).
     Spends Claude usage; Fable has no published price. Its `subagent_tokens`
     counter is inflated and not comparable to output tokens; quote it as "one
     large dispatch".
   Choosing Fable here **is** the per-dispatch consent the standing rule requires.

6. **Usage check on the chosen model.**
   - Astra: `claude-codex-review --usage`. **Exit 3 (≥90% of the week used) →
     stop** and ask: wait for the reset, or switch to Fable. `unknown` → say it is unknown, never "plenty".
   - Fable: ask for **both** readings from the Account & Usage panel — the
     5-hour and the weekly. Fable can exhaust either, and those readings
     **are** the usage check; quote the dispatch against the tighter one, and
     if the weekly is the constraint, say Astra spends a different budget.
     `claude-usage-anchor <pct>` narrows the session estimate with the
     5-hour figure only — never pass it the weekly one; if it says the
     estimator is off (the default), the panel
     readings stand on their own. Read every figure as a floor.

7. **Round 1.**
   - Astra: `claude-codex-review --architecture-review <brief> --moment <m>
     --repo <repo> -o <prefix>-review.json`.
     Keep the `codex session:` line.
   - Fable: dispatch the `Agent` tool with `subagent_type:
     toolkit-build:architecture-reviewer`, giving it the absolute paths of the fixed
     instructions, the design lenses, the brief and the schema
     (`${CLAUDE_PLUGIN_ROOT}/tools/build-lens/architecture-review-report.schema.json`),
     plus `moment` and `round: 1`. Save its final message verbatim to the `.json`
     path, then `claude-codex-review --validate-report <json> --round 1 --moment <m>`.
   A non-zero exit is a failed review, never a clean one: report it.

8. **Research rounds — at most two.** If `status` is `needs_context`, answer
   every request. Fetch the evidence yourself, or with Sonnet subagents for bulk
   reads (pass `model: sonnet`). Write `<prefix>-answers-round-<N>.md`:
   per request, its `id`, the **source** (path, command, connector), and the
   **verbatim** excerpt or output. **Evidence only — no opinion, no conclusion,
   no "I think"**: the reviewer's independence is what the frontier price buys.
   Then continue the same reviewer:
   - Astra: `claude-codex-review --resume <session> --answers <file> --round <N>
     --moment <m> --repo <repo> -o <json>`.
   - Fable: `SendMessage` to the same agent with the answers and `round: N`; save
     its reply to the `.json` path; `--validate-report <json> --round <N> --moment <m>`.
   Round 3 is final by contract; the wrapper refuses a round-3 `needs_context`.

9. **Fact-check all three layers.** For every load-bearing claim in
   `principles`, `changes` and `lens_verdicts`, check it against the repository
   and record **confirmed**, **refuted** or **unchecked**. A refuted claim
   removes or rewrites what rests on it. **An unchecked claim
   cannot back a binding principle or change:** move that item to the unverified list,
   non-binding, unless the user explicitly accepts it. A reviewer with no refuted
   claims on record is indistinguishable from one nobody checked.

10. **Hand off.** Write `<prefix>-review.md`:
    front matter (`status: live`, `subject`, `reviewer`, `moment`, `brief`, and
    a cost line — the wrapper's estimate for Astra, `subagent_tokens` with its
    caveat for Fable), then, in this order: the **fact-check table**, the
    **principles**, the **change plan**, the **implementation sketch**, the
    **unverified** list.
    - **Design moment:** fixes go into the design text; findings and their
      dispositions go into the build's ledger. This replaces Design Review for
      that design, so `/build` continues at its step 8 (the user reviews the
      written spec).
    - **Final moment:** propose each surviving change to the user as a
      backlog row for the mission spec. Adding rows is routine; re-ranking is
      the user's call. Each change later runs through `/build`, with the principles quoted
      in its design as the anchor.

## How Opus uses the result

The three layers are ranked, and the ranking is the instruction:

| Layer | Binding? | When the build meets reality |
|---|---|---|
| Principles | Yes | Breaking one needs the user's approval |
| Change plan | On intent and `done_when` | Deliver the outcome; the route is yours |
| Implementation sketch | No | Deviate freely while the principles hold; record each deviation in one ledger line |

Anchor to the principles, not the sketch. A sketch detail that proves wrong is
the expected case, not a reason to stop.

## Red flags

| Thought | Reality |
|---|---|
| "The brief is obvious, I'll dispatch and show them after" | Step 4 is the gate. A misframed brief buys an expensive review of the wrong question. |
| "I'll mention what the last review found so it doesn't repeat it" | That anchors the reviewer. Facts and questions only. |
| "The reviewer asked for X; I'll explain what I think it means" | Answers are sourced evidence. Your opinion steers the review. |
| "These findings look right, skip the fact-check" | Step 9 is what makes the plan safe to act on. Record every disposition. |
| "It's a big build, run it at every milestone" | Two moments: design and final. |
