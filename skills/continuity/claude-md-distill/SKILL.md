---
name: claude-md-distill
description: >
  Distill an over-cap CLAUDE.md-family file down to its rules, moving the accumulated
  reasoning behind them into a verbatim rationale archive that is never loaded. Use
  when a `context-growth` notice names a `CLAUDE.md`, a workspace `CLAUDE.md`, or
  `CLAUDE.md.template` as over its cap, or when asked to "distill CLAUDE.md", "this
  CLAUDE.md is over cap", "move the rationale out of CLAUDE.md", "cut CLAUDE.md down",
  or anything phrased like it. Not for memory files — those are `memory-distill`. Also
  use it when a `context-growth` notice reports that always-on instructions are over
  budget, or when asked to consolidate the always-on context.
---

# CLAUDE.md Distill

A CLAUDE.md is a rules file that is injected into every session that loads it. It
grows because a rule is cheap to write and paid forever, and because most of what
makes one long is the *reasoning*, not the *rule*. This skill splits the two: rules
stay in `CLAUDE.md`, capped; the reasoning moves to a sibling `CLAUDE.history.md`,
uncapped and never loaded.

**Read this whole file before touching a CLAUDE.md.** Step 0 decides whether
distilling is even the right move today.

## What this is NOT for

- **A memory file** (`~/.claude/projects/*/memory/*.md`) → `memory-distill`. Different
  split, different archive location, different triage.
- **A `.claude/rules/` file or a memory topic file** → no archive route. Its sibling
  archive would itself be loaded, so `distill_guard.py install` refuses it. Change it
  with a targeted Edit. For a rules file, add a `paths:` list or move the rule into
  `CLAUDE.md`. For a topic file, split it into two topics.
- **`TOOLING.md`** → neither skill. Its digest block is *capped* rather than growing,
  and its archive already exists: the body of the file the digest is already inside.
  The fix there is demoting rows out of the `DIGEST` block into the body below, which
  is already on-demand. Nothing is relocated and no archive is created.
  **Over the cap the block is withheld entirely** (since 2026-09-14) — the session
  gets no map at all — so an over-cap digest is urgent in a way an over-cap
  `CLAUDE.md` is not, and demoting rows is the only fix. Do not raise the cap.

## Consolidation mode — the always-on total is over budget

**Entry:** a separate path through this skill. When you come here from the always-on
budget notice or a direct ask, **Step 0 and the file-type exclusions in "What this is
NOT for" do not apply**: the target is the always-on *total*, not one file over its
cap, and memory rows and the `TOOLING.md` digest are in scope. These sections still
apply: the split criterion, "Superseded rules are deleted", the mtime guard, and "After
writing". When Consolidation mode is done, stop. Do not continue into Step 0.

**When:** that notice (`[context-growth] always-on instructions are over budget.`), or
a direct ask to consolidate the always-on context. The scope is the always-on surfaces
`claude-context-budget --report` lists: the loaded `CLAUDE.md` files, pathless
`.claude/rules` files, `MEMORY.md`'s Always rows, and the `TOOLING.md` digest.

**Pass A — blocks that are stale or stated twice:** delete a block that is no longer
true. Where two always-on surfaces state the same rule, keep the copy whose **scope
covers every session the other reaches**: a user-level `~/.claude/CLAUDE.md` rule
reaches every workspace, so deleting it in favour of a workspace copy narrows it. If
neither copy's scope covers the other's, keep both, or name the narrowing in the
question under step 6.

**Pass B — Always rows already carried elsewhere:** a memory whose rule a loaded
`CLAUDE.md` or rules file already states is retyped `type: reference`, **and its row is
moved by hand**: remove it from `MEMORY.md`'s Always section and add it to the topic
file (`memory/topics/<name>.md`) that fits, as `- [Title](../file.md) — hook`.
`claude-memory-lint` reports misplaced rows but never moves one. **Work in the memory
directory the budget measured** — the `MEMORY.md` path `claude-context-budget --report`
prints under its index row — because `claude-memory-lint`'s own default can resolve a
different one. After the move run `claude-memory-lint --report <that directory>`,
confirm the report names that directory, and confirm zero findings, so the memory is
still reachable. The memory file stays.

**Pass C — true duplicates only:** two memories stating the same rule merge into one,
and the other's row goes. Memories on a shared theme are not duplicates; do not merge
them.

**Decide nothing alone:** collect every proposed change, then ask through
`AskUserQuestion`: one grouped question per pass, each leading with a recommended
default. Past ~40 items, never ask per row.

