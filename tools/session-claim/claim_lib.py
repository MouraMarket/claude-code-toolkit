#!/usr/bin/env python3
"""Which checkout a file belongs to, and who currently holds it.

The unit of claim is the git toplevel. Two worktrees of one repo resolve to
DIFFERENT toplevels, so sessions in separate worktrees never contend -- the
tool rewards the workflow it exists to encourage rather than fighting it.
"""

# Design decisions about this file that look like accidental complexity are
# recorded in docs/decisions.md. Read the relevant one before simplifying
# anything here.

import contextlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime

import identity_lib

_LIB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
import portable

CLAIMS_PATH = os.path.expanduser("~/.claude/session-claims/claims.json")

# TIMEOUT BUDGET -- every wait charged against ONE claim-hook invocation.
#
# The ceiling is the "timeout": 15 on the session-claim entry in
# `plugin-build/hooks/hooks.json`. Exceed it and the harness kills the hook before
# it produces a verdict, so the write is silently ALLOWED -- the guard fails
# exactly when contention is real.
#
# The full worst case, which is NOT just the two constants below:
#
#   identity_lib.PS_TIMEOUT   x2   process_table() in resolve_owner, plus one
#                                  start_times() for the whole prune/holder
#                                  pass. resolve_owner no longer forks for the
#                                  owner's start time; the hook reads
#                                  it from that table.
#   LOCK_TIMEOUT              x2   the claims lock, then the breaker lock
#                                  inside record_block() on the blocking path.
#                                  Sequential, never nested -- the claims lock
#                                  is released before record_block is reached.
#   WORKTREE_TIMEOUT          x1   create_worktree takes exactly one of its
#                                  two git calls, never both
#   = 2 + 2 + 8 = 12s against a 15s ceiling
#
# The `ps` terms were once missing from this comment while
# being the LARGEST ones (5s each), and the per-record `proc_start()` inside
# prune() made the total grow without bound as claims accumulated. Both are
# fixed: `ps` is 1s, and prune shares a single start_times() map across all
# records, so the number of claim records is no longer a term at all.
# TestTimeoutBudget in tests/test_claim_lib.py asserts this arithmetic AND
# that the per-record fork count is zero. Change any of these constants and
# that test is where the relationship is checked.

# Bounded wait for the claims-file lock. Contention here means two sessions
# claiming in the same instant; if a full second is not enough, proceeding
# unlocked is the right answer -- waiting longer does not make it righter.
LOCK_TIMEOUT = 1.0

# Bounded wait for `git worktree add`. Well under a second on a normal repo;
# 8s covers a large one without eating the hook's remaining headroom.
WORKTREE_TIMEOUT = 8


def find_checkout(file_path):
    """The git toplevel containing file_path, or None.

    Pure Python, no subprocess. `git rev-parse --show-toplevel` forks a
    process and this runs on every file write; across a build with hundreds of
    edits that is a lot of pure latency.

    The directory containing `.git` IS the toplevel whether `.git` is a
    directory (ordinary checkout) or a file (worktree, submodule). Both cases
    fall out of the same walk, and the worktree case is the one the whole
    design depends on.
    """
    if not file_path:
        return None
    try:
        current = os.path.dirname(os.path.realpath(os.path.abspath(file_path)))
    except Exception:
        return None
    while True:
        try:
            if os.path.exists(os.path.join(current, ".git")):
                return current
        except OSError:
            return None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def load_claims():
    """Every claim record. Missing or corrupt file -> []."""
    try:
        with open(CLAIMS_PATH, encoding="utf-8") as handle:
            records = json.load(handle)
    except Exception:
        return []
    return records if isinstance(records, list) else []


def save_claims(records):
    """Persist. Never raises -- a hook must not fail on a write it cannot do."""
    try:
        os.makedirs(os.path.dirname(CLAIMS_PATH), exist_ok=True)
        with open(CLAIMS_PATH, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2)
    except Exception:
        pass


