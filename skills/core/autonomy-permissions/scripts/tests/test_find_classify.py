#!/usr/bin/env python3
"""`find` is judged by its ARGUMENTS, not by its head.

`find . -delete` and `find . -exec rm {} +` mutate or run code, and no prefix
rule covers them, so calling them "already free" was a silent wrong answer.
Every table entry is split with str.split() -- never shlex -- because that is
the token shape both callers (classify and would_prompt) actually produce.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from scan_transcripts import ALWAYS_FREE, classify, find_is_free, subcommands  # noqa: E402
from replay_permissions import would_prompt  # noqa: E402
from test_scripts_end_to_end import ScriptCase, tool_use  # noqa: E402

FREE = [
    "", ".", ". -name '*.py'", '. -name "*.md" -type f',
    ". -type f -maxdepth 2", ". -mtime -7", ". -perm -644",
    ". -size -1M -print", ". -print0", ". -ls", ". -printf %p",
    "-L . -name x", ". -newermt 2026-01-01",
    ". -path ./x -prune -o -print", ". ! -name x",
    ". \\( -name a -o -name b \\)", ". -quit",
    # a quoted space splits across tokens
    '. -name "my file.txt"',
]

NOT_FREE = [
    ". -delete", ". -exec rm {} +", ". -exec rm {} \\;",
    ". -execdir x {} +", ". -ok rm {} \\;", ". -okdir rm {} \\;",
    ". -fprint /tmp/x", ". -fprint0 /tmp/x", ". -fprintf /tmp/x %p",
    ". -fls /tmp/x", "-files0-from list", ". -rm", "-sf x .", "-xf x .",
    # declared divergence from native: every token is checked, no value-skip
    ". -name -delete",
    ". -type f -delete", ". '-delete'", '. "-exec" rm {} +',
    ". \\-delete", ". -type f -de''lete", '. -type f -de""lete',
    "$HOME -name x", "`pwd` -name x", '. -name "$X"',
    ". -name <(touch m)", ". -name *.py",
    ". -name x?", ". -name [ab]", ". -name 'unclosed",
    # A dangling escape is a line continuation the
    # segment splitter cut, and an unquoted paren is shell grouping.
    ". -de\\", ". -name x \\", ". -delete)", "( . -delete )",
]

NO_PREFIX = "find (no prefix rule covers it)"


class TestFindIsFree(unittest.TestCase):
    def test_free_inputs(self):
        for s in FREE:
            with self.subTest(args=s):
                self.assertIs(find_is_free(s.split()), True, s)

    def test_not_free_inputs(self):
        for s in NOT_FREE:
            with self.subTest(args=s):
                self.assertIs(find_is_free(s.split()), False, s)


class TestClassify(unittest.TestCase):
    def test_find_is_not_in_the_always_free_set(self):
        self.assertNotIn("find", ALWAYS_FREE)

    def test_not_free_find_is_no_prefix(self):
        for s in NOT_FREE:
            with self.subTest(args=s):
                self.assertEqual(classify("find " + s), ("find", "no-prefix"))

    def test_free_find_is_already_free(self):
        for s in FREE:
            with self.subTest(args=s):
                self.assertEqual(classify("find " + s), ("find", "already-free"))


class TestWouldPrompt(unittest.TestCase):
    def test_free_find_does_not_prompt(self):
        self.assertIsNone(would_prompt("find . -name x", []))

    def test_mutating_find_prompts(self):
        self.assertEqual(would_prompt("find . -delete", []), NO_PREFIX)

    def test_prefix_rules_do_not_cover_it(self):
        for patterns in (["find *"], ["find*"], ["find . *"], ["find . -de*"]):
            with self.subTest(patterns=patterns):
                self.assertEqual(would_prompt("find . -delete", patterns),
                                 NO_PREFIX)

    def test_a_tool_wide_grant_covers_it(self):
        # Bare `Bash` and `Bash(*)` both reach here as the pattern "*" and are
        # a blanket grant natively, not a prefix rule.
        self.assertIsNone(would_prompt("find . -delete", ["*"]))
        self.assertIsNone(would_prompt("find . -delete", ["git *", "*"]))

    def test_question_mark_and_bracket_are_literal_in_an_exact_rule(self):
        # Only `*` is a wildcard in a Bash rule.
        for cmd in ("find . -name '[ab]' -delete", "find . -name 'a?' -delete"):
            with self.subTest(cmd=cmd):
                self.assertIsNone(would_prompt(cmd, [cmd]))

    def test_a_rule_spelled_like_the_command_but_holding_a_wildcard_does_not(self):
        # Claude Code reads `*` in a rule as a wildcard, so this is a wildcard
        # rule even though its text equals the segment.
        cmd = "find . -name *.x -delete"
        self.assertEqual(would_prompt(cmd, [cmd]), NO_PREFIX)

    def test_an_exact_rule_covers_it(self):
        self.assertIsNone(would_prompt("find . -delete", ["find . -delete"]))
        self.assertIsNone(would_prompt("find   .   -delete", ["find . -delete"]))

    def test_a_prefix_rule_is_harmless_on_a_free_find(self):
        self.assertIsNone(would_prompt("find . -name x", ["find *"]))


class TestCommandShapes(unittest.TestCase):
    """Whole commands, through the same splitter the scripts use (Code Review)."""

    SHAPES = (
        "find . -de\\\nlete",      # backslash-newline joins into -delete
        "'find' . -delete",          # quoted head
        "(find . -delete)",          # grouped
        "\"find\" . -exec rm {} +",
    )

    def test_each_shape_is_no_prefix_in_scan_and_replay(self):
        for cmd in self.SHAPES:
            with self.subTest(cmd=cmd):
                seg = next(subcommands(cmd))
                self.assertEqual(classify(seg), ("find", "no-prefix"))
                self.assertEqual(would_prompt(seg, ["find *"]), NO_PREFIX)
                self.assertEqual(would_prompt(seg, []), NO_PREFIX)


class TestScannerEndToEnd(ScriptCase):
    def _transcript(self):
        self.write_transcript([
            tool_use("Bash", command="find . -type f -delete"),
            tool_use("Bash", command="find . -type f -delete"),
            tool_use("Bash", command="find . -name x"),
        ])
        self.write_settings()

    def test_json_buckets(self):
        self._transcript()
        r = self.run_script("scan_transcripts.py", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(dict(out["no_prefix_rule"])["find"], 2)
        self.assertEqual(dict(out["already_free"])["find"], 1)
        self.assertNotIn("find", dict(out["grantable"]))
        self.assertNotIn("find", dict(out["exec_wrappers"]))

    def test_text_report_headings(self):
        self._transcript()
        r = self.run_script("scan_transcripts.py")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(
            "PROMPTS REGARDLESS — no prefix rule covers these\n"
            "  find with -exec/-delete/-fprint* (or an unquoted glob): only an "
            "exact-match rule covers it.\n"
            "  A write through an output redirect ('<family> >'): only a "
            "blanket Bash grant covers it.", r.stdout)
        self.assertIn(
            "ALREADY FREE — built-in read-only set (git and find judged by "
            "their arguments; unsure counts as not free), needs no rule",
            r.stdout)


class TestReplayEndToEnd(ScriptCase):
    def test_a_prefix_rule_does_not_cover_a_mutating_find(self):
        self.write_transcript([
            tool_use("Bash", command="find . -type f -delete"),
        ])
        self.write_settings(allow=["Bash(find *)"])
        # exit 1 means "over the prompt-rate threshold"; lift it so the
        # return code only reports a crash
        r = self.run_script("replay_permissions.py", "--threshold", "100")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(NO_PREFIX, r.stdout)
        self.assertIn("Would prompt: 1/", r.stdout)


if __name__ == "__main__":
    unittest.main()
