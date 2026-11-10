#!/usr/bin/env python3
"""Behavioural tests for git-tidy.

The old `~/.gitconfig` alias started with `git checkout "$def"` — silently
yanking a checkout off whatever feature branch it was standing on. git-tidy
fast-forwards the default branch via `git fetch origin <def>:<def>` instead,
which updates that branch's ref directly without ever touching HEAD.

These build real temp repos with a real remote (a bare repo) and real
worktrees, and assert on actual SHAs and actual current branches — not just
exit codes. `git fetch . <def>:<def>` (fetching from the LOCAL repo instead of
`origin`) also exits 0 and prints nothing alarming, but updates nothing; an
exit-code-only assertion would pass against that broken form, so every
fast-forward case here checks the branch's SHA actually moved.
"""

import os
import json
import shutil
import subprocess
import sys
import tempfile
import unittest

GIT_TIDY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "git-tidy")
TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True)


def git_out(*args, cwd):
    return git(*args, cwd=cwd).stdout.strip()


def sha_of(ref, cwd):
    return git_out("rev-parse", ref, cwd=cwd)


def run_tidy(cwd, env=None):
    """Run git-tidy in `cwd`. `env` is merged over the ambient environment."""
    full_env = dict(os.environ, **env) if env else None
    return subprocess.run([GIT_TIDY], cwd=cwd, capture_output=True, text=True,
                          env=full_env)


def commit_on_branch(root, parent, branch, filename, push):
    """Commit `filename` on `branch` WITHOUT moving the parent clone's HEAD.

    Works in a short-lived side worktree, so the parent stays on whatever branch
    it was standing on. Creates the branch from `main` when it does not exist.
    Returns the new tip sha.
    """
    side = os.path.join(root, "side-%s" % branch)
    exists = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", "refs/heads/" + branch],
        cwd=parent, capture_output=True).returncode == 0
    if exists:
        git("worktree", "add", "-q", side, branch, cwd=parent)
    else:
        git("worktree", "add", "-q", side, "-b", branch, "main", cwd=parent)
    with open(os.path.join(side, filename), "w") as handle:
        handle.write("%s\n" % filename)
    git("add", "-A", cwd=side)
    git("commit", "-q", "-m", filename, cwd=side)
    if push:
        git("push", "-q", "-u", "origin", branch, cwd=side)
    tip = sha_of("HEAD", cwd=side)
    git("worktree", "remove", "--force", side, cwd=parent)
    return tip


def reported_branches(stdout):
    """Just the branch names listed under the no-upstream report.

    Asserting on the whole of stdout cannot answer 'was main reported' — the
    word appears in fast-forward output and in 'Deleted branch' lines too. The
    report body is the indented run directly after the header; the trailing
    advice lines are prefixed 'git tidy:' and end it.
    """
    names, in_report = [], False
    for line in stdout.splitlines():
        if "have no upstream" in line:
            in_report = True
            continue
        if in_report:
            if line.startswith("  ") and not line.startswith("git tidy:"):
                names.append(line.strip())
            else:
                break
    return names


def make_remote_and_clone(root, clone_name="parent"):
    """A bare remote with one commit on main, and a clone of it (origin/HEAD
    set up the same way a real `git clone` sets it, so default-branch
    detection exercises the real code path, not the `main` fallback)."""
    remote = os.path.join(root, "remote.git")
    clone = os.path.join(root, clone_name)
    # -b main, explicitly: without it the branch name comes from the ambient
    # init.defaultBranch, which scripts/setup-git.sh sets to main on our
    # machines and which is `master` anywhere else. Every push below names
    # `main`, so on a fresh checkout this fixture failed and the suite passed
    # only on a machine this toolkit had already configured — exactly the
    # hermeticity trap CLAUDE.md warns about. Every other suite in this repo
    # already passes -b main; this was the one that did not.
    git("init", "-q", "--bare", "-b", "main", remote, cwd=root)
    git("clone", "-q", remote, clone, cwd=root)
    git("config", "user.email", "t@example.com", cwd=clone)
    git("config", "user.name", "Test", cwd=clone)
    with open(os.path.join(clone, "f.txt"), "w") as handle:
        handle.write("hi\n")
    git("add", "-A", cwd=clone)
    git("commit", "-q", "-m", "init", cwd=clone)
    git("push", "-q", "origin", "main", cwd=clone)
    return remote, clone


