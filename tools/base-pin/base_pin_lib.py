#!/usr/bin/env python3
"""Report when `origin/<default>` is ahead of the worktree you are working in.

Why this exists
---------------
Every worktree of a repo shares one `.git`, and `origin/*` refs live in the
common dir. Worktrees isolate the working tree and `HEAD`; they do NOT isolate
remote-tracking refs. So the moment any session fetches or merges, `origin/main`
moves for every worktree of that repo at once — and a session mid-build has no
way to notice.

`repo-freshness` cannot cover this. It runs on `SessionStart` only, and its
`should_report` is deliberately silent while on a feature branch, which is every
build session. That silence is correct for its job (consumer staleness) and
leaves this case unreported.

There is no pin file
--------------------
The design this replaced recorded the commit a worktree branched from, in git's
per-worktree admin dir, written at `git worktree add` time. That was solving a
problem that does not exist: `git merge-base HEAD origin/<default>` already IS
the base, computed exactly, with no state to write, no state to go stale, and no
change to a live guard.

It is also strictly better than a recorded base in the case that matters. Merge
`origin/main` into your branch and the merge-base moves forward with it, so the
count drops to zero — correct. A recorded base would keep reporting drift you
had already resolved, which is the failure mode that teaches people to ignore a
notice.

So the whole computation collapses to one count: `rev-list --count
HEAD..origin/<default>`, which is what a merge would actually bring in. Do not
reintroduce a pin to "make the base explicit" — and see `behind_count` for why
that count is the RIGHT one rather than an exact identity with the merge-base
range, a distinction an earlier version of this docstring got wrong.

Everything degrades to silence
------------------------------
Every failure path returns None or False and the hook prints nothing. A notice
tool that guesses is worse than one that is quiet, because a wrong count trains
the reader to distrust the right ones.
"""

import json
import os
import subprocess
import time
from typing import Optional

# Runs on every prompt submit, so the budget is tight, and it is a BUDGET
# rather than a preference: `MAX_GIT_CALLS * GIT_TIMEOUT_SECONDS` must fit
# inside the `timeout` declared for this hook in plugin-build/hooks/hooks.json,
# and `TestTimeoutBudget` enforces that. If the harness kills the hook
# mid-git it produces no output at all — and for a notice tool, no output is
# indistinguishable from "nothing to report", which is the exact failure this
# tool exists to prevent. So slow here fails SILENT, not loud.
#
# Re-derive the count from the code rather than trusting this comment; the
# equivalent comment on `session-claim`'s budget has gone wrong before.
GIT_TIMEOUT_SECONDS = 2

# Blocking git calls on the reporting path, worst case:
#   git_dirs, default_branch, behind_count, origin_sha, current_head,
#   new_commits  -- six. `spawn_background_fetch` is a detached Popen and
#   `seconds_since_fetch` is a stat, so neither is charged here.
# `git_dirs` deliberately asks for BOTH values in one call; splitting it back
# into two would put this over budget.
MAX_GIT_CALLS = 6

DEFAULT_FETCH_INTERVAL_SECONDS = 4 * 3600
DEFAULT_MAX_COMMITS_LISTED = 5

# Records older than this are dropped. Keyed on wall-clock rather than session
# liveness on purpose: establishing liveness needs `ps`, and this file is read
# on every prompt submit in every session. A stale record costs one suppressed
# notice in a session that has been gone for a week.
STATE_TTL_SECONDS = 7 * 24 * 3600

DEFAULT_STATE_PATH = "~/.claude/base-pin-state.json"

# Unset for every git call. `GIT_DIR` is the load-bearing one, and it breaks
# detection in BOTH directions depending on what it points at. Measured
# 2026-08-26, `git rev-parse --git-dir --git-common-dir`, identical for the
# one-call and two-call forms:
#
#   GIT_DIR=<primary .git>,     cwd=worktree  -> both resolve there, EQUAL
#       -> a real worktree reads as the primary checkout: SILENCE, the
#          failure this whole tool exists to prevent.
#   GIT_DIR=<worktree admin>,   cwd=primary   -> --git-dir follows GIT_DIR,
#          --git-common-dir stays put, so they DIFFER
#       -> the primary checkout starts reporting drift that
#          `claude-repo-freshness` already owns: duplicate notices.
#
# Both directions have a test, and the false-negative one is the direction you
# do NOT reach for first: the obvious test — GIT_DIR at the worktree's own
# admin dir while standing in that worktree — lands in the differ case, where
# the worktree already differs, so it passes with the guard deleted. That test
# was written, passed, and its mutation SURVIVED. Do not collapse the two.
#
# `lib/worktree_detect.sh` documents this trap for the installer, and its own
# suite is MORE careful than this one's first cut was: its GIT_DIR test points
# the variable at the primary repo's `.git` — the equal-producing direction —
# and `test_only_git_dir_can_actually_cause_a_miss` asserts the mechanism at
# the git level rather than inferring it from a verdict. Only the prose in
# CLAUDE.md is ambiguous ("resolve to its target" reads as the worktree's admin
# dir, but only the common-`.git` value produces equality). Nothing to fix
# there; copy its test's shape, not this module's first attempt.
#
# `GIT_COMMON_DIR` and `GIT_WORK_TREE` are cleared as cheap defence, NOT as
# tested guarantees — same posture as the shell helper. Do not promote them,
# and do not delete `GIT_DIR` on the strength of their harmlessness.
_UNSET_GIT_VARS = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")

