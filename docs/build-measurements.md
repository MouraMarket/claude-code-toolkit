# How `/build`'s gates are measured

**This file holds methods, not measurements.** It says how to find out what a
`/build` gate costs, what it caught and what it missed, on your own machine and
plan. It carries no figures from anyone's setup: token counts, window shares,
quota rates and yields differ by plan, model, repository and the size of what
you review, so a number copied from someone else is not an estimate. Measure,
then re-measure before you quote one.

`/build` reads this file through `tools/build-lens/build-measurements.md`.

## The basis

Two rules govern every figure you produce with the methods below.

- **Cost means output tokens against your usage windows**, not dollars. On a
  subscription a gate is not invoiced; it depletes the 5-hour and 7-day
  windows, and running out mid-build is the real failure. Dollars are an
  invoice only for work billed to an API key; elsewhere they are a
  model-comparison proxy.
- **Say which numbers are measured and which are inferred**, every time. An
  extrapolation is labelled as one. A plausible figure nobody measured is the
  kind that turns out wrong by a multiple.

Quote a range with its basis, never a bare point.

## Measuring what a build or a gate spent

Run `claude-build-cost <session transcript.jsonl>` (`tools/build-cost/`; `/build`
runs it at Phase 5). The `claude-build-cost-record` hook also records every
qualifying session at `SessionEnd`, and `claude-build-cost --ledger` shows the
record.

What it does, so you can check it rather than re-derive it by hand:

- **Reads every subagent transcript** under the session's `subagents/`
  directory, including workflow agents and agents written under a worktree's
  project directory.
- **Dedupes on `message.id`.** Claude Code writes one entry per content block
  and repeats that message's `usage` on each, so a per-entry sum inflates a
  transcript several times over. It merges a message's entries by per-field
  maximum, the one rule correct for every shape the entries come in.
- **Classifies lanes from each agent's `agentType`** (`main`, `executor`,
  `review`, `other`), never from the file path, which cannot tell a task
  reviewer from an executor.
- **Prices and weights by model version** from Anthropic's list prices, dated
  in `cost_lib.PRICES_AS_OF`. A version newer than the table, or a message run
  off list price (fast mode, a regional endpoint, batch or priority tier), is
  NOT PRICED and NOT WEIGHTED: named, never counted as free. The opus-equivalent
  column weighs output by each model's output rate against Claude Opus 5.5's, a
  price-derived proxy for window draw, not a published ratio.

**Do not quote the `subagent_tokens` field a workflow returns.** It counts
input, cache and output together, and its ratio to output can vary from
run to run, so measure your own and do not expect one correction factor to hold.

**Reading the ledger.**

- A row is a **session**, not a build. A long session spans several builds,
  and a resumed session appends a superseding row; `--ledger` collapses rows
  per session.
- The `kind` column is a **shape**: `build` (an executor ran with review
  machinery), `adhoc` (an executor ran without), `gate` (review only), `none`
  (recorded by hand, nothing qualifying). Nothing on disk records that
  `/build` was typed, so `kind` errs in both directions: a `/build` with every
  gate declined reads `adhoc`, and any session that runs a workflow and
  dispatches executors reads `build`. Read it as a strong hint.
- **Read a share per `kind`**, never over the whole file, and decide
  affordability on the **absolute** figure against a window.

**Per-build accounting is out of scope.** Attributing spend to one build would
need the plan's identity in the record, and the `SessionEnd` payload does not
carry it. An instruction to pass a plan path by hand is a hope, not a
mechanism. Re-open this only with something that records plan identity without
anyone remembering to type it.

## Turning tokens into a share of a usage window

A percentage needs a ratio: output tokens per percentage point of a window, for
**your** seat. Take it from your own usage reading (the account usage panel, or
`claude-usage-anchor` when usage-watch is installed), against tokens counted
over the same stretch. Four limits ride with every share you quote:

- **The ratio is per seat.** Plans differ, so never rescale someone else's.
  The level re-anchors each window; a ratio measured on your machine carries
  across windows until the token counting changes, and then you re-derive it.
- **It counts one machine.** claude.ai and your other devices spend the same
  window, so a share computed here is a floor.
- **Cost tracks the reading surface you hand a reviewer**, not the number of
  reviewers or which gate it is. A brief naming many source files lands high,
  a short plan low.
- **Quote the range**: the low end from your anchored ratio, the high end from
  the most conservative ratio you have.

Say the quote before you dispatch a gate — one line, e.g. "Plan Review, about
N–M output tokens, roughly X–Y% of a five-hour window" — from your own recent
runs of that gate. A gate whose cost the user learns afterwards is one they did
not get to decline.

## Estimating a gate before you run it

