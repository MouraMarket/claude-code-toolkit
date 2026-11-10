#!/usr/bin/env python3
"""The dispatch advisory's decision rule.

Pure by design: no temp directory, no transcripts, no clock. The executable has
its own subprocess tests in test_hooks.py.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import dispatch_lib as dispatch  # noqa: E402

THRESHOLDS = (70, 85, 95)
RATIO = 20000.0

# 200,000 tokens is exactly 10 points at RATIO.
RICH = {"review": (200000.0, 7), "executor": (100000.0, 6)}


def call(**overrides):
    kwargs = dict(pct=40, source="reading", bucket="5h", lane="review",
                  lane_stats=RICH, ratio=RATIO, ratio_source="measured",
                  thresholds=THRESHOLDS)
    kwargs.update(overrides)
    return dispatch.verdict(**kwargs)


class TestSilence(unittest.TestCase):
    def test_no_percentage_is_silent(self):
        got = call(pct=None, source=None, bucket=None)
        self.assertEqual(got, dispatch.Verdict("", "", None, ""))


class TestHalt(unittest.TestCase):
    def test_a_96_reading_asks_with_a_system_message(self):
        got = call(pct=96.0, bucket="5h")
        self.assertEqual(got.permission_decision, "ask")
        self.assertIn("5h usage is 96% (reading), past the 95% halt",
                      got.reason)
        self.assertIn("USAGE HALT (reading)", got.system_message)
        self.assertIn("5h window is at 96%", got.system_message)

    def test_halt_is_inclusive(self):
        self.assertEqual(call(pct=95.0).permission_decision, "ask")

    def test_a_94_reading_does_not_ask(self):
        got = call(pct=94.0)
        self.assertIsNone(got.permission_decision)
        self.assertEqual(got.system_message, "")
        self.assertEqual(got.reason, "")
        self.assertIn("94%", got.context)

    def test_an_estimate_at_95_asks_and_says_it_is_an_estimate(self):
        got = call(pct=95, source="estimate")
        self.assertEqual(got.permission_decision, "ask")
        self.assertIn("USAGE HALT (estimate)", got.system_message)
        self.assertIn("(estimate)", got.reason)

    def test_the_bucket_is_named(self):
        got = call(pct=97.0, bucket="7d")
        self.assertIn("7d usage is 97%", got.reason)
        self.assertIn("7d window", got.context)

    def test_the_halt_threshold_comes_from_the_caller(self):
        self.assertEqual(
            call(pct=90.0, thresholds=(50, 70, 90)).permission_decision, "ask")


class TestCaveats(unittest.TestCase):
    def test_an_estimate_carries_both_caveats(self):
        got = call(pct=60, source="estimate")
        self.assertIn(dispatch.FLOOR_CAVEAT, got.context)
        self.assertIn(dispatch.WINDOW_CAVEAT, got.context)
        self.assertIn("(5h window, estimate)", got.context)

    def test_a_reading_carries_neither(self):
        got = call(pct=60, source="reading")
        self.assertNotIn(dispatch.FLOOR_CAVEAT, got.context)
        self.assertNotIn(dispatch.WINDOW_CAVEAT, got.context)
        self.assertIn("(5h window, reading)", got.context)


class TestForecast(unittest.TestCase):
    def test_five_samples_forecast(self):
        got = call(lane_stats={"review": (200000.0, 5)})
        self.assertIn("about 10 points (median 200,000 output tokens "
                      "across n=5", got.context)

    def test_four_samples_do_not(self):
        got = call(lane_stats={"review": (200000.0, 4)})
        self.assertIn("No cost history for the review lane yet "
                      "(4 of 5 records needed).", got.context)
        self.assertNotIn("historically cost", got.context)

    def test_no_lane_entry_is_zero_of_five(self):
        got = call(lane="other")
        self.assertIn("(0 of 5 records needed)", got.context)

    def test_it_fits(self):
        got = call(pct=60)
        self.assertIn("That fits: 10 <= 40.", got.context)

    def test_it_fits_exactly(self):
        self.assertIn("That fits: 10 <= 10.", call(pct=90).context)

    def test_it_does_not_fit(self):
        got = call(pct=93)
        self.assertIn("That does not fit: 10 > 7.", got.context)
        self.assertIsNone(got.permission_decision)

    def test_a_forecast_never_blocks_below_halt(self):
        self.assertIsNone(call(pct=94, lane="executor").permission_decision)

    def test_headroom_is_floored_at_zero(self):
        self.assertIn("about 0 points of headroom", call(pct=100).context)

    def test_no_ratio_means_no_expected_cost(self):
        got = call(ratio=None, ratio_source="none")
        self.assertIn("no expected cost in points", got.context)
        self.assertNotIn("historically cost", got.context)

    def test_a_configured_ratio_is_named(self):
        got = call(ratio_source="configured")
        self.assertIn("configured usage_watch.default_ratio", got.context)

    def test_a_measured_ratio_is_not_called_configured(self):
        self.assertNotIn("configured", call().context)

    def test_the_review_lane_reads_as_mid_range(self):
        self.assertIn("mid-range", call(lane="review").context)
        self.assertIn("over-states a single dispatch",
                      call(lane="executor").context)

    def test_a_malformed_threshold_raises(self):
        with self.assertRaises(Exception):
            call(thresholds=None)


class TestLaneForDispatch(unittest.TestCase):
    """The lane a dispatch will be recorded under, resolved from the SAME
    table cost_lib uses to classify it afterwards — imported, never copied.
    Two tables would disagree the first time an agent type was added, and the
    forecast would price a dispatch against a lane it never lands in."""

    TABLE = {"executor": "executor", "code-reviewer": "review",
             "task-reviewer": "review", "general-purpose": "other"}

    def test_a_workflow_is_a_review_lane(self):
        self.assertEqual(
            dispatch.lane_for_dispatch("Workflow", None, self.TABLE), "review")

    def test_an_agent_resolves_through_the_table(self):
        self.assertEqual(
            dispatch.lane_for_dispatch("Agent", "executor", self.TABLE),
            "executor")
        self.assertEqual(
            dispatch.lane_for_dispatch("Agent", "code-reviewer", self.TABLE),
            "review")

    def test_a_toolkit_build_agent_lanes_as_its_bare_name(self):
        self.assertEqual(
            dispatch.lane_for_dispatch("Agent", "toolkit-build:executor", self.TABLE),
            "executor")
        self.assertEqual(
            dispatch.lane_for_dispatch("Agent", "toolkit-build:code-reviewer", self.TABLE),
            "review")
        self.assertEqual(
            dispatch.lane_for_dispatch("Agent", "toolkit-build:brand-new", self.TABLE),
            "other")

    def test_the_prefixes_are_cost_libs_prefixes(self):
        """Restated because dispatch_lib imports nothing; the recorder and the
        forecast must strip the same prefixes or a dispatch is priced against a
        lane it is never recorded under."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.realpath(__file__)))), "build-cost"))
        import cost_lib
        self.assertEqual(dispatch.PLUGIN_AGENT_PREFIXES,
                         cost_lib.PLUGIN_AGENT_PREFIXES)

    def test_another_plugins_agent_is_other_not_executor(self):
        """Only `toolkit-build:` is stripped, as in cost_lib.bare_agent_type."""
        self.assertEqual(
            dispatch.lane_for_dispatch("Agent", "otherplugin:executor", self.TABLE),
            "other")

    def test_an_unknown_agent_type_is_other_not_executor(self):
        """Same rule as cost_lib.lane_for_agent_file: a new agent type in a
        future release must not be silently priced as executor work."""
        self.assertEqual(
            dispatch.lane_for_dispatch("Agent", "brand-new-thing", self.TABLE),
            "other")
        self.assertEqual(
            dispatch.lane_for_dispatch("Agent", None, self.TABLE), "other")

    def test_an_unusable_table_still_answers(self):
        self.assertEqual(dispatch.lane_for_dispatch("Agent", "executor", None),
                         "other")


