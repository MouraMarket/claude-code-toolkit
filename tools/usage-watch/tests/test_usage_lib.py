"""Tests for usage-budget decision logic.

Hermetic and clock-injected: every test passes an explicit `now`, so none of
these depend on when the suite runs. The real rate_limits payload only appears
after a live API response, so every fixture here is hand-built.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import usage_lib as usage

NOW = 1_776_000_000  # a fixed epoch-seconds "now" for every test


def payload(five=None, seven=None):
    """A statusLine payload carrying only what these tests care about."""
    limits = {}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five[0], "resets_at": five[1]}
    if seven is not None:
        limits["seven_day"] = {"used_percentage": seven[0], "resets_at": seven[1]}
    return {"rate_limits": limits} if limits else {}


class TestExtractBuckets(unittest.TestCase):

    def test_both_buckets_are_extracted(self):
        got = usage.extract_buckets(payload(five=(72.4, NOW + 600),
                                           seven=(88.1, NOW + 86400)))
        self.assertEqual(set(got), {"five_hour", "seven_day"})
        self.assertAlmostEqual(got["five_hour"]["used_percentage"], 72.4)
        self.assertEqual(got["seven_day"]["resets_at"], NOW + 86400)

    def test_absent_rate_limits_yields_nothing(self):
        # The normal state before a session's first API response.
        self.assertEqual(usage.extract_buckets({}), {})

    def test_one_bucket_present_is_fine(self):
        got = usage.extract_buckets(payload(five=(10.0, NOW + 60)))
        self.assertEqual(set(got), {"five_hour"})

    def test_garbage_shapes_are_dropped_not_raised(self):
        for bad in (None, [], "nope", {"rate_limits": None},
                    {"rate_limits": "nope"}, {"rate_limits": {"five_hour": 5}},
                    {"rate_limits": {"five_hour": {"used_percentage": "x",
                                                  "resets_at": NOW}}}):
            self.assertEqual(usage.extract_buckets(bad), {}, repr(bad))

    def test_booleans_are_rejected(self):
        # bool subclasses int, so True would otherwise read as 1% used and
        # resets_at=1 (1970). The guard exists; this is what proves it still does
        # — a review found it could be deleted with the whole suite still green.
        for field in ("used_percentage", "resets_at"):
            entry = {"used_percentage": 50.0, "resets_at": NOW + 600}
            entry[field] = True
            self.assertEqual(
                usage.extract_buckets({"rate_limits": {"five_hour": entry}}),
                {}, field)

    def test_infinities_are_rejected(self):
        # Reachable, not theoretical: json.load accepts the non-standard
        # `Infinity` literal and json.dump emits it, so a bad value round-trips
        # through our own cache file. int(inf) raises OverflowError, and an
        # infinite used_percentage would be read as HALT forever.
        for field in ("used_percentage", "resets_at"):
            for value in (float("inf"), float("-inf")):
                entry = {"used_percentage": 50.0, "resets_at": NOW + 600}
                entry[field] = value
                self.assertEqual(
                    usage.extract_buckets({"rate_limits": {"five_hour": entry}}),
                    {}, "{0}={1}".format(field, value))

    def test_a_non_finite_payload_never_raises(self):
        # The docstring promises {} for every unusable shape rather than an
        # exception. read_cache calls this with no try/except of its own.
        try:
            usage.extract_buckets({"rate_limits": {
                "five_hour": {"used_percentage": float("inf"),
                              "resets_at": float("inf")}}})
        except Exception as exc:  # pragma: no cover - the assertion is the point
            self.fail("extract_buckets raised {0!r}".format(exc))

    def test_an_unknown_bucket_name_is_ignored(self):
        # Anthropic ships other windows (seven_day_opus, extra_usage). We track
        # only the two documented in the statusLine schema.
        got = usage.extract_buckets(
            {"rate_limits": {"seven_day_opus": {"used_percentage": 99,
                                                "resets_at": NOW + 10}}})
        self.assertEqual(got, {})


class TestLiveBuckets(unittest.TestCase):

    def test_an_expired_window_is_discarded(self):
        # THE expensive failure: a rolled-over window's cached 88% would halt a
        # build against a budget that is actually empty.
        buckets = usage.extract_buckets(payload(five=(88.0, NOW - 1)))
        self.assertEqual(usage.live_buckets(buckets, NOW), {})

    def test_a_window_resetting_exactly_now_is_discarded(self):
        buckets = usage.extract_buckets(payload(five=(88.0, NOW)))
        self.assertEqual(usage.live_buckets(buckets, NOW), {})

    def test_a_future_window_survives(self):
        buckets = usage.extract_buckets(payload(five=(88.0, NOW + 1)))
        self.assertEqual(set(usage.live_buckets(buckets, NOW)), {"five_hour"})

    def test_only_the_expired_bucket_is_dropped(self):
        buckets = usage.extract_buckets(payload(five=(91.0, NOW - 5),
                                                seven=(40.0, NOW + 86400)))
        self.assertEqual(set(usage.live_buckets(buckets, NOW)), {"seven_day"})


class TestBinding(unittest.TestCase):

    def test_the_tighter_bucket_wins(self):
        buckets = usage.extract_buckets(payload(five=(40.0, NOW + 600),
                                                seven=(88.0, NOW + 86400)))
        name, pct, resets = usage.binding(buckets)
        self.assertEqual(name, "seven_day")
        self.assertAlmostEqual(pct, 88.0)
        self.assertEqual(resets, NOW + 86400)

    def test_the_tighter_bucket_wins_the_other_way_too(self):
        buckets = usage.extract_buckets(payload(five=(91.0, NOW + 600),
                                                seven=(55.0, NOW + 86400)))
        self.assertEqual(usage.binding(buckets)[0], "five_hour")

    def test_no_buckets_means_no_binding_constraint(self):
        self.assertIsNone(usage.binding({}))

    def test_a_tie_resolves_deterministically(self):
        # Not arbitrary: a flapping name in the warning text reads as a bug.
        buckets = usage.extract_buckets(payload(five=(80.0, NOW + 600),
                                                seven=(80.0, NOW + 86400)))
        self.assertEqual(usage.binding(buckets)[0], "five_hour")


class TestTierFor(unittest.TestCase):
    """Boundaries are inclusive: at exactly the threshold, the tier applies."""

    def test_below_notice_is_none(self):
        self.assertEqual(usage.tier_for(69.9, 70, 85, 95), usage.TIER_NONE)

    def test_at_notice_is_notice(self):
        self.assertEqual(usage.tier_for(70, 70, 85, 95), usage.TIER_NOTICE)

    def test_just_below_gate_is_still_notice(self):
        self.assertEqual(usage.tier_for(84.9, 70, 85, 95), usage.TIER_NOTICE)

    def test_at_gate_is_gate(self):
        self.assertEqual(usage.tier_for(85, 70, 85, 95), usage.TIER_GATE)

    def test_just_below_halt_is_still_gate(self):
        self.assertEqual(usage.tier_for(94.9, 70, 85, 95), usage.TIER_GATE)

    def test_at_halt_is_halt(self):
        self.assertEqual(usage.tier_for(95, 70, 85, 95), usage.TIER_HALT)

    def test_above_halt_is_halt(self):
        self.assertEqual(usage.tier_for(100, 70, 85, 95), usage.TIER_HALT)

    def test_zero_and_negative_are_none(self):
        self.assertEqual(usage.tier_for(0, 70, 85, 95), usage.TIER_NONE)
        self.assertEqual(usage.tier_for(-1, 70, 85, 95), usage.TIER_NONE)

    def test_a_non_finite_percentage_is_no_tier_not_halt(self):
        # Fail closed on garbage. Ranking inf as HALT would pin a session at the
        # strictest tier for as long as a bad cache entry survived.
        for value in (float("inf"), float("-inf"), float("nan")):
            self.assertEqual(usage.tier_for(value, 70, 85, 95),
                             usage.TIER_NONE, repr(value))

    def test_custom_thresholds_are_honoured(self):
        self.assertEqual(usage.tier_for(50, 50, 60, 70), usage.TIER_NOTICE)
        self.assertEqual(usage.tier_for(65, 50, 60, 70), usage.TIER_GATE)


class TestTierAtLeast(unittest.TestCase):

    def test_severity_ordering(self):
        self.assertTrue(usage.tier_at_least(usage.TIER_HALT, usage.TIER_GATE))
        self.assertTrue(usage.tier_at_least(usage.TIER_GATE, usage.TIER_GATE))
        self.assertFalse(usage.tier_at_least(usage.TIER_NOTICE, usage.TIER_GATE))
        self.assertFalse(usage.tier_at_least(usage.TIER_NONE, usage.TIER_NOTICE))

    def test_an_unknown_tier_is_treated_as_lowest(self):
        self.assertFalse(usage.tier_at_least("bogus", usage.TIER_NOTICE))


class TestFormatting(unittest.TestCase):

    def test_a_reset_within_the_day_shows_a_clock_time(self):
        out = usage.format_reset(NOW + 3600, NOW)
        self.assertRegex(out, r"^\d{2}:\d{2}$")

    def test_a_reset_further_out_shows_a_weekday(self):
        out = usage.format_reset(NOW + 4 * 86400, NOW)
        self.assertIn(out, ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))

    def test_an_unusable_reset_renders_a_placeholder_not_a_crash(self):
        for bad in (None, "soon", float("nan")):
            self.assertIsInstance(usage.format_reset(bad, NOW), str)

    def test_bucket_labels_are_human(self):
        self.assertEqual(usage.bucket_label("five_hour"), "5h")
        self.assertEqual(usage.bucket_label("seven_day"), "7d")
        self.assertEqual(usage.bucket_label("mystery"), "mystery")


import json
import tempfile


class TestConfigDefaults(unittest.TestCase):
    """With no config file, every getter returns its documented default."""

    def setUp(self):
        os.environ["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent-on-purpose.json"
        usage._reset_config_cache()

    def test_enabled_defaults_on(self):
        self.assertTrue(usage.is_enabled())

    def test_subagent_and_postmortem_default_on(self):
        self.assertTrue(usage.subagent_context_enabled())
        self.assertTrue(usage.postmortem_enabled())

    def test_thresholds_default_to_70_85_95(self):
        self.assertEqual(usage.thresholds(), (70, 85, 95))

    def test_stale_after_defaults_to_fifteen_minutes(self):
        self.assertEqual(usage.stale_after(), 900)

    def test_paths_are_absolute_and_expanded(self):
        for path in (usage.cache_path(), usage.state_path()):
            self.assertTrue(os.path.isabs(path))
            self.assertNotIn("~", path)

    def test_the_two_state_files_are_not_the_same_file(self):
        # One file with two writers would race: the status line rewrites every
        # 30s while a hook records an announcement.
        self.assertNotEqual(usage.cache_path(), usage.state_path())


class TestConfigOverrides(unittest.TestCase):

    def _write_config(self, blob):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(blob, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        os.environ["CLAUDE_TOOLKIT_CONFIG"] = handle.name
        usage._reset_config_cache()

    def test_thresholds_are_read_from_config(self):
        self._write_config({"usage_watch": {"notice_pct": 50, "gate_pct": 60,
                                            "halt_pct": 70}})
        self.assertEqual(usage.thresholds(), (50, 60, 70))

    def test_enabled_false_is_honoured(self):
        self._write_config({"usage_watch": {"enabled": False}})
        self.assertFalse(usage.is_enabled())

    def test_nonsense_thresholds_fall_back_to_defaults(self):
        self._write_config({"usage_watch": {"notice_pct": "seventy",
                                            "gate_pct": None}})
        notice, gate, halt = usage.thresholds()
        self.assertEqual((notice, gate, halt), (70, 85, 95))

    def test_out_of_order_thresholds_are_repaired_not_obeyed(self):
        # halt below gate would make the halt tier unreachable. Sorting is the
        # conservative repair: the strictest configured number still bites.
        self._write_config({"usage_watch": {"notice_pct": 95, "gate_pct": 85,
                                            "halt_pct": 70}})
        notice, gate, halt = usage.thresholds()
        self.assertLessEqual(notice, gate)
        self.assertLessEqual(gate, halt)

    def test_a_malformed_config_file_does_not_raise(self):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.write("{ this is not json")
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        os.environ["CLAUDE_TOOLKIT_CONFIG"] = handle.name
        usage._reset_config_cache()
        self.assertEqual(usage.thresholds(), (70, 85, 95))
        self.assertTrue(usage.is_enabled())


class TestCacheRoundTrip(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "budget.json")

    def test_a_written_cache_reads_back(self):
        buckets = usage.extract_buckets(payload(five=(72.0, NOW + 600)))
        self.assertTrue(usage.write_cache(buckets, NOW, path=self.cache))
        got = usage.read_cache(NOW + 1, path=self.cache, stale_after=900)
        self.assertAlmostEqual(got["five_hour"]["used_percentage"], 72.0)

    def test_an_absent_cache_reads_as_empty(self):
        got = usage.read_cache(NOW, path=os.path.join(self.dir, "nope.json"),
                               stale_after=900)
        self.assertEqual(got, {})

    def test_a_malformed_cache_reads_as_empty(self):
        with open(self.cache, "w") as handle:
            handle.write("{ broken")
        self.assertEqual(usage.read_cache(NOW, path=self.cache,
                                          stale_after=900), {})

    def test_a_stale_cache_reads_as_empty(self):
        buckets = usage.extract_buckets(payload(five=(72.0, NOW + 100000)))
        usage.write_cache(buckets, NOW, path=self.cache)
        # Written at NOW, read 901s later with a 900s ceiling.
        self.assertEqual(usage.read_cache(NOW + 901, path=self.cache,
                                          stale_after=900), {})

    def test_reading_applies_the_expiry_discard(self):
        # A window that rolled over between write and read must not come back.
        buckets = usage.extract_buckets(payload(five=(88.0, NOW + 10)))
        usage.write_cache(buckets, NOW, path=self.cache)
        self.assertEqual(usage.read_cache(NOW + 11, path=self.cache,
                                          stale_after=900), {})

    def test_writing_is_atomic_leaving_no_temp_files(self):
        usage.write_cache(usage.extract_buckets(payload(five=(1.0, NOW + 60))),
                          NOW, path=self.cache)
        leftovers = [n for n in os.listdir(self.dir) if n != "budget.json"]
        self.assertEqual(leftovers, [])

    def test_writing_to_an_unwritable_path_returns_false_not_raise(self):
        # Must carry a FRESH payload, or write_cache returns False at the
        # freshness gate and never reaches the filesystem — the test would
        # still pass while proving nothing about the branch it is named for.
        fresh = usage.extract_buckets(payload(five=(50.0, NOW + 600)))
        self.assertFalse(usage.write_cache(fresh, NOW,
                                           path="/nonexistent-dir/x/budget.json"))


class TestFreshPayloadGate(unittest.TestCase):
    """A replayed payload must not be stamped with a current `written_at`.

    Built from a real incident shape: a cache carried a five_hour window that
    had reset long before the cache was written, next to a seven_day window
    reading a calm figure. Both numbers were stale; only one of them
    said so.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "budget.json")

    def test_an_all_live_payload_is_fresh(self):
        buckets = usage.extract_buckets(payload(five=(50.0, NOW + 600),
                                                seven=(20.0, NOW + 86400)))
        self.assertTrue(usage.is_fresh_payload(buckets, NOW))

    def test_one_dead_window_condemns_the_whole_payload(self):
        # The incident shape exactly: five_hour long expired, seven_day calm.
        buckets = usage.extract_buckets(payload(five=(92.0, NOW - 165600),
                                                seven=(8.0, NOW + 400000)))
        self.assertFalse(usage.is_fresh_payload(buckets, NOW))

    def test_a_window_resetting_exactly_now_is_not_fresh(self):
        buckets = usage.extract_buckets(payload(five=(50.0, NOW)))
        self.assertFalse(usage.is_fresh_payload(buckets, NOW))

    def test_an_empty_payload_is_not_fresh(self):
        for bad in ({}, None, [], "nope"):
            self.assertFalse(usage.is_fresh_payload(bad, NOW), repr(bad))

    def test_a_stale_payload_is_refused_by_write_cache(self):
        buckets = usage.extract_buckets(payload(five=(92.0, NOW - 165600),
                                                seven=(8.0, NOW + 400000)))
        self.assertFalse(usage.write_cache(buckets, NOW, path=self.cache))
        self.assertFalse(os.path.exists(self.cache))

    def test_refusing_leaves_an_older_good_cache_untouched(self):
        # The honest outcome: the previous written_at stays put and ages into
        # staleness, rather than being refreshed onto rotten numbers.
        good = usage.extract_buckets(payload(five=(30.0, NOW + 600)))
        self.assertTrue(usage.write_cache(good, NOW, path=self.cache))
        rotten = usage.extract_buckets(payload(five=(92.0, NOW - 165600),
                                               seven=(8.0, NOW + 400000)))
        self.assertFalse(usage.write_cache(rotten, NOW + 60, path=self.cache))
        with open(self.cache) as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["written_at"], NOW)
        self.assertAlmostEqual(
            on_disk["buckets"]["five_hour"]["used_percentage"], 30.0)


