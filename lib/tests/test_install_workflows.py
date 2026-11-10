#!/usr/bin/env python3
"""Behavioural tests for install.sh's "Workflows -> ~/.claude/settings.json"
section, which writes `enableWorkflows: true` to user settings when the toolkit
config's `build.enable_workflows` is the JSON boolean `true` and the builder
profile is selected.

The section is a thin shell around `lib/workflows_setting.py` (the pure
planner). These tests drive the REAL `install.sh` through `subprocess` against
a fixture repo, with `$HOME` sandboxed and `CLAUDE_TOOLKIT_CONFIG` pointed at a
fixture config OUTSIDE `$HOME/.config/claude-code-toolkit` (the fault-injection
test makes that directory read-only). Every table-row test asserts BOTH the
final settings.json and the install record's `enable_workflows_added` flag.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

import install_fixture as fx
import test_install_auto_memory as am

SET_LINE = "\033[33m->\033[0m enableWorkflows = true"
REMOVE_LINE = "\033[33m->\033[0m removing enableWorkflows (no longer wanted)"
OK_OWNED_LINE = "\033[32mOK\033[0m enableWorkflows = true (set by the toolkit)"
OK_UNMANAGED_LINE = ("\033[32mOK\033[0m enableWorkflows not managed "
                     "(build.enable_workflows is off or builder is not selected)")
EDITED_LINE = ("\033[31m!!\033[0m enableWorkflows was changed since the toolkit set it "
               "— left as you set it; no longer managed")
SKIP_LINE = "enableWorkflows skipped: toolkit lib not importable"

PROFILES = ["core", "continuity", "builder"]


def workflows_section(stdout):
    marker = "Workflows -> ~/.claude/settings.json"
    start = stdout.index(marker)
    end = stdout.index("\n\n", start)
    return stdout[start:end]


def read_bytes(path):
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except FileNotFoundError:
        return None


class WorkflowsCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.repo = am.make_fixture_repo(self.root)
        # Outside $HOME/.config/claude-code-toolkit on purpose.
        self.config_path = os.path.join(self.root, "toolkit-config.json")
        am.seed_home(self.home)
        self.record_file = os.path.join(
            self.home, ".config", "claude-code-toolkit", "install-record.json")

    def config(self, enable=True, profiles=PROFILES):
        data = {}
        if profiles is not None:
            data["profiles"] = profiles
        if enable is not None:
            data["build"] = {"enable_workflows": enable}
        am.write_config(self.config_path, data)

    def run_install(self, args=()):
        return am.run_install(self.repo, self.home, self.config_path, args)

    def settings(self):
        path = am.settings_path_for(self.home)
        if not os.path.exists(path):
            return {}
        with open(path) as handle:
            return json.load(handle)

    def flag(self):
        """The record's enable_workflows_added, or None when absent."""
        if not os.path.exists(self.record_file):
            return None
        with open(self.record_file) as handle:
            return json.load(handle).get("enable_workflows_added")

    def install_ok(self, args=()):
        result = self.run_install(args)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return workflows_section(result.stdout)

    def do_set(self):
        """A first run that sets the key; the state every owned row starts from."""
        self.config(True)
        section = self.install_ok()
        self.assertIn(SET_LINE, section)
        self.assertIs(True, self.settings()["enableWorkflows"])
        self.assertIs(True, self.flag())