class TestFloorCaveatCarriesTheMechanism(unittest.TestCase):
    """`dispatch_lib.FLOOR_CAVEAT` is a deliberate restating of
    `estimate_lib.FLOOR_CAVEAT`, and the two had drifted at birth: this module's
    version dropped WHY the number is a floor and substituted provenance the
    surrounding sentence already carried.

    The mechanism is the load-bearing half. "Other devices spend the same
    window" is what tells a reader which direction the number is wrong in;
    without it a floor reads as a reading. Pinned on the shared PHRASE rather
    than on string equality, so either sentence can be reworded for its own
    surface — which is the whole reason there are two constants — without the
    reason going missing from one of them.
    """

    PHRASE = "spend the same window"

    def _estimate_lib(self):
        import estimate_lib
        return estimate_lib

    def test_the_dispatch_advisory_says_why_it_is_a_floor(self):
        self.assertIn(self.PHRASE, dispatch.FLOOR_CAVEAT)
        self.assertIn("floor", dispatch.FLOOR_CAVEAT)

    def test_the_estimator_it_restates_says_the_same_thing(self):
        """The other side of the pair. Without this, the assertion above keeps
        passing after the ORIGINAL loses the mechanism, and the constant this
        one exists to stay in step with is the one that drifted."""
        self.assertIn(self.PHRASE, self._estimate_lib().FLOOR_CAVEAT)


if __name__ == "__main__":
    unittest.main()