class TestPartialReadingIsDiscarded(unittest.TestCase):
    """max() across windows cannot spot a window that is simply absent."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "budget.json")

    def test_a_partial_reading_reads_as_nothing(self):
        both = usage.extract_buckets(payload(five=(92.0, NOW + 10),
                                             seven=(8.0, NOW + 400000)))
        usage.write_cache(both, NOW, path=self.cache)
        # 11s later the five_hour window has rolled; the calm seven_day has not.
        got = usage.read_cache(NOW + 11, path=self.cache, stale_after=900)
        self.assertEqual(got, {}, "a surviving calm window must not stand in "
                                  "for the tight one that vanished")

    def test_an_all_live_reading_still_comes_back_whole(self):
        both = usage.extract_buckets(payload(five=(92.0, NOW + 600),
                                             seven=(8.0, NOW + 400000)))
        usage.write_cache(both, NOW, path=self.cache)
        got = usage.read_cache(NOW + 60, path=self.cache, stale_after=900)
        self.assertEqual(set(got), {"five_hour", "seven_day"})


class TestReadCacheStatus(unittest.TestCase):
    """Three states, because the middle one needs its own handling."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "budget.json")

    def _status(self, at):
        return usage.read_cache_status(at, path=self.cache, stale_after=900)

    def test_all_live_reads_ok(self):
        usage.write_cache(usage.extract_buckets(
            payload(five=(50.0, NOW + 600), seven=(20.0, NOW + 400000))),
            NOW, path=self.cache)
        state, buckets = self._status(NOW + 60)
        self.assertEqual(state, usage.READING_OK)
        self.assertEqual(set(buckets), {"five_hour", "seven_day"})

    def test_a_rolled_window_reads_partial_not_none(self):
        # The ordinary rollover. Conflating it with READING_NONE is what made
        # a healthy session cry "no live reading" over a 60-second-old cache.
        usage.write_cache(usage.extract_buckets(
            payload(five=(50.0, NOW + 10), seven=(20.0, NOW + 400000))),
            NOW, path=self.cache)
        state, buckets = self._status(NOW + 11)
        self.assertEqual(state, usage.READING_PARTIAL)
        self.assertEqual(set(buckets), {"seven_day"})

    def test_stale_reads_none_even_when_every_window_is_live(self):
        usage.write_cache(usage.extract_buckets(payload(five=(50.0, NOW + 99999))),
                          NOW, path=self.cache)
        self.assertEqual(self._status(NOW + 901)[0], usage.READING_NONE)

    def test_absent_and_malformed_read_none(self):
        self.assertEqual(usage.read_cache_status(
            NOW, path=os.path.join(self.dir, "nope.json"))[0], usage.READING_NONE)
        with open(self.cache, "w") as handle:
            handle.write("{ broken")
        self.assertEqual(self._status(NOW)[0], usage.READING_NONE)

    def test_every_window_rolled_on_a_fresh_file_reads_partial(self):
        # A single-window cache is a normal shape (see
        # test_one_bucket_present_is_fine), and its rollover empties the live
        # set entirely. Reading that as READING_NONE told a healthy session,
        # 30 seconds after a good write, that "a status line was feeding it
        # and has stopped" — and burned the announcement slot doing so.
        usage.write_cache(usage.extract_buckets(payload(five=(50.0, NOW + 10))),
                          NOW, path=self.cache)
        state, buckets = self._status(NOW + 30)
        self.assertEqual(state, usage.READING_PARTIAL)
        self.assertEqual(buckets, {})

    def test_that_same_file_reads_none_once_it_goes_stale(self):
        # The other half of the contract: transient now, but if the feed
        # really has stopped this must still become an announced UNKNOWN.
        usage.write_cache(usage.extract_buckets(payload(five=(50.0, NOW + 10))),
                          NOW, path=self.cache)
        self.assertEqual(self._status(NOW + 901)[0], usage.READING_NONE)

    def test_read_cache_still_discards_a_partial(self):
        # Pins the narrow contract itself, not a caller's use of it: all three
        # hooks now read through read_cache_status, so nothing in production
        # reaches read_cache and only tests like this one do.
        usage.write_cache(usage.extract_buckets(
            payload(five=(92.0, NOW + 10), seven=(8.0, NOW + 400000))),
            NOW, path=self.cache)
        self.assertEqual(usage.read_cache(NOW + 11, path=self.cache,
                                          stale_after=900), {})


