#!/usr/bin/env python3
"""Wrappers match Claude Code's documented list, not a hand-made one.

Source: https://code.claude.com/docs/en/permissions, "Wrappers" and "Exec
wrappers and `find` actions" (read 2026-10-25). Every claim has a positive
control, so a branch that always (or never) fires cannot pass.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from scan_transcripts import PREFIX_UNAPPROVABLE, STRIPPED_WRAPPERS, classify  # noqa: E402
from replay_permissions import _matches_any, would_prompt  # noqa: E402


class TestStrippedList(unittest.TestCase):
    def test_the_set_is_exactly_the_documented_one(self):
        self.assertEqual(STRIPPED_WRAPPERS, {
            "timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin", "noglob"})

    def test_sudo_is_not_stripped(self):
        self.assertNotIn("sudo", STRIPPED_WRAPPERS)

    def test_the_unapprovable_set_is_exactly_the_documented_one(self):
        self.assertEqual(PREFIX_UNAPPROVABLE, {"watch", "setsid", "ionice", "flock"})


class TestSudo(unittest.TestCase):
    def test_sudo_is_an_exec_wrapper(self):
        self.assertEqual(classify("sudo rm -rf build"), ("sudo", "exec-wrapper"))

    def test_a_rm_rule_does_not_cover_sudo_rm(self):
        self.assertEqual(would_prompt("sudo rm -rf build", ["rm *"]), "sudo (exec wrapper)")

    def test_control_a_stripped_wrapper_is_covered(self):
        self.assertIsNone(would_prompt("nohup rm -rf build", ["rm *"]))
        self.assertEqual(classify("nohup rm -rf build")[0], "rm")


class TestXargs(unittest.TestCase):
    def test_bare_xargs_is_stripped_by_classify(self):
        self.assertEqual(classify("xargs make test"), ("make", "grantable"))

    def test_bare_xargs_is_covered_by_a_prefix_rule(self):
        self.assertIsNone(would_prompt("xargs make test", ["make *"]))

    def test_flagged_xargs_is_the_head(self):
        self.assertEqual(classify("xargs -n1 make test"), ("xargs", "exec-wrapper"))
        self.assertEqual(would_prompt("xargs -n1 make test", ["make *"]),
                         "xargs (exec wrapper)")


class TestPrefixUnapprovable(unittest.TestCase):
    def test_each_head_is_never_covered_by_a_prefix_rule(self):
        for head in sorted(PREFIX_UNAPPROVABLE):
            with self.subTest(head=head):
                cmd = f"{head} git status"
                self.assertEqual(classify(cmd), (head, "no-prefix"))
                self.assertEqual(would_prompt(cmd, [f"{head} *"]),
                                 f"{head} (no prefix rule covers it)")

    def test_a_tool_wide_grant_or_an_exact_rule_covers_it(self):
        for head in sorted(PREFIX_UNAPPROVABLE):
            with self.subTest(head=head):
                cmd = f"{head} git status"
                self.assertIsNone(would_prompt(cmd, ["*"]))
                self.assertIsNone(would_prompt(cmd, [cmd]))


class TestMatchesAny(unittest.TestCase):
    def test_sudo_is_not_stripped_for_an_ask_rule(self):
        self.assertIs(_matches_any("sudo rm x", ["rm *"], "ask"), False)

    def test_control_nohup_is_stripped(self):
        self.assertIs(_matches_any("nohup rm x", ["rm *"], "ask"), True)


class TestAssignmentHandling(unittest.TestCase):
    def test_an_unsafe_assignment_still_blocks_allow_coverage(self):
        self.assertIsNotNone(would_prompt("FOO=bar make test", ["make *"]))

    def test_control_a_safe_assignment_is_covered(self):
        self.assertIsNone(would_prompt("NODE_ENV=test make test", ["make *"]))


class TestWrapperArguments(unittest.TestCase):
    """A wrapper's own arguments go with it, per the grammar in `_wrapper_width`.

    The forms come from the 2.1.306 binary (`Fk`); the docs give only the
    `timeout 30 npm test` example.
    """

    STRIPPED = [
        "timeout 5 rm x", "timeout 2.5s rm x", "timeout -k 5 10 rm x",
        "timeout --signal=KILL 10 rm x", "timeout -sKILL 10 rm x",
        "timeout --kill-after 3 -- 10 rm x", "timeout --preserve-status -v 1m rm x",
        "nice -n 10 rm x", "nice -n -5 rm x", "nice -5 rm x", "nice -- rm x",
        "stdbuf -oL rm x", "stdbuf -oL -e0 -- rm x",
        "time -- rm x", "nohup rm x", "command -p rm x", "builtin -- rm x",
        "nice -n 5 timeout 10 rm x", "NODE_ENV=test timeout 5 rm x",
    ]
    KEPT = {
        "timeout abc rm x": "timeout", "timeout --bogus 5 rm x": "timeout",
        "stdbuf -o L rm x": "stdbuf", "stdbuf rm x": "stdbuf",
        "command -v rm": "command", "builtin -x rm": "builtin",
        "noglob -x rm": "noglob", "nohup": "nohup",
    }
    LEFT_FLAG = ["time -p rm x", "nice -n10 rm x", "nice --adjustment=5 rm x", "nohup -x rm"]

    def test_a_parsed_form_is_matched_as_the_inner_command(self):
        for cmd in self.STRIPPED:
            with self.subTest(cmd=cmd):
                self.assertEqual(classify(cmd), ("rm", "grantable"))
                self.assertIsNone(would_prompt(cmd, ["rm *"]))
                self.assertIs(_matches_any(cmd, ["rm *"], "ask"), True)

    def test_with_no_rules_a_parsed_form_still_prompts(self):
        # The silent case: the flag used to become the head and read as free.
        for cmd in self.STRIPPED:
            with self.subTest(cmd=cmd):
                self.assertEqual(would_prompt(cmd, []), "rm")

    def test_an_unparsed_wrapper_stays_the_head_as_an_exec_wrapper(self):
        for cmd, head in self.KEPT.items():
            with self.subTest(cmd=cmd):
                self.assertEqual(classify(cmd), (head, "exec-wrapper"))
                self.assertEqual(would_prompt(cmd, ["rm *"]), f"{head} (exec wrapper)")
                self.assertIs(_matches_any(cmd, ["rm *"], "ask"), False)

    def test_a_wrapper_argument_left_as_the_command_prompts(self):
        for cmd in self.LEFT_FLAG:
            with self.subTest(cmd=cmd):
                self.assertIsNone(classify(cmd))
                flag = cmd.split()[1]
                for patterns in (["rm *"], []):
                    self.assertEqual(would_prompt(cmd, patterns),
                                     f"{flag} (wrapper argument left as the command)")

    def test_control_a_flag_with_nothing_stripped_is_still_bleed(self):
        self.assertIsNone(would_prompt("--foo bar", []))


class TestXargsAgreesWithReplay(unittest.TestCase):
    """What classify proposes under a bare `xargs` is what replay says covers it."""

    def test_a_proposed_family_covers_the_command_and_nothing_less_does(self):
        for cmd in ("xargs git status", "xargs grep foo", "xargs make test"):
            with self.subTest(cmd=cmd):
                family, reason = classify(cmd)
                self.assertEqual(reason, "grantable")
                self.assertIsNone(would_prompt(cmd, [f"{family} *"]))
                self.assertIsNotNone(would_prompt(cmd, []))

    def test_nothing_after_xargs_is_stripped(self):
        for cmd, head in (("xargs timeout 5 rm x", "timeout"), ("xargs nohup rm x", "nohup")):
            with self.subTest(cmd=cmd):
                self.assertEqual(classify(cmd), (head, "exec-wrapper"))
                self.assertIsNotNone(would_prompt(cmd, ["rm *"]))

    def test_control_already_free_without_xargs(self):
        self.assertEqual(classify("git status"), ("git status", "already-free"))


if __name__ == "__main__":
    unittest.main()
