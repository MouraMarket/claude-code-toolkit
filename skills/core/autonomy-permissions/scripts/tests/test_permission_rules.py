#!/usr/bin/env python3
"""Behavioural tests for the permission-rule matcher.

The matcher is a port of Claude Code's own `me()`/`Ea()`, so these tests are
written against the ORIGINAL's observable behaviour, not against this
implementation's structure. If the two ever disagree, the binary wins.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import permission_rules as pr  # noqa: E402


class TestParseMcp(unittest.TestCase):
    def test_a_bare_server_name_parses_with_no_tool(self):
        self.assertEqual(pr.parse_mcp("mcp__tracker"), ("tracker", None))

    def test_a_server_and_tool_parse_apart(self):
        self.assertEqual(pr.parse_mcp("mcp__tracker__get_task"),
                         ("tracker", "get_task"))

    def test_a_tool_name_containing_the_separator_is_rejoined(self):
        # `s.join("__")` in the original -- the tool keeps its own separators.
        self.assertEqual(pr.parse_mcp("mcp__srv__a__b__c"),
                         ("srv", "a__b__c"))

    def test_a_non_mcp_name_is_not_an_mcp_reference(self):
        for name in ("Bash", "Read", "skill__foo", "mcp", "mcp__"):
            self.assertIsNone(pr.parse_mcp(name), name)

    def test_an_empty_server_segment_is_rejected(self):
        # The original guards on `!o`, so `mcp____tool` has no server.
        self.assertIsNone(pr.parse_mcp("mcp____tool"))


class TestServerWideMatching(unittest.TestCase):
    """The behaviour this whole skill change rests on."""

    def test_a_bare_server_rule_matches_every_tool_on_that_server(self):
        for tool in ("mcp__tracker__get_task", "mcp__tracker__delete_task",
                     "mcp__tracker__anything_at_all"):
            self.assertTrue(pr.mcp_rule_matches("mcp__tracker", tool), tool)

    def test_a_star_tool_rule_matches_every_tool_on_that_server(self):
        self.assertTrue(
            pr.mcp_rule_matches("mcp__tracker__*", "mcp__tracker__delete_task"))

    def test_a_server_rule_does_not_reach_a_different_server(self):
        self.assertFalse(
            pr.mcp_rule_matches("mcp__tracker", "mcp__gmail__send_message"))

    def test_a_server_prefix_is_not_a_server_match(self):
        # Guards against a `startswith` implementation: "tracker" must not
        # match "tracker_extra". This is the mistake the port exists to avoid.
        self.assertFalse(
            pr.mcp_rule_matches("mcp__tracker", "mcp__tracker_extra__get"))

    def test_an_exact_tool_rule_matches_only_that_tool(self):
        rule = "mcp__tracker__get_task"
        self.assertTrue(pr.mcp_rule_matches(rule, "mcp__tracker__get_task"))
        self.assertFalse(pr.mcp_rule_matches(rule, "mcp__tracker__get_list"))

    def test_a_non_mcp_rule_never_matches_an_mcp_tool(self):
        self.assertFalse(pr.mcp_rule_matches("Bash", "mcp__tracker__get_task"))

    def test_a_tool_rule_does_not_match_the_bare_server_name(self):
        # `o.toolName !== undefined` in the original: a pattern rule cannot
        # match a call that names no tool.
        self.assertFalse(pr.mcp_rule_matches("mcp__tracker__g*", "mcp__tracker"))

    def test_a_star_rule_matches_the_bare_server_name(self):
        # `n.toolName==="*"` short-circuits BEFORE the `o.toolName !== undefined`
        # check in the original, so `__*` is whole-server even here; folding it
        # into the glob branch would be equivalent for every named tool and
        # wrong only for this one.
        self.assertTrue(pr.mcp_rule_matches("mcp__tracker__*", "mcp__tracker"))


class TestPartialGlobs(unittest.TestCase):
    def test_legality_and_matching_are_tracked_as_separate_claims(self):
        # These were one flag, and collapsing them produced a false statement
        # in the docs: the binary's validator DOES offer `mcp__srv__get_*` as
        # an allow-rule example, while `Ea`'s pattern branch stays unread.
        # Flipping either should be a deliberate act with evidence.
        self.assertIs(pr.PARTIAL_GLOB_RULES_ARE_LEGAL, True)
        self.assertIs(pr.PARTIAL_GLOB_MATCHING_VERIFIED, False)

    def test_a_partial_pattern_is_still_modelled_rather_than_ignored(self):
        # A rule a human already wrote must be modelled, or the replay reports
        # a prompt that will not happen.
        self.assertTrue(
            pr.mcp_rule_matches("mcp__tracker__get_*", "mcp__tracker__get_task"))
        self.assertFalse(
            pr.mcp_rule_matches("mcp__tracker__get_*", "mcp__tracker__set_task"))


class TestVerdictPrecedence(unittest.TestCase):
    def rules(self, allow=(), ask=(), deny=()):
        return {"allow": [pr.parse_rule(r) for r in allow],
                "ask": [pr.parse_rule(r) for r in ask],
                "deny": [pr.parse_rule(r) for r in deny]}

    def test_an_uncovered_tool_prompts(self):
        self.assertEqual(
            pr.verdict("mcp__tracker__get_task", self.rules()), "prompt")

    def test_a_server_wide_allow_covers_a_tool(self):
        r = self.rules(allow=["mcp__tracker"])
        self.assertEqual(pr.verdict("mcp__tracker__get_task", r), "allow")

    def test_deny_beats_a_server_wide_allow(self):
        # The entire safety argument for the recommended pattern: one broad
        # allow, with the destructive few carved back out.
        r = self.rules(allow=["mcp__tracker"],
                       deny=["mcp__tracker__delete_task"])
        self.assertEqual(pr.verdict("mcp__tracker__delete_task", r), "deny")
        self.assertEqual(pr.verdict("mcp__tracker__get_task", r), "allow")

    def test_ask_beats_allow_but_not_deny(self):
        r = self.rules(allow=["mcp__tracker"], ask=["mcp__tracker__delete_task"])
        self.assertEqual(pr.verdict("mcp__tracker__delete_task", r), "ask")
        r2 = self.rules(allow=["mcp__tracker"],
                        ask=["mcp__tracker__delete_task"],
                        deny=["mcp__tracker__delete_task"])
        self.assertEqual(pr.verdict("mcp__tracker__delete_task", r2), "deny")

    def test_a_bare_tool_rule_grants_a_non_mcp_tool(self):
        self.assertEqual(pr.verdict("Write", self.rules(allow=["Write"])),
                         "allow")

    def test_a_path_scoped_rule_does_not_count_as_a_blanket_grant(self):
        # `Write(~/x/**)` must not read as "Write is allowed" -- that would
        # under-report prompting on every other path.
        r = self.rules(allow=["Write(~/safe/**)"])
        self.assertEqual(pr.verdict("Write", r), "prompt")

    def test_skill_is_modelled_as_prompting_not_free(self):
        # Filed as always-free, an uncovered Skill call never reached verdict().
        for name in ("Skill", "SlashCommand"):
            self.assertNotIn(name, pr.ALWAYS_FREE_TOOLS)
            self.assertTrue(pr.is_modelled(name))
        self.assertEqual(pr.verdict("Skill", self.rules()), "prompt")
        self.assertEqual(pr.verdict("Skill", self.rules(allow=["Skill"])),
                         "allow")


class TestLoadRules(unittest.TestCase):
    """`load_rules` reads a fixed set of paths, one of which is `~`-relative.

    Every test here sandboxes $HOME. Without that they assert against whatever
    settings the machine running them happens to have -- which is the ambient-
    state dependency the repo's testing standard forbids, and it is not
    hypothetical: the first cut of `test_a_missing_or_malformed_scope...`
    failed by picking up 59 real rules off this laptop.
    """

    def setUp(self):
        self._home = os.environ.get("HOME")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["HOME"] = os.path.join(self._tmp.name, "home")
        os.makedirs(os.path.join(os.environ["HOME"], ".claude"))

    def tearDown(self):
        if self._home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._home
        self._tmp.cleanup()

    def write_user_scope(self, payload):
        path = os.path.join(os.environ["HOME"], ".claude/settings.json")
        with open(path, "w") as fh:
            json.dump(payload, fh)

    def test_scopes_merge_and_carry_provenance(self):
        self.write_user_scope({"permissions": {"allow": ["mcp__usr"]}})
        ws = os.path.join(self._tmp.name, "ws")
        os.makedirs(os.path.join(ws, ".claude"))
        with open(os.path.join(ws, ".claude/settings.json"), "w") as fh:
            json.dump({"permissions": {"allow": ["mcp__proj"]}}, fh)
        with open(os.path.join(ws, ".claude/settings.local.json"), "w") as fh:
            json.dump({"permissions": {"allow": ["mcp__local"]}}, fh)
        rules = pr.load_rules(workspace=ws)
        raws = {r.raw: r.scope for r in rules["allow"]}
        self.assertEqual(raws.get("mcp__usr"), "user")
        self.assertEqual(raws.get("mcp__proj"), "project")
        self.assertEqual(raws.get("mcp__local"), "local")

    def test_the_user_scope_alone_is_enough_to_cover_a_tool(self):
        # Positive control for the sandbox: proves $HOME is actually being
        # read, so the malformed-scope test below cannot pass vacuously.
        self.write_user_scope({"permissions": {"allow": ["mcp__usr"]}})
        ws = os.path.join(self._tmp.name, "empty-ws")
        os.makedirs(ws)
        self.assertEqual(pr.verdict("mcp__usr__x", pr.load_rules(workspace=ws)),
                         "allow")

    def test_a_missing_or_malformed_scope_is_skipped_not_fatal(self):
        ws = os.path.join(self._tmp.name, "ws")
        os.makedirs(os.path.join(ws, ".claude"))
        with open(os.path.join(ws, ".claude/settings.json"), "w") as fh:
            fh.write("{ not json")
        self.assertEqual(pr.load_rules(workspace=ws)["allow"], [])

    def test_an_explicit_settings_path_does_not_merge_other_scopes(self):
        # Evaluating a proposal must not silently borrow the live config's
        # grants, or every proposal looks better than it is.
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"permissions": {"allow": ["mcp__only"]}}, fh)
            path = fh.name
        try:
            rules = pr.load_rules(settings_path=path)
            self.assertEqual([r.raw for r in rules["allow"]], ["mcp__only"])
        finally:
            os.unlink(path)


class TestProposal(unittest.TestCase):
    def test_a_proposal_names_the_destructive_tools_it_would_sweep_in(self):
        calls = ["mcp__tracker__get_task"] * 3 + ["mcp__tracker__delete_task"]
        rules = {"allow": [], "ask": [], "deny": []}
        out = pr.propose_server_rules(calls, rules)["tracker"]
        self.assertEqual(out["calls"], 4)
        self.assertEqual(out["proposed_rule"], "mcp__tracker")
        self.assertIn("mcp__tracker__delete_task", out["destructive_seen"])
        self.assertNotIn("mcp__tracker__get_task", out["destructive_seen"])

    def test_a_proposal_counts_only_the_calls_that_would_prompt(self):
        calls = ["mcp__tracker__get_task", "mcp__tracker__delete_task"]
        rules = {"allow": [pr.parse_rule("mcp__tracker__get_task")],
                 "ask": [], "deny": []}
        out = pr.propose_server_rules(calls, rules)["tracker"]
        self.assertEqual(out["prompting"], {"mcp__tracker__delete_task"})

    def test_an_existing_server_rule_is_reported_as_present(self):
        rules = {"allow": [pr.parse_rule("mcp__tracker")], "ask": [], "deny": []}
        out = pr.propose_server_rules(["mcp__tracker__x"], rules)["tracker"]
        self.assertTrue(out["server_rule_present"])
        self.assertEqual(out["prompting"], set())

    def test_non_mcp_calls_are_ignored_by_the_proposal(self):
        rules = {"allow": [], "ask": [], "deny": []}
        self.assertEqual(pr.propose_server_rules(["Bash", "Read"], rules), {})


class TestDestructiveHeuristic(unittest.TestCase):
    def test_it_reads_the_tool_leaf_not_the_server_name(self):
        # A server whose NAME contains a hint word must not mark every one of
        # its tools destructive.
        self.assertFalse(pr.looks_destructive("mcp__send_grid__get_stats"))
        self.assertTrue(pr.looks_destructive("mcp__send_grid__send_email"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
