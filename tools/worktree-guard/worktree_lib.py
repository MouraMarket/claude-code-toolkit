#!/usr/bin/env python3
"""Library core for claude-worktree-guard: context, the bounded git runner,
and machine-local config loading.

This guard began as a zsh script; 'the zsh original' below means that version,
which is not shipped here. Ports the zsh original's early setup faithfully -- the git timeout budget,
`expand_path`, the no-fallback `canonical_root` reasoning, POSIX `dirname` --
replacing its zsh-specific mechanism (a perl alarm, `typeset -F SECONDS`,
`jq`) with stdlib equivalents. The entry point `claude-worktree-guard` only
reads stdin, calls `evaluate`, and maps the verdict to an exit code.
"""

import collections
import json
import os
import re
import subprocess
import sys
import time

# --- constants -------------------------------------------------------------
#
# GIT_TIMEOUT_SECONDS bounds one call; BUDGET_SECONDS bounds the SUM of every
# git call in one hook invocation -- a per-call cap alone cannot bound the
# total, since the number of calls scales with the command line's own length.
# 7 rather than 5: at 5s, a long command with hundreds of git invocations
# was REFUSED on a healthy repo where the pre-change guard allowed it --
# training people onto WORKTREE_ALLOW=1 is as bad as leaving a tail
# unjudged. At 7s, exhaustion takes a command length no real command reaches;
# a command that does (thousands of invocations, far past the declared 10s
# hook timeout) gets the WHOLE hook
# killed by the harness regardless, which happened before this change too --
# so this budget bounds GIT time only, never the hook's wall clock (the
# non-git walk is linear in command length and uncapped). Accepted gap: a
# filesystem slow enough to matter but under GIT_TIMEOUT_SECONDS (say 1.5s
# against a 2s cap) exhausts the budget with no single call stalling, and is
# therefore ALLOWED -- nothing here tells that apart from an ordinary long
# command.
#
# The two outcomes are kept as separate GitResult statuses rather than one
# shared failure code because they carry opposite evidence: "stalled" (a call
# the timeout actually killed) is direct evidence something is wrong and may
# refuse a BLOCKING_VERBS verb; "budget" (nothing attempted) is evidence only
# that the command was long, and must never refuse -- see check_invocation's
# own stalled/budget handling.
GIT_TIMEOUT_SECONDS = 2
BUDGET_SECONDS = 7
GIT_PATH_POSIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"


# --- the shared danger-guard import, done lazily ----------------------------
#
# `guard_lib` carries the tokenizer, `unquote`, `_strip_heredocs`, `canonical`
# and `_SEGMENT_SEPARATORS` the command walk needs. Importing it is
# deferred to first use rather than done at module level: the overwhelming
# majority of Bash calls never mention git at all, and the fast path that
# rules those out on the raw command text must not pay for importing a
# sibling module it will often never need.
_guard_lib_module = None


def _guard_lib():
    global _guard_lib_module
    if _guard_lib_module is None:
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
            "danger-guard"))
        import guard_lib
        _guard_lib_module = guard_lib
    return _guard_lib_module


Verdict = collections.namedtuple("Verdict", "blocked message")
# blocked: bool. message: the full stderr text when blocked, "" when allowed.

GitResult = collections.namedtuple("GitResult", "status stdout")
# status: one of "ok" "stalled" "budget" "failed".
# stdout: decoded UTF-8, trailing newlines stripped -- "" unless status is
# "ok" or a non-zero exit ("failed").

Context = collections.namedtuple(
    "Context", "home config_path windows allow git clock msys_path environ",
    defaults=(None,))
# home: str -- what `~` and `$HOME` in a command expand to.
# config_path: str -- the config file (may not exist).
# windows: bool -- os.name == "nt".
# allow: bool -- WORKTREE_ALLOW/WT_ALLOW_MAIN; the whole guard allows.
# git: callable(target_dir, args) -> GitResult, bound to one shared deadline.
# clock: callable() -> float, monotonic.
# msys_path: callable(str) -> str or None on Windows; None itself on POSIX.
# environ: mapping or None -- variables config paths may name; None resolves none.


# --- the bounded git runner --------------------------------------------------
#
# Every git call here was UNBOUNDED in an earlier cut of this guard, until a
# stalled mount or a wedged repo hung the whole hook -- see
# GIT_TIMEOUT_SECONDS/BUDGET_SECONDS above for the sizing and the
# stalled-vs-budget evidence distinction. The zsh original bounded this with a
# perl `alarm` (no native timeout in zsh) plus a wall-clock BUDGET shared
# across every git call in one hook invocation, because a per-call cap alone
# cannot bound the total when the number of calls scales with the command
# line's own length (a separator-delimited walk makes one or more git calls
# per segment). `subprocess.run(timeout=...)` replaces the perl alarm
# directly; the budget-sharing and the 124-vs-125 distinction (a call the
# alarm actually killed is evidence something is wrong and may refuse a
# BLOCKING_VERBS verb; a budget that merely ran out with nothing attempted is
# not evidence and must not) are unchanged.
#
# `make_runners` is called once per hook invocation, from
# `context_from_environment`, and returns a `(git, msys_path)` pair that
# share ONE deadline, measured from the moment this function runs -- not from
# each call -- exactly like the zsh `SECONDS` variable it replaces.
def make_runners(clock, budget_seconds, windows):
    start = clock()

    def _run(argv):
        left = budget_seconds - (clock() - start)
        cap = min(GIT_TIMEOUT_SECONDS, left)
        # Truncate toward zero -- not to avoid overspending the budget (an
        # untruncated cap is already <= `left`, so it can never do that), but
        # to match the zsh original's `alarm`, which takes only an INTEGER
        # and treats alarm(0) as CANCEL rather than "fire immediately": a
        # sub-second remainder there had to round down to 0 and refuse to
        # run, rather than risk an uncapped call. `subprocess.run`'s
        # `timeout=` takes a float and needs no such truncation technically;
        # it is kept anyway so the EFFECTIVE cutoff -- one second below
        # budget_seconds -- carries over unchanged.
        icap = int(cap)
        if icap < 1:
            return GitResult("budget", "")   # budget gone; nothing attempted
        env = dict(os.environ)
        if not windows:
            env["PATH"] = GIT_PATH_POSIX
        try:
            completed = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, timeout=icap, env=env)
        except subprocess.TimeoutExpired:
            return GitResult("stalled", "")   # this call was killed
        except OSError:
            return GitResult("failed", "")    # no git, or similar
        out = completed.stdout.decode("utf-8", errors="replace").rstrip("\n")
        if completed.returncode != 0:
            return GitResult("failed", out)
        return GitResult("ok", out)

    def git(target_dir, args):
        return _run(["git", "-C", target_dir] + list(args))

    if not windows:
        return git, None

    def msys_path(p):
        # cygpath, under the SAME deadline and cap/truncation rule as git --
        # a wedged cygpath can hang the hook exactly as a wedged git can.
        result = _run(["cygpath", "-m", p])
        return result.stdout if result.status == "ok" else None

    return git, msys_path


# --- context: the only reader of os.environ ---------------------------------

