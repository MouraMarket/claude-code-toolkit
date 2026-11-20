#!/usr/bin/env python3
"""Behavioural tests for install.sh's plugin step.

install.sh registers the `toolkit-core` plugin through the `claude` CLI and
removes ONLY the hook commands the old settings template ever wrote (the exact
strings in `claude/legacy-hook-commands.json`). These drive the REAL install.sh
through `subprocess` against a minimal fixture repo, a sandbox HOME and the
shared `claude` shim from `install_fixture.py`, and assert on three things at
once: the exit code, the settings file, and the shim's argv log.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import install_fixture as fx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
WORKTREE_DETECT_SH = os.path.join(REPO_ROOT, "lib", "worktree_detect.sh")
FROZEN = os.path.join(REPO_ROOT, "lib", "tests", "fixtures", "settings.hooks.legacy.json")

ADD = "plugin marketplace add "
REMOVE = "plugin marketplace remove claude-code-toolkit"
INSTALL = "plugin install toolkit-core@claude-code-toolkit --scope user"
ENABLE = "plugin enable toolkit-core@claude-code-toolkit --scope user"


class GitFixtureError(subprocess.CalledProcessError):
    """A failed fixture git call. The base message omits stderr, which is
    where git says why, so this one carries the cwd, stderr and stdout."""

    cwd = None

    def __str__(self):
        lines = [super().__str__(), "cwd: %s" % self.cwd,
                 "stderr: %s" % ((self.stderr or "").strip() or "(no stderr)")]
        if self.output:
            lines.append("stdout: %s" % self.output.strip())
        return "\n".join(lines)


def git(*args, cwd):
    env = dict(os.environ)
    for key in [k for k in env if k.startswith("GIT_")]:
        del env[key]
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    cmd = ["git"] + list(args)
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, env=env,
                          universal_newlines=True)
    if done.returncode != 0:
        error = GitFixtureError(done.returncode, cmd, output=done.stdout,
                                stderr=done.stderr)
        error.cwd = cwd
        raise error
    return done


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


class PluginInstallTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = os.path.join(self.root, "home")
        self.bin_dir = os.path.join(self.root, "shimbin")
        self.state = os.path.join(self.root, "shim-state.json")
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.repo, "claude"))
        os.makedirs(os.path.join(self.repo, "tools"))
        os.makedirs(os.path.join(self.repo, "lib"))
        os.symlink(INSTALL_SH, os.path.join(self.repo, "install.sh"))
        os.symlink(WORKTREE_DETECT_SH, os.path.join(self.repo, "lib", "worktree_detect.sh"))
        with open(os.path.join(self.repo, "claude", "permissions.baseline.json"), "w") as handle:
            handle.write('{"permissions": {}}\n')
        fx.add_plugin_layout(self.repo)
        # A link whose source is missing from the repo is a required failure.
        fx.add_link_targets(self.repo, self.home)
        git("init", "-q", "-b", "main", self.repo, cwd=self.root)
        git("config", "user.email", "t@example.com", cwd=self.repo)
        git("config", "user.name", "Test", cwd=self.repo)
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "init", cwd=self.repo)
        fx.install_claude_shim(self.bin_dir, self.state)
        # A valid empty config so first-time setup never prompts.
        config_dir = os.path.join(self.home, ".config", "claude-code-toolkit")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "config.json"), "w") as handle:
            json.dump({}, handle)
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")

    # -- helpers ----------------------------------------------------------

    def write_settings(self, data=None, raw=None):
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        with open(self.settings_path, "wb") as handle:
            handle.write(raw if raw is not None else json.dumps(data, indent=2).encode())
        return read(self.settings_path)

    def settings(self):
        with open(self.settings_path) as handle:
            return json.load(handle)

    def run_install(self, args=(), extra_env=None, path=None):
        env = dict(os.environ)
        env["HOME"] = self.home
        env["USERPROFILE"] = self.home
        env["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent.json"
        for key in [k for k in env if k.startswith("GIT_")]:
            del env[key]
        env.pop("ALLOW_WORKTREE_INSTALL", None)
        env = fx.shim_env(env, self.bin_dir, self.state)
        if path is not None:
            env["PATH"] = path
        env.update(extra_env or {})
        return subprocess.run([os.path.join(self.repo, "install.sh")] + list(args),
                              cwd=self.repo, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)

    def log(self):
        return fx.shim_log(self.state)

    def legacy_hooks(self):
        with open(FROZEN) as handle:
            hooks = json.load(handle)["hooks"]
        return json.loads(json.dumps(hooks).replace("__HOME__", self.home))

    def link_into_repo_from_home_bin(self):
        """The shape the `~/bin` prune deletes: a link into the repo whose
        basename equals its target's."""
        os.makedirs(os.path.join(self.home, "bin"))
        link = os.path.join(self.home, "bin", "claude-retired")
        os.symlink(os.path.join(self.repo, "tools", "retired", "claude-retired"), link)
        return link

    def expected_status_line(self):
        return json.loads(json.dumps(fx.real_host_settings()["statusLine"])
                          .replace("__HOME__", self.home))

    def assertNoRegistrationCalls(self):
        for line in self.log():
            self.assertFalse(line.startswith(ADD) or line.startswith("plugin install"),
                             "unexpected registration call: %s" % line)


