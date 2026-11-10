"""Builds the reviewer's whole merge-context framing, entirely from `git` and
`gh` output.

A hand-assembled review brief leaks the author's rationale through
*selection* — what got pasted in is an argument. This module has no input by
which a plan, spec, or architect prose could enter as COMMENTARY alongside a
diff: every diff-mode section is built from a `git` or `gh` command this file
runs itself. There is no `--context` flag and there must never be one; a test
asserts that by argument surface.

**`--plan` is the one carve-out, and it is not a reopening of that
prohibition.** It does not add prose alongside a diff — it names the
artifact a Plan Review actually reviews, prefixed by a fixed requirements
block this repo ships, identical every run. See `build_plan_brief`'s
docstring for why that distinction holds.

Three traps, measured, are why this exists instead of a hand checklist:

1. The merge-ref fetch refspec needs a leading `+`, or a *re*-fetch after a
   fix round is rejected non-fast-forward and the local ref silently keeps
   the old merge.
2. `+` is necessary and not sufficient — GitHub computes `refs/pull/<N>/merge`
   lazily, so a stale one still fetches with exit 0. The freshness check
   (merge ref's first parent == the base) is mandatory and fails CLOSED;
   `mergeable: null` is treated as stale, not as fine.
3. A diff range built from a bare local branch name drags unrelated,
   already-merged files into the review surface. Every range here is built
   from `origin/<base>`, never a local branch name. One exception, and only
   in `--pre-pr`: a repo with NO remote named `origin` at all (a local-only
   project) has no upstream for a local branch to be stale against, so its
   local base branch is the base. That is keyed on the remote being absent,
   never on a fetch failing: an `origin` that is configured but unreachable
   still refuses. See `pre_pr_base_ref`.

A fourth case: once a PR closes, `refs/pull/<N>/merge` stops existing and the
fetch fails with `couldn't find remote ref` — which reads like a broken
command and means a merged (or otherwise closed) PR. That failure is caught
and given an actionable message rather than surfaced as raw git stderr.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Tuple

# `realpath`, not `abspath`: this module is reached through a `~/.local/bin`
# symlink for the executable that imports it, and `abspath` does not resolve
# a symlink. Imported normally, `__file__` is already the resolved path; this
# keeps `freshness_lib` reachable if a caller loads the module by a link path.
_TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.join(_TOOLS_DIR, "repo-freshness"))
import freshness_lib  # noqa: E402

# Same `realpath` rule as `_TOOLS_DIR` above, resolved once at import time,
# right beside this module -- `tools/merge-brief/plan-requirements.md`, never
# `tools/plan-requirements.md` and never a symlink's own directory. Exposed
# as a module attribute (not folded inline into `_read_plan_requirements`)
# so a test can point it at a missing or empty file without touching the
# real, shared one.
PLAN_REQUIREMENTS_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                       "plan-requirements.md")

# Re-exported rather than reimplemented: `freshness_lib`'s docstring says its
# resolution order is kept in step with the `git tidy` alias, and a third
# caller here is fine — a second RESOLVER is not.
default_branch = freshness_lib.default_branch

# Every git call is capped so a wedged repo or a dead network can never hang
# the tool. Reads are fast; fetch/diff/merge get more room for a large repo.
GIT_TIMEOUT_SECONDS = 5
FETCH_TIMEOUT_SECONDS = 30
DIFF_TIMEOUT_SECONDS = 30
GH_TIMEOUT_SECONDS = 30

# A silently truncated diff produces a reviewer that reports clean on the
# part it never saw. Past this size
# the tool shows the file list and says plainly that the reviewer must Read
# the files itself.
MAX_DIFF_BYTES = 400_000

# Says what was actually searched rather than asserting a disk measurement.
# A blinded review measured the overclaim in the first cut ("no test files or
# CI workflows found on disk either"): `_TEST_FILE_RE` knows Python, Go, JS/TS
# and `*_test.rb`, and misses RSpec `*_spec.rb`, `*Test.java`, `*Tests.cs`,
# bats, `test_*.sh`, Elixir and Rust `tests/*.rs` — so an RSpec repo with two
# spec files got the full sentence. The old wording was also wrong there, but
# VAGUELY; dressing the same false negative as a measurement is the fail-open
# direction this whole change exists to close. Naming the limit costs one
# clause and cannot go stale the way an assertion can.
NO_SUITE_TEXT = ("(no command documented in CLAUDE.md, and no CI workflow or "
                 "common-convention test file tracked in git — note this "
                 "recognises Python/Go/JS/TS/Ruby naming, so a suite in another "
                 "language may exist unseen. Look before falsifying by hand.)")

EVIDENCE_SAMPLE = 8

# Basenames that ARE a test suite. Conservative on purpose -- this decides
# whether the brief is allowed to say "no suite", so a false positive here
# suppresses a true negative, which is the milder of the two errors but still
# an error.
_TEST_FILE_RE = re.compile(
    r"^(?:test_.+|.+_test)\.(?:py|rb|go|js|ts)$"
    r"|^.+\.(?:test|spec)\.(?:js|jsx|ts|tsx)$"
    r"|^run_all\.py$|^conftest\.py$|^tests?\.py$")

_WORKFLOW_PATH_RE = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")


class BriefError(Exception):
    """A precondition or freshness failure.

    The executable prints `str(exc)` to stderr and exits 2 — a brief is never
    written, on stdout or at `-o`, when this is raised.
    """


class PlanFileError(BriefError):
    """The `--plan` file itself is missing, unreadable, or empty.

    A subclass of `BriefError`, but the executable checks for THIS class
    first and exits 3, not 2 — a bad `--plan` path is the caller's usage
    mistake, the same class of error as an unrecognized flag, not a repo
    precondition. `plan-requirements.md` being missing is the opposite case:
    the caller did nothing wrong, the repo's own fixed file is broken, and
    that stays a `BriefError` at exit 2.
    """


def _git_output(repo: str, *args: str, timeout: int = GIT_TIMEOUT_SECONDS) -> Optional[str]:
    """A read-only git command's stdout, or None on any failure at all.

    None means "the command failed" and is distinct from "" (the command
    succeeded and printed nothing) — callers that need to tell an empty diff
    apart from a bad ref rely on that distinction.
    """
    try:
        result = subprocess.run(
            ("git", "-C", repo) + args,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", "replace").strip()


def _git_run(repo: str, args: Tuple[str, ...],
             timeout: int = GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Any git command, stdout+stderr captured as text, never raising.

    Unlike `_git_output`, callers here need the return code and stderr text
    (a fetch failure is not the same failure as a diff failure, and the
    closed-PR case is told apart by matching stderr), so this always returns
    a CompletedProcess rather than collapsing every failure to None.
    """
    cmd = ("git", "-C", repo) + tuple(args)
    try:
        return subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", "timed out after %ss" % timeout)
    except Exception as exc:
        return subprocess.CompletedProcess(cmd, 1, "", str(exc))


