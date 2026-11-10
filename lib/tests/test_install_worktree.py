#!/usr/bin/env python3
"""Behavioural tests for install.sh's linked-worktree guard.

The guard used to match `$REPO` against the literal string
`*/.claude/worktrees/*` — this repo's own convention and nothing else. Any
worktree created anywhere but there (`git worktree add ../foo`, a temp dir, a
`wt`-style helper with its own layout) sailed straight through a real apply and
repointed every `~/.local/bin` symlink — `claude-danger-guard` included — at a
directory that vanishes the moment the worktree is removed. That disarms every
guard and command on the machine, and it is completely silent: the install
reports success.

Detection is now git-based: a linked worktree's resolved `--git-dir` and
`--git-common-dir` differ, while the primary checkout's name the same directory.
That is a property of the repository rather than of anyone's naming convention.

These tests drive the REAL `install.sh` through `subprocess` (per this repo's
rule to test executables, not just the lib) against git fixtures built fresh in
a temp dir — never against this machine's real worktrees or its real `~/.claude`.
Every case that runs a real apply points `$HOME` at a sandbox directory, so
nothing here can touch the real `~/.claude` or `~/.local/bin`.

They are also the first tests this repo has ever had for `install.sh` at all.
Every prior installer change was verified by running it, which is how a guard
that only recognised one naming convention survived as long as it did.

Ported from the sibling toolkit in the mouramarket account, where the same defect
was found first. The fixture helpers here are deliberately shared-shaped so the
hooks-merge and consent suites can reuse them when the installer behaviour they
cover lands.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

import install_fixture as fx

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
SETUP_GIT_SH = os.path.join(REPO_ROOT, "scripts", "setup-git.sh")
WORKTREE_DETECT_SH = os.path.join(REPO_ROOT, "lib", "worktree_detect.sh")

REFUSAL = "refusing to install from a linked git worktree"

# The first section install.sh prints after the guard, and before it reads
# config or runs first-time setup. Its presence proves execution got past the
# check point, whatever the stub fixture does afterwards.
PAST_THE_GUARD = "Directories"


def git(*args, cwd):
    """Every fixture git call, with the machine's own git config neutralised.

    Without this, `git commit` in a throwaway fixture runs whatever the machine
    has in global `core.hooksPath` — on the author's machine that is this repo's
    own secret-scanning pre-commit, verified firing inside temp repos. Benign for
    today's fixture content, but it makes the whole suite depend on a global hook
    being present, fast and passing: a machine whose global pre-commit fails gets
    `CalledProcessError` from the fixture builder rather than a meaningful test
    failure. The repo's rule is that tests may not depend on what happens to be
    installed.
    """
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, env=env,
                          universal_newlines=True)


def make_fixture_repo(root, name="fixture"):
    """A minimal git repo shaped just enough like this toolkit for install.sh to
    run past the worktree guard: a symlink to the REAL install.sh (so the code
    under test is the actual repo script, not a copy) plus the `claude/*.json`
    files install.sh reads unconditionally, even under --check.

    Deliberately does NOT include `config.example.json` (`add_plugin_layout`
    does copy `lib/setup_config.py`, which the declined-core preflight needs).
    A real apply against a sandbox HOME with no config reaches first-time setup
    and dies there — which is fine, because every real-apply case below
    asserts only that it got PAST the guard, never that a full install
    completes against a stub repo. Adding the example would make the fixture
    run further and prove nothing extra.
    """
    repo = os.path.join(root, name)
    os.makedirs(os.path.join(repo, "claude"))
    os.makedirs(os.path.join(repo, "tools"))
    os.makedirs(os.path.join(repo, "lib"))
    os.symlink(INSTALL_SH, os.path.join(repo, "install.sh"))
    # The guard sources this; without it install.sh fails CLOSED and every
    # case here would pass for the wrong reason.
    os.symlink(WORKTREE_DETECT_SH,
               os.path.join(repo, "lib", "worktree_detect.sh"))
    fx.add_plugin_layout(repo)
    # A link whose source is missing from the repo is a required failure, so the
    # fixture carries an empty target for every `link` call (committed, so a
    # linked worktree of it has them too). The seed_* helpers below overwrite
    # individual ones with stub content.
    fx.add_link_targets(repo, root)
    with open(os.path.join(repo, "claude", "permissions.baseline.json"), "w") as handle:
        handle.write('{"permissions": {}}\n')
    git("init", "-q", "-b", "main", repo, cwd=root)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def add_worktree(repo, root, branch="feature", name="fixture-wt"):
    wt = os.path.join(root, name)
    git("worktree", "add", "-q", wt, "-b", branch, cwd=repo)
    return wt


def make_fixture_repo_nested_install(root, name="nested_fixture", subdir="nested"):
    """A PLAIN (non-worktree) git repo whose `install.sh` lives one level BELOW
    the git top-level rather than at it.

    `$REPO` in install.sh comes from the script's own location
    (`dirname "${BASH_SOURCE[0]}"`), not from `git rev-parse --show-toplevel`,
    so `is_linked_worktree` is invoked with `-C <subdir>` here. That forces git
    to discover the repository by walking UP from `<subdir>`, which makes
    `--git-dir` come back already absolute while `--git-common-dir` comes back
    relative to `<subdir>` (`../.git`). A raw string comparison of those two
    disagrees even though they name the same directory — exactly the false
    refusal the absolute-path resolution step exists to prevent.
    """
    repo = os.path.join(root, name)
    nested = os.path.join(repo, subdir)
    os.makedirs(os.path.join(nested, "claude"))
    os.makedirs(os.path.join(nested, "tools"))
    os.makedirs(os.path.join(nested, "lib"))
    os.symlink(INSTALL_SH, os.path.join(nested, "install.sh"))
    os.symlink(WORKTREE_DETECT_SH,
               os.path.join(nested, "lib", "worktree_detect.sh"))
    fx.add_plugin_layout(nested)
    with open(os.path.join(nested, "claude", "permissions.baseline.json"), "w") as handle:
        handle.write('{"permissions": {}}\n')
    git("init", "-q", "-b", "main", repo, cwd=root)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return nested


def run_install(cwd, home, args=(), extra_env=None):
    env = dict(os.environ)
    # install.sh honours CLAUDE_TOOLKIT_CONFIG, so an exported one would
    # select profiles this fixture does not carry.
    env.pop("CLAUDE_TOOLKIT_CONFIG", None)
    env["HOME"] = home
    # Never inherit the escape hatch from the runner's own shell — that would
    # silently turn every refusal case below into a pass.
    env.pop("ALLOW_WORKTREE_INSTALL", None)
    if extra_env:
        env.update(extra_env)
    env = fx.shim_env_for_home(env, home)
    if "--check" in args:
        fx.seed_home_installed(home, cwd)
    # stdin=DEVNULL is load-bearing, not tidiness: `capture_output=True` does
    # NOT redirect stdin, so install.sh's first-time setup — which branches on
    # `sys.stdin.isatty()` — would inherit the runner's terminal and block
    # forever. The failure is asymmetric and therefore nearly invisible: it
    # hangs when a human runs the suite from a terminal and passes when CI runs
    # it from a pipe. The timeout is the backstop for every other way a real
    # apply could block. (Raised by a peer session that lost a BLOCKING finding
    # to exactly this.)
    return subprocess.run([os.path.join(cwd, "install.sh")] + list(args),
                          cwd=cwd, env=env, capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=120,
                          universal_newlines=True)


class FixtureTestCase(unittest.TestCase):
    """Common temp-dir plumbing: a fresh git root and a fresh sandbox HOME,
    both cleaned up automatically."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)


