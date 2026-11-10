"""Tests for the throughput bound and the decision log.

The breaker is the bound on every unknown in this design -- an untested bound
is not a bound, so these are not optional.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import breaker_lib
import identity_lib

DEAD_PID = 999999


class BreakerCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._b, self._l = breaker_lib.BREAKER_PATH, breaker_lib.LOG_PATH
        breaker_lib.BREAKER_PATH = os.path.join(self.tmp, "breaker.json")
        breaker_lib.LOG_PATH = os.path.join(self.tmp, "decisions.log")

    def tearDown(self):
        breaker_lib.BREAKER_PATH, breaker_lib.LOG_PATH = self._b, self._l


class TestBreaker(BreakerCase):
    """Counts are kept against LIVE pids only.

    These used to use the arbitrary pid 42. They cannot any more, and that is
    the point of the liveness change: breaker state for a process that no
    longer exists is discarded on sight, so a counting test has to name a
    process that is actually running. os.getpid() and os.getppid() are two
    such, and they are different from each other.
    """

    def setUp(self):
        super().setUp()
        self.pid = os.getpid()
        self.other = os.getppid()

    def test_starts_untripped(self):
        self.assertFalse(breaker_lib.is_tripped(self.pid, 3))

    def test_counts_blocks(self):
        self.assertEqual(breaker_lib.record_block(self.pid), 1)
        self.assertEqual(breaker_lib.record_block(self.pid), 2)

    def test_trips_at_the_limit(self):
        for _ in range(3):
            breaker_lib.record_block(self.pid)
        self.assertTrue(breaker_lib.is_tripped(self.pid, 3))

    def test_does_not_trip_below_the_limit(self):
        breaker_lib.record_block(self.pid)
        breaker_lib.record_block(self.pid)
        self.assertFalse(breaker_lib.is_tripped(self.pid, 3))

    def test_counts_are_per_pid(self):
        for _ in range(3):
            breaker_lib.record_block(self.pid)
        self.assertTrue(breaker_lib.is_tripped(self.pid, 3))
        self.assertFalse(breaker_lib.is_tripped(self.other, 3))

    def test_zero_limit_disables_the_breaker(self):
        # Documented as permitted: 0 removes the only bound on an identity bug.
        for _ in range(50):
            breaker_lib.record_block(self.pid)
        self.assertFalse(breaker_lib.is_tripped(self.pid, 0))

    def test_reset_clears_a_pid(self):
        for _ in range(3):
            breaker_lib.record_block(self.pid)
        breaker_lib.reset(self.pid)
        self.assertFalse(breaker_lib.is_tripped(self.pid, 3))

    def test_corrupt_state_is_treated_as_empty(self):
        with open(breaker_lib.BREAKER_PATH, "w") as handle:
            handle.write("{not json")
        self.assertFalse(breaker_lib.is_tripped(self.pid, 3))
        self.assertEqual(breaker_lib.record_block(self.pid), 1)

    def test_unwritable_state_never_raises(self):
        breaker_lib.BREAKER_PATH = "/proc/nope/breaker.json"
        breaker_lib.record_block(self.pid)   # must not raise
        self.assertFalse(breaker_lib.is_tripped(self.pid, 3))

    def test_the_real_proc_start_round_trips_and_still_trips(self):
        # The liveness check must not be so strict that it discards the state
        # of the session it was just written for.
        start = identity_lib.proc_start(self.pid)
        self.assertTrue(start)
        for _ in range(3):
            breaker_lib.record_block(self.pid, start)
        self.assertTrue(breaker_lib.is_tripped(self.pid, 3))


class TestBreakerLiveness(BreakerCase):
    """A crashed session must not disarm the guard for whoever inherits its pid.

    reset() only runs on the clean SessionEnd path. A crash, a force-quit or a
    VS Code reload leaves the entry behind, and pids are recycled -- so without
    liveness pruning, a brand-new session can start life with the guard already
    stood down and never know.
    """

    def _write(self, state):
        with open(breaker_lib.BREAKER_PATH, "w") as handle:
            json.dump(state, handle)

    def test_a_recycled_pid_does_not_inherit_a_tripped_breaker(self):
        # THE bug: same pid, different process. The recorded start time is what
        # tells them apart.
        pid = os.getpid()
        self._write({str(pid): {"count": 3,
                                "proc_start": "Thu Jan  1 00:00:00 1970"}})
        self.assertFalse(breaker_lib.is_tripped(pid, 3),
                         "a stale entry for a reused pid must not stand the "
                         "guard down for the new process")

    def test_a_dead_pids_entry_is_ignored(self):
        self._write({str(DEAD_PID): {"count": 9, "proc_start": None}})
        self.assertFalse(breaker_lib.is_tripped(DEAD_PID, 3))

    def test_a_dead_pids_entry_is_dropped_from_the_file(self):
        # Otherwise the file only ever grows.
        pid = os.getpid()
        self._write({str(DEAD_PID): {"count": 9, "proc_start": None}})
        breaker_lib.record_block(pid)
        with open(breaker_lib.BREAKER_PATH) as handle:
            state = json.load(handle)
        self.assertNotIn(str(DEAD_PID), state)
        self.assertIn(str(pid), state)

    def test_reset_also_flushes_dead_entries(self):
        pid = os.getpid()
        self._write({str(DEAD_PID): {"count": 9, "proc_start": None},
                     str(pid): {"count": 1, "proc_start": None}})
        breaker_lib.reset(pid)
        with open(breaker_lib.BREAKER_PATH) as handle:
            state = json.load(handle)
        self.assertEqual(state, {})

    def test_the_old_bare_int_format_is_treated_as_stale(self):
        # State written before entries carried a proc_start cannot be told
        # apart from a recycled pid, so it is dropped rather than trusted. The
        # safe direction: the guard comes back ARMED.
        pid = os.getpid()
        self._write({str(pid): 3})
        self.assertFalse(breaker_lib.is_tripped(pid, 3))
        self.assertEqual(breaker_lib.record_block(pid), 1)

    def test_junk_entries_do_not_raise(self):
        self._write({"not-a-pid": {"count": 2}, "12": "nope",
                     str(os.getpid()): {"count": "three"}})
        self.assertFalse(breaker_lib.is_tripped(os.getpid(), 3))
        self.assertEqual(breaker_lib.record_block(os.getpid()), 1)


class TestBreakerUnavailableTable(BreakerCase):
    """A failed ps must not wipe live counts: record_block persists them, and
    under sustained failure the breaker would never trip."""

    def setUp(self):
        super().setUp()
        self.child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(self.child.wait)
        self.addCleanup(self.child.kill)
        self.real_start = identity_lib.proc_start(self.child.pid)
        if self.real_start is None:
            self.skipTest("no ps")

    def test_a_count_survives_an_unavailable_table(self):
        breaker_lib.record_block(self.child.pid, self.real_start, starts={})
        self.assertEqual(breaker_lib.record_block(
            self.child.pid, self.real_start, starts={}), 2)

    def test_a_working_table_still_drops_a_missing_pid(self):
        # Control: a non-empty table lacking the pid is a positive answer.
        breaker_lib.record_block(self.child.pid, self.real_start)
        self.assertIs(breaker_lib.is_tripped(
            self.child.pid, 1, starts={1: "x"}), False)


class TestBreakerLocking(BreakerCase):

    def test_concurrent_record_block_does_not_lose_a_count(self):
        # record_block is a read-modify-write on a file every contending
        # session writes. Unlocked, two hooks blocking in the same instant both
        # read N and both write N+1, and the breaker ends up one block further
        # from standing down than it should be. Run it for real: 8 processes,
        # 8 increments, no fewer.
        #
        # Each worker raises LOCK_TIMEOUT. Every holder forks `ps` inside the
        # lock, so on a loaded machine 8 holders in a row exceed the real 1s and
        # the last waiters fall open -- the documented contract, not a race.
        # Left at 1s, this test measured machine load, not the lock (a
        # slower `ps` loses counts at 1s and not at 60s).
        # A missing lock still loses counts with the long timeout.
        pid = os.getpid()
        tool_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        program = (
            "import sys; sys.path.insert(0, %r)\n"
            "import breaker_lib, claim_lib\n"
            "claim_lib.LOCK_TIMEOUT = 30\n"
            "breaker_lib.BREAKER_PATH = %r\n"
            "breaker_lib.record_block(%d)\n" % (
                tool_dir, breaker_lib.BREAKER_PATH, pid))
        workers = [subprocess.Popen([sys.executable, "-c", program])
                   for _ in range(8)]
        for worker in workers:
            self.assertEqual(worker.wait(timeout=30), 0)
        with open(breaker_lib.BREAKER_PATH) as handle:
            state = json.load(handle)
        self.assertEqual(state[str(pid)]["count"], 8)

    def test_a_missing_parent_directory_is_created_not_fatal(self):
        # record_block creates a missing parent directory rather than failing.
        # The genuine unwritable-path case is covered by
        # test_unwritable_state_never_raises, which uses /proc/nope.
        breaker_lib.BREAKER_PATH = os.path.join(self.tmp, "deep", "breaker.json")
        self.assertEqual(breaker_lib.record_block(os.getpid()), 1)


class TestDecisionLog(BreakerCase):

    def test_writes_one_json_line_per_decision(self):
        breaker_lib.log_decision("block", pid=1, checkout="/a", reason="held")
        breaker_lib.log_decision("claim", pid=1, checkout="/b")
        with open(breaker_lib.LOG_PATH) as handle:
            lines = [json.loads(x) for x in handle if x.strip()]
        self.assertEqual([x["decision"] for x in lines], ["block", "claim"])
        self.assertEqual(lines[0]["checkout"], "/a")
        self.assertTrue(lines[0]["at"])

    def test_unwritable_log_never_raises(self):
        breaker_lib.LOG_PATH = "/proc/nope/decisions.log"
        breaker_lib.log_decision("block", pid=1)   # must not raise

    def test_log_is_capped(self):
        breaker_lib.LOG_MAX_BYTES = 400
        try:
            for i in range(200):
                breaker_lib.log_decision("claim", pid=i, checkout="/some/path")
            self.assertLess(os.path.getsize(breaker_lib.LOG_PATH),
                            breaker_lib.LOG_MAX_BYTES * 3)
        finally:
            breaker_lib.LOG_MAX_BYTES = 1_000_000

    def test_a_single_line_larger_than_the_target_still_caps(self):
        # Nothing guarantees one entry fits in half the cap. When it doesn't,
        # the file is emptied and only the newest line survives -- a hard
        # bound is the point, and this log is diagnostic, so dropping history
        # costs nothing a decision depends on.
        breaker_lib.LOG_MAX_BYTES = 200
        try:
            for i in range(10):
                breaker_lib.log_decision("block", pid=i, checkout="/x" * 300)
            self.assertLess(os.path.getsize(breaker_lib.LOG_PATH),
                            breaker_lib.LOG_MAX_BYTES * 20)
        finally:
            breaker_lib.LOG_MAX_BYTES = 1_000_000


if __name__ == "__main__":
    unittest.main()
