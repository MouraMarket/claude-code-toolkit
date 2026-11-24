#!/usr/bin/env python3
"""The `/onboard` skill wires `claude-setup-audit` into the walkthrough at
three points: a snapshot before anything writes, the audit report in
Phase 1b, and `apply-safe` before the reconciliation steps in Phase 2. These
tests parse the document's own fenced ```bash blocks, in order, and locate
headings by their parsed line text -- never a whole-document substring search
(repo CLAUDE.md, "Assert against parsed structure").

`ONBOARDING.md` and `README.md` get a lighter substring check each.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.dirname(HERE))
import claude_imports  # noqa: E402
ONBOARD_MD = os.path.join(REPO, "skills", "core", "onboard", "SKILL.md")
ONBOARDING_MD = os.path.join(REPO, "ONBOARDING.md")
README_MD = os.path.join(REPO, "README.md")

_FENCE_RE = re.compile(r"```bash\n(.*?)```", re.S)
_HEADING_RE = re.compile(r"^(##\s+.*)$", re.M)

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))


def _bash_blocks(text):
    """Every fenced ```bash block's contents, in document order."""
    return [m.group(1) for m in _FENCE_RE.finditer(text)]


def _first_index(blocks, needle):
    for i, block in enumerate(blocks):
        if needle in block:
            return i
    return None


def _heading_offsets(text, prefix):
    """Character offsets of every heading line starting with `prefix`
    (e.g. "## Phase 1b"), read from the document's own parsed heading
    lines -- never a bare `prefix in text` search. A heading inside a fence
    is quoted text, not a heading."""
    offsets = []
    for m in claude_imports.matches_outside_fences(_HEADING_RE, text):
        if m.group(1).startswith(prefix):
            offsets.append(m.start())
    return offsets


def _block_offset(text, block_content):
    """The character offset of the ```bash fence whose body is
    `block_content` (the first match, which is unambiguous for the needles
    used below since each is unique in this document)."""
    return text.index("```bash\n" + block_content)


class OnboardDocCase(unittest.TestCase):

    def setUp(self):
        with open(ONBOARD_MD, encoding="utf-8") as handle:
            self.text = handle.read()
        self.blocks = _bash_blocks(self.text)
        self.assertTrue(self.blocks, "no fenced bash blocks found at all")


class TestSnapshotBlockComesFirst(OnboardDocCase):
    """Test 1: the first block naming `claude-setup-audit snapshot` precedes
    the first block naming `setup_config.py` and the first naming `mkdir` --
    it must be the first command block in the file that writes anything."""

    def test_snapshot_precedes_setup_config_and_mkdir(self):
        snapshot_i = _first_index(self.blocks, "claude-setup-audit snapshot")
        setup_config_i = _first_index(self.blocks, "setup_config.py")
        mkdir_i = _first_index(self.blocks, "mkdir")
        self.assertIsNotNone(snapshot_i, "no block calls claude-setup-audit snapshot")
        self.assertIsNotNone(setup_config_i, "no block calls setup_config.py")
        self.assertIsNotNone(mkdir_i, "no block calls mkdir")
        self.assertLess(snapshot_i, setup_config_i)
        self.assertLess(snapshot_i, mkdir_i)


class TestAuditJsonBetweenPhase1bAndPhase2(OnboardDocCase):
    """Test 2: a block calling `claude-setup-audit --json` sits between the
    `## Phase 1b` and `## Phase 2` headings, located by parsed heading lines."""

    def test_audit_json_block_is_inside_phase_1b(self):
        phase_1b = _heading_offsets(self.text, "## Phase 1b")
        phase_2 = _heading_offsets(self.text, "## Phase 2")
        self.assertEqual(len(phase_1b), 1, "expected exactly one ## Phase 1b heading")
        self.assertEqual(len(phase_2), 1, "expected exactly one ## Phase 2 heading")

        json_i = _first_index(self.blocks, "claude-setup-audit --json")
        self.assertIsNotNone(json_i, "no block calls claude-setup-audit --json")
        offset = _block_offset(self.text, self.blocks[json_i])
        self.assertGreater(offset, phase_1b[0])
        self.assertLess(offset, phase_2[0])


class TestApplySafePrecedesInstall(OnboardDocCase):
    """Test 3: the block naming `claude-setup-audit apply-safe` comes before
    the first block whose only command is the install, `./install.sh`, bare or
    with the suite skip row 84 gives it after a passing preview."""

    INSTALL_BLOCKS = ("./install.sh", "CLAUDE_INSTALL_SKIP_SUITES=1 ./install.sh")

    def test_apply_safe_precedes_the_bare_install_block(self):
        apply_i = _first_index(self.blocks, "claude-setup-audit apply-safe")
        self.assertIsNotNone(apply_i, "no block calls claude-setup-audit apply-safe")
        install_i = None
        for i, block in enumerate(self.blocks):
            if block.strip() in self.INSTALL_BLOCKS:
                install_i = i
                break
        self.assertIsNotNone(install_i, "no block whose only command is ./install.sh")
        self.assertLess(apply_i, install_i)


class TestOnboardingAndReadmeMentionIt(unittest.TestCase):
    """Test 4: `ONBOARDING.md` mentions the tool; `README.md` carries its
    tool-table row."""

    def test_onboarding_md_mentions_claude_setup_audit(self):
        with open(ONBOARDING_MD, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("claude-setup-audit", text)

    def test_readme_has_a_tool_table_row(self):
        with open(README_MD, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertTrue(
            any(line.startswith("| `tools/setup-audit/`") for line in lines),
            "no README.md row starting '| `tools/setup-audit/`'")


class TestFencedHeadingsAreQuoted(unittest.TestCase):
    """A `##` line inside a fence is quoted text. `_heading_offsets` must not
    report one, and must leave every live heading's offset where it sits
    (the fenced body is still in the text)."""

    def test_heading_offsets_skip_headings_inside_fences(self):
        for name, opener, closer in FENCES:
            with self.subTest(fence=name):
                template = ("## Phase 1b\nlive\n%s\n## Phase 2 fake\n%s\ntail\n"
                            "## Phase 2\nbody\n")
                doc = template % (opener, closer)
                self.assertEqual(_heading_offsets(doc, "## Phase 2"),
                                 [doc.index("## Phase 2\nbody")])
                self.assertEqual(_heading_offsets(doc, "## Phase 1b"), [0])
                self.assertIn("## Phase 2 fake", doc, "the fenced body was dropped")
                # Positive control: without the fence markers the same line IS a heading.
                bare = template % ("", "")
                self.assertEqual(_heading_offsets(bare, "## Phase 2"),
                                 [bare.index("## Phase 2 fake"), bare.index("## Phase 2\nbody")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
