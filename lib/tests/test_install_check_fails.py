#!/usr/bin/env python3
"""`./install.sh --check` must be able to FAIL.

A check that cannot reach a red verdict is a check nobody reads. These drive
the REAL install.sh against the shared fixture and `claude` shim, and pin one
"protected" verdict (its six conditions) from both sides: a
seeded, migrated fixture exits 0, and each way of being unprotected exits 1
with its own line. Managed settings are always read from a sandbox directory
(CLAUDE_TOOLKIT_MANAGED_DIR), so a real /Library/... file can never leak in.
The toolkit must never write `disableAllHooks` / `allowManagedHooksOnly`, so
every such case also asserts the settings file is byte-identical afterwards.
"""

import json
import os
import stat
import sys
import unittest

import install_fixture as fx
from test_install_plugin import PluginInstallTestCase, read

PLUGIN = fx.PLUGIN_ID


class CheckVerdictTestCase(PluginInstallTestCase):

    def setUp(self):
        super().setUp()
        self.managed = os.path.join(self.root, "managed")
        os.makedirs(os.path.join(self.managed, "managed-settings.d"))
        fx.seed_installed(self.state, self.repo)
        # --check also fails on a link that is not in place.
        fx.install_links(self.repo, self.home)
        self.write_settings({"statusLine": self.expected_status_line()})

    def check(self, extra_env=None, path=None):
        env = {"CLAUDE_TOOLKIT_MANAGED_DIR": self.managed}
        env.update(extra_env or {})
        return self.run_install(["--check"], extra_env=env, path=path)

    def install(self, extra_env=None):
        env = {"CLAUDE_TOOLKIT_MANAGED_DIR": self.managed}
        env.update(extra_env or {})
        return self.run_install([], extra_env=env)

    def write_managed(self, data, name="managed-settings.json"):
        with open(os.path.join(self.managed, name), "w") as handle:
            json.dump(data, handle)

    def assertRed(self, proc, text):
        out = proc.stdout + proc.stderr
        self.assertEqual(1, proc.returncode, out)
        self.assertIn(text, out)

    def set_hooks(self, hooks):
        with open(os.path.join(self.repo, "plugin", "hooks", "hooks.json"), "w") as handle:
            json.dump({"hooks": hooks}, handle)


class TestPositiveControl(CheckVerdictTestCase):

    def test_seeded_migrated_fixture_is_protected(self):
        proc = self.check()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_managed_enabled_plugin_with_managed_only_is_protected(self):
        # Managed-only hooks: every selected plugin (the default is core and
        # continuity) must be enabled by a managed file to count as protected.
        self.write_managed({"allowManagedHooksOnly": True,
                            "enabledPlugins": {PLUGIN: True,
                                               "toolkit-continuity@claude-code-toolkit": True}})
        proc = self.check()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_managed_only_without_continuity_is_not_protected(self):
        self.write_managed({"allowManagedHooksOnly": True,
                            "enabledPlugins": {PLUGIN: True}})
        proc = self.check()
        self.assertNotEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("no managed file enables toolkit-continuity@claude-code-toolkit", proc.stdout)