class WorktreeFixtureTestCase(FixtureTestCase):
    """A primary checkout plus one linked worktree off it (`git worktree add`),
    at a path the old `*/.claude/worktrees/*` pattern could not see."""

    def setUp(self):
        super(WorktreeFixtureTestCase, self).setUp()
        self.repo = make_fixture_repo(self.root)
        self.wt = add_worktree(self.repo, self.root)


class TestRealApplyFromLinkedWorktree(WorktreeFixtureTestCase):

    def test_refuses_names_the_directory_exits_1_and_writes_nothing(self):
        proc = run_install(self.wt, self.home)

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(REFUSAL, proc.stderr)
        self.assertIn(self.wt, proc.stderr,
                      "refusal message did not name the offending directory")
        # The guard must fire before the FIRST filesystem write — "Directories"
        # is the first section that creates anything — so the sandbox HOME,
        # which started empty, must still be empty.
        self.assertEqual(os.listdir(self.home), [],
                         "something was written to HOME before the guard fired")


class TestOldPathPatternWouldHaveMissedThis(WorktreeFixtureTestCase):
    """The regression this whole file exists for, stated as an assertion.

    The fixture worktree lives in a temp dir, so the retired
    `*/.claude/worktrees/*` pattern does not match its path. If someone
    reintroduces a path-shaped check, this fails while a suite full of
    `.claude/worktrees/`-shaped fixtures would still pass — which is how the
    original defect survived.
    """

    def test_the_refused_worktree_is_not_under_a_claude_worktrees_path(self):
        self.assertNotIn("/.claude/worktrees/", self.wt,
                         "fixture no longer proves the pattern-independence "
                         "this test exists for")
        proc = run_install(self.wt, self.home)
        self.assertIn(REFUSAL, proc.stderr)