class TestFreshInstall(PluginInstallTestCase):

    def test_registers_and_installs_and_sets_the_status_line(self):
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        log = self.log()
        add = ADD + self.repo
        self.assertIn(add, log)
        self.assertIn(INSTALL, log)
        self.assertLess(log.index(add), log.index(INSTALL))
        settings = self.settings()
        self.assertNotIn("hooks", settings)
        # The statusLine runs claude-usage-line, linked by the same run, with
        # __HOME__ expanded: a literal __HOME__ names no file and the status
        # line fails silently every session.
        self.assertEqual(self.expected_status_line(), settings.get("statusLine"))
        self.assertNotIn("__HOME__", json.dumps(settings["statusLine"]))
        self.assertIn("Running sessions keep their old hooks until /reload-plugins",
                      proc.stdout)


class TestStatusLineStep(PluginInstallTestCase):
    """The statusLine is the only surface Claude Code hands live rate limits
    to, so the installer adds its own when there is none and NEVER replaces
    one that is somebody else's."""

    def test_a_custom_status_line_is_kept_and_the_cost_is_named(self):
        mine = {"type": "command", "command": "echo mine"}
        self.write_settings({"statusLine": mine})
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(mine, self.settings()["statusLine"])
        self.assertIn("statusLine is yours and was left alone", proc.stdout)
        self.assertIn("claude-usage-line", proc.stdout)

    def test_a_matching_status_line_is_reported_ok_and_not_rewritten(self):
        self.write_settings({"statusLine": self.expected_status_line()})
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertNotIn("statusLine (adding)", proc.stdout)
        self.assertRegex(proc.stdout, r"OK\S* statusLine\n")
        self.assertEqual(self.expected_status_line(), self.settings()["statusLine"])

    def test_check_reports_the_addition_and_writes_nothing(self):
        before = self.write_settings({"theme": "dark"})
        proc = self.run_install(["--check"])
        self.assertIn("statusLine (adding)", proc.stdout)
        self.assertEqual(before, read(self.settings_path))


