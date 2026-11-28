# Decisions not to re-litigate

Each entry here defends code that looks like accidental complexity and is not.
The code carries a short warning comment where the temptation to simplify shows
up; the full reasoning lives here, so it costs nothing per session. `CLAUDE.md`
keeps a one-line pointer to the ones that shape everyday work.

**These are not history to skim.** Each names a failure mode that comes back if
the behaviour is simplified. Before changing a file, search this document for its
path: every entry lists the files it defends on its `Defends:` line.

## Safety guards

### The danger guard refuses the PowerShell tool: an exception to "fails open"

Defends: `tools/danger-guard/guard_lib.py`, `tools/danger-guard/claude-danger-guard`

- **Every PowerShell call is blocked, harmless or not.** None of the guard's
  rules read PowerShell syntax, so allowing the tool would leave an unguarded
  shell next to a guarded one, and an unguarded shell looks exactly like a quiet
  day. Windows is not supported, but the PowerShell tool can still be offered to
  Claude, so the refusal stays as a boundary. The message tells Claude to use the
  Bash tool and tells you to set `CLAUDE_CODE_USE_POWERSHELL_TOOL` to `"0"` under
  `env` in `~/.claude/settings.json`. Porting the rules to PowerShell was
  rejected: a second parser is a second place to be wrong. Like the worktree
  guard's stall refusal, this is a deliberate, scoped exception to "the guard
  fails open".
- **A command nested too deeply to parse is blocked, not crashed on.** The quote
  and substitution scanners recurse once per nesting level, and a crash exits 0,
  which means allow. A few hundred nested `"$(...)"` ahead of a destructive
  command would otherwise slip through. `RecursionError` can be triggered by
  input alone, so it is caught and blocked; every other exception is still a
  visible crash that allows.

### The git global-option list is measured against real git

Defends: `tools/danger-guard/guard_lib.py` (`_git`, `_gitopt`, `_gitval`)

Every rule about `git push` and history rewriting first skips git's global
options (`git -C <dir> --no-pager push ...`), so an option missing from the list
is a bypass: the rule never reaches `push` and the force push runs unjudged.

- **Each option is checked against real git, never copied from its usage line.**
  Negation spellings are not regular. git accepts `--no-literal-pathspecs` and
  rejects `--noliteral-pathspecs`; a list written from the usage text had the
  rejected spelling, missed the accepted one, and omitted `--shallow-file`. Both
  gaps let a force push through while the guard said nothing. Tests in
  `tools/danger-guard/tests/test_guard.py` pin each spelling, and the comment
  above them carries the probe loop to re-run before editing the list. Run it
  inside a real repository: outside one, `rev-parse` fails for an unrelated
  reason and every option reads as rejected.
- **An option's value may be quoted and contain spaces**, so `_gitval` is a run
  of bare characters and quoted chunks, not `[^\s]+`. A value pattern that ended
  at the first space made every value-taking option a bypass at once
  (`git -C "/Users/me/My Repo" push --force`).

### A remote-branch delete is blocked only in its refspec spelling

Defends: `tools/danger-guard/guard_lib.py` (`_EMPTY_SRC_REFSPEC_RE`)

- **`git push origin :branch` is blocked; `git push --delete origin branch` and
  `-d` are allowed. The asymmetry is the decision.** Both delete the remote
  branch. The flag forms announce it, and the baseline permission set's
  `Bash(git push *)` rule already stops them at a prompt. This guard is for the
  shapes a permission rule cannot express, and the empty-source refspec is one: it
  reads like an ordinary push.
- **Why not block `--delete` too.** A block here is a hard refusal that needs
  `DANGER_ALLOW=1` for the whole session. The flag form is routine post-merge
  cleanup while the refspec form is the deliberate deletion, so blocking the
  flag would turn a routine step into a session-wide escape hatch and buy nothing the prompt was not already buying. If you remove the
  `Bash(git push *)` ask through `permissions.baseline_opt_out`, nothing stops a
  flag-form delete; that is your trade to make, not this guard's.
