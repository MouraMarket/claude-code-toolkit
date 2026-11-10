#!/usr/bin/env python3
"""Behavioural tests for `permissions.baseline_opt_out` in install.sh.

install.sh UNIONS `claude/permissions.baseline.json` into
`~/.claude/settings.json` on every run and never removes anything, so a person
who deliberately deleted a baseline `ask` rule got it back on the next install.
`permissions.baseline_opt_out` (a list of exact `ask` rule strings in the
toolkit config) lets them stop specific rules being re-added.

Pinned here:
  - absent key: unchanged behaviour, everything is unioned
  - listed `ask` rules are skipped and reported, on apply AND on --check
  - `deny` rules are never opt-out-able (they guard secrets) -- warn, apply
  - unknown entries and a non-list value are warned about, never silently used
  - union-only still holds: an opted-out rule already in settings stays there
  - --check writes nothing

These drive the REAL `install.sh` through `subprocess` against a minimal
fixture repo, with `$HOME` sandboxed. Nothing touches the real `~/.claude`.
"""

import json
import os
import subprocess
import tempfile
import unittest

import install_fixture as fx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
WORKTREE_DETECT_SH = os.path.join(REPO_ROOT, "lib", "worktree_detect.sh")
TOOLKIT_CONFIG_PY = os.path.join(REPO_ROOT, "lib", "toolkit_config.py")

BASELINE = {"permissions": {"deny": ["Read(~/.ssh/**)"],
                            "ask": ["Bash(git push *)", "Bash(gh pr merge *)"]}}
PUSH = "Bash(git push *)"
MERGE = "Bash(gh pr merge *)"
DENY = "Read(~/.ssh/**)"


def git(*args, cwd):
    """Fixture git call with the machine's own git config neutralised, so a
    global core.hooksPath can never fire inside a throwaway repo."""
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, env=env,
                          universal_newlines=True)


def make_fixture_repo(root, name="fixture"):
    """A minimal git repo with the REAL install.sh, worktree_detect.sh and
    toolkit_config.py symlinked in, an empty tools/, and a two-ask / one-deny
    baseline."""
    repo = os.path.join(root, name)
    os.makedirs(os.path.join(repo, "claude"))
    os.makedirs(os.path.join(repo, "tools"))
    os.makedirs(os.path.join(repo, "lib"))
    os.symlink(INSTALL_SH, os.path.join(repo, "install.sh"))
    os.symlink(WORKTREE_DETECT_SH, os.path.join(repo, "lib", "worktree_detect.sh"))
    os.symlink(TOOLKIT_CONFIG_PY, os.path.join(repo, "lib", "toolkit_config.py"))
    fx.add_plugin_layout(repo)
    # A link whose source is missing from the repo is a required failure.
    fx.add_link_targets(repo, root)
    with open(os.path.join(repo, "claude", "permissions.baseline.json"), "w") as handle:
        json.dump(BASELINE, handle)
    git("init", "-q", "-b", "main", repo, cwd=root)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def seed_home(home, config, settings=None):
    """ONE config file, read by both install.sh's bash first-run check and
    toolkit_config (the env override is popped in run_install)."""
    config_dir = os.path.join(home, ".config", "claude-code-toolkit")
    os.makedirs(config_dir)
    with open(os.path.join(config_dir, "config.json"), "w") as handle:
        json.dump(config, handle)
    if settings is not None:
        claude_dir = os.path.join(home, ".claude")
        os.makedirs(claude_dir)
        with open(os.path.join(claude_dir, "settings.json"), "w") as handle:
            json.dump(settings, handle)


def run_install(repo, home, args=()):
    env = dict(os.environ)
    env["HOME"] = home
    env.pop("CLAUDE_TOOLKIT_CONFIG", None)
    env.pop("ALLOW_WORKTREE_INSTALL", None)
    env = fx.shim_env_for_home(env, home)
    if "--check" in args:
        fx.seed_home_installed(home, repo)
        fx.install_links(repo, home)
    # stdin=DEVNULL: capture_output does not redirect stdin, so an interactive
    # branch would hang on the runner's terminal.
    return subprocess.run([os.path.join(repo, "install.sh")] + list(args),
                          cwd=repo, env=env, capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=120,
                          universal_newlines=True)


