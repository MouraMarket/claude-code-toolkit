# claude-code-toolkit: working in this repo

This repository is a Claude Code toolkit for one person's Mac: guards, hooks,
skills, subagents, permission sets, the instruction templates, and the
`install.sh` that sets them up. This file is the rulebook for **changing the
toolkit itself**. The policy the toolkit gives your own sessions lives in
`policy/CLAUDE.md`, imported into `~/.claude/CLAUDE.md`; it is not repeated here.

**Adding to this file?** A line here is paid in every session that loads the
repo. A new rule earns a section. Something already covered earns nothing. A
sharper version of a rule edits that rule in place. Rationale, evidence and
incidents behind a rule go to `docs/decisions.md`, never here.

## Behaviour in the repo, values in your config

| | In this repo | Outside it |
|---|---|---|
| **Behaviour**: hooks, agents, skills, scripts | yes | |
| **Policy**: permission sets, templates, conventions | yes | |
| **Your paths, names, thresholds** | | `~/.config/claude-code-toolkit/config.json` |
| **Your `CLAUDE.md`** (built from `CLAUDE.md.template`) | template only | your workspace root |
| **Session history, memory, approvals** | | `~/.claude/`, never here |

The test for a new thing: would it work on someone else's Mac without editing
it? If it only works because of a path or a preference of yours, the behaviour
goes in the repo and the value goes in config.

### Adding a configurable value

Never hardcode a path, name or threshold in a tool. Read it through
`lib/toolkit_config.py`:

```python
import toolkit_config as config
handover_dir = config.handover_dir()                       # well-known getter
url = config.get("session_titler", "some_key")             # arbitrary key
```

Then do all three, or the value is invisible to everyone else:

1. Document the key in `config.example.json`, **including the default it falls
   back to** and what happens when it is absent.
2. Give it a sane default in code. A missing key must never raise inside a hook.
3. If it affects safety, make the absent case the **stricter** one.
   `workspace_root` is the model: unset means the danger guard trusts almost
   nothing, rather than guessing a root and silently widening what may be
   deleted.

If setup should ask about it, add a prompt to `lib/setup_config.py` and a test
to `lib/tests/test_setup_config.py`.

**Configured paths are expanded only by `toolkit_config.expand_path`.** A private
expander in a guard once let a `$VAR` spelling disarm a protected path. Guards
load `lib/toolkit_config.py` by file path, so **a test that copies a guard
elsewhere must copy `lib/toolkit_config.py` to the same relative place**, or the
guard silently loses its config.

## Hard rules

- **One source of truth for a live tool.** A skill that needs the danger guard
  references `tools/danger-guard/`; it never carries its own copy.
- **No secrets, ever.** Credentials resolve at run time from an environment
  variable or a reference your secret manager resolves. Tests use synthetic
  credentials, names and paths only.
- **`realpath`, not `abspath`, for anything `__file__`-relative.** The commands
  run through `~/.local/bin` symlinks, and `abspath` does not resolve a symlink:
  the import fails at run time while every test passes. It bites where a tool
  walks several directories up from `__file__` to reach `lib/`.
- **Hooks are best-effort and exit 0 on unexpected input.** A broken hook must
  never cost you a prompt. Wrap the body, never the diagnostics. **A guard that
  crashes still allows, but never looks like one that checked:** it prints one
  JSON object, `systemMessage` plus `additionalContext`, saying it crashed and
  did NOT check, with no `permissionDecision`.
  `lib/tests/test_guard_crash_reports.py` pins every PreToolUse guard to that
  shape.
- **The guard fails open; the config fails strict.** Anything a guard does not
  positively recognise as dangerous, it allows: a guard that misfires on
  ordinary work gets disabled, and a disabled guard protects nothing. A missing
  or broken config, by contrast, narrows what it trusts. Do not "fix" that
  asymmetry.

  **One narrow exception:** `claude-worktree-guard` refuses when a git call
  **was killed by its own timeout** (a stall is positive evidence something is
  wrong), and only for a verb in `BLOCKING_VERBS`. **It does not refuse when the
  time budget merely runs out**: a spent budget is not evidence. At extreme
  command lengths the guard fails open, because a killed `PreToolUse` hook
  returns no verdict.
- **The shipped default is not one person's preference.** `baseline` applies on
  every install and only ever narrows. `autonomous` is opt-in, behind a typed
  confirmation, and adds to `baseline` rather than replacing it. A looser
  setting belongs in your config (`permissions.baseline_opt_out`), never in the
  shipped file.
