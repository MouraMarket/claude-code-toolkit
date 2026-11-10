#!/usr/bin/env python3
"""Pins the split between policy/CLAUDE.md (toolkit policy, imported live) and
CLAUDE.md.template (the personal skeleton a user copies once).

Headings are parsed, never searched for in the whole document: a sentinel
phrase quoted in prose must not count as the section being present.
"""

import os
import re
import sys
import unittest

LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB)
import claude_imports
import team_policy

REPO = os.path.dirname(LIB)
POLICY = os.path.join(REPO, "policy", "CLAUDE.md")
TEMPLATE = os.path.join(REPO, "CLAUDE.md.template")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _unfenced_lines(text):
    """Lines outside fenced blocks: fenced text is quoted, never a live instruction.

    The import reader's own parser, so fence length and nesting are judged the
    way the import itself is.
    """
    return [line for _n, line in claude_imports.lines_outside_fences(text)]


def _headings(text):
    """`#`/`##` heading lines outside fenced blocks."""
    return [line.rstrip() for line in _unfenced_lines(text) if re.match(r"^#{1,2} ", line)]


def _body_under(text, heading, end_pattern):
    out, inside = [], False
    for line in _unfenced_lines(text):
        if line.rstrip() == heading:
            inside = True
            continue
        if inside and re.match(end_pattern, line):
            break
        if inside:
            out.append(line)
    return "\n".join(out)


def _section(text, heading):
    """The unfenced body of one `# ` section, up to the next `# ` heading."""
    return _body_under(text, heading, r"^# ")


def _subsection(text, heading):
    """The unfenced body of one `## ` section, up to the next `#`/`##` heading."""
    return _body_under(text, heading, r"^#{1,2} ")


class TestFencedTextIsQuoted(unittest.TestCase):
    """The helpers the policy tests read through: fenced text never counts."""

    FENCES = {
        "backtick": ("```", "```"),
        "tilde": ("~~~", "~~~"),
        # A longer fence holding a shorter one is one fence.
        "nested": ("````\n```", "```\n````"),
    }

    def test_a_fenced_heading_is_not_a_heading(self):
        for name, (top, bottom) in self.FENCES.items():
            with self.subTest(name):
                text = "# Live\n%s\n# Quoted\n## Quoted too\n%s\n## Live too" % (top, bottom)
                self.assertEqual(_headings(text), ["# Live", "## Live too"])

    def test_a_fenced_body_is_not_section_text(self):
        for name, (top, bottom) in self.FENCES.items():
            with self.subTest(name):
                text = "# Live\n%s\nquoted rule\n%s\nlive rule\n## Sub\n%s\nquoted sub\n%s\n" \
                       "live sub\n# Next" % (top, bottom, top, bottom)
                self.assertNotIn("quoted", _section(text, "# Live"))
                self.assertIn("live rule", _section(text, "# Live"))
                self.assertEqual(_subsection(text, "## Sub").strip(), "live sub")

    def test_a_fenced_heading_does_not_end_a_section(self):
        for name, (top, bottom) in self.FENCES.items():
            with self.subTest(name):
                text = "# Live\n%s\n# Quoted\n%s\nstill live\n# Next" % (top, bottom)
                self.assertIn("still live", _section(text, "# Live"))


class TestPolicySplit(unittest.TestCase):

    def test_sentinels_live_in_policy_and_not_in_template(self):
        policy = {team_policy._heading_text(h) for h in _headings(_read(POLICY))}
        template = {team_policy._heading_text(h) for h in _headings(_read(TEMPLATE))}
        for heading in team_policy.SENTINEL_HEADINGS:
            self.assertIn(heading, policy)
            self.assertNotIn(heading, template)

    def test_template_headings_are_exactly_the_personal_ones(self):
        template = set(_headings(_read(TEMPLATE)))
        self.assertEqual(template, {"# CLAUDE.md — template", "# About me",
                                    "# Tone", "# My machine", "# Toolkit policy"})
        self.assertEqual(template & set(_headings(_read(POLICY))), set())

    def test_policy_has_no_import_line(self):
        self.assertEqual(claude_imports.iter_imports(_read(POLICY)), [])
        # positive control: the scan does see an import
        self.assertEqual(len(claude_imports.iter_imports("x\n@/a/b\n")), 1)

    def test_template_keeps_a_todo_line_for_the_install_check(self):
        self.assertTrue(any(line.startswith("**TODO")
                            for line in _read(TEMPLATE).split("\n")))

    def test_contradiction_order_lists_six_items_in_order(self):
        body = _section(_read(POLICY), "# Contradiction handling")
        self.assertTrue(body, "# Contradiction handling not found in policy")
        phrases = ("current conversation", "repo's own", "workspace `CLAUDE.md`",
                   "This file", "Personal preferences", "Memory")
        indexes = [body.find(phrase) for phrase in phrases]
        self.assertNotIn(-1, indexes, indexes)
        self.assertEqual(indexes, sorted(set(indexes)))

    def test_carried_guard_and_merge_stops_sit_in_the_gate_section(self):
        body = " ".join(_subsection(_read(POLICY), "## Gate at decisions, not execution").split())
        self.assertIn("a guard, deny rule or failing check that blocks the work", body)
        self.assertIn("**Never disable a guard to get unstuck.**", body)
        self.assertIn("never merge past a failing or pending check", body)

    def test_carried_evidence_rules_sit_under_how_to_work(self):
        text = _read(POLICY)
        self.assertIn("## Evidence — show it, don't claim it",
                      _section(text, "# How to work with me").split("\n"))
        body = " ".join(_subsection(text, "## Evidence — show it, don't claim it").split())
        for phrase in ("**Run the tests and quote the actual output**",
                       "**Where there is no test suite, say so and falsify by hand:**",
                       "**A green result is not evidence until you have seen it fail.**",
                       "say which numbers are measured and which are inferred"):
            self.assertIn(phrase, body)

    def test_carried_routing_table_sits_under_working_conventions(self):
        body = _section(_read(POLICY), "# Working conventions")
        rows = [line for line in body.split("\n") if line.strip().startswith("|")]
        self.assertEqual(len(rows), 6, rows)  # header, rule, four routes
        self.assertTrue(rows[2].rstrip().endswith(
            "| `TOOLING.md` (and its digest block if it belongs there) |"))
        self.assertIn("`TOOLING.md` wins and the memory goes", body)

    def test_policy_names_builder_agents_only_with_their_plugin(self):
        # Runs now; lib/tests/test_plugin_layout.py checks every dispatch site
        # once rows 14a, 14b and 15 land.
        bare = re.findall(r"`(executor|task-reviewer|code-reviewer)`", _read(POLICY))
        self.assertEqual(bare, [])
        self.assertEqual(re.findall(r"`[a-z-]+:(?:executor|task-reviewer)`", _read(POLICY)),
                         ["`toolkit-build:executor`", "`toolkit-build:executor`",
                          "`toolkit-build:task-reviewer`"])

    def test_policy_fits_context_lint_cap(self):
        self.assertLessEqual(len(_read(POLICY).split("\n")), 800)


if __name__ == "__main__":
    unittest.main()
