#!/usr/bin/env python3
"""Tests for `claude-heavy-run`, all through subprocess.

The environment is built explicitly (never from os.environ): HOME, the config
file and state_dir sit in a temp dir, and PATH holds a fake `sysctl`.
"""
import importlib.machinery
import importlib.util
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
        self.state_setting = self.state_dir  # what config says; tests override
        self.use_config = True

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

    def path_value(self):
        return self.bin + ":/usr/bin:/bin"

    def env(self, extra=None):
        config = os.path.join(self.tmp, "config.json")
        if self.use_config:
            with open(config, "w") as handle:
                json.dump({"load_guard": {"state_dir": self.state_setting,
                                          "min_available_gb": self.floor}},
                          handle)
        else:
            config = os.path.join(self.tmp, "no-such-config.json")
        env = {"HOME": self.tmp, "PATH": self.path_value(),
               "CLAUDE_TOOLKIT_CONFIG": config}
        env.update(extra or {})
        return env

    def cmd(self, *argv):
        return [sys.executable, WRAPPER] + list(argv)

    def run_wrapper(self, *argv, **kwargs):
        return subprocess.run(
            self.cmd(*argv), env=self.env(kwargs.get("extra")), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30)

    def start_holder(self, *job, **kwargs):
        child = subprocess.Popen(
            kwargs.get("cmd") or self.cmd(*job), env=self.env(), stdin=subprocess.DEVNULL,
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

    def test_d_unusable_configured_floor_falls_back_to_3_gb(self):
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
        with open(self.lock_path) as handle:
            record1 = lib.parse_holder(handle.read())
        self.assertEqual(record1["argv"], ["sh", "-c", "exit 3"])
        second = self.run_wrapper("sh", "-c", "exit 4")
        self.assertEqual(second.returncode, 4, second.stderr)
        with open(self.lock_path) as handle:
            record2 = lib.parse_holder(handle.read())
        # the second run rewrote the record rather than appending to the first
        self.assertEqual(record2["argv"], ["sh", "-c", "exit 4"])
        self.assertGreaterEqual(record2["started"], record1["started"])

    def test_missing_command_is_127(self):
        self.assertEqual(self.run_wrapper("no-such-heavy-tool-xyz").returncode, 127)

    def test_no_arguments_is_usage_error(self):
        self.assertEqual(self.run_wrapper().returncode, 2)


class TestNoConfig(HeavyRunCase):

    def test_default_state_dir_lands_under_home_and_floor_is_3(self):
        self.use_config = False
        marker = os.path.join(self.tmp, "ran")
        self.set_sysctl("18")  # 2.88 GB < default floor 3
        refused = self.run_wrapper("touch", marker)
        self.assertEqual(refused.returncode, 75, refused.stderr)
        self.assertFalse(os.path.exists(marker))
        self.set_sysctl("19")  # 3.04 GB
        ran = self.run_wrapper("touch", marker)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertTrue(os.path.exists(marker))
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, ".claude", "load-guard", "heavy-run.lock")))


class TestStateDirRule(HeavyRunCase):

    def test_relative_or_unexpanded_state_dir_refuses(self):
        marker = os.path.join(self.tmp, "ran")
        for bad in ("rel/dir", "$UNSET_LOAD_GUARD_VAR/x"):
            self.state_setting = bad
            result = self.run_wrapper("touch", marker)
            self.assertEqual(result.returncode, 75, (bad, result.stderr))
            self.assertIn("state_dir", result.stderr)
            self.assertFalse(os.path.exists(marker), bad)
        # positive control: an absolute value runs
        self.state_setting = self.state_dir
        self.assertEqual(self.run_wrapper("touch", marker).returncode, 0)
        self.assertTrue(os.path.exists(marker))


