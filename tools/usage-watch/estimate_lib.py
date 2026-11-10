"""Estimate the 5-hour budget from transcript tokens when no reading exists.

`rate_limits` reaches only the terminal status line, so every other surface
can say UNKNOWN and nothing more. This module counts what CAN be seen on every
surface, the per-response `usage` records in transcript JSONL, and reports a
DRIFT from the last real reading (an anchor), not an absolute figure. A fixed
tokens-per-percent ratio varies by plan and workload, so an absolute estimate
is bounded by nothing; a drift is bounded by its own size.

Every anchor carries its window's `resets_at`, so no window phase is inferred.
The count sees one machine while the window is account-wide, so the figure is
a floor (see FLOOR_CAVEAT). A ratio and `scan_usage` are a matched pair: a
measured ratio is fitted to this reader's counting, so re-derive, never rescale.
"""
import collections
import datetime
import glob
import json
import math
import os
import tempfile
import time
from typing import List, Optional, Tuple

FIVE_HOURS = 5 * 3600
DEFAULT_TRANSCRIPT_ROOT = "~/.claude/projects"
# Main transcripts, and subagent/workflow transcripts (a majority of the corpus
# during a /build). The `agent-*.jsonl` shape is load-bearing: each has a
# sibling agent-<id>.meta.json that a bare `agent-*` would read as a transcript.
TRANSCRIPT_GLOBS = (
    ("*", "*.jsonl"),
    ("*", "*", "subagents", "**", "agent-*.jsonl"),
)

# SCAN_TIME_BUDGET is tied to claude-usage-watch's 5s hook timeout in
# plugin-continuity/hooks/hooks.json; TestScanTimeBudget pins the pair.
HOOK_TIMEOUT_SECONDS = 5.0
SCAN_FILE_BUDGET = 400
SCAN_TIME_BUDGET = 2.0

# The time budget is re-checked inside a file (one transcript can exceed it).
SCAN_TIME_CHECK_EVERY = 2000

# Under 5 points apart, two whole-percent readings mostly measure rounding.
RATIO_MIN_PCT_DELTA = 5.0
# `resets_at` values this close name the same window (windows are 5h apart).
SAME_WINDOW_TOLERANCE = 1800

# A reading older than this is not paired with a token count taken now.
ANCHOR_MAX_READING_AGE = 120.0

DEFAULT_ANCHOR_PATH = "~/.claude/usage-anchors.json"

RATIO_MEASURED, RATIO_CONFIGURED, RATIO_NONE = "measured", "configured", "none"

FLOOR_CAVEAT = ("Counts this machine only — claude.ai and other devices spend "
                "the same window — so read it as a floor.")

_ISO_FORMATS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ")

Estimate = collections.namedtuple(
    "Estimate",
    "pct tokens resets_at anchor_pct anchor_age ratio ratio_source truncated")


def config_value(key: str, default: str) -> str:
    """A `usage_watch` path from config, expanded; `default` on any failure."""
    try:
        import usage_lib
        config = getattr(usage_lib, "_config", None)
        if config is not None:
            configured = config.get_path("usage_watch", key, default=default)
            return os.path.expanduser(configured or default)
    except Exception:
        pass
    return os.path.expanduser(default)


def transcript_root() -> str:
    """Where Claude Code writes transcript JSONL (configurable for tests)."""
    return config_value("transcript_root", DEFAULT_TRANSCRIPT_ROOT)


def anchor_path() -> str:
    """Where anchors live (a file of their own, apart from announcements)."""
    return config_value("anchor_path", DEFAULT_ANCHOR_PATH)


def default_ratio() -> Optional[float]:
    """The configured tokens-per-1%, `usage_watch.default_ratio`, or None.

    Nothing ships: the figure depends on the plan and account. Absent, boolean,
    non-numeric, non-finite or non-positive all mean None.
    """
    try:
        import usage_lib
        config = getattr(usage_lib, "_config", None)
        if config is not None:
            value = config.get("usage_watch", "default_ratio", default=None)
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)):
                value = float(value)
                if math.isfinite(value) and value > 0:
                    return value
    except Exception:
        pass
    return None


