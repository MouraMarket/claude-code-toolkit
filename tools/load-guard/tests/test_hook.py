#!/usr/bin/env python3
"""Tests for `claude-load-guard`, the PreToolUse hook, driven through subprocess.

The pure verdicts are covered in test_load_guard_lib.py; this file proves the
real executable reads the payload, config and environment and prints the right
thing. Every run gets an explicit environment so the developer's own config and
LOAD_GUARD_ALLOW never leak in.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
HOOK = os.path.join(TOOL, "claude-load-guard")


class TestHookExecutable(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = os.path.realpath(tmp.name)

    def config(self, obj=None, raw=None):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as fh:
            fh.write(raw if raw is not None else json.dumps(obj))
        return path

    def run_hook(self, stdin, config=None, extra_env=None):
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp,
               "CLAUDE_TOOLKIT_CONFIG": config or os.path.join(
                   self.tmp, "no-such-config.json")}
        env.update(extra_env or {})
        return subprocess.run([sys.executable, HOOK], input=stdin,
                              capture_output=True, text=True, env=env,
                              timeout=60)

    def bash(self, command, **kwargs):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}}
        return self.run_hook(json.dumps(payload), **kwargs)

    def assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def assert_denied(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        hso = json.loads(proc.stdout)["hookSpecificOutput"]
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        self.assertEqual(hso["permissionDecision"], "deny")
        return hso["permissionDecisionReason"]

    def test_heavy_command_is_denied_with_exact_rewrite(self):
        reason = self.assert_denied(self.bash("ffmpeg -i a b"))
        self.assertIn("ffmpeg", reason)
        self.assertIn("`claude-heavy-run ffmpeg -i a b`", reason)
        self.assertIn("LOAD_GUARD_ALLOW=1", reason)
        self.assertIn("human", reason)

    def test_rewrite_names_only_the_offending_segment(self):
        reason = self.assert_denied(self.bash("ls && ffmpeg -i a b | wc -l"))
        self.assertIn("`claude-heavy-run ffmpeg -i a b`", reason)

    def test_rewrite_keeps_leading_assignments_before_the_wrapper(self):
        reason = self.assert_denied(self.bash("FOO=1 ffmpeg -i a b"))
        self.assertIn("`FOO=1 claude-heavy-run ffmpeg -i a b`", reason)

    def test_ordinary_command_prints_nothing(self):
        self.assert_silent(self.bash("ls"))

    def test_wrapped_command_prints_nothing(self):
        self.assert_silent(self.bash("claude-heavy-run ffmpeg -i a b"))

    def test_non_bash_tool_prints_nothing(self):
        payload = {"tool_name": "Write",
                   "tool_input": {"command": "ffmpeg -i a b"}}
        self.assert_silent(self.run_hook(json.dumps(payload)))

    def test_override_in_hook_env_prints_nothing(self):
        proc = self.bash("ffmpeg -i a b",
                         extra_env={"LOAD_GUARD_ALLOW": "1"})
        self.assert_silent(proc)

    def test_override_prefix_on_command_prints_nothing(self):
        self.assert_silent(self.bash("LOAD_GUARD_ALLOW=1 ffmpeg -i a b"))

    def test_override_wins_over_a_broken_config(self):
        proc = self.bash("ffmpeg -i a b", config=self.config(raw="{nope"),
                         extra_env={"LOAD_GUARD_ALLOW": "1"})
        self.assert_silent(proc)

    def test_disabled_in_config_prints_nothing(self):
        cfg = self.config({"load_guard": {"enabled": False}})
        self.assert_silent(self.bash("ffmpeg -i a b", config=cfg))

    def test_broken_config_fails_strict_and_still_denies(self):
        cfg = self.config(raw="{nope")
        self.assert_denied(self.bash("ffmpeg -i a b", config=cfg))

    def test_extra_command_in_config_is_denied(self):
        cfg = self.config({"load_guard": {"commands": ["reel_extract"]}})
        reason = self.assert_denied(
            self.bash("python3 reel_extract.py in.mp4", config=cfg))
        self.assertIn("reel_extract", reason)

    def test_extra_command_is_not_heavy_without_config(self):
        self.assert_silent(self.bash("python3 reel_extract.py in.mp4"))

    def test_malformed_stdin_prints_nothing_and_exits_zero(self):
        self.assert_silent(self.run_hook("{not json"))

    def test_non_object_payload_prints_nothing(self):
        self.assert_silent(self.run_hook("[1, 2]"))

    def test_missing_command_prints_nothing(self):
        payload = {"tool_name": "Bash", "tool_input": {}}
        self.assert_silent(self.run_hook(json.dumps(payload)))


if __name__ == "__main__":
    unittest.main()
