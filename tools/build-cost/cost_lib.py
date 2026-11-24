"""Per-model token and dollar accounting for a Claude Code session transcript.

## Why this exists

`/build` splits a multi-file build between an architect model in the main loop
and cheaper executor subagents. The claim is that this costs less than running
everything on the architect model. Without a record, that claim is untested.

Anthropic's own cost-optimization guidance is blunt about the trap here:
**per-token price is not per-task cost.** A cheaper model that needs more turns,
or that gets a task wrong and has to be re-dispatched, can cost more end to end.
The split may well be right — but "cheaper sticker price" is not evidence, and
neither is the absence of a complaint.

This is the cheap half of finding out. It does not run an A/B or score quality;
it records what each build actually spent, split by model and by main-loop vs
subagent. After a handful of builds there is real per-task data to reason from.

**What it cannot tell you:** whether the work was any good. Cost without a
quality bar is half a measurement — a config that is cheaper and worse looks
like a win in this ledger. Read it alongside how many BLOCKED reports the build
produced and how much rework followed, not on its own.
"""

import json
import os
import re
import statistics
import sys

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "thread-handover"))

import watch_lib  # noqa: E402  — one transcript reader, shared, not copied


def _rates(input_rate, write_5m, write_1h, read, output):
    return {"input": input_rate, "cache_write_5m": write_5m,
            "cache_write_1h": write_1h, "cache_read": read, "output": output}


# Anthropic's list prices, USD per million tokens, read from
# https://platform.claude.com/docs/en/about-claude/pricing on PRICES_AS_OF.
# A ledger row is only as current as this table: prices change, so treat old
# rows as indicative, and re-read the page before trusting a new model's row.
#
# Keyed on (family, version), never on family alone. Prices differ by
# generation in both directions (Opus 4.1 is three times Opus 4.5; Opus 5.5 is
# below Opus 5), so a family-level price silently misprices whichever
# generation it was not copied from. A version missing here is NOT PRICED.
#
# Named rates rather than a tuple, so no other sequence has to match its order.
# Cache reads are not a fixed fraction of input: 0.025x on Fable 5.1, 0.05x on
# Opus 5.5 and Sonnet 5.5, 0.1x elsewhere. Copy each rate from the page.
PRICES_AS_OF = "2026-10-17"
PRICING = {
    ("fable", "5.1"):  _rates(10.00, 12.50, 20.00, 0.25, 50.00),
    ("fable", "5"):    _rates(10.00, 12.50, 20.00, 1.00, 50.00),
    ("mythos", "5.1"): _rates(10.00, 12.50, 20.00, 0.25, 50.00),
    ("mythos", "5"):   _rates(10.00, 12.50, 20.00, 1.00, 50.00),
    ("opus", "5.5"):   _rates(4.00, 5.00, 8.00, 0.20, 20.00),
    ("opus", "5"):     _rates(5.00, 6.25, 10.00, 0.50, 25.00),
    ("opus", "4.8"):   _rates(5.00, 6.25, 10.00, 0.50, 25.00),
    ("opus", "4.7"):   _rates(5.00, 6.25, 10.00, 0.50, 25.00),
    ("opus", "4.6"):   _rates(5.00, 6.25, 10.00, 0.50, 25.00),
    ("opus", "4.5"):   _rates(5.00, 6.25, 10.00, 0.50, 25.00),
    ("opus", "4.1"):   _rates(15.00, 18.75, 30.00, 1.50, 75.00),
    ("opus", "4"):     _rates(15.00, 18.75, 30.00, 1.50, 75.00),
    ("sonnet", "5.5"): _rates(2.00, 2.50, 4.00, 0.10, 10.00),
    ("sonnet", "5"):   _rates(2.00, 2.50, 4.00, 0.20, 10.00),
    ("sonnet", "4.6"): _rates(3.00, 3.75, 6.00, 0.30, 15.00),
    ("sonnet", "4.5"): _rates(3.00, 3.75, 6.00, 0.30, 15.00),
    ("sonnet", "4"):   _rates(3.00, 3.75, 6.00, 0.30, 15.00),
    ("haiku", "5.5"):  _rates(0.10, 0.125, 0.20, 0.01, 0.50),
    ("haiku", "4.5"):  _rates(1.00, 1.25, 2.00, 0.10, 5.00),
    ("haiku", "3.5"):  _rates(0.80, 1.00, 1.60, 0.08, 4.00),
}

