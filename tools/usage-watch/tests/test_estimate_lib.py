"""Scanning, anchors, ratio and the single figure. Hermetic: temp roots only."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import estimate_lib as est  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(HERE))), "lib"))
import hook_manifest  # noqa: E402

NOW = 1787000000.0  # a fixed, arbitrary epoch -- never time.time()


def iso(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def write_transcript(root, project, name, entries):
    """entries = [(epoch, output_tokens)] -> one JSONL file shaped like a real one."""
    folder = os.path.join(root, project)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, name + ".jsonl")
    with open(path, "w") as handle:
        for epoch, out in entries:
            handle.write(json.dumps({
                "timestamp": iso(epoch),
                "message": {"usage": {"output_tokens": out,
                                      "input_tokens": 3,
                                      "cache_read_input_tokens": 900000}},
            }) + "\n")
    os.utime(path, (entries[-1][0], entries[-1][0]) if entries else None)
    return path


class TestScanUsage(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_it_collects_records_in_time_order(self):
        write_transcript(self.root, "projA", "s1",
                         [(NOW - 300, 100), (NOW - 100, 250)])
        write_transcript(self.root, "projB", "s2", [(NOW - 200, 50)])
        records, truncated = est.scan_usage(self.root, NOW - 3600, NOW)
        self.assertFalse(truncated)
        self.assertEqual([r[1] for r in records], [100, 50, 250])

    def test_records_older_than_since_are_dropped(self):
        write_transcript(self.root, "projA", "s1", [(NOW - 7200, 999)])
        write_transcript(self.root, "projA", "s2", [(NOW - 60, 7)])
        records, _ = est.scan_usage(self.root, NOW - 3600, NOW)
        self.assertEqual([r[1] for r in records], [7])

    def test_a_line_without_usage_is_skipped_not_fatal(self):
        folder = os.path.join(self.root, "projA")
        os.makedirs(folder)
        with open(os.path.join(folder, "s1.jsonl"), "w") as handle:
            handle.write('{"timestamp": "%s", "type": "user"}\n' % iso(NOW - 50))
            handle.write("not json at all\n")
            handle.write(json.dumps({"timestamp": iso(NOW - 40),
                                     "message": {"usage": {"output_tokens": 12}}}) + "\n")
        records, _ = est.scan_usage(self.root, NOW - 3600, NOW)
        self.assertEqual([r[1] for r in records], [12])

    def test_a_missing_root_is_empty_not_an_exception(self):
        records, truncated = est.scan_usage(
            os.path.join(self.root, "nope"), NOW - 3600, NOW)
        self.assertEqual(records, [])
        self.assertFalse(truncated)

    def test_the_file_budget_truncates_and_says_so(self):
        for i in range(5):
            write_transcript(self.root, "projA", "s%d" % i, [(NOW - 100 - i, 10)])
        records, truncated = est.scan_usage(self.root, NOW - 3600, NOW,
                                            file_budget=2)
        self.assertTrue(truncated)
        self.assertEqual(len(records), 2)


def write_raw(root, relative, rows):
    """One JSONL file from explicit (epoch, usage-dict, id) rows; id=None omits the id."""
    path = os.path.join(root, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        for epoch, usage, message_id in rows:
            message = {"usage": usage}
            if message_id is not None:
                message["id"] = message_id
            handle.write(json.dumps({"timestamp": iso(epoch),
                                     "message": message}) + "\n")
    os.utime(path, (rows[-1][0], rows[-1][0]) if rows else None)
    return path


def out(n):
    return {"output_tokens": n, "input_tokens": 3, "cache_read_input_tokens": 900000}


class TestScanDeduplicatesByMessageId(unittest.TestCase):
    """One API response is many JSONL lines; a per-field max counts it once."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _tokens(self):
        records, truncated = est.scan_usage(self.root, NOW - 3600, NOW)
        self.assertFalse(truncated)
        return [r[1] for r in records]

    def test_repeated_complete_usage_counts_once(self):
        # Every content block carries the same full usage.
        write_raw(self.root, "projA/s1.jsonl",
                  [(NOW - 300, out(500), "msg_1"),
                   (NOW - 299, out(500), "msg_1"),
                   (NOW - 298, out(500), "msg_1")])
        self.assertEqual(self._tokens(), [500])

    def test_streamed_partial_keeps_the_final_value_not_the_first(self):
        # Keeping the FIRST value badly understates output on agent files.
        write_raw(self.root, "projA/s1.jsonl",
                  [(NOW - 300, out(3), "msg_1"),
                   (NOW - 299, out(3), "msg_1"),
                   (NOW - 298, out(4210), "msg_1")])
        self.assertEqual(self._tokens(), [4210])

    def test_a_trailing_zero_entry_does_not_erase_a_real_message(self):
        # Keeping the LAST value would zero a real message.
        write_raw(self.root, "projA/s1.jsonl",
                  [(NOW - 300, out(773), "msg_1"),
                   (NOW - 299, out(0), "msg_1")])
        self.assertEqual(self._tokens(), [773])

    def test_distinct_ids_stay_distinct(self):
        write_raw(self.root, "projA/s1.jsonl",
                  [(NOW - 300, out(100), "msg_1"),
                   (NOW - 200, out(250), "msg_2")])
        self.assertEqual(sorted(self._tokens()), [100, 250])

    def test_entries_with_no_id_are_counted_separately(self):
        # Folding id-less entries into one key would collapse a transcript.
        write_raw(self.root, "projA/s1.jsonl",
                  [(NOW - 300, out(100), None),
                   (NOW - 200, out(250), None),
                   (NOW - 100, out(7), None)])
        self.assertEqual(sorted(self._tokens()), [7, 100, 250])

    def test_the_same_id_in_two_files_is_not_merged(self):
        # Over-counting is the safe direction for a floor.
        write_raw(self.root, "projA/s1.jsonl", [(NOW - 300, out(100), "msg_1")])
        write_raw(self.root, "projB/s2.jsonl", [(NOW - 200, out(100), "msg_1")])
        self.assertEqual(sorted(self._tokens()), [100, 100])

    def test_a_group_carries_its_earliest_timestamp(self):
        write_raw(self.root, "projA/s1.jsonl",
                  [(NOW - 300, out(3), "msg_1"),
                   (NOW - 120, out(900), "msg_1")])
        records, _ = est.scan_usage(self.root, NOW - 3600, NOW)
        self.assertEqual(records, [(NOW - 300, 900)])


