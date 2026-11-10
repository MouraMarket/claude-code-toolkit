#!/usr/bin/env python3
"""Tests for lib/settings_io.py: the `set-key` and `set-mode` Action kinds.

`apply_settings_actions` is pure, so each test hands it a settings dict and
reads back the dict it returns.
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
import settings_io  # noqa: E402
from settings_io import Action, apply_settings_actions  # noqa: E402


def set_key(key, expected_present, expected, value):
    return Action("settings", "set-key", key,
                  {"expected_present": expected_present, "expected": expected, "value": value})


def set_mode(expected, present, value):
    return Action("settings", "set-mode", "defaultMode",
                  {"expected": expected, "present": present, "value": value})


class SetKeyTests(unittest.TestCase):
    def test_sets_when_expected_absent_and_absent(self):
        after = apply_settings_actions({"a": 1}, [set_key("k", False, None, {"x": 1})])
        self.assertEqual(after, {"a": 1, "k": {"x": 1}})

    def test_sets_when_value_matches(self):
        after = apply_settings_actions({"k": "old"}, [set_key("k", True, "old", "new")])
        self.assertEqual(after, {"k": "new"})

    def test_sets_over_a_present_null(self):
        after = apply_settings_actions({"k": None}, [set_key("k", True, None, 5)])
        self.assertEqual(after, {"k": 5})

    def test_no_change_when_expected_absent_but_present(self):
        before = {"k": "there"}
        self.assertEqual(apply_settings_actions(before, [set_key("k", False, None, "new")]), before)

    def test_no_change_when_expected_present_but_absent(self):
        self.assertEqual(apply_settings_actions({}, [set_key("k", True, "old", "new")]), {})

    def test_no_change_when_value_differs(self):
        before = {"k": "other"}
        self.assertEqual(apply_settings_actions(before, [set_key("k", True, "old", "new")]), before)

    def test_input_untouched_and_value_copied(self):
        value = {"x": [1]}
        before = {"a": 1}
        snapshot = copy.deepcopy(before)
        after = apply_settings_actions(before, [set_key("k", False, None, value)])
        self.assertEqual(before, snapshot)
        after["k"]["x"].append(2)
        self.assertEqual(value, {"x": [1]})


class SetModeTests(unittest.TestCase):
    def test_sets_a_present_null(self):
        after = apply_settings_actions({"permissions": {"defaultMode": "auto"}},
                                       [set_mode("auto", True, None)])
        self.assertIn("defaultMode", after["permissions"])
        self.assertIsNone(after["permissions"]["defaultMode"])

    def test_sets_a_value(self):
        after = apply_settings_actions({"permissions": {"defaultMode": "auto", "allow": ["x"]}},
                                       [set_mode("auto", True, "plan")])
        self.assertEqual(after, {"permissions": {"defaultMode": "plan", "allow": ["x"]}})

    def test_pops_and_drops_an_emptied_permissions(self):
        after = apply_settings_actions({"permissions": {"defaultMode": "auto"}, "k": 1},
                                       [set_mode("auto", False, None)])
        self.assertEqual(after, {"k": 1})

    def test_pops_and_keeps_other_permissions(self):
        after = apply_settings_actions({"permissions": {"defaultMode": "auto", "allow": ["x"]}},
                                       [set_mode("auto", False, None)])
        self.assertEqual(after, {"permissions": {"allow": ["x"]}})

    def test_no_change_when_mode_differs(self):
        before = {"permissions": {"defaultMode": "plan"}}
        self.assertEqual(apply_settings_actions(before, [set_mode("auto", False, None)]), before)
        self.assertEqual(apply_settings_actions(before, [set_mode("auto", True, "x")]), before)

    def test_no_change_when_mode_absent_or_permissions_not_a_dict(self):
        for before in ({}, {"permissions": {}}, {"permissions": "x"}):
            self.assertEqual(apply_settings_actions(before, [set_mode(None, True, "x")]), before)

    def test_input_untouched(self):
        before = {"permissions": {"defaultMode": "auto"}}
        snapshot = copy.deepcopy(before)
        apply_settings_actions(before, [set_mode("auto", False, None)])
        self.assertEqual(before, snapshot)


class TypeStrictKeyActionTests(unittest.TestCase):
    """A value re-read as `1` is not the `True` the install wrote."""

    def test_remove_key_leaves_a_one_when_true_was_expected(self):
        action = Action("settings", "remove-key", "k", {"expected": True})
        self.assertEqual(apply_settings_actions({"k": 1}, [action]), {"k": 1})

    def test_remove_key_removes_a_true_when_true_was_expected(self):
        action = Action("settings", "remove-key", "k", {"expected": True})
        self.assertEqual(apply_settings_actions({"k": True}, [action]), {})

    def test_set_key_leaves_a_one_when_true_was_expected(self):
        self.assertEqual(apply_settings_actions({"k": 1}, [set_key("k", True, True, False)]), {"k": 1})

    def test_set_key_replaces_a_true_when_true_was_expected(self):
        self.assertEqual(apply_settings_actions({"k": True}, [set_key("k", True, True, False)]),
                         {"k": False})


class WriteSettingsTests(unittest.TestCase):
    def test_writes_atomically_and_keeps_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w") as handle:
                json.dump({"a": 1}, handle)
            os.chmod(path, 0o600)
            settings_io.write_settings(path, [set_key("k", False, None, 2)])
            with open(path) as handle:
                self.assertEqual(json.load(handle), {"a": 1, "k": 2})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual(os.listdir(tmp), ["settings.json"])

    def test_a_change_of_json_type_alone_is_written(self):
        # True == 1 == 1.0 in Python: a rollback restoring a numeric base over
        # the toolkit's `true` must still reach the disk, with its own type.
        for base in (1, 1.0):
            with self.subTest(base=base), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "settings.json")
                with open(path, "w") as handle:
                    json.dump({"enableWorkflows": True}, handle)
                settings_io.write_settings(path, [set_key("enableWorkflows", True, True, base)])
                with open(path) as handle:
                    value = json.load(handle)["enableWorkflows"]
                self.assertIs(type(value), type(base))
                self.assertEqual(value, base)

    def test_no_change_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            with open(path, "w") as handle:
                handle.write('{"k": true}')
            settings_io.write_settings(path, [set_key("k", True, False, True)])
            with open(path) as handle:
                self.assertEqual(handle.read(), '{"k": true}')


class WriteJsonAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "out.json")

    def _with_umask(self, mask):
        old = os.umask(mask)
        self.addCleanup(os.umask, old)

    def test_existing_mode_kept(self):
        with open(self.path, "w") as handle:
            handle.write("{}")
        os.chmod(self.path, 0o640)
        settings_io.write_json_atomic(self.path, {"a": 1})
        self.assertEqual(os.stat(self.path).st_mode & 0o7777, 0o640)

    def test_new_file_mode_follows_umask(self):
        for mask in (0o022, 0o027):
            old = os.umask(mask)
            try:
                path = os.path.join(self.tmp, "new%o.json" % mask)
                settings_io.write_json_atomic(path, {"a": 1})
            finally:
                os.umask(old)
            self.assertEqual(os.stat(path).st_mode & 0o7777, 0o666 & ~mask)

    def test_umask_restored(self):
        old = os.umask(0o027)
        try:
            settings_io.write_json_atomic(self.path, {})
            self.assertEqual(os.umask(old), 0o027)
        finally:
            os.umask(old)

    def test_symlink_stays_a_symlink(self):
        real = os.path.join(self.tmp, "real.json")
        with open(real, "w") as handle:
            handle.write("{}")
        link = os.path.join(self.tmp, "link.json")
        os.symlink(real, link)
        settings_io.write_json_atomic(link, {"a": 1})
        self.assertTrue(os.path.islink(link))
        with open(real) as handle:
            self.assertEqual(json.load(handle), {"a": 1})

    def test_ensure_ascii_true_bytes(self):
        data = {"name": "caf\u00e9"}
        settings_io.write_json_atomic(self.path, data, ensure_ascii=True)
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), (json.dumps(data, indent=2) + "\n").encode("utf-8"))

    def test_default_keeps_non_ascii_raw(self):
        settings_io.write_json_atomic(self.path, {"name": "caf\u00e9"})
        with open(self.path, "rb") as handle:
            self.assertIn("caf\u00e9".encode("utf-8"), handle.read())

    def test_failed_dump_leaves_target_and_no_temp(self):
        with open(self.path, "w") as handle:
            handle.write('{"keep": true}')
        with mock.patch("settings_io.json.dump", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                settings_io.write_json_atomic(self.path, {"a": 1})
        with open(self.path) as handle:
            self.assertEqual(handle.read(), '{"keep": true}')
        self.assertEqual(os.listdir(self.tmp), ["out.json"])


class CreateAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "snap.json")

    def test_creates_with_bytes_and_source_mtime(self):
        source = os.path.join(self.tmp, "source.json")
        with open(source, "w") as handle:
            handle.write("x")
        os.utime(source, (1000000000, 1000000000))
        self.assertTrue(settings_io.create_atomic(self.path, b"abc", stat_from=source))
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b"abc")
        self.assertEqual(int(os.stat(self.path).st_mtime), 1000000000)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["snap.json", "source.json"])

    def test_existing_file_untouched(self):
        with open(self.path, "wb") as handle:
            handle.write(b"old")
        self.assertFalse(settings_io.create_atomic(self.path, b"new"))
        with open(self.path, "rb") as handle:
            self.assertEqual(handle.read(), b"old")
        self.assertEqual(os.listdir(self.tmp), ["snap.json"])

    def test_failure_after_temp_write_leaves_nothing(self):
        with mock.patch("settings_io.os.link", side_effect=OSError("nope")):
            with self.assertRaises(OSError):
                settings_io.create_atomic(self.path, b"abc")
        self.assertEqual(os.listdir(self.tmp), [])


if __name__ == "__main__":
    unittest.main()
