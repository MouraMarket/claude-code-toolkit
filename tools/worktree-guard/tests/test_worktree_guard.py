#!/usr/bin/env python3
"""Behavioural tests for claude-worktree-guard.

Runs the real hook as a subprocess with real PreToolUse payloads and asserts
only on its exit code: 0 allows, 2 blocks. A fake that only matched the
script's regexes against a string would prove nothing about the toplevel
comparison that both rules actually depend on, so these tests build real
throwaway git repos with real worktrees in a temp directory and point the
guard at them.
"""

import ast
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

GUARD = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "claude-worktree-guard")
WORKTREE_LIB = os.path.join(os.path.dirname(GUARD), "worktree_lib.py")

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DANGER_GUARD = os.path.join(TOOLS_DIR, "danger-guard", "claude-danger-guard")

sys.path.insert(0, os.path.join(os.path.dirname(TOOLS_DIR), "lib"))
import hook_manifest  # noqa: E402

ALLOW, BLOCK = 0, 2

# A crash must never pass for an evaluated ALLOW. The Python guard's entry
# point turns an unexpected exception into a JSON systemMessage on stdout
# (carrying this literal marker) and still exits 0 -- indistinguishable from a
# real allow by exit code alone, so every place that spawns the guard and
# reads a result fails the test outright when the marker shows up, rather than
# reporting a quiet ALLOW. Same pattern as claude-danger-guard's test suite.
_CRASH_MARKER = "claude-worktree-guard crashed"


def _fail_if_crashed(command, stdout):
    if _CRASH_MARKER in stdout:
        raise AssertionError(
            "claude-worktree-guard crashed rather than judging: %r\nstdout: %s"
            % (command, stdout))


def git(*args, cwd):
    subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                   capture_output=True, text=True)


def make_repo_with_worktree(root):
    """A repo with two registered worktrees: `main` (branch main) and
    `wt` (branch feature), so Rule B is active for either checkout."""
    main = os.path.join(root, "main")
    wt = os.path.join(root, "wt")
    os.makedirs(main)
    git("init", "-q", cwd=main)
    git("config", "user.email", "t@example.com", cwd=main)
    git("config", "user.name", "Test", cwd=main)
    with open(os.path.join(main, "f.txt"), "w") as handle:
        handle.write("hi\n")
    git("add", "-A", cwd=main)
    git("commit", "-q", "-m", "init", cwd=main)
    git("worktree", "add", "-q", wt, "-b", "feature", cwd=main)
    return main, wt


def make_single_worktree_repo(root):
    """A repo with exactly one checkout — Rule B must never fire here."""
    repo = os.path.join(root, "solo")
    os.makedirs(repo)
    git("init", "-q", cwd=repo)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    with open(os.path.join(repo, "f.txt"), "w") as handle:
        handle.write("hi\n")
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    git("branch", "feature", cwd=repo)
    return repo


def run_guard_capture(tool_name, tool_input, cwd, env=None, config_path=None):
    """The single primitive. Returns the whole CompletedProcess so a caller
    can assert on stderr (the block message), not just the exit code -- needed
    to distinguish Rule C's delete verdict from its history-rewriting one,
    which share an exit code by design.

    CLAUDE_TOOLKIT_CONFIG defaults to a nonexistent path so these tests never
    depend on whatever config happens to be installed on the machine running
    them. That is required for Rule A/B too, not just Rule C: before Rule C
    landed this helper set no config at all and the suite inherited the
    machine's, which is exactly the ambient-state dependency a hermetic
    test must not have. It passed everywhere only because nothing read
    config yet.
    """
    payload = json.dumps({"tool_name": tool_name, "tool_input": tool_input,
                          "cwd": cwd})
    environ = dict(os.environ)
    environ.pop("WORKTREE_ALLOW", None)
    environ.pop("WT_ALLOW_MAIN", None)
    environ["CLAUDE_TOOLKIT_CONFIG"] = config_path or "/nonexistent.json"
    if env:
        environ.update(env)
    proc = subprocess.run([GUARD], input=payload, capture_output=True,
                          text=True, env=environ)
    _fail_if_crashed(tool_input.get("command", tool_name), proc.stdout)
    return proc


def run_guard_full(tool_name, tool_input, cwd, env=None, config_path=None):
    """Like run_guard, but for any tool -- Edit/Write/MultiEdit/NotebookEdit
    included, which Rule C guards and the git tokenizer never sees."""
    return run_guard_capture(tool_name, tool_input, cwd, env=env,
                             config_path=config_path).returncode


def run_guard(command, cwd, env=None, config_path=None):
    return run_guard_full("Bash", {"command": command}, cwd, env=env,
                          config_path=config_path)


def guard_stderr(command, cwd, env=None, config_path=None):
    """The refusal text. `block` writes to stderr and exits 2."""
    return run_guard_capture("Bash", {"command": command}, cwd, env=env,
                             config_path=config_path).stderr


_GIT_PATH_POSIX_LINE = 'GIT_PATH_POSIX = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"'


def guard_copy_with_git_path(bindir):
    """Copy `tools/worktree-guard/` and `tools/danger-guard/` into a fresh
    sandbox under `<sandbox>/tools/`, preserving their sibling layout --
    `worktree_lib.py`'s lazy `guard_lib` import walks `../danger-guard`
    relative to its OWN directory, so the copy has to keep the two as
    siblings rather than copying the worktree guard alone. Also copies
    `lib/toolkit_config.py` to `<sandbox>/lib/`: the guards load it from three
    levels above the guard module (`<root>/lib/toolkit_config.py`), so the
    sandbox must mirror the repo layout or the copy cannot resolve config
    paths at all. Rewrites the copy's `GIT_PATH_POSIX` line to name
    `bindir`, which is the only thing that controls which `git` the bounded
    runner (`make_runners`, in `worktree_lib.py`) actually execs -- unlike
    the zsh original, nothing here needs jq, perl, grep or any other tool on
    that PATH, since the Python guard shells out to nothing but git (and, on
    Windows, cygpath). Returns the copy's entry-point path.
    """
    sandbox = tempfile.mkdtemp()
    atexit.register(shutil.rmtree, sandbox, ignore_errors=True)
    shutil.copytree(os.path.dirname(GUARD),
                    os.path.join(sandbox, "tools", "worktree-guard"))
    shutil.copytree(os.path.dirname(DANGER_GUARD),
                    os.path.join(sandbox, "tools", "danger-guard"))
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(GUARD)))
    os.makedirs(os.path.join(sandbox, "lib"))
    shutil.copy(os.path.join(repo_root, "lib", "toolkit_config.py"),
                os.path.join(sandbox, "lib", "toolkit_config.py"))

    lib_path = os.path.join(sandbox, "tools", "worktree-guard",
                            "worktree_lib.py")
    with open(lib_path) as handle:
        source = handle.read()
    found = source.count(_GIT_PATH_POSIX_LINE)
    if found != 1:
        raise AssertionError(
            "expected exactly one GIT_PATH_POSIX line in worktree_lib.py, "
            "found %d: %r" % (found, _GIT_PATH_POSIX_LINE))
    with open(lib_path, "w") as handle:
        handle.write(source.replace(
            _GIT_PATH_POSIX_LINE, "GIT_PATH_POSIX = %r" % bindir, 1))

    return os.path.join(sandbox, "tools", "worktree-guard",
                        "claude-worktree-guard")


# `_run` -- the one inner function `make_runners` builds that actually spawns
# anything -- is the ONLY place this guard may reach subprocess or an os
# exec/spawn call. `make_runners`' own body (outside `_run`) and `git`/
# `msys_path` (the closures it returns, which only ever call `_run`) do not
# count: a bare call anywhere else, including directly in `make_runners`, is
# an unbounded hang waiting to happen -- see GIT_TIMEOUT_SECONDS/BUDGET_SECONDS'
# own comment in worktree_lib.py.
_BOUNDED_RUNNER_FUNC = "_run"


def subprocess_offenders(source, filename="<string>"):
    """AST canary, replacing the zsh original's text-matching one (which
    enumerated shell contexts a bare `git` call might appear in -- meaningless
    here, since Python has no such contexts to enumerate). Returns a list of
    "<file>:<line>: <base>.<attr>" strings for every `subprocess` attribute
    access, or `os.system`/`os.popen`/`os.exec*`/`os.spawn*` call, found
    OUTSIDE a function lexically nested inside `_run` -- an empty list means
    clean. Deliberately narrower than "inside make_runners": make_runners'
    own body, outside `_run`, is not covered either."""
    tree = ast.parse(source, filename=filename)
    offenders = []

    def walk(node, enclosing):
        if isinstance(node, ast.FunctionDef):
            enclosing = enclosing + [node.name]
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            base, attr = node.value.id, node.attr
            is_subprocess = base == "subprocess"
            is_os_exec = (base == "os" and (
                attr in ("system", "popen")
                or attr.startswith("exec") or attr.startswith("spawn")))
            if ((is_subprocess or is_os_exec)
                    and _BOUNDED_RUNNER_FUNC not in enclosing):
                offenders.append("%s:%d: %s.%s"
                                 % (filename, node.lineno, base, attr))
        for child in ast.iter_child_nodes(node):
            walk(child, enclosing)

    walk(tree, [])
    return offenders


class WorktreeGuardTestCase(unittest.TestCase):
    """Shared fixture: one repo with two worktrees (main + wt), one repo
    with a single checkout (solo)."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp()
        cls.main, cls.wt = make_repo_with_worktree(cls._dir)
        cls.solo = make_single_worktree_repo(cls._dir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def assertAllowed(self, command, cwd):
        self.assertEqual(run_guard(command, cwd), ALLOW,
                         "expected ALLOW: %s (cwd=%s)" % (command, cwd))

    def assertBlocked(self, command, cwd):
        self.assertEqual(run_guard(command, cwd), BLOCK,
                         "expected BLOCK: %s (cwd=%s)" % (command, cwd))


class TestRuleACrossCheckoutReach(WorktreeGuardTestCase):
    """A state-mutating git command whose resolved target differs from cwd."""

    def test_commit_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s commit -am x" % self.wt, self.main)

    def test_checkout_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s checkout main" % self.main, self.wt)

    def test_push_via_leading_cd_is_blocked(self):
        self.assertBlocked("cd %s && git push" % self.wt, self.main)

    # A harmless git statement first must not hide a cross-checkout one after
    # it: each separator ends the first statement's argument scan, so the
    # second `git` is judged on its own. Without these, a separator set that
    # lost `&&` or `||` left every other test green.
    def test_cross_checkout_commit_after_an_and_chain_is_blocked(self):
        self.assertBlocked("git status && git -C %s commit -am x" % self.wt,
                           self.main)

    def test_cross_checkout_commit_after_an_or_chain_is_blocked(self):
        self.assertBlocked("git status || git -C %s commit -am x" % self.wt,
                           self.main)

    def test_merge_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s merge feature" % self.wt, self.main)

    def test_reset_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s reset --hard" % self.wt, self.main)

    def test_stash_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s stash" % self.wt, self.main)

    def test_add_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s add -A" % self.wt, self.main)

    def test_restore_via_dash_c_is_blocked(self):
        # Allowed in one's own checkout,
        # but Rule A still applies once it reaches into another checkout.
        self.assertBlocked("git -C %s restore f.txt" % self.wt, self.main)

    def test_checkout_dashdash_path_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s checkout -- f.txt" % self.wt, self.main)

    # The verb set came from walking git's porcelain commands asking
    # "can this alter another checkout's working tree, index, or refs?" —
    # these were missing and passed cross-checkout before the fix.
    def test_clean_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s clean -fd" % self.wt, self.main)

    def test_rm_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s rm f.txt" % self.wt, self.main)

    def test_apply_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s apply x.patch" % self.wt, self.main)

    def test_am_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s am x.patch" % self.wt, self.main)

    def test_mv_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s mv f.txt g.txt" % self.wt, self.main)

    def test_revert_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s revert HEAD" % self.wt, self.main)

    def test_submodule_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s submodule update" % self.wt, self.main)

    def test_tag_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s tag v1" % self.wt, self.main)

    # More verbs the same walk found: sparse-checkout rewrites
    # the target's actual working-tree contents (the same risk class as
    # clean/rm above); notes and replace both write refs.
    def test_sparse_checkout_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s sparse-checkout set foo" % self.wt, self.main)

    def test_notes_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s notes add -m x" % self.wt, self.main)

    def test_replace_via_dash_c_is_blocked(self):
        self.assertBlocked("git -C %s replace HEAD HEAD~0" % self.wt, self.main)


class TestMonitorIsJudgedLikeBashByRuleA(WorktreeGuardTestCase):
    """The Monitor tool runs a shell command, so Rule A applies to it too
    (R1-06). Paired with Bash so the pair can only diverge by tool name."""

    def _both(self, command, cwd):
        return (run_guard_full("Bash", {"command": command}, cwd),
                run_guard_full("Monitor", {"command": command}, cwd))

    def test_cross_checkout_commit_is_blocked_under_both_tools(self):
        bash, monitor = self._both("git -C %s commit -m x" % self.main, self.wt)
        self.assertEqual((bash, monitor), (BLOCK, BLOCK))

    def test_in_place_status_is_allowed_under_both_tools(self):
        bash, monitor = self._both("git status", self.wt)
        self.assertEqual((bash, monitor), (ALLOW, ALLOW))


class TestRuleADoesNotFireInPlace(WorktreeGuardTestCase):
    """Same set of commands, run without reaching into another checkout."""

    def test_commit_in_place_is_allowed(self):
        self.assertAllowed("git commit -am x", self.main)

    def test_push_in_place_is_allowed(self):
        self.assertAllowed("git push", self.main)

    def test_merge_in_place_is_allowed(self):
        self.assertAllowed("git merge feature", self.main)

    def test_cross_checkout_status_is_allowed(self):
        # Read-only: never state-mutating, regardless of target.
        self.assertAllowed("git -C %s status" % self.wt, self.main)

    def test_cross_checkout_log_is_allowed(self):
        self.assertAllowed("git -C %s log -1" % self.wt, self.main)

    def test_cross_checkout_worktree_list_is_exempt(self):
        self.assertAllowed("git -C %s worktree list" % self.wt, self.main)

    # Same newly added F1 verbs, run in place — must stay invisible there.
    def test_clean_in_place_is_allowed(self):
        self.assertAllowed("git clean -fd", self.main)

    def test_rm_in_place_is_allowed(self):
        self.assertAllowed("git rm --cached f.txt", self.main)

    def test_mv_in_place_is_allowed(self):
        self.assertAllowed("git mv f.txt renamed.txt", self.main)

    def test_tag_in_place_is_allowed(self):
        self.assertAllowed("git tag v1", self.main)

    # Same newly added F5 verbs, run in place — must stay invisible there.
    def test_sparse_checkout_in_place_is_allowed(self):
        self.assertAllowed("git sparse-checkout set foo", self.main)

    def test_notes_in_place_is_allowed(self):
        self.assertAllowed("git notes add -m x", self.main)

    def test_replace_in_place_is_allowed(self):
        self.assertAllowed("git replace HEAD HEAD~0", self.main)

    # F5's DELIBERATELY EXEMPT list is load-bearing, not decorative: `config`
    # is a representative exempt verb (settings, not tracked content) and
    # must stay allowed even reaching cross-checkout.
    def test_config_cross_checkout_is_exempt(self):
        self.assertAllowed("git -C %s config --get user.name" % self.wt, self.main)

    def test_cross_checkout_worktree_add_is_exempt(self):
        self.assertAllowed("git -C %s worktree add -q %s -b throwaway"
                           % (self.main, os.path.join(self._dir, "throwaway")),
                           self.wt)

    def test_cross_checkout_worktree_remove_is_exempt(self):
        # Deliberately not blocked: git already refuses a dirty removal, and
        # removing a finished worktree from elsewhere is the correct cleanup.
        throwaway = os.path.join(self._dir, "throwaway-for-removal")
        git("worktree", "add", "-q", throwaway, "-b", "throwaway-for-removal",
            cwd=self.main)
        self.assertAllowed("git -C %s worktree remove %s" % (self.main, throwaway),
                           self.wt)


class TestRuleBBranchSwitchingUnderConcurrency(WorktreeGuardTestCase):
    """Blocked shapes, run in place, in the repo that has >=2 worktrees."""

    def test_checkout_branch_is_blocked(self):
        self.assertBlocked("git checkout feature", self.main)

    def test_switch_branch_is_blocked(self):
        self.assertBlocked("git switch main", self.wt)

    def test_checkout_dash_b_new_branch_is_blocked(self):
        self.assertBlocked("git checkout -b brand-new", self.main)

    def test_checkout_dash_cap_b_new_branch_is_blocked(self):
        self.assertBlocked("git checkout -B brand-new", self.main)

    def test_switch_dash_c_new_branch_is_blocked(self):
        self.assertBlocked("git switch -c brand-new", self.main)

    def test_reset_hard_is_blocked(self):
        self.assertBlocked("git reset --hard", self.main)

    def test_reset_merge_is_blocked(self):
        self.assertBlocked("git reset --merge", self.main)

    def test_reset_keep_is_blocked(self):
        self.assertBlocked("git reset --keep", self.main)

    def test_bare_stash_is_blocked(self):
        self.assertBlocked("git stash", self.main)

    def test_stash_push_is_blocked(self):
        self.assertBlocked("git stash push -m wip", self.main)

    def test_stash_pop_is_blocked(self):
        self.assertBlocked("git stash pop", self.main)

    def test_stash_apply_is_blocked(self):
        self.assertBlocked("git stash apply", self.main)

    def test_stash_drop_is_blocked(self):
        self.assertBlocked("git stash drop", self.main)

    def test_stash_clear_is_blocked(self):
        self.assertBlocked("git stash clear", self.main)


class TestRuleBExplicitlyAllowed(WorktreeGuardTestCase):
    """Every shape Rule B allows, even in a >=2-worktree repo."""

    def test_checkout_dashdash_path_is_allowed(self):
        self.assertAllowed("git checkout -- f.txt", self.main)

    def test_restore_is_allowed(self):
        self.assertAllowed("git restore f.txt", self.main)

    def test_commit_is_allowed(self):
        self.assertAllowed("git commit -am x", self.main)

    def test_add_is_allowed(self):
        self.assertAllowed("git add -A", self.main)

    def test_diff_is_allowed(self):
        self.assertAllowed("git diff", self.main)

    def test_log_is_allowed(self):
        self.assertAllowed("git log -1", self.main)

    def test_status_is_allowed(self):
        self.assertAllowed("git status", self.main)

    def test_show_is_allowed(self):
        self.assertAllowed("git show HEAD", self.main)

    def test_fetch_is_allowed(self):
        self.assertAllowed("git fetch", self.main)

    def test_push_is_allowed(self):
        self.assertAllowed("git push", self.main)

    def test_pull_is_allowed(self):
        self.assertAllowed("git pull", self.main)

    def test_merge_is_allowed(self):
        self.assertAllowed("git merge feature", self.main)

    def test_rebase_is_allowed(self):
        self.assertAllowed("git rebase feature", self.main)

    def test_cherry_pick_is_allowed(self):
        self.assertAllowed("git cherry-pick --abort", self.main)

    def test_branch_listing_is_allowed(self):
        self.assertAllowed("git branch", self.main)

    def test_worktree_add_is_allowed(self):
        self.assertAllowed(
            "git worktree add -q %s -b another" % os.path.join(self._dir, "another"),
            self.main)

    def test_worktree_prune_is_allowed(self):
        self.assertAllowed("git worktree prune", self.main)

    def test_rev_parse_is_allowed(self):
        self.assertAllowed("git rev-parse HEAD", self.main)

    def test_stash_list_is_allowed(self):
        self.assertAllowed("git stash list", self.main)

    def test_stash_show_is_allowed(self):
        self.assertAllowed("git stash show", self.main)


class TestBranchSwitchRefusalNamesTheRestoreEscapes(WorktreeGuardTestCase):
    """`git checkout <file>` is refused, so the refusal has to say what works.

    Without `--`, a bare argument is a branch to switch to or a file to
    restore, and the guard does not ask git which. Refusing is the right
    direction — letting a real switch through would be the silent failure —
    but a caller who meant a file restore then has a working command and no
    way to learn it from the refusal.

    Each escape is asserted TWICE: that the message names it, and that it is
    genuinely allowed. Naming alone would keep passing if the guard's own
    behaviour changed underneath the text, which is the shape of advice that
    reads correct and no longer is.
    """

    def setUp(self):
        self.message = guard_stderr("git checkout feature", self.main)

    def test_the_refusal_names_git_restore(self):
        self.assertIn("git restore <path>", self.message)

    def test_git_restore_really_is_allowed(self):
        self.assertAllowed("git restore f.txt", self.main)

    def test_the_refusal_names_checkout_dashdash(self):
        self.assertIn("git checkout -- <path>", self.message)

    def test_checkout_dashdash_really_is_allowed(self):
        self.assertAllowed("git checkout -- f.txt", self.main)

    def test_the_refusal_still_names_the_branch_switch_route(self):
        # The restore advice must not crowd out what the caller asked for.
        self.assertIn("git worktree add <path> <branch>", self.message)

    def test_the_switch_refusal_does_not_offer_a_restore_route(self):
        # `git switch` has no file-restore reading at all, so naming one
        # there would be advice that cannot work.
        switch_message = guard_stderr("git switch feature", self.main)
        self.assertIn("git worktree add <path> <branch>", switch_message)
        self.assertNotIn("git restore", switch_message)

    def test_push_is_allowed(self):
        self.assertAllowed("git push", self.main)

    def test_pull_is_allowed(self):
        self.assertAllowed("git pull", self.main)

    def test_merge_is_allowed(self):
        self.assertAllowed("git merge feature", self.main)

    def test_rebase_is_allowed(self):
        self.assertAllowed("git rebase feature", self.main)

    def test_cherry_pick_is_allowed(self):
        self.assertAllowed("git cherry-pick --abort", self.main)

    def test_branch_listing_is_allowed(self):
        self.assertAllowed("git branch", self.main)

    def test_worktree_add_is_allowed(self):
        self.assertAllowed(
            "git worktree add -q %s -b another" % os.path.join(self._dir, "another"),
            self.main)

    def test_worktree_prune_is_allowed(self):
        self.assertAllowed("git worktree prune", self.main)

    def test_rev_parse_is_allowed(self):
        self.assertAllowed("git rev-parse HEAD", self.main)

    def test_stash_list_is_allowed(self):
        self.assertAllowed("git stash list", self.main)

    def test_stash_show_is_allowed(self):
        self.assertAllowed("git stash show", self.main)


class TestRuleBInactiveOnSingleCheckout(WorktreeGuardTestCase):
    """The exact same blocked shapes must be invisible in a one-worktree repo."""

    def test_checkout_branch_is_allowed(self):
        self.assertAllowed("git checkout feature", self.solo)

    def test_checkout_dash_b_is_allowed(self):
        self.assertAllowed("git checkout -b brand-new", self.solo)

    def test_reset_hard_is_allowed(self):
        self.assertAllowed("git reset --hard", self.solo)

    def test_bare_stash_is_allowed(self):
        self.assertAllowed("git stash", self.solo)


class TestRuleAThroughParenthesizedSubshells(WorktreeGuardTestCase):
    """`( cd X && ... )` must be caught exactly like the
    unparenthesized form — it's the idiom a careful script uses precisely
    because it does NOT leak its cd into the caller's shell."""

    def test_unparenthesized_cd_is_blocked(self):
        # Baseline: proves the fixture actually blocks this shape at all.
        self.assertBlocked("cd %s && git commit -am x" % self.wt, self.main)

    def test_parenthesized_cd_is_blocked(self):
        self.assertBlocked("( cd %s && git commit -am x )" % self.wt, self.main)

    def test_nested_parenthesized_cd_is_blocked(self):
        self.assertBlocked("( ( cd %s && git commit -am x ) )" % self.wt, self.main)

    def test_directory_context_pops_back_after_subshell_closes(self):
        # The cd inside ( ... ) is scoped to that subshell in a real shell;
        # a command AFTER it closes must see the ORIGINAL directory context,
        # not the one left behind by the subshell.
        self.assertAllowed(
            "( cd %s && git status ) && git commit -am x" % self.wt, self.main)


