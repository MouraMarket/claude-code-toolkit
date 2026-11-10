"""Tests for checkout resolution and the claims store.

Hermetic: every git test builds a throwaway repo in a temp directory, and
CLAIMS_PATH is redirected into a temp file for every store test. Nothing here
touches the real ~/.claude or the real toolkit checkout.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOL_DIR)
import claim_lib
import identity_lib

LIB_DIR = os.path.join(os.path.dirname(os.path.dirname(TOOL_DIR)), "lib")
sys.path.insert(0, LIB_DIR)
import portable


def git(repo, *args):
    subprocess.run(("git", "-C", repo) + args, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_repo():
    root = os.path.realpath(tempfile.mkdtemp())
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "T")
    with open(os.path.join(root, "seed.txt"), "w") as handle:
        handle.write("seed")
    git(root, "add", "seed.txt")
    git(root, "commit", "-m", "seed")
    return root


class TestFindCheckout(unittest.TestCase):

    def test_finds_the_toplevel_from_a_nested_file(self):
        root = make_repo()
        nested = os.path.join(root, "a", "b")
        os.makedirs(nested)
        target = os.path.join(nested, "f.py")
        self.assertEqual(claim_lib.find_checkout(target), root)

    def test_finds_the_toplevel_from_a_file_at_the_root(self):
        root = make_repo()
        self.assertEqual(claim_lib.find_checkout(os.path.join(root, "f.py")), root)

    def test_a_file_outside_any_repo_is_none(self):
        plain = os.path.realpath(tempfile.mkdtemp())
        self.assertIsNone(claim_lib.find_checkout(os.path.join(plain, "f.py")))

    def test_a_worktree_resolves_to_its_OWN_toplevel(self):
        # The whole design rests on this: two worktrees of one repo are two
        # different checkouts and must never contend with each other.
        root = make_repo()
        wt = os.path.join(root, "wt")
        git(root, "worktree", "add", "-b", "side", wt)
        self.assertTrue(os.path.isfile(os.path.join(wt, ".git")))  # a FILE, not a dir
        self.assertEqual(claim_lib.find_checkout(os.path.join(wt, "f.py")),
                         os.path.realpath(wt))
        self.assertNotEqual(claim_lib.find_checkout(os.path.join(wt, "f.py")),
                            claim_lib.find_checkout(os.path.join(root, "f.py")))

    def test_a_nonexistent_file_in_a_real_repo_still_resolves(self):
        # The hook runs BEFORE the write, so the file usually does not exist yet.
        root = make_repo()
        self.assertEqual(claim_lib.find_checkout(os.path.join(root, "brand-new.py")), root)

    def test_relative_paths_resolve_against_cwd(self):
        root = make_repo()
        cwd = os.getcwd()
        try:
            os.chdir(root)
            self.assertEqual(claim_lib.find_checkout("f.py"), root)
        finally:
            os.chdir(cwd)

    def test_empty_path_is_none(self):
        self.assertIsNone(claim_lib.find_checkout(""))
        self.assertIsNone(claim_lib.find_checkout(None))


class TestStore(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._real = claim_lib.CLAIMS_PATH
        claim_lib.CLAIMS_PATH = os.path.join(self.tmp, "claims.json")

    def tearDown(self):
        claim_lib.CLAIMS_PATH = self._real

    def owner(self, pid=None):
        pid = pid or os.getpid()
        return {"pid": pid, "proc_start": None, "session_id": "s-%d" % pid,
                "name": "n-%d" % pid}

    def test_missing_file_loads_as_empty(self):
        self.assertEqual(claim_lib.load_claims(), [])

    def test_corrupt_file_loads_as_empty(self):
        with open(claim_lib.CLAIMS_PATH, "w") as handle:
            handle.write("{not json")
        self.assertEqual(claim_lib.load_claims(), [])

    def test_non_list_file_loads_as_empty(self):
        with open(claim_lib.CLAIMS_PATH, "w") as handle:
            json.dump({"a": 1}, handle)
        self.assertEqual(claim_lib.load_claims(), [])

    def test_add_then_load_round_trips(self):
        records = claim_lib.add_claim([], "/repo", self.owner(), "/t.jsonl")
        claim_lib.save_claims(records)
        loaded = claim_lib.load_claims()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["checkout"], "/repo")
        self.assertEqual(loaded[0]["transcript_path"], "/t.jsonl")

    def test_add_is_idempotent_for_the_same_owner_and_checkout(self):
        records = claim_lib.add_claim([], "/repo", self.owner(), None)
        records = claim_lib.add_claim(records, "/repo", self.owner(), None)
        self.assertEqual(len(records), 1)

    def test_holder_ignores_your_own_claim(self):
        me = self.owner()
        records = claim_lib.add_claim([], "/repo", me, None)
        self.assertIsNone(claim_lib.holder(records, "/repo", me["pid"]))

    def test_holder_finds_a_different_live_owner(self):
        other = self.owner(pid=os.getpid())   # live pid, different owner id below
        records = claim_lib.add_claim([], "/repo", other, None)
        found = claim_lib.holder(records, "/repo", owner_pid=os.getpid() + 1)
        self.assertIsNotNone(found)
        self.assertEqual(found["checkout"], "/repo")

    def test_holder_ignores_a_dead_owner(self):
        dead = {"pid": 999999, "proc_start": None, "session_id": "x", "name": "x"}
        records = claim_lib.add_claim([], "/repo", dead, None)
        self.assertIsNone(claim_lib.holder(records, "/repo", os.getpid()))

    def test_holder_ignores_a_different_checkout(self):
        records = claim_lib.add_claim([], "/other", self.owner(), None)
        self.assertIsNone(claim_lib.holder(records, "/repo", os.getpid() + 1))

    def test_prune_drops_dead_and_keeps_live(self):
        records = claim_lib.add_claim([], "/a", self.owner(), None)
        records = claim_lib.add_claim(
            records, "/b", {"pid": 999999, "proc_start": None,
                            "session_id": "d", "name": "d"}, None)
        pruned = claim_lib.prune(records)
        self.assertEqual([r["checkout"] for r in pruned], ["/a"])

    def _live_child_record(self):
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        real_start = identity_lib.proc_start(child.pid)
        if real_start is None:
            self.skipTest("no ps")
        return {"checkout": "/x", "pid": child.pid, "proc_start": real_start}

    def test_prune_keeps_a_live_claim_when_the_table_is_unavailable(self):
        # {} is what start_times() returns when ps fails. prune's result gets
        # persisted, so reading a live holder as dead would erase its claim.
        record = self._live_child_record()
        self.assertEqual(claim_lib.prune([record], {}), [record])

    def test_holder_sees_a_live_claim_when_the_table_is_unavailable(self):
        record = self._live_child_record()
        self.assertEqual(claim_lib.holder([record], "/x", 1, starts={}), record)

    def test_prune_still_drops_a_pid_missing_from_a_working_table(self):
        # Control: a NON-empty table lacking the pid is a positive answer.
        record = self._live_child_record()
        self.assertEqual(claim_lib.prune([record], {1: "x"}), [])

    def test_release_drops_only_that_pid(self):
        mine = self.owner()
        theirs = {"pid": mine["pid"] + 1, "proc_start": None,
                  "session_id": "o", "name": "o"}
        records = claim_lib.add_claim([], "/a", mine, None)
        records = claim_lib.add_claim(records, "/b", theirs, None)
        left = claim_lib.release(records, mine["pid"])
        self.assertEqual([r["checkout"] for r in left], ["/b"])

    def test_save_never_raises_on_an_unwritable_path(self):
        claim_lib.CLAIMS_PATH = "/proc/definitely/not/writable/claims.json"
        claim_lib.save_claims([{"checkout": "/a"}])   # must not raise


class TestWorktreeCreation(unittest.TestCase):

    def test_names_are_deterministic_for_the_same_owner_and_checkout(self):
        owner = {"pid": 5, "proc_start": None, "session_id": "abcdef123456",
                 "name": "n"}
        first = claim_lib.worktree_paths("/x/my-repo", owner)
        second = claim_lib.worktree_paths("/x/my-repo", owner)
        self.assertEqual(first, second)
        path, branch = first
        self.assertIn(".claude/worktrees/", path)
        self.assertTrue(branch.startswith("claim/my-repo-"))

    def test_creates_a_real_worktree(self):
        root = make_repo()
        owner = {"pid": 5, "proc_start": None, "session_id": "abcdef123456",
                 "name": "n"}
        path, branch = claim_lib.worktree_paths(root, owner)
        self.assertTrue(claim_lib.create_worktree(root, path, branch))
        self.assertTrue(os.path.isdir(path))
        listed = subprocess.run(["git", "-C", root, "worktree", "list"],
                                capture_output=True, text=True).stdout
        registered = [os.path.realpath(line.split()[0])
                      for line in listed.splitlines() if line.strip()]
        self.assertIn(os.path.realpath(path), registered)

    def test_creation_is_idempotent_when_the_worktree_already_exists(self):
        root = make_repo()
        owner = {"pid": 5, "proc_start": None, "session_id": "abcdef123456",
                 "name": "n"}
        path, branch = claim_lib.worktree_paths(root, owner)
        self.assertTrue(claim_lib.create_worktree(root, path, branch))
        self.assertTrue(claim_lib.create_worktree(root, path, branch))

    def test_creation_failure_returns_false_not_an_exception(self):
        plain = tempfile.mkdtemp()   # not a git repo
        self.assertFalse(claim_lib.create_worktree(
            plain, os.path.join(plain, "wt"), "claim/x"))

    def test_a_plain_directory_at_the_target_path_is_not_a_worktree(self):
        # git worktree add can be killed mid-populate and leave a directory
        # behind with no .git and no entry in git's own registry. A plain
        # pre-existing directory must not be mistaken for a real worktree.
        root = make_repo()
        owner = {"pid": 5, "proc_start": None, "session_id": "abcdef123456",
                 "name": "n"}
        path, branch = claim_lib.worktree_paths(root, owner)
        os.makedirs(path)
        self.assertFalse(claim_lib.create_worktree(root, path, branch))


class TestWorktreeExclusion(unittest.TestCase):
    """The worktree is created for the BLOCKED session but lands in the
    HOLDER's checkout. Left untracked it shows up in their `git status`, and a
    routine `git add -A` stages it as an embedded gitlink -- so the artifact
    rides into the PR of the session that did nothing wrong.
    """

    def status(self, repo):
        return subprocess.run(["git", "-C", repo, "status", "--short"],
                              capture_output=True, text=True).stdout

    def owner(self):
        return {"pid": 5, "proc_start": None, "session_id": "abcdef123456",
                "name": "n"}

    def test_the_created_worktree_does_not_dirty_the_holders_status(self):
        root = make_repo()
        path, branch = claim_lib.worktree_paths(root, self.owner())
        self.assertTrue(claim_lib.create_worktree(root, path, branch))
        self.assertIn(".claude", self.status(root),
                      "precondition: without the exclude, git sees it")
        self.assertTrue(claim_lib.ensure_worktrees_ignored(root))
        self.assertEqual(self.status(root), "",
                         "the holder's working tree must come back clean")

    def test_it_is_idempotent(self):
        root = make_repo()
        for _ in range(3):
            self.assertTrue(claim_lib.ensure_worktrees_ignored(root))
        with open(os.path.join(root, ".git", "info", "exclude")) as handle:
            body = handle.read()
        self.assertEqual(body.count(claim_lib.EXCLUDE_ENTRY), 1)

    def test_it_appends_rather_than_replacing_existing_rules(self):
        root = make_repo()
        exclude = os.path.join(root, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "w") as handle:
            handle.write("*.secret\n")
        self.assertTrue(claim_lib.ensure_worktrees_ignored(root))
        with open(exclude) as handle:
            body = handle.read()
        self.assertIn("*.secret", body)
        self.assertIn(claim_lib.EXCLUDE_ENTRY, body)

    def test_it_appends_cleanly_when_the_file_has_no_trailing_newline(self):
        root = make_repo()
        exclude = os.path.join(root, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "w") as handle:
            handle.write("*.secret")          # no trailing newline
        self.assertTrue(claim_lib.ensure_worktrees_ignored(root))
        with open(exclude) as handle:
            lines = [line.strip() for line in handle]
        self.assertIn("*.secret", lines)
        self.assertIn(claim_lib.EXCLUDE_ENTRY, lines)

    def test_it_works_when_the_holder_is_itself_a_worktree(self):
        # `.git` is a FILE there, and git reads info/exclude from the COMMON
        # git dir -- writing into the worktree's own gitdir would produce a
        # file git never reads, and this fix would silently do nothing.
        root = make_repo()
        holder = os.path.join(root, "holder-wt")
        git(root, "worktree", "add", "-b", "holder", holder)
        self.assertTrue(os.path.isfile(os.path.join(holder, ".git")))
        path, branch = claim_lib.worktree_paths(holder, self.owner())
        self.assertTrue(claim_lib.create_worktree(holder, path, branch))
        self.assertIn(".claude", self.status(holder))
        self.assertTrue(claim_lib.ensure_worktrees_ignored(holder))
        self.assertEqual(self.status(holder), "")

    def test_a_directory_that_is_not_a_checkout_returns_false(self):
        plain = tempfile.mkdtemp()
        self.assertFalse(claim_lib.ensure_worktrees_ignored(plain))

    def test_a_broken_git_file_returns_false_rather_than_raising(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, ".git"), "w") as handle:
            handle.write("not a gitdir pointer")
        self.assertFalse(claim_lib.ensure_worktrees_ignored(root))

    def test_an_unwritable_exclude_file_returns_false_rather_than_raising(self):
        root = make_repo()
        exclude = os.path.join(root, ".git", "info", "exclude")
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "w") as handle:
            handle.write("*.secret\n")
        os.chmod(exclude, 0o400)
        try:
            self.assertFalse(claim_lib.ensure_worktrees_ignored(root))
        finally:
            os.chmod(exclude, 0o600)


# A child that takes claim_lib's lock, signals ready, and holds it until a
# release marker appears (bounded, so a broken test can't hang the suite
# forever). Mirrors lib/tests/test_portable.py's own child script.
CHILD_HOLDS_THE_LOCK = """
import sys
sys.path.insert(0, {tool_dir!r})
sys.path.insert(0, {lib_dir!r})
import os
import time
import claim_lib

