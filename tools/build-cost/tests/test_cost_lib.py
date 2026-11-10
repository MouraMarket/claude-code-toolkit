#!/usr/bin/env python3
"""Tests for claude-build-cost.

The point of this tool is a number someone will act on — whether to keep the
Opus/Sonnet split. A silently wrong number is worse than no number, so the two
things most load-bearing here are the main/subagent split and the refusal to
price an unknown model at zero.
"""

import itertools
import json
import os
import subprocess
import sys
import tempfile
import pathlib
import unittest
import uuid
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
sys.path.insert(0, TOOL_DIR)

import cost_lib  # noqa: E402

CLI = os.path.join(TOOL_DIR, "claude-build-cost")

_MESSAGE_SEQ = itertools.count()


def entry(model, usage, sidechain=False, message_id=None):
    """One JSONL entry.

    `message_id` defaults to a fresh unique id so an ordinary fixture models one
    message per entry — the same relationship real data has. Pass an explicit id
    to several entries to model what Claude Code actually writes: N entries for
    one API response, each repeating that message's usage.
    """
    if message_id is None:
        message_id = "msg_auto_%d" % next(_MESSAGE_SEQ)
    row = {"message": {"id": message_id, "model": model, "usage": usage}}
    if sidechain:
        row["isSidechain"] = True
    return row


def content_block_entries(model, usage, blocks=4, message_id="msg_blocks",
                          sidechain=False):
    """The repeated-complete shape: one entry per content block, same usage on each."""
    return [entry(model, dict(usage), sidechain=sidechain, message_id=message_id)
            for _ in range(blocks)]


def streamed_entries(model, usage, message_id="msg_streamed", sidechain=False):
    """The streamed-partial shape: output arrives only on the last entry.

    Input and cache are already complete on the partial entries, which is why a
    fix that keeps the first entry looks right on cache and is badly wrong on
    output.
    """
    partial = dict(usage, output_tokens=3)
    return [entry(model, dict(partial), sidechain=sidechain, message_id=message_id),
            entry(model, dict(partial), sidechain=sidechain, message_id=message_id),
            entry(model, dict(usage), sidechain=sidechain, message_id=message_id)]


def trailing_zero_entries(model, usage, message_id="msg_trailing", sidechain=False):
    """The third real shape: a complete entry followed by all-zero entries.

    Seen in the real corpus. This is the shape that makes "just keep
    the last entry" lose a whole message — it would have discarded
    a real message's output tokens and cache reads.
    """
    zero = dict.fromkeys(
        ("input_tokens", "output_tokens",
         "cache_creation_input_tokens", "cache_read_input_tokens"), 0)
    return [entry(model, dict(usage), sidechain=sidechain, message_id=message_id),
            entry(model, dict(zero), sidechain=sidechain, message_id=message_id)]


def transcript(rows):
    handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for row in rows:
        handle.write(json.dumps(row) + "\n")
    handle.close()
    return handle.name


