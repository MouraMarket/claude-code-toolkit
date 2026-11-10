"""Tests for claude-thread-deliver's `thread_handover.enabled` gate.

Run through subprocess with HOME redirected. `INDEX_PATH` and
`HANDOVER_DIR`'s default both resolve under `~/.claude`, computed once at
import time from the environment -- a subprocess run of this hook must never
point at this machine's real HOME, because `sweep()` MOVES files and having a
real handover silently relocated mid-use is exactly the disruption the grace
gates exist to prevent.

TestPluginDelivery runs both hooks the way Claude Code does: the command
strings from the continuity plugin's hooks.json, through `/bin/sh -c`, with
CLAUDE_PLUGIN_ROOT set.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(TOOL_DIR, "claude-thread-deliver")
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))
PLUGIN_ROOT = os.path.join(REPO, "plugin-continuity")


class TestEnabledGate(unittest.TestCase):
    """`thread_handover.enabled` gates claude-thread-deliver's main() inline,
    the way `claude-memory-routing` does."""

    def _register_pending(self, tmp, project_dir, created_session_id="AUTHOR"):
        """A real handover file plus an index record naming it, written
        directly under the redirected HOME -- so nothing here ever touches
        this machine's real ~/.claude."""
        claude_dir = os.path.join(tmp, ".claude", "thread-handover")
        os.makedirs(claude_dir, exist_ok=True)
        handover_path = os.path.join(tmp, "2026-08-10-gate-test-handover.md")
        with open(handover_path, "w") as handle:
            handle.write("# handover\n")
        index_path = os.path.join(claude_dir, "index.json")
        with open(index_path, "w") as handle:
            json.dump([{
                "path": handover_path,
                "project_dir": project_dir,
                "created_at": "2026-08-10T10:00:00",
                "created_session_id": created_session_id,
                "consumed_at": None,
                "consumed_session_id": None,
            }], handle)
        return handover_path

    def _run(self, config_blob, tmp, session_id):
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w") as handle:
            json.dump(config_blob, handle)
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = config_path
        env["HOME"] = tmp  # isolate INDEX_PATH / HANDOVER_DIR from the real machine
        payload = json.dumps({"session_id": session_id, "cwd": tmp})
        return subprocess.run([sys.executable, HOOK], input=payload, env=env,
                              capture_output=True, text=True, timeout=20)

    def test_disabled_exits_zero_with_empty_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._register_pending(tmp, tmp)
            proc = self._run({"thread_handover": {"enabled": False}}, tmp, "READER")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")

    def test_key_absent_still_announces_the_pending_handover(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._register_pending(tmp, tmp)
            proc = self._run({}, tmp, "READER")
            self.assertEqual(proc.returncode, 0)
            self.assertIn(path, proc.stdout)

    def test_pointer_names_the_configured_owner_without_a_pronoun(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._register_pending(tmp, tmp)
            proc = self._run({"owner_name": "Sam"}, tmp, "READER")
            self.assertIn("unless Sam attaches it", proc.stdout)
            self.assertIsNone(re.search(r"\b(he|she|him|her|his)\b", proc.stdout),
                              proc.stdout)

    def test_a_non_boolean_value_still_announces_the_pending_handover(self):
        # "no" is not `is False` -- a garbage value must not read as off.
        with tempfile.TemporaryDirectory() as tmp:
            path = self._register_pending(tmp, tmp)
            proc = self._run({"thread_handover": {"enabled": "no"}}, tmp, "READER")
            self.assertEqual(proc.returncode, 0)
            self.assertIn(path, proc.stdout)


def _plugin_command(event, executable):
    """The command string the continuity plugin registers for `executable`."""
    with open(os.path.join(PLUGIN_ROOT, "hooks", "hooks.json"),
              encoding="utf-8") as handle:
        hooks = json.load(handle)["hooks"]
    for group in hooks.get(event, []):
        for entry in group.get("hooks", []):
            if entry.get("command", "").endswith("/" + executable):
                return entry["command"]
    raise AssertionError("%s not registered on %s" % (executable, event))


class TestPluginDelivery(unittest.TestCase):
    """Register -> SessionStart announces -> a prompt naming it claims it."""

    INFINITE = {"arm_cap": float("inf"), "urgent_cap": float("inf"),
                "sweep_grace_sessions": float("inf"),
                "sweep_grace_days": float("inf")}

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.handover_dir = os.path.join(self.tmp, ".claude", "handovers")
        os.makedirs(self.handover_dir)
        self.path = os.path.join(self.handover_dir,
                                 "2026-08-10-demo--first-leg-handover.md")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("# handover\n")
        index_dir = os.path.join(self.tmp, ".claude", "thread-handover")
        os.makedirs(index_dir)
        self.index = os.path.join(index_dir, "index.json")
        with open(self.index, "w", encoding="utf-8") as handle:
            json.dump([{
                "path": self.path, "project_dir": self.tmp,
                "created_at": "2026-08-10T10:00:00",
                "created_session_id": "AUTHOR",
                "consumed_at": None, "consumed_session_id": None,
            }], handle)

    def run_hook(self, event, executable, payload, section):
        config = os.path.join(self.tmp, "config.json")
        with open(config, "w", encoding="utf-8") as handle:
            json.dump({"thread_handover": section}, handle)
        env = {"PATH": "/usr/bin:/bin", "HOME": self.tmp,
               "CLAUDE_TOOLKIT_CONFIG": config,
               "CLAUDE_PLUGIN_ROOT": PLUGIN_ROOT}
        return subprocess.run(
            ["/bin/sh", "-c", _plugin_command(event, executable)],
            input=json.dumps(payload), env=env, capture_output=True,
            text=True, timeout=30)

    def consumed_by(self):
        with open(self.index, encoding="utf-8") as handle:
            return json.load(handle)[0]["consumed_session_id"]

    def deliver_then_attach(self, section):
        start = self.run_hook("SessionStart", "claude-thread-deliver",
                              {"session_id": "READER", "cwd": self.tmp}, section)
        self.assertEqual(start.returncode, 0, start.stderr)
        self.assertIn(self.path, start.stdout)
        # Announcing marks nothing.
        self.assertIsNone(self.consumed_by())

        transcript = os.path.join(self.tmp, "transcript.jsonl")
        with open(transcript, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "message": {
                "role": "user",
                "content": "Continue from " + self.path}}) + "\n")
        prompt = self.run_hook("UserPromptSubmit", "claude-thread-watch",
                               {"session_id": "READER", "cwd": self.tmp,
                                "transcript_path": transcript}, section)
        self.assertEqual(prompt.returncode, 0, prompt.stderr)
        self.assertEqual(self.consumed_by(), "READER")

    def test_announce_then_claim(self):
        self.deliver_then_attach({})

    def test_infinite_numbers_in_config_do_not_silence_either_hook(self):
        # int(float("inf")) raises OverflowError; both modules read these
        # keys at import, so before get_int caught it each hook exited 0
        # having done nothing.
        self.deliver_then_attach(dict(self.INFINITE))

    def test_disabled_silences_both_hooks(self):
        start = self.run_hook("SessionStart", "claude-thread-deliver",
                              {"session_id": "READER", "cwd": self.tmp},
                              {"enabled": False})
        self.assertEqual((start.returncode, start.stdout), (0, ""))
        transcript = os.path.join(self.tmp, "transcript.jsonl")
        with open(transcript, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": self.path}}) + "\n")
        prompt = self.run_hook("UserPromptSubmit", "claude-thread-watch",
                               {"session_id": "READER", "cwd": self.tmp,
                                "transcript_path": transcript},
                               {"enabled": False})
        self.assertEqual((prompt.returncode, prompt.stdout), (0, ""))
        self.assertIsNone(self.consumed_by())

    def test_malformed_payloads_exit_zero(self):
        for event, executable in (("SessionStart", "claude-thread-deliver"),
                                  ("UserPromptSubmit", "claude-thread-watch")):
            for payload in ("not json", "[]", ""):
                config = os.path.join(self.tmp, "config.json")
                with open(config, "w", encoding="utf-8") as handle:
                    json.dump({}, handle)
                proc = subprocess.run(
                    ["/bin/sh", "-c", _plugin_command(event, executable)],
                    input=payload, capture_output=True, text=True, timeout=30,
                    env={"PATH": "/usr/bin:/bin", "HOME": self.tmp,
                         "CLAUDE_TOOLKIT_CONFIG": config,
                         "CLAUDE_PLUGIN_ROOT": PLUGIN_ROOT})
                self.assertEqual(proc.returncode, 0, (executable, payload))
                self.assertEqual(proc.stdout, "", (executable, payload))


if __name__ == "__main__":
    unittest.main()