**Write:** any `CLAUDE.md`-family rewrite goes through `distill-guard`'s
`stamp`/`install` (the mtime-guard section below). Where a rule moved into a rules
file, leave a one-line pointer.

**Re-measure:** run `claude-context-budget --report` and state the always-on total
before → after.

**Proof, or say it did not run:** Passes B and C move rules one hop away, and a rule
one hop away can stop firing. Before the write, list the rules the pass moves. After
it, check each still applies on a fresh session task that should trigger it. With no
harness to run that check, say plainly the proof did not run. Never report the pass as
verified.

## Step 0 — size triage first, always

**Look the cap up; never take "over cap" on trust.** A `context-growth` notice
names it. On a direct ask, run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/context-lint/claude-context-lint" --report <the file's directory>
```

It prints each `CLAUDE.md` with its lines, cap and state, plus a Core table when a
file has a Core (its own, tighter cap). Triage on the cap the file is over. If it
is under every cap the report shows, say so with the numbers and stop: there is
nothing to distill.

| How far over cap | What to do |
|---|---|
| **Under ~40 lines over** | **Do not distill.** Look for a rule that is no longer true and delete it, and for material that merely *sharpens* a rule already present (bucket 3 below) and fold it in. If nothing is superseded and nothing needs folding, say so and stop — that is a valid, cheap outcome, not a failure. |
| **~40+ lines over** | Full distillation: apply the split criterion, move the reasoning to the archive. |

The notice fires at a single cap because a second, lower threshold in the hook
would have to be kept in step with the first forever. The routing judgment belongs
here instead. A file barely over cap buys little from a full pass, and the tokens
are better spent on the delete pass.

## The split criterion

**Stays in `CLAUDE.md`:**
- The imperative — what to do, what not to do.
- The trigger condition: *when* the rule applies, and when it does not.
- Exact commands, flags, paths, thresholds, and any number that gets acted on.
- Which of several options is the default.

**Moves to the archive:**
- The incident that produced the rule.
- Measured evidence and the numbers behind a decision that is now settled.
- Superseded versions of a rule, and the history of how it changed.
- Worked examples and illustrative walkthroughs.

**The exception — keep one clause inline wherever a rule was previously overturned
or re-litigated.** A bare rule with no scar tissue invites the argument again, and
the whole cost of that argument lands on the person who has to re-make the decision.
One clause is far cheaper. The tell is dated language already in the file:
"re-litigated four times", "this said X until <date>", "corrected", "was wrong".
Where you see that, the rule has a history of being reopened — keep just enough to
close it.

## The four-bucket write rule

This is what actually bounds growth, and skipping it is how a distillation is undone
within weeks. Every new piece of material sorts into exactly one bucket:

1. **Genuinely new rule** — nothing in the file covers it. It earns its own section.
2. **Already covered** — an existing rule says this. Add **nothing**. A CLAUDE.md is
   not a log of every time a rule turned out to be right.
3. **Sharpens an existing rule** — a sharper trigger, a corrected number, an edge
   case the rule missed. **Edit that rule in place. Never append a new section.**
   This bucket is the one a first pass misreads, because sharpening material reads
   like new material.
4. **New rationale for a rule already present** — evidence, an incident, a
   measurement that justifies something the file already says. **Goes straight to
   the archive.** Do not inline it.

**Bucket 4 is the one specific to this file family, and it is where the growth comes
from.** The default destination for a new *why* is the archive, not the injected
file.

## The archive — one sibling file, append-only

`CLAUDE.history.md`, a direct sibling of its source (`CLAUDE.md.template`'s archive
is `CLAUDE.md.history.md` — the extension split puts the stem at `CLAUDE.md`).

- **Never loaded.** Claude Code discovers `CLAUDE.md` and `CLAUDE.local.md` only —
  verified against the binary and by live probe. The archive is cold by construction,
  not by convention, which is what makes moving text into it a real reduction.
- **Append-only.** Each distillation adds a dated section. The header is written once,
  on first creation, and never rewritten.
- **Uncapped**, and the growth notice does not match it.

A rule moved to the archive is *retired, not deleted* — recoverable when someone
challenges the rule and wants to know why it exists.

## Superseded rules are deleted; reasoning is archived

Different calls about different things, and conflating them is a real hazard:

- A rule that is **no longer true** — superseded by a later decision, a retired tool,
  a fixed bug — is **deleted outright.** Do not archive it "just in case". A reader
  handed a dead rule as current is worse off than one who never saw it.
- The **reasoning** behind a rule that is still live is **archived, never deleted.**
  It is the evidence the rule was needed.

## The mtime guard is mandatory — two commands, in this order

A workspace `CLAUDE.md` is typically not in a git repo, so there is no version
control and at most a periodic `claude-backup` copy (days old at best), so a lost
write is still lost, and several sessions can be editing it at once. Distillation
rewrites the whole file. There is no way to detect a stale read except to check the
file immediately before writing it.

```bash
TOKEN=$(python3 "${CLAUDE_PLUGIN_ROOT}/tools/distill-guard/distill_guard.py" stamp <abs-path>)
# ... read the file, write the distilled text to a NEW staging file (where: below) ...
python3 "${CLAUDE_PLUGIN_ROOT}/tools/distill-guard/distill_guard.py" install <abs-path> <new-file> "$TOKEN"
```

- **Stage the new file where a write needs no permission:** the session scratchpad
  if your system prompt names one, else a new `distill-staging/` folder under the
  current directory. Never outside the working directory and never under
  `~/.claude/`: Claude Code asks permission for that write, or refuses it in an
  unattended run. If a staging write is refused anyway, re-stage in one of those two
  places and carry on; a refused staging write is not a reason to stop.
- `stamp` writes its confirmation to **stderr** and the token alone to **stdout**, so
  the `$( )` capture above is safe. Do not add `| tail -n1`, and if you ever see the
  confirmation on stdout, that is a regression — a test pins the contract.
- **Pass that exact token back to `install`.** The guard keeps no state between the
  two commands; the token is the whole record. That is what makes the check mean
  *"has this changed since I read it"* rather than *"since anyone last looked"*.
- `install` re-checks mtime and size. If either moved, it aborts with exit `1`,
  leaves both files untouched, and says why — a peer wrote to the file between your
  stamp and your install. **The fix is to re-read and redo the distillation, not to
  re-stamp and force the write through.**
- Every input is validated **before** anything is written, so a mistyped staged path
  aborts having touched nothing and a retry cannot duplicate an archive section.
- On a match it snapshots the target's **current** content into the archive in the
  same process, before moving the staged file into place — the original survives even
  if the distillation turns out to be wrong.

**The guard only protects writes that go through it.** A whole-file `Write` over an
existing in-scope file is now refused by `claude-distill-write-guard`, which is what
forces the rewrite through `stamp`/`install` in the first place. A Bash rewrite
(`cat >`, `python open('w')`, `mv`/`cp` over the file) still bypasses both checks
and destroys a concurrent write silently.

## After writing, check what you changed

- **Re-read the distilled file top to bottom.** A rule that lost its trigger
  condition is now a rule that fires everywhere, which is worse than the bloat.
- **Every pointer resolves.** A rule that says "see the section below" after the
  section moved to the archive is a dangling reference; either inline what it needed
  or point at the archive explicitly.
- **The file is actually under its cap** — count it rather than assuming:
  `wc -l <file>`.
- **A `CLAUDE.md.template` change reaches no machine already set up.** The installer
  never copies it; only `/onboard` and a manual copy place the file, so an existing
  workspace `CLAUDE.md` keeps its old text until someone copies the change across by
  hand. Say so and stop. `policy/CLAUDE.md` is the opposite: it is imported live, so
  distilling it changes the instructions of every installed copy on its next update.

## Worked examples

Wherever a `*.history.md` file sits next to the CLAUDE.md-family file it archives,
the pair is a worked example of this split. **This must not assume a git repo** — a
workspace `CLAUDE.md` is typically not in one, so a recipe keyed on
`git rev-parse --show-toplevel` finds nothing there and, if its own failure is
swallowed with `2>/dev/null`, looks identical to "no distillations exist." It also
has to match both archive shapes: `CLAUDE.md`'s is `CLAUDE.history.md`, but
`CLAUDE.md.template`'s is `CLAUDE.md.history.md` (the extension split puts the stem
at `CLAUDE.md`) — a glob for the literal name `CLAUDE.history.md` never finds it.

Search the directory holding the file you're about to distill, plus your home
directory, and let any real error print rather than redirecting it away:

```bash
find "$(dirname "<abs-path>")" "$HOME" -maxdepth 2 -name '*.history.md'
```

If that finds nothing, this machine has no distillations yet and the rules above are
the whole specification.