def session_with_agents(main_rows, agents):
    """Build a temp session dir. `agents` maps relative agent-file path -> rows.

    Returns the transcript path. Mirrors the real layout: the transcript is
    <dir>/<sid>.jsonl and its subagents are <dir>/<sid>/subagents/...
    """
    root = tempfile.mkdtemp()
    sid = "11111111-2222-3333-4444-555555555555"
    transcript_path = os.path.join(root, sid + ".jsonl")
    with open(transcript_path, "w") as handle:
        for row in main_rows:
            handle.write(json.dumps(row) + "\n")
    for rel, rows in agents.items():
        target = os.path.join(root, sid, "subagents", rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
    return transcript_path


# Anthropic's pricing page on 2026-10-17, USD per million tokens, transcribed by
# hand here and NOT read from cost_lib: (input, 5m write, 1h write, read, output).
# A test that compared PRICING with itself could not catch a mistyped rate.
PAGE_2026_10_17 = {
    "claude-fable-5-1":  (10.00, 12.50, 20.00, 0.25, 50.00),
    "claude-fable-5":    (10.00, 12.50, 20.00, 1.00, 50.00),
    "claude-mythos-5-1": (10.00, 12.50, 20.00, 0.25, 50.00),
    "claude-mythos-5":   (10.00, 12.50, 20.00, 1.00, 50.00),
    "claude-opus-5-5":   (4.00, 5.00, 8.00, 0.20, 20.00),
    "claude-opus-5":     (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-8":   (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-7":   (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-6":   (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-5":   (5.00, 6.25, 10.00, 0.50, 25.00),
    "claude-opus-4-1":   (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-opus-4":     (15.00, 18.75, 30.00, 1.50, 75.00),
    "claude-sonnet-5-5": (2.00, 2.50, 4.00, 0.10, 10.00),
    "claude-sonnet-5":   (2.00, 2.50, 4.00, 0.20, 10.00),
    "claude-sonnet-4-6": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-sonnet-4-5": (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-sonnet-4":   (3.00, 3.75, 6.00, 0.30, 15.00),
    "claude-haiku-5-5":  (0.10, 0.125, 0.20, 0.01, 0.50),
    "claude-haiku-4-5":  (1.00, 1.25, 2.00, 0.10, 5.00),
    "claude-3-5-haiku":  (0.80, 1.00, 1.60, 0.08, 4.00),
}
HAIKU_5_5_OVER_100K = (0.50, 0.625, 1.00, 0.05, 2.50)


EACH = 10_000  # per class: a 30,000-token prompt, under Haiku 5.5's 100k tier


def ten_thousand_each(one_hour=False):
    """A usage object with EACH tokens in every class; writes at one TTL."""
    usage = {"input_tokens": EACH, "output_tokens": EACH,
             "cache_creation_input_tokens": EACH,
             "cache_read_input_tokens": EACH}
    if one_hour:
        usage["cache_creation"] = {"ephemeral_1h_input_tokens": EACH,
                                   "ephemeral_5m_input_tokens": 0}
    return usage


class TestTier(unittest.TestCase):

    def test_resolves_family_and_version(self):
        """Ids carry date and context suffixes; neither changes the version."""
        for model, expected in (
            ("claude-opus-5-5", ("opus", "5.5")),
            ("claude-opus-5-5[1m]", ("opus", "5.5")),
            ("claude-opus-5", ("opus", "5")),
            ("claude-opus-4-20250514", ("opus", "4")),
            ("claude-sonnet-4-5-20250929", ("sonnet", "4.5")),
            ("CLAUDE-HAIKU-4-5", ("haiku", "4.5")),
            ("claude-fable-5-1", ("fable", "5.1")),
            ("claude-3-5-haiku-20241022", ("haiku", "3.5")),
        ):
            with self.subTest(model=model):
                self.assertEqual(cost_lib.model_key(model), expected)
                self.assertEqual(cost_lib.tier(model), expected)

    def test_unknown_model_is_none_not_a_default_tier(self):
        """Defaulting to a tier would price a new model confidently and wrongly."""
        self.assertIsNone(cost_lib.tier("some-future-model"))
        self.assertIsNone(cost_lib.tier(""))
        self.assertIsNone(cost_lib.tier(None))
        self.assertIsNone(cost_lib.tier("<synthetic>"))

    def test_a_newer_version_of_a_known_family_is_not_priced_by_family(self):
        """The family-level table priced every Opus at one generation's rate."""
        self.assertEqual(("opus", "6"), cost_lib.model_key("claude-opus-6"))
        self.assertIsNone(cost_lib.tier("claude-opus-6"))
        self.assertIsNone(cost_lib.tier("claude-sonnet-5-7"))
        self.assertIsNone(cost_lib.cost_of({"output_tokens": 1}, "claude-opus-6"))
        self.assertIsNone(cost_lib.usage_weight("claude-opus-6"))

    def test_prices_are_dated(self):
        self.assertEqual("2026-10-17", cost_lib.PRICES_AS_OF)


class TestCostOf(unittest.TestCase):

    def test_every_model_matches_the_pricing_page(self):
        """EACH tokens of each class, 5-minute writes, then 1-hour writes."""
        scale = EACH / 1_000_000
        for model, (inp, w5m, w1h, read, out) in PAGE_2026_10_17.items():
            with self.subTest(model=model):
                self.assertAlmostEqual(cost_lib.cost_of(ten_thousand_each(), model),
                                       (inp + w5m + read + out) * scale, places=9)
                self.assertAlmostEqual(
                    cost_lib.cost_of(ten_thousand_each(one_hour=True), model),
                    (inp + w1h + read + out) * scale, places=9)

    def test_every_priced_version_is_on_the_page(self):
        """No row in PRICING that the transcription above does not check."""
        checked = {cost_lib.model_key(model) for model in PAGE_2026_10_17}
        self.assertEqual(checked, set(cost_lib.PRICING))

    def test_each_token_class_has_its_own_rate(self):
        """One class at a time, so swapping two rates cannot cancel out."""
        usage_by_class = (
            ({"input_tokens": 1_000_000}, 4.00),
            ({"output_tokens": 1_000_000}, 20.00),
            ({"cache_creation_input_tokens": 1_000_000}, 5.00),
            ({"cache_read_input_tokens": 1_000_000}, 0.20),
        )
        for usage, expected in usage_by_class:
            with self.subTest(usage=usage):
                self.assertAlmostEqual(
                    cost_lib.cost_of(usage, "claude-opus-5-5"), expected, places=6)

    def test_mixed_ttl_writes_split_by_the_breakdown(self):
        # 300k at 1h ($8) + 700k at 5m ($5) on Opus 5.5 = 2.40 + 3.50.
        usage = {"cache_creation_input_tokens": 1_000_000,
                 "cache_creation": {"ephemeral_1h_input_tokens": 300_000,
                                    "ephemeral_5m_input_tokens": 700_000}}
        self.assertAlmostEqual(cost_lib.cost_of(usage, "claude-opus-5-5"), 5.90, places=6)

    def test_no_breakdown_prices_writes_at_five_minutes(self):
        usage = {"cache_creation_input_tokens": 1_000_000}
        self.assertAlmostEqual(cost_lib.cost_of(usage, "claude-opus-5-5"), 5.00, places=6)

    def test_a_breakdown_larger_than_the_total_is_capped(self):
        # A corrupt breakdown cannot bill more 1h tokens than were written.
        usage = {"cache_creation_input_tokens": 100_000,
                 "cache_creation": {"ephemeral_1h_input_tokens": 900_000}}
        self.assertAlmostEqual(cost_lib.cost_of(usage, "claude-opus-5-5"), 0.80, places=6)

    def test_a_negative_breakdown_is_read_as_none(self):
        # A corrupt negative breakdown cannot move writes off the 1h rate or
        # drive the cost below zero: it prices exactly like no breakdown.
        no_breakdown = cost_lib.cost_of(
            {"cache_creation_input_tokens": 100}, "claude-opus-5-5")
        for value in (-1, -1_000_000):
            with self.subTest(value=value):
                usage = {"cache_creation_input_tokens": 100,
                         "cache_creation": {"ephemeral_1h_input_tokens": value}}
                self.assertAlmostEqual(
                    cost_lib.cost_of(usage, "claude-opus-5-5"), no_breakdown, places=9)
        self.assertGreater(no_breakdown, 0)

    def test_haiku_5_5_long_prompt_tier_starts_above_100k(self):
        inp, w5m, _w1h, read, out = HAIKU_5_5_OVER_100K
        at_threshold = {"input_tokens": 40_000, "cache_read_input_tokens": 50_000,
                        "cache_creation_input_tokens": 10_000, "output_tokens": 1_000}
        # 100,000 prompt tokens: standard rates (0.10, 0.125, 0.01, 0.50).
        standard = (40_000 * 0.10 + 10_000 * 0.125 + 50_000 * 0.01 + 1_000 * 0.50) / 1e6
        self.assertAlmostEqual(cost_lib.cost_of(at_threshold, "claude-haiku-5-5"),
                               standard, places=9)
        over = dict(at_threshold, input_tokens=40_001)
        # 100,001: every token of the request at the long-prompt rates.
        long_rate = (40_001 * inp + 10_000 * w5m + 50_000 * read + 1_000 * out) / 1e6
        self.assertAlmostEqual(cost_lib.cost_of(over, "claude-haiku-5-5"),
                               long_rate, places=9)

    def test_only_haiku_5_5_has_a_long_prompt_tier(self):
        big = {"input_tokens": 500_000}
        self.assertAlmostEqual(cost_lib.cost_of(big, "claude-haiku-4-5"), 0.50, places=6)
        self.assertAlmostEqual(cost_lib.cost_of(big, "claude-opus-5-5"), 2.00, places=6)

    def test_cache_reads_are_far_cheaper_than_fresh_input(self):
        """If this ever inverts, the ledger will recommend the wrong architecture."""
        fresh = cost_lib.cost_of({"input_tokens": 1_000_000}, "claude-opus-5")
        cached = cost_lib.cost_of({"cache_read_input_tokens": 1_000_000}, "claude-opus-5")
        self.assertLess(cached, fresh / 5)

    def test_unknown_model_returns_none(self):
        self.assertIsNone(cost_lib.cost_of({"input_tokens": 1000}, "mystery-model"))


class TestOffListPrice(unittest.TestCase):
    """Fast mode, a regional endpoint or another service tier is not list price."""

    def test_reasons(self):
        self.assertIsNone(cost_lib.off_list_price({}))
        self.assertIsNone(cost_lib.off_list_price(
            {"speed": "standard", "inference_geo": "global", "service_tier": "standard"}))
        self.assertEqual("speed=fast", cost_lib.off_list_price({"speed": "fast"}))
        self.assertEqual("inference_geo=us", cost_lib.off_list_price({"inference_geo": "us"}))
        self.assertEqual("service_tier=batch",
                         cost_lib.off_list_price({"service_tier": "batch"}))

    def test_an_off_list_message_is_neither_priced_nor_weighted_and_is_named(self):
        path = transcript([
            entry("claude-opus-5-5", {"output_tokens": 1000}),
            entry("claude-opus-5-5", {"output_tokens": 500, "speed": "fast"}),
        ])
        try:
            report = cost_lib.analyze(path)
        finally:
            os.unlink(path)
        summary = cost_lib.summarize(report)
        self.assertAlmostEqual(0.02, summary["total_cost"], places=9)
        self.assertEqual(1000, summary["total_opus_equivalent_output"])
        self.assertEqual(["claude-opus-5-5 (speed=fast)"], summary["unpriced_models"])
        self.assertEqual(["claude-opus-5-5 (speed=fast)"], summary["unweighted_models"])
        self.assertEqual(1500, summary["total_output"])

    def test_the_reason_survives_a_later_entry_without_it(self):
        rows = [entry("claude-opus-5-5", {"output_tokens": 3, "speed": "fast"},
                      message_id="msg_fast"),
                entry("claude-opus-5-5", {"output_tokens": 900}, message_id="msg_fast")]
        path = transcript(rows)
        try:
            summary = cost_lib.summarize(cost_lib.analyze(path))
        finally:
            os.unlink(path)
        self.assertEqual(0.0, summary["total_cost"])
        self.assertEqual(["claude-opus-5-5 (speed=fast)"], summary["unpriced_models"])


class TestOneHourWritesMerge(unittest.TestCase):

    def test_repeated_entries_merge_the_one_hour_share_by_max(self):
        # Streamed shape: the breakdown is on every entry; output only on the last.
        usage = {"cache_creation_input_tokens": 1_000_000, "output_tokens": 0,
                 "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000}}
        rows = [entry("claude-opus-5-5", dict(usage), message_id="m"),
                entry("claude-opus-5-5", {"output_tokens": 0}, message_id="m")]
        path = transcript(rows)
        try:
            summary = cost_lib.summarize(cost_lib.analyze(path))
        finally:
            os.unlink(path)
        # 1M one-hour writes on Opus 5.5 = $8, not the 5-minute $5.
        self.assertAlmostEqual(8.00, summary["total_cost"], places=6)


class TestAnalyze(unittest.TestCase):

    def test_splits_main_loop_from_inline_sidechains(self):
        """The whole reason the tool exists: architect spend vs everything else.

        An inline sidechain is a legacy transcript shape — real subagents now
        live in their own files. It lands in `other` because its agentType is
        unknowable, never in `executor`.
        """
        path = transcript([
            entry("claude-opus-5", {"input_tokens": 1_000_000}),
            entry("claude-sonnet-5", {"input_tokens": 1_000_000}, sidechain=True),
        ])
        try:
            report = cost_lib.analyze(path)
        finally:
            os.unlink(path)
        self.assertEqual(report["totals"]["main"]["messages"], 1)
        self.assertEqual(report["totals"]["other"]["messages"], 1)
        self.assertEqual(report["totals"]["executor"]["messages"], 0)
        self.assertAlmostEqual(report["totals"]["main"]["cost"], 5.00, places=4)
        # Sonnet 5 input is $2 per million on the 2026-10-17 page.
        self.assertAlmostEqual(report["totals"]["other"]["cost"], 2.00, places=4)

    def test_sidechain_entries_are_not_dropped(self):
        """Regression guard on the shared reader.

        watch_lib.iter_entries defaults to skipping sidechains — correct for the
        handover watcher, fatal here. If that default ever leaks back in, every
        executor becomes free and the split looks unbeatable.
        """
        path = transcript([entry("claude-sonnet-5", {"output_tokens": 1000}, sidechain=True)])
        try:
            report = cost_lib.analyze(path)
        finally:
            os.unlink(path)
        self.assertEqual(report["totals"]["other"]["messages"], 1)
        self.assertGreater(report["totals"]["other"]["cost"], 0)

    def test_unpriced_model_is_counted_but_not_costed(self):
        path = transcript([entry("mystery-model", {"input_tokens": 1_000_000})])
        try:
            report = cost_lib.analyze(path)
        finally:
            os.unlink(path)
        self.assertEqual(report["totals"]["main"]["messages"], 1)
        self.assertEqual(report["totals"]["main"]["cost"], 0.0)
        self.assertEqual(report["totals"]["main"]["unpriced"], 1)
        self.assertIn("mystery-model", cost_lib.summarize(report)["unpriced_models"])

    def test_entries_without_usage_are_skipped_not_fatal(self):
        path = transcript([
            {"message": "a bare string"},
            {"message": {"model": "claude-opus-5"}},
            {"no_message": True},
            entry("claude-opus-5", {"input_tokens": 100}),
        ])
        try:
            report = cost_lib.analyze(path)
        finally:
            os.unlink(path)
        self.assertEqual(report["totals"]["main"]["messages"], 1)

    def test_empty_transcript(self):
        path = transcript([])
        try:
            summary = cost_lib.summarize(cost_lib.analyze(path))
        finally:
            os.unlink(path)
        self.assertEqual(summary["total_cost"], 0.0)


class TestOneResponseCountedOnce(unittest.TestCase):
    """One API response is many JSONL entries. `_add` must run once per message."""

    def test_repeated_content_blocks_count_once(self):
        usage = {"input_tokens": 10, "output_tokens": 100,
                  "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30}
        path = transcript(content_block_entries("claude-opus-5", usage, blocks=4))
        try:
            summary = cost_lib.summarize(cost_lib.analyze(path))
        finally:
            os.unlink(path)
        self.assertEqual(100, summary["main_output"])
        self.assertEqual(10, summary["main_input"])
        self.assertEqual(20, summary["main_cache_write"])
        self.assertEqual(30, summary["main_cache_read"])
        self.assertEqual(1, summary["main_messages"])

    def test_streamed_partial_entries_keep_the_completed_output(self):
        """This is the test that fails if someone "fixes" the double-count by
        keeping the first entry — name it so that intent survives."""
        usage = {"input_tokens": 10, "output_tokens": 5000,
                  "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30}
        path = transcript(streamed_entries("claude-opus-5", usage))
        try:
            summary = cost_lib.summarize(cost_lib.analyze(path))
        finally:
            os.unlink(path)
        self.assertEqual(5000, summary["main_output"])

    def test_a_trailing_all_zero_entry_does_not_erase_the_message(self):
        """This is the test that fails if someone "simplifies" the rule to
        keeping the last entry — the other tempting shortcut, and the one that
        looks most obviously correct. Name it so that intent survives."""
        usage = {"input_tokens": 10, "output_tokens": 773,
                  "cache_creation_input_tokens": 20, "cache_read_input_tokens": 578973}
        path = transcript(trailing_zero_entries("claude-opus-5", usage))
        try:
            summary = cost_lib.summarize(cost_lib.analyze(path))
        finally:
            os.unlink(path)
        self.assertEqual(773, summary["main_output"])
        self.assertEqual(578973, summary["main_cache_read"])

    def test_two_distinct_messages_still_sum(self):
        """Guards against a fix that collapses everything into one bucket."""
        path = transcript([
            entry("claude-opus-5", {"output_tokens": 100}, message_id="msg_a"),
            entry("claude-opus-5", {"output_tokens": 100}, message_id="msg_b"),
        ])
        try:
            summary = cost_lib.summarize(cost_lib.analyze(path))
        finally:
            os.unlink(path)
        self.assertEqual(200, summary["main_output"])

    def test_the_same_message_id_in_two_lanes_does_not_merge(self):
        """Grouping is per lane, deliberately."""
        path = session_with_agents(
            [entry("claude-opus-5", {"output_tokens": 50}, message_id="msg_shared")],
            {"agent-aaa.jsonl": [entry("claude-opus-5", {"output_tokens": 50},
                                       message_id="msg_shared", sidechain=True)]})
        summary = cost_lib.summarize(cost_lib.analyze(path))
        self.assertEqual(50, summary["main_output"])
        self.assertEqual(50, summary["other_output"])

    def test_an_entry_without_a_message_id_is_still_counted(self):
        """Never seen in the real corpus, but the failure direction matters:
        an id-less entry must be counted, not dropped."""
        row = {"message": {"model": "claude-opus-5", "usage": {"output_tokens": 42}}}
        path = transcript([row])
        try:
            summary = cost_lib.summarize(cost_lib.analyze(path))
        finally:
            os.unlink(path)
        self.assertEqual(42, summary["main_output"])
        self.assertEqual(1, summary["main_messages"])

    def test_cost_and_opus_equivalent_follow_the_deduped_usage(self):
        usage = {"input_tokens": 10, "output_tokens": 100,
                  "cache_creation_input_tokens": 20, "cache_read_input_tokens": 30}
        model = "claude-opus-5"
        path = transcript(content_block_entries(model, usage, blocks=4))
        try:
            summary = cost_lib.summarize(cost_lib.analyze(path))
        finally:
            os.unlink(path)
        self.assertAlmostEqual(cost_lib.cost_of(usage, model), summary["total_cost"],
                               places=4)
        weight, _is_floor = cost_lib.usage_weight(model)
        self.assertEqual(round(usage["output_tokens"] * weight),
                         summary["total_opus_equivalent_output"])


class TestAgentFileDiscovery(unittest.TestCase):
    def test_finds_both_task_and_workflow_agent_files(self):
        path = session_with_agents(
            [entry("claude-opus-5", {"output_tokens": 10})],
            {
                "agent-aaa.jsonl": [entry("claude-sonnet-5", {"output_tokens": 5}, sidechain=True)],
                "workflows/wf_x/agent-bbb.jsonl": [
                    entry("claude-opus-5", {"output_tokens": 7}, sidechain=True)],
            })
        found = cost_lib.agent_files(path)
        self.assertEqual(2, len(found))
        self.assertTrue(any("workflows" in f for f in found))
        self.assertTrue(any(f.endswith("agent-aaa.jsonl") for f in found))

    def test_no_subagents_directory_is_not_an_error(self):
        path = transcript([entry("claude-opus-5", {"output_tokens": 10})])
        self.assertEqual([], cost_lib.agent_files(path))

    def test_meta_json_files_are_not_mistaken_for_transcripts(self):
        # agent-<id>.meta.json sits beside agent-<id>.jsonl. A careless glob of
        # "agent-*" would read the metadata as a transcript and count nothing.
        path = session_with_agents(
            [entry("claude-opus-5", {"output_tokens": 10})],
            {"agent-aaa.jsonl": [entry("claude-sonnet-5", {"output_tokens": 5}, sidechain=True)]})
        meta = os.path.join(os.path.splitext(path)[0], "subagents", "agent-aaa.meta.json")
        with open(meta, "w") as handle:
            json.dump({"agentType": "executor", "model": "sonnet"}, handle)
        self.assertEqual(1, len(cost_lib.agent_files(path)))

    def test_analyze_counts_tokens_from_agent_files(self):
        path = session_with_agents(
            [entry("claude-opus-5", {"output_tokens": 10})],
            {"agent-aaa.jsonl": [entry("claude-sonnet-5", {"output_tokens": 5}, sidechain=True)]})
        report = cost_lib.analyze(path)
        non_main = sum(lane["messages"]
                       for name, lane in report["totals"].items() if name != "main")
        self.assertEqual(1, non_main,
                         "agent-file messages must land in a non-main lane")


class TestWorktreeSubagentDiscovery(unittest.TestCase):
    """Subagents of an EnterWorktree session land in a sibling project dir."""

    def _tree(self, projects_name="projects"):
        base = tempfile.mkdtemp()
        projects = os.path.join(base, projects_name)
        sid = str(uuid.uuid4())
        own = os.path.join(projects, "-start")
        os.makedirs(own)
        path = os.path.join(own, sid + ".jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps(entry("claude-opus-5", {"output_tokens": 10})) + "\n")
        return projects, sid, path

    def _agent(self, projects, slug, sid, rel, tokens=5, agent_type=None):
        target = os.path.join(projects, slug, sid, "subagents", rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as handle:
            handle.write(json.dumps(entry(
                "claude-sonnet-5", {"output_tokens": tokens}, sidechain=True)) + "\n")
        if agent_type:
            meta = target[:-len(".jsonl")] + ".meta.json"
            with open(meta, "w") as handle:
                json.dump({"agentType": agent_type}, handle)
        return target

    def test_finds_agents_in_a_sibling_project_dir(self):
        projects, sid, path = self._tree()
        agent = self._agent(projects, "-start-worktree", sid, "agent-aaa.jsonl",
                            tokens=7, agent_type="toolkit-build:executor")
        self.assertEqual([agent], cost_lib.agent_files(path))
        self.assertEqual(["toolkit-build:executor"], cost_lib.agent_types(path))
        report = cost_lib.analyze(path)
        self.assertEqual(7, report["totals"]["executor"]["output"])

    def test_agents_in_both_dirs_are_all_counted(self):
        projects, sid, path = self._tree()
        self._agent(projects, "-start", sid, "agent-aaa.jsonl")
        self._agent(projects, "-start-worktree", sid, "agent-bbb.jsonl")
        self._agent(projects, "-start-worktree", sid, "workflows/wf_x/agent-ccc.jsonl")
        self.assertEqual(3, len(cost_lib.agent_files(path)))

    def test_own_dir_is_not_counted_twice(self):
        projects, sid, path = self._tree()
        self._agent(projects, "-start", sid, "agent-aaa.jsonl")
        self._agent(projects, "-start", sid, "agent-bbb.jsonl")
        self.assertEqual(2, len(cost_lib.agent_files(path)))

    def test_other_session_ids_in_sibling_dirs_are_ignored(self):
        projects, sid, path = self._tree()
        self._agent(projects, "-other", str(uuid.uuid4()), "agent-zzz.jsonl")
        self._agent(projects, "-suffix", sid + "-suffix", "agent-yyy.jsonl")
        self.assertEqual([], cost_lib.agent_files(path))

    def test_no_sibling_scan_outside_a_projects_dir(self):
        projects, sid, path = self._tree(projects_name="not-projects")
        self._agent(projects, "-start-worktree", sid, "agent-aaa.jsonl")
        self.assertEqual([], cost_lib.agent_files(path))

    def test_relative_transcript_path_still_scans_siblings(self):
        # The CLI takes whatever path the user typed. A bare "x.jsonl" has no
        # grandparent to name until it is made absolute.
        projects, sid, path = self._tree()
        self._agent(projects, "-start-worktree", sid, "agent-aaa.jsonl")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(os.path.dirname(path))
        self.assertEqual(1, len(cost_lib.agent_files(os.path.basename(path))))

    def test_meta_json_in_sibling_dir_is_not_a_transcript(self):
        projects, sid, path = self._tree()
        sibling = os.path.join(projects, "-start-worktree", sid, "subagents")
        os.makedirs(sibling)
        with open(os.path.join(sibling, "agent-aaa.meta.json"), "w") as handle:
            json.dump({"agentType": "executor"}, handle)
        self.assertEqual([], cost_lib.agent_files(path))

    def test_projects_listdir_failure_keeps_own_dir_agents(self):
        projects, sid, path = self._tree()
        own_agent = self._agent(projects, "-start", sid, "agent-aaa.jsonl")
        real_listdir = os.listdir
        reached = []

        def flaky_listdir(target="."):
            if os.path.realpath(target) == os.path.realpath(projects):
                reached.append(target)
                raise OSError("injected")
            return real_listdir(target)

        with mock.patch.object(cost_lib.os, "listdir", flaky_listdir):
            found = cost_lib.agent_files(path)
        self.assertTrue(reached, "the projects dir was never listed")
        self.assertEqual([own_agent], found)


class TestLaneClassification(unittest.TestCase):
    def _session(self, agent_type):
        rows = {"agent-aaa.jsonl": [
            entry("claude-sonnet-5", {"output_tokens": 5}, sidechain=True)]}
        path = session_with_agents(
            [entry("claude-opus-5", {"output_tokens": 10})], rows)
        meta = os.path.join(os.path.splitext(path)[0], "subagents", "agent-aaa.meta.json")
        if agent_type is not None:
            with open(meta, "w") as handle:
                json.dump({"agentType": agent_type}, handle)
        return path

    def test_executor_agent_lands_in_executor_lane(self):
        report = cost_lib.analyze(self._session("executor"))
        self.assertEqual(1, report["totals"]["executor"]["messages"])
        self.assertEqual(0, report["totals"]["review"]["messages"])

    def test_task_reviewer_lands_in_review_not_executor(self):
        # The regression this task exists to prevent. A path heuristic puts
        # task-reviewer in `executor`, which is the conflation the four-lane
        # model was designed to remove.
        report = cost_lib.analyze(self._session("task-reviewer"))
        self.assertEqual(1, report["totals"]["review"]["messages"])
        self.assertEqual(0, report["totals"]["executor"]["messages"])

    def test_workflow_subagent_lands_in_review(self):
        report = cost_lib.analyze(self._session("workflow-subagent"))
        self.assertEqual(1, report["totals"]["review"]["messages"])

    def test_architect_reviewer_lands_in_review_not_other(self):
        # architect-reviewer is a review agent, same as task-reviewer and
        # code-reviewer. Missing from the table, it fell into `other`.
        report = cost_lib.analyze(self._session("architect-reviewer"))
        self.assertEqual(1, report["totals"]["review"]["messages"])
        self.assertEqual(0, report["totals"]["other"]["messages"])

    def test_general_purpose_lands_in_other(self):
        report = cost_lib.analyze(self._session("general-purpose"))
        self.assertEqual(1, report["totals"]["other"]["messages"])
        self.assertEqual(0, report["totals"]["executor"]["messages"])

    def test_missing_meta_file_is_other_and_is_named(self):
        report = cost_lib.analyze(self._session(None))
        self.assertEqual(1, report["totals"]["other"]["messages"])
        self.assertIn("<no meta>", cost_lib.summarize(report)["unknown_agent_types"])

    def test_unrecognised_agent_type_is_other_and_is_named(self):
        report = cost_lib.analyze(self._session("brand-new-agent-kind"))
        self.assertEqual(1, report["totals"]["other"]["messages"])
        self.assertIn("brand-new-agent-kind",
                      cost_lib.summarize(report)["unknown_agent_types"])

    def test_inline_sidechain_is_other_not_executor(self):
        # Legacy transcripts wrote subagents inline. We cannot know their type,
        # so they must not be credited to the executor lane, which is the
        # number the routing decision turns on.
        path = transcript([
            entry("claude-opus-5", {"output_tokens": 10}),
            entry("claude-sonnet-5", {"output_tokens": 5}, sidechain=True)])
        report = cost_lib.analyze(path)
        self.assertEqual(1, report["totals"]["other"]["messages"])
        self.assertEqual(0, report["totals"]["executor"]["messages"])


class TestSummarizeFourLanes(unittest.TestCase):
    def test_returns_a_key_per_lane(self):
        report = cost_lib.analyze(transcript([entry("claude-opus-5", {"output_tokens": 10})]))
        summary = cost_lib.summarize(report)
        for key in ("main_cost", "executor_cost", "review_cost", "other_cost",
                    "main_messages", "executor_messages", "review_messages",
                    "other_messages", "unknown_agent_types"):
            self.assertIn(key, summary)


class TestTokenFirstReport(unittest.TestCase):
    def test_summarize_exposes_output_tokens_per_lane(self):
        report = cost_lib.analyze(transcript([
            entry("claude-opus-5", {"output_tokens": 1234, "input_tokens": 7,
                                    "cache_creation_input_tokens": 20,
                                    "cache_read_input_tokens": 300})]))
        summary = cost_lib.summarize(report)
        self.assertEqual(1234, summary["main_output"])
        self.assertEqual(1234, summary["total_output"])
        self.assertEqual(7, summary["main_input"])
        # Distinct, non-equal values: defect #2's mutation evidence is that
        # transposing cache_creation_input_tokens and cache_read_input_tokens
        # inside _add left every test green, because none asserted a cache
        # figure at all.
        self.assertEqual(20, summary["main_cache_write"])
        self.assertEqual(300, summary["main_cache_read"])
        for lane in ("executor", "review", "other"):
            self.assertEqual(0, summary["%s_output" % lane])

    def test_the_opus_equivalent_total_equals_the_sum_of_its_lanes(self):
        """The per-lane figures are printed beside the total, so it must add up.

        `round(sum of unrounded)` and `sum of rounded` differ by up to 2, and on
        a real session they did differ. That
        was invisible while only the total was printed. The whole reason for the
        per-lane column is that a reader can check the headline against the rows,
        so a table that does not reconcile costs more than a token of precision.

        The fixture needs Sonnet output in TWO SEPARATE lanes, which is why it
        goes through `_agent_session` rather than `session_with_agents`: without
        a meta.json every agent lands in `other`, one lane's fractions get
        summed before rounding, and the test passes under the very mutation it
        exists to catch. Verified: this fixture fails when
        `total_opus_equivalent_output` reverts to `round(sum of unrounded)`.

        Three Sonnet 5 tokens weigh 1.5. Two lanes of it round to 2 + 2 = 4,
        while round(1.5 + 1.5) = 3. Only per-lane fractions expose the
        discrepancy, so integral weights (Opus 5.5 at 1.0) alone would prove
        nothing.
        """
        report = cost_lib.analyze(_agent_session(
            entry("claude-opus-5-5", {"output_tokens": 1000}),
            {"exec": ("claude-sonnet-5", 3, "executor"),
             "rev": ("claude-sonnet-5", 3, "task-reviewer")}))
        summary = cost_lib.summarize(report)
        lanes = sum(summary["%s_opus_equivalent_output" % name]
                    for name in cost_lib.LANES)
        self.assertEqual(
            lanes, summary["total_opus_equivalent_output"],
            "the printed total must equal the printed lanes, or the report "
            "invites a reader to add up the rows and find it wrong")

    def test_report_names_tokens_before_dollars(self):
        path = transcript([entry("claude-opus-5", {"output_tokens": 1234})])
        result = subprocess.run(
            [sys.executable, CLI, path, "--no-record"],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        out = result.stdout
        self.assertIn("1,234", out, "output tokens must be visible in the report")
        # `in` before `index`: str.index raises ValueError on a missing
        # substring, which reads as a broken test rather than a failed
        # assertion and invites someone to weaken it.
        self.assertIn("output tokens", out)
        self.assertIn("$", out)
        self.assertLess(out.index("output tokens"), out.index("$"),
                        "tokens must be reported before dollars")

    def test_report_labels_dollars_as_a_proxy(self):
        path = transcript([entry("claude-opus-5", {"output_tokens": 1234})])
        result = subprocess.run(
            [sys.executable, CLI, path, "--no-record"],
            capture_output=True, text=True)
        self.assertIn("proxy", result.stdout.lower(),
                      "a dollar figure without the word 'proxy' reads as a bill")


class TestUsageWeight(unittest.TestCase):
    # Output rates from the 2026-10-17 page over Opus 5.5's $20, by hand.

    def test_opus_5_5_is_the_reference(self):
        self.assertEqual((1.0, False), cost_lib.usage_weight("claude-opus-5-5[1m]"))

    def test_priced_models_derive_from_their_output_rate(self):
        for model, expected in (("claude-sonnet-5", 0.5),      # 10 / 20
                                ("claude-opus-5", 1.25),       # 25 / 20
                                ("claude-opus-4-1", 3.75),     # 75 / 20
                                ("claude-haiku-5-5", 0.025),   # 0.50 / 20
                                ("claude-fable-5-1", 2.5)):    # 50 / 20
            with self.subTest(model=model):
                weight, is_floor = cost_lib.usage_weight(model)
                self.assertAlmostEqual(expected, weight)
                self.assertFalse(is_floor)

    def test_no_stated_or_floored_weight_ships(self):
        """Every default weight comes from a published price."""
        self.assertEqual({}, cost_lib.USAGE_WEIGHT)
        self.assertEqual(frozenset(), cost_lib.FLOOR_WEIGHTS)

    def test_fable_is_priced_from_its_public_rate(self):
        # 100 output tokens at $50 per million.
        self.assertAlmostEqual(
            0.005, cost_lib.cost_of({"output_tokens": 100}, "claude-fable-5-1"), places=9)

    def test_an_override_can_mark_a_floor(self):
        with mock.patch.object(cost_lib, "USAGE_WEIGHT", {("fable", "5.1"): 3.0}), \
                mock.patch.object(cost_lib, "FLOOR_WEIGHTS", frozenset({("fable", "5.1")})):
            self.assertEqual((3.0, True), cost_lib.usage_weight("claude-fable-5-1"))

    def test_unknown_model_has_no_weight(self):
        self.assertIsNone(cost_lib.usage_weight("some-future-model"))


def _report_with_floor(path, floor_key=("fable", "5.1")):
    """Run the CLI in-process with `floor_key` marked as a floored weight.

    No floor ships by default, so the floor path is reachable only through the
    override; a subprocess would not see the patch.
    """
    import contextlib
    import importlib.machinery
    import importlib.util
    import io
    loader = importlib.machinery.SourceFileLoader(
        "claude_build_cost_floor", os.path.join(TOOL_DIR, "claude-build-cost"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)
    out = io.StringIO()
    with mock.patch.object(cost_lib, "FLOOR_WEIGHTS", frozenset({floor_key})), \
            contextlib.redirect_stdout(out):
        code = cli.main([path, "--no-record"])
    return code, out.getvalue()


class TestOpusEquivalent(unittest.TestCase):
    def test_fable_output_is_weighted_from_its_price(self):
        report = cost_lib.analyze(transcript([
            entry("claude-fable-5-1", {"output_tokens": 1000})]))
        summary = cost_lib.summarize(report)
        self.assertEqual(2500, summary["total_opus_equivalent_output"])
        self.assertFalse(summary["opus_equivalent_is_floor"])
        self.assertEqual([], summary["unpriced_models"])
        self.assertAlmostEqual(0.05, summary["total_cost"], places=9)

    def test_sonnet_output_is_discounted(self):
        report = cost_lib.analyze(transcript([
            entry("claude-sonnet-5", {"output_tokens": 1000})]))
        summary = cost_lib.summarize(report)
        self.assertEqual(500, summary["total_opus_equivalent_output"])
        self.assertFalse(summary["opus_equivalent_is_floor"])

    def test_unweighted_model_is_excluded_and_named(self):
        report = cost_lib.analyze(transcript([
            entry("some-future-model", {"output_tokens": 1000})]))
        summary = cost_lib.summarize(report)
        self.assertEqual(0, summary["total_opus_equivalent_output"])
        self.assertIn("some-future-model", summary["unweighted_models"])

    def test_floor_marker_reaches_the_report(self):
        path = transcript([entry("claude-fable-5-1", {"output_tokens": 1000})])
        code, out = _report_with_floor(path)
        self.assertEqual(0, code)
        self.assertIn("≥", out, "a floored total must print with the >= marker")

    def test_no_floor_marker_by_default(self):
        path = transcript([entry("claude-fable-5-1", {"output_tokens": 1000})])
        result = subprocess.run(
            [sys.executable, CLI, path, "--no-record"],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotIn("≥", result.stdout)


class TestZeroTokenMessagesAreNotUnpriced(unittest.TestCase):
    """A message that contributes zero tokens cannot understate
    anything, so it must not fire NOT PRICED or NOT WEIGHTED."""

    def test_a_zero_token_unknown_model_is_not_reported_unpriced(self):
        report = cost_lib.analyze(transcript([
            entry("<synthetic>", {"input_tokens": 0, "output_tokens": 0,
                                  "cache_creation_input_tokens": 0,
                                  "cache_read_input_tokens": 0})]))
        summary = cost_lib.summarize(report)
        self.assertEqual([], summary["unpriced_models"])
        self.assertEqual([], summary["unweighted_models"])

    def test_an_unknown_model_that_spent_tokens_is_still_reported(self):
        """This is the test that stops the fix from muting a real warning."""
        report = cost_lib.analyze(transcript([
            entry("<synthetic>", {"output_tokens": 100})]))
        summary = cost_lib.summarize(report)
        self.assertIn("<synthetic>", summary["unpriced_models"])
        self.assertIn("<synthetic>", summary["unweighted_models"])


def _agent_session(main_row, agents):
    """`session_with_agents` plus the meta.json each agent needs to land in a
    lane. `agents` maps a short name -> (model, output_tokens, agentType)."""
    files = {"agent-%s.jsonl" % name: [
        entry(model, {"output_tokens": tokens}, sidechain=True)]
        for name, (model, tokens, _agent_type) in agents.items()}
    path = session_with_agents([main_row], files)
    subagents_dir = os.path.join(os.path.splitext(path)[0], "subagents")
    for name, (_model, _tokens, agent_type) in agents.items():
        meta = os.path.join(subagents_dir, "agent-%s.meta.json" % name)
        with open(meta, "w") as handle:
            json.dump({"agentType": agent_type}, handle)
    return path


def _lane_row(stdout, lane_name):
    """The per-lane table row for `lane_name`, or None. Matched on the first
    whitespace-split field so "review" (the row) is not confused with
    "review-gate" (the headline share label)."""
    for line in stdout.splitlines():
        fields = line.strip().split()
        if fields and fields[0] == lane_name:
            return line
    return None


class TestWeightedHeadlineShare(unittest.TestCase):
    """The two headline share lines, and the per-lane opus-eq column that
    lets a reader check them."""

    def test_headline_share_is_weighted_not_raw(self):
        """Raw share would read 75% (1500/2000); weighted reads 60%
        (750/1250, since Sonnet 5's weight is 0.5 against Opus 5.5). A fixture
        where the two coincide proves nothing."""
        path = _agent_session(
            entry("claude-opus-5-5", {"output_tokens": 500}),
            {"exec": ("claude-sonnet-5", 1500, "executor")})
        result = subprocess.run(
            [sys.executable, CLI, path, "--no-record"],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("executor share of window draw: 60%", result.stdout)
        self.assertNotIn("75%", result.stdout)

    def test_review_gate_share_is_pinned(self):
        """No assertion in either suite reads this line — only the executor
        share above is pinned. Verified: pointing the numerator at
        `main_opus_equivalent_output` instead of `review_opus_equivalent_output`
        left the whole suite green before this was added. Fixture chosen so
        main/executor/review opus-equivalent output are 100/500/400 (Opus 5.5,
        Sonnet 5 at 0.5, Opus 5.5) against a weighted total of 1000 — three
        DISTINCT shares (10%/50%/40%), so the review line cannot pass by
        reading main's or executor's share instead of its own."""
        path = _agent_session(
            entry("claude-opus-5-5", {"output_tokens": 100}),
            {"exec": ("claude-sonnet-5", 1000, "executor"),
             "rev": ("claude-opus-5-5", 400, "task-reviewer")})
        result = subprocess.run(
            [sys.executable, CLI, path, "--no-record"],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("review-gate share            : 40%", result.stdout)
        self.assertNotIn("review-gate share            : 10%", result.stdout,
                         "must not read main's share instead of review's")

    def test_the_per_lane_opus_eq_column_reads_the_weighted_value(self):
        """The report's per-lane `opus-eq` column had no value assertion —
        only the presence or absence of `≥` was checked, which survives the
        column printing raw output instead of the weighted figure. Sonnet 5's
        weight is 0.5, so 1,000 raw output tokens weigh 500 — asserting both
        numbers on the executor row proves the column is the weighted one,
        not a second copy of the output column."""
        path = _agent_session(
            entry("claude-opus-5-5", {"output_tokens": 10}),
            {"exec": ("claude-sonnet-5", 1000, "executor")})
        result = subprocess.run(
            [sys.executable, CLI, path, "--no-record"],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        executor_line = _lane_row(result.stdout, "executor")
        self.assertIsNotNone(executor_line)
        self.assertIn("1,000", executor_line,
                      "the raw output column must show the raw token count")
        self.assertIn(" 500 ", executor_line,
                      "the opus-eq column must show sonnet's WEIGHTED figure "
                      "(0.5x), not a repeat of the raw output")

    # `test_the_floor_caveat_prints_for_a_fable_bearing_session` was folded into
    # the test below rather than kept. Rewriting the caveat to name both
    # directions invalidated its literal-string assertion, and the surviving
    # half — `assertIn("minimum share")` — was a strict subset of the next test
    # against an identical fixture. Two tests where one is strictly weaker
    # invites someone to satisfy the weak one and think the line is covered.

    def test_the_floor_caveat_names_both_directions(self):
        """The previous round's finding, restated: `!` and `*` were split
        apart because one marker cannot carry two meanings. The floor caveat
        had the same shape in prose — it named the minimum direction (a lane
        containing the floored model) and said nothing about the maximum
        direction (every OTHER lane's share, computed against a total that may
        be larger than the floored figure printed for it)."""
        path = transcript([entry("claude-fable-5-1", {"output_tokens": 1000})])
        code, out = _report_with_floor(path)
        self.assertEqual(0, code)
        self.assertIn("minimum share", out)
        self.assertIn("maximum", out)

    def test_a_floored_lane_is_marked_and_an_unfloored_one_is_not(self):
        """A test asserting only the presence of a marker passes on an
        implementation that marks every row — this asserts the executor
        row, which carries no floored model, does NOT get the marker."""
        path = _agent_session(
            entry("claude-opus-5-5", {"output_tokens": 10}),
            {"exec": ("claude-sonnet-5", 500, "executor"),
             "rev": ("claude-fable-5-1", 500, "task-reviewer")})
        code, out = _report_with_floor(path)
        self.assertEqual(0, code)
        review_line = _lane_row(out, "review")
        executor_line = _lane_row(out, "executor")
        self.assertIsNotNone(review_line)
        self.assertIsNotNone(executor_line)
        self.assertIn("≥", review_line,
                      "the review row carries the floored model and must read as a floor")
        self.assertNotIn("≥", executor_line,
                         "the executor row carries no floored model and must not read as a floor")

    def test_a_lane_with_an_unweighted_model_is_marked_not_silently_zero(self):
        """The mirror half lives here too: a positive-only assertion on the
        executor row passes an implementation that marks EVERY lane, or one
        that deletes the legend and marks the whole session. The main row
        spent only opus (weighted, not unweighted), so it must not carry
        `!` — proving the marker is per-lane, not blanket."""
        path = _agent_session(
            entry("claude-opus-5", {"output_tokens": 1000}),
            {"exec": ("some-future-model", 500, "executor")})
        result = subprocess.run(
            [sys.executable, CLI, path, "--no-record"],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        executor_line = _lane_row(result.stdout, "executor")
        main_line = _lane_row(result.stdout, "main")
        self.assertIsNotNone(executor_line)
        self.assertIsNotNone(main_line)
        self.assertIn("!", executor_line)
        self.assertNotIn("!", main_line,
                         "main spent no unweighted model and must not carry the marker")
        self.assertIn("some-future-model", result.stdout)
        self.assertIn("shares are understated for that lane", result.stdout)

    def test_a_session_with_no_weighted_model_says_so_rather_than_nothing(self):
        """This is the `elif` branch: without this test it is unreachable by
        any other fixture in the suite."""
        path = transcript([entry("some-future-model", {"output_tokens": 777})])
        result = subprocess.run(
            [sys.executable, CLI, path, "--no-record"],
            capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("NO SHARE AVAILABLE", result.stdout)
        self.assertIn("777", result.stdout)


class TestLedger(unittest.TestCase):

    def test_appends_rather_than_overwrites(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "nested", "ledger.jsonl")
            self.assertTrue(cost_lib.append_ledger(path, {"total_cost": 1.0}))
            self.assertTrue(cost_lib.append_ledger(path, {"total_cost": 2.0}))
            with open(path) as handle:
                rows = [json.loads(line) for line in handle]
        self.assertEqual([r["total_cost"] for r in rows], [1.0, 2.0])

    def test_unwritable_path_returns_false_without_raising(self):
        """A ledger write must never be able to fail a build."""
        self.assertFalse(cost_lib.append_ledger("/nonexistent-root/x.jsonl", {"a": 1}))


class TestDocstringIntegrity(unittest.TestCase):
    """`lane_stats` carries a decision record in its docstring, and a 2026-08-30
    edit silently orphaned it.

    The new paragraph was inserted ABOVE the existing docstring rather than
    merged into it, leaving two consecutive string literals. Python takes the
    first as `__doc__`; the original docstring became a no-op expression
    statement. Nothing failed, nothing warned, and `help()` quietly stopped
    returning the matched-pair warning about `estimate_lib.DEFAULT_RATIO`, the
    median-not-mean choice, and the zero-skipping rule that produced a measured
    false forecast.

    Found by a blinded review, not by the suite. This class is the guard that
    was missing.
    """

    def test_lane_stats_keeps_its_whole_decision_record(self):
        doc = cost_lib.lane_stats.__doc__
        self.assertIsNotNone(doc, "lane_stats lost its docstring entirely")
        for needle in ("UNIT MISMATCH", "MEDIAN, not mean"):
            self.assertIn(
                needle, doc,
                "`lane_stats.__doc__` no longer contains %r. If a paragraph was "
                "added above the existing docstring, Python is returning only "
                "the first literal and the rest is a no-op expression — merge "
                "them into one string." % needle)

    def test_no_function_in_cost_lib_has_an_orphaned_second_docstring(self):
        """The general form, so this cannot recur elsewhere in the module by the
        same edit. Two leading string literals is always the bug: the second is
        dead text that reads exactly like documentation."""
        import ast
        tree = ast.parse(pathlib.Path(cost_lib.__file__).read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.ClassDef)):
                continue
            leading = 0
            for stmt in node.body:
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                        and isinstance(stmt.value.value, str):
                    leading += 1
                    continue
                break
            self.assertLessEqual(
                leading, 1,
                "%s at line %d opens with %d string literals. Only the first is "
                "__doc__; the others are dead text that reads like documentation"
                % (node.name, node.lineno, leading))


class TestCLI(unittest.TestCase):

    def _run(self, *args):
        return subprocess.run((sys.executable, CLI) + args,
                              capture_output=True, text=True, timeout=60)

    def test_reports_and_records(self):
        with tempfile.TemporaryDirectory() as root:
            path = transcript([
                entry("claude-opus-5", {"input_tokens": 1_000_000}),
                entry("claude-sonnet-5", {"input_tokens": 1_000_000}, sidechain=True),
            ])
            # CLAUDE_TOOLKIT_CONFIG must be neutralised, not just HOME. Overriding
            # HOME alone leaves the ambient variable inherited, so on a machine
            # whose real config sets build_cost.ledger_path the tool writes
            # somewhere else entirely and this test fails for a reason that has
            # nothing to do with the code — the hermeticity rule in CLAUDE.md.
            env = dict(os.environ, HOME=root,
                       CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
            try:
                result = subprocess.run(
                    (sys.executable, CLI, path, "--label", "test build"),
                    capture_output=True, text=True, timeout=60, env=env)
            finally:
                os.unlink(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            for lane in ("main", "executor", "review", "other"):
                self.assertIn(lane, result.stdout,
                              "lane %r missing from the report" % lane)
            # Asserted directly, not as one arm of an `or`. The old form was
            # `exists(default) or exists(ledger)` where nothing ever configured
            # `ledger`, so the second arm could not be true and the assertion
            # silently reduced to this one.
            default_ledger = os.path.join(root, ".claude", "build-costs.jsonl")
            self.assertTrue(os.path.exists(default_ledger),
                            "no ledger written to the default path: %s" % result.stdout)

    def test_configured_ledger_path_is_honoured(self):
        """The configured-path branch of ledger_path(), which nothing covered.

        Its absence is why a real bug stayed green: claude-build-cost resolved
        lib/ with two dirname calls instead of three, so `import toolkit_config`
        worked only because an earlier import had already put the module in
        sys.modules. Had that chain changed, `except ImportError` would have set
        _config = None and every configured ledger_path would have been ignored
        in silence. A test that only ever exercises the default cannot see that.
        """
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "custom", "ledger.jsonl")
            config_path = os.path.join(root, "config.json")
            with open(config_path, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)

            path = transcript([entry("claude-opus-5", {"input_tokens": 1_000_000})])
            env = dict(os.environ, HOME=root, CLAUDE_TOOLKIT_CONFIG=config_path)
            try:
                result = subprocess.run(
                    (sys.executable, CLI, path, "--label", "configured"),
                    capture_output=True, text=True, timeout=60, env=env)
            finally:
                os.unlink(path)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.exists(ledger),
                            "build_cost.ledger_path was ignored; wrote nothing to %s" % ledger)
            default_ledger = os.path.join(root, ".claude", "build-costs.jsonl")
            self.assertFalse(os.path.exists(default_ledger),
                             "wrote to the default path despite a configured one")

    def test_ledger_reads_old_subagent_rows_without_crashing(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-01-01T00:00:00", "label": "old row",
                    "total_cost": 1.0, "main_cost": 0.6, "subagent_cost": 0.4}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("old row", result.stdout)

    def test_ledger_reads_new_four_lane_rows(self):
        """Retargeted for the token-first view: the fixture used to
        carry only USD keys and assert a per-lane dollar figure the ledger no
        longer prints. The executors column now reads opus-equivalent output
        tokens, so the fixture must carry that key and the assertion must
        check the *executor* one specifically — the same guard as before,
        re-pointed at the new key rather than retired."""
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-02-02T00:00:00", "label": "new row",
                    "total_cost": 10.0, "main_cost": 6.0, "executor_cost": 3.0,
                    "review_cost": 1.0, "other_cost": 0.0,
                    "total_output": 4000,
                    "total_opus_equivalent_output": 3700,
                    "main_opus_equivalent_output": 1000,
                    "executor_opus_equivalent_output": 2000,
                    "review_opus_equivalent_output": 700,
                    "other_opus_equivalent_output": 0}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("new row", result.stdout)
            # The executors column must read the new key, not fall through to a
            # missing key and print 0 (or —) across a ledger of real builds —
            # the defect moving from analyze() into the consumer view.
            self.assertIn("2,000", result.stdout)
            # The review-gate footer share must not silently read 0% when the
            # row actually carries review spend.
            self.assertNotIn("review gates 0% of it", result.stdout)
            # Pinned to the actual value: 700/3700 = 18.9...% -> 19%. Main's
            # opus-equivalent output (1000) would give 27% instead — distinct
            # from review's 19%, so a footer numerator swapped to
            # `main_opus_equivalent_output` cannot satisfy this by accident.
            self.assertIn("review gates 19% of it", result.stdout,
                          "the footer's review share must read the review "
                          "lane, not another one")

    @staticmethod
    def _kind_of(line):
        """The kind cell of a rendered ledger row, BY POSITION.

        Necessary rather than tidy. Asserting `assertIn("—", line)` was vacuous:
        `_lane_cell` renders `—` for any lane a sparse fixture omits, so the
        dash matched came from the lane columns and the kind cell was never
        observed. A reviewer's mutation — `_kind_cell` returning "gate" for an
        empty `agent_types` — survived the whole suite because of it.

        `when` is an ISO timestamp with no spaces, so the kind cell is always
        field 1.
        """
        return line.split()[1]

    def _ledger_view(self, root, *rows_):
        """Render `--ledger` over the given rows. Hermetic: HOME and
        CLAUDE_TOOLKIT_CONFIG both neutralised, per the rule this file already
        documents two tests above."""
        ledger = os.path.join(root, "ledger.jsonl")
        with open(ledger, "w") as handle:
            for row in rows_:
                handle.write(json.dumps(row) + "\n")
        config = os.path.join(root, "config.json")
        with open(config, "w") as handle:
            json.dump({"build_cost": {"ledger_path": ledger}}, handle)
        return subprocess.run(
            (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
            timeout=60, env=dict(os.environ, HOME=root,
                                 CLAUDE_TOOLKIT_CONFIG=config))

    def test_the_kind_column_separates_a_build_from_a_gate_only_session(self):
        """Review share is bimodal — high inside a gated build, much lower
        across all executor-bearing sessions — so a single share over a
        mixed population describes neither. The column is what lets a reader
        split them instead of averaging, which is the mistake that let one
        figure stand as the typical build for weeks.
        """
        with tempfile.TemporaryDirectory() as root:
            # Labels deliberately avoid the words "build" and "gate". The first
            # cut used "a real build" / "gate only" and MUTATION CAUGHT IT:
            # deleting the kind cell from the rows left both assertions green,
            # because they were matching the label text. A fixture whose label
            # contains the value under test makes the assertion vacuous.
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-02T00:00:00", "label": "ran-an-executor",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 "agent_types": ["executor", "workflow-subagent"],
                 "qualified_by": ["executor", "workflow-subagent"]},
                {"timestamp": "2026-02-03T00:00:00", "label": "lenses-only",
                 "total_output": 2000, "total_opus_equivalent_output": 1800,
                 "agent_types": ["workflow-subagent"],
                 "qualified_by": ["workflow-subagent"]})
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = {}
            for line in result.stdout.splitlines():
                if "ran-an-executor" in line:
                    lines["build"] = line
                elif "lenses-only" in line:
                    lines["gate"] = line
            self.assertIn("build", lines, "the executor row did not render")
            self.assertIn("gate", lines, "the lens-only row did not render")
            # Asserted per row, not file-wide: both words appear in the legend,
            # so `assertIn("gate", stdout)` passes with the column deleted.
            self.assertIn(
                "build", lines["build"],
                "a session that ran an executor must render as `build`")
            self.assertIn(
                "gate", lines["gate"],
                "a session whose only qualifying agent was a review lens must "
                "render as `gate` — otherwise it averages in as a build")
            self.assertNotIn(
                "build", lines["gate"],
                "the lens-only row rendered as a build")

    def test_a_row_predating_agent_types_is_not_guessed_at(self):
        """**The honest-degradation property, and the reason it matters.**

        A row without `agent_types` predates the field, and it is genuinely
        unclassifiable: the old qualifying set was executor-OR-task-reviewer,
        and `task-reviewer` is a REVIEW-lane type, so an old row could have
        qualified on review work alone. Inferring `build` from a row's mere
        existence would put a confident wrong label on exactly the rows a cost
        argument reaches for — the failure this repo has on record as "a derived
        number dressed as a reading."
        """
        with tempfile.TemporaryDirectory() as root:
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-02T00:00:00", "label": "old row",
                 "total_output": 1000, "total_opus_equivalent_output": 900})
            self.assertEqual(result.returncode, 0, result.stderr)
            row_line = next(ln for ln in result.stdout.splitlines()
                            if "old row" in ln)
            self.assertIn(
                "—", row_line,
                "a row with no `agent_types` must render as `—`, not be "
                "classified. Every row that predates the field lacks "
                "the field")
            for guess in ("build", "gate"):
                self.assertNotIn(
                    guess, row_line,
                    "a pre-field row was labelled %r. It cannot be known, and "
                    "guessing mislabels the oldest and most-cited rows" % guess)

    def test_a_malformed_agent_types_degrades_rather_than_raising(self):
        """The ledger is append-only and long-lived, so it will eventually hold
        something unexpected — every other reader in this file degrades per row
        for the same reason. A string where a list belongs is the shape a
        hand-edit produces.
        """
        with tempfile.TemporaryDirectory() as root:
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-02T00:00:00", "label": "junk field",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 "agent_types": "executor"},
                {"timestamp": "2026-02-03T00:00:00", "label": "empty field",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 "agent_types": []})
            self.assertEqual(
                result.returncode, 0,
                "a malformed `agent_types` crashed the ledger view: %s"
                % result.stderr)
            # Asserted on the kind cell by position, never on the line. The
            # earlier form matched `—` from the LANE cells and could not see
            # this column — a reviewer's mutation survived the whole suite
            # through that gap.
            junk = next(ln for ln in result.stdout.splitlines() if "junk field" in ln)
            self.assertEqual(
                "—", self._kind_of(junk),
                "a non-list `agent_types` must render `—` in the KIND cell")
            empty = next(ln for ln in result.stdout.splitlines() if "empty field" in ln)
            self.assertEqual(
                "none", self._kind_of(empty),
                "an EMPTY `agent_types` is not the same as an absent one — we "
                "looked and found no subagents, so it says `none`. Collapsing "
                "it into `—` throws away a fact we hold")

    def test_the_bimodal_caveat_appears_when_a_non_build_row_is_present(self):
        """The footer share averages whatever is in the file. When the file is
        mixed, saying so is the difference between a number and a misleading
        one — and it is exactly the correction this change exists to make.
        """
        with tempfile.TemporaryDirectory() as root:
            mixed = self._ledger_view(
                root,
                {"timestamp": "2026-02-02T00:00:00", "label": "lenses-only",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 "review_opus_equivalent_output": 900,
                 "agent_types": ["workflow-subagent"]})
            self.assertIn(
                "bimodal", mixed.stdout,
                "a ledger containing a gate-only row must warn that the "
                "headline share averages two different populations")
        with tempfile.TemporaryDirectory() as root:
            pure = self._ledger_view(
                root,
                {"timestamp": "2026-02-02T00:00:00", "label": "ran-an-executor",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 # Carries review machinery deliberately. This fixture calls
                 # itself an all-BUILD ledger, and since 2026-08-31 a bare
                 # ["executor"] is `adhoc` — so without this the control was
                 # asserting about a ledger of a different kind than it claimed,
                 # and failed for a reason that had nothing to do with caveats.
                 "agent_types": ["executor", "workflow-subagent"]})
            # The positive control. Without it, printing the caveat
            # unconditionally would satisfy the assertion above — and an
            # always-on caveat is one nobody reads.
            self.assertNotIn(
                "bimodal", pure.stdout,
                "the caveat fired on an all-build ledger, where there is no "
                "mixed population to warn about")

    def test_the_kind_column_is_wide_enough_for_its_widest_value(self):
        """`%*s` is a MINIMUM width, so an undersized `_KIND_W` does not truncate
        — it misaligns every row, which reads as a rendering quirk rather than a
        bug. A blinded review found `_KIND_W = 4` was the one mutation of eleven
        the suite did not kill.

        Derived from the CLI's own values rather than asserting the literal 5, so
        adding a longer kind fails here instead of quietly shipping a ragged
        table."""
        widest = max(len(k) for k in ("build", "adhoc", "gate", "none"))
        self.assertGreaterEqual(
            cli_module_kind_width(), widest,
            "the kind column is narrower than its widest value, so rows will "
            "misalign rather than truncate — easy to read past")

    def test_executor_work_with_no_review_machinery_is_not_called_a_build(self):
        """The column added 2026-08-31, and the session that forced it.

        One row spent a very large number of executor tokens: many executors named
        "Task 1...Task 6" plus one blinded `code-reviewer`, no `task-reviewer`
        and no workflow agents. Plan-driven executor use, not a `/build`.
        Averaged in with real `/build` sessions it drags the review share of the
        population down and nothing in the row says so.

        The fixture reproduces exactly that agent mix. Note the label avoids
        every word under test — an earlier test in this class was caught by
        mutation for matching its own fixture's label.
        """
        with tempfile.TemporaryDirectory() as root:
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-04T00:00:00", "label": "nine-workers",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 "agent_types": ["executor", "code-reviewer"],
                 "qualified_by": ["executor", "code-reviewer"]})
            self.assertEqual(result.returncode, 0, result.stderr)
            line = next(ln for ln in result.stdout.splitlines()
                        if "nine-workers" in ln)
            self.assertEqual(
                "adhoc", self._kind_of(line),
                "executor work with no task-reviewer and no workflow agent was "
                "labelled a build, which is the dilution this column exists to "
                "expose")

    def test_a_blinded_code_reviewer_alone_does_not_promote_it_to_a_build(self):
        """The control for the entry above, and the reason `code-reviewer` is
        excluded from REVIEW_MACHINERY. Any session can dispatch a blinded
        pre-PR review by hand — the motivating row did exactly that. Counting it
        would label that very session a `/build` and leave the dilution
        invisible with the column looking like it worked."""
        self.assertNotIn("code-reviewer", cli_module_review_machinery(),
                         "code-reviewer counts as /build machinery, so the "
                         "session that motivated this column reads as a build")

    def test_either_half_of_the_review_machinery_is_enough(self):
        """`task-reviewer` OR `workflow-subagent` — not both. A /build that
        declined its gates still dispatches per-task reviewers, and one whose
        reviewers were targeted rather than universal still runs its gates
        through a workflow. Requiring both would label each of those `adhoc`."""
        with tempfile.TemporaryDirectory() as root:
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-05T00:00:00", "label": "per-task-only",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 "agent_types": ["executor", "task-reviewer"],
                 "qualified_by": ["executor", "task-reviewer"]},
                {"timestamp": "2026-02-06T00:00:00", "label": "gates-only",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 "agent_types": ["executor", "workflow-subagent"],
                 "qualified_by": ["executor", "workflow-subagent"]})
            self.assertEqual(result.returncode, 0, result.stderr)
            for label in ("per-task-only", "gates-only"):
                line = next(ln for ln in result.stdout.splitlines()
                            if label in ln)
                self.assertEqual("build", self._kind_of(line),
                                 "%s was not classified as a build" % label)

    def test_the_legend_says_adhoc_is_a_shape_not_a_record(self):
        """A label a reader trusts as "this was not a /build" would be wrong on
        a /build whose gates were declined — a documented, supported choice. The
        caveat is the difference between a useful column and a confident wrong
        one, and prose caveats are exactly what drifts out of a file."""
        with tempfile.TemporaryDirectory() as root:
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-07T00:00:00", "label": "worker-only",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 "agent_types": ["executor"], "qualified_by": ["executor"]})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("shape", result.stdout)
            self.assertIn("declined", result.stdout)

    def test_kind_ignores_qualified_by_and_reads_what_actually_ran(self):
        """Regression guard for a review finding.

        `qualified_by` is the intersection with whatever
        `qualifying_agent_types` was configured to at record time. Classifying
        off it meant anyone who dropped `executor` from that list got every
        build labelled `gate` — a wrong label, no symptom, on exactly
        the rows a cost argument reaches for.

        The fixture makes the disagreement explicit: an executor ran, and
        `qualified_by` does not mention it.
        """
        with tempfile.TemporaryDirectory() as root:
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-02T00:00:00", "label": "narrow-config",
                 "total_output": 1000, "total_opus_equivalent_output": 900,
                 "agent_types": ["executor", "workflow-subagent"],
                 "qualified_by": ["workflow-subagent"]})
            self.assertEqual(result.returncode, 0, result.stderr)
            line = next(ln for ln in result.stdout.splitlines()
                        if "narrow-config" in ln)
            self.assertIn(
                "build", line,
                "a session where an executor ran must read `build` even when "
                "this machine's qualifying_agent_types did not list executor")
            self.assertNotIn("gate", line)

    def test_a_hand_recorded_research_session_is_not_labelled_gate(self):
        """The BLOCKING finding from the blinded merge review.

        There are TWO ledger writers and only one of them gates. The
        `SessionEnd` hook refuses a session with no qualifying agent;
        `claude-build-cost <transcript>` records whatever it is pointed at, by
        design. So a research-only session reaches the ledger through the manual
        writer alone — and calling it `gate` printed a review label on a row
        whose review lane is 0, directly beneath a legend asserting "`gate` was
        review only", dragging the per-kind review share down for exactly the
        population the column exists to isolate.

        The row's own empty `qualified_by` already carried the distinction. The
        fix consults it rather than guessing.
        """
        with tempfile.TemporaryDirectory() as root:
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-02T00:00:00", "label": "research-only",
                 "total_output": 200, "total_opus_equivalent_output": 160,
                 "agent_types": ["Explore", "general-purpose"],
                 "qualified_by": []})
            self.assertEqual(result.returncode, 0, result.stderr)
            line = next(ln for ln in result.stdout.splitlines()
                        if "research-only" in ln)
            self.assertEqual(
                "none", self._kind_of(line),
                "a session that ran only research agents must not read `gate` "
                "— it ran no review agent and its review lane is 0")

    def test_a_review_only_session_still_reads_gate(self):
        """Positive control for the finding above. Distinguishing `none` from
        `gate` is only worth anything if `gate` still fires — collapsing both
        into `none` would satisfy the previous test and destroy the column."""
        with tempfile.TemporaryDirectory() as root:
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-02T00:00:00", "label": "lenses-ran",
                 "total_output": 200, "total_opus_equivalent_output": 160,
                 "agent_types": ["workflow-subagent"],
                 "qualified_by": ["workflow-subagent"]})
            line = next(ln for ln in result.stdout.splitlines()
                        if "lenses-ran" in ln)
            self.assertEqual("gate", self._kind_of(line))

    def test_a_half_record_is_not_guessed_at(self):
        """`agent_types` present, `qualified_by` absent — reachable from a row
        written by an older build of either writer. `gate` would be a guess in
        the direction that pollutes the population, so it reads `—`."""
        with tempfile.TemporaryDirectory() as root:
            result = self._ledger_view(
                root,
                {"timestamp": "2026-02-02T00:00:00", "label": "half-record",
                 "total_output": 200, "total_opus_equivalent_output": 160,
                 "agent_types": ["workflow-subagent"]})
            line = next(ln for ln in result.stdout.splitlines()
                        if "half-record" in ln)
            self.assertEqual("—", self._kind_of(line))

    def test_agent_types_skips_a_non_string_or_empty_agent_type(self):
        """The guard a reviewer's mutation found untested.

        `cost_lib.agent_types` filters on `isinstance(..., str)` and
        non-emptiness. Deleting that guard left both suites green, so the
        classification could have been fed `None` or `""` as an agentType — and
        `"executor" in names` against a set holding `None` is a silent wrong
        answer rather than a crash.
        """
        with tempfile.TemporaryDirectory() as root:
            sid = "ffffffff-1111-2222-3333-444444444444"
            tp = os.path.join(root, sid + ".jsonl")
            with open(tp, "w") as handle:
                handle.write(json.dumps({
                    "message": {"model": "claude-opus-5",
                                "usage": {"output_tokens": 10}}}) + "\n")
            sub = os.path.join(root, sid, "subagents")
            os.makedirs(sub)
            for index, meta in enumerate((
                    {"agentType": "executor"},      # the one real type
                    {"agentType": None},            # non-string
                    {"agentType": ""},              # empty
                    {"agentType": 7},               # non-string, non-None
                    {"spawnDepth": 1},              # key absent entirely
            )):
                base = os.path.join(sub, "agent-%03d" % index)
                with open(base + ".jsonl", "w") as handle:
                    handle.write(json.dumps({
                        "message": {"model": "claude-sonnet-5",
                                    "usage": {"output_tokens": 5}}}) + "\n")
                with open(base + ".meta.json", "w") as handle:
                    json.dump(meta, handle)
            self.assertEqual(
                ["executor"], cost_lib.agent_types(tp),
                "agent_types must drop a non-string or empty agentType. Passing "
                "None or '' through reaches the `executor in names` test as a "
                "silent wrong answer rather than a crash")

    def test_the_manual_writer_stamps_what_the_hook_stamps(self):
        """The defect review found, and why it mattered more than a blank cell.

        `--ledger` collapses per `session_id` keeping the largest
        `total_output`, so a manual row missing these fields could SUPERSEDE the
        hook's correctly-labelled row and erase a real `build` label — printing
        `—` and "predates the field" on a session recorded that same day.

        No mutation caught this: there was no line to mutate. The CLI simply
        never wrote the fields, which is the class mutation testing is blind to
        — it grades the tests you wrote, not the ones you owed.
        """
        with tempfile.TemporaryDirectory() as root:
            path = transcript([
                entry("claude-opus-5", {"output_tokens": 100}),
                entry("claude-sonnet-5", {"output_tokens": 50}, sidechain=True),
            ])
            session_dir = os.path.join(
                os.path.dirname(path),
                os.path.splitext(os.path.basename(path))[0])
            subagents = os.path.join(session_dir, "subagents")
            os.makedirs(subagents, exist_ok=True)
            base = os.path.join(subagents, "agent-000")
            with open(base + ".jsonl", "w") as handle:
                handle.write(json.dumps({
                    "message": {"model": "claude-sonnet-5",
                                "usage": {"output_tokens": 50}}}) + "\n")
            with open(base + ".meta.json", "w") as handle:
                json.dump({"agentType": "executor"}, handle)
            ledger = os.path.join(root, "ledger.jsonl")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            try:
                result = subprocess.run(
                    (sys.executable, CLI, path, "--label", "manual"),
                    capture_output=True, text=True, timeout=60,
                    env=dict(os.environ, HOME=root,
                             CLAUDE_TOOLKIT_CONFIG=config))
            finally:
                os.unlink(path)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(ledger) as handle:
                row = json.loads(handle.readline())
            self.assertIn(
                "executor", row.get("agent_types", []),
                "the manual writer must stamp `agent_types`, or its row renders "
                "`—` and can supersede the hook's correctly-labelled one")
            self.assertIn(
                "executor", row.get("qualified_by", []),
                "the manual writer must stamp `qualified_by` too — one writer "
                "recording a field the other omits is how two writers come to "
                "disagree about what a row means")

    def test_a_non_dict_ledger_row_does_not_break_the_view(self):
        """Reproduction from the finding: a line reading `null` parses fine
        and is not a dict, so `.get` on it raises AttributeError."""
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write("null\n")
                handle.write(json.dumps({
                    "timestamp": "2026-03-01T00:00:00", "label": "good row",
                    "total_output": 500,
                    "main_opus_equivalent_output": 500}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("good row", result.stdout)

    def test_a_non_numeric_field_does_not_break_the_view(self):
        """A row can be a dict and still carry a string where a number
        belongs. `sum()`/format() over that raises TypeError, which neither
        the ValueError nor the isinstance guard catches — Step 1b's target."""
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-02T00:00:00", "label": "bad field",
                    "total_output": "not-a-number"}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("bad field", result.stdout)

    def test_two_rows_for_one_session_collapse_to_the_larger(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-03T00:00:00", "label": "first firing",
                    "session_id": "sid-resumed", "total_output": 1000,
                    "main_opus_equivalent_output": 1000}) + "\n")
                handle.write(json.dumps({
                    "timestamp": "2026-03-03T09:00:00", "label": "second firing",
                    "session_id": "sid-resumed", "total_output": 9000,
                    "main_opus_equivalent_output": 9000}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("second firing", result.stdout)
            self.assertNotIn("first firing", result.stdout)
            self.assertIn("9,000", result.stdout)
            self.assertIn("1 sessions", result.stdout)

    def test_a_manual_row_collapses_against_the_hook_row_for_one_session(self):
        """The BLOCKING finding's reproduction. The hook stamps `session_id`;
        before the fix the manual CLI stamped none, so a manual run for a
        session the hook already recorded rendered as a second, cheaper
        session and roughly doubled the footer's total. The hook-shaped row
        (500) is hand-written straight to the ledger, as a real hook would;
        the manual row (9,000, from the transcript's own tokens) is written
        by actually running the CLI without --no-record, so this exercises
        the real write path Step 1 fixes. Different totals so the test cannot
        pass by collapsing to the wrong row."""
        with tempfile.TemporaryDirectory() as root:
            sid = "shared-session-12345"
            transcript_path = os.path.join(root, sid + ".jsonl")
            with open(transcript_path, "w") as handle:
                handle.write(json.dumps(
                    entry("claude-opus-5", {"output_tokens": 9000})) + "\n")

            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-07T00:00:00", "label": "hook firing",
                    "session_id": sid, "total_output": 500,
                    "main_opus_equivalent_output": 500}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            env = dict(os.environ, HOME=root, CLAUDE_TOOLKIT_CONFIG=config)

            record_result = subprocess.run(
                (sys.executable, CLI, transcript_path, "--label", "manual run"),
                capture_output=True, text=True, timeout=60, env=env)
            self.assertEqual(0, record_result.returncode, record_result.stderr)

            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=env)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("1 sessions", result.stdout,
                          "one session recorded twice must render as one row")
            self.assertIn("manual run", result.stdout)
            self.assertNotIn("hook firing", result.stdout,
                             "the smaller row must not survive the collapse")
            self.assertIn("9,000", result.stdout,
                          "the larger total_output must be what's rendered")

    def test_rows_without_a_session_id_are_not_collapsed(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-04T00:00:00", "label": "manual one",
                    "total_output": 100, "main_opus_equivalent_output": 100}) + "\n")
                handle.write(json.dumps({
                    "timestamp": "2026-03-04T01:00:00", "label": "manual two",
                    "total_output": 200, "main_opus_equivalent_output": 200}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("manual one", result.stdout)
            self.assertIn("manual two", result.stdout)
            self.assertIn("2 sessions", result.stdout)

    def test_unpriced_and_unweighted_are_marked_on_different_columns(self):
        """Two different gaps, and one marker cannot stand for both.

        `unpriced` means no DOLLAR price. A model can be weighted without one
        (a USAGE_WEIGHT override, or an older row written while a model had a
        stated weight and no published price), so an unpriced row's
        opus-equivalent figure can be complete while only its usd-proxy is
        short. What shortens opus-eq is `unweighted` — a model with no weight
        has its output excluded from the total entirely. On real sessions the
        two sets genuinely differ.

        So this row is unpriced, NOT unweighted: an older row of exactly that
        shape, which the reader must still render. The usd column must
        be marked and the opus-eq column must NOT be — asserting only that some
        marker appears somewhere would pass on the conflated version this
        replaced.
        """
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-05T00:00:00", "label": "fable build",
                    "total_output": 1000, "total_opus_equivalent_output": 2000,
                    "opus_equivalent_is_floor": True, "total_cost": 3.0,
                    "unpriced_models": ["claude-fable-5"]}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("$3.00*", result.stdout,
                          "an unpriced row's usd-proxy must carry the * marker")
            self.assertIn("≥2,000", result.stdout,
                          "a floored opus-eq must still carry its >= marker")
            self.assertNotIn("2,000!", result.stdout,
                             "Fable is unpriced but WEIGHTED, so opus-eq is "
                             "complete and must not be marked understated")
            self.assertIn("usd-proxy understated", result.stdout)
            self.assertNotIn("opus-eq understated", result.stdout)
            self.assertIn("claude-fable-5", result.stdout)

    def test_an_unweighted_row_marks_the_opus_eq_column(self):
        """The mirror case: a model with no weight, so opus-eq really is short.

        Without this, the marker split could be implemented by simply deleting
        the opus-eq marker and nothing would fail.
        """
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-06T00:00:00", "label": "future model",
                    "total_output": 1000, "total_opus_equivalent_output": 400,
                    "total_cost": 1.0,
                    "unweighted_models": ["claude-nextgen-9"]}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("400!", result.stdout,
                          "an unweighted row's opus-eq must carry the ! marker")
            self.assertIn("opus-eq understated", result.stdout)
            self.assertNotIn("usd-proxy understated", result.stdout)
            self.assertIn("claude-nextgen-9", result.stdout)

    def test_ledger_marks_the_unweighted_lane_and_not_the_other(self):
        """The same gap as the floor marker, on a third key — found by CI review.

        `summarize` writes `<lane>_unweighted` into every row, and `show_ledger`
        read it nowhere: `!` appeared only on the `opus-eq` TOTAL column, from
        the session-wide `unweighted_models`. So the ledger could say a model
        was unweighted without saying which lane was short — which is precisely
        why the per-lane floor keys exist, and precisely the shape of
        `unknown_agent_types` being recorded in every row and read by nothing.

        This row deliberately omits `unweighted_models`, so a marker read from
        the session-wide key renders nothing and the positive assertion fails.
        The two lane values are non-substring so neither assertion can be
        satisfied by the other lane's cell.
        """
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-12T00:00:00", "label": "unweighted mix",
                    "total_output": 8000,
                    "executor_opus_equivalent_output": 431,
                    "executor_unweighted": 3,
                    "review_opus_equivalent_output": 962,
                    "review_unweighted": 0}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("431!", result.stdout,
                          "the executor lane spent on an unweighted model, so its "
                          "cell must say so — the total column cannot name a lane")
            self.assertNotIn("962!", result.stdout,
                             "the review lane has no unweighted spend and must "
                             "not be marked; marking every lane is the failure "
                             "a positive-only assertion would miss")

    def test_ledger_marks_the_floored_lane_and_not_the_other(self):
        """Mirror of `test_a_floored_lane_is_marked_and_an_unfloored_one_is_not`
        (the report's version), for the ledger's per-lane cells. No fixture in
        either suite carried a `<lane>_opus_equivalent_is_floor` key, so the
        ledger's per-lane `≥` marker was entirely untested — reading the
        SESSION-WIDE `opus_equivalent_is_floor` flag instead of the per-lane
        one would pass every other ledger test, since none of them sets that
        key either. This row deliberately omits the session-wide flag, so a
        marker read from it (rather than from `review_opus_equivalent_is_floor`)
        renders nothing at all — the negative assertion below would then be
        vacuously true and the positive one would fail."""
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-11T00:00:00", "label": "floor mix",
                    "total_output": 8000,
                    "review_opus_equivalent_output": 500,
                    "review_opus_equivalent_is_floor": True,
                    "executor_opus_equivalent_output": 700}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("≥500", result.stdout,
                          "the floored review lane must carry the marker")
            self.assertNotIn("≥700", result.stdout,
                             "the unfloored executor lane must not carry it")

    def test_a_float_token_value_renders_rather_than_crashing(self):
        """Defect #6's exact failure mode surviving inside the helper written
        to close it. `_number` admits float on purpose (`total_cost` is one),
        so a row carrying `total_output: 1234.0` reached the `",d"` format
        and crashed the whole view with a ValueError, exit 1, mid-table."""
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-08T00:00:00", "label": "float row",
                    "total_output": 1234.0}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("1,234", result.stdout)

    def test_a_non_string_in_a_model_list_does_not_break_the_legend(self):
        """The legend join raises TypeError on a non-string list item AFTER
        the table has already printed — the `isinstance(..., list)` guards
        above only check the field is a list at all, not what is inside it."""
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-09T00:00:00", "label": "mixed list",
                    "total_output": 100, "total_cost": 1.0,
                    "unpriced_models": [1, "claude-fable-5"]}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("claude-fable-5", result.stdout)

    def test_an_unknown_agent_type_row_is_marked(self):
        """cost_lib.summarize writes `unknown_agent_types` into every row and
        the ledger never read it — a row that KNOWS its executor and review
        lanes are understated rendered with no marker and no legend. The
        marker must be distinct from `!` (unweighted) and `*` (unpriced),
        the same lesson that split those two apart."""
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-10T00:00:00", "label": "unknown type",
                    "total_output": 100, "executor_opus_equivalent_output": 100,
                    "unknown_agent_types": ["brand-new-agent-kind"]}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(0, result.returncode, result.stderr)
            # `_lane_row` (used elsewhere in this file) matches print_report's
            # per-lane table rows, which each start with the lane name — the
            # ledger's rows are one per session and start with a timestamp, so
            # the marker is checked the same way the other ledger marker tests
            # above check theirs: as a substring beside the value it modifies.
            self.assertIn("100†", result.stdout,
                          "the executor cell must carry the new marker")
            self.assertNotIn("100!", result.stdout,
                             "must not reuse the unweighted marker")
            self.assertNotIn("100*", result.stdout,
                             "must not reuse the unpriced marker")
            self.assertIn("brand-new-agent-kind", result.stdout)
            self.assertIn("executor/review understated", result.stdout)

    def test_an_old_row_without_the_new_keys_still_renders(self):
        """A pre-four-lane row: no session_id, no output tokens, no
        opus-equivalent keys at all — just the original dollar fields. Every
        missing field must render `—`, never a fabricated `0`."""
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-01-01T00:00:00", "label": "ancient row",
                    "total_cost": 1.0, "main_cost": 0.6, "subagent_cost": 0.4}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ancient row", result.stdout)
            self.assertIn("—", result.stdout)

    def test_every_lane_the_row_carries_is_rendered(self):
        """Four DISTINCT non-zero values, so no assertion can be satisfied by
        the wrong column. Without this, a renderer that silently omits the
        `review` column would pass every other test in this list — and the
        whole point of the task is that the review lane is currently
        invisible.

        The original fixture used `total_output: 1111` while checking the
        main lane for `"111"` — `1,111` (the comma-formatted rendering of
        1111) CONTAINS `"111"`, so deleting the main column entirely still
        passed. Chosen instead: 517 / 829 / 643 / 756 for
        main/executor/review/other, and 9284 for total_output. Checked by
        script (formatting each with its own `,d` thousands separator,
        exactly as the CLI renders them) that none of these five strings is
        a substring of any other, in either direction, and that none is a
        substring of the row's timestamp digits either. This row carries no
        `total_opus_equivalent_output` or `total_cost`, so those two columns
        render as `—` and cannot collide with anything.
        """
        with tempfile.TemporaryDirectory() as root:
            ledger = os.path.join(root, "ledger.jsonl")
            with open(ledger, "w") as handle:
                handle.write(json.dumps({
                    "timestamp": "2026-03-06T00:00:00", "label": "four lanes",
                    "total_output": 9284,
                    "main_opus_equivalent_output": 517,
                    "executor_opus_equivalent_output": 829,
                    "review_opus_equivalent_output": 643,
                    "other_opus_equivalent_output": 756}) + "\n")
            config = os.path.join(root, "config.json")
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger}}, handle)
            result = subprocess.run(
                (sys.executable, CLI, "--ledger"), capture_output=True, text=True,
                timeout=60, env=dict(os.environ, HOME=root,
                                     CLAUDE_TOOLKIT_CONFIG=config))
            self.assertEqual(result.returncode, 0, result.stderr)
            for value in (517, 829, 643, 756):
                self.assertIn(format(value, ",d"), result.stdout,
                             "lane value %d missing from the ledger view" % value)

    def test_the_lib_path_the_cli_inserts_actually_exists(self):
        """Catches the dirname-depth bug at its cause, which no behavioural test can.

        test_configured_ledger_path_is_honoured above covers the CONSEQUENCE —
        _config being None — but not this, because with the wrong depth the
        import still succeeds: cost_lib imports watch_lib, which has already put
        the real lib/ on sys.path and registered toolkit_config in sys.modules,
        so the bad path is simply never needed. Verified by mutation: reverting
        to two dirname calls leaves every behavioural test green.

        So assert the structural fact instead. Three dirname calls from
        <repo>/tools/build-cost/claude-build-cost reach <repo>; two reach
        <repo>/tools and append a tools/lib that has never existed.
        """
        with open(CLI) as handle:
            source = handle.read()
        expected = ("os.path.dirname(os.path.dirname(os.path.dirname("
                    "os.path.realpath(__file__))))")
        self.assertIn(
            expected, source,
            "the lib/ path insertion must use three dirname calls, like every "
            "other tool here; fewer resolves to a directory that does not exist "
            "and only imports by accident")

        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.realpath(CLI))))
        self.assertTrue(
            os.path.isdir(os.path.join(repo_root, "lib")),
            "three dirname calls from the CLI must land on a real lib/")
        self.assertFalse(
            os.path.isdir(os.path.join(repo_root, "tools", "lib")),
            "a tools/lib now exists, so this test's premise is stale — rewrite it")

    def test_missing_transcript_exits_2(self):
        result = self._run("/nope/does-not-exist.jsonl", "--no-record")
        self.assertEqual(result.returncode, 2)

    def test_json_output_parses(self):
        path = transcript([entry("claude-opus-5", {"input_tokens": 1000})])
        try:
            result = self._run(path, "--json", "--no-record")
        finally:
            os.unlink(path)
        self.assertEqual(result.returncode, 0, result.stderr)
        json.loads(result.stdout)


