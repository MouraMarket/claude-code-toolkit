#!/usr/bin/env python3
"""Tests for claude-build-cost-record, the SessionEnd hook.

Driven through subprocess, never imported. A hook that passes unit tests and
crashes through the symlink is the documented failure mode this guards against.
"""

import itertools
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
HOOK = os.path.join(TOOL_DIR, "claude-build-cost-record")

_NEW_WORK_SEQ = itertools.count()


def build_session(agent_types, root=None):
    """A session dir with one agent file per entry in `agent_types`.

    Returns (transcript_path, ledger_path, config_path).
    """
    root = root or tempfile.mkdtemp()
    sid = "11111111-2222-3333-4444-555555555555"
    transcript_path = os.path.join(root, sid + ".jsonl")
    with open(transcript_path, "w") as handle:
        handle.write(json.dumps({
            "message": {"model": "claude-opus-5", "usage": {"output_tokens": 100}}}) + "\n")
    subagents = os.path.join(root, sid, "subagents")
    os.makedirs(subagents, exist_ok=True)
    for index, agent_type in enumerate(agent_types):
        base = os.path.join(subagents, "agent-%03d" % index)
        with open(base + ".jsonl", "w") as handle:
            handle.write(json.dumps({
                "isSidechain": True,
                "message": {"model": "claude-sonnet-5",
                            "usage": {"output_tokens": 50}}}) + "\n")
        with open(base + ".meta.json", "w") as handle:
            json.dump({"agentType": agent_type}, handle)
    ledger_path = os.path.join(root, "ledger.jsonl")
    config_path = os.path.join(root, "config.json")
    with open(config_path, "w") as handle:
        json.dump({"build_cost": {"ledger_path": ledger_path}}, handle)
    return transcript_path, ledger_path, config_path


def append_new_work(transcript_path, output_tokens):
    """Append one new usage-bearing entry to a transcript, modeling real growth.

    Used to test a resumed session: the session fires SessionEnd once, keeps
    working, and fires again. A fresh `message.id` is load-bearing — reusing an
    existing one would model repetition, not growth, and `analyze`'s per-message
    grouping would merge it with an earlier entry rather than adding to the
    total. `stored_growth_key` would then correctly refuse to supersede, and
    the test built on this helper would pass while proving nothing.
    """
    with open(transcript_path, "a") as handle:
        handle.write(json.dumps({
            "message": {"id": "msg_new_work_%d" % next(_NEW_WORK_SEQ),
                        "model": "claude-opus-5",
                        "usage": {"output_tokens": output_tokens}}}) + "\n")


def run_hook(payload, config_path):
    return subprocess.run(
        [sys.executable, HOOK], input=json.dumps(payload),
        capture_output=True, text=True,
        env=dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config_path))


def rows(ledger_path):
    """Every parseable line of a ledger.

    Unparseable lines are skipped rather than raised on, matching how the hook
    itself reads the ledger. A helper that dies on a junk line cannot be used to
    test what the hook does with a junk line — which is the whole point of
    test_a_non_dict_ledger_row_does_not_stop_recording.
    """
    if not os.path.exists(ledger_path):
        return []
    parsed = []
    with open(ledger_path) as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                parsed.append(json.loads(line))
            except ValueError:
                continue
    return parsed


REAL_LEDGER = os.path.expanduser("~/.claude/build-costs.jsonl")


def real_ledger_state():
    """(exists, size) of the developer's actual ledger. Never opened, only stat'd."""
    if not os.path.exists(REAL_LEDGER):
        return (False, 0)
    return (True, os.path.getsize(REAL_LEDGER))


REAL_LEDGER_AT_IMPORT = real_ledger_state()


