#!/usr/bin/env python3
"""Tests for lib/plugin_paths.py: the one plugin-root resolver, the owned-link
test install.sh prunes with, and the bare-name clash inventory. Every test runs
in a sandbox HOME; nothing ambient is read."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LIB = os.path.join(REPO_ROOT, "lib")
sys.path.insert(0, LIB)

import plugin_paths  # noqa: E402

SCRIPT = os.path.join(LIB, "plugin_paths.py")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def write_json(path, data):
    write(path, json.dumps(data))


def profile(plugin, directory, default=False):
    return {"plugin": plugin, "dir": directory, "default": default, "requires": [],
            "agents": False, "summary": "x"}


def write_registry(repo, profiles=None):
    """A registry in `repo`: core-only unless `profiles` names others."""
    if profiles is None:
        profiles = {"core": profile("toolkit-core", "plugin", default=True)}
    write_json(os.path.join(repo, "claude", "profiles.json"), {"profiles": profiles})


def link_plugin_skills(repo, directory="plugin", target="../skills"):
    """`<repo>/<directory>/skills` -> `target`, the way a plugin ships its skills."""
    os.makedirs(os.path.join(repo, directory), exist_ok=True)
    os.symlink(target, os.path.join(repo, directory, "skills"))


def core_only_layout(repo):
    """What every clash fixture needs: a core-only registry and plugin/skills."""
    write_registry(repo)
    link_plugin_skills(repo)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.home = os.path.join(self.tmp, "home")
        self.claude = os.path.join(self.home, ".claude")
        os.makedirs(self.claude)

    def directory_marketplace(self, name="claude-code-toolkit", plugin="toolkit-core",
                              source="./plugin", checkout=None):
        """Register a directory-source marketplace; return its checkout path."""
        checkout = checkout or os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(checkout, "plugin"), exist_ok=True)
        write_json(os.path.join(checkout, ".claude-plugin", "marketplace.json"),
                   {"name": name, "plugins": [{"name": plugin, "source": source}]})
        known = os.path.join(self.claude, "plugins", "known_marketplaces.json")
        try:
            with open(known, encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError:
            data = {}
        data[name] = {"source": {"source": "directory", "path": checkout},
                      "installLocation": checkout}
        write_json(known, data)
        return checkout

    def hosted_plugin(self, marketplace="mp", plugin="p"):
        path = os.path.join(self.claude, "plugins", "marketplaces", marketplace,
                            "plugins", plugin)
        os.makedirs(path)
        return path

    def install_record(self, key, path):
        """Record `key` as installed at `path` (how Claude Code locates hosted plugins)."""
        record = os.path.join(self.claude, "plugins", "installed_plugins.json")
        write_json(record, {"version": 2, "plugins": {
            key: [{"scope": "user", "installPath": path}]}})

    def settings(self, enabled):
        write_json(os.path.join(self.claude, "settings.json"),
                   {"enabledPlugins": enabled})


class TestPluginRoot(Sandbox):
    def test_directory_marketplace_returns_realpath(self):
        checkout = self.directory_marketplace()
        self.assertEqual(plugin_paths.plugin_root(self.home, "toolkit-core"),
                         os.path.realpath(os.path.join(checkout, "plugin")))

    def test_directory_source_symlink_is_resolved(self):
        checkout = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(self.tmp, "real-plugin"))
        os.makedirs(checkout)
        os.symlink(os.path.join(self.tmp, "real-plugin"),
                   os.path.join(checkout, "plugin"))
        self.directory_marketplace(checkout=checkout)
        self.assertEqual(plugin_paths.plugin_root(self.home, "toolkit-core"),
                         os.path.join(self.tmp, "real-plugin"))

    def test_hosted_fallback(self):
        path = self.hosted_plugin()
        self.assertEqual(plugin_paths.plugin_root(self.home, "p"), path)

    def test_malformed_known_marketplaces_falls_through(self):
        known = os.path.join(self.claude, "plugins", "known_marketplaces.json")
        path = self.hosted_plugin()
        for body in ("{not json", "[]", json.dumps({"x": {}}),
                     json.dumps({"x": {"source": "directory"}}),
                     json.dumps({"x": None})):
            write(known, body)
            self.assertEqual(plugin_paths.plugin_root(self.home, "p"), path, body)
        os.rmdir(path)
        self.assertIsNone(plugin_paths.plugin_root(self.home, "p"))

    def test_plugin_absent_from_directory_marketplace(self):
        self.directory_marketplace(plugin="other")
        self.assertIsNone(plugin_paths.plugin_root(self.home, "toolkit-core"))

    def test_non_string_source_is_ignored(self):
        self.directory_marketplace(source={"source": "github"})
        self.assertIsNone(plugin_paths.plugin_root(self.home, "toolkit-core"))

    def test_missing_source_directory_is_none(self):
        self.directory_marketplace(source="./not-there")
        self.assertIsNone(plugin_paths.plugin_root(self.home, "toolkit-core"))

    def test_a_bad_entry_does_not_hide_a_later_good_one(self):
        checkout = self.directory_marketplace()
        write_json(os.path.join(checkout, ".claude-plugin", "marketplace.json"),
                   {"name": "m", "plugins": [
                       {"name": "toolkit-core", "source": {"source": "github"}},
                       {"name": "toolkit-core", "source": "./plugin"}]})
        self.assertEqual(plugin_paths.plugin_root(self.home, "toolkit-core"),
                         os.path.realpath(os.path.join(checkout, "plugin")))

    def test_a_hosted_marketplace_is_not_read_as_a_directory_source(self):
        # Its own marketplace.json lists the plugin, but only a directory
        # source is the checkout; a hosted copy resolves through marketplaces/.
        location = os.path.join(self.tmp, "hosted-copy")
        os.makedirs(os.path.join(location, "plugins", "p"))
        write_json(os.path.join(location, ".claude-plugin", "marketplace.json"),
                   {"name": "h", "plugins": [{"name": "p", "source": "./plugins/p"}]})
        write_json(os.path.join(self.claude, "plugins", "known_marketplaces.json"),
                   {"h": {"source": {"source": "github", "repo": "o/r"},
                          "installLocation": location}})
        self.assertIsNone(plugin_paths.plugin_root(self.home, "p"))

    def test_install_path_is_never_read(self):
        decoy = os.path.join(self.tmp, "stale-cache")
        os.makedirs(decoy)
        write_json(os.path.join(self.claude, "plugins", "installed_plugins.json"),
                   {"version": 2, "plugins": {"toolkit-core@claude-code-toolkit": [
                       {"installPath": decoy}]}})
        self.assertIsNone(plugin_paths.plugin_root(self.home, "toolkit-core"))
        checkout = self.directory_marketplace()
        self.assertEqual(plugin_paths.plugin_root(self.home, "toolkit-core"),
                         os.path.realpath(os.path.join(checkout, "plugin")))

    def test_nothing_installed(self):
        self.assertIsNone(plugin_paths.plugin_root(self.home, "toolkit-core"))
        self.assertIsNone(plugin_paths.plugin_root(None, "toolkit-core"))


class TestInstalledRoot(Sandbox):
    def cache_plugin(self, key="railway@mp", scopes=("user",), make_dir=True):
        """Record a hosted plugin in installed_plugins.json; return its cache dir."""
        cache = os.path.join(self.claude, "plugins", "cache", key.replace("@", "-"))
        if make_dir:
            os.makedirs(cache)
        write_json(os.path.join(self.claude, "plugins", "installed_plugins.json"),
                   {"version": 2, "plugins": {key: [
                       {"scope": scope, "installPath": cache + "-" + scope
                        if scope != "user" else cache} for scope in scopes]}})
        return cache

    def test_hosted_plugin_resolves_through_installed_plugins(self):
        cache = self.cache_plugin()
        self.assertEqual(plugin_paths.installed_root(self.home, "railway@mp"), cache)

    def test_user_scope_entry_wins_over_an_earlier_one(self):
        cache = self.cache_plugin(scopes=("project", "user"))
        os.makedirs(cache + "-project")
        self.assertEqual(plugin_paths.installed_root(self.home, "railway@mp"), cache)

    def test_first_entry_when_none_is_user_scope(self):
        self.cache_plugin(scopes=("project",))
        os.makedirs(os.path.join(self.claude, "plugins", "cache", "railway-mp-project"))
        self.assertEqual(plugin_paths.installed_root(self.home, "railway@mp"),
                         os.path.join(self.claude, "plugins", "cache",
                                      "railway-mp") + "-project")

    def test_missing_install_path_directory_is_none(self):
        self.cache_plugin(make_dir=False)
        self.assertIsNone(plugin_paths.installed_root(self.home, "railway@mp"))

    def test_directory_source_uses_the_checkout_not_the_stale_cache(self):
        checkout = self.directory_marketplace()
        stale = self.cache_plugin(key="toolkit-core@claude-code-toolkit")
        root = plugin_paths.installed_root(self.home, "toolkit-core@claude-code-toolkit")
        self.assertEqual(root, os.path.join(checkout, "plugin"))
        self.assertNotEqual(root, stale)

    def test_unknown_or_malformed_is_none_and_never_raises(self):
        self.assertIsNone(plugin_paths.installed_root(self.home, "x@y"))
        self.assertIsNone(plugin_paths.installed_root(self.home, "no-marketplace"))
        self.assertIsNone(plugin_paths.installed_root(None, "x@y"))
        self.assertIsNone(plugin_paths.installed_root(self.home, None))
        write(os.path.join(self.claude, "plugins", "installed_plugins.json"), "{bad")
        self.assertIsNone(plugin_paths.installed_root(self.home, "x@y"))
        write_json(os.path.join(self.claude, "plugins", "installed_plugins.json"),
                   {"plugins": {"x@y": "nope"}})
        self.assertIsNone(plugin_paths.installed_root(self.home, "x@y"))


class TestInstalledRootMarketplace(Sandbox):
    def two_marketplaces(self):
        roots = {}
        for name in ("aaa", "zzz"):
            checkout = os.path.join(self.tmp, "checkout-" + name)
            self.directory_marketplace(name=name, plugin="shared",
                                       checkout=checkout)
            roots[name] = os.path.realpath(os.path.join(checkout, "plugin"))
        return roots

    def test_directory_source_resolves_in_its_own_marketplace(self):
        roots = self.two_marketplaces()
        self.assertEqual(plugin_paths.installed_root(self.home, "shared@zzz"),
                         roots["zzz"])
        self.assertEqual(plugin_paths.installed_root(self.home, "shared@aaa"),
                         roots["aaa"])

    def test_directory_source_without_the_plugin_is_none(self):
        self.two_marketplaces()
        self.assertIsNone(plugin_paths.installed_root(self.home, "other@zzz"))

    def test_clash_is_read_from_the_enabled_marketplace_copy(self):
        roots = self.two_marketplaces()
        write(os.path.join(roots["zzz"], "skills", "build", "SKILL.md"), "x")
        repo = os.path.join(self.tmp, "repo")
        write(os.path.join(repo, "skills", "build", "SKILL.md"), "x")
        core_only_layout(repo)
        self.settings({"shared@zzz": True})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo),
                         ["shared:build and toolkit-core:build share a bare name: "
                          "/build runs whichever plugin loaded first. Retire one copy."])


class TestOwnedLegacyLinks(Sandbox):
    def test_ownership(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(repo, "commands"))
        os.makedirs(os.path.join(self.tmp, "foreign"))
        os.makedirs(os.path.join(self.tmp, "repo-old"))
        cmds = os.path.join(self.claude, "commands")
        os.makedirs(cmds)
        write(os.path.join(repo, "commands", "a.md"), "x")
        os.symlink(os.path.join(repo, "commands", "a.md"), os.path.join(cmds, "abs.md"))
        # ~/.claude/commands/../../../repo/commands: relative to the link's parent.
        os.symlink(os.path.relpath(os.path.join(repo, "commands", "a.md"), cmds),
                   os.path.join(cmds, "rel.md"))
        os.symlink(os.path.join(repo, "commands", "gone.md"), os.path.join(cmds, "dangling.md"))
        os.symlink(os.path.join(repo, "..", "foreign"), os.path.join(cmds, "traversal.md"))
        os.symlink(os.path.join(repo + "-old", "x"), os.path.join(cmds, "sibling.md"))
        os.symlink(os.path.join(self.tmp, "foreign"), os.path.join(cmds, "elsewhere.md"))
        write(os.path.join(cmds, "real.md"), "x")
        os.makedirs(os.path.join(cmds, "realdir"))
        found = {os.path.basename(p): owned
                 for p, owned in plugin_paths.owned_legacy_links(self.home, repo)}
        self.assertEqual(found, {"abs.md": True, "rel.md": True, "dangling.md": True,
                                 "traversal.md": False, "sibling.md": False,
                                 "elsewhere.md": False})

    def test_walks_all_four_dirs_in_order_and_tolerates_missing(self):
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        for sub in ("agents", "skills"):
            os.makedirs(os.path.join(self.claude, sub))
            os.symlink(os.path.join(repo, sub + "-x"),
                       os.path.join(self.claude, sub, "z"))
        got = plugin_paths.owned_legacy_links(self.home, repo)
        self.assertEqual([p for p, _ in got],
                         [os.path.join(self.claude, "agents", "z"),
                          os.path.join(self.claude, "skills", "z")])


class TestSkillClashes(Sandbox):
    def repo_with_skill(self, name="build"):
        repo = os.path.join(self.tmp, "repo")
        write(os.path.join(repo, "skills", name, "SKILL.md"), "x")
        os.makedirs(os.path.join(repo, "skills", "no-skill-md"))
        core_only_layout(repo)
        return repo

    def other_plugin(self, shipping="build"):
        root = self.hosted_plugin()
        self.install_record("p@mp", root)
        write(os.path.join(root, "skills", shipping, "SKILL.md"), "x")
        return root

    def test_personal_skill_shadows(self):
        repo = self.repo_with_skill()
        os.makedirs(os.path.join(self.claude, "skills", "build"))
        got = plugin_paths.skill_clashes(self.home, repo)
        self.assertEqual(got, ["~/.claude/skills/build shadows toolkit-core:build: a bare "
                               "/build runs the personal copy. Remove it (./install.sh "
                               "removes it if it links into this checkout)."])

    def test_dangling_personal_skill_link_still_shadows(self):
        repo = self.repo_with_skill()
        os.makedirs(os.path.join(self.claude, "skills"))
        os.symlink(os.path.join(self.tmp, "nowhere"),
                   os.path.join(self.claude, "skills", "build"))
        self.assertEqual(len(plugin_paths.skill_clashes(self.home, repo)), 1)

    def test_personal_command_file_shadows(self):
        repo = self.repo_with_skill()
        write(os.path.join(self.claude, "commands", "build.md"), "x")
        got = plugin_paths.skill_clashes(self.home, repo)
        self.assertEqual(got, ["~/.claude/commands/build.md shadows toolkit-core:build: a bare "
                               "/build may run the personal command. Remove it (./install.sh "
                               "removes it if it links into this checkout)."])

    def test_enabled_other_plugin_clash(self):
        repo = self.repo_with_skill()
        self.other_plugin()
        self.settings({"p@mp": True})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo),
                         ["p:build and toolkit-core:build share a bare name: /build runs "
                          "whichever plugin loaded first. Retire one copy."])

    def test_cache_only_hosted_plugin_is_located_and_clashes(self):
        repo = self.repo_with_skill()
        cache = os.path.join(self.claude, "plugins", "cache", "railway-mp")
        write(os.path.join(cache, "skills", "build", "SKILL.md"), "x")
        write_json(os.path.join(self.claude, "plugins", "installed_plugins.json"),
                   {"version": 2, "plugins": {"railway@mp": [
                       {"scope": "user", "installPath": cache}]}})
        self.settings({"railway@mp": True})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo),
                         ["railway:build and toolkit-core:build share a bare name: "
                          "/build runs whichever plugin loaded first. Retire one copy."])

    def test_other_plugins_command_clashes(self):
        repo = self.repo_with_skill()
        root = self.hosted_plugin()
        self.install_record("p@mp", root)
        write(os.path.join(root, "commands", "build.md"), "x")
        self.settings({"p@mp": True})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo),
                         ["p:build (a command) and toolkit-core:build share a bare "
                          "name: /build runs whichever plugin loaded first. "
                          "Retire one copy."])

    def test_disabled_plugin_and_self_never_clash(self):
        repo = self.repo_with_skill()
        self.other_plugin()
        self.settings({"p@mp": False})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo), [])
        root = self.directory_marketplace()
        write(os.path.join(root, "plugin", "skills", "build", "SKILL.md"), "x")
        self.settings({"p@mp": False, "toolkit-core@claude-code-toolkit": True})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo), [])

    def test_other_plugin_without_that_skill(self):
        repo = self.repo_with_skill()
        self.other_plugin(shipping="different")
        self.settings({"p@mp": True})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo), [])

    def test_no_settings_file_is_a_fresh_machine(self):
        self.assertEqual(plugin_paths.skill_clashes(self.home, self.repo_with_skill()), [])

    def test_malformed_settings_fails_strict(self):
        repo = self.repo_with_skill()
        incomplete = ("cannot read enabledPlugins from ~/.claude/settings.json: "
                      "the plugin clash check is incomplete")
        for body in ("{not json", "[]", json.dumps({"enabledPlugins": []}),
                     json.dumps({"enabledPlugins": "x"})):
            write(os.path.join(self.claude, "settings.json"), body)
            self.assertEqual(plugin_paths.skill_clashes(self.home, repo), [incomplete], body)

    def test_settings_without_enabled_plugins_is_fine(self):
        write_json(os.path.join(self.claude, "settings.json"), {"model": "x"})
        self.assertEqual(plugin_paths.skill_clashes(self.home, self.repo_with_skill()), [])

    def test_unlocatable_enabled_plugin_fails_strict_once(self):
        repo = self.repo_with_skill()
        self.settings({"ghost@a": True, "ghost@b": True})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo),
                         ["enabled plugin ghost could not be located: the plugin "
                          "clash check is incomplete"])

    def siblings_repo(self):
        """Registry core (plugin/) + extra (plugin-extra/), one skill each."""
        repo = os.path.join(self.tmp, "repo")
        write(os.path.join(repo, "skills", "build", "SKILL.md"), "x")
        write(os.path.join(repo, "skills-extra", "extra-skill", "SKILL.md"), "x")
        write_registry(repo, {
            "core": profile("toolkit-core", "plugin", default=True),
            "extra": profile("extra", "plugin-extra")})
        link_plugin_skills(repo)
        link_plugin_skills(repo, "plugin-extra", "../skills-extra")
        return repo

    def test_sibling_plugins_enabled_together_do_not_clash(self):
        repo = self.siblings_repo()
        self.settings({"toolkit-core@claude-code-toolkit": True, "extra@claude-code-toolkit": True})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo), [])

    def test_two_toolkit_profiles_shipping_one_name_clash(self):
        # Merge Review: skipping the toolkit's whole marketplace hid a real
        # clash between two of its own plugins. A name shipped by two toolkit
        # plugins is a clash whether or not either is enabled.
        repo = self.siblings_repo()
        write(os.path.join(repo, "skills-extra", "build", "SKILL.md"), "x")
        self.settings({"toolkit-core@claude-code-toolkit": True, "extra@claude-code-toolkit": True})
        got = plugin_paths.skill_clashes(self.home, repo)
        self.assertEqual(len(got), 1, got)
        self.assertIn("extra:build", got[0])
        self.assertIn("toolkit-core:build", got[0])

    def test_a_sibling_skill_is_worded_with_its_own_plugin(self):
        repo = self.siblings_repo()
        os.makedirs(os.path.join(self.claude, "skills", "extra-skill"))
        got = plugin_paths.skill_clashes(self.home, repo)
        self.assertEqual(len(got), 1, got)
        self.assertIn("shadows extra:extra-skill:", got[0])
        self.assertNotIn("toolkit-core:extra-skill", got[0])

    def test_a_third_party_plugin_clashes_with_core(self):
        repo = self.siblings_repo()
        self.other_plugin(shipping="build")
        self.settings({"p@mp": True, "extra@claude-code-toolkit": True})
        self.assertEqual(plugin_paths.skill_clashes(self.home, repo),
                         ["p:build and toolkit-core:build share a bare name: /build runs "
                          "whichever plugin loaded first. Retire one copy."])

    def test_a_malformed_registry_says_the_check_is_incomplete(self):
        repo = self.repo_with_skill()
        write(os.path.join(repo, "claude", "profiles.json"), "{not json")
        got = plugin_paths.skill_clashes(self.home, repo)
        self.assertEqual(len(got), 1, got)
        self.assertTrue(got[0].startswith("plugin inventory unreadable ("), got)
        self.assertTrue(got[0].endswith("): the plugin clash check is incomplete"), got)

    def test_an_unlistable_skills_dir_says_the_check_is_incomplete(self):
        # A profile whose skills cannot be listed must not read as one that
        # ships none: its clashes would vanish from the report.
        repo = self.repo_with_skill()
        os.remove(os.path.join(repo, "plugin", "skills"))
        os.makedirs(os.path.join(self.claude, "skills", "build"))
        got = plugin_paths.skill_clashes(self.home, repo)
        self.assertEqual(got, ["plugin inventory unreadable (cannot list plugin/skills): "
                               "the plugin clash check is incomplete"])

    def test_a_dangling_skills_link_says_the_check_is_incomplete(self):
        repo = self.siblings_repo()
        os.remove(os.path.join(repo, "plugin-extra", "skills"))
        os.symlink("../nowhere", os.path.join(repo, "plugin-extra", "skills"))
        got = plugin_paths.skill_clashes(self.home, repo)
        self.assertEqual(len(got), 1, got)
        self.assertIn("cannot list plugin-extra/skills", got[0])

    def test_a_missing_registry_says_the_check_is_incomplete(self):
        repo = self.repo_with_skill()
        os.remove(os.path.join(repo, "claude", "profiles.json"))
        got = plugin_paths.skill_clashes(self.home, repo)
        self.assertEqual(len(got), 1, got)
        self.assertIn("the plugin clash check is incomplete", got[0])

    def test_messages_are_sorted(self):
        repo = self.repo_with_skill()
        os.makedirs(os.path.join(self.claude, "skills", "build"))
        write(os.path.join(self.claude, "commands", "build.md"), "x")
        got = plugin_paths.skill_clashes(self.home, repo)
        self.assertEqual(len(got), 2)
        self.assertEqual(got, sorted(got))


class TestCli(Sandbox):
    def test_utf8_stream_loop_present(self):
        # Pinned UTF-8 output: a non-ASCII path never fails on a console
        # set to a legacy code page.
        with open(SCRIPT, encoding="utf-8") as handle:
            self.assertIn("reconfigure(encoding=\"utf-8\"", handle.read())

    def run_cli(self, *argv):
        return subprocess.run([sys.executable, SCRIPT] + list(argv),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def test_legacy_and_clashes_output(self):
        repo = os.path.join(self.tmp, "repo")
        write(os.path.join(repo, "skills", "build", "SKILL.md"), "x")
        core_only_layout(repo)
        os.makedirs(os.path.join(self.claude, "commands"))
        os.symlink(os.path.join(repo, "a"), os.path.join(self.claude, "commands", "mine"))
        os.symlink(os.path.join(self.tmp, "f"), os.path.join(self.claude, "commands", "theirs"))
        os.makedirs(os.path.join(self.claude, "skills", "build"))
        res = self.run_cli("legacy", self.home, repo)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.splitlines(), [
            "owned\t" + os.path.join(self.claude, "commands", "mine"),
            "kept\t" + os.path.join(self.claude, "commands", "theirs")])
        res = self.run_cli("clashes", self.home, repo)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(len(res.stdout.splitlines()), 1)
        self.assertIn("~/.claude/skills/build shadows toolkit-core:build", res.stdout)

    def test_bad_argv_exits_2(self):
        for argv in ([], ["legacy"], ["legacy", "a"], ["nope", "a", "b"],
                     ["clashes", "a", "b", "c"]):
            res = self.run_cli(*argv)
            self.assertEqual(res.returncode, 2, argv)
            self.assertIn("usage", res.stderr.lower())
            self.assertEqual(res.stdout, "")


class TestNeverRaises(Sandbox):
    """A non-string or empty home (or a non-string repo) yields the documented
    empty result instead of an exception."""
    BAD_HOMES = (None, "", 123)

    def test_plugin_root_and_installed_root_return_none(self):
        # A hosted marketplace dir exists, so a bad plugin name reaches the join.
        os.makedirs(os.path.join(self.claude, "plugins", "marketplaces", "m", "plugins"))
        for plugin in (None, "", 123):
            self.assertIsNone(plugin_paths.plugin_root(self.home, plugin), plugin)
        for home in self.BAD_HOMES:
            self.assertIsNone(plugin_paths.plugin_root(home, "toolkit-core"), home)
            self.assertIsNone(plugin_paths.installed_root(home, "p@mp"), home)

    def test_owned_legacy_links_returns_empty(self):
        for home in self.BAD_HOMES:
            self.assertEqual(plugin_paths.owned_legacy_links(home, "/repo"), [], home)
        for repo in (None, "", 123):
            self.assertEqual(plugin_paths.owned_legacy_links(self.home, repo), [], repo)

    def test_skill_clashes_returns_empty(self):
        for home in self.BAD_HOMES:
            self.assertEqual(plugin_paths.skill_clashes(home, "/repo"), [], home)
        for repo in (None, "", 123):
            self.assertEqual(plugin_paths.skill_clashes(self.home, repo), [], repo)


class TestDirectoryPluginRoots(Sandbox):
    def manifest(self, checkout, plugins):
        write_json(os.path.join(checkout, ".claude-plugin", "marketplace.json"),
                   {"name": "m", "plugins": plugins})

    def test_lists_every_directory_root_and_no_hosted_one(self):
        first = self.directory_marketplace(
            name="one", plugin="a", checkout=os.path.join(self.tmp, "one"))
        os.makedirs(os.path.join(first, "other"))
        self.manifest(first, [{"name": "a", "source": "./plugin"},
                              {"name": "b", "source": "./other"}])
        second = self.directory_marketplace(
            name="two", plugin="c", checkout=os.path.join(self.tmp, "two"))
        hosted = self.hosted_plugin()
        known = os.path.join(self.claude, "plugins", "known_marketplaces.json")
        with open(known, encoding="utf-8") as handle:
            data = json.load(handle)
        hosted_location = os.path.dirname(os.path.dirname(hosted))
        data["hosted"] = {"source": {"source": "github", "repo": "o/r"},
                          "installLocation": hosted_location}
        write_json(known, data)
        # A real hosted marketplace carries its own marketplace.json, so only
        # the source-kind check can keep its plugin out of the list.
        self.manifest(hosted_location, [
            {"name": os.path.basename(hosted),
             "source": "./plugins/" + os.path.basename(hosted)}])
        self.assertEqual(
            plugin_paths.directory_plugin_roots(self.home),
            sorted([os.path.join(first, "plugin"), os.path.join(first, "other"),
                    os.path.join(second, "plugin")]))

    def test_realpaths_a_symlinked_checkout(self):
        checkout = self.directory_marketplace()
        link = os.path.join(self.tmp, "link")
        os.symlink(checkout, link)
        known = os.path.join(self.claude, "plugins", "known_marketplaces.json")
        with open(known, encoding="utf-8") as handle:
            data = json.load(handle)
        data["claude-code-toolkit"]["installLocation"] = link
        write_json(known, data)
        self.assertEqual(plugin_paths.directory_plugin_roots(self.home),
                         [os.path.join(checkout, "plugin")])

    def test_duplicate_roots_are_listed_once(self):
        checkout = self.directory_marketplace()
        self.manifest(checkout, [{"name": "a", "source": "./plugin"},
                                 {"name": "b", "source": "./plugin"}])
        self.assertEqual(plugin_paths.directory_plugin_roots(self.home),
                         [os.path.join(checkout, "plugin")])

    def test_skips_non_string_source_and_missing_directory(self):
        checkout = self.directory_marketplace()
        self.manifest(checkout, [{"name": "x", "source": {"source": "url"}},
                                 {"name": "y", "source": "./nowhere"},
                                 {"name": "z", "source": 7},
                                 {"name": "a", "source": "./plugin"}])
        self.assertEqual(plugin_paths.directory_plugin_roots(self.home),
                         [os.path.join(checkout, "plugin")])

    def test_bad_home_returns_empty(self):
        for home in (None, "", 123):
            self.assertEqual(plugin_paths.directory_plugin_roots(home), [], home)

    def test_malformed_known_marketplaces_returns_empty(self):
        known = os.path.join(self.claude, "plugins", "known_marketplaces.json")
        os.makedirs(os.path.dirname(known), exist_ok=True)
        for content in ("{not json", "[]", '{"m": "text"}', '{"m": {"source": 3}}'):
            with open(known, "w", encoding="utf-8") as handle:
                handle.write(content)
            self.assertEqual(plugin_paths.directory_plugin_roots(self.home), [],
                             content)

    def test_malformed_marketplace_json_skips_that_entry_only(self):
        good = self.directory_marketplace(
            name="good", plugin="g", checkout=os.path.join(self.tmp, "good"))
        bad = self.directory_marketplace(
            name="bad", plugin="b", checkout=os.path.join(self.tmp, "bad"))
        for content in ("{not json", "[]", '{"plugins": "x"}', '{"plugins": [3]}'):
            with open(os.path.join(bad, ".claude-plugin", "marketplace.json"), "w",
                      encoding="utf-8") as handle:
                handle.write(content)
            self.assertEqual(plugin_paths.directory_plugin_roots(self.home),
                             [os.path.join(good, "plugin")], content)


if __name__ == "__main__":
    unittest.main()
