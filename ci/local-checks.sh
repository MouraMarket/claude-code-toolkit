#!/usr/bin/env bash
#
# Local CI: every check this repo's PRs must pass, in one script. There is no
# Actions workflow behind it (`tests.yml` and `context-lint.yml` are deleted).
# `claude-local-ci` (tools/local-ci) runs this as a subprocess in a merged
# worktree, reads its final "<N> checks run" line and records the result in a
# receipt (posted to GitHub only with `--post`); a human can also just run it
# from the repo root.
#
# Run from the repo root. Prints exactly one `PASS <name>` or
# `FAIL <name>: <reason>` line per check below, in order, NEVER stopping at the
# first failure, then exactly one `<N> checks run` line. Exits 0 only if all
# seven checks PASS; 1 otherwise.
#
# Exception: a run the ENVIRONMENT broke ends on one
# `local-checks: environment: <reason>` line instead, exit non-zero, never 0
# (see environment() below). Interrupted (INT/TERM): no count line, exit
# 128+signal. Disk full: the full contract, then that line, exit 2. No run
# directory: that line only, exit 2.
#
#   suites (3.9)    suites (3.13)    secret scan    installer    context lint
#   skill bundles    ci self-test    (all seven run at once; see the self-test's note below)
#
# ---------------------------------------------------------------------------
# Why this exists (moved here from tests.yml's own header; that file is gone):
#
# Hermetic CI. Before the suites check existed, the test suites ran only via
# `./install.sh` on one person's machine. A PR could land with a red suite and
# nobody would know until the next install — which, for a repo whose whole job
# is safety hooks, is the wrong time to find out.
#
# The two standards in CLAUDE.md — "tests must be hermetic" and "they must
# pass with no config at all" — are not separately enforced here because the
# disposable HOME below *is* that case. A fresh machine has no
# ~/.config/claude-code-toolkit/config.json, no roster, no workspace. Any
# suite that quietly depended on a real machine's config or state fails here
# and nowhere else. That is the point.
#
# Trap, if you ever "simplify" this away: do NOT build the disposable HOME
# with a bare `mktemp -d`. `/tmp`, `/private/tmp` and `/var/folders` are
# SAFE_ROOTS in claude-danger-guard, so a temp-dir HOME makes every path under
# `~` trusted and several of its BLOCK cases invert to ALLOW — suites that
# exercise the guard look broken and are not. The disposable HOME below is
# rooted under the REAL HOME's own cache directory instead, specifically to
# land outside those roots.
#
# No path filter gates any of this (the deleted `tests.yml` had none,
# deliberately): a credential or a suite that landed outside whatever path
# list someone wrote would get no CI at all until it was already in history.
# The whole run costs well under what that is worth.
#
# `ran == discovered`, not just `ran > 0`. The suites loop's stdin IS the
# queue of remaining suites (`done < <(find ...)` below), so a suite that
# reads stdin — a hook test calling its own drain in-process, say — eats the
# rest of the queue. The loop then hits EOF having printed a pass for every
# suite it reached, and a bare `ran -eq 0` check cannot see that a partial
# sweep happened — a truncated queue just reports a smaller number. Comparing
# against what `find` actually discovered is the only thing that catches it.
# `</dev/null` on the suite invocation is what prevents the truncation in the
# first place; the comparison is the backstop in case that redirect is ever
# lost.
#
# Concurrency. All seven checks below start together, the first six
# each under its own disposable HOME beneath one CI_ROOT, and each writes to its own
# output file instead of echoing straight to the terminal — concurrent
# writers interleaving mid-line would otherwise corrupt the PASS/FAIL
# contract. The main script `wait`s on every PID, then prints each output
# file in the FIXED order below, so the contract (one line per check, in
# order, then the count) looks identical to a caller whether the checks ran
# serially or not. `set -m` gives each backgrounded job its own process
# group, so the EXIT/INT/TERM trap can kill a check's whole subtree — not
# just the wrapper function, but a nested `python3` suite process too — with
# one `kill` aimed at the negative PID.
#
# Suites in parallel. Within each suites check, up to
# SUITE_JOBS suites run at once. Run one after another, a check's suites
# dominate the wall time while most cores sit idle.
# The loop still reads the queue on stdin and counts what it STARTED
# (`ran == discovered` is unchanged). Each suite gets its own HOME under the
# check's HOME, because concurrent suites sharing one HOME could collide.
# Results come back through per-suite files, read in sorted order, and a
# suite with no exit-status file FAILS.
#
# `ci self-test` (B1) runs the repo's own `ci/tests` suites, i.e. this script's
# own harness, which no other check reaches (they only find tools/lib/skills/evals/scripts).
# It starts WITH the other six. It ran after them until
# its harness lost its last wall-time assertion; serially it added its whole
# run time to every run. It uses the REAL HOME because the nested
# local-checks.sh runs it spawns resolve interpreters through uv's registry
# under the real HOME. Nesting guard: the harness's
# fixtures carry `ci/tests`, so every fixture run would start the harness again.
# The check exports CLAUDE_LOCAL_CI_NESTED=1 into its suites' environment only,
# and skips itself (PASS) when that variable is already set. `claude-local-ci`
# scrubs the variable before launching this script, so a stray export in a
# caller's shell can never make a real run skip it.
#
# The installer check runs with CLAUDE_INSTALL_SKIP_SUITES=1: install.sh
# would otherwise re-run the entire suite sweep a third time internally,
# against yet another HOME, which is both wasted wall time and a second,
# uncoordinated source of truth for whether the suites passed.
# ---------------------------------------------------------------------------

