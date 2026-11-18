"""Is the installed toolkit behind origin? Answered without blocking the prompt.

The toolkit's plugins load from this checkout (a local directory marketplace),
and `~/.local/bin/*` are per-file symlinks into its working tree. A `git pull`
therefore changes what runs
at the next session start — and, just as importantly, NOT pulling changes nothing
visible. Staleness here has no symptom: hooks keep working, they are simply the
old ones. The sharp case is `claude-danger-guard`, a safety tool that can sit
weeks out of date while looking perfectly healthy.

## Why the network call is not on the hot path

Reading how far behind we are needs no network — `git rev-list` counts against
refs already on disk. Only *refreshing* those refs does. So the hook reports from
the last known state and spawns a detached `git fetch` for next time. Worst case
is a stale reading; there is no case where a slow network delays a prompt, which
is the trade this repo's hook rules demand.

The throttle keys off `.git/FETCH_HEAD`'s mtime rather than a state file of our
own: git already records exactly "when did we last fetch", and a second source of
truth for that could only ever disagree with the first.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional, Tuple

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "lib"))
try:
    import toolkit_config as _config
except Exception:  # pragma: no cover - config is optional by design
    _config = None

# Four hours: frequent enough that an update lands the same working day, rare
# enough that a laptop opening twenty sessions does not fetch twenty times.
DEFAULT_FETCH_INTERVAL_SECONDS = 4 * 3600

# Every git call is capped. A hook may not hang on a wedged repo or a network
# filesystem, and returning "unknown" is always an acceptable answer here.
#
# 2, not the original 5 -- sized for the REPORT loop's worst case
# (`measure_repos` makes up to 4 blocking git calls per watched repo:
# `default_branch`, `commits_behind`, `commits_ahead`, `current_branch`),
# which nothing bounded before a whole-phase `Deadline` was wired around it.
# The arithmetic that sets this value, shared against one declared hook
# timeout alongside the sync phase's own budget:
#
#   sync_end   <= sync_lib.NET_BUDGET + sync_tail + gh_repo_list   = 6 + 7 + 4 = 17
#   report_end <= max(sync_end, REPORT_DEADLINE_SECONDS 13) + 4 x GIT_TIMEOUT_SECONDS
#              =  17 + 8 = 25   <=   declared 30 - MARGIN 2 = 28    (headroom 3)
#
# `TestWholeHookTimeoutBudget` in `tools/repo-freshness/tests/
# test_sync_lib.py` is what actually ENFORCES this arithmetic, not this
# comment -- it asserts across both this module and `sync_lib`. It checks
# constants and call counts only; that `run()` really stops ADMITTING work
# at the deadline is pinned separately, by `TestRunStopsAdmittingAtTheDeadline`.
# `freshness_lib` has no phase budget of its own -- the sync phase's budget
# is `sync_lib.NET_BUDGET`, and this module's own bound is only the
# per-repo `Deadline` the REPORT loop is wrapped in.
#
# `tools/merge-brief/merge_brief_lib.py` is an OUT-OF-TOOL caller of this
# module: it puts this tool's directory on `sys.path`, imports this module,
# and re-exports `default_branch` rather than reimplementing branch
# resolution. It defines its own, looser `GIT_TIMEOUT_SECONDS = 5`. Lowering
# the constant here must never silently narrow THAT timeout -- `claude-merge-
# brief` is what builds the blinded review briefs this repo's merge gates
# depend on, and it now passes its own value explicitly at the
# `default_branch(repo, timeout=...)` call site instead of inheriting this one.
GIT_TIMEOUT_SECONDS = 2

# Which repos are watched, and why each one earns a line at session start.
#
# The test is NOT "is this repo important" — it is "would being behind on it go
# unnoticed". A repo you WORK in announces its own staleness: you branch off
# origin, you fetch, a PR shows you the conflict. A repo you CONSUME says
# nothing at all, and that silence is the whole problem.
#
#   toolkit  - consumed as behaviour. Hooks, skills and agents load from the
#              working tree, so stale means running old code, danger guard
#              included.
#   generic  - anything you add to `watch`. Read or run from, so stale means
#              it may be acting on out-of-date information.
#
# A repo you work in every day does not belong in `watch`: it self-reports.
# Neither does a plugin marketplace Claude Code keeps its own clone of under
# ~/.claude/plugins/marketplaces/ — a separate editing checkout of the same
# repo changes nothing that loads, so its staleness changes nothing at all.
KIND_TOOLKIT = "toolkit"
KIND_GENERIC = "generic"

_KIND_WHY = {
    KIND_TOOLKIT: ("hooks, skills and agents load from this checkout and its "
                   "~/.local/bin tools are symlinks into it, so this "
                   "session is running the OLDER code"),
    KIND_GENERIC: ("this repo is read or run from, so it may be acting on "
                   "out-of-date information"),
}

# Anything a user adds to `watch` gets the generic reason above; nothing is
# named here by default. `watched_repos` still looks up through this map, so
# a specific reason for a specific repo name has a place to go. A vague reason
# is still worth printing — what makes the notice useful is naming the repo
# and the command, not the prose.
_KIND_BY_NAME = {}

# Only the toolkit needs install.sh: a pull refreshes existing symlinks by
# itself, but a newly added tool has nothing pointing at it until the installer
# runs. The toolkit's command is the installer's update mode, which pulls and
# installs in one step. Nothing links to a watched repo, so a plain pull is the
# whole story.
_KIND_COMMAND = {
    KIND_TOOLKIT: "{repo}/install.sh --update",
}
_DEFAULT_COMMAND = "git -C {repo} pull"

# Repos watched when config says nothing: none. The toolkit itself is always
# checked (it is the repo the hook lives in, so it is found rather than
# configured). Naming anything else here would nag every fresh install about
# a repo it has never heard of, and discovering "every repo under repos/"
# would sweep in the ones that self-report; a notice people learn to skim is
# worse than no notice. Add your own via `repo_freshness.watch`.
DEFAULT_WATCH = ()

_HEADER = "=== REPO UPDATE{plural} AVAILABLE ==="

# The footer used to end "it is informational, not an error, and not a reason to
# stop what they asked for" — and nothing else. Read literally, that says: report
# this and move on. Which is exactly what happened, every session,
# while the toolkit sat a commit behind. The notice was designed to be ignored.
#
# So it now names the action too. The distinction it has to carry is narrow: do
# not *stop* the work to pull, and do not pull silently either — offer it, in one
# sentence, alongside whatever was actually asked. Every command above is a
# fast-forward plus an installer, which is the rare case where the answer is
# always yes and the only cost of asking is the asking.
#
# What this deliberately does NOT do is act on its own. A hook that rewrites a
# working tree while nobody is looking is the thing this tool has always refused
# to be, and a session-start notice is the worst possible moment for it — the
# human has not said anything yet. Offering is the whole change.
_FOOTER = """
Say this to {owner} once, near the start, and offer to run the command(s) above in
the same breath — they are fast-forwards, so the answer is nearly always yes, but
it is still theirs to give. Then carry on with what they actually asked for: this
is a one-line offer, not a reason to stop. Do not repeat it, and do not pull
without being told to.
"""

# The footer above promises a fast-forward. That promise is only sound when the
# checkout has no commits of its own — `commits_behind` counts HEAD..origin/main
# and is blind to the other direction, so a DIVERGED checkout produced the same
# "N behind" and the same confident offer, for a pull that is really a merge (or
# a hard failure on a dirty tree). Pressing an offer you have mis-described is
# worse than the silence this footer was written to replace, because the human
# says yes to the thing you named rather than the thing that happens.
_FOOTER_DIVERGED = """
Say this to {owner} once, near the start. **Do NOT offer a plain pull for the
diverged repo(s) above** — a pull there is a merge, not a fast-forward, and it
fails outright on a dirty tree. Say it has local commits that origin does not,
and let {owner} decide between merging, rebasing, or leaving it alone; that
choice is theirs and nothing here can make it safely. Any repo listed WITHOUT
"diverged" is an ordinary fast-forward and can be offered normally. Then carry
on with what they actually asked for: this is a one-line note, not a reason to
stop. Do not repeat it, and never pull without being told to.
"""

# The footer for a notice that is BLOCKED-only — no stale report survives the
# blocked exclusion, so there are no commands above it at all. `_FOOTER` and
# `_FOOTER_DIVERGED` both open with "offer to run the command(s) above" or
# point at a specific diverged one; neither is true here, and offering a pull
# for a repo the sync phase just could not fast-forward is the same failure
# `_FOOTER_DIVERGED` exists to prevent, arriving by a different route. What is
# actionable instead is reaching a clean state, and that is the human's call,
# not something this notice can do for them.
_FOOTER_BLOCKED = """
Say this to {owner} once, near the start: the repo(s) above were left
untouched — either the sync phase never attempted them, or it tried and a
fast-forward was refused; either way nothing changed. Getting to a clean
state is {owner}'s call: commit or stash local changes, reattach a detached
HEAD, or resolve a diverged or refused pull by hand, whichever applies. Then
carry on with what they actually asked for: this is a one-line note, not a
reason to stop. Do not repeat it, and do not pull without being told to.
"""

# The footer for a notice that is PURE time-budget — no stale report, no
# blocked repo, no failed pull survives, only a nonzero `unchecked` and/or
# `cloned_unchecked`. Neither `_FOOTER` nor `_FOOTER_BLOCKED` fits: there is
# nothing above to offer a command for, and nothing above was refused either
# — the honest claim is narrower than both, "we simply did not get to it".
_FOOTER_TIME_BUDGET = """
Say this to {owner} once, near the start: the sync phase ran out of time
before finishing this session — nothing was changed either way, it simply
was not reached. Then carry on with what they actually asked for: this is a
one-line note, not a reason to stop. Do not repeat it, and do not pull
without being told to.
"""

# The footer for a notice that is PURE `unmeasured` -- no stale report, no
# blocked repo, no failed pull, and no sync-phase `unchecked` /
# `cloned_unchecked` survives, only a nonzero `unmeasured`. `_FOOTER_TIME_
# BUDGET` above is the wrong footer for this case even though it LOOKS like
# the same shape ("ran out of time, nothing to see"): it names "the sync
# phase" by name, and on a machine where sync is off (the default) that
# phase never ran at all -- attributing the overrun to it would blame a
# phase that never started for what the REPORT phase's own `Deadline` did.
# This footer names no phase and claims only what `unmeasured` actually
# means: staleness unknown, nothing pulled or attempted either way.
_FOOTER_UNMEASURED = """
Say this to {owner} once, near the start: some watched repos were never
reached this session, so their staleness is unknown — nothing was pulled or
attempted for them either way, it simply was not checked. Then carry on with
what they actually asked for: this is a one-line note, not a reason to stop.
Do not repeat it, and do not pull without being told to.
"""

# The footer for a notice carrying BOTH a sync-phase time-budget count and a
# nonzero `unmeasured` -- reachable in practice (sync is enabled by default
# on most machines, and both phases share one hook-wide `Deadline`). Neither
# single-cause footer above is honest alone here: `_FOOTER_TIME_BUDGET`
# would say "nothing was changed either way" and stay silent on staleness
# being unknown; `_FOOTER_UNMEASURED` would say staleness is unknown and
# stay silent on the sync counts sitting above it. This one states both
# facts, so the notice never asserts only one of the two things that are
# both true here.
_FOOTER_UNMEASURED_AND_TIME_BUDGET = """
Say this to {owner} once, near the start: this session ran out of time on
two different things — the sync counts above mean nothing was pulled or
attempted there either way, and separately, some watched repos were never
reached by the report phase at all, so their staleness is unknown. Then
carry on with what they actually asked for: this is a one-line note, not a
reason to stop. Do not repeat it, and do not pull without being told to.
"""

# `render`'s `blocked` section: repos the sync phase TRIED and could not
# fast-forward. Distinct from the stale/behind section above, whose repos were
# never attempted at all. A skip is itself the signal — today that information
# does not exist anywhere else, so naming it here is the whole point.
_BLOCKED_HEADER = "=== NOT SYNCED ==="

# These three reasons mean the sync phase touched nothing. "no upstream"
# (and anything else `eligibility()` may return later) does not get the same
# reassurance, because nothing here can promise a repo with no tracking ref
# was left untouched in the sense that matters — it just never had a target.
#
# "could not read" belongs here too, and for a different reason than the
# other two: it is a claim about the REPO's state ("dirty", "detached") that
# we genuinely do not know when a git call fails or times out. But "nothing
# was changed in this repo" is not a claim about the repo — it is a claim
# about what THIS TOOL did, and that we know with certainty even when the
# repo's own state is unreadable: `eligibility()` returning "could not read"
# means every check in `sync_lib.eligibility()` is local and read-only, and
# a repo that lands there is never handed to `_pull()` at all. Withholding
# the reassurance would invite exactly the doubt it exists to settle.
_BLOCKED_NO_CHANGE_REASONS = frozenset(
    {"uncommitted changes", "detached HEAD", "could not read"})

# `render`'s `failed` section: repos the sync phase ACTUALLY RAN `git pull
# --ff-only` against and had it refused (a real divergence, a non-zero exit,
# or a timeout) -- distinct from `blocked` above, whose repos `eligibility()`
# rejected before any pull was ever attempted. The two are not the same fact
# and are never merged into one list: "was not pulled because it is dirty"
# sends a reader to `git status`; "was pulled at and refused" sends them to
# `git log`/`git diff` against origin instead.
_FAILED_HEADER = "=== SYNC ATTEMPTED, NOT APPLIED ==="

# Unlike `_BLOCKED_NO_CHANGE_REASONS`, EVERY reason in `failed` gets the
# "nothing was changed" reassurance, not just some of them. `--ff-only` is
# the guarantee that makes this safe to say unconditionally: it either
# applies cleanly or refuses outright -- there is no partial state for a
# diverged repo, a generic non-zero exit, or a `subprocess` timeout to leave
# behind, so the repo's own state is never in doubt the way `eligibility()`
# timing out can leave it.
_FAILED_LINE = ("{0} was pulled at and refused — {1}; nothing was changed "
                 "in this repo (a fast-forward either applies cleanly or "
                 "not at all).")

# `render`'s time-budget section: NOT a per-repo fact at all. `unchecked` and
# `cloned_unchecked` count repos/candidate names `sync_lib.run()`'s
# whole-phase budget never reached -- nothing was learned about them, so
# there is nothing to name and no reason to name one. Printed as its own
# section so "ran out of time" is never confused with "nothing to report",
# which is the entire reason `SyncResult` carries these two counters
# separately from `blocked` and `failed` in the first place.
_TIME_BUDGET_HEADER = "=== SYNC INCOMPLETE ==="
_UNCHECKED_LINE = ("Ran out of time before checking {0} already-cloned "
                    "repo(s) for updates — their status this session is "
                    "unknown, not necessarily clean.")
_CLONED_UNCHECKED_LINE = ("Ran out of time before cloning {0} newly "
                           "discovered repo(s) — they were not added to "
                           "the workspace this session.")

# `render`'s REPORT-phase time-budget section -- distinct from the section
# above, which is about the SYNC phase. `unmeasured` is `measure_repos`'
# own counter: repos the report loop's `Deadline` stopped from STARTING, a
# fact that has nothing to do with whether anything was pulled. Sync being
# off (the default -- see `sync_enabled()`) is exactly the case that makes
# conflating the two dangerous: with no sync phase at all, describing an
# `unmeasured` notice in terms of "the sync phase ran out of time" would
# name a phase that never ran as the one that did. So the wording here says
# only what `measure_repos` actually knows -- these repos were never
# reached, staleness unknown -- and never "pulled", "attempted", or
# "skipped", none of which `unmeasured` means.
_UNMEASURED_HEADER = "=== STALENESS UNKNOWN ==="
_UNMEASURED_LINE = ("{0} watched repo(s) were never reached this session — "
                     "their staleness is unknown, not necessarily clean.")


def repo_root(start: Optional[str] = None) -> Optional[str]:
    """The git repo containing this file, or None.

    `realpath`, never `abspath`: this module is reached through a `~/.local/bin`
    symlink, and `abspath` would resolve to `~/.local/bin` and find no repo.
    """
    path = os.path.dirname(os.path.realpath(start or __file__))
    while True:
        if os.path.isdir(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


_REPO_SELECTING_GIT_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE",
)


def clean_git_env() -> dict:
    """A copy of `os.environ` with every repo-SELECTING git var stripped.

    `git -C <dir>` is meant to name the repo, but an ambient `GIT_DIR` (or
    any of its siblings) overrides `-C` outright -- so a caller that merely
    happens to be running inside, or downstream of, a process that set one
    of these silently answers about a DIFFERENT repo than the one just
    named. `-C`'s target must win; that is the entire point of passing it.
    Shared by `_git` and by `budget_lib.checkout_behind`'s own `git
    rev-parse --show-toplevel` call, so the two can never drift on which
    vars are stripped.
    """
    return {k: v for k, v in os.environ.items()
            if k not in _REPO_SELECTING_GIT_VARS}


def _git(repo: str, *args: str, timeout: Optional[float] = None) -> Optional[str]:
    """Run a read-only git command, or return None on any failure at all.

    `timeout` overrides `GIT_TIMEOUT_SECONDS` for this one call when given;
    `None` (the default) falls back to the module constant, read live at call
    time so a test that monkeypatches `GIT_TIMEOUT_SECONDS` still sees it.
    This exists for `default_branch`, whose own `timeout` parameter threads
    through here -- see its docstring for why.

    **An empty `repo` returns None rather than running.** `git -C ""` is a
    silent no-op, not an error — git runs in the *current working directory*
    and exits 0. Measured: from inside this repo, `git -C "" rev-parse
    --abbrev-ref HEAD` prints `main`. So without this check every function
    here answers confidently about whatever directory the hook happened to be
    launched from, while their docstrings promise None means "cannot tell".
    A wrong answer that looks like a reading is the one failure this module is
    built to avoid.

    Not reachable through today's callers — `repo_root()` returns a path or
    None, never "", and `watched_repos()` filters on `isdir(<path>/.git)`. That
    is the point: the guarantee lived in the callers, so any new caller, or a
    refactor of an existing one, silently inherited the hazard. It belongs in
    the function whose contract it is.

    Found by a peer session hitting the identical `git -C ""` no-op in
    `lib/worktree_detect.sh`, where the consequence was worse — it authorised
    an install based on the wrong directory.
    """
    if not repo:
        return None
    try:
        # env=clean_git_env(): an ambient GIT_DIR/etc overrides `-C` outright,
        # so it must be stripped -- the `-C` target above is what must win.
        result = subprocess.run(
            ("git", "-C", repo) + args,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout if timeout is not None else GIT_TIMEOUT_SECONDS,
            env=clean_git_env())
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def default_branch(repo: str, timeout: Optional[float] = None) -> str:
    """The remote's default branch, falling back to main.

    Same resolution order as the `git tidy` alias in scripts/setup-git.sh, so
    the two never disagree about which branch "up to date" is measured against.

    `timeout` lets a caller override this module's `GIT_TIMEOUT_SECONDS` for
    this one call -- added for `tools/merge-brief/merge_brief_lib.py`, which
    puts this tool on `sys.path` and re-exports this function directly rather
    than reimplementing branch resolution, and which defines its own, looser
    `GIT_TIMEOUT_SECONDS = 5`. Without this parameter, tightening THIS
    module's constant for the report loop's budget would have silently
    narrowed merge-brief's timeout too. `None` (the default) falls back to
    `GIT_TIMEOUT_SECONDS`, exactly as before this parameter existed.
    """
    ref = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD",
               timeout=timeout)
    if ref and ref.startswith("origin/"):
        return ref[len("origin/"):]
    return "main"


def commits_behind(repo: str, branch: str) -> Optional[int]:
    """How many commits `origin/branch` has that HEAD does not. No network.

    None means "cannot tell" — no such ref, not a repo, git unavailable — and
    every caller must treat that as "say nothing" rather than "up to date".
    Counting from HEAD rather than from the default branch is deliberate: the
    symlinks point at the WORKING TREE, so what matters is whether the files
    currently checked out are missing commits, whatever branch they are on.
    """
    out = _git(repo, "rev-list", "--count", "HEAD..origin/{0}".format(branch))
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def commits_ahead(repo: str, branch: str) -> Optional[int]:
    """How many commits HEAD has that `origin/branch` does not. No network.

    The mirror of `commits_behind`, and it exists for one reason: **behind
    alone cannot tell a fast-forward from a divergence.** `HEAD..origin/main`
    counts only what upstream has, so a checkout that is 2 behind and 3 ahead
    reports exactly the same "2 behind" as one that is a clean fast-forward.
    The notice then offered a `git pull` and called it a fast-forward, which for
    the diverged case is a merge — or a hard failure on a dirty tree.

    `git tidy` in this same repo already refuses to guess between a merge and a
    rebase when the default branch has diverged. This is that distinction,
    arriving at the notice that tells you to pull in the first place.

    None means "cannot tell", exactly as in `commits_behind`, and callers must
    treat it as unknown rather than as zero.
    """
    out = _git(repo, "rev-list", "--count", "origin/{0}..HEAD".format(branch))
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def current_branch(repo: str) -> Optional[str]:
    """The checked-out branch, or None. `"HEAD"` means detached."""
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def should_report(current: Optional[str], default: str) -> bool:
    """PURE. Whether being behind is worth saying, given the checked-out branch.

    Only when sitting ON the default branch. That is the consumer case — not
    working on the repo, just hasn't pulled — and it is the only case where
    "you are behind origin/main" is both true and actionable.

    On a feature branch, being behind `origin/main` is not staleness at all: it
    is what having a branch open MEANS. Warning there fires most working days in
    any repo people actually develop in, and asks for something nobody wants
    anyway (pulling main into a half-finished branch). That noise is what makes
    a notice get skimmed, which would cost the toolkit and watched-repo
    warnings their whole value.

    Detached HEAD (`"HEAD"`) and an unreadable branch both mean silence: neither
    is a state where an automatic "you should pull" is safe advice.

    The accepted cost: someone who leaves a branch checked out and forgets stops
    being told. They are told again the moment they return to the default branch,
    which is also the moment it starts mattering again.

    A detached toolkit HEAD that is a rollback pin is reported separately, by
    `pin_notice`.
    """
    if not current or current == "HEAD":
        return False
    return current == default


_PIN_HEADER = "=== TOOLKIT PINNED ==="


def pin_notice(repo: str, record_file: str, deadline: "Deadline") -> Optional[str]:
    """The notice for a toolkit checkout pinned by `install.sh --rollback`, or None.

    The install record is read FIRST, with no git call, so the common case (no
    pinned rollback entry) costs nothing. `deadline` is checked before each git
    call, so this can overrun the shared deadline by at most one
    `GIT_TIMEOUT_SECONDS` call. Anything unexpected is None: a notice that
    cannot be proven true is not printed.
    """
    try:
        import install_record  # on sys.path via this module's import-time insert
        entry = install_record.rollback(install_record.load(record_file))
        if entry is None or not isinstance(entry.get("pinned_at"), str):
            return None
        if deadline.expired():
            return None
        if current_branch(repo) != "HEAD":
            return None
        if deadline.expired():
            return None
        if _git(repo, "rev-parse", "HEAD") != entry["commit"]:
            return None
        if deadline.expired():
            return None
        default = default_branch(repo)
        if deadline.expired():
            return None
        behind = commits_behind(repo, default)
        count = ("{0} commit(s)".format(behind) if behind is not None
                 else "an unknown number of commits")
        return (_PIN_HEADER + "\n\n"
                "claude-code-toolkit is pinned at {0} by ./install.sh --rollback "
                "since {1}, {2} behind origin/{3}. When the fix has landed: "
                "{4}/install.sh --update".format(
                    entry["commit"][:12], entry["pinned_at"], count, default, repo))
    except Exception:
        return None


def seconds_since_fetch(repo: str, now: Optional[float] = None) -> Optional[float]:
    """Age of the last fetch, or None if this repo has never fetched."""
    try:
        mtime = os.path.getmtime(os.path.join(repo, ".git", "FETCH_HEAD"))
    except OSError:
        return None
    return (time.time() if now is None else now) - mtime


def should_fetch(age_seconds: Optional[float], interval: Optional[int] = None) -> bool:
    """PURE. Whether a background refresh is due.

    `None` age means never fetched, which is exactly when a refresh matters most
    — a fresh clone has no `origin/main` to count against, so until it fetches
    once, `commits_behind` can only answer "unknown".
    """
    if interval is None:
        interval = DEFAULT_FETCH_INTERVAL_SECONDS
    if age_seconds is None:
        return True
    return age_seconds >= interval


def fetch_interval() -> int:
    """Configured throttle, or the default. Never raises inside a hook."""
    if _config is None:
        return DEFAULT_FETCH_INTERVAL_SECONDS
    try:
        value = _config.get_int("repo_freshness", "fetch_interval_seconds",
                                default=DEFAULT_FETCH_INTERVAL_SECONDS)
    except Exception:
        return DEFAULT_FETCH_INTERVAL_SECONDS
    return value if value > 0 else DEFAULT_FETCH_INTERVAL_SECONDS


def is_enabled() -> bool:
    """False only when explicitly disabled. Absent config means on."""
    if _config is None:
        return True
    try:
        return _config.get("repo_freshness", "enabled", default=True) is not False
    except Exception:
        return True


def spawn_background_fetch(repo: str) -> bool:
    """Start a detached `git fetch` and return immediately.

    `start_new_session` detaches it from this hook's process group so it
    outlives the hook and can never hold the session open. Output goes nowhere:
    a fetch that fails is not worth a word at session start, and the next run
    simply reports from whatever refs did land.

    **Refuses an empty `repo`, for the same reason `_git` does and with a worse
    consequence.** This builds its own argv rather than going through `_git`, so
    it did not inherit that guard — and `git -C ""` is a no-op, so an empty path
    here does not fail: it fetches in whatever directory the hook was launched
    from. That is network I/O updating an unrelated repo's remote-tracking refs,
    where `_git`'s version of the same bug only produced a wrong reading.

    The harm is the stray fetch itself, not a bad return value: today's only
    caller, `spawn_due_fetches` in this same module, USES what this returns
    (to count how many fetches were actually spawned), and the throttle keys
    off `.git/FETCH_HEAD` mtime rather than anything recorded here. Returning
    `True` for a fetch that went somewhere else would still be a lie to that
    caller, which is why the guard returns `False` rather than simply
    skipping.

    Found by review, after `_git` was hardened in the same commit — the fix
    landed on the shared helper and missed the one caller that does not use it.
    """
    if not repo:
        return False
    try:
        subprocess.Popen(
            ("git", "-C", repo, "fetch", "--quiet", "origin"),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, start_new_session=True)
        return True
    except Exception:
        return False


def watch_list() -> tuple:
    """Configured extra repos to watch, or the default. Never raises."""
    if _config is None:
        return DEFAULT_WATCH
    try:
        configured = _config.get("repo_freshness", "watch", default=None)
    except Exception:
        return DEFAULT_WATCH
    if configured is None:
        return DEFAULT_WATCH
    if not isinstance(configured, (list, tuple)):
        return DEFAULT_WATCH
    # An explicit empty list is a real choice — "watch nothing but the toolkit"
    # — and must not silently fall back to the default.
    return tuple(str(entry) for entry in configured if str(entry).strip())


def resolve_watch_entry(entry: str, workspace_root: Optional[str]) -> Optional[str]:
    """PURE. Turn a watch-list entry into an absolute path, or None.

    An entry may be a full path (`~/somewhere/notes`) or a bare repo name
    (`notes`), which resolves under `<workspace_root>/repos/`. The bare form
    is what makes a portable config possible: paths differ between machines,
    repo names do not, so the same config line works on any machine with the
    same layout under a different workspace root.

    Returns None when a bare name is given and no workspace root is configured,
    because guessing a root is how a tool ends up reporting on a directory the
    user never meant it to touch.
    """
    entry = (entry or "").strip()
    if not entry:
        return None
    if entry.startswith("~") or os.path.isabs(entry):
        return os.path.expanduser(os.path.expandvars(entry))
    if not workspace_root:
        return None
    return os.path.join(os.path.expanduser(workspace_root), "repos", entry)


def watched_repos(toolkit: Optional[str] = None) -> list:
    """Every repo to check, as (path, kind) pairs, toolkit first.

    The toolkit is self-located rather than configured — it is the repo this
    file lives in, so asking the user to name it could only ever introduce a
    way to name it wrongly.
    """
    root = workspace_root()
    pairs = []
    toolkit_path = toolkit if toolkit is not None else repo_root()
    if toolkit_path:
        pairs.append((toolkit_path, KIND_TOOLKIT))
    for entry in watch_list():
        resolved = resolve_watch_entry(entry, root)
        # Skip anything that is not actually a git checkout: an unconfigured or
        # never-cloned repo must cost nothing, not produce a warning about
        # itself.
        if resolved and os.path.isdir(os.path.join(resolved, ".git")):
            if not any(os.path.realpath(resolved) == os.path.realpath(p)
                       for p, _ in pairs):
                name = os.path.basename(resolved.rstrip("/"))
                pairs.append((resolved, _KIND_BY_NAME.get(name, KIND_GENERIC)))
    return pairs


def _now() -> float:
    """The module's only clock read for deadline bookkeeping.

    Mirrors `sync_lib._now` exactly, and for the same reason: `Deadline`
    reads the clock through this function rather than `time.monotonic()`
    directly, so a test can drive expiry by monkeypatching
    `freshness_lib._now` instead of sleeping for real seconds. A real hook
    invocation never overrides it, so this is exactly `time.monotonic()` in
    production. Kept separate from `sync_lib._now` -- one module's clock
    read must never depend on the other's still working.
    """
    return time.monotonic()


class Deadline:
    """Wraps an ABSOLUTE `time.monotonic()` value -- never a duration.

    The caller computes the deadline itself, typically `_now() + budget`, and
    hands it in already absolute -- the same idiom `sync_lib.run()` uses for
    its own `deadline` local. This class does not compute a deadline from a
    duration, on purpose: a whole-hook budget shared between the sync and
    report phases needs both to be measured against the SAME wall-clock
    endpoint, and a constructor that started its own countdown from "now"
    would let each phase silently measure a different one.

    `expired()` is the only way this reads the clock, and it goes through
    `_now()` so a test drives expiry by monkeypatching that function, never
    by sleeping.
    """

    def __init__(self, at: float):
        self._at = at

    def expired(self) -> bool:
        return _now() >= self._at


def spawn_due_fetches(repos, interval: Optional[int] = None) -> int:
    """Spawn a detached `git fetch` for every repo in `repos` whose fetch is
    overdue. Returns how many were actually spawned.

    `repos` is the identical `(path, kind)` sequence `watched_repos()`
    returns -- the SAME object `measure_repos` is handed, not a separately
    fetched or re-derived list, so the two never disagree about which repos
    exist this session. Only `path` is passed on to `seconds_since_fetch` and
    `spawn_background_fetch`; `kind` is unpacked and discarded here, exactly
    as `measure_repos` discards it for those same two calls.

    Takes NO deadline, deliberately. `spawn_background_fetch` starts a
    detached, `start_new_session=True` `Popen` and returns immediately -- the
    fetch itself runs after this process is done deciding anything, so there
    is no wall-clock cost here to budget against. Adding one would only
    complicate the one thing this function does (decide whether to spawn)
    for a cost that does not exist.

    `interval` resolves through `fetch_interval()` (configured value, or the
    default) when not given explicitly -- the same resolution the report
    loop has always used.
    """
    if interval is None:
        interval = fetch_interval()
    spawned = 0
    for repo, _kind in repos:
        if should_fetch(seconds_since_fetch(repo), interval):
            if spawn_background_fetch(repo):
                spawned += 1
    return spawned


def measure_repos(repos, deadline: Deadline) -> Tuple[list, int]:
    """Measure every repo in `repos`, stopping early if `deadline` expires.
    Returns `(reports, unmeasured)`.

    Body lifted verbatim from the report loop in `claude-repo-freshness`,
    MINUS the fetch spawn -- that half now lives in `spawn_due_fetches`,
    called separately by the hook and never budgeted (see its docstring for
    why). `repos` is the same `(path, kind)` sequence `watched_repos()`
    returns.

    `deadline.expired()` is checked BEFORE starting each repo, never during
    one -- the same idiom `sync_lib.run()` uses for its own pull loop. Once
    expired, every repo not yet STARTED is counted in `unmeasured` and the
    loop stops immediately.

    `unmeasured` counts only repos the deadline stopped from STARTING. A
    repo that started and was then filtered out of `reports` by
    `should_report` (on a feature branch, or detached HEAD) WAS measured --
    it simply produced no report entry. The invariant this supports is
    against repos CONSIDERED, not repos reported: every repo this loop
    actually reached, whether or not it earned a `reports` entry, plus
    `unmeasured`, equals `len(repos)`.

    `reports` entries keep today's 5-tuple `(path, kind, behind, branch,
    ahead)` -- the shape `render()` already accepts.
    """
    reports = []
    for index, (repo, kind) in enumerate(repos):
        if deadline.expired():
            return reports, len(repos) - index
        branch = default_branch(repo)
        if not should_report(current_branch(repo), branch):
            continue
        reports.append((repo, kind, commits_behind(repo, branch),
                        branch, commits_ahead(repo, branch)))
    return reports, 0


def workspace_root() -> Optional[str]:
    """The configured workspace root, or None. Never raises inside a hook."""
    if _config is None:
        return None
    try:
        return _config.workspace_root()
    except Exception:
        return None


def render(reports, owner: str = "the human", *, blocked=None, failed=None,
           unchecked: int = 0, cloned_unchecked: int = 0,
           unmeasured: int = 0) -> Optional[str]:
    """PURE. The session-start notice for every stale, blocked, failed, or
    incompletely-checked repo, or None.

    `reports` is an iterable of (path, kind, behind, branch) or
    (path, kind, behind, branch, ahead). A `behind` of None or 0 contributes
    nothing: unknown state and up to date are different facts, but neither is
    worth a line at the top of every session.

    The optional fifth element is `ahead` — commits HEAD has that origin does
    not. Truthy means the checkout has **diverged**, and the footer stops
    promising a fast-forward. It is optional rather than required so that every
    existing caller and test keeps working; absent or None means "not measured",
    which renders exactly as before. Note that "not measured" and "measured as
    zero" deliberately read the same here: the footer's fast-forward claim was
    the status quo, and an unmeasured repo is no worse off than it was.

    **Calling this with only `(reports, owner)` renders byte-for-byte
    identical to before `blocked`, `failed`, `unchecked` and
    `cloned_unchecked` existed** — every new parameter defaults to "no data",
    and every new branch below is reachable only by supplying one.

    `blocked` and `failed` are both keyword-only iterables of (path, reason)
    pairs, and they carry DIFFERENT facts that are never merged into one
    list: `blocked` is a repo the sync phase never even attempted (rejected
    by `eligibility()` before any `git pull` ran — dirty, detached, no
    upstream, unreadable); `failed` is a repo it DID run `git pull --ff-only`
    against and had refused (diverged, a generic non-zero exit, or a
    timeout). "was not pulled because it is dirty" and "was pulled at and
    refused" send a reader to different next actions, so they get separate
    sections with separate headers. Both are nonetheless true cases of
    "nothing in your repo changed" — `--ff-only` either applies cleanly or
    does nothing at all — and each section says so in terms accurate to
    itself: `blocked` only for the reasons in `_BLOCKED_NO_CHANGE_REASONS`
    (a claim about what THIS TOOL did, not about the repo's own state), and
    `failed` unconditionally, since `--ff-only` cannot leave a partial
    change behind regardless of why it refused.

    For both, `None` and `[]` are different inputs and must stay different:
    `None` means the sync phase did not run at all, and `[]` means it ran
    and found nothing to report there — both render exactly as if the
    argument were absent. Only a non-empty iterable changes the output. A
    repo appearing in both `reports` and `blocked`/`failed` is dropped from
    the ordinary behind-count loop and rendered once, under its section,
    with no `git pull` command — that command is the one the sync phase
    already ran (and had refused) or chose not to run, and printing it
    anyway is the exact failure the diverged footer above was written to
    prevent for a different reason.

    `unchecked` and `cloned_unchecked` are plain counts, not per-repo facts —
    `sync_lib.run()`'s whole-phase time budget ran out before looking at that
    many already-cloned repos / newly-discovered names, so there is nothing to
    name, only a number. A human must be able to tell "nothing to do" from
    "ran out of time", so a nonzero count here ALWAYS earns its own line,
    even when `reports`, `blocked` and `failed` are all empty — the
    difference between "clean" and "unknown" is the entire reason these
    counters exist, and staying silent would erase it.

    `unmeasured` is `measure_repos`' own count, and it is a DIFFERENT fact
    from `unchecked`/`cloned_unchecked` even though all three are "ran out
    of time" counters: those two belong to the SYNC phase's `sync_lib.run()`,
    this one to the REPORT phase's own `Deadline`, and the two phases can each
    run out of time independently of the other. It says only that these
    repos' staleness is unknown THIS SESSION — never that anything was
    pulled, attempted, or skipped for them, none of which `measure_repos`
    ever claims about a repo it never started.

    The footer is chosen by what actually printed above, not by which
    arguments are non-empty, and it must never claim or imply completeness
    when `failed`, `unchecked`, `cloned_unchecked`, or `unmeasured` is
    non-empty:
    - `stale` non-empty: the ordinary or diverged footer, offering the
      command(s) genuinely printed above.
    - `stale` empty but `blocked` or `failed` non-empty: `_FOOTER_BLOCKED`,
      which describes only the repo(s) actually named above and offers no
      command — accurate for both "never attempted" and "attempted and
      refused" without conflating them.
    - Everything above empty, only `unchecked`/`cloned_unchecked` nonzero:
      `_FOOTER_TIME_BUDGET`, which claims nothing about any repo's state,
      named or not — the honest claim in that case is narrower than either
      of the other two footers. It names the SYNC phase specifically, which
      is only safe because this branch requires a nonzero sync-phase count.
    - Everything above empty, only `unmeasured` nonzero: `_FOOTER_UNMEASURED`,
      which names no phase and claims only that staleness is unknown —
      `_FOOTER_TIME_BUDGET` would misname the REPORT phase's overrun as the
      sync phase's, which is a live case since sync defaults to off.
    - Everything above empty, both a sync-phase count and `unmeasured`
      nonzero: `_FOOTER_UNMEASURED_AND_TIME_BUDGET`, which states both facts
      rather than picking one of the two single-cause footers and going
      silent on the other.
    """
    blocked_by_path = {}
    for entry in (blocked or []):
        if not entry:
            continue
        path = entry[0] if len(entry) > 0 else None
        if not path:
            continue
        reason = entry[1] if len(entry) > 1 else None
        blocked_by_path.setdefault(path.rstrip("/"), reason)

    failed_by_path = {}
    for entry in (failed or []):
        if not entry:
            continue
        path = entry[0] if len(entry) > 0 else None
        if not path:
            continue
        reason = entry[1] if len(entry) > 1 else None
        failed_by_path.setdefault(path.rstrip("/"), reason)

    excluded = set(blocked_by_path) | set(failed_by_path)
    stale = [r for r in (reports or []) if r[2] and r[2] > 0
             and r[0].rstrip("/") not in excluded]

    if (not stale and not blocked_by_path and not failed_by_path
            and not unchecked and not cloned_unchecked and not unmeasured):
        return None

    any_diverged = False
    lines = []
    if stale:
        lines.append(_HEADER.format(plural="S" if len(stale) > 1 else ""))
        for report in stale:
            path, kind, behind, branch = report[0], report[1], report[2], report[3]
            ahead = report[4] if len(report) > 4 else None
            # GENERIC, not a named kind: the fallback fires only for a kind
            # `_KIND_BY_NAME` never produces, and the honest thing to say about
            # an unrecognised repo is the generic reason, not a claim about
            # what the repo holds.
            why = _KIND_WHY.get(kind, _KIND_WHY[KIND_GENERIC])
            lines.append("")
            if ahead:
                any_diverged = True
                lines.append(
                    "{0} has DIVERGED from origin/{1}: {2} commit(s) behind and {3} "
                    "of its own — {4}."
                    .format(os.path.basename(path.rstrip("/")), branch, behind,
                            ahead, why))
                # Deliberately NOT the pull command. Printing a runnable `git pull`
                # under a footer that says do not offer a pull is the change's own
                # failure mode: the human reads the command, not the paragraph, and
                # a plain pull here is a merge. Offer the read-only inspection
                # instead — what the local commits ARE is the thing they need in
                # order to choose between merging, rebasing, and leaving it.
                lines.append("  inspect first, this is NOT a pull:  "
                             "git -C {0} log --oneline origin/{1}..HEAD"
                             .format(path, branch))
            else:
                lines.append("{0} is {1} commit(s) behind origin/{2} — {3}."
                             .format(os.path.basename(path.rstrip("/")), behind,
                                     branch, why))
                lines.append("  " + _KIND_COMMAND.get(kind, _DEFAULT_COMMAND)
                             .format(repo=path))

    if blocked_by_path:
        if lines:
            lines.append("")
        lines.append(_BLOCKED_HEADER)
        for path, reason in blocked_by_path.items():
            name = os.path.basename(path)
            lines.append("")
            if reason in _BLOCKED_NO_CHANGE_REASONS:
                lines.append(
                    "{0} was NOT pulled — {1}; nothing was changed in this repo."
                    .format(name, reason))
            else:
                lines.append("{0} was NOT pulled — {1}.".format(name, reason))

    if failed_by_path:
        if lines:
            lines.append("")
        lines.append(_FAILED_HEADER)
        for path, reason in failed_by_path.items():
            name = os.path.basename(path)
            # A real `git pull` failure message can be long and multi-line
            # (stderr, verbatim) — only its first line is worth a notice
            # meant to be read in one glance.
            reason = (reason or "git pull failed").strip().splitlines()[0]
            lines.append("")
            lines.append(_FAILED_LINE.format(name, reason))

    if unchecked or cloned_unchecked:
        if lines:
            lines.append("")
        lines.append(_TIME_BUDGET_HEADER)
        if unchecked:
            lines.append("")
            lines.append(_UNCHECKED_LINE.format(unchecked))
        if cloned_unchecked:
            lines.append("")
            lines.append(_CLONED_UNCHECKED_LINE.format(cloned_unchecked))

    if unmeasured:
        if lines:
            lines.append("")
        lines.append(_UNMEASURED_HEADER)
        lines.append("")
        lines.append(_UNMEASURED_LINE.format(unmeasured))

    # Which footer applies is decided by what actually printed above, not by
    # which arguments are non-empty. `stale` is the only section that ever
    # prints a command, so its presence is what "there are commands above"
    # means. `blocked`/`failed` win the next tier because they describe
    # something concrete, even without a command. Only when NOTHING concrete
    # survives — pure time-budget, on one or both phases — does one of the
    # three narrowest footers apply, chosen by which phase(s) actually ran
    # out of time: naming a phase that did not (`_FOOTER_TIME_BUDGET` names
    # sync specifically) would misattribute the overrun.
    if stale:
        footer = _FOOTER_DIVERGED if any_diverged else _FOOTER
    elif blocked_by_path or failed_by_path:
        footer = _FOOTER_BLOCKED
    elif unmeasured and (unchecked or cloned_unchecked):
        footer = _FOOTER_UNMEASURED_AND_TIME_BUDGET
    elif unmeasured:
        footer = _FOOTER_UNMEASURED
    else:
        footer = _FOOTER_TIME_BUDGET
    lines.append(footer.format(owner=owner))
    return "\n".join(lines)
