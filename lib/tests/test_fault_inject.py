"""Tests for the fault-injection counter."""

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import fault_inject
import test_install_fault_injection as sweep_tests

SKIP_REASON = "fault injection needs POSIX process groups and SIGKILL"
PREAMBLE = "import os, sys\nH = os.environ['FAULT_HOME']\n"


class FaultBase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.checkout = tempfile.mkdtemp()
        self.scratch = tempfile.mkdtemp()
        self.outside = tempfile.mkdtemp()
        self.base_bin = tempfile.mkdtemp()
        for path in (
            self.home, self.checkout, self.scratch, self.outside, self.base_bin
        ):
            self.addCleanup(shutil.rmtree, path, True)
        base = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("FAULT_")
        }
        base["HOME"] = self.home
        base["PATH"] = self.base_bin + os.pathsep + base.get("PATH", os.defpath)
        base["PYTHONDONTWRITEBYTECODE"] = "1"
        self.base_env = base
        self.env = fault_inject.install(base, self.home, self.checkout, self.scratch)
        self.runs = 0

    def fresh_env(self, at=None, **extra):
        """A copy of the fault env with its own log, so each probe counts alone."""
        self.runs += 1
        env = dict(self.env)
        env["FAULT_LOG"] = os.path.join(self.scratch, "log-%d" % self.runs)
        if at is not None:
            env["FAULT_AT"] = str(at)
        env.update(extra)
        return env

    def run_cmd(self, argv, env):
        proc = subprocess.run(
            argv,
            env=env,
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
        )
        return proc, fault_inject.events(env["FAULT_LOG"])

    def probe(self, code, at=None, **extra):
        env = self.fresh_env(at, **extra)
        return self.run_cmd([sys.executable, "-c", PREAMBLE + code], env)

    def tool(self, name, args, at=None, **extra):
        env = self.fresh_env(at, **extra)
        exe = os.path.join(self.scratch, "fault-bin", name)
        return self.run_cmd([exe] + list(args), env)

    def touch(self, directory, name, text=""):
        path = os.path.join(directory, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path


def kinds(found):
    return [kind for _n, kind, _detail in found]


@unittest.skipIf(os.name == "nt", SKIP_REASON)
class TestCounter(FaultBase):
    def test_three_opens_under_home_count_three(self):
        code = (
            "for name in 'abc':\n"
            "    fd = os.open(H + '/' + name, os.O_CREAT | os.O_WRONLY)\n"
            "    os.close(fd)\n"
        )
        proc, found = self.probe(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([n for n, _k, _d in found], [1, 2, 3])
        self.assertEqual(kinds(found), ["open"] * 3)

    def test_fault_at_kills_after_the_event_is_performed(self):
        code = (
            "for name in 'abc':\n"
            "    fd = os.open(H + '/' + name, os.O_CREAT | os.O_WRONLY)\n"
            "    os.close(fd)\n"
        )
        proc, found = self.probe(code, at=2)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(len(found), 2)
        self.assertTrue(os.path.exists(os.path.join(self.home, "b")))
        self.assertFalse(os.path.exists(os.path.join(self.home, "c")))

    def test_a_kill_at_an_in_place_open_leaves_the_file_truncated(self):
        # The state the sweep exists to reach: open(p, "w") has truncated the
        # file and the kill lands before any byte is written back.
        path = self.touch(self.home, "settings.json", '{"keep": true}\n')
        code = (
            "with open(H + '/settings.json', 'w') as handle:\n"
            "    handle.write('{\"new\": true}\\n')\n"
        )
        proc, found = self.probe(code, at=1)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(kinds(found), ["open"])
        with open(path) as handle:
            self.assertEqual(handle.read(), "")

    def test_write_outside_home_and_checkout_is_not_counted(self):
        code = (
            "p = %r + '/x'\n"
            "f = open(p, 'w'); f.write('x'); f.close()\n"
            "os.replace(p, p + '2')\n"
            "os.remove(p + '2')\n"
        ) % self.outside
        proc, found = self.probe(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(found, [])

    def test_checkout_writes_count_too(self):
        code = "open(os.environ['FAULT_CHECKOUT'] + '/f', 'w').close()\n"
        proc, found = self.probe(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(kinds(found), ["open", "close"])

    def test_path_that_only_shares_a_prefix_is_not_under_the_root(self):
        sibling = self.home + "-sibling"
        os.makedirs(sibling)
        self.addCleanup(shutil.rmtree, sibling, True)
        code = "open(%r + '/f', 'w').close()\n" % sibling
        proc, found = self.probe(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(found, [])

    def test_symlink_into_home_resolves_to_its_target(self):
        link = os.path.join(self.outside, "viahome")
        os.symlink(self.home, link)
        code = "open(%r + '/f', 'w').close()\n" % link
        proc, found = self.probe(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(kinds(found), ["open", "close"])

    def test_fault_off_counts_nothing(self):
        code = "open(H + '/f', 'w').close()\nos.mkdir(H + '/d')\n"
        proc, found = self.probe(code, FAULT_OFF="1", FAULT_AT="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(found, [])

    def test_failed_operation_is_not_counted(self):
        code = "try:\n    os.remove(H + '/missing')\nexcept OSError:\n    pass\n"
        proc, found = self.probe(code)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(found, [])

    def test_install_returns_a_new_env(self):
        original = dict(self.base_env)
        again = fault_inject.install(
            self.base_env, self.home, self.checkout, self.scratch
        )
        self.assertEqual(self.base_env, original)
        self.assertIsNot(again, self.base_env)
        self.assertTrue(
            again["PATH"].startswith(os.path.join(self.scratch, "fault-bin"))
        )
        self.assertEqual(again["FAULT_HOME"], os.path.realpath(self.home))
        self.assertEqual(again["FAULT_CHECKOUT"], os.path.realpath(self.checkout))

    def test_install_skips_a_tool_that_is_not_found(self):
        empty_bin = tempfile.mkdtemp()
        scratch = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty_bin, True)
        self.addCleanup(shutil.rmtree, scratch, True)
        fault_inject.install({"PATH": empty_bin}, self.home, self.checkout, scratch)
        self.assertEqual(os.listdir(os.path.join(scratch, "fault-bin")), [])

    def test_events_of_a_missing_log_is_empty(self):
        self.assertEqual(
            fault_inject.events(os.path.join(self.scratch, "nope.log")), []
        )


@unittest.skipIf(os.name == "nt", SKIP_REASON)
class TestToolWrappers(FaultBase):
    def init_repo(self):
        # Outside the fault env: setup is not part of any run.
        git = shutil.which("git")
        if git is None:
            self.skipTest("git not installed")
        for argv in (
            ["init", "-q"],
            ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
             "--allow-empty", "-m", "init"],
        ):
            subprocess.run(
                [git, "-C", self.checkout] + argv,
                env=self.base_env,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def test_read_only_git_is_not_counted(self):
        self.init_repo()
        for args in (
            ["-C", self.checkout, "rev-parse", "HEAD"],
            ["-C", self.checkout, "status", "--short"],
            ["--no-pager", "-C", self.checkout, "log", "-1"],
            ["-C", self.checkout, "-c", "core.quotepath=off", "diff", "--stat"],
            ["-C", self.checkout, "config", "--get", "user.name"],
            ["-C", self.checkout, "branch", "--show-current"],
            ["-C", self.checkout, "symbolic-ref", "HEAD"],
            ["-C", self.checkout, "worktree", "list"],
        ):
            with self.subTest(args=args):
                proc, found = self.tool("git", args)
                self.assertEqual(found, [], proc.stderr)

    def test_git_exit_code_is_passed_through(self):
        self.init_repo()
        proc, found = self.tool(
            "git", ["-C", self.checkout, "rev-parse", "--verify", "nope-ref"]
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(found, [])

    def test_mutating_git_is_counted_once(self):
        self.init_repo()
        proc, found = self.tool("git", ["-C", self.checkout, "checkout", "-q", "-b", "x"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(kinds(found), ["git"])
        branch = subprocess.run(
            ["git", "-C", self.checkout, "branch", "--show-current"],
            env=self.base_env,
            stdout=subprocess.PIPE,
            universal_newlines=True,
        ).stdout.strip()
        self.assertEqual(branch, "x")

    def test_git_kill_lands_after_the_checkout(self):
        self.init_repo()
        proc, found = self.tool(
            "git", ["-C", self.checkout, "checkout", "-q", "-b", "y"], at=1
        )
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        self.assertEqual(len(found), 1)
        branch = subprocess.run(
            ["git", "-C", self.checkout, "branch", "--show-current"],
            env=self.base_env,
            stdout=subprocess.PIPE,
            universal_newlines=True,
        ).stdout.strip()
        self.assertEqual(branch, "y")

    def test_ln_into_home_is_counted(self):
        target = self.touch(self.outside, "t")
        link = os.path.join(self.home, "link")
        proc, found = self.tool("ln", ["-sfn", target, link])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(kinds(found), ["ln"])
        self.assertTrue(os.path.islink(link))

    def test_ln_outside_the_roots_is_not_counted(self):
        target = self.touch(self.outside, "t")
        proc, found = self.tool("ln", ["-sfn", target, os.path.join(self.outside, "l")])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(found, [])

    def test_rm_cp_mkdir_mv_under_home_count_one_each(self):
        src = self.touch(self.home, "a")
        proc, found = self.tool("cp", ["-p", src, os.path.join(self.home, "b")])
        self.assertEqual((proc.returncode, kinds(found)), (0, ["cp"]), proc.stderr)
        proc, found = self.tool("mv", [src, os.path.join(self.home, "c")])
        self.assertEqual((proc.returncode, kinds(found)), (0, ["mv"]), proc.stderr)
        proc, found = self.tool("mkdir", ["-p", os.path.join(self.home, "d", "e")])
        self.assertEqual((proc.returncode, kinds(found)), (0, ["mkdir"]), proc.stderr)
        proc, found = self.tool("rm", ["-f", os.path.join(self.home, "c")])
        self.assertEqual((proc.returncode, kinds(found)), (0, ["rm"]), proc.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.home, "c")))

    def test_rm_of_a_link_into_home_counts_though_target_is_outside(self):
        target = self.touch(self.outside, "t")
        link = os.path.join(self.home, "l")
        os.symlink(target, link)
        proc, found = self.tool("rm", ["-f", link])
        self.assertEqual((proc.returncode, kinds(found)), (0, ["rm"]), proc.stderr)

    def write_claude_shim(self):
        shim = os.path.join(self.base_bin, "claude")
        with open(shim, "w") as handle:
            handle.write(
                "#!%s\n"
                "import os\n"
                "d = os.path.join(os.environ['HOME'], '.claude')\n"
                "os.makedirs(d, exist_ok=True)\n"
                "with open(os.path.join(d, 'settings.json'), 'w') as f:\n"
                "    f.write('{}')\n" % sys.executable
            )
        os.chmod(shim, 0o755)
        # The shim exists only now: rebuild the wrappers so `claude` is found.
        self.env = fault_inject.install(
            self.base_env, self.home, self.checkout, self.scratch
        )

    def test_claude_call_is_one_event_whatever_the_shim_writes(self):
        self.write_claude_shim()
        proc, found = self.tool("claude", ["plugin", "install", "x@y"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(kinds(found), ["claude"])
        self.assertIn("plugin install x@y", found[0][2])
        self.assertTrue(
            os.path.exists(os.path.join(self.home, ".claude", "settings.json"))
        )

    def test_claude_under_fault_off_counts_nothing(self):
        self.write_claude_shim()
        proc, found = self.tool("claude", ["plugin", "list"], FAULT_OFF="1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(found, [])


# Requirement 4a: (label, setup files, setup dirs, probe code, expected kinds).
COVERAGE = [
    ("open w + close", [], [],
     "f = open(H + '/p', 'w'); f.write('x'); f.close()",
     ["open", "close"]),
    ("with open w", [], [],
     "with open(H + '/p', 'w') as f:\n    f.write('x')",
     ["open", "close"]),
    ("os.open + fdopen + close", [], [],
     "fd = os.open(H + '/p', os.O_CREAT | os.O_WRONLY)\n"
     "f = os.fdopen(fd, 'w'); f.write('x'); f.close()",
     ["open", "close"]),
    ("NamedTemporaryFile", [], [],
     "import tempfile\n"
     "f = tempfile.NamedTemporaryFile(dir=H, delete=False)\n"
     "f.write(b'x'); f.close()",
     ["open", "close"]),
    ("os.replace", ["a"], [], "os.replace(H + '/a', H + '/b')", ["replace"]),
    ("os.rename", ["a"], [], "os.rename(H + '/a', H + '/b')", ["rename"]),
    ("os.remove", ["a"], [], "os.remove(H + '/a')", ["remove"]),
    ("os.unlink", ["a"], [], "os.unlink(H + '/a')", ["unlink"]),
    ("os.symlink", [], [], "os.symlink('target', H + '/l')", ["symlink"]),
    ("os.link", ["a"], [], "os.link(H + '/a', H + '/b')", ["link"]),
    ("os.mkdir", [], [], "os.mkdir(H + '/d')", ["mkdir"]),
    ("os.rmdir", [], ["d"], "os.rmdir(H + '/d')", ["rmdir"]),
    ("os.chmod", ["a"], [], "os.chmod(H + '/a', 0o600)", ["chmod"]),
    ("os.utime", ["a"], [], "os.utime(H + '/a', (1, 1))", ["utime"]),
    ("os.truncate", ["a"], [], "os.truncate(H + '/a', 0)", ["truncate"]),
    ("shutil.copystat", ["a", "b"], [],
     "import shutil\nshutil.copystat(H + '/a', H + '/b')",
     ["chmod", "utime"]),
    ("read-only open", ["a"], [], "open(H + '/a').read()", []),
]


@unittest.skipIf(os.name == "nt", SKIP_REASON)
class TestCoverageTable(FaultBase):
    def test_each_primitive_produces_exactly_its_events(self):
        for label, files, dirs, code, expected in COVERAGE:
            with self.subTest(primitive=label):
                for name in files:
                    self.touch(self.home, name, "x")
                for name in dirs:
                    os.mkdir(os.path.join(self.home, name))
                proc, found = self.probe(code)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                got = kinds(found)
                # copystat's chmod/utime order differs between Pythons.
                if label == "shutil.copystat":
                    got = sorted(got)
                self.assertEqual(got, expected, found)
                self.cleanup_home()

    def cleanup_home(self):
        for entry in os.listdir(self.home):
            path = os.path.join(self.home, entry)
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            else:
                os.remove(path)


@unittest.skipIf(os.name == "nt", SKIP_REASON)
class TestKillReport(FaultBase):
    COUNTED = "os.mkdir(H + '/a')\n"

    def reap(self, proc):
        # Whatever a test started must be gone when it ends, pass or fail.
        # Only an un-reaped outer's pid is still ours: once communicate() has
        # reaped it, the pid (and any inner's) may belong to another process.
        # A live inner shares the outer's group unless it left it, and the
        # one that leaves kills its own group.
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        proc.communicate()

    def outer(self, code, at=None):
        """An outer sh, in its own session, running the probe as its child."""
        pidfile = os.path.join(self.scratch, "inner.pid")
        env = self.fresh_env(at, FAULT_PIDFILE=pidfile)
        command = "%s -c %s; exit $?" % (
            shlex.quote(sys.executable), shlex.quote(PREAMBLE + code))
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command], env=env, start_new_session=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True)
        self.addCleanup(self.reap, proc)
        return proc, env["FAULT_LOG"], pidfile

    def test_a_normal_kill_reports_one_line_from_inside_the_group(self):
        env = self.fresh_env(1)
        proc, found = self.run_cmd(
            [sys.executable, "-c", PREAMBLE + self.COUNTED], env)
        self.assertEqual(-signal.SIGKILL, proc.returncode)
        reports = fault_inject.kill_report(env["FAULT_LOG"])
        self.assertEqual(1, len(reports))
        self.assertEqual("1", reports[0]["n"])
        self.assertEqual(reports[0]["pgrp"], reports[0]["sid"])
        self.assertEqual("", fault_inject.diagnose(-9, reports, len(found), 1))

    def test_an_emitter_outside_the_group_is_named(self):
        proc, log, _pidfile = self.outer("os.setpgid(0, 0)\n" + self.COUNTED, at=1)
        proc.communicate()
        self.assertNotEqual(0, proc.returncode)
        self.assertNotEqual(-signal.SIGKILL, proc.returncode)
        reports = fault_inject.kill_report(log)
        self.assertEqual(1, len(reports))
        self.assertNotEqual(reports[0]["pgrp"], reports[0]["sid"])
        text = fault_inject.diagnose(
            proc.returncode, reports, len(fault_inject.events(log)), 1)
        self.assertIn("CLASS emitter-outside-group", text)

    def test_a_stray_kill_leaves_no_report(self):
        code = (
            "with open(os.environ['FAULT_PIDFILE'], 'w') as f:\n"
            "    f.write(str(os.getpid()))\n"
            "import time\ntime.sleep(60)\n" + self.COUNTED)
        proc, log, pidfile = self.outer(code, at=1)
        for _ in range(200):
            if os.path.exists(pidfile) and os.path.getsize(pidfile):
                break
            time.sleep(0.05)
        with open(pidfile) as handle:
            os.kill(int(handle.read()), signal.SIGKILL)
        proc.communicate()
        self.assertNotEqual(0, proc.returncode)
        reports = fault_inject.kill_report(log)
        self.assertEqual([], reports)
        text = fault_inject.diagnose(
            proc.returncode, reports, len(fault_inject.events(log)), 1)
        self.assertIn("CLASS stray-kill", text)

    def test_a_kill_from_inside_the_group_that_missed_is_named(self):
        report = [{"n": "1", "pgrp": "50", "sid": "50"}]
        text = fault_inject.diagnose(137, report, 1, 1)
        self.assertIn("CLASS partial-group-kill", text)
        self.assertIn("returncode=137", text)

    def test_a_stale_report_from_another_kill_point_is_ignored(self):
        stale = [{"n": "3", "pgrp": "50", "sid": "60"}]
        text = fault_inject.diagnose(137, stale, 1, 1)
        self.assertIn("CLASS stray-kill", text)
        self.assertNotIn("emitter-outside-group", text)

    def test_assert_killed_carries_the_diagnosis(self):
        class Sweep(sweep_tests.TestFaultInjection):
            def setUp(self):
                pass

        log = os.path.join(self.scratch, "wired.log")
        with open(log + ".kill", "w") as handle:
            handle.write("n=1 pid=9 ppid=8 pgrp=9 sid=7 pgid_ppid=7 pgid_sid=7\n")
        proc = subprocess.CompletedProcess([], 137, stdout="out", stderr="err")
        sweep = Sweep("test_update_recovers_from_a_kill_at_every_event")
        with self.assertRaises(AssertionError) as caught:
            sweep.assert_killed(proc, [(1, "x", "y")], 1, log)
        self.assertIn("CLASS emitter-outside-group", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
