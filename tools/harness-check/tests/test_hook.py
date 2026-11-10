#!/usr/bin/env python3
"""Behavioural tests for `claude-harness-check --hook`.

Hermetic throughout: every subprocess env is built from scratch (never
`dict(os.environ)`) with only `PATH`, `HOME`, `USERPROFILE`,
`CLAUDE_TOOLKIT_CONFIG`, `HARNESS_CHECK_STATE_PATH` and
`HARNESS_CHECK_NOTICED_PATH` set — so a test can never pass because of
something already installed on this machine, and a subprocess test that
inherited the ambient environment would assert a different contract per
machine (see claude-code-toolkit's CLAUDE.md on hermetic subprocess tests).

`PATH` carries only a per-test directory holding a `python3` symlink to
`sys.executable`: the hook's shebang is `#!/usr/bin/env python3`, `env`
resolves that name through the child's own `PATH`, and the interpreter
running the tests is not necessarily named `python3`.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, TOOL_DIR)
import harness_lib as lib

HOOK = os.path.join(TOOL_DIR, "claude-harness-check")


class HookCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.python_dir = os.path.join(self.tmp, "pybin")
        os.makedirs(self.python_dir)
        os.symlink(sys.executable, os.path.join(self.python_dir, "python3"))
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home, exist_ok=True)
        self.config_path = os.path.join(self.tmp, "config.json")
        self.results_path = os.path.join(self.tmp, "results.json")
        self.noticed_path = os.path.join(self.tmp, "noticed.json")
        self.transcript_path = os.path.join(self.tmp, "transcript.jsonl")

    # --- fixtures ------------------------------------------------------

    def write_config(self, enabled):
        with open(self.config_path, "w") as handle:
            json.dump({"harness_check": {"enabled": enabled}}, handle)

    def write_transcript(self, version):
        with open(self.transcript_path, "w") as handle:
            handle.write(json.dumps({"version": version}) + "\n")

    def write_results(self, versions):
        with open(self.results_path, "w") as handle:
            json.dump({"versions": versions}, handle)

    def write_noticed(self, noticed):
        with open(self.noticed_path, "w") as handle:
            json.dump({"noticed": noticed}, handle)

    def read_noticed(self):
        with open(self.noticed_path) as handle:
            return json.load(handle)

    # --- running the real executable ------------------------------------

    def env(self, config_path=None):
        """Built from scratch — never `dict(os.environ)`."""
        return {
            "PATH": self.python_dir,
            "HOME": self.home,
            "USERPROFILE": self.home,
            "CLAUDE_TOOLKIT_CONFIG": (
                config_path if config_path is not None else self.config_path),
            "HARNESS_CHECK_STATE_PATH": self.results_path,
            "HARNESS_CHECK_NOTICED_PATH": self.noticed_path,
        }

    def run_hook(self, payload, config_path=None):
        stdin = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            [HOOK, "--hook"], input=stdin, capture_output=True, text=True,
            env=self.env(config_path=config_path))


# ============================================================ enabled/speaks

class TestUnverifiedNotice(HookCase):

    def test_enabled_unverified_version_speaks_on_user_prompt_submit(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        done = self.run_hook({"session_id": "s1",
                              "transcript_path": self.transcript_path,
                              "hook_event_name": "UserPromptSubmit"})
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"],
            "UserPromptSubmit")
        self.assertTrue(payload["hookSpecificOutput"]["additionalContext"]
                        .startswith("[harness-check] Claude Code 2.1.283"))

    def test_enabled_unverified_version_speaks_on_post_tool_batch(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        done = self.run_hook({"session_id": "s1",
                              "transcript_path": self.transcript_path,
                              "hook_event_name": "PostToolBatch"})
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "PostToolBatch")
        self.assertTrue(payload["hookSpecificOutput"]["additionalContext"]
                        .startswith("[harness-check] Claude Code 2.1.283"))

    def test_second_invocation_same_session_is_silent(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        payload = {"session_id": "s1", "transcript_path": self.transcript_path}
        first = self.run_hook(payload)
        self.assertNotEqual(first.stdout, "")
        second = self.run_hook(payload)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")

    def test_new_session_same_unverified_version_speaks_again(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        self.run_hook({"session_id": "s1",
                       "transcript_path": self.transcript_path})
        second = self.run_hook({"session_id": "s2",
                                "transcript_path": self.transcript_path})
        self.assertNotEqual(second.stdout, "")


# ============================================================ silent cases

class TestSilentConditions(HookCase):

    def test_disabled_config_absent_is_silent_and_creates_no_files(self):
        """The positive control for this class: the enabled case creates the
        noticed file and never the results file (see TestUnverifiedNotice)."""
        self.write_transcript("2.1.283")
        done = self.run_hook({"session_id": "s1",
                              "transcript_path": self.transcript_path},
                             config_path="/nonexistent-config.json")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")
        self.assertFalse(os.path.exists(self.noticed_path))
        self.assertFalse(os.path.exists(self.results_path))

    def test_enabled_case_creates_the_noticed_file_never_the_results_file(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        done = self.run_hook({"session_id": "s1",
                              "transcript_path": self.transcript_path})
        self.assertNotEqual(done.stdout, "")
        self.assertTrue(os.path.exists(self.noticed_path))
        self.assertFalse(os.path.exists(self.results_path))

    def test_agent_id_present_is_silent(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        done = self.run_hook({"session_id": "s1",
                              "transcript_path": self.transcript_path,
                              "agent_id": "agent-1"})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_version_recorded_pass_is_silent(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        self.write_results({"2.1.283": {"result": "pass",
                                        "checked_at": "2026-09-29T00:00:00",
                                        "failures": [], "rev": lib.RECORD_REV}})
        done = self.run_hook({"session_id": "s1",
                              "transcript_path": self.transcript_path})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_legacy_pass_without_rev_still_notices(self):
        # A pass written before RECORD_REV existed may have skipped H5(c)
        # unchecked, so its provenance is unknown: the hook speaks once.
        self.write_config(True)
        self.write_transcript("2.1.283")
        self.write_results({"2.1.283": {"result": "pass",
                                        "checked_at": "2026-09-29T00:00:00",
                                        "failures": []}})
        done = self.run_hook({"session_id": "s1",
                              "transcript_path": self.transcript_path})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("has not been verified", done.stdout)

    def test_malformed_stdin_exits_zero_silently(self):
        done = self.run_hook("not json")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_nonexistent_transcript_path_exits_zero_silently(self):
        self.write_config(True)
        done = self.run_hook({"session_id": "s1",
                              "transcript_path": os.path.join(
                                  self.tmp, "nope.jsonl")})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_missing_session_id_exits_zero_silently(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        done = self.run_hook({"transcript_path": self.transcript_path})
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")


# =================================================================== exit 0

class TestAlwaysExitsZero(HookCase):

    def test_every_case_here_exits_zero(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        cases = [
            ({"session_id": "s1", "transcript_path": self.transcript_path},
             None),
            ("garbage", None),
            ({}, None),
            ({"session_id": "s1", "transcript_path": self.transcript_path},
             "/nonexistent-config.json"),
        ]
        for payload, config_path in cases:
            done = self.run_hook(payload, config_path=config_path)
            self.assertEqual(done.returncode, 0,
                             "payload=%r config=%r stderr=%r"
                             % (payload, config_path, done.stderr))


# ============================================================== round trip

class TestNoticedRoundTrip(HookCase):
    """The hook's own write, read back through the real lib functions."""

    def test_noticed_file_dedupes_through_a_second_process(self):
        self.write_config(True)
        self.write_transcript("2.1.283")
        payload = {"session_id": "s1", "transcript_path": self.transcript_path}
        self.run_hook(payload)
        doc = self.read_noticed()
        self.assertEqual(doc["noticed"]["s1"], "2.1.283")
        # A fresh process, same session and version, must see the write the
        # first process made — the dedupe key is the file, not memory.
        second = self.run_hook(payload)
        self.assertEqual(second.stdout, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
