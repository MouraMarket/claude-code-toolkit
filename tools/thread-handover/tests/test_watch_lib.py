import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOL_DIR)
import watch_lib

# claim_lib lives in the sibling tool, used only to assert CLAIMS_PATH agrees
# with its mirror in watch_lib (see TestOutstandingWork.test_claims_path_...).
sys.path.insert(0, os.path.normpath(
    os.path.join(TOOL_DIR, "..", "session-claim")))

# GIT_TIMEOUT (1.0s) bounds each git call in the prompt path, and a call that
# overruns it reads as None -- "unknown". On a loaded machine a fixture git
# call can overrun it and `assertTrue(None)` fails. The budget
# is the hook's business, not what these tests check, so they run with room.
_ORIG_GIT_TIMEOUT = watch_lib.GIT_TIMEOUT


def setUpModule():
    watch_lib.GIT_TIMEOUT = 30.0


def tearDownModule():
    watch_lib.GIT_TIMEOUT = _ORIG_GIT_TIMEOUT


def write_transcript(lines):
    """Write a list of dicts as a .jsonl file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def assistant_usage(input_tokens=0, cache_creation=0, cache_read=0,
                    model="claude-opus-5", sidechain=False):
    entry = {
        "type": "assistant",
        "timestamp": "2026-08-10T12:00:00.000Z",
        "message": {
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "hi"}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    }
    if sidechain:
        entry["isSidechain"] = True
    return entry


class TestReadContext(unittest.TestCase):

    def test_sums_all_three_token_fields(self):
        path = write_transcript([assistant_usage(2, 3000, 61000)])
        ctx, model = watch_lib.read_context(path)
        self.assertEqual(ctx, 64002)
        self.assertEqual(model, "claude-opus-5")

    def test_uses_last_entry_not_first(self):
        path = write_transcript([
            assistant_usage(0, 0, 10_000),
            assistant_usage(0, 0, 90_000),
        ])
        ctx, _ = watch_lib.read_context(path)
        self.assertEqual(ctx, 90_000)

    def test_sidechain_entries_are_excluded(self):
        path = write_transcript([
            assistant_usage(0, 0, 90_000),
            assistant_usage(0, 0, 5_000, sidechain=True),
        ])
        ctx, _ = watch_lib.read_context(path)
        self.assertEqual(ctx, 90_000)

    def test_malformed_lines_are_skipped(self):
        path = write_transcript([assistant_usage(0, 0, 42_000)])
        with open(path, "a") as f:
            f.write("{not json\n")
        ctx, _ = watch_lib.read_context(path)
        self.assertEqual(ctx, 42_000)

    def test_missing_file_returns_zero(self):
        ctx, model = watch_lib.read_context("/nonexistent/path.jsonl")
        self.assertEqual(ctx, 0)
        self.assertIsNone(model)

    def test_empty_transcript_returns_zero(self):
        path = write_transcript([])
        ctx, model = watch_lib.read_context(path)
        self.assertEqual(ctx, 0)
        self.assertIsNone(model)


class TestWindowFor(unittest.TestCase):

    def test_known_1m_models(self):
        for model in ("claude-opus-5", "claude-opus-4-8", "claude-sonnet-5",
                      "claude-sonnet-4-6", "claude-fable-5", "claude-opus-5[1m]"):
            self.assertEqual(watch_lib.window_for(model), 1_000_000, model)

    def test_haiku_is_200k(self):
        self.assertEqual(watch_lib.window_for("claude-haiku-4-5-20251001"), 200_000)

    def test_unknown_model_defaults_to_200k(self):
        self.assertEqual(watch_lib.window_for("some-future-model"), 200_000)

    def test_none_defaults_to_200k(self):
        self.assertEqual(watch_lib.window_for(None), 200_000)


class TestThresholds(unittest.TestCase):

    def test_1m_window_uses_absolute_caps(self):
        self.assertEqual(watch_lib.thresholds(1_000_000), (250_000, 700_000))

    def test_200k_window_uses_fractions(self):
        arm, urgent = watch_lib.thresholds(200_000)
        self.assertEqual(arm, 70_000)
        self.assertEqual(urgent, 140_000)


class TestStageFor(unittest.TestCase):

    W = 1_000_000

    def test_below_arm_is_quiet(self):
        self.assertEqual(watch_lib.stage_for(249_999, self.W), "quiet")

    def test_at_arm_is_armed(self):
        self.assertEqual(watch_lib.stage_for(250_000, self.W), "armed")

    def test_just_below_urgent_is_armed(self):
        self.assertEqual(watch_lib.stage_for(699_999, self.W), "armed")

    def test_at_urgent_is_urgent(self):
        self.assertEqual(watch_lib.stage_for(700_000, self.W), "urgent")

    def test_snooze_suppresses_armed(self):
        # snoozed at 300k, +10% of 1M = quiet until 400k
        self.assertEqual(watch_lib.stage_for(350_000, self.W, snoozed_at=300_000),
                         "quiet")

    def test_snooze_expires_after_ten_percent(self):
        self.assertEqual(watch_lib.stage_for(400_000, self.W, snoozed_at=300_000),
                         "armed")

    def test_snooze_does_not_suppress_urgent(self):
        self.assertEqual(watch_lib.stage_for(750_000, self.W, snoozed_at=740_000),
                         "urgent")

    def test_zero_context_is_quiet(self):
        self.assertEqual(watch_lib.stage_for(0, self.W), "quiet")


def user_entry(text, timestamp="2026-08-10T12:00:00.000Z"):
    return {"type": "user", "timestamp": timestamp,
            "message": {"role": "user", "content": text}}


def tool_use_entry(name, tool_input, block_id="toolu_1"):
    return {"type": "assistant", "timestamp": "2026-08-10T12:00:00.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": block_id, "name": name,
                 "input": tool_input}]}}


def tool_result_entry(text, tool_use_id="toolu_1"):
    return {"type": "user", "timestamp": "2026-08-10T12:00:00.000Z",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_use_id,
                 "content": text}]}}


HANDOVER = "/specs/2026-08-10-demo-handover.md"


class TestMentionsPath(unittest.TestCase):

    def test_attachment_in_a_prompt_counts(self):
        entries = [user_entry("@specs/2026-08-10-demo-handover.md go")]
        self.assertTrue(watch_lib.mentions_path(HANDOVER, entries))

    def test_bare_basename_in_a_prompt_counts(self):
        entries = [user_entry("pick up 2026-08-10-demo-handover.md please")]
        self.assertTrue(watch_lib.mentions_path(HANDOVER, entries))

    def test_block_list_prompt_counts(self):
        # Real attachments arrive as a text-block list, not a bare string.
        entries = [{"type": "user", "message": {"role": "user", "content": [
            {"type": "text", "text": "@2026-08-10-demo-handover.md continue"}]}}]
        self.assertTrue(watch_lib.mentions_path(HANDOVER, entries))

    def test_tool_result_does_not_count(self):
        # The merge-marker lesson: an `ls` of specs/ names every handover in it.
        entries = [tool_result_entry("2026-08-10-demo-handover.md\nother.md")]
        self.assertFalse(watch_lib.mentions_path(HANDOVER, entries))

    def test_tool_use_input_does_not_count(self):
        entries = [tool_use_entry("Read", {"file_path": HANDOVER})]
        self.assertFalse(watch_lib.mentions_path(HANDOVER, entries))

    def test_unrelated_prompt_does_not_count(self):
        entries = [user_entry("let's continue where we left off")]
        self.assertFalse(watch_lib.mentions_path(HANDOVER, entries))

    def test_different_handover_does_not_count(self):
        entries = [user_entry("@2026-08-09-gads-handover.md go")]
        self.assertFalse(watch_lib.mentions_path(HANDOVER, entries))

    def test_empty_path_is_false(self):
        self.assertFalse(watch_lib.mentions_path("", [user_entry("hi")]))
        self.assertFalse(watch_lib.mentions_path(None, [user_entry("hi")]))

    def test_no_entries_is_false(self):
        self.assertFalse(watch_lib.mentions_path(HANDOVER, []))


class TestDetectBoundaries(unittest.TestCase):

    def test_gh_pr_merge_command(self):
        entries = [tool_use_entry("Bash", {"command": "gh pr merge 42 --squash"})]
        self.assertIn("pr_merged", watch_lib.detect_boundaries(entries))

    def test_git_tidy_command(self):
        entries = [tool_use_entry("Bash", {"command": "git tidy"})]
        self.assertIn("pr_merged", watch_lib.detect_boundaries(entries))

    def test_merge_marker_in_bash_tool_result(self):
        entries = [
            tool_use_entry("Bash", {"command": "gh pr view 17"}, block_id="b1"),
            tool_result_entry("Squashed and merged PR #17", tool_use_id="b1"),
        ]
        self.assertIn("pr_merged", watch_lib.detect_boundaries(entries))

    def test_merge_marker_in_read_result_does_not_fire(self):
        # Reading this plan file — which documents these very markers — must not
        # look like a merge. The rule self-triggered before this guard existed.
        entries = [
            tool_use_entry("Read", {"file_path": "plan.md"}, block_id="r1"),
            tool_result_entry("| `pr_merged` | ... `Squashed and merged` ...",
                              tool_use_id="r1"),
        ]
        self.assertNotIn("pr_merged", watch_lib.detect_boundaries(entries))

    def test_unrelated_bash_does_not_fire(self):
        entries = [tool_use_entry("Bash", {"command": "git status"})]
        self.assertEqual(watch_lib.detect_boundaries(entries), [])

    def test_idle_gap_over_thirty_minutes(self):
        entries = [user_entry("a", "2026-08-10T12:00:00.000Z"),
                   user_entry("b", "2026-08-10T12:45:00.000Z")]
        self.assertIn("idle_gap", watch_lib.detect_boundaries(entries))

    def test_short_gap_does_not_fire(self):
        entries = [user_entry("a", "2026-08-10T12:00:00.000Z"),
                   user_entry("b", "2026-08-10T12:10:00.000Z")]
        self.assertNotIn("idle_gap", watch_lib.detect_boundaries(entries))

    def test_single_user_entry_does_not_fire_idle_gap(self):
        self.assertEqual(watch_lib.detect_boundaries([user_entry("a")]), [])

    def test_all_todos_completed(self):
        entries = [tool_use_entry("TodoWrite", {"todos": [
            {"content": "x", "status": "completed"},
            {"content": "y", "status": "completed"}]})]
        self.assertIn("phase_done", watch_lib.detect_boundaries(entries))

    def test_partial_todos_does_not_fire(self):
        entries = [tool_use_entry("TodoWrite", {"todos": [
            {"content": "x", "status": "completed"},
            {"content": "y", "status": "in_progress"}]})]
        self.assertNotIn("phase_done", watch_lib.detect_boundaries(entries))

    def test_empty_todos_does_not_fire(self):
        entries = [tool_use_entry("TodoWrite", {"todos": []})]
        self.assertNotIn("phase_done", watch_lib.detect_boundaries(entries))

    def test_executor_done_result(self):
        entries = [
            tool_use_entry("Task", {"subagent_type": "executor"}, block_id="t1"),
            tool_result_entry("Task 3 complete. DONE", tool_use_id="t1"),
        ]
        self.assertIn("phase_done", watch_lib.detect_boundaries(entries))

    def test_namespaced_executor_done_result(self):
        entries = [
            tool_use_entry("Agent", {"subagent_type": "toolkit-build:executor"}, block_id="t1"),
            tool_result_entry("Task 3 complete. DONE", tool_use_id="t1"),
        ]
        self.assertIn("phase_done", watch_lib.detect_boundaries(entries))

    def test_namespaced_task_reviewer_done_result(self):
        entries = [
            tool_use_entry("Task", {"subagent_type": "toolkit-build:task-reviewer"}, block_id="t1"),
            tool_result_entry("Review complete. DONE", tool_use_id="t1"),
        ]
        self.assertIn("phase_done", watch_lib.detect_boundaries(entries))

    def test_bare_agent_type_strips_the_toolkit_prefix_only(self):
        self.assertEqual("executor", watch_lib._bare_agent_type("toolkit-build:executor"))
        self.assertEqual("code-reviewer", watch_lib._bare_agent_type("toolkit-build:code-reviewer"))
        self.assertEqual("executor", watch_lib._bare_agent_type("executor"))
        self.assertEqual("other:executor", watch_lib._bare_agent_type("other:executor"))

    def test_another_plugins_executor_does_not_fire(self):
        entries = [
            tool_use_entry("Task", {"subagent_type": "other:executor"}, block_id="t1"),
            tool_result_entry("DONE", tool_use_id="t1"),
        ]
        self.assertNotIn("phase_done", watch_lib.detect_boundaries(entries))

    def test_done_from_unmatched_id_does_not_fire(self):
        entries = [
            tool_use_entry("Task", {"subagent_type": "executor"}, block_id="t1"),
            tool_result_entry("DONE", tool_use_id="other"),
        ]
        self.assertNotIn("phase_done", watch_lib.detect_boundaries(entries))

    def test_other_subagent_type_does_not_fire(self):
        entries = [
            tool_use_entry("Task", {"subagent_type": "Explore"}, block_id="t1"),
            tool_result_entry("DONE", tool_use_id="t1"),
        ]
        self.assertNotIn("phase_done", watch_lib.detect_boundaries(entries))

    def test_empty_input_is_safe(self):
        self.assertEqual(watch_lib.detect_boundaries([]), [])


class TestNoClientCodes(unittest.TestCase):
    """The public toolkit detects no change of client or project code."""

    def test_no_roster_or_code_machinery(self):
        for name in ("ROSTER_PATH", "load_client_codes", "detect_topic_shift",
                     "_dominant_code", "_CODE_RE"):
            self.assertFalse(hasattr(watch_lib, name), name)

    def test_a_change_of_three_letter_codes_is_not_a_boundary(self):
        path = write_transcript([user_entry("pull QQQ numbers")] * 10
                                + [user_entry("now ZZZ creative")] * 5)
        self.addCleanup(os.unlink, path)
        self.assertEqual(watch_lib.boundaries_for(path), [])


class TestMalformedShapes(unittest.TestCase):
    """A truthy non-dict `message` must degrade, never raise.

    Regression guard: `entry.get("message") or {}` passes a bare string
    straight through, and the next .get() raises AttributeError.
    """

    # type "user", so the entries reach the prompt checks in detect_boundaries
    # and mentions_path rather than being filtered out first.
    BAD = [{"type": "user", "timestamp": "2026-08-10T12:00:00.000Z",
            "message": "not-a-dict"}]

    def test_read_context_survives_string_message(self):
        path = write_transcript(self.BAD + [assistant_usage(0, 0, 5_000)])
        ctx, _ = watch_lib.read_context(path)
        self.assertEqual(ctx, 5_000)

    def test_detect_boundaries_survives_string_message(self):
        self.assertEqual(watch_lib.detect_boundaries(self.BAD), [])

    def test_mentions_path_survives_string_message(self):
        self.assertFalse(watch_lib.mentions_path(
            "/h/2026-08-10-x-handover.md", self.BAD * 20))


class TestState(unittest.TestCase):

    def setUp(self):
        self._original = watch_lib.STATE_DIR
        self._tmp = tempfile.mkdtemp()
        watch_lib.STATE_DIR = self._tmp

    def tearDown(self):
        watch_lib.STATE_DIR = self._original

    def test_missing_state_returns_defaults(self):
        state = watch_lib.load_state("new-session")
        self.assertEqual(state["stage"], "quiet")
        self.assertEqual(state["turn"], 0)
        self.assertEqual(state["signals"], [])
        self.assertIsNone(state["armed_at"])
        self.assertIsNone(state["snoozed_at"])

    def test_round_trip(self):
        state = watch_lib.load_state("s1")
        state["stage"] = "armed"
        state["turn"] = 7
        state["signals"] = ["idle_gap"]
        watch_lib.save_state("s1", state)
        self.assertEqual(watch_lib.load_state("s1")["turn"], 7)
        self.assertEqual(watch_lib.load_state("s1")["signals"], ["idle_gap"])

    def test_corrupt_state_returns_defaults(self):
        path = os.path.join(self._tmp, "bad.json")
        with open(path, "w") as handle:
            handle.write("{not json")
        self.assertEqual(watch_lib.load_state("bad")["stage"], "quiet")

    def test_save_creates_directory(self):
        watch_lib.STATE_DIR = os.path.join(self._tmp, "deep", "nested")
        watch_lib.save_state("s2", watch_lib.load_state("s2"))
        self.assertTrue(os.path.isdir(watch_lib.STATE_DIR))


class TestShouldInject(unittest.TestCase):

    def base(self, **overrides):
        state = {"stage": "quiet", "armed_at": None, "signals": [],
                 "turn": 1, "last_inject_turn": 0, "snoozed_at": None}
        state.update(overrides)
        return state

    def test_urgent_always_injects(self):
        state = self.base(stage="urgent", turn=2, last_inject_turn=2)
        self.assertTrue(watch_lib.should_inject(state, "urgent", []))

    def test_stage_change_injects(self):
        state = self.base(stage="quiet", turn=3, last_inject_turn=1)
        self.assertTrue(watch_lib.should_inject(state, "armed", []))

    def test_new_signal_injects(self):
        state = self.base(stage="armed", signals=[], turn=3, last_inject_turn=2)
        self.assertTrue(watch_lib.should_inject(state, "armed", ["idle_gap"]))

    def test_same_signal_does_not_inject(self):
        state = self.base(stage="armed", signals=["idle_gap"],
                          turn=3, last_inject_turn=2)
        self.assertFalse(watch_lib.should_inject(state, "armed", ["idle_gap"]))

    def test_fifth_turn_injects(self):
        state = self.base(stage="armed", turn=7, last_inject_turn=2)
        self.assertTrue(watch_lib.should_inject(state, "armed", []))

    def test_quiet_never_injects(self):
        state = self.base(stage="quiet", turn=99, last_inject_turn=0)
        self.assertFalse(watch_lib.should_inject(state, "quiet", []))

    def test_steady_armed_stays_silent(self):
        state = self.base(stage="armed", turn=4, last_inject_turn=2)
        self.assertFalse(watch_lib.should_inject(state, "armed", []))


class TestPromptDetection(unittest.TestCase):

    def test_plain_user_entry_is_a_prompt(self):
        self.assertTrue(watch_lib._is_prompt(user_entry("do the thing")))

    def test_tool_result_entry_is_not_a_prompt(self):
        self.assertFalse(watch_lib._is_prompt(tool_result_entry("output")))

    def test_idle_gap_ignores_tool_result_timing(self):
        # A slow tool call is not an idle human. Real gap here is zero.
        entries = [user_entry("a", "2026-08-10T12:00:00.000Z"),
                   tool_result_entry("slow output")]
        entries[1]["timestamp"] = "2026-08-10T12:45:00.000Z"
        self.assertNotIn("idle_gap", watch_lib.detect_boundaries(entries))

    def test_idle_gap_still_fires_between_real_prompts(self):
        entries = [user_entry("a", "2026-08-10T12:00:00.000Z"),
                   tool_result_entry("fast output"),
                   user_entry("b", "2026-08-10T12:45:00.000Z")]
        entries[1]["timestamp"] = "2026-08-10T12:00:30.000Z"
        self.assertIn("idle_gap", watch_lib.detect_boundaries(entries))


class TestInjectFloor(unittest.TestCase):

    def test_default_state_has_last_inject_at(self):
        original = watch_lib.STATE_DIR
        try:
            watch_lib.STATE_DIR = tempfile.mkdtemp()
            self.assertIsNone(watch_lib.load_state("fresh")["last_inject_at"])
        finally:
            watch_lib.STATE_DIR = original

    def test_scan_floor_prefers_last_inject_at(self):
        self.assertEqual(
            watch_lib.scan_floor({"armed_at": "2026-08-10T12:00:00+00:00",
                                  "last_inject_at": "2026-08-10T13:00:00+00:00"}),
            "2026-08-10T13:00:00+00:00")

    def test_scan_floor_falls_back_to_armed_at(self):
        self.assertEqual(
            watch_lib.scan_floor({"armed_at": "2026-08-10T12:00:00+00:00",
                                  "last_inject_at": None}),
            "2026-08-10T12:00:00+00:00")


def _run_git(*args, cwd):
    subprocess.run(["git", "-C", cwd] + list(args), check=True,
                   capture_output=True, text=True)


def _init_repo(path):
    """A real one-commit repo on a branch named `main`, for checkout_holds_work
    tests. Named explicitly rather than trusting `git init`'s default, which
    varies with the machine's init.defaultBranch config."""
    os.makedirs(path, exist_ok=True)
    _run_git("init", "-q", cwd=path)
    _run_git("checkout", "-q", "-b", "main", cwd=path)
    _run_git("config", "user.email", "test@example.com", cwd=path)
    _run_git("config", "user.name", "Test", cwd=path)
    with open(os.path.join(path, "file.txt"), "w") as handle:
        handle.write("hello\n")
    _run_git("add", ".", cwd=path)
    _run_git("commit", "-q", "-m", "init", cwd=path)


