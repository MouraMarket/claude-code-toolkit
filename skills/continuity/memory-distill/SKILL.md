---
name: memory-distill
description: >
  Distill an over-cap memory/ entry down to its ruled essentials, collapsing repeated
  incidents into general rules while preserving the original text as a verbatim
  incident history. Use when a `context-growth` PostToolUse notice names a memory
  file as over the memory distillation cap, or when asked to "distill this memory",
  "this memory file is over cap", "collapse the incidents into rules", or anything
  phrased like it. Also covers a manual check of whether a memory file under
  `~/.claude/projects/*/memory/` is due for distillation. Do NOT use for the whole
  MEMORY.md index nearing its limit (use memory-index-distill).
---

# Memory Distill

A memory file (`~/.claude/projects/<project>/memory/<name>.md`) is a rules file, not
a diary. It grows because every incident that produced a lesson gets a new section,
and most of what makes a memory long is the *evidence*, not the *rule*. This skill
splits the two apart: rules stay in `<name>.md`, capped; the verbatim incidents move
to `memory/history/<name>.history.md`, uncapped.

**Read this whole file before touching a memory file.** Step 0 below decides whether
full distillation is even the right move today.

## Step 0 — size triage first, always

**Look the cap up; never take "over cap" on trust.** The cap is configurable, per
machine and per file, so a number remembered from another file or another machine
is not this file's cap. Run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/context-growth/claude-context-growth" --cap <the memory file>
```

It prints the file's lines, its effective cap (the one the `context-growth` notice
uses, per-file overrides included) and a route. Follow the route:

| Route | What to do |
|---|---|
| **`under-cap`** | Nothing to distill. Say so with the numbers and stop. |
| **`delete-pass`** (a little over) | **Do not distill.** Look for a superseded rule to delete and for an incident that only *sharpens* an existing rule (see the third bucket below) rather than earning its own section. Deleting a dead rule is worth more than the bytes it frees — a recall handing over a superseded rule as current is the actual hazard, not the line count. If nothing is superseded and nothing needs sharpening, say so and stop. That is a valid, cheap outcome — not a failure to distill. |
| **`distill`** (well over) | Full distillation: collapse repeated incidents into rules, move the verbatim body to `history/`. |

The notice fires at a single cap because a second, lower threshold in the hook
would have to be kept in step with the first forever; the routing judgment lives in
the command instead. The band exists because full distillation is not worth its own
cost until the file is well clear of the cap. What a distillation saves comes from
collapsing repeated incidents, so a large file has a lot to collapse and a file just
over the cap has little: the tokens spent distilling it are better spent on the
delete pass. To see where that point sits for your own files, compare a file's size
before and after a distillation.

## The three-bucket write rule

This is the whole point of distillation, and skipping it is how "refinements" quietly
reintroduce the unbounded growth this skill exists to stop. Every incident sorts into
exactly one bucket:

1. **Genuinely new lesson** — nothing in the fact file covers it. It earns its own
   section.
2. **Already covered** — an existing rule already says this. Add **nothing**. A
   memory file is not a log of every time a rule was confirmed correct.
3. **Sharpens an existing rule** — the incident refines *how* to apply a rule that is
   already there (a sharper trigger condition, a corrected number, an edge case the
   rule missed). **Edit that rule's how-to-apply text in place. Never append a new
   section for this.** This bucket is the one a first pass tends to miss, because a
   sharpening incident reads like new material — but treating it as bucket 1 is
   exactly the growth this skill was built to stop.

## Fact vs history — two files, two contracts

- **`<name>.md`** — rules, why they hold, and how to apply them. Capped whole-file
  (280 lines by default; `--cap` above prints this file's). This is what gets read
  and recalled.
- **`memory/history/<name>.history.md`** — the dated, verbatim incidents behind those
  rules. Uncapped. **Deliberately absent from `MEMORY.md` and never auto-recalled** —
  it exists to be read on purpose, when someone needs the original case, not to be
  loaded by default.
- The fact file carries **one pointer line** back to its history whenever a history
  file exists for it (see any already-distilled memory for the wording), so a reader
  always knows the evidence exists even though it is never pulled in automatically.

## Superseded rules are deleted; incident evidence is archived

These are different calls, made about different things, and conflating them is a
real hazard:

- A rule that is **no longer true** — superseded by a later decision, a retired
  design, a fixed bug — gets **deleted from `<name>.md` outright.** Do not move it to
  history "just in case." A recall that hands a dead rule back as if it were current
  is worse than losing it; archiving it does not fix that, it just delays the mistake.
- The **incident text** behind a still-live rule is **archived**, never deleted — it
  is the evidence a rule was ever needed, and it is what the mtime guard below exists
  to protect.

## The mtime guard is mandatory — two commands, in this order

`memory/` has no version control, at most a periodic `claude-backup` copy (days
old at best), so a lost write is still lost, and more than one session can be
reading or writing it at the same time. There is no way to detect a stale read
except by checking the file right before writing it, so both commands below are
required —
never write a distilled file to a memory path directly. A direct `Write` over an
existing memory file is refused by `claude-distill-write-guard` for this reason.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/distill-guard/distill_guard.py" stamp <abs-path-to-memory-file>
# prints a token on its own last line -- keep it
# ... read the file, write the distilled text to a NEW staging file (where: below) ...
python3 "${CLAUDE_PLUGIN_ROOT}/tools/distill-guard/distill_guard.py" install <abs-path-to-memory-file> <path-to-new-file> <token>
```