def tearDownModule():
    """The real hermeticity guard for this file, checked once after every test.

    This has to live at module scope, not in a test method. The previous version
    was a test that ran the hook with a config-supplied `ledger_path` and then
    compared the real ledger before and after its own call — but that call can
    never reach the real-home fallback, so the comparison could not fail from the
    hazard its name promised. It reported `ok` while a sibling test wrote a
    synthetic row to the real ledger, which was verified against a stand-in HOME.

    Only a check that spans the whole module catches the actual regression:
    any test losing its `HOME` override reaches `~/.claude/build-costs.jsonl` and
    seeds the developer's real dataset — on every suite run, every install, and
    every machine.
    """
    after = real_ledger_state()
    if after != REAL_LEDGER_AT_IMPORT:
        raise AssertionError(
            "a test in this file wrote to the real ledger at %s (%r -> %r). "
            "Every test that can reach the no-config fallback must override HOME "
            "as well as CLAUDE_TOOLKIT_CONFIG."
            % (REAL_LEDGER, REAL_LEDGER_AT_IMPORT, after))


class TestQualifyingSessions(unittest.TestCase):
    def test_build_session_writes_one_row(self):
        transcript, ledger, config = build_session(["executor", "task-reviewer"])
        result = run_hook({"session_id": "s", "transcript_path": transcript,
                           "cwd": "/tmp", "hook_event_name": "SessionEnd",
                           "reason": "prompt_input_exit"}, config)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, len(rows(ledger)))

    def test_research_only_session_writes_nothing(self):
        transcript, ledger, config = build_session(["general-purpose", "general-purpose"])
        result = run_hook({"session_id": "s", "transcript_path": transcript,
                           "cwd": "/tmp", "hook_event_name": "SessionEnd",
                           "reason": "clear"}, config)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], rows(ledger))

    def test_session_with_no_agents_writes_nothing(self):
        transcript, ledger, config = build_session([])
        run_hook({"session_id": "s", "transcript_path": transcript, "cwd": "/tmp",
                  "hook_event_name": "SessionEnd", "reason": "other"}, config)
        self.assertEqual([], rows(ledger))

    def test_recorded_row_carries_the_four_lanes(self):
        transcript, ledger, config = build_session(["executor"])
        run_hook({"session_id": "s", "transcript_path": transcript, "cwd": "/tmp",
                  "hook_event_name": "SessionEnd", "reason": "other"}, config)
        row = rows(ledger)[0]
        for key in ("main_cost", "executor_cost", "review_cost", "other_cost",
                    "total_output", "timestamp"):
            self.assertIn(key, row)
        self.assertEqual(1, row["executor_messages"])