class TestScanReachesSubagentTranscripts(unittest.TestCase):
    """Task-tool (subagents/) and Workflow (subagents/workflows/wf_*/) agents count."""

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _tokens(self):
        records, truncated = est.scan_usage(self.root, NOW - 3600, NOW)
        self.assertFalse(truncated)
        return sorted(r[1] for r in records)

    def test_a_task_tool_subagent_transcript_is_counted(self):
        write_raw(self.root, "projA/s1.jsonl", [(NOW - 300, out(100), "m1")])
        write_raw(self.root, "projA/s1/subagents/agent-abc.jsonl",
                  [(NOW - 250, out(4000), "m2")])
        self.assertEqual(self._tokens(), [100, 4000])

    def test_a_workflow_agent_transcript_is_counted(self):
        write_raw(self.root, "projA/s1.jsonl", [(NOW - 300, out(100), "m1")])
        write_raw(self.root, "projA/s1/subagents/workflows/wf_x1y2/agent-def.jsonl",
                  [(NOW - 250, out(7000), "m3")])
        self.assertEqual(self._tokens(), [100, 7000])

    def test_a_deeper_unforeseen_nesting_is_still_counted(self):
        write_raw(self.root, "projA/s1/subagents/workflows/wf_x/nested/agent-g.jsonl",
                  [(NOW - 250, out(55), "m4")])
        self.assertEqual(self._tokens(), [55])

    def test_the_sibling_meta_json_does_not_consume_the_file_budget(self):
        """Globbing `agent-*` would read metadata files; the file budget shows it."""
        for i in range(2):
            write_raw(self.root, "projA/s1/subagents/agent-%d.jsonl" % i,
                      [(NOW - 250 - i, out(4000), "m%d" % i)])
            meta = os.path.join(self.root,
                                "projA/s1/subagents/agent-%d.meta.json" % i)
            with open(meta, "w") as handle:
                json.dump({"agentType": "executor"}, handle)
            os.utime(meta, (NOW - 250 - i, NOW - 250 - i))
        # A budget of 3 admits the two transcripts, not all four files.
        records, truncated = est.scan_usage(self.root, NOW - 3600, NOW,
                                           file_budget=3)
        self.assertFalse(truncated, "metadata is being counted as a transcript")
        self.assertEqual(sorted(r[1] for r in records), [4000, 4000])

    def test_two_patterns_matching_one_file_read_it_once(self):
        """A file reached by two patterns is charged to the file budget once."""
        write_raw(self.root, "projA/s1.jsonl", [(NOW - 300, out(100), "m1")])
        original = est.TRANSCRIPT_GLOBS
        est.TRANSCRIPT_GLOBS = (("*", "*.jsonl"), ("*", "s1.jsonl"))
        try:
            records, truncated = est.scan_usage(self.root, NOW - 3600, NOW,
                                                file_budget=1)
            self.assertFalse(truncated, "one file is charged to the budget twice")
            self.assertEqual([r[1] for r in records], [100])
        finally:
            est.TRANSCRIPT_GLOBS = original

    def test_agent_files_count_against_the_file_budget(self):
        for i in range(4):
            write_raw(self.root, "projA/s1/subagents/agent-%d.jsonl" % i,
                      [(NOW - 100 - i, out(10), "m%d" % i)])
        records, truncated = est.scan_usage(self.root, NOW - 3600, NOW,
                                           file_budget=2)
        self.assertTrue(truncated)
        self.assertEqual(len(records), 2)

