"""`generate_title`'s child `claude -p` call must set
`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` -- a title needs no memory, and skipping
it saves tokens. `claude-title-session` has no `.py`
extension, so it is loaded via `SourceFileLoader`, matching test_hook.py.
"""

import importlib.machinery
import importlib.util
import os
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK_PATH = os.path.join(ROOT, "claude-title-session")

spec = importlib.util.spec_from_loader(
    "hook_env_test",
    importlib.machinery.SourceFileLoader("hook_env_test", HOOK_PATH))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class _FakeCompletedProcess:
    def __init__(self):
        self.returncode = 0
        self.stdout = "a fine title"
        self.stderr = ""


class TestGenerateTitleDisablesAutoMemory(unittest.TestCase):
    def test_env_passed_to_subprocess_run_disables_auto_memory(self):
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeCompletedProcess()

        # Seed a conflicting parent environment first: an inherited value
        # must not be able to satisfy the assertion below.
        with mock.patch.dict(os.environ, {
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0"}):
            with mock.patch.object(hook.title_lib, "build_prompt",
                                    return_value="prompt"):
                with mock.patch.object(hook.subprocess, "run",
                                        side_effect=_fake_run):
                    hook.generate_title({}, "/tmp")

        self.assertEqual(
            captured["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")


class TestGenerateTitleDoesNotPersistSession(unittest.TestCase):
    def test_child_call_disables_session_persistence(self):
        # A persisted titling call becomes a ghost session that shadows the
        # real one in history search.
        captured = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _FakeCompletedProcess()

        with mock.patch.object(hook.title_lib, "build_prompt",
                                return_value="prompt"):
            with mock.patch.object(hook.subprocess, "run",
                                    side_effect=_fake_run):
                hook.generate_title({}, "/tmp")

        self.assertIn("--no-session-persistence", captured["cmd"])


if __name__ == "__main__":
    unittest.main()