class TestLastTodoStatus(unittest.TestCase):
    """Criteria 1 and 2. Criterion 1 is the ordering test -- it must fail if
    the function early-returns on the first match instead of the last."""

    def test_last_all_completed_overrides_earlier_incomplete(self):
        path = write_transcript([
            tool_use_entry("TodoWrite", {"todos": [
                {"content": "x", "status": "in_progress"},
                {"content": "y", "status": "pending"}]}),
            tool_use_entry("TodoWrite", {"todos": [
                {"content": "x", "status": "completed"},
                {"content": "y", "status": "completed"}]}),
        ])
        # assertIs, not assertFalse -- None is falsy too, and this must prove
        # the real all-completed case, not merely "not truthy".
        self.assertIs(watch_lib.last_todo_status(path), False)

    def test_last_incomplete_returns_true(self):
        path = write_transcript([
            tool_use_entry("TodoWrite", {"todos": [
                {"content": "x", "status": "completed"},
                {"content": "y", "status": "completed"}]}),
            tool_use_entry("TodoWrite", {"todos": [
                {"content": "x", "status": "completed"},
                {"content": "y", "status": "in_progress"}]}),
        ])
        self.assertTrue(watch_lib.last_todo_status(path))

    def test_no_todowrite_returns_none(self):
        path = write_transcript([user_entry("hi")])
        self.assertIsNone(watch_lib.last_todo_status(path))

    def test_only_empty_todo_lists_returns_none(self):
        path = write_transcript([
            tool_use_entry("TodoWrite", {"todos": []}),
            tool_use_entry("TodoWrite", {"todos": []}),
        ])
        self.assertIsNone(watch_lib.last_todo_status(path))