class TestFutureDatedRecords(unittest.TestCase):
    """`now` is an enforced upper bound on the scan."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects")
        os.makedirs(self.root)

    def test_a_record_from_the_future_is_not_counted(self):
        write_transcript(self.root, "projA", "s1", [
            (NOW - 1200, 5000),
            (NOW + 86400, 900000),   # tomorrow, on a skewed clock
        ])
        records, truncated = est.scan_usage(self.root, NOW - est.FIVE_HOURS, NOW)
        self.assertFalse(truncated)
        self.assertEqual([out for _stamp, out in records], [5000])

class TestScanTimeBudget(unittest.TestCase):
    """The scan budget and the hook timeout move together."""

    def test_the_budget_leaves_the_hook_room_to_finish(self):
        self.assertLess(est.SCAN_TIME_BUDGET, est.HOOK_TIMEOUT_SECONDS / 2.0,
                        "the scan may not claim half the hook's timeout")

    def test_the_declared_timeout_matches_the_plugin_manifest(self):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
        declared = set()
        for row in hook_manifest.registrations(hook_manifest.load(repo)):
            if row["exe"] == "claude-usage-watch":
                declared.add(float(row["timeout"] or 0))
        self.assertTrue(declared, "no claude-usage-watch entry found")
        self.assertEqual(declared, {est.HOOK_TIMEOUT_SECONDS},
                         "HOOK_TIMEOUT_SECONDS drifted from the plugin manifest")

    def test_the_dispatch_hook_declares_the_same_timeout(self):
        """The dispatch hook declares the same timeout."""
        tmp = tempfile.mkdtemp()
        root = os.path.join(tmp, "projects")
        os.makedirs(root)
        entries = [(NOW - 1200 + i, 10) for i in range(est.SCAN_TIME_CHECK_EVERY * 2)]
        write_transcript(root, "projA", "big", entries)

        class ScriptedClock(object):
            """Under budget at the between-files check, then far past it."""

            def __init__(self):
                self.readings = [0.0, 0.01]

            def time(self):
                if self.readings:
                    return self.readings.pop(0)
                return 99.0

        real_time = est.time
        est.time = ScriptedClock()
        try:
            records, truncated = est.scan_usage(root, NOW - est.FIVE_HOURS, NOW,
                                                time_budget=1.0)
        finally:
            est.time = real_time

        self.assertTrue(truncated, "the scan read a whole file past its deadline")
        self.assertLess(len(records), len(entries))
        self.assertGreater(len(records), 0, "it stopped inside the file, not before it")

class TestDefaultRatioIsConfigurable(unittest.TestCase):
    """`usage_watch.default_ratio` is the only configured ratio; nothing ships."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _resolve(self, value):
        """`(default_ratio(), derive_ratio({}))` from a CHILD process carrying a
        config: toolkit_config caches its load at import, so in-process proves
        nothing."""
        blob = {"usage_watch": {}}
        if value is not None:
            blob["usage_watch"]["default_ratio"] = value
        path = os.path.join(self.tmp, "config-%s.json" % (value,))
        with open(path, "w") as handle:
            json.dump(blob, handle)
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=path)
        code = ("import sys; sys.path.insert(0, %r); import estimate_lib as e; "
                "print(e.default_ratio()); print(*e.derive_ratio({}))"
                % os.path.dirname(HERE))
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        default, derived = result.stdout.strip().split("\n")
        return (None if default == "None" else float(default)), derived

    def test_a_configured_ratio_is_used_and_labelled_configured(self):
        self.assertEqual(self._resolve(9000), (9000.0, "9000.0 configured"))

    def test_an_absent_key_is_no_ratio(self):
        self.assertEqual(self._resolve(None), (None, "None none"))

    def test_a_zero_or_negative_ratio_is_no_ratio_rather_than_a_divisor(self):
        self.assertIsNone(self._resolve(0)[0])
        self.assertIsNone(self._resolve(-5)[0])

    def test_a_non_numeric_or_boolean_ratio_is_no_ratio(self):
        self.assertIsNone(self._resolve("lots")[0])
        self.assertIsNone(self._resolve(True)[0])  # isinstance(True, int)


