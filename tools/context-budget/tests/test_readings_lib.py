#!/usr/bin/env python3
"""Tests for readings_lib -- the reading log, its rate and its forecast.

Every reading is hand-built, never the user's live log: that file grows every
session and asserting against it would pass or fail for reasons unrelated to
this code.
"""

import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

# Pin the config away from this machine's, as the sibling suites do.
os.environ["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent-readings-lib-test.json"

sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO, "tools", "memory-lint"))

import instruction_lib
import readings_lib


class FakeConfig(object):
    """Stands in for `toolkit_config`: the only method readings_lib calls."""

    def __init__(self, value=None, raises=False):
        self.value = value
        self.raises = raises

    def get_path(self, *keys, default=None):
        if self.raises:
            raise RuntimeError("boom")
        return self.value if self.value is not None else default


def _reading(date_str, root="/root", total=100, surfaces=None):
    return {
        "v": instruction_lib.UNIT_VERSION,
        "date": date_str,
        "ts": date_str + "T09:00:00",
        "workspace_root": root,
        "surfaces": surfaces if surfaces is not None else [],
        "always_on_total": total,
    }


class TestReadingsPath(unittest.TestCase):
    def test_none_config_returns_expanded_default(self):
        self.assertEqual(
            readings_lib.readings_path(None),
            os.path.expanduser(readings_lib.DEFAULT_READINGS_PATH),
        )

    def test_a_configured_path_is_expanded(self):
        config = FakeConfig(value="~/somewhere/readings.jsonl")
        self.assertEqual(
            readings_lib.readings_path(config),
            os.path.expanduser("~/somewhere/readings.jsonl"),
        )

    def test_a_raising_config_falls_back_to_the_default(self):
        config = FakeConfig(raises=True)
        self.assertEqual(
            readings_lib.readings_path(config),
            os.path.expanduser(readings_lib.DEFAULT_READINGS_PATH),
        )


class TestAppendAndHasReading(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="readings-lib-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        # Nested, so append_reading must create the parent directory.
        self.path = os.path.join(self.dir, "nested", "readings.jsonl")

    def test_append_then_has_reading_is_true(self):
        reading = _reading("2026-09-20", root="/root-a")
        self.assertTrue(readings_lib.append_reading(self.path, reading))
        self.assertTrue(
            readings_lib.has_reading(self.path, "2026-09-20", instruction_lib.UNIT_VERSION, "/root-a")
        )
        self.assertFalse(
            readings_lib.has_reading(self.path, "2026-09-21", instruction_lib.UNIT_VERSION, "/root-a")
        )

    def test_an_unloadable_line_for_today_does_not_suppress_the_write(self):
        """A line with today's date, version and root but no `surfaces` list
        is discarded by `load`. It used to suppress `--record` anyway, so the
        day vanished from the series with nothing said. The control: a line
        whose surface FIELD is malformed still loads, so it still suppresses.
        """
        v = instruction_lib.UNIT_VERSION
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"date": "2026-09-20", "v": v,
                                     "workspace_root": "/root-a"}) + "\n")
        self.assertFalse(readings_lib.has_reading(self.path, "2026-09-20", v, "/root-a"))
        self.assertTrue(readings_lib.append_reading(self.path, _reading("2026-09-20", root="/root-a")))
        self.assertTrue(readings_lib.has_reading(self.path, "2026-09-20", v, "/root-a"))
        self.assertEqual(len(readings_lib.load(self.path, "/root-a", v)), 1)

        odd = _reading("2026-09-22", root="/root-a",
                       surfaces=[{"key": "k", "instructions": "many"}])
        self.assertTrue(readings_lib.append_reading(self.path, odd))
        self.assertTrue(readings_lib.has_reading(self.path, "2026-09-22", v, "/root-a"))

    def test_repeated_date_last_wins_and_different_dates_both_load(self):
        first = _reading("2026-09-20", root="/root-a", total=100)
        second = _reading("2026-09-20", root="/root-a", total=150)  # same date
        third = _reading("2026-09-21", root="/root-a", total=160)

        self.assertTrue(readings_lib.append_reading(self.path, first))
        self.assertTrue(readings_lib.append_reading(self.path, second))
        loaded = readings_lib.load(self.path, "/root-a", instruction_lib.UNIT_VERSION)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["always_on_total"], 150)

        self.assertTrue(readings_lib.append_reading(self.path, third))
        loaded = readings_lib.load(self.path, "/root-a", instruction_lib.UNIT_VERSION)
        self.assertEqual(len(loaded), 2)
        self.assertEqual([r["date"] for r in loaded],
                          [datetime.date(2026, 9, 20), datetime.date(2026, 9, 21)])


