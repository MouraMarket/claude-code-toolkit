#!/usr/bin/env python3
"""Test-first harness for `ci/local-checks.sh`.

Builds a throwaway copy of the toolkit at HEAD (`git archive HEAD | tar -x`),
overlays the WORKING TREE's `ci/local-checks.sh` AND `install.sh` (both new
or modified and uncommitted, so they are not in HEAD), and runs the script
there — never against this checkout directly, since several cases need a
MODIFIED copy (a broken suite, an added probe pair) and none of them may
touch the real repo. `install.sh` is overlaid too because the installer
check depends on its (also uncommitted) CLAUDE_INSTALL_SKIP_SUITES support.

Runnable as:  cd ci && python3 tests/test_local_checks.py

The fixture needs its own git history, not just a plain extracted tree: the
script's "secret scan" check runs `claude-secret-scan --all`, which reads
`git ls-files`, and a bare `tar -x` has no `.git` at all.

The unmodified-copy case is slow — the full suite sweep, twice over (one per
Python version). Opt-in (LOCAL_CI_FULL=1): the same property is
also proved by every direct `ci/local-checks.sh` run on the real worktree.
Minutes, not seconds. Keep it; it is the only case that proves the whole
contract end to end rather than one rule in isolation.

Every OTHER case builds a SLIM fixture (`slim=True`): after extraction, every
real `tests/test_*.py` under tools/, lib/, skills/ AND ci/ is deleted except
whatever the case explicitly keeps (a probe suite it just added, or one small
real suite named in `keep_suites`). `installer` and `context lint` still run on a
slim copy and may legitimately PASS or FAIL depending on what's left standing
— a slim case asserts only the property it exists to prove (a suites check's
PASS/FAIL, a probe's own assertion, the overall count), never a blanket "all
seven PASSed". This is what keeps every case but the unmodified one fast: a
few seconds instead of the full suite sweep three times over.

A single case can be run alone, for mutation testing without paying for the
whole file:  cd ci && python3 tests/test_local_checks.py <Class>.<test>
"""
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))        # ci/tests
CI_DIR = os.path.dirname(HERE)                            # ci
REPO_ROOT = os.path.dirname(CI_DIR)                        # repo root
LOCAL_CHECKS = os.path.join(CI_DIR, "local-checks.sh")
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")

CHECK_RE = re.compile(r'^(PASS|FAIL) ([^:]+?)(?::\s*(.*))?$')
COUNT_RE = re.compile(r'^(\d+) checks run$')

# A probe suite that proves rule 4 (the per-pass PATH shim): a child process
# started with the bare name `python3` must resolve to the SAME version as
# the suite that spawned it, not whatever `python3` means on the ambient PATH.
PROBE_CHILD_VERSION = """\
import subprocess, sys, unittest

class T(unittest.TestCase):
    def test_child_python3_matches_this_interpreter(self):
        out = subprocess.run(
            ["python3", "-c", "import sys; print(tuple(sys.version_info[:2]))"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(out, str(tuple(sys.version_info[:2])))

if __name__ == "__main__":
    unittest.main()
"""

# A probe suite that proves rule 3 (the disposable HOME): HOME must be
# outside the temp SAFE_ROOTS claude-danger-guard trusts, and must not be the
# real HOME — checked via the password database, since os.path.expanduser("~")
# reads the same overridden $HOME and would prove nothing.
PROBE_DISPOSABLE_HOME = """\
import os, pwd, unittest

class T(unittest.TestCase):
    def test_home_is_disposable(self):
        home = os.environ["HOME"]
        for root in ("/tmp", "/private/tmp", "/var/folders"):
            self.assertFalse(home.startswith(root), home)
        real_home = pwd.getpwuid(os.getuid()).pw_dir
        self.assertNotEqual(home, real_home)

if __name__ == "__main__":
    unittest.main()
"""

# Fix 1: a failing suite whose OWN stdout contains a line that happens to
# start with "PASS " must not inflate the verdict count. The suite prints a
# line indistinguishable, by prefix, from the script's own PASS marker, then
# fails — proving the verdict must come from each check's own LAST line, not
# a grep across concatenated output.
PROBE_PASS_LINE_THEN_FAIL = """\
import sys

print("PASS intermediate assertion")
sys.exit(1)
"""

# A passing test print()s, then a later test fails. Block-buffered
# stdout lands the print AFTER unittest's stderr summary, so the failing
# suite's tail reads like a passing test's output.
PROBE_PRINT_THEN_FAIL = """\
import unittest


class T(unittest.TestCase):
    def test_a_prints(self):
        print("LOCAL_CI_ORDER_MARKER")

    def test_b_fails(self):
        self.fail("boom")


unittest.main()
"""

# The ci self-test's half of the cross-check overlap proof. Lives in
# the fixture's ci/tests (two levels below the root, not three) and records
# its interval under the label "ci" in the same file PROBE_OVERLAP writes to.
# It sleeps longer than PROBE_OVERLAP so a few seconds of start-up skew
# between checks cannot hide an overlap that happened.
PROBE_OVERLAP_CI = """\
import os, time, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

class T(unittest.TestCase):
    def test_record_interval(self):
        start = time.time()
        time.sleep(6)
        with open(os.path.join(ROOT, ".probe-overlap"), "a") as fh:
            fh.write("ci %f %f\\n" % (start, time.time()))

if __name__ == "__main__":
    unittest.main()
"""

# Proves suites inside ONE check run concurrently. Each copy appends
# "<major.minor> <start> <end>" to a file in the fixture root; run serially,
# two copies under the same version can never overlap in time.
PROBE_OVERLAP = """\
import os, sys, time, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

class T(unittest.TestCase):
    def test_record_interval(self):
        start = time.time()
        time.sleep(3)
        with open(os.path.join(ROOT, ".probe-overlap"), "a") as fh:
            fh.write("%d.%d %f %f\\n" % (sys.version_info[0], sys.version_info[1],
                                         start, time.time()))

if __name__ == "__main__":
    unittest.main()
"""

# A suite that never reports an exit status. It SIGKILLs the shell
# that started it (its parent), so the per-suite `.rc` file is never written,
# then exits 0 itself. The check must FAIL it, never count it as a pass.
PROBE_KILLS_ITS_RUNNER = """\
import os, signal

os.kill(os.getppid(), signal.SIGKILL)
"""