- **Stage the new file where a write needs no permission:** the session scratchpad
  if your system prompt names one, else a new `distill-staging/` folder under the
  current directory. Never outside the working directory and never under
  `~/.claude/`: Claude Code asks permission for that write, or refuses it in an
  unattended run. If a staging write is refused anyway, re-stage in one of those two
  places and carry on; a refused staging write is not a reason to stop.
- `stamp` prints a token describing the target's mtime and size at the moment you
  read it. **Pass that exact token back to `install`.** The guard keeps no state
  between the two commands — the token is the whole record, which is what makes the
  check mean *"has this changed since I read it"* rather than *"since anyone last
  looked at it."* A shared stamp file would let a peer session's stamp silently
  re-arm your install; there is no shared file to do that with.
- `install` re-checks mtime and size against the token. If either moved, it aborts
  with exit `1`, leaves the target and the staged file both untouched, and prints why
  — a peer session wrote to the file between the stamp and the install, and the fix
  is to re-read and redo the distillation, not to re-stamp and force the write through.
- Every input is checked **before** anything is written, including the staged file's
  existence. A mistyped staged path aborts having touched nothing, so a retry cannot
  leave a duplicate section in `history/`.
- On a match, it snapshots the target's **current** content into `history/` in the
  same process, before moving the staged file into place — so the original text
  survives even if the distillation itself turns out to be wrong.
- **The history snapshot never truncates an existing history file.** A first
  snapshot creates `history/<name>.history.md`; every later distillation of the same
  file appends a new dated section to it. This matters specifically because
  `memory/` has no version control and at most a days-old `claude-backup` copy:
  an install that overwrote an existing history would destroy the verbatim
  incident record, with nothing current to recover it from.
  Always pass the target as the same absolute path both times — the guard derives
  `history/` from exactly the path it is given, and never reconstructs one from a
  bare name.

## Worked examples

Wherever a `history/` subdirectory already exists next to a memory file, the pair is
a worked example of this split. Find them with:

```bash
find "$HOME/.claude/projects" -path '*/memory/history/*.history.md'
```

(Not `ls ~/.claude/projects/*/memory/history/`: with no match, zsh refuses the
unmatched glob before `ls` runs, which reads as an error rather than "none".)

Read a few of both sides before distilling your first file — they show the split in
practice: what stays in the fact file, what moves to history, and how the pointer
line back to history is worded. If that prints nothing (or says the projects
directory does not exist), this machine has no distillations yet and the rules
above are the whole specification.

## Verify frontmatter after writing

Before finishing, re-open the fact file and check its frontmatter:

- `name` matches the filename (`review-discipline.md` carries `name:
  review-discipline`, not something else).
- `type` is one of `user`, `feedback`, `project`, or `reference` — nothing else.
- There is **exactly one** of each key. No duplicates.

This step is not a nicety. A duplicate `node_type` key was written by hand during
the original distillation sweep and was caught only because a validation pass ran
afterward — nothing else in the pipeline would have noticed it.
