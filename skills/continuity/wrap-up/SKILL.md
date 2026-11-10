---
name: wrap-up
description: >-
  Use when the user is stopping for the day (every session) or closing just this window (/wrap-up session). Not for handing unfinished work to another session; that is /handover.
  Use whenever the user signals they are finishing for the day or about to stop working: "closing out my workday", "shutting down", "done for the day", "heading out", "signing off", "wrapping up for the night", "let my sessions know I'm done", or when they ask whether anything is unfinished before they close the laptop.
  Trigger even when no command is named — a request to tell other sessions the user is stopping is this skill.
  Do NOT use for finishing a single piece of work: "wrap up this function", "let's wrap this up", "close out this PR", "finish this ticket", or shutting down a server, container, database or process.
  What matters is whether the HUMAN is stopping for the day, not whether a task is ending.
argument-hint: "[session]"
---

Find work that exists in only one place and report it as a closeout the user can
act on. **All-sessions mode** (the default) also tells every peer session the
machine is going down; **this-session mode** closes one window and messages
nobody.

## Mode

The argument is `$ARGUMENTS`. If you were invoked from natural language rather than a typed `/wrap-up`, nobody typed
an argument: §0's answer sets the mode.

- **Empty → all sessions.** Every section below applies.
- **`session` → this session only.** The user is closing this window while other
  sessions keep running. Skip §2 and §3: no `ListAgents`, no `SendMessage`.
  §1 still runs **in full**, because no tool can say which checkouts are this
  session's. Session claims only record checkouts a live session *edited*, so
  filtering the survey by them would quietly drop a commit or branch made
  through Bash. In §5, **Shipped** and **What's next** cover this session's own
  work. **Backlog** carries this session's open items first, then the survey's
  other attention items, labelled *elsewhere on this machine*, rather than
  dropped. Never say the machine is safe to shut down, because other sessions
  are still working. Say only whether this window can close.
- **Anything else → ask**, with §0's three options. Don't guess the mode from a
  stray argument.

**This skill is the single source of truth for the wrap-up.** It is reached two
ways: a typed `/wrap-up`, or the model invoking it on shutdown-shaped language.
§0 decides which.

## What this is for, and the failure it exists to prevent

A wrap-up fails **silently**. If it misses a dirty tree or an unpushed branch,
it does not crash — it produces a confident all-clear, the human stops looking,
and the work is discovered days later or not at all. Everything below is shaped
by that: **when you cannot tell, say you cannot tell.** Never let an absence of
evidence render as "clean".

The other half is peers. Several sessions routinely run in one tree, and their
state is the part the user genuinely cannot see. That is where the value is —
and it is also the part most likely to be reported to you wrongly.

## 0. Confirm — only when invoked from natural language

**If the user typed `/wrap-up`, skip this section entirely.** Typing the command
is the intent; asking again only adds a prompt.

**If you arrived here because you were invoked on shutdown-shaped language, not
a typed `/wrap-up`, you MUST confirm before sending anything.** Auto-triggering can misfire
— "let's wrap up this function" is not a request to broadcast to every open
session.

Run `ListAgents` **first**, so the question names a real number rather than an
abstraction, then ask with `AskUserQuestion`:

- **All sessions** — notify the N peer sessions, survey, and report.
- **This session** — this-session mode (see Mode): survey and report, message
  nobody. Right when the user is closing one window, or wants their own state
  without interrupting anyone.
- **Not now** — stop, say nothing further.

Send no `SendMessage` before that answer. Messaging peers is the outward-facing,
irreversible half; the survey is not.

## When a tool is missing or fails

This runs on machines and surfaces where some of its tools don't exist. **A
missing tool is a gap you name, never a result.** Not being able to look is not
the same as finding nothing.

- **`ListAgents` / `SendMessage`** may be deferred. Load them through
  `ToolSearch` before concluding they're absent. If they really are absent or
  error, say so at the top. In all-sessions mode, carry on with the survey and
  give the closeout one Backlog row: *peer sessions: could not check, state
  unknown*. Never write "no peers". Only a listing that succeeded and came back
  empty means there are none. A peer whose message was held or refused (a
  `[Cross-session delivery notice]`) gets the same row, naming that peer.