# Proves each check gets its own disposable HOME (rule 1 in the
# contract) and that the installer check skips the suite sweep
# (CLAUDE_INSTALL_SKIP_SUITES=1). Appends "<major.minor> <HOME>" to a marker
# file at a path fixed relative to this suite's own __file__, inside the
# fixture root — so every check that executes this suite leaves a line
# behind, regardless of which working directory it ran from.
PROBE_MARKER = """\
import os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))              # .../tools/x/tests
FIXTURE_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
MARKER = os.path.join(FIXTURE_ROOT, ".probe-calls")

class T(unittest.TestCase):
    def test_records_version_and_home(self):
        line = "%d.%d %s\\n" % (sys.version_info[0], sys.version_info[1],
                                 os.environ.get("HOME", ""))
        with open(MARKER, "a") as fh:
            fh.write(line)

if __name__ == "__main__":
    unittest.main()
"""

# Proves the interrupt trap. Records its own pid (one file per Python
# version, inside the fixture root) so the test can confirm the process is
# alive before sending SIGINT and gone afterward, then sleeps long enough to
# still be alive when the signal arrives.
PROBE_SLEEP_60S = """\
import os, sys, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))              # .../tools/x/tests
FIXTURE_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
PIDFILE = os.path.join(FIXTURE_ROOT, ".sleep-pid-%d.%d" % sys.version_info[:2])

class T(unittest.TestCase):
    def test_sleep_long(self):
        with open(PIDFILE, "w") as fh:
            fh.write(str(os.getpid()))
        time.sleep(60)

if __name__ == "__main__":
    unittest.main()
"""

# A suite that dies the way a full disk kills one,
# with the OS's own ENOSPC message, raised for real rather than printed.
PROBE_DISK_FULL = """\
import errno, os

raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
"""

ENVIRONMENT_PREFIX = "local-checks: environment: "

# B1: proves the self-test runs `ci/tests` suites with the nesting guard
# exported into THEIR environment (and only theirs). Appends what the suite
# sees in CLAUDE_LOCAL_CI_NESTED to a marker file in the fixture root.
PROBE_NESTED = """\
import os

HERE = os.path.dirname(os.path.abspath(__file__))              # .../ci/tests
FIXTURE_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
with open(os.path.join(FIXTURE_ROOT, ".probe-nested"), "a") as fh:
    fh.write("%s\\n" % os.environ.get("CLAUDE_LOCAL_CI_NESTED"))
"""

# B1: a ci suite that fails, with a marker the test looks for in stdout.
PROBE_CI_FAILS = """\
raise SystemExit("CI_SELFTEST_MARKER")
"""

# B1: a PATH shim `uv` that records its argv, then runs the real uv.
UV_SHIM = """\
#!/bin/sh
echo "$@" >> "$(dirname "$0")/../.uv-calls"
exec "{real_uv}" "$@"
"""

# B1 CR#1: the nested-run probe. Lives in the fixture's ci/tests, so the OUTER
# script's seventh check runs it; it runs the fixture's own local-checks.sh as a
# NESTED run (guard exported) whose suites include the heartbeat probe below.
PROBE_NESTED_RUN = """\
import os, subprocess, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))              # .../ci/tests
FIXTURE_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

class T(unittest.TestCase):
    def test_nested_run(self):
        # A sentinel FILE, not an env var: the nested script re-execs under
        # `env -i`, which would drop any variable this probe set for it.
        open(os.path.join(FIXTURE_ROOT, ".nested_probe"), "w").close()
        env = dict(os.environ, CLAUDE_LOCAL_CI_NESTED="1")
        subprocess.run(["./ci/local-checks.sh"], cwd=FIXTURE_ROOT, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       stdin=subprocess.DEVNULL)

if __name__ == "__main__":
    unittest.main()
"""

# B1 CR#1: appends a timestamp to <fixture>/.heartbeat every 0.2s for 120s —
# but only inside the NESTED run: <fixture>/.nested_probe must exist (only the
# nested probe creates it) AND CLAUDE_LOCAL_CI_NESTED must be "1", which only
# the nested run's suites see. The env check matters because the seventh
# check now runs alongside the outer suites checks, so the sentinel alone
# could let an OUTER suite beat, and the interrupt test would then pass
# without ever reaching the nested trap it exists to prove.
PROBE_HEARTBEAT = """\
import os, time, unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

class T(unittest.TestCase):
    def test_beat(self):
        if os.environ.get("CLAUDE_LOCAL_CI_NESTED") != "1":
            return
        if not os.path.exists(os.path.join(ROOT, ".nested_probe")):
            return
        path = os.path.join(ROOT, ".heartbeat")
        end = time.monotonic() + 120
        while time.monotonic() < end:
            with open(path, "a") as fh:
                fh.write("%f\\n" % time.time())
            time.sleep(0.2)

if __name__ == "__main__":
    unittest.main()
"""

# B2 CR (sibling review): imports a module that exists only in a temp dir the
# caller passes as PYTHONPATH.
PROBE_PYTHONPATH_IMPORT = """\
import unittest

class T(unittest.TestCase):
    def test_import(self):
        import only_in_caller_pythonpath  # noqa: F401

if __name__ == "__main__":
    unittest.main()
"""


def _discover_real_suites(root):
    """Relative paths (POSIX-style, matching extra_files keys) of every real
    suite under tools/, lib/, skills/, evals/, scripts/, ci/ in a fixture copy —
    mirrors the script's own `find tools lib skills evals scripts ...` and the
    self-test's `find ci ...`."""
    found = []
    for base in ("tools", "lib", "skills", "evals", "scripts", "ci"):
        base_dir = os.path.join(root, base)
        for dirpath, _dirs, files in os.walk(base_dir):
            if os.path.basename(dirpath) != "tests":
                continue
            for name in files:
                if name.startswith("test_") and name.endswith(".py"):
                    full = os.path.join(dirpath, name)
                    found.append(os.path.relpath(full, root).replace(os.sep, "/"))
    return found


