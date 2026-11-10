#!/usr/bin/env python3
"""Tests for claude-wrap-up-survey.

Held to this repo's two standards:

- **Hermetic.** Every fixture repo is created with `GIT_CONFIG_GLOBAL` and
  `GIT_CONFIG_SYSTEM` at /dev/null and an explicit `-b main`, so the machine's
  own `init.defaultBranch`, `pull.rebase` or hooksPath cannot change a result.
  Nothing reads the real workspace, `~/.claude`, or the network.
- **The executable is tested, not just the lib.** A green lib suite says
  nothing about the script a session runs. `TestExecutable` drives
  `claude-wrap-up-survey` as a real subprocess.

The assertions that matter most are the ones separating "no" from "cannot
tell" — a survey that reports a checkout it failed to read as clean is the
exact silent failure this tool exists to prevent.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.join(os.path.dirname(HERE), "claude-wrap-up-survey")
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(HERE))), "lib"))

import wrap_up_lib  # noqa: E402


def git(cwd, *args):
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = "t"
    env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = "t@example.com"
    for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
        env.pop(var, None)
    return subprocess.run(
        ("git", "-C", cwd) + args,
        capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
    )


def make_repo(root, name):
    """A repo with one commit on `main`, plus a bare 'remote' it tracks."""
    path = os.path.join(root, name)
    os.makedirs(path)
    git(path, "init", "-b", "main")
    with open(os.path.join(path, "f.txt"), "w") as fh:
        fh.write("one\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "one")

    remote = os.path.join(root, name + ".git")
    git(root, "init", "--bare", "-b", "main", remote)
    git(path, "remote", "add", "origin", remote)
    git(path, "push", "-u", "origin", "main")
    return path, remote


def commit(path, text, name="f.txt"):
    with open(os.path.join(path, name), "a") as fh:
        fh.write(text + "\n")
    git(path, "add", ".")
    git(path, "commit", "-m", text)


class TestGitGuards(unittest.TestCase):
    def test_an_empty_path_returns_none_rather_than_running(self):
        # `git -C ""` is a silent no-op that describes the CWD. Without this
        # guard the tool answers confidently about a directory nobody named.
        self.assertIsNone(wrap_up_lib._git("", "rev-parse", "HEAD"))

    def test_a_nonexistent_path_returns_none(self):
        self.assertIsNone(wrap_up_lib._git("/nonexistent/xyz", "rev-parse", "HEAD"))

    def test_git_env_vars_are_cleared(self):
        env = wrap_up_lib._env()
        for var in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
            self.assertNotIn(var, env)


class TestSurveyCheckout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])
        self.repo, self.remote = make_repo(self.tmp, "alpha")

    def test_a_clean_tracked_checkout_is_quiet(self):
        s = wrap_up_lib.survey_checkout(self.repo)
        self.assertEqual(s["branch"], "main")
        self.assertEqual(s["dirty_files"], 0)
        self.assertEqual(s["upstream"], "ok")
        self.assertEqual(s["unpushed"], 0)
        self.assertIs(s["local_only"], False)
        self.assertIs(s["pr_check_required"], False)

    def test_uncommitted_files_are_counted(self):
        with open(os.path.join(self.repo, "new.txt"), "w") as fh:
            fh.write("x\n")
        self.assertEqual(wrap_up_lib.survey_checkout(self.repo)["dirty_files"], 1)

    def test_unpushed_commits_are_counted(self):
        commit(self.repo, "two")
        self.assertEqual(wrap_up_lib.survey_checkout(self.repo)["unpushed"], 1)

    def test_a_branch_that_was_never_pushed_is_local_only(self):
        # The unambiguous case: no upstream at all, so no PR can exist for it.
        git(self.repo, "checkout", "-b", "scratch")
        commit(self.repo, "work")
        s = wrap_up_lib.survey_checkout(self.repo)
        self.assertEqual(s["upstream"], "none")
        self.assertEqual(s["commits_not_in_default"], 1)
        self.assertIs(s["local_only"], True)
        # Never pushed means no PR to check — asking gh here is noise.
        self.assertIs(s["pr_check_required"], False)

    def test_a_pushed_branch_ahead_of_default_defers_to_gh(self):
        # The squash trap: this branch may be fully merged and still read as
        # ahead. The tool must not conclude either way.
        git(self.repo, "checkout", "-b", "feature")
        commit(self.repo, "work")
        git(self.repo, "push", "-u", "origin", "feature")
        s = wrap_up_lib.survey_checkout(self.repo)
        self.assertEqual(s["upstream"], "ok")
        self.assertIs(s["pr_check_required"], True)
        self.assertIs(s["local_only"], False)

    def test_a_deleted_upstream_reads_as_gone_not_none(self):
        # 'gone' and 'none' are different problems: gone means git tidy may
        # delete the branch; none means it was never pushed.
        git(self.repo, "checkout", "-b", "feature")
        commit(self.repo, "work")
        git(self.repo, "push", "-u", "origin", "feature")
        git(self.repo, "push", "origin", "--delete", "feature")
        git(self.repo, "fetch", "--prune", "origin")
        self.assertEqual(wrap_up_lib.upstream_state(self.repo), "gone")

    def test_default_branch_is_not_guessed_when_it_does_not_exist(self):
        # Guessing 'main' where no such ref exists would make every downstream
        # count silently empty rather than absent.
        bare = os.path.join(self.tmp, "empty")
        os.makedirs(bare)
        git(bare, "init", "-b", "trunk")
        self.assertIsNone(wrap_up_lib.default_branch(bare))


class TestAttention(unittest.TestCase):
    def test_an_unreadable_status_is_surfaced_not_treated_as_clean(self):
        # The core silent failure. None means "could not tell" and must appear.
        result = {"repos": [{
            "name": "x", "path": "/x", "branch": "main",
            "dirty_files": None, "unpushed": 0, "worktrees": [],
        }]}
        items = wrap_up_lib.attention(result)
        self.assertEqual(len(items), 1)
        self.assertIn("could not read status", items[0]["reasons"][0])

    # These fixtures carry `default_branch` because `survey_checkout` always
    # sets it, and `attention` treats its absence as a could-not-tell. An
    # earlier version of these dicts omitted it — they were passing only
    # because nothing yet looked, which is the same shape as the defect this
    # module is about.
    def test_a_clean_checkout_produces_no_item(self):
        result = {"repos": [{
            "name": "x", "path": "/x", "branch": "main", "default_branch": "main",
            "dirty_files": 0, "unpushed": 0, "upstream": "ok", "worktrees": [],
        }]}
        self.assertEqual(wrap_up_lib.attention(result), [])

    def test_worktrees_are_flattened_into_attention(self):
        result = {"repos": [{
            "name": "x", "path": "/x", "branch": "main", "default_branch": "main",
            "dirty_files": 0, "unpushed": 0, "upstream": "ok",
            "worktrees": [{
                "path": "/x/wt", "branch": "feat", "default_branch": "main",
                "dirty_files": 2, "unpushed": None, "upstream": "none",
            }],
        }]}
        items = wrap_up_lib.attention(result)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["path"], "/x/wt")


    def _checkout(self, **overrides):
        base = {
            "name": "x", "path": "/x", "branch": "main", "default_branch": "main",
            "dirty_files": 0, "unpushed": 0, "upstream": "ok", "worktrees": [],
        }
        base.update(overrides)
        return {"repos": [base]}

    def _reasons(self, result):
        return [r for item in wrap_up_lib.attention(result) for r in item["reasons"]]

    def test_an_uncountable_unpushed_total_is_attention(self):
        reasons = self._reasons(self._checkout(upstream="ok", unpushed=None))
        self.assertIn("could not count unpushed commits", reasons)

    def test_a_zero_unpushed_count_has_no_could_not_count_reason(self):
        reasons = self._reasons(self._checkout(upstream="ok", unpushed=0))
        self.assertNotIn("could not count unpushed commits", reasons)

    def test_an_uncountable_ahead_total_is_attention(self):
        reasons = self._reasons(self._checkout(
            branch="feat", default_branch="main", commits_not_in_default=None))
        self.assertIn("could not count commits ahead of main", reasons)

    def test_default_branch_with_no_ahead_count_is_not_a_gap(self):
        reasons = self._reasons(self._checkout(
            branch="main", default_branch="main", commits_not_in_default=None))
        self.assertFalse([r for r in reasons if r.startswith("could not count")])


class TestSurveyRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])

    def _failing_worktree_list(self):
        real = wrap_up_lib._git

        def fake(cwd, *args):
            if args[:3] == ("worktree", "list", "--porcelain"):
                return None
            return real(cwd, *args)
        return mock.patch.object(wrap_up_lib, "_git", side_effect=fake)

    def _repo_with_dirty_worktree(self):
        root = os.path.join(self.tmp, "repos")
        os.makedirs(root)
        repo, _ = make_repo(root, "proj")
        wt = os.path.join(self.tmp, "wt")
        git(repo, "worktree", "add", "-b", "feat", wt)
        with open(os.path.join(wt, "untracked.txt"), "w") as fh:
            fh.write("x\n")
        return root, repo, wt

    def test_failed_worktree_listing_is_attention_not_all_clear(self):
        root, repo, wt = self._repo_with_dirty_worktree()
        # Positive control: unpatched, the dirty worktree is in attention.
        items = wrap_up_lib.attention(wrap_up_lib.survey(root))
        self.assertIn(os.path.realpath(wt),
                      [os.path.realpath(i["path"]) for i in items])
        with self._failing_worktree_list():
            result = wrap_up_lib.survey(root)
        self.assertIs(result["surveyed"], True)
        reasons = [r for i in wrap_up_lib.attention(result) for r in i["reasons"]]
        self.assertTrue([r for r in reasons if r.startswith("could not list worktrees")],
                        reasons)

    def test_list_worktrees_returns_none_when_git_fails(self):
        root, repo, wt = self._repo_with_dirty_worktree()
        self.assertEqual(len(wrap_up_lib.list_worktrees(repo)), 1)
        with self._failing_worktree_list():
            self.assertIsNone(wrap_up_lib.list_worktrees(repo))

    def test_repo_git_cannot_open_is_surveyed_not_dropped(self):
        os.makedirs(os.path.join(self.tmp, "broken", ".git"))
        os.makedirs(os.path.join(self.tmp, "plain"))
        found = [os.path.basename(p) for p in wrap_up_lib.find_repos(self.tmp)]
        self.assertIn("broken", found)
        self.assertNotIn("plain", found)
        items = wrap_up_lib.attention(wrap_up_lib.survey(self.tmp))
        broken = [i for i in items if i["repo"] == "broken"]
        self.assertEqual(len(broken), 1)
        self.assertIn("could not read status", broken[0]["reasons"])

    def test_an_absent_root_reports_that_it_could_not_look(self):
        # Not an empty list: "nothing to report" and "did not look" are
        # different answers and only one of them is safe to shut down on.
        # A typo'd repos_root would otherwise certify the machine as clean.
        result = wrap_up_lib.survey("/nonexistent/root/xyz")
        self.assertIs(result["surveyed"], False)
        self.assertIn("does not exist", result["reason"])
        self.assertEqual(result["repos"], [])

    def test_an_empty_but_real_root_is_a_genuine_all_clear(self):
        # The control for the test above: an existing directory with no repos
        # really did get looked at, and must NOT be reported as unsurveyed.
        # Without this, "always return surveyed: false" would pass.
        result = wrap_up_lib.survey(self.tmp)
        self.assertIs(result["surveyed"], True)
        self.assertEqual(result["repos"], [])

    def test_no_configured_root_is_reported_as_unsurveyed(self):
        # Hermetic: `_config` is neutralised rather than the real config being
        # consulted. The previous version of this test read the machine's own
        # config and degraded to `else {"surveyed": False}` — on any installed
        # machine it asserted a dict literal it had just written, so flipping
        # this early return to True left the whole suite green. A test that
        # cannot fail is the failure this repo names most often.
        saved = wrap_up_lib._config
        wrap_up_lib._config = None
        try:
            self.assertIsNone(wrap_up_lib.repos_root())
            result = wrap_up_lib.survey(None)
        finally:
            wrap_up_lib._config = saved
        self.assertIs(result["surveyed"], False)
        self.assertIn("no workspace_root", result["reason"])
        self.assertEqual(result["repos"], [])

    def test_a_repo_with_no_remote_at_all_is_not_reported_as_clean(self):
        # The case that zeroed out every signal: on the default branch, so
        # `commits_not_in_default` is None, and no upstream, so `unpushed` is
        # None too. Everything here exists on exactly one disk.
        path = os.path.join(self.tmp, "unpushed")
        os.makedirs(path)
        git(path, "init", "-b", "main")
        with open(os.path.join(path, "a.txt"), "w") as fh:
            fh.write("only copy\n")
        git(path, "add", ".")
        git(path, "commit", "-m", "one")

        result = wrap_up_lib.survey(self.tmp)
        entry = result["repos"][0]
        self.assertEqual(entry["upstream"], "none")
        self.assertIsNone(entry["commits_not_in_default"])
        items = wrap_up_lib.attention(result)
        self.assertTrue(items, "a repo that was never pushed must not survey as clean")
        self.assertIn("no upstream", " ".join(items[0]["reasons"]))

    def test_an_unresolvable_default_branch_is_surfaced(self):
        # `default_branch` None makes every count None. Silent nulls here are
        # could-not-tells and must reach the human.
        path = os.path.join(self.tmp, "trunk")
        os.makedirs(path)
        git(path, "init", "-b", "trunk")
        with open(os.path.join(path, "a.txt"), "w") as fh:
            fh.write("x\n")
        git(path, "add", ".")
        git(path, "commit", "-m", "one")

        result = wrap_up_lib.survey(self.tmp)
        self.assertIsNone(result["repos"][0]["default_branch"])
        reasons = " ".join(wrap_up_lib.attention(result)[0]["reasons"])
        self.assertIn("default branch", reasons)

    def test_non_git_directories_are_skipped(self):
        os.makedirs(os.path.join(self.tmp, "notarepo"))
        make_repo(self.tmp, "real")
        names = [r["name"] for r in wrap_up_lib.survey(self.tmp)["repos"]]
        self.assertEqual(names, ["real"])

    def test_worktrees_are_surveyed(self):
        repo, _ = make_repo(self.tmp, "alpha")
        wt = os.path.join(self.tmp, "wt")
        git(repo, "worktree", "add", "-b", "side", wt)
        entry = wrap_up_lib.survey(self.tmp)["repos"][0]
        self.assertEqual(len(entry["worktrees"]), 1)
        self.assertEqual(entry["worktrees"][0]["branch"], "side")


class TestExecutable(unittest.TestCase):
    """Drive the real script, the way a session does."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(subprocess.run, ["rm", "-rf", self.tmp])

    def run_tool(self, *args, config=None, extra_env=None):
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = config or "/nonexistent.json"
        env["HOME"] = os.path.join(self.tmp, "home")
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, TOOL, *args],
            capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
            timeout=60,
        )

    def test_it_emits_valid_json_and_exits_zero(self):
        make_repo(self.tmp, "alpha")
        proc = self.run_tool("--root", self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIs(payload["surveyed"], True)
        self.assertEqual(payload["repos"][0]["name"], "alpha")

    def test_it_reports_dirty_work_through_attention(self):
        repo, _ = make_repo(self.tmp, "alpha")
        with open(os.path.join(repo, "scratch.txt"), "w") as fh:
            fh.write("unsaved\n")
        payload = json.loads(self.run_tool("--root", self.tmp).stdout)
        self.assertTrue(payload["attention"])
        self.assertIn("uncommitted", payload["attention"][0]["reasons"][0])

    def test_with_no_config_and_no_root_it_says_it_did_not_survey(self):
        # `run_tool` pins CLAUDE_TOOLKIT_CONFIG at a path that does not exist,
        # so this is deterministic rather than machine-dependent. It used to
        # guard its only assertion behind `if payload["surveyed"] is False`,
        # which meant it never asserted the thing its name promises.
        proc = self.run_tool()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIs(payload["surveyed"], False)
        self.assertIn("reason", payload)
        self.assertEqual(payload["repos"], [])

    def test_a_root_flag_with_no_value_is_refused(self):
        # It used to fall through and survey the CONFIGURED root — answering
        # confidently about a directory the caller never named.
        proc = self.run_tool("--root")
        self.assertEqual(proc.returncode, 3, proc.stdout)
        self.assertIn("--root needs a directory", proc.stderr)
        self.assertEqual(proc.stdout.strip(), "")

    def write_config(self, data):
        path = os.path.join(self.tmp, "config.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def test_a_configured_root_expands_variables(self):
        # Same reader as claude-backup (toolkit_config.repos_root), so both
        # tools name the same folder for the same config.
        root = os.path.join(self.tmp, "code")
        make_repo(root, "alpha")
        config = self.write_config({"wrap_up": {"repos_root": "$WRAP_UP_TEST_ROOT"}})
        proc = self.run_tool(config=config, extra_env={"WRAP_UP_TEST_ROOT": root})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["repos_root"], root)
        self.assertIs(payload["surveyed"], True)
        self.assertEqual(payload["repos"][0]["name"], "alpha")

    def test_an_unusable_configured_root_is_named_not_defaulted(self):
        # With a workspace root whose repos/ holds a repo, a fallback to the
        # default would survey it and look like a real answer.
        workspace = os.path.join(self.tmp, "ws")
        make_repo(os.path.join(workspace, "repos"), "alpha")
        config = self.write_config({"workspace_root": workspace,
                                    "wrap_up": {"repos_root": "relative/repos"}})
        payload = json.loads(self.run_tool(config=config).stdout)
        self.assertIs(payload["surveyed"], False)
        self.assertIn("wrap_up.repos_root", payload["reason"])
        self.assertEqual(payload["repos"], [])

    def test_it_runs_with_no_config_at_all(self):
        # What a fresh clone looks like. A missing config must never raise.
        make_repo(self.tmp, "alpha")
        proc = self.run_tool("--root", self.tmp)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
