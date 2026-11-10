#!/usr/bin/env python3
"""Tests for lib/uninstall.py: `gather` reads state, `plan` turns it into Actions.

`plan` is pure, so most tests hand it a state dict built by `make_state` and
look at the (actions, kept, blockers) it returns. The `gather` tests build a
real repo and HOME in a temp directory and stub `run_cli` and `run_git`.
"""

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB)
import uninstall  # noqa: E402
from uninstall import Action  # noqa: E402

REPO = "/r/repo"
HOME = "/h"
PUSH = "Bash(git push:*)"
MERGE = "Bash(gh pr merge:*)"
HOOKS_DIR = HOME + "/.config/claude-code-toolkit/git-hooks"
TOOLKIT_DEPS = {
    "toolkit-core": [],
    "toolkit-build": ["toolkit-core"],
    "toolkit-continuity": ["toolkit-core"],
    "toolkit-mac": ["toolkit-core"],
    "toolkit-maintainer": ["toolkit-core", "toolkit-build"],
}
MARKET_HERE = {"name": "claude-code-toolkit", "source": "directory", "path": REPO}


def make_state(**overrides):
    state = {
        "errors": [],
        "settings": {},
        "record": None,
        "record_unreadable": False,
        "plugins": [],
        "marketplaces": [],
        "plugin_dependencies": {},
        "retained_plugin_dependencies": {},
        "local_bin": [],
        "home_bin": [],
        "legacy_links": [],
        "managed_links": [],
        "claude_md_path": HOME + "/.claude/CLAUDE.md",
        "claude_md": None,
        "claude_md_is_symlink": False,
        "claude_md_imports": [],
        "policy_real": REPO + "/policy/CLAUDE.md",
        "git_global": [],
        "hooks_path": None,
        "hooks_path_origin": None,
        "global_config_origin": "file:" + HOME + "/.gitconfig",
        "hooks_dir": HOOKS_DIR,
        "hooks_dir_entries": [],
        "hooks_dir_exists": True,
        "wanted_status_line": None,
        "legacy_commands": set(),
        "auto_memory_target": None,
        "baseline_rules": {},
        "autonomous_default_mode": None,
    }
    state.update(overrides)
    return state


def run_plan(**overrides):
    return uninstall.plan(make_state(**overrides), REPO, HOME)


def record(allow=(), ask=(), deny=(), mode=None, exclude=None):
    data = {"schema": 1, "permissions_added": {
        "allow": list(allow), "ask": list(ask), "deny": list(deny)}}
    if mode is not None:
        data["default_mode"] = mode
    if exclude is not None:
        data["claude_md_excludes_added"] = exclude
    return data


def by(actions, surface, kind=None):
    return [a for a in actions if a.surface == surface and (kind is None or a.kind == kind)]


class PermissionTests(unittest.TestCase):
    def test_1_recorded_rule_removed_unrecorded_kept(self):
        actions, _, blockers = run_plan(
            record=record(ask=[PUSH]),
            settings={"permissions": {"ask": [PUSH, MERGE]}})
        self.assertEqual(by(actions, "settings", "remove-rule"),
                         [Action("settings", "remove-rule", "ask", {"rule": PUSH})])
        self.assertEqual(blockers, [])

    def test_2_no_record_no_action_and_a_kept(self):
        actions, kept, _ = run_plan(settings={"permissions": {"ask": [PUSH]}})
        self.assertEqual(by(actions, "settings"), [])
        self.assertTrue(any("ownership unknown" in k.reason for k in kept))

    def test_3_restore_mode_only_when_current_equals_set(self):
        rec = record(mode={"set": "auto", "previous": "default"})
        actions, _, _ = run_plan(record=rec, settings={"permissions": {"defaultMode": "auto"}})
        self.assertEqual(by(actions, "settings", "restore-mode"),
                         [Action("settings", "restore-mode", "defaultMode", {"previous": "default"})])
        actions, _, _ = run_plan(record=rec, settings={"permissions": {"defaultMode": "plan"}})
        self.assertEqual(by(actions, "settings", "restore-mode"), [])

    def test_unreadable_record_is_kept_not_silent(self):
        _, kept, _ = run_plan(record_unreadable=True,
                              settings={"permissions": {"ask": [PUSH]}})
        self.assertTrue(any(k.surface == "permissions"
                            and k.reason == "install record unreadable: ownership unknown"
                            for k in kept))


