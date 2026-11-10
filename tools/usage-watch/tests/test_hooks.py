"""Payload-in / exit-code-out tests for all four usage-watch executables, run
as real subprocesses.

Running them as processes rather than importing them is deliberate, and this
suite is the reason the tool exists in its current form: a whole-branch review
reintroduced two previously-fixed executable bugs (the wrong StopFailure key;
a subagent's PostToolBatch bleeding into the main loop) and the 93-test
`test_usage_lib.py` suite stayed fully green, because not one line of any
executable had ever been run. Two further defects (a written but unregistered
handover; UserPromptSubmit silenced by dedupe meant for PostToolBatch) lived in
the same blind spot for the whole build. Follow
`tools/session-claim/tests/test_hooks.py` and
`tools/thread-handover/tests/test_watch_hook.py` for the established shape.

Isolation is harder here than in those two tools. `cache_path`/`state_path`/
`handover_dir` all resolve through `CLAUDE_TOOLKIT_CONFIG`, so a per-test
config file is enough for those. But `index_lib.INDEX_PATH` — where
`claude-usage-postmortem` registers a handover so `claude-thread-deliver` can
ever see it — is a module-level constant computed once from `~/.claude/...`
with no config knob at all. The only way to keep that registration off the
real index is to run the postmortem subprocess with `HOME` pointed at a
throwaway directory, so `os.path.expanduser("~")` resolves somewhere harmless
before `index_lib` is ever imported. Every postmortem invocation in this file
does that, and `TestPostmortemRealIndexUntouched` asserts the real index
never moved.
"""
import ast
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINE = os.path.join(TOOL_DIR, "claude-usage-line")
WATCH = os.path.join(TOOL_DIR, "claude-usage-watch")
SUBAGENT = os.path.join(TOOL_DIR, "claude-usage-subagent")
POSTMORTEM = os.path.join(TOOL_DIR, "claude-usage-postmortem")
DISPATCH = os.path.join(TOOL_DIR, "claude-usage-dispatch")
EXECUTABLES = (LINE, WATCH, SUBAGENT, POSTMORTEM, DISPATCH)

# CODEX_HOME isolation, applied at module import -- before ANY spawn
# below builds a subprocess env. Every run() env is built from
# dict(os.environ), so leaving CODEX_HOME pointing at this machine's real
# ~/.codex would have every pre-existing test in this file read this
# machine's actual Codex session logs through the new Codex-line code path.
# An empty, otherwise untouched temp dir means default_sessions_root()
# resolves to a directory with no "sessions" subfolder, so claude-usage-watch
# finds no Codex line to add and every existing test's behaviour is
# unchanged -- see TestUsageWatchCodex for the class that overrides this
# per-test with a real fixture.
os.environ["CODEX_HOME"] = tempfile.mkdtemp()

# Only for HANDOVER_NAME_RE -- a read of a module-level regex constant, never
# a write. Real registration checks below read the redirected-HOME index file
# by hand, never through this import, since index_lib.INDEX_PATH in THIS
# (parent) process was already resolved against the real HOME at import time.
sys.path.insert(0, os.path.join(os.path.dirname(TOOL_DIR), "thread-handover"))
import index_lib  # noqa: E402

# The shared Codex library, for the tests that build calibrated launchers and
# rollout fixtures. claude-usage-watch itself must still run without it.
sys.path.insert(0, os.path.join(os.path.dirname(TOOL_DIR), "codex-review"))
try:
    import codex_review_lib  # noqa: E402,F401
except ImportError:
    codex_review_lib = None

sys.path.insert(0, TOOL_DIR)
import estimate_lib as est  # noqa: E402

# A synthetic tokens-per-percent ratio. Nothing ships one, so every fixture
# that needs a band sets this through `usage_watch.default_ratio` or a stored
# measured ratio, and sizes its token counts from this one name: a fixture
# whose arithmetic restates the number goes stale the day the number moves.
FIXTURE_RATIO = 20000.0

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(TOOL_DIR)), "lib"))
import hook_manifest  # noqa: E402
# Only for UNKNOWN_BUCKET, so the state-file assertion below names the bucket
# the code actually writes rather than restating the string.
import usage_lib as usage  # noqa: E402

# StopFailure's real ten-value enum (verified against the v2.1.228 binary).
# Only the first two are ours.
OTHER_EIGHT_REASONS = ("overloaded", "authentication_failed",
                      "oauth_org_not_allowed", "invalid_request",
                      "model_not_found", "server_error", "max_output_tokens",
                      "unknown")

REAL_INDEX_PATH = os.path.join(os.path.expanduser("~"),
                               ".claude", "thread-handover", "index.json")


def write_config(tmp_dir, usage_watch=None, thread_handover=None,
                 build_cost=None):
    """A config file redirecting every path this tool touches into `tmp_dir`,
    with any per-test overrides merged on top.

    `build_cost` exists because the dispatch hook reads the build-cost ledger,
    and that path's key is `build_cost.ledger_path` — it has been that key
    since the ledger shipped. Putting a `ledger_path` under `usage_watch`
    instead would be silently ignored, and the hook would read the REAL ledger
    on this machine while the test looked hermetic.
    """
    blob = {
        "usage_watch": dict({
            "cache_path": os.path.join(tmp_dir, "budget.json"),
            "state_path": os.path.join(tmp_dir, "state.json"),
            "transcript_root": os.path.join(tmp_dir, "projects"),
            "anchor_path": os.path.join(tmp_dir, "anchors.json"),
        }, **(usage_watch or {})),
        "thread_handover": dict({
            "handover_dir": os.path.join(tmp_dir, "handovers"),
        }, **(thread_handover or {})),
        "build_cost": dict({
            "ledger_path": os.path.join(tmp_dir, "build-costs.jsonl"),
        }, **(build_cost or {})),
    }
    path = os.path.join(tmp_dir, "config.json")
    with open(path, "w") as handle:
        json.dump(blob, handle)
    return path


def seed_cache(cache_path, buckets, written_at=None):
    with open(cache_path, "w") as handle:
        json.dump({"written_at": written_at if written_at is not None
                   else time.time(), "buckets": buckets}, handle)


def run(hook, payload=None, config=None, env=None, raw=None, home=None):
    """Run an executable as a subprocess. `config` is a path (falling back to
    a nonexistent one, matching the repo's no-config-at-all guarantee);
    `home` redirects HOME for the postmortem/index-isolation cases; `raw`
    sends a literal string instead of JSON, for malformed-input tests."""
    environment = dict(os.environ)
    environment["CLAUDE_TOOLKIT_CONFIG"] = config if config is not None \
        else "/nonexistent.json"
    # STRIP THE SURFACE VARIABLE unless a test sets it deliberately. Otherwise
    # every hook subprocess inherits whichever surface happens to be running
    # the suite — measured: a `claude-vscode` session makes four of these tests
    # fail and a terminal makes them pass, on identical code. That is the
    # ambient-state dependency this repo bans, and it is the nastier kind: the
    # suite does not merely become flaky, it asserts a DIFFERENT contract per
    # machine. Absent, `status_line_surface()` returns None — "cannot tell" —
    # which is the relay-keeping fallback and therefore exactly the behaviour
    # every test here was written against before surface detection existed.
    environment.pop("CLAUDE_CODE_ENTRYPOINT", None)
    if home is not None:
        environment["HOME"] = home
    if env:
        environment.update(env)
    if raw is not None:
        stdin = raw
    elif payload is None:
        stdin = ""
    else:
        stdin = json.dumps(payload)
    return subprocess.run([sys.executable, hook], input=stdin,
                          capture_output=True, text=True, env=environment,
                          timeout=10)


def read_fake_index(fake_home):
    """The registered handover records under a redirected HOME, or []."""
    path = os.path.join(fake_home, ".claude", "thread-handover", "index.json")
    try:
        with open(path) as handle:
            data = json.load(handle)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def snapshot_real_index():
    try:
        with open(REAL_INDEX_PATH) as handle:
            return handle.read()
    except Exception:
        return None


