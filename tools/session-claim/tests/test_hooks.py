"""Payload-in / exit-code-out tests for both hooks, run as subprocesses.

Running them as real processes rather than importing them is deliberate: every
defect on the previous branch of this repo came from EXECUTING a tool, none
from reading its diff, including two false-ALLOWs behind a fully green suite.
For a guard, a false-ALLOW means it looks installed and enforces nothing.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLAIM = os.path.join(TOOL_DIR, "claude-session-claim")
RELEASE = os.path.join(TOOL_DIR, "claude-session-release")
sys.path.insert(0, TOOL_DIR)
import identity_lib


def sandbox_env():
    """State paths pointed at a throwaway directory."""
    box = tempfile.mkdtemp()
    return {"CLAIM_CLAIMS_PATH": os.path.join(box, "claims.json"),
            "CLAIM_BREAKER_PATH": os.path.join(box, "breaker.json"),
            "CLAIM_LOG_PATH": os.path.join(box, "decisions.log"),
            "CLAIM_TEST_PS_TIMEOUT": "30"}


def write_payload():
    """A genuine file write into a throwaway repo, plus its sandbox env."""
    box = tempfile.mkdtemp()
    repo = os.path.realpath(tempfile.mkdtemp())
    subprocess.run(("git", "-C", repo, "init", "-b", "main"), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    env = {"CLAIM_CLAIMS_PATH": os.path.join(box, "claims.json"),
           "CLAIM_BREAKER_PATH": os.path.join(box, "breaker.json"),
           "CLAIM_LOG_PATH": os.path.join(box, "decisions.log"),
           "CLAIM_TEST_OWNER_PID": str(os.getpid()),
           "CLAIM_TEST_PS_TIMEOUT": "30"}
    payload = {"tool_name": "Write",
               "tool_input": {"file_path": os.path.join(repo, "new.py")}}
    return payload, env


def run(hook, payload=None, env=None, raw=None):
    """Run a hook as a subprocess, with its state redirected to a sandbox.

    The redirect is applied to EVERY invocation, including the ones that are
    expected to return long before any state is read. That is deliberate:
    those tests were previously hermetic only because an early return happened
    to fire first -- hermetic by call ordering, which is not a property a test
    should depend on. Recording that CLAIM_ALLOW was used, or hoisting prune()
    so claims stay tidy on every call, are both ordinary changes; either one
    moves the return past a write, and this suite would have started editing
    the developer's own ~/.claude/session-claims/ while still passing green.

    `env` is applied last, so a caller with its own sandbox still wins.
    `raw` sends a literal string instead of JSON, for malformed-input tests.
    """
    environment = dict(os.environ)
    environment["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent.json"
    environment.update(sandbox_env())
    if env:
        environment.update(env)
    stdin = raw if raw is not None else json.dumps(payload)
    return subprocess.run([sys.executable, hook], input=stdin,
                          capture_output=True, text=True, env=environment)


class TestHotPath(unittest.TestCase):
    """Matcher is `*`, so this process starts on EVERY tool call."""

    def test_a_payload_with_no_file_path_is_allowed(self):
        result = run(CLAIM, {"tool_name": "Read",
                             "tool_input": {"pattern": "x"}})
        self.assertEqual(result.returncode, 0)

    def test_garbage_stdin_is_allowed(self):
        result = run(CLAIM, raw="{not json")
        self.assertEqual(result.returncode, 0)

    def test_empty_stdin_is_allowed(self):
        result = run(CLAIM, raw="")
        self.assertEqual(result.returncode, 0)

    def test_a_non_dict_payload_is_allowed(self):
        result = run(CLAIM, ["a", "list"])
        self.assertEqual(result.returncode, 0)

    def test_missing_tool_input_is_allowed(self):
        result = run(CLAIM, {"tool_name": "Bash"})
        self.assertEqual(result.returncode, 0)

    # CATASTROPHE bounds, not budgets. The real properties are asserted
    # structurally in TestPathCostIsStructural below; these only catch a
    # regression that no fork and no import can explain -- a pure-Python
    # blow-up. Both are set far above anything load can produce, so neither can
    # flake: see the class docstring there for the measured numbers.
    #
    # BOTH paths carry one. Keeping it on the hot path alone put it where a
    # blow-up is least plausible -- a straight-line sequence of dict lookups
    # with no loop -- while the write path, which iterates every claim record
    # inside a cross-process lock, would have had no wall-clock bound at all.
    # That path is the one charged against the hook's declared timeout, and
    # per claim_lib's header a hook killed on that timeout returns no verdict,
    # so the write is ALLOWED. Slow there fails open and silently.
    CEILING_MS = 2000

    def average_ms(self, payload, env=None):
        run(CLAIM, payload, env=env)             # warm the interpreter cache
        start = time.time()
        for _ in range(5):
            run(CLAIM, payload, env=env)
        return ((time.time() - start) / 5) * 1000

    def test_the_hot_path_is_bounded_by_a_wall_clock_smoke_ceiling(self):
        average = self.average_ms({"tool_name": "Read",
                                   "tool_input": {"pattern": "x"}})
        self.assertLess(average, self.CEILING_MS,
                        "hot path averaged %.0fms" % average)

    def test_the_write_path_is_bounded_by_a_wall_clock_smoke_ceiling(self):
        payload, env = write_payload()
        average = self.average_ms(payload, env=env)
        self.assertLess(average, self.CEILING_MS,
                        "write path averaged %.0fms" % average)


def import_trace(payload, env=None):
    """Module names the hook actually imported, from `python3 -v`.

    The interpreter writes `import 'claim_lib' # <loader>` to stderr, WITH the
    quotes. A pattern without them matches nothing on either path, which reads
    exactly like a clean hot path -- the first version of this helper had that
    bug and only the positive control below caught it.
    """
    environment = dict(os.environ)
    environment["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent.json"
    environment.update(sandbox_env())
    if env:
        environment.update(env)
    result = subprocess.run([sys.executable, "-v", CLAIM],
                            input=json.dumps(payload), capture_output=True,
                            text=True, env=environment)
    return set(re.findall(r"^import '([^']+)'", result.stderr, re.M))


def fork_log(payload, env=None):
    """Every `ps` and `git` the hook forked, in order.

    The shims LOG AND DELEGATE -- they `exec` the real binary, so behaviour is
    identical and only the count is observed. A shim that answered for itself
    would be measuring its own fixture instead of the hook.
    """
    box = tempfile.mkdtemp()
    shim_dir = os.path.join(box, "shims")
    os.makedirs(shim_dir)
    marker = os.path.join(box, "forks.log")
    for name in ("ps", "git"):
        real = shutil.which(name)
        if real is None:                          # nothing to intercept
            raise unittest.SkipTest("%s not on PATH" % name)
        path = os.path.join(shim_dir, name)
        with open(path, "w") as handle:
            handle.write('#!/bin/sh\nprintf "%%s\\n" "%s" >> "%s"\nexec "%s" "$@"\n'
                         % (name, marker, real))
        os.chmod(path, 0o755)

    environment = dict(os.environ)
    environment["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent.json"
    environment.update(sandbox_env())
    if env:
        environment.update(env)
    # Prepended, not replacing: only `ps` and `git` are intercepted, so an
    # empty log means "did not fork these" rather than "could not run at all".
    environment["PATH"] = shim_dir + os.pathsep + environment["PATH"]
    subprocess.run([sys.executable, CLAIM], input=json.dumps(payload),
                   capture_output=True, text=True, env=environment)
    if not os.path.exists(marker):
        return []
    with open(marker) as handle:
        return handle.read().split()


class TestPathCostIsStructural(unittest.TestCase):
    """What the hot path may cost, asserted without a clock.

    These replace two wall-clock budgets (250ms hot, 400ms write) that were
    measuring the machine, not the code. Under heavy CPU saturation this suite failed
    intermittently, in the timing assertions and nothing else in the tool. The
    write path was tripping on ambient load, which is exactly what this repo's
    hermeticity rule forbids a test from asserting against. `min`-of-N was
    considered as a fix and rejected: it buys too little against the real
    overshoot.

    The question those budgets were really asking is whether the hot path does
    filesystem or git work, and whether the write path's cost grows with the
    number of claim records. Both are structural, so both are asserted
    structurally here and hold on any machine at any load.

    Every probe has a POSITIVE CONTROL: a probe that silently observes nothing
    passes a hot-path assertion for free, and the import probe really did have
    that bug when it was written.
    """

    # TWO hot payloads, because they leave by different doors and only one of
    # them was ever asserted. `Read` returns at the READ_ONLY_TOOLS branch;
    # `Bash` is neither a reader nor a file write, so it carries on to the
    # no-path early exit a few lines below -- and that is the exit the hook's
    # own docstring names as the one that must happen before any import.
    # Everything in that second class (Bash, Task, TodoWrite, WebFetch, every
    # MCP tool) went through an unasserted window: measured, an import hoisted
    # to just below the reader branch left the whole suite green while a Bash
    # payload's trace showed claim_lib and identity_lib loaded.
    HOT_PAYLOADS = {
        "reader": {"tool_name": "Read", "tool_input": {"pattern": "x"}},
        "non-writer": {"tool_name": "Bash", "tool_input": {"command": "ls"}},
    }
    HEAVY = frozenset({"claim_lib", "identity_lib", "breaker_lib",
                       "toolkit_config"})

    def write_payload(self):
        return write_payload()

    def with_records(self, env, count):
        """Seed the claims file with `count` records of a LIVE, non-owning pid.

        The pid has to be alive or this fixture proves nothing, and that is not
        a detail — the first version seeded `999000 + i`, above the PID ceiling.
        `is_pid_live` is an existence check FIRST (`os.kill(pid, 0)`) and a
        start-time check second, and only the second half can fork. Dead pids
        never reach it, so record count and fork count were decoupled by
        construction no matter what the code did: the historical regression
        reintroduced (one `ps` per record) left the whole suite green.

        `proc_start` must also be non-None and WRONG. None degrades to a bare
        existence check that returns before forking, and a match would keep the
        record alive and change what the hook then does. Wrong-but-present is
        the only shape that reaches the compare, which is the line under test.
        """
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(120)"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        with open(env["CLAIM_CLAIMS_PATH"], "w") as handle:
            json.dump([{"checkout": "/nonexistent/r%d" % i,
                        "pid": child.pid,
                        "session": "s%d" % i,
                        "proc_start": "Thu Jan  1 00:00:00 1970"}
                       for i in range(count)], handle)

    # --- imports -------------------------------------------------------------

    def test_the_hot_path_imports_nothing_heavy(self):
        for label, payload in self.HOT_PAYLOADS.items():
            with self.subTest(exit=label):
                self.assertEqual(import_trace(payload) & self.HEAVY, set())

    def test_control_the_write_path_does_import_them(self):
        # Without this, a probe that saw nothing would pass the test above no
        # matter what the hook did.
        payload, env = self.write_payload()
        self.assertEqual(import_trace(payload, env) & self.HEAVY, self.HEAVY)

    # --- forks ---------------------------------------------------------------

    def test_the_hot_path_forks_no_ps_and_no_git(self):
        for label, payload in self.HOT_PAYLOADS.items():
            with self.subTest(exit=label):
                self.assertEqual(fork_log(payload), [])

    def test_control_the_write_path_does_fork(self):
        payload, env = self.write_payload()
        self.assertNotEqual(fork_log(payload, env), [],
                            "the fork probe observed nothing on a path that "
                            "resolves identity -- the probe is broken")

    def test_write_path_fork_count_does_not_grow_with_record_count(self):
        # The property the 400ms budget was standing in for, and the one that
        # actually regressed once: liveness used to fork `ps` PER RECORD, and
        # nothing caps the record count. Asserted at the executable, where the
        # in-process equivalent in test_claim_lib cannot reach.
        payload, env = self.write_payload()
        self.with_records(env, 0)
        empty = fork_log(payload, env)
        payload, env = self.write_payload()
        self.with_records(env, 100)
        loaded = fork_log(payload, env)
        self.assertEqual(sorted(empty), sorted(loaded),
                         "fork count moved between 0 and 100 records: "
                         "%s vs %s" % (sorted(empty), sorted(loaded)))


class TestEscapeHatches(unittest.TestCase):

    def test_claim_allow_env_var_short_circuits(self):
        result = run(CLAIM, {"tool_name": "Write",
                             "tool_input": {"file_path": __file__}},
                     env={"CLAIM_ALLOW": "1"})
        self.assertEqual(result.returncode, 0)


class TestNonRepoFiles(unittest.TestCase):

    def test_a_file_outside_any_repo_is_allowed_silently(self):
        plain = tempfile.mkdtemp()
        result = run(CLAIM, {"tool_name": "Write",
                             "tool_input": {"file_path":
                                            os.path.join(plain, "f.txt")}})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stderr.strip(), "")


def failing_start_table_shim(case):
    """A `ps` that fails ONLY the start-time table, and delegates the rest.

    Returns (env, marker): `env` carries the shim on PATH for `run(env=...)`,
    and `marker` is a file the shim appends to when it fails the table. Without
    the marker a pass proves nothing: a hook that never asked for the table
    would be indistinguishable from one that coped with its failure.
    Everything else (owner resolution) goes to the real ps, so the only thing
    that differs from production is the one call under test.
    """
    real_ps = shutil.which("ps")
    if real_ps is None:
        case.skipTest("no ps")
    shim_dir = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, shim_dir, True)
    marker = os.path.join(shim_dir, "marker")
    shim = os.path.join(shim_dir, "ps")
    with open(shim, "w") as handle:
        handle.write('#!/bin/sh\n'
                     'case "$*" in *pid=,lstart=*) echo failed >> "%s"; exit 1;; esac\n'
                     'exec "%s" "$@"\n' % (marker, real_ps))
    os.chmod(shim, 0o755)
    return {"PATH": shim_dir + os.pathsep + os.environ["PATH"]}, marker


def failing_per_pid_start_shim(case):
    """A `ps` that fails ONLY the per-pid start query (`-o lstart= -p <pid>`),
    and delegates everything else, the start table included, to the real ps.

    Same `(env, marker)` return as failing_start_table_shim. On a healthy
    machine the per-pid query and the table return the same string, so only a
    shim that fails the per-pid query alone can tell which source the hook read.
    """
    real_ps = shutil.which("ps")
    if real_ps is None:
        case.skipTest("no ps")
    shim_dir = tempfile.mkdtemp()
    case.addCleanup(shutil.rmtree, shim_dir, True)
    marker = os.path.join(shim_dir, "marker")
    shim = os.path.join(shim_dir, "ps")
    with open(shim, "w") as handle:
        handle.write('#!/bin/sh\n'
                     'case "$*" in *"lstart= -p"*) echo failed >> "%s"; exit 1;; esac\n'
                     'exec "%s" "$@"\n' % (marker, real_ps))
    os.chmod(shim, 0o755)
    return {"PATH": shim_dir + os.pathsep + os.environ["PATH"]}, marker


class TestRelease(unittest.TestCase):
    """Every test here must point at sandbox paths -- CLAIM_TEST_OWNER_PID
    only takes effect once CLAIM_CLAIMS_PATH is also set (see the compound
    gate in identity_lib.resolve_hook_owner), and without both the hook falls
    through to real
    process-tree identity resolution and can drop the CURRENT session's own
    claim in ~/.claude/session-claims/claims.json.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.claims = os.path.join(self.tmp, "claims.json")
        self.breaker = os.path.join(self.tmp, "breaker.json")
        self.log = os.path.join(self.tmp, "decisions.log")

    def _env(self, owner_pid):
        return {"CLAIM_CLAIMS_PATH": self.claims,
                "CLAIM_BREAKER_PATH": self.breaker,
                "CLAIM_LOG_PATH": self.log,
                "CLAIM_TEST_OWNER_PID": str(owner_pid),
                "CLAIM_TEST_PS_TIMEOUT": "30"}

    def test_release_always_exits_zero(self):
        result = run(RELEASE, {"session_id": "s", "hook_event_name": "SessionEnd"},
                     env=self._env(os.getpid()))
        self.assertEqual(result.returncode, 0)

    def test_release_survives_garbage(self):
        environment = dict(os.environ)
        environment.update(self._env(os.getpid()))
        result = subprocess.run([sys.executable, RELEASE], input="{not json",
                                capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 0)

    def test_release_drops_only_the_calling_owners_claim(self):
        # This is the contract release() actually has: it is a targeted
        # removal, not a wipe. A claims file with two owners' claims in it,
        # and only the one named by CLAIM_TEST_OWNER_PID should disappear.
        #
        # Both pids are real, LIVE processes on purpose (the test process and
        # its parent), not arbitrary numbers: the release hook also prunes
        # dead-owner claims after releasing, so two dead pids would pass this
        # assertion for the wrong reason -- pruned, not released -- and hide a
        # broken release() behind a working prune().
        owner_x = os.getppid()
        owner_y = os.getpid()
        with open(self.claims, "w") as handle:
            json.dump([
                {"checkout": "/repo/x", "pid": owner_x, "proc_start": None,
                 "session_id": "x", "name": "session-x",
                 "transcript_path": None, "claimed_at": "2026-08-12T00:00:00"},
                {"checkout": "/repo/y", "pid": owner_y, "proc_start": None,
                 "session_id": "y", "name": "session-y",
                 "transcript_path": None, "claimed_at": "2026-08-12T00:00:00"},
            ], handle)
        result = run(RELEASE, {"session_id": "x", "hook_event_name": "SessionEnd"},
                     env=self._env(owner_x))
        self.assertEqual(result.returncode, 0)
        with open(self.claims) as handle:
            remaining = json.load(handle)
        remaining_pids = {record["pid"] for record in remaining}
        self.assertNotIn(owner_x, remaining_pids,
                         "release must drop the calling owner's own claim")
        self.assertIn(owner_y, remaining_pids,
                      "release must not touch a different owner's claim")


    def test_release_keeps_other_live_claims_when_the_start_table_fails(self):
        # If the table fails, release must not read every other live claim as
        # dead: it rewrites claims.json, so that verdict would be persisted.
        # The surviving claim carries its REAL proc_start; with None it would
        # degrade to an existence check and never reach the defect.
        real_start = identity_lib.proc_start(os.getpid())
        if real_start is None:
            self.skipTest("no ps")
        owner_x = os.getppid()
        owner_y = os.getpid()
        with open(self.claims, "w") as handle:
            json.dump([
                {"checkout": "/repo/x", "pid": owner_x, "proc_start": None,
                 "session_id": "x", "name": "session-x",
                 "transcript_path": None, "claimed_at": "2026-08-12T00:00:00"},
                {"checkout": "/repo/y", "pid": owner_y, "proc_start": real_start,
                 "session_id": "y", "name": "session-y",
                 "transcript_path": None, "claimed_at": "2026-08-12T00:00:00"},
            ], handle)
        shim_env, marker = failing_start_table_shim(self)
        env = self._env(owner_x)
        env.update(shim_env)
        result = run(RELEASE, {"session_id": "x", "hook_event_name": "SessionEnd"},
                     env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.claims) as handle:
            remaining_pids = {record["pid"] for record in json.load(handle)}
        self.assertNotIn(owner_x, remaining_pids)
        self.assertIn(owner_y, remaining_pids,
                      "a failed start table must not drop a live claim")
        self.assertTrue(os.path.exists(marker), "the shim never fired")


