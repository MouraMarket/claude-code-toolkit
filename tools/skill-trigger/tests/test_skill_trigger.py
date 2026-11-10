"""Tests for skill-trigger (parser and classification).

`find_skill_name`'s index-reuse guard is the load-bearing test in this file:
MEASURED in `tests/fixtures/skill-call.jsonl`, the real stream reuses
`index=1` for a later `text` block after the Skill call's own block closes.
A parser that concatenates every `content_block_delta` matching an index for
the rest of the file — rather than stopping at that block's own
`content_block_stop` — runs straight past the Skill block's end and, given a
second `tool_use` at the same reused index, would silently read its
arguments as the first block's.
"""

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))
sys.path.insert(0, TOOL_DIR)
import skill_trigger_lib as lib  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures")
CLI_PATH = os.path.join(TOOL_DIR, "claude-skill-trigger")


def _load_lines(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return f.readlines()


def _content_block_start(index, block_type, name=None):
    content_block = {"type": block_type}
    if name is not None:
        content_block["name"] = name
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": index,
            "content_block": content_block,
        },
    }


def _content_block_delta(index, partial_json):
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "input_json_delta", "partial_json": partial_json},
        },
    }


def _content_block_stop(index):
    return {
        "type": "stream_event",
        "event": {"type": "content_block_stop", "index": index},
    }


class TestIterEvents(unittest.TestCase):
    def test_skips_unparseable_lines(self):
        lines = [
            "not json at all\n",
            json.dumps({"type": "system", "subtype": "init"}) + "\n",
            "\n",
        ]
        events = list(lib.iter_events(lines))
        self.assertEqual(events, [{"type": "system", "subtype": "init"}])


class TestFindSkillNameOnRealFixtures(unittest.TestCase):
    def test_skill_call_fixture_returns_the_skill_name(self):
        events = list(lib.iter_events(_load_lines("skill-call.jsonl")))
        self.assertEqual(lib.find_skill_name(events), "toolkit-build:build")

    def test_no_skill_fixture_returns_none(self):
        events = list(lib.iter_events(_load_lines("no-skill.jsonl")))
        self.assertIsNone(lib.find_skill_name(events))


class TestFindSkillNameSynthetic(unittest.TestCase):
    def test_single_fragment_returns_the_skill_name(self):
        events = [
            _content_block_start(1, "tool_use", "Skill"),
            _content_block_delta(1, '{"skill": "a:b", "args": "x"}'),
            _content_block_stop(1),
        ]
        self.assertEqual(lib.find_skill_name(events), "a:b")

    def test_split_mid_key_returns_the_skill_name(self):
        events = [
            _content_block_start(1, "tool_use", "Skill"),
            _content_block_delta(1, '{"sk'),
            _content_block_delta(1, 'ill": "a:b"}'),
            _content_block_stop(1),
        ]
        self.assertEqual(lib.find_skill_name(events), "a:b")

    def test_split_mid_value_returns_the_skill_name(self):
        events = [
            _content_block_start(1, "tool_use", "Skill"),
            _content_block_delta(1, '{"skill": "a'),
            _content_block_delta(1, ':b"}'),
            _content_block_stop(1),
        ]
        self.assertEqual(lib.find_skill_name(events), "a:b")

    def test_block_that_never_completes_returns_none(self):
        events = [
            _content_block_start(1, "tool_use", "Skill"),
            _content_block_delta(1, '{"skill": "a'),
            # No closing quote, no content_block_stop -- stream just ends.
        ]
        self.assertIsNone(lib.find_skill_name(events))

    def test_two_skill_blocks_returns_the_first(self):
        events = [
            _content_block_start(1, "tool_use", "Skill"),
            _content_block_delta(1, '{"skill": "first:one"}'),
            _content_block_stop(1),
            _content_block_start(2, "tool_use", "Skill"),
            _content_block_delta(2, '{"skill": "second:two"}'),
            _content_block_stop(2),
        ]
        self.assertEqual(lib.find_skill_name(events), "first:one")

    def test_index_reuse_after_incomplete_block_returns_none(self):
        """The terminator is what makes this pass: an implementation that
        ignores content_block_stop and just keeps concatenating fragments at
        a matching index would return "a...wrong:skill" or "wrong:skill"
        instead of None.
        """
        events = [
            _content_block_start(1, "tool_use", "Skill"),
            _content_block_delta(1, '{"skill": "a'),  # never closes
            _content_block_stop(1),
            _content_block_start(1, "tool_use", "Skill"),
            _content_block_delta(1, '{"skill": "wrong:skill"}'),
            _content_block_stop(1),
        ]
        self.assertIsNone(lib.find_skill_name(events))


class TestClassify(unittest.TestCase):
    def test_returns_three_distinct_identity_constants(self):
        self.assertIs(lib.classify(None, "a:b"), lib.NO_SKILL)
        self.assertIs(lib.classify("a:b", "a:b"), lib.MATCH)
        self.assertIs(lib.classify("x:y", "a:b"), lib.COLLISION)

    def test_requires_both_arguments(self):
        with self.assertRaises(TypeError):
            lib.classify("a:b")


class TestExtractInit(unittest.TestCase):
    def test_returns_skills_list_from_fixture(self):
        events = list(lib.iter_events(_load_lines("skill-call.jsonl")))
        init = lib.extract_init(events)
        self.assertIsNotNone(init)
        self.assertGreater(len(init["skills"]), 0)
        self.assertTrue(init["claude_code_version"])


# --- the subprocess runner ---------------------------------------------
#
# A stub `claude` executable is put at the front of PATH for the duration of
# each test. `run_case` reads `os.environ` at call time (see
# skill_trigger_lib.run_case), so mutating the real `os.environ["PATH"]`
# before calling it is what actually takes effect -- these fixtures do that,
# and always restore it in a `finally`.

_STUB_REPLAY_TEMPLATE = '''#!/usr/bin/env python3
import os, sys

envfile = os.environ.get("STUB_ENVFILE")
if envfile:
    with open(envfile, "w") as f:
        f.write(os.environ.get("CLAUDECODE", "ABSENT"))

pidfile = os.environ.get("STUB_PIDFILE")
if pidfile:
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

with open("__FIXTURE_PATH__") as f:
    sys.stdout.write(f.read())
sys.stdout.flush()
'''

# Prints one init line, then hangs -- with a REAL descendant in the same
# process group (via os.fork, no setsid of its own). This is what makes the
# process-group test meaningful: a cleanup that signals only the direct
# child (run_case's own proc.pid) leaves this descendant alive, and
# `os.killpg(pgid, 0)` would still find a live member of the group. Each
# branch is self-bounded at ~2 minutes so a broken cleanup during the
# mutation exercise below leaks nothing permanent.
#
# STUB_TERM_DELAY makes the descendant, on SIGTERM, close stdout and then
# take that many seconds to die. That is the state Linux CI hit by chance:
# the pipe is closed, so run_case's reader sees EOF and returns, but the
# member is still in the group (on Linux, a zombie awaiting init's reap
# still answers signal 0). Closing stdout is load-bearing -- a member still
# holding the pipe keeps the reader blocked until it exits, hiding the race.
_STUB_BLOCK = '''#!/usr/bin/env python3
import json, os, signal, sys, time

pidfile = os.environ.get("STUB_PIDFILE")
if pidfile:
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))

child_pid = os.fork()
if child_pid == 0:
    term_delay = os.environ.get("STUB_TERM_DELAY")
    if term_delay:
        def _slow_exit(signum, frame):
            os.close(1)
            time.sleep(float(term_delay))
            os._exit(0)
        signal.signal(signal.SIGTERM, _slow_exit)
    for _ in range(120):
        time.sleep(1)
else:
    sys.stdout.write(json.dumps({"type": "system", "subtype": "init",
                                  "skills": ["x"],
                                  "claude_code_version": "1.0"}) + "\\n")
    sys.stdout.flush()
    for _ in range(120):
        time.sleep(1)
'''

_STUB_EXIT_NONZERO = '''#!/usr/bin/env python3
import sys
sys.exit(1)
'''

_STUB_GARBAGE = '''#!/usr/bin/env python3
import sys
sys.stdout.write("not json at all\\n")
sys.stdout.flush()
'''

# Zero-event fixtures for the RUNNER_ERROR discriminator (init is None):
# neither of these ever emits an init line, which is exactly what should
# make them RUNNER_ERROR rather than falling through to OK/NO_SKILL.
_STUB_EXIT_ZERO_NO_OUTPUT = '''#!/usr/bin/env python3
import sys
sys.exit(0)
'''

_STUB_HANG_NO_OUTPUT = '''#!/usr/bin/env python3
import time
for _ in range(120):
    time.sleep(1)
'''

# Emits init, then crashes on its own (auth/network failure shape): the
# child closes stdout by itself (EOF, not our own early-kill and not a
# timeout), and its own exit code is non-zero. Before the fix, this fell
# through to status="OK", observed=None -- a crashed run reported as a
# genuine NO_SKILL routing result.
_STUB_INIT_THEN_EXIT_1 = '''#!/usr/bin/env python3
import json, sys
sys.stdout.write(json.dumps({"type": "system", "subtype": "init",
                              "skills": ["x"], "plugins": [],
                              "claude_code_version": "1.0"}) + "\\n")
sys.stdout.flush()
sys.exit(1)
'''

# Emits init, then hangs past any timeout (a slow run, not a crash): the
# child never closes stdout and never exits on its own. A timeout after
# init with no Skill call is an incomplete run, not a NO_SKILL result.
_STUB_INIT_THEN_HANG = '''#!/bin/sh
printf '%s\\n' '{"type": "system", "subtype": "init", "skills": ["x"], "plugins": [], "claude_code_version": "1.0"}'
exec sleep 120
'''

# Emits a complete, resolvable Skill call and then keeps printing forever,
# appending to a side file on every post-resolution line. This is the
# early-kill test's fixture: asserted by side effect (the file's length),
# never by elapsed time (asserting on a wall clock is flaky).
_STUB_KEEPS_PRINTING_AFTER_SKILL = '''#!/usr/bin/env python3
import json, os, sys, time

logfile = os.environ.get("STUB_POST_RESOLVE_LOG")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


emit({"type": "system", "subtype": "init", "skills": ["x"], "plugins": [],
      "claude_code_version": "1.0"})
emit({"type": "stream_event", "event": {
    "type": "content_block_start", "index": 1,
    "content_block": {"type": "tool_use", "name": "Skill", "input": {}}}})
emit({"type": "stream_event", "event": {
    "type": "content_block_delta", "index": 1,
    "delta": {"type": "input_json_delta",
              "partial_json": json.dumps({"skill": "toolkit-build:build"})}}})
emit({"type": "stream_event", "event": {
    "type": "content_block_stop", "index": 1}})

# The Skill block is fully resolved above -- nothing after this point should
# still be running once run_case returns, if it kills at resolution.
for _ in range(600):
    if logfile:
        with open(logfile, "a") as f:
            f.write("x")
    emit({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 99,
        "delta": {"type": "text_delta", "text": "noise"}}})
    time.sleep(0.1)
'''


def _replay_stub(fixture_name):
    return _STUB_REPLAY_TEMPLATE.replace(
        "__FIXTURE_PATH__", os.path.join(FIXTURES, fixture_name)
    )


# Records the child's own argv (`run_case` must append `--settings`
# with the read-only payload), then replays a fixture exactly like
# _STUB_REPLAY_TEMPLATE -- so a test using this stub still gets a normal
# NO_SKILL run out of `run_case`/`run_positive_control`, and can separately
# inspect what argv it was actually launched with.
_STUB_RECORD_ARGV = '''#!/usr/bin/env python3
import json, os, sys

argvfile = os.environ.get("STUB_ARGVFILE")
if argvfile:
    with open(argvfile, "w") as f:
        f.write(json.dumps(sys.argv[1:]))

with open("__FIXTURE_PATH__") as f:
    sys.stdout.write(f.read())
sys.stdout.flush()
'''


# Every stub dir THIS process created, so tearDownModule's leak sweep
# matches only its own stubs -- a concurrent run of this suite has live
# stubs of its own, which are not a leak here.
_CREATED_STUB_DIRS = []


@contextlib.contextmanager
def _stub_claude(script_body):
    """Put a stub `claude` executable at the front of PATH for the block."""
    stub_dir = tempfile.mkdtemp(prefix="skill-trigger-stub-")
    _CREATED_STUB_DIRS.append(stub_dir)
    stub_path = os.path.join(stub_dir, "claude")
    with open(stub_path, "w") as f:
        f.write(script_body)
    os.chmod(stub_path, stat.S_IRWXU)
    original_path = os.environ.get("PATH", "")
    os.environ["PATH"] = stub_dir + os.pathsep + original_path
    try:
        yield stub_dir
    finally:
        os.environ["PATH"] = original_path
        shutil.rmtree(stub_dir, ignore_errors=True)


@contextlib.contextmanager
def _empty_path():
    """Point PATH at an empty directory only -- guarantees no `claude`
    binary is reachable, even if one is installed on this machine.
    """
    empty_dir = tempfile.mkdtemp(prefix="skill-trigger-empty-path-")
    original_path = os.environ.get("PATH", "")
    os.environ["PATH"] = empty_dir
    try:
        yield
    finally:
        os.environ["PATH"] = original_path
        shutil.rmtree(empty_dir, ignore_errors=True)