class TestCacheAge(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "budget.json")

    def test_age_is_measured_from_written_at(self):
        usage.write_cache(usage.extract_buckets(payload(five=(1.0, NOW + 600))),
                          NOW, path=self.cache)
        self.assertAlmostEqual(usage.cache_age(NOW + 3600, path=self.cache),
                               3600.0)

    def test_an_absent_cache_has_no_age_rather_than_a_huge_one(self):
        # None and "very old" are different diagnoses and must stay different.
        self.assertIsNone(usage.cache_age(
            NOW, path=os.path.join(self.dir, "nope.json")))

    def test_a_malformed_cache_has_no_age(self):
        with open(self.cache, "w") as handle:
            handle.write("{ broken")
        self.assertIsNone(usage.cache_age(NOW, path=self.cache))


class TestUnknownAnnouncement(unittest.TestCase):
    """Unknown is a standing condition, deduped on a clock rather than a tier."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state.json")

    def test_it_is_announced_once_then_suppressed(self):
        self.assertFalse(usage.unknown_announced("sess-1", NOW, path=self.state))
        usage.record_unknown("sess-1", NOW, path=self.state, reannounce=3600)
        self.assertTrue(usage.unknown_announced("sess-1", NOW + 60,
                                                path=self.state))

    def test_it_re_arms_once_the_window_lapses(self):
        usage.record_unknown("sess-1", NOW, path=self.state, reannounce=3600)
        self.assertFalse(usage.unknown_announced("sess-1", NOW + 3601,
                                                 path=self.state))

    def test_sessions_do_not_share_the_notice(self):
        usage.record_unknown("sess-1", NOW, path=self.state, reannounce=3600)
        self.assertFalse(usage.unknown_announced("sess-2", NOW + 60,
                                                 path=self.state))

    def test_the_unknown_record_does_not_evict_a_real_tier(self):
        # Both live in one session's dict; the synthetic bucket must not
        # collide with, or prune away, a genuine window's announcement.
        usage.record_announcement("sess-1", "five_hour", NOW + 600,
                                  usage.TIER_GATE, now=NOW, path=self.state)
        usage.record_unknown("sess-1", NOW, path=self.state, reannounce=3600)
        self.assertTrue(usage.already_announced(
            "sess-1", "five_hour", NOW + 600, usage.TIER_GATE, path=self.state))

    def test_unknown_is_not_rankable_against_a_real_tier(self):
        # TIER_UNKNOWN is outside TIER_ORDER on purpose: it can be true at 3%
        # or at 99%, so every answer to "is it at least a gate?" is wrong.
        self.assertNotIn(usage.TIER_UNKNOWN, usage.TIER_ORDER)
        self.assertFalse(usage.tier_at_least(usage.TIER_UNKNOWN,
                                             usage.TIER_NOTICE))


class TestUnknownMessage(unittest.TestCase):

    def test_it_says_unknown_and_refuses_the_headroom_reading(self):
        text = usage.unknown_message(16506)
        self.assertIn("UNKNOWN", text)
        self.assertIn("not as headroom", text.lower())

    def test_it_names_the_terminal_only_cause(self):
        self.assertIn("terminal", usage.unknown_message(60).lower())

    def test_the_age_clause_is_rendered_in_hours(self):
        self.assertIn("5h old", usage.unknown_message(5 * 3600))

    def test_a_young_cache_renders_in_minutes(self):
        self.assertIn("30m old", usage.unknown_message(1800))

    def test_no_age_means_no_invented_number(self):
        text = usage.unknown_message(None)
        # NOT assertNotIn("old"): this string carries owner() from whatever
        # config is installed, and a three-letter substring is hermetic only
        # until someone is named Harold or Reynolds. Match the clause itself.
        self.assertNotIn("cached reading is", text)
        self.assertIn("UNKNOWN", text)

    def test_a_stopped_feed_and_a_missing_one_name_different_causes(self):
        # Sending someone to the wrong place is how a warning earns a reader
        # who skips it. "A status line stopped" and "none was ever configured"
        # have nothing to do with each other.
        stopped = usage.unknown_message(18000)
        never = usage.unknown_message(None)
        self.assertIn("has stopped", stopped)
        self.assertNotIn("API key", stopped)
        self.assertIn("ever written", never)
        self.assertIn("API key", never)
        self.assertNotIn("has stopped", never)

    def test_the_never_fed_case_offers_the_off_switch(self):
        # API-key auth never receives rate_limits at all, so for those
        # sessions the notice can never clear. Say what to do about it.
        self.assertIn("enabled", usage.unknown_message(None))

    def test_a_garbage_age_degrades_to_no_clause(self):
        # The name promises no clause, so check for its absence — asserting
        # only that UNKNOWN survives would pass even if a garbage age rendered
        # "(last cached reading is nanm old)".
        for bad in ("nope", float("inf"), float("nan")):
            text = usage.unknown_message(bad)
            self.assertIn("UNKNOWN", text, repr(bad))
            self.assertNotIn("cached reading is", text, repr(bad))


class TestUnknownCauseForABrokenFile(unittest.TestCase):
    """The third cause. A cache that will not parse has no age, and on age
    alone the notice said no status line was ever installed — to someone whose
    status line was running fine — and offered the off switch."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "budget.json")

    def test_a_malformed_cache_reads_unreadable_not_absent(self):
        with open(self.cache, "w") as handle:
            handle.write("{ broken")
        self.assertEqual(usage.cache_presence(path=self.cache),
                         usage.CACHE_UNREADABLE)

    def test_a_cache_with_no_written_at_reads_unreadable(self):
        # Parses fine, buckets PERFECTLY usable, no timestamp: still a file a
        # status line wrote something into, so still not "never fed". The
        # buckets have to be valid or the empty-buckets check below satisfies
        # this test and the written_at guard could be deleted unnoticed --
        # which is what a {"buckets": {}} fixture did here.
        with open(self.cache, "w") as handle:
            json.dump({"buckets": {"five_hour": {"used_percentage": 50.0,
                                                 "resets_at": NOW + 600}}},
                      handle)
        self.assertEqual(usage.cache_presence(path=self.cache),
                         usage.CACHE_UNREADABLE)

    def test_an_absent_file_reads_absent(self):
        self.assertEqual(
            usage.cache_presence(path=os.path.join(self.dir, "nope.json")),
            usage.CACHE_ABSENT)

    def test_garbage_buckets_beside_a_good_timestamp_read_unreadable(self):
        # The "written by an older version" case, and the one that made this
        # more than prose drift: read_cache_status rejects these buckets and
        # returns READING_NONE, while cache_age reports a real, small age -- so
        # stopping at written_at rendered "0m old, a status line has stopped"
        # about a file written seconds ago.
        with open(self.cache, "w") as handle:
            json.dump({"written_at": NOW, "buckets": {"five_hour": "nope"}},
                      handle)
        self.assertEqual(usage.cache_presence(path=self.cache),
                         usage.CACHE_UNREADABLE)
        self.assertEqual(usage.read_cache_status(NOW + 1, path=self.cache,
                                                 stale_after=900)[0],
                         usage.READING_NONE)

    def test_a_file_under_an_unsearchable_directory_is_not_absent(self):
        # os.path.exists returns False here -- the stat fails for the same
        # permission reason the open did -- so deciding on existence rather
        # than errno sent a file that is plainly there to "never fed".
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        locked = os.path.join(self.dir, "locked")
        os.makedirs(locked)
        target = os.path.join(locked, "budget.json")
        with open(target, "w") as handle:
            json.dump({"written_at": NOW, "buckets": {}}, handle)
        os.chmod(locked, 0o000)
        try:
            if os.path.exists(target):
                self.skipTest("this filesystem ignores directory permissions")
            self.assertEqual(usage.cache_presence(path=target),
                             usage.CACHE_UNREADABLE)
        finally:
            os.chmod(locked, 0o700)

    def test_a_path_whose_parent_is_a_file_reads_absent(self):
        # ENOTDIR. Nothing is there and nothing can be, so never-fed is the
        # honest cause -- this is the case existence-checking got right and
        # errno has to keep right.
        blocker = os.path.join(self.dir, "notadir")
        with open(blocker, "w") as handle:
            handle.write("x")
        self.assertEqual(
            usage.cache_presence(path=os.path.join(blocker, "budget.json")),
            usage.CACHE_ABSENT)

    def test_a_path_that_cannot_be_opened_is_not_absent(self):
        # A directory where the cache belongs: open() raises OSError, but the
        # path plainly exists, so "never fed" -- and its advice to disable the
        # tool -- is the wrong story.
        target = os.path.join(self.dir, "adirectory.json")
        os.makedirs(target)
        self.assertEqual(usage.cache_presence(path=target),
                         usage.CACHE_UNREADABLE)

    def test_a_healthy_cache_reads_readable(self):
        usage.write_cache(usage.extract_buckets(payload(five=(1.0, NOW + 600))),
                          NOW, path=self.cache)
        self.assertEqual(usage.cache_presence(path=self.cache),
                         usage.CACHE_READABLE)

    def test_the_notice_blames_the_file_not_the_status_line(self):
        text = usage.unknown_message(None, presence=usage.CACHE_UNREADABLE)
        self.assertIn("UNKNOWN", text)
        self.assertIn("could not be read", text)
        # The two claims that were actively wrong for this case.
        self.assertNotIn("ever written", text)
        self.assertNotIn("API key", text)

    def test_an_absent_cache_still_says_never_fed(self):
        text = usage.unknown_message(None, presence=usage.CACHE_ABSENT)
        self.assertIn("ever written", text)
        self.assertNotIn("could not be read", text)

    def test_a_readable_but_stale_cache_still_blames_the_feed(self):
        text = usage.unknown_message(18000, presence=usage.CACHE_READABLE)
        self.assertIn("has stopped", text)
        self.assertNotIn("could not be read", text)

    def test_a_garbage_age_beside_a_broken_file_still_blames_the_file(self):
        # The except branch picks a cause too, and it used to pick the wrong
        # one for exactly this pair.
        text = usage.unknown_message("nope", presence=usage.CACHE_UNREADABLE)
        self.assertIn("could not be read", text)
        self.assertNotIn("cached reading is", text)

    def test_a_readable_age_does_not_override_a_broken_file(self):
        # Presence wins, and the clause goes with it. A file with intact
        # written_at and unusable buckets HAS an age, so age-first rendered
        # "0m old, a status line has stopped" about a file seconds old -- and
        # an age quoted off a file we just called unreadable is half-believable
        # in the worst way.
        text = usage.unknown_message(30, presence=usage.CACHE_UNREADABLE)
        self.assertIn("could not be read", text)
        self.assertNotIn("has stopped", text)
        self.assertNotIn("cached reading is", text)


