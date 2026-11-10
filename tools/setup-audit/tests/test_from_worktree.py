#!/usr/bin/env python3
"""Tests for the report's `from_worktree` field.

`build_report`/`main --json` report whether the repo being audited is a linked
git worktree (`audit_lib.is_linked_worktree`) --
from one, every `link` row compares `dest` against a target that vanishes the
moment the branch merges, so `linked_elsewhere`/`will_repoint` counts are not
real drift. The human summary says so, as its first line, only when true.

Run from a linked worktree of this repo, the `REPO` half is exercised against
the genuine article rather than a synthetic one (it skips otherwise); `fixtures.make_fixture_repo` (a fresh `git init`) is the
primary-checkout control.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

sys.path.insert(0, HERE)
sys.path.insert(0, TOOL_DIR)

os.environ["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(
    tempfile.gettempdir(), "setup-audit-tests-hermetic-nonexistent.json")

import fixtures  # noqa: E402  (first: it sets up the import path)
import audit_lib  # noqa: E402


class TestFromWorktreeField(unittest.TestCase):

    def _sandbox_home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def test_real_worktree_reports_true(self):
        if not audit_lib.is_linked_worktree(REPO):
            self.skipTest("not running from a linked worktree")
        home = self._sandbox_home()
        report = audit_lib.build_report(home=home, repo=REPO, environ={})
        self.assertTrue(report["from_worktree"], report)

    def test_primary_checkout_reports_false(self):
        root = tempfile.mkdtemp(prefix="setup-audit-fixture-repo-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = fixtures.make_fixture_repo(root)
        self.assertFalse(audit_lib.is_linked_worktree(fixture))
        home = self._sandbox_home()
        report = audit_lib.build_report(home=home, repo=fixture, environ={})
        self.assertFalse(report["from_worktree"], report)


class TestFromWorktreeCLI(unittest.TestCase):

    def _sandbox_home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def test_json_reports_true_from_the_real_worktree(self):
        if not audit_lib.is_linked_worktree(REPO):
            self.skipTest("not running from a linked worktree")
        home = self._sandbox_home()
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": home}):
            with contextlib.redirect_stdout(buf):
                rc = audit_lib.main(["--json"], repo=REPO)
        self.assertEqual(rc, 0, buf.getvalue())
        report = json.loads(buf.getvalue())
        self.assertTrue(report["from_worktree"], report)

    def test_json_reports_false_from_a_primary_checkout(self):
        root = tempfile.mkdtemp(prefix="setup-audit-fixture-repo-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = fixtures.make_fixture_repo(root)
        home = self._sandbox_home()
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": home}):
            with contextlib.redirect_stdout(buf):
                rc = audit_lib.main(["--json"], repo=fixture)
        self.assertEqual(rc, 0, buf.getvalue())
        report = json.loads(buf.getvalue())
        self.assertFalse(report["from_worktree"], report)


class TestFixtureWorktree(unittest.TestCase):
    """The true branch against a linked worktree this suite builds itself,
    so it runs wherever the suite does (the REPO tests above skip outside
    one)."""

    def test_linked_worktree_of_a_fixture_reports_and_warns(self):
        root = tempfile.mkdtemp(prefix="setup-audit-fixture-repo-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = fixtures.make_fixture_repo(root)
        worktree = os.path.join(root, "wt")
        fixtures.git_worktree_add(fixture, worktree)
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)

        report = audit_lib.build_report(home=home, repo=worktree, environ={})
        self.assertIs(report["from_worktree"], True, report)
        summary = audit_lib._human_summary(report)
        self.assertEqual(summary.splitlines()[0],
                         TestHumanSummaryWorktreeWarning._WARNING, summary)

        control = audit_lib.build_report(home=home, repo=fixture, environ={})
        self.assertIs(control["from_worktree"], False, control)


class TestFixtureGuards(unittest.TestCase):
    """A fixture that reaches the real repository's git dir runs its
    `init`/`config`/`commit` there: the shared config and the real branch."""

    def test_mirror_refuses_git_entries(self):
        root = tempfile.mkdtemp(prefix="setup-audit-mirror-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for name in (".git", ".gitignore"):
            with self.assertRaises(AssertionError):
                fixtures._mirror(root, os.path.join(fixtures.REPO, name))
        self.assertEqual(os.listdir(root), [])
        # Positive control: an ordinary file mirrors.
        fixtures._mirror(root, os.path.join(fixtures.REPO, "install.sh"))
        self.assertTrue(os.path.islink(os.path.join(root, "install.sh")))

    def test_mirrored_files_carry_no_git_entry_and_no_test(self):
        paths = fixtures._mirrored_files()
        self.assertIn(os.path.join(fixtures.REPO, "install.sh"), paths)
        for path in paths:
            rel = os.path.relpath(path, fixtures.REPO)
            self.assertFalse(rel.split(os.sep)[0].startswith(".git"), rel)
            self.assertNotIn("tests", rel.split(os.sep), rel)

    def test_own_git_dir_refuses_a_borrowed_one(self):
        root = tempfile.mkdtemp(prefix="setup-audit-gitdir-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        real = os.path.join(root, "real")
        fixtures._git("init", "-q", real, cwd=root)
        borrowed = os.path.join(root, "borrowed")
        os.makedirs(borrowed)
        fixtures.write(os.path.join(borrowed, ".git"),
                       "gitdir: %s\n" % os.path.join(real, ".git"))
        with self.assertRaises(AssertionError):
            fixtures.require_own_git_dir(borrowed)
        # Positive control: a repository's own .git passes.
        fixtures.require_own_git_dir(real)


class TestHumanSummaryWorktreeWarning(unittest.TestCase):
    """The plan's exact warning is the FIRST line when `from_worktree` is
    true, and absent when it is false (positive control)."""

    _WARNING = (
        "run from a linked worktree: every link compares against it, so "
        "repoint and linked_elsewhere counts are not real drift. Re-run "
        "from the primary checkout.")

    def _sandbox_home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def test_warning_is_first_line_when_true(self):
        if not audit_lib.is_linked_worktree(REPO):
            self.skipTest("not running from a linked worktree")
        home = self._sandbox_home()
        report = audit_lib.build_report(home=home, repo=REPO, environ={})
        summary = audit_lib._human_summary(report)
        self.assertEqual(summary.splitlines()[0], self._WARNING, summary)

    def test_warning_absent_when_false(self):
        root = tempfile.mkdtemp(prefix="setup-audit-fixture-repo-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixture = fixtures.make_fixture_repo(root)
        home = self._sandbox_home()
        report = audit_lib.build_report(home=home, repo=fixture, environ={})
        summary = audit_lib._human_summary(report)
        self.assertNotIn(self._WARNING, summary)
        self.assertNotEqual(summary.splitlines()[0], self._WARNING, summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