class TestRunCase(unittest.TestCase):
    """RUNNER_ERROR must never be reportable as NO_SKILL -- a broken
    harness is not a routing result -- so every test here asserts `status`
    explicitly, never just `observed`.
    """

    def test_stub_replaying_skill_call_fixture(self):
        with _stub_claude(_replay_stub("skill-call.jsonl")):
            run = lib.run_case("irrelevant prompt", timeout_s=10)
        self.assertEqual(run.status, "OK")
        self.assertEqual(run.observed, "toolkit-build:build")
        self.assertIsNotNone(run.init)

    def test_stub_replaying_no_skill_fixture(self):
        with _stub_claude(_replay_stub("no-skill.jsonl")):
            run = lib.run_case("irrelevant prompt", timeout_s=10)
        self.assertEqual(run.status, "OK")
        self.assertIsNone(run.observed)

    def test_init_then_exit_nonzero_is_runner_error_not_ok(self):
        """A child that emits init and then crashes on its own (EOF, not
        our kill and not a timeout) with a non-zero exit must not read as a
        genuine NO_SKILL routing result.
        """
        with _stub_claude(_STUB_INIT_THEN_EXIT_1):
            run = lib.run_case("irrelevant prompt", timeout_s=10)
        self.assertEqual(run.status, "RUNNER_ERROR")
        self.assertIsNotNone(run.init)
        self.assertIn("exit", run.error)
        self.assertIn("1", run.error)

    def test_timeout_after_init_is_incomplete_not_no_skill(self):
        """A timeout after init with no Skill call is neither a harness
        failure nor a routing result: it must never read as NO_SKILL."""
        with _stub_claude(_STUB_INIT_THEN_HANG):
            # A shell stub starts in milliseconds; the margin is wide, not a guarantee.
            run = lib.run_case("irrelevant prompt", timeout_s=2.0)
        self.assertEqual(run.status, "INCOMPLETE")
        self.assertIsNone(run.observed)
        self.assertIsNotNone(run.init)
        self.assertIn("timed out", run.error)


    def test_init_then_no_skill_stream_exit_zero_still_ok(self):
        """The NO_SKILL path must survive the fix above: a clean exit after
        init with no Skill call is still a real, reportable OK/NO_SKILL
        result, not RUNNER_ERROR.
        """
        with _stub_claude(_replay_stub("no-skill.jsonl")):
            run = lib.run_case("irrelevant prompt", timeout_s=10)
        self.assertEqual(run.status, "OK")
        self.assertIsNone(run.observed)

    def test_missing_claude_binary_is_runner_error_not_no_skill(self):
        with _empty_path():
            run = lib.run_case("irrelevant prompt", timeout_s=5)
        self.assertEqual(run.status, "RUNNER_ERROR")
        self.assertIsNone(run.observed)

    def test_non_zero_exit_with_no_events_is_runner_error(self):
        with _stub_claude(_STUB_EXIT_NONZERO):
            run = lib.run_case("irrelevant prompt", timeout_s=5)
        self.assertEqual(run.status, "RUNNER_ERROR")
        self.assertIsNone(run.observed)

    def test_all_output_unparseable_is_runner_error(self):
        with _stub_claude(_STUB_GARBAGE):
            run = lib.run_case("irrelevant prompt", timeout_s=5)
        self.assertEqual(run.status, "RUNNER_ERROR")
        self.assertIsNone(run.observed)

    def _run_blocking_stub(self, extra_env):
        """Run `_STUB_BLOCK` to its timeout; return the stub's pgid."""
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-run-")
        pidfile = os.path.join(tmp_dir, "pid")
        env = dict(extra_env, STUB_PIDFILE=pidfile)
        os.environ.update(env)
        try:
            with _stub_claude(_STUB_BLOCK):
                start = time.monotonic()
                # A Python stub (it needs fork and signals) can be slow
                # to print init on a loaded machine, and a timeout
                # before init reads RUNNER_ERROR. 3s is margin, not a budget.
                run = lib.run_case("irrelevant prompt", timeout_s=3.0)
                elapsed = time.monotonic() - start
        finally:
            for key in env:
                del os.environ[key]

        # Catastrophe bound, not a budget (elapsed time is never
        # the actual check) -- this only stops a broken
        # cleanup from hanging the suite forever.
        self.assertLess(elapsed, 30)

        # A timeout after init with no Skill call is an incomplete run.
        self.assertEqual(run.status, "INCOMPLETE")
        self.assertIsNone(run.observed)

        self.assertTrue(os.path.exists(pidfile))
        with open(pidfile) as f:
            pgid = int(f.read().strip())
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return pgid

    def assertGroupGone(self, pgid, deadline_s=10.0):
        """Poll until the process group no longer exists.

        `run_case` waits only for the group LEADER, its own child. Every
        other member got the same `killpg` but dies, and is reaped by init,
        on the kernel's schedule -- so a single check right after `run_case`
        returns races that, and failed twice on Linux CI. `deadline_s` is
        not a budget: an unsignalled member of `_STUB_BLOCK` lives ~120s, so
        10s still separates killed from not-killed by an order of magnitude.
        PermissionError (Darwin's answer for a zombie-only group, see
        `_kill_process_group`) means "not gone yet", so it keeps polling.
        """
        deadline = time.monotonic() + deadline_s
        while True:
            try:
                os.killpg(pgid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                pass
            if time.monotonic() >= deadline:
                self.fail("process group {0} still exists {1}s after "
                          "run_case returned".format(pgid, deadline_s))
            time.sleep(0.05)

    def test_timeout_kills_the_whole_process_group(self):
        pgid = self._run_blocking_stub({})
        self.assertGroupGone(pgid)

    def test_group_check_tolerates_a_member_slow_to_exit(self):
        """Pins the group-check fix deterministically: the slow member is still in
        the group when `run_case` returns, the state CI hit by chance."""
        pgid = self._run_blocking_stub({"STUB_TERM_DELAY": "1"})
        self.assertGroupGone(pgid)

    def test_exit_zero_with_no_output_is_runner_error(self):
        """Midpoint defect: exit 0 with no output fell through to status='OK',
        observed=None -- a harness that never started, serialised as a
        real routing result (NO_SKILL). Assert `status`, not `observed`:
        both shapes already return `observed=None`, which is why it slipped
        through.
        """
        with _stub_claude(_STUB_EXIT_ZERO_NO_OUTPUT):
            run = lib.run_case("irrelevant prompt", timeout_s=5)
        self.assertEqual(run.status, "RUNNER_ERROR")
        self.assertIsNone(run.observed)

    def test_timeout_with_no_output_is_runner_error(self):
        """Same defect, the timeout-shaped half: no init event ever
        arrives, so this must not read as a clean 'no skill call' result.
        """
        with _stub_claude(_STUB_HANG_NO_OUTPUT):
            run = lib.run_case("irrelevant prompt", timeout_s=0.5)
        self.assertEqual(run.status, "RUNNER_ERROR")
        self.assertIsNone(run.observed)

    def test_early_kill_observed_by_side_effect_not_elapsed_time(self):
        """The buffering defect: `communicate()` blocks until the child
        exits, so a stub emitting a complete Skill call still burned the
        full timeout. Asserted without a wall clock: the stub
        appends to a side file on every line it emits *after* the Skill
        block resolves. A streaming implementation kills at resolution and
        the file stays short or is never created; a buffering one drains
        the whole stream first and the file is long.
        """
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-earlykill-")
        logfile = os.path.join(tmp_dir, "post-resolve.log")
        os.environ["STUB_POST_RESOLVE_LOG"] = logfile
        try:
            with _stub_claude(_STUB_KEEPS_PRINTING_AFTER_SKILL):
                run = lib.run_case("irrelevant prompt", timeout_s=5)
        finally:
            del os.environ["STUB_POST_RESOLVE_LOG"]

        self.assertEqual(run.status, "OK")
        self.assertEqual(run.observed, "toolkit-build:build")

        if os.path.exists(logfile):
            with open(logfile) as f:
                content = f.read()
        else:
            content = ""
        # The stub writes one "x" per 0.1s post-resolution line. A run_case
        # that kills at resolution returns within a fraction of a second; a
        # buffering one blocks for the whole 5s timeout, which at this
        # interval would append ~50 characters. 10 cleanly separates the two.
        self.assertLess(
            len(content), 10,
            "run_case consumed the stream past the Skill block's "
            "resolution instead of killing the process group at that point")
        shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_claudecode_removed_from_child_environment(self):
        """Proved from the CHILD's side: the stub itself writes what it
        observed, so inspecting the dict we pass proves nothing about what
        the child actually saw.
        """
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-env-")
        envfile = os.path.join(tmp_dir, "env")
        os.environ["STUB_ENVFILE"] = envfile
        os.environ["CLAUDECODE"] = "1"
        try:
            with _stub_claude(_replay_stub("no-skill.jsonl")):
                lib.run_case("irrelevant prompt", timeout_s=10)
        finally:
            del os.environ["STUB_ENVFILE"]
            del os.environ["CLAUDECODE"]

        self.assertTrue(os.path.exists(envfile))
        with open(envfile) as f:
            seen = f.read().strip()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        self.assertEqual(seen, "ABSENT")


# --- case file loading, the CLI, and the report ------------------------

_STUB_SENTINEL = '''#!/usr/bin/env python3
import os
sentinel = os.environ.get("STUB_SENTINEL")
if sentinel:
    with open(sentinel, "w") as f:
        f.write("invoked")
'''

# Emits a distinct Skill-call stream per prompt prefix, so one CLI run can
# exercise all three classify() states in a single case file without three
# separate fixture files.
_STUB_MULTI_STATE = '''#!/usr/bin/env python3
import json, sys

prompt = sys.argv[sys.argv.index("-p") + 1]


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")


emit({"type": "system", "subtype": "init", "skills": ["x"], "plugins": [],
      "claude_code_version": "9.9.9"})

if prompt.startswith("MATCH:"):
    skill = "toolkit-build:build"
elif prompt.startswith("COLLISION:"):
    skill = "wrong:skill"
else:
    skill = None

if skill is not None:
    emit({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "name": "Skill", "input": {}}}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta",
                  "partial_json": json.dumps({"skill": skill})}}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 1}})

sys.stdout.flush()
'''


class TestLoadCases(unittest.TestCase):
    """Unit-level coverage of the case-file schema, independent of the CLI's
    exit-code wiring (that's TestCLIMalformedInputRunsNothing below).
    """

    def _write(self, tmp_dir, content):
        path = os.path.join(tmp_dir, "cases.json")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_missing_file_raises_case_file_error(self):
        with self.assertRaises(lib.CaseFileError):
            lib.load_cases("/nonexistent/path/cases.json")

    def test_not_valid_json_raises_case_file_error(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, "not json {")
            with self.assertRaises(lib.CaseFileError):
                lib.load_cases(path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_not_a_list_raises_case_file_error(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps({"a": 1}))
            with self.assertRaises(lib.CaseFileError):
                lib.load_cases(path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_empty_list_raises_case_file_error(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps([]))
            with self.assertRaises(lib.CaseFileError):
                lib.load_cases(path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_element_not_an_object_raises_case_file_error_naming_index(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(["just a string"]))
            with self.assertRaises(lib.CaseFileError) as ctx:
                lib.load_cases(path)
            self.assertIn("case 0", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_missing_prompt_raises_case_file_error_naming_index(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps([{"expect": "a:b"}]))
            with self.assertRaises(lib.CaseFileError) as ctx:
                lib.load_cases(path)
            self.assertIn("case 0", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_missing_expect_raises_case_file_error_naming_index(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps([{"prompt": "hi"}]))
            with self.assertRaises(lib.CaseFileError) as ctx:
                lib.load_cases(path)
            self.assertIn("case 0", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_invalid_control_value_raises_case_file_error_naming_index(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "hi", "expect": "a:b", "control": "negative"}]))
            with self.assertRaises(lib.CaseFileError) as ctx:
                lib.load_cases(path)
            self.assertIn("case 0", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_positive_control_without_colon_raises_case_file_error(self):
        """A positive-control case's `expect` must be fully-qualified
        `plugin:skill` -- it has to resolve to a file the positive control can mutate.
        """
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "hi", "expect": "bare_name",
                  "control": "positive"}]))
            with self.assertRaises(lib.CaseFileError) as ctx:
                lib.load_cases(path)
            self.assertIn("case 0", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_bare_expect_without_colon_is_legal_on_non_control_case(self):
        """Bare names remain legal where nothing resolves a path."""
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "hi", "expect": "bare_name"}]))
            cases = lib.load_cases(path)
            self.assertEqual(
                cases,
                [{"prompt": "hi", "expect": "bare_name", "forbid": None,
                  "control": None}])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _assert_rejected_naming_case_0(self, content):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(content))
            with self.assertRaises(lib.CaseFileError) as ctx:
                lib.load_cases(path)
            self.assertIn("case 0", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_valid_forbid_case_loads_with_expect_none(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "hi", "forbid": "a:b"}]))
            cases = lib.load_cases(path)
            self.assertEqual(
                cases,
                [{"prompt": "hi", "expect": None, "forbid": "a:b",
                  "control": None}])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_expect_only_case_loads_with_forbid_none(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "hi", "expect": "a:b"}]))
            self.assertIsNone(lib.load_cases(path)[0]["forbid"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_both_expect_and_forbid_is_rejected(self):
        self._assert_rejected_naming_case_0(
            [{"prompt": "hi", "expect": "a:b", "forbid": "c:d"}])

    def test_neither_expect_nor_forbid_is_rejected(self):
        self._assert_rejected_naming_case_0([{"prompt": "hi"}])

    def test_non_string_forbid_is_rejected(self):
        self._assert_rejected_naming_case_0([{"prompt": "hi", "forbid": 3}])

    def test_empty_string_forbid_is_rejected(self):
        self._assert_rejected_naming_case_0([{"prompt": "hi", "forbid": ""}])

    def test_forbid_with_positive_control_is_rejected(self):
        self._assert_rejected_naming_case_0(
            [{"prompt": "hi", "forbid": "a:b", "control": "positive"}])

    def test_control_defaults_to_none_when_absent(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "hi", "expect": "a:b"}]))
            cases = lib.load_cases(path)
            self.assertIsNone(cases[0]["control"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_slash_prompt_raises_case_file_error_naming_index(self):
        """A prompt whose lstrip() starts with '/' directly invokes a
        command or skill via `!` preprocessing that runs before any tool
        call -- outside the read-only hook entirely."""
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "/x", "expect": "a:b"}]))
            with self.assertRaises(lib.CaseFileError) as ctx:
                lib.load_cases(path)
            self.assertIn("case 0", str(ctx.exception))
            self.assertIn("starts with '/'", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_slash_prompt_with_leading_whitespace_raises_case_file_error(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "  /x", "expect": "a:b"}]))
            with self.assertRaises(lib.CaseFileError) as ctx:
                lib.load_cases(path)
            self.assertIn("case 0", str(ctx.exception))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_prompt_with_embedded_slash_is_legal(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "a/b", "expect": "a:b"}]))
            cases = lib.load_cases(path)
            self.assertEqual(cases[0]["prompt"], "a/b")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_prompt_mentioning_a_path_is_legal(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cases-")
        try:
            path = self._write(tmp_dir, json.dumps(
                [{"prompt": "use /tmp", "expect": "a:b"}]))
            cases = lib.load_cases(path)
            self.assertEqual(cases[0]["prompt"], "use /tmp")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestCLIMalformedInputRunsNothing(unittest.TestCase):
    """Every malformed-input path exits 2 and invokes nothing -- proven with
    a sentinel file a stub `claude` writes if it runs at all. An exit-code-
    only assertion would not show that no subprocess ran.
    """

    def _assert_refused(self, args_fn, cases_content):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cli-")
        try:
            sentinel = os.path.join(tmp_dir, "sentinel")
            cases_path = os.path.join(tmp_dir, "cases.json")
            if cases_content is not None:
                with open(cases_path, "w") as f:
                    f.write(cases_content)
            args = args_fn(cases_path)
            with _stub_claude(_STUB_SENTINEL):
                env = dict(os.environ)
                env["STUB_SENTINEL"] = sentinel
                result = subprocess.run(
                    [CLI_PATH] + args, capture_output=True, text=True,
                    env=env, cwd=tmp_dir)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertFalse(
                os.path.exists(sentinel),
                "malformed input must run no subprocess")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_cases_flag_absent(self):
        self._assert_refused(lambda cases_path: ["--runs", "1"], None)

    def test_cases_file_missing(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "1"], None)

    def test_cases_file_not_valid_json(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "1"],
            "not json {")

    def test_cases_file_not_a_list(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "1"],
            json.dumps({"a": 1}))

    def test_cases_file_empty_list(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "1"],
            json.dumps([]))

    def test_cases_file_element_not_an_object(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "1"],
            json.dumps(["just a string"]))

    def test_cases_file_missing_prompt(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "1"],
            json.dumps([{"expect": "a:b"}]))

    def test_cases_file_missing_expect(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "1"],
            json.dumps([{"prompt": "hi"}]))

    def test_cases_file_invalid_control_value(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "1"],
            json.dumps([{"prompt": "hi", "expect": "a:b",
                          "control": "negative"}]))

    def test_positive_control_without_colon(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "1"],
            json.dumps([{"prompt": "hi", "expect": "bare",
                          "control": "positive"}]))

    def test_timeout_non_integer(self):
        self._assert_refused(
            lambda cases_path: [
                "--cases", cases_path, "--timeout", "abc", "--runs", "1"],
            json.dumps([{"prompt": "hi", "expect": "a:b"}]))

    def test_timeout_less_than_one(self):
        self._assert_refused(
            lambda cases_path: [
                "--cases", cases_path, "--timeout", "0", "--runs", "1"],
            json.dumps([{"prompt": "hi", "expect": "a:b"}]))

    def test_json_parent_directory_missing(self):
        """Midpoint defect: this check used to run only after the whole
        eval, so a bad --json path burned the entire run before failing to
        write. It must be validated up front with every sibling check here,
        exiting 2 and running no subprocess.
        """
        self._assert_refused(
            lambda cases_path: [
                "--cases", cases_path, "--json",
                os.path.join(os.path.dirname(cases_path),
                             "nonexistent_subdir", "report.json"),
                "--runs", "1"],
            json.dumps([{"prompt": "hi", "expect": "a:b"}]))

    def test_runs_non_integer(self):
        """--runs, like --timeout, must reject a non-integer up front,
        running no subprocess."""
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "abc"],
            json.dumps([{"prompt": "hi", "expect": "a:b"}]))

    def test_runs_less_than_one(self):
        self._assert_refused(
            lambda cases_path: ["--cases", cases_path, "--runs", "0"],
            json.dumps([{"prompt": "hi", "expect": "a:b"}]))