class TestAnnouncementDedupe(unittest.TestCase):
    """PostToolBatch fires after every batch. Without dedupe it nags."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.state = os.path.join(self.dir, "state.json")

    def test_a_tier_is_announced_once_per_window(self):
        args = ("sess-1", "five_hour", NOW + 600, usage.TIER_GATE)
        self.assertFalse(usage.already_announced(*args, path=self.state))
        usage.record_announcement(*args, now=NOW, path=self.state)
        self.assertTrue(usage.already_announced(*args, path=self.state))

    def test_a_new_window_re_arms_the_same_tier(self):
        usage.record_announcement("sess-1", "five_hour", NOW + 600,
                                  usage.TIER_GATE, now=NOW, path=self.state)
        # Same session, same tier, but the window has rolled to a new resets_at.
        self.assertFalse(usage.already_announced(
            "sess-1", "five_hour", NOW + 99999, usage.TIER_GATE,
            path=self.state))

    def test_escalating_to_a_higher_tier_is_always_announced(self):
        usage.record_announcement("sess-1", "five_hour", NOW + 600,
                                  usage.TIER_GATE, now=NOW, path=self.state)
        self.assertFalse(usage.already_announced(
            "sess-1", "five_hour", NOW + 600, usage.TIER_HALT, path=self.state))

    def test_dropping_back_to_a_lower_tier_is_not_re_announced(self):
        usage.record_announcement("sess-1", "five_hour", NOW + 600,
                                  usage.TIER_HALT, now=NOW, path=self.state)
        self.assertTrue(usage.already_announced(
            "sess-1", "five_hour", NOW + 600, usage.TIER_NOTICE,
            path=self.state))

    def test_sessions_are_independent(self):
        usage.record_announcement("sess-1", "five_hour", NOW + 600,
                                  usage.TIER_GATE, now=NOW, path=self.state)
        self.assertFalse(usage.already_announced(
            "sess-2", "five_hour", NOW + 600, usage.TIER_GATE, path=self.state))

    def test_a_missing_state_file_means_nothing_announced_yet(self):
        self.assertFalse(usage.already_announced(
            "sess-1", "five_hour", NOW + 600, usage.TIER_GATE,
            path=os.path.join(self.dir, "absent.json")))

    def test_a_malformed_state_file_does_not_raise(self):
        with open(self.state, "w") as handle:
            handle.write("nope")
        self.assertFalse(usage.already_announced(
            "sess-1", "five_hour", NOW + 600, usage.TIER_GATE, path=self.state))
        self.assertTrue(usage.record_announcement(
            "sess-1", "five_hour", NOW + 600, usage.TIER_GATE, now=NOW,
            path=self.state))

    def test_a_session_with_one_live_bucket_survives_pruning(self):
        # The off-by-one the two-independent-sessions test below cannot see:
        # swapping any() for all() in the prune loop drops this session, and
        # its still-live seven_day tier then gets re-announced on the next
        # PostToolBatch. Review confirmed any()->all() passes without this.
        usage.record_announcement("mixed", "five_hour", NOW - 100,
                                  usage.TIER_GATE, now=NOW - 200,
                                  path=self.state)
        usage.record_announcement("mixed", "seven_day", NOW + 500,
                                  usage.TIER_GATE, now=NOW, path=self.state)
        with open(self.state) as handle:
            data = json.load(handle)
        self.assertIn("mixed", data.get("sessions", {}))
        self.assertTrue(usage.already_announced(
            "mixed", "seven_day", NOW + 500, usage.TIER_GATE, path=self.state))

    def test_a_malformed_now_does_not_raise(self):
        # Documented contract is "never raises". Prior to this, now=None hit a
        # TypeError comparing int > None inside the prune loop.
        for bad in (None, "soon", float("nan")):
            self.assertTrue(usage.record_announcement(
                "s", "five_hour", NOW + 600, usage.TIER_GATE, now=bad,
                path=self.state), repr(bad))

    def test_the_temp_file_is_created_beside_its_target(self):
        # os.replace is atomic only WITHIN a filesystem, so a temp file in the
        # shared temp dir defeats the point. Asserting on leftover files cannot
        # catch this — a temp file in /tmp is invisible to the target dir — so
        # assert on the call itself. Review confirmed dropping dir= otherwise
        # passes the whole suite on a machine where /tmp shares a filesystem.
        seen = {}
        real_mkstemp = usage.tempfile.mkstemp

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return real_mkstemp(*args, **kwargs)

        usage.tempfile.mkstemp = spy
        try:
            usage.write_cache(
                usage.extract_buckets(payload(five=(1.0, NOW + 60))), NOW,
                path=os.path.join(self.dir, "budget.json"))
        finally:
            usage.tempfile.mkstemp = real_mkstemp
        self.assertEqual(seen.get("dir"), self.dir)

    def test_expired_sessions_are_pruned_so_the_file_cannot_grow_forever(self):
        usage.record_announcement("old", "five_hour", NOW - 10,
                                  usage.TIER_GATE, now=NOW - 20, path=self.state)
        usage.record_announcement("new", "five_hour", NOW + 600,
                                  usage.TIER_GATE, now=NOW, path=self.state)
        with open(self.state) as handle:
            data = json.load(handle)
        self.assertNotIn("old", data.get("sessions", {}))
        self.assertIn("new", data.get("sessions", {}))


class TestRenderStatusLine(unittest.TestCase):

    def base(self, **extra):
        blob = {
            "cwd": "/Users/x/work/repos/webapp",
            "model": {"id": "claude-opus-5[1m]", "display_name": "Opus 5"},
            "workspace": {"current_dir": "/Users/x/work/repos/webapp",
                          "project_dir": "/Users/x/work/repos/webapp"},
            "context_window": {"used_percentage": 34.2},
        }
        blob.update(extra)
        return blob

    def test_the_line_carries_dir_model_and_context(self):
        line = usage.render_status(self.base())
        self.assertIn("webapp", line)
        self.assertIn("Opus 5", line)
        self.assertIn("34%", line)

    def test_both_budgets_appear_when_present(self):
        line = usage.render_status(self.base(
            rate_limits={"five_hour": {"used_percentage": 72.4,
                                       "resets_at": int(time.time()) + 3600},
                         "seven_day": {"used_percentage": 88.1,
                                       "resets_at": int(time.time()) + 300000}}))
        self.assertIn("5h 72%", line)
        self.assertIn("7d 88%", line)

    def test_absent_rate_limits_still_renders_a_line(self):
        # The normal state before the first API response of every session.
        line = usage.render_status(self.base())
        self.assertTrue(line.strip())
        self.assertNotIn("5h", line)

    def test_an_expired_budget_window_is_not_shown(self):
        line = usage.render_status(self.base(
            rate_limits={"five_hour": {"used_percentage": 99.0,
                                       "resets_at": int(time.time()) - 5}}))
        self.assertNotIn("99%", line)

    def test_a_worktree_branch_is_shown_when_present(self):
        line = usage.render_status(self.base(
            worktree={"name": "usage-watch", "path": "/tmp/x",
                      "branch": "feat/usage-watch"}))
        self.assertIn("feat/usage-watch", line)

    def test_a_control_character_in_any_segment_cannot_break_the_line(self):
        # A newline is legal in a POSIX path segment, so cwd alone can carry
        # one. Review reproduced this against the real executable: the rendered
        # line contained an embedded \n and corrupted the status bar.
        line = usage.render_status(self.base(
            cwd="/tmp/weird\ndir", model={"display_name": "Opus\r5"}))
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertNotIn("\t", line)

    def test_garbage_payloads_render_something_rather_than_raising(self):
        for bad in ({}, None, {"context_window": "nope"}, {"model": 5}):
            self.assertIsInstance(usage.render_status(bad), str)

    def test_the_line_is_a_single_line(self):
        line = usage.render_status(self.base())
        self.assertNotIn("\n", line)


class TestTierMessage(unittest.TestCase):

    def msg(self, tier):
        return usage.tier_message(tier, "seven_day", 88.4, NOW + 300000, NOW)

    def test_none_tier_produces_no_message(self):
        self.assertEqual(usage.tier_message(usage.TIER_NONE, "seven_day", 10.0,
                                            NOW + 600, NOW), "")

    def test_every_real_tier_names_the_number_and_the_window(self):
        for tier in (usage.TIER_NOTICE, usage.TIER_GATE, usage.TIER_HALT):
            text = self.msg(tier)
            self.assertIn("88", text)
            self.assertIn("7d", text)

    def test_the_gate_wording_is_pinned_verbatim(self):
        # Keyword-presence assertions cannot see an inversion: review proved the
        # clause could be flipped to "go ahead and dispatch more subagents
        # freely" with the whole suite green. These pin the exact instruction,
        # because the subagent protection IS this sentence — there is no other
        # mechanism enforcing it.
        text = self.msg(usage.TIER_GATE)
        self.assertIn("STOP and raise an AskUserQuestion", text)
        self.assertIn("do not dispatch new subagents", text)

    def test_the_halt_wording_is_pinned_verbatim(self):
        text = self.msg(usage.TIER_HALT)
        self.assertIn("Start no new plan task", text)
        self.assertIn("dispatch nothing", text)

    def test_no_tier_ever_authorises_continuing(self):
        # A directional check the substring tests cannot make: no message may
        # tell the model to carry on regardless.
        for tier in (usage.TIER_GATE, usage.TIER_HALT):
            lowered = self.msg(tier).lower()
            for phrase in ("go ahead", "keep going", "without waiting",
                           "do not stop", "freely"):
                self.assertNotIn(phrase, lowered, "{0}: {1}".format(tier, phrase))

    def test_the_gate_tier_demands_a_decision(self):
        text = self.msg(usage.TIER_GATE).lower()
        self.assertIn("askuserquestion", text.replace(" ", ""))

    def test_the_gate_and_halt_tiers_mention_subagent_dispatch(self):
        for tier in (usage.TIER_GATE, usage.TIER_HALT):
            self.assertIn("dispatch", self.msg(tier).lower())

    def test_the_halt_tier_forbids_new_work_and_asks_for_a_handover(self):
        text = self.msg(usage.TIER_HALT).lower()
        self.assertIn("handover", text)

    def test_the_notice_tier_does_not_gate(self):
        text = self.msg(usage.TIER_NOTICE).lower()
        self.assertNotIn("askuserquestion", text.replace(" ", ""))

    def test_hook_output_is_valid_json_with_the_echoed_event(self):
        blob = json.loads(usage.hook_output("PostToolBatch", "hello"))
        self.assertEqual(blob["hookSpecificOutput"]["hookEventName"],
                         "PostToolBatch")
        self.assertEqual(blob["hookSpecificOutput"]["additionalContext"],
                         "hello")


class TestSubagentMessage(unittest.TestCase):

    def test_it_states_the_budget_and_the_blocked_contract(self):
        text = usage.subagent_message("seven_day", 88.4, NOW + 300000, NOW,
                                      agent_type="executor")
        self.assertIn("88", text)
        self.assertIn("7d", text)
        self.assertIn("BLOCKED", text)

    def test_it_never_tells_the_subagent_to_stop_unconditionally(self):
        # A worker that refuses everything is worse than one that overruns.
        text = usage.subagent_message("five_hour", 96.0, NOW + 600, NOW).lower()
        self.assertNotIn("do not do the task", text)

    def test_a_healthy_budget_produces_no_message(self):
        self.assertEqual(
            usage.subagent_message("five_hour", 12.0, NOW + 600, NOW), "")

    def test_the_agent_type_is_optional(self):
        self.assertIsInstance(
            usage.subagent_message("five_hour", 90.0, NOW + 600, NOW), str)


class TestPostmortem(unittest.TestCase):

    def test_only_budget_reasons_trigger_a_postmortem(self):
        self.assertTrue(usage.should_postmortem("rate_limit"))
        self.assertTrue(usage.should_postmortem("billing_error"))

    def test_every_other_api_error_is_ignored(self):
        # The exact StopFailure enum. An overloaded API is not a budget problem,
        # and writing a handover for it would cry wolf on every transient blip.
        for reason in ("overloaded", "authentication_failed",
                       "oauth_org_not_allowed", "invalid_request",
                       "model_not_found", "server_error", "max_output_tokens",
                       "unknown", "", None):
            self.assertFalse(usage.should_postmortem(reason), repr(reason))

    def test_a_superstring_reason_is_not_a_match(self):
        # Exact membership, not substring. Review proved a substring-matching
        # regression passed the whole suite, because none of the eight ignored
        # reasons happens to contain "rate_limit" or "billing_error".
        for reason in ("rate_limit_exceeded", "pre_rate_limit", "RATE_LIMIT",
                       "Billing_Error", " rate_limit"):
            self.assertFalse(usage.should_postmortem(reason), repr(reason))

    def test_a_session_id_with_a_path_separator_is_made_safe(self):
        # Not a directory-escape bug: the mandatory date-budget- prefix means a
        # leading ../ never names a real directory. The bug is that os.replace
        # then fails with ENOENT and the document is silently discarded — no
        # file, no error, exit 0 — exactly when a build has died on budget.
        for hostile in ("../../../tmp/evil", "a/b/c", "..", "/", "  ", ""):
            slug = usage.safe_slug(hostile)
            self.assertNotIn("/", slug, repr(hostile))
            self.assertNotIn("\\", slug, repr(hostile))
            self.assertTrue(slug, repr(hostile))
            self.assertEqual(slug, os.path.basename(slug), repr(hostile))

    def test_safe_slug_keeps_ordinary_ids_recognisable(self):
        # Sanitising must not make the filename useless for finding the session.
        self.assertEqual(usage.safe_slug("s1"), "s1")
        self.assertEqual(usage.safe_slug("a1b2-c3d4_e5"), "a1b2-c3d4_e5")

    def test_safe_slug_is_bounded(self):
        self.assertLessEqual(len(usage.safe_slug("x" * 500)), 64)

    def test_an_undetermined_tree_does_not_read_as_clean(self):
        # dirty=None means git never answered. Saying "clean" there tells a
        # human to stop looking for uncommitted work that may well exist.
        doc = usage.postmortem_document("rate_limit", "/x", "main", None, {}, NOW)
        self.assertNotIn("clean", doc.lower())
        self.assertIn("determined", doc.lower())

    def test_the_document_names_the_reason_branch_and_dirty_files(self):
        doc = usage.postmortem_document(
            "rate_limit", "/Users/x/repos/webapp", "feat/thing",
            ["M scripts/a.py", "?? scripts/b.py"],
            usage.extract_buckets(payload(seven=(99.0, NOW + 300000))), NOW)
        self.assertIn("rate_limit", doc)
        self.assertIn("feat/thing", doc)
        self.assertIn("scripts/a.py", doc)
        self.assertIn("scripts/b.py", doc)

    def test_a_clean_tree_is_stated_explicitly(self):
        doc = usage.postmortem_document("rate_limit", "/x", "main", [], {}, NOW)
        self.assertIn("clean", doc.lower())

    def test_the_document_never_claims_work_was_committed(self):
        doc = usage.postmortem_document("rate_limit", "/x", "main",
                                        ["M a.py"], {}, NOW).lower()
        self.assertNotIn("committed for you", doc)

    def test_error_details_are_recorded_when_present(self):
        # StopFailure's error_details carries the human-readable limit
        # message, and it is lost if the document never surfaces it.
        doc = usage.postmortem_document(
            "rate_limit", "/x", "main", [], {}, NOW,
            error_details="5-hour limit reached, resets 14:00")
        self.assertIn("5-hour limit reached", doc)

    def test_error_details_are_omitted_when_absent(self):
        doc = usage.postmortem_document("rate_limit", "/x", "main", [], {}, NOW)
        self.assertNotIn("Error detail", doc)


class ShippedReannounceDefaultTest(unittest.TestCase):
    """Pin the unknown-notice cadence a fresh clone gets.

    Every other test in this file passes `reannounce=` explicitly — correct, since
    they assert the dedupe behaviour rather than the chosen period, but it left the
    shipped default asserted by nothing at all.

    The cadence is a real policy call and it has been wrong in both directions.
    Hourly (the old cadence) assumed unknown is transient; on any surface without
    a status line it is permanent, so hourly nagged forever about something the
    reader cannot act on. Silence was worse and is what the whole notice exists to
    prevent: a stale cache used to make the hooks return quietly, and silence is
    indistinguishable from headroom.
    """

    def _seconds_with_config(self, config_path):
        """unknown_reannounce_seconds() as a fresh import sees it.

        A subprocess because the module resolves its config once, at import; an
        in-process call would grade whatever config the runner happens to have.
        """
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config_path)
        tool_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.check_output(
            [sys.executable, "-c",
             "import usage_lib as u; print(u.unknown_reannounce_seconds())"],
            cwd=tool_dir, env=env, universal_newlines=True)
        return int(out.strip())

    def _write_config(self, value):
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False)
        json.dump({"usage_watch": {"unknown_reannounce_seconds": value}}, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_default_is_once_per_working_session(self):
        # This exact-value pin is also what keeps the default FINITE, so there is
        # no separate boundedness test: an assertEqual to 43200 already fails for
        # an infinite sentinel or any other value. One was written here and
        # removed — it could not fail unless this one failed too, which is the
        # same redundancy that retired test_both_gates_still_exist_with_no_config
        # in test_index_lib.py. Applying that standard in one file and not the
        # other is how a suite accumulates tests that only look like coverage.
        #
        # Why finite matters, since the test no longer says it: record_unknown
        # stores its expiry in the same `resets_at` slot a real window uses, so
        # record_announcement's pruning collects it on the same schedule. An
        # infinite sentinel would be the one entry that grows that file forever.
        self.assertEqual(self._seconds_with_config("/nonexistent.json"), 43200)

    def test_a_configured_value_is_honoured(self):
        # The negative control for the two fallback cases below: without this,
        # a getter that ignored config entirely would pass all of them.
        self.assertEqual(self._seconds_with_config(self._write_config(600)), 600)

    def test_a_nonpositive_config_value_falls_back_rather_than_repeating(self):
        # 0 must not come to mean "every turn" — that is the nag this default was
        # retuned to remove, and config is the other road to it.
        for bad in (0, -1, -3600):
            with self.subTest(value=bad):
                self.assertEqual(
                    self._seconds_with_config(self._write_config(bad)), 43200)

    def test_a_non_numeric_config_value_falls_back(self):
        self.assertEqual(
            self._seconds_with_config(self._write_config("hourly")), 43200)


class TestCacheReportReadsOnce(unittest.TestCase):
    """`claude-usage-watch` asked the cache three questions per prompt, each
    through its own `open()`, while a status line rewrote that file between
    them. `cache_report` answers all of them from one read.

    The open COUNT is the assertion, not the field values: a report whose
    fields merely agree with the individual readers on a static fixture would
    agree just as well if it opened the file three times, which is the defect.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "budget.json")

    def _write(self, blob):
        with open(self.path, "w") as handle:
            json.dump(blob, handle)

    def _healthy(self, written_at=None, pct=42.0):
        self._write({"written_at": NOW - 60 if written_at is None else written_at,
                     "buckets": {"five_hour": {"used_percentage": pct,
                                               "resets_at": NOW + 3600}}})

    def _count_opens(self, call):
        """How many times `call` opens THE CACHE FILE. Counts only this path:
        `_configured_stale_after` reads the toolkit config, which is a real
        open and none of this function's business."""
        real = open
        seen = []

        def counting(target, *args, **kwargs):
            if target == self.path:
                seen.append(target)
            return real(target, *args, **kwargs)

        import builtins
        builtins.open = counting
        try:
            result = call()
        finally:
            builtins.open = real
        return len(seen), result

    def test_it_opens_the_cache_exactly_once(self):
        self._healthy()
        opens, report = self._count_opens(
            lambda: usage.cache_report(NOW, path=self.path, stale_after=900))
        self.assertEqual(opens, 1)
        self.assertEqual(report.status, usage.READING_OK)

    def test_the_three_separate_readers_open_it_three_times(self):
        """The baseline the change is measured against. If this ever drops to
        one, the readers have been merged too and the count above proves less
        than it claims — so it is asserted rather than assumed."""
        self._healthy()
        opens, _ = self._count_opens(lambda: (
            usage.read_cache_status(NOW, path=self.path, stale_after=900),
            usage.cache_age(NOW, path=self.path),
            usage.cache_presence(path=self.path)))
        self.assertEqual(opens, 3)

    def test_it_opens_once_even_when_the_file_is_missing(self):
        opens, report = self._count_opens(
            lambda: usage.cache_report(NOW, path=self.path, stale_after=900))
        self.assertEqual(opens, 1)
        self.assertEqual(report.status, usage.READING_NONE)
        self.assertEqual(report.presence, usage.CACHE_ABSENT)
        self.assertIsNone(report.age)

    def test_every_field_matches_its_own_reader(self):
        """Equivalence, so the delegation cannot quietly change an answer.
        Weak on its own — see the class docstring — and load-bearing beside
        the count."""
        cases = [
            {"written_at": NOW - 60,
             "buckets": {"five_hour": {"used_percentage": 42.0,
                                       "resets_at": NOW + 3600}}},
            {"written_at": NOW + 40,          # future stamp: signed vs clamped
             "buckets": {"five_hour": {"used_percentage": 42.0,
                                       "resets_at": NOW + 3600}}},
            {"written_at": NOW - 60, "buckets": {"five_hour": "garbage"}},
            {"written_at": "not a number", "buckets": {}},
            {"buckets": {}},
            [1, 2, 3],
        ]
        for blob in cases:
            self._write(blob)
            report = usage.cache_report(NOW, path=self.path, stale_after=900)
            status, buckets = usage.read_cache_status(NOW, path=self.path,
                                                      stale_after=900)
            self.assertEqual(report.status, status, repr(blob))
            self.assertEqual(report.buckets, buckets, repr(blob))
            self.assertEqual(report.age, usage.cache_age(NOW, path=self.path),
                             repr(blob))
            self.assertEqual(report.reading_age,
                             usage.cache_reading_age(NOW, path=self.path),
                             repr(blob))
            self.assertEqual(report.presence,
                             usage.cache_presence(path=self.path), repr(blob))

    def test_an_unparseable_file_is_unreadable_not_absent(self):
        """The distinction `unknown_message` turns into two different remedies,
        and the one place the snapshot must NOT collapse an exception."""
        with open(self.path, "w") as handle:
            handle.write("{not json")
        report = usage.cache_report(NOW, path=self.path, stale_after=900)
        self.assertEqual(report.presence, usage.CACHE_UNREADABLE)

    def test_an_unvalidatable_bucket_degrades_rather_than_raising(self):
        """`extract_buckets` is NOT total: `math.isfinite` raises
        `OverflowError` on an out-of-float-range int, so a JSON *integer*
        literal of 309+ digits is enough. Moving that call onto the shared
        path put three readers that could never see an exception in reach of
        one — measured against the parent commit:

            read_cache_status(stale)  ('none', {})  -> raised
            cache_age                 a float       -> raised
            cache_reading_age         a float       -> raised

        A JSON *float* literal like `1e400` is a different case and was always
        fine: it parses to `inf`, which `math.isfinite` answers rather than
        rejecting. The integer path is the one with no float to answer about.
        """
        self._write({"written_at": NOW - 60,
                     "buckets": {"five_hour": {"used_percentage": int("9" * 400),
                                               "resets_at": NOW + 3600}}})
        report = usage.cache_report(NOW, path=self.path, stale_after=900)
        self.assertEqual(report.presence, usage.CACHE_UNREADABLE)
        self.assertEqual(report.status, usage.READING_NONE)
        self.assertEqual(report.age, 60.0)
        # Every individual reader degrades too, not just the report.
        self.assertEqual(usage.cache_presence(path=self.path),
                         usage.CACHE_UNREADABLE)
        self.assertEqual(usage.cache_age(NOW, path=self.path), 60.0)
        self.assertEqual(usage.cache_reading_age(NOW, path=self.path), 60.0)
        self.assertEqual(usage.read_cache_status(NOW, path=self.path,
                                                 stale_after=900),
                         (usage.READING_NONE, {}))

    def test_an_unvalidatable_bucket_degrades_on_a_STALE_file_too(self):
        """PARITY WITH THE PARENT, not a guard test — and the distinction is
        stated because the name would otherwise imply the stronger claim.

        A stale file short-circuits on the ceiling before `extract_buckets` is
        reached, so `_status_from`'s guard is not what answers here. Measured:
        narrowing that guard so `OverflowError` escapes leaves this test green,
        and so does disabling the ceiling check — because with the guard in
        place BOTH paths return `(READING_NONE, {})`. Two mechanisms, one
        observable outcome, and here they genuinely cannot be split: agreeing
        is the correct behaviour.

        What it is worth keeping for: the parent returned `('none', {})` on
        exactly this input, and that answer must not change. It is a
        regression pin on the base contract. The guard itself is pinned by
        `test_an_unvalidatable_bucket_degrades_rather_than_raising`, on a
        FRESH file, where the ceiling cannot answer instead.
        """
        self._write({"written_at": NOW - 100000,
                     "buckets": {"five_hour": {"used_percentage": int("9" * 400),
                                               "resets_at": NOW + 3600}}})
        self.assertEqual(usage.read_cache_status(NOW, path=self.path,
                                                 stale_after=900),
                         (usage.READING_NONE, {}))
        self.assertEqual(usage.cache_report(NOW, path=self.path,
                                            stale_after=900).status,
                         usage.READING_NONE)

    def test_garbage_buckets_are_unreadable_but_still_carry_an_age(self):
        """`cache_presence` and `cache_age` genuinely diverge here, so the
        snapshot returns the parsed data alongside an UNREADABLE verdict
        rather than throwing it away."""
        self._write({"written_at": NOW - 300, "buckets": {"five_hour": 7}})
        report = usage.cache_report(NOW, path=self.path, stale_after=900)
        self.assertEqual(report.presence, usage.CACHE_UNREADABLE)
        self.assertEqual(report.age, 300.0)


