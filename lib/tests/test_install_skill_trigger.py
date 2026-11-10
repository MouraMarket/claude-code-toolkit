#!/usr/bin/env python3
"""Registration test for `claude-skill-trigger` in the installer.

**Why this test is required rather than nice-to-have.**
`TestExecutableNamesCoversEveryLink` (test_install_links_inventory.py) cannot
cover an omission of THIS shape: it asserts set equality between
`EXECUTABLE_NAMES` and the `link "$REPO/tools/..."` calls, so a tool missing
from BOTH lists passes green. `./install.sh --check` does not close the gap
either: it discovers test suites from the filesystem, which says nothing about
whether the executable was ever linked. Without a test that runs the installer
and checks the resulting symlink, `claude-skill-trigger` could ship unlinked
with the inventory check, `--check` and the whole suite green.

This drives the REAL `install.sh` against a full copy of the repo with
`tests/` stripped, so the installer's own Tests section finds nothing to run
and a fixture install stays fast. `HOME` is redirected to a sandbox, so
nothing here touches the real `~/.claude` or `~/.local/bin`. It needs the
genuine tree, not a stub repo with an empty `tools/`, to prove a real symlink
resolves to a real file under `tools/skill-trigger/`.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import install_fixture as fx  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


def _bash_available():
    return shutil.which("bash") is not None


class InstallSandbox(unittest.TestCase):
    """A throwaway HOME plus a throwaway copy of the repo."""

    def setUp(self):
        if not _bash_available():
            self.skipTest("bash not available")
        if not os.path.isfile(os.path.join(REPO, "install.sh")):
            self.skipTest("install.sh not found next to lib/")

        self.tmp = tempfile.mkdtemp(prefix="install-skill-trigger-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "repo")

        # Strip `tests` so the installer's suite runner finds nothing: without
        # this a real apply inside this test runs the whole toolkit suite.
        shutil.copytree(
            REPO, self.repo,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", ".claude", "tests",
                                          "__pycache__"))
        fx.stub_pending_components(self.repo)
        os.makedirs(self.home + "/.local/bin", exist_ok=True)

    def run_install(self, *args):
        env = dict(os.environ)
        # The redirected HOME is what makes this hermetic: install.sh reads
        # $HOME/.config/claude-code-toolkit/config.json, so HOME is the lever.
        env["HOME"] = self.home
        env.pop("CLAUDE_CONFIG_DIR", None)
        env = fx.shim_env_for_home(env, self.home)
        if "--check" in args:
            fx.seed_home_installed(self.home, self.repo)
        return subprocess.run(
            ["bash", os.path.join(self.repo, "install.sh")] + list(args),
            capture_output=True, text=True, env=env, cwd=self.repo,
            # REQUIRED, not tidiness: capture_output leaves stdin INHERITED, and
            # install.sh prompts when it sees a TTY and no config. Run from a
            # real terminal without this, the test blocks forever and silently
            # eats the operator's keystrokes.
            stdin=subprocess.DEVNULL,
            timeout=600)


class TestClaudeSkillTriggerIsLinked(InstallSandbox):

    def test_a_real_apply_links_claude_skill_trigger_to_its_executable(self):
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        link_path = os.path.join(self.home, ".local", "bin",
                                 "claude-skill-trigger")
        self.assertTrue(os.path.lexists(link_path),
                        "claude-skill-trigger was never linked into "
                        "~/.local/bin")
        expected = os.path.realpath(os.path.join(
            self.repo, "tools", "skill-trigger", "claude-skill-trigger"))
        self.assertEqual(os.path.realpath(link_path), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
