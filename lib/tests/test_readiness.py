#!/usr/bin/env python3
"""lib/readiness.py: per-profile state, the version rule, the transcript tail read,
and the CLI's exit codes, over fixture repos and homes built in tempdirs.

Every run sets or clears CLAUDE_CODE_SESSION_ID and CLAUDE_CONFIG_DIR explicitly:
this suite may itself run inside a Claude Code session that exports them.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, LIB)
import hook_heartbeat as hb  # noqa: E402
import readiness  # noqa: E402

SID = "ready-sess-1"
TESTED = "2.1.306"
OTHER = "2.1.999"


def registry(tested=TESTED):
    data = {"profiles": {
        "core": {"plugin": "toolkit-core", "dir": "plugin", "default": True,
                 "requires": [], "agents": False, "summary": "core"},
        "builder": {"plugin": "toolkit-build", "dir": "plugin-build", "default": False,
                    "requires": ["core"], "agents": False, "summary": "build",
                    "features": ["Workflow tool"]}}}
    if tested:
        data["tested_claude_code"] = tested
    return data


class ReadinessTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = os.path.join(self.root, "home")
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.home, ".claude"))
        os.makedirs(os.path.join(self.home, hb.RECORD_DIR))
        self.write_repo()
        saved = {k: os.environ.pop(k, None)
                 for k in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_SESSION_ID")}
        for key, value in saved.items():
            if value is not None:
                self.addCleanup(os.environ.__setitem__, key, value)
        self.config = os.path.join(self.root, "config.json")
        self.write_config({"profiles": ["builder"]})

    def write_repo(self, tested=TESTED):
        for directory, hooks in (("plugin", {"SessionStart": [], "PreToolUse": []}),
                                 ("plugin-build", {"SessionStart": []})):
            os.makedirs(os.path.join(self.repo, directory, "hooks"), exist_ok=True)
            with open(os.path.join(self.repo, directory, "hooks", "hooks.json"), "w") as h:
                json.dump({"hooks": hooks}, h)
        os.makedirs(os.path.join(self.repo, "claude"), exist_ok=True)
        with open(os.path.join(self.repo, "claude", "profiles.json"), "w") as handle:
            json.dump(registry(tested), handle)

    def write_config(self, data):
        with open(self.config, "w") as handle:
            json.dump(data, handle)

    def enable(self, *plugins):
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as handle:
            json.dump({"enabledPlugins": {p + "@claude-code-toolkit": True
                                          for p in plugins}}, handle)

    def sha(self, directory):
        with open(os.path.join(self.repo, directory, "hooks", "hooks.json"), "rb") as h:
            return hashlib.sha256(h.read()).hexdigest()

    def record(self, name, body):
        path = os.path.join(self.home, hb.RECORD_DIR, "%s.%s" % (SID, name))
        with open(path, "w") as handle:
            handle.write(body)

    def prove_core(self):
        root = os.path.join(self.repo, "plugin")
        self.record("fired", "fired_at=1\nplugin_root=%s\nroot_source=env\n" % root)
        self.record("start", "started_at=1\nplugin_root=%s\nroot_source=env\n"
                    "hooks_sha256=%s\n" % (root, self.sha("plugin")))

    def prove_build(self):
        root = os.path.join(self.repo, "plugin-build")
        self.record("plugin-build.start", "started_at=1\nplugin_root=%s\nroot_source=env\n"
                    "hooks_sha256=%s\n" % (root, self.sha("plugin-build")))

    def transcript(self, lines, name=SID + ".jsonl"):
        folder = os.path.join(self.home, ".claude", "projects", "proj-a")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "w") as handle:
            handle.write(lines if isinstance(lines, str) else
                         "".join(json.dumps(line) + "\n" for line in lines))

    def report(self, session=SID):
        with mock.patch.object(readiness.profiles, "selected",
                               return_value=(["core", "builder"], None, False)):
            return readiness.report(self.repo, self.home, session)

    def states(self, session=SID):
        return {row["name"]: row["state"] for row in self.report(session)["profiles"]}


class TestStates(ReadinessTestCase):

    def test_configured_when_not_enabled(self):
        self.enable("toolkit-core")
        self.assertEqual({"core": "installed", "builder": "configured"}, self.states())

    def test_installed_when_enabled_but_no_record(self):
        self.enable("toolkit-core", "toolkit-build")
        result = self.report()
        self.assertEqual({"core": "installed", "builder": "installed"}, self.states())
        self.assertIn("plugin-build", result["profiles"][1]["reason"])

    def test_installed_without_a_session(self):
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.prove_build()
        self.assertEqual({"core": "installed", "builder": "installed"}, self.states(None))

    def test_runtime_proven_needs_tested_session_version(self):
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.prove_build()
        self.transcript([{"version": TESTED}])
        self.assertEqual({"core": "runtime-proven", "builder": "runtime-proven"}, self.states())

    def test_proven_hooks_on_another_version_are_unverified(self):
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.prove_build()
        self.transcript([{"version": OTHER}])
        result = self.report()
        self.assertEqual({"core": "unverified", "builder": "unverified"}, self.states())
        reason = result["profiles"][0]["reason"]
        self.assertIn(OTHER, reason)
        self.assertIn(TESTED, reason)

    def test_proven_hooks_with_unknown_session_version_are_unverified(self):
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.prove_build()
        self.assertEqual({"core": "unverified", "builder": "unverified"}, self.states())
        self.assertIn("unknown", self.report()["profiles"][0]["reason"])

    def test_path_version_never_proves(self):
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.prove_build()
        with mock.patch.object(readiness, "path_version", return_value=TESTED):
            self.assertEqual({"core": "unverified", "builder": "unverified"}, self.states())

    def test_no_tested_version_is_never_proven(self):
        self.write_repo(tested=None)
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.prove_build()
        self.transcript([{"version": TESTED}])
        self.assertEqual({"core": "unverified", "builder": "unverified"}, self.states())

    def test_one_profile_unproven_others_proven(self):
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.transcript([{"version": TESTED}])
        self.assertEqual({"core": "runtime-proven", "builder": "installed"}, self.states())

    def test_needs_lists_events_then_features(self):
        self.enable("toolkit-core", "toolkit-build")
        rows = {r["name"]: r["needs"] for r in self.report()["profiles"]}
        self.assertEqual(["SessionStart", "PreToolUse"], rows["core"])
        self.assertEqual(["SessionStart", "Workflow tool"], rows["builder"])

    def test_unreadable_settings_cannot_be_computed(self):
        with self.assertRaises(readiness.ReadinessError):
            self.report()


class TestSessionVersion(ReadinessTestCase):

    def test_latest_nonempty_string_wins(self):
        self.transcript([{"version": "1.0.0"}, {"type": "x"}, {"version": "2.0.0"},
                         {"version": ""}, {"version": 7}])
        self.assertEqual("2.0.0", readiness.session_version(self.home, SID))

    def test_malformed_lines_are_skipped(self):
        self.transcript('{"version": "1.2.3"}\nnot json\n[1, 2]\n\n{"version": \n')
        self.assertEqual("1.2.3", readiness.session_version(self.home, SID))

    def test_no_file_is_none(self):
        self.assertIsNone(readiness.session_version(self.home, SID))

    def test_no_version_anywhere_is_none(self):
        self.transcript([{"type": "user"}])
        self.assertIsNone(readiness.session_version(self.home, SID))

    def test_other_sessions_transcript_is_ignored(self):
        self.transcript([{"version": OTHER}], name="someone-else.jsonl")
        self.assertIsNone(readiness.session_version(self.home, SID))

    def test_bad_session_ids_are_rejected(self):
        self.transcript([{"version": OTHER}], name="x.jsonl")
        for bad in (None, "", "../x", "*", "a/b", "a.b", 7):
            self.assertIsNone(readiness.session_version(self.home, bad), repr(bad))

    def test_only_the_tail_is_read(self):
        head = json.dumps({"version": "1.0.0"}) + "\n"
        filler = (json.dumps({"pad": "x" * 1000}) + "\n") * 400
        self.transcript(head + filler)
        self.assertGreater(len(filler), readiness.TRANSCRIPT_TAIL_BYTES)
        self.assertIsNone(readiness.session_version(self.home, SID))

    def test_honours_claude_config_dir(self):
        folder = os.path.join(self.root, "cfg", "projects", "p")
        os.makedirs(folder)
        with open(os.path.join(folder, SID + ".jsonl"), "w") as handle:
            handle.write(json.dumps({"version": "3.3.3"}) + "\n")
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(self.root, "cfg")}):
            self.assertEqual("3.3.3", readiness.session_version(self.home, SID))


class TestPathVersion(unittest.TestCase):

    def run_with(self, **kwargs):
        with mock.patch.object(readiness.subprocess, "run", **kwargs):
            return readiness.path_version()

    def test_leading_version(self):
        done = mock.Mock(stdout="2.1.306 (Claude Code)\n")
        self.assertEqual("2.1.306", self.run_with(return_value=done))

    def test_garbage_is_none(self):
        self.assertIsNone(self.run_with(return_value=mock.Mock(stdout="hello")))

    def test_failure_never_raises(self):
        self.assertIsNone(self.run_with(side_effect=FileNotFoundError()))
        self.assertIsNone(self.run_with(side_effect=subprocess.TimeoutExpired("claude", 5)))


class TestCli(ReadinessTestCase):

    def cli(self, session=SID, repo=None):
        env = dict(os.environ, HOME=self.home, CLAUDE_TOOLKIT_CONFIG=self.config,
                   PATH="/usr/bin:/bin")
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        return subprocess.run([sys.executable, os.path.join(LIB, "readiness.py"), "report",
                               repo or self.repo], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)

    def test_exit_0_all_proven_prints_header_lines_and_needs(self):
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.prove_build()
        self.transcript([{"version": TESTED}])
        proc = self.cli()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        lines = proc.stdout.splitlines()
        self.assertIn("tested with Claude Code " + TESTED, lines[0])
        self.assertIn("this session runs " + TESTED, lines[0])
        self.assertIn("claude on PATH:", lines[0])
        self.assertTrue(any(l.startswith("core (toolkit-core): runtime-proven") for l in lines))
        self.assertTrue(any(l.startswith("builder (toolkit-build): runtime-proven")
                            for l in lines))
        self.assertIn("  needs: SessionStart, Workflow tool", lines)

    def test_exit_0_unverified_names_unknown_session(self):
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.prove_build()
        proc = self.cli()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("this session runs unknown", proc.stdout)
        self.assertIn("builder (toolkit-build): unverified", proc.stdout)

    def test_exit_1_installed(self):
        self.enable("toolkit-core", "toolkit-build")
        self.prove_core()
        self.transcript([{"version": TESTED}])
        proc = self.cli()
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("builder (toolkit-build): installed", proc.stdout)

    def test_exit_1_configured(self):
        self.enable("toolkit-core")
        self.prove_core()
        proc = self.cli()
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("builder (toolkit-build): configured", proc.stdout)

    def test_exit_1_without_a_session(self):
        self.enable("toolkit-core", "toolkit-build")
        proc = self.cli(session=None)
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)

    def test_exit_2_when_it_cannot_be_computed(self):
        proc = self.cli(repo=os.path.join(self.root, "nope"))
        self.assertEqual(2, proc.returncode, proc.stdout + proc.stderr)
        self.assertTrue(proc.stderr.strip())
        self.assertEqual("", proc.stdout)

    def test_exit_2_without_settings(self):
        proc = self.cli()
        self.assertEqual(2, proc.returncode, proc.stdout + proc.stderr)

    def test_exit_2_on_usage(self):
        proc = subprocess.run([sys.executable, os.path.join(LIB, "readiness.py")],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(2, proc.returncode)


if __name__ == "__main__":
    unittest.main()
