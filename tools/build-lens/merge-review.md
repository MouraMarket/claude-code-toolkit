### Resolve the mode first

Run `claude-build-route --review-mode`. Under `economical`, Steps 1–2 below are
what you run. Under `full-power`, skip both and dispatch `claude-codex-review`
instead:

| Gate | wrapper invocation |
|---|---|
| Code Review | `claude-codex-review --repo <branch worktree> --base <default branch> --plan <plan file> -o <report path>` |
| Merge Review | `claude-codex-review --repo <branch worktree> --base <default branch> --merge-with <default branch> -o <report path>` |

**`-o` is REQUIRED, same as `--base` and `--repo`.** Use a report path distinct
from Code Review's.

**`claude-merge-brief` is not used on this path at all.** The wrapper's own
`git diff` derives the review surface mechanically, which is stronger blinding
than a built brief. Under `--merge-with`, the
wrapper sends two labelled stdin blocks, the branch diff and what moved under
you since, where Code Review sends the branch diff with the plan. That is what makes
this a different instrument from Code Review, not the diff itself — a
three-dot diff is byte-identical before and after a clean merge.

If the wrapper exits 4, Codex is unavailable: follow "Exit 4" in
`code-review.md` in this directory.

**A repo with no `origin` remote** (`git remote` prints nothing): in `/build`'s
G5 step, skip the fetch and merge `<base>` instead of `origin/<base>`. Code
Review (both modes) and economical Merge Review review against the local base
branch there, and so does full-power Merge Review: `--merge-with <base>` merges
the local branch and fetches nothing. A configured `origin` that fails to fetch
is a real failure, not this case.

### Economical mode

### Step 1 — build the brief by script

```bash
brief="$(git rev-parse --path-format=absolute --git-common-dir)/build/merge-brief.md"
mkdir -p "$(dirname "$brief")"
claude-merge-brief --pre-pr -o "$brief"        # before the PR exists
# claude-merge-brief --pr <N> -o "$brief"      # PR already open
test -s "$brief" && echo "$brief"
```

**Scratch lives inside the repo, never in `/tmp`** — worktrees under
`<repo>/.claude/worktrees/`, files under the git dir as above — so the path
does not depend on the platform's temporary directory.

**Do not assemble this by hand.** A hand-built brief leaks your rationale through
*selection* — what you chose to paste is an argument, and the reviewer reads it
as one. The script has no opinion: merge ref, changed files, commit messages,
the diff and the suite, and nothing else. There is no PR body: it is author
framing the reviewer cannot otherwise reach, and under `/build` it names the
plan and design. Its diff modes (`--pr`, `--pre-pr`) have no flag by which a
plan or a spec can enter, and they must never get one. Its `--plan` flag is a separate
mode that builds a Plan Review brief with no diff in it, so it cannot add a plan
to a merge brief.

It also executes the three measured traps in the merge-ref fetch so you do not
have to remember them — chiefly that GitHub computes merge refs **lazily**, so a
`+` fetch can exit 0 and hand you a merge that will never happen. The script
fails closed on that. One trap it cannot absorb: **once a PR closes,
`refs/pull/<N>/merge` stops existing**, and the fetch fails `couldn't find remote
ref`, which reads like a broken command and means a merged PR. Review what landed
instead: `git diff <squash-commit>^ <squash-commit>`.

### Step 2 — one blinded reviewer, with execution as a returned field

The script is **`blinded-review.js`**, in the same directory as this file:

```
Workflow({ scriptPath: "<absolute path of blinded-review.js in the same directory as this file>",
           args: { gate: "merge", briefPath: "<the path Step 1 printed>",
                   lensDir: "<absolute path of the directory holding this file>" } })
```

**If `Workflow` is not in your tools**, dispatch the same review through the
Agent tool. Use `Workflow` when it is in your tools; when it is not, or the
call is refused or fails before a reviewer runs, use this instead; never skip
the gate, and never review the change yourself in its place. `Workflow` is off
by default on Pro plans, and the `enableWorkflows` setting,
`CLAUDE_CODE_WORKFLOWS=false`, `CLAUDE_CODE_DISABLE_WORKFLOWS=1`, a server
flag or an org policy can remove it. It also refuses a `scriptPath` outside
your working directories, which an installed plugin usually is: that refusal
is this case. Its error suggests passing the script inline through `script`;
never pass the script inline, because a script you retyped is not the one
this gate's tests run. Under `Workflow` the report is the call's return
value; write no report file. Under the Agent tool the reviewer writes the
report beside the brief, at
`<directory of the brief>/merge-report-<attempt>.json`, with `<attempt>`
starting at 1 and incremented for every retry; a report path is never reused
or deleted.