class TestGitDirEnvVarCannotDisarmTheGuard(WorktreeFixtureTestCase):
    """`git -C <path> rev-parse` ignores `-C` for repository discovery entirely
    when GIT_DIR is set in the environment — git uses the env var outright. A
    stray GIT_DIR left over in the invoking shell (from an earlier
    `GIT_DIR=... git ...`, a wrapper script, or a tool that exports and never
    unsets) would make BOTH `--git-dir` and `--git-common-dir` resolve to
    wherever that GIT_DIR points, so a real linked worktree would compare equal
    to itself and the refusal would never fire — the exact catastrophe the guard
    exists to prevent. `is_linked_worktree` must neutralize GIT_DIR (and
    GIT_COMMON_DIR / GIT_WORK_TREE) for every git call it makes, regardless of
    what the parent shell has exported."""

    def test_refusal_still_fires_with_stray_git_dir_exported(self):
        # Point GIT_DIR at the PRIMARY repo's .git — the value most likely to be
        # left over from unrelated work in the same shell, and the one that would
        # make the worktree compare equal to itself.
        stray_git_dir = os.path.join(self.repo, ".git")
        proc = run_install(self.wt, self.home,
                           extra_env={"GIT_DIR": stray_git_dir})

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(REFUSAL, proc.stderr)
        self.assertIn(self.wt, proc.stderr)
        self.assertEqual(os.listdir(self.home), [],
                         "something was written to HOME before the guard fired")

    def test_a_stray_git_common_dir_fails_safe_via_the_fallback(self):
        """Named for what actually happens, which is NOT what `-u GIT_COMMON_DIR`
        is doing.

        Measured: a stray GIT_COMMON_DIR does not make the two values compare
        equal — it makes `rev-parse` fail outright. Resolution then returns 1 and
        the `.git`-file fallback delivers the refusal. So this refusal is real and
        worth pinning, but deleting `-u GIT_COMMON_DIR` does not break it, and an
        earlier version of this test claimed otherwise under a name promising the
        neutralisation was covered.

        That is the second time this one test passed for the wrong reason. The
        first fix retargeted the env var at the worktree's own gitdir and only
        changed WHICH wrong reason. The lesson is in CLAUDE.md: when two
        mechanisms can produce the same refusal, a test naming one of them proves
        nothing until the other is disabled.
        """
        own_git_dir = subprocess.run(
            ["git", "-C", self.wt, "rev-parse", "--absolute-git-dir"],
            capture_output=True, universal_newlines=True, check=True).stdout.strip()
        proc = run_install(self.wt, self.home,
                           extra_env={"GIT_COMMON_DIR": own_git_dir})

        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn(REFUSAL, proc.stderr)

    def test_only_git_dir_can_actually_cause_a_miss(self):
        """The asymmetry, asserted rather than described.

        GIT_DIR is the only one of the three that is load-bearing: from inside a
        linked worktree it makes BOTH values succeed and resolve to the stray
        target, so they compare EQUAL, resolution returns success, and the
        fallback is never reached — a silent miss. GIT_COMMON_DIR makes rev-parse
        fail instead, which routes to the fallback and refuses.

        This asserts the mechanism at the git level, because that is what makes
        `-u GIT_DIR` load-bearing and the other two merely cheap. If git ever
        changes so that GIT_COMMON_DIR also produces equality, this fails and the
        comment in worktree_detect.sh needs rewriting — which is the point.
        """
        def resolve(env_extra):
            env = dict(os.environ, **env_extra)
            out = {}
            for flag in ("--git-dir", "--git-common-dir"):
                proc = subprocess.run(["git", "-C", self.wt, "rev-parse", flag],
                                      capture_output=True, env=env,
                                      universal_newlines=True)
                out[flag] = (proc.returncode, proc.stdout.strip())
            return out

        primary_git = os.path.join(self.repo, ".git")

        # GIT_DIR: both succeed, both equal -> resolution says "primary".
        with_git_dir = resolve({"GIT_DIR": primary_git})
        self.assertEqual(with_git_dir["--git-dir"][0], 0)
        self.assertEqual(with_git_dir["--git-common-dir"][0], 0)
        self.assertEqual(with_git_dir["--git-dir"][1],
                         with_git_dir["--git-common-dir"][1],
                         "a stray GIT_DIR must produce EQUAL values — that is "
                         "why -u GIT_DIR is the load-bearing one")

        # GIT_COMMON_DIR: rev-parse fails -> resolution fails -> fallback.
        own = subprocess.run(
            ["git", "-C", self.wt, "rev-parse", "--absolute-git-dir"],
            capture_output=True, universal_newlines=True, check=True).stdout.strip()
        with_common = resolve({"GIT_COMMON_DIR": own})
        self.assertNotEqual(with_common["--git-dir"][0], 0,
                            "a stray GIT_COMMON_DIR is documented as making "
                            "rev-parse FAIL, not as producing equality")


class TestAllowWorktreeInstallEscapeHatch(WorktreeFixtureTestCase):

    def test_env_var_gets_past_the_refusal(self):
        proc = run_install(self.wt, self.home,
                           extra_env={"ALLOW_WORKTREE_INSTALL": "1"})

        # It may still fail later for unrelated reasons (this fixture repo has no
        # lib/setup_config.py or config.example.json) — only the refusal itself
        # must be gone, replaced by the documented warning.
        self.assertNotIn(REFUSAL, proc.stderr)
        self.assertIn("proceeding only because", proc.stdout)
        self.assertIn("ALLOW_WORKTREE_INSTALL=1", proc.stdout)

    def test_any_other_value_does_not_open_the_hatch(self):
        # The check is `!= "1"`, so "true"/"yes"/"0" must all still refuse. A
        # loosened comparison here would open the hatch by accident.
        for value in ("true", "yes", "0", ""):
            with self.subTest(value=value):
                proc = run_install(self.wt, self.home,
                                   extra_env={"ALLOW_WORKTREE_INSTALL": value})
                self.assertIn(REFUSAL, proc.stderr)


class TestCheckFromLinkedWorktree(WorktreeFixtureTestCase):
    """--check is exempt by design: it only reports, it changes nothing, and
    running it from a worktree is the documented way to run the suite there."""

    def test_check_exits_0_with_no_refusal(self):
        proc = run_install(self.wt, self.home, args=["--check"])

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn(REFUSAL, proc.stderr)
        self.assertNotIn("refusing to install", proc.stdout)


class TestNonGitDirectoryProceeds(FixtureTestCase):
    """A directory that is not a git repository at all — e.g. someone unzipped a
    tarball of the repo. `rev-parse` failing must never be read as "is a
    worktree"; the install must proceed exactly as it would from any ordinary
    checkout. This is the one place the guard deliberately fails OPEN, because
    with no repository there is no worktree to be linked."""

    def setUp(self):
        super(TestNonGitDirectoryProceeds, self).setUp()
        self.plain = os.path.join(self.root, "plain")
        os.makedirs(self.plain)
        os.symlink(INSTALL_SH, os.path.join(self.plain, "install.sh"))
        os.makedirs(os.path.join(self.plain, "lib"))
        os.symlink(WORKTREE_DETECT_SH,
                   os.path.join(self.plain, "lib", "worktree_detect.sh"))
        # Sanity-check the fixture's own premise: this directory (and its
        # ancestors, as far as git is concerned) must not already be inside some
        # OTHER git repository, or `git -C` would find that enclosing repo
        # instead and the test would prove nothing.
        not_a_repo = subprocess.run(
            ["git", "-C", self.plain, "rev-parse", "--git-dir"],
            capture_output=True, universal_newlines=True)
        self.assertNotEqual(not_a_repo.returncode, 0,
                            "fixture directory is unexpectedly inside a git "
                            "repository — this test's premise does not hold "
                            "in this environment")

    def test_proceeds_past_the_guard(self):
        proc = run_install(self.plain, self.home)

        self.assertNotIn(REFUSAL, proc.stderr)
        self.assertIn(PAST_THE_GUARD, proc.stdout)


