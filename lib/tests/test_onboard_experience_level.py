#!/usr/bin/env python3
"""`/onboard` asks a new / some / power-user level first, and that level may
change how things are explained -- never what is consented to or guarded.

The silent failure this pins: the level question drifting below Phase 0 (so the
early phases run at the wrong register), or the "identical at every level"
invariants drifting out of the section that grants the per-level differences --
at which point a reader of the table alone learns power users get a faster path
and never learns what that path may not skip.

Every assertion reads a parsed section (text between one `## ` heading and the
next), never the whole document (repo CLAUDE.md, "Assert against parsed
structure"). The ONBOARDING.md half is checked too.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import claude_imports  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ONBOARD_MD = os.path.join(REPO, "skills", "core", "onboard", "SKILL.md")
ONBOARDING_MD = os.path.join(REPO, "ONBOARDING.md")

LEVELS = ["New to this", "Some experience", "Power user"]

_HEADING_RE = re.compile(r"^(## .*)$", re.M)

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _sections(text):
    """[(heading line, body)] for every `## ` heading, in document order. A
    `## ` line inside a fence is quoted text, not a heading."""
    headings = claude_imports.matches_outside_fences(_HEADING_RE, text)
    ends = [match.start() for match in headings[1:]] + [len(text)]
    return [(match.group(1), text[match.end():end]) for match, end in zip(headings, ends)]


def _preamble(text):
    """Everything before the first `## ` heading (frontmatter, introduction)."""
    headings = claude_imports.matches_outside_fences(_HEADING_RE, text)
    return text[:headings[0].start()] if headings else text


def _section(text, prefix):
    for heading, body in _sections(text):
        if heading.startswith(prefix):
            return body
    return None


def _heading_index(text, prefix):
    for i, (heading, _) in enumerate(_sections(text)):
        if heading.startswith(prefix):
            return i
    return None


class TestTheLevelIsAskedFirst(unittest.TestCase):
    def setUp(self):
        self.text = _read(ONBOARD_MD)

    def test_the_level_section_precedes_phase_0(self):
        level = _heading_index(self.text, "## Their level")
        phase0 = _heading_index(self.text, "## Phase 0")
        self.assertIsNotNone(level, "no '## Their level' section")
        self.assertIsNotNone(phase0)
        self.assertLess(level, phase0)

    def test_the_level_is_the_first_question_about_them(self):
        behave = _section(self.text, "## How to behave")
        items = re.findall(r"^1\. (.*)$", behave, flags=re.M)
        self.assertEqual(len(items), 1, "expected one numbered question list")
        self.assertIn("new to them", items[0])

    def test_the_section_offers_each_level_as_an_option(self):
        body = _section(self.text, "## Their level")
        options = re.findall(r"^- \*\*(.+?)\*\*", body, flags=re.M)
        self.assertEqual(options, LEVELS)


class TestWhatTheLevelChanges(unittest.TestCase):
    def setUp(self):
        self.body = _section(_read(ONBOARD_MD), "## Their level")

    def _table(self):
        rows = [line for line in self.body.splitlines() if line.startswith("|")]
        return [[c.strip() for c in row.strip("|").split("|")] for row in rows]

    def test_the_table_has_one_column_per_level(self):
        header = self._table()[0]
        self.assertEqual(header[1:], LEVELS)

    def test_the_table_covers_wording_and_collisions(self):
        labels = [row[0] for row in self._table()[2:]]
        self.assertIn("**Terms**", labels)
        self.assertIn("**A collision (Phase 2)**", labels)

    def test_the_invariants_sit_in_the_same_section_as_the_table(self):
        paragraphs = [p for p in self.body.split("\n\n")
                      if p.startswith("**Identical at every level:**")]
        self.assertEqual(len(paragraphs), 1)
        invariant = paragraphs[0]
        for must in ("snapshot", "apply-safe", "consent prompt", "unguarded",
                     "danger-guard", "autonomous"):
            self.assertIn(must, invariant)

    def test_phase_4_writes_the_level_into_their_claude_md(self):
        phase4 = _section(_read(ONBOARD_MD), "## Phase 4")
        about = [p for p in phase4.split("\n- ") if p.startswith("**About me.**")]
        self.assertEqual(len(about), 1)
        self.assertIn("level they chose", about[0])


class TestNoStaleQuestionCount(unittest.TestCase):
    """Adding the level made three questions four. The count was written in
    several phrasings; a sweep for one missed the rest (repo CLAUDE.md, "sweep
    for every PHRASING"). Neither doc states a count now."""

    # Phrasings about ASKING, so "Three things carry most of the value" and
    # "It reports three things about a file" (not questions) stay legal.
    STALE = re.compile(r"\bask(s)?\s+(you\s+)?(exactly\s+)?(three|four)\b|"
                       r"\bonly (three|four) things\b|\bof your (three|four)\b|"
                       r"\b(three|four) questions\b|"
                       r"\bquestion (one|two|three|four) of\b", re.I)

    def _assert_no_count(self, path):
        # Everything before the first `## ` (frontmatter, introduction) counts too.
        text = _read(path)
        for heading, body in [("(before the first ## heading)", _preamble(text))] + _sections(text):
            hits = [m.group(0) for m in self.STALE.finditer(body)]
            self.assertEqual(hits, [], "%s, %s" % (os.path.basename(path), heading))

    def test_the_skill_does_not_state_a_question_count(self):
        self._assert_no_count(ONBOARD_MD)

    def test_onboarding_md_does_not_state_a_question_count(self):
        self._assert_no_count(ONBOARDING_MD)

    def test_the_pattern_catches_each_retired_phrasing(self):
        # Positive control: the exact sentences this change removed.
        for old in ("It will ask you three things", "Ask exactly three things",
                    "the only three things to actually ask",
                    "This is question two of your three"):
            self.assertRegex(old, self.STALE)


class TestFencedHeadingsAreQuoted(unittest.TestCase):
    """A `## ` line inside a fence is quoted text. It must not start or end a
    section or the preamble, and the fenced body stays where it sits."""

    def test_sections_and_preamble_skip_headings_inside_fences(self):
        for name, opener, closer in FENCES:
            with self.subTest(fence=name):
                doc = ("intro\n%s\n## Quoted\n%s\nmore intro\n"
                       "## Alpha\nlive\n%s\n## Beta fake\n%s\ntail\n## Beta\nbody\n"
                       % (opener, closer, opener, closer))
                self.assertEqual([h for h, _ in _sections(doc)], ["## Alpha", "## Beta"])
                alpha = _section(doc, "## Alpha")
                self.assertIn("tail", alpha, "a fenced heading ended the section")
                self.assertIn("## Beta fake", alpha, "the fenced body was dropped")
                self.assertEqual(_section(doc, "## Beta"), "\nbody\n")
                self.assertIn("more intro", _preamble(doc))


if __name__ == "__main__":
    unittest.main()
