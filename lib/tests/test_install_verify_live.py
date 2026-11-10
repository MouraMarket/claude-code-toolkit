#!/usr/bin/env python3
"""install.sh --verify-live, and the informational Live and Guards lines of --check.

Every run sets or unsets CLAUDE_CODE_SESSION_ID explicitly: this suite may itself
run inside a Claude Code session that exports one.
"""

import datetime
import hashlib
import json
import re
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

# The module, not the class: a TestCase imported by name is collected here
# too, and its own cases would run a second time under this suite's name.
import test_install_check_fails as check_fails  # noqa: E402
import install_fixture as fx  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import install_record  # noqa: E402

SID = "live-sess-1"
TESTED = "2.1.306"
CONTINUITY = "toolkit-continuity@claude-code-toolkit"
MUTATING = ("plugin install", "plugin enable", "plugin marketplace add",
            "plugin marketplace remove", "plugin remove", "plugin uninstall")
HEARTBEAT_DIR = os.path.join(".claude", "claude-code-toolkit", "heartbeat")


class VerifyLiveTestCase(check_fails.CheckVerdictTestCase):

    def setUp(self):
        super().setUp()
        # run_install copies os.environ: drop any ambient id so unset really is unset.
        saved = os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, "CLAUDE_CODE_SESSION_ID", saved)

    def run_live(self, args, session=None):
        env = {"CLAUDE_TOOLKIT_MANAGED_DIR": self.managed}
        if session is not None:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return self.run_install(args, extra_env=env)

    def enable_selected(self, extra=()):
        """Enable core and continuity (the no-config selection) plus `extra`, so a
        selected profile reads as installed rather than configured."""
        settings = self.settings()
        settings["enabledPlugins"] = {pid: True for pid in
                                      (fx.PLUGIN_ID, CONTINUITY) + tuple(extra)}
        self.write_settings(settings)

    def seed_profile(self, plugin_dir="plugin-continuity", sha=None):
        """A start record for a profile plugin, on its current hooks.json."""
        root = os.path.join(self.repo, plugin_dir)
        with open(os.path.join(root, "hooks", "hooks.json"), "rb") as handle:
            current = hashlib.sha256(handle.read()).hexdigest()
        directory = os.path.join(self.home, HEARTBEAT_DIR)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, "%s.%s.start" % (SID, plugin_dir)), "w") as handle:
            handle.write("started_at=1\nplugin_root=%s\nroot_source=env\n"
                         "hooks_sha256=%s\n" % (root, current if sha is None else sha))

    def seed_version(self, version):
        folder = os.path.join(self.home, ".claude", "projects", "proj-a")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, SID + ".jsonl"), "w") as handle:
            handle.write(json.dumps({"type": "user", "version": version}) + "\n")

    def seed_all(self, version=TESTED):
        """Core and continuity enabled and proven; the session on `version`."""
        self.enable_selected()
        self.seed_records()
        self.seed_profile()
        if version:
            self.seed_version(version)

    def seed_records(self, sha=None):
        plugin = os.path.join(self.repo, "plugin")
        with open(os.path.join(plugin, "hooks", "hooks.json"), "rb") as handle:
            current = hashlib.sha256(handle.read()).hexdigest()
        directory = os.path.join(self.home, HEARTBEAT_DIR)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, SID + ".fired"), "w") as handle:
            handle.write("fired_at=1\nplugin_root=%s\nroot_source=env\n" % plugin)
        with open(os.path.join(directory, SID + ".start"), "w") as handle:
            handle.write("started_at=1\nplugin_root=%s\nroot_source=env\n"
                         "hooks_sha256=%s\n" % (plugin, current if sha is None else sha))