class TestUnprotected(CheckVerdictTestCase):

    def test_1_cli_absent(self):
        path = fx.path_without_claude(self.root)
        self.assertRed(self.check(path=path), "claude CLI")

    def test_1_list_call_fails(self):
        self.assertRed(self.check({"CLAUDE_SHIM_FAIL": "list"}), "claude plugin")

    def test_1_unparseable_json(self):
        self.assertRed(self.check({"CLAUDE_SHIM_BAD_JSON": "1"}), "unparseable")

    def test_2_marketplace_missing(self):
        fx.install_claude_shim(self.bin_dir, self.state)
        self.assertRed(self.check(), "marketplace claude-code-toolkit")

    def test_2_marketplace_elsewhere(self):
        fx.seed_installed(self.state, os.path.join(self.root, "elsewhere"))
        self.assertRed(self.check(), "marketplace claude-code-toolkit")

    def test_3_plugin_missing(self):
        with open(self.state) as handle:
            state = json.load(handle)
        state["plugins"] = []
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        self.assertRed(self.check(), "plugin " + PLUGIN)

    def test_3_plugin_installed_at_project_scope_only(self):
        fx.seed_project_scoped(self.state, self.repo)
        self.assertRed(self.check(), "plugin " + PLUGIN)

    def test_3_plugin_disabled(self):
        with open(self.state) as handle:
            state = json.load(handle)
        state["plugins"][0]["enabled"] = False
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        self.assertRed(self.check(), "plugin " + PLUGIN)

    def test_4_disable_all_hooks_in_user_settings(self):
        before = self.write_settings({"disableAllHooks": True,
                                      "statusLine": self.expected_status_line()})
        proc = self.check()
        self.assertRed(proc, "disableAllHooks")
        self.assertIn("will not change", proc.stdout)
        self.assertIn("project", proc.stdout)
        self.assertEqual(before, read(self.settings_path))

    def test_4_disable_all_hooks_in_managed_file(self):
        before = read(self.settings_path)
        self.write_managed({"disableAllHooks": True})
        self.assertRed(self.check(), "disableAllHooks")
        self.assertEqual(before, read(self.settings_path))

    def test_4_disable_all_hooks_in_managed_drop_in(self):
        before = read(self.settings_path)
        self.write_managed({"disableAllHooks": True}, "managed-settings.d/50-x.json")
        self.assertRed(self.check(), "disableAllHooks")
        self.assertEqual(before, read(self.settings_path))

    def test_4_disable_all_hooks_false_is_fine(self):
        self.write_managed({"disableAllHooks": False})
        self.assertEqual(0, self.check().returncode)

    def test_4b_managed_hooks_only_without_managed_plugin(self):
        before = read(self.settings_path)
        self.write_managed({"allowManagedHooksOnly": True})
        self.assertRed(self.check(), "allowManagedHooksOnly")
        self.assertEqual(before, read(self.settings_path))

    def test_4b_managed_plugin_must_be_true(self):
        self.write_managed({"allowManagedHooksOnly": True,
                            "enabledPlugins": {PLUGIN: False}})
        self.assertRed(self.check(), "allowManagedHooksOnly")

    def test_5_legacy_handler_still_in_settings(self):
        legacy = fx.real_legacy_commands()[0].replace("__HOME__", self.home)
        before = self.write_settings({
            "statusLine": self.expected_status_line(),
            "hooks": {"Stop": [{"hooks": [{"type": "command", "command": legacy}]}]}})
        proc = self.check()
        self.assertRed(proc, legacy)
        self.assertEqual(before, read(self.settings_path))

    def test_6_manifest_unreadable(self):
        with open(os.path.join(self.repo, "plugin", "hooks", "hooks.json"), "w") as handle:
            handle.write("{ nope")
        self.assertRed(self.check(), "hook manifest")

    def test_6_registered_exe_missing(self):
        self.set_hooks({"Stop": [{"hooks": [{
            "type": "command", "command": '"${CLAUDE_PLUGIN_ROOT}"/tools/x/claude-gone'}]}]})
        self.assertRed(self.check(), "claude-gone")

    def test_6_registered_exe_not_executable(self):
        exe = os.path.join(self.repo, "plugin", "tools", "x", "claude-noexec")
        os.makedirs(os.path.dirname(exe))
        with open(exe, "w") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(exe, stat.S_IRUSR | stat.S_IWUSR)
        self.set_hooks({"Stop": [{"hooks": [{
            "type": "command", "command": '"${CLAUDE_PLUGIN_ROOT}"/tools/x/claude-noexec'}]}]})
        self.assertRed(self.check(), "claude-noexec")

    def test_every_failure_is_reported_in_one_run(self):
        self.write_managed({"disableAllHooks": True, "allowManagedHooksOnly": True})
        proc = self.check()
        self.assertRed(proc, "disableAllHooks")
        self.assertIn("allowManagedHooksOnly", proc.stdout)
        self.assertIn("\nTests\n", proc.stdout)          # the script ran to the end
        self.assertNotIn("Check complete", proc.stdout)  # and did not claim success


class TestRealInstallEndsProtected(CheckVerdictTestCase):

    def test_enable_that_changes_nothing_exits_1(self):
        with open(self.state) as handle:
            state = json.load(handle)
        state["plugins"][0]["enabled"] = False
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        proc = self.install({"CLAUDE_SHIM_NOOP": "enable"})
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)

    def test_disable_all_hooks_in_user_settings_exits_1(self):
        self.write_settings({"disableAllHooks": True,
                             "statusLine": self.expected_status_line()})
        proc = self.install()
        self.assertRed(proc, "disableAllHooks")
        self.assertTrue(self.settings()["disableAllHooks"])

    def test_managed_hooks_only_exits_1(self):
        self.write_managed({"allowManagedHooksOnly": True})
        self.assertRed(self.install(), "allowManagedHooksOnly")

    def test_clean_install_exits_0(self):
        proc = self.install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
