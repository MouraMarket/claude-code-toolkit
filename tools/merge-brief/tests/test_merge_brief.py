#!/usr/bin/env python3
"""Tests for claude-merge-brief.

Every scenario here is built from real, throwaway git repos in `tempfile` —
never a real remote — because the whole point of this tool is that its three
traps (missing `+`, a lazily-stale merge ref, a bare local branch name in a
diff range) are measured git behaviour, not opinions this suite can fake.

PR mode's git mechanics (the fetch refspec, the freshness check) are tested
directly against a hand-built bare "origin" that plays GitHub — nothing here
calls the real `gh` CLI against a real GitHub PR, which would violate the
hermeticity this repo requires. `pr_base_and_mergeable` is the only function
that shells out to `gh`, and it is thin enough that it is exercised only by
inspection, never against a live API.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
# .../tools/merge-brief -> .../tools -> the worktree root, so a positive
# control can scan this repo's OWN /build skill through --plan.
REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "lib", "tests"))

import merge_brief_lib as lib  # noqa: E402

CLI = os.path.join(TOOL_DIR, "claude-merge-brief")


def _load_cli_module():
    """Import claude-merge-brief as a module, the way TestArgumentSurface
    does for `build_parser()`, so `main()` can be called IN-PROCESS.

    Needed for a --pr provenance-scan fixture: `pr_base_and_mergeable`
    shells out to `gh`, which this suite never calls for real (see
    the module docstring), so it is mocked on `merge_brief_lib`
    itself. That mock only reaches `main()`'s calls if `main()` runs in
    this process — `merge_brief_lib` is already in `sys.modules` by the
    time the loaded module's own `import merge_brief_lib as lib` runs, so
    Python binds it to the SAME module object this test file imported,
    and `mock.patch.object(lib, ...)` here is visible to it.
    """
    loader = importlib.machinery.SourceFileLoader("_cli_provenance", CLI)
    spec = importlib.util.spec_from_loader("_cli_provenance", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _call_cli_main(cli_module, argv):
    """Call `main()` in-process and capture what it would have printed,
    without going through `subprocess` — the seam `_load_cli_module` above
    exists for."""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = cli_module.main(argv)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def _git_available():
    return shutil.which("git") is not None


def git(*args, cwd):
    return subprocess.run(("git",) + args, cwd=cwd, check=True,
                          capture_output=True, text=True)


def git_out(*args, cwd):
    return git(*args, cwd=cwd).stdout.strip()


def sha_of(ref, cwd):
    return git_out("rev-parse", ref, cwd=cwd)


def run_cli(args, cwd=None):
    return subprocess.run([sys.executable, CLI] + list(args), cwd=cwd,
                          capture_output=True, text=True)


class GitRepoTestCase(unittest.TestCase):
    """A bare 'remote' plus a real clone, standing in for a GitHub origin.

    Never a real network remote — `git init --bare` in a tempdir plays that
    role entirely locally.
    """

    def setUp(self):
        if not _git_available():
            self.skipTest("git not available")
        self.tmp = tempfile.mkdtemp(prefix="merge-brief-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = os.path.join(self.tmp, "remote.git")
        self.work = os.path.join(self.tmp, "work")
        # -b main explicitly: relying on ambient init.defaultBranch makes this
        # suite pass only on a machine this toolkit already configured.
        git("init", "-q", "--bare", "-b", "main", self.remote, cwd=self.tmp)
        git("clone", "-q", self.remote, self.work, cwd=self.tmp)
        git("config", "user.email", "t@example.com", cwd=self.work)
        git("config", "user.name", "Tester", cwd=self.work)

    def commit(self, filename, content, cwd=None, message=None):
        cwd = cwd or self.work
        with open(os.path.join(cwd, filename), "w", encoding="utf-8") as handle:
            handle.write(content)
        git("add", filename, cwd=cwd)
        git("commit", "-q", "-m", message or ("add %s" % filename), cwd=cwd)
        return sha_of("HEAD", cwd=cwd)

    def push_to_ref(self, sha, ref, cwd=None):
        """Land a commit object on the bare remote at an arbitrary ref name,
        the way GitHub lands PR commits at refs/pull/<N>/{head,merge}.

        Pushed to a scratch branch first (objects must exist on the remote
        before `update-ref` can point anything at them), then the scratch
        branch is deleted — `refs/pull/<N>/merge` itself is never a real
        branch and must not look like one afterward.
        """
        cwd = cwd or self.work
        git("push", "-q", "origin", "%s:refs/tmp/scratch" % sha, cwd=cwd)
        git("update-ref", ref, sha, cwd=self.remote)
        git("push", "-q", "origin", ":refs/tmp/scratch", cwd=cwd)

    def land_upstream_commit(self, filename, content, message="peer work"):
        """Push a commit to `origin/main` from a second clone — a peer's PR
        landing while this branch is open.

        The clone directory is keyed on `filename` so this can be called more
        than once in one test; a fixed directory name silently fails the second
        time.
        """
        other = os.path.join(self.tmp, "other-%s" % filename)
        git("clone", "-q", self.remote, other, cwd=self.tmp)
        git("config", "user.email", "o@example.com", cwd=other)
        git("config", "user.name", "Other", cwd=other)
        self.commit(filename, content, cwd=other, message=message)
        git("push", "-q", "origin", "main", cwd=other)

    def feature_branch_with_upstream_movement(self):
        """A feature branch PLUS an upstream commit it does not carry, so that
        `git merge origin/main` produces a real merge commit.

        Any test whose subject is the merge must go through this. Without the
        upstream half there is nothing to merge: `git merge` reports "Already up
        to date", the merge sha *is* the branch head, and anything keyed on the
        merge becomes indistinguishable from the same thing keyed on the branch.
        A test can then pass against branch-scoped behaviour while its name
        claims it proves merge-scoped behaviour — which is exactly what
        `TestCheckoutPathIsPerMerge` did until a review caught it.
        """
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("mine.txt", "branch work\n", message="branch work")
        self.land_upstream_commit("upstream.txt", "landed meanwhile\n")


class TestFetchRefetchAfterNonFastForward(GitRepoTestCase):
    """Trap 1: the leading `+` on the merge-ref refspec.

    GitHub recomputes `refs/pull/<N>/merge` on every push to the PR, and the
    new computation is not a descendant of the old one — a sibling commit,
    not a fast-forward. Refetching it must succeed and move the local ref;
    this is exactly what a `+` refspec buys.
    """

    def test_second_fetch_after_remote_moves_non_fast_forward(self):
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        merge_a = self.commit("file.txt", "x\n", message="mergeA")
        self.push_to_ref(merge_a, "refs/pull/1/merge")

        lib.fetch_pr_merge_ref(self.work, 1, "main")
        self.assertEqual(sha_of("origin/pr-1-merge", cwd=self.work), merge_a)

        # A sibling of merge_a (same parent, different content) — moving the
        # remote ref to this is a non-fast-forward change from the caller's
        # point of view, exactly like GitHub recomputing the merge ref.
        git("reset", "-q", "--hard", "HEAD~1", cwd=self.work)
        merge_b = self.commit("file2.txt", "y\n", message="mergeB")
        self.push_to_ref(merge_b, "refs/pull/1/merge")

        lib.fetch_pr_merge_ref(self.work, 1, "main")
        self.assertEqual(sha_of("origin/pr-1-merge", cwd=self.work), merge_b,
                         "the local merge ref did not move to the recomputed "
                         "merge — a re-review would read stale code")


class TestFreshnessCheck(GitRepoTestCase):
    """Trap 2: `+` is necessary but not sufficient. A `+` fetch of a stale
    merge ref still exits 0, so freshness is a SEPARATE check: the merge
    ref's first parent must equal the base, and `mergeable: null` is stale
    too, not "fine because we can't tell."
    """

    def test_fresh_merge_ref_passes(self):
        base_sha = self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        merge_sha = self.commit("file.txt", "x\n", message="mergeA")
        self.push_to_ref(merge_sha, "refs/pull/1/merge")
        lib.fetch_pr_merge_ref(self.work, 1, "main")

        parent, base = lib.check_freshness(self.work, 1, "main", mergeable=True)
        self.assertEqual(parent, base_sha)
        self.assertEqual(base, base_sha)

    def test_stale_merge_ref_raises_with_both_shas(self):
        old_base = self.commit("base.txt", "v1\n")
        new_base = self.commit("base.txt", "v2\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        # Built off the OLD base tip, not the current one — simulates
        # GitHub's lazily-cached, now-stale merge computation.
        git("checkout", "-q", old_base, cwd=self.work)
        stale_merge = self.commit("file.txt", "x\n", message="stale merge")
        self.push_to_ref(stale_merge, "refs/pull/1/merge")

        lib.fetch_pr_merge_ref(self.work, 1, "main")
        with self.assertRaises(lib.BriefError) as ctx:
            lib.check_freshness(self.work, 1, "main", mergeable=True)
        message = str(ctx.exception)
        self.assertIn(old_base, message)
        self.assertIn(new_base, message)

    def test_mergeable_null_is_treated_as_stale(self):
        base_sha = self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        merge_sha = self.commit("file.txt", "x\n", message="mergeA")
        self.push_to_ref(merge_sha, "refs/pull/1/merge")
        lib.fetch_pr_merge_ref(self.work, 1, "main")

        # ^1 matches the base perfectly here — the ONLY thing wrong is
        # mergeable=None, and that alone must still raise.
        with self.assertRaises(lib.BriefError):
            lib.check_freshness(self.work, 1, "main", mergeable=None)
        self.assertIsNotNone(base_sha)  # the fixture itself is sane


class TestClosedPr(GitRepoTestCase):
    """Trap 4: once a PR closes, `refs/pull/<N>/merge` is gone. Git's own
    message reads like a broken command; this must be replaced with an
    actionable one naming the squash-diff fallback."""

    def test_missing_merge_ref_gives_actionable_message(self):
        self.commit("base.txt", "v1\n")
        with self.assertRaises(lib.BriefError) as ctx:
            lib.fetch_pr_merge_ref(self.work, 999, "main")
        message = str(ctx.exception)
        self.assertIn("closed", message.lower())
        self.assertIn("git diff <squash-commit>^ <squash-commit>", message)
        self.assertNotIn("fatal:", message,
                         "raw git stderr leaked into the user-facing message")


class TestPrePrDiffRangeNeverUsesLocalBranch(GitRepoTestCase):
    """Trap 3: a stale LOCAL base branch must never leak into the diff. The
    local `main` here is deliberately left behind while `origin/main` moves
    — if the diff range were built from bare `main` instead of `origin/main`,
    the upstream-only change would show up as noise in the PR's own diff.
    """

    def test_diff_reflects_origin_base_not_stale_local_base(self):
        self.commit("shared.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("feature.txt", "hello\n")

        # A second clone advances origin/main without ever touching `work`'s
        # local `main` ref — that ref is now genuinely stale.
        other = os.path.join(self.tmp, "other")
        git("clone", "-q", self.remote, other, cwd=self.tmp)
        git("config", "user.email", "o@example.com", cwd=other)
        git("config", "user.name", "Other", cwd=other)
        self.commit("shared.txt", "v2\n", cwd=other, message="upstream edit")
        git("push", "-q", "origin", "main", cwd=other)

        brief = lib.build_pre_pr_brief(self.work)

        self.assertIn("feature.txt", brief)
        self.assertNotIn("shared.txt", brief,
                         "an upstream-only change leaked into the PR's own diff "
                         "— the range was built from a stale local branch")
        # And the fixture's own local ref really is stale, confirming the
        # test is exercising the trap and not a no-op.
        self.assertEqual(git_out("show", "refs/heads/main:shared.txt", cwd=self.work),
                         "v1")


class TestPrePrConflict(GitRepoTestCase):
    """A merge conflict is a legitimate result, not a bug: exit with the
    conflicted paths named, never review a conflicted tree, and never leave
    the scratch worktree behind."""

    def test_conflict_raises_and_names_paths_and_cleans_up(self):
        self.commit("shared.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("shared.txt", "feature-change\n", message="feature edit")

        other = os.path.join(self.tmp, "other")
        git("clone", "-q", self.remote, other, cwd=self.tmp)
        git("config", "user.email", "o@example.com", cwd=other)
        git("config", "user.name", "Other", cwd=other)
        self.commit("shared.txt", "other-change\n", cwd=other, message="conflicting edit")
        git("push", "-q", "origin", "main", cwd=other)

        with self.assertRaises(lib.BriefError) as ctx:
            lib.build_pre_pr_brief(self.work)
        message = str(ctx.exception)
        self.assertIn("does not merge cleanly", message)
        self.assertIn("shared.txt", message)

        worktrees = git_out("worktree", "list", cwd=self.work)
        self.assertEqual(len(worktrees.splitlines()), 1,
                         "a scratch worktree was left behind after a conflict")


class TestPrePrMergeIsReachable(GitRepoTestCase):
    """The merge must outlive the scratch worktree, and the brief must say where.

    This is the defect a blinded review caught on this tool's own first pass, and
    it is worth stating precisely because everything about it looked fine: the
    merge was built correctly, the brief was complete, the scratch worktree was
    cleaned up properly, and the reviewer then ran the suite against the BRANCH
    and reported `ran: true` with a real command. Nothing in the report could
    distinguish that from a merge run.

    It happens whenever the base moves under a branch: a tool removed on
    `main` while the branch was being written leaves every branch suite green
    and one merge suite missing. A branch that is green and a merge that is
    not is the ONE class a merge-context gate exists for.
    """

    def test_merge_survives_at_a_named_ref_after_the_worktree_is_gone(self):
        self.feature_branch_with_upstream_movement()
        brief = lib.build_pre_pr_brief(self.work)

        ref = lib.merge_ref_name(self.work)
        parked = git_out("rev-parse", ref, cwd=self.work)
        self.assertTrue(parked, "the merge was not parked at %s" % ref)

        self.assertIn(parked, brief,
                      "the brief does not name the merge SHA it was built from")
        self.assertIn("worktree add", brief,
                      "the brief never tells the reviewer to check the merge "
                      "out, so they will run the suite on the branch")
        # The checkout instruction must name the immutable SHA, not the ref: the
        # ref moves on the next run of this tool and an already-written brief
        # would then point a reviewer at a different tree than its own diff.
        self.assertRegex(
            brief, r"worktree add --detach \S+ " + parked,
            "the checkout command must name the merge SHA, not a ref that moves")

    def test_the_parked_merge_actually_contains_both_sides(self):
        """The point of checking it out is that it differs from the branch. If
        the parked tree lacked the upstream half, the ref would be reachable and
        still useless."""
        self.feature_branch_with_upstream_movement()
        lib.build_pre_pr_brief(self.work)
        ref = lib.merge_ref_name(self.work)

        listing = git_out("ls-tree", "-r", "--name-only", ref, cwd=self.work).splitlines()
        self.assertIn("mine.txt", listing, "the merge lost the branch's own work")
        self.assertIn("upstream.txt", listing,
                      "the merge lost the upstream work — this is the half the "
                      "branch checkout does not have, and the whole reason to "
                      "review a merge rather than a branch")

    def test_two_branches_do_not_share_one_parked_ref(self):
        """Concurrent sessions in one repo are the normal case here, so a shared
        ref would hand one session the other's merge."""
        self.feature_branch_with_upstream_movement()
        lib.build_pre_pr_brief(self.work)
        feature_ref = lib.merge_ref_name(self.work)

        git("checkout", "-q", "-b", "second", "main", cwd=self.work)
        self.commit("second.txt", "other branch\n", message="second branch work")
        lib.build_pre_pr_brief(self.work)
        second_ref = lib.merge_ref_name(self.work)

        self.assertNotEqual(feature_ref, second_ref)
        self.assertNotEqual(git_out("rev-parse", feature_ref, cwd=self.work),
                            git_out("rev-parse", second_ref, cwd=self.work))

    def test_a_merge_that_cannot_be_parked_is_refused_not_briefed(self):
        """Without the ref nothing reaches the merge, and a brief naming it
        sends the reviewer back to the branch. A real git failure, no mock: a
        ref named `refs/merge-brief` itself leaves no room for
        `refs/merge-brief/<branch>`."""
        self.feature_branch_with_upstream_movement()
        head = git_out("rev-parse", "HEAD", cwd=self.work)
        git("update-ref", "refs/merge-brief", head, cwd=self.work)
        with self.assertRaises(lib.BriefError) as caught:
            lib.build_pre_pr_brief(self.work)
        self.assertIn("could not park it", str(caught.exception))


