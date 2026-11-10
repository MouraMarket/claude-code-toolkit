#!/usr/bin/env python3
"""Tests for lib/toolkit_update.py: `version`, `update` and `rollback`.

Pure git fixtures, no install.sh. Each test builds a bare origin, a `dev`
clone that pushes new commits, and the `repo` checkout under test.
"""

import contextlib
import functools
import io
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB)
import install_record as ir  # noqa: E402
import settings_io  # noqa: E402
import toolkit_update as tu  # noqa: E402

SCRIPT = os.path.join(LIB, "toolkit_update.py")
NOW = "2026-10-13T10:00:00Z"


def _clean_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com",
                "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com",
                "GIT_AUTHOR_DATE": "2026-01-02T03:04:05Z",
                "GIT_COMMITTER_DATE": "2026-01-02T03:04:05Z"})
    return env


def git(cwd, *args, check=True):
    out = subprocess.run(["git", "-C", cwd] + list(args), capture_output=True, encoding="utf-8",
                         stdin=subprocess.DEVNULL, env=_clean_env(), timeout=60)
    if check and out.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), out.stderr))
    return out.stdout.strip()


def profile(plugin, dirname, default):
    return {"plugin": plugin, "dir": dirname, "summary": "s", "default": default,
            "agents": False, "requires": []}


def registry_json(*extra):
    """A profiles.json holding `core` plus one non-default profile per name in `extra`."""
    profiles = {"core": profile("toolkit-core", "plugin", True)}
    for name in extra:
        profiles[name] = profile(name, "plugin-" + name, False)
    return json.dumps({"profiles": profiles})


def read_bytes(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


class UpdateTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = os.path.realpath(tmp.name)
        self.origin = os.path.join(self.root, "origin.git")
        self.dev = os.path.join(self.root, "dev")
        self.repo = os.path.join(self.root, "repo")
        self.home = os.path.join(self.root, "home")
        self.rec = ir.record_path(self.home)
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", self.origin],
                       check=True, env=_clean_env(), stdin=subprocess.DEVNULL)
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(["git", "init", "-q", "-b", "main", self.dev],
                       check=True, env=_clean_env(), stdin=subprocess.DEVNULL)
        git(self.dev, "remote", "add", "origin", self.origin)
        # The first commit carries a profile registry: a rollback reads the
        # target commit's. It is staged here and committed with a.txt below.
        os.makedirs(os.path.join(self.dev, "claude"))
        with open(os.path.join(self.dev, "claude", "profiles.json"), "w") as handle:
            handle.write(registry_json())
        git(self.dev, "add", "claude/profiles.json")
        self.a = self.push("a.txt", "one\n")
        subprocess.run(["git", "clone", "-q", self.origin, self.repo],
                       check=True, env=_clean_env(), stdin=subprocess.DEVNULL)
        git(self.repo, "remote", "set-head", "origin", "main")

    def push(self, name, content):
        """A new commit on origin/main, made through the dev clone."""
        path = os.path.join(self.dev, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(content)
        git(self.dev, "add", name)
        git(self.dev, "commit", "-q", "-m", "add " + name)
        git(self.dev, "push", "-q", "origin", "main")
        return git(self.dev, "rev-parse", "HEAD")

    def head(self, repo=None):
        return git(repo or self.repo, "rev-parse", "HEAD")

    def installed(self, commit=None):
        ir.finish(self.rec, self.repo, commit or self.head(), ["builder"], "2026-10-01T00:00:00Z")

    def lock_record_dir(self):
        """Make the record's directory unwritable (the caller skips as root)."""
        os.makedirs(os.path.dirname(self.rec), exist_ok=True)
        os.chmod(os.path.dirname(self.rec), 0o500)
        self.addCleanup(os.chmod, os.path.dirname(self.rec), 0o700)

    def snapshot(self):
        return (self.head(), git(self.repo, "for-each-ref", "refs/heads"), read_bytes(self.rec))

    def write_settings(self, data):
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        with open(self.settings_path, "w") as handle:
            json.dump(data, handle)

    def live_settings(self):
        with open(self.settings_path) as handle:
            return json.load(handle)

    # The origin `update` must find. The fixture's origin is a local path, so
    # every update names it; a test that repoints origin sets this to match.
    expected_origin = None

    def run_fn(self, fn, repo=None, run_cli=None):
        out, err = io.StringIO(), io.StringIO()
        kwargs = {}
        if fn is tu.update:
            kwargs["expected_origin"] = self.expected_origin or self.origin
        if fn is tu.rollback and run_cli is not None:
            kwargs["run_cli"] = run_cli
        if fn in (tu.update, tu.rollback):
            fn = functools.partial(fn, home=self.home)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fn(repo or self.repo, self.rec, **kwargs)
        return code, out.getvalue(), err.getvalue()

    def version(self, repo=None):
        before = (read_bytes(self.rec), self.head(repo))
        code, out, _ = self.run_fn(tu.version, repo)
        self.assertEqual(code, 0)
        self.assertEqual((read_bytes(self.rec), self.head(repo)), before)
        return out

    def update_to_tip(self):
        """Advance origin one commit and update to it. Returns (old, new)."""
        old = self.head()
        new = self.push("b%s.txt" % old[:6], "x\n")
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, 0, err)
        return old, new


class TestVersion(UpdateTestCase):
    def test_on_main_with_matching_record(self):
        self.installed()
        out = self.version()
        short = self.head()[:12]
        self.assertIn("Toolkit version: %s (2026-01-02) add a.txt\n" % short, out)
        self.assertIn("State: on main\n", out)
        self.assertIn("Local changes: none\n", out)
        self.assertIn("Last install: %s at 2026-10-01T00:00:00Z (this commit)\n" % short, out)
        self.assertIn("Behind origin/main: 0 commit(s), as of the last fetch\n", out)
        self.assertIn("Rollback point: none\n", out)
        self.assertNotIn("linked worktree", out)

    def test_record_commit_differs(self):
        self.installed(self.a)
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "local")
        out = self.version()
        self.assertIn("Last install: %s at 2026-10-01T00:00:00Z; the checkout has moved since, "
                      "run ./install.sh\n" % self.a[:12], out)

    def test_no_record(self):
        self.assertIn("Last install: none recorded\n", self.version())

    def test_ownership_only_record(self):
        ir.add_permissions(self.rec, {"allow": ["x"]})
        self.assertIn("Last install: none recorded\n", self.version())

    def test_behind_counts_fetched_commits(self):
        self.push("b.txt", "x\n")
        git(self.repo, "fetch", "-q", "origin")
        self.assertIn("Behind origin/main: 1 commit(s), as of the last fetch\n", self.version())

    def test_origin_ref_missing(self):
        git(self.repo, "update-ref", "-d", "refs/remotes/origin/main")
        self.assertIn("Behind origin/main: unknown (no origin/main ref)\n", self.version())

    def test_dirty(self):
        with open(os.path.join(self.repo, "a.txt"), "w") as handle:
            handle.write("changed\n")
        self.assertIn("Local changes: 1 tracked file(s) changed\n", self.version())

    def test_feature_branch(self):
        git(self.repo, "checkout", "-q", "-b", "feat")
        self.assertIn("State: on branch feat (updates track main)\n", self.version())

    def test_foreign_detached(self):
        git(self.repo, "checkout", "-q", "--detach", "HEAD")
        self.assertIn("State: detached HEAD, not a rollback pin\n", self.version())

    def test_pinned_and_rollback_point(self):
        old, new = self.update_to_tip()
        self.assertEqual(self.run_fn(tu.rollback)[0], 0)
        out = self.version()
        pinned_at = ir.rollback(ir.load(self.rec))["pinned_at"]
        self.assertIn("State: pinned at %s by ./install.sh --rollback since %s; "
                      "./install.sh --update returns to main\n" % (old[:12], pinned_at), out)
        self.assertIn("Rollback point: %s recorded " % old[:12], out)

    def test_pin_with_head_moved_is_not_a_pin(self):
        self.update_to_tip()
        self.run_fn(tu.rollback)
        git(self.repo, "checkout", "-q", "--detach", "main")
        self.assertIn("State: detached HEAD, not a rollback pin\n", self.version())

    def test_linked_worktree(self):
        wt = os.path.join(self.root, "wt")
        git(self.repo, "worktree", "add", "-q", wt, "-b", "wtb")
        out = self.version(wt)
        self.assertTrue(out.endswith("This is a linked worktree; the installed toolkit is the "
                                     "primary checkout.\n"), out)
        self.assertNotIn("linked worktree", self.version())

    def test_version_leaves_the_index_alone_when_mtime_is_bumped(self):
        target = os.path.join(self.repo, "a.txt")
        index = os.path.join(self.repo, ".git", "index")
        os.utime(target, (1_900_000_000, 1_900_000_000))
        before = read_bytes(index)
        self.version()
        self.assertEqual(read_bytes(index), before)