def context_from_environment():
    """Build a Context from the live process -- the ONLY function in this
    module that reads os.environ."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    config_path = os.environ.get("CLAUDE_TOOLKIT_CONFIG") or os.path.join(
        home, ".config", "claude-code-toolkit", "config.json")
    windows = os.name == "nt"
    allow = (os.environ.get("WORKTREE_ALLOW") == "1"
             or os.environ.get("WT_ALLOW_MAIN") == "1")

    # Test-only budget override, honoured ONLY when both variables are set --
    # a single unconditional override inside a guard is a backdoor, and the
    # compound gate is the real protection. An earlier cut clamped this to
    # only ever SHORTEN the budget; the clamp is gone because lengthening it
    # changes no verdict -- it can only make a TEST hook outlive its own
    # declared timeout, which costs a flaky test and cannot reach production,
    # so an unenforceable clamp plus a comment claiming it protects something
    # was worse than neither. An unparseable value falls back to
    # BUDGET_SECONDS rather than raising.
    budget_seconds = BUDGET_SECONDS
    test_budget = os.environ.get("WORKTREE_TEST_BUDGET")
    test_harness = os.environ.get("WORKTREE_TEST_HARNESS")
    if test_budget and test_harness:
        try:
            budget_seconds = float(test_budget)
        except ValueError:
            budget_seconds = BUDGET_SECONDS

    clock = time.monotonic
    git, msys_path = make_runners(clock, budget_seconds, windows)
    return Context(home=home, config_path=config_path, windows=windows,
                   allow=allow, git=git, clock=clock, msys_path=msys_path,
                   environ=dict(os.environ))


# --- machine-local config ----------------------------------------------------

def _expand_path(p, home, environ=None):
    """Delegates to `toolkit_config.expand_path` (one implementation, shared
    with claude-danger-guard, reached through its lazy loader), then applies a
    single trailing-slash trim; the root `/` is left whole rather than
    becoming empty. Returns `None` for an unresolved path, or when the shared
    module is unavailable (fail-open, as for a broken config). With no
    `environ`, no variables are resolved."""
    try:
        p = _guard_lib()._toolkit_config().expand_path(
            p, home=home, environ=environ if environ is not None else {}, strict=True)
    except Exception:
        return None   # module unavailable: the path is absent (fail-open, as for a broken config)
    if p is None:
        return None
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    return p


def load_config(config_path, home, environ=None):
    """Read `config_path` into `(workspace_root, canonical_root)`, each ""
    when absent. Any error, a non-object top level, or a non-string value is
    "" for that field -- never raises. A config-driven crash would disarm
    every rule this guard has, same reasoning as claude-danger-guard's
    config loader.

    `canonical_root` comes ONLY from `worktree_guard.canonical_root`,
    deliberately with NO fallback to `workspace_root`. A sibling tool
    (`tools/repo-freshness/sync_lib.py`) already rejected guessing
    `<workspace_root>/repo` as a sync root, for the same reason this rule
    rejects it for a GUARD: there, a wrong guess is a silent no-op; here, a
    wrong guess is a silent WIDENING of what gets refused -- Rule C would
    start blocking edits and commits on the default branch of every checkout
    under workspace_root, on any machine that merely set workspace_root for
    the danger guard's benefit, never having decided to enable Rule C at
    all. Switching a refusal on is a decision its owner makes explicitly,
    never one inferred from an unrelated key.

    `canonical_root` is resolved to its real path only when it is currently
    a directory -- the zsh `cd ... && pwd -P` this replaces, and for the
    same reason: every target it is compared against comes from `git
    rev-parse --show-toplevel`, which always resolves symlinks, so comparing
    an unresolved configured path against an already-resolved toplevel would
    silently never match. A canonical_root that does not exist (yet, or any
    longer) resolves to "" -- the fail-open case: Rule C stays inactive
    rather than guessing.
    """
    try:
        # Explicit UTF-8: the ambient locale (cp1252 on Windows) would turn a
        # non-ASCII canonical_root into a miss and switch Rule C off silently.
        with open(config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        data = None

    workspace_root = ""
    canonical_root = ""
    if isinstance(data, dict):
        ws = data.get("workspace_root")
        if isinstance(ws, str) and ws:
            workspace_root = _expand_path(ws, home, environ) or ""
        worktree_guard_cfg = data.get("worktree_guard")
        if isinstance(worktree_guard_cfg, dict):
            cr = worktree_guard_cfg.get("canonical_root")
            if isinstance(cr, str) and cr:
                canonical_root = _expand_path(cr, home, environ) or ""

    if canonical_root and os.path.isdir(canonical_root):
        canonical_root = os.path.realpath(canonical_root)
    else:
        canonical_root = ""

    return workspace_root, canonical_root


def shell_dirname(p):
    """POSIX `dirname` semantics: strip trailing `/`s, then split on the
    last remaining one -- no `/` at all -> "."; `/x` -> `/`; `/` -> `/`.
    Used wherever the zsh original calls `dirname` or the `:h` modifier."""
    stripped = p.rstrip("/")
    if stripped == "":
        return "/"   # p was "/" or all slashes
    idx = stripped.rfind("/")
    if idx == -1:
        return "."
    if idx == 0:
        return "/"
    return stripped[:idx]


# A leading `/<one letter>` boundary -- exactly `/x` or `/x/...` -- is Git
# Bash's own drive-mount spelling, already recognised directly by
# `guard_lib.canonical` (it rewrites `/c/...` to `c:/...` itself). Anything
# else that starts with `/` on Windows (`/tmp/...`, an MSYS-internal path) is
# NOT a drive mount and needs `ctx.msys_path` (cygpath) to translate it into
# something `canonical` can fold at all -- native `git.exe` never understood
# either spelling, but `canonical` only understands the mount form.
_MOUNT_FORM_RE = re.compile(r"^/[A-Za-z]($|/)")

# After canonicalisation, a Windows drive path reads `x:/...`, always
# lowercase (canonical's own case-fold) -- the one shape that counts as
# "absolute" on Windows beyond a leading `/`.
_WINDOWS_DRIVE_ABS_RE = re.compile(r"^[a-z]:")


def prepare_path(p, base, ctx):
    """Prepare a (possibly relative, possibly Windows-spelled) path for every
    comparison or git call the guard makes -- cd targets, `git
    -C`/`--work-tree`/`--git-dir`/`env -C` arguments, the payload `cwd`, and
    the Edit family's `file_path`/`notebook_path`. Four steps, in this exact
    order (each is load-bearing against the next on Windows; on macOS steps
    2 and 3 change nothing):

    1. A leading `~`/every `$HOME` or `${HOME}` expands to `ctx.home` (the
       braced spelling is the same variable to the shell; left unexpanded it
       read as a relative path that resolves nowhere, so the command went
       unjudged) -- must run FIRST,
       because `ctx.home` itself may be a Windows path (`C:\\Users\\me`)
       that step 3 still needs to canonicalise.
    2. On Windows only, with a working `ctx.msys_path`: a path that starts
       with `/` but is NOT Git Bash's own drive-mount spelling (`/c`,
       `/c/...`) is an MSYS-internal path (`/tmp/...`) that native `git.exe`
       cannot resolve at all -- translate it through `cygpath` (`ctx.msys_path`)
       first. A translation failure (anything but a string back) leaves the
       path exactly as it was; `canonical` below will fail to fold it, which
       is honest -- a path this guard could not translate is a path it
       cannot promise is the same directory as anything else.
    3. `guard_lib.canonical(p, ctx.windows)` -- the identity function on
       POSIX, so nothing below this line ever changes a POSIX path. On
       Windows it folds backslashes, the `/c/...` mount form, and case onto
       one comparable spelling.
    4. A still-relative result is joined onto `base`. "Relative" means "does
       not start with `/`" on POSIX, same as the zsh original's
       `resolve_dir`; on Windows ONLY, a canonicalised drive path (`c:/...`,
       lowercase because step 3 already folded it) also counts as absolute,
       so it is never joined onto `base` by mistake. The `ctx.windows` gate
       on that second check is itself load-bearing: without it, a POSIX
       relative path that merely starts with a drive-letter-shaped prefix
       (`c:foo`, not an actual Windows absolute path there) would stop being
       joined to `base` on this Mac too.

    Every call site that takes a PARENT of a prepared path (the git-dir
    parent below, and the Edit path's `edit_dir`) calls this FIRST and takes
    the parent of the RESULT, never the reverse. On POSIX that ordering is a
    no-op change from the zsh original's (which took `dirname` of the raw,
    unresolved git-dir first) -- the two commute here because neither step
    changes the other's answer when the separator is always `/`. It stops
    commuting on Windows, where `shell_dirname` does not understand
    `C:\\x\\.git`, which is why the order matters now even though POSIX
    cannot see it."""
    if p.startswith("~"):
        p = ctx.home + p[1:]
    p = p.replace("${HOME}", ctx.home).replace("$HOME", ctx.home)

    if (ctx.windows and ctx.msys_path is not None
            and p.startswith("/") and not _MOUNT_FORM_RE.match(p)):
        translated = ctx.msys_path(p)
        if isinstance(translated, str):
            p = translated

    p = _guard_lib().canonical(p, ctx.windows)

    is_absolute = p.startswith("/")
    if not is_absolute and ctx.windows and _WINDOWS_DRIVE_ABS_RE.match(p):
        is_absolute = True
    if not is_absolute:
        p = base + "/" + p
    return p


def _block_message(reason, command):
    """The zsh original's `block()` function, stderr text byte for byte:
    five lines, each newline-terminated (`print -r --` always appends one).
    The entry point writes this straight to stderr and exits 2; it never
    prints anything else on the block path."""
    return (
        "BLOCKED by claude-worktree-guard: %s\n"
        "\n"
        "Command: %s\n"
        "\n"
        "If this is genuinely what you want, re-run this session with "
        "WORKTREE_ALLOW=1\n"
        "(or WT_ALLOW_MAIN=1 — both spellings work) set.\n"
        % (reason, command)
    )


# Every verb that can alter a target's working tree, index, or refs. Rule A
# refuses these cross-checkout; Rule B's four are a subset. ONE module-level
# tuple rather than a literal list at each call site, because two copies of a
# filter is how a mutation aimed at one silently grades the other -- it is
# read (as a Name, not reconstructed) at exactly two places below: the stall
# refusal in `check_invocation` and Rule A's own check there. A verb absent
# from this tuple AND from the DELIBERATELY EXEMPT comment in
# `check_invocation` is a gap someone missed, not a decision. Keep that
# property when adding one.
BLOCKING_VERBS = (
    "add", "am", "apply", "checkout", "cherry-pick", "clean", "commit",
    "fetch", "merge", "mv", "notes", "pull", "push", "rebase", "replace",
    "reset", "restore", "revert", "rm", "sparse-checkout", "stash",
    "submodule", "switch", "tag",
)


def is_sep(tok):
    """Statement/segment boundary tokens for the command walk below. The
    zsh original had its own `is_sep` (`; && || | ( ) { }`, `&` deliberately
    excluded); this instead reuses claude-danger-guard's own separator set,
    `guard_lib._SEGMENT_SEPARATORS`, which recognises `&`, `&|`, `&!`, `;|`
    and `;&` too -- a deliberate change from the zsh version, so the two guards
    agree on what counts as a judgment boundary rather than silently
    disagreeing on background jobs and the newer compound operators."""
    return tok in _guard_lib()._SEGMENT_SEPARATORS


# A leading `NAME=value` assignment prefix at the start of a statement, most
# commonly `GIT_DIR=`/`GIT_WORK_TREE=` before `git`, but any assignment is
# consumed the same way so it is never mistaken for a command token. Mirrors
# the zsh glob `[A-Za-z_]*=*` literally: starts with a letter or underscore,
# and contains an `=` somewhere after that -- not a stricter "valid shell
# identifier" check, because the zsh original was not one either.
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_].*=")

# Rule C's push carve-outs, translated from the zsh `grep -E` byte for byte
# -- `[[:space:]]` becomes `[ \t\f\v\r]` (the joined args
# string is never expected to carry an embedded newline, so its absence here
# changes nothing observable). Tested per LINE of the joined string, same as
# `grep` over piped input, not with `re.MULTILINE` substituted in for a
# whole-string DOTALL scan.
_SPACE_CLASS = r"[ \t\f\v\r]"
_FORCE_PUSH_RE = re.compile(
    r"(^|" + _SPACE_CLASS + r")(-f|--force|--force-with-lease|--mirror|"
    r"--prune)(=|" + _SPACE_CLASS + r"|$)")
_PLUS_REFSPEC_RE = re.compile(
    r"(^|" + _SPACE_CLASS + r")\+[A-Za-z0-9_][A-Za-z0-9_./-]*(:|"
    + _SPACE_CLASS + r"|$)")


def _matches_any_line(pattern, text):
    return any(pattern.search(line) for line in text.split("\n"))


def _is_delete_refspec_token(tok):
    """A refspec `[+]<src>:<dst>` with an empty SOURCE deletes `<dst>` on
    the remote -- `:?*`/`+:?*` in the zsh original, which require at least
    one character after the colon (exactly what a real delete has, and a
    bare `:`/`+:` does not -- see `check_rule_c_git` for the bare-`+:` arm,
    which is forced but deletes nothing and must NOT match here)."""
    if tok.startswith(":") and len(tok) > 1:
        return True
    if tok.startswith("+:") and len(tok) > 2:
        return True
    return False


class _Walk(object):
    """Per-invocation state for the command walk and Rule C's Edit-family
    surface: the git runner (via `ctx`), the raw command text block messages
    quote back, cwd's own resolved toplevel (Rule A's comparison target),
    and the per-target-directory caches the zsh original kept as global
    associative arrays.

    Built fresh inside `evaluate()` on every call -- deliberately NOT
    module-level state, unlike the zsh globals it replaces. The zsh
    original's own `wt_count_cache` comment records that its cache never
    actually worked (every caller ran it inside a subshell), so this is a
    design the zsh file never got to exercise: these caches DO persist
    across the whole walk of one command line and DO actually cache, but
    only for the lifetime of one `evaluate()` call -- two separate calls in
    the same process (as `test_walk_linearity.py` makes) must never see each
    other's cached verdicts."""

    def __init__(self, ctx, canonical_root, raw_cmd="", cwd_top=""):
        self.ctx = ctx
        self.canonical_root = canonical_root
        self.raw_cmd = raw_cmd
        self.cwd_top = cwd_top
        self.default_branch_cache = {}
        self.current_branch_cache = {}
        self.wt_count_cache = {}
        self.linked_worktree_cache = {}


