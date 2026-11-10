#!/usr/bin/env python3
"""install.sh exits 1 when a REQUIRED component fails.

A required component is a link whose source is missing from the repo, a link not
in place under `--check`, a relocated CLAUDE_CONFIG_DIR, or an unparseable
config.json. Each used to print a warning and exit 0, so "install succeeded"
said nothing about whether the install worked. These drive the REAL install.sh
against the shared fixture and `claude` shim. The base fixture is a healthy one:
every link target exists (`add_link_targets`), every link is installed
(`install_links`), the plugin is seeded and protected. Each case then breaks
exactly one thing, and the positive controls pin what must stay a warning.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import install_fixture as fx  # noqa: E402
import install_record  # noqa: E402
from test_install_check_fails import CheckVerdictTestCase
from test_install_plugin import git

HEADER = "Required components failed:"
# A `~/.local/bin` link whose source no hook manifest names, so removing it
# fails only the link source and not a registered hook.
MISSING_SOURCE = os.path.join("tools", "wrap-up", "claude-wrap-up-survey")
MISSING = "link source missing from the repo: " + MISSING_SOURCE
NOT_IN_PLACE = "1 link(s) not in place"
CONFIG_DIR = "CLAUDE_CONFIG_DIR points away from ~/.claude"
BAD_JSON = "config.json is not valid JSON"


class RequiredFailuresTestCase(CheckVerdictTestCase):

    def run_mode(self, args, extra_env=None):
        env = {"CLAUDE_TOOLKIT_MANAGED_DIR": self.managed}
        env.update(extra_env or {})
        return self.run_install(list(args), extra_env=env)

    def out(self, proc):
        return proc.stdout + proc.stderr

    def assertFailed(self, proc, *entries):
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn(HEADER, proc.stdout)
        for entry in entries:
            self.assertIn(entry, proc.stdout)

    def assertClean(self, proc):
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertNotIn(HEADER, proc.stdout)

    def config_path(self):
        return os.path.join(self.home, ".config", "claude-code-toolkit", "config.json")


class TestMissingSource(RequiredFailuresTestCase):

    def test_healthy_fixture_apply_and_check_exit_0(self):
        # The control for every case below: nothing is broken yet.
        self.assertClean(self.run_mode([]))
        self.assertClean(self.run_mode(["--check"]))

    def test_deleted_target_fails_apply_and_check(self):
        os.remove(os.path.join(self.repo, MISSING_SOURCE))
        self.assertFailed(self.run_mode([]), MISSING)
        self.assertFailed(self.run_mode(["--check"]), MISSING)


class TestLinkNotInPlace(RequiredFailuresTestCase):

    def test_absent_link_fails_check_and_apply_repairs_it(self):
        self.assertClean(self.run_mode([]))
        # Stand-in for "a pull added a tool": a link the checkout has a target
        # for, with nothing installed at its destination.
        os.remove(os.path.join(self.home, ".local", "bin", "claude-model-guard"))
        self.assertFailed(self.run_mode(["--check"]), NOT_IN_PLACE)
        self.assertClean(self.run_mode([]))
        self.assertClean(self.run_mode(["--check"]))

    def test_apply_never_fails_for_an_absent_link(self):
        os.remove(os.path.join(self.home, ".local", "bin", "claude-model-guard"))
        proc = self.run_mode([])
        self.assertClean(proc)
        self.assertTrue(os.path.islink(
            os.path.join(self.home, ".local", "bin", "claude-model-guard")))


class TestRelocatedConfigDir(RequiredFailuresTestCase):

    def test_relocated_config_dir_fails_apply_and_check(self):
        elsewhere = os.path.join(self.root, "relocated")
        os.makedirs(elsewhere)
        env = {"CLAUDE_CONFIG_DIR": elsewhere}
        # An apply is refused up front; `--check` reports it as a required failure.
        proc = self.run_mode([], env)
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn("refusing to install: CLAUDE_CONFIG_DIR", proc.stderr)
        self.assertFailed(self.run_mode(["--check"], env), CONFIG_DIR)

    def test_required_failure_does_not_stamp_the_install_commit(self):
        record_file = install_record.record_path(self.home)
        self.assertClean(self.run_mode([]))
        first = install_record.load(record_file)["commit"]
        with open(os.path.join(self.repo, "later.txt"), "w") as handle:
            handle.write("later\n")
        git("add", "later.txt", cwd=self.repo)
        git("-c", "user.email=t@example.com", "-c", "user.name=Test",
            "commit", "-q", "-m", "later", cwd=self.repo)
        head = git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        self.assertNotEqual(first, head)
        elsewhere = os.path.join(self.root, "relocated")
        os.makedirs(elsewhere)
        proc = self.run_mode([], {"CLAUDE_CONFIG_DIR": elsewhere})
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn("refusing to install: CLAUDE_CONFIG_DIR", proc.stderr)
        self.assertEqual(install_record.load(record_file)["commit"], first)
        # Positive control: a healthy run stamps the new HEAD.
        self.assertClean(self.run_mode([]))
        self.assertEqual(install_record.load(record_file)["commit"], head)


class TestInvalidConfig(RequiredFailuresTestCase):

    def test_unparseable_config_fails_check(self):
        with open(self.config_path(), "w") as handle:
            handle.write("{not json")
        self.assertFailed(self.run_mode(["--check"]), BAD_JSON)

    def test_a_setup_that_repairs_the_config_clears_the_failure(self):
        # The real setup is interactive; a stub stands in for a successful repair
        # (writes valid JSON, exits 0). Judged after setup, the failure must go.
        with open(self.config_path(), "w") as handle:
            handle.write("{not json")
        stub = os.path.join(self.repo, "lib", "setup_config.py")
        with open(stub, "w") as handle:
            # Import-safe like the real module: install.sh's preflight
            # imports it before reading any existing config.
            handle.write("import json, sys\n"
                         "if __name__ == '__main__':\n"
                         "    json.dump({'workspace_root': '/tmp'}, open(sys.argv[1], 'w'))\n")
        # assertClean, not assertNotIn(BAD_JSON): the pre-setup WARNING line
        # carries the same words and is printed either way.
        self.assertClean(self.run_mode(["--setup"]))

    def test_a_setup_that_leaves_it_broken_still_fails(self):
        # Positive control for the test above: the stub exits 0 but writes nothing.
        with open(self.config_path(), "w") as handle:
            handle.write("{not json")
        stub = os.path.join(self.repo, "lib", "setup_config.py")
        with open(stub, "w") as handle:
            handle.write("import sys\nif __name__ == '__main__':\n    sys.exit(0)\n")
        self.assertFailed(self.run_mode(["--setup"]), BAD_JSON)


class TestSetupKeepsAnExistingConfig(RequiredFailuresTestCase):

    def test_setup_without_a_tty_leaves_a_valid_config_alone(self):
        # The real setup_config.py runs here. `--setup` passes --replace, so
        # with no TTY it exits 2 (the unchanged no-TTY path), not 3 ("setup
        # aborted"): dropping --replace from install.sh turns this red.
        original = b'{"owner_name": "Kept"}\n'
        with open(self.config_path(), "wb") as handle:
            handle.write(original)
        proc = self.run_mode(["--setup"])
        self.assertNotIn("setup aborted", self.out(proc))
        self.assertClean(proc)
        with open(self.config_path(), "rb") as handle:
            self.assertEqual(original, handle.read())


class TestStillWarningsOnly(RequiredFailuresTestCase):
    """Positive controls: each of these must keep exiting 0."""

    def test_a_real_file_where_a_link_belongs(self):
        dest = os.path.join(self.home, ".local", "bin", "claude-secret-scan")
        os.remove(dest)
        with open(dest, "w") as handle:
            handle.write("my own build command\n")
        proc = self.run_mode(["--check"])
        self.assertClean(proc)
        self.assertIn("exists and is a real file", proc.stdout)

    def test_config_without_workspace_root(self):
        with open(self.config_path(), "w") as handle:
            handle.write("{}\n")
        proc = self.run_mode(["--check"])
        self.assertClean(proc)
        self.assertIn("has no workspace_root", proc.stdout)

    def test_local_bin_not_on_path(self):
        path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                               if p != os.path.join(self.home, ".local", "bin"))
        proc = self.run_install(["--check"], extra_env={
            "CLAUDE_TOOLKIT_MANAGED_DIR": self.managed}, path=self.bin_dir + os.pathsep + path)
        self.assertClean(proc)

    def test_config_dir_symlinked_to_home_claude(self):
        link = os.path.join(self.root, "claude-link")
        os.symlink(os.path.join(self.home, ".claude"), link)
        self.assertClean(self.run_mode(["--check"], {"CLAUDE_CONFIG_DIR": link}))


class TestAllAtOnce(RequiredFailuresTestCase):

    def test_three_failures_are_listed_in_one_run(self):
        os.remove(os.path.join(self.repo, MISSING_SOURCE))
        elsewhere = os.path.join(self.root, "relocated")
        os.makedirs(elsewhere)
        with open(self.config_path(), "w") as handle:
            handle.write("{not json")
        proc = self.run_mode(["--check"], {"CLAUDE_CONFIG_DIR": elsewhere})
        self.assertFailed(proc, MISSING, CONFIG_DIR, BAD_JSON)


class TestFromALinkedWorktree(RequiredFailuresTestCase):

    def test_check_there_notes_the_worktree_and_does_not_count_absent_links(self):
        wt = os.path.join(self.root, "fixture-wt")
        git("worktree", "add", "-q", wt, "-b", "feature", cwd=self.repo)
        # Every link was installed from the primary, so from the worktree each
        # one reads as absent. That is the noise the exemption exists for.
        fx.seed_installed(self.state, wt)
        self.repo = wt
        proc = self.run_mode(["--check"])
        self.assertNotIn("link(s) not in place", proc.stdout)
        self.assertIn("LINKED WORKTREE", proc.stdout)
        self.assertNotIn(HEADER, proc.stdout, self.out(proc))
        self.assertEqual(0, proc.returncode, self.out(proc))


class TestOtherModesUnchanged(RequiredFailuresTestCase):

    def test_links_only_on_an_installed_fixture_exits_0(self):
        proc = self.run_mode(["--links-only"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertNotIn(HEADER, proc.stdout)

    def test_manifest_prints_no_required_block_and_exits_0(self):
        os.remove(os.path.join(self.repo, MISSING_SOURCE))
        proc = self.run_mode(["--manifest"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertNotIn(HEADER, proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