class TestStoreRoundTrip(unittest.TestCase):

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "anchors.json")

    def test_saving_then_loading_returns_the_same_store(self):
        store = est.record_anchor({}, 45, NOW, NOW + 3600, None)
        self.assertTrue(est.save_store(store, path=self.path))
        self.assertEqual(est.load_store(path=self.path), store)

    def test_an_absent_store_loads_empty(self):
        self.assertEqual(est.load_store(path=self.path), {})

    def test_a_malformed_store_loads_empty_rather_than_raising(self):
        with open(self.path, "w") as handle:
            handle.write("{ broken")
        self.assertEqual(est.load_store(path=self.path), {})


RESET = NOW + 3 * 3600


class TestRecordAnchor(unittest.TestCase):

    def test_a_first_reading_sets_first_and_anchor_to_one_entry(self):
        store = est.record_anchor({}, 40, NOW, RESET, None)
        self.assertEqual(store["anchor"], {"pct": 40.0, "at": NOW, "resets_at": RESET})
        self.assertEqual(store["first"], store["anchor"])
        self.assertNotIn("ratio", store)

    def test_a_second_reading_six_points_up_measures_the_ratio(self):
        store = est.record_anchor({}, 30, NOW, RESET, None)
        store = est.record_anchor(store, 36, NOW + 600, RESET, 60000)
        self.assertEqual(store["ratio"], {"value": 10000.0, "source": "measured",
                                          "derived_at": NOW + 600})
        self.assertEqual(store["first"]["pct"], 30.0)

    def test_exactly_the_minimum_delta_measures(self):
        store = est.record_anchor({}, 30, NOW, RESET, None)
        store = est.record_anchor(store, 35, NOW + 600, RESET, 50000)
        self.assertEqual(store["ratio"]["value"], 10000.0)

    def test_four_points_up_stores_no_new_ratio(self):
        store = est.record_anchor({}, 30, NOW, RESET, None)
        store = est.record_anchor(store, 34, NOW + 600, RESET, 40000)
        self.assertNotIn("ratio", store)

    def test_a_different_window_rebases_first(self):
        store = est.record_anchor({}, 30, NOW, RESET, None)
        later = RESET + est.SAME_WINDOW_TOLERANCE + 1
        store = est.record_anchor(store, 50, NOW + 600, later, 99999)
        self.assertEqual(store["first"]["resets_at"], later)
        self.assertNotIn("ratio", store)

    def test_a_lower_same_window_reading_rebases_first(self):
        store = est.record_anchor({}, 50, NOW, RESET, None)
        store = est.record_anchor(store, 20, NOW + 600, RESET, 99999)
        self.assertEqual(store["first"]["pct"], 20.0)
        self.assertNotIn("ratio", store)

    def test_an_old_shape_store_loses_its_keys_but_keeps_a_measured_ratio(self):
        old = {"anchors": [{"pct": 1}], "window_start": 5.0,
               "ratio": {"value": 9000.0, "source": "measured", "derived_at": 1.0}}
        store = est.record_anchor(old, 40, NOW, RESET, None)
        self.assertNotIn("anchors", store)
        self.assertNotIn("window_start", store)
        self.assertEqual(store["ratio"], old["ratio"])

    def test_an_invalid_reading_changes_nothing(self):
        store = est.record_anchor({}, 40, NOW, RESET, None)
        self.assertEqual(est.record_anchor(store, 101, NOW, RESET, None), store)
        self.assertEqual(est.record_anchor(store, True, NOW, RESET, None), store)


