#!/usr/bin/env python3
"""setup-git.sh links every client-side Git hook (except pre-commit) to the
forwarder, so the global core.hooksPath no longer silences a repo's own hooks.

core.hooksPath REPLACES per-repo .git/hooks. pre-commit already chains through
git-pre-commit; every other client-side hook was dead the moment the global path
was set. These tests run the real script against a sandboxed HOME and
GIT_CONFIG_GLOBAL — nothing here may touch the real ~/.gitconfig or ~/.config.

The fixture is a COPY of the files setup-git.sh needs, inside a throwaway git
repo. Not a symlink to scripts/setup-git.sh: REPO_ROOT is computed from the
script's own (unresolved) location, and the forwarder links must point at a real
tools/secret-scan/git-hook-forward under it. The suite itself may run from a
linked worktree, which the script refuses; the fixture is a primary checkout.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
SETUP_GIT_SH = os.path.join(REPO_ROOT, "scripts", "setup-git.sh")

# The 28 client- and server-side hook names githooks(5) documents, written out here on
# purpose: this is the canary that notices setup-git.sh's list drifting.
ALL_HOOKS = frozenset("""
applypatch-msg pre-applypatch post-applypatch pre-commit pre-merge-commit
prepare-commit-msg commit-msg post-commit pre-rebase post-checkout post-merge
pre-push pre-receive update post-receive post-update reference-transaction
push-to-checkout pre-auto-gc post-rewrite sendemail-validate fsmonitor-watchman
post-index-change p4-changelist p4-prepare-changelist p4-post-changelist
p4-pre-submit proc-receive
""".split())
# pre-commit stays git-pre-commit (it already chains); server-side hooks are out
# of scope; the last three are deliberately not forwarded.
EXCLUDED = frozenset("""
pre-commit pre-receive update post-receive post-update
push-to-checkout proc-receive fsmonitor-watchman
""".split())


def parse_forwarded_hooks():
    """FORWARDED_HOOKS as setup-git.sh declares it — parsed, not re-typed."""
    with open(SETUP_GIT_SH) as handle:
        lines = handle.read().splitlines()
    start = next(i for i, line in enumerate(lines)
                 if re.match(r"\s*FORWARDED_HOOKS=\(", line))
    body = []
    first = re.sub(r"^\s*FORWARDED_HOOKS=\(", "", lines[start])
    for line in [first] + lines[start + 1:]:
        line = line.split("#", 1)[0]
        closed = ")" in line
        body.append(line.split(")", 1)[0])
        if closed:
            break
    return "\n".join(body).split()


def clean_env(home):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["GIT_CONFIG_GLOBAL"] = os.path.join(home, ".gitconfig")
    return env


def git(args, cwd, env):
    return subprocess.run(["git"] + args, cwd=cwd, env=env, capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=60,
                          universal_newlines=True)


class SetupGitForwardingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="setup-git-fwd-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.env = clean_env(self.home)
        self.repo = os.path.join(self.tmp, "toolkit")
        os.makedirs(self.repo)
        self.assertEqual(git(["init", "-q"], self.repo, self.env).returncode, 0)
        for rel in ("scripts/setup-git.sh", "lib/worktree_detect.sh",
                    "tools/secret-scan/git-hook-forward",
                    "tools/secret-scan/git-pre-commit",
                    "tools/secret-scan/claude-secret-scan",
                    "tools/secret-scan/secret_lib.py",
                    # The scanner's rule table (one table, shared with the leak gate).
                    "tools/leak-gate/leak_gate_lib.py"):
            dest = os.path.join(self.repo, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(os.path.join(REPO_ROOT, rel), dest)
        self.hooks_dir = os.path.join(self.home, ".config",
                                      "claude-code-toolkit", "git-hooks")
        self.forwarder = os.path.join(self.repo, "tools", "secret-scan",
                                      "git-hook-forward")
        self.pre_commit = os.path.join(self.repo, "tools", "secret-scan",
                                       "git-pre-commit")

    def run_setup(self, *args):
        return subprocess.run([os.path.join(self.repo, "scripts", "setup-git.sh")]
                              + list(args), cwd=self.repo, env=self.env,
                              capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=120, universal_newlines=True)


class TestForwarderLinks(SetupGitForwardingCase):
    def test_apply_links_every_forwarded_hook_and_only_those(self):
        proc = self.run_setup()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        expected = ALL_HOOKS - EXCLUDED
        self.assertEqual(len(expected), 20)
        for name in sorted(expected):
            link = os.path.join(self.hooks_dir, name)
            self.assertTrue(os.path.islink(link), name + " is not linked")
            self.assertEqual(os.path.realpath(link),
                             os.path.realpath(self.forwarder), name)
            self.assertTrue(os.access(link, os.X_OK), name)
        self.assertEqual(
            os.path.realpath(os.path.join(self.hooks_dir, "pre-commit")),
            os.path.realpath(self.pre_commit))
        for name in sorted(EXCLUDED - {"pre-commit"}):
            self.assertFalse(os.path.lexists(os.path.join(self.hooks_dir, name)),
                             name + " must not be forwarded")
        self.assertEqual(sorted(os.listdir(self.hooks_dir)),
                         sorted(expected | {"pre-commit"}))

    def test_check_creates_nothing(self):
        proc = self.run_setup("--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.exists(self.hooks_dir),
                         "--check created the hooks directory")

    def test_foreign_hooks_path_gets_no_forwarders(self):
        foreign = os.path.join(self.tmp, "their-hooks")
        os.makedirs(foreign)
        git(["config", "--global", "core.hooksPath", foreign], self.repo, self.env)

        proc = self.run_setup()

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(os.listdir(foreign), [])
        self.assertFalse(os.path.exists(self.hooks_dir))
        current = git(["config", "--global", "--get", "core.hooksPath"],
                      self.repo, self.env)
        self.assertEqual(current.stdout.strip(), foreign)


class TestOwnedEntriesOnly(SetupGitForwardingCase):
    """A name in the hooks dir that is not ours is the user's, and stays."""

    def _seed(self):
        os.makedirs(self.hooks_dir)

    def test_a_regular_file_is_kept_byte_identical_and_named_in_a_warning(self):
        self._seed()
        own = os.path.join(self.hooks_dir, "pre-push")
        body = b"#!/bin/sh\n# my own global policy\nexit 0\n"
        with open(own, "wb") as handle:
            handle.write(body)
        os.chmod(own, 0o755)

        proc = self.run_setup()

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(os.path.islink(own), "pre-push was replaced by a link")
        with open(own, "rb") as handle:
            self.assertEqual(handle.read(), body)
        warnings = [line for line in proc.stdout.splitlines() if "kept your own" in line]
        self.assertEqual(len(warnings), 1, proc.stdout)
        self.assertIn("pre-push", warnings[0])
        self.assertIn("~/.config/claude-code-toolkit/git-hooks", warnings[0])
        self.assertNotIn("forwarded hooks resolve to an executable", proc.stdout)
        for name in sorted((ALL_HOOKS - EXCLUDED) - {"pre-push"}):
            link = os.path.join(self.hooks_dir, name)
            self.assertTrue(os.path.islink(link), name)
            self.assertEqual(os.path.realpath(link),
                             os.path.realpath(self.forwarder), name)

    def test_a_symlink_to_another_target_is_left_pointing_there(self):
        self._seed()
        other = os.path.join(self.tmp, "their-commit-msg")
        with open(other, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(other, 0o755)
        link = os.path.join(self.hooks_dir, "commit-msg")
        os.symlink(other, link)

        proc = self.run_setup()

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.readlink(link), other)
        self.assertIn("commit-msg", [w for w in proc.stdout.splitlines()
                                     if "kept your own" in w][0])

    def test_a_dangling_link_from_an_old_checkout_is_relinked(self):
        self._seed()
        link = os.path.join(self.hooks_dir, "post-merge")
        os.symlink("/nonexistent/old-checkout/tools/secret-scan/git-hook-forward", link)

        proc = self.run_setup()

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(os.path.islink(link))
        self.assertEqual(os.path.realpath(link), os.path.realpath(self.forwarder))
        self.assertNotIn("kept your own", proc.stdout)

    def test_a_second_run_on_an_all_ours_dir_is_ok_with_no_warning(self):
        self.assertEqual(self.run_setup().returncode, 0)

        proc = self.run_setup()

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("20 forwarded hooks resolve to an executable", proc.stdout)
        self.assertNotIn("kept your own", proc.stdout)

    def test_check_reports_a_foreign_name_and_writes_nothing(self):
        self._seed()
        own = os.path.join(self.hooks_dir, "pre-push")
        with open(own, "w") as handle:
            handle.write("mine\n")

        proc = self.run_setup("--check")

        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("kept your own pre-push", proc.stdout)
        self.assertEqual(os.listdir(self.hooks_dir), ["pre-push"])


