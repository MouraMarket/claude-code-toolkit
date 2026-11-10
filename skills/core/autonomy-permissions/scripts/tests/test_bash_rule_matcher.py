#!/usr/bin/env python3
"""Bash rule matching follows Claude Code 2.1.292, not fnmatch.

Every ORACLE row is a native output: the extracted `HAe`/`Lpo`/`Cit`/`Z7`
functions were executed under node on 2026-10-09. Do not "correct" a row to
make the port pass -- the port is what is wrong if one fails.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from permission_rules import bash_rule_matches, parse_bash_rule  # noqa: E402
from replay_permissions import _matches_any, would_prompt  # noqa: E402

# (rule, command, bucket, type, match)
ORACLE = [
    ("git *", "git", "allow", "wildcard", True),
    ("git *", "git status", "allow", "wildcard", True),
    ("git *", "gitx", "allow", "wildcard", False),
    ("git status", "git status", "allow", "exact", True),
    ("git status", "git status -s", "allow", "exact", False),
    ("ls ?", "ls ?", "allow", "exact", True),
    ("ls ?", "ls a", "allow", "exact", False),
    ("ls [ab]", "ls a", "allow", "exact", False),
    ("ls [ab]", "ls [ab]", "allow", "exact", True),
    ("ls [ab] *", "ls [ab] x", "allow", "wildcard", True),
    ("ls ? *", "ls a x", "allow", "wildcard", False),
    ("git:*", "git", "allow", "prefix", True),
    ("git:*", "git status", "allow", "prefix", True),
    ("git:*", "gitx", "allow", "prefix", False),
    ("git commit:*", "git commit -m x", "allow", "prefix", True),
    ("git commit:*", "xargs git commit -m x", "allow", "prefix", True),
    ("rm \\*", "rm *", "allow", "exact", False),
    ("rm \\*", "rm x", "allow", "exact", False),
    ("rm \\* *", "rm * x", "allow", "wildcard", True),
    ("git * --dry-run", "git push --dry-run", "allow", "wildcard", True),
    ("git * --dry-run", "git push", "allow", "wildcard", False),
    ("*", "anything at all", "allow", "wildcard", True),
    ("*", "", "allow", "wildcard", True),
    ("npm run *", "xargs npm run build", "allow", "wildcard", True),
    ("npm run * --x", "xargs npm run a --x", "allow", "wildcard", False),
    ("npm run * --x", "xargs npm run a --x", "ask", "wildcard", True),
    ("npm run * --x", "npm run a --x", "allow", "wildcard", True),
    ("git  status *", "git status -s", "allow", "wildcard", True),
    ("Git *", "git status", "allow", "wildcard", False),
    ("a.b *", "axb c", "allow", "wildcard", False),
    ("echo $HOME *", "echo $HOME x", "allow", "wildcard", True),
    ("cat /x/**/y", "cat /x/y", "allow", "wildcard", True),
    ("cat /x/**/y", "cat /x/a/b/y", "allow", "wildcard", True),
    ("git *", "git status\nrm -rf x", "allow", "wildcard", True),
    ("git push *", "git push origin main", "ask", "wildcard", True),
    ("find . -delete", "find . -delete", "allow", "exact", True),
    ("find:*", "find . -delete", "allow", "prefix", True),
]


class TestOracle(unittest.TestCase):
    def test_bash_rule_matches(self):
        for rule, command, bucket, _type, expected in ORACLE:
            with self.subTest(rule=rule, command=command, bucket=bucket):
                self.assertIs(bash_rule_matches(rule, command, bucket), expected)

    def test_parse_bash_rule_type(self):
        for rule, _command, _bucket, rule_type, _expected in ORACLE:
            with self.subTest(rule=rule):
                self.assertEqual(parse_bash_rule(rule)[0], rule_type)

    def test_parse_bash_rule_payload(self):
        self.assertEqual(parse_bash_rule("git commit:*"), ("prefix", "git commit"))
        self.assertEqual(parse_bash_rule("git *"), ("wildcard", "git *"))
        self.assertEqual(parse_bash_rule("ls ?"), ("exact", "ls ?"))


class TestReplayUsesNativeMatcher(unittest.TestCase):
    # Heads outside the built-in free set: a free head never prompts.
    def test_question_mark_is_literal(self):
        self.assertEqual(would_prompt("python3 a", ["python3 ?"]), "python3")
        self.assertIsNone(would_prompt("python3 ?", ["python3 ?"]))

    def test_trailing_star_covers_bare_head(self):
        self.assertIsNone(would_prompt("npm", ["npm *"]))

    def test_colon_star_is_prefix(self):
        self.assertIsNone(would_prompt("npm test", ["npm:*"]))

    def test_matches_any_bucket(self):
        seg = "xargs npm run a --x"
        self.assertTrue(_matches_any(seg, ["npm run * --x"], "ask"))
        self.assertFalse(_matches_any(seg, ["npm run * --x"], "allow"))


if __name__ == "__main__":
    unittest.main()
