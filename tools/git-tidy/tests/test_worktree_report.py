#!/usr/bin/env python3
"""Behavioural tests for git-tidy's worktree report.

These build real repos with a real bare remote and real worktrees, and assert
on what the report says about each one. The pull-request data is injected
through GIT_TIDY_PR_DATA so nothing here touches the network, and the claims
file through GIT_TIDY_CLAIMS_PATH so nothing reads the machine's real sessions.

The case that matters most is `test_commits_after_merge_hold_work`. A branch
whose PR merged and which then gained a commit is indistinguishable from a
finished one by every purely-local test, and two real worktrees in this
workspace were in exactly that state while an audit listed them as safe to
delete. If that test ever goes green for the wrong reason the report becomes a
recommendation to lose work.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, TOOL_DIR)
import worktree_lib as lib
import worktree_report as report

GIT_TIDY = os.path.join(TOOL_DIR, "git-tidy")

# Bodies for a shimmed `ps`. PS_DEAD is the measured macOS shape for a pid that
# does not exist: one header line on stdout, exit 1, nothing on stderr.
PS_ALIVE = 'echo "  PID TTY           TIME CMD"; echo "$2 ttys000 0:00.01 x"; exit 0'
PS_DEAD = 'echo "  PID TTY           TIME CMD"; exit 1'
PS_REFUSED = 'echo "ps: operation not permitted" >&2; exit 1'


def git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class WorktreeFixture(unittest.TestCase):
    """A repo with an origin, a default branch, and a place to add worktrees."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = os.path.join(self.tmp, "remote.git")
        self.repo = os.path.join(self.tmp, "repo")
        git("init", "--bare", "-b", "main", self.remote, cwd=self.tmp)
        git("clone", self.remote, self.repo, cwd=self.tmp)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T"),
                           ("commit.gpgsign", "false")):
            git("config", key, value, cwd=self.repo)
        self.write("README.md", "hello\n")
        git("add", "-A", cwd=self.repo)
        git("commit", "-m", "init", cwd=self.repo)
        git("push", "-u", "origin", "main", cwd=self.repo)
        # `git tidy` resolves the default branch from origin/HEAD.
        git("remote", "set-head", "origin", "main", cwd=self.repo)
        self.claims_path = os.path.join(self.tmp, "claims.json")
        self.set_claims([])

    def write(self, name, text, cwd=None):
        path = os.path.join(cwd or self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
        with open(path, "w") as handle:
            handle.write(text)

    def add_worktree(self, name, branch):
        path = os.path.join(self.repo, ".wt", name)
        git("worktree", "add", "-b", branch, path, "main", cwd=self.repo)
        # realpath because `git worktree list` resolves symlinks, and on macOS
        # the whole temp tree lives under /var -> /private/var. Comparing the
        # unresolved path fails on the fixture, not on the report.
        return os.path.realpath(path)

    def commit_in(self, path, name, text, message):
        self.write(name, text, cwd=path)
        git("add", "-A", cwd=path)
        git("commit", "-m", message, cwd=path)
        return git("rev-parse", "HEAD", cwd=path)

    def set_prs(self, records):
        path = os.path.join(self.tmp, "prs.json")
        with open(path, "w") as handle:
            json.dump(records, handle)
        os.environ["GIT_TIDY_PR_DATA"] = path

    def set_claims(self, records):
        with open(self.claims_path, "w") as handle:
            json.dump(records, handle)
        os.environ["GIT_TIDY_CLAIMS_PATH"] = self.claims_path

    def shim_ps(self, script_body):
        """Put a fake `ps` first on PATH for this test.

        Restricted sandboxes deny `ps` outright, so a test that leaned on the
        real one would assert against ambient state. The shim makes liveness an
        input the test controls.
        """
        shim_dir = os.path.join(self.tmp, "shim")
        os.makedirs(shim_dir, exist_ok=True)
        shim = os.path.join(shim_dir, "ps")
        with open(shim, "w") as handle:
            handle.write("#!/bin/sh\n%s\n" % script_body)
        os.chmod(shim, 0o755)
        original = os.environ.get("PATH", "")
        os.environ["PATH"] = shim_dir + os.pathsep + original
        self.addCleanup(os.environ.__setitem__, "PATH", original)

    def run_report(self, cwd=None):
        import io
        buf = io.StringIO()
        report.report(cwd or self.repo, out=buf)
        return buf.getvalue()

    def tearDown(self):
        for key in ("GIT_TIDY_PR_DATA", "GIT_TIDY_CLAIMS_PATH"):
            os.environ.pop(key, None)


class TestClassification(WorktreeFixture):

    def test_branch_identical_to_merged_pr_is_removable(self):
        path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        out = self.run_report()
        self.assertIn("safe to remove", out)
        self.assertIn("identical to merged #7", out)
        self.assertIn("git worktree remove %s" % path, out)

    def test_commits_after_merge_hold_work(self):
        """The case both real audits got wrong. Must never read as removable."""
        path = self.add_worktree("kept", "feat/kept")
        merged = self.commit_in(path, "a.txt", "a\n", "shipped work")
        self.commit_in(path, "b.txt", "b\n", "added after the merge")
        self.set_prs([{"headRefName": "feat/kept", "headRefOid": merged,
                       "number": 9}])
        out = self.run_report()
        self.assertIn("hold work", out)
        self.assertIn("1 commit(s) added after #9 merged", out)
        self.assertNotIn("git worktree remove %s" % path, out)

    def test_uncommitted_changes_hold_work(self):
        path = self.add_worktree("dirty", "feat/dirty")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.write("a.txt", "edited\n", cwd=path)
        self.set_prs([{"headRefName": "feat/dirty", "headRefOid": tip,
                       "number": 11}])
        out = self.run_report()
        self.assertIn("1 uncommitted change(s)", out)
        self.assertNotIn("git worktree remove %s" % path, out)

    def test_untracked_file_holds_work(self):
        """An untracked file is unique content too -- git would not keep it."""
        path = self.add_worktree("untracked", "feat/untracked")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.write("scratch.txt", "notes\n", cwd=path)
        self.set_prs([{"headRefName": "feat/untracked", "headRefOid": tip,
                       "number": 12}])
        out = self.run_report()
        self.assertIn("uncommitted change(s)", out)
        self.assertNotIn("git worktree remove %s" % path, out)

    def test_unused_branch_at_default_is_reported_but_not_removable(self):
        path = self.add_worktree("fresh", "feat/fresh")
        self.set_prs([])
        out = self.run_report()
        self.assertIn("nothing ahead of main", out)
        self.assertIn("unused - no PR and nothing ahead", out)
        self.assertNotIn("safe to remove", out)
        self.assertNotIn("git worktree remove %s" % path, out)

    def test_unmerged_commits_without_a_pr_hold_work(self):
        path = self.add_worktree("wip", "feat/wip")
        self.commit_in(path, "a.txt", "a\n", "unpushed work")
        self.set_prs([])
        out = self.run_report()
        self.assertIn("no merged PR, 1 commit(s) ahead of main", out)
        self.assertNotIn("git worktree remove %s" % path, out)

    def test_missing_pr_data_reports_unknown_and_suggests_nothing(self):
        path = self.add_worktree("opaque", "feat/opaque")
        self.commit_in(path, "a.txt", "a\n", "work")
        os.environ["GIT_TIDY_PR_DATA"] = os.path.join(self.tmp, "absent.json")
        out = self.run_report()
        self.assertIn("could not be classified", out)
        self.assertNotIn("safe to remove", out)
        self.assertNotIn("git worktree remove", out)


class TestExclusions(WorktreeFixture):

    def test_main_working_tree_is_never_listed(self):
        self.add_worktree("shipped", "feat/shipped")
        self.set_prs([])
        out = self.run_report()
        self.assertNotIn("git worktree remove %s\n" % self.repo, out)

    def test_current_worktree_is_never_suggested(self):
        path = self.add_worktree("here", "feat/here")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/here", "headRefOid": tip,
                       "number": 3}])
        # Standing in it: it holds nothing, but removing it is not advice.
        out = self.run_report(cwd=path)
        self.assertNotIn("git worktree remove %s" % path, out)
        # Standing elsewhere: the same worktree IS offered.
        self.assertIn("git worktree remove %s" % path, self.run_report())

    def test_live_claim_is_left_alone(self):
        path = self.add_worktree("busy", "feat/busy")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/busy", "headRefOid": tip,
                       "number": 5}])
        self.shim_ps(PS_ALIVE)
        self.set_claims([{"checkout": path, "pid": os.getpid(),
                          "name": "session-x"}])
        out = self.run_report()
        self.assertIn("in use by a live session", out)
        self.assertNotIn("git worktree remove %s" % path, out)

    def test_stale_claim_warns_but_still_offers_removal(self):
        path = self.add_worktree("paused", "feat/paused")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/paused", "headRefOid": tip,
                       "number": 6}])
        # The shim is what makes this claim dead; the real `ps` is not consulted.
        self.shim_ps(PS_DEAD)
        self.set_claims([{"checkout": path, "pid": 999999999,
                          "name": "session-y"}])
        out = self.run_report()
        self.assertIn("breaks that session's cwd", out)
        self.assertIn("git worktree remove %s" % path, out)


