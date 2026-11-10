"""install.sh --check reports whether the toolkit policy loads.

The toolkit policy arrives through one `@`-import line in ~/.claude/CLAUDE.md. The
check is read-only: it warns, never changes the exit code, and says so when
the checkout carries no lib/team_policy.py.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from test_install_auto_memory import make_fixture_repo, run_install, seed_home  # noqa: E402

LIB = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, LIB)
import team_policy  # noqa: E402


class TestInstallCheckTeamPolicy(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.workspace = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.repo = make_fixture_repo(self.root)
        self.with_policy_files(self.repo)
        seed_home(self.home)
        self.config = {"workspace_root": self.workspace}
        with open(os.path.join(self.home, ".config", "claude-code-toolkit",
                               "config.json"), "w") as handle:
            json.dump(self.config, handle)
        self.config_path = os.path.join(self.root, "toolkit-config.json")
        self.write_config()

    def with_policy_files(self, repo):
        for name in ("team_policy.py", "claude_imports.py"):
            os.symlink(os.path.join(LIB, name), os.path.join(repo, "lib", name))
        os.makedirs(os.path.join(repo, "policy"))
        with open(os.path.join(repo, "policy", "CLAUDE.md"), "w") as handle:
            handle.write("# Claude Code toolkit policy\n")

    def write_config(self):
        with open(self.config_path, "w") as handle:
            json.dump(self.config, handle)

    def write_user_file(self, text):
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        with open(os.path.join(self.home, ".claude", "CLAUDE.md"), "w") as handle:
            handle.write(text)

    def check(self, repo=None, workspace_md="# Workspace\n"):
        with open(os.path.join(self.workspace, "CLAUDE.md"), "w") as handle:
            handle.write(workspace_md)
        proc = run_install(repo or self.repo, self.home, self.config_path, args=("--check",))
        section = proc.stdout[proc.stdout.index("Your CLAUDE.md"):]
        end = section.index("\n\n") if "\n\n" in section else len(section)
        return proc, section[:end]

    def good_import(self):
        return team_policy.import_line(self.repo) + "\n"

    def test_missing_user_file_warns_with_remedy(self):
        _, section = self.check()
        self.assertIn("toolkit policy: missing", section)
        self.assertIn('team_policy.py" ensure "%s"' % self.repo, section)

    def test_good_import_is_ok(self):
        self.write_user_file(self.good_import())
        _, section = self.check()
        self.assertIn("toolkit policy: ok", section)
        self.assertNotIn("toolkit policy: missing", section)

    def test_import_to_nonexistent_policy_is_dangling(self):
        self.write_user_file("@%s/nowhere/policy/CLAUDE.md\n" % self.root)
        _, section = self.check()
        self.assertIn("toolkit policy: dangling", section)

    def test_legacy_copy_in_workspace_claude_md(self):
        self.write_user_file(self.good_import())
        _, section = self.check(workspace_md=(
            "## Gate at decisions, not execution\n"
            "## Usage budget — stop at a boundary, not mid-task\n"))
        self.assertIn("toolkit policy: legacy-copy", section)

    def test_opted_out(self):
        self.config["team_policy"] = {"import": False}
        self.write_config()
        _, section = self.check()
        self.assertIn("toolkit policy: opted-out", section)

    def test_fixture_without_team_policy_says_not_checked(self):
        bare = make_fixture_repo(self.root, name="bare")
        _, section = self.check(bare)
        self.assertIn("toolkit policy: not checked", section)

    def test_exit_code_unchanged_by_policy_state(self):
        missing, _ = self.check()
        self.write_user_file(self.good_import())
        ok, _ = self.check()
        self.assertEqual(missing.returncode, ok.returncode)


if __name__ == "__main__":
    unittest.main()