# --- Clean environment: re-exec under `env -i` with an allowlist -----------
# The Actions runner this replaces started from a clean environment; a
# developer's shell does not. An inherited PYTHONPATH, PYTHONOPTIMIZE or
# PIP_* faked a green run before, so an allowlist closes the class rather than
# each instance. Add a variable here only when a check genuinely needs it.
# The fuller explanation is in automations' ci/local-checks.sh.
# CLAUDE_LOCAL_CI_NESTED is the nesting guard (see `ci self-test` above);
# without it every nested run would start the harness again.
# Clean environment, unconditionally, before any other command runs. The FIRST
# start re-execs under `/usr/bin/env -i` with an allowlist, as `bash -p`, and
# inspects nothing, so nothing inherited (GREP_OPTIONS emptying a grep, an
# exported function shadowing a test runner) can sway the decision.
#
# Privileged mode is the guard. It is read from `$-`, which no environment
# variable can set, and under -p bash imports no functions and skips BASH_ENV,
# so the builtins the SECOND start calls are the real ones. The guard used to
# be an environment variable (LOCAL_CI_REEXEC): forgeable, and with it an
# exported compgen() -- or builtin(), which defeats `builtin compgen` -- made
# the check below see nothing, and every check PASSed.
#
# If `exec` is itself an inherited function and returns, the `:?` line aborts:
# an assignment and a keyword, which no function can shadow. ($((1/0)) does
# not abort bash 3.2, measured.) On the second start anything unexpected exits
# 2 loudly: an error status, never a false green.
#
# Not covered, and no script can cover it: code the caller hands bash to run
# BEFORE this line -- a BASH_ENV file, or an exported exec() that does not
# return. That is the caller running their own program, not inherited state.
if [[ $- != *p* ]]; then
  exec /usr/bin/env -i \
    HOME="$HOME" PATH="$PATH" USER="${USER:-}" LOGNAME="${LOGNAME:-}" \
    LANG="${LANG:-en_US.UTF-8}" TMPDIR="${TMPDIR:-/tmp}" TERM="${TERM:-dumb}" \
    ${CLAUDE_LOCAL_CI_NESTED:+CLAUDE_LOCAL_CI_NESTED="$CLAUDE_LOCAL_CI_NESTED"} \
    "$BASH" -p "$0" "$@"
  _lc_null=""
  [[ ${_lc_null:?local-checks: the clean re-exec did not happen (exec shadowed by an inherited function?)} ]]
fi
_lc_dirty=""
while IFS= read -r _lc_v; do
  case "$_lc_v" in
    HOME|PATH|USER|LOGNAME|LANG|TMPDIR|TERM|PWD|OLDPWD|SHLVL|_|CLAUDE_LOCAL_CI_NESTED) ;;
    *) _lc_dirty="$_lc_dirty $_lc_v" ;;
  esac
done < <(compgen -e)
if [[ -n "$(compgen -A function)" ]]; then
  _lc_dirty="$_lc_dirty functions:$(compgen -A function)"
fi
if [[ -n "$_lc_dirty" ]]; then
  echo "local-checks: environment not clean after re-exec:$_lc_dirty" >&2
  exit 2
fi
unset _lc_dirty _lc_v

set -uo pipefail

set -m