class TestPrimaryCheckoutProceeds(FixtureTestCase):
    """The primary (non-worktree) checkout itself: `--git-dir` and
    `--git-common-dir` name the same path, so the guard must not fire."""

    def setUp(self):
        super(TestPrimaryCheckoutProceeds, self).setUp()
        self.repo = make_fixture_repo(self.root)

    def test_proceeds_past_the_guard(self):
        proc = run_install(self.repo, self.home)

        self.assertNotIn(REFUSAL, proc.stderr)
        self.assertIn(PAST_THE_GUARD, proc.stdout)


class TestRelativeCommonDirDoesNotFalselyRefuse(FixtureTestCase):
    """Regression test for the absolute-path resolution itself.

    Deleting the `cd ... && pwd -P` step and comparing the raw `--git-dir` /
    `--git-common-dir` strings still passes every OTHER test in this file,
    because in every other fixture the two raw values are either both
    relative-and-identical (a plain repo invoked at its own root) or both
    already absolute (a linked worktree, whose `.git` file stores an absolute
    `gitdir:` target). Only when `is_linked_worktree` is invoked somewhere git
    has to discover the repository by walking UP the tree do the two raw strings
    differ syntactically while still naming the same directory — which is why
    the nested fixture exists.
    """

    def setUp(self):
        super(TestRelativeCommonDirDoesNotFalselyRefuse, self).setUp()
        self.nested = make_fixture_repo_nested_install(self.root)

    def test_ordinary_nested_repo_is_not_refused(self):
        proc = run_install(self.nested, self.home)

        self.assertNotIn(REFUSAL, proc.stderr,
                         "raw-string comparison would see an absolute "
                         "--git-dir and a relative --git-common-dir as "
                         "'different' and wrongly refuse an ordinary repo")
        self.assertIn(PAST_THE_GUARD, proc.stdout)


class TestDanglingWorktreeIsStillRefused(WorktreeFixtureTestCase):
    """A linked worktree whose admin directory is GONE — the primary was moved,
    renamed, re-cloned, or `.git/worktrees/<name>` was deleted.

    `rev-parse` fails here, and reading that as "not a worktree" is the worst
    possible answer: this is the most doomed target there is. Measured before the
    fallback existed — install.sh exited 0, reported success, and pointed
    ~/.local/bin/claude-danger-guard into the dead worktree.

    A REGRESSION test in the strict sense: the retired path pattern refused this
    case (it only read the path, which is unchanged), so the git-based rewrite
    briefly made a BROKEN repo more installable than a healthy one. The `.git`
    FILE still names a path under `worktrees/`, which is what the fallback reads.
    """

    def _assert_rev_parse_now_fails(self):
        probe = subprocess.run(["git", "-C", self.wt, "rev-parse", "--git-dir"],
                               capture_output=True, universal_newlines=True)
        self.assertNotEqual(probe.returncode, 0,
                            "fixture premise: rev-parse must fail, or this test "
                            "passes via the ordinary comparison path")

    def test_refuses_when_the_admin_dir_is_deleted(self):
        admin = os.path.join(self.repo, ".git", "worktrees")
        self.assertTrue(os.path.isdir(admin), "fixture premise: admin dir exists")
        shutil.rmtree(admin)
        self._assert_rev_parse_now_fails()

        proc = run_install(self.wt, self.home)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(REFUSAL, proc.stderr)
        self.assertEqual(os.listdir(self.home), [],
                         "something was written to HOME before the guard fired")

    def test_refuses_when_the_primary_clone_has_moved(self):
        moved = self.repo + "-moved"
        shutil.move(self.repo, moved)
        self.addCleanup(shutil.rmtree, moved, ignore_errors=True)
        self._assert_rev_parse_now_fails()

        proc = run_install(self.wt, self.home)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn(REFUSAL, proc.stderr)

    def test_refuses_even_with_git_unavailable_entirely(self):
        stub_dir = os.path.join(self.root, "stubbin")
        os.makedirs(stub_dir)
        stub = os.path.join(stub_dir, "git")
        with open(stub, "w") as handle:
            handle.write("#!/bin/sh\nexit 127\n")
        os.chmod(stub, 0o755)

        proc = run_install(self.wt, self.home,
                           extra_env={"PATH": stub_dir + os.pathsep
                                      + os.environ.get("PATH", "")})

        self.assertIn(REFUSAL, proc.stderr)


class TestLegitimateGitFileLayoutsStillProceed(FixtureTestCase):
    """The fallback reads a `.git` FILE, and two legitimate layouts also use one.
    Neither may be refused, or the fallback has traded a silent failure for a
    loud one that blocks real installs."""

    def test_a_submodule_checkout_proceeds(self):
        parent = make_fixture_repo(self.root, name="parent")
        child = make_fixture_repo(self.root, name="child")
        git("-c", "protocol.file.allow=always", "submodule", "add", "-q",
            child, "sub", cwd=parent)
        sub = os.path.join(parent, "sub")
        self.assertTrue(os.path.isfile(os.path.join(sub, ".git")),
                        "fixture premise: a submodule uses a .git FILE")
        with open(os.path.join(sub, ".git")) as handle:
            self.assertNotIn("/worktrees/", handle.read())

        proc = run_install(sub, self.home)
        self.assertNotIn(REFUSAL, proc.stderr)
        self.assertIn(PAST_THE_GUARD, proc.stdout)

    def test_a_separate_git_dir_checkout_proceeds(self):
        work = os.path.join(self.root, "sgd")
        gitdir = os.path.join(self.root, "sgd-gitdir")
        os.makedirs(os.path.join(work, "claude"))
        os.makedirs(os.path.join(work, "tools"))
        os.makedirs(os.path.join(work, "lib"))
        os.symlink(INSTALL_SH, os.path.join(work, "install.sh"))
        os.symlink(WORKTREE_DETECT_SH,
                   os.path.join(work, "lib", "worktree_detect.sh"))
        for name, body in (("permissions.baseline.json", '{"permissions": {}}\n'),):
            with open(os.path.join(work, "claude", name), "w") as handle:
                handle.write(body)
        fx.add_plugin_layout(work)
        git("init", "-q", "-b", "main", "--separate-git-dir", gitdir, work,
            cwd=self.root)
        self.assertTrue(os.path.isfile(os.path.join(work, ".git")),
                        "fixture premise: --separate-git-dir uses a .git FILE")

        proc = run_install(work, self.home)
        self.assertNotIn(REFUSAL, proc.stderr)
        self.assertIn(PAST_THE_GUARD, proc.stdout)