def build_fixture(extra_files=None, break_suite=None, slim=False, keep_suites=()):
    """A throwaway, buildable copy of the toolkit at HEAD, as its own git repo.

    extra_files: {relative_path: content} written in before the commit —
    used to add a tiny probe suite under a new tools/x/tests/.
    break_suite: a relative path (e.g. "lib/tests/test_toolkit_config.py")
    inside the copy to make fail, by prepending
    `raise SystemExit("LOCAL_CI_DIAG_MARKER")` — the marker string also lets a
    test confirm the suite's own diagnostic output reached the script's stdout.
    slim: delete every real suite under tools/, lib/, skills/, evals/, scripts/, ci/ EXCEPT
    whatever `extra_files` just added and whatever `keep_suites` names —
    see the module docstring. Only the unmodified-copy case skips this.
    keep_suites: relative paths of real suites to retain in a slim copy.
    """
    tmp = tempfile.mkdtemp(prefix="local-checks-fixture-")
    archive = subprocess.run(
        ["git", "-C", REPO_ROOT, "archive", "HEAD"],
        capture_output=True, check=True)
    subprocess.run(["tar", "-x"], input=archive.stdout, cwd=tmp, check=True)

    os.makedirs(os.path.join(tmp, "ci"), exist_ok=True)
    dest = os.path.join(tmp, "ci", "local-checks.sh")
    shutil.copy(LOCAL_CHECKS, dest)
    os.chmod(dest, os.stat(LOCAL_CHECKS).st_mode | stat.S_IEXEC)

    install_dest = os.path.join(tmp, "install.sh")
    shutil.copy(INSTALL_SH, install_dest)
    os.chmod(install_dest, os.stat(INSTALL_SH).st_mode | stat.S_IEXEC)

    for rel, content in (extra_files or {}).items():
        path = os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(content)

    if slim:
        keep = set(extra_files or {}) | set(keep_suites)
        for rel in _discover_real_suites(tmp):
            if rel not in keep:
                os.remove(os.path.join(tmp, rel))

    if break_suite:
        # PREPEND, not append: every suite ends in
        # `if __name__ == "__main__": unittest.main()`, and unittest.main()
        # itself calls sys.exit() on success — so an appended raise is dead
        # code, never reached. A prepended raise aborts the module before
        # anything else runs, unconditionally.
        path = os.path.join(tmp, break_suite)
        with open(path) as fh:
            original = fh.read()
        with open(path, "w") as fh:
            fh.write('raise SystemExit("LOCAL_CI_DIAG_MARKER")\n' + original)

    env = dict(os.environ, GIT_AUTHOR_NAME="ci", GIT_AUTHOR_EMAIL="ci@localhost",
               GIT_COMMITTER_NAME="ci", GIT_COMMITTER_EMAIL="ci@localhost")
    subprocess.run(["git", "init", "-q"], cwd=tmp, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=tmp, check=True, env=env)
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "seed"],
        cwd=tmp, check=True, env=env)
    return tmp


def run_script(fixture, env_overrides=None, timeout=1800):
    # NESTED=1 by default so no fixture run starts the ci self-test (fixtures
    # carry ci/tests, so it would recurse). A case that needs the real
    # self-test passes {"CLAUDE_LOCAL_CI_NESTED": None}, which removes the key.
    env = dict(os.environ, CLAUDE_LOCAL_CI_NESTED="1")
    for key, value in (env_overrides or {}).items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return subprocess.run(
        ["./ci/local-checks.sh"], cwd=fixture, env=env,
        capture_output=True, text=True, timeout=timeout)


def parse_checks(stdout):
    """-> ([(status, name, reason), ...], trailing_count_or_None)."""
    checks = []
    count = None
    for line in stdout.splitlines():
        m = CHECK_RE.match(line)
        if m:
            checks.append((m.group(1), m.group(2), m.group(3) or ""))
            continue
        m = COUNT_RE.match(line)
        if m:
            count = int(m.group(1))
    return checks, count


class TestLocalChecksUnmodified(unittest.TestCase):
    """Slow — see module docstring. The one case proving the whole contract.

    Opt-in: set LOCAL_CI_FULL=1. A direct `time ci/local-checks.sh` run on
    the real worktree already proves this same property on every run, and
    paying for both roughly doubles the wall time."""

    @classmethod
    def setUpClass(cls):
        if os.environ.get("LOCAL_CI_FULL") != "1":
            raise unittest.SkipTest(
                "set LOCAL_CI_FULL=1 to run the full unmodified-copy sweep")
        cls.fixture = build_fixture()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.fixture, ignore_errors=True)

    def test_unmodified_copy_passes_all_seven_checks(self):
        result = run_script(self.fixture)
        checks, count = parse_checks(result.stdout)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(checks), 7, checks)
        self.assertTrue(all(status == "PASS" for status, _, _ in checks), checks)
        self.assertEqual(count, 7)
        self.assertEqual(result.stdout.strip().splitlines()[-1], "7 checks run")


class TestLocalChecksSkillBundles(unittest.TestCase):
    """The seventh check, `skill bundles`, runs scripts/check_bundles.py."""

    SKILL = "skills/mac/caffeinate/SKILL.md"

    def test_an_over_limit_description_fails_skill_bundles(self):
        fixture = build_fixture(slim=True)
        try:
            # Replace the front matter with one whose description is 1,100
            # characters, 76 over the 1,024 cap, so THIS skill is a problem
            # whatever else is already over a limit in the copy.
            with open(os.path.join(fixture, self.SKILL), "w") as fh:
                fh.write("---\nname: caffeinate\ndescription: %s\n---\n\n# x\n"
                         % ("d" * 1100))
            result = run_script(fixture)
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _ in checks}
        self.assertEqual(result.returncode, 1, out)
        self.assertEqual(count, 7, out)
        self.assertEqual(by_name.get("skill bundles"), "FAIL", out)
        self.assertIn(
            "FAIL %s: has a description of 1100 characters" % self.SKILL,
            result.stdout, out)
        self.assertRegex(result.stdout, r"(?m)^FAIL skill bundles: ", out)
        # A check this change did not touch still prints its own line.
        self.assertEqual(by_name.get("secret scan"), "PASS", out)


KEEP_SUITE = "lib/tests/test_toolkit_config.py"  # small, fast, real


class TestLocalChecksOneSuiteBroken(unittest.TestCase):
    def test_a_broken_suite_fails_both_suites_checks_but_others_still_run(self):
        fixture = build_fixture(slim=True, keep_suites=(KEEP_SUITE,),
                                 break_suite=KEEP_SUITE)
        try:
            result = run_script(fixture)
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        checks, count = parse_checks(result.stdout)
        by_name = {name: (status, reason) for status, name, reason in checks}
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(checks), 7, checks)
        self.assertEqual(count, 7)
        self.assertEqual(by_name["suites (3.9)"][0], "FAIL", by_name)
        self.assertEqual(by_name["suites (3.13)"][0], "FAIL", by_name)
        # The three checks that do not run the suite loop still print a line —
        # "never stopping at the first failure" applies to the whole script.
        for name in ("secret scan", "installer", "context lint", "skill bundles"):
            self.assertIn(name, by_name)
        # The broken suite's own diagnostic output (its SystemExit message)
        # must reach the script's stdout — not just its name and "failed".
        self.assertIn("LOCAL_CI_DIAG_MARKER", result.stdout, result.stdout)