class TestTableRows(WorkflowsCase):

    def test_set_when_wanted_unowned_absent(self):
        am.write_settings(self.home, {"otherKey": "keep-me"})
        self.config(True)
        section = self.install_ok()
        self.assertIn(SET_LINE, section)
        written = self.settings()
        self.assertIs(True, written["enableWorkflows"])
        self.assertEqual("keep-me", written["otherKey"])
        self.assertIs(True, self.flag())

    def test_foreign_false_is_left_alone(self):
        am.write_settings(self.home, {"enableWorkflows": False})
        self.config(True)
        section = self.install_ok()
        self.assertIn("\033[32mOK\033[0m enableWorkflows is yours (false) — left as is",
                      section)
        self.assertIs(False, self.settings()["enableWorkflows"])
        self.assertIsNone(self.flag())

    def test_foreign_true_is_left_alone_and_not_claimed(self):
        am.write_settings(self.home, {"enableWorkflows": True})
        self.config(True)
        section = self.install_ok()
        self.assertIn("enableWorkflows is yours (true) — left as is", section)
        self.assertIs(True, self.settings()["enableWorkflows"])
        self.assertIsNone(self.flag())

    def test_ok_when_wanted_owned_true_and_second_run_writes_nothing(self):
        self.do_set()
        before = read_bytes(am.settings_path_for(self.home))
        section = self.install_ok()
        self.assertIn(OK_OWNED_LINE, section)
        self.assertEqual(before, read_bytes(am.settings_path_for(self.home)))
        self.assertIs(True, self.settings()["enableWorkflows"])
        self.assertIs(True, self.flag())

    def test_edited_when_wanted_owned_and_value_is_false(self):
        self.do_set()
        am.write_settings(self.home, {"enableWorkflows": False})
        section = self.install_ok()
        self.assertIn(EDITED_LINE, section)
        self.assertIs(False, self.settings()["enableWorkflows"])
        self.assertIsNone(self.flag())

    def test_edited_when_wanted_owned_and_key_was_removed(self):
        self.do_set()
        am.write_settings(self.home, {})
        section = self.install_ok()
        self.assertIn(EDITED_LINE, section)
        self.assertNotIn("enableWorkflows", self.settings())
        self.assertIsNone(self.flag())

    def test_edited_when_wanted_owned_and_value_is_one(self):
        self.do_set()
        am.write_settings(self.home, {"enableWorkflows": 1})
        section = self.install_ok()
        self.assertIn(EDITED_LINE, section)
        self.assertEqual(1, self.settings()["enableWorkflows"])
        self.assertIsNot(True, self.settings()["enableWorkflows"])
        self.assertIsNone(self.flag())

    def test_remove_when_unwanted_owned_true(self):
        self.do_set()
        am.write_settings(self.home, {"enableWorkflows": True, "otherKey": "keep-me"})
        self.config(False)
        section = self.install_ok()
        self.assertIn(REMOVE_LINE, section)
        written = self.settings()
        self.assertNotIn("enableWorkflows", written)
        self.assertEqual("keep-me", written["otherKey"])
        self.assertIsNone(self.flag())

    def test_edited_when_unwanted_owned_and_value_is_false(self):
        self.do_set()
        am.write_settings(self.home, {"enableWorkflows": False})
        self.config(False)
        section = self.install_ok()
        self.assertIn(EDITED_LINE, section)
        self.assertIs(False, self.settings()["enableWorkflows"])
        self.assertIsNone(self.flag())

    def test_unwanted_unowned_is_not_managed_whatever_the_value(self):
        am.write_settings(self.home, {"enableWorkflows": True})
        self.config(False)
        section = self.install_ok()
        self.assertIn(OK_UNMANAGED_LINE, section)
        self.assertIs(True, self.settings()["enableWorkflows"])
        self.assertIsNone(self.flag())


class TestNotWanted(WorkflowsCase):

    def test_builder_not_selected_is_not_wanted(self):
        self.config(True, profiles=["core", "continuity"])
        section = self.install_ok()
        self.assertIn(OK_UNMANAGED_LINE, section)
        self.assertNotIn("enableWorkflows", self.settings())
        self.assertIsNone(self.flag())

    def test_string_true_is_not_wanted(self):
        self.config("true")
        section = self.install_ok()
        self.assertIn(OK_UNMANAGED_LINE, section)
        self.assertNotIn("enableWorkflows", self.settings())
        self.assertIsNone(self.flag())

    def test_absent_config_key_is_not_wanted(self):
        self.config(None)
        section = self.install_ok()
        self.assertIn(OK_UNMANAGED_LINE, section)
        self.assertNotIn("enableWorkflows", self.settings())
        self.assertIsNone(self.flag())


class TestProfilesError(WorkflowsCase):
    """A selection profiles.selected() cannot resolve must never read as
    "builder is off". A whole install.sh run cannot reach this section with a
    bad selection (the Hooks step before it stops on the same error), so these
    tests run the section's own python, extracted from install.sh, against the
    fixture repo and a sandboxed $HOME."""

    def run_section(self, check):
        with open(am.INSTALL_SH) as handle:
            text = handle.read()
        start = text.index('echo "Workflows -> ~/.claude/settings.json"')
        start = text.index("<<'PY'\n", start) + len("<<'PY'\n")
        code = text[start:text.index("\nPY\n", start)]
        env = dict(os.environ)
        env.update({"HOME": self.home, "CLAUDE_TOOLKIT_CONFIG": self.config_path,
                    "CHECK": "1" if check else "0", "REPO": self.repo,
                    "SETTINGS": am.settings_path_for(self.home)})
        return subprocess.run(["python3", "-"], input=code, env=env,
                              capture_output=True, universal_newlines=True,
                              timeout=60)

    def remove_case_with_unreadable_selection(self, check):
        self.do_set()
        am.write_config(self.config_path, {"profiles": ["no-such-profile"],
                                           "build": {"enable_workflows": False}})
        before = (read_bytes(am.settings_path_for(self.home)),
                  read_bytes(self.record_file))
        result = self.run_section(check)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("\033[31m!!\033[0m enableWorkflows left as is: "
                      "cannot tell the selected profiles (", result.stdout)
        self.assertNotIn("removing", result.stdout)
        self.assertIs(True, self.settings()["enableWorkflows"])
        self.assertIs(True, self.flag())
        self.assertEqual(before, (read_bytes(am.settings_path_for(self.home)),
                                  read_bytes(self.record_file)))

    def test_unreadable_selection_never_removes_the_key(self):
        self.remove_case_with_unreadable_selection(check=False)

    def test_unreadable_selection_under_check_writes_nothing(self):
        self.remove_case_with_unreadable_selection(check=True)


