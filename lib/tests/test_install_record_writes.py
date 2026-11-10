#!/usr/bin/env python3
"""install.sh keeps the install record: what it added to the user's settings.

Pinned here:
  - a fresh apply records exactly the rules, the exclude pattern, the commit
    and the checkout it wrote
  - a rule already in settings is NOT recorded (the installer did not add it)
  - a re-run never shrinks the record
  - --check never creates a record
  - the record is written BEFORE settings.json, and a record failure stops the
    write; a converged machine with no record fails the run
  - hook_manifest.wanted_status_line quotes a HOME that needs it

These drive the REAL install.sh against a sandbox $HOME (fixtures from
test_install_permissions_opt_out); nothing touches the real ~/.claude.
"""

import json
import os
import sys
import tempfile
import unittest

import install_fixture as fx
import test_install_permissions_opt_out as base
from test_install_permissions_opt_out import DENY, MERGE, PUSH

LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB)
import hook_manifest  # noqa: E402

PATTERN = "**/.claude/worktrees/*/CLAUDE.md"


class RecordWritesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.repo = base.make_fixture_repo(self.root)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.config_dir = os.path.join(self.home, ".config", "claude-code-toolkit")
        self.record_file = os.path.join(self.config_dir, "install-record.json")
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")

    def apply(self, settings=None, args=(), expect=0):
        if not os.path.isdir(self.config_dir):
            base.seed_home(self.home, {}, settings)
        elif settings is not None:
            os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
            with open(self.settings_path, "w") as handle:
                json.dump(settings, handle)
        result = base.run_install(self.repo, self.home, args)
        self.assertEqual(expect, result.returncode, result.stdout + result.stderr)
        return result

    def record(self):
        with open(self.record_file) as handle:
            return json.load(handle)

    def lock_config_dir(self):
        os.chmod(self.config_dir, 0o500)
        self.addCleanup(os.chmod, self.config_dir, 0o700)

    def test_fresh_apply_records_what_it_wrote(self):
        self.apply()
        record = self.record()
        self.assertEqual({"deny": [DENY], "ask": [PUSH, MERGE], "allow": []},
                         record["permissions_added"])
        self.assertEqual({"pattern": PATTERN, "created_key": True},
                         record["claude_md_excludes_added"])
        self.assertEqual(base.git("rev-parse", "HEAD", cwd=self.repo).stdout.strip(),
                         record["commit"])
        self.assertEqual(os.path.realpath(self.repo), record["checkout"])

    def test_rule_already_present_is_not_recorded(self):
        self.apply(settings={"permissions": {"ask": [PUSH]}})
        added = self.record()["permissions_added"]
        self.assertNotIn(PUSH, added["ask"])
        self.assertIn(MERGE, added["ask"])
        self.assertEqual([DENY], added["deny"])

    def test_second_apply_keeps_the_record(self):
        self.apply()
        first = self.record()["permissions_added"]
        self.apply()
        second = self.record()["permissions_added"]
        self.assertEqual(first, second)
        self.assertEqual(3, sum(len(v) for v in second.values()))

    def test_existing_exclude_key_is_not_created_by_us(self):
        self.apply(settings={"claudeMdExcludes": ["x"]})
        self.assertIs(False, self.record()["claude_md_excludes_added"]["created_key"])

    def test_check_creates_no_record(self):
        base.seed_home(self.home, {})
        base.run_install(self.repo, self.home, ("--check",))
        self.assertFalse(os.path.exists(self.record_file))

    def test_record_failure_stops_the_permissions_write(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        base.seed_home(self.home, {})
        self.lock_config_dir()
        result = base.run_install(self.repo, self.home)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("permissions NOT written", result.stdout)
        settings = {}
        if os.path.exists(self.settings_path):
            with open(self.settings_path) as handle:
                settings = json.load(handle)
        self.assertNotIn("permissions", settings)

    def test_record_failure_stops_the_excludes_write(self):
        # Every baseline rule is already present, so the permissions step adds
        # nothing and never touches the record; the excludes step is the first
        # writer, and its own record-first failure must stop its own write.
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        base.seed_home(self.home, {}, settings={"permissions": {
            "deny": [base.DENY], "ask": [base.PUSH, base.MERGE]}})
        self.lock_config_dir()
        result = base.run_install(self.repo, self.home)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("permissions NOT written", result.stdout)
        self.assertIn("claudeMdExcludes NOT written", result.stdout)
        with open(self.settings_path) as handle:
            self.assertNotIn("claudeMdExcludes", json.load(handle))

    def test_converged_recordless_machine_fails_the_run(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory modes")
        self.apply()
        os.remove(self.record_file)
        self.lock_config_dir()
        result = base.run_install(self.repo, self.home)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("install record could not be written",
                      result.stdout + result.stderr)

    def test_wanted_status_line_quotes_only_when_needed(self):
        repo = os.path.join(self.root, "sl")
        os.makedirs(os.path.join(repo, "claude"))
        with open(os.path.join(repo, "claude", "settings.host.json"), "w") as handle:
            json.dump({"statusLine": {"type": "command",
                                      "command": "__HOME__/.local/bin/claude-usage-line"}},
                      handle)
        spaced = hook_manifest.wanted_status_line(repo, "/Users/a b")
        self.assertTrue(spaced["command"].startswith("'/Users/a b'/.local/bin/"))
        plain = hook_manifest.wanted_status_line(repo, "/Users/ab")
        self.assertTrue(plain["command"].startswith("/Users/ab/.local/bin/"))
        with open(os.path.join(repo, "claude", "settings.host.json"), "w") as handle:
            json.dump({}, handle)
        self.assertIsNone(hook_manifest.wanted_status_line(repo, "/Users/ab"))


if __name__ == "__main__":
    unittest.main()
