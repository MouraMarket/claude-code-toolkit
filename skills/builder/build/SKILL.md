---
name: build
description: Run a multi-file build through the Opus-architect / Sonnet-executor split, with adversarial review gates. Use when about to implement a multi-file change, a new tool, hook or skill, or anything with real verification — invoke before writing code. Not for single-context work — analysis, a short document, a recap, one file.
argument-hint: [what you want built]
allowed-tools: Read, Write, Edit, Bash, Grep, Glob, Agent, Workflow, TodoWrite, AskUserQuestion
---

# /build — architect in Opus, execute in Sonnet, attack the plan before it runs

You are the **architect** for this build. You do design, sequencing, judgment,
review, and the user's decision gates. Transcription goes to
pinned Sonnet executors, so your context stays free for work only you can do.

Build target: **$ARGUMENTS**

## Standing obligations — this file is read once, so these are up front

Not re-read on later turns; these eight recur all build.

1. **Before every gate, check the resolver still prints the ledger's `mode:`,
   and say the quote and ledger it** — the table under Plan Review.
2. **Append the ledger after every task** (`Task N:`, `Ruling:`, `Miss:` lines),
   and never pass it to any gate.
3. **Pass an explicit `model` on every subagent dispatch.**
4. **Read `executed` before `findings`, and check every finding against the
   repo** — a refuted one is recorded as refuted, never dropped.
5. **Write a `Miss:` line the moment a gate is shown to have missed something**
   — during the build or after it ships.
6. **Every message asking the user to decide follows the message rule in
   `brainstorming.md`'s intro** — the Phase 1 classification, a mid-build
   stop, Phase 6.
7. **Write the PR body's `Plan:`, `Design:` and `Dosage:` lines from what
   actually ran**, in this build's mode — and correct `Dosage:` if it ratcheted.
8. **After the merge, graduate any human work to the mission spec, then flip the
   plan's, design's and ledger's `status:`, then `git tidy --prune-worktrees`.**

## Phase 0 — check your own model, and record the review mode

If you are not running on Opus, say so in one line and ask whether to switch
(`/model opus[1m]`) or proceed.

Then run `claude-build-route --review-mode --repo <repo>` and write what it prints —
`economical` or `full-power` — into the ledger header (Phase 2); if it
refuses, show the user why and ask. If a gate's
re-run disagrees with the ledger, config changed mid-build: stop and ask.
Beside it, note the expected runtime: `workflow` if the `Workflow` tool is in
your tools, else `agent`.

Then resolve where specs go: `python3 "${CLAUDE_PLUGIN_ROOT}/lib/toolkit_config.py" specs-dir`
prints `<specs_dir>`. **Exit 3: ask the user where specs go**; never use a
folder under the current directory.

## Phase 1 — design (you, Opus)

**Skip this phase only if you can name the design file and its front matter
carries an `Approved:` line.** A handover, a backlog row or a mission spec is
not an approved design.

Otherwise, **classify the work out loud before you design it**, so the user can
override the classification rather than discover it, then follow that path's
steps in `${CLAUDE_PLUGIN_ROOT}/tools/build-lens/brainstorming.md` (absent: STOP — the `toolkit-build` plugin is not
loaded; run `/reload-plugins` or start a new session):

- **Spike** — a feasibility question whose output is an answer, not code you
  keep. No spec, no plan.
- **Bounded** — a well-scoped change to a flow that **already exists in this
  repo to read**. A short design in chat, then **stop** until the user says yes.
- **Architectural** — new subsystems, or changes that restructure how components
  fit together. Questions, approaches, a sectioned design, a written spec,
  Design Review, then the user's yes — before anything is built.

**When in doubt take the heavier path. The ratchet is one-way:** complexity
discovered mid-task upgrades the classification, and nothing downgrades it.

**Red Flags — the rationalizations that defeat this classification.**

| Rationalization | Reality |
|---|---|
| "Too simple to need a design" | Size is not evidence — under-specified plans are what this phase prevents. |
| "I'll call it bounded and skip the spec" | No existing flow to change means it isn't bounded. |
| "There's a handover or backlog row, so it's approved" | Only an `Approved:` line on a design file counts. |

## Dosage — declare the failure shape once; it decides Merge Review

**Before the first gate, decide one thing and write it down: would a defect in
this build announce itself?**