def store_at(pct, resets_at=RESET, at=NOW - 600, ratio=None):
    store = est.record_anchor({}, pct, at, resets_at, None)
    if ratio:
        store["ratio"] = {"value": ratio, "source": "measured", "derived_at": at}
    return store


class TestLiveAnchorAndEstimate(unittest.TestCase):

    def test_an_anchor_is_void_once_its_window_resets(self):
        store = store_at(40)
        self.assertIsNotNone(est.live_anchor(store, RESET - 1))
        self.assertIsNone(est.live_anchor(store, RESET))
        self.assertIsNone(est.estimate(RESET, store, [(RESET - 5, 99)]))

    def test_the_estimate_adds_tokens_over_the_ratio(self):
        store = store_at(40, ratio=10000.0)
        got = est.estimate(NOW, store, [(NOW - 600 + 1, 30000)])
        self.assertEqual((got.pct, got.tokens, got.ratio_source), (43, 30000, "measured"))

    def test_tokens_at_or_before_the_anchor_are_not_counted(self):
        store = store_at(40, ratio=10000.0)
        got = est.estimate(NOW, store, [(NOW - 600, 50000), (NOW - 700, 50000)])
        self.assertEqual((got.pct, got.tokens), (40, 0))

    def test_the_estimate_caps_at_100(self):
        store = store_at(90, ratio=1000.0)
        self.assertEqual(est.estimate(NOW, store, [(NOW - 10, 999999)]).pct, 100)

    def test_no_ratio_means_tokens_but_no_percentage(self):
        with mock.patch.object(est, "default_ratio", return_value=None):
            got = est.estimate(NOW, store_at(40), [(NOW - 10, 777)])
        self.assertEqual((got.pct, got.tokens), (None, 777))

    def test_malformed_inputs_never_raise(self):
        for records in (None, [None], [("x", 1)]):
            self.assertIsNone(est.estimate(NOW, store_at(40, ratio=1e4), records))
        self.assertEqual(est.record_anchor(None, 40, NOW, RESET, None)["anchor"]["pct"], 40.0)

class TestDeriveRatioOrder(unittest.TestCase):

    def test_measured_beats_configured_beats_none(self):
        measured = store_at(40, ratio=9000.0)
        with mock.patch.object(est, "default_ratio", return_value=5000.0):
            self.assertEqual(est.derive_ratio(measured), (9000.0, "measured"))
            self.assertEqual(est.derive_ratio({}), (5000.0, "configured"))
        with mock.patch.object(est, "default_ratio", return_value=None):
            self.assertEqual(est.derive_ratio({}), (None, "none"))