class TestVerifyLive(VerifyLiveTestCase):

    def out(self, proc):
        return proc.stdout + proc.stderr

    def test_unset_exits_1_and_says_through_claude(self):
        proc = self.run_live(["--verify-live"])
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn("through Claude Code", proc.stdout)

    def test_set_without_records_exits_1(self):
        proc = self.run_live(["--verify-live"], session=SID)
        self.assertEqual(1, proc.returncode, self.out(proc))

    def test_valid_records_exit_0(self):
        self.seed_all()
        proc = self.run_live(["--verify-live"], session=SID)
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertIn("hooks fired", proc.stdout)

    def test_all_proven_on_tested_version_is_runtime_proven(self):
        self.seed_all()
        proc = self.run_live(["--verify-live"], session=SID)
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertIn("core (toolkit-core): runtime-proven", proc.stdout)
        self.assertIn("continuity (toolkit-continuity): runtime-proven", proc.stdout)
        self.assertNotIn("unverified", proc.stdout)

    def test_all_proven_on_untested_version_exits_0_as_unverified(self):
        self.seed_all(version="9.9.9")
        proc = self.run_live(["--verify-live"], session=SID)
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertIn("core (toolkit-core): unverified", proc.stdout)
        self.assertIn("9.9.9", proc.stdout)
        self.assertNotIn("runtime-proven", proc.stdout)

    def test_optional_profile_enabled_without_record_exits_1_naming_it(self):
        self.seed_all()
        os.remove(os.path.join(self.home, HEARTBEAT_DIR,
                               SID + ".plugin-continuity.start"))
        proc = self.run_live(["--verify-live"], session=SID)
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn("continuity (toolkit-continuity): installed", proc.stdout)
        self.assertIn("core (toolkit-core): runtime-proven", proc.stdout)

    def test_selected_profile_not_enabled_exits_1(self):
        self.seed_all()
        settings = self.settings()
        del settings["enabledPlugins"][CONTINUITY]
        self.write_settings(settings)
        proc = self.run_live(["--verify-live"], session=SID)
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn("continuity (toolkit-continuity): configured", proc.stdout)

    def test_missing_readiness_helper_exits_1_unavailable(self):
        self.seed_all()
        os.remove(os.path.join(self.repo, "lib", "readiness.py"))
        proc = self.run_live(["--verify-live"], session=SID)
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn("readiness check unavailable", proc.stdout)

    def test_old_hooks_hash_exits_1(self):
        self.seed_records(sha="0" * 64)
        proc = self.run_live(["--verify-live"], session=SID)
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn("changed after", proc.stdout)

    def test_verify_live_changes_nothing(self):
        self.seed_all()
        self.run_live(["--verify-live"], session=SID)
        # One line per CLI call, arguments included: match on the prefix, or a
        # real call (which always carries an argument) never compares equal.
        log = self.log()
        for call in MUTATING:
            self.assertFalse([line for line in log if line.startswith(call)], log)

    def test_missing_helper_degrades_not_crashes(self):
        os.remove(os.path.join(self.repo, "lib", "hook_heartbeat.py"))
        proc = self.run_live(["--check"], session=SID)
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertIn("live check unavailable", proc.stdout)
        proc = self.run_live(["--verify-live"], session=SID)
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn("live check unavailable", proc.stdout)