def advance_origin_main(remote, root, name="updater"):
    """Push a new commit to origin/main from an independent clone, so `main`
    in the fixture under test is genuinely behind (or diverges from) it."""
    updater = os.path.join(root, name)
    git("clone", "-q", remote, updater, cwd=root)
    git("config", "user.email", "t@example.com", cwd=updater)
    git("config", "user.name", "Test", cwd=updater)
    with open(os.path.join(updater, "f.txt"), "a") as handle:
        handle.write("remote advance\n")
    git("commit", "-q", "-am", "remote advance", cwd=updater)
    git("push", "-q", "origin", "main", cwd=updater)
    return updater


class TempRepoTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


class TestRegressionParentStaysOnItsBranch(TempRepoTestCase):
    """The exact bug this task fixes, stated directly."""

    def test_parent_on_feature_stays_on_feature_with_worktree_elsewhere(self):
        remote, parent = make_remote_and_clone(self.root)
        git("checkout", "-q", "-b", "feature", cwd=parent)
        # A second worktree on its own branch — the concurrency this whole
        # guard/tidy pairing exists to protect.
        other = os.path.join(self.root, "other")
        git("worktree", "add", "-q", other, "-b", "other-branch", cwd=parent)
        advance_origin_main(remote, self.root)

        proc = run_tidy(parent)

        self.assertEqual(git_out("branch", "--show-current", cwd=parent), "feature")
        self.assertEqual(git_out("rev-parse", "HEAD", cwd=other),
                         git_out("rev-parse", "other-branch", cwd=parent))
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestFastForward(TempRepoTestCase):

    def test_from_feature_branch_moves_default_branch_sha_to_match_origin(self):
        remote, parent = make_remote_and_clone(self.root)
        git("checkout", "-q", "-b", "feature", cwd=parent)
        advance_origin_main(remote, self.root)
        before = sha_of("main", cwd=parent)

        proc = run_tidy(parent)

        after = sha_of("main", cwd=parent)
        origin_sha = sha_of("origin/main", cwd=parent)
        self.assertNotEqual(before, after, "main did not move at all")
        self.assertEqual(after, origin_sha, "main did not reach origin/main")
        self.assertEqual(git_out("branch", "--show-current", cwd=parent), "feature")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_already_on_default_branch_still_fast_forwards(self):
        remote, parent = make_remote_and_clone(self.root)
        advance_origin_main(remote, self.root)
        before = sha_of("main", cwd=parent)

        proc = run_tidy(parent)

        after = sha_of("main", cwd=parent)
        origin_sha = sha_of("origin/main", cwd=parent)
        self.assertNotEqual(before, after)
        self.assertEqual(after, origin_sha)
        self.assertEqual(git_out("branch", "--show-current", cwd=parent), "main")
        self.assertEqual(proc.returncode, 0, proc.stderr)


class TestDivergedDefaultBranch(TempRepoTestCase):

    def test_off_default_branch_warns_leaves_sha_unchanged_still_cleans_gone(self):
        remote, parent = make_remote_and_clone(self.root)
        git("checkout", "-q", "-b", "feature", cwd=parent)
        updater = advance_origin_main(remote, self.root)
        # Diverge local `main` from origin/main WITHOUT checking it out (the
        # current checkout is on `feature`) — fetch a commit that was never
        # pushed and force-point main at it directly.
        with open(os.path.join(updater, "f.txt"), "a") as handle:
            handle.write("divergent, not pushed\n")
        git("commit", "-q", "-am", "divergent, not pushed", cwd=updater)
        diverged_sha = sha_of("HEAD", cwd=updater)
        git("fetch", "-q", updater, "HEAD:refs/tmp/diverged", cwd=parent)
        git("branch", "-f", "main", diverged_sha, cwd=parent)
        git("update-ref", "-d", "refs/tmp/diverged", cwd=parent)

        # A [gone] branch to prove cleanup still runs on the warn path.
        # Created from origin/main, not local main: local main now points at an
        # unpublished commit, and a branch there is correctly KEPT (nothing
        # proves it merged). origin/main is provably merged, so this still
        # proves cleanup runs on the diverged-warning path.
        git("branch", "stale", "origin/main", cwd=parent)
        git("push", "-q", "origin", "stale", cwd=parent)
        git("branch", "--set-upstream-to=origin/stale", "stale", cwd=parent)
        git("push", "-q", "origin", "--delete", "stale", cwd=parent)

        proc = run_tidy(parent)

        self.assertEqual(sha_of("main", cwd=parent), diverged_sha,
                         "main moved despite having diverged")
        self.assertIn("diverged", proc.stderr)
        self.assertEqual(git_out("branch", "--show-current", cwd=parent), "feature")
        result = subprocess.run(["git", "branch", "--list", "stale"], cwd=parent,
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "", "stale [gone] branch was not cleaned up")


