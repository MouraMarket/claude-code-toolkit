# Mission spec

Read from the handover skill's §2. When the mission has no spec, or its spec lacks a `## MVP` or a `## Waiting on a human` section.

Discovered work outlives the thread that found it, so it takes the same test as
every row above, and it fails the handover on the same grounds: this document
is scratch.

**That flip takes the same test as §1's `mission_status`: `## Waiting on a
human` must exist and carry no outstanding row.** This is the key
`claude-spec-archive` actually reads. Flip it with rows still open and the file
is archived once it clears the age floor, every later run skips it, and
`rg "^status: live" <specs_dir>/*.md` can never surface those rows again.

**If the mission has no spec, write one now.** Either trigger is enough:

1. **This is the mission's second handover.** Work spanning two threads is not
   the single-sitting work the spec-first rule exempts, whatever it looked like
   at the start.
2. **The build produced work someone must do later**: a console or ops step, a
   deferred decision, or a second PR. A project that ships all its code in one
   round never gets a second handover, so trigger 1 never fires, and without
   this trigger its leftovers would have nowhere to live.

Its front matter is `status: live` and `subject: <mission>`, the slug, never
the title. A file containing nothing but a mission statement, a backlog and a
`## Waiting on a human` section is a legitimate spec; write the thin version
rather than none.

**On a multi-session build, that spec also needs a `## MVP` directly above its
`## Backlog`**: 3–5 lines saying the smallest version that delivers the end
goal, plus what is explicitly out. Every later session re-checks its backlog
against it, so a spec without one leaves that check reading a section that does
not exist. Single-sitting work needs neither section.

**Trigger 2 is about the mission, not this thread.** If this thread discovered
human work belonging to a *different* mission that has no spec, that mission
needs one too. Write it, and point this handover's Open work at it.

**A copy committed to a feature branch is not the mission spec**: it is
invisible from `main` and dies with the branch. The canonical file is in
`<specs_dir>`.

### Waiting on a human: the section the spec must have

**Every mission spec gets a `## Waiting on a human` section, and it is not part
of the backlog.** It holds the work no PR can contain and no check can watch: a
DNS change, an OAuth redirect URI, provisioning an account, deleting a service,
rotating a credential, a decision only the user can make.

One row each, with four columns: **the step, who owns it, what silently breaks
if it is skipped, and its status.** Order the rows when the order matters, and
say that it does (a DNS change made before its OAuth redirect URI can lock
everyone out, for example).

**The status column is what makes the section checkable, so do not drop it.**
Rows are marked done *in place*, never deleted: the file is the only record
that the step existed, and a deleted row cannot be told from a step nobody
wrote. So the gate cannot read "empty" as "no rows":

> **The section passes when every row is marked done, or explicitly decided as
> no-action. It fails while any row is outstanding, or unmarked, which is not
> the same as done.**

A section with no status column cannot pass at all.

Why it is separate from the backlog:

- **Every row fails silently by construction.** No PR contains it, no CI checks
  it, no deploy guard covers it. A backlog item can at least be discovered by
  someone reading code; these cannot.
- **A plan's ops appendix is not a home for it.** A plan describes how to build
  and goes quiet the moment it is finished. This section is in the file that
  gets read every thread.
- **It keeps the mission honest.** Neither `mission_status: shipped` nor the
  spec's own `status: shipped` may be written while a row here is outstanding.

**Rows leave this section only when the human confirms they are done**, never
because a thread inferred it from a merged PR or a green check. If you cannot
tell, the row stays and you say you could not tell.

## Front matter and claude-spec-archive

This front matter cannot collide with `claude-spec-archive`. That tool
classifies any handover-named file as unmanaged **before** it reads front
matter, so these keys are inert to it, which is also why the key is
`mission_status` rather than `status`.