- **Estimate from your own runs of the same gate on a similar brief.** A figure
  carried across from a different plan is not an estimate: cost scales with what
  the reviewer reads.
- **A re-round costs more than the round before it**, not less. A fix pass
  grows the artifact, and the reviewer reads all of it again. Budget a re-round
  at least at the first round's cost.
- **Scoping the brief shrinks the brief, not the bill**, when the reviewer still
  reads the repository and runs the suite. The saving comes from whether a gate
  runs and how many times, not from making one pass cheaper.

## An external reviewer's quota

A reviewer that bills a separate quota (Codex, for example) has its own window,
and its meter can be coarse. If `used_percent` is an integer, one run's delta is
quantisation noise; estimate per-gate cost from the run's own token counts
instead, against a rate fitted per window.

**Fitting the rate.** Group every rollout by window, keyed on
`(plan_type, window_minutes, resets_at)` with `resets_at` rounded up to the
hour (it jitters by seconds). For each window, divide the peak `used_percent` by
the sessions' total uncached input tokens (each file's last cumulative
`input_tokens − cached_input_tokens`):

```bash
python3 - <<'PY'
import json, os, glob, math, statistics, collections, time
groups = collections.defaultdict(list)
for path in glob.glob(os.path.expanduser("~/.codex/sessions/**/rollout-*.jsonl"), recursive=True):
    models, usage, rl = set(), None, None
    for line in open(path):
        try: o = json.loads(line)
        except Exception: continue
        p = o.get("payload") or {}
        if o.get("type") == "turn_context" and p.get("model"): models.add(p["model"])
        if o.get("type") == "event_msg" and p.get("type") == "token_count":
            usage = (p.get("info") or {}).get("total_token_usage") or usage
            rl = p.get("rate_limits") or rl
    pr = (rl or {}).get("primary")
    if not usage or not isinstance(pr, dict) or not isinstance(pr.get("resets_at"), (int, float)): continue
    key = (rl.get("plan_type"), pr.get("window_minutes"), math.ceil(pr["resets_at"] / 3600) * 3600)
    groups[key].append((models, usage["input_tokens"] - usage.get("cached_input_tokens", 0), pr.get("used_percent") or 0))
for key in sorted(groups, key=lambda k: k[2]):
    rows = groups[key]; tokens = sum(r[1] for r in rows); used = max(r[2] for r in rows)
    ppm = used / (tokens / 1e6) if tokens else 0
    per = sorted(r[1] * ppm / 1e6 for r in rows)
    models = collections.Counter(m for r in rows for m in r[0])
    print(key[0], key[1], time.strftime("%m-%d %H:%M", time.localtime(key[2])), f"n={len(rows)} used={used} "
          f"ppm={ppm:.2f} median={statistics.median(per):.2f} p90={per[int(0.9 * (len(per) - 1))]:.2f} models={dict(models)}")
PY
```

**Check it before you trust it:** pick one window you have worked out by hand
and confirm the script reproduces your figure. If it does not, the log format
has drifted and no number from the run is usable. Then:

- **Fit only a window with one model.** No split of a mixed window's points is
  defensible.
- **Fit only a window with real spend.** At a `used` of a few points the
  integer meter makes the rate mostly noise.
- **Add a new key for a new plan or model, never edit a neighbour.** Rates do
  not carry across keys.

A meter that reads 100% does not verifiably mean a gate cannot run: the snapshot
keeps its old value until the reviewer next runs, and a window can be replaced
early.

## What a gate caught, and what it missed

**Catches.** When a gate finds a defect no mechanical check could have
expressed, write it down with the run. The question worth asking before you
decline a gate is not "is the code risky" but "does this change assert
anything a suite cannot check": a number in a document, or a sentence about what
a machine does, is graded by nothing except a reviewer.

**Catches alone say nothing about recall.** A gate that caught a few things and
missed ten times as many produces the same list of catches. Recall needs misses written
down as reliably as catches.

### The miss ledger

Write a `Miss:` line in that build's ledger sidecar the moment a defect surfaces
that a gate should have caught — mid-build or after merge. The format is in
Phase 3 of `skills/builder/build/SKILL.md`. Read recall back with
`rg '^Miss:' <specs_dir>`, which includes the archive (`<specs_dir>` is what
`python3 <toolkit checkout>/lib/toolkit_config.py specs-dir` prints, from any
directory).

Nothing can be built to fill it. It needs a miss noticed and written when
someone says "the gate should have caught this" — not later. If you find
yourself saying it, that is the entry.

## Measurement pitfalls

Each of these can produce a confident wrong number.

- **Equal cardinality is not set identity.** Two counts that happen to be equal
  are not necessarily the same things; compute the intersection.
- **A zero from a grep is a claim about the grep.** A payload stored as a JSON
  string inside a JSONL record has its keys escaped, so `"key":` finds nothing
  where `\"key\":` finds every one.
