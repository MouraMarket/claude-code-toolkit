"""The dispatch advisory's decision rule. PURE: no file I/O, no clock.

`PreToolUse` fires before a dispatch RUNS, not before the model decides, so
below halt this advisory informs the NEXT decision. Only `permissionDecision`
can change the call the hook fires on, and this module sets it only at halt.

The percentage comes from the binding real reading when there is one, else from
an anchored estimate. An estimate counts this machine only, so it is a floor.
Every exception propagates: the executable reports a crash rather than guessing.
"""
import collections

MIN_SAMPLES = 5

# Every advisory names the window it can see: the 5-hour window can sit at 30%
# while the 7-day sits past 80%.
WINDOW_CAVEAT = ("This is the 5-hour window only — the 7-day window is "
                 "invisible to this advisory and can be the binding one.")

# Restated from estimate_lib (this module imports nothing). The mechanism, that
# other devices spend the same window, is what must survive any rewording.
FLOOR_CAVEAT = ("Counts this machine only — claude.ai and other devices spend "
                "the same window — so read it as a floor.")

Verdict = collections.namedtuple(
    "Verdict", "context system_message permission_decision reason")

# The same tuple as cost_lib.PLUGIN_AGENT_PREFIXES, restated because this module
# imports nothing; a test pins the two equal.
PLUGIN_AGENT_PREFIXES = ("toolkit-build:",)


def lane_for_dispatch(tool_name, subagent_type, lane_by_agent_type):
    """The lane this dispatch will be RECORDED under, so the forecast prices it
    against the right history.

    `lane_by_agent_type` is `cost_lib.LANE_BY_AGENT_TYPE`, passed in rather than
    copied. A Workflow is a review lane. Anything unrecognised is `other`,
    never `executor`, as in `cost_lib.lane_for_agent_file`.
    """
    try:
        if tool_name == "Workflow":
            return "review"
        if isinstance(lane_by_agent_type, dict) and isinstance(subagent_type, str):
            # Only the toolkit-build plugin's prefix is stripped; another
            # plugin's `executor` is not this toolkit's executor.
            bare = subagent_type
            for prefix in PLUGIN_AGENT_PREFIXES:
                if subagent_type.startswith(prefix):
                    bare = subagent_type[len(prefix):]
                    break
            return lane_by_agent_type.get(bare, "other")
    except Exception:
        pass
    return "other"


def _session_caveat(lane):
    """How to read a per-session median for THIS lane. A ledger row is one
    session: it over-states one Agent dispatch, but a review dispatch is often
    the whole gate, so the review figure is mid-range rather than a ceiling."""
    if lane == "review":
        return ("recorded SESSIONS; a review dispatch is often the whole gate, "
                "so read this as a mid-range figure rather than a ceiling")
    return "recorded SESSIONS, so it over-states a single dispatch"


def _sample_count(lane, lane_stats):
    try:
        return int(lane_stats[lane][1])
    except Exception:
        return 0


def _expected_points(lane, lane_stats, ratio):
    """(points, median_tokens, n) for a lane with enough history, else None.

    A missing ratio is no forecast, never a number.
    """
    try:
        median, count = lane_stats[lane]
        count = int(count)
        if count < MIN_SAMPLES:
            return None
        ratio = float(ratio)
        if ratio <= 0:
            return None
        return (int(round(float(median) / ratio)), float(median), count)
    except Exception:
        return None


def _cost_clause(lane, lane_stats, ratio, ratio_source, headroom):
    expected = _expected_points(lane, lane_stats, ratio)
    if expected is None:
        if ratio is None:
            return "No tokens-per-percent ratio, so no expected cost in points."
        return ("No cost history for the {0} lane yet ({1} of {2} records "
                "needed).".format(lane, _sample_count(lane, lane_stats),
                                  MIN_SAMPLES))
    points, median, count = expected
    fit = ("That fits: {0} <= {1}." if points <= headroom
           else "That does not fit: {0} > {1}.").format(points, headroom)
    clause = ("A {0} dispatch has historically cost about {1} points (median "
              "{2:,.0f} output tokens across n={3} {4}). {5}".format(
                  lane, points, median, count, _session_caveat(lane), fit))
    if ratio_source == "configured":
        clause += " The points use the configured usage_watch.default_ratio."
    return clause


def verdict(*, pct, source, bucket, lane, lane_stats, ratio, ratio_source,
            thresholds):
    """The advisory for one dispatch. `pct is None` is silence; at or past
    halt it also asks. `source` is "reading" or "estimate"."""
    if pct is None:
        return Verdict("", "", None, "")
    notice, gate, halt = (int(value) for value in thresholds)
    kind = "reading" if source == "reading" else "estimate"
    headroom = max(0, 100 - int(round(pct)))
    context = ("USAGE ({0} window, {1}): {2:.0f}%, about {3} points of "
               "headroom. ".format(bucket, kind, pct, headroom))
    context += _cost_clause(lane, lane_stats, ratio, ratio_source, headroom)
    if kind == "estimate":
        context += " " + FLOOR_CAVEAT + " " + WINDOW_CAVEAT
    if pct >= halt:
        return Verdict(
            context,
            "USAGE HALT ({0}): the {1} window is at {2:.0f}%, past the {3}% "
            "halt threshold. Nothing new should be dispatched.".format(
                kind, bucket, pct, halt),
            "ask",
            "{0} usage is {1:.0f}% ({2}), past the {3}% halt threshold.".format(
                bucket, pct, kind, halt))
    return Verdict(context, "", None, "")