class TestDispositions(WorktreeFixture):
    """What a thread declared, overlaid on what the repository shows.

    The invariant under test is one-directional: a declaration may make the
    report more conservative and never less. Reversing that turns /handover
    into a way to talk git tidy into recommending a delete.
    """

    def set_disposition(self, path, disposition, reason="", recorded_at=None):
        from datetime import datetime
        store = os.path.join(self.tmp, "dispositions.json")
        payload = {os.path.realpath(path): {
            "disposition": disposition, "reason": reason,
            "recorded_at": recorded_at or datetime.now().isoformat(timespec="seconds")}}
        with open(store, "w") as handle:
            json.dump(payload, handle)
        os.environ["GIT_TIDY_DISPOSITIONS_PATH"] = store

    def tearDown(self):
        os.environ.pop("GIT_TIDY_DISPOSITIONS_PATH", None)
        super().tearDown()

    def test_hold_protects_a_worktree_that_holds_nothing(self):
        """The paused-session case: no data at stake, but a cwd that resumes."""
        path = self.add_worktree("paused", "feat/paused")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/paused", "headRefOid": tip,
                       "number": 8}])
        self.assertIn("git worktree remove %s" % path, self.run_report())
        self.set_disposition(path, "hold", "waiting on the Data Desk ship")
        out = self.run_report()
        self.assertIn("held deliberately: waiting on the Data Desk ship", out)
        self.assertNotIn("git worktree remove %s" % path, out)

    def test_retire_cannot_override_real_unmerged_work(self):
        """A declaration must never talk the report into losing commits."""
        path = self.add_worktree("wrong", "feat/wrong")
        merged = self.commit_in(path, "a.txt", "a\n", "shipped")
        self.commit_in(path, "b.txt", "b\n", "landed after")
        self.set_prs([{"headRefName": "feat/wrong", "headRefOid": merged,
                       "number": 14}])
        self.set_disposition(path, "retire")
        out = self.run_report()
        self.assertNotIn("git worktree remove %s" % path, out)
        self.assertIn("the repository disagrees", out)

    def test_retire_annotates_an_already_removable_worktree(self):
        path = self.add_worktree("done", "feat/done")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/done", "headRefOid": tip,
                       "number": 15}])
        self.set_disposition(path, "retire")
        out = self.run_report()
        self.assertIn("marked it retired", out)
        self.assertIn("git worktree remove %s" % path, out)

    def test_hold_reports_its_age(self):
        path = self.add_worktree("stale", "feat/stale")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/stale", "headRefOid": tip,
                       "number": 16}])
        self.set_disposition(path, "hold", "paused", recorded_at="2020-01-01T00:00:00")
        self.assertIn("day(s) ago", self.run_report())

    # --- a hold REASON is a snapshot, and nothing used to re-read it --------
    # The report once said a worktree "holds
    # PR #N ... unmerged and awaiting review" days after #N merged. The
    # reason is the sentence a human reads to decide whether to keep a
    # checkout, so a stale one is not a cosmetic problem — it is a confident
    # false statement inside a tool people trust.

    def _held_naming_pr(self, name, branch, reason, pr_number, merged_extra=()):
        """A held worktree whose reason names a PR, plus other merged PRs."""
        path = self.add_worktree(name, branch)
        tip = git("rev-parse", "HEAD", cwd=path)
        prs = [{"headRefName": branch, "headRefOid": tip, "number": pr_number}]
        for number in merged_extra:
            prs.append({"headRefName": "feat/other-%d" % number,
                        "headRefOid": tip, "number": number})
        self.set_prs(prs)
        self.set_disposition(path, "hold", reason)
        return path

    def test_a_hold_reason_naming_a_merged_pr_is_flagged(self):
        path = self._held_naming_pr(
            "stalereason", "feat/stalereason",
            "holds PR #55, unmerged and awaiting review", 40, merged_extra=[55])
        out = self.run_report()
        self.assertIn("#55, already merged", out)
        # More conservative only: the hold itself must survive the warning.
        self.assertNotIn("git worktree remove %s" % path, out)
        self.assertIn("holds PR #55, unmerged and awaiting review", out)

    def test_a_hold_reason_naming_an_unmerged_pr_is_not_flagged(self):
        """The control. Without it, a warning on every hold would pass above."""
        self._held_naming_pr(
            "openreason", "feat/openreason",
            "holds PR #64, unmerged and awaiting review", 41)
        self.assertNotIn("already merged", self.run_report())

    def test_a_hold_reason_naming_no_pr_is_not_flagged(self):
        self._held_naming_pr("noref", "feat/noref", "paused mid-refactor", 42,
                             merged_extra=[55])
        self.assertNotIn("already merged", self.run_report())

    def test_a_bare_number_in_a_reason_is_not_a_pr_reference(self):
        # "#55" is a reference; "55 files" is prose that happens to contain a
        # number. Without this, dropping the `#` from the pattern kills no
        # test, and any hold reason mentioning a quantity could be warned about
        # the moment a PR with that number merged.
        self._held_naming_pr("barenum", "feat/barenum",
                             "paused after 55 files changed", 43,
                             merged_extra=[55])
        self.assertNotIn("already merged", self.run_report())

    def test_missing_pr_data_does_not_claim_a_reason_is_stale(self):
        """No PR data means "cannot tell", which must read as silence.

        `merged_prs` is None here, and the fetch is capped at 300 besides, so a
        number's absence never proves a PR is still open.
        """
        path = self.add_worktree("nodata", "feat/nodata")
        self.set_disposition(path, "hold", "holds PR #55, awaiting review")
        os.environ.pop("GIT_TIDY_PR_DATA", None)
        out = self.run_report()
        self.assertNotIn("already merged", out)
        self.assertNotIn("git worktree remove %s" % path, out)

    def test_a_live_session_still_wins_over_a_hold(self):
        path = self.add_worktree("busy", "feat/busy")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/busy", "headRefOid": tip,
                       "number": 17}])
        self.shim_ps(PS_ALIVE)
        self.set_claims([{"checkout": path, "pid": os.getpid(), "name": "s"}])
        self.set_disposition(path, "hold", "paused")
        self.assertIn("in use by a live session", self.run_report())

    def test_unreadable_store_is_not_an_error(self):
        path = self.add_worktree("fine", "feat/fine")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/fine", "headRefOid": tip,
                       "number": 18}])
        os.environ["GIT_TIDY_DISPOSITIONS_PATH"] = os.path.join(self.tmp, "nope.json")
        self.assertIn("git worktree remove %s" % path, self.run_report())