def toplevel_of(ctx, target_dir):
    """Read-only; any error (not a repo, path does not exist) means "can't
    tell" -- the caller must treat that as allow, never as block. NOT
    cached: unlike the per-target caches below, each call in the walk
    resolves a DIFFERENT target_dir in the overwhelming majority of real
    commands, so there is no repeat lookup here to save."""
    return ctx.git(target_dir, ["rev-parse", "--show-toplevel"])


# A killed call or a spent budget says nothing about the directory, so its
# empty answer is never cached: one stall would otherwise answer every later
# statement against the same checkout with "can't tell", which is an allow.
# An ordinary failure (no origin/HEAD, a detached HEAD) IS the answer and is
# cached.
_UNSETTLED = ("stalled", "budget")


def default_branch_of(walk, target_dir):
    """Cached per target directory. Through `walk.ctx.git`, not a bare git
    call: every git call on the decision path is charged against the hook's
    timeout budget. An exhausted budget or a real failure both leave `d`
    empty here, which leaves Rule C INACTIVE for this target -- the
    fail-open direction this rule already documents ("stays inactive rather
    than guessing"). The narrow refuse-on-stall exception in
    `check_invocation` is scoped to BLOCKING_VERBS, which none of Rule C's
    verbs are, so it does not extend here."""
    if target_dir in walk.default_branch_cache:
        return walk.default_branch_cache[target_dir]
    result = walk.ctx.git(
        target_dir, ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"])
    unsettled = result.status in _UNSETTLED
    d = result.stdout
    if d.startswith("origin/"):
        d = d[len("origin/"):]
    if not d:
        for candidate in ("main", "master"):
            r2 = walk.ctx.git(
                target_dir, ["show-ref", "--verify", "--quiet",
                             "refs/heads/%s" % candidate])
            if r2.status == "ok":
                d = candidate
                break
            unsettled = unsettled or r2.status in _UNSETTLED
    if not unsettled:
        walk.default_branch_cache[target_dir] = d
    return d


def current_branch_of(walk, target_dir):
    """Cached per target directory, same shape as `default_branch_of`."""
    if target_dir in walk.current_branch_cache:
        return walk.current_branch_cache[target_dir]
    result = walk.ctx.git(target_dir, ["symbolic-ref", "--quiet", "--short", "HEAD"])
    b = result.stdout
    if result.status not in _UNSETTLED:
        walk.current_branch_cache[target_dir] = b
    return b


def _strip_one_trailing_slash(s):
    """Strip exactly one trailing `/`, unless `s` is `/` itself. `canonical`
    already strips a trailing slash from an ordinary path but deliberately
    keeps it on a bare drive root (`x:/`) and on `/` -- so the two sides of
    `is_canonical_checkout`'s comparison can differ by exactly that one
    character even when they name the same directory:
    `shell_dirname(canonical("C:/proj"))` is `c:`, while `canonical("C:\\")`
    (a drive-root `canonical_root`) is `c:/` -- a drive-root canonical_root
    would otherwise never match any checkout under it."""
    if s != "/" and s.endswith("/"):
        return s[:-1]
    return s


def is_linked_worktree(walk, target_dir):
    """True when target_dir is a LINKED worktree (`git worktree add`), not
    a primary checkout.

    A linked worktree is never a canonical checkout, even when it sits
    directly under the canonical root. Worktrees are often created as
    siblings of their primary checkout, so with a flat layout
    (`<root>/project`, `<root>/project-feature`) they are immediate children
    of the root too. Without this check Rule C refuses every edit and commit
    in a worktree that holds the default branch, advising "do the work in a
    worktree" to someone already standing in one, while the real primary
    checkout beside it allows everything. An over-block is a regression, not
    extra safety: a guard that refuses ordinary work gets switched off.

    The test is git's own: `--git-dir` and `--git-common-dir` name the same
    directory in a primary checkout and different ones in a linked worktree
    (the same discriminator `lib/worktree_detect.sh` uses for install.sh;
    that helper is shell sourced by installers, while this hook routes every
    git call through its timeout budget, so the rule is written in both
    places rather than shared). Both answers are made absolute against
    target_dir and resolved before they are compared, because git prints
    them relative to where it ran (`.git` in a primary checkout).

    One git call, through `walk.ctx.git`, inside the hook's budget, cached
    per target directory unless the call stalled or the budget ran out. Any
    answer this cannot read is "not linked": the exclusion only ever removes
    a refusal, so not knowing leaves Rule C to its other conditions, each of
    which needs its own git answer before it can refuse."""
    if target_dir in walk.linked_worktree_cache:
        return walk.linked_worktree_cache[target_dir]
    result = walk.ctx.git(target_dir, ["rev-parse", "--git-dir", "--git-common-dir"])
    linked = False
    if result.status == "ok":
        lines = result.stdout.split("\n")
        if len(lines) == 2 and lines[0] and lines[1]:
            git_dir, common_dir = (
                os.path.realpath(os.path.join(target_dir, line)) for line in lines)
            linked = git_dir != common_dir
    if result.status not in _UNSETTLED:
        walk.linked_worktree_cache[target_dir] = linked
    return linked


def is_canonical_checkout(walk, target_dir):
    """True only when target_dir is an IMMEDIATE child of the configured
    canonical root and is not a linked worktree (`is_linked_worktree`).
    `walk.canonical_root` is "" whenever
    `worktree_guard.canonical_root` is not configured, in which case this
    always fails closed (Rule C stays inactive, never guesses).

    Both sides are canonicalised BEFORE the parent is taken --
    `shell_dirname(canonical(target_dir))`, compared against
    `canonical(canonical_root)` -- never the reverse. Neither side arrives
    pre-folded: `target_dir` here is `rev-parse --show-toplevel`'s raw
    stdout, and `canonical_root` comes straight from config -- on POSIX
    canonicalising either is a no-op, but on Windows `shell_dirname` of a
    raw backslash toplevel (`C:\\Users\\me\\repos\\x`, which real `git.exe`
    can return) sees no `/` at all and answers `.`, so Rule C would silently
    never fire if the parent were taken first.

    Both sides are then run through `_strip_one_trailing_slash` before the
    comparison -- see its own docstring for why a drive-root canonical_root
    needs it.

    The lexical comparison runs first, so a target outside the root never
    pays for the linked-worktree git call."""
    if not walk.canonical_root:
        return False
    guard_lib_mod = _guard_lib()
    windows = walk.ctx.windows
    left = shell_dirname(guard_lib_mod.canonical(target_dir, windows))
    right = guard_lib_mod.canonical(walk.canonical_root, windows)
    if _strip_one_trailing_slash(left) != _strip_one_trailing_slash(right):
        return False
    return not is_linked_worktree(walk, target_dir)


def on_canonical_default_branch(walk, target_dir):
    """True only when target_dir is a canonical checkout currently sitting
    on its OWN default branch -- the condition every Rule C surface gates
    on before doing anything else."""
    if not is_canonical_checkout(walk, target_dir):
        return False
    branch = current_branch_of(walk, target_dir)
    if not branch:
        return False   # detached HEAD: not our problem
    default = default_branch_of(walk, target_dir)
    if not default:
        return False
    return branch == default


def worktree_count(walk, target_dir):
    """Returns (status, count). status is "stalled" when the `worktree
    list` call itself was killed by the timeout -- the ONLY status this
    caller distinguishes, since a budget exhaustion or any other git
    failure is "can't tell" and folds to a count of 0 (Rule B stays
    inactive). Cached per target directory, but ONLY on a genuine "ok"
    result: a stalled or budget-exhausted call returns before the cache is
    written, because an expired budget is not a property of this
    directory, so it has no business living in a per-directory cache."""
    if target_dir in walk.wt_count_cache:
        return "ok", walk.wt_count_cache[target_dir]
    result = walk.ctx.git(target_dir, ["worktree", "list", "--porcelain"])
    if result.status == "stalled":
        return "stalled", 0
    if result.status != "ok":
        return "ok", 0   # budget gone, or an ordinary failure -- can't tell
    count = sum(1 for line in result.stdout.split("\n")
               if line.startswith("worktree "))
    walk.wt_count_cache[target_dir] = count
    return "ok", count


def tokens_after_dashdash(args):
    """Count of tokens after the first literal "--", or -1 if "--" does not
    appear at all. `git checkout main --` has zero tokens after "--" and is
    a branch switch (git itself prints "Switched to branch 'main'"), not a
    file restore -- only "--" followed by at least one path is."""
    for idx, tok in enumerate(args):
        if tok == "--":
            return len(args) - idx - 1
    return -1


_RULE_B_NEW_BRANCH = (
    "Rule B (branch switching under concurrency) — %s has multiple "
    "worktrees registered; creating and switching to a new branch here "
    "risks clobbering whichever session holds it. Use 'git worktree add "
    "-b <branch> <path>' instead.")
_RULE_B_CHECKOUT_AMBIGUOUS = (
    "Rule B (branch switching under concurrency) — %s has multiple "
    "worktrees registered; switching its branch here would move it out "
    "from under whichever session holds it. To restore a file, use 'git "
    "restore <path>' or 'git checkout -- <path>' — both are allowed here. "
    "To switch a branch, use 'git worktree add <path> <branch>' and work "
    "in its own worktree.")
_RULE_B_SWITCH = (
    "Rule B (branch switching under concurrency) — %s has multiple "
    "worktrees registered; switching its branch here would move it out "
    "from under whichever session holds it. Use 'git worktree add <path> "
    "<branch>' to check it out in its own worktree instead.")
_RULE_B_RESET = (
    "Rule B (branch switching under concurrency) — %s has multiple "
    "worktrees registered; resetting the working tree here could blow "
    "away work another session is relying on. Do this in a dedicated "
    "worktree instead.")
_RULE_B_STASH = (
    "Rule B (branch switching under concurrency) — %s has multiple "
    "worktrees registered; stashing here changes working-tree state "
    "another session may depend on. Do this in a dedicated worktree "
    "instead.")


def check_rule_c_git(walk, subcommand, target_top, args):
    """Rule C: landing directly on a canonical checkout's OWN default
    branch. Independent of worktree count, unlike Rule B. Returns a
    blocking Verdict, or None to allow."""
    if not on_canonical_default_branch(walk, target_top):
        return None
    branch = current_branch_of(walk, target_top)

    if subcommand in ("commit", "rebase"):
        reason = (
            "Rule C (canonical checkout on default branch) — 'git %s' "
            "would land work directly on '%s' in the canonical checkout "
            "%s. Do the work in a worktree instead: 'git worktree add "
            "<path> -b <branch>'."
            % (subcommand, branch, target_top))
        return Verdict(True, _block_message(reason, walk.raw_cmd))

    if subcommand == "merge":
        if "--ff-only" in args:
            return None   # can only advance a pointer or refuse
        reason = (
            "Rule C (canonical checkout on default branch) — a "
            "non-fast-forward 'git merge' would land on '%s' in the "
            "canonical checkout %s. 'git merge --ff-only' is allowed; "
            "anything else needs a worktree." % (branch, target_top))
        return Verdict(True, _block_message(reason, walk.raw_cmd))

    if subcommand == "push":
        # A remote-branch DELETE is not a history rewrite, and gets its own
        # verdict -- a refspec `[+]<src>:<dst>` with an empty source deletes
        # `<dst>`, which is what `--delete`/`-d` do too. A PER-TOKEN check,
        # not a regex over the whole line, for the same reason: an SSH
        # remote (`git@host:user/repo.git`) or an ordinary refspec
        # (`local:remote`) has a colon that is never the first character.
        for a in args:
            if a in ("--delete", "-d") or _is_delete_refspec_token(a):
                reason = (
                    "Rule C (canonical checkout on default branch) — git "
                    "push --delete — it removes a branch from the remote. "
                    "For a merged branch that is harmless cleanup; for an "
                    "unmerged one the remote held the only published copy. "
                    "Delete it in the GitHub UI, where it shows you which "
                    "case you are in.")
                return Verdict(True, _block_message(reason, walk.raw_cmd))
            if a == "+:":
                # The FORCED form of the matching push (`git push origin
                # +:`) -- still no destination, so nothing is deleted; it
                # gets the history-rewrite verdict, never the delete one.
                reason = (
                    "Rule C (canonical checkout on default branch) — a "
                    "history-rewriting push would run against '%s' in the "
                    "canonical checkout %s. A plain, non-force 'git push' "
                    "is allowed." % (branch, target_top))
                return Verdict(True, _block_message(reason, walk.raw_cmd))

        # How these arms overlap with claude-danger-guard, which also judges
        # push shapes, but everywhere and only for blast radius:
        #   --force-with-lease  danger-guard ALLOWS it (it recommends it as
        #                       the safe alternative to --force). Rule C
        #                       blocks it anyway: a canonical checkout's own
        #                       default branch is never the place to land a
        #                       history-rewriting push.
        #   --prune             danger-guard has no rule for it; this rule
        #                       alone covers it.
        #   --mirror, +:        danger-guard blocks these too.
        #   --delete/-d         handled by the delete arm above. danger-guard
        #                       ALLOWS both, deliberately: they announce
        #                       themselves, and its block would be a hard
        #                       refusal needing DANGER_ALLOW=1 for the whole
        #                       session. So on a canonical checkout's default
        #                       branch the delete arm is the ONLY refusal.
        #
        # READ THIS BEFORE DELETING ANY ARM HERE. A comment describing the
        # other guard cannot be checked by that guard's tests, so it goes
        # stale silently, and an earlier version was wrong in both
        # directions. The pairs below are therefore parsed and re-run
        # against danger-guard by TestTheCrossGuardCommentAgreesWithTheRealGuard,
        # and pinned guard-against-guard by TestCrossGuardVerdictTable.
        # Measured against this repo's danger-guard, by running it:
        #   +:  BLOCK   --mirror BLOCK   :main BLOCK
        #   --delete ALLOW   -d ALLOW   --prune ALLOW   --force-with-lease ALLOW
        #
        # Which guard refuses first is not something to rely on: PreToolUse
        # hooks matching the same event run concurrently, so the order their
        # refusals arrive in is a timing race.
        joined = " ".join(args)
        if (_matches_any_line(_FORCE_PUSH_RE, joined)
                or _matches_any_line(_PLUS_REFSPEC_RE, joined)):
            reason = (
                "Rule C (canonical checkout on default branch) — a "
                "history-rewriting push would run against '%s' in the "
                "canonical checkout %s. A plain, non-force 'git push' is "
                "allowed." % (branch, target_top))
            return Verdict(True, _block_message(reason, walk.raw_cmd))

    return None


def check_invocation(walk, subcommand, target_path, args):
    """One per-git-invocation rule check: subcommand, the resolved target
    path, and the tokens that followed the subcommand within its own
    statement (already de-quoted). Returns a blocking Verdict, or None to
    allow (and keep walking)."""
    if not subcommand:
        return None
    if subcommand == "worktree":
        return None   # deliberately exempt, both rules

    result = toplevel_of(walk.ctx, target_path)
    if result.status == "stalled":
        # A REAL stall: this call was killed by the timeout. Refuse, but
        # only a verb that could have been refused anyway -- refusing a
        # verb whose verdict was already an unconditional allow (log, diff,
        # config, gc, ...) buys no safety and spends real trust.
        if subcommand in BLOCKING_VERBS:
            reason = (
                "resolving %s stalled and had to be killed, so 'git %s' — "
                "which this guard may have needed to refuse — is "
                "unjudged. A repository or filesystem that does not "
                "answer 'which checkout is this' is the usual cause."
                % (target_path, subcommand))
            return Verdict(True, _block_message(reason, walk.raw_cmd))
        return None
    if result.status == "budget":
        return None   # nothing attempted; says nothing about this target
    target_top = result.stdout
    if not target_top:
        return None   # can't resolve -> allow, don't guess

    windows = walk.ctx.windows
    guard_lib_mod = _guard_lib()
    if (walk.cwd_top
            and guard_lib_mod.canonical(target_top, windows)
            != guard_lib_mod.canonical(walk.cwd_top, windows)):
        # Rule A: cross-checkout reach. Only meaningful when cwd itself is
        # inside a repo (cwd_top set). Compared through `canonical`, not as
        # raw strings: on Windows, real `git.exe` can answer the SAME
        # toplevel with a different case or separator depending on which
        # spelling it was asked about, and an uncanonicalised compare would
        # see that as two different checkouts.
        #
        # BLOCKED — alters the target's working tree, index, HEAD, or refs:
        if subcommand in BLOCKING_VERBS:
            reason = (
                "Rule A (cross-checkout reach) — 'git %s' targets %s, but "
                "this shell is standing in %s. Run it from within %s "
                "instead." % (subcommand, target_top, walk.cwd_top, target_top))
            return Verdict(True, _block_message(reason, walk.raw_cmd))
        # DELIBERATELY EXEMPT — considered and ruled out, not merely absent:
        #   branch          git itself already refuses to move or delete a
        #                   branch another worktree holds; no gap here to
        #                   close, and blocking ordinary branch housekeeping
        #                   cross-checkout would be a pure misfire.
        #   worktree        exempt entirely (see the early return above) —
        #                   adding/removing a worktree from elsewhere IS the
        #                   normal workflow, not a violation of it.
        #   config          changes settings, not tracked content.
        #   gc, maintenance, prune, repack, fsck, reflog expire
        #                   plumbing/housekeeping, not content mutation.
        #   update-ref, update-index, symbolic-ref
        #                   plumbing primitives; porcelain commands built on
        #                   top of them (checkout, reset, etc.) are covered.
        #   status, log, diff, show, rev-parse, describe, blame, shortlog,
        #   ls-files, ls-tree, ls-remote, cat-file, grep, remote (listing),
        #   branch (listing)
        #                   read-only, never alter anything regardless of
        #                   target.
        #
        # NOT exempt, on reflection: `tag` is BLOCKED, full stop — including
        # `git tag -l`. Rule A branches on the subcommand, not its flags, and
        # `tag` can also create/delete a tag, so the whole subcommand is
        # blocked cross-checkout rather than parsing `-l` out as a special
        # case. Over-blocking a read-only listing is the safe-side error. The
        # same applies to `stash`: `git -C <other> stash list` is read-only
        # but blocked anyway — `stash` is one subcommand here, not a set of
        # flags to parse apart.
        return None

    # Either the same checkout, or cwd itself is not inside a repo at all
    # (Rule A above skipped) -- Rule B and Rule C territory either way,
    # evaluated against target_top. Rule C first, independent of worktree
    # count.
    if subcommand in ("commit", "rebase", "merge", "push"):
        verdict = check_rule_c_git(walk, subcommand, target_top, args)
        if verdict is not None:
            return verdict

    if subcommand not in ("checkout", "switch", "reset", "stash"):
        return None

    wt_status, wt_n = worktree_count(walk, target_top)
    if wt_status == "stalled":
        reason = (
            "counting %s's worktrees stalled and had to be killed, so "
            "this guard cannot tell whether 'git %s' here would move a "
            "checkout another session is holding. A repository or "
            "filesystem that does not answer is the usual cause."
            % (target_top, subcommand))
        return Verdict(True, _block_message(reason, walk.raw_cmd))
    if wt_n < 2:
        return None

    if subcommand == "checkout":
        if "-b" in args or "-B" in args:
            return Verdict(True, _block_message(_RULE_B_NEW_BRANCH % target_top,
                                                 walk.raw_cmd))
        if tokens_after_dashdash(args) > 0:
            return None   # `--` followed by at least one path: file restore
        if len(args) > 0:
            return Verdict(True, _block_message(
                _RULE_B_CHECKOUT_AMBIGUOUS % target_top, walk.raw_cmd))
        return None

    if subcommand == "switch":
        if "-c" in args or "-C" in args:
            return Verdict(True, _block_message(_RULE_B_NEW_BRANCH % target_top,
                                                 walk.raw_cmd))
        if len(args) > 0:
            return Verdict(True, _block_message(_RULE_B_SWITCH % target_top,
                                                 walk.raw_cmd))
        return None

    if subcommand == "reset":
        if "--hard" in args or "--merge" in args or "--keep" in args:
            return Verdict(True, _block_message(_RULE_B_RESET % target_top,
                                                 walk.raw_cmd))
        return None

    if subcommand == "stash":
        # A flag as the first token means no subcommand was given at all,
        # which git treats as an implicit `push` -- same as bare
        # `git stash`. Only a non-flag first token is ever a real stash
        # subcommand.
        if len(args) == 0 or args[0].startswith("-"):
            return Verdict(True, _block_message(_RULE_B_STASH % target_top,
                                                 walk.raw_cmd))
        if args[0] in ("push", "save", "pop", "apply", "drop", "clear"):
            return Verdict(True, _block_message(_RULE_B_STASH % target_top,
                                                 walk.raw_cmd))
        return None

    return None


# In the separator set, but reserved words rather than operators: special
# only where a command starts, plain words in an argument slot. `git -C !`
# takes `!` as its directory in bash and zsh alike.
_RESERVED_WORDS = frozenset(("!", "{", "}"))


def _normalized_command_word(token, windows):
    """A statement-start word reduced to the command the shell will find:
    basename (a backslash also separates on Windows), `.exe` stripped on
    Windows, lower-cased (macOS and Windows filesystems are case-insensitive,
    so `GIT` runs git). Walk tokens are already unquoted, so this does NOT
    reuse guard_lib's `_normalize_word`, whose `unquote` would strip a
    literal backslash from an already-unquoted Windows path."""
    cut = token.rfind("/")
    if windows:
        cut = max(cut, token.rfind("\\"))
    word = token[cut + 1:]
    if windows and word.lower().endswith(".exe"):
        word = word[:-4]
    return word.lower()


def _option_value(tokens, sep, idx):
    """The value an option takes from tokens[idx], and the index after it.

    A separator (or the end of the command) in the value slot supplies no
    value: it ends the statement, exactly as the shell would, so it is left
    for the walk to see. Taking it as the value stepped over the separator
    and hid the next statement from judgment. `sep` is the
    lexical separator mask, so a quoted or escaped `;` is still a value, and
    a reserved word is one too, because it is not an operator here."""
    if idx < len(tokens) and (not sep[idx] or tokens[idx] in _RESERVED_WORDS):
        return tokens[idx], idx + 1
    return "", idx


# env(1) options, per platform, because a form env rejects
# never reaches git and refusing it would break fail-open. macOS runs BSD env;
# a Windows session runs Git Bash's GNU env. Short flags take no value; short
# valued options take one, attached or as the next token. `-0` is left out on
# purpose: both envs refuse to run a program with it. `-S`/`--split-string`
# re-splits its value into new words, so it is unrecognised too, and anything
# unrecognised falls through unjudged, as it always did.
_ENV_OPTIONS = {
    False: (frozenset("iv"), frozenset("uPC"), frozenset(), frozenset()),
    True: (frozenset("iv"), frozenset("uC"),
           frozenset(("--ignore-environment", "--debug")),
           frozenset(("--unset", "--chdir"))),
}
_GIT_ENV_VARS = ("GIT_DIR", "GIT_WORK_TREE")


def _parse_env_prefix(tokens, sep, start, windows=False):
    """Read env's options from tokens[start:] up to the program it runs.

    Returns (program_index, chdir, assigned, cleared), or None when a word
    is not understood -- the caller then falls through, unjudged, exactly
    as before this parser existed. `chdir` is the LAST -C value, as env
    applies. `assigned` maps GIT_DIR/GIT_WORK_TREE set by NAME=value words;
    `cleared` holds those env removes from the inherited environment (`-i`
    clears both, `-u NAME` one), so a shell prefix it drops is not judged.
    Option parsing stops at the first NAME=value word, as getopt does."""
    flags, valued, long_flags, long_valued = _ENV_OPTIONS[bool(windows)]
    ei = start
    chdir = ""
    assigned = {}
    cleared = set()
    in_options = True
    n = len(tokens)

    def unset(name):
        if name in _GIT_ENV_VARS:
            cleared.add(name)

    while ei < n and not sep[ei]:
        tok = tokens[ei]
        if _ASSIGNMENT_RE.match(tok):
            name, _, value = tok.partition("=")
            if name in _GIT_ENV_VARS:
                assigned[name] = value
            in_options = False
            ei += 1
            continue
        if not in_options:
            break
        if tok == "--":
            ei += 1
            in_options = False
            continue
        if tok == "-":                       # obsolete spelling of -i
            cleared.update(_GIT_ENV_VARS)
            ei += 1
            continue
        if tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if name in long_flags and not eq:
                if name == "--ignore-environment":
                    cleared.update(_GIT_ENV_VARS)
                ei += 1
            elif name in long_valued:
                if not eq:
                    value, ei = _option_value(tokens, sep, ei + 1)
                else:
                    ei += 1
                if name == "--chdir" and value:
                    chdir = value
                elif name == "--unset":
                    unset(value)
            else:
                return None
            continue
        if tok.startswith("-") and len(tok) > 1:
            # A getopt cluster: flags, then at most one valued option whose
            # value is the rest of the cluster or the next token.
            ei += 1
            for k, ch in enumerate(tok[1:], 1):
                if ch in flags:
                    if ch == "i":
                        cleared.update(_GIT_ENV_VARS)
                    continue
                if ch in valued:
                    value = tok[k + 1:]
                    if not value:
                        value, ei = _option_value(tokens, sep, ei)
                    if ch == "C" and value:
                        chdir = value
                    elif ch == "u":
                        unset(value)
                    break
                return None
            continue
        break
    if ei < n and not sep[ei]:
        return ei, chdir, assigned, cleared
    return None


# Shell variables the command line assigns to itself (mission row 106).
# `W=<worktree>; git -C $W stash push` ran unjudged on 2026-11-22: `$W` was
# never expanded, the target resolved nowhere, and an unresolvable target
# allows. A literal `NAME=value` that is a statement of its own (`W=/x;`,
# `W=/x &&`, a newline) or an `export NAME=value` is recorded and expanded in
# every directory target the walk prepares. What it records is deliberately
# narrow, each rule the shell's own:
#   - a prefix assignment (`W=/x git ...`) is not recorded: the shell expands
#     `$W` in that command BEFORE the prefix applies;
#   - one inside `( )`, or followed by a pipe or `&`, is not kept: it ran in
#     a subshell;
#   - a value holding `$(...)` or a backtick is unknowable, so it UNBINDS the
#     name rather than leave an older literal in force;
#   - `W+=/sub` appends to the recorded value; onto an unknown one it
#     unbinds the name;
#   - a token holding a single quote or a backslash anywhere is not
#     expanded: the unquoted text no longer says which `$` the shell kept
#     literal (`'$W'`, `\$W`), and expanding one could refuse a command
#     that names a real `$W` directory.
# Anything still unresolved allows, exactly as before (fail open). A value
# set in an earlier Bash call or in the environment is not on this command
# line at all and stays out of reach.
_ASSIGN_NAME_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?)=")
_VAR_REF_RE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
# Separators after which a standalone assignment is still in force: the
# rest run their left side in a subshell (`|`, `|&`) or the background (`&`).
_SUBSHELL_SEPARATORS = frozenset(("|", "|&", "&", "&|", "&!"))


