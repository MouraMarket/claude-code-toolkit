#!/usr/bin/env python3
"""`/onboard` Phase 4 wires the toolkit policy in (`team_policy.py ensure`),
Phase 6b step 6 checks it (`--status`), `autonomy-permissions` step 6 points
at the toolkit policy instead of a workspace gate copy, and the
autonomous grant's pointer names `policy/CLAUDE.md`. Sections are located by
parsed headings, never a whole-document substring search.
"""

import json
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
AUTONOMY_MD = os.path.join(REPO, "skills", "core", "autonomy-permissions", "SKILL.md")
AUTONOMOUS_JSON = os.path.join(REPO, "claude", "permissions.autonomous.json")

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))

STATES = ["ok", "opted-out", "missing", "dangling", "legacy-copy", "error"]
RESULTS = ["wrote", "unchanged", "opted-out", "refused"]


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _section(text, prefix, level):
    """Text of the heading section starting with `prefix` (heading level
    `level`), up to the next heading of the same or shallower level. A
    heading inside a fence is quoted text: it neither starts nor ends one."""
    heading_re = re.compile(r"^(#{1,%d})\s+(.*)$" % level, re.M)
    headings = claude_imports.matches_outside_fences(heading_re, text)
    for i, match in enumerate(headings):
        if len(match.group(1)) == level and match.group(2).startswith(prefix):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            return text[match.start():end]
    return None


def _description(text):
    lines = text.split("\n")
    start = lines.index("description: >") + 1
    end = lines.index("---", start)
    return " ".join(line.strip() for line in lines[start:end])


class OnboardTeamPolicyCase(unittest.TestCase):

    def setUp(self):
        text = _read(ONBOARD_MD)
        self.phase4 = _section(text, "Phase 4 ", 2)
        self.phase6b = _section(text, "Phase 6b ", 2)
        self.assertIsNotNone(self.phase4)
        self.assertIsNotNone(self.phase6b)

    def test_phase4_runs_ensure(self):
        bodies = re.findall(r"```bash\n(.*?)```", self.phase4, re.S)
        self.assertIn("python3 lib/team_policy.py ensure .", [b.strip() for b in bodies])
        for result in RESULTS:
            self.assertIn("`%s`" % result, self.phase4)

    def test_phase4_drops_old_copy(self):
        for gone in ("**The two data paths.**", "reconcile rather than shrug",
                     "Most of it is already written"):
            self.assertNotIn(gone, self.phase4)

    def test_phase6b_step6_checks_status(self):
        match = re.search(r"^6\. .*?(?=\n\S)", self.phase6b, re.S | re.M)
        self.assertIsNotNone(match, "no step 6 in Phase 6b")
        step = match.group(0)
        self.assertIn('team_policy.py" --status', step)
        self.assertIn("```bash", step)
        for state in STATES:
            self.assertIn("`%s`" % state, step)
        step5 = re.search(r"^5\. .*?(?=\n\S)", self.phase6b, re.S | re.M).group(0)
        self.assertNotIn("team_policy.py", step5)

    def test_phase6b_legacy_copy_bullet_compares_and_rechecks(self):
        bullet = re.search(r"^\s*- `legacy-copy`:.*?(?=\n\s*- `|\n\S|\Z)",
                           self.phase6b, re.S | re.M)
        self.assertIsNotNone(bullet)
        flat = " ".join(bullet.group(0).split())
        self.assertIn("compare it with the same section in", flat)
        self.assertIn("must read `ok`", flat)

    def test_phase6b_protected_sentence(self):
        self.assertIn("Steps 4 to 6 are reported separately", " ".join(self.phase6b.split()))


class AutonomyPermissionsCase(unittest.TestCase):

    def setUp(self):
        self.text = _read(AUTONOMY_MD)

    def test_step6_points_at_team_policy(self):
        step = _section(self.text, "6. Tighten the gates", 3)
        self.assertIsNotNone(step)
        self.assertIn("team_policy.py", step)
        self.assertNotIn("not `~/.claude/CLAUDE.md`", step)

    def test_description_names_no_workspace_gate_copy(self):
        self.assertNotIn("<workspace_root>/CLAUDE.md", _description(self.text))


class AutonomousGrantCase(unittest.TestCase):

    def test_pointer_names_policy(self):
        data = json.loads(_read(AUTONOMOUS_JSON))
        found = []

        def walk(node):
            if isinstance(node, list):
                if any(isinstance(s, str) and "THE TRADE THIS ASSUMES" in s for s in node):
                    found.append(" ".join(s for s in node if isinstance(s, str)))
                for item in node:
                    walk(item)
            elif isinstance(node, dict):
                for value in node.values():
                    walk(value)

        walk(data)
        self.assertTrue(found, "no list holds THE TRADE THIS ASSUMES")
        self.assertIn("policy/CLAUDE.md", found[0])
        self.assertNotIn("CLAUDE.md.template", found[0])


class TestFencedHeadingsAreQuoted(unittest.TestCase):
    """Phase 4 quotes its commands in fences, and a `# comment` there is
    shaped like a heading. `_section` must neither end nor start a section on
    one, and must keep the fenced body (the bash this suite reads) in it."""

    def test_section_skips_headings_inside_fences(self):
        for name, opener, closer in FENCES:
            with self.subTest(fence=name):
                doc = ("## Alpha\nlive\n%s\n# fake top\n## Beta fake\n### Gamma\n%s\n"
                       "tail\n## Beta\nbody\n" % (opener, closer))
                alpha = _section(doc, "Alpha", 2)
                self.assertIn("tail", alpha, "a fenced heading ended the section")
                self.assertIn("## Beta fake", alpha, "the fenced body was dropped")
                self.assertEqual(_section(doc, "Beta", 2), "## Beta\nbody\n")
                self.assertIsNone(_section(doc, "Gamma", 3))


if __name__ == "__main__":
    unittest.main()