class TestIndexing(unittest.TestCase):

    def test_newest_pr_wins_for_a_reused_branch_name(self):
        index = lib._index([
            {"headRefName": "feat/x", "headRefOid": "new", "number": 20},
            {"headRefName": "feat/x", "headRefOid": "old", "number": 4},
        ])
        self.assertEqual(index["feat/x"]["oid"], "new")
        self.assertEqual(index["feat/x"]["number"], 20)

    def test_malformed_payload_is_not_an_empty_index(self):
        """None and {} mean different things -- unknown vs no merged PRs."""
        self.assertIsNone(lib._index({"not": "a list"}))
        self.assertEqual(lib._index([]), {})

    def test_slug_from_ssh_and_https(self):
        for url, want in (
                ("git@github.com:example-org/infra.git",
                 "example-org/infra"),
                ("https://github.com/example-org/infra",
                 "example-org/infra")):
            with tempfile.TemporaryDirectory() as tmp:
                subprocess.run(["git", "init", "-q", tmp], check=True)
                subprocess.run(["git", "-C", tmp, "remote", "add", "origin", url],
                               check=True)
                self.assertEqual(lib.slug_from_remote(tmp), want)


class TestExecutable(WorktreeFixture):
    """The report has to work through git-tidy itself, not just in-process."""

    def test_git_tidy_prints_the_worktree_section(self):
        path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        out = subprocess.run([GIT_TIDY], cwd=self.repo, capture_output=True,
                             text=True, env=dict(os.environ))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("safe to remove", out.stdout)
        self.assertIn("git worktree remove %s" % path, out.stdout)

    def test_a_broken_report_does_not_break_git_tidy(self):
        """git-tidy's branch work is done by then; the report must not undo it."""
        self.add_worktree("shipped", "feat/shipped")
        os.environ["GIT_TIDY_PR_DATA"] = os.path.join(self.tmp, "absent.json")
        env = dict(os.environ)
        env["GIT_TIDY_CLAIMS_PATH"] = "/dev/null/not-a-path"
        out = subprocess.run([GIT_TIDY], cwd=self.repo, capture_output=True,
                             text=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)