class TestLegacyRemoval(PluginInstallTestCase):

    def test_full_legacy_set_is_removed_and_snapshot_is_pre_run(self):
        hooks = self.legacy_hooks()
        self.assertTrue(hooks)
        pre = self.write_settings({"theme": "dark", "hooks": hooks})
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        settings = self.settings()
        self.assertNotIn("hooks", settings)
        self.assertEqual("dark", settings["theme"])
        # Taken before the CLI wrote extraKnownMarketplaces/enabledPlugins.
        snapshot = self.settings_path + ".pre-claude-code-toolkit.bak"
        self.assertEqual(pre, read(snapshot))
        # Immutable: a second run (settings now differ) must not overwrite it.
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(pre, read(snapshot))
        self.assertIn("extraKnownMarketplaces", settings)
        self.assertIn(INSTALL, self.log())

    def test_foreign_handler_and_event_survive_beside_legacy(self):
        legacy = self.legacy_hooks()["PreToolUse"][0]["hooks"]
        self.assertGreaterEqual(len(legacy), 1)
        # Under $HOME and named like ours, on purpose: another person's own tool in
        # ~/.local/bin is the case a prefix or name-pattern match would delete.
        own = os.path.join(self.home, ".local", "bin", "claude-my-own-guard")
        foreign = {"type": "command", "command": own, "timeout": 5}
        notify = [{"hooks": [{"type": "command", "command": "/me/bin/notify"}]}]
        self.write_settings({"hooks": {
            "PreToolUse": [{"matcher": "Bash",
                            "hooks": [legacy[0], foreign] + legacy[1:]}],
            "Notification": notify}})
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        hooks = self.settings()["hooks"]
        self.assertEqual([{"matcher": "Bash", "hooks": [foreign]}], hooks["PreToolUse"])
        self.assertEqual(notify, hooks["Notification"])
        self.assertEqual({"PreToolUse", "Notification"}, set(hooks))
        self.assertIn("kept PreToolUse: " + own, proc.stdout)
        for command in [h["command"] for h in legacy]:
            self.assertIn("removed PreToolUse hook entry from settings.json "
                          "(no file is deleted): " + command, proc.stdout)

    def test_duplicate_legacy_entries_print_once_with_a_count(self):
        # H4 F15: the same command under three matchers printed three bare
        # "removed <path>" lines, read as the guard file being deleted.
        command = self.legacy_hooks()["PreToolUse"][0]["hooks"][0]["command"]
        handler = {"type": "command", "command": command}
        self.write_settings({"hooks": {"PreToolUse": [
            {"matcher": m, "hooks": [handler]} for m in ("Bash", "Edit", "Write")]}})
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertNotIn("hooks", self.settings())
        line = ("removed PreToolUse hook entry from settings.json "
                "(no file is deleted): %s (x3)" % command)
        self.assertEqual(1, proc.stdout.count(line), proc.stdout)
        self.assertEqual(1, proc.stdout.count(command), proc.stdout)

    def test_home_bin_spellings_are_removed_too(self):
        with open(os.path.join(REPO_ROOT, "claude", "legacy-hook-commands.json")) as handle:
            spellings = [c.replace("__HOME__", self.home)
                         for c in json.load(handle)["commands"]
                         if c.startswith("__HOME__/bin/")]
        self.assertEqual(6, len(spellings))
        self.write_settings({"hooks": {"PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": c}
                                          for c in spellings]}]}})
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertNotIn("hooks", self.settings())
        for command in spellings:
            self.assertIn("hook entry from settings.json (no file is deleted): " + command,
                          proc.stdout)

    def test_malformed_matcher_block_is_kept_as_is(self):
        legacy = self.legacy_hooks()["PreToolUse"][0]["hooks"][0]
        malformed = {"matcher": "Bash", "hooks": {"not": "a list"}}
        self.write_settings({"hooks": {"PreToolUse": [
            malformed, {"matcher": "*", "hooks": [legacy]}]}})
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([malformed], self.settings()["hooks"]["PreToolUse"])


class TestAlreadyMigrated(PluginInstallTestCase):

    def test_second_run_changes_nothing_and_registers_nothing(self):
        self.write_settings({"theme": "dark"})
        first = self.run_install()
        self.assertEqual(0, first.returncode, first.stdout + first.stderr)
        after_first = read(self.settings_path)
        os.utime(self.settings_path, (1_000_000_000, 1_000_000_000))
        before = os.stat(self.settings_path).st_mtime_ns
        calls = len(self.log())
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(after_first, read(self.settings_path))
        self.assertEqual(before, os.stat(self.settings_path).st_mtime_ns)
        for line in self.log()[calls:]:
            self.assertFalse(line.startswith(ADD) or line.startswith("plugin install"),
                             "second run registered again: %s" % line)
        self.assertIn("nothing to remove", proc.stdout)