class TestUpdate(UpdateTestCase):
    def test_fast_forward_records_the_old_head(self):
        old, new = self.update_to_tip()
        self.assertEqual(self.head(), new)
        point = ir.rollback(ir.load(self.rec))
        self.assertEqual(point["commit"], old)
        self.assertIsNone(point["pinned_at"])

    def test_fast_forward_prints_the_move(self):
        old = self.head()
        new = self.push("b.txt", "x\n")
        _, out, _ = self.run_fn(tu.update)
        self.assertIn("Updating %s -> %s" % (old[:12], new[:12]), out)
        self.assertNotIn("setup-git.sh", out)

    def test_setup_git_notice_only_when_that_file_changed(self):
        self.push("scripts/setup-git.sh", "#!/bin/sh\n")
        _, out, _ = self.run_fn(tu.update)
        self.assertIn("scripts/setup-git.sh changed: run ./scripts/setup-git.sh once from this "
                      "folder.", out)

    def test_already_current_with_matching_record(self):
        self.installed()
        before = read_bytes(self.rec)
        code, out, _ = self.run_fn(tu.update)
        self.assertEqual(code, 10)
        self.assertIn("Already up to date at %s." % self.head()[:12], out)
        self.assertEqual(read_bytes(self.rec), before)

    def test_already_current_without_record(self):
        code, out, _ = self.run_fn(tu.update)
        self.assertEqual(code, 11)
        self.assertIn("Already at origin/main; running the install it never had.", out)
        self.assertIsNone(read_bytes(self.rec))

    def test_already_current_with_stale_record(self):
        self.installed(self.a)
        self.push("b.txt", "x\n")
        git(self.repo, "pull", "-q", "--ff-only")
        self.assertEqual(self.run_fn(tu.update)[0], 11)

    def move_origin_default_to_release(self):
        """origin's default becomes `release`; its `main` stays, frozen."""
        git(self.dev, "checkout", "-q", "-b", "release")
        with open(os.path.join(self.dev, "r.txt"), "w") as handle:
            handle.write("release\n")
        git(self.dev, "add", "r.txt")
        git(self.dev, "commit", "-q", "-m", "release")
        git(self.dev, "push", "-q", "origin", "release")
        git(self.origin, "symbolic-ref", "HEAD", "refs/heads/release")
        return git(self.dev, "rev-parse", "HEAD")

    def test_a_changed_default_branch_is_followed_not_ignored(self):
        self.installed()
        release = self.move_origin_default_to_release()
        before = self.snapshot()
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, 1)
        self.assertIn("on branch main; updates track release. Run `git fetch origin release` "
                      "and `git checkout release` first", err)
        self.assertEqual(self.snapshot(), before)
        # The advice, followed to the letter, gets there.
        git(self.repo, "fetch", "-q", "origin", "release")
        git(self.repo, "checkout", "-q", "release")
        self.assertIn(self.run_fn(tu.update)[0], (0, 11))
        self.assertEqual((git(self.repo, "branch", "--show-current"), self.head()),
                         ("release", release))

    def test_a_pin_returns_to_a_changed_default_branch(self):
        self.update_to_tip()
        self.run_fn(tu.rollback)
        release = self.move_origin_default_to_release()
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, 0, err)
        self.assertEqual((git(self.repo, "branch", "--show-current"), self.head()),
                         ("release", release))
        # --version never asks origin, so it reads the answer update cached.
        self.assertIn("State: on release\n", self.version())

    def test_a_failed_default_branch_cache_is_reported_not_silent(self):
        self.installed()
        self.move_origin_default_to_release()
        git(self.repo, "fetch", "-q", "origin", "release")
        git(self.repo, "checkout", "-q", "release")
        real_git = tu._git

        def refuse_set_head(repo, *args, **kwargs):
            if args[:3] == ("remote", "set-head", "origin"):
                return subprocess.CompletedProcess(args, 1, "", "cannot lock ref")
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(tu, "_git", refuse_set_head):
            code, _, err = self.run_fn(tu.update)
        self.assertIn(code, (0, 11), err)
        self.assertIn("could not record origin's default branch (release)", err)
        # Positive control: the warning is about the cache, and the cache is stale.
        self.assertIn("updates track main", self.version())

    def assert_refused(self, prefix_text):
        before = self.snapshot()
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Not updated: "), err)
        self.assertIn(prefix_text, err)
        self.assertEqual(self.snapshot(), before)

    def test_refuses_dirty(self):
        self.push("b.txt", "x\n")
        with open(os.path.join(self.repo, "a.txt"), "w") as handle:
            handle.write("changed\n")
        self.assert_refused("local changes")

    def test_refuses_feature_branch(self):
        git(self.repo, "checkout", "-q", "-b", "feat")
        self.assert_refused("on branch feat")

    def test_refuses_foreign_detached(self):
        git(self.repo, "checkout", "-q", "--detach", "HEAD")
        self.assert_refused("not a rollback pin")

    def test_refuses_diverged_main(self):
        self.push("b.txt", "x\n")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "local only")
        self.assert_refused("does not")

    def test_refuses_fetch_failure(self):
        missing = os.path.join(self.root, "missing")
        git(self.repo, "remote", "set-url", "origin", missing)
        self.expected_origin = missing
        self.assert_refused("could not fetch origin/main")

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_refuses_unwritable_record_before_moving(self):
        self.push("b.txt", "x\n")
        self.installed()
        self.lock_record_dir()
        self.assert_refused("could not write the install record")


class TestRefusedMoveKeepsTheRollbackPoint(UpdateTestCase):
    """A merge git refuses (an untracked file in the way) leaves HEAD where it
    was, so it must leave the rollback point where it was too: the point is
    written before the move, and a refused move puts the earlier one back."""

    def block_next_update(self):
        self.push("c.txt", "from origin\n")
        with open(os.path.join(self.repo, "c.txt"), "w") as handle:
            handle.write("untracked\n")

    def test_refused_merge_keeps_the_previous_rollback_point(self):
        old, new = self.update_to_tip()
        self.block_next_update()
        code, _, _ = self.run_fn(tu.update)
        self.assertEqual(code, 1)
        self.assertEqual(self.head(), new)
        self.assertEqual(ir.rollback(ir.load(self.rec))["commit"], old)

    def test_refused_first_update_leaves_no_rollback_point(self):
        self.installed()
        self.block_next_update()
        code, _, _ = self.run_fn(tu.update)
        self.assertEqual(code, 1)
        self.assertIsNone(ir.rollback(ir.load(self.rec)))
        self.assertIsNotNone(ir.load(self.rec))


