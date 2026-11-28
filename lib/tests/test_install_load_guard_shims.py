#!/usr/bin/env python3
"""install.sh's load-guard shims: ~/.local/bin/<tool> -> claude-heavy-run.

A shim is created only while the REAL tool exists on PATH outside ~/.local/bin
(a shim for an absent tool would answer "not installed" where the shell's own
"command not found" belongs), only unless `load_guard.enabled` is false, and
never over something that is not already our shim. The set is dynamic (built-ins
plus `load_guard.commands`), so these are not `link` calls and
`TestExecutableNamesCoversEveryLink` does not see them: this file is what keeps
them honest.

Drives the real install.sh through the worktree suite's fixture repo, with a
sandbox HOME and a PATH that holds a fake real tool and none of the real ones.
"""

import json
import os
import shutil
import stat
import tempfile
import unittest

import install_fixture as fx
import test_install_worktree as wt

REPO_ROOT = wt.REPO_ROOT
HEAVY_RUN = os.path.join("tools", "load-guard", "claude-heavy-run")


def path_without(scratch_dir, names):
    """The machine's PATH with every entry in `names` hidden: a directory that
    holds one is replaced by a shadow directory of symlinks to the rest."""
    out = []
    for index, directory in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not directory or not os.path.isdir(directory):
            continue
        if not any(os.path.lexists(os.path.join(directory, n)) for n in names):
            out.append(directory)
            continue
        shadow = os.path.join(scratch_dir, "shadow-%d" % index)
        os.makedirs(shadow, exist_ok=True)
        for entry in os.listdir(directory):
            if entry not in names:
                try:
                    os.symlink(os.path.join(directory, entry), os.path.join(shadow, entry))
                except OSError:
                    pass
        out.append(shadow)
    return os.pathsep.join(out)


class ShimInstallCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.repo = wt.make_fixture_repo(self.root)
        # The shim step imports these; the stub fixture ships them empty or not at all.
        os.makedirs(os.path.join(self.repo, "tools", "load-guard"), exist_ok=True)
        shutil.copy(os.path.join(REPO_ROOT, "tools", "load-guard", "load_guard_lib.py"),
                    os.path.join(self.repo, "tools", "load-guard", "load_guard_lib.py"))
        shutil.copy(os.path.join(REPO_ROOT, "lib", "toolkit_config.py"),
                    os.path.join(self.repo, "lib", "toolkit_config.py"))
        self.tool_dir = os.path.join(self.root, "realbin")
        os.makedirs(self.tool_dir)
        self.local_bin = os.path.join(self.home, ".local", "bin")
        os.makedirs(self.local_bin)
        # Without a config file install.sh stops at first-time setup, before any link.
        self.write_config({})

    def add_real_tool(self, name):
        path = os.path.join(self.tool_dir, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)

    def write_config(self, data):
        directory = os.path.join(self.home, ".config", "claude-code-toolkit")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "config.json"), "w") as handle:
            json.dump(data, handle)

    def install(self, *args):
        hidden = {"ffmpeg", "whisper-cli", "llama-server", "llama-cli", "llama-bench",
                  "mytool"}
        path = self.tool_dir + os.pathsep + path_without(
            os.path.join(self.root, "shadows"), hidden)
        return wt.run_install(self.repo, self.home, args, {"PATH": path})

    def shim(self, name):
        return os.path.join(self.local_bin, name)

    def is_our_shim(self, name):
        path = self.shim(name)
        return os.path.islink(path) and os.readlink(path) == os.path.join(self.repo, HEAVY_RUN)


class TestShimInstall(ShimInstallCase):
    def test_shim_created_when_the_real_tool_exists(self):
        self.add_real_tool("ffmpeg")
        self.install()
        self.assertTrue(self.is_our_shim("ffmpeg"))

    def test_no_shim_for_a_tool_that_is_not_installed(self):
        self.add_real_tool("ffmpeg")
        self.install()
        self.assertFalse(os.path.lexists(self.shim("whisper-cli")))
        self.assertFalse(os.path.lexists(self.shim("llama-server")))

    def test_no_shim_when_disabled(self):
        self.add_real_tool("ffmpeg")
        self.write_config({"load_guard": {"enabled": False}})
        self.install()
        self.assertFalse(os.path.lexists(self.shim("ffmpeg")))

    def test_disabling_prunes_an_existing_shim(self):
        self.add_real_tool("ffmpeg")
        self.install()
        self.assertTrue(self.is_our_shim("ffmpeg"))
        self.write_config({"load_guard": {"enabled": False}})
        self.install()
        self.assertFalse(os.path.lexists(self.shim("ffmpeg")))

    def test_a_foreign_file_is_left_untouched(self):
        self.add_real_tool("ffmpeg")
        with open(self.shim("ffmpeg"), "w") as handle:
            handle.write("mine\n")
        result = self.install()
        self.assertFalse(os.path.islink(self.shim("ffmpeg")))
        with open(self.shim("ffmpeg")) as handle:
            self.assertEqual("mine\n", handle.read())
        self.assertIn("not touching", result.stdout)

    def test_a_foreign_symlink_is_left_untouched_and_not_pruned(self):
        self.add_real_tool("ffmpeg")
        os.symlink("/usr/bin/true", self.shim("ffmpeg"))
        self.install()
        self.assertEqual("/usr/bin/true", os.readlink(self.shim("ffmpeg")))

    def test_a_config_extra_name_gets_a_shim(self):
        self.add_real_tool("mytool")
        self.write_config({"load_guard": {"commands": ["/some/dir/mytool"]}})
        self.install()
        self.assertTrue(self.is_our_shim("mytool"))

    def test_a_shim_for_a_tool_no_longer_installed_is_pruned(self):
        self.add_real_tool("ffmpeg")
        self.install()
        os.remove(os.path.join(self.tool_dir, "ffmpeg"))
        self.install()
        self.assertFalse(os.path.lexists(self.shim("ffmpeg")))

    def test_the_wrapper_itself_is_not_taken_for_the_real_tool(self):
        # ~/.local/bin on PATH holding only the shim is not "the tool exists".
        os.symlink(os.path.join(self.repo, HEAVY_RUN), self.shim("ffmpeg"))
        path = path_without(os.path.join(self.root, "shadows"), {"ffmpeg"})
        wt.run_install(self.repo, self.home, (),
                       {"PATH": self.local_bin + os.pathsep + path})
        self.assertFalse(os.path.lexists(self.shim("ffmpeg")))


class TestShimCheck(ShimInstallCase):
    def test_second_run_proposes_no_change(self):
        self.add_real_tool("ffmpeg")
        self.write_config({"load_guard": {"commands": ["mytool"]}})
        self.add_real_tool("mytool")
        self.install()
        again = self.install("--check")
        out = again.stdout
        self.assertIn("OK\x1b[0m ~/.local/bin/ffmpeg", out)   # seen and in place
        self.assertIn("OK\x1b[0m ~/.local/bin/mytool", out)
        self.assertNotIn("~/.local/bin/ffmpeg -> ", out)
        self.assertNotIn("~/.local/bin/mytool -> ", out)
        self.assertNotIn("shim no longer wanted", out)

    def test_check_proposes_the_missing_shim_and_creates_nothing(self):
        self.add_real_tool("ffmpeg")
        result = self.install("--check")
        self.assertIn("~/.local/bin/ffmpeg -> tools/load-guard/claude-heavy-run", result.stdout)
        self.assertFalse(os.path.lexists(self.shim("ffmpeg")))


if __name__ == "__main__":
    unittest.main()