class TestNextRoundEndpointIsNamed(GitRepoTestCase):
    """The brief must name the SHA to pass as `--since` next round.

    Getting this wrong un-scopes the review SILENTLY rather than failing, which
    is why the brief states it rather than leaving it to be inferred. Read as
    "the last commit I reviewed", a reader reaches for the branch commit — but
    the previous round reviewed a MERGE, and a range starting at the branch
    commit re-includes everything that merge pulled in from upstream.

    With `--since <branch commit>`, the "scoped" brief re-serves what the
    previous round already read and comes back nearly the size of the
    unscoped one. The jurisdiction half of the STOP rule then does nothing,
    and nothing says so.
    """

    def test_brief_names_its_own_merge_sha_as_the_next_since(self):
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("mine.txt", "work\n", message="branch work")

        brief = lib.build_pre_pr_brief(self.work)
        merge_sha = git_out("rev-parse", lib.merge_ref_name(self.work), cwd=self.work)
        self.assertIn(
            "--since %s" % merge_sha, brief,
            "the brief does not tell the next round which SHA to scope from, so "
            "the obvious wrong choice (a branch commit) is the likely one")


class TestDirtyCheckFailsClosedWhenItCannotTell(GitRepoTestCase):
    """`working_tree_dirty` returns None for "clean" and MUST NOT return None
    for "could not tell".

    Its docstring makes that distinction load-bearing, and this repo has a
    standing rule about exactly this shape: a function whose whole design is
    that "no" and "cannot tell" lead to different behaviour erases the
    distinction the moment one collapses into the other. Nothing covered the
    could-not-tell branch, so replacing its `raise` with `return None` left the
    whole suite green — a fail-open reachable with no symptom.
    """

    def test_unreadable_repo_raises_rather_than_reporting_clean(self):
        missing = os.path.join(self.tmp, "not-a-repo-at-all")
        os.makedirs(missing)
        with self.assertRaises(lib.BriefError) as ctx:
            lib.working_tree_dirty(missing)
        self.assertIn("could not read", str(ctx.exception))

    def test_a_clean_tree_still_returns_none(self):
        """The complement, so the fix above cannot be 'raise unconditionally'."""
        self.commit("base.txt", "v1\n")
        self.assertIsNone(lib.working_tree_dirty(self.work))


class TestCheckoutPathIsPerMerge(GitRepoTestCase):
    """The checkout path must be as unique as the ref it checks out.

    `merge_ref_name` is branch-scoped precisely because two sessions reviewing
    one repo is normal here. A hardcoded checkout path reintroduces that exact
    collision one layer out: session B's `worktree add` fails `already exists`,
    an unchained `cd` on the next line lands B inside A's tree, and B then runs
    the suite there and reports `ran: true` — truthfully, about A's merge. B's
    cleanup line deletes A's checkout mid-run.

    Observed rather than imagined: a stalled reviewer left the shared path
    behind and the next reviewer collided with it on its first command.

    **An earlier version of this class proved none of that.** Its body built
    two *branches* with `origin/main` already an ancestor of both, so neither
    call produced a merge at all — `git merge` returned "Already up to date" and
    the parked sha was just the branch head. Two branch heads differ, so the
    assertion passed; a path keyed on the *branch name* also passed it. That
    mutation survived the full suite. The subject is therefore now **one branch
    with two different merges**, which is the only shape that separates the two.
    """

    def test_the_same_branch_with_two_merges_gets_two_paths(self):
        self.feature_branch_with_upstream_movement()
        first = lib.build_pre_pr_brief(self.work)
        head_after_first = sha_of("HEAD", cwd=self.work)

        # Upstream moves again. The branch is untouched — same name, same head —
        # so the ONLY thing that differs between these two briefs is the merge.
        self.land_upstream_commit("upstream2.txt", "landed later\n",
                                  message="second peer work")
        second = lib.build_pre_pr_brief(self.work)

        self.assertEqual(
            sha_of("HEAD", cwd=self.work), head_after_first,
            "the branch head moved between the two briefs, so this test no "
            "longer distinguishes a per-merge path from a per-branch one — "
            "which is exactly how it silently stopped testing its own name")

        paths = [set(re.findall(r"\S+/\.claude/worktrees/g5-merge-\S+", b)) for b in (first, second)]
        self.assertTrue(paths[0] and paths[1], "no checkout path in the brief")
        self.assertFalse(
            paths[0] & paths[1],
            "two different merges of ONE branch share a checkout path (%s) — "
            "the second session lands in the first session's tree"
            % (paths[0] & paths[1]))

        # Names the surviving mutation directly, so the failure message says
        # what went wrong rather than only that two sets intersected.
        branch = lib.merge_ref_name(self.work).rsplit("/", 1)[-1]
        self.assertNotIn(
            "/.claude/worktrees/g5-merge-%s" % branch, first,
            "the checkout path is keyed on the branch name, not on the merge")

        # An unchained `cd` after a failed `add` succeeds into whatever is
        # already there, which is the whole mechanism of the collision.
        self.assertRegex(first, r"&& cd \S+/\.claude/worktrees/g5-merge-",
                         "the cd is not chained to the worktree add")

    def test_two_branches_over_one_base_get_two_paths(self):
        """The other collision shape, and it needs its own test.

        The same-branch test above kills a path keyed on the branch NAME. It
        cannot kill one keyed on the BASE, because it never varies the base —
        a `base_sha`-keyed path passed the whole suite when this case was
        missing (measured right after the same-branch test replaced rather
        than joined it). Two branches sharing one base is the
        only shape that separates a merge-keyed path from a base-keyed one.

        Both branches must produce a REAL merge, which is what the deleted
        version of this test got wrong: with `origin/main` already an ancestor
        of both, neither call merged anything and the two paths differed only
        because the two branch heads did.
        """
        self.feature_branch_with_upstream_movement()
        feature_head = sha_of("HEAD", cwd=self.work)
        first = lib.build_pre_pr_brief(self.work)

        # `main` is still at the original base, so this branch is BEHIND the
        # upstream commit — merging origin/main into it is a real merge too.
        git("checkout", "-q", "-b", "second", "main", cwd=self.work)
        self.commit("other.txt", "other work\n", message="second branch work")
        second_head = sha_of("HEAD", cwd=self.work)
        second = lib.build_pre_pr_brief(self.work)

        # The docstring's precondition, enforced rather than merely asserted in
        # prose. Where nothing merges, the merge sha IS the branch head — this
        # test would then be comparing branch heads again, which is precisely
        # how its predecessor rotted while staying green.
        for label, brief, head in (("feature", first, feature_head),
                                   ("second", second, second_head)):
            found = re.search(r"- merge SHA: (\S+)", brief)
            self.assertTrue(found, "no merge SHA in the %s brief" % label)
            self.assertNotEqual(
                found.group(1), head,
                "the %s brief's merge sha is its own branch head, so nothing "
                "merged and this test no longer varies the merge" % label)

        base = sha_of("origin/main", cwd=self.work)
        self.assertNotIn(
            "/.claude/worktrees/g5-merge-%s" % base[:12], first,
            "the checkout path is keyed on the base, not on the merge — every "
            "branch reviewed against one base would share a checkout path")

        paths = [set(re.findall(r"\S+/\.claude/worktrees/g5-merge-\S+", b)) for b in (first, second)]
        self.assertTrue(paths[0] and paths[1], "no checkout path in the brief")
        self.assertFalse(
            paths[0] & paths[1],
            "two branches over one base share a checkout path (%s)"
            % (paths[0] & paths[1]))


class TestPrePrRefusesADirtyTree(GitRepoTestCase):
    """Uncommitted work is not in the merge, so a brief built over it describes
    a tree that will never exist — while looking complete.

    This fails CLOSED on purpose, matching the freshness check beside it rather
    than the warn-and-continue it replaced. It bites in the normal case: this
    project's executors are dispatched to leave work uncommitted, so the
    architect must commit before Merge Review runs.
    """

    def test_modified_tracked_file_refuses_and_writes_nothing(self):
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        with open(os.path.join(self.work, "base.txt"), "w") as handle:
            handle.write("uncommitted edit\n")

        out = os.path.join(self.tmp, "brief.md")
        result = run_cli(["--pre-pr", "--repo", self.work, "-o", out])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("uncommitted changes", result.stderr)
        self.assertIn("base.txt", result.stderr)
        self.assertFalse(os.path.exists(out),
                         "a brief was written despite the precondition failing")

    def test_untracked_file_also_refuses(self):
        """Untracked counts too: a new file the author has not added is exactly
        as absent from the merge as an unstaged edit, and it is the likelier of
        the two to be the whole change."""
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        with open(os.path.join(self.work, "brand-new.py"), "w") as handle:
            handle.write("# never committed\n")

        result = run_cli(["--pre-pr", "--repo", self.work])
        self.assertEqual(result.returncode, 2)
        self.assertIn("brand-new.py", result.stderr)
        self.assertNotIn("# Merge-context review brief", result.stdout,
                         "a brief reached stdout despite the refusal")


