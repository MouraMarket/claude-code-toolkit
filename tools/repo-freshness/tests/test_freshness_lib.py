"""Tests for the toolkit staleness check.

Hermetic: every git test builds a throwaway repo and its own "remote" in a temp
directory. Nothing here reads the real toolkit repo, whose commit count changes
with every merge and would make these pass or fail depending on the day.
"""
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import freshness_lib as freshness


def git(repo, *args):
    subprocess.run(("git", "-C", repo) + args, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _flat(text):
    """Collapse whitespace so a prose assertion survives a re-wrap.

    The footer is hard-wrapped, so any phrase long enough to be worth asserting
    on will eventually straddle a newline — "do not pull / without being told
    to" already does. A test that breaks when someone reflows a paragraph
    teaches people to loosen the assertion, which is the opposite of the point.
    """
    return re.sub(r"\s+", " ", text or "")


def commit(repo, name):
    with open(os.path.join(repo, name), "w") as handle:
        handle.write(name)
    git(repo, "add", name)
    git(repo, "commit", "-m", name)


class TestPureDecisions(unittest.TestCase):
    """should_fetch and render never touch git, and carry the whole policy."""

    def test_never_fetched_means_fetch_now(self):
        # A fresh clone has no origin/main to count against, so this is exactly
        # when a refresh matters most.
        self.assertTrue(freshness.should_fetch(None, 3600))

    def test_recent_fetch_is_not_repeated(self):
        self.assertFalse(freshness.should_fetch(60, 3600))

    def test_fetch_is_due_once_the_interval_has_elapsed(self):
        self.assertTrue(freshness.should_fetch(3600, 3600))
        self.assertTrue(freshness.should_fetch(99999, 3600))

    def test_default_interval_is_used_when_none_is_given(self):
        self.assertFalse(freshness.should_fetch(1, None))
        self.assertTrue(
            freshness.should_fetch(freshness.DEFAULT_FETCH_INTERVAL_SECONDS, None))

    def test_up_to_date_renders_nothing(self):
        self.assertIsNone(freshness.render(
            [("/repo", freshness.KIND_TOOLKIT, 0, "main")]))

    def test_unknown_state_renders_nothing(self):
        # None means "could not tell". Reporting it as up to date would be a
        # lie; reporting it as behind would cry wolf on every non-git checkout.
        self.assertIsNone(freshness.render(
            [("/repo", freshness.KIND_TOOLKIT, None, "main")]))

    def test_a_negative_count_renders_nothing(self):
        self.assertIsNone(freshness.render(
            [("/repo", freshness.KIND_TOOLKIT, -1, "main")]))

    def test_no_reports_at_all_renders_nothing(self):
        self.assertIsNone(freshness.render([]))
        self.assertIsNone(freshness.render(None))

    def test_being_behind_renders_the_count_and_the_fix(self):
        out = freshness.render(
            [("/repo", freshness.KIND_TOOLKIT, 2, "main")], "Sam")
        self.assertIn("2 commit(s) behind origin/main", out)
        self.assertIn("/repo/install.sh --update", out)

    def test_the_notice_names_the_owner_and_tells_claude_not_to_nag(self):
        out = freshness.render(
            [("/repo", freshness.KIND_TOOLKIT, 1, "main")], "Sam")
        self.assertIn("Sam", out)
        self.assertIn("Do not repeat it", out)

    def test_the_notice_asks_to_run_the_command_rather_than_only_reporting_it(self):
        # The footer IS this hook's entire behavioural payload, and nothing
        # pinned it until now. It used to say the notice was "informational, not
        # an error, and not a reason to stop what they asked for" and stop there
        # — which reads as "report this and move on", and is exactly what
        # happened, every session, for weeks, while the toolkit sat behind.
        # Reporting without offering is the failure this hook exists to prevent.
        out = _flat(freshness.render(
            [("/repo", freshness.KIND_TOOLKIT, 1, "main")], "Sam"))
        self.assertIn("offer to run", out)

    def test_the_notice_still_forbids_pulling_unasked(self):
        # The other half, and the reason the fix is an OFFER rather than an
        # action: this must not become a hook that rewrites a working tree at
        # session start, before the human has said anything at all.
        out = _flat(freshness.render(
            [("/repo", freshness.KIND_TOOLKIT, 1, "main")], "Sam"))
        self.assertIn("do not pull without being told to", out.lower())

    def test_the_notice_still_says_not_to_stop_the_work(self):
        # The half of the old wording that was correct and must survive: an
        # offer is one line alongside the real task, not a detour into it.
        out = _flat(freshness.render(
            [("/repo", freshness.KIND_TOOLKIT, 1, "main")], "Sam"))
        self.assertIn("not a reason to stop", out)

    def test_the_toolkit_line_explains_that_old_code_is_running(self):
        # Without this, "you are 2 commits behind" reads as trivia rather than
        # "the hooks in this session are the old ones".
        out = freshness.render([("/repo", freshness.KIND_TOOLKIT, 1, "main")])
        self.assertIn("symlink", out)

    # The kinds are `toolkit` and `generic`. The cases below pin what a
    # NON-TOOLKIT repo gets: a different reason, a plain pull, and never the
    # installer.

    def test_a_watched_repo_explains_a_different_risk_than_the_toolkit(self):
        # A stale watched repo is not "old code" -- it is a repo being read or
        # run from while out of date, which is a different sentence.
        out = freshness.render([("/x/notes", freshness.KIND_GENERIC, 4, "main")])
        self.assertIn("out-of-date information", out)
        self.assertNotIn("symlink", out)

    def test_a_watched_repo_is_not_told_to_run_install(self):
        # Nothing links to a watched repo; suggesting install.sh would be noise
        # at best and a confusing failure at worst.
        out = freshness.render([("/x/notes", freshness.KIND_GENERIC, 4, "main")])
        self.assertIn("git -C /x/notes pull", out)
        self.assertNotIn("install.sh", out)

    def test_only_stale_repos_appear(self):
        out = freshness.render([
            ("/x/toolkit", freshness.KIND_TOOLKIT, 0, "main"),
            ("/x/notes", freshness.KIND_GENERIC, 3, "main"),
        ])
        self.assertIn("notes", out)
        self.assertNotIn("toolkit", out)

    def test_several_stale_repos_share_one_block(self):
        out = freshness.render([
            ("/x/claude-code-toolkit", freshness.KIND_TOOLKIT, 2, "main"),
            ("/x/notes", freshness.KIND_GENERIC, 3, "main"),
        ])
        self.assertEqual(out.count("=== REPO UPDATES AVAILABLE ==="), 1)
        self.assertIn("claude-code-toolkit is 2", out)
        self.assertIn("notes is 3", out)
        self.assertEqual(out.count("Do not repeat it"), 1)

    def test_the_header_is_singular_for_one_repo(self):
        out = freshness.render([("/x/notes", freshness.KIND_GENERIC, 1, "main")])
        self.assertIn("=== REPO UPDATE AVAILABLE ===", out)

    def test_an_unrecognised_kind_renders_the_generic_reason(self):
        # The fallback for a kind no map produces: the generic sentence, never
        # a claim about what the repo holds.
        out = freshness.render([("/x/notes", "no-such-kind", 1, "main")])
        self.assertIn("out-of-date information", out)
        self.assertIn("git -C /x/notes pull", out)

    def test_an_unknown_repo_still_gets_a_reason_and_a_command(self):
        # Anything a user adds to `watch` must render usefully, not blankly.
        out = freshness.render([("/x/whatever", freshness.KIND_GENERIC, 1, "main")])
        self.assertIn("whatever is 1", out)
        self.assertIn("git -C /x/whatever pull", out)
        self.assertNotIn("install.sh", out)

    def test_only_the_toolkit_is_told_to_run_install(self):
        # Every kind EXCEPT the toolkit, derived from `_KIND_WHY` rather than
        # listed, so a kind added later is covered without anyone extending it.
        others = [k for k in freshness._KIND_WHY if k != freshness.KIND_TOOLKIT]
        self.assertTrue(others, "no non-toolkit kind to check")
        for kind in others:
            out = freshness.render([("/x/repo", kind, 1, "main")])
            self.assertNotIn("install.sh", out, kind)
        out = freshness.render([("/x/repo", freshness.KIND_TOOLKIT, 1, "main")])
        self.assertIn("install.sh", out)


class TestShouldReport(unittest.TestCase):
    """When being behind is worth saying. Pure; the whole noise policy.

    Getting this wrong in the permissive direction is what makes a session-start
    notice get skimmed — and a skimmed notice protects nothing.
    """

    def test_on_the_default_branch_it_reports(self):
        self.assertTrue(freshness.should_report("main", "main"))

    def test_on_a_feature_branch_it_stays_quiet(self):
        # Behind origin/main here is not staleness — it is what having a branch
        # open means, and pulling main into it is not what anyone wants.
        self.assertFalse(freshness.should_report("feat/thing", "main"))

    def test_a_detached_head_stays_quiet(self):
        self.assertFalse(freshness.should_report("HEAD", "main"))

    def test_an_unreadable_branch_stays_quiet(self):
        self.assertFalse(freshness.should_report(None, "main"))
        self.assertFalse(freshness.should_report("", "main"))

    def test_it_honours_a_non_main_default_branch(self):
        self.assertTrue(freshness.should_report("master", "master"))
        self.assertFalse(freshness.should_report("main", "master"))


class TestWatchDefaults(unittest.TestCase):
    """With no `watch` configured, only the toolkit is checked.

    The fixture workspace holds real clones under `repos/`, so a default that
    named any repo, or a discovery sweep, would have something to find: an
    empty answer here is the default being empty, not the fixture being bare.
    """

    def workspace(self, *names):
        root = tempfile.mkdtemp(prefix="freshness-ws-")
        self.addCleanup(shutil.rmtree, root, True)
        for name in names:
            os.makedirs(os.path.join(root, "repos", name, ".git"))
        return root

    def test_no_config_watches_the_toolkit_only(self):
        root = self.workspace("notes", "infra")
        with mock.patch.object(freshness, "_config", None), \
                mock.patch.object(freshness, "workspace_root", return_value=root):
            self.assertEqual(freshness.watch_list(), ())
            self.assertEqual(freshness.watched_repos(toolkit="/x/toolkit"),
                             [("/x/toolkit", freshness.KIND_TOOLKIT)])

    def test_a_configured_entry_is_watched_with_the_generic_reason(self):
        root = self.workspace("notes")
        with mock.patch.object(freshness, "watch_list", return_value=("notes",)), \
                mock.patch.object(freshness, "workspace_root", return_value=root):
            self.assertEqual(
                freshness.watched_repos(toolkit="/x/toolkit"),
                [("/x/toolkit", freshness.KIND_TOOLKIT),
                 (os.path.join(root, "repos", "notes"), freshness.KIND_GENERIC)])


class TestWatchResolution(unittest.TestCase):
    """Turning config entries into paths. Pure, no filesystem assumptions."""

    def test_a_bare_name_resolves_under_the_workspace_repos_dir(self):
        # The bare form is what makes a shared default possible: paths are
        # personal, repo names are not.
        self.assertEqual(
            freshness.resolve_watch_entry("notes", "/home/sam/Work"),
            "/home/sam/Work/repos/notes")

    def test_an_absolute_path_is_used_as_given(self):
        self.assertEqual(
            freshness.resolve_watch_entry("/elsewhere/notes", "/home/sam/Work"),
            "/elsewhere/notes")

    def test_a_tilde_path_is_expanded(self):
        out = freshness.resolve_watch_entry("~/somewhere/notes", None)
        self.assertTrue(out.startswith(os.path.expanduser("~")))
        self.assertNotIn("~", out)

    def test_a_bare_name_without_a_workspace_root_resolves_to_nothing(self):
        # Guessing a root is how a tool ends up reporting on a directory the
        # user never meant it to touch.
        self.assertIsNone(freshness.resolve_watch_entry("notes", None))

    def test_blank_entries_resolve_to_nothing(self):
        self.assertIsNone(freshness.resolve_watch_entry("", "/root"))
        self.assertIsNone(freshness.resolve_watch_entry("   ", "/root"))


class GitRepoTestCase(unittest.TestCase):

    def setUp(self):
        if not self._git_available():
            self.skipTest("git not available")
        self._tmp = tempfile.mkdtemp()
        self.remote = os.path.join(self._tmp, "remote.git")
        self.clone = os.path.join(self._tmp, "clone")

        origin = os.path.join(self._tmp, "origin")
        os.makedirs(origin)
        git(origin, "init", "-q", "-b", "main")
        git(origin, "config", "user.email", "t@example.com")
        git(origin, "config", "user.name", "T")
        commit(origin, "first")
        subprocess.run(("git", "clone", "-q", "--bare", origin, self.remote),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(("git", "clone", "-q", self.remote, self.clone),
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(self.clone, "config", "user.email", "t@example.com")
        git(self.clone, "config", "user.name", "T")
        self.origin = origin

    @staticmethod
    def _git_available():
        try:
            subprocess.run(("git", "--version"), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            return False

    def push_new_commit(self, name="second"):
        commit(self.origin, name)
        git(self.origin, "push", "-q", self.remote, "main")


class TestPinNotice(GitRepoTestCase):
    """`pin_notice`: reported only for a detached HEAD that is the recorded
    rollback pin. The record is read first, so the common case costs no git."""

    def setUp(self):
        super().setUp()
        self.record_file = os.path.join(self._tmp, "install-record.json")
        self.head = subprocess.run(
            ("git", "-C", self.clone, "rev-parse", "HEAD"), check=True,
            stdout=subprocess.PIPE).stdout.decode().strip()

    def write_record(self, commit_sha=None, pinned_at="2026-10-13T09:00:00Z",
                     with_rollback=True, raw=None):
        data = {"schema": 1, "permissions_added": {}}
        if with_rollback:
            data["rollback"] = {"commit": commit_sha or self.head,
                                "recorded_at": "2026-10-12T09:00:00Z",
                                "pinned_at": pinned_at}
        with open(self.record_file, "w", encoding="utf-8") as handle:
            handle.write(raw if raw is not None else json.dumps(data))

    def detach(self):
        git(self.clone, "checkout", "-q", "--detach", "HEAD")

    def notice(self, deadline=None):
        return freshness.pin_notice(
            self.clone, self.record_file,
            deadline or freshness.Deadline(time.monotonic() + 1000))

    def test_pinned_reports_sha_command_and_behind_count(self):
        self.write_record()
        self.detach()
        self.push_new_commit()
        git(self.clone, "fetch", "-q", "origin")
        out = self.notice()
        self.assertIn("=== TOOLKIT PINNED ===", out)
        self.assertIn(self.head[:12], out)
        self.assertIn("install.sh --update", out)
        self.assertIn("2026-10-13T09:00:00Z", out)
        self.assertIn("1 commit(s) behind", out)

    def test_unknown_count_when_behind_cannot_be_read(self):
        self.write_record()
        self.detach()
        with mock.patch.object(freshness, "commits_behind", lambda r, b: None):
            self.assertIn("an unknown number of commits", self.notice())

    def test_detached_but_no_rollback_entry(self):
        self.write_record(with_rollback=False)
        self.detach()
        self.assertIsNone(self.notice())

    def test_pinned_at_none(self):
        self.write_record(pinned_at=None)
        self.detach()
        self.assertIsNone(self.notice())

    def test_detached_at_a_different_commit(self):
        self.write_record(commit_sha="0" * 40)
        self.detach()
        self.assertIsNone(self.notice())

    def test_on_a_branch_with_a_pinned_record(self):
        self.write_record()
        self.assertIsNone(self.notice())

    def test_malformed_record(self):
        self.write_record(raw="{not json")
        self.detach()
        self.assertIsNone(self.notice())

    def test_missing_record(self):
        self.detach()
        self.assertIsNone(self.notice())

    def _counting_git(self):
        calls = []
        real = freshness._git

        def counting(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)
        return calls, counting

    def test_no_pinned_record_makes_no_git_call(self):
        self.write_record(with_rollback=False)
        calls, counting = self._counting_git()
        with mock.patch.object(freshness, "_git", counting):
            self.assertIsNone(self.notice())
        self.assertEqual(calls, [])

    def test_expired_deadline_makes_no_git_call(self):
        self.write_record()
        self.detach()
        calls, counting = self._counting_git()
        with mock.patch.object(freshness, "_git", counting):
            self.assertIsNone(self.notice(freshness.Deadline(0)))
        self.assertEqual(calls, [])


class TestRepoRoot(GitRepoTestCase):

    def test_finds_the_repo_from_a_nested_path(self):
        nested = os.path.join(self.clone, "a", "b")
        os.makedirs(nested)
        probe = os.path.join(nested, "file.py")
        with open(probe, "w") as handle:
            handle.write("")
        self.assertEqual(os.path.realpath(freshness.repo_root(probe)),
                         os.path.realpath(self.clone))

    def test_returns_none_outside_any_repo(self):
        outside = os.path.join(self._tmp, "not-a-repo")
        os.makedirs(outside)
        probe = os.path.join(outside, "file.py")
        with open(probe, "w") as handle:
            handle.write("")
        # tempfile dirs are not inside a repo on any machine this runs on.
        self.assertIsNone(freshness.repo_root(probe))

    def test_resolves_through_a_symlink(self):
        # The whole reason this uses realpath: the hook is reached via
        # ~/.local/bin, and abspath would not resolve the symlink.
        link = os.path.join(self._tmp, "linked.py")
        target = os.path.join(self.clone, "real.py")
        with open(target, "w") as handle:
            handle.write("")
        os.symlink(target, link)
        self.assertEqual(os.path.realpath(freshness.repo_root(link)),
                         os.path.realpath(self.clone))


class TestCommitsBehind(GitRepoTestCase):

    def test_a_current_clone_is_zero_behind(self):
        self.assertEqual(freshness.commits_behind(self.clone, "main"), 0)

    def test_counts_commits_pushed_after_the_clone(self):
        self.push_new_commit("second")
        self.push_new_commit("third")
        git(self.clone, "fetch", "-q", "origin")
        self.assertEqual(freshness.commits_behind(self.clone, "main"), 2)

    def test_counts_nothing_until_the_refs_are_fetched(self):
        # The point of the background fetch: without it the count is stale, and
        # stale here means silently reassuring.
        self.push_new_commit("second")
        self.assertEqual(freshness.commits_behind(self.clone, "main"), 0)

    def test_a_missing_branch_is_unknown_not_zero(self):
        self.assertIsNone(freshness.commits_behind(self.clone, "no-such-branch"))

    def test_a_non_repo_is_unknown_not_zero(self):
        self.assertIsNone(freshness.commits_behind(self._tmp, "main"))

    def test_local_commits_do_not_hide_missing_upstream_ones(self):
        # Diverged: the clone has its own commit AND is missing one from origin.
        # Being ahead must not mask being behind.
        self.push_new_commit("second")
        commit(self.clone, "local-only")
        git(self.clone, "fetch", "-q", "origin")
        self.assertEqual(freshness.commits_behind(self.clone, "main"), 1)

    def test_a_feature_branch_still_counts_missing_main_commits(self):
        # commits_behind reports the raw fact even on a feature branch. Whether
        # that fact is worth SAYING is should_report's job, not this one's —
        # keeping the count honest here means the two can be tested separately.
        self.push_new_commit("second")
        git(self.clone, "fetch", "-q", "origin")
        git(self.clone, "checkout", "-q", "-b", "feature")
        self.assertEqual(freshness.commits_behind(self.clone, "main"), 1)

    def test_a_foreign_ambient_GIT_DIR_does_not_override_dash_C(self):
        """Merge Review FIX 1 -- an ambient `GIT_DIR` naming a DIFFERENT repo
        must never win over `git -C <dir>`. Without a sanitized subprocess
        env, `_git` would answer about the foreign repo's HEAD instead of
        `self.clone`'s."""
        self.push_new_commit("second")
        self.push_new_commit("third")
        git(self.clone, "fetch", "-q", "origin")

        foreign_tmp = tempfile.mkdtemp()
        foreign = os.path.join(foreign_tmp, "foreign")
        os.makedirs(foreign)
        git(foreign, "init", "-q", "-b", "main")
        git(foreign, "config", "user.email", "t@example.com")
        git(foreign, "config", "user.name", "T")
        commit(foreign, "unrelated")

        prior = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = os.path.join(foreign, ".git")
        try:
            result = freshness.commits_behind(self.clone, "main")
        finally:
            if prior is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = prior
        self.assertEqual(result, 2)


class TestCommitsAhead(GitRepoTestCase):
    """The count that separates a fast-forward from a divergence.

    `commits_behind` is identical for both, which is exactly why the notice
    could promise a fast-forward and mean a merge.
    """

    def test_a_current_clone_is_zero_ahead(self):
        self.assertEqual(freshness.commits_ahead(self.clone, "main"), 0)

    def test_counts_local_commits_origin_does_not_have(self):
        commit(self.clone, "local-only")
        commit(self.clone, "local-two")
        self.assertEqual(freshness.commits_ahead(self.clone, "main"), 2)

    def test_being_behind_alone_leaves_ahead_at_zero(self):
        # The fast-forward case. If this ever returns non-zero, every ordinary
        # pull offer would be downgraded to the diverged wording.
        self.push_new_commit("second")
        git(self.clone, "fetch", "-q", "origin")
        self.assertEqual(freshness.commits_behind(self.clone, "main"), 1)
        self.assertEqual(freshness.commits_ahead(self.clone, "main"), 0)

    def test_divergence_shows_in_BOTH_counts(self):
        # The whole point: behind is 1 either way, and only `ahead` tells the
        # two situations apart.
        self.push_new_commit("second")
        commit(self.clone, "local-only")
        git(self.clone, "fetch", "-q", "origin")
        self.assertEqual(freshness.commits_behind(self.clone, "main"), 1)
        self.assertEqual(freshness.commits_ahead(self.clone, "main"), 1)

    def test_a_missing_branch_is_unknown_not_zero(self):
        self.assertIsNone(freshness.commits_ahead(self.clone, "no-such-branch"))

    def test_a_non_repo_is_unknown_not_zero(self):
        self.assertIsNone(freshness.commits_ahead(self._tmp, "main"))


class TestDivergedNoticeDoesNotPromiseAFastForward(unittest.TestCase):
    """A diverged repo must not be handed the fast-forward footer.

    The failure this prevents is not a crash: it is the human saying yes to
    "it's a fast-forward" and getting a merge, or a hard failure on a dirty
    tree. The notice is read once, at speed, and acted on.
    """

    def test_the_fast_forward_claim_is_absent_when_diverged(self):
        out = freshness.render(
            [("/x/repo", freshness.KIND_TOOLKIT, 2, "main", 3)])
        self.assertNotIn("they are fast-forwards", out)
        self.assertIn("DIVERGED", out)
        self.assertIn("a pull there is a merge, not a fast-forward", out,
                      "the footer no longer explains WHY a pull is wrong here")

    def test_no_runnable_pull_command_is_printed_for_a_diverged_repo(self):
        # The footer forbidding a pull is not enough: a reader acts on the
        # runnable line, not the paragraph under it. This is the finding a
        # review caught — the first cut hedged the prose and still printed
        # `git pull && install.sh` directly beneath the warning.
        out = freshness.render(
            [("/x/repo", freshness.KIND_TOOLKIT, 2, "main", 3)])
        # Look for a RUNNABLE line, not for the word "pull" — the footer says
        # "pull" several times on purpose, and string-surgery around that is
        # how this assertion gets quietly defanged later.
        # `git -C <path> pull` as an actual invocation. Matching the bare word
        # would also hit the inspection line's own "this is NOT a pull:".
        runnable = [line for line in out.splitlines()
                    if re.search(r"git\s+-C\s+\S+\s+pull\b", line)]
        self.assertEqual(
            runnable, [],
            "a runnable pull command is printed for a DIVERGED repo — the "
            "reader acts on the command, not the paragraph under it:\n%s" % out)
        self.assertIn("log --oneline origin/main..HEAD", out,
                      "no read-only way to inspect the divergence was offered")

    def test_a_clean_repo_still_gets_its_runnable_pull_command(self):
        # The complement, so the fix above cannot be "never print a command".
        out = freshness.render(
            [("/x/repo", freshness.KIND_TOOLKIT, 2, "main", 0)])
        self.assertIn("pull", out)

    def test_both_counts_are_named_so_the_human_can_judge(self):
        out = freshness.render(
            [("/x/repo", freshness.KIND_TOOLKIT, 2, "main", 3)])
        self.assertIn("2 commit(s) behind", out)
        self.assertIn("3 of its own", out)

    def test_ahead_zero_keeps_the_ordinary_fast_forward_footer(self):
        out = freshness.render(
            [("/x/repo", freshness.KIND_TOOLKIT, 2, "main", 0)])
        self.assertIn("they are fast-forwards", out)
        self.assertNotIn("DIVERGED", out)

    def test_a_four_tuple_still_renders_exactly_as_before(self):
        # Backward compatibility is load-bearing: every existing caller and test
        # passes four elements, and an unmeasured repo must not be reported as
        # diverged on the strength of a missing field.
        four = freshness.render([("/x/repo", freshness.KIND_TOOLKIT, 2, "main")])
        five = freshness.render([("/x/repo", freshness.KIND_TOOLKIT, 2, "main", None)])
        self.assertEqual(four, five)
        self.assertIn("they are fast-forwards", four)
        self.assertNotIn("DIVERGED", four)

    def test_one_diverged_repo_hedges_the_footer_for_the_whole_notice(self):
        # Mixed batch: the footer is shared, so it must be the safe one, and it
        # has to tell the reader which line it applies to.
        out = freshness.render([
            ("/x/clean", freshness.KIND_GENERIC, 1, "main", 0),
            ("/x/dirty", freshness.KIND_GENERIC, 1, "main", 4),
        ])
        self.assertNotIn("they are fast-forwards", out)
        self.assertIn("Do NOT offer a plain pull", out)
        self.assertIn("WITHOUT", out)          # names how to read the clean one
        self.assertIn("clean is 1 commit(s) behind", out)
        self.assertIn("dirty has DIVERGED", out)


class TestAnEmptyRepoPathIsUnknownNotTheCurrentDirectory(GitRepoTestCase):
    """`git -C ""` is a no-op, so an empty path used to answer about cwd.

    Not a fail-open — a *confident wrong answer*. Every function here documents
    that None means "cannot tell", and without the guard an empty path returned
    a real branch name and real commit counts for whatever directory the process
    was launched from.

    **These tests must run with cwd inside a real git repo**, otherwise the
    unguarded code fails for an unrelated reason (`not a git repository`) and
    they pass without testing anything — the same shape as a check that passes
    on bash's own error message. `setUp` chdirs into the fixture clone and the
    first test asserts the arrangement actually holds.
    """

    def setUp(self):
        super().setUp()
        self._cwd = os.getcwd()
        self.addCleanup(os.chdir, self._cwd)
        os.chdir(self.clone)

    def test_the_fixture_puts_us_inside_a_real_repo(self):
        """The arrangement, asserted against CWD — which is the thing that
        matters and the thing the first version of this test did not check.

        It asserted `current_branch(self.clone)`, an explicit path, so it
        passed whether or not the `chdir` had happened. Removing the `chdir`
        survived mutation. It only *looked* load-bearing because the suite is
        normally run from inside this repo, so the ambient cwd was a git repo
        anyway — the tests below would still have caught a missing guard here,
        and would have passed vacuously for anyone running them from `/tmp`.
        """
        self.assertEqual(
            os.path.realpath(os.getcwd()), os.path.realpath(self.clone),
            "the fixture did not chdir into the clone, so an unguarded "
            "`git -C \"\"` would read whatever directory the runner was "
            "launched from — the tests below would then pass or fail on "
            "ambient state rather than on the guard")
        self.assertTrue(
            os.path.isdir(os.path.join(os.getcwd(), ".git")),
            "cwd is not a working git repo, so an unguarded call would fail "
            "for an unrelated reason and these tests could not tell the guard "
            "from a plain git error")

    def test_current_branch_of_an_empty_path_is_None(self):
        self.assertIsNone(freshness.current_branch(""))

    def test_commits_behind_of_an_empty_path_is_None(self):
        self.assertIsNone(freshness.commits_behind("", "main"))

    def test_commits_ahead_of_an_empty_path_is_None(self):
        self.assertIsNone(freshness.commits_ahead("", "main"))

    def test_background_fetch_refuses_an_empty_path_rather_than_fetching_cwd(self):
        """The worse half of the same hazard, and it bypasses `_git`.

        `spawn_background_fetch` builds its own argv, so hardening `_git` did
        not cover it. With an empty path `git -C ""` would fetch in whatever
        directory the hook was launched from — network I/O against an unrelated
        repo's remote-tracking refs.

        The return value is asserted because it is the only thing observable
        from here without letting a stray fetch actually run. Note today's only
        caller discards it, so `False` is not what prevents the harm — the early
        return is. It matters for a future caller that does check.
        """
        self.assertIs(freshness.spawn_background_fetch(""), False)

    def test_background_fetch_still_works_for_a_real_repo(self):
        # The complement, so the fix above cannot be "always return False".
        self.assertIs(freshness.spawn_background_fetch(self.clone), True)

    def test_None_means_unknown_here_and_not_zero(self):
        # The distinction the whole module rests on: `render` drops a repo whose
        # `behind` is 0 AND one whose `behind` is None, so conflating them is
        # invisible in the notice and wrong in the count.
        self.assertIsNot(freshness.commits_behind("", "main"), 0)
        self.assertIsNot(freshness.commits_ahead("", "main"), 0)


class TestCurrentBranch(GitRepoTestCase):

    def test_reports_the_checked_out_branch(self):
        self.assertEqual(freshness.current_branch(self.clone), "main")

    def test_reports_a_feature_branch(self):
        git(self.clone, "checkout", "-q", "-b", "feat/thing")
        self.assertEqual(freshness.current_branch(self.clone), "feat/thing")

    def test_a_non_repo_reports_nothing(self):
        self.assertIsNone(freshness.current_branch(self._tmp))

    def test_a_feature_branch_is_not_reported_even_when_behind(self):
        # End to end across the two functions: the count is 1, the answer is
        # still silence.
        self.push_new_commit("second")
        git(self.clone, "fetch", "-q", "origin")
        git(self.clone, "checkout", "-q", "-b", "feature")
        self.assertEqual(freshness.commits_behind(self.clone, "main"), 1)
        self.assertFalse(freshness.should_report(
            freshness.current_branch(self.clone), "main"))

    def test_returning_to_the_default_branch_reports_again(self):
        # The accepted cost of the branch rule is bounded by this: the moment
        # you are back where it matters, you are told.
        self.push_new_commit("second")
        git(self.clone, "fetch", "-q", "origin")
        git(self.clone, "checkout", "-q", "-b", "feature")
        git(self.clone, "checkout", "-q", "main")
        self.assertTrue(freshness.should_report(
            freshness.current_branch(self.clone), "main"))


class TestDefaultBranch(GitRepoTestCase):

    def test_falls_back_to_main_when_origin_head_is_unset(self):
        git(self.clone, "remote", "set-head", "origin", "--delete")
        self.assertEqual(freshness.default_branch(self.clone), "main")

    def test_a_non_repo_falls_back_to_main(self):
        self.assertEqual(freshness.default_branch(self._tmp), "main")


class TestFetchAge(GitRepoTestCase):

    def test_a_repo_that_never_fetched_reports_none(self):
        fetch_head = os.path.join(self.clone, ".git", "FETCH_HEAD")
        if os.path.exists(fetch_head):
            os.remove(fetch_head)
        self.assertIsNone(freshness.seconds_since_fetch(self.clone))

    def test_age_is_measured_from_fetch_head(self):
        git(self.clone, "fetch", "-q", "origin")
        age = freshness.seconds_since_fetch(self.clone)
        self.assertIsNotNone(age)
        self.assertLess(age, 60)

    def test_an_old_fetch_reports_a_large_age(self):
        git(self.clone, "fetch", "-q", "origin")
        fetch_head = os.path.join(self.clone, ".git", "FETCH_HEAD")
        long_ago = time.time() - 86400
        os.utime(fetch_head, (long_ago, long_ago))
        self.assertGreater(freshness.seconds_since_fetch(self.clone), 80000)


class TestBackgroundFetch(GitRepoTestCase):

    def test_a_spawned_fetch_eventually_updates_the_refs(self):
        self.push_new_commit("second")
        self.assertTrue(freshness.spawn_background_fetch(self.clone))
        for _ in range(50):
            if freshness.commits_behind(self.clone, "main"):
                break
            time.sleep(0.1)
        self.assertEqual(freshness.commits_behind(self.clone, "main"), 1)

    def test_spawning_against_a_bad_path_does_not_raise(self):
        # git itself fails here, not Popen — the contract is only "never raise".
        self.assertTrue(freshness.spawn_background_fetch("/nonexistent/repo") in (True, False))


class TestBlockedArgument(unittest.TestCase):
    """render()'s new keyword-only `blocked` argument.

    `None` means the sync phase did not run; `[]` means it ran and blocked
    nothing. Both must render exactly as before this argument existed — that
    is the distinction a later task depends on, so it gets its own tests
    rather than being inferred from the non-empty case alone.
    """

    def test_no_blocked_argument_renders_exactly_as_before(self):
        reports = [("/x/repo", freshness.KIND_TOOLKIT, 2, "main")]
        self.assertEqual(freshness.render(reports, "Sam"),
                         freshness.render(reports, "Sam", blocked=None))

    def test_an_empty_blocked_list_renders_exactly_as_the_default(self):
        reports = [("/x/repo", freshness.KIND_TOOLKIT, 2, "main")]
        self.assertEqual(freshness.render(reports, "Sam"),
                         freshness.render(reports, "Sam", blocked=[]))

    def test_up_to_date_with_no_blocked_argument_stays_none(self):
        self.assertIsNone(freshness.render([], "Sam"))

    def test_up_to_date_with_an_empty_blocked_list_stays_none(self):
        self.assertIsNone(freshness.render([], "Sam", blocked=[]))

    def test_a_blocked_repo_is_worth_saying_even_when_nothing_is_behind(self):
        # Reports is empty — nothing is stale — and the notice must still be
        # non-None, because a blocked repo is news on its own.
        out = freshness.render(
            [], "Sam", blocked=[("/x/foo", "uncommitted changes")])
        self.assertIsNotNone(out)
        self.assertIn("foo", out)
        self.assertIn("uncommitted changes", out)

    def test_uncommitted_changes_states_nothing_was_changed(self):
        out = freshness.render(
            [], "Sam", blocked=[("/x/foo", "uncommitted changes")])
        self.assertIn("nothing was changed", out.lower())

    def test_detached_head_states_nothing_was_changed(self):
        out = freshness.render(
            [], "Sam", blocked=[("/x/foo", "detached HEAD")])
        self.assertIn("nothing was changed", out.lower())

    def test_no_upstream_does_not_claim_nothing_changed(self):
        # This repo was never eligible in the first place, so the
        # "nothing was changed" reassurance is not a claim this reason
        # can support the way it can for a dirty or detached checkout.
        out = freshness.render(
            [], "Sam", blocked=[("/x/foo", "no upstream")])
        self.assertNotIn("nothing was changed", out.lower())
        self.assertIn("no upstream", out)

    def test_could_not_read_states_nothing_was_changed(self):
        # "could not read" means eligibility() itself failed or timed out --
        # the repo's OWN state is unknown, but "nothing was changed" is a
        # claim about what THIS TOOL did (never reached the network), which
        # we know with certainty regardless of the repo's unreadable state.
        out = freshness.render(
            [], "Sam", blocked=[("/x/foo", "could not read")])
        self.assertIn("nothing was changed", out.lower())
        self.assertIn("could not read", out)

    def test_a_repo_in_both_reports_and_blocked_appears_once_under_blocked(self):
        out = freshness.render(
            [("/x/foo", freshness.KIND_TOOLKIT, 2, "main")], "Sam",
            blocked=[("/x/foo", "uncommitted changes")])
        self.assertNotIn("foo is 2 commit(s) behind", out)
        self.assertEqual(out.count("foo"), 1)

    def test_a_repo_in_both_reports_and_blocked_offers_no_pull_command(self):
        out = freshness.render(
            [("/x/foo", freshness.KIND_TOOLKIT, 2, "main")], "Sam",
            blocked=[("/x/foo", "uncommitted changes")])
        runnable = [line for line in out.splitlines()
                    if re.search(r"git\s+-C\s+\S+\s+pull\b", line)]
        self.assertEqual(runnable, [])

    def test_the_diverged_footer_still_wins_when_a_report_has_diverged(self):
        # The diverged repo here is untouched by `blocked` — a separate repo
        # is blocked — so the ordinary diverged-footer behaviour must survive.
        out = freshness.render(
            [("/x/repo", freshness.KIND_TOOLKIT, 2, "main", 3)], "Sam",
            blocked=[("/x/other", "no upstream")])
        self.assertIn("DIVERGED", out)
        self.assertIn("Do NOT offer a plain pull", out)


class TestBlockedFooterChoice(unittest.TestCase):
    """Which of the three footers actually appears, asserted on the TEXT.

    `assertIsNotNone` alone is what let a real defect through here: a
    blocked-only notice returned non-None while still ending on `_FOOTER`,
    which offers to run "the command(s) above" when there are none — the
    same class of failure `_FOOTER_DIVERGED` exists to prevent, arriving by a
    different route. These tests read the footer text itself, per repo shape.
    """

    _OFFER_PHRASE = "offer to run the command(s) above"
    _DIVERGED_PHRASE = "Do NOT offer a plain pull"

    def test_blocked_only_gets_the_blocked_footer(self):
        # Nothing in `reports` at all — the pure "sync ran, found one repo it
        # could not touch" case.
        out = _flat(freshness.render(
            [], "Sam", blocked=[("/x/foo", "uncommitted changes")]))
        self.assertIn("left untouched", out)
        self.assertIn("commit or stash", out)
        self.assertNotIn(self._OFFER_PHRASE, out)
        self.assertNotIn(self._DIVERGED_PHRASE, out)

    def test_a_fully_absorbed_report_also_gets_the_blocked_footer(self):
        # `reports` has an entry, but it is the SAME repo as the blocked one,
        # so after the blocked-exclusion `stale` is empty too — no commands
        # survive to be offered, even though `reports` was non-empty going in.
        out = _flat(freshness.render(
            [("/x/foo", freshness.KIND_TOOLKIT, 2, "main")], "Sam",
            blocked=[("/x/foo", "uncommitted changes")]))
        self.assertIn("left untouched", out)
        self.assertNotIn(self._OFFER_PHRASE, out)
        self.assertNotIn(self._DIVERGED_PHRASE, out)

    def test_stale_only_keeps_the_ordinary_footer(self):
        # No `blocked` at all — the pre-existing behaviour, reasserted here on
        # the footer text specifically rather than just non-None.
        out = _flat(freshness.render(
            [("/x/repo", freshness.KIND_TOOLKIT, 2, "main")], "Sam"))
        self.assertIn(self._OFFER_PHRASE, out)
        self.assertNotIn("left untouched", out)
        self.assertNotIn(self._DIVERGED_PHRASE, out)

    def test_stale_and_blocked_together_keeps_the_ordinary_footer(self):
        # A real command survives (a DIFFERENT repo than the blocked one), so
        # there genuinely is something to offer — the blocked section must not
        # suppress that.
        out = _flat(freshness.render(
            [("/x/behind", freshness.KIND_TOOLKIT, 2, "main")], "Sam",
            blocked=[("/x/other", "uncommitted changes")]))
        self.assertIn(self._OFFER_PHRASE, out)
        self.assertNotIn("left untouched", out)
        self.assertNotIn(self._DIVERGED_PHRASE, out)
        self.assertIn("other", out)   # the blocked section still renders

    def test_stale_and_blocked_together_with_a_diverged_report_keeps_diverged(self):
        # Same as above, but the surviving stale entry has itself diverged —
        # the diverged footer must still win over both the ordinary and the
        # blocked one.
        out = _flat(freshness.render(
            [("/x/behind", freshness.KIND_TOOLKIT, 2, "main", 3)], "Sam",
            blocked=[("/x/other", "no upstream")]))
        self.assertIn(self._DIVERGED_PHRASE, out)
        self.assertNotIn(self._OFFER_PHRASE, out)
        self.assertNotIn("left untouched", out)


class TestFailedArgument(unittest.TestCase):
    """render()'s new keyword-only `failed` argument — repos the sync phase
    ACTUALLY RAN a pull against and had refused, distinct from `blocked`
    (never attempted).
    """

    def test_no_failed_argument_renders_exactly_as_before(self):
        reports = [("/x/repo", freshness.KIND_TOOLKIT, 2, "main")]
        self.assertEqual(freshness.render(reports, "Sam"),
                         freshness.render(reports, "Sam", failed=None))

    def test_an_empty_failed_list_renders_exactly_as_the_default(self):
        reports = [("/x/repo", freshness.KIND_TOOLKIT, 2, "main")]
        self.assertEqual(freshness.render(reports, "Sam"),
                         freshness.render(reports, "Sam", failed=[]))

    def test_up_to_date_with_an_empty_failed_list_stays_none(self):
        self.assertIsNone(freshness.render([], "Sam", failed=[]))

    def test_a_failed_repo_is_worth_saying_even_when_nothing_is_behind(self):
        out = freshness.render(
            [], "Sam", failed=[("/x/foo", "fatal: not possible to fast-forward")])
        self.assertIsNotNone(out)
        self.assertIn("foo", out)
        self.assertIn("not possible to fast-forward", out)

    def test_failed_says_pulled_at_and_refused_not_was_not_pulled(self):
        # The whole point of separating this from `blocked`: it must NOT use
        # blocked's "was NOT pulled" phrasing, which is false for a repo that
        # genuinely was.
        out = freshness.render([], "Sam", failed=[("/x/foo", "some reason")])
        self.assertIn("was pulled at and refused", out)
        self.assertNotIn("was NOT pulled", out)

    def test_every_failed_reason_states_nothing_was_changed(self):
        # Unlike `blocked`, ALL failed reasons get the reassurance --
        # --ff-only cannot leave a partial change behind no matter why it
        # refused.
        out = freshness.render(
            [], "Sam", failed=[("/x/foo", "some unstructured git error")])
        self.assertIn("nothing was changed", out.lower())

    def test_only_the_first_line_of_a_multiline_reason_is_shown(self):
        out = freshness.render(
            [], "Sam",
            failed=[("/x/foo", "fatal: refused\nhint: try rebasing\nhint: or merging")])
        self.assertIn("fatal: refused", out)
        self.assertNotIn("hint: try rebasing", out)

    def test_a_repo_in_both_reports_and_failed_appears_once_under_failed(self):
        out = freshness.render(
            [("/x/foo", freshness.KIND_TOOLKIT, 2, "main")], "Sam",
            failed=[("/x/foo", "some reason")])
        self.assertNotIn("foo is 2 commit(s) behind", out)
        self.assertEqual(out.count("foo"), 1)

    def test_blocked_and_failed_are_never_merged_into_one_section(self):
        out = _flat(freshness.render(
            [], "Sam",
            blocked=[("/x/dirty", "uncommitted changes")],
            failed=[("/x/diverged", "fatal: refused")]))
        self.assertIn("=== NOT SYNCED ===", out)
        self.assertIn("=== SYNC ATTEMPTED, NOT APPLIED ===", out)


class TestFailedFooterChoice(unittest.TestCase):

    _OFFER_PHRASE = "offer to run the command(s) above"
    _DIVERGED_PHRASE = "Do NOT offer a plain pull"

    def test_failed_only_gets_the_blocked_footer(self):
        out = _flat(freshness.render(
            [], "Sam", failed=[("/x/foo", "some reason")]))
        self.assertIn("left untouched", out)
        self.assertNotIn(self._OFFER_PHRASE, out)
        self.assertNotIn(self._DIVERGED_PHRASE, out)

    def test_stale_and_failed_together_keeps_the_ordinary_footer(self):
        out = _flat(freshness.render(
            [("/x/behind", freshness.KIND_TOOLKIT, 2, "main")], "Sam",
            failed=[("/x/other", "some reason")]))
        self.assertIn(self._OFFER_PHRASE, out)
        self.assertNotIn("left untouched", out)
        self.assertIn("other", out)


class TestUncheckedArguments(unittest.TestCase):
    """render()'s new keyword-only `unchecked` / `cloned_unchecked` counts --
    the one case that is NEWS on its own even with nothing else to report,
    since it is the only way to tell "nothing to do" apart from "ran out of
    time".
    """

    def test_no_unchecked_arguments_renders_exactly_as_before(self):
        reports = [("/x/repo", freshness.KIND_TOOLKIT, 2, "main")]
        self.assertEqual(freshness.render(reports, "Sam"),
                         freshness.render(reports, "Sam", unchecked=0,
                                           cloned_unchecked=0))

    def test_zero_unchecked_with_nothing_else_stays_none(self):
        self.assertIsNone(freshness.render([], "Sam", unchecked=0, cloned_unchecked=0))

    def test_a_nonzero_unchecked_is_worth_saying_alone(self):
        # No reports, no blocked, no failed -- this is the case a silent
        # `render()` would make indistinguishable from "nothing to do".
        out = freshness.render([], "Sam", unchecked=4)
        self.assertIsNotNone(out)
        self.assertIn("4", out)
        self.assertIn("ran out of time", out.lower())

    def test_a_nonzero_cloned_unchecked_is_worth_saying_alone(self):
        out = freshness.render([], "Sam", cloned_unchecked=2)
        self.assertIsNotNone(out)
        self.assertIn("2", out)
        self.assertIn("cloning", out.lower())

    def test_unchecked_and_cloned_unchecked_are_both_named_when_both_nonzero(self):
        out = freshness.render([], "Sam", unchecked=3, cloned_unchecked=1)
        self.assertIn("3", out)
        self.assertIn("1", out)

    def test_pure_time_budget_notice_gets_its_own_footer(self):
        # Neither the ordinary footer (nothing to offer) nor the blocked
        # footer (nothing was actually refused or skipped by name) fits.
        out = _flat(freshness.render([], "Sam", unchecked=1))
        self.assertIn("ran out of time", out.lower())
        self.assertNotIn("left untouched", out)
        self.assertNotIn("offer to run the command(s) above", out)

    def test_unchecked_alongside_a_blocked_repo_still_gets_the_blocked_footer(self):
        # A concrete, named repo survives, so the narrower time-budget
        # footer must not win over it.
        out = _flat(freshness.render(
            [], "Sam", blocked=[("/x/foo", "uncommitted changes")], unchecked=2))
        self.assertIn("left untouched", out)
        self.assertNotIn("ran out of time before finishing this session", out)


class TestGitTimeoutConstant(unittest.TestCase):

    def test_git_timeout_seconds_is_two(self):
        # Sized for measure_repos' worst case (4 blocking git calls per
        # repo); `TestWholeHookTimeoutBudget` in tests/test_sync_lib.py is
        # what actually enforces this, this is just a canary that the
        # constant did not drift back to 5.
        self.assertEqual(freshness.GIT_TIMEOUT_SECONDS, 2)


class TestDeadline(unittest.TestCase):
    """Deadline reads the clock ONLY through _now() -- every test here drives
    expiry by monkeypatching that function, never by sleeping."""

    def test_not_yet_expired(self):
        deadline = freshness.Deadline(100.0)
        with mock.patch.object(freshness, "_now", return_value=50.0):
            self.assertFalse(deadline.expired())

    def test_expired_exactly_at_the_deadline(self):
        deadline = freshness.Deadline(100.0)
        with mock.patch.object(freshness, "_now", return_value=100.0):
            self.assertTrue(deadline.expired())

    def test_expired_after_the_deadline(self):
        deadline = freshness.Deadline(100.0)
        with mock.patch.object(freshness, "_now", return_value=101.0):
            self.assertTrue(deadline.expired())

    def test_is_constructed_from_an_absolute_value_not_a_duration(self):
        # Built from an ALREADY-absolute time.monotonic() value the caller
        # computed (e.g. _now() + budget) -- construction itself must never
        # read the clock, so building it under one fake `_now()` and reading
        # it under a different one must not shift what it means.
        with mock.patch.object(freshness, "_now", return_value=1000.0):
            deadline = freshness.Deadline(1060.0)
        with mock.patch.object(freshness, "_now", return_value=1059.0):
            self.assertFalse(deadline.expired())
        with mock.patch.object(freshness, "_now", return_value=1060.0):
            self.assertTrue(deadline.expired())


class TestSpawnDueFetches(unittest.TestCase):
    """spawn_due_fetches: the fetch half of the report loop, extracted."""

    def test_takes_no_deadline_parameter(self):
        params = inspect.signature(freshness.spawn_due_fetches).parameters
        self.assertNotIn("deadline", params)

    def test_accepts_the_exact_object_watched_repos_returns(self):
        pairs = [("/repo/a", freshness.KIND_TOOLKIT),
                 ("/repo/b", freshness.KIND_GENERIC)]
        with mock.patch.object(freshness, "watched_repos", return_value=pairs), \
             mock.patch.object(freshness, "should_fetch", return_value=True), \
             mock.patch.object(freshness, "spawn_background_fetch",
                               return_value=True) as spawn:
            count = freshness.spawn_due_fetches(freshness.watched_repos())
        self.assertEqual(count, 2)
        spawn.assert_any_call("/repo/a")
        spawn.assert_any_call("/repo/b")
        for call in spawn.call_args_list:
            # spawn_background_fetch must receive the PATH string, never the
            # (path, kind) tuple.
            self.assertIsInstance(call.args[0], str)

    def test_spawns_for_every_due_repo_and_no_others(self):
        pairs = [("/repo/a", freshness.KIND_TOOLKIT),
                 ("/repo/b", freshness.KIND_GENERIC)]
        ages = {"/repo/a": 10, "/repo/b": 99999}

        def fake_seconds_since_fetch(repo, now=None):
            return ages[repo]

        with mock.patch.object(freshness, "seconds_since_fetch",
                                side_effect=fake_seconds_since_fetch), \
             mock.patch.object(freshness, "spawn_background_fetch",
                               return_value=True) as spawn:
            count = freshness.spawn_due_fetches(pairs, interval=100)
        spawn.assert_called_once_with("/repo/b")
        self.assertEqual(count, 1)

    def test_resolves_interval_via_fetch_interval_when_none(self):
        pairs = [("/repo/a", freshness.KIND_TOOLKIT)]
        with mock.patch.object(freshness, "fetch_interval", return_value=42) as fi, \
             mock.patch.object(freshness, "seconds_since_fetch", return_value=100), \
             mock.patch.object(freshness, "should_fetch",
                               return_value=True) as should_fetch, \
             mock.patch.object(freshness, "spawn_background_fetch",
                               return_value=True):
            freshness.spawn_due_fetches(pairs)
        fi.assert_called_once()
        should_fetch.assert_called_once_with(100, 42)


class TestMeasureRepos(GitRepoTestCase):
    """measure_repos: the report loop, extracted and deadline-bounded."""

    def _second_clone(self, name="clone2"):
        second = os.path.join(self._tmp, name)
        subprocess.run(("git", "clone", "-q", self.remote, second), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(second, "config", "user.email", "t@example.com")
        git(second, "config", "user.name", "T")
        return second

    def test_an_already_expired_deadline_makes_zero_git_calls(self):
        repos = [(self.clone, freshness.KIND_TOOLKIT)]
        already_expired = freshness.Deadline(float("-inf"))
        with mock.patch.object(freshness, "_git") as git_call:
            reports, unmeasured = freshness.measure_repos(repos, already_expired)
        git_call.assert_not_called()
        self.assertEqual(reports, [])
        self.assertEqual(unmeasured, len(repos))

    def test_matches_the_current_inline_loop_ordering_and_shape(self):
        # repo 1: one commit behind, not diverged.
        self.push_new_commit("second")
        git(self.clone, "fetch", "-q", "origin")
        # repo 2: freshly cloned, perfectly in sync.
        second = self._second_clone()

        repos = [(self.clone, freshness.KIND_TOOLKIT),
                 (second, freshness.KIND_GENERIC)]
        never_expiring = freshness.Deadline(float("inf"))
        reports, unmeasured = freshness.measure_repos(repos, never_expiring)

        expected = [
            (self.clone, freshness.KIND_TOOLKIT, 1, "main", 0),
            (second, freshness.KIND_GENERIC, 0, "main", 0),
        ]
        self.assertEqual(reports, expected)
        self.assertEqual(unmeasured, 0)

    def test_captures_a_diverged_repos_ahead_count(self):
        second = self._second_clone()
        commit(second, "local-only")
        self.push_new_commit("second")
        git(second, "fetch", "-q", "origin")

        repos = [(second, freshness.KIND_TOOLKIT)]
        never_expiring = freshness.Deadline(float("inf"))
        reports, unmeasured = freshness.measure_repos(repos, never_expiring)

        self.assertEqual(reports, [(second, freshness.KIND_TOOLKIT, 1, "main", 1)])
        self.assertEqual(unmeasured, 0)

    def test_a_deadline_expiring_partway_leaves_the_rest_unmeasured(self):
        second = self._second_clone()
        repos = [(self.clone, freshness.KIND_TOOLKIT),
                 (second, freshness.KIND_GENERIC)]

        calls = {"n": 0}

        def fake_now():
            calls["n"] += 1
            # Not expired before the FIRST repo; expired before the second.
            return 0.0 if calls["n"] == 1 else 100.0

        deadline = freshness.Deadline(50.0)
        with mock.patch.object(freshness, "_now", side_effect=fake_now):
            reports, unmeasured = freshness.measure_repos(repos, deadline)

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0][0], self.clone)
        self.assertEqual(unmeasured, 1)
        self.assertEqual(len(reports) + unmeasured, len(repos))

    def test_a_repo_filtered_by_should_report_still_counts_as_considered(self):
        # On a feature branch, should_report is False -- the repo is
        # MEASURED (it earns no report entry) but must not count toward
        # `unmeasured`, which is only for repos the deadline never STARTED.
        git(self.clone, "checkout", "-q", "-b", "feat/thing")
        repos = [(self.clone, freshness.KIND_TOOLKIT)]
        never_expiring = freshness.Deadline(float("inf"))
        reports, unmeasured = freshness.measure_repos(repos, never_expiring)
        self.assertEqual(reports, [])
        self.assertEqual(unmeasured, 0)


class TestUnmeasuredArgument(unittest.TestCase):
    """render()'s new keyword-only `unmeasured` count -- the REPORT phase's
    own overrun, distinct from `unchecked`/`cloned_unchecked` which belong to
    the SYNC phase. `render` stays pure; nothing here reads a clock or git.
    """

    def test_render_never_touches_the_world(self):
        # PURE means PURE: every escape hatch a hook might reach for is
        # monkeypatched to raise, and render() must never call any of them.
        with mock.patch.object(freshness.subprocess, "run",
                                side_effect=AssertionError("subprocess.run")), \
             mock.patch("builtins.open",
                        side_effect=AssertionError("open")), \
             mock.patch.object(freshness.time, "monotonic",
                               side_effect=AssertionError("time.monotonic")), \
             mock.patch.object(freshness, "_git",
                               side_effect=AssertionError("_git")), \
             mock.patch.object(freshness, "default_branch",
                               side_effect=AssertionError("default_branch")), \
             mock.patch.object(freshness, "commits_behind",
                               side_effect=AssertionError("commits_behind")), \
             mock.patch.object(freshness, "commits_ahead",
                               side_effect=AssertionError("commits_ahead")), \
             mock.patch.object(freshness, "current_branch",
                               side_effect=AssertionError("current_branch")):
            out = freshness.render(
                [("/x/behind", freshness.KIND_TOOLKIT, 2, "main")], "Sam",
                blocked=[("/x/foo", "uncommitted changes")],
                failed=[("/x/bar", "some reason")],
                unchecked=2, cloned_unchecked=1, unmeasured=3)
        self.assertIsNotNone(out)

    def test_zero_unmeasured_with_nothing_else_stays_none(self):
        self.assertIsNone(freshness.render([], "Sam", unmeasured=0))

    def test_pure_unmeasured_notice_names_no_sync_and_states_the_count(self):
        # Acceptance: the sync phase never ran on this machine (the
        # default), so a notice built ONLY from `unmeasured` must never say
        # "sync" in any case, and must say staleness is unknown, not that
        # anything was pulled, attempted, or skipped.
        out = freshness.render([], "Sam", unmeasured=3, blocked=None,
                                failed=None, unchecked=0, cloned_unchecked=0)
        self.assertIsNotNone(out)
        self.assertNotIn("sync", out.lower())
        self.assertIn("3", out)
        self.assertIn("staleness", out.lower())
        self.assertIn("unknown", out.lower())

    def test_unmeasured_and_unchecked_render_as_distinct_statements(self):
        # Both facts are real and different -- neither line may swallow the
        # other's substring, or a human reading past one hides the other.
        out = freshness.render([], "Sam", unmeasured=2, unchecked=4)
        unmeasured_line = freshness._UNMEASURED_LINE.format(2)
        unchecked_line = freshness._UNCHECKED_LINE.format(4)
        self.assertIn(unmeasured_line, out)
        self.assertIn(unchecked_line, out)
        self.assertNotIn(unmeasured_line, unchecked_line)
        self.assertNotIn(unchecked_line, unmeasured_line)


class TestUnmeasuredFooterChoice(unittest.TestCase):
    """Every reachable row of the footer-selection table gets its own test,
    read on the footer TEXT -- `assertIsNotNone` alone would not catch a
    footer that names the wrong phase or asserts only half of two true
    facts. The exhaustive row list is the five tests below, each labelled
    "Row N" in its own comment: any-cause present, nothing at all,
    unchecked-only, unmeasured-only, and both unchecked and unmeasured
    together.
    """

    _OFFER_PHRASE = "offer to run the command(s) above"
    _DIVERGED_PHRASE = "Do NOT offer a plain pull"
    _BLOCKED_PHRASE = "left untouched"
    _SYNC_TIME_BUDGET_PHRASE = "the sync phase ran out of time"

    def test_any_present_keeps_the_existing_footer_unchanged(self):
        # Row 1: stale/blocked/failed present -- unmeasured must not alter
        # which footer wins, whatever else is also nonzero.
        out = _flat(freshness.render(
            [("/x/repo", freshness.KIND_TOOLKIT, 2, "main")], "Sam",
            unmeasured=5, unchecked=1))
        self.assertIn(self._OFFER_PHRASE, out)
        self.assertNotIn(self._BLOCKED_PHRASE, out)

        out = _flat(freshness.render(
            [], "Sam", blocked=[("/x/foo", "uncommitted changes")],
            unmeasured=5))
        self.assertIn(self._BLOCKED_PHRASE, out)
        self.assertNotIn(self._OFFER_PHRASE, out)

        out = _flat(freshness.render(
            [], "Sam", failed=[("/x/foo", "some reason")], unmeasured=5))
        self.assertIn(self._BLOCKED_PHRASE, out)

    def test_none_zero_zero_is_none(self):
        # Row 2: nothing at all -- no notice.
        self.assertIsNone(freshness.render(
            [], "Sam", blocked=None, failed=None, unchecked=0,
            cloned_unchecked=0, unmeasured=0))

    def test_none_unchecked_only_keeps_the_sync_time_budget_footer(self):
        # Row 3: only unchecked/cloned_unchecked -- unchanged behaviour.
        out = _flat(freshness.render([], "Sam", unchecked=3, unmeasured=0))
        self.assertIn(self._SYNC_TIME_BUDGET_PHRASE, out)

    def test_none_unmeasured_only_gets_the_new_unmeasured_footer(self):
        # Row 4: only unmeasured -- the new footer, naming no phase.
        out = _flat(freshness.render([], "Sam", unmeasured=2))
        self.assertNotIn("sync", out.lower())
        self.assertIn("staleness is unknown", out)
        self.assertNotIn(self._BLOCKED_PHRASE, out)
        self.assertNotIn(self._OFFER_PHRASE, out)

    # Wording that appears in `_FOOTER_UNMEASURED_AND_TIME_BUDGET` and in
    # NEITHER single-cause footer. This is the discriminating assertion for
    # the combined case: a regression that silently falls back to `_FOOTER_UNMEASURED`
    # (deleting the combined-footer `elif` branch, leaving the bare
    # `elif unmeasured:` to catch the case) still satisfies "mentions
    # staleness" and "mentions nothing pulled" -- `_FOOTER_UNMEASURED` says
    # both of those on its own -- so only a phrase unique to the COMBINED
    # footer can tell the two apart.
    _COMBINED_UNIQUE_PHRASE = "ran out of time on two different things"

    def test_both_unchecked_and_unmeasured_get_a_footer_naming_both_facts(self):
        # Row 5, the reachable-in-practice case: sync enabled (unchecked>0)
        # AND the report phase's own deadline also stopped it (unmeasured>0),
        # with no stale/blocked/failed at all. The footer must not pick
        # ONE single-cause footer and go silent on the other half.
        out = _flat(freshness.render([], "Sam", unchecked=4, unmeasured=2))
        self.assertIn(self._COMBINED_UNIQUE_PHRASE, out,
                      "does not use the COMBINED footer's own wording -- a "
                      "silent fallback to a single-cause footer would still "
                      "pass the two checks below")
        self.assertNotEqual(out.count("nothing was pulled or attempted"), 0,
                             "does not state the sync half at all")
        self.assertIn("staleness is unknown", out,
                      "does not state the unmeasured half at all")
        # Neither single-cause footer's own body may stand in alone. Both
        # sides are flattened here -- `out` already is, and comparing it
        # against an UN-flattened multi-line footer constant can never
        # match regardless of what actually rendered, which is exactly the
        # bug this replaces (previously inert: a raw-newline needle against
        # a flattened haystack).
        self.assertNotIn(_flat(freshness._FOOTER_TIME_BUDGET.format(owner="Sam")), out)
        self.assertNotIn(_flat(freshness._FOOTER_UNMEASURED.format(owner="Sam")), out)

    def test_both_cloned_unchecked_and_unmeasured_also_get_the_combined_footer(self):
        # Same row, via the OTHER sync-phase counter -- the condition is
        # "unchecked OR cloned_unchecked", not "unchecked" specifically.
        out = _flat(freshness.render([], "Sam", cloned_unchecked=1, unmeasured=1))
        self.assertIn(self._COMBINED_UNIQUE_PHRASE, out)
        self.assertIn("staleness is unknown", out)
        self.assertNotEqual(out.count("nothing was pulled or attempted"), 0)
        self.assertNotIn(_flat(freshness._FOOTER_TIME_BUDGET.format(owner="Sam")), out)
        self.assertNotIn(_flat(freshness._FOOTER_UNMEASURED.format(owner="Sam")), out)


class TestUnmeasuredDoesNotChangeExistingBehaviour(unittest.TestCase):
    """`unmeasured=0` (the default) must render BYTE-FOR-BYTE identical to
    calling render() without the argument at all, across every existing
    footer branch -- comparing the two calls alone is vacuous (both take the
    default and the identical path), so each expected string below was
    captured from the pre-change `render()` and is pinned as a literal.
    """

    def test_stale_only(self):
        reports = [("/x/repo", freshness.KIND_TOOLKIT, 2, "main")]
        expected = (
            "=== REPO UPDATE AVAILABLE ===\n\n"
            "repo is 2 commit(s) behind origin/main — hooks, skills and "
            "agents load from this checkout and its ~/.local/bin tools are "
            "symlinks into it, so this session is running the OLDER code.\n"
            "  /x/repo/install.sh --update\n\n"
            "Say this to Sam once, near the start, and offer to run the "
            "command(s) above in\n"
            "the same breath — they are fast-forwards, so the answer is "
            "nearly always yes, but\n"
            "it is still theirs to give. Then carry on with what they "
            "actually asked for: this\n"
            "is a one-line offer, not a reason to stop. Do not repeat it, "
            "and do not pull\n"
            "without being told to.\n"
        )
        self.assertEqual(freshness.render(reports, "Sam"), expected)
        self.assertEqual(freshness.render(reports, "Sam", unmeasured=0), expected)

    def test_blocked_only(self):
        expected = (
            "=== NOT SYNCED ===\n\n"
            "foo was NOT pulled — uncommitted changes; nothing was changed "
            "in this repo.\n\n"
            "Say this to Sam once, near the start: the repo(s) above were "
            "left\n"
            "untouched — either the sync phase never attempted them, or it "
            "tried and a\n"
            "fast-forward was refused; either way nothing changed. Getting "
            "to a clean\n"
            "state is Sam's call: commit or stash local changes, reattach "
            "a detached\n"
            "HEAD, or resolve a diverged or refused pull by hand, whichever "
            "applies. Then\n"
            "carry on with what they actually asked for: this is a one-line "
            "note, not a\n"
            "reason to stop. Do not repeat it, and do not pull without "
            "being told to.\n"
        )
        out = freshness.render([], "Sam", blocked=[("/x/foo", "uncommitted changes")])
        out_unmeasured = freshness.render(
            [], "Sam", blocked=[("/x/foo", "uncommitted changes")], unmeasured=0)
        self.assertEqual(out, expected)
        self.assertEqual(out_unmeasured, expected)

    def test_failed_only(self):
        expected = (
            "=== SYNC ATTEMPTED, NOT APPLIED ===\n\n"
            "foo was pulled at and refused — some reason; nothing was "
            "changed in this repo (a fast-forward either applies cleanly "
            "or not at all).\n\n"
            "Say this to Sam once, near the start: the repo(s) above were "
            "left\n"
            "untouched — either the sync phase never attempted them, or it "
            "tried and a\n"
            "fast-forward was refused; either way nothing changed. Getting "
            "to a clean\n"
            "state is Sam's call: commit or stash local changes, reattach "
            "a detached\n"
            "HEAD, or resolve a diverged or refused pull by hand, whichever "
            "applies. Then\n"
            "carry on with what they actually asked for: this is a one-line "
            "note, not a\n"
            "reason to stop. Do not repeat it, and do not pull without "
            "being told to.\n"
        )
        out = freshness.render([], "Sam", failed=[("/x/foo", "some reason")])
        out_unmeasured = freshness.render(
            [], "Sam", failed=[("/x/foo", "some reason")], unmeasured=0)
        self.assertEqual(out, expected)
        self.assertEqual(out_unmeasured, expected)

    def test_unchecked_only(self):
        expected = (
            "=== SYNC INCOMPLETE ===\n\n"
            "Ran out of time before checking 4 already-cloned repo(s) for "
            "updates — their status this session is unknown, not "
            "necessarily clean.\n\n"
            "Say this to Sam once, near the start: the sync phase ran out "
            "of time\n"
            "before finishing this session — nothing was changed either "
            "way, it simply\n"
            "was not reached. Then carry on with what they actually asked "
            "for: this is a\n"
            "one-line note, not a reason to stop. Do not repeat it, and do "
            "not pull\n"
            "without being told to.\n"
        )
        out = freshness.render([], "Sam", unchecked=4)
        out_unmeasured = freshness.render([], "Sam", unchecked=4, unmeasured=0)
        self.assertEqual(out, expected)
        self.assertEqual(out_unmeasured, expected)

    def test_mixed(self):
        expected = (
            "=== REPO UPDATE AVAILABLE ===\n\n"
            "behind is 2 commit(s) behind origin/main — hooks, skills and "
            "agents load from this checkout and its ~/.local/bin tools are "
            "symlinks into it, so this session is running the OLDER code.\n"
            "  /x/behind/install.sh --update\n\n"
            "=== NOT SYNCED ===\n\n"
            "other was NOT pulled — uncommitted changes; nothing was "
            "changed in this repo.\n\n"
            "Say this to Sam once, near the start, and offer to run the "
            "command(s) above in\n"
            "the same breath — they are fast-forwards, so the answer is "
            "nearly always yes, but\n"
            "it is still theirs to give. Then carry on with what they "
            "actually asked for: this\n"
            "is a one-line offer, not a reason to stop. Do not repeat it, "
            "and do not pull\n"
            "without being told to.\n"
        )
        reports = [("/x/behind", freshness.KIND_TOOLKIT, 2, "main")]
        kwargs = {"blocked": [("/x/other", "uncommitted changes")]}
        out = freshness.render(reports, "Sam", **kwargs)
        out_unmeasured = freshness.render(reports, "Sam", unmeasured=0, **kwargs)
        self.assertEqual(out, expected)
        self.assertEqual(out_unmeasured, expected)

    def test_empty(self):
        self.assertIsNone(freshness.render([], "Sam"))
        self.assertIsNone(freshness.render([], "Sam", unmeasured=0))

    def test_render_all_returns_none_still(self):
        self.assertIsNone(freshness.render([], unmeasured=0))


class TestDefaultBranchTimeout(GitRepoTestCase):
    """default_branch's optional `timeout` overrides GIT_TIMEOUT_SECONDS for
    one call -- added for merge_brief_lib, an out-of-tool caller."""

    def test_no_timeout_uses_the_module_constant(self):
        with mock.patch.object(freshness.subprocess, "run",
                               wraps=freshness.subprocess.run) as run:
            freshness.default_branch(self.clone)
        used = run.call_args.kwargs["timeout"]
        self.assertEqual(used, freshness.GIT_TIMEOUT_SECONDS)

    def test_an_explicit_timeout_overrides_the_module_constant(self):
        with mock.patch.object(freshness.subprocess, "run",
                               wraps=freshness.subprocess.run) as run:
            freshness.default_branch(self.clone, timeout=5)
        used = run.call_args.kwargs["timeout"]
        self.assertEqual(used, 5)
        self.assertNotEqual(used, freshness.GIT_TIMEOUT_SECONDS)


if __name__ == "__main__":
    unittest.main()
