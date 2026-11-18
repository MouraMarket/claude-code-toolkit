#!/usr/bin/env python3
"""Tests for `codex_review_lib`'s meter-reading trio: `read_rate_limits`,
`read_run_delta`, `format_headroom`.

Every test is hermetic (its own tempdir `sessions_root`, controlled mtimes)
except the positive control, which reads the real `~/.codex/sessions`
tree and `skipTest`s when it is absent or carries no `rate_limits` block --
a green suite against a stubbed external surface only tests your model of
that surface, per this repo's testing rule.
"""

import datetime
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, HERE)
import codex_review_lib  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(HERE), "build-lens"))
import review_lib  # noqa: E402

REAL_SESSIONS_ROOT = os.path.expanduser("~/.codex/sessions")


def _write_jsonl(path, lines):
    """Write `lines` (already-JSON-encodable objects, or raw strings for
    deliberately-malformed content) one per line to `path`."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            if isinstance(line, str):
                handle.write(line + "\n")
            else:
                handle.write(json.dumps(line) + "\n")


def _event_line(rate_limits):
    """A line in the real nested shape: `payload.rate_limits`, not a
    top-level key."""
    return {"type": "event_msg",
            "payload": {"type": "token_count", "rate_limits": rate_limits}}


def _touch_mtime(path, when):
    os.utime(path, (when, when))


class TempSessionsRoot:
    """A throwaway `sessions_root` tempdir, cleaned up on context exit."""

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix="codex-review-sessions-")
        return self.root

    def __exit__(self, *exc_info):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)
        return False


class TestNestedShape(unittest.TestCase):
    """the block lives at `payload.rate_limits`, not top level.

    A reader that does `obj.get("rate_limits")` on the raw JSON line finds
    nothing -- there is no top-level `rate_limits` key on a real line, only
    a nested one -- so it returns None on every line in this fixture and
    this test fails against it. That is deliberate: this test exists to
    catch exactly that "simplification."
    """

    def test_parses_the_real_nested_shape(self):
        with TempSessionsRoot() as root:
            rate_limits = {"primary": {"used_percent": 12.5,
                                        "window_minutes": 10080}}
            _write_jsonl(os.path.join(root, "rollout-a.jsonl"),
                         [_event_line(rate_limits)])

            result = codex_review_lib.read_rate_limits(root)

        self.assertEqual(result, rate_limits)


class TestPositiveControl(unittest.TestCase):
    """a real log, not a fixture, proves the reader parses production
    data rather than just this test file's own idea of the shape."""

    def test_reads_a_block_from_the_real_sessions_tree(self):
        if not os.path.isdir(REAL_SESSIONS_ROOT):
            self.skipTest(f"{REAL_SESSIONS_ROOT} does not exist on this machine")

        result = codex_review_lib.read_rate_limits(REAL_SESSIONS_ROOT)
        if result is None:
            self.skipTest("no rollout file under ~/.codex/sessions carries "
                           "a rate_limits block on this machine")

        self.assertIsInstance(result, dict)
        self.assertIn("primary", result)


class TestLastBlockWins(unittest.TestCase):
    """several `rate_limits`-carrying lines in one file -- the LAST
    one wins, not the first."""

    def test_returns_the_last_block_not_the_first(self):
        with TempSessionsRoot() as root:
            first = {"primary": {"used_percent": 1.0}}
            last = {"primary": {"used_percent": 99.0}}
            _write_jsonl(os.path.join(root, "rollout-a.jsonl"), [
                _event_line(first),
                {"type": "event_msg", "payload": {"type": "other"}},
                _event_line(last),
            ])

            result = codex_review_lib.read_rate_limits(root)

        self.assertEqual(result, last)


class TestNewestFirstBothDirections(unittest.TestCase):
    """newest-first is asserted in BOTH directions with two separate
    fixtures, because an earlier assumption about which file carries the
    block was measured to be false.

    A reader that SKIPS the newest file fails `test_block_only_in_newest`.
    A reader that reads ONLY the newest file fails `test_block_only_in_older`.
    """

    def test_block_only_in_newest(self):
        with TempSessionsRoot() as root:
            now = time.time()
            older_no_block = os.path.join(root, "rollout-older.jsonl")
            newest_with_block = os.path.join(root, "rollout-newest.jsonl")
            block = {"primary": {"used_percent": 42.0}}

            _write_jsonl(older_no_block,
                         [{"type": "event_msg", "payload": {"type": "other"}}])
            _write_jsonl(newest_with_block, [_event_line(block)])
            _touch_mtime(older_no_block, now - 100)
            _touch_mtime(newest_with_block, now)

            result = codex_review_lib.read_rate_limits(root)

        self.assertEqual(result, block)

    def test_block_only_in_older(self):
        with TempSessionsRoot() as root:
            now = time.time()
            older_with_block = os.path.join(root, "rollout-older.jsonl")
            newest_no_block = os.path.join(root, "rollout-newest.jsonl")
            block = {"primary": {"used_percent": 7.0}}

            _write_jsonl(older_with_block, [_event_line(block)])
            _write_jsonl(newest_no_block,
                         [{"type": "event_msg", "payload": {"type": "other"}}])
            _touch_mtime(older_with_block, now - 100)
            _touch_mtime(newest_no_block, now)

            result = codex_review_lib.read_rate_limits(root)

        self.assertEqual(result, block)


class TestNoneCasesNeverRaise(unittest.TestCase):
    """five distinct `None` cases, asserted individually rather than
    in one loop, so a regression in any single case fails on its own name.

    For each of the five, the call must raise nothing -- wrapped in a
    bare `try/except BaseException` so the test itself fails (rather than
    erroring out uninformatively) if `read_rate_limits` ever raises.
    """

    def _assert_none_without_raising(self, sessions_root):
        try:
            result = codex_review_lib.read_rate_limits(sessions_root)
        except BaseException as exc:  # noqa: BLE001 -- this IS the assertion
            self.fail(f"read_rate_limits raised {exc!r} instead of "
                      f"returning None")
        self.assertIsNone(result)

    def test_missing_root(self):
        with TempSessionsRoot() as root:
            missing = os.path.join(root, "does-not-exist")
        self._assert_none_without_raising(missing)

    def test_empty_root(self):
        with TempSessionsRoot() as root:
            self._assert_none_without_raising(root)

    def test_no_rate_limits_anywhere(self):
        with TempSessionsRoot() as root:
            _write_jsonl(os.path.join(root, "rollout-a.jsonl"), [
                {"type": "event_msg", "payload": {"type": "task_started"}},
                {"type": "event_msg", "payload": {"type": "token_count"}},
            ])
            self._assert_none_without_raising(root)

    def test_invalid_json_lines(self):
        with TempSessionsRoot() as root:
            _write_jsonl(os.path.join(root, "rollout-a.jsonl"), [
                "this is not json at all {{{",
                "{unterminated",
            ])
            self._assert_none_without_raising(root)

    def test_malformed_structure(self):
        """Valid JSON, wrong shape: the line parses but is a bare list, and
        a second line's `payload` is a string rather than a mapping. Both
        must be skipped rather than raising an AttributeError/TypeError
        trying to call `.get` on something that isn't a dict."""
        with TempSessionsRoot() as root:
            _write_jsonl(os.path.join(root, "rollout-a.jsonl"), [
                [1, 2, 3],
                {"type": "event_msg", "payload": "not-a-mapping"},
            ])
            self._assert_none_without_raising(root)


class TestRunDeltaNeverFallsBack(unittest.TestCase):
    """`read_run_delta` returns `None` when the only block is in a
    file OLDER than `before_path`, and a real delta when a newer file has
    one. It must never fall back to `before_path`'s own file (or any older
    one) to manufacture a delta -- that produces a fabricated 0.0."""

    def test_none_when_only_block_is_older_than_before_path(self):
        with TempSessionsRoot() as root:
            now = time.time()
            before_path = os.path.join(root, "rollout-before.jsonl")
            older_path = os.path.join(root, "rollout-even-older.jsonl")

            _write_jsonl(before_path,
                         [_event_line({"primary": {"used_percent": 10.0}})])
            _write_jsonl(older_path,
                         [_event_line({"primary": {"used_percent": 3.0}})])
            _touch_mtime(older_path, now - 200)
            _touch_mtime(before_path, now - 100)

            result = codex_review_lib.read_run_delta(root, before_path)

        self.assertIsNone(result)

    def test_delta_when_a_newer_file_has_one(self):
        with TempSessionsRoot() as root:
            now = time.time()
            before_path = os.path.join(root, "rollout-before.jsonl")
            after_path = os.path.join(root, "rollout-after.jsonl")

            _write_jsonl(before_path,
                         [_event_line({"primary": {"used_percent": 10.0}})])
            _write_jsonl(after_path,
                         [_event_line({"primary": {"used_percent": 15.5}})])
            _touch_mtime(before_path, now - 100)
            _touch_mtime(after_path, now)

            result = codex_review_lib.read_run_delta(root, before_path)

        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 5.5)


class TestSnapshotMeter(unittest.TestCase):
    """`snapshot_meter` returns `(path, block)` -- the exact rollout
    file the block came from, not just the block. It shares
    `_rollout_paths`/`_newest_first`/`_last_rate_limits_block` with
    `read_rate_limits` rather than re-walking independently, so the two can
    never select a different file for the same tree -- asserted below
    against the same multi-file fixtures the newest-first test uses for `read_rate_limits`.
    """

    def test_returns_a_path_that_exists_and_the_block_read_from_it(self):
        with TempSessionsRoot() as root:
            block = {"primary": {"used_percent": 22.0}}
            path = os.path.join(root, "rollout-a.jsonl")
            _write_jsonl(path, [_event_line(block)])

            result_path, result_block = codex_review_lib.snapshot_meter(root)

            self.assertTrue(os.path.exists(result_path))
            self.assertEqual(result_path, path)

        self.assertEqual(result_block, block)

    def test_none_none_on_missing_root(self):
        with TempSessionsRoot() as root:
            missing = os.path.join(root, "does-not-exist")
        result = codex_review_lib.snapshot_meter(missing)
        self.assertEqual(result, (None, None))

    def test_none_none_on_empty_root(self):
        with TempSessionsRoot() as root:
            result = codex_review_lib.snapshot_meter(root)
        self.assertEqual(result, (None, None))

    def test_path_matches_read_rate_limits_when_the_block_is_in_the_newest_file(self):
        with TempSessionsRoot() as root:
            now = time.time()
            older_no_block = os.path.join(root, "rollout-older.jsonl")
            newest_with_block = os.path.join(root, "rollout-newest.jsonl")
            block = {"primary": {"used_percent": 42.0}}

            _write_jsonl(older_no_block,
                         [{"type": "event_msg", "payload": {"type": "other"}}])
            _write_jsonl(newest_with_block, [_event_line(block)])
            _touch_mtime(older_no_block, now - 100)
            _touch_mtime(newest_with_block, now)

            expected_block = codex_review_lib.read_rate_limits(root)
            result_path, result_block = codex_review_lib.snapshot_meter(root)

        self.assertEqual(result_path, newest_with_block)
        self.assertEqual(result_block, expected_block)
        self.assertEqual(result_block, block)

    def test_path_matches_read_rate_limits_when_the_block_is_in_an_older_file(self):
        with TempSessionsRoot() as root:
            now = time.time()
            older_with_block = os.path.join(root, "rollout-older.jsonl")
            newest_no_block = os.path.join(root, "rollout-newest.jsonl")
            block = {"primary": {"used_percent": 7.0}}

            _write_jsonl(older_with_block, [_event_line(block)])
            _write_jsonl(newest_no_block,
                         [{"type": "event_msg", "payload": {"type": "other"}}])
            _touch_mtime(older_with_block, now - 100)
            _touch_mtime(newest_no_block, now)

            expected_block = codex_review_lib.read_rate_limits(root)
            result_path, result_block = codex_review_lib.snapshot_meter(root)

        self.assertEqual(result_path, older_with_block)
        self.assertEqual(result_block, expected_block)
        self.assertEqual(result_block, block)


class TestFormatHeadroom(unittest.TestCase):
    """Not one of the eight numbered ACs, but `format_headroom` is a
    deliverable of this task: it must never return an empty string, on
    `None` or on any input."""

    def test_none_yields_a_non_empty_unreadable_string(self):
        result = codex_review_lib.format_headroom(None)
        self.assertTrue(result)
        self.assertIn("unreadable", result)

    def test_a_real_block_yields_a_one_line_human_string(self):
        block = {"primary": {"used_percent": 30.0, "window_minutes": 10080}}
        result = codex_review_lib.format_headroom(block)
        self.assertTrue(result)
        self.assertNotIn("\n", result)
        self.assertIn("70.0", result)  # headroom = 100 - 30

    def test_malformed_block_yields_a_non_empty_unreadable_string(self):
        result = codex_review_lib.format_headroom({"primary": "not-a-mapping"})
        self.assertTrue(result)
        self.assertIn("unreadable", result)


BUILD_ARGV_REPO_PATH = "/repo/target"
BUILD_ARGV_SCHEMA_PATH = "/repo/schema.json"
BUILD_ARGV_OUTPUT_PATH = "/tmp/report.json"
BUILD_ARGV_PROMPT = "review this repository and report findings as JSON"


def _sample_argv(skip_git_check=False):
    """`build_argv` called with fixed sample arguments, reused across the
    tests below so each one only asserts, never re-derives, the inputs."""
    return codex_review_lib.build_argv(
        repo_path=BUILD_ARGV_REPO_PATH,
        schema_path=BUILD_ARGV_SCHEMA_PATH,
        output_path=BUILD_ARGV_OUTPUT_PATH,
        prompt=BUILD_ARGV_PROMPT,
        model=PINNED_MODEL,
        skip_git_check=skip_git_check,
    )


def _c_value_starting_with(argv, prefix):
    """The value token following a `-c` flag whose value starts with
    `prefix` (e.g. `"model_reasoning_effort"`), or `None` if no `-c` pair
    has that key. Used instead of `argv.index("-c")` because `-c` appears
    twice in a correct argv and `.index` only ever finds the first."""
    for i, tok in enumerate(argv):
        if tok == "-c" and i + 1 < len(argv) and argv[i + 1].startswith(prefix):
            return argv[i + 1]
    return None


# The pins each gate must dispatch with, written as LITERALS rather than read
# from `codex_review_lib` -- a test that compares the argv to the constant
# passes when the constant itself is edited, which is the change this exists
# to make loud. Moving a pin means editing both, on purpose. Per-gate split
# a gate-to-model map: one model for Code Review, another for Merge, Plan and Design.
PINNED_MODEL = "gpt-6-astra"
PINNED_CODE_MODEL = "gpt-5.6-terra"
PINNED_EFFORT_PAIR = 'model_reasoning_effort="high"'


def _assert_pinned_model_and_effort(testcase, argv, model=PINNED_MODEL):
    """Both halves of the pin, each with its value: `-m` followed by the
    model, and the effort `-c` pair. Without `-m`, codex inherits the
    model from `~/.codex/config.toml` and the report looks normal."""
    testcase.assertIn("-m", argv, "no -m: the model would be inherited "
                                  "from ~/.codex/config.toml")
    testcase.assertEqual(argv.count("-m"), 1)
    testcase.assertEqual(argv[argv.index("-m") + 1], model)
    testcase.assertEqual(_c_value_starting_with(argv, "model_reasoning_effort"),
                         PINNED_EFFORT_PAIR)


class TestBuildArgvOneTestPerFlag(unittest.TestCase):
    """one test per flag, each asserting BOTH the flag and its
    value, so a mutation that deletes only the value (leaving a bare flag
    behind) reds this test rather than slipping past a membership-only
    check."""

    def test_dash_cap_c_carries_repo_path(self):
        argv = _sample_argv()
        idx = argv.index("-C")
        self.assertEqual(argv[idx + 1], BUILD_ARGV_REPO_PATH)

    def test_reasoning_effort_c_pair(self):
        # This asserts the ARGV, not a behavioural difference in the
        # review. This machine's own ~/.codex/config.toml already sets
        # model_reasoning_effort = "high", so no live run here could ever
        # distinguish "the flag set it" from "the file already had it" --
        # a run against this machine looks identical either way. The flag
        # exists for a teammate's machine, where its absence silently
        # reasons at the tool's default and returns a confident
        # zero-finding review indistinguishable from a clean gate.
        value = _c_value_starting_with(_sample_argv(), "model_reasoning_effort")
        self.assertEqual(value, PINNED_EFFORT_PAIR)

    def test_dash_m_carries_the_pinned_model(self):
        # Same reasoning as the effort pair above, and a sharper case of it:
        # the model in this machine's config.toml changed
        # between two gates, so the argv is the only place the reviewer's
        # identity can be fixed.
        argv = _sample_argv()
        idx = argv.index("-m")
        self.assertEqual(argv[idx + 1], PINNED_MODEL)

    def test_the_pin_is_the_same_with_skip_git_check(self):
        _assert_pinned_model_and_effort(self, _sample_argv(skip_git_check=True))

    def test_sandbox_mode_c_pair(self):
        value = _c_value_starting_with(_sample_argv(), "sandbox_mode")
        self.assertEqual(value, 'sandbox_mode="workspace-write"')

    def test_output_schema_carries_schema_path(self):
        argv = _sample_argv()
        idx = argv.index("--output-schema")
        self.assertEqual(argv[idx + 1], BUILD_ARGV_SCHEMA_PATH)

    def test_dash_o_carries_output_path(self):
        argv = _sample_argv()
        idx = argv.index("-o")
        self.assertEqual(argv[idx + 1], BUILD_ARGV_OUTPUT_PATH)


class TestBuildArgvOnePositionalLast(unittest.TestCase):
    """exactly one positional in argv -- the prompt -- and it
    is the LAST element. This replaces a `-C`-before-`review`
    ordering test (there is no `review` subcommand any more, so that
    invariant no longer applies) onto the new one, and it fails the same
    way: a positional in the wrong place (e.g. wedged right after `-C`
    instead of at the end) is silently consumed as a flag's value,
    producing an argv that parses fine and reviews with no instructions.

    A test that only asserts the prompt is PRESENT (`assertIn(prompt,
    argv)`) PASSES a prompt wedged into a flag's value slot -- membership
    does not see position. This test instead walks the whole argv after
    the fixed `codex exec` prefix, classifying every token as a flag, a
    flag's value, or a positional, and requires exactly one positional
    overall -- plus a direct check that it sits at `argv[-1]`, which is
    what actually catches the wedged-after-`-C` mutation (the positional
    COUNT stays one either way; only its position moves).
    """

    FLAGS_TAKING_A_VALUE = {"-C", "-m", "-c", "--output-schema", "-o"}

    def _positionals_after_codex_exec(self, argv):
        self.assertEqual(argv[0], "codex")
        self.assertEqual(argv[1], "exec")
        positionals = []
        i = 2
        while i < len(argv):
            tok = argv[i]
            if tok in self.FLAGS_TAKING_A_VALUE:
                self.assertLess(i + 1, len(argv), f"{tok} has no value")
                i += 2
            elif tok.startswith("-"):
                i += 1  # a boolean flag, e.g. --skip-git-repo-check
            else:
                positionals.append(tok)
                i += 1
        return positionals

    def test_exactly_one_positional_and_it_is_the_prompt(self):
        argv = _sample_argv()
        self.assertEqual(self._positionals_after_codex_exec(argv), [BUILD_ARGV_PROMPT])

    def test_exactly_one_positional_with_skip_git_check(self):
        argv = _sample_argv(skip_git_check=True)
        self.assertEqual(self._positionals_after_codex_exec(argv), [BUILD_ARGV_PROMPT])

    def test_prompt_is_the_last_element(self):
        argv = _sample_argv()
        self.assertEqual(argv[-1], BUILD_ARGV_PROMPT)

    def test_prompt_is_the_last_element_with_skip_git_check(self):
        argv = _sample_argv(skip_git_check=True)
        self.assertEqual(argv[-1], BUILD_ARGV_PROMPT)


class TestBuildArgvNoDeadForm(unittest.TestCase):
    """a regression guard against rebuilding the dead form.
    Measured twice, the second time against a schema legal at
    every object level: `codex exec review --output-schema <schema> -o
    <file>` exits 0 and writes PROSE to `<file>`, not structured output --
    `-o` is `--output-last-message`, the agent's final message, not a JSON
    sink. `"review" not in argv` and `"--base" not in argv` are exact
    list-membership checks (an element equal to the literal `"review"`,
    not a substring match), so a prompt that happens to contain the word
    "review" in running text does not trip this guard -- only rebuilding
    the subcommand form itself does.
    """

    def test_review_subcommand_is_absent(self):
        self.assertNotIn("review", _sample_argv())

    def test_dash_dash_base_is_absent(self):
        self.assertNotIn("--base", _sample_argv())


class TestBuildArgvCPairsAdjacent(unittest.TestCase):
    """each `-c` pair is TWO adjacent argv entries -- `"-c"`
    then the `'key="value"'` string -- never one fused string. There is no
    shell here to split a fused token, so `codex` would receive a single
    malformed argument."""

    def test_two_separate_c_flags_each_followed_by_its_value(self):
        argv = _sample_argv()
        c_indices = [i for i, tok in enumerate(argv) if tok == "-c"]
        self.assertEqual(len(c_indices), 2)
        for idx in c_indices:
            self.assertLess(idx + 1, len(argv))

    def test_c_pairs_are_in_order_reasoning_effort_then_sandbox(self):
        argv = _sample_argv()
        c_indices = [i for i, tok in enumerate(argv) if tok == "-c"]
        first_value, second_value = argv[c_indices[0] + 1], argv[c_indices[1] + 1]
        self.assertEqual(first_value, PINNED_EFFORT_PAIR)
        self.assertEqual(second_value, 'sandbox_mode="workspace-write"')

    def test_no_element_fuses_the_flag_and_its_value(self):
        # argv[-1] is the prompt -- a real positional string, legitimately
        # full of spaces. This check is scoped to everything BEFORE
        # it: the flags and their `-c` values, where a fused
        # `'-c key="value"'` single token would be the actual bug.
        argv = _sample_argv()
        for tok in argv[:-1]:
            self.assertNotIn(" ", tok,
                              f"argv element {tok!r} looks fused -- a real "
                              f"shell-less argv has no spaces inside one entry")


class TestBuildArgvPurity(unittest.TestCase):
    """`build_argv` is PURE -- no filesystem access, no
    subprocess, no env read. Proven by calling it with paths that do not
    exist anywhere on disk and getting a normal result back rather than an
    exception; a function that stats or opens its path arguments would
    raise on these. This is also why `--skip-git-repo-check` cannot be
    decided in here -- it is an explicit boolean parameter, defaulting to
    `False`, and the executable is what decides its value against the real
    filesystem."""

    def test_nonexistent_paths_produce_a_normal_result(self):
        argv = codex_review_lib.build_argv(
            repo_path="/no/such/repo",
            schema_path="/no/such/schema.json",
            output_path="/no/such/output.json",
            prompt="review this diff",
            model=PINNED_MODEL,
        )
        self.assertIsInstance(argv, list)
        self.assertIn("/no/such/repo", argv)
        self.assertNotIn("--skip-git-repo-check", argv)

    def test_skip_git_check_is_a_pure_boolean_parameter(self):
        argv = codex_review_lib.build_argv(
            repo_path="/no/such/repo",
            schema_path="/no/such/schema.json",
            output_path="/no/such/output.json",
            prompt="review this diff",
            model=PINNED_MODEL,
            skip_git_check=True,
        )
        self.assertIn("--skip-git-repo-check", argv)


class TestModelForGate(unittest.TestCase):
    """Per-gate split. Each gate's model is asserted
    as a LITERAL, so swapping two entries in `GATE_MODELS` -- the silent
    failure, a Plan Review on the Code gate's model that reads like a normal report --
    goes red here."""

    def test_code_gate_runs_on_terra(self):
        self.assertEqual(codex_review_lib.model_for_gate("code"),
                         PINNED_CODE_MODEL)

    def test_merge_plan_and_design_gates_run_on_astra(self):
        for gate in ("merge", "plan", "design"):
            with self.subTest(gate=gate):
                self.assertEqual(codex_review_lib.model_for_gate(gate),
                                 PINNED_MODEL)

    def test_an_unknown_gate_raises_rather_than_falling_back(self):
        with self.assertRaises(ValueError):
            codex_review_lib.model_for_gate("Code")

    def test_build_argv_has_no_default_model(self):
        # A default would let a caller that forgot its gate's model
        # dispatch on the wrong model silently.
        with self.assertRaises(TypeError):
            codex_review_lib.build_argv(
                repo_path="/r", schema_path="/s", output_path="/o",
                prompt="p")


class TestGateModelsCoverTheGates(unittest.TestCase):
    def test_gate_models_keys_are_exactly_the_gates(self):
        self.assertEqual(set(codex_review_lib.GATE_MODELS),
                         set(codex_review_lib.GATES))
        self.assertEqual(set(codex_review_lib.GATES),
                         {"code", "merge", "plan", "design", "architecture"})