def _gh_run(repo: str, args: Tuple[str, ...],
            timeout: int = GH_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """A `gh` command run with cwd=repo, so `gh` infers owner/repo from the
    checkout's own remote rather than needing it named here."""
    cmd = ("gh",) + tuple(args)
    try:
        return subprocess.run(
            cmd, cwd=repo, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        raise BriefError("the gh CLI is not installed or not on PATH — required for --pr mode")
    except subprocess.TimeoutExpired:
        raise BriefError("gh %s timed out after %ss" % (" ".join(args), timeout))


def _ensure_repo(repo: str) -> None:
    """Raise BriefError unless `repo` is a real git working tree."""
    if not os.path.isdir(repo):
        raise BriefError("no such directory: %r" % repo)
    if _git_output(repo, "rev-parse", "--show-toplevel") is None:
        raise BriefError("%r is not inside a git working tree" % repo)


# --------------------------------------------------------------------------
# PR mode: fetch + freshness
# --------------------------------------------------------------------------

def pr_base_and_mergeable(repo: str, pr_number: int) -> Tuple[str, Optional[bool]]:
    """The PR's *actual* base branch and GitHub's `mergeable` verdict.

    Not necessarily the default branch — assuming it is would silently diff
    the wrong surface. `mergeable` is None when GitHub has not computed it
    yet, which the freshness check downstream treats as stale, not as fine.
    """
    result = _gh_run(repo, ("api", "repos/{owner}/{repo}/pulls/%d" % pr_number),
                      timeout=GH_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise BriefError("gh api could not fetch PR #%d:\n%s" % (pr_number, result.stderr.strip()))
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise BriefError("gh api returned unparseable JSON for PR #%d" % pr_number)
    base_ref = (payload.get("base") or {}).get("ref")
    if not base_ref:
        raise BriefError("gh api response for PR #%d has no base.ref" % pr_number)
    return base_ref, payload.get("mergeable")


def fetch_pr_merge_ref(repo: str, pr_number: int, base: str) -> None:
    """Fetch the PR's merge ref and refresh `origin/<base>` in one call.

    Trap 1: the leading `+` on the merge-ref refspec is load-bearing. GitHub
    recomputes `refs/pull/<N>/merge` on every push, so a stale local copy
    from an earlier fix round is not a fast-forward of the new one — without
    `+`, a re-fetch is REJECTED (non-fast-forward, exit 1) and the local ref
    silently keeps the old merge, so a re-review reads code that no longer
    exists.

    Trap 4: once a PR closes, the ref is gone. Git's own message —
    `couldn't find remote ref refs/pull/<N>/merge` — reads like a broken
    command. Caught here and replaced with the actionable fallback.
    """
    merge_refspec = "+refs/pull/%d/merge:refs/remotes/origin/pr-%d-merge" % (pr_number, pr_number)
    base_refspec = "+%s:refs/remotes/origin/%s" % (base, base)
    result = _git_run(repo, ("fetch", "origin", merge_refspec, base_refspec),
                       timeout=FETCH_TIMEOUT_SECONDS)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        merge_ref_path = "refs/pull/%d/merge" % pr_number
        # Matched against the SPECIFIC ref path, not the bare phrase: the base
        # refspec in this same fetch can fail with the identical git wording
        # ("couldn't find remote ref main") when the base itself is missing,
        # and that is a different problem from a closed PR.
        if "couldn't find remote ref" in stderr.lower() and merge_ref_path in stderr:
            raise BriefError(
                "PR #%d has no refs/pull/%d/merge to fetch. That reads like a "
                "broken command; it means the PR is closed — GitHub deletes "
                "this ref once a PR closes, merged or not. Review the squash "
                "commit instead: git diff <squash-commit>^ <squash-commit>"
                % (pr_number, pr_number))
        raise BriefError("git fetch of PR #%d's merge ref failed:\n%s" % (pr_number, stderr))


def check_freshness(repo: str, pr_number: int, base: str,
                     mergeable: Optional[bool]) -> Tuple[Optional[str], Optional[str]]:
    """Raise BriefError unless the merge ref is fresh; else return both SHAs.

    Trap 2: GitHub computes `refs/pull/<N>/merge` lazily, so a `+` fetch of a
    stale one still exits 0. The only test that matters is the merge ref's
    first parent against the base: `mergeable: null` means "not computed"
    and must be treated as stale exactly like a `^1` mismatch — not as fine.
    """
    merge_ref = "origin/pr-%d-merge" % pr_number
    parent_sha = _git_output(repo, "rev-parse", "%s^1" % merge_ref)
    base_sha = _git_output(repo, "rev-parse", "origin/%s" % base)

    reasons = []
    if mergeable is None:
        reasons.append("gh reports mergeable=null (GitHub has not computed this merge yet)")
    if parent_sha is None or base_sha is None or parent_sha != base_sha:
        reasons.append("%s^1 does not match origin/%s" % (merge_ref, base))

    if reasons:
        raise BriefError(
            "%s looks stale, not reviewable:\n  - %s\nCompared: %s^1 = %s vs origin/%s = %s"
            % (merge_ref, "\n  - ".join(reasons), merge_ref, parent_sha or "?",
               base, base_sha or "?"))
    return parent_sha, base_sha


# --------------------------------------------------------------------------
# Pre-PR mode: merge into a scratch worktree
# --------------------------------------------------------------------------

def merge_ref_name(repo: str) -> str:
    """Where a pre-PR merge is parked, namespaced by branch.

    Branch-scoped rather than a single `latest`, because two sessions
    reviewing two branches in one repo is the normal case here, and a shared
    ref would silently hand one of them the other's merge — the exact
    "reviewing something other than what you think" family this tool exists
    to close.
    """
    branch = _git_output(repo, "rev-parse", "--abbrev-ref", "HEAD") or "DETACHED"
    if branch == "HEAD":                       # detached: no branch name to use
        branch = "detached-" + (_git_output(repo, "rev-parse", "--short", "HEAD") or "unknown")
    return "refs/merge-brief/%s" % branch


def working_tree_dirty(repo: str) -> Optional[str]:
    """Uncommitted changes as porcelain text, or None if the tree is clean.

    None means clean. It does NOT mean "could not tell" — `_git_output`
    returns None on failure too, so that case is converted to a raised error
    by the caller rather than silently read as clean.
    """
    out = _git_output(repo, "status", "--porcelain")
    if out is None:
        raise BriefError("could not read `git status` in %r — refusing to build a "
                          "brief that might not describe what is on disk" % repo)
    return out or None


def pre_pr_base_ref(repo: str, base: str) -> Tuple[str, bool]:
    """The ref `--pre-pr` merges and diffs against, and whether it is local.

    `origin/<base>` whenever the repo HAS a remote named `origin`, reachable or
    not: an unreachable origin must fail the fetch loudly, never fall back to a
    local branch that may be stale against it (trap 3). Only a repo with no
    `origin` remote at all uses `refs/heads/<base>`, because there is nothing
    for that branch to be behind. "Could not list the remotes" is not "no
    remote", so it raises rather than picking the local path.
    """
    remotes = _git_output(repo, "remote")
    if remotes is None:
        raise BriefError("could not list git remotes in %r, so cannot tell whether "
                         "to review against origin/%s or a local branch" % (repo, base))
    if "origin" in remotes.split():
        return "origin/%s" % base, False
    return "refs/heads/%s" % base, True


def _local_branches(repo: str) -> List[str]:
    out = _git_output(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [b for b in (out or "").splitlines() if b.strip()]


def build_pre_pr_merge(repo: str, base: str) -> Tuple[str, str, str]:
    """Merge the base (`pre_pr_base_ref`) into a detached scratch worktree; never the
    caller's checkout. Returns (start_sha, base_sha, merge_sha).

    A merge conflict is a legitimate result, not a bug: raised as a
    BriefError naming the conflicted paths, never reviewed as a conflicted
    tree. The worktree is removed on every exit path, including that one.

    **The merge is parked at `refs/merge-brief/<branch>` before the worktree
    goes away, and that is load-bearing rather than tidy.** Without it the
    merge commit is reachable from nothing: it survives `gc` for two weeks and
    is findable by nobody, so a reviewer told to "run the suite" runs it
    against the BRANCH and reports, truthfully, that it executed. The report
    cannot distinguish that from a merge-tree run — which makes the one class
    of defect this whole gate exists for (a branch that is fine and a merge
    that is not) precisely the class the default path cannot reach. It
    happens whenever the base moved meanwhile: a tool removed on `main` that
    the branch still runs leaves every branch suite green and one merge suite
    missing.

    **A dirty tree is refused, not warned about.** The freshness check next to
    it fails closed, and failing open here would be backwards: an
    uncommitted change is not in the merge, so the brief would describe a tree
    that does not exist while looking complete. Same asymmetry as the
    freshness check: fail strict.
    """
    dirty = working_tree_dirty(repo)
    if dirty:
        raise BriefError(
            "the working tree in %r has uncommitted changes, so a merge built "
            "from HEAD would not contain them:\n%s\n"
            "Commit first. Merge Review reviews a merge, and uncommitted work is not in "
            "one — this refuses rather than warns because a brief that looks "
            "complete and describes a tree nobody will ever have is worse than "
            "no brief." % (repo, dirty))

    start_sha = _git_output(repo, "rev-parse", "HEAD")
    if not start_sha:
        raise BriefError("could not resolve HEAD in %r" % repo)

    base_ref, local = pre_pr_base_ref(repo, base)
    if local:
        base_sha = _git_output(repo, "rev-parse", "--verify", "--quiet",
                               "%s^{commit}" % base_ref)
        if not base_sha:
            branches = _local_branches(repo)
            raise BriefError(
                "this repo has no `origin` remote, so the base is a local branch, and "
                "there is no local branch %r. Local branches: %s.\nPass the branch "
                "this work started from: claude-merge-brief --pre-pr --base <branch>"
                % (base, ", ".join(branches) if branches else "(none)"))
    else:
        fetch = _git_run(repo, ("fetch", "origin", "+%s:refs/remotes/origin/%s" % (base, base)),
                          timeout=FETCH_TIMEOUT_SECONDS)
        if fetch.returncode != 0:
            raise BriefError("could not fetch origin/%s:\n%s" % (base, fetch.stderr.strip()))

        base_sha = _git_output(repo, "rev-parse", "origin/%s" % base)
        if not base_sha:
            raise BriefError("origin/%s does not exist in %r after fetch" % (base, repo))

    tmpdir = tempfile.mkdtemp(prefix="claude-merge-brief-")
    worktree_added = False
    try:
        add = _git_run(repo, ("worktree", "add", "--detach", tmpdir, "HEAD"),
                        timeout=FETCH_TIMEOUT_SECONDS)
        if add.returncode != 0:
            raise BriefError("could not create a scratch worktree for the merge:\n%s"
                              % add.stderr.strip())
        worktree_added = True

        merge = _git_run(tmpdir, ("merge", "--no-edit", base_ref),
                          timeout=FETCH_TIMEOUT_SECONDS)
        if merge.returncode != 0:
            conflicted = _git_output(tmpdir, "diff", "--name-only", "--diff-filter=U") or ""
            paths = [p for p in conflicted.splitlines() if p.strip()]
            lines = ["the branch does not merge cleanly into %s." % base_ref,
                     "Do not review a conflicted tree. Conflicted paths:"]
            lines += (["  %s" % p for p in paths] if paths else ["  (none reported by git)"])
            raise BriefError("\n".join(lines))

        merge_sha = _git_output(tmpdir, "rev-parse", "HEAD")
        if not merge_sha:
            raise BriefError("merge into the scratch worktree succeeded but HEAD "
                              "could not be resolved")

        # Park it BEFORE the finally block drops the worktree — this is the
        # only thing keeping the merge reachable, and the brief tells the
        # reviewer to check it out. Failing to park is fatal rather than
        # cosmetic: a brief naming a ref that does not exist sends the
        # reviewer back to the branch, which is the defect this fixes.
        ref = merge_ref_name(repo)
        parked = _git_run(repo, ("update-ref", ref, merge_sha),
                           timeout=FETCH_TIMEOUT_SECONDS)
        if parked.returncode != 0:
            raise BriefError(
                "built the merge (%s) but could not park it at %s:\n%s\n"
                "Refusing to emit a brief for a merge nothing can reach."
                % (merge_sha[:12], ref, parked.stderr.strip()))
        return start_sha, base_sha, merge_sha
    finally:
        if worktree_added:
            _git_run(repo, ("worktree", "remove", "--force", tmpdir),
                      timeout=FETCH_TIMEOUT_SECONDS)
        # Backstop: `worktree remove` already deletes the directory on
        # success, but never leave a scratch dir behind on any other exit
        # path (add failure, an unexpected exception).
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Brief sections
# --------------------------------------------------------------------------

def diff_stat(repo: str, left: str, right: str) -> str:
    out = _git_output(repo, "diff", "--stat", left, right, timeout=DIFF_TIMEOUT_SECONDS)
    if out is None:
        raise BriefError("git diff --stat %s %s failed — is %s a valid ref? "
                          "(a bad --since value lands here)" % (left, right, left))
    return out


def commit_messages(repo: str, left: str, right: str) -> str:
    range_expr = "%s..%s" % (left, right)
    out = _git_output(repo, "log", "--format=%h %s%n%b", range_expr, timeout=DIFF_TIMEOUT_SECONDS)
    if out is None:
        raise BriefError("git log %s failed — is %s a valid ref?" % (range_expr, left))
    return out


def full_diff(repo: str, left: str, right: str) -> str:
    out = _git_output(repo, "diff", left, right, timeout=DIFF_TIMEOUT_SECONDS)
    if out is None:
        raise BriefError("git diff %s %s failed — is %s a valid ref?" % (left, right, left))
    return out


def diff_name_only(repo: str, left: str, right: str) -> str:
    return _git_output(repo, "diff", "--name-only", left, right,
                        timeout=DIFF_TIMEOUT_SECONDS) or ""


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# Single-backtick inline spans. Deliberately excludes backticks and newlines
# from the body, so an unclosed span cannot swallow the rest of the line.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# A heading that is ABOUT testing -- on word boundaries, not a substring.
# "test" as a substring also matches "Fastest path", "Latest changes" and
# "Protest"; a blinded review measured `## Fastest path` hijacking the
# search and returning `./deploy.sh --prod` as the suite command. That
# predates the inline-span fallback (a fenced block in such a section was
# always enough), but the fallback WIDENS it, because prose spans are far
# more common than fenced blocks -- so it is fixed here rather than left
# as inherited.
_TEST_HEADING_RE = re.compile(r"\btests?\b|\btesting\b", re.IGNORECASE)

# A token inside the span itself that says the command is about tests. Note
# `run_all` and `unittest` are here because `tests?\b` does not match them.
_TEST_TOKEN_RE = re.compile(r"\btests?\b|pytest|unittest|run_all|testing",
                             re.IGNORECASE)

# Argv[0]s that RUN something. Basenames, so `.venv/bin/python` resolves via
# `python`. This is an allowlist rather than a blocklist on purpose: the
# unknown case must return nothing, per the asymmetry in `_looks_like_a_command`.
_RUNNERS = frozenset((
    "python", "python2", "python3", "py", "pytest", "tox", "unittest", "nose",
    "make", "npm", "npx", "yarn", "pnpm", "node", "deno", "bun",
    "go", "cargo", "rake", "bundle", "mvn", "gradle", "dotnet", "swift",
    "bash", "sh", "zsh", "ruby", "perl", "php", "java", "dart", "flutter",
))


def _looks_like_a_command(span: str) -> bool:
    """Is this span a suite command, or just a word in backticks?

    **It must carry ARGUMENTS.** A bare identifier is rejected, whatever it
    looks like.

    That is deliberately stricter than the obvious rule, and the asymmetry is
    the reason. Getting this wrong has two very different costs:

    * Return NOTHING when a suite exists → the brief says "no suite in this
      repo" and the reviewer falsifies by hand. Bad — it is the whole bug this
      fallback was added to fix — but it is the pre-existing behaviour and it
      is *visible* in the review output.
    * Return the WRONG command → the reviewer runs it. If it fails, fine, that
      is loud. If it **succeeds**, they report "suite green" having run
      something else entirely, and nothing anywhere says otherwise.

    The second is strictly worse for a guard that already fails open, so the
    tie-breaks go toward returning nothing.

    A first cut accepted a bare word unless it carried `/` or `.`. A blinded
    review measured what that admits from real prose: `skipTest`, `-x`,
    `PYTHONPATH`, `test_guard_is_armed` — and `ruff`, which is the one that
    matters. `ruff` is a REAL tool that exits 0 and prints nothing alarming,
    so ``Run `ruff` first, then `python3 -m pytest``` would have had a
    reviewer lint the repo and call it a passing suite. A `## Testing`
    section that says "where an environment dependency is unavoidable,
    `skipTest`" is safe only while it happens to lead with a fence.

    **Accepted gap, stated rather than implied:** a repo documenting a
    one-word command (`tox`, `make`) in prose alone gets None. That is
    unchanged from before this fallback existed, so it is a case not yet
    fixed rather than one newly broken — and adding a fence fixes it.
    """
    return span.split() != [span]


def _is_a_test_target(token: str) -> bool:
    """Is this argument a thing tests live in, rather than a word containing
    "test"?

    **Flags are excluded outright**, and that is the point of the function.
    `\\btests?\\b` treats `-` as a word boundary, so `--skip-tests`,
    `--no-tests`, `--ignore-tests` and `--tests-passed` all "name tests" to a
    naive search — which is how a deploy script's own opt-out flag came to
    satisfy the test-token half of this rule.

    What survives is a path whose basename or any segment IS a test location
    (`tests/`, `test/`, `test_foo.py`) or a known runner module (`pytest`,
    `unittest`, `run_all`). A token that merely CONTAINS the word does not
    qualify: `requirements-test.txt` is a real argument to a real `pip install`
    that a reviewer would run to completion, and it is not a suite.
    """
    if not token or token.startswith("-"):
        return False
    if re.search(r"\b(?:pytest|unittest|run_all)\b", token, re.IGNORECASE):
        return True
    segments = [s for s in token.replace("\\", "/").split("/") if s]
    for segment in segments:
        if segment.lower() in ("test", "tests"):
            return True
    basename = segments[-1] if segments else token
    return bool(re.match(r"^tests?_", basename, re.IGNORECASE))


def _runs_tests(span: str) -> bool:
    """Does this span, read with NO surrounding context, run a test suite?

    Used only outside a test-titled heading. Inside one the section itself is
    the evidence that a span is the suite command; outside one the span has to
    carry its own. Two ways to qualify:

    1. **argv[0]'s basename is a known runner** AND some non-flag argument is a
       test target (`pytest`, `tests/…`, `test_foo.py`).
    2. **argv[0] itself names tests** — `./run-tests.sh`, `bin/pytest-all`.

    **An explicit path is NOT accepted on its own, and the first cut's bug was
    exactly that.** It read "a `./`- or `/`-prefixed head is a thing being
    executed whatever it is called" and returned True with no further check,
    leaving the conjunction to be satisfied by the word "test" appearing
    anywhere in the span — including inside a flag. A blinded review measured
    what that admits, all returned as the suite command:

        ./deploy.sh --skip-tests        ./deploy.sh --no-tests
        ./release.sh --tests-passed     sh scripts/down.sh --test-stack
        npm run build --ignore-tests    /bin/rm -rf tests/tmp
        python3 -m pip install -r requirements-test.txt

    The first six are the catastrophic shape this function's own docstring
    calls the worst outcome it has: a blinded reviewer, told to run the suite,
    shipping to production or deleting a directory. The `pip install` one is
    likelier and worse in a quieter way — it exits 0, prints success, silently
    mutates the reviewer's Python environment, and reads as a green suite.

    The repos it was tested on not hitting this is not the bar: the tool runs
    against arbitrary repos.

    **Accepted gap:** a repo whose runner is not on the list, documented only
    in prose outside a test heading, gets None — the pre-existing behaviour,
    not a new break. Widening `_RUNNERS` is the fix, and it is a decision to
    take deliberately rather than by loosening the shape of the rule.
    """
    tokens = span.split()
    if not tokens:
        return False
    head, args = tokens[0], tokens[1:]
    basename = os.path.basename(head.rstrip("/")) or head
    if _TEST_TOKEN_RE.search(basename):
        return True                       # ./run-tests.sh, bin/pytest-all
    if basename not in _RUNNERS:
        return False
    return any(_is_a_test_target(arg) for arg in args)


def find_suite_command(repo: str) -> Optional[str]:
    """The repo's own documented test command, or None.

    Resolved generically from `CLAUDE.md` rather than a hardcoded per-repo
    list: the first heading whose title mentions "test", and the first
    fenced code block inside that section.

    Heading detection is suspended for the duration of ANY fenced code
    block, not just the target one — a doc that shows a shell comment or an
    example markdown heading inside an unrelated snippet must never be read
    as real document structure.

    **If the section has no fenced block, fall back to the first INLINE code
    span in it that is not a bare file path.** A fence still wins wherever
    one exists, so no repo that already resolved changes.

    That fallback is not a nicety. Fenced-only detection returns None for a
    repo whose `## Testing` section documents its command in an inline span
    with no fence, however many tests it has, and the brief renders
    `NO_SUITE_TEXT`. A blinded reviewer is then told to falsify by hand, and
    "no suite in this repo" reads exactly like a repo that genuinely has
    none: **a review guard failing OPEN.** It is easy to miss, because the
    tool WORKS in a repo that uses a fence and FAILS in the repo it is used
    on.

    `_looks_like_a_command` is what keeps that fallback honest: a span must
    carry arguments, so a bare `ruff` cannot have a reviewer lint the repo and
    report a green suite.

    **Third tier: if there is no test-titled heading AT ALL, scan the whole
    document for a prose line about tests.** That is a SECOND root cause, and
    it is not the same bug as the first. A repo can document its command
    under a heading like `## Repo conventions` — "Run a skill's suite with
    ``python3 tests/<skill-name>/run_all.py``" — so there is no heading to
    search, and the first two tiers return None *correctly by their own
    logic* while being wrong about a repo with several suites.

    **Widening where we look is paid for by tightening what we accept.** A
    span found this way must additionally satisfy `_runs_tests`, which is
    strictly stronger than the in-section rule. Detection is deliberately
    strict (see `_looks_like_a_command`) and nothing here relaxes it: every
    earlier tier behaves exactly as before, and this tier is reached only
    where they all returned None.

    **Ordering is load-bearing, not cosmetic.** A `CLAUDE.md` can contain
    `set -e` and `... FAIL` in prose lines that mention tests. One that
    resolves at tier 1 (its `## Testing` section leads with a fence) never
    reaches the document-wide scan — but "never reached" is not a
    guarantee, which is why `_runs_tests` rejects both on their own merits
    too. Do not reorder the tiers, and do not let tier 3 run when a test
    heading exists: a test section that documents nothing runnable means
    "no documented command", not "go looking elsewhere".
    """
    claude_md = os.path.join(repo, "CLAUDE.md")
    if not os.path.isfile(claude_md):
        return None
    try:
        with open(claude_md, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None

    in_section = False
    section_level = None
    saw_a_test_heading = False
    inside_any_fence = False
    capturing = False
    code_lines = []
    inline_fallback = None
    document_fallback = None

    for line in text.splitlines():
        is_fence = line.strip().startswith("```")

        if capturing:
            if is_fence:
                return "\n".join(code_lines)
            code_lines.append(line)
            continue

        if is_fence:
            inside_any_fence = not inside_any_fence
            if in_section and inside_any_fence:
                capturing = True
                code_lines = []
            continue

        if inside_any_fence:
            continue  # content of an unrelated fence is never document structure

        match = _HEADING_RE.match(line)
        if not match:
            # Prose. Inside the test section it may carry the command as an
            # inline span; remember the first plausible one but keep scanning,
            # because a fence later in the same section still outranks it.
            if in_section and inline_fallback is None:
                for span in _INLINE_CODE_RE.findall(line):
                    span = span.strip()
                    if span and _looks_like_a_command(span):
                        inline_fallback = span
                        break
            # Tier-3 candidate, gathered unconditionally and consumed only if
            # the document turns out to have no test heading at all. Kept out
            # of the tier-1/2 control flow deliberately: precedence then lives
            # in ONE place (the return below) instead of being a property of
            # which branch happens to run first, so it cannot be broken by
            # editing either branch alone.
            #
            # There is deliberately NO extra gate on the LINE's prose. One was
            # written and deleted the same day: a mutation removing it survived
            # the whole suite, and the reason it survived is that it was
            # near-redundant by construction -- the span sits inside the line,
            # so a `\btests?\b` line check is satisfied by almost every span
            # `_TEST_TOKEN_RE` already accepted. Where the two DID differ it
            # was wrong: `pytest`, `run_all` and `unittest` carry no standalone
            # "test" word, so ``Invoke `pytest -q` from the root.`` was rejected
            # -- a false negative, in the fail-open direction, on one of the
            # most common ways a suite is documented. `_runs_tests` is the
            # whole acceptance rule.
            if document_fallback is None:
                for span in _INLINE_CODE_RE.findall(line):
                    span = span.strip()
                    if span and _looks_like_a_command(span) and _runs_tests(span):
                        document_fallback = span
                        break
            continue
        level, title = len(match.group(1)), match.group(2).strip()
        if in_section and level <= section_level:
            break  # left the test section; the inline fallback is all we have
        if not in_section and _TEST_HEADING_RE.search(title):
            in_section = True
            section_level = level
            saw_a_test_heading = True

    if inline_fallback is not None:
        return inline_fallback
    # A test section that documents nothing runnable means "this repo does not
    # document a command", NOT "look somewhere else" -- the scoping in tiers
    # 1-2 is the whole point, and letting tier 3 rescue that case would undo it.
    if saw_a_test_heading:
        return None
    return document_fallback


def suite_evidence(repo: str) -> Tuple[List[str], List[str]]:
    """(CI workflow paths, test file paths) found on disk, repo-relative.

    **This exists so the brief can never again claim a repo has no suite on
    the strength of failing to parse its prose.** `find_suite_command` reads
    one file and answers "is a command DOCUMENTED"; that is a different
    question from "does this repo HAVE tests", and conflating them is what
    made the `## Suite` section a guard that fails open. It can do so for two
    unrelated reasons (the fenced-block gap, and the no-test-heading gap this
    sits beside), which is the argument for fixing the CLASS rather than a
    third parser edge.

    Deliberately returns PATHS and never commands. Extracting `run:` steps out
    of workflows would name a check command exactly — and would also, on one
    mis-scoped workflow, hand a blinded reviewer a deploy command to run. A
    path a reviewer reads costs one file open; a command a reviewer runs
    cannot be taken back.

    **Enumerated with `git ls-files`, never an `os.walk`.** A walk was the
    first cut and it was wrong: concurrent sessions keep their checkouts in
    `.claude/worktrees/` inside the repo, so a walk counts every test file in
    every OTHER session's tree too, often several times the repo's own.
    Handing a reviewer those paths points them at a peer's working tree,
    where running anything can disturb a live session's files. Tracked files
    are also the right SET on the merits: gitignored
    build output, virtualenvs and vendored dependencies drop out with no
    prune list to maintain and drift.

    The claim this supports — "this repo carries tests" — is not
    SHA-sensitive, so reading the checkout's index rather than the merge tree
    is deliberate and costs nothing.

    Never raises: a git failure yields no evidence, which degrades to the
    pre-existing wording rather than failing the brief.
    """
    workflows: List[str] = []
    tests: List[str] = []
    listing = _git_output(repo, "ls-files", "-z", timeout=DIFF_TIMEOUT_SECONDS)
    if not listing:
        return workflows, tests
    for path in sorted(p for p in listing.split("\0") if p):
        if _WORKFLOW_PATH_RE.match(path):
            workflows.append(path)
        elif _TEST_FILE_RE.match(os.path.basename(path)):
            tests.append(path)
    return workflows, tests


def render_suite_section(repo: str) -> str:
    """The body of the brief's `## Suite` section.

    Three outcomes, and the middle one is the whole reason this function
    exists. A reviewer's "nothing was executed" is only informative if the
    brief could tell "this repo has no tests" apart from "this tool could not
    find them" — and until now it rendered both as the same sentence.
    """
    command = find_suite_command(repo)
    workflows, tests = suite_evidence(repo)

    lines: List[str] = []
    if command:
        lines += ["```", command, "```",
                  "", "Resolved from this repo's own `CLAUDE.md`."]
    elif tests:
        # Only TEST FILES license this claim. Workflows alone used to, and a
        # blinded review measured the result: a repo with one `deploy.yml` and
        # zero tests rendered "this repo DOES carry tests ... find the command
        # from the evidence below and run it" over a deploy workflow. That is a
        # false statement wrapped around an instruction to override the
        # reviewer's correct conclusion — and it quietly undid the
        # paths-never-commands decision `suite_evidence` is built on.
        lines += [
            "**No command could be resolved from this repo's `CLAUDE.md` — but "
            "this repo DOES carry tests.** Do not report that it has no suite. "
            "Find the command from the evidence below and run it; if you end up "
            "executing nothing, say so explicitly and say why.",
        ]
    elif workflows:
        lines += [
            "**No command in `CLAUDE.md`, and no test file matched — but this "
            "repo has CI.** Read the workflow(s) below before concluding "
            "anything: they may run a suite this tool's filename conventions "
            "do not recognise, or they may only deploy. Say which you found.",
        ]
    else:
        return NO_SUITE_TEXT

    if workflows or tests:
        header = ("Other verification this repo carries — what is above may not be "
                  "all of it:" if command else "Evidence tracked in git:")
        lines += ["", header]
        for path in workflows[:EVIDENCE_SAMPLE]:
            lines.append("  - %s" % path)
        if len(workflows) > EVIDENCE_SAMPLE:
            lines.append("  - ... and %d more workflow(s)" % (len(workflows) - EVIDENCE_SAMPLE))
        if tests:
            # Grouped by directory, NOT the first N filenames. The flat sample
            # was alphabetical, so in a repo with one large suite all eight
            # slots went to that suite's directory and the repo's OTHER
            # suites were unreachable from the brief. The count said
            # "20 test file(s)", so a reviewer knew more existed and
            # could not learn WHICH: a quieter version of the same fail-open
            # this section exists to close. Locations are what a reviewer
            # actually needs, and there are far fewer of them than files.
            locations = {}
            for path in tests:
                parent = os.path.dirname(path) or "."
                locations[parent] = locations.get(parent, 0) + 1
            shown = sorted(locations)[:EVIDENCE_SAMPLE]
            lines.append("  - %d test file(s) across %d location(s):"
                         % (len(tests), len(locations)))
            for parent in shown:
                lines.append("      %s  (%d file%s)"
                             % (parent, locations[parent],
                                "" if locations[parent] == 1 else "s"))
            if len(locations) > EVIDENCE_SAMPLE:
                lines.append("      ... and %d more location(s)"
                             % (len(locations) - EVIDENCE_SAMPLE))
        lines += ["", "Report WHICH of these you executed, not just that you ran "
                      "something."]
    return "\n".join(lines)


def _render_diff_section(repo: str, left: str, right: str, diff_text: str) -> str:
    if len(diff_text.encode("utf-8")) > MAX_DIFF_BYTES:
        names = diff_name_only(repo, left, right)
        return (
            "## Diff\n\n"
            "This diff exceeds %d bytes and is NOT included here. The reviewer "
            "MUST `Read` these files directly rather than trust a summary — a "
            "silently truncated diff reports clean on the third it never saw.\n\n"
            "Changed files:\n```\n%s\n```\n\n"
            "Commit range: `%s..%s`"
            % (MAX_DIFF_BYTES, names, left, right))
    return "## Diff\n\n```diff\n%s\n```" % diff_text


def _render_header(repo: str, mode: str, target: str, base: str,
                    base_sha: Optional[str], merge_sha: Optional[str],
                    freshness: str, since: Optional[str],
                    checkout_ref: Optional[str] = None) -> str:
    lines = [
        "# Merge-context review brief",
        "",
        "This file is the reviewer's ONLY framing. Every section below is built "
        "from `git` or `gh` output — there is no way for a plan, spec, or "
        "architect's own prose to enter it.",
        "",
        "- repo: %s" % repo,
        "- mode: %s" % mode,
        "- target: %s" % target,
        "- base: %s (%s)" % (base, base_sha or "?"),
        "- merge SHA: %s" % (merge_sha or "?"),
        "- freshness: %s" % freshness,
    ]
    if since:
        lines.append(
            "- SCOPED TO A FIX ROUND: the changed-files and diff sections below "
            "are restricted to changes since %s. This is the `jurisdiction` "
            "half of the STOP rule — out-of-jurisdiction findings re-file as "
            "new work rather than looping this review." % since)
    if merge_sha:
        # The left endpoint for the NEXT round, stated here because getting it
        # wrong silently un-scopes the review rather than failing. Read as "the
        # last commit I reviewed", a reader reaches for the branch commit — and
        # that range re-includes everything the previous round's merge already
        # pulled in from upstream, so the "scoped" brief comes back nearly the
        # size of the unscoped one.
        lines += [
            "",
            "Next fix round: pass `--since %s` — the MERGE sha above, not a "
            "branch commit." % merge_sha,
        ]
    if checkout_ref and merge_sha:
        # Without this the merge is reachable from nothing and the reviewer
        # silently runs the suite against the BRANCH — which is the one thing
        # a merge-context gate must not do, and it reports `ran: true` while
        # doing it.
        #
        # Two properties of these three lines are load-bearing, and the first
        # draft got both wrong in the same way — by being GLOBAL where the ref
        # beside it is per-merge:
        #
        # 1. **Check out the SHA, not the ref.** A ref moves: re-run this tool on
        #    the same branch and `refs/merge-brief/<branch>` points somewhere
        #    else, while an already-written brief still names the old merge. A
        #    reviewer holding that brief would check out the ref and review a
        #    different tree than the diff describes — trap 2 from this module's
        #    own docstring, reproduced in the mode that does not check for it.
        #    The SHA is immutable; the ref exists only to keep it reachable.
        # 2. **The path must be per-merge, and inside the repo.** A hardcoded
        #    `/tmp/g5-merge` collides exactly as a shared ref would: session B's
        #    `worktree add` fails `already exists`, and an unchained `cd` on the
        #    next line then lands B in A's tree, where it runs the suite and
        #    truthfully reports `ran: true` about the wrong merge. B's cleanup
        #    then deletes A's checkout mid-run. Observed: a stalled reviewer left
        #    `/tmp/g5-merge` behind and the next one collided with it on its
        #    first command. It also has to sit under `<repo>/.claude/worktrees/`
        #    rather than `/tmp`, so the path has the same shape on macOS and
        #    Windows — the same reason `merge-review.md` gives for scratch in
        #    general.
        # `&&` for the same reason — an unchained `cd` after a failed `add`
        # succeeds into whatever is already there.
        #
        # Forward slashes, not `os.sep`: this string is printed into `git -C`
        # and `cd` instructions, and both take forward slashes on Windows (Git
        # Bash), so the printed command reads the same regardless of which OS
        # built the brief.
        repo_fwd = repo.replace(os.sep, "/")
        path = "%s/.claude/worktrees/g5-merge-%s" % (repo_fwd, merge_sha[:12])
        lines += [
            "",
            "## RUN THE SUITE AGAINST THE MERGE, NOT THE BRANCH",
            "",
            "The repository checkout above is on the *branch*. The merged tree is "
            "a different tree, and it is the one under review — a branch whose "
            "suite is green and a merge whose suite is not is exactly the defect "
            "class this review exists to catch. Check the merge out first:",
            "",
            "```bash",
            "git -C %s worktree add --detach %s %s \\" % (repo_fwd, path, merge_sha),
            "  && cd %s     # run the suite HERE" % path,
            "```",
            "",
            "That path is derived from the merge commit, so it cannot collide with "
            "a review of a *different* merge. It is not a guarantee of uniqueness, "
            "because a brief is not always built over a real merge: if the base is "
            "already an ancestor the sha is the branch head, and if the branch is "
            "merely behind then the merge fast-forwards and the sha is the base. So "
            "two reviews of an unchanged branch — or of two branches that both "
            "fast-forward to the same base — do land on one path. The *collision* "
            "then fails loudly: the `worktree add` exits `already exists` and the "
            "`&&` stops you there instead of dropping you into the other session's "
            "tree. What is NOT loud is the review surface in that case — where "
            "nothing merged, this brief shows `(no changes)` and an empty diff "
            "while exiting 0, which reads exactly like a clean review. Check the "
            "changed-files section before concluding anything from it. Clean up "
            "with `git -C %s worktree remove --force %s` when you are done. If you "
            "report having executed anything, say which tree you executed it in."
            % (repo_fwd, path),
        ]
    return "\n".join(lines)


def _assemble(repo: str, header: str, stat_diff_left: str, endpoint: str,
              log_left: str, suite: str) -> str:
    stat = diff_stat(repo, stat_diff_left, endpoint)
    commits = commit_messages(repo, log_left, endpoint)
    diff_text = full_diff(repo, stat_diff_left, endpoint)
    diff_section = _render_diff_section(repo, stat_diff_left, endpoint, diff_text)
    sections = [
        header,
        "## Changed files\n\n```\n%s\n```" % (stat if stat.strip() else "(no changes)"),
        "## Commit messages\n\n```\n%s\n```" % (commits if commits.strip() else "(no commits)"),
        diff_section,
        "## Suite\n\n%s" % suite,
    ]
    return "\n\n".join(sections)


# --------------------------------------------------------------------------
# Top-level entry points
# --------------------------------------------------------------------------

def build_pr_brief(repo: str, pr_number: int, since: Optional[str] = None) -> str:
    _ensure_repo(repo)
    base, mergeable = pr_base_and_mergeable(repo, pr_number)
    fetch_pr_merge_ref(repo, pr_number, base)
    parent_sha, base_sha = check_freshness(repo, pr_number, base, mergeable)
    endpoint = "origin/pr-%d-merge" % pr_number
    merge_sha = _git_output(repo, "rev-parse", endpoint)

    stat_diff_left = since if since else "origin/%s" % base
    log_left = "origin/%s" % base
    freshness = ("FRESH — %s^1 (%s) matches origin/%s (%s); gh mergeable=%s"
                 % (endpoint, parent_sha, base, base_sha, mergeable))
    # PR mode needs no parked ref: `origin/pr-N-merge` is already a durable
    # local ref the fetch just wrote, so the reviewer can check it out by name.
    header = _render_header(repo, "pr", "PR #%d" % pr_number, base, base_sha, merge_sha,
                             freshness, since, checkout_ref=endpoint)
    suite = render_suite_section(repo)
    return _assemble(repo, header, stat_diff_left, endpoint, log_left, suite)


def build_pre_pr_brief(repo: str, base: Optional[str] = None,
                        since: Optional[str] = None) -> str:
    _ensure_repo(repo)
    # Explicit `timeout=`, not merge-brief's own module default alone:
    # `freshness_lib.GIT_TIMEOUT_SECONDS` was tightened 5 -> 2 for that
    # module's report-loop budget and must never silently narrow the timeout
    # THIS tool builds its blinded review briefs against. See
    # `freshness_lib.default_branch`'s docstring.
    resolved_base = base if base else default_branch(repo, timeout=GIT_TIMEOUT_SECONDS)
    start_sha, base_sha, merge_sha = build_pre_pr_merge(repo, resolved_base)
    base_ref, local = pre_pr_base_ref(repo, resolved_base)

    stat_diff_left = since if since else base_ref
    log_left = base_ref
    freshness = ("N/A — this merge was built directly by this tool, just now; "
                 "GitHub's lazy computation of a merge ref does not apply here")
    if local:
        freshness += ("; the base is the LOCAL branch %s, because this repo has no "
                      "`origin` remote" % resolved_base)
    header = _render_header(repo, "pre-pr", "base %s" % resolved_base,
                             "%s (local branch; no origin remote)" % resolved_base
                             if local else resolved_base,
                             base_sha, merge_sha, freshness, since,
                             checkout_ref=merge_ref_name(repo))
    suite = render_suite_section(repo)
    return _assemble(repo, header, stat_diff_left, merge_sha, log_left, suite)


def _read_plan_requirements() -> str:
    """The fixed Plan Review rubric, read verbatim from `PLAN_REQUIREMENTS_PATH`.

    Missing or empty is a `BriefError` (precondition, exit 2) — not a
    silently shorter brief. `tools/build-lens/plan-review.md` defines a
    brief as "requirements plus the plan," and a brief carrying the plan
    alone would let the lenses judge it clean against requirements they were
    never given, silently.
    """
    try:
        with open(PLAN_REQUIREMENTS_PATH, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise BriefError(
            "plan-requirements.md is missing or unreadable at %s (%s) — a "
            "plan brief with no requirements block is not a Plan Review "
            "brief" % (PLAN_REQUIREMENTS_PATH, exc))
    if not text.strip():
        raise BriefError(
            "plan-requirements.md at %s is empty — a plan brief with no "
            "requirements block is not a Plan Review brief" % PLAN_REQUIREMENTS_PATH)
    return text


_CLASSIFICATION_LINE_RE = re.compile(r"^Classification: (\S+)\s*$")
_DESIGN_LINE_RE = re.compile(r"^Design: (.+?)\s*$")
_APPROVED_LINE_RE = re.compile(r"^Approved: \d{4}-\d{2}-\d{2}\s*$")
_REQUIREMENTS_LINE_RE = re.compile(r"^Requirements: (.+?)\s*$")


def validate_plan_header(plan_path: str, plan_text: str) -> Optional[str]:
    """Refuse a plan with no classification, or an `architectural` one with
    no approved design or no source requirements, raising `PlanFileError`.

    Scans `plan_text` line by line, **skipping fenced code blocks**: a line
    whose `lstrip()` starts with three backticks or three tildes toggles
    "inside a fence", and a line inside one is never matched — a plan that
    merely SHOWS a `Classification:` line as an example inside a fence must
    not satisfy this check.

    A classification line is a whole line matching
    `^Classification: (\\S+)\\s*$` (no leading whitespace); a design line is
    a whole line matching `^Design: (.+?)\\s*$`, whose value has one
    surrounding pair of backticks stripped if present.

    `bounded` needs nothing further. `architectural` needs exactly one
    design value (repeats of the identical value are fine, two DIFFERENT
    values are a conflict) naming a design file — resolved against the
    plan's own directory when relative, never `os.getcwd()` — whose front
    matter (between two `---` lines, the first being the file's own first
    line) carries an `Approved: YYYY-MM-DD` line. The design file is read
    only to check that; nothing from it is embedded in the brief.

    A requirements line is a whole line matching `^Requirements: (.+?)\\s*$`,
    scanned in the same loop with the same fence skipping, one surrounding
    pair of backticks stripped, normalised before deduplication. Two
    DIFFERENT values are a conflict. `architectural` also needs one (checked
    AFTER the design checks). Returns the requirements path, resolved the
    way the design's is (absolute as-is, else against the plan's own
    directory, never `os.getcwd()`), or `None` when the plan has no such line
    (only `bounded` may). The file is NOT opened here: the caller embeds it.
    """
    classifications = []
    design_values = []
    requirements_values = []
    inside_fence = False
    for line in plan_text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            inside_fence = not inside_fence
            continue
        if inside_fence:
            continue
        match = _CLASSIFICATION_LINE_RE.match(line)
        if match:
            classifications.append(match.group(1))
            continue
        match = _DESIGN_LINE_RE.match(line)
        if match:
            # Normalise BEFORE deduplication: `design.md` and `` `design.md` ``
            # name one design, not two.
            value = match.group(1)
            if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
                value = value[1:-1]
            design_values.append(value)
            continue
        match = _REQUIREMENTS_LINE_RE.match(line)
        if match:
            value = match.group(1)
            if len(value) >= 2 and value.startswith("`") and value.endswith("`"):
                value = value[1:-1]
            requirements_values.append(value)

    if not classifications:
        raise PlanFileError(
            "no `Classification:` line in --plan file %r" % plan_path)
    distinct_classifications = sorted(set(classifications))
    if len(distinct_classifications) > 1:
        raise PlanFileError(
            "conflicting `Classification:` lines in --plan file %r: %s"
            % (plan_path, distinct_classifications))
    classification = distinct_classifications[0]
    if classification not in ("bounded", "architectural"):
        raise PlanFileError(
            "unrecognised classification %r in --plan file %r"
            % (classification, plan_path))
    distinct_requirements = sorted(set(requirements_values))
    if len(distinct_requirements) > 1:
        raise PlanFileError(
            "conflicting `Requirements:` lines in --plan file %r: %s"
            % (plan_path, distinct_requirements))
    requirements_path = None
    if distinct_requirements:
        requirements_value = distinct_requirements[0]
        if os.path.isabs(requirements_value):
            requirements_path = requirements_value
        else:
            requirements_path = os.path.join(
                os.path.dirname(os.path.realpath(plan_path)),
                requirements_value)
    if classification == "bounded":
        return requirements_path

    if not design_values:
        raise PlanFileError(
            "no `Design:` line in --plan file %r — architectural work needs "
            "an approved design" % plan_path)
    distinct_designs = sorted(set(design_values))
    if len(distinct_designs) > 1:
        raise PlanFileError(
            "conflicting `Design:` lines in --plan file %r: %s"
            % (plan_path, distinct_designs))
    design_value = distinct_designs[0]

    if os.path.isabs(design_value):
        design_path = design_value
    else:
        design_path = os.path.join(
            os.path.dirname(os.path.realpath(plan_path)), design_value)

    try:
        with open(design_path, encoding="utf-8") as handle:
            design_text = handle.read()
    # UnicodeDecodeError is a ValueError, not an OSError: a non-UTF-8 design
    # must refuse the same way an unreadable one does, never a traceback.
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanFileError(
            "design file %r could not be read: %s" % (design_path, exc))

    design_lines = design_text.splitlines()
    approved = False
    if design_lines and design_lines[0] == "---":
        # An `Approved:` line counts only once the block is seen to CLOSE: an
        # unterminated `---` is not front matter, so nothing below it may
        # approve the design.
        seen_approved = False
        for line in design_lines[1:]:
            if line == "---":
                approved = seen_approved
                break
            if _APPROVED_LINE_RE.match(line):
                seen_approved = True
    if not approved:
        raise PlanFileError(
            "no `Approved:` line in design file %r" % design_path)

    if requirements_path is None:
        raise PlanFileError(
            "no `Requirements:` line in --plan file %r — architectural work "
            "needs the source requirements it answers" % plan_path)
    return requirements_path


def build_plan_brief(repo: str, plan_path: str) -> str:
    """Build a Plan Review brief: the fixed requirements block, THEN the
    plan file's own text, verbatim — never the plan alone.

    A plan has no merge ref, no diff, no commit log, and no base to check
    freshness against, so this builder never calls `_assemble` — that
    function is the PR/pre-PR choke point (`build_pr_brief`,
    `build_pre_pr_brief`) and every one of its sections would be a lie here.
    The `## Suite` section IS retained, deliberately: a plan reviewer still
    needs to know how this repo verifies its own work, same as every other
    mode.

    `plan_path` is opened RAW as caller-supplied input — the one thing
    `--plan` was carved out to allow, and the only reason this function
    (unlike every other section builder in this module) reads a path handed
    in by an argument rather than one it derives itself from `git`/`gh`.
    Missing, unreadable, or empty raises `PlanFileError`, not `BriefError`:
    a bad `--plan` path is the caller's usage mistake, exit 3, the same
    class as an unrecognized flag — never a repo precondition.

    Immediately after that check, `validate_plan_header` runs against the
    plan's own text: no `Classification:` line, or an `architectural` one
    with no approved design, is refused the same way (`PlanFileError`, exit
    3) — the design file is only CHECKED for an `Approved:` line, never
    embedded in the brief.

    When the plan carries a `Requirements:` line, `validate_plan_header`
    returns the resolved path and that file IS embedded verbatim as a
    `## Source requirements` section between the rubric and the plan, with
    a `- requirements file:` header line. Unreadable or whitespace-only is
    a `PlanFileError` (exit 3). With no such line the brief is unchanged.
    """
    _ensure_repo(repo)
    requirements = _read_plan_requirements()

    try:
        with open(plan_path, encoding="utf-8") as handle:
            plan_text = handle.read()
    # UnicodeDecodeError is a ValueError, not an OSError: a non-UTF-8 plan is
    # the caller's unreadable file, exit 3, never a traceback.
    except (OSError, UnicodeDecodeError) as exc:
        raise PlanFileError("could not read --plan file %r: %s" % (plan_path, exc))
    if not plan_text.strip():
        raise PlanFileError("--plan file %r is empty" % plan_path)
    requirements_path = validate_plan_header(plan_path, plan_text)

    source_requirements = None
    if requirements_path is not None:
        try:
            with open(requirements_path, encoding="utf-8") as handle:
                source_requirements = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise PlanFileError(
                "could not read `Requirements:` file %r: %s"
                % (requirements_path, exc))
        if not source_requirements.strip():
            raise PlanFileError(
                "`Requirements:` file %r is empty" % requirements_path)

    framing = (
        "This file is the reviewer's ONLY framing. The requirements below "
        "are a fixed file this repo ships, identical every run; the plan "
        "below is read verbatim from the file named here — there is no "
        "diff, no commit log, and no freshness check, because a plan has "
        "none of those.")
    header_lines = ["# Merge-context review brief", "", framing]
    if source_requirements is not None:
        header_lines[2] = framing + (
            " The source requirements below are read verbatim from the "
            "file the plan's `Requirements:` line names: the acceptance "
            "contract the plan answers to.")
    header_lines += [
        "",
        "- repo: %s" % repo,
        "- mode: plan",
        "- plan file: %s" % plan_path,
    ]
    if source_requirements is not None:
        header_lines.append("- requirements file: %s" % requirements_path)
    header = "\n".join(header_lines)
    suite = render_suite_section(repo)
    sections = [
        header,
        "## Requirements\n\n%s" % requirements.strip(),
    ]
    if source_requirements is not None:
        # Verbatim, like the plan below: `.strip()` only decided emptiness.
        sections.append("## Source requirements\n\n%s" % source_requirements)
    sections += [
        # Embedded VERBATIM, not stripped: `plan_text.strip()` above only
        # decides whether the file is empty, not what gets embedded. A plan
        # opening with an indented code block would otherwise silently lose
        # its leading whitespace.
        "## Plan\n\n%s" % plan_text,
        "## Suite\n\n%s" % suite,
    ]
    return "\n\n".join(sections)


# --------------------------------------------------------------------------
# Provenance scan — shared across every mode, anchored in the executable's
# `main()`, never here
# --------------------------------------------------------------------------
#
# A brief that quotes a prior review's own answer key hands the next
# reviewer the answer before it looks at the diff or plan — the leak this
# scan exists to catch. It runs on the fully-assembled brief string, after
# `build_pr_brief` / `build_pre_pr_brief` / `build_plan_brief` return and
# before `claude-merge-brief`'s `main()` writes or prints it, because that
# is the one call site downstream of all three builders (`_assemble` is
# not: `build_plan_brief` never calls it). See that executable's `main()`
# for the wiring and this module's own docstring for why `--plan` is the
# only mode where a findings PAYLOAD is refused rather than warned.
#
# Both regexes were validated against a 7-case corpus (legitimate repo
# files that must PASS, plus three genuine review answer keys that must
# REFUSE) — do not loosen either without re-running such a corpus.
HEADING = re.compile(r'^#{1,6}\s+.*\bround\s+\d', re.I | re.M)
PAYLOAD = re.compile(
    r'"(severity|verdict)"\s*:\s*'
    r'"(BLOCKING|MAJOR|MINOR|IMPORTANT|CONFIRMED|REFUTED)"'
    r'|"recommendation"\s*:\s*"',
    re.I)

# A clean review report has `"findings": []` and no severity/verdict at all,
# so PAYLOAD alone never sees it -- it sails through as an answer key saying
# "the previous round approved." The `recommendation` arm closes that gap
# without touching the schema, which already carries `recommendation` on
# every report, clean or not -- deliberately VALUE-AGNOSTIC (any quoted
# value, not an enumerated list) because the schema's enum
# (`merge-review-report.schema.json`) has already changed under this arm
# once (APPROVE/REQUEST_CHANGES -> +COMMENT) and enumerating it a second
# time just reopens the same gap for the next value. Safe to leave
# unlocked: `"recommendation"` appears with a `{` (not a quote) in the
# schema itself, and with a quoted value only inside test fixture files --
# which are diffs, never plans, and this arm only refuses under `--plan`.

# A round-N HEADING is not by itself evidence of a leaked answer key: a plan
# section titled `## Round 2 deployment milestones` is ordinary phased-plan
# structure, and refusing it is the "check that cannot pass on correct input"
# failure that gets disabled, then deleted. HEADING_VOCAB narrows REFUSE to a
# round-N heading that ALSO carries findings vocabulary on the SAME line —
# matched against the matched line only, never the whole document, or this
# collapses back to refusing everything.
HEADING_VOCAB = re.compile(r'(findings?|review|adjudicat|verdict|blocking)', re.I)

# WARN-only signals, unlocked (unlike HEADING/PAYLOAD above): a bare
# "BLOCKING" and a bare "round N" phrase that is NOT a heading. The /build
# skill's own text (`skills/builder/build/SKILL.md`) carries them in prose
# with no HEADING or PAYLOAD match — the positive control this scan must
# never refuse.
_BLOCKING_WORD = re.compile(r'\bBLOCKING\b')
_ROUND_PHRASE = re.compile(r'\bround\s+\d', re.I)


class ProvenanceLeakError(BriefError):
    """The brief would hand the reviewer a prior review's own answer key.

    A `BriefError`, not a `PlanFileError`: this is a property of the
    ASSEMBLED BRIEF, not of the caller's `--plan` argument, so it stays at
    exit 2 in every mode, including `--pr` and `--pre-pr`, which have no
    `--plan` argument to blame it on.
    """


def scan_for_leaked_provenance(brief: str, mode: str) -> List[str]:
    """Scan a fully-built brief for review-answer-key leakage.

    Two levels, decided per line:

    - REFUSE (raises `ProvenanceLeakError`, exit 2, no brief written):
      `HEADING` matches AND `HEADING_VOCAB` also matches that SAME line, in
      EVERY mode — a `## Round 2 findings` heading is never legitimate in
      any brief. `PAYLOAD` matches AND `mode == "plan"` — a plan should
      never contain a findings payload, so this is exactly the answer-key
      leak the scan exists to stop (including a plan quoting its own prior
      review findings).
    - WARN (returned, one string per hit, each carrying its 1-based line
      number; the brief is still written, exit 0): `HEADING` matches but
      `HEADING_VOCAB` does NOT match that line — an ordinary round-N plan
      heading with no findings vocabulary (`## Round 2 deployment
      milestones`, `# Usage report round 2 (T15-T17) — Implementation
      Plan`). `PAYLOAD` matches under `--pr` / `--pre-pr` — a diff
      legitimately touches this repo's own findings-shaped fixtures
      (`merge-review-report.schema.json`, `test_codex_review.py`), so
      refusing there is the "check that cannot reach PASS on a correct
      repo" failure that gets disabled, not fixed. Prose `BLOCKING` and
      prose `round N` (not a heading), in every mode.

    A count threshold was considered and rejected: density separates the
    corpus but is a magic number tuned to five files, and fails silently on
    a real 2-finding review. Mode is a property of the invocation, not of
    a sample.
    """
    warnings: List[str] = []
    for lineno, line in enumerate(brief.splitlines(), start=1):
        if HEADING.search(line):
            if HEADING_VOCAB.search(line):
                raise ProvenanceLeakError(
                    "line %d looks like a review-answer-key heading (%r) — "
                    "this brief was not written. HEADING refuses in every "
                    "mode when it also carries findings vocabulary; strip "
                    "the heading and rerun."
                    % (lineno, line.strip()))
            warnings.append(
                "line %d: round-N heading with no findings vocabulary (%r) "
                "— warn-only (ordinary phased-plan structure, not a leaked "
                "review heading)" % (lineno, line.strip()))
            continue
        payload_match = PAYLOAD.search(line)
        if payload_match:
            if mode == "plan":
                raise ProvenanceLeakError(
                    "line %d carries a findings payload (%r) — this brief "
                    "was not written. A plan should never contain one; "
                    "PAYLOAD refuses under --plan specifically."
                    % (lineno, payload_match.group(0)))
            warnings.append(
                "line %d: findings payload %r — warn-only under %r mode "
                "(a diff may legitimately carry this repo's own "
                "findings-shaped fixtures)" % (lineno, payload_match.group(0), mode))
            continue
        if _BLOCKING_WORD.search(line):
            warnings.append("line %d: prose 'BLOCKING'" % lineno)
        if _ROUND_PHRASE.search(line):
            warnings.append("line %d: prose 'round N'" % lineno)
    return warnings