def expand_shell_vars(value, scopes, raw=""):
    """`value` with every `$NAME`/`${NAME}` the command line assigned
    replaced by its recorded value. `scopes` is a tuple of name -> value
    maps searched in order; a name present in an earlier one shadows the
    rest, even when its value is None (unbound). Searched, never merged: a
    merged copy per assignment made the walk quadratic in the number of
    names (test_walk_linearity). Unknown names are left as written, so
    `$HOME` still reaches `prepare_path`. `raw` is the token as typed: one
    holding a single quote or a backslash is returned unchanged, since the
    unquoted text no longer says which `$` the shell left literal."""
    if ("$" not in value or "'" in raw or "\\" in raw
            or not any(scopes)):
        return value

    def substitute(match):
        name = match.group(1) or match.group(2)
        for scope in scopes:
            if name in scope:
                known = scope[name]
                return match.group(0) if known is None else known
        return match.group(0)

    return _VAR_REF_RE.sub(substitute, value)


def _lookup(scopes, name):
    for scope in scopes:
        if name in scope:
            return scope[name]
    return None


def _record_assignment(tok, raw, scopes, into):
    """Record `NAME=value` (or `NAME+=value`, which appends) from one token
    into `into`, expanding the value against `scopes` (see
    expand_shell_vars). A non-identifier name records nothing; an
    unknowable value -- or an append onto an unknown one -- records None,
    which unbinds the name."""
    match = _ASSIGN_NAME_RE.match(tok)
    if not match:
        return
    name, append = match.group(1), match.group(2)
    value = tok[match.end():]
    if "$(" in value or "`" in value:
        into[name] = None   # unbinds: a later lookup must not see an old literal
        return
    # The raw token is `NAME=...`, so its quoting starts after the `=`.
    value = expand_shell_vars(value, scopes, raw.partition("=")[2])
    if append:
        base = _lookup(scopes, name)
        value = None if base is None else base + value
    into[name] = value


