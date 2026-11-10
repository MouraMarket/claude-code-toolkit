#!/usr/bin/env python3
"""`/toolkit-core:onboard verify` offers to bring existing work into the
teammate's `<workspace_root>`.

The step moves a teammate's folders, so what it must never lose is where it
sits (only after the guard is proven, before the closing summary) and the
lines that keep a move safe. Sections are located by parsed headings, never a
whole-document substring search, so a line moved out of the step fails here.
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

HEADING = "Bring existing work over (optional)"

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))


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


class OnboardExistingWorkCase(unittest.TestCase):

    def setUp(self):
        text = _read(ONBOARD_MD)
        self.phase6b = _section(text, "Phase 6b ", 2)
        self.assertIsNotNone(self.phase6b, "no ## Phase 6b heading")
        step = _section(self.phase6b, HEADING, 3)
        self.assertIsNotNone(step, "no ### %s inside Phase 6b" % HEADING)
        # Whitespace-normalised, so a re-wrap never reddens a phrase check.
        self.step = " ".join(step.split())

    def test_offered_only_after_protection_and_before_close(self):
        protected = self.phase6b.index("If steps 2 and 3 both pass")
        step = self.phase6b.index("### " + HEADING)
        close = self.phase6b.index("### Close")
        self.assertLess(protected, step)
        self.assertLess(step, close)
        # The summary sits under Close, never inside the optional step, so
        # skipping the move cannot read as skipping the summary.
        self.assertNotIn("Then close with a short summary", self.step)
        self.assertIn("Offer this only when steps 2 and 3 both passed", self.step)

    def test_nothing_moves_until_rows_are_named(self):
        self.assertIn("Nothing moves until they name rows", self.step)
        self.assertIn("Move nothing yet", self.step)

    def test_cloud_folders_are_left_alone(self):
        # The rule itself, not just the paths: a reversed instruction that
        # still names the folders must fail.
        match = re.search(r"\*\*Leave anything in a cloud-synced folder where it "
                          r"is:\*\* (.*?)\. Judge each folder by its real path",
                          self.step)
        self.assertIsNotNone(match, "no leave-alone rule for cloud-synced folders")
        for path in ("~/Library/CloudStorage", "~/Library/Mobile Documents",
                     "~/Dropbox", "~/OneDrive"):
            self.assertIn(path, match.group(1))
        # Tied to the workspace check's markers, so the two lists cannot drift.
        self.assertIn("`CLOUD_MARKERS` in `lib/setup_config.py`", self.step)
        sys.path.insert(0, os.path.join(REPO, "lib"))
        import setup_config
        self.assertIn("dropbox", setup_config.CLOUD_MARKERS)
        self.assertIn("onedrive", setup_config.CLOUD_MARKERS)

    def test_a_move_never_overwrites_or_breaks_a_worktree(self):
        self.assertIn("never merge into or overwrite it", self.step)
        self.assertIn("with `mv`, never copying and deleting", self.step)
        # Both linked-worktree cases carry the keep-in-place action.
        match = re.search(r"Mark a row \*\*keep in place\*\* when moving it would "
                          r"break something: (.*?)\. 3\.", self.step)
        self.assertIsNotNone(match, "no keep-in-place rule")
        self.assertIn("whose `.git` is a *file* (a linked worktree)", match.group(1))
        self.assertIn("`git worktree list` shows more than one", match.group(1))

    def test_says_past_conversations_stay_behind(self):
        self.assertIn("past conversations stay with the old folder", self.step)

    def test_destinations_are_inside_their_workspace_root(self):
        self.assertIn("<workspace_root>", self.step)
        self.assertNotIn("repos/<name>", self.step)


class TestRunbook(unittest.TestCase):

    def test_runbook_carries_the_step_in_step_2(self):
        step2 = _section(_read(ONBOARDING_MD), "Step 2: ", 2)
        self.assertIsNotNone(step2, "no ## Step 2 in ONBOARDING.md")
        step2 = " ".join(step2.split())
        self.assertIn("bring your existing work over", step2)
        self.assertIn("cloud-synced folder (Google Drive, iCloud Drive, Dropbox, "
                      "OneDrive) stays where it is", step2)
        self.assertIn("Past conversations stay filed under a folder's old location", step2)


class TestFencedHeadingsAreQuoted(unittest.TestCase):
    """SKILL.md quotes commands in fences, and a `# comment` there is shaped
    like a heading. `_section` must neither end nor start a section on one,
    and must keep the fenced body in the section that holds it."""

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