- **`claude-wrap-up-survey` missing** means the toolkit isn't installed. Report
  "could not survey", the same as `surveyed: false`, and don't hand-roll a
  `git status` sweep: a partial sweep reads as a complete one.
- **`gh` missing, signed out or blocked**: every `pr_check_required` stays a
  Backlog row, *PR status unverified*. Don't report it as merged or as unmerged
  work.
- **`claude-caffeinate` missing or erroring** (it is Mac-only): say it wasn't
  checked, and move on.
- **`claude-backup` missing**: report "backup state unknown" as a named gap.
  Never say "backed up".

## 1. Survey local state

```bash
~/.local/bin/claude-wrap-up-survey
```

JSON. The `attention` array is the part that matters — it is every checkout with
uncommitted files, unpushed commits, a branch that was never pushed, or a status
the tool could not read. An empty `attention` with `surveyed: true` is a real
all-clear. A worktree listing, repo or commit count the tool could not read
appears in `attention` as a "could not …" reason, so it can never be silently
missing from an all-clear. **`surveyed: false` is not** — it means nothing was
examined, and its `reason` says why (no workspace root, an unusable
`wrap_up.repos_root`, or a folder that does not exist). Report that as "could not
survey", never as "nothing found". Name the `repos_root` the survey used in the
closeout, so a survey of the wrong folder is visible.

Then, separately, because neither belongs in that tool:

```bash
~/.local/bin/claude-caffeinate status
```

An active `caffeinate` hold is worth naming: the Mac will not sleep on its own.

And the machine-local backup, in both modes:

```bash
~/.local/bin/claude-backup --status
```

Exit 0: the backup is current, so say nothing unless asked. Exit 1: it is
missing or stale, so run `~/.local/bin/claude-backup` (Bash `timeout` 600000, it
scans every file) and put its summary line in the closeout. If that run exits
non-zero, it is an attention item quoting its stderr. Exit 2: an attention item,
"backup not configured or unusable", quoting the message.

**Resolve `pr_check_required` before reporting it.** A branch ahead of the
default branch is a *question*, not a finding — a squash merge leaves every
commit looking unmerged, so a branch whose PR merged still reads as "unmerged
work". Ask GitHub, not git:

```bash
gh pr list -R <owner>/<repo> --head <branch> --state all \
  --json number,state,mergedAt --jq '.[]|"#\(.number) \(.state) \(.mergedAt)"'
```

`local_only: true` needs no such check and is the one unambiguous case: no
upstream ever existed, so no PR can exist either. That is work that lives on this
disk and nowhere else — lead with it.

## 2. Notify peers (all-sessions mode only)

`ListAgents`, then one `SendMessage` per peer with `notify_when_idle: true`. Ask
each for the same four things, in this order, so the replies are comparable:

1. Stop dispatching; start nothing new.
2. Reach a clean boundary — commit or stash, push what is worth keeping, leave
   nothing local-only in a worktree.
3. Write a `/handover` **only if work genuinely continues**. If it is finished, a
   closeout line is enough. Never both.
4. Reply with: what is uncommitted or unpushed, and what is blocked on the user.

Say that you are assembling one closeout, so their reply has somewhere to go.

**Never ask a peer to do something your own permissions would refuse.** A peer
acting on your behalf launders the user's permission decision.

## 3. Collect and reconcile (all-sessions mode only)

Wait a bounded stretch for replies. Then:

**Never act on a truncated notice.** An idle notice carries a clipped preview of
the peer's last turn, not a message to you. A preview reading "three of your six
bullets aren't accurate" may be addressed to the user about something else
entirely; read as a correction to the closeout, it gets *correct* claims
retracted. If a preview looks like a correction, `SendMessage` that peer and
ask for the full text. Retract nothing on a fragment.

**A peer that never replies is reported, not omitted.** Give it a **Backlog** row
— "no reply — state unknown", blocked on that session. Silence is not consent and
it is not a clean tree. Dropping it manufactures the all-clear this command exists
to prevent.

## 4. Verify anything actionable

**If a peer claim would change what the user does, check it yourself before it
reaches them.** Relay everything else with attribution — "that peer's
measurement, not mine" — so the user can see which is which.