def _walk_tokens(walk, tokens, raw_tokens, cwd):
    """The whole command, token for token, tracking directory context
    across cd/&&/;/|/() -- the zsh original's equivalent.
    `(`/`)` push/pop dir_ctx on a stack rather than just resetting
    statement_start, so a command following a closed subshell sees the
    directory context from BEFORE it, matching what a real shell would do.
    Separator identity is lexical, read from the raw token, so a quoted or
    escaped `;`/`)` is a word, not a boundary.
    """
    n = len(tokens)
    i = 0
    # Lexical separator mask: a token ends a statement only if the tokenizer
    # produced it unquoted. `tokens` is already unquoted, so `')'` and `\)`
    # look exactly like a real `)` there.
    sep = [is_sep(t) for t in raw_tokens]
    statement_start = True
    dir_stack = []
    dir_ctx = cwd
    # GIT_DIR=/GIT_WORK_TREE= set as a leading env-var prefix on the git
    # invocation itself, cleared at every statement boundary as well as
    # once consumed by a `git` token -- env vars only apply to the ONE
    # command they prefix.
    env_gd = ""
    env_wt = ""
    # Set only while resolving a `git` invocation reached through
    # `env -C <dir> git ...` -- dir_ctx is swapped to <dir> for that single
    # invocation, then restored, because `env -C` changes the child
    # process's cwd, not the calling shell's (unlike `cd`, which persists).
    env_restore_dir_ctx = None
    # Same-line shell variables (see _SUBSHELL_SEPARATORS above): `shell_vars`
    # is what is in force; `pending_vars` holds a statement's leading
    # assignments until its end shows whether a command followed them.
    # A subshell's bindings are undone on `)` from `undo_log` (name, prior
    # value) back to the mark `(` pushed -- never by copying the table, which
    # costs names x subshells (test_walk_linearity).
    shell_vars = {}
    pending_vars = {}
    undo_log = []
    vars_marks = []
    missing = object()

    def bind(name, value):
        if vars_marks:
            undo_log.append((name, shell_vars.get(name, missing)))
        shell_vars[name] = value

    def expand(idx):
        return expand_shell_vars(tokens[idx], (shell_vars,), raw_tokens[idx])

    while i < n:
        tok = tokens[i]

        if raw_tokens[i] == "(":
            dir_stack.append(dir_ctx)
            vars_marks.append(len(undo_log))
            statement_start = True
            env_gd = ""
            env_wt = ""
            pending_vars = {}
            i += 1
            continue

        if raw_tokens[i] == ")":
            if dir_stack:
                dir_ctx = dir_stack.pop()
            if vars_marks:
                mark = vars_marks.pop()
                while len(undo_log) > mark:
                    name, prior = undo_log.pop()
                    if prior is missing:
                        shell_vars.pop(name, None)
                    else:
                        shell_vars[name] = prior
            statement_start = True
            env_gd = ""
            env_wt = ""
            pending_vars = {}
            i += 1
            continue

        if sep[i]:
            # Assignments with no command after them persist -- unless this
            # separator ran them in a subshell or the background.
            if (statement_start and pending_vars
                    and raw_tokens[i] not in _SUBSHELL_SEPARATORS):
                for name, value in pending_vars.items():
                    bind(name, value)
            pending_vars = {}
            statement_start = True
            env_gd = ""
            env_wt = ""
            i += 1
            continue

        if tok == "cd" and statement_start:
            if i + 1 < n and not sep[i + 1]:
                dir_ctx = prepare_path(expand(i + 1), dir_ctx, walk.ctx)
                i += 2
            else:
                i += 1
            statement_start = False
            continue

        if statement_start and _ASSIGNMENT_RE.match(tok):
            if tok.startswith("GIT_DIR="):
                env_gd = expand(i)[len("GIT_DIR="):]
            elif tok.startswith("GIT_WORK_TREE="):
                env_wt = expand(i)[len("GIT_WORK_TREE="):]
            # Left to right, as the shell assigns: `R=/x W=$R/wt` sees R.
            _record_assignment(tok, raw_tokens[i],
                               (pending_vars, shell_vars), pending_vars)
            i += 1
            continue   # statement_start stays True: more may follow

        if statement_start and tok == "export":
            # `export NAME=value ...`: every word is expanded before export
            # runs, so all of them see the variables from BEFORE it.
            exported = {}
            i += 1
            while i < n and not sep[i]:
                if _ASSIGNMENT_RE.match(tokens[i]):
                    _record_assignment(tokens[i], raw_tokens[i],
                                       (shell_vars,), exported)
                i += 1
            for name, value in exported.items():
                bind(name, value)
            statement_start = False
            continue

        if statement_start and _normalized_command_word(
                tok, walk.ctx.windows) == "env":
            # `env [options] [NAME=value] git ...`, with env and git each
            # spelled as a bare word or a path. Only a git
            # program is resolved; anything else, or an option the parser
            # does not understand, falls through untouched.
            parsed = _parse_env_prefix(tokens, sep, i + 1, walk.ctx.windows)
            if parsed and _normalized_command_word(
                    tokens[parsed[0]], walk.ctx.windows) == "git":
                ei, env_c_dir, assigned, cleared = parsed
                if env_c_dir:
                    env_restore_dir_ctx = dir_ctx
                    dir_ctx = prepare_path(
                        expand_shell_vars(env_c_dir, (shell_vars,)),
                        dir_ctx, walk.ctx)
                if "GIT_DIR" in cleared:
                    env_gd = ""
                if "GIT_WORK_TREE" in cleared:
                    env_wt = ""
                env_gd = expand_shell_vars(
                    assigned.get("GIT_DIR", env_gd), (shell_vars,))
                env_wt = expand_shell_vars(
                    assigned.get("GIT_WORK_TREE", env_wt), (shell_vars,))
                i = ei
                continue   # statement_start stays True: git is the command
            i += 1
            statement_start = False
            continue

        # A bare `git` is judged ANYWHERE in a statement, not only where a
        # command starts: that is what catches the wrapper forms (`sudo git
        # ...`, `time git ...`, `nohup git ...`, `command git ...`), which a
        # statement-start gate would leave unjudged. The price is that a
        # harmless argument named `git` (`echo git -C <other> commit`) is
        # judged too and can be refused -- the safe-side error, the same
        # trade as Rule A refusing `tag -l`. Only the path spelling (`/usr/bin/git`) needs the
        # statement start, since a path argument is far more often data.
        # TestGitWrappersAreJudged pins the wrappers.
        if tok == "git" or (statement_start
                            and _normalized_command_word(
                                tok, walk.ctx.windows) == "git"):
            j = i + 1
            c_dir = ""
            wt_target = ""
            gd_target = ""
            while j < n and not sep[j] and tokens[j].startswith("-"):
                if tokens[j] == "-C":
                    c_at = j + 1
                    c_value, j = _option_value(tokens, sep, c_at)
                    if c_value:
                        c_value = expand(c_at)
                    # git applies each -C relative to the previous one, and
                    # an empty value leaves the directory unchanged.
                    if c_value:
                        c_dir = prepare_path(c_value, c_dir or dir_ctx,
                                             walk.ctx)
                elif tokens[j] == "-c":
                    _, j = _option_value(tokens, sep, j + 1)
                elif tokens[j].startswith("--work-tree="):
                    wt_target = expand(j)[len("--work-tree="):]
                    j += 1
                elif tokens[j] == "--work-tree":
                    wt_at = j + 1
                    wt_target, j = _option_value(tokens, sep, wt_at)
                    if wt_target:
                        wt_target = expand(wt_at)
                elif tokens[j].startswith("--git-dir="):
                    gd_target = expand(j)[len("--git-dir="):]
                    j += 1
                elif tokens[j] == "--git-dir":
                    gd_at = j + 1
                    gd_target, j = _option_value(tokens, sep, gd_at)
                    if gd_target:
                        gd_target = expand(gd_at)
                else:
                    j += 1

            # No subcommand means the statement ended at j (a separator, or
            # the end): the args scan starts AT j, so the walk resumes on
            # that separator and the next statement is judged. Starting at
            # j + 1 stepped over it.
            subcommand = ""
            k = j
            if j < n and not sep[j]:
                subcommand = tokens[j]
                k = j + 1

            args_rest = []
            while k < n and not sep[k]:
                args_rest.append(tokens[k])
                k += 1

            # --work-tree (if given) IS the target directory outright.
            # Otherwise --git-dir's parent is -- this is what --git-dir
            # typically points AT (<checkout>/.git), and even when it
            # isn't exact, toplevel_of re-resolves against the real
            # filesystem, so an imprecise-but-inside path still lands on
            # the correct toplevel. -C is next, then whatever cd/subshell
            # context is in effect. Every candidate is prepared with
            # prepare_path before any parent is taken from it -- see
            # prepare_path's own docstring for why that order is load-
            # bearing on Windows even though it is inert here.
            # Relative --work-tree/--git-dir (and GIT_WORK_TREE/GIT_DIR) are
            # interpreted by git against the directory after every -C.
            effective_wt = wt_target or env_wt
            effective_gd = gd_target or env_gd
            base_dir = c_dir or dir_ctx
            if effective_wt:
                target_path = prepare_path(effective_wt, base_dir, walk.ctx)
            elif effective_gd:
                target_path = shell_dirname(
                    prepare_path(effective_gd, base_dir, walk.ctx))
            elif c_dir:
                target_path = c_dir
            else:
                target_path = prepare_path(dir_ctx, dir_ctx, walk.ctx)
            env_gd = ""
            env_wt = ""

            verdict = check_invocation(walk, subcommand, target_path, args_rest)
            if verdict is not None:
                return verdict

            if env_restore_dir_ctx is not None:
                dir_ctx = env_restore_dir_ctx
                env_restore_dir_ctx = None

            i = k
            statement_start = False
            continue

        i += 1
        statement_start = False

    return Verdict(False, "")


