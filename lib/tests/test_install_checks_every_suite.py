#!/usr/bin/env python3
"""`./install.sh --check` must run EVERY suite, not the ones before the first
one that reads stdin.

The failure this pins is the worst shape a check can have: a green check that
silently skipped most of its work. A suite loop of the shape

    while read -r suite; do ... done < <(find ... | sort)

reads its queue of remaining suites on stdin. Any suite that reads stdin (a
SessionStart hook draining its event payload, say) consumes the rest of that
queue. The loop then hits EOF, prints nothing about the suites it never
reached, and exits 0 with "Check complete." The fix is `</dev/null` on the
suite invocation, and a sweep that compares what completed against what was
discovered.

Two guards, because either alone can rot:
  * TestSuiteRunnerIsolatesStdin: the redirect is present, AND the loop is
    still stdin-fed (a positive control: without it the redirect could be
    pinned long after it stopped protecting anything).
  * TestTheRedirectActuallyPreventsTruncation: the same loop shape, built in
    a fixture with a deliberately stdin-eating "suite", asserting the redirect
    is what keeps the queue intact.

Deliberately NOT here: an end-to-end `./install.sh --check` run of THIS repo.
This module lives in `lib/tests/`, which `--check` executes, so shelling out to
it would recurse, each level forking a full suite sweep. The fixture below
runs the real installer against a stub repo with stand-in suites instead.
"""
import os
import re
import subprocess
import sys
import shutil
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import install_fixture as fx  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")

# Every place the repo runs the whole suite through a stdin-fed loop. Fixing
# one and leaving the others is the defect this list exists to stop: the check
# that gates a merge (ci/local-checks.sh) is as exposed as install.sh.
SUITE_RUNNERS = {
    "install.sh": INSTALL_SH,
    "ci/local-checks.sh": os.path.join(REPO_ROOT, "ci", "local-checks.sh"),
}
# The repository's own instructions document the same loop for a reader to
# run. CLAUDE.md, AGENTS.md and README.md each document the loop, and each
# documented loop is checked.
INSTRUCTION_RUNNERS = {
    "CLAUDE.md": os.path.join(REPO_ROOT, "CLAUDE.md"),
    "AGENTS.md": os.path.join(REPO_ROOT, "AGENTS.md"),
}
README_RUNNERS = {
    "README.md": os.path.join(REPO_ROOT, "README.md"),
}

# The line in each file that invokes one suite, matched on SHAPE rather than
# on the loop variable's name. A guard whose coverage depends on what someone
# named their loop variable is a guard that silently shrinks.
RUNNER_RE = re.compile(r'python3 "tests/\$\(basename "\$\w+"\)"')

# A loop fed by a find on stdin, anchored to a line start. Anchoring is the
# whole point: an unanchored search matched the PROSE of a comment describing
# the loop ("`done < <(find ...)` below"), so restructuring the real loop left
# the control green. Two shapes: process substitution (`done < <(find ...)`)
# and a pipe into `while read`, tolerant of extra find predicates such as
# `-type f` (a stricter pattern reported that as "no longer stdin-fed", the
# false-alarm direction that gets a guard deleted).
FED_BY_FIND_RE = re.compile(
    r"^\s*(done < <\(find "
    r"|find .*-path '\*/tests/test_\*\.py'.*\|\s*sort\s*\|"
    r"\s*while read)",
    re.M)


def suite_runner_lines(path):
    """Every line in `path` that invokes one test suite inside a loop."""
    with open(path, encoding="utf-8") as fh:
        return [l for l in fh if RUNNER_RE.search(l)]


class RunnerChecks:
    """The two checks, for one dict of runners."""

    RUNNERS = {}

    def test_every_documented_runner_redirects_stdin_from_devnull(self):
        for label, path in self.RUNNERS.items():
            with self.subTest(runner=label):
                lines = suite_runner_lines(path)
                self.assertTrue(
                    lines,
                    f"no suite-runner line found in {label}: if the loop was "
                    f"restructured, re-derive this guard against the new shape "
                    f"rather than deleting it")
                for line in lines:
                    self.assertIn(
                        "</dev/null", line,
                        f"{label} runs a suite without </dev/null. The loop "
                        f"reads the suite list on stdin, so a suite that reads "
                        f"stdin eats the rest of the queue and the run exits 0 "
                        f"having covered only a prefix. See this module's "
                        f"docstring.")

    def test_the_loops_really_are_fed_by_a_find_on_stdin(self):
        """Positive control. If a loop stopped being stdin-fed, the guard above
        would be pinning a redirect that no longer protects anything: green,
        and meaningless."""
        for label, path in self.RUNNERS.items():
            with self.subTest(runner=label):
                with open(path, encoding="utf-8") as fh:
                    body = fh.read()
                self.assertRegex(
                    body, FED_BY_FIND_RE,
                    f"{label}'s loop is no longer fed by a find on stdin; its "
                    f"</dev/null guard may now be vestigial. Re-derive both.")


