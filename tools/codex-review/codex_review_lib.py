#!/usr/bin/env python3
"""Pure helpers for reading Codex's rate-limit meter out of `~/.codex/sessions`
rollout logs, and for formatting what they say.

`sessions_root` is a REQUIRED parameter on every function here, never
defaulted. Defaulting it to `~/.codex/sessions` is what makes an executable
built on top of this module untestable, so the executable takes a
`--sessions-root` seam instead. `default_sessions_root()` is the one place
in this module that resolves that default; every other function here still
takes `sessions_root` explicitly.

The real log shape (measured against a live rollout file):

    {"type": "event_msg",
     "payload": {"type": "token_count", "rate_limits": {"primary": {...}, ...}}}

`rate_limits` sits at `payload.rate_limits`, NOT top level. A reader doing
`obj.get("rate_limits")` silently returns None on every real line -- there is
no top-level key to find; `test_codex_review.py` pins the nested shape.

The newest rollout file does NOT reliably carry the rate_limits block, and
it is not reliably a fixed number of files back either. Neither direction is
safe to assume, so every walk here is newest-first and keeps going until it
finds one, rather than trusting the first or the newest file specifically.
"""

import datetime
import glob
import json
import math
import os
import re
import shutil
import time


def default_sessions_root():
    """The default `sessions_root` Codex itself uses: `$CODEX_HOME/sessions`
    when `CODEX_HOME` is set to a non-empty value (inferred from Codex's
    behaviour, not documented), else
    `~/.codex/sessions`. This is the ONE place in this module that resolves
    that default -- see the module docstring. `--sessions-root` still
    overrides this for every caller; nothing here reads `CODEX_HOME` again."""
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return os.path.join(codex_home, "sessions")
    return os.path.expanduser("~/.codex/sessions")


def _rollout_paths(sessions_root):
    """Every `rollout-*.jsonl` file under `sessions_root`, at any depth.

    Returns `[]` for a missing or empty root. Never raises -- a permissions
    error or a root that is a file rather than a directory both fall
    through to `[]` via the `isdir` guard, and `glob.glob` itself does not
    raise on an unreadable entry, it just omits it.
    """
    if not sessions_root or not os.path.isdir(sessions_root):
        return []
    return glob.glob(os.path.join(sessions_root, "**", "rollout-*.jsonl"),
                      recursive=True)


