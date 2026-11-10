"""Tests for `readonly_hook.py`, the PreToolUse hook that keeps a
trigger-eval child session read-only.

Every test runs the hook as a subprocess, matching the sibling suite's rule
(`test_skill_trigger.py`): a hook must be exercised through its
own executable, never only through an imported function, because a stub that
imports it clean can still crash at the shebang or the stdin read.
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
HOOK = os.path.join(TOOL_DIR, "readonly_hook.py")

ALLOWED = ("Read", "Grep", "Glob", "ToolSearch", "TodoWrite", "WebFetch", "WebSearch")

DENIED = (
    "Skill", "Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "Agent",
    "Task", "EnterWorktree", "CronCreate", "Artifact",
    "mcp__claude_ai_ClickUp__clickup_create_task",
    "mcp__searchable__get_current_date",
)

MALFORMED_PAYLOADS = ("", "not json", "[]", "{}", '{"tool_name": 5}')


def _run_hook(payload_text):
    return subprocess.run(
        [sys.executable, HOOK], input=payload_text, capture_output=True,
        text=True)


class TestAllowedToolsExitZero(unittest.TestCase):
    def test_each_allowed_tool_exits_zero_with_empty_stdout(self):
        for name in ALLOWED:
            with self.subTest(tool=name):
                result = _run_hook(json.dumps({"tool_name": name}))
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


class TestDeniedToolsExitTwo(unittest.TestCase):
    def test_each_denied_tool_exits_two_with_name_in_stderr(self):
        for name in DENIED:
            with self.subTest(tool=name):
                result = _run_hook(json.dumps({"tool_name": name}))
                self.assertEqual(result.returncode, 2)
                self.assertIn(name, result.stderr)


class TestMalformedPayloadsExitTwo(unittest.TestCase):
    def test_each_malformed_payload_exits_two(self):
        for payload_text in MALFORMED_PAYLOADS:
            with self.subTest(payload=payload_text):
                result = _run_hook(payload_text)
                self.assertEqual(result.returncode, 2)

    def test_a_crash_inside_main_still_exits_two(self):
        # A closed stdin makes `sys.stdin` None, so `main()` raises before any
        # JSON handling -- the only real input that reaches the __main__
        # wrapper's `except BaseException`. Without this test a mutation of
        # that branch to exit 0 (fail OPEN) survives the whole suite.
        result = subprocess.run(
            ["/bin/sh", "-c", 'exec "$0" "$1" <&-', sys.executable, HOOK],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("crashed", result.stderr)


class TestAllowedToolsFrozenset(unittest.TestCase):
    def test_allowed_tools_is_exactly_the_seven(self):
        spec = importlib.util.spec_from_file_location("readonly_hook", HOOK)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.ALLOWED_TOOLS, frozenset(ALLOWED))


class TestNonAsciiStdin(unittest.TestCase):
    def _stderr_for(self, tool_name):
        # Raw UTF-8 bytes in under a cp1252 stdio. Claude Code reads hook
        # stderr as UTF-8, so the child's output is decoded as UTF-8
        # whatever its own PYTHONIOENCODING is.
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        payload = json.dumps({"tool_name": tool_name, "tool_input": {}},
                             ensure_ascii=False).encode("utf-8")
        result = subprocess.run([sys.executable, HOOK], input=payload,
                                capture_output=True, env=env)
        return result.stderr.decode("utf-8")

    def test_non_ascii_tool_name_reaches_claude_code_as_utf8_under_cp1252(self):
        # U+02BB is not in cp1252: a cp1252 stderr cannot carry it at all.
        self.assertIn("Hawai\u02bbi", self._stderr_for("Hawai\u02bbi"))
        self.assertIn("\u00c9crire", self._stderr_for("\u00c9crire"))


if __name__ == "__main__":
    unittest.main()
