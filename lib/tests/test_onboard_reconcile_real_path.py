#!/usr/bin/env python3
"""H4 F2: retiring a shadowing skill `mv`'d a tracked folder out of a repo,
because ~/.claude/skills was a symlink into a git checkout.

reconcile.md's real-path check must actually tell the cases apart, so this RUNS
the documented snippet rather than grepping the sentence around it: a skill
reached through a symlink into a repo, a skill folder that IS a repo's root, a
`~/` spelling, a path that does not exist, and the plain folder control.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
RECONCILE = os.path.join(REPO, "skills", "core", "onboard", "references", "reconcile.md")
SECTION = "## Then reconcile what collided"


def clean_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["HOME"] = home
    return env


def collision_section():
    with open(RECONCILE, encoding="utf-8") as handle:
        text = handle.read()
    start = text.index(SECTION)
    end = text.find("\n## ", start + len(SECTION))
    return text[start:end if end != -1 else len(text)]


class TestRealPathCheck(unittest.TestCase):

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="reconcile-real-path-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        self.skills = os.path.join(self.home, ".claude", "skills")

    def snippet(self):
        part = collision_section()
        blocks = [b for b in re.findall(r"```bash\n(.*?)```", part, re.S)
                  if "rev-parse" in b and "--show-toplevel" in b]
        self.assertEqual(len(blocks), 1, "reconcile.md needs exactly one real-path check")
        self.assertIn('"<path>"', blocks[0])
        flat = " ".join(part.split())
        self.assertIn("this is a repo change, not a cleanup: stop and ask", flat)
        self.assertIn("Only `not in a repo` means the steps above apply", flat)
        return blocks[0]

    def run_snippet(self, path):
        script = self.snippet().replace('"<path>"', '"$1"')
        return subprocess.run(["bash", "-c", script, "probe", path],
                              env=clean_env(self.home), capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=60)

    def git_init(self, path):
        subprocess.run(["git", "init", "-q", path], env=clean_env(self.home), check=True,
                       stdin=subprocess.DEVNULL)

    def test_a_skill_reached_through_a_symlink_into_a_repo_names_the_repo(self):
        dotfiles = os.path.join(self.tmp, "dotfiles")
        os.makedirs(os.path.join(dotfiles, "skills", "build"))
        self.git_init(dotfiles)
        os.makedirs(os.path.dirname(self.skills))
        os.symlink(os.path.join(dotfiles, "skills"), self.skills)
        done = self.run_snippet(os.path.join(self.skills, "build"))
        self.assertEqual(done.stdout.strip(), dotfiles, done.stderr)

    def test_a_skill_folder_that_is_a_repo_root_names_itself(self):
        """Codex Merge Review: probing only the parent missed a repo cloned
        straight into ~/.claude/skills/<name>."""
        build = os.path.join(self.skills, "build")
        os.makedirs(build)
        self.git_init(build)
        done = self.run_snippet(build)
        self.assertEqual(done.stdout.strip(), build, done.stderr)

    def test_a_home_relative_spelling_is_expanded(self):
        """Codex Merge Review: a quoted `~` is never expanded by the shell."""
        build = os.path.join(self.skills, "build")
        os.makedirs(build)
        self.git_init(build)
        done = self.run_snippet("~/.claude/skills/build")
        self.assertEqual(done.stdout.strip(), build, done.stderr)

    def test_a_missing_path_is_an_error_not_a_clean_answer(self):
        done = self.run_snippet(os.path.join(self.skills, "nope"))
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("not in a repo", done.stdout)
        self.assertIn("no such path", done.stderr)

    def test_a_plain_skill_folder_says_not_in_a_repo(self):
        """The control: the check can say "not in a repo"."""
        plain = os.path.join(self.skills, "build")
        os.makedirs(plain)
        done = self.run_snippet(plain)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "not in a repo")


if __name__ == "__main__":
    unittest.main()
