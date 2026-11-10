#!/usr/bin/env python3
"""Tests for hook_manifest across several plugins (one per profile).

Hermetic: the multi-profile cases build a temp repo; the baseline cases read
the real repo's plugins' hooks.json files directly, not through hook_manifest.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

LIB = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
REPO = os.path.dirname(LIB)
sys.path.insert(0, LIB)
import hook_manifest

HOOK_FIXED = '"${CLAUDE_PLUGIN_ROOT}"/tools/%s/%s'


def hooks_doc(events):
    """events: {event: [exe, ...]} -> a hooks.json document."""
    hooks = {}
    for event, exes in events.items():
        hooks[event] = [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": HOOK_FIXED % (x, x), "timeout": 5}]}
            for x in exes]
    return {"hooks": hooks}


def write_json(path, doc):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(doc, handle)


def registry(*pairs):
    profiles = {}
    for name, dir_ in pairs:
        profiles[name] = {"plugin": "toolkit-" + name, "dir": dir_,
                          "default": name == "core", "requires": [],
                          "agents": False, "summary": name + " summary"}
    return {"profiles": profiles}


def raw_profile_files():
    """[(profile, raw hooks dict)] for the five real plugins, in registry order."""
    out = []
    for profile, directory in (("core", "plugin"), ("continuity", "plugin-continuity"),
                               ("builder", "plugin-build"),
                               ("maintainer", "plugin-maintainer"), ("mac", "plugin-mac")):
        with open(os.path.join(REPO, directory, "hooks", "hooks.json"), encoding="utf-8") as h:
            out.append((profile, json.load(h)["hooks"]))
    return out


def concatenated(files):
    """Per-event concatenation of blocks across raw hooks dicts, in order."""
    merged = {}
    for raw in files:
        for event, blocks in raw.items():
            merged.setdefault(event, []).extend(blocks)
    return merged


class TestRealRepoBaseline(unittest.TestCase):
    """Behaviour on the real repo's five plugins; must hold before and after."""

    def setUp(self):
        self.files = raw_profile_files()
        self.raw = self.files[0][1]

    def test_load_is_the_concatenation_of_the_five_files(self):
        self.assertEqual(hook_manifest.load(REPO),
                         concatenated([raw for _, raw in self.files]))

    def test_registrations_cover_every_handler(self):
        rows = hook_manifest.registrations(hook_manifest.load(REPO))
        handlers = sum(len(b.get("hooks", [])) for _, raw in self.files
                       for blocks in raw.values() for b in blocks)
        self.assertEqual(len(rows), handlers)
        self.assertGreater(handlers, 5)

    def test_resolve_two_arg_is_plugin_dir(self):
        self.assertEqual(hook_manifest.resolve(REPO, "tools/x/x"),
                         os.path.join(REPO, "plugin", "tools/x/x"))


class TestRealRepoAggregate(unittest.TestCase):
    """New readers on the real repo: load_plugin reads one file, load all five."""

    def setUp(self):
        self.files = raw_profile_files()

    def test_load_plugin_returns_each_profiles_own_file(self):
        for profile, raw in self.files:
            self.assertEqual(hook_manifest.load_plugin(REPO, profile), raw, profile)

    def test_plugin_registrations_tag_the_file_each_row_came_from(self):
        expected = []
        for profile, raw in self.files:
            expected.extend(dict(r, profile=profile)
                            for r in hook_manifest.registrations(raw))
        self.assertEqual(hook_manifest.plugin_registrations(REPO), expected)