TOTAL=7

# Suites run at once inside EACH suites check (two checks, so twice this in
# flight). See "Suites in parallel" in the header for why. To size it for your
# machine, time one run at each value on an otherwise idle machine and keep the
# smallest value past which the wall time stops falling.
SUITE_JOBS=4

pass() {
  echo "PASS $1"
}

fail() {
  echo "FAIL $1: $2"
}

# The run could not produce a verdict, so it ends on this line instead of the
# count's verdict: interrupted, out of disk, or no run directory. Always the
# LAST line printed; claude-local-ci records it as `error`, not `failure`.
environment() {
  echo "local-checks: environment: $1"
}

# PIDs of the backgrounded checks, filled in as each is started below, and
# the run directory, set once it exists. Both start empty because the traps
# below go in BEFORE any slow work: a signal while interpreters resolve must
# end on the environment line too (Code Review finding, rows 11/13).
PIDS=()
CI_ROOT=""

# On EXIT, INT or TERM: kill every check still running, whole process group
# (nested `python3` suite children included — `set -m` above is what gives
# each backgrounded job its own group, so `-$pid` reaches all of it), then
# remove CI_ROOT. No orphan may outlive the script.
#
# TERM first, then a bounded grace period, then KILL. The grace period exists
# because the seventh check runs ci/tests, which launches NESTED local-checks.sh
# processes: each nested script's `set -m` puts ITS checks in new process
# groups this script never registered, and only the nested script's own trap
# can reach them. KILLing it straight after TERM would stop that trap running
# and leave its suites alive. The caller (`claude-local-ci`) must wait longer
# than this grace before it kills this shell, or the KILL below never runs;
# its CHILD_EXIT_TIMEOUT_S must exceed CLEANUP_GRACE_POLLS x 0.2 s.
CLEANUP_GRACE_POLLS=25
cleanup() {
  local pid alive waited
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill -TERM -- "-$pid" 2>/dev/null
  done
  for (( waited = 1; waited <= CLEANUP_GRACE_POLLS; waited++ )); do
    alive=0
    for pid in "${PIDS[@]:-}"; do
      [[ -n "$pid" ]] || continue
      kill -0 -- "-$pid" 2>/dev/null && alive=1
    done
    (( alive )) || break
    sleep 0.2
  done
  for pid in "${PIDS[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill -KILL -- "-$pid" 2>/dev/null
  done
  [[ -n "$CI_ROOT" ]] && rm -rf "$CI_ROOT"
}

# INT/TERM must END the script. A handler that only cleans up returns into
# the `wait` loop below, which then `cat`s the out files cleanup just
# deleted, prints `7 checks run` and exits 1: a red for a run nobody finished
# Further INT/TERM are ignored while cleanup
# runs, so a second signal cannot cut its grace period short; the caller's
# KILL still can.
on_signal() {
  trap '' INT TERM
  trap - EXIT
  cleanup
  environment "interrupted by SIG$1"
  exit $((128 + $2))
}
trap cleanup EXIT
trap 'on_signal INT 2' INT
trap 'on_signal TERM 15' TERM

# --- Rule 1: resolve interpreters FIRST, with the real HOME --------------
#
# Before the disposable HOME exists, so that `uv python find` (which reads
# uv's own registry under the real $HOME) and `command -v` are answered by
# the machine this actually runs on, not by a sandbox that has neither.
#
# Reported through stdout rather than a global set inside a function, because
# every caller captures the result with `$(...)`, which forks a subshell —
# any variable assigned inside that subshell is invisible to the parent.

resolve_interpreter_path() {
  local v="$1"
  if command -v uv >/dev/null 2>&1; then
    # --system: bare `uv python find` walks up to the repo's own .venv, whose
    # dev packages would leak into the 3.9 checks.
    uv python find --system "$v" 2>/dev/null
  else
    command -v "python$v" 2>/dev/null
  fi
}

# check_interpreter V -> "OK <path>" or "ERR <reason>" on stdout.
check_interpreter() {
  local v="$1" py got
  py="$(resolve_interpreter_path "$v")"
  if [[ -z "$py" ]]; then
    echo "ERR no python$v on this machine (uv python find / command -v both failed)"
    return
  fi
  if [[ ! -x "$py" ]]; then
    echo "ERR $py is not executable"
    return
  fi
  got="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
  if [[ -z "$got" ]]; then
    echo "ERR $py did not report a version"
    return
  fi
  if [[ "$got" != "$v" ]]; then
    echo "ERR $py reports $got, not $v"
    return
  fi
  echo "OK $py"
}

