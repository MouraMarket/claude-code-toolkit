"""Text pins for the backup wiring: the distill claims, /wrap-up and the config."""
import json
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))))
DISTILL_SKILLS = [
    os.path.join(REPO, "skills", "continuity", "memory-distill", "SKILL.md"),
    os.path.join(REPO, "skills", "continuity", "claude-md-distill", "SKILL.md"),
]
GUARD_FILES = [
    os.path.join(REPO, "tools", "distill-guard", "distill_guard.py"),
    os.path.join(REPO, "tools", "distill-guard", "tests", "test_distill_guard.py"),
]
STALE_CLAIMS = ["no git and no backup", "has no backup", "control and no backup"]


def read_collapsed(path):
    with open(path, encoding="utf-8") as handle:
        return re.sub(r"\s+", " ", handle.read())


class TestNoStaleBackupClaim(unittest.TestCase):
    def test_no_file_claims_no_backup(self):
        for path in DISTILL_SKILLS + GUARD_FILES:
            text = read_collapsed(path)
            for claim in STALE_CLAIMS:
                self.assertNotIn(claim, text, "%s still says %r" % (path, claim))

    def test_distill_skills_name_claude_backup(self):
        for path in DISTILL_SKILLS:
            self.assertIn("claude-backup", read_collapsed(path), path)


WRAP_UP_SKILL = os.path.join(REPO, "skills", "continuity", "wrap-up", "SKILL.md")


class TestWrapUp(unittest.TestCase):
    def test_wrap_up_runs_status(self):
        self.assertIn("claude-backup --status", read_collapsed(WRAP_UP_SKILL))

    def test_every_tool_the_skill_runs_is_linked_by_install_sh(self):
        text = read_collapsed(WRAP_UP_SKILL)
        names = sorted(set(re.findall(r"~/\.local/bin/([\w-]+)", text)))
        self.assertEqual(names, ["claude-backup", "claude-caffeinate",
                                 "claude-wrap-up-survey"])
        with open(os.path.join(REPO, "install.sh"), encoding="utf-8") as handle:
            install = handle.read()
        for name in names:
            self.assertRegex(install, r'(?m)^link "\$REPO/tools/[\w-]+/%s"\s+'
                             r'"\$HOME/\.local/bin/%s"$' % (name, name))

    def test_the_closeout_names_the_surveyed_folder(self):
        self.assertIn("Name the `repos_root` the survey used", read_collapsed(WRAP_UP_SKILL))

    def test_no_dated_internal_history(self):
        # Written for a stranger: dated incidents from another machine mean
        # nothing to them. (Timestamps inside JSON examples would be fine.)
        self.assertEqual(re.findall(r"\b20\d\d-\d\d-\d\d\b", read_collapsed(WRAP_UP_SKILL)), [])


class TestConfigExample(unittest.TestCase):
    def test_backup_section_defaults(self):
        with open(os.path.join(REPO, "config.example.json"), encoding="utf-8") as handle:
            backup = json.load(handle)["backup"]
        for key in ("destination", "keep", "max_age_days", "extra_paths", "exclude"):
            self.assertIn(key, backup)
        self.assertEqual(backup["keep"], 8)
        self.assertEqual(backup["max_age_days"], 7)
        self.assertEqual(backup["extra_paths"], [])
        self.assertEqual(backup["exclude"], [])
        # No sample folder: the example is copied verbatim on a no-TTY install.
        self.assertIsNone(backup["destination"])


if __name__ == "__main__":
    unittest.main()
