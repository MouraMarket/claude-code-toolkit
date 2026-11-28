#!/usr/bin/env python3
"""Tests for `claude-heavy-run`, all through subprocess.

The environment is built explicitly (never from os.environ): HOME, the config
file and state_dir sit in a temp dir, and PATH holds a fake `sysctl`.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
WRAPPER = os.path.join(TOOL, "claude-heavy-run")
sys.path.insert(0, TOOL)

import load_guard_lib as lib  # noqa: E402


class HeavyRunCase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="heavy-run-test.")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_dir = os.path.join(self.tmp, "state")  # created by the tool
        self.lock_path = os.path.join(self.state_dir, "heavy-run.lock")
        self.bin = os.path.join(self.tmp, "bin")
        os.mkdir(self.bin)
        self.children = []
        self.addCleanup(self._reap)
        self.set_sysctl("55")  # 55% of 16 GB = 8.8 GB available
        self.floor = 3

    def _reap(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait()
            for stream in (child.stdout, child.stderr):
                if stream:
                    stream.close()

    def set_sysctl(self, level, memsize="17179869184", exit_code=0):
        """Fake sysctl answering both keys; 16 GiB of RAM unless told otherwise."""
        path = os.path.join(self.bin, "sysctl")
        with open(path, "w") as handle:
            handle.write(
                "#!/bin/sh\ncase \"$2\" in\n"
                "  kern.memorystatus_level) printf '%%s\\n' '%s' ;;\n"
                "  hw.memsize) printf '%%s\\n' '%s' ;;\n"
                "  *) exit 1 ;;\nesac\nexit %d\n" % (level, memsize, exit_code))
        os.chmod(path, 0o755)

    def env(self):
        config = os.path.join(self.tmp, "config.json")
        with open(config, "w") as handle:
            json.dump({"load_guard": {"state_dir": self.state_dir,
                                      "min_available_gb": self.floor}},
                      handle)
        return {"HOME": self.tmp, "PATH": self.bin + ":/usr/bin:/bin",
                "CLAUDE_TOOLKIT_CONFIG": config}

    def cmd(self, *argv):
        return [sys.executable, WRAPPER] + list(argv)

    def run_wrapper(self, *argv):
        return subprocess.run(
            self.cmd(*argv), env=self.env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30)

    def start_holder(self, *job):
        child = subprocess.Popen(
            self.cmd(*job), env=self.env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True)
        self.children.append(child)
        return child, self.wait_for_holder()

    def wait_for_holder(self):
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with open(self.lock_path) as handle:
                    holder = lib.parse_holder(handle.read())
            except OSError:
                holder = None
            if holder:
                return holder
            time.sleep(0.05)
        self.fail("no holder record appeared in the lock file")

    def kill(self, child):
        child.kill()
        child.wait()


class TestExitAndLock(HeavyRunCase):

    def test_a_exit_code_passes_through(self):
        self.assertEqual(self.run_wrapper("sh", "-c", "exit 7").returncode, 7)
        self.assertEqual(self.run_wrapper("--", "sh", "-c", "exit 0").returncode, 0)

    def test_b_second_run_refused_then_third_succeeds(self):
        child, holder = self.start_holder("sleep", "5")
        second = self.run_wrapper("sh", "-c", "exit 0")
        self.assertEqual(second.returncode, 75, second.stderr)
        self.assertIn(str(child.pid), second.stderr)
        self.assertIn("sleep", second.stderr)
        self.kill(child)
        # positive control: the refusal came from the held lock, not a broken run
        third = self.run_wrapper("sh", "-c", "exit 0")
        self.assertEqual(third.returncode, 0, third.stderr)

    def test_c_lock_survives_exec(self):
        pid_file = os.path.join(self.tmp, "job.pid")
        child, holder = self.start_holder(
            "sh", "-c", "echo $$ > '%s'; exec sleep 5" % pid_file)
        deadline = time.time() + 15
        while not os.path.exists(pid_file) and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)  # let the job finish its exec into sleep
        with open(pid_file) as handle:
            job_pid = int(handle.read())
        self.assertEqual(holder["pid"], job_pid)
        self.assertEqual(child.pid, job_pid)
        # the lock is held by the exec'd job, not released by the exec
        second = self.run_wrapper("sh", "-c", "exit 0")
        self.assertEqual(second.returncode, 75, second.stderr)


class TestMemoryFloor(HeavyRunCase):

    def marker_run(self, *prefix):
        marker = os.path.join(self.tmp, "ran")
        if os.path.exists(marker):
            os.remove(marker)
        result = self.run_wrapper(*(list(prefix) + ["touch", marker]))
        return result, os.path.exists(marker)

    def test_d_below_floor_refuses_and_job_never_runs(self):
        self.set_sysctl("10")  # 1.6 GB available, floor 3
        result, ran = self.marker_run()
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(ran)
        self.assertIn("1.6", result.stderr)
        self.assertIn("3.0", result.stderr)

    def test_d_above_floor_runs(self):
        self.set_sysctl("55")
        result, ran = self.marker_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ran)

    def test_needs_gb_larger_than_headroom_refuses(self):
        self.set_sysctl("38")  # 6.1 GB available, floor 3: 9 + 3 > 6.1
        result, ran = self.marker_run("--needs-gb", "9")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(ran)
        self.assertIn("9.0", result.stderr)  # the message names N

    def test_needs_gb_that_fits_runs(self):
        self.set_sysctl("38")  # 6.1 GB available: 2 + 3 <= 6.1
        result, ran = self.marker_run("--needs-gb", "2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ran)

    def test_needs_gb_accepts_double_dash_after(self):
        result, ran = self.marker_run("--needs-gb", "1", "--")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ran)

    def test_needs_gb_not_a_number_is_usage_error(self):
        for bad in ("abc", "0", "-2", "nan", "inf"):
            result, ran = self.marker_run("--needs-gb", bad)
            self.assertEqual(result.returncode, 2, (bad, result.stderr))
            self.assertFalse(ran)
        self.assertEqual(self.run_wrapper("--needs-gb").returncode, 2)

    def test_d_garbage_refuses(self):
        self.set_sysctl("garbage")
        result, ran = self.marker_run()
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(ran)
        self.assertIn("could not read", result.stderr)

    def test_d_unreadable_memsize_refuses(self):
        for bad in ("garbage", "", "0"):
            self.set_sysctl("55", memsize=bad)
            result, ran = self.marker_run()
            self.assertEqual(result.returncode, 75, (bad, result.stderr))
            self.assertFalse(ran)
            self.assertIn("could not read", result.stderr)

    def test_d_sysctl_failure_refuses(self):
        self.set_sysctl("55", exit_code=1)
        result, ran = self.marker_run()
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(ran)

    def test_d_default_floor_is_3_gb_without_config(self):
        self.floor = "not a number"  # unusable value falls back to the default
        self.set_sysctl("18")  # 2.88 GB available, under 3
        result, ran = self.marker_run()
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertFalse(ran)
        self.set_sysctl("19")  # 3.04 GB
        result, ran = self.marker_run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ran)


class TestStatus(HeavyRunCase):

    def test_e_status_free_then_held_then_free(self):
        free = self.run_wrapper("--status")
        self.assertEqual((free.returncode, free.stdout.strip()), (0, "free"))
        child, holder = self.start_holder("sleep", "5")
        held = self.run_wrapper("--status")
        self.assertEqual(held.returncode, 0)
        self.assertIn(str(child.pid), held.stdout)
        self.assertNotEqual(held.stdout.strip(), "free")
        self.kill(child)
        # the dead holder's record is still in the file; status must say free
        self.assertTrue(os.path.getsize(self.lock_path) > 0)
        after = self.run_wrapper("--status")
        self.assertEqual(after.stdout.strip(), "free")


class TestSameStateDir(HeavyRunCase):

    def test_f_runs_twice_against_same_state_dir(self):
        first = self.run_wrapper("sh", "-c", "exit 3")
        self.assertEqual(first.returncode, 3, first.stderr)
        self.assertTrue(os.path.isdir(self.state_dir))
        second = self.run_wrapper("sh", "-c", "exit 4")
        self.assertEqual(second.returncode, 4, second.stderr)

    def test_missing_command_is_127(self):
        self.assertEqual(self.run_wrapper("no-such-heavy-tool-xyz").returncode, 127)

    def test_no_arguments_is_usage_error(self):
        self.assertEqual(self.run_wrapper().returncode, 2)


if __name__ == "__main__":
    unittest.main()