PY_39="" ; PY_39_OK=0 ; PY_39_REASON=""
result="$(check_interpreter 3.9)"
if [[ "$result" == OK\ * ]]; then
  PY_39="${result#OK }"
  PY_39_OK=1
else
  PY_39_REASON="${result#ERR }"
fi

PY_313="" ; PY_313_OK=0 ; PY_313_REASON=""
result="$(check_interpreter 3.13)"
if [[ "$result" == OK\ * ]]; then
  PY_313="${result#OK }"
  PY_313_OK=1
else
  PY_313_REASON="${result#ERR }"
fi

# Rule 5: zsh not on PATH fails both suites checks. danger-guard's tokenizer
# test shells out to the real zsh binary as its oracle, so both suites need it
# regardless of Python. (claude-worktree-guard is Python since W3, #208.)
ZSH_OK=0
command -v zsh >/dev/null 2>&1 && ZSH_OK=1

# --- Rule 2: hermetic config -----------------------------------------------
export CLAUDE_TOOLKIT_CONFIG=/nonexistent.json
# PYTHON* and every other inherited variable are already gone: see the
# clean-environment re-exec at the top.

# --- Rule 3: one root for every check's own disposable HOME ---------
# Each check below gets its OWN HOME under this root (rule 1), set just
# before it starts — never here, since this script's own HOME must stay the
# real one while it is still resolving interpreters and building CI_ROOT
# itself.
REAL_HOME="$HOME"
# No CI_ROOT means nothing below can run; carrying on with an empty one
# reported every check as a FAIL.
# Captured apart from CI_ROOT: on failure it holds mktemp's error text, and
# cleanup must never be handed that to `rm -rf`.
if ! _lc_root="$(mkdir -p "$REAL_HOME/.cache/claude-local-ci" 2>&1 &&
                 mktemp -d "$REAL_HOME/.cache/claude-local-ci/run.XXXXXX" 2>&1)"; then
  environment "cannot create the run directory: $(printf '%s\n' "$_lc_root" | tail -n 1)"
  exit 2
fi
CI_ROOT="$_lc_root"


# run_suites_check <label> <version> <ok> <reason> <py>
#
# Mirrors the deleted tests.yml's `suites` job loop exactly, with two additions: a
# per-pass PATH shim (rule 4) so a `#!/usr/bin/env python3` child resolves to
# the SAME version as the suite that spawned it, and HOME is already this
# check's own disposable one (set by the caller before backgrounding it).
run_suites_check() {
  local label="$1" v="$2" ok="$3" reason="$4" py="$5"

  if (( ! ok )); then
    fail "$label" "$reason"
    return
  fi
  if (( ! ZSH_OK )); then
    fail "$label" "zsh not found on PATH"
    return
  fi

  local shim="$HOME/bin-$v"
  mkdir -p "$shim"
  ln -sf "$py" "$shim/python3"

  # Up to SUITE_JOBS suites at once (see the header). This runs in start_bg's
  # own subshell; `set +m` keeps every suite in this check's process group, so
  # cleanup's group kill reaches them, and stops job-done notices landing in
  # this check's output file.
  set +m
  local results="$HOME/suite-results"
  mkdir -p "$results"
  local ran=0 failed=0 fail_list="" diag_out="" suite base label2 n
  while IFS= read -r suite; do
    base="$(dirname "$(dirname "$suite")")"
    ran=$((ran + 1))
    echo "$suite" > "$results/$ran.name"
    while (( $(jobs -pr | wc -l) >= SUITE_JOBS )); do
      sleep 0.2
    done
    mkdir -p "$HOME/s$ran"
    # </dev/null is load-bearing — see the header comment block above. It
    # must stay on this SAME physical line: lib/tests/test_install_checks_
    # every_suite.py asserts the redirect is present on the line it matches
    # the runner invocation on, not merely somewhere nearby. Each suite gets
    # its own HOME (concurrent suites must not share one) and its own output
    # file, so a failing suite's traceback survives to be printed below. The
    # `.rc` file is written only when the suite returns: a missing one below
    # means the suite never finished, which is a FAIL, never a pass.
    # PYTHONUNBUFFERED: a test's print() is stdout, block-buffered into the
    # file and flushed at exit, so it landed AFTER unittest's stderr summary
    # and the tail read like a passing test.
    ( cd "$base" && HOME="$HOME/s$ran" PATH="$shim:$PATH" PYTHONUNBUFFERED=1 python3 "tests/$(basename "$suite")" >"$results/$ran.log" 2>&1 </dev/null; echo "$?" > "$results/$ran.rc" ) &
  done < <(find tools lib skills evals scripts -path '*/tests/test_*.py' -type f | sort)
  wait

  if [ "$ran" -eq 0 ]; then
    fail "$label" "no suites found — the find pattern or the layout changed"
    return
  fi

  local discovered
  discovered=$(find tools lib skills evals scripts -path '*/tests/test_*.py' -type f | wc -l | tr -d ' ')
  if [ "$ran" -ne "$discovered" ]; then
    fail "$label" "ran $ran suites but found $discovered — the run was truncated"
    return
  fi

  # Read the results back in the sorted order the suites were started in.
  for (( n = 1; n <= ran; n++ )); do
    suite="$(cat "$results/$n.name")"
    base="$(dirname "$(dirname "$suite")")"
    label2="$(basename "$base")/$(basename "$suite")"
    if [[ "$(cat "$results/$n.rc" 2>/dev/null)" != "0" ]]; then
      failed=$((failed + 1))
      fail_list="$fail_list $label2"
      [[ -f "$results/$n.rc" ]] || echo "(no exit status: the suite did not finish)" >> "$results/$n.log"
      diag_out="$diag_out--- $label2 ---
$(cat "$results/$n.log" 2>/dev/null)
"
    fi
  done

  if (( failed > 0 )); then
    printf '%s' "$diag_out"
    fail "$label" "$failed suite(s) failed:$fail_list"
    return
  fi

  pass "$label"
}