@contextlib.contextmanager
def locked(lock_path=None):
    """Serialise a load -> mutate -> save cycle across processes.

    Defaults to guarding CLAIMS_PATH; `lock_path` names a different lock file
    for a different piece of state. breaker_lib passes its own rather than
    carrying a second copy of this -- the fail-open contract below is subtle
    enough that two implementations of it would eventually disagree, and one of
    them would be the one running.

    Locks a dedicated file BESIDE the state file, never the state file itself
    -- that gets replaced wholesale on every save, and locking a file you
    replace is a classic way to lose the lock. Fails open: if the lock file's
    directory cannot be created, or the lock cannot be acquired within
    LOCK_TIMEOUT, this yields anyway rather than raising or hanging -- a rare
    lost update is better than a hook that blocks every write. Not safe to
    nest. The lock itself is `portable.exclusive_lock` -- the platform-specific
    primitive lives there and nowhere else.
    """
    stack = contextlib.ExitStack()
    try:
        lock_path = lock_path or (CLAIMS_PATH + ".lock")
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        stack.enter_context(portable.exclusive_lock(lock_path, timeout=LOCK_TIMEOUT))
    except Exception:
        pass  # fail open: a rare lost update beats a hook that blocks every write
    with stack:
        yield


def _is_live(record, starts=None):
    return identity_lib.is_pid_live(record.get("pid"), record.get("proc_start"),
                                    starts=starts)


def prune(records, starts=None):
    """Drop records whose owning process is gone.

    This is what makes lockout impossible. SessionEnd releases cleanly, but it
    does not fire on a crash, a force-quit, or a VS Code reload -- and sessions
    die that way routinely.

    `starts` is an optional identity_lib.start_times() map, shared across the
    whole pass so liveness costs ONE `ps` rather than one per record. Omit it
    and one is fetched here; pass the same map to holder() afterwards and the
    pair costs one fork between them. Empty input skips the fork entirely,
    which is the common case on a machine running a single session.
    """
    records = [r for r in records if isinstance(r, dict)]
    if not records:
        return []
    if starts is None:
        starts = identity_lib.start_times()
    return [r for r in records if _is_live(r, starts)]


def holder(records, checkout, owner_pid, starts=None):
    """A LIVE record for `checkout` owned by someone other than owner_pid."""
    for record in records:
        if not isinstance(record, dict):
            continue
        if record.get("checkout") != checkout:
            continue
        if record.get("pid") == owner_pid:
            continue
        if _is_live(record, starts):
            return record
    return None


def add_claim(records, checkout, owner, transcript_path):
    """Record this owner's claim on this checkout. Idempotent."""
    for record in records:
        if (isinstance(record, dict) and record.get("checkout") == checkout
                and record.get("pid") == owner.get("pid")):
            return records
    return list(records) + [{
        "checkout": checkout,
        "pid": owner.get("pid"),
        "proc_start": owner.get("proc_start"),
        "session_id": owner.get("session_id"),
        "name": owner.get("name"),
        "transcript_path": transcript_path,
        "claimed_at": datetime.now().isoformat(timespec="seconds"),
    }]


def release(records, pid):
    """Drop every claim held by `pid`."""
    return [r for r in records
            if not (isinstance(r, dict) and r.get("pid") == pid)]


def worktree_paths(checkout, owner):
    """(worktree_path, branch_name) for this owner in this checkout.

    Deterministic on (checkout, session): a repeat block reuses the same
    worktree instead of spawning another. The branch name is generic because
    the hook cannot know what is being built -- the block message tells Claude
    to `git branch -m` before opening a PR.
    """
    session_id = (owner.get("session_id") or str(owner.get("pid") or "0"))
    short = session_id[:8]
    basename = os.path.basename(checkout.rstrip(os.sep)) or "repo"
    path = os.path.join(checkout, ".claude", "worktrees", "claim-%s" % short)
    branch = "claim/%s-%s" % (basename, short)
    return path, branch


