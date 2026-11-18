# The kickoff prompt

Read from the handover skill's §5, before writing the kickoff prompt, when §3 found no document was needed.

Section numbers (§0–§5) are the handover skill's own `SKILL.md` sections.

## Its shape, with an example

Say so in one line, with the reason: *"No handover document — everything
outstanding is in `<spec>`."* Then end with **a kickoff prompt in a fenced
block**, written so a fresh thread can start from it with no other context:

```
Mission: reporting-pipeline-v2. Spec: /Users/you/workspace/specs/2026-03-02-reporting-pipeline-v2.md — read its MVP, Backlog and Waiting on a human sections first.
Last thread: repos/reporting, branch nightly-cron at a1b2c3d, merged as PR #41. Tree clean.
Start with: Backlog row 6 (backfill the March gap). It serves the mission.
Standing instructions in memory: backfill-before-append.
Written from possibly-compacted context: confirm any file, branch or PR claim before acting on it, and check memory before asserting what the user has or has not said.
```

## What each line is for

What each line is for, and the rules that keep the prompt honest:

- **The mission line names the slug verbatim**, plus the spec's absolute path.
  With no document attached, §1 takes the mission from the next thread's first
  substantive prompt; this line *is* that prompt, so the slug carries forward
  instead of being re-derived. Omit it only if the thread had no mission.
- **The state line is §0's facts** (checkout, branch, short commit, PR) and is
  subject to §0's scoping rules. With a fallback `source`, write what you did and
  where, not a branch name.
- **The first-step line says whether it serves the mission or the detour**, as
  §3's *Immediate next step* requires.
- **Standing instructions are pointers**: memory names, never their content.
  Drop the line if there are none.
- **The last line is the epistemic header**, shortened, and is never dropped.
  §3's *Never assert what the user has not said* applies to every line here too.
- **One item per line, and never wrap a sentence across lines**: the user
  pastes this verbatim. Keep it under ten lines; anything longer needed the
  document.

## Why a new thread

Recommend they start a **new** thread and paste it. New matters here for a
different reason: the point is a fresh context, and pasting it back into this
thread buys nothing.
