### Resolve the mode first

Run `claude-build-route --review-mode`.

Under **`economical`**, dispatch one blinded reviewer, handed the plan as its contract:

```bash
claude-merge-brief --pre-pr -o <brief path>
test -s <brief path> && test -s <plan file>
```

```
Workflow({ scriptPath: "<absolute path of blinded-review.js in the same directory as this file>",
           args: { gate: "code", briefPath: "<absolute brief path>", planPath: "<absolute plan path>",
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
`<directory of the brief>/code-report-<attempt>.json`, with `<attempt>`
starting at 1 and incremented for every retry; a report path is never reused
or deleted.

```bash
claude-review-gate prompt --gate code --brief <absolute brief path> --lens-dir <absolute path of the directory holding this file> --plan <absolute plan path> --report <report path>
```

```
Agent({ subagent_type: "toolkit-build:code-reviewer", model: "opus", effort: "max",
         description: "code review", prompt: "<everything the command printed>" })
```

```bash
claude-review-gate check --report <report path>
```

Exit 0 prints an `accepted:` line for the build ledger. Exit 1 or 2 means the
gate did not pass: fix the cause and dispatch again with the next attempt
number, and never read the report yourself as a substitute for `check`. If
the Agent tool takes no `effort`, the code and merge gates still run at
`max` from the code-reviewer agent's own settings, but the design and plan
gates do not: record that as a `Ruling:` line. `"enableWorkflows": true` in
your Claude Code user settings turns `Workflow` on, if you choose to.

The reviewer applies every lens in `### Code Review lenses` below. Under **`full-power`**, dispatch `claude-codex-review` instead:

| Gate | wrapper invocation |
|---|---|
| Code Review | `claude-codex-review --repo <branch worktree> --base <default branch> --plan <plan file> -o <report path>` |
| Merge Review | `claude-codex-review --repo <branch worktree> --base <default branch> --merge-with <default branch> -o <report path>` |

**`-o` is REQUIRED, same as `--base` and `--repo`** — the wrapper fails at
argument parsing without it. Code Review's row omits `--merge-with`; Merge
Review's adds it, and that flag is what keeps the two gates two gates rather
than one run read twice — not the diff, which is byte-identical either way.
See `merge-review.md` in the same directory as this file for what `--merge-with` changes.
Code Review's `--plan` sends the plan as the contract the `spec-drift` lens
reads; without it the wrapper says spec drift was not checked. Merge Review's
row never carries it — the wrapper refuses the pair.

Both diff gates dispatch Codex in a scratch worktree the wrapper builds
from committed history and removes afterwards — a detached snapshot of HEAD
for Code Review, the merge result for Merge Review — so uncommitted edits
in `--repo` are never reviewed, and a dirty `--repo` prints a warning
counting them. Before dispatch the wrapper prints one
`reviewed snapshot: head=… tree=… base=…@… merge-base=…` line; copy it into
the build ledger as a `Snapshot:` line.

**Exit 4 means Codex is unavailable, not that the review failed:** `codex`
is not on PATH, its auth file is missing (never signed in), or the account
does not serve the model this gate uses (the wrapper says which). Any other
`codex` failure exits 1. Nothing was reviewed. Stop and ask the
user to choose: fix Codex (install it, sign in, or set
`codex_review.models.<gate>` to a model the account serves) and re-run the
wrapper, or run this one gate with the `economical` dispatch and a
`Ruling:` line naming the reason. Never record the gate as run, and never
change `build.review` on the user's behalf.

### Code Review lenses

**Always:**

| key | prompt |
|---|---|
| `spec-drift` | Compare the diff against the plan. Flag anything built that no task asked for, and any acceptance criterion the diff does not actually satisfy. |
| `neutered-tests` | Find tests that pass for the wrong reason: assertions loosened, skips added, exceptions swallowed, expected values hardcoded, a test rewritten to match the behaviour it was meant to constrain, or a test whose name describes a branch its body never reaches. A green suite that no longer tests anything is worse than a red one. |

**Always, too:**

| key | prompt |
|---|---|
| `correctness` | Trace the actual control flow of the changed code. Look hardest at error paths, empty inputs, and the case where an external call fails. Give a concrete failing input for every finding. |
| `house-rules` | Read the target repository's `CLAUDE.md`. Flag violations and quote the rule. Pay attention to which things are supposed to fail open and which fail strict — a change that flips one is a finding even if every test passes. |