class TestRollback(UpdateTestCase):
    def test_rollback_pins_at_the_recorded_commit(self):
        old, new = self.update_to_tip()
        code, out, _ = self.run_fn(tu.rollback)
        self.assertEqual(code, 0)
        self.assertIn("Rolling back to %s" % old[:12], out)
        self.assertEqual(self.head(), old)
        self.assertEqual(git(self.repo, "symbolic-ref", "--quiet", "HEAD", check=False), "")
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main"), new)
        self.assertIsInstance(ir.rollback(ir.load(self.rec))["pinned_at"], str)

    def test_second_rollback_stays_put(self):
        old, _ = self.update_to_tip()
        self.run_fn(tu.rollback)
        self.assertEqual(self.run_fn(tu.rollback)[0], 0)
        self.assertEqual(self.head(), old)

    def test_no_record_refuses(self):
        before = self.snapshot()
        code, _, err = self.run_fn(tu.rollback)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Not rolled back: "), err)
        self.assertEqual(self.snapshot(), before)

    def test_commit_absent_from_repo_refuses(self):
        ir.set_rollback(self.rec, "ab" * 20, NOW)
        before = self.snapshot()
        code, _, err = self.run_fn(tu.rollback)
        self.assertEqual(code, 1)
        self.assertIn("is not in this repo", err)
        self.assertEqual(self.snapshot(), before)

    def test_refuses_dirty_and_feature_branch(self):
        self.update_to_tip()
        git(self.repo, "checkout", "-q", "-b", "feat")
        before = self.snapshot()
        code, _, err = self.run_fn(tu.rollback)
        self.assertEqual((code, self.snapshot()), (1, before))
        self.assertIn("rollback works from main", err)
        git(self.repo, "checkout", "-q", "main")
        with open(os.path.join(self.repo, "a.txt"), "w") as handle:
            handle.write("changed\n")
        before = self.snapshot()
        code, _, err = self.run_fn(tu.rollback)
        self.assertEqual((code, self.snapshot()), (1, before))
        self.assertIn("local changes", err)


class TestReturnFromPin(UpdateTestCase):
    def test_update_from_the_pin_returns_to_main(self):
        old, new = self.update_to_tip()
        self.run_fn(tu.rollback)
        code, out, err = self.run_fn(tu.update)
        self.assertEqual(code, 0, err)
        self.assertEqual(git(self.repo, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.head(), new)
        point = ir.rollback(ir.load(self.rec))
        self.assertEqual(point["commit"], old)
        self.assertIsNone(point["pinned_at"])
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "main@{upstream}"),
                         "origin/main")
        self.assertIn("Updating %s -> %s" % (old[:12], new[:12]), out)

    def test_blocked_return_leaves_the_pin(self):
        old, new = self.update_to_tip()
        self.run_fn(tu.rollback)
        self.push("c.txt", "from origin\n")
        with open(os.path.join(self.repo, "c.txt"), "w") as handle:
            handle.write("untracked\n")
        before = (self.head(), git(self.repo, "rev-parse", "refs/heads/main"))
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, 1)
        self.assertEqual((self.head(), git(self.repo, "rev-parse", "refs/heads/main")), before)
        self.assertEqual(self.head(), old)
        self.assertEqual(git(self.repo, "rev-parse", "refs/heads/main"), new)
        self.assertIsNotNone(tu.read_state(self.repo, self.rec)["pin"])

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_unwritable_record_leaves_the_pin(self):
        old, _ = self.update_to_tip()
        self.run_fn(tu.rollback)
        pinned_at = ir.rollback(ir.load(self.rec))["pinned_at"]
        self.push("c.txt", "x\n")
        self.lock_record_dir()
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, 1)
        self.assertIn("could not write the install record", err)
        self.assertEqual(self.head(), old)
        self.assertEqual(ir.rollback(ir.load(self.rec))["pinned_at"], pinned_at)

    def test_refuses_when_local_main_is_not_behind_origin(self):
        self.update_to_tip()
        self.run_fn(tu.rollback)
        git(self.repo, "update-ref", "refs/heads/main", git(self.repo, "commit-tree", "-p",
            "refs/heads/main", "-m", "local only", "refs/heads/main^{tree}"))
        self.push("c.txt", "x\n")
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, 1)
        self.assertIn("does not", err)


class TestOriginPin(UpdateTestCase):
    """`update` checks origin's URL before it contacts origin at all."""

    def traced_update(self, expected):
        """Run update with every git call recorded; returns (code, err, argv list)."""
        calls, real = [], tu._git

        def tracing(repo, *args, **kwargs):
            calls.append(args)
            return real(repo, *args, **kwargs)

        tu._git = tracing
        try:
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = tu.update(self.repo, self.rec, self.home, expected_origin=expected)
        finally:
            tu._git = real
        return code, err.getvalue(), calls

    def assert_untouched_and_offline(self, before, calls):
        self.assertEqual(self.snapshot(), before)
        contacted = [c for c in calls if c and c[0] in ("ls-remote", "fetch")]
        self.assertEqual(contacted, [], "a refused origin was contacted: %r" % contacted)

    def test_matching_origin_updates(self):
        new = self.push("b.txt", "x\n")
        code, err, _ = self.traced_update(self.origin)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head(), new)

    def test_mismatch_refuses_before_contacting_origin(self):
        self.push("b.txt", "x\n")
        before = self.snapshot()
        expected = "https://github.com/someone/else"
        code, err, calls = self.traced_update(expected)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Not updated: origin is "), err)
        self.assertIn(self.origin, err)
        self.assertIn(expected, err)
        self.assertIn("--update --set update.origin=" + shlex.quote(self.origin), err)
        self.assertIn("git remote set-url origin " + expected, err)
        self.assert_untouched_and_offline(before, calls)

    def test_every_origin_url_must_match(self):
        self.push("b.txt", "x\n")
        git(self.repo, "config", "--add", "remote.origin.url", os.path.join(self.root, "other.git"))
        before = self.snapshot()
        code, err, calls = self.traced_update(self.origin)
        self.assertEqual(code, 1)
        self.assertIn("other.git", err)
        self.assert_untouched_and_offline(before, calls)

    def test_insteadof_rewrite_decides(self):
        # Configured text names a fake host; insteadOf rewrites it to the real origin.
        git(self.repo, "remote", "set-url", "origin", "fake:repo")
        git(self.repo, "config", "url.%s.insteadOf" % self.origin, "fake:repo")
        new = self.push("b.txt", "x\n")
        code, err, _ = self.traced_update(self.origin)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head(), new)
        # And the other way: configured text matches, the effective URL does not.
        git(self.repo, "remote", "set-url", "origin", self.origin)
        git(self.repo, "config", "--unset", "url.%s.insteadOf" % self.origin)
        elsewhere = os.path.join(self.root, "elsewhere.git")
        git(self.repo, "config", "url.%s.insteadOf" % elsewhere, self.origin)
        self.push("c.txt", "y\n")
        before = self.snapshot()
        code, err, calls = self.traced_update(self.origin)
        self.assertEqual(code, 1)
        self.assertIn("elsewhere.git", err)
        self.assert_untouched_and_offline(before, calls)

    def test_no_origin_remote(self):
        git(self.repo, "remote", "remove", "origin")
        before = self.snapshot()
        code, err, calls = self.traced_update(self.origin)
        self.assertEqual(code, 1)
        self.assertEqual(err, "Not updated: this checkout has no origin remote\n")
        self.assert_untouched_and_offline(before, calls)

    def test_version_reports_the_origin(self):
        def origin_line(expected):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(tu.version(self.repo, self.rec, expected_origin=expected), 0)
            return [line for line in out.getvalue().splitlines() if line.startswith("Origin:")]

        self.assertEqual(origin_line(self.origin),
                         ["Origin: %s (matches the pinned origin)" % self.origin])
        self.assertEqual(origin_line("https://github.com/someone/else"),
                         ["Origin: %s (does NOT match https://github.com/someone/else; "
                          "./install.sh --update will refuse)" % self.origin])
        git(self.repo, "remote", "remove", "origin")
        self.assertEqual(origin_line(self.origin), ["Origin: none"])