class PluginTests(unittest.TestCase):
    def ids(self, *names, **scopes):
        return [{"id": n + "@claude-code-toolkit", "scope": scopes.get(n.replace("-", "_"), "user")}
                for n in names]

    def test_4_dependents_first_marketplace_last(self):
        plugins = self.ids("toolkit-core", "toolkit-build", "toolkit-continuity", "toolkit-mac", "toolkit-maintainer")
        actions, _, blockers = run_plan(plugins=plugins, marketplaces=[MARKET_HERE],
                                        plugin_dependencies=TOOLKIT_DEPS)
        self.assertEqual(blockers, [])
        order = [a.target for a in actions if a.surface == "plugin"]
        self.assertLess(order.index("toolkit-maintainer@claude-code-toolkit"), order.index("toolkit-build@claude-code-toolkit"))
        self.assertEqual(order[-1], "toolkit-core@claude-code-toolkit")
        self.assertEqual(len(order), 5)
        kinds = [(a.surface, a.kind) for a in actions]
        self.assertEqual(kinds[-1], ("marketplace", "remove"))
        self.assertEqual(actions[-1].target, "claude-code-toolkit")
        self.assertEqual(actions[0].data, {})

    def test_5_other_scope_blocks_everything(self):
        plugins = [{"id": "toolkit-core@claude-code-toolkit", "scope": "user"},
                   {"id": "toolkit-core@claude-code-toolkit", "scope": "project"}]
        actions, _, blockers = run_plan(plugins=plugins, marketplaces=[MARKET_HERE])
        self.assertEqual(by(actions, "plugin") + by(actions, "marketplace"), [])
        self.assertTrue(any("project" in b.detail for b in blockers))

    def test_6_retained_dependent_blocks(self):
        plugins = self.ids("toolkit-core", "toolkit-build")
        actions, _, blockers = run_plan(
            plugins=plugins, marketplaces=[MARKET_HERE],
            plugin_dependencies=TOOLKIT_DEPS,
            retained_plugin_dependencies={"foo@other": ["toolkit-build"]})
        self.assertEqual(by(actions, "plugin") + by(actions, "marketplace"), [])
        self.assertTrue(any("foo@other" in b.detail for b in blockers))

    def test_6b_retained_dependent_on_something_not_removed_is_fine(self):
        actions, _, blockers = run_plan(
            plugins=self.ids("toolkit-core"), marketplaces=[MARKET_HERE],
            plugin_dependencies=TOOLKIT_DEPS,
            retained_plugin_dependencies={"foo@other": ["bar"]})
        self.assertEqual(blockers, [])
        self.assertEqual(len(by(actions, "plugin")), 1)

    def test_orphaned_toolkit_plugin_blocks_when_the_marketplace_is_gone(self):
        plugins = [{"id": "toolkit-core@claude-code-toolkit", "scope": "user"}]
        actions, _, blockers = run_plan(plugins=plugins, marketplaces=[])
        self.assertEqual(by(actions, "plugin") + by(actions, "marketplace"), [])
        self.assertTrue(any("marketplace is gone" in b.detail and "toolkit-core@claude-code-toolkit" in b.detail
                            for b in blockers), blockers)

    def test_plugins_of_a_marketplace_elsewhere_are_not_orphans(self):
        # Another checkout's marketplace owns them; this checkout's uninstall lists, never blocks.
        elsewhere = {"name": "claude-code-toolkit", "source": "directory", "path": "/other/checkout"}
        plugins = [{"id": "toolkit-build@claude-code-toolkit", "scope": "user"}]
        _, kept, blockers = run_plan(plugins=plugins, marketplaces=[elsewhere])
        self.assertFalse(any("marketplace is gone" in b.detail for b in blockers), blockers)
        self.assertTrue(any(k.surface == "marketplace" for k in kept), kept)

    def test_7_marketplace_elsewhere_is_kept(self):
        elsewhere = {"name": "claude-code-toolkit", "source": "directory", "path": "/other/place"}
        actions, kept, _ = run_plan(plugins=self.ids("toolkit-core"), marketplaces=[elsewhere])
        self.assertEqual(by(actions, "plugin") + by(actions, "marketplace"), [])
        self.assertTrue(any(k.surface == "marketplace" for k in kept))

    def test_7b_non_directory_marketplace_is_kept(self):
        hosted = {"name": "claude-code-toolkit", "source": "github", "path": REPO}
        actions, kept, _ = run_plan(plugins=self.ids("toolkit-core"), marketplaces=[hosted])
        self.assertEqual(by(actions, "plugin") + by(actions, "marketplace"), [])
        self.assertTrue(kept)

    def test_20_orphaned_enabled_plugins_and_known_marketplace(self):
        settings = {"enabledPlugins": {"toolkit-continuity@claude-code-toolkit": True, "x@other": True},
                    "extraKnownMarketplaces": {"claude-code-toolkit": {"source": {"path": REPO}}}}
        actions, _, _ = run_plan(settings=settings)
        self.assertEqual(by(actions, "settings", "remove-enabled"),
                         [Action("settings", "remove-enabled", "toolkit-continuity@claude-code-toolkit", {})])
        self.assertEqual(by(actions, "settings", "remove-known-marketplace"),
                         [Action("settings", "remove-known-marketplace", "claude-code-toolkit", {})])

    def test_20b_known_marketplace_elsewhere_is_left(self):
        settings = {"extraKnownMarketplaces": {"claude-code-toolkit": {"source": {"path": "/other"}}}}
        actions, _, _ = run_plan(settings=settings)
        self.assertEqual(by(actions, "settings", "remove-known-marketplace"), [])

    def test_20c_listed_marketplace_means_no_orphan_cleanup(self):
        settings = {"enabledPlugins": {"toolkit-continuity@claude-code-toolkit": True}}
        actions, _, _ = run_plan(settings=settings, marketplaces=[MARKET_HERE])
        self.assertEqual(by(actions, "settings", "remove-enabled"), [])


