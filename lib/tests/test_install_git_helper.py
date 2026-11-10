#!/usr/bin/env python3
"""The fixture `git` helper shows git's own stderr when a call fails.

A bare CalledProcessError names the command and exit code but not why git
failed, which made an intermittent exit 128 in the install tests undiagnosable.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from test_install_plugin import GitFixtureError, git


class GitHelperTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def failure(self):
        # A fresh repo, so the failure never depends on what encloses TMPDIR.
        git("init", "-q", self.dir, cwd=self.dir)
        with self.assertRaises(GitFixtureError) as caught:
            git("rev-parse", "--verify", "no-such-ref", cwd=self.dir)
        return caught.exception

    def test_failure_shows_git_stderr(self):
        err = self.failure()
        self.assertIsInstance(err, subprocess.CalledProcessError)
        self.assertIn("128", str(err))
        self.assertIn("Needed a single revision", str(err))

    def test_success_returns_completed_process(self):
        done = git("init", "-q", self.dir, cwd=self.dir)
        self.assertIsInstance(done, subprocess.CompletedProcess)
        self.assertEqual(0, done.returncode)

    def test_failure_names_cwd(self):
        self.assertIn(self.dir, str(self.failure()))


if __name__ == "__main__":
    unittest.main()