class TestExitZeroOnAnyInput(unittest.TestCase):
    """Every executable is best-effort and exits 0 on anything it is handed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = write_config(self.tmp)

    def _valid_payload(self):
        # Carries enough of everything (agent_id, error, reason, rate_limits)
        # that no executable takes an early-return path before its own I/O.
        return {"hook_event_name": "PostToolBatch", "session_id": "s1",
                "agent_id": "a1", "agent_type": "executor",
                "error": "rate_limit", "reason": "rate_limit",
                "cwd": self.tmp,
                "model": {"display_name": "Opus 5"},
                "context_window": {"used_percentage": 40},
                "rate_limits": {"seven_day": {"used_percentage": 90,
                                              "resets_at": int(time.time()) + 300000}}}

    def test_a_valid_payload_exits_zero(self):
        for hook in EXECUTABLES:
            result = run(hook, self._valid_payload(), config=self.config,
                        home=self.tmp)
            self.assertEqual(result.returncode, 0, (hook, result.stderr))

    def test_an_empty_dict_exits_zero(self):
        for hook in EXECUTABLES:
            result = run(hook, {}, config=self.config, home=self.tmp)
            self.assertEqual(result.returncode, 0, (hook, result.stderr))

    def test_malformed_json_exits_zero(self):
        for hook in EXECUTABLES:
            result = run(hook, raw="{not json", config=self.config,
                        home=self.tmp)
            self.assertEqual(result.returncode, 0, (hook, result.stderr))

    def test_empty_stdin_exits_zero(self):
        for hook in EXECUTABLES:
            result = run(hook, raw="", config=self.config, home=self.tmp)
            self.assertEqual(result.returncode, 0, (hook, result.stderr))

    def test_a_non_dict_payload_exits_zero(self):
        for hook in EXECUTABLES:
            result = run(hook, raw="[1, 2, 3]", config=self.config,
                        home=self.tmp)
            self.assertEqual(result.returncode, 0, (hook, result.stderr))

    def test_unwritable_configured_paths_exit_zero(self):
        # A regular file where every target expects a writable directory --
        # os.makedirs raises NotADirectoryError/FileExistsError underneath it,
        # and every write path in usage_lib and the postmortem executable
        # must degrade silently rather than let that escape.
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as handle:
            handle.write("x")
        bad_config = write_config(self.tmp, usage_watch={
            "cache_path": os.path.join(blocker, "sub", "budget.json"),
            "state_path": os.path.join(blocker, "sub", "state.json"),
        }, thread_handover={
            "handover_dir": os.path.join(blocker, "sub2"),
        })
        for hook in EXECUTABLES:
            result = run(hook, self._valid_payload(), config=bad_config,
                        home=self.tmp)
            self.assertEqual(result.returncode, 0, (hook, result.stderr))


class TestUsageLine(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = write_config(self.tmp)
        self.cache = os.path.join(self.tmp, "budget.json")

    def test_it_renders_dir_model_and_context(self):
        payload = {"cwd": "/tmp/demo",
                   "model": {"display_name": "Opus 5"},
                   "context_window": {"used_percentage": 34.2}}
        result = run(LINE, payload, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("demo", result.stdout)
        self.assertIn("Opus 5", result.stdout)
        self.assertIn("34%", result.stdout)

    def test_it_writes_a_cache_only_when_buckets_are_present(self):
        result = run(LINE, {"cwd": "/tmp/demo"}, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(self.cache),
                         "an empty rate_limits write must not clobber a "
                         "cache with nothing")

        payload = {"cwd": "/tmp/demo",
                   "rate_limits": {"seven_day": {"used_percentage": 88.1,
                                                 "resets_at":
                                                 int(time.time()) + 300000}}}
        result = run(LINE, payload, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(os.path.exists(self.cache))
        with open(self.cache) as handle:
            written = json.load(handle)
        self.assertAlmostEqual(
            written["buckets"]["seven_day"]["used_percentage"], 88.1)

    def test_the_line_never_contains_a_newline(self):
        payload = {"cwd": "/tmp/demo",
                   "model": {"display_name": "Opus 5"},
                   "context_window": {"used_percentage": 34.2},
                   "rate_limits": {"five_hour": {"used_percentage": 72.4,
                                                 "resets_at":
                                                 int(time.time()) + 3600}}}
        result = run(LINE, payload, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("\n", result.stdout)


class TestUsageLineNonAsciiStdin(unittest.TestCase):

    def _stdout_for(self, cwd):
        # Raw UTF-8 bytes in under a cp1252 stdio. Claude Code reads the
        # status line as UTF-8, so stdout is decoded as UTF-8 whatever the
        # child's own PYTHONIOENCODING is.
        env = dict(os.environ, PYTHONIOENCODING="cp1252",
                   HOME=tempfile.mkdtemp(),
                   CLAUDE_TOOLKIT_CONFIG="/nonexistent")
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        payload = json.dumps({"cwd": cwd,
                              "model": {"display_name": "Opus"}},
                             ensure_ascii=False).encode("utf-8")
        result = subprocess.run([sys.executable, LINE], input=payload,
                                capture_output=True, env=env, timeout=10)
        return result.stdout.decode("utf-8")

    def test_status_line_reaches_claude_code_as_utf8_under_cp1252(self):
        # U+02BB is not in cp1252: a cp1252 stdout cannot carry it at all.
        self.assertIn("Hawai\u02bbi", self._stdout_for("/tmp/Hawai\u02bbi"))
        out = self._stdout_for("/tmp/Jos\u00e9")
        self.assertIn("Jos\u00e9", out)
        self.assertNotIn("Jos\u00c3\u00a9", out)


class TestUsageWatch(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = write_config(self.tmp)
        self.cache = os.path.join(self.tmp, "budget.json")
        seed_cache(self.cache, {"seven_day": {"used_percentage": 90.0,
                                              "resets_at":
                                              int(time.time()) + 300000}})

    def test_it_emits_at_gate(self):
        result = run(WATCH, {"hook_event_name": "PostToolBatch",
                             "session_id": "sess-1"}, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        blob = json.loads(result.stdout)
        self.assertEqual(blob["hookSpecificOutput"]["hookEventName"],
                         "PostToolBatch")
        self.assertIn("GATE", blob["hookSpecificOutput"]["additionalContext"])

    def test_it_is_silent_when_agent_id_is_present(self):
        # Pin: PostToolBatch fires inside subagents too, carrying the
        # DISPATCHING session's session_id but the subagent's own agent_id.
        # That loop belongs to claude-usage-subagent, which has already
        # spoken -- this hook must emit nothing at all when agent_id shows up.
        result = run(WATCH, {"hook_event_name": "PostToolBatch",
                             "session_id": "sess-1", "agent_id": "worker-1"},
                    config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_it_is_deduped_on_a_repeat_posttoolbatch(self):
        first = run(WATCH, {"hook_event_name": "PostToolBatch",
                            "session_id": "sess-1"}, config=self.config)
        self.assertNotEqual(first.stdout, "")
        second = run(WATCH, {"hook_event_name": "PostToolBatch",
                             "session_id": "sess-1"}, config=self.config)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout, "")

    def test_it_is_not_deduped_on_a_repeat_userpromptsubmit(self):
        # Pin: UserPromptSubmit fires once per turn and is the boundary
        # the spec names as the natural place to raise a stop/continue
        # decision. Deduping it means that after HALT is announced once, the
        # human can type "ok, do the next task" and get silence for the rest of a
        # five-hour window.
        first = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                            "session_id": "sess-1"}, config=self.config)
        self.assertNotEqual(first.stdout, "")
        second = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotEqual(second.stdout, "",
                            "UserPromptSubmit must emit every time, even "
                            "after a prior identical announcement")


class TestUsageWatchUnknown(unittest.TestCase):
    """No reading is not no news.

    This is the shape of a real incident: the status line renders in
    the terminal only, so on the VS Code extension the cache simply stops
    advancing. It went stale, `read_cache` returned {}, the hook returned
    quietly, and a session ran into its 5-hour limit having been told nothing
    for two days. Silence and "plenty of headroom" are the same bytes.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = write_config(self.tmp)
        self.cache = os.path.join(self.tmp, "budget.json")

    def _stale(self):
        # Written 5 hours ago against a 900s ceiling, carrying a window that
        # is still notionally live -- so staleness, not expiry, is what bites.
        seed_cache(self.cache,
                   {"five_hour": {"used_percentage": 92.0,
                                  "resets_at": int(time.time()) + 300000}},
                   written_at=time.time() - 18000)

    def test_a_stale_cache_announces_unknown(self):
        self._stale()
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout, "", "a stale cache must not be silent")
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("UNKNOWN", context)
        self.assertIn("not as headroom", context.lower())

    def test_an_absent_cache_announces_unknown(self):
        # A machine where no status line has ever run -- every non-terminal
        # surface on a fresh install.
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("UNKNOWN", context)

    def test_the_stale_age_is_reported(self):
        self._stale()
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("5h old", context)

    def test_a_broken_cache_blames_the_file_not_a_missing_status_line(self):
        # The third cause reaching the hook. A truncated file has no age, so
        # the age-only split sent a terminal user whose status line was working
        # to "no status line has ever written the cache" -- and told them to
        # switch the tool off.
        with open(self.cache, "w") as handle:
            handle.write('{"written_at": 12345, "buckets"')
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("UNKNOWN", context)
        self.assertIn("could not be read", context)
        self.assertNotIn("ever written", context)
        self.assertNotIn("API key", context)

    def test_a_fresh_cache_with_unusable_buckets_does_not_claim_a_dead_feed(self):
        # Parses, carries a current timestamp, buckets the reader rejects. The
        # age is real and small, so an age-driven cause renders "no live
        # reading here (last cached reading is 0m old). A status line was
        # feeding it and has stopped" -- about a file written seconds ago.
        with open(self.cache, "w") as handle:
            json.dump({"written_at": time.time(),
                       "buckets": {"five_hour": "nope"}}, handle)
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("could not be read", context)
        self.assertNotIn("has stopped", context)
        self.assertNotIn("cached reading is", context)

    def test_it_is_deduped_on_both_events(self):
        # Unlike a tier crossing, unknown is a standing condition. Repeating it
        # every prompt is how a warning teaches its reader to skim past it.
        self._stale()
        for event in ("UserPromptSubmit", "PostToolBatch"):
            first = run(WATCH, {"hook_event_name": event,
                                "session_id": "sess-" + event},
                        config=self.config)
            self.assertNotEqual(first.stdout, "", event)
            second = run(WATCH, {"hook_event_name": event,
                                 "session_id": "sess-" + event},
                         config=self.config)
            self.assertEqual(second.stdout, "", event + " must dedupe unknown")

    def test_a_second_session_still_hears_it(self):
        self._stale()
        run(WATCH, {"hook_event_name": "UserPromptSubmit",
                    "session_id": "sess-1"}, config=self.config)
        other = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                            "session_id": "sess-2"}, config=self.config)
        self.assertIn("UNKNOWN", other.stdout)

    def test_a_subagent_is_still_silent(self):
        # The subagent pin, extended to the unknown path: a worker must not consume the
        # architect's slot, and must not be handed main-loop wording.
        self._stale()
        result = run(WATCH, {"hook_event_name": "PostToolBatch",
                             "session_id": "sess-1", "agent_id": "worker-1"},
                     config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_a_live_cache_still_reports_its_tier_not_unknown(self):
        # The regression that would make this whole change worthless: a real
        # reading must never be reported as unreadable.
        seed_cache(self.cache, {"seven_day": {"used_percentage": 90.0,
                                              "resets_at":
                                              int(time.time()) + 300000}})
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("GATE", context)
        self.assertNotIn("UNKNOWN", context)

    def test_a_rollover_is_not_reported_as_a_dead_status_line(self):
        # The ordinary five-hour rollover in a HEALTHY terminal session: the
        # cache is seconds old, but five_hour has just reset and the status
        # line is still replaying the last payload. Calling that "no live
        # reading here (last cached reading is 0m old)" is self-contradictory
        # and names the wrong cause -- and worse, it would spend the whole
        # announcement slot, so a status line dying a minute later would go
        # unreported.
        seed_cache(self.cache,
                   {"five_hour": {"used_percentage": 40.0,
                                  "resets_at": int(time.time()) - 10},
                    "seven_day": {"used_percentage": 8.0,
                                  "resets_at": int(time.time()) + 300000}})
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "",
                         "a mid-rollover reading with nothing above a tier "
                         "must stay quiet, not cry unknown")

    def test_a_rollover_does_not_spend_the_unknown_slot(self):
        # The consequence that makes the above blocking rather than cosmetic.
        seed_cache(self.cache,
                   {"five_hour": {"used_percentage": 40.0,
                                  "resets_at": int(time.time()) - 10},
                    "seven_day": {"used_percentage": 8.0,
                                  "resets_at": int(time.time()) + 300000}})
        run(WATCH, {"hook_event_name": "UserPromptSubmit",
                    "session_id": "sess-1"}, config=self.config)
        # The status line now genuinely dies: the cache goes stale.
        self._stale()
        after = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                            "session_id": "sess-1"}, config=self.config)
        self.assertIn("UNKNOWN", after.stdout,
                      "the rollover must not have consumed the announcement "
                      "slot a real outage needs")

    def test_a_visible_window_at_gate_still_reports_during_a_rollover(self):
        # The other half: discarding a partial reading wholesale would trade a
        # real alarm for a vaguer one. seven_day is visible and at gate.
        seed_cache(self.cache,
                   {"five_hour": {"used_percentage": 5.0,
                                  "resets_at": int(time.time()) - 10},
                    "seven_day": {"used_percentage": 90.0,
                                  "resets_at": int(time.time()) + 300000}})
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("GATE", context)
        self.assertNotIn("UNKNOWN", context)

    def test_a_never_written_cache_names_a_different_cause(self):
        # "A feed stopped" and "no feed was ever configured" send someone to
        # two unrelated places. The absent case must not blame the terminal.
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "sess-1"}, config=self.config)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("ever written", context)
        self.assertIn("API key", context)
        self.assertNotIn("has stopped", context)


