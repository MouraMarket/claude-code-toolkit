#!/usr/bin/env python3
"""Every instruction to add a profile also names continuity.

A "profiles" list replaces the default set, so an example or a how-to that lists
builder, mac or maintainer without continuity quietly steers a reader to a
machine that no longer installs session continuity. This reads the prose that
carries those instructions.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODEL_ROLES_FILES = ("CLAUDE.md.template", os.path.join("policy", "CLAUDE.md"))
PROSE_FILES = ("ONBOARDING.md", "install.sh", "config.example.json",
               os.path.join("skills", "core", "onboard", "SKILL.md"))
LIST_LITERAL = re.compile(r"\[[^\[\]]*\]")
ADDED_PROFILES = ('"builder"', '"mac"', '"maintainer"')


def read(relpath):
    with open(os.path.join(REPO, relpath), encoding="utf-8") as handle:
        return handle.read()


def model_roles_file():
    """The file that currently holds the `# Model roles` heading."""
    for relpath in MODEL_ROLES_FILES:
        path = os.path.join(REPO, relpath)
        if os.path.isfile(path) and re.search(r"^# Model roles", read(relpath), re.M):
            return relpath
    return None


def model_roles_section(text):
    """From the `# Model roles` heading to the next `# ` heading."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("# Model roles"))
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("# ")),
               len(lines))
    return "\n".join(lines[start:end])


def list_literals_missing_continuity(relpath):
    """(literals naming an added profile, those among them lacking continuity)."""
    text = read(relpath).replace('\\"', '"')
    seen = [m for m in LIST_LITERAL.findall(text)
            if any(name in m for name in ADDED_PROFILES)]
    return seen, [m for m in seen if '"continuity"' not in m]


class TestModelRolesNamesContinuity(unittest.TestCase):

    def test_the_section_exists_somewhere(self):
        self.assertIsNotNone(model_roles_file(),
                             "no file in %s holds a '# Model roles' heading" % (MODEL_ROLES_FILES,))

    def test_the_section_names_continuity(self):
        relpath = model_roles_file()
        self.assertIsNotNone(relpath)
        self.assertIn('"continuity"', model_roles_section(read(relpath)),
                      "%s: # Model roles never says to keep continuity" % relpath)


class TestEveryProfileListKeepsContinuity(unittest.TestCase):

    def test_each_list_naming_an_added_profile_also_names_continuity(self):
        files = list(PROSE_FILES) + [model_roles_file()]
        self.assertIsNotNone(files[-1])
        missing = {}
        for relpath in files:
            _, lacking = list_literals_missing_continuity(relpath)
            if lacking:
                missing[relpath] = lacking
        self.assertEqual({}, missing, "profile lists without continuity")

    def test_the_scan_sees_a_list_in_the_files_that_carry_examples(self):
        # install.sh stays in PROSE_FILES, so a list it ever prints is checked,
        # but it carries no example: the work installer's only one was a legacy
        # plugin-rename warning the port does not ship.
        for relpath in ("ONBOARDING.md", "config.example.json"):
            seen, _ = list_literals_missing_continuity(relpath)
            self.assertTrue(seen, "%s: scan found no profile list literal" % relpath)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
