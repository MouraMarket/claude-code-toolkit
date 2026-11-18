---
name: handover
description: Hand this thread over to a new session — a handover document when one is needed, and always a paste-ready kickoff for the next thread
---

Hand this thread over so the next one can pick it up without re-deriving what
happened. **Every run ends with something to paste into a new thread**: a
one-line pointer to a handover document, or, when §3 finds no document is
needed, a short kickoff prompt that stands on its own.

**This template is the single source of truth for handover format.** The watch
hook deliberately does not restate it.

## 0. Establish your scope before gathering anything

Several sessions can run in one tree, each in its own worktree. Nothing about a
git command tells you which one you are describing, so a handover will happily
report another session's branch as its own. Resolve your checkout first and
derive every git fact from it.

**Run this in the main loop.** A subagent's Bash cwd resets to the primary
checkout on every call, so a delegated `git status` reports `main`, and reports
it confidently.

```bash
python3 - <<'PY'
import json, os, subprocess

CLAIMS = os.path.expanduser("~/.claude/session-claims/claims.json")
session = os.environ.get("CLAUDE_CODE_SESSION_ID")

checkout, source = None, "session claim"
try:
    # One session can hold several claims, one per checkout it has written to,
    # in insertion order rather than recency. A claim also outlives the
    # worktree it names. Keep only claims whose directory still exists, then
    # take the newest.
    mine = [r for r in json.load(open(CLAIMS))
            if r.get("session_id") == session and r.get("checkout")
            and os.path.isdir(r["checkout"])]
    if mine:
        checkout = max(mine, key=lambda r: r.get("claimed_at") or "")["checkout"]
except Exception:
    pass

if not checkout:
    # No usable claim: this session has not written to a repo, it works in the
    # primary checkout (which records nothing), or its claim names a removed
    # worktree. cwd is the right answer in all three, but ONLY because this
    # block runs in the main loop.
    source = "cwd, no usable claim"
    checkout = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True).stdout.strip()

if not checkout or not os.path.isdir(checkout):
    raise SystemExit(
        "could not resolve a checkout -- say so in the handover instead of "
        "reporting git facts you cannot scope")

def git(*args):
    return subprocess.run(("git", "-C", checkout) + args,
                          capture_output=True, text=True).stdout.strip()

base = next((b for b in ("origin/main", "origin/master", "main", "master")
             if git("rev-parse", "--verify", "--quiet", b)), None)

print("checkout:", checkout)
print("  source:", source)
print("  branch:", git("rev-parse", "--abbrev-ref", "HEAD"))
print("    HEAD:", git("rev-parse", "--short", "HEAD"))
print("   dirty:", len(git("status", "--porcelain").splitlines()), "file(s)")
if base:
    print("   range: %s..HEAD" % base)
    for line in git("log", "--oneline", "%s..HEAD" % base).splitlines():
        print("      ", line)
else:
    print("   range: no default branch found -- scope the diff by hand")
PY
```

When the builder profile is installed, `claude-session-claim` records
`session_id -> checkout` for every session that writes a file, which is why the
claim is authoritative and cwd is only a fallback. If the block refuses, **write
the handover without git facts and say why**: an unscoped branch name is worse
than no branch name.

**Read the `source` line before you trust the checkout.** `session claim` is
evidence: the claim hook recorded that this session wrote a file there.
`cwd, no usable claim` is a **guess**, and it is wrong in a case that looks
normal. A session that edited files outside any repo, or only deleted worktrees
and branches, holds no claim at all, and an ordinary `cd` earlier in the thread
leaves cwd pointing at a repo it never touched. The block then reports a clean
tree for a session that did a great deal.

So when `source` is the fallback, **do not report git facts as this thread's
work** unless you can say what you actually changed there. Name the checkout as
unverified, or write the State section as prose about what you did and where,
which for a thread spanning several repos, or none, is the truthful shape anyway.

**This block skips a dead claim; it must never delete one.** A stale-looking
record can belong to a live session, and an unlocked write from here races every
peer reading the claims file. `references/rationale.md` has the reasoning behind
this and the other §0 rules.

### Then say what happens to the worktree

