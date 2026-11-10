#!/usr/bin/env python3
"""Tests for the profile registry and selection (lib/profiles.py).

Hermetic: every config is a temp file, and the CLI runs in a subprocess with
CLAUDE_TOOLKIT_CONFIG set, so the developer's real config never leaks in.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections import OrderedDict

LIB = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
REPO = os.path.dirname(LIB)
sys.path.insert(0, LIB)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_manifest
import profiles

SCRIPT = os.path.join(LIB, "profiles.py")


def entry(plugin, dir_, default=False, requires=(), agents=False):
    return {"plugin": plugin, "dir": dir_, "default": default,
            "requires": list(requires), "agents": agents,
            "summary": "%s summary" % plugin}


def registry_doc(**overrides):
    """A minimal valid registry document; overrides replace whole profiles."""
    doc = {"profiles": OrderedDict([("core", entry("toolkit-core", "plugin",
                                                  default=True, agents=True))])}
    for name, value in overrides.items():
        if value is None:
            doc["profiles"].pop(name, None)
        else:
            doc["profiles"][name] = value
    return doc


def four_profile_registry():
    reg = OrderedDict()
    for name, plugin_entry in (
        ("core", entry("toolkit-core", "plugin", default=True, agents=True)),
        ("continuity", entry("toolkit-continuity", "plugin-continuity", default=True)),
        ("builder", entry("toolkit-builder", "plugin-builder", requires=["core"])),
        ("maintainer", entry("toolkit-maintainer", "plugin-maintainer", requires=["builder"])),
    ):
        plugin_entry["name"] = name
        reg[name] = plugin_entry
    return reg


class TempCase(unittest.TestCase):

    def tmp(self):
        path = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, path, True)
        return path

    def repo_with(self, doc, raw=None):
        repo = self.tmp()
        os.makedirs(os.path.join(repo, "claude"))
        with open(os.path.join(repo, profiles.REGISTRY), "w") as handle:
            handle.write(raw if raw is not None else json.dumps(doc))
        return repo

    def config_file(self, data, raw=None):
        path = os.path.join(self.tmp(), "config.json")
        with open(path, "w") as handle:
            handle.write(raw if raw is not None else json.dumps(data))
        return path

    def run_cli(self, args, config_path):
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config_path)
        return subprocess.run([sys.executable, SCRIPT] + args, env=env,
                              stdin=subprocess.DEVNULL, capture_output=True,
                              text=True)


class TestLoadRegistry(TempCase):

    def test_shipped_registry_has_the_five_profiles_with_every_field(self):
        reg = profiles.load_registry(REPO)
        expected = [
            ("core", "toolkit-core", "plugin", True, [], False),
            ("continuity", "toolkit-continuity", "plugin-continuity", True, ["core"], False),
            ("builder", "toolkit-build", "plugin-build", False, ["core"], True),
            ("maintainer", "toolkit-maintainer", "plugin-maintainer", False,
             ["core", "builder"], False),
            ("mac", "toolkit-mac", "plugin-mac", False, ["core"], False),
        ]
        self.assertEqual(list(reg), [row[0] for row in expected])
        for name, plugin, dir_, default, requires, agents in expected:
            with self.subTest(profile=name):
                self.assertEqual(reg[name]["name"], name)
                self.assertEqual(reg[name]["plugin"], plugin)
                self.assertEqual(reg[name]["dir"], dir_)
                self.assertIs(reg[name]["default"], default)
                self.assertEqual(reg[name]["requires"], requires)
                self.assertIs(reg[name]["agents"], agents)
                self.assertTrue(reg[name]["summary"])

    def test_comment_keys_are_ignored_at_every_level(self):
        doc = registry_doc()
        doc["//"] = "top"
        doc["profiles"]["//"] = "mid"
        doc["profiles"]["core"]["//"] = "leaf"
        reg = profiles.load_registry(self.repo_with(doc))
        self.assertEqual(list(reg), ["core"])

    def test_order_is_file_order(self):
        doc = registry_doc(zeta=entry("z", "plugin-z"), alpha=entry("a", "plugin-a"))
        reg = profiles.load_registry(self.repo_with(doc))
        self.assertEqual(list(reg), ["core", "zeta", "alpha"])

    def rejects(self, doc=None, raw=None):
        with self.assertRaises(profiles.RegistryError):
            profiles.load_registry(self.repo_with(doc, raw=raw))

    def test_missing_file(self):
        with self.assertRaises(profiles.RegistryError):
            profiles.load_registry(self.tmp())

    def test_bad_json(self):
        self.rejects(raw="{not json")

    def test_no_core(self):
        self.rejects(registry_doc(core=None, other=entry("o", "plugin-o", default=True)))

    def test_core_not_default(self):
        self.rejects(registry_doc(core=entry("toolkit-core", "plugin", default=False)))

    def test_duplicate_plugin(self):
        self.rejects(registry_doc(other=entry("toolkit-core", "plugin-other")))

    def test_duplicate_dir(self):
        self.rejects(registry_doc(other=entry("other", "plugin")))

    def test_dir_must_be_plugin_or_plugin_dash_name(self):
        self.rejects(registry_doc(other=entry("other", "extra")))
        self.rejects(registry_doc(other=entry("other", "plugin-Extra")))
        self.rejects(registry_doc(other=entry("other", "plugin-")))

    def test_dir_plugin_dash_name_is_accepted(self):
        reg = profiles.load_registry(self.repo_with(
            registry_doc(extra=entry("extra", "plugin-extra"))))
        self.assertEqual(reg["extra"]["dir"], "plugin-extra")

    def test_core_dir_must_be_exactly_plugin(self):
        self.rejects(registry_doc(core=entry("toolkit-core", "plugin-core",
                                             default=True)))

    def test_dir_with_slash(self):
        self.rejects(registry_doc(other=entry("other", "a/b")))

    def test_requires_unknown_profile(self):
        self.rejects(registry_doc(other=entry("other", "plugin-other", requires=["ghost"])))

    def test_non_bool_agents(self):
        bad = entry("other", "plugin-other")
        bad["agents"] = "yes"
        self.rejects(registry_doc(other=bad))

    def test_bad_profile_name(self):
        self.rejects(registry_doc(Core=entry("other", "plugin-other")))

    def test_profile_name_with_trailing_newline(self):
        # `$` matches before a final newline, so `^[a-z]+$` with .match let
        # "x\n" through; the name check must be a full match.
        self.rejects(registry_doc(**{"x\n": entry("other", "plugin-other")}))

    def test_requires_not_a_list_of_names(self):
        for value in ("core", None, ["core", 3]):
            bad = entry("other", "plugin-other")
            bad["requires"] = value
            with self.subTest(requires=value):
                self.rejects(registry_doc(other=bad))

    def test_empty_summary(self):
        bad = entry("other", "plugin-other")
        bad["summary"] = ""
        self.rejects(registry_doc(other=bad))

    def test_absent_prerequisites_load_as_an_empty_list(self):
        reg = profiles.load_registry(self.repo_with(registry_doc()))
        self.assertEqual(reg["core"]["prerequisites"], [])

    def test_valid_prerequisites_load(self):
        good = entry("other", "plugin-other")
        good["prerequisites"] = [{"id": "a@b", "why": "because"}]
        reg = profiles.load_registry(self.repo_with(registry_doc(other=good)))
        self.assertEqual(reg["other"]["prerequisites"], [{"id": "a@b", "why": "because"}])

    def test_malformed_prerequisites_are_rejected(self):
        why = "because"
        for value in ("a@b", {"id": "a@b", "why": why}, ["a@b"],
                      [{"why": why}], [{"id": "a@b"}],
                      [{"id": "ab", "why": why}],
                      [{"id": "a@b@c", "why": why}],
                      [{"id": "@b", "why": why}],
                      [{"id": "a@", "why": why}],
                      [{"id": "", "why": why}],
                      [{"id": 3, "why": why}],
                      [{"id": "a@claude-code-toolkit", "why": why}],
                      [{"id": "a@b", "why": ""}],
                      [{"id": "a@b", "why": 3}]):
            bad = entry("other", "plugin-other")
            bad["prerequisites"] = value
            with self.subTest(prerequisites=value):
                with self.assertRaises(profiles.RegistryError) as caught:
                    profiles.load_registry(self.repo_with(registry_doc(other=bad)))
                self.assertIn("profile other", str(caught.exception))

    def test_no_shipped_profile_declares_a_prerequisite(self):
        # Every shipped skill works without an outside plugin; a prerequisite
        # here would make install.sh tell every user of that profile to
        # install one.
        reg = profiles.load_registry(REPO)
        self.assertEqual(set(reg), {"core", "continuity", "builder",
                                    "maintainer", "mac"})
        for name, entry in reg.items():
            self.assertEqual(entry["prerequisites"], [], name)


class TestPrerequisites(unittest.TestCase):

    def registry(self):
        reg = four_profile_registry()
        for name in reg:
            reg[name]["prerequisites"] = []
        reg["builder"]["prerequisites"] = [{"id": "x@m", "why": "builder why"}]
        reg["maintainer"]["prerequisites"] = [{"id": "y@m", "why": "maintainer why"},
                                              {"id": "x@m", "why": "maintainer x why"}]
        return reg

    def test_one_row_per_id_in_selection_order_with_every_profile_needing_it(self):
        rows = profiles.prerequisites(self.registry(), ["core", "builder", "maintainer"])
        self.assertEqual(rows, [("x@m", "builder why", ["builder", "maintainer"]),
                                ("y@m", "maintainer why", ["maintainer"])])

    def test_only_selected_profiles_count(self):
        self.assertEqual(profiles.prerequisites(self.registry(), ["core", "continuity"]), [])
        self.assertEqual(profiles.prerequisites(self.registry(), ["core", "builder"]),
                         [("x@m", "builder why", ["builder"])])


class TestPluginId(unittest.TestCase):

    def test_plugin_id(self):
        self.assertEqual(profiles.plugin_id({"plugin": "toolkit-core"}),
                         "toolkit-core@claude-code-toolkit")
        self.assertEqual(profiles.plugin_id({"plugin": "toolkit-builder"}),
                         "toolkit-builder@claude-code-toolkit")


class TestResolveSelection(unittest.TestCase):

    def setUp(self):
        self.reg = four_profile_registry()

    def resolve(self, config, config_error=None):
        return profiles.resolve_selection(self.reg, config, config_error)

    def test_absent_key_gives_defaults(self):
        self.assertEqual(self.resolve({}), (["core", "continuity"], None, True))

    def test_null_is_a_list_error_not_absent(self):
        names, error, defaulted = self.resolve({"profiles": None})
        self.assertIsNone(names)
        self.assertIn("must be a list", error)
        self.assertFalse(defaulted)

    def test_string_is_a_list_error(self):
        names, error, defaulted = self.resolve({"profiles": "builder"})
        self.assertIsNone(names)
        self.assertEqual(error, "profiles must be a list of profile names")

    def test_non_string_member_is_a_list_error(self):
        names, error, _ = self.resolve({"profiles": ["core", 3]})
        self.assertIsNone(names)
        self.assertEqual(error, "profiles must be a list of profile names")

    def test_unknown_names_the_name_and_lists_valid(self):
        names, error, defaulted = self.resolve({"profiles": ["nope"]})
        self.assertIsNone(names)
        self.assertIn("unknown profile(s): nope", error)
        self.assertIn("valid: core, continuity, builder, maintainer", error)
        self.assertFalse(defaulted)

    def test_empty_list_is_core_only_and_not_defaulted(self):
        self.assertEqual(self.resolve({"profiles": []}), (["core"], None, False))

    def test_core_only(self):
        self.assertEqual(self.resolve({"profiles": ["core"]}), (["core"], None, False))

    def test_requires_closure_in_registry_order(self):
        names, error, defaulted = self.resolve({"profiles": ["maintainer"]})
        self.assertEqual(names, ["core", "builder", "maintainer"])
        self.assertIsNone(error)
        self.assertFalse(defaulted)

    def test_defaults_are_closed_over_requires_too(self):
        # Merge Review: the absent-key path returned the defaults before the
        # requires closure, so a default profile needing a non-default helper
        # installed without it. Both paths must select the same dependencies.
        reg = OrderedDict()
        for name, plugin_entry in (
            ("core", entry("toolkit-core", "plugin", default=True)),
            ("extra", entry("extra", "plugin-extra", default=True, requires=["helper"])),
            ("helper", entry("helper", "plugin-helper")),
        ):
            plugin_entry["name"] = name
            reg[name] = plugin_entry
        self.assertEqual(profiles.resolve_selection(reg, {}, None),
                         (["core", "extra", "helper"], None, True))
        self.assertEqual(profiles.resolve_selection(reg, {"profiles": ["extra"]}, None),
                         (["core", "extra", "helper"], None, False))

    def test_explicit_list_drops_non_core_defaults(self):
        names, _, _ = self.resolve({"profiles": ["builder"]})
        self.assertEqual(names, ["core", "builder"])

    def test_config_error_wins_over_a_valid_config(self):
        names, error, defaulted = self.resolve({"profiles": ["core"]}, "x is not valid JSON")
        self.assertIsNone(names)
        self.assertEqual(error, "config file x is not valid JSON")
        self.assertFalse(defaulted)


class TestMissingDefaults(unittest.TestCase):

    def setUp(self):
        self.reg = four_profile_registry()

    def missing(self, config):
        names, _, defaulted = profiles.resolve_selection(self.reg, config, None)
        return profiles.missing_defaults(self.reg, names, defaulted)

    def test_a_list_without_continuity_misses_it(self):
        self.assertEqual(self.missing({"profiles": ["core", "builder"]}), ["continuity"])

    def test_a_list_keeping_continuity_misses_nothing(self):
        self.assertEqual(self.missing({"profiles": ["continuity", "builder"]}), [])

    def test_the_defaulted_selection_misses_nothing(self):
        self.assertEqual(self.missing({}), [])

    def test_core_is_never_reported(self):
        self.assertNotIn("core", profiles.missing_defaults(self.reg, ["builder"], False))

    def test_two_missing_defaults_come_back_in_registry_order(self):
        self.reg["builder"]["default"] = True
        self.assertEqual(self.missing({"profiles": ["core"]}), ["continuity", "builder"])


class TestSelected(TempCase):

    def probe(self, config_path):
        code = ("import sys; sys.path.insert(0, %r); import profiles; "
                "print(repr(profiles.selected(%r)))" % (LIB, REPO))
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config_path)
        out = subprocess.run([sys.executable, "-c", code], env=env,
                             stdin=subprocess.DEVNULL, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return eval(out.stdout)

    def test_truncated_config_is_an_error_never_defaults(self):
        names, error, defaulted = self.probe(self.config_file(None, raw='{"profiles": ["co'))
        self.assertIsNone(names)
        self.assertIn("not valid JSON", error)
        self.assertFalse(defaulted)

    def test_missing_config_defaults(self):
        names, error, defaulted = self.probe("/nonexistent/config.json")
        self.assertEqual((names, error, defaulted), (["core", "continuity"], None, True))

    def test_missing_toolkit_config_module(self):
        # A copy of profiles.py with no sibling toolkit_config.py.
        lib = self.tmp()
        shutil.copy(SCRIPT, lib)
        repo = self.tmp()
        os.makedirs(os.path.join(repo, "claude"))
        shutil.copy(os.path.join(REPO, profiles.REGISTRY), os.path.join(repo, "claude"))
        code = ("import sys; sys.path.insert(0, %r); import profiles; "
                "print(repr(profiles.selected(%r)))" % (lib, repo))
        # cwd=lib: `-c` puts the cwd on sys.path, which must not find the real sibling.
        out = subprocess.run([sys.executable, "-c", code], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True, cwd=lib)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(eval(out.stdout),
                         (None, "lib/toolkit_config.py unavailable", False))


class TestEnabledProfiles(unittest.TestCase):

    def setUp(self):
        self.reg = profiles.load_registry(REPO)

    def test_unreadable_settings(self):
        names, error = profiles.enabled_profiles(self.reg, None)
        self.assertIsNone(names)
        self.assertEqual(error, "cannot read ~/.claude/settings.json")

    def test_enabled_plugins_not_an_object(self):
        names, error = profiles.enabled_profiles(self.reg, {"enabledPlugins": []})
        self.assertIsNone(names)
        self.assertEqual(error, "enabledPlugins is not an object")

    def test_enabled_core_only(self):
        settings = {"enabledPlugins": {"toolkit-core@claude-code-toolkit": True, "other@x": True}}
        self.assertEqual(profiles.enabled_profiles(self.reg, settings), ({"core"}, None))

    def test_only_exact_true_counts(self):
        settings = {"enabledPlugins": {"toolkit-core@claude-code-toolkit": "true"}}
        self.assertEqual(profiles.enabled_profiles(self.reg, settings), (set(), None))

    def test_absent_enabled_plugins_is_empty(self):
        self.assertEqual(profiles.enabled_profiles(self.reg, {}), (set(), None))


class TestCli(TempCase):

    def test_selected_default(self):
        out = self.run_cli(["selected", REPO], "/nonexistent.json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(),
                         ["core", "continuity",
                          '# default: set "profiles" in your toolkit config to choose'])

    def test_selected_explicit_has_no_default_line(self):
        out = self.run_cli(["selected", REPO], self.config_file({"profiles": []}))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), ["core"])

    def test_selected_unknown_exits_1(self):
        out = self.run_cli(["selected", REPO], self.config_file({"profiles": ["nope"]}))
        self.assertEqual(out.returncode, 1)
        self.assertIn("unknown profile(s): nope", out.stderr)
        self.assertEqual(out.stdout, "")

    def test_components_real_repo(self):
        out = self.run_cli(["components", REPO], "/nonexistent.json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(),
                         ["ok\tplugin/skills", "ok\tplugin-continuity/skills"])

    def test_components_missing_agents_exits_1(self):
        doc = registry_doc()
        doc["profiles"]["core"]["agents"] = True
        repo = self.repo_with(doc)
        os.makedirs(os.path.join(repo, "plugin", "skills"))
        out = self.run_cli(["components", repo], "/nonexistent.json")
        self.assertEqual(out.returncode, 1)
        self.assertEqual(out.stdout.splitlines(),
                         ["ok\tplugin/skills", "missing\tplugin/agents"])

    def test_components_follows_symlinks(self):
        doc = registry_doc()
        doc["profiles"]["core"]["agents"] = True
        repo = self.repo_with(doc)
        os.makedirs(os.path.join(repo, "skills"))
        os.makedirs(os.path.join(repo, "agents"))
        os.makedirs(os.path.join(repo, "plugin"))
        os.symlink("../skills", os.path.join(repo, "plugin", "skills"))
        os.symlink("../agents", os.path.join(repo, "plugin", "agents"))
        out = self.run_cli(["components", repo], "/nonexistent.json")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_components_registry_error_exits_2(self):
        out = self.run_cli(["components", self.tmp()], "/nonexistent.json")
        self.assertEqual(out.returncode, 2)
        self.assertTrue(out.stderr.strip())

    def test_components_selection_error_exits_2(self):
        out = self.run_cli(["components", REPO], self.config_file({"profiles": ["nope"]}))
        self.assertEqual(out.returncode, 2)
        self.assertIn("unknown profile(s)", out.stderr)

    def test_bad_argv_exits_2_with_usage(self):
        for args in ([], ["bogus", REPO], ["selected"]):
            out = self.run_cli(args, "/nonexistent.json")
            self.assertEqual(out.returncode, 2, args)
            self.assertIn("usage", out.stderr.lower())


def disagreements(reg, market):
    """Both directions: marketplace plugins lacking a profile, and profiles
    lacking a marketplace entry; a source/dir mismatch counts either way."""
    problems = []
    # skill-<name> plugins are single-skill plugins, not profiles.
    market = [p for p in market if not p["name"].startswith("skill-")]
    by_name = {p["name"]: p for p in market}
    by_plugin = {e["plugin"]: e for e in reg.values()}
    for name, plugin in by_name.items():
        match = by_plugin.get(name)
        if match is None or "./" + match["dir"] != plugin["source"]:
            problems.append("marketplace plugin %s has no matching profile" % name)
    for plugin_name, match in by_plugin.items():
        plugin = by_name.get(plugin_name)
        if plugin is None or "./" + match["dir"] != plugin["source"]:
            problems.append("profile %s has no matching marketplace entry" % match["name"])
    return problems


class TestFeaturesAndTestedVersion(TempCase):

    def doc_with_features(self, features):
        doc = registry_doc()
        doc["profiles"]["core"]["features"] = features
        return doc

    def test_features_default_to_empty(self):
        reg = profiles.parse_registry(registry_doc(), "x")
        self.assertEqual(reg["core"]["features"], [])

    def test_features_are_kept(self):
        reg = profiles.parse_registry(self.doc_with_features(["a b"]), "x")
        self.assertEqual(reg["core"]["features"], ["a b"])

    def test_bad_features_are_refused(self):
        for bad in ("a", None, [1], [""], ["ok", ""], {"a": "b"}):
            with self.subTest(bad=bad):
                with self.assertRaises(profiles.RegistryError):
                    profiles.parse_registry(self.doc_with_features(bad), "x")

    def test_tested_version_validation(self):
        self.assertIsNone(profiles.tested_version({"profiles": {}}))
        self.assertEqual(profiles.tested_version({"tested_claude_code": "2.1.306"}), "2.1.306")
        for bad in ("2.1", "v2.1.306", "2.1.306-beta", "", 2, ["2.1.306"]):
            with self.subTest(bad=bad):
                with self.assertRaises(profiles.RegistryError):
                    profiles.tested_version({"tested_claude_code": bad})

    def test_registry_without_tested_version_still_parses(self):
        self.assertIn("core", profiles.parse_registry(registry_doc(), "x"))

    def test_required_features_lists_events_then_features(self):
        repo = self.tmp()
        os.makedirs(os.path.join(repo, "plugin-x", "hooks"))
        with open(os.path.join(repo, "plugin-x", "hooks", "hooks.json"), "w") as handle:
            handle.write('{"hooks": {"SessionStart": [], "PreToolUse": []}}')
        item = {"dir": "plugin-x", "features": ["Thing"]}
        self.assertEqual(profiles.required_features(repo, item),
                         ["SessionStart", "PreToolUse", "Thing"])

    def test_required_features_without_hooks_json_is_just_features(self):
        item = {"dir": "plugin-none", "features": ["Thing"]}
        self.assertEqual(profiles.required_features(self.tmp(), item), ["Thing"])
        self.assertEqual(profiles.required_features(self.tmp(), {"dir": "plugin-none"}), [])

    def test_real_registry_declares_the_version_the_readme_names(self):
        version = profiles.load_tested_version(REPO)
        self.assertIsNotNone(version)
        with open(os.path.join(REPO, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        self.assertIn("tested with %s" % version, readme)

    def test_real_build_profile_features_start_with_its_hook_events(self):
        reg = profiles.load_registry(REPO)
        entry_ = reg["builder"]
        with open(os.path.join(REPO, entry_["dir"], "hooks", "hooks.json"),
                  encoding="utf-8") as handle:
            events = list(json.load(handle, object_pairs_hook=OrderedDict)["hooks"])
        got = profiles.required_features(REPO, entry_)
        self.assertTrue(events)
        self.assertEqual(got[:len(events)], events)
        self.assertEqual(got[len(events):], entry_["features"])
        self.assertTrue(entry_["features"])


class TestMarketplaceAgreement(unittest.TestCase):

    def setUp(self):
        with open(os.path.join(REPO, ".claude-plugin", "marketplace.json"),
                  encoding="utf-8") as handle:
            self.market = json.load(handle)["plugins"]
        self.reg = profiles.load_registry(REPO)

    def test_shipped_registry_and_marketplace_agree_both_ways(self):
        self.assertEqual(disagreements(self.reg, self.market), [])

    def test_registry_only_profile_is_noticed(self):
        reg = OrderedDict(self.reg)
        extra = entry("toolkit-extra", "plugin-extra")
        extra["name"] = "extra"
        reg["extra"] = extra
        self.assertEqual(len(disagreements(reg, self.market)), 1)

    def test_marketplace_only_plugin_is_noticed(self):
        market = self.market + [{"name": "toolkit-extra", "source": "./extra"}]
        self.assertEqual(len(disagreements(self.reg, market)), 1)

    def test_skill_plugin_needs_no_profile(self):
        market = self.market + [{"name": "skill-demo", "source": "./plugins/demo"}]
        self.assertEqual(disagreements(self.reg, market), [])

    def test_source_mismatch_is_noticed(self):
        market = [dict(self.market[0], source="./elsewhere")] + self.market[1:]
        self.assertEqual(len(disagreements(self.reg, market)), 2)


class TestMarketplaceConstant(unittest.TestCase):

    def test_hook_manifest_marketplace_matches(self):
        self.assertEqual(hook_manifest.MARKETPLACE, profiles.MARKETPLACE)


if __name__ == "__main__":
    unittest.main()