class TestResolveGateModel(unittest.TestCase):
    """`resolve_gate_model` turns the raw `codex_review.models` config value
    into a gate's model. Every refusal names the offending key, because a
    value that silently fell back to the pin would dispatch a review on a
    model the user did not choose."""

    def _refused(self, models_config, gate="code"):
        with self.assertRaises(codex_review_lib.ConfigError) as caught:
            codex_review_lib.resolve_gate_model(gate, models_config)
        return str(caught.exception)

    def test_config_error_is_a_value_error(self):
        self.assertTrue(issubclass(codex_review_lib.ConfigError, ValueError))

    def test_unknown_gate_raises_value_error(self):
        with self.assertRaises(ValueError):
            codex_review_lib.resolve_gate_model("Code", None)

    def test_valid_override_is_configured(self):
        self.assertEqual(
            codex_review_lib.resolve_gate_model("code", {"code": "my-model"}),
            ("my-model", "configured"))

    def test_override_applies_only_to_its_own_gate(self):
        self.assertEqual(
            codex_review_lib.resolve_gate_model("merge", {"code": "my-model"}),
            (PINNED_MODEL, "default"))

    def test_unset_forms_keep_the_pin(self):
        for models_config in (None, {}, {"code": None}):
            with self.subTest(models_config=models_config):
                self.assertEqual(
                    codex_review_lib.resolve_gate_model("code", models_config),
                    (PINNED_CODE_MODEL, "default"))

    def test_models_that_is_not_an_object_is_refused(self):
        for bad in (["code"], "my-model", 1, True, 1.5):
            with self.subTest(bad=bad):
                self.assertIn("codex_review.models must be an object",
                              self._refused(bad))

    def test_empty_whitespace_and_leading_dash_values_are_refused(self):
        for bad in ("", "a b", " x", "x\n", "-x", "--model"):
            with self.subTest(bad=bad):
                message = self._refused({"code": bad})
                self.assertIn("codex_review.models.code", message)
                self.assertIn(repr(bad), message)

    def test_bool_and_int_values_are_refused(self):
        for bad in (True, False, 1, 0, 1.5, ["m"], {"m": 1}):
            with self.subTest(bad=bad):
                self.assertIn("codex_review.models.code",
                              self._refused({"code": bad}))

    def test_unknown_key_is_refused_even_when_resolving_another_gate(self):
        message = self._refused({"review": "my-model", "code": "ok"},
                                gate="code")
        self.assertIn("codex_review.models.review", message)
        self.assertIn("is not a gate", message)
        self.assertIn("code, design, merge, plan", message)

    def test_bad_value_under_another_gate_is_refused_too(self):
        self.assertIn("codex_review.models.plan",
                      self._refused({"plan": "-x"}, gate="code"))


class TestValidModelName(unittest.TestCase):
    def test_accepts_plain_names(self):
        for good in ("m", "gpt-6-astra", "my.model_1"):
            with self.subTest(good=good):
                self.assertTrue(codex_review_lib.valid_model_name(good))

    def test_refuses_everything_else(self):
        for bad in ("", "a b", "a\tb", "-x", None, True, 1, b"m", ["m"]):
            with self.subTest(bad=bad):
                self.assertFalse(codex_review_lib.valid_model_name(bad))


class TestUnavailableReason(unittest.TestCase):
    """The shared availability predicate: `codex` on PATH and the auth file
    readable. A missing piece must come back as a sentence, never a raise."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="codex-review-avail-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.shim_dir = os.path.join(self.root, "bin")
        self.empty_dir = os.path.join(self.root, "empty")
        self.home = os.path.join(self.root, "home")
        for directory in (self.shim_dir, self.empty_dir, self.home):
            os.makedirs(directory)
        shim = os.path.join(self.shim_dir, "codex")
        with open(shim, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(shim, 0o755)

    def _write_auth(self):
        os.makedirs(os.path.join(self.home, ".codex"), exist_ok=True)
        with open(os.path.join(self.home, ".codex", "auth.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{}")

    def test_no_codex_on_path(self):
        self._write_auth()
        with mock.patch.dict(os.environ, {"PATH": self.empty_dir}):
            reason = codex_review_lib.unavailable_reason(home=self.home)
        self.assertEqual(reason, "the codex binary is not on PATH")

    def test_binary_is_checked_before_auth(self):
        with mock.patch.dict(os.environ, {"PATH": self.empty_dir}):
            reason = codex_review_lib.unavailable_reason(home=self.home)
        self.assertEqual(reason, "the codex binary is not on PATH")

    def test_auth_missing(self):
        with mock.patch.dict(os.environ, {"PATH": self.shim_dir}):
            reason = codex_review_lib.unavailable_reason(home=self.home)
        self.assertEqual(
            reason,
            "the Codex auth file is missing or unreadable: %s"
            % os.path.join(self.home, ".codex", "auth.json"))

    def test_auth_is_a_directory_counts_as_unreadable(self):
        os.makedirs(os.path.join(self.home, ".codex", "auth.json"))
        with mock.patch.dict(os.environ, {"PATH": self.shim_dir}):
            reason = codex_review_lib.unavailable_reason(home=self.home)
        self.assertIn("missing or unreadable", reason)

    def test_both_present_is_available(self):
        self._write_auth()
        with mock.patch.dict(os.environ, {"PATH": self.shim_dir}):
            self.assertIsNone(
                codex_review_lib.unavailable_reason(home=self.home))

    def test_default_home_is_the_users_home(self):
        self._write_auth()
        with mock.patch.dict(os.environ, {"PATH": self.shim_dir,
                                          "HOME": self.home}):
            self.assertIsNone(codex_review_lib.unavailable_reason())


class TestModelRejected(unittest.TestCase):
    """Only the measured shape of a model the account does not serve is
    "unavailable"; any other failure is an ordinary non-zero exit."""

    LINE = ('ERROR: {"detail":"The model is not supported when using Codex '
            'with a ChatGPT account.","status":400}')

    def test_the_measured_line_is_rejected(self):
        self.assertTrue(codex_review_lib.model_rejected(
            'unexpected status 400: {"detail":"model is not supported",'
            '"status":400}'))

    def test_status_with_spaces_is_rejected(self):
        self.assertTrue(codex_review_lib.model_rejected(
            '{"status": 400, "detail": "model is not supported"}'))

    def test_other_status_is_not_rejected(self):
        self.assertFalse(codex_review_lib.model_rejected(
            '{"detail":"model is not supported","status":401}'))

    def test_status_400_alone_is_not_rejected(self):
        self.assertFalse(codex_review_lib.model_rejected(
            '{"detail":"bad request","status":400}'))

    def test_phrase_alone_is_not_rejected(self):
        self.assertFalse(codex_review_lib.model_rejected(
            "the model is not supported"))

    def test_phrase_is_case_sensitive(self):
        self.assertFalse(codex_review_lib.model_rejected(
            '{"detail":"Model Is Not Supported","status":400}'))

    def test_other_text_and_non_strings_are_not_rejected(self):
        for value in ("", "boom", None, b'"status":400 model is not supported',
                      400, True):
            with self.subTest(value=value):
                self.assertFalse(codex_review_lib.model_rejected(value))


class TestBuildArgvSkipGitCheckBeforePositional(unittest.TestCase):
    """`--skip-git-repo-check`, when present, appears BEFORE
    the positional prompt -- otherwise it is consumed as part of the
    prompt rather than parsed as a flag. the positional-count walk would
    also catch a trailing flag after the prompt (it would either be
    swallowed into the positional count, or land after `argv[-1]` and fail
    the "prompt is last" check), but the plan calls for a direct assertion
    too, since this is the exact case a careless `argv.append(...)` after
    the prompt is already in place would break.
    """

    def test_skip_git_check_appears_before_the_prompt(self):
        argv = _sample_argv(skip_git_check=True)
        self.assertLess(argv.index("--skip-git-repo-check"),
                         argv.index(BUILD_ARGV_PROMPT))


# ---------------------------------------------------------------------------
# Calibration, budget, and per-run cost: `default_sessions_root`,
# `mtime_or_none`, `calibration`, `gates_left`, `format_gates_left`,
# `run_cost`. All pure-lib tests.
# ---------------------------------------------------------------------------

# The library ships no calibration, so every test of the calibrated code paths
# installs this synthetic entry for its own duration and never depends on a
# shipped one.
SYNTHETIC_CALIBRATION = {
    ("test-plan", 10080, "test-model"): {
        "points_per_million_uncached": 10.0,
        "gate_points": (0.5, 1.0),
    },
}
with_synthetic_calibration = mock.patch.dict(
    codex_review_lib.CALIBRATIONS, SYNTHETIC_CALIBRATION)


def _calibrated_block(used_percent, resets_at,
                       plan_type="test-plan",
                       window_minutes=10080):
    """A `rate_limits` block shaped for the synthetic key in
    `SYNTHETIC_CALIBRATION`, with `used_percent`/`resets_at` as the usual
    per-test variables."""
    return {
        "plan_type": plan_type,
        "primary": {"used_percent": used_percent, "window_minutes": window_minutes,
                    "resets_at": resets_at},
    }


class TestMtimeOrNonePublicAlias(unittest.TestCase):
    """`mtime_or_none` is the public alias other modules import; `_mtime_or_none`
    keeps working unchanged for this module's own internal callers."""

    def test_alias_matches_the_private_function(self):
        with TempSessionsRoot() as root:
            path = os.path.join(root, "f.jsonl")
            _write_jsonl(path, [{"a": 1}])
            self.assertEqual(codex_review_lib.mtime_or_none(path),
                              codex_review_lib._mtime_or_none(path))

    def test_missing_path_is_none(self):
        self.assertIsNone(codex_review_lib.mtime_or_none("/no/such/path"))


class TestWindowKeyRoundsUp(unittest.TestCase):
    """`window_key` rounds UP to the hour, never to the nearest hour --
    the key must never land BEFORE the `resets_at` it describes, or
    `record_announcement`'s prune (which drops any record whose stored
    `resets_at` is `<= now`) can delete it on the very write that created
    it."""

    def test_key_is_never_before_the_value(self):
        hour = 1790000000 - (1790000000 % 3600)  # an arbitrary whole hour
        cases = [hour + 1,               # HH:00:01
                 hour + 13 * 60,         # HH:13
                 hour + 29 * 60 + 59,    # HH:29:59
                 hour + 30 * 60,         # HH:30
                 hour + 59 * 60 + 59]    # HH:59:59
        for value in cases:
            self.assertGreaterEqual(codex_review_lib.window_key(value), value,
                                    "window_key(%r)" % value)

    def test_equal_keys_for_x_and_x_plus_5_mid_hour(self):
        hour = 1790000000 - (1790000000 % 3600)
        x = hour + 30 * 60  # mid-hour: nowhere near either boundary
        self.assertEqual(codex_review_lib.window_key(x),
                          codex_review_lib.window_key(x + 5))

    def test_non_number_returns_none(self):
        self.assertIsNone(codex_review_lib.window_key(None))
        self.assertIsNone(codex_review_lib.window_key("not a number"))
        self.assertIsNone(codex_review_lib.window_key(True))


class TestDefaultSessionsRoot(unittest.TestCase):
    """`default_sessions_root` honours `CODEX_HOME` when set and
    non-empty, and falls back to `~/.codex/sessions` otherwise -- including
    when `CODEX_HOME` is set to the EMPTY string, which must not be read as
    "set"."""

    def setUp(self):
        self._old = os.environ.get("CODEX_HOME")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._old

    def test_honours_codex_home(self):
        os.environ["CODEX_HOME"] = "/tmp/fake-codex-home"
        self.assertEqual(codex_review_lib.default_sessions_root(),
                          os.path.join("/tmp/fake-codex-home", "sessions"))

    def test_falls_back_without_codex_home(self):
        os.environ.pop("CODEX_HOME", None)
        self.assertEqual(codex_review_lib.default_sessions_root(),
                          os.path.expanduser("~/.codex/sessions"))

    def test_empty_codex_home_falls_back(self):
        os.environ["CODEX_HOME"] = ""
        self.assertEqual(codex_review_lib.default_sessions_root(),
                          os.path.expanduser("~/.codex/sessions"))


@with_synthetic_calibration
class TestGatesLeftNoneCases(unittest.TestCase):
    """`gates_left` returns None for a block that is None, a
    `resets_at` already in the past, one equal to `now` (not strictly
    AFTER it), and one that is a bool -- Python's bool-is-an-int means
    `True`/`False` would otherwise silently read as 1/0 epoch seconds."""

    def test_block_none(self):
        self.assertIsNone(
            codex_review_lib.gates_left(None, time.time(), "test-model"))

    def test_resets_at_in_the_past(self):
        now = time.time()
        block = _calibrated_block(50.0, now - 10)
        self.assertIsNone(codex_review_lib.gates_left(block, now, "test-model"))

    def test_resets_at_equal_to_now(self):
        now = time.time()
        block = _calibrated_block(50.0, now)
        self.assertIsNone(codex_review_lib.gates_left(block, now, "test-model"))

    def test_resets_at_is_a_bool(self):
        # L2: `now=time.time()` can't fail for the stated reason -- True
        # equals 1, already far in the past against a real epoch `now`, so
        # the `resets_at <= now` expiry check alone returns None regardless
        # of whether the isinstance(bool) guard exists. Use a `now` the bool
        # value is still "in the future" against (0, or any value < 1), so
        # only the isinstance(bool) guard can produce the None here.
        now = 0
        block = _calibrated_block(50.0, True)
        self.assertIsNone(codex_review_lib.gates_left(block, now, "test-model"))


@with_synthetic_calibration
class TestGatesLeftCalibratedAndUncalibrated(unittest.TestCase):
    """the synthetic-calibration block at used=82 gives
    low=18/high=36 (18 headroom // 1.0 p90 = 18, // 0.5 median = 36);
    used=100 and used=101 both floor to 0/0 -- headroom is clamped at 0.0,
    never negative. The SAME block with `plan_type` "free" has no fit, so
    low/high are None while `used_percent` still reports; model "other" is
    the same story for a different missing key."""

    def test_used_82_gives_low_18_high_36(self):
        now = time.time()
        block = _calibrated_block(82.0, now + 86400)
        result = codex_review_lib.gates_left(block, now, "test-model")
        self.assertEqual(result["low"], 18)
        self.assertEqual(result["high"], 36)
        self.assertEqual(result["used_percent"], 82.0)

    def test_used_100_floors_to_zero(self):
        now = time.time()
        block = _calibrated_block(100.0, now + 86400)
        result = codex_review_lib.gates_left(block, now, "test-model")
        self.assertEqual(result["low"], 0)
        self.assertEqual(result["high"], 0)

    def test_used_101_still_floors_to_zero_not_negative(self):
        now = time.time()
        block = _calibrated_block(101.0, now + 86400)
        result = codex_review_lib.gates_left(block, now, "test-model")
        self.assertEqual(result["low"], 0)
        self.assertEqual(result["high"], 0)

    def test_uncalibrated_plan_type_has_no_gate_count(self):
        now = time.time()
        block = _calibrated_block(82.0, now + 86400, plan_type="free")
        result = codex_review_lib.gates_left(block, now, "test-model")
        self.assertIsNone(result["low"])
        self.assertIsNone(result["high"])
        self.assertEqual(result["used_percent"], 82.0)

    def test_uncalibrated_model_has_no_gate_count(self):
        now = time.time()
        block = _calibrated_block(82.0, now + 86400)
        result = codex_review_lib.gates_left(block, now, "other")
        self.assertIsNone(result["low"])
        self.assertIsNone(result["high"])


class TestCalibrationShipsEmpty(unittest.TestCase):
    """A fit is one account's measurement of one window and does not
    transfer, so the library ships none. A seeded entry would price every
    user's gates at someone else's rate with nothing to say so."""

    def test_calibrations_is_empty(self):
        self.assertEqual(codex_review_lib.CALIBRATIONS, {})


class TestFormatGatesLeftExactStrings(unittest.TestCase):
    """`format_gates_left`'s exact output strings. TZ is pinned to
    UTC in THIS class only, so `%a %m-%d %H:%M` is deterministic regardless
    of the machine running the suite."""

    def setUp(self):
        self._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "UTC"
        time.tzset()

    def tearDown(self):
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        time.tzset()

    def _result(self, low, high, used_percent=82.0, resets_at=1700000000.0,
                window_minutes=10080, plan_type="test-plan",
                model="test-model"):
        return {
            "used_percent": used_percent,
            "resets_at": resets_at,
            "window_minutes": window_minutes,
            "plan_type": plan_type,
            "model": model,
            "low": low,
            "high": high,
        }

    def test_calibrated_range(self):
        result = self._result(17, 26)
        snap_mtime = 1699999000.0
        snap = time.strftime("%a %m-%d %H:%M", time.localtime(snap_mtime))
        reset = time.strftime("%a %m-%d %H:%M", time.localtime(1700000000.0))
        # Built with +, not %-interpolation -- a literal "%" immediately
        # followed by " o" (from "82% of") is itself a valid (if bizarre)
        # old-style format spec (a space flag + an %o conversion), so
        # applying % to a template carrying that text raises instead of
        # leaving it alone.
        expected = ("codex budget (estimate): ≈17–26 review gates "
                    "left — 82% of the 7-day window used as of " + snap
                    + ", resets " + reset)
        self.assertEqual(
            codex_review_lib.format_gates_left(result, snap_mtime), expected)

    def test_low_equals_high(self):
        result = self._result(5, 5)
        reset = time.strftime("%a %m-%d %H:%M", time.localtime(1700000000.0))
        expected = ("codex budget (estimate): 5 review gates left — "
                    "82% of the 7-day window used, resets " + reset)
        self.assertEqual(
            codex_review_lib.format_gates_left(result, None), expected)

    def test_uncalibrated(self):
        result = self._result(None, None, plan_type="free", model="other-model")
        snap_mtime = 1699999000.0
        snap = time.strftime("%a %m-%d %H:%M", time.localtime(snap_mtime))
        reset = time.strftime("%a %m-%d %H:%M", time.localtime(1700000000.0))
        expected = ("codex budget: 82% of the 7-day window used as of "
                    + snap + ", resets " + reset + " — no fitted "
                    "per-gate cost for plan free / model other-model, "
                    "so no gate count")
        self.assertEqual(
            codex_review_lib.format_gates_left(result, snap_mtime), expected)

    def test_no_snapshot_mtime_drops_as_of_clause(self):
        result = self._result(17, 26)
        reset = time.strftime("%a %m-%d %H:%M", time.localtime(1700000000.0))
        result_str = codex_review_lib.format_gates_left(result, None)
        self.assertNotIn("as of", result_str)
        self.assertIn("resets %s" % reset, result_str)

    def test_window_43200_is_30_day(self):
        result = self._result(17, 26, window_minutes=43200)
        result_str = codex_review_lib.format_gates_left(result, None)
        self.assertIn("30-day", result_str)

    def test_none_result_is_empty_string(self):
        self.assertEqual(codex_review_lib.format_gates_left(None, 123.0), "")


def _session_meta_line(cwd, timestamp):
    return {"type": "session_meta",
            "payload": {"id": "test-session", "cwd": cwd,
                        "originator": "codex_exec", "timestamp": timestamp}}


def _turn_context_line(model):
    return {"type": "turn_context", "payload": {"model": model}}


def _token_count_line(input_tokens, cached_input_tokens, rate_limits=None):
    payload = {"type": "token_count",
               "info": {"total_token_usage": {
                   "input_tokens": input_tokens,
                   "cached_input_tokens": cached_input_tokens,
                   "output_tokens": 0}}}
    if rate_limits is not None:
        payload["rate_limits"] = rate_limits
    return {"type": "event_msg", "payload": payload}


def _iso_utc(ts):
    """ISO-8601 UTC string in the exact shape Codex writes -- milliseconds
    (3 digits, not 6), trailing `Z` -- for epoch-seconds `ts`."""
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


class TestRunCost(unittest.TestCase):
    """`run_cost`. TZ is pinned to `Pacific/Honolulu` (UTC-10) in
    THIS class so a naive LOCAL parse of the UTC timestamp -- dropping the
    `.replace("Z", "+00:00")` AS-UTC requirement -- lands 10 HOURS LATE,
    not early.

    That direction matters: every fixture here times the session at `now`
    (or `now - 3600` for the one negative case), and the `since - 5` floor
    only rejects a parse that comes out TOO OLD. A naive local parse comes
    out too NEW instead, so it still clears that floor on every MATCH
    fixture -- those tests cannot tell a correct AS-UTC parse from a naive
    one off by 10 hours in the wrong direction, and would pass unchanged
    under either. Only `test_session_timestamp_too_old_does_not_match`
    actually guards the AS-UTC requirement: its session timestamp is
    `now - 3600`, which a correct parse correctly rejects (too old) and a
    naive local parse would WRONGLY accept (shifted 10 hours into the
    future, past the floor). Do not loosen or remove that one test on the
    theory that the others already cover this class's premise -- they do
    not."""

    def setUp(self):
        self._old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "Pacific/Honolulu"
        time.tzset()

    def tearDown(self):
        if self._old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = self._old_tz
        time.tzset()

    def _cwd_dir(self):
        d = tempfile.mkdtemp(prefix="codex-review-cwd-")
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        return d

    @with_synthetic_calibration
    def test_match_returns_uncached_from_the_last_of_two_lines(self):
        with TempSessionsRoot() as root:
            now = time.time()
            target_cwd = self._cwd_dir()
            path = os.path.join(root, "rollout-match.jsonl")
            _write_jsonl(path, [
                _session_meta_line(target_cwd, _iso_utc(now)),
                _turn_context_line("test-model"),
                # First line's uncached (50,000) must differ from the last's
                # (100,000), or a first-line reader passes this test.
                _token_count_line(50000, 0),
                _token_count_line(
                    200000, 100000,
                    rate_limits={"plan_type": "test-plan",
                                 "primary": {"used_percent": 50.0,
                                             "window_minutes": 10080,
                                             "resets_at": now + 86400}}),
            ])
            _touch_mtime(path, now + 1)

            result = codex_review_lib.run_cost(root, target_cwd, now)

        self.assertIsNotNone(result)
        self.assertEqual(result["uncached_input"], 100000)
        self.assertEqual(result["model"], "test-model")
        self.assertAlmostEqual(result["points"], 100000 / 1e6 * 10.0)
        self.assertEqual(result["plan_type"], "test-plan")

    def test_different_cwd_does_not_match(self):
        with TempSessionsRoot() as root:
            now = time.time()
            target_cwd = self._cwd_dir()
            other_cwd = self._cwd_dir()
            path = os.path.join(root, "rollout-a.jsonl")
            _write_jsonl(path, [
                _session_meta_line(other_cwd, _iso_utc(now)),
                _turn_context_line("gpt-6-astra"),
                _token_count_line(100000, 0),
            ])
            _touch_mtime(path, now + 1)

            result = codex_review_lib.run_cost(root, target_cwd, now)

        self.assertIsNone(result)

    def test_session_timestamp_too_old_does_not_match(self):
        with TempSessionsRoot() as root:
            now = time.time()
            target_cwd = self._cwd_dir()
            path = os.path.join(root, "rollout-a.jsonl")
            _write_jsonl(path, [
                _session_meta_line(target_cwd, _iso_utc(now - 3600)),
                _turn_context_line("gpt-6-astra"),
                _token_count_line(100000, 0),
            ])
            _touch_mtime(path, now + 1)  # passes the mtime filter on its own

            result = codex_review_lib.run_cost(root, target_cwd, now)

        self.assertIsNone(result)

    def test_two_matching_files_returns_none(self):
        with TempSessionsRoot() as root:
            now = time.time()
            target_cwd = self._cwd_dir()
            for name in ("rollout-a.jsonl", "rollout-b.jsonl"):
                path = os.path.join(root, name)
                _write_jsonl(path, [
                    _session_meta_line(target_cwd, _iso_utc(now)),
                    _turn_context_line("gpt-6-astra"),
                    _token_count_line(100000, 0),
                ])
                _touch_mtime(path, now + 1)

            result = codex_review_lib.run_cost(root, target_cwd, now)

        self.assertIsNone(result)

    def test_symlinked_cwd_argument_still_matches(self):
        with TempSessionsRoot() as root:
            now = time.time()
            target_cwd = self._cwd_dir()
            symlink_dir = tempfile.mkdtemp(prefix="codex-review-symlink-")
            self.addCleanup(shutil.rmtree, symlink_dir, ignore_errors=True)
            symlink_path = os.path.join(symlink_dir, "link")
            os.symlink(target_cwd, symlink_path)
            path = os.path.join(root, "rollout-a.jsonl")
            _write_jsonl(path, [
                _session_meta_line(target_cwd, _iso_utc(now)),
                _turn_context_line("gpt-6-astra"),
                _token_count_line(100000, 0),
            ])
            _touch_mtime(path, now + 1)

            result = codex_review_lib.run_cost(root, symlink_path, now)

        self.assertIsNotNone(result)
        self.assertEqual(result["uncached_input"], 100000)

    def test_mtime_before_since_excludes_even_a_fresh_session_meta(self):
        # L13 kill: the mtime filter (`mtime < since`) is a SEPARATE guard
        # from the session_meta timestamp floor exercised above -- a file
        # whose session_meta claims a timestamp at `since` but whose mtime
        # on disk is stale must still be excluded, or a file rewritten or
        # copied after the fact could be attributed to a run it predates.
        with TempSessionsRoot() as root:
            now = time.time()
            target_cwd = self._cwd_dir()
            path = os.path.join(root, "rollout-a.jsonl")
            _write_jsonl(path, [
                _session_meta_line(target_cwd, _iso_utc(now)),
                _turn_context_line("gpt-6-astra"),
                _token_count_line(100000, 0),
            ])
            _touch_mtime(path, now - 100)  # stale mtime, fresh session_meta

            result = codex_review_lib.run_cost(root, target_cwd, now)

        self.assertIsNone(result)

    def test_no_rate_limits_block_in_file_gives_none_points(self):
        with TempSessionsRoot() as root:
            now = time.time()
            target_cwd = self._cwd_dir()
            path = os.path.join(root, "rollout-a.jsonl")
            _write_jsonl(path, [
                _session_meta_line(target_cwd, _iso_utc(now)),
                _turn_context_line("gpt-6-astra"),
                _token_count_line(100000, 0),  # no rate_limits kwarg
            ])
            _touch_mtime(path, now + 1)

            result = codex_review_lib.run_cost(root, target_cwd, now)

        self.assertIsNotNone(result)
        self.assertIsNone(result["points"])
        self.assertEqual(result["uncached_input"], 100000)
        self.assertIsNone(result["plan_type"])


