#!/usr/bin/env python3
"""Tests for claude_home.py -- the shared `$CLAUDE_CONFIG_DIR` resolver.

Every test that touches `CLAUDE_CONFIG_DIR` goes through `setUp`/`tearDown`,
which save whatever was ambient and restore it -- hermetic, never inheriting
whatever happens to be set in the running shell.
"""

import os
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
LIB_DIR = os.path.dirname(HERE)

sys.path.insert(0, LIB_DIR)
import claude_home  # noqa: E402


class ClaudeHomeTestCase(unittest.TestCase):
    def setUp(self):
        self._previous = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._previous

    # -- AC1.1: env unset/set/empty/tilde, with and without `home` ---------

    def test_env_unset_expands_dotclaude(self):
        self.assertEqual(claude_home.claude_home(),
                          os.path.expanduser("~/.claude"))

    def test_env_set_absolute_path_wins(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "/x/cfg"
        self.assertEqual(claude_home.claude_home(), "/x/cfg")

    def test_env_empty_string_counts_as_unset(self):
        os.environ["CLAUDE_CONFIG_DIR"] = ""
        self.assertEqual(claude_home.claude_home(),
                          os.path.expanduser("~/.claude"))

    def test_env_set_tilde_path_expands(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "~/cfg"
        self.assertEqual(claude_home.claude_home(),
                          os.path.expanduser("~/cfg"))

    def test_home_param_used_when_env_unset(self):
        self.assertEqual(claude_home.claude_home(home="/h"),
                          os.path.join("/h", ".claude"))

    def test_env_wins_over_home_param(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "/x/cfg"
        self.assertEqual(claude_home.claude_home(home="/h"), "/x/cfg")

    # -- AC1.2: read at call time, not cached -------------------------------

    def test_read_at_call_time_two_different_answers(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "/first/cfg"
        first = claude_home.claude_home()
        os.environ["CLAUDE_CONFIG_DIR"] = "/second/cfg"
        second = claude_home.claude_home()

        self.assertEqual(first, "/first/cfg")
        self.assertEqual(second, "/second/cfg")
        self.assertNotEqual(first, second)

    # -- AC1.3: user_rules_dir() -------------------------------------------

    def test_user_rules_dir_env_unset(self):
        expected = os.path.join(os.path.expanduser("~/.claude"), "rules")
        self.assertEqual(claude_home.user_rules_dir(), expected)

    def test_user_rules_dir_env_set(self):
        os.environ["CLAUDE_CONFIG_DIR"] = "/x/cfg"
        self.assertEqual(claude_home.user_rules_dir(),
                          os.path.join("/x/cfg", "rules"))

    def test_user_rules_dir_with_home_param(self):
        self.assertEqual(claude_home.user_rules_dir(home="/h"),
                          os.path.join("/h", ".claude", "rules"))


if __name__ == "__main__":
    unittest.main()