class TestDefaultBranchHeldByAnotherWorktree(TempRepoTestCase):

    def test_warns_with_the_right_message_and_moves_nothing(self):
        remote, parent = make_remote_and_clone(self.root)
        # `main` can only be checked out into the second worktree once the
        # parent has moved off it — git refuses to let two checkouts hold
        # the same branch at once, same as it would for a real peer session.
        git("checkout", "-q", "-b", "feature", cwd=parent)
        mainwt = os.path.join(self.root, "mainwt")
        git("worktree", "add", "-q", mainwt, "main", cwd=parent)
        advance_origin_main(remote, self.root)
        before = sha_of("main", cwd=parent)

        proc = run_tidy(parent)

        self.assertEqual(sha_of("main", cwd=parent), before,
                         "main moved even though another worktree holds it")
        self.assertIn("checked out in another worktree", proc.stderr)
        self.assertNotIn("diverged", proc.stderr)
        self.assertEqual(git_out("branch", "--show-current", cwd=parent), "feature")
        self.assertEqual(sha_of("HEAD", cwd=mainwt), before,
                         "the other worktree's HEAD moved")


class TestDirtyWorkingTree(TempRepoTestCase):
    """F1: the dirty-tree gate only protects the path that actually touches
    the working tree — `git merge --ff-only`, on the default branch. Off the
    default branch, nothing this script does moves a file, so a dirty tree
    there must not block it."""

    def test_dirty_on_default_branch_refuses_and_changes_nothing(self):
        remote, parent = make_remote_and_clone(self.root)
        advance_origin_main(remote, self.root)
        before = sha_of("main", cwd=parent)
        with open(os.path.join(parent, "f.txt"), "a") as handle:
            handle.write("uncommitted\n")

        proc = run_tidy(parent)

        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(sha_of("main", cwd=parent), before, "main moved on a dirty tree")
        status = git_out("status", "--porcelain", cwd=parent)
        self.assertIn("f.txt", status, "the uncommitted change was touched")

    def test_dirty_off_default_branch_proceeds_and_leaves_the_change_alone(self):
        remote, parent = make_remote_and_clone(self.root)
        git("checkout", "-q", "-b", "feature", cwd=parent)
        advance_origin_main(remote, self.root)
        with open(os.path.join(parent, "f.txt"), "a") as handle:
            handle.write("uncommitted\n")

        # A [gone] branch, to prove cleanup runs on this path too.
        git("branch", "stale", "main", cwd=parent)
        git("push", "-q", "origin", "stale", cwd=parent)
        git("branch", "--set-upstream-to=origin/stale", "stale", cwd=parent)
        git("push", "-q", "origin", "--delete", "stale", cwd=parent)

        proc = run_tidy(parent)

        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The default branch still fast-forwarded...
        self.assertEqual(sha_of("main", cwd=parent), sha_of("origin/main", cwd=parent),
                         "main did not fast-forward while the tree was dirty")
        # ...the uncommitted change is still there, and untouched...
        status = git_out("status", "--porcelain", cwd=parent)
        self.assertIn("f.txt", status, "the uncommitted change was discarded")
        with open(os.path.join(parent, "f.txt")) as handle:
            self.assertTrue(handle.read().endswith("uncommitted\n"),
                            "the uncommitted change was modified")
        # ...and [gone] cleanup still ran.
        result = subprocess.run(["git", "branch", "--list", "stale"], cwd=parent,
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "", "stale [gone] branch was not cleaned up")


class TestOutsideARepo(TempRepoTestCase):
    """M6: outside a repo, git-tidy used to emit three git errors plus a
    spurious "could not update <def>" warning before exiting 0. A guard at
    the top makes it one clean line and a non-zero exit."""

    def test_plain_directory_fails_fast_with_one_message(self):
        plain = os.path.join(self.root, "plain")
        os.makedirs(plain)

        proc = run_tidy(plain)

        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("not a git repository", proc.stderr)
        self.assertNotIn("could not update", proc.stderr)


