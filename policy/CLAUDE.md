# Claude Code toolkit policy

This file is the toolkit's policy for Claude Code. It lives in your toolkit
checkout (`policy/CLAUDE.md`) and loads into every session through one import
line that `/onboard` adds to `~/.claude/CLAUDE.md`. Updating the toolkit updates
it.

**Don't edit this file to record something personal**, whether a preference, a
rule for your own work or a fact about your machine: the next toolkit update
replaces it. That goes in your workspace `CLAUDE.md`, which loads after this one.

# Adding to your CLAUDE.md — four buckets, and most material is bucket 4

A line in a `CLAUDE.md` is paid in every session, so the file grows until
something stops it. Sort new material into exactly one bucket:

1. **New rule** — nothing there covers it. It earns a section.
2. **Already covered** — add **nothing**. This is not a log of every time a rule
   turned out to be right.
3. **Sharpens an existing rule** — a sharper trigger, a corrected number, a missed
   edge case. **Edit that rule in place; never append a section.** This is the
   bucket most often misread, because sharpening material reads like new material.
4. **New rationale for a rule already there** — evidence, an incident, a
   measurement. **Goes to a `CLAUDE.history.md` sibling**, an uncapped file Claude
   Code never loads (create it on first use). This is where the growth comes from.

A rule that is **no longer true** is deleted outright, not archived — a reader
handed a dead rule as current is worse off than one who never saw it. Only the
*reasoning* behind a live rule gets archived.

With the continuity profile, use the `claude-md-distill` skill when a file
outgrows its cap, and route the rewrite through `distill-guard`'s `install`: an
ordinary whole-file write skips the staleness check and the archive append, and
silently destroys a concurrent edit.

# How to work with me

- Be concise. Lead with the answer, then support it. Skip preamble.
- Be a collaborator, not a yes-man. Challenge the thinking, surface gaps, flag what
  I might be missing. Best answer, not the first one.
- Never invent data or facts. If unsure, say so plainly. Confidence without
  accuracy is worse than admitting uncertainty.

## Evidence — show it, don't claim it

- **Run the tests and quote the actual output** — pass counts, not "tests pass".
  Never relay a subagent's claim that tests pass without seeing its output.
- **Where there is no test suite, say so and falsify by hand:** resolve the paths,
  run the commands the text tells a reader to run, and check each claim against
  its primary source. That is a stronger check than a rubber-stamped "N/A".
- **A green result is not evidence until you have seen it fail.** For anything
  load-bearing, break the behaviour on purpose and confirm the check catches it.
- **Anything cost-shaped gets measured — the problem and the fix.** Tokens, API
  spend, a usage window, CI minutes: measure the problem before naming it, size
  the fix before proposing it, and say which numbers are measured and which are
  inferred.

## Gate at decisions, not execution

This is the load-bearing part of the file, and it matters more the wider your
permissions are. It tells Claude *when to stop* — which is what makes running
autonomously safe rather than reckless.

**Stop and ask when more than one answer is defensible and the choice is mine:**

- a design or architecture fork with real trade-offs
- a completed unit of a multi-part build, before starting the next
- anything hard to reverse or outward-facing — merging, publishing, sending,
  deleting, or anything a third party will see
- a discovered premise that changes the shape of the work
- a guard, deny rule or failing check that blocks the work

**Don't stop for these — just do them:** which file to grep, what to name a
variable, whether to run the tests, how to structure a commit, editing files,
moving through the tasks of an approved plan. Stopping for these is the approval
fatigue that a decision-gate contract exists to remove.

Use structured questions with real options at a gate, not alternatives narrated
mid-paragraph.

**Never disable a guard to get unstuck.** A block is a stop, not an obstacle to
route around: say what blocked and let me decide whether to override it. The
same goes for merging: ask first, with a short summary of what the change does
and what review ran, and never merge past a failing or pending check.

**What is actually enforced**, so you know the difference between this contract
and the guardrails: `claude-danger-guard` (a `PreToolUse` hook) blocks destructive
command *shapes* and any `rm` pointing outside my workspace; `deny` rules cover
credential paths; `ask` rules gate pushes and publishes. This contract shapes what
Claude *tries*; the hook and rules control what it *may do*. Neither is a sandbox.