class TestSuiteRunnerIsolatesStdin(RunnerChecks, unittest.TestCase):
    RUNNERS = SUITE_RUNNERS

    def test_ci_compares_what_ran_against_what_was_discovered(self):
        """`ran -eq 0` cannot see a PARTIAL sweep: a truncated queue just
        reports a smaller number. Only a comparison against the discovered
        count catches it, and ci/local-checks.sh is the check that gates a
        merge. One comparison per suite loop: the file runs more than one, and
        a presence check alone stays green when one loop loses its compare."""
        path = SUITE_RUNNERS["ci/local-checks.sh"]
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        loops = len(suite_runner_lines(path))
        self.assertGreater(loops, 0, "no suite loops found to compare against")
        self.assertEqual(
            loops, body.count('"$ran" -ne "$discovered"'),
            "ci/local-checks.sh has %d suite loops but not one ran-vs-discovered "
            "comparison each, so a truncated run reports a smaller number and "
            "exits 0." % loops)


class TestTheRepoInstructionsRunnersIsolateStdin(RunnerChecks, unittest.TestCase):
    RUNNERS = INSTRUCTION_RUNNERS


class TestTheReadmeRunnerIsolatesStdin(RunnerChecks, unittest.TestCase):
    RUNNERS = README_RUNNERS


