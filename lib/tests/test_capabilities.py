#!/usr/bin/env python3
"""docs/capabilities.json lists everything the toolkit offers; keep it true.

The silent failure this guards: a capability with no entry, or an entry that
points at nothing, reads as a complete registry and is not. Nothing else
complains, because the registry is only read by people. Each check below is a
function over the parsed registry so a positive control can run it on a copy
with one entry, command or hook removed and assert it reports the gap.
"""
import copy
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.join(REPO, "lib"))
from capabilities import (  # noqa: E402
    CACHE,
    load_registry,
    manifest_commands,
    hook_relpaths,
    check_schema,
    check_ids,
    check_paths,
    check_delivery,
    check_config,
    check_requires,
    check_coverage,
    check_unowned_suites,
    check_tool_entry_points,
    check_commands,
    check_hooks,
    check_private,
)


def _drop(registry, ident):
    copy_ = copy.deepcopy(registry)
    copy_["capabilities"] = [e for e in copy_["capabilities"] if e["id"] != ident]
    return copy_


def _first(registry, kind):
    return next(e for e in registry["capabilities"] if e["kind"] == kind)


class CapabilitiesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = load_registry()
        cls.commands = manifest_commands()
        cls.relpaths = hook_relpaths()

    # ---- one test per R7 failure clause ----

    def test_schema_is_exact(self):
        self.assertEqual(check_schema(self.registry), [])
        self.assertEqual(self.registry["schema"], 1)

    def test_id_repeats_or_prefix_disagrees_with_kind(self):
        self.assertEqual(check_ids(self.registry), [])

    def test_entry_point_or_tests_path_missing_or_without_tests(self):
        self.assertEqual(check_paths(self.registry), [])

    def test_delivery_names_no_real_profile_or_plugin_or_disagrees_with_location(self):
        self.assertEqual(check_delivery(self.registry), [])

    def test_config_key_not_in_config_example(self):
        self.assertEqual(check_config(self.registry), [])

    def test_requires_id_unknown(self):
        self.assertEqual(check_requires(self.registry), [])

    def test_skill_agent_or_tool_dir_without_entry(self):
        self.assertEqual(check_coverage(self.registry), [])
        self.assertEqual(check_tool_entry_points(self.registry), [])

    def test_suite_owned_by_no_capability(self):
        self.assertEqual(check_unowned_suites(self.registry), [])

    def test_manifest_command_unlisted_or_listed_one_missing(self):
        self.assertEqual(check_commands(self.registry, self.commands), [])

    def test_hook_registration_unlisted_or_listed_one_missing(self):
        self.assertEqual(check_hooks(self.registry, self.relpaths), [])

    def test_file_holds_no_absolute_path_or_private_plan_row(self):
        self.assertEqual(check_private(self.registry), [])

    # ---- positive controls: each check must report a deliberate defect ----

    def test_control_repeated_id(self):
        broken = copy.deepcopy(self.registry)
        broken["capabilities"].append(copy.deepcopy(broken["capabilities"][0]))
        self.assertTrue(check_ids(broken))

    def test_control_prefix_disagrees_with_kind(self):
        broken = copy.deepcopy(self.registry)
        _first(broken, "skill")["kind"] = "agent"
        self.assertTrue(check_ids(broken))

    def test_control_missing_entry_point(self):
        broken = copy.deepcopy(self.registry)
        _first(broken, "tool")["entry_points"].append("tools/no-such-file")
        self.assertTrue(check_paths(broken))

    def test_control_tests_path_without_a_test(self):
        broken = copy.deepcopy(self.registry)
        _first(broken, "tool")["tests"] = ["docs"]
        self.assertTrue(check_paths(broken))

    def test_control_delivery_unknown(self):
        broken = copy.deepcopy(self.registry)
        _first(broken, "tool")["delivery"] = "profile:nope"
        self.assertTrue(check_delivery(broken))

    def test_control_skill_delivery_disagrees_with_location(self):
        broken = copy.deepcopy(self.registry)
        skill = next(e for e in broken["capabilities"] if e["delivery"] == "profile:builder"
                     and e["kind"] == "skill")
        skill["delivery"] = "profile:core"
        self.assertTrue(check_delivery(broken))

    def test_control_hook_delivery_disagrees_with_registration(self):
        broken = copy.deepcopy(self.registry)
        hooked = next(e for e in broken["capabilities"] if e["hooks"])
        hooked["delivery"] = "checkout"
        self.assertTrue(check_delivery(broken))

    def test_control_hook_registered_in_a_profile_not_depending_on_its_home(self):
        # continuity does not require builder, so a builder hook registered there
        # names the wrong home.
        import hook_manifest
        rows = hook_manifest.plugin_registrations(REPO)
        hooked = next(e for e in self.registry["capabilities"]
                      if e["hooks"] and e["delivery"] == "profile:builder")
        rows.append({"relpath": hooked["hooks"][0], "profile": "continuity"})
        problems = check_delivery(self.registry, rows)
        self.assertTrue([p for p in problems if p.startswith(hooked["id"] + ":")], problems)

    def test_control_hook_missing_from_its_delivery_profile(self):
        # The heartbeat may register in every profile that depends on core, but
        # not ONLY there: core, its delivery, must register it too.
        import hook_manifest
        rows = [r for r in hook_manifest.plugin_registrations(REPO)
                if not (r["relpath"] == "tools/hook-heartbeat/claude-hook-heartbeat"
                        and r["profile"] == "core")]
        problems = check_delivery(self.registry, rows)
        self.assertTrue([p for p in problems if p.startswith("tool:hook-heartbeat:")], problems)

    def test_control_config_key_unknown(self):
        broken = copy.deepcopy(self.registry)
        _first(broken, "tool")["config"] = ["no_such_key"]
        self.assertTrue(check_config(broken))

    def test_control_requires_unknown(self):
        broken = copy.deepcopy(self.registry)
        _first(broken, "skill")["requires"] = ["tool:no-such-tool"]
        self.assertTrue(check_requires(broken))

    def test_control_removed_tool_entry(self):
        self.assertTrue(check_coverage(_drop(self.registry, "tool:leak-gate")))

    def test_control_removed_skill_entry(self):
        self.assertTrue(check_coverage(_drop(self.registry, "skill:build")))

    def test_control_removed_plugin_skill_entry(self):
        self.assertTrue(check_coverage(_drop(self.registry, "skill:humanizer")))

    def test_control_removed_agent_entry(self):
        self.assertTrue(check_coverage(_drop(self.registry, "agent:executor")))

    def test_control_unregistered_suite(self):
        # Row 59's shape: a suite file lands and no capability lists it.
        broken = copy.deepcopy(self.registry)
        owner = next(e for e in broken["capabilities"]
                     if "lib/tests/test_readiness.py" in e["tests"])
        owner["tests"].remove("lib/tests/test_readiness.py")
        self.assertEqual(check_unowned_suites(broken),
                         ["lib/tests/test_readiness.py is owned by no capability"])

    def test_control_tool_entry_point_outside_its_dir(self):
        broken = copy.deepcopy(self.registry)
        _first(broken, "tool")["entry_points"].append("install.sh")
        self.assertTrue(check_tool_entry_points(broken))

    def test_control_removed_command(self):
        broken = copy.deepcopy(self.registry)
        tool = next(e for e in broken["capabilities"] if e["commands"])
        tool["commands"].pop()
        self.assertTrue(check_commands(broken, self.commands))

    def test_control_invented_command(self):
        broken = copy.deepcopy(self.registry)
        _first(broken, "tool")["commands"].append("claude-no-such-command")
        self.assertTrue(check_commands(broken, self.commands))

    def test_control_removed_hook(self):
        broken = copy.deepcopy(self.registry)
        tool = next(e for e in broken["capabilities"] if e["hooks"])
        tool["hooks"].pop()
        self.assertTrue(check_hooks(broken, self.relpaths))

    def test_control_invented_hook(self):
        broken = copy.deepcopy(self.registry)
        _first(broken, "tool")["hooks"].append("tools/no-such-tool/no-such-hook")
        self.assertTrue(check_hooks(broken, self.relpaths))

    def test_control_private_text(self):
        for text in ("/Users/someone/x", "see ~/notes", "row 12a", "PENDING", "2026-01-02",
                     "in /tmp/x"):
            self.assertTrue(check_private([text]), text)

    def test_manifest_sandbox_is_outside_tmp(self):
        tmp = os.path.realpath(os.environ.get("TMPDIR", "/tmp"))
        self.assertFalse(os.path.realpath(CACHE).startswith(tmp + os.sep))
        self.assertNotEqual(os.path.realpath(CACHE).split(os.sep)[1], "tmp")


if __name__ == "__main__":
    unittest.main()
