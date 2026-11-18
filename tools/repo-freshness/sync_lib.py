"""The pull engine behind `repo-freshness`'s sync phase.

`freshness_lib.py` answers "is this repo behind" without touching the network
on its hot path. This module is the other half: it is what actually runs
`git pull --ff-only` against a working tree, unattended, at session start —
which is why it earns a stricter posture than the read-only reporting side.

## Off by default, narrower on any doubt

Reporting a repo is behind costs nothing if it is wrong. Pulling one is not
reversible in the same way: a fast-forward on a repo the human meant to leave
alone is still a working-tree change nobody asked for. So every function here
follows the same rule the rest of this repo's config does — a missing or
malformed setting must make behaviour *narrower*, never wider. `sync_enabled()`
is the sharpest case: only a literal Python `True` turns this on. `"true"`,
`1`, and any other truthy value are OFF, because a config value that merely
*looks* like an opt-in must never BE one.

## This function does not derive a root

`sync_root()` returns `repo_freshness.sync.repo_root` or None. It does not fall
back to `<workspace_root>/repos`, to the bare workspace root, or to anything
else. The sync phase is inert until someone names a directory.

The history is worth keeping, because it is what makes the rule non-obvious.
The zsh tool this replaces (`claude-workspace-sync`) defaulted to
`<workspace_root>/repo` — singular — a spelling that existed in no real layout.
It resolved to a directory that was never there, discovered zero repos and
exited 0: a no-op indistinguishable from "nothing to sync". The obvious lesson
was "use the right spelling"; the actual lesson is that a *tool which writes*
has no business inferring where.

Workspaces differ, and neither common layout is wrong: some keep every clone
under `<workspace_root>/repos/`, others keep them flat directly under the root.
A fallback covering both would mean pulling every checkout beneath the
workspace root — including scratch worktrees — on a machine where nobody asked
for it. That is not the same mistake as the singular `/repo`, it is a worse
one: the first failed silently and changed nothing, this one would succeed
silently and change working trees.

Note the neighbouring guard, `claude-worktree-guard`, DOES carry a
two-candidate fallback, deliberately, and that is not an inconsistency to tidy
up. A guard that guesses right turns protection ON and a guess that misses is
inert; here the polarity is reversed. Compare what a tool DOES with the path
before reusing another tool's way of finding one.

## PATH hardening

This module executes `git` — and `gh` — rather than only
reading with it, unlike `freshness_lib`'s read-only helpers. `subprocess`
resolves a bare command name through the *inherited* `PATH`, so a hook that
skipped this would run whatever `git` a hostile or merely stale `PATH` entry
put in front of the real one. `PATH_ENV` is the explicit `env=` every
subprocess call in this module must pass instead.
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Optional, Tuple

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "lib"))
try:
    import toolkit_config as _config
except Exception:  # pragma: no cover - config is optional by design
    _config = None

# The hardened environment every subprocess call in this module must pass as
# `env=`. Built once at import time -- hooks are short-lived processes, so
# there is no staleness risk in capturing os.environ here rather than per call.
_SAFE_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

# GIT_DIR, GIT_WORK_TREE and GIT_COMMON_DIR redirect what `git -C <dir>`
# actually operates on -- silently. This repo's own `lib/worktree_detect.sh`
# names the same class of hazard as load-bearing: a stray GIT_DIR makes two
# DIFFERENT paths both resolve to its target, so a comparison between them
# reports "same repo" when they are not. Here the stakes are worse, because
# this module does not just compare -- it PULLS. Without stripping these, a
# caller could measure `repo` and fast-forward some other directory the
# environment happened to name.
#
# GIT_SSH_COMMAND is stripped for a related but distinct reason: git
# substitutes it for `ssh` on every SSH-transport network call, so a stray
# value there is a plain command-injection surface the moment a `pull` or
# `clone` reaches the network -- the same class of hazard PATH
# hardening exists to close, just aimed at the network leg instead of `git`
# itself.
#
# GIT_CONFIG_GLOBAL is deliberately NOT in this list. It can change git's
# DEFAULTS (credential helper, aliases, pull.rebase) but it cannot redirect
# which repo `-C` targets, which is the specific "measure one thing, act on
# another" hazard the three vars above close -- and the one place a config
# default of that shape would matter, the pull's merge strategy, is pinned
# by `--ff-only` on the command line, which a config default cannot
# override.
_STRIPPED_ENV_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_SSH_COMMAND")


def _hardened_env(base_env=None):
    """Build the hardened env from `base_env` (or the real `os.environ`).

    Split out from the `PATH_ENV` assignment below so a test can rebuild it
    against an arbitrary, tainted base environment. `PATH_ENV` itself is
    captured once, at import time -- by the time a test can set
    `os.environ`, that snapshot has already been taken, so this is the only
    seam that can prove the stripping actually happens.
    """
    env = dict(os.environ if base_env is None else base_env)
    env["PATH"] = _SAFE_PATH
    for var in _STRIPPED_ENV_VARS:
        env.pop(var, None)
    return env


PATH_ENV = _hardened_env()

# Bounds every LOCAL, read-only git call this module makes (rev-parse, status).
# Not the network budget for the pull/clone phase -- that is NET_TIMEOUT /
# NET_BUDGET, added in the task that adds the network calls.
#
# 1 second, not the original 5 (then 2): `TestTimeoutBudget` in the test
# suite pins this against the SessionStart hook's declared timeout in
# plugin/hooks/hooks.json, and `eligibility()` makes up to THREE of these
# calls per repo -- the 3x multiplier is what makes this constant the
# leverage point it is. It is the cheapest and safest term to cut: these are
# local, read-only calls that complete in milliseconds on any ordinary repo,
# unlike NET_TIMEOUT/NET_BUDGET below which bound real network calls and how
# many repos get checked per session. See that test for the full worst-case
# arithmetic.
GIT_TIMEOUT_SECONDS = 1

DEFAULT_THROTTLE_HOURS = 20


def _is_test_sandbox() -> bool:
    """True only when `CLAUDE_TOOLKIT_CONFIG` has been redirected away from
    the real per-machine config path.

    This is the same shape of signal `identity_lib.resolve_hook_owner()`
    uses for `CLAIM_TEST_OWNER_PID`, gated on `CLAIM_CLAIMS_PATH` (see
    `docs/decisions.md`, "CLAIM_TEST_OWNER_PID is gated on
    CLAIM_CLAIMS_PATH"): an unconditional bin
    override inside a module whose whole job is PATH hardening is a
    backdoor. This marker alone does NOT prove a sandbox: `CLAUDE_TOOLKIT_CONFIG`
    is also a real runtime override (the installer sets it), which is why
    `_test_bin_override()` adds a second, independent condition. Checked LIVE
    against `os.environ` on every call, not against `toolkit_config.CONFIG_PATH`
    -- the latter is resolved once at import time, and this module's own
    tests move it in-process via `ConfigTestCase`, which would never be
    reflected in a value captured before the test ran.
    """
    return bool(os.environ.get("CLAUDE_TOOLKIT_CONFIG"))


def _test_bin_override(env_var: str) -> Optional[str]:
    """`env_var`'s value, but ONLY when `_is_test_sandbox()` is true AND the
    value is an absolute path that resolves inside the system temp directory
    -- one ANDed gate, mirroring `identity_lib`'s `CLAIM_TEST_OWNER_PID` /
    `CLAIM_CLAIMS_PATH` compound. No part alone is enough: the marker is also
    a real runtime override, a relative name could resolve unpredictably, and
    an absolute path anywhere is exactly the hostile-PATH-adjacent case
    `PATH_ENV` exists to close. Every test shim lives in a `tempfile` dir;
    no real `git` or `gh` does.

    The limit, stated so nobody over-reads it: this stops a stray or stale
    variable from swapping the executable, not an attacker who already
    controls the hook's environment. Such an attacker can also set the
    `GIT_*` variables `PATH_ENV` does not strip.
    """
    if not _is_test_sandbox():
        return None
    value = os.environ.get(env_var)
    if not value or not os.path.isabs(value):
        return None
    temp_root = os.path.realpath(tempfile.gettempdir())
    resolved = os.path.realpath(value)
    if os.path.commonpath([temp_root, resolved]) != temp_root:
        return None
    return value


def _git_bin() -> str:
    """Which `git` executable every call in this module invokes.

    `SYNC_TEST_GIT_BIN` overrides it -- for tests only, gated by
    `_test_bin_override()`: only in a demonstrable test sandbox, and only
    with an ABSOLUTE path. `PATH_ENV` hardens `PATH` to a fixed system list,
    so a shim placed in a test's own temp directory would never be found by
    a bare `"git"` lookup; this is the seam that lets a test hand it in
    directly instead. A real hook invocation never redirects
    `CLAUDE_TOOLKIT_CONFIG`, so it always resolves the real system `git`
    through `PATH_ENV` exactly as before this seam existed.
    """
    return _test_bin_override("SYNC_TEST_GIT_BIN") or "git"


def _git_timeout() -> float:
    """Per-call bound for `_git`, seconds: `GIT_TIMEOUT_SECONDS`, unless
    `SYNC_TEST_GIT_TIMEOUT` overrides it in a test sandbox.

    The hook tests drive the real executable as a subprocess, so
    `_give_real_git_room()`'s in-process patch cannot reach it. Under
    local-ci's concurrent suites one local git call overran the 1s bound and
    a diverged repo read "could not read". Gated on
    `_is_test_sandbox()` like the bin overrides, and only a finite value
    above zero counts: anything else falls back to the real bound, because
    `subprocess.run(timeout=inf)` raises, and `_git` would turn that raise
    into a None on every call.
    """
    if _is_test_sandbox():
        try:
            value = float(os.environ.get("SYNC_TEST_GIT_TIMEOUT", ""))
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    return GIT_TIMEOUT_SECONDS


def _git(repo: str, *args: str) -> Optional[str]:
    """Run a read-only git command with the hardened PATH, or return None.

    Mirrors `freshness_lib._git`'s empty-path guard exactly, and for the same
    reason: `git -C ""` is not an error, it is a silent no-op that answers
    about the CURRENT WORKING DIRECTORY. Without this check, an unresolved
    repo path here would not fail -- it would confidently report on whatever
    directory the hook happened to be launched from.
    """
    if not repo:
        return None
    try:
        result = subprocess.run(
            (_git_bin(), "-C", repo) + args,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=PATH_ENV, timeout=_git_timeout())
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def sync_enabled() -> bool:
    """Whether the pull phase may run at all. `False` for anything but `True`.

    Absent config, `False`, `None`, and every truthy-but-non-bool value
    (`"true"`, `1`, `[]`) all read as OFF. Only a literal Python `True` --
    which only a JSON `true` deserialises to -- turns this on. A hook that
    rewrites working trees unattended must never arrive by upgrade because a
    string happened to be truthy.
    """
    if _config is None:
        return False
    try:
        value = _config.get("repo_freshness", "sync", "enabled", default=False)
    except Exception:
        return False
    return value is True


def sync_root() -> Optional[str]:
    """Where to look for repos to sync. CONFIGURED ONLY -- never derived.

    `repo_freshness.sync.repo_root`, or None. There is deliberately no fallback
    to `<workspace_root>/repos` or to anything else -- see the module docstring,
    "This function does not derive a root". None means the sync phase does
    nothing at all this session, which is the right answer for a tool nobody
    has told where to write.

    A configured path that is not an existing directory also returns None: the
    same rule one step later. A root that was moved or misspelled must make
    this narrower, not send a pull loop somewhere adjacent to where it meant.
    """
    if _config is None:
        return None

    try:
        root = _config.get_path("repo_freshness", "sync", "repo_root")
    except Exception:
        return None

    if not root or not os.path.isdir(root):
        return None
    return root


def throttle_hours() -> int:
    """How often the pull phase may run. Configured, default 20 hours.

    A non-int or a value `<= 0` both fall back to the default -- the same
    "narrower on doubt" rule as everywhere else here, since a bad value could
    otherwise mean "run every time" (0 or negative) rather than "run rarely".

    Validated LOCALLY against the raw value rather than through
    `toolkit_config.get_int()`: that shared helper calls `int(value)`, which
    SUCCEEDS -- it does not fall back -- for a JSON float like `1.5`
    (truncating to `1`) and for a numeric string like `"6"`. Both are
    genuinely different from the documented promise in `config.example.json`
    ("a non-integer value ... falls back to 20"), and other callers of
    `get_int` rely on its looser, coercing behaviour, so the stricter check
    belongs here rather than in the shared function. `bool` is excluded even
    though `isinstance(True, int)` is true in Python -- a stray `true` here
    must not silently become `1`.
    """
    if _config is None:
        return DEFAULT_THROTTLE_HOURS
    try:
        value = _config.get(
            "repo_freshness", "sync", "throttle_hours", default=DEFAULT_THROTTLE_HOURS)
    except Exception:
        return DEFAULT_THROTTLE_HOURS
    if not isinstance(value, int) or isinstance(value, bool):
        return DEFAULT_THROTTLE_HOURS
    return value if value > 0 else DEFAULT_THROTTLE_HOURS


def gh_owner() -> Optional[str]:
    """The GitHub owner to discover clones for, or None. NO default.

    Unlike every other getter in this module, absence here does not fall
    back to a value -- it falls back to "do not discover at all". See
    `discover_clones()`'s docstring for why: one `gh` login commonly sees
    more than one owner (a personal account that is also an organization
    member), so guessing an owner from an existing remote or `gh auth
    status` would risk silently sweeping a whole organization into
    someone's workspace. A
    non-string configured value (a number, a list, `True`) also reads as
    unset rather than being coerced.
    """
    if _config is None:
        return None
    try:
        value = _config.get("repo_freshness", "sync", "gh_owner", default=None)
    except Exception:
        return None
    return value if isinstance(value, str) and value.strip() else None


def candidate_repos(root: str) -> list:
    """Immediate subdirectories of `root` that are git checkouts. Never recurses.

    Sorted so the order is deterministic across runs -- both for tests and so
    a throttled/budgeted pull phase processes repos in a stable order
    rather than whatever `os.listdir` happens to return.
    """
    if not root:
        return []
    try:
        names = os.listdir(root)
    except Exception:
        return []
    repos = []
    for name in names:
        path = os.path.join(root, name)
        if os.path.isdir(path) and os.path.isdir(os.path.join(path, ".git")):
            repos.append(path)
    return sorted(repos)


def eligibility(repo: str) -> Optional[str]:
    """None when `repo` can be fast-forwarded; otherwise why it cannot be.

    Checked in this order -- could not read, detached HEAD, uncommitted
    changes, no upstream -- and every check is local and read-only: **no
    network call is made here.** That matters for the pull phase this feeds:
    a repo eligibility rejects is skipped before it ever reaches the network,
    which is also what lets it be reported as "tried and could not" rather
    than silently ignored.

    "could not read" is its own reason, not folded into "detached HEAD" or
    "uncommitted changes". `_git` returns `None` for BOTH "the answer is no"
    and "the call timed out / failed" -- and this function is the one place
    that has to tell those apart, because `render()` prints these reasons to
    the human verbatim, including "nothing was changed in this repo" for the
    two real states. A git call that merely timed out must never be reported
    as a specific, confident, WRONG state. Same rule as
    `freshness_lib.commits_behind`'s docstring: "None means 'cannot tell' ...
    every caller must treat that as 'say nothing'" -- here, "say nothing"
    means naming the uncertainty itself rather than picking one of the real
    states to stand in for it.

    The third check reads the upstream via `for-each-ref` rather than
    `rev-parse @{u}` for exactly this reason: `rev-parse @{u}` FAILS (returns
    None from `_git`) both when there is genuinely no upstream configured
    and when the git call itself errors -- the two cases this function must
    tell apart would collapse into the same None. `for-each-ref` succeeds
    (exit 0) either way, with an empty `%(upstream)` when none is configured,
    so a real failure and a real "no upstream" stay distinguishable.
    """
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return "could not read"
    if branch == "HEAD":
        return "detached HEAD"

    status = _git(repo, "status", "--porcelain")
    if status is None:
        return "could not read"
    if status != "":
        return "uncommitted changes"

    upstream = _git(repo, "for-each-ref", "--format=%(upstream)",
                     "refs/heads/{0}".format(branch))
    if upstream is None:
        return "could not read"
    if not upstream:
        return "no upstream"

    return None


# Bounds a single network-touching git call (`pull`, `clone`, and `gh repo
# list`). Distinct from GIT_TIMEOUT_SECONDS above, which bounds the local,
# read-only calls `eligibility()` makes -- those never reach the network, so
# they do not need to tolerate a slow remote.
#
# 4 seconds, not the original 8 -- held here, not cut further, when the
# declared hook timeout came down to 15. `NET_BUDGET` and `GIT_TIMEOUT_SECONDS`
# absorbed that cut instead: an over-tight per-call network timeout turns an
# ordinary-but-slow connection into a failed pull, and a failed pull is now
# REPORTED to the human, so cutting here converts a quiet success into visible
# noise. `TestTimeoutBudget` pins this constant, plus NET_BUDGET below,
# against the SessionStart hook's declared timeout -- see that test for the
# worst-case arithmetic that set both numbers.
NET_TIMEOUT = 4

# Bounds the WHOLE pull phase, not any one call. A per-call timeout alone does
# not bound the phase: NET_TIMEOUT x repo_count grows without limit as the
# candidate list grows, so a slow or unreachable remote could still turn every
# session start into a multi-minute stall. `run()` checks this before
# starting each repo and stops rather than working through the rest.
#
# 6 seconds (history: 15, then 4, then 2, then 6). SessionStart is the one
# event a human actually waits through, which is why this was cut to 2; that
# cut starved the phase in practice. Measured 2026-11-10 on a 10-repo
# workspace over SSH: `gh repo list` 0.75s and ~0.7-2.1s per pull
# round-trip, so the then-serial loop reached one or two repos per run --
# always the same alphabetical head, and with a 20-hour throttle the tail
# was never pulled at all (12 days stale when found). Pulls are now
# concurrent (`MAX_PARALLEL_PULLS`), so admission costs only the local
# eligibility checks plus waiting for a free slot: 6 seconds admits a
# typical workspace in one pass and the phase ends in about one round-trip.
#
# A repo the budget does not reach is NOT reliably picked up next session:
# candidate order is sorted and the throttle is long, so a workspace too big
# for one pass starves the same tail every run. Raising capacity hides that
# rather than removing it -- rotating the start point would remove it.
#
# The deadline is checked only BETWEEN items -- never during one -- so it caps
# how much NEW work can START, not the total wall-clock time of one `run()`
# call: the last item admitted before the deadline can still add its own full
# GIT_TIMEOUT_SECONDS x 3 + NET_TIMEOUT on top, and earlier pulls (started
# sooner, each bounded by NET_TIMEOUT) end before it does. Do not read a
# declared hook timeout out of this comment: the tests read it live from the
# plugin's hook manifest. `TestTimeoutBudget` is the arithmetic that keeps
# the picture inside that timeout, and `TestRunStopsAdmittingAtTheDeadline`
# is what pins that admission really stops; change this constant only
# alongside both.
NET_BUDGET = 6

# How many `git pull`s may be in flight at once. Measured 2026-11-10: 8
# concurrent SSH round-trips to GitHub through an SSH agent took 2.24s
# against ~14s serially -- the agent did not serialise them. Not a config
# key: it is a politeness bound on concurrent connections to one host, not a
# personal preference.
MAX_PARALLEL_PULLS = 8


def _net_timeout() -> float:
    """Per-call network timeout, seconds. `SYNC_TEST_NET_TIMEOUT` overrides it
    for tests only; a real hook invocation never sets it."""
    override = os.environ.get("SYNC_TEST_NET_TIMEOUT")
    if override:
        try:
            return float(override)
        except (TypeError, ValueError):
            pass
    return NET_TIMEOUT


def _net_budget() -> float:
    """Whole-phase wall-clock budget, seconds. `SYNC_TEST_NET_BUDGET`
    overrides it for tests only; a real hook invocation never sets it."""
    override = os.environ.get("SYNC_TEST_NET_BUDGET")
    if override:
        try:
            return float(override)
        except (TypeError, ValueError):
            pass
    return NET_BUDGET


def _now() -> float:
    """The module's only clock read for deadline/budget bookkeeping --
    `discover_clones()` and `run()` both call this instead of
    `time.monotonic()` directly, so a test can drive the deadline logic by
    monkeypatching `sync_lib._now` (`mock.patch.object(sync_lib, "_now", ...)`)
    rather than sleeping for real seconds. A real hook invocation never
    overrides it, so this is exactly `time.monotonic()` in production. Kept
    as a plain function, not a class, so `mock.patch.object` is enough.
    """
    return time.monotonic()


def _gh_bin() -> str:
    """Which `gh` executable `discover_clones()` invokes.

    `SYNC_TEST_GH_BIN` overrides it -- for tests only, gated by
    `_test_bin_override()` exactly like `_git_bin()` above: only in a
    demonstrable test sandbox, and only with an ABSOLUTE path. `PATH_ENV`
    hardens `PATH` to a fixed system list, so a shim placed in a test's own
    temp directory would never be found by a bare `"gh"` lookup. A real hook
    invocation never redirects `CLAUDE_TOOLKIT_CONFIG`.
    """
    return _test_bin_override("SYNC_TEST_GH_BIN") or "gh"


def _gh_repo_names(owner: str) -> list:
    """`gh repo list <owner> -L 200 --json name --jq '.[].name'`, one repo
    name per line of stdout. Any failure -- `gh` absent, non-zero exit, a
    timeout, or output that does not decode -- returns `[]` rather than
    raising; `discover_clones()` treats an empty list the same whether it
    means "no repos" or "could not ask".
    """
    try:
        result = subprocess.run(
            (_gh_bin(), "repo", "list", owner, "-L", "200",
             "--json", "name", "--jq", ".[].name"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=PATH_ENV, timeout=_net_timeout())
    except Exception:
        return []
    if result.returncode != 0:
        return []
    try:
        text = result.stdout.decode("utf-8", "replace")
    except Exception:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def _clone(owner: str, name: str, dest: str) -> bool:
    """`gh repo clone <owner>/<name> <dest>`, bounded by `NET_TIMEOUT`.

    `gh repo clone` rather than a bare `git clone` against a constructed URL,
    because `gh` is what the machine is already authenticated through --
    reusing it means clone discovery works the same way for a private repo
    as `_gh_repo_names()`'s own listing call did. Any failure returns
    `False` rather than raising; the caller decides what that means for the
    overall discovery result.
    """
    try:
        result = subprocess.run(
            (_gh_bin(), "repo", "clone", "{0}/{1}".format(owner, name), dest),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=PATH_ENV, timeout=_net_timeout())
    except Exception:
        return False
    return result.returncode == 0


def discover_clones(root: str, owner: Optional[str],
                     deadline: Optional[float] = None) -> Tuple[list, int]:
    """Clone every repo under `owner` on GitHub that has no directory of the
    same name under `root` yet. Returns `(cloned, skipped)`: `cloned` is the
    list of newly-cloned paths; `skipped` counts discovered names that were
    NOT attempted because `deadline` had already passed.

    Returns `([], 0)` IMMEDIATELY when `owner` is falsy -- no `gh` call of any
    kind happens in that case. This is the single most important property in
    this function: `owner` is never inferred from an existing remote, git
    config, or `gh auth status`, because one `gh` login commonly sees more
    than one owner (a personal account that is also an organization member),
    and a guessed owner would silently sweep a whole organization into
    someone's workspace.

    An existing directory under `root` is skipped whether or not it is a git
    checkout -- `discover_clones()` only ever adds, it never inspects or
    touches something already there. Any failure along the way -- `gh`
    absent, a non-zero exit from either the list or the clone call, a
    timeout, or unparseable output -- degrades to `[]` (or to simply skipping
    that one name) rather than raising; a clone phase that cannot complete is
    not a reason to fail the pull phase that follows it in `run()`.

    `deadline` is a `time.monotonic()` value shared with `run()`'s own pull
    loop, checked BEFORE each individual clone -- the same idiom the pull loop
    uses before each repo. `None` means unbounded (the default, so a direct
    caller / test not passing one gets an unbounded run). Once the
    deadline has passed, every name from that point on is counted in
    `skipped` and NOT attempted -- this is what lets a human reading the
    result tell "there was nothing new to clone" (`skipped == 0`) apart from
    "we ran out of time before cloning" (`skipped > 0`). The `gh repo list`
    call itself is NOT gated by `deadline`, matching how `candidate_repos()`
    is not gated by it in the pull loop either: it is a single, necessary,
    already-`NET_TIMEOUT`-bounded call to learn what there is to do, not a
    per-item cost that can grow with the candidate count.
    """
    if not owner or not root:
        return [], 0
    names = _gh_repo_names(owner)
    cloned = []
    skipped = 0
    for index, name in enumerate(names):
        if deadline is not None and _now() >= deadline:
            skipped += len(names) - index
            break
        dest = os.path.join(root, name)
        if os.path.exists(dest):
            continue
        if _clone(owner, name, dest):
            cloned.append(dest)
    return cloned, skipped


@dataclass
class SyncResult:
    """The outcome of one call to `run()`.

    `synced` and `cloned` are plain repo paths. `blocked` and `failed` are
    `(repo path, reason)` pairs: `blocked` for a repo `eligibility()` rejected
    before any network call ever happened, `failed` for one that WAS attempted
    -- a real `git pull --ff-only` refusal (e.g. a diverged repo), a non-zero
    exit, or a timeout. `unchecked` counts repos the whole-phase `NET_BUDGET`
    cut off before the PULL loop looked at them at all -- not synced, not
    blocked, not failed, because nothing was learned about them this run.
    `cloned_unchecked` is the same idea for the CLONE phase: names `gh repo
    list` returned that were never attempted because the budget ran out
    before `discover_clones()` reached them. The two are counted separately
    -- one over local repos already on disk, the other over remote names that
    do not exist on disk yet -- rather than merged into one number, so a
    human reading the result is never left guessing which phase ran out of
    time, especially in the case where both hit zero in the same run.
    """
    synced: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    cloned: list = field(default_factory=list)
    unchecked: int = 0
    cloned_unchecked: int = 0


def _pull(repo: str) -> Tuple[bool, str]:
    """Run `git pull --ff-only --quiet` against `repo`. Returns `(ok, message)`.

    `--ff-only` is this module's entire safety contract: never a merge, never
    a rebase, never a push, never a force. A non-zero exit -- including the
    ordinary "not possible to fast-forward" refusal on a diverged repo -- or a
    timeout both come back as `(False, message)`; `git` is never retried and
    never escalated to anything stronger by this function or its caller.
    """
    if not repo:
        return False, "empty repo path"
    try:
        result = subprocess.run(
            (_git_bin(), "-C", repo, "pull", "--ff-only", "--quiet"),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=PATH_ENV, timeout=_net_timeout())
    except subprocess.TimeoutExpired:
        return False, "timed out after {0}s".format(_net_timeout())
    except Exception as exc:
        return False, "could not run git: {0}".format(exc)
    if result.returncode == 0:
        return True, ""
    message = (result.stderr or result.stdout or b"").decode("utf-8", "replace").strip()
    return False, message or "git pull failed (exit {0})".format(result.returncode)


def run(root: str, owner: Optional[str] = None) -> SyncResult:
    """The bounded pull phase: fast-forward every eligible repo under `root`.

    Every repo `eligibility()` rejects lands in `blocked` WITHOUT a network
    call -- `eligibility()` is entirely local and read-only, so
    nothing here ever reaches the network for a repo this loop is about to
    skip. Everything else gets exactly one `git pull --ff-only --quiet`,
    bounded by `NET_TIMEOUT`; a non-zero exit or a timeout lands it in
    `failed` and it is never retried within this call.

    The loop is additionally bounded by `NET_BUDGET` -- but that budget covers
    the WHOLE call, not just this loop. `deadline` is computed once, at the
    very top of this function, BEFORE `discover_clones()` runs, and the same
    value is passed into it and reused here. This matters because this
    function is invoked from a SessionStart hook: a human is looking at a
    prompt that has not appeared yet for as long as `run()` takes, and cloning
    is both the FIRST phase to run and the slower one per call. Two deadlines
    -- one for cloning, a fresh one for pulling -- would let an owner with many
    new repos blow through the intended whole-phase bound before a single
    pull is even attempted; one shared deadline is what keeps that bounded.
    Checked BEFORE starting each repo -- not after. Once the deadline has
    passed, every repo not yet looked at is counted in `unchecked` and the
    loop stops immediately, rather than working through the rest at whatever
    cost a per-call timeout alone would allow. `discover_clones()`'s own
    `skipped` count is the same idea, for the names it did not get to.

    Never raises. A root that disappears mid-run, a repo whose `.git` cannot
    be read, `git` itself being absent from `PATH_ENV`, and a deadline already
    exhausted by the clone phase before the pull loop even starts all resolve
    quietly (into `blocked`/`failed` for a repo, or into `unchecked` for the
    whole remaining list) rather than taking the whole run down.

    `owner` feeds `discover_clones()` FIRST, before the pull loop -- a
    freshly cloned repo is already at the remote's HEAD, so there is nothing
    for the pull loop to do with it, and `discover_clones()` itself makes
    zero `gh` calls when `owner` is falsy. `discover_clones()` never raises
    on its own, but the call is still wrapped here: a clone phase that cannot
    complete must never take down the pull phase that follows it. The
    ordering (clone before pull) is unchanged by the shared deadline -- the
    fix is bounding the clone phase, not deprioritising it.

    This function used to also accept `now`, unused by anything in its body.
    It was removed rather than left as a documented no-op parameter --
    `throttle_due()` and `record_run()` are where a clock actually matters,
    and each keeps its own `now` for testability.
    """
    result = SyncResult()
    deadline = _now() + _net_budget()
    try:
        result.cloned, result.cloned_unchecked = discover_clones(root, owner, deadline=deadline)
    except Exception:
        pass
    try:
        repos = candidate_repos(root)
    except Exception:
        return result

    # Pulls run concurrently, up to MAX_PARALLEL_PULLS at once. Admission is
    # still serial and still deadline-gated exactly as before: wait for a
    # free slot, THEN check the deadline, then run the local eligibility
    # checks, then hand the pull to a worker. Waiting for a slot BEFORE the
    # check is what keeps the bound honest -- checking first and then
    # blocking on a full pool would admit a repo long after the deadline.
    # Outcomes are recorded per repo and emitted in candidate order at the
    # end, so `synced`/`failed` stay deterministic whatever order the
    # workers happen to finish in.
    outcomes = {}

    def _record(future, repo):
        try:
            ok, message = future.result()
        except Exception as exc:
            ok, message = False, str(exc)
        outcomes[repo] = (ok, message)

    in_flight = {}
    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_PULLS) as pool:
        for index, repo in enumerate(repos):
            if len(in_flight) >= MAX_PARALLEL_PULLS:
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    _record(future, in_flight.pop(future))
            if _now() >= deadline:
                result.unchecked += len(repos) - index
                break
            try:
                reason = eligibility(repo)
            except Exception:
                result.blocked.append((repo, "could not read"))
                continue
            if reason is not None:
                result.blocked.append((repo, reason))
                continue
            in_flight[pool.submit(_pull, repo)] = repo
        for future in list(in_flight):
            _record(future, in_flight.pop(future))

    for repo in repos:
        if repo in outcomes:
            ok, message = outcomes[repo]
            if ok:
                result.synced.append(repo)
            else:
                result.failed.append((repo, message))
    return result


def stamp_path() -> str:
    """Where the throttle records the last time the sync phase ran.

    Built from `os.path.expanduser("~")` rather than a hardcoded path, so it
    honours `$HOME` -- both for a real per-user machine and for a test that
    points `$HOME` at a sandbox before calling `throttle_due()`/`record_run()`.
    """
    return os.path.join(os.path.expanduser("~"), ".cache", "claude-repo-sync.stamp")


def throttle_due(now: Optional[float] = None) -> bool:
    """Whether the sync phase may run again. Fails toward `True` (due) on any
    doubt: an absent stamp, one that cannot be read, one whose contents are
    not a number, and one dated in the FUTURE all mean due, alongside the
    ordinary case of a stamp simply older than `throttle_hours()`.

    This is the deliberately safe direction the plan names explicitly: an
    extra fast-forward pull is idempotent and cheap, while silently never
    syncing again because a stamp got corrupted is the outcome worth
    avoiding. A future-dated stamp cannot be trusted to mean anything --
    treating it as "just synced, nothing to do" would be trusting exactly the
    kind of corruption this function exists to fail safe against.
    """
    when = now if now is not None else time.time()
    try:
        with open(stamp_path(), encoding="utf-8") as handle:
            raw = handle.read().strip()
    except Exception:
        return True
    try:
        stamped = float(raw)
    except (TypeError, ValueError):
        return True
    if stamped > when:
        return True
    age_hours = (when - stamped) / 3600.0
    return age_hours >= throttle_hours()


def record_run(now: Optional[float] = None) -> None:
    """Record that the sync phase just ran, for `throttle_due()` to read next
    time. Called by the hook AFTER the sync phase, whatever its outcome.

    Best-effort: an unwritable cache directory (missing, read-only, whatever)
    does not raise. It also must not retroactively undo the sync phase that
    already ran -- this function only ever records a fact about the past, so
    a failure here just means the NEXT invocation reads `throttle_due()` as
    `True` again, which is the same safe-on-doubt direction `throttle_due()`
    itself takes.
    """
    when = now if now is not None else time.time()
    path = stamp_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(str(when))
    except Exception:
        pass