# ---------------------------------------------------------------------------
# The `claude-codex-review` executable.
#
# These run the real executable through `subprocess`, per this repo's rule
# ("Test the executables, not just the lib" -- `usage-watch` shipped 93
# green unit tests and zero coverage of any hook). A fake `codex` **shim**
# on a prepended PATH dir stands in for the real binary; real throwaway git
# repos, built with `subprocess`, stand in for the target checkout.
#
# `--merge-with` (the merge-with tests) has its own fixture helper, `_make_merge_fixture_
# repo`, further down -- a real `origin` remote, not the linear/diverging
# fixtures above, because the merge-target-preparation procedure fetches
# from and merges an actual `origin/<ref>`.
# ---------------------------------------------------------------------------

WRAPPER = os.path.join(HERE, "claude-codex-review")

# A fake `codex`, configured entirely through env vars so one script covers
# every scenario below rather than generating a new file per test:
#
#   CODEX_SHIM_DUMP         required. Where to record what this invocation
#                           received: `{"argv": [...], "cwd": "..."}` as
#                           JSON at that path, and the raw stdin bytes at
#                           `<path>.stdin`. Written UNCONDITIONALLY, before
#                           anything else -- so a test asserting "the shim
#                           was not run" can assert this path does not
#                           exist, rather than that it exists-but-empty.
#   CODEX_SHIM_EXIT         the exit code to return. Default "0".
#   CODEX_SHIM_OUTPUT_MODE  what to write at the `-o` path. Default
#                           "conforming" (a schema- and semantically-valid
#                           report). "none" writes nothing at all.
#   CODEX_SHIM_ROLLOUT      A file path. When set, the shim writes a
#                           rollout-shaped jsonl there: a `session_meta`
#                           line (`cwd = os.getcwd()`, `timestamp` = now
#                           UTC with a trailing Z), a `turn_context` line
#                           (model `CODEX_SHIM_MODEL`, default
#                           "gpt-6-astra"), and one `token_count` line
#                           (`input_tokens=CODEX_SHIM_INPUT` default
#                           200000, `cached_input_tokens=CODEX_SHIM_CACHED`
#                           default 100000, plus a `rate_limits` block
#                           using `CODEX_SHIM_PLAN` (default
#                           "test-plan") and
#                           `CODEX_SHIM_USED` (default 50), window 10080,
#                           `resets_at` = now + 86400). Unset -> no rollout
#                           is written, i.e. today's behaviour exactly.
#   CODEX_SHIM_REPORT_FILE  Task 4. A file path. When set (and the output
#                           mode is not a failure mode: none, malformed,
#                           prose, hang, kill_parent still win), the shim
#                           copies that file's bytes to the `-o` path
#                           instead of the default report.
SHIM_SOURCE = '''#!/usr/bin/env python3
"""Fake `codex`, for claude-codex-review's tests only. See
CODEX_SHIM_DUMP / CODEX_SHIM_EXIT / CODEX_SHIM_OUTPUT_MODE /
CODEX_SHIM_ROLLOUT."""
import datetime
import json
import os
import sys

dump_path = os.environ["CODEX_SHIM_DUMP"]
argv = sys.argv[1:]
import subprocess
try:
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=os.getcwd(),
                          capture_output=True, text=True, check=True
                          ).stdout.strip()
except Exception:
    head = None
read_path = os.environ.get("CODEX_SHIM_READ")
read_text = None
if read_path:
    try:
        with open(os.path.join(os.getcwd(), read_path), "r",
                  encoding="utf-8") as handle:
            read_text = handle.read()
    except OSError:
        read_text = None
with open(dump_path, "w", encoding="utf-8") as handle:
    json.dump({"argv": argv, "cwd": os.getcwd(), "head": head,
               "read": read_text}, handle)

stdin_bytes = sys.stdin.buffer.read()
with open(dump_path + ".stdin", "wb") as handle:
    handle.write(stdin_bytes)

rollout_path = os.environ.get("CODEX_SHIM_ROLLOUT")
if rollout_path:
    now = datetime.datetime.now(tz=datetime.timezone.utc)
    timestamp = now.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (now.microsecond // 1000)
    model = os.environ.get("CODEX_SHIM_MODEL", "gpt-6-astra")
    input_tokens = int(os.environ.get("CODEX_SHIM_INPUT", "200000"))
    cached_tokens = int(os.environ.get("CODEX_SHIM_CACHED", "100000"))
    plan_type = os.environ.get("CODEX_SHIM_PLAN", "test-plan")
    used_percent = float(os.environ.get("CODEX_SHIM_USED") or 50)
    rollout_lines = [
        {"type": "session_meta",
         "payload": {"id": "shim-session", "cwd": os.getcwd(),
                     "originator": "codex_exec", "timestamp": timestamp}},
        {"type": "turn_context", "payload": {"model": model}},
        {"type": "event_msg",
         "payload": {"type": "token_count",
                     "info": {"total_token_usage": {
                         "input_tokens": input_tokens,
                         "cached_input_tokens": cached_tokens,
                         "output_tokens": 0}},
                     "rate_limits": {
                         "plan_type": plan_type,
                         "primary": {"used_percent": used_percent,
                                     "window_minutes": 10080,
                                     "resets_at": now.timestamp() + 86400}}}},
    ]
    rollout_dir = os.path.dirname(rollout_path)
    if rollout_dir:
        os.makedirs(rollout_dir, exist_ok=True)
    with open(rollout_path, "w", encoding="utf-8") as handle:
        for rollout_line in rollout_lines:
            handle.write(json.dumps(rollout_line) + "\\n")

output_path = None
for i, tok in enumerate(argv):
    if tok == "-o" and i + 1 < len(argv):
        output_path = argv[i + 1]
        break

REPORTS = {
    "conforming": {
        "recommendation": "APPROVE",
        "executed": {"ran": True, "commands": ["python3 tests/test_x.py"],
                     "why_not": ""},
        "findings": [],
    },
    "missing_field": {
        "recommendation": "APPROVE",
        "executed": {"ran": True, "commands": ["x"], "why_not": ""},
    },
    "undeclared_key": {
        "recommendation": "APPROVE",
        "executed": {"ran": True, "commands": ["x"], "why_not": ""},
        "findings": [],
        "SURPRISE": 1,
    },
    "empty_execution": {
        "recommendation": "APPROVE",
        "executed": {"ran": False, "commands": [], "why_not": ""},
        "findings": [],
    },
    "approve_with_blocking": {
        "recommendation": "APPROVE",
        "executed": {"ran": True, "commands": ["python3 t.py"], "why_not": ""},
        "findings": [{"where": "x", "defect": "y", "consequence": "z",
                       "severity": "BLOCKING", "evidence": "e"}],
    },
    "blank_command": {
        "recommendation": "APPROVE",
        "executed": {"ran": True, "commands": ["   "], "why_not": ""},
        "findings": [],
    },
    "legit_not_ran": {
        "recommendation": "COMMENT",
        "executed": {"ran": False, "commands": [],
                     "why_not": "no test suite in this repo"},
        "findings": [{"where": "x", "defect": "y", "consequence": "z",
                       "severity": "IMPORTANT", "evidence": "e"}],
    },
}

mode = os.environ.get("CODEX_SHIM_OUTPUT_MODE", "conforming")

if mode == "kill_parent":
    # SIGKILL runs no exit handlers, so the wrapper's stdout buffer is
    # lost: only what it flushed before dispatch reaches the pipe.
    import signal
    os.kill(os.getppid(), signal.SIGKILL)
    sys.exit(0)
elif mode == "hang":
    import time
    time.sleep(30)
elif mode == "none":
    pass
elif mode == "malformed":
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write("{not valid json,,,")
elif mode == "prose":
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(
                "I reviewed the change and it looks solid overall. The "
                "error handling could be tightened in a couple of places "
                "but nothing here is blocking.")
elif os.environ.get("CODEX_SHIM_REPORT_FILE"):
    if output_path:
        with open(os.environ["CODEX_SHIM_REPORT_FILE"], "rb") as src:
            report_bytes = src.read()
        with open(output_path, "wb") as handle:
            handle.write(report_bytes)
elif mode in REPORTS:
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(REPORTS[mode], handle)
else:
    if output_path:
        with open(output_path, "w", encoding="utf-8") as handle:
            json.dump(REPORTS["conforming"], handle)

shim_stderr = os.environ.get("CODEX_SHIM_STDERR")
if shim_stderr:
    sys.stderr.write(shim_stderr + "\\n")

sys.exit(int(os.environ.get("CODEX_SHIM_EXIT", "0")))
'''


def _git(*args, cwd):
    # Returns the CompletedProcess so a caller can read stdout; every
    # existing caller ignores it, which is why returning it is safe.
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True)


def _write_agents_md(root):
    """Every fixture repo carries a committed AGENTS.md: the wrapper refuses
    a review without one, so a fixture lacking it would test only that
    refusal."""
    with open(os.path.join(root, "AGENTS.md"), "w", encoding="utf-8") as handle:
        handle.write("# house rules\n")


def _make_fixture_repo(root, with_branch_commit=True):
    """A throwaway git repo with a tag `base` on the first commit and,
    unless `with_branch_commit` is False, one additional commit on top --
    the "branch work" `git diff base...HEAD` would show. With it False, the
    branch is identical to its base: an empty-diff fixture."""
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    _write_agents_md(root)
    with open(os.path.join(root, "f.txt"), "w", encoding="utf-8") as handle:
        handle.write("base\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "base", cwd=root)
    _git("tag", "base", cwd=root)
    if with_branch_commit:
        with open(os.path.join(root, "f.txt"), "w", encoding="utf-8") as handle:
            handle.write("base\nbranch change\n")
        _git("add", "-A", cwd=root)
        _git("commit", "-q", "-m", "branch work", cwd=root)
    return root


def _make_diverging_fixture_repo(root):
    """A repo where `base` and the reviewed branch (`review`, checked out
    as HEAD) have genuinely DIVERGED from a common ancestor, rather than
    `base` being a strict ancestor of HEAD.

    This matters specifically for the diff-equality test: `diff <base>...HEAD`
    (three-dot) and `diff <base>..HEAD` (two-dot) are byte-IDENTICAL on a
    purely linear fixture (`_make_fixture_repo`'s shape), because
    `merge-base(base, HEAD)` collapses to `base` itself when base is a
    strict ancestor -- so a regression from three-dot to two-dot would
    pass that comparison undetected. Here `base` gains its own commit
    after the fork, so the two forms genuinely differ and a comparison
    against the three-dot form actually exercises which one the wrapper
    used.
    """
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    _write_agents_md(root)
    with open(os.path.join(root, "shared.txt"), "w", encoding="utf-8") as handle:
        handle.write("start\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "common ancestor", cwd=root)

    _git("branch", "base", cwd=root)
    _git("checkout", "-q", "base", cwd=root)
    with open(os.path.join(root, "shared.txt"), "w", encoding="utf-8") as handle:
        handle.write("start\nmoved on base independently\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "base moved on independently", cwd=root)

    _git("checkout", "-q", "-b", "review", "HEAD~1", cwd=root)
    with open(os.path.join(root, "reviewed.txt"), "w", encoding="utf-8") as handle:
        handle.write("branch work\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "reviewed branch work", cwd=root)
    # HEAD is now "review" -- what claude-codex-review diffs against --base.
    return root


def _make_merge_fixture_repo(root, upstream_moves=True, conflict=False):
    """A throwaway git repo wired to a real `origin` remote, for
    `--merge-with` fixtures -- the merge-target-preparation procedure does
    a real `git fetch origin <ref>` and `git merge origin/<ref>`, so the
    fixture needs an actual remote, not just tags on a single repo the way
    `_make_fixture_repo` and `_make_diverging_fixture_repo` do.

    Builds three things under `root`: `remote/` (a bare repo acting as
    `origin`), `seed/` (a scratch working copy used to push commits to
    it), and `repo/` (the actual `--repo` checkout, cloned from `remote`,
    carrying one LOCAL, unpushed branch commit on top). Returns `repo/`.

    `upstream_moves=True` (the default) pushes a second commit to
    `remote`'s `main` from `seed` AFTER `repo` is cloned -- so `repo`'s
    local `origin/main` is stale until the wrapper's own `git fetch`
    advances it, and `merge-base(origin/main, HEAD)` sits at the ORIGINAL
    commit, strictly behind the post-fetch `origin/main`. This is the fixture for
    the clean-merge and branch-point tests: an upstream commit exists precisely so the
    upstream block can be asserted non-empty, non-vacuously.
    `upstream_moves=False` leaves `remote` untouched after the clone, so
    nothing lands upstream and `merge-base(origin/main, HEAD)` collapses
    to `origin/main` itself -- the empty-delta fixture.

    `conflict=True` makes the upstream commit and the branch commit touch
    the SAME line of the SAME file (`shared.txt`), so merging
    `origin/main` into `repo`'s branch conflicts -- the conflict fixture. The
    non-conflict branch commit touches a different file (`branch.txt`)
    entirely, so the clean merge is genuinely clean rather than accidentally
    non-conflicting.
    """
    remote = os.path.join(root, "remote")
    seed = os.path.join(root, "seed")
    repo = os.path.join(root, "repo")

    _git("init", "-q", "--bare", remote, cwd=root)
    # The BARE remote needs its HEAD pinned too, not just the seed below.
    # `git clone` picks the branch to check out from the remote's HEAD, so
    # a remote left on an ambient `init.defaultBranch` of `master` hands
    # back a clone with NO checkout and an unborn `master` -- the branch
    # commit then has no parent, shares no history with `origin/main`, and
    # `git merge-base origin/main HEAD` exits non-zero with EMPTY stderr.
    # That surfaces as a merge-prep failure 40 lines away from its cause.
    # Green on any machine configured for `main` and red on CI, which is
    # exactly the ambient-state dependency this repo's CLAUDE.md forbids.
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=remote)

    _git("init", "-q", seed, cwd=root)
    # Explicit, not relying on `init.defaultBranch` -- that config varies
    # by git version/install, and a fixture whose branch name depends on
    # ambient config is not hermetic.
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=seed)
    _git("config", "user.email", "t@example.com", cwd=seed)
    _git("config", "user.name", "Test", cwd=seed)
    _write_agents_md(seed)
    with open(os.path.join(seed, "shared.txt"), "w", encoding="utf-8") as handle:
        handle.write("start\n")
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", "common ancestor", cwd=seed)
    _git("push", "-q", remote, "main", cwd=seed)

    _git("clone", "-q", remote, repo, cwd=root)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    # Fail HERE if the clone did not land on `main`, rather than letting a
    # detached or unborn HEAD surface later as an opaque `merge-base`
    # failure with empty stderr. A fixture that builds the wrong shape
    # must say so at the point it built it.
    head = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo).stdout.strip()
    assert head == "main", (
        "merge fixture clone is on %r, not 'main' -- the bare remote's HEAD "
        "is not pinned, so this fixture is reading ambient "
        "init.defaultBranch" % head)

    if conflict:
        with open(os.path.join(repo, "shared.txt"), "w", encoding="utf-8") as handle:
            handle.write("start\nBRANCH VERSION\n")
    else:
        with open(os.path.join(repo, "branch.txt"), "w", encoding="utf-8") as handle:
            handle.write("branch work\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "branch work", cwd=repo)

    if upstream_moves:
        with open(os.path.join(seed, "shared.txt"), "w", encoding="utf-8") as handle:
            if conflict:
                handle.write("start\nUPSTREAM VERSION\n")
            else:
                handle.write("start\nupstream change\n")
        _git("add", "-A", cwd=seed)
        _git("commit", "-q", "-m", "upstream work", cwd=seed)
        _git("push", "-q", remote, "main", cwd=seed)

    return repo


def _write_shim(shim_dir):
    path = os.path.join(shim_dir, "codex")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(SHIM_SOURCE)
    os.chmod(path, 0o755)
    return path


def _home_with_auth(root):
    """A sandbox HOME carrying a readable ~/.codex/auth.json, so preflight's
    auth check passes without touching the real machine's credentials."""
    home = os.path.join(root, "home")
    os.makedirs(os.path.join(home, ".codex"), exist_ok=True)
    with open(os.path.join(home, ".codex", "auth.json"), "w",
              encoding="utf-8") as handle:
        handle.write('{"ok": true}\n')
    return home


def _config_path_for(home):
    """Where a test's toolkit config lives: beside the sandbox HOME, so it
    is absent unless the test writes it."""
    return os.path.join(os.path.dirname(home), "toolkit-config.json")


def _base_env(shim_dir, home, extra=None):
    """PATH carries the shim ahead of the real PATH (so `git` is still
    reachable), HOME points at the sandbox built by `_home_with_auth`, and
    the toolkit config points at a file inside the sandbox (absent unless a
    test writes it) so no test depends on what happens to be installed on
    this machine."""
    env = dict(os.environ)
    env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
    env["HOME"] = home
    env["CLAUDE_TOOLKIT_CONFIG"] = _config_path_for(home)
    if extra:
        env.update(extra)
    return env


def _run_wrapper(args, env, timeout=30):
    return subprocess.run(
        [sys.executable, WRAPPER] + list(args), env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout)


def _make_wrapper_copy(root, brief_content):
    """A throwaway copy of the toolkit paths the wrapper imports, laid out
    like the repository: `copy/tools/codex-review/{claude-codex-review,
    codex_review_lib.py,[review-brief.md]}`, `copy/tools/build-lens/
    {review_lib.py,merge-review-report.schema.json}` and
    `copy/lib/toolkit_config.py`. `review-brief.md` can then be omitted or
    emptied WITHOUT ever touching the real, committed file -- there is no
    `--brief` flag to seam this.

    `brief_content=None` omits `review-brief.md` from the copy entirely
    (the "missing" case); any string writes it verbatim (the "empty /
    whitespace" case). The copy's `HERE` resolves to its own
    `tools/codex-review` because it is a genuine file, not a symlink --
    `realpath(__file__)` lands there rather than bouncing back to the real
    repo location the way the symlink test deliberately does. The copy
    carries no other brief and no `tools/merge-brief`.
    """
    copy_root = os.path.join(root, "copy")
    dest_dir = os.path.join(copy_root, "tools", "codex-review")
    lens_dir = os.path.join(copy_root, "tools", "build-lens")
    lib_dir = os.path.join(copy_root, "lib")
    for directory in (dest_dir, lens_dir, lib_dir):
        os.makedirs(directory)
    copied_exe = os.path.join(dest_dir, "claude-codex-review")
    shutil.copy2(WRAPPER, copied_exe)
    shutil.copy2(os.path.join(HERE, "codex_review_lib.py"),
                 os.path.join(dest_dir, "codex_review_lib.py"))
    real_lens = os.path.join(os.path.dirname(HERE), "build-lens")
    for name in ("review_lib.py", "merge-review-report.schema.json"):
        shutil.copy2(os.path.join(real_lens, name), os.path.join(lens_dir, name))
    shutil.copy2(os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib",
                              "toolkit_config.py"),
                 os.path.join(lib_dir, "toolkit_config.py"))
    if brief_content is not None:
        with open(os.path.join(dest_dir, "review-brief.md"), "w",
                  encoding="utf-8") as handle:
            handle.write(brief_content)
    os.chmod(copied_exe, 0o755)
    return copied_exe


def _expected_diff(repo, base="base"):
    """The same `git diff <base>...HEAD` command the wrapper itself runs,
    computed independently here so the diff test has something to compare against
    that is not the wrapper's own function."""
    result = subprocess.run(
        ["git", "-C", repo, "diff", "%s...HEAD" % base],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout


class CodexReviewExecutableTestCase(unittest.TestCase):
    """Shared fixture machinery: a tempdir holding a fixture repo, a shim
    dir, a sandbox HOME with a valid auth.json, and an empty sessions-root
    so the meter read is hermetic by default."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="codex-review-exe-")
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name

        self.repo = os.path.join(self.root, "repo")
        os.makedirs(self.repo)
        _make_fixture_repo(self.repo, with_branch_commit=True)

        self.shim_dir = os.path.join(self.root, "shim")
        os.makedirs(self.shim_dir)
        _write_shim(self.shim_dir)

        self.home = _home_with_auth(self.root)
        self.sessions_root = os.path.join(self.root, "sessions")  # absent

        self.output_path = os.path.join(self.root, "report.json")
        self.dump_path = os.path.join(self.root, "codex-dump.json")

    def env(self, output_mode=None, exit_code=None, extra=None):
        e = {"CODEX_SHIM_DUMP": self.dump_path}
        if output_mode is not None:
            e["CODEX_SHIM_OUTPUT_MODE"] = output_mode
        if exit_code is not None:
            e["CODEX_SHIM_EXIT"] = str(exit_code)
        if extra:
            e.update(extra)
        return _base_env(self.shim_dir, self.home, extra=e)

    def base_args(self, extra=None):
        args = ["--base", "base", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        if extra:
            args = args + list(extra)
        return args

    def shim_was_run(self):
        return os.path.isfile(self.dump_path)

    def shim_dump(self):
        with open(self.dump_path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def doc_args(self, flag, paths):
        """the args for a document-gate dispatch -- `flag` is
        `--plan-review` or `--design-review`, `paths` the file(s) to pass
        it. Never carries `--base`; a document gate takes none."""
        return ([flag] + list(paths) +
                ["--repo", self.repo, "-o", self.output_path,
                 "--sessions-root", self.sessions_root])

    def shim_rollout_path(self, name="rollout-shim.jsonl"):
        """Where the shim writes its rollout fixture when
        `CODEX_SHIM_ROLLOUT` is set -- inside `self.sessions_root`, which
        does not need to exist beforehand: the shim creates it."""
        return os.path.join(self.sessions_root, name)

    def _byte_index(self, haystack, needle):
        """The byte offset of `needle` (a str, UTF-8 encoded) inside
        `haystack` (bytes) -- used for the ordering assertion, which
        is explicitly about byte offset, not just "earlier in the
        decoded text."""
        idx = haystack.find(needle.encode("utf-8"))
        self.assertGreaterEqual(idx, 0, "%r not found in stdout" % needle)
        return idx


class TestExecutableRealpath(CodexReviewExecutableTestCase):
    """`realpath`, not `abspath`, for anything `__file__`-relative --
    this runs through a `~/.local/bin` symlink in the real install, and
    `abspath` does not resolve a symlink: it lands on the symlink's own
    directory and both the `codex_review_lib` import and the
    `review-brief.md` read fail at runtime while passing every test that
    invokes the script at its real path."""

    def test_reached_through_a_symlink_still_resolves_the_brief_and_lib(self):
        bin_dir = os.path.join(self.root, "bin")
        os.makedirs(bin_dir)
        link = os.path.join(bin_dir, "claude-codex-review")
        os.symlink(WRAPPER, link)

        result = subprocess.run(
            [sys.executable, link] + self.base_args(),
            env=self.env(output_mode="conforming"),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())


class TestExecutableCodexAbsent(CodexReviewExecutableTestCase):
    """`codex` absent from PATH -> exit 4, message says Codex is
    unavailable and names the binary."""

    def test_missing_codex_binary_exits_four(self):
        env = dict(os.environ)
        env["PATH"] = "/usr/bin:/bin"  # no shim dir on PATH at all
        env["HOME"] = self.home
        env["CLAUDE_TOOLKIT_CONFIG"] = _config_path_for(self.home)

        result = _run_wrapper(self.base_args(), env=env)

        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(b"Codex is unavailable", result.stderr)
        self.assertIn(b"codex", result.stderr.lower())
        self.assertFalse(self.shim_was_run())


class TestExecutableAuthMissing(CodexReviewExecutableTestCase):
    """Auth file missing (HOME points at a sandbox with no
    ~/.codex/auth.json) -> exit 4, message says Codex is unavailable."""

    def test_missing_auth_file_exits_four(self):
        empty_home = os.path.join(self.root, "empty-home")
        os.makedirs(empty_home)
        env = self.env(output_mode="conforming")
        env["HOME"] = empty_home

        result = _run_wrapper(self.base_args(), env=env)

        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertIn(b"Codex is unavailable", result.stderr)
        self.assertIn(b"auth", result.stderr.lower())
        self.assertFalse(self.shim_was_run())


class TestExecutableBriefMissingOrEmpty(CodexReviewExecutableTestCase):
    """`review-brief.md` missing, or present but empty/whitespace ->
    non-zero, message names the file. Two separate tests -- empty is the
    one a careless `touch` during setup produces, and it is a different
    code path than "the file does not exist at all."

    Both run a throwaway COPY of the executable (`_make_wrapper_copy`)
    rather than touching the real, committed `review-brief.md` -- that
    file is explicitly out of scope to create, rewrite, or edit, and even a
    rename-then-restore-in-cleanup risks leaving it missing if the test
    process is killed mid-run.
    """

    def test_brief_file_missing_entirely(self):
        copied_exe = _make_wrapper_copy(self.root, brief_content=None)

        result = subprocess.run(
            [sys.executable, copied_exe] + self.base_args(),
            env=self.env(output_mode="conforming"), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"review-brief.md", result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_brief_file_present_but_whitespace_only(self):
        copied_exe = _make_wrapper_copy(self.root, brief_content="   \n\t\n   ")

        result = subprocess.run(
            [sys.executable, copied_exe] + self.base_args(),
            env=self.env(output_mode="conforming"), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"review-brief.md", result.stderr)
        self.assertFalse(self.shim_was_run())


class TestExecutableMeterUnreadableStillDispatches(CodexReviewExecutableTestCase):
    """Meter unreadable but `codex` present -> reaches dispatch and
    exits 0. `--sessions-root` is the seam: it points at a directory that
    does not exist, so `snapshot_meter` returns `(None, None)` and
    `format_headroom` prints the "unreadable" string -- and the run must
    proceed past that rather than treating it as fatal."""

    def test_unreadable_meter_does_not_block_the_dispatch(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="conforming"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"unreadable", result.stdout.lower())
        self.assertTrue(self.shim_was_run())


class TestExecutableArgvAssertions(CodexReviewExecutableTestCase):
    """The shim asserts the argv it received contains both `-c` pairs,
    `-C` and the pinned `-m`, and that the LAST argv element is the brief file's exact
    contents -- end-to-end enforcement of the pure builder, not just a
    unit test of `build_argv` in isolation.

    Compared against the SAME plain `.read()` of `review-brief.md` that the
    executable itself performs -- no stripping on either side, per the
    architect's decision, so there is exactly one definition of "the brief
    file's exact contents." """

    def test_dispatched_argv_carries_c_pairs_c_and_the_brief_as_the_last_element(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)

        dump = self.shim_dump()
        argv = dump["argv"]

        self.assertIn("-C", argv)
        self.assertEqual(argv.count("-c"), 2)
        # `--base` alone is the Code gate: the Code gate's model, not the Merge gate's.
        _assert_pinned_model_and_effort(self, argv, PINNED_CODE_MODEL)
        self.assertIn(b"codex gate: code review on gpt-5.6-terra (default)",
                      result.stdout)

        brief_path = os.path.join(os.path.dirname(WRAPPER), "review-brief.md")
        with open(brief_path, "r", encoding="utf-8") as handle:
            expected_brief = handle.read()
        self.assertEqual(argv[-1], expected_brief)


class TestExecutableStdinIsTheDiffAlone(CodexReviewExecutableTestCase):
    """Without `--merge-with`, the shim's stdin is byte-identical to
    `git -C <target> diff <base>...HEAD` -- one diff, no section markers.
    This is the blinding contract made testable: with `--base` gone from
    the argv entirely, nothing but this test proves the reviewer sees the
    mechanical diff and only the mechanical diff. Both directions: stdin
    equals the diff, AND stdin contains no text from the brief.

    Uses `_make_diverging_fixture_repo`, NOT the shared linear fixture --
    on a linear fixture (base is a strict ancestor of HEAD), `diff
    base...HEAD` and `diff base..HEAD` are byte-identical, so comparing
    against the three-dot form would pass even if the wrapper had
    regressed to two-dot. Divergence is what makes this comparison
    actually distinguish the two.
    """

    def setUp(self):
        super().setUp()
        diverging_repo = os.path.join(self.root, "repo-diverging")
        os.makedirs(diverging_repo)
        _make_diverging_fixture_repo(diverging_repo)
        self.repo = diverging_repo

    def test_stdin_equals_the_mechanical_diff_and_carries_no_brief_text(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)

        expected_three_dot = _expected_diff(self.repo, base="base")
        two_dot = subprocess.run(
            ["git", "-C", self.repo, "diff", "base..HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        self.assertTrue(expected_three_dot,
                         "fixture produced an empty three-dot diff; test is vacuous")
        self.assertNotEqual(
            expected_three_dot, two_dot,
            "fixture's two-dot and three-dot diffs are identical; this "
            "fixture cannot distinguish the two forms and the test below "
            "proves nothing")

        with open(self.dump_path + ".stdin", "rb") as handle:
            actual_stdin = handle.read()
        self.assertEqual(actual_stdin, expected_three_dot)

        brief_path = os.path.join(os.path.dirname(WRAPPER), "review-brief.md")
        with open(brief_path, "rb") as handle:
            brief_bytes = handle.read()
        # A real sentence from the brief, not the whole file -- the brief
        # is prose and near-certain not to appear verbatim inside a git
        # diff of this fixture's tiny text changes.
        sentinel = brief_bytes.strip().splitlines()[0]
        self.assertNotIn(sentinel, actual_stdin)


class TestExecutableEmptyDiff(CodexReviewExecutableTestCase):
    """Empty diff -> wrapper exits non-zero and never dispatches.
    Fixture: a branch identical to its base. Assert the shim was NOT run --
    a wrapper that dispatches and then complains has already spent the
    review."""

    def setUp(self):
        super().setUp()
        # Overwrite the default fixture (which has a branch commit) with
        # one whose branch equals its base.
        empty_repo = os.path.join(self.root, "repo-empty")
        os.makedirs(empty_repo)
        _make_fixture_repo(empty_repo, with_branch_commit=False)
        self.repo = empty_repo

    def test_empty_diff_is_a_loud_failure_and_the_shim_never_runs(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="conforming"))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.shim_was_run())


class TestExecutableUnresolvableBase(CodexReviewExecutableTestCase):
    """Unresolvable `--base` -> non-zero, git's stderr surfaced, shim
    not run. A separate guard from the empty-diff case -- one fix (e.g. "treat a git
    failure as an empty diff") could mask the other."""

    def test_bad_base_ref_is_a_loud_failure_with_gits_stderr_surfaced(self):
        args = ["--base", "definitely-not-a-real-ref", "--repo", self.repo,
                "-o", self.output_path, "--sessions-root", self.sessions_root]

        result = _run_wrapper(args, env=self.env(output_mode="conforming"))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.shim_was_run())