# A model priced by prompt length: (threshold, rates above it). The prompt is
# every input token of the request, cache reads and writes included, and each
# request is priced on its own.
LONG_PROMPT_PRICING = {
    ("haiku", "5.5"): (100_000, _rates(0.50, 0.625, 1.00, 0.05, 2.50)),
}

MODEL_FAMILIES = ("opus", "sonnet", "haiku", "fable", "mythos")

# The model whose output rate defines weight 1.0 in the opus-equivalent column.
REFERENCE_MODEL = ("opus", "5.5")

# How fast a model draws down a usage window relative to REFERENCE_MODEL, where
# you know better than the price ratio: {(family, version): weight}. Empty by
# default, so every weight below is derived from list prices. A weight added
# here that you know only as a minimum also goes in FLOOR_WEIGHTS: totals that
# include it then print with a `≥`.
USAGE_WEIGHT = {}
FLOOR_WEIGHTS = frozenset()

# claude-<family>-<major>[-<minor>] with an optional dated snapshot or context
# suffix, or the older claude-<major>-<minor>-<family>. A minor is one or two
# digits not followed by another digit, so a `-20251101` date is never read as
# a minor version.
_FAMILY_FIRST = re.compile(
    r"claude-(%s)-(\d+)(?:-(\d{1,2})(?!\d))?" % "|".join(MODEL_FAMILIES))
_VERSION_FIRST = re.compile(
    r"claude-(\d+)-(\d{1,2})-(%s)(?![a-z])" % "|".join(MODEL_FAMILIES))


def model_key(model_id):
    """(family, version) for a model id, e.g. ("opus", "5.5"), or None.

    `claude-opus-5-5[1m]`, `claude-sonnet-4-5-20250929` and `CLAUDE-HAIKU-4-5`
    resolve to the version their name states. Anything that does not parse is
    None, so it lands in NOT PRICED rather than borrowing a sibling's price.
    """
    if not isinstance(model_id, str) or not model_id:
        return None
    lowered = model_id.lower()
    match = _FAMILY_FIRST.search(lowered)
    if match:
        name, major, minor = match.groups()
        return name, major if minor is None else "%s.%s" % (major, minor)
    match = _VERSION_FIRST.search(lowered)
    if match:
        major, minor, name = match.groups()
        return name, "%s.%s" % (major, minor)
    return None


def family(model_id):
    """The model family a model id names, or None."""
    key = model_key(model_id)
    return key[0] if key else None


def tier(model_id):
    """The PRICING key for a model, or None when this table has no price for it.

    Narrower than `model_key`: a release newer than this table parses fine and
    still returns None here, so it lands in `unpriced`. A ledger that quietly
    under-reports is worse than one that says it does not know.
    """
    key = model_key(model_id)
    return key if key in PRICING else None


def usage_weight(model_id):
    """(weight, is_floor) relative to REFERENCE_MODEL, or None when unknown.

    A model with no price and no override is excluded from the weighted total
    and named in the warning rather than counted as zero — same fail-loud rule
    as NOT PRICED, so a new model release cannot silently read as free.
    """
    key = model_key(model_id)
    if key is None:
        return None
    if key in USAGE_WEIGHT:
        return USAGE_WEIGHT[key], key in FLOOR_WEIGHTS
    if key in PRICING:
        # A price-derived proxy for window draw, not a published usage ratio —
        # say so wherever it is reported.
        return (PRICING[key]["output"] / PRICING[REFERENCE_MODEL]["output"],
                key in FLOOR_WEIGHTS)
    return None


def _one_hour_writes(usage):
    """The cache-write tokens a usage object bills at the 1-hour rate.

    Read from the merged flat field when present, else from the API's own
    `cache_creation.ephemeral_1h_input_tokens` breakdown. No breakdown means 0:
    every write is then priced at the 5-minute rate, the API's default TTL.
    """
    value = usage.get("cache_write_1h")
    if value is None:
        breakdown = usage.get("cache_creation")
        if isinstance(breakdown, dict):
            value = breakdown.get("ephemeral_1h_input_tokens")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    # A negative breakdown is corrupt; read it as none, or it would move tokens
    # from the 1h rate to the 5m rate and can drive a message's cost negative.
    return max(value, 0)