class TestPreCommitStatusIsHonest(SetupGitForwardingCase):
    """H4 F5: --check said `OK ... (pre-commit installed)` for ANY executable
    pre-commit, an old fork's included, while the apply re-links it and
    lib/secret_scan_status.py calls the same state not active."""

    def seed_foreign_pre_commit(self, as_link):
        os.makedirs(self.hooks_dir)
        old_fork = os.path.join(self.tmp, "old-fork-pre-commit")
        with open(old_fork, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(old_fork, 0o755)
        hook = os.path.join(self.hooks_dir, "pre-commit")
        if as_link:
            os.symlink(old_fork, hook)
        else:
            shutil.copy2(old_fork, hook)
        git(["config", "--global", "core.hooksPath", self.hooks_dir], self.home, self.env)
        return old_fork

    def assert_named_foreign_then_relinked(self, check_out, detail):
        self.assertNotIn("(pre-commit installed)", check_out)
        self.assertIn("NOT this checkout's scanner", check_out)
        self.assertIn(detail, check_out)
        self.assertIn("re-link pre-commit to", check_out)
        applied = self.run_setup()
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertEqual(os.path.realpath(os.path.join(self.hooks_dir, "pre-commit")),
                         os.path.realpath(self.pre_commit))
        again = self.run_setup("--check")
        self.assertIn("(pre-commit installed)", again.stdout)

    def test_a_link_to_another_checkouts_hook_is_named_not_ok(self):
        old_fork = self.seed_foreign_pre_commit(as_link=True)
        proc = self.run_setup("--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(os.readlink(os.path.join(self.hooks_dir, "pre-commit")), old_fork,
                         "--check changed the hook")
        self.assert_named_foreign_then_relinked(proc.stdout, "-> " + old_fork)

    def test_a_regular_executable_file_is_named_not_ok(self):
        self.seed_foreign_pre_commit(as_link=False)
        proc = self.run_setup("--check")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assert_named_foreign_then_relinked(proc.stdout, "(a regular file, not a link)")

    def test_an_unresolvable_path_is_never_reported_installed(self):
        """Codex Merge Review: two failed resolutions are two empty strings,
        which compared equal and printed "installed"."""
        self.assertEqual(self.run_setup().returncode, 0)
        shim = os.path.join(self.tmp, "shim")
        os.makedirs(shim)
        fake = os.path.join(shim, "python3")
        with open(fake, "w") as handle:
            handle.write("#!/bin/sh\nexit 1\n")
        os.chmod(fake, 0o755)
        env = dict(self.env, PATH=shim + os.pathsep + self.env.get("PATH", ""))
        proc = subprocess.run([os.path.join(self.repo, "scripts", "setup-git.sh"), "--check"],
                              cwd=self.repo, env=env, capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("(pre-commit installed)", proc.stdout)
        self.assertIn("could not be checked", proc.stdout)

    def test_this_checkouts_hook_is_ok(self):
        """The control: --check can still say installed."""
        self.assertEqual(self.run_setup().returncode, 0)
        proc = self.run_setup("--check")
        self.assertIn("(pre-commit installed)", proc.stdout)
        self.assertNotIn("NOT this checkout's scanner", proc.stdout)


class TestForwardedListDrift(unittest.TestCase):
    def test_forwarded_plus_excluded_is_exactly_the_28(self):
        forwarded = parse_forwarded_hooks()
        self.assertEqual(len(forwarded), len(set(forwarded)), "duplicate name")
        self.assertEqual(len(ALL_HOOKS), 28)
        self.assertEqual(len(EXCLUDED), 8)
        self.assertEqual(set(forwarded) & EXCLUDED, set(), "overlap")
        self.assertEqual(set(forwarded) | EXCLUDED, set(ALL_HOOKS))


class TestEndToEnd(SetupGitForwardingCase):
    def test_a_commit_runs_the_repos_own_hook_under_the_global_path(self):
        proc = self.run_setup()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        work = os.path.join(self.tmp, "work")
        os.makedirs(work)
        for args in (["init", "-q"], ["config", "user.name", "T"],
                     ["config", "user.email", "t@example.com"]):
            self.assertEqual(git(args, work, self.env).returncode, 0)
        proof = os.path.join(self.tmp, "proof.txt")
        own = os.path.join(work, ".git", "hooks", "commit-msg")
        with open(own, "w") as handle:
            handle.write('#!/bin/sh\ncp "$1" "%s"\nexit 0\n' % proof)
        os.chmod(own, 0o755)
        with open(os.path.join(work, "a.txt"), "w") as handle:
            handle.write("hello\n")
        git(["add", "a.txt"], work, self.env)

        commit = git(["commit", "-q", "-m", "own hook ran"], work, self.env)

        self.assertEqual(commit.returncode, 0, commit.stdout + commit.stderr)
        self.assertTrue(os.path.exists(proof), "the repo's own commit-msg did not run")
        with open(proof) as handle:
            self.assertIn("own hook ran", handle.read())


STATUS = os.path.join(REPO_ROOT, "lib", "secret_scan_status.py")


class TestSecretScanStatus(SetupGitForwardingCase):
    """Row 74: lib/secret_scan_status.py is the one answer to "is commit secret
    scanning on?", read by install.sh's report and /onboard's verify. It says
    `active` only after the real setup-git.sh ran for THIS checkout."""

    def status(self):
        done = subprocess.run(["python3", STATUS, self.repo], env=self.env,
                              capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=60, universal_newlines=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout.strip()

    def test_unset_before_setup_git_runs(self):
        self.assertEqual(self.status(), "unset")

    def test_active_after_setup_git_runs(self):
        done = self.run_setup()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.status(), "active")

    def test_stale_when_the_pre_commit_is_gone(self):
        self.assertEqual(self.run_setup().returncode, 0)
        os.unlink(os.path.join(self.hooks_dir, "pre-commit"))
        self.assertEqual(self.status(), "stale")

    def test_foreign_when_the_hooks_path_is_someone_elses(self):
        theirs = os.path.join(self.home, "my-hooks")
        os.makedirs(theirs)
        git(["config", "--global", "core.hooksPath", theirs], self.home, self.env)
        self.assertEqual(self.status(), "foreign")

    def test_install_sh_reports_from_this_probe(self):
        with open(os.path.join(REPO_ROOT, "install.sh"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn('python3 "$REPO/lib/secret_scan_status.py" "$REPO"', text)
        self.assertNotIn('print("stale")', text, "a second, inline copy of the probe")


if __name__ == "__main__":
    unittest.main()