class SettingsKeyTests(unittest.TestCase):
    STATUS = {"type": "command", "command": HOME + "/.local/bin/claude-usage-line"}
    LINKS = [(REPO + "/tools/u/claude-usage-line", HOME + "/.local/bin/claude-usage-line"),
             (REPO + "/tools/g/claude-danger-guard", HOME + "/.local/bin/claude-danger-guard")]

    def test_8_status_line_equal_removed(self):
        actions, _, blockers = run_plan(
            settings={"statusLine": dict(self.STATUS)}, wanted_status_line=dict(self.STATUS),
            managed_links=self.LINKS)
        self.assertEqual(by(actions, "settings", "remove-key"),
                         [Action("settings", "remove-key", "statusLine", {"expected": self.STATUS})])
        self.assertEqual(blockers, [])

    def test_8_status_line_edited_blocks(self):
        edited = dict(self.STATUS, padding=1)
        actions, kept, blockers = run_plan(
            settings={"statusLine": edited}, wanted_status_line=dict(self.STATUS),
            managed_links=self.LINKS)
        self.assertEqual(by(actions, "settings", "remove-key"), [])
        self.assertTrue(any(k.surface == "statusLine" for k in kept))
        self.assertTrue(any("claude-usage-line" in b.detail or "statusLine" in b.detail
                            for b in blockers))

    def test_auto_memory_directory(self):
        target = "~/.claude/projects/-w/memory"
        actions, _, _ = run_plan(settings={"autoMemoryDirectory": target},
                                 auto_memory_target=target)
        self.assertEqual(by(actions, "settings", "remove-key"),
                         [Action("settings", "remove-key", "autoMemoryDirectory", {"expected": target})])
        actions, kept, _ = run_plan(settings={"autoMemoryDirectory": "~/mine"},
                                    auto_memory_target=target)
        self.assertEqual(by(actions, "settings", "remove-key"), [])
        self.assertTrue(any(k.surface == "autoMemoryDirectory" for k in kept))

    def test_5_exclude_needs_the_record(self):
        pattern = "**/.claude/worktrees/*/CLAUDE.md"
        rec = record(exclude={"pattern": pattern, "created_key": True})
        actions, _, _ = run_plan(record=rec, settings={"claudeMdExcludes": [pattern, "x"]})
        self.assertEqual(by(actions, "settings", "remove-exclude"),
                         [Action("settings", "remove-exclude", "claudeMdExcludes",
                                 {"pattern": pattern, "created_key": True})])
        actions, kept, _ = run_plan(settings={"claudeMdExcludes": [pattern]})
        self.assertEqual(by(actions, "settings", "remove-exclude"), [])
        self.assertTrue(any(k.surface == "claudeMdExcludes" for k in kept))

    def test_6_legacy_hooks(self):
        command = HOME + "/.local/bin/claude-base-pin"
        settings = {"hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": command},
            {"type": "command", "command": "/h/mine.sh"}]}]}}
        actions, _, blockers = run_plan(settings=settings, legacy_commands={command},
                                        managed_links=self.LINKS)
        self.assertEqual(by(actions, "settings", "remove-hook"),
                         [Action("settings", "remove-hook", "PreToolUse", {"command": command})])
        self.assertEqual(blockers, [])

    def test_9_independence_scan_on_hooks(self):
        def hooks(command):
            return {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": command}]}]}}
        for command, expect_block in (("~/bin/mine.sh", False),
                                      ("~/bin/my-claude-usage-line-v2", False),
                                      (HOME + "/.local/bin/claude-danger-guard", True)):
            _, _, blockers = run_plan(settings=hooks(command), managed_links=self.LINKS)
            self.assertEqual(bool(blockers), expect_block, command)

    def test_scan_ignores_permissions_and_plugin_keys(self):
        settings = {"permissions": {"allow": ["Bash(" + REPO + "/x:*)"]},
                    "enabledPlugins": {"a@" + REPO: True},
                    "extraKnownMarketplaces": {"m": {"path": REPO}}}
        _, _, blockers = run_plan(settings=settings, marketplaces=[MARKET_HERE])
        self.assertEqual(blockers, [])

    def test_scan_blocks_on_non_ascii_checkout_path(self):
        repo = "/r/O\u02bbahu/repo"
        state = make_state(settings={"env": {"X": repo + "/tools"}})
        _, _, blockers = uninstall.plan(state, repo, HOME)
        self.assertTrue(any(b.surface == "settings" for b in blockers))

    def test_scan_blocks_on_checkout_path_in_other_key(self):
        _, _, blockers = run_plan(settings={"env": {"X": REPO + "/tools"}})
        self.assertTrue(blockers)


class LinkTests(unittest.TestCase):
    ROW = (REPO + "/tools/n/claude-notes", HOME + "/.local/bin/claude-notes")

    def test_11_local_bin(self):
        entries = [
            (self.ROW[1], True, self.ROW[0]),
            (HOME + "/.local/bin/my-notes", True, REPO + "/README.md"),
            (HOME + "/.local/bin/foo", True, "/usr/bin/true"),
        ]
        actions, _, blockers = run_plan(local_bin=entries, managed_links=[self.ROW])
        self.assertEqual(by(actions, "link"),
                         [Action("link", "unlink", self.ROW[1], {"expected_target": self.ROW[0]})])
        self.assertEqual(len(blockers), 1)
        self.assertIn("my-notes", blockers[0].detail)

    def test_11_without_the_foreign_link(self):
        entries = [(HOME + "/.local/bin/foo", True, "/usr/bin/true")]
        actions, kept, blockers = run_plan(local_bin=entries, managed_links=[self.ROW])
        self.assertEqual((by(actions, "link"), blockers), ([], []))

    def test_19_relative_link_resolving_inside_blocks(self):
        entries = [(HOME + "/.local/bin/my-notes", True, "../../../r/repo/README.md")]
        actions, _, blockers = run_plan(local_bin=entries)
        self.assertEqual(by(actions, "link"), [])
        self.assertEqual(len(blockers), 1)

    def test_12_home_bin_basename_rule(self):
        own = (HOME + "/bin/claude-x", True, REPO + "/tools/x/claude-x")
        actions, _, blockers = run_plan(home_bin=[own])
        self.assertEqual(by(actions, "link"),
                         [Action("link", "unlink", own[0], {"expected_target": own[2]})])
        self.assertEqual(blockers, [])
        other = (HOME + "/bin/claude-x", True, REPO + "/tools/x/different")
        actions, _, blockers = run_plan(home_bin=[other])
        self.assertEqual(by(actions, "link"), [])
        self.assertEqual(len(blockers), 1)

    def test_12_home_bin_sibling_prefix_is_not_ours(self):
        sibling = (HOME + "/bin/claude-x", True, REPO + "-old/tools/x/claude-x")
        actions, _, blockers = run_plan(home_bin=[sibling])
        self.assertEqual((by(actions, "link"), blockers), ([], []))

    def test_9_legacy_claude_links(self):
        owned = (HOME + "/.claude/commands/a.md", REPO + "/commands/a.md", True)
        foreign = (HOME + "/.claude/commands/b.md", "/elsewhere/b.md", False)
        actions, _, blockers = run_plan(legacy_links=[owned, foreign])
        self.assertEqual(by(actions, "link"),
                         [Action("link", "unlink", owned[0], {"expected_target": owned[1]})])
        self.assertEqual(blockers, [])


class PolicyTests(unittest.TestCase):
    def test_import_line_removed_and_not_a_scan_hit(self):
        text = "# mine\n@" + REPO + "/policy/CLAUDE.md\n"
        actions, _, blockers = run_plan(claude_md=text,
                                        claude_md_imports=[(2, REPO + "/policy/CLAUDE.md", True)])
        self.assertEqual(by(actions, "policy"),
                         [Action("policy", "remove-import", HOME + "/.claude/CLAUDE.md", {})])
        self.assertEqual(blockers, [])

    def test_mixed_import_line_is_a_blocker_and_no_removal(self):
        text = "@" + REPO + "/policy/CLAUDE.md Always ask before touching production.\n"
        actions, _, blockers = run_plan(claude_md=text,
                                        claude_md_imports=[(1, REPO + "/policy/CLAUDE.md", False)])
        self.assertEqual(by(actions, "policy"), [])
        self.assertEqual([b.surface for b in blockers], ["claude-md"])

    def test_mixed_line_still_scanned_when_a_pure_import_is_removed(self):
        # Only the import-only line is dropped from the scan; the mixed one stays a hit.
        policy = REPO + "/policy/CLAUDE.md"
        text = "@" + policy + "\n@" + policy + " Always ask before touching production.\n"
        actions, _, blockers = run_plan(claude_md=text,
                                        claude_md_imports=[(1, policy, True), (2, policy, False)])
        self.assertEqual(by(actions, "policy"),
                         [Action("policy", "remove-import", HOME + "/.claude/CLAUDE.md", {})])
        self.assertEqual([b.surface for b in blockers], ["claude-md"])

    def test_13_inline_reference_blocks(self):
        text = "Use @" + REPO + "/policy/CLAUDE.md for rules.\n"
        _, _, blockers = run_plan(claude_md=text)
        self.assertEqual(len(blockers), 1)

    def test_symlinked_claude_md_blocks_the_removal(self):
        text = "@" + REPO + "/policy/CLAUDE.md\n"
        actions, _, blockers = run_plan(claude_md=text, claude_md_is_symlink=True,
                                        claude_md_imports=[(1, REPO + "/policy/CLAUDE.md", True)])
        self.assertEqual(by(actions, "policy"), [])
        self.assertTrue(blockers)


class GitTests(unittest.TestCase):
    PRE = (HOOKS_DIR + "/pre-commit", True, REPO + "/tools/secret-scan/git-pre-commit")
    FWD = (HOOKS_DIR + "/post-merge", True, REPO + "/tools/secret-scan/git-hook-forward")
    LINE = ("file:" + HOME + "/.gitconfig", "core.hookspath=" + HOOKS_DIR)

    def git(self, **overrides):
        base = dict(hooks_path=HOOKS_DIR, hooks_path_origin=self.LINE[0],
                    git_global=[self.LINE], hooks_dir_entries=[self.PRE, self.FWD])
        base.update(overrides)
        return run_plan(**base)

    def test_10_toolkit_only_hooks_dir(self):
        actions, _, blockers = self.git()
        self.assertEqual(blockers, [])
        git = by(actions, "git")
        self.assertEqual([(a.kind, a.target) for a in git], [
            ("unlink-hook", self.PRE[0]), ("unlink-hook", self.FWD[0]),
            ("unset-hookspath", "core.hooksPath"), ("rmdir", HOOKS_DIR)])
        self.assertEqual(git[0].data, {"expected_target": self.PRE[2]})

    def test_10_tilde_spelling_of_hooks_path(self):
        actions, _, blockers = self.git(hooks_path="~/.config/claude-code-toolkit/git-hooks",
                                        git_global=[(self.LINE[0], "core.hookspath=~/.config/claude-code-toolkit/git-hooks")])
        self.assertEqual(blockers, [])
        self.assertEqual(by(actions, "git")[-2].kind, "unset-hookspath")

    def test_10_real_file_blocks_all_git_actions(self):
        real = (HOOKS_DIR + "/post-merge", False, None)
        actions, _, blockers = self.git(hooks_dir_entries=[self.PRE, real])
        self.assertEqual(by(actions, "git"), [])
        self.assertTrue(any("post-merge" in b.detail for b in blockers))

    def test_10_hooks_path_into_checkout_elsewhere_blocks(self):
        path = REPO + "/tools/secret-scan"
        actions, _, blockers = run_plan(hooks_path=path, hooks_path_origin=self.LINE[0],
                                        git_global=[(self.LINE[0], "core.hookspath=" + path)])
        self.assertEqual(by(actions, "git"), [])
        self.assertTrue(blockers)

    def test_16_setup_git_predicate(self):
        pre = (HOOKS_DIR + "/pre-commit", True, "/other/x/git-pre-commit")
        fwd = (HOOKS_DIR + "/post-merge", True, "/other/x/git-hook-forward")
        actions, _, blockers = self.git(hooks_dir_entries=[pre, fwd])
        self.assertEqual(blockers, [])
        self.assertEqual(len(by(actions, "git", "unlink-hook")), 2)

    def test_16_inside_but_not_a_toolkit_hook_blocks(self):
        backup = (HOOKS_DIR + "/post-merge", True, REPO + "/tools/backup/claude-backup")
        actions, _, blockers = self.git(hooks_dir_entries=[self.PRE, backup])
        self.assertEqual(by(actions, "git"), [])
        self.assertTrue(blockers)

    def test_16_pre_commit_must_point_at_git_pre_commit(self):
        odd = (HOOKS_DIR + "/pre-commit", True, REPO + "/tools/secret-scan/git-hook-forward-x")
        actions, _, blockers = self.git(hooks_dir_entries=[odd])
        self.assertEqual(by(actions, "git"), [])
        self.assertTrue(blockers)

    def test_18_included_file_origin_blocks(self):
        origin = "file:" + HOME + "/.gitconfig-extra"
        actions, _, blockers = self.git(hooks_path_origin=origin,
                                        git_global=[(origin, "core.hookspath=" + HOOKS_DIR)])
        self.assertEqual(by(actions, "git"), [])
        self.assertTrue(any(origin in b.detail or origin in b.remediation for b in blockers))

    def test_other_global_config_pointing_at_checkout_blocks(self):
        lines = [self.LINE, ("file:" + HOME + "/.gitconfig", "alias.x=!" + REPO + "/tools/x")]
        _, _, blockers = self.git(git_global=lines)
        self.assertEqual(len(blockers), 1)

    def test_missing_hooks_dir_skips_rmdir(self):
        actions, _, _ = self.git(hooks_dir_entries=[], hooks_dir_exists=False)
        self.assertEqual([a.kind for a in by(actions, "git")], ["unset-hookspath"])


class ErrorTests(unittest.TestCase):
    def test_14_errors_become_blockers(self):
        _, _, blockers = run_plan(errors=[("legacy-links", "Permission denied")])
        self.assertEqual(len(blockers), 1)
        self.assertEqual(blockers[0].surface, "legacy-links")
        self.assertIn("Permission denied", blockers[0].detail)

    def test_plan_does_not_mutate_its_state(self):
        state = make_state(record=record(ask=[PUSH]),
                           settings={"permissions": {"ask": [PUSH]}, "env": {"A": "b"}})
        before = copy.deepcopy(state)
        uninstall.plan(state, REPO, HOME)
        self.assertEqual(state, before)


class GatherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uninstall-plan-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.home)
        os.makedirs(os.path.join(self.repo, "policy"))

    def cli(self, plugins="[]", marketplaces="[]"):
        def run_cli(argv):
            return True, marketplaces if argv[:2] == ["plugin", "marketplace"] else plugins
        return run_cli

    @staticmethod
    def git_ok(argv):
        return 0, "", ""

    def gather(self, run_cli=None, run_git=None, repo=None, rows=()):
        return uninstall.gather(repo or self.repo, self.home,
                                os.path.join(self.home, ".claude", "settings.json"),
                                run_cli or self.cli(), run_git or self.git_ok, list(rows))

    def put(self, relpath, text):
        path = os.path.join(self.home, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_15_cli_missing_is_an_error_and_a_blocker(self):
        state = self.gather(run_cli=lambda argv: (False, "claude CLI not found"))
        self.assertTrue(any("claude CLI not found" in msg for _, msg in state["errors"]))
        _, _, blockers = uninstall.plan(state, self.repo, self.home)
        self.assertTrue(blockers)

    def test_21_absent_global_gitconfig_is_not_an_error(self):
        def run_git(argv):
            return 128, "", "fatal: unable to read config file '/h/.gitconfig': No such file or directory\n"
        state = self.gather(run_git=run_git)
        self.assertEqual(state["git_global"], [])
        self.assertEqual([e for e in state["errors"] if e[0] == "git"], [])
        self.assertIsNone(state["hooks_path"])
        self.assertEqual(state["global_config_origin"],
                         "file:" + os.path.join(self.home, ".gitconfig"))

    def test_other_git_failure_is_an_error(self):
        state = self.gather(run_git=lambda argv: (1, "", "boom"))
        self.assertTrue([e for e in state["errors"] if e[0] == "git"])

    def test_j_includes_flag_is_passed(self):
        top = "file:/h/.gitconfig"
        inc = "file:/h/.gitconfig-extra"

        def run_git(argv):
            if "--includes" in argv:
                return 0, top + "\tuser.name=a\n" + inc + "\tcore.hookspath=/x/hooks\n", ""
            return 0, top + "\tuser.name=a\n", ""
        state = self.gather(run_git=run_git)
        self.assertEqual(state["hooks_path"], "/x/hooks")
        self.assertEqual(state["hooks_path_origin"], inc)
        self.assertEqual(state["global_config_origin"], top)
        self.assertIn((top, "user.name=a"), state["git_global"])

    def test_last_hookspath_wins(self):
        def run_git(argv):
            return 0, ("file:/h/.gitconfig\tcore.hookspath=/a\n"
                       "file:/h/.gitconfig\tcore.hookspath=/b\n"), ""
        self.assertEqual(self.gather(run_git=run_git)["hooks_path"], "/b")

    def test_settings_states(self):
        self.assertEqual(self.gather()["settings"], {})
        self.put(".claude/settings.json", "{not json")
        self.assertTrue([e for e in self.gather()["errors"] if e[0] == "settings"])
        self.put(".claude/settings.json", "[]")
        self.assertTrue([e for e in self.gather()["errors"] if e[0] == "settings"])
        self.put(".claude/settings.json", '{"a": 1}')
        state = self.gather()
        self.assertEqual(state["settings"], {"a": 1})
        self.assertEqual([e for e in state["errors"] if e[0] == "settings"], [])

    def test_record_unreadable(self):
        self.put(".config/claude-code-toolkit/install-record.json", "garbage")
        state = self.gather()
        self.assertIsNone(state["record"])
        self.assertTrue(state["record_unreadable"])

    def test_links_and_imports(self):
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        os.makedirs(os.path.join(self.home, ".claude", "commands"))
        own_target = os.path.join(self.repo, "commands", "a.md")
        os.symlink(own_target, os.path.join(self.home, ".claude", "commands", "a.md"))
        os.symlink("/usr/bin/true", os.path.join(self.home, ".claude", "commands", "b.md"))
        os.symlink("/usr/bin/true", os.path.join(self.home, ".local", "bin", "foo"))
        self.put(".local/bin/plain", "x")
        self.put(".claude/CLAUDE.md", "# a\n@" + os.path.join(self.repo, "policy", "CLAUDE.md") + "\n")
        state = self.gather(rows=[("/t", "/l")])
        links = {os.path.basename(p): (rl, owned) for p, rl, owned in state["legacy_links"]}
        self.assertEqual(links, {"a.md": (own_target, True), "b.md": ("/usr/bin/true", False)})
        bin_entries = {os.path.basename(p): (sym, rl) for p, sym, rl in state["local_bin"]}
        self.assertEqual(bin_entries, {"foo": (True, "/usr/bin/true"), "plain": (False, None)})
        self.assertEqual(state["home_bin"], [])
        self.assertEqual(state["managed_links"], [("/t", "/l")])
        self.assertEqual(state["claude_md_imports"],
                         [(2, os.path.realpath(os.path.join(self.repo, "policy", "CLAUDE.md")), True)])
        self.assertEqual(state["policy_real"],
                         os.path.realpath(os.path.join(self.repo, "policy", "CLAUDE.md")))
        self.assertFalse(state["claude_md_is_symlink"])

    def test_mixed_import_line_is_not_import_only(self):
        policy = os.path.join(self.repo, "policy", "CLAUDE.md")
        self.put(".claude/CLAUDE.md", "@" + policy + "\n@" + policy + " some text\n")
        state = self.gather()
        real = os.path.realpath(policy)
        self.assertEqual(state["claude_md_imports"], [(1, real, True), (2, real, False)])

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores chmod")
    def test_unreadable_legacy_dir_is_an_error(self):
        directory = os.path.join(self.home, ".claude", "commands")
        os.makedirs(directory)
        os.chmod(directory, 0)
        self.addCleanup(os.chmod, directory, 0o700)
        state = self.gather()
        self.assertTrue([e for e in state["errors"] if e[0] == "legacy-links"])

    def test_dependencies_from_the_real_repo(self):
        real_repo = os.path.dirname(LIB)
        state = self.gather(repo=real_repo)
        self.assertEqual(state["plugin_dependencies"]["toolkit-maintainer"],
                         ["toolkit-core", "toolkit-build", "skill-prompt-optimizer"])
        self.assertEqual(state["plugin_dependencies"]["toolkit-core"], [])
        self.assertEqual(state["errors"], [])

    def test_17_retained_plugin_dependencies_read_the_live_root(self):
        live = os.path.join(self.tmp, "live")
        os.makedirs(os.path.join(live, ".claude-plugin"))
        with open(os.path.join(live, ".claude-plugin", "plugin.json"), "w") as handle:
            json.dump({"name": "foo", "dependencies": ["toolkit-build"]}, handle)
        plugins = json.dumps([{"id": "foo@other", "scope": "user", "installPath": "/stale"},
                              {"id": "toolkit-core@claude-code-toolkit", "scope": "user"},
                              {"id": "toolkit-build@claude-code-toolkit", "scope": "user"}])
        seen = []

        def installed_root(home, key):
            seen.append(key)
            return live
        with mock.patch.object(uninstall.plugin_paths, "installed_root", installed_root):
            state = self.gather(run_cli=self.cli(plugins=plugins))
        self.assertEqual(seen, ["foo@other"])
        self.assertEqual(state["retained_plugin_dependencies"], {"foo@other": ["toolkit-build"]})
        market = {"name": "claude-code-toolkit", "source": "directory", "path": self.repo}
        state["marketplaces"] = [market]
        state["plugin_dependencies"] = TOOLKIT_DEPS
        actions, _, blockers = uninstall.plan(state, self.repo, self.home)
        self.assertEqual(by(actions, "plugin"), [])
        self.assertTrue(blockers)

    def test_17_retained_plugin_without_a_root_is_a_blocker(self):
        plugins = json.dumps([{"id": "foo@other", "scope": "user"}])
        with mock.patch.object(uninstall.plugin_paths, "installed_root", lambda h, k: None):
            state = self.gather(run_cli=self.cli(plugins=plugins))
        self.assertTrue(state["errors"])
        _, _, blockers = uninstall.plan(state, self.repo, self.home)
        self.assertTrue(blockers)

    def test_retained_plugin_manifest_without_dependencies_key(self):
        live = os.path.join(self.tmp, "live")
        os.makedirs(os.path.join(live, ".claude-plugin"))
        with open(os.path.join(live, ".claude-plugin", "plugin.json"), "w") as handle:
            json.dump({"name": "foo"}, handle)
        plugins = json.dumps([{"id": "foo@other", "scope": "user"}])
        with mock.patch.object(uninstall.plugin_paths, "installed_root", lambda h, k: live):
            state = self.gather(run_cli=self.cli(plugins=plugins))
        self.assertEqual(state["retained_plugin_dependencies"], {"foo@other": []})
        self.assertEqual([e for e in state["errors"] if e[0] == "plugins"], [])
    def plugin_manifest(self, name, body):
        root = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(root, ".claude-plugin"), exist_ok=True)
        with open(os.path.join(root, ".claude-plugin", "plugin.json"), "w") as handle:
            json.dump(body, handle)
        return root

    @staticmethod
    def errors_for(state, surfaces):
        # The scratch repo has no plugin manifests; only the surface under test matters.
        return [e for e in state["errors"] if e[0] in surfaces]

    def test_synced_plugin_reads_install_path_when_there_is_no_live_root(self):
        install = self.plugin_manifest("synced-toprank", {"name": "toprank"})
        plugins = json.dumps([{"id": "toprank@synced", "scope": "synced", "installPath": install}])
        with mock.patch.object(uninstall.plugin_paths, "installed_root", lambda h, k: None):
            state = self.gather(run_cli=self.cli(plugins=plugins))
        self.assertEqual(self.errors_for(state, ("retained-plugins",)), [])
        self.assertEqual(state["retained_plugin_dependencies"], {"toprank@synced": []})
        _, _, blockers = uninstall.plan(state, self.repo, self.home)
        self.assertEqual([b for b in blockers if b.surface in ("retained-plugins", "plugin")], [])

    def test_synced_plugin_with_unreadable_install_path_blocks(self):
        plugins = json.dumps([{"id": "toprank@synced", "scope": "synced",
                               "installPath": os.path.join(self.tmp, "nowhere")}])
        with mock.patch.object(uninstall.plugin_paths, "installed_root", lambda h, k: None):
            state = self.gather(run_cli=self.cli(plugins=plugins))
        self.assertTrue(any(e[0] == "retained-plugins" for e in state["errors"]))
        _, _, blockers = uninstall.plan(state, self.repo, self.home)
        self.assertTrue(blockers)

    def test_directory_marketplace_plugin_never_falls_back_to_install_path(self):
        install = self.plugin_manifest("stale-cache", {"name": "foo"})
        plugins = json.dumps([{"id": "foo@dirmarket", "scope": "user", "installPath": install}])
        markets = json.dumps([{"name": "dirmarket", "source": "directory", "path": "/somewhere"}])
        with mock.patch.object(uninstall.plugin_paths, "installed_root", lambda h, k: None):
            state = self.gather(run_cli=self.cli(plugins=plugins, marketplaces=markets))
        self.assertTrue(any(e[0] == "retained-plugins" for e in state["errors"]))
        self.assertEqual(state["retained_plugin_dependencies"], {})

    def test_baseline_rules_and_autonomous_mode_from_the_repo_files(self):
        claude = os.path.join(self.repo, "claude")
        os.makedirs(claude)
        with open(os.path.join(claude, "permissions.baseline.json"), "w") as handle:
            json.dump({"permissions": {"deny": [DENY], "ask": [PUSH]}}, handle)
        with open(os.path.join(claude, "permissions.autonomous.json"), "w") as handle:
            json.dump({"permissions": {"allow": ["Bash(*)"], "defaultMode": "auto"}}, handle)
        state = self.gather()
        self.assertEqual(state["baseline_rules"], {"allow": ["Bash(*)"], "ask": [PUSH], "deny": [DENY]})
        self.assertEqual(state["autonomous_default_mode"], "auto")
        self.assertEqual(self.errors_for(state, ("baseline-rules",)), [])

    def test_missing_permission_files_are_no_rules(self):
        state = self.gather()
        self.assertEqual(state["baseline_rules"], {})
        self.assertIsNone(state["autonomous_default_mode"])
        self.assertEqual(self.errors_for(state, ("baseline-rules",)), [])

    def test_invalid_permission_file_is_an_error(self):
        claude = os.path.join(self.repo, "claude")
        os.makedirs(claude)
        with open(os.path.join(claude, "permissions.baseline.json"), "w") as handle:
            handle.write("{nope")
        state = self.gather()
        self.assertTrue(any(e[0] == "baseline-rules" for e in state["errors"]))

    def test_xdg_git_config_hookspath_blocks(self):
        xdg = os.path.join(self.home, ".config", "git", "config")
        self.put(".config/git/config", "")
        hooks_dir = os.path.join(self.home, ".config", "claude-code-toolkit", "git-hooks")
        calls = []

        def run_git(argv):
            calls.append(argv)
            if "--file" in argv:
                return 0, "file:%s\tcore.hookspath=%s\n" % (xdg, hooks_dir), ""
            return 0, "", ""
        with mock.patch.dict(os.environ):
            os.environ.pop("XDG_CONFIG_HOME", None)
            state = self.gather(run_git=run_git)
        self.assertIn(["config", "--file", xdg, "--includes", "--show-origin", "--list"], calls)
        self.assertEqual(self.errors_for(state, ("git",)), [])
        actions, _, blockers = uninstall.plan(state, self.repo, self.home)
        self.assertEqual(by(actions, "git"), [])
        self.assertTrue(any(b.surface == "git" and "gitconfig" in b.detail + b.remediation
                            or b.surface == "git" and "config" in b.detail for b in blockers))

    def test_no_xdg_file_means_no_extra_git_call(self):
        calls = []

        def run_git(argv):
            calls.append(argv)
            return 0, "", ""
        with mock.patch.dict(os.environ):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.gather(run_git=run_git)
        self.assertFalse(any("--file" in argv for argv in calls))