class TestTheWatchHookUsesOneReport(unittest.TestCase):
    """A source pin, because the regression is a CALL-SITE one and no output
    distinguishes three reads from one on a file that did not change between
    them. Reverting any single call site here is what this catches."""

    def test_the_hook_names_no_per_question_reader(self):
        hook = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "claude-usage-watch")
        # `with`, not a bare `open(...).read()`: the leaked handle printed a
        # ResourceWarning into the suite's output, and that output is read by
        # `install.sh` where it had none before.
        with open(hook) as handle:
            source = handle.read()
        self.assertIn("usage.cache_report(", source)
        for retired in ("usage.read_cache_status(", "usage.cache_age(",
                        "usage.cache_presence(", "usage.cache_reading_age("):
            self.assertNotIn(retired, source,
                             "%s is a second read of a file that moves" % retired)


class TestCacheReadingAgeIsSigned(unittest.TestCase):
    """`cache_age` clamps a negative to 0.0 — right for the sentence it feeds,
    and wrong for a freshness GATE, where a future stamp is the single most
    suspicious value a cache can carry."""

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "budget.json")

    def _write(self, written_at):
        with open(self.path, "w") as handle:
            json.dump({"written_at": written_at, "buckets": {}}, handle)

    def test_a_normal_age_matches_cache_age(self):
        self._write(1000.0)
        self.assertEqual(usage.cache_reading_age(1600.0, path=self.path),
                         600.0)

    def test_a_future_stamp_is_negative_here_and_zero_through_cache_age(self):
        self._write(2000.0)
        self.assertEqual(usage.cache_reading_age(1000.0, path=self.path),
                         -1000.0)
        self.assertEqual(usage.cache_age(1000.0, path=self.path), 0.0)

    def test_absent_malformed_and_unusable_are_none(self):
        self.assertIsNone(usage.cache_reading_age(
            1000.0, path=os.path.join(self.path, "nope")))
        with open(self.path, "w") as handle:
            handle.write("{ broken")
        self.assertIsNone(usage.cache_reading_age(1000.0, path=self.path))
        for bad in (None, True, "soon", float("nan")):
            self._write(bad)
            self.assertIsNone(usage.cache_reading_age(1000.0,
                                                       path=self.path))


