#!/usr/bin/env python3
"""Tests for claude-base-pin.

Two standards this file is held to, both from the repo's CLAUDE.md:

- **Hermetic.** Every git call runs with `GIT_CONFIG_GLOBAL` and
  `GIT_CONFIG_SYSTEM` pointed at /dev/null and an explicit `-b main`, so the
  machine's own `init.defaultBranch`, `pull.rebase` and hooksPath cannot change
  a result. Nothing here reads the real `~/.claude` or `~/.local/bin`.
- **The executable is tested, not just the lib.** `usage-watch` shipped 93 green
  unit tests with zero lines of any hook exercised, and a review reintroduced two
  already-fixed bugs through it. Every behavioural assertion below drives
  `claude-base-pin` as a real subprocess.
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
TOOL = os.path.join(os.path.dirname(HERE), "claude-base-pin")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "lib"))

import base_pin_lib as pin  # noqa: E402
import hook_manifest  # noqa: E402


def git(cwd, *args):
    """A hermetic git call. Raises on failure — fixtures must not fail quietly."""
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_TERMINAL_PROMPT": "0",
    })
    for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
        env.pop(var, None)
    return subprocess.run(
        ("git", "-C", cwd) + args, check=True, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True).stdout.strip()


class Fixture:
    """A bare origin, a primary clone, and a linked worktree on a branch.

    Deliberately a real repo rather than a mock. Everything under test is a
    question about git's own view of a checkout — `--git-dir` vs
    `--git-common-dir`, where FETCH_HEAD lives, what `HEAD..origin/main`
    counts — and a mock would be asserting this file's beliefs about git.
    """

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="base-pin-test-")
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)

        self.origin = os.path.join(self.root, "origin.git")
        subprocess.run(("git", "init", "--bare", "-b", "main", self.origin),
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

        self.primary = os.path.join(self.root, "primary")
        subprocess.run(("git", "clone", self.origin, self.primary),
                       check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        self._commit(self.primary, "first", "1")
        git(self.primary, "push", "-u", "origin", "main")

        self.worktree = os.path.join(self.primary, ".claude", "worktrees", "wt")
        git(self.primary, "worktree", "add", "-b", "feat/x", self.worktree,
            "origin/main")

    def _commit(self, repo, name, body):
        with open(os.path.join(repo, name), "w") as handle:
            handle.write(body)
        git(repo, "add", "-A")
        git(repo, "commit", "-m", "commit %s" % name)

    def advance_origin(self, n=1, prefix="landed"):
        """Land `n` commits on origin/main, as another session's merge would."""
        other = os.path.join(self.root, "other-%s" % prefix)
        if not os.path.isdir(other):
            subprocess.run(("git", "clone", self.origin, other), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        git(other, "fetch", "origin")
        git(other, "checkout", "-B", "main", "origin/main")
        for i in range(n):
            self._commit(other, "%s-%d" % (prefix, i), str(i))
        git(other, "push", "origin", "main")

    def fetch(self):
        """Refresh the primary's refs — shared by every worktree."""
        git(self.primary, "fetch", "origin")

    def state_file(self):
        return os.path.join(self.home, "state.json")

    def run_hook(self, cwd, session="s1", extra_env=None):
        """Drive the hook as a real subprocess. Returns (stdout, returncode)."""
        env = dict(os.environ)
        env.update({
            "HOME": self.home,
            "CLAUDE_TOOLKIT_CONFIG": "/nonexistent.json",
            "BASE_PIN_STATE_PATH": self.state_file(),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        })
        for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE",
                    "BASE_PIN_ALLOW"):
            env.pop(var, None)
        if extra_env:
            env.update(extra_env)
        payload = json.dumps({
            "session_id": session, "cwd": cwd,
            "hook_event_name": "UserPromptSubmit",
        })
        result = subprocess.run(
            (sys.executable, TOOL), input=payload, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, timeout=60)
        return result.stdout, result.returncode

    def destroy(self):
        shutil.rmtree(self.root, ignore_errors=True)