class BlockingCase(unittest.TestCase):
    """A temp repo plus a claims file naming a live-but-different owner --
    everything needed to drive the one path that exits 2."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.claims = os.path.join(self.tmp, "claims.json")
        self.repo = os.path.realpath(tempfile.mkdtemp())
        for args in (("init", "-b", "main"),
                     ("config", "user.email", "t@example.com"),
                     ("config", "user.name", "T")):
            subprocess.run(("git", "-C", self.repo) + args, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(self.repo, "seed.txt"), "w") as handle:
            handle.write("seed")
        for args in (("add", "seed.txt"), ("commit", "-m", "seed")):
            subprocess.run(("git", "-C", self.repo) + args, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # A REAL process to play the blocked session. It used to be
        # os.getpid() + 1, an arbitrary number that usually happened not to
        # exist -- which is now load-bearing, because breaker state is kept
        # only for live pids. With a dead owner the breaker's own count is
        # discarded between invocations and the guard never stands down, so
        # test_the_breaker_stands_the_guard_down failed roughly whenever the
        # neighbouring pid was unoccupied. In production this cannot arise:
        # the owner is the running claude process that made the tool call.
        self.owner = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"])
        self.addCleanup(self.owner.wait)
        self.addCleanup(self.owner.kill)

    def _env(self):
        # CLAIM_TEST_OWNER_PID pins this hook run's owner to a pid that is NOT
        # the one holding the claim below. Without it these tests would only
        # pass when run from inside a live Claude session, since identity is
        # otherwise derived from the process tree -- exactly the ambient
        # dependency the repo's hermeticity rule forbids.
        return {"CLAIM_CLAIMS_PATH": self.claims,
                "CLAIM_BREAKER_PATH": os.path.join(self.tmp, "breaker.json"),
                "CLAIM_LOG_PATH": os.path.join(self.tmp, "decisions.log"),
                "CLAIM_TEST_OWNER_PID": str(self.owner.pid),
                "CLAIM_TEST_PS_TIMEOUT": "30"}

    def _hold(self):
        """os.getpid() is the TEST process -- live, and different from the
        hook subprocess's own resolved owner."""
        with open(self.claims, "w") as handle:
            json.dump([{"checkout": self.repo, "pid": os.getpid(),
                        "proc_start": None, "session_id": "other",
                        "name": "session-zz", "transcript_path": None,
                        "claimed_at": "2026-08-12T00:00:00"}], handle)

    def _write_payload(self):
        return {"tool_name": "Write",
                "tool_input": {"file_path": os.path.join(self.repo, "new.py")}}