DENY = "Read(~/secrets/**)"


class UnrecordedRuleTests(unittest.TestCase):
    def test_unrecorded_toolkit_rules_are_kept_with_a_reason(self):
        actions, kept, _ = run_plan(
            record=record(),
            baseline_rules={"deny": [DENY], "ask": [PUSH]},
            settings={"permissions": {"deny": [DENY], "ask": [PUSH, MERGE]}})
        self.assertEqual(by(actions, "settings"), [])
        named = [k for k in kept if "not recorded as added by this install" in k.reason]
        self.assertEqual(sorted(k.detail for k in named), sorted([DENY, PUSH]))

    def test_recorded_rule_is_an_action_not_a_kept(self):
        actions, kept, _ = run_plan(
            record=record(ask=[PUSH]), baseline_rules={"ask": [PUSH]},
            settings={"permissions": {"ask": [PUSH]}})
        self.assertEqual(len(by(actions, "settings", "remove-rule")), 1)
        self.assertEqual([k for k in kept if "not recorded" in k.reason], [])

    def test_autonomous_mode_without_a_recorded_mode_is_kept(self):
        _, kept, _ = run_plan(record=record(), autonomous_default_mode="auto",
                              settings={"permissions": {"defaultMode": "auto"}})
        self.assertTrue(any(k.detail == "defaultMode" and "not recorded" in k.reason for k in kept))

    def test_autonomous_mode_with_a_recorded_mode_is_not_kept(self):
        actions, kept, _ = run_plan(
            record=record(mode={"set": "auto", "previous": None}), autonomous_default_mode="auto",
            settings={"permissions": {"defaultMode": "auto"}})
        self.assertEqual(len(by(actions, "settings", "restore-mode")), 1)
        self.assertEqual([k for k in kept if k.detail == "defaultMode"], [])

    def test_no_record_keeps_the_single_ownership_unknown(self):
        _, kept, _ = run_plan(baseline_rules={"ask": [PUSH]},
                              settings={"permissions": {"ask": [PUSH]}})
        self.assertEqual([k.reason for k in kept if k.surface == "permissions"],
                         ["no install record: ownership unknown"])


