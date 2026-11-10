#!/usr/bin/env python3
"""guard_lib._scan: tokenize's loop, generalised with a resume point and an
early stop.

TestResumeProperty checks the resume property -- splitting a scan at any point and resuming
from the state `_scan` hands back reproduces exactly the tokens a single
scan of the whole string would produce. TestStopAfterFirst checks that
`stop_after_first` stops exactly when the first token is known, never
changes what that token is, and never misses a case where a second token
exists.
"""

import os
import random
import sys
import unittest

_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, _DIR)
import guard_lib  # noqa: E402  (needs sys.path set up first)

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import test_tokenizer  # noqa: E402  (reuses its ast-based fixture extraction)

_SEED = 20261002
_RANDOM_COUNT = 20000

# Fragments the generator draws from -- every shape AC1.2 requires, plus the
# brace-group shapes that specifically carry brace_depth/expect_command
# across a resume point.
_FRAGMENTS = [
    '"', "'", "`", "$(", "${", "$'", "<(", "((", ")", "}", "{", "()",
    "\\", "#", ";", "&&", "|", "<<", "<<-", "2>", " ", "\t", "x", "EOF", "\n",
    "{ ", "\n} ", "} {",
]

# The extension list X from AC1.2, as Python literals.
X_VALUES = [
    "", "x", '"', "'", ")", "}", "\\", "<<EOF", "a b; c", "`", "((", "$(",
    "} {a", "} {#<<E",
]
# The two X values AC1.2 names as the ones that observe a carried
# brace_depth: a standalone "}" closes a group only when one is open, which
# changes expect_command for the "{" that follows.
_BRACE_X_VALUES = ("} {a", "} {#<<E")


def _generate_random_corpus(count, seed):
    rng = random.Random(seed)
    strings = []
    for _ in range(count):
        n = rng.randint(1, 12)
        strings.append("".join(rng.choice(_FRAGMENTS) for _ in range(n)))
    return strings


def _build_corpus():
    items = list(test_tokenizer.extract_test_guard_fixtures())
    items.extend(test_tokenizer.HAND_FIXTURES)
    items.extend(_generate_random_corpus(_RANDOM_COUNT, _SEED))
    return items


# Built once per process: both test classes below draw on the same corpus,
# and it is deterministic (seeded), so sharing it costs nothing but compute.
_CORPUS = _build_corpus()


class TestResumeProperty(unittest.TestCase):
    """The resume property: for `r = _scan(P)` and any `X`, `tokenize(P + "\\n" + X)` equals
    `r.tokens[:r.resume_token_count] + _scan(P[r.resume_offset:] + "\\n" + X,
    r.resume_brace_depth, r.resume_expect_command).tokens` (modulo
    `tokenize`'s `[""]` fallback for an empty result)."""

    def test_resume_property_holds_for_every_p_and_x(self):
        word_in_progress = 0
        at_end = 0
        brace_depth_gt_zero = 0
        brace_depth_distinguishes = 0
        trailing_backslash = 0
        continuation = 0

        for p in _CORPUS:
            r = guard_lib._scan(p)

            if r.resume_offset < len(p):
                word_in_progress += 1
            else:
                at_end += 1
            if r.resume_brace_depth > 0:
                brace_depth_gt_zero += 1
            # An unescaped trailing single backslash: the LAST char is "\"
            # and it is not itself escaped by a preceding "\".
            if p and p[-1] == "\\" and not (len(p) >= 2 and p[-2] == "\\"):
                trailing_backslash += 1
            if "\\\n" in p:
                continuation += 1

            distinguished = False
            for x in X_VALUES:
                tail = guard_lib._scan(
                    p[r.resume_offset:] + "\n" + x,
                    r.resume_brace_depth, r.resume_expect_command).tokens
                actual = list(r.tokens[:r.resume_token_count]) + tail
                if not actual:
                    actual = [""]
                expected = guard_lib.tokenize(p + "\n" + x)
                with self.subTest(p=p, x=x):
                    self.assertEqual(actual, expected)

                if x in _BRACE_X_VALUES:
                    tail_from_zero = guard_lib._scan(
                        p[r.resume_offset:] + "\n" + x, 0,
                        r.resume_expect_command).tokens
                    if tail_from_zero != tail:
                        distinguished = True
            if distinguished:
                brace_depth_distinguishes += 1

        print("\nTestResumeProperty floors (corpus size %d):" % len(_CORPUS))
        print("  resume_offset < len(P)  (word in progress): %d" % word_in_progress)
        print("  resume_offset == len(P) (at end):            %d" % at_end)
        print("  resume_brace_depth > 0:                      %d" % brace_depth_gt_zero)
        print("  a brace-X distinguishes carried depth:        %d" % brace_depth_distinguishes)
        print("  P ends in an unescaped single backslash:      %d" % trailing_backslash)
        print("  P contains a backslash-newline continuation:  %d" % continuation)

        self.assertGreaterEqual(word_in_progress, 1000)
        self.assertGreaterEqual(at_end, 1000)
        self.assertGreaterEqual(brace_depth_gt_zero, 500)
        self.assertGreaterEqual(brace_depth_distinguishes, 200)
        self.assertGreaterEqual(trailing_backslash, 500)
        self.assertGreaterEqual(continuation, 100)


class TestStopAfterFirst(unittest.TestCase):
    """For `r = _scan(T, stop_after_first=True)`: if `r.stop_offset` is
    set, it names a valid prefix of `T` that reproduces the first token;
    and a full scan that produces a second token proves `stop_offset` must
    have been set."""

    def test_stop_after_first_matches_tokenize_and_counts_floors(self):
        set_count = 0
        none_count = 0

        for t in _CORPUS:
            full = guard_lib._scan(t)
            stopped = guard_lib._scan(t, stop_after_first=True)
            with self.subTest(t=t):
                if stopped.stop_offset is not None:
                    set_count += 1
                    self.assertLessEqual(stopped.stop_offset, len(t))
                    self.assertEqual(guard_lib.tokenize(t)[0], stopped.tokens[0])
                else:
                    none_count += 1
                if len(full.tokens) >= 2:
                    self.assertIsNotNone(stopped.stop_offset)

        print("\nTestStopAfterFirst floors (corpus size %d):" % len(_CORPUS))
        print("  stop_offset set:  %d" % set_count)
        print("  stop_offset None: %d" % none_count)

        self.assertGreaterEqual(set_count, 1000)
        self.assertGreaterEqual(none_count, 200)


if __name__ == "__main__":
    print("%d corpus entries" % len(_CORPUS))
    unittest.main(verbosity=1)