class TestBlocking(BlockingCase):
    """The one path that exits 2."""

    def test_blocks_when_another_live_owner_holds_the_checkout(self):
        # os.getpid() here is the TEST process -- live, and different from the
        # hook subprocess's own resolved owner.
        with open(self.claims, "w") as handle:
            json.dump([{"checkout": self.repo, "pid": os.getpid(),
                        "proc_start": None, "session_id": "other",
                        "name": "session-zz", "transcript_path": None,
                        "claimed_at": "2026-08-12T00:00:00"}], handle)
        result = run(CLAIM, {"tool_name": "Write",
                             "tool_input": {"file_path":
                                            os.path.join(self.repo, "new.py")}},
                     env=self._env())
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("session-zz", result.stderr)
        self.assertIn("EnterWorktree", result.stderr)
        self.assertIn("CLAIM_ALLOW=1", result.stderr)

    def test_the_block_creates_the_worktree_it_names(self):
        with open(self.claims, "w") as handle:
            json.dump([{"checkout": self.repo, "pid": os.getpid(),
                        "proc_start": None, "session_id": "other",
                        "name": "session-zz", "transcript_path": None,
                        "claimed_at": "2026-08-12T00:00:00"}], handle)
        run(CLAIM, {"tool_name": "Write",
                    "tool_input": {"file_path": os.path.join(self.repo, "new.py")}},
            env=self._env())
        worktrees = os.path.join(self.repo, ".claude", "worktrees")
        self.assertTrue(os.path.isdir(worktrees))
        self.assertTrue(os.listdir(worktrees))

    def test_a_dead_holder_does_not_block(self):
        with open(self.claims, "w") as handle:
            json.dump([{"checkout": self.repo, "pid": 999999,
                        "proc_start": None, "session_id": "dead",
                        "name": "dead", "transcript_path": None,
                        "claimed_at": "2026-08-12T00:00:00"}], handle)
        result = run(CLAIM, {"tool_name": "Write",
                             "tool_input": {"file_path":
                                            os.path.join(self.repo, "new.py")}},
                     env=self._env())
        self.assertEqual(result.returncode, 0)

    def test_the_breaker_stands_the_guard_down(self):
        with open(self.claims, "w") as handle:
            json.dump([{"checkout": self.repo, "pid": os.getpid(),
                        "proc_start": None, "session_id": "other",
                        "name": "session-zz", "transcript_path": None,
                        "claimed_at": "2026-08-12T00:00:00"}], handle)
        payload = {"tool_name": "Write",
                   "tool_input": {"file_path": os.path.join(self.repo, "new.py")}}
        codes = [run(CLAIM, payload, env=self._env()).returncode
                 for _ in range(5)]
        self.assertEqual(codes[0], 2)
        self.assertEqual(codes[-1], 0,
                         "breaker must stand the guard down: got %r" % (codes,))

    def test_reads_do_not_disarm_the_breaker_before_the_first_write(self):
        # Regression for the disarm sequence: Read carries file_path just like
        # Write does, and without the READ_ONLY_TOOLS exclusion three reads
        # would each count as a block, tripping the breaker before the write
        # the guard exists to stop ever got a chance to run.
        with open(self.claims, "w") as handle:
            json.dump([{"checkout": self.repo, "pid": os.getpid(),
                        "proc_start": None, "session_id": "other",
                        "name": "session-zz", "transcript_path": None,
                        "claimed_at": "2026-08-12T00:00:00"}], handle)
        read_payload = {"tool_name": "Read",
                        "tool_input": {"file_path":
                                       os.path.join(self.repo, "new.py")}}
        for _ in range(3):
            result = run(CLAIM, read_payload, env=self._env())
            self.assertEqual(result.returncode, 0, result.stderr)
        write_payload = {"tool_name": "Write",
                         "tool_input": {"file_path":
                                        os.path.join(self.repo, "new.py")}}
        result = run(CLAIM, write_payload, env=self._env())
        log_path = os.path.join(self.tmp, "decisions.log")
        if os.path.exists(log_path):
            with open(log_path) as handle:
                log_text = handle.read()
        else:
            log_text = "(no log)"
        self.assertEqual(result.returncode, 2,
                         "%s\ndecision log:\n%s" % (result.stderr, log_text))

    def test_the_block_leaves_the_holders_working_tree_clean(self):
        # The worktree is created for the BLOCKED session, but it lands inside
        # the HOLDER's checkout. Untracked, it turns up in their `git status`
        # and a routine `git add -A` stages it as an embedded gitlink -- so the
        # artifact rides into the PR of the session that did nothing wrong.
        self._hold()
        result = run(CLAIM, self._write_payload(), env=self._env())
        self.assertEqual(result.returncode, 2, result.stderr)
        status = subprocess.run(["git", "-C", self.repo, "status", "--short"],
                                capture_output=True, text=True).stdout
        self.assertEqual(status, "",
                         "the holder's tree must be clean after we plant a "
                         "worktree in it; got %r" % status)


