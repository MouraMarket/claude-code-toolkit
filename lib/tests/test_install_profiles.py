#!/usr/bin/env python3
"""install.sh installs and checks one plugin per SELECTED profile.

Drives the REAL install.sh against a fixture repo with a registry, a sandbox
HOME and the shared `claude` shim, and asserts on the shim's argv log, the
output and the exit code. A broken profile selection must stop the run before
any CLI call: guessing a default would install a different plugin set than the
one asked for.
"""

import copy
import json
import os
import stat
import sys
import unittest

import install_fixture as fx
from test_install_plugin import PluginInstallTestCase, git

EXTRA_ID = "extra@claude-code-toolkit"
CORE_INSTALL = "plugin install toolkit-core@claude-code-toolkit --scope user"
CONTINUITY_INSTALL = "plugin install toolkit-continuity@claude-code-toolkit --scope user"
EXTRA_INSTALL = "plugin install extra@claude-code-toolkit --scope user"


def two_profiles(extra_agents=False):
    registry = copy.deepcopy(fx.real_profiles())
    registry["profiles"]["extra"] = {
        "plugin": "extra", "dir": "plugin-extra", "default": False,
        "requires": [], "agents": extra_agents, "summary": "A second test profile."}
    return registry


class ProfilesTestCase(PluginInstallTestCase):

    def use_registry(self, registry):
        fx.add_plugin_layout(self.repo, profiles=registry)
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "profiles", cwd=self.repo)

    def write_config(self, data=None, raw=None, name="config-under-test.json"):
        path = os.path.join(self.root, name)
        with open(path, "w") as handle:
            handle.write(raw if raw is not None else json.dumps(data))
        return path

    def run_with(self, data, *args, **kwargs):
        path = self.write_config(data, **kwargs)
        return self.run_install(args, extra_env={"CLAUDE_TOOLKIT_CONFIG": path})

    def installs(self):
        return [line for line in self.log() if line.startswith("plugin install")]

    def disables(self):
        return [line for line in self.log() if line.startswith("plugin disable")]

    def plugin_ids(self):
        # The shim's own list of what is installed.
        with open(self.state) as handle:
            return {p["id"]: p["enabled"] for p in json.load(handle)["plugins"]}


class TestDefaultSelection(ProfilesTestCase):

    def test_no_config_installs_core_and_continuity_and_says_it_defaulted(self):
        proc = self.run_install()
        self.assertEqual([CORE_INSTALL, CONTINUITY_INSTALL], self.installs(),
                         proc.stdout + proc.stderr)
        self.assertIn("profiles: core, continuity (default", proc.stdout)
        self.assertIn('set "profiles" in your toolkit config to choose', proc.stdout)


class TestABrokenSelectionStopsBeforeTheCli(ProfilesTestCase):

    def assert_stopped(self, proc, *needles):
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("profiles:", proc.stdout)
        for needle in needles:
            self.assertIn(needle, proc.stdout)
        self.assertEqual([], self.installs())
        self.assertEqual([], self.log(), "a CLI call ran before the selection check")

    def test_an_unknown_name_names_it_and_the_valid_ones(self):
        proc = self.run_with({"profiles": ["nope"]})
        self.assert_stopped(proc, "nope", "valid: core")

    def test_an_unknown_name_stops_check_too(self):
        proc = self.run_with({"profiles": ["nope"]}, "--check")
        self.assert_stopped(proc, "nope", "valid: core")

    def test_a_truncated_config_file_stops_the_install(self):
        proc = self.run_with(None, raw='{"profiles": ["co')
        self.assert_stopped(proc, "not valid JSON")

    def test_a_truncated_config_file_stops_check_too(self):
        proc = self.run_with(None, "--check", raw='{"profiles": ["co')
        self.assert_stopped(proc, "not valid JSON")