class TestOriginConfig(UpdateTestCase):
    """The expected origin from the toolkit config, through the real CLI."""

    def run_cli_update(self, config, path=None):
        home = os.path.join(self.root, "home")
        os.makedirs(home, exist_ok=True)
        env = dict(_clean_env(), HOME=home)
        if config is None:
            env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(self.root, "absent.json")
        else:
            path = path or os.path.join(self.root, "config.json")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(config if isinstance(config, str) else json.dumps(config))
            env["CLAUDE_TOOLKIT_CONFIG"] = path
        return subprocess.run([sys.executable, SCRIPT, "update", self.repo], capture_output=True,
                              encoding="utf-8", stdin=subprocess.DEVNULL, env=env, timeout=60)

    def test_the_refusal_names_a_command_that_makes_the_next_update_pass(self):
        # A fork user follows the refusal literally. The config sits under a
        # path with a space, so the printed command must quote it as one word.
        new = self.push("b.txt", "x\n")
        config = os.path.join(self.root, "my config", "config.json")
        refused = self.run_cli_update({"owner_name": "Sam"}, config)
        self.assertEqual(refused.returncode, 1)
        command = refused.stderr.split(" run: ", 1)[1].split(" ; otherwise", 1)[0]
        argv = shlex.split(command)
        self.assertEqual(argv, ["python3", os.path.join(self.repo, "lib", "setup_config.py"),
                                config, self.repo, "--update", "--set",
                                "update.origin=" + self.origin])
        # The fixture checkout is not a toolkit, so the real one stands in for
        # the script and the repo argument (it reads config.example.json there).
        toolkit = os.path.dirname(LIB)
        argv[1], argv[3] = os.path.join(LIB, "setup_config.py"), toolkit
        done = subprocess.run([sys.executable] + argv[1:], capture_output=True, encoding="utf-8",
                              stdin=subprocess.DEVNULL, env=_clean_env(), timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        with open(config) as handle:
            self.assertEqual(json.load(handle)["update"], {"origin": self.origin})
        home = os.path.join(self.root, "home")
        out = subprocess.run([sys.executable, SCRIPT, "update", self.repo], capture_output=True,
                             encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60,
                             env=dict(_clean_env(), HOME=home, CLAUDE_TOOLKIT_CONFIG=config))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), new)

    def test_config_override_is_honoured(self):
        new = self.push("b.txt", "x\n")
        out = self.run_cli_update({"update": {"origin": self.origin}})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), new)

    def test_absent_key_pins_the_public_origin(self):
        self.push("b.txt", "x\n")
        before = self.snapshot()
        for config in (None, {}, {"update": {}}):
            out = self.run_cli_update(config)
            self.assertEqual(out.returncode, 1, config)
            self.assertIn(tu.PUBLIC_ORIGIN, out.stderr)
            self.assertEqual(self.snapshot(), before)

    def test_malformed_key_refuses_without_falling_back(self):
        self.push("b.txt", "x\n")
        before = self.snapshot()
        for config in ({"update": {"origin": ""}}, {"update": {"origin": 5}},
                       {"update": {"origin": None}}, {"update": "x"}):
            out = self.run_cli_update(config)
            self.assertEqual(out.returncode, 1, config)
            self.assertIn("update.origin in your toolkit config must be a URL or path",
                          out.stderr, config)
            self.assertNotIn(tu.PUBLIC_ORIGIN, out.stderr)
            self.assertEqual(self.snapshot(), before)

    def test_broken_config_refuses(self):
        self.push("b.txt", "x\n")
        before = self.snapshot()
        out = self.run_cli_update("{not json")
        self.assertEqual(out.returncode, 1)
        self.assertIn("Not updated: cannot read your toolkit config", out.stderr)
        self.assertEqual(self.snapshot(), before)


class TestSnapshot(UpdateTestCase):
    """`update` records the live settings and the record's owned fields with the rollback point."""

    SL = {"type": "command", "command": "sl"}

    def state_after_update(self):
        self.update_to_tip()
        return ir.rollback_state(ir.load(self.rec))

    def test_records_what_the_settings_hold(self):
        self.write_settings({"permissions": {"allow": ["Bash(a:*)"], "deny": ["Bash(d:*)"],
                                             "defaultMode": "plan"},
                             "statusLine": self.SL, "claudeMdExcludes": ["p"]})
        ir.add_permissions(self.rec, {"allow": ["Bash(a:*)"]}, "plan", None)
        ir.add_exclude(self.rec, "p", True)
        live = self.state_after_update()["settings"]
        self.assertEqual(live["permissions"], {"allow": ["Bash(a:*)"], "ask": [], "deny": ["Bash(d:*)"]})
        self.assertEqual(live["defaultMode"], {"present": True, "value": "plan"})
        self.assertEqual(live["claudeMdExcludes"], {"present": True, "value": ["p"]})
        self.assertEqual(live["statusLine"], {"present": True, "value": self.SL})
        self.assertEqual(live["autoMemoryDirectory"], {"present": False})
        state = ir.rollback_state(ir.load(self.rec))
        self.assertEqual(state["permissions_added"]["allow"], ["Bash(a:*)"])
        self.assertEqual(state["default_mode"], {"set": "plan", "previous": None})
        self.assertEqual(state["claude_md_excludes_added"], {"pattern": "p", "created_key": True})

    def test_a_null_value_is_recorded_present(self):
        self.write_settings({"autoMemoryDirectory": None, "permissions": {"defaultMode": None}})
        live = self.state_after_update()["settings"]
        self.assertEqual(live["autoMemoryDirectory"], {"present": True, "value": None})
        self.assertEqual(live["defaultMode"], {"present": True, "value": None})

    def test_absent_settings_file_records_everything_absent(self):
        state = self.state_after_update()
        self.assertEqual(state["settings"], {
            "permissions": {"allow": [], "ask": [], "deny": []},
            "defaultMode": {"present": False}, "claudeMdExcludes": {"present": False},
            "statusLine": {"present": False}, "autoMemoryDirectory": {"present": False}})
        self.assertNotIn("default_mode", state)

    def assert_refused_untouched(self, content, code_expected=1):
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        with open(self.settings_path, "w") as handle:
            handle.write(content)
        before = (self.snapshot(), read_bytes(self.settings_path))
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, code_expected, err)
        self.assertTrue(err.startswith("Not updated: "), err)
        self.assertIn(self.settings_path, err)
        self.assertEqual((self.snapshot(), read_bytes(self.settings_path)), before)

    def test_unreadable_settings_refuse_on_the_moving_path(self):
        self.installed()
        self.push("b.txt", "x\n")
        self.assert_refused_untouched("{not json")

    def test_non_object_settings_refuse_on_the_moving_path(self):
        self.installed()
        self.push("b.txt", "x\n")
        self.assert_refused_untouched("[1]")

    def test_unreadable_settings_refuse_when_already_current(self):
        self.installed()
        self.assert_refused_untouched("{not json")

    def test_unreadable_settings_refuse_when_at_origin_without_a_record(self):
        self.assert_refused_untouched("{not json")

    def test_from_a_pin_the_state_is_refreshed_and_the_commit_kept(self):
        old, _ = self.update_to_tip()
        self.run_fn(tu.rollback)
        self.write_settings({"statusLine": self.SL})
        self.push("later.txt", "x\n")
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, 0, err)
        entry = ir.rollback(ir.load(self.rec))
        self.assertEqual(entry["commit"], old)
        self.assertEqual(entry["state"]["settings"]["statusLine"], {"present": True, "value": self.SL})

    def test_the_snapshot_and_the_rollback_point_are_one_write_before_the_move(self):
        self.push("b.txt", "x\n")
        writes, real_write, real_git = [], ir._write, tu._git

        def counting_write(path, record):
            writes.append(path)
            return real_write(path, record)

        seen_at_merge = []

        def tracing_git(repo, *args, **kwargs):
            if args and args[0] == "merge":
                seen_at_merge.append(len(writes))
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(ir, "_write", counting_write), mock.patch.object(tu, "_git", tracing_git):
            self.assertEqual(self.run_fn(tu.update)[0], 0)
        self.assertEqual(seen_at_merge, [1])
        self.assertEqual(len(writes), 1)
        self.assertIsNotNone(ir.rollback_state(ir.load(self.rec)))