- **Concurrent work gets its own worktree.** If another session might touch this
  repo, run `git worktree add -b <branch> <path>` and work there instead of
  switching the shared checkout. `claude-worktree-guard` enforces this: it always
  blocks a state-changing git command reaching into a different checkout than the
  one you are in (reads such as `git -C <other> status` are allowed), and once a
  repo has two or more worktrees it also blocks branch switches,
  hard resets and stashing in the shared checkout. Escape hatch:
  `WORKTREE_ALLOW=1`.

  **Rule C is off unless `worktree_guard.canonical_root` is set, never derived**
  from `workspace_root`. When set, it refuses work landing directly on a
  canonical checkout's default branch. Hatch: `WT_ALLOW_MAIN=1`; the reasoning is
  in `config.example.json`.
- **`policy/CLAUDE.md` is live.** `lib/team_policy.py ensure` adds one import
  line to `~/.claude/CLAUDE.md`, so a policy edit reaches every session on that
  machine on the next `git pull`. `team_policy.import: false` only stops `ensure`
  from writing the import: one already in `~/.claude/CLAUDE.md` keeps loading,
  and `--status` reports `opted-out` and says so. Remove the line to stop it.
  `CLAUDE.md.template` is the opposite case: `install.sh`
  never copies it, so a change reaches only a `CLAUDE.md` built after it.
  `install.sh --check` reports the import's state (`team_policy.py --status`).
  Say which of the two a change touches.
- **Never run `./install.sh` from a worktree.** It resolves the repo root to
  wherever it runs, so from a linked worktree it repoints every `~/.local/bin`
  symlink, `claude-danger-guard` included, at a directory deleted the moment the
  branch merges. The installer refuses; `ALLOW_WORKTREE_INSTALL=1` exists only
  for testing `install.sh` itself. `--check` is exempt, but from a worktree it
  lists every `~/.local/bin` symlink as pending: that is the symptom, not drift.

  **Detection is git-based, not path-based.** `lib/worktree_detect.sh`, sourced
  by `install.sh` and `scripts/setup-git.sh`, compares the resolved `--git-dir`
  with `--git-common-dir`. Both callers fail closed if the helper is unreadable.
  Each load-bearing property has a test in `lib/tests/test_install_worktree.py`.
- **Anything new in the installer gets a test in `lib/tests/`.** The fixture
  helpers there (`make_fixture_repo`, `run_install`) link the real `install.sh`
  into a throwaway git repo and point `$HOME` at a sandbox.

## Testing

Every tool has a `tests/` directory; so do `lib/`, `skills/`, `evals/`,
`scripts/` and `ci/`. Run all of them. This exits non-zero on any failing suite
**and** when it finds no suites at all:

```bash
failed=0; ran=0
while read -r t; do
  ran=$((ran + 1))
  (cd "$(dirname "$(dirname "$t")")" && env -u CLAUDE_CONFIG_DIR -u GIT_CONFIG_GLOBAL python3 "tests/$(basename "$t")" </dev/null) || { echo "FAIL $t"; failed=1; }
done < <(find tools lib skills evals scripts ci -path '*/tests/test_*.py' -type f | sort)
echo "$ran suites run"; [ "$ran" -gt 0 ] && [ "$failed" -eq 0 ]
```

**`</dev/null` is load-bearing.** The queue of remaining suites rides the loop's
own stdin, so a suite that reads stdin swallows the rest and the loop still
reports a pass for everything it reached. `install.sh`, `ci/local-checks.sh`,
this file and `AGENTS.md` carry the same redirect, and
`lib/tests/test_install_checks_every_suite.py` pins them.
`env -u CLAUDE_CONFIG_DIR -u GIT_CONFIG_GLOBAL` is pinned the same way: suites
re-point `HOME` at fixtures, and either variable would follow your real setup
past it and turn suites red.

**A suite outside those directories runs nowhere.** `lib/tests/
test_suite_discovery.py` reads the roots from `install.sh` and
`ci/local-checks.sh` and fails on any `tests/test_*.py` outside them. To add a
root, add it to both runners.

The full check is `ci/local-checks.sh` from the repo root: both Pythons, the
secret scan, the installer, context lint, the skill bundle limits
(`scripts/check_bundles.py`) and its own self-test. `claude-local-ci`
runs it against a head merged with its base and records a receipt. Python 3.9
standard library only; the suites also run on 3.13. There is no `pytest`.

