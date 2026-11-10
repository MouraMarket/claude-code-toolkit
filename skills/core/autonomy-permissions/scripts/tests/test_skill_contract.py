#!/usr/bin/env python3
"""The promises SKILL.md makes about itself, read from its parsed structure.

The skill widens what Claude may do without asking, so two promises carry its
safety: it runs only when the user invokes it (never on the model's own
judgement), and nothing it proposes is written before the user approves that
exact change. Each is pinned to the section that has to carry it, so moving
the text elsewhere fails the same way deleting it does.
"""

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
SKILL_MD = os.path.join(os.path.dirname(os.path.dirname(HERE)), "SKILL.md")
SKILL_DIR = os.path.dirname(SKILL_MD)
REPO = os.path.dirname(os.path.dirname(os.path.dirname(SKILL_DIR)))

# The import reader's fence parser: SKILL.md quotes bash whose `# comments` are
# shaped like headings, and a heading inside a fence is quoted text, not one.
sys.path.insert(0, os.path.join(REPO, "lib"))
import claude_imports  # noqa: E402

_H1_RE = re.compile(r"^# .*$", re.M)
_H1_H2_RE = re.compile(r"^#{1,2} .*$", re.M)
_H1_H3_RE = re.compile(r"^#{1,3} .*$", re.M)

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))


def _read():
    with open(SKILL_MD, encoding="utf-8") as handle:
        return handle.read()


def _body_under(text, heading_re, is_wanted):
    """Text after the first live heading line (one `heading_re` matches and
    `is_wanted` accepts) up to the next live `heading_re` heading, or None."""
    headings = claude_imports.matches_outside_fences(heading_re, text)
    for index, match in enumerate(headings):
        if is_wanted(match.group(0)):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return text[match.end() + 1:end]
    return None


def _frontmatter(text):
    """Top-level `key: value` pairs of the leading `---` block."""
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if match is None:
        return {}
    keys = {}
    for line in match.group(1).split("\n"):
        pair = re.match(r"^([a-z][a-z-]*):\s*(.*)$", line)
        if pair:
            keys[pair.group(1)] = pair.group(2).strip()
    return keys


def _banner(text):
    """The blockquote that opens the body, before the first H1."""
    body = text.split("\n---\n", 1)[1]
    first_h1 = claude_imports.matches_outside_fences(_H1_RE, body)
    head = body[:first_h1[0].start()] if first_h1 else body
    return " ".join(line[1:].strip() for line in head.split("\n") if line.startswith(">"))


def _section(text, title):
    """Body of the `## <title>` section, up to the next H1 or H2."""
    return _body_under(text, _H1_H2_RE, lambda line: line == "## " + title)


def _step(text, prefix):
    """Body of the `###` step whose heading starts with `prefix`, up to the next H1-H3."""
    return _body_under(text, _H1_H3_RE, lambda line: line.startswith(prefix))


def _bullets(section):
    """Top-level `- ` bullets of a section, each flattened to one line."""
    items = re.split(r"\n(?=- )", "\n" + section.strip())
    return [" ".join(item.split()) for item in items if item.strip().startswith("- ")]


class TestManualInvocation(unittest.TestCase):
    def test_frontmatter_disables_model_invocation(self):
        self.assertEqual(_frontmatter(_read()).get("disable-model-invocation"), "true")

    def test_banner_says_it_is_slash_only_and_changes_nothing_by_itself(self):
        banner = _banner(_read())
        self.assertIn("`disable-model-invocation: true`", banner)
        self.assertIn("`/autonomy-permissions`", banner)
        self.assertIn("changes no permission", banner)

    def test_banner_drops_the_installer_opt_out_claim(self):
        # The skill ships with the core plugin; "not installed by install.sh"
        # would tell a user the opposite of what is on their machine.
        self.assertNotIn("not installed", _banner(_read()))

    def test_positive_control_the_banner_parser_sees_the_banner(self):
        self.assertIn("ADVANCED USERS ONLY", _banner(_read()))


class TestNoWriteBeforeApproval(unittest.TestCase):
    def setUp(self):
        self.section = _section(_read(), "What this skill will not do")
        self.assertIsNotNone(self.section, "no 'What this skill will not do' section")

    def test_the_first_refusal_is_writing_settings_unapproved(self):
        first = _bullets(self.section)[0]
        self.assertTrue(first.startswith("- **Write a settings file before the user approves"),
                        first)
        self.assertIn("`AskUserQuestion`", first)
        self.assertIn("authorizes no permission change", first)

    def test_positive_control_the_bullet_parser_splits_the_section(self):
        bullets = _bullets(self.section)
        self.assertGreater(len(bullets), 5)
        self.assertTrue(any("bypassPermissions" in b for b in bullets))


class TestStepSixFindsThePolicyCheckout(unittest.TestCase):
    def test_names_the_public_marketplace_entry(self):
        step = _step(_read(), "### 6. Tighten the gates")
        self.assertIsNotNone(step)
        self.assertIn("the `claude-code-toolkit` entry in `claude plugin marketplace list`",
                      step)


class TestGuardCommandsUseTheCheckout(unittest.TestCase):
    def test_every_guard_invocation_is_checkout_relative(self):
        fences = re.findall(r"```bash\n(.*?)```", _read(), re.S)
        calls = [line for fence in fences for line in fence.split("\n")
                 if "danger-guard" in line]
        self.assertTrue(calls, "no fenced command runs the danger guard")
        for line in calls:
            self.assertIn('"<checkout>/tools/danger-guard/', line)


class TestFencedHeadingsAreQuoted(unittest.TestCase):
    """A heading-shaped line inside a fence (a bash `# comment`) is quoted
    text. No parser here may end or start a section on one, and the fenced
    body stays in the section that holds it."""

    def test_section_and_step_skip_headings_inside_fences(self):
        for name, opener, closer in FENCES:
            with self.subTest(fence=name):
                doc = ("## Alpha\n### 6. Step\nlive\n%s\n# comment\n## Beta\n%s\n"
                       "tail\n### 7. Next\nmore\n## Beta\nbody\n" % (opener, closer))
                alpha = _section(doc, "Alpha")
                self.assertIn("more", alpha, "a fenced heading ended the section")
                self.assertIn("# comment", alpha, "the fenced body was dropped")
                self.assertEqual(_section(doc, "Beta"), "body\n")
                step = _step(doc, "### 6. Step")
                self.assertIn("tail", step, "a fenced heading ended the step")
                self.assertNotIn("more", step)

    def test_banner_skips_an_h1_inside_a_fence(self):
        for name, opener, closer in FENCES:
            with self.subTest(fence=name):
                doc = ("---\nname: x\n---\n> first\n%s\n# comment\n%s\n> after\n"
                       "# Title\n> not banner\n" % (opener, closer))
                self.assertEqual(_banner(doc), "first after")


if __name__ == "__main__":
    unittest.main()