class TestGoneBranchCleanup(TempRepoTestCase):

    def test_gone_branch_is_deleted_and_worktree_held_branch_survives(self):
        remote, parent = make_remote_and_clone(self.root)

        git("branch", "gone-branch", "main", cwd=parent)
        git("push", "-q", "origin", "gone-branch", cwd=parent)
        git("branch", "--set-upstream-to=origin/gone-branch", "gone-branch", cwd=parent)

        held = os.path.join(self.root, "held")
        git("branch", "gone-but-held", "main", cwd=parent)
        git("push", "-q", "origin", "gone-but-held", cwd=parent)
        git("branch", "--set-upstream-to=origin/gone-but-held", "gone-but-held", cwd=parent)
        git("worktree", "add", "-q", held, "gone-but-held", cwd=parent)

        git("push", "-q", "origin", "--delete", "gone-branch", cwd=parent)
        git("push", "-q", "origin", "--delete", "gone-but-held", cwd=parent)

        proc = run_tidy(parent)

        result = subprocess.run(["git", "branch", "--list", "gone-branch"], cwd=parent,
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "", "[gone] branch was not deleted")

        result = subprocess.run(["git", "branch", "--list", "gone-but-held"], cwd=parent,
                                capture_output=True, text=True)
        self.assertNotEqual(result.stdout.strip(), "",
                            "branch held by another worktree was deleted anyway")

        # The two assertions above were already true before the output-collapsing
        # change in Step 4 — deletion behaviour didn't change, only what gets
        # printed. A behaviour-only test can't catch a regression in the OUTPUT,
        # so assert on it directly: the summary line must appear, git's raw
        # "used by worktree" error must not leak to stderr, and the ordinary
        # success message for the branch that WAS deleted must still print.
        self.assertIn("1 branch(es) skipped - held by worktrees", proc.stdout)
        self.assertNotIn("used by worktree", proc.stderr)
        self.assertIn("Deleted branch", proc.stdout)


class TestGoneBranchNeedsProof(TempRepoTestCase):
    """A gone upstream is not proof of a merge: a remote branch is also deleted
    when a PR closes unmerged, or by hand. A [gone] branch is deleted only when
    its tip is in origin/<default> or in a merged PR's head; anything else is
    kept and listed."""

    def write_prs(self, records):
        path = os.path.join(self.root, "prs.json")
        with open(path, "w") as handle:
            json.dump(records, handle)
        return {"GIT_TIDY_PR_DATA": path}

    def delete_remote(self, parent, branch):
        git("push", "-q", "origin", "--delete", branch, cwd=parent)

    def branch_exists(self, parent, branch):
        return subprocess.run(["git", "rev-parse", "--verify", "-q",
                               "refs/heads/" + branch], cwd=parent,
                              capture_output=True).returncode == 0

    def test_gone_branch_with_unpublished_commit_survives(self):
        remote, parent = make_remote_and_clone(self.root)
        commit_on_branch(self.root, parent, "gone-wip", "a.txt", push=True)
        unpublished = commit_on_branch(self.root, parent, "gone-wip", "b.txt",
                                       push=False)
        self.delete_remote(parent, "gone-wip")

        proc = run_tidy(parent)

        self.assertTrue(self.branch_exists(parent, "gone-wip"),
                        "a branch holding an unpublished commit was deleted")
        self.assertEqual(sha_of("gone-wip", cwd=parent), unpublished)
        self.assertIn("were kept - nothing shows they merged", proc.stdout)
        self.assertIn("gone-wip", proc.stdout)
        self.assertEqual(git_out("branch", "--show-current", cwd=parent), "main")

    def test_squash_merged_gone_branch_is_deleted(self):
        remote, parent = make_remote_and_clone(self.root)
        tip = commit_on_branch(self.root, parent, "gone-squash", "s.txt", push=True)
        self.delete_remote(parent, "gone-squash")
        env = self.write_prs([{"headRefName": "gone-squash", "headRefOid": tip,
                               "number": 41}])

        proc = run_tidy(parent, env=env)

        self.assertFalse(self.branch_exists(parent, "gone-squash"),
                         "a branch identical to its merged PR was not deleted")
        self.assertIn("Deleted branch gone-squash", proc.stdout)

    def test_gone_branch_behind_its_merged_pr_is_deleted(self):
        remote, parent = make_remote_and_clone(self.root)
        first = commit_on_branch(self.root, parent, "gone-behind", "a.txt", push=True)
        second = commit_on_branch(self.root, parent, "gone-behind", "b.txt", push=True)
        git("branch", "-f", "gone-behind", first, cwd=parent)
        self.delete_remote(parent, "gone-behind")
        env = self.write_prs([{"headRefName": "gone-behind", "headRefOid": second,
                               "number": 42}])

        proc = run_tidy(parent, env=env)

        self.assertFalse(self.branch_exists(parent, "gone-behind"),
                         "a branch contained in its merged PR was not deleted")
        self.assertIn("Deleted branch gone-behind", proc.stdout)

    def test_gone_branch_kept_when_pr_head_differs(self):
        remote, parent = make_remote_and_clone(self.root)
        pr_head = commit_on_branch(self.root, parent, "gone-extra", "p.txt", push=True)
        commit_on_branch(self.root, parent, "gone-extra", "q.txt", push=False)
        self.delete_remote(parent, "gone-extra")
        env = self.write_prs([{"headRefName": "gone-extra", "headRefOid": pr_head,
                               "number": 41}])

        proc = run_tidy(parent, env=env)

        self.assertTrue(self.branch_exists(parent, "gone-extra"),
                        "commits landed after the merge and the branch was deleted")
        self.assertIn("1 commit(s) not in merged #41", proc.stdout)

    def test_missing_helper_keeps_gone_branches(self):
        remote, parent = make_remote_and_clone(self.root)
        # At main's tip, so the ancestry proof WOULD apply if the helper ran.
        git("branch", "gone-merged", "main", cwd=parent)
        git("push", "-q", "-u", "origin", "gone-merged", cwd=parent)
        self.delete_remote(parent, "gone-merged")
        lone = os.path.join(self.root, "lone")
        os.makedirs(lone)
        shutil.copy(GIT_TIDY, os.path.join(lone, "git-tidy"))

        proc = subprocess.run([os.path.join(lone, "git-tidy")], cwd=parent,
                              capture_output=True, text=True)

        self.assertTrue(self.branch_exists(parent, "gone-merged"),
                        "a branch was deleted with no helper to prove it merged")
        self.assertIn("no [gone] branch was deleted", proc.stderr)