class TestTheRedirectActuallyPreventsTruncation(unittest.TestCase):
    """Behavioural control on the mechanism itself.

    Without this, the guards above are string matches that could both be
    satisfied by a redirect that does not do what we think it does.
    """

    def _run_loop(self, redirect):
        tmp = tempfile.mkdtemp(prefix="check-loop-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        eater = os.path.join(tmp, "eater.py")
        with open(eater, "w") as fh:
            fh.write("import sys; sys.stdin.read()\n")
        # A while-read loop fed by a queue on stdin, running a stdin-reading
        # child for the first item only.
        script = textwrap.dedent(f"""
            seen=0
            while read -r item; do
              seen=$((seen+1))
              python3 {eater} >/dev/null 2>&1 {redirect}
            done < <(printf 'a\\nb\\nc\\nd\\ne\\n')
            echo "SEEN=$seen"
        """)
        out = subprocess.run(["bash", "-c", script], capture_output=True,
                             text=True, stdin=subprocess.DEVNULL, timeout=60)
        return out.stdout.strip()

    def test_without_the_redirect_the_queue_is_eaten(self):
        self.assertEqual(
            self._run_loop(""), "SEEN=1",
            "the fixture failed to reproduce the truncation, so the test below "
            "proves nothing. Re-derive both against the real loop shape")

    def test_with_the_redirect_every_item_is_reached(self):
        self.assertEqual(self._run_loop("</dev/null"), "SEEN=5")


class TestSweepCountsWhatItRan(unittest.TestCase):
    """The real installer, a fixture repo, the claude shim, suites opted IN.

    The sweep must compare suites COMPLETED against suites DISCOVERED, so a
    sweep that found nothing, or stopped early, is a red run rather than a
    quiet one. The fixture's own suites are tiny stand-ins, never this repo's.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = os.path.join(self.root, "home")
        self.repo = os.path.join(self.root, "repo")
        for sub in ("claude", "tools", "lib/tests"):
            os.makedirs(os.path.join(self.repo, sub))
        os.symlink(INSTALL_SH, os.path.join(self.repo, "install.sh"))
        os.symlink(os.path.join(REPO_ROOT, "lib", "worktree_detect.sh"),
                   os.path.join(self.repo, "lib", "worktree_detect.sh"))
        with open(os.path.join(self.repo, "claude", "permissions.baseline.json"), "w") as fh:
            fh.write('{"permissions": {}}\n')
        fx.add_plugin_layout(self.repo)
        config_dir = os.path.join(self.home, ".config", "claude-code-toolkit")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "config.json"), "w") as fh:
            fh.write("{}")
        self.bin_dir = os.path.join(self.root, "shimbin")
        self.state = os.path.join(self.root, "shim-state.json")
        fx.install_claude_shim(self.bin_dir, self.state)
        fx.seed_installed(self.state, self.repo)
        # --check fails on a missing link source or an absent link.
        fx.add_link_targets(self.repo, self.home)
        fx.install_links(self.repo, self.home)
        self.managed = os.path.join(self.root, "managed")
        os.makedirs(self.managed)

    def add_suite(self, name, body):
        path = os.path.join(self.repo, "lib", "tests", name)
        with open(path, "w") as fh:
            fh.write(body)

    def base_env(self):
        env = dict(os.environ)
        for key in [k for k in env if k.startswith("GIT_")]:
            del env[key]
        env.pop("ALLOW_WORKTREE_INSTALL", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.update({"HOME": self.home, "USERPROFILE": self.home,
                    "CLAUDE_TOOLKIT_CONFIG": "/nonexistent.json",
                    "CLAUDE_TOOLKIT_MANAGED_DIR": self.managed})
        return env

    def run_install(self):
        env = fx.shim_env(self.base_env(), self.bin_dir, self.state, skip_suites=False)
        env.pop("CLAUDE_INSTALL_SKIP_SUITES", None)
        # --check: same sweep, no writes. The fixture is already protected.
        return subprocess.run([os.path.join(self.repo, "install.sh"), "--check"],
                              cwd=self.repo, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)

    def test_zero_suites_is_red(self):
        proc = self.run_install()
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("no test suites found", proc.stdout)

    def test_one_passing_suite_is_green(self):
        self.add_suite("test_a.py", "import sys; sys.exit(0)\n")
        proc = self.run_install()
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"OK\S*\s+lib/test_a\.py")

    def test_stdin_reader_does_not_swallow_the_queue(self):
        self.add_suite("test_a.py", "import sys; sys.exit(0)\n")
        self.add_suite("test_b.py", "import sys; sys.stdin.read()\n")
        self.add_suite("test_c.py", "import sys; sys.exit(1)\n")
        self.add_suite("test_d.py", "import sys; sys.exit(0)\n")
        proc = self.run_install()
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        for ok in ("lib/test_a.py", "lib/test_b.py", "lib/test_d.py"):
            self.assertRegex(proc.stdout, r"OK\S*\s+" + re.escape(ok))
        self.assertIn("lib/test_c.py FAILED", proc.stdout)

    def test_a_stdin_reader_cannot_swallow_the_queue_even_without_the_redirect(self):
        """The sweep collects its suites into an array BEFORE running any, so
        the per-suite </dev/null is a second line of defence, not the only one.
        Remove it from an installer copy: the failing suite must still RUN."""
        with open(INSTALL_SH, encoding="utf-8") as fh:
            body = fh.read()
        stripped = body.replace(">/dev/null 2>&1 </dev/null )", ">/dev/null 2>&1 )")
        self.assertNotEqual(body, stripped, "mutation did not apply")
        os.remove(os.path.join(self.repo, "install.sh"))
        copy = os.path.join(self.repo, "install.sh")
        with open(copy, "w", encoding="utf-8") as fh:
            fh.write(stripped)
        os.chmod(copy, 0o755)
        marker = os.path.join(self.root, "c-ran")
        self.add_suite("test_a.py", "import sys; sys.stdin.read()\n")
        self.add_suite("test_b.py",
                       "import sys; open(%r, 'w').close(); sys.exit(1)\n" % marker)
        proc = self.run_install()
        self.assertTrue(os.path.exists(marker), proc.stdout + proc.stderr)
        self.assertEqual(1, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("lib/test_b.py FAILED", proc.stdout)

    def test_suites_do_not_inherit_the_users_config_overrides(self):
        """A user may set CLAUDE_CONFIG_DIR to their own ~/.claude (install.sh
        allows it) or GIT_CONFIG_GLOBAL. Suites re-point HOME at fixtures, so
        either one leaking through turned 29 of 178 suites red."""
        seen = os.path.join(self.root, "seen")
        self.add_suite("test_a.py", textwrap.dedent("""\
            import os, sys
            leaked = [k for k in ("CLAUDE_CONFIG_DIR", "GIT_CONFIG_GLOBAL") if k in os.environ]
            open(%r, "w").write(",".join(leaked))
            sys.exit(1 if leaked else 0)
            """ % seen))
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        env = fx.shim_env(self.base_env(), self.bin_dir, self.state, skip_suites=False)
        env.pop("CLAUDE_INSTALL_SKIP_SUITES", None)
        env["CLAUDE_CONFIG_DIR"] = os.path.join(self.home, ".claude")
        env["GIT_CONFIG_GLOBAL"] = os.path.join(self.root, "user.gitconfig")
        proc = subprocess.run([os.path.join(self.repo, "install.sh"), "--check"],
                              cwd=self.repo, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)
        with open(seen, encoding="utf-8") as fh:
            self.assertEqual("", fh.read(), proc.stdout + proc.stderr)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"OK\S*\s+lib/test_a\.py")

    def test_skip_is_labelled_not_a_pass_and_keeps_the_exit_code(self):
        self.add_suite("test_c.py", "import sys; sys.exit(1)\n")
        env = fx.shim_env(self.base_env(), self.bin_dir, self.state, skip_suites=True)
        env["CLAUDE_INSTALL_SKIP_SUITES"] = "1"
        proc = subprocess.run([os.path.join(self.repo, "install.sh"), "--check"],
                              cwd=self.repo, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertIn("Tests SKIPPED (CLAUDE_INSTALL_SKIP_SUITES=1) — not a pass",
                      proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2 if "-v" in sys.argv else 1)