class AcceptedRecordNeverRaisesTests(unittest.TestCase):
    def test_plan_on_every_record_load_accepts(self):
        perms = [{}, {"allow": []}, {"ask": [PUSH]}, {"allow": ["a"], "ask": [], "deny": [DENY]}]
        modes = [None, {"set": "auto", "previous": None}, {"set": "auto", "previous": "plan"}]
        excludes = [None, {"pattern": "p", "created_key": True}, {"pattern": "p", "created_key": False}]
        accepted = 0
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "record.json")
            for perm in perms:
                for mode in modes:
                    for exclude in excludes:
                        data = {"schema": 1, "permissions_added": perm}
                        if mode is not None:
                            data["default_mode"] = mode
                        if exclude is not None:
                            data["claude_md_excludes_added"] = exclude
                        with open(path, "w") as handle:
                            json.dump(data, handle)
                        loaded = uninstall.install_record.load(path)
                        if loaded is None:
                            continue
                        accepted += 1
                        settings = {"permissions": {"allow": ["a"], "ask": [PUSH], "deny": [DENY],
                                                    "defaultMode": "auto"},
                                    "claudeMdExcludes": ["p"]}
                        run_plan(record=loaded, settings=settings,
                                 baseline_rules={"ask": [PUSH]}, autonomous_default_mode="auto")
        self.assertEqual(accepted, len(perms) * len(modes) * len(excludes))