with claim_lib.locked({lock_path!r}):
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


class TestLockedExcludesAcrossProcesses(unittest.TestCase):
    """claim_lib.locked() takes a REAL cross-process exclusive lock.

    A child process holds `locked(p)` open; while it does, a fresh
    `portable.exclusive_lock(p)` in this process must see the contention and
    yield False, and must yield True once the child releases. This is the
    property the refactor onto portable.exclusive_lock must preserve -- run
    unchanged against the fcntl-based implementation first (the baseline),
    then again after the refactor.
    """

    def test_a_child_holding_locked_excludes_the_parent_and_releases(self):
        tmp = tempfile.mkdtemp()
        lock_path = os.path.join(tmp, "lock")
        ready = os.path.join(tmp, "ready")
        release = os.path.join(tmp, "release")
        script = CHILD_HOLDS_THE_LOCK.format(
            tool_dir=TOOL_DIR, lib_dir=LIB_DIR, lock_path=lock_path,
            ready=ready, release=release)
        child = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            _wait_for(ready, 10, "child never signalled it holds the lock")

            with portable.exclusive_lock(lock_path, timeout=0) as acquired:
                self.assertFalse(acquired)

            open(release, "w").close()
            child.wait(timeout=10)
            self.assertEqual(child.returncode, 0, child.stderr.read())

            with portable.exclusive_lock(lock_path) as acquired:
                self.assertTrue(acquired)
        finally:
            if not os.path.exists(release):
                open(release, "w").close()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
            child.stdout.close()
            child.stderr.close()