- **Loud** — it crashes, throws, exits non-zero, renders blank, or a human sees an
  obviously wrong answer the first time they use it. **Skip Merge Review.** Ship
  it and fix in flight.
- **Silent** — a confident wrong answer, or no answer where one was expected, with
  nothing downstream to complain: numbers a human trusts unchecked; guards and
  hooks; scheduled work nobody watches; tests and fixtures that are themselves
  the evidence; anything installed onto someone else's machine.
  **Run Merge Review.**

| | Design Review | Plan Review | Code Review | Merge Review |
|---|---|---|---|---|
| **Loud** | architectural only | run | run | skip |
| **Silent** | architectural only | run | run | run |

**Every gate that runs applies its full checklist, whatever the shape.**

**Record the call in the PR body, in one line, either way**.

**The declaration ratchets one way: loud may become silent mid-build, never the
reverse.** A loud build that turns out to touch a guard, a hook, a number read
unchecked, or anything installed onto someone else's machine runs Merge Review. Log it as a
`Ruling:` and **correct the `Dosage:` line in the PR body when you write it**.

## Gate: Design Review — the approach, before a plan exists (architectural builds)

Runs at step 7 of
`${CLAUDE_PLUGIN_ROOT}/tools/build-lens/brainstorming.md` (or `architecture-review`): dispatch the reviewer
`${CLAUDE_PLUGIN_ROOT}/tools/build-lens/design-review.md` describes.

**Read the gate's reference before dispatching. If it is not there, STOP — the
`toolkit-build` plugin is not loaded; run `/reload-plugins` or start a new session.**
The four gate references ship in the plugin. **Do not
proceed by inventing lenses** — a gate run with made-up lenses returns a
clean-looking review. This applies at every gate below.

## Phase 2 — write the plan (you, Opus)

The plan is the contract: one reviewable unit per task, no placeholders, and
your own re-read before any gate runs.

**Non-negotiable, and the load-bearing constraint of this command:** every task
in the plan must carry

1. explicit **acceptance criteria**, and
2. an exact **verification command** to run — one capable of failing on the
   unmodified repo.

Executors report `BLOCKED` when either is missing; otherwise a Sonnet executor
confidently builds something plausible and wrong. Ask of each task: *could this
be read two ways?*

**Mark every premise the plan rests on as verified or inferred, in the plan
text.** A payload shape, a signature, a file's contents, a config key: an
inferred premise is not a defect; one that reads like a measured one is.

**Split a plan before you gate it, not after.** Gate cost tracks brief size, and
a long plan draws a long list of findings, each of which you then adjudicate.
**Over roughly 800 lines or 8 tasks, split the build and gate each
part.**

**Write the plan to a stable path and leave it there:**
`<specs_dir>/YYYY-MM-DD-<slug>-plan.md` — Phase 6 records that exact path.
Create the ledger beside it now: `<specs_dir>/YYYY-MM-DD-<slug>-ledger.md`,
its header carrying `mode:` (Phase 0) and `dosage:`.

**Open it with front matter**, before the first heading — a design spec and the
ledger too:

```markdown
---
status: live
subject: <toolkit | my-app | ...>
---
```

`subject` is a slug naming the *work*, never a sentence: it becomes an archive folder. `claude-spec-archive` needs both keys.

**The plan names its classification on its own line** — `Classification:
bounded` or `Classification: architectural`, plus `Design: <file>` and
`Requirements: <file>` when architectural. Plan Review refuses a plan without
them, and embeds the requirements file verbatim, so it holds the acceptance
contract only: no deliberation, no review findings.

**Work only a human can do does not go in the plan.** A console step, a
credential to rotate, a decision only the user can make goes in the mission
spec's `## Waiting on a human` (shape: `/handover` §2) — a plan merges and goes
quiet.

## Gate: Plan Review — attack the plan before it runs (the load-bearing gate)

**Run this on every build that reaches Phase 2. Do not skip it to save time.**
Until this gate, the only reader of the plan is the person who wrote it.

Read `${CLAUDE_PLUGIN_ROOT}/tools/build-lens/plan-review.md` and dispatch the one reviewer it
describes. Resolve every finding **in the plan text** before a single executor
runs.

### Which reviewer runs

