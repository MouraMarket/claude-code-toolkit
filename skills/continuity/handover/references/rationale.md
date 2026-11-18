# Why the handover skill is shaped this way

Read from the handover skill when you want the reasoning behind a rule. Every rule below is also stated in `SKILL.md`; this file holds the reasoning.

Section numbers (§0–§5) are the handover skill's own `SKILL.md` sections.

## §0: the scope block skips a dead claim and never deletes one

The claims file is written under a lock by a safety hook, so an unlocked write
from the skill races every peer reading it. A stale-looking record can belong to a
live session, so deleting it is wrong even when it looks tidy. Paying one
`isdir` per run is the cheap side of that trade.

## §0: why the scope block keeps only existing claims, newest first

One session can hold several claims, one per checkout it has written to, in
insertion order rather than recency. Two failures live there, and taking the
first record hits both:

- A claim **outlives the worktree it names**. A session that finished a worktree
  and deleted it still holds a claim pointing at nothing, and accepting that
  path unchecked skips the cwd fallback and makes the block refuse outright.
- An **older surviving claim outranks the current one**, which hands back a real
  directory that is simply the wrong one: the quiet failure §0 exists to
  prevent.

Keep only claims whose directory still exists, then take the newest.

## §0: when cwd is the right fallback

With no usable claim, the session has not written to a repo, works in the
primary checkout (which records nothing), or its claim names a removed
worktree. cwd is the right answer in all three, but only because the block runs
in the main loop: a subagent's cwd resets to the primary checkout on every call.

## §0: why the leak table matters

Every row in the table returns a plausible, confidently formatted answer, and
the next thread has no way to tell a borrowed branch name from a real one.
Naming the checkout in the State section is what makes it checkable.

## §1: why the mission step exists

A thread almost never ends on the goal it started with. Discovery pulls in
related work, that work gets done, and the handover describes the **detour**,
because every other section of the template asks what happened and nothing
asked what you set out to do. A chain of handovers named after their detours
looks like several unrelated projects at a glance, and no one name groups them.

## §1: why a shipped mission needs an empty human-work section

"The PR merged" is not the same claim as "the mission shipped". A build can
land every line of code and leave a console or DNS step in a plan's appendix,
and the thing people actually open keeps serving the old version with nothing to
say so. One line of honest front matter separates those two states.

`mission_status` in a handover is inert to `claude-spec-archive`, which
classifies any handover-named file as unmanaged before it reads front matter.
That is why the key is `mission_status` and not `status`, and why the spec's own
`status:` is the flip that actually loses rows.

## §1: the missing-section trap

A spec that files its human work under some other heading (nested inside the
backlog, or under a per-person heading) gives the check nothing to read, and an
absent section is vacuously empty. That reading is how the bug ships.

## §1: what a chain looks like on disk

```
2026-08-28-billing-api--rate-limits-handover.md
2026-08-30-billing-api--retry-budget-handover.md
2026-08-31-billing-api--phase-3-handover.md
2026-08-30-reporting-v2--adapter-handover.md
```

`HANDOVER_NAME_RE` is `^\d{4}-\d{2}-\d{2}-.+-handover\.md$`, so mission and leg
both live inside the `.+` and need no code change.

## §2: archived is not a reason to skip draining

Archived is not deleted, but a file nobody will open again is still not a home
for a standing instruction. An answer recorded as an open question in one
handover gets asked again in the next thread.

## §3: why checking beats chasing

Re-asking an answered question costs the user's patience and their trust that
you retain anything, while leaving a real question unasked costs one round-trip.
Bias toward checking, not chasing.

## §3: why a thread may need no document

After §2 and reconciliation, most of what a handover used to carry already lives
in a durable file. A document restating the spec is a second copy that will drift
from it, so when nothing is left that only the document could hold, the kickoff
prompt carries the thread on its own.

## §4: why every shell block runs in the main loop

Two independent reasons. A subagent's Bash cwd resets to the primary checkout on
each call, so anything reading cwd describes the wrong worktree. And
`CLAUDE_CODE_SESSION_ID` is set per session including subagents, so a delegated
block reads the subagent's id: the snooze writes to a state file the hook never
reads, and registration stamps a `created_session_id` that does not match the
thread, which would let this session consume its own handover on the next
compact.