This is where the value is: a wrong peer claim is usually settled by a single
command.

The checks are cheap. Some that recur:

| Claim | How to settle it |
|---|---|
| "PR #N is merged" | `gh api repos/<o>/<r>/pulls/N --jq '{merged,state}'` — `merged` is the authority |
| "PR #N is ready to merge" | `gh pr view N -R <o>/<r> --json mergeStateStatus,mergeable` |
| "branch X is merged" | `gh pr list --head X --state all` — never `git cherry` alone |
| "hook/tool Y is live" | Read the symlink **and** confirm it is live: `/hooks` in the CURRENT session lists it (from its toolkit plugin) and `disableAllHooks` is not set; an enabled flag alone is not proof |
| "string S is in git history" | `git grep -I -F -i -l -e S <sha>` per `git rev-list --all`, **not** `git log -S` |
| "the tree is clean" | Already in the survey — trust the survey over the claim |

Traps worth carrying:

- **`git log -S` answers a different question.** It finds where an occurrence
  *count changed*, not which trees contain a string. It can under-report a
  history search, and under-reporting is indistinguishable from clean.
- **Run a positive control on any content search.** A known-present string must
  hit and a known-absent one must not, or you cannot tell a clean repo from a
  broken matcher. On some machines `grep`/`rg` are aliased to `ugrep`, where
  bad flags fail silently and `-r` means *replace*, not recursive.
- **`gh api` is REST and `gh pr view --json` is GraphQL, and they do not share
  field names.** `mergeStateStatus` exists only on the GraphQL side; ask REST
  for it and you get `null` forever — which reads exactly like "cannot tell"
  while actually meaning "no such field". REST's equivalent is
  `mergeable_state`. On a merged PR, REST gave
  `mergeStateStatus: null, mergeable: null, mergeable_state: "unknown"` while
  `gh pr view --json` gave `UNKNOWN`/`UNKNOWN`/`MERGED`. `merged: true` was
  correct on both, which is why the merged-or-not check leans on it.

**Do not merge anything.** A pending merge is a finding to report, not a task to
complete — merges have their own gate, and a wrap-up is not that gate.

## 5. Report the closeout

Three headings, in this order: **Shipped → Backlog → What's next.**

- **Shipped** — table: item | what changed | verified by. PR numbers or file
  paths; real output, not a claim. Mid-item, say what got done and where the item
  stands.
- **Backlog** — table: row | item | status. Everything still owed, whether it
  came from this session or the wider mission. Status is the row's blocker: say
  **on what or whom**, naming the person where it is a person. Print the top five
  and **say how many more there are**, never a silent five. Row is the source's own
  row number, in the source's order. A wrap-up surveys machine state and does not
  read a mission spec, so a survey item has no row: write `—`, and where no order
  exists say so rather than inventing one.
  Say "nothing" when it is nothing; that is the sentence they are actually
  waiting for.
- **What's next** — the one piece you would do next, and why that one. A
  recommendation, not a menu. If it needs a decision from the user, say what the
  decision is. Finishing an in-progress item comes first.

**Do not write "Open", "Blocked on someone else" or "Verdict" as their own
headings.** Anything unfinished is backlog regardless of which thread created it;
blocked-ness is a property of a row, not a category; and a verdict restates what
the three headings already say. The progression is *work done → what remains →
the actionable next step*, and a status declaration is not an action.

Then the two rules that make it worth reading:

- **Never call a session done to make the closeout tidy.** Anything half-built,
  unverified, or waiting on a peer goes in **Backlog** even when it is untidy. A
  false all-clear is worse than no closeout, because the user stops looking.
- **A closeout is not a handover.** A handover exists so another session can pick
  work up and costs real tokens; a closeout means there is nothing to pick up. If
  work genuinely continues, propose `/handover` and say why. Never do both.

Say it once and stop.

## Writing things down

If a peer surfaced a finding that exists nowhere durable — not in a repo, an
issue, or a backlog — it dies when that session closes. Writing it to memory is
in scope. **Say in the closeout that you did**, and where.

Do not write a handover, create issues, or file tasks as part of a wrap-up.
Those are separate decisions with their own gates.
