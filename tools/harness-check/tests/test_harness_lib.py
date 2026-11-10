#!/usr/bin/env python3
"""Tests for the pure logic in harness_lib.py — state, version reading and the notice decision.

State, transcript-version reading and the notice decision. No subprocess, no
config, no filesystem beyond a per-test temp directory for the save/load
round trips. Hermetic: every test builds its own data, never reads
`~/.claude` or any ambient state.
"""

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import harness_lib  # noqa: E402


# ------------------------------------------------------------- transcript_version --

class TestTranscriptVersion(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-check-tv-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, lines):
        path = os.path.join(self.tmp, "transcript.jsonl")
        with open(path, "w") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
        return path

    def test_returns_the_last_version_in_the_file(self):
        path = self._write([
            {"type": "user", "version": "2.1.280"},
            {"type": "assistant", "version": "2.1.280"},
            {"type": "assistant", "version": "2.1.283"},
        ])
        self.assertEqual(harness_lib.transcript_version(path), "2.1.283")

    def test_a_missing_path_returns_none(self):
        self.assertIsNone(
            harness_lib.transcript_version(os.path.join(self.tmp, "nope.jsonl")))

    def test_a_falsy_path_returns_none(self):
        self.assertIsNone(harness_lib.transcript_version(None))
        self.assertIsNone(harness_lib.transcript_version(""))

    def test_an_empty_file_returns_none(self):
        path = os.path.join(self.tmp, "empty.jsonl")
        open(path, "w").close()
        self.assertIsNone(harness_lib.transcript_version(path))

    def test_lines_that_carry_no_version_return_none(self):
        path = self._write([
            {"type": "user", "entrypoint": "cli"},
            {"type": "assistant", "text": "OK"},
        ])
        self.assertIsNone(harness_lib.transcript_version(path))

    def test_only_the_declared_tail_is_scanned(self):
        # A version line placed before the tail window must be invisible once
        # enough filler follows it to push it out of the last
        # TRANSCRIPT_TAIL_BYTES bytes of the file. Proves the truncation is
        # enforced, not merely documented.
        path = os.path.join(self.tmp, "big.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({"type": "assistant", "version": "1.0.0"}) + "\n")
            filler = json.dumps({"type": "assistant", "text": "x" * 200}) + "\n"
            while handle.tell() < harness_lib.TRANSCRIPT_TAIL_BYTES + 50000:
                handle.write(filler)
        self.assertIsNone(harness_lib.transcript_version(path))


# ------------------------------------------------------------------ load_json/save_json --

class TestLoadSaveJson(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-check-io-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_json_returns_the_empty_default_for_a_missing_file(self):
        path = os.path.join(self.tmp, "nope.json")
        self.assertEqual(harness_lib.load_json(path, {"versions": {}}),
                         {"versions": {}})

    def test_load_json_returns_the_empty_default_for_corrupt_json(self):
        path = os.path.join(self.tmp, "corrupt.json")
        with open(path, "w") as handle:
            handle.write("{not valid json")
        self.assertEqual(harness_lib.load_json(path, {}), {})

    def test_load_json_returns_the_empty_default_when_the_top_level_is_not_a_dict(self):
        path = os.path.join(self.tmp, "list.json")
        with open(path, "w") as handle:
            json.dump([1, 2, 3], handle)
        self.assertEqual(harness_lib.load_json(path, {}), {})

    def test_save_then_load_round_trips(self):
        path = os.path.join(self.tmp, "state.json")
        obj = {"versions": {"9.9.9": {"result": "pass"}}}
        self.assertTrue(harness_lib.save_json(path, obj))
        self.assertEqual(harness_lib.load_json(path, {}), obj)

    def test_save_json_leaves_no_temp_file_behind(self):
        path = os.path.join(self.tmp, "state.json")
        harness_lib.save_json(path, {"a": 1})
        leftovers = [n for n in os.listdir(self.tmp) if n != "state.json"]
        self.assertEqual(leftovers, [])

    def test_save_json_creates_missing_parent_directories(self):
        path = os.path.join(self.tmp, "nested", "deeper", "state.json")
        self.assertTrue(harness_lib.save_json(path, {"a": 1}))
        self.assertEqual(harness_lib.load_json(path, {}), {"a": 1})


# ------------------------------------------------------------------------ record_result --

class TestRecordResult(unittest.TestCase):

    def test_sets_the_version_entry(self):
        results = harness_lib.record_result({}, "9.9.9", "pass", [],
                                            "2026-09-29T10:00:00Z")
        self.assertEqual(results, {"versions": {"9.9.9": {
            "result": "pass", "checked_at": "2026-09-29T10:00:00Z",
            "failures": [], "rev": harness_lib.RECORD_REV,
        }}})

    def test_partial_stores_unverified_ids_and_the_default_stores_none(self):
        results = harness_lib.record_result({}, "9.9.9", "partial", [],
                                            "2026-09-29T10:00:00Z",
                                            unverified=["H5"])
        record = results["versions"]["9.9.9"]
        self.assertEqual(record["result"], "partial")
        self.assertEqual(record["unverified"], ["H5"])
        self.assertEqual(record["rev"], harness_lib.RECORD_REV)
        plain = harness_lib.record_result({}, "9.9.9", "pass", [], "x")
        self.assertNotIn("unverified", plain["versions"]["9.9.9"])

    def test_overwrites_an_existing_entry_for_the_same_version(self):
        results = {"versions": {"9.9.9": {"result": "pass",
                                          "checked_at": "old", "failures": []}}}
        results = harness_lib.record_result(results, "9.9.9", "fail", ["H2"],
                                            "2026-09-29T11:00:00Z")
        self.assertEqual(results["versions"]["9.9.9"], {
            "result": "fail", "checked_at": "2026-09-29T11:00:00Z",
            "failures": ["H2"], "rev": harness_lib.RECORD_REV,
        })


# -------------------------------------------------------------------------- mark_noticed --

class TestMarkNoticed(unittest.TestCase):

    def test_creates_the_noticed_map_when_absent(self):
        doc = harness_lib.mark_noticed({}, "sess-1", "9.9.9")
        self.assertEqual(doc, {"noticed": {"sess-1": "9.9.9"}})

    def test_caps_at_200_and_keeps_the_newest(self):
        doc = {"noticed": {}}
        for i in range(250):
            doc = harness_lib.mark_noticed(doc, "sess-%d" % i, "9.9.9")
        self.assertEqual(len(doc["noticed"]), 200)
        self.assertNotIn("sess-0", doc["noticed"])
        self.assertNotIn("sess-49", doc["noticed"])
        self.assertIn("sess-50", doc["noticed"])
        self.assertIn("sess-249", doc["noticed"])

    def test_a_re_insert_moves_the_key_to_the_end_so_it_survives_the_cap(self):
        doc = {"noticed": {}}
        for i in range(199):
            doc = harness_lib.mark_noticed(doc, "sess-%d" % i, "9.9.9")
        # sess-0 is the oldest so far. Re-insert it, then push 5 more new
        # sessions in. If the re-insert had not moved it to the end, it would
        # be evicted well before a genuinely-old key.
        doc = harness_lib.mark_noticed(doc, "sess-0", "9.9.9")
        for i in range(199, 204):
            doc = harness_lib.mark_noticed(doc, "sess-%d" % i, "9.9.9")
        self.assertIn("sess-0", doc["noticed"])


# -------------------------------------------------------------------------- decide_notice --

def _speaking_state():
    """A (results, noticed, session_id, version, enabled, agent_id) tuple
    where every silent condition is false, so decide_notice speaks.

    Used as the baseline for the five silence tests below: each flips
    exactly one field to its silencing value, so a test fails if that
    field's check is ever deleted from decide_notice.
    """
    return ({"versions": {}}, {"noticed": {}}, "sess-1", "9.9.9", True, None)


class TestDecideNotice(unittest.TestCase):

    def test_speaks_when_every_condition_would_allow_it(self):
        results, noticed, session_id, version, enabled, agent_id = _speaking_state()
        text = harness_lib.decide_notice(results, noticed, session_id, version,
                                         enabled, agent_id)
        self.assertIsNotNone(text)

    def test_silent_when_disabled(self):
        results, noticed, session_id, version, enabled, agent_id = _speaking_state()
        text = harness_lib.decide_notice(results, noticed, session_id, version,
                                         False, agent_id)
        self.assertIsNone(text)

    def test_silent_when_agent_id_is_present(self):
        results, noticed, session_id, version, enabled, agent_id = _speaking_state()
        text = harness_lib.decide_notice(results, noticed, session_id, version,
                                         enabled, "agent-42")
        self.assertIsNone(text)

    def test_silent_when_version_is_none(self):
        results, noticed, session_id, version, enabled, agent_id = _speaking_state()
        text = harness_lib.decide_notice(results, noticed, session_id, None,
                                         enabled, agent_id)
        self.assertIsNone(text)

    def test_silent_when_the_version_is_recorded_pass(self):
        results, noticed, session_id, version, enabled, agent_id = _speaking_state()
        results = {"versions": {version: {"result": "pass", "rev": harness_lib.RECORD_REV,
                                          "checked_at": "2026-09-29T00:00:00Z",
                                          "failures": []}}}
        text = harness_lib.decide_notice(results, noticed, session_id, version,
                                         enabled, agent_id)
        self.assertIsNone(text)

    def test_silent_when_the_version_is_recorded_partial(self):
        # The approved design: partial is quiet but honest.
        results = {"versions": {"9.9.9": {
            "result": "partial", "rev": harness_lib.RECORD_REV, "unverified": ["H5"],
            "checked_at": "2026-09-29T00:00:00Z", "failures": []}}}
        text = harness_lib.decide_notice(results, {"noticed": {}}, "sess-1",
                                         "9.9.9", True, None)
        self.assertIsNone(text)

    def test_a_recorded_fail_still_notices(self):
        results = {"versions": {"9.9.9": {
            "result": "fail", "rev": harness_lib.RECORD_REV, "checked_at": "2026-09-29T00:00:00Z",
            "failures": ["H5"]}}}
        text = harness_lib.decide_notice(results, {"noticed": {}}, "sess-1",
                                         "9.9.9", True, None)
        self.assertIn("FAILED harness check H5", text)

    def test_a_legacy_pass_without_rev_gets_the_unverified_notice(self):
        # Pre-rev records may hide an unchecked H5(c) stored as pass.
        results = {"versions": {"9.9.9": {
            "result": "pass", "checked_at": "2026-09-29T00:00:00Z",
            "failures": []}}}
        text = harness_lib.decide_notice(results, {"noticed": {}}, "sess-1",
                                         "9.9.9", True, None)
        self.assertIsNotNone(text)
        self.assertIn("has not been verified", text)

    def test_a_legacy_fail_without_rev_still_gets_the_failed_notice(self):
        results = {"versions": {"9.9.9": {
            "result": "fail", "checked_at": "2026-09-29T00:00:00Z",
            "failures": ["H2"]}}}
        text = harness_lib.decide_notice(results, {"noticed": {}}, "sess-1",
                                         "9.9.9", True, None)
        self.assertIn("FAILED harness check H2", text)

    def test_a_current_rev_pass_is_quiet(self):
        results = {"versions": {"9.9.9": {
            "result": "pass", "rev": harness_lib.RECORD_REV, "checked_at": "2026-09-29T00:00:00Z",
            "failures": []}}}
        self.assertIsNone(harness_lib.decide_notice(
            results, {"noticed": {}}, "sess-1", "9.9.9", True, None))

    def test_a_rev_2_pass_predates_h6_and_gets_the_unverified_notice(self):
        # Rev 3: H6 (the 200-line index cut) joined the probe set, so a
        # rev-2 pass never checked it. Literal 2 here means "legacy".
        results = {"versions": {"9.9.9": {
            "result": "pass", "rev": 2, "checked_at": "2026-09-29T00:00:00Z",
            "failures": []}}}
        text = harness_lib.decide_notice(
            results, {"noticed": {}}, "sess-1", "9.9.9", True, None)
        self.assertIsNotNone(text)
        self.assertIn("has not been verified", text)
        self.assertEqual(harness_lib._last_verified_version(results["versions"]),
                         "none")

    def test_last_verified_skips_partial_and_legacy_pass(self):
        versions = {
            "1.0": {"result": "partial", "rev": harness_lib.RECORD_REV, "checked_at": "2026-09-20T00:00:00Z"},
            "0.9": {"result": "pass", "rev": harness_lib.RECORD_REV, "checked_at": "2026-09-01T00:00:00Z"},
            "0.95": {"result": "pass", "checked_at": "2026-09-10T00:00:00Z"},
        }
        self.assertEqual(harness_lib._last_verified_version(versions), "0.9")

    def test_silent_when_this_session_was_already_noticed_for_this_version(self):
        results, noticed, session_id, version, enabled, agent_id = _speaking_state()
        noticed = {"noticed": {session_id: version}}
        text = harness_lib.decide_notice(results, noticed, session_id, version,
                                         enabled, agent_id)
        self.assertIsNone(text)

    def test_the_no_record_text_matches_exactly(self):
        results, noticed, session_id, version, enabled, agent_id = _speaking_state()
        text = harness_lib.decide_notice(results, noticed, session_id, version,
                                         enabled, agent_id)
        self.assertEqual(
            text,
            "[harness-check] Claude Code 9.9.9 is running and has not been "
            "verified against the memory design's 6 harness assumptions "
            "(last verified: none). Run claude-harness-check (3 Haiku runs, "
            "about a minute).")

    def test_the_no_record_text_names_the_last_pass_by_latest_checked_at(self):
        results = {"versions": {
            "9.9.6": {"result": "pass", "rev": harness_lib.RECORD_REV, "checked_at": "2026-09-01T00:00:00Z",
                     "failures": []},
            "9.9.7": {"result": "pass", "rev": harness_lib.RECORD_REV, "checked_at": "2026-09-10T00:00:00Z",
                     "failures": []},
        }}
        noticed = {"noticed": {}}
        text = harness_lib.decide_notice(results, noticed, "sess-1", "9.9.9",
                                         True, None)
        self.assertIn("last verified: 9.9.7", text)

    def test_the_failed_text_matches_exactly(self):
        results = {"versions": {"9.9.9": {
            "result": "fail", "checked_at": "2026-09-29T00:00:00Z",
            "failures": ["H1", "H4"],
        }}}
        noticed = {"noticed": {}}
        text = harness_lib.decide_notice(results, noticed, "sess-1", "9.9.9",
                                         True, None)
        self.assertEqual(
            text,
            "[harness-check] Claude Code 9.9.9 FAILED harness check H1, H4 "
            "on 2026-09-29. memory-lint and context-budget numbers may be "
            "wrong until fixed. Run claude-harness-check --status for "
            "detail, and re-run claude-harness-check after fixing.")

    def test_does_not_mutate_its_inputs(self):
        results, noticed, session_id, version, enabled, agent_id = _speaking_state()
        before_results = json.loads(json.dumps(results))
        before_noticed = json.loads(json.dumps(noticed))
        harness_lib.decide_notice(results, noticed, session_id, version,
                                  enabled, agent_id)
        self.assertEqual(results, before_results)
        self.assertEqual(noticed, before_noticed)

    def test_alternating_versions_in_one_session_speak_only_for_the_unrecorded_one(self):
        # Acceptance 2: A, B, A where A is pass and B unrecorded. Only B
        # speaks, and only once — the second A must stay silent because it
        # is pass, not because it was already noticed.
        results = {"versions": {"A": {"result": "pass", "rev": harness_lib.RECORD_REV,
                                      "checked_at": "2026-09-01T00:00:00Z",
                                      "failures": []}}}
        noticed = {"noticed": {}}
        session_id = "sess-1"
        spoken = []
        for version in ("A", "B", "A"):
            text = harness_lib.decide_notice(results, noticed, session_id,
                                             version, True, None)
            if text is not None:
                spoken.append(version)
                noticed = harness_lib.mark_noticed(noticed, session_id, version)
        self.assertEqual(spoken, ["B"])

    def test_a_fail_version_speaks_again_in_a_new_session_after_being_noticed(self):
        # Acceptance 3: a fail is re-noticed every session, not once per
        # version, because it "stays visible until fixed".
        results = {"versions": {"X": {"result": "fail",
                                      "checked_at": "2026-09-29T00:00:00Z",
                                      "failures": ["H3"]}}}
        noticed = harness_lib.mark_noticed({"noticed": {}}, "session-1", "X")
        text = harness_lib.decide_notice(results, noticed, "session-2", "X",
                                         True, None)
        self.assertIsNotNone(text)
        self.assertIn("FAILED harness check", text)


# ------------------------------------------------------ two writers, one file each --

class TestTwoWritersOneFileEach(unittest.TestCase):
    """Acceptance 6: the interleaving Plan Review demonstrated.

    results holds A=pass. The hook loads both files. The runner then
    records A=fail and saves. The hook saves noticed. Reloading the results
    file must show A=fail — because the hook never writes the results file,
    a stale snapshot it loaded earlier cannot clobber the runner's write.
    With one shared file written by both parties, this is exactly the case
    that loses the runner's update.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-check-race-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_runners_fail_write_survives_a_hook_that_loaded_a_stale_pass(self):
        results_path = os.path.join(self.tmp, "results.json")
        noticed_path = os.path.join(self.tmp, "noticed.json")

        seeded = harness_lib.record_result({}, "A", "pass", [],
                                           "2026-09-01T00:00:00Z")
        harness_lib.save_json(results_path, seeded)
        harness_lib.save_json(noticed_path, {"noticed": {}})

        # The hook loads both files up front (its own read of state).
        hook_results = harness_lib.load_json(results_path, {})
        hook_noticed = harness_lib.load_json(noticed_path, {"noticed": {}})

        # Meanwhile the runner records a NEW result, independently, and
        # saves it — the only writer of the results file.
        runner_results = harness_lib.load_json(results_path, {})
        runner_results = harness_lib.record_result(runner_results, "A", "fail",
                                                    ["H1"], "2026-09-02T00:00:00Z")
        harness_lib.save_json(results_path, runner_results)

        # Only now does the hook save noticed — the only writer of that
        # file — using the (now stale) copy of `hook_results` it loaded
        # earlier. It must never write that stale copy back to disk.
        hook_noticed = harness_lib.mark_noticed(hook_noticed, "sess-1", "A")
        harness_lib.save_json(noticed_path, hook_noticed)

        reloaded = harness_lib.load_json(results_path, {})
        self.assertEqual(reloaded["versions"]["A"]["result"], "fail")
        # Sanity: the hook's stale read really did see "pass", so the
        # scenario is a genuine race and not a no-op.
        self.assertEqual(hook_results["versions"]["A"]["result"], "pass")


# -------------------------------------------------------------------- build_fixture --

_TOKEN_SHAPE_RE = re.compile(r"[A-Z]+-[A-Z]+-\d{3}")

# Which relative file each token belongs to -- used to prove a token never
# leaks into a file it isn't supposed to be in.
_TOKEN_HOME = {
    "CTRL": ("CLAUDE.md",),
    "HIST": ("CLAUDE.history.md",),
    "RULE_ON": (".claude", "rules", "always.md"),
    "RULE_SCOPED": (".claude", "rules", "scoped.md"),
    "MEM": ("mem", "MEMORY.md"),
    "IDX_EARLY": ("mem", "MEMORY.md"),
    "IDX_LATE": ("mem", "MEMORY.md"),
    "TOPIC": ("mem", "topics", "t.md"),
    "MEMHIST": ("mem", "history", "MEMORY.history.md"),
    "LINE_HEAD": ("mem-lines", "MEMORY.md"),
    "LINE_EARLY": ("mem-lines", "MEMORY.md"),
    "LINE_LATE": ("mem-lines", "MEMORY.md"),
}


def _line_spans(text):
    """[(line, start_char, end_char), ...] for `text`, computed independently
    of anything `build_fixture` does internally -- plain line splitting."""
    spans = []
    offset = 0
    for line in text.splitlines(keepends=True):
        start = offset
        offset += len(line)
        spans.append((line, start, offset))
    return spans


class TestBuildFixture(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-check-bf-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _read(self, fx, *parts):
        with open(os.path.join(fx.root, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_memory_md_clears_all_four_assertions_independently(self):
        # Acceptance 1: re-measure the three offsets and the line cap
        # ourselves, with no call into the builder's own assertion code.
        fx = harness_lib.build_fixture(self.tmp, random.Random(1))
        text = self._read(fx, "mem", "MEMORY.md")
        spans = _line_spans(text)

        early_end = None
        late_start = None
        for line, start, end in spans:
            if fx.tokens["IDX_EARLY"] in line:
                early_end = end
            if fx.tokens["IDX_LATE"] in line:
                late_start = start
        self.assertIsNotNone(early_end, "IDX_EARLY row not found")
        self.assertIsNotNone(late_start, "IDX_LATE row not found")

        self.assertLessEqual(early_end, 24900)
        self.assertGreaterEqual(len(text[:early_end].encode("utf-8")), 25400)
        self.assertGreaterEqual(late_start, 25300)
        self.assertLessEqual(text.count("\n"), 180)

    def test_ascii_only_filler_makes_build_fixture_raise_fixture_error(self):
        # Acceptance 2: with no multibyte characters anywhere in the filler,
        # the same character-based recipe can no longer clear the UTF-8
        # byte floor -- proving that floor is enforced, not incidental.
        original = harness_lib._filler_row

        def ascii_filler(n):
            return ("- Filler entry {0} padding padding padding padding "
                    "padding padding padding text long enough to roughly "
                    "match the real filler row length with no multibyte "
                    "characters anywhere in it at all whatsoever.\n").format(n)

        harness_lib._filler_row = ascii_filler
        try:
            with self.assertRaises(harness_lib.FixtureError):
                harness_lib.build_fixture(self.tmp, random.Random(2))
        finally:
            harness_lib._filler_row = original

    def test_tokens_distinct_filler_clean_and_each_token_stays_in_its_file(self):
        # Acceptance 3.
        fx = harness_lib.build_fixture(self.tmp, random.Random(3))
        self.assertEqual(set(fx.tokens.keys()), set(_TOKEN_HOME.keys()))
        self.assertEqual(len(set(fx.tokens.values())), len(fx.tokens))

        contents = {}
        for dirpath, _dirnames, filenames in os.walk(fx.root):
            for name in filenames:
                path = os.path.join(dirpath, name)
                try:
                    with open(path, encoding="utf-8") as handle:
                        contents[path] = handle.read()
                except (UnicodeDecodeError, OSError):
                    contents[path] = ""

        for key, value in fx.tokens.items():
            owner = os.path.join(fx.root, *_TOKEN_HOME[key])
            holders = sorted(p for p, text in contents.items() if value in text)
            self.assertEqual(holders, [owner], "token {0} leaked".format(key))

        memory_text = contents[os.path.join(fx.root, "mem", "MEMORY.md")]
        for line in memory_text.splitlines():
            if "Filler entry" in line:
                self.assertIsNone(_TOKEN_SHAPE_RE.search(line), line)

    # -- mem-lines/MEMORY.md: the index line cut on its own fixture (H6) ----

    def test_lines_memory_md_satisfies_every_contract_bullet_read_back_off_disk(self):
        fx = harness_lib.build_fixture(self.tmp, random.Random(5))
        text = self._read(fx, "mem-lines", "MEMORY.md")
        lines = text.split("\n")
        self.assertIn(fx.tokens["LINE_HEAD"], lines[0])
        self.assertIn(fx.tokens["LINE_EARLY"], lines[194])
        self.assertIn(fx.tokens["LINE_LATE"], lines[205])
        self.assertTrue(text.endswith("\n"))
        self.assertGreaterEqual(text.strip().count("\n") + 1, 215)
        # Independence: a 25,000-char or 25,000-byte cut ALONE keeps LINE_LATE.
        self.assertLessEqual(len(text.encode("utf-8")), 20000)
        text.encode("ascii")  # raises if not ASCII
        for number, line in enumerate(lines, start=1):
            if number in (1, 195, 206):
                continue
            self.assertIsNone(_TOKEN_SHAPE_RE.search(line), line)
        for key in ("LINE_HEAD", "LINE_EARLY", "LINE_LATE"):
            self.assertEqual(text.count(fx.tokens[key]), 1, key)

    def test_lines_memory_md_raises_fixture_error_when_the_byte_cap_is_breached(self):
        original = harness_lib._line_filler_row
        harness_lib._line_filler_row = lambda n: "- Line filler {0} {1}.".format(
            n, "x" * 100)  # ~120 chars a row: 214 rows overrun 20,000 bytes
        try:
            with self.assertRaises(harness_lib.FixtureError):
                harness_lib.build_fixture(self.tmp, random.Random(6))
        finally:
            harness_lib._line_filler_row = original

    def test_contain_py_allows_read_of_src_x_py_and_blocks_everything_else(self):
        # Acceptance 4.
        fx = harness_lib.build_fixture(self.tmp, random.Random(4))
        contain = os.path.join(fx.root, "hooks", "contain.py")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": self.tmp,
              "USERPROFILE": self.tmp}

        def run(payload):
            return subprocess.run(
                [sys.executable, contain],
                input=json.dumps(payload).encode("utf-8"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env, timeout=10)

        allowed = run({"tool_name": "Read",
                      "tool_input": {"file_path": os.path.join(fx.root, "src", "x.py")}})
        self.assertEqual(allowed.returncode, 0)
        blocked_path = os.path.join(fx.root, "blocked.jsonl")
        self.assertFalse(os.path.exists(blocked_path))

        blocked_read = run({"tool_name": "Read",
                           "tool_input": {"file_path": os.path.join(fx.root, "mem", "MEMORY.md")}})
        self.assertEqual(blocked_read.returncode, 2)

        blocked_bash = run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(blocked_bash.returncode, 2)

        with open(blocked_path) as handle:
            lines = [json.loads(l) for l in handle if l.strip()]
        self.assertEqual([l["tool_name"] for l in lines], ["Read", "Bash"])

    def test_projects_dir_for_a_var_path_uses_the_private_var_realpath(self):
        # Acceptance 5.
        var_path = "/var"
        real = os.path.realpath(var_path)
        if real == var_path:
            self.skipTest("/var is not a symlink on this machine")

        old = os.environ.get("HARNESS_CHECK_PROJECTS_ROOT")
        os.environ["HARNESS_CHECK_PROJECTS_ROOT"] = self.tmp
        try:
            result = harness_lib.projects_dir_for(var_path)
        finally:
            if old is None:
                os.environ.pop("HARNESS_CHECK_PROJECTS_ROOT", None)
            else:
                os.environ["HARNESS_CHECK_PROJECTS_ROOT"] = old

        expected_slug = re.sub(r"[^A-Za-z0-9]", "-", real)
        self.assertEqual(result, os.path.join(self.tmp, expected_slug))
        self.assertIn("private", result)


# ------------------------------------------ probe argv / fixture hook wiring --
# Code Review Fix 2: pure pins on two things a stub could otherwise ignore
# entirely without any test noticing -- the exact argv `_probe_argv` builds,
# and the hook registrations `build_fixture` writes into the fixture's own
# `.claude/settings.json`. Mutating either (see the module docstring's fix
# notes) must turn one of these red.

class TestProbeArgvAndFixtureHookWiring(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-check-argv-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_probe_argv_carries_every_required_flag(self):
        fx = harness_lib.build_fixture(self.tmp, random.Random(11))
        argv = harness_lib._probe_argv("/path/to/claude", fx)

        self.assertEqual(argv[0], "/path/to/claude")
        self.assertIn("-p", argv)
        self.assertNotIn("--no-session-persistence", argv)

        def value_after(flag):
            idx = argv.index(flag)
            return argv[idx + 1]

        self.assertEqual(value_after("--model"), "haiku")
        self.assertEqual(value_after("--setting-sources"), "project")
        self.assertEqual(value_after("--tools"), "Read")
        self.assertEqual(value_after("--output-format"), "stream-json")
        self.assertIn("--verbose", argv)
        self.assertIn("--strict-mcp-config", argv)
        self.assertIn("--max-budget-usd", argv)

        settings = json.loads(value_after("--settings"))
        self.assertEqual(settings.get("autoMemoryDirectory"), fx.root + "/mem")

        lines_argv = harness_lib._probe_argv("/path/to/claude", fx, mem_dir="mem-lines")
        lines_settings = json.loads(lines_argv[lines_argv.index("--settings") + 1])
        self.assertEqual(lines_settings.get("autoMemoryDirectory"),
                         fx.root + "/mem-lines")

    def test_fixture_settings_json_registers_both_real_hooks(self):
        fx = harness_lib.build_fixture(self.tmp, random.Random(12))
        settings_path = os.path.join(fx.root, ".claude", "settings.json")
        with open(settings_path, encoding="utf-8") as handle:
            settings = json.load(handle)

        instr_groups = settings["hooks"]["InstructionsLoaded"]
        instr_commands = [h["command"] for g in instr_groups for h in g["hooks"]]
        self.assertTrue(
            any("hooks/log_loaded.py" in c for c in instr_commands),
            instr_commands)

        pretool_groups = settings["hooks"]["PreToolUse"]
        star_commands = [h["command"] for g in pretool_groups
                         if g.get("matcher") == "*" for h in g["hooks"]]
        self.assertTrue(
            any("hooks/contain.py" in c for c in star_commands),
            star_commands)


# ---------------------------------------------------------- evaluate/aggregate --
# Canned records only -- no subprocess, no real `claude` run. Where a
# test needs the real 200-line/25,000-char MEMORY.md truncation boundary
# (H1), it builds a real fixture with `build_fixture` and reads the file it
# wrote; everywhere else it constructs a `Fixture` directly with hand-picked
# tokens, per the task brief.

def _transcript_of(instructions, body):
    """A transcript built from its own instructions, the way a real one (and
    the runner stub) carries them: an `instructions` attachment line plus a
    short body line. This is what lets a transcript check see the content a
    run loaded -- a hand-written token-free transcript hides a missing cut.
    """
    attachment = {"type": "attachment",
                  "attachment": {"type": "instructions", "files": instructions}}
    return json.dumps(attachment) + "\n" + body


def _cut_like_the_harness(text):
    """MEMORY.md as the harness cuts it: first 200 lines, THEN 25,000
    characters."""
    return "\n".join(text.split("\n")[:200])[:25000]


def _make_healthy_records(fx):
    """`(r1, r2, calling, r3)` that make every probe PASS against `fx`, plus
    `fx.loaded`/`fx.blocked` set the same way the runner would. `fx` must
    come from `build_fixture` -- `mem/MEMORY.md`'s real content is read off
    disk and cut the way the harness cuts it: first 200 lines, THEN 25,000
    characters. This is what makes the H1 assertions meaningful instead of
    hand-waved: IDX_EARLY survives that cut, IDX_LATE never does.
    """
    memory_path = os.path.join(fx.root, "mem", "MEMORY.md")
    with open(memory_path, encoding="utf-8") as handle:
        memory_text = handle.read()
    cut_text = _cut_like_the_harness(memory_text)

    lines_path = os.path.join(fx.root, "mem-lines", "MEMORY.md")
    with open(lines_path, encoding="utf-8") as handle:
        lines_text = handle.read()
    lines_cut_text = _cut_like_the_harness(lines_text)

    claude_md_path = os.path.join(fx.root, "CLAUDE.md")
    with open(claude_md_path, encoding="utf-8") as handle:
        claude_md_text = handle.read()

    x_py = os.path.join(fx.root, "src", "x.py")
    read_x_py = {"name": "Read", "input": {"file_path": x_py}}

    r1_instructions = [
        {"path": claude_md_path, "type": "Project", "content": claude_md_text},
        {"path": memory_path, "type": "AutoMem", "content": cut_text},
    ]
    r2_instructions = [
        {"path": claude_md_path, "type": "Project", "content": claude_md_text},
    ]
    r3_instructions = [
        {"path": claude_md_path, "type": "Project", "content": claude_md_text},
        {"path": lines_path, "type": "AutoMem", "content": lines_cut_text},
    ]
    r1 = {
        "exit": 0,
        "init": {"model": "claude-haiku-4-5-20251001", "claude_code_version": "9.9.9"},
        "result": {"type": "result"},
        "tool_uses": [read_x_py],
        "transcript": _transcript_of(
            r1_instructions, "R1 transcript body carrying no forbidden token at all."),
        "instructions": r1_instructions,
    }
    r2 = {
        "exit": 0,
        "init": {"model": "claude-haiku-4-5-20251001", "claude_code_version": "9.9.9"},
        "result": {"type": "result"},
        "tool_uses": [read_x_py],
        "transcript": _transcript_of(
            r2_instructions, "R2 transcript body, auto-memory disabled, no MEM here."),
        "instructions": r2_instructions,
    }
    r3 = {
        "exit": 0,
        "init": {"model": "claude-haiku-4-5-20251001", "claude_code_version": "9.9.9"},
        "result": {"type": "result"},
        "tool_uses": [read_x_py],
        "transcript": _transcript_of(
            r3_instructions, "R3 transcript body carrying no forbidden token at all."),
        "instructions": r3_instructions,
    }

    fx.loaded = [
        {"file_path": os.path.join(fx.root, ".claude", "rules", "always.md"),
         "load_reason": "session_start"},
        {"file_path": os.path.join(fx.root, ".claude", "rules", "scoped.md"),
         "load_reason": "path_glob_match"},
    ]
    fx.blocked = []

    calling_dir = os.path.join(fx.root, "calling-mem")
    os.makedirs(calling_dir, exist_ok=True)
    calling_mem_path = os.path.join(calling_dir, "MEMORY.md")
    calling_mem_text = "calling session memory placeholder\n"
    with open(calling_mem_path, "w", encoding="utf-8") as handle:
        handle.write(calling_mem_text)
    calling = {
        "skip": None,
        "version": "9.9.9",
        "instructions": [
            {"path": calling_mem_path, "type": "AutoMem", "content": calling_mem_text},
        ],
        "expected_dir": calling_dir,
    }

    return r1, r2, calling, r3


class TestEvaluate(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-check-eval-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fixture(self, seed):
        return harness_lib.build_fixture(self.tmp, random.Random(seed))

    def _by_id(self, results):
        return {r.id: r for r in results}

    # -- acceptance 1: a healthy canned set passes every probe ------------

    def test_a_healthy_canned_set_passes_every_probe(self):
        fx = self._fixture(101)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        self.assertEqual(version, "9.9.9")
        self.assertEqual([r.id for r in results], ["H1", "H2", "H3", "H4", "H5", "H6"])
        for r in results:
            self.assertEqual(r.status, "PASS", "{0}: {1}".format(r.id, r.reason))
        self.assertEqual(harness_lib.aggregate(version, results), ("pass", [], 0))

    # -- acceptance 2: one mutation per probe turns exactly that probe FAIL

    def test_h1_fails_when_idx_late_leaks_into_the_r1_transcript(self):
        fx = self._fixture(102)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["transcript"] += fx.tokens["IDX_LATE"]
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H1"].status, "FAIL")
        self.assertEqual(
            by_id["H1"].reason,
            "index cut moved above ~25,300 chars; "
            "lint_lib.INDEX_TRUNCATION_CHARS is stale")
        for pid in ("H2", "H3", "H4", "H5"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), ("fail", ["H1"], 1))

    def test_h1_fails_when_the_automem_content_is_cut_at_25000_bytes(self):
        # The byte-cut acceptance test: a real MEMORY.md, cut at 25,000
        # UTF-8 BYTES (decode errors="ignore") rather than 25,000
        # characters, must drop IDX_EARLY -- proving the evaluator would
        # catch a byte-based harness cut, not just a char-based one.
        fx = self._fixture(103)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        memory_path = os.path.join(fx.root, "mem", "MEMORY.md")
        with open(memory_path, "rb") as handle:
            raw = handle.read()
        byte_cut_text = raw[:25000].decode("utf-8", errors="ignore")
        self.assertNotIn(fx.tokens["IDX_EARLY"], byte_cut_text,
                         "fixture sanity: the byte cut should drop IDX_EARLY")
        for entry in r1["instructions"]:
            if entry["type"] == "AutoMem":
                entry["content"] = byte_cut_text
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H1"].status, "FAIL")
        self.assertEqual(
            by_id["H1"].reason,
            "index cut moved below ~24,900 chars, or is now in bytes; "
            "lint_lib.INDEX_TRUNCATION_CHARS is stale")
        for pid in ("H2", "H3", "H4", "H5"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))

    def test_h2_fails_when_topic_leaks_into_the_r1_transcript(self):
        fx = self._fixture(104)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["transcript"] += fx.tokens["TOPIC"]
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H2"].status, "FAIL")
        for pid in ("H1", "H3", "H4", "H5", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), ("fail", ["H2"], 1))

    def test_h3_fails_when_scoped_md_is_logged_at_session_start(self):
        fx = self._fixture(105)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        fx.loaded.append({
            "file_path": os.path.join(fx.root, ".claude", "rules", "scoped.md"),
            "load_reason": "session_start",
        })
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H3"].status, "FAIL")
        self.assertIn("session_start", by_id["H3"].reason)
        for pid in ("H1", "H2", "H4", "H5", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), ("fail", ["H3"], 1))

    def test_h4_fails_when_hist_leaks_into_the_r1_transcript(self):
        fx = self._fixture(106)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["transcript"] += fx.tokens["HIST"]
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H4"].status, "FAIL")
        for pid in ("H1", "H2", "H3", "H5", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), ("fail", ["H4"], 1))

    def test_h5a_fails_when_the_r1_automem_path_points_elsewhere(self):
        fx = self._fixture(107)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        for entry in r1["instructions"]:
            if entry["type"] == "AutoMem":
                entry["path"] = os.path.join(fx.root, "mem", "elsewhere.md")
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "FAIL")
        self.assertIn("(a)", by_id["H5"].reason)
        for pid in ("H1", "H2", "H3", "H4", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), ("fail", ["H5"], 1))

    def test_h5b_fails_when_r2_carries_an_automem_entry(self):
        fx = self._fixture(108)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r2["instructions"].append(
            {"path": os.path.join(fx.root, "mem", "MEMORY.md"), "type": "AutoMem",
             "content": "should not exist when auto-memory is disabled"})
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "FAIL")
        self.assertIn("(b)", by_id["H5"].reason)
        for pid in ("H1", "H2", "H3", "H4", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), ("fail", ["H5"], 1))

    def test_h5c_fails_when_the_calling_entry_resolves_to_another_directory(self):
        fx = self._fixture(109)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        other_dir = os.path.join(fx.root, "not-the-expected-dir")
        os.makedirs(other_dir, exist_ok=True)
        calling["expected_dir"] = other_dir
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "FAIL")
        self.assertIn("(c)", by_id["H5"].reason)
        for pid in ("H1", "H2", "H3", "H4", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), ("fail", ["H5"], 1))

    # -- acceptance 3: each void rule voids exactly the probes listed ------

    def test_preexisting_projects_dir_voids_every_probe(self):
        fx = self._fixture(110)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        fx.preexisting_projects_dir = True
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        self.assertEqual(version, "9.9.9")
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("cannot own", r.reason)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r1_unusable_voids_every_probe(self):
        fx = self._fixture(111)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["exit"] = 1
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("R1 unusable", r.reason)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r2_unusable_with_valid_init_result_but_empty_instructions_voids_only_h5(self):
        # Acceptance 3's named case: r2 exits 0 with a valid init/result,
        # but its instructions list is empty -- no positive control (no
        # Project entry carrying CTRL) -- so H5 is VOID, never PASS.
        fx = self._fixture(112)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r2["instructions"] = []
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "VOID")
        self.assertIn("R2 unusable", by_id["H5"].reason)
        for pid in ("H1", "H2", "H3", "H4", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_blocked_tool_calls_void_only_h3(self):
        fx = self._fixture(113)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        fx.blocked = [{"tool_name": "Bash", "tool_input": {"command": "ls"}}]
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H3"].status, "VOID")
        for pid in ("H1", "H2", "H4", "H5", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_no_read_of_src_x_py_voids_only_h3(self):
        fx = self._fixture(114)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["tool_uses"] = []
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H3"].status, "VOID")
        for pid in ("H1", "H2", "H4", "H5", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    # -- review follow-up: isolating tests for _unusable_reason's other
    # branches (mutation-tested; deleting any of these left the rest of the
    # suite green before this follow-up landed) ----------------------------------

    def test_r1_model_not_haiku_voids_every_probe(self):
        fx = self._fixture(130)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["init"]["model"] = "claude-opus-5-5"
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("R1 unusable", r.reason)
        self.assertIn("Haiku", results[0].reason)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r2_model_not_haiku_voids_only_h5(self):
        fx = self._fixture(131)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r2["init"]["model"] = "claude-opus-5-5"
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "VOID")
        self.assertIn("R2 unusable", by_id["H5"].reason)
        self.assertIn("Haiku", by_id["H5"].reason)
        for pid in ("H1", "H2", "H3", "H4", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r1_transcript_none_voids_every_probe(self):
        fx = self._fixture(132)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["transcript"] = None
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("R1 unusable", r.reason)
        self.assertIn("transcript", results[0].reason)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r2_transcript_none_voids_only_h5(self):
        fx = self._fixture(133)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r2["transcript"] = None
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "VOID")
        self.assertIn("transcript", by_id["H5"].reason)
        for pid in ("H1", "H2", "H3", "H4", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r1_init_none_voids_every_probe(self):
        fx = self._fixture(134)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["init"] = None
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("R1 unusable", r.reason)
        self.assertIn("init missing", results[0].reason)
        # No init means no version either -- aggregate exits 2 on that basis
        # too, but the void rule fires regardless of what version resolves to.
        self.assertIsNone(version)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r1_result_none_voids_every_probe(self):
        fx = self._fixture(135)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["result"] = None
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("R1 unusable", r.reason)
        self.assertIn("result missing", results[0].reason)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r2_init_none_voids_only_h5(self):
        # "r2 once is enough" -- either init or result missing exercises the
        # same shared `_unusable_reason` path for R2; init is picked here.
        fx = self._fixture(136)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r2["init"] = None
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "VOID")
        self.assertIn("init missing", by_id["H5"].reason)
        for pid in ("H1", "H2", "H3", "H4", "H6"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r1_no_project_ctrl_entry_voids_every_probe(self):
        # The r1-side twin of test_r2_unusable_with_valid_init_result_but_
        # empty_instructions_voids_only_h5 -- same missing positive control,
        # but on R1, which must void all six rather than just H5.
        fx = self._fixture(137)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["instructions"] = []
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("R1 unusable", r.reason)
        self.assertIn("no Project instructions entry containing CTRL", results[0].reason)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    # -- review follow-up: evaluate must not raise on a non-dict run --------

    def test_evaluate_does_not_raise_when_r1_is_not_a_dict(self):
        fx = self._fixture(138)
        _unused_r1, r2, calling, r3 = _make_healthy_records(fx)
        version, results = harness_lib.evaluate(fx, None, r2, calling, r3)
        self.assertIsNone(version)
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("R1 unusable", r.reason)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    # -- acceptance 4: FAIL beats VOID --------------------------------------

    def test_fail_beats_void_h1_fail_plus_h3_void_aggregates_to_fail_h1(self):
        fx = self._fixture(115)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        r1["transcript"] += fx.tokens["IDX_LATE"]     # H1 FAIL
        fx.blocked = [{"tool_name": "Bash", "tool_input": {}}]  # H3 VOID
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H1"].status, "FAIL")
        self.assertEqual(by_id["H3"].status, "VOID")
        for pid in ("H2", "H4", "H5"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))
        self.assertEqual(harness_lib.aggregate(version, results), ("fail", ["H1"], 1))

    # -- acceptance 6: calling SKIP still lets H5 PASS ----------------------

    def test_calling_unchecked_skip_makes_h5_partial_with_the_skip_reason(self):
        fx = self._fixture(116)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        calling["skip"] = "not run from a Claude session"
        calling["skip_kind"] = "unchecked"
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "PARTIAL")
        self.assertIn("(c) user-level: NOT VERIFIED (not run from a Claude session)",
                      by_id["H5"].reason)
        self.assertEqual(harness_lib.aggregate(version, results),
                         ("partial", ["H5"], 0))

    def test_calling_version_mismatch_is_partial_with_a_composed_reason(self):
        fx = self._fixture(117)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        calling["skip"] = None
        calling["version"] = "9.9.8"
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "PARTIAL")
        self.assertIn("(c) user-level: NOT VERIFIED (calling session runs 9.9.8, "
                      "probed binary 9.9.9)", by_id["H5"].reason)
        self.assertEqual(harness_lib.aggregate(version, results),
                         ("partial", ["H5"], 0))

    def test_calling_not_applicable_skip_still_passes_h5(self):
        fx = self._fixture(118)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        calling["skip"] = "no user-level autoMemoryDirectory"
        calling["skip_kind"] = "not_applicable"
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H5"].status, "PASS")
        self.assertIn("SKIP (no user-level autoMemoryDirectory)", by_id["H5"].reason)
        self.assertEqual(harness_lib.aggregate(version, results), ("pass", [], 0))

    def test_calling_skip_with_no_kind_is_strict_partial(self):
        fx = self._fixture(119)
        r1, r2, calling, r3 = _make_healthy_records(fx)
        calling["skip"] = "no user-level autoMemoryDirectory"
        calling.pop("skip_kind", None)
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        self.assertEqual(self._by_id(results)["H5"].status, "PARTIAL")
        self.assertEqual(harness_lib.aggregate(version, results),
                         ("partial", ["H5"], 0))


class TestEvaluateH6(unittest.TestCase):
    """H6: the 200-line index cut, judged on R3 and the mem-lines fixture."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-check-h6-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _healthy(self, seed):
        fx = harness_lib.build_fixture(self.tmp, random.Random(seed))
        return (fx,) + _make_healthy_records(fx)

    def _by_id(self, results):
        return {r.id: r for r in results}

    def _set_r3_content(self, r3, content):
        for entry in r3["instructions"]:
            if entry["type"] == "AutoMem":
                entry["content"] = content
        r3["transcript"] = _transcript_of(r3["instructions"], "R3 body.")

    def _lines_text(self, fx):
        with open(os.path.join(fx.root, "mem-lines", "MEMORY.md"),
                  encoding="utf-8") as handle:
            return handle.read()

    def _assert_rest_pass(self, by_id):
        for pid in ("H1", "H2", "H3", "H4", "H5"):
            self.assertEqual(by_id[pid].status, "PASS", "{0}: {1}".format(pid, by_id[pid].reason))

    def test_healthy_set_passes_h6_and_aggregates_to_pass(self):
        fx, r1, r2, calling, r3 = self._healthy(201)
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H6"].status, "PASS", by_id["H6"].reason)
        self.assertEqual(harness_lib.aggregate(version, results), ("pass", [], 0))

    def test_a_25000_char_cut_alone_keeps_line_late_and_fails_h6(self):
        # Independence: the line cut is judged on its own fixture, so a
        # harness that dropped the 200-line step is caught.
        fx, r1, r2, calling, r3 = self._healthy(202)
        self._set_r3_content(r3, self._lines_text(fx)[:25000])
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H6"].status, "FAIL")
        self.assertIn("INDEX_TRUNCATION_LINES", by_id["H6"].reason)
        self.assertIn("above", by_id["H6"].reason)
        self._assert_rest_pass(by_id)
        self.assertEqual(harness_lib.aggregate(version, results), ("fail", ["H6"], 1))

    def test_line_late_in_the_r3_transcript_fails_h6_above(self):
        fx, r1, r2, calling, r3 = self._healthy(203)
        r3["transcript"] += fx.tokens["LINE_LATE"]
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H6"].status, "FAIL")
        self.assertIn("above", by_id["H6"].reason)
        self._assert_rest_pass(by_id)

    def test_a_190_line_cut_drops_line_early_and_fails_h6_below(self):
        fx, r1, r2, calling, r3 = self._healthy(204)
        self._set_r3_content(
            r3, "\n".join(self._lines_text(fx).split("\n")[:190]))
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H6"].status, "FAIL")
        self.assertIn("below", by_id["H6"].reason)
        self.assertIn("INDEX_TRUNCATION_LINES", by_id["H6"].reason)
        self._assert_rest_pass(by_id)

    def test_r3_without_a_mem_lines_automem_entry_voids_h6(self):
        fx, r1, r2, calling, r3 = self._healthy(205)
        r3["instructions"] = [e for e in r3["instructions"]
                              if e["type"] != "AutoMem"]
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H6"].status, "VOID")
        self.assertIn("mem-lines/MEMORY.md", by_id["H6"].reason)
        self._assert_rest_pass(by_id)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r3_automem_at_another_path_voids_h6(self):
        fx, r1, r2, calling, r3 = self._healthy(206)
        for entry in r3["instructions"]:
            if entry["type"] == "AutoMem":
                entry["path"] = os.path.join(fx.root, "mem", "MEMORY.md")
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        self.assertEqual(self._by_id(results)["H6"].status, "VOID")

    def test_r3_unusable_voids_only_h6(self):
        fx, r1, r2, calling, r3 = self._healthy(207)
        r3["exit"] = 1
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        by_id = self._by_id(results)
        self.assertEqual(by_id["H6"].status, "VOID")
        self.assertTrue(by_id["H6"].reason.startswith("R3 unusable:"), by_id["H6"].reason)
        self._assert_rest_pass(by_id)
        self.assertEqual(harness_lib.aggregate(version, results), (None, [], 2))

    def test_r1_unusable_voids_all_six(self):
        fx, r1, r2, calling, r3 = self._healthy(208)
        r1["exit"] = 1
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        self.assertEqual([r.id for r in results],
                         ["H1", "H2", "H3", "H4", "H5", "H6"])
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("R1 unusable", r.reason)

    def test_preexisting_projects_dir_voids_all_six(self):
        fx, r1, r2, calling, r3 = self._healthy(209)
        fx.preexisting_projects_dir = True
        version, results = harness_lib.evaluate(fx, r1, r2, calling, r3)
        self.assertEqual(len(results), 6)
        for r in results:
            self.assertEqual(r.status, "VOID", "{0}: {1}".format(r.id, r.reason))
            self.assertIn("cannot own", r.reason)


# ------------------------------------------------------------------- aggregate --

class TestAggregate(unittest.TestCase):

    def test_version_none_exits_2_even_when_every_probe_passed(self):
        # Acceptance 5.
        results = [harness_lib.ProbeResult(pid, "PASS", "ok")
                  for pid in ("H1", "H2", "H3", "H4", "H5", "H6")]
        self.assertEqual(harness_lib.aggregate(None, results), (None, [], 2))

    def test_any_fail_records_fail_and_exits_1(self):
        results = [
            harness_lib.ProbeResult("H1", "PASS", "ok"),
            harness_lib.ProbeResult("H2", "FAIL", "bad"),
            harness_lib.ProbeResult("H3", "VOID", "unreadable"),
            harness_lib.ProbeResult("H4", "PASS", "ok"),
            harness_lib.ProbeResult("H5", "PASS", "ok"),
            harness_lib.ProbeResult("H6", "PASS", "ok"),
        ]
        self.assertEqual(harness_lib.aggregate("9.9.9", results),
                         ("fail", ["H2"], 1))

    def test_any_void_with_no_fail_records_nothing_and_exits_2(self):
        results = [harness_lib.ProbeResult(pid, "VOID" if pid == "H3" else "PASS", "x")
                  for pid in ("H1", "H2", "H3", "H4", "H5", "H6")]
        self.assertEqual(harness_lib.aggregate("9.9.9", results), (None, [], 2))

    def test_partial_records_partial_with_the_ids_and_exits_0(self):
        results = [harness_lib.ProbeResult(pid, "PARTIAL" if pid == "H5" else "PASS", "x")
                  for pid in ("H1", "H2", "H3", "H4", "H5", "H6")]
        self.assertEqual(harness_lib.aggregate("9.9.9", results),
                         ("partial", ["H5"], 0))

    def test_fail_beats_partial(self):
        results = [harness_lib.ProbeResult("H1", "FAIL", "bad"),
                   harness_lib.ProbeResult("H2", "PASS", "ok"),
                   harness_lib.ProbeResult("H3", "PASS", "ok"),
                   harness_lib.ProbeResult("H4", "PASS", "ok"),
                   harness_lib.ProbeResult("H5", "PARTIAL", "x"),
                   harness_lib.ProbeResult("H6", "PASS", "ok")]
        self.assertEqual(harness_lib.aggregate("9.9.9", results),
                         ("fail", ["H1"], 1))

    def test_void_beats_partial(self):
        results = [harness_lib.ProbeResult("H1", "VOID", "x"),
                   harness_lib.ProbeResult("H2", "PASS", "ok"),
                   harness_lib.ProbeResult("H3", "PASS", "ok"),
                   harness_lib.ProbeResult("H4", "PASS", "ok"),
                   harness_lib.ProbeResult("H5", "PARTIAL", "x"),
                   harness_lib.ProbeResult("H6", "PASS", "ok")]
        self.assertEqual(harness_lib.aggregate("9.9.9", results), (None, [], 2))

    def test_all_pass_records_pass_and_exits_0(self):
        results = [harness_lib.ProbeResult(pid, "PASS", "ok")
                  for pid in ("H1", "H2", "H3", "H4", "H5", "H6")]
        self.assertEqual(harness_lib.aggregate("9.9.9", results), ("pass", [], 0))


class TestCallingLookupSkipKinds(unittest.TestCase):
    """`_calling_lookup` says WHY it skipped: couldn't check vs not applicable."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="harness-lookup-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.settings_path = os.path.join(self.tmp, "settings.json")
        self.projects_root = os.path.join(self.tmp, "projects")
        self.mem_dir = os.path.join(self.tmp, "mem")
        os.makedirs(self.mem_dir)
        saved = {k: os.environ.get(k) for k in (
            "HARNESS_CHECK_USER_SETTINGS", "HARNESS_CHECK_PROJECTS_ROOT",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY")}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.addCleanup(restore)
        os.environ["HARNESS_CHECK_USER_SETTINGS"] = self.settings_path
        os.environ["HARNESS_CHECK_PROJECTS_ROOT"] = self.projects_root
        os.environ.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)

    def _settings(self, **fields):
        with open(self.settings_path, "w", encoding="utf-8") as handle:
            json.dump(dict({"autoMemoryDirectory": self.mem_dir}, **fields), handle)

    def _transcript(self, with_automem=False):
        session_dir = os.path.join(self.projects_root, "-x")
        os.makedirs(session_dir, exist_ok=True)
        files = []
        if with_automem:
            files.append({"path": os.path.join(self.mem_dir, "MEMORY.md"),
                          "type": "AutoMem", "content": "c"})
        with open(os.path.join(session_dir, "abc.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "version": "9.9.9"}) + "\n")
            handle.write(json.dumps({"type": "attachment", "attachment": {
                "type": "instructions", "files": files}}) + "\n")

    def _write_memory(self, text):
        with open(os.path.join(self.mem_dir, "MEMORY.md"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def _check(self, expected_skip, expected_kind):
        calling = harness_lib._calling_lookup("abc")
        self.assertEqual(calling["skip"], expected_skip)
        self.assertEqual(calling["skip_kind"], expected_kind)

    def test_no_session_id_is_unchecked(self):
        calling = harness_lib._calling_lookup(None)
        self.assertEqual(calling["skip"], "not run from a Claude session")
        self.assertEqual(calling["skip_kind"], "unchecked")

    def test_missing_transcript_is_unchecked(self):
        self._settings()
        self._check("calling transcript not found", "unchecked")

    def test_settings_file_absent_is_not_applicable(self):
        self._check("no user-level autoMemoryDirectory", "not_applicable")

    def test_settings_without_the_key_is_not_applicable(self):
        with open(self.settings_path, "w", encoding="utf-8") as handle:
            json.dump({}, handle)
        self._check("no user-level autoMemoryDirectory", "not_applicable")

    def test_malformed_settings_is_unchecked(self):
        with open(self.settings_path, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        calling = harness_lib._calling_lookup("abc")
        self.assertEqual(calling["skip_kind"], "unchecked")
        self.assertIn("could not be read", calling["skip"])

    def test_unparseable_transcript_is_unchecked_not_not_applicable(self):
        # With no MEMORY.md, an unparseable
        # transcript could read as "nothing loaded" -> not_applicable -> PASS,
        # hiding a wrong-directory AutoMem. It cannot show what was loaded.
        self._settings()
        session_dir = os.path.join(self.projects_root, "-x")
        os.makedirs(session_dir, exist_ok=True)
        with open(os.path.join(session_dir, "abc.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("{not json\n")
        self._check("calling transcript has no readable instructions record", "unchecked")

    def test_parsed_transcript_with_no_memory_md_stays_not_applicable(self):
        # Control for the test above: a readable instructions record with no
        # AutoMem entry and no MEMORY.md is still a genuine not-applicable.
        self._settings()
        self._transcript()
        self._check("no MEMORY.md to load in the user-level autoMemoryDirectory",
                    "not_applicable")

    def test_auto_memory_disabled_by_env_is_not_applicable(self):
        self._settings()
        self._transcript()
        self._write_memory("x\n")
        os.environ["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
        self._check("auto-memory disabled in the calling session", "not_applicable")

    def test_auto_memory_enabled_false_is_not_applicable(self):
        self._settings(autoMemoryEnabled=False)
        self._transcript()
        self._write_memory("x\n")
        self._check("autoMemoryEnabled is false", "not_applicable")

    def test_empty_memory_md_is_not_applicable(self):
        self._settings()
        self._transcript()
        self._write_memory("")
        self._check("no MEMORY.md to load in the user-level autoMemoryDirectory",
                    "not_applicable")

    def test_absent_memory_md_is_not_applicable(self):
        self._settings()
        self._transcript()
        self._check("no MEMORY.md to load in the user-level autoMemoryDirectory",
                    "not_applicable")

    def test_unreadable_memory_md_is_unchecked(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root can read a chmod 000 file")
        self._settings()
        self._transcript()
        self._write_memory("x\n")
        path = os.path.join(self.mem_dir, "MEMORY.md")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        self._check("MEMORY.md could not be read", "unchecked")

    def test_a_resolved_lookup_has_no_skip_kind(self):
        self._settings()
        self._transcript(with_automem=True)
        self._write_memory("x\n")
        calling = harness_lib._calling_lookup("abc")
        self.assertIsNone(calling["skip"])
        self.assertIsNone(calling["skip_kind"])


class TestGeneratedHooksUnderCp1252Stdio(unittest.TestCase):
    """The baked hook scripts read UTF-8 payloads whatever the stdio encoding is."""

    def _run_hook(self, script_text, root, payload):
        hooks = os.path.join(root, "hooks")
        os.makedirs(hooks)
        script = os.path.join(hooks, "hook.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(script_text)
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        return subprocess.run([sys.executable, script], input=data, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_generated_loaded_hook_logs_a_non_ascii_path_under_cp1252(self):
        with tempfile.TemporaryDirectory() as root:
            self._run_hook(harness_lib._log_loaded_script(root), root,
                           {"file_path": "/tmp/Jos\u00e9/CLAUDE.md",
                            "load_reason": "x"})
            with open(os.path.join(root, "loaded.jsonl"), encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["file_path"], "/tmp/Jos\u00e9/CLAUDE.md")

    def test_generated_contain_hook_allows_its_own_read_under_cp1252(self):
        with tempfile.TemporaryDirectory() as tmp:
            # realpath: the hook compares realpaths, and macOS /var is a symlink.
            root = os.path.join(os.path.realpath(tmp), "fix\u00e9")
            os.makedirs(os.path.join(root, "src"))
            target = os.path.join(root, "src", "x.py")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("")
            result = self._run_hook(
                harness_lib._contain_script(root), root,
                {"tool_name": "Read", "tool_input": {"file_path": target}})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_contain_hook_blocks_and_logs_a_non_ascii_forbidden_read_under_cp1252(self):
        with tempfile.TemporaryDirectory() as tmp:
            # realpath: the hook compares realpaths, and macOS /var is a symlink.
            root = os.path.join(os.path.realpath(tmp), "fix\u00e9")
            os.makedirs(os.path.join(root, "src"))
            result = self._run_hook(
                harness_lib._contain_script(root), root,
                {"tool_name": "Read",
                 "tool_input": {"file_path": "/outside/Jos\u00e9/secrets"}})
            with open(os.path.join(root, "blocked.jsonl"), encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["tool_input"]["file_path"],
                         "/outside/Jos\u00e9/secrets")


if __name__ == "__main__":
    unittest.main(verbosity=2)