**Before a release, `python3 scripts/check-capabilities.py --all --require-evidence --live-evidence FILE`.**
It proves each entry in `docs/capabilities.json` with tests that actually ran:
every suite passes at least one test on both Pythons, nothing fails, and every
skip that fires is listed with its reason in `docs/capability-skips.json`. It
needs Node and a clean tree, and fails any suite no capability owns. `FILE` is
the record of a live run of every skill (its format is in the script's
docstring); without it each skill stays `awaiting-live` and the run exits 1, so
leaving it off checks the suites only and is never a release pass. A new suite
needs an owner in the registry; a new skip needs an allowlist entry or a fix.

**From inside a worktree, use `./install.sh --check`.** After `EnterWorktree`,
Claude Code itself refuses commands it cannot prove stay in the worktree: loops,
computed `cd`, a computed program. Put multi-step work in a script file and run
it by literal path. `--check` runs the same suites and changes nothing; read only
its `Tests` section.

Standards every suite is held to:

- **Hermetic.** No dependence on whose config, roster or workspace is
  installed. Where an environment dependency is unavoidable, `skipTest`; never
  assert against ambient state.
- **Passes with no config at all**, which is what a fresh clone looks like.
  `toolkit_config` reads `CLAUDE_TOOLKIT_CONFIG` once, at import, so an
  in-process call passes the config-derived value explicitly:

  ```bash
  CLAUDE_TOOLKIT_CONFIG=/nonexistent.json python3 tests/test_whatever.py
  ```
- **A subprocess test inherits the SURFACE it runs on.** Anything the subprocess
  can reach (an env var, a CLI on PATH, the network, the clock, a git config
  value, an inherited signal disposition, the filesystem outside its sandbox) is
  ambient state. Build a child's environment explicitly, never from
  `dict(os.environ)`. Strip every `GIT_*` variable in a git fixture. Pin HEAD on
  a bare fixture remote as well as the seed repo, because `git init` reads
  `init.defaultBranch` and `clone` carries it. A non-interactive shell starts
  `&` jobs with SIGINT ignored; reset it in `preexec_fn` and assert the
  interrupt landed.
- **A wall-clock budget is an assertion against ambient state.** Ask what the
  budget stood in for and assert that (does the path do git work, does cost grow
  with record count); PATH shims that log argv and `exec` the real binary
  usually get you there. A hook's declared timeout is itself the property, so no
  tighter timing assertion. A loose ceiling may stay as a catastrophe bound if
  its comment says it is not a budget.
- **Every silent probe needs a positive control.** A probe that observes nothing
  passes a "did no work" assertion for free. Assert the heavy path trips it, in
  the same test class.
- **Test the executables, not just the lib.** A hook with a green lib suite and
  none of the hook itself exercised can reintroduce a fixed bug. Run hooks
  through `subprocess`.
- **Run a tool twice against the same path** wherever it writes to a
  caller-supplied path, with a positive control: a fixture that starts clean
  every run cannot see a staleness bug.
- **A read and a write that name a location must name the same one.**
  `git -C <worktree> fetch` writes `FETCH_HEAD` under the worktree's own git dir;
  `git rev-parse --git-path FETCH_HEAD` is the check.
- **Parse structure; never search a whole document.** Assert against a parsed
  section or table row, and mutate by moving text as well as changing it: a
  moved section must fail. When a doc tells a reader to do something, pin what
  makes it doable, not the sentence.
- **A verification command says how it sees uncommitted work.** `git grep`
  searches tracked files only and `git diff` ignores untracked paths; use
  `git grep --untracked` or `git ls-files --others`.
- **On an `Optional[bool]`, `assertFalse` passes for `None` too.** Use
  `assertIs(x, False)` and `assertIsNone(x)`.
- **No count of a growing set in prose.** Where a count must exist, derive it
  and pin it with a test.

**A green test is not evidence until you have seen it fail.** Mutation-test
anything load-bearing: delete the guard, invert the condition, and watch the
suite go red. Common false passes: asserting only `returncode == 0`, asserting
behaviour that predates the change, a fixture that cannot reach the failure (a
dead pid, a fresh path). Assert a mutation applied before trusting a green run.
Read a harness's kill list from the summary block (`^(?:FAIL|ERROR): (\S+)`),
never from `-v` output, which prints docstrings on the result line.

