#!/usr/bin/env python3
"""Tests for lib/install_record.py, the record install.sh keeps of what it added.

Pinned here: `load` rejects anything malformed, the three writers merge and
write atomically, a corrupt record is moved aside, write failures propagate,
and the `finish` CLI records checkout, commit and profiles.
"""

import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB)
import install_record as ir  # noqa: E402

SCRIPT = os.path.join(LIB, "install_record.py")


def _read(path):
    with open(path, "rb") as handle:
        return handle.read()


class RecordTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.realpath(self._tmp.name)
        self.path = ir.record_path(self.home)

    def write_raw(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w") as handle:
            handle.write(text)


class TestLoad(RecordTestCase):
    def test_record_path(self):
        self.assertEqual(
            self.path,
            os.path.join(self.home, ".config", "claude-code-toolkit", "install-record.json"))

    def test_absent(self):
        self.assertIsNone(ir.load(self.path))

    def test_directory(self):
        os.makedirs(self.path)
        self.assertIsNone(ir.load(self.path))

    def test_bad_shapes_are_none(self):
        base = {"schema": 1, "permissions_added": {"allow": [], "ask": [], "deny": []}}
        cases = {
            "non-json": "{nope",
            "list": "[]",
            "schema 2": json.dumps(dict(base, schema=2)),
            "allow string": json.dumps(dict(base, permissions_added={"allow": "x"})),
            "previous int": json.dumps(dict(base, default_mode={"set": "a", "previous": 3})),
            "set missing": json.dumps(dict(base, default_mode={"previous": None})),
            "exclude bad": json.dumps(dict(base, claude_md_excludes_added={"pattern": "p"})),
            "created_key str": json.dumps(dict(
                base, claude_md_excludes_added={"pattern": "p", "created_key": "yes"})),
            "commit int": json.dumps(dict(base, commit=5)),
            "profiles str": json.dumps(dict(base, profiles="builder")),
            "profiles mixed": json.dumps(dict(base, profiles=["a", 1])),
            "permissions not dict": json.dumps(dict(base, permissions_added=[])),
            "permissions_added missing": json.dumps({"schema": 1}),
            "previous missing": json.dumps(dict(base, default_mode={"set": "a"})),
            "set int": json.dumps(dict(base, default_mode={"set": 1, "previous": None})),
            "default_mode not dict": json.dumps(dict(base, default_mode="auto")),
            "default_mode null": json.dumps(dict(base, default_mode=None)),
            "exclude null": json.dumps(dict(base, claude_md_excludes_added=None)),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.write_raw(text)
                self.assertIsNone(ir.load(self.path))

    def test_valid_shapes(self):
        self.write_raw(json.dumps({
            "schema": 1,
            "permissions_added": {"allow": ["a"], "ask": [], "deny": []},
            "default_mode": {"set": "auto", "previous": None},
            "claude_md_excludes_added": {"pattern": "p", "created_key": True},
            "checkout": "/x", "commit": None, "profiles": None, "installed_at": "t"}))
        self.assertIsInstance(ir.load(self.path), dict)

    def test_permissions_added_may_omit_kinds(self):
        for perms in ({}, {"allow": ["a"]}):
            with self.subTest(perms):
                self.write_raw(json.dumps({"schema": 1, "permissions_added": perms}))
                self.assertIsInstance(ir.load(self.path), dict)


class TestAddPermissions(RecordTestCase):
    def test_union_first_seen_order(self):
        ir.add_permissions(self.path, {"allow": ["a", "b"], "deny": ["d"]})
        ir.add_permissions(self.path, {"allow": ["b", "c"], "ask": ["k"]})
        record = ir.load(self.path)
        self.assertEqual(record["permissions_added"],
                         {"allow": ["a", "b", "c"], "ask": ["k"], "deny": ["d"]})

    def test_empty_added_removes_nothing(self):
        ir.add_permissions(self.path, {"allow": ["a"]})
        ir.add_permissions(self.path, {})
        ir.add_permissions(self.path, {"allow": []})
        self.assertEqual(ir.load(self.path)["permissions_added"]["allow"], ["a"])

    def test_previous_survives_second_call(self):
        ir.add_permissions(self.path, {}, mode_set="auto", mode_previous="default")
        ir.add_permissions(self.path, {}, mode_set="plan", mode_previous="auto")
        self.assertEqual(ir.load(self.path)["default_mode"],
                         {"set": "plan", "previous": "default"})

    def test_first_mode_none_previous(self):
        ir.add_permissions(self.path, {}, mode_set="auto")
        self.assertEqual(ir.load(self.path)["default_mode"],
                         {"set": "auto", "previous": None})

    def test_no_mode_leaves_it_absent(self):
        ir.add_permissions(self.path, {"allow": ["a"]})
        self.assertNotIn("default_mode", ir.load(self.path))


class TestAddExcludeAndFinish(RecordTestCase):
    def test_exclude_first_wins(self):
        ir.add_exclude(self.path, "one", True)
        ir.add_exclude(self.path, "two", False)
        self.assertEqual(ir.load(self.path)["claude_md_excludes_added"],
                         {"pattern": "one", "created_key": True})

    def test_finish_overwrites(self):
        ir.finish(self.path, "/a", "c1", ["x"], "t1")
        ir.finish(self.path, "/b", None, None, "t2")
        record = ir.load(self.path)
        self.assertEqual((record["checkout"], record["commit"], record["profiles"],
                          record["installed_at"]), ("/b", None, None, "t2"))

    def test_writers_compose(self):
        ir.add_permissions(self.path, {"ask": ["k"]})
        ir.add_exclude(self.path, "p", False)
        ir.finish(self.path, "/a", "c", [], "t")
        record = ir.load(self.path)
        self.assertEqual(record["permissions_added"]["ask"], ["k"])
        self.assertIn("claude_md_excludes_added", record)
        self.assertEqual(record["checkout"], "/a")


class TestFirstInstalledAt(RecordTestCase):
    def test_first_finish_sets_it(self):
        ir.finish(self.path, "/a", "c", [], "2026-10-01T00:00:00Z")
        self.assertEqual(ir.load(self.path)["first_installed_at"], "2026-10-01T00:00:00Z")

    def test_second_finish_keeps_it(self):
        ir.finish(self.path, "/a", "c", [], "2026-10-01T00:00:00Z")
        ir.finish(self.path, "/a", "c", [], "2026-10-13T00:00:00Z")
        record = ir.load(self.path)
        self.assertEqual(record["installed_at"], "2026-10-13T00:00:00Z")
        self.assertEqual(record["first_installed_at"], "2026-10-01T00:00:00Z")

    def test_third_finish_still_keeps_the_first(self):
        # Two runs cannot tell "kept" from "copied from the last installed_at":
        # both read the first stamp. A third run can.
        for stamp in ("2026-10-01T00:00:00Z", "2026-10-13T00:00:00Z", "2026-10-20T00:00:00Z"):
            ir.finish(self.path, "/a", "c", [], stamp)
        self.assertEqual(ir.load(self.path)["first_installed_at"], "2026-10-01T00:00:00Z")

    def test_non_string_is_malformed(self):
        ir.finish(self.path, "/a", "c", [], "t")
        record = json.loads(_read(self.path))
        record["first_installed_at"] = 5
        self.write_raw(json.dumps(record))
        self.assertIsNone(ir.load(self.path))

    def test_existing_record_without_key_gets_it(self):
        ir.add_permissions(self.path, {"ask": ["k"]})
        self.assertNotIn("first_installed_at", ir.load(self.path))
        ir.finish(self.path, "/a", "c", [], "2026-10-13T00:00:00Z")
        self.assertEqual(ir.load(self.path)["first_installed_at"], "2026-10-13T00:00:00Z")


    def test_legacy_record_keeps_its_last_install_as_the_first(self):
        record = {"schema": 1, "permissions_added": {"allow": [], "ask": [], "deny": []},
                  "installed_at": "2026-10-05T00:00:00Z"}
        self.write_raw(json.dumps(record))
        ir.finish(self.path, "/a", "c", [], "2026-10-13T00:00:00Z")
        self.assertEqual(ir.load(self.path)["first_installed_at"], "2026-10-05T00:00:00Z")


SHA = "a" * 40


def _state():
    return {
        "permissions_added": {"allow": ["Bash(ls)"], "ask": [], "deny": ["Bash(rm)"]},
        "default_mode": {"set": "auto", "previous": None},
        "claude_md_excludes_added": {"pattern": "*/x.md", "created_key": True},
        "settings": {
            "permissions": {"allow": ["Bash(ls)"], "ask": [], "deny": []},
            "defaultMode": {"present": True, "value": None},
            "claudeMdExcludes": {"present": False},
            "statusLine": {"present": True, "value": {"type": "command"}},
            "autoMemoryDirectory": {"present": False},
        },
    }


class TestRollbackState(RecordTestCase):
    def test_round_trip(self):
        ir.set_rollback(self.path, SHA, "t", _state())
        self.assertEqual(ir.rollback_state(ir.load(self.path)), _state())

    def test_none_without_state(self):
        ir.set_rollback(self.path, SHA, "t")
        self.assertIsNone(ir.rollback_state(ir.load(self.path)))
        self.assertIsNone(ir.rollback_state({"schema": 1}))

    def test_malformed_state_raises(self):
        record = {"rollback": {"commit": SHA, "recorded_at": "t", "pinned_at": None,
                               "state": {"settings": {}}}}
        with self.assertRaises(ValueError):
            ir.rollback_state(record)

    def test_write_refuses_malformed_shapes(self):
        def drop(key):
            return lambda st: st.pop(key)

        def extra_top(st):
            st["other"] = 1

        def extra_settings(st):
            st["settings"]["other"] = {"present": False}

        def present_no_value(st):
            st["settings"]["statusLine"] = {"present": True}

        def absent_with_value(st):
            st["settings"]["statusLine"] = {"present": False, "value": 1}

        def bad_bucket(st):
            st["settings"]["permissions"]["allow"] = "Bash(ls)"

        def missing_bucket(st):
            del st["settings"]["permissions"]["deny"]

        def missing_added_bucket(st):
            del st["permissions_added"]["ask"]

        mutators = [drop("settings"), drop("permissions_added"), extra_top, extra_settings,
                    present_no_value, absent_with_value, bad_bucket, missing_bucket,
                    missing_added_bucket]
        ir.set_rollback(self.path, SHA, "t", _state())
        before = _read(self.path)
        for mutate in mutators:
            state = _state()
            mutate(state)
            with self.assertRaises(ValueError):
                ir.set_rollback(self.path, SHA, "t2", state)
            with self.assertRaises(ValueError):
                ir.refresh_state(self.path, state)
            self.assertEqual(_read(self.path), before)


class TestRefreshAndRestoreOwned(RecordTestCase):
    def test_refresh_state_needs_an_entry(self):
        ir.finish(self.path, "/c", "abc", ["p"], "t")
        before = _read(self.path)
        with self.assertRaises(ValueError):
            ir.refresh_state(self.path, _state())
        self.assertEqual(_read(self.path), before)

    def test_refresh_state_replaces_only_state(self):
        ir.set_rollback(self.path, SHA, "t", _state())
        new = _state()
        new["settings"]["statusLine"] = {"present": False}
        ir.refresh_state(self.path, new)
        entry = ir.rollback(ir.load(self.path))
        self.assertEqual(entry["state"], new)
        self.assertEqual((entry["commit"], entry["recorded_at"]), (SHA, "t"))

    def test_restore_owned_leaves_profiles_and_commit(self):
        ir.finish(self.path, "/c", "abc", ["p"], "t0")
        ir.add_permissions(self.path, {"allow": ["Bash(new)"]}, "plan", "auto")
        ir.set_rollback(self.path, SHA, "t", _state())
        ir.restore_owned(self.path, _state(), "pin")
        record = ir.load(self.path)
        self.assertEqual(record["permissions_added"], _state()["permissions_added"])
        self.assertEqual(record["default_mode"], {"set": "auto", "previous": None})
        self.assertEqual(record["claude_md_excludes_added"], _state()["claude_md_excludes_added"])
        self.assertEqual(record["profiles"], ["p"])
        self.assertEqual(record["commit"], "abc")
        self.assertEqual(record["installed_at"], "t0")
        self.assertEqual(record["first_installed_at"], "t0")
        self.assertEqual(record["rollback"]["pinned_at"], "pin")
        self.assertEqual(record["rollback"]["state"], _state())

    def test_restore_owned_pops_absent_keys(self):
        ir.add_permissions(self.path, {}, "plan", "auto")
        ir.add_exclude(self.path, "*/y.md", False)
        ir.set_rollback(self.path, SHA, "t", _state())
        state = _state()
        del state["default_mode"]
        del state["claude_md_excludes_added"]
        ir.restore_owned(self.path, state, "pin")
        record = ir.load(self.path)
        self.assertNotIn("default_mode", record)
        self.assertNotIn("claude_md_excludes_added", record)

    def test_restore_owned_needs_an_entry(self):
        ir.finish(self.path, "/c", "abc", ["p"], "t")
        before = _read(self.path)
        with self.assertRaises(ValueError):
            ir.restore_owned(self.path, _state(), "pin")
        self.assertEqual(_read(self.path), before)


class TestCorrupt(RecordTestCase):
    def test_corrupt_moved_aside(self):
        self.write_raw("{not json")
        ir.add_permissions(self.path, {"allow": ["new"]})
        moved = glob.glob(self.path + ".corrupt-*")
        self.assertEqual(len(moved), 1)
        self.assertRegex(moved[0], r"\.corrupt-\d{8}T\d{6}Z$")
        self.assertEqual(_read(moved[0]), b"{not json")
        record = ir.load(self.path)
        self.assertEqual(record["permissions_added"], {"allow": ["new"], "ask": [], "deny": []})

    def test_wrong_schema_also_moved(self):
        self.write_raw(json.dumps({"schema": 2}))
        ir.add_exclude(self.path, "p", True)
        self.assertEqual(len(glob.glob(self.path + ".corrupt-*")), 1)
        self.assertIsNotNone(ir.load(self.path))


class TestWriteFailure(RecordTestCase):
    def setUp(self):
        super().setUp()
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")

    def test_each_writer_raises_and_file_untouched(self):
        ir.add_permissions(self.path, {"allow": ["a"]})
        before = _read(self.path)
        directory = os.path.dirname(self.path)
        os.chmod(directory, 0o500)
        self.addCleanup(os.chmod, directory, 0o700)
        writers = {
            "add_permissions": lambda: ir.add_permissions(self.path, {"allow": ["z"]}),
            "add_exclude": lambda: ir.add_exclude(self.path, "p", True),
            "finish": lambda: ir.finish(self.path, "/a", "c", [], "t"),
        }
        for name, call in writers.items():
            with self.subTest(name):
                with self.assertRaises(OSError):
                    call()
                self.assertEqual(_read(self.path), before)

    def test_failed_serialisation_leaves_no_temp_file(self):
        ir.add_permissions(self.path, {"allow": ["a"]})
        before = _read(self.path)
        directory = os.path.dirname(self.path)
        listing = sorted(os.listdir(directory))

        def failing_dump(*_args, **_kwargs):
            raise TypeError("injected serialisation failure")

        with mock.patch.object(ir.json, "dump", failing_dump):
            with self.assertRaises(TypeError):
                ir.add_permissions(self.path, {"allow": ["b"]})
        self.assertEqual(_read(self.path), before)
        self.assertEqual(sorted(os.listdir(directory)), listing)


class TestWritersRefuseRejectedShapes(RecordTestCase):
    """A writer never leaves a record load() or rollback() would reject: the
    next writer would move the record aside and lose what the installer owns."""

    SHA = "a" * 40

    def assert_refused_and_untouched(self, call):
        before = _read(self.path)
        with self.assertRaises(ValueError):
            call()
        self.assertEqual(_read(self.path), before)
        self.assertIsNotNone(ir.load(self.path))

    def setUp(self):
        super().setUp()
        ir.add_permissions(self.path, {"allow": ["kept"]})

    def test_non_string_permission_rule(self):
        self.assert_refused_and_untouched(
            lambda: ir.add_permissions(self.path, {"allow": [7]}))
        ir.add_permissions(self.path, {"allow": ["next"]})
        self.assertEqual(ir.load(self.path)["permissions_added"]["allow"], ["kept", "next"])

    def test_non_string_mode(self):
        self.assert_refused_and_untouched(
            lambda: ir.add_permissions(self.path, {}, mode_set=5))

    def test_non_bool_created_key(self):
        self.assert_refused_and_untouched(lambda: ir.add_exclude(self.path, "p", "yes"))

    def test_non_string_checkout_or_profiles(self):
        self.assert_refused_and_untouched(lambda: ir.finish(self.path, 5, "c", [], "t"))
        self.assert_refused_and_untouched(lambda: ir.finish(self.path, "/a", "c", [5], "t"))

    def test_bad_rollback_commit_or_stamp(self):
        self.assert_refused_and_untouched(
            lambda: ir.set_rollback(self.path, "not-a-sha", "2026-10-13T00:00:00Z"))
        self.assert_refused_and_untouched(lambda: ir.set_rollback(self.path, self.SHA, 7))

    def test_bad_restored_entry_or_pin(self):
        self.assert_refused_and_untouched(
            lambda: ir.restore_rollback(self.path, {"commit": "x"}))
        ir.set_rollback(self.path, self.SHA, "r")
        self.assert_refused_and_untouched(lambda: ir.mark_pinned(self.path, 5))

    def test_valid_rollback_still_written(self):
        ir.set_rollback(self.path, self.SHA, "r")
        self.assertEqual(ir.rollback(ir.load(self.path))["commit"], self.SHA)


class TestEnableWorkflowsAdded(RecordTestCase):
    def test_true_loads_and_other_values_do_not(self):
        base = {"schema": 1, "permissions_added": {"allow": [], "ask": [], "deny": []}}
        self.write_raw(json.dumps(dict(base, enable_workflows_added=True)))
        self.assertIs(ir.load(self.path)["enable_workflows_added"], True)
        for bad in (False, None, "yes"):
            self.write_raw(json.dumps(dict(base, enable_workflows_added=bad)))
            self.assertIsNone(ir.load(self.path), repr(bad))

    def test_set_workflows_owned_round_trips(self):
        ir.finish(self.path, "/c", "abc", ["p"], "t0")
        ir.add_exclude(self.path, "*/x.md", True)
        before = _read(self.path)
        ir.set_workflows_owned(self.path, True)
        record = ir.load(self.path)
        self.assertIs(record["enable_workflows_added"], True)
        del record["enable_workflows_added"]
        self.assertEqual(json.loads(before.decode("utf-8")), record)
        ir.set_workflows_owned(self.path, False)
        self.assertEqual(_read(self.path), before)

    def test_state_without_the_flag_passes(self):
        self.assertTrue(ir._state_ok(_state()))

    def test_state_with_true_passes_and_false_fails(self):
        state = _state()
        state["enable_workflows_added"] = True
        self.assertTrue(ir._state_ok(state))
        ir.set_rollback(self.path, SHA, "t", state)
        self.assertEqual(ir.rollback_state(ir.load(self.path)), state)
        state["enable_workflows_added"] = False
        self.assertFalse(ir._state_ok(state))
        with self.assertRaises(ValueError):
            ir.set_rollback(self.path, SHA, "t2", state)

    def test_state_base_is_validated_as_a_value_entry(self):
        for good in ({"present": False}, {"present": True, "value": 1},
                     {"present": True, "value": None}):
            state = _state()
            state["enable_workflows_base"] = good
            self.assertTrue(ir._state_ok(state), good)
        for bad in ({"present": True}, {"present": False, "value": 1}, None, "x"):
            state = _state()
            state["enable_workflows_base"] = bad
            self.assertFalse(ir._state_ok(state), bad)

    def test_restore_owned_copies_or_pops_the_flag(self):
        ir.finish(self.path, "/c", "abc", ["p"], "t0")
        ir.set_workflows_owned(self.path, True)
        ir.set_rollback(self.path, SHA, "t", _state())
        ir.restore_owned(self.path, _state(), "pin")
        self.assertNotIn("enable_workflows_added", ir.load(self.path))
        with_flag = _state()
        with_flag["enable_workflows_added"] = True
        with_flag["enable_workflows_base"] = {"present": False}
        ir.restore_owned(self.path, with_flag, "pin")
        record = ir.load(self.path)
        self.assertIs(record["enable_workflows_added"], True)
        self.assertNotIn("enable_workflows_base", record)


class TestRollback(RecordTestCase):
    SHA = "a" * 40
    OTHER = "b" * 40

    def entry(self, **over):
        value = {"commit": self.SHA, "recorded_at": "2026-10-13T00:00:00Z", "pinned_at": None}
        value.update(over)
        return value

    def test_rollback_none_for_bad_shapes(self):
        missing_recorded = self.entry()
        del missing_recorded["recorded_at"]
        missing_pinned = self.entry()
        del missing_pinned["pinned_at"]
        cases = {
            "no key": {},
            "non-dict": {"rollback": "x"},
            "short commit": {"rollback": self.entry(commit="abc")},
            "uppercase commit": {"rollback": self.entry(commit="A" * 40)},
            "39 hex": {"rollback": self.entry(commit="a" * 39)},
            "41 hex": {"rollback": self.entry(commit="a" * 41)},
            "trailing non-hex": {"rollback": self.entry(commit="a" * 40 + "z")},
            "recorded_at missing": {"rollback": missing_recorded},
            "pinned_at missing": {"rollback": missing_pinned},
            "pinned_at int": {"rollback": self.entry(pinned_at=5)},
        }
        for name, record in cases.items():
            with self.subTest(name):
                self.assertIsNone(ir.rollback(record))
        self.assertIsNone(ir.rollback(None))
        self.assertIsNone(ir.rollback([1]))

    def test_rollback_returns_valid_entry(self):
        for pinned in (None, "2026-10-13T01:00:00Z"):
            with self.subTest(pinned=pinned):
                value = self.entry(pinned_at=pinned)
                self.assertEqual(ir.rollback({"rollback": value}), value)

    def test_malformed_rollback_still_loads(self):
        self.write_raw(json.dumps({"schema": 1, "permissions_added": {"allow": ["a"]},
                                   "rollback": {"commit": "nope"}}))
        record = ir.load(self.path)
        self.assertIsNotNone(record)
        self.assertEqual(record["permissions_added"]["allow"], ["a"])
        self.assertIsNone(ir.rollback(record))

    def test_set_rollback_keeps_other_fields(self):
        ir.add_permissions(self.path, {"allow": ["a"]}, mode_set="auto", mode_previous=None)
        ir.finish(self.path, "/c", self.OTHER, [], "t")
        ir.set_rollback(self.path, self.SHA, "2026-10-13T00:00:00Z")
        record = ir.load(self.path)
        self.assertEqual(record["permissions_added"]["allow"], ["a"])
        self.assertEqual(record["commit"], self.OTHER)
        self.assertEqual(record["default_mode"]["set"], "auto")
        self.assertEqual(ir.rollback(record), self.entry())

    def test_mark_pinned_sets_and_keeps_commit(self):
        ir.set_rollback(self.path, self.SHA, "r")
        ir.mark_pinned(self.path, "2026-10-13T02:00:00Z")
        point = ir.rollback(ir.load(self.path))
        self.assertEqual(point["pinned_at"], "2026-10-13T02:00:00Z")
        self.assertEqual(point["commit"], self.SHA)

    def test_mark_pinned_without_entry_raises_and_writes_nothing(self):
        ir.add_permissions(self.path, {"allow": ["a"]})
        before = _read(self.path)
        with self.assertRaises(ValueError):
            ir.mark_pinned(self.path, "t")
        self.assertEqual(_read(self.path), before)

    def test_clear_pinned(self):
        ir.set_rollback(self.path, self.SHA, "r")
        ir.mark_pinned(self.path, "t")
        ir.clear_pinned(self.path)
        self.assertIsNone(ir.rollback(ir.load(self.path))["pinned_at"])

    def test_clear_pinned_without_entry_is_a_no_op(self):
        ir.add_permissions(self.path, {"allow": ["a"]})
        before = _read(self.path)
        ir.clear_pinned(self.path)
        self.assertEqual(_read(self.path), before)

    def test_set_rollback_after_mark_pinned_resets_pin(self):
        ir.set_rollback(self.path, self.SHA, "r")
        ir.mark_pinned(self.path, "t")
        ir.set_rollback(self.path, self.OTHER, "r2")
        point = ir.rollback(ir.load(self.path))
        self.assertIsNone(point["pinned_at"])
        self.assertEqual(point["commit"], self.OTHER)

    def test_restore_none_removes_the_entry(self):
        ir.add_permissions(self.path, {"allow": ["a"]})
        ir.set_rollback(self.path, self.SHA, "r")
        ir.restore_rollback(self.path, None)
        record = ir.load(self.path)
        self.assertNotIn("rollback", record)
        self.assertEqual(record["permissions_added"]["allow"], ["a"])

    def test_restore_entry_puts_it_back_as_it_was(self):
        captured = self.entry(pinned_at="2026-10-13T01:00:00Z")
        ir.set_rollback(self.path, self.OTHER, "r2")
        ir.restore_rollback(self.path, captured)
        self.assertEqual(ir.rollback(ir.load(self.path)), captured)

    def test_ensure_writable_leaves_record_equal(self):
        ir.set_rollback(self.path, self.SHA, "r")
        ir.mark_pinned(self.path, "t")
        before = ir.load(self.path)
        ir.ensure_writable(self.path)
        after = ir.load(self.path)
        self.assertEqual(after, before)
        self.assertEqual(ir.rollback(after)["pinned_at"], "t")

    def test_ensure_writable_raises_when_directory_is_read_only(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        ir.set_rollback(self.path, self.SHA, "r")
        directory = os.path.dirname(self.path)
        os.chmod(directory, 0o500)
        self.addCleanup(os.chmod, directory, 0o700)
        with self.assertRaises(OSError):
            ir.ensure_writable(self.path)


class TestRemove(RecordTestCase):
    def test_absent_ok(self):
        ir.remove(self.path)

    def test_removes(self):
        ir.finish(self.path, "/a", "c", [], "t")
        ir.remove(self.path)
        self.assertFalse(os.path.exists(self.path))


class TestCli(RecordTestCase):
    def run_cli(self, *args):
        env = dict(os.environ, HOME=self.home)
        return subprocess.run([sys.executable, SCRIPT] + list(args), env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def make_repo(self):
        repo = os.path.join(self.home, "repo")
        os.makedirs(repo)
        git = ["git", "-C", repo]
        subprocess.run(git + ["init", "-q"], check=True)
        subprocess.run(git + ["-c", "user.email=t@t", "-c", "user.name=t",
                              "commit", "-q", "--allow-empty", "-m", "x"], check=True)
        return repo

    def test_finish_records_checkout_and_commit(self):
        repo = self.make_repo()
        result = self.run_cli("finish", repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = ir.load(self.path)
        self.assertIsNotNone(record)
        self.assertEqual(record["checkout"], os.path.realpath(repo))
        self.assertRegex(record["commit"], r"^[0-9a-f]{40}$")
        # no registry in this fixture, so profile selection fails -> None
        self.assertIsNone(record["profiles"])
        self.assertRegex(record["installed_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_finish_not_a_git_repo_commit_none(self):
        plain = os.path.join(self.home, "plain")
        os.makedirs(plain)
        result = self.run_cli("finish", plain)
        self.assertEqual(result.returncode, 0, result.stderr)
        record = ir.load(self.path)
        self.assertIsNotNone(record)
        self.assertIsNone(record["commit"])

    def test_bad_argv_exit_2(self):
        for args in (["bogus"], [], ["finish"], ["finish", "a", "b"]):
            with self.subTest(args):
                result = self.run_cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("usage", result.stderr.lower())
                self.assertNotIn("Traceback", result.stderr)

    def test_finish_oserror_exits_1(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        repo = self.make_repo()
        os.makedirs(os.path.dirname(self.path))
        os.chmod(os.path.dirname(self.path), 0o500)
        self.addCleanup(os.chmod, os.path.dirname(self.path), 0o700)
        result = self.run_cli("finish", repo)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
