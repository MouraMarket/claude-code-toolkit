### What this gate reviews, and when

This gate reviews a design — an approach, before a plan exists. There is no
diff yet and no task list yet, only the shape of the thing you intend to
build: which pieces exist, what calls what, and which problem each piece is
answering. Every architectural build runs it, at step 7 of
`brainstorming.md` in the same directory as this file, before you write the plan, not after
— the review is meant to change the plan, not to grade one already written.
It is skipped for the design revision a design-moment `architecture-review`
reviewed (a mission marked `Architecture review: yes`); that review carries
these lenses. A material change after it still runs this gate, per step 8 of
`brainstorming.md`.

The reason to run it at all, rather than folding this into Plan Review, is
cost. A design defect caught here costs one conversation turn; the same
defect caught at Plan Review costs a plan rewrite, because the plan has
already been built on top of it; and caught at Code Review it costs the
build itself, because an executor has already spent tokens implementing the
wrong approach faithfully. Each later gate is a more expensive place to learn
the same thing.

### Independence — point, do not duplicate

The three mechanisms that carry independence — blinding, inverted burden of
proof and effort asymmetry — are set out in
`plan-review.md` in the same directory as this file and apply here unchanged. `/effort max`
on the architect's own session collapses the third mechanism the same way it
does there.

### Which reviewer runs — resolve the mode first

Run `claude-build-route --review-mode`.

Under **`economical`**, assemble the brief, then dispatch one blinded reviewer:

```bash
cat <design document> <the requirements it answers> > <brief path>
test -s <brief path>
```

```
Workflow({ scriptPath: "<absolute path of blinded-review.js in the same directory as this file>",
           args: { gate: "design", briefPath: "<absolute brief path>",
                   lensDir: "<absolute path of the directory holding this file>" } })
```

**If `Workflow` is not in your tools**, dispatch the same review through the
Agent tool. Use `Workflow` when it is in your tools; when it is not, or the
call fails because the tool is unavailable, use this instead; never skip the
gate, and never review the design yourself in its place. `Workflow` is off
by default on Pro plans, and the `enableWorkflows` setting,
`CLAUDE_CODE_WORKFLOWS=false`, `CLAUDE_CODE_DISABLE_WORKFLOWS=1`, a server
flag or an org policy can remove it. The report goes beside the brief, at
`<directory of the brief>/design-report-<attempt>.json`, with `<attempt>`
starting at 1 and incremented for every retry; a report path is never reused
or deleted.

```bash
claude-review-gate prompt --gate design --brief <absolute brief path> --lens-dir <absolute path of the directory holding this file> --report <report path>
```

```
Agent({ model: "opus", effort: "max",
         description: "design review", prompt: "<everything the command printed>" })
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

The reviewer applies every row of the lens table below.

Under **`full-power`**, dispatch the wrapper's design mode, naming the
design document first and the requirements it answers after it:

```bash
claude-codex-review --design-review <design document> <the requirements it answers> --repo <repo> -o <report path>
```

It joins the files under one `=== <path> ===` header each, runs the same leak
scan a plan brief gets, sends the fixed prompt
`tools/codex-review/design-brief.md`, and validates the report as every other
gate's is validated. **Blinding is held by the wrapper:** it takes file paths
and nothing else — never a prior round's findings, never which revision this
is, never the author's summary. The requirements file goes to the reviewer
verbatim, so choose one that carries none of those either. Do not dispatch
plain `codex exec` with a prompt you wrote.

If the wrapper exits 4, Codex is unavailable: follow "Exit 4" in
`code-review.md` in this directory.

### The lens table

| key | prompt |
|---|---|
| `wrong-premise` | You are reading a design, not a plan. Every design rests on claims about how something that already exists behaves — a file's contents, a tool's flags, a config key, an API's response, what a command builds and when it builds it. For each such claim, decide whether the author READ it or REASONED to it, and flag every one that reads as measured but is not sourced. An inferred premise is not a defect; an inferred premise dressed as a measured one is. Name the file or command that would settle each. |
| `simpler-alternative` | Describe the smallest thing that would solve the problem this design states — not the problem it implies. Then, for each piece of the design that goes beyond that, name what specifically breaks if the piece is cut. Anything you cannot name a concrete failure for is unjustified complexity and is a finding. The house order of preference is: no new file over a new file, one function over a framework, a config line over a script, an existing tool over a built one. |
| `silent-failure` | For each component of this design, answer one question: when it is wrong, what tells anybody? Name the observer — a crash, a non-zero exit, a blank render, a human seeing an obviously wrong answer on first use. Flag every component whose failure produces a confident wrong answer, or no answer where one was expected, with nothing downstream complaining. A guard that fails open, a scheduled job nobody watches, a test that cannot fail, and a number a human trusts unchecked are all this class. |
| `already-exists` | Search the target repository for something that already does what this design proposes, in whole or in part, and flag any duplication you find. Then read the repository's `CLAUDE.md` "Decisions not to re-litigate" section and `docs/decisions.md`, and flag anything this design re-proposes that has already been measured and rejected. Quote the decision and its date. |
| `seams` | This design will be called by things that do not exist yet. Name every seam — what a caller passes in, what it gets back, what happens when an input is absent or malformed, and how a caller selects behaviour. Flag any seam described in prose but given no concrete shape, and any place where the design requires a caller to know something the design never hands them. |

You wrote the design, so you are the worst-positioned reader of whether it is
a good one. That is why this runs as fresh context rather than as your own
second look at it — and why the reviewer reads at `max`, above the effort you
wrote the design at. A clean-looking result from a gate you ran honestly, with
the surfaces named truthfully, is the intended outcome on a genuinely sound
design; it is also the one result this design cannot distinguish from a
reviewer who never looked hard enough. Applying all five lenses at that
effort floor is what keeps those two cases from collapsing into each other.