def _is_registered_worktree(checkout, path):
    """True if `path` is registered as a worktree of `checkout` in git.

    A directory existing at `path` is not enough -- `git worktree add` can be
    killed mid-populate (create_worktree's own timeout, a crash, a force-quit)
    and leave a partly-filled directory behind with no `.git` and no entry in
    git's own registry. Never raises; any error here means "not registered".
    """
    try:
        result = subprocess.run(
            ["git", "-C", checkout, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, encoding="utf-8", timeout=WORKTREE_TIMEOUT)
    except Exception:
        return False
    if result.returncode != 0:
        return False
    target = os.path.realpath(path)
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        registered = line[len("worktree "):].strip()
        try:
            if os.path.realpath(registered) == target:
                return True
        except Exception:
            continue
    return False


def create_worktree(checkout, path, branch):
    """Create the worktree. True on success or if it already exists.

    False on any failure, and the caller must then ALLOW rather than block:
    blocking a session with no working way forward is worse than the
    contamination this exists to prevent.
    """
    if os.path.isdir(path):
        return _is_registered_worktree(checkout, path)
    try:
        result = subprocess.run(
            ["git", "-C", checkout, "worktree", "add", "-b", branch, path],
            capture_output=True, text=True, encoding="utf-8", timeout=WORKTREE_TIMEOUT)
    except Exception:
        return False
    return result.returncode == 0 and os.path.isdir(path)


# The entry written into .git/info/exclude. Leading slash so it anchors at the
# repo root, trailing slash so it matches the directory and everything under it.
EXCLUDE_ENTRY = "/.claude/worktrees/"

EXCLUDE_COMMENT = "# added by claude-session-claim (worktree for another session)"


def _git_dir(checkout):
    """The directory git actually keeps this checkout's exclude file in, or None.

    Two things make this more than `os.path.join(checkout, ".git")`:

    - In a linked worktree `.git` is a FILE containing `gitdir: <path>`. The
      holder we are writing for may well be working in a worktree already.
    - git reads info/exclude from the COMMON directory, not the per-worktree
      one. Writing to a worktree's own gitdir would produce a file git never
      reads, and this fix would silently do nothing.

    Pure path resolution, no subprocess: `git rev-parse --git-common-dir` would
    answer the same question but adds another bounded git call to a hook that
    has just spent one on `git worktree add`. Any failure returns None and the
    caller skips silently.
    """
    try:
        dot_git = os.path.join(checkout, ".git")
        if os.path.isdir(dot_git):
            git_dir = dot_git
        elif os.path.isfile(dot_git):
            with open(dot_git, encoding="utf-8") as handle:
                content = handle.read().strip()
            if not content.startswith("gitdir:"):
                return None
            git_dir = content[len("gitdir:"):].strip()
            if not git_dir:
                return None
            git_dir = os.path.join(checkout, git_dir)   # no-op if absolute
        else:
            return None
        common = os.path.join(git_dir, "commondir")
        if os.path.isfile(common):
            with open(common, encoding="utf-8") as handle:
                relative = handle.read().strip()
            if relative:
                git_dir = os.path.join(git_dir, relative)
        return os.path.normpath(git_dir)
    except Exception:
        return None


def ensure_worktrees_ignored(checkout):
    """Add `/.claude/worktrees/` to <checkout>'s .git/info/exclude. Idempotent.

    The worktree is created for the BLOCKED session, but it lands inside the
    HOLDER's checkout -- a session that did nothing wrong now has an untracked
    `.claude/` in `git status`, and a routine `git add -A` stages it as an
    embedded gitlink. That artifact then rides into the holder's PR, which is
    precisely the class of failure this tool exists to prevent. Few repos
    carry a .gitignore rule for `.claude/`, so it is not hypothetical.

    info/exclude rather than .gitignore on purpose: it is repo-local and
    unversioned, so nothing appears in the holder's diff either. Their working
    tree comes back clean without them being handed a commit to make.

    Returns True when the entry is present afterwards, False when it could not
    be written. NEVER raises, and the caller must not treat False as a reason to
    fail the worktree creation -- a dirty `git status` is a nuisance; a session
    blocked with nowhere to go is a stoppage.
    """
    git_dir = _git_dir(checkout)
    if not git_dir:
        return False
    try:
        exclude_path = os.path.join(git_dir, "info", "exclude")
        if os.path.exists(exclude_path):
            with open(exclude_path, encoding="utf-8") as handle:
                existing = handle.read()
            if any(line.strip() == EXCLUDE_ENTRY
                   for line in existing.splitlines()):
                return True
        else:
            existing = ""
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        with open(exclude_path, "a", encoding="utf-8") as handle:
            handle.write("%s%s\n%s\n" % (prefix, EXCLUDE_COMMENT, EXCLUDE_ENTRY))
        return True
    except Exception:
        return False