class TestRegistrationRepair(PluginInstallTestCase):

    def test_marketplace_at_another_path_is_removed_then_added(self):
        other = os.path.join(self.root, "elsewhere")
        os.makedirs(other)
        fx.seed_installed(self.state, other)
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        log = self.log()
        add = ADD + self.repo
        self.assertIn(REMOVE, log)
        self.assertIn(add, log)
        self.assertLess(log.index(REMOVE), log.index(add))

    def test_disabled_plugin_is_enabled(self):
        fx.seed_installed(self.state, self.repo)
        with open(self.state) as handle:
            state = json.load(handle)
        state["plugins"][0]["enabled"] = False
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn(ENABLE, self.log())
        self.assertNoRegistrationCalls()


class TestProjectScopedEntryIsNotTheUsers(PluginInstallTestCase):
    """`plugin list --json` also reports project-scoped installs. Only a
    user-scoped entry puts hooks on THIS machine's every session."""

    def test_project_scoped_only_entry_gets_a_user_scope_install(self):
        fx.seed_project_scoped(self.state, self.repo)
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn(INSTALL, self.log())
        with open(self.state) as handle:
            plugins = json.load(handle)["plugins"]
        # Core's project-scoped entry stays and gains a user-scoped one; the
        # default selection also installs continuity at user scope.
        core_scopes = sorted(p["scope"] for p in plugins if p["id"] == fx.PLUGIN_ID)
        self.assertEqual(["project", "user"], core_scopes)
        self.assertEqual(["user"], [p["scope"] for p in plugins
                                    if p["id"] == "toolkit-continuity@claude-code-toolkit"])

    def test_project_scoped_only_with_install_that_changes_nothing_fails(self):
        hooks = self.legacy_hooks()
        pre = self.write_settings({"hooks": hooks})
        fx.seed_project_scoped(self.state, self.repo)
        proc = self.run_install(extra_env={"CLAUDE_SHIM_NOOP": "install"})
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn(INSTALL, self.log())
        self.assertIn("is not installed and enabled", proc.stdout)
        self.assertEqual(hooks, self.settings()["hooks"])
        self.assertEqual(pre, read(self.settings_path + ".pre-claude-code-toolkit.bak"))

    def test_disabled_user_entry_is_enabled_at_user_scope(self):
        fx.seed_installed(self.state, self.repo)
        with open(self.state) as handle:
            state = json.load(handle)
        state["plugins"][0]["enabled"] = False
        state["plugins"].append({"id": fx.PLUGIN_ID, "enabled": True,
                                 "scope": "project", "installPath": "/shim/p"})
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn(ENABLE, self.log())


