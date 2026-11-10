### The independence problem, and what we do about it

An Opus reviewer reading an Opus plan shares the author's priors. It will find
the reasoning persuasive because it is reasoning it would have produced. A
different model is the obvious fix, and a cheaper one is the wrong fix.

The reviewer is Opus at `max`. **A cheaper reviewer fails silently:** its
failure mode is finding nothing, and zero confirmed findings is byte-identical
to a clean gate. **Do not trade the reviewer down to save a gate's cost.**

Three mechanisms carry independence. **All three are load-bearing. Do not drop
one to simplify the prompt.**

1. **Blind the reviewer.** Pass the fixed rubric, the plan's source
   requirements (its `Requirements:` file) and the plan. Never pass your
   brainstorming transcript, your rationale, or your summary of why the design is
   right. Shared priors do their damage when the reviewer can read the author's
   chain of thought and agree with it. A reviewer who sees only *spec in, plan
   out* has to re-derive rather than ratify.
2. **Invert the burden of proof.** The reviewer is not asked whether the plan is
   good. It is told the plan is broken and asked where, and a finding it cannot
   evidence is marked as asserted rather than demonstrated.
3. **Effort asymmetry.** The reviewer is the same model you are, so its
   independence comes entirely from *depth*: it runs at `max`, above the effort
   you wrote the plan at, which surfaces what your own pass did not reach.

**A fourth mechanism, several blind readers with one lens each, is not used.**
One reader applies every lens. What that gives up is real: one reader can
anchor across lenses where several blind readers could not, and there is no
separate refutation stage. **How much one reader misses compared with several
is UNMEASURED.**

### Which reviewer runs — resolve the mode first

Run `claude-build-route --review-mode`.

Under **`economical`**, dispatch one blinded reviewer:

```bash
claude-merge-brief --plan <plan file> --repo <repo> > <brief path>
test -s <brief path>
```

```
Workflow({ scriptPath: "<absolute path of blinded-review.js in the same directory as this file>",
           args: { gate: "plan", briefPath: "<absolute brief path>",
                   lensDir: "<absolute path of the directory holding this file>" } })
```

**If `Workflow` is not in your tools**, dispatch the same review through the
Agent tool. Use `Workflow` when it is in your tools; when it is not, or the
call fails because the tool is unavailable, use this instead; never skip the
gate, and never review the plan yourself in its place. `Workflow` is off
by default on Pro plans, and the `enableWorkflows` setting,
`CLAUDE_CODE_WORKFLOWS=false`, `CLAUDE_CODE_DISABLE_WORKFLOWS=1`, a server
flag or an org policy can remove it. The report goes beside the brief, at
`<directory of the brief>/plan-report-<attempt>.json`, with `<attempt>`
starting at 1 and incremented for every retry; a report path is never reused
or deleted.

```bash
claude-review-gate prompt --gate plan --brief <absolute brief path> --lens-dir <absolute path of the directory holding this file> --report <report path>
```

```
Agent({ model: "opus", effort: "max",
         description: "plan review", prompt: "<everything the command printed>" })
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

Expand `~` yourself. The reviewer applies every row of the lens table below and returns `recommendation`, `executed` and `findings`; read `executed` before `findings`.

Under **`full-power`**, dispatch the wrapper's plan mode:

```bash
claude-codex-review --plan-review <plan file> --repo <repo> -o <report path>
```

It builds the brief itself — `claude-merge-brief`'s plan brief and its leak
scan — sends the fixed prompt `tools/codex-review/plan-brief.md`, identical
every run, and validates the report as it does at Code Review: a stale report
at `-o` is removed first, and a report that fails the schema or describes a
review that never happened exits non-zero. **Blinding is held by the wrapper,
not by you:** it takes a plan path and nothing else, so there is nowhere to
hand over a prior round's findings. Do not dispatch plain `codex exec` with a
prompt you wrote — that path leaked a prior round into the prompt.

If the wrapper exits 4, Codex is unavailable: follow "Exit 4" in
`code-review.md` in this directory.

Under `full-power` Codex applies the same lens table; this gate then spends
Codex quota and zero Claude budget.

### Plan Review lenses

`brief` is the fixed rubric, a `## Source requirements` section when the
plan's `Requirements:` line names a file, and the full plan text — and nothing
else. It comes from `claude-merge-brief --plan`, never hand-assembled.

One reviewer applies every row.

| key | prompt |
|---|---|
| `ambiguity` | You are the executor who will implement one of these tasks and be blamed when it ships wrong. For each task, name every point where you would have to guess — a name, a path, a schema, an interface, a behaviour on empty input. Two readings that produce different code is a defect. |
| `verification` | For each task, decide whether its verification command is *capable of failing* if the task were implemented wrong. Flag any that would pass on the unmodified repo, that assert only an exit code against code which swallows errors, or whose acceptance criteria restate the task instead of describing observable behaviour. |
| `omission` | Compare the source requirements (the brief's `## Source requirements` section; when there is none, the plan's own stated goal) against the tasks. What do they ask for that no task delivers? What did the plan quietly drop, defer, or narrow without saying so? Missing work is the defect no per-task review can ever catch. |
| `sequence` | Trace the task order. Flag any task that depends on a file, function, config key, or fixture that a *later* task creates, and any task whose verification depends on work not yet done. |
| `house-rules` | Read the target repository's `CLAUDE.md`, including the nearest nested one. Flag anything in this plan that violates a stated rule, and especially anything under a "decisions not to re-litigate" heading. Quote the rule. |
| `instruction-docs` | A plan is an instruction document: an executor obeys it literally, and a model will obey any command, skill, agent, `CLAUDE.md` or `AGENTS.md`, prompt, MCP tool description or hook message a task writes. Flag any task that leaves the executor to compose a rule, a claim or a number the plan never states. Check each factual claim the plan makes, or tells the executor to write, against its primary source — the code, the config, or the document it describes. Resolve every path, command and setting the plan tells the executor to use from where the executor will actually be standing — a worktree, a shell whose working directory resets between calls, or a machine where the installer has not run yet — and flag any that does not exist there. Then, as a separate step, hunt for claims the plan leaves stale in files no task touches. List each behaviour, rule, default or flow the plan changes, one line each. For each, search the whole repository — templates, agent and command definitions, READMEs, skills and other docs — by the words that describe it, not only its identifiers: a stale claim usually states the old behaviour in words the plan never uses. Flag each hit that no task updates, and record the searches you ran in `why_not`. |