class ShimCase(HeavyRunCase):
    """A temp dir with a `faketool` shim symlink ahead of a fake real tool."""

    TOOL_NAME = "faketool"

    def setUp(self):
        super().setUp()
        self.build_shims()

    def build_shims(self):
        self.shimbin = os.path.join(self.tmp, "shimbin")
        self.realbin = os.path.join(self.tmp, "realbin")
        os.mkdir(self.shimbin)
        os.mkdir(self.realbin)
        self.marker = os.path.join(self.tmp, "marker")
        self.shim_path = os.path.join(self.shimbin, self.TOOL_NAME)
        os.symlink(WRAPPER, self.shim_path)
        real = os.path.join(self.realbin, self.TOOL_NAME)
        with open(real, "w") as handle:
            handle.write(
                "#!/bin/sh\n"
                "echo \"$$ $*\" >> \"$MARKER\"\n"
                "[ \"$1\" = slow ] && exec sleep 5\n"
                "[ \"$1\" = nap ] && sleep 2\n"
                "exit 0\n")
        os.chmod(real, 0o755)
        self.with_real = True

    def path_value(self):
        parts = [self.shimbin, self.bin]
        if self.with_real:
            parts.append(self.realbin)
        return ":".join(parts + ["/usr/bin", "/bin"])

    def env(self, extra=None):
        merged = {"MARKER": self.marker}
        merged.update(extra or {})
        return HeavyRunCase.env(self, merged)

    def shim_cmd(self, *args):
        return [self.shim_path] + list(args)

    def run_shim(self, *args, **kwargs):
        return subprocess.run(
            self.shim_cmd(*args), env=self.env(kwargs.get("extra")),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=30)

    def start_shim(self, *args):
        return self.start_holder(cmd=self.shim_cmd(*args))

    def wait_for_marker(self, count):
        """The holder record lands before the exec, so wait for the tool itself."""
        deadline = time.time() + 15
        while len(self.marker_lines()) < count and time.time() < deadline:
            time.sleep(0.05)
        self.assertGreaterEqual(len(self.marker_lines()), count)

    def marker_lines(self):
        if not os.path.exists(self.marker):
            return []
        with open(self.marker) as handle:
            return handle.read().splitlines()


class TestShim(ShimCase):

    def test_shim_runs_the_real_tool_under_the_lock(self):
        child, holder = self.start_shim("slow")
        self.assertEqual(holder["argv"], [self.TOOL_NAME, "slow"])
        self.assertEqual(holder["pid"], child.pid)
        self.wait_for_marker(1)
        second = self.run_shim("go")
        self.assertEqual(second.returncode, 75, second.stderr)
        self.assertIn(str(child.pid), second.stderr)
        self.assertEqual(len(self.marker_lines()), 1)  # only the holder ran
        self.kill(child)
        third = self.run_shim("go")  # positive control
        self.assertEqual(third.returncode, 0, third.stderr)
        self.assertTrue(self.marker_lines()[-1].endswith(" go"))

    def test_loop_of_three_shim_calls_runs_exactly_one(self):
        script = ("%s nap & %s nap & %s nap & wait"
                  % ((self.shim_path,) * 3))
        result = subprocess.run(
            ["sh", "-c", script], env=self.env(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30)
        self.assertEqual(len(self.marker_lines()), 1, result.stderr)
        self.assertEqual(result.stderr.count("another heavy job"), 2,
                         result.stderr)

    def test_only_the_shim_on_path_is_127_and_does_not_loop(self):
        self.with_real = False
        result = self.run_shim("go")  # a self-exec loop would hit the timeout
        self.assertEqual(result.returncode, 127, result.stderr)
        self.assertIn("not installed", result.stderr)
        self.assertEqual(self.marker_lines(), [])

    def test_held_env_passes_without_lock(self):
        child, holder = self.start_shim("slow")
        self.wait_for_marker(1)
        refused = self.run_shim("go")
        self.assertEqual(refused.returncode, 75, refused.stderr)  # control
        passed = self.run_shim("go", extra={"CLAUDE_HEAVY_RUN_HELD": "1"})
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertTrue(self.marker_lines()[-1].endswith(" go"))

    def test_held_env_skips_the_memory_floor_too(self):
        self.set_sysctl("1")
        refused = self.run_shim("go")
        self.assertEqual(refused.returncode, 75, refused.stderr)  # control
        passed = self.run_shim("go", extra={"CLAUDE_HEAVY_RUN_HELD": "1"})
        self.assertEqual(passed.returncode, 0, passed.stderr)

    def test_light_calls_pass_while_the_lock_is_held(self):
        child, holder = self.start_shim("slow")
        self.wait_for_marker(1)
        for flag in ("--version", "-version", "--help", "-h", "-help"):
            result = self.run_shim(flag)
            self.assertEqual(result.returncode, 0, (flag, result.stderr))
            self.assertTrue(self.marker_lines()[-1].endswith(" " + flag))
        # not light: a flag plus anything else, or a different flag
        for args in (("--version", "x"), ("-v",), ("--versions",)):
            result = self.run_shim(*args)
            self.assertEqual(result.returncode, 75, (args, result.stderr))

    def test_wrapped_job_can_call_a_shim_inside_it(self):
        result = self.run_wrapper("sh", "-c", "%s inner" % self.shim_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker_lines()[-1].endswith(" inner"))
        # also by bare name through PATH, the way a script would call it
        result = self.run_wrapper("sh", "-c", "%s inner2" % self.TOOL_NAME)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker_lines()[-1].endswith(" inner2"))

    def test_wrapper_sets_held_env_for_the_job(self):
        out = os.path.join(self.tmp, "held")
        result = self.run_wrapper(
            "sh", "-c", "echo \"$CLAUDE_HEAVY_RUN_HELD\" > '%s'" % out)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(out) as handle:
            self.assertEqual(handle.read().strip(), "1")

    def test_relative_state_dir_refuses_in_shim_mode_too(self):
        self.state_setting = "rel/dir"
        result = self.run_shim("go")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertIn("state_dir", result.stderr)
        self.assertEqual(self.marker_lines(), [])

    def test_shim_below_floor_refuses_and_tool_never_ran(self):
        self.set_sysctl("10")
        result = self.run_shim("go")
        self.assertEqual(result.returncode, 75, result.stderr)
        self.assertEqual(self.marker_lines(), [])