class TestLocking(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._real = claim_lib.CLAIMS_PATH

    def tearDown(self):
        claim_lib.CLAIMS_PATH = self._real

    def test_yields_when_the_lock_directory_does_not_exist_yet(self):
        claim_lib.CLAIMS_PATH = os.path.join(self.tmp, "sub", "claims.json")
        ran = []
        with claim_lib.locked():
            ran.append(True)
        self.assertEqual(ran, [True])

    def test_yields_when_the_lock_path_is_unwritable(self):
        claim_lib.CLAIMS_PATH = "/proc/definitely/not/writable/claims.json"
        ran = []
        with claim_lib.locked():
            ran.append(True)   # must still run -- fail open, never raise
        self.assertEqual(ran, [True])

    def test_load_add_save_round_trips_inside_the_lock(self):
        claim_lib.CLAIMS_PATH = os.path.join(self.tmp, "claims.json")
        owner = {"pid": os.getpid(), "proc_start": None,
                 "session_id": "s", "name": "n"}
        with claim_lib.locked():
            records = claim_lib.load_claims()
            records = claim_lib.add_claim(records, "/repo", owner, None)
            claim_lib.save_claims(records)
        loaded = claim_lib.load_claims()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["checkout"], "/repo")


class TestTimeoutBudget(unittest.TestCase):
    """The REAL worst case for one hook invocation, not a subset of it.

    Both hooks are killed by the harness at the timeout in
    plugin-build/hooks/hooks.json. Killed means no verdict, which means the write
    is silently ALLOWED -- the guard fails exactly when contention is real.

    Earlier versions of this test accounted only for the lock and the worktree
    and asserted a relationship that was not the bound: identity_lib's `ps`
    calls were the LARGEST terms (5s each) and appeared nowhere, and prune()
    forked one MORE `ps` per claim record, so the true worst case was
    19 + 5N seconds against a 15s ceiling. Every term is enumerated below and
    every constant is read from the module that owns it.
    """

    # Read from plugin-build/hooks/hooks.json (PreToolUse session-claim, SessionEnd
    # session-release) rather than restated, so a manifest change that lowers
    # either timeout turns this test red instead of leaving a stale literal.
    MARGIN = 1

    @staticmethod
    def _declared_timeout(event, exe):
        import hook_manifest
        repo = os.path.dirname(LIB_DIR)
        rows = [r for r in hook_manifest.registrations(hook_manifest.load(repo))
                if r["event"] == event and r["exe"] == exe]
        if len(rows) != 1:
            raise AssertionError("expected exactly one %s registration of %s in "
                                 "plugin-build/hooks/hooks.json, found %d"
                                 % (event, exe, len(rows)))
        return rows[0]["timeout"]

    @property
    def HOOK_TIMEOUT(self):
        return self._declared_timeout("PreToolUse", "claude-session-claim")

    @property
    def RELEASE_TIMEOUT(self):
        return self._declared_timeout("SessionEnd", "claude-session-release")

    # `ps` calls charged against ONE claim-hook invocation:
    #   process_table()  identity_lib.resolve_owner
    #   start_times()    the hook, once, threaded into every liveness check
    # proc_start() is gone from resolve_owner: the owner's start time
    # is read from the start_times() table, so it costs no extra `ps`.
    # It is 2 no matter how many claim records or breaker entries exist --
    # asserted by the fork-count test below, which is the half of this budget
    # arithmetic cannot prove on its own.
    PS_CALLS = 2

    # The claims lock, then the breaker lock in record_block. Sequential within
    # one invocation, never held at the same time, so they add rather than
    # overlap. Both are claim_lib.LOCK_TIMEOUT -- breaker_lib borrows the same
    # locked() rather than carrying its own.
    LOCK_ACQUISITIONS = 2

    # The number of claim records the fork-count assertion is made at. Nothing
    # bounds the real file, which is the point: the cost must not depend on it.
    ASSUMED_RECORDS = 100

    def test_the_claim_hook_worst_case_fits_inside_its_timeout(self):
        budget = (self.PS_CALLS * identity_lib.PS_TIMEOUT
                  + self.LOCK_ACQUISITIONS * claim_lib.LOCK_TIMEOUT
                  # create_worktree runs `git worktree list` OR
                  # `git worktree add`, never both.
                  + claim_lib.WORKTREE_TIMEOUT)
        self.assertLessEqual(budget, self.HOOK_TIMEOUT - self.MARGIN,
                             "claim hook worst case %ss vs %ss ceiling"
                             % (budget, self.HOOK_TIMEOUT))

    def test_the_release_hook_worst_case_fits_inside_its_timeout(self):
        # Same two `ps` calls and both locks; no git, since release never
        # creates a worktree.
        budget = (self.PS_CALLS * identity_lib.PS_TIMEOUT
                  + self.LOCK_ACQUISITIONS * claim_lib.LOCK_TIMEOUT)
        self.assertLessEqual(budget, self.RELEASE_TIMEOUT - self.MARGIN,
                             "release hook worst case %ss vs %ss ceiling"
                             % (budget, self.RELEASE_TIMEOUT))

    def test_prune_forks_one_ps_no_matter_how_many_records(self):
        # The term the budget above assumes away. Before this, liveness forked
        # `ps` per record, which adds up with the record count, inside the lock,
        # on a path that runs on every file write -- and unbounded, since
        # nothing caps the record count.
        # The pid must be LIVE, and this is not cosmetic. `is_pid_live` runs
        # `os.kill(pid, 0)` FIRST and only forks `proc_start` after it passes,
        # so a dead pid returns before the line this test is about. Seeded with
        # `999000 + i` -- above the PID ceiling -- `proc_start` was unreachable
        # and `assertEqual(calls["proc_start"], 0)` held no matter what prune
        # did. Measured: the historical per-record-fork regression, reintroduced,
        # left this test green.
        child = subprocess.Popen([sys.executable, "-c",
                                  "import time; time.sleep(120)"],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        records = [{"checkout": "/r/%d" % i, "pid": child.pid,
                    "proc_start": "Thu Jan  1 00:00:00 1970"}
                   for i in range(self.ASSUMED_RECORDS)]
        calls = {"start_times": 0, "proc_start": 0}
        real_start_times = identity_lib.start_times
        real_proc_start = identity_lib.proc_start

        def counted_start_times():
            calls["start_times"] += 1
            return real_start_times()

        def counted_proc_start(pid):
            calls["proc_start"] += 1
            return real_proc_start(pid)

        identity_lib.start_times = counted_start_times
        identity_lib.proc_start = counted_proc_start
        try:
            pruned = claim_lib.prune(records)
        finally:
            identity_lib.start_times = real_start_times
            identity_lib.proc_start = real_proc_start
        self.assertEqual(pruned, [])       # all dead, all dropped
        self.assertEqual(calls["start_times"], 1)
        self.assertEqual(calls["proc_start"], 0,
                         "liveness must not fork per record")

    def test_prune_of_an_empty_list_forks_nothing_at_all(self):
        # The common case on a machine running one session: no claims, no cost.
        calls = []
        real = identity_lib.start_times
        identity_lib.start_times = lambda: calls.append(1) or {}
        try:
            self.assertEqual(claim_lib.prune([]), [])
        finally:
            identity_lib.start_times = real
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