class TestLocalChecksSuiteStdoutImpersonatesPassLine(unittest.TestCase):
    """Fix 1: a suite's own stdout can contain a line starting with "PASS ".
    The verdict must be decided from each check's own LAST line (which must
    equal exactly "PASS <name>"), never from a count of lines across the
    concatenated output — or this failing suite inflates the count and the
    script exits 0 with FAILs printed."""

    def test_suite_impersonating_a_pass_line_still_fails_the_check(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_impersonate.py": PROBE_PASS_LINE_THEN_FAIL,
        })
        try:
            result = run_script(fixture)
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        checks, count = parse_checks(result.stdout)
        # The impersonating suite's own diagnostic line ("PASS intermediate
        # assertion") is printed verbatim — the contract for PRINTED output
        # is unchanged — so it also matches CHECK_RE and inflates len(checks)
        # here. The property under test is the VERDICT, read by name, and the
        # overall exit code; not a count of every PASS/FAIL-shaped line.
        by_name = {name: (status, reason) for status, name, reason in checks}
        self.assertEqual(count, 7)
        self.assertEqual(by_name["suites (3.9)"][0], "FAIL",
                          result.stdout + result.stderr)
        self.assertEqual(by_name["suites (3.13)"][0], "FAIL",
                          result.stdout + result.stderr)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)


class TestLocalChecksFailingSuiteOutputStaysInOrder(unittest.TestCase):
    """A failing suite's printed log must keep its tests' stdout in
    order with unittest's stderr summary, or the tail reads like a pass."""

    def test_a_print_lands_before_the_failure_summary(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_order.py": PROBE_PRINT_THEN_FAIL,
        })
        try:
            result = run_script(fixture)
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        blocks = result.stdout.split("--- x/test_order.py ---")[1:]
        self.assertEqual(len(blocks), 2, result.stdout + result.stderr)  # 3.9 and 3.13
        for block in blocks:
            marker = block.find("LOCAL_CI_ORDER_MARKER")
            summary = block.find("FAILED (failures=1)")
            self.assertNotEqual(marker, -1, block)
            self.assertNotEqual(summary, -1, block)
            self.assertLess(marker, summary, block)


class TestLocalChecksMissingInterpreter(unittest.TestCase):
    def test_path_without_uv_or_python313_fails_only_the_313_suites_check(self):
        fixture = build_fixture(slim=True, keep_suites=(KEEP_SUITE,))
        try:
            shim = os.path.join(fixture, ".shim")
            os.makedirs(shim, exist_ok=True)
            # A literal `python3.9` binary, the way a GitHub runner's
            # actions/setup-python lays it out — simulates that layout without
            # depending on `uv`, so this case is independent of it.
            py39 = subprocess.run(
                ["uv", "python", "find", "--system", "3.9"],
                capture_output=True, text=True, check=True).stdout.strip()
            os.symlink(py39, os.path.join(shim, "python3.9"))
            # No uv, no ~/.local/bin (where python3.13 is symlinked on this
            # machine) — only system dirs plus the shim above.
            minimal_path = shim + ":/usr/bin:/bin:/usr/sbin:/sbin"
            result = run_script(fixture, env_overrides={"PATH": minimal_path})
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        checks, count = parse_checks(result.stdout)
        by_name = {name: (status, reason) for status, name, reason in checks}
        self.assertEqual(result.returncode, 1)
        self.assertEqual(count, 7)
        self.assertEqual(by_name["suites (3.13)"][0], "FAIL", by_name)


class TestLocalChecksChildVersionAndDisposableHome(unittest.TestCase):
    """One fixture proves rules 3 and 4 together: a probe suite spawning a
    bare `python3` child (rule 4), and a suite asserting HOME is the
    disposable one, not the real one and not a temp SAFE_ROOT (rule 3)."""

    def test_probe_suites_pass_under_both_versions(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_probe.py": PROBE_CHILD_VERSION,
            "tools/x/tests/test_home.py": PROBE_DISPOSABLE_HOME,
        })
        try:
            result = run_script(fixture)
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        checks, count = parse_checks(result.stdout)
        by_name = {name: (status, reason) for status, name, reason in checks}
        self.assertEqual(len(checks), 7, checks)
        self.assertEqual(count, 7)
        # Only the two suites checks are this case's property (rules 3 and
        # 4) — `installer`/`context lint` are free to PASS or FAIL on a slim
        # tree and are deliberately not asserted on here.
        self.assertEqual(by_name["suites (3.9)"][0], "PASS",
                          result.stdout + result.stderr)
        self.assertEqual(by_name["suites (3.13)"][0], "PASS",
                          result.stdout + result.stderr)


class TestLocalChecksConcurrency(unittest.TestCase):
    """The checks start concurrently -- both suites
    checks AND the ci self-test. Asserted as overlapping intervals recorded
    by the probes themselves, not as a wall-clock budget (a budget went red
    on a loaded machine for reasons that had nothing to do with this)."""

    def test_checks_run_concurrently_not_serially(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_overlap.py": PROBE_OVERLAP,
            "ci/tests/test_overlap_ci.py": PROBE_OVERLAP_CI,
        })
        try:
            result = run_script(fixture,
                                env_overrides={"CLAUDE_LOCAL_CI_NESTED": None})
            with open(os.path.join(fixture, ".probe-overlap")) as fh:
                rows = [ln.split() for ln in fh.read().splitlines() if ln.strip()]
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, count = parse_checks(result.stdout)
        self.assertEqual(len(checks), 7, out)
        self.assertEqual(count, 7, out)
        spans = {}
        for label, start, end in rows:
            spans.setdefault(label, []).append((float(start), float(end)))
        self.assertEqual(sorted(spans), ["3.13", "3.9", "ci"], rows)
        for label in spans:
            self.assertEqual(len(spans[label]), 1, rows)

        def overlap(a, b):
            (s1, e1), (s2, e2) = spans[a][0], spans[b][0]
            return s1 < e2 and s2 < e1

        self.assertTrue(overlap("3.9", "3.13"),
                        "the two suites checks ran one after another: %r\n%s"
                        % (spans, out))
        for version in ("3.9", "3.13"):
            self.assertTrue(overlap("ci", version),
                            "the ci self-test did not overlap suites (%s): %r\n%s"
                            % (version, spans, out))


class TestLocalChecksSuitesRunInParallel(unittest.TestCase):
    """Within one suites check, suites run concurrently (SUITE_JOBS),
    asserted as overlapping intervals, not as a wall-clock budget."""

    def test_two_suites_in_one_check_overlap(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_overlap_a.py": PROBE_OVERLAP,
            "tools/x/tests/test_overlap_b.py": PROBE_OVERLAP,
        })
        try:
            result = run_script(fixture)
            with open(os.path.join(fixture, ".probe-overlap")) as fh:
                rows = [ln.split() for ln in fh.read().splitlines() if ln.strip()]
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        by_version = {}
        for version, start, end in rows:
            by_version.setdefault(version, []).append((float(start), float(end)))
        self.assertEqual(sorted(by_version), ["3.13", "3.9"], rows)
        for version, spans in by_version.items():
            self.assertEqual(len(spans), 2, (version, rows))
            (s1, e1), (s2, e2) = spans
            self.assertTrue(s1 < e2 and s2 < e1,
                            "the two %s suites ran one after another: %r\n%s"
                            % (version, spans, out))


