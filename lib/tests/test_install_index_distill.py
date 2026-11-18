"""install.sh must link claude-tool-rules and list it in EXECUTABLE_NAMES.

test_install_links_inventory.py derives its inventory from install.sh itself, so
it cannot see a tool whose link line and name were both deleted. This pins the
presence positively.
"""

import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")


def _text():
    with open(INSTALL_SH, encoding="utf-8") as fh:
        return fh.read()


def _assert_installed(case, tool, rel_source):
    text = _text()
    pattern = re.compile(
        r'^link\s+"\$REPO/%s"\s+"\$HOME/\.local/bin/%s"\s*$' % (re.escape(rel_source), re.escape(tool)),
        re.M)
    case.assertRegex(text, pattern)
    names = re.search(r"^EXECUTABLE_NAMES=\((.*?)\)", text, re.M | re.S)
    case.assertIsNotNone(names)
    case.assertIn(tool, names.group(1).replace("\\", " ").split())


class TestIndexDistillInstall(unittest.TestCase):
    def test_tool_rules_linked_and_listed(self):
        _assert_installed(self, "claude-tool-rules", "tools/tool-rules/claude-tool-rules")

    def test_index_distill_linked_and_listed(self):
        _assert_installed(self, "claude-index-distill",
                          "tools/index-distill/claude-index-distill")


if __name__ == "__main__":
    unittest.main()
