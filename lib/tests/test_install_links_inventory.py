#!/usr/bin/env python3
"""Provisioning assertions about install.sh's link inventory.

The profile plugins deliver hooks through `${CLAUDE_PLUGIN_ROOT}`, but the
executables themselves are still linked into `~/.local/bin` by `link` calls,
and the status line names one of them. Nothing else checks that these
inventories agree, and a drift installs a tool inert with every suite green.

Every plugin hook command resolving to an executable is pinned in
`test_plugin_hooks.py`; the status line's link is pinned here. No count of
links appears in this file: every count is derived from the `link` calls, so
a row that adds a tool cannot leave a typed number behind.
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")

# One link call: `link "$REPO/tools/..." "$HOME/.local/bin/<name>"`, leading its
# line. The separator spans a backslash continuation but not a bare newline,
# which ends the command. `${HOME}` braces are the same destination.
_ARGSEP = r'(?:[ \t]|\\\n)+'
_HOME_LOCAL_BIN = r'"\$\{?HOME\}?/\.local/bin/'
_LOCAL_BIN_LINK = re.compile(
    r'^[ \t]*(link)' + _ARGSEP + r'"\$REPO/tools/[^"]+"' + _ARGSEP
    + _HOME_LOCAL_BIN + r'([^"]+)"', re.M)

# Any call to a helper whose name ends in `link` that mentions a ~/.local/bin
# destination, at a line start or after `;`, `&`, `|`, `then`, `do`, `else`.
# Looser than the scan above on every axis that scan is strict about, so it
# can detect the scan narrowing. `_ARGSEP` after the name keeps out an
# assignment such as `x_link="$HOME/.local/bin/..."`.
_ANY_LINKISH_LOCAL_BIN = re.compile(
    r'(?:^|[;&|]|\bthen\b|\bdo\b|\belse\b)[ \t]*(\w*link)' + _ARGSEP
    + r'(?:[^\n]|\\\n)*?' + _HOME_LOCAL_BIN, re.M)

# Helpers that NAME a path without creating a link. Data, not regex: one of
# these naming ~/.local/bin is correct code, and teaching the strict scan its
# name could never clear the control, because that scan also requires a
# `"$REPO/tools/..."` argument this helper never has.
_NON_LINKING_HELPERS = {"note_managed_link"}


def install_sh_text():
    with open(INSTALL_SH, encoding="utf-8") as handle:
        return handle.read()


def linked_local_bin_executables(text):
    """Every executable name `install.sh` places in ~/.local/bin. Raises
    nothing: each caller carries its own emptiness check."""
    return {m.group(2) for m in _LOCAL_BIN_LINK.finditer(text)}


def executable_names(text):
    block = re.search(r"^EXECUTABLE_NAMES=\((.*?)\)\n", text, re.M | re.S)
    if block is None:
        return None
    return set(block.group(1).split()) - {"\\"}


class TestTheLinkScanSeesEveryLinkishCall(unittest.TestCase):
    """The scan's own positive control. Every guard here compares against
    `linked_local_bin_executables()`; if a link call takes a shape that scan
    does not match, its link becomes invisible to all of them at once and
    every one keeps passing."""

    def test_no_linkish_local_bin_call_is_missed_by_the_strict_scan(self):
        text = install_sh_text()
        loose = [name for name in _ANY_LINKISH_LOCAL_BIN.findall(text)
                 if name not in _NON_LINKING_HELPERS]
        strict = [m.group(1) for m in _LOCAL_BIN_LINK.finditer(text)]
        self.assertTrue(loose, "found no link calls at all to check")
        self.assertEqual(
            len(loose), len(strict),
            "a call to a `*link` helper names a ~/.local/bin destination that "
            "`_LOCAL_BIN_LINK` does not match, so every guard here is blind to "
            "it. Three causes: (1) a new helper name: teach `_LOCAL_BIN_LINK` "
            "the name; (2) a call shape the strict scan does not match, such "
            "as an `&&` or `; then` one-liner: write the call on its own line; "
            "(3) a helper that only NAMES a path: add it to "
            "`_NON_LINKING_HELPERS`.")
        self.assertEqual(
            set(), _NON_LINKING_HELPERS & set(strict),
            "a helper that DOES create ~/.local/bin links is listed in "
            "`_NON_LINKING_HELPERS`, so its links are excluded from the "
            "control that is supposed to see every one of them")


class TestExecutableNamesCoversEveryLink(unittest.TestCase):
    """`EXECUTABLE_NAMES` is hand-maintained and nothing reads it at runtime
    (the prunes walk the directories instead), so this test is the only thing
    keeping it honest. Do not delete it as dead-code hygiene on the strength
    of the array being unread: nothing else breaks when the array drifts,
    which is exactly why the check has to be a test."""

    def test_every_linked_executable_is_listed(self):
        text = install_sh_text()
        listed = executable_names(text)
        self.assertIsNotNone(
            listed, "EXECUTABLE_NAMES array not found: this guard cannot run, "
                    "and without this assertion it would pass by finding nothing")
        linked = linked_local_bin_executables(text)
        self.assertTrue(listed, "EXECUTABLE_NAMES parsed as empty")
        self.assertTrue(linked, "found no ~/.local/bin link calls to check")
        self.assertEqual(set(), linked - listed,
                         "linked into ~/.local/bin but missing from EXECUTABLE_NAMES")
        self.assertEqual(set(), listed - linked,
                         "in EXECUTABLE_NAMES but no longer linked")


class TestLocalCiIsLinked(unittest.TestCase):
    """`claude-local-ci` installs the same way every other executable does: a
    `link` call into `~/.local/bin` plus an entry in `EXECUTABLE_NAMES`.
    `TestExecutableNamesCoversEveryLink` only checks that the two inventories
    agree with EACH OTHER: it would pass just as well if both omitted this
    tool, so it cannot stand in for this test."""

    def test_local_ci_is_linked_and_listed(self):
        text = install_sh_text()
        self.assertRegex(
            text,
            r'link "\$REPO/tools/local-ci/claude-local-ci" +"\$HOME/\.local/bin/claude-local-ci"')
        self.assertIn("claude-local-ci", executable_names(text) or set())


class TestStatusLineIsActuallyLinked(unittest.TestCase):
    """A status line naming an executable the installer never links installs
    inert, and where the status line is the only source of budget data,
    inert is indistinguishable from a quiet window. `settings.host.json` holds
    the command; `install.sh` merges it into the machine's settings, so it
    must name something a `link` call provides."""

    def test_status_line_command_resolves_to_a_linked_executable(self):
        linked = linked_local_bin_executables(install_sh_text())
        with open(os.path.join(REPO_ROOT, "claude", "settings.host.json"),
                  encoding="utf-8") as handle:
            host = json.load(handle)
        command = host["statusLine"]["command"]
        self.assertIn("/.local/bin/", command)
        executable = command.split("/.local/bin/", 1)[1].split()[0]
        self.assertEqual("claude-usage-line", executable,
                         "statusLine no longer names claude-usage-line: this "
                         "test would pass vacuously on a different tool")
        self.assertIn(executable, linked,
                      "statusLine names an executable install.sh never links "
                      "into ~/.local/bin, so it installs inert")


if __name__ == "__main__":
    unittest.main()
