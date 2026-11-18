#!/usr/bin/env python3
"""Lint: no shell script writes a literal `~` as a ${var/pattern/replacement}.

`${path/#$HOME/~}` prints `~/x` on macOS's bash 3.2, but bash 4.3+ (Ubuntu,
WSL, Git Bash) tilde-expands the replacement back to $HOME, so the output
shows the full path. Three onboarding self-tests failed on it
on WSL. The portable spelling is `${path/#$HOME/$TILDE}` with
`TILDE='~'`: an expanded variable is never tilde-expanded.

This macOS machine runs bash 3.2, where the bug is invisible to every
behavioural test, so this lint is the only check that can fail here.

Run: cd lib && python3 tests/test_tilde_display_lint.py
"""

import os
import re
import subprocess
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

# ${name/pat/~...}, ${name//pat/~...}, ${name/#pat/~...}, ${name/%pat/~...}
LITERAL_TILDE_REPLACEMENT = re.compile(r"\$\{\w+//?[#%]?[^/}]*/~")


def shell_files():
    out = subprocess.run(["git", "-C", REPO_ROOT, "ls-files", "-z"],
                         capture_output=True, check=True).stdout
    for rel in out.decode("utf-8").split("\0"):
        if not rel:
            continue
        path = os.path.join(REPO_ROOT, rel)
        if not os.path.isfile(path) or os.path.islink(path):
            continue
        if rel.endswith(".sh"):
            yield rel, path
            continue
        try:
            with open(path, "rb") as handle:
                first = handle.readline(200)
        except OSError:
            continue
        if first.startswith(b"#!") and (b"bash" in first or b"/sh" in first):
            yield rel, path


def findings():
    found = []
    for rel, path in shell_files():
        with open(path, encoding="utf-8", errors="replace") as handle:
            for lineno, line in enumerate(handle, 1):
                if LITERAL_TILDE_REPLACEMENT.search(line):
                    found.append("%s:%d: %s" % (rel, lineno, line.strip()))
    return found


class TestTildeDisplayLint(unittest.TestCase):
    def test_pattern_catches_the_bug_shape(self):
        # Positive control: a pattern that matches nothing passes for free.
        for bad in ('ok "${linkname/#$HOME/~}"', 'x=${d/$HOME/~}', 'y=${d//#$HOME/~/z}'):
            self.assertRegex(bad, LITERAL_TILDE_REPLACEMENT)
        for good in ('ok "${linkname/#$HOME/$TILDE}"', "TILDE='~'", 'cd ~/x', '${HOME}/~x'):
            self.assertNotRegex(good, LITERAL_TILDE_REPLACEMENT)

    def test_scan_reaches_the_installer(self):
        rels = {rel for rel, _ in shell_files()}
        self.assertIn("install.sh", rels)
        self.assertIn("scripts/setup-git.sh", rels)

    def test_no_literal_tilde_replacement(self):
        found = findings()
        self.assertEqual(found, [], "literal ~ in a ${var/pat/~} replacement expands "
                         "to $HOME on bash 4.3+; use $TILDE:\n  " + "\n  ".join(found))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    unittest.main()
