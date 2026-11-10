#!/usr/bin/env python3
"""Tests for portable.py, the thin Windows layer -- today, one lock.

Real processes, never mocked `fcntl`: the contention tests spawn an actual
child interpreter holding the lock, because a mocked `fcntl.flock` proves
nothing about whether two independent file descriptors on the same path
actually exclude each other.
"""

import ast
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
LIB_DIR = os.path.dirname(HERE)
PORTABLE_PATH = os.path.join(LIB_DIR, "portable.py")

sys.path.insert(0, LIB_DIR)
import portable  # noqa: E402


# A child that takes the lock, signals ready, and holds it until a release
# marker appears (bounded, so a broken test can't hang the suite forever).
CHILD_HOLDS_THE_LOCK = """
import sys
sys.path.insert(0, {lib_dir!r})
import os
import time
import portable

with portable.exclusive_lock({lock_path!r}) as acquired:
    if not acquired:
        raise SystemExit("child could not acquire a lock nothing else holds yet")
    with open({ready!r}, "w") as fh:
        fh.write("ready")
    deadline = time.monotonic() + 10
    while not os.path.exists({release!r}):
        if time.monotonic() > deadline:
            raise SystemExit("release marker never appeared within 10s")
        time.sleep(0.05)
"""


def _wait_for(path, timeout, message):
    """Bounded poll for a marker file -- never an unbounded wait."""
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise AssertionError(message)
        time.sleep(0.05)


class LockContentionTestCase(unittest.TestCase):
    """Base for the two tests that need a child process holding the lock."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.lock_path = os.path.join(self.tmp, "lock")
        self.ready = os.path.join(self.tmp, "ready")
        self.release = os.path.join(self.tmp, "release")
        script = CHILD_HOLDS_THE_LOCK.format(
            lib_dir=LIB_DIR, lock_path=self.lock_path,
            ready=self.ready, release=self.release)
        self.child = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _wait_for(self.ready, 10, "child never signalled it holds the lock")

    def tearDown(self):
        if not os.path.exists(self.release):
            open(self.release, "w").close()
        try:
            self.child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()
        self.child.stdout.close()
        self.child.stderr.close()


class TestUncontended(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_uncontended_lock_yields_true_and_the_file_exists(self):
        path = os.path.join(self.tmp, "lock")
        with portable.exclusive_lock(path) as acquired:
            self.assertTrue(acquired)
        self.assertTrue(os.path.isfile(path))


class TestTwoProcessContention(LockContentionTestCase):

    def test_a_second_process_is_excluded_until_the_first_releases(self):
        with portable.exclusive_lock(self.lock_path, timeout=0) as acquired:
            self.assertFalse(acquired)

        open(self.release, "w").close()
        self.child.wait(timeout=10)
        self.assertEqual(self.child.returncode, 0, self.child.stderr.read())

        with portable.exclusive_lock(self.lock_path) as acquired:
            self.assertTrue(acquired)


class TestTimeoutAttemptCount(LockContentionTestCase):

    def test_timeout_zero_makes_exactly_one_attempt(self):
        calls = []
        real_try_lock = portable._try_lock

        def counting(fd):
            calls.append(1)
            return real_try_lock(fd)

        with mock.patch.object(portable, "_try_lock", side_effect=counting):
            with portable.exclusive_lock(self.lock_path, timeout=0) as acquired:
                self.assertFalse(acquired)
        self.assertEqual(len(calls), 1)

    def test_a_positive_timeout_retries_for_at_least_that_long(self):
        calls = []
        real_try_lock = portable._try_lock

        def counting(fd):
            calls.append(1)
            return real_try_lock(fd)

        with mock.patch.object(portable, "_try_lock", side_effect=counting):
            start = time.monotonic()
            with portable.exclusive_lock(self.lock_path, timeout=0.3) as acquired:
                self.assertFalse(acquired)
            elapsed = time.monotonic() - start

        self.assertGreater(len(calls), 1)
        # Lower bound only -- never an upper bound (the repo's no-wall-clock rule).
        self.assertGreaterEqual(elapsed, 0.25)


class TestUnopenablePath(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_unopenable_path_raises_oserror_from_the_with_statement(self):
        parent = os.path.join(self.tmp, "not_a_dir")
        with open(parent, "w"):
            pass
        path = os.path.join(parent, "lock")
        with self.assertRaises(OSError):
            with portable.exclusive_lock(path):
                self.fail("body must not run when open() fails")


class TestReleaseOnExit(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_a_second_lock_in_the_same_process_succeeds_after_the_first_exits(self):
        path = os.path.join(self.tmp, "lock")
        with portable.exclusive_lock(path) as acquired:
            self.assertTrue(acquired)
        with portable.exclusive_lock(path) as acquired:
            self.assertTrue(acquired)


class TestExceptionInBodyStillReleases(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_an_exception_in_the_body_propagates_and_the_lock_is_released(self):
        path = os.path.join(self.tmp, "lock")

        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with portable.exclusive_lock(path) as acquired:
                self.assertTrue(acquired)
                raise Boom("boom")

        with portable.exclusive_lock(path) as acquired:
            self.assertTrue(acquired)


class TestNoTopLevelPlatformImport(unittest.TestCase):
    """Acceptance criterion: importing portable must never fail on either OS."""

    def test_fcntl_and_msvcrt_are_not_imported_at_module_level(self):
        with open(PORTABLE_PATH, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=PORTABLE_PATH)
        names = set()
        for node in tree.body:  # top level only -- nested imports are fine
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module)
        self.assertNotIn("fcntl", names)
        self.assertNotIn("msvcrt", names)


if __name__ == "__main__":
    unittest.main()