class TestPrePrHappyPath(GitRepoTestCase):
    """`--pre-pr` in a fixture repo writes all five sections and exits 0, and
    leaves no worktree behind."""

    def test_all_five_sections_present_and_exits_zero(self):
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("feature.txt", "hello\n")

        result = run_cli(["--pre-pr", "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)
        brief = result.stdout
        for heading in ("# Merge-context review brief", "## Changed files",
                        "## Commit messages", "## Diff", "## Suite"):
            self.assertIn(heading, brief, "missing section: %s" % heading)
        self.assertIn("- mode: pre-pr", brief)
        self.assertNotIn("PR body", brief)

        worktrees = git_out("worktree", "list", cwd=self.work)
        self.assertEqual(len(worktrees.splitlines()), 1)

    def test_output_flag_writes_to_file(self):
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("feature.txt", "hello\n")

        out_path = os.path.join(self.tmp, "brief.md")
        result = run_cli(["--pre-pr", "--repo", self.work, "-o", out_path])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        with open(out_path, encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("## Diff", content)


class TestPrePrInARepoWithNoOriginRemote(GitRepoTestCase):
    """Row 72 (28b F-E6): `--pre-pr` exited 2 "could not fetch origin/main" in a
    local-only repo, so the economical Code Review could not run at all. With no
    remote named `origin`, the local base branch IS the base. The fallback keys
    on the remote being ABSENT, never on a fetch failing: an `origin` that is
    configured but unreachable must still refuse, or trap 3 (a stale local
    base) comes back silently."""

    def setUp(self):
        super().setUp()
        self.local = os.path.join(self.tmp, "local-only")
        git("init", "-q", "-b", "main", self.local, cwd=self.tmp)
        git("config", "user.email", "t@example.com", cwd=self.local)
        git("config", "user.name", "Tester", cwd=self.local)

    def _branch_with_base_movement(self, base="main"):
        """A feature branch plus a base-only commit, so the merge is a real one."""
        self.commit("base.txt", "v1\n", cwd=self.local)
        if base != "main":
            git("branch", "-q", "-m", "main", base, cwd=self.local)
        git("checkout", "-q", "-b", "feature", cwd=self.local)
        self.commit("mine.txt", "branch work\n", cwd=self.local, message="branch work")
        git("checkout", "-q", base, cwd=self.local)
        self.commit("baseonly.txt", "moved meanwhile\n", cwd=self.local, message="base moved")
        git("checkout", "-q", "feature", cwd=self.local)

    def test_fixture_really_has_no_remote(self):
        self.assertEqual(git_out("remote", cwd=self.local), "")

    def test_reviews_against_the_local_base_branch(self):
        self._branch_with_base_movement()
        result = run_cli(["--pre-pr", "--repo", self.local])
        self.assertEqual(result.returncode, 0, result.stderr)
        brief = result.stdout
        self.assertIn("mine.txt", brief)
        self.assertNotIn("baseonly.txt", brief,
                         "a base-only change leaked into the branch's own diff")
        self.assertIn("local branch; no origin remote", brief)
        # The merge is real and parked, as on the origin path.
        merge_sha = sha_of("refs/merge-brief/feature", cwd=self.local)
        parents = git_out("rev-list", "--parents", "-n", "1", merge_sha, cwd=self.local).split()
        self.assertEqual(len(parents), 3, "expected a two-parent merge commit")
        self.assertIn(sha_of("main", cwd=self.local), parents)
        self.assertIn("- merge SHA: %s" % merge_sha, brief)
        self.assertEqual(len(git_out("worktree", "list", cwd=self.local).splitlines()), 1)

    def test_an_explicit_base_selects_another_local_branch(self):
        self._branch_with_base_movement(base="master")
        result = run_cli(["--pre-pr", "--repo", self.local, "--base", "master"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mine.txt", result.stdout)
        self.assertNotIn("baseonly.txt", result.stdout)

    def test_a_missing_default_branch_refuses_with_a_runnable_fix(self):
        self._branch_with_base_movement(base="master")
        result = run_cli(["--pre-pr", "--repo", self.local])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("no `origin` remote", result.stderr)
        self.assertIn("master", result.stderr)
        self.assertIn("claude-merge-brief --pre-pr --base <branch>", result.stderr)

    def test_a_configured_but_unreachable_origin_still_refuses(self):
        """The anti-fallback property: a configured origin is never swapped for
        the local branch, even when it cannot be fetched."""
        self._branch_with_base_movement()
        git("remote", "add", "origin", os.path.join(self.tmp, "does-not-exist.git"),
            cwd=self.local)
        result = run_cli(["--pre-pr", "--repo", self.local])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("could not fetch origin/main", result.stderr)

    def test_unlistable_remotes_raise_rather_than_pick_local(self):
        real = lib._git_output

        def fake(repo, *args, **kwargs):
            if args == ("remote",):
                return None
            return real(repo, *args, **kwargs)

        with mock.patch.object(lib, "_git_output", side_effect=fake):
            with self.assertRaises(lib.BriefError) as ctx:
                lib.pre_pr_base_ref(self.local, "main")
        self.assertIn("could not list git remotes", str(ctx.exception))

    def test_a_repo_with_origin_still_uses_origin(self):
        self.assertEqual(lib.pre_pr_base_ref(self.work, "main"), ("origin/main", False))
        self.assertEqual(lib.pre_pr_base_ref(self.local, "main"), ("refs/heads/main", True))


class TestPrBriefCarriesNoPrBody(GitRepoTestCase):
    """The `--pr` brief carries no PR body: it is author framing the reviewer
    cannot otherwise reach, and the builder never asks `gh` for it."""

    def test_pr_brief_has_no_body_section_and_never_asks_gh_for_one(self):
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        merge_sha = self.commit("file.txt", "x\n", message="mergeA")
        self.push_to_ref(merge_sha, "refs/pull/1/merge")

        recorded = []

        def fake_gh_run(repo, argv, *args, **kwargs):
            recorded.append(tuple(argv))
            return subprocess.CompletedProcess(argv, 0, stdout="SECRET BODY\n", stderr="")

        cli_module = _load_cli_module()
        with mock.patch.object(lib, "pr_base_and_mergeable",
                                return_value=("main", True)), \
             mock.patch.object(lib, "_gh_run", side_effect=fake_gh_run):
            exit_code, out, err = _call_cli_main(
                cli_module, ["--pr", "1", "--repo", self.work])

        self.assertEqual(exit_code, 0, out + err)
        lines = out.splitlines()
        self.assertFalse([l for l in lines if l.startswith("## PR body")],
                         "a `## PR body` heading is present")
        self.assertNotIn("SECRET BODY", out)
        self.assertFalse([argv for argv in recorded if "body" in argv],
                         "a gh call asked for the PR body: %r" % recorded)
        # Sections located by their own headings, in order, by first index.
        indices = [lines.index(h) if h in lines else None
                   for h in ("## Changed files", "## Commit messages")]
        diff_idx = next((i for i, l in enumerate(lines) if l.startswith("## Diff")), None)
        suite_idx = lines.index("## Suite") if "## Suite" in lines else None
        order = indices + [diff_idx, suite_idx]
        self.assertNotIn(None, order, "a named section is missing: %r" % order)
        self.assertEqual(order, sorted(order))


class TestNoBriefWrittenOnFailure(GitRepoTestCase):
    """Exit 2 (a precondition failure) must never leave a brief behind, on
    stdout OR at -o."""

    def test_conflict_writes_no_file_and_prints_no_brief_to_stdout(self):
        self.commit("shared.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("shared.txt", "feature-change\n", message="feature edit")

        other = os.path.join(self.tmp, "other")
        git("clone", "-q", self.remote, other, cwd=self.tmp)
        git("config", "user.email", "o@example.com", cwd=other)
        git("config", "user.name", "Other", cwd=other)
        self.commit("shared.txt", "other-change\n", cwd=other, message="conflicting edit")
        git("push", "-q", "origin", "main", cwd=other)

        out_path = os.path.join(self.tmp, "brief.md")
        result = run_cli(["--pre-pr", "--repo", self.work, "-o", out_path])
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("# Merge-context review brief", result.stdout)
        self.assertFalse(os.path.exists(out_path),
                         "a brief file was written despite a precondition failure")


class TestSinceScoping(GitRepoTestCase):
    """`--since REF` narrows changed-files and diff, but not commit messages
    — the commit-messages section always shows the whole PR's history."""

    def test_since_narrows_diff_and_stat_but_not_commit_log(self):
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("f1.txt", "one\n", message="commit1")
        since_sha = sha_of("HEAD", cwd=self.work)
        self.commit("f2.txt", "two\n", message="commit2")

        result = run_cli(["--pre-pr", "--repo", self.work, "--since", since_sha])
        self.assertEqual(result.returncode, 0, result.stderr)
        brief = result.stdout

        self.assertIn("SCOPED TO A FIX ROUND", brief)
        self.assertIn(since_sha, brief)
        self.assertIn("f2.txt", brief)
        self.assertNotIn("f1.txt", brief,
                         "--since did not narrow the changed-files/diff sections")
        # Commit messages are NOT scoped — both commits still listed.
        self.assertIn("commit1", brief)
        self.assertIn("commit2", brief)


class TestMaxDiffBytesTruncation(GitRepoTestCase):
    """Past MAX_DIFF_BYTES, the diff is replaced with a file list and an
    explicit instruction to Read the files — never silently truncated."""

    def test_oversized_diff_shows_file_list_not_truncated_content(self):
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        big_content = "x\n" * 200_000  # comfortably over MAX_DIFF_BYTES as a diff
        self.commit("big.txt", big_content, message="a big file")

        original = lib.MAX_DIFF_BYTES
        lib.MAX_DIFF_BYTES = 1000
        try:
            brief = lib.build_pre_pr_brief(self.work)
        finally:
            lib.MAX_DIFF_BYTES = original

        self.assertIn("MUST `Read` these files", brief)
        self.assertIn("big.txt", brief)
        self.assertLess(len(brief.encode("utf-8")), 50_000,
                        "the oversized diff was embedded instead of truncated")


class TestFindSuiteCommand(GitRepoTestCase):
    """Resolved generically from the target repo's own CLAUDE.md — never a
    hardcoded per-repo list."""

    def _write_claude_md(self, text):
        path = os.path.join(self.work, "CLAUDE.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_finds_first_fenced_block_under_a_testing_heading(self):
        self._write_claude_md(
            "# A repo\n\n"
            "## Something else\n\n```\nnot the suite\n```\n\n"
            "## Testing\n\nRun it:\n\n```bash\npytest -q\n```\n\n"
            "## Later section\n\n```\nirrelevant\n```\n")
        self.assertEqual(lib.find_suite_command(self.work), "pytest -q")

    def test_no_claude_md_returns_none(self):
        self.assertIsNone(lib.find_suite_command(self.work))

    def test_heading_with_no_test_section_returns_none(self):
        self._write_claude_md("# A repo\n\n## Usage\n\n```\nsome command\n```\n")
        self.assertIsNone(lib.find_suite_command(self.work))

    def test_fenced_content_is_never_read_as_document_structure(self):
        # A shell comment and an example markdown heading, both inside an
        # UNRELATED fence that appears before the real Testing section, must
        # never be mistaken for real headings.
        self._write_claude_md(
            "# A repo\n\n"
            "## Example\n\n```bash\n#!/usr/bin/env bash\n## Testing\necho hi\n```\n\n"
            "## Testing\n\n```\nmake test\n```\n")
        self.assertEqual(lib.find_suite_command(self.work), "make test")

    def test_an_inline_code_span_is_used_when_the_section_has_no_fence(self):
        """The defect this fallback exists for, in the shape that produced it.

        A `CLAUDE.md` that documents its suite in an inline code span, with
        ZERO fence markers in its `## Testing` section, made the
        fenced-block-only search return None, and the brief rendered
        NO_SUITE_TEXT for a repo with thousands of tests. A blinded reviewer
        was told to hand-falsify instead of running them, and "no suite in
        this repo" is indistinguishable from a repo that genuinely has none —
        a review guard failing OPEN.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Testing\n\n"
            "- TDD for parser logic: failing test -> implement -> pass. Run\n"
            "  `.venv/bin/python -m pytest -q` from the repo root.\n"
            "- Tests never make network calls.\n\n"
            "## Workflow\n\n```\nirrelevant\n```\n")
        self.assertEqual(lib.find_suite_command(self.work),
                         ".venv/bin/python -m pytest -q")

    def test_a_fenced_block_still_wins_over_an_inline_span(self):
        """The fallback must not change any repo that already worked. A fence
        is the documented primary path and stays authoritative even when a
        span appears earlier in the same section."""
        self._write_claude_md(
            "# A repo\n\n"
            "## Testing\n\n"
            "Prose mentioning `some-other-thing --flag` first.\n\n"
            "```bash\npytest -q\n```\n")
        self.assertEqual(lib.find_suite_command(self.work), "pytest -q")

    def test_a_bare_file_path_span_is_not_mistaken_for_a_command(self):
        """The first span in a section is often a PATH, not a command.

        A section that names a test file, such as `tests/test_safe_http.py`,
        beside its command is common. Returning a path would send the reviewer
        to a command that cannot run, so a span with no whitespace that looks
        like a path is skipped rather than returned.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Testing\n\n"
            "One deliberate exception, `tests/test_safe_http.py`, binds two\n"
            "loopback servers. Run `pytest -q` to exercise it.\n")
        self.assertEqual(lib.find_suite_command(self.work), "pytest -q")

    def test_a_bare_word_is_never_taken_as_the_command(self):
        """A span must carry ARGUMENTS. A bare identifier is rejected.

        This asserted the OPPOSITE until a blinded review priced it. The
        first cut accepted any bare word without `/` or `.`, which admits
        `skipTest`, `-x`, `PYTHONPATH` and — the one that matters — `ruff`.
        `ruff` is a real tool that exits 0 and prints nothing alarming, so
        a reviewer handed it lints the repo and reports a green suite.

        The asymmetry decides it: returning nothing is the pre-existing
        failure and is VISIBLE in the brief; returning a wrong command that
        SUCCEEDS is invisible. For a guard that already fails open, ties go
        to returning nothing.
        """
        self._write_claude_md(
            "# A repo\n\n## Testing\n\n"
            "Run `ruff` first, then `python3 -m pytest` for the suite.\n")
        self.assertEqual(lib.find_suite_command(self.work), "python3 -m pytest")

    def test_a_slash_path_with_no_dot_is_rejected(self):
        """Pins the `/` case specifically.

        A blinded review measured that deleting the `/` half of the old
        path check left the whole suite green, because the only path fixture
        (`tests/test_safe_http.py`) carried BOTH a slash and a dot and so
        could not tell the two conditions apart. Extension-less directory
        paths are common in prose (`session-titler/tests/`), so the case
        gets its own fixture rather than riding on a compound one.
        """
        self._write_claude_md(
            "# A repo\n\n## Testing\n\n"
            "Suites live in `bin/run-suite`. Invoke `make check -j4`.\n")
        self.assertEqual(lib.find_suite_command(self.work), "make check -j4")

    def test_a_heading_merely_CONTAINING_test_does_not_hijack_the_search(self):
        """`"test" in title` also matches "Fastest", "Latest" and "Protest".

        Measured by a blinded review: a `## Fastest path` section carrying
        an inline span returned `./deploy.sh --prod` as the suite command.
        The substring match predates the inline fallback — a fenced block in
        such a section was always enough — but spans are far more common
        than fences, so the fallback widens the exposure and it is fixed
        here rather than inherited.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Fastest path\n\nUse `./deploy.sh --prod` to ship.\n\n"
            "## Testing\n\nRun `pytest -q` for the suite.\n")
        self.assertEqual(lib.find_suite_command(self.work), "pytest -q")

    def test_ordinary_test_headings_all_still_match(self):
        """The word-boundary tightening must not narrow the real cases."""
        for title in ("Testing", "Tests", "Running tests", "Test suite",
                      "Unit-testing", "TESTING"):
            with self.subTest(title=title):
                self._write_claude_md(
                    "# A repo\n\n## %s\n\nRun `pytest -q` here.\n" % title)
                self.assertEqual(lib.find_suite_command(self.work), "pytest -q")

    def test_the_FIRST_plausible_span_wins_not_the_last(self):
        """Added because a mutation SURVIVED without it.

        Dropping the `inline_fallback is None` guard makes every later line
        overwrite the candidate, so the LAST plausible span wins instead of
        the first. Every other test here has exactly one plausible span, so
        none of them could tell the two apart and the mutation was invisible.

        First is the correct rule: a Testing section leads with how to run
        the suite and follows with detail, so a later span is commentary —
        `--lf` reruns, a single-file example, a coverage invocation. Handing
        a reviewer the last one runs some narrower thing and reports it as
        the suite.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Testing\n\n"
            "Run `pytest -q` for the whole suite.\n"
            "To rerun just the failures, `pytest --lf -x` is quicker.\n")
        self.assertEqual(lib.find_suite_command(self.work), "pytest -q")

    def test_a_span_outside_the_test_section_is_never_used(self):
        """Scoping is the whole point: a span in an earlier section is not
        this repo's test command, and a test section that documents nothing
        runnable must still return None."""
        self._write_claude_md(
            "# A repo\n\n"
            "## Usage\n\nRun `./app --serve` to start it.\n\n"
            "## Testing\n\nWe do not have a suite yet.\n")
        self.assertIsNone(lib.find_suite_command(self.work))

    def test_an_earlier_fence_does_not_derail_the_span_search(self):
        """Renamed: the old name promised a branch this cannot reach.

        It was `..._a_span_inside_an_unrelated_fence_is_not_used`, which reads
        as coverage of "spans inside fences are skipped". A blinded review
        showed that claim is unfalsifiable here: `in_section and
        inside_any_fence and not capturing` is unreachable, because a fence
        opening inside the test section sets `capturing`, and headings are
        skipped while any fence is open, so `in_section` can never turn on
        mid-fence. Adding the span scan into the `inside_any_fence` branch
        changes no result on a corpus of real docs.

        What this DOES establish, and what it is now named for: an earlier
        unrelated fence — one containing a `## Testing` line and a span —
        leaves the search correctly positioned to find the real section
        afterwards. That kills the mutation deleting `if inside_any_fence:
        continue`, via the heading path.

        Kept rather than deleted, with an honest name, because a test whose
        name overstates its reach is the false-comfort direction: someone
        later reads the name and believes a branch is guarded.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Example\n\n```bash\n## Testing\nrun `not-the-suite --x`\n```\n\n"
            "## Testing\n\nRun `pytest -q` here.\n")
        self.assertEqual(lib.find_suite_command(self.work), "pytest -q")

    def test_a_command_under_a_NON_test_heading_is_found(self):
        """The SECOND root cause in this function, in the shape that produced it.

        A `CLAUDE.md` can document its suite under `## Repo conventions` —
        with no test-titled heading anywhere in the file, tiers 1 and 2 have
        nothing to search and return None *correctly by their own logic*
        while being wrong about a repo with several suites and CI workflows.
        The inline-span fallback fixed a different gap (a command in an
        inline span rather than a fence) and left this one.

        The fixture is such a bullet, verbatim including the `<skill-name>`
        placeholder — which does not run as written, and is the reason the
        evidence listing beside it is not decoration.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Repo conventions\n\n"
            "- **Skill tests live at repo root**, in `tests/<skill-name>/`, not\n"
            "  inside the skill folder. Run a skill's suite with\n"
            "  `python3 tests/<skill-name>/run_all.py` — stdlib `assert` modules.\n")
        self.assertEqual(lib.find_suite_command(self.work),
                         "python3 tests/<skill-name>/run_all.py")

    def test_the_document_scan_does_not_run_when_a_test_heading_exists(self):
        """Widening where we look must not widen it past the scoping.

        A `## Testing` section that documents nothing runnable means "this repo
        documents no command" — NOT "go and look in the other sections". Without
        this the tier-3 scan rescues exactly the case
        `test_a_span_outside_the_test_section_is_never_used` exists to forbid,
        and that test cannot catch it because its `./app --serve` span fails
        `_runs_tests` for an unrelated reason. This fixture puts a span that
        WOULD pass tier 3 outside the section, so only the precedence rule can
        keep it out.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Usage\n\nOur test harness is `python3 tests/run_all.py` internally.\n\n"
            "## Testing\n\nWe do not have a suite yet.\n")
        self.assertIsNone(lib.find_suite_command(self.work))

    def test_the_document_scan_rejects_a_span_that_does_not_run_tests(self):
        """Tier 3 widens the SEARCH AREA, so it must narrow what it ACCEPTS.

        Both spans here are the kind a real `CLAUDE.md` carries on prose
        lines that mention tests — `set -e` from an installer discussion and
        `... FAIL` from a mutation-harness rule. Under a line-signal-only
        rule (no `_runs_tests`) such a doc resolves to `set -e`: a command
        that runs, exits 0, prints nothing, and would be reported as a green
        suite. That is the failure mode strict detection exists to prevent,
        reachable again through the wider search.

        A doc whose `## Testing` section leads with a fence is safe only
        because it never reaches tier 3. Safety that depends on another
        section's formatting is not safety.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Notes\n\n"
            "Unreachable except via `set -e` on unrelated lines in the tests.\n"
            "With -v, unittest prints a test's docstring on the `... FAIL` line.\n")
        self.assertIsNone(lib.find_suite_command(self.work))

    def test_the_document_scan_rejects_a_non_runner_that_merely_mentions_tests(self):
        """`_runs_tests` is a conjunction, and this pins the half that the
        test-token check alone would let through.

        ``grep -rn 'test' tools/`` names tests and carries arguments, so only
        the argv[0] allowlist rejects it. A reviewer handed it greps the repo,
        sees exit 0, and reports a green suite — the invisible failure, not the
        visible one.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Notes\n\n"
            "Find the test helpers with `grep -rn 'test' tools/` before editing.\n")
        self.assertIsNone(lib.find_suite_command(self.work))

    def test_the_document_scan_rejects_a_runner_that_does_not_mention_tests(self):
        """The other half of the same conjunction.

        `./deploy.sh --prod` passes the argv[0] check (an explicit path is a
        thing being executed), so only the test-token check rejects it. This is
        the worst outcome the function has — a blinded reviewer told to "run
        the suite" shipping to production — and it is reachable from any prose
        line that happens to mention tests near a deploy command.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Shipping\n\n"
            "Once the tests are green, `./deploy.sh --prod` ships it.\n")
        self.assertIsNone(lib.find_suite_command(self.work))

    def test_the_FIRST_document_wide_span_wins_not_the_last(self):
        """Mirrors `test_the_FIRST_plausible_span_wins_not_the_last` for tier 3.

        Dropping the `document_fallback is None` guard makes every later line
        overwrite the candidate. No other tier-3 fixture has two acceptable
        spans, so none of them can tell the two apart — the same blind spot
        that let the tier-2 version of this mutation survive.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Conventions\n\n"
            "Run the suite with `python3 tests/run_all.py` from the root.\n"
            "To rerun one module, `python3 tests/test_one.py` is quicker.\n")
        self.assertEqual(lib.find_suite_command(self.work),
                         "python3 tests/run_all.py")

    def test_a_pytest_line_with_no_standalone_test_word_still_resolves(self):
        """Regression, found by a mutation that SURVIVED.

        A first cut gated tier 3 on the LINE's prose as well as the span. The
        mutation removing that gate survived the whole suite, and the reason is
        that the gate was near-redundant by construction: the span sits inside
        the line, so a `\\btests?\\b` line check is satisfied by almost every
        span `_TEST_TOKEN_RE` already accepted.

        Where the two DID differ, the gate was wrong. `pytest`, `run_all` and
        `unittest` contain no standalone "test" word, so this line — one of the
        most common ways a suite is documented — was rejected. A false negative
        renders NO_SUITE_TEXT, which is the fail-open direction this whole
        change exists to close. The gate is gone; this pins the case.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Conventions\n\nInvoke `pytest -q` from the root.\n")
        self.assertEqual(lib.find_suite_command(self.work), "pytest -q")

    def test_a_document_wide_span_inside_a_fence_is_not_used(self):
        """Rewritten after a blinded review showed it could not fail.

        The old fixture's fenced block held a BARE line, not a backticked
        span, so `_INLINE_CODE_RE` could never match inside it whatever the
        fence guard did — the test passed on a property it never exercised.
        The reviewer demonstrated it two ways: deleting `if inside_any_fence:
        continue` left this test green (the suite killed that mutation, but
        only through pre-existing tier-1/2 tests), and hoisting the tier-3
        gather ABOVE the shared fence guard — the realistic way to break
        tier-3 fence handling alone — survived the rest of the suite while making the
        tool return a command out of a markdown example.

        The fixture now carries a real span inside the fence AND a real
        command after it, so the two outcomes are distinguishable: reading the
        fence returns the example, skipping it returns the true command.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Example\n\n"
            "Here is how a suite gets documented:\n\n"
            "```markdown\n"
            "Run the suite with `python3 tests/example_placeholder.py`.\n"
            "```\n\n"
            "## Conventions\n\n"
            "The real command is `python3 tests/run_all.py`.\n")
        self.assertEqual(lib.find_suite_command(self.work),
                         "python3 tests/run_all.py")

    def test_the_tier3_scan_runs_AFTER_the_fence_guard_not_before(self):
        """Pins the ordering the hoist mutation broke.

        A fenced example is the ONLY content before the real command here, so
        gathering a tier-3 candidate before the `inside_any_fence` check
        returns the example and nothing else can mask it. Named for the
        ordering rather than the outcome, because the outcome is shared with
        several other tests and the ordering is what has no other guard.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Example\n\n"
            "```markdown\n"
            "`python3 tests/from_inside_a_fence.py`\n"
            "```\n\n"
            "Outside the fence the real one is `python3 tests/run_all.py`.\n")
        self.assertEqual(lib.find_suite_command(self.work),
                         "python3 tests/run_all.py")

    def test_tier3_still_requires_arguments_on_the_span(self):
        """A surviving mutation: dropping `_looks_like_a_command` from the
        tier-3 gate changed nothing any test could see.

        It is measurably load-bearing. A doc that mentions the bare word
        `pytest` before naming the real command resolves correctly with it and
        returns bare `pytest` without it — first-wins means the bare word gets
        there first. That is the bare-`ruff` failure reappearing
        through the wider search: a real tool that exits 0 and prints nothing
        alarming, reported as a green suite.
        """
        self._write_claude_md(
            "# A repo\n\n"
            "## Conventions\n\n"
            "We use `pytest`; run the suite with `python3 tests/run_all.py`.\n")
        self.assertEqual(lib.find_suite_command(self.work),
                         "python3 tests/run_all.py")

    def test_a_deploy_script_with_a_test_flag_is_never_the_suite(self):
        """The BLOCKING finding, in every shape the review measured.

        `\\btests?\\b` treats `-` as a word boundary, so a deploy script's own
        opt-out flag satisfied a naive test-token search; the first cut then
        accepted any `./`- or `/`-prefixed argv[0] with no further check, so
        the conjunction collapsed to "an explicit path that mentions tests
        anywhere". Each of these was returned as the suite command, to a
        blinded reviewer told to run it.

        The `pip install` case is the likeliest and the quietest: it exits 0,
        prints success, mutates the reviewer's Python environment, and reads
        as a green suite.
        """
        hostile = (
            "./deploy.sh --skip-tests",
            "./deploy.sh --no-tests",
            "./release.sh --tests-passed",
            "sh scripts/down.sh --test-stack",
            "npm run build --ignore-tests",
            "/bin/rm -rf tests/tmp",
            "python3 -m pip install -r requirements-test.txt",
        )
        for span in hostile:
            with self.subTest(span=span):
                self._write_claude_md(
                    "# A repo\n\n## Shipping\n\n"
                    "Once the tests are green, `%s` is the step.\n" % span)
                self.assertIsNone(lib.find_suite_command(self.work))

    def test_a_tests_path_inside_a_FLAG_does_not_make_a_command_the_suite(self):
        """A surviving mutation in the fix round, kept rather than deleted
        because — unlike the last one — it proved load-bearing when measured.

        Deleting the `token.startswith("-")` exclusion changed no test, since
        every hostile fixture (`--skip-tests`, `--no-tests`) is rejected by the
        segment rules anyway. But a flag whose VALUE is an absolute path under
        `tests/` does reach them: `--find-links=/opt/tests/wheels` splits to
        `[--find-links=, opt, tests, wheels]` and `tests` matches exactly.
        Measured with and without the guard — False vs True on all three spans
        below.

        A flag is never the thing being executed, so excluding it is right.
        `pip install` is again the quiet case: exit 0, prints success, mutates
        the reviewer's environment, reads as a green suite.
        """
        for span in ("python3 -m pip install --find-links=/opt/tests/wheels .",
                     "npm run build --ignore=/opt/tests/x",
                     "python3 -m build --exclude=/srv/tests/fixtures"):
            with self.subTest(span=span):
                self._write_claude_md(
                    "# A repo\n\n## Packaging\n\n"
                    "Before the tests run, `%s` prepares it.\n" % span)
                self.assertIsNone(lib.find_suite_command(self.work))

    def test_a_test_named_runner_script_is_still_accepted(self):
        """The control for the finding above.

        Tightening argv[0] must not reject a script whose NAME is the suite.
        Without this, the fix to the blocking finding is indistinguishable
        from deleting the explicit-path branch outright — which was itself a
        surviving mutation, so nothing else covers this direction.

        Every span here carries an argument, deliberately. A bare
        `./run-tests.sh` is a ONE-TOKEN span and `_looks_like_a_command`
        rejects it before `_runs_tests` is consulted at all — the documented
        one-word gap `_looks_like_a_command` states, not a property of
        this branch.
        The first draft of this test used bare spans and failed for that
        reason; asserting them here would have pinned the wrong rule to the
        wrong function.
        """
        for span in ("./run-tests.sh -v", "./scripts/run-tests.sh --all",
                     "bin/pytest-all --quiet"):
            with self.subTest(span=span):
                self._write_claude_md(
                    "# A repo\n\n## Conventions\n\nInvoke `%s` here.\n" % span)
                self.assertEqual(lib.find_suite_command(self.work), span)

    # DELIBERATELY NOT ADDED: a test resolving real sibling checkouts and
    # asserting one returns a command. It reads ambient state, so it must
    # skip when they are absent -- and it resolves them relative to this
    # file, which inside `.claude/worktrees/<name>/` points at the worktree.
    # It would therefore skip in every worktree, which is where this tool is
    # developed: a guard with an off switch that is ON by default. The
    # synthetic fixtures above encode the real doc shapes verbatim and run
    # everywhere; check real repos BY HAND instead.
    #
    # A by-hand check is only as good as the reader's belief about the repo:
    # one such check recorded a repo as "correctly None (it documents no
    # suite)" when it had several suites, documented under
    # `## Repo conventions` -- a false negative written down as a
    # confirmation, which is how the second root cause survived to be found
    # separately.

    def test_fallback_text_used_by_the_cli_when_no_suite_documented(self):
        """The true-negative case: nothing documented AND nothing on disk."""
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("feature.txt", "hello\n")
        result = run_cli(["--pre-pr", "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no command documented in CLAUDE.md", result.stdout)


class TestSuiteEvidence(GitRepoTestCase):
    """`find_suite_command` answers "is a command DOCUMENTED". That is a
    different question from "does this repo HAVE tests", and rendering both as
    one sentence is what made `## Suite` a guard that fails open — twice, in
    two repos, for two unrelated parser reasons."""

    def _write(self, relpath, content="x\n"):
        full = os.path.join(self.work, relpath)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)
        git("add", relpath, cwd=self.work)
        git("commit", "-q", "-m", "add %s" % relpath, cwd=self.work)

    def test_workflows_and_test_files_are_both_found(self):
        self._write(".github/workflows/tests.yml", "name: Tests\n")
        self._write("tests/alpha-skill/run_all.py")
        self._write("tests/test_check_bundles.py")
        workflows, tests = lib.suite_evidence(self.work)
        self.assertEqual(workflows, [".github/workflows/tests.yml"])
        self.assertEqual(tests, ["tests/alpha-skill/run_all.py",
                                 "tests/test_check_bundles.py"])

    def test_an_empty_repo_yields_no_evidence(self):
        """The true negative has to stay reachable, or the fix trades a
        fail-open for a section that can never say anything useful."""
        self.commit("README.md", "hi\n")
        self.assertEqual(lib.suite_evidence(self.work), ([], []))

    def test_untracked_and_ignored_paths_are_not_evidence(self):
        """Enumerated with `git ls-files`, and this is why.

        The first cut used `os.walk`. Concurrent sessions keep their
        checkouts under `.claude/worktrees/` (gitignored), so the walk
        reported several times the repo's own test files, nearly all inside
        OTHER live sessions' trees. Handing a blinded reviewer those paths
        points them at a peer's working tree, where running anything can
        disturb a live session's files.

        A walk with a prune list would also have to be maintained; tracked
        files are the right set by construction.
        """
        self._write(".gitignore", ".claude/worktrees/\n")
        peer = os.path.join(self.work, ".claude", "worktrees", "peer", "tests")
        os.makedirs(peer)
        with open(os.path.join(peer, "test_peer.py"), "w", encoding="utf-8") as handle:
            handle.write("# a peer session's file\n")
        with open(os.path.join(self.work, "test_untracked.py"),
                  "w", encoding="utf-8") as handle:
            handle.write("# never committed\n")
        _, tests = lib.suite_evidence(self.work)
        self.assertEqual(tests, [])

    def test_a_non_git_directory_degrades_to_no_evidence(self):
        """Never raises. Less evidence falls back to the pre-existing wording;
        an exception would fail a whole brief over one section."""
        plain = os.path.join(self.tmp, "not-a-repo")
        os.makedirs(plain)
        self.assertEqual(lib.suite_evidence(plain), ([], []))


class TestSuiteSectionCannotClaimNoSuiteWithTestsPresent(GitRepoTestCase):
    """The class fix. A reviewer's "nothing was executed" is only informative
    if the brief could tell "this repo has no tests" apart from "this tool
    could not find them"."""

    def _write(self, relpath, content="x\n"):
        full = os.path.join(self.work, relpath)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(content)
        git("add", relpath, cwd=self.work)
        git("commit", "-q", "-m", "add %s" % relpath, cwd=self.work)

    def test_undocumented_but_tested_repo_is_never_called_suiteless(self):
        """No CLAUDE.md at all, a suite and a workflow on disk — the
        no-test-heading situation with the documentation removed, which is
        where the next repo lands."""
        self._write(".github/workflows/tests.yml", "name: Tests\n")
        self._write("tests/alpha/run_all.py")
        section = lib.render_suite_section(self.work)
        self.assertNotIn("no suite in this repo", section)
        self.assertIn("DOES carry tests", section)
        self.assertIn(".github/workflows/tests.yml", section)
        self.assertIn("tests/alpha", section)

    def test_a_documented_command_still_leads_the_section(self):
        """Evidence supplements the command; it must never displace it."""
        self._write("CLAUDE.md", "# R\n\n## Testing\n\n```\npytest -q\n```\n")
        self._write(".github/workflows/bundle-checks.yml", "name: bundle\n")
        section = lib.render_suite_section(self.work)
        self.assertIn("pytest -q", section)
        self.assertNotIn("DOES carry tests", section)
        self.assertIn(".github/workflows/bundle-checks.yml", section)

    def test_a_genuinely_suiteless_repo_still_says_so(self):
        """The positive control. Without it, "never claim no suite" is
        satisfied by a function that can no longer produce a true negative —
        disarmed in the opposite direction, and indistinguishable from fixed
        in every other test here."""
        self._write("CLAUDE.md", "# R\n\nNo tests here.\n")
        section = lib.render_suite_section(self.work)
        self.assertEqual(section, lib.NO_SUITE_TEXT)
        self.assertIn("no command documented in CLAUDE.md", section)
        # The message must NOT assert a broader disk measurement than
        # `_TEST_FILE_RE` performs -- it names the conventions it knows.
        self.assertIn("may exist unseen", section)

    def test_a_deploy_only_workflow_does_not_claim_the_repo_carries_tests(self):
        """A blinded review measured this: a repo with one `deploy.yml` and
        zero test files rendered "this repo DOES carry tests ... find the
        command from the evidence below and run it."

        That is a false statement wrapped around an instruction to override
        the reviewer's correct conclusion — and pointing it at a deploy
        workflow partly undoes the paths-never-commands decision the evidence
        block is built on. Only TEST FILES license the strong claim; workflows
        alone get a weaker, true one that still prevents a silent "no suite".
        """
        self._write(".github/workflows/deploy.yml", "name: Deploy\n")
        section = lib.render_suite_section(self.work)
        self.assertNotIn("DOES carry tests", section)
        self.assertNotEqual(section, lib.NO_SUITE_TEXT)
        self.assertIn("has CI", section)
        self.assertIn(".github/workflows/deploy.yml", section)

    def test_the_evidence_sample_shows_every_suite_not_the_first_directory(self):
        """Found by running the tool on a repo with several suites.

        The sample was the first 8 filenames, alphabetically, so all eight
        slots went to the first suite's directory and the repo's three
        other suites were unreachable from the brief. The header said
        "20 test file(s)", so a reviewer knew more existed and could not learn
        WHICH — a quieter version of the same fail-open this section closes.

        The fixture needs MORE than `EVIDENCE_SAMPLE` files in the
        alphabetically-first directory, or a grouped renderer and a flat one
        produce identical output and the test cannot tell them apart. That is
        the "two conditions one fixture cannot distinguish" trap this repo has
        hit before, so the count is asserted explicitly.
        """
        for n in range(lib.EVIDENCE_SAMPLE + 2):
            self._write("tests/aaa_first/test_%02d.py" % n)
        self._write("tests/zzz_last/run_all.py")
        self._write("tests/test_top_level.py")

        _, tests = lib.suite_evidence(self.work)
        self.assertGreater(len(tests), lib.EVIDENCE_SAMPLE)

        section = lib.render_suite_section(self.work)
        self.assertIn("tests/aaa_first", section)
        self.assertIn("tests/zzz_last", section)
        self.assertIn("tests/test_top_level.py".rsplit("/", 1)[0], section)
        # The total must survive the regrouping: it is the only signal that
        # anything was elided at all.
        self.assertIn("%d test file(s)" % len(tests), section)

    def test_the_cli_renders_the_evidence_path_end_to_end(self):
        """Through the executable, not just the lib: a green unit suite
        can exercise zero lines of the entry point a user actually runs."""
        self._write(".github/workflows/tests.yml", "name: Tests\n")
        self._write("tests/alpha/run_all.py")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("feature.txt", "hello\n")
        result = run_cli(["--pre-pr", "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("no suite in this repo", result.stdout)
        self.assertIn("DOES carry tests", result.stdout)


class TestPlanMode(GitRepoTestCase):
    """`--plan FILE` builds a Plan Review brief: the fixed requirements
    block from `plan-requirements.md`, then FILE's own text, verbatim. No
    merge ref, no diff, no commit log, no freshness check — a plan has none
    of those."""

    def _write_plan(self, name, text, classify=True):
        """`classify=True` (the default) appends a `Classification: bounded`
        header line, since `build_plan_brief` refuses a plan with none.
        Pass `classify=False` for a fixture that is deliberately
        empty or whitespace-only -- appending text would stop it from being
        either."""
        if classify:
            text = text + "\n\nClassification: bounded\n"
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_plan_is_in_the_mutually_exclusive_mode_group(self):
        """Mutation 1's target: drop --plan from the group and this test
        goes red -- `--plan x --pre-pr` must exit 3, not run either mode."""
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        result = run_cli(["--plan", plan_path, "--pre-pr", "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertNotIn("Traceback", result.stderr)

    def test_base_is_rejected_under_plan(self):
        """--base already has this shape via the --pre-pr check -- asserted
        here so a future refactor of that check cannot silently drop it."""
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        result = run_cli(["--plan", plan_path, "--base", "main", "--repo", self.work])
        self.assertEqual(result.returncode, 3)

    def test_since_is_rejected_under_plan(self):
        """--since had NO guard before this task; a plan has no commit
        range to scope."""
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        result = run_cli(["--plan", plan_path, "--since", "abc1234", "--repo", self.work])
        self.assertEqual(result.returncode, 3)

    def test_missing_plan_file_exits_3_naming_the_path(self):
        missing = os.path.join(self.tmp, "does-not-exist.md")
        result = run_cli(["--plan", missing, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertIn(missing, result.stderr)

    def test_empty_plan_file_exits_3(self):
        """Mutation 2's target: let an empty plan file through and this
        test goes red -- a zero-byte brief is the "gate that read nothing"
        failure."""
        empty = self._write_plan("empty.md", "", classify=False)
        result = run_cli(["--plan", empty, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertIn(empty, result.stderr)

    def test_a_plan_that_is_not_utf8_exits_3_not_a_traceback(self):
        """A decode error is a ValueError, not an OSError, so catching only
        OSError let it escape as a traceback and exit 1."""
        path = os.path.join(self.tmp, "latin1.md")
        with open(path, "wb") as handle:
            handle.write(b"# Caf\xe9 plan\n\nClassification: bounded\n")
        result = run_cli(["--plan", path, "--repo", self.work])
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn(path, result.stderr)

    def test_an_unwritable_output_path_exits_2_not_a_traceback(self):
        """`-o` into a directory that does not exist: no brief was written,
        which is exit 2's meaning, said on stderr, never a raw traceback."""
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        out = os.path.join(self.tmp, "no-such-dir", "brief.md")
        result = run_cli(["--plan", plan_path, "--repo", self.work, "-o", out])
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("could not write the brief", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_plan_requirements_exits_2_with_resolved_path(self):
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        bogus = os.path.join(self.tmp, "no-such-requirements.md")
        with mock.patch.object(lib, "PLAN_REQUIREMENTS_PATH", bogus):
            with self.assertRaises(lib.BriefError) as cm:
                lib.build_plan_brief(self.work, plan_path)
        self.assertNotIsInstance(cm.exception, lib.PlanFileError)
        self.assertIn(bogus, str(cm.exception))

    def test_empty_plan_requirements_exits_2(self):
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        empty_requirements = self._write_plan(
            "empty-requirements.md", "", classify=False)
        with mock.patch.object(lib, "PLAN_REQUIREMENTS_PATH", empty_requirements):
            with self.assertRaises(lib.BriefError) as cm:
                lib.build_plan_brief(self.work, plan_path)
        self.assertNotIsInstance(cm.exception, lib.PlanFileError)

    def test_brief_carries_requirements_then_plan_verbatim_with_mode_header(self):
        """Mutation 4's target: make build_plan_brief skip
        the requirements block and the requirements-present assertion below
        goes red."""
        plan_text = "# A distinctive plan\n\nDo the specific thing.\n"
        plan_path = self._write_plan("plan.md", plan_text)
        with open(lib.PLAN_REQUIREMENTS_PATH, encoding="utf-8") as handle:
            requirements_text = handle.read().strip()

        brief = lib.build_plan_brief(self.work, plan_path)

        self.assertIn("- mode: plan", brief)
        self.assertIn(requirements_text, brief)
        self.assertIn(plan_text, brief)
        self.assertLess(
            brief.index(requirements_text), brief.index(plan_text),
            "the requirements block must come BEFORE the plan text, not after")

    def test_plan_text_is_embedded_verbatim_preserving_leading_whitespace(self):
        """`plan_text.strip()` removes
        leading/trailing whitespace before embedding, and the OLD version of
        the test above compared against `plan_text.strip()` too, so it could
        never fail. A plan opening with an indented code block would
        silently lose its indentation. Mutation's target: re-introduce
        `.strip()` on the embedded plan text and this test goes red -- the
        comparison here is against the UNSTRIPPED original, so a stripped
        embed cannot pass it by accident."""
        plan_text = "    first-line code block\n\nDo the thing.\n"
        plan_path = self._write_plan("indented-plan.md", plan_text)

        brief = lib.build_plan_brief(self.work, plan_path)

        self.assertIn("## Plan\n\n" + plan_text, brief,
                       "the plan's leading whitespace was not preserved "
                       "verbatim in the embedded brief")

    def test_brief_has_no_diff_commit_or_freshness_sections(self):
        """Mutation 3's target: have build_plan_brief emit a diff section
        and this test goes red -- a plan brief that silently grew a diff
        section would be reviewed as a code change."""
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        brief = lib.build_plan_brief(self.work, plan_path)
        self.assertNotIn("## Diff", brief)
        self.assertNotIn("## Commit messages", brief)
        self.assertNotIn("freshness:", brief.lower())

    def test_suite_section_is_retained(self):
        """A plan reviewer still needs to know how this repo verifies its
        own work, same as every other mode."""
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        brief = lib.build_plan_brief(self.work, plan_path)
        self.assertIn("## Suite", brief)

    def test_cli_end_to_end_writes_a_plan_brief(self):
        """Through the executable, not just the lib: a green unit suite
        can exercise zero lines of the entry point a user actually runs."""
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## Requirements", result.stdout)
        self.assertIn("## Plan", result.stdout)
        self.assertIn("Do the thing.", result.stdout)

    # --- The source requirements ride in the brief ------------------------

    def _write_file(self, relative, text):
        path = os.path.join(self.tmp, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _brief_lines(self, brief):
        return brief.split("\n")

    def test_source_requirements_sit_between_rubric_and_plan(self):
        sentinel = "sentinel-source-requirement-4f2a"
        self._write_file("req.md", "Intro line\n\n%s\n" % sentinel)
        plan_path = self._write_file(
            "plan.md",
            "# A plan\n\nDo the thing.\n\nClassification: bounded\n"
            "Requirements: req.md\n")
        brief = lib.build_plan_brief(self.work, plan_path)
        lines = self._brief_lines(brief)
        order = ["## Requirements", "## Source requirements", "## Plan",
                 "## Suite"]
        for heading in order:
            self.assertIn(heading, lines)
        indices = [lines.index(heading) for heading in order]
        self.assertEqual(indices, sorted(set(indices)),
                         "brief headings out of order: %r" % indices)
        self.assertEqual(lines.count("## Source requirements"), 1)
        self.assertEqual(brief.count(sentinel), 1)
        sentinel_at = next(i for i, line in enumerate(lines)
                           if sentinel in line)
        self.assertGreater(sentinel_at, lines.index("## Source requirements"))
        self.assertLess(sentinel_at, lines.index("## Plan"))
        self.assertIn("- requirements file: ", brief)

    def test_source_requirements_are_embedded_verbatim(self):
        text = "    indented first line\nsecond line\nlast line no newline"
        self._write_file("req.md", text)
        plan_path = self._write_file(
            "plan.md",
            "# A plan\n\nClassification: bounded\nRequirements: req.md\n")
        brief = lib.build_plan_brief(self.work, plan_path)
        self.assertIn(text, brief)

    def test_no_requirements_line_leaves_the_brief_as_before(self):
        plan_path = self._write_plan("plan.md", "# A plan\n\nDo the thing.\n")
        brief = lib.build_plan_brief(self.work, plan_path)
        lines = self._brief_lines(brief)
        self.assertNotIn("## Source requirements", lines)
        self.assertNotIn("requirements file:", brief)
        self.assertLess(lines.index("## Requirements"), lines.index("## Plan"))
        self.assertLess(lines.index("## Plan"), lines.index("## Suite"))

    def test_requirements_resolve_against_the_plan_not_the_cwd_end_to_end(self):
        self._write_file("req.md", "only in the cwd\n")
        plan_path = self._write_file(
            "sub/plan.md",
            "# A plan\n\nClassification: bounded\nRequirements: req.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work],
                         cwd=self.tmp)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn(os.path.join("sub", "req.md"), result.stderr)
        # Positive control: the file beside the plan makes it succeed.
        self._write_file("sub/req.md", "beside the plan\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work],
                         cwd=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("beside the plan", result.stdout)

    def test_missing_requirements_file_exits_3(self):
        plan_path = self._write_file(
            "plan.md",
            "# A plan\n\nClassification: bounded\nRequirements: gone.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("gone.md", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_empty_requirements_file_exits_3(self):
        self._write_file("req.md", "  \n\n \t\n")
        plan_path = self._write_file(
            "plan.md",
            "# A plan\n\nClassification: bounded\nRequirements: req.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("req.md", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_design_body_still_never_reaches_the_brief(self):
        self._write_file(
            "design.md",
            "---\nstatus: live\nsubject: test\nApproved: 2026-09-23\n---\n\n"
            "sentinel-design-body-9c1d\n")
        self._write_file("req.md", "sentinel-requirements-body-7b3e\n")
        plan_path = self._write_file(
            "plan.md",
            "# A plan\n\nClassification: architectural\nDesign: design.md\n"
            "Requirements: req.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("sentinel-design-body-9c1d", result.stdout)
        self.assertIn("sentinel-requirements-body-7b3e", result.stdout)

    def test_leaked_heading_in_requirements_refuses_under_plan(self):
        self._write_file("req.md", "Requirements\n\n## Round 2 findings\n\nx\n")
        plan_path = self._write_file(
            "plan.md",
            "# A plan\n\nClassification: bounded\nRequirements: req.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")


class TestPlanHeaderValidation(GitRepoTestCase):
    """`merge_brief_lib.validate_plan_header`, wired into
    `build_plan_brief` right after the existing empty-file check. Every case
    goes through the real executable via `run_cli`, with its own raw writer
    -- most of these fixtures are deliberately malformed, so routing them
    through `TestPlanMode`'s `_write_plan` (which always adds a valid
    `Classification:` line) would defeat the point."""

    def _write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _write_approved_design(self, name, extra_body=""):
        text = ("---\nstatus: live\nsubject: test\nApproved: 2026-09-23\n"
                 "---\n\n%s" % extra_body)
        return self._write(name, text)

    def test_a_no_classification_line_is_refused(self):
        plan_path = self._write("plan.md", "# A plan\n\nDo the thing.\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("no `Classification:` line", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_b_unrecognised_classification_is_refused(self):
        plan_path = self._write(
            "plan.md", "# A plan\n\nClassification: sideways\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("unrecognised classification", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_c_conflicting_classification_lines_is_refused(self):
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: bounded\n\n"
            "Classification: architectural\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("conflicting `Classification:` lines", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_d_architectural_with_no_design_line_is_refused(self):
        plan_path = self._write(
            "plan.md", "# A plan\n\nClassification: architectural\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("no `Design:` line", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_e_missing_design_file_is_refused_naming_resolved_path(self):
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\n"
            "Design: missing-design.md\n")
        resolved = os.path.join(
            os.path.dirname(os.path.realpath(plan_path)), "missing-design.md")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("design file", result.stderr)
        self.assertIn(resolved, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_f_design_file_with_front_matter_but_no_approved_is_refused(self):
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\nDesign: design.md\n")
        self._write(
            "design.md", "---\nstatus: live\nsubject: test\n---\n\nBody.\n")
        design_resolved = os.path.join(
            os.path.dirname(os.path.realpath(plan_path)), "design.md")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("no `Approved:` line", result.stderr)
        self.assertIn(design_resolved, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_n_unterminated_front_matter_is_not_front_matter(self):
        """With no closing `---`, the whole
        file read as front matter, so any `Approved:` line anywhere counted.
        An unclosed block is not front matter at all."""
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\nDesign: design.md\n")
        self._write(
            "design.md",
            "---\nstatus: live\nApproved: 2026-09-23\nsome stray prose\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("no `Approved:` line", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_g_approved_line_in_the_body_after_front_matter_is_refused(self):
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\nDesign: design.md\n")
        self._write(
            "design.md",
            "---\nstatus: live\nsubject: test\n---\n\n"
            "Approved: 2026-09-23\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("no `Approved:` line", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_h_classification_line_inside_a_fence_does_not_count(self):
        plan_path = self._write(
            "plan.md",
            "# A plan\n\n```\nClassification: bounded\n```\n\nDo the thing.\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("no `Classification:` line", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_i_valid_architectural_with_bare_filename_design_succeeds(self):
        self._write_approved_design(
            "design.md", extra_body="sentinel-design-body-text\n")
        self._write("req.md", "the source requirements\n")
        plan_text = ("# A plan\n\nClassification: architectural\n"
                      "Design: design.md\nRequirements: req.md\n")
        plan_path = self._write("plan.md", plan_text)
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(plan_text, result.stdout)
        self.assertNotIn("sentinel-design-body-text", result.stdout)

    def test_j_valid_bounded_with_no_design_line_succeeds(self):
        plan_path = self._write(
            "plan.md", "# A plan\n\nClassification: bounded\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_k_valid_architectural_with_backtick_wrapped_design_path_succeeds(self):
        self._write_approved_design("design.md")
        self._write("req.md", "the source requirements\n")
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\n"
            "Design: `design.md`\nRequirements: req.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_l_conflicting_design_lines_naming_two_existing_files_is_refused(self):
        self._write_approved_design("design-one.md")
        self._write_approved_design("design-two.md")
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\n"
            "Design: design-one.md\nDesign: design-two.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(result.stdout, "")
        self.assertIn("conflicting `Design:` lines", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_m_repeated_identical_design_line_succeeds(self):
        self._write_approved_design("design.md")
        self._write("req.md", "the source requirements\n")
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\n"
            "Design: design.md\nDesign: design.md\nRequirements: req.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_o_bare_and_backticked_spellings_of_one_design_succeed(self):
        """Code Review (IMPORTANT, confirmed): dedupe ran before the backtick
        strip, so two spellings of one path were refused as a conflict."""
        self._write_approved_design("design.md")
        self._write("req.md", "the source requirements\n")
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\n"
            "Design: design.md\nDesign: `design.md`\nRequirements: req.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_p_non_utf8_design_is_refused_without_a_traceback(self):
        """Code Review (IMPORTANT, confirmed): a decode error escaped the
        OSError handler and crashed the CLI with exit 1."""
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\nDesign: design.md\n")
        with open(os.path.join(self.tmp, "design.md"), "wb") as handle:
            handle.write(b"\xff")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("could not be read", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_q_architectural_without_requirements_is_refused(self):
        self._write_approved_design("design.md")
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\nDesign: design.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("Requirements:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_r_conflicting_requirements_lines_are_refused(self):
        self._write_approved_design("design.md")
        self._write("req-one.md", "one\n")
        self._write("req-two.md", "two\n")
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\nDesign: design.md\n"
            "Requirements: req-one.md\nRequirements: req-two.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("conflicting", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_s_requirements_line_inside_a_fence_does_not_count(self):
        self._write_approved_design("design.md")
        self._write("req.md", "the source requirements\n")
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\nDesign: design.md\n"
            "\n```\nRequirements: req.md\n```\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("Requirements:", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_t_architectural_with_requirements_is_accepted(self):
        self._write_approved_design("design.md")
        self._write("req.md", "the source requirements\n")
        plan_path = self._write(
            "plan.md",
            "# A plan\n\nClassification: architectural\nDesign: design.md\n"
            "Requirements: req.md\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_u_requirements_resolve_against_the_plan_not_the_cwd(self):
        """Direct, in-process: the return value is the resolved path, joined
        to the plan's own directory (realpath), never `os.getcwd()`."""
        sub = os.path.join(self.tmp, "sub")
        os.makedirs(sub)
        plan_path = os.path.join(sub, "plan.md")
        plan_text = "# A plan\n\nClassification: bounded\nRequirements: req.md\n"
        previous_cwd = os.getcwd()
        self.addCleanup(os.chdir, previous_cwd)
        os.chdir(self.tmp)
        resolved = lib.validate_plan_header(plan_path, plan_text)
        self.assertEqual(
            resolved, os.path.join(os.path.realpath(sub), "req.md"))
        self.assertNotEqual(resolved, os.path.join(self.tmp, "req.md"))
        # A bounded plan with no line returns None, not a path.
        self.assertIsNone(lib.validate_plan_header(
            plan_path, "# A plan\n\nClassification: bounded\n"))
        # An absolute value comes back unchanged.
        absolute = os.path.join(self.tmp, "elsewhere", "req.md")
        self.assertEqual(
            lib.validate_plan_header(
                plan_path,
                "# A plan\n\nClassification: bounded\nRequirements: %s\n"
                % absolute),
            absolute)


class TestArgumentSurface(GitRepoTestCase):
    """No flag accepts free text or a file to embed alongside a diff. Tested
    by argument surface — an unrecognised flag is rejected outright — not by
    scanning the brief's text for keywords.

    `--plan FILE` is the one narrow exception, and it is pinned here too:
    the whole-flag-set test below now expects to see it, and the open()
    allowlist test right after it expects to see exactly the two new reads
    `--plan` requires — never a third, and never a looser check that would
    let some future flag ride in on the same allowance.
    """

    def test_the_whole_flag_set_is_pinned(self):
        """The parity guarantee is that there is NO WAY IN, so it has to be
        asserted over the whole argument surface rather than over the flags that
        happen to exist today.

        `test_unknown_flag_is_rejected` below cannot carry this on its own: it
        asserts a property of argparse, not of this tool, and it would still pass
        the day somebody adds `--context FILE`. Then the brief would carry
        whatever the dispatcher chose to put in it, blinding would be gone, and
        every test here would stay green — the gate looking clean while catching
        nothing, which is the exact failure mode this whole gate exists to
        remove.

        So: adding a flag fails this test on purpose. That is the review
        checkpoint. If the new flag genuinely cannot inject prose, widen the set
        deliberately and say why in the commit.
        """
        loader = importlib.machinery.SourceFileLoader("_cli", CLI)
        spec = importlib.util.spec_from_loader("_cli", loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)

        options = set()
        for action in cli.build_parser()._actions:
            options.update(action.option_strings)
        self.assertEqual(
            {"-h", "--help", "--pr", "--pre-pr", "--plan", "--repo",
             "--base", "--since", "-o", "--output"},
            options,
            "the argument surface changed. Every flag here resolves to a git "
            "ref, a repo path, an output destination, or (for --plan only) "
            "the one file it exists to review. Adding a flag that carries "
            "prose alongside a DIFF would silently end blinding.")

    # The set of `open()` targets `merge_brief_lib.py` is allowed to read.
    # `--plan` earns exactly two: the fixed, repo-shipped requirements path,
    # and the caller's own --plan file (the artifact under review, not
    # commentary alongside one). `claude_md` predates this task. Any other
    # target is a potential injection channel.
    #
    # `requirements_path` is the opposite of `design_path`:
    # it IS a channel into the brief, deliberately. The file is named by the
    # plan itself (the artifact under review) and is the acceptance contract
    # Plan Review's coverage lens checks the plan against, so its text is
    # embedded verbatim. The CLI still gains no flag.
    #
    # `design_path` is the one
    # deliberate widening: `validate_plan_header` opens the plan's `Design:`
    # file ONLY to check its front matter for an `Approved:` line, and
    # nothing read from it reaches the brief -- so it is a check, not a
    # channel. `TestPlanHeaderValidation.test_i_*` pins that: a sentinel
    # written only into the design's body must be absent from the brief.
    _ALLOWED_OPEN_TARGETS = frozenset(
        ("claude_md", "PLAN_REQUIREMENTS_PATH", "plan_path", "design_path",
         "requirements_path"))

    def test_no_flag_takes_a_free_text_or_file_value(self):
        """The complement of the pin above: not just *which* flags exist, but
        that none of them is a channel. `--repo` and `-o` do take paths, and
        neither one is read INTO the brief — one selects the subject, the other
        is where the brief is written."""
        with open(os.path.join(TOOL_DIR, "merge_brief_lib.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        # Matched on the first argument only — `encoding=`/`errors=` kwargs are
        # in the same parens and are not read targets.
        for call in re.findall(r"open\(([^)]*)\)", source):
            target = call.split(",")[0].strip()
            self.assertIn(
                target, self._ALLOWED_OPEN_TARGETS,
                "merge_brief_lib reads {!r} off disk — every diff-mode "
                "section must come from git or gh output, and --plan's own "
                "carve-out is a fixed two targets, not a general "
                "licence".format(target))

    def test_the_open_allowlist_is_exactly_the_plan_targets_not_a_licence(self):
        """The carve-out `--plan` earns is pinned by COUNT as well as by
        membership, not just the per-target `assertIn` above.

        A per-target check alone would still pass if this allowlist were
        quietly widened alongside a fourth target — every individual entry
        would look plausible on its own, which is exactly the "carve-out
        becomes a general licence" failure. Pinning the
        exact set here means widening it — for `--plan` or for any future
        flag — has to be a deliberate edit to THIS test, not a side effect
        of loosening the one above. It was widened once, deliberately, for
        `design_path` -- see the comment on `_ALLOWED_OPEN_TARGETS`.
        """
        with open(os.path.join(TOOL_DIR, "merge_brief_lib.py"),
                  encoding="utf-8") as handle:
            source = handle.read()
        targets = {call.split(",")[0].strip()
                   for call in re.findall(r"open\(([^)]*)\)", source)}
        self.assertEqual(
            targets, {"claude_md", "PLAN_REQUIREMENTS_PATH", "plan_path",
                      "design_path", "requirements_path"},
            "the open() targets in merge_brief_lib.py changed. If a new "
            "flag genuinely needs to read a caller-named file, widen this "
            "set (and the allowlist above) DELIBERATELY and say why in the "
            "commit; a hypothetical --context-style flag reading its own "
            "value must fail THIS assertion, not slip through it.")

    def test_unknown_flag_is_rejected(self):
        result = run_cli(["--pre-pr", "--context", "some plan text", "--repo", self.work])
        self.assertEqual(result.returncode, 3)
        self.assertIn("unrecognized arguments", result.stderr)

    def test_base_without_pre_pr_is_a_usage_error(self):
        result = run_cli(["--pr", "1", "--base", "main", "--repo", self.work])
        self.assertEqual(result.returncode, 3)

    def test_neither_pr_nor_pre_pr_is_a_usage_error(self):
        result = run_cli(["--repo", self.work])
        self.assertEqual(result.returncode, 3)

    def test_both_pr_and_pre_pr_is_a_usage_error(self):
        result = run_cli(["--pr", "1", "--pre-pr", "--repo", self.work])
        self.assertEqual(result.returncode, 3)


class TestBaseResolutionUsesMergeBriefsOwnTimeout(GitRepoTestCase):
    """`freshness_lib.GIT_TIMEOUT_SECONDS` was tightened 5 -> 2 for the
    REPORT loop's own budget and must never silently narrow the timeout THIS
    tool builds its blinded review briefs against. Without this test, the
    coupling between the two constants is silent and the full suite stays
    green either way."""

    def test_symbolic_ref_call_runs_at_merge_briefs_timeout(self):
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("feature.txt", "hello\n")

        real_run = lib.freshness_lib.subprocess.run
        with mock.patch.object(lib.freshness_lib.subprocess, "run",
                               wraps=real_run) as run:
            lib.build_pre_pr_brief(self.work)

        symbolic_ref_calls = [
            call for call in run.call_args_list
            if "symbolic-ref" in call.args[0]]
        self.assertTrue(symbolic_ref_calls, "default_branch's git call never ran")
        for call in symbolic_ref_calls:
            self.assertEqual(call.kwargs["timeout"], lib.GIT_TIMEOUT_SECONDS)
            self.assertNotEqual(
                call.kwargs["timeout"], lib.freshness_lib.GIT_TIMEOUT_SECONDS,
                "base resolution ran at freshness_lib's own timeout, not "
                "merge-brief's -- the two constants have silently recoupled")


class TestProvenanceScanRegexes(unittest.TestCase):
    """Direct tests of `HEADING`/`PAYLOAD`/`scan_for_leaked_provenance`,
    decoupled from git and the CLI -- the regressions below are
    properties of the regex itself, not of any
    particular brief. No `GitRepoTestCase` needed; nothing here touches a
    repo."""

    def test_heading_matches_the_acceptance_example_case_insensitively(self):
        """An unflagged regex could not match its own
        acceptance example (`re.search(r'^#{1,6} .*\\bround \\d',
        '## Round 2 findings')` -> False). Mutation 3's target: drop
        `re.I` from `HEADING` and this test goes red."""
        self.assertTrue(lib.HEADING.search("## Round 2 findings"))

    def test_extra_whitespace_does_not_slip_past_the_literal_spaces(self):
        """`\\s+` replaces literal spaces in the locked regex specifically
        so `##  Round  2` cannot slip past a naive single-space match."""
        self.assertTrue(lib.HEADING.search("##  Round  2  findings"))

    def test_heading_requires_round_and_a_digit_not_just_a_hash_heading(self):
        self.assertIsNone(lib.HEADING.search("## An ordinary heading"))
        self.assertIsNone(lib.HEADING.search("## Round two findings"))

    def test_payload_requires_a_literal_value_not_a_bare_key(self):
        """`"severity"` alone is legitimate (a schema saying
        `"severity": {"type": ...}`, or a real `"confirmed":` JSON key in
        `docs/build-measurements.md`) and must never match. Mutation 5's
        target: loosen `PAYLOAD` to bare `"severity"` and this test goes
        red."""
        self.assertIsNone(lib.PAYLOAD.search('"severity": {"type": "string"}'))
        self.assertIsNone(lib.PAYLOAD.search('{"confirmed": 3, "escalated": true}'))

    def test_payload_matches_a_real_instance(self):
        self.assertTrue(lib.PAYLOAD.search('"severity": "BLOCKING"'))
        self.assertTrue(lib.PAYLOAD.search('"verdict": "confirmed"'))

    def test_payload_never_refuses_on_the_real_schema_file(self):
        """The real-world example: `merge-review-report.schema.json`
        legitimately says `"severity": {"type": ...}` -- a schema, not an
        instance. Scanned as if it were a --plan document (the mode where
        PAYLOAD refuses), it must never raise. Mutation 5's target
        (loosening PAYLOAD to bare `"severity"`) makes this file refuse."""
        schema_path = os.path.join(
            REPO_ROOT, "tools", "build-lens", "merge-review-report.schema.json")
        with open(schema_path, encoding="utf-8") as handle:
            text = handle.read()
        lib.scan_for_leaked_provenance(text, "plan")  # must not raise

    def test_payload_warns_not_refuses_on_the_real_codex_review_fixtures(self):
        """The complement, using codex-review's own test fixtures:
        `test_codex_review.py` carries real `"severity": "BLOCKING"`
        instances as fixture data, legitimate inside a DIFF. Scanned as a
        diff (`--pr`/`--pre-pr`) it must warn, not refuse."""
        fixture_path = os.path.join(
            REPO_ROOT, "tools", "codex-review", "tests", "test_codex_review.py")
        with open(fixture_path, encoding="utf-8") as handle:
            text = handle.read()
        warnings = lib.scan_for_leaked_provenance(text, "pre-pr")  # must not raise
        self.assertTrue(warnings, "the file's own literal fixtures produced "
                         "no warning at all -- PAYLOAD stopped matching them")

    def test_heading_refuses_in_every_mode(self):
        for mode in ("plan", "pr", "pre-pr"):
            with self.assertRaises(lib.ProvenanceLeakError):
                lib.scan_for_leaked_provenance("## Round 2 findings\n", mode)

    def test_payload_refuses_only_under_plan(self):
        """The mode split itself, at the function that decides it -- the
        CLI-level tests below pin the same rule through the wiring."""
        brief = '"severity": "BLOCKING"\n'
        with self.assertRaises(lib.ProvenanceLeakError):
            lib.scan_for_leaked_provenance(brief, "plan")
        for mode in ("pr", "pre-pr"):
            warnings = lib.scan_for_leaked_provenance(brief, mode)
            self.assertTrue(warnings, "PAYLOAD under %r produced no warning "
                             "at all -- WARN is not a silent no-op" % mode)

    def test_prose_blocking_and_round_warn_with_line_numbers(self):
        brief = "line one\nBLOCKING word here\nround 2 mentioned here\n"
        warnings = lib.scan_for_leaked_provenance(brief, "plan")
        self.assertTrue(any("line 2" in w and "BLOCKING" in w for w in warnings))
        self.assertTrue(any("line 3" in w and "round N" in w for w in warnings))

    def test_scan_of_ordinary_text_is_silent(self):
        """The complement of every REFUSE/WARN test above: plain text
        triggers neither."""
        self.assertEqual(
            lib.scan_for_leaked_provenance("plain text, nothing to see\n", "plan"),
            [])


class TestProvenanceScanCli(GitRepoTestCase):
    """End-to-end: the scan is wired into `claude-merge-brief`'s `main()`,
    downstream of all three builders, before either the `-o FILE` or the
    stdout path writes anything. A test that only calls
    `lib.scan_for_leaked_provenance` directly cannot kill mutation 4 (the
    scan moved into the `--plan` branch only) or 4b (only the `-o` path
    scanned) — those mutations leave the function itself correct and break
    only how `main()` calls it, so every test here goes through the real
    executable, either via `run_cli` (subprocess) or `_call_cli_main`
    (in-process, for the one mode that needs `gh` mocked)."""

    def _write_plan(self, name, text, classify=True):
        """`classify=True` (the default) appends a `Classification: bounded`
        header line, since `build_plan_brief` refuses a plan with none. No
        fixture in this class is empty or whitespace-only, so
        none needs `classify=False`."""
        if classify:
            text = text + "\n\nClassification: bounded\n"
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_heading_refuses_under_plan(self):
        plan_path = self._write_plan(
            "plan.md", "## Round 2 findings\n\nleaked answer key\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("heading", result.stderr.lower())

    def test_heading_refuses_under_pre_pr_default_stdout_path(self):
        """Mutation 4b's target (guard only -o, leave stdout unscanned):
        this is the default invocation with NO -o flag, so if the scan
        were gated on `args.output` this test goes red.

        The heading rides in the COMMIT MESSAGE BODY, not a file's
        contents: `git diff` prefixes every added line with `+`, which
        would put the heading past `HEADING`'s `^#{1,6}` anchor and prove
        nothing. `commit_messages()` renders `%b` verbatim with no such
        prefix (measured directly against `git log --format=%h %s%n%b`),
        so it is the one section a bare heading can reach unmodified."""
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("feature.txt", "hello\n",
                     message="feature work\n\n## Round 2 findings\n\nleaked answer key")

        result = run_cli(["--pre-pr", "--repo", self.work])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("heading", result.stderr.lower())

    def test_heading_refuses_under_pre_pr_o_path_writes_nothing(self):
        """The redirected path, so the -o branch is proven guarded too --
        not just proven NOT to be the only guarded one (that's the test
        above)."""
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("feature.txt", "hello\n",
                     message="feature work\n\n## Round 2 findings\n\nleaked answer key")

        out = os.path.join(self.tmp, "brief.md")
        result = run_cli(["--pre-pr", "--repo", self.work, "-o", out])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertFalse(os.path.exists(out),
                          "a brief was written despite a leaked heading")

    def test_heading_refuses_under_pr(self):
        """When every positive control invokes `--plan`, a scan wired
        into that branch alone passes them all, so this mode gets its own
        fixture. `pr_base_and_mergeable`
        is mocked (never a live `gh` call, matching this suite's own
        hermeticity rule) and `main()` runs IN-PROCESS via `_call_cli_main`
        so the mock reaches it. The heading rides in the merge commit's
        MESSAGE, which `commit_messages()` renders verbatim -- the PR brief
        carries no PR body, so a commit message is the channel left.
        """
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        merge_sha = self.commit("file.txt", "x\n",
                                message="mergeA\n\n## Round 2 findings\n\nleaked answer key")
        self.push_to_ref(merge_sha, "refs/pull/1/merge")

        cli_module = _load_cli_module()
        with mock.patch.object(lib, "pr_base_and_mergeable",
                                return_value=("main", True)):
            exit_code, out, err = _call_cli_main(
                cli_module, ["--pr", "1", "--repo", self.work])

        self.assertEqual(exit_code, 2, out + err)
        self.assertEqual(out, "")
        self.assertIn("heading", err.lower())

    def test_payload_refuses_under_plan(self):
        plan_path = self._write_plan(
            "plan.md", '{"severity": "BLOCKING", "note": "leaked"}\n')
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_payload_warns_not_refuses_under_pre_pr(self):
        """The mode split's whole point: a diff legitimately carries this
        repo's own findings-shaped fixtures, so PAYLOAD warns rather than
        refuses outside --plan."""
        self.commit("base.txt", "v1\n")
        git("push", "-q", "origin", "main", cwd=self.work)
        git("checkout", "-q", "-b", "feature", cwd=self.work)
        self.commit("finding.json", '{"severity": "BLOCKING", "note": "leaked"}\n')

        result = run_cli(["--pre-pr", "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("findings payload", result.stderr)

    def test_prose_blocking_warns_under_plan(self):
        plan_path = self._write_plan(
            "plan.md", "This finding is BLOCKING and must be fixed.\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("BLOCKING", result.stderr)

    def test_prose_round_warns_under_plan(self):
        plan_path = self._write_plan(
            "plan.md", "Let's revisit this in round 2 of the plan.\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("round N", result.stderr)

    def test_warnings_never_land_in_the_brief(self):
        """Mutation 6's target: write warnings into the brief instead of
        stderr. The plan text itself legitimately says BLOCKING (that is
        what is being warned about) -- the assertion is that the
        WARNING's own wrapper text never reaches stdout."""
        plan_path = self._write_plan(
            "plan.md", "This finding is BLOCKING and must be fixed.\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0)
        self.assertIn("warning:", result.stderr)
        self.assertNotIn("warning:", result.stdout)

    def test_positive_control_build_md_through_plan_warns_but_does_not_refuse(self):
        """Without this, the refuse-set could tighten silently and nothing
        would notice until a gate could not run. Measured on the /build
        skill: 1 `BLOCKING`, 0 prose `round N`, 0 headings, 0 payloads —
        round-N warning behaviour stays covered by
        test_prose_round_warns_under_plan. The counts are a snapshot of the
        skill; the exit-0 assertion is the property."""
        build_md = os.path.join(REPO_ROOT, "skills", "builder", "build", "SKILL.md")
        with open(build_md, encoding="utf-8") as handle:
            text = handle.read()
        plan_path = self._write_plan("build-plan.md", text)

        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(re.findall(r"prose 'BLOCKING'", result.stderr)), 1)
        self.assertEqual(len(re.findall(r"prose 'round N'", result.stderr)), 0)

    def test_zero_findings_approve_report_refuses_under_plan(self):
        """A CLEAN review report carries `"findings": []`
        and no severity/verdict at all, so the pre-fix PAYLOAD (matching
        only `severity`/`verdict`) never saw it -- it sailed through as an
        answer key saying "the previous round approved." Mutation 1's
        target: drop `recommendation` and its two enum values from PAYLOAD
        and this test goes red."""
        report = ('{"recommendation":"APPROVE","executed":{"ran":false,'
                  '"commands":[],"why_not":"none"},"findings":[]}\n')
        plan_path = self._write_plan("clean-report.json", report)
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("findings payload", result.stderr)

    def test_request_changes_report_also_refuses_under_plan(self):
        """The other enum value PAYLOAD must recognise -- a REQUEST_CHANGES
        verdict is exactly as much an answer key as an APPROVE one."""
        report = ('{"recommendation":"REQUEST_CHANGES","executed":{"ran":true,'
                  '"commands":["pytest"],"why_not":""},"findings":[]}\n')
        plan_path = self._write_plan("request-changes-report.json", report)
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")

    def test_comment_report_also_refuses_under_plan(self):
        """The schema's `recommendation`
        enum is `APPROVE | REQUEST_CHANGES | COMMENT`
        (`tools/build-lens/merge-review-report.schema.json`), and PAYLOAD
        used to enumerate only the first two -- a COMMENT report leaked
        through as a clean answer key with zero warnings. Mutation's
        target: revert the `recommendation` arm to the enumerated
        APPROVE|REQUEST_CHANGES form and this test goes red."""
        report = ('{"recommendation":"COMMENT","executed":{"ran":false,'
                  '"commands":[],"why_not":"none"},"findings":[]}\n')
        plan_path = self._write_plan("comment-report.json", report)
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn("findings payload", result.stderr)

    def test_round_n_heading_with_no_findings_vocabulary_warns_not_refuses(self):
        """`## Round 2 deployment milestones` is ordinary
        phased-plan structure, not a leaked review heading -- HEADING alone
        used to refuse it. Mutation 2's target: drop the HEADING_VOCAB
        conjunction (refuse on any round-N heading) and this test goes red."""
        plan_path = self._write_plan(
            "phased-plan.md",
            "# A plan\n\n## Round 2 deployment milestones\n\nShip the second phase.\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("round-N heading", result.stderr)

    def test_real_corpus_round_n_heading_warns_not_refuses(self):
        """A plan title of the shape real plans carry, refused by the
        pre-fix HEADING alone.
        Mutation 3's target: apply HEADING_VOCAB to the whole brief instead
        of the matched line -- "Implementation Plan" carries no vocabulary
        itself, but scanning the WHOLE document would let unrelated
        vocabulary elsewhere in the plan re-trigger the refusal, and this
        fixture's title is the only content, so that mutation cannot be
        distinguished here without the companion test below."""
        plan_path = self._write_plan(
            "usage-plan.md",
            "# Usage report round 2 (T15-T17) — Implementation Plan\n\n"
            "Do the phased work.\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("round-N heading", result.stderr)

    def test_line_scoped_vocab_does_not_leak_from_elsewhere_in_the_document(self):
        """Kills mutation 3 directly: applying HEADING_VOCAB document-wide
        instead of to the matched line alone. This plan carries a legitimate
        round-N heading with no vocabulary on its own line, PLUS the word
        "review" elsewhere in the body -- if HEADING_VOCAB is checked against
        the whole brief, the review word turns the warn into a refusal even
        though it does not sit on the heading line."""
        plan_path = self._write_plan(
            "usage-plan-with-review-word.md",
            "# Usage report round 2 (T15-T17) — Implementation Plan\n\n"
            "The team will review the deployed changes next quarter.\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("round-N heading", result.stderr)

    def test_round_n_findings_heading_still_refuses_under_plan(self):
        """The original AC, re-asserted after the fix: `## Round 2 findings`
        carries findings vocabulary on the heading line itself and must
        still refuse in every mode. Regression guard alongside
        `test_heading_refuses_under_plan` above."""
        plan_path = self._write_plan(
            "leaked.md", "## Round 2 findings\n\nleaked answer key\n")
        result = run_cli(["--plan", plan_path, "--repo", self.work])
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "")


class TestRunsThroughASymlink(unittest.TestCase):
    """`install.sh` links the executable into `~/.local/bin`, so it runs as
    a symlink whose own directory holds no `merge_brief_lib` and no
    `repo-freshness`. Every other test runs the script in place, so this is
    the one that proves the installed shape runs at all."""

    def test_help_runs_from_a_symlink_in_another_directory(self):
        with tempfile.TemporaryDirectory() as bin_dir:
            link = os.path.join(bin_dir, "claude-merge-brief")
            os.symlink(CLI, link)
            result = subprocess.run([sys.executable, link, "--help"],
                                    cwd=bin_dir, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--pre-pr", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