class TestLoadValidation(unittest.TestCase):
    """Acceptance 2: malformed lines, interleaved between two valid readings."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="readings-lib-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.path = os.path.join(self.dir, "readings.jsonl")

    def _write_lines(self, lines):
        with open(self.path, "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(line + "\n")

    def test_malformed_lines_are_skipped_and_valid_ones_load_in_date_order(self):
        valid_a = json.dumps(_reading("2026-09-18", root="/root", total=100))
        valid_b = json.dumps(
            _reading(
                "2026-09-25",
                root="/root",
                total=200,
                surfaces=[
                    {"key": "x", "ok": True, "instructions": "unknown",
                     "value": 1, "cap": 2}
                ],
            )
        )
        lines = [
            valid_a,
            "not even json {",
            # A version that is never the live one, derived rather than a
            # literal: a literal 2 silently became the live version at v2.
            json.dumps({"v": instruction_lib.UNIT_VERSION + 1, "date": "2026-09-19",
                        "workspace_root": "/root", "surfaces": []}),
            json.dumps({"v": instruction_lib.UNIT_VERSION, "date": "2026-09-19",
                        "workspace_root": "/other-root", "surfaces": []}),
            json.dumps({"v": instruction_lib.UNIT_VERSION,
                        "workspace_root": "/root", "surfaces": []}),  # no date
            json.dumps({"v": instruction_lib.UNIT_VERSION, "date": [],
                        "workspace_root": "/root", "surfaces": []}),
            json.dumps({"v": instruction_lib.UNIT_VERSION, "date": "2026-09-20",
                        "workspace_root": "/root", "surfaces": 3}),
            valid_b,
        ]
        self._write_lines(lines)

        loaded = readings_lib.load(self.path, "/root", instruction_lib.UNIT_VERSION)
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["date"], datetime.date(2026, 9, 18))
        self.assertEqual(loaded[1]["date"], datetime.date(2026, 9, 25))

        surface = loaded[1]["surfaces"][0]
        self.assertIs(surface["ok"], False)
        self.assertIsNone(surface["instructions"])

    def test_missing_file_returns_empty_list(self):
        missing = os.path.join(self.dir, "does-not-exist.jsonl")
        self.assertEqual(
            readings_lib.load(missing, "/root", instruction_lib.UNIT_VERSION), []
        )


class TestSeriesAndRatePerWeek(unittest.TestCase):
    def test_six_day_gap_is_insufficient(self):
        today = datetime.date(2026, 9, 26)
        points = [
            (datetime.date(2026, 9, 20), 100),
            (datetime.date(2026, 9, 26), 130),
        ]
        rate, reason = readings_lib.rate_per_week(points, today)
        self.assertIsNone(rate)
        self.assertEqual(reason, "insufficient history (2 readings since 2026-09-20)")

    def test_seven_day_gap_is_exactly_70(self):
        today = datetime.date(2026, 9, 27)
        points = [
            (datetime.date(2026, 9, 20), 100),
            (datetime.date(2026, 9, 27), 170),
        ]
        rate, reason = readings_lib.rate_per_week(points, today)
        self.assertEqual(rate, 70.0)
        self.assertIsNone(reason)

    def test_a_point_older_than_28_days_is_excluded(self):
        today = datetime.date(2026, 9, 27)
        points = [
            (datetime.date(2026, 8, 1), 1),  # older than the 28-day window
            (datetime.date(2026, 9, 20), 100),
            (datetime.date(2026, 9, 27), 170),
        ]
        rate, reason = readings_lib.rate_per_week(points, today)
        self.assertEqual(rate, 70.0)

    def test_no_readings_at_all_is_the_no_readings_reason(self):
        rate, reason = readings_lib.rate_per_week([], datetime.date(2026, 9, 27))
        self.assertIsNone(rate)
        self.assertEqual(reason, "no readings")

    def test_series_filters_a_failed_observation_in_the_middle(self):
        readings = [
            {"date": datetime.date(2026, 9, 20),
             "surfaces": [{"key": "k", "ok": True, "instructions": 100}]},
            {"date": datetime.date(2026, 9, 23),
             "surfaces": [{"key": "k", "ok": False, "instructions": None}]},
            {"date": datetime.date(2026, 9, 27),
             "surfaces": [{"key": "k", "ok": True, "instructions": 170}]},
        ]
        points = readings_lib.series(readings, "k", "instructions")
        self.assertEqual(
            points,
            [(datetime.date(2026, 9, 20), 100), (datetime.date(2026, 9, 27), 170)],
        )
        rate, reason = readings_lib.rate_per_week(points, datetime.date(2026, 9, 27))
        self.assertEqual(rate, 70.0)
        self.assertIsNone(reason)

    def test_total_series_excludes_null_totals(self):
        readings = [
            {"date": datetime.date(2026, 9, 20), "always_on_total": 100},
            {"date": datetime.date(2026, 9, 21), "always_on_total": None},
            {"date": datetime.date(2026, 9, 22), "always_on_total": 110},
        ]
        self.assertEqual(
            readings_lib.total_series(readings),
            [(datetime.date(2026, 9, 20), 100), (datetime.date(2026, 9, 22), 110)],
        )


class TestForecast(unittest.TestCase):
    def test_live_forecast_is_one_day_not_three(self):
        """Design Review's probe: slope from the log, start from the live value."""
        self.assertEqual(readings_lib.forecast(70.0, 190, 200), 1.0)

    def test_over_cap(self):
        self.assertEqual(readings_lib.forecast(70.0, 201, 200), "over")

    def test_shrinking_is_not_growing(self):
        self.assertEqual(readings_lib.forecast(-3, 50, 200), "not growing")

    def test_no_rate_is_none(self):
        self.assertIsNone(readings_lib.forecast(None, 50, 200))