def is_estimate_enabled() -> bool:
    """True ONLY when `usage_watch.estimate` is explicitly true; absent is off."""
    try:
        import usage_lib
        config = getattr(usage_lib, "_config", None)
        if config is None:
            return False
        return config.get("usage_watch", "estimate", default=False) is True
    except Exception:
        return False


def parse_timestamp(text) -> Optional[float]:
    """A transcript's ISO-8601 UTC stamp as epoch seconds, or None. Never raises."""
    if not isinstance(text, str):
        return None
    for fmt in _ISO_FORMATS:
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=datetime.timezone.utc).timestamp()
    return None


def scan_usage(root, since: float, now: float,
               file_budget: int = SCAN_FILE_BUDGET,
               time_budget: float = SCAN_TIME_BUDGET
               ) -> Tuple[List[Tuple[float, int]], bool]:
    """Every `(timestamp, output_tokens)` in `[since, now]`, time-sorted.

    One record per API response, not per JSONL line: entries are grouped by
    `(path, message.id)` and reduced with a per-field max (a message's `usage`
    repeats on every content block). A group's stamp is its earliest. Returns
    `(records, truncated)`; truncated means a budget stopped the scan, so the
    total is a floor. Files older than `since` are skipped by mtime.
    """
    started = time.time()
    truncated = False
    try:
        base = os.path.expanduser(root)
        paths = []
        for parts in TRANSCRIPT_GLOBS:
            paths.extend(glob.glob(os.path.join(base, *parts), recursive=True))
        paths = list(dict.fromkeys(paths))
    except Exception:
        return ([], False)

    fresh = []
    for path in paths:
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime >= since:
            fresh.append((mtime, path))
    fresh.sort(reverse=True)
    if len(fresh) > file_budget:
        fresh = fresh[:file_budget]
        truncated = True

    groups = {}
    for _mtime, path in fresh:
        if time.time() - started > time_budget:
            truncated = True
            break
        try:
            with open(path, encoding="utf-8") as handle:
                for lineno, line in enumerate(handle):
                    if lineno % SCAN_TIME_CHECK_EVERY == 0 and lineno:
                        if time.time() - started > time_budget:
                            truncated = True
                            break
                    if '"usage"' not in line:
                        continue
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    stamp = parse_timestamp(entry.get("timestamp"))
                    # `now` is an enforced upper bound (future records dropped).
                    if stamp is None or stamp < since or stamp > now:
                        continue
                    message = entry.get("message")
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if not isinstance(usage, dict):
                        continue
                    out = usage.get("output_tokens")
                    if isinstance(out, bool) or not isinstance(out, (int, float)):
                        continue
                    out = int(out)
                    message_id = message.get("id")
                    if isinstance(message_id, str) and message_id:
                        key = (path, message_id)
                    else:
                        # No usable id: a key of its own counts it once.
                        key = (path, "\0line-%d" % lineno)
                    group = groups.get(key)
                    if group is None:
                        groups[key] = [stamp, out]
                    else:
                        if stamp < group[0]:
                            group[0] = stamp
                        if out > group[1]:
                            group[1] = out
        except (IOError, OSError):
            continue

    records = [(stamp, out) for stamp, out in groups.values()]
    records.sort()
    return (records, truncated)



def load_store(path: Optional[str] = None) -> dict:
    """The anchor store, or {} for absent/malformed. Never raises."""
    target = path if path is not None else anchor_path()
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_store(store: dict, path: Optional[str] = None) -> bool:
    """Persist atomically. False on any failure; never raises."""
    target = path if path is not None else anchor_path()
    try:
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".usage-anchors-")
    except Exception:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(store, handle)
        os.replace(tmp_path, target)
        return True
    except Exception:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        return False


