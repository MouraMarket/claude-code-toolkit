"""Tests for owner resolution.

Hermetic: no test reads the real ~/.claude/sessions, and no test asserts
against the machine's actual process tree except through a table it built
itself. The one exception is a live self-resolution smoke test, which skips
when it cannot find a claude ancestor rather than failing -- these run in CI
and in shells that have no claude parent.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import identity_lib


# The desktop app's Claude Code binary, as `ps comm=` shows it (observed on this
# Mac 2026-10-21). The space in "Application Support" is the whole point.
DESKTOP_CLAUDE = ("/Users/x/Library/Application Support/Claude/claude-code/2.1.295/"
                  "9d6405864fdc/claude.app/Contents/MacOS/claude")
VSCODE_CLAUDE = ("/Users/x/.vscode/extensions/anthropic.claude-code-2.1.300-darwin-arm64/"
                 "resources/native-binary/claude")


class TestWalkToClaude(unittest.TestCase):
    """The walk is pure: it takes a table of EXECUTABLE paths (`ps comm=`) and
    returns a pid or None."""

    def test_finds_claude_at_the_top(self):
        table = {
            10: (5, "/usr/bin/python3"),
            5: (2, "/bin/zsh"),
            2: (1, VSCODE_CLAUDE),
        }
        self.assertEqual(identity_lib.walk_to_claude(10, table), 2)

    def test_finds_the_desktop_apps_claude_under_a_path_with_spaces(self):
        # The old argv split read this as ".../Application" and the
        # guard allowed every edit in the desktop app.
        table = {
            10: (5, "/usr/bin/python3"),
            5: (2, "/bin/zsh"),
            2: (1, DESKTOP_CLAUDE),
        }
        self.assertEqual(identity_lib.walk_to_claude(10, table), 2)

    def test_finds_bare_claude_command(self):
        table = {7: (3, "/usr/bin/python3"), 3: (1, "claude")}
        self.assertEqual(identity_lib.walk_to_claude(7, table), 3)

    def test_returns_none_when_no_claude_ancestor(self):
        table = {9: (4, "/usr/bin/python3"), 4: (1, "/bin/zsh")}
        self.assertIsNone(identity_lib.walk_to_claude(9, table))

    def test_does_not_match_an_executable_merely_named_like_claude(self):
        # Exact basename only: a `claude-notes` tool, or a directory called
        # claude, is not the claude binary.
        table = {8: (1, "/usr/local/bin/claude-notes"),
                 7: (1, "/Users/x/claude/bin/vim")}
        self.assertIsNone(identity_lib.walk_to_claude(8, table))
        self.assertIsNone(identity_lib.walk_to_claude(7, table))

    def test_stops_at_pid_one_without_looping(self):
        table = {6: (1, "/bin/sh"), 1: (1, "/sbin/launchd")}
        self.assertIsNone(identity_lib.walk_to_claude(6, table))

    def test_survives_a_cycle_in_the_table(self):
        # A malformed table must not hang a hook that runs on every tool call.
        table = {4: (5, "/bin/a"), 5: (4, "/bin/b")}
        self.assertIsNone(identity_lib.walk_to_claude(4, table))

    def test_returns_none_for_a_pid_absent_from_the_table(self):
        self.assertIsNone(identity_lib.walk_to_claude(999, {1: (1, "/sbin/launchd")}))

    def test_whitespace_only_command_does_not_raise(self):
        # A malformed table entry must degrade to None, never IndexError.
        self.assertIsNone(identity_lib.walk_to_claude(5, {5: (1, "   ")}))

    def test_empty_string_command_does_not_raise(self):
        self.assertIsNone(identity_lib.walk_to_claude(5, {5: (1, "")}))


class TestRegistry(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._real = identity_lib.SESSIONS_DIR
        identity_lib.SESSIONS_DIR = self.tmp

    def tearDown(self):
        identity_lib.SESSIONS_DIR = self._real

    def _write(self, pid, data):
        with open(os.path.join(self.tmp, "%d.json" % pid), "w") as handle:
            json.dump(data, handle)

    def test_reads_a_registry_entry(self):
        self._write(4242, {"pid": 4242, "sessionId": "abc", "name": "session-d5"})
        entry = identity_lib.registry_entry(4242)
        self.assertEqual(entry["sessionId"], "abc")
        self.assertEqual(entry["name"], "session-d5")

    def test_missing_entry_is_none(self):
        self.assertIsNone(identity_lib.registry_entry(4242))

    def test_corrupt_entry_is_none_not_an_exception(self):
        with open(os.path.join(self.tmp, "77.json"), "w") as handle:
            handle.write("{not json")
        self.assertIsNone(identity_lib.registry_entry(77))

    def test_non_dict_entry_is_none(self):
        self._write(78, ["a", "list"])
        self.assertIsNone(identity_lib.registry_entry(78))


class TestLiveness(unittest.TestCase):

    def test_current_process_is_live_when_start_matches(self):
        pid = os.getpid()
        self.assertTrue(identity_lib.is_pid_live(pid, identity_lib.proc_start(pid)))

    def test_current_process_is_not_live_when_start_differs(self):
        # This is the PID-reuse guard: right pid, wrong process.
        self.assertFalse(identity_lib.is_pid_live(os.getpid(), "Thu Jan  1 00:00:00 1970"))

    def test_unknown_start_falls_back_to_existence(self):
        # A record written before proc_start existed still resolves.
        self.assertTrue(identity_lib.is_pid_live(os.getpid(), None))

    def test_dead_pid_is_not_live(self):
        self.assertFalse(identity_lib.is_pid_live(999999, None))

    def test_nonsense_pid_is_not_live(self):
        self.assertFalse(identity_lib.is_pid_live(0, None))
        self.assertFalse(identity_lib.is_pid_live(-1, None))
        self.assertFalse(identity_lib.is_pid_live(None, None))


class TestStartTimes(unittest.TestCase):
    """One `ps` for every process's start time, instead of one per pid."""

    def test_agrees_with_proc_start_for_this_process(self):
        # The two must be byte-identical or threading the map through liveness
        # would silently start reporting live processes as dead.
        starts = identity_lib.start_times()
        if not starts:
            self.skipTest("no process table available")
        self.assertEqual(starts.get(os.getpid()),
                         identity_lib.proc_start(os.getpid()))

    def test_a_shared_map_answers_liveness_without_forking(self):
        pid = os.getpid()
        starts = {pid: identity_lib.proc_start(pid)}
        self.assertTrue(identity_lib.is_pid_live(pid, starts[pid], starts=starts))

    def test_a_shared_map_still_catches_a_recycled_pid(self):
        pid = os.getpid()
        starts = {pid: identity_lib.proc_start(pid)}
        self.assertFalse(identity_lib.is_pid_live(
            pid, "Thu Jan  1 00:00:00 1970", starts=starts))

    def test_a_pid_missing_from_the_map_is_not_live(self):
        # A NON-empty map that lacks the pid is a working table: the pid started
        # after the snapshot, so it is a recycled pid. (An EMPTY map is the
        # "ps failed" signal, covered by TestFailedInspection.)
        self.assertIs(identity_lib.is_pid_live(
            os.getpid(), "Thu Jan  1 00:00:00 1970",
            starts={1: "Thu Jan  1 00:00:00 1970"}), False)