class TestUsageLineRefusesStalePayloads(unittest.TestCase):
    """The status line must not stamp a current time onto replayed numbers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = write_config(self.tmp)
        self.cache = os.path.join(self.tmp, "budget.json")

    def test_a_replayed_payload_does_not_refresh_the_cache(self):
        # Shape of the incident: a cache carried a five_hour window that had
        # reset long before it was written, beside a calm seven_day.
        # Both numbers were stale; only one of them admitted it.
        good = {"rate_limits": {
            "five_hour": {"used_percentage": 30.0,
                          "resets_at": int(time.time()) + 600}}}
        run(LINE, good, config=self.config)
        with open(self.cache) as handle:
            before = json.load(handle)

        replayed = {"rate_limits": {
            "five_hour": {"used_percentage": 92.0,
                          "resets_at": int(time.time()) - 165600},
            "seven_day": {"used_percentage": 8.0,
                          "resets_at": int(time.time()) + 400000}}}
        result = run(LINE, replayed, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)

        with open(self.cache) as handle:
            after = json.load(handle)
        self.assertEqual(after["written_at"], before["written_at"],
                         "a replayed payload must not refresh written_at")
        self.assertNotIn("seven_day", after["buckets"])

    def test_a_live_payload_still_writes(self):
        payload = {"rate_limits": {
            "five_hour": {"used_percentage": 55.0,
                          "resets_at": int(time.time()) + 600},
            "seven_day": {"used_percentage": 20.0,
                          "resets_at": int(time.time()) + 400000}}}
        result = run(LINE, payload, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.cache) as handle:
            written = json.load(handle)
        self.assertEqual(set(written["buckets"]), {"five_hour", "seven_day"})

    def test_the_status_line_still_renders_a_refused_payload(self):
        # Rendering and caching are separate jobs. Refusing to cache rotten
        # numbers must not blank the human's status line.
        replayed = {"cwd": "/tmp/demo", "rate_limits": {
            "five_hour": {"used_percentage": 92.0,
                          "resets_at": int(time.time()) - 165600}}}
        result = run(LINE, replayed, config=self.config)
        self.assertIn("demo", result.stdout)


class TestPartialReadingReachesEveryCaller(unittest.TestCase):
    """`read_cache` discarding a partial reading was right for the main hook
    and a silent regression for the other two.

    Both used to receive `live_buckets(...)` — whatever was still visible —
    and were carried along by a docstring change alone when `read_cache`
    narrowed. The subagent case is the one that matters: a real GATE warning
    to a dispatched worker became silence, which is the failure this whole
    tool exists to deny, resolved the wrong way inside the PR that denied it.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = write_config(self.tmp)
        self.cache = os.path.join(self.tmp, "budget.json")
        # A five_hour window that rolled since the write, beside a seven_day
        # still plainly visible at 90% -- gate territory.
        seed_cache(self.cache,
                   {"five_hour": {"used_percentage": 5.0,
                                  "resets_at": int(time.time()) - 10},
                    "seven_day": {"used_percentage": 90.0,
                                  "resets_at": int(time.time()) + 300000}})

    def test_the_subagent_is_still_warned_during_a_rollover(self):
        result = run(SUBAGENT, {"hook_event_name": "SubagentStart",
                                "session_id": "sess-1", "agent_id": "worker-1",
                                "agent_type": "executor"}, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(result.stdout, "",
                            "a visible 90% must still reach a dispatched "
                            "worker while another window is mid-rollover")
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("90%", context)
        self.assertIn("BLOCKED", context)

    def test_the_postmortem_records_a_visible_window(self):
        result = run(POSTMORTEM, {"hook_event_name": "StopFailure",
                                  "session_id": "sess-1", "error": "rate_limit",
                                  "cwd": self.tmp},
                     config=self.config, home=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        written = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "handovers")):
            for name in files:
                with open(os.path.join(root, name)) as handle:
                    written.append(handle.read())
        self.assertTrue(written, "the postmortem wrote no document")
        self.assertIn("90%", written[0])
        self.assertNotIn("not recorded", written[0])
        # ...and says the reading was incomplete. Without this the document
        # reads "Budget at death: 7-day at 90%" with no hint that the window
        # which actually rolled -- the likely killer -- is missing from it.
        self.assertIn("PARTIAL", written[0])

    def test_a_complete_reading_carries_no_partial_label(self):
        # The label has to discriminate, not decorate: every window live means
        # the figure really is the binding constraint.
        seed_cache(self.cache,
                   {"five_hour": {"used_percentage": 96.0,
                                  "resets_at": int(time.time()) + 600},
                    "seven_day": {"used_percentage": 90.0,
                                  "resets_at": int(time.time()) + 300000}})
        result = run(POSTMORTEM, {"hook_event_name": "StopFailure",
                                  "session_id": "sess-2", "error": "rate_limit",
                                  "cwd": self.tmp},
                     config=self.config, home=self.tmp)
        self.assertEqual(result.returncode, 0, result.stderr)
        written = []
        for root, _dirs, files in os.walk(os.path.join(self.tmp, "handovers")):
            for name in files:
                with open(os.path.join(root, name)) as handle:
                    written.append(handle.read())
        self.assertTrue(written, "the postmortem wrote no document")
        self.assertIn("96%", written[0])
        self.assertNotIn("PARTIAL", written[0])


