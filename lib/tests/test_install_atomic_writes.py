#!/usr/bin/env python3
"""install.sh writes settings.json atomically (temp file + rename) and takes its
backups with a no-clobber atomic create.

The static tests pin each write site's exact call (a final-file byte check
cannot see one site's wrong `ensure_ascii`, because a later step re-serializes
the whole object). The behavioural tests drive the REAL install.sh through the
fixture of `test_install_plugin`.
"""

import glob
import json
import os
import re
import stat
import unittest

import test_install_plugin as base
from test_install_plugin import INSTALL_SH, REPO_ROOT, read

WRITE_CALL = "settings_io.write_json_atomic(settings_path, settings, ensure_ascii=True)"
SNAPSHOT_LIB = os.path.join(REPO_ROOT, "tools", "setup-audit", "snapshot_lib.py")


class TestStatic(unittest.TestCase):

    def setUp(self):
        with open(INSTALL_SH) as handle:
            self.text = handle.read()

    def test_no_in_place_write_or_cp_backup_remains(self):
        self.assertNotIn('open(settings_path, "w")', self.text)
        self.assertNotIn('"xb"', self.text)
        self.assertNotIn('cp -p "$SETTINGS"', self.text)

    def test_every_site_uses_the_exact_call_and_nothing_else_does(self):
        # Six sites: hooks/statusLine, permissions, auto memory, enableWorkflows
        # set and remove, CLAUDE.md excludes. A new write site
        # changes both counts, so its call gets checked here, not assumed.
        self.assertEqual(self.text.count(WRITE_CALL), 6)
        self.assertEqual(self.text.count("write_json_atomic("), 6)

    def test_snapshot_lib_describes_the_atomic_write(self):
        with open(SNAPSHOT_LIB) as handle:
            text = handle.read()
        self.assertNotIn("edits in place", text)
        self.assertNotIn("opens for\n  read-and-write", text)
        self.assertNotIn("opens for read-and-write", re.sub(r"\s+", " ", text))


class AtomicInstallTestCase(base.PluginInstallTestCase):

    def setUp(self):
        super().setUp()
        # A baseline rule makes the install write the permissions step.
        with open(os.path.join(self.repo, "claude", "permissions.baseline.json"), "w") as handle:
            json.dump({"permissions": {"allow": ["Bash(ls:*)"]}}, handle)

    def install_ok(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def immutable(self):
        return self.settings_path + ".pre-claude-code-toolkit.bak"


class TestBytes(AtomicInstallTestCase):

    def test_final_file_is_ascii_escaped_json(self):
        self.write_settings({"note": "café"})
        self.install_ok()
        raw = read(self.settings_path)
        self.assertIn(b"caf\\u00e9", raw)
        parsed = json.loads(raw.decode("utf-8"))
        self.assertEqual(parsed["note"], "café")
        self.assertEqual(raw, (json.dumps(parsed, indent=2) + "\n").encode("utf-8"))
        self.assertIn("Bash(ls:*)", parsed["permissions"]["allow"])


class TestModeAndLink(AtomicInstallTestCase):

    def test_mode_0640_is_kept(self):
        self.write_settings({"note": "x"})
        os.chmod(self.settings_path, 0o640)
        self.install_ok()
        self.assertEqual(stat.S_IMODE(os.stat(self.settings_path).st_mode), 0o640)
        self.assertIn("Bash(ls:*)", self.settings()["permissions"]["allow"])

    def test_symlinked_settings_stay_a_link_and_the_target_changes(self):
        dotfiles = os.path.join(self.root, "dotfiles")
        os.makedirs(dotfiles)
        target = os.path.join(dotfiles, "settings.json")
        with open(target, "w") as handle:
            json.dump({"note": "x"}, handle)
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        os.symlink(target, self.settings_path)
        self.install_ok()
        self.assertTrue(os.path.islink(self.settings_path))
        self.assertEqual(os.path.realpath(self.settings_path), os.path.realpath(target))
        with open(target) as handle:
            self.assertIn("Bash(ls:*)", json.load(handle)["permissions"]["allow"])


class TestBackups(AtomicInstallTestCase):

    def test_immutable_backup_is_created_once_with_the_pre_run_bytes(self):
        before = self.write_settings({"note": "café"})
        self.install_ok()
        self.assertEqual(read(self.immutable()), before)
        self.assertNotEqual(read(self.settings_path), before)
        self.install_ok()
        self.assertEqual(read(self.immutable()), before)

    def test_a_backup_that_cannot_be_written_stops_the_run(self):
        # The `cp -p` this replaced aborted under `set -e`; the atomic create
        # must stay loud rather than skip the only pre-toolkit copy.
        before = self.write_settings({"note": "x"})
        claude_dir = os.path.dirname(self.settings_path)
        os.chmod(claude_dir, 0o555)
        self.addCleanup(os.chmod, claude_dir, 0o755)
        result = self.run_install()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("could not save the snapshot of settings.json", result.stdout + result.stderr)
        self.assertFalse(os.path.exists(self.immutable()))
        self.assertEqual(read(self.settings_path), before)

    def test_timed_backup_holds_the_pre_run_bytes_and_no_temp_is_left(self):
        before = self.write_settings({"note": "x"})
        self.install_ok()
        timed = glob.glob(self.settings_path + ".claude-code-toolkit-*.bak")
        self.assertTrue(timed)
        self.assertIn(before, [read(path) for path in timed])
        leftovers = [name for name in os.listdir(os.path.dirname(self.settings_path))
                     if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