class TestCheckoutHoldsWork(unittest.TestCase):
    """Criteria 3 and 4. Real temp git repos via `git init` -- not a mocked
    subprocess, since the thing under test is how real git behaves."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_dirty_tree_is_true(self):
        repo = os.path.join(self._tmp, "dirty")
        _init_repo(repo)
        with open(os.path.join(repo, "untracked.txt"), "w") as handle:
            handle.write("scratch\n")
        self.assertTrue(watch_lib.checkout_holds_work(repo))

    def test_clean_non_ahead_repo_is_false(self):
        repo = os.path.join(self._tmp, "clean")
        _init_repo(repo)
        # assertIs, not assertFalse -- None is falsy too, and this must prove
        # the real clean/not-ahead case, not merely "not a git repo".
        self.assertIs(watch_lib.checkout_holds_work(repo), False)

    def test_clean_but_ahead_of_default_branch_is_true(self):
        repo = os.path.join(self._tmp, "ahead")
        _init_repo(repo)
        _run_git("checkout", "-q", "-b", "feature", cwd=repo)
        with open(os.path.join(repo, "extra.txt"), "w") as handle:
            handle.write("more\n")
        _run_git("add", ".", cwd=repo)
        _run_git("commit", "-q", "-m", "extra", cwd=repo)
        self.assertTrue(watch_lib.checkout_holds_work(repo))

    def test_non_repo_directory_is_none(self):
        not_repo = os.path.join(self._tmp, "not-a-repo")
        os.makedirs(not_repo)
        self.assertIsNone(watch_lib.checkout_holds_work(not_repo))

    def test_nonexistent_cwd_is_none(self):
        self.assertIsNone(watch_lib.checkout_holds_work(
            os.path.join(self._tmp, "does-not-exist")))

    def test_git_missing_from_path_is_none(self):
        repo = os.path.join(self._tmp, "clean2")
        _init_repo(repo)
        old_path = os.environ.get("PATH")
        try:
            os.environ["PATH"] = self._tmp  # a directory that has no git binary
            self.assertIsNone(watch_lib.checkout_holds_work(repo))
        finally:
            if old_path is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old_path


    def test_no_upstream_and_no_main_or_master_is_none(self):
        # A clean tree is NOT an answer to "does HEAD carry commits that exist
        # nowhere else" -- those commits would be clean too. With no upstream
        # and no main/master there is nothing local to compare against, so the
        # honest answer is unknown. Returning False here would suppress a
        # handover for a repo whose default branch is simply named something
        # else, which is a wrong suppression on an ABSENCE of evidence.
        path = os.path.join(self._tmp, "trunk-repo")
        _init_repo(path)
        _run_git("branch", "-m", "main", "trunk", cwd=path)
        self.assertIsNone(watch_lib.checkout_holds_work(path))

    def test_no_upstream_no_main_but_dirty_still_reports_true(self):
        # The unknown above must not swallow a signal we DO have: a dirty tree
        # is decided before any base-branch comparison is attempted.
        path = os.path.join(self._tmp, "trunk-dirty")
        _init_repo(path)
        _run_git("branch", "-m", "main", "trunk", cwd=path)
        with open(os.path.join(path, "file.txt"), "a") as handle:
            handle.write("changed\n")
        self.assertIs(watch_lib.checkout_holds_work(path), True)


class TestOutstandingWork(unittest.TestCase):
    """Criteria 5, 5b, 5c, 5d."""

    def setUp(self):
        self._orig_claims = watch_lib.CLAIMS_PATH
        self._tmp = tempfile.mkdtemp()

    def tearDown(self):
        watch_lib.CLAIMS_PATH = self._orig_claims
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _write_claims(self, records):
        path = os.path.join(self._tmp, "claims.json")
        with open(path, "w") as handle:
            json.dump(records, handle)
        watch_lib.CLAIMS_PATH = path

    def test_measured_case_no_todo_non_repo_cwd_clean_claim_is_false(self):
        # THE motivating case (design spec, 1b-pre): no TodoWrite anywhere, the
        # hook payload's cwd is the workspace root -- not a git repo at all --
        # and the session's actually-claimed worktree is clean and not ahead.
        # Requiring BOTH signals to be False (rather than "no True and >=1
        # False") would suppress nothing here, forever: the todo signal is
        # None, not False.
        transcript = write_transcript([user_entry("hi")])
        workspace_root = os.path.join(self._tmp, "workspace-root")
        os.makedirs(workspace_root)
        worktree = os.path.join(self._tmp, "worktree")
        _init_repo(worktree)
        self._write_claims([{"session_id": "s1", "checkout": worktree}])
        # assertIs, not assertFalse -- None is falsy too, and this is exactly
        # the distinction a "require both dimensions False" bug would blur:
        # todo_signal is None here, so a buggy AND-of-both-False check would
        # return None instead of False and this assertion must catch that.
        self.assertIs(
            watch_lib.outstanding_work(transcript, workspace_root, "s1"), False)

    def test_dirty_claimed_checkout_is_true_even_when_cwd_is_not_a_repo(self):
        # Without this, the claims lookup could be entirely inert and every
        # other test here would still pass.
        transcript = write_transcript([user_entry("hi")])
        workspace_root = os.path.join(self._tmp, "workspace-root")
        os.makedirs(workspace_root)
        worktree = os.path.join(self._tmp, "worktree")
        _init_repo(worktree)
        with open(os.path.join(worktree, "scratch.txt"), "w") as handle:
            handle.write("wip\n")
        self._write_claims([{"session_id": "s1", "checkout": worktree}])
        self.assertTrue(
            watch_lib.outstanding_work(transcript, workspace_root, "s1"))

    def test_claims_path_mirrors_claim_lib(self):
        import claim_lib
        self.assertEqual(watch_lib.CLAIMS_PATH, claim_lib.CLAIMS_PATH)

    def test_missing_claims_file_contributes_no_candidates(self):
        transcript = write_transcript([user_entry("hi")])
        workspace_root = os.path.join(self._tmp, "workspace-root")
        os.makedirs(workspace_root)
        watch_lib.CLAIMS_PATH = os.path.join(self._tmp, "does-not-exist.json")
        self.assertIsNone(
            watch_lib.outstanding_work(transcript, workspace_root, "s1"))

    def test_unparseable_claims_file_contributes_no_candidates(self):
        transcript = write_transcript([user_entry("hi")])
        workspace_root = os.path.join(self._tmp, "workspace-root")
        os.makedirs(workspace_root)
        path = os.path.join(self._tmp, "claims.json")
        with open(path, "w") as handle:
            handle.write("{not json")
        watch_lib.CLAIMS_PATH = path
        self.assertIsNone(
            watch_lib.outstanding_work(transcript, workspace_root, "s1"))

    def test_claims_for_other_sessions_only_contributes_no_candidates(self):
        transcript = write_transcript([user_entry("hi")])
        workspace_root = os.path.join(self._tmp, "workspace-root")
        os.makedirs(workspace_root)
        worktree = os.path.join(self._tmp, "worktree")
        _init_repo(worktree)
        self._write_claims([{"session_id": "other-session", "checkout": worktree}])
        self.assertIsNone(
            watch_lib.outstanding_work(transcript, workspace_root, "s1"))


class TestShouldInjectOutstanding(unittest.TestCase):
    """Criteria 6 and 7, and mutations 3 and 4."""

    def base(self, **overrides):
        state = {"stage": "quiet", "armed_at": None, "signals": [],
                 "turn": 1, "last_inject_turn": 0, "snoozed_at": None}
        state.update(overrides)
        return state

    def test_outstanding_false_suppresses_armed(self):
        state = self.base(stage="quiet", turn=3, last_inject_turn=1)
        self.assertFalse(
            watch_lib.should_inject(state, "armed", [], outstanding=False))

    def test_outstanding_false_does_not_suppress_urgent(self):
        state = self.base(stage="urgent", turn=2, last_inject_turn=2)
        self.assertTrue(
            watch_lib.should_inject(state, "urgent", [], outstanding=False))

    def test_outstanding_omitted_matches_prior_behaviour(self):
        # Every existing should_inject test calls it without `outstanding` at
        # all; this just names the contract explicitly for this signature.
        state = self.base(stage="armed", turn=4, last_inject_turn=2)
        self.assertFalse(watch_lib.should_inject(state, "armed", []))

    def test_outstanding_none_does_not_suppress(self):
        # None means "couldn't tell" and must never be read as a positive
        # finding of no-work -- only `is False` may suppress. A stage change
        # ordinarily injects; if outstanding=None were treated as falsy (the
        # `not outstanding` mutation) it would suppress this too.
        state = self.base(stage="quiet", turn=3, last_inject_turn=1)
        self.assertTrue(
            watch_lib.should_inject(state, "armed", [], outstanding=None))


if __name__ == "__main__":
    unittest.main()