- **Still caught:** `-fd` is blocked by the force rule on its `f`, and a quoted
  bare colon (`git push origin ':'`, git's matching push) deletes nothing and is
  allowed. If you think this is a hole, re-measure the corpus
  (`tools/danger-guard/corpus_diff.py`) and change the permission rule, not this
  pattern.

### A heredoc inside $(...) has its body scanned

Defends: `tools/danger-guard/guard_lib.py`

- **A heredoc inside `$(...)` has its body scanned, and that is left unfixed on
  purpose.** The tokenizer returns the whole substitution, body included, as one
  token, so no bare `<<` token appears and the body is never stripped. The
  text-grep rules then read prose as a command:
  `git commit -m "$(cat <<'EOF' ... EOF)"` is refused when the message mentions
  `sudo` or `git push --force`.
- **The fix points the dangerous way.** Reaching that `<<` needs a second
  recursive shell parser or a text-level `<<` detector. The text detector is
  exactly what was removed, because `1 << 3` is a bit shift: over-firing strips
  text that is not a heredoc body, so everything after it goes unscanned. That is
  a silent false ALLOW where a false ALLOW costs most. The defect itself is a
  false BLOCK: it fails safe and has a one-flag workaround.
- **What ships instead** is a hint in the refusal naming `git commit -F <file>`,
  shown when the command contains `<<` with `$(` or a backtick. The hint changes
  no verdict, so it cannot create a false ALLOW.
- Only the text-grep rules reach inside a substitution. The `rm`, `find` and
  `cd` rules are token- and segment-based, so a message mentioning `rm -rf ~` does
  not trip this and one mentioning `sudo` does. Re-measure against real
  transcripts (`tools/danger-guard/corpus_diff.py`) before reopening.

### A command substitution is not a hiding place

Defends: `tools/danger-guard/guard_lib.py` (`_SUDO_RE`, `_RAW_DISK_RE`, the `dd` matcher)

- **Keep `(` and a backtick in every anchor class.** A substitution puts `(`
  immediately before its first word, so rules anchored only on start of line, a
  separator or whitespace allowed `echo "$(sudo rm -rf /var/log/x)"` and
  `X=$(sudo ...)` while refusing the same command with one more space.
  Over-blocking here is loud and under-blocking is silent.
- **The `;` in these classes does no work; do not hunt for a hole there.** `;` is
  its own token and the command is rebuilt with tokens joined by spaces, so every
  rule sees `true ; dd ...`. `test_a_separator_form_still_blocks` pins it.
- **Still open, deliberately:** `mkfs` must be followed by whitespace or end of
  string, so `mkfs.hfs /dev/disk2` matches nothing. Matching it changes what the
  rule means, not where it may start. Measure before widening.

### Guard coverage: Monitor, equivalent spellings, symlinks, brace expansion

Defends: `tools/danger-guard/guard_lib.py`, `tools/worktree-guard/worktree_lib.py`

**Both shell guards judge the `Monitor` tool exactly like `Bash`.** A Monitor
carries a shell `command` (or a WebSocket URL, which has nothing to judge).

**The danger guard judges what the shell will actually do, and every new check
can only add a block:**

- The command word is normalized before dispatch: unquoted, reduced to its
  basename and lower-cased (`/bin/rm`, `'rm'`, `\rm`, `RM`), past `VAR=x`
  prefixes and `env`/`exec`/`command`/`time` options.
- Every delete target is judged twice: lexically, and where it physically lands
  (symlinks followed, then `..` applied, as the kernel does). A plain `cd` stays
  logical, because the shell collapses `..` before the chdir; only `cd -P` and
  `env -C` are physical. Configured roots also match by their real path.
- Brace expansion in `rm`/`find` targets and `cd` operands is expanded and every
  word judged beside the raw token, within caps. A token with too many `{` or too
  many characters is not scanned at all: unbounded expansion can run close to
  the hook's timeout, and a killed hook returns no verdict. The caps are in
  the code.

**The worktree guard** follows file symlinks for Rule C and walks up to the
nearest existing directory before asking git which checkout a write lands in.
`git -C` applies in sequence, and relative `--git-dir`/`--work-tree` resolve
after every `-C`.

**Accepted gaps:** `bash -c`, `sh -c`, `eval`, `xargs rm` and `env -S` hide a
command inside an argument; reaching them would take a second parser. A
`realpath` on a hung network mount can stall the hook.

**Replaying real transcripts.** `tools/danger-guard/corpus_diff.py` compares two
guard builds on a transcript corpus. A Rule C replay is only valid against the
filesystem the payloads were recorded on: judge a Rule C change only on payloads
whose directory still exists.

### The worktree guard refuses `git checkout <name>`

Defends: `tools/worktree-guard/worktree_lib.py`, `tools/worktree-guard/claude-worktree-guard`

In a checkout with more than one worktree, the guard refuses `git checkout <name>`
and does not ask git whether the name is a ref. Without `--`, a bare argument is
either a branch to switch to or a file to restore. Running `rev-parse --verify`
and allowing the non-ref case is the obvious fix and the wrong one: a wrong answer
lets a real branch switch through silently, the one direction this guard exists
to stop. `git restore <path>` and `git checkout -- <path>` both work and are
tested. The refusal names those two, and each is asserted twice: that the
message names it, and that it is genuinely allowed.

### The worktree guard's walk reads separators lexically and never steps over one

Defends: `tools/worktree-guard/worktree_lib.py`, `tools/worktree-guard/claude-worktree-guard`

- **A separator ends a statement only if it was written unquoted, and when it
  was, it always ends one.** A `git` with no subcommand began its argument scan
  past the separator; an option's value slot took a separator as the value; a
  quoted `)` closed a subshell. Each let a second `git` command hide behind the
  separator unjudged. Separator identity is read from the raw token, so a quoted
  or escaped `;` or `)` is a word. Pinned by
  `TestNoStatementHidesBehindASeparator` and
  `TestQuotedSemicolonIsAnOptionValue`.
- **An option's value slot ends only on an operator, never on a reserved word.**
  `!`, `{` and `}` are special only where a command starts, so `git -C !` takes
  `!` as its directory. Pinned by `TestReservedWordIsAnOptionValue`.

### The model guard denies rather than asks, and never judges Workflow

Defends: `tools/model-guard/claude-model-guard`, `tools/model-guard/guard_lib.py`

- **Deny, not ask.** `ask` puts a prompt in front of every dispatch on a setup
  built to remove prompts, and on a surface with no prompt UI it stalls instead
  of gating. A denial is self-correcting: the reason reaches Claude, which
  re-dispatches naming a model. Being wrong costs one round trip.
- **`Workflow` is outside `GUARDED_TOOLS`.** A workflow's per-agent models are
  arguments inside a JavaScript source string; parsing them out to deny would be
  a guess, and a guard that denies real work on a guess earns a permanent
  `MODEL_GUARD_ALLOW=1`.
- **The definition lookup is where the care went.** macOS's default filesystem is
  case-insensitive, so opening `.../Explore.md` can return another plugin's
  `explore.md` and its pinned model would let the built-in `Explore` through.
  `read_pinned_model` lists the directory and matches the filename exactly. One
  ordered search path keeps the first-match rule: two separate searches let a
  later scope resurrect a pin the harness would never consult. The wiring has its
  own tests, separate from `decide`'s.
- The guard ships in `toolkit-build`, so it runs only where that plugin is
  enabled.

### session-claim matches `*` and excludes readers

Defends: `tools/session-claim/claude-session-claim`

The claim hook matches every tool and excludes a list of readers
(`READ_ONLY_TOOLS`) instead of listing writers. Missing a new reader costs one
spurious claim; missing a new writer is a silent hole in the guard. "A tool that
writes a file names it, so filter on `file_path`" is only half true: `Read`
carries `file_path` too, and treating it as a write tripped the circuit breaker
and disarmed the guard before the first real write. So the filter keys on a path
(`file_path` or `notebook_path`) and then drops the known readers.

### CLAIM_TEST_OWNER_PID is gated on CLAIM_CLAIMS_PATH

Defends: `tools/session-claim/identity_lib.py` (`resolve_hook_owner`)

`CLAIM_TEST_OWNER_PID` is honoured only when `CLAIM_CLAIMS_PATH` is also set. An
unconditional identity override inside a guard is a backdoor; one that works
only after the claims file is redirected to a sandbox can only ever affect a
sandbox. The override exists because identity is read from the process tree, so
the block and release paths could otherwise be tested only inside a real Claude
session. The compound gate lives in `resolve_hook_owner()`, which the claim and
release hooks both call. Do not split it into two independent checks.
`tools/repo-freshness/sync_lib.py` uses the same shape for its own test marker.

### The claim hook's timeout budget is three numbers

Defends: `tools/session-claim/claim_lib.py`, `tools/session-claim/identity_lib.py`

`identity_lib.PS_TIMEOUT` (counted twice), `claim_lib.LOCK_TIMEOUT` (twice) and
`claim_lib.WORKTREE_TIMEOUT` (once) must together fit inside the `timeout`
declared for the claim hook in `plugin-build/hooks/hooks.json`. The two `ps`
calls are the process-table read and one shared start-time pass; the two lock
waits are the claims lock then the breaker lock; the worktree term counts once
because `create_worktree` runs one of its two git calls, never both.
`TestTimeoutBudget` in `tools/session-claim/tests/test_claim_lib.py` enforces the
sum. If the harness kills the hook mid-git, there is no verdict and the write is
allowed: the guard fails open exactly when contention is real.

### Claim coverage is file-writing tools, not shell writes: the accepted gap

Defends: `tools/session-claim/claude-session-claim`

The claim hook covers tools that name a file (`file_path` or `notebook_path`:
`Edit`, `Write`, `NotebookEdit`). A file written by `bash: echo > file` records
no claim and never will. Closing that needs a second shell parser inside the
hook, and its false-positive surface costs more than the hole is worth. The
coverage claim is about tools, not about writes.

### The claude-process walk is duplicated on purpose

Defends: `tools/session-claim/identity_lib.py`, `tools/caffeinate/caffeinate_lib.py`

The walk up the process table to the `claude` process exists in `session-claim`
and in `caffeinate`, on purpose: sharing it would put a shared dependency inside the guard, for a few lines of
walk. The cost: a change in how Claude Code appears in the process table needs
fixing in both places, and both docstrings name the other. A third caller is the
trigger to revisit. `ps` reports the path as invoked (`~/.local/bin/claude`), not
the versioned file it links to, and both shapes are fixtured, because a failed
walk is silent.

**Both read `ps comm=` and match an exact basename `claude`. Never go back to
`command=` and splitting argv.** `comm=` is the executable path alone, spaces
intact. The desktop app's binary lives under
`~/Library/Application Support/Claude/claude-code/...`, and splitting argv cut
that at "Application". No desktop session resolved, so session-claim allowed
every edit, silently, on the default surface. Pinned by `TestProcessTableReader`
and `test_finds_the_desktop_apps_claude_under_a_path_with_spaces` in
`tools/session-claim/tests/test_identity_lib.py`.

## Integrity checks

### claude-toolkit-integrity reads settings AND the install dirs

Defends: `tools/toolkit-integrity/toolkit_integrity_lib.py`

- **It reads `~/.claude/settings.json`, the install directories and each enabled
  plugin's hook manifest, and none is a fallback for another.** They overlap on a
  dangling `~/.local/bin` link that a hook names, so the tests assert
  `unrunnable_hooks` and `broken_links` directly, and the hook test asserts the
  `PreToolUse:` label only the hooks section emits.
- **Enumeration alone cannot see a hook whose link was never created**: there is
  no directory entry to find. That is why configured commands are read at all.
- **`root_and_head` has no `if not start` guard, deliberately.** `_git` already
  refuses an empty path, and `git -C ""` runs in the cwd and exits 0. A second
  guard with the same answer would hide a mutation of the one protecting every
  caller.
- **`last_reported` returns `Optional[str]`, and the third state decides.** No
  record is a first run (record a baseline, print nothing); a different record is
  drift. A boolean cannot carry that.
- **The test sandbox spells its directories out literally** rather than reading
  `LINK_DIRS`, so narrowing the constant fails the mutation, not the fixture.

### Stateful-guard canaries run on a throwaway fixture at SessionStart

Defends: `tools/toolkit-integrity/toolkit_integrity_lib.py` (`stateful_canary_problems`)

- **SessionStart, not an on-demand flag.** A check nobody remembers to run
  reports nothing. Presence is not protection: the danger and model guards are
  probed with fixed deny and allow payloads, and the worktree guard,
  distill-write guard and session-claim against a temporary git fixture removed
  afterwards.
- **The fixture keeps your global git hooks out** with a local `core.hooksPath`
  pointing at an empty directory.
- **session-claim keeps its real identity but gets sandboxed state.** Its
  `CLAIM_*_PATH` variables point into the fixture, so a canary block cannot count
  against the live session's breaker. Identity stays real on purpose: when it
  fails, the guard allows every edit, which is the failure most worth catching.
- **Escape hatches are stripped for the probe** (`DANGER_ALLOW`,
  `MODEL_GUARD_ALLOW`, `WORKTREE_ALLOW`, `WT_ALLOW_MAIN`, `CLAIM_ALLOW`), so a
  canary measures the guard, not a choice made in your shell.

### Hook-fires audit: transcripts against heartbeat records

Defends: `lib/hook_heartbeat.py` (`audit`), `tools/toolkit-integrity/toolkit_integrity_lib.py` (`fires_audit`)

- **Transcripts are the independent witness.** Every other detector is itself a
  hook or calls a guard directly, so none notices the harness no longer calling
  hooks. A transcript records which shell commands actually ran; one with no
  `<session>.fired` heartbeat ran unguarded. Refused calls never count.
- **A 14-day window, measured by session start, under the writer's 30-day
  prune**, so every eligible session still has its evidence. A test pins 14 below
  the prune age.
- **The era is immutable.** `first_installed_at` is stamped once by the install
  record, so a repair install cannot move the window past an earlier outage.
- **Never silent about itself.** An unreadable transcript, one too large to finish
  in the SessionStart share, and a crash of the audit each produce a notice. The
  `Guards:` line never changes `--check`'s exit status, and a reported session is
  not reported again for 30 days.

## Continuity

### The handover sweep's two grace gates

Defends: `tools/thread-handover/index_lib.py`, `tools/thread-handover/claude-thread-deliver`

The sweep retires a consumed handover only when both gates clear:
`thread_handover.sweep_grace_sessions` (default 3) distinct other sessions have
started, **and** `thread_handover.sweep_grace_days` (default 7) days have passed
since it was attached. Do not collapse them to one.

- **Why two.** SessionStart fires per window and per `/clear`, so with several
  windows open the session count is spent within minutes. Count alone would
  retire a document while the session that attached it is still working from it.
  In practice the day floor decides; keep the session gate anyway, and when you
  quote a deadline, quote the days.
- **Failure bias.** `_days_elapsed_since` and `_grace_cleared` fail toward keeping
  the file, including for a missing, unparseable or future `consumed_at`.
- **The compiled default stays 7.** Shortening it is a config choice. A test run
  with no config pins 3 and 7, and `lib/setup_config.py` never prompts for either
  key. The day gate is exact wall clock, so 3 means 72 hours.
- **Do not set either key to `0` to disable the sweep; use `enabled: false`.**
  `sweep_grace_days: 0` drops the day gate. `sweep_grace_sessions: 0` is
  unpredictable: depending on whether a record already carries a witness list, it
  is kept forever or archived at the day floor. A fresh test record survives,
  which is how "0 means off" looks verified when it is not.

### The handover sweep archives rather than deletes

Defends: `tools/thread-handover/index_lib.py`

A retired handover moves to `<handover_dir>/archive/handovers/` with its
filename kept; it is never deleted. Carrying an unresolved item from one
handover to the next is discretionary and nothing verifies it, so deleting the
predecessor on a clock keyed to attachment rather than resolution made a dropped
item unrecoverable on a timer. The `/handover` skill asks a successor to account
for every inherited item; archiving is the backstop when that step is skipped.

- **`archive_dir()` is a function, not a module constant.** `HANDOVER_DIR` can be
  rebound (the tests do it); a constant computed at import would file into a
  stale directory while reporting success.
- **A collision suffix goes before `-handover.md`**, not after `.md`, so the
  archived name still matches `HANDOVER_NAME_RE`, the pattern that makes
  `spec-archive` refuse the file.
- **Do not give `spec-archive` the same grace gates for symmetry**; see below.

### spec-archive triggers on a declared status:

Defends: `tools/spec-archive/archive_lib.py` (`classify`)

`spec-archive` archives on a declared `status:` (`shipped` or `superseded`), once
the file is older than `min_age_days`. The age is a floor after the declaration,
never the trigger. Provenance (a spec named in a merged PR) misses specs that
never ship a PR and misfires on live work that happens to be named in one; age
alone files work that is live but idle. `status:` is written by the spec's
author at write time, when it is cheap and certain.

### Front matter and .spec-status.json are non-overlapping

Defends: `tools/spec-archive/archive_lib.py`

A `.md` path never consults `.spec-status.json`; a non-`.md` path (a directory, a
`.json`, a `.py`) never consults front matter and declares its status in
`<specs_dir>/.spec-status.json`, keyed by basename. Two mechanisms that can
produce the same verdict on the same input hide each other's failures. Splitting
the source by file type means a broken manifest reader can never quietly cover
for broken front-matter parsing, or the reverse.

### spec-archive gets no grace period

Defends: `tools/spec-archive/archive_lib.py`, `tools/spec-archive/claude-spec-archive`

Both `spec-archive` and the handover sweep move files rather than delete them,
and both run from SessionStart. `spec-archive` gets no grace gates; these are the
substitutes:

- **The trigger is a declared `status:`** a human wrote. Nothing acts on a file
  its author has not marked done.
- **An age floor.** `min_age_days` keeps a freshly declared spec in place, and a
  spec flipped in a run is not archived in that same run.
- **A lock.** `<specs_dir>/.spec-archive.lock`, one non-blocking attempt; a
  session that does not get it exits silently.
- **Move-only recovery.** A flip is undone by setting `status: live` back; an
  archived file comes back with `mv`. A move never leaves `specs_dir` and refuses
  to overwrite.

Why that is enough here: a reader of a moved spec fails loudly with "file not
found", while a session mid-way through a vanished handover gets nothing. And
**silence is not consent**: a missing or unknown status, or unparseable front
matter, is reported and left alone.

### spec-archive's stale flip

Defends: `tools/spec-archive/archive_lib.py`

- **The flip is off by default; auto-archive still runs.**
  `spec_archive.stale_flip_days` absent or `0` means a `status: live` file is never
  touched. The absent case is the strict one because the flip rewrites a status
  line you never asked to have rewritten.
- **A "Waiting on a human" section is `open` unless it proves itself `clear`,
  and `absent` is a third state, not a pass.** A table with no `Status` column,
  a header-only table, or any row whose status is not a recognised closed word
  reads `open`. A file with a `Backlog` or `MVP` heading does not flip when the
  section is absent.
- **Unfinished work blocks the flip**: an unticked task box, or an open row in a
  `Backlog` table.
- **Only mtime is a clock.** The flip adds no timestamp or state file; its own
  write resets the mtime, so a freshly flipped file sits the ordinary
  `min_age_days` floor before archive will consider it.

## git tidy

### git tidy reports no-upstream branches, never deletes them

Defends: `tools/git-tidy/git-tidy`, `tools/git-tidy/worktree_lib.py`

`git tidy` deletes a `[gone]` branch only with positive merge evidence: its tip is
in `origin/<default>`, or equals or is an ancestor of a merged pull request's
head. A remote branch also vanishes when a PR closes unmerged, and "upstream gone,
so finished" once deleted an unpublished commit and exited 0. A branch with **no
upstream configured** carries no signal at all (`%(upstream:track)` is empty,
never `[gone]`), and `git worktree add -b` creates exactly that. Nothing local
separates "squash-merged and done" from "never pushed, still being written", so
it is reported, never deleted. Wrong in the delete direction is unrecoverable;
wrong in the report direction costs one line.

### --prune-worktrees and proven-merged [gone] branches are the only things git tidy deletes

Defends: `tools/git-tidy/git-tidy`, `tools/git-tidy/worktree_lib.py`

With `--prune-worktrees`, `git tidy` also removes worktrees whose content is
identical to a merged pull request's head, and nothing else.

- **`removable()` is one function called by the printer and the pruner**, so the
  report cannot reassure you about one set while the flag deletes another.
- **A clean worktree with no merged PR is excluded**: that state also describes a
  worktree created for work that has not had its first edit yet.
- **`--force` is never passed.** Git's refusal to remove a worktree with modified
  or untracked files is the backstop for the classifier being wrong.
- **Ignored files are checked first**, because `git worktree remove` deletes them
  without `--force`. Only `__pycache__/`, `.pytest_cache/` and `*.pyc` count as
  disposable.
- **Claim liveness is three-state, and `unknown` never prunes.**
- **An unknown option is refused, not ignored**: a misspelled prune flag doing
  nothing looks exactly like a run with nothing to remove.

### git tidy updating itself mid-run is not a bug: don't wrap it in main(){}

Defends: `tools/git-tidy/git-tidy`

Run from the primary checkout, `git tidy`'s own `git merge --ff-only` can replace
`tools/git-tidy/git-tidy` while bash is executing it. That is harmless: git
writes a new file and swaps it in, so bash keeps reading the old inode to the
end, and a `main() {...}; main "$@"` wrapper would change nothing. The run that
pulls an update finishes the old script and hands its arguments to the new
`worktree_report.py`; the next run is consistent.

## Installation

### Hooks, skills and agents ship in the profile plugins

Defends: `install.sh`, `claude/legacy-hook-commands.json`, `claude/profiles.json`

- **The checkout is a local directory marketplace.** The profile plugins
  (`toolkit-core`, `toolkit-continuity`, `toolkit-build`, `toolkit-maintainer`,
  `toolkit-mac`) load from the `plugin*/` directories, registered by `install.sh`
  through the `claude` CLI. A guard runs only where its plugin is enabled.
- **Hook commands use shell form with a quoted `"${CLAUDE_PLUGIN_ROOT}"`**, which
  survives a checkout path with spaces.
- **Owned entries only.** `install.sh` removes from `settings.json` exactly the
  strings in `claude/legacy-hook-commands.json`, never by pattern. A hook of
  yours that merely looks similar survives.
- **Nothing is linked into `~/.claude/{commands,agents,skills,build-lens}`.** An
  old link is removed by its resolved target, with component containment, never
  by name, so a link of yours that shares a name survives. A bare-name clash
  with a third-party plugin is a warning, because it is the user's call. A
  personal copy, two toolkit profiles shipping one name, or an incomplete
  inventory still fails `./install.sh --check`, because the runtime picks one
  copy silently.
- **Agents dispatch as `toolkit-build:<name>`**, and skills are namespaced by
  their plugin.
- **`./install.sh --check` fails on** a plugin not installed and enabled, a
  marketplace that is not this checkout, `disableAllHooks`, legacy duplicates in
  `settings.json`, missing or non-executable hook paths, and zero or incomplete
  test suites. The toolkit never sets `disableAllHooks` itself.
- **The pre-install snapshot** is `settings.json.pre-claude-code-toolkit.bak`,
  written once and never overwritten. Running sessions keep the old wiring until
  `/reload-plugins` or a new session.

### Profiles: one registry, core always installed

Defends: `claude/profiles.json`, `lib/profiles.py`

One profile is one plugin, listed in `claude/profiles.json`. `core` and
`continuity` install by default; `builder`, `maintainer` and `mac` are opt in.
`core` is always installed, and every `requires` is added with the profile that
needs it. A `profiles` list in your config chooses the set; a broken registry or
config stops the install rather than falling back to defaults. A plugin directory
must be named `plugin` or `plugin-<name>`, so the integrity hook watches its hook
manifest.

### `EXECUTABLE_NAMES` is a hand-kept inventory, and a test is its only check

Defends: `install.sh` (`EXECUTABLE_NAMES`)

`EXECUTABLE_NAMES` lists every executable `install.sh` links into
`~/.local/bin`. Nothing reads it at run time: the stale-link prunes walk the
directories, deliberately, because an exclusion list someone must remember to
update deletes a live tool's link when it drifts. So
`TestExecutableNamesCoversEveryLink` is the only thing keeping the array honest,
and it checks both directions. Do not delete that test as hygiene on the grounds
that the array is unread, and do not make a prune iterate the array.

### The load guard denies, and its lock rides on the job itself

Defends: `tools/load-guard/claude-load-guard`, `tools/load-guard/claude-heavy-run`

- **Deny, not ask.** A bare heavy command is refused with the exact
  `claude-heavy-run ...` rewrite. An ask puts a prompt in front of an unattended
  run, which nobody can answer; a deny lets Claude correct itself.
- **exec with an inherited fd, not a supervisor.** The wrapper takes the `flock`,
  then `exec`s the job in the same process with the lock fd inheritable. The lock
  lives exactly as long as the job, a crash leaves nothing stale, and there is no
  signal forwarding to get wrong. Making the fd non-inheritable breaks this
  silently; the survives-exec test pins it.
- **Unreadable memory fails strict in the wrapper and open in the hook.** The
  floor is a safety value, so the wrapper refuses to run on a reading it cannot
  trust; the hook does not read memory and fails open on anything it cannot parse.

### Install record and ownership-aware uninstall

Defends: `lib/install_record.py`, `lib/uninstall.py`

- **Ownership comes from the install record** (`~/.config/claude-code-toolkit/
  install-record.json`): the permission rules the installer added, the
  `defaultMode` it replaced, the `claudeMdExcludes` pattern it added. Everything
  else is proven from its live value at uninstall time (the status line, the
  links, the import line, the hooks path); a second source of truth would drift.
- **Intent is recorded before the settings write.** A recorded rule that never
  landed is harmless, since uninstall removes only rules that are present; the
  reverse order loses ownership for good if a crash falls between the writes.
- **No backfill.** A machine installed before the record existed keeps every
  permission rule on uninstall, and is told ownership is unrecorded. An
  unreadable record is treated the same way.
- **Blockers refuse the whole run.** With any blocker, nothing changes and each
  remediation is printed; a partial apply would leave the machine neither
  installed nor clean.
- **The CLI is not trusted to protect you.** Uninstall checks plugin dependencies
  itself, removes dependents first, stops at the first failure, and re-lists
  plugins to confirm each removal before touching the marketplace.

### Update, version and rollback

Defends: `lib/toolkit_update.py`, `install.sh`, `scripts/check_layout.py`

- **The version is the checkout commit.** Plugins and links load straight from
  the checkout, so `./install.sh --version` prints the commit, its date and
  subject, and whether the checkout is on the default branch, another branch or
  pinned.
- **No plugin sets `version`** (`scripts/check_layout.py` refuses one). A GitHub
  marketplace install caches a copy keyed by that string and takes no update
  until it changes, so a forgotten bump strands those users silently. Without
  one, Claude Code uses the commit, the same version a checkout has. The cost is
  a commit SHA in `/plugin` where a release number would be. `VERSION` stays as
  the release tag the PR-review Action template pins, and nothing else.
- **`--update` fast-forwards, then runs the new installer.** It refuses before
  contacting origin unless origin is the toolkit's public repository or the one
  `update.origin` names (for a fork), because the installer runs whatever code an
  update brings. It records a rollback point before anything moves.
- **A rollback is a detached pin.** A reset default branch would be pulled forward
  on the next session, undoing the rollback unasked. `--rollback` refuses with
  local changes or from a feature branch, restores the configuration snapshot
  recorded with the rollback point, and removes plugins the older version did
  not have. `repo-freshness` names a pinned checkout every session, with the
  command that returns it.
- **No auto-rollback.** A failed update is reported with the recovery command.

### There is no build-route hook; the build skill's description routes

Defends: `tools/build-route/claude-build-route`, `tools/build-route/trigger-cases.json`

Build-shaped prompts reach `toolkit-build:build` from the skill's description
alone. `tools/build-route/trigger-cases.json` holds the prompts that pin that,
with positive and negative controls; run them with `claude-skill-trigger`.
`claude-build-route` is a `--review-mode` command only. The re-open condition for
a routing hook is a committed case that fails.

## /build

### /build's economical gates run one blinded reviewer

Defends: `tools/build-lens/blinded-review.js`, `tools/build-lens/review_lib.py`, `tools/build-lens/claude-review-gate`, `skills/builder/build/SKILL.md`

Each of `/build`'s gates (design, plan, code, merge) dispatches **one** reviewer.
Under `build.review: "economical"` (the default) that is one Opus reviewer at
`max`, run through `tools/build-lens/blinded-review.js` under `Workflow`; under
`"full-power"` it is Codex. A per-lens fan-out with a separate refutation stage
was tried and removed; do not restore it. How much one reviewer misses compared
with several is unmeasured, and the build skill says so.

- **Where `Workflow` is not available, the gate runs through the Agent tool**
  (`claude-review-gate`, `review_lib.py`). That is only the same review if it
  shares the prompt and the acceptance rule: the tests compare the two prompts and
  hold both report checks to `tools/build-lens/tests/semantic_cases.json`, so
  change the JavaScript and Python copies together. The Agent call may take no
  `effort` argument, so every gate gets `max` from its agent's frontmatter:
  `agents/code-reviewer.md` at code and merge, `agents/spec-reviewer.md` at
  design and plan (row 102, 2026-11-21; before it, the design and plan fallback
  was a plain agent below `max`, and its cost landed in the ledger's `other`
  lane).
- **Do not trim the reviewer prompt or the lens tables.** An Opus reviewer reading
  an Opus plan shares the author's priors. Three things stand in for a second
  model: the reviewer is blinded to the author's rationale, it is told the
  artifact is broken and asked where, and it runs at higher effort than the
  author. Drop one and the gate becomes an agreement machine that reads exactly
  like a clean review. For the same reason, do not run your own session at
  `/effort max`.
- **A report that describes a review that never happened is refused**: `ran:
  true` with no or blank commands, `ran: false` with no findings or a blank
  `why_not`, `APPROVE` alongside a `BLOCKING` finding.
- **There is no refuter, so you adjudicate.** Check every finding against the
  repo; record a refuted one as refuted rather than deleting it. Full-power Codex
  runs at `model_reasoning_effort="high"`, set by `claude-codex-review`; a hand-run
  `codex` that omits it reasons at a lower default.
- **If a gate feels too expensive, split the build.** Measure your own gate cost
  with `claude-build-cost`; `docs/build-measurements.md` gives the method.