class TestMissingDetectHelperFailsClosed(WorktreeFixtureTestCase):
    """The guard is sourced from lib/worktree_detect.sh. If that file is gone the
    install must STOP, not proceed unguarded — a guard whose implementation has
    vanished is the one case where an install must never be optimistic."""

    def test_absent_helper_refuses_and_writes_nothing(self):
        os.unlink(os.path.join(self.repo, "lib", "worktree_detect.sh"))

        proc = run_install(self.repo, self.home)

        # Fails closed either way: `set -e` on the failing `source` would also
        # abort. That is why this asserts the EXPLICIT message rather than just
        # the filename — mutation-verified, disabling the check left a green
        # suite because bash's own "No such file or directory" names the path
        # too. The explicit refusal is what tells a human what to do about it.
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("refusing to install", proc.stderr)
        self.assertIn("worktree_detect.sh", proc.stderr)
        self.assertEqual(os.listdir(self.home), [],
                         "an unguarded install wrote to HOME")


class TestSetupGitSharesTheSameGuard(WorktreeFixtureTestCase):
    """setup-git.sh writes a GLOBAL core.hooksPath pre-commit symlink, so it must
    refuse a linked worktree for the same reason install.sh does — and it kept
    matching the retired path pattern after install.sh stopped, which is the
    duplication lib/worktree_detect.sh removes.

    A dangling symlink there is silent in the worst way: git skips a hook it
    cannot execute, and core.hooksPath REPLACES per-repo hooks, so secret
    scanning AND every existing pre-commit hook on the machine go off at once.
    """

    def _run_setup_git(self, cwd, args=()):
        env = dict(os.environ)
        env["HOME"] = self.home
        # Sandbox the global gitconfig too — this script writes to it.
        env["GIT_CONFIG_GLOBAL"] = os.path.join(self.home, ".gitconfig")
        return subprocess.run([os.path.join(cwd, "scripts", "setup-git.sh")]
                              + list(args),
                              cwd=cwd, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)

    def _install_setup_git(self):
        for base in (self.repo, self.wt):
            scripts = os.path.join(base, "scripts")
            if not os.path.isdir(scripts):
                os.makedirs(scripts)
            target = os.path.join(scripts, "setup-git.sh")
            if not os.path.exists(target):
                os.symlink(SETUP_GIT_SH, target)

    def test_refuses_a_linked_worktree_off_the_convention_path(self):
        self._install_setup_git()
        self.assertNotIn("/.claude/worktrees/", self.wt,
                         "fixture must not sit on the retired pattern's path")

        proc = self._run_setup_git(self.wt)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("linked git worktree", proc.stdout + proc.stderr)
        gitconfig = os.path.join(self.home, ".gitconfig")
        if os.path.exists(gitconfig):
            with open(gitconfig) as handle:
                self.assertNotIn("hooksPath", handle.read())

    def test_primary_checkout_is_not_refused(self):
        # NOT --check. `(( ! CHECK )) && is_linked_worktree ...` short-circuits,
        # so under --check the detection is never called and this asserted
        # nothing — mutation-verified: making is_linked_worktree always refuse
        # failed other tests and not this one. A false refusal here blocks onboarding,
        # since README and commands/onboard.md both tell people to run this
        # script by hand, so the false-refusal direction is the one that needs
        # covering. A real apply is safe: HOME and GIT_CONFIG_GLOBAL are both
        # sandboxed by _run_setup_git.
        self._install_setup_git()
        proc = self._run_setup_git(self.repo)
        self.assertNotIn("linked git worktree", proc.stdout + proc.stderr)

    def test_refuses_when_the_shared_helper_is_unreadable(self):
        # Asserts the EXPLICIT refusal, not just the filename. `set -e` on the
        # failing `source` would abort anyway and bash's own "No such file or
        # directory" names the path, so a filename-only assertion passes with the
        # check deleted — measured. This is the third time that exact trap has
        # appeared in this suite; the first two were in install.sh's equivalent
        # and in the GIT_COMMON_DIR test.
        self._install_setup_git()
        os.unlink(os.path.join(self.wt, "lib", "worktree_detect.sh"))

        proc = self._run_setup_git(self.wt)

        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("refusing to install git hooks", proc.stdout + proc.stderr)

    def test_the_guard_runs_before_anything_global_is_written(self):
        # The refusal used to sit AFTER init.defaultBranch, pull.rebase,
        # core.excludesfile and the ~/.gitignore_global append, so "refuses
        # outright" was not true of it. A guard that fires after its first side
        # effect is not a guard.
        self._install_setup_git()
        proc = self._run_setup_git(self.wt)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)

        gitconfig = os.path.join(self.home, ".gitconfig")
        if os.path.exists(gitconfig):
            with open(gitconfig) as handle:
                written = handle.read()
            for key in ("defaultBranch", "rebase", "excludesfile", "hooksPath"):
                self.assertNotIn(key, written,
                                 "%s was written before the guard refused" % key)