class TestBranchStepIsolation(WorktreeFixture):
    """The branch step and the worktree step fail independently."""

    def test_branch_step_failure_is_reported_and_worktree_step_still_runs(self):
        import contextlib
        import io
        self.add_worktree("fresh", "feat/fresh")
        self.set_prs([])

        def boom(_repo):
            raise RuntimeError("simulated")

        original = lib.gone_branches
        lib.gone_branches = boom
        self.addCleanup(setattr, lib, "gone_branches", original)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = report.main(["worktree_report.py", self.repo, "--gone-branches"])

        self.assertEqual(code, 0)
        self.assertIn("branch cleanup stopped (RuntimeError)", err.getvalue())
        self.assertIn("unused - no PR and nothing ahead", out.getvalue())


class TestPrune(WorktreeFixture):
    """`--prune` acts on the CACHE verdict and on nothing else.

    Every test here asserts against `git worktree list` rather than against the
    printed summary. The summary is what the code believes it did; the list is
    what happened, and the two are exactly what a wrong verdict would separate.
    """

    def run_prune(self, cwd=None, verdicts=None):
        import io
        buf = io.StringIO()
        report.prune(cwd or self.repo, verdicts=verdicts, out=buf)
        return buf.getvalue()

    def worktrees(self, cwd=None):
        out = git("worktree", "list", "--porcelain", cwd=cwd or self.repo)
        return {os.path.realpath(l.split(" ", 1)[1])
                for l in out.splitlines() if l.startswith("worktree ")}

    def test_a_cache_worktree_is_removed(self):
        path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        self.assertIn(path, self.worktrees())
        out = self.run_prune()
        self.assertIn("removed %s" % path, out)
        self.assertNotIn(path, self.worktrees())
        self.assertFalse(os.path.exists(path))

    def test_the_branch_survives_the_removal(self):
        """git-tidy's own [gone] cleanup owns branch deletion, on the next run."""
        path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        self.run_prune()
        branches = git("for-each-ref", "--format=%(refname:short)",
                       "refs/heads", cwd=self.repo).split()
        self.assertIn("feat/shipped", branches)

    def test_a_holds_worktree_is_not_removed(self):
        path = self.add_worktree("kept", "feat/kept")
        merged = self.commit_in(path, "a.txt", "a\n", "shipped work")
        self.commit_in(path, "b.txt", "b\n", "added after the merge")
        self.set_prs([{"headRefName": "feat/kept", "headRefOid": merged,
                       "number": 9}])
        self.run_prune()
        self.assertIn(path, self.worktrees())

    def test_a_live_worktree_is_not_removed(self):
        path = self.add_worktree("busy", "feat/busy")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/busy", "headRefOid": tip,
                       "number": 5}])
        self.shim_ps(PS_ALIVE)
        self.set_claims([{"checkout": path, "pid": os.getpid(), "name": "s"}])
        self.run_prune()
        self.assertIn(path, self.worktrees())

    def test_an_unknown_worktree_is_not_removed(self):
        """No PR data means "cannot tell", and cannot-tell must never delete."""
        path = self.add_worktree("opaque", "feat/opaque")
        self.commit_in(path, "a.txt", "a\n", "work")
        os.environ["GIT_TIDY_PR_DATA"] = os.path.join(self.tmp, "absent.json")
        out = self.run_prune()
        self.assertIn(path, self.worktrees())
        self.assertIn("none was removed", out)

    def test_a_held_disposition_is_not_removed(self):
        """The paused-session case: holds no data, but a cwd that resumes."""
        path = self.add_worktree("paused", "feat/paused")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/paused", "headRefOid": tip,
                       "number": 8}])
        store = os.path.join(self.tmp, "dispositions.json")
        with open(store, "w") as handle:
            json.dump({os.path.realpath(path): {
                "disposition": "hold", "reason": "waiting on review"}}, handle)
        os.environ["GIT_TIDY_DISPOSITIONS_PATH"] = store
        self.addCleanup(os.environ.pop, "GIT_TIDY_DISPOSITIONS_PATH", None)
        self.run_prune()
        self.assertIn(path, self.worktrees())

    def test_the_checkout_you_are_standing_in_is_not_removed(self):
        path = self.add_worktree("here", "feat/here")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/here", "headRefOid": tip,
                       "number": 3}])
        out = self.run_prune(cwd=path)
        self.assertIn(path, self.worktrees())
        self.assertIn("none was removed", out)
        # The control: from anywhere else the same worktree IS removed.
        self.run_prune()
        self.assertNotIn(path, self.worktrees())

    def test_nothing_to_remove_says_so(self):
        self.set_prs([])
        out = self.run_prune()
        self.assertIn("no worktree was safe to remove", out)

    def test_git_refusing_one_removal_does_not_cost_the_others(self):
        """The backstop: when the verdict and git disagree, git wins.

        The verdicts are hand-built so one CACHE entry points at a worktree that
        is actually dirty -- exactly the state a wrong classification would
        produce. `--force` is what would defeat git's refusal here, and this is
        the test that would catch it being added.
        """
        dirty = self.add_worktree("dirty", "feat/dirty")
        clean = self.add_worktree("clean", "feat/clean")
        self.write("scratch.txt", "unsaved\n", cwd=dirty)
        verdicts = [{"path": p, "branch": "b", "state": lib.CACHE,
                     "note": "", "live": False, "warn": "", "merged": True}
                    for p in (dirty, clean)]
        out = self.run_prune(verdicts=verdicts)
        self.assertIn("KEPT %s" % dirty, out)
        self.assertIn(dirty, self.worktrees())
        self.assertIn("removed %s" % clean, out)
        self.assertNotIn(clean, self.worktrees())
        self.assertIn("removed 1 of 2", out)

    def test_an_unused_worktree_is_not_removed(self):
        """The peer-worktree case: no PR and nothing ahead must survive prune."""
        fresh = self.add_worktree("fresh", "feat/fresh")
        shipped_path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(shipped_path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        self.run_prune()
        self.assertIn(fresh, self.worktrees())
        self.assertNotIn(shipped_path, self.worktrees())

    def test_a_stale_entry_whose_directory_is_gone_is_cleared(self):
        path = self.add_worktree("ghost", "feat/ghost")
        self.set_prs([])
        shutil.rmtree(path)
        out = self.run_prune()
        self.assertNotIn(path, self.worktrees())
        self.assertIn("stale worktree entry", out)

    def test_force_appears_nowhere_in_the_report(self):
        """Git's refusal is the safety mechanism; --force is how it is lost."""
        with open(os.path.join(TOOL_DIR, "worktree_report.py")) as handle:
            source = handle.read()
        for line in source.splitlines():
            if line.lstrip().startswith("#") or line.lstrip().startswith("-"):
                continue        # prose about --force is the point, not a use
            self.assertNotIn('"--force"', line)
            self.assertNotIn("'--force'", line)


class TestPruneIsOptOnly(WorktreeFixture):
    """Without the flag, nothing about today's behaviour changes."""

    def module(self, *args):
        out = subprocess.run(
            [sys.executable, os.path.join(TOOL_DIR, "worktree_report.py"),
             self.repo] + list(args),
            capture_output=True, text=True, env=dict(os.environ))
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def test_report_output_is_byte_identical_with_and_without_the_flag(self):
        path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        plain = self.module()
        pruning = self.module("--prune")
        self.assertTrue(pruning.startswith(plain),
                        "--prune changed the report itself:\n%r\n%r"
                        % (plain, pruning))
        self.assertIn("git worktree remove %s" % path, plain)

    def test_without_the_flag_the_worktree_survives(self):
        path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        self.module()
        self.assertTrue(os.path.exists(path))


class TestPruneThroughGitTidy(WorktreeFixture):
    """The flag has to work through git-tidy itself, not just in-process."""

    def tidy(self, *args):
        return subprocess.run([GIT_TIDY] + list(args), cwd=self.repo,
                              capture_output=True, text=True,
                              env=dict(os.environ))

    def test_prune_worktrees_removes_a_cache_worktree(self):
        path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        out = self.tidy("--prune-worktrees")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("removed %s" % path, out.stdout)
        self.assertFalse(os.path.exists(path))

    def test_without_the_flag_git_tidy_only_suggests(self):
        path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        out = self.tidy()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("git worktree remove %s" % path, out.stdout)
        self.assertTrue(os.path.exists(path))

    def test_prune_worktrees_leaves_an_unused_worktree(self):
        path = self.add_worktree("fresh", "feat/fresh")
        self.set_prs([])
        out = self.tidy("--prune-worktrees")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.exists(path))

    def test_an_unknown_option_is_refused_rather_than_ignored(self):
        """A typo'd delete flag doing nothing looks like a clean run."""
        path = self.add_worktree("shipped", "feat/shipped")
        tip = self.commit_in(path, "a.txt", "a\n", "work")
        self.set_prs([{"headRefName": "feat/shipped", "headRefOid": tip,
                       "number": 7}])
        out = self.tidy("--prune-worktree")
        self.assertEqual(out.returncode, 1)
        self.assertIn("unknown option", out.stderr)
        self.assertTrue(os.path.exists(path))


