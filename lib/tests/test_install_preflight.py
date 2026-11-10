#!/usr/bin/env python3
"""install.sh's refusals before it changes anything, and its Config section.

- A non-macOS host is refused before anything is read or written.
- A config that declines an always-on core guard stops a real run before any
  change; `--check` reports it as a required failure. A config the check
  cannot read the guards for (lib/setup_config.py missing) fails closed.
- The Config health section prints `setup_config.py --report`, and a report
  that crashes is shown with its exit code, never read as "nothing to report".
- The no-TTY first run says the copied example sets no workspace root.
- `--yes` outside `--uninstall --yes` is an unknown option, not a no-op, and
  a mode given extra arguments is refused before anything runs.

"Changed nothing" is checked on the whole sandbox HOME (every path, link
target and byte) and on the `claude` shim's call log, not on one file.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import install_fixture as fx
from test_install_permissions_opt_out import make_fixture_repo, seed_home

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# macOS system Python writes bytecode under $HOME/Library/Caches, so with HOME
# redirected any python3 the installer runs creates it in the sandbox. Python's
# write, not the installer's: without this, three refusal tests fail from a
# plain terminal and pass only under PYTHONDONTWRITEBYTECODE=1.
PYCACHE = os.path.join("Library", "Caches", "com.apple.python")
PYCACHE_PARENTS = {"Library", os.path.join("Library", "Caches")}


def snapshot(root):
    """{relative path: ("link", target) | ("file", bytes) | ("dir", None)}."""
    found = {}
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            path = os.path.join(current, name)
            rel = os.path.relpath(path, root)
            if (rel in PYCACHE_PARENTS or rel == PYCACHE
                    or rel.startswith(PYCACHE + os.sep)):
                continue
            if os.path.islink(path):
                found[rel] = ("link", os.readlink(path))
            elif os.path.isdir(path):
                found[rel] = ("dir", None)
            else:
                with open(path, "rb") as handle:
                    found[rel] = ("file", handle.read())
    return found


class PreflightTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.repo = make_fixture_repo(self.root)
        # `--report` reads the example as its schema.
        shutil.copyfile(os.path.join(REPO_ROOT, "config.example.json"),
                        os.path.join(self.repo, "config.example.json"))
        self.home = os.path.join(self.root, "home")
        self.workspace = os.path.join(self.root, "workspace")
        os.makedirs(self.workspace)
        self.fake_bin = os.path.join(self.root, "fake-bin")
        os.makedirs(self.fake_bin)
        self.set_uname("Darwin")

    def set_uname(self, system):
        path = os.path.join(self.fake_bin, "uname")
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\necho %s\n" % system)
        os.chmod(path, 0o755)

    def seed(self, config):
        seed_home(self.home, config)

    def run_install(self, *args):
        env = dict(os.environ)
        env["HOME"] = self.home
        env.pop("CLAUDE_TOOLKIT_CONFIG", None)
        env.pop("ALLOW_WORKTREE_INSTALL", None)
        env = fx.shim_env_for_home(env, self.home)
        # The fake uname goes first, ahead of the claude shim.
        env["PATH"] = self.fake_bin + os.pathsep + env["PATH"]
        return subprocess.run([os.path.join(self.repo, "install.sh")] + list(args),
                              cwd=self.repo, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)

    def shim_calls(self):
        return fx.shim_log(fx.shim_for_home(self.home)[1])

    def assertChangedNothing(self, before, proc):
        self.assertEqual(snapshot(self.home), before, proc.stdout + proc.stderr)
        self.assertEqual(self.shim_calls(), [], "the claude CLI was called")


class TestNonMacHostIsRefused(PreflightTestCase):

    def test_linux_apply_refuses_and_changes_nothing(self):
        self.seed({"workspace_root": self.workspace})
        before = snapshot(self.home)
        self.set_uname("Linux")
        proc = self.run_install()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("supports macOS only", proc.stderr)
        self.assertIn("'Linux'", proc.stderr)
        self.assertChangedNothing(before, proc)

    def test_check_is_refused_too(self):
        self.seed({"workspace_root": self.workspace})
        self.set_uname("Linux")
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("supports macOS only", proc.stderr)
        self.assertNotIn("Directories", proc.stdout)

    def test_refused_before_the_worktree_helper_is_read(self):
        """Before ANY read: even a checkout missing its worktree helper (which
        otherwise fails closed with its own message) gets the platform answer."""
        os.remove(os.path.join(self.repo, "lib", "worktree_detect.sh"))
        self.set_uname("Linux")
        proc = self.run_install()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("supports macOS only", proc.stderr)
        self.assertNotIn("worktree_detect.sh", proc.stderr)

    def test_darwin_proceeds(self):
        """Positive control: the same fixture on Darwin gets past the check."""
        self.seed({"workspace_root": self.workspace})
        proc = self.run_install("--check")
        self.assertNotIn("supports macOS only", proc.stderr)
        self.assertIn("Directories", proc.stdout)


class TestDeclinedCoreStopsBeforeAnyChange(PreflightTestCase):
    DECLINED = {"danger_guard": {"installed": False}}

    def config(self, **extra):
        config = {"workspace_root": self.workspace}
        config.update(extra)
        return config

    def test_apply_refuses_names_the_guard_and_changes_nothing(self):
        self.seed(self.config(**self.DECLINED))
        before = snapshot(self.home)
        proc = self.run_install()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("declines guards this toolkit's core always installs", proc.stderr)
        self.assertIn("  - danger_guard", proc.stderr)
        self.assertIn("--update --accept-core", proc.stderr)
        self.assertChangedNothing(before, proc)

    def test_setup_refuses_too(self):
        self.seed(self.config(**self.DECLINED))
        before = snapshot(self.home)
        proc = self.run_install("--setup")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("  - danger_guard", proc.stderr)
        self.assertChangedNothing(before, proc)

    def test_every_declined_core_guard_is_named(self):
        self.seed(self.config(danger_guard={"installed": False},
                              secret_scan={"installed": False}))
        proc = self.run_install()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("  - danger_guard", proc.stderr)
        self.assertIn("  - secret_scan", proc.stderr)

    def test_check_reports_it_as_a_required_failure(self):
        self.seed(self.config(**self.DECLINED))
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("refusing to install", proc.stderr)
        self.assertIn("config declines danger_guard, which core always installs", proc.stdout)
        health = proc.stdout[proc.stdout.index("Config health"):]
        self.assertIn("config declines danger_guard", health.split("\n\n")[0])

    def test_accepted_or_undecided_guards_do_not_stop_it(self):
        """Positive control: only an explicit `false` is a refusal."""
        for value in ({"installed": True}, {}, {"installed": None}):
            with self.subTest(value=value):
                shutil.rmtree(self.home, ignore_errors=True)
                self.seed(self.config(danger_guard=value))
                proc = self.run_install()
                self.assertNotIn("declines guards", proc.stderr)
                self.assertIn("Directories", proc.stdout)

    def test_a_config_it_cannot_check_fails_closed(self):
        os.remove(os.path.join(self.repo, "lib", "setup_config.py"))
        self.seed(self.config())
        before = snapshot(self.home)
        proc = self.run_install()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("could not check", proc.stderr)
        self.assertChangedNothing(before, proc)

    def test_an_unparseable_config_without_the_helper_fails_closed_too(self):
        """The double fault: a broken config must not skip the helper check."""
        os.remove(os.path.join(self.repo, "lib", "setup_config.py"))
        self.seed(self.config())
        with open(os.path.join(self.home, ".config", "claude-code-toolkit",
                               "config.json"), "w") as handle:
            handle.write("{broken")
        before = snapshot(self.home)
        proc = self.run_install()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("could not check", proc.stderr)
        self.assertChangedNothing(before, proc)

    def test_no_config_means_nothing_to_check(self):
        """No config yet: the preflight has nothing to read, so the missing
        module is not its business (first-time setup is)."""
        os.remove(os.path.join(self.repo, "lib", "setup_config.py"))
        os.makedirs(self.home)
        proc = self.run_install("--check")
        self.assertNotIn("could not check", proc.stderr)
        self.assertIn("Directories", proc.stdout)


class TestConfigHealth(PreflightTestCase):

    def health(self, proc):
        section = proc.stdout[proc.stdout.index("Config health"):]
        return section.split("\n\n")[0]

    def test_a_clean_config_says_so(self):
        # An explicit canonical_root (null included) is a decision, so the
        # report has nothing to say about an inferred one.
        self.seed({"workspace_root": self.workspace,
                   "worktree_guard": {"canonical_root": None}})
        proc = self.run_install("--check")
        self.assertIn("nothing declined, retired or unread", self.health(proc))

    def test_a_report_finding_is_printed(self):
        self.seed({"workspace_root": self.workspace, "no_such_section": {"x": 1}})
        proc = self.run_install("--check")
        health = self.health(proc)
        self.assertIn("config section no_such_section is not read by any tool", health)
        self.assertNotIn("nothing declined", health)

    def test_a_crashing_report_is_shown_with_its_exit_code(self):
        stub = os.path.join(self.repo, "lib", "setup_config.py")
        os.remove(stub)
        with open(stub, "w") as handle:
            handle.write("import sys\n"
                         "def core_conflicts(config):\n    return []\n"
                         "if __name__ == '__main__':\n    sys.exit(3)\n")
        self.seed({"workspace_root": self.workspace})
        proc = self.run_install("--check")
        health = self.health(proc)
        self.assertIn("config report failed (exit 3)", health)
        self.assertNotIn("nothing declined", health)


class TestToolingDigestHealth(PreflightTestCase):
    """Config health asks digest_lib.missing_source, the digest hook's own
    question, whether an enabled digest has a map to read."""

    def setUp(self):
        super().setUp()
        # The fixture already holds a stub executable there (a link target);
        # the check imports the real module beside it.
        os.symlink(os.path.join(REPO_ROOT, "tools", "tooling-digest", "digest_lib.py"),
                   os.path.join(self.repo, "tools", "tooling-digest", "digest_lib.py"))

    def health(self, config):
        config = dict(config, worktree_guard={"canonical_root": None})
        self.seed(config)
        proc = self.run_install("--check")
        return proc.stdout[proc.stdout.index("Config health"):].split("\n\n")[0]

    def test_an_enabled_digest_without_its_map_is_named(self):
        health = self.health({"workspace_root": self.workspace})
        self.assertIn("tooling-digest is on but %s does not exist"
                      % os.path.join(self.workspace, "TOOLING.md"), health)

    def test_a_present_map_is_quiet(self):
        with open(os.path.join(self.workspace, "TOOLING.md"), "w") as handle:
            handle.write("# Tooling\n")
        self.assertNotIn("tooling-digest", self.health({"workspace_root": self.workspace}))

    def test_a_disabled_digest_is_quiet(self):
        health = self.health({"workspace_root": self.workspace,
                              "tooling_digest": {"enabled": False}})
        self.assertNotIn("tooling-digest", health)

    def test_no_root_and_no_path_is_named_unconfigured(self):
        health = self.health({})
        self.assertIn("tooling-digest is on but no file is configured", health)

    def test_a_check_that_cannot_run_says_so(self):
        os.remove(os.path.join(self.repo, "tools", "tooling-digest", "digest_lib.py"))
        health = self.health({"workspace_root": self.workspace})
        self.assertIn("tooling-digest check failed (exit 1)", health)


class TestNoTtyFirstRun(PreflightTestCase):

    def test_the_copied_example_is_named_as_setting_no_root(self):
        os.makedirs(self.home)
        proc = self.run_install()
        self.assertIn("copied config.example.json", proc.stdout)
        self.assertIn("no workspace root set", proc.stdout)
        self.assertNotIn("placeholder", proc.stdout)
        with open(os.path.join(self.home, ".config", "claude-code-toolkit",
                               "config.json")) as handle:
            copied = json.load(handle)
        self.assertIsNone(copied.get("workspace_root"))


class TestModeArguments(PreflightTestCase):

    def test_yes_alone_is_an_unknown_option_and_changes_nothing(self):
        self.seed({"workspace_root": self.workspace})
        before = snapshot(self.home)
        proc = self.run_install("--yes")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("unknown option: --yes", proc.stderr)
        self.assertChangedNothing(before, proc)

    def test_a_shipped_mode_with_extra_arguments_is_refused(self):
        self.seed({"workspace_root": self.workspace})
        before = snapshot(self.home)
        proc = self.run_install("--check", "--yes")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("only valid as: ./install.sh --uninstall --yes", proc.stderr)
        self.assertChangedNothing(before, proc)
        proc = self.run_install("--check", "--setup")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("--check takes no other arguments", proc.stderr)
        self.assertChangedNothing(before, proc)

    def test_help_lists_every_mode(self):
        proc = self.run_install("--help")
        self.assertEqual(proc.returncode, 0)
        for mode in ("--check", "--links-only", "--manifest", "--verify-live",
                     "--setup", "--autonomous", "--version", "--update", "--rollback",
                     "--uninstall", "--uninstall [--yes]"):
            self.assertIn(mode, proc.stdout)


if __name__ == "__main__":
    unittest.main()