class TestGateOnlySessionsRecord(unittest.TestCase):
    """A session that ran a review gate and wrote no code must record.

    Added 2026-08-30 for a MEASURED blind spot, not a hypothetical one. With
    only `executor` and `task-reviewer` qualifying, a `/build` gate session
    recorded nothing — so the ledger under-sampled exactly the review-heavy
    sessions every cost argument is about. One real instance: a review-heavy build with two
    Plan Review rounds, absent from the dataset
    entirely, while a single figure was cited as the typical build. (That
    figure and its correction moved to `docs/build-measurements.md` on
    2026-08-31 when `build.md` was split; it was in `build.md` until then.)

    These are WIRING tests through the hook, deliberately. `base-pin` shipped
    a green suite and every mutation killed with a BLOCKING defect in exactly this
    seam: the predicate was right and nothing exercised the path that called
    it, so no mutation of the wiring existed to survive.
    """

    def test_a_workflow_only_session_records(self):
        """`/build`'s Plan Review and Code Review lenses run as
        `workflow-subagent`. This is the case that recorded
        nothing."""
        transcript, ledger, config = build_session(
            ["workflow-subagent", "workflow-subagent"])
        result = run_hook({"session_id": "s", "transcript_path": transcript,
                           "cwd": "/tmp", "hook_event_name": "SessionEnd",
                           "reason": "other"}, config)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(
            1, len(rows(ledger)),
            "a session whose only agents were workflow lenses wrote no ledger "
            "row — the review-heavy sessions stay invisible and the ledger "
            "keeps under-sampling them")

    def test_a_code_reviewer_only_session_records(self):
        """Merge Review and Progress Check dispatch `code-reviewer` directly
        rather than through a workflow, so it is a second, separate route to a
        gate-only session."""
        transcript, ledger, config = build_session(["code-reviewer"])
        run_hook({"session_id": "s", "transcript_path": transcript,
                  "cwd": "/tmp", "hook_event_name": "SessionEnd",
                  "reason": "other"}, config)
        self.assertEqual(1, len(rows(ledger)))

    def test_the_row_names_what_qualified_it(self):
        """`workflow-subagent` is ANY workflow agent, not a `/build` lens
        specifically — nothing in the metadata distinguishes them (a workflow
        agent's `.meta.json` carries agentType, spawnDepth and model, and no
        description). So widening the qualifying set buys dilution, and
        `qualified_by` is what makes the dilution filterable instead of silent.

        Sorted, because a set's iteration order is not stable and an unstable
        field makes two identical sessions differ in a diff.
        """
        transcript, ledger, config = build_session(
            ["workflow-subagent", "general-purpose"])
        run_hook({"session_id": "s", "transcript_path": transcript,
                  "cwd": "/tmp", "hook_event_name": "SessionEnd",
                  "reason": "other"}, config)
        row = rows(ledger)[0]
        self.assertEqual(
            ["workflow-subagent"], row.get("qualified_by"),
            "the row must name which qualifying types were present, and must "
            "NOT include a non-qualifying agent that merely ran alongside them")

    def test_the_row_records_what_actually_ran_as_well(self):
        """`agent_types` is the field `--ledger` classifies `build`/`gate` from,
        and it must be config-independent — see the CLI-side test of the same
        property.

        This test exists because MUTATION FOUND IT MISSING: the hook's
        `agent_types` stamp could be deleted with every suite still green. The
        hook tests asserted `qualified_by` and the CLI tests asserted
        `agent_types`, so each writer had one field covered and the other did
        not. Two writers, two fields, and the coverage was diagonal.
        """
        transcript, ledger, config = build_session(
            ["executor", "workflow-subagent", "general-purpose"])
        run_hook({"session_id": "s", "transcript_path": transcript, "cwd": "/tmp",
                  "hook_event_name": "SessionEnd", "reason": "other"}, config)
        row = rows(ledger)[0]
        self.assertEqual(
            ["executor", "general-purpose", "workflow-subagent"],
            row.get("agent_types"),
            "the hook must stamp every observed agentType — INCLUDING "
            "non-qualifying ones. This is what ran, not why the row exists; "
            "narrowing it to the qualifying set is what `qualified_by` is for")

    def test_qualified_by_records_every_qualifying_type_present(self):
        transcript, ledger, config = build_session(
            ["executor", "workflow-subagent", "task-reviewer"])
        run_hook({"session_id": "s", "transcript_path": transcript,
                  "cwd": "/tmp", "hook_event_name": "SessionEnd",
                  "reason": "other"}, config)
        self.assertEqual(
            ["executor", "task-reviewer", "workflow-subagent"],
            rows(ledger)[0].get("qualified_by"))

    def test_a_non_qualifying_session_still_writes_nothing(self):
        """The positive control for this whole class. Widening the qualifying
        set must not widen it to everything — without this, replacing the
        membership test with `True` would pass every test above.
        """
        transcript, ledger, config = build_session(["general-purpose", "Explore"])
        run_hook({"session_id": "s", "transcript_path": transcript,
                  "cwd": "/tmp", "hook_event_name": "SessionEnd",
                  "reason": "other"}, config)
        self.assertEqual(
            [], rows(ledger),
            "a research-only session recorded a row — the gate that keeps the "
            "dataset meaningful has stopped gating")