The mode in the ledger header; `${CLAUDE_PLUGIN_ROOT}/tools/build-lens/plan-review.md` carries both dispatches. Under
`full-power` it is `claude-codex-review --plan-review <plan file>`, which
builds the brief and sends the fixed prompt itself: hand it the plan path and
nothing else — never a prior round's findings.

**`/effort max` on your own session is a standing prohibition, not a
footnote.** It levels you with the reviewer, collapsing the effort asymmetry
`${CLAUDE_PLUGIN_ROOT}/tools/build-lens/plan-review.md` names as one of three independence mechanisms — and a levelled
reviewer still returns a clean-looking review.

### What this costs, and how to quote it

**Say the quote out loud before you dispatch a gate.** One line — *"Plan
Review, economical, one reviewer, unknown: not yet measured"*, or *"Plan
Review, full power, Codex quota only."* Quote from this table, and add it to the ledger as a
`Quote:` line.

| What you are about to run | Output tokens | % of a 5h window |
|---|---|---|
| `economical` — one blinded reviewer | **your own measurement only**: run `claude-build-cost` after one gate; until you have, say "unknown" | **your seat's ratio only**, else "unknown on this seat" |
| `full-power` — Codex | 0 Claude | Codex quota; the wrapper prints its meter after each run |

**How much one reviewer misses compared with several is UNMEASURED.** Do not
describe the single reviewer as measured-equivalent.

**Cost here means tokens, and tokens mean usage limits.** Quote tokens; reserve
dollars for API-key work. A percentage needs **your own seat's ratio**,
stated in your own `CLAUDE.md`; never borrow another seat's. A scan-derived
ratio **counts this machine only**: a **floor**. Quote the range, never a
point.

**Do not quote the `subagent_tokens` field a workflow returns.** Use
`claude-build-cost` (Phase 5); the methods are in
`${CLAUDE_PLUGIN_ROOT}/tools/build-lens/build-measurements.md`.

### Acting on the Plan Review

Resolve every finding **in the plan text**. A finding you disagree with is still
a signal the plan reads ambiguously — rewrite for the reader. More than a handful
of `BLOCKING` findings means the plan is not close: rewrite it and run Plan
Review **once** more.

**One re-round, then execute. There is no third Plan Review pass.** A fix pass
grows the plan, and cost tracks brief size.

## Phase 3 — execute (pinned Sonnet subagents)

Give the executor the plan path, task identifier and mode, never "the next task."
**Dispatch each task twice: implement, then prove** (verification, mutations)
in a fresh executor. Cost is turns × context, and proof on a full context is
the expensive half.

| Role | Agent | Model · effort |
|---|---|---|
| Implement, or prove, one task | `toolkit-build:executor` | `sonnet` · medium |
| Review one task's diff | `toolkit-build:task-reviewer` | `sonnet` · high |

Both ship in `toolkit-build`; dispatch by name.

**`toolkit-build:task-reviewer` is off by default** — Code Review reads the whole branch.
Dispatch it only when a later task builds on this one's contract, and say so.
**Re-run one mutation yourself, the most load-bearing**, not all.

**Never omit the model on any other dispatch.** An omitted model inherits Opus.

**If the session is running `ultracode`, its standing "author a workflow for every
substantive task" default stops here.** Workflow agents inherit the session
model.

### The Build Ledger — append a line as each task lands

**Append to a sidecar beside the plan — `<specs_dir>/YYYY-MM-DD-<slug>-ledger.md`,
same `status:` and `subject:` front matter — after every task.**

```
Task N: complete (<base>..<head>, review clean | K parked | review skipped)
Task N: minor (deferred): ...
Ruling: <what you decided>. <why>. <cost if wrong>.
Quote: <gate> | <mode> | <the quote you said> | <runtime>.
Snapshot: <gate> | <its "reviewed snapshot:" line, verbatim> | <runtime>.
Miss: <gate> | <declared shape> | <the defect it should have caught>.
```

`<runtime>` is the runtime that actually ran the gate (`workflow`, `agent` or
`codex`). A Workflow call that fails as unavailable falls back to `agent`,
with a `Ruling:` line. Under `agent`, `Snapshot:` is `check`'s `accepted:` line.