class TestCheckLiveLine(VerifyLiveTestCase):

    def test_check_unset_exits_0_and_points_at_verify_live(self):
        proc = self.run_live(["--check"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("--verify-live", proc.stdout)

    def test_check_set_and_proven_shows_live_line_exit_0(self):
        self.seed_records()
        proc = self.run_live(["--check"], session=SID)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("Live: core's (toolkit-core) hooks fired", proc.stdout)

    def test_check_set_not_proven_still_exits_0(self):
        proc = self.run_live(["--check"], session=SID)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("Live:", proc.stdout)

    def test_check_prints_readiness_block_and_exit_is_unchanged(self):
        baseline = self.run_live(["--check"], session=SID)
        self.assertIn("Readiness:", baseline.stdout)
        # Unproven profiles (verify-live would exit 1) leave --check at 0.
        self.assertIn("core (toolkit-core): configured", baseline.stdout)
        self.assertEqual(0, baseline.returncode, baseline.stdout + baseline.stderr)
        self.seed_all()
        proc = self.run_live(["--check"], session=SID)
        self.assertEqual(baseline.returncode, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("core (toolkit-core): runtime-proven", proc.stdout)

    def test_check_readiness_crash_prints_unavailable_exit_unchanged(self):
        baseline = self.run_live(["--check"], session=SID)
        os.remove(os.path.join(self.repo, "lib", "readiness.py"))
        proc = self.run_live(["--check"], session=SID)
        self.assertIn("Readiness: check unavailable", proc.stdout)
        self.assertEqual(baseline.returncode, proc.returncode, proc.stdout + proc.stderr)


class TestCheckGuardsLine(VerifyLiveTestCase):
    """The informational Guards line: never changes --check's exit status."""

    GUARD_SID = "abcdef12-0000-4000-8000-000000000000"

    def setUp(self):
        super().setUp()
        self.config_dir = os.path.join(self.root, "cfg")

    def write_era(self):
        install_record.finish(install_record.record_path(self.home), "/checkout",
                              "abc123", ["core"], "2020-01-01T00:00:00Z")

    def write_transcript(self):
        """A real-shape transcript, started yesterday, whose Bash call ran."""
        start = (datetime.datetime.utcnow() - datetime.timedelta(days=1)
                 ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        folder = os.path.join(self.config_dir, "projects", "proj-a")
        os.makedirs(folder)
        lines = [
            {"type": "user", "timestamp": start,
             "message": {"role": "user", "content": "hello"}},
            {"type": "assistant", "timestamp": start,
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "id": "toolu_1", "name": "Bash",
                  "input": {"command": "ls"}}]}},
            {"type": "user", "timestamp": start,
             "message": {"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_1",
                  "is_error": False, "content": "ok"}]}},
        ]
        with open(os.path.join(folder, self.GUARD_SID + ".jsonl"), "w") as handle:
            handle.write("".join(json.dumps(line) + "\n" for line in lines))

    def write_fired(self):
        directory = os.path.join(self.home, HEARTBEAT_DIR)
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, self.GUARD_SID + ".fired"), "w") as handle:
            handle.write("fired_at=1\n")

    def run_check(self):
        env = {"CLAUDE_TOOLKIT_MANAGED_DIR": self.managed,
               "CLAUDE_CONFIG_DIR": self.config_dir}
        return self.run_install(["--check"], extra_env=env)

    def guards(self, proc):
        """(marker, lines printed after it): the marker is ok()'s "OK" or act()'s
        "->". The sandbox --check already exits 1, so an exit status alone cannot
        show the Guards block ending the run; output continuing past it can."""
        lines = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout).splitlines()
        index = next(i for i, line in enumerate(lines) if "Guards:" in line)
        marker = lines[index].split()[0]
        return marker, len([l for l in lines[index + 1:] if l.strip()])

    def test_ran_and_not_fired_is_named_and_exit_unchanged(self):
        self.write_era()
        baseline = self.run_check()
        self.write_transcript()
        proc = self.run_check()
        self.assertIn("Guards:", proc.stdout)
        self.assertIn(self.GUARD_SID[:8], proc.stdout)
        self.assertNotIn("hooks fired in every session", proc.stdout)
        self.assertEqual(baseline.returncode, proc.returncode, proc.stdout + proc.stderr)
        marker, after = self.guards(proc)
        self.assertEqual(marker, "->")
        self.assertGreater(after, 0, "install.sh stopped at the Guards line")

    def test_fired_session_is_clean(self):
        self.write_era()
        self.write_transcript()
        self.write_fired()
        proc = self.run_check()
        self.assertIn("Guards: hooks fired in every session started since", proc.stdout)
        self.assertEqual(self.guards(proc)[0], "OK")

    def test_no_install_record_says_so(self):
        self.write_transcript()
        proc = self.run_check()
        self.assertIn("Guards: no first-install time recorded yet", proc.stdout)

    def test_missing_helper_degrades_and_exit_unchanged(self):
        self.write_era()
        baseline = self.run_check()
        os.remove(os.path.join(self.repo, "lib", "hook_heartbeat.py"))
        proc = self.run_check()
        self.assertIn("Guards: check unavailable", proc.stdout)
        self.assertEqual(baseline.returncode, proc.returncode, proc.stdout + proc.stderr)
        self.assertGreater(self.guards(proc)[1], 0, "install.sh stopped at the Guards line")


if __name__ == "__main__":
    unittest.main()