class TestSharedReaderContractUnchanged(unittest.TestCase):
    """The handover watcher must still ignore sidechains.

    cost_lib needed the opposite behaviour from the same reader. Adding a
    parameter is only safe if the default did not move — if it did, the watcher
    starts counting subagent context as this thread's and arms early.
    """

    def test_watcher_default_still_skips_sidechains(self):
        sys.path.insert(0, os.path.join(os.path.dirname(TOOL_DIR), "thread-handover"))
        import watch_lib
        path = transcript([
            entry("claude-opus-5", {"input_tokens": 10}),
            entry("claude-sonnet-5", {"input_tokens": 999}, sidechain=True),
        ])
        try:
            default = list(watch_lib.iter_entries(path))
            everything = list(watch_lib.iter_entries(path, include_sidechains=True))
        finally:
            os.unlink(path)
        self.assertEqual(len(default), 1, "the watcher's default changed")
        self.assertEqual(len(everything), 2)


class TestLaneStats(unittest.TestCase):
    """Per-lane medians from the ledger, for forecasting a dispatch.

    MEDIAN, not mean: records can differ several-fold in total output,
    so a mean is dragged by whichever build was
    largest. `n` travels with every figure because a median of two is not a
    forecast, and the caller states it rather than hiding it.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ledger = os.path.join(self.tmp, "build-costs.jsonl")

    def _write(self, records):
        with open(self.ledger, "w") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

    def test_an_odd_count_takes_the_middle_value(self):
        self._write([{"review_output": 100}, {"review_output": 900},
                     {"review_output": 300}])
        stats = cost_lib.lane_stats(self.ledger)
        self.assertEqual(stats["review"], (300.0, 3))

    def test_an_even_count_averages_the_middle_pair(self):
        self._write([{"review_output": 100}, {"review_output": 300},
                     {"review_output": 500}, {"review_output": 900}])
        self.assertEqual(cost_lib.lane_stats(self.ledger)["review"], (400.0, 4))

    def test_every_lane_is_read_independently(self):
        self._write([{"main_output": 10, "executor_output": 20,
                      "review_output": 30, "other_output": 40}])
        stats = cost_lib.lane_stats(self.ledger)
        self.assertEqual(stats["main"], (10.0, 1))
        self.assertEqual(stats["executor"], (20.0, 1))
        self.assertEqual(stats["review"], (30.0, 1))
        self.assertEqual(stats["other"], (40.0, 1))

    def test_a_lane_with_no_usable_records_is_absent_not_zero(self):
        """A zero would drag the median toward "free", which is the direction
        that makes a dispatch look affordable. Absent is a state the caller
        renders as "no history"; zero is a lie it renders as a forecast."""
        self._write([{"review_output": 500}])
        stats = cost_lib.lane_stats(self.ledger)
        self.assertNotIn("main", stats)
        self.assertIn("review", stats)

    def test_non_numeric_and_boolean_values_are_skipped(self):
        """`True` is an int in Python. A ledger field that somehow held one
        must not be counted as a one-token build."""
        self._write([{"review_output": "lots"}, {"review_output": True},
                     {"review_output": None}, {"review_output": 700}])
        self.assertEqual(cost_lib.lane_stats(self.ledger)["review"], (700.0, 1))

    def test_a_zero_is_an_unused_lane_and_is_skipped(self):
        """INVERTED from the plan's spec, on evidence. The plan asserted
        `(50.0, 2)` here — a zero counted as a real reading — on the
        assumption that an unused lane shows up as a MISSING key. It never
        does: `summarize` writes all four `<lane>_output` keys on every row.
        See the next test for what that produced on the real ledger."""
        self._write([{"review_output": 0}, {"review_output": 100}])
        self.assertEqual(cost_lib.lane_stats(self.ledger)["review"], (100.0, 1))

    def test_an_all_zero_lane_is_absent_so_nothing_forecasts_it_as_free(self):
        """The defect this inversion closes, reproduced as a fixture.

        `other` is the fallback lane for every unrecognised subagent_type. On
        a real ledger it returned a zero median over several rows that never used the
        lane — which clears MIN_SAMPLES and made the advisory say an `other`
        dispatch "has historically cost about 0 points … That fits: 0 <= 47".
        A confident zero on the default lane for anything new.
        """
        self._write([{"review_output": 500, "other_output": 0}
                     for _ in range(7)])
        stats = cost_lib.lane_stats(self.ledger)
        self.assertNotIn("other", stats,
                         "an unused lane must not present as a measured zero")
        self.assertEqual(stats["review"], (500.0, 7))

    def test_a_malformed_line_costs_one_line_not_the_reading(self):
        with open(self.ledger, "w") as handle:
            handle.write("not json\n")
            handle.write("\n")
            handle.write(json.dumps({"review_output": 400}) + "\n")
        self.assertEqual(cost_lib.lane_stats(self.ledger)["review"], (400.0, 1))

    def test_a_missing_ledger_is_empty_not_an_exception(self):
        self.assertEqual(
            cost_lib.lane_stats(os.path.join(self.tmp, "nope.jsonl")), {})

    def test_a_non_dict_record_is_skipped(self):
        self._write([[1, 2, 3], {"review_output": 200}])
        self.assertEqual(cost_lib.lane_stats(self.ledger)["review"], (200.0, 1))

    def test_a_resumed_session_counts_once_not_twice(self):
        """The hook appends a superseding row when a session grows, so an
        uncollapsed read counts one session twice and reports an `n` larger
        than the number of sessions observed. Found on a real ledger: `n` exceeded the
        number of sessions.

        The two rows carry DIFFERENT review_output so the test cannot pass by
        collapsing to the wrong row, and a third standalone session makes the
        uncollapsed median (300) differ from the collapsed one (500) — with
        only the pair, both medians would be 500 and the test would pass on
        the uncollapsed implementation it exists to catch.
        """
        self._write([
            {"session_id": "sid-a", "total_output": 1000, "review_output": 100},
            {"session_id": "sid-a", "total_output": 9000, "review_output": 500},
            {"session_id": "sid-b", "total_output": 2000, "review_output": 900},
        ])
        self.assertEqual(cost_lib.lane_stats(self.ledger)["review"], (700.0, 2))

    def test_rows_without_a_session_id_are_each_their_own_sample(self):
        """Every row the manual CLI writes carries no session_id. Collapsing
        those together would erase real, distinct observations — the mirror of
        the test above, and the direction that under-counts `n`."""
        self._write([{"review_output": 100}, {"review_output": 300},
                     {"review_output": 500}])
        self.assertEqual(cost_lib.lane_stats(self.ledger)["review"], (300.0, 3))

    def test_the_larger_row_wins_regardless_of_file_order(self):
        """`collapse_ledger_rows` keeps the larger `total_output`, not the last
        line. Asserted with the larger row FIRST so a last-write-wins
        implementation fails."""
        self._write([
            {"session_id": "sid-a", "total_output": 9000, "review_output": 500},
            {"session_id": "sid-a", "total_output": 1000, "review_output": 100},
        ])
        self.assertEqual(cost_lib.lane_stats(self.ledger)["review"], (500.0, 1))

    def test_equal_output_breaks_on_prompt_side_tokens(self):
        """A resumed session that added input and no output must still
        supersede: the older row would otherwise keep a smaller dollar figure.

        Both file orders. Newer-second is what the hook writes, and an
        output-only tie keeps the first row, so it fails that order;
        newer-first fails last-write-wins."""
        older = {"session_id": "sid-a", "total_output": 150, "main_input": 0,
                 "total_cost": 0.0}
        newer = {"session_id": "sid-a", "total_output": 150, "main_input": 1_000_000,
                 "total_cost": 4.0}
        for order in ([older, newer], [newer, older]):
            with self.subTest(first=order[0]["main_input"]):
                rows = cost_lib.collapse_ledger_rows(order)
                self.assertEqual(1, len(rows))
                self.assertEqual(1_000_000, rows[0]["main_input"])

    def test_more_output_still_wins_over_more_input(self):
        rows = cost_lib.collapse_ledger_rows([
            {"session_id": "sid-a", "total_output": 100, "main_cache_read": 9_000_000},
            {"session_id": "sid-a", "total_output": 101, "main_cache_read": 0},
        ])
        self.assertEqual(101, rows[0]["total_output"])



def cli_module_kind_width():
    """`_KIND_W` as the CLI defines it, read from source for the same reason as
    `cli_module_review_machinery` — a test that restates the value under test
    passes when the two disagree."""
    import ast
    for node in ast.parse(open(CLI, encoding="utf-8").read()).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "_KIND_W" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("_KIND_W is gone from the CLI")


def cli_module_review_machinery():
    """`REVIEW_MACHINERY` as the CLI actually defines it.

    Read from the file rather than re-declared here: a test that restates the
    set under test passes when the two disagree, which is the whole failure the
    set exists to prevent.
    """
    import ast
    src = open(CLI, encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "REVIEW_MACHINERY" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("REVIEW_MACHINERY is gone from the CLI")

class TestPluginNamespacedAgents(unittest.TestCase):
    """The agents ship in the `toolkit-build` plugin, so a session records
    `toolkit-build:executor`. It must lane and qualify exactly like the bare
    historical spelling."""

    def _session(self, agent_type):
        return TestLaneClassification._session(self, agent_type)

    def test_namespaced_executor_lands_in_executor_lane(self):
        report = cost_lib.analyze(self._session("toolkit-build:executor"))
        self.assertEqual(1, report["totals"]["executor"]["messages"])
        self.assertEqual(0, report["totals"]["review"]["messages"])
        self.assertEqual([], report["unknown_agent_types"])

    def test_namespaced_code_reviewer_lands_in_review_lane(self):
        report = cost_lib.analyze(self._session("toolkit-build:code-reviewer"))
        self.assertEqual(1, report["totals"]["review"]["messages"])
        self.assertEqual(0, report["totals"]["executor"]["messages"])
        self.assertEqual([], report["unknown_agent_types"])

    def test_a_namespaced_unknown_type_is_still_unknown(self):
        report = cost_lib.analyze(self._session("toolkit-build:mystery"))
        self.assertEqual(["toolkit-build:mystery"], report["unknown_agent_types"])

    def test_bare_agent_type_strips_only_the_plugin_prefix(self):
        self.assertEqual("executor", cost_lib.bare_agent_type("toolkit-build:executor"))
        self.assertEqual("executor", cost_lib.bare_agent_type("executor"))
        self.assertEqual("other:executor", cost_lib.bare_agent_type("other:executor"))

    def test_only_this_toolkit_s_plugin_prefix_is_recognised(self):
        self.assertEqual(("toolkit-build:",), cost_lib.PLUGIN_AGENT_PREFIXES)

    def test_another_plugin_s_executor_is_not_counted_as_ours(self):
        # A same-named agent from someone else's plugin is not this /build's
        # executor; it lands in `other` and is named.
        report = cost_lib.analyze(self._session("someone-else:executor"))
        self.assertEqual(0, report["totals"]["executor"]["messages"])
        self.assertEqual(1, report["totals"]["other"]["messages"])
        self.assertEqual(["someone-else:executor"], report["unknown_agent_types"])

    def test_namespaced_architect_reviewer_lands_in_review_lane(self):
        report = cost_lib.analyze(self._session("toolkit-build:architect-reviewer"))
        self.assertEqual(1, report["totals"]["review"]["messages"])
        self.assertEqual([], report["unknown_agent_types"])

    def test_the_default_qualifying_set_includes_architect_reviewer(self):
        class EmptyConfig:
            def get_list(self, *keys):
                return []
        for config in (None, EmptyConfig()):
            with self.subTest(config=config):
                self.assertIn("architect-reviewer",
                              cost_lib.qualifying_agent_types(config))

    def test_a_session_of_only_a_namespaced_architect_reviewer_qualifies(self):
        wanted = cost_lib.qualifying_agent_types(None)
        self.assertEqual(["toolkit-build:architect-reviewer"], cost_lib.qualifies(
            ["toolkit-build:architect-reviewer"], wanted))

    def test_qualifies_matches_across_spellings_and_keeps_the_observed_one(self):
        self.assertEqual(["toolkit-build:executor"], cost_lib.qualifies(
            ["toolkit-build:executor", "general-purpose"], ["executor"]))
        self.assertEqual(["executor"], cost_lib.qualifies(
            ["executor", "general-purpose"], ["toolkit-build:executor"]))
        self.assertEqual([], cost_lib.qualifies(["general-purpose"], ["executor"]))

    def test_kind_cell_calls_a_namespaced_build_a_build(self):
        import importlib.machinery
        import importlib.util
        path = os.path.join(TOOL_DIR, "claude-build-cost")
        loader = importlib.machinery.SourceFileLoader("claude_build_cost_cli", path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)
        self.assertEqual("build", cli._kind_cell(
            {"agent_types": ["toolkit-build:executor", "toolkit-build:task-reviewer"]}))
        self.assertEqual("adhoc", cli._kind_cell(
            {"agent_types": ["toolkit-build:executor", "toolkit-build:code-reviewer"]}))


class TestWorktreeSplitCLI(unittest.TestCase):
    def test_cli_counts_agents_written_under_a_worktree_project_dir(self):
        root = tempfile.mkdtemp()
        sid = str(uuid.uuid4())
        start_dir = os.path.join(root, "projects", "-start")
        os.makedirs(start_dir)
        transcript_path = os.path.join(start_dir, sid + ".jsonl")
        with open(transcript_path, "w") as handle:
            handle.write(json.dumps({
                "message": {"model": "claude-opus-5", "usage": {"output_tokens": 100}}}) + "\n")
        subagents = os.path.join(root, "projects", "-start-wt", sid, "subagents")
        os.makedirs(subagents)
        base = os.path.join(subagents, "agent-000")
        with open(base + ".jsonl", "w") as handle:
            handle.write(json.dumps({
                "isSidechain": True,
                "message": {"model": "claude-sonnet-5",
                            "usage": {"output_tokens": 777}}}) + "\n")
        with open(base + ".meta.json", "w") as handle:
            json.dump({"agentType": "toolkit-build:executor"}, handle)

        result = subprocess.run(
            [sys.executable, CLI, transcript_path, "--json", "--no-record"],
            capture_output=True, text=True)

        self.assertEqual(0, result.returncode, result.stderr)
        summary = json.loads(result.stdout)["summary"]
        self.assertEqual(777, summary["executor_output"])


REPO = os.path.dirname(os.path.dirname(TOOL_DIR))


class TestShippedDefaults(unittest.TestCase):
    """What ships beside the tool must not stand in for anyone's own numbers."""

    def test_example_config_values_equal_the_code_defaults(self):
        # config.example.json is copied verbatim on a no-TTY install, so any
        # value here that differs from the default becomes a live setting.
        with open(os.path.join(REPO, "config.example.json"), encoding="utf-8") as handle:
            section = json.load(handle)["build_cost"]
        values = {k: v for k, v in section.items() if not k.startswith("//")}
        self.assertEqual({"enabled", "ledger_path", "qualifying_agent_types"}, set(values))
        self.assertIs(True, values["enabled"])
        self.assertEqual(cost_lib.DEFAULT_LEDGER_PATH, values["ledger_path"])
        self.assertEqual(cost_lib.DEFAULT_QUALIFYING_AGENT_TYPES,
                         values["qualifying_agent_types"])

    def test_measurement_record_holds_methods_not_measurements(self):
        # The plan requires whole-file replacement of the work calibration:
        # keep the measurement method, never the measurements.
        with open(os.path.join(REPO, "docs", "build-measurements.md"),
                  encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("This file holds methods, not measurements.", text[:600])
        self.assertIn("re-measure", text[:1500])

    def test_the_specs_dir_resolver_names_its_root(self):
        # A reader stands in the repository being built, not the toolkit, so a
        # checkout-relative `python3 lib/toolkit_config.py` fails there.
        with open(os.path.join(REPO, "docs", "build-measurements.md"),
                  encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("python3 <toolkit checkout>/lib/toolkit_config.py specs-dir", text)
        self.assertNotIn("`python3 lib/toolkit_config.py", text)


if __name__ == "__main__":
    unittest.main(verbosity=1)