class TestLivenessUnknown(WorktreeFixture):
    """Liveness is three-state, and only a positive "dead" makes a claim stale.

    A `ps` that cannot answer (a sandbox, no POSIX `ps`) used to read as dead,
    which made a live session's worktree removable.
    """

    def merged_worktree_with_claim(self, pid=12345):
        path = self.add_worktree("busy", "feat/busy")
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/busy", "headRefOid": tip,
                       "number": 21}])
        self.set_claims([{"checkout": path, "pid": pid, "name": "s"}])
        return path

    def test_malformed_pids_are_unknown_even_when_ps_would_say_dead(self):
        # The shim answers "dead" for anything, so a regression that hands a
        # malformed pid to `ps`, or returns False for it directly, fails here.
        self.shim_ps(PS_DEAD)
        for pid in ("not-a-pid", 0, -1, None):
            with self.subTest(pid=pid):
                self.assertIsNone(lib.process_state(pid))

    def test_a_claim_with_a_malformed_pid_keeps_a_merged_worktree(self):
        import io
        # -1 and a string are truthy, so they reach process_state rather than
        # load_claims' `if pid` guard.
        path = self.merged_worktree_with_claim(pid="not-a-pid")
        self.shim_ps(PS_DEAD)
        report.prune(self.repo, out=io.StringIO())
        self.assertTrue(os.path.isdir(path),
                        "a claim whose pid could not be read let the worktree be pruned")

    def test_ps_refused_is_unknown_not_dead(self):
        self.shim_ps(PS_REFUSED)
        self.assertIsNone(lib.process_state(12345))

    def test_ps_dead_shape_is_false(self):
        self.shim_ps(PS_DEAD)
        self.assertIs(lib.process_state(12345), False)

    def test_ps_alive_is_true(self):
        self.shim_ps(PS_ALIVE)
        self.assertIs(lib.process_state(12345), True)

    def test_out_of_range_shape_is_unknown(self):
        self.shim_ps('echo "ps: process id too large: 999999" >&2; exit 1')
        self.assertIsNone(lib.process_state(999999))

    def test_unknown_liveness_keeps_a_merged_worktree(self):
        import io
        path = self.merged_worktree_with_claim()
        self.shim_ps(PS_REFUSED)
        verdicts = report.verdicts_for(self.repo)
        buf = io.StringIO()
        report.report(self.repo, out=buf, verdicts=verdicts)
        report.prune(self.repo, verdicts=verdicts, out=buf)
        self.assertTrue(os.path.isdir(path))
        self.assertIn("could not check whether it is still running",
                      buf.getvalue())
        self.assertNotIn("git worktree remove %s" % path, buf.getvalue())

    def test_unknown_positive_control_dead_claim_is_still_removable(self):
        import io
        path = self.merged_worktree_with_claim()
        self.shim_ps(PS_DEAD)
        report.prune(self.repo, out=io.StringIO())
        self.assertFalse(os.path.exists(path))


