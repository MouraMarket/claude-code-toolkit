# The section guidance

Read from the handover skill's §3, before writing sections 1–8 of a handover document.

Section numbers (§0–§5) are the handover skill's own `SKILL.md` sections.

## Items 1–8, in full

1. **Epistemic header** (mandatory, first thing in the file). State that this
   was written from possibly-compacted context, that file:line references are
   leads to confirm rather than facts to assert, and that **any claim about
   what the user has or has not said must be checked against memory before it
   is acted on.** This is the most valuable part of the document; do not drop
   it or soften it.
2. **Mission: what this chain is for, and whether it moved.** Three lines, and
   they are not a summary of the thread:
   - the mission, and the path to the spec holding its backlog;
   - whether **this thread** moved it: shipped it, advanced it, or did not
     touch it;
   - if `mission_status: abandoned`, why, and **who decided**. Never record
     that on your own judgement; a mission is abandoned by the user, not by a
     thread that ran out of road.

   **A thread that spent every token on discovered side work still writes "the
   mission did not move" here.** Describing a productive detour reads as
   progress, and the next thread has no way to tell the difference.
3. **State**: what is done, merged, and live. **Name the checkout §0 resolved,
   then the branch and commit**; the checkout is what lets the next thread tell
   your branch from others open in the same tree, and stating it makes the rest
   of this section falsifiable. Test counts, measured not estimated. If you did
   not run the tests in this thread, say so rather than quoting a remembered
   number.
4. **Immediate next step**: the single thing the next thread should do first,
   and what it is blocked on. **Say whether it serves the mission or the
   detour.** Both are legitimate answers; leaving it unsaid is not. A chain
   that never returns to the mission gets there one defensible next step at a
   time. If the answer is the detour, say what it buys and what has to be true
   before the mission resumes.
5. **Standing instructions, as pointers, not content.** These were written to
   memory in §2. List them in one line each so the next thread knows they
   exist, and name the memory that holds each. Do not restate the reasoning.
6. **Credentials, paths, and gotchas**: the non-obvious operational details.
   Never a secret's value; say where it lives.
7. **Open work: this thread's leftovers only, in priority order.** The
   mission's backlog is in the mission spec; link it, do not copy it, and do
   not re-rank it here. What belongs here is what *this* thread left
   unfinished and what it inherited and deliberately dropped, the latter with
   its reason.
8. **Corrected wrong beliefs**: anything asserted in this thread that turned
   out to be false, and how it was caught. If it is a fact about a repo or the
   harness rather than about this thread, put it in that repo's `CLAUDE.md`
   and link it from here.