def _mtime_or_none(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


# Public alias -- `_mtime_or_none` keeps its name (and every internal caller
# keeps using it) so this is purely an additional public entry point for
# callers outside this module.
mtime_or_none = _mtime_or_none


def _newest_first(paths):
    """`paths` sorted newest-mtime-first. A path whose mtime cannot be read
    (e.g. it vanished mid-walk) sorts last rather than raising."""
    return sorted(paths, key=lambda p: (_mtime_or_none(p) is None, -(_mtime_or_none(p) or 0)))


def _last_rate_limits_block(path):
    """The `rate_limits` mapping from the LAST line in `path` that carries
    one at `payload.rate_limits`, or None if the file has none, cannot be
    opened, or is not valid line-delimited JSON.

    Deliberately tolerant line-by-line: one garbage line (unparseable JSON,
    JSON that parses but isn't an object, a `payload` that isn't a mapping)
    is skipped rather than aborting the whole file, because a single bad
    line must not hide a real block sitting elsewhere in the same file.
    """
    last_block = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                block = payload.get("rate_limits")
                if isinstance(block, dict):
                    last_block = block
    except OSError:
        return None
    return last_block


def read_rate_limits(sessions_root):
    """Walk `<sessions_root>/**/rollout-*.jsonl` newest-first by mtime and
    return the `rate_limits` mapping from the last line that carries one, in
    the first (newest) file that has one at all.

    Returns `None` on a missing root, an empty root, a tree with no
    `rate_limits` block anywhere, or unparseable content. Never raises --
    every failure mode collapses to `None` rather than propagating, because
    this is a meter reader that runs alongside a dispatch that must not be
    derailed by a corrupt log.
    """
    try:
        for path in _newest_first(_rollout_paths(sessions_root)):
            block = _last_rate_limits_block(path)
            if block is not None:
                return block
        return None
    except BaseException:
        return None


def snapshot_meter(sessions_root):
    """Like `read_rate_limits`, but also returns the rollout file the block
    came from -- `(path, block)`, or `(None, None)` on a missing root, an
    empty root, or a tree with no `rate_limits` block anywhere.

    `read_run_delta`'s `before_path` needs the EXACT file a "before" reading
    came from as its baseline; `read_rate_limits` throws that away and
    returns only the mapping, which leaves nothing correct to pass. This is
    the path-carrying sibling that exists to close that gap -- it is not a
    replacement for `read_rate_limits`, which keeps its own callers
    unchanged.

    Shares `_rollout_paths`, `_newest_first` and `_last_rate_limits_block`
    with `read_rate_limits` rather than re-walking independently, so the two
    can never select a different file for the same tree. Never raises, for
    the same reason `read_rate_limits` never does: this is a meter reader
    that runs alongside a dispatch that must not be derailed by a corrupt
    log.
    """
    try:
        for path in _newest_first(_rollout_paths(sessions_root)):
            block = _last_rate_limits_block(path)
            if block is not None:
                return path, block
        return None, None
    except BaseException:
        return None, None


def _primary_used_percent(block):
    """`block["primary"]["used_percent"]` if that shape holds and the value
    is numeric, else None. Never raises on a malformed block."""
    if not isinstance(block, dict):
        return None
    primary = block.get("primary")
    if not isinstance(primary, dict):
        return None
    used_percent = primary.get("used_percent")
    if isinstance(used_percent, bool) or not isinstance(used_percent, (int, float)):
        return None
    return float(used_percent)


def read_run_delta(sessions_root, before_path):
    """The COST reader, not the headroom reader.

    No production caller; `run_cost` replaced it because one run's integer
    meter delta is quantisation noise (most runs round to a 0 or 1 point
    delta regardless of their real token cost). Left in place, with its own
    tests, rather than deleted.

    `before_path` names the rollout file that was the "before" reading --
    the file a caller already read a `rate_limits` block from prior to
    dispatching a review. This function reads that file's own last block as
    the baseline, then looks for a `rate_limits` block in a file whose mtime
    is strictly NEWER than `before_path`'s. If it finds one, it returns
    `after.primary.used_percent - before.primary.used_percent`. If it does
    not -- no file is newer, or no newer file carries a block, or the
    baseline itself is unreadable -- it returns `None`, meaning "this run
    produced no meter event."

    It must NEVER fall back to a file that is not strictly newer than
    `before_path`. Reusing `read_rate_limits`'s "walk newest-first until any
    file has a block" logic here would, on a run that produced no new event,
    walk straight back to `before_path` itself (the newest file available)
    and subtract it from itself -- a fabricated 0.0 that looks exactly like
    a real, measured zero-cost review and is not.
    """
    try:
        if not before_path or not os.path.isfile(before_path):
            return None
        before_mtime = _mtime_or_none(before_path)
        if before_mtime is None:
            return None
        before_block = _last_rate_limits_block(before_path)
        before_percent = _primary_used_percent(before_block)
        if before_percent is None:
            return None

        newer_paths = [p for p in _rollout_paths(sessions_root)
                        if (_mtime_or_none(p) or -1) > before_mtime]
        for path in _newest_first(newer_paths):
            after_block = _last_rate_limits_block(path)
            after_percent = _primary_used_percent(after_block)
            if after_percent is not None:
                return after_percent - before_percent
        return None
    except BaseException:
        return None


# Keyed by (plan_type, window_minutes, model) -> {"points_per_million_uncached":
# float, "gate_points": (median, p90)}. Empty on purpose: a fit is one
# account's measurement of one window and does not transfer. Fit your own
# from your rollout logs (docs/build-measurements.md, "An external
# reviewer's quota"), one key per plan, window and model, and never rescale
# one key into another -- there is no safe conversion factor between them.
# With no entry for a key, every caller reports the meter's used percent
# and no gate count.
CALIBRATIONS = {}


def calibration(block, model):
    """The `CALIBRATIONS` entry for `(block["plan_type"],
    block["primary"]["window_minutes"], model)`, or None when `block` is
    malformed or there is no fit for that exact combination. Never raises."""
    try:
        if not isinstance(block, dict):
            return None
        primary = block.get("primary")
        if not isinstance(primary, dict):
            return None
        key = (block.get("plan_type"), primary.get("window_minutes"), model)
        return CALIBRATIONS.get(key)
    except BaseException:
        return None


def gates_left(block, now, model):
    """How many more review gates the meter in `block` can afford, as of
    `now` (a `time.time()`-style epoch second) -- an ESTIMATE, bounded by
    `calibration`'s per-gate median/p90, never a guarantee (100% used does not
    verifiably mean a gate cannot run).

    None when `block` carries no readable `primary.used_percent`, or
    `primary.resets_at` is missing, a bool, not a number, or not strictly
    after `now` -- a window that has already reset (or resets this instant)
    describes a meter reading nobody should act on.

    Otherwise a dict: `used_percent`, `resets_at`, `window_minutes` (or
    None), `plan_type` (or None), `model`, and `low`/`high` -- the estimated
    gate range, `int(headroom // p90)` / `int(headroom // median)` with
    `headroom = max(0.0, 100.0 - used_percent)`, when `calibration(block,
    model)` exists, else both None (an uncalibrated plan/window/model still
    reports `used_percent`, just no gate count). Never raises.
    """
    try:
        used_percent = _primary_used_percent(block)
        if used_percent is None:
            return None

        primary = block["primary"]  # a dict: _primary_used_percent checked it
        resets_at = primary.get("resets_at")
        if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
            return None
        if resets_at <= now:
            return None

        window_minutes = primary.get("window_minutes")
        if isinstance(window_minutes, bool) or not isinstance(window_minutes, (int, float)):
            window_minutes = None
        plan_type = block.get("plan_type")

        headroom = max(0.0, 100.0 - used_percent)
        cal = calibration(block, model)
        if cal is not None:
            median, p90 = cal["gate_points"]
            low = int(headroom // p90)
            high = int(headroom // median)
        else:
            low = None
            high = None

        return {
            "used_percent": used_percent,
            "resets_at": resets_at,
            "window_minutes": window_minutes,
            "plan_type": plan_type,
            "model": model,
            "low": low,
            "high": high,
        }
    except BaseException:
        return None


def format_gates_left(result, snapshot_mtime):
    """A one-line human string for `gates_left`'s `result`, or `""` when
    `result` is None -- the caller decides whether to print anything at
    all. `resets_at` and `snapshot_mtime` are formatted via
    `time.localtime`, both `"%a %m-%d %H:%M"`. `snapshot_mtime` of None
    drops the " as of {snap}" clause entirely rather than printing a
    placeholder. Never raises (returns "" on any failure, same as the None
    case -- there is nothing safer to print)."""
    try:
        if result is None:
            return ""

        used = result["used_percent"]
        window_minutes = result.get("window_minutes")
        if isinstance(window_minutes, (int, float)) and not isinstance(window_minutes, bool):
            if window_minutes % 1440 == 0:
                window = "%d-day" % (window_minutes // 1440)
            else:
                window = "%d-minute" % window_minutes
        else:
            window = "current"

        reset = time.strftime("%a %m-%d %H:%M", time.localtime(result["resets_at"]))
        as_of = ""
        if snapshot_mtime is not None:
            snap = time.strftime("%a %m-%d %H:%M", time.localtime(snapshot_mtime))
            as_of = " as of %s" % snap

        low = result.get("low")
        high = result.get("high")

        if low is not None and high is not None:
            if low == high:
                head = "codex budget (estimate): %d review gates left" % low
            else:
                head = "codex budget (estimate): ≈%d–%d review gates left" % (low, high)
            tail = "%.0f%% of the %s window used%s, resets %s" % (
                used, window, as_of, reset)
            return "%s — %s" % (head, tail)

        plan_type = result.get("plan_type")
        model = result.get("model")
        head = "codex budget: %.0f%% of the %s window used%s, resets %s" % (
            used, window, as_of, reset)
        tail = ("no fitted per-gate cost for plan %s / model %s, so no "
                "gate count") % (plan_type, model)
        return "%s — %s" % (head, tail)
    except BaseException:
        return ""


def window_key(resets_at):
    """The dedupe key a caller (`claude-usage-watch`'s Codex line) should
    record a `gates_left` result's `resets_at` against: rounded UP to the
    top of the hour (`math.ceil`), never to the NEAREST hour.

    Jitter moves `resets_at` by a few seconds across rollout files describing
    the same window, and a naive `round()` can land the key BEFORE
    `resets_at` itself whenever the true value sits just past a half-hour
    midpoint. `record_announcement`'s prune drops any record whose stored
    `resets_at` is `<= now` -- so a key rounded down could be written and
    pruned on the very same write that created it, silently re-arming the
    dedupe on the next prompt in the same session. Rounding UP guarantees
    `window_key(x) >= x` for every `x`, so the key can never be stale
    before the window it describes has even reset.

    Residual, accepted: a reset within the jitter AFTER an exact hour boundary
    (HH:00:00) can still re-announce once -- the two jittered readings
    round up to different hours. Never raises: a non-numeric `resets_at`
    (including a bool, which is technically an int) returns None.
    """
    if isinstance(resets_at, bool) or not isinstance(resets_at, (int, float)):
        return None
    try:
        return math.ceil(resets_at / 3600.0) * 3600
    except BaseException:
        return None


def _session_meta(path):
    """The `payload` mapping from `path`'s FIRST line, when that line parses
    as JSON, is a dict, and carries `type == "session_meta"` -- else
    None. Reads defensively rather than assuming the premise holds for every
    file; a file that does not start with a session_meta line simply cannot
    be matched by `run_cost`."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError:
        return None
    first_line = first_line.strip()
    if not first_line:
        return None
    try:
        obj = json.loads(first_line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return None
    return payload


def _last_model_and_uncached(path):
    """Scan every line of `path` for the last `turn_context.payload.model`
    (None if no such line) and the last line whose `payload.info.
    total_token_usage` is a dict (`info` is null on some lines, so a
    bare `payload.get("info")` must be type-checked, not just presence
    checked). Returns `(model, uncached)`, where `uncached` is
    `input_tokens - cached_input_tokens` with missing keys treated as 0 and
    the result floored at 0, or None if no `total_token_usage` line exists
    anywhere in the file. Never raises -- an unreadable or malformed file
    yields `(None, None)`."""
    model = None
    uncached = None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                if obj.get("type") == "turn_context":
                    candidate_model = payload.get("model")
                    if candidate_model is not None:
                        model = candidate_model
                info = payload.get("info")
                if isinstance(info, dict):
                    usage = info.get("total_token_usage")
                    if isinstance(usage, dict):
                        input_tokens = usage.get("input_tokens") or 0
                        cached_tokens = usage.get("cached_input_tokens") or 0
                        uncached = max(0, input_tokens - cached_tokens)
    except OSError:
        return None, None
    return model, uncached


def _attributed_rollout(sessions_root, cwd, since):
    """The ONE rollout file under `sessions_root` that `run_cost`'s three
    filters attribute to a dispatch, else None. Shared by `run_cost` and
    `rollout_session_id` so the two can never attribute different files.

    The filters: mtime `>= since`; `session_meta.payload.cwd` resolves
    (`os.path.realpath`) to the same place as `cwd`; `session_meta.payload.
    timestamp`, parsed AS UTC (`datetime.fromisoformat(ts.replace("Z",
    "+00:00")).timestamp()`), is `>= since - 5` (a few seconds of slack for
    the session starting an instant before `since` was captured). Zero
    matches or two-or-more matches both return None -- an ambiguous
    attribution is not a measurement. Never raises."""
    try:
        target_cwd = os.path.realpath(cwd)
        matches = []
        for path in _rollout_paths(sessions_root):
            mtime = _mtime_or_none(path)
            if mtime is None or mtime < since:
                continue

            meta = _session_meta(path)
            if meta is None:
                continue
            meta_cwd = meta.get("cwd")
            if not isinstance(meta_cwd, str):
                continue
            if os.path.realpath(meta_cwd) != target_cwd:
                continue

            ts = meta.get("timestamp")
            if not isinstance(ts, str):
                continue
            try:
                parsed_ts = datetime.datetime.fromisoformat(
                    ts.replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                continue
            if parsed_ts < since - 5:
                continue

            matches.append(path)

        if len(matches) != 1:
            return None
        return matches[0]
    except BaseException:
        return None


def rollout_session_id(sessions_root, cwd, since):
    """The `session_meta.payload.id` string of the rollout file
    `_attributed_rollout` picks for this dispatch, else None (no unique
    attribution, or the id is missing or not a string). Never raises."""
    try:
        path = _attributed_rollout(sessions_root, cwd, since)
        if path is None:
            return None
        meta = _session_meta(path)
        session_id = meta.get("id") if meta is not None else None
        return session_id if isinstance(session_id, str) else None
    except BaseException:
        return None


def run_cost(sessions_root, cwd, since):
    """The per-run COST reader that replaced `read_run_delta`: the uncached
    input tokens a single Codex run burned, attributed to it by file
    identity rather than by meter delta (which quantises to whole points and
    is noise for one run).

    None unless EXACTLY ONE rollout file under `sessions_root` passes all
    three filters: its mtime is `>= since`; its `session_meta.payload.cwd`
    resolves (`os.path.realpath`) to the same place as `cwd`; its
    `session_meta.payload.timestamp`, parsed AS UTC
    (`datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()`), is
    `>= since - 5` (a few seconds of slack for the session starting an
    instant before `since` was captured). Zero matches or two-or-more
    matches both return None -- an ambiguous attribution is not a
    measurement.

    For the one match: `model` is the last `turn_context.payload.model`
    (None if absent); `uncached` comes from `_last_model_and_uncached`, and
    if the file has no `total_token_usage` line at all this function returns
    None outright, since there is no token count to report. `block` is the
    file's last `rate_limits` block (`_last_rate_limits_block`, may be
    None). Returns `{"path", "model", "uncached_input": int, "points":
    float|None, "plan_type": str|None}`, where `points` is `uncached / 1e6 *
    cal["points_per_million_uncached"]` when `calibration(block, model)`
    exists, else None. Never raises.
    """
    try:
        path = _attributed_rollout(sessions_root, cwd, since)
        if path is None:
            return None
        model, uncached = _last_model_and_uncached(path)
        if uncached is None:
            return None

        block = _last_rate_limits_block(path)
        plan_type = block.get("plan_type") if isinstance(block, dict) else None
        cal = calibration(block, model)
        points = None
        if cal is not None:
            points = uncached / 1e6 * cal["points_per_million_uncached"]

        return {
            "path": path,
            "model": model,
            "uncached_input": int(uncached),
            "points": points,
            "plan_type": plan_type,
        }
    except BaseException:
        return None


# The model and reasoning effort each Codex gate runs at. Pinned, because an
# omitted `-m` inherits ~/.codex/config.toml: ambient state that can change
# between two gates and still produce a normal-looking report from a
# different reviewer. The Code gate -- the most frequent and narrowest --
# runs on a cheaper model; Plan, Design and Merge run on the stronger one,
# so a silent change still gets the stronger reviewer at Merge. A model the
# signed-in account does not serve fails loudly (HTTP 400), never by
# falling back. `codex_review.models.<gate>` in the toolkit config
# overrides a pin; see `resolve_gate_model`.
CODEX_MODEL = "gpt-6-astra"
CODE_GATE_MODEL = "gpt-5.6-terra"
CODEX_REASONING_EFFORT = "high"

GATES = ("code", "merge", "plan", "design", "architecture")

GATE_MODELS = {
    "code": CODE_GATE_MODEL,
    "merge": CODEX_MODEL,
    "plan": CODEX_MODEL,
    "design": CODEX_MODEL,
    "architecture": CODEX_MODEL,
}


def model_for_gate(gate):
    """The pinned model for one gate. An unknown gate raises rather than
    falling back to a default: a fallback would dispatch a review on a
    model nobody chose, and the report would read exactly like a normal
    one."""
    if gate not in GATE_MODELS:
        raise ValueError("unknown Codex gate %r (expected one of %s)" %
                         (gate, ", ".join(sorted(GATE_MODELS))))
    return GATE_MODELS[gate]


class ConfigError(ValueError):
    """The `codex_review.models` config value is unusable. Raised instead of
    falling back to a pin: a misspelled override that quietly reverted to the
    default would dispatch a review on a model the user did not choose."""


def valid_model_name(value):
    """True iff `value` is a string usable as a `-m` argument: non-empty, no
    whitespace, and not starting with `-` (which `codex` would parse as a
    flag). `bool` is not a `str`, and neither is anything else non-string."""
    return (isinstance(value, str) and value != ""
            and not any(c.isspace() for c in value)
            and not value.startswith("-"))


def resolve_gate_model(gate, models_config):
    """`(model, source)` for one gate, `source` being "configured" when the
    toolkit config's `codex_review.models` names a model for it, else
    "default" (the pin from `model_for_gate`).

    `models_config` is the raw `codex_review.models` value: None means not
    set. Anything but None or a dict raises `ConfigError`. EVERY key and
    value is checked, not only `gate`'s, so a typo under one gate is caught
    by whichever gate runs first rather than lurking until its own gate
    does. An unknown `gate` raises `ValueError`, as `model_for_gate` does."""
    default_model = model_for_gate(gate)
    if models_config is None:
        return default_model, "default"
    if not isinstance(models_config, dict):
        raise ConfigError("codex_review.models must be an object mapping "
                          "gate names to model names")
    for key, value in models_config.items():
        if key not in GATES:
            raise ConfigError("codex_review.models.%s is not a gate "
                              "(expected one of %s)"
                              % (key, ", ".join(sorted(GATES))))
        if value is not None and not valid_model_name(value):
            raise ConfigError("codex_review.models.%s must be null or a "
                              "model name: a non-empty string with no "
                              "whitespace that does not start with '-' "
                              "(got %r)" % (key, value))
    configured = models_config.get(gate)
    if configured is not None:
        return configured, "configured"
    return default_model, "default"


CODEX_AUTH_PATH = os.path.join("~", ".codex", "auth.json")


def unavailable_reason(home=None):
    """None when Codex can be used -- `codex` on PATH and its auth file
    readable -- else a sentence saying which is missing. `home` replaces the
    user's home directory when given. Never raises: this runs as a preflight
    where a crash would hide the very message it exists to give."""
    try:
        if shutil.which("codex") is None:
            return "the codex binary is not on PATH"
        base = home if home is not None else os.path.expanduser("~")
        auth_path = os.path.join(base, CODEX_AUTH_PATH[2:])  # drop "~/"
        try:
            with open(auth_path, "rb"):
                pass
        except OSError:
            return ("the Codex auth file is missing or unreadable: %s"
                    % auth_path)
        return None
    except BaseException as exc:  # noqa: BLE001 -- see the docstring
        return "Codex availability could not be checked: %r" % (exc,)


def model_rejected(stderr_text):
    """True iff `stderr_text` carries the measured shape of Codex's error for
    a model the signed-in account does not serve: an HTTP `"status":400` and
    the phrase `model is not supported`. Any other failure stays an ordinary
    non-zero exit, so a network error or a bad request is never misreported
    as an unavailable model. `bool` and non-strings are not text."""
    if not isinstance(stderr_text, str):
        return False
    return (re.search(r'"status"\s*:\s*400', stderr_text) is not None
            and "model is not supported" in stderr_text)


# Architecture review: `max` effort, read-only, on the `architecture` gate's
# model (GATE_MODELS, overridable like every gate). `ultra` is deliberately
# not used, and the CLI does not validate the value, so a typo here would
# pass silently.
ARCHITECTURE_EFFORT = "max"
ARCHITECTURE_SANDBOX = "read-only"


def _architecture_config_args():
    return ["-c", 'model_reasoning_effort="%s"' % ARCHITECTURE_EFFORT,
            "-c", 'sandbox_mode="%s"' % ARCHITECTURE_SANDBOX]


def build_architecture_argv(repo_path, schema_path, output_path, prompt, model):
    """The `codex` argv for an architecture review, as a list. The prompt is
    the only positional and is last."""
    return (["codex", "exec", "-C", repo_path, "-m", model]
            + _architecture_config_args()
            + ["--output-schema", schema_path, "-o", output_path, prompt])


def build_resume_argv(session_id, schema_path, output_path, prompt, model):
    """The `codex exec resume` argv. Probed 2026-10-20: a resumed session
    silently reverts reasoning effort and sandbox unless both `-c` pairs are
    re-passed, and `resume` has no `-s`/`-C` to do it with."""
    return (["codex", "exec", "resume", session_id, "-m", model]
            + _architecture_config_args()
            + ["--output-schema", schema_path, "-o", output_path, prompt])


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def weekly_reading(block, now):
    """The 7-day window of a rate-limits `block`, or None. Either of
    `primary`/`secondary` may be the weekly one (window_minutes == 10080).
    None when the block or window is unreadable or the reset has passed.
    Never raises."""
    if not isinstance(block, dict):
        return None
    for key in ("primary", "secondary"):
        window = block.get(key)
        if not isinstance(window, dict):
            continue
        if window.get("window_minutes") != 10080:
            continue
        used = window.get("used_percent")
        resets_at = window.get("resets_at")
        # An unreadable or expired weekly candidate is skipped, not fatal:
        # it must not mask a readable weekly window in the other slot.
        if not _is_number(used) or not _is_number(resets_at):
            continue
        if not math.isfinite(used) or not math.isfinite(resets_at):
            continue
        if resets_at <= now:
            continue
        return {"used_percent": float(used), "resets_at": float(resets_at),
                "window_minutes": 10080}
    return None


def format_weekly(reading):
    """One line for the weekly meter; `reading` is `weekly_reading`'s result."""
    if reading is None:
        return "codex weekly: unknown (no readable 7-day window)"
    reset = time.strftime("%a %m-%d %H:%M",
                          time.localtime(reading["resets_at"]))
    return "codex weekly: %s%% used, resets %s" % (reading["used_percent"],
                                                   reset)


CORE_LENSES = ("coherence", "elegance", "rebuild", "unaccounted")
DESIGN_LENSES = ("wrong-premise", "simpler-alternative", "silent-failure",
                 "already-exists", "seams")


def _blank(value):
    return not isinstance(value, str) or value.strip() == ""


def _lens_has_evidence(report, lens):
    return any(v.get("lens") == lens and v.get("evidence")
               for v in report["lens_verdicts"])


def architecture_violation(report, expected_round, expected_moment):
    """None for a schema-valid architecture report that is also semantically
    sound, else the first reason it is not. The schema cannot see string
    content, so blank evidence and a final report with no lens coverage pass
    it; this is the check that rejects them."""
    if report["round"] != expected_round:
        return "round %r, expected %r" % (report["round"], expected_round)
    if report["moment"] != expected_moment:
        return "moment %r, expected %r" % (report["moment"], expected_moment)
    executed = report["executed"]
    if executed["ran"]:
        if not executed["commands"] or any(_blank(c)
                                           for c in executed["commands"]):
            return "executed.ran is true but commands is empty or blank"
    elif _blank(executed["why_not"]):
        return "executed.ran is false but why_not is blank"

    for request in report["context_requests"]:
        for field in ("id", "question", "why_needed"):
            if _blank(request[field]):
                return "blank context_requests %s" % field
    for verdict in report["lens_verdicts"]:
        if _blank(verdict["verdict"]):
            return "blank verdict for lens %s" % verdict["lens"]
        if any(_blank(e) for e in verdict["evidence"]):
            return "blank evidence item for lens %s" % verdict["lens"]
    for principle in report["principles"]:
        for field in ("statement", "why"):
            if _blank(principle[field]):
                return "blank principle %s" % field
    for change in report["changes"]:
        for field in ("id", "title", "what", "why", "serves_objective",
                      "preserve", "done_when"):
            if _blank(change[field]):
                return "blank change %s (%r)" % (field, change["id"])
        if any(_blank(e) for e in change["evidence"]):
            return "blank evidence item in change %r" % change["id"]
    if any(_blank(item) for item in report["unverified"]):
        return "blank unverified item"

    if report["status"] == "final":
        if report["context_requests"]:
            return "final report still has context_requests"
        # The fixed brief asks for two to four binding principles.
        if not 2 <= len(report["principles"]) <= 4:
            return ("final report has %d principles (2-4 required)"
                    % len(report["principles"]))
        for lens in CORE_LENSES:
            if not _lens_has_evidence(report, lens):
                return "no evidenced verdict for core lens %s" % lens
        for change in report["changes"]:
            if not change["evidence"]:
                return "change %r has no evidence" % change["id"]
        if report["changes"] and _blank(report["implementation_sketch"]):
            return "changes present but implementation_sketch is blank"
        if expected_moment == "design":
            for lens in DESIGN_LENSES:
                if not _lens_has_evidence(report, lens):
                    return "no evidenced verdict for design lens %s" % lens
    if report["status"] == "needs_context":
        count = len(report["context_requests"])
        if not 1 <= count <= 10:
            return "needs_context with %d context_requests (1-10 allowed)" % count
    if expected_round == 3 and report["status"] != "final":
        return "round 3 must be final, got %r" % report["status"]
    return None


def build_argv(repo_path, schema_path, output_path, prompt, model,
               skip_git_check=False):
    """The exact `codex` argv for every gate's dispatch, as a list --
    never a shell string, since there is no shell to strip quoting.

    ```
    codex exec -C <repo_path>
      -m <model>                         # model_for_gate(gate)
      -c model_reasoning_effort="high"   # CODEX_REASONING_EFFORT
      -c sandbox_mode="workspace-write"
      --output-schema <schema_path>
      -o <output_path>
      [--skip-git-repo-check]      # only when skip_git_check is True
      <prompt>                     # the ONLY positional, ALWAYS LAST
    ```

    **`codex exec review --base` is DEAD -- measured, not reasoned.** Two
    live runs, the second against a schema legal at every object level:
    `codex exec review --output-schema <schema> -o <file>`
    exits 0 and writes PROSE to `<file>`, not structured output. `-o` is
    `--output-last-message` -- the agent's final message, not a JSON sink.
    This function used to build `codex exec -C <repo_path> review --base
    <base_ref> ...`; that form is superseded, not an alternative still on
    the table. There is no longer any `review` subcommand, so `base_ref` is
    gone from this signature entirely -- the caller generates a diff
    mechanically and puts it on stdin, which this pure
    function never sees.

    `-C <repo_path>` has no subcommand to precede any more, so its position
    is unconstrained -- do not re-add an ordering assertion about it.

    **`<prompt>` is the ONLY positional and it goes LAST.** This is the
    invariant that replaces the old `-C`-before-`review` ordering rule, and
    it fails the same way: a positional in the wrong place (e.g. wedged
    right after `-C`) is silently consumed as a flag's value, producing an
    argv that parses fine and reviews with no instructions. A test that only
    asserts the prompt is PRESENT passes that mutation -- membership does
    not see position.

    `repo_path` is supplied by the caller, not chosen here -- it is what
    makes Code Review and Merge Review different gates, and this function
    has no opinion about which. Same for `prompt`: it is read from a fixed
    house-brief file by the caller, and placed here, never authored here --
    that is what keeps `build_argv` pure (see below).

    `model` is REQUIRED, with no default: a default would let a caller that
    forgot its gate's model dispatch silently on the wrong model, and the per-gate
    split would be off with nothing to say so. Resolve it with
    `model_for_gate`.

    This function is PURE: no filesystem access, no subprocess, no env read.
    That is why `skip_git_check` is an explicit boolean parameter rather than
    something decided in here -- the caller makes that decision,
    against the real filesystem.
    """
    argv = [
        "codex", "exec", "-C", repo_path,
        "-m", model,
        "-c", 'model_reasoning_effort="%s"' % CODEX_REASONING_EFFORT,
        "-c", 'sandbox_mode="workspace-write"',
        "--output-schema", schema_path,
        "-o", output_path,
    ]
    if skip_git_check:
        argv.append("--skip-git-repo-check")
    argv.append(prompt)
    return argv


def format_headroom(block):
    """A one-line human string describing `block` (a `rate_limits` mapping
    as returned by `read_rate_limits`, or `None`).

    `None` in -- or any block missing a readable `primary.used_percent` --
    yields a string SAYING the meter is unreadable, never an empty string:
    an empty line is silence, and silence on a cost surface reads as "all
    clear" rather than "unknown."
    """
    used_percent = _primary_used_percent(block)
    if used_percent is None:
        return "codex meter unreadable: no rate_limits block found"

    headroom = 100.0 - used_percent
    window_minutes = None
    if isinstance(block, dict) and isinstance(block.get("primary"), dict):
        raw_window = block["primary"].get("window_minutes")
        if isinstance(raw_window, (int, float)) and not isinstance(raw_window, bool):
            window_minutes = raw_window

    if window_minutes is not None:
        return ("codex headroom: {:.1f}% remaining ({:.1f}% used of a "
                "{:.0f}-minute window)").format(headroom, used_percent, window_minutes)
    return "codex headroom: {:.1f}% remaining ({:.1f}% used)".format(
        headroom, used_percent)
