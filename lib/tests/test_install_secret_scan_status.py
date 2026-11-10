#!/usr/bin/env python3
"""install.sh says whether commits are actually scanned for credentials.

The `~/.local/bin/claude-secret-scan` link is for running the scanner by hand.
Commits are scanned only when global `core.hooksPath` holds a pre-commit that
resolves to this checkout's `tools/secret-scan/git-pre-commit`, which
`scripts/setup-git.sh` sets up and install.sh never does. An installer that
links the scanner and says nothing else reads as "protected" on a machine where
no commit is scanned, so both a real run and `--check` print the real state.

The state is a warning, never a required failure: setup-git.sh is a separate,
machine-wide choice. And a `core.hooksPath` that is not ours is never reported
as active, because install.sh cannot see whether that hook calls the scanner.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_install_check_fails import CheckVerdictTestCase  # noqa: E402

SECTION = "Secret scanning (pre-commit)"
ACTIVE = "active: commits on this machine are scanned"
NOT_ACTIVE = "NOT active"


class SecretScanStatusTestCase(CheckVerdictTestCase):

    def setUp(self):
        super().setUp()
        self.hooks_dir = os.path.join(self.home, ".config", "claude-code-toolkit", "git-hooks")
        self.precommit = os.path.join(self.repo, "tools", "secret-scan", "git-pre-commit")
        if not os.path.exists(self.precommit):
            os.makedirs(os.path.dirname(self.precommit), exist_ok=True)
            with open(self.precommit, "w") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(self.precommit, 0o755)

    def set_hooks_path(self, value):
        subprocess.run(("git", "config", "--file", os.path.join(self.home, ".gitconfig"),
                        "core.hooksPath", value), check=True)

    def link_our_hook(self, target=None):
        os.makedirs(self.hooks_dir, exist_ok=True)
        os.symlink(target or self.precommit, os.path.join(self.hooks_dir, "pre-commit"))
        self.set_hooks_path(self.hooks_dir)

    def section(self, proc):
        """The lines of this section only, so another section's words can't pass."""
        out = proc.stdout
        self.assertIn(SECTION, out, proc.stdout + proc.stderr)
        body = out.split(SECTION, 1)[1]
        lines = []
        for line in body.splitlines()[1:]:
            if line and not line.startswith(" "):
                break
            lines.append(line)
        return "\n".join(lines)

    def both_modes(self):
        return (("apply", self.install()), ("check", self.check()))


class TestStatus(SecretScanStatusTestCase):

    def test_no_hooks_path_says_not_active_and_names_setup_git(self):
        for mode, proc in self.both_modes():
            with self.subTest(mode=mode):
                text = self.section(proc)
                self.assertIn(NOT_ACTIVE, text)
                self.assertNotIn(ACTIVE, text)
                self.assertIn("scripts/setup-git.sh", text)
                self.assertIn("by hand", text)

    def test_our_hook_resolving_to_this_checkout_is_active(self):
        self.link_our_hook()
        for mode, proc in self.both_modes():
            with self.subTest(mode=mode):
                text = self.section(proc)
                self.assertIn(ACTIVE, text)
                self.assertNotIn(NOT_ACTIVE, text)

    def test_a_tilde_hooks_path_is_expanded(self):
        self.link_our_hook()
        self.set_hooks_path("~/.config/claude-code-toolkit/git-hooks")
        self.assertIn(ACTIVE, self.section(self.check()))

    def test_a_foreign_hooks_path_is_never_active(self):
        # Even one whose pre-commit IS ours: we did not set that directory up,
        # and cannot see what else it does or whether it stays that way.
        foreign = os.path.join(self.home, "my-hooks")
        os.makedirs(foreign)
        os.symlink(self.precommit, os.path.join(foreign, "pre-commit"))
        self.set_hooks_path(foreign)
        text = self.section(self.check())
        self.assertIn(NOT_ACTIVE, text)
        self.assertNotIn(ACTIVE, text)
        self.assertIn("yours", text)
        self.assertIn("scripts/setup-git.sh", text)

    def test_a_broken_hook_link_is_not_active(self):
        self.link_our_hook(target=os.path.join(self.root, "gone", "git-pre-commit"))
        text = self.section(self.check())
        self.assertIn(NOT_ACTIVE, text)
        self.assertNotIn(ACTIVE, text)

    def test_a_hook_from_another_checkout_is_not_active(self):
        other = os.path.join(self.root, "other-checkout", "git-pre-commit")
        os.makedirs(os.path.dirname(other))
        with open(other, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(other, 0o755)
        self.link_our_hook(target=other)
        text = self.section(self.check())
        self.assertIn(NOT_ACTIVE, text)
        self.assertNotIn(ACTIVE, text)

    def test_a_non_executable_hook_is_not_active(self):
        self.link_our_hook()
        os.chmod(self.precommit, 0o644)
        self.assertIn(NOT_ACTIVE, self.section(self.check()))

    def test_not_active_is_a_warning_not_a_required_failure(self):
        self.assertEqual(0, self.check().returncode)
        self.assertEqual(0, self.install().returncode)


if __name__ == "__main__":
    unittest.main()