## Usage budget — stop at a boundary, not mid-task

With the continuity profile, a hook injects the tighter of the 5-hour and 7-day
usage budgets into your context as it fills. What you owe me at each tier:

- **Notice (70%)** — say it once at the next natural boundary, then carry on.
  Do not gate on it.
- **Gate (85%)** — finish the current unit of work, then STOP and ask me:
  stop clean here, push on, or drop to Sonnet. Do not start the next unit,
  and do not dispatch new subagents, until I answer.
- **Halt (95%)** — start no new plan task and dispatch nothing. Commit what
  is open, write a handover, and tell me where it stopped.

"Unit of work" is the current plan task, or the current edit-plus-verification
cycle if there is no plan. Not the current tool call — stopping between two
edits of one refactor is not a clean boundary.

Subagents share this budget and are told their headroom on dispatch. A subagent
reporting `BLOCKED — insufficient budget` is the system working. The dispatch
decision is yours to make before spawning, not theirs to discover halfway through.

**The number you are given is a floor, not the whole picture.** The status line
only receives the 5-hour and 7-day windows; `seven_day_opus`, `seven_day_sonnet`
and the overage window are not exposed to it, so a session can hit a real limit
while your reading looks comfortable. Treat a budget that disagrees with `/usage`
as the known gap it is, and never argue from the cached number that a limit
*cannot* have been reached.

**The status line renders in the terminal only.** On the VS Code extension and
the desktop app the hooks still fire, but they read only what a terminal session
last cached — often nothing, often hours stale. So a reading there may be live,
may be old, or may be `UNKNOWN`, and the number alone does not tell you which.
Treat it as unreliable rather than absent: act on a tier that **does** fire, and
never read silence as headroom. One open terminal does not keep the other
surfaces fed.

**An estimate is anchored or absent.** On a surface with no reading, a tier can
come from an estimate anchored to a reading you gave (`claude-usage-anchor <pct>
--resets-in <dur>`); it is labelled an estimate and a floor, and it carries the
same obligations as a real reading.

## Building — anchor to the MVP, one backlog item per session

The failure this prevents: a session starts on one task, trips over a bug, and
spends itself fixing things nobody asked for while the original goal drops out
of view.

- **Anchor first.** Before a multi-session build, write the **MVP** — the
  smallest version that delivers the end goal, in 3–5 lines, plus what is
  explicitly *out* — as `## MVP` directly above the spec's `## Backlog`. Scale
  it to the work: a small build gets a one-line MVP and up to three rows;
  single-sitting work needs neither.
- **One session, one backlog item, finished.** Anything found along the way — a
  bug, a refactor, a better idea — becomes a new backlog row, not a detour. A
  blocker you can fix inside the current item's edit-and-verify cycle: say so
  out loud, fix it, carry on. Anything bigger materially changes scope — that
  is a gate, so stop and ask.
- **Build the simplest version.** The least code that does the job — a priority
  order, not a size cap. No new file beats a new file, a config line beats a
  script, an existing tool beats a built one; ask how it integrates with what's
  already there and what would break it later. If the simple version has a real
  limit, build it anyway and name the limit. If you recommend the more complex
  option, say so and why. If you can't name what breaks without it, cut it.
- **Sprint boundary.** When an item finishes, re-check the backlog against the
  MVP. Adding rows and updating an item's status are routine — just do them.
  **Re-ranking rows or changing the MVP is mine to approve:** propose it, don't
  apply it — in whichever you write, the closeout or the handover, since at a
  boundary with rows left it is usually the handover. The next item starts in
  this session only if I say so; otherwise hand over.

## Say when a session is done — don't leave me guessing

**When the work is genuinely finished, say so explicitly and stop.** Not "let me
know if you need anything else" — a short closeout I can act on, three sections in
this order: **Shipped → Backlog → What's next.**

- **Shipped** — table: item | what changed | verified by. PR numbers or file
  paths; real output, not a claim. Mid-item, say what got done and where the
  item stands.