class TestLocalChecksUnfinishedSuiteFails(unittest.TestCase):
    """A suite whose exit status never comes back is a FAIL."""

    def test_suite_that_never_reports_fails_both_suites_checks(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_kills_runner.py": PROBE_KILLS_ITS_RUNNER,
        })
        try:
            result = run_script(fixture)
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, count = parse_checks(result.stdout)
        by_name = {name: (status, reason) for status, name, reason in checks}
        self.assertEqual(count, 7, out)
        for name in ("suites (3.9)", "suites (3.13)"):
            self.assertEqual(by_name[name][0], "FAIL", out)
            self.assertIn("x/test_kills_runner.py", by_name[name][1], out)
        self.assertIn("the suite did not finish", result.stdout, out)
        self.assertEqual(result.returncode, 1, out)


class TestLocalChecksDistinctHomeAndInstallerSkip(unittest.TestCase):
    """Rules 1 and 4: each check gets its own disposable HOME, and the
    installer check skips the suite sweep entirely (so the probe suite below
    runs exactly twice — once per suites pass — never a third time via
    install.sh's own internal loop)."""

    def test_two_suites_passes_distinct_homes_installer_skips(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_marker.py": PROBE_MARKER,
        })
        try:
            run_script(fixture)
            marker = os.path.join(fixture, ".probe-calls")
            with open(marker) as fh:
                lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        self.assertEqual(len(lines), 2, lines)
        versions = set()
        homes = set()
        for line in lines:
            version, home = line.split(" ", 1)
            versions.add(version)
            homes.add(home)
        self.assertEqual(versions, {"3.9", "3.13"}, lines)
        self.assertEqual(len(homes), 2, lines)


class TestLocalChecksCiSelfTest(unittest.TestCase):
    """B1: the seventh check runs the repo's own `ci/tests` suites (alongside
    the other six), with the nesting guard exported into
    THEIR environment only — and skips itself when it is already nested."""

    UNNESTED = {"CLAUDE_LOCAL_CI_NESTED": None}

    def _run(self, extra_files, env_overrides, keep_suites=(KEEP_SUITE,)):
        fixture = build_fixture(slim=True, extra_files=extra_files,
                                keep_suites=keep_suites)
        try:
            result = run_script(fixture, env_overrides=env_overrides)
            marker = os.path.join(fixture, ".probe-nested")
            lines = []
            if os.path.exists(marker):
                with open(marker) as fh:
                    lines = fh.read().splitlines()
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        checks, count = parse_checks(result.stdout)
        return result, {name: (status, reason) for status, name, reason in checks}, \
            checks, count, lines

    def test_runs_ci_suites_and_they_see_the_guard(self):
        result, by_name, checks, count, lines = self._run(
            {"ci/tests/test_probe_nested.py": PROBE_NESTED}, self.UNNESTED)
        out = result.stdout + result.stderr
        self.assertEqual(by_name.get("ci self-test", ("missing",))[0], "PASS", out)
        self.assertEqual(lines, ["1"], out)
        self.assertEqual(count, 7, out)
        self.assertEqual(len(checks), 7, out)
        self.assertEqual(result.returncode, 0, out)

    def test_a_nested_run_skips_the_self_test(self):
        result, by_name, checks, count, lines = self._run(
            {"ci/tests/test_probe_nested.py": PROBE_NESTED},
            {"CLAUDE_LOCAL_CI_NESTED": "1"})
        out = result.stdout + result.stderr
        self.assertEqual(by_name.get("ci self-test", ("missing",))[0], "PASS", out)
        self.assertIn(
            "ci self-test skipped: nested run (CLAUDE_LOCAL_CI_NESTED=1)",
            result.stdout.splitlines(), out)
        self.assertEqual(lines, [], out)
        self.assertEqual(count, 7, out)

    def test_a_failing_ci_suite_fails_the_check_and_the_others_still_print(self):
        result, by_name, checks, count, lines = self._run(
            {"ci/tests/test_probe_fails.py": PROBE_CI_FAILS}, self.UNNESTED)
        out = result.stdout + result.stderr
        self.assertEqual(by_name.get("ci self-test", ("missing",))[0], "FAIL", out)
        self.assertIn("CI_SELFTEST_MARKER", result.stdout, out)
        self.assertEqual(result.returncode, 1, out)
        for name in ("suites (3.9)", "suites (3.13)", "secret scan",
                     "installer", "context lint", "skill bundles"):
            self.assertIn(name, by_name, out)
        self.assertEqual(count, 7, out)

    def test_no_ci_suites_is_a_failure(self):
        result, by_name, checks, count, lines = self._run({}, self.UNNESTED)
        out = result.stdout + result.stderr
        self.assertEqual(
            by_name.get("ci self-test"),
            ("FAIL", "no ci/tests suites found"), out)
        self.assertEqual(result.returncode, 1, out)

    @unittest.skipUnless(shutil.which("uv"), "uv not on PATH")
    def test_uv_is_asked_for_system_interpreters_only(self):
        fixture = build_fixture(slim=True, keep_suites=(KEEP_SUITE,))
        try:
            shim = os.path.join(fixture, ".shim")
            os.makedirs(shim)
            shim_uv = os.path.join(shim, "uv")
            with open(shim_uv, "w") as fh:
                fh.write(UV_SHIM.format(real_uv=shutil.which("uv")))
            os.chmod(shim_uv, 0o755)
            result = run_script(
                fixture, env_overrides={"PATH": shim + ":" + os.environ["PATH"]})
            with open(os.path.join(fixture, ".uv-calls")) as fh:
                calls = [ln for ln in fh.read().splitlines() if ln.strip()]
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        finds = [c for c in calls if c.startswith("python find")]
        self.assertGreaterEqual(len(finds), 2, (calls, result.stdout))
        for call in finds:
            self.assertIn("--system", call.split(), calls)