class TestUsageSubagent(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config = write_config(self.tmp)
        self.cache = os.path.join(self.tmp, "budget.json")
        seed_cache(self.cache, {"seven_day": {"used_percentage": 90.0,
                                              "resets_at":
                                              int(time.time()) + 300000}})

    def test_two_distinct_agent_ids_are_each_told(self):
        first = run(SUBAGENT, {"hook_event_name": "SubagentStart",
                               "agent_id": "a1", "agent_type": "executor"},
                    config=self.config)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("BLOCKED", first.stdout)

        second = run(SUBAGENT, {"hook_event_name": "SubagentStart",
                                "agent_id": "a2", "agent_type": "executor"},
                    config=self.config)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("BLOCKED", second.stdout,
                     "dedupe is per-subagent, not per-build -- a second "
                     "dispatched agent must still be told its headroom")

    def test_it_emits_when_agent_id_is_absent(self):
        result = run(SUBAGENT, {"hook_event_name": "SubagentStart"},
                    config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BLOCKED", result.stdout)

    def test_it_is_silent_under_subagent_context_false(self):
        disabled = write_config(self.tmp,
                                usage_watch={"subagent_context": False})
        result = run(SUBAGENT, {"hook_event_name": "SubagentStart",
                                "agent_id": "a3"}, config=disabled)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")


class PostmortemCase(unittest.TestCase):
    """Every subclass runs `claude-usage-postmortem` with HOME redirected, and
    asserts the real ~/.claude/thread-handover/index.json never moved."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_home = tempfile.mkdtemp()
        self.config = write_config(self.tmp)
        self.handovers = os.path.join(self.tmp, "handovers")
        self._real_index_before = snapshot_real_index()

    def tearDown(self):
        self.assertEqual(snapshot_real_index(), self._real_index_before,
                         "the REAL ~/.claude/thread-handover/index.json was "
                         "touched by a hermetic test")

    def fire(self, payload):
        base = {"hook_event_name": "StopFailure", "session_id": "sess-pm",
               "cwd": self.tmp}
        base.update(payload)
        return run(POSTMORTEM, base, config=self.config, home=self.fake_home)

    def handover_files(self):
        if not os.path.isdir(self.handovers):
            return []
        return os.listdir(self.handovers)


class TestPostmortemTriggers(PostmortemCase):

    def test_error_rate_limit_writes_and_registers(self):
        result = self.fire({"session_id": "sess-rl", "error": "rate_limit"})
        self.assertEqual(result.returncode, 0, result.stderr)
        files = self.handover_files()
        self.assertEqual(len(files), 1, files)
        self.assertRegex(files[0], index_lib.HANDOVER_NAME_RE)

        records = read_fake_index(self.fake_home)
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["created_session_id"], "sess-rl")
        self.assertTrue(records[0]["path"].endswith(files[0]))

    def test_error_billing_error_writes_and_registers(self):
        result = self.fire({"session_id": "sess-be", "error": "billing_error"})
        self.assertEqual(result.returncode, 0, result.stderr)
        files = self.handover_files()
        self.assertEqual(len(files), 1, files)
        records = read_fake_index(self.fake_home)
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["created_session_id"], "sess-be")

    def test_firing_twice_in_one_session_registers_only_one_record(self):
        # The filename is deterministic (date + reason + session), so a session
        # that dies on budget, resumes when the window resets, and dies again
        # overwrites the one document. Registering twice would leave a second,
        # permanently-pending index record: attaching the file consumes exactly
        # one, and the leftover is re-announced at every future SessionStart and
        # never swept, because sweep only considers consumed records.
        for _ in range(3):
            result = self.fire({"session_id": "sess-twice", "error": "rate_limit"})
            self.assertEqual(result.returncode, 0, result.stderr)

        self.assertEqual(len(self.handover_files()), 1)
        records = read_fake_index(self.fake_home)
        self.assertEqual(len(records), 1, records)

    def test_the_filename_matches_the_handover_index_regex(self):
        self.fire({"session_id": "sess-name/../weird", "error": "rate_limit"})
        files = self.handover_files()
        self.assertEqual(len(files), 1, files)
        self.assertRegex(files[0], index_lib.HANDOVER_NAME_RE)


class TestPostmortemNonTriggers(PostmortemCase):

    def test_every_other_stopfailure_reason_writes_nothing(self):
        for reason in OTHER_EIGHT_REASONS:
            result = self.fire({"error": reason})
            self.assertEqual(result.returncode, 0, (reason, result.stderr))
        self.assertEqual(self.handover_files(), [])
        self.assertEqual(read_fake_index(self.fake_home), [])

    def test_a_payload_using_only_the_old_reason_key_with_a_non_budget_value(
            self):
        # An ignored value must stay ignored no matter which key carries it.
        for reason in OTHER_EIGHT_REASONS:
            result = self.fire({"reason": reason})
            self.assertEqual(result.returncode, 0, (reason, result.stderr))
        self.assertEqual(self.handover_files(), [])

    def test_a_payload_carrying_only_the_legacy_reason_key_still_fires(self):
        # Verified against the ACTUAL executable rather than against prose
        # about it, as anything asserting a payload shape should be. `error`
        # is the real StopFailure key; `reason` is read only as
        # "belt-and-braces" per the module docstring, so a payload carrying
        # only the legacy `reason` key still triggers a write.
        result = self.fire({"reason": "rate_limit"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.handover_files()), 1, self.handover_files())


ANCHOR = os.path.join(TOOL_DIR, "claude-usage-anchor")


def write_transcript_for(root, epoch, output_tokens):
    """One JSONL record shaped like a real transcript entry.

    A local copy rather than an import from test_estimate_lib: these two files
    are run directly as scripts, so a cross-import depends on which directory
    the runner happened to start in. Ten duplicated lines beat a suite that
    passes only when invoked one way.
    """
    import datetime
    folder = os.path.join(root, "projA")
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    path = os.path.join(folder, "s1.jsonl")
    with open(path, "w") as handle:
        handle.write(json.dumps({
            "timestamp": stamp,
            "message": {"usage": {"output_tokens": output_tokens,
                                  "input_tokens": 3,
                                  "cache_read_input_tokens": 900000}},
        }) + "\n")
    os.utime(path, (epoch, epoch))
    return path


def seed_anchor_store(path, pct, at, resets_at, ratio=None):
    """A store with one live anchor and, optionally, a measured ratio."""
    blob = {"anchor": {"pct": float(pct), "at": at, "resets_at": resets_at}}
    if ratio is not None:
        blob["ratio"] = {"value": ratio, "source": "measured",
                         "derived_at": at}
    with open(path, "w") as handle:
        json.dump(blob, handle)


def read_anchor(path):
    try:
        with open(path) as handle:
            return json.load(handle).get("anchor")
    except Exception:
        return None


class TestUsageAnchorCLI(unittest.TestCase):
    """A human-run CLI, not a hook: it may exit nonzero and print plain text."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # `estimate` ships OFF, so every test of what this CLI DOES opts in.
        self.config = write_config(self.tmp, usage_watch={"estimate": True})
        self.store = os.path.join(self.tmp, "anchors.json")
        # `current_estimate` returns None when the transcript root is absent.
        os.makedirs(os.path.join(self.tmp, "projects"))

    def _run(self, args, config=None):
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = config or self.config
        env["HOME"] = self.tmp
        return subprocess.run([sys.executable, ANCHOR] + args,
                              capture_output=True, text=True, env=env)

    def test_it_is_executable_and_has_a_shebang(self):
        self.assertTrue(os.access(ANCHOR, os.X_OK), "not chmod +x")
        with open(ANCHOR) as handle:
            self.assertTrue(handle.readline().startswith("#!"))

    def test_the_cli_always_passes_a_zero_reading_age(self):
        """A human just read the panel. PARSED, not grepped: the literal also
        appears in comments."""
        with open(ANCHOR) as handle:
            tree = ast.parse(handle.read())
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "write_anchor"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0].args), 4)
        age = calls[0].args[3]
        self.assertIsInstance(age, ast.Constant)
        self.assertEqual(age.value, 0.0)

    def test_a_bare_percentage_is_refused_for_want_of_a_reset_time(self):
        result = self._run(["45"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("--resets-in is required", result.stdout)
        self.assertIsNone(read_anchor(self.store))

    def test_a_percentage_with_a_reset_time_anchors_the_window(self):
        before = time.time()
        result = self._run(["45", "--resets-in", "4h20m"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Anchored at 45%.", result.stdout)
        # No ratio ships in this config, so the sentence says so.
        self.assertIn("No percentage yet", result.stdout)
        anchor = read_anchor(self.store)
        self.assertEqual(anchor["pct"], 45.0)
        self.assertAlmostEqual(anchor["resets_at"], before + 15600, delta=5)
        self.assertAlmostEqual(anchor["resets_at"], time.time() + 15600,
                               delta=5)

    def test_a_reset_time_outside_one_minute_to_five_hours_is_refused(self):
        for bad in ("30s", "5h1m", "0m"):
            result = self._run(["45", "--resets-in", bad])
            self.assertEqual(result.returncode, 1, bad)
            self.assertIn("--resets-in takes a duration", result.stdout, bad)
            self.assertIsNone(read_anchor(self.store), bad)

    def test_one_minute_and_five_hours_are_accepted(self):
        for good in ("1m", "5h"):
            self.assertEqual(
                self._run(["45", "--resets-in", good]).returncode, 0, good)

    def test_a_ratio_config_turns_the_anchor_into_a_percentage(self):
        config = write_config(self.tmp, usage_watch={
            "estimate": True, "default_ratio": FIXTURE_RATIO})
        result = self._run(["45", "--resets-in", "2h"], config=config)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("Estimated 45% of the 5h window", result.stdout)

    def test_an_unwritable_store_is_reported_as_such(self):
        blocker = os.path.join(self.tmp, "blocker")
        open(blocker, "w").close()
        config = write_config(self.tmp, usage_watch={
            "anchor_path": os.path.join(blocker, "anchors.json"),
            "estimate": True})
        result = self._run(["45", "--resets-in", "2h"], config=config)
        self.assertEqual(result.returncode, 1)
        self.assertIn("Could not anchor: could not write", result.stdout)

    def test_the_anchor_lands_in_the_temp_store_not_the_real_one(self):
        self._run(["45", "--resets-in", "2h"])
        self.assertTrue(os.path.exists(self.store))

    def test_a_nonsense_argument_is_refused_with_a_message(self):
        result = self._run(["banana"])
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stdout.strip() or result.stderr.strip())

    def test_a_percentage_out_of_range_is_refused(self):
        self.assertEqual(
            self._run(["145", "--resets-in", "2h"]).returncode, 1)

    def test_a_misspelt_flag_is_refused(self):
        self.assertEqual(self._run(["45", "--reset-in", "2h"]).returncode, 1)

    def test_show_with_no_anchor_says_how_to_anchor(self):
        result = self._run(["--show"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("No live anchor", result.stdout)
        self.assertIn("--resets-in", result.stdout)

    def test_show_prints_the_estimate_for_a_live_anchor(self):
        now = time.time()
        seed_anchor_store(self.store, 40, now - 600, now + 3600,
                          ratio=FIXTURE_RATIO)
        result = self._run(["--show"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Estimated 40% of the 5h window", result.stdout)


class TestWatchEstimate(unittest.TestCase):
    """`claude-usage-watch` with no cache: the estimate tier and the anchor
    write, through the real executable with a hermetic HOME and config."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = os.path.join(self.tmp, "projects")
        self.store = os.path.join(self.tmp, "anchors.json")
        self.cache = os.path.join(self.tmp, "budget.json")
        self.config = write_config(self.tmp, usage_watch={"estimate": True})
        self.now = time.time()

    def _run(self, session="s1", event="UserPromptSubmit", env=None):
        result = run(WATCH, {"hook_event_name": event, "session_id": session},
                     config=self.config, home=self.tmp, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return ""
        return json.loads(result.stdout)["hookSpecificOutput"][
            "additionalContext"]

    def _seed(self, anchor_pct, drift_pct, resets_in=3600):
        seed_anchor_store(self.store, anchor_pct, self.now - 1800,
                          self.now + resets_in, ratio=FIXTURE_RATIO)
        write_transcript_for(self.projects, self.now - 60,
                             int(drift_pct * FIXTURE_RATIO))

    def test_a_halt_from_an_estimate_fires_on_any_surface(self):
        # No CLAUDE_CODE_ENTRYPOINT: `run` strips it, so the surface is
        # unrecognised. The tier must not depend on the surface.
        self._seed(90, 6)
        context = self._run()
        self.assertIn("HALT FROM AN ESTIMATE", context)
        self.assertIn("Start no new plan task", context)
        self.assertIn("Estimated 96%", context)

    def test_the_tier_also_fires_on_a_terminal_surface(self):
        self._seed(90, 6)
        context = self._run(env={"CLAUDE_CODE_ENTRYPOINT": "claude-cli"})
        self.assertIn("HALT FROM AN ESTIMATE", context)

    def test_an_expired_anchor_is_void(self):
        """Positive control: the same tokens, a reset time in the past."""
        self._seed(90, 6, resets_in=-60)
        context = self._run()
        self.assertNotIn("FROM AN ESTIMATE", context)
        self.assertIn("UNKNOWN", context)
        self.assertIn("No live anchor", context)

    def test_a_quiet_estimate_adds_a_clause_but_no_tier(self):
        self._seed(20, 1)
        context = self._run()
        self.assertIn("UNKNOWN", context)
        self.assertIn("Estimated 21%", context)
        self.assertNotIn("FROM AN ESTIMATE", context)

    def test_post_tool_batch_says_the_tier_once(self):
        self._seed(90, 6)
        self.assertIn("HALT FROM AN ESTIMATE",
                      self._run(event="PostToolBatch"))
        self.assertEqual(self._run(event="PostToolBatch"), "")

    def test_a_healthy_reading_carries_no_estimate(self):
        self._seed(90, 6)
        seed_cache(self.cache, {"five_hour": {
            "used_percentage": 91.0, "resets_at": int(self.now) + 3000}})
        context = self._run()
        self.assertIn("USAGE BUDGET — GATE", context)
        self.assertNotIn("estimate", context.lower())

    def _cache(self, age, five=50, seven=None, resets_in=10800):
        buckets = {"five_hour": {"used_percentage": five,
                                 "resets_at": int(self.now) + resets_in}}
        if seven is not None:
            buckets["seven_day"] = {"used_percentage": seven,
                                    "resets_at": int(self.now) + resets_in}
        seed_cache(self.cache, buckets, written_at=time.time() - age)

    def test_a_fresh_reading_anchors_with_its_own_reset_time(self):
        self._cache(0)
        self._run()
        anchor = read_anchor(self.store)
        self.assertIsNotNone(anchor, "a fresh reading did not anchor")
        self.assertEqual(anchor["pct"], 50)
        self.assertEqual(anchor["resets_at"], int(self.now) + 10800)

    def test_the_five_hour_reading_anchors_even_when_seven_day_binds(self):
        self._cache(0, five=30, seven=90)
        self._run()
        self.assertEqual(read_anchor(self.store)["pct"], 30)

    def test_a_reading_300s_old_is_too_old_to_anchor(self):
        """Inside stale_after_seconds, so the tier path trusts it, but past
        ANCHOR_MAX_READING_AGE (120s) for pairing with a token count."""
        self._cache(300)
        self._run()
        self.assertIsNone(read_anchor(self.store))

    def test_a_future_stamped_cache_is_not_anchored(self):
        self._cache(-3600)
        self._run()
        self.assertIsNone(read_anchor(self.store))

    def test_the_estimator_off_writes_no_anchor(self):
        self.config = write_config(self.tmp)
        self._cache(0)
        self._run()
        self.assertIsNone(read_anchor(self.store))


class TestSubagentEstimate(unittest.TestCase):
    """`claude-usage-subagent` with no cache falls back to the estimate, so a
    worker on a surface with no status line is not told nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.projects = os.path.join(self.tmp, "projects")
        self.store = os.path.join(self.tmp, "anchors.json")
        self.now = time.time()
        self.config = write_config(self.tmp, usage_watch={"estimate": True})
        self._seed(60, 12)

    def _seed(self, anchor_pct, drift_pct):
        seed_anchor_store(self.store, anchor_pct, self.now - 1800,
                          self.now + 3600, ratio=FIXTURE_RATIO)
        write_transcript_for(self.projects, self.now - 60,
                             int(drift_pct * FIXTURE_RATIO))

    def _run(self, agent_id="a1"):
        return run(SUBAGENT, {"hook_event_name": "SubagentStart",
                              "agent_id": agent_id, "agent_type": "executor"},
                   config=self.config, home=self.tmp)

    def test_it_speaks_with_no_cache_at_all(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"][
            "additionalContext"]
        self.assertIn("72%", context)
        self.assertIn("ESTIMATED", context)
        self.assertIn("BLOCKED", context)

    def test_a_real_reading_still_wins(self):
        seed_cache(os.path.join(self.tmp, "budget.json"),
                   {"five_hour": {"used_percentage": 91,
                                  "resets_at": int(self.now + 3600)}})
        context = json.loads(self._run().stdout)["hookSpecificOutput"][
            "additionalContext"]
        self.assertIn("91", context)
        self.assertNotIn("ESTIMATED", context)

    def test_the_estimate_switch_gates_the_fallback(self):
        self.config = write_config(self.tmp)
        self.assertEqual(self._run().stdout.strip(), "")

    def test_a_quiet_estimate_says_nothing(self):
        self._seed(5, 1)
        self.assertEqual(self._run().stdout.strip(), "")

    def test_an_anchor_with_no_ratio_says_nothing(self):
        seed_anchor_store(self.store, 80, self.now - 1800, self.now + 3600)
        self.assertEqual(self._run().stdout.strip(), "")

    def test_each_worker_is_told_once_but_a_second_worker_is_still_told(self):
        self.assertTrue(self._run(agent_id="first").stdout.strip())
        self.assertEqual(self._run(agent_id="first").stdout.strip(), "")
        self.assertTrue(self._run(agent_id="second").stdout.strip())


class TestSurfaceAwareNotice(unittest.TestCase):
    """The relay split, THROUGH THE HOOK: lib tests cannot reach the wiring."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.transcripts = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.transcripts, "projA"))
        self.anchors = os.path.join(self.tmp, "anchors.json")
        self.config = write_config(self.tmp, usage_watch={
            "transcript_root": self.transcripts,
            "anchor_path": self.anchors,
            "state_path": os.path.join(self.tmp, "state.json"),
            "estimate": True,
            "default_ratio": FIXTURE_RATIO,
        })

    def _context(self, entrypoint="claude-vscode", session=None,
                 event="UserPromptSubmit"):
        env = {} if entrypoint is None else {
            "CLAUDE_CODE_ENTRYPOINT": entrypoint}
        result = run(WATCH, {"hook_event_name": event,
                             "session_id": session or ("s-" + str(id(self)))},
                     config=self.config, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        if not result.stdout.strip():
            return ""
        return json.loads(result.stdout)[
            "hookSpecificOutput"]["additionalContext"]

    # --- the relay split, end to end ---------------------------------------

    def test_vs_code_keeps_the_truth_and_drops_the_relay(self):
        context = self._context("claude-vscode")
        self.assertIn("NOT as headroom", context)
        self.assertNotIn("Say so to", context)

    def test_a_terminal_keeps_both(self):
        context = self._context("claude-cli")
        self.assertIn("NOT as headroom", context)
        self.assertIn("Say so to", context)

    def test_an_unknown_surface_keeps_both(self):
        context = self._context("claude-brand-new")
        self.assertIn("NOT as headroom", context)
        self.assertIn("Say so to", context)

    def test_positive_control_the_notice_is_never_empty(self):
        """"Does not contain 'Say so to'" is satisfied by the empty string,
        and an empty notice is the exact failure — silence reading as
        headroom — that this whole module was rewritten to prevent."""
        for entrypoint in ("claude-vscode", "claude-cli", "claude-brand-new",
                           None):
            context = self._context(entrypoint, session="pc-%s" % entrypoint)
            self.assertIn("USAGE BUDGET", context, repr(entrypoint))
            self.assertGreater(len(context.strip()), 200, repr(entrypoint))


class TestEstimateOffByDefault(unittest.TestCase):
    """The shipped contract, pinned. Two whole-branch reviews found the token
    count wrong in two compounding ways, so the number is withheld until the
    counting is fixed AND DEFAULT_RATIO re-derived in the same change. Every
    other test in this file opts IN; this class is the only one that sees what
    a real machine sees, and without it a future edit could switch the feature
    on for everyone silently."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.transcripts = os.path.join(self.tmp, "projects")
        os.makedirs(os.path.join(self.transcripts, "projA"))
        # No `estimate` key at all — exactly what a fresh clone has.
        self.config = write_config(self.tmp, usage_watch={
            "transcript_root": self.transcripts,
            "anchor_path": os.path.join(self.tmp, "anchors.json"),
        })
        self.cache = os.path.join(self.tmp, "budget.json")
        path = os.path.join(self.transcripts, "projA", "s1.jsonl")
        recent = time.time() - 600
        import datetime
        stamp = datetime.datetime.fromtimestamp(
            recent, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        with open(path, "w") as handle:
            handle.write(json.dumps({
                "timestamp": stamp,
                "message": {"usage": {"output_tokens": 150000}},
            }) + "\n")
        os.utime(path, (recent, recent))

    def test_the_unknown_notice_carries_no_estimate_and_no_anchor_prompt(self):
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "off-1"}, config=self.config)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("UNKNOWN", context)          # the honest half still fires
        self.assertNotIn("claude-usage-anchor", context)
        self.assertNotIn("estimated", context)

    def test_a_no_status_line_surface_names_no_anchor_command_either(self):
        """The structural tail must not name `claude-usage-anchor` while the
        estimator is off — the command REFUSES in that state, printing "the
        budget estimator is off, so there is nothing to anchor" and exiting 1.

        Off is the shipped default, so this is what a fresh install gets. The test
        above covers the relay path, where the hint never appears anyway;
        only a positively-identified no-status-line surface reaches the tail
        that carries it. `config.example.json` settled the same question for
        the dispatch advisory: an advisory naming a command that cannot run is
        worse than one that stays quiet.
        """
        result = run(WATCH, {"hook_event_name": "UserPromptSubmit",
                             "session_id": "off-vscode"}, config=self.config,
                     env={"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"})
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)[
            "hookSpecificOutput"]["additionalContext"]
        # Positive control: prove we are on the structural tail at all, or
        # "does not name the command" passes on the relay path for free.
        self.assertIn("structural rather than a fault", context)
        self.assertNotIn("claude-usage-anchor", context)

    def test_a_healthy_reading_writes_no_anchor(self):
        # The flag gates the write, and that is the whole contract now. The
        # first reason was that an anchor would persist an inflated token
        # count; the counting is fixed, and the gate is still absolute
        # because `off` has to mean off however the reason changes.
        seed_cache(self.cache, {"five_hour": {"used_percentage": 30.0,
                                              "resets_at": int(time.time()) + 3000}})
        run(WATCH, {"hook_event_name": "UserPromptSubmit",
                    "session_id": "off-2"}, config=self.config)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "anchors.json")))

    def test_the_cli_refuses_and_says_how_to_enable(self):
        """Refuses, names the flag, and does NOT blame the fixed defect.

        This once asserted `over-count`, until de-duplication landed and that
        sentence became false — a message that sends the reader to fix
        an already-fixed bug is worse than a bare refusal. The negative
        assertion is the load-bearing half: it is what makes this test fail if
        the stale explanation is ever pasted back in.
        """
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = self.config
        result = subprocess.run([sys.executable, ANCHOR, "45"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("estimator is off", result.stdout)
        self.assertIn("usage_watch.estimate", result.stdout)
        self.assertNotIn("over-count", result.stdout)
        self.assertNotIn("not yet fixed", result.stdout)
        self.assertNotIn("horizon", result.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "anchors.json")))

    def test_show_also_refuses_rather_than_printing_a_number(self):
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = self.config
        result = subprocess.run([sys.executable, ANCHOR, "--show"],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 1)
        # No percentage anywhere in the refusal. The old form split on the word
        # "over-count" and only checked the text before it, so a number leaking
        # after that word would have passed -- and the word is now gone, which
        # would have made the assertion vacuous rather than merely weak.
        self.assertNotIn("%", result.stdout)


class TestAScanThatDiesDoesNotSpendTheSlot(unittest.TestCase):
    """The UNKNOWN notice is deduped once per session, and the clause
    appended to it scans transcripts inside a hook declared `"timeout": 5`.

    If the slot is recorded BEFORE the notice is printed — which it was — then
    a scan that overruns the harness timeout costs the notice *and* buys an
    hour of silence in exchange for nothing. Silence reading as headroom is
    the exact failure this module was rewritten for, so the ordering is a
    contract, not a detail.

    Simulated with a stub `estimate_lib` that hard-exits where the scan runs.
    The hook is COPIED rather than symlinked: it resolves its sibling
    directory through `os.path.realpath(__file__)`, so a symlink would import
    the real module and the stub would never load.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sandbox = os.path.join(self.tmp, "bin")
        os.makedirs(self.sandbox)
        source = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        shutil.copy(os.path.join(source, "claude-usage-watch"),
                    os.path.join(self.sandbox, "claude-usage-watch"))
        # usage_lib is SYMLINKED, not copied: it locates lib/toolkit_config
        # three directories above its own realpath, so a copy lands on
        # <sandbox>/../../lib, finds nothing, and silently falls back to the
        # REAL ~/.claude paths — a non-hermetic test that writes to the live
        # state file. The symlink resolves back to the repo and config loads.
        os.symlink(os.path.join(source, "usage_lib.py"),
                   os.path.join(self.sandbox, "usage_lib.py"))
        with open(os.path.join(self.sandbox, "estimate_lib.py"), "w") as handle:
            handle.write(
                "import os\n"
                "def is_estimate_enabled():\n"
                "    return True\n"
                # os._exit, not an exception: the hook catches exceptions by
                # design, and what is being modelled is the harness killing
                # the process outright.
                "def current_estimate(now, root=None, path=None):\n"
                "    os._exit(9)\n"
                "def write_anchor(*a, **k):\n"
                "    os._exit(9)\n")
        self.hook = os.path.join(self.sandbox, "claude-usage-watch")
        self.state = os.path.join(self.tmp, "state.json")
        self.config = write_config(self.tmp, usage_watch={
            "cache_path": os.path.join(self.tmp, "absent-budget.json"),
            "state_path": self.state,
            "estimate": True,
        })

    def _run(self):
        return run(self.hook, {"hook_event_name": "UserPromptSubmit",
                               "session_id": "sess-dead"}, config=self.config)

    def _restore_the_real_estimate_lib(self):
        source = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        shutil.copy(os.path.join(source, "estimate_lib.py"),
                    os.path.join(self.sandbox, "estimate_lib.py"))

    def test_the_unknown_slot_survives_a_scan_that_never_returns(self):
        first = self._run()
        # The stub kills the process where the scan would be, so nothing is
        # printed -- that half is the premise, not the finding.
        self.assertEqual(first.returncode, 9)
        self.assertEqual(first.stdout.strip(), "")

        # The finding, asserted through BEHAVIOUR rather than by reaching into
        # the state file: the next prompt must still be able to announce.
        # Before the fix, record_unknown had already run and this session was
        # silent for the rest of the slot having said nothing at all.
        #
        # An earlier version of this test read the state JSON and asserted on
        # a key name that does not exist in it, so it passed against the
        # unfixed hook too. Mutation testing is the only reason that was
        # caught.
        self._restore_the_real_estimate_lib()
        second = self._run()
        self.assertEqual(second.returncode, 0)
        self.assertIn("UNKNOWN", second.stdout,
                      "the unknown slot was spent by a run that printed nothing")

    def test_a_run_that_does_print_still_spends_the_slot(self):
        # The other half of the contract, so the fix cannot be "never record".
        # Real estimate_lib, so the notice completes normally.
        self._restore_the_real_estimate_lib()
        first = self._run()
        self.assertEqual(first.returncode, 0)
        self.assertIn("UNKNOWN", first.stdout)
        second = self._run()
        self.assertEqual(second.stdout.strip(), "",
                         "the notice repeated inside its dedupe window")


class TestAnEmptyNoticeDoesNotSpendTheSlot(unittest.TestCase):
    """`unknown_message` returns "" only from its two exception handlers — never
    as deliberate suppression. So an internal failure there must not buy silence:
    it printed nothing, and spending the slot anyway means the session is never
    told its budget is unreadable.

    `record_unknown` once sat OUTSIDE the `if text:` branch, so this
    path did exactly that. It survived two reviews because the sibling case the
    ordering was written for — a scan killed by the harness timeout — IS handled
    correctly. The ordering was right and the scope was wrong, which is not a
    distinction a test asserting "the timeout case works" can make.

    usage_lib is SHIMMED rather than copied or stubbed: the real module locates
    lib/toolkit_config three directories above its own realpath, so a plain copy
    silently falls back to the live ~/.claude state file. This loads the real
    module by path (keeping its realpath, and therefore its config) and replaces
    one function.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.sandbox = os.path.join(self.tmp, "bin")
        os.makedirs(self.sandbox)
        source = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        shutil.copy(os.path.join(source, "claude-usage-watch"),
                    os.path.join(self.sandbox, "claude-usage-watch"))
        shutil.copy(os.path.join(source, "estimate_lib.py"),
                    os.path.join(self.sandbox, "estimate_lib.py"))
        real = os.path.join(source, "usage_lib.py")
        with open(os.path.join(self.sandbox, "usage_lib.py"), "w") as handle:
            handle.write(
                "import importlib.util\n"
                "_spec = importlib.util.spec_from_file_location(\n"
                "    '_real_usage_lib', %r)\n"
                "_m = importlib.util.module_from_spec(_spec)\n"
                "_spec.loader.exec_module(_m)\n"
                "for _k in dir(_m):\n"
                "    if not _k.startswith('__'):\n"
                "        globals()[_k] = getattr(_m, _k)\n"
                "# The one override: model unknown_message hitting its own\n"
                "# except-branch and returning the empty string.\n"
                "def unknown_message(*a, **k):\n"
                "    return ''\n" % real)
        self.hook = os.path.join(self.sandbox, "claude-usage-watch")
        self.state = os.path.join(self.tmp, "state.json")
        self.config = write_config(self.tmp, usage_watch={
            "cache_path": os.path.join(self.tmp, "absent-budget.json"),
            "state_path": self.state,
        })

    def _run(self):
        return run(self.hook, {"hook_event_name": "UserPromptSubmit",
                               "session_id": "sess-1"}, config=self.config)

    def _restore_the_real_usage_lib(self):
        source = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
        target = os.path.join(self.sandbox, "usage_lib.py")
        os.unlink(target)
        os.symlink(os.path.join(source, "usage_lib.py"), target)

    def test_an_empty_notice_prints_nothing(self):
        # Guards the fixture's own premise. If the shim stopped working this
        # would print a real notice and the test below would pass vacuously.
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("UNKNOWN", result.stdout)

    def test_the_slot_survives_a_notice_that_came_back_empty(self):
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("UNKNOWN", first.stdout)

        # With the real module back, the next prompt must still be able to warn.
        # If the empty run spent the slot, this is silent for ~12 hours.
        self._restore_the_real_usage_lib()
        second = self._run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("UNKNOWN", second.stdout,
                      "an empty notice spent the slot, so the session was "
                      "never told its budget is unreadable")


class TestAnEmptyTierDoesNotSpendTheSlot(TestAnEmptyNoticeDoesNotSpendTheSlot):
    """The tier path's version of the class above. `tier_message` returns ""
    for a real crossing only from its own except-branch (TIER_NONE returns
    before it is called), so an empty message printed nothing and must not
    spend the PostToolBatch dedupe slot -- that slot is keyed on the window's
    `resets_at`, so spending it silently means no GATE or HALT for the rest
    of the window. `announce_tier` once recorded BEFORE printing.
    """

    def setUp(self):
        super().setUp()
        with open(os.path.join(self.sandbox, "usage_lib.py")) as handle:
            shim = handle.read()
        shim = shim.replace(
            "def unknown_message(*a, **k):\n",
            "def tier_message(*a, **k):\n")
        self.assertIn("def tier_message(*a, **k):", shim)
        with open(os.path.join(self.sandbox, "usage_lib.py"), "w") as handle:
            handle.write(shim)
        cache = os.path.join(self.tmp, "budget.json")
        seed_cache(cache, {"seven_day": {"used_percentage": 90.0,
                                         "resets_at":
                                         int(time.time()) + 300000}})
        self.config = write_config(self.tmp, usage_watch={
            "cache_path": cache, "state_path": self.state})

    def _run(self):
        return run(self.hook, {"hook_event_name": "PostToolBatch",
                               "session_id": "sess-1"}, config=self.config)

    def test_an_empty_notice_prints_nothing(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("GATE", result.stdout)

    def test_the_slot_survives_a_notice_that_came_back_empty(self):
        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotIn("GATE", first.stdout)

        self._restore_the_real_usage_lib()
        second = self._run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("GATE", second.stdout,
                      "an empty tier message spent the PostToolBatch slot, "
                      "so the GATE was never announced this window")


def seed_ledger(path, records):
    with open(path, "w") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


class TestDispatchHook(unittest.TestCase):
    """PreToolUse on Agent|Workflow, as a subprocess. Asserts on emitted
    CONTENT, never on exit status: the hook always exits 0."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.projects = os.path.join(self.tmp, "projects")
        os.makedirs(self.projects)
        self.cache = os.path.join(self.tmp, "budget.json")
        self.anchors = os.path.join(self.tmp, "anchors.json")
        self.ledger = os.path.join(self.tmp, "build-costs.jsonl")
        self.now = time.time()
        # Distinct session_ids: lane_stats collapses rows per session.
        seed_ledger(self.ledger,
                    [{"session_id": "sid%d" % i, "total_output": 200000,
                      "review_output": 200000} for i in range(7)])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self, estimate=True, advisory=True):
        blob = {"dispatch_advisory": advisory, "estimate": estimate}
        return write_config(self.tmp, usage_watch=blob,
                            build_cost={"ledger_path": self.ledger})

    def _anchor_at(self, pct, extra_tokens):
        """An anchored 5h estimate of `pct` + extra_tokens / FIXTURE_RATIO."""
        seed_anchor_store(self.anchors, pct, self.now - 600, self.now + 3600,
                          ratio=FIXTURE_RATIO)
        write_transcript_for(self.projects, self.now - 100, extra_tokens)

    def _run(self, config, prompt_id=None):
        payload = {"hook_event_name": "PreToolUse", "session_id": "s1",
                   "tool_name": "Agent",
                   "tool_input": {"subagent_type": "code-reviewer",
                                  "prompt": "review this"}}
        if prompt_id is not None:
            payload["prompt_id"] = prompt_id
        return run(DISPATCH, payload, config=config)

    def _hso(self, result):
        self.assertTrue(result.stdout.strip(),
                        "the hook printed nothing: %s" % result.stderr)
        return json.loads(result.stdout)["hookSpecificOutput"]

    def test_advisory_off_is_silent(self):
        seed_cache(self.cache, {"five_hour": {"used_percentage": 99.0,
                                              "resets_at": int(self.now) + 3600}})
        result = self._run(self._config(advisory=False))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_no_config_at_all_is_silent(self):
        result = self._run(None)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_a_96_five_hour_reading_asks(self):
        seed_cache(self.cache, {"five_hour": {"used_percentage": 96.0,
                                              "resets_at": int(self.now) + 3600}})
        result = self._run(self._config())
        self.assertEqual(result.returncode, 0, result.stderr)
        blob = json.loads(result.stdout)
        self.assertEqual(blob["hookSpecificOutput"]["permissionDecision"], "ask")
        self.assertIn("USAGE HALT (reading)", blob["systemMessage"])
        self.assertNotIn("floor", blob["hookSpecificOutput"]["additionalContext"])

    def test_the_binding_seven_day_reading_is_named(self):
        seed_cache(self.cache, {
            "five_hour": {"used_percentage": 10.0,
                          "resets_at": int(self.now) + 3600},
            "seven_day": {"used_percentage": 97.0,
                          "resets_at": int(self.now) + 86400}})
        hso = self._hso(self._run(self._config()))
        self.assertEqual(hso["permissionDecision"], "ask")
        self.assertIn("7d usage is 97%", hso["permissionDecisionReason"])

    def test_a_reading_below_halt_forecasts_without_asking(self):
        seed_cache(self.cache, {"five_hour": {"used_percentage": 40.0,
                                              "resets_at": int(self.now) + 3600}})
        seed_anchor_store(self.anchors, 1, self.now - 600, self.now + 3600,
                          ratio=FIXTURE_RATIO)
        hso = self._hso(self._run(self._config()))
        self.assertNotIn("permissionDecision", hso)
        self.assertIn("A review dispatch has historically cost",
                      hso["additionalContext"])

    def test_no_cache_and_an_anchored_estimate_at_halt_asks(self):
        self._anchor_at(90.0, 100000)   # +5 points at FIXTURE_RATIO
        hso = self._hso(self._run(self._config()))
        self.assertEqual(hso["permissionDecision"], "ask")
        self.assertIn("(estimate)", hso["permissionDecisionReason"])
        self.assertIn("read it as a floor", hso["additionalContext"])

    def test_no_cache_and_the_estimator_off_is_silent(self):
        self._anchor_at(90.0, 100000)
        result = self._run(self._config(estimate=False))
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "")

    def test_two_identical_dispatches_in_one_prompt_both_print(self):
        seed_cache(self.cache, {"five_hour": {"used_percentage": 40.0,
                                              "resets_at": int(self.now) + 3600}})
        config = self._config()
        first = self._run(config, prompt_id="p1")
        second = self._run(config, prompt_id="p1")
        self.assertTrue(first.stdout.strip())
        self.assertEqual(first.stdout, second.stdout)


class TestWatchRunsWithoutCodexReview(unittest.TestCase):
    """The Codex line rides on a sibling tool. A missing or broken
    `tools/codex-review/` must cost the line, never the hook.

    A COPY of this tool in a scratch tree, because the real checkout's sibling
    may or may not exist depending on which rows have landed: the copy fixes
    the sibling's state for each case, and a Codex rollout log is present in
    both, so the only thing standing between it and a printed line is the
    import.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(self.tree, "tools"))
        shutil.copytree(TOOL_DIR, os.path.join(self.tree, "tools", "usage-watch"),
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        os.symlink(os.path.join(os.path.dirname(os.path.dirname(TOOL_DIR)), "lib"),
                   os.path.join(self.tree, "lib"))
        self.watch = os.path.join(self.tree, "tools", "usage-watch",
                                  "claude-usage-watch")
        self.codex_home = os.path.join(self.tmp, "codex")
        folder = os.path.join(self.codex_home, "sessions", "2026", "10", "01")
        os.makedirs(folder)
        with open(os.path.join(folder, "rollout-fixture.jsonl"), "w") as handle:
            handle.write(json.dumps({"type": "event_msg", "payload": {
                "type": "token_count", "rate_limits": {"primary": {
                    "used_percent": 100, "window_minutes": 10080,
                    "resets_at": time.time() + 3600}}}}) + "\n")
        self.config = write_config(self.tmp)

    def _run(self):
        return run(self.watch, {"hook_event_name": "UserPromptSubmit",
                                "session_id": "no-codex"},
                   config=self.config, env={"CODEX_HOME": self.codex_home})

    def test_an_absent_sibling_costs_the_line_not_the_hook(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("review gate", result.stdout)
        self.assertEqual(result.stderr.strip(), "")
        # The hook's own job still happens: no reading, so it says UNKNOWN.
        self.assertIn("UNKNOWN", result.stdout)

    def test_a_sibling_that_fails_to_import_costs_the_line_not_the_hook(self):
        broken = os.path.join(self.tree, "tools", "codex-review")
        os.makedirs(broken)
        with open(os.path.join(broken, "codex_review_lib.py"), "w") as handle:
            handle.write("raise RuntimeError('broken sibling')\n")
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("review gate", result.stdout)
        self.assertIn("UNKNOWN", result.stdout)


class TestDispatchHookIsRegistered(unittest.TestCase):
    """A hook nothing fires is a hook that does nothing. install.sh registers
    the plugins from each plugin's hooks/hooks.json, so a feature can ship with green
    tests, a clean --check, and no wiring at all."""

    def _repo(self):
        return os.path.dirname(os.path.dirname(TOOL_DIR))

    def test_manifest_declares_it_on_agent_and_workflow(self):
        manifest = hook_manifest.load(self._repo())
        matchers = [
            row["matcher"]
            for row in hook_manifest.registrations(manifest)
            if row["event"] == "PreToolUse"
            and row["exe"] == "claude-usage-dispatch"]
        self.assertEqual(matchers, ["Agent|Workflow"])

    def test_the_installer_links_it(self):
        with open(os.path.join(self._repo(), "install.sh")) as handle:
            source = handle.read()
        self.assertIn("claude-usage-dispatch", source)
        self.assertIn("tools/usage-watch/claude-usage-dispatch", source)

    def test_the_config_example_documents_the_key(self):
        with open(os.path.join(self._repo(), "config.example.json")) as handle:
            blob = json.load(handle)
        self.assertIn("dispatch_advisory", blob["usage_watch"])
        self.assertIn("//dispatch_advisory", blob["usage_watch"])


def write_codex_fixture(codex_home, used_percent, resets_at,
                        plan_type="test-plan",
                        window_minutes=10080, model="test-model"):
    """A minimal Codex rollout file under `<codex_home>/sessions/...`, shaped
    as a real rollout log is: a session_meta first line, a turn_context model
    line, and a token_count event carrying the `rate_limits` block
    `claude-usage-watch`'s Codex line reads. Always the SAME filename, so a
    second call overwrites rather than leaving two rollout files for
    `snapshot_meter` to choose between by mtime -- deterministic attribution
    for the dedupe tests, which rewrite this fixture mid-test."""
    folder = os.path.join(codex_home, "sessions", "2026", "10", "01")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "rollout-fixture.jsonl")
    lines = [
        {"type": "session_meta",
         "payload": {"id": "fixture", "cwd": codex_home,
                    "originator": "codex_exec",
                    "timestamp": "2026-10-01T11:33:23.285Z"}},
        {"type": "turn_context", "payload": {"model": model}},
        {"type": "event_msg",
         "payload": {"type": "token_count",
                    "info": {"total_token_usage": {
                        "input_tokens": 100000, "cached_input_tokens": 0,
                        "output_tokens": 500}},
                    "rate_limits": {
                        "primary": {"used_percent": used_percent,
                                   "window_minutes": window_minutes,
                                   "resets_at": resets_at},
                        "plan_type": plan_type}}},
    ]
    with open(path, "w") as handle:
        for obj in lines:
            handle.write(json.dumps(obj) + "\n")
    return path


# The one calibration entry the calibrated cases inject. None ships in the
# repository, so the hook can only reach a gate estimate when a test supplies
# one. A median of 0.5 and a p90 of 1.0 points per gate make the arithmetic
# readable: headroom H gives between H // 1.0 and H // 0.5 gates.
CODEX_TEST_CALIBRATION = {
    ("test-plan", 10080, "test-model"): {
        "points_per_million_uncached": 10.0,
        "gate_points": (0.5, 1.0)}}

# Runs the real hook with the calibration table and reference model patched,
# in the same process, so the hook's own import of the shared library gets
# the patched module. A subprocess is needed because the hook reads stdin and
# is only ever run as a program.
CODEX_CALIBRATED_LAUNCHER = """\
import runpy, sys
sys.path.insert(0, %(codex_dir)r)
import codex_review_lib
codex_review_lib.CALIBRATIONS = %(calibrations)r
codex_review_lib.CODEX_MODEL = "test-model"
runpy.run_path(%(watch)r, run_name="__main__")
"""


class TestUsageWatchCodex(unittest.TestCase):
    """The Codex budget line. Every test here points CODEX_HOME at
    its own fixture dir, overriding the module-level isolation above for
    just this one subprocess call -- see write_codex_fixture.

    No calibration ships, so a plain run is the uncalibrated path: a notice
    at 90% used, a halt line at 100%, never a gate count. `run_calibrated`
    injects a calibration to reach the estimate path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.config = write_config(self.tmp)
        self.codex_home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.codex_home, ignore_errors=True)

    def run_watch(self, event="UserPromptSubmit", session="sess-codex",
                 agent_id=None, codex_home=None, config=None):
        payload = {"hook_event_name": event, "session_id": session}
        if agent_id is not None:
            payload["agent_id"] = agent_id
        env = {"CODEX_HOME": codex_home if codex_home is not None
              else self.codex_home, "TZ": "UTC"}
        return run(WATCH, payload, config=config or self.config, env=env)

    def run_calibrated(self, session="sess-codex", config=None):
        launcher = os.path.join(self.tmp, "calibrated-launcher.py")
        with open(launcher, "w") as handle:
            handle.write(CODEX_CALIBRATED_LAUNCHER % {
                "codex_dir": os.path.join(os.path.dirname(TOOL_DIR),
                                          "codex-review"),
                "calibrations": CODEX_TEST_CALIBRATION,
                "watch": WATCH})
        payload = {"hook_event_name": "UserPromptSubmit",
                   "session_id": session}
        env = {"CODEX_HOME": self.codex_home, "TZ": "UTC"}
        return run(launcher, payload, config=config or self.config, env=env)

    @staticmethod
    def context_of(result):
        # json.dumps escapes non-ASCII, so the en dash in a gate range is
        # not literal in raw stdout -- parse first.
        return json.loads(result.stdout)["hookSpecificOutput"][
            "additionalContext"]

    def config_with_threshold(self, value):
        return write_config(self.tmp, usage_watch={"codex_low_gates": value})

    def test_calibrated_notice_is_one_json_object(self):
        write_codex_fixture(self.codex_home, 80, time.time() + 86400)
        config = self.config_with_threshold(30)
        result = self.run_calibrated(config=config)
        self.assertEqual(result.returncode, 0, result.stderr)
        blob = json.loads(result.stdout)
        context = blob["hookSpecificOutput"]["additionalContext"]
        self.assertIn("≈20–40 review gates left", context)
        self.assertIn("Each full-power /build gate", context)

    def test_calibrated_estimate_above_the_threshold_is_silent(self):
        # 80% used gives 20 gates at the low end, which is above 15.
        write_codex_fixture(self.codex_home, 80, time.time() + 86400)
        config = self.config_with_threshold(15)
        result = self.run_calibrated(config=config)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex budget", result.stdout)

    def test_calibrated_estimate_exactly_on_the_threshold_fires(self):
        # The comparison is `low <= threshold`: a low end sitting exactly on
        # the ceiling is still low enough to warn about. 90% used gives 10.
        write_codex_fixture(self.codex_home, 90, time.time() + 86400)
        fires = self.run_calibrated(
            config=self.config_with_threshold(10), session="sess-on")
        self.assertEqual(fires.returncode, 0, fires.stderr)
        self.assertIn("≈10–20 review gates left", self.context_of(fires))

        silent = self.run_calibrated(
            config=self.config_with_threshold(9), session="sess-under")
        self.assertEqual(silent.returncode, 0, silent.stderr)
        self.assertNotIn("codex budget", silent.stdout)

    def test_calibrated_zero_threshold_means_only_at_100_percent(self):
        # A threshold of 0 is documented as "only when the meter reads 100%".
        # 95% used leaves a low end of 5, which a threshold of 0 must not
        # fire on; only the halt tier at 100% may.
        resets_at = time.time() + 86400
        config = self.config_with_threshold(0)

        write_codex_fixture(self.codex_home, 95, resets_at)
        result = self.run_calibrated(config=config, session="sess-zero-95")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex budget", result.stdout)

        write_codex_fixture(self.codex_home, 100, resets_at)
        result2 = self.run_calibrated(config=config, session="sess-zero-100")
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertIn("codex budget", result2.stdout)
        self.assertIn("0 review gates left", result2.stdout)
        self.assertIn("early reset would not show", result2.stdout)

    def test_uncalibrated_zero_threshold_means_only_at_100_percent(self):
        # The same contract with no fitted cost, which is every installation
        # until someone fits one: 0 silences the 90% notice too, and only the
        # halt tier at 100% may fire.
        resets_at = time.time() + 86400
        config = self.config_with_threshold(0)

        write_codex_fixture(self.codex_home, 95, resets_at)
        result = self.run_watch(config=config, session="sess-nofit-zero-95")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex budget", result.stdout)

        write_codex_fixture(self.codex_home, 100, resets_at)
        result2 = self.run_watch(config=config, session="sess-nofit-zero-100")
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertIn("codex budget", result2.stdout)
        self.assertIn("no fitted per-gate cost", result2.stdout)

    def test_invalid_threshold_falls_back_to_the_default(self):
        # A negative or boolean `codex_low_gates` must fall back to 15, not
        # be used as given: a raw -1 would suppress the notice, and a raw
        # `true` (equal to 1 in Python) would too, though the estimate is
        # genuinely low. 87% used gives a low end of 13, which clears 15.
        write_codex_fixture(self.codex_home, 87, time.time() + 86400)
        for index, value in enumerate((-1, True)):
            with self.subTest(value=value):
                result = self.run_calibrated(
                    config=self.config_with_threshold(value),
                    session="sess-invalid-%d" % index)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("≈13–26 review gates left",
                              self.context_of(result))

    def test_below_90_percent_is_silent_and_a_high_threshold_cannot_fire(self):
        # With no estimate there is nothing for the threshold to compare, so
        # a high `codex_low_gates` does not make the line appear early.
        write_codex_fixture(self.codex_home, 50, time.time() + 86400)
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex budget", result.stdout)

        result2 = self.run_watch(config=self.config_with_threshold(60),
                                 session="sess-high-threshold")
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertNotIn("codex budget", result2.stdout)

    def test_uncalibrated_90_percent_is_a_notice_naming_the_missing_fit(self):
        write_codex_fixture(self.codex_home, 90, time.time() + 86400)
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"][
            "additionalContext"]
        self.assertIn("codex budget", context)
        self.assertIn("no fitted per-gate cost", context)
        self.assertNotIn("review gates left", context)

    def test_uncalibrated_threshold_is_exactly_90_not_85_or_92(self):
        # Pin the exact boundary rather than only interior points: 89 stays
        # silent and 90 announces.
        resets_at = time.time() + 86400
        write_codex_fixture(self.codex_home, 89, resets_at)
        result = self.run_watch(session="sess-89")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex budget", result.stdout)

        write_codex_fixture(self.codex_home, 90, resets_at)
        result2 = self.run_watch(session="sess-90")
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertIn("codex budget", result2.stdout)
        self.assertIn("no fitted per-gate cost", result2.stdout)

    def test_uncalibrated_100_percent_is_the_halt_line(self):
        write_codex_fixture(self.codex_home, 100, time.time() + 3600)
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("codex budget: 100% of the 7-day window used",
                      result.stdout)
        self.assertIn("no fitted per-gate cost", result.stdout)
        self.assertIn("early reset would not show", result.stdout)
        self.assertNotIn("cannot run", result.stdout)
        self.assertNotIn("can run", result.stdout)

    def test_an_unlisted_plan_is_uncalibrated_too(self):
        # A plan with no table entry takes the same 90% path, and names the
        # plan it could not price.
        write_codex_fixture(self.codex_home, 92, time.time() + 86400,
                            plan_type="other-plan")
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no fitted per-gate cost for plan other-plan",
                      result.stdout)

    def test_codex_line_delivered_alone_via_finally_when_main_body_is_quiet(self):
        # A healthy status-line cache makes `_main_body` print nothing at all
        # (reading fine, no tier crossed), so the only way the Codex line can
        # reach stdout is main()'s `finally` clause. The second prompt in the
        # same session must still be deduped -- which only happens if
        # `finally` records the announcement, same as the ride-along path in
        # `emit` does.
        now = time.time()
        cache_path = os.path.join(self.tmp, "budget.json")
        seed_cache(cache_path, {
            "five_hour": {"used_percentage": 10, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 10, "resets_at": now + 3 * 86400}})
        write_codex_fixture(self.codex_home, 90, now + 86400)

        first = self.run_watch(session="finally-sess")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("no fitted per-gate cost", first.stdout)
        self.assertNotIn("UNKNOWN", first.stdout)  # proves the quiet path, not the ride-along path

        second = self.run_watch(session="finally-sess")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotIn("codex budget", second.stdout)

    def test_resets_at_in_the_past_is_absent(self):
        write_codex_fixture(self.codex_home, 90, time.time() - 10)
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex budget", result.stdout)

    def test_dedupe_hour_rounded_jitter_and_per_session_rearm(self):
        # The base resets_at sits exactly half an hour before the window_key
        # boundary (ceil-to-the-hour) of a point ~24h out, so a +5s jitter
        # never crosses that boundary and the dedupe key is deterministic --
        # a value chosen near the boundary itself would make this test flaky
        # depending on the instant it happened to run.
        candidate = time.time() + 86400
        boundary = math.ceil(candidate / 3600.0) * 3600
        resets_at = boundary - 1800
        write_codex_fixture(self.codex_home, 90, resets_at)
        first = self.run_watch(session="sess-a")
        self.assertIn("codex budget", first.stdout)

        second = self.run_watch(session="sess-a")
        self.assertNotIn("codex budget", second.stdout)

        write_codex_fixture(self.codex_home, 90, resets_at + 5)
        third = self.run_watch(session="sess-a")
        self.assertNotIn("codex budget", third.stdout)

        fourth = self.run_watch(session="sess-b")
        self.assertIn("codex budget", fourth.stdout)

    def test_a_new_window_is_announced_again(self):
        # A different `resets_at` is a different window, so the dedupe key
        # changes and the same session hears about it once more.
        resets_at = time.time() + 86400
        write_codex_fixture(self.codex_home, 90, resets_at)
        first = self.run_watch(session="sess-window")
        self.assertIn("codex budget", first.stdout)

        repeat = self.run_watch(session="sess-window")
        self.assertNotIn("codex budget", repeat.stdout)

        write_codex_fixture(self.codex_home, 90, resets_at + 7 * 86400)
        next_window = self.run_watch(session="sess-window")
        self.assertIn("codex budget", next_window.stdout)

    def test_posttoolbatch_and_nonempty_agent_id_are_silent(self):
        write_codex_fixture(self.codex_home, 90, time.time() + 86400)
        result = self.run_watch(event="PostToolBatch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex budget", result.stdout)

        result2 = self.run_watch(event="UserPromptSubmit",
                                 agent_id="worker-1", session="sess-agent")
        self.assertEqual(result2.returncode, 0, result2.stderr)
        self.assertNotIn("codex budget", result2.stdout)

    def test_reading_none_path_combines_unknown_and_codex(self):
        write_codex_fixture(self.codex_home, 90, time.time() + 86400)
        result = self.run_watch()
        self.assertEqual(result.returncode, 0, result.stderr)
        blob = json.loads(result.stdout)
        context = blob["hookSpecificOutput"]["additionalContext"]
        self.assertIn("UNKNOWN", context)
        self.assertIn("no fitted per-gate cost", context)

    def test_tier_crossing_and_codex_line_are_one_json_object(self):
        # A tier crossing (`announce_tier` -> `emit`) and a pending Codex
        # line must ride the SAME print. If `announce_tier` ever printed
        # directly instead of going through `emit`, the Codex line would be
        # left pending and main()'s `finally` clause would print it
        # separately -- stdout would then carry two JSON objects instead of
        # one.
        now = time.time()
        cache_path = os.path.join(self.tmp, "budget.json")
        seed_cache(cache_path, {
            "five_hour": {"used_percentage": 90.0, "resets_at": now + 3600}})
        write_codex_fixture(self.codex_home, 90, now + 86400)

        result = self.run_watch(session="sess-combo")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, result.stdout)
        context = json.loads(lines[0])["hookSpecificOutput"]["additionalContext"]
        self.assertIn("GATE", context)
        self.assertIn("no fitted per-gate cost", context)

    def test_disabled_usage_watch_suppresses_the_codex_line(self):
        # `_codex_line` carries its OWN `usage.is_enabled()` guard, separate
        # from `_main_body`'s -- without it, turning usage-watch off would
        # still surface Codex budget info, which is not what "disabled"
        # means to someone who set it.
        write_codex_fixture(self.codex_home, 90, time.time() + 86400)
        config = write_config(self.tmp, usage_watch={"enabled": False})
        result = self.run_watch(config=config, session="sess-disabled")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex budget", result.stdout)

    def test_no_sessions_dir_is_silent(self):
        empty_home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty_home, ignore_errors=True)
        result = self.run_watch(codex_home=empty_home)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("codex budget", result.stdout)

    def test_through_a_symlink_the_import_still_works(self):
        # Catches a broken `..` import (e.g. abspath over realpath): the hook
        # is run through a link and must still find the sibling library.
        write_codex_fixture(self.codex_home, 90, time.time() + 86400)
        symlink_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, symlink_dir, ignore_errors=True)
        link_path = os.path.join(symlink_dir, "claude-usage-watch")
        os.symlink(WATCH, link_path)
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = self.config
        env["CODEX_HOME"] = self.codex_home
        env["TZ"] = "UTC"
        env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        result = subprocess.run(
            [sys.executable, link_path],
            input=json.dumps({"hook_event_name": "UserPromptSubmit",
                              "session_id": "sess-symlink"}),
            capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("no fitted per-gate cost", context)


if __name__ == "__main__":
    unittest.main()
