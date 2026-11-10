"""Every test suite in the repository lives where a runner finds it.

The runners (`ci/local-checks.sh`, `./install.sh --check`) discover suites with
`find <roots> -path '*/tests/test_*.py'`. A suite outside those roots runs
nowhere and certifies nothing, and nothing announces it: root `tests/` once sat
unrun because no runner discovered it. The roots are read from the runners
themselves, so adding a root to a runner is the only way to make a new place
legal, and the two runners must agree on them.

Run: python3 lib/tests/test_suite_discovery.py
"""
import os
import re
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# `find tools lib skills -path '*/tests/test_*.py'` in local-checks.sh.
LOCAL_FIND_RE = re.compile(r"\bfind ((?:[\w.-]+ )+)-path '\*/tests/test_\*\.py'")
# `find "$REPO/tools" "$REPO/lib" ... -path '*/tests/test_*.py'` in install.sh.
INSTALL_FIND_RE = re.compile(r"\bfind ((?:\"\$REPO/[\w.-]+\" )+)-path '\*/tests/test_\*\.py'")
SUITE_RE = re.compile(r"(^|/)tests/test_[^/]*\.py$")


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def local_root_sets():
    """Each distinct root list local-checks.sh passes to a suite find."""
    return {tuple(m.split()) for m in LOCAL_FIND_RE.findall(read("ci/local-checks.sh"))}


def install_root_sets():
    return {tuple(re.findall(r"\$REPO/([\w.-]+)", m))
            for m in INSTALL_FIND_RE.findall(read("install.sh"))}


def undiscovered(paths, roots):
    """Suite paths no runner's find reaches: a suite must sit directly in a
    `tests/` dir (find's `*/tests/test_*.py`) under one of `roots`."""
    return [p for p in paths if SUITE_RE.search(p) and p.split("/")[0] not in roots]


def repo_files(root=REPO):
    """Tracked plus untracked-but-not-ignored, so an uncommitted suite is seen."""
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=True).stdout
    return [p for p in out.splitlines() if os.path.isfile(os.path.join(root, p))]


class TestRunnersAgreeOnRoots(unittest.TestCase):

    def test_local_checks_has_the_suite_and_ci_finds(self):
        sets = local_root_sets()
        self.assertIn(("ci",), sets, "the ci self-test find is gone or reshaped")
        self.assertEqual(len(sets), 2, f"expected the suite find and the ci find, got {sets}")

    def test_install_check_finds_the_same_roots_as_local_ci(self):
        local = [s for s in local_root_sets() if s != ("ci",)]
        install = install_root_sets()
        self.assertEqual(len(install), 1, f"install.sh suite finds: {install}")
        self.assertEqual(set(install.pop()), set(local[0]),
                         "install.sh --check and local CI search different roots, "
                         "so a suite can pass one and never run in the other")


class TestEverySuiteIsDiscovered(unittest.TestCase):

    def roots(self):
        return {r for s in local_root_sets() for r in s}

    def test_no_suite_in_the_repository_is_outside_the_roots(self):
        files = repo_files()
        self.assertTrue([p for p in files if SUITE_RE.search(p)],
                        "found no suites at all: the scan is broken, not clean")
        self.assertEqual(undiscovered(files, self.roots()), [],
                         "these suites run in no runner; move them under a root "
                         "the runners find, or add the root to BOTH runners")

    def test_a_root_level_suite_is_reported(self):
        """Negative control: the row-36 shape, and a suite in an unlisted dir."""
        roots = self.roots()
        self.assertEqual(
            undiscovered(["tests/test_x.py", "docs/tests/test_y.py",
                          "lib/tests/test_z.py", "lib/tests/fixtures/data.py"], roots),
            ["tests/test_x.py", "docs/tests/test_y.py"])

    def test_the_scan_sees_tracked_and_untracked_suites(self):
        """The scan reaches an uncommitted suite, which is where a misplaced one
        starts. Built in a throwaway repository: a probe in the real one would
        land in other suites' untracked-file scans while they run."""
        with tempfile.TemporaryDirectory() as root:
            def git(*args):
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                                "-c", "core.hooksPath=/dev/null",
                                "-c", "commit.gpgsign=false", *args],
                               cwd=root, check=True, capture_output=True)
            for rel in ("lib/tests/test_tracked.py", "tests/test_tracked_root.py",
                        "tests/test_untracked_root.py", "ignored/tests/test_i.py"):
                os.makedirs(os.path.join(root, os.path.dirname(rel)), exist_ok=True)
                with open(os.path.join(root, rel), "w", encoding="utf-8") as fh:
                    fh.write("x = 1\n")
            with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as fh:
                fh.write("ignored/\n")
            git("init", "-q")
            git("add", ".gitignore", "lib/tests/test_tracked.py", "tests/test_tracked_root.py")
            git("commit", "-q", "-m", "fixture")
            self.assertEqual(sorted(undiscovered(repo_files(root), {"lib"})),
                             ["tests/test_tracked_root.py", "tests/test_untracked_root.py"])

    def test_the_discovered_roots_are_the_real_ones(self):
        """Positive control: a parse that returned nothing would pass everything."""
        self.assertTrue({"tools", "lib", "skills", "scripts", "ci"} <= self.roots(),
                        self.roots())


if __name__ == "__main__":
    unittest.main()
