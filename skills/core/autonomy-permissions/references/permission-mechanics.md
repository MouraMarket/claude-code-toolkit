# Permission mechanics — verified reference

Verified against the Claude Code permissions documentation (`code.claude.com/docs/en/permissions`)
on 2026-07-30, Claude Code v2.1.215. Re-verify before relying on a detail here; this file records
behaviour that has changed between versions and is likely to keep changing.

## Contents

- [Evaluation order](#evaluation-order)
- [Bash rule syntax](#bash-rule-syntax)
- [Read and Edit rule syntax](#read-and-edit-rule-syntax)
- [Permission modes](#permission-modes)
- [Hooks vs. rules](#hooks-vs-rules)
- [Four traps that fail silently](#four-traps-that-fail-silently)

## Evaluation order

**deny → ask → allow.** First match in that order wins. Rule specificity does not reorder
anything.

The consequence that shapes every deny list: **a deny rule cannot carry exceptions.** `Bash(aws *)`
in deny blocks `aws s3 ls` even with an explicit `Bash(aws s3 ls)` allow rule. If you need
"everything except X", the deny rule has to describe X precisely, or you need a hook.

Deny rules behave differently depending on shape:

- A **bare tool name** (`Bash`) removes the tool from Claude's context entirely — Claude never sees
  it exists.
- A **scoped rule** (`Bash(rm *)`) leaves the tool available and blocks matching calls.

Across settings scopes, deny always wins: a user-level deny blocks a project-level allow, because
deny from any scope is evaluated before allow from any scope.

## Bash rule syntax

Wildcards are `*` and may appear anywhere. Only `*` is a wildcard (`?` and `[` are literal).
`\*` is a literal star only inside a rule that also holds an unescaped `*`: `Bash(rm \* *)`
matches `rm * x`, but `Bash(rm \*)` has no wildcard, so it is an exact rule that keeps its
backslash and never matches `rm *`.

| Rule | Matches |
|---|---|
| `Bash(npm run build)` | exactly `npm run build` |
| `Bash(npm run test *)` | commands starting with `npm run test` |
| `Bash(npm *)` | any command starting with `npm ` |
| `Bash(* install)` | any command ending with ` install` |
| `Bash(git * main)` | `git checkout main`, `git log --oneline main` |

A single `*` spans spaces, so one wildcard covers multiple arguments.

**Word boundary:** `*` with a preceding space requires the prefix to end at a space or
end-of-string. `Bash(ls *)` matches `ls -la` but not `lsof`. `Bash(ls*)` without the space matches
both. Prefer the spaced form; the unspaced form catches unrelated binaries.

**Compound commands are split.** Recognised separators: `&&`, `||`, `;`, `|`, `|&`, `&`, newline.
Every subcommand must match a rule independently — `Bash(safe-cmd *)` does not permit
`safe-cmd && other-cmd`. This is why a deny rule containing a pipe (`Bash(curl * | sh)`) can never
fire: the pipeline is already split before matching.

**Wrappers stripped before matching:** `timeout`, `time`, `nice`, `nohup`, `stdbuf`, `command`,
`builtin`, and `noglob`. So `Bash(npm test *)` covers
`timeout 30 npm test`.

`xargs` is not stripped; instead a prefix rule (`X:*`) also matches `xargs X …`, and a wildcard
rule falls back to matching `xargs <pattern>`, for allow rules only when the pattern ends in `*`
(ask and deny rules always).

**Wrappers NOT stripped:** `npx`, `docker exec`, `devbox run`, `mise exec`, `direnv exec`. These
execute their arguments, so `Bash(devbox run *)` permits `devbox run rm -rf .`. Grant the runner
and inner command together (`Bash(devbox run npm test)`) or accept that you are allowing arbitrary
execution.

**Never auto-approvable by prefix:** `watch`, `setsid`, `ionice`, `flock`, and `find` with `-exec`
or `-delete`. These always prompt; only an exact-match rule covers a specific invocation.

**Commands invoked by path are not a command family.** `/Users/me/.claude/plugins/cache/…/task-brief`
and `$SDD/scripts/review-package` match no `Bash(<name> *)` rule — the rule would have to contain
the whole path, which breaks on the next plugin version bump, and a `$VAR` path is not knowable
until runtime. In agentic work this is not an edge case: skills and plugins invoke helper scripts
by path constantly, and they can be most of the prompts left under an
otherwise-thorough mined allowlist. This is the single strongest argument for `Bash(*)` plus a
hook over any mined allowlist.

**Environment assignments:** allow rules match past a leading assignment only for a known-safe set,
so `Bash(npm test *)` covers `NODE_ENV=test npm test` but not `FOO=bar npm test`. Deny and ask
rules match past *any* leading assignment — `Bash(rm *)` in deny still catches `FOO=bar rm -rf tmp/`.

### Built-in read-only commands

These run with no prompt **in every mode**, and writing allow rules for them accomplishes nothing:

`ls` `cat` `echo` `pwd` `head` `tail` `grep` `find` (without `-exec`/`-delete`/`-fprint*` and kin) `wc` `which` `diff` `stat` `du` `cd`
plus read-only forms of `git`.

The set is not configurable. To make one of them prompt, add an `ask` or `deny` rule.

They still prompt when: an unquoted glob is combined with a write-capable command (`find`, `sort`,
`sed`, `git`); `docker` is pointed at another daemon; `file` uses `-m`/`-f`; an argument is a
Windows UNC path; or the command exceeds 10,000 characters or cannot be parsed.

**A write through an output redirect is never free.** `echo x > a.txt` prompts with no rule, still
prompts under `Bash(echo *)` and under a bare `Edit` rule, and runs free only under a blanket
`Bash(*)` grant (a bare `Bash` rule is assumed to behave the same, not probed). Redirects to
`/dev/null` and fd copies such as `2>&1` are not writes and stay free. Measured 2026-10-09 on
v2.1.292. `permissions.defaultMode: acceptEdits` and path-scoped `Edit(...)` rules may clear a write
inside the working directory; the scripts do not model modes or path rules.

`cd` into the working directory or an additional directory is read-only, but `cd` **plus** `git`
prompts when the directory actually changes — running `git` in a new directory can execute that
directory's hooks.

**This caveat governs the no-rule case only.** Verified empirically on v2.1.215: with a matching
allow rule (`Bash(git *)`, or a blanket `Bash(*)`), `cd /some/other/repo && git status` runs
without a prompt. The caveat describes when the *built-in read-only set* stops covering you, not a
behaviour that overrides explicit rules. Worth stating because the opposite assumption produces
phantom findings when auditing a config that is in fact clean.

## Read and Edit rule syntax

Both use **gitignore pattern syntax**. Four anchor forms, and the difference between them is the
most common source of rules that silently match nothing:

| Pattern | Anchors at | Example |
|---|---|---|
| `//path` | filesystem root | `Read(//Users/you/secrets/**)` |
| `~/path` | home directory | `Read(~/Documents/*.pdf)` |
| `/path` | **the settings source**, not the filesystem root | `Edit(/src/**/*.ts)` |
| `path` or `./path` | current directory | `Read(*.env)` |

A single leading slash is **not** an absolute path. In `~/.claude/settings.json`, `Read(/secrets/**)`
resolves to `~/.claude/secrets/**` — not `/secrets` and not `<project>/secrets`.

**For user-level rules meant to apply inside every project, use `//` or `~/`.** A `/path` rule in
user settings can only ever match under `~/.claude/`.

Where `/path` resolves, by source:

| Rule defined in | `/path` resolves to |
|---|---|
| `.claude/settings.json` | `<project root>/path` |
| `.claude/settings.local.json` | `<original cwd>/path` |
| `~/.claude/settings.json` | `~/.claude/path` |
| CLI flags, `/permissions`, session rules | `<original cwd>/path` |

**Coverage:** `Edit` rules apply to all built-in file-editing tools. `Read` rules apply
best-effort to Grep, Glob, `@file` mentions, and IDE-shared context. A `Read` deny rule also blocks
`Edit` on the same path (v2.1.208+), including creating a new file there — but not `Write` or
`NotebookEdit`, so add an explicit `Edit` deny for paths nothing may change.

**Limit:** Read and Edit deny rules cover built-in tools and the file commands Claude Code
recognises in Bash (`cat`, `head`, `tail`, `sed`). They do **not** cover arbitrary subprocesses — a
Python or Node script that opens the path itself is unaffected. Only OS-level sandboxing blocks
all processes.

## Permission modes

Set via `defaultMode`:

| Mode | Behaviour |
|---|---|
| `default` (alias `manual`) | Prompts on first use of each tool |
| `acceptEdits` | Auto-accepts file edits and `mkdir`/`touch`/`mv`/`cp` for paths in the working directory or `additionalDirectories` |
| `plan` | Reads and read-only shell only; no source edits |
| `auto` | Auto-approves with background safety checks that verify actions match the request |
| `dontAsk` | Auto-**denies** anything not pre-approved. Note `AskUserQuestion` is denied too |
| `bypassPermissions` | Skips prompts except explicit `ask` rules and MCP tools marked `requiresUserInteraction` |

`bypassPermissions` also stops prompting for writes to `.git`, `.config/git`, `.claude`, `.vscode`,
`.idea`, `.husky`, `.cargo`, `.devcontainer`, `.yarn`, `.mvn`. Documented as container/VM-only.

Its one remaining circuit breaker: removals targeting filesystem root or home (`rm -rf /`,
`rm -rf ~`) still prompt, including through `$(...)`, backticks, or `<(...)` substitution
(v2.1.208+).

`dontAsk` denying `AskUserQuestion` makes it incompatible with a decision-gate workflow — the gate
mechanism itself gets blocked.

To prevent either mode from being selected, set `permissions.disableBypassPermissionsMode` or
`permissions.disableAutoMode` to `"disable"`.

## Hooks vs. rules

This is the leverage point for an autonomy setup.

- **A `PreToolUse` hook exiting with code 2 stops the call before permission rules are evaluated.**
  It therefore overrides allow rules, including a bare `Bash` allow.
- **Hook `"allow"` decisions do not bypass deny or ask rules.** Deny-first precedence is preserved
  regardless of what the hook returns.

So the two compose in one direction only: hooks can *subtract* from what rules permit, never add.

The documented idiom for "run everything except a few shapes" is exactly this: put `Bash` in
allow, and register a `PreToolUse` hook that rejects the specific commands you want blocked. A
hook can inspect the entire command string — pipelines, argument paths, substitutions — none of
which a rule pattern can see.

Practical guidance that follows: **use deny rules for paths, hooks for command shapes.** Path
patterns are what gitignore syntax is genuinely good at. Command shapes are what it is genuinely
bad at.

## Four traps that fail silently

Each of these produces a config that looks correct and does nothing.

1. **`Write(...)`, `NotebookEdit(...)`, `MultiEdit(...)`, `Glob(...)` path rules are accepted but
   never consulted.** File permissions are checked against `Edit(path)` and `Read(path)` only. Use
   `Edit(docs/**)` instead of `Write(docs/**)`, and `Read(docs/**)` instead of `Glob(docs/**)`.
   Claude Code warns at startup (v2.1.210+):
   `Permission deny rule (.claude/settings.json): Write(docs/**) is not matched by file permission checks`
   A tool-name rule with no path (deny `Write`) is fine and matches at the tool level.

2. **`/path` in user settings anchors at `~/.claude/`.** `Read(/secrets/**)` in
   `~/.claude/settings.json` protects `~/.claude/secrets/**` and nothing else. Use `//` or `~/`.

3. **Deny rules containing shell operators never match.** The command is split on separators before
   matching, so `Bash(curl * | sh)` has no subcommand to match. Pipe-to-shell needs a hook.

4. **Argument-constraining Bash patterns are defeated trivially.** `Bash(curl http://github.com/ *)`
   misses `curl -X GET http://...`, `https://`, `curl -L` through a redirect, and `URL=... && curl $URL`.
   Runs of spaces and tabs are collapsed before prefix and wildcard matching, so doubled spaces do
   not defeat a pattern. For network control, deny the Bash network tools outright and use
   `WebFetch(domain:...)` — noting that allowing Bash at all means `curl` can still reach anything
   unless denied.

## Where approvals get saved

"Yes, don't ask again" for a Bash command writes to `.claude/settings.local.json` at the **git
repository root**, resolved through worktrees to the main checkout (v2.1.211+), so the rule applies
across the whole repo including its worktrees. File-modification approvals are never saved — they
last until the session ends. Outside a git repo, or when the repo root is the home directory, the
rule lands in the starting directory.

Relevant to a worktree workflow: an approval granted in one worktree now applies in all of them,
which is usually what you want and occasionally surprising.