class TestLocalChecksInterrupt(unittest.TestCase):
    """Rule 3: the EXIT/INT/TERM trap kills every still-running check's
    process group — no orphan may outlive the script."""

    def test_sigint_leaves_no_survivors(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_sleep_long.py": PROBE_SLEEP_60S,
        })
        pidfiles = {
            "3.9": os.path.join(fixture, ".sleep-pid-3.9"),
            "3.13": os.path.join(fixture, ".sleep-pid-3.13"),
        }
        proc = None
        pids = {}
        try:
            proc = subprocess.Popen(
                ["./ci/local-checks.sh"], cwd=fixture,
                env=dict(os.environ, CLAUDE_LOCAL_CI_NESTED="1"),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            # Bounded deadline polling, in the test process, until BOTH
            # suites passes have recorded a live pid — a fixed sleep races
            # against the fork and a fixed timeout alone can't tell "not yet"
            # from "never".
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and len(pids) < len(pidfiles):
                for v, pf in pidfiles.items():
                    if v in pids or not os.path.exists(pf):
                        continue
                    with open(pf) as fh:
                        content = fh.read().strip()
                    if content:
                        pids[v] = int(content)
                if len(pids) < len(pidfiles):
                    time.sleep(0.2)
            self.assertEqual(
                set(pids), set(pidfiles),
                "not every suites pass wrote a pid file: %r" % pids)

            for v, pid in pids.items():
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    self.fail(
                        "pid %d (%s) was not alive before SIGINT" % (pid, v))

            proc.send_signal(signal.SIGINT)
            try:
                proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("script did not exit within 20s of SIGINT")

            gone_deadline = time.monotonic() + 5
            remaining = dict(pids)
            while time.monotonic() < gone_deadline and remaining:
                for v, pid in list(remaining.items()):
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        del remaining[v]
                if remaining:
                    time.sleep(0.2)
            for v, pid in remaining.items():
                alive = True
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    alive = False
                self.assertFalse(
                    alive, "pid %d (%s) survived SIGINT" % (pid, v))
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
            for pid in pids.values():
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            shutil.rmtree(fixture, ignore_errors=True)


class TestLocalChecksTermIsEnvironment(unittest.TestCase):
    """A TERMed run is an interruption, not a red.
    The trap used to clean up and RETURN, so the script carried on, `cat`ed
    the out files it had just deleted, printed `7 checks run` and exited 1 —
    which claude-local-ci posts as a test failure."""

    def test_sigterm_reports_environment_and_no_count(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_sleep_long.py": PROBE_SLEEP_60S,
        })
        pidfiles = [os.path.join(fixture, ".sleep-pid-3.9"),
                    os.path.join(fixture, ".sleep-pid-3.13")]
        proc = None
        try:
            proc = subprocess.Popen(
                ["./ci/local-checks.sh"], cwd=fixture,
                env=dict(os.environ, CLAUDE_LOCAL_CI_NESTED="1"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # The checks must be mid-flight, or this tests an early exit.
            deadline = time.monotonic() + 20
            while (time.monotonic() < deadline
                   and not all(os.path.exists(pf) for pf in pidfiles)):
                time.sleep(0.2)
            self.assertTrue(all(os.path.exists(pf) for pf in pidfiles),
                            "the sleeping suites never started")
            proc.send_signal(signal.SIGTERM)
            try:
                stdout, stderr = proc.communicate(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("script did not exit within 20s of SIGTERM")
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
            shutil.rmtree(fixture, ignore_errors=True)
        out = stdout + stderr
        _checks, count = parse_checks(stdout)
        # Exact signal exit: proves the interrupt landed, not merely "non-zero".
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, out)
        self.assertIsNone(count, out)
        self.assertEqual(stdout.strip().splitlines()[-1],
                         ENVIRONMENT_PREFIX + "interrupted by SIGTERM", out)
        self.assertNotIn("No such file", out)


    @unittest.skipUnless(shutil.which("uv"), "uv not on PATH")
    def test_sigterm_while_interpreters_resolve_reports_environment(self):
        # Code Review finding: the traps went in only after interpreter
        # resolution, so a TERM there died silently. A slow `uv` shim holds
        # the script in that window; bash runs the trap once uv returns.
        fixture = build_fixture(slim=True, keep_suites=(KEEP_SUITE,))
        try:
            shim = os.path.join(fixture, ".shim")
            os.makedirs(shim)
            started = os.path.join(fixture, ".uv-started")
            with open(os.path.join(shim, "uv"), "w") as fh:
                fh.write('#!/bin/sh\ntouch "%s"\nsleep 3\nexec "%s" "$@"\n'
                         % (started, shutil.which("uv")))
            os.chmod(os.path.join(shim, "uv"), 0o755)
            proc = subprocess.Popen(
                ["./ci/local-checks.sh"], cwd=fixture,
                env=dict(os.environ, CLAUDE_LOCAL_CI_NESTED="1",
                         PATH=shim + ":" + os.environ["PATH"]),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline and not os.path.exists(started):
                    time.sleep(0.1)
                self.assertTrue(os.path.exists(started), "the uv shim never ran")
                proc.send_signal(signal.SIGTERM)
                stdout, stderr = proc.communicate(timeout=30)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = stdout + stderr
        self.assertEqual(proc.returncode, 128 + signal.SIGTERM, out)
        self.assertEqual(parse_checks(stdout), ([], None), out)
        self.assertEqual(stdout.strip().splitlines()[-1:],
                         [ENVIRONMENT_PREFIX + "interrupted by SIGTERM"], out)


class TestLocalChecksDiskFullIsEnvironment(unittest.TestCase):
    """A check that failed with ENOSPC is a full
    disk, not a red. Every line of the normal contract still prints (the
    diagnostics are the evidence), then one environment line, exit 2."""

    def test_enospc_in_a_failed_check_reports_environment(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_disk_full.py": PROBE_DISK_FULL,
        })
        try:
            result = run_script(fixture)
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _ in checks}
        self.assertEqual(by_name["suites (3.9)"], "FAIL", out)
        self.assertEqual(count, 7, out)
        self.assertIn("[Errno 28] No space left on device", result.stdout, out)
        self.assertEqual(result.stdout.strip().splitlines()[-1],
                         ENVIRONMENT_PREFIX + "disk full (No space left on device)", out)
        self.assertEqual(result.returncode, 2, out)

    def test_ordinary_failure_still_ends_on_the_count(self):
        # Positive control for the case above: same shape, no ENOSPC.
        fixture = build_fixture(slim=True, keep_suites=(KEEP_SUITE,),
                                break_suite=KEEP_SUITE)
        try:
            result = run_script(fixture)
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        self.assertEqual(result.stdout.strip().splitlines()[-1], "7 checks run", out)
        self.assertEqual(result.returncode, 1, out)


class TestLocalChecksNoRunDirectoryIsEnvironment(unittest.TestCase):
    """When CI_ROOT cannot be created (a full disk
    is one way), nothing below can run. It used to carry on with an empty
    CI_ROOT and report every check as a FAIL."""

    def test_unwritable_cache_reports_environment_before_any_check(self):
        fixture = build_fixture(slim=True, keep_suites=(KEEP_SUITE,))
        try:
            home = os.path.join(fixture, ".fakehome")
            os.makedirs(home)
            # A FILE where the cache directory must go: mkdir and mktemp fail.
            with open(os.path.join(home, ".cache"), "w") as fh:
                fh.write("not a directory\n")
            result = run_script(fixture, env_overrides={"HOME": home})
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, count = parse_checks(result.stdout)
        self.assertEqual(checks, [], out)
        self.assertIsNone(count, out)
        last = result.stdout.strip().splitlines()[-1]
        self.assertTrue(last.startswith(
            ENVIRONMENT_PREFIX + "cannot create the run directory"), out)
        self.assertEqual(result.returncode, 2, out)


class TestLocalChecksInterruptDuringSelfTest(unittest.TestCase):
    """B1 CR#1: an interrupt while the seventh check is running a NESTED
    local-checks.sh must not leave the nested run's suites alive. The outer
    trap has to give the nested script time to run its own trap."""

    def test_sigint_during_self_test_leaves_no_nested_survivors(self):
        token = "hb%s" % uuid.uuid4().hex
        fixture = build_fixture(slim=True, extra_files={
            "ci/tests/test_probe_nested_run.py": PROBE_NESTED_RUN,
            "tools/x/tests/test_%s.py" % token: PROBE_HEARTBEAT,
        })
        heartbeat = os.path.join(fixture, ".heartbeat")
        marker = "test_%s.py" % token
        proc = None
        try:
            env = dict(os.environ)
            env.pop("CLAUDE_LOCAL_CI_NESTED", None)
            proc = subprocess.Popen(
                ["./ci/local-checks.sh"], cwd=fixture, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and not os.path.exists(heartbeat):
                time.sleep(0.2)
            self.assertTrue(os.path.exists(heartbeat),
                            "heartbeat file never appeared")

            # Positive control: pgrep can see the live probe before the interrupt.
            before = subprocess.run(["pgrep", "-f", marker],
                                    capture_output=True, text=True)
            self.assertNotEqual(before.stdout.strip(), "",
                                "pgrep cannot see the live probe")

            proc.send_signal(signal.SIGINT)
            try:
                proc.communicate(timeout=40)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("script did not exit within 40s of SIGINT")

            size_a = os.path.getsize(heartbeat)
            time.sleep(2)
            size_b = os.path.getsize(heartbeat)
            self.assertEqual(size_a, size_b,
                             "heartbeat still growing after the script exited")
            after = subprocess.run(["pgrep", "-f", marker],
                                   capture_output=True, text=True)
            self.assertEqual(after.stdout.strip(), "",
                             "nested probe process survived: %s" % after.stdout)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.communicate()
            subprocess.run(["pkill", "-KILL", "-f", marker])
            shutil.rmtree(fixture, ignore_errors=True)


class TestLocalChecksIgnoresCallerPythonPath(unittest.TestCase):
    """CR (claude-skills sibling): a caller's PYTHONPATH must not reach the
    suites, or an undeclared import passes on this machine only."""

    def test_pythonpath_import_fails_both_suites_checks(self):
        extra = tempfile.mkdtemp(prefix="local-checks-pythonpath-")
        fixture = None
        try:
            with open(os.path.join(extra, "only_in_caller_pythonpath.py"), "w") as fh:
                fh.write("VALUE = 1\n")
            fixture = build_fixture(slim=True, extra_files={
                "tools/x/tests/test_pythonpath_import.py": PROBE_PYTHONPATH_IMPORT,
            })
            result = run_script(fixture, env_overrides={"PYTHONPATH": extra})
        finally:
            shutil.rmtree(extra, ignore_errors=True)
            if fixture:
                shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, _count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _r in checks}
        self.assertEqual(by_name.get("suites (3.9)"), "FAIL", out)
        self.assertEqual(by_name.get("suites (3.13)"), "FAIL", out)
        self.assertEqual(result.returncode, 1, out)



class TestLocalChecksIgnoresCallerPythonOptimize(unittest.TestCase):
    """Merge Review (claude-skills sibling): PYTHONOPTIMIZE strips plain
    asserts, so a suite whose only check is an assert would exit 0."""

    def test_pythonoptimize_does_not_strip_a_suite_assert(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_optimize_probe.py": 'assert False, "sentinel"\n',
        })
        try:
            result = run_script(fixture, env_overrides={"PYTHONOPTIMIZE": "1"})
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, _count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _r in checks}
        self.assertEqual(by_name.get("suites (3.9)"), "FAIL", out)
        self.assertEqual(by_name.get("suites (3.13)"), "FAIL", out)
        self.assertEqual(result.returncode, 1, out)