class TestEstimatedSubagentMessage(unittest.TestCase):
    """The no-reading fallback for a dispatched worker: silence there reads as
    headroom, so an estimate at or past notice speaks."""

    NOW = 1787000000.0

    def test_it_says_nothing_below_the_notice_threshold(self):
        self.assertEqual(
            usage.estimated_subagent_message(20, None, self.NOW), "")

    def test_it_warns_at_the_estimate(self):
        text = usage.estimated_subagent_message(88, None, self.NOW)
        self.assertIn("88%", text)
        self.assertIn("BLOCKED", text)
        self.assertIn("ESTIMATED", text)

    def test_it_names_the_window_it_can_see(self):
        text = usage.estimated_subagent_message(99, None, self.NOW)
        self.assertIn("5-hour", text)
        self.assertIn("7-day", text)

    def test_unusable_input_is_an_empty_string_not_a_traceback(self):
        self.assertEqual(usage.estimated_subagent_message(None, None, 0.0), "")
        self.assertEqual(usage.estimated_subagent_message("a", None, 0.0), "")

    def test_a_bool_is_refused(self):
        self.assertEqual(usage.estimated_subagent_message(True, None, 0.0), "")

    def test_it_tells_the_worker_when_the_window_comes_back(self):
        text = usage.estimated_subagent_message(
            99, self.NOW + 3600, self.NOW)
        self.assertIn(usage.format_reset(self.NOW + 3600, self.NOW), text)

    def test_an_absent_reset_prints_no_clause_rather_than_a_question_mark(self):
        text = usage.estimated_subagent_message(99, None, self.NOW)
        self.assertTrue(text)
        self.assertNotIn("?", text)