# Case-insensitive so `GIT.EXE ...` reaches the walk (see
# _normalized_command_word).
_BASH_FAST_PATH_RE = re.compile(r"(^|[^A-Za-z])git([^A-Za-z]|$)",
                                re.IGNORECASE)


def _evaluate_edit(payload, ctx):
    """Rule C only, no git tokenizer -- Edit/Write/MultiEdit/NotebookEdit
    never touch a shell, so Rule A/B have nothing to evaluate. Rule C,
    "don't land directly on a canonical checkout's default branch," applies
    regardless of how the write gets there."""
    _, canonical_root = load_config(ctx.config_path, ctx.home, ctx.environ)
    # Rule C is the ONLY rule on this surface, and it cannot fire at all
    # without a canonical_root. Leave before spending anything -- this hook
    # is registered for the whole Edit family, so without this check every
    # file edit on an unconfigured machine (the default) would pay a git
    # subprocess to reach a verdict that could only ever have been allow.
    if not canonical_root:
        return Verdict(False, "")

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        path = tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return Verdict(False, "")

    # `file_path`/`notebook_path` are always absolute in a real payload, so
    # `base` is inert here -- `cwd` if the payload carries one (same shape
    # as the Bash path), else home. Prepared through prepare_path like every
    # other path the guard resolves, THEN the parent taken if it is a file
    # or not a directory -- see prepare_path's docstring on why this order
    # is load-bearing on Windows.
    cwd = payload.get("cwd")
    base = cwd if isinstance(cwd, str) and cwd else ctx.home
    prepared = prepare_path(path, base, ctx)
    if os.path.isfile(prepared) or not os.path.isdir(prepared):
        edit_dir = shell_dirname(prepared)
    else:
        edit_dir = prepared

    # Host-native only: a simulated-Windows path cannot be resolved against
    # this filesystem, so there the lexical answer above stands. Otherwise
    # follow a file symlink or symlinked parent to where the write really
    # lands, then walk up past directories that do not exist yet -- git
    # cannot run in one, and the failed lookup would read as "not a repo".
    if ctx.windows == (os.name == "nt"):
        try:
            dest = _guard_lib().canonical(os.path.realpath(prepared), ctx.windows)
            real_dir = dest if os.path.isdir(dest) else shell_dirname(dest)
            while not os.path.isdir(real_dir):
                parent = shell_dirname(real_dir)
                if ctx.windows and len(parent) == 2 and parent[1] == ":":
                    parent += "/"
                if parent == real_dir:
                    break
                real_dir = parent
            edit_dir = real_dir
        except (OSError, ValueError):
            pass

    result = ctx.git(edit_dir, ["rev-parse", "--show-toplevel"])
    edit_top = result.stdout if result.status == "ok" else ""
    if not edit_top:
        return Verdict(False, "")

    walk = _Walk(ctx, canonical_root)
    if not on_canonical_default_branch(walk, edit_top):
        return Verdict(False, "")

    edit_branch = current_branch_of(walk, edit_top)
    reason = (
        "Rule C (canonical checkout on default branch) — editing %s would "
        "land work directly on '%s' in the canonical checkout %s. Do the "
        "work in a worktree instead: 'git worktree add <path> -b <branch>'."
        % (path, edit_branch, edit_top))
    return Verdict(True, _block_message(reason, "Edit %s" % path))


