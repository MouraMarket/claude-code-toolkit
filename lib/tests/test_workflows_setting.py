#!/usr/bin/env python3
"""Tests for workflows_setting.py, the pure planner behind `enableWorkflows`.

One test per row of the rule table, plus the strict `is True` comparisons.
Hermetic: no I/O.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import workflows_setting as ws

ABSENT = ws.ABSENT


class PlanTableTestCase(unittest.TestCase):
    """One test per row of the table."""

    def test_row1_wanted_not_owned_absent_sets(self):
        self.assertEqual(ws.plan(ABSENT, True, False), "set")

    def test_row2_wanted_not_owned_present_is_foreign(self):
        for value in (True, False, 1, "true", None, "x"):
            self.assertEqual(ws.plan(value, True, False), "foreign", value)

    def test_row3_wanted_owned_true_is_ok(self):
        self.assertEqual(ws.plan(True, True, True), "ok")

    def test_row4_wanted_owned_anything_else_is_edited(self):
        for value in (ABSENT, False, 1, "true", None, 0):
            self.assertEqual(ws.plan(value, True, True), "edited", value)

    def test_row5_not_wanted_owned_true_removes(self):
        self.assertEqual(ws.plan(True, False, True), "remove")

    def test_row6_not_wanted_owned_anything_else_is_edited(self):
        for value in (ABSENT, False, 1, "true", None):
            self.assertEqual(ws.plan(value, False, True), "edited", value)

    def test_row7_not_wanted_not_owned_is_ok(self):
        for value in (ABSENT, True, False, 1, "x", None):
            self.assertEqual(ws.plan(value, False, False), "ok", value)

    def test_plan_is_total(self):
        for current in (ABSENT, True, False, 1, 0, "", None, [], {}, 1.0):
            for wanted in (True, False):
                for owned in (True, False):
                    self.assertIn(ws.plan(current, wanted, owned),
                                  ("set", "foreign", "ok", "edited", "remove"))


class WantedTestCase(unittest.TestCase):
    def test_true_with_builder_is_wanted(self):
        self.assertIs(ws.wanted(True, ["core", "builder"]), True)

    def test_string_true_is_not_wanted(self):
        self.assertFalse(ws.wanted("true", ["builder"]))

    def test_one_is_not_wanted(self):
        self.assertFalse(ws.wanted(1, ["builder"]))

    def test_none_is_not_wanted(self):
        self.assertFalse(ws.wanted(None, ["builder"]))

    def test_false_is_not_wanted(self):
        self.assertFalse(ws.wanted(False, ["builder"]))

    def test_absent_builder_is_not_wanted(self):
        self.assertFalse(ws.wanted(True, ["core", "continuity"]))
        self.assertFalse(ws.wanted(True, []))
        self.assertFalse(ws.wanted(True, None))


class OwnedTestCase(unittest.TestCase):
    def test_owned_only_for_true(self):
        self.assertTrue(ws.owned({ws.RECORD_KEY: True}))
        self.assertFalse(ws.owned({ws.RECORD_KEY: 1}))
        self.assertFalse(ws.owned({ws.RECORD_KEY: "true"}))
        self.assertFalse(ws.owned({}))
        self.assertFalse(ws.owned(None))
        self.assertFalse(ws.owned([]))


class OwnedAndTrueTestCase(unittest.TestCase):
    def test_owned_and_true(self):
        self.assertTrue(ws.owned_and_true(
            {ws.RECORD_KEY: True}, {ws.KEY: True}))

    def test_record_without_key_is_not_owned(self):
        self.assertFalse(ws.owned_and_true({}, {ws.KEY: True}))

    def test_current_one_is_not_true(self):
        self.assertFalse(ws.owned_and_true(
            {ws.RECORD_KEY: True}, {ws.KEY: 1}))

    def test_false_absent_and_non_dict_settings(self):
        record = {ws.RECORD_KEY: True}
        self.assertFalse(ws.owned_and_true(record, {ws.KEY: False}))
        self.assertFalse(ws.owned_and_true(record, {}))
        self.assertFalse(ws.owned_and_true(record, None))


if __name__ == "__main__":
    unittest.main()