class TestUntrackedBranchReporting(TempRepoTestCase):
    """A branch with NO upstream configuration is structurally invisible to the
    [gone] check: `%(upstream:track)` is empty for it, never the literal
    '[gone]', so it survives every tidy forever.

    That is not a corner case here — `git worktree add -b <name>` creates a
    branch with no tracking config, and this repo's CLAUDE.md prescribes exactly
    that for concurrent work. The prescribed workflow produces branches the
    prescribed cleanup tool cannot see.

    These branches are REPORTED, never deleted. Without an upstream there is no
    signal that separates 'squash-merged and finished' from 'never pushed, still
    being written' — and a wrong delete cannot be undone.
    """

    def test_untracked_branch_is_reported_by_name_and_not_deleted(self):
        remote, parent = make_remote_and_clone(self.root)
        git("checkout", "-q", "-b", "work-current", cwd=parent)
        # No push, no --set-upstream-to: exactly what `git worktree add -b` leaves.
        git("branch", "loose-orphan", "main", cwd=parent)

        proc = run_tidy(parent)

        result = subprocess.run(["git", "branch", "--list", "loose-orphan"], cwd=parent,
                                capture_output=True, text=True)
        self.assertNotEqual(result.stdout.strip(), "",
                            "an untracked branch was DELETED - it must only be reported")
        self.assertIn("loose-orphan", proc.stdout,
                      "the untracked branch was not reported, so it stays invisible")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_untracked_branch_held_by_a_worktree_is_not_reported(self):
        """A worktree still holding the branch means work in progress. Naming it
        every run is noise, and noise is what gets a report ignored."""
        remote, parent = make_remote_and_clone(self.root)
        git("checkout", "-q", "-b", "work-current", cwd=parent)
        held = os.path.join(self.root, "held")
        git("branch", "held-orphan", "main", cwd=parent)
        git("worktree", "add", "-q", held, "held-orphan", cwd=parent)

        proc = run_tidy(parent)

        self.assertNotIn("held-orphan", proc.stdout,
                         "a branch an active worktree holds was reported as untracked")

    def test_current_branch_is_never_reported(self):
        """`work-current` has no upstream either — it was just created. Reporting
        the branch someone is standing on is pure noise.

        Named for the current branch only. It used to claim the default branch
        too, which it never reached: the fixture clones, so `main` carries
        tracking config and never enters the loop at all. The default-branch
        skip has its own test below.
        """
        remote, parent = make_remote_and_clone(self.root)
        git("checkout", "-q", "-b", "work-current", cwd=parent)

        proc = run_tidy(parent)

        self.assertNotIn("work-current", proc.stdout,
                         "the checked-out branch was reported as untracked")
        self.assertNotIn("no upstream", proc.stdout,
                         "reported an untracked branch when there was none to report")

    def test_an_untracked_default_branch_is_never_reported(self):
        """The `$def` skip is reachable, not defensive — git-tidy creates this
        state itself.

        `git fetch origin <def>:<def>`, the fast-forward path taken whenever you
        run this from a feature branch, creates a local default branch with NO
        tracking config. Confirmed by hand: delete local `main`, fetch it back
        that way, and `branch.main.merge` is unset.

        Without the skip, `main` would be listed under "have no upstream - left
        alone" next to the advice `git branch -D <name>` — pointing the reader
        at the one branch they must not delete. Report-only means that costs a
        line rather than a branch, which is exactly why it would go unnoticed.
        """
        remote, parent = make_remote_and_clone(self.root)
        git("checkout", "-q", "-b", "work-current", cwd=parent)
        # The state the fetch above leaves behind, reached directly.
        git("branch", "--unset-upstream", "main", cwd=parent)
        # A branch that SHOULD be reported, so this cannot pass by the report
        # being empty for some unrelated reason.
        git("branch", "loose-orphan", "main", cwd=parent)

        proc = run_tidy(parent)

        reported = reported_branches(proc.stdout)
        self.assertIn("loose-orphan", reported,
                      "the report did not run at all, so this proves nothing")
        self.assertNotIn("main", reported,
                         "the default branch was offered up for deletion")

    def test_gone_branches_are_still_deleted_alongside_the_report(self):
        """The report must not cannibalise the deletion path: a [gone] branch and
        an untracked branch in the same repo get different treatment."""
        remote, parent = make_remote_and_clone(self.root)
        git("checkout", "-q", "-b", "work-current", cwd=parent)

        git("branch", "gone-branch", "main", cwd=parent)
        git("push", "-q", "origin", "gone-branch", cwd=parent)
        git("branch", "--set-upstream-to=origin/gone-branch", "gone-branch", cwd=parent)
        git("push", "-q", "origin", "--delete", "gone-branch", cwd=parent)

        git("branch", "loose-orphan", "main", cwd=parent)

        proc = run_tidy(parent)

        result = subprocess.run(["git", "branch", "--list", "gone-branch"], cwd=parent,
                                capture_output=True, text=True)
        self.assertEqual(result.stdout.strip(), "",
                         "[gone] branch was not deleted once reporting was added")
        result = subprocess.run(["git", "branch", "--list", "loose-orphan"], cwd=parent,
                                capture_output=True, text=True)
        self.assertNotEqual(result.stdout.strip(), "",
                            "untracked branch was deleted by the [gone] path")
        self.assertIn("loose-orphan", proc.stdout)