**And these, every time** — each names the diffs it matters most on, but the reviewer applies all of them:

| key | add when | prompt |
|---|---|---|
| `error-asymmetry` | the diff touches a hook, guard, or any `except` | In this codebase some things fail **open** and some fail **strict**, and which is which is deliberate: guards fail open because a guard that misfires gets disabled, config fails strict because a missing value must narrow trust. For every error path in this diff, name which direction it fails and whether that matches the surrounding component's documented direction. Flag any `except` that now swallows a *wider* class of error than before, any new fallback that hides a real failure from the one person who could act on it, and any handler whose recovery makes a later failure harder to diagnose. Do **not** flag a bare `except: exit(0)` in a hook — that is the documented house pattern. Flag it only where the swallowing is new, wider than the pattern requires, or wraps the diagnostics as well as the body. |
| `coverage-gaps` | the diff adds a branch, guard clause, or failure mode | Ignore line coverage; ask what *behaviour* is now untested. For each new branch, guard clause, error path, and boundary condition, decide whether any test would fail if it were wrong. Report only gaps where you can name the concrete regression the missing test would catch, and rate each 1–10 on how much it would cost to miss. Also flag the inverse: a new test you cannot map to any failure mode. Remember this repo's rule — a green test is not evidence until someone has seen it fail. |
| `comment-rot` | the diff changes behaviour a comment or docstring describes | Find comments, docstrings, and prose that the diff has made wrong, and comments that were *already* wrong and are now load-bearing because someone will trust them. Check every comment that states a constant, a relationship between constants, a payload shape, a firing site, or an ordering guarantee against the code as it now stands. A comment can stay wrong through several edits because each reader trusts it — re-derive the claim from the code, never from the prose. |
| `instruction-docs` | the diff changes text a model reads and obeys — a command, skill, agent, `CLAUDE.md` or `AGENTS.md`, a prompt, an MCP tool description, or a hook's message | Treat every instruction document the diff touches as executable: a model will obey it literally, and no test reads it. Check each factual claim it makes against its primary source — the code, the config, or the other document it describes — including claims the change introduced while moving or rewriting nearby text. Flag a sentence that still reads as an instruction but lost its antecedent when the text around it moved: a "this", "above", "that rule" or section name that now points at nothing. Resolve every path, command and setting it tells a reader to use from where that reader will actually be standing — a different repository, or a machine where the installer has not run yet — and flag any that does not exist there. Flag any contradiction with another section, an example, or a reference file. Then, as a separate step that no suite can do for you, hunt for claims this change made stale in files it did not touch. List each behaviour, rule, default or flow the diff changes, one line each. For each, search the whole repository — templates, agent and command definitions, READMEs, skills and other docs, not only the directories the diff touched — by the words that describe it, not only its identifiers: a stale claim usually states the old behaviour in words the diff never uses. Read every hit and flag each one that still describes the old behaviour or now contradicts the new one. Record the behaviours you listed and the searches you ran in `why_not`, so a skipped hunt is visible. |

You wrote the plan, so you are the worst-positioned reader of whether it was a
good one. That is why this runs as fresh context rather than as your own pass
over the diff — and why the reviewer reads at `max`, above the effort you wrote
it at.

The last three came from reading `pr-review-toolkit`'s specialist agents rather
than installing it. A plugin's agents cost always-on context in every session and
are tuned to somebody else's codebase — `silent-failure-hunter` in particular
treats every swallowed error as a defect, which would flag every hook in this repository. A
lens costs nothing until it runs and says what *this* repo means. Take the idea,
leave the plugin.

For an ordinary review outside a `/build` — someone else's PR, a branch you did
not write — dispatch the `toolkit-build:code-reviewer` subagent instead. It is the
same agent the economical mode above dispatches through `blinded-review.js`, at
the same depth (`agents/code-reviewer.md` carries `effort: max`), so outside the
gate it is neither cheaper nor shallower. Its blinding comes from what you hand
it: a brief from `claude-merge-brief`, never a bare PR number. That subagent is also the instrument
the standing merge-ref rule uses — see
`merge-review.md` in the same directory as this file.