# run_cmd_check <label> <command...> — CLAUDE_TOOLKIT_CONFIG is already
# exported above; HOME is this check's own disposable one, set by the
# caller before backgrounding it.
run_cmd_check() {
  local label="$1"; shift
  local out rc reason
  out="$("$@" 2>&1)"
  rc=$?
  if (( rc == 0 )); then
    pass "$label"
    return
  fi
  reason="$(printf '%s\n' "$out" | tail -n 1)"
  [[ -n "$reason" ]] || reason="exit $rc, no output"
  [[ -n "$out" ]] && printf '%s\n' "$out"
  fail "$label" "$reason"
}

# The installer check (rule 4): skip its own internal suite sweep. It
# already ran here, twice, under its own two HOMEs above — a third pass
# inside install.sh would be redundant and would run under yet another HOME,
# with no way to reconcile a disagreement against the two above.
#
# With suites skipped the check runs the installer end to end in this check's
# scratch HOME, and every step names itself on failure (the last line of
# output is what the summary shows):
#   1. ./install.sh exits 0 (links, plugin marketplace add + install).
#      ALLOW_WORKTREE_INSTALL=1 because this HOME is disposable, so
#      repointing its links at a worktree is harmless.
#   2. ./install.sh --check exits 0 (the plugin is installed and enabled).
#   3. Disable every selected plugin (dependents first, so the CLI allows
#      it), then --check must exit 1: proves --check can fail,
#      so step 2's pass means something.
#   4. `claude plugin validate` on the marketplace root and on plugin/ exits 0
#      (manifests the real CLI rejects fail here, not on a teammate's Mac).
# The REAL `claude` CLI runs here, on purpose: this is the one place the shim
# the unit tests use is not. If it is absent the check FAILS, never skips; a
# skipped check would read as a pass.
run_installer_check() {
  export CLAUDE_INSTALL_SKIP_SUITES=1 ALLOW_WORKTREE_INSTALL=1
  run_cmd_check "installer" installer_steps
}