class TestFailuresLeaveHooksAlone(PluginInstallTestCase):

    def test_no_claude_on_path_exits_before_the_prunes(self):
        pre = self.write_settings({"hooks": self.legacy_hooks()})
        link = self.link_into_repo_from_home_bin()
        proc = self.run_install(path=fx.path_without_claude(self.root))
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(pre, read(self.settings_path))
        self.assertEqual([], self.log())
        self.assertTrue(os.path.islink(link), "the ~/bin prune ran after the refusal")
        self.assertIn("claude CLI not on PATH", proc.stdout)

    def assert_failed_cleanly(self, extra_env):
        hooks = self.legacy_hooks()
        pre = self.write_settings({"hooks": hooks})
        link = self.link_into_repo_from_home_bin()
        proc = self.run_install(extra_env=extra_env)
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(hooks, self.settings()["hooks"])
        self.assertEqual(pre, read(self.settings_path + ".pre-claude-code-toolkit.bak"))
        self.assertTrue(os.path.islink(link), "the ~/bin prune ran after the failure")
        return proc

    def test_add_failure(self):
        proc = self.assert_failed_cleanly({"CLAUDE_SHIM_FAIL": "add"})
        self.assertIn("marketplace add", proc.stdout)
        self.assertNotIn(INSTALL, self.log())

    def test_install_failure(self):
        proc = self.assert_failed_cleanly({"CLAUDE_SHIM_FAIL": "install"})
        self.assertIn("plugin install", proc.stdout)

    def test_unparseable_json(self):
        proc = self.assert_failed_cleanly({"CLAUDE_SHIM_BAD_JSON": "1"})
        self.assertIn("unparseable JSON", proc.stdout)

    def test_cli_that_claims_success_but_installs_nothing(self):
        proc = self.assert_failed_cleanly({"CLAUDE_SHIM_NOOP": "install"})
        self.assertIn(INSTALL, self.log())
        self.assertIn("is not installed and enabled", proc.stdout)

    def test_invalid_json_settings_are_refused_untouched(self):
        pre = self.write_settings(raw=b'{"hooks": ')
        proc = self.run_install()
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(pre, read(self.settings_path))
        self.assertIn("not valid JSON", proc.stdout)


