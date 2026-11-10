"""Live Claude.ai usage budget, and what Claude owes the human as it runs out.

Claude Code hands live `rate_limits` to exactly one surface — the status line —
and paints its own warning above 70% for the HUMAN to read. The model never sees
it. That asymmetry is the bug this tool exists to fix: the party making the
sequencing decision ("do I start the next plan task?") is the one party not told.

Two rules here are load-bearing and non-obvious:

* An expired window is DISCARDED, never trusted. `resets_at` is epoch seconds,
  and a cached "88% used" for a 5-hour window that already rolled over is not
  merely stale — it is wrong in the expensive direction. Acting on it halts a
  build against a budget that is in fact empty.

* The binding constraint is max() across windows, not a per-window ladder. A
  heavy week binds on seven_day while five_hour looks calm; a marathon session
  binds on five_hour while the week looks calm. Watching one alone means the
  other ends the session with no warning at all.

Every function in this section is PURE and takes an explicit `now`, so the
decisions can be tested without a clock and without a live subscription.
"""
from __future__ import annotations

import collections
import json
import math
import os
import sys
import tempfile
import time
from typing import Dict, Optional, Tuple

# Tier names, ascending in severity. The obligations attached to each live in
# policy/CLAUDE.md, not here: this module decides WHICH tier, never what to do about it.
TIER_NONE = "none"
TIER_NOTICE = "notice"
TIER_GATE = "gate"
TIER_HALT = "halt"
TIER_ORDER = (TIER_NONE, TIER_NOTICE, TIER_GATE, TIER_HALT)

# Deliberately OUTSIDE TIER_ORDER. "Unknown" is not a rung on the severity
# ladder — it is the absence of a reading, and it can be true at 3% or at 99%.
# Ranking it would let `tier_at_least` compare it against a real tier, and
# every answer to that comparison is wrong.
TIER_UNKNOWN = "unknown"

# The synthetic bucket the unknown notice is deduped under. Cannot collide
# with a real window: BUCKETS below is the closed set of real names.
UNKNOWN_BUCKET = "__unknown__"

# A SECOND synthetic bucket, for a tier taken from an estimate rather than a
# reading. Deliberately not UNKNOWN_BUCKET: the two are deduped on opposite
# schedules because they are opposite kinds of thing. The unknown notice is a
# standing CONDITION and is said once per working session; an estimate crossing
# a threshold is an EVENT, and the band climbs through a session — a HALT that
# crossed at hour four must not be swallowed by a notice printed at hour zero.
ESTIMATE_BUCKET = "__estimate__"

# What a cache read yielded. Only READING_NONE means "no reading exists" —
# READING_PARTIAL is a window mid-rollover in an otherwise healthy session,
# and conflating the two is what makes a once-per-session warning cry wolf.
READING_OK = "ok"
READING_PARTIAL = "partial"
READING_NONE = "none"

# Whether a status line ever wrote something usable here, independent of
# whether the reading is still current. Only `unknown_message` needs this, to
# keep "no status line ever ran here" from being said to someone whose status
# line is running and whose cache file is merely corrupt. See `cache_presence`.
CACHE_ABSENT = "absent"
CACHE_UNREADABLE = "unreadable"
CACHE_READABLE = "readable"

# How long an unknown notice stays announced before it is repeated. Unknown is
# a standing CONDITION, not an event — repeating it every turn is how a warning
# trains its reader to skip it.
#
# This was once 3600, on the reasoning that "once an hour keeps it
# present without nagging." That reasoning assumed unknown is a transient state
# a reader would want re-checked. It is not: the status line is the only source
# of rate-limit data and it renders in the TERMINAL ONLY, so on the VS Code
# extension and the desktop app the condition is permanent and an hourly cadence
# is a permanent nag. A warning that cannot be acted on and repeats anyway is
# exactly the thing that teaches its reader to skim — the failure this constant
# was meant to avoid, reached by the other road.
#
# 12 hours makes it once per working session in practice, which keeps the rule
# that matters — no reading and a calm reading must never look the same — while
# saying it once. It stays deliberately FINITE: `record_unknown` stores its
# expiry in the same `resets_at` slot a real window uses so that
# `record_announcement`'s pruning collects it on the same schedule, and an
# unknown bucket that never expired would be the one entry growing that file
# without bound. Do not "simplify" this to an infinite sentinel.
#
# If the notice will never clear for a structural reason — a custom statusLine
# that does not pipe through claude-usage-line, or API-key auth, where
# rate-limit data reaches no surface at all — the answer is `enabled: false`,
# not a longer window. See config.example.json.
DEFAULT_UNKNOWN_REANNOUNCE_SECONDS = 43200

# Defaults chosen to sit just under Claude Code's own 70% warning threshold, so
# the model learns no later than the human does.
DEFAULT_NOTICE_PCT = 70
DEFAULT_GATE_PCT = 85
DEFAULT_HALT_PCT = 95

# Only the two windows the statusLine schema documents. Anthropic ships others
# (seven_day_opus, extra_usage, …) that are absent for most accounts; tracking an
# undocumented bucket would make the binding constraint unpredictable per plan.
BUCKETS = ("five_hour", "seven_day")

_LABELS = {"five_hour": "5h", "seven_day": "7d"}


def bucket_label(name: str) -> str:
    """PURE. Short human label for a bucket, or the raw name."""
    return _LABELS.get(name, name)


def extract_buckets(payload) -> Dict[str, dict]:
    """PURE. Pull the usable rate-limit buckets out of a statusLine payload.

    Returns {} for every unusable shape rather than raising: `rate_limits` is
    absent before a session's first API response and on API-key auth, so
    "nothing to report" is the common case, not an error.
    """
    if not isinstance(payload, dict):
        return {}
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return {}
    out = {}
    for name in BUCKETS:
        entry = limits.get(name)
        if not isinstance(entry, dict):
            continue
        pct = entry.get("used_percentage")
        resets = entry.get("resets_at")
        # bool is an int subclass; reject it explicitly so True never reads as 1.
        if isinstance(pct, bool) or isinstance(resets, bool):
            continue
        if not isinstance(pct, (int, float)) or not isinstance(resets, (int, float)):
            continue
        # isfinite rejects NaN AND both infinities in one check. Infinity is not
        # hypothetical: `json.load` accepts the non-standard `Infinity` literal,
        # `json.dump` emits it, and we persist these buckets to a cache file — so
        # one malformed payload would round-trip. Two things then break, and the
        # second is the dangerous one: `int(inf)` raises OverflowError below, and
        # an infinite `used_percentage` sails into `binding` -> `tier_for`, which
        # reads it as HALT and pins the session there for as long as the cache
        # survives. Failing closed on a garbage number is the whole point.
        if not math.isfinite(pct) or not math.isfinite(resets):
            continue
        out[name] = {"used_percentage": float(pct), "resets_at": int(resets)}
    return out


def live_buckets(buckets: Dict[str, dict], now: float) -> Dict[str, dict]:
    """PURE. Drop any window that has already reset.

    Inclusive of the boundary: a window resetting exactly now is spent, and
    treating it as live is the failure mode described in the module docstring.
    """
    if not isinstance(buckets, dict):
        return {}
    return {name: data for name, data in buckets.items()
            if isinstance(data, dict)
            and isinstance(data.get("resets_at"), (int, float))
            and data["resets_at"] > now}


def is_fresh_payload(buckets: Dict[str, dict], now: float) -> bool:
    """PURE. False when a payload carries any already-reset window.

    A LIVE payload can never contain an expired window — Claude Code recomputes
    `rate_limits` against the response it just received, so every window it
    hands over is in the future. One dead window therefore proves the whole
    payload is a replay, and the windows that still look live are equally old.

    This is not hypothetical. A replayed cache can carry a `five_hour` window
    that reset long before the cache was written, alongside a `seven_day`
    window reading a calm figure. The seven_day figure is stale too,
    and nothing in the file said so. Rejecting the whole
    payload is what stops the calm half from being believed.

    Empty is False: there is nothing to vouch for, and callers treat False as
    "do not persist this".
    """
    if not isinstance(buckets, dict) or not buckets:
        return False
    return len(live_buckets(buckets, now)) == len(buckets)