def _number(value) -> bool:
    """A finite int or float; never a bool."""
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _valid_entry(entry) -> bool:
    """A dict with finite numeric pct (0..100), at and resets_at; no bools."""
    if not isinstance(entry, dict):
        return False
    if not all(_number(entry.get(key)) for key in ("pct", "at", "resets_at")):
        return False
    return 0 <= entry["pct"] <= 100


def same_window(a, b) -> bool:
    """Both entries valid and their `resets_at` within SAME_WINDOW_TOLERANCE."""
    return (_valid_entry(a) and _valid_entry(b)
            and abs(a["resets_at"] - b["resets_at"]) <= SAME_WINDOW_TOLERANCE)


def live_anchor(store, now: float) -> Optional[dict]:
    """The stored anchor while valid and its window has not reset, else None."""
    anchor = store.get("anchor") if isinstance(store, dict) else None
    if _valid_entry(anchor) and anchor["resets_at"] > now:
        return anchor
    return None


def derive_ratio(store) -> Tuple[Optional[float], str]:
    """`(tokens_per_percent, source)`: measured, else configured, else none."""
    stored = store.get("ratio") if isinstance(store, dict) else None
    if isinstance(stored, dict) and stored.get("source") == RATIO_MEASURED:
        value = stored.get("value")
        if _number(value) and value > 0:
            return (float(value), RATIO_MEASURED)
    configured = default_ratio()
    if configured is not None:
        return (configured, RATIO_CONFIGURED)
    return (None, RATIO_NONE)


def _first_for(store: dict, entry: dict) -> Optional[dict]:
    """The stored `first` if `entry` continues its window upward, else None."""
    first = store.get("first")
    if same_window(first, entry) and first["pct"] <= entry["pct"]:
        return first
    return None


def record_anchor(store: dict, pct, at, resets_at, tokens_between) -> dict:
    """A new store with `pct` as the anchor. Pure.

    `first` is the window's earliest reading and the ratio's base. A second
    reading RATIO_MIN_PCT_DELTA or more above it, with a positive
    `tokens_between`, measures the ratio. An invalid reading changes nothing.
    """
    store = store if isinstance(store, dict) else {}
    entry = {"pct": pct, "at": at, "resets_at": resets_at}
    if not _valid_entry(entry):
        return dict(store)
    entry = {key: float(value) for key, value in entry.items()}
    first = _first_for(store, entry)
    new = {"anchor": entry, "first": first or entry}
    if isinstance(store.get("ratio"), dict):
        new["ratio"] = store["ratio"]
    if (first is not None and isinstance(tokens_between, int)
            and not isinstance(tokens_between, bool) and tokens_between > 0
            and entry["pct"] - first["pct"] >= RATIO_MIN_PCT_DELTA):
        new["ratio"] = {
            "value": tokens_between / (entry["pct"] - first["pct"]),
            "source": RATIO_MEASURED, "derived_at": entry["at"]}
    return new


def write_anchor(now: float, pct, resets_at, reading_age,
                 root: Optional[str] = None,
                 path: Optional[str] = None) -> Tuple[bool, str]:
    """Record a real reading as the anchor: `(ok, reason)`. Never raises.

    Scans only when a ratio could be measured; unchanged costs no scan or write.
    """
    try:
        if not is_estimate_enabled():
            return (False, "the estimator is off (usage_watch.estimate)")
        if not _number(pct) or not 0 <= pct <= 100:
            return (False, "the percentage must be a number from 0 to 100")
        if (not _number(resets_at) or resets_at <= now
                or resets_at > now + FIVE_HOURS + 60):
            return (False, "the reset time must be within the next 5 hours")
        if not _number(reading_age) or not 0 <= reading_age <= ANCHOR_MAX_READING_AGE:
            return (False, "the reading is too old to pair with a token count")
        at = now - reading_age
        target = path if path is not None else anchor_path()
        store = load_store(target)
        entry = {"pct": float(pct), "at": at, "resets_at": float(resets_at)}
        stored = store.get("anchor")
        if same_window(stored, entry) and stored["pct"] == entry["pct"]:
            return (True, "unchanged")
        first, tokens_between = _first_for(store, entry), None
        if first is not None and entry["pct"] - first["pct"] >= RATIO_MIN_PCT_DELTA:
            records, truncated = scan_usage(root or transcript_root(),
                                            first["at"], now)
            if not truncated:
                tokens_between = sum(out for stamp, out in records
                                     if first["at"] < stamp <= at)
        updated = record_anchor(store, pct, at, resets_at, tokens_between)
        if not save_store(updated, target):
            return (False, "could not write %s" % target)
        return (True, "anchored")
    except Exception as exc:
        return (False, "anchoring failed: %s" % exc)


