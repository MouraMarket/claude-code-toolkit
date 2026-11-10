"""Tests for the prompt-optimizer PreToolUse hook.

Hermetic: every test points CLAUDE_TOOLKIT_CONFIG at a temp file or a
nonexistent path and HOME at a temp directory, never at whatever exists on
this machine.

The executable is exercised through subprocess, not just the lib: a suite of
green unit tests can run zero lines of the hook itself. The child's PATH is
built from scratch around a `python3` symlink, because the hook's shebang is
`#!/usr/bin/env python3` and the interpreter running the tests is not
necessarily named `python3`.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO, "lib"))
import plugin_paths  # noqa: E402
import prompt_optimizer_lib  # noqa: E402

HOOK = os.path.join(TOOL_DIR, "claude-prompt-optimizer")
LIB = os.path.join(TOOL_DIR, "prompt_optimizer_lib.py")
ENABLED = {"prompt_optimizer": {"enabled": True}}


class FakeConfig:
    """Stands in for toolkit_config with a dict, so tests need no real file."""

    def __init__(self, data=None):
        self.data = data or {}

    def get(self, *keys, default=None):
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node


class HomeTestCase(unittest.TestCase):
    """A temp HOME with helpers to install a skill or write settings."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude"))

    def install_user_skill(self):
        folder = os.path.join(self.home, ".claude", "skills", "prompt-optimizer")
        os.makedirs(folder)
        with open(os.path.join(folder, "SKILL.md"), "w") as handle:
            handle.write("---\nname: prompt-optimizer\n---\n")

    def write_settings(self, data=None, raw=None):
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as handle:
            handle.write(raw if raw is not None else json.dumps(data))

    def make_plugin_root(self, with_skill=True):
        root = os.path.join(self.tmp, "plugin-root")
        folder = os.path.join(root, "skills", "prompt-optimizer")
        os.makedirs(folder)
        if with_skill:
            with open(os.path.join(folder, "SKILL.md"), "w") as handle:
                handle.write("---\nname: prompt-optimizer\n---\n")
        return root


class TestIsEnabled(unittest.TestCase):
    """The load-bearing gate: absent config means OFF, an opt-in default."""

    def test_key_absent_from_config_is_off(self):
        self.assertIs(prompt_optimizer_lib.is_enabled(FakeConfig()), False)

    def test_no_config_module_reachable_is_off(self):
        # A config-less environment: CLAUDE_TOOLKIT_CONFIG pointed at a path
        # that does not exist. toolkit_config.get() degrades to {} in that
        # case, which is the real "absent means OFF" path this hook relies on.
        import toolkit_config
        original = toolkit_config.CONFIG_PATH
        toolkit_config.CONFIG_PATH = "/nonexistent-config.json"
        toolkit_config.load(refresh=True)
        try:
            self.assertIs(prompt_optimizer_lib.is_enabled(None), False)
        finally:
            toolkit_config.CONFIG_PATH = original
            toolkit_config.load(refresh=True)

    def test_explicit_enabled_true_is_on(self):
        self.assertIs(prompt_optimizer_lib.is_enabled(FakeConfig(ENABLED)), True)

    def test_explicit_enabled_false_is_off(self):
        config = FakeConfig({"prompt_optimizer": {"enabled": False}})
        self.assertIs(prompt_optimizer_lib.is_enabled(config), False)

    def test_truthy_non_bool_values_are_off(self):
        # Only a literal True counts. A string or a number must not enable it
        # by accident; bool is not an int and not a str.
        for value in ("true", 1, "yes", [True]):
            with self.subTest(value=value):
                config = FakeConfig({"prompt_optimizer": {"enabled": value}})
                self.assertIs(prompt_optimizer_lib.is_enabled(config), False)

    def test_empty_section_is_off(self):
        config = FakeConfig({"prompt_optimizer": {}})
        self.assertIs(prompt_optimizer_lib.is_enabled(config), False)

    def test_broken_config_get_is_off_not_raising(self):
        class BrokenConfig:
            def get(self, *keys, default=None):
                raise RuntimeError("boom")

        self.assertIs(prompt_optimizer_lib.is_enabled(BrokenConfig()), False)