class TestTwoProfiles(ProfilesTestCase):

    def setUp(self):
        super().setUp()
        self.use_registry(two_profiles())

    def test_selecting_extra_installs_both_in_registry_order(self):
        proc = self.run_with({"profiles": ["extra"]})
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([CORE_INSTALL, EXTRA_INSTALL], self.installs())
        self.assertIn("profiles: core, extra", proc.stdout)
        self.assertNotIn("(default", proc.stdout)
        check = self.run_with({"profiles": ["extra"]}, "--check")
        self.assertIn("plugin extra@claude-code-toolkit enabled", check.stdout)
        self.assertNotIn("is not installed and enabled", check.stdout)

    def test_check_passes_the_plugin_section_once_both_are_installed(self):
        self.run_with({"profiles": ["extra"]})
        self.assertEqual([CORE_INSTALL, EXTRA_INSTALL], self.installs())
        proc = self.run_with({"profiles": ["extra"]}, "--check")
        for line in proc.stdout.splitlines():
            if "plugin" in line and "!!" in line:
                self.fail("plugin section failed: " + line)
        self.assertIn("hooks are protected", proc.stdout)

    def test_a_plugin_enabled_outside_the_profiles_is_left_alone(self):
        self.run_install()
        self.assertEqual([CORE_INSTALL, CONTINUITY_INSTALL], self.installs())
        proc = self.run_install(["--check"])
        self.assertNotIn("enabled outside your profiles", proc.stdout)
        # Enable extra through the shim, as a user would by hand.
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        import subprocess
        env = dict(os.environ)
        env["HOME"] = self.home
        env = fx.shim_env(env, self.bin_dir, self.state)
        subprocess.run(["claude", "plugin", "install", EXTRA_ID, "--scope", "user"],
                       env=env, check=True, capture_output=True)
        proc = self.run_install(["--check"])
        self.assertIn("extra@claude-code-toolkit enabled outside your profiles — left as is",
                      proc.stdout)
        self.assertEqual([], self.disables())
        self.assertIn("hooks are protected", proc.stdout)

    def test_a_selected_profile_disabled_after_install_fails_check(self):
        self.run_with({"profiles": ["extra"]})
        import subprocess
        env = dict(os.environ)
        env["HOME"] = self.home
        env = fx.shim_env(env, self.bin_dir, self.state)
        subprocess.run(["claude", "plugin", "disable", EXTRA_ID, "--scope", "user"],
                       env=env, check=True, capture_output=True)
        proc = self.run_with({"profiles": ["extra"]}, "--check")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("extra@claude-code-toolkit", proc.stdout)
        self.assertIn("is disabled", proc.stdout)
        self.assertIn("plugin extra@claude-code-toolkit is not installed and enabled", proc.stdout)

    def test_the_config_path_reaches_the_embedded_block_through_the_env_var(self):
        """Premise P12: only CLAUDE_TOOLKIT_CONFIG points at the config."""
        path = self.write_config({"profiles": ["extra"]}, name="elsewhere.json")
        proc = self.run_install(extra_env={"CLAUDE_TOOLKIT_CONFIG": path})
        self.assertEqual([CORE_INSTALL, EXTRA_INSTALL], self.installs(),
                         proc.stdout + proc.stderr)


class TestComponents(ProfilesTestCase):

    def test_a_selected_profile_missing_its_agents_dir_is_a_required_failure(self):
        self.use_registry(two_profiles(extra_agents=True))
        import shutil
        shutil.rmtree(os.path.join(self.repo, "plugin-extra", "agents"))
        proc = self.run_with({"profiles": ["extra"]})
        self.assertIn("plugin-extra/agents", proc.stdout)
        self.assertIn("!!", proc.stdout)
        self.assertNotEqual(0, proc.returncode, proc.stdout + proc.stderr)

    def test_default_selection_reports_skills_and_no_agents(self):
        # Core ships no agents under the split; only builder's dir does.
        proc = self.run_install()
        self.assertRegex(proc.stdout, r"OK\S* plugin/skills")
        self.assertRegex(proc.stdout, r"OK\S* plugin-continuity/skills")
        self.assertNotIn("/agents", proc.stdout)

    def test_the_builder_profile_reports_its_agents_dir(self):
        proc = self.run_with({"profiles": ["builder"]})
        self.assertRegex(proc.stdout, r"OK\S* plugin-build/agents")
        self.assertNotRegex(proc.stdout, r"plugin/agents")