class TestRuleAThroughGitDirAndWorkTree(WorktreeGuardTestCase):
    """--git-dir/--work-tree are git's own target-selecting
    flags, parsed in the same loop as -C, and were bypassing resolution
    entirely before the fix."""

    def test_work_tree_and_git_dir_separate_args_is_blocked(self):
        self.assertBlocked(
            "git --work-tree=%s --git-dir=%s/.git commit -am x" % (self.wt, self.wt),
            self.main)

    def test_work_tree_attached_form_is_blocked(self):
        self.assertBlocked("git --work-tree=%s commit -am x" % self.wt, self.main)

    def test_git_dir_only_resolves_to_its_parent_is_blocked(self):
        self.assertBlocked("git --git-dir=%s/.git commit -am x" % self.wt, self.main)

    def test_work_tree_pointing_at_own_checkout_is_allowed(self):
        self.assertAllowed("git --work-tree=%s commit -am x" % self.main, self.main)


class TestRuleBBranchExemptionStaysAllowed(WorktreeGuardTestCase):
    """Locks in a deliberate exemption: `branch` (including a mutating
    -D) is exempt from both rules. Git itself already refuses to delete or
    move a branch another worktree holds, so there is no gap here to close,
    and this must not silently regress back to blocking it."""

    def test_branch_dash_cap_d_cross_checkout_is_allowed(self):
        self.assertAllowed(
            "git -C %s branch -D some-unrelated-branch" % self.wt, self.main)

    def test_branch_dash_cap_d_in_place_is_allowed(self):
        self.assertAllowed("git branch -D some-unrelated-branch", self.main)


class TestEscapeHatchAndHeredocs(WorktreeGuardTestCase):

    def test_worktree_allow_escape_hatch(self):
        self.assertEqual(
            run_guard("git checkout feature", self.main, env={"WORKTREE_ALLOW": "1"}),
            ALLOW)

    def test_without_escape_hatch_still_blocks(self):
        # Sanity check the fixture actually blocks, so the escape-hatch test
        # above is proving something.
        self.assertBlocked("git checkout feature", self.main)

    def test_heredoc_mentioning_checkout_is_allowed(self):
        self.assertAllowed(
            "cat > notes.md <<'EOF'\nnever run git checkout other-branch\nEOF",
            self.main)

    def test_heredoc_mentioning_cross_checkout_reach_is_allowed(self):
        self.assertAllowed(
            "cat > notes.md <<'EOF'\ncd %s && git commit -am x\nEOF" % self.wt,
            self.main)


class TestRuleBFiresWhenCwdIsNotARepo(WorktreeGuardTestCase):
    """cwd not being inside a repo must skip Rule A (nothing to
    compare against) but still evaluate Rule B against whatever checkout the
    command resolves to — the user's ordinary project-root cwd is exactly
    this shape (a plain directory holding several repos as subdirectories)."""

    def test_cd_into_two_worktree_repo_then_checkout_is_blocked(self):
        self.assertBlocked("cd %s && git checkout feature" % self.main, self._dir)

    def test_cd_into_two_worktree_repo_then_reset_hard_is_blocked(self):
        self.assertBlocked("cd %s && git reset --hard" % self.main, self._dir)

    def test_dash_c_into_two_worktree_repo_then_checkout_is_blocked(self):
        self.assertBlocked("git -C %s checkout feature" % self.main, self._dir)

    def test_cd_into_single_worktree_repo_then_checkout_is_allowed(self):
        # Rule B stays invisible in a single-checkout repo, same as always.
        self.assertAllowed("cd %s && git checkout feature" % self.solo, self._dir)

    def test_commit_in_place_from_non_repo_cwd_is_allowed(self):
        # No cd/-C at all: target resolves to cwd itself, which is not a
        # repo either, so nothing can be resolved — allow, don't guess.
        self.assertAllowed("git commit -am x", self._dir)


class TestRuleBStashFlagBypasses(WorktreeGuardTestCase):
    """I4: a flag as stash's first token (e.g. `-u`) is not a subcommand at
    all — git treats it as an implicit `push`, same as bare `git stash`."""

    def test_stash_dash_u_is_blocked(self):
        self.assertBlocked("git stash -u", self.main)

    def test_stash_include_untracked_is_blocked(self):
        self.assertBlocked("git stash --include-untracked", self.main)

    def test_stash_dash_k_is_blocked(self):
        self.assertBlocked("git stash -k", self.main)

    def test_stash_keep_index_is_blocked(self):
        self.assertBlocked("git stash --keep-index", self.main)

    def test_stash_dash_m_message_is_blocked(self):
        self.assertBlocked("git stash -m note", self.main)

    def test_stash_dash_p_is_blocked(self):
        self.assertBlocked("git stash -p", self.main)

    def test_stash_dash_cap_s_is_blocked(self):
        self.assertBlocked("git stash -S", self.main)

    def test_stash_dash_u_in_single_worktree_repo_is_allowed(self):
        self.assertAllowed("git stash -u", self.solo)


class TestRuleBCheckoutDashDashRequiresPath(WorktreeGuardTestCase):
    """I5: `--` with nothing after it is not a file restore — git switches
    branches and prints "Switched to branch '<name>'"."""

    def test_checkout_branch_dashdash_nothing_after_is_blocked(self):
        self.assertBlocked("git checkout main --", self.main)

    def test_checkout_dashdash_path_is_still_allowed(self):
        self.assertAllowed("git checkout -- f.txt", self.main)

    def test_checkout_branch_dashdash_path_is_still_allowed(self):
        self.assertAllowed("git checkout main -- f.txt", self.main)


class TestRuleAClosesAdditionalReachShapes(WorktreeGuardTestCase):
    """I6: three more shapes that genuinely reach another checkout."""

    def test_brace_group_cd_is_blocked(self):
        self.assertBlocked("{ cd %s && git commit -m x; }" % self.wt, self.main)

    def test_git_dir_and_work_tree_env_prefix_is_blocked(self):
        self.assertBlocked(
            "GIT_DIR=%s/.git GIT_WORK_TREE=%s git commit -m x" % (self.wt, self.wt),
            self.main)

    def test_git_dir_env_prefix_alone_is_blocked(self):
        self.assertBlocked("GIT_DIR=%s/.git git commit -m x" % self.wt, self.main)

    def test_env_dash_cap_c_is_blocked(self):
        self.assertBlocked("env -C %s git commit -m x" % self.wt, self.main)

    def test_env_dash_cap_c_own_checkout_is_allowed(self):
        self.assertAllowed("env -C %s git commit -am x" % self.main, self.main)

    def test_env_dash_cap_c_does_not_leak_into_next_statement(self):
        # env -C only changes the wrapped command's cwd, not the shell's own
        # — a command AFTER it must see the ORIGINAL directory context.
        self.assertAllowed(
            "env -C %s git status && git commit -am x" % self.wt, self.main)