class TestWriteAnchor(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.path = os.path.join(tmp, "anchors.json")
        self.root = os.path.join(tmp, "projects")
        os.makedirs(self.root)
        patcher = mock.patch.object(est, "is_estimate_enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, pct, resets_at=RESET, age=0.0):
        return est.write_anchor(NOW, pct, resets_at, age,
                                root=self.root, path=self.path)

    def test_it_refuses_bad_input_and_writes_nothing(self):
        for args in [(101,), (True,), (50, NOW - 1), (50, NOW + est.FIVE_HOURS + 61),
                     (50, RESET, 121.0), (50, RESET, -1.0)]:
            ok, reason = self.write(*args)
            self.assertFalse(ok, args)
            self.assertTrue(reason)
        self.assertFalse(os.path.exists(self.path))

    def test_it_refuses_when_the_estimator_is_off(self):
        with mock.patch.object(est, "is_estimate_enabled", return_value=False):
            ok, reason = self.write(50)
        self.assertEqual((ok, reason), (False, "the estimator is off (usage_watch.estimate)"))

    def test_a_save_failure_is_reported(self):
        with mock.patch.object(est, "save_store", return_value=False):
            self.assertEqual(self.write(50), (False, "could not write %s" % self.path))

    def test_the_boundary_values_are_accepted(self):
        self.assertEqual(self.write(50, NOW + est.FIVE_HOURS + 60, 120.0),
                         (True, "anchored"))

    def test_an_unchanged_reading_does_not_scan_or_write(self):
        self.assertEqual(self.write(40), (True, "anchored"))
        with mock.patch.object(est, "scan_usage", return_value=([], False)) as scan, \
                mock.patch.object(est, "save_store") as save:
            self.assertEqual(self.write(40, RESET + 60), (True, "unchanged"))
        self.assertEqual((scan.call_count, save.call_count), (0, 0))

    def test_a_reading_six_points_up_scans_once_and_measures(self):
        self.write(40)
        write_transcript(self.root, "p", "s", [(NOW + 100, 60000)])
        with mock.patch.object(est, "scan_usage", wraps=est.scan_usage) as scan:
            self.assertEqual(est.write_anchor(NOW + 600, 46, RESET, 0.0,
                                              root=self.root, path=self.path),
                             (True, "anchored"))
        self.assertEqual(scan.call_count, 1)
        self.assertEqual(est.load_store(self.path)["ratio"]["value"], 10000.0)

    def test_a_truncated_scan_writes_the_anchor_but_no_ratio(self):
        self.write(40)
        with mock.patch.object(est, "scan_usage", return_value=([(NOW, 5)], True)):
            ok, _ = est.write_anchor(NOW + 600, 50, RESET, 0.0,
                                     root=self.root, path=self.path)
        store = est.load_store(self.path)
        self.assertTrue(ok)
        self.assertEqual(store["anchor"]["pct"], 50.0)
        self.assertNotIn("ratio", store)


class TestCurrentEstimate(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.mkdtemp()
        self.path = os.path.join(tmp, "anchors.json")
        self.root = os.path.join(tmp, "projects")
        os.makedirs(self.root)
        est.save_store(store_at(40, ratio=10000.0), self.path)
        write_transcript(self.root, "p", "s", [(NOW - 100, 20000)])

    def get(self, now=NOW, root=None):
        return est.current_estimate(now, root=root or self.root, path=self.path)

    def test_a_live_anchor_gives_an_estimate(self):
        with mock.patch.object(est, "is_estimate_enabled", return_value=True):
            self.assertEqual(self.get().pct, 42)

    def test_it_is_none_with_the_estimator_off(self):
        with mock.patch.object(est, "is_estimate_enabled", return_value=False):
            self.assertIsNone(self.get())

    def test_it_is_none_without_a_live_anchor(self):
        with mock.patch.object(est, "is_estimate_enabled", return_value=True):
            self.assertIsNone(self.get(now=RESET + 1))

    def test_it_is_none_with_a_missing_transcript_root(self):
        with mock.patch.object(est, "is_estimate_enabled", return_value=True):
            self.assertIsNone(self.get(root=os.path.join(self.root, "nope")))


class TestSentences(unittest.TestCase):

    def make(self, ratio_source="measured", pct=43, truncated=False):
        return est.Estimate(pct, 30000, RESET, 40.0, 2400.0, 10000.0,
                            ratio_source, truncated)

    def test_the_figure_is_one_number_and_a_floor(self):
        text = est.estimate_sentence(self.make(), NOW)
        self.assertIn("Estimated 43%", text)
        self.assertIn("floor", text)
        self.assertIn("anchored at 40% 40m ago", text)
        self.assertIsNone(re.search(r"\d+-\d+%", text))
        self.assertNotIn("configured", text)

    def test_a_configured_ratio_is_named(self):
        self.assertIn("usage_watch.default_ratio",
                      est.estimate_sentence(self.make("configured"), NOW))

    def test_no_ratio_reports_tokens_and_no_percentage(self):
        text = est.estimate_sentence(self.make("none", pct=None), NOW)
        self.assertIn("No percentage yet: 30,000 output tokens", text)
        self.assertNotIn("Estimated", text)

    def test_a_truncated_scan_is_disclosed(self):
        self.assertIn("partial", est.estimate_sentence(self.make(truncated=True), NOW))

    def test_none_is_empty_and_the_unanchored_text_names_the_flag(self):
        self.assertEqual(est.estimate_sentence(None, NOW), "")
        self.assertIn("--resets-in", est.unanchored_sentence())

    def test_render_age(self):
        self.assertEqual(est.render_age(2400), "40m")
        self.assertEqual(est.render_age(5400), "1.5h")
        self.assertEqual(est.render_age(7200), "2h")
        self.assertEqual((est.render_age(-1), est.render_age("x")), ("", ""))


if __name__ == "__main__":
    unittest.main()