def cost_of(usage, model_id):
    """Dollars for one message's usage. None when the model is unpriced."""
    key = tier(model_id)
    if key is None:
        return None
    tokens = {field: usage.get(field) or 0 for field in USAGE_FIELDS}
    rates = PRICING[key]
    if key in LONG_PROMPT_PRICING:
        threshold, long_rates = LONG_PROMPT_PRICING[key]
        prompt = (tokens["input_tokens"] + tokens["cache_read_input_tokens"]
                  + tokens["cache_creation_input_tokens"])
        if prompt > threshold:
            rates = long_rates
    writes = tokens["cache_creation_input_tokens"]
    writes_1h = min(_one_hour_writes(usage), writes)
    return (tokens["input_tokens"] * rates["input"]
            + (writes - writes_1h) * rates["cache_write_5m"]
            + writes_1h * rates["cache_write_1h"]
            + tokens["cache_read_input_tokens"] * rates["cache_read"]
            + tokens["output_tokens"] * rates["output"]) / 1_000_000


# Usage fields other than the defaults mean a price this table does not hold
# (fast mode, a regional endpoint, batch or priority tier), so such a message
# is not priced. Absent means the default.
LIST_PRICE_CONDITIONS = (("speed", "standard"), ("inference_geo", "global"),
                         ("service_tier", "standard"))


def off_list_price(usage):
    """'speed=fast'-style reason when a usage object is not at list price, else None."""
    reasons = []
    for field, default in LIST_PRICE_CONDITIONS:
        value = usage.get(field)
        if value is not None and value != default:
            reasons.append("%s=%s" % (field, value))
    return ", ".join(reasons) or None


# The four token fields, in one place: `_add` reads them and `_merge_usage`
# merges them, and a field present in one and missing from the other is a
# silent under-count.
USAGE_FIELDS = ("input_tokens", "output_tokens",
                "cache_creation_input_tokens", "cache_read_input_tokens")

# Bucket keys, same order as USAGE_FIELDS, so `_add` can zip the two rather
# than naming each field a second time.
_BUCKET_KEYS = ("input", "output", "cache_write", "cache_read")


def _merge_usage(into, usage):
    """Per-field max of one message's repeated usage objects.

    Claude Code writes one entry per content block and repeats that message's
    `usage` on each, so a naive sum inflates a main transcript. Three
    shapes exist and per-field max is the only rule correct for all three:

      repeated-complete  every entry carries the same full usage
      streamed-partial   `output_tokens: 3` until the last entry
      trailing-zero      a complete entry followed by all-zero entries

    Both obvious simplifications are measured wrong. Keeping the FIRST value
    understates output on agent files (streamed-partial). Keeping the LAST
    zeroes a real message (trailing-zero: it would have discarded
    a real message's output tokens and cache reads). A full-corpus scan of
    usage-bearing entries found zero groups in which a non-zero field decreases,
    which is what makes a max rule safe rather than merely convenient.

    The 1-hour share of the cache writes merges the same way, into the flat
    `cache_write_1h` field `cost_of` reads, and an off-list-price reason seen
    on any entry is kept.
    """
    for field in USAGE_FIELDS:
        value = usage.get(field) or 0
        if value > into[field]:
            into[field] = value
    one_hour = _one_hour_writes(usage)
    if one_hour > into.get("cache_write_1h", 0):
        into["cache_write_1h"] = one_hour
    reason = off_list_price(usage)
    if reason and not into.get("off_list_price"):
        into["off_list_price"] = reason


def _blank():
    return {"messages": 0, "input": 0, "output": 0, "cache_write": 0,
            "cache_read": 0, "cost": 0.0, "unpriced": 0,
            "opus_equivalent": 0.0, "unweighted": 0, "floored": False}


def _add(bucket, usage, model_id):
    bucket["messages"] += 1
    for usage_field, bucket_field in zip(USAGE_FIELDS, _BUCKET_KEYS):
        bucket[bucket_field] += usage.get(usage_field) or 0

    # A message with no tokens cannot understate anything, so it must not fire
    # the fail-loud warnings. Claude Code writes `"model": "<synthetic>"` on
    # locally generated placeholder messages — all four token
    # fields are zero on them. Flagging them trains the reader to ignore NOT PRICED and NOT
    # WEIGHTED, which is what those warnings exist for.
    #
    # The id is named here so it can be grepped, but it deliberately appears in
    # no condition: the gate is a token test, because a future placeholder under
    # a different id would otherwise reintroduce exactly this noise. An unknown
    # model that actually spent tokens must still warn.
    contributes = any(usage.get(field) for field in USAGE_FIELDS)

    # Off list price: neither priced nor weighted, and `analyze` keys it under
    # its own name so the warning says why.
    off_list = bool(usage.get("off_list_price"))

    cost = None if off_list else cost_of(usage, model_id)
    if cost is None:
        if contributes:
            bucket["unpriced"] += 1
    else:
        bucket["cost"] += cost

    weighted = None if off_list else usage_weight(model_id)
    if weighted is None:
        if contributes:
            bucket["unweighted"] += 1
    else:
        weight, is_floor = weighted
        bucket["opus_equivalent"] += (usage.get(USAGE_FIELDS[1]) or 0) * weight
        if is_floor:
            bucket["floored"] = True


