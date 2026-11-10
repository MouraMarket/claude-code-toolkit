#!/usr/bin/env python3
"""`guard_lib.brace_expand`: bash brace expansion over a RAW token, pure.

The raw token still carries its quotes, so quoting decides what is a brace
group: a `{`, `,` or `}` inside quotes or after a backslash is literal, and
`${...}` is parameter expansion, never a group.
"""

import os
import sys
import unittest

_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, _DIR)
import guard_lib  # noqa: E402  (needs sys.path set up first)

LIMIT = 64


class TestBraceExpand(unittest.TestCase):

    def expand(self, raw, limit=LIMIT):
        return guard_lib.brace_expand(raw, limit)

    def test_a_simple_group(self):
        self.assertEqual(self.expand("a{b,c}d"), ["abd", "acd"])

    def test_two_groups_expand_in_bash_order(self):
        self.assertEqual(self.expand("{a,b}{1,2}"), ["a1", "a2", "b1", "b2"])

    def test_nested_groups(self):
        self.assertEqual(self.expand("a{b,{c,d}}"), ["ab", "ac", "ad"])

    def test_numeric_sequence(self):
        self.assertEqual(self.expand("{1..3}"), ["1", "2", "3"])

    def test_descending_sequence(self):
        self.assertEqual(self.expand("{3..1}"), ["3", "2", "1"])

    def test_sequence_with_a_step(self):
        self.assertEqual(self.expand("{1..10..3}"), ["1", "4", "7", "10"])

    def test_letter_sequence(self):
        self.assertEqual(self.expand("{a..c}"), ["a", "b", "c"])

    def test_not_a_group_is_unchanged(self):
        for raw in ("{a}", "{}", "a{b"):
            with self.subTest(raw=raw):
                self.assertEqual(self.expand(raw), [raw])

    def test_a_plain_token_is_unchanged(self):
        self.assertEqual(self.expand("build/x"), ["build/x"])

    def test_quoted_or_escaped_braces_are_literal(self):
        for raw in ("'{a,b}'", '"{a,b}"', "\\{a,b}", "$'{a,b}'"):
            with self.subTest(raw=raw):
                self.assertEqual(self.expand(raw), [raw])

    def test_parameter_expansion_is_skipped_whole(self):
        self.assertEqual(self.expand("${HOME}/{a,b}"),
                         ["${HOME}/a", "${HOME}/b"])

    def test_a_word_count_over_the_limit_is_refused(self):
        self.assertIsNone(self.expand("{1..100}"))
        self.assertEqual(len(self.expand("{1..64}")), 64)
        self.assertIsNone(self.expand("{1..65}"))

    def test_a_product_over_the_limit_is_refused(self):
        self.assertIsNone(self.expand("{a,b}" * 7))
        self.assertEqual(len(self.expand("{a,b}" * 6)), 64)

    def test_a_huge_range_is_refused_without_being_built(self):
        self.assertIsNone(self.expand("{1..99999999999}"))

    def test_too_many_openers_is_refused_before_scanning(self):
        # 32 unmatched `{` are still scanned; 33 are refused outright.
        self.assertEqual(self.expand("x/" + "{" * 32), ["x/" + "{" * 32])
        self.assertIsNone(self.expand("x/" + "{" * 33))

    def test_a_long_token_with_a_brace_is_refused_before_scanning(self):
        at_cap = "a" * (4096 - len("{x,y}")) + "{x,y}"
        self.assertEqual(len(self.expand(at_cap)), 2)
        self.assertIsNone(self.expand("a" + at_cap))
        # The length cap applies only when there is a `{` to scan for.
        long_plain = "a" * 10000
        self.assertEqual(self.expand(long_plain), [long_plain])


if __name__ == "__main__":
    unittest.main()
