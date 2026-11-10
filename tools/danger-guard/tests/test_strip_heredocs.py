#!/usr/bin/env python3
"""guard_lib._strip_heredocs / _heredoc_skip_through: differential test
against the quadratic implementation it replaced, kept below verbatim as
the oracle.

TestMatchesTheReference generates multi-line commands and checks
`guard_lib._strip_heredocs(c) == _strip_heredocs_reference(c)` for every one,
while counting how often `_heredoc_skip_through` actually gets to skip
tokenizing an opener line. TestBraceDepthCarriesAcrossResume pins the hand
fixture from Plan Review that needs brace_depth to survive the resume point,
at three nesting depths, and checks the guard still blocks it end to end.
TestBackslashContinuationAcrossResume does the same for the shape Code
Review found the long generator blind to, plus a short-line differential.
"""

import os
import random
import sys
import unittest

_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, _DIR)
import guard_lib  # noqa: E402  (needs sys.path set up first)

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import test_rule_linearity  # noqa: E402  (reuses _build_context)


def _strip_heredocs_reference(raw_cmd):
    """VERBATIM copy of `_strip_heredocs` as it stood before the resumable
    scan replaced its quadratic re-tokenizing. Never imported by the guard; this file's
    test oracle only. The only edits from the original are qualifying its
    four callees through `guard_lib.`."""
    src_lines = raw_cmd.split("\n")
    n = len(src_lines)
    kept = []
    delims = []
    dashed = []
    handled = 0
    li = 0
    while li < n:
        line = src_lines[li]
        kept.append(line)

        if guard_lib._line_opens_heredoc(line):
            prefix = "\n".join(kept)
            ptoks = guard_lib.tokenize(prefix)
            delims = []
            dashed = []
            expect = False
            expect_dash = False
            for tok in ptoks:
                if expect:
                    delims.append(guard_lib.unquote(tok))
                    dashed.append(expect_dash)
                    expect = False
                elif tok == "<<":
                    expect = True
                    expect_dash = False
                elif tok == "<<-":
                    expect = True
                    expect_dash = True
            # `<<<` is a here-string and never reaches this list: it is its
            # own token, so it never sets `expect`. Several openers on one
            # line are consumed in order, which is the order the shell reads
            # their bodies in.
            while handled < len(delims):
                delim = delims[handled]
                is_dashed = dashed[handled]
                handled += 1
                li += 1
                while li < n:
                    # ONLY `<<-` allows an indented terminator. Stripping
                    # indentation for a plain `<<` ends the body early, and
                    # the rest of the body is then scanned as if it were
                    # shell.
                    if is_dashed:
                        candidate = src_lines[li].lstrip(guard_lib._HEREDOC_STRIP_CHARS)
                    else:
                        candidate = src_lines[li]
                    if candidate == delim:
                        break
                    li += 1
                # An unterminated heredoc runs off the end and the rest is
                # dropped -- what the shell does with it too.
        li += 1
    return kept


# --- the generator -----------------------------------------------------------
#
# AC2.1's fragment list, split into three pools so a "line" reads like a real
# shell line rather than fragment soup throughout: heredoc openers (every
# shape named in the plan, including two openers on one line), terminator
# lines (some of which never actually match a delimiter, to exercise the
# "never closes" floor), and the AC1.2 quote/substitution alphabet plus the
# brace-group shapes this task's resume point has to carry correctly.
#
# Per the plan's notation, `\` inside a backtick span is ONE backslash
# character -- so the two concatenations below build fragments containing
# two and one literal backslash respectively, not an escape sequence.
_OPENERS = [
    "cat <<EOF", "<<-EOF", "<<'EOF'", '<<"E"',
    "<<E" + "\\" + "\\" + "OF", "<<<x", "1 << 3",
    "cat <<EOF <<EOF2",  # two openers on one line
]
_TERMS = ["EOF", "\\" + "tEOF", " EOF", "E"]
_SOUP = [
    '"', "'", "`", "$(", "${", "$'", "<(", "((", ")", "}", "{", "()",
    "\\", "#", ";", "&&", "|", "<<", "<<-", "2>", " ", "\t", "x", "EOF",
    "{ ", "} {#<<E",
]

_LINE_KINDS = (
    [("opener", frag) for frag in _OPENERS] * 6
    + [("term", frag) for frag in _TERMS] * 5
    + [("soup", None)] * 4
)

_SEED = 20261002
_CASE_COUNT = 60000


def _random_line(rng):
    kind, frag = rng.choice(_LINE_KINDS)
    if kind == "soup":
        k = rng.randint(1, 4)
        return "".join(rng.choice(_SOUP) for _ in range(k))
    extra = ""
    if rng.random() < 0.3:
        extra = "".join(rng.choice(_SOUP) for _ in range(rng.randint(1, 2)))
    return frag + extra


def _generate_case(rng):
    n_lines = rng.randint(1, 16)
    return "\n".join(_random_line(rng) for _ in range(n_lines))


def _generate_corpus(count, seed):
    rng = random.Random(seed)
    return [_generate_case(rng) for _ in range(count)]