class TestKillSwitch(BlockingCase):
    """`session_claim.enabled` is the tool's only off switch, and it was
    asserted nowhere. A kill switch nobody tests is a kill switch nobody can
    rely on in the moment they need it.
    """

    def _config(self, body):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w") as handle:
            json.dump(body, handle)
        return path

    def test_enabled_false_allows_a_write_that_would_otherwise_block(self):
        self._hold()
        env = self._env()
        env["CLAUDE_TOOLKIT_CONFIG"] = self._config(
            {"session_claim": {"enabled": False}})
        result = run(CLAIM, self._write_payload(), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr.strip(), "")

    def test_the_same_write_blocks_with_the_key_absent(self):
        # Absent means ON. Without this the test above would pass just as well
        # against a hook that never blocks at all.
        self._hold()
        env = self._env()
        env["CLAUDE_TOOLKIT_CONFIG"] = self._config({})
        result = run(CLAIM, self._write_payload(), env=env)
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_the_same_write_blocks_with_no_config_file_at_all(self):
        self._hold()
        env = self._env()
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(self.tmp, "missing.json")
        result = run(CLAIM, self._write_payload(), env=env)
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_enabled_true_blocks(self):
        self._hold()
        env = self._env()
        env["CLAUDE_TOOLKIT_CONFIG"] = self._config(
            {"session_claim": {"enabled": True}})
        result = run(CLAIM, self._write_payload(), env=env)
        self.assertEqual(result.returncode, 2, result.stderr)