class TestMultiProfile(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        write_json(os.path.join(self.repo, "claude", "profiles.json"),
                   registry(("core", "plugin"), ("extra", "plugin-extra")))
        write_json(os.path.join(self.repo, "plugin", "hooks", "hooks.json"),
                   hooks_doc({"PreToolUse": ["a"], "SessionStart": ["s"]}))
        write_json(os.path.join(self.repo, "plugin-extra", "hooks", "hooks.json"),
                   hooks_doc({"PreToolUse": ["b"], "Stop": ["z"]}))

    def exes(self, manifest, event):
        return [h["command"].rsplit("/", 1)[1] for b in manifest[event] for h in b["hooks"]]

    def test_load_concatenates_in_registry_order(self):
        manifest = hook_manifest.load(self.repo)
        self.assertEqual(self.exes(manifest, "PreToolUse"), ["a", "b"])
        self.assertEqual(self.exes(manifest, "SessionStart"), ["s"])
        self.assertEqual(self.exes(manifest, "Stop"), ["z"])

    def test_load_plugin_returns_only_that_profile(self):
        manifest = hook_manifest.load_plugin(self.repo, "extra")
        self.assertEqual(sorted(manifest), ["PreToolUse", "Stop"])
        self.assertEqual(self.exes(manifest, "PreToolUse"), ["b"])

    def test_plugin_registrations_tag_each_row(self):
        rows = hook_manifest.plugin_registrations(self.repo)
        self.assertEqual({(r["exe"], r["profile"]) for r in rows},
                         {("a", "core"), ("s", "core"), ("b", "extra"), ("z", "extra")})

    def test_registrations_profile_param(self):
        rows = hook_manifest.registrations(hook_manifest.load_plugin(self.repo, "extra"),
                                           profile="extra")
        self.assertTrue(rows and all(r["profile"] == "extra" for r in rows))
        plain = hook_manifest.registrations(hook_manifest.load_plugin(self.repo, "extra"))
        self.assertTrue(all("profile" not in r for r in plain))

    def test_unknown_profile_raises(self):
        with self.assertRaises(hook_manifest.ManifestError):
            hook_manifest.load_plugin(self.repo, "nope")
        with self.assertRaises(hook_manifest.ManifestError):
            hook_manifest.resolve(self.repo, "tools/x/x", "nope")

    def test_unreadable_profile_file_raises(self):
        os.remove(os.path.join(self.repo, "plugin-extra", "hooks", "hooks.json"))
        with self.assertRaises(hook_manifest.ManifestError):
            hook_manifest.load_plugin(self.repo, "extra")
        with self.assertRaises(hook_manifest.ManifestError):
            hook_manifest.load(self.repo)

    def test_resolve_uses_each_profiles_own_tools_dir(self):
        exe = os.path.join(self.repo, "plugin-extra", "tools", "x", "x")
        os.makedirs(os.path.dirname(exe))
        with open(exe, "w") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(exe, 0o755)
        self.assertTrue(os.path.exists(hook_manifest.resolve(self.repo, "tools/x/x", "extra")))
        self.assertFalse(os.path.exists(hook_manifest.resolve(self.repo, "tools/x/x")))

    def test_resolve_extra_missing_tools_dir_does_not_exist(self):
        core_exe = os.path.join(self.repo, "plugin", "tools", "x", "x")
        os.makedirs(os.path.dirname(core_exe))
        with open(core_exe, "w") as handle:
            handle.write("#!/bin/sh\n")
        self.assertTrue(os.path.exists(hook_manifest.resolve(self.repo, "tools/x/x")))
        self.assertFalse(os.path.exists(hook_manifest.resolve(self.repo, "tools/x/x", "extra")))


class TestFallbackAndMalformed(unittest.TestCase):

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.repo, True)
        write_json(os.path.join(self.repo, "plugin", "hooks", "hooks.json"),
                   hooks_doc({"PreToolUse": ["a"]}))

    def test_no_registry_reads_core_alone(self):
        manifest = hook_manifest.load(self.repo)
        self.assertEqual(list(manifest), ["PreToolUse"])
        rows = hook_manifest.plugin_registrations(self.repo)
        self.assertEqual([(r["exe"], r["profile"]) for r in rows], [("a", "core")])
        self.assertEqual(hook_manifest.resolve(self.repo, "tools/a/a"),
                         os.path.join(self.repo, "plugin", "tools/a/a"))

    def test_registry_present_but_profiles_module_unavailable_raises(self):
        # A broken install (registry shipped, lib/profiles.py gone) must not
        # quietly read as core-only: that would drop every other plugin's
        # hooks from install.sh's and integrity's checks.
        path = os.path.join(self.repo, "claude", "profiles.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write("{}")
        original = hook_manifest._profiles
        hook_manifest._profiles = lambda: None
        self.addCleanup(setattr, hook_manifest, "_profiles", original)
        with self.assertRaises(hook_manifest.ManifestError):
            hook_manifest.load(self.repo)
        with self.assertRaises(hook_manifest.ManifestError):
            hook_manifest.plugin_registrations(self.repo)

    def test_profiles_module_unavailable_without_registry_is_core_only(self):
        original = hook_manifest._profiles
        hook_manifest._profiles = lambda: None
        self.addCleanup(setattr, hook_manifest, "_profiles", original)
        rows = hook_manifest.plugin_registrations(self.repo)
        self.assertEqual([(r["exe"], r["profile"]) for r in rows], [("a", "core")])

    def test_malformed_registry_raises_not_falls_back(self):
        path = os.path.join(self.repo, "claude", "profiles.json")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write("{not json")
        with self.assertRaises(hook_manifest.ManifestError):
            hook_manifest.load(self.repo)
        with self.assertRaises(hook_manifest.ManifestError):
            hook_manifest.plugin_registrations(self.repo)

    def test_invalid_registry_rules_raise(self):
        write_json(os.path.join(self.repo, "claude", "profiles.json"), {"profiles": {}})
        with self.assertRaises(hook_manifest.ManifestError):
            hook_manifest.load(self.repo)


if __name__ == "__main__":
    unittest.main()