class EnableWorkflowsTests(unittest.TestCase):
    REMOVE = Action("settings", "remove-key", "enableWorkflows", {"expected": True})

    def owned_record(self):
        data = record()
        data["enable_workflows_added"] = True
        return data

    def test_owned_and_true_removes_the_key(self):
        actions, kept, _ = run_plan(record=self.owned_record(),
                                    settings={"enableWorkflows": True})
        self.assertEqual(by(actions, "settings", "remove-key"), [self.REMOVE])
        self.assertEqual([k for k in kept if k.surface == "enableWorkflows"], [])

    def test_owned_but_false_is_kept_not_removed(self):
        actions, kept, _ = run_plan(record=self.owned_record(),
                                    settings={"enableWorkflows": False})
        self.assertEqual(by(actions, "settings", "remove-key"), [])
        self.assertEqual([k for k in kept if k.surface == "enableWorkflows"],
                         [uninstall.Kept("enableWorkflows", "enableWorkflows",
                                         "changed since the toolkit set it")])

    def test_not_owned_true_is_left_alone(self):
        actions, kept, _ = run_plan(record=record(), settings={"enableWorkflows": True})
        self.assertEqual(by(actions, "settings", "remove-key"), [])
        self.assertEqual([k for k in kept if k.surface == "enableWorkflows"], [])