class HookBehaviour(unittest.TestCase):
    """The acceptance criteria, driven through the executable."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)

    def test_reports_when_origin_is_ahead(self):
        self.fx.advance_origin(3)
        self.fx.fetch()
        out, code = self.fx.run_hook(self.fx.worktree)
        self.assertEqual(code, 0)
        self.assertIn("IS AHEAD OF THIS WORKTREE", out)
        self.assertIn("3 commits landed", out)
        self.assertIn("git merge origin/main", out)

    def test_names_the_worktree_and_its_branch(self):
        self.fx.advance_origin(1)
        self.fx.fetch()
        out, _ = self.fx.run_hook(self.fx.worktree)
        self.assertIn("Worktree: wt", out)
        self.assertIn("feat/x", out)

    def test_singular_for_one_commit(self):
        self.fx.advance_origin(1)
        self.fx.fetch()
        out, _ = self.fx.run_hook(self.fx.worktree)
        self.assertIn("1 commit landed", out)
        self.assertNotIn("1 commits", out)

    def test_lists_the_commit_subjects(self):
        self.fx.advance_origin(2, prefix="subj")
        self.fx.fetch()
        out, _ = self.fx.run_hook(self.fx.worktree)
        self.assertIn("commit subj-0", out)
        self.assertIn("commit subj-1", out)

    def test_silent_on_second_prompt_with_unchanged_origin(self):
        self.fx.advance_origin(2)
        self.fx.fetch()
        first, _ = self.fx.run_hook(self.fx.worktree)
        self.assertIn("IS AHEAD", first)
        second, code = self.fx.run_hook(self.fx.worktree)
        self.assertEqual(second, "")
        self.assertEqual(code, 0)

    def test_fires_again_when_origin_moves_again(self):
        self.fx.advance_origin(1, prefix="a")
        self.fx.fetch()
        self.assertIn("IS AHEAD", self.fx.run_hook(self.fx.worktree)[0])
        self.assertEqual(self.fx.run_hook(self.fx.worktree)[0], "")
        self.fx.advance_origin(1, prefix="b")
        self.fx.fetch()
        self.assertIn("IS AHEAD", self.fx.run_hook(self.fx.worktree)[0])

    def test_dedupe_is_per_session(self):
        """A second session in the same worktree is told independently."""
        self.fx.advance_origin(1)
        self.fx.fetch()
        self.assertIn("IS AHEAD", self.fx.run_hook(self.fx.worktree, "s1")[0])
        self.assertIn("IS AHEAD", self.fx.run_hook(self.fx.worktree, "s2")[0])

    def test_silent_in_the_primary_checkout(self):
        """repo-freshness owns this case; two notices for one fact is noise."""
        self.fx.advance_origin(4)
        self.fx.fetch()
        out, code = self.fx.run_hook(self.fx.primary)
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_silent_when_not_behind(self):
        out, code = self.fx.run_hook(self.fx.worktree)
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_silent_after_merging_origin(self):
        """The merge-base moves, so a resolved drift stops being reported.

        This is the behaviour a recorded pin file would have got wrong: it
        would keep reporting a base the branch had already merged past.
        """
        self.fx.advance_origin(2)
        self.fx.fetch()
        self.assertIn("IS AHEAD", self.fx.run_hook(self.fx.worktree)[0])
        git(self.fx.worktree, "merge", "--no-edit", "origin/main")
        out, _ = self.fx.run_hook(self.fx.worktree, session="s-after-merge")
        self.assertEqual(out, "")

    # Two directions, because a stray GIT_DIR breaks detection BOTH ways and
    # only one of them was obvious. Measured 2026-08-26:
    #
    #   GIT_DIR=<worktree admin>, cwd=primary   -> values DIFFER
    #       -> primary misread as a worktree     (false positive: noise)
    #   GIT_DIR=<primary .git>,   cwd=worktree  -> values EQUAL
    #       -> worktree misread as primary       (false negative: SILENCE)
    #
    # The first version of this test set GIT_DIR to the worktree's own admin
    # dir while standing IN the worktree — the differ case, where the worktree
    # already differs, so it passed identically with and without the fix and
    # the mutation survived. Keep both directions; the silent one is the one
    # that matters and it is not the one you reach for first.

    def test_stray_git_dir_cannot_silence_a_real_worktree(self):
        """The FALSE NEGATIVE direction. GIT_DIR at the common .git makes both
        values resolve there and compare equal, so a linked worktree reads as
        the primary checkout and the hook goes quiet where it must speak."""
        self.fx.advance_origin(1)
        self.fx.fetch()
        _, common = pin.git_dirs(self.fx.worktree)
        out, code = self.fx.run_hook(
            self.fx.worktree, extra_env={"GIT_DIR": common})
        self.assertEqual(code, 0)
        self.assertIn("IS AHEAD", out)

    def test_stray_git_dir_cannot_make_the_primary_look_like_a_worktree(self):
        """The FALSE POSITIVE direction. GIT_DIR at a worktree's admin dir makes
        --git-dir report it while --git-common-dir stays put, so they differ and
        the primary checkout starts reporting drift repo-freshness already owns."""
        self.fx.advance_origin(1)
        self.fx.fetch()
        admin, _ = pin.git_dirs(self.fx.worktree)
        out, code = self.fx.run_hook(
            self.fx.primary, extra_env={"GIT_DIR": admin})
        self.assertEqual(code, 0)
        self.assertEqual(out, "")

    def test_escape_hatch_silences_it(self):
        self.fx.advance_origin(1)
        self.fx.fetch()
        out, code = self.fx.run_hook(
            self.fx.worktree, extra_env={"BASE_PIN_ALLOW": "1"})
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_notice_is_written_before_the_state_is_recorded(self):
        """Order matters, and the invariant had no test in either direction.

        Recording first opens a suppression path: stdout is buffered, so a
        kill or a broken pipe after `save_state` marks the sha announced while
        the notice is never seen for it again. Printing first can only cost a
        repeat. Asserted structurally because the failure needs a kill at a
        precise instant, which is not a test worth writing — but an unordered
        pair of lines is exactly what a later edit reshuffles.
        """
        with open(TOOL) as handle:
            body = handle.read()
        write_at = body.index("sys.stdout.write(notice)")
        save_at = body.index("pin.save_state(pin.record_reported(")
        self.assertLess(
            write_at, save_at,
            "save_state must not run before the notice is written and flushed")
        self.assertIn("sys.stdout.flush()", body)

    def test_records_dedupe_state_on_disk(self):
        self.fx.advance_origin(1)
        self.fx.fetch()
        self.fx.run_hook(self.fx.worktree)
        with open(self.fx.state_file()) as handle:
            state = json.load(handle)
        self.assertIn("s1", state)
        record = list(state["s1"].values())[0]
        self.assertIsInstance(record["sha"], str)
        self.assertEqual(len(record["sha"]), 40)


class HookFailurePaths(unittest.TestCase):
    """Every one of these must print nothing and exit 0."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)

    def _assert_quiet(self, out, code):
        self.assertEqual(out, "")
        self.assertEqual(code, 0)

    def test_cwd_is_not_a_repo(self):
        self._assert_quiet(*self.fx.run_hook(self.fx.home))

    def test_cwd_does_not_exist(self):
        self._assert_quiet(
            *self.fx.run_hook(os.path.join(self.fx.root, "nope")))

    def test_malformed_payload(self):
        result = subprocess.run(
            (sys.executable, TOOL), input="not json", text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self._assert_quiet(result.stdout, result.returncode)

    def test_payload_missing_cwd(self):
        result = subprocess.run(
            (sys.executable, TOOL), input=json.dumps({"session_id": "s"}),
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60)
        self._assert_quiet(result.stdout, result.returncode)

    def test_payload_missing_session_id(self):
        """No session id means no dedupe key, so speaking would repeat forever."""
        self.fx.advance_origin(1)
        self.fx.fetch()
        result = subprocess.run(
            (sys.executable, TOOL),
            input=json.dumps({"cwd": self.fx.worktree}), text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self._assert_quiet(result.stdout, result.returncode)

    def test_no_origin_ref_at_all(self):
        """A worktree in a repo with no remote-tracking ref cannot be judged."""
        solo = os.path.join(self.fx.root, "solo")
        subprocess.run(("git", "init", "-b", "main", solo), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(os.path.join(solo, "f"), "w") as handle:
            handle.write("x")
        git(solo, "add", "-A")
        git(solo, "commit", "-m", "only")
        wt = os.path.join(solo, "wt")
        git(solo, "worktree", "add", "-b", "b", wt)
        self._assert_quiet(*self.fx.run_hook(wt))

    def test_unwritable_state_file_still_reports(self):
        """A failed write must not suppress the notice.

        A repeated notice is a nuisance; a suppressed one is the failure this
        tool exists to prevent, so the asymmetry is deliberate.
        """
        self.fx.advance_origin(1)
        self.fx.fetch()
        out, code = self.fx.run_hook(
            self.fx.worktree,
            extra_env={"BASE_PIN_STATE_PATH": "/proc/nope/state.json"})
        self.assertEqual(code, 0)
        self.assertIn("IS AHEAD", out)


class LibContracts(unittest.TestCase):
    """Unit-level contracts that the subprocess tests cannot pin precisely."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)

    def test_dirs_are_linked_is_the_predicate_the_hook_uses(self):
        """Pure, and shared with the hook.

        The hook calls this rather than inlining `git_dir == common_dir`, so
        inverting it breaks the hook's behaviour too. In the first cut the
        inline comparison meant inverting the tested function failed only its
        own unit test while the hook kept working — the production branch had
        no assertion on it.
        """
        self.assertIs(pin.dirs_are_linked(("/a/.git/worktrees/w", "/a/.git")),
                      True)
        self.assertIs(pin.dirs_are_linked(("/a/.git", "/a/.git")), False)
        self.assertIsNone(pin.dirs_are_linked(None))

    def test_is_linked_worktree_three_states(self):
        """`assertIs`/`assertIsNone`, never `assertFalse`.

        `False` and `None` are both falsy, so `assertFalse` would pass for
        either and erase the distinction this Optional[bool] exists to carry.
        """
        self.assertIs(pin.is_linked_worktree(self.fx.worktree), True)
        self.assertIs(pin.is_linked_worktree(self.fx.primary), False)
        self.assertIsNone(pin.is_linked_worktree(self.fx.home))

    def test_git_refuses_empty_cwd(self):
        """`git -C ""` is a silent no-op that answers about the WRONG dir."""
        self.assertIsNone(pin._git("", "rev-parse", "HEAD"))

    def test_spawn_background_fetch_refuses_empty_cwd(self):
        self.assertIs(pin.spawn_background_fetch(""), False)

    def test_git_dirs_are_absolute(self):
        dirs = pin.git_dirs(self.fx.worktree)
        self.assertIsNotNone(dirs)
        for path in dirs:
            self.assertTrue(os.path.isabs(path), path)

    def test_seconds_since_fetch_reads_the_common_dir(self):
        """freshness_lib joins <repo>/.git/FETCH_HEAD, which is a FILE here.

        Using that path from a worktree returns None forever, so `should_fetch`
        would fire a background fetch on every single prompt submit, silently.
        """
        self.fx.fetch()
        _, common = pin.git_dirs(self.fx.worktree)
        self.assertIsNotNone(pin.seconds_since_fetch(common))
        stale = os.path.join(self.fx.worktree, ".git", "FETCH_HEAD")
        self.assertFalse(os.path.exists(stale))

    def test_seconds_since_fetch_unknown_without_a_fetch(self):
        self.assertIsNone(pin.seconds_since_fetch(self.fx.home))
        self.assertIsNone(pin.seconds_since_fetch(""))

    def test_should_fetch_is_pure(self):
        self.assertIs(pin.should_fetch(None), True)
        self.assertIs(pin.should_fetch(0.0, 100), False)
        self.assertIs(pin.should_fetch(100.0, 100), True)
        self.assertIs(pin.should_fetch(99.0, 100), False)

    def test_behind_count_counts_against_the_merge_base(self):
        self.fx.advance_origin(3)
        self.fx.fetch()
        self.assertEqual(pin.behind_count(self.fx.worktree, "main"), 3)
        self.fx._commit(self.fx.worktree, "mine", "m")
        self.assertEqual(
            pin.behind_count(self.fx.worktree, "main"), 3,
            "own commits must not change how far behind the base is")

    def test_default_branch_falls_back_to_main(self):
        self.assertEqual(pin.default_branch(self.fx.worktree), "main")
        self.assertEqual(pin.default_branch(self.fx.home), "main")

    def test_render_returns_none_when_nothing_to_say(self):
        for count in (0, None, -1):
            self.assertIsNone(pin.render(count, "main", "wt", "b"))

    def test_render_never_claims_the_ref_moved_since_you_started(self):
        """Wording is load-bearing.

        origin/<branch> may already have been ahead when the worktree was
        created, so "moved since you started" can be false while the count is
        true. A notice that overstates gets distrusted, and a distrusted notice
        is worth less than none.
        """
        text = pin.render(2, "main", "wt", "feat/x")
        self.assertIn("IS AHEAD OF THIS WORKTREE", text)
        self.assertNotIn("since you started", text)
        self.assertNotIn("moved since", text)

    def test_max_commits_listed_zero_lists_nothing(self):
        self.fx.advance_origin(2)
        self.fx.fetch()
        self.assertEqual(pin.new_commits(self.fx.worktree, "main", 0), [])


class StateTable(unittest.TestCase):

    def test_already_reported_keys_on_the_sha(self):
        state = pin.record_reported({}, "s", "/c", "aaa")
        self.assertIs(pin.already_reported(state, "s", "/c", "aaa"), True)
        self.assertIs(
            pin.already_reported(state, "s", "/c", "bbb"), False,
            "a new origin sha must re-fire; a boolean flag would go quiet")

    def test_already_reported_is_scoped_per_session_and_checkout(self):
        state = pin.record_reported({}, "s", "/c", "aaa")
        self.assertIs(pin.already_reported(state, "other", "/c", "aaa"), False)
        self.assertIs(pin.already_reported(state, "s", "/other", "aaa"), False)

    def test_prune_drops_expired(self):
        old = time.time() - pin.STATE_TTL_SECONDS - 1
        state = {"s": {"/c": {"sha": "a", "at": old}}}
        self.assertEqual(pin.prune_state(state), {})

    def test_prune_keeps_fresh(self):
        state = {"s": {"/c": {"sha": "a", "at": time.time()}}}
        self.assertIn("s", pin.prune_state(state))

    def test_prune_drops_malformed_without_raising(self):
        """Deserialisation is a caller, and it supplies shapes no constructor will."""
        for bad in ({"s": "not-a-dict"},
                    {"s": {"/c": "not-a-dict"}},
                    {"s": {"/c": {}}},
                    {"s": {"/c": {"sha": "a"}}},
                    {"s": {"/c": {"at": time.time()}}},
                    {"s": {"/c": {"sha": 5, "at": time.time()}}},
                    {"s": {"/c": {"sha": "a", "at": "nope"}}}):
            self.assertEqual(pin.prune_state(bad), {}, bad)

    def test_load_state_tolerates_garbage(self):
        root = tempfile.mkdtemp(prefix="base-pin-state-")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "s.json")
        with open(path, "w") as handle:
            handle.write("{not json")
        self.assertEqual(pin.load_state(path), {})
        with open(path, "w") as handle:
            handle.write("[1,2,3]")
        self.assertEqual(pin.load_state(path), {})
        self.assertEqual(pin.load_state(os.path.join(root, "absent")), {})

    def test_save_state_is_atomic_and_leaves_no_temp(self):
        root = tempfile.mkdtemp(prefix="base-pin-state-")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "nested", "s.json")
        self.assertIs(pin.save_state({"s": {"/c": {"sha": "a", "at": 1}}},
                                     path), True)
        self.assertEqual(pin.load_state(path)["s"]["/c"]["sha"], "a")
        self.assertEqual(
            [n for n in os.listdir(os.path.dirname(path)) if ".tmp." in n], [])

    def test_save_state_reports_failure_rather_than_raising(self):
        self.assertIs(pin.save_state({}, "/proc/nope/deeper/s.json"), False)


class FetchWiring(unittest.TestCase):
    """Does the hook actually fetch when stale, and NOT when fresh?

    This had ZERO coverage in the first cut, and that is why a real defect
    shipped through 45 green tests: the read used the common dir while the
    write landed in the worktree admin dir, so the clock never advanced and
    the hook spawned a detached `git fetch` on every prompt submit forever.
    Replacing `spawn_background_fetch(cwd)` with `pass` left every test green.

    `test_should_fetch_is_pure` cannot see any of that — it asserts the
    predicate, which was always correct. The bug was in the WIRING, so the
    test has to drive the hook and count real fetches.

    A `git` PATH shim logs argv and then `exec`s the real binary, so the
    observation does not change behaviour — the pattern CLAUDE.md prescribes
    for session-claim's `ps`/`git` probes. And per the same rule, a probe that
    observes nothing passes a "does no work" assertion for free, so the
    fresh-FETCH_HEAD positive control below is mandatory, not optional.
    """

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.destroy)
        self.shim_dir = os.path.join(self.fx.root, "shim")
        os.makedirs(self.shim_dir)
        self.log = os.path.join(self.fx.root, "git-calls.log")
        real = shutil.which("git")
        self.assertIsNotNone(real, "no git on PATH")
        shim = os.path.join(self.shim_dir, "git")
        with open(shim, "w") as handle:
            # `%%s` for the shell's own printf format; only the log path and
            # the real binary are Python substitutions.
            handle.write(
                '#!/bin/sh\n'
                'printf "%%s\\n" "$*" >> "%s"\n'
                'exec "%s" "$@"\n' % (self.log, real))
        os.chmod(shim, 0o755)

    def _fetches(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as handle:
            return [l for l in handle.read().splitlines() if " fetch " in l]

    def _run(self, session="s1"):
        return self.fx.run_hook(
            self.fx.worktree, session=session,
            extra_env={"PATH": self.shim_dir + os.pathsep + os.environ["PATH"]})

    def _worktree_fetch_head(self):
        git_dir, _ = pin.git_dirs(self.fx.worktree)
        return os.path.join(git_dir, "FETCH_HEAD")

    def test_the_shim_observes_git_at_all(self):
        """Positive control for the probe itself, before trusting a count."""
        self._run()
        with open(self.log) as handle:
            self.assertIn("rev-parse", handle.read())

    def test_fetches_when_the_clock_says_stale(self):
        try:
            os.unlink(self._worktree_fetch_head())
        except OSError:
            pass
        self._run()
        self.assertEqual(len(self._fetches()), 1, self._fetches())

    def test_does_not_fetch_again_while_the_clock_is_fresh(self):
        """The regression test. Four prompts, one fetch — not four.

        Measured on the defective version: 4 submits produced 4 fetches,
        because the hook's own fetch wrote a FETCH_HEAD the hook never read.
        """
        try:
            os.unlink(self._worktree_fetch_head())
        except OSError:
            pass
        for i in range(4):
            self._run(session="s%d" % i)
        self.assertEqual(
            len(self._fetches()), 1,
            "expected exactly one fetch across 4 prompts, got %d: %s"
            % (len(self._fetches()), self._fetches()))

    def test_the_hooks_own_fetch_advances_the_clock_it_reads(self):
        """The invariant behind the bug, asserted directly.

        Read and write must name the same git-dir. Asserting the count alone
        would pass if BOTH sides moved to some third consistent location that
        git never writes — this pins the actual file.
        """
        target = self._worktree_fetch_head()
        try:
            os.unlink(target)
        except OSError:
            pass
        self.assertIsNone(
            pin.seconds_since_fetch(pin.git_dirs(self.fx.worktree)[0]),
            "clock should read unknown with FETCH_HEAD removed")
        self._run()
        for _ in range(100):
            if os.path.exists(target):
                break
            time.sleep(0.05)
        self.assertTrue(
            os.path.exists(target),
            "the hook's fetch did not write the FETCH_HEAD the hook reads")
        self.assertIsNotNone(
            pin.seconds_since_fetch(pin.git_dirs(self.fx.worktree)[0]))

    def test_fetch_head_is_per_worktree_not_common(self):
        """Pins the git behaviour the whole fix rests on.

        Measured on git 2.50.1: `git -C <worktree> fetch` writes
        <common>/worktrees/<name>/FETCH_HEAD and never <common>/FETCH_HEAD.
        If git ever changes this, the fix's premise is gone and this fails —
        which is the point.
        """
        git_dir, common = pin.git_dirs(self.fx.worktree)
        self.assertNotEqual(git_dir, common)
        for path in (os.path.join(common, "FETCH_HEAD"),
                     os.path.join(git_dir, "FETCH_HEAD")):
            try:
                os.unlink(path)
            except OSError:
                pass
        git(self.fx.worktree, "fetch", "--quiet", "origin")
        self.assertTrue(os.path.exists(os.path.join(git_dir, "FETCH_HEAD")))
        self.assertFalse(os.path.exists(os.path.join(common, "FETCH_HEAD")))


class TimeoutBudget(unittest.TestCase):
    """The git budget must fit the timeout declared for the hook.

    Not a style check. If the harness kills this hook mid-git it produces no
    output, and for a notice tool no output is indistinguishable from "nothing
    to report" — so going over budget fails SILENT, in exactly the direction
    the tool exists to prevent. Same shape as `session-claim`'s
    `TestTimeoutBudget`, and the same instruction applies: if you change one of
    the numbers, run this.
    """

    def _declared_timeout(self):
        repo = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
        for row in hook_manifest.registrations(hook_manifest.load(repo)):
            if row["event"] == "UserPromptSubmit" and row["exe"] == "claude-base-pin":
                self.assertEqual("tools/base-pin/claude-base-pin", row["relpath"])
                return row["timeout"]
        return None

    def test_hook_is_registered_on_user_prompt_submit(self):
        """A tool nothing invokes is installed inert, and looks fine."""
        self.assertIsNotNone(
            self._declared_timeout(),
            "claude-base-pin missing from the plugin manifest UserPromptSubmit")

    def test_git_budget_fits_the_declared_timeout(self):
        declared = self._declared_timeout()
        self.assertIsNotNone(declared)
        worst = pin.MAX_GIT_CALLS * pin.GIT_TIMEOUT_SECONDS
        self.assertLessEqual(
            worst, declared,
            "%d git calls x %ds = %ds exceeds the declared %ss timeout"
            % (pin.MAX_GIT_CALLS, pin.GIT_TIMEOUT_SECONDS, worst, declared))

    def test_max_git_calls_matches_the_code(self):
        """Counts every `_git(` call site in the lib. Re-derived, not declared.

        The first version filtered a HARDCODED six-name tuple, so `len(...)`
        was 6 no matter what the code did — a new lib function calling `_git`
        and invoked by the hook left the suite green with the worst case at
        7 x 2 = 14s against a 15s timeout. That is prose in Python, and
        CLAUDE.md's instruction on session-claim's budget is explicit:
        re-derive from the constants rather than trusting the prose.

        Counting call sites over-counts if a `_git` caller is not on the
        reporting path. That is the safe direction: it forces whoever adds one
        to look at the budget, which is the whole point.
        """
        with open(os.path.join(os.path.dirname(HERE),
                               "base_pin_lib.py")) as handle:
            lib = handle.read()
        # Every `_git(` occurrence except its own definition.
        sites = lib.count("_git(") - lib.count("def _git(")
        self.assertEqual(
            sites, pin.MAX_GIT_CALLS,
            "found %d `_git(` call sites but MAX_GIT_CALLS is %d — a new "
            "blocking git call needs budgeting against the declared timeout"
            % (sites, pin.MAX_GIT_CALLS))


class ConfigAbsent(unittest.TestCase):
    """A fresh clone has no config at all, and that must be the working case."""

    def test_defaults_when_config_is_missing(self):
        self.assertIs(pin.is_enabled(), True)
        self.assertEqual(pin.fetch_interval(),
                         pin.DEFAULT_FETCH_INTERVAL_SECONDS)
        self.assertEqual(pin.max_commits_listed(),
                         pin.DEFAULT_MAX_COMMITS_LISTED)
        self.assertTrue(pin.state_path().endswith(".json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