class TestGuards(unittest.TestCase):
    def test_subagent_payload_writes_nothing(self):
        transcript, ledger, config = build_session(["executor"])
        result = run_hook({"session_id": "s", "transcript_path": transcript,
                           "cwd": "/tmp", "hook_event_name": "SessionEnd",
                           "reason": "other", "agent_id": "agent-0f1e2d"}, config)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], rows(ledger),
                         "a SessionEnd firing inside a subagent must not record")

    def test_malformed_json_exits_zero_and_writes_nothing(self):
        _transcript, ledger, config = build_session(["executor"])
        result = subprocess.run(
            [sys.executable, HOOK], input="{not json at all",
            capture_output=True, text=True,
            env=dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config))
        self.assertEqual(0, result.returncode)
        self.assertEqual([], rows(ledger))

    def test_empty_stdin_exits_zero(self):
        _transcript, _ledger, config = build_session([])
        result = subprocess.run(
            [sys.executable, HOOK], input="", capture_output=True, text=True,
            env=dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config))
        self.assertEqual(0, result.returncode)

    def test_missing_transcript_exits_zero(self):
        _transcript, ledger, config = build_session(["executor"])
        result = run_hook({"session_id": "s", "transcript_path": "/nonexistent/x.jsonl",
                           "cwd": "/tmp", "hook_event_name": "SessionEnd",
                           "reason": "other"}, config)
        self.assertEqual(0, result.returncode)
        self.assertEqual([], rows(ledger))

    def test_no_config_at_all_still_exits_zero(self):
        # HOME is overridden as well as CLAUDE_TOOLKIT_CONFIG, and that is not
        # optional. With no config, ledger_path() falls back to
        # os.path.expanduser("~/.claude/build-costs.jsonl"). This session
        # QUALIFIES, so the hook writes — against the real home directory if
        # HOME is left inherited, appending a synthetic row to the developer's
        # actual ledger on every run of this suite, on every install, and on
        # every machine. `test_reports_and_records` in
        # test_cost_lib.py carries the same override and the same warning.
        root = tempfile.mkdtemp()
        transcript, _ledger, _config = build_session(["executor"], root=root)
        fake_home = os.path.join(root, "home")
        os.makedirs(fake_home, exist_ok=True)
        result = subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"session_id": "s", "transcript_path": transcript,
                              "cwd": "/tmp", "hook_event_name": "SessionEnd",
                              "reason": "other"}),
            capture_output=True, text=True,
            env=dict(os.environ, HOME=fake_home,
                     CLAUDE_TOOLKIT_CONFIG="/nonexistent.json"))
        self.assertEqual(0, result.returncode, result.stderr)
        # The fallback path must be the sandboxed HOME, proving the override
        # took and that nothing reached the real one.
        self.assertTrue(os.path.exists(
            os.path.join(fake_home, ".claude", "build-costs.jsonl")))
        # This is the one test that reaches the fallback, so it is the one that
        # can pollute the real ledger. Asserted here as well as in
        # tearDownModule so the failure names the responsible test.
        self.assertEqual(REAL_LEDGER_AT_IMPORT, real_ledger_state(),
                         "the HOME override did not take and this test wrote to "
                         "the real ledger at %s" % REAL_LEDGER)

    def test_a_configured_ledger_path_diverts_writes_from_the_real_one(self):
        """A config-supplied ledger_path must be used in preference to the fallback.

        Named for what it actually checks. It cannot serve as the file's
        hermeticity guard — the config override means this call can never reach
        the real-home fallback — so that job belongs to tearDownModule.
        """
        root = tempfile.mkdtemp()
        transcript, ledger, config = build_session(["executor"], root=root)
        run_hook({"session_id": "s", "transcript_path": transcript, "cwd": "/tmp",
                  "hook_event_name": "SessionEnd", "reason": "other"}, config)
        self.assertEqual(1, len(rows(ledger)))
        self.assertEqual(REAL_LEDGER_AT_IMPORT, real_ledger_state())

    def test_a_non_dict_ledger_row_does_not_stop_recording(self):
        """One bad line must not disable the feature for every future session.

        `json.loads` returns None for a line reading `null`, and `None.get` is an
        AttributeError, not a ValueError. Catching only ValueError let that
        propagate out of the ledger reader (`already_recorded` at the time, now
        `stored_growth_key`), out of main, and into the top-level
        wrapper — so the hook exited 0 having written nothing, for this session
        and every session after it, until someone hand-edited the ledger. The
        ledger is append-only and long-lived; a reader of it must degrade per
        line rather than per file.
        """
        transcript, ledger, config = build_session(["executor"])
        with open(ledger, "w") as handle:
            handle.write("null\n")
            handle.write("12345\n")
            handle.write('"a bare string"\n')
            handle.write("{not json at all\n")
            handle.write(json.dumps({"session_id": "an-older-build"}) + "\n")
        result = run_hook({"session_id": "brand-new-session",
                           "transcript_path": transcript, "cwd": "/tmp",
                           "hook_event_name": "SessionEnd", "reason": "other"}, config)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("", result.stderr,
                         "a malformed ledger line must not raise a diagnostic")
        recorded = [r for r in rows(ledger)
                    if isinstance(r, dict) and r.get("session_id") == "brand-new-session"]
        self.assertEqual(1, len(recorded),
                         "the new session must still be recorded past the bad lines")

    def test_a_non_dict_ledger_row_does_not_break_idempotence(self):
        """The bad-line fix must not cost the duplicate guard.

        Skipping unparseable lines is only safe if the parseable ones are still
        compared — otherwise the fix for one silent failure introduces another.

        The stderr assertion is load-bearing and was added after this test was
        caught passing under mutation: a row count of 2 is satisfied both by
        correctly skipping the duplicate AND by the pre-fix hook crashing on the
        `null` line before it could append. Only the absence of a diagnostic
        separates the two.

        The fixture's `total_output` is set larger than the session's own total
        (150, from `build_session`'s 100 main + 50 executor tokens) so that
        `stored_growth_key` reads a real stored figure the new firing cannot
        exceed. Idempotence is now supersede-on-growth rather than
        first-write-wins, so a row with no `total_output` at all would read as
        stored=0 and the hook would append a third row rather than skip.
        """
        transcript, ledger, config = build_session(["executor"])
        with open(ledger, "w") as handle:
            handle.write("null\n")
            handle.write(json.dumps(
                {"session_id": "already-here", "total_output": 999999}) + "\n")
        result = run_hook({"session_id": "already-here", "transcript_path": transcript,
                           "cwd": "/tmp", "hook_event_name": "SessionEnd",
                           "reason": "other"}, config)
        self.assertEqual("", result.stderr,
                         "the duplicate must be skipped deliberately, not by crashing")
        self.assertEqual(2, len(rows(ledger)),
                         "an already-recorded session must still be skipped")