class TestResolutionStepIsIndependentlyCorrect(FixtureTestCase):
    """`resolve_git_dirs` on its own, not through a verdict.

    This class exists because of a defect it would have caught immediately. A
    `CDPATH` hardening pass changed the resolution to `cd -- "./$git_dir"`, which
    is fine for a relative value and broken for an absolute one — and a linked
    worktree's `--git-dir` IS absolute. Resolution failed silently for every real
    worktree, the `.git`-file fallback picked up the whole decision, and all 18
    tests stayed green because both paths refuse a worktree.

    Two mechanisms with one observable outcome hide each other's failures. So the
    resolution is asserted here directly: real absolute paths out, differing for
    a worktree and identical for a primary checkout.
    """

    def _resolve(self, target):
        # resolve_git_dirs sets RESOLVED_GIT_DIR / RESOLVED_COMMON_DIR rather
        # than printing them — a two-line stdout protocol truncates on a path
        # containing a newline. Echo them here so the test can still assert the
        # resolution directly rather than inferring it from a verdict.
        script = (
            '. "%s"\n'
            'if resolve_git_dirs "%s"; then\n'
            '  printf "%%s\\n%%s\\n" "$RESOLVED_GIT_DIR" "$RESOLVED_COMMON_DIR"\n'
            'else\n'
            '  exit 1\n'
            'fi\n' % (WORKTREE_DETECT_SH, target))
        proc = subprocess.run(["bash", "-c", script], capture_output=True,
                              universal_newlines=True)
        return proc.returncode, proc.stdout.splitlines()

    def test_a_worktree_resolves_to_two_different_absolute_paths(self):
        repo = make_fixture_repo(self.root)
        wt = add_worktree(repo, self.root)

        rc, lines = self._resolve(wt)
        self.assertEqual(rc, 0, "resolution failed outright")
        self.assertEqual(len(lines), 2, lines)
        for line in lines:
            self.assertTrue(line.startswith("/"),
                            "not an absolute path: %r" % line)
            self.assertTrue(os.path.isdir(line),
                            "resolved to something that is not a directory: %r"
                            % line)
        self.assertNotEqual(lines[0], lines[1],
                            "a linked worktree's git-dir and common-dir must "
                            "differ — this is the whole mechanism")

    def test_a_primary_checkout_resolves_to_one_directory_twice(self):
        repo = make_fixture_repo(self.root, name="primary-only")

        rc, lines = self._resolve(repo)
        self.assertEqual(rc, 0, "resolution failed outright")
        self.assertEqual(len(lines), 2, lines)
        self.assertEqual(lines[0], lines[1],
                         "a primary checkout's two dirs must resolve equal, or "
                         "every ordinary install is refused")

    def test_a_nested_checkout_resolves_equal_despite_mixed_relativity(self):
        # The case where git returns one absolute and one relative value. Raw
        # string comparison would call these different and refuse a legitimate
        # repo; resolution must make them equal.
        nested = make_fixture_repo_nested_install(self.root)

        rc, lines = self._resolve(nested)
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], lines[1])

    def test_an_unusable_argument_reports_failure(self):
        # The resolver validates its own input, and that check is invisible
        # through is_linked_worktree because the verdict layer rejects the same
        # arguments first, so removing it fails no other test. resolve_git_dirs
        # is public in a shared library and this suite calls it directly, so it
        # gets its own assertion rather than inheriting one.
        for arg, label in (("", "empty"),
                           (os.path.join(self.root, "nope"), "nonexistent")):
            with self.subTest(arg=label):
                rc, lines = self._resolve(arg)
                self.assertNotEqual(rc, 0,
                                    "an %s argument resolved successfully" % label)
                self.assertEqual(lines, [])

    def test_a_non_repo_reports_failure_rather_than_empty_success(self):
        plain = os.path.join(self.root, "notarepo")
        os.makedirs(plain)
        rc, lines = self._resolve(plain)
        self.assertNotEqual(rc, 0)
        self.assertEqual(lines, [])


class TestBrokenGitFileLayoutsAreNotMistakenForWorktrees(FixtureTestCase):
    """The fallback's `/worktrees/` test, exercised on the only inputs that reach
    it.

    A healthy submodule or `--separate-git-dir` checkout never gets that far:
    resolution succeeds and returns a verdict first. So loosening the fallback to
    match any `.git` file leaves the whole suite green — measured. The narrowness
    only matters once rev-parse ALSO fails, which is what these fixtures build.
    Without them, `*/worktrees/*` could be widened to `-n $gitdir_target` and
    nothing would notice until it refused a real install.
    """

    def _break_and_run(self, work, expect_refusal):
        probe = subprocess.run(["git", "-C", work, "rev-parse", "--git-dir"],
                               capture_output=True, universal_newlines=True)
        self.assertNotEqual(probe.returncode, 0,
                            "fixture premise: rev-parse must fail so the "
                            "fallback is what decides")
        proc = run_install(work, self.home)
        if expect_refusal:
            self.assertIn(REFUSAL, proc.stderr)
        else:
            self.assertNotIn(REFUSAL, proc.stderr)
            self.assertIn(PAST_THE_GUARD, proc.stdout)

    def test_a_broken_submodule_still_proceeds(self):
        parent = make_fixture_repo(self.root, name="parent")
        child = make_fixture_repo(self.root, name="child")
        git("-c", "protocol.file.allow=always", "submodule", "add", "-q",
            child, "sub", cwd=parent)
        sub = os.path.join(parent, "sub")
        shutil.rmtree(os.path.join(parent, ".git", "modules"))
        self._break_and_run(sub, expect_refusal=False)

    def test_a_broken_separate_git_dir_still_proceeds(self):
        work = os.path.join(self.root, "sgd")
        gitdir = os.path.join(self.root, "sgd-gitdir")
        os.makedirs(os.path.join(work, "claude"))
        os.makedirs(os.path.join(work, "tools"))
        os.makedirs(os.path.join(work, "lib"))
        os.symlink(INSTALL_SH, os.path.join(work, "install.sh"))
        os.symlink(WORKTREE_DETECT_SH,
                   os.path.join(work, "lib", "worktree_detect.sh"))
        for name, body in (("permissions.baseline.json", '{"permissions": {}}\n'),):
            with open(os.path.join(work, "claude", name), "w") as handle:
                handle.write(body)
        fx.add_plugin_layout(work)
        git("init", "-q", "-b", "main", "--separate-git-dir", gitdir, work,
            cwd=self.root)
        shutil.rmtree(gitdir)
        self._break_and_run(work, expect_refusal=False)