class TestFailedStartTable(BlockingCase):
    """A failed start-time table means no claim's liveness can be judged, so
    the hook fails open and touches nothing. It once read a live
    holder as dead and saved its own claim in the holder's place; reading
    "unknown" as live instead would block on a stale claim whose pid was
    recycled. Neither verdict is evidence, so neither may be acted on."""

    def _hold_with(self, proc_start):
        with open(self.claims, "w") as handle:
            json.dump([{"checkout": self.repo, "pid": os.getpid(),
                        "proc_start": proc_start, "session_id": "other",
                        "name": "session-zz", "transcript_path": None,
                        "claimed_at": "2026-08-12T00:00:00"}], handle)

    def _claimed_pids(self):
        with open(self.claims) as handle:
            return {record["pid"] for record in json.load(handle)}

    def _run_with_failed_table(self):
        """Run the claim hook with the start table failing. Asserts it fails
        open without touching claims.json, and that the shim really fired."""
        with open(self.claims) as handle:
            before = handle.read()
        shim_env, marker = failing_start_table_shim(self)
        env = self._env()
        env.update(shim_env)
        result = run(CLAIM, self._write_payload(), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.claims) as handle:
            self.assertEqual(handle.read(), before,
                             "an unjudgeable table must not rewrite claims")
        self.assertTrue(os.path.exists(marker), "the shim never fired")

    def test_a_live_holder_keeps_its_claim_when_the_start_table_fails(self):
        # The holder carries its REAL proc_start. _hold()'s None would degrade
        # to an existence check and never reach the defect.
        real_start = identity_lib.proc_start(os.getpid())
        if real_start is None:
            self.skipTest("no ps")
        self._hold_with(real_start)
        self._run_with_failed_table()

    def test_a_recycled_holder_does_not_block_when_the_start_table_fails(self):
        # A live pid carrying an obsolete start is a stale claim on a recycled
        # pid. Unknown liveness must not turn it into grounds to refuse.
        self._hold_with("Thu Jan  1 00:00:00 1970")
        self._run_with_failed_table()

    def test_a_working_table_still_clears_a_recycled_holder(self):
        # Control: a positive "start time differs" answer still clears.
        self._hold_with("Thu Jan  1 00:00:00 1970")
        result = run(CLAIM, self._write_payload(), env=self._env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(os.getpid(), self._claimed_pids())


class TestOwnerStartTime(BlockingCase):
    """A claim or breaker entry never carries a missing start time. The
    owner's start comes from the same table as every liveness check; when the
    table lacks the owner, the hook allows and writes nothing."""

    def _claims_records(self):
        with open(self.claims) as handle:
            return json.load(handle)

    def _write_empty_claims(self):
        with open(self.claims, "w") as handle:
            handle.write("[]")

    def _breaker_entry(self, env):
        with open(env["CLAIM_BREAKER_PATH"]) as handle:
            return json.load(handle)[str(self.owner.pid)]

    def _dead_pid(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        pid = child.pid
        child.kill()
        child.wait()
        return pid

    def _run_with_dead_owner(self):
        self._write_empty_claims()
        with open(self.claims) as handle:
            before = handle.read()
        env = self._env()
        env["CLAIM_TEST_OWNER_PID"] = str(self._dead_pid())
        result = run(CLAIM, self._write_payload(), env=env)
        return result, before, env

    def test_a_new_claim_stores_the_owners_real_start_time(self):
        expected = identity_lib.proc_start(self.owner.pid)
        if expected is None:
            self.skipTest("no ps")
        self._write_empty_claims()
        result = run(CLAIM, self._write_payload(), env=self._env())
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self._claims_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["pid"], self.owner.pid)
        self.assertEqual(records[0]["proc_start"], expected)
        self.assertIsNotNone(records[0]["proc_start"])

    def test_an_owner_missing_from_the_start_table_writes_nothing(self):
        result, before, env = self._run_with_dead_owner()
        self.assertEqual(result.returncode, 0, result.stderr)
        with open(self.claims) as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse(os.path.exists(env["CLAIM_BREAKER_PATH"]))
        with open(env["CLAIM_LOG_PATH"]) as handle:
            self.assertIn("owner start unknown", handle.read())

    def test_control_a_live_owner_with_the_same_setup_does_claim(self):
        # Positive control for the test above: a hook that never writes
        # anything could not pass that one for the right reason.
        if identity_lib.proc_start(self.owner.pid) is None:
            self.skipTest("no ps")
        self._write_empty_claims()
        result = run(CLAIM, self._write_payload(), env=self._env())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self._claims_records()), 1)

    def test_a_block_records_the_owners_real_start_in_the_breaker(self):
        expected = identity_lib.proc_start(self.owner.pid)
        if expected is None:
            self.skipTest("no ps")
        self._hold()
        env = self._env()
        result = run(CLAIM, self._write_payload(), env=env)
        self.assertEqual(result.returncode, 2, result.stderr)
        entry = self._breaker_entry(env)
        self.assertEqual(entry["proc_start"], expected)
        self.assertIsNotNone(entry["proc_start"])

    def test_the_owners_start_comes_from_the_table_not_a_per_pid_query(self):
        # On a healthy machine the per-pid query and the table agree, so the
        # tests above cannot tell the sources apart. This shim fails only the
        # per-pid query; a hook that asked it would store None (or crash).
        expected = identity_lib.proc_start(self.owner.pid)
        if expected is None:
            self.skipTest("no ps")
        shim_env, marker = failing_per_pid_start_shim(self)
        self._hold()
        env = self._env()
        env.update(shim_env)
        result = run(CLAIM, self._write_payload(), env=env)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self._breaker_entry(env)["proc_start"], expected)

        self._write_empty_claims()
        env["CLAIM_BREAKER_PATH"] = os.path.join(self.tmp, "breaker2.json")
        result = run(CLAIM, self._write_payload(), env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        records = self._claims_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["proc_start"], expected)
        self.assertFalse(os.path.exists(marker),
                         "the hook made a per-pid start query")

        # Positive control: the shim really fails a per-pid query and logs it.
        probe = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(self.owner.pid)],
            env={**os.environ, **shim_env},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertNotEqual(probe.returncode, 0)
        self.assertTrue(os.path.exists(marker), "the shim never matched")


if __name__ == "__main__":
    unittest.main()
