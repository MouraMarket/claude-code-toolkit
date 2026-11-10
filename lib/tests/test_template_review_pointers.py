#!/usr/bin/env python3
"""Tests pinning the toolkit policy's review-mechanics pointers to what is
actually installed.

The contract under test is "the policy only points at things that exist and
work" -- a stale hand-copied dispatch or a dangling `build-lens` path reads
fine and silently teaches every new user review steps the gates no longer take.
"""

import os
import re
import subprocess
import sys
import unittest

REPO = os.path.realpath(os.path.join(os.path.dirname(os.path.realpath(__file__)), "..", ".."))

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))


class TestPolicyReviewPointers(unittest.TestCase):

    def _policy_text(self):
        with open(os.path.join(REPO, "policy", "CLAUDE.md")) as handle:
            return handle.read()

    def test_every_build_lens_path_the_policy_names_is_installed(self):
        """The policy points at the merge-review steps inside the toolkit
        checkout (they ship in the plugin, not under `~/.claude`), and that
        file must exist."""
        template_text = self._policy_text()
        self.assertIn("tools/build-lens/merge-review.md", template_text)
        self.assertTrue(
            os.path.isfile(os.path.join(REPO, "tools/build-lens/merge-review.md"))
        )
        self.assertNotIn("~/.claude/build-lens", template_text)

    def test_the_policy_names_the_review_steps_and_command(self):
        """The policy names both pointers."""
        text = self._policy_text()
        self.assertIn("tools/build-lens/merge-review.md", text)
        self.assertIn("claude-build-route --review-mode", text)
        self.assertNotIn("~/.claude/build-lens", text)

    def test_the_review_mode_command_the_policy_names_runs(self):
        """The `claude-build-route --review-mode` command the policy tells a
        reader to run must actually run and print `economical` on a fresh
        machine, since that is the promise the policy makes."""
        template_text = self._policy_text()
        self.assertIn("claude-build-route --review-mode", template_text)

        script = os.path.join(REPO, "tools/build-route/claude-build-route")
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        result = subprocess.run(
            [sys.executable, script, "--review-mode"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            cwd=REPO,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "economical")

    def test_the_policy_routes_no_gate_to_a_retired_reviewer(self):
        """The policy must not carry its own copy of the dispatch steps --
        a hand dispatch of the `code-reviewer` subagent, or `claude-merge-brief`.
        The economical gates still use both, but only through
        `blinded-review.js` and the steps in
        `tools/build-lens/merge-review.md`, which is where they live."""
        template_text = self._policy_text()
        for marker in ("code-reviewer", "claude-merge-brief"):
            self.assertNotIn(
                marker,
                template_text,
                "%s: the steps live in tools/build-lens/merge-review.md" % marker,
            )


if __name__ == "__main__":
    unittest.main()
