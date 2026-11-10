#!/usr/bin/env python3
"""The git half of "already free" is judged by subcommand AND arguments.

`git branch -D x` and `git config user.name Bob` mutate, so a family-level
"read-only" verdict (the old family set) called them free. These tests
pin the argument-level verdicts; every failure prints the offending input.
"""

import json
import os
import shlex
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from scan_transcripts import classify, git_is_free, git_subcommand  # noqa: E402
from replay_permissions import would_prompt  # noqa: E402
from test_scripts_end_to_end import ScriptCase, tool_use  # noqa: E402

FREE = [
    "status", "log --oneline -5", "-C /x log",
    "diff HEAD~1", "branch", "branch -a -v", "branch -vv",
    "branch --list feat*", "branch -l feat*", "branch --contains abc123",
    "branch --merged main", "branch --show-current", "remote", "remote -v",
    "remote show origin", "remote get-url origin", "config user.name",
    "config --get user.name", "config --list", "config get user.name",
    "config --file x.cfg --get a.b", "config --file c.cfg -- demo.key",
]

NOT_FREE = [
    "branch -D x", "branch -d x", "branch -Dr x", "branch newbr",
    # List mode makes the positional legal, so only the short-option allowlist
    # can catch these -- without them, letting `D` through goes unnoticed.
    "branch -D -l x", "branch -d --list x",
    "branch -m a b", "branch -u origin/x", "branch --set-upstream-to=origin/x",
    "branch --track x origin/x", "-C /x branch -D y", "remote remove origin",
    "remote rm origin", "remote add o https://e/x", "remote set-url o u",
    "remote prune o", "remote update", "config user.name Bob",
    "config --unset user.name", "config --add a.b c", "config set user.name Bob",
    "config -e", "diff --output=/tmp/x", "log --output /tmp/x",
    "diff --outp=/tmp/x", "config --file c.cfg -- demo.key --get",
    "config --file c.cfg --remove-sect demo", "config --global",
    "commit -m x", "push", "",
]


class TestGitIsFree(unittest.TestCase):
    def test_free_inputs(self):
        for cmd in FREE:
            with self.subTest(cmd=cmd):
                self.assertTrue(git_is_free(shlex.split(cmd)), cmd)

    def test_not_free_inputs(self):
        for cmd in NOT_FREE:
            with self.subTest(cmd=cmd):
                self.assertFalse(git_is_free(shlex.split(cmd)), cmd)


class TestGitSubcommand(unittest.TestCase):
    def test_global_options_are_skipped(self):
        self.assertEqual(git_subcommand(["-C", "/x", "log", "-1"]), ("log", ["-1"]))
        self.assertEqual(git_subcommand(["-c", "a=b", "--no-pager", "status"]),
                         ("status", []))
        self.assertEqual(git_subcommand([]), ("", []))


class TestClassify(unittest.TestCase):
    def test_classify_is_judged_by_arguments(self):
        self.assertEqual(classify("git branch -D x"), ("git branch", "grantable"))
        self.assertEqual(classify("git branch"), ("git branch", "already-free"))
        self.assertEqual(classify("git -C /repo remote remove o"),
                         ("git remote", "grantable"))


class TestWouldPrompt(unittest.TestCase):
    def test_would_prompt_is_judged_by_arguments(self):
        self.assertEqual(would_prompt("git branch -D x", []), "git branch")
        self.assertEqual(would_prompt("git status", []), "")
        self.assertIsNone(would_prompt("git branch -D x", ["git branch *"]))
        self.assertEqual(
            would_prompt("git config --file c.cfg -- demo.key --get", []),
            "git config")
        self.assertEqual(
            would_prompt("git config --file c.cfg --remove-sect demo", []),
            "git config")


class TestCallerTokenShape(unittest.TestCase):
    """Both callers split on whitespace, not shlex, so quotes survive into the
    tokens. The tables above use shlex.split and never see that shape."""

    QUOTED = (
        # Config overrides can name a program git runs.
        ("git -c core.fsmonitor=prog status --short", "git status"),
        ("git -C /x -c core.pager=cat log", "git log"),
        ('git --config-env=core.pager=PAGER log', "git log"),
        ('git log "--output=/tmp/x" -1', "git log"),
        ("git branch '-D' x", "git branch"),
        ('git config "--remove-sect" demo', "git config"),
        ("git remote 'remove' o", "git remote"),
    )

    def test_quoted_mutating_forms_are_not_free(self):
        for cmd, family in self.QUOTED:
            with self.subTest(cmd=cmd):
                self.assertEqual(classify(cmd), (family, "grantable"))
                self.assertEqual(would_prompt(cmd, []), family)


class TestScannerEndToEnd(ScriptCase):
    def _transcript(self):
        self.write_transcript([
            tool_use("Bash", command="git branch -D old"),
            tool_use("Bash", command="git branch -D old"),
            tool_use("Bash", command="git status"),
        ])
        self.write_settings()

    def test_a_destructive_branch_is_not_listed_as_already_free(self):
        self._transcript()
        r = self.run_script("scan_transcripts.py", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        free = dict(json.loads(r.stdout)["already_free"])
        self.assertIn("git status", free)
        self.assertNotIn("git branch", free)

    def test_the_text_report_carries_the_new_heading(self):
        self._transcript()
        r = self.run_script("scan_transcripts.py")
        self.assertIn(
            "ALREADY FREE — built-in read-only set (git and find judged by "
            "their arguments; unsure counts as not free), needs no rule",
            r.stdout)


if __name__ == "__main__":
    unittest.main()