def session_dir_for(transcript_path):
    """The directory beside the transcript that holds the session's subagents.

    A session transcript is <dir>/<session-id>.jsonl and its subagents usually
    live in <dir>/<session-id>/subagents/, so dropping the extension is the
    mapping for that case. It is not the whole truth: after EnterWorktree the
    subagents land under a sibling project dir (see `agent_files`), so this
    returns only the transcript's own session dir.
    """
    return os.path.splitext(transcript_path)[0]


def _walk_agent_files(root):
    """Every agent-*.jsonl under `root`; unreadable directories are skipped."""
    found = []
    for directory, _subdirs, names in os.walk(root):
        for name in names:
            if name.startswith("agent-") and name.endswith(".jsonl"):
                found.append(os.path.join(directory, name))
    return found


def agent_files(transcript_path):
    """Every subagent transcript for this session, sorted. [] when there are none.

    Two shapes exist and both count: Task-tool subagents land directly in
    subagents/, Workflow-tool agents land in subagents/workflows/wf_*/. A
    recursive walk covers both without caring which is which.

    Two locations are read. The transcript's own <dir>/<sid>/subagents/ is
    always walked. When the transcript sits in a `projects` directory, every
    sibling project dir's <sid>/subagents/ is walked too: a session that ran
    EnterWorktree keeps its transcript under the start dir's slug but its
    subagents land under the worktree's slug, so reading only the own dir
    silently counted zero agents (and made the SessionEnd hook write no row).
    The `projects` check bounds the scan to that layout; a transcript anywhere
    else behaves as before. Results are deduplicated by realpath.

    The `.jsonl` suffix is load-bearing: each agent-<id>.jsonl has a sibling
    agent-<id>.meta.json, and globbing `agent-*` would read metadata as a
    transcript. That yields zero usage rows and no error — the exact silent
    under-count this whole task exists to remove.
    """
    roots = [os.path.join(session_dir_for(transcript_path), "subagents")]
    # abspath: a relative path from the CLI ("x.jsonl") has no grandparent to name.
    projects_dir = os.path.dirname(os.path.dirname(os.path.abspath(transcript_path)))
    if os.path.basename(projects_dir) == "projects":
        session_id = os.path.basename(os.path.splitext(transcript_path)[0])
        try:
            entries = os.listdir(projects_dir)
        except OSError:
            entries = []
        for entry_name in sorted(entries):
            roots.append(os.path.join(projects_dir, entry_name, session_id, "subagents"))
    found = {}
    for root in roots:
        try:
            paths = _walk_agent_files(root)
        except OSError:
            continue
        for path in paths:
            found.setdefault(os.path.realpath(path), path)
    return sorted(found.values())


LANES = ("main", "executor", "review", "other")

# The agents ship in the `toolkit-build` plugin, so history records
# `toolkit-build:executor`; an agent installed outside the plugin records the
# bare `executor`. Both spellings must lane the same.
PLUGIN_AGENT_PREFIXES = ("toolkit-build:",)


def bare_agent_type(agent_type):
    """`toolkit-build:executor` -> `executor`; anything else unchanged."""
    if isinstance(agent_type, str):
        for prefix in PLUGIN_AGENT_PREFIXES:
            if agent_type.startswith(prefix):
                return agent_type[len(prefix):]
    return agent_type

# agentType -> lane. Read from the sibling agent-<id>.meta.json, never guessed
# from the file's path: the path only distinguishes workflow agents from the
# rest, which files task-reviewer under `executor` and destroys the one
# distinction this ledger exists to make.
LANE_BY_AGENT_TYPE = {
    "executor": "executor",
    "task-reviewer": "review",
    "code-reviewer": "review",
    "architect-reviewer": "review",
    "spec-reviewer": "review",
    "workflow-subagent": "review",
    "general-purpose": "other",
    "Explore": "other",
    "Plan": "other",
    "claude": "other",
    "claude-code-guide": "other",
}

# Printed when an agentType is not in the table above. Kept distinct from a
# real type name so the warning reads honestly.
NO_META = "<no meta>"