class ApplySettingsActionsTests(unittest.TestCase):
    def apply(self, settings, *actions):
        before = copy.deepcopy(settings)
        result = uninstall.apply_settings_actions(settings, list(actions))
        self.assertEqual(settings, before, "input was mutated")
        return result

    def test_remove_rule_drops_the_emptied_list_and_permissions(self):
        act = Action("settings", "remove-rule", "ask", {"rule": PUSH})
        self.assertEqual(self.apply({"permissions": {"ask": [PUSH]}, "x": 1}, act), {"x": 1})
        self.assertEqual(self.apply({"permissions": {"ask": [PUSH, MERGE], "deny": ["d"]}}, act),
                         {"permissions": {"ask": [MERGE], "deny": ["d"]}})
        self.assertEqual(self.apply({"permissions": {"ask": [PUSH], "deny": ["d"]}}, act),
                         {"permissions": {"deny": ["d"]}})

    def test_restore_mode(self):
        act = Action("settings", "restore-mode", "defaultMode", {"previous": "plan"})
        self.assertEqual(self.apply({"permissions": {"defaultMode": "auto", "deny": ["d"]}}, act),
                         {"permissions": {"defaultMode": "plan", "deny": ["d"]}})
        none = Action("settings", "restore-mode", "defaultMode", {"previous": None})
        self.assertEqual(self.apply({"permissions": {"defaultMode": "auto", "deny": ["d"]}}, none),
                         {"permissions": {"deny": ["d"]}})
        self.assertEqual(self.apply({"permissions": {"defaultMode": "auto"}, "x": 1}, none), {"x": 1})

    def test_remove_key_only_when_value_still_equals(self):
        act = Action("settings", "remove-key", "statusLine", {"expected": {"type": "command"}})
        self.assertEqual(self.apply({"statusLine": {"type": "command"}, "x": 1}, act), {"x": 1})
        edited = {"statusLine": {"type": "command", "padding": 1}}
        self.assertEqual(self.apply(edited, act), edited)

    def test_remove_exclude(self):
        keep = Action("settings", "remove-exclude", "claudeMdExcludes", {"pattern": "p", "created_key": False})
        drop = Action("settings", "remove-exclude", "claudeMdExcludes", {"pattern": "p", "created_key": True})
        self.assertEqual(self.apply({"claudeMdExcludes": ["p"]}, keep), {"claudeMdExcludes": []})
        self.assertEqual(self.apply({"claudeMdExcludes": ["p"]}, drop), {})
        self.assertEqual(self.apply({"claudeMdExcludes": ["p", "q"]}, drop), {"claudeMdExcludes": ["q"]})

    def test_remove_hook_drops_empty_blocks_events_and_hooks(self):
        act = Action("settings", "remove-hook", "Stop", {"command": "old"})
        settings = {"hooks": {"Stop": [{"hooks": [{"command": "old"}]},
                                       {"hooks": [{"command": "old"}, {"command": "mine"}]}],
                              "Start": [{"hooks": [{"command": "s"}]}]}}
        self.assertEqual(self.apply(settings, act),
                         {"hooks": {"Stop": [{"hooks": [{"command": "mine"}]}],
                                    "Start": [{"hooks": [{"command": "s"}]}]}})
        only = {"hooks": {"Stop": [{"hooks": [{"command": "old"}]}]}}
        self.assertEqual(self.apply(only, act), {})

    def test_remove_enabled_and_known_marketplace(self):
        enabled = Action("settings", "remove-enabled", "a@claude-code-toolkit", {})
        self.assertEqual(self.apply({"enabledPlugins": {"a@claude-code-toolkit": True, "b@x": True}}, enabled),
                         {"enabledPlugins": {"b@x": True}})
        known = Action("settings", "remove-known-marketplace", "claude-code-toolkit", {})
        self.assertEqual(self.apply({"extraKnownMarketplaces": {"claude-code-toolkit": {}, "o": {}}}, known),
                         {"extraKnownMarketplaces": {"o": {}}})

    def test_non_settings_actions_are_ignored(self):
        settings = {"x": 1}
        self.assertEqual(self.apply(settings, Action("link", "unlink", "/p", {"expected_target": "t"})), settings)