**It is a separate file for one reason, and it is the reason that decides the
design: the plan is a gate brief.** A `Ruling:` line is rationale, so **the
ledger is never passed to any gate, and never pasted into a `brief`.**

**The failure this prevents is re-dispatch after a compaction.** **After any
compaction, trust the ledger and `git log` over your own account of
the build**, and reconcile them before dispatching.

**The `Ruling:` lines are the half that reaches the user.** A ruling is anything
you settled that the plan did not: a parked finding, a mid-build plan correction,
an interface where the plan was silent — **every one is read back in Phase 6**.

**A `Miss:` line is the only instrument that measures gate recall.** Write it
the moment a defect surfaces that a gate should have caught — mid-build or after
merge, into that build's ledger. Read recall back with `rg '^Miss:' <specs_dir>`.

## Gate: Progress Check — is it still building the planned thing? (midpoint, builds over ~six tasks)

At roughly half the tasks, dispatch **one** `toolkit-build:code-reviewer` subagent (`model:
opus`) with the plan, the diff so far, and every executor `OBSERVATIONS` block:
*does the build still match the plan, and do the observations add up to a plan
defect nobody escalated?*

**Skip Progress Check when every task is already complete.** Code Review is handed
the plan and checks spec drift in both modes.

## Phase 4 — handling BLOCKED

A `BLOCKED` report is the system working. Resolve it yourself — never hand the
ambiguity back down. Two on one task means the plan is wrong: fix the plan text,
then resume.

## Gate: Code Review — adversarial review of the finished branch

Read `${CLAUDE_PLUGIN_ROOT}/tools/build-lens/code-review.md` and dispatch the one reviewer it
describes, with no rationale from you. In both modes it is handed the plan
as its contract.

### Which reviewer runs

The mode in the ledger header; `${CLAUDE_PLUGIN_ROOT}/tools/build-lens/code-review.md` carries both dispatches —
`blinded-review.js` with `gate: "code"`, or `claude-codex-review --plan <plan file>` without
`--merge-with`. Use a report path distinct from
Merge Review's: two gates writing one path means you adjudicate one report
believing you read two.

**Do not size Code Review against a reviewer you have not confirmed is
running.** Nothing else reviews the branch behind this gate. **The
merge surface belongs to Merge Review, below.**

## Gate: Merge Review — the branch as a merge, not as an artifact

**What Code Review cannot see.** Code Review reviews the branch as an artifact;
this reviews it as a **merge**, when Dosage says **Silent**. Read `${CLAUDE_PLUGIN_ROOT}/tools/build-lens/merge-review.md`
before dispatching.

**G5 — run the suite on the merged tree.** Before dispatching, build a merged
tree YOURSELF, run the repo's own suite on it, and report the output — under
`full-power` the wrapper's scratch worktree exists only inside the dispatch.

```bash
git -C <repo> fetch origin <base>
git -C <repo> worktree add <repo>/.claude/worktrees/build-merged-check HEAD
git -C <repo>/.claude/worktrees/build-merged-check merge --no-edit origin/<base>
# run the repo's suite HERE, and report its output
git -C <repo> worktree remove --force <repo>/.claude/worktrees/build-merged-check
```

A conflict is the finding — resolve it, never review a conflicted tree. The
reviewer's `executed` field cannot stand in for your own run: a reviewer's
sandbox can fail a suite spuriously.

### Which reviewer runs

The mode in the ledger header. Under `economical`, `blinded-review.js` with
`gate: "merge"`, as `${CLAUDE_PLUGIN_ROOT}/tools/build-lens/merge-review.md` describes. Under `full-power`, the same
`claude-codex-review --repo <branch worktree> --base <default branch> -o <report path>`
as Code Review, minus `--plan`, plus `--merge-with <default branch>` — a branch name, not
`origin/…`. **`--merge-with` is what makes the two gates different, not the
diff:** it sends the branch diff and what moved under you since as two labelled
blocks. `-o` is required, and never Code Review's path.

### Fix rounds — ONE ROUND, THEN SHIP

**There is no second Merge Review pass, under any condition.** After Merge
Review returns findings: resolve them, **mutation-test each fix against the
specific property it claims to establish**, and ship. Whatever a second round
would have caught becomes an in-flight bug — deliberately. The cheap second
opinion on a fix diff is `/code-review` at low/medium, as `${CLAUDE_PLUGIN_ROOT}/tools/build-lens/merge-review.md`
describes; it does not reopen the loop.