class TestListWorktreesNonUtf8Locale(TempRepoTestCase):
    """`list_worktrees` decodes git's output as UTF-8 whatever the locale is."""

    # Python reads the locale at start-up, so the code under test runs in a child.
    NON_UTF8 = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"}
    CODE = ("import json, sys; sys.path.insert(0, %r); import worktree_lib; "
            "print(json.dumps(worktree_lib.list_worktrees(sys.argv[1])))" % TOOL_DIR)

    def test_list_worktrees_keeps_a_non_ascii_path_under_a_non_utf8_locale(self):
        env = dict(os.environ, **self.NON_UTF8)
        env.pop("PYTHONIOENCODING", None)
        probe = subprocess.run(
            [sys.executable, "-c",
             "import locale; print(locale.getpreferredencoding(False))"],
            env=env, stdout=subprocess.PIPE, universal_newlines=True, check=True)
        if probe.stdout.strip().lower().replace("-", "") == "utf8":
            self.skipTest("could not force a non-UTF-8 locale here")
        repo = os.path.join(self.root, "repo")
        git("init", "-q", "-b", "main", repo, cwd=self.root)
        git("config", "user.email", "t@example.com", cwd=repo)
        git("config", "user.name", "Test", cwd=repo)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
        worktree = os.path.join(self.root, "wt-Jos\u00e9")
        git("worktree", "add", "-q", worktree, "-b", "b1", cwd=repo)
        result = subprocess.run([sys.executable, "-c", self.CODE, repo], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=True)
        entries = json.loads(result.stdout.decode("ascii"))
        self.assertEqual(len(entries), 1, entries)
        path, branch = entries[0]
        self.assertEqual(os.path.realpath(path), os.path.realpath(worktree))
        self.assertEqual(branch, "b1")


if __name__ == "__main__":
    unittest.main(verbosity=1)