class TestCheckSixFollowsTheOwningProfile(ProfilesTestCase):

    def test_an_extra_hook_is_resolved_under_the_extra_plugin(self):
        self.use_registry(two_profiles())
        command = '"${CLAUDE_PLUGIN_ROOT}"/tools/x/x'
        hooks = {"PreToolUse": [{"matcher": "Bash",
                                 "hooks": [{"type": "command", "command": command}]}]}
        with open(os.path.join(self.repo, "plugin-extra", "hooks", "hooks.json"), "w") as h:
            json.dump({"hooks": hooks}, h)
        os.makedirs(os.path.join(self.repo, "plugin-extra", "tools"), exist_ok=True)
        # The core plugin HAS the executable: resolving without the profile finds it.
        exe = os.path.join(self.repo, "plugin", "tools", "x", "x")
        os.makedirs(os.path.dirname(exe), exist_ok=True)
        with open(exe, "w") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(exe, os.stat(exe).st_mode | stat.S_IXUSR)
        self.run_with({"profiles": ["extra"]})
        proc = self.run_with({"profiles": ["extra"]}, "--check")
        # The embedded block exits 3 ("not protected"); the script's own exit is 1.
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("Hooks are NOT protected", proc.stdout)
        self.assertIn("hook x is missing", proc.stdout)
        self.assertIn(os.path.join("plugin-extra", "tools", "x", "x"), proc.stdout)


class TestMissingDefaultWarning(ProfilesTestCase):

    def test_a_list_without_continuity_warns_and_does_not_change_the_exit(self):
        proc = self.run_with({"profiles": ["builder"]}, "--check")
        self.assertIn("profiles: your list leaves out continuity, a default profile",
                      proc.stdout)
        self.assertIn('Add "continuity" to keep it', proc.stdout)
        self.assertNotIn("leaves out core", proc.stdout)
        # Warning only: the exit matches a list that keeps continuity.
        kept = self.run_with({"profiles": ["continuity", "builder"]}, "--check")
        self.assertEqual(kept.returncode, proc.returncode, proc.stdout + kept.stdout)

    def test_a_list_keeping_continuity_does_not_warn(self):
        proc = self.run_with({"profiles": ["continuity", "builder"]}, "--check")
        self.assertNotIn("leaves out", proc.stdout)

    def test_an_absent_key_does_not_warn(self):
        proc = self.run_install(["--check"])
        self.assertNotIn("leaves out", proc.stdout)


A_ID = "toolkit-a@claude-code-toolkit"
B_ID = "toolkit-b@claude-code-toolkit"


def four_profiles():
    """two_profiles() plus `a` and `b`, two more non-default test profiles."""
    registry = two_profiles()
    for name in ("a", "b"):
        registry["profiles"][name] = {
            "plugin": "toolkit-" + name, "dir": "plugin-" + name, "default": False,
            "requires": [], "agents": False, "summary": "Test profile %s." % name}
    return registry


