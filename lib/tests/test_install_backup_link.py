#!/usr/bin/env python3
"""install.sh must link claude-backup and list it in EXECUTABLE_NAMES.

test_install_links_inventory.py only checks the two lists agree, so removing
both registrations would pass it; this pins the registration itself.
"""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
TOOL = os.path.join(REPO_ROOT, "tools", "backup", "claude-backup")


class TestBackupRegistered(unittest.TestCase):
    def setUp(self):
        with open(INSTALL_SH, encoding="utf-8") as handle:
            self.text = handle.read()

    def test_link_line_present(self):
        pattern = (r'^link\s+"\$REPO/tools/backup/claude-backup"\s+'
                   r'"\$HOME/\.local/bin/claude-backup"\s*$')
        self.assertRegex(self.text, re.compile(pattern, re.M))

    def test_in_executable_names(self):
        match = re.search(r"^EXECUTABLE_NAMES=\((.*?)\)", self.text, re.M | re.S)
        self.assertIsNotNone(match)
        self.assertIn("claude-backup", match.group(1).replace("\\", " ").split())

    def test_tool_exists_and_is_executable(self):
        self.assertTrue(os.path.isfile(TOOL))
        self.assertTrue(os.access(TOOL, os.X_OK))


if __name__ == "__main__":
    unittest.main()