class TestLocalChecksCallerMarkerCannotBypassCleanEnv(unittest.TestCase):
    """Row-9 Code Review: a caller exporting LOCAL_CI_CLEAN_ENV must not skip
    the clean-environment re-exec (the marker is bound to the script's PID)."""

    def test_caller_marker_plus_pythonoptimize_still_fails_an_assert(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_optimize_probe.py": 'assert False, "sentinel"\n',
        })
        try:
            result = run_script(fixture, env_overrides={
                "LOCAL_CI_CLEAN_ENV": "1", "PYTHONOPTIMIZE": "1"})
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, _count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _r in checks}
        self.assertEqual(by_name.get("suites (3.9)"), "FAIL", out)
        self.assertEqual(by_name.get("suites (3.13)"), "FAIL", out)
        self.assertEqual(result.returncode, 1, out)


class TestLocalChecksNoSentinelCanSkipTheCleanEnv(unittest.TestCase):
    """Row-9 Merge Review: cleanliness is checked as a property, not trusted
    from a marker. (1) The reviewer's exact probe -- export a PID-shaped
    marker then exec the script -- must still clean. (2) The LOCAL_CI_REEXEC
    loop-guard variable no longer exists: the guard is privileged mode (`$-`
    contains p), which no environment variable can set, so a forged one is just
    dropped by the unconditional re-exec and cannot skip the clean environment."""

    def _fixture(self):
        return build_fixture(slim=True, extra_files={
            "tools/x/tests/test_optimize_probe.py": 'assert False, "sentinel"\n',
        })

    def test_exec_with_a_pid_marker_still_cleans(self):
        fixture = self._fixture()
        try:
            env = dict(os.environ, CLAUDE_LOCAL_CI_NESTED="1", PYTHONOPTIMIZE="1")
            result = subprocess.run(
                ["bash", "-c",
                 "export LOCAL_CI_CLEAN_ENV=$$; exec bash ci/local-checks.sh"],
                cwd=fixture, env=env, capture_output=True, text=True, timeout=1800)
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, _count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _r in checks}
        self.assertEqual(by_name.get("suites (3.9)"), "FAIL", out)
        self.assertEqual(result.returncode, 1, out)

    def test_forged_reexec_var_is_dropped_and_pythonoptimize_cleaned(self):
        fixture = self._fixture()
        try:
            result = run_script(fixture, env_overrides={
                "LOCAL_CI_REEXEC": "1", "PYTHONOPTIMIZE": "1"})
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, _count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _r in checks}
        self.assertEqual(by_name.get("suites (3.9)"), "FAIL", out)
        self.assertEqual(by_name.get("suites (3.13)"), "FAIL", out)
        self.assertEqual(result.returncode, 1, out)
        self.assertNotIn("environment not clean", out)