**A mutation harness must defeat the bytecode cache.** On macOS, `python3`
caches `.pyc` under `~/Library/Caches/com.apple.python`, keyed on mtime and size,
so a same-size, same-second rewrite can run the old bytecode. After every write,
`os.utime` the file to a distinct, increasing mtime. Clearing the cache directory
is not the prescription.

**Mutation testing cannot see a path you never covered.** It grades the tests
you wrote, not the ones you owed. List a hook's side effects and ask which have
a test that would notice their absence. Before refactoring anything
safety-critical, write the behavioural test against the old code first.

## Verification beyond tests

Tests pass in-process; hooks run from the plugin in a real session. Both matter.

- **After changing a hook, exercise it through the installed path**, not only
  its tests: an import-path bug can pass every test and crash every hook.
- **Hooks load at session start.** A new session is enough. Plugin hooks, and
  an edit to a plugin's `hooks/hooks.json`, need `/reload-plugins` or a new
  session. You cannot verify a hook change in the session that made it; `/hooks`
  shows what is active.
- `./install.sh --check` should propose **zero** changes on an installed
  machine. If it proposes something, the installer or the repo is wrong.
  `--check` proves hooks are configured. `--verify-live`, run by Claude inside a
  session, proves that session's hooks fired for every selected profile plugin
  and reports each one's readiness against the tested Claude Code version. A
  terminal run of `--verify-live` cannot prove anything, and says so.
- **The integrity hook's SessionStart tool-rules audit** names a session whose main
  transcript shows a successful call matching a live tool-rules page (after the
  page's mtime and the audit's first run, last 6 days) and no
  `~/.claude/tool-rules-state/` record, among sessions where core's heartbeat fired.
  `claude-toolkit-integrity --tool-rules-audit` runs it by hand.

## Decisions not to re-litigate

These look like accidental complexity and are not. Each has a failure mode
waiting if it is simplified. Short warnings live as comments at the code they
defend; the full reasoning is in `docs/decisions.md`. Search that file before
simplifying anything under `tools/`.

- **The handover sweep has two grace gates, ANDed.** Collapsing them to the
  session count is the obvious simplification and a bug. The same trap:
  `sweep_grace_sessions: 0`, the setting someone reaches for to stop deletion,
  is what causes it.
- **Hooks, skills and agents ship in the profile plugin that holds them.**
  `install.sh` removes only the exact command strings in
  `claude/legacy-hook-commands.json`, never by pattern, so a hook of yours in
  `settings.json` is never touched. It links only `~/.local/bin`.
- **`/build`'s gates dispatch one reviewer each.** `economical` runs one
  blinded Claude reviewer (`tools/build-lens/blinded-review.js` through
  `Workflow`, or one Agent dispatch with the same prompt where `Workflow` is not
  available); `full-power` runs Codex. Every finding is checked against the repo
  and a refuted one is recorded as refuted, never dropped.
- **`/build`'s gate costs are measured on your own seat.** No ratio or price
  ships as a measurement; `docs/build-measurements.md` holds the method. Read it
  before quoting a gate's cost or proposing a cheaper tier.

## Verifying against Claude Code itself

- **Verify a payload's shape against the running binary, never against your own
  plan.** The binary a VS Code session runs lives in the extension bundle, named
  by `$CLAUDE_CODE_EXECPATH`; the newest file under
  `~/.local/share/claude/versions/` is not necessarily it.
- **The CLI enforces plugin `dependencies` on load and on disable, not on
  uninstall.** Disable and uninstall dependents first.
- **`PreToolUse` output has three channels:** `additionalContext` reaches
  Claude, top-level `systemMessage` reaches you, `permissionDecision` reaches the
  harness. An advisory hook omits `permissionDecision` (never `"allow"`, which
  bypasses other permission rules), and prints nothing at all rather than an
  empty `hookSpecificOutput`.
- **To contain a child `claude -p` without changing what it sees, use a
  `--settings` `PreToolUse` hook, never a deny rule.** A deny rule naming a tool
  removes it from the model's context. `tools/skill-trigger/` does this.
- **`install.sh` sets user-level `autoMemoryDirectory`**, so every session,
  headless ones included, loads your memory. A `claude -p` spawn meant to be
  neutral sets `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`.
- **Verify where a hook fires, not only what it carries.** `PostToolBatch` also
  fires inside subagents with the parent's `session_id`.
- **Silence must never look like a calm reading.** Usage limits reach only the
  status line, which runs in the terminal. With no usable reading, the usage
  hooks say `UNKNOWN`, and a derived number is always labelled an estimate.
