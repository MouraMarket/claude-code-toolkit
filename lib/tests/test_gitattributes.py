#!/usr/bin/env python3
"""A Windows clone must check every script out with LF endings.

git's Windows default is core.autocrlf=true, which rewrites text files to CRLF
on checkout. A CRLF shebang (`#!/usr/bin/env bash\\r`) fails with "bad
interpreter", so without `.gitattributes` every tool in this repo is broken on
a Windows or WSL machine and nothing on a Mac would notice.

Two guards:
  * TestEveryTrackedFileResolvesToLf -- asks git, not a grep of the file, what
    eol each tracked path gets. Catches the rule being removed, narrowed, or
    overridden by a later line.
  * TestTheRuleSurvivesAWindowsCheckout -- a fixture repo cloned under
    core.autocrlf=true, with a control proving the same clone WITHOUT the rule
    comes back CRLF. Without the control, a fixture that never produced CRLF
    would pass for free.
"""
import os
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ATTRIBUTES = os.path.join(REPO, ".gitattributes")

# Hermetic git: no global or system config, so a developer's own autocrlf or
# attributes file cannot decide the result.
GIT_ENV = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
for leaked in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
    GIT_ENV.pop(leaked, None)


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args), env=GIT_ENV,
                          capture_output=True, text=True, check=True).stdout


class TestEveryTrackedFileResolvesToLf(unittest.TestCase):
    def test_every_tracked_file_resolves_to_lf(self):
        if not os.path.isdir(os.path.join(REPO, ".git")) and \
                not os.path.isfile(os.path.join(REPO, ".git")):
            self.skipTest("not a git checkout")
        paths = git(REPO, "ls-files").splitlines()
        self.assertGreater(len(paths), 0)
        out = subprocess.run(["git", "-C", REPO, "check-attr", "--stdin", "eol"],
                             input="\n".join(paths) + "\n", env=GIT_ENV,
                             capture_output=True, text=True, check=True).stdout
        not_lf = [line for line in out.splitlines() if not line.endswith(": eol: lf")]
        self.assertEqual(not_lf, [], "tracked files that would not check out LF")


class TestTheRuleSurvivesAWindowsCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _clone_under_autocrlf(self, with_rule):
        seed = os.path.join(self.tmp, "seed-%s" % with_rule)
        os.makedirs(seed)
        git(seed, "init", "-q", "-b", "main")
        with open(os.path.join(seed, "tool.sh"), "w", newline="\n") as f:
            f.write("#!/usr/bin/env bash\necho ok\n")
        if with_rule:
            shutil.copy(ATTRIBUTES, os.path.join(seed, ".gitattributes"))
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "seed")
        clone = os.path.join(self.tmp, "clone-%s" % with_rule)
        git(self.tmp, "-c", "core.autocrlf=true", "clone", "-q", seed, clone)
        with open(os.path.join(clone, "tool.sh"), "rb") as f:
            return f.read()

    def test_control_without_the_rule_the_clone_is_crlf(self):
        self.assertIn(b"\r\n", self._clone_under_autocrlf(with_rule=False))

    def test_with_the_rule_the_clone_is_lf(self):
        self.assertNotIn(b"\r", self._clone_under_autocrlf(with_rule=True))


if __name__ == "__main__":
    unittest.main()