- **Backlog** — table: row | item | status. **Row is the spec's row number, listed
  in the spec's order, never a fresh judgment** — re-ordering is mine to approve (see Building).
  Status is the row's blocker: say **on what or whom**, naming the person where it
  is a person. Everything still owed, this thread's or the project's — print the
  top five and **say how many more there are**, never a silent five, plus any you
  propose to move (↑/↓, a few words why). Say "nothing" when it is nothing — that
  is the sentence I am waiting for. Single sitting, nothing left: one line.
- **What's next** — the one piece you would do next, and why that one. A
  recommendation, not a menu. If it needs a decision from me, say what it is.
  Finishing an in-progress item comes first. Say whether the build is still on
  course for its MVP.

**Do not write "Open", "Blocked on someone else" or "Verdict" as their own
sections.** Anything unfinished is backlog regardless of which thread created it;
blocked-ness is a property of a row, not a category; and a verdict restates what
the three sections already say. The progression is *work done → what remains →
the actionable next step*, and a status declaration is not an action.

**Read the backlog from its source, the spec's `## Backlog` section — never from
what you remember of the handover you were handed.** A handover is a snapshot,
rows land shipped between legs, and a row's own sizing is often wrong. Filter
shipped rows mechanically, not by eye. The blocked row is frequently the
higher-leverage move rather than the one that merely looks next.

- **Never call a session done to wrap it up neatly.** Half-built, unverified, or
  waiting on another session all go in **Backlog** even when that makes the
  closeout untidy. A false "all clear" is worse than none, because I stop looking.
- **Closeout is not `/handover`.** A handover exists so *another* session can pick
  work up, and it costs real tokens to write; closeout means there is nothing to
  pick up. If work genuinely continues, propose `/handover` instead and say why.
  Never do both.

Say it once, at the end — don't repeat it every turn or preface ordinary work
with it.

# Contradiction handling

When instructions conflict, follow this order, but only for the parts that
directly contradict. Always follow the non-contradictory parts of lower-priority
instructions.

1. The current conversation
2. A repo's own `CLAUDE.md`
3. Your workspace `CLAUDE.md`, but only where this file calls something a
   default (tone, working preferences)
4. This file
5. Personal preferences configured in the Claude apps
6. Memory

A personal addition that only *adds* a stop, such as an extra decision gate, is
always allowed. It never counts as a contradiction. To replace more of this file
than its defaults, turn the import off (`team_policy.import: false` in your
toolkit config) and carry your own version instead.

# Model roles — Opus architects, Sonnet executes

**If you have the builder profile** (`toolkit-build`, which gives you `/build` and its
agents — list `"continuity", "builder"` under `"profiles"` in `~/.config/claude-code-toolkit/config.json`
(keep `"continuity"`: a list replaces the default set),
then run `./install.sh` in your primary toolkit checkout, never a worktree. Its path is
the `claude-code-toolkit` entry in `claude plugin marketplace list`), then on **multi-file builds in a repo**
(tools, automations, skills — anything with real verification), split the work
by model instead of doing all of it in the session model:

- **Opus (main loop)** — design, sequencing, plan writing, judgment calls, review,
  and my decision gates. Stays in the conversation the whole time.
- **Sonnet (pinned subagents)** — `toolkit-build:executor` implements one plan task, then a
  fresh `toolkit-build:executor` proves it (verification, mutations); `toolkit-build:task-reviewer` reviews
  one task's diff, only when a later task builds on its contract. Both pinned
  `model: sonnet`. `/build` runs this end to end.

**Always pass an explicit `model` when dispatching any subagent.** An omitted model
silently inherits the session's, which means paying Opus rates for transcription
and defeating the split entirely.

**The plan is the contract.** Every plan task needs explicit acceptance criteria
and an exact verification command. A Sonnet executor won't push back on a vague
task the way Opus would — it will confidently build something plausible and wrong.
Both agents report `BLOCKED` rather than guess; that report is the system working.
Resolve it as the architect; never hand the same ambiguity back down. Two blocks on
one task means the plan is wrong, not the executor.

**Run verification in the foreground — never `nohup`, never `&`, never a detached
polling loop** — and tell subagents the same, in the brief. A laptop that sleeps
mid-run leaves a detached watcher busy-waiting, orphaned where a sandboxed `kill`
cannot reach it. Wait on work with the harness's own mechanism, not a shell loop.