- **Rank on confirmed findings, never raw.** Raw yield rewards whatever files
  the most claims; refutation is what makes a finding count.
- **Zero confirmed findings is byte-identical to a clean gate.** A cheaper or
  blinder reviewer that confirms nothing looks exactly like a pass, so compare
  arms on the same artifact with the same judge before calling them equal.
- **A duration nobody measured is still a factual claim.** Check the
  timestamps rather than estimating from memory.
- **A decision written in a doc is not a configuration change.** To know whether
  a workflow still runs, read its `on:` block; an Actions `state` of `active` is
  only the enable switch.
- **Measure a heuristic against the real corpus, not its fixtures.** A fixture
  shows the failure you predicted; the corpus shows the one you did not (see the
  harness below).
- **Mark each premise of a plan as verified or inferred** in the plan text. An
  inferred premise is not a defect; an inferred premise that reads like a
  measured one is.

## The `DUPE_OVERLAP` corpus harness

**Run this before introducing any cross-reviewer dedupe key**, and read the
merged groups rather than only the rate: what matters is whether the things it
absorbed were the same claim. The corpus is every findings payload your review
agents have written, in the workflow subagent transcripts. It can show that a
key ANDed on free-text `where` fields never fires at all, the opposite of the
over-merging fixtures might predict.

```bash
python3 - <<'PY'
import json, pathlib, re
from itertools import combinations
from collections import defaultdict
runs = defaultdict(list)
for j in (pathlib.Path.home() / ".claude/projects").glob("*/*/subagents/workflows/**/agent-*.jsonl"):
    for line in j.read_text(errors="replace").splitlines():
        if '"findings"' not in line: continue
        try: rec = json.loads(line)
        except Exception: continue
        for b in ((rec.get("message") or {}).get("content") or []):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                for f in ((b.get("input") or {}).get("findings") or []):
                    # `where` AND `defect` is what separates a review finding
                    # from the other workflows that also emit a `findings` array.
                    if isinstance(f, dict) and "where" in f and "defect" in f:
                        runs[j.parent.name].append(dict(f, agent=j.stem))

STOP = set("""a an the and or but if then than that this these those is are was were be been
being it its of to in on at by for with from as into over under about not no nor do does did
doing what when where which who whom how why can could should would will shall may might must
there their they them we you your our his her one two some any all each every have has had
having only just also very more most other another same such both either neither""".split())
def toks(f):
    ws = re.sub(r"[^a-z0-9 ]+", " ", str(f.get("defect") or "").lower()).split()
    return {w for w in ws if len(w) > 2 and w not in STOP}
def jac(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0

DUPE_OVERLAP = 0.45     # <- the value under test
pairs = same_where = 0
merged_groups = absorbed = 0
for fs in runs.values():
    for a, b in combinations(fs, 2):
        if a["agent"] == b["agent"]: continue   # dedupe targets CROSS-reviewer repeats
        pairs += 1
        wa = re.sub(r"[^a-z0-9:._/-]+", " ", str(a.get("where") or "").lower()).strip()
        wb = re.sub(r"[^a-z0-9:._/-]+", " ", str(b.get("where") or "").lower()).strip()
        same_where += (wa == wb)
    groups = []
    for f in fs:
        hit = next((g for g in groups
                    if f["agent"] not in g["agents"] and jac(toks(f), toks(g["head"])) >= DUPE_OVERLAP), None)
        if hit: hit["agents"].append(f["agent"]); hit["n"] += 1
        else: groups.append({"head": f, "agents": [f["agent"]], "n": 1})
    for g in groups:
        if g["n"] > 1: merged_groups += 1; absorbed += g["n"] - 1

total = sum(len(v) for v in runs.values())
if not pairs:
    raise SystemExit("no cross-reviewer pairs found: nothing to measure")
print(f"{total} findings across {len(runs)} runs, {pairs} cross-reviewer pairs")
print(f"identical `where`: {same_where} pairs ({100*same_where/pairs:.2f}%) "
      f"— an ANDed where-key can never beat this ceiling")
print(f"at DUPE_OVERLAP={DUPE_OVERLAP}: {merged_groups} merge groups, "
      f"{absorbed} findings absorbed ({100*absorbed/total:.1f}%)")
PY
```

## Why the PR body carries the plan path

The plan is the contract a build is made against, and it lives in a specs
directory that no history records. The PR body is written once and kept, so it
is the only place the association between a plan and the change that shipped it
can live. Matching plans to PRs by filename instead is a guess, and it can be
wrong in both directions in one pass: calling another session's live plan
shipped, and missing one that did ship. To check how often your own plans are
named, count the specs in `<specs_dir>` that appear in any merged PR body.