def seed_config(home):
    """Write a valid `config.json` into the sandbox HOME so a real apply skips
    interactive first-time setup entirely.

    `make_fixture_repo` deliberately omits `lib/setup_config.py` and
    `config.example.json` (see its docstring), so without a pre-existing
    config a real apply against a sandbox HOME dies inside first-time setup —
    `python3 "$REPO/lib/setup_config.py"` fails with "No such file" (exit 2),
    install.sh's rc==2 branch then tries to `cp "$REPO/config.example.json"`,
    that file is also absent, and `set -e` aborts the whole script before it
    ever reaches the "/build workflow scripts" section this file tests.
    Pre-seeding the config short-circuits `[[ ! -f "$CONFIG" ]]` so first-time
    setup never runs at all."""
    config_dir = os.path.join(home, ".config", "claude-code-toolkit")
    os.makedirs(config_dir, exist_ok=True)
    with open(os.path.join(config_dir, "config.json"), "w") as handle:
        handle.write('{"workspace_root": "%s"}\n' % os.path.join(home, "workspace"))


def seed_build_route(repo):
    """`make_fixture_repo` does not include `tools/build-route/claude-build-route`
    — the minimal fixture only creates the `tools/` directory, not its
    contents. The `link` call for the `--review-mode` CLI
    (install.sh, the `~/.local/bin` block) needs a real file at that path or
    it prints "missing in repo, skipped" and creates nothing, so the fixture
    repo gets its own stub copy here."""
    build_route = os.path.join(repo, "tools", "build-route")
    os.makedirs(build_route, exist_ok=True)
    with open(os.path.join(build_route, "claude-build-route"), "w") as handle:
        handle.write("#!/usr/bin/env python3\n"
                     "# stub claude-build-route for install.sh fixture tests\n")