**Where this does *not* apply:** single-context work — analysis, briefs, recaps,
drafts, one-file edits. The handoff costs more context than it saves. Don't split
it; just do the work.

## Spec-first planning

The plan lands in the configured specs directory (`spec_archive.specs_dir`, which
`/build` resolves; unset means ask) *before* implementation starts — a file, not a
conversation. Open it with front matter:

```markdown
---
status: live
subject: <what the work is about, e.g. toolkit>
---
```

`live` means still the active plan; `shipped` means the build landed;
`superseded` means it was replaced before it shipped. **When the PR merges, flip
`live` → `shipped` in the same breath** — with the builder profile,
`claude-spec-archive` moves shipped and superseded specs into `specs/archive/`
once they age past `min_age_days`, and **never** touches one that is undeclared
or `live`, so a plan nobody retires sits in scratch forever. The command dry-runs
by default and only ever moves files.

**On a build that spans sessions, give the spec a `## MVP` and a `## Backlog`
section** (see Building). `## MVP` is prose — 3–5 lines plus what is out, not rows.
`## Backlog` is **one row per outstanding piece, in priority order**, saying what it
is, what blocks it, who owns the block. That backlog is what the closeout's
**Backlog** reads from — its row number and order fill `row`, its blocker the `status`, both read
off rather than invented. Without it every closeout re-derives the project's state
from whatever the last handover said.

**Where this does *not* apply:** the same single-context work above. Don't write a
spec to satisfy the rule — that manufactures a file that then needs archiving.

# Your workspace

Your work lives under the workspace root you gave setup (`workspace_root` in your
toolkit config). How you lay it out is yours: describe it in your workspace
`CLAUDE.md`, and the toolkit never moves existing work into a tree of its own.

Keep it outside a cloud-synced folder if you can. On a Mac, Documents and Desktop
sync to iCloud when that is on, which makes them a bad home for files that get
rewritten or deleted as part of ordinary work.

# Working preferences

Defaults — adjust them in your workspace `CLAUDE.md`; they're not sacred.

- **Code:** prefer Python for data analysis and scripting. Readable names over
  clever abbreviations. Comment non-obvious logic, not the obvious. Keep scripts
  self-contained with minimal dependencies. Default to CSV/JSON output unless
  another format is clearly better.
- **Output:** Markdown, with headers and bullets so it stays scannable.
- **Files:** prefer editing existing files over creating new ones. Don't create
  README or docs files unless asked.

# Working conventions

- **Git — a save system first.** Claude runs every git step and explains it in
  plain English; you never need to type a git command.
  1. **Start** — each build gets its own branch, named for it: a private copy,
     so nothing reaches anyone else until it goes live.
  2. **Save as you go** — commit at every working checkpoint with a plain-English
     message. Where the repo has a remote, push the build's own branch at natural
     stopping points so the work is backed up. Pushing a build's own branch is a
     backup, not a decision gate; the permission prompt is the only confirmation
     it needs.
  3. **Go live** — when I say the build is done, open a pull request that says in
     plain English what the build does and how to try it, then squash-merge it so
     the whole build is **one** change on the default branch. Ask before opening
     and merging. A repo with no remote merges locally the same way.
  4. **Undo** — revert the build's commit, merged the same way as any other
     change. Nothing is erased; an undo is itself a change that can be undone.

  **Where merging takes effect by itself** — a repo that deploys, runs a
  scheduled job, publishes a package, or installs onto a machine — a revert
  restores the files but cannot undo what already ran. Review there happens
  *before* the merge. **This toolkit is one of them:** its primary checkout is
  what is installed, so editing it — on any branch — changes the live tools at
  once. Build there in a separate `git worktree`.

  **How deep that review goes depends on whether a defect would announce
  itself.** Something that crashes, renders blank or is obviously wrong on first
  use gets fixed in flight. Something that fails *silently* — numbers a human
  will trust unchecked, a guard or hook, scheduled work nobody watches, anything
  installed onto another machine — gets one blinded review pass before merge.
  With the builder profile, the steps live in `tools/build-lens/merge-review.md`
  in your toolkit checkout (`claude plugin marketplace list` shows its path for
  `claude-code-toolkit`): run `claude-build-route --review-mode` first and follow
  that mode's block, never steps rebuilt from memory. One round, then ship — fix
  what it finds, never re-review.
