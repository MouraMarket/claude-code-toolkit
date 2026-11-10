#!/usr/bin/env python3
"""Pins `harness-check`'s wiring: the hook is registered where the maintainer
plugin's manifest says, linked by `install.sh`, and off by default.

The tool's logic and its own tests live under `tools/harness-check/`. What
they cannot check is whether the tool is reachable: a hook registered in a
plugin's `hooks/hooks.json` that `install.sh` never links installs inert,
because Claude Code silently skips a hook it cannot execute, so a typo here is
indistinguishable from a hook with nothing to say. This mirrors
`TestExecutableNamesCoversEveryLink` in `test_install_links_inventory.py`,
reading the registration through `lib/hook_manifest.py`, scoped to this one
tool.
"""

import json
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
CONFIG_EXAMPLE_JSON = os.path.join(REPO_ROOT, "config.example.json")

sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
import hook_manifest  # noqa: E402

HOOK_RELPATH = "tools/harness-check/claude-harness-check"
HOOK_ARGS = ["--hook"]


def _harness_rows(manifest):
    """Every plugin-manifest handler that is the harness-check `--hook` call."""
    return [row for row in hook_manifest.registrations(manifest)
            if row["exe"] == "claude-harness-check" and row["args"] == HOOK_ARGS]


class TestHarnessCheckHookRegistration(unittest.TestCase):
    """Exactly one `--hook` entry under each of PostToolBatch and
    UserPromptSubmit, with the expected command and a 5-second timeout, and
    none anywhere else."""

    def setUp(self):
        self.manifest = hook_manifest.load_plugin(REPO_ROOT, "maintainer")

    def test_registered_once_under_each_expected_event_with_a_5s_timeout(self):
        by_event = {}
        for row in _harness_rows(self.manifest):
            self.assertEqual(HOOK_RELPATH, row["relpath"])
            by_event.setdefault(row["event"], []).append(row)

        self.assertEqual(
            {"PostToolBatch", "UserPromptSubmit"}, set(by_event.keys()),
            "expected the hook registered under exactly PostToolBatch and "
            "UserPromptSubmit, and nowhere else")

        for event, hooks in by_event.items():
            self.assertEqual(
                1, len(hooks),
                "expected exactly one {0} entry, found {1}".format(
                    event, len(hooks)))
            self.assertEqual(5, hooks[0].get("timeout"),
                             "{0} entry's timeout is not 5".format(event))

    def test_registered_in_the_matcherless_group_appended_at_the_end(self):
        # The hook joins the existing matcher-less group rather than adding a
        # group of its own: the entry's group carries no "matcher" key, and the hook command sits
        # last in that group's hooks list.
        for event in ("PostToolBatch", "UserPromptSubmit"):
            groups = [g for g in self.manifest[event]
                      if "matcher" not in g]
            self.assertEqual(
                1, len(groups),
                "expected exactly one matcher-less group under {0}".format(
                    event))
            commands = [hook_manifest.parse_command(h.get("command"))
                        for h in groups[0]["hooks"]]
            self.assertEqual(
                (HOOK_RELPATH, HOOK_ARGS), commands[-1],
                "expected the harness-check hook last in {0}'s "
                "matcher-less group, found order {1}".format(event, commands))


class TestHarnessCheckLinkedIntoInstallSh(unittest.TestCase):
    """`EXECUTABLE_NAMES` and a `link` call both name the tool — the two
    halves `test_install_links_inventory.py` checks for every link,
    scoped to this one addition."""

    def setUp(self):
        with open(INSTALL_SH) as handle:
            self.text = handle.read()

    def test_executable_names_lists_it(self):
        block = re.search(r"EXECUTABLE_NAMES=\((.*?)\)\n", self.text, re.S).group(1)
        listed = set(block.split()) - {"\\"}
        self.assertIn("claude-harness-check", listed)

    def test_a_link_call_targets_its_executable(self):
        linked = dict(re.findall(
            r'^link "(\$REPO/tools/[^"]+)" +"\$HOME/\.local/bin/([^"]+)"',
            self.text, re.M))
        self.assertIn("claude-harness-check", linked.values())
        target = [t for t, n in linked.items() if n == "claude-harness-check"][0]
        self.assertEqual(
            "$REPO/tools/harness-check/claude-harness-check", target)


class TestHarnessCheckConfigDefault(unittest.TestCase):
    """`harness_check.enabled` ships false, because a notice that fires
    unasked is noise, and it is the config `--hook` mode reads before touching any of its own state."""

    def test_enabled_defaults_to_false_in_the_example_config(self):
        with open(CONFIG_EXAMPLE_JSON) as handle:
            config = json.load(handle)
        self.assertIn("harness_check", config,
                     "config.example.json has no harness_check section")
        self.assertIs(config["harness_check"].get("enabled"), False)


if __name__ == "__main__":
    unittest.main()