def _present(value):
    return {"present": True, "value": value}


class TestSnapshotEnableWorkflows(UpdateTestCase):
    """`snapshot_state` carries the ownership flag and always the live enableWorkflows entry."""

    def test_flag_copied_when_the_record_has_it_and_omitted_when_not(self):
        self.assertNotIn("enable_workflows_added", tu.snapshot_state({}, {}))
        record = {"enable_workflows_added": True}
        self.assertIs(tu.snapshot_state(record, {})["enable_workflows_added"], True)

    def test_base_records_presence_and_type(self):
        self.assertEqual(tu.snapshot_state({}, {})["enable_workflows_base"], {"present": False})
        for value in (False, True, 1):
            base = tu.snapshot_state({}, {"enableWorkflows": value})["enable_workflows_base"]
            self.assertEqual(base, {"present": True, "value": value})
            self.assertIs(type(base["value"]), type(value))

    def test_snapshot_passes_the_shape_check_and_restore_leaves_base_out(self):
        self.write_settings({"enableWorkflows": True})
        ir.set_workflows_owned(self.rec, True)
        state = self.state_after_update()
        self.assertIs(state["enable_workflows_added"], True)
        self.assertEqual(state["enable_workflows_base"], {"present": True, "value": True})
        self.assertNotIn("enable_workflows_base", ir.load(self.rec))

    def state_after_update(self):
        self.update_to_tip()
        return ir.rollback_state(ir.load(self.rec))


ABSENT = {"present": False}