installer_steps() {
  local out rc
  if ! command -v claude >/dev/null 2>&1; then
    echo "installer step 0 (claude CLI): 'claude' not found on PATH; this check needs the real CLI and does not skip"
    return 1
  fi
  out="$(./install.sh </dev/null 2>&1)"; rc=$?
  if (( rc != 0 )); then
    printf '%s\n' "$out"
    echo "installer step 1 (./install.sh): exit $rc, expected 0"
    return 1
  fi
  out="$(./install.sh --check </dev/null 2>&1)"; rc=$?
  if (( rc != 0 )); then
    printf '%s\n' "$out"
    echo "installer step 2 (./install.sh --check after install): exit $rc, expected 0"
    return 1
  fi
  # Every selected profile's plugin, dependents first: the real CLI refuses to
  # disable toolkit-core while toolkit-continuity (or any profile) still requires it.
  local ids id
  ids="$(python3 - <<'PY'
import sys
sys.path.insert(0, "lib")
import profiles
names, error, _ = profiles.selected(".")
if error:
    sys.exit("profiles: " + error)
registry = profiles.load_registry(".")
print("\n".join(profiles.plugin_id(registry[n]) for n in reversed(names)))
PY
)"; rc=$?
  if (( rc != 0 )) || [[ -z "$ids" ]]; then
    printf '%s\n' "$ids"
    echo "installer step 3 (list selected plugins): exit $rc, expected 0 and at least one plugin"
    return 1
  fi
  while IFS= read -r id; do
    out="$(claude plugin disable "$id" 2>&1)"; rc=$?
    if (( rc != 0 )); then
      printf '%s\n' "$out"
      echo "installer step 3 (claude plugin disable $id): exit $rc, expected 0"
      return 1
    fi
  done <<< "$ids"
  out="$(./install.sh --check </dev/null 2>&1)"; rc=$?
  if (( rc != 1 )); then
    printf '%s\n' "$out"
    echo "installer step 3 (./install.sh --check with the plugin disabled): exit $rc, expected 1"
    return 1
  fi
  out="$(claude plugin validate . 2>&1)"; rc=$?
  if (( rc != 0 )); then
    printf '%s\n' "$out"
    echo "installer step 4 (claude plugin validate .): exit $rc, expected 0"
    return 1
  fi
  out="$(claude plugin validate plugin 2>&1)"; rc=$?
  if (( rc != 0 )); then
    printf '%s\n' "$out"
    echo "installer step 4 (claude plugin validate plugin): exit $rc, expected 0"
    return 1
  fi
}

# run_selftest_check — the seventh check; see the `ci self-test` note in the
# header. Runs under the REAL HOME (the caller passes it to start_bg).
run_selftest_check() {
  local label="ci self-test"
  if [[ "${CLAUDE_LOCAL_CI_NESTED:-}" == "1" ]]; then
    echo "ci self-test skipped: nested run (CLAUDE_LOCAL_CI_NESTED=1)"
    pass "$label"
    return
  fi

  local discovered
  discovered=$(find ci -path '*/tests/test_*.py' -type f | wc -l | tr -d ' ')
  if [ "$discovered" -eq 0 ]; then
    fail "$label" "no ci/tests suites found"
    return
  fi
  if (( ! PY_39_OK )); then
    fail "$label" "$PY_39_REASON"
    return
  fi

  # The guard goes into THIS check's environment only (it runs in start_bg's
  # subshell), never the script's own.
  export CLAUDE_LOCAL_CI_NESTED=1
  local shim="$CI_ROOT/bin-selftest"
  mkdir -p "$shim"
  ln -sf "$PY_39" "$shim/python3"

  local ran=0 failed=0 fail_list="" diag_out="" suite label2 diagfile
  diagfile="$CI_ROOT/selftest-diag.log"
  while IFS= read -r suite; do
    label2="ci/$(basename "$suite")"
    ran=$((ran + 1))
    # </dev/null for the same reason as in run_suites_check.
    if ( cd ci && PATH="$shim:$PATH" python3 "tests/$(basename "$suite")" >"$diagfile" 2>&1 </dev/null ); then
      :
    else
      failed=$((failed + 1))
      fail_list="$fail_list $label2"
      diag_out="$diag_out--- $label2 ---
$(cat "$diagfile")
"
    fi
  done < <(find ci -path '*/tests/test_*.py' -type f | sort)

  if [ "$ran" -ne "$discovered" ]; then
    fail "$label" "ran $ran suites but found $discovered — the run was truncated"
    return
  fi
  if (( failed > 0 )); then
    printf '%s' "$diag_out"
    fail "$label" "$failed suite(s) failed:$fail_list"
    return
  fi
  pass "$label"
}

# start_bg <outfile> <home> <command...> — runs <command...> (a shell
# function defined above, inherited by the subshell fork below) with its own
# HOME, in its own process group (rule: `set -m`), capturing all output to
# <outfile> instead of the terminal so seven concurrent writers cannot
# interleave mid-line. Appends the backgrounded PID to PIDS.
start_bg() {
  local outfile="$1" home="$2"; shift 2
  mkdir -p "$home"
  ( export HOME="$home"; "$@" ) > "$outfile" 2>&1 &
  PIDS+=("$!")
}