class TestMatchesTheReference(unittest.TestCase):

    def test_strip_heredocs_matches_the_reference_and_floors_hold(self):
        corpus = _generate_corpus(_CASE_COUNT, _SEED)

        # Count calls by wrapping the module global -- the guard must call
        # `_heredoc_skip_through` through it, or this sees nothing (AC2.2).
        original = guard_lib._heredoc_skip_through
        calls = []

        def _counting_wrapper(head, brace_depth, expect_command, src_lines, j0):
            result = original(head, brace_depth, expect_command, src_lines, j0)
            calls.append((src_lines, j0, result))
            return result

        guard_lib._heredoc_skip_through = _counting_wrapper
        try:
            cuts = 0
            for c in corpus:
                expected = _strip_heredocs_reference(c)
                with self.subTest(c=c):
                    actual = guard_lib._strip_heredocs(c)
                    self.assertEqual(actual, expected)
                if len(expected) < len(c.split("\n")):
                    cuts += 1
        finally:
            guard_lib._heredoc_skip_through = original

        returned_ge_j0 = 0
        opener_actually_skipped = 0
        never_closes = 0
        for src_lines, j0, result in calls:
            if result >= j0:
                returned_ge_j0 += 1
                if any(guard_lib._line_opens_heredoc(src_lines[k])
                       for k in range(j0, result + 1)):
                    opener_actually_skipped += 1
            if result == len(src_lines) - 1 and j0 <= len(src_lines) - 1:
                never_closes += 1

        print("\nTestMatchesTheReference floors (%d cases, %d _heredoc_skip_through calls):"
              % (len(corpus), len(calls)))
        print("  cut happened (len(kept) < len(src_lines)):  %d" % cuts)
        print("  skip_through returned >= j0:                %d" % returned_ge_j0)
        print("  an opener line in j0..returned was skipped: %d" % opener_actually_skipped)
        print("  returned n - 1 (never closes), j0 <= n - 1: %d" % never_closes)

        self.assertGreaterEqual(cuts, 5000)
        self.assertGreaterEqual(returned_ge_j0, 2000)
        self.assertGreaterEqual(opener_actually_skipped, 500)
        self.assertGreaterEqual(never_closes, 200)


def _brace_fixture(depth):
    # depth=1 reproduces the Plan Review fixture exactly:
    # "{ cat <<EOF\nbody\nEOF\n} {#<<E\nrm -rf ~/Documents\nE". Depths 2 and 3
    # change only the group nesting -- an extra `{ ... }` layer each time --
    # to pin that brace_depth survives the resume point at more than one
    # level, not just 0/1.
    return (("{ " * depth) + "cat <<EOF\nbody\nEOF\n"
            + ("} " * depth) + "{#<<E\nrm -rf ~/Documents\nE")


class TestBraceDepthCarriesAcrossResume(unittest.TestCase):

    def test_brace_depth_carries_across_resume(self):
        for depth in (1, 2, 3):
            cmd = _brace_fixture(depth)
            with self.subTest(depth=depth):
                expected = _strip_heredocs_reference(cmd)
                self.assertEqual(guard_lib._strip_heredocs(cmd), expected)
                self.assertIn("rm -rf ~/Documents", expected)

                ctx = test_rule_linearity._build_context(
                    "/tmp/claude-danger-guard-nonexistent-config.json")
                payload = {"tool_name": "Bash", "tool_input": {"command": cmd},
                          "cwd": "/tmp"}
                verdict = guard_lib.evaluate(payload, ctx)
                self.assertTrue(verdict.blocked, "expected a blocked verdict, got %r"
                                 % (verdict,))


# A review found the long-line generator above blind to one shape: a
# resume point whose open word ends in a backslash, which the next kept line
# turns into a line continuation. Snapshotting the delimiter scanner one
# token late (`k == r.resume_token_count + 1`) passed every test above and
# every floor, yet dropped the trailing `rm` from this command. Short lines
# over a small alphabet reach the shape in a few hundred cases; the long
# generator never did.
_CONTINUATION_COMMAND = "\n".join(
    ["<<\\", "\\", "", "<<;", "rm -rf ~/Documents"])
_SHORT_ALPHABET = ("<<", "<<-", "\\", ";", " ", "E", '"', "'", "x", "{", "}",
                   "#", "$(", ")", "`", "<<E", "2>")
_SHORT_SEED = 7
_SHORT_COUNT = 50000


class TestBackslashContinuationAcrossResume(unittest.TestCase):

    def test_the_code_review_command_keeps_its_rm_and_blocks(self):
        expected = _strip_heredocs_reference(_CONTINUATION_COMMAND)
        self.assertIn("rm -rf ~/Documents", expected)
        self.assertEqual(guard_lib._strip_heredocs(_CONTINUATION_COMMAND), expected)
        ctx = test_rule_linearity._build_context(
            "/tmp/claude-danger-guard-nonexistent-config.json")
        payload = {"tool_name": "Bash",
                   "tool_input": {"command": _CONTINUATION_COMMAND}, "cwd": "/tmp"}
        verdict = guard_lib.evaluate(payload, ctx)
        self.assertTrue(verdict.blocked, "expected a blocked verdict, got %r" % (verdict,))

    def test_short_lines_match_the_reference(self):
        rng = random.Random(_SHORT_SEED)
        for _ in range(_SHORT_COUNT):
            lines = ["".join(rng.choice(_SHORT_ALPHABET)
                             for _ in range(rng.randint(0, 3)))
                     for _ in range(rng.randint(2, 6))]
            cmd = "\n".join(lines)
            expected = _strip_heredocs_reference(cmd)
            if guard_lib._strip_heredocs(cmd) != expected:
                self.assertEqual(guard_lib._strip_heredocs(cmd), expected,
                                 "command %r" % cmd)


if __name__ == "__main__":
    unittest.main(verbosity=1)