class TestFindSkill(HomeTestCase):

    def test_user_skill_file_is_found_by_bare_name(self):
        self.install_user_skill()
        self.assertEqual(prompt_optimizer_lib.find_skill(self.home), "prompt-optimizer")

    def test_user_skill_directory_without_skill_md_is_not_found(self):
        os.makedirs(os.path.join(self.home, ".claude", "skills", "prompt-optimizer"))
        self.assertIsNone(prompt_optimizer_lib.find_skill(self.home))

    def test_enabled_plugin_skill_is_found_as_plugin_colon_skill(self):
        root = self.make_plugin_root()
        self.write_settings({"enabledPlugins": {"toolkit-extras@some-market": True}})
        with mock.patch.object(plugin_paths, "installed_root", return_value=root):
            self.assertEqual(prompt_optimizer_lib.find_skill(self.home),
                             "toolkit-extras:prompt-optimizer")

    def test_enabled_plugin_without_the_skill_is_not_found(self):
        root = self.make_plugin_root(with_skill=False)
        self.write_settings({"enabledPlugins": {"toolkit-extras@some-market": True}})
        with mock.patch.object(plugin_paths, "installed_root", return_value=root):
            self.assertIsNone(prompt_optimizer_lib.find_skill(self.home))

    def test_plugin_not_enabled_by_a_literal_true_is_not_found(self):
        # bool is not an int and not a str: only a JSON true enables a plugin.
        root = self.make_plugin_root()
        for value in (False, 1, "true", None):
            with self.subTest(value=value):
                self.write_settings({"enabledPlugins": {"toolkit-extras@m": value}})
                with mock.patch.object(plugin_paths, "installed_root", return_value=root):
                    self.assertIsNone(prompt_optimizer_lib.find_skill(self.home))

    def test_enabled_plugins_that_is_a_list_is_not_found(self):
        self.write_settings({"enabledPlugins": ["toolkit-extras@m"]})
        self.assertIsNone(prompt_optimizer_lib.find_skill(self.home))

    def test_settings_that_are_not_an_object_are_not_found(self):
        self.write_settings(["enabledPlugins"])
        self.assertIsNone(prompt_optimizer_lib.find_skill(self.home))

    def test_invalid_json_settings_are_not_found(self):
        self.write_settings(raw="{not json")
        self.assertIsNone(prompt_optimizer_lib.find_skill(self.home))

    def test_missing_settings_are_not_found(self):
        self.assertIsNone(prompt_optimizer_lib.find_skill(self.home))

    def test_a_missing_or_non_string_home_is_not_found(self):
        for home in (None, "", 0, False, ["/tmp"]):
            with self.subTest(home=home):
                self.assertIsNone(prompt_optimizer_lib.find_skill(home))

    def test_the_first_sorted_match_wins(self):
        root = self.make_plugin_root()
        self.write_settings({"enabledPlugins": {"zeta@m": True, "alpha@m": True}})
        with mock.patch.object(plugin_paths, "installed_root", return_value=root):
            self.assertEqual(prompt_optimizer_lib.find_skill(self.home),
                             "alpha:prompt-optimizer")


class TestReminderText(unittest.TestCase):

    def test_the_reminder_names_the_skill_to_invoke(self):
        text = prompt_optimizer_lib.reminder_text("prompt-optimizer")
        self.assertTrue(text.startswith("prompt-optimizer: "))
        self.assertIn("/prompt-optimizer skill", text)
        self.assertIn("proceed as-is.", text)

    def test_the_plugin_form_is_substituted_verbatim(self):
        text = prompt_optimizer_lib.reminder_text("extras:prompt-optimizer")
        self.assertIn("/extras:prompt-optimizer skill", text)

    def test_payload_carries_the_reminder_text_verbatim(self):
        data = prompt_optimizer_lib.payload("prompt-optimizer")
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(data["hookSpecificOutput"]["additionalContext"],
                         prompt_optimizer_lib.reminder_text("prompt-optimizer"))


class TestBuildNeedsTheSkill(HomeTestCase):

    def test_enabled_without_a_skill_yields_the_empty_string(self):
        self.assertEqual(
            prompt_optimizer_lib.build(FakeConfig(ENABLED), home=self.home), "")

    def test_enabled_with_a_skill_yields_json_naming_it(self):
        self.install_user_skill()
        out = prompt_optimizer_lib.build(FakeConfig(ENABLED), home=self.home)
        data = json.loads(out)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertIn("/prompt-optimizer", data["hookSpecificOutput"]["additionalContext"])

    def test_disabled_with_a_skill_yields_the_empty_string(self):
        self.install_user_skill()
        config = FakeConfig({"prompt_optimizer": {"enabled": False}})
        self.assertEqual(prompt_optimizer_lib.build(config, home=self.home), "")

    def test_unconfigured_yields_the_empty_string(self):
        self.install_user_skill()
        self.assertEqual(prompt_optimizer_lib.build(FakeConfig(), home=self.home), "")

    def test_disabled_reads_nothing_beyond_the_config(self):
        self.install_user_skill()
        config = FakeConfig({"prompt_optimizer": {"enabled": False}})
        with mock.patch.object(prompt_optimizer_lib, "find_skill",
                               side_effect=AssertionError("touched the home")):
            self.assertEqual(prompt_optimizer_lib.build(config, home=self.home), "")