class TestClaudeBuildRouteSymlinkCreatedByRealApply(FixtureTestCase):
    """install.sh's `~/.local/bin` block: the `link` call for
    `claude-build-route`, which `/build` and the lenses run as
    `claude-build-route --review-mode`. A real apply must create the symlink in `~/.local/bin/` and
    it must resolve to the actual file in the repo — not merely exist as
    some symlink."""

    def setUp(self):
        super(TestClaudeBuildRouteSymlinkCreatedByRealApply, self).setUp()
        self.repo = make_fixture_repo(self.root)
        seed_build_route(self.repo)
        seed_config(self.home)

    def test_the_symlink_is_created_and_resolves_into_the_repo(self):
        proc = run_install(self.repo, self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        link_path = os.path.join(self.home, ".local", "bin", "claude-build-route")
        self.assertTrue(os.path.islink(link_path),
                        "claude-build-route was not created as a symlink")
        expected_target = os.path.join(self.repo, "tools", "build-route",
                                       "claude-build-route")
        self.assertEqual(os.path.realpath(link_path),
                         os.path.realpath(expected_target),
                         "claude-build-route does not resolve to the repo's copy")


def seed_merge_brief(repo):
    """Stub `tools/merge-brief/claude-merge-brief` into the minimal fixture
    repo, same pattern as `seed_build_route`: without a real file the `link`
    call prints "missing in repo, skipped" and creates nothing."""
    merge_brief = os.path.join(repo, "tools", "merge-brief")
    os.makedirs(merge_brief, exist_ok=True)
    with open(os.path.join(merge_brief, "claude-merge-brief"), "w") as handle:
        handle.write("#!/usr/bin/env python3\n"
                     "# stub claude-merge-brief for install.sh fixture tests\n")


class TestClaudeMergeBriefSymlinkCreatedByRealApply(FixtureTestCase):
    """install.sh's `~/.local/bin` block: the `link` call for
    `claude-merge-brief`, which builds every /build review gate's brief. A
    real apply must create the symlink and it must resolve to the repo's
    copy. Modelled on `TestClaudeBuildRouteSymlinkCreatedByRealApply`."""

    def setUp(self):
        super(TestClaudeMergeBriefSymlinkCreatedByRealApply, self).setUp()
        self.repo = make_fixture_repo(self.root)
        seed_merge_brief(self.repo)
        seed_config(self.home)

    def test_the_symlink_is_created_and_resolves_into_the_repo(self):
        proc = run_install(self.repo, self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        link_path = os.path.join(self.home, ".local", "bin", "claude-merge-brief")
        self.assertTrue(os.path.islink(link_path),
                        "claude-merge-brief was not created as a symlink")
        expected_target = os.path.join(self.repo, "tools", "merge-brief",
                                       "claude-merge-brief")
        self.assertEqual(os.path.realpath(link_path),
                         os.path.realpath(expected_target),
                         "claude-merge-brief does not resolve to the repo's copy")


def seed_review_gate(repo):
    """Stub `tools/build-lens/claude-review-gate` into the minimal fixture
    repo, same pattern as `seed_merge_brief`: without a real file the `link`
    call prints "missing in repo, skipped" and creates nothing."""
    build_lens = os.path.join(repo, "tools", "build-lens")
    os.makedirs(build_lens, exist_ok=True)
    with open(os.path.join(build_lens, "claude-review-gate"), "w") as handle:
        handle.write("#!/usr/bin/env python3\n"
                     "# stub claude-review-gate for install.sh fixture tests\n")


class TestClaudeReviewGateSymlinkCreatedByRealApply(FixtureTestCase):
    """install.sh's `~/.local/bin` block: the `link` call for
    `claude-review-gate`, which runs every /build review gate when the
    `Workflow` tool is absent. A real apply must create the symlink and it
    must resolve to the repo's copy. Modelled on
    `TestClaudeMergeBriefSymlinkCreatedByRealApply`."""

    def setUp(self):
        super(TestClaudeReviewGateSymlinkCreatedByRealApply, self).setUp()
        self.repo = make_fixture_repo(self.root)
        seed_review_gate(self.repo)
        seed_config(self.home)

    def test_the_symlink_is_created_and_resolves_into_the_repo(self):
        proc = run_install(self.repo, self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        link_path = os.path.join(self.home, ".local", "bin", "claude-review-gate")
        self.assertTrue(os.path.islink(link_path),
                        "claude-review-gate was not created as a symlink")
        expected_target = os.path.join(self.repo, "tools", "build-lens",
                                       "claude-review-gate")
        self.assertEqual(os.path.realpath(link_path),
                         os.path.realpath(expected_target),
                         "claude-review-gate does not resolve to the repo's copy")


def seed_codex_review(repo):
    """`make_fixture_repo` does not include `tools/codex-review/`
    — the minimal fixture only creates the `tools/` directory, not its
    contents. The `link` call for `claude-codex-review` (install.sh, the
    `~/.local/bin` block) needs a real file at
    that path or it prints "missing in repo, skipped" and creates nothing,
    so the fixture repo gets its own stub copy here — same pattern as
    `seed_build_route`. `review-brief.md` is deliberately NOT seeded: it is
    never linked, so a fixture that skipped it would prove nothing about
    that omission either way."""
    codex_review = os.path.join(repo, "tools", "codex-review")
    os.makedirs(codex_review, exist_ok=True)
    with open(os.path.join(codex_review, "claude-codex-review"), "w") as handle:
        handle.write("#!/usr/bin/env python3\n"
                     "# stub claude-codex-review for install.sh fixture tests\n")


class TestClaudeCodexReviewSymlinkCreatedByRealApply(FixtureTestCase):
    """install.sh's `~/.local/bin` block: the `link` call for
    `claude-codex-review`. A real apply must create the symlink in
    `~/.local/bin/` and it must resolve to the actual file in the repo — not
    merely exist as some symlink. Modelled directly on
    `TestClaudeBuildRouteSymlinkCreatedByRealApply` above, per this repo's
    convention for a newly-linked executable."""

    def setUp(self):
        super(TestClaudeCodexReviewSymlinkCreatedByRealApply, self).setUp()
        self.repo = make_fixture_repo(self.root)
        seed_codex_review(self.repo)
        seed_config(self.home)

    def test_the_symlink_is_created_and_resolves_into_the_repo(self):
        proc = run_install(self.repo, self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        link_path = os.path.join(self.home, ".local", "bin", "claude-codex-review")
        self.assertTrue(os.path.islink(link_path),
                        "claude-codex-review was not created as a symlink")
        expected_target = os.path.join(self.repo, "tools", "codex-review",
                                       "claude-codex-review")
        self.assertEqual(os.path.realpath(link_path),
                         os.path.realpath(expected_target),
                         "claude-codex-review does not resolve to the repo's copy")

    def test_review_brief_is_not_linked(self):
        """The trap this task names explicitly: `review-brief.md` is a data
        file the executable resolves beside itself via `realpath`, not a
        second entry point. Linking it would put a copy on PATH with nothing
        reading it."""
        proc = run_install(self.repo, self.home)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(
            os.path.lexists(os.path.join(self.home, ".local", "bin", "review-brief.md")),
            "review-brief.md must never be linked into ~/.local/bin")


class TestAnUnusableArgumentFailsClosed(FixtureTestCase):
    """`git -C ""` is a NO-OP, not an error, so an empty argument makes every
    git call describe the CURRENT WORKING DIRECTORY instead of the path asked
    about. Measured before the fix: `is_linked_worktree ""` answered "refuse"
    from inside a worktree and "proceed" from a primary checkout — tracking the
    caller's cwd, authorising a write on the strength of a directory it never
    looked at.

    Both real callers derive their path from `$(cd ... && pwd)` under
    `set -euo pipefail`, so an empty value aborts before reaching the guard. That
    makes this branch unreachable *from install.sh and setup-git.sh today* — for
    a reason on an unrelated line in two other files, which nobody editing those
    would know they were preserving. It is reachable here, and this is a shared
    library whose next caller may not have that property.

    Credit where due: a peer session found the same shape in its own guard —
    protected by two unrelated upstream failures, neither about the thing being
    guarded — and the distinction between "unreachable" and "unreachable for
    reasons unrelated to this guard" is theirs.
    """

    def _verdict(self, arg, cwd):
        script = ('. "%s"\n'
                  'if is_linked_worktree "%s"; then echo REFUSE; else echo PROCEED; fi\n'
                  % (WORKTREE_DETECT_SH, arg))
        proc = subprocess.run(["bash", "-c", script], cwd=cwd,
                              capture_output=True, universal_newlines=True)
        return proc.stdout.strip()

    def test_an_empty_argument_refuses_from_any_cwd(self):
        # Both cwds, because the defect was that the answer tracked the cwd. A
        # single-cwd test would have passed against the broken version from
        # inside a worktree.
        plain = os.path.join(self.root, "plain")
        os.makedirs(plain)
        repo = make_fixture_repo(self.root)
        wt = add_worktree(repo, self.root)
        for cwd, label in ((plain, "non-repo"), (repo, "primary"), (wt, "worktree")):
            with self.subTest(cwd=label):
                self.assertEqual(self._verdict("", cwd), "REFUSE",
                                 "an empty argument authorised a write from a "
                                 "%s cwd" % label)

    def test_a_nonexistent_directory_refuses(self):
        missing = os.path.join(self.root, "not-there")
        self.assertFalse(os.path.exists(missing))
        self.assertEqual(self._verdict(missing, self.root), "REFUSE")

    def test_a_real_primary_checkout_is_still_not_refused(self):
        # The negative control: the validation above must not turn into a blanket
        # refusal, which would fail every legitimate install closed.
        repo = make_fixture_repo(self.root, name="control")
        self.assertEqual(self._verdict(repo, self.root), "PROCEED")


if __name__ == "__main__":
    unittest.main(verbosity=1)
