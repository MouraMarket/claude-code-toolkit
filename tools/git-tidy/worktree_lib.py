#!/usr/bin/env python3
"""Classify worktrees by what they would COST to delete, not by whether the
work in them is finished.

The question people actually ask is "is this worktree done?", and it is the
wrong question — it cannot be answered from the machine. A paused session and
an abandoned one look identical: same dead pid, same released claim, same
untouched files. Two audits of this workspace got it wrong in both directions
by trying.

The answerable question is "does this directory hold anything that exists
nowhere else?" A worktree that holds nothing is a cache. Deleting it loses no
data whether or not anyone comes back, because `git worktree add` recreates it
from a branch that still exists. A worktree that holds something must be left
alone whether or not anyone comes back.

Two tests were tried against real worktrees and both failed, so do not reach for them again:

  - `git merge-tree --write-tree <default> HEAD` and comparing to the default
    branch's tree. Reported a conflict for many worktrees in one repo. A
    squash-merged branch conflicts with the default branch as soon as the
    default branch touches the same files again, which is normal, so "conflict"
    carried no information about whether the branch held anything.
  - Diffing the default branch against HEAD restricted to the files the branch
    touched. Same flaw for the same reason: it cannot separate "this branch has
    unique content" from "the default branch moved on afterwards".

What works is the pull request. A squash merge records the exact head commit it
merged, so comparing that commit to the branch's current tip is exact: equal
means the branch is byte-identical to what shipped, and anything else means
commits landed after the merge. That last case is not hypothetical — it catches
worktrees whose PRs had merged and which were still holding unmerged work.

The cost of that exactness is a network call, so every path here degrades to
UNKNOWN rather than guessing when the PR data is missing. Reporting nothing is
the documented trade throughout this tool: a wrong delete is unrecoverable and
a missing line costs one run.

Automatic pause detection: investigated, and both cheap routes are closed
--------------------------------------------------------------------------
This report cannot see a *pause*. A paused session and a finished one leave the
same dead pid and the same released claim, which is why the disposition written
by `/handover` exists at all. That covers only threads that run `/handover`,
and most never do, so an automatic signal was scoped. Two obvious sources were
checked and neither works:

  - A breadcrumb written into `claims.json`. `claim_lib.prune()` keeps only
    records whose process is alive, so a breadcrumb is dead by definition and
    is removed on the next claim or release. Relaxing `prune` to spare it would
    trade the property that makes lockout impossible for a reporting
    convenience -- not a trade worth making for a hint.
  - Deriving "last active here" from session transcripts. They do carry `cwd`
    and `gitBranch` on most entries, so this looks free. It is not: a session's
    cwd is frequently the WORKSPACE ROOT while it edits a worktree. One session
    logged 524 `cwd` records, every one of them the workspace root, while
    working inside a worktree the whole time -- keyed on cwd it would have
    missed its own work entirely.

What that leaves, if anyone builds it: `claude-session-release` is the moment
the checkout-to-session link is destroyed, and `prune()` is the same moment for
a crash. Those are the only two places the information still exists. Note also
that what such a breadcrumb buys is RECENCY -- "last active 2 hours ago" versus
"three weeks ago" -- which ranks worktrees rather than proving anything about
any one of them.
"""

# Design decisions about this file that look like accidental complexity are
# recorded in docs/decisions.md. Read the relevant one before simplifying
# anything here.

import json
import os
import re
import subprocess

HOLDS = "holds"
CACHE = "cache"
UNKNOWN = "unknown"
LIVE = "live"

# A dead claim is not evidence of abandonment -- see the module docstring -- but
# it IS evidence that a session once owned this checkout and never released it
# cleanly. `claude-session-release` clears the claim at SessionEnd, so a claim
# that outlived its process means the session was interrupted rather than
# closed, which is what a paused session looks like. Worth a warning on an
# otherwise-removable worktree, because deleting it does not lose data but does
# break that session's cwd if it ever resumes.
STALE_CLAIM_NOTE = "a session claimed this and never released it; removing it "\
                   "loses no work but breaks that session's cwd if it resumes"