```bash
claude-review-gate prompt --gate merge --brief <absolute brief path> --lens-dir <absolute path of the directory holding this file> --report <report path>
```

```
Agent({ subagent_type: "toolkit-build:code-reviewer", model: "opus", effort: "max",
         description: "merge review", prompt: "<everything the command printed>" })
```

Wait for the reviewer to finish before `check`: pass `run_in_background: false` where your
Agent tool accepts it, and otherwise wait for its completion notice. A report that is still
missing while the reviewer runs is not a failed attempt; never dispatch a second reviewer
for it.

```bash
claude-review-gate check --report <report path>
```

Exit 0 prints an `accepted:` line for the build ledger. Exit 1 or 2 means the
gate did not pass: fix the cause and dispatch again with the next attempt
number, and never read the report yourself as a substitute for `check`. If
the Agent tool takes no `effort`, every gate still runs at `max` from its
agent's own settings: code-reviewer at code and merge, spec-reviewer at
design and plan. `"enableWorkflows": true` in your Claude Code user settings
turns `Workflow` on, if you choose to.

Three args: `gate: "merge"`, `briefPath`, the path Step 1 printed, and `lensDir`. Handing the reviewer
the **file** is what keeps it blinded — its own description will accept a bare
PR number and then read the PR body, which defeats the mechanism. The returned
report requires `recommendation`, `executed` and `findings`; `executed.ran` is a
field precisely so "nothing was executed" is a result you have to read rather
than a silence.

**Budget several minutes for the suite, and say so when you dispatch.** On a
freshly created worktree `./install.sh --check` runs every suite in the repo and
takes minutes, not seconds. A reviewer that has not been told this can read a
long silence as a hang and stall at the moment it goes to run the suite,
returning nothing at all, which costs the whole pass rather than degrading it. Do not run a second suite in parallel with a
reviewer's either; you are competing for one machine.

**Read `executed` before you read `findings`.** A clean review from a reviewer
that ran nothing is not a clean review — it is an unread one, and the two look
identical in the prose. That is the same failure the `unread` counter exists to
catch at Plan Review.

### Why the fix-round cap is what it is

**The cap itself is stated in `skills/builder/build/SKILL.md`, under `### Fix rounds — ONE
ROUND, THEN SHIP`, and that is the copy to act on.** It is one Merge Review
pass: fix its findings, mutation-test each fix, and ship. There is no second
Merge Review pass. What follows is only
the reasoning behind it — read it if you are tempted to re-litigate the cap, not
to find out what the cap is.

Why the old jurisdiction-plus-evidence STOP rule failed: fixes carry their own
defect rate, and the artifact *grows* each round rather than the fix surface
shrinking.

A round cap was rejected once on the grounds that capping findings is a
false-negative generator. That is still true and this is not that: the cap is on
*passes*, not on findings within the pass. The one pass reports everything it
finds.


**Mutation-testing the fixes is the load-bearing half, not a formality.** A
fix's own test is the thing in this codebase most likely to be vacuous, and a
review round routinely finds one. Break each fix on purpose and
confirm its test goes red. That is what replaces the second Merge Review pass
the cap forbids, and it costs approximately nothing.

**Where a second opinion is cheap, take it:** `/code-review` at low/medium on the
fix diff costs little and can catch a fix that silently *dropped* coverage. It forks with the author's context, so it is a smell-test
rather than a blinded pass — never a substitute for round one, and it does not
reopen the loop. Read it, fix what is real, ship regardless.


**The known limit, stated rather than papered over:** "did the reviewer execute
this" is a claim the reviewer makes about itself, and one that overstates what it
ran promotes its own advisory findings to blocking. The mitigation is that
`commands` names what was run, so the claim is checkable in one command — and
this repo's rule is already that a green result is not evidence until you have
seen it fail. **Spot-check the named command on any finding that decides a
merge.**

**Where there is no suite, say so in `why_not`** — and do the hand
falsification instead: resolve the paths, run the commands the diff tells a
reader to run, read the file the prose asserts something about. Those are
commands; list them with `ran: true`. `ran: false` is for a review that
could run nothing at all, and with no findings the wrapper rejects it.
That hand check is stronger than a test suite on a docs diff: it finds
BLOCKING defects on diffs with no code in them.