def _evaluate_bash(payload, ctx):
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    raw_cmd = tool_input.get("command")
    if not isinstance(raw_cmd, str) or not raw_cmd:
        return Verdict(False, "")

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()

    # Fast path, on the RAW command: the overwhelming majority of Bash
    # calls never mention git at all, and `guard_lib` (heredoc stripping,
    # tokenizing) must not be imported to find that out. A command that
    # mentions "git" only inside a heredoc body still reaches the walk
    # below rather than being allowed here -- harmless, since the walk
    # strips the heredoc and finds nothing to judge either way; it is
    # cheaper in Python to defer that distinction to guard_lib's own
    # heredoc stripper than to duplicate it here just to preserve the zsh
    # original's exact ordering (which ran the fast-path grep AFTER
    # stripping, something only free because strip_heredocs was a plain
    # shell function there).
    if not _BASH_FAST_PATH_RE.search(raw_cmd):
        return Verdict(False, "")

    guard_lib_mod = _guard_lib()
    kept = guard_lib_mod._strip_heredocs(raw_cmd)
    raw_tokens = guard_lib_mod.tokenize("\n".join(kept))
    tokens = [guard_lib_mod.unquote(t) for t in raw_tokens]

    # `cwd` is the payload's own value (or os.getcwd()), prepared here --
    # the same treatment every other path in this module gets -- before its
    # first use below. Unprepared, a Windows `cwd` reaches `ctx.git` raw:
    # a drive-mount spelling (`/c/Users/...`) native `git.exe` cannot
    # resolve, or a differently-cased spelling that would make an
    # otherwise-identical checkout fail to compare equal to itself.
    cwd = prepare_path(cwd, cwd, ctx)

    # Resolve the checkout this shell is standing in. If cwd is not inside
    # a git repo, cwd_top stays "" -- that does NOT mean allow-everything:
    # Rule A (which compares against cwd_top) has nothing to compare, so it
    # stays inactive, but Rule B still evaluates against whatever checkout
    # each git invocation actually resolves to. This call runs AFTER the
    # command has already been tokenized above, not before -- the walk just
    # needs cwd_top before IT starts. An expiry HERE never refuses: refusing
    # here would refuse every command merely CONTAINING the word git,
    # including `git worktree add`, the very remedy this guard's own
    # refusals recommend.
    cwd_result = ctx.git(cwd, ["rev-parse", "--show-toplevel"])
    cwd_top = cwd_result.stdout if cwd_result.status == "ok" else ""

    _, canonical_root = load_config(ctx.config_path, ctx.home, ctx.environ)
    walk = _Walk(ctx, canonical_root, raw_cmd=raw_cmd, cwd_top=cwd_top)
    return _walk_tokens(walk, tokens, raw_tokens, cwd)