class TestIgnoredFiles(WorktreeFixture):
    """Ignored files are held work unless they are a known Python cache.

    `git status --porcelain` omits ignored files and `git worktree remove`
    (no `--force`) deletes them, so a worktree that looked clean could lose
    ignored data on prune. Each test commits a `.gitignore` and a tracked
    `pkg/mod.py` (so `pkg/` is a tracked directory, not a wholly-ignored
    untracked one) and creates the ignored files AFTER the committed tip.
    """

    def ignoring_worktree(self):
        path = self.add_worktree("ign", "feat/ign")
        self.write(".gitignore",
                   "private-data/\n__pycache__/\n*.pyc\n.pytest_cache/\n",
                   cwd=path)
        self.write("pkg/mod.py", "x = 1\n", cwd=path)
        git("add", "-A", cwd=path)
        git("commit", "-m", "ignore rules", cwd=path)
        tip = git("rev-parse", "HEAD", cwd=path)
        self.set_prs([{"headRefName": "feat/ign", "headRefOid": tip,
                       "number": 31}])
        return path

    def test_ignored_data_holds_and_survives_prune(self):
        import io
        path = self.ignoring_worktree()
        self.write("private-data/client.csv", "a,b\n", cwd=path)
        out = self.run_report()
        self.assertIn("ignored path(s) git would delete with it: private-data/",
                      out)
        self.assertNotIn("git worktree remove %s" % path, out)
        report.prune(self.repo, out=io.StringIO())
        self.assertTrue(os.path.exists(
            os.path.join(path, "private-data", "client.csv")))

    def test_python_caches_alone_are_still_removable(self):
        """Positive control: a cache-only worktree must still be removable."""
        import io
        path = self.ignoring_worktree()
        self.write("pkg/__pycache__/mod.cpython-39.pyc", "x", cwd=path)
        self.write(".pytest_cache/v/x", "x", cwd=path)
        status = git("status", "--porcelain", "--ignored", cwd=path)
        self.assertEqual(sorted(status.splitlines()),
                         ["!! .pytest_cache/", "!! pkg/__pycache__/"])
        out = self.run_report()
        self.assertIn("safe to remove", out)
        self.assertIn("git worktree remove %s" % path, out)
        report.prune(self.repo, out=io.StringIO())
        self.assertFalse(os.path.exists(path))

    def test_show_untracked_files_no_does_not_blind_the_check(self):
        import io
        git("config", "status.showUntrackedFiles", "no", cwd=self.repo)
        path = self.ignoring_worktree()
        self.write("private-data/client.csv", "a,b\n", cwd=path)
        self.write("scratch.txt", "s\n", cwd=path)
        out = self.run_report()
        self.assertNotIn("git worktree remove %s" % path, out)
        self.assertIn("private-data/", out)
        report.prune(self.repo, out=io.StringIO())
        self.assertTrue(os.path.exists(
            os.path.join(path, "private-data", "client.csv")))
        self.assertTrue(os.path.exists(os.path.join(path, "scratch.txt")))

    def test_is_disposable_ignored_table(self):
        for path in ("lib/__pycache__/", ".pytest_cache/", "a/b.pyc"):
            self.assertIs(lib.is_disposable_ignored(path), True, path)
        for path in ("output/", "private-data/", "__pycache__", "notes.pyc/",
                     '"weird name/__pycache__/"'):
            self.assertIs(lib.is_disposable_ignored(path), False, path)


class TestNativePs(unittest.TestCase):
    """The only place real `ps` output is asserted: pins the "dead" shape."""

    def setUp(self):
        try:
            result = subprocess.run(["ps", "-p", str(os.getpid())],
                                    capture_output=True)
        except Exception:
            self.skipTest("native ps unavailable here")
        if result.returncode != 0:
            self.skipTest("native ps unavailable here")

    def test_own_pid_is_alive(self):
        self.assertIs(lib.process_state(os.getpid()), True)

    def test_reaped_child_is_dead(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIs(lib.process_state(child.pid), False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