OUT_39="$CI_ROOT/out-suites-39"
OUT_313="$CI_ROOT/out-suites-313"
OUT_SECRET="$CI_ROOT/out-secret-scan"
OUT_INSTALLER="$CI_ROOT/out-installer"
OUT_LINT="$CI_ROOT/out-context-lint"
OUT_BUNDLES="$CI_ROOT/out-skill-bundles"
OUT_SELFTEST="$CI_ROOT/out-ci-self-test"

start_bg "$OUT_39" "$CI_ROOT/home-suites-39" \
  run_suites_check "suites (3.9)" "3.9" "$PY_39_OK" "$PY_39_REASON" "$PY_39"
start_bg "$OUT_313" "$CI_ROOT/home-suites-313" \
  run_suites_check "suites (3.13)" "3.13" "$PY_313_OK" "$PY_313_REASON" "$PY_313"
start_bg "$OUT_SECRET" "$CI_ROOT/home-secret-scan" \
  run_cmd_check "secret scan" ./tools/secret-scan/claude-secret-scan --all
start_bg "$OUT_INSTALLER" "$CI_ROOT/home-installer" \
  run_installer_check
start_bg "$OUT_LINT" "$CI_ROOT/home-context-lint" \
  run_cmd_check "context lint" ./tools/context-lint/claude-context-lint --check .
start_bg "$OUT_BUNDLES" "$CI_ROOT/home-skill-bundles" \
  run_cmd_check "skill bundles" python3 scripts/check_bundles.py
start_bg "$OUT_SELFTEST" "$REAL_HOME" run_selftest_check

# Wait on each PID individually (not a bare `wait`), so a check that is
# killed by the trap above still accounts for itself rather than vanishing.
for pid in "${PIDS[@]}"; do
  wait "$pid" 2>/dev/null
done

# Print each check's own output, in the FIXED A1 order, regardless of which
# finished first.
cat "$OUT_39"
cat "$OUT_313"
cat "$OUT_SECRET"
cat "$OUT_INSTALLER"
cat "$OUT_LINT"
cat "$OUT_BUNDLES"
cat "$OUT_SELFTEST"

# Each check's verdict comes from its OWN output file's LAST line, which must
# equal exactly "PASS <name>" — never a grep across the concatenated output.
# A failing suite's own stdout can contain a line that merely STARTS WITH
# "PASS " (its own diagnostic, printed above the FAIL line); counting any such
# line inflates the verdict and lets the script exit 0 while FAILs are
# printed. The last line is the contract every check function (pass/fail
# above) actually produces.
check_passed() {
  local outfile="$1" name="$2"
  [[ "$(tail -n 1 "$outfile")" == "PASS $name" ]]
}

PASSED=0
check_passed "$OUT_39" "suites (3.9)" && PASSED=$((PASSED + 1))
check_passed "$OUT_313" "suites (3.13)" && PASSED=$((PASSED + 1))
check_passed "$OUT_SECRET" "secret scan" && PASSED=$((PASSED + 1))
check_passed "$OUT_INSTALLER" "installer" && PASSED=$((PASSED + 1))
check_passed "$OUT_LINT" "context lint" && PASSED=$((PASSED + 1))
check_passed "$OUT_BUNDLES" "skill bundles" && PASSED=$((PASSED + 1))
check_passed "$OUT_SELFTEST" "ci self-test" && PASSED=$((PASSED + 1))

echo "$TOTAL checks run"

if (( PASSED == TOTAL )); then
  exit 0
fi

# A full disk fails whatever happens to be writing, so its reds say nothing
# about the code (four suites, `[Errno 28]` in
# mkdtemp, green on re-run). Read from the symptom, not a free-space
# preflight: the disk can fill mid-run, and a threshold would be a guess.
# Case-sensitive on purpose: it is the OS's own strerror text, and
# thread-handover's tests raise a lowercase look-alike. Still exit 2, never
# 0: a mislabelled real failure stays non-green and reproduces on re-run.
if grep -qsF "No space left on device" "$OUT_39" "$OUT_313" "$OUT_SECRET" \
     "$OUT_INSTALLER" "$OUT_LINT" "$OUT_BUNDLES" "$OUT_SELFTEST"; then
  environment "disk full (No space left on device)"
  exit 2
fi
exit 1