class ShimUninstallTests(unittest.TestCase):
    """The shim's `plugin uninstall` mirrors the real CLI (scope-aware, no dependency check)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = os.path.join(self.root, "home")
        self.bin_dir = os.path.join(self.root, "shimbin")
        self.state = os.path.join(self.root, "shim-state.json")
        fx.install_claude_shim(self.bin_dir, self.state)
        fx.seed_plugins(self.state, ["m"], [("a@m", True, "user"), ("b@m", True, "user"),
                                            ("a@m", True, "project")])
        os.makedirs(os.path.join(self.home, ".claude"))
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")
        with open(self.settings_path, "w") as handle:
            json.dump({"enabledPlugins": {"a@m": True, "b@m": True}}, handle)

    def claude(self, *args):
        env = dict(os.environ)
        env["HOME"] = self.home
        env = fx.shim_env(env, self.bin_dir, self.state)
        return subprocess.run([os.path.join(self.bin_dir, "claude")] + list(args),
                              env=env, capture_output=True, stdin=subprocess.DEVNULL,
                              universal_newlines=True)

    def plugins(self):
        with open(self.state) as handle:
            return sorted((p["id"], p["scope"]) for p in json.load(handle)["plugins"])

    def test_uninstall_user_scope_only(self):
        result = self.claude("plugin", "uninstall", "a@m", "--scope", "user")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Successfully uninstalled plugin: a@m", result.stdout)
        self.assertEqual(self.plugins(), [("a@m", "project"), ("b@m", "user")])
        with open(self.settings_path) as handle:
            self.assertEqual(json.load(handle)["enabledPlugins"], {"b@m": True})

    def test_uninstall_absent_id_exits_1(self):
        result = self.claude("plugin", "uninstall", "zzz@m", "--scope", "user")
        self.assertEqual(result.returncode, 1)
        self.assertIn("plugin not installed", result.stderr)
        self.assertEqual(len(self.plugins()), 3)

    def test_remove_alias_and_default_scope(self):
        result = self.claude("plugin", "remove", "b@m")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.plugins(), [("a@m", "project"), ("a@m", "user")])


class TestPluginVersionRefresh(PluginInstallTestCase):
    """`claude plugin list` reports the commit a directory-marketplace plugin
    was installed at and keeps it until `claude plugin update`, while the hooks
    run from the checkout. So after --update/--rollback the obvious check shows
    the old version (row 75). install.sh refreshes a plugin whose listed
    commit is not the checkout's, and never fails the run over it."""

    CONTINUITY = "toolkit-continuity@claude-code-toolkit"
    STALE = "0000deadbeef"

    def setUp(self):
        super().setUp()
        self.head12 = git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()[:12]

    def seed(self, version, enabled=True):
        fx.seed_installed(self.state, self.repo)
        with open(self.state) as handle:
            state = json.load(handle)
        for plugin in state["plugins"]:
            plugin["enabled"] = enabled
            if version is not None:
                plugin["version"] = version
        with open(self.state, "w") as handle:
            json.dump(state, handle)

    def versions(self):
        with open(self.state) as handle:
            return {p["id"]: p.get("version") for p in json.load(handle)["plugins"]}

    def latest(self):
        return {"CLAUDE_SHIM_LATEST": json.dumps(
            {fx.PLUGIN_ID: self.head12, self.CONTINUITY: self.head12})}

    def updates(self):
        return [line for line in self.log() if line.startswith("plugin update")]

    def test_a_stale_plugin_is_refreshed_to_the_checkout(self):
        self.seed(self.STALE)
        proc = self.run_install(extra_env=self.latest())
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(sorted(["plugin update %s --scope user" % fx.PLUGIN_ID,
                                 "plugin update %s --scope user" % self.CONTINUITY]),
                         sorted(self.updates()))
        self.assertEqual({fx.PLUGIN_ID: self.head12, self.CONTINUITY: self.head12},
                         self.versions())
        self.assertIn("refreshing plugin %s (shows %s, the checkout is at %s)"
                      % (fx.PLUGIN_ID, self.STALE, self.head12), proc.stdout)
        self.assertNotIn("still shows", proc.stdout)

    def test_a_disabled_stale_plugin_is_enabled_then_refreshed(self):
        self.seed(self.STALE, enabled=False)
        proc = self.run_install(extra_env=self.latest())
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        log = self.log()
        self.assertIn(ENABLE, log)
        self.assertIn("plugin update %s --scope user" % fx.PLUGIN_ID, log)
        self.assertLess(log.index(ENABLE), log.index("plugin update %s --scope user" % fx.PLUGIN_ID))
        self.assertEqual(self.head12, self.versions()[fx.PLUGIN_ID])

    def test_a_plugin_at_the_checkout_commit_is_not_refreshed(self):
        self.seed(self.head12)
        proc = self.run_install(extra_env=self.latest())
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.updates())

    def test_no_reported_version_means_no_refresh(self):
        # The shim's own default, and an older CLI's shape: nothing to compare.
        self.seed(None)
        proc = self.run_install(extra_env=self.latest())
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.updates())

    def test_a_manifest_version_is_not_compared(self):
        self.seed("1.2.0")
        proc = self.run_install(extra_env=self.latest())
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.updates())

    def test_check_reports_the_stale_version_and_refreshes_nothing(self):
        self.seed(self.STALE)
        proc = self.run_install(["--check"], extra_env=self.latest())
        self.assertIn("plugin %s shows %s, the checkout is at %s (would refresh)"
                      % (fx.PLUGIN_ID, self.STALE, self.head12), proc.stdout)
        self.assertEqual([], self.updates())
        self.assertEqual(self.STALE, self.versions()[fx.PLUGIN_ID])

    def test_a_refused_refresh_is_reported_and_the_install_succeeds(self):
        self.seed(self.STALE)
        proc = self.run_install(extra_env=dict(self.latest(), CLAUDE_SHIM_FAIL="update"))
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("until you run: claude plugin update %s --scope user" % fx.PLUGIN_ID,
                      proc.stdout)
        self.assertEqual(self.STALE, self.versions()[fx.PLUGIN_ID])

    def test_a_refresh_that_changes_nothing_is_reported(self):
        # The CLI says it updated, but the listing still shows the old commit.
        self.seed(self.STALE)
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("plugin %s still shows %s after refreshing it, not the checkout's %s"
                      % (fx.PLUGIN_ID, self.STALE, self.head12), proc.stdout)


if __name__ == "__main__":
    unittest.main()
