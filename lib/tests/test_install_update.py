#!/usr/bin/env python3
"""`./install.sh --version | --update | --rollback`, through the REAL installer.

The shared fixture symlinks install.sh to the real installer, so setUp first
swaps in a regular-file copy (and asserts it) -- otherwise a test that pushes a
modified install.sh would write through to the repo's own installer. The fixture
repo is then made a clone of a bare origin, and a second clone (dev) pushes the
new commits an update picks up.
"""

import json
import os
import shutil
import subprocess
import sys
import unittest

import install_fixture as fx
from test_install_check_fails import CheckVerdictTestCase
from test_install_plugin import INSTALL_SH, git, read


def head(repo):
    return git("rev-parse", "HEAD", cwd=repo).stdout.strip()


def record_of(home):
    path = os.path.join(home, ".config", "claude-code-toolkit", "install-record.json")
    with open(path) as handle:
        return json.load(handle)


class UpdateTestCase(CheckVerdictTestCase):

    def setUp(self):
        super().setUp()
        installer = os.path.join(self.repo, "install.sh")
        os.remove(installer)
        shutil.copyfile(INSTALL_SH, installer)
        os.chmod(installer, 0o755)
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "regular-file installer", cwd=self.repo)
        self.origin = os.path.join(self.root, "origin.git")
        git("clone", "-q", "--bare", self.repo, self.origin, cwd=self.root)
        git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.origin)
        git("remote", "add", "origin", self.origin, cwd=self.repo)
        git("fetch", "-q", "origin", cwd=self.repo)
        git("branch", "--set-upstream-to=origin/main", cwd=self.repo)
        git("remote", "set-head", "origin", "main", cwd=self.repo)
        self.dev = os.path.join(self.root, "dev")
        git("clone", "-q", self.origin, self.dev, cwd=self.root)
        git("config", "user.email", "t@example.com", cwd=self.dev)
        git("config", "user.name", "Test", cwd=self.dev)
        for repo in (self.repo, self.dev):
            self.assertFalse(os.path.islink(os.path.join(repo, "install.sh")), repo)
        self.origin_config = os.path.join(self.root, "origin-config.json")
        with open(self.origin_config, "w") as handle:
            json.dump({"update": {"origin": self.origin}}, handle)

    def push_change(self, files, message="change"):
        for name, content in files.items():
            path = os.path.join(self.dev, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(content)
            if name == "install.sh":
                os.chmod(path, 0o755)
        git("add", "-A", cwd=self.dev)
        git("commit", "-q", "-m", message, cwd=self.dev)
        git("push", "-q", "origin", "main", cwd=self.dev)
        return head(self.dev)

    def real_installer_lines(self):
        with open(INSTALL_SH) as handle:
            return handle.read().splitlines(True)

    def run_in(self, path, args):
        """Run `path`/install.sh with the fixture's sandbox env."""
        env = dict(os.environ)
        env["HOME"] = self.home
        env["USERPROFILE"] = self.home
        # The fixture's origin is a local path, not the pinned public URL, so the
        # config names it; nothing else in this config changes the install.
        env["CLAUDE_TOOLKIT_CONFIG"] = self.origin_config
        for key in [k for k in env if k.startswith("GIT_")]:
            del env[key]
        env.pop("ALLOW_WORKTREE_INSTALL", None)
        env = fx.shim_env(env, self.bin_dir, self.state)
        env["CLAUDE_TOOLKIT_MANAGED_DIR"] = self.managed
        return subprocess.run([os.path.join(path, "install.sh")] + list(args),
                              cwd=path, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)

    def run_args(self, args):
        return self.run_in(self.repo, args)

    def out(self, proc):
        return proc.stdout + proc.stderr


class TestUpdateModes(UpdateTestCase):

    def test_update_refuses_an_origin_the_config_does_not_name(self):
        self.assertEqual(0, self.install().returncode)
        self.push_change({"notes.txt": "x"})
        with open(self.origin_config, "w") as handle:
            json.dump({}, handle)
        before = (head(self.repo), json.dumps(record_of(self.home), sort_keys=True))
        proc = self.run_args(["--update"])
        self.assertEqual(1, proc.returncode, self.out(proc))
        self.assertIn("Not updated: origin is %s, not https://github.com/mouramarket/"
                      "claude-code-toolkit" % self.origin, proc.stderr)
        self.assertEqual(before, (head(self.repo),
                                  json.dumps(record_of(self.home), sort_keys=True)))

    def test_update_fast_forwards_and_installs(self):
        self.assertEqual(0, self.install().returncode)
        old = head(self.repo)
        new = self.push_change({"notes.txt": "x"})
        proc = self.run_args(["--update"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertEqual(new, head(self.repo))
        self.assertIn("Updated ", self.out(proc))
        record = record_of(self.home)
        self.assertEqual(new, record["commit"])
        self.assertEqual(old, record["rollback"]["commit"])

    def test_already_current_runs_no_install(self):
        self.assertEqual(0, self.install().returncode)
        before = fx.shim_log(self.state)
        proc = self.run_args(["--update"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertIn("Already up to date", self.out(proc))
        self.assertEqual(before, fx.shim_log(self.state))

    def test_already_current_without_record_runs_install_and_says_so(self):
        # rc 11: at origin, but no install record names this commit.
        current = head(self.repo)
        proc = self.run_args(["--update"])
        text = self.out(proc)
        self.assertEqual(0, proc.returncode, text)
        self.assertEqual(current, head(self.repo))
        self.assertIn("Already at %s; ran the install it never had." % current[:12], text)
        self.assertNotIn("Updated ", text)
        self.assertEqual(current, record_of(self.home)["commit"])

    def test_already_current_failed_install_gives_no_rollback_advice(self):
        self.push_change({"install.sh": self.failing_installer()}, "broken installer")
        git("pull", "-q", "--ff-only", cwd=self.repo)
        proc = self.run_args(["--update"])
        text = self.out(proc)
        self.assertEqual(3, proc.returncode, text)
        self.assertIn("so the checkout did not move, but the install failed (exit 3)", text)
        self.assertNotIn("--rollback", text)

    def test_child_runs_the_new_script(self):
        lines = self.real_installer_lines()
        lines.insert(1, "echo NEW-INSTALL-MARKER\n")
        self.push_change({"install.sh": "".join(lines)})
        proc = self.run_args(["--update"])
        self.assertIn("NEW-INSTALL-MARKER", self.out(proc))

    def failing_installer(self):
        # The failure sits on the first install step (the Config section),
        # never relative to the --update/--rollback block: anchoring it to
        # that block would move it with the block, and the recovery test
        # could no longer see a dispatch placed after the install steps.
        lines = self.real_installer_lines()
        config = lines.index('echo "Config"\n')
        lines.insert(config, "exit 3\n")
        return "".join(lines)

    def test_failed_install_then_rollback_then_return(self):
        self.assertEqual(0, self.install().returncode)
        old = head(self.repo)
        self.push_change({"install.sh": self.failing_installer()}, "broken installer")
        proc = self.run_args(["--update"])
        text = self.out(proc)
        self.assertEqual(3, proc.returncode, text)
        self.assertIn("./install.sh --rollback", text)
        self.assertIn("git -C %s checkout --detach %s" % (self.repo, old), text)

        proc = self.run_args(["--rollback"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertEqual(old, head(self.repo))
        self.assertEqual("", git("branch", "--show-current", cwd=self.repo).stdout.strip())
        self.assertIn("Pinned at ", self.out(proc))

        # Return: a fixing commit restores the real installer.
        fixed = self.push_change({"install.sh": "".join(self.real_installer_lines())}, "fix")
        proc = self.run_args(["--update"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertEqual(fixed, head(self.repo))
        self.assertEqual("main", git("branch", "--show-current", cwd=self.repo).stdout.strip())

    def test_worktree_refuses_update_and_rollback_but_not_version(self):
        wt = os.path.join(self.root, "wt")
        git("worktree", "add", "-q", "--detach", wt, cwd=self.repo)
        before = head(wt)
        for flag in ("--update", "--rollback"):
            proc = self.run_in(wt, [flag])
            self.assertEqual(1, proc.returncode, self.out(proc))
            self.assertIn("refusing to install from a linked git worktree", self.out(proc))
            self.assertEqual(before, head(wt))
        proc = self.run_in(wt, ["--version"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertIn("This is a linked worktree", proc.stdout)

    def test_declined_core_refuses_update_and_rollback_before_moving(self):
        self.assertEqual(0, self.install().returncode)
        self.push_change({"notes.txt": "x"})
        self.assertEqual(0, self.run_args(["--update"]).returncode)
        self.push_change({"more.txt": "y"})
        config = os.path.join(self.home, ".config", "claude-code-toolkit", "config.json")
        os.makedirs(os.path.dirname(config), exist_ok=True)
        with open(config, "w") as handle:
            json.dump({"danger_guard": {"installed": False}}, handle)
        before = (head(self.repo), json.dumps(record_of(self.home), sort_keys=True))
        for flag in ("--update", "--rollback"):
            proc = self.run_args([flag])
            self.assertEqual(1, proc.returncode, self.out(proc))
            self.assertIn("declines guards this toolkit's core always installs", proc.stderr)
            self.assertEqual(before, (head(self.repo),
                                      json.dumps(record_of(self.home), sort_keys=True)))
        proc = self.run_args(["--version"])
        self.assertEqual(0, proc.returncode, self.out(proc))

    def test_version_prints_state(self):
        proc = self.run_args(["--version"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertIn("Toolkit version: ", proc.stdout)


class RollbackFixture(UpdateTestCase):
    """`--rollback` puts the configuration back, not just the checkout.

    A manages no statusLine; B (pushed from dev) adds a baseline rule, the
    statusLine, a default `probe` profile and a `probe` link."""

    PROBE_RULE = "Bash(echo rollback-probe:*)"
    PROBE_PLUGIN = "toolkit-probe@claude-code-toolkit"
    NON_DEFAULT_PLUGIN = "toolkit-mac@claude-code-toolkit"
    PATTERN = "**/.claude/worktrees/*/CLAUDE.md"

    def setUp(self):
        super().setUp()
        # The inherited setUp seeds the expected statusLine, and A's install
        # never removes it, so A would "manage" it. Rebuild A without one.
        fx._write_json(os.path.join(self.repo, "claude", "settings.host.json"), {})
        git("add", "-A", cwd=self.repo)
        git("commit", "-q", "-m", "A without a statusLine", cwd=self.repo)
        git("push", "-q", "origin", "HEAD:main", cwd=self.repo)
        git("pull", "-q", "--ff-only", cwd=self.dev)
        self.a_head = head(self.repo)
        self.assertEqual(self.a_head, head(self.dev))
        proc = self.run_args([])
        self.assertEqual(0, proc.returncode, self.out(proc))
        settings = self.settings()
        settings.pop("statusLine", None)
        settings.setdefault("permissions", {}).setdefault("allow", []).append("Bash(user-own:*)")
        self.write_settings(settings)
        self.assertNotIn("statusLine", self.settings())
        self.probe_link = os.path.join(self.home, ".local", "bin", "claude-probe")

    def push_b(self, mac_default=False):
        """Commit B on dev and push it; returns B's commit."""
        base = os.path.join(self.dev, "claude")
        with open(os.path.join(base, "profiles.json")) as handle:
            registry = json.load(handle)
        registry["profiles"]["probe"] = {
            "plugin": "toolkit-probe", "dir": "plugin-probe", "default": True,
            "requires": ["core"], "agents": False, "summary": "Rollback probe."}
        if mac_default:
            registry["profiles"]["mac"]["default"] = True
        fx.add_plugin_layout(self.dev, profiles=registry)
        with open(os.path.join(base, "permissions.baseline.json")) as handle:
            baseline = json.load(handle)
        baseline.setdefault("permissions", {}).setdefault("allow", []).append(self.PROBE_RULE)
        fx._write_json(os.path.join(base, "permissions.baseline.json"), baseline)
        fx._write_json(os.path.join(base, "settings.host.json"), fx.real_host_settings())
        probe = os.path.join(self.dev, "tools", "probe", "claude-probe")
        os.makedirs(os.path.dirname(probe), exist_ok=True)
        with open(probe, "w") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(probe, 0o755)
        lines = self.real_installer_lines()
        anchor = [i for i, line in enumerate(lines)
                  if line.startswith('link "$REPO/tools/setup-audit/claude-setup-audit"')]
        self.assertEqual(1, len(anchor))
        lines.insert(anchor[0] + 1, 'link "$REPO/tools/probe/claude-probe"'
                     '            "$HOME/.local/bin/claude-probe"\n')
        with open(os.path.join(self.dev, "install.sh"), "w") as handle:
            handle.write("".join(lines))
        os.chmod(os.path.join(self.dev, "install.sh"), 0o755)
        git("add", "-A", cwd=self.dev)
        git("commit", "-q", "-m", "B", cwd=self.dev)
        git("push", "-q", "origin", "main", cwd=self.dev)
        return head(self.dev)

    def update_to_b(self, mac_default=False):
        b_head = self.push_b(mac_default)
        proc = self.run_args(["--update"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertEqual(b_head, head(self.repo))
        return b_head

    def installed_ids(self):
        with open(self.state) as handle:
            return [p["id"] for p in json.load(handle)["plugins"] if p.get("scope") == "user"]

    def owned(self):
        record = record_of(self.home)
        return {key: record.get(key) for key in
                ("permissions_added", "default_mode", "claude_md_excludes_added")}


class TestRollbackRestore(RollbackFixture):
    """R1-R7 on the A/B fixture above."""

    def test_r1_update_then_rollback_restores_the_configuration(self):
        self.update_to_b()
        settings = self.settings()
        self.assertIn(self.PROBE_RULE, settings["permissions"]["allow"])
        self.assertEqual(self.expected_status_line(), settings["statusLine"])
        self.assertIn(self.PROBE_PLUGIN, self.installed_ids())
        self.assertTrue(os.path.islink(self.probe_link))

        proc = self.run_args(["--rollback"])
        text = self.out(proc)
        self.assertEqual(0, proc.returncode, text)
        self.assertEqual(self.a_head, head(self.repo))
        settings = self.settings()
        self.assertNotIn(self.PROBE_RULE, settings["permissions"]["allow"])
        self.assertIn("Bash(user-own:*)", settings["permissions"]["allow"])
        self.assertNotIn("statusLine", settings)
        self.assertNotIn(self.PROBE_RULE,
                         json.dumps(record_of(self.home)["permissions_added"]))
        self.assertTrue(any(line.startswith("plugin uninstall " + self.PROBE_PLUGIN)
                            for line in fx.shim_log(self.state)), fx.shim_log(self.state))
        self.assertNotIn(self.PROBE_PLUGIN, self.installed_ids())
        self.assertFalse(os.path.lexists(self.probe_link))

    def test_r2_a_statusline_the_user_edited_survives_the_rollback(self):
        self.update_to_b()
        settings = self.settings()
        settings["statusLine"] = {"type": "command", "command": "/usr/local/bin/mine"}
        self.write_settings(settings)
        proc = self.run_args(["--rollback"])
        text = self.out(proc)
        self.assertEqual(0, proc.returncode, text)
        self.assertEqual({"type": "command", "command": "/usr/local/bin/mine"},
                         self.settings()["statusLine"])
        self.assertIn("statusLine changed since the update; left as you set it", text)

    def test_r2b_a_removed_exclude_pattern_is_named_and_the_install_re_adds_it(self):
        self.update_to_b()
        settings = self.settings()
        self.assertIn(self.PATTERN, settings["claudeMdExcludes"])
        settings["claudeMdExcludes"] = [p for p in settings["claudeMdExcludes"]
                                        if p != self.PATTERN]
        self.write_settings(settings)
        proc = self.run_args(["--rollback"])
        text = self.out(proc)
        self.assertEqual(0, proc.returncode, text)
        self.assertIn("claudeMdExcludes was removed since the update", text)
        # Documented behaviour: A's install adds the pattern back.
        self.assertIn(self.PATTERN, self.settings()["claudeMdExcludes"])

    def test_r3_the_toolkit_config_is_untouched(self):
        self.update_to_b()
        with open(self.origin_config) as handle:
            config = json.load(handle)
        config["added_after_update"] = {"note": "mine"}
        with open(self.origin_config, "w") as handle:
            json.dump(config, handle)
        before = read(self.origin_config)
        proc = self.run_args(["--rollback"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertEqual(before, read(self.origin_config))

    def test_r4_a_rule_the_user_added_after_the_update_survives(self):
        self.update_to_b()
        settings = self.settings()
        settings["permissions"]["allow"].append("Bash(added-later:*)")
        self.write_settings(settings)
        proc = self.run_args(["--rollback"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        allow = self.settings()["permissions"]["allow"]
        self.assertIn("Bash(added-later:*)", allow)
        self.assertIn("Bash(user-own:*)", allow)
        self.assertNotIn(self.PROBE_RULE, allow)

    def test_r5_a_second_rollback_changes_nothing(self):
        self.update_to_b()
        proc = self.run_args(["--rollback"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        settings, owned = read(self.settings_path), self.owned()
        proc = self.run_args(["--rollback"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertEqual(self.a_head, head(self.repo))
        self.assertEqual(settings, read(self.settings_path))
        self.assertEqual(owned, self.owned())

    def test_r6_update_after_rollback_returns_to_b(self):
        b_head = self.update_to_b()
        proc = self.run_args(["--rollback"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertEqual(self.a_head, head(self.repo))
        proc = self.run_args(["--update"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertEqual(b_head, head(self.repo))
        settings = self.settings()
        self.assertIn(self.PROBE_RULE, settings["permissions"]["allow"])
        self.assertEqual(self.expected_status_line(), settings["statusLine"])
        self.assertIn(self.PROBE_PLUGIN, self.installed_ids())

    def test_r7_a_profile_b_made_default_is_uninstalled_by_a_install(self):
        with open(os.path.join(self.repo, "claude", "profiles.json")) as handle:
            self.assertFalse(json.load(handle)["profiles"]["mac"]["default"])
        self.update_to_b(mac_default=True)
        self.assertIn(self.NON_DEFAULT_PLUGIN, self.installed_ids())
        proc = self.run_args(["--rollback"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        self.assertNotIn(self.NON_DEFAULT_PLUGIN, self.installed_ids())


class TestFlags(UpdateTestCase):

    def test_bad_combinations_exit_2_and_change_nothing(self):
        settings = read(self.settings_path)
        before = head(self.repo)
        for args in (["--version", "x"], ["--update", "--rollback"],
                     ["--rollback", "--yes"], ["--check", "--version"],
                     ["--setup", "--rollback"], ["--help", "--update"]):
            proc = self.run_args(args)
            self.assertEqual(2, proc.returncode, (args, self.out(proc)))
            self.assertIn("takes no other arguments", proc.stderr)
        self.assertEqual(before, head(self.repo))
        self.assertEqual(settings, read(self.settings_path))

    def test_help_mentions_all_three(self):
        proc = self.run_args(["--help"])
        self.assertEqual(0, proc.returncode, self.out(proc))
        for flag in ("--version", "--update", "--rollback"):
            self.assertIn(flag, proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