class TestPsTimeoutSeam(unittest.TestCase):
    """CLAIM_TEST_PS_TIMEOUT only counts inside a sandbox (CLAIM_CLAIMS_PATH)."""

    def env(self, **values):
        base = {k: v for k, v in os.environ.items()
                if k not in ("CLAIM_TEST_PS_TIMEOUT", "CLAIM_CLAIMS_PATH")}
        base.update(values)
        return mock.patch.dict(os.environ, base, clear=True)

    def test_raised_inside_a_sandbox(self):
        with self.env(CLAIM_TEST_PS_TIMEOUT="30", CLAIM_CLAIMS_PATH="/x/c.json"):
            self.assertEqual(identity_lib._ps_timeout(), 30.0)

    def test_inert_without_the_sandbox_path(self):
        with self.env(CLAIM_TEST_PS_TIMEOUT="30"):
            self.assertEqual(identity_lib._ps_timeout(), identity_lib.PS_TIMEOUT)

    def test_bad_values_fall_back_to_the_constant(self):
        for bad in ("0", "-1", "abc", "inf", "Infinity", "1e309", "nan"):
            with self.env(CLAIM_TEST_PS_TIMEOUT=bad,
                          CLAIM_CLAIMS_PATH="/x/c.json"):
                self.assertEqual(identity_lib._ps_timeout(),
                                 identity_lib.PS_TIMEOUT, bad)

    def test_process_table_passes_the_raised_timeout_to_ps(self):
        with self.env(CLAIM_TEST_PS_TIMEOUT="30", CLAIM_CLAIMS_PATH="/x/c.json"):
            with mock.patch.object(identity_lib.subprocess, "run") as run:
                run.return_value = mock.Mock(stdout="")
                identity_lib.process_table()
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)