class TestLockFileReplaced(HeavyRunCase):
    """The inode re-check, tested in-process: the race cannot be timed from outside."""

    def load_wrapper(self):
        loader = importlib.machinery.SourceFileLoader("heavy_run_under_test",
                                                      WRAPPER)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def flock_that_replaces(self, module, times, recreate):
        """A fcntl stand-in whose flock deletes (and, if `recreate`, re-creates
        under a new inode) the lock file right after succeeding."""
        real_flock = module.fcntl.flock
        lock_path = self.lock_path
        calls = []

        class FakeFcntl(object):
            LOCK_EX = module.fcntl.LOCK_EX
            LOCK_NB = module.fcntl.LOCK_NB

            @staticmethod
            def flock(fd, op):
                real_flock(fd, op)
                calls.append(fd)
                if len(calls) <= times:
                    os.unlink(lock_path)
                    if recreate:
                        open(lock_path, "w").close()

        module.fcntl = FakeFcntl
        return calls

    def check_retry(self, recreate):
        module = self.load_wrapper()
        calls = self.flock_that_replaces(module, 1, recreate)
        outcome, fd = module._acquire(self.state_dir)
        self.addCleanup(os.close, fd)
        self.assertEqual(outcome, "ok")
        self.assertEqual(len(calls), 2)  # the first lock was on a dead inode
        mine, path = os.fstat(fd), os.stat(self.lock_path)
        self.assertEqual((mine.st_dev, mine.st_ino), (path.st_dev, path.st_ino))

    def test_replaced_lock_file_is_retried_and_the_new_inode_is_locked(self):
        self.check_retry(recreate=True)

    def test_deleted_lock_file_is_retried_and_recreated(self):
        self.check_retry(recreate=False)

    def test_lock_file_replaced_every_time_refuses_after_three_tries(self):
        module = self.load_wrapper()
        calls = self.flock_that_replaces(module, 99, True)
        outcome, fd = module._acquire(self.state_dir)
        self.assertEqual((outcome, fd), ("raced", None))
        self.assertEqual(len(calls), 3)


if __name__ == "__main__":
    unittest.main()