def estimate(now: float, store: dict, records,
             truncated: bool = False) -> Optional[Estimate]:
    """Anchor % plus tokens since the anchor over the ratio, capped at 100.

    Pure. None without a live anchor; `pct` is None without a ratio.
    """
    anchor = live_anchor(store, now)
    if anchor is None:
        return None
    try:
        tokens = sum(out for stamp, out in records if stamp > anchor["at"])
    except (TypeError, ValueError):
        return None
    ratio, source = derive_ratio(store)
    pct = None
    if ratio is not None:
        pct = min(100, int(round(anchor["pct"] + tokens / ratio)))
    return Estimate(pct, tokens, anchor["resets_at"], anchor["pct"],
                    now - anchor["at"], ratio, source, bool(truncated))


def current_estimate(now: float, root: Optional[str] = None,
                     path: Optional[str] = None) -> Optional[Estimate]:
    """The estimate from the stored anchor and a fresh scan. Never raises."""
    try:
        if not is_estimate_enabled():
            return None
        store = load_store(path)
        anchor = live_anchor(store, now)
        base = os.path.expanduser(root or transcript_root())
        if anchor is None or not os.path.isdir(base):
            return None
        records, truncated = scan_usage(base, anchor["at"], now)
        return estimate(now, store, records, truncated)
    except Exception:
        return None


def render_age(seconds) -> str:
    """`40m` (floored minutes) or `1.5h`; "" for a negative or non-number."""
    if not _number(seconds) or seconds < 0:
        return ""
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    return ("%.1f" % (seconds / 3600.0)).replace(".0", "") + "h"


def estimate_sentence(est: Optional[Estimate], now: float) -> str:
    """One sentence describing the estimate, always a floor; "" for None."""
    if est is None:
        return ""
    age = render_age(est.anchor_age)
    if est.pct is not None:
        conf = (", using the configured usage_watch.default_ratio"
                if est.ratio_source == RATIO_CONFIGURED else "")
        text = ("Estimated %d%% of the 5h window (anchored at %.0f%% %s ago, "
                "plus %s output tokens since)%s. %s"
                % (est.pct, est.anchor_pct, age, format(est.tokens, ","),
                   conf, FLOOR_CAVEAT))
    else:
        text = ("No percentage yet: %s output tokens since the %.0f%% anchor "
                "%s ago, and no tokens-per-percent ratio. Anchor again once "
                "the panel has moved 5+ points this window, or set "
                "usage_watch.default_ratio. %s"
                % (format(est.tokens, ","), est.anchor_pct, age, FLOOR_CAVEAT))
    if est.truncated:
        text += (" The transcript scan hit its budget, so the token count "
                 "is partial.")
    return text


def unanchored_sentence() -> str:
    """The notice when no live anchor exists."""
    return ("No live anchor for this 5-hour window. Run `claude-usage-anchor "
            "<pct> --resets-in <e.g. 4h20m>` with the 5h figure and its reset "
            "time from the Account & Usage panel; a terminal status line "
            "records one automatically.")