def lane_for_agent_file(agent_path):
    """(lane, unknown_agent_type) for one subagent transcript.

    Unknown or unreadable metadata lands in `other` and is reported, never
    absorbed into `executor`. A new agent type shipping in a future Claude Code
    release must show up as a warning, not as silently miscounted executor work.
    """
    meta_path = agent_path[: -len(".jsonl")] + ".meta.json"
    try:
        with open(meta_path, encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return "other", NO_META
    if not isinstance(meta, dict):
        return "other", NO_META
    agent_type = meta.get("agentType")
    if not isinstance(agent_type, str) or not agent_type:
        return "other", NO_META
    lane = LANE_BY_AGENT_TYPE.get(bare_agent_type(agent_type))
    if lane is None:
        return "other", agent_type
    return lane, None


DEFAULT_QUALIFYING_AGENT_TYPES = [
    "executor", "task-reviewer", "workflow-subagent", "code-reviewer",
    "architect-reviewer", "spec-reviewer"]


def qualifying_agent_types(config):
    """Which `agentType`s make a session worth recording at `SessionEnd`.

    Lives here, not in the hook, because the manual CLI is a second writer of a
    ledger row and needs the same answer — a second copy of this list is how the
    two writers came to disagree about what a row means. Ordered most-build-like
    first purely so `qualified_by` reads sensibly; every caller uses it as a set.

    The two review-lane types are in the default because without them a session
    that ran a `/build` review gate and wrote no code records nothing, so the ledger
    under-sampled exactly the review-heavy sessions a cost argument needs.
    `architect-reviewer` is in it for the same reason: a session that ran only
    that optional reviewer would otherwise record nothing.
    spec-reviewer was added 2026-11-21 (row 102): it is the design/plan gates' Agent
    fallback reviewer, and without it a session whose only gates ran that way
    recorded nothing.

    A missing, unreadable or empty value falls back to the default rather than
    to nothing — recording too much is visible in the data, recording nothing is
    not.
    """
    if config is None:
        return list(DEFAULT_QUALIFYING_AGENT_TYPES)
    try:
        configured = config.get_list("build_cost", "qualifying_agent_types")
    except Exception:
        return list(DEFAULT_QUALIFYING_AGENT_TYPES)
    return configured or list(DEFAULT_QUALIFYING_AGENT_TYPES)


def qualifies(observed, wanted):
    """The observed agent types whose bare form is among the wanted ones.

    Matches across the namespaced and bare spellings in both directions, so a
    config written either way qualifies a session recorded either way. The
    result keeps the observed spelling.
    """
    wanted_bare = {bare_agent_type(w) for w in wanted}
    return sorted(o for o in set(observed) if bare_agent_type(o) in wanted_bare)


def agent_types(transcript_path):
    """Every `agentType` observed among a session's subagents, as a sorted list.

    Cheap by design — reads the `.meta.json` siblings and never opens a
    transcript, so `claude-build-cost-record` can gate a non-build session on
    it without paying for `analyze`.

    Shared rather than inlined because there are now two writers of a ledger
    row: the SessionEnd hook and the manual CLI. The hook had this loop inline,
    and the CLI had nothing — which is exactly how the CLI came to write rows
    with no `agent_types` at all, rendering `kind: —` on a session recorded
    today and, because `collapse_ledger_rows` keeps the largest row, able to
    supersede the hook's correctly-labelled one. One source of truth, per this
    repo's rule about a second copy of a live tool.

    Unreadable or non-dict metadata is skipped per file, matching every other
    reader here: a session directory is long-lived and will eventually hold
    something unexpected.
    """
    found = set()
    for path in agent_files(transcript_path):
        meta_path = path[: -len(".jsonl")] + ".meta.json"
        try:
            with open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and isinstance(meta.get("agentType"), str) \
                and meta["agentType"]:
            found.add(meta["agentType"])
    return sorted(found)


def analyze(transcript_path):
    """Per-model totals for a session across four lanes.

    main     — the architect's own turns
    executor — /build plan tasks dispatched to a subagent
    review   — the Plan Review/Code Review gates and per-task reviewers
    other    — ad-hoc research agents, and anything we could not classify

    The executor/review split is the whole point: a Sonnet transcriber and an
    Opus review gate are the two things this ledger exists to tell apart.
    """
    by_model = {}
    totals = {lane: _blank() for lane in LANES}
    unknown_agent_types = set()

    def consume(path, lane_of_entry):
        # Group before accumulating: one API response is many JSONL entries.
        # Keyed on (lane, message.id) rather than message.id alone so a repeated
        # id can never merge across lanes; `order` keeps output deterministic.
        merged = {}
        order = []
        for index, entry in enumerate(
                watch_lib.iter_entries(path, include_sidechains=True)):
            message = entry.get("message")
            if not isinstance(message, dict):
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            lane = lane_of_entry(entry)
            message_id = message.get("id")
            if not isinstance(message_id, str) or not message_id:
                # An entry
                # with no id cannot be grouped with anything. Give it a key of
                # its own so it is counted once — over-counting is the safe
                # direction, dropping it is not.
                key = (lane, "\0entry-%d" % index)
            else:
                key = (lane, message_id)
            if key not in merged:
                # First entry in the group decides the model id. No
                # groups in the corpus span two model ids, so this cannot silently
                # mis-attribute.
                merged[key] = [dict.fromkeys(USAGE_FIELDS, 0),
                               message.get("model") or "unknown"]
                order.append(key)
            _merge_usage(merged[key][0], usage)

        for key in order:
            usage, model_id = merged[key]
            if usage.get("off_list_price"):
                model_id = "%s (%s)" % (model_id, usage["off_list_price"])
            lane = key[0]
            by_model.setdefault(model_id, {name: _blank() for name in LANES})
            _add(by_model[model_id][lane], usage, model_id)
            _add(totals[lane], usage, model_id)

    # An inline sidechain means a legacy transcript, from before subagents were
    # written to their own files. Its agentType is unknowable, so it goes to
    # `other` rather than being credited to a lane it may not belong to.
    consume(transcript_path,
            lambda entry: "other" if entry.get("isSidechain") else "main")

    for path in agent_files(transcript_path):
        lane, unknown = lane_for_agent_file(path)
        if unknown:
            unknown_agent_types.add(unknown)
        consume(path, lambda _entry, lane=lane: lane)

    return {"by_model": by_model, "totals": totals,
            "unknown_agent_types": sorted(unknown_agent_types)}


def summarize(report):
    """A flat summary: cost and messages per lane, plus anything we could not price."""
    totals = report["totals"]
    unpriced = sorted(
        model for model, lanes in report["by_model"].items()
        if any(lanes[name]["unpriced"] for name in LANES)
    )
    summary = {
        "total_cost": sum(totals[name]["cost"] for name in LANES),
        "models": sorted(report["by_model"]),
        "unpriced_models": unpriced,
        "unknown_agent_types": report.get("unknown_agent_types", []),
    }
    for name in LANES:
        lane = totals[name]
        summary["%s_cost" % name] = lane["cost"]
        summary["%s_messages" % name] = lane["messages"]
        summary["%s_output" % name] = lane["output"]
        summary["%s_input" % name] = lane["input"]
        summary["%s_cache_write" % name] = lane["cache_write"]
        summary["%s_cache_read" % name] = lane["cache_read"]
        # round(), never int(): int() truncates toward zero, which under-reports
        # a floored figure — the direction this whole feature exists to avoid.
        summary["%s_opus_equivalent_output" % name] = round(lane["opus_equivalent"])
        # Per-lane, not just session-wide: a floor or an unweighted model can
        # land in one lane and not another, and the session-wide flags below
        # cannot say which. Both values already exist on the bucket; this is
        # the first place either is surfaced.
        summary["%s_opus_equivalent_is_floor" % name] = lane["floored"]
        summary["%s_unweighted" % name] = lane["unweighted"]
    summary["total_output"] = sum(totals[name]["output"] for name in LANES)
    # Sum the ROUNDED lane figures, not round(sum of unrounded). The two differ
    # by up to 2 tokens, and the per-lane values are now printed beside this
    # total in both the report and the ledger — the whole reason for showing
    # them is that a reader can check the headline against the rows, so a table
    # that does not add up costs more than a token of precision. round(sum)
    # can disagree with the sum of the rounded lanes by one.
    summary["total_opus_equivalent_output"] = sum(
        summary["%s_opus_equivalent_output" % name] for name in LANES)
    summary["opus_equivalent_is_floor"] = any(totals[name]["floored"] for name in LANES)
    summary["unweighted_models"] = sorted(
        model for model, lanes in report["by_model"].items()
        if any(lanes[name]["unweighted"] for name in LANES))
    return summary


def append_ledger(path, record):
    """Append one JSON record. Best-effort: a failed write never breaks a build."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        return True
    except OSError:
        return False


DEFAULT_LEDGER_PATH = "~/.claude/build-costs.jsonl"


def ledger_path(config=None):
    """Where `claude-build-cost-record` appends, from `build_cost.ledger_path`.

    That config key already existed and is documented in `config.example.json`
    — this adds no new key. What it removes is the DEFAULT being hardcoded in
    two executables with two different signatures, now that a third caller
    (the dispatch advisory hook) needs it too.

    It lives in `cost_lib` because `cost_lib` owns this ledger's format. Adding
    it to `usage_lib` instead would have put a third copy in a fourth module,
    under a second config key (`usage_watch.ledger_path`) for a path that
    already has one — the duplicate-reader mistake this repo has a hard rule
    about, with the rule quoted above it.

    `config=None` means resolve it here, mirroring the guarded import in
    `usage_lib`. `claude-build-cost-record` passes the config it has already
    loaded; the hook and `claude-build-cost` call it bare.
    """
    if config is None:
        try:
            import toolkit_config as config
        except ImportError:
            config = None
    if config is not None:
        try:
            configured = config.get_path("build_cost", "ledger_path")
        except Exception:
            configured = None
        if configured:
            return configured
    return os.path.expanduser(DEFAULT_LEDGER_PATH)


def ledger_number(row, key, default=0):
    """A numeric field from an untrusted ledger row, or the default.

    The row being a dict says nothing about its values. An append-only ledger
    that outlives several schema versions will eventually carry a string, a
    null or a list where a number belongs, and arithmetic over that raises
    TypeError. Degrade per field, for the same reason parsing degrades per line.

    `isinstance(value, bool)` is excluded deliberately: `True` is an `int` in
    Python and would render as `1` token, a fabricated figure rather than a
    missing one.

    NOT usable where "absent" and "a real zero" must stay distinct — this
    collapses both to `default` by design. `lane_stats` keeps its own inline
    check for exactly that reason; see its docstring.
    """
    value = row.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return value


_PROMPT_SIDE = ("input", "cache_write", "cache_read")


def ledger_growth_key(row):
    """(total_output, prompt-side tokens) for ordering snapshots of one session.

    Snapshots of a session are cumulative, so the newer one is never smaller
    on any field. Output alone is not enough to tell them apart: a resumed
    session whose latest calls added input or cache tokens and no output would
    tie, and the older row would win, keeping its smaller dollar figure. Ties
    on output break on every lane's input, cache-write and cache-read tokens.
    """
    prompt_side = sum(ledger_number(row, "%s_%s" % (lane, field), 0)
                      for lane in LANES for field in _PROMPT_SIDE)
    return (ledger_number(row, "total_output", 0), prompt_side)


def collapse_ledger_rows(rows):
    """Collapse rows sharing a `session_id`, keeping the one with the larger
    `ledger_growth_key`. The hook appends a superseding row on growth rather than
    rewriting, so a real ledger legitimately carries several rows per session
    — this is what makes that append-only choice honest to read. A row with
    no `session_id` (every row the manual CLI writes) stands alone and must
    never be collapsed with anything else.

    Done on read as well as on write, deliberately: an older hook version, or
    a hand-appended row, can still produce duplicates the current writer would
    have suppressed. **Every reader of this ledger owes this call.** It lives
    here rather than in `claude-build-cost` because a second reader arrived —
    `lane_stats` — and read the ledger without collapsing, over-reporting `n`
    and a median computed from a duplicated row. Two
    collapse rules would have drifted the first time either changed.
    """
    best = {}
    order = []
    for row in rows:
        session_id = row.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            order.append(("row", row))
            continue
        current = best.get(session_id)
        if current is None:
            order.append(("session", session_id))
            best[session_id] = row
        elif ledger_growth_key(row) > ledger_growth_key(current):
            best[session_id] = row
    return [key if kind == "row" else best[key] for kind, key in order]


def lane_stats(ledger_path):
    """Median non-zero output per lane, with its sample count.

    **Reads every row, including gate-only sessions, and that is deliberate.**
    `build_cost.qualifying_agent_types` admits `workflow-subagent` and
    `code-reviewer`, so sessions that ran a review gate and wrote no code are
    recorded too, and they are part of this function's population for its one
    machine consumer, `claude-usage-dispatch`.

    Two reasons it is not filtered to `build` rows. Zero-valued lanes are
    skipped below, so a gate-only session cannot move the *executor* median at
    all; what it moves is the *review* median, upward. And that is the more
    honest figure for the question the consumer asks — it forecasts what
    dispatching into a lane costs, and a full review gate is the most expensive
    review work there is. Excluding it would make a review forecast
    systematically optimistic about exactly the dispatch worth warning about.

    Know the blast radius before revisiting this: `usage_watch.dispatch_advisory`
    defaults to FALSE, and a lane needs 5 recorded sessions before the advisory
    shows any figure. When it is turned on, the review median it reports is a
    mixed population by choice, and the ledger's own `kind` column is how a
    human separates them.

    {lane: (median_output_tokens, n)} from the build-cost ledger.

    Added to `cost_lib` rather than shipped as a second ledger reader: this
    module already owns `append_ledger` and the record format, and a reader
    living elsewhere is the duplicate-reader mistake this repo has a hard rule
    about. The two would drift the first time a field was renamed.

    MEDIAN, not mean: records can differ several-fold in size, so a mean
    reports whichever build was largest. `n` travels with every figure — a
    median of two is not a forecast, and the caller must be able to say so.

    THESE ARE PER-SESSION FIGURES. A ledger record is one session and a long
    session spans several builds, so a lane's median over-states what one
    dispatch costs. Say so wherever it is rendered rather than quietly
    treating it as a per-dispatch number.

    UNIT MISMATCH, DELIBERATE. These are RAW output tokens, the unit
    `estimate_lib`'s tokens-per-point ratio is expressed in, so dividing one by the other
    is dimensionally sound and any systematic factor in the counting cancels.
    The ledger also carries `<lane>_opus_equivalent_output`, which is the
    honest currency of window draw-down — and pairing that with a ratio fitted
    to raw tokens would be unit-mixing. The consequence is that a Sonnet-heavy
    lane's cost in points is over-stated here, which errs toward NOT
    dispatching. Do not fix one half of that pair without re-deriving the
    other; a constant and the counting it was fitted to are a matched pair.

    Missing, non-numeric, boolean AND ZERO values are all skipped. A zero drags
    the median toward "free", which is the direction that makes a dispatch look
    affordable, and it is never evidence: `summarize` writes all four
    `<lane>_output` keys on every row, so a session that never touched a lane
    records `0` for it rather than omitting the key.

    THE ZERO CASE WAS ORIGINALLY SPECIFIED THE OTHER WAY AND THE SPEC WAS
    WRONG. The plan said "a genuine 0 IS counted — a lane that spent nothing
    this session is data", which assumed an unused lane shows up as a MISSING
    key. It never does. On a real ledger `other` returned
    a zero median over several "samples", every one of them a lane that was simply not
    used — and that n clears MIN_SAMPLES, so the advisory told a reader that an
    `other` dispatch "has historically cost about 0 points … That fits: 0 <=
    47." `other` is the fallback lane for every unrecognised `subagent_type`,
    so that was the default answer for anything new. A lane with genuinely no
    usable records is now absent from the mapping, which the caller renders as
    "no history" — the honest answer.

    A real lane that ran and emitted zero output tokens is not a case this
    loses: an agent that produced nothing recorded nothing worth forecasting
    against, and treating it as a measured zero is what created the defect.
    This is also why the check below is inline rather than `ledger_number`,
    which defaults every unusable case to 0 and would re-create it.

    ROWS ARE COLLAPSED PER SESSION FIRST. The hook appends a superseding row
    when a session grows, so an uncollapsed read counts one session twice and
    reports an `n` larger than the number of sessions observed. Without it
    `n` over-counts sessions and moves the
    per-lane medians. The direction is NOT predictable —
    a duplicate shifts the median's position in the ordering rather than
    pulling it one way — so this is a correctness fix, not a bias correction,
    and `n` is the half that was plainly wrong.
    """
    collected = {lane: [] for lane in LANES}
    try:
        with open(ledger_path, encoding="utf-8") as handle:
            rows = []
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                rows.append(record)
    except OSError:
        return {}
    for record in collapse_ledger_rows(rows):
        for lane in LANES:
            value = record.get("%s_output" % lane)
            # `isinstance(True, int)` is True in Python, so the bool test
            # comes first or a stray True counts as one token.
            if isinstance(value, bool):
                continue
            if not isinstance(value, (int, float)):
                continue
            # Zero means "this session did not use this lane", never "this
            # lane was free" — see the docstring. `summarize` always writes
            # the key, so the missing-key branch above can never fire for a
            # row this tool wrote.
            if not value:
                continue
            collected[lane].append(float(value))
    return {lane: (float(statistics.median(values)), len(values))
            for lane, values in collected.items() if values}