class TestFailedInspection(unittest.TestCase):
    """A failed `ps` must not read a live process as dead: callers PERSIST the
    verdict (prune, claude-session-release), so only a positive answer may
    clear a record."""

    def setUp(self):
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.kill)
        # Captured BEFORE any shim is installed.
        self.real_start = identity_lib.proc_start(self.child.pid)
        if self.real_start is None:
            self.skipTest("no ps")

    def _shim_ps(self, script):
        """Put a `ps` running `script` first on PATH; restore PATH on cleanup."""
        shim_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, shim_dir, True)
        shim = os.path.join(shim_dir, "ps")
        with open(shim, "w") as f:
            f.write(script)
        os.chmod(shim, 0o755)
        original = os.environ["PATH"]
        self.addCleanup(os.environ.__setitem__, "PATH", original)
        os.environ["PATH"] = shim_dir + os.pathsep + original

    def test_an_empty_table_falls_back_to_existence(self):
        self.assertIs(identity_lib.is_pid_live(
            self.child.pid, self.real_start, starts={}), True)

    def test_an_empty_table_still_fails_a_dead_pid(self):
        # Control: the fallback is existence, not "always live".
        self.assertIs(identity_lib.is_pid_live(
            999999, self.real_start, starts={}), False)

    def test_a_failed_per_pid_lookup_falls_back_to_existence(self):
        self._shim_ps("#!/bin/sh\nexit 1\n")
        # Proves the shim fired, so the True below is the fallback.
        self.assertIsNone(identity_lib.proc_start(self.child.pid))
        self.assertIs(identity_lib.is_pid_live(
            self.child.pid, self.real_start), True)

    def test_a_failed_per_pid_lookup_with_output_is_unknown(self):
        # The shim PRINTS before failing, so this proves proc_start rejects a
        # failed ps's output rather than reading it as a (mismatched) start.
        self._shim_ps("#!/bin/sh\necho \"unexpected output\"\nexit 1\n")
        self.assertIsNone(identity_lib.proc_start(self.child.pid))
        self.assertIs(identity_lib.is_pid_live(
            self.child.pid, self.real_start), True)

    def test_a_failing_ps_with_output_is_no_table(self):
        # A failed ps that still printed a row must not become a one-row table.
        self._shim_ps('#!/bin/sh\necho "%d Thu Jan  1 00:00:00 1970"\nexit 1\n'
                      % os.getpid())
        self.assertEqual(identity_lib.start_times(), {})