class TestExecutableWorkingDirectory(CodexReviewExecutableTestCase):
    """The shim records its own working directory. The right place for
    the Code gate to dispatch is now a detached snapshot of HEAD, not
    `--repo` itself ): a wrapper that builds a correct argv and
    dispatches it from the wrong place passes the argv test and fails this."""

    def test_code_gate_dispatches_in_a_snapshot_of_head(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)

        dump = self.shim_dump()
        self.assertNotEqual(os.path.realpath(dump["cwd"]),
                            os.path.realpath(self.repo))
        self.assertEqual(
            dump["head"],
            _git("rev-parse", "HEAD", cwd=self.repo).stdout.strip())
        c_index = dump["argv"].index("-C")
        # realpath: macOS reports a temp dir as /private/var/... from
        # getcwd() but the wrapper passes the /var/... spelling.
        self.assertEqual(os.path.realpath(dump["argv"][c_index + 1]),
                         os.path.realpath(dump["cwd"]))


class TestCodeReviewRunsOnASnapshot(CodexReviewExecutableTestCase):
    """The Code gate reviews a detached snapshot of HEAD, never the
    live checkout, records what it reviewed outside the model's report, and
    warns when --repo holds uncommitted work the review cannot see."""

    def _read_env(self, path):
        return self.env(output_mode="conforming",
                        extra={"CODEX_SHIM_READ": path})

    def _worktree_count(self):
        listing = subprocess.run(
            ["git", "-C", self.repo, "worktree", "list", "--porcelain"],
            capture_output=True, text=True).stdout
        return listing.count("worktree ")

    def test_uncommitted_edit_is_not_visible_to_the_reviewer(self):
        with open(os.path.join(self.repo, "f.txt"), "w", encoding="utf-8") as h:
            h.write("DIRTY\n")
        result = _run_wrapper(self.base_args(), env=self._read_env("f.txt"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.shim_dump()["read"], "base\nbranch change\n")

    def test_untracked_file_is_not_visible(self):
        with open(os.path.join(self.repo, "scratch.txt"), "w",
                  encoding="utf-8") as h:
            h.write("untracked\n")
        result = _run_wrapper(self.base_args(), env=self._read_env("scratch.txt"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(self.shim_dump()["read"])

    def test_dirty_tree_warns_with_the_count(self):
        with open(os.path.join(self.repo, "f.txt"), "w", encoding="utf-8") as h:
            h.write("DIRTY\n")
        with open(os.path.join(self.repo, "scratch.txt"), "w",
                  encoding="utf-8") as h:
            h.write("untracked\n")
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"2 uncommitted path(s)", result.stderr)
        self.assertIn(b"NOT in this review", result.stderr)

    def test_dirty_warning_ignores_show_untracked_config(self):
        _git("config", "status.showUntrackedFiles", "no", cwd=self.repo)
        with open(os.path.join(self.repo, "scratch.txt"), "w",
                  encoding="utf-8") as h:
            h.write("untracked\n")
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"1 uncommitted path(s)", result.stderr)

    def test_clean_tree_does_not_warn(self):
        """Positive control for the two warning tests above."""
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"uncommitted path", result.stderr)

    def test_snapshot_line_names_head_tree_base_and_merge_base(self):
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        head, tree, base, base_sha, merge_base = _snapshot_line_fields(
            self, result.stdout)
        git = lambda *a: _git(*a, cwd=self.repo).stdout.strip()
        self.assertEqual(head, git("rev-parse", "HEAD"))
        self.assertEqual(tree, git("rev-parse", "HEAD^{tree}"))
        self.assertEqual(base, "base")
        self.assertEqual(base_sha, git("rev-parse", "base^{commit}"))
        self.assertEqual(merge_base, git("merge-base", "base", "HEAD"))
        self.assertLess(result.stdout.index(b"reviewed snapshot: "),
                        result.stdout.index(b"codex gate:"))

    def test_snapshot_removed_after_success(self):
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self._worktree_count(), 1)

    def test_snapshot_removed_after_a_dispatch_failure(self):
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming",
                                           exit_code=3))
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.shim_was_run())
        self.assertEqual(self._worktree_count(), 1)

    def test_snapshot_removed_after_an_empty_diff_refusal(self):
        empty_repo = os.path.join(self.root, "repo-empty")
        os.makedirs(empty_repo)
        _make_fixture_repo(empty_repo, with_branch_commit=False)
        self.repo = empty_repo
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"is empty", result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertEqual(self._worktree_count(), 1)

    def test_an_uncommitted_agents_md_is_refused_in_the_snapshot(self):
        _git("rm", "-q", "AGENTS.md", cwd=self.repo)
        _git("commit", "-q", "-m", "drop committed AGENTS.md", cwd=self.repo)
        with open(os.path.join(self.repo, "AGENTS.md"), "w",
                  encoding="utf-8") as h:
            h.write("# house rules, never committed\n")
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"in the reviewed snapshot", result.stderr)
        self.assertEqual(self._worktree_count(), 1)


class TestExecutableShimNonZero(CodexReviewExecutableTestCase):
    """Shim exits non-zero -> wrapper exits non-zero."""

    def test_codex_failure_propagates(self):
        result = _run_wrapper(
            self.base_args(),
            env=self.env(output_mode="conforming", exit_code=3))

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.shim_was_run())


class TestExecutableNoOutputFile(CodexReviewExecutableTestCase):
    """Shim exits 0 but writes no `-o` file -> wrapper exits
    non-zero."""

    def test_missing_report_file_is_a_loud_failure(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="none"))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.exists(self.output_path))


class TestExecutableStaleReportNotMistakenForFresh(CodexReviewExecutableTestCase):
    """The Code Review gate's finding: before the fix, step 7 only checked
    `os.path.isfile(output_path)` after `codex` exited 0 -- it never
    established that THIS dispatch created the file. A report already
    sitting at `-o` from an earlier run, combined with a second dispatch
    that writes nothing, made every downstream check (exists, parses,
    schema-valid, semantically valid) pass against the PREVIOUS run's
    report, with the wrapper printing a clean finding count and exiting 0.
    A review gate passed without reviewing the current diff.

    This passed as exit 0 before the fix -- confirmed by reverting the
    fix (removing the pre-dispatch unlink) and re-running: the first run's
    conforming report is what the second run's checks validate against,
    even though the second shim writes nothing.

    The missing-output test above does NOT catch this: it uses `self.output_path`, a fresh
    temp path unique to that test's `setUp`, so there is never a
    pre-existing file for the bug to exploit. The bug only shows up when
    two dispatches share the SAME `-o` path, which is exactly what running
    the same wrapper twice against the same report location does.
    """

    def test_a_stale_report_does_not_survive_a_run_that_writes_nothing(self):
        first = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(os.path.isfile(self.output_path))

        second = _run_wrapper(self.base_args(),
                               env=self.env(output_mode="none"))

        self.assertNotEqual(second.returncode, 0, second.stdout + second.stderr)

    def test_positive_control_a_second_run_that_writes_a_real_report_still_passes(self):
        """Without this, a fix that simply refuses to dispatch whenever a
        file is already at `output_path` would pass the test above for the
        wrong reason -- it would also refuse a second run that legitimately
        writes a fresh, valid report at a reused path."""
        first = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(os.path.isfile(self.output_path))

        second = _run_wrapper(self.base_args(),
                               env=self.env(output_mode="conforming"))

        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)


class TestExecutableMalformedJson(CodexReviewExecutableTestCase):
    """Shim writes malformed JSON -> wrapper exits non-zero."""

    def test_malformed_json_report_is_a_loud_failure(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="malformed"))

        self.assertNotEqual(result.returncode, 0)


class TestExecutableProse(CodexReviewExecutableTestCase):
    """Shim writes PROSE to the `-o` file and exits 0 -> wrapper
    exits non-zero. NOT a duplicate of the malformed-JSON test: prose is not "malformed
    JSON", it is a plausible-looking human report -- and it is exactly
    what the dead `codex exec review` form actually produced (measured
    exit 0, `-o` file present, contents prose). the malformed-JSON test exercises
    a JSON syntax error; this exercises well-formed English text that
    simply isn't JSON at all."""

    def test_prose_report_is_a_loud_failure(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="prose"))

        self.assertNotEqual(result.returncode, 0)


class TestExecutableMissingRequiredField(CodexReviewExecutableTestCase):
    """Shim writes valid JSON missing a required field ("findings")
    -> wrapper exits non-zero."""

    def test_report_missing_a_required_field_is_a_loud_failure(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="missing_field"))

        self.assertNotEqual(result.returncode, 0)


class TestExecutableUndeclaredKey(CodexReviewExecutableTestCase):
    """Shim writes valid JSON with an undeclared top-level key ->
    wrapper exits non-zero. The end-to-end half of the schema check; fails without
    it since `additionalProperties: false` would otherwise be silently
    ignored."""

    def test_report_with_an_undeclared_key_is_a_loud_failure(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="undeclared_key"))

        self.assertNotEqual(result.returncode, 0)


class TestExecutableSemanticallyEmptyReport(CodexReviewExecutableTestCase):
    """Shim writes `ran:false, commands:[], findings:[]` -- the
    fixture from the plan verbatim, schema-valid ON PURPOSE -- and the
    wrapper must exit non-zero. A structurally valid report can still
    describe a review that never happened."""

    def test_ran_false_with_no_why_not_and_no_findings_is_a_loud_failure(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="empty_execution"))

        self.assertNotEqual(result.returncode, 0)


class TestExecutableLegitimateNonExecution(CodexReviewExecutableTestCase):
    """Shim writes `ran:false` with a REAL `why_not` and one real
    finding -> wrapper exits 0 and prints the explanation. Guards the
    over-correction: some repos have no suite, and that is a
    legitimate result, not a broken gate."""

    def test_ran_false_with_a_real_why_not_and_a_finding_exits_zero(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="legit_not_ran"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"no test suite in this repo", result.stdout)


class TestExecutableHelpNeverReadsStdin(CodexReviewExecutableTestCase):
    """`--help` prints and exits 0 WITHOUT reading stdin.

    There is no `timeout` binary on this machine (`timeout 1 true` -> exit
    127, measured), so a test built around wrapping the invocation in one
    would silently do nothing or hang the whole suite. This uses
    `subprocess.run(..., timeout=...)` directly and feeds stdin a pipe
    whose write end is never written and stays open for the duration of the
    call -- if `--help` ever touches stdin, this blocks until the timeout
    fires rather than returning instantly either way."""

    def test_help_exits_zero_without_touching_stdin(self):
        read_fd, write_fd = os.pipe()
        try:
            with os.fdopen(read_fd, "rb") as stdin_handle:
                result = subprocess.run(
                    [sys.executable, WRAPPER, "--help"],
                    stdin=stdin_handle, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, timeout=5)
        except subprocess.TimeoutExpired:
            self.fail("--help blocked reading an open, unwritten stdin; "
                      "arguments must be parsed before any stdin access")
        finally:
            os.close(write_fd)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"--merge-with", result.stdout)