def _git(repo, *args):
    """stdout of a git command, or '' on any failure. Never raises."""
    try:
        out = subprocess.run(("git", "-C", repo) + args,
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    except Exception:
        return ""
    return out.stdout.strip()


def _git_rc(repo, *args):
    """(returncode, stdout) of a git command, or (None, '') if it could not run.

    `_git` collapses every failure into an empty string, which cannot tell
    "not an ancestor" (exit 1) from "unknown object" (128) -- and a caller
    asking for proof must treat only exit 0 as a yes.
    """
    try:
        out = subprocess.run(("git", "-C", repo) + args,
                             capture_output=True, text=True, encoding="utf-8", timeout=30)
    except Exception:
        return None, ""
    return out.returncode, out.stdout.strip()


def gone_branches(repo):
    """Local branches whose upstream is gone (deleted on the remote)."""
    names = []
    for line in _git(repo, "for-each-ref",
                     "--format=%(refname:short) %(upstream:track)",
                     "refs/heads").splitlines():
        # Branch names contain no whitespace, so the first space ends the name.
        name, _, track = line.partition(" ")
        if name and track == "[gone]":
            names.append(name)
    return names


def gone_branch_evidence(repo, branch, merged_prs, default_branch):
    """(proven, reason): does anything show this [gone] branch's work merged?

    The old rule "upstream gone, therefore finished" is false: a remote branch
    is deleted when a PR is closed unmerged, or by hand, and a reviewer measured
    a real fixture losing an unpublished commit that way with exit 0. Deletion
    needs positive evidence -- the tip is already in origin/<default> (a merge
    or rebase merge), or is a merged PR's head or an ancestor of it (a squash
    merge, which leaves no ancestry behind). Only exit code 0 from git counts as
    proof; "no", "unknown object" and "could not run" all fall through to keep.
    """
    tip = _git(repo, "rev-parse", "--verify", "refs/heads/" + branch)
    if not tip:
        return False, "could not read its tip"
    rc = _git_rc(repo, "merge-base", "--is-ancestor", tip,
                 "origin/" + default_branch)[0]
    if rc == 0:
        return True, "already in origin/%s" % default_branch
    if merged_prs is None:
        return False, ("no pull-request data, so a squash merge cannot be told "
                       "from unpublished work")
    pull = merged_prs.get(branch)
    if pull and pull.get("oid"):
        oid = pull["oid"]
        number = pull.get("number")
        if tip == oid:
            return True, "identical to merged #%s" % number
        if _git_rc(repo, "merge-base", "--is-ancestor", tip, oid)[0] == 0:
            return True, "contained in merged #%s" % number
        extra = _git(repo, "rev-list", "--count", oid + ".." + tip) or "?"
        return False, "%s commit(s) not in merged #%s" % (extra, number)
    ahead = _git(repo, "rev-list", "--count",
                 "origin/%s..%s" % (default_branch, tip)) or "?"
    return False, "no merged PR, %s commit(s) not in origin/%s" % (ahead, default_branch)


def list_worktrees(repo):
    """Every worktree except the main one, as (path, branch) pairs.

    The main working tree is always the first record `git worktree list`
    emits, and it is excluded because it is not a candidate for removal -- it
    is the checkout everything else was added from. Bare and detached entries
    are excluded too: neither carries a branch to compare a merge against.
    """
    porcelain = _git(repo, "worktree", "list", "--porcelain")
    if not porcelain:
        return []
    records, current = [], {}
    for line in porcelain.splitlines():
        if not line:
            if current:
                records.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)

    out = []
    for record in records[1:]:          # [0] is the main working tree
        path = record.get("worktree")
        if not path or "bare" in record:
            continue
        branch = (record.get("branch") or "").replace("refs/heads/", "")
        if not branch:
            continue                    # detached HEAD -- nothing to compare
        out.append((path, branch))
    return out


DISPOSITIONS_PATH = os.path.expanduser("~/.claude/worktree-dispositions.json")


def load_dispositions(path=None):
    """{realpath: record} of what a thread said about its own worktree.

    Written by `/handover`, which runs at exactly the moment a thread stops and
    is therefore the only place that knows whether stopping means finished or
    paused. Nothing else on the machine can tell those apart -- that is the
    whole reason this file exists.

    `GIT_TIDY_DISPOSITIONS_PATH` redirects the lookup for the tests.
    """
    path = (path or os.environ.get("GIT_TIDY_DISPOSITIONS_PATH")
            or DISPOSITIONS_PATH)
    try:
        with open(path, encoding="utf-8") as handle:
            records = json.load(handle)
    except Exception:
        return {}
    if not isinstance(records, dict):
        return {}
    return {os.path.realpath(k): v for k, v in records.items()
            if isinstance(v, dict)}


