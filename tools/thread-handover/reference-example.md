---
mission: reporting-pipeline-v2
mission_spec: /Users/you/specs/2026-03-02-reporting-pipeline-v2.md
mission_status: live
---

# Reporting Pipeline V2 — thread handover (2026-03-14)

> **Illustrative example.** Invented project, invented numbers. It exists to show
> the *shape* of a good handover — the mission declared up front, the epistemic
> header, measured claims, a single unambiguous next step, and corrected beliefs
> carried forward instead of quietly dropped.
>
> The filename this would be saved under is
> `2026-03-14-reporting-pipeline-v2--nightly-cron-handover.md`: mission, `--`,
> then the leg this thread actually did.

**Written from possibly-compacted context.** Everything below is a lead to
confirm, not a fact to assert. Re-read any file before making a claim about it; do
not quote a line number from this document without opening the file first. Test
counts were measured in this thread, not remembered — but re-run them anyway,
since the code was still being edited late in the session.

**Anything here about what the user has or has not said is the weakest claim in
the document** — this thread cannot see their other threads. Check `memory/` and
`MEMORY.md` before acting on it, and especially before asking them again.

## Mission

**Reporting Pipeline V2** — backlog in
`/Users/you/specs/2026-03-02-reporting-pipeline-v2.md`. Still `live`.

**This thread advanced it but did not finish it.** Extract, normalize and load
are live and the nightly cron runs; the 2023 backfill is what remains, and it is
the next step below.

Two things this thread discovered went to the spec's Backlog rather than here —
the null-`region` rows in the legacy source, and the timezone handling in
`normalize.py`. Three more were migrated there out of the handover I attached.
All five are real work, none of them is Pipeline V2's finish line, and a document
that gets archived in a week is the wrong place for any of them to wait.

## State: what is done and live

Branch `feat/pipeline-v2`, merged as PR #41. Commit `a3f9c21` on `main`.

- `pipeline/extract.py` — pulls raw rows, retries on 429 with backoff. Live.
- `pipeline/normalize.py` — the schema mapping. Live, but see the timezone gotcha.
- `pipeline/load.py` — upserts into Postgres. Live.
- Nightly cron at 03:00 UTC. Two clean runs so far.

**Tests: 214 passing** (`python3 -m pytest`, 11s). Measured this session, twice —
once before the last commit and once after.

### First live run — measured, not estimated

| | Expected | Actual |
|---|---|---|
| Rows extracted | ~40,000 | 38,412 |
| Rows rejected | <1% | 0.4% (154) |
| Wall clock | <10 min | 6m 22s |

The 154 rejections are all one shape: a null `region` on rows older than 2024.
Not a bug in this pipeline — the source has been null there since before the
migration. Left alone deliberately; it is in the mission spec's Backlog.

## Immediate next step

**Backfill 2023 into staging and diff row counts against the legacy table.**
Command and expected output are in `docs/backfill.md`. Nothing else should be
trusted until that diff comes back clean — the normalizer has only ever been run
against post-2024 data.

Not blocked on anything.

## Standing instructions — written to memory, listed here as pointers

All three were written to memory during this thread. The memory is the source of
truth; these lines exist only so the next thread knows to look.

- Never run the backfill against production — `[[pipeline-backfill-safety]]`
- Fix the source rather than adding a normalizer special-case — `[[pipeline-normalizer-policy]]`
- Report row counts as measured numbers, never approximations — `[[comms-style-brevity]]`

## Open questions

Not answered *in this thread*. Check `memory/` and `MEMORY.md` before raising any
of these — the user may well have answered them elsewhere.

- Whether the 2023 rejections should be backfilled at all, or left as a known gap.

## Paths and gotchas

- Credentials come from the environment or a secrets manager at runtime. **Never
  paste a literal token into the repo** — if you find one, that is the bug.
- `normalize.py` assumes input timestamps are UTC. They are *not* — the source
  emits local time with no offset. Currently corrected at the call site, which is
  the wrong layer. Fixing it properly is in the mission spec's Backlog.
- The cron writes to `/var/log/pipeline/`, which is **not** rotated. It grew 400MB
  in three days during testing.
- `make test` and `pytest` are not equivalent — `make test` skips the integration
  suite. Use `pytest` when you need the real answer.

## Open work — this thread's leftovers only

**The mission backlog is in `/Users/you/specs/2026-03-02-reporting-pipeline-v2.md`.**
Five items live there and are deliberately not repeated here: the timezone fix,
the null-`region` decision, log rotation, alerting on the nightly reconciliation
job (inherited from the previous handover, untouched this thread, and not less
true for that), and the 5xx retry defer. Copying them into this file would create
a second backlog, and two backlogs disagree.

What is left below is only what *this* thread failed to close:

1. **Uncommitted: the `--dry-run` flag on `load.py`.** Written, works by hand, no
   test. Finish the test or revert it — do not merge it as it stands.
2. **Dropped, deliberately:** the plan to shard the staging table. The row count
   came in an order of magnitude under the estimate, so the problem it solved does
   not exist. Recorded rather than dropped silently — a vanished item reads as
   done to the next thread.

## Corrected wrong beliefs

Carried forward so the next thread does not re-derive them the expensive way.

- **"The source timestamps are UTC."** False, and it cost about two hours. They
  are local with no offset, so everything looked correct until a run crossed a DST
  boundary and rows landed an hour apart.
- **"`make test` runs everything."** False. It skips integration tests, so a
  green `make test` hid a real failure for most of the session.
- **"Rejected rows are logged."** They are counted, not logged. The 154 rejections
  were only identifiable by re-querying the source afterwards.

## Escape hatch

**This file is moved into `~/Work/handovers/archive/handovers/` automatically
3 days after the session that attaches it.** If any of it is worth keeping where people look,
move it into the repo or fold it into a spec before then. A handover nobody
attaches is never retired at all, so the failure mode is clutter, not lost work.

<!-- Note for anyone writing one of these, not part of the note itself: the "3"
above is a RESOLVED value, read from index_lib.GRACE_DAYS on the machine that
wrote it (the escape-hatch item in skills/continuity/handover/SKILL.md prints
it). Do not copy the number from this example, and do not write the shipped
default: sweep_grace_days is configurable, and this note is the only thing that
tells a human when the file moves. Resolve the DIRECTORY the same way: it is
`<thread_handover.handover_dir>/archive/handovers/`, and the path above is one
machine's value. Say ARCHIVED, not deleted. There is a second gate (other
sessions must have started) but it is spent almost at once when several windows
are open, so a note that presents two conditions makes a reader reason about a
deadline instead of reading it. -->