def permissions_section(stdout):
    """Stdout from the Permissions header to the next blank line."""
    start = stdout.index("Permissions -> ~/.claude/settings.json")
    end = stdout.find("\n\n", start)
    return stdout[start:] if end == -1 else stdout[start:end]


class OptOutTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.repo = make_fixture_repo(self.root)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")

    def run_with(self, config, settings=None, args=()):
        seed_home(self.home, config, settings)
        result = run_install(self.repo, self.home, args)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return permissions_section(result.stdout)

    def applied(self):
        with open(self.settings_path) as handle:
            return json.load(handle)["permissions"]

    def test_absent_key_unions_everything(self):
        section = self.run_with({})
        perms = self.applied()
        self.assertEqual({PUSH, MERGE}, set(perms["ask"]))
        self.assertEqual([DENY], perms["deny"])
        self.assertNotIn("opted out", section)

    def test_listed_ask_rule_is_skipped_and_reported(self):
        section = self.run_with({"permissions": {"baseline_opt_out": [PUSH]}})
        perms = self.applied()
        self.assertIn(MERGE, perms["ask"])
        self.assertNotIn(PUSH, perms["ask"])
        self.assertIn("opted out: ask " + PUSH, section)

    def test_check_reports_opt_out_and_writes_nothing(self):
        section = self.run_with({"permissions": {"baseline_opt_out": [PUSH]}},
                                args=("--check",))
        self.assertIn("opted out: ask " + PUSH, section)
        self.assertIn("ask += " + MERGE, section)
        self.assertNotIn("ask += " + PUSH, section)
        self.assertFalse(os.path.exists(self.settings_path))

    def test_deny_rule_cannot_be_opted_out(self):
        section = self.run_with({"permissions": {"baseline_opt_out": [DENY]}})
        perms = self.applied()
        self.assertEqual([DENY], perms["deny"])
        self.assertEqual({PUSH, MERGE}, set(perms["ask"]))
        self.assertIn("cannot remove deny rule " + DENY, section)
        self.assertIn("applied anyway", section)

    def test_unknown_entry_warns_and_everything_lands(self):
        section = self.run_with({"permissions": {"baseline_opt_out": ["Bash(nope *)"]}})
        perms = self.applied()
        self.assertEqual({PUSH, MERGE}, set(perms["ask"]))
        self.assertEqual([DENY], perms["deny"])
        self.assertIn("matches no baseline rule", section)

    def test_duplicate_ask_entry_warns_as_duplicate(self):
        section = self.run_with({"permissions": {"baseline_opt_out": [PUSH, PUSH]}})
        perms = self.applied()
        self.assertEqual(1, section.count("opted out: ask " + PUSH))
        self.assertIn("twice", section)
        self.assertNotIn("matches no baseline rule", section)
        self.assertNotIn(PUSH, perms["ask"])
        self.assertIn(MERGE, perms["ask"])

    def test_duplicate_unknown_entry_warns_once_as_unmatched_then_duplicate(self):
        section = self.run_with(
            {"permissions": {"baseline_opt_out": ["Bash(nope *)", "Bash(nope *)"]}})
        self.assertEqual(1, section.count("matches no baseline rule"))
        self.assertIn("twice", section)

    def test_non_list_value_is_ignored_with_warning(self):
        section = self.run_with({"permissions": {"baseline_opt_out": PUSH}})
        perms = self.applied()
        self.assertEqual({PUSH, MERGE}, set(perms["ask"]))
        self.assertEqual([DENY], perms["deny"])
        self.assertIn("must be a list", section)

    def test_union_only_keeps_an_opted_out_rule_already_in_settings(self):
        section = self.run_with({"permissions": {"baseline_opt_out": [PUSH]}},
                                settings={"permissions": {"ask": [PUSH]}})
        perms = self.applied()
        # Without this the test passes on an installer that ignores opt-out.
        self.assertIn("opted out: ask " + PUSH, section)
        self.assertIn(PUSH, perms["ask"])
        self.assertIn(MERGE, perms["ask"])

    def test_check_is_idempotent_when_everything_is_covered(self):
        section = self.run_with(
            {"permissions": {"baseline_opt_out": [PUSH, MERGE]}},
            settings={"permissions": {"deny": [DENY]}}, args=("--check",))
        self.assertIn("permissions already cover the baseline set", section)
        self.assertNotIn("+=", section)


if __name__ == "__main__":
    unittest.main()