class TestIdempotence(unittest.TestCase):
    def test_a_second_firing_with_no_new_work_writes_nothing(self):
        # SessionEnd fires more than once per session id — repeats happen.
        # Idempotence is now supersede-on-growth rather than
        # first-write-wins, so a repeat firing of an unchanged transcript must
        # still leave exactly one row.
        #
        # Empty stderr is load-bearing, same reason as
        # test_a_non_dict_ledger_row_does_not_break_idempotence: "1 row" is also
        # satisfied by the hook crashing before the second firing's append,
        # which is a defect this branch has already shipped once.
        transcript, ledger, config = build_session(["executor"])
        payload = {"session_id": "same-session", "transcript_path": transcript,
                   "cwd": "/tmp", "hook_event_name": "SessionEnd", "reason": "other"}
        run_hook(payload, config)
        result = run_hook(payload, config)
        result = run_hook(payload, config)
        self.assertEqual("", result.stderr,
                         "a no-growth repeat must be skipped deliberately, not by crashing")
        self.assertEqual(1, len(rows(ledger)),
                         "three firings with no new work must leave one row")

    def test_input_only_growth_supersedes(self):
        # A re-fire whose new calls added prompt tokens and no output must
        # still append: output-only growth kept the first row's smaller input
        # and dollar figures.
        transcript, ledger, config = build_session(["executor"])
        payload = {"session_id": "input-growth", "transcript_path": transcript,
                   "cwd": "/tmp", "hook_event_name": "SessionEnd", "reason": "other"}
        run_hook(payload, config)
        with open(transcript, "a") as handle:
            handle.write(json.dumps({"message": {
                "id": "msg_input_only", "model": "claude-opus-5-5",
                "usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}) + "\n")
        result = run_hook(payload, config)
        self.assertEqual("", result.stderr)
        written = rows(ledger)
        self.assertEqual(2, len(written))
        self.assertEqual(written[0]["total_output"], written[1]["total_output"])
        self.assertEqual(1_000_000, written[1]["main_input"])

    def test_a_resumed_session_supersedes_its_truncated_row(self):
        # The 86.5% loss this fixes: SessionEnd fires, the session keeps
        # working, and SessionEnd fires again. First-write-wins recorded only
        # the first firing's total forever. Supersede-on-growth appends a
        # second row carrying the larger total instead.
        transcript, ledger, config = build_session(["executor"])
        payload = {"session_id": "resumed-session", "transcript_path": transcript,
                   "cwd": "/tmp", "hook_event_name": "SessionEnd", "reason": "other"}
        run_hook(payload, config)
        first_total = rows(ledger)[0]["total_output"]
        append_new_work(transcript, 100000)
        result = run_hook(payload, config)
        self.assertEqual(0, result.returncode, result.stderr)
        all_rows = rows(ledger)
        self.assertEqual(2, len(all_rows), "growth must append a superseding row")
        totals = [row["total_output"] for row in all_rows]
        self.assertEqual(first_total + 100000, max(totals),
                         "the superseding row must carry the larger total")

    def test_a_different_session_still_records(self):
        transcript, ledger, config = build_session(["executor"])
        for sid in ("session-a", "session-b"):
            run_hook({"session_id": sid, "transcript_path": transcript,
                      "cwd": "/tmp", "hook_event_name": "SessionEnd",
                      "reason": "other"}, config)
        self.assertEqual(2, len(rows(ledger)))


class TestEnabledSwitch(unittest.TestCase):
    def test_disabled_writes_nothing(self):
        transcript, ledger, config = build_session(["executor"])
        with open(config, "w") as handle:
            json.dump({"build_cost": {"ledger_path": ledger, "enabled": False}}, handle)
        result = run_hook({"session_id": "s", "transcript_path": transcript,
                           "cwd": "/tmp", "hook_event_name": "SessionEnd",
                           "reason": "other"}, config)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([], rows(ledger))

    def test_absent_key_means_enabled(self):
        transcript, ledger, config = build_session(["executor"])
        run_hook({"session_id": "s", "transcript_path": transcript, "cwd": "/tmp",
                  "hook_event_name": "SessionEnd", "reason": "other"}, config)
        self.assertEqual(1, len(rows(ledger)))


class TestHookLibPathStructure(unittest.TestCase):
    def test_the_lib_path_the_hook_inserts_actually_exists(self):
        """Catches the dirname-depth bug at its cause, which no behavioural test can.

        The hook does `import cost_lib` before `import toolkit_config`, and
        `cost_lib` itself imports `watch_lib`, which has already put the real
        `lib/` on sys.path and registered `toolkit_config` in `sys.modules` — so
        with the wrong depth, `import toolkit_config` still succeeds and `config`
        is never None. `test_no_config_at_all_still_exits_zero` and the other
        behavioural tests in this file cannot see that: mutation-verified,
        reducing the hook's insertion to two `dirname` calls (pointing at a
        `tools/lib` that has never existed) leaves the whole suite green.

        The consequences of that silent `config = None` are total, not partial:
        `ledger_path(None)` ignores `build_cost.ledger_path` and writes to the
        real `~/.claude/build-costs.jsonl`, `qualifying_types(None)` ignores the
        configured set, and the `if config is not None` wrapper around the
        enabled check skips it entirely — making `enabled: false`, which
        `config.example.json` calls "the only supported off switch," inert.

        So assert the structural fact instead. Three dirname calls from
        <repo>/tools/build-cost/claude-build-cost-record reach <repo>; two reach
        <repo>/tools and append a tools/lib that has never existed.
        """
        with open(HOOK) as handle:
            source = handle.read()
        expected = ("os.path.dirname(os.path.dirname(os.path.dirname("
                    "os.path.realpath(__file__))))")
        self.assertIn(
            expected, source,
            "the lib/ path insertion must use three dirname calls, like every "
            "other tool here; fewer resolves to a directory that does not exist "
            "and only imports by accident")

        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.realpath(HOOK))))
        self.assertTrue(
            os.path.isdir(os.path.join(repo_root, "lib")),
            "three dirname calls from the hook must land on a real lib/")
        self.assertFalse(
            os.path.isdir(os.path.join(repo_root, "tools", "lib")),
            "a tools/lib now exists, so this test's premise is stale — rewrite it")


class TestConfigurableQualifiers(unittest.TestCase):
    def test_qualifying_types_can_be_widened(self):
        transcript, ledger, config = build_session(["general-purpose"])
        with open(config, "w") as handle:
            json.dump({"build_cost": {"ledger_path": ledger,
                                      "qualifying_agent_types": ["general-purpose"]}},
                      handle)
        run_hook({"session_id": "s", "transcript_path": transcript, "cwd": "/tmp",
                  "hook_event_name": "SessionEnd", "reason": "other"}, config)
        self.assertEqual(1, len(rows(ledger)))


class TestNamespacedAgentsQualify(unittest.TestCase):
    PAYLOAD = {"session_id": "s", "cwd": "/tmp", "hook_event_name": "SessionEnd",
               "reason": "other"}

    def _record(self, agent_types, wanted=None):
        transcript, ledger, config = build_session(agent_types)
        if wanted is not None:
            with open(config, "w") as handle:
                json.dump({"build_cost": {"ledger_path": ledger,
                                          "qualifying_agent_types": wanted}}, handle)
        run_hook(dict(self.PAYLOAD, transcript_path=transcript), config)
        return rows(ledger)

    def test_namespaced_executor_qualifies_under_the_default_config(self):
        recorded = self._record(["toolkit-build:executor"])
        self.assertEqual(1, len(recorded))
        self.assertEqual(["toolkit-build:executor"], recorded[0]["qualified_by"])
        self.assertEqual(["toolkit-build:executor"], recorded[0]["agent_types"])
        self.assertEqual(1, recorded[0]["executor_messages"])

    def test_a_namespaced_architect_reviewer_alone_qualifies_by_default(self):
        recorded = self._record(["toolkit-build:architect-reviewer"])
        self.assertEqual(1, len(recorded))
        self.assertEqual(["toolkit-build:architect-reviewer"],
                         recorded[0]["qualified_by"])

    def test_a_spec_reviewer_only_session_qualifies_under_the_default_config(self):
        recorded = self._record(["toolkit-build:spec-reviewer"])
        self.assertEqual(1, len(recorded))

    def test_a_general_purpose_only_session_writes_nothing(self):
        self.assertEqual([], self._record(["general-purpose"]))

    def test_namespaced_code_reviewer_lanes_review(self):
        recorded = self._record(["toolkit-build:code-reviewer"])
        self.assertEqual(1, recorded[0]["review_messages"])

    def test_a_bare_config_qualifies_a_namespaced_session(self):
        self.assertEqual(1, len(self._record(["toolkit-build:executor"], ["executor"])))

    def test_a_namespaced_config_qualifies_a_bare_session(self):
        recorded = self._record(["executor"], ["toolkit-build:executor"])
        self.assertEqual(["executor"], recorded[0]["qualified_by"])

    def test_a_non_build_namespaced_session_still_writes_nothing(self):
        self.assertEqual([], self._record(["toolkit-build:mystery"]))


class TestWorktreeSplitSession(unittest.TestCase):
    """After EnterWorktree a session's later subagents land under the worktree's
    own project dir while the main transcript stays where the session started."""

    def test_records_a_session_whose_agents_are_in_a_worktree_project_dir(self):
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
        ledger_path = os.path.join(root, "ledger.jsonl")
        config_path = os.path.join(root, "config.json")
        with open(config_path, "w") as handle:
            json.dump({"build_cost": {"ledger_path": ledger_path}}, handle)

        result = run_hook({"session_id": sid, "transcript_path": transcript_path,
                           "cwd": "/x", "reason": "exit"}, config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        written = rows(ledger_path)
        self.assertEqual(1, len(written))
        self.assertEqual(sid, written[0]["session_id"])
        self.assertIn("toolkit-build:executor", written[0]["agent_types"])
        self.assertEqual(777, written[0]["executor_output"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