class TestLogStatus(unittest.TestCase):
    def test_no_readings_is_the_none_state(self):
        state, newest = readings_lib.log_status([], datetime.date(2026, 9, 26))
        self.assertEqual(state, "none")
        self.assertIsNone(newest)

    def test_a_newest_reading_over_3_days_old_is_stale(self):
        readings = [{"date": datetime.date(2026, 9, 20)}]
        state, newest = readings_lib.log_status(readings, datetime.date(2026, 9, 26))
        self.assertEqual(state, "stale")
        self.assertEqual(newest, datetime.date(2026, 9, 20))

    def test_a_newest_reading_within_3_days_is_fresh(self):
        readings = [{"date": datetime.date(2026, 9, 24)}]
        state, newest = readings_lib.log_status(readings, datetime.date(2026, 9, 26))
        self.assertEqual(state, "fresh")
        self.assertEqual(newest, datetime.date(2026, 9, 24))


class TestSurfaceStaleSince(unittest.TestCase):
    def test_a_surface_absent_from_recent_readings_returns_its_last_ok_date(self):
        readings = [
            {"date": datetime.date(2026, 9, 20),
             "surfaces": [{"key": "a", "ok": True}]},
            {"date": datetime.date(2026, 9, 26),
             "surfaces": [{"key": "b", "ok": True}]},
        ]
        self.assertEqual(
            readings_lib.surface_stale_since(readings, "a"),
            datetime.date(2026, 9, 20),
        )

    def test_a_surface_present_yesterday_is_none(self):
        readings = [
            {"date": datetime.date(2026, 9, 25),
             "surfaces": [{"key": "a", "ok": True}]},
            {"date": datetime.date(2026, 9, 26),
             "surfaces": [{"key": "b", "ok": True}]},
        ]
        self.assertIsNone(readings_lib.surface_stale_since(readings, "a"))


class TestBuildReading(unittest.TestCase):
    def test_shape_matches_design_4_3(self):
        now = datetime.datetime(2026, 9, 26, 9, 30, 15)
        records = [{"key": "CLAUDE.md", "instructions": 109}]
        reading = readings_lib.build_reading(records, 210, "/root", now)
        self.assertEqual(
            reading,
            {
                "v": instruction_lib.UNIT_VERSION,
                "date": "2026-09-26",
                "ts": "2026-09-26T09:30:15",
                "workspace_root": "/root",
                "surfaces": records,
                "always_on_total": 210,
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