class TestStatus(HomeTestCase):

    def test_off_when_not_enabled(self):
        self.install_user_skill()
        self.assertEqual(
            prompt_optimizer_lib.status(FakeConfig(), home=self.home), ("off", None))

    def test_inert_when_enabled_without_a_skill(self):
        self.assertEqual(
            prompt_optimizer_lib.status(FakeConfig(ENABLED), home=self.home),
            ("inert", None))

    def test_on_when_enabled_with_a_skill(self):
        self.install_user_skill()
        self.assertEqual(
            prompt_optimizer_lib.status(FakeConfig(ENABLED), home=self.home),
            ("on", "prompt-optimizer"))

    def test_off_when_disabled_reads_nothing_beyond_the_config(self):
        with mock.patch.object(prompt_optimizer_lib, "find_skill",
                               side_effect=AssertionError("touched the home")):
            self.assertEqual(
                prompt_optimizer_lib.status(FakeConfig(), home=self.home), ("off", None))


class Subprocesses(HomeTestCase):
    """Runs a script with an environment built from scratch."""

    def setUp(self):
        super().setUp()
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        os.symlink(sys.executable, os.path.join(self.bin, "python3"))
        self.config_path = os.path.join(self.tmp, "config.json")

    def env(self, config=None, raw_config=None):
        if raw_config is not None:
            with open(self.config_path, "w") as handle:
                handle.write(raw_config)
            path = self.config_path
        elif config is None:
            path = "/nonexistent-config.json"
        else:
            with open(self.config_path, "w") as handle:
                json.dump(config, handle)
            path = self.config_path
        return {"PATH": self.bin, "HOME": self.home, "CLAUDE_TOOLKIT_CONFIG": path}

    def run_script(self, script, args=(), stdin='{"tool_name":"Agent"}', **env_kwargs):
        # The library is a plain module, not an executable: run it by interpreter.
        command = [sys.executable, script] if script.endswith(".py") else [script]
        return subprocess.run(
            command + list(args), input=stdin, env=self.env(**env_kwargs),
            capture_output=True, text=True, timeout=20,
        )


class TestHookExecutable(Subprocesses):
    """Through subprocess, the way Claude Code actually runs it."""

    def test_enabled_with_a_skill_emits_one_json_line_with_the_reminder(self):
        self.install_user_skill()
        result = self.run_script(HOOK, config=ENABLED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        data = json.loads(result.stdout)
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "PreToolUse")
        self.assertEqual(data["hookSpecificOutput"]["additionalContext"],
                         prompt_optimizer_lib.reminder_text("prompt-optimizer"))

    def test_enabled_without_a_skill_emits_nothing(self):
        result = self.run_script(HOOK, config=ENABLED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_disabled_emits_nothing(self):
        self.install_user_skill()
        result = self.run_script(HOOK, config={"prompt_optimizer": {"enabled": False}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_config_absent_entirely_emits_nothing(self):
        # What a fresh clone looks like: the opt-in default.
        self.install_user_skill()
        result = self.run_script(HOOK, config=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_malformed_stdin_still_exits_zero_and_emits_nothing_extra(self):
        self.install_user_skill()
        result = self.run_script(HOOK, config=ENABLED, stdin="not json at all")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)

    def test_empty_stdin_still_exits_zero(self):
        result = self.run_script(HOOK, config=ENABLED, stdin="")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_config_file_that_is_not_valid_json_exits_zero_silently(self):
        self.install_user_skill()
        result = self.run_script(HOOK, raw_config="{not json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class TestStatusCli(Subprocesses):

    def test_off_prints_nothing_and_exits_zero(self):
        result = self.run_script(LIB, ["status"], config=None)
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)

    def test_on_names_the_skill(self):
        self.install_user_skill()
        result = self.run_script(LIB, ["status"], config=ENABLED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "on: reminds before each subagent dispatch to use /prompt-optimizer")

    def test_inert_says_the_skill_is_missing(self):
        result = self.run_script(LIB, ["status"], config=ENABLED)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "inert: prompt_optimizer.enabled is true but no prompt-optimizer skill "
            "is installed, so the reminder stays silent. Install the skill, or set "
            "prompt_optimizer.enabled to false.")

    def test_unknown_argument_prints_usage_and_exits_two(self):
        result = self.run_script(LIB, ["bogus"], config=ENABLED)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("usage", result.stderr)

    def test_no_argument_exits_two(self):
        self.assertEqual(self.run_script(LIB, [], config=ENABLED).returncode, 2)

    def test_main_returns_two_in_process_for_a_bad_argument(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(prompt_optimizer_lib.main(["bogus"]), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