class TestRuleAThroughEveryEnvSpelling(WorktreeGuardTestCase):
    """`env` was once resolved only as a literal `env` token
    followed by `-C`/NAME=value and a literal `git`. A git path, an env path,
    or any other env option before `-C` either hid the git call from judgment
    or judged it in the wrong directory. Options are those of macOS env(1)
    and GNU env. Pre-fix verdict noted per test."""

    # Pre-fix: ALLOW -- `/usr/bin/git` after env was never judged.
    def test_env_dash_cap_c_then_git_path_is_blocked(self):
        self.assertBlocked("env -C %s /usr/bin/git commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW -- same, with git's own -C.
    def test_env_then_git_path_with_git_dash_cap_c_is_blocked(self):
        self.assertBlocked("env /usr/bin/git -C %s commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW -- `/usr/bin/env` was not recognised as env.
    def test_env_path_dash_cap_c_is_blocked(self):
        self.assertBlocked("/usr/bin/env -C %s git commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW -- `-u NAME` stopped the scan before `-C`.
    def test_unset_before_dash_cap_c_is_blocked(self):
        self.assertBlocked("env -u FOO -C %s git commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW -- `-i` stopped the scan before `-C`.
    def test_ignore_environment_before_dash_cap_c_is_blocked(self):
        self.assertBlocked("env -i -C %s git commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW -- `-P` stopped the scan before `-C`.
    def test_altpath_before_dash_cap_c_is_blocked(self):
        self.assertBlocked("env -P /usr/bin -C %s git commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW -- a clustered `-iC` was not read as `-i -C`.
    def test_clustered_flags_ending_in_cap_c_is_blocked(self):
        self.assertBlocked("env -iC %s git commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW -- the attached `-C<dir>` form.
    def test_attached_dash_cap_c_is_blocked(self):
        self.assertBlocked("env -C%s git commit -am x" % self.wt, self.main)

    # GNU's long forms are judged only on Windows (Git Bash); see
    # TestEnvPrefixParser. macOS env rejects them ("illegal option"), so git
    # never runs there and a refusal would break fail-open.
    def test_gnu_chdir_on_macos_falls_through(self):
        self.assertAllowed("env --chdir=%s git commit -am x" % self.wt, self.main)

    # Both envs refuse to run a program with -0: git never runs.
    def test_dash_zero_falls_through(self):
        self.assertAllowed("env -0 -C %s git commit -am x" % self.wt, self.main)

    # Options end at the first NAME=value, so `-C` here is the program.
    def test_dash_cap_c_after_assignment_falls_through(self):
        self.assertAllowed("env FOO=1 -C %s git commit -am x" % self.wt, self.main)

    # env removes an inherited GIT_DIR before git starts.
    def test_unset_git_dir_drops_the_shell_prefix(self):
        self.assertAllowed(
            "GIT_DIR=%s/.git env -u GIT_DIR git commit -am x" % self.wt, self.main)

    def test_ignore_environment_drops_the_shell_prefix(self):
        self.assertAllowed(
            "GIT_DIR=%s/.git env -i git commit -am x" % self.wt, self.main)

    # Positive controls for the two above: the prefix survives env otherwise.
    def test_shell_git_dir_prefix_survives_plain_env(self):
        self.assertBlocked("GIT_DIR=%s/.git env git commit -am x" % self.wt, self.main)

    def test_shell_git_dir_prefix_survives_unset_of_another_var(self):
        self.assertBlocked(
            "GIT_DIR=%s/.git env -u FOO git commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW -- `--` ended the scan before `git`.
    def test_double_dash_before_git_is_blocked(self):
        self.assertBlocked("env -C %s -- git commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW -- NAME=value after env was skipped, not read.
    def test_env_git_dir_assignment_is_blocked(self):
        self.assertBlocked("env GIT_DIR=%s/.git git commit -m x" % self.wt, self.main)

    # Positive controls: the same spellings aimed at our own checkout, and
    # env options that change no directory, stay allowed.
    def test_env_dash_cap_c_git_path_own_checkout_is_allowed(self):
        self.assertAllowed("env -C %s /usr/bin/git commit -am x" % self.main, self.main)

    def test_env_path_unset_in_place_is_allowed(self):
        self.assertAllowed("/usr/bin/env -u FOO git commit -am x", self.main)

    def test_env_dash_cap_c_git_path_does_not_leak_into_next_statement(self):
        self.assertAllowed(
            "env -C %s /usr/bin/git status && git commit -am x" % self.wt, self.main)

    def test_env_running_something_else_is_allowed(self):
        self.assertAllowed("env -C %s ls" % self.wt, self.main)


class TestEnvPrefixParser(unittest.TestCase):
    """`_parse_env_prefix` per platform: macOS BSD env vs
    Git Bash's GNU env on Windows. Tokens carry no separators here."""

    def parse(self, words, windows):
        sys.path.insert(0, os.path.join(TOOLS_DIR, "worktree-guard"))
        import worktree_lib
        tokens = words.split()
        self.tokens = tokens
        return worktree_lib._parse_env_prefix(
            tokens, [False] * len(tokens), 0, windows)

    def test_gnu_chdir_forms_on_windows(self):
        for words in ("--chdir=/w git", "--chdir /w git"):
            parsed = self.parse(words, True)
            self.assertEqual(self.tokens[parsed[0]], "git", words)
            self.assertEqual(parsed[1], "/w", words)

    def test_gnu_unset_long_form_clears_on_windows(self):
        self.assertEqual(self.parse("--unset=GIT_DIR git", True)[3], {"GIT_DIR"})

    def test_long_options_are_not_understood_on_macos(self):
        self.assertIsNone(self.parse("--chdir=/w git", False))

    def test_dash_cap_p_is_bsd_only(self):
        self.assertIsNotNone(self.parse("-P /usr/bin git", False))
        self.assertIsNone(self.parse("-P /usr/bin git", True))

    def test_last_dash_cap_c_wins(self):
        self.assertEqual(self.parse("-C /a -C /b git", False)[1], "/b")


class TestPayloadIsDecodedAsUtf8(unittest.TestCase):
    """Claude Code writes the hook payload as UTF-8. Decoding stdin with the
    ambient encoding (cp1252 is Python's default on Windows) turns a
    non-ASCII path into a different string, git cannot resolve it, and the
    guard allows. Forced here with PYTHONIOENCODING, on any OS."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = os.path.join(self.tmp, "O\u02bbahu")
        os.makedirs(root)
        self.main, self.wt = make_repo_with_worktree(root)

    def _proc(self, encoding):
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": "git -C %s commit -am x" % self.wt},
                              "cwd": self.main}, ensure_ascii=False).encode("utf-8")
        env = dict(os.environ, PYTHONIOENCODING=encoding)
        for var in ("WORKTREE_ALLOW", "WT_ALLOW_MAIN"):
            env.pop(var, None)
        proc = subprocess.run([GUARD], input=payload, capture_output=True, env=env)
        _fail_if_crashed("utf-8 payload under %s" % encoding,
                         proc.stdout.decode("utf-8", "replace"))
        return proc

    def _run(self, encoding):
        return self._proc(encoding).returncode

    def test_utf8_control_blocks(self):
        # Positive control: with a UTF-8 stdin the fixture really is a
        # cross-checkout commit the guard refuses.
        self.assertEqual(self._run("utf-8"), 2)

    def test_a_cp1252_stdin_still_blocks(self):
        self.assertEqual(self._run("cp1252"), 2)

    def test_block_message_reaches_claude_code_as_utf8_under_cp1252(self):
        # Claude Code reads stderr as UTF-8: a strict decode must succeed
        # and carry the path, not escape text or a stray cp1252 byte.
        proc = self._proc("cp1252")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("O\u02bbahu", proc.stderr.decode("utf-8"))


class TestMalformedInput(WorktreeGuardTestCase):

    def test_empty_payload_allows(self):
        proc = subprocess.run([GUARD], input="", capture_output=True, text=True)
        _fail_if_crashed("<empty payload>", proc.stdout)
        self.assertEqual(proc.returncode, ALLOW)

    def test_garbage_payload_allows(self):
        proc = subprocess.run([GUARD], input="not json", capture_output=True,
                              text=True)
        _fail_if_crashed("not json", proc.stdout)
        self.assertEqual(proc.returncode, ALLOW)

    def test_missing_command_allows(self):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {}})
        proc = subprocess.run([GUARD], input=payload, capture_output=True,
                              text=True)
        _fail_if_crashed(payload, proc.stdout)
        self.assertEqual(proc.returncode, ALLOW)

    def test_non_bash_tool_is_ignored(self):
        payload = json.dumps({"tool_name": "Read",
                              "tool_input": {"command": "git checkout feature"},
                              "cwd": self.main})
        proc = subprocess.run([GUARD], input=payload, capture_output=True,
                              text=True)
        _fail_if_crashed(payload, proc.stdout)
        self.assertEqual(proc.returncode, ALLOW)

    def test_cwd_not_a_git_repo_allows(self):
        # Any error resolving cwd's own toplevel must fail open, never block —
        # a hook that blocked here would block every Bash call in a non-git
        # directory.
        outside = tempfile.mkdtemp()
        try:
            self.assertAllowed("git checkout feature", outside)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_dash_c_to_nonexistent_path_allows(self):
        self.assertAllowed(
            "git -C %s commit -am x" % os.path.join(self._dir, "does-not-exist"),
            self.main)

    def test_ordinary_non_git_commands_are_allowed(self):
        for command in ("ls -la", "npm install", "python3 -m pytest",
                        "echo 'digital transformation'"):
            self.assertAllowed(command, self.main)

    def test_deeply_nested_json_crashes_visibly_rather_than_propagating(self):
        # `json.loads` on a few thousand levels of `[`
        # exhausts Python's recursion limit INSIDE the decoder, before
        # `evaluate` ever runs. That RecursionError must be caught by the
        # entry point's crash boundary like any other crash -- not left to
        # propagate out of main() as a bare traceback (exit 1).
        #
        # The depth is version-dependent: 3.9 raises at 1200, but 3.13
        # parses 1200 and 5000 and first raises at 20,000 (measured
        # 2026-10-03). 100,000 raises on both. The control below fails
        # loudly on a future interpreter that parses this depth, rather
        # than letting the guard's silent exit 0 read as a crash defect.
        depth = 100000
        payload = "[" * depth + "0" + "]" * depth
        with self.assertRaises(RecursionError):
            json.loads(payload)
        proc = subprocess.run([GUARD], input=payload, capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, ALLOW)
        self.assertEqual(proc.stderr, "")
        parsed = json.loads(proc.stdout)
        self.assertTrue(parsed["systemMessage"].startswith(_CRASH_MARKER))

    def test_an_ordinary_non_dict_payload_still_exits_0_with_empty_stdout(self):
        # Positive control for the above: well-formed JSON whose top level
        # is not an object must still allow silently -- the crash boundary
        # added around json.loads must not start reporting THIS case as a
        # crash too.
        proc = subprocess.run([GUARD], input="[1, 2, 3]", capture_output=True,
                              text=True)
        self.assertEqual(proc.returncode, ALLOW)
        self.assertEqual(proc.stdout, "")


FORCE_EXPIRY = {"WORKTREE_TEST_BUDGET": "0", "WORKTREE_TEST_HARNESS": "1"}


class TestTimeoutBudget(WorktreeGuardTestCase):
    """The git budget and the `timeout` declared for this hook move together.

    Same shape as toolkit-integrity's and session-claim's, and the declared
    timeout is READ from plugin/hooks/hooks.json rather than restated. A
    restated copy passes against a stale number forever.

    Unlike a notice hook, going over here is not merely silent — a killed
    PreToolUse hook returns no verdict, so the command runs unjudged.
    """

    MARGIN = 1        # zsh start, three jq calls, the token walk, the write

    def _declared_timeout(self):
        repo = os.path.dirname(TOOLS_DIR)
        for row in hook_manifest.registrations(hook_manifest.load(repo)):
            if row["event"] == "PreToolUse" and row["exe"] == "claude-worktree-guard":
                self.assertEqual("tools/worktree-guard/claude-worktree-guard",
                                 row["relpath"])
                return row["timeout"]
        return None

    def _constants(self):
        """BUDGET_SECONDS and GIT_TIMEOUT_SECONDS, read out of
        worktree_lib.py as module-level assignments via `ast` -- never by
        text-matching a `NAME=value` line the way the zsh original's source
        was read, which has no Python equivalent (a module-level assignment
        here is `NAME = value`, and only `ast` tells a real one apart from,
        say, the same text inside a comment or docstring)."""
        with open(WORKTREE_LIB) as handle:
            source = handle.read()
        tree = ast.parse(source)
        values = {}
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)):
                continue
            name = node.targets[0].id
            if (name in ("BUDGET_SECONDS", "GIT_TIMEOUT_SECONDS")
                    and isinstance(node.value, ast.Constant)):
                values[name] = node.value.value
        return values

    def test_the_hook_is_registered_with_a_declared_timeout(self):
        """A guard nothing invokes is installed inert, and looks fine."""
        self.assertIsNotNone(
            self._declared_timeout(),
            "claude-worktree-guard missing from the plugin manifest PreToolUse")

    def test_both_constants_are_present(self):
        """A positive control for the parse below: if the names are renamed,
        this fails loudly instead of the budget test passing on an empty dict."""
        c = self._constants()
        self.assertIn("BUDGET_SECONDS", c)
        self.assertIn("GIT_TIMEOUT_SECONDS", c)

    def test_the_budget_fits_the_declared_timeout(self):
        """The bound is the budget; the extra cap in the sum below is SLACK.

        This said the opposite until 2026-09-01 -- that a call may begin with
        epsilon of budget left and run to its own full cap, so the overshoot
        is "structural, not slack." **That is false of this guard**, and
        `git_bounded` says so twenty lines from the code it describes:
        `(( left < cap )) && cap=$left` clamps every call to whatever remains,
        so the sum of all git time cannot exceed BUDGET_SECONDS. The comment
        above it records the same claim being retired once already, naming it
        "an earlier comment that claimed an 8s plus-a-full-cap worst case."
        This docstring was the surviving copy of exactly that.

        The arithmetic below is UNCHANGED and still errs safe -- keeping the
        `+ GIT_TIMEOUT_SECONDS` term makes the assertion strictly harder to
        satisfy than the real bound requires. Only the explanation was wrong,
        and it was wrong in the direction of describing a hazard the code does
        not have. Left as a deliberate margin rather than tightened, because
        what this test protects is the relationship between two numbers in two
        files, not the derivation.

        Found by porting this class into the sibling fork, where the same
        wrong claim arrived with it -- a ported comment is a claim about the
        ORIGIN repo, and here the origin was this one.
        """
        declared = self._declared_timeout()
        self.assertIsNotNone(declared)
        c = self._constants()
        worst = c["BUDGET_SECONDS"] + c["GIT_TIMEOUT_SECONDS"] + self.MARGIN
        self.assertLessEqual(
            worst, declared,
            "budget %ds + one %ds call + %ds margin = %ds exceeds the declared "
            "%ss timeout" % (c["BUDGET_SECONDS"], c["GIT_TIMEOUT_SECONDS"],
                             self.MARGIN, worst, declared))

    def test_every_git_call_goes_through_the_bounded_wrapper(self):
        """The canary, re-derived for Python. The zsh original enumerated
        shell CONTEXTS a bare `git` call might hide in (`if git ...`, inside
        `"$(...)"`, etc) because its source was shell text with no structure
        of its own. Python source has real structure, so the canary reads it
        instead: every `subprocess`/`os.exec*` surface in worktree_lib.py and
        the entry point must live inside `_run`, the one inner function
        `make_runners` builds that actually spawns (see
        `subprocess_offenders`'s own docstring) -- anything else bypasses the
        shared timeout budget exactly as a bare `git` call did in the zsh
        file.
        """
        with open(WORKTREE_LIB) as handle:
            lib_source = handle.read()
        with open(GUARD) as handle:
            entry_source = handle.read()
        offenders = (subprocess_offenders(lib_source, WORKTREE_LIB)
                    + subprocess_offenders(entry_source, GUARD))
        self.assertEqual(
            offenders, [],
            "these reach subprocess/os-exec outside _run:\n"
            + "\n".join(offenders))

    def test_the_canary_catches_a_bare_call_outside_the_runner(self):
        """Positive control on the PATTERN, not just the two files: a bare
        `subprocess.run(["git", ...])` placed in some OTHER function must be
        reported, or a canary that watches nothing would pass the assertion
        above for free."""
        fixture = (
            "import subprocess\n"
            "def make_runners():\n"
            "    def _run(argv):\n"
            "        return subprocess.run(argv)\n"
            "    return _run\n"
            "def other_helper():\n"
            "    return subprocess.run(['git', 'status'])\n"
        )
        offenders = subprocess_offenders(fixture, "<fixture>")
        self.assertEqual(len(offenders), 1,
                         "expected exactly one offender outside _run, "
                         "found: %r" % (offenders,))
        self.assertIn(":7: ", offenders[0])

    def test_the_canary_catches_a_bare_call_in_make_runners_own_body(self):
        """Tighter than the control above: `make_runners` itself is not a
        free pass just for sharing the name. A bare call directly in its
        body -- outside `_run`, the inner function that actually spawns --
        must be reported too, or the canary would accept exactly the shape
        AC3(i) mutates in."""
        fixture = (
            "import subprocess\n"
            "def make_runners():\n"
            "    subprocess.run(['git', 'status'])\n"
            "    def _run(argv):\n"
            "        return subprocess.run(argv)\n"
            "    return _run\n"
        )
        offenders = subprocess_offenders(fixture, "<fixture>")
        self.assertEqual(len(offenders), 1,
                         "expected exactly one offender in make_runners's "
                         "own body, found: %r" % (offenders,))
        self.assertIn(":3: ", offenders[0])


class TestAnExhaustedBudgetAllows(WorktreeGuardTestCase):
    """Budget exhaustion is "cannot tell", so it ALLOWS. A real stall refuses.

    This class asserted the opposite until it was seen what refusing on
    exhaustion actually costs: hundreds of ordinary `git commit` statements in
    one command, on a healthy local repo with nothing stalled, were REFUSED
    where the pre-change guard allowed them — and because the threshold is
    elapsed time rather than anything about the repo, it fell further under CPU
    load. A guard that refuses ordinary work nondeterministically
    is the guard people switch off.

    So the two causes are now separate. A call KILLED BY THE ALARM is direct
    evidence something is stalled and refuses (see TestAGenuinelySlowGitIsKilled
    — the only place the refusal path is exercised). A budget spent without any
    single call stalling is evidence only that the command was long.

    The cost of this direction is stated in
    `test_an_exhausted_budget_leaves_the_tail_of_a_command_unjudged` rather than
    left implicit, and BUDGET_SECONDS is sized so the case is out of reach of
    any real command.
    """

    # `git commit -am x` in its own checkout is the right probe for expiry: it
    # is ALLOWED on every ordinary path (Rule A needs a different target, Rule
    # B does not cover commit) and `commit` is in BLOCKING_VERBS, so an expiry
    # that lost the chance to judge it must refuse. `git status` was used here
    # until the narrowing below, and is now correctly allowed.
    PROBE = "git commit -am x"

    def test_an_exhausted_budget_allows_a_command_it_would_otherwise_allow(self):
        self.assertAllowed(self.PROBE, self.main)
        self.assertEqual(run_guard(self.PROBE, self.main, FORCE_EXPIRY), ALLOW)

    def test_an_exhausted_budget_leaves_the_tail_of_a_command_unjudged(self):
        """The accepted cost, asserted so it is a decision and not a surprise.

        Exhaustion means later calls return "cannot tell", so Rule A stops
        firing — a cross-checkout reach that would normally be refused is
        allowed. Before this change the guard judged every invocation and could
        instead hang forever; bounding anything at all buys this. What keeps it
        out of reach is the SIZE of the budget (~580 invocations), not the
        direction of the decision.
        """
        cross = "git -C %s commit -am x" % self.wt
        self.assertEqual(run_guard(cross, self.main), BLOCK)          # normally
        self.assertEqual(run_guard(cross, self.main, FORCE_EXPIRY), ALLOW)

    def test_a_sub_second_remainder_is_still_treated_as_no_budget(self):
        """The alarm(0) trap. `alarm` takes an integer and alarm(0) CANCELS the
        alarm rather than firing at once, so a remainder of 0.5s truncating to
        0 would run git with NO timeout at all. Under a second must mean stop —
        which now means return 125 and allow, not refuse."""
        env = {"WORKTREE_TEST_BUDGET": "0.5", "WORKTREE_TEST_HARNESS": "1"}
        self.assertEqual(run_guard(self.PROBE, self.main, env), ALLOW)
        # The positive control: the sub-second path must be REACHED, not merely
        # produce an allow for some unrelated reason. A cross-checkout reach is
        # normally refused, so its allow here proves resolution was skipped.
        self.assertEqual(
            run_guard("git -C %s commit -am x" % self.wt, self.main, env), ALLOW)

    def test_a_command_with_no_git_is_untouched_by_the_budget(self):
        """The fast path exits before any git call, so an expired budget must
        not turn ordinary work into a refusal. The cost there is negligible."""
        # NB: not "echo git-ish" — the fast-path grep is
        # `(^|[^A-Za-z])git([^A-Za-z]|$)`, so a hyphen counts as a boundary and
        # that string DOES reach the git path. Deliberately liberal; picking it
        # as a fast-path example was this test's own bug.
        for command in ("ls -la", "npm test && echo done", "echo hello"):
            self.assertEqual(run_guard(command, self.main, FORCE_EXPIRY), ALLOW,
                             "expected ALLOW despite expiry: %s" % command)

    def test_rule_a_blocks_for_its_own_reason_when_the_budget_is_intact(self):
        """Rule A's refusal must be Rule A's, and this test used to prove
        nothing about that.

        It ran under FORCE_EXPIRY and asserted only BLOCK. But site 1 clears
        `cwd_top` when the budget is gone, so Rule A CANNOT fire there — the
        refusal was the expiry path wearing Rule A's name, and the test
        duplicated its neighbour. Assert the message, with the budget intact.
        """
        err = guard_stderr("git -C %s commit -am x" % self.wt, self.main)
        self.assertIn("Rule A (cross-checkout reach)", err)


class TestExpiryRefusesOnlyWhatItMightHaveRefused(WorktreeGuardTestCase):
    """An expiry may not refuse a command the guard was never going to judge.

    The first cut refused on ANY expiry, at both of the first two call sites.
    Measured consequences, all of them false refusals on a stalled mount:

      - site 1 runs BEFORE tokenization, so it refused every command merely
        CONTAINING the word git — `man git`, `git --version`, and worst,
        `git worktree add <path> -b <branch>`, which is the remedy this guard's
        own refusal text recommends. It told you to run a command it was
        blocking, and suggested WORKTREE_ALLOW=1 as the way out.
      - site 2 refused verbs in the file's own DELIBERATELY EXEMPT list —
        `log`, `diff`, `config`, `gc` — whose verdict was already an
        unconditional allow, so refusing them buys no safety at all.

    Refusing what could have been refused keeps the property this guard chose
    (never silently under-judge); refusing everything else was a bug.
    """

    def test_a_non_git_command_is_allowed_under_expiry(self):
        for command in ("ls -la", "npm test", "echo hello"):
            self.assertEqual(run_guard(command, self.main, FORCE_EXPIRY), ALLOW,
                             "expected ALLOW under expiry: %s" % command)

    def test_a_command_merely_mentioning_git_is_allowed_under_expiry(self):
        """These reach the git path (the fast-path grep is deliberately
        liberal) but carry no git subcommand this guard judges."""
        for command in ("man git", "echo about to run git later",
                        "grep -r git ."):
            self.assertEqual(run_guard(command, self.main, FORCE_EXPIRY), ALLOW,
                             "expected ALLOW under expiry: %s" % command)

    def test_the_recommended_remedy_is_allowed_under_expiry(self):
        """The sharpest case. `git worktree add` is what the Rule B refusals
        tell you to run, and `worktree` is exempt from both rules — so an
        expiry must never be what stops you fixing the problem."""
        command = "git worktree add %s/new -b newbranch" % self._dir
        self.assertEqual(run_guard(command, self.main, FORCE_EXPIRY), ALLOW)

    def test_read_only_and_exempt_verbs_are_allowed_under_expiry(self):
        for verb in ("log -1", "diff", "status", "config user.name", "gc",
                     "branch -a", "show HEAD"):
            command = "git -C %s %s" % (self.wt, verb)
            self.assertEqual(
                run_guard(command, self.main, FORCE_EXPIRY), ALLOW,
                "expected ALLOW under expiry: %s" % command)

    def test_a_blocking_verb_is_still_refused_when_a_call_really_stalls(self):
        """The other half. Without it the tests above would pass for a guard
        that had simply stopped refusing anything.

        Exhaustion allows; a STALL refuses. The stall path needs a genuinely
        slow git rather than a zeroed budget, so it is exercised in full by
        TestAGenuinelySlowGitIsKilled — this asserts the pairing directly so
        the two halves cannot drift apart unnoticed.
        """
        for verb in ("commit -am x", "checkout main", "reset --hard", "stash",
                     "push", "revert HEAD", "clean -fd"):
            self.assertEqual(
                run_guard("git %s" % verb, self.main, FORCE_EXPIRY), ALLOW,
                "exhaustion must allow: git %s" % verb)
        self.assertTrue(
            any("stalled and had to be killed" in line
                for line in open(WORKTREE_LIB).read().splitlines()),
            "no refusal path names a stall, so nothing refuses any more")

    def test_every_blocking_verb_is_covered_by_one_list(self):
        """Rule A and the stall refusal consume the SAME array.

        Two copies of this filter is the shape git-tidy's `removable()` paid
        for — a mutation aimed at one silently grades the other. Re-derived
        for Python with `ast`: the zsh original read a `typeset -ga
        BLOCKING_VERBS=(...)` line and counted a literal consumer string;
        here BLOCKING_VERBS is a module-level tuple and its consumers are
        `Name` loads, which `ast` can count without caring how the
        comparison around each one is spelled.
        """
        with open(WORKTREE_LIB) as handle:
            source = handle.read()
        tree = ast.parse(source)

        verbs_node = None
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "BLOCKING_VERBS"):
                verbs_node = node.value
                break
        self.assertIsNotNone(verbs_node,
                             "BLOCKING_VERBS is not a module-level assignment")
        self.assertIsInstance(verbs_node, ast.Tuple,
                              "BLOCKING_VERBS must be one module-level tuple")
        verbs = [elt.value for elt in verbs_node.elts
                if isinstance(elt, ast.Constant)]

        loads = [n for n in ast.walk(tree)
                if isinstance(n, ast.Name) and n.id == "BLOCKING_VERBS"
                and isinstance(n.ctx, ast.Load)]
        self.assertEqual(
            len(loads), 2,
            "expected exactly two Name-load references to BLOCKING_VERBS "
            "(Rule A and the stall refusal); a literal verb list anywhere "
            "else is the duplicate this array exists to prevent -- found %d"
            % len(loads))

        # `revert` was dropped from the array on the first attempt at this
        # refactor and only the existing Rule A test caught it.
        for verb in ("revert", "sparse-checkout", "notes", "replace"):
            self.assertIn(verb, verbs, "%s missing from BLOCKING_VERBS" % verb)


class TestTheTestOverrideIsNotABackdoor(WorktreeGuardTestCase):
    """The budget override is reachable only in a sandbox, and only downward.

    Same compound-gate reasoning as session-claim's CLAIM_TEST_OWNER_PID: a
    single unconditional override inside a guard is a backdoor. Both halves are
    asserted, because either alone would look like it worked.
    """

    # `git commit -am x` rather than `git status`, and the reason is a trap
    # worth naming: `status` is not in BLOCKING_VERBS, so once expiry was
    # narrowed to refuse only verbs it might have blocked, a `status` probe was
    # allowed WHETHER OR NOT the gate held. The mutation weakening this gate
    # went from killed to surviving on that test alone. A probe has to be
    # something the property under test can actually change.
    PROBE = "git commit -am x"

    # A cross-checkout reach, because it is the only shape whose verdict the
    # override can still change: `git commit` in place is allowed with or
    # without a budget, so it could not detect a leaking gate.
    CROSS = None        # set in setUpClass-derived properties below

    def test_the_budget_variable_alone_does_nothing(self):
        cross = "git -C %s commit -am x" % self.wt
        self.assertEqual(
            run_guard(cross, self.main, {"WORKTREE_TEST_BUDGET": "0"}), BLOCK)

    def test_the_harness_variable_alone_does_nothing(self):
        cross = "git -C %s commit -am x" % self.wt
        self.assertEqual(
            run_guard(cross, self.main, {"WORKTREE_TEST_HARNESS": "1"}), BLOCK)

    def test_both_together_do_change_the_verdict(self):
        """The positive control the two tests above need: without it they pass
        for a gate that ignores the override entirely.

        The observable effect is no longer a refusal (exhaustion allows), so the
        probe is a cross-checkout reach — refused normally, allowed once the
        budget is gone because the target can no longer be resolved.
        """
        cross = "git -C %s commit -am x" % self.wt
        self.assertEqual(run_guard(cross, self.main), BLOCK)
        self.assertEqual(
            run_guard(cross, self.main,
                      {"WORKTREE_TEST_BUDGET": "0",
                       "WORKTREE_TEST_HARNESS": "1"}), ALLOW)

    def test_a_generous_budget_does_not_stop_the_guard_judging(self):
        """A long budget must not go quiet — it should behave like no override.

        This class used to assert that the override could only SHORTEN the
        budget. That clamp was removed: a mutation deleting it survived the
        whole suite, because lengthening has no observable effect on any
        verdict. Asserting the reachable property instead of the unreachable
        one.
        """
        env = {"WORKTREE_TEST_BUDGET": "999", "WORKTREE_TEST_HARNESS": "1"}
        self.assertEqual(run_guard("git commit -am x", self.main, env), ALLOW)
        self.assertEqual(
            run_guard("git -C %s commit -am x" % self.wt, self.main, env), BLOCK)


class TestAGenuinelySlowGitIsKilled(WorktreeGuardTestCase):
    """A real slow git, killed by the real timeout, at the real call sites.

    Every expiry test in TestAnExhaustedBudgetAllows/TestExpiryRefusesOnlyWhat...
    forces the budget to 0, which means the FIRST git call (resolving cwd's own
    checkout) always trips first. The later call sites are then unreachable,
    and mutations deleting their checks survived the whole suite — redundant
    outcomes hiding each other.

    So this class makes ONE call slow instead of making the budget small, which
    is also the only thing here that proves `subprocess.run(timeout=...)`
    actually fires and that `TimeoutExpired` is recognised as a stall rather
    than as an ordinary git failure. The shim itself is unchanged from the zsh
    era (still perl, still an in-process sleep before exec'ing the real git --
    see the comments below for why); what changed is how it reaches the
    guard: `guard_copy_with_git_path` points the Python guard's
    `GIT_PATH_POSIX` at a `bindir` holding nothing but this shim, since the
    Python guard shells out to nothing else -- no jq, no grep, no dirname.

    The slowness is keyed on the ARGUMENTS, so which site expires is
    deterministic rather than a race:
      - slow on `worktree list`  -> Rule B's count expires
      - slow on a `-C <target>`  -> the target's toplevel resolution expires
    """

    SLOW = 6        # comfortably over GIT_TIMEOUT_SECONDS

    def _guard_with_slow_git(self, slow_when):
        """A guard copy whose PATH holds a git that sleeps for matching args.

        `slow_when` is a PERL expression over @ARGV. Returns (guard, bindir).
        """
        bindir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bindir, ignore_errors=True)

        real_git = shutil.which("git")
        shim = os.path.join(bindir, "git")
        # The delay MUST happen in the shim's own process, not in a child.
        #
        # The first version slept via `sleep N`, and the test caught a real
        # property of the mechanism: a killed process does not take its
        # children down with it, and a pipe-reading parent (`subprocess.run`
        # here, `$(...)` in the zsh original) waits on the PIPE rather than on
        # the process it spawned directly — so an orphaned child holding
        # stdout keeps the read blocked and the guard waited the full 6s
        # regardless of the declared timeout.
        #
        # That shape is not what this guard faces: `rev-parse --show-toplevel`
        # and `worktree list --porcelain` are local metadata reads that spawn
        # no children, and a stalled filesystem blocks git itself, which
        # `subprocess.run(timeout=...)` does kill (it terminates the process
        # it spawned directly). So the shim models the real case by sleeping
        # in-process (perl's 4-arg select) and then exec'ing git -- the sleep
        # and the eventual git both run as the ONE process subprocess.run is
        # watching, with no orphaned child to hide behind.
        #
        # The grandchild limitation is real but out of scope here, and it is
        # named in worktree_lib.py's own comments rather than hidden: a git
        # subcommand that forks a helper (ssh, a credential helper) could
        # outlive the timeout. None of this guard's calls do.
        with open(shim, "w") as handle:
            handle.write(
                "#!/usr/bin/perl\n"
                "if (%s) { select(undef, undef, undef, %d); }\n"
                "exec('%s', @ARGV);\n" % (slow_when, self.SLOW, real_git))
        os.chmod(shim, 0o755)

        guard = guard_copy_with_git_path(bindir)
        return guard, bindir

    def _run(self, guard, command, cwd):
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command}, "cwd": cwd})
        environ = dict(os.environ)
        environ.pop("WORKTREE_ALLOW", None)
        environ.pop("WT_ALLOW_MAIN", None)
        proc = subprocess.run([guard], input=payload, capture_output=True,
                              text=True, env=environ)
        _fail_if_crashed(command, proc.stdout)
        return proc

    def test_the_shim_is_actually_slow(self):
        """Positive control. If the shim never sleeps, every test below passes
        for free and proves the opposite of its name."""
        guard, bindir = self._guard_with_slow_git('$ARGV[1] eq "SENTINEL"')
        shim = os.path.join(bindir, "git")
        start = time.time()
        subprocess.run([shim, "-C", "SENTINEL", "rev-parse"],
                       capture_output=True)
        elapsed = time.time() - start
        self.assertGreater(elapsed, self.SLOW - 1,
                           "the slow-git shim did not sleep (%.2fs)" % elapsed)

    def test_a_slow_worktree_list_expires_rule_bs_own_call(self):
        """`git stash` in its own checkout: cwd and target resolve fast, then
        Rule B's `worktree list` hangs and must be killed and refused."""
        guard, _ = self._guard_with_slow_git('$ARGV[2] eq "worktree"')
        done = self._run(guard, "git stash", self.main)
        self.assertEqual(done.returncode, BLOCK,
                         "a hung `worktree list` was not refused: %r"
                         % done.stderr)
        # "worktrees" appears in BOTH the ordinary Rule B refusal and this one,
        # and the fixture repo has 2 worktrees — so asserting that word passed
        # with the timeout mechanism REMOVED and with the shim never sleeping
        # at all. Measured three ways, all rc=2 with "worktrees" in stderr.
        # Assert the sentence only the stall path can write.
        self.assertIn("stalled and had to be killed", done.stderr)
        self.assertNotIn("multiple worktrees registered", done.stderr)

    def test_a_slow_target_resolution_expires_the_second_call(self):
        """`git commit` in its own checkout, with resolution of that checkout
        stalled: the first call expires (which now only clears cwd_top, so Rule
        A goes inactive rather than refusing), and the SECOND expiry is what
        must refuse — because `commit` is a verb the guard might have needed to
        block. So the refusal cannot be Rule A in disguise.
        """
        guard, _ = self._guard_with_slow_git('$ARGV[1] eq "%s"' % self.main)
        done = self._run(guard, "git commit -am x", self.main)
        self.assertEqual(done.returncode, BLOCK,
                         "a hung target resolution was not refused: %r"
                         % done.stderr)
        self.assertIn("stalled and had to be killed", done.stderr)

    def test_the_kill_happens_inside_the_declared_timeout(self):
        """The whole point: bounded, not merely eventually finished.

        Without the timeout this invocation takes SLOW seconds per call and the
        harness kills the hook at its declared 10s with no verdict at all.
        """
        guard, _ = self._guard_with_slow_git('$ARGV[2] eq "worktree"')
        start = time.time()
        self._run(guard, "git stash", self.main)
        elapsed = time.time() - start
        self.assertLess(elapsed, self.SLOW,
                        "the guard waited %.1fs for a git it should have "
                        "killed at its 2s cap" % elapsed)

    def test_an_ordinary_command_is_untouched_by_the_shim(self):
        """The shim must only slow what it is aimed at, or the tests above are
        measuring the wrong thing."""
        guard, _ = self._guard_with_slow_git('$ARGV[2] eq "worktree"')
        # `log` resolves a toplevel and never counts worktrees, so the shim
        # never sleeps for it and this must be fast AND allowed.
        start = time.time()
        rc = self._run(guard, "git -C %s log -1" % self.wt, self.main).returncode
        self.assertEqual(rc, ALLOW)
        self.assertLess(time.time() - start, self.SLOW,
                        "the shim slowed a call it was not aimed at")


class TestNoGitOnPathAllowsWithoutCrashing(WorktreeGuardTestCase):
    """With no `git` reachable at all, the guard still exits cleanly.

    Retired from the zsh original's "missing perl, git runs unbounded" class
    -- the branch it tested (zsh's `TIMEOUT_AVAILABLE` fallback to a bare,
    unbounded `git "$@"`) does not exist here: Python always has
    `subprocess.run(timeout=...)`, so there is no "no timeout available" path
    left to lose. What carries over is the same shape of risk -- a missing
    external dependency must degrade to a clean, unjudged ALLOW, never a
    crash or a silent always-allow that happens to look like one. `git` is
    the only external dependency the Python guard still has (`cygpath` on
    Windows is the other), so this exercises losing IT: with no git, the
    commands the guard would judge cannot run either, so `git status` and a
    cross-checkout `git commit` both have to resolve to nothing and exit 0
    with EMPTY stdout -- no crash marker.
    """

    def _run(self, guard, command, cwd):
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command}, "cwd": cwd})
        environ = dict(os.environ)
        environ.pop("WORKTREE_ALLOW", None)
        environ.pop("WT_ALLOW_MAIN", None)
        return subprocess.run([guard], input=payload, capture_output=True,
                              text=True, env=environ)

    def test_ordinary_work_is_still_allowed_without_git(self):
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        guard = guard_copy_with_git_path(empty)
        proc = self._run(guard, "git status", self.main)
        self.assertEqual(proc.returncode, ALLOW)
        self.assertEqual(proc.stdout, "")

    def test_rule_a_also_allows_without_git(self):
        """The sharpest case: a command that WOULD be refused if git worked
        resolves to nothing instead, because neither side can be told apart
        from the other without git to ask."""
        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, ignore_errors=True)
        guard = guard_copy_with_git_path(empty)
        proc = self._run(guard, "git -C %s commit -am x" % self.wt, self.main)
        self.assertEqual(proc.returncode, ALLOW)
        self.assertEqual(proc.stdout, "")

    def test_a_real_git_on_path_still_blocks_the_cross_checkout_commit(self):
        """Positive control: without it, the two ALLOWs above could just as
        well mean the guard crashed silently, or always allows regardless of
        git -- this proves the SAME copy mechanism, with a real git on
        `bindir`, still judges and still blocks."""
        bindir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bindir, ignore_errors=True)
        os.symlink(shutil.which("git"), os.path.join(bindir, "git"))
        guard = guard_copy_with_git_path(bindir)
        proc = self._run(guard, "git -C %s commit -am x" % self.wt, self.main)
        self.assertEqual(proc.returncode, BLOCK)


class TestConfigIsDecodedAsUtf8(unittest.TestCase):
    """load_config once opened the config with the
    ambient locale's encoding. Under a non-UTF-8 locale (cp1252 on Windows,
    ASCII under LC_ALL=C) any non-ASCII text in the file decoded wrong or
    raised, the loader treated the config as absent, and Rule C switched OFF
    silently.

    The paths stay ASCII and the non-ASCII text sits in another key: on Linux
    with UTF-8 mode and locale coercion both off, the FILESYSTEM encoding is
    ASCII too, so a non-ASCII directory name crashes the guard before the
    config matters (CI, 2026-10-03). macOS and Windows always use UTF-8 for
    paths, so that crash is this fixture's, not a user's.
    """

    NON_UTF8 = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0",
                "PYTHONCOERCECLOCALE": "0"}

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp()
        cls.canonical_root = os.path.join(cls._dir, "canon")
        cls.proj = os.path.join(cls.canonical_root, "proj")
        os.makedirs(cls.proj)
        git("init", "-q", "-b", "main", cwd=cls.proj)
        git("config", "user.email", "t@example.com", cwd=cls.proj)
        git("config", "user.name", "Test", cwd=cls.proj)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=cls.proj)
        cls.config_path = os.path.join(cls._dir, "config.json")
        with open(cls.config_path, "w", encoding="utf-8") as handle:
            json.dump({"worktree_guard": {"canonical_root": cls.canonical_root},
                       "note": "Oʻahu"},
                      handle, ensure_ascii=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def test_commit_on_default_branch_blocks_under_a_non_utf8_locale(self):
        # Positive control: the locale override must actually take the
        # guard's interpreter off UTF-8, or this test cannot fail.
        probe = subprocess.run(
            ["python3", "-c",
             "import locale; print(locale.getpreferredencoding(False))"],
            capture_output=True, text=True,
            env=dict(os.environ, **self.NON_UTF8))
        if probe.stdout.strip().lower().replace("-", "") in ("utf8",):
            self.skipTest("this interpreter defaults to UTF-8 under LC_ALL=C")
        self.assertEqual(
            run_guard("git commit -am x", self.proj, env=self.NON_UTF8,
                      config_path=self.config_path),
            BLOCK)

    def test_same_config_blocks_under_utf8(self):
        # Control for the fixture itself: Rule C must fire on this config
        # when the locale is not in play.
        self.assertEqual(
            run_guard("git commit -am x", self.proj,
                      env={"PYTHONUTF8": "1"}, config_path=self.config_path),
            BLOCK)


class TestRuleCCanonicalCheckoutOnDefaultBranch(unittest.TestCase):
    """Edit/Write/commit/rebase/non-ff-merge/
    history-rewriting-push landing directly on a canonical checkout's own
    default branch. Unlike Rule B this needs NO worktree registered at all —
    only worktree_guard.canonical_root configured; it is never derived
    from workspace_root."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp()
        cls.canonical_root = os.path.join(cls._dir, "canon")
        os.makedirs(cls.canonical_root)

        # A canonical checkout with NO worktree registered at all -- proves
        # Rule C does not need worktree_count >= 2 the way Rule B does.
        cls.proj = os.path.join(cls.canonical_root, "proj")
        os.makedirs(cls.proj)
        git("init", "-q", "-b", "main", cwd=cls.proj)
        git("config", "user.email", "t@example.com", cwd=cls.proj)
        git("config", "user.name", "Test", cwd=cls.proj)
        with open(os.path.join(cls.proj, "f.txt"), "w") as handle:
            handle.write("hi\n")
        git("add", "-A", cwd=cls.proj)
        git("commit", "-q", "-m", "init", cwd=cls.proj)
        git("branch", "feature", cwd=cls.proj)

        # A checkout that is NOT under canonical_root -- Rule C must be
        # invisible here regardless of branch.
        cls.elsewhere = os.path.join(cls._dir, "elsewhere", "proj2")
        os.makedirs(cls.elsewhere)
        git("init", "-q", "-b", "main", cwd=cls.elsewhere)
        git("config", "user.email", "t@example.com", cwd=cls.elsewhere)
        git("config", "user.name", "Test", cwd=cls.elsewhere)
        with open(os.path.join(cls.elsewhere, "f.txt"), "w") as handle:
            handle.write("hi\n")
        git("add", "-A", cwd=cls.elsewhere)
        git("commit", "-q", "-m", "init", cwd=cls.elsewhere)

        cls.config_path = os.path.join(cls._dir, "config.json")
        with open(cls.config_path, "w") as handle:
            json.dump({"worktree_guard": {"canonical_root": cls.canonical_root}},
                      handle)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def assertAllowed(self, tool, tool_input, cwd, env=None, config_path=None):
        self.assertEqual(
            run_guard_full(tool, tool_input, cwd, env=env,
                           config_path=config_path or self.config_path),
            ALLOW, "expected ALLOW: %s %s" % (tool, tool_input))

    def assertBlocked(self, tool, tool_input, cwd, env=None, config_path=None):
        self.assertEqual(
            run_guard_full(tool, tool_input, cwd, env=env,
                           config_path=config_path or self.config_path),
            BLOCK, "expected BLOCK: %s %s" % (tool, tool_input))

    def assertBlockedWithMessage(self, tool, tool_input, cwd, needle,
                                 not_needle=None, config_path=None):
        """Asserts BOTH the exit code AND that stderr carries `needle` (and,
        if given, does NOT carry `not_needle`) -- the whole point of the
        delete/history-rewrite split is that they share an exit code but
        must never share a message."""
        proc = run_guard_capture(tool, tool_input, cwd,
                                 config_path=config_path or self.config_path)
        self.assertEqual(proc.returncode, BLOCK,
                         "expected BLOCK: %s %s" % (tool, tool_input))
        self.assertIn(needle, proc.stderr,
                     "expected message containing %r, got: %s"
                     % (needle, proc.stderr))
        if not_needle is not None:
            self.assertNotIn(not_needle, proc.stderr,
                            "message should NOT contain %r, got: %s"
                            % (not_needle, proc.stderr))

    # --- Edit/Write/MultiEdit/NotebookEdit -----------------------------------

    def test_edit_on_default_branch_is_blocked(self):
        self.assertBlocked("Edit", {"file_path": os.path.join(self.proj, "f.txt")},
                           self.proj)

    def test_write_on_default_branch_is_blocked(self):
        self.assertBlocked("Write", {"file_path": os.path.join(self.proj, "g.txt")},
                           self.proj)

    def test_multiedit_on_default_branch_is_blocked(self):
        self.assertBlocked("MultiEdit", {"file_path": os.path.join(self.proj, "f.txt")},
                           self.proj)

    def test_notebookedit_on_default_branch_is_blocked(self):
        self.assertBlocked("NotebookEdit",
                           {"notebook_path": os.path.join(self.proj, "n.ipynb")},
                           self.proj)

    def test_edit_via_nested_path_is_blocked(self):
        # Toplevel resolution, not a literal-path match: a file several
        # directories deep still resolves to the canonical checkout.
        nested = os.path.join(self.proj, "src", "deep")
        os.makedirs(nested, exist_ok=True)
        self.assertBlocked("Edit", {"file_path": os.path.join(nested, "x.py")},
                           self.proj)

    def test_edit_on_feature_branch_is_allowed(self):
        git("checkout", "-q", "feature", cwd=self.proj)
        try:
            self.assertAllowed("Edit", {"file_path": os.path.join(self.proj, "f.txt")},
                               self.proj)
        finally:
            git("checkout", "-q", "main", cwd=self.proj)

    def test_edit_outside_canonical_root_is_allowed(self):
        self.assertAllowed("Edit", {"file_path": os.path.join(self.elsewhere, "f.txt")},
                           self.elsewhere)

    def test_edit_with_no_config_is_allowed(self):
        # No canonical_root known -> Rule C stays inactive, don't guess.
        self.assertAllowed("Edit", {"file_path": os.path.join(self.proj, "f.txt")},
                           self.proj, config_path="/nonexistent.json")

    def test_read_tool_is_ignored(self):
        self.assertAllowed("Read", {"file_path": os.path.join(self.proj, "f.txt")},
                           self.proj)

    # --- git commit / rebase / merge / push, independent of worktree count --

    def test_commit_on_default_branch_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git commit -am x"}, self.proj)

    def test_rebase_on_default_branch_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git rebase feature"}, self.proj)

    def test_non_ff_merge_on_default_branch_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git merge feature"}, self.proj)

    def test_ff_only_merge_is_allowed(self):
        # Carve-out 1: can only advance a pointer or refuse.
        self.assertAllowed("Bash", {"command": "git merge --ff-only feature"},
                           self.proj)

    def test_non_force_push_is_allowed(self):
        # Carve-out 2: publishes what already exists locally; never writes
        # the working tree or index.
        self.assertAllowed("Bash", {"command": "git push"}, self.proj)

    def test_force_push_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git push --force"}, self.proj)

    def test_force_with_lease_push_is_blocked(self):
        # This rule's push carve-out is narrower than danger-guard's: even
        # --force-with-lease is not "safe" directly on a canonical default
        # branch, because the point is never landing work there at all.
        self.assertBlocked("Bash", {"command": "git push --force-with-lease"},
                           self.proj)

    def test_delete_push_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git push origin --delete x"},
                           self.proj)

    def test_plus_refspec_push_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git push origin +main:main"},
                           self.proj)

    # --- characterization baseline for the empty-source-refspec work:
    # pins today's Rule C push
    # verdicts so a change that teaches this guard the colon-refspec delete
    # form does not silently touch anything else. Note Rule C's push
    # carve-out is narrower than danger-guard's by design --
    # --force-with-lease is BLOCKED here (test_force_with_lease_push_is_blocked
    # above), not allowed, so it is not repeated in the allowed cases below.
    def test_short_force_push_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git push -f"}, self.proj)

    def test_bare_plus_refspec_push_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git push origin +main"},
                           self.proj)

    def test_mirror_push_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git push --mirror origin"},
                           self.proj)

    def test_short_delete_flag_push_is_blocked(self):
        self.assertBlocked("Bash", {"command": "git push origin -d some-branch"},
                           self.proj)

    # --- the empty-source
    # refspec is recognized as a delete, and the delete verdict is split
    # from the history-rewriting one. The two share an exit code by design,
    # so every case here asserts on the MESSAGE -- distinguishing them is
    # the entire point of the change.
    DELETE_MSG = "removes a branch from the remote"
    HISTORY_MSG = "history-rewriting push would run against"

    def test_colon_main_refspec_is_blocked_with_delete_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin :main"}, self.proj,
            self.DELETE_MSG, not_needle=self.HISTORY_MSG)

    def test_colon_refs_heads_main_refspec_is_blocked_with_delete_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin :refs/heads/main"}, self.proj,
            self.DELETE_MSG, not_needle=self.HISTORY_MSG)

    def test_colon_refs_tags_refspec_is_blocked_with_delete_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin :refs/tags/v1"}, self.proj,
            self.DELETE_MSG, not_needle=self.HISTORY_MSG)

    def test_plus_colon_main_refspec_is_blocked_with_delete_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin +:main"}, self.proj,
            self.DELETE_MSG, not_needle=self.HISTORY_MSG)

    # --- the case that once shipped the
    # over-block. `:*`/`+:*` matched the BARE tokens `:`/`+:` too, because a
    # trailing `*` in a zsh glob matches empty -- and every existing colon
    # test above has a non-empty destination, so none of them caught it.
    def test_bare_colon_matching_push_is_allowed(self):
        # Git's documented MATCHING PUSH: non-force, deletes nothing.
        # Verified against a real bare repo: `git push origin :` returns
        # "Everything up-to-date", exit 0, every remote ref intact.
        self.assertAllowed("Bash", {"command": "git push origin :"}, self.proj)

    def test_bare_plus_colon_is_blocked_with_history_message_not_delete(self):
        # The FORCED form of that same matching push -- still no
        # destination, so still nothing to delete, but forced. Verified
        # against a real bare repo with a divergent remote: `git push origin
        # +:` force-overwrites it, the identical effect of
        # `git push origin +main`. It must get the history-rewrite verdict,
        # never the delete one -- the two share an exit code by design, so
        # only the message tells them apart.
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin +:"}, self.proj,
            self.HISTORY_MSG, not_needle=self.DELETE_MSG)

    def test_delete_among_several_refspecs_is_blocked_with_delete_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin main :old"}, self.proj,
            self.DELETE_MSG, not_needle=self.HISTORY_MSG)

    def test_long_delete_flag_is_blocked_with_delete_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin --delete some-branch"}, self.proj,
            self.DELETE_MSG, not_needle=self.HISTORY_MSG)

    def test_short_delete_flag_is_blocked_with_delete_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin -d some-branch"}, self.proj,
            self.DELETE_MSG, not_needle=self.HISTORY_MSG)

    def test_force_push_is_blocked_with_history_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push --force origin main"}, self.proj,
            self.HISTORY_MSG, not_needle=self.DELETE_MSG)

    def test_short_force_push_is_blocked_with_history_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push -f origin main"}, self.proj,
            self.HISTORY_MSG, not_needle=self.DELETE_MSG)

    def test_plus_refspec_push_is_blocked_with_history_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin +main"}, self.proj,
            self.HISTORY_MSG, not_needle=self.DELETE_MSG)

    def test_plus_main_colon_main_refspec_is_blocked_with_history_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push origin +main:main"}, self.proj,
            self.HISTORY_MSG, not_needle=self.DELETE_MSG)

    def test_mirror_push_is_blocked_with_history_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push --mirror origin"}, self.proj,
            self.HISTORY_MSG, not_needle=self.DELETE_MSG)

    def test_prune_push_is_blocked_with_history_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push --prune origin"}, self.proj,
            self.HISTORY_MSG, not_needle=self.DELETE_MSG)

    def test_force_with_lease_push_is_blocked_with_history_message(self):
        self.assertBlockedWithMessage(
            "Bash", {"command": "git push --force-with-lease origin main"},
            self.proj, self.HISTORY_MSG, not_needle=self.DELETE_MSG)

    def test_push_with_explicit_branch_is_allowed(self):
        self.assertAllowed("Bash", {"command": "git push origin main"},
                           self.proj)

    def test_push_local_colon_remote_refspec_is_allowed(self):
        self.assertAllowed("Bash", {"command": "git push origin local:remote"},
                           self.proj)

    def test_push_head_to_refs_heads_refspec_is_allowed(self):
        self.assertAllowed(
            "Bash", {"command": "git push origin HEAD:refs/heads/main"},
            self.proj)

    def test_push_to_ssh_remote_url_is_allowed(self):
        self.assertAllowed(
            "Bash",
            {"command": "git push git@github.com:user/repo.git main"},
            self.proj)

    def test_commit_on_feature_branch_is_allowed(self):
        git("checkout", "-q", "feature", cwd=self.proj)
        try:
            self.assertAllowed("Bash", {"command": "git commit -am x"}, self.proj)
        finally:
            git("checkout", "-q", "main", cwd=self.proj)

    def test_commit_outside_canonical_root_is_allowed(self):
        self.assertAllowed("Bash", {"command": "git commit -am x"}, self.elsewhere)

    def test_commit_with_no_config_is_allowed(self):
        self.assertAllowed("Bash", {"command": "git commit -am x"}, self.proj,
                           config_path="/nonexistent.json")

    def test_status_on_default_branch_is_allowed(self):
        # Rule C's verb scope is narrow -- read-only and non-landing verbs
        # stay untouched, same as the live copy.
        self.assertAllowed("Bash", {"command": "git status"}, self.proj)

    def test_workspace_root_alone_does_NOT_activate_rule_c(self):
        # THIS TEST WAS INVERTED ON 2026-09-01, deliberately. It used to assert
        # that workspace_root alone derives canonical_root as
        # <workspace_root>/repo, which an older copy of this guard did.
        # That fallback is gone, for two reasons recorded at the code and
        # in config.example.json: the singular spelling is the one
        # sync_lib.py already refuses to guess (it resolves to a directory that
        # is never there, giving a silent no-op), and deriving a REFUSAL from
        # an unrelated key turns protection on for someone who never asked.
        #
        # Every layout is exercised, because "no fallback" has to hold for the
        # spellings that would have worked as well as the one that would not --
        # asserting only the singular case would pass just as happily if the
        # fallback had merely been re-spelled to `repos`, or to the workspace
        # root itself (the flat layout an older copy fell back to when
        # `repos/` was absent). The flat case runs FIRST, while no `repos/`
        # exists, so a fallback that prefers `repos/` would take the flat arm.
        cfg_path = os.path.join(self._dir, "ws-only-config.json")
        with open(cfg_path, "w") as handle:
            json.dump({"workspace_root": self._dir}, handle)

        for spelling in ("", "repo", "repos"):
            root = os.path.join(self._dir, spelling) if spelling else self._dir
            proj2 = os.path.join(root, "proj2" if spelling else "proj-flat")
            os.makedirs(proj2, exist_ok=True)
            git("init", "-q", "-b", "main", cwd=proj2)
            git("config", "user.email", "t@example.com", cwd=proj2)
            git("config", "user.name", "Test", cwd=proj2)
            with open(os.path.join(proj2, "f.txt"), "w") as handle:
                handle.write("hi\n")
            git("add", "-A", cwd=proj2)
            git("commit", "-q", "-m", "init", cwd=proj2)
            self.assertAllowed("Bash", {"command": "git commit -am x"}, proj2,
                               config_path=cfg_path)

    def test_an_explicit_canonical_root_still_activates_it(self):
        # The control for the test above: without this, "Rule C never fires"
        # would satisfy it just as well as "Rule C needs an explicit key", and
        # a rule that had been accidentally disabled outright would pass.
        self.assertBlocked("Bash", {"command": "git commit -am x"}, self.proj,
                           config_path=self.config_path)

    # --- a linked worktree is never canonical ---------------------------------

    @staticmethod
    def _branch_of(cwd):
        """The checked-out branch, for asserting a fixture is what it claims:
        a fixture silently on the wrong branch passes an ALLOW assertion for
        free, because Rule C declines before reaching the condition under
        test."""
        return subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True).stdout.strip()

    def _flat_root_with_sibling_worktree(self):
        """A flat canonical root holding a primary checkout on a feature
        branch, a linked worktree BESIDE it holding `main`, and an ordinary
        primary checkout on `main` as the positive control. Fresh per test:
        the class fixture holds one repo, and a flat root with several is the
        shape under test."""
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        root = os.path.realpath(root)
        cfg_path = os.path.join(root, "config.json")
        with open(cfg_path, "w") as handle:
            json.dump({"worktree_guard": {"canonical_root": root}}, handle)

        def checkout(name):
            path = os.path.join(root, name)
            os.makedirs(path)
            git("init", "-q", "-b", "main", cwd=path)
            git("config", "user.email", "t@example.com", cwd=path)
            git("config", "user.name", "Test", cwd=path)
            with open(os.path.join(path, "f.txt"), "w") as handle:
                handle.write("hi\n")
            git("add", "-A", cwd=path)
            git("commit", "-q", "-m", "init", cwd=path)
            return path

        primary = checkout("proj2")
        # git refuses to check out a branch another worktree holds, so the
        # primary moves off `main` first, which is exactly how the real case
        # arises: the primary on a feature branch frees `main` for a sibling.
        git("checkout", "-q", "-b", "feature", cwd=primary)
        sibling = os.path.join(root, "proj2-main")
        git("worktree", "add", "-q", sibling, "main", cwd=primary)
        control = checkout("proj3")
        for path, branch in ((sibling, "main"), (control, "main"),
                             (primary, "feature")):
            self.assertEqual(
                self._branch_of(path), branch,
                "fixture is inert unless %s is on %s" % (path, branch))
        return cfg_path, sibling, control

    def test_a_sibling_linked_worktree_is_not_canonical(self):
        """With a flat layout, a linked worktree created beside its primary
        checkout is an immediate child of the canonical root too. Rule C must
        not refuse work in it: that would tell someone already standing in a
        worktree to "do the work in a worktree", while the primary checkout
        beside it, on a feature branch, allows everything.

        Positive control in the SAME root, so this cannot pass by Rule C
        being switched off wholesale: an ordinary primary checkout on its own
        default branch is still refused."""
        cfg_path, sibling, control = self._flat_root_with_sibling_worktree()
        self.assertAllowed("Bash", {"command": "git commit -am x"}, sibling,
                           config_path=cfg_path)
        self.assertBlocked("Bash", {"command": "git commit -am x"}, control,
                           config_path=cfg_path)

    def test_an_edit_in_a_sibling_linked_worktree_is_not_refused(self):
        """The Edit family reaches Rule C through the same canonical-checkout
        test, so the exclusion must hold there too, with the same control."""
        cfg_path, sibling, control = self._flat_root_with_sibling_worktree()
        self.assertAllowed("Edit", {"file_path": os.path.join(sibling, "f.txt")},
                           sibling, config_path=cfg_path)
        self.assertBlocked("Edit", {"file_path": os.path.join(control, "f.txt")},
                           control, config_path=cfg_path)


class TestRuleCFindsTheRealDestination(unittest.TestCase):
    """R1-04: Rule C judges the checkout a write really lands in -- through
    a directory that does not exist yet and through a file symlink -- not the
    checkout the written path merely starts in. Own fixture: `proj` (main)
    has a linked worktree `feat` on `feature`, which the sibling class's
    fixture deliberately lacks."""

    @classmethod
    def setUpClass(cls):
        # realpath: macOS temp dirs sit behind the /var -> /private/var link.
        cls._dir = os.path.realpath(tempfile.mkdtemp())
        cls.canonical_root = os.path.join(cls._dir, "canon")
        os.makedirs(cls.canonical_root)

        cls.proj = os.path.join(cls.canonical_root, "proj")
        os.makedirs(cls.proj)
        git("init", "-q", "-b", "main", cwd=cls.proj)
        git("config", "user.email", "t@example.com", cwd=cls.proj)
        git("config", "user.name", "Test", cwd=cls.proj)
        with open(os.path.join(cls.proj, "f.txt"), "w") as handle:
            handle.write("hi\n")
        git("add", "-A", cwd=cls.proj)
        git("commit", "-q", "-m", "init", cwd=cls.proj)

        cls.feat = os.path.join(cls.canonical_root, "feat")
        git("worktree", "add", "-q", cls.feat, "-b", "feature", cwd=cls.proj)
        with open(os.path.join(cls.feat, "g.txt"), "w") as handle:
            handle.write("hi\n")

        # feat/alias.txt -> proj/f.txt (write lands in the canonical checkout)
        os.symlink(os.path.join(cls.proj, "f.txt"),
                   os.path.join(cls.feat, "alias.txt"))
        # proj/alias2.txt -> feat/g.txt (write lands in the worktree)
        os.symlink(os.path.join(cls.feat, "g.txt"),
                   os.path.join(cls.proj, "alias2.txt"))

        cls.elsewhere = os.path.join(cls._dir, "elsewhere", "proj2")
        os.makedirs(cls.elsewhere)
        git("init", "-q", "-b", "main", cwd=cls.elsewhere)
        git("config", "user.email", "t@example.com", cwd=cls.elsewhere)
        git("config", "user.name", "Test", cwd=cls.elsewhere)
        with open(os.path.join(cls.elsewhere, "f.txt"), "w") as handle:
            handle.write("hi\n")
        git("add", "-A", cwd=cls.elsewhere)
        git("commit", "-q", "-m", "init", cwd=cls.elsewhere)

        cls.config_path = os.path.join(cls._dir, "config.json")
        with open(cls.config_path, "w") as handle:
            json.dump({"worktree_guard": {"canonical_root": cls.canonical_root}},
                      handle)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def verdict(self, tool, path, cwd):
        return run_guard_full(tool, {"file_path": path}, cwd,
                              config_path=self.config_path)

    def test_write_into_a_missing_directory_in_canonical_is_blocked(self):
        self.assertEqual(
            self.verdict("Write", os.path.join(self.proj, "new-dir", "new.txt"),
                         self.proj), BLOCK)

    def test_write_into_several_missing_directories_is_blocked(self):
        self.assertEqual(
            self.verdict("Write", os.path.join(self.proj, "a", "b", "c", "new.txt"),
                         self.proj), BLOCK)

    def test_edit_through_a_symlink_into_canonical_is_blocked(self):
        self.assertEqual(
            self.verdict("Edit", os.path.join(self.feat, "alias.txt"), self.feat),
            BLOCK)

    def test_edit_through_a_symlink_into_a_worktree_is_allowed(self):
        self.assertEqual(
            self.verdict("Edit", os.path.join(self.proj, "alias2.txt"), self.proj),
            ALLOW)

    def test_write_into_a_missing_directory_in_a_worktree_is_allowed(self):
        self.assertEqual(
            self.verdict("Write", os.path.join(self.feat, "new-dir", "new.txt"),
                         self.feat), ALLOW)

    def test_write_into_a_missing_directory_outside_canonical_root_is_allowed(self):
        self.assertEqual(
            self.verdict("Write",
                         os.path.join(self.elsewhere, "new-dir", "x.txt"),
                         self.elsewhere), ALLOW)


class TestHomeSpellingsResolve(WorktreeGuardTestCase):
    """`$HOME` and `${HOME}` are the same variable to the shell. The braced
    spelling once went unexpanded, so it read as a relative path that
    resolved nowhere and the cross-checkout command was allowed unjudged."""

    def _run(self, command, cwd):
        return run_guard(command, cwd, env={"HOME": self._dir})

    def test_braced_home_in_dash_c_is_blocked(self):
        self.assertEqual(self._run("git -C ${HOME}/wt commit -am x", self.main), BLOCK)

    def test_braced_home_in_cd_is_blocked(self):
        self.assertEqual(self._run("cd ${HOME}/wt && git commit -am x", self.main), BLOCK)

    def test_plain_home_is_blocked(self):
        # The spelling that always worked, as the control for the two above.
        self.assertEqual(self._run("git -C $HOME/wt commit -am x", self.main), BLOCK)

    def test_braced_home_naming_this_checkout_is_allowed(self):
        # Negative control: resolving it must not refuse in-place work.
        self.assertEqual(self._run("git -C ${HOME}/main commit -am x", self.main), ALLOW)


class TestGitWrappersAreJudged(WorktreeGuardTestCase):
    """A bare `git` is judged wherever it appears in a statement, so a
    command wrapper in front of it does not hide it. Gating bare `git` on the
    statement start would stop `echo git -C <other> commit` being refused,
    and would also let every form below through unjudged."""

    def test_wrapped_cross_checkout_commits_are_blocked(self):
        for wrapper in ("sudo", "time", "nohup", "command", "nice"):
            self.assertBlocked("%s git -C %s commit -am x" % (wrapper, self.wt),
                               self.main)

    def test_a_wrapped_in_place_command_is_allowed(self):
        # Control: the wrapper alone is not what refuses.
        for wrapper in ("sudo", "time", "nohup", "command", "nice"):
            self.assertAllowed("%s git status" % wrapper, self.main)


class TestEscapeHatchBothSpellings(WorktreeGuardTestCase):

    def test_wt_allow_main_escape_hatch(self):
        self.assertEqual(
            run_guard("git checkout feature", self.main,
                     env={"WT_ALLOW_MAIN": "1"}),
            ALLOW)

    def test_worktree_allow_escape_hatch_still_works(self):
        self.assertEqual(
            run_guard("git checkout feature", self.main,
                     env={"WORKTREE_ALLOW": "1"}),
            ALLOW)

    def test_only_the_exact_value_1_opens_either_hatch(self):
        # Without this, a hatch that opened for ANY value (`=0`, `=false`,
        # empty) would pass both tests above.
        for name in ("WORKTREE_ALLOW", "WT_ALLOW_MAIN"):
            for value in ("0", "", "true", "yes", " 1"):
                self.assertEqual(
                    run_guard("git checkout feature", self.main,
                              env={name: value}),
                    BLOCK, "%s=%r opened the hatch" % (name, value))


class TestCrossGuardVerdictTable(unittest.TestCase):
    """A shared table of push
    shapes run through BOTH guards, since the branch's premise is a single
    behavioural rule ('empty source side of a refspec deletes on push')
    implemented twice by different means -- claude-danger-guard by `grep -E`
    over the raw command string (never de-quotes), claude-worktree-guard's
    Rule C by a per-token `case` over args already run through zsh's
    ${(z)}/${(Q)} (splits and de-quotes). Divergences between them were once found
    only by cross-running the two by hand; this
    table makes that comparison a standing test rather than a one-off.

    Where the two guards legitimately differ, the table encodes it WITH a
    comment saying why the difference is principled, so it documents the
    seam on purpose rather than freezing an accident."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp()
        cls.canonical_root = os.path.join(cls._dir, "canon")
        os.makedirs(cls.canonical_root)
        cls.proj = os.path.join(cls.canonical_root, "proj")
        os.makedirs(cls.proj)
        git("init", "-q", "-b", "main", cwd=cls.proj)
        git("config", "user.email", "t@example.com", cwd=cls.proj)
        git("config", "user.name", "Test", cwd=cls.proj)
        with open(os.path.join(cls.proj, "f.txt"), "w") as handle:
            handle.write("hi\n")
        git("add", "-A", cwd=cls.proj)
        git("commit", "-q", "-m", "init", cwd=cls.proj)

        cls.worktree_config_path = os.path.join(cls._dir, "worktree-config.json")
        with open(cls.worktree_config_path, "w") as handle:
            json.dump({"worktree_guard": {"canonical_root": cls.canonical_root}},
                      handle)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def run_danger_guard(self, command):
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                              "cwd": self.proj})
        environ = dict(os.environ)
        environ.pop("DANGER_ALLOW", None)
        # No CLAUDE_TOOLKIT_CONFIG needed: none of the push-shape rules under
        # test here consult workspace_root or any other config key -- only
        # the unrelated `rm` scanner does.
        environ["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent.json"
        return subprocess.run([DANGER_GUARD], input=payload, capture_output=True,
                              text=True, env=environ).returncode

    def run_rule_c(self, command):
        return run_guard_full("Bash", {"command": command}, self.proj,
                              config_path=self.worktree_config_path)

    # A refspec assembled from pieces so no single line here reads as a
    # literal --delete/-f/colon-refspec token to anything scanning THIS
    # file's raw text the way the guards scan a command string -- purely
    # cosmetic here (this file is Python source, never a Bash command these
    # guards see), but it keeps the table's intent visually distinct from
    # the string it evaluates to.
    DOLLAR_QUOTE_COLON_MAIN = "$" + "'" + ":main" + "'"

    # (command, expected danger-guard verdict, expected Rule C verdict)
    TABLE = [
        # Bare `:` -- matching push, non-force, deletes nothing. Verified
        # against a real bare repo: exit 0, "Everything
        # up-to-date", every remote ref intact. Both guards agree: ALLOW.
        ("git push origin :", ALLOW, ALLOW),

        # Bare `+:` -- the FORCED form of the same matching push. Verified
        # against a real bare repo with a divergent remote: force-overwrites
        # it, identical effect to `git push origin +main`.
        #   - Rule C: BLOCK (history-rewrite verdict, via its own dedicated
        #     `+:` arm -- the general force-push regex needs a word
        #     character right after `+` and would otherwise miss it).
        #   - danger-guard: BLOCK since 2026-08-29, via a dedicated arm on
        #     its FORCE rule -- needed for exactly the reason Rule C needed
        #     one of its own. This row read ALLOW/BLOCK and was labelled a
        #     known accidental gap until then; the seam is now closed and the
        #     two guards agree. Force and not delete on purpose: there is no
        #     destination, so nothing is deleted.
        ("git push origin +:", BLOCK, BLOCK),

        # Quoted bare `:` -- the first row wearing one pair of quotes, so
        # still a matching push that deletes nothing. danger-guard never
        # de-quotes, and its delete regex used to count the CLOSING quote as
        # the destination and block it. An over-block is a regression here,
        # not extra safety; fixed 2026-08-29 by requiring the destination to
        # begin with a non-quote character. Rule C de-quotes and always saw
        # the bare colon, so only one side moved.
        ("git push origin ':'", ALLOW, ALLOW),

        # `--delete` / `-d` -- a PRINCIPLED divergence as of 2026-08-29, and the
        # newest one in this table. Both spellings do exactly what `:branch`
        # below does, but they ANNOUNCE it, and `Bash(git push *)` already stops
        # them at a prompt. danger-guard exists for the shapes a permission RULE
        # cannot express, so it deliberately does NOT match them: in
        # real command history `--delete` appears in ordinary post-merge
        # cleanup, while the refspec form is rare -- and
        # danger-guard's block is exit 2, a hard refusal needing DANGER_ALLOW=1
        # for the whole session. Rule C blocks anyway, because its concern is
        # narrower and different: not "is this a delete" but "is a canonical
        # checkout's own default branch the place to land this at all".
        ("git push origin --delete foo", ALLOW, BLOCK),
        ("git push origin -d foo", ALLOW, BLOCK),

        ("git push origin :main", BLOCK, BLOCK),
        ("git push origin +:main", BLOCK, BLOCK),
        ("git push origin :refs/heads/main", BLOCK, BLOCK),
        ("git push origin main :old", BLOCK, BLOCK),
        ("git push origin local:remote", ALLOW, ALLOW),
        ("git push origin HEAD:refs/heads/main", ALLOW, ALLOW),
        ("git push git@github.com:user/repo.git main", ALLOW, ALLOW),
        ("git push origin main", ALLOW, ALLOW),

        # --force-with-lease -- a PRINCIPLED divergence, not a gap. Both
        # guards discussed it explicitly and reached opposite, deliberate
        # conclusions: danger-guard names it in its own force-push block
        # message as the SAFE alternative to --force (it refuses when the
        # remote moved) and allows it outright. Rule C blocks it anyway
        # because its concern isn't force-push safety at all -- it's that a
        # canonical checkout's own default branch is never the place to land
        # ANY push carrying new commits from a history-rewriting shape,
        # which is narrower than danger-guard's carve-out on purpose.
        ("git push --force-with-lease origin main", ALLOW, BLOCK),

        # `$':main'` -- was a KNOWN ACCIDENTAL gap between the two until
        # 2026-08-29. danger-guard greps the RAW command string and STILL
        # never de-quotes or expands zsh's ANSI-C ($'...') quoting: the fix
        # is not that it learned to, but that its delete regex now tolerates
        # a literal `$` before the optional quote, the same way it already
        # tolerated the quote itself. Rule C reaches the same verdict by the
        # opposite route -- it tokenizes through zsh's own ${(z)}/${(Q)},
        # which resolves $'...', so the token it judges is the plain `:main`.
        # Same answer, two mechanisms; that is the seam, still real, now
        # agreeing.
        ("git push origin " + DOLLAR_QUOTE_COLON_MAIN, BLOCK, BLOCK),
    ]

    def test_cross_guard_verdict_table(self):
        failures = []
        for row in self.TABLE:
            command, expected_danger, expected_rule_c = row
            actual_danger = self.run_danger_guard(command)
            actual_rule_c = self.run_rule_c(command)
            if actual_danger != expected_danger:
                failures.append(
                    "danger-guard %r: expected %s, got %s"
                    % (command, "BLOCK" if expected_danger == BLOCK else "ALLOW",
                       "BLOCK" if actual_danger == BLOCK else "ALLOW"))
            if actual_rule_c != expected_rule_c:
                failures.append(
                    "Rule C %r: expected %s, got %s"
                    % (command, "BLOCK" if expected_rule_c == BLOCK else "ALLOW",
                       "BLOCK" if actual_rule_c == BLOCK else "ALLOW"))
        self.assertEqual(failures, [], "\n".join(failures))


class TestTheCrossGuardCommentAgreesWithTheRealGuard(unittest.TestCase):
    """`check_rule_c_git` carries a comment stating what claude-danger-guard
    does with each push shape, because which Rule C arms are the ONLY
    coverage depends on it: believing a wrong ALLOW there licenses deleting
    the one refusal a canonical checkout has. A comment about another guard
    cannot be checked by that guard's tests, so this parses the measured
    lines out of the comment and runs danger-guard on each shape it names.

    Distinct from TestCrossGuardVerdictTable on purpose: the table pins the
    two guards' verdicts against each other, this pins the COMMENT against
    one of them. An edit to the prose alone is invisible to the table.
    """

    MARKER = "against this repo's danger-guard, by running it:"

    # Every shape the comment's measured lines can name, mapped to a command
    # that exercises it. A shape in the comment with no entry here fails the
    # test rather than being skipped, so a new claim cannot go unchecked.
    SHAPES = {
        "+:": "git push origin +:",
        "--mirror": "git push --mirror origin",
        ":main": "git push origin :main",
        "--delete": "git push origin --delete foo",
        "-d": "git push origin -d foo",
        "--prune": "git push --prune origin main",
        "--force-with-lease": "git push --force-with-lease origin main",
    }

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def _claimed_verdicts(self):
        """(shape, verdict) pairs parsed out of the comment's measured lines."""
        with open(WORKTREE_LIB, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        start = next((i for i, l in enumerate(lines) if self.MARKER in l), None)
        self.assertIsNotNone(
            start,
            "the comment's measured block is gone: nothing states what "
            "danger-guard actually does, so no reader can tell which arm of "
            "Rule C is the only coverage there is")
        pairs = []
        for line in lines[start + 1:]:
            stripped = line.strip()
            if not stripped.startswith("#"):
                break
            found = re.findall(r"(\S+)\s+(BLOCK|ALLOW)\b", stripped)
            if not found:
                break
            pairs.extend(found)
        return pairs

    def _run_danger_guard(self, command):
        payload = json.dumps({"tool_name": "Bash",
                              "tool_input": {"command": command},
                              "cwd": self._dir})
        environ = dict(os.environ)
        environ.pop("DANGER_ALLOW", None)
        # None of danger-guard's push-shape rules consult config.
        environ["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent.json"
        return subprocess.run([sys.executable, DANGER_GUARD], input=payload,
                              capture_output=True, text=True,
                              env=environ).returncode

    def test_the_comment_parses_at_all(self):
        """The positive control. Without it, a comment reworded so the regex
        matches nothing would pass the agreement test below for free -- zero
        claims checked, zero disagreements found."""
        pairs = self._claimed_verdicts()
        self.assertEqual(
            len(pairs), len(self.SHAPES),
            "parsed %d verdict claims, expected %d -- the measured lines were "
            "reworded and this test is no longer reading them: %r"
            % (len(pairs), len(self.SHAPES), pairs))
        verdicts = {v for _, v in pairs}
        self.assertEqual(
            verdicts, {"BLOCK", "ALLOW"},
            "the comment claims only %r -- a table that lost one side of the "
            "asymmetry is the error shape this guards" % (verdicts,))

    def test_every_shape_named_has_a_command(self):
        unknown = [s for s, _ in self._claimed_verdicts() if s not in self.SHAPES]
        self.assertEqual(
            unknown, [],
            "the comment names shapes this test cannot run, so its claims "
            "about them are unchecked: %r" % (unknown,))

    def test_each_claimed_verdict_matches_danger_guard(self):
        expected = {"BLOCK": BLOCK, "ALLOW": ALLOW}
        disagreements = []
        for shape, claimed in self._claimed_verdicts():
            command = self.SHAPES.get(shape)
            if command is None:
                continue        # reported by the test above
            actual = self._run_danger_guard(command)
            if actual != expected[claimed]:
                disagreements.append(
                    "%s: comment says %s, danger-guard returned %d for %r"
                    % (shape, claimed, actual, command))
        self.assertEqual(
            disagreements, [],
            "the cross-guard comment describes a danger-guard that does not "
            "exist:\n" + "\n".join(disagreements))


class TestHookRegistrationCoversRuleCsSurface(unittest.TestCase):
    """The matcher is part of the payload surface, and no other test sees it.

    Rule C guards direct writes as well as git command shapes, so it needs
    Edit/Write/MultiEdit/NotebookEdit registered. Nothing in the guard or its
    behavioural tests can observe that: they invoke the hook directly with
    a payload, which is precisely the step a real session's matcher decides.
    Before this test existed, reverting the matcher to `"Bash"` left
    the suite green while silently re-inerting the whole Edit half.

    The same file also pins the SPLIT. Both guards used to share one block, so
    widening the matcher for Rule C dragged claude-danger-guard onto every file
    edit too -- a per-invocation cost to reach an allow it could never
    have decided otherwise, since it returns early on any tool it doesn't judge.
    They are separate blocks now and this asserts they stay that way.
    """

    def _rows(self):
        manifest = hook_manifest.load(os.path.dirname(TOOLS_DIR))
        return [row for row in hook_manifest.registrations(manifest)
                if row["event"] == "PreToolUse"]

    def _matcher_for(self, basename):
        found = [row["matcher"] for row in self._rows()
                 if row["exe"] == basename]
        self.assertEqual(len(found), 1,
                         "expected exactly one PreToolUse registration for %s, "
                         "found %d: %r" % (basename, len(found), found))
        return found[0]

    def test_the_worktree_guard_is_registered_for_the_edit_family(self):
        matcher = self._matcher_for("claude-worktree-guard")
        tools = set(matcher.split("|"))
        self.assertEqual(
            tools,
            {"Bash", "Monitor", "Edit", "Write", "MultiEdit", "NotebookEdit"},
            "Rule C guards direct writes; a matcher of %r leaves that half "
            "installed but never invoked" % matcher)

    def test_the_danger_guard_is_not_dragged_onto_file_edits(self):
        # "Bash", "Monitor" and "PowerShell" are the only tools
        # claude-danger-guard judges -- PowerShell unconditionally, Bash and
        # Monitor (a shell command too) by command shape. Any
        # other tool name in this matcher would be Rule C's edit-family
        # surface leaking in, spending a process per file edit for a verdict
        # the guard can never actually reach.
        matcher = self._matcher_for("claude-danger-guard")
        tools = set(matcher.split("|"))
        self.assertEqual(
            tools, {"Bash", "PowerShell", "Monitor"},
            "claude-danger-guard only judges Bash, PowerShell and Monitor, so "
            "registering it for %r spends a process per call for a verdict "
            "it cannot change" % matcher)


class TestDormantRuleCSpendsNothingOnEdits(WorktreeGuardTestCase):
    """With no canonical_root, the Edit surface must cost ZERO git calls.

    This hook is registered for Edit/Write/MultiEdit/NotebookEdit as well as
    Bash, because Rule C guards direct writes and not only git command shapes.
    Rule C is also INACTIVE on any machine that has not configured a
    canonical_root -- which is every machine by default, and deliberately this
    one. So without an early exit, every file edit would pay a config read and
    a `git rev-parse` subprocess to reach a verdict that could only ever have
    been "allow": all of the cost, none of the protection.

    Asserted as an INVOCATION COUNT, not a wall clock. A timing budget here
    would be an assertion against ambient state, which this repo has already
    replaced once for exactly that reason -- and the property being pinned is
    "spawns no subprocess", which a stopwatch only measures indirectly.
    """

    def _guard_with_counting_git(self):
        """A guard copy whose PATH holds a git that LOGS and then execs.

        Same shim-planting mechanism as TestAGenuinelySlowGitIsKilled, via
        `guard_copy_with_git_path`. Unlike the zsh-era version, nothing but
        the shim itself needs to be on `bindir` -- the Python guard allows
        everything on an ordinary edit by returning early (before `_guard_lib`
        is even imported), never by failing to find a tool on PATH, so there
        is no jq-shaped false pass to guard against here.
        """
        bindir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, bindir, ignore_errors=True)
        log = os.path.join(bindir, "git-calls.log")
        real_git = shutil.which("git")
        shim = os.path.join(bindir, "git")
        with open(shim, "w") as handle:
            handle.write(
                "#!/usr/bin/perl\n"
                "open(my $fh, '>>', '%s'); print $fh join(' ', @ARGV), \"\\n\";"
                " close($fh);\n"
                "exec('%s', @ARGV);\n" % (log, real_git))
        os.chmod(shim, 0o755)

        guard = guard_copy_with_git_path(bindir)
        return guard, log

    def _edit(self, guard, config_path):
        payload = json.dumps({"tool_name": "Edit",
                              "tool_input": {"file_path":
                                             os.path.join(self.main, "f.txt")},
                              "cwd": self.main})
        environ = dict(os.environ)
        environ.pop("WORKTREE_ALLOW", None)
        environ.pop("WT_ALLOW_MAIN", None)
        environ["CLAUDE_TOOLKIT_CONFIG"] = config_path
        proc = subprocess.run([guard], input=payload, capture_output=True,
                              text=True, env=environ)
        _fail_if_crashed("Edit %s" % os.path.join(self.main, "f.txt"), proc.stdout)
        return proc

    @staticmethod
    def _calls(log):
        if not os.path.exists(log):
            return []
        with open(log) as handle:
            return [line for line in handle.read().splitlines() if line.strip()]

    def test_no_canonical_root_means_no_git_subprocess(self):
        guard, log = self._guard_with_counting_git()
        proc = self._edit(guard, "/nonexistent.json")
        self.assertEqual(proc.returncode, ALLOW, proc.stderr)
        self.assertEqual(
            self._calls(log), [],
            "a dormant Rule C spawned git on an ordinary edit: %r"
            % (self._calls(log),))

    def test_the_probe_can_see_a_git_call_at_all(self):
        """The positive control. Without it, a probe that observes nothing
        passes the assertion above for free -- which is how a green test ends
        up proving nothing. Configure a canonical_root and the SAME edit must
        now reach git.
        """
        sandbox = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        config_path = os.path.join(sandbox, "config.json")
        with open(config_path, "w") as handle:
            json.dump({"worktree_guard":
                       {"canonical_root": os.path.dirname(self.main)}}, handle)

        guard, log = self._guard_with_counting_git()
        proc = self._edit(guard, config_path)
        self.assertGreater(
            len(self._calls(log)), 0,
            "the counting shim never fired, so the zero above is not evidence")
        # Verdict deliberately unasserted: this control is about whether the
        # probe can OBSERVE, not about what Rule C decides. Pinning the verdict
        # here would couple it to the fixture's default branch name, which
        # `git init` takes from ambient config.
        self.assertIn(proc.returncode, (ALLOW, BLOCK))


class TestACrashIsVisible(unittest.TestCase):
    """No test seam exists in the guard itself for this -- an environment
    switch that made it crash on command would be a second bypass. Instead
    this patches `worktree_lib.evaluate` in a SEPARATE process and runs the
    real entry-point file through `runpy`, so the file under test is
    exercised unmodified and the patch cannot leak into any other test.
    Same pattern as claude-danger-guard's own TestACrashIsVisible."""

    _GUARD_DIR = os.path.dirname(GUARD)

    _PAYLOAD = json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": "echo hi"},
                          "cwd": tempfile.gettempdir()})

    # Inserts the guard's own directory on sys.path (matching how the
    # interpreter bootstraps the real entry point), imports worktree_lib and
    # replaces its `evaluate` with one that always raises, then runs the
    # entry-point FILE itself via runpy so it re-imports the same (now
    # patched) worktree_lib module from sys.modules.
    _PATCHED_HARNESS = (
        "import runpy, sys\n"
        "sys.path.insert(0, %r)\n"
        "import worktree_lib\n"
        "def _boom(payload, ctx):\n"
        "    raise RuntimeError('boom')\n"
        "worktree_lib.evaluate = _boom\n"
        "runpy.run_path(%r, run_name='__main__')\n"
    ) % (_GUARD_DIR, GUARD)

    # The same harness shape, minus the patch -- proves the harness itself
    # (sys.path insertion, runpy) is not what produces the crash marker.
    _UNPATCHED_HARNESS = (
        "import runpy, sys\n"
        "sys.path.insert(0, %r)\n"
        "runpy.run_path(%r, run_name='__main__')\n"
    ) % (_GUARD_DIR, GUARD)

    def test_a_patched_crash_prints_the_marker_and_exits_0(self):
        proc = subprocess.run([sys.executable, "-c", self._PATCHED_HARNESS],
                              input=self._PAYLOAD, capture_output=True, text=True)
        self.assertEqual(proc.returncode, ALLOW)
        self.assertEqual(proc.stderr, "")
        parsed = json.loads(proc.stdout)
        self.assertTrue(parsed["systemMessage"].startswith("claude-worktree-guard crashed"))
        self.assertIn("RuntimeError: boom", parsed["systemMessage"])
        self.assertEqual(set(parsed), {"systemMessage", "hookSpecificOutput"})
        self.assertEqual(
            parsed["systemMessage"],
            "claude-worktree-guard crashed and did NOT check this command: RuntimeError: boom")
        self.assertEqual(parsed["hookSpecificOutput"], {
            "hookEventName": "PreToolUse",
            "additionalContext": parsed["systemMessage"]
            + ". This guard's check did not run; other guards and permission "
              "rules still apply. Tell the user this guard is broken.",
        })
        self.assertNotIn("permissionDecision", proc.stdout)

    def test_the_marker_check_raises_on_that_stdout(self):
        # The helper's own positive control: `_fail_if_crashed` is what
        # `run_guard_capture` (and the direct TestMalformedInput call sites)
        # call on every invocation, and this proves it actually raises
        # against a real crashed-guard stdout rather than merely being wired
        # in and never firing.
        proc = subprocess.run([sys.executable, "-c", self._PATCHED_HARNESS],
                              input=self._PAYLOAD, capture_output=True, text=True)
        with self.assertRaises(AssertionError):
            _fail_if_crashed("echo hi", proc.stdout)

    def test_the_unpatched_harness_allows_normally(self):
        proc = subprocess.run([sys.executable, "-c", self._UNPATCHED_HARNESS],
                              input=self._PAYLOAD, capture_output=True, text=True)
        self.assertEqual(proc.returncode, ALLOW)
        self.assertEqual(proc.stdout, "")

    def test_a_library_that_fails_to_import_is_reported(self):
        """The entry point alone, copied where no worktree_lib.py sits beside
        it, so its import fails exactly as a broken install's would. It must
        still allow, and say it did not check."""
        sandbox = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, sandbox, ignore_errors=True)
        lone = os.path.join(sandbox, "claude-worktree-guard")
        shutil.copy(GUARD, lone)
        proc = subprocess.run([sys.executable, lone], input=self._PAYLOAD,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, ALLOW)
        parsed = json.loads(proc.stdout)
        self.assertTrue(
            parsed["systemMessage"].startswith(
                "claude-worktree-guard crashed and did NOT check this command: "),
            parsed["systemMessage"])
        self.assertIn("worktree_lib", parsed["systemMessage"])
        self.assertNotIn("permissionDecision", proc.stdout)


class TestDeliberateVerdictChanges(WorktreeGuardTestCase):
    """A deliberate change from the zsh version (is_sep reuses
    guard_lib._SEGMENT_SEPARATORS,
    which recognises `&`, `&|`, `&!` as segment boundaries the zsh original's
    own is_sep did not) and the heredoc/comment handling that comes from
    reusing guard_lib's tokenizer (`_strip_heredocs`, comment stripping)
    rather than the zsh original's own hand-rolled versions of each.

    Every row was measured against the zsh original (same fixture repos --
    make_repo_with_worktree's main/wt, run from main) before this class was
    written. Where the zsh and Python columns
    differ, that is the deliberate change this class pins, not a bug.
    """

    def test_background_ampersand_cross_checkout_commit_is_blocked(self):
        # zsh: ALLOW (`&` was never a separator there, so the whole line was
        # one statement and the walk never saw a second `git`). Python: BLOCK
        # -- guard_lib._SEGMENT_SEPARATORS includes `&`, so this is two
        # statements and the second is judged on its own, same as `&&`/`;`.
        self.assertBlocked(
            "git status & git -C %s commit -am x" % self.wt, self.main)

    def test_background_disown_ampersand_pipe_is_blocked(self):
        # zsh: ALLOW. Python: BLOCK -- `&|` is zsh's background-and-disown
        # terminator, recognised as a separator here but not by the zsh
        # original's own is_sep.
        self.assertBlocked(
            "git status &| git -C %s commit -am x" % self.wt, self.main)

    def test_background_disown_ampersand_bang_is_blocked(self):
        # zsh: ALLOW. Python: BLOCK -- same reasoning as `&|`, for `&!`.
        self.assertBlocked(
            "git status &! git -C %s commit -am x" % self.wt, self.main)

    def test_comment_inside_a_substitution_does_not_hide_a_later_statement(self):
        # zsh: ALLOW -- not from any comment handling (zsh's (z) tokenizer
        # has none; see the measurement on the row below, where `#` comes
        # through as an ordinary word). The single quote before `q` never
        # closes, so `${(z)cmd}` folds everything after it -- the closing
        # `)`, the `;`, and the second git statement -- into ONE token
        # (measured), and the walk never sees a second statement to judge.
        # Python: BLOCK -- guard_lib's tokenizer matches the substitution's
        # own parens independently of the quote inside it, so `$(...)` still
        # closes where it should and the statement after the real `;` is
        # judged normally.
        command = "echo $(git log # ' q\n); git -C %s commit -am x" % self.wt
        self.assertBlocked(command, self.main)

    def test_a_quoted_heredoc_marker_does_not_hide_a_later_statement(self):
        # zsh: ALLOW (its own heredoc stripper matched the `<<` text itself,
        # even quoted, and ate the following line as a heredoc body it was
        # never going to be). Python: BLOCK -- guard_lib._strip_heredocs only
        # recognises an UNQUOTED `<<`, so a line that merely quotes the
        # characters `<< EOF` starts no heredoc at all, and the git statement
        # on the next line is judged normally.
        command = 'echo "<< EOF"\ngit -C %s commit -am x' % self.wt
        self.assertBlocked(command, self.main)

    def test_a_comment_followed_by_a_semicolon_on_the_same_line_is_allowed(self):
        # zsh: BLOCK -- not from any comment handling. zsh's (z) tokenizer
        # has none: measured, `${(z)}` on this exact string yields
        # `[git] [status] [#] [;] [git] -C /o commit -am x ...`, i.e. `#` is
        # an ordinary word. `;` is a real separator regardless, so the walk
        # sees two statements -- the harmless `git status #` and the real
        # cross-checkout `git commit`, which Rule A blocks. Python: ALLOW --
        # guard_lib's tokenizer DOES treat `#` as a genuine shell comment,
        # consuming everything to the end of this line (the `;` and the
        # second git statement included) and leaving only `git status` to
        # judge.
        command = "git status # ; git -C %s commit -am x" % self.wt
        self.assertAllowed(command, self.main)

    def test_a_comment_ending_at_a_real_newline_still_exposes_the_next_line(self):
        # Both: BLOCK. The comment swallows only its own line; a git
        # statement on the line AFTER it is still judged -- the control that
        # shows the row above is about mid-line `#`, not comments generally.
        command = "git status #\ngit -C %s commit -am x" % self.wt
        self.assertBlocked(command, self.main)

    def test_an_inline_comment_with_no_following_statement_is_allowed(self):
        # Both: ALLOW. Nothing to judge follows the comment on this line, and
        # there is no second line.
        command = "git status # git -C %s commit -am x" % self.wt
        self.assertAllowed(command, self.main)


class TestNoStatementHidesBehindASeparator(WorktreeGuardTestCase):
    """The walk once misidentified where a statement ends: a `git`
    with no subcommand, or a git/env option whose value slot held a
    separator, stepped over the separator; and a quoted or escaped separator
    was taken for a real one. Every method is pinned against a measured
    pre-fix verdict (2026-10-03)."""

    # Pre-fix: ALLOW (red) -- `which git` leaves subcommand empty and the
    # args scan started past the `&&`, hiding the cross-checkout commit.
    def test_which_git_and_chain(self):
        self.assertBlocked("which git && git -C %s commit -am x" % self.wt,
                           self.main)

    # Pre-fix: ALLOW (red) -- same shape with `echo git` and `;`.
    def test_echo_git_semicolon(self):
        self.assertBlocked("echo git; git -C %s commit -am x" % self.wt,
                           self.main)

    # Pre-fix: ALLOW (red) -- `&` is also a separator the walk stepped over.
    def test_echo_git_background(self):
        self.assertBlocked("echo git & git -C %s commit -am x" % self.wt,
                           self.main)

    # Pre-fix: ALLOW (red) -- bare `git --version` has no subcommand either.
    def test_git_version_semicolon(self):
        self.assertBlocked(
            "git --version; git -C %s commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW (red) -- `tokenize` emits a newline as `;`, same defect.
    def test_git_version_newline(self):
        self.assertBlocked(
            "git --version\ngit -C %s commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW (red) -- bare `git;` hid the Rule B `reset --hard`.
    def test_bare_git_then_rule_b(self):
        self.assertBlocked("git; git reset --hard", self.main)

    # Pre-fix: ALLOW (red) -- `-C`'s value slot held a separator, which was
    # taken as the directory and stepped over.
    def test_dash_C_without_value(self):
        self.assertBlocked("git -C ; git -C %s commit -am x" % self.wt,
                           self.main)

    # Pre-fix: ALLOW (red) -- same shape for `-c`.
    def test_dash_c_without_value(self):
        self.assertBlocked("git -c ; git -C %s commit -am x" % self.wt,
                           self.main)

    # Pre-fix: ALLOW (red) -- same shape for `--git-dir`.
    def test_git_dir_without_value(self):
        self.assertBlocked(
            "git --git-dir ; git -C %s commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW (red) -- same shape for `--work-tree`.
    def test_work_tree_without_value(self):
        self.assertBlocked(
            "git --work-tree && git -C %s commit -am x" % self.wt, self.main)

    # Pre-fix: ALLOW (red) -- `env -C`'s value slot held a separator too.
    def test_env_dash_C_without_value(self):
        self.assertBlocked("env -C ; git reset --hard", self.main)

    # Pre-fix: ALLOW (red) -- the unquoted walk saw the quoted `')'` as a
    # real close-paren and popped the subshell early, hiding the commit.
    def test_quoted_paren_does_not_close_subshell(self):
        self.assertBlocked(
            "(cd %s && echo ')' && git commit -am x)" % self.wt, self.main)

    # Pre-fix: ALLOW (red) -- same shape, escaped rather than quoted.
    def test_escaped_paren_does_not_close_subshell(self):
        self.assertBlocked(
            "(cd %s && echo \\) && git commit -am x)" % self.wt, self.main)

    # Pre-fix: ALLOW (red) -- the quoted `')'` was a git argument here, not
    # an `echo` argument, and still misread as a real close-paren.
    def test_quoted_paren_git_arg_does_not_close_subshell(self):
        self.assertBlocked(
            "(cd %s && git log ')' && git commit -am x)" % self.wt,
            self.main)

    # Pre-fix: ALLOW (red) -- a quoted `'('` was taken as a real open-paren,
    # desynchronising the subshell nesting count.
    def test_quoted_open_paren_does_not_open_subshell(self):
        self.assertBlocked(
            "(cd %s && (cd %s && echo '(') && git commit -am x)"
            % (self.wt, self.main), self.main)

    # Pre-fix: ALLOW (red) -- same shape, escaped rather than quoted.
    def test_escaped_open_paren_does_not_open_subshell(self):
        self.assertBlocked(
            "(cd %s && (cd %s && echo \\() && git commit -am x)"
            % (self.wt, self.main), self.main)

    # Pre-fix: ALLOW (green) -- bare `git --version` then a read-only `git
    # status`; no cross-checkout write anywhere in the command.
    def test_git_version_then_read_only(self):
        self.assertAllowed("git --version; git status", self.main)

    # Pre-fix: ALLOW (green) -- `which git` then a harmless `echo x`.
    def test_which_git_then_echo(self):
        self.assertAllowed("which git && echo x", self.main)

    # Pre-fix: ALLOW (green) -- `-C`'s value slot is empty, then a read-only
    # `git status`.
    def test_dash_C_without_value_then_read_only(self):
        self.assertAllowed("git -C ; git status", self.main)

    # Pre-fix: ALLOW (green) -- bare `git` alone does nothing judgeable.
    def test_bare_git_alone(self):
        self.assertAllowed("git", self.main)

    # Pre-fix: ALLOW (green) -- `env -C`'s value slot is empty, then a
    # read-only `git status`.
    def test_env_dash_C_without_value_then_read_only(self):
        self.assertAllowed("env -C ; git status", self.main)

    # Pre-fix: BLOCK (red, false positive) -- the old walk believed the `cd`
    # ran at top level, but it is inside a closed subshell: `git commit`
    # after it runs in self.main, never self.wt.
    def test_subshell_cd_ends_at_paren_after_bare_git(self):
        self.assertAllowed("(cd %s; git); git commit -am x" % self.wt,
                           self.main)

    # Pre-fix: BLOCK (red, false positive) -- `cd` is an argument to `echo`,
    # not a real `cd`; `git commit` still runs in self.main.
    def test_quoted_semicolon_starts_no_statement(self):
        self.assertAllowed(
            "echo ';' cd %s && git commit -am x" % self.wt, self.main)

    # Pre-fix: BLOCK (red, false positive) -- the second `git -C ... commit`
    # is an argument to `git log`, not a separate statement.
    def test_quoted_semicolon_inside_git_args(self):
        self.assertAllowed(
            "git log ';' git -C %s commit -am x" % self.wt, self.main)


class TestGitDashCMatchesRealGit(WorktreeGuardTestCase):
    """R1-05: `-C` values apply in sequence, relative --git-dir/--work-tree
    resolve against the directory after every `-C`, and a path- or
    case-spelled `git` counts at statement start. Real git is the oracle for
    the sequencing cases, so a wrong premise fails here, not in production.
    The attached `-CVALUE` form is NOT tested: git itself rejects it."""

    def _oracle_toplevel(self, options, cwd):
        environ = {k: v for k, v in os.environ.items()
                   if not k.startswith("GIT_")}
        proc = subprocess.run(
            ["git"] + options + ["rev-parse", "--show-toplevel"],
            cwd=cwd, capture_output=True, text=True, env=environ)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return os.path.realpath(proc.stdout.strip())

    def _assert_blocked_like_git(self, options):
        self.assertEqual(self._oracle_toplevel(options, self.wt),
                         os.path.realpath(self.main))
        command = "git %s commit -m x" % " ".join(
            "''" if o == "" else o for o in options)
        self.assertBlocked(command, self.wt)

    def test_last_dash_c_dot_is_relative_to_the_previous(self):
        self._assert_blocked_like_git(["-C", self.main, "-C", "."])

    def test_empty_dash_c_leaves_the_directory_unchanged(self):
        self._assert_blocked_like_git(["-C", self.main, "-C", ""])

    def test_relative_dash_c_after_parent_dash_c(self):
        parent, base = os.path.split(self.main)
        self._assert_blocked_like_git(["-C", parent, "-C", base])

    def test_relative_git_dir_and_work_tree_follow_dash_c(self):
        parent, base = os.path.split(self.main)
        self._assert_blocked_like_git(
            ["-C", parent, "--git-dir=%s/.git" % base,
             "--work-tree=%s" % base])

    def test_path_spelled_git_is_blocked(self):
        if not os.path.exists("/usr/bin/git"):
            self.skipTest("/usr/bin/git not present")
        self.assertBlocked("/usr/bin/git -C %s commit -m x" % self.main,
                           self.wt)

    def test_upper_case_git_is_blocked(self):
        self.assertBlocked("GIT -C %s commit -m x" % self.main, self.wt)

    def test_quoted_git_is_blocked(self):
        self.assertBlocked("'git' -C %s commit -m x" % self.main, self.wt)

    def test_dash_c_dot_is_allowed(self):
        self.assertAllowed("git -C . status", self.wt)

    def test_empty_dash_c_is_allowed(self):
        self.assertAllowed('git -C "" status', self.wt)

    def test_path_spelled_git_mid_statement_is_an_argument(self):
        self.assertAllowed(
            "echo /usr/bin/git -C %s commit" % self.main, self.wt)


class TestQuotedSemicolonIsAnOptionValue(WorktreeGuardTestCase):
    """A quoted or escaped `;` is an option value, not a separator; both
    methods pass pre-fix and pin against the naive fix. Lives in its own
    class because the symlink changes other verdicts: the old `env -C ;`
    parser takes `;` as a directory, which this symlink resolves to
    `wt`."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        os.symlink(cls.wt, os.path.join(cls.main, ";"))

    # Pre-fix: BLOCK (green) -- the quoted `;` is `-C`'s directory value,
    # which the symlink resolves to self.wt.
    def test_quoted_semicolon_is_a_dash_C_value(self):
        self.assertBlocked("git -C ';' commit -am x", self.main)

    # Pre-fix: BLOCK (green) -- same shape, escaped rather than quoted.
    def test_escaped_semicolon_is_a_dash_C_value(self):
        self.assertBlocked("git -C \\; commit -am x", self.main)


class TestReservedWordIsAnOptionValue(WorktreeGuardTestCase):
    """`!`, `{` and `}` sit in the shared separator set, but they are reserved
    words: special only where a command starts, ordinary words in an
    argument slot. So `git -C !` takes `!` as its directory, in bash and zsh
    alike. Treating them as the end of the statement
    there lost the directory and allowed a cross-checkout commit. Each
    symlink below resolves to self.wt, so every method must BLOCK."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for name in ("!", "{", "}"):
            os.symlink(cls.wt, os.path.join(cls.main, name))

    def test_bang_is_a_dash_C_value(self):
        self.assertBlocked("git -C ! commit -am x", self.main)

    def test_open_brace_is_a_dash_C_value(self):
        self.assertBlocked("git -C { commit -am x", self.main)

    def test_close_brace_is_a_dash_C_value(self):
        self.assertBlocked("git -C } commit -am x", self.main)

    def test_bang_is_an_env_dash_C_value(self):
        self.assertBlocked("env -C ! git commit -am x", self.main)


class TestDeepNestingBlocks(WorktreeGuardTestCase):
    """A quoted substitution nested at every level, unlike the danger guard's
    own equivalent fixture only in what follows it -- `; git status` rather
    than `; rm -rf`, since this is the worktree guard's own recursion limit,
    reached by the same shared `guard_lib.tokenize`."""

    def _guard_lib(self):
        sys.path.insert(0, os.path.join(TOOLS_DIR, "danger-guard"))
        import guard_lib
        return guard_lib

    def test_tokenize_itself_raises_recursion_error_at_400_levels(self):
        """The fixture's own positive control: without this, a guard that
        merely failed to recurse at all (e.g. by truncating input) would
        still pass the BLOCK assertion below for the wrong reason."""
        command = "echo " + '"$(echo ' * 400 + "x" + ')"' * 400 + "; git status"
        with self.assertRaises(RecursionError):
            self._guard_lib().tokenize(command)

    def test_400_levels_of_quoted_substitution_blocks_with_the_too_deep_reason(self):
        command = "echo " + '"$(echo ' * 400 + "x" + ')"' * 400 + "; git status"
        proc = run_guard_capture("Bash", {"command": command}, self.main)
        self.assertEqual(proc.returncode, BLOCK)
        self.assertIn("a command nested too deeply to parse", proc.stderr)
        self.assertNotIn(_CRASH_MARKER, proc.stdout)

    def test_20_levels_allows_normally(self):
        command = "echo " + '"$(echo ' * 20 + "x" + ')"' * 20 + "; git status"
        self.assertAllowed(command, self.main)


class TestCanonicalRootSpellings(unittest.TestCase):
    """Every spelling of `canonical_root` that names the same directory must
    arm Rule C. Runs the real executable; a commit on `main` inside the
    canonical root is BLOCK only when the configured root resolved to it."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp(dir="/tmp")
        cls.home = os.path.join(cls._dir, "home")
        cls.canon = os.path.join(cls.home, "canon")
        cls.proj = os.path.join(cls.canon, "proj")
        os.makedirs(cls.proj)
        git("init", "-q", "-b", "main", cwd=cls.proj)
        git("config", "user.email", "t@example.com", cwd=cls.proj)
        git("config", "user.name", "Test", cwd=cls.proj)
        git("commit", "-q", "--allow-empty", "-m", "init", cwd=cls.proj)
        cls.env = {"HOME": cls.home, "TK_CANON": cls.canon, "TK_H": cls.home}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._dir, ignore_errors=True)

    def _config(self, config):
        path = os.path.join(self._dir, "config.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        return path

    def test_control_no_canonical_root_allows(self):
        self.assertEqual(
            run_guard("git commit -am x", self.proj, env=self.env,
                      config_path=self._config({})),
            ALLOW)

    def test_every_spelling_activates_rule_c(self):
        for canonical_root in [self.canon, "~/canon", "$HOME/canon",
                               "${HOME}/canon", "$TK_CANON", "${TK_CANON}",
                               "$TK_H/canon"]:
            with self.subTest(canonical_root=canonical_root):
                config = {"worktree_guard": {"canonical_root": canonical_root}}
                self.assertEqual(
                    run_guard("git commit -am x", self.proj, env=self.env,
                              config_path=self._config(config)),
                    BLOCK)


class TestLoaderNeverReadsProcessEnvironmentWT(unittest.TestCase):
    """`load_config` resolves `$VAR` only from the `environ` it is handed.
    The process environment holds real directories here, so a loader that
    falls back to `os.environ` resolves them and the assertions fail."""

    def setUp(self):
        sys.path.insert(0, os.path.dirname(GUARD))
        import worktree_lib
        self.worktree_lib = worktree_lib
        self._dir = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, self._dir, ignore_errors=True)
        self.root = os.path.join(self._dir, "R")
        os.makedirs(os.path.join(self.root, "c"))
        for key in ("TK_LEAK_W", "TK_LEAK_X"):
            self.addCleanup(self._restore, key, os.environ.get(key))
            os.environ[key] = self.root
        self.config_path = os.path.join(self._dir, "config.json")
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump({"workspace_root": "$TK_LEAK_W/ws",
                       "worktree_guard": {"canonical_root": "${TK_LEAK_X}/c"}},
                      handle)

    @staticmethod
    def _restore(key, previous):
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous

    def test_environ_omitted(self):
        self.assertEqual(
            self.worktree_lib.load_config(self.config_path, "/h"), ("", ""))

    def test_environ_empty(self):
        self.assertEqual(
            self.worktree_lib.load_config(self.config_path, "/h", {}), ("", ""))

    def test_environ_supplied_resolves(self):
        environ = {"TK_LEAK_W": self.root, "TK_LEAK_X": self.root}
        self.assertEqual(
            self.worktree_lib.load_config(self.config_path, "/h", environ),
            (self.root + "/ws", os.path.realpath(self.root + "/c")))


class TestConfigModuleUnavailableWT(unittest.TestCase):
    """When `lib/toolkit_config.py` cannot be imported, the path is absent
    and the loader returns empty values -- it never raises."""

    def test_import_error_yields_empty_config(self):
        import types
        from unittest import mock
        sys.path.insert(0, os.path.dirname(GUARD))
        import worktree_lib

        def unavailable():
            raise ImportError("toolkit_config unavailable")

        tmp = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        real_dir = os.path.join(tmp, "canon")
        os.makedirs(real_dir)
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({"worktree_guard": {"canonical_root": real_dir}}, handle)
        fake = types.SimpleNamespace(_toolkit_config=unavailable)
        with mock.patch.object(worktree_lib, "_guard_lib", return_value=fake):
            self.assertEqual(
                worktree_lib.load_config(config_path, "/h", {}), ("", ""))


if __name__ == "__main__":
    unittest.main(verbosity=1)