class ApplyLinkTests(unittest.TestCase):
    """`apply` re-reads each link before deleting it: one repointed since the plan is left alone."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.link = os.path.join(self.root, "claude-x")

    def run_apply(self, expected):
        def refuse(argv):
            raise AssertionError("a link-only plan must not call %r" % (argv,))
        return uninstall.apply([Action("link", "unlink", self.link, {"expected_target": expected})],
                               REPO, self.root, os.path.join(self.root, "settings.json"),
                               refuse, refuse)

    def test_a_link_still_at_its_planned_target_is_removed(self):
        os.symlink("/r/repo/tools/x/claude-x", self.link)
        ok, messages = self.run_apply("/r/repo/tools/x/claude-x")
        self.assertTrue(ok, messages)
        self.assertFalse(os.path.lexists(self.link))

    def test_a_link_repointed_since_the_plan_is_kept_and_the_run_fails(self):
        os.symlink("/somewhere/else/mine", self.link)
        ok, messages = self.run_apply("/r/repo/tools/x/claude-x")
        self.assertFalse(ok, messages)
        self.assertTrue(any("now points at /somewhere/else/mine" in m for m in messages), messages)
        self.assertEqual("/somewhere/else/mine", os.readlink(self.link))


if __name__ == "__main__":
    unittest.main()