A pass reads the repository and runs its suite **whatever the diff's size**, so
the saving is in *whether* it runs and *how many times*, never a cheaper pass.

## Phase 5 — cost

```
claude-build-cost <this session's transcript.jsonl> --label "<what you built>" --no-record
```

It splits spend into lanes by `agentType`; a `SessionEnd` hook already records
every qualifying session. **Read its dollar figures as a proxy, not a bill.** Report tokens.

**Two things to record here that nothing else captures.**

- **The architect lane, named.** It is often the largest opus-equivalent line in
  the ledger. Say what it was: plan-writing and judgement, or re-reading
  material a subagent should have read for you.
- **Any defect that escaped a gate.** It is a `Miss:` line in the ledger (Phase 3).

## Phase 6 — gate

One report, in the turn's final text message, shaped like this:

| Part | Shape |
|---|---|
| Built | Two sentences, plus the verification's real output |
| Gates | Table: gate · `Quote:` · actual (Phase 5, where measured) · findings · what you did |
| Rulings | Table: every `Ruling:` line from the ledger · its cost if wrong. "Done" with none means they were swallowed |
| Left out | Cuts, executor `Observations`, tasks that skipped `toolkit-build:task-reviewer` |
| Changed how it works? | If yes, update the repo's `CLAUDE.md` — a trap or corrected belief, not a changelog |

End it on the ship call — pushing, publishing, merging — asked in that message.
`Architecture review: yes` and this PR empties Backlog → offer final `architecture-review`.
Moving between tasks is execution. Shipping is a decision.

### Provenance — the PR body names the plan

Once they approve shipping, **the PR body carries the plan's path on its own
line**, the design spec's too if the build had one, and the dosage call:

```
Plan: <specs_dir>/YYYY-MM-DD-<slug>-plan.md
Design: <specs_dir>/YYYY-MM-DD-<slug>-design.md
Dosage: silent — Merge Review ran (full-power, Codex)
```

Prefix, space, value, nothing else on the line, so they stay greppable. Describe
what ran in this build's mode — never vocabulary from the other mode.

### Once the PR merges, flip the plan's status

**First: graduate any human work out of the plan before you retire it.** A
console step, a deferred decision, a second PR goes to the mission spec's
`## Waiting on a human`, because **a finished plan is a file nobody opens
again.** So **leaving the plan `live` is not an alternative to moving the rows.**

Then `status: live` → `status: shipped` in the plan and the ledger. **This
covers the design spec too**. **If the build did not ship**, it is `superseded`. Leave it `live` only if
the work genuinely continues; a `live` plan is never archived.

## Throughout — how the architect conducts the build

- **You own every gate.** Subagents report to you; you talk to the user, and
  never forward findings as though they arrived pre-approved.
- **Between gates, keep moving.** The gates are where you stop.
- **Never accept an unverified claim.** A subagent that reports tests pass shows
  the output; relaying an unseen green launders an unknown into a fact.
- **Report failures as failures.** Never soften a blocked task, a skipped step,
  or a suite that errored into language that implies it succeeded.
- **Plan text is yours to fix; code is not.** Either way a finding is a claim —
  verify it against the repo before it earns a fix, and record a wrong one as
  refuted. Dispatch a *code* fix. **This is not a licence for a second gate
  pass** — a one-line fix you mutation-test in the same breath is the exception,
  ledgered as a `Ruling:`.

## When NOT to use this

If the user invoked `/build` for single-context work — analysis,
a short document, a recap, one file — say so in one line and do it. A flag
plus its test is two files: run the workflow. It earns its keep on multi-file
builds with real verification.

The gates scale on classification, build size and the failure shape declared under **Dosage**:

| | Design Review | Plan Review | Progress Check | Code Review | Merge Review |
|---|---|---|---|---|---|
| **Loud**, under ~6 tasks | architectural | run | skip | run | skip |
| **Loud**, ~6+ tasks | architectural | run | run | run | skip |
| **Silent**, under ~6 tasks | architectural | run | skip | run | run |
| **Silent**, ~6+ tasks | architectural | run | run | run | run |

**One rule that survives all of this: never let a gate's cost become a reason
to skip the gate.**