class TestStatusLineSurface(unittest.TestCase):
    """Which surface renders a status line — all three states.

    Every case patches the environment explicitly. A test that read the ambient
    CLAUDE_CODE_ENTRYPOINT would pass on exactly one surface, which is the
    hermeticity failure this repo bans — and it is not theoretical here: the
    hook suite really did assert a different contract per machine until `run()`
    started stripping the variable.
    """

    def test_a_terminal_is_positively_identified(self):
        """The terminal reports "cli". NOT "claude-cli".

        Read out of the binary's own assignment,
        `c.set("CLAUDE_CODE_ENTRYPOINT", e ? "sdk-cli" : "cli")`, and
        corroborated by the telemetry switch whose default arm is `case "cli"`.
        This constant was `claude-cli` for one commit, taken from a planning
        document that called the list MEASURED — which made the True state
        unreachable in production while this test still passed, because it and
        the code were reading the same wrong string.
        """
        self.assertTrue(usage.status_line_surface(
            {"CLAUDE_CODE_ENTRYPOINT": "cli"}))

    def test_the_string_claude_cli_is_not_an_entrypoint(self):
        """A regression pin for the defect above, asserted from the other side.

        The test above passes for any value both it and the constant agree on,
        so it cannot notice the constant being wrong. This one can: it names the
        wrong value specifically and fails if anyone reinstates it.
        """
        self.assertIsNone(usage.status_line_surface(
            {"CLAUDE_CODE_ENTRYPOINT": "claude-cli"}))

    def test_the_sdk_is_not_a_terminal_even_though_it_ends_in_cli(self):
        """`sdk-cli` shares a suffix with `cli` and has no status line. A
        substring or endswith match would classify it as a terminal."""
        self.assertIs(usage.status_line_surface(
            {"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"}), False)

    def test_vs_code_is_positively_identified_as_having_none(self):
        # assertIs, not assertFalse: the return is Optional[bool] and
        # assertFalse passes for None too, erasing the distinction this whole
        # function exists to draw.
        self.assertIs(usage.status_line_surface(
            {"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}), False)

    def test_the_other_known_surfaces_have_none_either(self):
        """Every non-`cli` arm of the binary's own entrypoint switch.

        Spelled out literally rather than iterated from
        `_NO_STATUS_LINE_ENTRYPOINTS`: a fixture built from the constant under
        test cannot fail for the right reason, and narrowing that constant would
        shrink the fixture in step and stay green.
        """
        for name in ("claude-vscode", "claude-desktop", "claude-desktop-3p",
                     "claude-in-teams", "claude_in_slack", "claude-in-slack",
                     "remote", "remote_baku", "remote_cowork",
                     "remote_desktop", "remote_mobile",
                     "sdk-cli", "sdk-ts", "sdk-py",
                     "mcp", "local-agent", "bench",
                     "claude-code-github-action"):
            self.assertIs(usage.status_line_surface(
                {"CLAUDE_CODE_ENTRYPOINT": name}), False, name)

    def test_the_values_that_were_invented_are_not_recognised(self):
        """Three of the first cut's six values do not exist in the binary at
        all. Pinned so a future edit cannot quietly reintroduce a list nobody
        checked — `claude-code-action` and `claude-code-on-the-web` appear zero
        times, and `claude-code-remote` only as an MCP product id."""
        for name in ("claude-code-action", "claude-code-on-the-web",
                     "claude-code-remote"):
            self.assertIsNone(usage.status_line_surface(
                {"CLAUDE_CODE_ENTRYPOINT": name}), name)

    def test_an_absent_variable_is_cannot_tell_not_no(self):
        self.assertIsNone(usage.status_line_surface({}))

    def test_an_unrecognised_surface_is_cannot_tell_not_no(self):
        """A surface nobody has enumerated must not be read as `False`.

        `False` suppresses the relay. Guessing it for an unknown entrypoint
        would silence the notice on a surface that may well have a status
        line — so the unknown case keeps today's behaviour instead.
        """
        self.assertIsNone(usage.status_line_surface(
            {"CLAUDE_CODE_ENTRYPOINT": "claude-something-new"}))

    def test_an_empty_or_non_string_value_is_cannot_tell(self):
        self.assertIsNone(usage.status_line_surface(
            {"CLAUDE_CODE_ENTRYPOINT": ""}))
        self.assertIsNone(usage.status_line_surface(
            {"CLAUDE_CODE_ENTRYPOINT": 7}))

    def test_the_value_is_matched_case_and_space_insensitively(self):
        self.assertIs(usage.status_line_surface(
            {"CLAUDE_CODE_ENTRYPOINT": " Claude-VSCode "}), False)


class TestUnknownMessageSplitsTruthFromRelay(unittest.TestCase):
    """The truth half is unconditional; only the relay clause is not.

    The standing rule — no reading and a calm reading must never look the
    same — is about the party DECIDING, and Claude is the decider. So the
    Claude-facing half survives on every surface and only the instruction to
    tell a human who cannot act on it is dropped.
    """

    def test_a_no_status_line_surface_keeps_the_truth_and_drops_the_relay(self):
        text = usage.unknown_message(3600.0, surface=False)
        self.assertIn("NOT as headroom", text)
        self.assertNotIn("Say so to", text)

    def test_a_terminal_keeps_both(self):
        text = usage.unknown_message(3600.0, surface=True)
        self.assertIn("NOT as headroom", text)
        self.assertIn("Say so to", text)

    def test_cannot_tell_keeps_both_because_the_relay_is_the_fallback(self):
        text = usage.unknown_message(3600.0, surface=None)
        self.assertIn("NOT as headroom", text)
        self.assertIn("Say so to", text)

    def test_positive_control_every_surface_renders_something(self):
        """Assertion 1 above is satisfied by the empty string.

        `unknown_message` returns "" from its exception handlers, so a change
        that made it raise internally would pass "drops the relay" on every
        surface while emitting no notice at all — the documented false-pass
        shape in this repo. Length, not truthiness: a stray space is truthy.
        """
        for surface in (True, False, None):
            text = usage.unknown_message(3600.0, surface=surface)
            self.assertGreater(len(text.strip()), 200, repr(surface))

    def test_the_unreadable_cause_takes_the_same_split(self):
        """The UNREADABLE branch is a SECOND return path out of the same
        function, and it used to format the template independently. That is
        the shape where one path silently keeps a tail the other drops."""
        text = usage.unknown_message(presence=usage.CACHE_UNREADABLE,
                                     surface=False)
        self.assertIn("NOT as headroom", text)
        self.assertNotIn("Say so to", text)
        self.assertIn("could not be read", text)

    def test_the_anchor_hint_is_withheld_while_the_estimator_is_off(self):
        """`claude-usage-anchor` REFUSES while `usage_watch.estimate` is false —
        it exits 1 saying there is nothing to anchor — and false is the shipped
        default, which is what a fresh install gets. So the notice must not name it.

        `config.example.json` settled this for the dispatch advisory in as many
        words: an advisory naming a command that cannot run is worse than one
        that stays quiet.
        """
        text = usage.unknown_message(3600.0, surface=False, estimate_on=False)
        self.assertNotIn("claude-usage-anchor", text)
        # Positive control: the rest of the structural tail is still there, so
        # this is not passing on an empty or truncated notice.
        self.assertIn("structural rather than a fault", text)

    def test_the_anchor_hint_appears_once_the_estimator_is_on(self):
        text = usage.unknown_message(3600.0, surface=False, estimate_on=True)
        self.assertIn("claude-usage-anchor", text)
        # No ratio ships, so one reading cannot by itself turn a band into a
        # number: the hint says what the second reading is for, and never
        # promises a band the machine has no ratio for.
        self.assertIn("--resets-in", text)
        self.assertIn("at least 5 points higher", text)

    def test_the_hint_is_withheld_for_a_truthy_non_bool(self):
        """`is True`, not truthiness, and this is the property that buys.

        The parameter is fed from `is_estimate_enabled()`, which returns a
        bool — so the "caller cannot say" case this guard was first documented
        as covering does not exist, and a test named for it would describe a
        branch the body never reaches. What `is True` actually rules out is a
        non-bool arriving from a config read: a dict, a non-empty string, a 1.
        Every one of those is truthy and none of them means the estimator is
        on. A mutation to `is not False` survives any test that only ever
        passes real booleans.
        """
        for value in ("false", {"enabled": False}, 1):
            self.assertNotIn("claude-usage-anchor", usage.unknown_message(
                3600.0, surface=False, estimate_on=value), repr(value))

    def test_the_hint_is_withheld_when_the_parameter_is_omitted(self):
        """The default is the stricter case for anything naming a command."""
        self.assertNotIn("claude-usage-anchor",
                         usage.unknown_message(3600.0, surface=False))

    def test_a_relayed_notice_never_carries_the_hint_either(self):
        """The hint belongs to the structural tail only. On a surface that
        keeps the relay, the human is being asked to look at a status line
        that exists."""
        self.assertNotIn("claude-usage-anchor", usage.unknown_message(
            3600.0, surface=True, estimate_on=True))

    def test_an_unpassed_surface_still_renders(self):
        """Every pre-existing caller omits `surface` and must keep working —
        the parameter defaults to resolving itself, not to None."""
        self.assertIn("NOT as headroom", usage.unknown_message(3600.0))


class TestEstimateTierMessage(unittest.TestCase):
    """A tier taken from an estimate: the sentence travels with it, and only
    GATE and HALT carry a work-stopper."""

    HALT_BLOCK = "dispatch nothing"
    GATE_BLOCK = "Do not start the next unit"
    SENTENCE = "Estimated 97% of the 5h window."

    def test_threshold_for_names_the_number_that_fired(self):
        self.assertEqual(usage.threshold_for(usage.TIER_GATE, 70, 85, 95), 85)
        self.assertIsNone(usage.threshold_for(usage.TIER_NONE, 70, 85, 95))
        self.assertIsNone(usage.threshold_for("nonsense", 70, 85, 95))

    def test_halt_blocks_and_carries_the_sentence(self):
        text = usage.estimate_tier_message(
            usage.TIER_HALT, 97, 95, self.SENTENCE)
        self.assertIn(self.HALT_BLOCK, text)
        self.assertIn(self.SENTENCE, text)
        self.assertIn("97%", text)

    def test_gate_blocks(self):
        text = usage.estimate_tier_message(
            usage.TIER_GATE, 88, 85, self.SENTENCE)
        self.assertIn(self.GATE_BLOCK, text)

    def test_notice_does_not_block(self):
        text = usage.estimate_tier_message(
            usage.TIER_NOTICE, 74, 70, self.SENTENCE)
        self.assertNotIn(self.GATE_BLOCK, text)
        self.assertNotIn(self.HALT_BLOCK, text)
        self.assertIn("74%", text)

    def test_every_rendered_tier_names_itself_and_its_threshold(self):
        for tier, label, threshold in ((usage.TIER_NOTICE, "NOTICE", 70),
                                       (usage.TIER_GATE, "GATE", 85),
                                       (usage.TIER_HALT, "HALT", 95)):
            text = usage.estimate_tier_message(
                tier, threshold + 1, threshold, self.SENTENCE)
            self.assertIn(label + " FROM AN ESTIMATE", text)
            self.assertIn(str(threshold) + "%", text)
            self.assertGreater(len(text.strip()), 100, tier)

    def test_no_tier_and_nonsense_render_nothing(self):
        self.assertEqual(usage.estimate_tier_message(
            usage.TIER_NONE, 99, 95, self.SENTENCE), "")
        self.assertEqual(usage.estimate_tier_message(
            "nonsense", 99, 95, self.SENTENCE), "")

    def test_a_sentence_that_cannot_join_renders_nothing(self):
        self.assertEqual(usage.estimate_tier_message(
            usage.TIER_HALT, 97, 95, None), "")


class TestExpiredBucketsArePrunedWithinALiveSession(unittest.TestCase):
    """The prune runs at TWO granularities, and only one of them existed.

    Per-session alone sufficed while every session held a handful of FIXED
    buckets. The dispatch advisory's wave key is not fixed — one bucket per
    user input, plus one per minute of a long dispatching turn — so a session
    that keeps working accumulated buckets for its whole life. Before
    this: a session holding many expired buckets and one live one retained all
    of them, because the filter only asked whether the SESSION was worth keeping.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "state.json")

    def _write(self, sessions):
        with open(self.path, "w") as handle:
            json.dump({"sessions": sessions}, handle)

    def _read(self):
        with open(self.path) as handle:
            return json.load(handle)["sessions"]

    def test_a_live_session_sheds_its_expired_buckets(self):
        self._write({"s1": dict(
            {"old%d" % i: {"resets_at": NOW - 10, "tier": usage.TIER_NOTICE}
             for i in range(100)},
            live={"resets_at": NOW + 3600, "tier": usage.TIER_NOTICE})})
        usage.record_announcement("s1", "fresh", NOW + 3600,
                                  usage.TIER_NOTICE, NOW, path=self.path)
        kept = self._read()["s1"]
        # Positive control: the write landed and the live bucket survived, or
        # "the expired ones are gone" is satisfied by a prune that ate
        # everything — which would be a silence bug, not a fix.
        self.assertIn("fresh", kept)
        self.assertIn("live", kept)
        self.assertEqual([k for k in kept if k.startswith("old")], [],
                         "expired buckets survived inside a live session")

    def test_a_session_with_nothing_live_still_goes_entirely(self):
        """The pre-existing per-session behaviour must survive the finer one."""
        self._write({"dead": {"a": {"resets_at": NOW - 10,
                                    "tier": usage.TIER_NOTICE}}})
        usage.record_announcement("s2", "b", NOW + 3600, usage.TIER_NOTICE,
                                  NOW, path=self.path)
        self.assertNotIn("dead", self._read())

    def test_an_unreadable_record_is_KEPT_not_dropped(self):
        """This prune is housekeeping. A record it cannot parse is not evidence
        that the record is spent, and dropping one would re-announce a tier
        that had already been said — noise, but noise this file exists to
        avoid. Kept alongside a live bucket so the session survives at all."""
        self._write({"s1": {"junk": {"resets_at": "soon"},
                            "live": {"resets_at": NOW + 3600,
                                     "tier": usage.TIER_NOTICE}}})
        usage.record_announcement("s1", "c", NOW + 3600, usage.TIER_NOTICE,
                                  NOW, path=self.path)
        self.assertIn("junk", self._read()["s1"])


class TestShippedDefaults(unittest.TestCase):
    """What ships beside the tool must not stand in for anyone's own numbers.

    config.example.json is copied verbatim on a no-TTY install, so any value
    in it that differs from the code's default becomes a live setting nobody
    chose. Each expected value is the code's own constant, so a default that
    moves without the example (or the reverse) fails here.
    """

    def _section(self):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.realpath(__file__)))))
        with open(os.path.join(repo, "config.example.json"),
                  encoding="utf-8") as handle:
            return json.load(handle)["usage_watch"]

    def test_example_config_values_equal_the_code_defaults(self):
        import estimate_lib as est
        values = {k: v for k, v in self._section().items()
                  if not k.startswith("//")}
        expected = {
            "enabled": True, "subagent_context": True,
            "postmortem_handover": True,
            "estimate": False, "dispatch_advisory": False,
            "notice_pct": usage.DEFAULT_NOTICE_PCT,
            "gate_pct": usage.DEFAULT_GATE_PCT,
            "halt_pct": usage.DEFAULT_HALT_PCT,
            "cache_path": usage.DEFAULT_CACHE_PATH,
            "state_path": usage.DEFAULT_STATE_PATH,
            "stale_after_seconds": usage.DEFAULT_STALE_AFTER_SECONDS,
            "unknown_reannounce_seconds": usage.DEFAULT_UNKNOWN_REANNOUNCE_SECONDS,
            "transcript_root": est.DEFAULT_TRANSCRIPT_ROOT,
            "anchor_path": est.DEFAULT_ANCHOR_PATH,
            # claude-usage-watch's literal threshold when the key is absent.
            "codex_low_gates": 15,
            # No ratio ships: null, which estimate_lib reads as no ratio.
            "default_ratio": None,
        }
        self.assertEqual(expected, values)

    def test_no_tokens_per_percent_ratio_ships(self):
        """The one key with no default at all: documented, never valued.
        Null rather than absent, so `--set usage_watch.default_ratio=N` can
        still find the key it validates against."""
        section = self._section()
        self.assertIsNone(section["default_ratio"])
        self.assertIn("NO DEFAULT SHIPS", section["//default_ratio"])
        # The setter takes integers; the note must not promise more.
        self.assertIn("integers only", section["//default_ratio"])

    def test_the_measurement_record_describes_a_persisting_ratio(self):
        """The estimator keeps a measured ratio across windows (only the
        level re-anchors), and the method record must say the same."""
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.realpath(__file__)))))
        with open(os.path.join(repo, "docs", "build-measurements.md"),
                  encoding="utf-8") as handle:
            text = " ".join(handle.read().split())
        self.assertIn("a ratio measured on your machine carries across windows",
                      text)
        self.assertNotIn("it re-anchors each window", text)

    def test_the_ratio_can_be_set_and_a_bad_one_is_refused(self):
        """The documented route to a configured ratio must work: a null
        example value types nothing by itself, so `--set` has to be told the
        key is an integer."""
        repo = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.realpath(__file__)))))
        tmp = tempfile.mkdtemp()
        config = os.path.join(tmp, "config.json")
        with open(config, "w") as handle:
            handle.write("{}")

        def set_ratio(value):
            return subprocess.run(
                [sys.executable, os.path.join(repo, "lib", "setup_config.py"),
                 config, repo, "--update",
                 "--set", "usage_watch.default_ratio=%s" % value],
                capture_output=True, text=True, stdin=subprocess.DEVNULL)

        for bad in ("true", "0", "1.5", '"20000"'):
            self.assertNotEqual(0, set_ratio(bad).returncode, bad)
        done = set_ratio("20000")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        with open(config) as handle:
            self.assertEqual(20000, json.load(handle)["usage_watch"]["default_ratio"])


if __name__ == "__main__":
    unittest.main()