def binding(buckets: Dict[str, dict]) -> Optional[Tuple[str, float, int]]:
    """PURE. The tightest window as (name, used_percentage, resets_at), or None.

    Ties break by BUCKETS order rather than dict order so the reported name is
    stable — a warning whose subject flaps between runs reads as a bug.
    """
    best = None
    for name in BUCKETS:
        data = buckets.get(name) if isinstance(buckets, dict) else None
        if not isinstance(data, dict):
            continue
        pct = data.get("used_percentage")
        if not isinstance(pct, (int, float)):
            continue
        if best is None or pct > best[1]:
            best = (name, float(pct), int(data.get("resets_at", 0)))
    return best


def tier_for(pct: float, notice: int, gate: int, halt: int) -> str:
    """PURE. Which tier a utilisation percentage falls in. Boundaries inclusive.

    Inclusive because the thresholds are read as "at 85%, gate" by every human
    who configures them, and an exclusive boundary would make 85.0 exactly do
    nothing.
    """
    try:
        value = float(pct)
    except (TypeError, ValueError):
        return TIER_NONE
    # Belt and braces with extract_buckets: a non-finite utilisation must read as
    # "no tier", never as HALT. An infinite percentage is meaningless, and the
    # tier ladder below would otherwise rank it as the most severe reading.
    if not math.isfinite(value):
        return TIER_NONE
    if value >= halt:
        return TIER_HALT
    if value >= gate:
        return TIER_GATE
    if value >= notice:
        return TIER_NOTICE
    return TIER_NONE


def tier_at_least(tier: str, minimum: str) -> bool:
    """PURE. Severity comparison. An unrecognised tier ranks lowest."""
    try:
        return TIER_ORDER.index(tier) >= TIER_ORDER.index(minimum)
    except ValueError:
        return False


def format_reset(resets_at, now: float) -> str:
    """PURE-ish (reads the local timezone). When a window comes back.

    A clock time within ~2 days, a weekday beyond that: "resets 23:40" is
    actionable, "resets Mon" is what you actually need for a weekly window.
    Anything unparseable renders "?" — a status line must never carry a
    traceback.
    """
    try:
        target = float(resets_at)
        if not math.isfinite(target):
            return "?"
    except (TypeError, ValueError):
        return "?"
    if target - now > 2 * 86400:
        return time.strftime("%a", time.localtime(target))
    return time.strftime("%H:%M", time.localtime(target))


# --- Config and the two state files -----------------------------------------
#
# Two files, deliberately never merged: `cache_path()` (the budget snapshot,
# written every ~30s by the status line) and `state_path()` (per-session
# announcement dedupe, written by the hooks). One file with two writers would
# race — whichever wrote last would silently drop the other's data.

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "lib"))
try:
    import toolkit_config as _config
except Exception:  # pragma: no cover - config is optional by design
    _config = None

DEFAULT_STALE_AFTER_SECONDS = 900
DEFAULT_CACHE_PATH = "~/.claude/usage-budget.json"
DEFAULT_STATE_PATH = "~/.claude/usage-budget-state.json"


def _reset_config_cache() -> None:
    """Force the next config read to re-parse CLAUDE_TOOLKIT_CONFIG. Tests only.

    `toolkit_config.CONFIG_PATH` is computed once at import time from the
    environment, so a test that swaps `CLAUDE_TOOLKIT_CONFIG` between cases
    must re-derive it too — `load(refresh=True)` alone would keep re-reading
    whichever path was current the first time this module was imported.
    """
    if _config is None:
        return
    try:
        _config.CONFIG_PATH = os.path.expanduser(
            os.environ.get("CLAUDE_TOOLKIT_CONFIG",
                           "~/.config/claude-code-toolkit/config.json"))
        _config.load(refresh=True)
    except Exception:
        pass


def is_enabled() -> bool:
    """False only when explicitly disabled. Absent config means on."""
    if _config is None:
        return True
    try:
        return _config.get("usage_watch", "enabled", default=True) is not False
    except Exception:
        return True


def subagent_context_enabled() -> bool:
    """False only when explicitly disabled. Absent config means on."""
    if _config is None:
        return True
    try:
        return _config.get("usage_watch", "subagent_context", default=True) is not False
    except Exception:
        return True


def dispatch_advisory_enabled() -> bool:
    """True ONLY when explicitly enabled. Absent config means OFF.

    Opposite default to `subagent_context` above, and the asymmetry is the
    point. That hook informs a worker after a decision is already made; this
    one can return `permissionDecision: "ask"` and interrupt a human. A key
    nobody has set must not do that, so the absent case is the stricter one —
    the repo rule, applied where it actually bites.

    A SEPARATE switch from `usage_watch.estimate`, and the two are NOT chained:
    with `estimate` off the advisory still gates on a real reading, and has no
    estimate to fall back on.
    """
    if _config is None:
        return False
    try:
        return _config.get("usage_watch", "dispatch_advisory",
                           default=False) is True
    except Exception:
        return False


def postmortem_enabled() -> bool:
    """False only when explicitly disabled. Absent config means on."""
    if _config is None:
        return True
    try:
        return _config.get("usage_watch", "postmortem_handover", default=True) is not False
    except Exception:
        return True


def thresholds() -> Tuple[int, int, int]:
    """Configured (notice, gate, halt), each defaulting independently.

    Always sorted before returning: an out-of-order config (say, halt below
    gate) would otherwise make the strictest tier unreachable. Sorting keeps
    the strictest configured number biting no matter which key it arrived
    under.
    """
    if _config is None:
        return (DEFAULT_NOTICE_PCT, DEFAULT_GATE_PCT, DEFAULT_HALT_PCT)
    try:
        notice = _config.get_int("usage_watch", "notice_pct", default=DEFAULT_NOTICE_PCT)
        gate = _config.get_int("usage_watch", "gate_pct", default=DEFAULT_GATE_PCT)
        halt = _config.get_int("usage_watch", "halt_pct", default=DEFAULT_HALT_PCT)
    except Exception:
        return (DEFAULT_NOTICE_PCT, DEFAULT_GATE_PCT, DEFAULT_HALT_PCT)
    lo, mid, hi = sorted((notice, gate, halt))
    return (lo, mid, hi)


def _configured_stale_after() -> int:
    """The actual lookup, split out so `read_cache`'s own `stale_after`
    parameter never has to shadow the public getter of the same name."""
    if _config is None:
        return DEFAULT_STALE_AFTER_SECONDS
    try:
        value = _config.get_int("usage_watch", "stale_after_seconds",
                                default=DEFAULT_STALE_AFTER_SECONDS)
    except Exception:
        return DEFAULT_STALE_AFTER_SECONDS
    return value if value > 0 else DEFAULT_STALE_AFTER_SECONDS


def stale_after() -> int:
    """Configured cache staleness ceiling in seconds. A value <= 0 falls back
    to the default rather than disabling staleness entirely."""
    return _configured_stale_after()


def cache_path() -> str:
    """Where the status line writes the budget snapshot. Always absolute."""
    if _config is None:
        return os.path.expanduser(DEFAULT_CACHE_PATH)
    try:
        configured = _config.get_path("usage_watch", "cache_path", default=DEFAULT_CACHE_PATH)
    except Exception:
        configured = DEFAULT_CACHE_PATH
    return os.path.expanduser(configured or DEFAULT_CACHE_PATH)


def state_path() -> str:
    """Where the hooks record per-session announcement dedupe. Always absolute.

    Deliberately a different file from cache_path() — see the module-level
    note above this section.
    """
    if _config is None:
        return os.path.expanduser(DEFAULT_STATE_PATH)
    try:
        configured = _config.get_path("usage_watch", "state_path", default=DEFAULT_STATE_PATH)
    except Exception:
        configured = DEFAULT_STATE_PATH
    return os.path.expanduser(configured or DEFAULT_STATE_PATH)


