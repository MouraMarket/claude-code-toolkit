#!/usr/bin/env python3
"""Pins that every script's shebang resolves its interpreter through PATH."""

import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ALLOWED = (b"#!/usr/bin/env python3", b"#!/usr/bin/env bash")
SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def _shebangs():
    """Yield (relative path, first line) for every file that starts with `#!`."""
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".venv")]
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            try:
                with open(path, "rb") as handle:
                    first = handle.readline().rstrip(b"\r\n")
            except OSError:
                continue
            if first.startswith(b"#!"):
                yield os.path.relpath(path, REPO_ROOT), first


class TestShebangs(unittest.TestCase):
    def test_every_shebang_resolves_through_path(self):
        """Local CI puts the interpreter under test first on PATH so a child
        `python3` matches its parent suite. A fixed interpreter path in a
        shebang bypasses that, so a 3.13 sweep would silently exercise that
        tool on another interpreter."""
        offenders = [(path, line.decode("utf-8", "replace"))
                     for path, line in _shebangs() if line not in ALLOWED]
        self.assertEqual(
            offenders, [],
            "a script's shebang names a fixed interpreter instead of "
            "`#!/usr/bin/env python3` or `#!/usr/bin/env bash`, so the PATH "
            "shim that pins the interpreter under test cannot reach it.")

    def test_the_walk_found_scripts(self):
        seen = sum(1 for _ in _shebangs())
        self.assertGreaterEqual(seen, 200, "the walk saw almost no scripts, so the check proves nothing")


if __name__ == "__main__":
    unittest.main()