try:
    import toolkit_config as _config
except Exception:  # pragma: no cover - absent config must never raise
    _config = None


def _env():
    env = dict(os.environ)
    for var in _UNSET_GIT_VARS:
        env.pop(var, None)
    return env


def _git(cwd: str, *args: str) -> Optional[str]:
    """Run a read-only git command in `cwd`, or None on any failure.

    **An empty `cwd` returns None rather than running.** `git -C ""` is a
    silent no-op, not an error: git runs in the process's actual working
    directory and exits 0. Without this check the hook would answer
    confidently about whatever directory it happened to be launched from,
    while every docstring here promises None means "cannot tell".

    The same no-op has bitten this repo before — `freshness_lib._git` and
    `lib/worktree_detect.sh` — so it is guarded at the boundary rather than
    left to callers.
    """
    if not cwd:
        return None
    try:
        result = subprocess.run(
            ("git", "-C", cwd) + args,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=GIT_TIMEOUT_SECONDS, env=_env())
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def git_dirs(cwd: str) -> Optional[tuple]:
    """`(git_dir, common_dir)` as absolute paths, or None if not in a repo.

    One git call for both, because this runs on every prompt submit and it is
    the call that cannot be skipped.

    Both are resolved to absolute paths before any comparison. Either can come
    back relative depending on how git discovered the repo, so comparing them
    raw reports "linked worktree" for an ordinary checkout. Do not "harden"
    this by prefixing `./` — a worktree's `--git-dir` is absolute, and
    `.//abs/path` does not exist. That precise mistake broke the installer's
    equivalent check while leaving all its tests green.
    """
    out = _git(cwd, "rev-parse", "--git-dir", "--git-common-dir")
    if not out:
        return None
    lines = out.splitlines()
    if len(lines) != 2:
        return None
    try:
        return tuple(os.path.realpath(os.path.join(cwd, line.strip()))
                     for line in lines)
    except Exception:
        return None


def dirs_are_linked(dirs: Optional[tuple]) -> Optional[bool]:
    """PURE. The linked-worktree predicate, over an already-resolved pair.

    Split out so the HOOK can call it. The first cut kept only
    `is_linked_worktree(cwd)` and the hook inlined `git_dir == common_dir`,
    because it already holds the tuple and calling the cwd form would have
    spent a second git call against the timeout budget. That left the tested
    function with no production caller: inverting it failed only its own unit
    test while the hook went on behaving correctly, so the branch production
    actually takes had no assertion on it at all.

    A predicate over the tuple costs nothing and is the same code both ways.
    """
    if dirs is None:
        return None
    return dirs[0] != dirs[1]


def is_linked_worktree(cwd: str) -> Optional[bool]:
    """True in a linked worktree, False in a primary checkout, None if unknown.

    Three states, deliberately. `False` and `None` lead to the same behaviour
    today (silence) but mean different things, and collapsing them would make
    "not in a repo at all" indistinguishable from "in the primary checkout" for
    any future caller.

    Tests must use `assertIs(x, False)` and `assertIsNone(x)`, never
    `assertFalse` — both are falsy, so `assertFalse` passes for either and
    erases exactly the distinction this return type exists to carry.
    """
    return dirs_are_linked(git_dirs(cwd))


