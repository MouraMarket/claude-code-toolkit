#!/usr/bin/env python3
"""Tests for install.sh's "Plugin components -> toolkit-core" section.

Commands, agents, skills and the build-lens files ship in the `toolkit-core`
plugin, so the installer links none of them into `~/.claude/` and removes the
links it made before. These tests drive the REAL `install.sh` against a
sandbox HOME and a sandbox copy of the repo (the `test_install_prune.py`
sandbox), because a bug here deletes files.

What is pinned:

1. An apply creates no link under `~/.claude/{commands,agents,skills,build-lens}`.
2. An apply removes exactly the links that point into THIS checkout (realpath
   plus component containment, so a `<repo>/../x` target and a `<repo>-old/x`
   sibling are not ours), keeps the rest, and is idempotent.
3. `--check` fails on an owned leftover, an unresolved plugin layout, a bare-name
   clash, and an unreadable `settings.json` (an incomplete clash inventory).
"""

import json
import os
import subprocess
import sys
import unittest

import install_fixture as fx
from test_install_prune import InstallSandbox

OWNED_COMMAND = ".claude/commands/build.md"
OWNED_LENS = ".claude/build-lens/merge-review.md"
# A bare name the repo's own `skills/` really ships, so the clash is real.
SKILL = "autonomy-permissions"


class ComponentsSandbox(InstallSandbox):

    def run_install(self, *args, **kwargs):
        """Like the prune sandbox's, with a hook to change HOME AFTER the
        --check seeding (which writes settings.json itself)."""
        after_seed = kwargs.pop("after_seed", None)
        env = dict(os.environ)
        # install.sh honours CLAUDE_TOOLKIT_CONFIG, so an exported one would
        # select profiles this fixture does not carry.
        env.pop("CLAUDE_TOOLKIT_CONFIG", None)
        env["HOME"] = self.home
        env = fx.shim_env_for_home(env, self.home)
        if "--check" in args:
            fx.seed_home_installed(self.home, self.repo)
            fx.install_links(self.repo, self.home)
        if after_seed:
            after_seed()
        return subprocess.run(
            ["bash", os.path.join(self.repo, "install.sh")] + list(args),
            capture_output=True, text=True, env=env, cwd=self.repo,
            stdin=subprocess.DEVNULL, timeout=600)

    def claude_links(self):
        found = []
        for sub in ("commands", "agents", "skills", "build-lens"):
            directory = os.path.join(self.home, ".claude", sub)
            if not os.path.isdir(directory):
                continue
            found += [os.path.join(directory, name)
                      for name in sorted(os.listdir(directory))
                      if os.path.islink(os.path.join(directory, name))]
        return found


class TestTheInstallerLinksNothingUnderClaudeHome(ComponentsSandbox):

    def test_an_apply_creates_no_link_under_claude_home(self):
        proc = self.run_install()
        # The positive control: the run finished and reached the section.
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Plugin components", proc.stdout)
        self.assertEqual(self.claude_links(), [],
                         "the installer linked a component into ~/.claude: "
                         "commands, agents, skills and build-lens ship in "
                         "the plugin")


class TestRemovalOfOwnedLegacyLinks(ComponentsSandbox):

    def seed(self):
        self.owned = [
            self.link(OWNED_COMMAND, os.path.join(self.repo, "commands/build.md")),
            self.link(OWNED_LENS,
                      os.path.join(self.repo, "tools/build-lens/merge-review.md")),
        ]
        # Dangling targets throughout: ownership is decided by the target's
        # normalized path, never by whether it exists.
        self.kept = [
            self.link(".claude/commands/foreign.md",
                      os.path.join(self.tmp, "elsewhere", "x.md")),
            self.link(".claude/agents/traversal.md",
                      os.path.join(self.repo, "..", "foreign")),
            self.link(".claude/agents/sibling.md",
                      self.repo + "-old/x"),
        ]
        self.real_file = os.path.join(self.home, ".claude/commands/my-notes.md")
        with open(self.real_file, "w") as handle:
            handle.write("mine\n")

    def test_only_the_owned_links_are_removed(self):
        self.seed()
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for path in self.owned:
            self.assertFalse(os.path.lexists(path), "%s survived" % path)
            self.assertIn("removing ~/" + os.path.relpath(path, self.home),
                          proc.stdout)
        for path in self.kept:
            self.assertTrue(os.path.islink(path), "%s was removed" % path)
            self.assertRegex(
                proc.stdout,
                r"~/%s kept \(not this checkout's\)" % os.path.relpath(path, self.home))
        with open(self.real_file) as handle:
            self.assertEqual(handle.read(), "mine\n")
        self.assertNotIn("my-notes.md", proc.stdout)

    def test_the_build_lens_directory_the_removal_emptied_is_gone(self):
        self.seed()
        self.run_install()
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude/build-lens")))

    def test_a_second_apply_removes_nothing(self):
        self.seed()
        self.run_install()
        proc = self.run_install()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("now ships in the toolkit-core plugin", proc.stdout)
        for path in self.kept:
            self.assertTrue(os.path.islink(path))