Record a `hold` (pausing, means to come back) or `retire` (shipped, checkout
disposable) disposition so `git tidy`'s worktree report can tell a pause from an
abandoned session: read `references/worktree-disposition.md` and run its block
in the main loop. **Skip it entirely if you are unsure**: an absent disposition
leaves the report on its own evidence, which is the safe default.

### What leaks, and what to do instead

| Leaks | Because | Use instead |
|---|---|---|
| `git branch`, `git branch -a` | lists every branch in the shared object store, including other sessions' in-flight work | the resolved `branch` above |
| `git log --all` | interleaves other worktrees' HEADs, often newer than yours | `git -C <checkout> log <base>..HEAD` |
| `git stash list` | one stash stack, shared by every worktree | nothing; it is not yours to report |
| a subagent running any of the above | its cwd is the primary checkout, not yours | run it in the main loop |
| per-directory notes injected at session start (a memory plugin's digest, for example) | they are per-directory, not per-session, so they carry other windows' work | your own transcript |
| sibling files in `<handover_dir>` | every session writes there; their open items are not yours | only what this thread did |

The failure mode is quiet: every one of these returns a plausible answer.
Naming the checkout in the State section is what makes it checkable.

## 1. Identify the mission, then choose the path

A thread rarely ends on the goal it started with. Discovery pulls in related
work, that work gets done, and the handover describes the **detour**, because
every other section of this template asks what happened and nothing asks what
you set out to do. A chain of handovers named after their detours looks like
several unrelated projects, and the goal that started it drops out of view.

So resolve two things before writing anything.

### The mission

**The mission is what the FIRST thread in this chain set out to deliver.** Not
what this thread worked on, and not whatever is most urgent now.

- **You attached a handover?** The mission is whatever its front matter
  declares. **Inherit it verbatim.** You do not get to re-derive it, and a
  thread that spent every token somewhere else inherits it unchanged; that is
  the point.
- **No handover attached?** The mission is what the user asked for in their
  first substantive prompt. Go read that prompt. Do not summarise the thread
  back to yourself: by the end of a long thread the detour is the most
  available thing in your context, and left to compete on availability it wins.
- **The mission shipped this thread?** Say so, and only then may the next
  thread open a new one.

Declare it in the handover's front matter, so the next thread inherits rather
than re-derives:

```yaml
---
mission: billing-api
mission_spec: /Users/you/specs/2026-08-28-billing-api.md
mission_status: live          # live | shipped | abandoned
---
```

`mission_spec` is the absolute path of the file holding the mission's backlog,
normally in `<specs_dir>` (see §2). `mission_status: abandoned` requires a
reason **and a decider** in the Mission section: a mission that quietly stops
being mentioned is indistinguishable, from the outside, from one that shipped.

**`mission_status: shipped` requires the spec's `## Waiting on a human` section
to exist and carry no outstanding row.** Read the spec and check before you
write it. A mission with a console step still outstanding is `live`, however
finished the code looks: "the PR merged" is not the same claim as "the mission
shipped."

**The same test gates the spec's own `status:`, and that is the flip that
actually loses rows.** `claude-spec-archive` reads the *spec's* `status:` and
nothing else; `mission_status` in a handover is inert to it. A spec flipped to
`status: shipped` with rows still outstanding is moved into `<specs_dir>/archive/`
once it clears the age floor and skipped by every later run, and the rows go
with it. `status: live` holds it at any age.

**A missing section fails this check; it does not pass it.** An absent section
is vacuously empty. If it is not there, find where that mission's human work is
written down, move it under the right heading, and check it then.

### The path

`<handover_dir>/YYYY-MM-DD-<mission>--<leg>-handover.md`

`<handover_dir>` is `thread_handover.handover_dir` in
`~/.config/claude-code-toolkit/config.json`. **Resolve it before you write the
file**: run the escape-hatch block in §3 (it prints `handover_dir` along with
the grace numbers you need anyway). Never assume a default; registration in §4
refuses a file outside the configured directory.

- `YYYY-MM-DD` is today's date.
- `<mission>` is the mission slug, lowercased and hyphenated, **inherited, not
  re-derived**. It is the same string in every file of the chain, which is what
  makes `ls *-<mission>--*` return the whole mission in one command.
- `--` (two hyphens) separates them. It is the only double hyphen in the name,
  so it stays unambiguous to a human scanning `ls` and to anything splitting on
  it.
- `<leg>` is what **this thread** actually did, lowercased and hyphenated.
- The filename **must** end in `-handover.md`: the sweep matches on it, and a
  file named otherwise is never delivered or cleaned up.

```
2026-08-28-billing-api--rate-limits-handover.md
2026-08-30-billing-api--retry-budget-handover.md
2026-08-31-billing-api--phase-3-handover.md
```

**Never omit the leg**, even when the thread did nothing but the mission. Two
handovers for one mission on one day would otherwise share a filename, and the
second write would silently overwrite the first. Where no natural leg name
exists, use the phase or iteration (`--v2`, `--phase-3`).

## 2. First, drain everything durable into memory

**Do this before writing a single line of the handover.** A handover is a
*scratch* document: it is archived out of the way once its grace period
passes, and it is only ever read if someone pastes its path. Anything that must
outlive this thread does not belong in it.

The test is one question: **would this still be true next month?**

| Still true next month | Where it goes |
|---|---|
| A decision the user made, and why | memory |
| An answer the user gave you | memory |
| A standing instruction or preference | memory |
| A fact about this machine or its accounts (a CLI, an account, a connection, a venv) | the user's `TOOLING.md`, if they keep one |
| A corrected wrong belief about a repo, the harness or a payload | that repo's `CLAUDE.md` |
| **A task discovered this thread that belongs to the mission and is not done** | **the mission spec's Backlog** |
| **The mission's ordered backlog itself** | **the mission spec**, never here |
| Branch, commit, what is half-done, what to do next | **the handover** |
| Gotchas specific to this build | **the handover** |

Walk the thread and write each durable item to memory **now**, then reference
it from the handover rather than restating it. A standing instruction recorded
only in a handover expires with the handover, and an answer recorded there as
an open question gets asked again.

If a durable item contradicts an existing memory, **update that memory** rather
than adding a second one. Two memories disagreeing is worse than one being
stale.

### The mission backlog lives in the spec, not in this file

Discovered work outlives the thread that found it, so it goes in the mission
spec's Backlog, never here.

**Where the spec is:** the configured specs directory, `<specs_dir>`, printed by
`python3 "${CLAUDE_PLUGIN_ROOT}/lib/toolkit_config.py" specs-dir` (exit 3 means
the key is unset: ask the user), per the spec-first planning rule. `status:
live` keeps it clear of `claude-spec-archive` until the mission ships, at which
point flipping it to `shipped` retires it.

Flipping the spec to `shipped` takes the same `## Waiting on a human` test as
§1's `mission_status`. **Read `references/mission-spec.md` now if the mission
has no spec, or its spec lacks a `## MVP` or a `## Waiting on a human`
section**: it says when a spec must be written, what it must hold, and how the
human-work section passes.

## 3. Write the document

### First: if you attached a handover, reconcile with it

**Skip this only if this thread attached no handover.** Otherwise read
`references/reconcile.md` before writing, then account for **every** open item
in the handover you attached, and its mission front matter: done (State), still
open for the mission (the spec's Backlog), this thread's own loose end (Open
work), or deliberately dropped (Open work, with the reason). There is no fifth
outcome.

### Then: decide whether this thread needs a document at all

After §2 and the reconciliation above, most of what a handover used to carry
already lives in a durable file. Sometimes nothing is left that only this
document could hold, and a document restating the spec is a second copy that
will drift from it. **Skip the document only when every one of these holds:**

- **Nothing half-done lives only in this thread.** §0 printed `dirty: 0`, and
  every commit in its range is pushed to a branch or a PR, or the thread
  touched no repo at all. Uncommitted or unpushed work always gets a document.
- **Open work is empty.** Everything outstanding went to the mission spec's
  Backlog or `## Waiting on a human`, or to memory. If reconciliation put even
  one item in *Open work*, write the document.
- **No gotcha or corrected wrong belief is left over.** §2 routed each one to
  memory or a repo's `CLAUDE.md`; nothing build-specific is waiting for the
  *Credentials, paths, and gotchas* or *Corrected wrong beliefs* sections.
- **The next step is one sentence that points at a durable file**: a spec row,
  an open PR, a plan task. If explaining it takes a paragraph, that paragraph
  is the document.

**This is not a way to skip §2.** The test runs *after* the draining and
reconciliation, and it passes only because they ran. When unsure, write the
document: a short document costs a file, a missing one costs the next thread
everything it cannot re-derive.

**If it passes, write no file and skip §4**: there is nothing to register, so
SessionStart will not point anyone at this thread. The kickoff prompt in §5 is
the only carrier, which is acceptable precisely because the test above
guarantees everything it points at is durable. Go to §5.

Otherwise, write the document with the sections below.

### Then: the sections

Use these sections in this order. Omit a section only if it would be empty.
Read `references/sections.md` before writing items 1–8: it says what each must
contain, and why. Item 9 is here in full.

1. **Epistemic header** (mandatory, first thing in the file): written from
   possibly-compacted context, file:line references are leads, and **any claim
   about what the user has or has not said must be checked against memory before
   it is acted on.**
2. **Mission: what this chain is for, and whether it moved.** **A thread that
   spent every token on discovered side work still writes "the mission did not
   move" here.**
3. **State**: **Name the checkout §0 resolved, then the branch and commit**;
   test counts measured, not estimated.
4. **Immediate next step**: **Say whether it serves the mission or the detour.**
5. **Standing instructions, as pointers, not content.**
6. **Credentials, paths, and gotchas**: never a secret's value.
7. **Open work: this thread's leftovers only, in priority order.**
8. **Corrected wrong beliefs**
9. **Escape hatch note**: state that this file is **moved into
   `<handover_dir>/archive/handovers/`** automatically once its grace period
   passes after the session that *attaches* it, and that it should be moved
   into a repo if it is worth keeping where people look. A handover nobody
   attaches is never retired at all, so the failure mode is clutter, not data
   loss. Say "archived", not "deleted": the sweep never deletes.

   **Say that the mission spec is NOT swept by this.** `claude-spec-archive`
   retires a spec only when its own front matter says `shipped` or
   `superseded`. That is what makes the backlog worth putting there, so state
   it rather than leaving a reader to assume the spec expires with this file.

   **Resolve the real numbers first; never write the defaults.** This note is
   the only thing that tells a human when their file moves, and the user may
   have changed the defaults in config:

   ```bash
   python3 - <<'PY'
   import os, sys
   TOOL_DIR = os.path.realpath("${CLAUDE_PLUGIN_ROOT}/tools/thread-handover")
   sys.path.insert(0, TOOL_DIR)
   import index_lib
   print("handover_dir:", index_lib.HANDOVER_DIR)
   print("sessions:", index_lib.GRACE_SESSIONS, "days:", index_lib.GRACE_DAYS)
   PY
   ```

   Write the values it prints, and say the day count is the one that
   decides in practice (several open windows spend the session count almost at
   once), so the note reads as a deadline in days.

### Never assert what the user has not said

An open question is a claim about the user's state of mind, and it is the least
verifiable thing in the document: you cannot read their other threads.

- **Banned:** "raised twice and never answered", "they still haven't
  confirmed", "follow up on this". Written as fact, with an imperative, this
  reads to the next thread as verified and gets acted on, and a question the
  user already answered elsewhere gets asked again.
- **Write instead:** "Open question, not answered *in this thread*. Check
  memory first; ask only if genuinely absent."

Bias toward checking, not chasing; `references/rationale.md` says why.

Model the tone and structure on `tools/thread-handover/reference-example.md`
in the toolkit checkout.

**Do not pad it.** A handover that restates the whole thread is as useless as
none. Include what changes what the next thread would do.

## 4. Register it in the index

After writing the file, run:

```bash
python3 - <<'PY'
import os, sys
from datetime import datetime

# Resolve the tool directory through the plugin root, so nothing here
# hardcodes where the toolkit was cloned.
TOOL_DIR = os.path.realpath("${CLAUDE_PLUGIN_ROOT}/tools/thread-handover")
sys.path.insert(0, TOOL_DIR)
import index_lib

print("handover_dir:", index_lib.HANDOVER_DIR)

path = "FULL_PATH_TO_THE_FILE_YOU_JUST_WROTE"   # substitute this one value
if not os.path.isfile(path):
    raise SystemExit("no such file: " + path)
if not index_lib.HANDOVER_NAME_RE.match(os.path.basename(path)):
    raise SystemExit("name must match YYYY-MM-DD-<slug>-handover.md: " + path)
if not os.path.realpath(path).startswith(
        os.path.realpath(index_lib.HANDOVER_DIR) + os.sep):
    # Registering outside the handover dir half-works, and that is worse than
    # failing: the file gets pointed at, but is_safe_to_retire rejects it
    # forever, so it and its index row survive every future sweep.
    raise SystemExit("must live in %s: %s" % (index_lib.HANDOVER_DIR, path))

index_lib.add_record(
    path,
    index_lib._config.workspace_root() or os.path.expanduser("~"),
    os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown",
    datetime.now().isoformat(timespec="seconds"),
)
print("registered:", path)
PY
```

Substitute the real path. Everything else resolves itself: the directory from
config, the toolkit location from the plugin root, and the session id (which
decides whether a session may consume its own handover) from the environment.

Registration is what makes the next session get *pointed at* the document and
cleaned up afterwards. **A handover you write but do not register is
invisible.**

### How the next session picks it up

Starting a session does not consume the handover; the user attaching it does,
and a consumed handover is archived (moved, never deleted) once its grace period
passes. `references/pickup.md` has the full lifecycle; read it if the user asks
how pickup or retirement works.

**Run every one of this skill's shell blocks in the main loop; never delegate
them to a subagent.** A subagent's cwd and `CLAUDE_CODE_SESSION_ID` both differ
from the thread's; `references/rationale.md` has what each breaks.

## 5. Report back

**Both shapes end the same way: a fenced block the user pastes into a new
thread.** It comes last and is never buried under the summary.

### When you wrote a document

Summarize what the document covers in two or three lines. Then end with **a
single copy-pasteable line, on its own, in a fenced block**; that line is the
handover:

```
Continue from /Users/you/Work/handovers/2026-08-14-billing-api--rate-limits-handover.md
```

Substitute the real absolute path. The line must contain the **full path**, as
plain text.

**Do not tell them to attach it with `@`**: a plain-text full path works in
every client and marks the handover consumed (`references/pickup.md` says why).

Recommend they start a **new** thread and paste that line. New matters:
SessionStart also fires on resume and compact, which keep the same session id,
and a session is never offered a handover it created itself.

### When §3 found no document was needed

Say so in one line, with the reason, then end with **a kickoff prompt in a
fenced block**, written so a fresh thread can start from it with no other
context. Read `references/kickoff-prompt.md` first: it has the five-line shape
with an example, and what each line is for. In short, the prompt carries the
mission slug and spec path, §0's scoped state, the first step (mission or
detour), standing-instruction pointers, and the shortened epistemic header. One
item per line, under ten lines; anything longer needed the document.

**Do not end the session yourself**; that is their call.

If they decline, record a snooze so the watch hook stays quiet until context
grows another 10% of the window:

```bash
python3 - <<'PY'
import glob, os, sys
TOOL_DIR = os.path.realpath("${CLAUDE_PLUGIN_ROOT}/tools/thread-handover")
sys.path.insert(0, TOOL_DIR)
import watch_lib

session = os.environ.get("CLAUDE_CODE_SESSION_ID")
if not session:
    raise SystemExit("no CLAUDE_CODE_SESSION_ID -- cannot snooze")
found = glob.glob(os.path.expanduser("~/.claude/projects/*/%s.jsonl" % session))
if not found:
    raise SystemExit("no transcript for session " + session)
ctx, _ = watch_lib.read_context(found[0])
if ctx <= 0:
    raise SystemExit("read 0 tokens -- refusing to record a snooze that suppresses nothing")

state = watch_lib.load_state(session)
state["snoozed_at"] = ctx
watch_lib.save_state(session, state)
print("snoozed at {:,} tokens for session {}".format(ctx, session))
PY
```