class TestDeselectedProfiles(ProfilesTestCase):
    """A profile the last install selected and this one does not: its plugin is
    uninstalled, unless something that stays installed still needs it."""

    def setUp(self):
        super().setUp()
        self.use_registry(four_profiles())

    def run_profiles(self, names, *args, **env):
        path = self.write_config({"profiles": names})
        env["CLAUDE_TOOLKIT_CONFIG"] = path
        return self.run_install(args, extra_env=env)

    def uninstalls(self):
        return [line for line in self.log() if line.startswith("plugin uninstall")]

    def record_profiles(self):
        path = os.path.join(self.home, ".config", "claude-code-toolkit", "install-record.json")
        with open(path) as handle:
            return json.load(handle).get("profiles")

    def depends(self, directory, plugin, deps):
        path = os.path.join(self.repo, directory, ".claude-plugin", "plugin.json")
        with open(path, "w") as handle:
            json.dump({"name": plugin, "dependencies": deps}, handle)

    def add_foreign_plugin(self, key, deps):
        """A plugin from another (non-directory) marketplace, whose manifest the
        install reads through its installPath."""
        root = os.path.join(self.root, "foreign", key.replace("@", "_"))
        os.makedirs(os.path.join(root, ".claude-plugin"))
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w") as handle:
            json.dump({"name": key.partition("@")[0], "dependencies": deps}, handle)
        with open(self.state) as handle:
            state = json.load(handle)
        state["plugins"].append({"id": key, "enabled": True, "scope": "user",
                                 "installPath": root})
        with open(self.state, "w") as handle:
            json.dump(state, handle)

    def first_install(self, names):
        proc = self.run_profiles(names)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        return proc

    def test_a_dropped_profile_is_uninstalled_and_the_record_follows(self):
        self.first_install(["extra", "a"])
        self.assertEqual(["core", "extra", "a"], self.record_profiles())
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(["plugin uninstall %s --scope user" % A_ID], self.uninstalls())
        self.assertNotIn(A_ID, self.plugin_ids())
        self.assertIn(EXTRA_ID, self.plugin_ids())
        self.assertIn("uninstalling %s (profile a is no longer selected)" % A_ID, proc.stdout)
        self.assertEqual(["core", "extra"], self.record_profiles())

    def test_a_plugin_enabled_by_hand_is_never_uninstalled(self):
        self.first_install(["extra"])
        import subprocess
        env = fx.shim_env(dict(os.environ, HOME=self.home), self.bin_dir, self.state)
        subprocess.run(["claude", "plugin", "install", A_ID, "--scope", "user"],
                       env=env, check=True, capture_output=True)
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.uninstalls())
        self.assertIn("%s enabled outside your profiles — left as is" % A_ID, proc.stdout)

    def test_no_record_means_nothing_is_owned(self):
        self.first_install(["extra", "a"])
        os.remove(os.path.join(self.home, ".config", "claude-code-toolkit",
                               "install-record.json"))
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.uninstalls())
        self.assertIn("%s enabled outside your profiles — left as is" % A_ID, proc.stdout)

    def test_a_plugin_another_plugin_needs_is_kept(self):
        self.first_install(["extra", "a"])
        self.add_foreign_plugin("other@thirdparty", ["toolkit-a"])
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.uninstalls())
        self.assertIn("kept %s: other@thirdparty depends on it" % A_ID, proc.stdout)
        self.assertNotIn("%s enabled outside your profiles" % A_ID, proc.stdout)

    def test_a_kept_plugin_with_a_stale_version_is_refreshed_not_uninstalled(self):
        """Codex Code Review (row 107): a deselected plugin kept for a dependent
        is live and served from this checkout, so a stale listed version is
        refreshed like a profile's. Enablement is never changed."""
        self.first_install(["extra", "a"])
        self.add_foreign_plugin("other@thirdparty", ["toolkit-a"])
        head12 = git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()[:12]
        with open(self.state) as handle:
            state = json.load(handle)
        for plugin in state["plugins"]:
            if plugin["id"] == A_ID:
                plugin["version"] = "aaaaaaaaaaaa"
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        proc = self.run_profiles(["extra"],
                                 CLAUDE_SHIM_LATEST=json.dumps({A_ID: head12}))
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.uninstalls())
        self.assertIn("kept %s: other@thirdparty depends on it" % A_ID, proc.stdout)
        self.assertEqual(["plugin update %s --scope user" % A_ID],
                         [l for l in self.log() if l.startswith("plugin update")])
        self.assertFalse([l for l in self.log()
                          if l.startswith(("plugin enable", "plugin disable"))
                          and A_ID in l])

    def test_dependents_are_uninstalled_first(self):
        self.first_install(["extra", "a", "b"])
        self.depends("plugin-b", "toolkit-b", ["toolkit-a"])
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(["plugin uninstall %s --scope user" % B_ID,
                          "plugin uninstall %s --scope user" % A_ID], self.uninstalls())

    def test_a_chain_from_a_staying_plugin_keeps_every_link(self):
        # extra (stays) -> a -> b: both a and b are still needed.
        self.first_install(["extra", "a", "b"])
        self.depends("plugin-extra", "extra", ["toolkit-a"])
        self.depends("plugin-a", "toolkit-a", ["toolkit-b@claude-code-toolkit"])
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.uninstalls())
        self.assertIn("kept %s: %s depends on it" % (A_ID, EXTRA_ID), proc.stdout)
        self.assertIn("kept %s: %s depends on it" % (B_ID, A_ID), proc.stdout)

    def test_a_chain_against_name_order_still_keeps_every_link(self):
        # extra (stays) -> b -> a: a sorts first, so a single pass would miss it.
        self.first_install(["extra", "a", "b"])
        self.depends("plugin-extra", "extra", ["toolkit-b"])
        self.depends("plugin-b", "toolkit-b", ["toolkit-a"])
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.uninstalls())
        self.assertIn("kept %s: %s depends on it" % (A_ID, B_ID), proc.stdout)

    def test_a_newly_selected_plugin_keeps_what_it_needs(self):
        # Switch from b to a, where a needs b: a is installed this very run, so
        # the plugin listing taken before the installs does not show it yet.
        self.first_install(["b"])
        self.depends("plugin-a", "toolkit-a", ["toolkit-b"])
        proc = self.run_profiles(["a"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.uninstalls())
        self.assertIn(B_ID, self.plugin_ids())
        self.assertIn("kept %s: %s depends on it" % (B_ID, A_ID), proc.stdout)

    def test_check_reports_and_changes_nothing(self):
        self.first_install(["extra", "a"])
        proc = self.run_profiles(["extra"], "--check")
        self.assertIn("plugin %s: profile a is no longer selected (would uninstall)" % A_ID,
                      proc.stdout)
        self.assertEqual([], self.uninstalls())
        self.assertIn(A_ID, self.plugin_ids())

    def test_a_refused_uninstall_fails_and_keeps_the_owned_set_for_a_retry(self):
        self.first_install(["extra", "a"])
        proc = self.run_profiles(["extra"], CLAUDE_SHIM_FAIL="uninstall")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(["core", "extra", "a"], self.record_profiles())
        self.assertIn(A_ID, self.plugin_ids())
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertNotIn(A_ID, self.plugin_ids())
        self.assertEqual(["core", "extra"], self.record_profiles())

    def test_an_uninstall_that_changes_nothing_fails_the_end_state_check(self):
        self.first_install(["extra", "a"])
        proc = self.run_profiles(["extra"], CLAUDE_SHIM_NOOP="uninstall")
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("plugin %s is still installed after uninstalling it" % A_ID, proc.stdout)
        self.assertEqual(["core", "extra", "a"], self.record_profiles())

    def test_a_recorded_profile_the_registry_no_longer_has_is_skipped(self):
        self.first_install(["extra", "a"])
        registry = four_profiles()
        del registry["profiles"]["a"]
        self.use_registry(registry)
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("profile a from your last install no longer exists; its plugin, "
                      "if any, was left as is", proc.stdout)
        self.assertEqual([], self.uninstalls())

    def test_an_unreadable_dependency_keeps_everything_and_says_why(self):
        self.first_install(["extra", "a"])
        self.add_foreign_plugin("other@thirdparty", [])
        os.remove(os.path.join(self.root, "foreign", "other_thirdparty", ".claude-plugin",
                               "plugin.json"))
        proc = self.run_profiles(["extra"])
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.uninstalls())
        self.assertIn("kept %s: cannot read other@thirdparty's plugin.json" % A_ID, proc.stdout)


class TestConfigDocumentation(unittest.TestCase):

    def test_config_example_documents_profiles(self):
        with open(os.path.join(fx.REPO_ROOT, "config.example.json")) as handle:
            example = json.load(handle)
        self.assertNotIn("profiles", example)
        note = example.get("//profiles")
        self.assertIsInstance(note, list)
        note = " ".join(note)
        self.assertIn("core", note)
        self.assertIn("default", note)
        self.assertIn("exit 1", note)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