class TestLocalChecksInheritedStateCannotSwayTheCleanEnv(unittest.TestCase):
    """Automations Code Review: the re-exec inspects nothing, so inherited
    state cannot blind it. (1) GREP_OPTIONS=-q must not hide a leaked variable.
    (2) An exported bash function shadowing python3 must not make a failing
    suite pass. (3) A forged privileged start (`bash -p`) plus an exported
    function, with an otherwise allowlisted environment, must exit 2 with no
    PASS line. (4) Exported compgen()/builtin() cannot blind the check.
    (5) An exported exec() that returns aborts the script, never a false green."""

    def _bash(self, fixture, prelude, extra_env, allowlisted_only=False,
              run="exec bash ci/local-checks.sh"):
        if allowlisted_only:
            # Only what the script's second start accepts, so the exported
            # function is the sole reason for the exit 2.
            env = {k: os.environ[k] for k in ("HOME", "PATH", "USER", "LOGNAME",
                                              "LANG", "TMPDIR", "TERM")
                   if k in os.environ}
            env.update(CLAUDE_LOCAL_CI_NESTED="1", **extra_env)
        else:
            env = dict(os.environ, CLAUDE_LOCAL_CI_NESTED="1", **extra_env)
        return subprocess.run(
            ["bash", "-c", prelude + "; " + run],
            cwd=fixture, env=env, capture_output=True, text=True, timeout=1800)

    def test_grep_options_cannot_hide_a_leaked_variable(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_env_leak.py": PROBE_ENV_LEAK,
        })
        try:
            result = run_script(fixture, env_overrides={
                "GREP_OPTIONS": "-q", "LOCAL_CI_LEAK_PROBE": "1"})
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, _count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _r in checks}
        self.assertEqual(by_name.get("suites (3.9)"), "PASS", out)
        self.assertEqual(by_name.get("suites (3.13)"), "PASS", out)
        self.assertEqual(result.returncode, 0, out)

    def test_exported_function_cannot_shadow_the_test_runner(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_optimize_probe.py": 'assert False, "sentinel"\n',
        })
        try:
            result = self._bash(
                fixture, "python3() { return 0; }; export -f python3", {})
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, _count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _r in checks}
        self.assertEqual(by_name.get("suites (3.9)"), "FAIL", out)
        self.assertEqual(by_name.get("suites (3.13)"), "FAIL", out)
        self.assertEqual(result.returncode, 1, out)

    def test_forged_privileged_start_with_an_exported_function_exits_2(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_optimize_probe.py": 'assert False, "sentinel"\n',
        })
        try:
            result = self._bash(
                fixture, "python3() { return 0; }; export -f python3", {},
                allowlisted_only=True, run="exec bash -p ci/local-checks.sh")
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, out)
        self.assertNotRegex(result.stdout, r"(?m)^PASS ", out)
        self.assertIn("environment not clean after re-exec", out)
        self.assertIn("BASH_FUNC_python3", out)

    def test_exported_compgen_and_builtin_cannot_blind_the_check(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_optimize_probe.py": 'assert False, "sentinel"\n',
        })
        try:
            result = self._bash(
                fixture,
                "compgen() { return 0; }; builtin() { return 0; }; "
                "export -f compgen builtin",
                {"LOCAL_CI_REEXEC": "1", "LOCAL_CI_LEAK_PROBE": "1",
                 "PYTHONOPTIMIZE": "1"},
                run="bash ci/local-checks.sh")
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, _count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _r in checks}
        self.assertEqual(by_name.get("suites (3.9)"), "FAIL", out)
        self.assertEqual(by_name.get("suites (3.13)"), "FAIL", out)
        self.assertEqual(result.returncode, 1, out)

    def test_forged_privileged_start_with_exported_compgen_and_leak_exits_2(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_env_leak.py": PROBE_ENV_LEAK,
        })
        try:
            result = self._bash(
                fixture, "compgen() { return 0; }; export -f compgen",
                {"LOCAL_CI_LEAK_PROBE": "1"}, allowlisted_only=True,
                run="exec bash -p ci/local-checks.sh")
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        self.assertEqual(result.returncode, 2, out)
        self.assertNotRegex(result.stdout, r"(?m)^PASS ", out)
        self.assertIn("environment not clean after re-exec", out)
        self.assertIn("LOCAL_CI_LEAK_PROBE", out)

    def test_shadowed_exec_aborts_instead_of_running_unclean(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_optimize_probe.py": 'assert False, "sentinel"\n',
        })
        try:
            # No `exec` in the run command: it would call the shadow itself.
            result = self._bash(
                fixture, "exec() { return 0; }; export -f exec", {},
                allowlisted_only=True, run="bash ci/local-checks.sh")
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, out)
        self.assertNotRegex(result.stdout, r"(?m)^PASS ", out)
        self.assertIn("the clean re-exec did not happen", out)


PROBE_ENV_LEAK = (
    "import os\n"
    "leaked = [k for k in ('LOCAL_CI_LEAK_PROBE', 'LOCAL_CI_CLEAN_ENV')\n"
    "          if k in os.environ]\n"
    "assert not leaked, 'inherited into the suite: %s' % leaked\n"
)


class TestLocalChecksCleanEnvironment(unittest.TestCase):
    """An arbitrary inherited variable never reaches a suite (the class, not
    the PYTHON* instance)."""

    def test_arbitrary_inherited_variable_never_reaches_a_suite(self):
        fixture = build_fixture(slim=True, extra_files={
            "tools/x/tests/test_env_leak.py": PROBE_ENV_LEAK,
        })
        try:
            result = run_script(
                fixture, env_overrides={"LOCAL_CI_LEAK_PROBE": "1"})
        finally:
            shutil.rmtree(fixture, ignore_errors=True)
        out = result.stdout + result.stderr
        checks, _count = parse_checks(result.stdout)
        by_name = {name: status for status, name, _r in checks}
        self.assertEqual(by_name.get("suites (3.9)"), "PASS", out)
        self.assertEqual(by_name.get("suites (3.13)"), "PASS", out)
        self.assertEqual(result.returncode, 0, out)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