class TestResolveHookOwner(unittest.TestCase):
    """The compound test seam, which both hooks now share instead of each
    carrying a copy. An identity override in a guard is a backdoor, so it is
    inert unless the claims file has ALSO been redirected to a sandbox.
    """

    def setUp(self):
        self._saved = {name: os.environ.get(name)
                       for name in ("CLAIM_TEST_OWNER_PID", "CLAIM_CLAIMS_PATH")}

    def tearDown(self):
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_both_variables_together_pin_the_owner(self):
        os.environ["CLAIM_TEST_OWNER_PID"] = "4242"
        os.environ["CLAIM_CLAIMS_PATH"] = "/tmp/sandbox-claims.json"
        owner = identity_lib.resolve_hook_owner()
        self.assertEqual(owner["pid"], 4242)
        self.assertEqual(owner["session_id"], "test-owner")

    def test_the_seam_owner_carries_no_start_time(self):
        os.environ["CLAIM_TEST_OWNER_PID"] = "4242"
        os.environ["CLAIM_CLAIMS_PATH"] = "/tmp/sandbox-claims.json"
        self.assertNotIn("proc_start", identity_lib.resolve_hook_owner())

    def test_the_override_alone_is_inert(self):
        # THE security property. Without CLAIM_CLAIMS_PATH this must fall
        # through to real process-tree resolution, which can only ever yield a
        # real owner or None -- never the test owner.
        os.environ["CLAIM_TEST_OWNER_PID"] = "4242"
        os.environ.pop("CLAIM_CLAIMS_PATH", None)
        owner = identity_lib.resolve_hook_owner()
        if owner is not None:
            self.assertNotEqual(owner["session_id"], "test-owner")
            self.assertNotEqual(owner["pid"], 4242)

    def test_a_non_numeric_override_is_none_not_an_exception(self):
        os.environ["CLAIM_TEST_OWNER_PID"] = "not-a-pid"
        os.environ["CLAIM_CLAIMS_PATH"] = "/tmp/sandbox-claims.json"
        self.assertIsNone(identity_lib.resolve_hook_owner())


class TestResolveOwner(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._real = identity_lib.SESSIONS_DIR
        identity_lib.SESSIONS_DIR = self.tmp

    def tearDown(self):
        identity_lib.SESSIONS_DIR = self._real

    def test_unresolvable_owner_is_none(self):
        # No claude ancestor -> identity not established -> caller must allow.
        table = {os.getpid(): (1, "/bin/zsh")}
        self.assertIsNone(identity_lib.resolve_owner(os.getpid(), table=table))

    def test_claude_ancestor_without_registry_entry_is_none(self):
        # Positive cross-check: a pid the registry does not know is not trusted.
        table = {50: (60, "/usr/bin/python3 h"), 60: (1, "/x/claude")}
        self.assertIsNone(identity_lib.resolve_owner(50, table=table))

    def test_resolves_when_walk_and_registry_agree(self):
        pid = os.getpid()
        with open(os.path.join(self.tmp, "%d.json" % pid), "w") as handle:
            json.dump({"pid": pid, "sessionId": "sess-1", "name": "session-aa"}, handle)
        table = {12345: (pid, "/usr/bin/python3 h"), pid: (1, "/x/native-binary/claude")}
        owner = identity_lib.resolve_owner(12345, table=table)
        self.assertEqual(owner["pid"], pid)
        self.assertEqual(owner["session_id"], "sess-1")
        self.assertEqual(owner["name"], "session-aa")
        self.assertNotIn("proc_start", owner)

    def test_resolving_an_owner_forks_no_ps_when_given_a_table(self):
        # The owner's start time comes from the hook's start_times() table, so
        # resolve_owner must not fork `ps` for it. The resolved-owner
        # assertion is the positive control: a counter on a path that returned
        # None early would prove nothing.
        pid = os.getpid()
        with open(os.path.join(self.tmp, "%d.json" % pid), "w") as handle:
            json.dump({"pid": pid, "sessionId": "sess-1", "name": "session-aa"}, handle)
        table = {12345: (pid, "/usr/bin/python3 h"), pid: (1, "/x/native-binary/claude")}
        calls = {"run": 0}
        real_run = identity_lib.subprocess.run

        def counted_run(*args, **kwargs):
            calls["run"] += 1
            return real_run(*args, **kwargs)

        identity_lib.subprocess.run = counted_run
        try:
            owner = identity_lib.resolve_owner(12345, table=table)
        finally:
            identity_lib.subprocess.run = real_run
        self.assertEqual(owner["pid"], pid)
        self.assertEqual(calls["run"], 0)

    def test_name_falls_back_to_session_id_when_registry_omits_it(self):
        pid = os.getpid()
        with open(os.path.join(self.tmp, "%d.json" % pid), "w") as handle:
            json.dump({"pid": pid, "sessionId": "sess-longer-than-eight"}, handle)
        table = {12345: (pid, "/usr/bin/python3 h"), pid: (1, "/x/claude")}
        owner = identity_lib.resolve_owner(12345, table=table)
        self.assertEqual(owner["name"], "sess-lon")


class TestProcessTableReader(unittest.TestCase):
    """The reader is wiring: the walk can only be right if the table it is
    handed carries whole executable paths. A fake `ps` answers each format the
    way the real one does, so reading `command=` (argv, args appended) instead
    of `comm=` loses the desktop app here, exactly as it did live."""

    def _fake_ps(self, rows):
        # rows: [(pid, ppid, executable, args)]
        def run(argv, **kwargs):
            fmt = argv[2] if len(argv) > 2 else ""
            lines = []
            for pid, ppid, exe, args in rows:
                if "comm=" in fmt:
                    lines.append("%5d %5d %s" % (pid, ppid, exe))
                else:
                    lines.append("%5d %5d %s %s" % (pid, ppid, exe, args))
            return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + "\n", "")
        return run

    def test_a_desktop_session_resolves_through_the_real_reader(self):
        rows = [(300, 200, "/usr/bin/python3", "/x/hook"),
                (200, 100, "/bin/zsh", "-c true"),
                (100, 1, DESKTOP_CLAUDE, "--output-format stream-json")]
        real_run = identity_lib.subprocess.run
        identity_lib.subprocess.run = self._fake_ps(rows)
        try:
            table = identity_lib.process_table()
        finally:
            identity_lib.subprocess.run = real_run
        self.assertEqual(table[100], (1, DESKTOP_CLAUDE))
        self.assertEqual(identity_lib.walk_to_claude(300, table), 100)

    def test_parse_keeps_a_path_with_spaces_whole(self):
        text = "  100     1 %s\n  200   100 /bin/zsh\n" % DESKTOP_CLAUDE
        table = identity_lib.parse_process_table(text)
        self.assertEqual(table, {100: (1, DESKTOP_CLAUDE), 200: (100, "/bin/zsh")})

    def test_parse_skips_malformed_lines(self):
        table = identity_lib.parse_process_table("garbage\n  12\n x y z\n  5 1 /bin/sh\n")
        self.assertEqual(table, {5: (1, "/bin/sh")})