class TestCheckFailures(ComponentsSandbox):

    def test_an_owned_leftover_fails_check_and_is_not_removed(self):
        link = self.link(OWNED_COMMAND, os.path.join(self.repo, "commands/build.md"))
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("owned legacy link still present: ~/.claude/commands/build.md",
                      proc.stdout)
        self.assertTrue(os.path.islink(link), "--check removed the link")

    def test_an_unresolved_plugin_layout_fails_check(self):
        os.remove(os.path.join(self.repo, "plugin", "skills"))
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("plugin/skills does not resolve", proc.stdout)

    def test_a_personal_skill_shadowing_a_plugin_skill_fails_check(self):
        self.assertTrue(os.path.isfile(
            os.path.join(self.repo, "skills", "core", SKILL, "SKILL.md")), "fixture is stale")
        os.makedirs(os.path.join(self.home, ".claude/skills", SKILL))
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("~/.claude/skills/%s shadows toolkit-core:%s" % (SKILL, SKILL),
                      proc.stdout)

    def test_an_unreadable_plugin_inventory_fails_check(self):
        """A `settings.json` the clash check cannot read is an incomplete
        inventory, never "no clashes". Invalid JSON never reaches this section
        (the hooks step before it refuses to touch the file and stops), so the
        reachable shape is valid JSON whose `enabledPlugins` is not a dict."""
        def corrupt():
            os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
            with open(os.path.join(self.home, ".claude", "settings.json"), "w") as handle:
                handle.write('{"enabledPlugins": []}')
        proc = self.run_install("--check", after_seed=corrupt)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("clash check is incomplete", proc.stdout)
        self.assertNotIn("no bare-name clashes", proc.stdout)

    def test_a_deleted_profile_registry_fails_check_and_blinds_the_clash_check(self):
        """No registry: the selection step stops --check, and the clash check on
        its own says it could not read the registry (never "no clashes")."""
        os.remove(os.path.join(self.repo, "claude", "profiles.json"))
        proc = self.run_install("--check")
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("profiles: cannot read", proc.stdout)
        self.assertNotIn("no bare-name clashes", proc.stdout)
        clash = subprocess.run(
            [sys.executable, os.path.join(self.repo, "lib", "plugin_paths.py"),
             "clashes", self.home, self.repo],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertIn("plugin inventory unreadable (cannot read", clash.stdout + clash.stderr)
        self.assertIn("profiles.json", clash.stdout + clash.stderr)

    def test_a_clash_check_that_did_not_run_fails_check(self):
        """A crashed `plugin_paths.py clashes` is a check that did not run,
        never "no clashes" (mutation I37). The sandbox copy exits 2 on that
        subcommand only, so the `legacy` call before it still runs."""
        path = os.path.join(self.repo, "lib", "plugin_paths.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        marker = 'if __name__ == "__main__":\n'
        self.assertEqual(source.count(marker), 1,
                         "plugin_paths.py's entry point changed; re-anchor this")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source.replace(
                marker,
                'if __name__ == "__main__" and sys.argv[1:2] == ["clashes"]:\n'
                '    sys.exit(2)\n' + marker))
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("plugin clash check did not run "
                      "(plugin_paths.py clashes exited 2)", proc.stdout)
        self.assertNotIn("no bare-name clashes", proc.stdout)

    def test_a_clean_machine_reports_no_clash(self):
        """The control for the five above: the section can pass."""
        proc = self.run_install("--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no bare-name clashes", proc.stdout)


class TestARetainedPersonalCommandFile(ComponentsSandbox):

    def test_an_apply_keeps_the_file_and_exits_1_naming_it(self):
        path = os.path.join(self.home, ".claude/commands", SKILL + ".md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("my own command\n")
        proc = self.run_install()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("~/.claude/commands/%s.md shadows toolkit-core:%s" % (SKILL, SKILL),
                      proc.stdout)
        with open(path) as handle:
            self.assertEqual(handle.read(), "my own command\n")


class TestLegacyPathsInTheUsersClaudeMd(ComponentsSandbox):
    WARNING = "still points at ~/.claude/build-lens or ~/.claude/commands"

    def apply_with_claude_md(self, text):
        workspace = os.path.join(self.tmp, "ws")
        os.makedirs(workspace, exist_ok=True)
        with open(os.path.join(workspace, "CLAUDE.md"), "w") as handle:
            handle.write(text)
        config = os.path.join(self.home, ".config", "claude-code-toolkit", "config.json")
        os.makedirs(os.path.dirname(config), exist_ok=True)
        with open(config, "w") as handle:
            json.dump({"workspace_root": workspace}, handle)
        return self.run_install()

    def test_a_claude_md_naming_the_retired_paths_is_warned(self):
        for text in ("Read ~/.claude/build-lens/merge-review.md first.\n",
                     "Run ~/.claude/commands/build.md.\n"):
            proc = self.apply_with_claude_md(text)
            self.assertIn(self.WARNING, proc.stdout, proc.stdout + proc.stderr)
            self.assertIn("tools/build-lens/<file> and skills/<group>/<name>/SKILL.md",
                          proc.stdout)

    def test_a_claude_md_without_them_is_not(self):
        proc = self.apply_with_claude_md("# Mine\nNothing legacy here.\n")
        self.assertNotIn(self.WARNING, proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("/CLAUDE.md", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