class TestCLISentinelHasAPositiveControl(unittest.TestCase):
    """Positive control for every `assertFalse(os.path.exists(sentinel))`
    in TestCLIMalformedInputRunsNothing above (a probe that
    observes nothing passes a does-no-work assertion for free):
    twelve tests assert the sentinel is absent; this one asserts it
    actually appears when a case legitimately runs, proving the sentinel
    mechanism can register a real invocation at all.
    """

    def test_well_formed_input_actually_invokes_the_stub(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-cli-positive-")
        try:
            sentinel = os.path.join(tmp_dir, "sentinel")
            cases_path = os.path.join(tmp_dir, "cases.json")
            with open(cases_path, "w") as f:
                json.dump([{"prompt": "hi", "expect": "a:b"}], f)
            with _stub_claude(_STUB_SENTINEL):
                env = dict(os.environ)
                env["STUB_SENTINEL"] = sentinel
                subprocess.run(
                    [CLI_PATH, "--cases", cases_path, "--timeout", "5",
                     "--runs", "1"],
                    capture_output=True, text=True, env=env, cwd=tmp_dir)
            self.assertTrue(
                os.path.exists(sentinel),
                "a well-formed case file must actually invoke the stub")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestCLIReport(unittest.TestCase):
    def test_three_cases_produce_three_distinct_states(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-report-")
        try:
            cases_path = os.path.join(tmp_dir, "cases.json")
            report_path = os.path.join(tmp_dir, "report.json")
            with open(cases_path, "w") as f:
                json.dump([
                    {"prompt": "MATCH: build this",
                     "expect": "toolkit-build:build"},
                    {"prompt": "COLLISION: whatever",
                     "expect": "toolkit-build:build"},
                    {"prompt": "NOSKILL: something else",
                     "expect": "toolkit-build:build"},
                ], f)
            with _stub_claude(_STUB_MULTI_STATE):
                result = subprocess.run(
                    [CLI_PATH, "--cases", cases_path,
                     "--json", report_path, "--timeout", "10",
                     "--runs", "1"],
                    capture_output=True, text=True, cwd=tmp_dir)
            self.assertTrue(os.path.exists(report_path), msg=result.stderr)
            with open(report_path) as f:
                report = json.load(f)
            states = {c["state"] for c in report["cases"]}
            self.assertEqual(states, {lib.MATCH, lib.COLLISION, lib.NO_SKILL})
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_forbid_case_runs_through_cli_and_reports_forbidden(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-report-")
        try:
            cases_path = os.path.join(tmp_dir, "cases.json")
            report_path = os.path.join(tmp_dir, "report.json")
            with open(cases_path, "w") as f:
                json.dump([
                    {"prompt": "MATCH: build this",
                     "expect": "toolkit-build:build"},
                    {"prompt": "COLLISION: whatever",
                     "forbid": "wrong:skill"},
                ], f)
            with _stub_claude(_STUB_MULTI_STATE):
                result = subprocess.run(
                    [CLI_PATH, "--cases", cases_path,
                     "--json", report_path, "--timeout", "10",
                     "--runs", "1"],
                    capture_output=True, text=True, cwd=tmp_dir)
            with open(report_path) as f:
                report = json.load(f)
            by_prompt = {c["prompt"]: c for c in report["cases"]}
            forbid_case = by_prompt["COLLISION: whatever"]
            self.assertEqual(forbid_case["forbidden"], "wrong:skill")
            self.assertEqual(forbid_case["state"], lib.FORBIDDEN)
            self.assertIsNone(by_prompt["MATCH: build this"]["forbidden"])
            self.assertIn("forbid='wrong:skill'", result.stdout)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_json_report_carries_non_empty_sibling_set(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-report-")
        try:
            cases_path = os.path.join(tmp_dir, "cases.json")
            report_path = os.path.join(tmp_dir, "report.json")
            with open(cases_path, "w") as f:
                json.dump(
                    [{"prompt": "MATCH: build this",
                      "expect": "toolkit-build:build"}], f)
            with _stub_claude(_STUB_MULTI_STATE):
                subprocess.run(
                    [CLI_PATH, "--cases", cases_path,
                     "--json", report_path, "--timeout", "10",
                     "--runs", "1"],
                    capture_output=True, text=True, cwd=tmp_dir)
            with open(report_path) as f:
                report = json.load(f)
            self.assertGreater(len(report["sibling_set"]), 0)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_runner_error_is_a_distinct_fourth_report_state(self):
        """A broken harness (here: `claude` exits non-zero with no events)
        must serialise as RUNNER_ERROR, never as NO_SKILL -- classify() is
        never called for it.
        """
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-runner-error-")
        try:
            cases_path = os.path.join(tmp_dir, "cases.json")
            report_path = os.path.join(tmp_dir, "report.json")
            with open(cases_path, "w") as f:
                json.dump([{"prompt": "irrelevant", "expect": "a:b"}], f)
            with _stub_claude(_STUB_EXIT_NONZERO):
                subprocess.run(
                    [CLI_PATH, "--cases", cases_path,
                     "--json", report_path, "--timeout", "5",
                     "--runs", "1"],
                    capture_output=True, text=True, cwd=tmp_dir)
            with open(report_path) as f:
                report = json.load(f)
            self.assertEqual(report["cases"][0]["state"], "RUNNER_ERROR")
            self.assertIsNotNone(report["cases"][0]["error"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_init_then_exit_nonzero_is_runner_error_not_no_skill(self):
        """A child that emits init and then crashes on its own must
        serialise as RUNNER_ERROR through the full CLI, never as NO_SKILL --
        the crashed-run-as-routing-result defect, exercised end to end.
        """
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-runner-error-")
        try:
            cases_path = os.path.join(tmp_dir, "cases.json")
            report_path = os.path.join(tmp_dir, "report.json")
            with open(cases_path, "w") as f:
                json.dump([{"prompt": "irrelevant", "expect": "a:b"}], f)
            with _stub_claude(_STUB_INIT_THEN_EXIT_1):
                subprocess.run(
                    [CLI_PATH, "--cases", cases_path,
                     "--json", report_path, "--timeout", "5",
                     "--runs", "1"],
                    capture_output=True, text=True, cwd=tmp_dir)
            with open(report_path) as f:
                report = json.load(f)
            self.assertEqual(report["cases"][0]["state"], "RUNNER_ERROR")
            self.assertNotEqual(report["cases"][0]["state"], lib.NO_SKILL)
            self.assertIsNotNone(report["cases"][0]["error"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_no_controls_requested_is_inconclusive_and_nonzero_exit(self):
        """The positive-control contract, asserted here so the control tests are
        not the only guard: with neither control requested, the eval cannot establish
        anything and must not report a pass.
        """
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-verdict-")
        try:
            cases_path = os.path.join(tmp_dir, "cases.json")
            report_path = os.path.join(tmp_dir, "report.json")
            with open(cases_path, "w") as f:
                json.dump(
                    [{"prompt": "MATCH: build this",
                      "expect": "toolkit-build:build"}], f)
            with _stub_claude(_STUB_MULTI_STATE):
                result = subprocess.run(
                    [CLI_PATH, "--cases", cases_path,
                     "--json", report_path, "--timeout", "10",
                     "--runs", "1"],
                    capture_output=True, text=True, cwd=tmp_dir)
            with open(report_path) as f:
                report = json.load(f)
            self.assertEqual(report["verdict"], "INCONCLUSIVE")
            self.assertNotEqual(result.returncode, 0)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestCLIRealpathSymlinkRegression(unittest.TestCase):
    """These tools run through `~/.local/bin` symlinks, and
    `abspath` does not resolve a symlink: it lands on the symlink's own
    directory (which has no skill_trigger_lib.py alongside it here) rather
    than following it to the real tool directory, and the import fails at
    runtime while passing every test that never goes through a symlink.
    """

    def test_invoked_through_a_symlink_still_imports_the_lib_and_runs(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-symlink-")
        try:
            real_exe = os.path.join(TOOL_DIR, "claude-skill-trigger")
            symlink_path = os.path.join(tmp_dir, "claude-skill-trigger")
            os.symlink(real_exe, symlink_path)
            cases_path = os.path.join(tmp_dir, "cases.json")
            with open(cases_path, "w") as f:
                json.dump([{"prompt": "irrelevant", "expect": "a:b"}], f)
            with _stub_claude(_replay_stub("no-skill.jsonl")):
                result = subprocess.run(
                    [symlink_path, "--cases", cases_path, "--timeout", "5",
                     "--runs", "1"],
                    capture_output=True, text=True, cwd=tmp_dir)
            self.assertNotIn("ModuleNotFoundError", result.stderr)
            self.assertNotIn("ImportError", result.stderr)
            self.assertEqual(result.returncode, 3, msg=result.stderr)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# --- the two controls ---------------------------------------------------
#
# Every test in this section sets HOME to a temp sandbox -- never the real
# ~/.claude -- carrying a fake plugin registry and two real skill fixtures
# (a block-scalar description and a single-line one), as the design
# requires. The stub `claude` below reads whichever fake SKILL.md the test
# points it at, on every invocation, and logs what it saw so a test can
# assert what the child actually observed (the same principle as the
# `STUB_ENVFILE` -- inspecting what we intended to write proves nothing
# about what happened).

FAKE_PLUGIN_KEY = "fake@mkt"
FAKE_SKILL_MARKER = "MARKER_WORD_9f3c"

_FAKE_SKILL_MD_BLOCK = """---
name: probe
description: >-
  This is a probe skill for testing purposes.
  MARKER_WORD_9f3c lives right here in the middle of the block scalar.
allowed-tools: []
---

Body content for the probe skill, untouched by any control.
"""

_FAKE_SKILL_MD_SINGLE_LINE = """---
name: probe_single
description: A single-line probe description containing MARKER_WORD_9f3c.
---

Body content for the single-line probe skill.
"""

_FAKE_SKILL_MD_NO_DESCRIPTION = """---
name: no_description
allowed-tools: []
---

Body content, no description key at all.
"""

_FAKE_SKILL_MD_NO_CLOSING_DELIM = """---
name: no_closing
description: has a value but the frontmatter never closes

Body content.
"""


def _write_installed_plugins(path, plugins_map):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"version": 2, "plugins": plugins_map}, f)


def _make_fake_home(base_dir, *, install_version="1.0.0"):
    """Build a fake $HOME carrying a plugin registry and real skill
    fixtures under the `cache/` tree, the way a real `installPath` points.
    Returns a dict of paths so callers only need the keys they use.
    """
    home = os.path.join(base_dir, "home")
    install_path = os.path.join(
        home, ".claude", "plugins", "cache", "fake-mkt", "fake", install_version)
    skills = {
        "probe": _FAKE_SKILL_MD_BLOCK,
        "probe_single": _FAKE_SKILL_MD_SINGLE_LINE,
        "no_description": _FAKE_SKILL_MD_NO_DESCRIPTION,
        "no_closing": _FAKE_SKILL_MD_NO_CLOSING_DELIM,
    }
    skill_md_paths = {}
    for name, content in skills.items():
        skill_dir = os.path.join(install_path, "skills", name)
        os.makedirs(skill_dir)
        md_path = os.path.join(skill_dir, "SKILL.md")
        with open(md_path, "w") as f:
            f.write(content)
        skill_md_paths[name] = md_path

    registry_path = os.path.join(
        home, ".claude", "plugins", "installed_plugins.json")
    _write_installed_plugins(registry_path, {
        FAKE_PLUGIN_KEY: [
            {"scope": "user", "installPath": install_path,
             "version": install_version,
             "installedAt": "2026-01-01T00:00:00Z",
             "lastUpdated": "2026-01-01T00:00:00Z", "gitCommitSha": "abc123"},
        ],
    })
    return {
        "home": home,
        "install_path": install_path,
        "registry_path": registry_path,
        "block_md": skill_md_paths["probe"],
        "single_md": skill_md_paths["probe_single"],
        "no_description_md": skill_md_paths["no_description"],
        "no_closing_md": skill_md_paths["no_closing"],
    }


# The single stub covering every control scenario. Env-var-driven rather than
# templated, since the control tests need many independent axes of behaviour
# (which file to read, whether to ignore content, whether to signal or
# chmod) that a per-fixture string-replace template can't express cleanly.
_STUB_CONTROL = '''#!/usr/bin/env python3
import json, os, signal, sys, time

prompt = sys.argv[sys.argv.index("-p") + 1]

skill_md_path = os.environ["STUB_SKILL_MD_PATH"]
with open(skill_md_path) as f:
    content = f.read()

log_path = os.environ.get("STUB_LOG")
if log_path:
    with open(log_path, "a") as f:
        f.write(content)
        f.write("\\n===\\n")

marker = os.environ.get("STUB_MARKER", "")
is_inert = marker not in content
harness_prompt = os.environ.get("STUB_HARNESS_PROMPT", "")
mode = os.environ.get("STUB_MODE", "content")
expect_skill = os.environ.get("STUB_EXPECT_SKILL", "fake:probe")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()


def _next_sequence_entry():
    """Consume one entry per invocation from STUB_SEQUENCE (comma-
    separated: match/none/other/error), via the same counter-file mechanism
    "only_first_invocation" already uses. Repeats the last entry past the
    end of the list.
    """
    seq = os.environ.get("STUB_SEQUENCE", "").split(",")
    counter_path = os.environ["STUB_COUNTER_FILE"]
    try:
        with open(counter_path) as f:
            count = int(f.read().strip())
    except (OSError, ValueError):
        count = 0
    idx = min(count, len(seq) - 1)
    with open(counter_path, "w") as f:
        f.write(str(count + 1))
    return seq[idx]


if mode == "sequence":
    # Dispatched BEFORE the init line: "error" must exit 0 printing
    # NOTHING at all (not even init), which is what makes run_case see it
    # as RUNNER_ERROR rather than a clean no-skill result.
    _entry = _next_sequence_entry()
    if _entry == "error":
        sys.exit(0)
    emit({"type": "system", "subtype": "init", "skills": [expect_skill],
          "plugins": [], "claude_code_version": "9.9.9"})
    if _entry == "match":
        _emit_skill = expect_skill
    elif _entry == "other":
        _emit_skill = "other:skill"
    else:  # "none"
        _emit_skill = None
    if _emit_skill is not None:
        emit({"type": "stream_event", "event": {
            "type": "content_block_start", "index": 1,
            "content_block": {"type": "tool_use", "name": "Skill",
                               "input": {}}}})
        emit({"type": "stream_event", "event": {
            "type": "content_block_delta", "index": 1,
            "delta": {"type": "input_json_delta",
                      "partial_json": json.dumps({"skill": _emit_skill})}}})
        emit({"type": "stream_event", "event": {
            "type": "content_block_stop", "index": 1}})
    sys.stdout.flush()
    sys.exit(0)

emit({"type": "system", "subtype": "init", "skills": [expect_skill],
      "plugins": [], "claude_code_version": "9.9.9"})

if harness_prompt and prompt == harness_prompt:
    should_emit = True
elif mode == "always_emit":
    should_emit = True
elif mode == "only_first_invocation":
    counter_path = os.environ["STUB_COUNTER_FILE"]
    try:
        with open(counter_path) as f:
            count = int(f.read().strip())
    except (OSError, ValueError):
        count = 0
    count += 1
    with open(counter_path, "w") as f:
        f.write(str(count))
    should_emit = (count == 1)
else:
    should_emit = not is_inert

if should_emit:
    emit({"type": "stream_event", "event": {
        "type": "content_block_start", "index": 1,
        "content_block": {"type": "tool_use", "name": "Skill", "input": {}}}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_delta", "index": 1,
        "delta": {"type": "input_json_delta",
                  "partial_json": json.dumps({"skill": expect_skill})}}})
    emit({"type": "stream_event", "event": {
        "type": "content_block_stop", "index": 1}})

pidfile = os.environ.get("STUB_PIDFILE")
if pidfile and is_inert:
    # Only the gutted invocation -- the one the interrupt lands in. The
    # main-case invocation is killed early by design, and a kill between
    # open("w") and write() left an EMPTY pidfile under load. Atomic
    # write, so a reader can never see a truncated file.
    with open(pidfile + ".tmp", "w") as f:
        f.write(str(os.getpid()))
    os.replace(pidfile + ".tmp", pidfile)

if is_inert:
    signal_parent = os.environ.get("STUB_SIGNAL_PARENT")
    if signal_parent == "sigint":
        os.kill(os.getppid(), signal.SIGINT)
        time.sleep(10)
    elif signal_parent == "sigterm":
        os.kill(os.getppid(), signal.SIGTERM)
        time.sleep(10)
    elif os.environ.get("STUB_CHMOD_ON_INERT"):
        os.chmod(os.path.dirname(skill_md_path), 0o555)

sys.stdout.flush()
'''


def _read_log_entries(log_path):
    """Split `_STUB_CONTROL`'s append-log into one entry per invocation."""
    if not os.path.exists(log_path):
        return []
    with open(log_path) as f:
        raw = f.read()
    return [part for part in raw.split("\n===\n") if part != ""]


def _run_cli(cases, args, *, home, cwd, extra_env=None, stub_script=_STUB_CONTROL):
    """Write `cases` to `<cwd>/cases.json`, run the CLI with `--cases` plus
    `args`, `HOME` set to `home`, and `stub_script` on PATH. Returns
    (CompletedProcess, cases_path).
    """
    cases_path = os.path.join(cwd, "cases.json")
    with open(cases_path, "w") as f:
        json.dump(cases, f)
    with _stub_claude(stub_script):
        # env is built INSIDE the stub context, after PATH has been
        # mutated to put the stub `claude` first -- built outside, the
        # subprocess would inherit the real PATH and never see the stub.
        env = dict(os.environ)
        env["HOME"] = home
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            [CLI_PATH, "--cases", cases_path] + args,
            capture_output=True, text=True, env=env, cwd=cwd,
            preexec_fn=_default_sigint)
    return result, cases_path


def _default_sigint():
    """Give the CLI a DEFAULT SIGINT disposition, as a terminal would.
    MEASURED: a non-interactive shell starts `&` jobs with SIGINT ignored,
    Python keeps an inherited ignore, and the CLI then never saw the
    stub's SIGINT -- the control ran to completion, exited 3, and the
    interrupt test passed without an interrupt ever landing.
    """
    signal.signal(signal.SIGINT, signal.SIG_DFL)


class TestPositiveControlRegistryAndFrontmatterValidation(unittest.TestCase):
    """Every scenario here must exit 2, edit nothing, and invoke no
    `claude` -- proven with a sentinel stub, matching
    TestCLIMalformedInputRunsNothing's pattern for the CLI tests.
    """

    def _assert_refused(self, home, expect, *, cwd=None, unchanged_path=None):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-registry-")
        try:
            sentinel = os.path.join(tmp_dir, "sentinel")
            run_cwd = cwd or tmp_dir
            unchanged_before = None
            if unchanged_path:
                with open(unchanged_path, "rb") as f:
                    unchanged_before = f.read()
            result, _ = _run_cli(
                [{"prompt": "irrelevant", "expect": expect,
                  "control": "positive"}],
                ["--positive-control", "--timeout", "5", "--runs", "1"],
                home=home, cwd=run_cwd,
                extra_env={"STUB_SENTINEL": sentinel},
                stub_script=_STUB_SENTINEL)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertFalse(
                os.path.exists(sentinel),
                "a resolution/frontmatter defect must run no subprocess")
            if unchanged_path:
                with open(unchanged_path, "rb") as f:
                    unchanged_after = f.read()
                self.assertEqual(
                    unchanged_before, unchanged_after,
                    "a defect must never touch any SKILL.md")
            return result
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_no_matching_plugin_key(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-home-")
        try:
            fx = _make_fake_home(tmp_dir)
            result = self._assert_refused(
                fx["home"], "ghost:probe", unchanged_path=fx["block_md"])
            self.assertIn("ghost", result.stderr)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_two_matching_plugin_keys(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-home-")
        try:
            fx = _make_fake_home(tmp_dir)
            _write_installed_plugins(fx["registry_path"], {
                "fake@a": [{"scope": "user", "installPath": fx["install_path"]}],
                "fake@b": [{"scope": "user", "installPath": fx["install_path"]}],
            })
            result = self._assert_refused(
                fx["home"], "fake:probe", unchanged_path=fx["block_md"])
            self.assertIn("fake@a", result.stderr)
            self.assertIn("fake@b", result.stderr)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_project_pin_conflicts_with_cwd(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-home-")
        try:
            fx = _make_fake_home(tmp_dir)
            project_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-project-")
            try:
                project_install_path = os.path.join(tmp_dir, "project-install")
                os.makedirs(project_install_path)
                _write_installed_plugins(fx["registry_path"], {
                    FAKE_PLUGIN_KEY: [
                        {"scope": "user", "installPath": fx["install_path"]},
                        {"scope": "project", "projectPath": project_dir,
                         "installPath": project_install_path},
                    ],
                })
                result = self._assert_refused(
                    fx["home"], "fake:probe", cwd=project_dir,
                    unchanged_path=fx["block_md"])
                self.assertIn(project_install_path, result.stderr)
                self.assertIn(fx["install_path"], result.stderr)
            finally:
                shutil.rmtree(project_dir, ignore_errors=True)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_skill_md_absent_at_install_path(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-home-")
        try:
            fx = _make_fake_home(tmp_dir)
            result = self._assert_refused(fx["home"], "fake:ghost_skill")
            self.assertIn("ghost_skill", result.stderr)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_no_description_key(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-home-")
        try:
            fx = _make_fake_home(tmp_dir)
            self._assert_refused(
                fx["home"], "fake:no_description",
                unchanged_path=fx["no_description_md"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_no_closing_frontmatter_delimiter(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-home-")
        try:
            fx = _make_fake_home(tmp_dir)
            self._assert_refused(
                fx["home"], "fake:no_closing",
                unchanged_path=fx["no_closing_md"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestPositiveControlFlagValidation(unittest.TestCase):
    """--positive-control's own preconditions, independent of registry
    resolution: at least one positive case, and exactly one distinct
    `expect` among them.
    """

    def test_no_positive_control_case(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-flag-")
        try:
            fx = _make_fake_home(tmp_dir)
            sentinel = os.path.join(tmp_dir, "sentinel")
            result, _ = _run_cli(
                [{"prompt": "irrelevant", "expect": "fake:probe"}],
                ["--positive-control", "--timeout", "5", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                extra_env={"STUB_SENTINEL": sentinel},
                stub_script=_STUB_SENTINEL)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertFalse(os.path.exists(sentinel))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_two_distinct_positive_expects(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-flag-")
        try:
            fx = _make_fake_home(tmp_dir)
            sentinel = os.path.join(tmp_dir, "sentinel")
            result, _ = _run_cli(
                [{"prompt": "a", "expect": "fake:probe", "control": "positive"},
                 {"prompt": "b", "expect": "fake:probe_single",
                  "control": "positive"}],
                ["--positive-control", "--timeout", "5", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                extra_env={"STUB_SENTINEL": sentinel},
                stub_script=_STUB_SENTINEL)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertFalse(os.path.exists(sentinel))
            self.assertIn("fake:probe", result.stderr)
            self.assertIn("fake:probe_single", result.stderr)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestPositiveControlHappyPath(unittest.TestCase):
    """The core positive-control contract, against both
    description shapes (a `>-` block scalar and a plain single-line
    value).
    """

    def _run_happy_path(self, tmp_dir, fx, skill_md_path, expect_skill):
        """Run the positive control against `skill_md_path`, which must be
        one of the SKILL.md files inside `fx` (the same fixture `home`
        points at) -- a stub pointed at any other file would never see the
        gut/restore this test is exercising.
        """
        with open(skill_md_path, "rb") as f:
            original_bytes = f.read()
        log_path = os.path.join(tmp_dir, "stub.log")
        report_path = os.path.join(tmp_dir, "report.json")
        result, _ = _run_cli(
            [{"prompt": "build this", "expect": expect_skill,
              "control": "positive"}],
            ["--positive-control", "--json", report_path,
             "--timeout", "10", "--runs", "1"],
            home=fx["home"], cwd=tmp_dir,
            extra_env={
                "STUB_SKILL_MD_PATH": skill_md_path,
                "STUB_LOG": log_path,
                "STUB_MARKER": FAKE_SKILL_MARKER,
                "STUB_EXPECT_SKILL": expect_skill,
            })
        self.assertEqual(result.returncode, 3, msg=result.stderr)

        with open(skill_md_path, "rb") as f:
            after_bytes = f.read()
        self.assertEqual(
            original_bytes, after_bytes,
            "the file must be byte-identical after a verified restore")
        self.assertFalse(os.path.exists(skill_md_path + lib.BACKUP_SUFFIX))

        with open(report_path) as f:
            report = json.load(f)
        positive = report["controls"]["positive"]
        # Gutted state is reported as counts; at --runs 1 this is the
        # same property: the one gutted run did not route to the skill.
        self.assertEqual(positive["gutted_matches"], 0)
        self.assertTrue(positive["collapsed"])
        self.assertTrue(positive["restored_verified"])
        self.assertTrue(positive["reproved"])
        self.assertEqual(positive["case_index"], 0)

        entries = _read_log_entries(log_path)
        inert_entries = [e for e in entries if lib.INERT_DESCRIPTION in e]
        self.assertEqual(
            len(inert_entries), 1,
            "the child must observe the inert description on exactly "
            "one invocation -- the gutted run, and no other")
        return entries

    def test_block_scalar_description_happy_path(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-happy-block-")
        try:
            fx = _make_fake_home(tmp_dir)
            self._run_happy_path(tmp_dir, fx, fx["block_md"], "fake:probe")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_single_line_description_happy_path(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-happy-single-")
        try:
            fx = _make_fake_home(tmp_dir)
            entries = self._run_happy_path(
                tmp_dir, fx, fx["single_md"], "fake:probe_single")
            inert_entry = next(e for e in entries if lib.INERT_DESCRIPTION in e)
            self.assertIn("name: probe_single", inert_entry)
            self.assertIn(
                "Body content for the single-line probe skill.", inert_entry)
            self.assertNotIn(FAKE_SKILL_MARKER, inert_entry)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestPositiveControlCollapseAndReprove(unittest.TestCase):
    """The two failure shapes of the control itself: a description that
    never actually gates the route, and a restore that succeeds but whose
    reprove still fails.
    """

    def test_never_collapses(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-nocollapse-")
        try:
            fx = _make_fake_home(tmp_dir)
            report_path = os.path.join(tmp_dir, "report.json")
            result, _ = _run_cli(
                [{"prompt": "build this", "expect": "fake:probe",
                  "control": "positive"}],
                ["--positive-control", "--harness-control",
                 "--json", report_path, "--timeout", "10", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                extra_env={
                    "STUB_SKILL_MD_PATH": fx["block_md"],
                    "STUB_MARKER": FAKE_SKILL_MARKER,
                    "STUB_MODE": "always_emit",
                    "STUB_HARNESS_PROMPT": lib.HARNESS_PROMPT,
                })
            self.assertEqual(result.returncode, 3, msg=result.stderr)
            with open(report_path) as f:
                report = json.load(f)
            self.assertEqual(report["verdict"], "INCONCLUSIVE")
            self.assertFalse(report["controls"]["positive"]["collapsed"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_collapses_but_does_not_reprove(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-noreprove-")
        try:
            fx = _make_fake_home(tmp_dir)
            report_path = os.path.join(tmp_dir, "report.json")
            counter_path = os.path.join(tmp_dir, "counter")
            result, _ = _run_cli(
                [{"prompt": "build this", "expect": "fake:probe",
                  "control": "positive"}],
                ["--positive-control", "--harness-control",
                 "--json", report_path, "--timeout", "10", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                extra_env={
                    "STUB_SKILL_MD_PATH": fx["block_md"],
                    "STUB_MARKER": FAKE_SKILL_MARKER,
                    "STUB_MODE": "only_first_invocation",
                    "STUB_COUNTER_FILE": counter_path,
                    "STUB_HARNESS_PROMPT": lib.HARNESS_PROMPT,
                })
            self.assertEqual(result.returncode, 3, msg=result.stderr)
            with open(report_path) as f:
                report = json.load(f)
            self.assertEqual(report["verdict"], "INCONCLUSIVE")
            self.assertFalse(report["controls"]["positive"]["reproved"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestPositiveControlInterrupt(unittest.TestCase):
    """SIGINT and SIGTERM mid-control: the file must end up byte-identical,
    the sidecar gone, and the stub's whole process group gone -- proving
    the `finally` fix (`_kill_process_group` moved out of the read
    loop's normal fall-through path).
    """

    def _run_interrupted(self, signal_name):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-interrupt-")
        try:
            fx = _make_fake_home(tmp_dir)
            with open(fx["block_md"], "rb") as f:
                original_bytes = f.read()
            pidfile = os.path.join(tmp_dir, "pid")
            log_path = os.path.join(tmp_dir, "log")
            result, _ = _run_cli(
                [{"prompt": "build this", "expect": "fake:probe",
                  "control": "positive"}],
                ["--positive-control", "--timeout", "10", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                extra_env={
                    "STUB_SKILL_MD_PATH": fx["block_md"],
                    "STUB_MARKER": FAKE_SKILL_MARKER,
                    "STUB_SIGNAL_PARENT": signal_name,
                    "STUB_PIDFILE": pidfile,
                    "STUB_LOG": log_path,
                })
            # The EXACT signal death, not merely non-zero: with no harness
            # control this run exits 3 anyway, so "non-zero" passes even if
            # the interrupt never landed.
            expected_rc = -(signal.SIGINT if signal_name == "sigint"
                            else signal.SIGTERM)
            self.assertEqual(result.returncode, expected_rc, msg=result.stderr)
            # Main case + gutted run, and NO reprove: the interrupt cut the
            # control short, which is the scenario under test.
            self.assertEqual(
                len(_read_log_entries(log_path)), 2, msg=result.stderr)

            with open(fx["block_md"], "rb") as f:
                after_bytes = f.read()
            self.assertEqual(
                original_bytes, after_bytes,
                "an interrupted control must still leave the file restored")
            self.assertFalse(os.path.exists(fx["block_md"] + lib.BACKUP_SUFFIX))

            self.assertTrue(os.path.exists(pidfile), msg=result.stderr)
            with open(pidfile) as f:
                pgid = int(f.read().strip())
            with self.assertRaises(ProcessLookupError):
                os.killpg(pgid, 0)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_sigint_mid_control(self):
        self._run_interrupted("sigint")

    def test_sigterm_mid_control(self):
        self._run_interrupted("sigterm")


# Writes its pid, THEN signals its parent with SIGINT, then hangs. The parent
# is the driver below, never the test runner.
_STUB_PIDFILE_THEN_SIGINT_PARENT = '''#!/usr/bin/env python3
import os, signal, time
pidfile = os.environ["STUB_PIDFILE"]
maskfile = os.environ.get("STUB_MASKFILE")
if maskfile:
    # The mask this process started with -- a parent that held interrupts
    # for the launch must not have passed that on.
    blocked = signal.pthread_sigmask(signal.SIG_BLOCK, [])
    with open(maskfile, "w") as f:
        f.write(" ".join(str(int(s)) for s in sorted(blocked)))
with open(pidfile + ".tmp", "w") as f:
    f.write(str(os.getpid()))
os.replace(pidfile + ".tmp", pidfile)
if os.environ.get("STUB_SIGINT_PARENT"):
    os.kill(os.getppid(), signal.SIGINT)
time.sleep(10)
'''

# Calls `run_case` with one stage held open until the stub has started, so the
# interrupt lands exactly where the test says. Prints what it observed; an
# uncaught KeyboardInterrupt ends it by SIGINT, as it ends the CLI.
_RUN_CASE_DRIVER = '''
import os, queue, signal, sys, threading, time
sys.path.insert(0, sys.argv[1])
import skill_trigger_lib as lib
mode, pidfile = sys.argv[2], os.environ["STUB_PIDFILE"]

def wait_for_stub():
    deadline = time.monotonic() + 20
    while not os.path.exists(pidfile) and time.monotonic() < deadline:
        time.sleep(0.01)

if mode == "interrupt_before_read_loop":
    original_start = threading.Thread.start
    def held_start(self):
        # The reader thread starting is the last step before the read loop.
        # A loaded machine can stall here; this holds it until the stub has
        # signalled, so the SIGINT lands in that gap.
        print("HELD", flush=True)
        wait_for_stub()
        time.sleep(2)
        return original_start(self)
    threading.Thread.start = held_start
    try:
        lib.run_case("irrelevant prompt", timeout_s=15)
        print("NO_INTERRUPT", flush=True)
    except KeyboardInterrupt:
        print("INTERRUPTED", flush=True)
elif mode == "interrupt_during_launch":
    # A REAL SIGINT the moment the child exists, before `Popen()` has
    # returned to the assignment. A raised KeyboardInterrupt would bypass a
    # signal mask entirely and test nothing.
    original_popen = lib.subprocess.Popen
    def launch_then_sigint(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        wait_for_stub()
        print("LAUNCHED", flush=True)
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(2)  # delivered here unless held
        return proc
    lib.subprocess.Popen = launch_then_sigint
    try:
        lib.run_case("irrelevant prompt", timeout_s=15)
        print("NO_INTERRUPT", flush=True)
    except KeyboardInterrupt:
        print("INTERRUPTED", flush=True)
elif mode == "no_interrupt":
    lib.run_case("irrelevant prompt", timeout_s=1)
    print("DONE", flush=True)
else:
    # "lock_error" / "plain_error": the 3.9 shape -- Queue.get raising
    # RuntimeError("release unlocked lock") during handling of an interrupt
    # -- or the same RuntimeError with an ordinary exception as context.
    def failing_get(self, block=True, timeout=None):
        wait_for_stub()
        try:
            raise (KeyboardInterrupt() if mode == "lock_error" else ValueError("x"))
        except BaseException:
            raise RuntimeError("release unlocked lock")
    queue.Queue.get = failing_get
    lib.run_case("irrelevant prompt", timeout_s=15)
'''


class TestRunCaseInterruptOutsideTheReadLoop(unittest.TestCase):
    """Two interrupt paths that the `test_sig*_mid_control`
    tests above hit only by chance under load, pinned deterministically here.
    Each runs `run_case` in a driver process, never in the test runner."""

    def _drive(self, mode, sigint_parent, maskfile=None):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-row58-")
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        driver = os.path.join(tmp_dir, "driver.py")
        with open(driver, "w") as f:
            f.write(_RUN_CASE_DRIVER)
        pidfile = os.path.join(tmp_dir, "pid")
        with _stub_claude(_STUB_PIDFILE_THEN_SIGINT_PARENT):
            env = dict(os.environ, STUB_PIDFILE=pidfile)
            if maskfile:
                env["STUB_MASKFILE"] = maskfile
            if sigint_parent:
                env["STUB_SIGINT_PARENT"] = "1"
            result = subprocess.run(
                [sys.executable, driver, TOOL_DIR, mode],
                capture_output=True, text=True, env=env, timeout=60,
                preexec_fn=_default_sigint)
        self.assertTrue(os.path.exists(pidfile), msg=result.stderr)
        with open(pidfile) as f:
            pgid = int(f.read().strip())
        return result, pgid

    def _assert_group_gone(self, pgid, result):
        try:
            os.killpg(pgid, 0)
        except (ProcessLookupError, PermissionError):
            return  # gone (Darwin reports a reaped-but-orphaned group as EPERM)
        os.killpg(pgid, signal.SIGKILL)  # don't leave it for 10s
        self.fail("claude's process group outlived the interrupt: %s" % result.stderr)

    def test_sigint_before_the_read_loop_still_kills_the_child(self):
        result, pgid = self._drive("interrupt_before_read_loop", sigint_parent=True)
        # The interrupt LANDED, in the held gap -- not merely "non-zero".
        self.assertEqual(result.stdout.split(), ["HELD", "INTERRUPTED"],
                         msg=result.stderr)
        self._assert_group_gone(pgid, result)

    @unittest.skipUnless(hasattr(signal, "pthread_sigmask"), "POSIX signal masks")
    def test_sigint_during_the_launch_still_kills_the_child(self):
        # Code Review's window: the child exists, `proc` is not yet assigned.
        result, pgid = self._drive("interrupt_during_launch", sigint_parent=False)
        self.assertEqual(result.stdout.split(), ["LAUNCHED", "INTERRUPTED"],
                         msg=result.stderr)
        self._assert_group_gone(pgid, result)

    @unittest.skipUnless(hasattr(signal, "pthread_sigmask"), "POSIX signal masks")
    def test_the_child_does_not_inherit_held_interrupts(self):
        # Held for the launch, SIGINT/SIGTERM would stay blocked through
        # exec -- and `_kill_process_group`'s SIGTERM would not land.
        maskfile = os.path.join(tempfile.mkdtemp(prefix="skill-trigger-mask-"), "mask")
        self.addCleanup(shutil.rmtree, os.path.dirname(maskfile), ignore_errors=True)
        result, pgid = self._drive("no_interrupt", sigint_parent=False,
                                   maskfile=maskfile)
        self.assertEqual(result.stdout.split(), ["DONE"], msg=result.stderr)
        with open(maskfile) as f:
            blocked = {int(s) for s in f.read().split()}
        self.assertNotIn(int(signal.SIGINT), blocked)
        self.assertNotIn(int(signal.SIGTERM), blocked)
        self._assert_group_gone(pgid, result)

    def test_lock_error_during_an_interrupt_exits_as_the_interrupt(self):
        result, pgid = self._drive("lock_error", sigint_parent=False)
        self.assertEqual(result.returncode, -signal.SIGINT, msg=result.stderr)
        self.assertNotIn("RuntimeError", result.stderr)
        self._assert_group_gone(pgid, result)

    def test_lock_error_without_an_interrupt_is_not_disguised(self):
        # Positive control for the one above: only an interrupt context is
        # re-raised. A RuntimeError over an ordinary exception stays itself.
        result, pgid = self._drive("plain_error", sigint_parent=False)
        self.assertEqual(result.returncode, 1, msg=result.stderr)
        self.assertIn("RuntimeError: release unlocked lock", result.stderr)
        self._assert_group_gone(pgid, result)


class TestPositiveControlReadOnlyRestore(unittest.TestCase):
    """The restore's failure path: the stub makes the skill's directory
    read-only the moment it sees the inert description, so the in-place
    atomic write in `run_positive_control`'s `finally` cannot create its
    temp file. Exit 4, the sidecar left in place, and -- after permissions
    are restored -- a second run's leftover-backup scan picks it up.
    """

    def test_read_only_directory_then_leftover_scan_recovers(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-readonly-")
        skill_dir = None
        try:
            fx = _make_fake_home(tmp_dir)
            skill_dir = os.path.dirname(fx["block_md"])
            with open(fx["block_md"], "rb") as f:
                original_bytes = f.read()

            result, _ = _run_cli(
                [{"prompt": "build this", "expect": "fake:probe",
                  "control": "positive"}],
                ["--positive-control", "--timeout", "10", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                extra_env={
                    "STUB_SKILL_MD_PATH": fx["block_md"],
                    "STUB_MARKER": FAKE_SKILL_MARKER,
                    "STUB_CHMOD_ON_INERT": "1",
                })
            self.assertEqual(result.returncode, 4, msg=result.stderr)
            self.assertIn(fx["block_md"], result.stderr)
            self.assertTrue(os.path.exists(fx["block_md"] + lib.BACKUP_SUFFIX))

            # Restore permissions -- required before the fixture can be
            # cleaned up at all, per the read-only-directory test contract.
            os.chmod(skill_dir, 0o755)

            second_result, _ = _run_cli(
                [{"prompt": "build this", "expect": "fake:probe"}],
                ["--timeout", "5", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                stub_script=_STUB_SENTINEL,
                extra_env={"STUB_SENTINEL": os.path.join(tmp_dir, "sentinel2")})
            self.assertEqual(second_result.returncode, 3, msg=second_result.stderr)
            self.assertIn("restored", second_result.stderr)
            self.assertFalse(
                os.path.exists(os.path.join(tmp_dir, "sentinel2")),
                "the leftover scan must run no cases")
            self.assertFalse(os.path.exists(fx["block_md"] + lib.BACKUP_SUFFIX))
            with open(fx["block_md"], "rb") as f:
                final_bytes = f.read()
            self.assertEqual(original_bytes, final_bytes)
        finally:
            if skill_dir and os.path.isdir(skill_dir):
                os.chmod(skill_dir, 0o755)
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestLeftoverBackupScan(unittest.TestCase):
    """The unconditional startup scan (main's step 2), against a
    backup planted directly under `marketplaces/` -- the root the control's own
    resolver never writes to, proving the scan covers it anyway.
    """

    def test_planted_backup_under_marketplaces_is_restored(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-leftover-")
        try:
            fx = _make_fake_home(tmp_dir)
            marketplaces_dir = os.path.join(
                fx["home"], ".claude", "plugins", "marketplaces", "some-mkt")
            os.makedirs(marketplaces_dir)
            target = os.path.join(marketplaces_dir, "SKILL.md")
            original_bytes = b"---\nname: x\ndescription: original\n---\nbody\n"
            gutted_bytes = b"---\nname: x\ndescription: gutted\n---\nbody\n"
            with open(target, "wb") as f:
                f.write(gutted_bytes)
            with open(target + lib.BACKUP_SUFFIX, "wb") as f:
                f.write(original_bytes)

            sentinel = os.path.join(tmp_dir, "sentinel")
            result, _ = _run_cli(
                [{"prompt": "irrelevant", "expect": "a:b"}],
                ["--timeout", "5", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                stub_script=_STUB_SENTINEL,
                extra_env={"STUB_SENTINEL": sentinel})

            self.assertEqual(result.returncode, 3, msg=result.stderr)
            self.assertIn("restored", result.stderr)
            self.assertFalse(os.path.exists(sentinel))
            self.assertFalse(os.path.exists(target + lib.BACKUP_SUFFIX))
            with open(target, "rb") as f:
                restored = f.read()
            self.assertEqual(restored, original_bytes)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestLeftoverBackupScanZeroByteRefusal(unittest.TestCase):
    """The create-before-read defect: a crash between sidecar creation and
    the original write used to leave a ZERO-BYTE sidecar behind, which the
    scan would then happily "restore" -- silently truncating the real,
    disarmed skill file to nothing. Neither shape may ever be restored or
    treated as a real backup.
    """

    def test_zero_byte_backup_is_never_restored_and_exits_4(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-zerobyte-")
        try:
            fx = _make_fake_home(tmp_dir)
            target = fx["block_md"]
            with open(target, "rb") as f:
                original_bytes = f.read()
            backup_path = target + lib.BACKUP_SUFFIX
            with open(backup_path, "wb"):
                pass  # zero bytes -- the exact create-before-read shape

            sentinel = os.path.join(tmp_dir, "sentinel")
            result, _ = _run_cli(
                [{"prompt": "irrelevant", "expect": "fake:probe"}],
                ["--timeout", "5", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                stub_script=_STUB_SENTINEL,
                extra_env={"STUB_SENTINEL": sentinel})

            self.assertEqual(result.returncode, 4, msg=result.stderr)
            self.assertIn("empty", result.stderr.lower())
            self.assertIn(target, result.stderr)
            self.assertIn(backup_path, result.stderr)
            self.assertFalse(
                os.path.exists(sentinel),
                "an empty backup must run no cases")
            self.assertTrue(
                os.path.exists(backup_path),
                "the sidecar must be left in place, not consumed")
            with open(target, "rb") as f:
                after_bytes = f.read()
            self.assertEqual(
                original_bytes, after_bytes,
                "the real skill file must never be truncated")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_stray_tmp_file_is_ignored(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-straytmp-")
        try:
            fx = _make_fake_home(tmp_dir)
            target = fx["block_md"]
            with open(target, "rb") as f:
                original_bytes = f.read()
            # Never ends in BACKUP_SUFFIX -- the scan must not treat this as
            # a sidecar at all.
            stray_tmp = target + lib.BACKUP_SUFFIX + ".tmp-12345"
            with open(stray_tmp, "wb") as f:
                f.write(b"leftover temp bytes")

            sentinel = os.path.join(tmp_dir, "sentinel")
            result, _ = _run_cli(
                [{"prompt": "irrelevant", "expect": "fake:probe"}],
                ["--timeout", "5", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                stub_script=_STUB_SENTINEL,
                extra_env={"STUB_SENTINEL": sentinel})

            # Normal behaviour: the scan restores nothing, so the case runs
            # as usual -- and the sentinel-only stub emits no init event, so
            # the single case comes back RUNNER_ERROR and the verdict is
            # INCONCLUSIVE, exit 3, same as every other sentinel-stub run in
            # this file.
            self.assertEqual(result.returncode, 3, msg=result.stderr)
            self.assertNotIn("restored", result.stderr)
            self.assertTrue(os.path.exists(sentinel))
            self.assertTrue(os.path.exists(stray_tmp))
            with open(stray_tmp, "rb") as f:
                self.assertEqual(f.read(), b"leftover temp bytes")
            with open(target, "rb") as f:
                self.assertEqual(f.read(), original_bytes)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestRunPositiveControlBackupCreationDurability(unittest.TestCase):
    """Direct, in-process calls to `run_positive_control` -- these two
    scenarios are NOT reachable through the full CLI, because `main`'s
    unconditional leftover-backup scan runs before `run_positive_control`
    and would already have claimed (or refused on) any planted sidecar
    before this function ever ran.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-durability-")
        self.fx = _make_fake_home(self.tmp_dir)
        self._restore_perms = []

    def tearDown(self):
        for path, mode in self._restore_perms:
            try:
                os.chmod(path, mode)
            except OSError:
                pass
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @staticmethod
    def _case():
        return {"prompt": "build this", "expect": "fake:probe",
                "control": "positive"}

    @staticmethod
    def _tmp_leftovers(skill_md_path):
        directory = os.path.dirname(skill_md_path)
        return [n for n in os.listdir(directory) if ".tmp-" in n]

    def test_backup_already_exists_raises_control_error_and_leaves_it_unchanged(self):
        """The pre-existing `ControlError` guard, exercised directly: this
        had no test naming it before now.
        """
        skill_md_path = self.fx["block_md"]
        backup_path = skill_md_path + lib.BACKUP_SUFFIX
        with open(skill_md_path, "rb") as f:
            original_bytes = f.read()
        with open(backup_path, "wb") as f:
            f.write(b"pre-existing backup contents")

        with self.assertRaises(lib.ControlError):
            lib.run_positive_control(
                self._case(), 0, skill_md_path,
                timeout_s=5, runs=1, baseline_matches=1)

        with open(backup_path, "rb") as f:
            self.assertEqual(f.read(), b"pre-existing backup contents")
        with open(skill_md_path, "rb") as f:
            self.assertEqual(f.read(), original_bytes)
        self.assertEqual(self._tmp_leftovers(skill_md_path), [])

    def test_read_failure_leaves_no_backup_and_no_tmp_file(self):
        """Before the fix, the sidecar was created FIRST and this read
        happened second -- so a read failure here left a zero-byte sidecar
        behind. This is the red this task's fix closes.
        """
        skill_md_path = self.fx["block_md"]
        backup_path = skill_md_path + lib.BACKUP_SUFFIX
        os.chmod(skill_md_path, 0o000)
        self._restore_perms.append((skill_md_path, 0o644))

        with self.assertRaises(OSError):
            lib.run_positive_control(
                self._case(), 0, skill_md_path,
                timeout_s=5, runs=1, baseline_matches=1)

        self.assertFalse(
            os.path.exists(backup_path),
            "a read failure must never leave a (zero-byte) sidecar behind")
        self.assertEqual(self._tmp_leftovers(skill_md_path), [])

    def test_a_sigterm_during_the_restore_still_restores_then_re_signals(self):
        """A SIGTERM that lands while the finally-block is restoring the file
        used to raise out of the restore, leaving the skill GUTTED on disk
        and the process alive. It must finish the restore, byte-verified,
        remove the sidecar, and only then re-signal itself."""
        skill_md_path = self.fx["block_md"]
        backup_path = skill_md_path + lib.BACKUP_SUFFIX
        with open(skill_md_path, "rb") as f:
            original_bytes = f.read()

        real_write = lib._atomic_write
        signals_raised = []

        def write_then_maybe_signal(path, data):
            if data == original_bytes and not signals_raised:
                # The restore write. raise_signal, not os.kill: os.kill is
                # patched below (globally, since lib.os IS os).
                signals_raised.append(True)
                signal.raise_signal(signal.SIGTERM)
            real_write(path, data)

        kills = []
        fake_run = lib.CaseRun(observed=None, init=None, status="OK",
                               error=None)
        saved_handler = signal.getsignal(signal.SIGTERM)
        try:
            with mock.patch.object(lib, "_atomic_write",
                                   side_effect=write_then_maybe_signal), \
                    mock.patch.object(lib, "run_case",
                                      return_value=fake_run), \
                    mock.patch.object(lib.os, "kill",
                                      side_effect=lambda pid, sig:
                                      kills.append((pid, sig))):
                result = lib.run_positive_control(
                    self._case(), 0, skill_md_path,
                    timeout_s=5, runs=1, baseline_matches=1)
        finally:
            # The re-signal path installs SIG_DFL before its (recorded) kill.
            signal.signal(signal.SIGTERM, saved_handler)

        self.assertEqual(signals_raised, [True],
                         "the mid-restore SIGTERM never fired")
        with open(skill_md_path, "rb") as f:
            self.assertEqual(f.read(), original_bytes)
        self.assertTrue(result["restored_verified"])
        self.assertFalse(os.path.exists(backup_path))
        self.assertIn((os.getpid(), signal.SIGTERM), kills,
                      "a SIGTERM seen during the restore was never re-sent")


class TestGutDescriptionRequiresAnOpeningDelimiter(unittest.TestCase):
    """The opening-`---` check had no test. These inputs carry a later `---`
    with a description above it, so WITHOUT the opening check the closing-
    delimiter scan would accept them and gut a file that has no frontmatter
    at all. Only the opening check can refuse them."""

    def _assert_refused_by_the_opening_check(self, raw):
        with self.assertRaises(lib.FrontmatterError) as ctx:
            lib.gut_description(raw, "probe/SKILL.md")
        self.assertIn("must begin with a line exactly '---'",
                      str(ctx.exception))

    def test_a_file_whose_first_line_is_not_the_delimiter_is_refused(self):
        self._assert_refused_by_the_opening_check(
            b"name: x\ndescription: y\n---\nbody\n")

    def test_a_leading_blank_line_is_refused(self):
        self._assert_refused_by_the_opening_check(
            b"\nname: x\ndescription: y\n---\nbody\n")

    def test_a_well_formed_file_still_guts(self):
        """Positive control: the refusal is about the delimiter, not a
        blanket failure on these bodies."""
        out = lib.gut_description(
            b"---\nname: x\ndescription: y\n---\nbody\n", "probe/SKILL.md")
        self.assertIn(lib.INERT_DESCRIPTION.encode("utf-8"), out)


# --- the harness control, and the truth table's rows --------------------

_STUB_HARNESS_SKILL = '''#!/usr/bin/env python3
import json, sys

def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")

emit({"type": "system", "subtype": "init", "skills": ["toolkit-build:build"],
      "plugins": [], "claude_code_version": "9.9.9"})
emit({"type": "stream_event", "event": {
    "type": "content_block_start", "index": 1,
    "content_block": {"type": "tool_use", "name": "Skill", "input": {}}}})
emit({"type": "stream_event", "event": {
    "type": "content_block_delta", "index": 1,
    "delta": {"type": "input_json_delta",
              "partial_json": json.dumps({"skill": "toolkit-build:build"})}}})
emit({"type": "stream_event", "event": {
    "type": "content_block_stop", "index": 1}})
sys.stdout.flush()
'''

_STUB_HARNESS_NO_SKILL = '''#!/usr/bin/env python3
import json, sys
sys.stdout.write(json.dumps({"type": "system", "subtype": "init",
                              "skills": [], "plugins": [],
                              "claude_code_version": "9.9.9"}) + "\\n")
sys.stdout.flush()
'''

_STUB_HARNESS_EXITS_ZERO_NO_OUTPUT = '''#!/usr/bin/env python3
import sys
sys.exit(0)
'''


class TestRunHarnessControl(unittest.TestCase):
    """`run_harness_control` does not compare `observed` to any name --
    which skill fires depends on the sibling set -- it only asks whether A
    Skill call happened at all.
    """

    def test_skill_emitting_stub_produces_true(self):
        with _stub_claude(_STUB_HARNESS_SKILL):
            result = lib.run_harness_control(timeout_s=10, runs=1)
        self.assertTrue(result["ran"])
        self.assertTrue(result["produced_skill_call"])
        self.assertEqual(result["observed"], "toolkit-build:build")

    def test_init_but_no_skill_call_produces_false(self):
        with _stub_claude(_STUB_HARNESS_NO_SKILL):
            result = lib.run_harness_control(timeout_s=10, runs=1)
        self.assertTrue(result["ran"])
        self.assertFalse(result["produced_skill_call"])

    def test_exits_zero_with_no_output_produces_false(self):
        with _stub_claude(_STUB_HARNESS_EXITS_ZERO_NO_OUTPUT):
            result = lib.run_harness_control(timeout_s=5, runs=1)
        self.assertTrue(result["ran"])
        self.assertFalse(result["produced_skill_call"])


class TestComputeVerdictTruthTable(unittest.TestCase):
    """Direct, fast coverage of every row of the truth table. `cases` here is a
    list of already-built case-report dicts (`build_case_report`'s shape), never
    raw `CaseRun`s.
    """

    def _case(self, state):
        return {"prompt": "p", "expected": "a:b", "observed": None,
                "state": state, "error": None, "control": None}

    def _good_positive(self):
        return {"ran": True, "skill": "a:b", "path_edited": "/x",
                "case_index": 0, "runs": 1, "baseline_matches": 1,
                "gutted_matches": 0, "reprove_matches": 1,
                "collapsed": True, "restored_verified": True,
                "reproved": True}

    def _good_harness(self):
        return {"ran": True, "observed": "a:b", "produced_skill_call": True}

    def test_row1_leftover_backup_wins_over_everything(self):
        verdict, code = lib.compute_verdict(
            {"positive": None, "harness": None}, [], leftover_backup=True)
        self.assertEqual((verdict, code), ("INCONCLUSIVE", 3))

    def test_row2_failed_restore_beats_absent_harness(self):
        positive = self._good_positive()
        positive["restored_verified"] = False
        verdict, code = lib.compute_verdict(
            {"positive": positive, "harness": None}, [self._case(lib.MATCH)])
        self.assertEqual((verdict, code), ("INCONCLUSIVE", 4))

    def test_row3_either_control_null(self):
        verdict, code = lib.compute_verdict(
            {"positive": None, "harness": self._good_harness()},
            [self._case(lib.MATCH)])
        self.assertEqual((verdict, code), ("INCONCLUSIVE", 3))
        verdict, code = lib.compute_verdict(
            {"positive": self._good_positive(), "harness": None},
            [self._case(lib.MATCH)])
        self.assertEqual((verdict, code), ("INCONCLUSIVE", 3))

    def test_row4_collapsed_false(self):
        positive = self._good_positive()
        positive["collapsed"] = False
        verdict, code = lib.compute_verdict(
            {"positive": positive, "harness": self._good_harness()},
            [self._case(lib.MATCH)])
        self.assertEqual((verdict, code), ("INCONCLUSIVE", 3))

    def test_row5_reproved_false(self):
        positive = self._good_positive()
        positive["reproved"] = False
        verdict, code = lib.compute_verdict(
            {"positive": positive, "harness": self._good_harness()},
            [self._case(lib.MATCH)])
        self.assertEqual((verdict, code), ("INCONCLUSIVE", 3))

    def test_row6_harness_produced_no_skill_call(self):
        harness = self._good_harness()
        harness["produced_skill_call"] = False
        verdict, code = lib.compute_verdict(
            {"positive": self._good_positive(), "harness": harness},
            [self._case(lib.MATCH)])
        self.assertEqual((verdict, code), ("INCONCLUSIVE", 3))

    def test_row7_a_case_is_runner_error(self):
        verdict, code = lib.compute_verdict(
            {"positive": self._good_positive(), "harness": self._good_harness()},
            [self._case(lib.MATCH), self._case(lib.RUNNER_ERROR)])
        self.assertEqual((verdict, code), ("INCONCLUSIVE", 3))

    def test_row7_a_case_is_incomplete(self):
        verdict, code = lib.compute_verdict(
            {"positive": self._good_positive(), "harness": self._good_harness()},
            [self._case(lib.MATCH), self._case("INCOMPLETE")])
        self.assertEqual((verdict, code), ("INCONCLUSIVE", 3))

    def test_row8_a_case_is_collision_or_no_skill(self):
        verdict, code = lib.compute_verdict(
            {"positive": self._good_positive(), "harness": self._good_harness()},
            [self._case(lib.MATCH), self._case(lib.COLLISION)])
        self.assertEqual((verdict, code), ("FAIL", 1))
        verdict, code = lib.compute_verdict(
            {"positive": self._good_positive(), "harness": self._good_harness()},
            [self._case(lib.NO_SKILL)])
        self.assertEqual((verdict, code), ("FAIL", 1))

    def test_row9_every_case_match_is_pass(self):
        verdict, code = lib.compute_verdict(
            {"positive": self._good_positive(), "harness": self._good_harness()},
            [self._case(lib.MATCH), self._case(lib.MATCH)])
        self.assertEqual((verdict, code), ("PASS", 0))


class TestComputeVerdictForbid(unittest.TestCase):
    def _controls(self):
        return {
            "positive": {"ran": True, "skill": "a:b", "path_edited": "/x",
                         "case_index": 0, "runs": 1, "baseline_matches": 1,
                         "gutted_matches": 0, "reprove_matches": 1,
                         "collapsed": True, "restored_verified": True,
                         "reproved": True},
            "harness": {"ran": True, "observed": "a:b",
                        "produced_skill_call": True},
        }

    @staticmethod
    def _case(state):
        return {"prompt": "p", "expected": None, "forbidden": "a:b",
                "observed": None, "state": state, "error": None,
                "control": None}

    def test_forbidden_case_is_fail(self):
        self.assertEqual(
            lib.compute_verdict(self._controls(), [self._case(lib.FORBIDDEN)]),
            ("FAIL", 1))

    def test_forbid_match_beside_expect_matches_is_pass(self):
        self.assertEqual(
            lib.compute_verdict(
                self._controls(),
                [self._case(lib.MATCH), self._case(lib.MATCH),
                 self._case(lib.MATCH)]),
            ("PASS", 0))


class TestRow9ReachableThroughTheCLI(unittest.TestCase):
    """Row 9 through the real CLI wiring, not just `compute_verdict`
    directly -- without this, an instrument that can never pass would still
    satisfy every unit-level truth-table test."""

    def test_both_controls_good_every_case_match_is_pass_exit_zero(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5-row9-")
        try:
            fx = _make_fake_home(tmp_dir)
            report_path = os.path.join(tmp_dir, "report.json")
            result, _ = _run_cli(
                [{"prompt": "build this", "expect": "fake:probe",
                  "control": "positive"}],
                ["--positive-control", "--harness-control",
                 "--json", report_path, "--timeout", "10", "--runs", "1"],
                home=fx["home"], cwd=tmp_dir,
                extra_env={
                    "STUB_SKILL_MD_PATH": fx["block_md"],
                    "STUB_MARKER": FAKE_SKILL_MARKER,
                    "STUB_EXPECT_SKILL": "fake:probe",
                    "STUB_HARNESS_PROMPT": lib.HARNESS_PROMPT,
                })
            self.assertEqual(result.returncode, 0, msg=result.stderr)
            with open(report_path) as f:
                report = json.load(f)
            self.assertEqual(report["verdict"], "PASS")
            self.assertTrue(report["controls"]["positive"]["collapsed"])
            self.assertTrue(report["controls"]["positive"]["reproved"])
            self.assertTrue(report["controls"]["harness"]["produced_skill_call"])
            self.assertTrue(
                all(c["state"] == lib.MATCH for c in report["cases"]))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# --- repeat runs -------------------------------------------------------
#
# Routing is close to a coin flip at N=1. These tests cover the aggregation `build_case_report` now does
# across a list of `CaseRun`, the N-run positive/harness control windows,
# and the load-bearing rule that N=1 must reproduce every pre-existing
# assertion exactly (enforced above by adding `--runs 1` to every
# pre-existing CLI-driving test, and `runs=1` to the direct
# `run_harness_control` calls).

def _cr(kind, expected="a:b"):
    """Build a synthetic CaseRun for build_case_report aggregation tests.
    M = matches `expected`, N = no skill call, O = a different skill
    (collision), E = RUNNER_ERROR.
    """
    if kind == "M":
        return lib.CaseRun(observed=expected, init={"skills": ["x"]},
                            status="OK", error=None)
    if kind == "N":
        return lib.CaseRun(observed=None, init={"skills": ["x"]},
                            status="OK", error=None)
    if kind == "O":
        return lib.CaseRun(observed="other:skill", init={"skills": ["x"]},
                            status="OK", error=None)
    if kind == "I":
        return lib.CaseRun(observed=None, init={"skills": ["x"]},
                            status="INCOMPLETE",
                            error="timed out after 10s with no Skill call")
    if kind == "E":
        return lib.CaseRun(observed=None, init=None, status="RUNNER_ERROR",
                            error="boom")
    raise ValueError(kind)


def _agg_case(expect="a:b", control=None):
    return {"prompt": "p", "expect": expect, "control": control}


class TestBuildCaseReportAggregation(unittest.TestCase):
    """`build_case_report` counts matches across a list of CaseRun rather
    than trusting a single run. At runs==1 this reduces exactly to the old
    single-run classify() result -- proven separately by every pre-existing
    CLI-driving test still passing with only `--runs 1` added.
    """

    def test_two_of_three_match_is_match_with_matches_count(self):
        report = lib.build_case_report(
            _agg_case(), [_cr("M"), _cr("N"), _cr("M")])
        self.assertEqual(report["state"], lib.MATCH)
        self.assertEqual(report["matches"], 2)
        self.assertEqual(report["runs"], 3)

    def test_one_of_three_match_is_no_skill(self):
        report = lib.build_case_report(
            _agg_case(), [_cr("N"), _cr("M"), _cr("N")])
        self.assertEqual(report["state"], lib.NO_SKILL)

    def test_a_collision_run_present_is_collision(self):
        report = lib.build_case_report(
            _agg_case(), [_cr("N"), _cr("O"), _cr("N")])
        self.assertEqual(report["state"], lib.COLLISION)

    def test_any_runner_error_wins_over_matches(self):
        report = lib.build_case_report(
            _agg_case(), [_cr("M"), _cr("M"), _cr("E")])
        self.assertEqual(report["state"], lib.RUNNER_ERROR)
        self.assertIsNotNone(report["error"])

    def test_exactly_half_is_match(self):
        report = lib.build_case_report(_agg_case(), [_cr("M"), _cr("N")])
        self.assertEqual(report["state"], lib.MATCH)
        self.assertEqual(report["matches"], 1)

    def test_observed_counts_sorted_descending_with_null_entry(self):
        """observed_counts ordering and the null entry, asserted on this one
        aggregation."""
        report = lib.build_case_report(
            _agg_case(), [_cr("M"), _cr("N"), _cr("M")])
        self.assertEqual(
            report["observed_counts"],
            [{"skill": "a:b", "count": 2}, {"skill": None, "count": 1}])
        self.assertEqual(report["observed"], "a:b")

    def test_all_incomplete_is_incomplete(self):
        report = lib.build_case_report(_agg_case(), [_cr("I")] * 5)
        self.assertEqual(report["state"], "INCOMPLETE")
        self.assertEqual(report["matches"], 0)
        self.assertEqual(report["incomplete"], 5)
        self.assertEqual(report["observed_counts"], [])
        self.assertIn("timed out", report["error"])

    def test_three_match_two_incomplete_is_match(self):
        report = lib.build_case_report(
            _agg_case(), [_cr("M")] * 3 + [_cr("I")] * 2)
        self.assertEqual(report["state"], lib.MATCH)
        self.assertEqual(report["incomplete"], 2)

    def test_two_match_three_incomplete_is_incomplete(self):
        """Fewer than half the runs completed, so no routing verdict."""
        report = lib.build_case_report(
            _agg_case(), [_cr("M")] * 2 + [_cr("I")] * 3)
        self.assertEqual(report["state"], "INCOMPLETE")

    def test_ok_runs_with_no_skill_stay_no_skill(self):
        report = lib.build_case_report(
            _agg_case(), [_cr("M")] * 2 + [_cr("N")] * 3)
        self.assertEqual(report["state"], lib.NO_SKILL)
        self.assertEqual(report["incomplete"], 0)

    def test_match_denominator_is_completed_runs_not_total(self):
        """2 of 3 completed runs match: MATCH. Under `matches*2 >= n` (n=5)
        this would read COLLISION, so this alone separates the two."""
        report = lib.build_case_report(
            _agg_case(), [_cr("M")] * 2 + [_cr("O")] + [_cr("I")] * 2)
        self.assertEqual(report["state"], lib.MATCH)


class TestBuildCaseReportForbid(unittest.TestCase):
    """A forbid case is a hit when the run observed anything but the
    forbidden skill -- including no Skill call at all."""

    @staticmethod
    def _forbid_case():
        return {"prompt": "p", "expect": None, "forbid": "other:skill",
                "control": None}

    def test_none_and_another_skill_are_hits_so_match(self):
        another = lib.CaseRun(observed="third:skill", init={"skills": ["x"]},
                              status="OK", error=None)
        report = lib.build_case_report(
            self._forbid_case(), [_cr("N")] * 3 + [another] * 2)
        self.assertEqual(report["state"], lib.MATCH)
        self.assertEqual(report["matches"], 5)
        self.assertEqual(report["forbidden"], "other:skill")
        self.assertIsNone(report["expected"])

    def test_three_of_five_observe_forbidden_is_forbidden(self):
        report = lib.build_case_report(
            self._forbid_case(), [_cr("O")] * 3 + [_cr("N")] * 2)
        self.assertEqual(report["state"], lib.FORBIDDEN)
        self.assertEqual(report["matches"], 2)

    def test_two_of_four_completed_tie_is_match(self):
        report = lib.build_case_report(
            self._forbid_case(),
            [_cr("O")] * 2 + [_cr("N")] * 2)
        self.assertEqual(report["state"], lib.MATCH)
        self.assertEqual(report["matches"], 2)

    def test_runner_error_still_takes_precedence(self):
        report = lib.build_case_report(
            self._forbid_case(), [_cr("N"), _cr("N"), _cr("E")])
        self.assertEqual(report["state"], lib.RUNNER_ERROR)

    def test_incomplete_still_takes_precedence(self):
        report = lib.build_case_report(
            self._forbid_case(), [_cr("N")] * 2 + [_cr("I")] * 3)
        self.assertEqual(report["state"], "INCOMPLETE")

    def test_expect_case_reports_forbidden_none(self):
        report = lib.build_case_report(_agg_case(), [_cr("M")])
        self.assertIsNone(report["forbidden"])


class TestBuildCaseReportThroughCLI(unittest.TestCase):
    """`--runs` reaching the report through the real CLI wiring, not just
    `build_case_report` directly."""

    def test_runs_three_carries_through_and_stub_sees_three_invocations(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5b-cli-")
        try:
            fx = _make_fake_home(tmp_dir)
            report_path = os.path.join(tmp_dir, "report.json")
            log_path = os.path.join(tmp_dir, "stub.log")
            counter_path = os.path.join(tmp_dir, "counter")
            result, _ = _run_cli(
                [{"prompt": "irrelevant", "expect": "fake:probe"}],
                ["--timeout", "10", "--runs", "3", "--json", report_path],
                home=fx["home"], cwd=tmp_dir,
                extra_env={
                    "STUB_SKILL_MD_PATH": fx["block_md"],
                    "STUB_MODE": "sequence",
                    "STUB_SEQUENCE": "match,match,match",
                    "STUB_COUNTER_FILE": counter_path,
                    "STUB_EXPECT_SKILL": "fake:probe",
                    "STUB_LOG": log_path,
                })
            with open(report_path) as f:
                report = json.load(f)
            self.assertEqual(report["cases"][0]["runs"], 3, msg=result.stderr)
            self.assertEqual(len(_read_log_entries(log_path)), 3)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestGuttedCollapsed(unittest.TestCase):
    """The positive control's collapse predicate, as a pure function."""

    def test_all_none_gutted_runs_collapse(self):
        runs = [_cr("N")] * 3
        self.assertEqual(lib._gutted_collapsed(runs, "a:b", 2), (0, True))

    def test_every_gutted_run_incomplete_is_not_a_collapse(self):
        runs = [_cr("I")] * 3
        self.assertEqual(lib._gutted_collapsed(runs, "a:b", 2), (0, False))

    def test_one_incomplete_gutted_run_is_not_a_collapse(self):
        runs = [_cr("N"), _cr("N"), _cr("I")]
        self.assertEqual(lib._gutted_collapsed(runs, "a:b", 2), (0, False))

    def test_runner_error_is_not_a_collapse(self):
        runs = [_cr("N"), _cr("E")]
        self.assertEqual(lib._gutted_collapsed(runs, "a:b", 2), (0, False))

    def test_zero_baseline_never_collapses(self):
        self.assertEqual(
            lib._gutted_collapsed([_cr("N")] * 3, "a:b", 0), (0, False))

    def test_a_gutted_match_is_not_a_collapse(self):
        self.assertEqual(
            lib._gutted_collapsed([_cr("M"), _cr("N")], "a:b", 2), (1, False))


class TestPositiveControlWithRuns(unittest.TestCase):
    """Baseline/gutted/reprove are each counted across `--runs`, not a
    single run."""

    def _run(self, tmp_dir, fx, sequence, *, runs=3):
        report_path = os.path.join(tmp_dir, "report.json")
        counter_path = os.path.join(tmp_dir, "counter")
        result, _ = _run_cli(
            [{"prompt": "build this", "expect": "fake:probe",
              "control": "positive"}],
            ["--positive-control", "--json", report_path,
             "--timeout", "10", "--runs", str(runs)],
            home=fx["home"], cwd=tmp_dir,
            extra_env={
                "STUB_SKILL_MD_PATH": fx["block_md"],
                "STUB_MODE": "sequence",
                "STUB_SEQUENCE": sequence,
                "STUB_COUNTER_FILE": counter_path,
                "STUB_EXPECT_SKILL": "fake:probe",
            })
        with open(report_path) as f:
            report = json.load(f)
        return result, report

    def test_happy_path_counts(self):
        """baseline [M,N,M]=2, gutted [N,N,N]=0, reprove [N,M,N]=1."""
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5b-pos-")
        try:
            fx = _make_fake_home(tmp_dir)
            sequence = "match,none,match,none,none,none,none,match,none"
            _, report = self._run(tmp_dir, fx, sequence)
            positive = report["controls"]["positive"]
            self.assertEqual(positive["runs"], 3)
            self.assertEqual(positive["baseline_matches"], 2)
            self.assertEqual(positive["gutted_matches"], 0)
            self.assertEqual(positive["reprove_matches"], 1)
            self.assertTrue(positive["collapsed"])
            self.assertTrue(positive["reproved"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_one_gutted_hit_is_not_a_collapse(self):
        """baseline [M,N,M]=2, gutted [N,M,N]=1 -- one hit is not a
        collapse."""
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5b-pos-")
        try:
            fx = _make_fake_home(tmp_dir)
            sequence = "match,none,match,none,match,none,none,none,none"
            _, report = self._run(tmp_dir, fx, sequence)
            self.assertFalse(report["controls"]["positive"]["collapsed"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_zero_baseline_never_collapses_for_free(self):
        """baseline [N,N,N]=0 -- a case that never routes must not
        "collapse" for free just because gutted is also all-none."""
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5b-pos-")
        try:
            fx = _make_fake_home(tmp_dir)
            sequence = ",".join(["none"] * 9)
            _, report = self._run(tmp_dir, fx, sequence)
            positive = report["controls"]["positive"]
            self.assertEqual(positive["baseline_matches"], 0)
            self.assertEqual(positive["gutted_matches"], 0)
            self.assertFalse(positive["collapsed"])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_reprove_all_none_is_not_reproved(self):
        """baseline [M,N,M]=2, gutted [N,N,N]=0 (collapses), reprove
        [N,N,N]=0 -- not reproved, exit 3."""
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5b-pos-")
        try:
            fx = _make_fake_home(tmp_dir)
            sequence = "match,none,match,none,none,none,none,none,none"
            result, report = self._run(tmp_dir, fx, sequence)
            positive = report["controls"]["positive"]
            self.assertTrue(positive["collapsed"])
            self.assertFalse(positive["reproved"])
            self.assertEqual(result.returncode, 3, msg=result.stderr)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestHarnessControlWithRuns(unittest.TestCase):
    """`run_harness_control`'s own N-run counting, direct (not through the
    CLI, matching TestRunHarnessControl's style above)."""

    def _harness(self, sequence, *, runs=3):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t5b-harness-")
        try:
            skill_md = os.path.join(tmp_dir, "skill.md")
            with open(skill_md, "w") as f:
                f.write("irrelevant -- sequence mode never reads this")
            counter_path = os.path.join(tmp_dir, "counter")
            env_extra = {
                "STUB_SKILL_MD_PATH": skill_md,
                "STUB_MODE": "sequence",
                "STUB_SEQUENCE": sequence,
                "STUB_COUNTER_FILE": counter_path,
                "STUB_EXPECT_SKILL": "toolkit-build:build",
            }
            previous = {k: os.environ.get(k) for k in env_extra}
            os.environ.update(env_extra)
            try:
                with _stub_claude(_STUB_CONTROL):
                    return lib.run_harness_control(timeout_s=10, runs=runs)
            finally:
                for k, v in previous.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_one_of_three_skill_calls(self):
        result = self._harness("none,none,match")
        self.assertEqual(result["skill_calls"], 1)
        self.assertTrue(result["produced_skill_call"])
        self.assertEqual(result["runs"], 3)

    def test_zero_of_three_skill_calls(self):
        result = self._harness("none,none,none")
        self.assertEqual(result["skill_calls"], 0)
        self.assertFalse(result["produced_skill_call"])


class TestHelpTextMentionsStochasticity(unittest.TestCase):
    """`--help` must state that routing is stochastic, that `--runs`
    defaults to 5, and that a result is a count, not a yes/no."""

    def test_help_mentions_stochastic_runs_default_and_count_semantics(self):
        result = subprocess.run(
            [CLI_PATH, "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # argparse's HelpFormatter wraps at word boundaries -- a literal
        # "default 5" substring is not safe against a line break landing
        # between the two words, so collapse all whitespace first.
        help_text = " ".join(result.stdout.split())
        self.assertIn("stochastic", help_text)
        self.assertIn("default 5", help_text)
        self.assertIn("count", help_text)


class TestReadonlyContainment(unittest.TestCase):
    """`run_case` must hand the child session a `--settings` payload
    carrying the read-only PreToolUse hook (containment for a
    trigger-eval child), and every caller that reaches `run_case` must get
    it too, not just the direct path.
    """

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-t-readonly-")
        self.argvfile = os.path.join(self.tmp_dir, "argv.json")
        os.environ["STUB_ARGVFILE"] = self.argvfile

    def tearDown(self):
        os.environ.pop("STUB_ARGVFILE", None)
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @staticmethod
    def _record_stub():
        return _STUB_RECORD_ARGV.replace(
            "__FIXTURE_PATH__", os.path.join(FIXTURES, "no-skill.jsonl"))

    def _read_argv(self):
        with open(self.argvfile) as f:
            return json.loads(f.read())

    def test_run_case_passes_readonly_settings(self):
        with _stub_claude(self._record_stub()):
            lib.run_case("x", timeout_s=10)
        argv = self._read_argv()
        self.assertEqual(argv.count("--settings"), 1)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(settings, json.loads(lib.readonly_settings()))
        self.assertIs(settings["disableAllHooks"], False)
        pre_tool_use = settings["hooks"]["PreToolUse"]
        self.assertEqual(len(pre_tool_use), 1)
        self.assertEqual(pre_tool_use[0]["matcher"], "*")
        self.assertEqual(len(pre_tool_use[0]["hooks"]), 1)
        self.assertEqual(pre_tool_use[0]["hooks"][0]["type"], "command")

    def test_the_command_the_child_receives_refuses(self):
        settings = json.loads(lib.readonly_settings())
        command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        parts = shlex.split(command)
        self.assertEqual(len(parts), 2)
        for part in parts:
            self.assertTrue(os.path.exists(part), msg=part)

        blocked = subprocess.run(
            parts, input='{"tool_name": "Bash"}',
            capture_output=True, text=True)
        self.assertEqual(blocked.returncode, 2, msg=blocked.stderr)

        allowed = subprocess.run(
            parts, input='{"tool_name": "Read"}',
            capture_output=True, text=True)
        self.assertEqual(allowed.returncode, 0, msg=allowed.stderr)

    def test_positive_control_path_passes_readonly_settings(self):
        fx = _make_fake_home(self.tmp_dir)
        case = {"prompt": "build this", "expect": "fake:probe",
                "control": "positive"}
        with _stub_claude(self._record_stub()):
            lib.run_positive_control(
                case, 0, fx["block_md"],
                timeout_s=10, runs=1, baseline_matches=1)
        argv = self._read_argv()
        self.assertIn("--settings", argv)
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(settings, json.loads(lib.readonly_settings()))

    def test_run_case_refuses_slash_prompt_before_launching(self):
        """A slash-command prompt's `!` preprocessing runs before
        any tool call, outside the hook entirely -- `run_case` must refuse
        it independently of `load_cases`, before the child ever launches.
        """
        with _stub_claude(self._record_stub()):
            run = lib.run_case("/x", timeout_s=5)
        self.assertEqual(run.status, "RUNNER_ERROR")
        self.assertIsNone(run.observed)
        self.assertIsNone(run.init)
        self.assertIsNotNone(run.error)
        self.assertFalse(
            os.path.exists(self.argvfile),
            "a slash-command prompt must never launch the child")


class TestHooksDisabledBy(unittest.TestCase):
    """`hooks_disabled_by` refuses a trigger-eval run whose user or project
    settings disable every hook, because `readonly_settings()`'s
    `disableAllHooks: false` pin outranks that choice and would silently
    re-enable hooks the user switched off."""

    def _write_json(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_home_settings_json_true_returns_that_path(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-hooks-disabled-")
        try:
            home = os.path.join(tmp_dir, "home")
            cwd = os.path.join(tmp_dir, "cwd")
            os.makedirs(cwd)
            path = os.path.join(home, ".claude", "settings.json")
            self._write_json(path, {"disableAllHooks": True})
            self.assertEqual(lib.hooks_disabled_by(home, cwd), path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_cwd_settings_json_true_returns_that_path(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-hooks-disabled-")
        try:
            home = os.path.join(tmp_dir, "home")
            cwd = os.path.join(tmp_dir, "cwd")
            os.makedirs(home)
            path = os.path.join(cwd, ".claude", "settings.json")
            self._write_json(path, {"disableAllHooks": True})
            self.assertEqual(lib.hooks_disabled_by(home, cwd), path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_cwd_settings_local_json_true_returns_that_path(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-hooks-disabled-")
        try:
            home = os.path.join(tmp_dir, "home")
            cwd = os.path.join(tmp_dir, "cwd")
            os.makedirs(home)
            path = os.path.join(cwd, ".claude", "settings.local.json")
            self._write_json(path, {"disableAllHooks": True})
            self.assertEqual(lib.hooks_disabled_by(home, cwd), path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_false_returns_none(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-hooks-disabled-")
        try:
            home = os.path.join(tmp_dir, "home")
            cwd = os.path.join(tmp_dir, "cwd")
            os.makedirs(cwd)
            path = os.path.join(home, ".claude", "settings.json")
            self._write_json(path, {"disableAllHooks": False})
            self.assertIsNone(lib.hooks_disabled_by(home, cwd))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_absent_returns_none(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-hooks-disabled-")
        try:
            home = os.path.join(tmp_dir, "home")
            cwd = os.path.join(tmp_dir, "cwd")
            os.makedirs(home)
            os.makedirs(cwd)
            self.assertIsNone(lib.hooks_disabled_by(home, cwd))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_invalid_json_returns_none(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-hooks-disabled-")
        try:
            home = os.path.join(tmp_dir, "home")
            cwd = os.path.join(tmp_dir, "cwd")
            os.makedirs(cwd)
            path = os.path.join(home, ".claude", "settings.json")
            self._write_json(path, "not json {")
            self.assertIsNone(lib.hooks_disabled_by(home, cwd))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestHooksDisabledByCLI(unittest.TestCase):
    """`claude-skill-trigger` calls `hooks_disabled_by` right after loading
    the case file and refuses, exiting 2, before any `claude` run."""

    def test_home_settings_disable_all_hooks_exits_2_before_any_run(self):
        tmp_dir = tempfile.mkdtemp(prefix="skill-trigger-hooks-disabled-cli-")
        try:
            home = os.path.join(tmp_dir, "home")
            settings_path = os.path.join(home, ".claude", "settings.json")
            os.makedirs(os.path.dirname(settings_path))
            with open(settings_path, "w") as f:
                json.dump({"disableAllHooks": True}, f)
            cases_path = os.path.join(tmp_dir, "cases.json")
            with open(cases_path, "w") as f:
                json.dump([{"prompt": "hi", "expect": "a:b"}], f)
            sentinel = os.path.join(tmp_dir, "sentinel")
            with _stub_claude(_STUB_SENTINEL):
                env = dict(os.environ)
                env["HOME"] = home
                env["STUB_SENTINEL"] = sentinel
                result = subprocess.run(
                    [CLI_PATH, "--cases", cases_path, "--runs", "1"],
                    capture_output=True, text=True, env=env, cwd=tmp_dir)
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            self.assertIn(settings_path, result.stderr)
            self.assertIn("disableAllHooks: true", result.stderr)
            self.assertFalse(
                os.path.exists(sentinel),
                "a disableAllHooks settings file must run no subprocess")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


class TestLeakSweepCannotPassVacuously(unittest.TestCase):
    """tearDownModule's sweep is the evidence that no stub leaked, so it
    needs its own controls: an empty `ps` listing must not read as clean."""

    DIR = "/tmp/skill-trigger-stub-abc"

    @staticmethod
    def _ps(stdout, returncode=0, stderr=""):
        return lambda *a, **k: subprocess.CompletedProcess(
            a[0] if a else [], returncode, stdout=stdout, stderr=stderr)

    def test_a_denied_ps_skips_rather_than_passes(self):
        with self.assertRaises(unittest.SkipTest):
            _leak_sweep(self._ps("", 1, "ps: operation not permitted"),
                        [self.DIR])

    def test_a_ps_that_cannot_launch_skips(self):
        def refuse(*a, **k):
            raise PermissionError(1, "Operation not permitted", "ps")
        with self.assertRaises(unittest.SkipTest):
            _leak_sweep(refuse, [self.DIR])

    def test_an_exit_0_listing_with_no_header_skips_too(self):
        with self.assertRaises(unittest.SkipTest):
            _leak_sweep(self._ps(""), [self.DIR])

    def test_a_clean_listing_passes(self):
        _leak_sweep(self._ps("  PID  PPID COMMAND\n  1 0 /sbin/launchd\n"),
                    [self.DIR])

    def test_a_leaked_stub_is_caught(self):
        with self.assertRaises(AssertionError):
            _leak_sweep(self._ps(
                "  PID  PPID COMMAND\n  42 1 python3 " + self.DIR
                + "/claude -p x\n"), [self.DIR])


_STUB_INVOCATION_RE = re.compile(r"skill-trigger-stub-\S*/claude\b")


def tearDownModule():
    """Final sweep, run once after every test in THIS module regardless of
    alphabetical class ordering (`unittest`'s module-level teardown hook,
    unlike a `TestCase` subclass, always runs last): no
    `skill-trigger-stub-*` process should remain, whatever its process
    group's original parent.

    Matches the actual invocation shape (`_stub_claude`'s temp directory
    followed by the literal `claude` filename it writes) rather than a bare
    substring -- a plain substring match self-matches any shell command
    that merely *mentions* "skill-trigger-stub" (this file's own tests do,
    in their docstrings and in ad-hoc diagnostic commands run alongside the
    suite), which is a false positive, not a leaked process.
    """
    _leak_sweep(subprocess.run, _CREATED_STUB_DIRS)


def _leak_sweep(run, created_dirs):
    """The sweep itself, with its `ps` runner injected so each outcome is
    testable: could not run (skip), clean (return), leak (raise)."""
    try:
        result = run(
            ["ps", "-eo", "pid,ppid,command"], capture_output=True, text=True)
    except OSError as exc:
        # Codex's sandbox refuses to launch `ps` at all.
        raise unittest.SkipTest(
            "leak sweep could not run: ps did not launch: {0}".format(exc))
    # A denied `ps` (Codex's sandbox: "operation not permitted") prints nothing,
    # and an empty listing finds no leak: a vacuous pass. Say the sweep could
    # not run instead. A skip is visible in the summary; a pass is not.
    if result.returncode != 0 or "PID" not in result.stdout.split("\n", 1)[0]:
        raise unittest.SkipTest(
            "leak sweep could not run: ps exited {0}: {1}".format(
                result.returncode, result.stderr.strip()[:200]))
    surviving = [
        line for line in result.stdout.splitlines()
        if _STUB_INVOCATION_RE.search(line)
        and any(d + "/claude" in line for d in created_dirs)]
    if surviving:
        raise AssertionError(
            "a stub process leaked past its test -- cleanup did not reap "
            "it:\n" + "\n".join(surviving))


class TestRunCaseDisablesAutoMemory(unittest.TestCase):
    """`install.sh` can point user-level `autoMemoryDirectory` at one
    directory for every session, so a trigger-eval child must opt out
    explicitly to stay neutral. `Popen` is mocked directly rather than
    through the `claude` stub so the assertion is on the call's `env`, not on
    anything `run_case` derives from a fake stream.
    """

    def test_env_passed_to_popen_disables_auto_memory(self):
        captured = {}

        class _FakeStdout:
            def __iter__(self):
                return iter([])

            def close(self):
                pass

        class _FakeProc:
            def __init__(self):
                self.stdout = _FakeStdout()
                self.pid = 999999999  # never a real, killable pid
                self.returncode = 0

            def wait(self, timeout=None):
                return 0

        def _fake_popen(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        # Seed a conflicting parent environment first: an inherited value
        # must not be able to satisfy the assertion below.
        with mock.patch.dict(os.environ, {
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0", "CLAUDECODE": "1"}):
            with mock.patch.object(lib.subprocess, "Popen",
                                    side_effect=_fake_popen):
                with mock.patch.object(lib, "_kill_process_group"):
                    lib.run_case("irrelevant prompt", timeout_s=5)

        self.assertEqual(
            captured["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertNotIn("CLAUDECODE", captured["env"])


# --- positive control reaches a directory-source plugin ------

_DIR_SKILL_MD = """---
name: build
description: >-
  Directory probe. MARKER_WORD_9f3c lives here.
---

Body of the directory-source skill.
"""

_STALE_SKILL_MD = """---
name: build
description: A stale cache copy.
---

Stale body.
"""


def _make_directory_home(base_dir, *, marketplaces=("claude-code-toolkit",),
                         plugin="toolkit-build", installed_marketplace="claude-code-toolkit",
                         write_dir_skill=True):
    """A fake $HOME where each name in `marketplaces` is a directory-source
    marketplace listing `plugin` at `./plugin` (its `skills/build/SKILL.md`
    written when `write_dir_skill`), and `installed_plugins.json` holds one
    user entry for `<plugin>@<installed_marketplace>` whose installPath is a
    separate stale cache copy that also has a `skills/build/SKILL.md`.
    """
    home = os.path.join(base_dir, "home")
    plugins_dir = os.path.join(home, ".claude", "plugins")
    os.makedirs(plugins_dir)
    known = {}
    dir_skill_mds = {}
    for name in marketplaces:
        location = os.path.join(base_dir, "checkouts", name)
        os.makedirs(os.path.join(location, ".claude-plugin"))
        with open(os.path.join(location, ".claude-plugin", "marketplace.json"), "w") as f:
            json.dump({"name": name,
                       "plugins": [{"name": plugin, "source": "./plugin"}]}, f)
        skill_dir = os.path.join(location, "plugin", "skills", "build")
        os.makedirs(skill_dir)
        md_path = os.path.join(skill_dir, "SKILL.md")
        if write_dir_skill:
            with open(md_path, "w") as f:
                f.write(_DIR_SKILL_MD)
        dir_skill_mds[name] = os.path.realpath(md_path)
        known[name] = {"source": {"source": "directory", "path": location},
                       "installLocation": location}
    with open(os.path.join(plugins_dir, "known_marketplaces.json"), "w") as f:
        json.dump(known, f)
    stale = os.path.join(plugins_dir, "cache", installed_marketplace, plugin, "1.0.0")
    os.makedirs(os.path.join(stale, "skills", "build"))
    stale_md = os.path.join(stale, "skills", "build", "SKILL.md")
    with open(stale_md, "w") as f:
        f.write(_STALE_SKILL_MD)
    _write_installed_plugins(
        os.path.join(plugins_dir, "installed_plugins.json"),
        {f"{plugin}@{installed_marketplace}": [
            {"scope": "user", "installPath": stale, "version": "1.0.0"}]})
    return {"home": home, "dir_md": dir_skill_mds, "stale_md": stale_md,
            "plugins_dir": plugins_dir}


class TestResolveSkillMdDirectorySource(unittest.TestCase):
    def test_directory_source_beats_stale_install_path(self):
        with tempfile.TemporaryDirectory(prefix="skill-trigger-dir-") as tmp:
            fx = _make_directory_home(tmp)
            got = lib.resolve_skill_md("toolkit-build:build", home=fx["home"], cwd=tmp)
            self.assertEqual(os.path.realpath(got), fx["dir_md"]["claude-code-toolkit"])
            self.assertNotEqual(os.path.realpath(got), os.path.realpath(fx["stale_md"]))

    def test_wrong_marketplace_is_never_chosen(self):
        with tempfile.TemporaryDirectory(prefix="skill-trigger-dir-") as tmp:
            fx = _make_directory_home(
                tmp, marketplaces=("aaa", "zzz"), plugin="shared",
                installed_marketplace="zzz")
            got = lib.resolve_skill_md("shared:build", home=fx["home"], cwd=tmp)
            self.assertEqual(os.path.realpath(got), fx["dir_md"]["zzz"])
            self.assertNotEqual(os.path.realpath(got), fx["dir_md"]["aaa"])

    def test_missing_skill_under_directory_root_refuses_without_fallback(self):
        with tempfile.TemporaryDirectory(prefix="skill-trigger-dir-") as tmp:
            fx = _make_directory_home(tmp, write_dir_skill=False)
            with self.assertRaises(lib.ControlError) as ctx:
                lib.resolve_skill_md("toolkit-build:build", home=fx["home"], cwd=tmp)
            self.assertIn(
                os.path.join("plugin", "skills", "build", "SKILL.md"), str(ctx.exception))

    def test_unimportable_plugin_paths_refuses_resolve_but_scan_still_runs(self):
        with tempfile.TemporaryDirectory(prefix="skill-trigger-dir-") as tmp:
            fx = _make_directory_home(tmp)
            cache_dir = os.path.join(fx["plugins_dir"], "cache", "other")
            os.makedirs(cache_dir)
            target = os.path.join(cache_dir, "SKILL.md")
            with open(target, "wb") as f:
                f.write(b"gutted")
            with open(target + lib.BACKUP_SUFFIX, "wb") as f:
                f.write(b"original")
            with mock.patch.dict(sys.modules, {"plugin_paths": None}):
                with self.assertRaises(lib.ControlError) as ctx:
                    lib.resolve_skill_md("toolkit-build:build", home=fx["home"], cwd=tmp)
                self.assertIn("plugin_paths", str(ctx.exception))
                with contextlib.redirect_stderr(open(os.devnull, "w")):
                    restored = lib.scan_and_restore_leftover_backups(home=fx["home"])
            self.assertEqual(restored, [target])
            with open(target, "rb") as f:
                self.assertEqual(f.read(), b"original")


class TestLeftoverScanDirectoryRoots(unittest.TestCase):
    def _plant(self, fx, backup_bytes):
        target = fx["dir_md"]["claude-code-toolkit"]
        with open(target, "wb") as f:
            f.write(b"gutted")
        with open(target + lib.BACKUP_SUFFIX, "wb") as f:
            f.write(backup_bytes)
        return target

    def test_sidecar_under_directory_root_is_restored(self):
        with tempfile.TemporaryDirectory(prefix="skill-trigger-dir-") as tmp:
            fx = _make_directory_home(tmp)
            target = self._plant(fx, b"original")
            with contextlib.redirect_stderr(open(os.devnull, "w")):
                restored = lib.scan_and_restore_leftover_backups(home=fx["home"])
            self.assertEqual([os.path.realpath(r) for r in restored], [target])
            with open(target, "rb") as f:
                self.assertEqual(f.read(), b"original")
            self.assertFalse(os.path.exists(target + lib.BACKUP_SUFFIX))

    def test_zero_byte_sidecar_under_directory_root_raises(self):
        with tempfile.TemporaryDirectory(prefix="skill-trigger-dir-") as tmp:
            fx = _make_directory_home(tmp)
            target = self._plant(fx, b"")
            with contextlib.redirect_stderr(open(os.devnull, "w")):
                with self.assertRaises(lib.LeftoverRestoreError):
                    lib.scan_and_restore_leftover_backups(home=fx["home"])
            with open(target, "rb") as f:
                self.assertEqual(f.read(), b"gutted")


class TestPositiveControlDirectorySourceEndToEnd(unittest.TestCase):
    def test_edits_and_restores_the_directory_source_file(self):
        with tempfile.TemporaryDirectory(prefix="skill-trigger-dir-") as tmp:
            fx = _make_directory_home(tmp)
            md = fx["dir_md"]["claude-code-toolkit"]
            with open(md, "rb") as f:
                original = f.read()
            log_path = os.path.join(tmp, "stub.log")
            report_path = os.path.join(tmp, "report.json")
            result, _ = _run_cli(
                [{"prompt": "build something", "expect": "toolkit-build:build",
                  "control": "positive"}],
                ["--positive-control", "--json", report_path,
                 "--timeout", "10", "--runs", "1"],
                home=fx["home"], cwd=tmp,
                extra_env={
                    "STUB_SKILL_MD_PATH": md, "STUB_LOG": log_path,
                    "STUB_MARKER": FAKE_SKILL_MARKER,
                    "STUB_EXPECT_SKILL": "toolkit-build:build"})
            self.assertEqual(result.returncode, 3, msg=result.stderr)
            with open(md, "rb") as f:
                self.assertEqual(f.read(), original)
            self.assertFalse(os.path.exists(md + lib.BACKUP_SUFFIX))
            with open(fx["stale_md"]) as f:
                self.assertEqual(f.read(), _STALE_SKILL_MD)
            with open(report_path) as f:
                positive = json.load(f)["controls"]["positive"]
            self.assertEqual(os.path.realpath(positive["path_edited"]), md)
            self.assertTrue(positive["collapsed"])
            self.assertTrue(positive["restored_verified"])
            inert = [e for e in _read_log_entries(log_path) if lib.INERT_DESCRIPTION in e]
            self.assertEqual(len(inert), 1)


class TestHarnessPromptMatchesTriggerCases(unittest.TestCase):
    def test_harness_prompt_equals_the_positive_trigger_case_prompt(self):
        path = os.path.join(REPO, "tools", "build-route", "trigger-cases.json")
        with open(path) as f:
            data = json.load(f)
        cases = data if isinstance(data, list) else data["cases"]
        positives = [c for c in cases if c.get("control") == "positive"]
        self.assertEqual(len(positives), 1)
        self.assertEqual(lib.HARNESS_PROMPT, positives[0]["prompt"])


if __name__ == "__main__":
    unittest.main()