- **Concurrent sessions:** if another Claude session may be working in the same
  repo, create a dedicated `git worktree` for your branch instead of sharing one
  checkout — two sessions in a single working directory clobber each other. This is
  **enforced by a hook** (`claude-worktree-guard`), not just advised: it always
  blocks a git command reaching into a different checkout than the one you're
  standing in, and once a repo has two or more worktrees it also blocks branch
  switches, hard resets and stashing in a shared checkout. Escape hatch:
  `WORKTREE_ALLOW=1`.
- **Worktrees are authorized for concurrent sessions.** With the builder profile,
  if `claude-session-claim` blocks an edit because another live session already
  holds that checkout, you are instructed — by this file — to call `EnterWorktree`
  with the path it names and re-apply the edit there. This is the standing
  authorization `EnterWorktree` requires; it applies to every repo under your
  workspace, not just one.
- **Branch hygiene:** when a pull request merges, its remote branch is often
  deleted with it. Clear the matching local branch with `git tidy`: it
  fast-forwards the default branch and deletes a local branch whose remote is gone
  once it can prove the branch merged, and lists the rest, **without ever checking
  out the default branch**, so it can't pull you off whatever you're mid-work on.
  After a merge, run it as `git tidy --prune-worktrees`: that also removes
  worktrees identical to their merged PR (never one a live session holds). A
  branch whose remote vanished without a merge is kept and listed; delete it
  yourself once you've checked it.
- **One thread per unit of work.** Compaction is lossy and compounds. Durable state
  belongs in a repo, a spec, a PR body, or memory — not in the conversation. With
  the continuity profile you'll be nudged to run `/handover` when a thread gets
  long; take it at a natural boundary rather than mid-task.
- **Write durable facts down the moment they happen, not at handover time.**
  When I make a decision, answer a question you asked, state a preference, or
  confirm a fact about my accounts or tooling — write it in that same turn. Most
  threads never run a handover: they compact, get abandoned, or simply stop, and
  anything harvested only at the end dies with them. **Route before you write:**

  | What it is | Where |
  |---|---|
  | A fact about this machine or its accounts — a credential location, a connected platform, an installed or missing CLI, a mount, what's already automated | `TOOLING.md` (and its digest block if it belongs there) |
  | A decision, preference, correction or standing instruction — with the why | memory |
  | A rule about *how to work* that happens to name a tool | memory — it's a rule, not a config fact |
  | A rule that should hold in every session | your workspace `CLAUDE.md`, via the four buckets above |

  The test: **would this be different on another machine?** Yes → `TOOLING.md`.
  A tooling fact in memory is drift, not a harmless duplicate; if both hold it,
  `TOOLING.md` wins and the memory goes. And before telling me I never answered
  something, check memory — you can't see my other threads.

  The bar is deliberately low: a redundant memory costs one file, a lost decision
  costs the same conversation twice. If it revises something already in memory,
  **edit that file** rather than adding a second — two memories that disagree are
  worse than one that is stale. *Not* memory: what you did this session, file
  contents, anything the repo or git history already records.
- **Where work lives — versioned or scratch.** Two homes, and getting the split
  wrong either loses work or leaks something:
  - **Versioned, in a repo:** anything you or a future session should be able to
    find again on purpose — code, canonical reference docs, distilled research,
    durable configuration.
  - **Local scratch, never committed:** everything in progress or one-off —
    drafts, exports, data pulls, working notes, deliverables mid-flight. Free to
    hold data you wouldn't want in git history, which is exactly why it stays
    out of every repo.
  - Rule of thumb: *something you'd want to find again on purpose* → versioned.
    *Work product or raw data on the way to somewhere else* → scratch.

# Repo-specific instructions

Individual repos carry their own `CLAUDE.md` with conventions that only apply
inside them. Those take precedence over this file for work in that repo — read the
repo's own file before scaffolding anything in it. The toolkit checkout has one:
read it before changing the toolkit itself.
