#!/usr/bin/env python3
"""lib/checkout_fingerprint.py: the hash /onboard compares before skipping the
suites (row 84). It must change exactly when something a suite could see
changes, and run through the real script, the way the skill runs it."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(REPO, "lib", "checkout_fingerprint.py")


class TestCheckoutFingerprint(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.home = os.path.join(self.root, "home")
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.home)
        os.makedirs(os.path.join(self.repo, "tools", "demo", "tests"))
        self.addCleanup(shutil.rmtree, self.root)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(HOME=self.home, USERPROFILE=self.home,
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.path.join(self.home, ".gitconfig"))
        self.git("init", "-q")
        self.write(".gitignore", "__pycache__/\n*.pyc\n")
        self.write("tools/demo/tests/test_demo.py", "print('one')\n")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.invalid",
                 "commit", "-qm", "seed")

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo] + list(args), env=self.env,
                       check=True, capture_output=True, stdin=subprocess.DEVNULL)

    def write(self, rel, text):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def run_script(self, *args):
        return subprocess.run([sys.executable, SCRIPT] + list(args), env=self.env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def fingerprint(self):
        done = self.run_script(self.repo)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertRegex(done.stdout.strip(), r"^[0-9a-f]{40,64}$")
        return done.stdout.strip()

    def test_an_unchanged_checkout_gives_the_same_hash(self):
        self.assertEqual(self.fingerprint(), self.fingerprint())

    def test_two_different_edits_to_one_file_give_different_hashes(self):
        """Both edits print ` M tools/demo/tests/test_demo.py` in git status;
        the Code Review repro of the first draft's record."""
        clean = self.fingerprint()
        self.write("tools/demo/tests/test_demo.py", "print('two')\n")
        first = self.fingerprint()
        self.write("tools/demo/tests/test_demo.py", "print('three')\n")
        second = self.fingerprint()
        self.assertEqual(len({clean, first, second}), 3)

    def test_a_new_untracked_suite_changes_the_hash_despite_status_config(self):
        before = self.fingerprint()
        self.git("config", "status.showUntrackedFiles", "no")
        self.write("tools/new/tests/test_new.py", "pass\n")
        self.assertNotEqual(before, self.fingerprint())

    def test_a_global_excludes_file_cannot_hide_a_new_suite(self):
        excludes = os.path.join(self.home, "ignore")
        with open(excludes, "w", encoding="utf-8") as handle:
            handle.write("test_*.py\n")
        with open(self.env["GIT_CONFIG_GLOBAL"], "w", encoding="utf-8") as handle:
            handle.write("[core]\n\texcludesFile = %s\n" % excludes)
        before = self.fingerprint()
        self.write("tools/new/tests/test_new.py", "pass\n")
        self.assertNotEqual(before, self.fingerprint())

    def test_files_the_repo_ignores_do_not_change_the_hash(self):
        """The preview's own byproducts must not break the match it is for."""
        before = self.fingerprint()
        self.write("tools/demo/tests/__pycache__/test_demo.cpython-39.pyc", "x")
        self.assertEqual(before, self.fingerprint())

    def test_the_real_index_is_never_touched(self):
        self.write("tools/new/tests/test_new.py", "pass\n")
        index = os.path.join(self.repo, ".git", "index")
        with open(index, "rb") as handle:
            before = handle.read()
        self.fingerprint()
        with open(index, "rb") as handle:
            self.assertEqual(before, handle.read())

    def test_a_subfolder_argument_still_hashes_the_whole_tree(self):
        whole = self.fingerprint()
        done = self.run_script(os.path.join(self.repo, "tools"))
        self.assertEqual(done.stdout.strip(), whole, done.stderr)

    def test_not_a_checkout_exits_1_with_no_hash(self):
        done = self.run_script(self.home)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, "")
        self.assertIn("no fingerprint", done.stderr)


if __name__ == "__main__":
    unittest.main()