class TestRestorePlan(unittest.TestCase):
    """The three-way rule, pure: base = snapshot, installed = the installer's value, current = now."""

    RULE = "Bash(probe:*)"
    SL = {"type": "command", "command": "sl"}

    def plan(self, before, now, record_before=None, record_now=None, installed=None):
        state = tu.snapshot_state(record_before, before)
        record = record_now if record_now is not None else record_before
        installed = installed or {"statusLine": None, "autoMemoryDirectory": None}
        return tu.restore_plan(record, state, now, installed)

    def kinds(self, actions):
        return [(a.kind, a.target) for a in actions]

    def test_a_rule_added_by_the_update_is_removed(self):
        record = {"permissions_added": {"allow": [self.RULE]}}
        actions, notes = self.plan({}, {"permissions": {"allow": [self.RULE]}}, {}, record)
        self.assertEqual(self.kinds(actions), [("remove-rule", "allow")])
        self.assertEqual(notes, [])

    def test_a_rule_live_before_and_after_is_kept(self):
        settings = {"permissions": {"allow": [self.RULE]}}
        record = {"permissions_added": {"allow": [self.RULE]}}
        self.assertEqual(self.plan(settings, settings, record, record), ([], []))

    def test_a_recorded_rule_the_user_removed_before_the_update_and_the_update_re_added_is_removed(self):
        # Plan Review finding 2: the record already owned the rule, so it is in
        # the snapshot's record half too; only the live lists show it was gone.
        record = {"permissions_added": {"allow": [self.RULE]}}
        actions, notes = self.plan({}, {"permissions": {"allow": [self.RULE]}}, record, record)
        self.assertEqual(self.kinds(actions), [("remove-rule", "allow")])
        self.assertEqual(notes, [])

    def test_a_rule_the_user_removed_needs_no_action(self):
        record = {"permissions_added": {"allow": [self.RULE]}}
        self.assertEqual(self.plan({}, {}, {}, record), ([], []))

    def test_unchanged_value_keys_do_nothing_whatever_the_installer_writes(self):
        settings = {"statusLine": self.SL, "autoMemoryDirectory": "~/m", "claudeMdExcludes": ["p"],
                    "permissions": {"defaultMode": "plan"}}
        record = {"default_mode": {"set": "auto", "previous": "plan"},
                  "claude_md_excludes_added": {"pattern": "p", "created_key": False}}
        installed = {"statusLine": _present({"x": 1}), "autoMemoryDirectory": ABSENT}
        self.assertEqual(self.plan(settings, settings, record, record, installed), ([], []))

    def test_default_mode_set_by_the_update_goes_back_to_absent(self):
        record = {"default_mode": {"set": "auto", "previous": None}}
        actions, notes = self.plan({}, {"permissions": {"defaultMode": "auto"}}, {}, record)
        self.assertEqual(self.kinds(actions), [("set-mode", "defaultMode")])
        self.assertEqual(actions[0].data, {"expected": "auto", "present": False, "value": None})
        self.assertEqual(notes, [])

    def test_default_mode_the_installer_re_wrote_over_a_changed_value(self):
        # The record is the same before and after; only the live value moved.
        record = {"default_mode": {"set": "auto", "previous": None}}
        actions, _ = self.plan({"permissions": {"defaultMode": "plan"}},
                               {"permissions": {"defaultMode": "auto"}}, record, record)
        self.assertEqual(actions[0].data, {"expected": "auto", "present": True, "value": "plan"})

    def test_a_present_null_default_mode_round_trips(self):
        record = {"default_mode": {"set": "auto", "previous": None}}
        before = {"permissions": {"defaultMode": None}}
        actions, _ = self.plan(before, {"permissions": {"defaultMode": "auto"}}, record, record)
        after = settings_io.apply_settings_actions({"permissions": {"defaultMode": "auto"}}, actions)
        self.assertEqual(after, before)

    def test_a_default_mode_the_user_changed_is_kept_and_named(self):
        record = {"default_mode": {"set": "auto", "previous": None}}
        actions, notes = self.plan({}, {"permissions": {"defaultMode": "plan"}}, {}, record)
        self.assertEqual(actions, [])
        self.assertEqual(notes, ["defaultMode changed since the update; left as you set it"])

    def test_an_exclude_pattern_the_update_added_is_removed(self):
        record = {"claude_md_excludes_added": {"pattern": "p", "created_key": True}}
        actions, _ = self.plan({}, {"claudeMdExcludes": ["p"]}, {}, record)
        self.assertEqual(self.kinds(actions), [("remove-exclude", "claudeMdExcludes")])
        self.assertEqual(actions[0].data, {"pattern": "p", "created_key": True})
        actions, _ = self.plan({"claudeMdExcludes": ["q"]}, {"claudeMdExcludes": ["q", "p"]}, {}, record)
        self.assertEqual(actions[0].data, {"pattern": "p", "created_key": False})

    def test_an_exclude_pattern_the_user_removed_is_named(self):
        record = {"claude_md_excludes_added": {"pattern": "p", "created_key": True}}
        actions, notes = self.plan({"claudeMdExcludes": ["p"]}, {"claudeMdExcludes": []}, record, record)
        self.assertEqual(actions, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("claudeMdExcludes was removed", notes[0])

    def test_no_recorded_pattern_means_no_exclude_action_or_note(self):
        self.assertEqual(self.plan({}, {"claudeMdExcludes": ["p"]}, {}, {}), ([], []))

    def test_a_status_line_the_update_added_is_removed(self):
        installed = {"statusLine": _present(self.SL), "autoMemoryDirectory": None}
        actions, notes = self.plan({}, {"statusLine": self.SL}, {}, {}, installed)
        self.assertEqual(self.kinds(actions), [("remove-key", "statusLine")])
        self.assertEqual(actions[0].data, {"expected": self.SL})
        self.assertEqual(notes, [])

    def test_a_status_line_the_update_replaced_is_put_back(self):
        old = {"type": "command", "command": "old"}
        installed = {"statusLine": _present(self.SL), "autoMemoryDirectory": None}
        actions, _ = self.plan({"statusLine": old}, {"statusLine": self.SL}, {}, {}, installed)
        self.assertEqual(self.kinds(actions), [("set-key", "statusLine")])
        self.assertEqual(actions[0].data, {"expected_present": True, "expected": self.SL, "value": old})

    def test_a_status_line_the_user_edited_is_kept_and_named(self):
        installed = {"statusLine": _present(self.SL), "autoMemoryDirectory": None}
        actions, notes = self.plan({}, {"statusLine": {"command": "mine"}}, {}, {}, installed)
        self.assertEqual(actions, [])
        self.assertEqual(notes, ["statusLine changed since the update; left as you set it"])

    def test_a_removed_value_is_named_as_removed(self):
        installed = {"statusLine": _present(self.SL), "autoMemoryDirectory": None}
        actions, notes = self.plan({"statusLine": self.SL}, {}, {}, {}, installed)
        self.assertEqual(actions, [])
        self.assertEqual(notes, ["statusLine was removed since the update; left removed "
                                 "(the install that follows may add it back)"])

    def test_an_unknown_installed_value_is_never_restored(self):
        for key, value in (("statusLine", self.SL), ("autoMemoryDirectory", "~/m")):
            actions, notes = self.plan({}, {key: value}, {}, {})
            self.assertEqual(actions, [], key)
            self.assertEqual(len(notes), 1, key)

    def test_auto_memory_the_update_set_is_removed_and_null_is_not_absent(self):
        installed = {"statusLine": None, "autoMemoryDirectory": _present("~/m")}
        actions, _ = self.plan({}, {"autoMemoryDirectory": "~/m"}, {}, {}, installed)
        self.assertEqual(self.kinds(actions), [("remove-key", "autoMemoryDirectory")])
        # Present null before, the managed value now: put back as null, not as absence.
        actions, _ = self.plan({"autoMemoryDirectory": None}, {"autoMemoryDirectory": "~/m"},
                               {}, {}, installed)
        self.assertEqual(actions[0].kind, "set-key")
        self.assertIsNone(actions[0].data["value"])
        # Unify off: the install leaves the key absent, so a present value is the user's.
        off = {"statusLine": None, "autoMemoryDirectory": ABSENT}
        actions, notes = self.plan({}, {"autoMemoryDirectory": "~/m"}, {}, {}, off)
        self.assertEqual((actions, len(notes)), ([], 1))


class TestRestorePlanEnableWorkflows(unittest.TestCase):
    """enableWorkflows rollback is three-way against the snapshot, never ownership alone."""

    OURS = {"enable_workflows_added": True}
    PREDATES = ("enableWorkflows: this rollback point predates the toolkit managing it; "
                "left as is \u2014 remove it yourself if you do not want it")
    CHANGED = "enableWorkflows changed since the update; left as you set it"

    def plan(self, before, now, record_now, legacy=False):
        state = tu.snapshot_state({}, before)
        if legacy:
            del state["enable_workflows_base"]
        return tu.restore_plan(record_now, state, now, {"statusLine": None, "autoMemoryDirectory": None})

    def test_base_absent_ours_true_removes_the_key(self):
        actions, notes = self.plan({}, {"enableWorkflows": True}, self.OURS)
        self.assertEqual(len(actions), 1)
        self.assertEqual((actions[0].kind, actions[0].target, actions[0].data),
                         ("remove-key", "enableWorkflows", {"expected": True}))
        self.assertEqual(notes, [])

    def test_base_true_user_removed_install_readded_is_left_alone(self):
        # Plan Review counterexample: current equals base, so nothing, though the record is ours.
        actions, notes = self.plan({"enableWorkflows": True}, {"enableWorkflows": True}, self.OURS)
        self.assertEqual((actions, notes), ([], []))

    def test_base_false_ours_true_sets_the_key_back_to_false(self):
        actions, notes = self.plan({"enableWorkflows": False}, {"enableWorkflows": True}, self.OURS)
        self.assertEqual(len(actions), 1)
        self.assertEqual((actions[0].kind, actions[0].target, actions[0].data),
                         ("set-key", "enableWorkflows",
                          {"expected_present": True, "expected": True, "value": False}))
        self.assertEqual(notes, [])

    def test_base_absent_ours_but_current_false_is_the_users_edit(self):
        actions, notes = self.plan({}, {"enableWorkflows": False}, self.OURS)
        self.assertEqual((actions, notes), ([], [self.CHANGED]))

    def test_not_ours_and_differs_from_base_is_noted_not_acted_on(self):
        actions, notes = self.plan({}, {"enableWorkflows": True}, {})
        self.assertEqual((actions, notes), ([], [self.CHANGED]))

    def test_legacy_snapshot_ours_and_true_gets_the_legacy_note(self):
        actions, notes = self.plan({}, {"enableWorkflows": True}, self.OURS, legacy=True)
        self.assertEqual((actions, notes), ([], [self.PREDATES]))

    def test_legacy_snapshot_not_ours_or_absent_says_nothing(self):
        self.assertEqual(self.plan({}, {"enableWorkflows": True}, {}, legacy=True), ([], []))
        self.assertEqual(self.plan({}, {}, self.OURS, legacy=True), ([], []))

    def test_base_absent_ours_current_one_is_not_what_the_install_wrote(self):
        actions, notes = self.plan({}, {"enableWorkflows": 1}, self.OURS)
        self.assertEqual((actions, notes), ([], [self.CHANGED]))

    def test_base_true_current_one_is_a_type_change_not_a_match(self):
        actions, notes = self.plan({"enableWorkflows": True}, {"enableWorkflows": 1}, {})
        self.assertEqual((actions, notes), ([], [self.CHANGED]))


class _StubConfig(object):
    """A toolkit config with nothing in it, so a test never reads the real one."""

    @staticmethod
    def get(*keys, default=None):
        return default


class FakeCli(object):
    def __init__(self, plugins=None, fail_list=False, fail_uninstall=False):
        self.plugins = plugins if plugins is not None else []
        self.fail_list, self.fail_uninstall = fail_list, fail_uninstall
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[:2] == ["plugin", "list"]:
            return (False, "boom") if self.fail_list else (True, json.dumps(self.plugins))
        if argv[:3] == ["plugin", "marketplace", "list"]:
            return (False, "boom") if self.fail_list else (True, "[]")
        if argv[:2] == ["plugin", "uninstall"]:
            return (False, "refused") if self.fail_uninstall else (True, "")
        raise AssertionError("unexpected claude call %r" % (argv,))


PROBE = "probe@claude-code-toolkit"
CORE = "toolkit-core@claude-code-toolkit"


class TestRollbackRestore(UpdateTestCase):
    """`rollback` undoes what the update's install changed, then moves the checkout."""

    RULE = "Bash(echo probe:*)"
    SL = {"type": "command", "command": "sl"}
    T0 = "2026-10-01T00:00:00Z"

    def setUp(self):
        super().setUp()
        patch = mock.patch.object(tu, "toolkit_config", _StubConfig)
        patch.start()
        self.addCleanup(patch.stop)

    def push_layout(self, core_depends_on=None):
        """The marketplace and plugin manifests the dependency check reads."""
        self.push(".claude-plugin/marketplace.json", json.dumps({"plugins": [
            {"name": "toolkit-core", "source": "plugin"}, {"name": "probe", "source": "plugin-probe"}]}))
        manifest = {"name": "toolkit-core"}
        if core_depends_on:
            manifest["dependencies"] = [core_depends_on]
        self.push("plugin/.claude-plugin/plugin.json", json.dumps(manifest))
        self.push("plugin-probe/.claude-plugin/plugin.json", json.dumps({"name": "probe"}))

    def scenario(self, layout=True, core_depends_on=None, profiles=("core", "probe")):
        """A at install, B pushed and updated to, B's install simulated. Returns A's commit."""
        ir.finish(self.rec, self.repo, self.a, ["core"], self.T0)
        self.write_settings({"permissions": {"allow": ["Bash(mine:*)"]}})
        self.push("claude/profiles.json", registry_json("probe"))
        self.push("claude/settings.host.json", json.dumps({"statusLine": self.SL}))
        if layout:
            self.push_layout(core_depends_on)
        code, _, err = self.run_fn(tu.update)
        self.assertEqual(code, 0, err)
        # What B's install did.
        ir.finish(self.rec, self.repo, self.head(), list(profiles), self.T0)
        ir.add_permissions(self.rec, {"allow": [self.RULE]})
        self.write_settings({"permissions": {"allow": ["Bash(mine:*)", self.RULE]},
                             "statusLine": self.SL})
        return self.a

    def both_plugins(self):
        return [{"id": PROBE, "scope": "user"}, {"id": CORE, "scope": "user"}]

    def test_restores_the_configuration_and_uninstalls_the_b_only_plugin(self):
        a = self.scenario()
        cli = FakeCli(self.both_plugins())
        code, out, err = self.run_fn(tu.rollback, run_cli=cli)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.head(), a)
        self.assertEqual(self.live_settings(), {"permissions": {"allow": ["Bash(mine:*)"]}})
        self.assertIn(["plugin", "uninstall", PROBE, "--scope", "user"], cli.calls)
        self.assertNotIn(["plugin", "uninstall", CORE, "--scope", "user"], cli.calls)
        record = ir.load(self.rec)
        self.assertEqual(record["permissions_added"]["allow"], [])
        self.assertEqual(record["profiles"], ["core", "probe"])
        self.assertIsInstance(ir.rollback(record)["pinned_at"], str)
        self.assertIn("Rolling back to %s" % a[:12], out)

    def test_a_plugin_something_staying_needs_is_kept_and_reported(self):
        self.scenario(core_depends_on="probe")
        cli = FakeCli(self.both_plugins())
        code, out, err = self.run_fn(tu.rollback, run_cli=cli)
        self.assertEqual(code, 0, err)
        self.assertNotIn("uninstall", [call[1] for call in cli.calls if len(call) > 1])
        self.assertIn("kept %s: %s depends on it" % (PROBE, CORE), out)

    def test_a_dependency_read_error_keeps_the_plugin_and_says_why(self):
        self.scenario(layout=False)
        cli = FakeCli(self.both_plugins())
        code, out, err = self.run_fn(tu.rollback, run_cli=cli)
        self.assertEqual(code, 0, err)
        self.assertNotIn("uninstall", [call[1] for call in cli.calls if len(call) > 1])
        self.assertIn("kept %s: cannot read the toolkit's plugin manifests" % PROBE, out)

    def test_enable_workflows_the_install_added_is_removed_with_its_flag(self):
        self.scenario()
        ir.set_workflows_owned(self.rec, True)
        self.write_settings({"permissions": {"allow": ["Bash(mine:*)", self.RULE]},
                             "statusLine": self.SL, "enableWorkflows": True})
        code, _, err = self.run_fn(tu.rollback, run_cli=FakeCli(self.both_plugins()))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.live_settings(), {"permissions": {"allow": ["Bash(mine:*)"]}})
        self.assertNotIn("enable_workflows_added", ir.load(self.rec))

    def test_a_listing_failure_refuses_and_changes_nothing(self):
        self.scenario()
        before = (self.snapshot(), read_bytes(self.settings_path))
        code, _, err = self.run_fn(tu.rollback, run_cli=FakeCli(fail_list=True))
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("Not rolled back: "), err)
        self.assertEqual((self.snapshot(), read_bytes(self.settings_path)), before)

    def test_an_uninstall_failure_stops_before_anything_else(self):
        self.scenario()
        before = (self.snapshot(), read_bytes(self.settings_path))
        code, _, err = self.run_fn(tu.rollback, run_cli=FakeCli(self.both_plugins(), fail_uninstall=True))
        self.assertEqual(code, 1)
        self.assertIn("Not rolled back: claude plugin uninstall %s failed; "
                      "re-run ./install.sh --rollback" % PROBE, err)
        self.assertEqual((self.snapshot(), read_bytes(self.settings_path)), before)

    def test_no_b_only_profile_never_calls_the_cli(self):
        self.scenario(profiles=("core",))
        cli = FakeCli(self.both_plugins())
        code, _, err = self.run_fn(tu.rollback, run_cli=cli)
        self.assertEqual(code, 0, err)
        self.assertEqual(cli.calls, [])
        self.assertEqual(self.live_settings(), {"permissions": {"allow": ["Bash(mine:*)"]}})

    def test_a_legacy_entry_moves_the_checkout_only(self):
        old, _ = self.update_to_tip()
        ir.set_rollback(self.rec, old, NOW)
        self.write_settings({"statusLine": self.SL})
        cli = FakeCli()
        code, out, err = self.run_fn(tu.rollback, run_cli=cli)
        self.assertEqual(code, 0, err)
        self.assertIn("No configuration snapshot was recorded with this rollback point; "
                      "only the checkout moves.", out)
        self.assertEqual(self.head(), old)
        self.assertEqual(cli.calls, [])
        self.assertEqual(self.live_settings(), {"statusLine": self.SL})

    def test_a_malformed_snapshot_refuses_and_names_the_manual_command(self):
        old, _ = self.update_to_tip()
        with open(self.rec) as handle:
            record = json.load(handle)
        record["rollback"]["state"] = {"bogus": 1}
        with open(self.rec, "w") as handle:
            json.dump(record, handle)
        before = self.snapshot()
        code, _, err = self.run_fn(tu.rollback, run_cli=FakeCli())
        self.assertEqual(code, 1)
        self.assertIn("the configuration snapshot in the install record is unreadable", err)
        self.assertIn("git -C %s checkout --detach %s && %s/install.sh" % (self.repo, old, self.repo), err)
        self.assertEqual(self.snapshot(), before)

    def test_unreadable_settings_refuse(self):
        self.scenario()
        with open(self.settings_path, "w") as handle:
            handle.write("{broken")
        before = self.snapshot()
        code, _, err = self.run_fn(tu.rollback, run_cli=FakeCli(self.both_plugins()))
        self.assertEqual((code, self.snapshot()), (1, before))
        self.assertIn(self.settings_path, err)

    def test_a_checkout_failure_reports_restored_but_not_moved(self):
        a = self.scenario()
        b = self.head()
        real = tu._git

        def failing(repo, *args, **kwargs):
            if args and args[0] == "checkout":
                return subprocess.CompletedProcess(args, 1, "", "boom")
            return real(repo, *args, **kwargs)

        with mock.patch.object(tu, "_git", failing):
            code, _, err = self.run_fn(tu.rollback, run_cli=FakeCli(self.both_plugins()))
        self.assertEqual(code, 1)
        self.assertIn("The configuration was restored, but the checkout did not move.", err)
        self.assertIn("git -C %s checkout --detach %s && %s/install.sh" % (self.repo, a, self.repo), err)
        self.assertEqual(self.head(), b)
        self.assertEqual(self.live_settings(), {"permissions": {"allow": ["Bash(mine:*)"]}})

    def test_a_second_rollback_writes_no_settings(self):
        self.scenario()
        self.assertEqual(self.run_fn(tu.rollback, run_cli=FakeCli(self.both_plugins()))[0], 0)
        owned = {k: v for k, v in ir.load(self.rec).items() if k != "rollback"}
        settings = read_bytes(self.settings_path)
        cli = FakeCli(self.both_plugins())
        with mock.patch.object(settings_io, "write_settings") as write:
            code, _, err = self.run_fn(tu.rollback, run_cli=cli)
        self.assertEqual(code, 0, err)
        write.assert_not_called()
        self.assertEqual(cli.calls, [])
        self.assertEqual(read_bytes(self.settings_path), settings)
        self.assertEqual({k: v for k, v in ir.load(self.rec).items() if k != "rollback"}, owned)

    def test_the_restore_runs_uninstall_then_settings_then_record_then_checkout(self):
        self.scenario()
        trace = []
        cli = FakeCli(self.both_plugins())

        def traced_cli(argv):
            if argv[:2] == ["plugin", "uninstall"]:
                trace.append("uninstall")
            return cli(argv)

        real_settings, real_owned, real_git = settings_io.write_settings, ir.restore_owned, tu._git

        def traced_settings(*args):
            trace.append("settings")
            return real_settings(*args)

        def traced_owned(*args):
            trace.append("record")
            return real_owned(*args)

        def traced_git(repo, *args, **kwargs):
            if args and args[0] == "checkout":
                trace.append("checkout")
            return real_git(repo, *args, **kwargs)

        with mock.patch.object(settings_io, "write_settings", traced_settings), \
                mock.patch.object(ir, "restore_owned", traced_owned), \
                mock.patch.object(tu, "_git", traced_git):
            code, _, err = self.run_fn(tu.rollback, run_cli=traced_cli)
        self.assertEqual(code, 0, err)
        self.assertEqual(trace, ["uninstall", "settings", "record", "checkout"])

    def test_notes_print_before_the_move(self):
        self.scenario()
        self.write_settings({"permissions": {"allow": ["Bash(mine:*)"]}, "statusLine": {"command": "mine"}})
        code, out, err = self.run_fn(tu.rollback, run_cli=FakeCli(self.both_plugins()))
        self.assertEqual(code, 0, err)
        self.assertLess(out.index("statusLine changed since the update; left as you set it"),
                        out.index("Rolling back to"))
        self.assertEqual(self.live_settings()["statusLine"], {"command": "mine"})


