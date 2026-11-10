#!/usr/bin/env python3
"""Behavioural tests for install.sh's per-run settings snapshot.

Every `install.sh` run that can change `~/.claude/settings.json` first copies
it to `settings.json.claude-code-toolkit-<UTC stamp>.bak` (never overwriting one, never
duplicating the newest), prunes to the 10 newest, and no later step writes a
`settings.json.bak`. `settings.json.pre-claude-code-toolkit.bak` is a separate, older
one-shot snapshot that this feature never counts, touches or deletes.

Drives the REAL install.sh through `subprocess` against the fixture repo of
test_install_auto_memory.py with a sandboxed HOME and the shared `claude` shim.
"""

import glob
import json
import os
import shutil
import subprocess
import tempfile
import unittest

import install_fixture as ifx
import test_install_auto_memory as fx

STAMP = "20261010T000000Z"
PRE_RUN = {"theme": "dark"}


class SnapshotTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.repo = fx.make_fixture_repo(self.root)
        self.config_path = os.path.join(self.root, "toolkit-config.json")
        fx.write_config(self.config_path, {})
        fx.seed_home(self.home)
        self.settings = fx.settings_path_for(self.home)

    def run_install(self, args=(), stamp=None):
        env = dict(os.environ)
        env["HOME"] = self.home
        env["CLAUDE_TOOLKIT_CONFIG"] = self.config_path
        env = ifx.shim_env_for_home(env, self.home)
        env.pop("ALLOW_WORKTREE_INSTALL", None)
        env.pop("INSTALL_SNAPSHOT_STAMP", None)
        if stamp:
            env["INSTALL_SNAPSHOT_STAMP"] = stamp
        result = subprocess.run([os.path.join(self.repo, "install.sh")] + list(args),
                                cwd=self.repo, env=env, capture_output=True,
                                stdin=subprocess.DEVNULL, timeout=120,
                                universal_newlines=True)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    def seed_protected(self):
        """The state `--check` / `--links-only` exit 0 from."""
        ifx.seed_home_installed(self.home, self.repo)
        ifx.install_links(self.repo, self.home)

    def hand_write(self, data):
        fx.write_settings(self.home, data)
        return self.raw()

    def raw(self):
        with open(self.settings, "rb") as handle:
            return handle.read()

    def timed(self):
        """Timed snapshot paths, by name."""
        return sorted(glob.glob(self.settings + ".claude-code-toolkit-*.bak"))

    def read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def seed_snapshot(self, name, content):
        path = "%s.claude-code-toolkit-%s.bak" % (self.settings, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        return path


class TestSnapshotMatchesPreRun(SnapshotTestCase):

    def test_ac1_1_one_new_snapshot_holds_the_pre_run_bytes(self):
        pre = self.hand_write(PRE_RUN)
        self.seed_snapshot("20200101T000000Z", b'{"older": true}\n')
        before = self.timed()
        result = self.run_install()
        added = [p for p in self.timed() if p not in before]
        self.assertEqual(1, len(added), self.timed())
        self.assertEqual(pre, self.read(added[0]))
        # The shim wrote to settings.json during the run, after the snapshot.
        self.assertNotEqual(pre, self.raw())
        self.assertIn("snapshot of settings.json saved to " + os.path.basename(added[0]),
                      result.stdout)


class TestRunTwice(SnapshotTestCase):

    def test_ac1_2_first_snapshot_is_never_touched(self):
        first_pre = self.hand_write(PRE_RUN)
        self.run_install()
        (first,) = self.timed()
        self.assertEqual(first_pre, self.read(first))
        first_bytes = self.read(first)
        edited = self.hand_write({"theme": "light", "mine": 1})
        self.run_install()
        self.assertEqual(first_bytes, self.read(first))
        others = [p for p in self.timed() if p != first]
        self.assertEqual(1, len(others), self.timed())
        self.assertEqual(edited, self.read(others[0]))

    def test_ac1_3_converged_third_run_adds_no_snapshot(self):
        self.hand_write(PRE_RUN)
        self.run_install()
        self.assertEqual(1, len(self.timed()))
        # P7: the first install changed settings.json, so run 2 snapshots too.
        self.run_install()
        self.assertEqual(2, len(self.timed()))
        self.run_install()
        self.assertEqual(2, len(self.timed()))

    def test_ac1_7_same_second_collision_suffixes_instead_of_overwriting(self):
        first_pre = self.hand_write(PRE_RUN)
        self.run_install(stamp=STAMP)
        edited = self.hand_write({"theme": "light", "mine": 1})
        self.run_install(stamp=STAMP)
        base = "%s.claude-code-toolkit-%s" % (self.settings, STAMP)
        self.assertEqual([base + "-2.bak", base + ".bak"], self.timed())
        self.assertEqual(first_pre, self.read(base + ".bak"))
        self.assertEqual(edited, self.read(base + "-2.bak"))


class TestRetention(SnapshotTestCase):

    def test_ac1_4_keeps_the_ten_newest_and_never_touches_pre_toolkit_toolkit(self):
        self.hand_write(PRE_RUN)
        for number in range(1, 13):
            self.seed_snapshot("202001%02dT000000Z" % number,
                               ('{"seed": %d}\n' % number).encode())
        pre_toolkit = self.settings + ".pre-claude-code-toolkit.bak"
        with open(pre_toolkit, "wb") as handle:
            handle.write(b'{"pre": "toolkit"}\n')
        self.run_install(stamp=STAMP)
        names = [os.path.basename(p) for p in self.timed()]
        expected = ["settings.json.claude-code-toolkit-202001%02dT000000Z.bak" % n
                    for n in range(4, 13)] + ["settings.json.claude-code-toolkit-%s.bak" % STAMP]
        self.assertEqual(sorted(expected), names)
        self.assertEqual(b'{"pre": "toolkit"}\n', self.read(pre_toolkit))

    def test_ac1_8_prune_orders_by_counter_not_by_name(self):
        pair_stamp = "20200101T000001Z"
        self.seed_snapshot(pair_stamp, b'{"pair": 1}\n')
        self.seed_snapshot(pair_stamp + "-2", b'{"pair": 2}\n')
        newest = None
        for number in range(2, 11):
            newest = b'{"seed": %d}\n' % number
            self.seed_snapshot("202001%02dT000000Z" % number, newest)
        # Settings equal the newest snapshot, so the run adds none and exactly
        # one prune is due (11 held, 10 kept). Plain name order would delete
        # `...Z-2.bak` ('-' sorts before '.'); the right victim is `...Z.bak`.
        os.makedirs(os.path.dirname(self.settings), exist_ok=True)
        with open(self.settings, "wb") as handle:
            handle.write(newest)
        self.run_install()
        names = [os.path.basename(p) for p in self.timed()]
        self.assertEqual(10, len(names), names)
        self.assertIn("settings.json.claude-code-toolkit-%s-2.bak" % pair_stamp, names)
        self.assertNotIn("settings.json.claude-code-toolkit-%s.bak" % pair_stamp, names)


class TestNoSnapshotWithoutWrites(SnapshotTestCase):

    def test_ac1_5_check_and_links_only_create_none(self):
        self.hand_write(PRE_RUN)
        self.seed_protected()
        self.run_install(args=["--check"])
        self.assertEqual([], self.timed())
        self.run_install(args=["--links-only"])
        self.assertEqual([], self.timed())


class TestNoPerStepBackup(SnapshotTestCase):

    def test_ac1_6_no_settings_json_bak_after_all_three_steps_write(self):
        workspace = os.path.join(self.root, "work space.root")
        os.makedirs(workspace)
        os.makedirs(fx.projects_dir_for(self.home, workspace))
        fx.write_config(self.config_path, {"workspace_root": workspace})
        # A baseline rule the settings lack, so the permissions step writes too.
        with open(os.path.join(self.repo, "claude", "permissions.baseline.json"), "w") as handle:
            json.dump({"permissions": {"ask": ["Bash(git push *)"]}}, handle)
        self.hand_write(PRE_RUN)
        result = self.run_install()
        for step in ("permissions", "autoMemoryDirectory", "claudeMdExcludes"):
            self.assertIn("wrote %s (snapshot taken at the start of this run)" % step,
                          result.stdout)
        self.assertFalse(os.path.exists(self.settings + ".bak"))


if __name__ == "__main__":
    unittest.main()