def _evaluate(payload, ctx):
    if ctx.allow:
        return Verdict(False, "")
    if not isinstance(payload, dict):
        return Verdict(False, "")

    tool = payload.get("tool_name")
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return _evaluate_edit(payload, ctx)
    if tool in ("Bash", "Monitor"):
        return _evaluate_bash(payload, ctx)
    return Verdict(False, "")


def evaluate(payload, ctx):
    """The guard's whole decision for one PreToolUse payload.

    The tokenizer recurses once per nesting level of quotes/substitutions, so
    a few hundred nested `"$(...)"` exhaust Python's recursion limit. Any
    other exception is a crash, which the entry point reports and ALLOWS;
    this one is caught here and BLOCKS instead, because it is reachable from
    input alone and a crash-allow reachable by construction would be a
    bypass -- same reasoning, same message, as claude-danger-guard's own
    `evaluate`."""
    try:
        return _evaluate(payload, ctx)
    except RecursionError:
        command = ""
        if isinstance(payload, dict):
            tool_input = payload.get("tool_input")
            if isinstance(tool_input, dict):
                cmd = tool_input.get("command")
                if isinstance(cmd, str):
                    command = cmd
        return Verdict(True, _block_message(_guard_lib()._TOO_DEEP_MESSAGE, command))