def _atomic_write(target: str, blob: dict, prefix: str) -> bool:
    """Write `blob` as JSON to `target` atomically. True on success.

    The temp file is created with `mkstemp(dir=...)` in the TARGET's own
    directory, never a shared temp dir: `os.replace` is only atomic within a
    single filesystem, so a cross-filesystem temp file would defeat the point
    of doing this at all.
    """
    try:
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=prefix)
    except Exception:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(blob, handle)
        os.replace(tmp_path, target)
        return True
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False


def write_cache(buckets: dict, now: float, path: Optional[str] = None) -> bool:
    """Atomically persist the live buckets so the hooks can read what the
    status line last saw. False on any failure — a hook missing one cycle's
    cache is far cheaper than a status line that crashes to write it.

    REFUSES a payload that is not fresh, and this refusal is the whole point of
    the function returning a bool. `written_at` is a claim about when the
    NUMBERS were true, and everything downstream — `read_cache`'s staleness
    ceiling most of all — reads it that way. Stamping `now` onto a replayed
    payload makes that claim a lie that no later check can catch, because the
    only evidence of the lie is the very field being falsified.

    Declining to write leaves the previous (correctly old) `written_at` in
    place, which ages into staleness and surfaces as "unknown". That is the
    honest outcome: no reading is not the same as a calm reading.
    """
    if not is_fresh_payload(buckets, now):
        return False
    target = path if path is not None else cache_path()
    return _atomic_write(target, {"written_at": now, "buckets": buckets},
                         ".usage-budget-")


def _cache_snapshot(target) -> Tuple[Optional[dict], str]:
    """`(data, presence)` from ONE open of the cache file.

    Every public reader below used to open the file itself, so a hook asking
    two questions about "the cache" asked them of two different files — see
    `cache_report` for what that cost. This is the single read they now share;
    the classification rules are exactly `cache_presence`'s, moved rather than
    rewritten, and its docstring is still where they are explained.

    `data` is the parsed dict when the file held one, else None. It is
    returned even when `presence` is CACHE_UNREADABLE, because the two
    questions genuinely diverge there: a file whose `buckets` are garbage
    carries a real, measurable `written_at`, and `cache_age` is right to
    report it while `cache_presence` is right to refuse the contents.
    """
    try:
        with open(target, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, NotADirectoryError):
        # Nothing is there, and nothing can be: ENOENT, or a path component
        # that is a file. Only genuine absence earns _CAUSE_NEVER_FED, whose
        # remedy — turn the tool off — is the most expensive one in the file.
        return (None, CACHE_ABSENT)
    except (IOError, OSError):
        # A file we cannot open is still a file. Permission-denied on the
        # file, on a parent directory, or a directory sitting where the cache
        # belongs: "exists but could not be read" is true of all three. The
        # errno decides this, NOT os.path.exists — a stat can fail for the
        # same permission reason and return False for a file that is plainly
        # there, and it opens a TOCTOU window besides.
        return (None, CACHE_UNREADABLE)
    except Exception:
        return (None, CACHE_UNREADABLE)
    if not isinstance(data, dict):
        return (None, CACHE_UNREADABLE)
    written_at = data.get("written_at")
    if isinstance(written_at, bool) or not isinstance(written_at, (int, float)):
        return (data, CACHE_UNREADABLE)
    try:
        usable = extract_buckets({"rate_limits": data.get("buckets")})
    except Exception:
        # `extract_buckets` is NOT total, and this call is on the shared path
        # for all four readers now, so an exception here reaches three of them
        # that could never see one before. `math.isfinite` raises
        # `OverflowError` on an out-of-float-range int, so a cache holding a
        # JSON *integer* literal of 309+ digits in `used_percentage` or
        # `resets_at` is enough. Measured against the parent commit:
        #
        #   read_cache_status(stale)  ('none', {})  -> raised
        #   cache_age                 a float       -> raised
        #   cache_reading_age         a float       -> raised
        #   cache_presence            raised        -> raised
        #
        # The visible cost was `claude-usage-postmortem` writing NOTHING on a
        # `rate_limit` death — no file, no error, exit 0 — on the one event
        # that document exists for. It also contradicted `read_cache_status`'
        # own promise, preserved below, that a half-written or hand-edited
        # file "must degrade to 'nothing to report', not a traceback".
        #
        # Note this makes `cache_presence` strictly BETTER than the parent
        # rather than merely equal: it raised there and returns UNREADABLE
        # here. That is the one place this refactor is not behaviour-preserving,
        # and it is deliberate — the promise above is the contract, and a file
        # whose contents cannot be validated is unreadable by definition.
        return (data, CACHE_UNREADABLE)
    if not usable:
        return (data, CACHE_UNREADABLE)
    return (data, CACHE_READABLE)


def _age_from(data: Optional[dict], now, signed: bool) -> Optional[float]:
    """Seconds since `written_at`, or None when there is nothing to measure.

    `signed=False` clamps a negative to 0.0 — right for a sentence a human
    reads, wrong for a freshness gate. See `cache_reading_age`.
    """
    if not isinstance(data, dict):
        return None
    written_at = data.get("written_at")
    if isinstance(written_at, bool) or not isinstance(written_at, (int, float)):
        return None
    try:
        age = float(now) - float(written_at)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(age):
        return None
    if signed:
        return age
    return age if age > 0 else 0.0


CacheReport = collections.namedtuple(
    "CacheReport", "status buckets age reading_age presence")


def cache_report(now: float, path: Optional[str] = None,
                 stale_after: Optional[int] = None) -> CacheReport:
    """Everything a hook asks about the cache, derived from ONE read.

    THE DEFECT THIS CLOSES. `claude-usage-watch` called `read_cache_status`,
    then `cache_age`, then `cache_presence` — three independent opens of a
    file a *different* process rewrites on every prompt. A status line landing
    between them pairs a verdict from the old file with an age from the new
    one, and the two failure shapes are the ones this module already has scar
    tissue for:

    * On the UNKNOWN path it reproduces the self-contradiction
      `read_cache_status`' docstring says was fixed — "no live reading here
      (last cached reading is 0m old)" — because the verdict is READING_NONE
      from the stale read and the age is ~0s from the fresh one. A warning
      visibly wrong about itself is one people learn to skip.
    * On the READING_OK path it pairs an OLD percentage with a NEW age, which
      is exactly the poisoning `estimate_lib.ANCHOR_MAX_READING_AGE`
      exists to stop — the age bound passes while the reading
      it is vouching for is arbitrarily stale.

    The window is small. It is not zero, it is entered once per prompt, and
    both outcomes are SILENT: nothing downstream can tell a mismatched pair
    from a matched one. That is what makes it worth a read rather than a
    comment.

    The four public readers keep their signatures and their behaviour, so a
    caller that genuinely wants one fact is unchanged. They just no longer
    open the file four times between them.
    """
    target = path if path is not None else cache_path()
    ceiling = stale_after if stale_after is not None else _configured_stale_after()
    data, presence = _cache_snapshot(target)
    status, buckets = _status_from(data, now, ceiling)
    return CacheReport(status=status, buckets=buckets,
                       age=_age_from(data, now, signed=False),
                       reading_age=_age_from(data, now, signed=True),
                       presence=presence)


def cache_presence(path: Optional[str] = None) -> str:
    """Did a status line write something USABLE here — absent, unreadable, or
    readable. Nothing about whether the reading is current.

    `cache_age` collapses "absent" and "unparseable" into None, which is right
    for measuring an age and wrong for explaining one: the second case means a
    status line probably IS running and wrote something unusable, and the
    notice must not tell that person no status line was ever installed.

    Validates the buckets through `extract_buckets`, deliberately the same
    check `read_cache_status` applies. Stopping at a numeric `written_at`
    looked cleaner and reintroduced the self-contradiction: a file whose
    buckets are garbage — the "written by an older version" case — reads
    `READING_NONE` there while carrying a real, small age here, so the notice
    said "no live reading here (last cached reading is 0m old). A status line
    was feeding it and has stopped" about a file written seconds earlier. If
    the reader cannot use the contents, the contents are not a reading, and
    the two functions have to agree on that.

    A healthy status line cannot produce such a file: `claude-usage-line`
    writes only when `extract_buckets` yields something, and `write_cache`
    refuses an empty payload. So unusable contents mean corruption or a
    foreign schema, never API-key auth — which never writes a file at all and
    correctly reads CACHE_ABSENT.
    """
    target = path if path is not None else cache_path()
    return _cache_snapshot(target)[1]