class TestCheckWritesNothing(WorkflowsCase):

    def snapshot(self):
        return (read_bytes(am.settings_path_for(self.home)),
                read_bytes(self.record_file))

    def test_check_on_set_prints_the_line_and_writes_nothing(self):
        am.write_settings(self.home, {"otherKey": "keep-me"})
        # `--check` exits 1 on a machine whose hooks are not protected, so a
        # real run (config off, so it writes no enableWorkflows) first installs
        # the links and plugins; this case is about what --check WRITES.
        self.config(False)
        self.install_ok()
        self.config(True)
        before = self.snapshot()
        result = self.run_install(["--check"])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(SET_LINE, workflows_section(result.stdout))
        self.assertEqual(before, self.snapshot())
        self.assertNotIn("enableWorkflows", self.settings())
        self.assertIsNone(self.flag())

    def test_check_on_remove_prints_the_line_and_writes_nothing(self):
        self.do_set()
        self.config(False)
        before = self.snapshot()
        result = self.run_install(["--check"])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(REMOVE_LINE, workflows_section(result.stdout))
        self.assertEqual(before, self.snapshot())
        self.assertIs(True, self.settings()["enableWorkflows"])
        self.assertIs(True, self.flag())

    def test_check_on_edited_leaves_the_flag(self):
        self.do_set()
        am.write_settings(self.home, {"enableWorkflows": False})
        before = self.snapshot()
        result = self.run_install(["--check"])
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(EDITED_LINE, workflows_section(result.stdout))
        self.assertEqual(before, self.snapshot())
        self.assertIs(True, self.flag())


class TestStubFixture(unittest.TestCase):

    def test_missing_module_skips_with_a_line_and_exits_zero(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        repo = am.make_fixture_repo(root)
        os.remove(os.path.join(repo, "lib", "workflows_setting.py"))
        am.git("add", "-A", cwd=repo)
        am.git("commit", "-q", "-m", "drop workflows_setting", cwd=repo)
        am.seed_home(home)
        config_path = os.path.join(root, "toolkit-config.json")
        am.write_config(config_path, {"profiles": PROFILES,
                                      "build": {"enable_workflows": True}})
        result = am.run_install(repo, home, config_path)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(SKIP_LINE, workflows_section(result.stdout))
        path = am.settings_path_for(home)
        if os.path.exists(path):
            with open(path) as handle:
                self.assertNotIn("enableWorkflows", json.load(handle))


class TestRecordFirst(WorkflowsCase):
    """The record is written BEFORE settings on `set`, so a record failure
    leaves settings.json without the key."""

    def tearDown(self):
        config_dir = os.path.dirname(self.record_file)
        if os.path.isdir(config_dir):
            os.chmod(config_dir, 0o700)

    def test_positive_control_writes_key_and_flag(self):
        self.config(True)
        section = self.install_ok()
        self.assertIn(SET_LINE, section)
        self.assertIs(True, self.settings()["enableWorkflows"])
        self.assertIs(True, self.flag())

    def test_unwritable_record_stops_the_settings_write(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.config(True)
        config_dir = os.path.dirname(self.record_file)
        os.chmod(config_dir, 0o500)
        # Guard the premise: the directory really is unwritable here.
        self.assertFalse(os.access(config_dir, os.W_OK), stat.filemode(
            os.stat(config_dir).st_mode))
        result = self.run_install()
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn("enableWorkflows NOT written", output)
        self.assertNotIn("enableWorkflows", self.settings())


class TestFixtureLayout(unittest.TestCase):

    def test_default_plugin_layout_ships_workflows_setting(self):
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        fx.add_plugin_layout(repo)
        self.assertTrue(os.path.isfile(os.path.join(repo, "lib", "workflows_setting.py")))


if __name__ == "__main__":
    unittest.main()
