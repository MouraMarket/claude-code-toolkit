"""Contract: memory-distill and claude-md-distill name where the staged file goes.

Both skills stage the distilled text in a new file, then hand it to
`distill_guard.py install`. The old text said only "a NEW file elsewhere", and
in live runs 4 of 5 sessions first staged outside their working directory: a
permission prompt for a person, a refusal in an unattended run, and one run
stopped there instead of re-staging. The fix names two places a write needs no
permission and says to re-stage, not stop, when a write is refused anyway.

The assertions read the skill's own mtime-guard section (the bash block and the
bullet list right after it), not the whole document, so text moved out of that
section fails as surely as text deleted.
"""
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.realpath(__file__)))))
SKILLS = ("memory-distill", "claude-md-distill")
SECTION_HEADING = "## The mtime guard is mandatory"


def guard_section(skill):
    path = os.path.join(REPO_ROOT, "skills", "continuity", skill, "SKILL.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    start = text.index(SECTION_HEADING)
    end = text.find("\n## ", start + len(SECTION_HEADING))
    return text[start:] if end == -1 else text[start:end]


def bash_block_and_first_bullet(section):
    match = re.search(r"```bash\n(.*?)\n```\n\n(- .*?)(?=\n- |\n\n|\Z)", section, re.S)
    if match is None:
        raise AssertionError("no bash block followed by a bullet list in the section")
    return match.group(1), match.group(2)


class StagingPlaceIsNamed(unittest.TestCase):

    def test_both_skills_name_the_staging_place(self):
        for skill in SKILLS:
            with self.subTest(skill=skill):
                block, bullet = bash_block_and_first_bullet(guard_section(skill))
                self.assertNotIn("elsewhere", block)
                self.assertIn("staging file (where: below)", block)
                flat = " ".join(bullet.split())
                self.assertIn("the session scratchpad if your system prompt names one", flat)
                self.assertIn("a new `distill-staging/` folder under the current directory", flat)
                self.assertIn("Never outside the working directory and never under `~/.claude/`", flat)
                self.assertIn("re-stage in one of those two places and carry on", flat)

    def test_the_section_reader_sees_the_real_section(self):
        # Positive control: the reader finds the stamp/install commands, so an
        # empty or misplaced slice cannot pass the assertions above by accident.
        for skill in SKILLS:
            with self.subTest(skill=skill):
                block, _ = bash_block_and_first_bullet(guard_section(skill))
                self.assertIn("distill_guard.py\" stamp", block)
                self.assertIn("distill_guard.py\" install", block)


if __name__ == "__main__":
    unittest.main()