def cache_age(now: float, path: Optional[str] = None) -> Optional[float]:
    """Seconds since the cache was written, or None if there is no usable
    `written_at` to measure from.

    None and a large number mean different things to a reader — "no status
    line has ever run here" versus "one ran, hours ago" — so this never
    collapses the two into a single sentinel.
    """
    target = path if path is not None else cache_path()
    return _age_from(_cache_snapshot(target)[0], now, signed=False)


def cache_reading_age(now: float, path: Optional[str] = None) -> Optional[float]:
    """Seconds since the cache was written, SIGNED, or None when unreadable.

    `cache_age` clamps a negative to 0.0, which is right for the sentence it
    feeds — a human does not need "the cache is -40s old" — and wrong for a
    freshness GATE, where a future stamp is the single most suspicious value a
    cache can carry. Clock skew, or a cache copied off another machine: both
    arrive as a negative here and as maximum freshness through `cache_age`.
    `read_cache_status` has the same blind spot (`now - written_at > ceiling`
    never trips on a future stamp), so such a cache also reads READING_OK — and
    an arbitrarily old percentage from another machine would then be paired with
    a token count taken this instant, which is the persistent poisoning
    `estimate_lib.ANCHOR_MAX_READING_AGE` exists to stop.
    """
    target = path if path is not None else cache_path()
    return _age_from(_cache_snapshot(target)[0], now, signed=True)


def read_cache_status(now: float, path: Optional[str] = None,
                      stale_after: Optional[int] = None
                      ) -> Tuple[str, Dict[str, dict]]:
    """The cache as (state, live buckets). The state is the point.

    Three outcomes, and collapsing the middle one into either neighbour is a
    bug in a different direction each time:

    * `READING_OK` — every window in the file is still live. Trustworthy.
    * `READING_PARTIAL` — the file is fresh, but at least one window has reset
      since it was written. This is the ORDINARY five-hour rollover in a
      healthy session: the status line keeps replaying the last payload until
      the next API response, `is_fresh_payload` correctly refuses to re-stamp
      it, and for those few seconds the reading is incomplete. A fresh file
      whose windows have ALL reset is the extreme of this, not a different
      kind of thing — see the branch comment below.
    * `READING_NONE` — absent, malformed, or stale past the ceiling. No
      reading exists.

    Only `READING_NONE` is a standing condition. Treating a rollover as one
    told a terminal user with a 45-second-old cache that there was "no live
    reading here (last cached reading is 0m old)" because "the status line
    renders in the TERMINAL only" — self-contradictory, wrong about its own
    cause, and it spent the announcement slot, so a status line that then
    genuinely died went unreported for the rest of that slot. A warning
    visibly wrong about itself is one people learn to skip.

    Re-validates through `extract_buckets`/`live_buckets` rather than trusting
    the file's shape — the cache is written by a different process, and a
    half-written or hand-edited file must degrade to "nothing to report", not
    a traceback.
    """
    target = path if path is not None else cache_path()
    ceiling = stale_after if stale_after is not None else _configured_stale_after()
    return _status_from(_cache_snapshot(target)[0], now, ceiling)


def _status_from(data: Optional[dict], now: float,
                 ceiling) -> Tuple[str, Dict[str, dict]]:
    """The (state, live buckets) half of a snapshot. See `read_cache_status`
    for what the three states mean and why the middle one must not be
    collapsed into either neighbour. Split out so `cache_report` can reach it
    without a second open — the logic is unchanged, only its input is."""
    if not isinstance(data, dict):
        return (READING_NONE, {})
    written_at = data.get("written_at")
    if isinstance(written_at, bool) or not isinstance(written_at, (int, float)):
        return (READING_NONE, {})
    if now - written_at > ceiling:
        return (READING_NONE, {})
    try:
        validated = extract_buckets({"rate_limits": data.get("buckets")})
    except Exception:
        # Second of the two `extract_buckets` call sites, and it needs the same
        # guard for the same reason -- `math.isfinite` raises `OverflowError`
        # on an out-of-float-range int literal. On a FRESH file the parent
        # commit raised here too, so this is not a regression; it is the same
        # promise three lines up in this function's own docstring, which says a
        # half-written or hand-edited file "must degrade to 'nothing to report',
        # not a traceback". Guarding only `_cache_snapshot` left this reachable
        # and the fix looking complete.
        return (READING_NONE, {})
    if not validated:
        return (READING_NONE, {})
    live = live_buckets(validated, now)
    if len(live) != len(validated):
        # Includes the case where NOTHING is live. A fresh file whose every
        # window has rolled is still a rollover, not a dead feed — and calling
        # it READING_NONE reproduced, for a single-window cache, the exact
        # self-contradiction the three states were introduced to remove: "no
        # live reading here (last cached reading is 0m old). A status line was
        # feeding it and has stopped", thirty seconds after a healthy write.
        # It spent the announcement slot too, so a status line dying a minute
        # later went unreported.
        #
        # An empty live set is the extreme of incomplete, not a different
        # kind of thing. Callers already handle it: no visible window can
        # cross a tier, so the hook stays quiet, and if the feed really has
        # stopped the file ages past the ceiling and READING_NONE announces
        # it properly. Every window resetting at once also means every budget
        # just refilled, so silence is accurate rather than merely safe.
        return (READING_PARTIAL, live)
    return (READING_OK, live)


def read_cache(now: float, path: Optional[str] = None,
               stale_after: Optional[int] = None) -> Dict[str, dict]:
    """The live buckets from a COMPLETE cache reading, or {} otherwise.

    A partial reading is discarded rather than returned short, because
    `binding` takes max() across windows and cannot see a window that is
    simply missing: a surviving seven_day at 8% beside a five_hour that rolled
    over reads as a calm 8%, and the one number that could have raised an
    alarm is gone.

    NOTE: as of the fix that moved `claude-usage-subagent` and
    `claude-usage-postmortem` across, this function has NO production callers
    left — all three use `read_cache_status`, which lets each one decide what a
    partial reading means for it. Only its own tests reach this. Before adding
    a caller, be sure you want a rollover to be indistinguishable from a dead
    status line, because that is the whole difference between the two.
    """
    status, buckets = read_cache_status(now, path=path, stale_after=stale_after)
    return buckets if status == READING_OK else {}


