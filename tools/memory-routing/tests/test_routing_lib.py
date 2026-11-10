"""Tests for the memory-routing nudge.

The interesting property is PRECISION, not recall. A reminder that fires on
ordinary memory writes trains the reader to skip it, at which point it is worse
than nothing — so the silence cases below carry more weight than the firing ones.

Hermetic: no file is read, no config is loaded. The executable is exercised
through subprocess, matching every sibling tool.
"""

import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO, "lib"))
import routing_lib

HOOK = os.path.join(TOOL_DIR, "claude-memory-routing")
MEM = "/Users/x/.claude/projects/-slug/memory/some-fact.md"

CONFIG_FACT = """---
name: service-api-token
---
The token lives at op://My Vault/Some API Token/credential and is also a
GitHub Actions secret on example-repo.
"""

RULE = """---
name: api-over-mcp
---
Prefer a direct API over that service's MCP connector.

**Why:** the direct API gives more control and avoids MCP-tool quirks.

**How to apply:** before reaching for a connector, check whether a credential
already exists. This was a decision made deliberately.
"""

ORDINARY = """---
name: weekly-scheduling-cadence
---
Tue/Thu are deep-work days. Match task weight to the day.

**Why:** Mon/Wed are meeting-heavy.

**How to apply:** put light tasks on meeting days.
"""


class TestPathTargeting(unittest.TestCase):
    def test_memory_entry_is_in_scope(self):
        self.assertTrue(routing_lib.is_memory_write(MEM))

    def test_the_index_itself_is_out_of_scope(self):
        # MEMORY.md names what lives elsewhere and is SUPPOSED to mention
        # tooling. Nudging on it would fire on every edit.
        idx = "/Users/x/.claude/projects/-slug/memory/MEMORY.md"
        self.assertFalse(routing_lib.is_memory_write(idx))

    def test_unrelated_markdown_is_out_of_scope(self):
        self.assertFalse(routing_lib.is_memory_write("/Users/x/notes/memory.md"))
        self.assertFalse(routing_lib.is_memory_write("/Users/x/workspace/TOOLING.md"))

    def test_missing_path_is_safe(self):
        self.assertFalse(routing_lib.is_memory_write(None))
        self.assertFalse(routing_lib.is_memory_write(""))


class TestSignalDetection(unittest.TestCase):
    def test_op_reference_is_a_signal(self):
        self.assertTrue(routing_lib.matched_signals("see op://My Vault/Thing/credential"))

    def test_venv_and_missing_cli_are_signals(self):
        self.assertTrue(routing_lib.matched_signals("the .venv here lacks googleapiclient"))
        self.assertTrue(routing_lib.matched_signals("gcloud is not installed"))

    def test_op_reference_survives_spaces_in_the_vault_name(self):
        # Regression: `op://\S+/\S+` could not match `op://My Vault/...` because
        # the space ended the first segment — so it missed exactly the
        # references most worth catching. 1Password vault names have spaces.
        self.assertTrue(routing_lib.matched_signals(
            "token at op://My Vault/Some API Token/credential"))

    def test_a_bare_op_scheme_mention_is_not_a_reference(self):
        # "local runs read the op:// ref" is a POINTER at TOOLING.md, which is
        # the behaviour we want — nudging on it would punish compliance.
        self.assertEqual(routing_lib.matched_signals(
            "local runs read the `op://` ref"), [])

    def test_prose_with_no_config_artifact_is_silent(self):
        self.assertEqual(routing_lib.matched_signals("Tue and Thu are deep-work days."), [])

    def test_empty_text_is_safe(self):
        self.assertEqual(routing_lib.matched_signals(""), [])
        self.assertEqual(routing_lib.matched_signals(None), [])


class TestRuleCancellation(unittest.TestCase):
    def test_two_rule_markers_cancel(self):
        self.assertTrue(routing_lib.looks_like_a_rule(RULE))

    def test_a_single_why_does_not_cancel(self):
        # Nearly every well-formed memory has one "Why:", including ones that
        # are purely config facts. One marker must not be enough.
        text = "The token is at op://My Vault/X/credential.\n\n**Why:** we need it."
        self.assertFalse(routing_lib.looks_like_a_rule(text))


class TestBuild(unittest.TestCase):
    """End to end, which is what the hook calls."""

    def test_config_fact_in_memory_fires(self):
        out = routing_lib.build({"file_path": MEM, "content": CONFIG_FACT})
        self.assertIn("TOOLING.md", out)

    def test_a_rule_that_names_tools_stays_silent(self):
        # api-over-mcp is exactly the memory that SHOULD stay in memory.
        self.assertEqual(routing_lib.build({"file_path": MEM, "content": RULE}), "")

    def test_ordinary_memory_stays_silent(self):
        self.assertEqual(routing_lib.build({"file_path": MEM, "content": ORDINARY}), "")

    def test_same_content_outside_memory_stays_silent(self):
        out = routing_lib.build({"file_path": "/Users/x/workspace/TOOLING.md",
                                 "content": CONFIG_FACT})
        self.assertEqual(out, "")

    def test_edit_payload_shape_is_read(self):
        out = routing_lib.build({"file_path": MEM, "new_string": CONFIG_FACT})
        self.assertIn("TOOLING.md", out)

    def test_garbage_payload_is_safe(self):
        self.assertEqual(routing_lib.build(None), "")
        self.assertEqual(routing_lib.build({}), "")
        self.assertEqual(routing_lib.build({"file_path": MEM}), "")


class TestHookExecutable(unittest.TestCase):
    """Through subprocess, the way Claude Code actually runs it."""

    def run_hook(self, payload, config="/nonexistent-config.json"):
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = config
        return subprocess.run(
            [sys.executable, HOOK], input=payload, env=env,
            capture_output=True, text=True, timeout=20,
        )

    def test_emits_valid_hook_json_on_a_config_fact(self):
        payload = json.dumps({
            "hook_event_name": "PostToolUse", "tool_name": "Write",
            "tool_input": {"file_path": MEM, "content": CONFIG_FACT},
        })
        result = self.run_hook(payload)
        self.assertEqual(result.returncode, 0)
        parsed = json.loads(result.stdout)
        self.assertEqual(
            parsed["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn("TOOLING.md",
                      parsed["hookSpecificOutput"]["additionalContext"])

    def test_silent_on_an_ordinary_memory(self):
        payload = json.dumps({
            "hook_event_name": "PostToolUse", "tool_name": "Write",
            "tool_input": {"file_path": MEM, "content": ORDINARY},
        })
        result = self.run_hook(payload)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_exits_zero_on_garbage_stdin(self):
        result = self.run_hook("not json at all")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_exits_zero_on_empty_stdin(self):
        result = self.run_hook("")
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
