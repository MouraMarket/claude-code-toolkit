#!/usr/bin/env python3
"""`install.sh --uninstall [--yes]`: ownership-aware removal, end to end.

Pinned here:
  - a round trip leaves the person's own settings, hooks and files alone
  - a rule that was already there, or a machine with no (or an empty) install
    record, loses nothing it cannot prove the installer added
  - a dry run changes nothing; a blocker refuses and changes nothing
  - a failed CLI step stops the run and keeps the record; a CLI that exits 0
    and does nothing is caught by the re-list guard and by the post-apply re-plan
  - dependents are uninstalled before their dependency
  - the flags, the linked-worktree refusal, and the pre-uninstall snapshot
  - a config that declines core guards does not stop removal

These drive the REAL install.sh (and the real lib/uninstall.py, linked into the
fixture) through `subprocess` against a sandbox $HOME and the claude shim.
Nothing touches the real ~/.claude, git config or plugins.
"""

import glob
import json
import os
import subprocess
import tempfile
import unittest

import install_fixture as fx
import test_install_permissions_opt_out as base
from test_install_permissions_opt_out import DENY, MERGE, PUSH

REPO_ROOT = base.REPO_ROOT
LIB = os.path.join(REPO_ROOT, "lib")
PLUGIN = "toolkit-core@claude-code-toolkit"
CONTINUITY = "toolkit-continuity@claude-code-toolkit"


def make_fixture_repo(root):
    """The opt-out suite's fixture plus what uninstall needs: the real lib
    modules it imports, a policy file, and each fixture plugin's real
    `dependencies` (add_plugin_layout writes only `name`; premise E)."""
    repo = base.make_fixture_repo(root)
    for module in ("team_policy.py", "claude_imports.py", "auto_memory.py", "uninstall.py"):
        dest = os.path.join(repo, "lib", module)
        if not os.path.lexists(dest):
            os.symlink(os.path.join(LIB, module), dest)
    os.makedirs(os.path.join(repo, "policy"))
    with open(os.path.join(repo, "policy", "CLAUDE.md"), "w") as handle:
        handle.write("# Conventions\n")
    for entry in fx.real_profiles()["profiles"].values():
        directory = entry["dir"]
        if directory == "plugin":
            continue
        with open(os.path.join(REPO_ROOT, directory, ".claude-plugin", "plugin.json")) as handle:
            dependencies = json.load(handle).get("dependencies")
        if dependencies is None:
            continue
        path = os.path.join(repo, directory, ".claude-plugin", "plugin.json")
        with open(path) as handle:
            manifest = json.load(handle)
        manifest["dependencies"] = dependencies
        with open(path, "w") as handle:
            json.dump(manifest, handle)
    base.git("add", "-A", cwd=repo)
    base.git("commit", "-q", "-m", "uninstall fixture", cwd=repo)
    return repo


class UninstallCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.repo = make_fixture_repo(self.root)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.gitconfig = os.path.join(self.home, ".gitconfig")
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")
        self.record_file = os.path.join(self.home, ".config", "claude-code-toolkit",
                                        "install-record.json")
        self.local_bin = os.path.join(self.home, ".local", "bin")
        self.state_path = fx.shim_for_home(self.home)[1]

    # -- helpers ----------------------------------------------------------

    def env(self, extra=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_") and not k.startswith("CLAUDE_SHIM_")
               and k not in ("CLAUDE_TOOLKIT_CONFIG", "ALLOW_WORKTREE_INSTALL", "XDG_CONFIG_HOME")}
        env["HOME"] = self.home
        env["GIT_CONFIG_GLOBAL"] = self.gitconfig
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env = fx.shim_env_for_home(env, self.home)
        env.update(extra or {})
        return env

    def run_in(self, repo, args, extra=None):
        return subprocess.run([os.path.join(repo, "install.sh")] + list(args), cwd=repo,
                              env=self.env(extra), capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=180,
                              universal_newlines=True)

    def invoke(self, args, extra=None):
        return self.run_in(self.repo, args, extra)

    def out(self, result):
        return result.stdout + result.stderr

    def seed(self, config=None, settings=None):
        base.seed_home(self.home, config if config is not None else {}, settings)

    def install(self):
        result = self.invoke(())
        self.assertEqual(0, result.returncode, self.out(result))

    def uninstall(self, *flags, extra=None):
        return self.invoke(("--uninstall",) + flags, extra)

    def settings(self):
        with open(self.settings_path) as handle:
            return json.load(handle)

    def write_settings(self, data):
        with open(self.settings_path, "w") as handle:
            json.dump(data, handle, indent=2)

    def settings_bytes(self):
        with open(self.settings_path, "rb") as handle:
            return handle.read()

    def state_bytes(self):
        with open(self.state_path, "rb") as handle:
            return handle.read()

    def shim_state(self):
        with open(self.state_path) as handle:
            return json.load(handle)

    def log(self):
        return fx.shim_log(self.state_path)

    def bin_listing(self):
        found = []
        for name in sorted(os.listdir(self.local_bin)):
            path = os.path.join(self.local_bin, name)
            found.append((name, os.readlink(path) if os.path.islink(path) else None))
        return found

    def points_into_repo(self):
        repo = os.path.realpath(self.repo)
        return [name for name, target in self.bin_listing()
                if target and os.path.realpath(os.path.join(self.local_bin, target)).startswith(repo + os.sep)]

    def without_cli_empties(self, settings):
        """The CLI empties these keys; uninstall does not delete CLI-owned keys."""
        settings = dict(settings)
        for key in ("enabledPlugins", "extraKnownMarketplaces"):
            if settings.get(key) == {}:
                del settings[key]
        return settings

    def ensure_policy(self):
        result = subprocess.run(["python3", os.path.join(self.repo, "lib", "team_policy.py"),
                                 "ensure", self.repo], env=self.env(), capture_output=True,
                                stdin=subprocess.DEVNULL, universal_newlines=True)
        self.assertEqual(0, result.returncode, self.out(result))
        self.assertIn("wrote", result.stdout)

    def assert_fully_uninstalled(self):
        state = self.shim_state()
        self.assertEqual([], [m for m in state["marketplaces"] if m["name"] == "claude-code-toolkit"])
        self.assertEqual([], [p for p in state["plugins"] if p["id"].endswith("@claude-code-toolkit")])
        self.assertEqual([], self.points_into_repo())
        self.assertFalse(os.path.exists(self.record_file))
        settings = self.settings()
        self.assertEqual([], [k for k in settings.get("enabledPlugins", {}) if k.endswith("@claude-code-toolkit")])
        self.assertNotIn("claude-code-toolkit", settings.get("extraKnownMarketplaces", {}))

    def hooks_dir(self):
        return os.path.join(self.home, ".config", "claude-code-toolkit", "git-hooks")

    # -- 1 ----------------------------------------------------------------

    def test_round_trip_removes_only_what_the_installer_added(self):
        self.seed({}, {"permissions": {"allow": ["Bash(ls)"]}, "theme": "dark"})
        self.install()
        settings = self.settings()
        settings["permissions"]["allow"].append("Bash(make)")
        personal_hook = {"PostToolUse": [{"hooks": [{"type": "command",
                                                     "command": "~/bin/mine.sh"}]}]}
        settings["hooks"] = personal_hook
        self.write_settings(settings)
        with open(os.path.join(self.local_bin, "mine"), "w") as handle:
            handle.write("mine\n")
        self.ensure_policy()
        self.assertTrue(self.points_into_repo(), "positive control: install made links")

        result = self.uninstall("--yes")
        self.assertEqual(0, result.returncode, self.out(result))
        self.assertIn("Uninstalled", result.stdout)
        self.assertEqual({"permissions": {"allow": ["Bash(ls)", "Bash(make)"]},
                          "theme": "dark", "hooks": personal_hook},
                         self.without_cli_empties(self.settings()))
        self.assertTrue(os.path.exists(os.path.join(self.local_bin, "mine")))
        self.assert_fully_uninstalled()
        with open(os.path.join(self.home, ".claude", "CLAUDE.md")) as handle:
            text = handle.read()
        self.assertNotIn("policy/CLAUDE.md", text)
        self.assertNotIn(self.repo, text)

    # -- 2 ----------------------------------------------------------------

    def test_pre_existing_identical_rule_survives(self):
        self.seed({}, {"permissions": {"deny": [DENY]}})
        self.install()
        result = self.uninstall("--yes")
        self.assertEqual(0, result.returncode, self.out(result))
        perms = self.settings()["permissions"]
        self.assertIn(DENY, perms["deny"])
        self.assertNotIn(PUSH, perms.get("ask", []))
        self.assertNotIn(MERGE, perms.get("ask", []))

    # -- 3 ----------------------------------------------------------------

    def assert_all_rules_present(self):
        perms = self.settings()["permissions"]
        self.assertIn(DENY, perms["deny"])
        self.assertIn(PUSH, perms["ask"])
        self.assertIn(MERGE, perms["ask"])

    def test_no_record_keeps_every_permission_rule(self):
        self.seed()
        self.install()
        os.remove(self.record_file)
        dry = self.uninstall()
        self.assertEqual(0, dry.returncode, self.out(dry))
        self.assertIn("ownership unknown", dry.stdout)
        result = self.uninstall("--yes")
        self.assertEqual(0, result.returncode, self.out(result))
        self.assert_all_rules_present()

    def test_converged_finish_only_record_keeps_every_permission_rule(self):
        self.seed()
        self.install()
        with open(self.record_file) as handle:
            record = json.load(handle)
        record["permissions_added"] = {}
        with open(self.record_file, "w") as handle:
            json.dump(record, handle)
        dry = self.uninstall()
        self.assertEqual(0, dry.returncode, self.out(dry))
        left = dry.stdout[dry.stdout.index("Left in place"):]
        left = left[:left.index("Blocked")]
        for rule in (DENY, PUSH, MERGE):
            self.assertIn(rule, left)
        self.assertIn("not recorded", left)
        result = self.uninstall("--yes")
        self.assertEqual(0, result.returncode, self.out(result))
        self.assert_all_rules_present()

    # -- 4 ----------------------------------------------------------------

    def test_dry_run_changes_nothing(self):
        self.seed()
        self.install()
        before = (self.settings_bytes(), self.bin_listing(), self.state_bytes())
        result = self.uninstall()
        self.assertEqual(0, result.returncode, self.out(result))
        self.assertIn("Dry run: nothing changed. Re-run with --uninstall --yes to apply.",
                      result.stdout)
        self.assertIn("Will remove", result.stdout)
        self.assertEqual(before, (self.settings_bytes(), self.bin_listing(), self.state_bytes()))
        self.assertTrue(os.path.exists(self.record_file))

    # -- 5 ----------------------------------------------------------------

    def assert_refused(self, result, before):
        self.assertEqual(1, result.returncode, self.out(result))
        self.assertIn("Refusing", result.stdout)
        self.assertEqual(before, (self.settings_bytes(), self.state_bytes(), self.bin_listing()))
        self.assertTrue(os.path.exists(self.record_file))

    def test_edited_status_line_blocks(self):
        self.seed()
        self.install()
        settings = self.settings()
        self.assertIn("statusLine", settings, "positive control: install set a statusLine")
        settings["statusLine"]["padding"] = 1
        self.write_settings(settings)
        before = (self.settings_bytes(), self.state_bytes(), self.bin_listing())
        self.assert_refused(self.uninstall("--yes"), before)

    def test_foreign_file_in_git_hooks_dir_blocks(self):
        self.seed()
        self.install()
        os.makedirs(self.hooks_dir())
        os.symlink(os.path.join(self.repo, "tools", "git-pre-commit"),
                   os.path.join(self.hooks_dir(), "pre-commit"))
        with open(os.path.join(self.hooks_dir(), "post-merge"), "w") as handle:
            handle.write("#!/bin/sh\n")
        with open(self.gitconfig, "w") as handle:
            handle.write("[core]\n\thooksPath = %s\n" % self.hooks_dir())
        before = (self.settings_bytes(), self.state_bytes(), self.bin_listing())
        result = self.uninstall("--yes")
        self.assert_refused(result, before)
        self.assertIn("post-merge", result.stdout)
        self.assertTrue(os.path.islink(os.path.join(self.hooks_dir(), "pre-commit")))

    def test_project_scope_install_blocks(self):
        self.seed()
        self.install()
        state = self.shim_state()
        state["plugins"].append({"id": PLUGIN, "enabled": True, "scope": "project",
                                 "installPath": "/shim/" + PLUGIN})
        with open(self.state_path, "w") as handle:
            json.dump(state, handle)
        before = (self.settings_bytes(), self.state_bytes(), self.bin_listing())
        result = self.uninstall("--yes")
        self.assert_refused(result, before)
        self.assertIn("project", result.stdout)

    # -- 6 ----------------------------------------------------------------

    def test_partial_failure_stops_keeps_the_record_and_a_rerun_finishes(self):
        self.seed()
        self.install()
        seen = len(self.log())
        result = self.uninstall("--yes", extra={"CLAUDE_SHIM_FAIL": CONTINUITY})
        self.assertEqual(1, result.returncode, self.out(result))
        self.assertIn("incomplete", result.stdout)
        self.assertTrue(os.path.exists(self.record_file))
        calls = self.log()[seen:]
        self.assertTrue(any("plugin uninstall " + CONTINUITY in line for line in calls),
                        "positive control: the failing call was made")
        self.assertFalse([l for l in calls if l.startswith("plugin uninstall " + PLUGIN)], calls)
        self.assertFalse([l for l in calls if l.startswith("plugin marketplace remove")], calls)
        self.assert_all_rules_present()
        again = self.uninstall("--yes")
        self.assertEqual(0, again.returncode, self.out(again))
        self.assert_fully_uninstalled()

    # -- 7 ----------------------------------------------------------------

    def test_silent_noop_on_a_plugin_is_caught_before_the_marketplace(self):
        self.seed()
        self.install()
        seen = len(self.log())
        result = self.uninstall("--yes", extra={"CLAUDE_SHIM_NOOP": CONTINUITY})
        self.assertEqual(1, result.returncode, self.out(result))
        self.assertIn(CONTINUITY, self.out(result))
        self.assertTrue(os.path.exists(self.record_file))
        calls = self.log()[seen:]
        self.assertTrue(any("plugin uninstall " + CONTINUITY in line for line in calls),
                        "positive control: the no-op call was made")
        self.assertFalse([l for l in calls if l.startswith("plugin marketplace remove")], calls)
        again = self.uninstall("--yes")
        self.assertEqual(0, again.returncode, self.out(again))
        self.assert_fully_uninstalled()

    def test_silent_noop_on_the_marketplace_is_caught_by_the_replan(self):
        self.seed()
        self.install()
        seen = len(self.log())
        result = self.uninstall("--yes", extra={"CLAUDE_SHIM_NOOP": "claude-code-toolkit"})
        self.assertEqual(1, result.returncode, self.out(result))
        self.assertIn("incomplete", result.stdout)
        self.assertTrue(os.path.exists(self.record_file))
        calls = self.log()[seen:]
        self.assertTrue(any(l.startswith("plugin marketplace remove claude-code-toolkit") for l in calls),
                        "positive control: apply reached the no-op marketplace call")
        again = self.uninstall("--yes")
        self.assertEqual(0, again.returncode, self.out(again))
        self.assert_fully_uninstalled()

    # -- 8 ----------------------------------------------------------------

    def test_dependents_are_uninstalled_before_their_dependency(self):
        self.seed({"profiles": ["continuity", "builder", "maintainer"]})
        self.install()
        self.assertIn("toolkit-maintainer@claude-code-toolkit",
                      [p["id"] for p in self.shim_state()["plugins"]])
        seen = len(self.log())
        result = self.uninstall("--yes")
        self.assertEqual(0, result.returncode, self.out(result))
        calls = self.log()[seen:]
        uninstalls = [l.split()[2] for l in calls if l.startswith("plugin uninstall ")]
        self.assertLess(uninstalls.index("toolkit-maintainer@claude-code-toolkit"),
                        uninstalls.index("toolkit-build@claude-code-toolkit"))
        self.assertEqual(PLUGIN, uninstalls[-1])
        self.assertEqual(4, len(uninstalls), uninstalls)
        last_uninstall = max(i for i, l in enumerate(calls) if l.startswith("plugin uninstall "))
        removes = [i for i, l in enumerate(calls) if l.startswith("plugin marketplace remove")]
        self.assertTrue(removes and min(removes) > last_uninstall, calls)

    # -- 9 ----------------------------------------------------------------

    def make_target(self):
        target = os.path.join(self.repo, "tools", "x", "claude-x")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as handle:
            handle.write("x\n")
        return target

    def test_home_bin_link_with_matching_name_is_removed(self):
        self.seed()
        self.install()
        target = self.make_target()
        os.makedirs(os.path.join(self.home, "bin"))
        link = os.path.join(self.home, "bin", "claude-x")
        os.symlink(target, link)
        result = self.uninstall("--yes")
        self.assertEqual(0, result.returncode, self.out(result))
        self.assertFalse(os.path.lexists(link))

    def test_home_bin_alias_is_a_blocker(self):
        self.seed()
        self.install()
        target = self.make_target()
        os.makedirs(os.path.join(self.home, "bin"))
        os.symlink(target, os.path.join(self.home, "bin", "alias"))
        result = self.uninstall()
        self.assertEqual(1, result.returncode, self.out(result))
        blocked = result.stdout[result.stdout.index("Blocked"):]
        self.assertIn("alias", blocked)

    # -- 10 ---------------------------------------------------------------

    def test_flags(self):
        for args in (("--yes",), ("--uninstall", "--check"), ("--uninstall", "--yes", "extra"),
                     ("--check", "--yes"), ("--uninstall", "extra"), ("--help", "--yes"),
                     ("-h", "--yes"), ("--yes", "--uninstall")):
            result = self.invoke(args)
            self.assertEqual(2, result.returncode, (args, self.out(result)))
        self.assertFalse(os.path.exists(self.record_file))

    def test_linked_worktree_refuses_even_a_dry_run(self):
        self.seed()
        wt = os.path.join(self.root, "wt")
        base.git("worktree", "add", "-q", "-b", "wt", wt, cwd=self.repo)
        result = self.run_in(wt, ("--uninstall",))
        self.assertEqual(1, result.returncode, self.out(result))
        self.assertIn("refusing", self.out(result))

    def test_a_config_declining_core_does_not_stop_removal(self):
        self.seed()
        self.install()
        config = os.path.join(self.home, ".config", "claude-code-toolkit", "config.json")
        with open(config) as handle:
            data = json.load(handle)
        data["danger_guard"] = {"installed": False}
        with open(config, "w") as handle:
            json.dump(data, handle)
        refused = self.invoke(())
        self.assertEqual(1, refused.returncode, self.out(refused))
        self.assertIn("declines guards this toolkit's core always installs", refused.stderr,
                      "positive control: this config stops a plain install")
        result = self.uninstall("--yes")
        self.assertEqual(0, result.returncode, self.out(result))
        self.assertNotIn("declines guards", self.out(result))
        self.assert_fully_uninstalled()

    # -- 11 ---------------------------------------------------------------

    def test_yes_snapshots_the_settings_first(self):
        self.seed()
        self.install()
        before = self.settings_bytes()
        pattern = os.path.join(self.home, ".claude", "settings.json.claude-code-toolkit-*.bak")

        def holds(bytes_):
            found = []
            for path in glob.glob(pattern):
                with open(path, "rb") as handle:
                    if handle.read() == bytes_:
                        found.append(path)
            return found

        self.assertEqual([], holds(before), "no snapshot of these bytes exists yet")
        result = self.uninstall("--yes")
        self.assertEqual(0, result.returncode, self.out(result))
        self.assertEqual(1, len(holds(before)))
        self.assertNotEqual(before, self.settings_bytes())

    def test_the_always_kept_snapshot_paths_are_the_files_the_installer_writes(self):
        self.seed({}, {"theme": "dark"})
        self.install()
        result = self.uninstall("--yes")
        self.assertEqual(0, result.returncode, self.out(result))
        kept = result.stdout[result.stdout.index("Always kept"):].splitlines()
        first = [l for l in kept if "settings snapshots:" in l][0].split("settings snapshots: ", 1)[1]
        timed = [l for l in kept if l.strip().startswith("and ")][0].strip()[len("and "):]
        self.assertTrue(os.path.isfile(first), "the printed pre-install snapshot exists: %s" % first)
        self.assertTrue(glob.glob(timed), "the printed pattern matches a timed snapshot: %s" % timed)

    def test_orphaned_plugin_without_its_marketplace_refuses(self):
        self.seed()
        self.install()
        state = self.shim_state()
        state["marketplaces"] = [m for m in state["marketplaces"] if m["name"] != "claude-code-toolkit"]
        self.assertTrue([p for p in state["plugins"] if p["id"] == PLUGIN], "positive control")
        with open(self.state_path, "w") as handle:
            json.dump(state, handle)
        result = self.uninstall("--yes")
        self.assertEqual(1, result.returncode, self.out(result))
        self.assertIn("marketplace is gone", result.stdout)
        self.assertNotIn("Uninstalled", self.out(result))

    def test_crashed_preflight_fails_closed(self):
        # A preflight that neither passes (0) nor reports blockers (1) must stop
        # the run: the --yes apply never starts, and no snapshot is taken.
        self.seed()
        self.install()
        stub = os.path.join(self.repo, "lib", "uninstall.py")
        marker = os.path.join(self.root, "applied")
        os.remove(stub)
        with open(stub, "w") as handle:
            handle.write("import sys\n"
                         "if '--preflight' in sys.argv: sys.exit(3)\n"
                         "open(%r, 'w').close()\n" % marker)
        directory = os.path.join(self.home, ".claude")
        before = sorted(glob.glob(os.path.join(directory, "settings.json.claude-code-toolkit-*.bak")))
        result = self.uninstall("--yes")
        self.assertEqual(1, result.returncode, self.out(result))
        self.assertIn("preflight failed", result.stderr)
        self.assertFalse(os.path.exists(marker), "the --yes run must not start")
        self.assertEqual(before, sorted(glob.glob(
            os.path.join(directory, "settings.json.claude-code-toolkit-*.bak"))))

    def test_blocked_yes_takes_no_snapshot(self):
        self.seed()
        self.install()
        settings = self.settings()
        settings["statusLine"]["padding"] = 1
        self.write_settings(settings)
        directory = os.path.join(self.home, ".claude")
        for number in range(10):
            path = os.path.join(directory, "settings.json.claude-code-toolkit-2020010%dT000000Z.bak" % number)
            with open(path, "w") as handle:
                handle.write("old %d" % number)

        def listing():
            found = []
            for path in sorted(glob.glob(os.path.join(directory, "settings.json.claude-code-toolkit-*.bak"))):
                with open(path, "rb") as handle:
                    found.append((path, handle.read()))
            return found

        before = listing()
        self.assertGreaterEqual(len(before), 10, "positive control")
        result = self.uninstall("--yes")
        self.assertEqual(1, result.returncode, self.out(result))
        self.assertIn("Refusing", result.stdout)
        self.assertEqual(before, listing())


if __name__ == "__main__":
    unittest.main()