def _load_state(path: str) -> dict:
    """The parsed state file, or {} for absent/malformed. Never raises."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def already_announced(session_id, bucket, resets_at, tier, path=None) -> bool:
    """True when this exact (session, bucket, resets_at) already recorded a
    tier at least as severe as `tier`.

    A different `resets_at` re-arms the same session/bucket/tier combination:
    the window rolled over, so whatever was announced against the old window
    says nothing about the new one.
    """
    target = path if path is not None else state_path()
    sessions = _load_state(target).get("sessions")
    if not isinstance(sessions, dict):
        return False
    session = sessions.get(session_id)
    if not isinstance(session, dict):
        return False
    record = session.get(bucket)
    if not isinstance(record, dict):
        return False
    if record.get("resets_at") != resets_at:
        return False
    return tier_at_least(record.get("tier"), tier)


def record_announcement(session_id, bucket, resets_at, tier, now, path=None) -> bool:
    """Record that `tier` has been announced for (session, bucket, resets_at).

    Prunes any session whose every recorded `resets_at` has already passed
    `now`, so the file cannot grow without bound over the life of a machine
    that runs many short sessions.

    A malformed `now` must not raise out of a function documented never to.
    Falling back to 0 keeps EVERY session rather than pruning blindly: the
    prune is housekeeping, while a wrongly-dropped record re-announces a tier
    that was already announced. Keeping is the safe direction here.
    """
    try:
        now = float(now)
        if not math.isfinite(now):
            now = 0.0
    except (TypeError, ValueError):
        now = 0.0

    target = path if path is not None else state_path()
    sessions = _load_state(target).get("sessions")
    if not isinstance(sessions, dict):
        sessions = {}

    session = sessions.get(session_id)
    if not isinstance(session, dict):
        session = {}
    session[bucket] = {"resets_at": resets_at, "tier": tier}
    sessions[session_id] = session

    # PRUNE PER BUCKET, THEN PER SESSION. The same policy at two granularities:
    # a record whose window has passed says nothing about the current one.
    #
    # Per-session alone was enough while every session held a handful of FIXED
    # buckets — one per tier, one per static state. The dispatch advisory's wave
    # key is not fixed: it is one bucket per user input, plus one more per
    # minute of any long turn that keeps dispatching, because the anchor-age
    # clause changes the rendered text. Before this: a session holding
    # many expired buckets and one live one retained all of them, because the `any()`
    # below only ever asked whether the SESSION was worth keeping. A session
    # that keeps working is never pruned, so its buckets accumulated for its
    # whole life — and every usage hook parses this file on every
    # UserPromptSubmit and PostToolBatch.
    #
    # The failure direction is the safe one: a bucket dropped early costs one
    # re-announced advisory, never a suppressed one. Anything unparseable is
    # KEPT rather than dropped — this prune is housekeeping, and a record it
    # cannot read is not evidence that the record is spent.
    def _live(record):
        return (isinstance(record, dict)
                and isinstance(record.get("resets_at"), (int, float))
                and not isinstance(record.get("resets_at"), bool)
                and record["resets_at"] > now)

    def _unreadable(record):
        return not (isinstance(record, dict)
                    and isinstance(record.get("resets_at"), (int, float))
                    and not isinstance(record.get("resets_at"), bool))

    pruned = {}
    for sid, session_buckets in sessions.items():
        if not isinstance(session_buckets, dict):
            continue
        kept = {name: record for name, record in session_buckets.items()
                if _live(record) or _unreadable(record)}
        if any(_live(record) for record in kept.values()):
            pruned[sid] = kept

    return _atomic_write(target, {"sessions": pruned}, ".usage-budget-state-")


def unknown_reannounce_seconds() -> int:
    """How long an unknown notice stays announced. Non-positive falls back to
    the default rather than meaning "repeat every turn"."""
    if _config is None:
        return DEFAULT_UNKNOWN_REANNOUNCE_SECONDS
    try:
        value = _config.get_int("usage_watch", "unknown_reannounce_seconds",
                                default=DEFAULT_UNKNOWN_REANNOUNCE_SECONDS)
    except Exception:
        return DEFAULT_UNKNOWN_REANNOUNCE_SECONDS
    return value if value > 0 else DEFAULT_UNKNOWN_REANNOUNCE_SECONDS


def unknown_announced(session_id, now: float, path: Optional[str] = None) -> bool:
    """True when this session was already told the budget is unreadable and
    that notice has not yet aged out.

    Deliberately does NOT go through `already_announced`: that function matches
    `resets_at` for exact equality, which is right for a real window (a new
    window is a genuinely new fact) and wrong here, where the stored value is
    an arbitrary expiry computed fresh on every call and would never match.
    """
    target = path if path is not None else state_path()
    sessions = _load_state(target).get("sessions")
    if not isinstance(sessions, dict):
        return False
    session = sessions.get(session_id)
    if not isinstance(session, dict):
        return False
    record = session.get(UNKNOWN_BUCKET)
    if not isinstance(record, dict):
        return False
    expires = record.get("resets_at")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return False
    return expires > now


def record_unknown(session_id, now: float, path: Optional[str] = None,
                   reannounce: Optional[int] = None) -> bool:
    """Record that this session has been told the budget is unreadable.

    Stores its expiry in the same `resets_at` slot a real window uses, which is
    what lets `record_announcement`'s pruning collect it on the same schedule as
    everything else — a synthetic bucket that never expired would be the one
    entry that grows this file without bound.
    """
    window = reannounce if reannounce is not None else unknown_reannounce_seconds()
    try:
        expires = float(now) + float(window)
    except (TypeError, ValueError):
        return False
    return record_announcement(session_id, UNKNOWN_BUCKET, expires,
                               TIER_UNKNOWN, now, path=path)


# --- The status line itself ---------------------------------------------
#
# render_status is the one place both jobs meet: it is called from the
# statusLine executable, which prints its return value and — as a separate
# side effect — writes the cache these same buckets came from.

def render_status(payload) -> str:
    """The single line printed to stdout by the statusLine command.

    Built as a ` · `-joined list of segments, each independently optional: any
    segment that cannot be derived from an unexpected payload shape is skipped
    rather than raising, so a malformed or partial payload still renders a
    short line instead of a traceback. Never contains a newline.
    """
    if not isinstance(payload, dict):
        payload = {}

    segments = []

    try:
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            segments.append(os.path.basename(cwd.rstrip("/")) or cwd)
    except Exception:
        pass

    try:
        worktree = payload.get("worktree")
        if isinstance(worktree, dict):
            branch = worktree.get("branch")
            if isinstance(branch, str) and branch:
                segments.append(branch)
    except Exception:
        pass

    try:
        model = payload.get("model")
        if isinstance(model, dict):
            name = model.get("display_name")
            if isinstance(name, str) and name:
                segments.append(name)
    except Exception:
        pass

    try:
        ctx = payload.get("context_window")
        if isinstance(ctx, dict):
            pct = ctx.get("used_percentage")
            if isinstance(pct, (int, float)) and not isinstance(pct, bool) \
                    and math.isfinite(pct):
                segments.append("ctx {0:.0f}%".format(pct))
    except Exception:
        pass

    try:
        now = time.time()
        buckets = live_buckets(extract_buckets(payload), now)
        for name in BUCKETS:
            data = buckets.get(name)
            if not isinstance(data, dict):
                continue
            pct = data.get("used_percentage")
            resets = data.get("resets_at")
            segments.append("{0} {1:.0f}% ↻{2}".format(
                bucket_label(name), pct, format_reset(resets, now)))
    except Exception:
        pass

    # Enforce the single-line contract at the boundary rather than trusting the
    # inputs. A newline is a legal character in a POSIX path segment, so `cwd`
    # alone can carry one — and a status line with an embedded newline corrupts
    # the terminal's status bar on every render, not just once. Substituting
    # rather than stripping keeps the segment visible and its width honest.
    # str.isprintable() is False for CR, LF, tab, and the rest of the control
    # range, and True for the "·" and "↻" separators this line is built from.
    return "".join(ch if ch.isprintable() else " "
                   for ch in " · ".join(segments))


# --- Main-loop tier injection -----------------------------------------------
#
# tier_message is what claude-usage-watch prints into the model's own context
# (UserPromptSubmit and PostToolBatch). Unlike the status line, this text is
# read by Claude, not the human — see the module docstring's asymmetry note.

def owner() -> str:
    """Who to name in a tier message. Defaults to "the human".

    Wrapped so a missing or broken config never raises inside a hook — same
    shape as every other config read in this module.
    """
    if _config is None:
        return "the human"
    try:
        return _config.owner_name()
    except Exception:
        return "the human"


# --- Which surface is this? --------------------------------------------------
#
# `rate_limits` reaches the status line and nothing else, and the status line
# renders in the TERMINAL only. So on the VS Code extension, the desktop app,
# the web and CI, the UNKNOWN state is STRUCTURAL and permanent rather than a
# fault someone can go and fix. Telling the person about it there is a
# reminder they cannot act on, which is the road to a skimmed-past warning this module has
# already travelled once from the other direction (see
# DEFAULT_UNKNOWN_REANNOUNCE_SECONDS).
#
# MEASURED, not inferred: a hook subprocess DOES inherit
# `CLAUDE_CODE_ENTRYPOINT`. Probed with a throwaway `PreToolUse`
# hook, which read `claude-vscode` out of its environment. That was
# a blocking premise — the variable is confirmed present for a process the Bash
# tool spawns, and a hook's environment is a SEPARATE assumption, which this
# repo's own rule says needs its own check. Do not downgrade this to a `strings`
# read of the binary; that is the truncated-strings inference the effort-levels
# claim was burned by.
#
# Worth knowing if you extend this: `CLAUDE_CODE_EXECPATH` is NOT in the hook
# environment even though it IS in the Bash tool's. The hook env is a subset,
# so corroborating on a second variable is not free.
STATUS_LINE_ENTRYPOINT_VAR = "CLAUDE_CODE_ENTRYPOINT"

# ALLOWLIST the surfaces that render a status line; everything else is treated
# as lacking one. The direction matters and the usual "absent means stricter"
# reasoning is NOT what justifies it here — getting this wrong is cheap in both
# directions. Mis-classifying a terminal as no-status-line suppresses only the
# RELAY of an UNKNOWN notice, and a terminal has a real reading, so its real
# tiers fire regardless. Mis-classifying VS Code as a terminal restores the old
# nag. Neither disables a protection. So a future entrypoint nobody has heard of
# defaults to "no status line", which is the behaviour that was asked for.
#
# THE TERMINAL REPORTS "cli", NOT "claude-cli". Read out of the binary's own
# assignment — `c.set("CLAUDE_CODE_ENTRYPOINT", e ? "sdk-cli" : "cli")` — and
# corroborated by its telemetry switch, whose default arm is `case "cli"`.
# The first cut of this file had `claude-cli`, taken from a planning document
# that labelled the list MEASURED. It was not: that literal appears in the
# binary only as an unrelated cache namespace, so the `True` state was
# unreachable in production and the tests asserting it only proved the
# classifier agreed with its own constant. Grep the assignment and the switch,
# never a list someone else wrote down.
_STATUS_LINE_ENTRYPOINTS = frozenset(("cli",))

# Enumerated from `switch(process.env.CLAUDE_CODE_ENTRYPOINT)` in the running
# binary (2.1.246), which is the only exhaustive list of the values Claude Code
# actually sets. Listing the negatives explicitly buys the third state below: an
# entrypoint outside BOTH sets is a surface this code has never heard of, and
# saying so is different from saying it has no status line.
#
# None of these render a status line — it is a terminal UI, so the desktop app,
# the VS Code extension, Slack and Teams have no place to draw one, and the
# SDK, MCP, agent, bench and Action entrypoints have no UI at all.
_NO_STATUS_LINE_ENTRYPOINTS = frozenset((
    "claude-vscode",
    "claude-desktop", "claude-desktop-3p",
    "claude-in-teams", "claude_in_slack", "claude-in-slack",
    "remote", "remote_baku", "remote_cowork", "remote_desktop",
    "remote_mobile",
    "sdk-cli", "sdk-ts", "sdk-py",
    "mcp", "local-agent", "bench",
    "claude-code-github-action",
))


def status_line_surface(env=None) -> Optional[bool]:
    """PURE (reads the process environment, or `env` when given).

    True  — positively a surface that renders a status line.
    False — positively one that does not.
    None  — the variable is absent or names a surface we do not know.

    The `None` case is NOT a synonym for `False` at the call sites: it means
    "cannot tell", and the relay is the fallback there, so an unrecognised
    surface behaves exactly as today rather than going quiet on a guess.

    ON THE RETURN TYPE: this is `Optional[bool]`, so `assertFalse` is not an
    assertion against it — `False` and `None` are both falsy and the whole
    design is that they lead to different behaviour. Tests use `assertIs(x,
    False)` and `assertIsNone(x)`; a truthiness check has already bitten in
    `thread-handover`.

    `env` is injectable so tests can drive all three states hermetically. A
    test that reads the ambient `CLAUDE_CODE_ENTRYPOINT` passes on exactly one
    surface and is the hermeticity failure this repo bans.
    """
    try:
        source = os.environ if env is None else env
        value = source.get(STATUS_LINE_ENTRYPOINT_VAR)
    except Exception:
        return None
    if not isinstance(value, str) or not value:
        return None
    name = value.strip().lower()
    if name in _STATUS_LINE_ENTRYPOINTS:
        return True
    if name in _NO_STATUS_LINE_ENTRYPOINTS:
        return False
    return None


_TIER_TEMPLATES = {
    TIER_NOTICE: (
        "USAGE BUDGET: {label} at {pct:.0f}% (resets {when}). Not urgent — "
        "mention it to {owner} at the next natural boundary and keep "
        "working. Do not gate on this."
    ),
    TIER_GATE: (
        "USAGE BUDGET — GATE: {label} at {pct:.0f}% (resets {when}). Finish "
        "the current unit of work, then STOP and raise an AskUserQuestion: "
        "stop clean here, push on, or drop to Sonnet. Do not start the next "
        "unit, and do not dispatch new subagents, before {owner} answers."
    ),
    TIER_HALT: (
        "USAGE BUDGET — HALT: {label} at {pct:.0f}% (resets {when}). Start "
        "no new plan task or unit of work and dispatch nothing. Commit what "
        "is open, write a handover (/handover), and report where it stopped."
    ),
}


# SPLIT INTO TRUTH AND RELAY, and only the relay is conditional.
#
# There are two audiences and only one of them goes quiet. The standing rule —
# "no reading and a calm reading must never look the same" — is about the party
# DECIDING, and Claude is the decider. So the truth half is UNCONDITIONAL and
# reaches Claude on every surface, verbatim. What drops on a surface that never
# has a reading is the instruction to relay it to a human who cannot act on it.
#
# If this ever finds itself suppressing the Claude-facing half, it has become
# the thing the rule forbids and should be stopped.
_UNKNOWN_TRUTH = (
    "USAGE BUDGET — UNKNOWN: there is no live reading here{age}. {cause} "
    "Treat this as unknown, NOT as headroom: the notice/gate/halt tiers "
    "cannot fire, so nothing will stop you walking into the limit."
)

_UNKNOWN_RELAY = (
    " Say so to {owner} before starting anything long, and prefer a stopping "
    "point you can resume from."
)

# The `False`-surface replacement for the relay. It still ends the notice with
# something actionable, because the alternative — stopping dead after the truth
# — reads as an unfinished sentence and invites the reader to relay it anyway.
_UNKNOWN_STRUCTURAL = (
    " This surface never has a reading, so this is structural rather than a "
    "fault, and there is nothing for {owner} to go and fix. Not worth raising "
    "with them unless they ask."
)

# APPENDED ONLY WHEN THE ESTIMATOR IS ON, and that condition is the whole
# point. `claude-usage-anchor` refuses outright while `usage_watch.estimate` is
# false — it prints "the budget estimator is off, so there is nothing to
# anchor" and exits 1 — and false is the SHIPPED DEFAULT, which is what a
# fresh install gets. `config.example.json` already settled this exact question for
# the dispatch advisory, in as many words: *"an advisory naming a command that
# cannot run is worse than one that stays quiet."* There is also nothing to
# narrow with the estimator off, so the sentence would be wrong twice over.
_UNKNOWN_ANCHOR_HINT = (
    " If they give you a usage percentage and its reset time from the Account "
    "& Usage panel, run `claude-usage-anchor <pct> --resets-in <e.g. 4h20m>` "
    "— one reading anchors this window, and a second at least 5 points higher "
    "measures this machine's ratio."
)

# Distinguishes "the caller did not pass a surface" from "the caller passed
# None", which is a real and different state — an unrecognised entrypoint takes
# the relay, and a test has to be able to drive that branch explicitly.
_UNSET = object()

_CAUSE_WENT_QUIET = (
    "A status line was feeding it and has stopped — that command renders in "
    "the TERMINAL only, so on the VS Code extension and the desktop app the "
    "budget hooks have nothing to read."
)

_CAUSE_NEVER_FED = (
    "No status line has ever written the cache on this machine. Either "
    "claude-usage-line is not installed as the statusLine command (a custom "
    "one of your own will not write it), or this account authenticates by API "
    "key, where the rate-limit data never arrives at all. If it is the "
    "latter, set usage_watch.enabled to false rather than seeing this every "
    "session."
)

# The third case, and the reason the split is not two-way. A cache that exists
# but cannot be parsed has no readable `written_at`, so `cache_age` returns
# None and the age-driven split lands on _CAUSE_NEVER_FED: it tells someone
# whose status line is running normally that none was ever installed, and
# suggests turning the tool off. Nothing about the file's contents can be
# trusted here, but its EXISTENCE still rules that story out.
_CAUSE_UNREADABLE = (
    "The budget cache exists but could not be read — truncated, hand-edited, "
    "or written by an older version. A status line may well be feeding it "
    "normally; the file is what is broken. Deleting it is safe: the next "
    "status-line render writes a fresh one."
)


def _compose_unknown(age_clause: str, cause: str, surface,
                     estimate_on: bool = False) -> str:
    """The one place the truth half and the tail are joined.

    ONE composition site for both of `unknown_message`'s returns. They used to
    be two independent `.format()` calls on a single template, which was safe
    while the template was one string; with a conditional tail, two call sites
    is how the UNREADABLE branch quietly keeps a tail the other one drops.

    The tail is chosen on `is False` — POSITIVELY identified as a surface with
    no status line. `None` means "cannot tell" and takes the relay, so an
    entrypoint nobody has enumerated behaves exactly as it does today rather
    than going quiet on a guess.
    """
    try:
        if surface is False:
            tail = _UNKNOWN_STRUCTURAL
            # `is True`, not truthiness: the caller passes whatever
            # `is_estimate_enabled()` returned, and defaulting to False here
            # means a caller that cannot answer does not advertise a command
            # that may refuse. Absent config is the stricter case, per the
            # repo's rule for anything that affects what a hook tells someone
            # to run.
            if estimate_on is True:
                tail += _UNKNOWN_ANCHOR_HINT
        else:
            tail = _UNKNOWN_RELAY
        return (_UNKNOWN_TRUTH + tail).format(
            age=age_clause, cause=cause, owner=owner())
    except Exception:
        return ""


def unknown_message(age: Optional[float] = None,
                    presence: Optional[str] = None,
                    surface=_UNSET, estimate_on: bool = False) -> str:
    """PURE-ish (reads config for the owner name). What to tell the main loop
    when the budget cannot be read at all.

    Names a DIFFERENT cause for each of three situations, because they send
    someone to three unrelated places and a warning that misdiagnoses itself
    is one people learn to skip:

    * a readable cache that has gone stale — a feed stopped;
    * no cache file at all — no feed was ever configured;
    * a cache file the reader cannot use — the FILE is broken, and the status
      line behind it may be perfectly healthy.

    The third needs `presence` (from `cache_presence`), and it takes
    PRECEDENCE over the age, which is the part that is easy to get wrong: a
    file with unusable buckets and an intact `written_at` has a real, small
    age, so an age-first split renders "no live reading here (last cached
    reading is 0m old). A status line was feeding it and has stopped" about a
    file written seconds ago. The age clause is suppressed with it — the
    timestamp is only as trustworthy as the file carrying it, and quoting an
    age off a file we have just called unreadable invites someone to believe
    half of it.

    `presence` is optional and defaults to the old age-only split, wrong only
    for that third case; the one production caller passes it.

    The clause is likewise omitted rather than guessed when `age` is None —
    inventing a number would send someone looking for a file that does not
    exist.

    `surface` is the `status_line_surface()` verdict and decides only the TAIL,
    never the truth half. Left unset it resolves itself, so every existing
    caller keeps working; passed explicitly — including as `None` — it is used
    as given, which is what lets a test drive all three states hermetically.
    """
    resolved = status_line_surface() if surface is _UNSET else surface

    if presence == CACHE_UNREADABLE:
        return _compose_unknown("", _CAUSE_UNREADABLE, resolved, estimate_on)

    clause = ""
    cause = _CAUSE_NEVER_FED
    try:
        if age is not None and math.isfinite(float(age)):
            cause = _CAUSE_WENT_QUIET
            hours = float(age) / 3600.0
            clause = (" (last cached reading is {0:.0f}h old)".format(hours)
                      if hours >= 1 else
                      " (last cached reading is {0:.0f}m old)".format(
                          float(age) / 60.0))
    except (TypeError, ValueError):
        clause = ""
        cause = _CAUSE_NEVER_FED
    return _compose_unknown(clause, cause, resolved, estimate_on)


def tier_message(tier: str, bucket: str, pct: float, resets_at, now: float) -> str:
    """PURE-ish (reads config for the owner name). What to tell the main loop.

    Empty string for TIER_NONE: the whole point is silence until there is
    something worth interrupting the loop for.
    """
    template = _TIER_TEMPLATES.get(tier)
    if template is None:
        return ""
    try:
        return template.format(
            label=bucket_label(bucket), pct=float(pct),
            when=format_reset(resets_at, now), owner=owner())
    except Exception:
        return ""


# --- Tiers taken from an ESTIMATE, on a surface with no reading -------------
#
# Same thresholds as a real reading; the tier may speak off a derived number.
# The estimate is a floor, so a tier from it is never late by construction.

_ESTIMATE_TIER_LABELS = {
    TIER_NOTICE: "NOTICE", TIER_GATE: "GATE", TIER_HALT: "HALT",
}

# What makes a tier a work-stopper, kept per tier so a test can assert against
# it. NOTICE is empty on purpose: it is never a work-stopper.
_ESTIMATE_BLOCKING = {
    TIER_NOTICE: "",
    TIER_GATE: (
        " Finish the current unit of work, then STOP and raise an "
        "AskUserQuestion: stop clean here, push on, or drop to Sonnet. Do not "
        "start the next unit, and do not dispatch new subagents, before "
        "{owner} answers."
    ),
    TIER_HALT: (
        " Start no new plan task or unit of work and dispatch nothing. Commit "
        "what is open, write a handover (/handover), and report where it "
        "stopped."
    ),
}


def threshold_for(tier: str, notice: int, gate: int, halt: int) -> Optional[int]:
    """PURE. The configured number a tier fired on; None for no tier."""
    return {TIER_NOTICE: notice, TIER_GATE: gate, TIER_HALT: halt}.get(tier)


def estimate_tier_message(tier: str, pct, threshold, sentence: str) -> str:
    """PURE-ish (reads config for the owner name). A tier taken from an estimate.

    Empty string for an unknown tier or anything it cannot render.
    """
    label = _ESTIMATE_TIER_LABELS.get(tier)
    if label is None:
        return ""
    try:
        head = (
            "USAGE BUDGET — {0} FROM AN ESTIMATE: the 5h window is estimated "
            "at {1}%, past the {0} threshold of {2}%. ".format(
                label, pct, threshold))
        return (head + sentence + " Tell {0}.".format(owner())
                + _ESTIMATE_BLOCKING.get(tier, "").format(owner=owner()))
    except Exception:
        return ""


def hook_output(event_name: str, context: str) -> str:
    """The JSON string a hook prints so Claude Code injects `context` for the
    model. Shared by every hook in this tool — only `hookEventName` differs."""
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": event_name, "additionalContext": context}})


# --- Subagent headroom injection --------------------------------------------
#
# subagent_message is what claude-usage-subagent prints on SubagentStart. It
# informs, never blocks: the decision not to dispatch belongs to the architect
# model, because a hook refusing dispatches would fight that judgement and
# would fail closed on stale cache data. Below `notice` it says nothing at
# all — a subagent doing a two-minute job at 12% needs no preamble, and noise
# on every dispatch is what gets a feature switched off.

_SUBAGENT_TEMPLATE = (
    "USAGE BUDGET: {label} at {pct:.0f}% of the shared limit (resets {when}). "
    "You share this budget with the session that dispatched you. If this "
    "task cannot plausibly finish within that headroom, stop now and report "
    "BLOCKED — insufficient budget, naming what you would have needed. Do "
    "not start work you cannot finish; a half-applied change costs more "
    "than a clean stop."
)


def subagent_message(bucket: str, pct: float, resets_at, now: float,
                     agent_type: Optional[str] = None) -> str:
    """PURE-ish (reads no config; agent_type is accepted but unused in the
    text itself). What to tell a dispatched subagent about the budget it
    shares with its dispatcher.

    Empty string below `notice` — see the module note above this function.
    `agent_type` is accepted for callers that have it on hand, but is not
    otherwise required to compose the message.
    """
    tier = tier_for(pct, *thresholds())
    if tier == TIER_NONE:
        return ""
    try:
        return _SUBAGENT_TEMPLATE.format(
            label=bucket_label(bucket), pct=float(pct),
            when=format_reset(resets_at, now))
    except Exception:
        return ""


_ESTIMATED_SUBAGENT_TEMPLATE = (
    "USAGE BUDGET (ESTIMATED, not a reading): the 5-hour window is estimated "
    "at {pct}%{when}. "
    "No status line runs on this surface, so the figure is derived from this "
    "machine's transcript tokens and counts this machine only — read it as a "
    "floor, and note the 7-day window is invisible to it. You share this "
    "budget with the session that dispatched you. If this task cannot "
    "plausibly finish within that headroom, stop now and report BLOCKED — "
    "insufficient budget, naming what you would have needed. Do not start "
    "work you cannot finish; a half-applied change costs more than a clean "
    "stop."
)


def estimated_subagent_message(pct, resets_at, now: float) -> str:
    """PURE. The estimate-based fallback when no reading exists at all.

    Empty below `notice`, for a bool, or for a non-number. An unusable
    `resets_at` degrades to no clause rather than a wrong time.
    """
    if isinstance(pct, bool):
        return ""
    try:
        pct = int(pct)
    except (TypeError, ValueError):
        return ""
    if tier_for(pct, *thresholds()) == TIER_NONE:
        return ""
    try:
        when = format_reset(resets_at, now)
    except Exception:
        when = "?"
    when = "" if when in ("", "?") else ", {0}".format(when)
    try:
        return _ESTIMATED_SUBAGENT_TEMPLATE.format(pct=pct, when=when)
    except Exception:
        return ""


# --- Postmortem handover on a dying turn -------------------------------------
#
# claude-usage-postmortem fires on StopFailure, once the turn is already dead.
# No model runs here — there is nobody left to ask for a narrative — so this
# document is composed entirely from git state, gathered read-only by the
# executable itself (subprocess calls live there, not in this pure-decision
# module; see freshness_lib._git for the pattern to copy).
#
# Only `rate_limit` and `billing_error` are ours. StopFailure ships eight
# other reasons (overloaded, authentication_failed, oauth_org_not_allowed,
# invalid_request, model_not_found, server_error, max_output_tokens, unknown)
# that are transient or unrelated to budget; writing a handover for any of
# them would cry wolf on every ordinary API blip.
#
# No auto-commit, ever. Auto-committing a dying session's dirty tree onto a
# throwaway branch was considered and explicitly rejected — the document
# names the dirty files, and the human decides what to do with them.

POSTMORTEM_REASONS = ("rate_limit", "billing_error")


def safe_slug(value, fallback: str = "unknown") -> str:
    """PURE. A filesystem-safe filename fragment built from untrusted input.

    `session_id` and `reason` arrive in a hook payload and go straight into a
    filename. The danger here is not directory escape — the mandatory
    `<date>-budget-` prefix means a leading `../` never names a real directory —
    it is that `os.replace` then fails with ENOENT and the write is silently
    abandoned. A postmortem that writes nothing, reports nothing and exits 0 is
    worse than no postmortem at all, because it looks like it worked.

    Everything outside [A-Za-z0-9_-] becomes a hyphen, and the result is capped
    so a pathological id cannot blow past a filesystem's name limit.
    """
    text = "" if value is None else str(value)
    cleaned = "".join(ch if (ch.isalnum() and ch.isascii()) or ch in "-_" else "-"
                      for ch in text)
    cleaned = cleaned.strip("-")[:64].strip("-")
    return cleaned or fallback


def should_postmortem(reason) -> bool:
    """PURE. Exact membership in POSTMORTEM_REASONS.

    Never a substring or prefix test, and `None`/`""` both read as False —
    the common "no StopFailure yet" and "not our reason" cases must degrade
    to silence, not to a spurious handover.
    """
    return reason in POSTMORTEM_REASONS


def handover_dir() -> str:
    """Where the handover document is written. Same directory `/handover`
    writes into and `claude-thread-deliver` sweeps, so this postmortem is
    picked up by the existing next-session flow rather than inventing a
    second one.

    Wrapped so a missing or broken config never raises inside a hook — same
    shape as every other config read in this module.
    """
    default = os.path.expanduser("~/.claude/handovers")
    if _config is None:
        return default
    try:
        return _config.handover_dir()
    except Exception:
        return default


def postmortem_document(reason: str, cwd, branch, dirty, buckets: dict,
                        now: float, error_details=None,
                        partial: bool = False) -> str:
    """The handover markdown, built entirely from git state.

    `dirty` is a list of `git status --porcelain` lines, or `[]` for a clean
    tree. `buckets` is the same shape `extract_buckets` produces. Never
    claims anything was committed — the dirty files are named for the human
    to review, never touched.

    `error_details` is StopFailure's `error_details` field, present on the
    real payload and carrying the human-readable limit message. Optional and
    printed verbatim (newlines flattened) when present; omitted entirely
    otherwise rather than printing "None".

    `partial` says the reading behind `buckets` was `READING_PARTIAL` — at
    least one window had already reset and is therefore absent. It must be
    labelled, because this document is retrospective: the window most likely
    to have just rolled is the one that stopped the turn, so an unlabelled
    "7-day at 8%" names a window with headroom in a document whose reason
    line says the budget did the killing.
    """
    date = time.strftime("%Y-%m-%d", time.localtime(now))
    lines = [
        "# Turn stopped on usage budget: {0} ({1})".format(reason, date),
        "",
        "Written by claude-usage-postmortem when the turn died on "
        "`StopFailure` reason `{0}`. No model ran to produce this document "
        "— it is built entirely from git state.".format(reason),
        "",
        "## Where it stopped",
        "",
        "- Branch: {0}".format(branch if branch else "unknown"),
        "- Directory: {0}".format(cwd if cwd else "unknown"),
    ]

    found = binding(buckets) if isinstance(buckets, dict) else None
    if found:
        name, pct, resets_at = found
        lines.append("- Budget at death: {0} at {1:.0f}% (resets {2})".format(
            bucket_label(name), pct, format_reset(resets_at, now)))
        if partial:
            lines.append(
                "- That reading was PARTIAL: at least one other window had "
                "already reset and is not shown above — possibly the one that "
                "stopped the turn. Treat the figure above as the only window "
                "still visible, not as proof there was headroom.")
    else:
        lines.append("- Budget at death: not recorded")

    if error_details:
        try:
            detail_text = str(error_details).replace("\n", " ").strip()
        except Exception:
            detail_text = ""
        if detail_text:
            lines.append("- Error detail: {0}".format(detail_text))

    lines.append("")
    lines.append("## Working tree")
    lines.append("")
    if dirty:
        lines.append("Dirty. Nothing below has been committed — review it "
                     "yourself before you commit anything:")
        lines.append("")
        for entry in dirty:
            lines.append("    {0}".format(entry))
    elif dirty is None:
        # Not the same as clean, and the difference is the whole point of this
        # section: "clean" tells a human they can stop looking, and saying it
        # when git was never successfully consulted is the one way this document
        # can actively mislead.
        lines.append("Could not be determined — `git status` did not answer "
                     "here. Check by hand before assuming nothing was lost.")
    else:
        lines.append("Working tree was clean when the turn died.")

    lines.append("")
    lines.append("## What to do next")
    lines.append("")
    lines.append("1. Run `git status` in `{0}` to confirm the state above is "
                 "still current.".format(cwd if cwd else "the working directory"))
    lines.append("2. Resume on branch `{0}`.".format(branch if branch else "unknown"))
    lines.append("3. Review and commit the dirty files above yourself — none "
                 "of them were touched automatically.")

    return "\n".join(lines)