class TestOriginMatches(unittest.TestCase):
    def test_github_spellings_are_one_repository(self):
        public = "https://github.com/mouramarket/claude-code-toolkit"
        for same in ("https://github.com/mouramarket/claude-code-toolkit.git",
                     "https://github.com/mouramarket/claude-code-toolkit/",
                     "https://user@github.com/mouramarket/claude-code-toolkit",
                     "git@github.com:mouramarket/claude-code-toolkit.git",
                     "ssh://git@github.com/mouramarket/claude-code-toolkit",
                     "ssh://git@ssh.github.com:443/mouramarket/claude-code-toolkit.git",
                     "https://GitHub.com/MouraMarket/Claude-Code-Toolkit"):
            self.assertTrue(tu.origin_matches(same, public), same)
        for other in ("https://github.com/someone/claude-code-toolkit",
                      "https://github.com/mouramarket/claude-code-toolkit-build",
                      "https://gitlab.com/mouramarket/claude-code-toolkit",
                      "https://github.com.evil.example/mouramarket/claude-code-toolkit"):
            self.assertFalse(tu.origin_matches(other, public), other)

    def test_other_hosts_keep_transport_port_and_case(self):
        base = "ssh://git@git.example.org/team/kit.git"
        self.assertTrue(tu.origin_matches("ssh://git.example.org:22/team/kit", base))
        self.assertTrue(tu.origin_matches("git@git.example.org:team/kit", base))
        self.assertFalse(tu.origin_matches("ssh://git@git.example.org:2222/team/kit", base))
        self.assertFalse(tu.origin_matches("https://git.example.org/team/kit", base))
        # Same host, port and path: the transport alone must still tell them apart.
        self.assertFalse(tu.origin_matches("ssh://git.example.org:443/team/kit",
                                           "https://git.example.org/team/kit"))
        self.assertFalse(tu.origin_matches("ssh://git@git.example.org/Team/Kit", base))
        self.assertTrue(tu.origin_matches("HTTPS://Git.Example.org:443/team/kit/",
                                          "https://git.example.org/team/kit"))

    def test_local_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.join(os.path.realpath(tmp), "origin.git")
            os.mkdir(real)
            link = os.path.join(os.path.realpath(tmp), "link.git")
            os.symlink(real, link)
            self.assertTrue(tu.origin_matches(link, real))
            self.assertTrue(tu.origin_matches("file://" + real, real))
            self.assertTrue(tu.origin_matches(real + "/", real))
            self.assertFalse(tu.origin_matches(real[:-len(".git")], real))
            self.assertFalse(tu.origin_matches("https://example.org" + real, real))
            self.assertFalse(tu.origin_matches(real, "https://example.org" + real))

    def test_nothing_matches_nothing(self):
        self.assertFalse(tu.origin_matches("", ""))
        self.assertFalse(tu.origin_matches("https://github.com/", "https://github.com/"))


class TestCli(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True,
                              encoding="utf-8", stdin=subprocess.DEVNULL, timeout=60)

    def test_no_args_and_unknown_subcommand_exit_2(self):
        for args in ([], ["bogus", "."], ["version"]):
            out = self.run_cli(*args)
            self.assertEqual(out.returncode, 2, args)
            self.assertIn("usage", out.stderr.lower())

    def test_version_runs(self):
        with tempfile.TemporaryDirectory() as home:
            repo = os.path.join(home, "r")
            subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True,
                           env=_clean_env(), stdin=subprocess.DEVNULL)
            git(repo, "commit", "-q", "--allow-empty", "-m", "first")
            env = dict(_clean_env(), HOME=home)
            out = subprocess.run([sys.executable, SCRIPT, "version", repo], capture_output=True,
                                 encoding="utf-8", stdin=subprocess.DEVNULL, env=env, timeout=60)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("Last install: none recorded", out.stdout)


if __name__ == "__main__":
    unittest.main()
