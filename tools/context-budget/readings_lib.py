"""The instruction meter's reading log: append, load, rate, forecast.

R2 needs a growth RATE, and a rate needs history a single scan cannot supply --
this is that history. One JSON object per line, one line per day per
workspace root, written by `claude-context-budget --record` (a SessionStart
hook) and read back by `--report`.

Pure except for the log file itself (`append_reading`/`load` do file I/O;
everything else here is arithmetic over what they return). `today`/`now` are
always parameters, never read inside a function -- that is what lets the rate
and forecast tests use fixed clocks instead of the real one.

Must NOT import `budget_lib`: `budget_lib` imports this module, and
the reverse import would be circular.
"""

import datetime
import json
import os

import instruction_lib

DEFAULT_READINGS_PATH = "~/.claude/context-budget/readings.jsonl"


def _is_valid_int(value):
    """int, not bool -- `isinstance(True, int)` is True in Python and would
    silently accept a boolean where the log expects a count."""
    return isinstance(value, int) and not isinstance(value, bool)


def readings_path(config):
    """Where the log lives: `context_budget.readings_path`, `~`-expanded.

    `config` is the `toolkit_config` module (or a stand-in exposing
    `get_path`), or None. `get_path`'s own default is returned UNEXPANDED when
    the key is absent, so the `~` in DEFAULT_READINGS_PATH still needs
    expanding here either way.
    """
    if config is not None:
        try:
            value = config.get_path(
                "context_budget", "readings_path", default=DEFAULT_READINGS_PATH
            )
            return os.path.expanduser(value)
        except Exception:
            pass
    return os.path.expanduser(DEFAULT_READINGS_PATH)


def has_reading(path, date_str, v, root):
    """Whether the log already holds a USABLE line for this (date, v, root).

    Judged through `load`, the same validation the report reads with. Only a
    line `load` would count suppresses a second write. A line carrying today's
    date, version and root but no `surfaces` list used to suppress the write
    while `load` discarded it, so `--record` silently no-oped and the day
    vanished from the series. A line with a malformed surface FIELD still
    loads (the field reads as None), so it still suppresses, as before. A
    second valid line for the same date is harmless: `load` keeps the last.
    """
    return any(reading["date"].isoformat() == date_str
               for reading in load(path, root, v))


def append_reading(path, reading):
    """One line, one `write()`. Never raises; False on any error."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(reading, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def load(path, root, v):
    """Every parseable, validated line for this (root, v), deduped by date
    (last line wins), sorted by date. Missing file -> []. See the module and
    design (4.3) for the per-line and per-surface validation this applies.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except Exception:
        return []

    by_date = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("v") != v or obj.get("workspace_root") != root:
            continue

        date_raw = obj.get("date")
        if not isinstance(date_raw, str):
            continue
        try:
            date_value = datetime.date.fromisoformat(date_raw)
        except Exception:
            continue

        surfaces_raw = obj.get("surfaces")
        if not isinstance(surfaces_raw, list):
            continue
        surfaces = []
        for item in surfaces_raw:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                continue
            surface = dict(item)
            for field in ("instructions", "value", "cap"):
                value = surface.get(field)
                if value is None:
                    continue  # None is a valid state on its own
                if not _is_valid_int(value):
                    surface[field] = None
                    surface["ok"] = False
            surfaces.append(surface)

        total = obj.get("always_on_total")
        if not _is_valid_int(total):
            total = None

        reading = dict(obj)
        reading["date"] = date_value
        reading["surfaces"] = surfaces
        reading["always_on_total"] = total
        by_date[date_value] = reading  # last line for this date wins

    return [by_date[d] for d in sorted(by_date)]


def build_reading(records, total, root, now):
    """Design 4.3's shape. `total` is the caller's own
    `budget_lib.always_on_total(records)` -- computed there, not here, so this
    module never has to import `budget_lib`."""
    return {
        "v": instruction_lib.UNIT_VERSION,
        "date": now.date().isoformat(),
        "ts": now.isoformat(timespec="seconds"),
        "workspace_root": root,
        "surfaces": records,
        "always_on_total": total,
    }


def series(readings, key, field):
    """[(date, number)] for one surface's `field`, `ok` readings only."""
    points = []
    for reading in readings:
        for surface in reading.get("surfaces") or []:
            if surface.get("key") != key or surface.get("ok") is not True:
                continue
            value = surface.get(field)
            if value is None:
                continue
            points.append((reading["date"], value))
    return points


def total_series(readings):
    """[(date, always_on_total)] for readings with a non-null total."""
    return [
        (r["date"], r["always_on_total"])
        for r in readings
        if r.get("always_on_total") is not None
    ]


def rate_per_week(points, today, window_days=28, floor_days=7):
    """(rate|None, reason|None). Only points within the last `window_days`
    (inclusive of today) count; a rate needs at least two of them spanning at
    least `floor_days`, or the series reads as noise rather than a trend."""
    window_start = today - datetime.timedelta(days=window_days)
    kept = sorted(
        (date, value) for date, value in points if window_start <= date <= today
    )
    if not kept:
        return None, "no readings"
    first_date, first_value = kept[0]
    last_date, last_value = kept[-1]
    span_days = (last_date - first_date).days
    if len(kept) < 2 or span_days < floor_days:
        return None, "insufficient history (%d readings since %s)" % (
            len(kept),
            first_date.isoformat(),
        )
    return (last_value - first_value) / span_days * 7, None


def forecast(value_rate_per_week, live_value, live_cap):
    """Slope from the log, starting point from the live measurement (design
    4.4): the log's newest reading can be most of a day old, so the report's
    own scan is the more current of the two starting points."""
    if live_value is not None and live_cap is not None and live_value > live_cap:
        return "over"
    if value_rate_per_week is not None and value_rate_per_week <= 0:
        return "not growing"
    if value_rate_per_week is None or live_cap is None or live_value is None:
        return None
    return (live_cap - live_value) / (value_rate_per_week / 7)


def log_status(readings, today, stale_days=3):
    """("none"|"stale"|"fresh", newest_date|None)."""
    if not readings:
        return "none", None
    newest = max(r["date"] for r in readings)
    if (today - newest).days > stale_days:
        return "stale", newest
    return "fresh", newest


def surface_stale_since(readings, key, stale_days=3):
    """The newest `ok` reading date for `key`, when it trails the log's own
    newest date by more than `stale_days` -- else None (including when `key`
    has no `ok` reading at all: nothing to compare, nothing to flag here)."""
    if not readings:
        return None
    newest_overall = max(r["date"] for r in readings)
    surface_dates = [
        r["date"]
        for r in readings
        for surface in (r.get("surfaces") or [])
        if surface.get("key") == key and surface.get("ok") is True
    ]
    if not surface_dates:
        return None
    newest_surface = max(surface_dates)
    if (newest_overall - newest_surface).days > stale_days:
        return newest_surface
    return None
