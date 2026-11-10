#!/usr/bin/env python3
"""install.sh says whether an enabled prompt reminder can actually fire.

The reminder hook prints nothing when `prompt_optimizer.enabled` is true but no
prompt-optimizer skill is installed, so an enabled setting with no skill reads
as working on a machine where it never speaks. Both a real run and `--check`
print the real state in a "Prompt reminder" section, and print nothing at all
when the setting is off or absent.

The state is only ever reported: it never turns a passing check red, because a
missing optional skill is not a broken install.
"""

import json
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_install_check_fails import CheckVerdictTestCase  # noqa: E402
import install_fixture as fx  # noqa: E402

SECTION = "Prompt reminder"
INERT = ("prompt_optimizer.enabled is true but no prompt-optimizer skill is "
         "installed, so the reminder stays silent.")
ON = "reminds before each subagent dispatch to use /prompt-optimizer"


class PromptReminderTestCase(CheckVerdictTestCase):

    def setUp(self):
        super().setUp()
        # The fixture repo is minimal: install.sh runs the status helper from
        # the repo it is run in, so that helper has to be there.
        dest = os.path.join(self.repo, "tools", "prompt-optimizer", "prompt_optimizer_lib.py")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(fx.REPO_ROOT, "tools", "prompt-optimizer",
                                     "prompt_optimizer_lib.py"), dest)

    def write_config(self, data):
        path = os.path.join(self.home, ".config", "claude-code-toolkit", "config.json")
        with open(path, "w") as handle:
            json.dump(data, handle)

    def install_user_skill(self):
        folder = os.path.join(self.home, ".claude", "skills", "prompt-optimizer")
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "w") as handle:
            handle.write("---\nname: prompt-optimizer\n---\n")

    def both_modes(self):
        return (("apply", self.install()), ("check", self.check()))


class TestPromptReminderStatus(PromptReminderTestCase):

    def test_no_config_section_prints_no_section(self):
        for mode, proc in self.both_modes():
            with self.subTest(mode=mode):
                self.assertNotIn(SECTION, proc.stdout + proc.stderr)

    def test_disabled_prints_no_section(self):
        self.write_config({"prompt_optimizer": {"enabled": False}})
        self.install_user_skill()
        for mode, proc in self.both_modes():
            with self.subTest(mode=mode):
                self.assertNotIn(SECTION, proc.stdout + proc.stderr)

    def test_enabled_without_a_skill_says_inert(self):
        self.write_config({"prompt_optimizer": {"enabled": True}})
        for mode, proc in self.both_modes():
            with self.subTest(mode=mode):
                out = proc.stdout + proc.stderr
                self.assertIn(SECTION, out)
                self.assertIn(INERT, out)
                self.assertNotIn(ON, out)

    def test_enabled_with_a_user_skill_names_the_skill(self):
        self.write_config({"prompt_optimizer": {"enabled": True}})
        self.install_user_skill()
        for mode, proc in self.both_modes():
            with self.subTest(mode=mode):
                out = proc.stdout + proc.stderr
                self.assertIn(SECTION, out)
                self.assertIn(ON, out)
                self.assertNotIn("inert", out)

    def test_inert_never_changes_the_exit_status(self):
        self.write_config({"prompt_optimizer": {"enabled": False}})
        disabled = (self.check().returncode, self.install().returncode)
        self.write_config({"prompt_optimizer": {"enabled": True}})
        enabled = (self.check().returncode, self.install().returncode)
        self.assertEqual(disabled, enabled)
        self.assertIn(INERT, self.check().stdout)


if __name__ == "__main__":
    unittest.main()