class TestExecutableAgentsMdRequired(CodexReviewExecutableTestCase):
    """A Codex review of a repo with no AGENTS.md is REFUSED, not warned
    about the house rules reach Codex only through
    that file. Every fixture commits one (`_write_agents_md`), so the
    refusal cases remove or blank it; the conforming case is the control
    that stops "always refuse" passing as the fix."""

    def _assert_refused(self, result):
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"no AGENTS.md", result.stderr)

    def test_refuses_when_agents_md_is_absent(self):
        os.remove(os.path.join(self.repo, "AGENTS.md"))
        self._assert_refused(
            _run_wrapper(self.base_args(), env=self.env(output_mode="conforming")))

    def test_refuses_when_agents_md_is_blank(self):
        with open(os.path.join(self.repo, "AGENTS.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("  \n\n")
        self._assert_refused(
            _run_wrapper(self.base_args(), env=self.env(output_mode="conforming")))

    def test_refuses_a_document_gate_too(self):
        os.remove(os.path.join(self.repo, "AGENTS.md"))
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n" + _PLAN_CLASSIFICATION_SUFFIX)
        self._assert_refused(
            _run_wrapper(self.doc_args("--plan-review", [plan_path]),
                         env=self.env(output_mode="conforming")))

    def test_dispatches_when_agents_md_is_present(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())
        self.assertNotIn(b"AGENTS.md", result.stderr)


class TestExecutableAgentsMdRequiredInTheMergeResult(CodexReviewExecutableTestCase):
    """Under --merge-with, Codex runs in the scratch worktree, so AGENTS.md
    must be there, not only in --repo. Code Review (BLOCKING, confirmed): an
    UNCOMMITTED AGENTS.md passed preflight and never reached the worktree."""

    def setUp(self):
        super().setUp()
        merge_repo = os.path.join(self.root, "repo-merge")
        os.makedirs(merge_repo)
        self.repo = _make_merge_fixture_repo(merge_repo, upstream_moves=True,
                                              conflict=False)

    def _args(self):
        return ["--base", "main", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", "main"]

    def test_an_uncommitted_agents_md_is_refused_and_the_worktree_removed(self):
        _git("rm", "-q", "AGENTS.md", cwd=self.repo)
        _git("commit", "-q", "-m", "drop committed AGENTS.md", cwd=self.repo)
        with open(os.path.join(self.repo, "AGENTS.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("# house rules, never committed\n")

        result = _run_wrapper(self._args(), env=self.env(output_mode="conforming"))

        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"in the merge result", result.stderr)
        listing = subprocess.run(["git", "worktree", "list", "--porcelain"],
                                 cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(listing.stdout.count("worktree "), 1, listing.stdout)

    def test_a_committed_agents_md_reaches_the_merge_result(self):
        """Control: the same fixture with AGENTS.md committed dispatches."""
        result = _run_wrapper(self._args(), env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())


_SNAPSHOT_LINE_RE = re.compile(
    r"^reviewed snapshot: head=([0-9a-f]{40}) tree=([0-9a-f]{40}) "
    r"base=(\S+)@([0-9a-f]{40}) merge-base=([0-9a-f]{40})$")


def _snapshot_line_fields(testcase, stdout_bytes):
    """The five parsed fields of the `reviewed snapshot:` line, or a test
    failure if it is absent or malformed."""
    lines = [l for l in stdout_bytes.decode("utf-8", "replace").splitlines()
             if l.startswith("reviewed snapshot: ")]
    testcase.assertEqual(len(lines), 1, stdout_bytes)
    match = _SNAPSHOT_LINE_RE.match(lines[0])
    testcase.assertIsNotNone(match, lines[0])
    return match.groups()


class TestExecutableMergeWithSendsBothBlocks(CodexReviewExecutableTestCase):
    """`--merge-with` sends BOTH labelled blocks -- rewritten after
    its first version (asserting the DIFF differs under a merge) was
    measured unsatisfiable: a three-dot diff is invariant under merging
    the base in, SHA-256 identical before and after.

    Against a fixture with both an upstream commit and a branch commit,
    cleanly merged: the dispatch target is the scratch worktree and it
    contains the merge result; stdin carries both section markers; each
    block equals the independently-recomputed `git diff` -- not the
    wrapper's own internals, same technique as `_expected_diff`;
    the upstream block is non-empty, which the fixture's upstream commit
    exists precisely to make non-vacuous; and Code Review's stdin on the
    SAME fixture carries NEITHER marker and equals the branch diff alone
    -- the assertion that proves the two gates are observably different.
    """

    def setUp(self):
        super().setUp()
        merge_repo = os.path.join(self.root, "repo-merge")
        os.makedirs(merge_repo)
        self.repo = _make_merge_fixture_repo(merge_repo, upstream_moves=True,
                                              conflict=False)

    def test_merge_with_sends_both_labelled_blocks_and_code_review_sends_neither(self):
        args = ["--base", "main", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", "main"]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())

        dump = self.shim_dump()
        # The dispatch target is a scratch worktree, distinct from --repo
        # itself, and it contains the merge result (build_argv's -C).
        self.assertNotEqual(os.path.realpath(dump["cwd"]),
                             os.path.realpath(self.repo))
        # Merge Review stays on its own model -- the Code gate's pin must not
        # leak into it just because both start from `--base`.
        _assert_pinned_model_and_effort(self, dump["argv"])

        with open(self.dump_path + ".stdin", "rb") as handle:
            stdin_bytes = handle.read()

        branch_marker = b"=== YOUR BRANCH ===\n"
        upstream_marker = b"=== MOVED UNDER YOU SINCE ===\n"
        self.assertTrue(stdin_bytes.startswith(branch_marker))
        self.assertIn(upstream_marker, stdin_bytes)
        upstream_idx = stdin_bytes.index(upstream_marker)
        branch_block = stdin_bytes[len(branch_marker):upstream_idx]
        upstream_block = stdin_bytes[upstream_idx + len(upstream_marker):]

        # Recomputed independently against `self.repo` -- the wrapper's own
        # `git fetch origin main` mutated `self.repo`'s `origin/main` ref
        # in place (fetch runs against --repo, never undone by cleanup,
        # which only removes the scratch worktree), so this is a genuine
        # cross-check against the wrapper's real inputs, not a
        # re-derivation of its internals.
        expected_branch = subprocess.run(
            ["git", "-C", self.repo, "diff", "origin/main...HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
        branch_point = subprocess.run(
            ["git", "-C", self.repo, "merge-base", "origin/main", "HEAD"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()
        expected_upstream = subprocess.run(
            ["git", "-C", self.repo, "diff", branch_point, "origin/main"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout

        self.assertTrue(expected_upstream,
                         "fixture produced an empty upstream diff; the "
                         "non-empty assertion below would be vacuous")
        self.assertEqual(branch_block, expected_branch)
        self.assertEqual(upstream_block, expected_upstream)

        # Code Review (no --merge-with) on the SAME fixture -- neither
        # marker, and stdin equals the branch diff alone. `--base
        # origin/main` mirrors what the build flow wires in practice (the default
        # branch ref) and, on this fixture, is the one ref whose diff
        # against HEAD equals `expected_branch` above.
        code_review_dump_path = os.path.join(self.root, "code-review-dump.json")
        code_review_env = self.env(output_mode="conforming")
        code_review_env["CODEX_SHIM_DUMP"] = code_review_dump_path
        code_review_args = ["--base", "origin/main", "--repo", self.repo,
                             "-o", os.path.join(self.root, "code-review-report.json"),
                             "--sessions-root", self.sessions_root]
        code_result = _run_wrapper(code_review_args, env=code_review_env)
        self.assertEqual(code_result.returncode, 0, code_result.stderr)

        with open(code_review_dump_path + ".stdin", "rb") as handle:
            code_review_stdin = handle.read()
        self.assertNotIn(branch_marker, code_review_stdin)
        self.assertNotIn(upstream_marker, code_review_stdin)
        self.assertEqual(code_review_stdin, expected_branch)

    def test_merge_gate_prints_the_snapshot_line(self):
        branch_head = _git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()
        ancestor = _git("rev-parse", "HEAD~1", cwd=self.repo).stdout.strip()
        args = ["--base", "main", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", "main"]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)

        head, tree, base, base_sha, merge_base = _snapshot_line_fields(
            self, result.stdout)
        dump = self.shim_dump()
        self.assertEqual(head, branch_head)
        self.assertNotEqual(
            tree, _git("rev-parse", branch_head + "^{tree}",
                       cwd=self.repo).stdout.strip())
        # The merge commit stays in the shared object store after the
        # scratch worktree is removed.
        self.assertEqual(
            tree, _git("rev-parse", dump["head"] + "^{tree}",
                       cwd=self.repo).stdout.strip())
        self.assertEqual(base, "origin/main")
        self.assertEqual(
            base_sha, _git("rev-parse", "origin/main^{commit}",
                           cwd=self.repo).stdout.strip())
        self.assertEqual(merge_base, ancestor)


class TestExecutableEmptyUpstreamDeltaIsLegitimate(CodexReviewExecutableTestCase):
    """Empty upstream delta -> exit 0 and says so. Fixture: a branch
    whose base has moved not at all. Assert exit 0, both markers still
    present, the upstream block empty, and the "equivalent to the code
    review" sentence printed. Guards the over-correction: an
    implementation reusing step 4's empty-diff rule for the upstream block
    too would fail this legitimate run."""

    def setUp(self):
        super().setUp()
        merge_repo = os.path.join(self.root, "repo-merge-quiet")
        os.makedirs(merge_repo)
        self.repo = _make_merge_fixture_repo(merge_repo, upstream_moves=False,
                                              conflict=False)

    def test_empty_upstream_delta_exits_zero_and_says_so(self):
        args = ["--base", "main", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", "main"]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"nothing landed on main since this branch diverged",
                       result.stdout)
        self.assertIn(b"equivalent to the code review", result.stdout)

        with open(self.dump_path + ".stdin", "rb") as handle:
            stdin_bytes = handle.read()
        branch_marker = b"=== YOUR BRANCH ===\n"
        upstream_marker = b"=== MOVED UNDER YOU SINCE ===\n"
        self.assertTrue(stdin_bytes.startswith(branch_marker))
        self.assertIn(upstream_marker, stdin_bytes)
        upstream_idx = stdin_bytes.index(upstream_marker)
        upstream_block = stdin_bytes[upstream_idx + len(upstream_marker):]
        self.assertEqual(upstream_block, b"")


def _make_local_only_merge_repo(root):
    """A repo with NO remote at all (row 77): `main` with a common ancestor,
    a `feature` branch with one commit, then one more commit on `main` so the
    upstream block is non-empty. Left on `feature`. Returns the repo path."""
    repo = os.path.join(root, "local-only")
    os.makedirs(repo)
    _git("init", "-q", repo, cwd=root)
    _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    _write_agents_md(repo)
    with open(os.path.join(repo, "shared.txt"), "w", encoding="utf-8") as handle:
        handle.write("start\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "common ancestor", cwd=repo)
    _git("checkout", "-q", "-b", "feature", cwd=repo)
    with open(os.path.join(repo, "branch.txt"), "w", encoding="utf-8") as handle:
        handle.write("branch work\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "branch work", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    with open(os.path.join(repo, "shared.txt"), "w", encoding="utf-8") as handle:
        handle.write("start\nupstream change\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "upstream work", cwd=repo)
    _git("checkout", "-q", "feature", cwd=repo)
    return repo


class TestExecutableMergeWithNoOriginRemote(CodexReviewExecutableTestCase):
    """Row 77: full-power Merge Review in a local-only repo merges the LOCAL
    branch instead of failing on `git fetch origin`; a missing local branch
    refuses with a runnable fix; and a CONFIGURED origin that cannot be
    fetched still refuses (keyed on the remote being absent, never on a
    fetch failing -- same keying as claude-merge-brief's row 72)."""

    def setUp(self):
        super().setUp()
        self.repo = _make_local_only_merge_repo(self.root)

    def _args(self, ref):
        return ["--base", "main", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", ref]

    def test_merges_the_local_branch_and_reviews_both_blocks(self):
        result = _run_wrapper(self._args("main"),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())

        main_sha = _git("rev-parse", "main", cwd=self.repo).stdout.strip()
        _, _, base_label, base_sha, _ = _snapshot_line_fields(self, result.stdout)
        self.assertEqual((base_label, base_sha), ("main", main_sha))

        with open(self.dump_path + ".stdin", "rb") as handle:
            stdin_bytes = handle.read()
        upstream_marker = b"=== MOVED UNDER YOU SINCE ===\n"
        self.assertIn(upstream_marker, stdin_bytes)
        upstream_block = stdin_bytes[stdin_bytes.index(upstream_marker)
                                     + len(upstream_marker):]
        self.assertIn(b"upstream change", upstream_block)

    def test_a_missing_local_branch_refuses_with_a_runnable_fix(self):
        result = _run_wrapper(self._args("trunk"),
                              env=self.env(output_mode="conforming"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"no `origin` remote", result.stderr)
        self.assertIn(b"--merge-with <branch>", result.stderr)
        self.assertIn(b"main", result.stderr)  # the local branches are listed

    def test_a_configured_but_unreachable_origin_still_refuses(self):
        _git("remote", "add", "origin",
             os.path.join(self.root, "does-not-exist.git"), cwd=self.repo)
        result = _run_wrapper(self._args("main"),
                              env=self.env(output_mode="conforming"))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"git fetch origin main failed", result.stderr)


class TestExecutableMergeConflict(CodexReviewExecutableTestCase):
    """`--merge-with` against a fixture with a CONFLICTING change ->
    non-zero, and `git worktree list` (run against the original --repo)
    shows the scratch worktree removed. The shim must never run -- a
    conflict is caught in merge-target prep, before dispatch."""

    def setUp(self):
        super().setUp()
        merge_repo = os.path.join(self.root, "repo-conflict")
        os.makedirs(merge_repo)
        self.repo = _make_merge_fixture_repo(merge_repo, upstream_moves=True,
                                              conflict=True)

    def test_conflict_is_a_loud_failure_and_the_scratch_worktree_is_gone(self):
        args = ["--base", "main", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", "main"]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.shim_was_run())

        worktree_list = subprocess.run(
            ["git", "-C", self.repo, "worktree", "list"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout
        self.assertEqual(len(worktree_list.strip().splitlines()), 1,
                          "a scratch worktree is still registered:\n%s" %
                          worktree_list)


class TestExecutableCleanupOnPostMergeFailure(CodexReviewExecutableTestCase):
    """Cleanup on a POST-merge failure, not just on conflict. The
    procedure says `worktree remove --force` runs "always, including on
    every failure path" -- the conflict test only exercises the conflict path, which
    fails BEFORE the worktree is used. Here the merge succeeds cleanly and
    the shim exits non-zero, and `git worktree list` must still show no
    scratch entry. An implementation that cleans up on conflict and leaks
    on every other path passes the conflict test and fails only this."""

    def setUp(self):
        super().setUp()
        merge_repo = os.path.join(self.root, "repo-merge-then-fail")
        os.makedirs(merge_repo)
        self.repo = _make_merge_fixture_repo(merge_repo, upstream_moves=True,
                                              conflict=False)

    def test_scratch_worktree_removed_after_a_post_merge_dispatch_failure(self):
        args = ["--base", "main", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", "main"]
        result = _run_wrapper(
            args, env=self.env(output_mode="conforming", exit_code=3))

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.shim_was_run())

        worktree_list = subprocess.run(
            ["git", "-C", self.repo, "worktree", "list"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout
        self.assertEqual(len(worktree_list.strip().splitlines()), 1,
                          "a scratch worktree is still registered:\n%s" %
                          worktree_list)


class TestExecutableBranchPointCapturedBeforeMerge(CodexReviewExecutableTestCase):
    """The branch point is captured BEFORE the merge. On a fixture
    with real upstream movement, the upstream block must be non-empty. A
    wrapper computing `merge-base` AFTER merging gets `origin/<ref>`
    itself (the merge makes it the merge-base) and produces an empty
    upstream block -- indistinguishable from a quiet week, and it would
    print the "equivalent to the code review" sentence for a run where the
    second gate genuinely bought something."""

    def setUp(self):
        super().setUp()
        merge_repo = os.path.join(self.root, "repo-merge-order")
        os.makedirs(merge_repo)
        self.repo = _make_merge_fixture_repo(merge_repo, upstream_moves=True,
                                              conflict=False)

    def test_upstream_block_is_non_empty_when_upstream_really_moved(self):
        args = ["--base", "main", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", "main"]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))

        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.dump_path + ".stdin", "rb") as handle:
            stdin_bytes = handle.read()
        upstream_marker = b"=== MOVED UNDER YOU SINCE ===\n"
        upstream_idx = stdin_bytes.index(upstream_marker)
        upstream_block = stdin_bytes[upstream_idx + len(upstream_marker):]

        self.assertTrue(upstream_block)
        self.assertNotIn(b"nothing landed on main", result.stdout)


# `build_plan_brief` now
# refuses a plan carrying no `Classification:` line, so every plan file
# dispatched through `--plan-review` below appends this suffix -- except a
# fixture deliberately missing, empty, or whitespace-only, which stays
# exactly as it is (those test the file-read refusal that runs BEFORE this
# check). Plans passed to Code Review's own `--plan` and files passed to
# `--design-review` are untouched: neither goes through `build_plan_brief`.
_PLAN_CLASSIFICATION_SUFFIX = "\n\nClassification: bounded\n"


def _plan_brief_path():
    return os.path.join(os.path.dirname(WRAPPER), "plan-brief.md")


def _design_brief_path():
    return os.path.join(os.path.dirname(WRAPPER), "design-brief.md")


def _import_merge_brief_lib():
    """These tests need the SAME `merge_brief_lib.build_plan_brief` the
    wrapper itself calls, computed independently here so a test comparing
    against it is not just re-reading the wrapper's own internals."""
    merge_brief_dir = os.path.join(os.path.dirname(HERE), "merge-brief")
    sys.path.insert(0, merge_brief_dir)
    import merge_brief_lib
    return merge_brief_lib


class TestPlanReviewDispatch(CodexReviewExecutableTestCase):
    """`--plan-review PLAN` builds stdin from
    `merge_brief_lib.build_plan_brief` and sends `plan-brief.md` as the
    positional -- never the plan text itself, and never `review-brief.md`."""

    def test_argv_carries_the_plan_brief_and_stdin_is_the_built_plan_brief(self):
        merge_brief_lib = _import_merge_brief_lib()
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n" + _PLAN_CLASSIFICATION_SUFFIX)

        result = _run_wrapper(self.doc_args("--plan-review", [plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())

        with open(_plan_brief_path(), "r", encoding="utf-8") as handle:
            expected_brief = handle.read()
        dump = self.shim_dump()
        self.assertEqual(dump["argv"][-1], expected_brief)
        # One pin for every gate, not a Code/Merge-only one.
        _assert_pinned_model_and_effort(self, dump["argv"])

        expected_stdin = merge_brief_lib.build_plan_brief(
            os.path.realpath(self.repo), os.path.realpath(plan_path)
        ).encode("utf-8")
        with open(self.dump_path + ".stdin", "rb") as handle:
            actual_stdin = handle.read()
        self.assertEqual(actual_stdin, expected_stdin)
        self.assertIn(b"do x", actual_stdin)
        self.assertNotIn(expected_brief.splitlines()[0].encode("utf-8"),
                          actual_stdin)

    def test_stdin_carries_the_source_requirements(self):
        sentinel = "sentinel-source-requirement-d41e"
        with open(os.path.join(self.root, "req.md"), "w",
                  encoding="utf-8") as handle:
            handle.write("Acceptance contract\n\n%s\n" % sentinel)
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n"
                         + _PLAN_CLASSIFICATION_SUFFIX
                         + "Requirements: req.md\n")

        result = _run_wrapper(self.doc_args("--plan-review", [plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.dump_path + ".stdin", "rb") as handle:
            stdin = handle.read()
        self.assertIn(sentinel.encode("utf-8"), stdin)
        self.assertIn(b"## Source requirements", stdin)

    def test_no_diff_is_sent(self):
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n" + _PLAN_CLASSIFICATION_SUFFIX)

        result = _run_wrapper(self.doc_args("--plan-review", [plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)

        with open(self.dump_path + ".stdin", "rb") as handle:
            actual_stdin = handle.read()
        self.assertNotIn(b"diff --git", actual_stdin)

    def test_a_plan_without_a_classification_is_refused_before_dispatch(self):
        """`build_plan_brief` now refuses a plan with no
        `Classification:` line before this wrapper ever dispatches --
        `merge_brief_lib.PlanFileError` is caught alongside every other
        `BriefError` at the same call site (`claude-codex-review` step 4)
        and returns 1, the same exit code every other bad-input case in
        `TestDocGateRefusesBadInput` uses."""
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n")

        result = _run_wrapper(self.doc_args("--plan-review", [plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(b"no `Classification:` line", result.stderr)
        self.assertFalse(self.shim_was_run())


class TestDesignReviewDispatch(CodexReviewExecutableTestCase):
    """`--design-review FILE [FILE ...]` joins each file, in the
    order given, under its own `=== <realpath> ===` header, and sends
    `design-brief.md` as the positional."""

    def test_files_are_joined_in_order_under_path_headers(self):
        real_dir = os.path.join(self.root, "real")
        os.makedirs(real_dir)
        design_path = os.path.join(real_dir, "design.md")
        reqs_path = os.path.join(real_dir, "requirements.md")
        with open(design_path, "w", encoding="utf-8") as handle:
            handle.write("design body\n")
        with open(reqs_path, "w", encoding="utf-8") as handle:
            handle.write("requirements body")  # no trailing newline

        link_dir = os.path.join(self.root, "link")
        os.symlink(real_dir, link_dir)
        given_design = os.path.join(link_dir, "design.md")
        given_reqs = os.path.join(link_dir, "requirements.md")

        result = _run_wrapper(
            self.doc_args("--design-review", [given_design, given_reqs]),
            env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())

        with open(_design_brief_path(), "r", encoding="utf-8") as handle:
            expected_brief = handle.read()
        dump = self.shim_dump()
        self.assertEqual(dump["argv"][-1], expected_brief)
        _assert_pinned_model_and_effort(self, dump["argv"])

        expected_stdin = (
            "=== %s ===\ndesign body\n=== %s ===\nrequirements body\n" %
            (os.path.realpath(given_design), os.path.realpath(given_reqs))
        ).encode("utf-8")
        with open(self.dump_path + ".stdin", "rb") as handle:
            actual_stdin = handle.read()
        self.assertEqual(actual_stdin, expected_stdin)


class TestDocGateStaleReportNotMistakenForFresh(CodexReviewExecutableTestCase):
    """the CLAUDE.md "run it TWICE against the SAME path" rule,
    exercised under `--plan-review` -- the diff gates already cover this,
    this is the document-gate path through the SAME shared step 5 removal."""

    def setUp(self):
        super().setUp()
        self.plan_path = os.path.join(self.root, "plan.md")
        with open(self.plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n" + _PLAN_CLASSIFICATION_SUFFIX)

    def test_second_run_writing_nothing_fails_even_though_a_report_exists(self):
        args = self.doc_args("--plan-review", [self.plan_path])
        first = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue(os.path.isfile(self.output_path))

        second = _run_wrapper(args, env=self.env(output_mode="none"))
        self.assertNotEqual(second.returncode, 0)

    def test_positive_control_second_run_writing_properly_passes(self):
        args = self.doc_args("--plan-review", [self.plan_path])
        first = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(first.returncode, 0, first.stderr)

        second = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(second.returncode, 0, second.stderr)


REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SEMANTIC_CASES_PATH = os.path.join(REPO_ROOT, "tools", "build-lens", "tests",
                                   "semantic_cases.json")
SEMANTIC_CASE_NAMES = {
    "clean_approve", "executed_missing", "ran_false_no_why_not_no_findings",
    "ran_false_honest_why_not_no_findings",
    "ran_false_blank_why_not_with_finding", "ran_false_honest_with_finding",
    "ran_true_no_commands", "ran_true_whitespace_command",
    "ran_true_one_blank_among_real", "approve_with_blocking",
    "approve_with_important_then_blocking", "approve_with_important_only",
    "request_changes_with_blocking",
}


def _load_wrapper_module():
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader(
        "claude_codex_review", WRAPPER)
    spec = importlib.util.spec_from_loader("claude_codex_review", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class TestSemanticCasesParity(unittest.TestCase):
    """The wrapper's semantic check is the build-lens one, checked against
    the same case file the JavaScript port in `blinded-review.js` is tested
    against, so "one rule in two languages" is verified rather than asserted
    in a comment."""

    @classmethod
    def setUpClass(cls):
        cls.module = _load_wrapper_module()
        with open(SEMANTIC_CASES_PATH, "r", encoding="utf-8") as handle:
            cls.cases = json.load(handle)

    def test_the_wrapper_uses_the_build_lens_module(self):
        lens_path = os.path.join(REPO_ROOT, "tools", "build-lens",
                                 "review_lib.py")
        self.assertEqual(os.path.realpath(self.module.review_lib.__file__),
                         os.path.realpath(lens_path))

    def test_every_case_matches_its_expectation(self):
        for case in self.cases:
            with self.subTest(name=case["name"]):
                got = self.module.review_lib.semantic_violation(case["report"])
                if case["expect"] == "accept":
                    self.assertIsNone(got)
                else:
                    self.assertIsInstance(got, str)
                    self.assertIn(case["reason"], got)

    def test_the_case_set_is_exactly_the_agreed_names(self):
        self.assertEqual({c["name"] for c in self.cases}, SEMANTIC_CASE_NAMES)


class TestNoLocalValidatorCopy(unittest.TestCase):
    """One validator, in build-lens: a copy here could drift from the one the
    economical path uses and accept a report that path would refuse."""

    NAMES = ("_semantic_violation", "semantic_violation", "_validate_node",
             "validate_against_schema")

    def test_neither_module_defines_a_validator(self):
        wrapper = _load_wrapper_module()
        for name in self.NAMES:
            with self.subTest(name=name):
                self.assertFalse(hasattr(codex_review_lib, name))
                self.assertNotIn(name, vars(wrapper))

    def test_no_case_file_beside_the_wrapper(self):
        self.assertFalse(os.path.exists(
            os.path.join(HERE, "tests", "semantic_cases.json")))


class TestModelRejectedExitsFour(CodexReviewExecutableTestCase):
    """A model the account does not serve is the same situation as a missing
    Codex: nothing was reviewed, and the user chooses how to proceed."""

    MEASURED = ('ERROR: {"detail":"The model is not supported when using '
                'Codex with a ChatGPT account.","status":400}')

    def test_rejected_model_exits_four_and_leaves_no_report(self):
        result = _run_wrapper(
            self.base_args(),
            env=self.env(output_mode="conforming", exit_code=1,
                         extra={"CODEX_SHIM_STDERR": self.MEASURED}))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertTrue(self.shim_was_run())
        self.assertIn(b"Codex is unavailable", result.stderr)
        self.assertIn(b"gpt-5.6-terra", result.stderr)
        self.assertIn(b"codex_review.models.code", result.stderr)
        self.assertFalse(os.path.exists(self.output_path))


class TestOtherCodexFailureStaysOne(CodexReviewExecutableTestCase):
    """Only the model-rejection shape is "unavailable"; any other non-zero
    exit is a failed review."""

    def test_a_401_failure_exits_one(self):
        result = _run_wrapper(
            self.base_args(),
            env=self.env(output_mode="conforming", exit_code=1,
                         extra={"CODEX_SHIM_STDERR":
                                'ERROR: {"detail":"unauthorized","status":401}'}))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(b"Codex is unavailable", result.stderr)


class _ConfigWritingTestCase(CodexReviewExecutableTestCase):
    def write_config(self, content):
        """`content` is written as JSON unless it is a str, which is written
        verbatim (so a test can write invalid JSON)."""
        text = content if isinstance(content, str) else json.dumps(content)
        with open(_config_path_for(self.home), "w", encoding="utf-8") as handle:
            handle.write(text)


class TestConfiguredModelReachesTheArgv(_ConfigWritingTestCase):
    def test_configured_code_model_is_dispatched_and_labelled(self):
        self.write_config({"codex_review": {"models": {"code": "my-model"}}})
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.shim_dump()["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "my-model")
        self.assertIn(b"codex gate: code review on my-model (configured)",
                      result.stdout)

    def test_another_gate_keeps_its_pin(self):
        self.write_config({"codex_review": {"models": {"code": "my-model"}}})
        merge_repo = os.path.join(self.root, "repo-merge-config")
        os.makedirs(merge_repo)
        repo = _make_merge_fixture_repo(merge_repo, upstream_moves=True,
                                        conflict=False)
        args = ["--base", "main", "--repo", repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", "main"]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.shim_dump()["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], PINNED_MODEL)
        self.assertIn(b"codex gate: merge review on gpt-6-astra (default)",
                      result.stdout)


class TestBadModelConfigRefusesBeforeDispatch(_ConfigWritingTestCase):
    """A config the wrapper cannot trust is refused before any review is
    paid for, naming what to fix."""

    def _assert_refused(self, content, named):
        self.write_config(content)
        result = _run_wrapper(self.base_args(),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(named.encode("utf-8"), result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_models_that_is_not_an_object(self):
        self._assert_refused({"codex_review": {"models": ["code"]}},
                             "codex_review.models")

    def test_empty_model_name(self):
        self._assert_refused({"codex_review": {"models": {"code": ""}}},
                             "codex_review.models.code")

    def test_model_name_with_whitespace(self):
        self._assert_refused({"codex_review": {"models": {"code": "a b"}}},
                             "codex_review.models.code")

    def test_model_name_starting_with_a_dash(self):
        self._assert_refused({"codex_review": {"models": {"code": "-x"}}},
                             "codex_review.models.code")

    def test_boolean_model_name(self):
        self._assert_refused({"codex_review": {"models": {"code": True}}},
                             "codex_review.models.code")

    def test_key_that_is_not_a_gate(self):
        self._assert_refused({"codex_review": {"models": {"review": "m"}}},
                             "codex_review.models.review")

    def test_codex_review_that_is_not_an_object(self):
        self._assert_refused({"codex_review": "full"}, "codex_review")

    def test_config_that_is_not_valid_json(self):
        self._assert_refused("{not json", _config_path_for(self.home))


class TestExecutableNewSemanticRules(CodexReviewExecutableTestCase):
    """The two newer validator rules, through the real executable on a Code
    gate."""

    def _assert_rejected(self, mode, reason):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode=mode))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertTrue(self.shim_was_run())
        self.assertIn(b"not a review this gate can accept", result.stderr)
        self.assertIn(reason, result.stderr)

    def test_approve_with_a_blocking_finding_is_rejected(self):
        self._assert_rejected("approve_with_blocking", b"APPROVE")

    def test_a_blank_command_is_rejected(self):
        self._assert_rejected("blank_command", b"blank entry")


class TestDocGateValidatesTheReport(CodexReviewExecutableTestCase):
    """a document gate runs the SAME schema and semantic validation
    as a diff gate, exercised under `--plan-review`."""

    def setUp(self):
        super().setUp()
        self.plan_path = os.path.join(self.root, "plan.md")
        with open(self.plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n" + _PLAN_CLASSIFICATION_SUFFIX)

    def test_empty_execution_fails_and_says_why(self):
        result = _run_wrapper(
            self.doc_args("--plan-review", [self.plan_path]),
            env=self.env(output_mode="empty_execution"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"not a review this gate can accept", result.stderr)
        self.assertIn(b"findings is empty", result.stderr)

    def test_prose_fails(self):
        result = _run_wrapper(
            self.doc_args("--plan-review", [self.plan_path]),
            env=self.env(output_mode="prose"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"is not valid JSON", result.stderr)

    def test_undeclared_key_fails(self):
        result = _run_wrapper(
            self.doc_args("--plan-review", [self.plan_path]),
            env=self.env(output_mode="undeclared_key"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"additional property 'SURPRISE' is not allowed",
                      result.stderr)

    def test_legit_not_ran_passes(self):
        result = _run_wrapper(
            self.doc_args("--plan-review", [self.plan_path]),
            env=self.env(output_mode="legit_not_ran"))
        self.assertEqual(result.returncode, 0, result.stderr)


class TestSmallWrapperFixes(CodexReviewExecutableTestCase):
    """Headroom visible before the spend, `--merge-with`
    refusing a remote ref, and no abbreviated flags."""

    def _register_worktree_cleanup(self):
        """SIGKILL skips the wrapper's `finally`, so cleanup is guaranteed
        only on catchable exits. Registered BEFORE the run so it fires
        after it: removes every worktree other than the repo itself."""
        def cleanup():
            listing = subprocess.run(
                ["git", "-C", self.repo, "worktree", "list", "--porcelain"],
                capture_output=True, text=True).stdout
            for line in listing.splitlines():
                if not line.startswith("worktree "):
                    continue
                path = line[len("worktree "):]
                if os.path.realpath(path) == os.path.realpath(self.repo):
                    continue
                subprocess.run(
                    ["git", "-C", self.repo, "worktree", "remove", "--force",
                     path], capture_output=True)
        self.addCleanup(cleanup)

    def test_headroom_line_is_flushed_before_codex_runs(self):
        """Off a TTY, an unflushed print sits in the buffer until exit. The
        shim SIGKILLs the wrapper mid-dispatch, so the line is on the pipe
        only if SOME flush ran between it and codex starting. That is the
        property pinned, not the headroom print's own flush=True: the
        flushed "reviewed snapshot:" and "codex gate:" lines also carry it
        out, so removing any one flush survives and only removing all of
        them goes red (measured). `--plan` keeps the "no --plan
        given" line out of the run, so it is not one more carrier."""
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n")
        self._register_worktree_cleanup()
        result = _run_wrapper(self.base_args(["--plan", plan_path]),
                              env=self.env(output_mode="kill_parent"))
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertTrue(self.shim_was_run())
        self.assertIn(b"codex meter", result.stdout)

    def test_gates_left_line_is_flushed_before_codex_runs(self):
        """same mechanism as the headroom test above, but for the
        pre-dispatch GATES-LEFT line (Step 2's own `try/except`, separate
        from `format_headroom`'s). A rollout is pre-seeded so the
        line has something to say, and the shim SIGKILLs the wrapper
        mid-dispatch so only flushed, pre-dispatch output survives. As with
        the headroom test, its own flush=True is one of several carriers:
        this pins that the line reaches the pipe before codex starts, not
        which print flushed it."""
        os.makedirs(self.sessions_root, exist_ok=True)
        _write_jsonl(os.path.join(self.sessions_root, "rollout-pre.jsonl"),
                     [_event_line(_calibrated_block(90.0, time.time() + 86400))])
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n")
        self._register_worktree_cleanup()
        result = _run_wrapper(self.base_args(["--plan", plan_path]),
                              env=self.env(output_mode="kill_parent"))
        self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr)
        self.assertIn(b"codex budget: 90% of the 7-day window used",
                      result.stdout)

    def test_merge_with_a_remote_ref_is_a_usage_error_naming_the_branch(self):
        result = _run_wrapper(self.base_args(["--merge-with", "origin/main"]),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"pass 'main'", result.stderr)

    def test_an_abbreviated_flag_is_rejected(self):
        """`--merge` must not silently resolve to `--merge-with`."""
        result = _run_wrapper(self.base_args(["--merge", "main"]),
                              env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"unrecognized arguments", result.stderr)


class TestDocGateUsageErrors(CodexReviewExecutableTestCase):
    """every document-gate usage conflict fails at argument parsing
    (exit 2) and dispatches nothing."""

    def setUp(self):
        super().setUp()
        self.plan_path = os.path.join(self.root, "plan.md")
        with open(self.plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n" + _PLAN_CLASSIFICATION_SUFFIX)
        self.design_path = os.path.join(self.root, "design.md")
        with open(self.design_path, "w", encoding="utf-8") as handle:
            handle.write("design content\n")

    def test_plan_review_with_base_is_a_usage_error(self):
        args = ["--plan-review", self.plan_path, "--base", "base",
                "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_design_review_with_merge_with_is_a_usage_error(self):
        args = ["--design-review", self.design_path, "--merge-with", "main",
                "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_both_document_flags_is_a_usage_error(self):
        args = ["--plan-review", self.plan_path,
                "--design-review", self.design_path,
                "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_no_base_and_no_document_flag_is_a_usage_error(self):
        args = ["--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"--base is required", result.stderr)


class TestDocGateRefusesBadInput(CodexReviewExecutableTestCase):
    """bad document input is a loud failure, never a dispatch --
    including the mode-specific findings-payload refusal that pins the
    `"plan"` mode argument `scan_for_leaked_provenance` is always called
    with, under BOTH document flags."""


    def _valid_reqs(self):
        """A well-formed requirements file, so a design-review test reaches
        the content check it is named for rather than the two-file rule."""
        path = os.path.join(self.root, "requirements.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Requirements\n\n- do the thing\n")
        return path
    def test_missing_plan_path(self):
        missing = os.path.join(self.root, "does-not-exist.md")
        result = _run_wrapper(self.doc_args("--plan-review", [missing]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_plan_file_whitespace_only(self):
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("   \n")
        result = _run_wrapper(self.doc_args("--plan-review", [plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_design_file_empty(self):
        design_path = os.path.join(self.root, "design.md")
        with open(design_path, "w", encoding="utf-8") as handle:
            handle.write("")
        result = _run_wrapper(self.doc_args("--design-review", [design_path, self._valid_reqs()]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_plan_file_with_leaked_round_heading_is_refused(self):
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n## Round 2 findings\n\nbody\n"
                          + _PLAN_CLASSIFICATION_SUFFIX)
        result = _run_wrapper(self.doc_args("--plan-review", [plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_design_file_with_leaked_round_heading_is_refused(self):
        design_path = os.path.join(self.root, "design.md")
        with open(design_path, "w", encoding="utf-8") as handle:
            handle.write("# A design\n\n## Round 2 findings\n\nbody\n")
        result = _run_wrapper(self.doc_args("--design-review", [design_path, self._valid_reqs()]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_plan_file_with_findings_payload_is_refused(self):
        # Built with `%` so the literal payload string never appears in
        # this file's own source -- the scan under --pr/--pre-pr WARNS on
        # exactly this shape, and a source-literal copy would make this
        # repo's own test file the kind of false positive that check
        # deliberately tolerates elsewhere.
        payload_line = '{"%s": "%s"}' % ("severity", "BLOCKING")
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write(("# A plan\n\n%s\n" % payload_line)
                          + _PLAN_CLASSIFICATION_SUFFIX)
        result = _run_wrapper(self.doc_args("--plan-review", [plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_design_file_with_findings_payload_is_refused(self):
        payload_line = '{"%s": "%s"}' % ("severity", "BLOCKING")
        design_path = os.path.join(self.root, "design.md")
        with open(design_path, "w", encoding="utf-8") as handle:
            handle.write("# A design\n\n%s\n" % payload_line)
        result = _run_wrapper(self.doc_args("--design-review", [design_path, self._valid_reqs()]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())


class TestDocGateWarnsAndContinues(CodexReviewExecutableTestCase):
    """a prose `round N` (not a heading) is warn-only in every
    mode -- the run still dispatches and exits 0, but stderr says so."""

    def test_prose_round_n_warns_but_continues(self):
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\nThis is the third round 3 of edits.\n"
                          + _PLAN_CLASSIFICATION_SUFFIX)
        result = _run_wrapper(self.doc_args("--plan-review", [plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())
        # Assert the scan's own warning text, not just `b"WARNING"`: any
        # other warning on stderr would make a bare match pass with the
        # provenance warning never printed at all.
        self.assertIn(b"prose 'round N'", result.stderr)


class TestDocGateBriefMissing(CodexReviewExecutableTestCase):
    """`_make_wrapper_copy`'s scratch copy carries `review-brief.md`
    but never `plan-brief.md`/`design-brief.md` -- proving the brief-missing
    failure names the SELECTED brief file, not always `review-brief.md`."""

    def test_plan_review_with_no_plan_brief_in_the_copy_fails_loudly(self):
        copied_exe = _make_wrapper_copy(self.root, "brief")
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n" + _PLAN_CLASSIFICATION_SUFFIX)

        result = subprocess.run(
            [sys.executable, copied_exe, "--plan-review", plan_path,
             "--repo", self.repo, "-o", self.output_path,
             "--sessions-root", self.sessions_root],
            env=self.env(output_mode="conforming"), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"plan-brief.md", result.stderr)
        self.assertFalse(self.shim_was_run())


BUILD_LENS = os.path.join(os.path.dirname(HERE), "build-lens")


def _reference_lens_rows(path, heading):
    """Rows of a `| `key` | prompt |` lens table, scoped to the span that
    starts at the line equal to `heading` and ends at the next line
    starting with `### ` (or end of file). Same row regex
    test_build_lens.py's `_parse_design_lens_table` uses, so a reference
    table and this helper agree on what counts as a row."""
    text = open(path, encoding="utf-8").read()
    lines = text.split("\n")
    start = None
    for i, line in enumerate(lines):
        if line == heading:
            start = i + 1
            break
    if start is None:
        raise AssertionError("heading %r not found in %s" % (heading, path))
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("### "):
            end = i
            break
    span = "\n".join(lines[start:end])
    pattern = re.compile(r"^\|\s*`([a-z-]+)`\s*\|\s*(.+?)\s*\|\s*$", re.MULTILINE)
    return pattern.findall(span)


def _brief_lens_lines(path):
    """Every `- `key`: prompt` bullet line in a brief, as `{key: prompt}`
    with the prompt whitespace-normalised. A substring check on the whole
    normalised brief text passes if the reference wording appears ANYWHERE
    in the file, in any order, under any key -- this compares each lens's
    prompt to exactly the row it claims to carry."""
    pattern = re.compile(r"^- `([a-z-]+)`: (.*)$")
    text = open(path, encoding="utf-8").read()
    result = {}
    for line in text.split("\n"):
        match = pattern.match(line)
        if match:
            result[match.group(1)] = " ".join(match.group(2).split())
    return result


class TestDocBriefsCarryTheReferenceLenses(unittest.TestCase):
    """The
    two fixed document-gate prompts, `plan-brief.md` and `design-brief.md`,
    carry their gate's lens questions word for word from the reference lens
    tables in `tools/build-lens/{plan,design}-review.md`. A test fails if the
    two drift apart in either direction -- a brief with stale wording, or a
    brief that has grown a lens the reference table does not have."""

    def test_plan_brief_carries_every_plan_review_lens(self):
        rows = _reference_lens_rows(
            os.path.join(BUILD_LENS, "plan-review.md"), "### Plan Review lenses")
        self.assertEqual(len(rows), 6, rows)
        # Plans are prose an executor obeys, so Plan Review carries a
        # plan-shaped instruction-docs lens too.
        self.assertIn("instruction-docs", dict(rows))
        reference_mapping = {key: " ".join(prompt.split()) for key, prompt in rows}
        brief_mapping = _brief_lens_lines(os.path.join(HERE, "plan-brief.md"))
        self.assertEqual(brief_mapping, reference_mapping)

    def test_design_brief_carries_every_design_review_lens(self):
        rows = _reference_lens_rows(
            os.path.join(BUILD_LENS, "design-review.md"), "### The lens table")
        self.assertEqual(len(rows), 5, rows)
        reference_mapping = {key: " ".join(prompt.split()) for key, prompt in rows}
        brief_mapping = _brief_lens_lines(os.path.join(HERE, "design-brief.md"))
        self.assertEqual(brief_mapping, reference_mapping)

    def test_no_brief_carries_a_lens_the_reference_lacks(self):
        cases = [
            ("plan-brief.md", os.path.join(BUILD_LENS, "plan-review.md"),
             "### Plan Review lenses"),
            ("design-brief.md", os.path.join(BUILD_LENS, "design-review.md"),
             "### The lens table"),
        ]
        line_pattern = re.compile(r"^- `([a-z-]+)`: ")
        for brief_name, ref_path, heading in cases:
            reference_keys = {key for key, _ in _reference_lens_rows(ref_path, heading)}
            brief_text = open(os.path.join(HERE, brief_name), encoding="utf-8").read()
            brief_keys = {m.group(1) for m in
                          (line_pattern.match(line) for line in brief_text.split("\n"))
                          if m}
            self.assertEqual(brief_keys, reference_keys, brief_name)


class TestCodeReviewWithPlan(CodexReviewExecutableTestCase):
    """`--plan FILE` on a Code Review sends `=== THE PLAN ===` +
    the plan bytes + `=== THE DIFF ===` + the diff -- the third stdin
    shape -- and prints a notice when no plan is given at all."""

    def test_stdin_is_plan_block_then_diff_block(self):
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# Plan\n- Step 1\n")

        result = _run_wrapper(self.base_args(["--plan", plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)

        with open(self.dump_path + ".stdin", "rb") as handle:
            actual_stdin = handle.read()
        expected = (b"=== THE PLAN ===\n# Plan\n- Step 1\n"
                    b"=== THE DIFF ===\n" + _expected_diff(self.repo))
        self.assertEqual(actual_stdin, expected)
        # `--plan` is spec-drift context for a Code gate, not a Plan Review:
        # it stays on the Code gate's model.
        _assert_pinned_model_and_effort(self, self.shim_dump()["argv"],
                                        PINNED_CODE_MODEL)

    def test_plan_without_trailing_newline_still_separates_blocks(self):
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("no newline")

        result = _run_wrapper(self.base_args(["--plan", plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)

        with open(self.dump_path + ".stdin", "rb") as handle:
            actual_stdin = handle.read()
        self.assertTrue(actual_stdin.startswith(
            b"=== THE PLAN ===\nno newline\n=== THE DIFF ===\n"))

    def test_no_notice_when_plan_given(self):
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# Plan\n- Step 1\n")

        result = _run_wrapper(self.base_args(["--plan", plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"no --plan given", result.stdout)

    def test_notice_when_plan_absent(self):
        result = _run_wrapper(self.base_args(),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            b"no --plan given: this Code Review has no plan to check "
            b"spec drift against", result.stdout)


class TestPlanFlagUsageErrors(CodexReviewExecutableTestCase):
    """every `--plan` usage conflict fails at argument parsing
    (exit 2) and dispatches nothing."""

    def setUp(self):
        super().setUp()
        self.plan_path = os.path.join(self.root, "plan.md")
        with open(self.plan_path, "w", encoding="utf-8") as handle:
            handle.write("# Plan\n- Step 1\n")

    def test_plan_with_merge_with_is_a_usage_error(self):
        args = ["--base", "base", "--merge-with", "main", "--plan",
                 self.plan_path, "--repo", self.repo, "-o", self.output_path,
                 "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"Merge Review is blinded to the plan", result.stderr)

    def test_plan_with_plan_review_is_a_usage_error(self):
        args = ["--plan-review", self.plan_path, "--plan", self.plan_path,
                 "--repo", self.repo, "-o", self.output_path,
                 "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_plan_with_design_review_is_a_usage_error(self):
        design_path = os.path.join(self.root, "design.md")
        with open(design_path, "w", encoding="utf-8") as handle:
            handle.write("design content\n")
        args = ["--design-review", design_path, "--plan", self.plan_path,
                 "--repo", self.repo, "-o", self.output_path,
                 "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())


class TestPlanFlagRefusesBadInput(CodexReviewExecutableTestCase):
    """bad `--plan` input is a loud failure, never a dispatch.

    Asserts the EXACT exit code (1) and a message naming `--plan`, rather
    than just `!= 0` -- on the pre-Task-3 wrapper, an unrecognised `--plan`
    flag itself makes argparse exit 2, which a bare `!= 0` would pass
    vacuously without this code path existing at all."""

    def test_missing_plan_path(self):
        missing = os.path.join(self.root, "does-not-exist.md")
        result = _run_wrapper(self.base_args(["--plan", missing]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"--plan file", result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_plan_file_whitespace_only(self):
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("   \n")
        result = _run_wrapper(self.base_args(["--plan", plan_path]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"--plan file", result.stderr)
        self.assertIn(b"empty or whitespace-only", result.stderr)
        self.assertFalse(self.shim_was_run())


class TestEmptyFlagValuesAreUsageErrors(CodexReviewExecutableTestCase):
    """Code Review fix: an empty string given to `--plan-review`, `--plan`,
    or `--design-review` is a usage error, not an absent flag -- a
    truthiness check on any of these would let `--plan-review ""
    --base base` fall through and silently dispatch a Code Review, or let
    `--plan ""` silently become a plan-less Code Review. Every case here
    fails at argument parsing (exit 2), before anything else is checked,
    and dispatches nothing."""

    def test_empty_plan_review_with_base_is_a_usage_error(self):
        args = ["--plan-review", "", "--base", "base",
                "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"empty path", result.stderr)

    def test_empty_plan_review_alone_is_a_usage_error(self):
        args = ["--plan-review", "",
                "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"empty path", result.stderr)

    def test_empty_plan_with_base_is_a_usage_error(self):
        args = ["--base", "base", "--plan", "",
                "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"empty path", result.stderr)

    def test_empty_design_review_is_a_usage_error(self):
        args = ["--design-review", "",
                "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"empty path", result.stderr)

    def test_empty_merge_with_is_a_usage_error(self):
        # Without this, the gate was chosen by presence (Merge) but
        # the merge was skipped by truthiness: a Code diff, mislabelled.
        args = self.base_args(["--merge-with", ""])
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        self.assertIn(b"empty ref given to --merge-with", result.stderr)


class TestReviewBriefCarriesSpecDrift(unittest.TestCase):
    """`review-brief.md`'s third stdin shape and its `spec-drift`
    lens carry `code-review.md`'s `spec-drift` row word for word."""

    def test_review_brief_carries_the_spec_drift_lens_and_plan_markers(self):
        rows = _reference_lens_rows(
            os.path.join(BUILD_LENS, "code-review.md"),
            "### Code Review lenses")
        spec_drift_prompt = None
        for key, prompt in rows:
            if key == "spec-drift":
                spec_drift_prompt = prompt
                break
        self.assertIsNotNone(spec_drift_prompt, rows)

        brief = " ".join(open(os.path.join(HERE, "review-brief.md"),
                               encoding="utf-8").read().split())
        needle = " ".join(spec_drift_prompt.split())
        self.assertIn(needle, brief)
        self.assertIn("=== THE PLAN ===", brief)
        self.assertIn("=== THE DIFF ===", brief)


class TestReviewBriefCarriesInstructionDocs(unittest.TestCase):
    """`review-brief.md` (the Codex reviewer) carries `code-review.md`'s
    `instruction-docs` row word for word, so `full-power` and `economical`
    review prose with the same lens. Added after a re-run
    found the Codex brief had no prose lens at all: three models scored
    0-1 of 5 on a diff made mostly of instruction documents."""

    def test_review_brief_carries_the_instruction_docs_lens(self):
        rows = dict(_reference_lens_rows(
            os.path.join(BUILD_LENS, "code-review.md"),
            "### Code Review lenses"))
        self.assertIn("instruction-docs", rows)
        # A three-column row (`key | add when | prompt`): the regex returns
        # both trailing cells joined, so the prompt is after the last pipe.
        prompt = rows["instruction-docs"].rsplit("|", 1)[-1]
        self.assertGreater(len(prompt.split()), 50, prompt)

        brief = " ".join(open(os.path.join(HERE, "review-brief.md"),
                               encoding="utf-8").read().split())
        self.assertIn(" ".join(prompt.split()), brief)



class TestDesignReviewNeedsTheRequirements(CodexReviewExecutableTestCase):
    """Merge Review finding (R3): the Design Review contract is the design
    AND the requirements it answers, so a lone design file is a usage error
    (exit 2, nothing dispatched) rather than a review judged with no
    contract. Two files is the positive control."""

    def _write(self, name, body):
        path = os.path.join(self.root, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def test_one_file_is_a_usage_error(self):
        design = self._write("design.md", "# A design\n")
        result = _run_wrapper(self.doc_args("--design-review", [design]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(b"at least two files", result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_two_files_dispatch(self):
        design = self._write("design.md", "# A design\n")
        reqs = self._write("reqs.md", "# Requirements\n")
        result = _run_wrapper(self.doc_args("--design-review", [design, reqs]),
                               env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())


# ---------------------------------------------------------------------------
# Pre-dispatch budget line, token-based per-run cost, post-run
# budget line. `CODEX_SHIM_ROLLOUT` (see SHIM_SOURCE above) is how these
# tests put a real rollout file where `run_cost` and the post-run
# `snapshot_meter` can find it, attributed to the dispatch target by cwd.
# ---------------------------------------------------------------------------


class TestExecutableCostAndBudgetLinesAfterDispatch(CodexReviewExecutableTestCase):
    """a shim rollout written during dispatch -> stdout carries the
    token-based cost line (100,000 uncached tokens, no fitted rate for the
    shim's plan) and, AFTER it, the post-run budget line computed from that
    SAME rollout's default 50%-used block, which says it has no gate count
    for the same reason."""

    def test_cost_line_then_budget_line(self):
        rollout = self.shim_rollout_path()
        result = _run_wrapper(
            self.base_args(),
            env=self.env(output_mode="conforming",
                          extra={"CODEX_SHIM_ROLLOUT": rollout}))

        self.assertEqual(result.returncode, 0, result.stderr)
        cost_line = ("codex run used 100,000 uncached input tokens on "
                     "gpt-6-astra; no fitted rate for plan "
                     "test-plan / that model, so no cost "
                     "estimate")
        budget_prefix = "codex budget: 50% of the 7-day window used"

        cost_idx = self._byte_index(result.stdout, cost_line)
        budget_idx = self._byte_index(result.stdout, budget_prefix)
        self.assertGreater(budget_idx, cost_idx)
        self.assertIn(b"no fitted per-gate cost", result.stdout)


class TestExecutablePreDispatchBudgetBeforePostRunCost(CodexReviewExecutableTestCase):
    """a rollout ALREADY sitting in the sessions root (used 90, reset
    tomorrow) before the wrapper even starts -> its budget line prints
    before dispatch, at a byte offset before the post-run cost line."""

    def test_pre_dispatch_budget_line_precedes_post_run_cost_line(self):
        os.makedirs(self.sessions_root, exist_ok=True)
        now = time.time()
        pre_path = os.path.join(self.sessions_root, "rollout-pre.jsonl")
        _write_jsonl(pre_path,
                     [_event_line(_calibrated_block(90.0, now + 86400))])

        rollout = self.shim_rollout_path()
        result = _run_wrapper(
            self.base_args(),
            env=self.env(output_mode="conforming",
                          extra={"CODEX_SHIM_ROLLOUT": rollout}))

        self.assertEqual(result.returncode, 0, result.stderr)
        pre_dispatch_prefix = "codex budget: 90% of the 7-day window used"
        cost_prefix = "codex run used 100,000 uncached input tokens"

        pre_idx = self._byte_index(result.stdout, pre_dispatch_prefix)
        cost_idx = self._byte_index(result.stdout, cost_prefix)
        self.assertLess(pre_idx, cost_idx)


class TestExecutableUncalibratedModelNoCostEstimate(CodexReviewExecutableTestCase):
    """the shim's rollout carries a model with no fit
    ("other-model") -> the "no fitted rate" line names it, and no
    "≈" cost figure appears anywhere in stdout."""

    def test_no_fitted_rate_message_names_the_model(self):
        rollout = self.shim_rollout_path()
        result = _run_wrapper(
            self.base_args(),
            env=self.env(output_mode="conforming",
                          extra={"CODEX_SHIM_ROLLOUT": rollout,
                                 "CODEX_SHIM_MODEL": "other-model"}))

        self.assertEqual(result.returncode, 0, result.stderr)
        expected = ("codex run used 100,000 uncached input tokens on "
                    "other-model; no fitted rate for plan "
                    "test-plan / that model, so no "
                    "cost estimate")
        self.assertIn(expected.encode("utf-8"), result.stdout)
        # "No cost estimate" is a claim about the COST line specifically --
        # the post-run BUDGET line is keyed on the pinned CODEX_MODEL, not
        # on what model this particular run used, so whenever a calibration
        # exists for that pin it can show a gates-left range with its own
        # "≈". Only the cost line's own
        # "≈...% of the window" form must be absent.
        self.assertNotIn(b"codex run cost (estimate): \xe2\x89\x88",
                          result.stdout)


class TestExecutableNoRolloutWritten(CodexReviewExecutableTestCase):
    """`CODEX_SHIM_ROLLOUT` unset -> no rollout file is written, and
    the wrapper still exits 0 for a conforming report, printing the "not
    found / not attributable" line."""

    def test_not_found_message_and_exit_zero(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="conforming"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            b"codex run log not found (or not attributable); per-run "
            b"cost not measured", result.stdout)


class TestExecutableMeterMovedNeverAppears(CodexReviewExecutableTestCase):
    """the retired `read_run_delta`-based "codex meter moved" line
    must never appear in stdout, with or without a shim rollout."""

    def test_old_meter_moved_phrase_is_gone_without_rollout(self):
        result = _run_wrapper(self.base_args(), env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"codex meter moved", result.stdout)

    def test_old_meter_moved_phrase_is_gone_with_rollout(self):
        rollout = self.shim_rollout_path()
        result = _run_wrapper(
            self.base_args(),
            env=self.env(output_mode="conforming",
                          extra={"CODEX_SHIM_ROLLOUT": rollout}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"codex meter moved", result.stdout)

    def test_old_meter_moved_phrase_absent_when_step2_has_a_meter(self):
        """the previous positive control only proved the phrase is gone
        when Step 2 found NO meter at all (meter_path is None), so the
        retired `if meter_path is not None: ...` branch could have been
        silently restored without this suite noticing -- it would never run.
        Seed a calibrated rollout BEFORE dispatch (as the pre-seeded-rollout test does) so Step 2
        DOES have a `meter_path`, giving that branch something to print if
        it were still there."""
        os.makedirs(self.sessions_root, exist_ok=True)
        now = time.time()
        pre_path = os.path.join(self.sessions_root, "rollout-pre.jsonl")
        _write_jsonl(pre_path,
                     [_event_line(_calibrated_block(90.0, now + 86400))])
        _touch_mtime(pre_path, now - 60)

        result = _run_wrapper(
            self.base_args(),
            env=self.env(output_mode="conforming",
                          extra={"CODEX_SHIM_ROLLOUT": self.shim_rollout_path()}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(b"codex meter moved", result.stdout, result.stdout.decode())


class TestExecutableOldSameCwdRolloutNotCounted(CodexReviewExecutableTestCase):
    """a PRE-EXISTING rollout with the SAME cwd as the dispatch
    target, a session timestamp 1h before dispatch, and its mtime forced
    (`os.utime`) to NOW + 60s -- i.e. it passes the mtime filter on its
    own -- must still be excluded by the timestamp filter, leaving only
    the shim's own rollout as the single match. The cost line must report
    the shim's 100,000 tokens, not `None` (which a mtime-only filter, or a
    filter that counts both files as a match, would both produce).

    Dispatches a Plan Review: the Code gate's cwd is now a fresh temp path,
    so a seeded same-cwd rollout is impossible there and the test would
    pass vacuously."""

    def test_old_rollout_with_forced_future_mtime_is_excluded(self):
        os.makedirs(self.sessions_root, exist_ok=True)
        now = time.time()
        target_cwd = os.path.realpath(self.repo)
        old_path = os.path.join(self.sessions_root, "rollout-old.jsonl")
        _write_jsonl(old_path, [
            _session_meta_line(target_cwd, _iso_utc(now - 3600)),
            _turn_context_line("gpt-6-astra"),
            _token_count_line(5000, 0),
        ])
        _touch_mtime(old_path, now + 60)

        rollout = self.shim_rollout_path()
        plan_path = os.path.join(self.root, "plan.md")
        with open(plan_path, "w", encoding="utf-8") as handle:
            handle.write("# A plan\n\n- Step 1: do x\n" +
                         _PLAN_CLASSIFICATION_SUFFIX)
        result = _run_wrapper(
            self.doc_args("--plan-review", [plan_path]),
            env=self.env(output_mode="conforming",
                          extra={"CODEX_SHIM_ROLLOUT": rollout}))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"100,000 uncached input tokens", result.stdout)
        self.assertNotIn(
            b"codex run log not found (or not attributable)", result.stdout)


class TestExecutableMergeWithAttributesAttributesToScratchWorktree(CodexReviewExecutableTestCase):
    """under `--merge-with`, the cost line still appears (the
    no-fitted-rate form, since no fit ships), attributed
    to the scratch worktree -- `run_cost`'s `cwd` argument is
    `dispatch_target`, the merge worktree, not `--repo`, and the shim's own
    `os.getcwd()` (its `session_meta.cwd`) is that same scratch worktree,
    so the match only succeeds if attribution followed the dispatch
    target rather than the original `--repo`."""

    def setUp(self):
        super().setUp()
        merge_repo = os.path.join(self.root, "repo-merge-cost")
        os.makedirs(merge_repo)
        self.repo = _make_merge_fixture_repo(merge_repo, upstream_moves=True,
                                              conflict=False)

    def test_cost_line_appears_under_merge_with(self):
        rollout = self.shim_rollout_path()
        args = ["--base", "main", "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root, "--merge-with", "main"]
        result = _run_wrapper(
            args, env=self.env(output_mode="conforming",
                                extra={"CODEX_SHIM_ROLLOUT": rollout}))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"codex run used 100,000 uncached input tokens",
                      result.stdout)

        dump = self.shim_dump()
        self.assertNotEqual(os.path.realpath(dump["cwd"]),
                             os.path.realpath(self.repo))


class TestExecutableSessionsRootDefault(CodexReviewExecutableTestCase):
    """Step 2's `--sessions-root`-absent default now resolves through
    `codex_review_lib.default_sessions_root()` (CODEX_HOME-aware), not a
    hardcoded `~/.codex/sessions` string. Pointing `CODEX_HOME` at a
    sandbox directory holding a calibrated rollout and omitting
    `--sessions-root` entirely proves the wrapper actually calls that
    function rather than its own hardcoded fallback. The rollout has no
    fitted rate, so the line reports the used percent only."""

    def test_omitted_sessions_root_honours_codex_home(self):
        codex_home = os.path.join(self.root, "fake-codex-home")
        sessions_root = os.path.join(codex_home, "sessions")
        os.makedirs(sessions_root)
        now = time.time()
        _write_jsonl(os.path.join(sessions_root, "rollout-a.jsonl"),
                     [_event_line(_calibrated_block(90.0, now + 86400))])

        args = ["--base", "base", "--repo", self.repo, "-o", self.output_path]
        env = self.env(output_mode="conforming", extra={"CODEX_HOME": codex_home})

        result = _run_wrapper(args, env=env)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"codex budget: 90% of the 7-day window used",
                      result.stdout)


CALIBRATED_LAUNCHER = '''
import importlib.machinery
import importlib.util
import sys

wrapper_path, plan_type, model, points, median, p90 = sys.argv[1:7]
loader = importlib.machinery.SourceFileLoader("claude_codex_review", wrapper_path)
spec = importlib.util.spec_from_loader("claude_codex_review", loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)
module.codex_review_lib.CALIBRATIONS[(plan_type, 10080, model)] = {
    "points_per_million_uncached": float(points),
    "gate_points": (float(median), float(p90)),
}
sys.exit(module.main(sys.argv[7:]))
'''


class TestCalibratedEstimateLinesWithASyntheticFit(CodexReviewExecutableTestCase):
    """The calibrated print paths stay covered although no fit ships: the
    test injects one into the library for a launcher process and checks the
    two estimate lines the wrapper prints from it."""

    def test_estimate_lines_come_from_the_injected_fit(self):
        plan_type = "test-plan"  # the shim's rollout plan
        model = "gpt-5.6-terra"  # the Code gate's model
        points_per_million, median, p90 = 10.0, 0.5, 1.0
        used_percent = 50.0  # the shim's default meter reading
        uncached_tokens = 100000  # shim input 200,000 minus 100,000 cached
        cost = uncached_tokens / 1e6 * points_per_million
        headroom = 100.0 - used_percent
        low, high = int(headroom // p90), int(headroom // median)

        launcher = os.path.join(self.root, "calibrated_launcher.py")
        with open(launcher, "w", encoding="utf-8") as handle:
            handle.write(CALIBRATED_LAUNCHER)
        env = self.env(output_mode="conforming",
                       extra={"CODEX_SHIM_ROLLOUT": self.shim_rollout_path(),
                              "CODEX_SHIM_MODEL": model,
                              "PYTHONIOENCODING": "utf-8"})
        result = subprocess.run(
            [sys.executable, launcher, WRAPPER, plan_type, model,
             str(points_per_million), str(median), str(p90)] +
            self.base_args(),
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30)

        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.decode("utf-8")
        self.assertIn("codex budget (estimate): ≈%d–%d review gates left" %
                      (low, high), out)
        self.assertIn("codex run cost (estimate): ≈%.1f%% of the window "
                      "(100,000 uncached input tokens on %s, rate from the "
                      "calibration table)" % (cost, model), out)


class TestSigtermStillRemovesTheSnapshot(CodexReviewExecutableTestCase):
    """SIGTERM's default action skips the
    wrapper's `finally`, so cancelling a Code Review left the snapshot
    registered -- and a second worktree arms worktree-guard's rules. The
    wrapper now turns SIGTERM into SystemExit(143). The shim's `hang` mode
    sleeps after writing its dump, so the signal lands mid-dispatch."""

    def test_sigterm_mid_dispatch_exits_143_and_removes_the_snapshot(self):
        proc = subprocess.Popen(
            [sys.executable, WRAPPER] + self.base_args(),
            env=self.env(output_mode="hang"), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        deadline = time.time() + 20
        while not self.shim_was_run() and time.time() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.shim_was_run(), "shim never started")
        proc.send_signal(signal.SIGTERM)
        _, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, err)
        listing = subprocess.run(["git", "worktree", "list", "--porcelain"],
                                 cwd=self.repo, capture_output=True, text=True)
        self.assertEqual(listing.stdout.count("worktree "), 1, listing.stdout)


class TestBriefsStateTheValidatorRules(unittest.TestCase):
    """Each Codex brief's EXECUTION section tells the reviewer the
    rules the report validator enforces, so a rejection is never a surprise.
    `review-brief.md` also says which tree the reviewer is standing in."""

    HEADING = "## EXECUTION IS A REQUIRED FIELD, NOT A HOPE"

    def _section(self, name):
        text = open(os.path.join(HERE, name), encoding="utf-8").read()
        sections = re.split(r"(?m)^(?=## )", text)
        matches = [s for s in sections
                   if s.splitlines()[0].strip() == self.HEADING]
        self.assertEqual(len(matches), 1, name)
        return " ".join(matches[0].split())

    def test_every_brief_states_the_rejection_rules(self):
        for name in ("review-brief.md", "plan-brief.md", "design-brief.md"):
            section = self._section(name)
            self.assertIn("is rejected as", section, name)
            self.assertIn("indistinguishable from a broken gate",
                          section, name)
            # The two rules the validator enforces, each on its own
            # assertion so dropping either one from a brief reddens.
            self.assertIn("`APPROVE` alongside a `BLOCKING` finding",
                          section, name)
            self.assertIn("a blank entry in `commands`", section, name)

    def test_review_brief_names_the_scratch_worktree(self):
        section = self._section("review-brief.md")
        self.assertIn("scratch worktree", section)
        self.assertIn("uncommitted", section.lower())


class TestExecutableStaleLocalBase(CodexReviewExecutableTestCase):
    """Code Review's `--base main` resolved the LOCAL `main`, which
    goes stale when the checkout has not fast-forwarded since a peer merge,
    so the diff silently carried other PRs' changes.

    Fixture: `repo` on `feature` (one commit, `branch.txt`) forked from the
    common ancestor, local `main` at that ancestor, tracking `origin/main`.
    With `upstream_moves`, a peer commit (`shared.txt`) lands on origin
    AFTER the clone, so local `main` -- and the local `origin/main` ref,
    until a fetch -- is strictly behind. `head_has_peer` merges that peer
    commit into `feature`: the stale-base shape, where the stale base makes the
    diff carry it. Without it the branch forked before the peer merge and
    `main...HEAD` is branch-only, so it must dispatch (Code Review finding
    1). `drop_tracking` deletes `refs/remotes/origin/main` afterwards, so
    only the configured upstream says main tracks origin.
    """

    def make_repo(self, upstream_moves, head_has_peer=False, drop_tracking=False,
                  narrow_refspec=False):
        merge_repo = os.path.join(self.root, "repo-stale")
        os.makedirs(merge_repo)
        self.repo = _make_merge_fixture_repo(merge_repo, upstream_moves=False)
        _git("checkout", "-q", "-b", "feature", cwd=self.repo)
        _git("branch", "-f", "main", "origin/main", cwd=self.repo)
        if upstream_moves:
            seed = os.path.join(merge_repo, "seed")
            with open(os.path.join(seed, "shared.txt"), "w", encoding="utf-8") as handle:
                handle.write("start\npeer change\n")
            _git("add", "-A", cwd=seed)
            _git("commit", "-q", "-m", "peer merge", cwd=seed)
            _git("push", "-q", os.path.join(merge_repo, "remote"), "main", cwd=seed)
            self.peer_sha = _git("rev-parse", "HEAD", cwd=seed).stdout.strip()
        if head_has_peer:
            _git("fetch", "-q", "origin", cwd=self.repo)
            _git("merge", "-q", "--no-edit", "origin/main", cwd=self.repo)
        if drop_tracking:
            _git("update-ref", "-d", "refs/remotes/origin/main", cwd=self.repo)
            # Non-vacuous: the configured upstream is the ONLY tracking signal.
            self.assertEqual("origin", _git("config", "branch.main.remote",
                                            cwd=self.repo).stdout.strip())
        if narrow_refspec:
            # A plain `fetch origin main` updates no tracking ref under this,
            # so only an explicit refspec brings origin/main back.
            _git("config", "remote.origin.fetch",
                 "+refs/heads/other:refs/remotes/origin/other", cwd=self.repo)
        self.local_main = _git("rev-parse", "main", cwd=self.repo).stdout.strip()

    def run_base(self, base):
        args = ["--base", base, "--repo", self.repo, "-o", self.output_path,
                "--sessions-root", self.sessions_root]
        return _run_wrapper(args, env=self.env(output_mode="conforming"))

    def assert_refused(self, result):
        stderr = result.stderr.decode("utf-8", "replace")
        self.assertNotEqual(result.returncode, 0, stderr)
        self.assertFalse(self.shim_was_run(), "a stale base must dispatch nothing")
        self.assertIn("--base main is behind origin/main", stderr)
        self.assertIn("--base origin/main", stderr)
        # The refusal leaves local main where it was: it reports, never moves.
        self.assertEqual(self.local_main,
                         _git("rev-parse", "main", cwd=self.repo).stdout.strip())

    def test_stale_base_under_a_head_carrying_the_peer_refuses(self):
        self.make_repo(upstream_moves=True, head_has_peer=True)
        self.assert_refused(self.run_base("main"))

    def test_pruned_tracking_ref_is_refetched_not_skipped(self):
        self.make_repo(upstream_moves=True, head_has_peer=True, drop_tracking=True)
        self.assert_refused(self.run_base("main"))

    def test_narrow_refspec_still_refetches_the_tracking_ref(self):
        self.make_repo(upstream_moves=True, head_has_peer=True, drop_tracking=True,
                       narrow_refspec=True)
        self.assert_refused(self.run_base("main"))

    def test_stale_base_before_the_peer_merge_dispatches_branch_only(self):
        self.make_repo(upstream_moves=True, head_has_peer=False)
        result = self.run_base("main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())
        with open(self.dump_path + ".stdin", "rb") as handle:
            stdin_bytes = handle.read()
        self.assertIn(b"branch work", stdin_bytes)
        self.assertNotIn(b"peer change", stdin_bytes)

    def test_current_local_base_still_dispatches(self):
        self.make_repo(upstream_moves=False)
        result = self.run_base("main")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())

    def test_origin_base_is_fetched_so_the_escape_hatch_is_current(self):
        # The local origin/main ref is stale until a fetch; without one the
        # escape hatch would review against the same stale base.
        self.make_repo(upstream_moves=True)
        result = self.run_base("origin/main")
        stdout = result.stdout.decode("utf-8", "replace")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_was_run())
        self.assertIn("base=origin/main@%s " % self.peer_sha, stdout)


class TestSetterMatchesTheWrapper(unittest.TestCase):
    """`--set codex_review.models.<gate>` and the wrapper must accept the same
    model names: a value the setter stores but the wrapper refuses would
    surface only at dispatch."""

    CASES = ["my-model", "gpt-5.6-terra", "a", "", " ", "a b", "a\tb", "a\nb",
             "-x", "--model", "-", "x-", True, False, 1, 0, 1.5, [], ["m"], {}]

    @classmethod
    def setUpClass(cls):
        lib_dir = os.path.realpath(os.path.join(HERE, os.pardir, os.pardir, "lib"))
        sys.path.insert(0, lib_dir)
        try:
            import setup_config
        finally:
            sys.path.remove(lib_dir)
        cls.setup_config = setup_config

    def test_the_two_validators_agree_on_every_case(self):
        for value in self.CASES:
            wrapper = codex_review_lib.valid_model_name(value)
            if isinstance(value, bool) or not isinstance(value, str):
                # `--set` hands the validator what _type_check let through: a
                # string or None, never another type.
                setter = False
            else:
                setter = self.setup_config._codex_model_or_null(value, None)[0]
            self.assertEqual(setter, wrapper, "disagree on %r" % (value,))

    def test_null_is_accepted_by_the_setter(self):
        self.assertEqual(self.setup_config._codex_model_or_null(None, None), (True, None))

    def test_the_setter_itself_refuses_a_non_string(self):
        for value in (True, False, 1, 1.5, [], {}):
            self.assertFalse(self.setup_config._codex_model_or_null(value, None)[0], repr(value))


class TestArchitectureArgv(unittest.TestCase):
    def setUp(self):
        self.argv = codex_review_lib.build_architecture_argv(
            "/r", "/s.json", "/o.json", "the prompt", codex_review_lib.CODEX_MODEL)

    def test_exact_list(self):
        self.assertEqual(self.argv, [
            "codex", "exec", "-C", "/r", "-m", codex_review_lib.CODEX_MODEL,
            "-c", 'model_reasoning_effort="max"',
            "-c", 'sandbox_mode="read-only"',
            "--output-schema", "/s.json", "-o", "/o.json", "the prompt"])

    def test_c_values_and_prompt_last(self):
        self.assertTrue(self.argv)
        self.assertEqual(self.argv[-1], "the prompt")
        values = [self.argv[i + 1] for i, a in enumerate(self.argv)
                  if a == "-c"]
        self.assertEqual(len(values), 2)
        self.assertIn("max", values[0])
        self.assertIn("read-only", values[1])

    def test_constants(self):
        self.assertEqual(codex_review_lib.ARCHITECTURE_EFFORT, "max")
        self.assertEqual(codex_review_lib.ARCHITECTURE_SANDBOX, "read-only")
        self.assertEqual(codex_review_lib.model_for_gate("architecture"),
                         codex_review_lib.CODEX_MODEL)


class TestResumeArgv(unittest.TestCase):
    def setUp(self):
        self.argv = codex_review_lib.build_resume_argv(
            "sess-1", "/s.json", "/o.json", "the prompt",
            codex_review_lib.CODEX_MODEL)

    def test_exact_list(self):
        self.assertEqual(self.argv, [
            "codex", "exec", "resume", "sess-1",
            "-m", codex_review_lib.CODEX_MODEL,
            "-c", 'model_reasoning_effort="max"',
            "-c", 'sandbox_mode="read-only"',
            "--output-schema", "/s.json", "-o", "/o.json", "the prompt"])

    def test_resume_repasses_effort_and_sandbox(self):
        # Resume silently reverts both unless each -c pair is re-passed.
        self.assertTrue(self.argv)
        self.assertEqual(self.argv[-1], "the prompt")
        values = [self.argv[i + 1] for i, a in enumerate(self.argv)
                  if a == "-c"]
        self.assertEqual(len(values), 2)
        self.assertIn('model_reasoning_effort="max"', values)
        self.assertIn('sandbox_mode="read-only"', values)

    def test_no_sandbox_or_cwd_flags(self):
        self.assertTrue(self.argv)
        self.assertNotIn("-s", self.argv)
        self.assertNotIn("-C", self.argv)


class TestWeeklyReading(unittest.TestCase):
    NOW = 1000.0

    def win(self, minutes, used, resets=5000.0):
        return {"window_minutes": minutes, "used_percent": used,
                "resets_at": resets}

    def test_unreadable_weekly_primary_does_not_mask_weekly_secondary(self):
        # Code Review finding: a malformed or expired weekly primary used to
        # return None outright, hiding a readable 95% secondary and letting
        # --usage exit 0 instead of 3.
        for primary in (self.win(10080, "broken"),
                        self.win(10080, 50, resets=500.0),
                        self.win(10080, float("nan"))):
            got = codex_review_lib.weekly_reading(
                {"primary": primary, "secondary": self.win(10080, 95)},
                self.NOW)
            self.assertIsNotNone(got, primary)
            self.assertEqual(got["used_percent"], 95.0, primary)

    def test_picks_secondary_when_primary_is_short(self):
        got = codex_review_lib.weekly_reading(
            {"primary": self.win(300, 10), "secondary": self.win(10080, 95)},
            self.NOW)
        self.assertEqual(got, {"used_percent": 95.0, "resets_at": 5000.0,
                               "window_minutes": 10080})
        self.assertIsInstance(got["used_percent"], float)

    def test_picks_primary_when_weekly(self):
        got = codex_review_lib.weekly_reading(
            {"primary": self.win(10080, 40), "secondary": self.win(300, 90)},
            self.NOW)
        self.assertEqual(got["used_percent"], 40.0)

    def test_none_cases(self):
        w = codex_review_lib.weekly_reading
        weekly = lambda **kw: {"primary": dict(self.win(10080, 5), **kw)}
        self.assertIsNone(w(None, self.NOW))
        self.assertIsNone(w("x", self.NOW))
        self.assertIsNone(w({"primary": self.win(300, 5),
                             "secondary": self.win(60, 5)}, self.NOW))
        self.assertIsNone(w(weekly(resets_at=900.0), self.NOW))
        self.assertIsNone(w(weekly(resets_at=self.NOW), self.NOW))
        self.assertIsNone(w(weekly(used_percent=True), self.NOW))
        self.assertIsNone(w(weekly(used_percent="95"), self.NOW))
        self.assertIsNone(w(weekly(resets_at=True), self.NOW))
        self.assertIsNone(w(weekly(resets_at="5000"), self.NOW))
        self.assertIsNone(w({"primary": {"window_minutes": 10080,
                                         "resets_at": 5000.0}}, self.NOW))
        self.assertIsNone(w({"primary": {"window_minutes": 10080,
                                         "used_percent": 5}}, self.NOW))

    def test_malformed_windows_never_raise(self):
        w = codex_review_lib.weekly_reading
        self.assertIsNone(w({}, self.NOW))
        self.assertIsNone(w({"primary": None, "secondary": 7}, self.NOW))
        got = w({"primary": "junk", "secondary": self.win(10080, 12)},
                self.NOW)
        self.assertEqual(got["used_percent"], 12.0)


class TestFormatWeekly(unittest.TestCase):
    def test_none_says_unknown(self):
        out = codex_review_lib.format_weekly(None)
        self.assertEqual(
            out, "codex weekly: unknown (no readable 7-day window)")

    def test_reading_line(self):
        reading = {"used_percent": 95.0, "resets_at": 5000.0,
                   "window_minutes": 10080}
        out = codex_review_lib.format_weekly(reading)
        stamp = time.strftime("%a %m-%d %H:%M", time.localtime(5000.0))
        self.assertEqual(out, "codex weekly: 95.0%% used, resets %s" % stamp)


ARCHITECTURE_SCHEMA_PATH = os.path.join(
    os.path.dirname(HERE), "build-lens",
    "architecture-review-report.schema.json")
ARCHITECTURE_CASES_PATH = os.path.join(
    HERE, "tests", "semantic_cases_architecture.json")


def _load_architecture_schema():
    with open(ARCHITECTURE_SCHEMA_PATH, encoding="utf-8") as handle:
        return json.load(handle)


def _load_architecture_cases():
    with open(ARCHITECTURE_CASES_PATH, encoding="utf-8") as handle:
        return json.load(handle)


class TestArchitectureSchema(unittest.TestCase):
    def test_schema_parses(self):
        self.assertIsInstance(_load_architecture_schema(), dict)

    def test_strict_mode_every_object_node(self):
        """Every object node lists all its properties in `required` and
        sets additionalProperties false (OpenAI strict mode)."""
        objects = []

        def walk(node, path):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    objects.append(path)
                    props = node.get("properties")
                    self.assertIsInstance(props, dict, path)
                    self.assertEqual(sorted(node.get("required", [])),
                                     sorted(props.keys()), path)
                    self.assertIs(node.get("additionalProperties"), False,
                                  path)
                for key, child in node.items():
                    walk(child, path + "/" + key)
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    walk(child, "%s[%d]" % (path, index))

        walk(_load_architecture_schema(), "#")
        # root + context_requests, lens_verdicts, principles, changes,
        # executed items: a walk that found fewer proves nothing.
        self.assertGreaterEqual(len(objects), 6)

    def test_every_case_report_is_schema_valid(self):
        schema = _load_architecture_schema()
        for case in _load_architecture_cases():
            violations = review_lib.validate_against_schema(
                schema, case["report"])
            self.assertEqual(list(violations), [], case["name"])


class TestArchitectureSemanticCases(unittest.TestCase):
    def test_cases_accept_and_reject(self):
        cases = _load_architecture_cases()
        self.assertGreater(len(cases), 0)
        for case in cases:
            got = codex_review_lib.architecture_violation(
                case["report"], case["round"], case["moment"])
            if case["expect"] == "accept":
                self.assertIsNone(got, case["name"])
            else:
                self.assertEqual(case["expect"], "reject", case["name"])
                self.assertIsInstance(got, str, case["name"])
                self.assertNotEqual(got.strip(), "", case["name"])

    def test_cases_cover_every_status_moment_and_rule(self):
        cases = _load_architecture_cases()
        accepted = set((c["report"]["status"], c["moment"])
                       for c in cases if c["expect"] == "accept")
        for status in ("final", "needs_context"):
            for moment in ("design", "final"):
                self.assertIn((status, moment), accepted)
        rejected = " ".join(c["reason"] for c in cases
                            if c["expect"] == "reject")
        for rule in range(1, 9):
            self.assertIn("rule %d:" % rule, rejected)

    def test_lens_constants(self):
        self.assertEqual(codex_review_lib.CORE_LENSES,
                         ("coherence", "elegance", "rebuild", "unaccounted"))
        self.assertEqual(
            codex_review_lib.DESIGN_LENSES,
            ("wrong-premise", "simpler-alternative", "silent-failure",
             "already-exists", "seams"))

    def test_non_string_content_is_blank(self):
        accepted = [c for c in _load_architecture_cases()
                    if c["name"] == "final_design"][0]
        report = json.loads(json.dumps(accepted["report"]))
        report["principles"][0]["why"] = 5
        got = codex_review_lib.architecture_violation(report, 1, "design")
        self.assertIsInstance(got, str)


# --- Architecture-review wrapper flags: parse rules, --check-brief,
# --usage, --validate-report (Task 3). None of these needs `codex` on PATH.


class TestArchitectureParse(CodexReviewExecutableTestCase):
    """Every parse rule exits 2 and runs nothing."""

    def setUp(self):
        super().setUp()
        self.brief = os.path.join(self.root, "brief.md")
        with open(self.brief, "w", encoding="utf-8") as handle:
            handle.write("# Brief\n\nA clean brief.\n")
        self.report = os.path.join(self.root, "arch-report.json")
        self.answers = os.path.join(self.root, "answers.md")

    def assert_usage_error(self, args, fragment=None):
        result = _run_wrapper(args, env=self.env(output_mode="conforming"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())
        if fragment is not None:
            self.assertIn(fragment.encode("utf-8"), result.stderr)

    def repo_out(self):
        return ["--repo", self.repo, "-o", self.output_path]

    def test_two_family_flags_conflict(self):
        self.assert_usage_error(["--usage", "--check-brief", self.brief])
        self.assert_usage_error(["--check-brief", self.brief,
                                 "--validate-report", self.report,
                                 "--round", "1", "--moment", "design"])
        self.assert_usage_error(["--architecture-review", self.brief,
                                 "--moment", "design", "--usage"]
                                + self.repo_out())
        self.assert_usage_error(["--resume", "abc", "--answers", self.answers,
                                 "--round", "2", "--moment", "design",
                                 "--architecture-review", self.brief]
                                + self.repo_out())

    def test_family_flag_does_not_combine_with_other_modes(self):
        for family in (["--check-brief", self.brief], ["--usage"]):
            for other in (["--base", "base"], ["--plan", self.brief],
                          ["--plan-review", self.brief],
                          ["--design-review", self.brief, self.brief],
                          ["--merge-with", "main"]):
                self.assert_usage_error(family + other + self.repo_out())

    def test_empty_values_are_usage_errors(self):
        self.assert_usage_error(["--check-brief", ""], "empty")
        self.assert_usage_error(["--validate-report", "", "--round", "1",
                                 "--moment", "design"], "empty")
        self.assert_usage_error(["--architecture-review", "",
                                 "--moment", "design"] + self.repo_out(),
                                "empty")
        self.assert_usage_error(["--resume", "", "--answers", self.answers,
                                 "--round", "2", "--moment", "design"]
                                + self.repo_out(), "empty")

    def test_validate_report_needs_round_and_moment(self):
        self.assert_usage_error(["--validate-report", self.report,
                                 "--moment", "design"], "--round")
        self.assert_usage_error(["--validate-report", self.report,
                                 "--round", "1"], "--moment")

    def test_architecture_review_requirements(self):
        base = ["--architecture-review", self.brief]
        self.assert_usage_error(base + ["--repo", self.repo, "-o",
                                        self.output_path], "--moment")
        self.assert_usage_error(base + ["--moment", "design", "-o",
                                        self.output_path], "--repo")
        self.assert_usage_error(base + ["--moment", "design", "--repo",
                                        self.repo], "-o")
        self.assert_usage_error(base + ["--moment", "design", "--round", "1"]
                                + self.repo_out(), "--round")

    def test_resume_requirements(self):
        full = ["--resume", "abc", "--answers", self.answers,
                "--round", "2", "--moment", "design"] + self.repo_out()

        def without(flag):
            out = list(full)
            index = out.index(flag)
            del out[index:index + 2]
            return out

        self.assert_usage_error(without("--answers"), "--answers")
        self.assert_usage_error(without("--round"), "--round")
        self.assert_usage_error(without("--moment"), "--moment")
        self.assert_usage_error(without("--repo"), "--repo")
        self.assert_usage_error(without("-o"), "-o")
        bad_round = list(full)
        bad_round[bad_round.index("--round") + 1] = "1"
        self.assert_usage_error(bad_round, "--round")

    def test_check_brief_and_usage_take_no_round_moment_answers(self):
        for family in (["--check-brief", self.brief], ["--usage"]):
            for extra in (["--round", "1"], ["--moment", "design"],
                          ["--answers", self.answers]):
                self.assert_usage_error(family + extra)

    def test_existing_modes_now_enforce_repo_and_output_in_main(self):
        self.assert_usage_error(["--base", "base", "-o", self.output_path],
                                "--repo")
        self.assert_usage_error(["--base", "base", "--repo", self.repo],
                                "-o")


class TestCheckBrief(CodexReviewExecutableTestCase):
    def write_brief(self, text):
        path = os.path.join(self.root, "brief.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def run_check(self, path):
        return _run_wrapper(["--check-brief", path],
                            env=self.env(output_mode="conforming"))

    def test_clean_brief_passes(self):
        result = self.run_check(self.write_brief("# Brief\n\nClean.\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"brief check: OK", result.stdout)
        self.assertNotIn(b"warning", result.stdout)
        self.assertFalse(self.shim_was_run())

    def test_answer_key_heading_is_refused(self):
        result = self.run_check(self.write_brief(
            "# Brief\n\n## Round 2 findings\n\nstuff\n"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"Round 2 findings", result.stderr)
        self.assertNotIn(b"brief check: OK", result.stdout)

    def test_prose_blocking_warns_and_exits_zero(self):
        result = self.run_check(self.write_brief(
            "# Brief\n\nThis is BLOCKING work.\n"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"brief warning: line 3: prose 'BLOCKING'",
                      result.stdout)
        self.assertIn(b"brief check: OK with 1 warning(s)", result.stdout)

    def test_missing_and_empty_files_exit_one(self):
        result = self.run_check(os.path.join(self.root, "nope.md"))
        self.assertEqual(result.returncode, 1, result.stderr)
        result = self.run_check(self.write_brief("  \n"))
        self.assertEqual(result.returncode, 1, result.stderr)


class TestUsageFlag(CodexReviewExecutableTestCase):
    def run_usage(self, block, secondary_only=False):
        if block is not None:
            _write_jsonl(os.path.join(self.sessions_root, "rollout-a.jsonl"),
                         [_event_line(block)])
        return _run_wrapper(["--usage", "--sessions-root", self.sessions_root],
                            env=self.env())

    def weekly(self, used, key="primary"):
        return {key: {"used_percent": used, "window_minutes": 10080,
                      "resets_at": time.time() + 86400}}

    def setUp(self):
        super().setUp()
        os.makedirs(self.sessions_root)

    def test_fifty_percent_exits_zero(self):
        result = self.run_usage(self.weekly(50.0))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"codex weekly: 50.0% used", result.stdout)
        self.assertFalse(self.shim_was_run())

    def test_ninety_percent_exits_three_with_stop_line(self):
        result = self.run_usage(self.weekly(90.0))
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b"codex weekly: 90.0% used", result.stdout)
        self.assertIn(b"STOP: weekly Codex usage at or above 90%",
                      result.stdout)

    def test_just_under_ninety_exits_zero(self):
        result = self.run_usage(self.weekly(89.9))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ninety_five_as_secondary_exits_three(self):
        block = self.weekly(95.0, key="secondary")
        block["primary"] = {"used_percent": 10.0, "window_minutes": 300,
                            "resets_at": time.time() + 3600}
        result = self.run_usage(block)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn(b"codex weekly: 95.0% used", result.stdout)

    def test_no_weekly_window_is_unknown_and_exits_zero(self):
        result = self.run_usage({"primary": {"used_percent": 99.0,
                                             "window_minutes": 300,
                                             "resets_at": time.time() + 3600}})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"unknown", result.stdout)

    def test_expired_window_is_unknown_and_exits_zero(self):
        block = self.weekly(99.0)
        block["primary"]["resets_at"] = time.time() - 60
        result = self.run_usage(block)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"unknown", result.stdout)

    def test_absent_root_is_unknown_and_exits_zero(self):
        shutil.rmtree(self.sessions_root)
        result = self.run_usage(None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"unknown", result.stdout)


class TestValidateReport(CodexReviewExecutableTestCase):
    def case(self, name):
        for entry in _load_architecture_cases():
            if entry["name"] == name:
                return entry
        self.fail("no architecture case named %r" % name)

    def first_case(self, expect):
        for entry in _load_architecture_cases():
            if entry["expect"] == expect:
                return entry
        self.fail("no %s architecture case" % expect)

    def run_validate(self, text, round_, moment):
        path = os.path.join(self.root, "arch-report.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return _run_wrapper(["--validate-report", path, "--round",
                             str(round_), "--moment", moment],
                            env=self.env())

    def test_accepted_case_passes_with_counts(self):
        entry = self.case("final_design")
        report = entry["report"]
        result = self.run_validate(json.dumps(report), entry["round"],
                                   entry["moment"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            ("architecture report: valid (status %s, round %d, %d change(s), "
             "%d context request(s))" % (
                 report["status"], report["round"], len(report["changes"]),
                 len(report["context_requests"]))).encode("utf-8"),
            result.stdout)
        self.assertFalse(self.shim_was_run())

    def test_every_accept_case_passes_and_every_reject_that_is_schema_valid_fails(self):
        for entry in _load_architecture_cases():
            result = self.run_validate(json.dumps(entry["report"]),
                                       entry["round"], entry["moment"])
            want = 0 if entry["expect"] == "accept" else 1
            self.assertEqual(result.returncode, want,
                             (entry["name"], result.stderr))

    def test_semantic_reject_case_fails(self):
        entry = self.first_case("reject")
        result = self.run_validate(json.dumps(entry["report"]),
                                   entry["round"], entry["moment"])
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(b"architecture report: valid", result.stdout)

    def test_wrong_expected_round_fails(self):
        entry = self.case("final_design")
        result = self.run_validate(json.dumps(entry["report"]), 2,
                                   entry["moment"])
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_non_json_fails(self):
        result = self.run_validate("this is not json", 1, "design")
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_missing_file_fails(self):
        result = _run_wrapper(["--validate-report",
                               os.path.join(self.root, "nope.json"),
                               "--round", "1", "--moment", "design"],
                              env=self.env())
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_schema_invalid_report_fails(self):
        report = json.loads(json.dumps(self.case("final_design")["report"]))
        del report["lens_verdicts"]
        result = self.run_validate(json.dumps(report), 1, "design")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"lens_verdicts", result.stderr)

    def test_non_object_json_fails(self):
        result = self.run_validate("[1, 2]", 1, "design")
        self.assertEqual(result.returncode, 1, result.stderr)



ARCHITECTURE_BRIEF_FILE = os.path.join(HERE, "architecture-brief.md")
DESIGN_LENS_FILE = os.path.join(os.path.dirname(HERE), "build-lens",
                                "design-review.md")


def _make_architecture_wrapper_copy(root, c3_text):
    """A throwaway `tools/codex-review/` holding the wrapper, the lib and a
    caller-chosen `architecture-brief.md` (the real C3 is never edited),
    with `tools/build-lens` and `tools/merge-brief` symlinked beside it so
    the schema and the brief scan still resolve."""
    tools = os.path.join(root, "arch-copy", "tools")
    dest_dir = os.path.join(tools, "codex-review")
    os.makedirs(dest_dir)
    copied_exe = os.path.join(dest_dir, "claude-codex-review")
    shutil.copy2(WRAPPER, copied_exe)
    shutil.copy2(os.path.join(HERE, "codex_review_lib.py"),
                 os.path.join(dest_dir, "codex_review_lib.py"))
    with open(os.path.join(dest_dir, "architecture-brief.md"), "w",
              encoding="utf-8") as handle:
        handle.write(c3_text)
    for name in ("build-lens", "merge-brief"):
        os.symlink(os.path.join(os.path.dirname(HERE), name),
                   os.path.join(tools, name))
    # The wrapper reads the toolkit config (codex_review.models) from lib/.
    os.symlink(os.path.join(os.path.dirname(os.path.dirname(HERE)), "lib"),
               os.path.join(root, "arch-copy", "lib"))
    os.chmod(copied_exe, 0o755)
    return copied_exe


def _session_rollout(path, cwd, session_id, now, mtime):
    _write_jsonl(path, [
        {"type": "session_meta",
         "payload": {"id": session_id, "cwd": cwd,
                     "originator": "codex_exec",
                     "timestamp": _iso_utc(now)}},
        _turn_context_line("gpt-6-astra"),
        _token_count_line(200000, 100000),
    ])
    _touch_mtime(path, mtime)


class TestRolloutSessionId(unittest.TestCase):
    def setUp(self):
        self.cwd = tempfile.mkdtemp(prefix="codex-review-cwd-")
        self.addCleanup(shutil.rmtree, self.cwd, ignore_errors=True)
        self.now = time.time()

    def test_single_attributed_rollout_returns_its_id(self):
        with TempSessionsRoot() as root:
            _session_rollout(os.path.join(root, "rollout-a.jsonl"), self.cwd,
                             "sess-123", self.now, self.now + 1)
            self.assertEqual(
                codex_review_lib.rollout_session_id(root, self.cwd, self.now),
                "sess-123")

    def test_cwd_is_compared_by_realpath(self):
        link = self.cwd + "-link"
        os.symlink(self.cwd, link)
        self.addCleanup(os.unlink, link)
        with TempSessionsRoot() as root:
            _session_rollout(os.path.join(root, "rollout-a.jsonl"), link,
                             "sess-link", self.now, self.now + 1)
            self.assertEqual(
                codex_review_lib.rollout_session_id(root, self.cwd, self.now),
                "sess-link")

    def test_two_matches_are_ambiguous(self):
        with TempSessionsRoot() as root:
            for name, sid in (("rollout-a.jsonl", "one"), ("rollout-b.jsonl", "two")):
                _session_rollout(os.path.join(root, name), self.cwd, sid,
                                 self.now, self.now + 1)
            self.assertIsNone(
                codex_review_lib.rollout_session_id(root, self.cwd, self.now))

    def test_other_cwd_old_mtime_old_timestamp_do_not_match(self):
        other = tempfile.mkdtemp(prefix="codex-review-other-")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        with TempSessionsRoot() as root:
            _session_rollout(os.path.join(root, "rollout-cwd.jsonl"), other,
                             "x", self.now, self.now + 1)
            _session_rollout(os.path.join(root, "rollout-mtime.jsonl"), self.cwd,
                             "y", self.now, self.now - 100)
            _session_rollout(os.path.join(root, "rollout-ts.jsonl"), self.cwd,
                             "z", self.now - 3600, self.now + 1)
            self.assertIsNone(
                codex_review_lib.rollout_session_id(root, self.cwd, self.now))

    def test_absent_root_and_non_string_id_are_none(self):
        self.assertIsNone(codex_review_lib.rollout_session_id(
            "/nonexistent-sessions-root", self.cwd, self.now))
        with TempSessionsRoot() as root:
            path = os.path.join(root, "rollout-a.jsonl")
            _session_rollout(path, self.cwd, "ignored", self.now, self.now + 1)
            lines = open(path, encoding="utf-8").read().splitlines()
            first = json.loads(lines[0])
            first["payload"]["id"] = 7
            lines[0] = json.dumps(first)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            _touch_mtime(path, self.now + 1)
            self.assertIsNone(
                codex_review_lib.rollout_session_id(root, self.cwd, self.now))

    def test_agrees_with_run_cost_on_the_same_file(self):
        with TempSessionsRoot() as root:
            path = os.path.join(root, "rollout-a.jsonl")
            _session_rollout(path, self.cwd, "same", self.now, self.now + 1)
            cost = codex_review_lib.run_cost(root, self.cwd, self.now)
            self.assertIsNotNone(cost)
            self.assertEqual(cost["path"], path)
            self.assertEqual(
                codex_review_lib.rollout_session_id(root, self.cwd, self.now),
                "same")


class ArchitectureWrapperTestCase(CodexReviewExecutableTestCase):
    def setUp(self):
        super().setUp()
        self.brief = os.path.join(self.root, "brief.md")
        with open(self.brief, "w", encoding="utf-8") as handle:
            handle.write("# Brief\n\nObjectives, verbatim.\n")
        self.shim_report = os.path.join(self.root, "shim-report.json")

    def case(self, name):
        for entry in _load_architecture_cases():
            if entry["name"] == name:
                return entry
        self.fail("no architecture case named %r" % name)

    def arch_env(self, report, extra=None, **kwargs):
        """`report` is a dict (written as JSON), a str (written verbatim)."""
        with open(self.shim_report, "w", encoding="utf-8") as handle:
            handle.write(report if isinstance(report, str)
                         else json.dumps(report))
        e = {"CODEX_SHIM_REPORT_FILE": self.shim_report}
        if extra:
            e.update(extra)
        return self.env(extra=e, **kwargs)

    def stdin_bytes(self):
        with open(self.dump_path + ".stdin", "rb") as handle:
            return handle.read()

    def stdout_lines(self, result):
        return result.stdout.decode("utf-8").splitlines()

    def real_output(self):
        return os.path.realpath(self.output_path)


class TestArchitectureDispatch(ArchitectureWrapperTestCase):
    def arch_args(self, moment="design", brief=None):
        return ["--architecture-review", brief or self.brief,
                "--moment", moment, "--repo", self.repo,
                "-o", self.output_path, "--sessions-root", self.sessions_root]

    def expected_prompt(self):
        with open(ARCHITECTURE_BRIEF_FILE, "r", encoding="utf-8") as handle:
            text = handle.read()
        return text.replace("{DESIGN_LENS_FILE}", DESIGN_LENS_FILE)

    def test_round_one_argv_stdin_and_cwd(self):
        entry = self.case("final_design")
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            entry["report"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        dump = self.shim_dump()
        prompt = self.expected_prompt()
        self.assertEqual(dump["argv"], codex_review_lib.build_architecture_argv(
            os.path.realpath(self.repo), ARCHITECTURE_SCHEMA_PATH,
            self.real_output(), prompt, codex_review_lib.CODEX_MODEL)[1:])
        self.assertIn(DESIGN_LENS_FILE, dump["argv"][-1])
        self.assertTrue(os.path.isabs(DESIGN_LENS_FILE))
        self.assertNotIn("{DESIGN_LENS_FILE}", dump["argv"][-1])
        with open(self.brief, "rb") as handle:
            self.assertEqual(self.stdin_bytes(), handle.read())
        self.assertEqual(dump["cwd"], os.path.realpath(self.repo))

    def test_output_lines(self):
        entry = self.case("final_design")
        report = entry["report"]
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            report, extra={"CODEX_SHIM_ROLLOUT": self.shim_rollout_path()}))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.stdout_lines(result)
        gate = "codex gate: architecture review (design) on gpt-6-astra (default) at max"
        self.assertIn(gate, lines)
        self.assertIn("codex session: shim-session", lines)
        self.assertTrue(any(l.startswith("codex weekly:") for l in lines),
                        lines)
        self.assertLess(
            [i for i, l in enumerate(lines) if l.startswith("codex weekly:")][0],
            lines.index(gate))
        # The shipped calibration table has no rate for the shim's plan, so
        # the per-run line is the token count (as the diff gates' tests).
        self.assertTrue(any(l.startswith("codex run used 100,000 uncached input "
                                         "tokens on gpt-6-astra")
                            for l in lines), lines)
        self.assertEqual(lines[-1],
                         "architecture report: %s (status %s, %d change(s), "
                         "%d context request(s))" % (
                             self.real_output(), report["status"],
                             len(report["changes"]),
                             len(report["context_requests"])))

    def test_needs_context_with_a_session_exits_zero(self):
        entry = self.case("needs_context_design")
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            entry["report"],
            extra={"CODEX_SHIM_ROLLOUT": self.shim_rollout_path()}))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"codex session: shim-session", result.stdout)

    def test_needs_context_without_a_session_exits_one(self):
        entry = self.case("needs_context_design")
        result = _run_wrapper(self.arch_args(),
                              env=self.arch_env(entry["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"codex session: unknown", result.stdout)
        self.assertIn(b"resum", result.stderr)

    def test_final_without_a_session_is_still_ok(self):
        entry = self.case("final_design")
        result = _run_wrapper(self.arch_args(),
                              env=self.arch_env(entry["report"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"codex session: unknown", result.stdout)

    def test_answer_key_brief_is_refused_before_dispatch(self):
        with open(self.brief, "w", encoding="utf-8") as handle:
            handle.write("# Brief\n\n## Round 2 findings\n\nstuff\n")
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            self.case("final_design")["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_brief_warnings_are_printed_and_dispatch_continues(self):
        with open(self.brief, "w", encoding="utf-8") as handle:
            handle.write("# Brief\n\nThis is BLOCKING work.\n")
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            self.case("final_design")["report"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"brief warning: ", result.stdout)

    def test_missing_brief_file_exits_one(self):
        result = _run_wrapper(
            self.arch_args(brief=os.path.join(self.root, "nope.md")),
            env=self.arch_env(self.case("final_design")["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_c3_without_the_placeholder_is_refused(self):
        exe = _make_architecture_wrapper_copy(
            self.root, "# Architecture review\n\nNo placeholder here.\n")
        result = subprocess.run(
            [sys.executable, exe] + self.arch_args(),
            env=self.arch_env(self.case("final_design")["report"]),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"DESIGN_LENS_FILE", result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_c3_copy_with_the_placeholder_dispatches(self):
        # Positive control for the copy helper: the refusal above must come
        # from the missing placeholder, not from a broken copy.
        exe = _make_architecture_wrapper_copy(
            self.root, "# Architecture review\n\nLenses: {DESIGN_LENS_FILE}\n")
        result = subprocess.run(
            [sys.executable, exe] + self.arch_args(),
            env=self.arch_env(self.case("final_design")["report"]),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.shim_dump()["argv"][-1].startswith(
            "# Architecture review\n\nLenses: "))

    def test_missing_c3_is_refused(self):
        exe = _make_architecture_wrapper_copy(self.root, "")
        result = subprocess.run(
            [sys.executable, exe] + self.arch_args(),
            env=self.arch_env(self.case("final_design")["report"]),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_missing_agents_md_is_refused(self):
        os.remove(os.path.join(self.repo, "AGENTS.md"))
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            self.case("final_design")["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_moment_mismatch_exits_one(self):
        result = _run_wrapper(self.arch_args(moment="final"),
                              env=self.arch_env(
                                  self.case("final_design")["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn(b"architecture report:", result.stdout)

    def test_report_failures_exit_one(self):
        good = self.case("final_design")["report"]
        broken = json.loads(json.dumps(good))
        del broken["lens_verdicts"]
        broken_ran = self.case("ran_true_no_commands")["report"]
        cases = [
            ("semantic", lambda: self.arch_env(broken_ran)),
            ("schema", lambda: self.arch_env(broken)),
            ("not json", lambda: self.arch_env("{nope,,")),
            ("empty", lambda: self.arch_env("  \n")),
            ("nonzero", lambda: self.arch_env(good, exit_code=3)),
            ("no output", lambda: self.arch_env(good, output_mode="none")),
        ]
        for label, make_env in cases:
            # Built lazily: every arch_env call rewrites the shim's report.
            result = _run_wrapper(self.arch_args(), env=make_env())
            self.assertEqual(result.returncode, 1, (label, result.stderr))
            self.assertNotIn(b"architecture report:", result.stdout, label)

    def test_stale_report_is_not_mistaken_for_fresh(self):
        with open(self.output_path, "w", encoding="utf-8") as handle:
            json.dump(self.case("final_design")["report"], handle)
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            self.case("final_design")["report"], output_mode="none"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(os.path.exists(self.output_path))


class TestArchitectureGateModel(ArchitectureWrapperTestCase):
    """The architecture gate takes its model from `codex_review.models` like
    every other gate, so an account that does not serve the pin can still
    run it, and a refused model exits 4 rather than 1."""

    MEASURED = TestModelRejectedExitsFour.MEASURED
    arch_args = TestArchitectureDispatch.arch_args

    def write_config(self, content):
        _ConfigWritingTestCase.write_config(self, content)

    def test_configured_model_reaches_the_argv_and_the_label(self):
        self.write_config({"codex_review": {"models": {"architecture": "my-model"}}})
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            self.case("final_design")["report"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = self.shim_dump()["argv"]
        self.assertEqual(argv[argv.index("-m") + 1], "my-model")
        self.assertIn("codex gate: architecture review (design) on my-model "
                      "(configured) at max", self.stdout_lines(result))

    def test_rejected_model_exits_four_and_leaves_no_report(self):
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            self.case("final_design")["report"], exit_code=1,
            extra={"CODEX_SHIM_STDERR": self.MEASURED}))
        self.assertEqual(result.returncode, 4, result.stderr)
        self.assertTrue(self.shim_was_run())
        self.assertIn(b"codex_review.models.architecture", result.stderr)
        self.assertFalse(os.path.exists(self.output_path))

    def test_bad_config_value_refuses_before_dispatch(self):
        self.write_config({"codex_review": {"models": {"architecture": "a b"}}})
        result = _run_wrapper(self.arch_args(), env=self.arch_env(
            self.case("final_design")["report"]))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(self.shim_was_run())


class TestArchitectureResume(ArchitectureWrapperTestCase):
    ANSWERS = "## c1\n\nThe sourced answer, line two.\n"

    def setUp(self):
        super().setUp()
        self.answers = os.path.join(self.root, "answers.md")
        with open(self.answers, "w", encoding="utf-8") as handle:
            handle.write(self.ANSWERS)

    def resume_args(self, round_="2", moment="final", answers=None):
        return ["--resume", "sess-abc", "--answers", answers or self.answers,
                "--round", round_, "--moment", moment, "--repo", self.repo,
                "-o", self.output_path, "--sessions-root", self.sessions_root]

    def test_round_two_argv_and_empty_stdin(self):
        entry = self.case("needs_context_final")
        result = _run_wrapper(self.resume_args(),
                              env=self.arch_env(entry["report"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        dump = self.shim_dump()
        argv = dump["argv"]
        self.assertEqual(argv[:3], ["exec", "resume", "sess-abc"])
        reference = codex_review_lib.build_resume_argv(
            "sess-abc", ARCHITECTURE_SCHEMA_PATH, self.real_output(),
            "x", codex_review_lib.CODEX_MODEL)[1:]
        self.assertEqual(argv[:-1], reference[:-1])
        self.assertIn('model_reasoning_effort="max"', argv)
        self.assertIn('sandbox_mode="read-only"', argv)
        self.assertEqual(argv[argv.index('model_reasoning_effort="max"') - 1],
                         "-c")
        self.assertEqual(argv[argv.index('sandbox_mode="read-only"') - 1],
                         "-c")
        prompt = argv[-1]
        self.assertTrue(prompt.endswith(self.ANSWERS), prompt)
        self.assertIn("`round` = 2", prompt)
        self.assertNotIn("FINAL round", prompt)
        self.assertEqual(self.stdin_bytes(), b"")
        self.assertEqual(dump["cwd"], os.path.realpath(self.repo))

    def test_round_three_prompt_says_final_round(self):
        entry = self.case("final_design_round3")
        result = _run_wrapper(self.resume_args(round_="3", moment="design"),
                              env=self.arch_env(entry["report"]))
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.shim_dump()["argv"][-1]
        self.assertTrue(prompt.endswith(self.ANSWERS), prompt)
        self.assertIn("`round` = 3", prompt)
        self.assertIn("FINAL round", prompt)
        self.assertIn("`status: final`", prompt)
        self.assertIn("unverified", prompt)

    def test_output_lines(self):
        entry = self.case("needs_context_final")
        report = entry["report"]
        result = _run_wrapper(self.resume_args(),
                              env=self.arch_env(report))
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = self.stdout_lines(result)
        self.assertIn("codex gate: architecture resume round 2 (final) on "
                      "gpt-6-astra (default) at max", lines)
        self.assertIn("codex session: sess-abc", lines)
        self.assertTrue(any("not attributable" in l for l in lines), lines)
        self.assertEqual(lines[-1],
                         "architecture report: %s (status %s, %d change(s), "
                         "%d context request(s))" % (
                             self.real_output(), report["status"],
                             len(report["changes"]),
                             len(report["context_requests"])))

    def test_needs_context_on_round_three_exits_one(self):
        entry = self.case("round3_needs_context")
        result = _run_wrapper(self.resume_args(round_="3", moment="design"),
                              env=self.arch_env(entry["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_moment_mismatch_exits_one(self):
        entry = self.case("needs_context_final")
        result = _run_wrapper(self.resume_args(moment="design"),
                              env=self.arch_env(entry["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_round_mismatch_exits_one(self):
        entry = self.case("final_design_round3")
        result = _run_wrapper(self.resume_args(round_="2", moment="design"),
                              env=self.arch_env(entry["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)

    def test_missing_or_empty_answers_exit_one_without_dispatch(self):
        entry = self.case("needs_context_final")
        result = _run_wrapper(
            self.resume_args(answers=os.path.join(self.root, "nope.md")),
            env=self.arch_env(entry["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())
        with open(self.answers, "w", encoding="utf-8") as handle:
            handle.write("  \n")
        result = _run_wrapper(self.resume_args(),
                              env=self.arch_env(entry["report"]))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())

    def test_dispatch_failures_exit_one(self):
        good = self.case("needs_context_final")["report"]
        for label, make_env in (
                ("nonzero", lambda: self.arch_env(good, exit_code=3)),
                ("no output", lambda: self.arch_env(good, output_mode="none")),
                ("not json", lambda: self.arch_env("{nope,,"))):
            result = _run_wrapper(self.resume_args(), env=make_env())
            self.assertEqual(result.returncode, 1, (label, result.stderr))

    def test_stale_report_is_not_mistaken_for_fresh(self):
        good = self.case("needs_context_final")["report"]
        with open(self.output_path, "w", encoding="utf-8") as handle:
            json.dump(good, handle)
        result = _run_wrapper(self.resume_args(), env=self.arch_env(
            good, output_mode="none"))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(os.path.exists(self.output_path))

    def test_no_codex_is_refused(self):
        env = self.arch_env(self.case("needs_context_final")["report"])
        env["PATH"] = "/usr/bin:/bin"
        result = _run_wrapper(self.resume_args(), env=env)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertFalse(self.shim_was_run())


if __name__ == "__main__":
    unittest.main()