def _age_days(stamp):
    """Whole days since an ISO stamp, or None when it cannot be read."""
    if not isinstance(stamp, str) or not stamp:
        return None
    from datetime import datetime
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    now = datetime.now(tz=when.tzinfo) if when.tzinfo else datetime.now()
    try:
        return max(0, int((now - when).total_seconds() // 86400))
    except Exception:
        return None


def prs_named_in(reason):
    """PR numbers a hold reason mentions -- 'holds PR #55, unmerged' -> {55}.

    Deliberately shallow. The reason is free prose a thread wrote about itself,
    so the only thing worth extracting is a `#NNN` token; anything cleverer
    would be guessing at sentence structure that nobody promised.
    """
    return {int(n) for n in re.findall(r"#(\d+)", reason or "")}


def apply_disposition(verdict, record, merged_numbers=None):
    """Overlay what the thread declared onto what the repository shows.

    A declaration may only ever make this report MORE conservative. "hold"
    promotes a cache worktree to held, because a paused session's checkout
    holds no data but removing it still breaks the cwd it resumes into, and
    nothing else can see that. "retire" never demotes: a thread saying it is
    finished does not outrank commits that are demonstrably not on the default
    branch, and when the two disagree that disagreement is the interesting
    part -- it means work landed after the thread thought it was done.

    A hold's REASON, though, is a snapshot of the world when the thread stopped,
    and nothing re-read it. This report once said a worktree
    "holds PR #N ... unmerged and awaiting review" days after #N merged.
    That is worse than a stale note, because a hold reason is the sentence a
    human uses to decide whether to keep the checkout -- so it decays into a
    confident false statement inside a tool people trust.

    `merged_numbers` is the set of merged PR numbers, so the reason can be
    checked rather than repeated. The hold SURVIVES either way: a merged PR
    does not prove the checkout is disposable (work can have landed after it,
    and the thread may be paused mid-follow-up), and demoting on that basis
    would be the one thing this function promises never to do. It adds a
    warning beside the reason instead.

    Absence is not evidence here. `merged_numbers` is None when PR data could
    not be fetched, and the fetch is capped, so a number simply not in the set
    means "not known to be merged", never "still open" -- and produces silence.
    """
    if not record:
        return verdict
    declared = record.get("disposition")
    age = _age_days(record.get("recorded_at"))
    when = "" if age is None else ", %d day(s) ago" % age

    if declared == "hold" and verdict["state"] != LIVE:
        reason = record.get("reason") or "no reason recorded"
        verdict["state"] = HOLDS
        verdict["note"] = "held deliberately: %s%s" % (reason, when)
        shipped = sorted(prs_named_in(reason) & (merged_numbers or set()))
        if shipped:
            verdict["warn"] = (
                "the reason above names %s, already merged - re-check why this "
                "is held before trusting it" % ", ".join("#%d" % n for n in shipped))
    elif declared == "retire":
        if verdict["state"] == CACHE:
            verdict["note"] += "; the thread that wrote it marked it retired"
        elif verdict["state"] == HOLDS:
            verdict["warn"] = ("marked retired%s, but the repository disagrees "
                               "- work landed after that call" % when)
    return verdict


# Ignored paths that are safe to lose with a worktree: regenerable Python
# caches only. The list is deliberately short and fails safe -- a cache type
# missing from it makes a worktree read as holding work (one extra report line),
# never the reverse. A build's `output/` directory is exactly the kind of
# path that must hold.
DISPOSABLE_IGNORED_DIRS = ("__pycache__", ".pytest_cache")
DISPOSABLE_IGNORED_SUFFIXES = (".pyc",)


def is_disposable_ignored(path):
    """True when `path` (the text after `!! ` in porcelain output) is a cache.

    A wholly-ignored directory is reported as one entry with a trailing `/`.
    A path git quoted (leading `"`, unusual characters) is never trusted.
    """
    if path.startswith('"'):
        return False
    if path.endswith("/"):
        return os.path.basename(path.rstrip("/")) in DISPOSABLE_IGNORED_DIRS
    return os.path.basename(path).endswith(DISPOSABLE_IGNORED_SUFFIXES)


def classify_one(path, branch, merged_prs, claims, default_branch):
    """One worktree's verdict.

    `merged_prs` maps branch name -> {"number": int, "oid": str} for merged
    pull requests, or is None when the data could not be fetched. None and an
    empty dict mean different things and must not be collapsed: None is "we do
    not know", an empty dict is "we asked and this repo has no merged PRs".
    """
    verdict = {"path": path, "branch": branch, "state": UNKNOWN,
               "note": "", "live": False, "warn": "", "merged": False}

    claim = claims.get(os.path.realpath(path))
    if claim:
        name, alive = claim
        if alive is True:
            verdict["live"] = True
            verdict["state"] = LIVE
            verdict["note"] = "in use by session %s" % name
            return verdict
        if alive is None:
            # Could not tell whether the session is running. Only a positive
            # "not running" may make a claim stale; unknown must not delete.
            verdict["state"] = UNKNOWN
            verdict["note"] = ("claimed by session %s; could not check "
                               "whether it is still running" % name)
            return verdict
        verdict["warn"] = STALE_CLAIM_NOTE

    # `--ignored` because `git worktree remove` (no --force) deletes ignored
    # files without complaint, so a clean `--porcelain` proves nothing about
    # them. `--untracked-files=normal` is explicit because a user's
    # `status.showUntrackedFiles=no` makes `--porcelain --ignored` print
    # nothing at all -- neither untracked nor ignored entries.
    lines = [l for l in _git(path, "status", "--porcelain", "--ignored",
                             "--untracked-files=normal").splitlines() if l]
    dirty = [l for l in lines if not l.startswith("!! ")]
    kept_ignored = [l[3:] for l in lines
                    if l.startswith("!! ") and not is_disposable_ignored(l[3:])]
    if dirty or kept_ignored:
        parts = []
        if dirty:
            parts.append("%d uncommitted change(s)" % len(dirty))
        if kept_ignored:
            parts.append("%d ignored path(s) git would delete with it: %s" % (
                len(kept_ignored), ", ".join(kept_ignored[:3])
                + (", ..." if len(kept_ignored) > 3 else "")))
        verdict["state"] = HOLDS
        verdict["note"] = "; ".join(parts)
        return verdict

    if merged_prs is None:
        verdict["note"] = "no pull-request data; cannot tell shipped from unshipped"
        return verdict

    tip = _git(path, "rev-parse", "HEAD")
    pull = merged_prs.get(branch)
    if pull:
        if pull.get("oid") == tip and tip:
            verdict["state"] = CACHE
            verdict["merged"] = True
            verdict["note"] = "identical to merged #%s" % pull.get("number")
        else:
            extra = _git(path, "rev-list", "--count",
                         "%s..HEAD" % pull.get("oid")) or "?"
            verdict["state"] = HOLDS
            verdict["note"] = "%s commit(s) added after #%s merged" % (
                extra, pull.get("number"))
        return verdict

    # No merged PR. A branch sitting exactly on the default branch has nothing
    # of its own regardless -- that covers worktrees created and never used.
    ahead = _git(path, "rev-list", "--count",
                 "origin/%s..HEAD" % default_branch)
    if ahead == "0":
        verdict["state"] = CACHE
        verdict["note"] = "no merged PR, nothing ahead of %s" % default_branch
    elif ahead:
        verdict["state"] = HOLDS
        verdict["note"] = "no merged PR, %s commit(s) ahead of %s" % (
            ahead, default_branch)
    else:
        verdict["note"] = "could not compare against origin/%s" % default_branch
    return verdict


def classify(repo, merged_prs, claims, default_branch="main", dispositions=None):
    """Every worktree's verdict, in the order git lists them.

    The declared disposition is applied last, on top of the finished verdict,
    because `classify_one` returns early on several paths and an overlay that
    only some of them reach is an overlay that silently does not apply.
    """
    if dispositions is None:
        dispositions = load_dispositions()
    # `merged_prs` is None when the data could not be fetched, and indexing it
    # would raise, so the guard is required either way. None is then carried
    # through rather than flattened to an empty set — NOT because the two
    # behave differently today (they do not: the warning fires only on a
    # positive match, so both produce silence) but so the value's type keeps
    # saying "unknown" for any future reader who wants to distinguish them.
    merged_numbers = None
    if merged_prs is not None:
        merged_numbers = {pull.get("number") for pull in merged_prs.values()
                          if isinstance(pull, dict)
                          and isinstance(pull.get("number"), int)}
    out = []
    for path, branch in list_worktrees(repo):
        verdict = classify_one(path, branch, merged_prs, claims, default_branch)
        out.append(apply_disposition(verdict,
                                     dispositions.get(os.path.realpath(path)),
                                     merged_numbers))
    return out


def slug_from_remote(repo):
    """`owner/name` for the origin remote, or None if it is not a GitHub URL."""
    url = _git(repo, "remote", "get-url", "origin")
    if not url:
        return None
    if url.endswith(".git"):
        url = url[:-4]
    if url.startswith("git@"):
        _, _, tail = url.partition(":")
    elif "://" in url:
        tail = url.split("://", 1)[1].partition("/")[2]
    else:
        return None
    parts = [p for p in tail.split("/") if p]
    return "/".join(parts[-2:]) if len(parts) >= 2 else None


def fetch_merged_prs(repo):
    """Merged PRs for this repo as {branch: {number, oid}}, or None.

    None on every failure mode -- no `gh`, not authenticated, offline, not a
    GitHub remote, rate limited. The caller reports UNKNOWN rather than
    guessing, so none of these need telling apart.

    `GIT_TIDY_PR_DATA` points at a JSON file with the same shape as the `gh`
    response and is how the tests stay hermetic. It only ever substitutes an
    input to a read-only report -- there is nothing here for it to bypass.
    """
    override = os.environ.get("GIT_TIDY_PR_DATA")
    if override:
        try:
            with open(override, encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return None
        return _index(payload)

    slug = slug_from_remote(repo)
    if not slug:
        return None
    try:
        out = subprocess.run(
            ["gh", "pr", "list", "-R", slug, "--state", "merged",
             "--limit", "300", "--json", "headRefName,headRefOid,number"],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        return _index(json.loads(out.stdout or "[]"))
    except Exception:
        return None


def _index(payload):
    """Newest merged PR wins when a branch name was reused across PRs.

    `gh` returns newest first, so the first record for a branch is the one to
    keep -- an older PR's head commit would make a current branch look like it
    had unmerged commits piled on top of a merge that has since been
    superseded.
    """
    if not isinstance(payload, list):
        return None
    index = {}
    for record in payload:
        if not isinstance(record, dict):
            continue
        branch = record.get("headRefName")
        if not branch or branch in index:
            continue
        index[branch] = {"number": record.get("number"),
                         "oid": record.get("headRefOid")}
    return index


def process_state(pid):
    """True if `pid` is running, False if positively not, None if we cannot tell.

    `None` exists because a sandbox, or a platform without POSIX `ps`, cannot
    inspect processes, and a failed inspection used to read as "dead" -- which
    made a live session's worktree removable (R2-01). Only a positive answer
    may clear a claim.

    The dead test is this strict on purpose: it is the measured macOS shape
    (exit 1, empty stderr, exactly the one header line on stdout). An
    out-of-range pid prints to stderr and is treated as unknown.

    Deliberately not `tools/session-claim/identity_lib.is_pid_live`: that
    returns False on `PermissionError`, which is right for lockout prevention
    and wrong for a deleter, and it probes with `os.kill`, which is not a safe
    probe on Windows.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        result = subprocess.run(["ps", "-p", str(pid)], capture_output=True,
                                text=True, encoding="utf-8", timeout=10)
    except Exception:
        return None
    if result.returncode == 0:
        return True
    if (result.returncode == 1 and result.stderr.strip() == ""
            and len(result.stdout.strip().splitlines()) == 1):
        return False
    return None


def load_claims(path=None):
    """{realpath: (session name, True | False | None)} for every claim.

    The second element is `process_state`: running, positively not running, or
    unknown. A claim with no pid is unknown, not dead.

    `GIT_TIDY_CLAIMS_PATH` redirects the lookup and is how the tests avoid
    reading the machine's real claims. Like `GIT_TIDY_PR_DATA` it substitutes
    an input to a read-only report and gates nothing.
    """
    path = (path or os.environ.get("GIT_TIDY_CLAIMS_PATH")
            or os.path.expanduser("~/.claude/session-claims/claims.json"))
    try:
        with open(path, encoding="utf-8") as handle:
            records = json.load(handle)
    except Exception:
        return {}
    if not isinstance(records, list):
        return {}
    claims = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("checkout"):
            continue
        pid = record.get("pid")
        alive = process_state(pid) if pid else None
        claims[os.path.realpath(record["checkout"])] = (
            record.get("name") or "unnamed", alive)
    return claims