def default_branch(cwd: str) -> str:
    """The remote's default branch, falling back to `main`.

    `origin/HEAD` is a symbolic ref that a fresh clone has and a `worktree add`
    inherits. When it is missing — some clones never set it — `main` is the
    most common default, and the guess this falls back to.
    """
    ref = _git(cwd, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref and "/" in ref:
        return ref.split("/", 1)[1]
    return "main"


def behind_count(cwd: str, branch: str) -> Optional[int]:
    """How many commits `origin/<branch>` has that this HEAD does not.

    This is the count against the merge-base, not against the tip: `HEAD..X`
    means "reachable from X but not from HEAD" — which is what a merge would
    actually bring in, and the reason no pin file is needed.

    **Not an exact identity with `merge-base(HEAD, X)..X`, and the earlier
    docstring here overstated it.** With multiple merge bases git picks one:
    measured on a criss-cross history, `mine..main` = 1 while
    `$(git merge-base mine main)..main` = 2. `HEAD..X` is the *correct* number
    of the two — the pin-file design would have over-reported on exactly that
    history — so the code is right and only the justification was too strong.
    Worth stating precisely, because this is the sentence a future reader is
    pointed at when declining to reintroduce a pin.

    None when the ref is missing (never fetched), the repo is unreadable, or
    HEAD is unborn.
    """
    out = _git(cwd, "rev-list", "--count", "HEAD..origin/%s" % branch)
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def origin_sha(cwd: str, branch: str) -> Optional[str]:
    """Current `origin/<branch>` sha — the dedupe key, not a display value."""
    return _git(cwd, "rev-parse", "origin/%s" % branch)


def current_head(cwd: str) -> Optional[str]:
    """The checked-out branch name, or None. `"HEAD"` means detached.

    Display only — it names the branch in the notice. Nothing branches on it,
    so a detached HEAD is reported verbatim rather than special-cased: the
    count against `origin/<default>` is meaningful either way.
    """
    return _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")


def new_commits(cwd: str, branch: str, limit: int) -> list:
    """Up to `limit` one-line subjects of what landed, newest first.

    Cosmetic but load-bearing for whether the notice gets read: "3 commits
    ahead" is a number, and "#78, #79, #80" is a reason to act.
    """
    if limit <= 0:
        return []
    out = _git(cwd, "log", "--format=%h %s", "--max-count=%d" % limit,
               "HEAD..origin/%s" % branch)
    if not out:
        return []
    return out.splitlines()


def seconds_since_fetch(git_dir: str) -> Optional[float]:
    """Age of the last fetch, or None if this worktree has never fetched.

    Takes the PER-WORKTREE admin dir (`--git-dir`), which is the one place
    this can be read from, because it is the one place the write lands.

    **`FETCH_HEAD` is a per-worktree pseudo-ref.** Measured 2026-08-26 on git
    2.50.1: `git -C <worktree> fetch origin` writes
    `<common>/worktrees/<name>/FETCH_HEAD` and does NOT touch
    `<common>/FETCH_HEAD`; `git rev-parse --git-path FETCH_HEAD` from a
    worktree confirms it. A fresh `git clone` creates neither.

    The read and the write must therefore name the same git-dir or the clock
    never advances. `freshness_lib` gets this right by accident of symmetry —
    it joins `<repo>/.git/FETCH_HEAD` and fetches with `cwd = <repo>`, one
    git-dir for both. This tool's first cut split them: read from `<common>`,
    fetch with `cwd = <worktree>`. `should_fetch` then returned True forever
    and the hook spawned a detached `git fetch` on EVERY prompt submit, with
    no output and no way to notice. Measured with a `git` PATH shim: 4 prompt
    submits, 4 fetches.

    Using `git_dir` makes the two provably the same location and needs no
    extra git call, since the hook already holds it. The cost is that the
    clock is per-worktree rather than per-repo, so N worktrees each fetch once
    per interval instead of one between them. That is the honest trade and
    `config.example.json` says so; a shared clock would mean fetching against
    a primary-checkout path derived by stripping a component off `--git-dir`,
    which guesses wrong under `--separate-git-dir`.

    In a primary checkout `git_dir == common_dir`, so this is the same path
    `freshness_lib` reads and behaviour there is unchanged.
    """
    if not git_dir:
        return None
    try:
        mtime = os.path.getmtime(os.path.join(git_dir, "FETCH_HEAD"))
    except OSError:
        return None
    return time.time() - mtime


def should_fetch(age_seconds: Optional[float],
                 interval: Optional[int] = None) -> bool:
    """PURE. Whether a background refresh is due.

    None age means never fetched, which is when it matters most: with no
    `origin/<default>` on disk, `behind_count` can only answer "unknown".
    """
    if interval is None:
        interval = DEFAULT_FETCH_INTERVAL_SECONDS
    if age_seconds is None:
        return True
    return age_seconds >= interval


def spawn_background_fetch(cwd: str) -> bool:
    """Start a detached `git fetch` and return immediately.

    `start_new_session` detaches it from the hook's process group so it
    outlives the hook and can never hold the session open. Output goes
    nowhere: a fetch that fails is not worth a word, and the next prompt
    reports from whatever refs did land.

    Refuses an empty `cwd` for the same reason `_git` does, and with a worse
    consequence — this builds its own argv, so it does not inherit that guard.
    """
    if not cwd:
        return False
    try:
        subprocess.Popen(
            ("git", "-C", cwd, "fetch", "--quiet", "origin"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True, env=_env())
        return True
    except Exception:
        return False


# ---------------------------------------------------------------- config -----

def is_enabled() -> bool:
    """False only when explicitly disabled. Absent config means on."""
    if _config is None:
        return True
    try:
        return _config.get("base_pin", "enabled", default=True) is not False
    except Exception:
        return True


def fetch_interval() -> int:
    if _config is None:
        return DEFAULT_FETCH_INTERVAL_SECONDS
    try:
        value = _config.get_int("base_pin", "fetch_interval_seconds",
                                default=DEFAULT_FETCH_INTERVAL_SECONDS)
    except Exception:
        return DEFAULT_FETCH_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_FETCH_INTERVAL_SECONDS


def max_commits_listed() -> int:
    if _config is None:
        return DEFAULT_MAX_COMMITS_LISTED
    try:
        value = _config.get_int("base_pin", "max_commits_listed",
                                default=DEFAULT_MAX_COMMITS_LISTED)
    except Exception:
        return DEFAULT_MAX_COMMITS_LISTED
    return value if value >= 0 else DEFAULT_MAX_COMMITS_LISTED


# ----------------------------------------------------------------- state -----

def state_path() -> str:
    if _config is not None:
        try:
            configured = _config.get("base_pin", "state_path", default=None)
            if configured:
                return os.path.expanduser(configured)
        except Exception:
            pass
    return os.path.expanduser(DEFAULT_STATE_PATH)


def load_state(path: Optional[str] = None) -> dict:
    """The dedupe table, or an empty one. Never raises."""
    try:
        with open(path or state_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def prune_state(state: dict, now: Optional[float] = None) -> dict:
    """Drop records past `STATE_TTL_SECONDS`, and anything malformed.

    Malformed entries are dropped rather than preserved: this file is only ever
    a dedupe cache, so the cost of discarding a record is one repeated notice,
    while the cost of carrying a shape the reader does not expect is a raise
    inside a hook.
    """
    if now is None:
        now = time.time()
    clean = {}
    for session, checkouts in (state or {}).items():
        if not isinstance(checkouts, dict):
            continue
        kept = {}
        for checkout, record in checkouts.items():
            if not isinstance(record, dict):
                continue
            stamp = record.get("at")
            if not isinstance(stamp, (int, float)):
                continue
            if now - stamp > STATE_TTL_SECONDS:
                continue
            if not isinstance(record.get("sha"), str):
                continue
            kept[checkout] = {"sha": record["sha"], "at": stamp}
        if kept:
            clean[session] = kept
    return clean


def already_reported(state: dict, session: str, checkout: str,
                     sha: str) -> bool:
    """True when this exact `origin/<default>` sha was already announced here.

    Keyed on the sha rather than a boolean so the notice re-fires the next time
    main moves, and never repeats on an unchanged state. A boolean would say it
    once per session and then go quiet through every subsequent merge.
    """
    try:
        return state[session][checkout]["sha"] == sha
    except Exception:
        return False


def record_reported(state: dict, session: str, checkout: str, sha: str,
                    now: Optional[float] = None) -> dict:
    if now is None:
        now = time.time()
    state.setdefault(session, {})[checkout] = {"sha": sha, "at": now}
    return state


def save_state(state: dict, path: Optional[str] = None) -> bool:
    """Write the dedupe table. False on any failure, and the caller carries on.

    Written to a temp file and renamed, because several sessions write this on
    their own prompt submits and a torn file read by the next one would look
    like corrupt state rather than a race. A lost write costs one repeated
    notice, which is why there is no lock: the contention window is a rename.
    """
    target = path or state_path()
    tmp = target + ".tmp.%d" % os.getpid()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, target)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- render -----

_NOTICE = """=== origin/{branch} IS AHEAD OF THIS WORKTREE ===

{count} commit{plural} landed on origin/{branch} that this worktree does not have.
Worktree: {name}  (branch: {head})
{commits}
Every worktree of a repo shares one set of origin/* refs, so another session
merging moves this for all of them at once.

Merge before you push, or your PR will carry a stale base:
  git merge origin/{branch}
"""


def render(count: Optional[int], branch: str, name: str, head: str,
           commits: Optional[list] = None) -> Optional[str]:
    """The notice, or None when there is nothing to say.

    Says "is ahead of this worktree", never "moved since you started". Some of
    those commits may predate the session — `origin/<branch>` can already have
    been ahead when the worktree was created. "Ahead of this worktree" is true
    either way, and it is the fact that decides whether you must merge before
    pushing. The other phrasing is wrong in a way that trains the reader to
    distrust the notice, which costs more than saying nothing.
    """
    if not count or count <= 0:
        return None
    listed = ""
    if commits:
        listed = "\n" + "\n".join("  " + line for line in commits) + "\n"
    return _NOTICE.format(
        branch=branch, count=count, plural="" if count == 1 else "s",
        name=name or "unknown", head=head or "unknown", commits=listed)