class TestLiveSelfResolution(unittest.TestCase):
    """The one non-hermetic check, and it skips rather than fails."""

    def test_resolves_this_process_to_a_real_claude_when_running_under_one(self):
        table = identity_lib.process_table()
        if not table:
            self.skipTest("no process table available")
        pid = identity_lib.walk_to_claude(os.getpid(), table)
        if pid is None:
            self.skipTest("not running under a claude process")
        self.assertTrue(identity_lib.is_pid_live(pid, identity_lib.proc_start(pid)))


class TestNonUtf8Locale(unittest.TestCase):
    """The session file is UTF-8; a non-UTF-8 ambient locale must not mangle it."""

    def test_registry_entry_reads_a_non_ascii_session_file_under_a_non_utf8_locale(self):
        env = dict(os.environ, LC_ALL="C", LANG="C", PYTHONUTF8="0")
        env.pop("PYTHONIOENCODING", None)
        probe = subprocess.run(
            [sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
            env=env, stdout=subprocess.PIPE, check=True)
        if probe.stdout.decode("ascii").strip().lower().replace("-", "") == "utf8":
            self.skipTest("child locale still reports UTF-8")
        with tempfile.TemporaryDirectory() as tmp:
            payload = json.dumps({"cwd": "/Users/Zoë", "name": "x"}, ensure_ascii=False)
            with open(os.path.join(tmp, "12345.json"), "wb") as handle:
                handle.write(payload.encode("utf-8"))
            code = (
                "import json, sys; sys.path.insert(0, %r); import identity_lib; "
                "identity_lib.SESSIONS_DIR = %r; "
                "print(json.dumps(identity_lib.registry_entry(12345)))"
                % (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), tmp))
            out = subprocess.run([sys.executable, "-c", code], env=env,
                                 stdout=subprocess.PIPE, check=True)
        entry = json.loads(out.stdout.decode("ascii"))
        self.assertIsNotNone(entry)
        self.assertEqual(entry["cwd"], "/Users/Zoë")


if __name__ == "__main__":
    unittest.main()
