#!/usr/bin/env python3
"""Tests for `snapshot_lib.take_snapshot` and the `snapshot` CLI subcommand.

Every sandbox is a fresh, empty `tempfile.mkdtemp()` -- nothing here
touches the real machine. `fixtures.make_fixture_repo` builds a throwaway git
repo, symlinked into the real toolkit at every path a run actually needs, so a
real `install.sh` / `scripts/setup-git.sh` can be driven through `subprocess`
without ever touching this worktree's own `$HOME` or the real one.

`CLAUDE_TOOLKIT_CONFIG` is forced to a guaranteed-nonexistent path below,
before any test runs -- same reasoning as the other setup-audit suites.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

sys.path.insert(0, HERE)
sys.path.insert(0, TOOL_DIR)

os.environ["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(
    tempfile.gettempdir(), "setup-audit-tests-hermetic-nonexistent.json")
# take_snapshot reads these from the process environment (the same values
# setup-git.sh honours). Ambient values point it at the RUNNER's git config
# instead of the sandbox's; GitHub runners set XDG_CONFIG_HOME. Tests that
# exercise them set them explicitly with mock.patch.dict.
for _name in ("XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL"):
    os.environ.pop(_name, None)

import fixtures  # noqa: E402  (first: it sets up the import path)
import audit_lib  # noqa: E402
import snapshot_lib  # noqa: E402


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _tree_state(home):
    """Every path under `home`, recursively: {absolute path: (kind, data)},
    `kind` one of "symlink" (data = the `readlink` string), "file" (data =
    sha256) or "dir" (data = None). This is the test's OWN independent
    tree-walker, used only to know what changed -- never `snapshot_lib`'s
    internals."""
    state = {}
    for base, dirs, files in os.walk(home, followlinks=False):
        for name in dirs:
            full = os.path.join(base, name)
            if os.path.islink(full):
                state[full] = ("symlink", os.readlink(full))
            else:
                state[full] = ("dir", None)
        for name in files:
            full = os.path.join(base, name)
            if os.path.islink(full):
                state[full] = ("symlink", os.readlink(full))
            else:
                state[full] = ("file", _sha256(full))
    return state


def _changed_or_removed(before, after):
    """Every path present `before` whose state differs (or is gone) `after`
    -- never a newly-added path, which this test does not care about."""
    return {path for path, state in before.items()
            if after.get(path) != state}


def _dir_hash(path):
    """A whole-subtree fingerprint, order-independent per level, used only
    to assert a PAST snapshot's tree is byte-for-byte unchanged."""
    h = hashlib.sha256()
    for base, dirs, files in os.walk(path):
        dirs.sort()
        for name in sorted(files):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, path)
            h.update(rel.encode())
            if os.path.islink(full):
                h.update(b"symlink:" + os.readlink(full).encode())
            else:
                with open(full, "rb") as handle:
                    h.update(handle.read())
    return h.hexdigest()


# `_write_config`/`_run_install` moved to `fixtures.write_config` /
# `fixtures.run_install` so the apply-safe suite, which drives
# the identical real-install pattern, does not carry a second copy.
_write_config = fixtures.write_config
_run_install = fixtures.run_install


def _run_setup_git(fixture, home, extra_env=None):
    proc = subprocess.run(
        ["bash", os.path.join(fixture, "scripts", "setup-git.sh")],
        env=fixtures.env_for(home, extra_env), cwd=fixture, capture_output=True,
        text=True, stdin=subprocess.DEVNULL, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def _find_entry(manifest, path):
    for entry in manifest:
        if entry["path"] == path:
            return entry
    return None


def _find_referent(manifest, path):
    for entry in manifest:
        ref = entry.get("referent")
        if ref and ref["path"] == path:
            return ref
    return None


class SandboxCase(unittest.TestCase):
    """A throwaway, empty HOME (and, where needed, a throwaway fixture repo)
    per test. Nothing here touches the real machine."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def _fixture(self):
        root = tempfile.mkdtemp(prefix="setup-audit-fixture-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return fixtures.make_fixture_repo(root)


class TestWriteSurface(SandboxCase):
    """Every path a real install run changes or removes must have a
    manifest entry recording its BEFORE state -- a changed symlink referent
    counts through its own `referent` record, not a top-level path entry."""

    def test_every_changed_or_removed_path_was_recorded(self):
        home = self._home()
        fixture = self._fixture()

        # ~/.claude/commands/build.md -- a REAL file in a legacy link
        # directory: install.sh removes only its own old links there, so
        # it leaves this alone.
        build_md = os.path.join(home, ".claude", "commands", "build.md")
        fixtures.write(build_md, "not the real build.md\n")

        # ~/.claude/commands/mine.md -- theirs-only, never touched.
        mine_md = os.path.join(home, ".claude", "commands", "mine.md")
        fixtures.write(mine_md, "# mine\n")

        # ~/.claude/settings.json as a SYMLINK to ~/dotfiles/settings.json,
        # holding one custom PreToolUse hook. install.sh's hooks merge opens
        # this path for read AND write, which follows the symlink straight
        # through to the referent -- the symlink's own readlink string never
        # changes, but the referent's bytes do.
        referent = os.path.join(home, "dotfiles", "settings.json")
        fixtures.write(referent, json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [
                {"type": "command", "command": "echo custom"}]}]}}))
        settings_json = os.path.join(home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings_json), exist_ok=True)
        os.symlink(referent, settings_json)

        # ~/.local/bin/git-tidy symlinked to a SANDBOX script -- a real
        # manifest destination pointing elsewhere, so install.sh repoints it.
        sandbox_script = os.path.join(home, "not-the-real-git-tidy")
        fixtures.write(sandbox_script, "#!/bin/sh\necho old\n")
        git_tidy = os.path.join(home, ".local", "bin", "git-tidy")
        os.makedirs(os.path.dirname(git_tidy), exist_ok=True)
        os.symlink(sandbox_script, git_tidy)

        # ~/.local/bin/claude-mine -- theirs-only, real file, never touched.
        claude_mine = os.path.join(home, ".local", "bin", "claude-mine")
        fixtures.write(claude_mine, "#!/bin/sh\necho mine\n")

        # ~/bin/claude-old symlinked into the fixture repo, matching
        # basename -- the OLD ~/bin prune removes it unconditionally,
        # regardless of whether its target exists.
        claude_old = os.path.join(home, "bin", "claude-old")
        os.makedirs(os.path.dirname(claude_old), exist_ok=True)
        os.symlink(os.path.join(fixture, "claude-old"), claude_old)

        # A pre-existing REAL ~/.config/claude-code-toolkit/git-hooks/pre-commit
        # -- scripts/setup-git.sh's `ln -sfn` replaces it with a symlink.
        pre_commit = os.path.join(
            home, ".config", "claude-code-toolkit", "git-hooks", "pre-commit")
        fixtures.write(pre_commit, "#!/bin/sh\necho old pre-commit\n")

        # ~/.gitconfig with [user] only -- setup-git.sh adds keys to it.
        gitconfig = os.path.join(home, ".gitconfig")
        fixtures.write(gitconfig, "[user]\n\tname = Old\n\temail = old@example.com\n")

        # A real config.json, rewritten by `--update` below. `--write` refuses
        # an existing config, so `--update` is the run that changes it.
        ws = os.path.join(home, "workspace")
        os.makedirs(ws, exist_ok=True)
        cfg_path = _write_config(fixture, home, ws)

        # SNAPSHOT_KEEP (10) timed snapshots with old stamps and distinct
        # bytes. This run adds an 11th (settings.json matches none of them)
        # and prunes the oldest -- a removed path the manifest must record.
        timed = []
        for index in range(10):
            path = os.path.join(
                home, ".claude",
                "settings.json.claude-code-toolkit-2025010%dT000000Z.bak" % index)
            fixtures.write(path, "old snapshot %d, distinct bytes\n" % index)
            timed.append(path)
        oldest_snapshot = timed[0]

        # An existing ~/.gitignore_global holding one line -- setup-git.sh
        # appends the missing patterns.
        gitignore_global = os.path.join(home, ".gitignore_global")
        fixtures.write(gitignore_global, "*.mylog\n")

        before = _tree_state(home)
        snap = snapshot_lib.take_snapshot(
            home, fixture, ws, now="20260101T000000000001Z")

        update = subprocess.run(
            ["python3", os.path.join(fixture, "lib", "setup_config.py"),
             cfg_path, fixture, "--update", "--enable", "session_titler"],
            env=fixtures.env_for(home), cwd=fixture, capture_output=True,
            text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(update.returncode, 0, update.stdout + update.stderr)
        _run_install(fixture, home)
        _run_setup_git(fixture, home)

        after = _tree_state(home)
        changed = _changed_or_removed(before, after)

        # Positive control: the changed-or-removed set is non-empty and
        # includes every path this test seeded specifically to change.
        expected = {referent, git_tidy, pre_commit, oldest_snapshot,
                    gitignore_global, cfg_path}
        self.assertTrue(changed, "nothing changed -- fixture did not run")
        self.assertTrue(expected <= changed, (expected - changed, sorted(changed)))

        with open(os.path.join(snap, "manifest.json")) as handle:
            manifest = json.load(handle)

        for path in changed:
            entry = _find_entry(manifest, path)
            kind, data = before[path]
            if entry is not None:
                self.assertEqual(entry["type"], kind, (path, entry))
                if kind == "file":
                    self.assertEqual(entry["sha256"], data, (path, entry))
                elif kind == "symlink":
                    self.assertEqual(entry["link_target"], data, (path, entry))
                continue
            # No top-level entry -- must be recorded as a symlink's referent.
            ref = _find_referent(manifest, path)
            self.assertIsNotNone(
                ref, "no manifest entry or referent record for %r" % path)
            self.assertEqual(kind, "file", (path, kind))
            self.assertEqual(ref["sha256"], data, (path, ref))


class TestTimedSettingsSnapshots(SandboxCase):
    """install.sh's per-run `settings.json.claude-code-toolkit-<STAMP>[-N].bak`
    snapshots are each recorded by the audit."""

    def test_every_timed_snapshot_is_recorded(self):
        home = self._home()
        names = ["settings.json.claude-code-toolkit-20250101T000000Z.bak",
                 "settings.json.claude-code-toolkit-20250101T000000Z-2.bak",
                 "settings.json.claude-code-toolkit-20250102T000000Z.bak"]
        for name in names:
            fixtures.write(os.path.join(home, ".claude", name),
                           "bytes of %s\n" % name)
        snap = snapshot_lib.take_snapshot(
            home, REPO, None, now="20260101T000000000004Z")
        with open(os.path.join(snap, "manifest.json")) as handle:
            manifest = json.load(handle)
        for name in names:
            entry = _find_entry(manifest, os.path.join(home, ".claude", name))
            self.assertIsNotNone(entry, name)
            self.assertEqual(entry["type"], "file", entry)


class TestReferentSurvivesLaterChanges(SandboxCase):
    """A snapshot's referent bytes never change after the fact, even
    across two further installs that keep editing the live referent."""

    def test_referent_bytes_are_immutable(self):
        home = self._home()
        fixture = self._fixture()
        ws = os.path.join(home, "workspace")
        os.makedirs(ws, exist_ok=True)

        referent = os.path.join(home, "dotfiles", "settings.json")
        original = json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [
                {"type": "command", "command": "echo one"}]}]}})
        fixtures.write(referent, original)
        settings_json = os.path.join(home, ".claude", "settings.json")
        os.makedirs(os.path.dirname(settings_json), exist_ok=True)
        os.symlink(referent, settings_json)

        _write_config(fixture, home, ws)

        snap = snapshot_lib.take_snapshot(
            home, fixture, ws, now="20260101T000000000002Z")
        with open(os.path.join(snap, "manifest.json")) as handle:
            manifest = json.load(handle)
        entry = _find_entry(manifest, settings_json)
        self.assertIsNotNone(entry, manifest)
        ref = entry["referent"]
        self.assertIsNotNone(ref, entry)
        with open(os.path.join(snap, ref["stored_as"])) as handle:
            stored = handle.read()
        self.assertEqual(stored, original)

        _run_install(fixture, home)

        # Edit the referent again, after the first install already did.
        with open(referent) as handle:
            current = json.loads(handle.read())
        current["hooks"]["Notification"] = [{"matcher": "*", "hooks": [
            {"type": "command", "command": "echo two"}]}]
        fixtures.write(referent, json.dumps(current))

        _run_install(fixture, home)

        with open(os.path.join(snap, ref["stored_as"])) as handle:
            stored_again = handle.read()
        self.assertEqual(stored_again, original)


class TestAbsentPaths(unittest.TestCase):
    """Absent Kind-2 paths appear as "absent", never omitted."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def test_absent_paths_are_recorded(self):
        home = self._home()
        # Hermetic against this machine's own environment: `_xdg_git_config_dir` reads
        # `XDG_CONFIG_HOME` from `os.environ` directly (there is no
        # `environ` parameter on `take_snapshot`), so an in-process call
        # must not inherit whatever this process happens to have set --
        # this is what makes the default-branch path below deterministic.
        env = dict(os.environ)
        env.pop("XDG_CONFIG_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            snap = snapshot_lib.take_snapshot(
                home, REPO, None, now="20260101T000000000003Z")
        with open(os.path.join(snap, "manifest.json")) as handle:
            manifest = json.load(handle)
        by_path = {entry["path"]: entry for entry in manifest}

        expected_absent = (
            os.path.join(home, ".claude", "settings.json"),
            os.path.join(home, ".claude", "settings.json.pre-claude-code-toolkit.bak"),
            os.path.join(home, ".claude", "CLAUDE.md"),
            os.path.join(home, ".gitconfig"),
            os.path.join(home, ".gitignore_global"),
            os.path.join(home, ".config", "git", "config"),
        )
        for path in expected_absent:
            self.assertIn(path, by_path, sorted(by_path))
            self.assertEqual(by_path[path]["type"], "absent", by_path[path])


class TestWorkspaceFiles(unittest.TestCase):
    """With a workspace root, its CLAUDE.md and TOOLING.md are recorded:
    a present one with its bytes, an absent one as `absent`."""

    def test_workspace_instruction_files_are_recorded(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        ws = os.path.join(home, "workspace")
        claude_md = os.path.join(ws, "CLAUDE.md")
        fixtures.write(claude_md, "# workspace\n")
        snap = snapshot_lib.take_snapshot(
            home, REPO, ws, now="20260101T000000000040Z")
        with open(os.path.join(snap, "manifest.json")) as handle:
            by_path = {e["path"]: e for e in json.load(handle)}
        self.assertEqual(by_path[claude_md]["type"], "file", by_path.get(claude_md))
        self.assertEqual(by_path[claude_md]["sha256"], _sha256(claude_md))
        self.assertEqual(by_path[os.path.join(ws, "TOOLING.md")]["type"], "absent")

        # Positive control: without a workspace root, neither is recorded.
        snap2 = snapshot_lib.take_snapshot(
            home, REPO, None, now="20260101T000000000041Z")
        with open(os.path.join(snap2, "manifest.json")) as handle:
            paths = {e["path"] for e in json.load(handle)}
        self.assertNotIn(claude_md, paths)


class TestAppendOnly(unittest.TestCase):
    """A second snapshot never touches the first; a pre-created
    directory sharing the same stamp raises, and is itself left unchanged."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def test_two_snapshots_and_a_stamp_collision(self):
        home = self._home()
        fixtures.write(os.path.join(
            home, ".config", "claude-code-toolkit", "config.json"), "{}\n")
        snap1 = snapshot_lib.take_snapshot(
            home, REPO, None, now="20260101T000000000004Z")
        hash1 = _dir_hash(snap1)

        snap2 = snapshot_lib.take_snapshot(
            home, REPO, None, now="20260101T000000000005Z")
        self.assertNotEqual(snap1, snap2)
        self.assertEqual(_dir_hash(snap1), hash1, "an earlier snapshot changed")

        # The second snapshot must not copy the first into itself: apply-safe
        # snapshots on every run, so nesting would grow storage quadratically
        # and silently. Positive control: the walk did record config-dir rows.
        with open(os.path.join(snap2, "manifest.json")) as handle:
            rows = json.load(handle)
        snapshots_dir = os.path.join(
            home, ".config", "claude-code-toolkit", "snapshots")
        self.assertTrue(any(r["path"].startswith(
            os.path.join(home, ".config", "claude-code-toolkit")) for r in rows))
        nested = [r["path"] for r in rows
                  if r["path"] == snapshots_dir
                  or r["path"].startswith(snapshots_dir + os.sep)]
        self.assertEqual(nested, [], "snapshot recorded an earlier snapshot")

        stamp = "20260101T000000000006Z"
        pre_existing = os.path.join(
            home, ".config", "claude-code-toolkit", "snapshots", stamp)
        marker = os.path.join(pre_existing, "marker.txt")
        fixtures.write(marker, "pre-existing content\n")
        before_hash = _dir_hash(pre_existing)

        with self.assertRaises(snapshot_lib.SnapshotError):
            snapshot_lib.take_snapshot(home, REPO, None, now=stamp)

        self.assertEqual(_dir_hash(pre_existing), before_hash,
                         "a stamp collision must not touch the existing directory")
        # And the FIRST snapshot is still untouched too.
        self.assertEqual(_dir_hash(snap1), hash1)


class TestSnapshotCli(unittest.TestCase):
    """`snapshot` returns 0 and prints an existing path; a failing
    manifest repo returns 2."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def test_success_prints_an_existing_path(self):
        home = self._home()
        rc, out, err = fixtures.run_cli(["snapshot"], home)
        self.assertEqual(rc, 0, out + err)
        path = out.strip()
        self.assertTrue(os.path.isdir(path), (path, out, err))

    def test_failing_manifest_repo_returns_2(self):
        # `fixtures.run_cli` always drives the real installed CLI against the
        # REAL repo (its own `repo=REPO` default), so a failing manifest repo
        # has to go through `audit_lib.main` directly -- same pattern as
        # `test_audit_links.py::TestMainAuditError`.
        home = self._home()
        self.assertNotEqual(home, fixtures.REAL_HOME)
        bad_repo = tempfile.mkdtemp(prefix="setup-audit-badrepo-")
        self.addCleanup(shutil.rmtree, bad_repo, ignore_errors=True)
        fixtures.write(os.path.join(bad_repo, "install.sh"), "#!/bin/sh\nexit 1\n")
        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(audit_lib.main(["snapshot"], repo=bad_repo), 2)


def _run_take_snapshot_subprocess(home, repo, workspace_root, now, timeout=20):
    """Runs `snapshot_lib.take_snapshot` in a FRESH subprocess. A hang must
    FAIL the test, not wedge the suite: `subprocess.run`'s own timeout kills
    the child (so a hung process never lingers) and raises
    `TimeoutExpired`, which the caller converts into a normal, readable
    assertion failure rather than an uncaught exception."""
    code = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import snapshot_lib\n"
        "try:\n"
        "    path = snapshot_lib.take_snapshot(%r, %r, %r, now=%r)\n"
        "    print('SNAPSHOT_OK', path)\n"
        "except snapshot_lib.SnapshotError as exc:\n"
        "    print('SNAPSHOT_ERROR', str(exc))\n"
    ) % (TOOL_DIR, home, repo, workspace_root, now)
    return subprocess.run(
        ["python3", "-c", code], capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=timeout)


class TestFifoRefusal(SandboxCase):
    """A FIFO would make `take_snapshot` block forever in
    `open()`. `take_snapshot` runs in a SEPARATE subprocess here specifically
    so a regression hangs that one subprocess (killed by `subprocess.run`'s
    own timeout) rather than the whole suite."""

    def test_fifo_directly_in_a_copy_dir_refuses_promptly(self):
        home = self._home()
        commands_dir = os.path.join(home, ".claude", "commands")
        os.makedirs(commands_dir, exist_ok=True)
        os.mkfifo(os.path.join(commands_dir, "a-fifo.md"))

        try:
            proc = _run_take_snapshot_subprocess(
                home, REPO, None, "20260101T000000000010Z")
        except subprocess.TimeoutExpired:
            self.fail("take_snapshot hung on a top-level FIFO instead of "
                     "refusing promptly")
        self.assertIn("SNAPSHOT_ERROR", proc.stdout, proc.stdout + proc.stderr)
        self.assertNotIn("SNAPSHOT_OK", proc.stdout, proc.stdout)

    def test_positive_control_no_fifo_directly(self):
        home = self._home()
        commands_dir = os.path.join(home, ".claude", "commands")
        fixtures.write(os.path.join(commands_dir, "normal.md"), "# ok\n")
        proc = _run_take_snapshot_subprocess(
            home, REPO, None, "20260101T000000000011Z")
        self.assertIn("SNAPSHOT_OK", proc.stdout, proc.stdout + proc.stderr)

    def test_fifo_nested_inside_a_real_dir_refuses(self):
        home = self._home()
        skill_dir = os.path.join(home, ".claude", "skills", "my-skill")
        os.makedirs(skill_dir, exist_ok=True)
        fixtures.write(os.path.join(skill_dir, "SKILL.md"), "# ok\n")
        os.mkfifo(os.path.join(skill_dir, "nested-fifo"))

        try:
            proc = _run_take_snapshot_subprocess(
                home, REPO, None, "20260101T000000000012Z")
        except subprocess.TimeoutExpired:
            self.fail("take_snapshot hung on a nested FIFO instead of "
                     "refusing promptly")
        self.assertIn("SNAPSHOT_ERROR", proc.stdout, proc.stdout + proc.stderr)
        self.assertNotIn("SNAPSHOT_OK", proc.stdout, proc.stdout)
        # Refused by the pre-flight walk, before copytree started: copytree's
        # own named-pipe error would also surface as SnapshotError, but only
        # after a partial copy, and it does not cover device nodes.
        self.assertIn("refusing to snapshot", proc.stdout, proc.stdout)
        self.assertNotIn("INCOMPLETE", proc.stdout, proc.stdout)

    def test_positive_control_no_fifo_nested(self):
        home = self._home()
        skill_dir = os.path.join(home, ".claude", "skills", "my-skill")
        fixtures.write(os.path.join(skill_dir, "SKILL.md"), "# ok\n")
        proc = _run_take_snapshot_subprocess(
            home, REPO, None, "20260101T000000000013Z")
        self.assertIn("SNAPSHOT_OK", proc.stdout, proc.stdout + proc.stderr)


class TestUnreadableFileRefusesWithIncompleteMessage(SandboxCase):
    """An unreadable file must never leak a raw `OSError` /
    `shutil.Error` out of `take_snapshot` -- it must become a `SnapshotError`
    naming the snapshot INCOMPLETE, and the CLI must map that to exit 3."""

    def _chmod_000_file(self, home, name):
        skill_dir = os.path.join(home, ".claude", "skills", name)
        os.makedirs(skill_dir, exist_ok=True)
        blocked = os.path.join(skill_dir, "blocked.txt")
        fixtures.write(blocked, "secret\n")
        os.chmod(blocked, 0o000)
        # LIFO: restore permissions before the sandbox's own rmtree cleanup.
        self.addCleanup(os.chmod, blocked, 0o644)
        return blocked

    def test_unreadable_file_raises_incomplete_snapshot_error(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file permission bits")
        home = self._home()
        self._chmod_000_file(home, "my-skill")

        with self.assertRaises(snapshot_lib.SnapshotError) as ctx:
            snapshot_lib.take_snapshot(
                home, REPO, None, now="20260101T000000000014Z")
        self.assertIn("INCOMPLETE", str(ctx.exception), str(ctx.exception))

    def test_via_cli_main_returns_3(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file permission bits")
        home = self._home()
        self._chmod_000_file(home, "my-skill2")

        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(audit_lib.main(["snapshot"], repo=REPO), 3)

    def test_positive_control_mode_0644_snapshots_fine(self):
        home = self._home()
        skill_dir = os.path.join(home, ".claude", "skills", "my-skill3")
        readable = os.path.join(skill_dir, "readable.txt")
        fixtures.write(readable, "not secret\n")
        os.chmod(readable, 0o644)

        snap = snapshot_lib.take_snapshot(
            home, REPO, None, now="20260101T000000000015Z")
        self.assertTrue(os.path.isdir(snap))


class TestUnlistableDirectoryRefusesLoudly(SandboxCase):
    """`os.walk`'s default silently skips a
    directory it cannot list, rather than raising -- confirmed by
    execution: `~/.config/claude-code-toolkit/git-hooks` at mode 0300 (no
    read permission, so it cannot be LISTED) made `snapshot` exit 0 having
    never recorded `git-hooks/pre-commit` at all, and
    `scripts/setup-git.sh` then replaced that file with no backup to
    restore it from."""

    def test_unlistable_git_hooks_dir_refuses(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permission bits")
        home = self._home()
        git_hooks = os.path.join(
            home, ".config", "claude-code-toolkit", "git-hooks")
        os.makedirs(git_hooks, exist_ok=True)
        fixtures.write(os.path.join(git_hooks, "pre-commit"),
                       "#!/bin/sh\necho hi\n")
        os.chmod(git_hooks, 0o300)
        # LIFO: restore permissions before the sandbox's own rmtree cleanup.
        self.addCleanup(os.chmod, git_hooks, 0o755)

        with self.assertRaises(snapshot_lib.SnapshotError) as ctx:
            snapshot_lib.take_snapshot(
                home, REPO, None, now="20260101T000000000020Z")
        self.assertIn("INCOMPLETE", str(ctx.exception), str(ctx.exception))

    def test_positive_control_mode_0700_succeeds_and_records_pre_commit(self):
        home = self._home()
        git_hooks = os.path.join(
            home, ".config", "claude-code-toolkit", "git-hooks")
        os.makedirs(git_hooks, exist_ok=True)
        pre_commit = os.path.join(git_hooks, "pre-commit")
        fixtures.write(pre_commit, "#!/bin/sh\necho hi\n")
        os.chmod(git_hooks, 0o700)

        snap = snapshot_lib.take_snapshot(
            home, REPO, None, now="20260101T000000000021Z")
        with open(os.path.join(snap, "manifest.json")) as handle:
            manifest = json.load(handle)
        self.assertTrue(
            any(e["path"] == pre_commit for e in manifest), manifest)


class TestDirectoryReferentSurvives(SandboxCase):
    """A symlink whose referent is a DIRECTORY
    (`~/.config/claude-code-toolkit/git-hooks` -> a dotfiles copy, the
    confirmed real-world shape) used to be recorded link-only -- no bytes
    saved -- so `scripts/setup-git.sh`'s `ln -sfn` replacing the real
    `pre-commit` INSIDE the referent left nothing to restore it from."""

    def test_setup_git_replaces_the_referent_file_and_bytes_survive(self):
        home = self._home()
        fixture = self._fixture()
        ws = os.path.join(home, "workspace")
        os.makedirs(ws, exist_ok=True)

        referent_dir = os.path.join(home, "dotfiles", "git-hooks")
        original_pre_commit = "#!/bin/sh\necho ORIGINAL\n"
        pre_commit_path = os.path.join(referent_dir, "pre-commit")
        fixtures.write(pre_commit_path, original_pre_commit)

        git_hooks_link = os.path.join(
            home, ".config", "claude-code-toolkit", "git-hooks")
        os.makedirs(os.path.dirname(git_hooks_link), exist_ok=True)
        os.symlink(referent_dir, git_hooks_link)

        snap = snapshot_lib.take_snapshot(
            home, fixture, ws, now="20260101T000000000022Z")

        _run_setup_git(fixture, home)

        # Positive control: setup-git.sh really did replace the file INSIDE
        # the referent (`ln -sfn` resolves the "git-hooks" path component
        # transparently through the symlink) -- else this test proves
        # nothing. The link itself, `git_hooks_link`, is untouched: only
        # its FINAL path component changes underneath it.
        self.assertTrue(
            os.path.islink(pre_commit_path),
            "setup-git.sh did not replace pre-commit -- positive control failed")
        self.assertEqual(os.readlink(git_hooks_link), referent_dir)

        with open(os.path.join(snap, "manifest.json")) as handle:
            manifest = json.load(handle)
        entry = _find_entry(manifest, git_hooks_link)
        self.assertIsNotNone(entry, manifest)
        ref = entry["referent"]
        self.assertIsNotNone(ref, entry)
        self.assertIsNone(ref["sha256"], ref)
        stored_pre_commit = os.path.join(snap, ref["stored_as"], "pre-commit")
        with open(stored_pre_commit) as handle:
            self.assertEqual(handle.read(), original_pre_commit)


class TestXdgGitConfig(SandboxCase):
    """Git's XDG global config
    (`$XDG_CONFIG_HOME/git/config`, or `<home>/.config/git/config` when
    unset) was entirely missing from the Kind-2 fixed paths -- confirmed by
    execution: with `~/.config/git/config` present and `~/.gitconfig`
    absent, `git config --global` WRITES to that file, and a snapshot taken
    just before `scripts/setup-git.sh` ran recorded nothing to restore it
    from."""

    def test_xdg_git_config_changes_and_before_state_is_recorded(self):
        home = self._home()
        fixture = self._fixture()

        xdg_config = os.path.join(home, ".config", "git", "config")
        original = "[user]\n\tname = Old\n\temail = old@example.com\n"
        fixtures.write(xdg_config, original)
        # No ~/.gitconfig at all -- this is the exact scenario where git
        # prefers the XDG file for both reads and writes.
        self.assertFalse(os.path.lexists(os.path.join(home, ".gitconfig")))

        snap = snapshot_lib.take_snapshot(
            home, fixture, None, now="20260101T000000000024Z")

        _run_setup_git(fixture, home)

        # Positive control: setup-git.sh really did write to the XDG file,
        # and never created a ~/.gitconfig instead.
        with open(xdg_config) as handle:
            after = handle.read()
        self.assertNotEqual(after, original, "setup-git.sh did not write to "
                            "the XDG config -- positive control failed")
        self.assertFalse(os.path.lexists(os.path.join(home, ".gitconfig")),
                         "a ~/.gitconfig should never have been created")

        with open(os.path.join(snap, "manifest.json")) as handle:
            manifest = json.load(handle)
        entry = _find_entry(manifest, xdg_config)
        self.assertIsNotNone(entry, manifest)
        self.assertEqual(entry["type"], "file", entry)
        self.assertEqual(
            entry["sha256"], hashlib.sha256(original.encode()).hexdigest(), entry)


class TestXdgGitConfigDirHelper(unittest.TestCase):
    """A direct, hermetic check of `_xdg_git_config_dir`'s two branches.
    `fixtures.env_for` strips `XDG_CONFIG_HOME` for every subprocess test
    above, so the variable-SET branch needs its own in-process check."""

    def test_xdg_config_home_set_and_absolute(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/xdg"}):
            self.assertEqual(
                snapshot_lib._xdg_git_config_dir("/tmp/irrelevant-home"),
                "/custom/xdg/git")

    def test_xdg_config_home_absent_falls_back_to_home_config_git(self):
        home = "/tmp/irrelevant-home"
        env = dict(os.environ)
        env.pop("XDG_CONFIG_HOME", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                snapshot_lib._xdg_git_config_dir(home),
                os.path.join(home, ".config", "git"))

    def test_xdg_config_home_relative_value_is_ignored(self):
        home = "/tmp/irrelevant-home"
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/path"}):
            self.assertEqual(
                snapshot_lib._xdg_git_config_dir(home),
                os.path.join(home, ".config", "git"))


class TestGitConfigGlobalEnv(SandboxCase):
    """`GIT_CONFIG_GLOBAL` names an
    alternate global git config, and `scripts/setup-git.sh` inherits it (it
    strips no `GIT_*` var at all) -- but the Kind-2 discovery in
    `_git_excludes_file` stripped every `GIT_*` var, including this one, so
    it always asked about `~/.gitconfig` instead of the config
    `setup-git.sh` actually reads and writes. Confirmed by execution:
    with `GIT_CONFIG_GLOBAL` pointed at a custom config whose own
    `core.excludesfile` names a custom ignore file, `setup-git.sh` changed
    BOTH that config and that ignore file, and neither had a manifest row
    to restore from."""

    def test_custom_global_config_and_its_excludesfile_are_both_recorded(self):
        home = self._home()
        fixture = self._fixture()

        custom_global = os.path.join(home, "dotfiles", "gitconfig-custom")
        custom_ignore = os.path.join(home, "dotfiles", "ignore-custom")
        original_global = "[core]\n\texcludesfile = %s\n" % custom_ignore
        fixtures.write(custom_global, original_global)
        original_ignore = "*.mylog\n"
        fixtures.write(custom_ignore, original_ignore)
        # No ~/.gitconfig at all -- GIT_CONFIG_GLOBAL is the only global git
        # sees, for both this snapshot's own discovery and setup-git.sh.
        self.assertFalse(os.path.lexists(os.path.join(home, ".gitconfig")))

        extra = {"GIT_CONFIG_GLOBAL": custom_global}
        with mock.patch.dict(os.environ, extra):
            snap = snapshot_lib.take_snapshot(
                home, fixture, None, now="20260101T000000000031Z")

        _run_setup_git(fixture, home, extra_env=extra)

        # Positive control: setup-git.sh really did write to BOTH files --
        # else this test proves nothing.
        with open(custom_global) as handle:
            global_after = handle.read()
        with open(custom_ignore) as handle:
            ignore_after = handle.read()
        self.assertNotEqual(global_after, original_global,
                            "setup-git.sh did not write to the custom global "
                            "config -- positive control failed")
        self.assertNotEqual(ignore_after, original_ignore,
                            "setup-git.sh did not append to the custom "
                            "excludesfile -- positive control failed")
        self.assertFalse(os.path.lexists(os.path.join(home, ".gitconfig")),
                         "a ~/.gitconfig should never have been created")

        with open(os.path.join(snap, "manifest.json")) as handle:
            manifest = json.load(handle)

        global_entry = _find_entry(manifest, custom_global)
        self.assertIsNotNone(global_entry, manifest)
        self.assertEqual(global_entry["type"], "file", global_entry)
        self.assertEqual(
            global_entry["sha256"],
            hashlib.sha256(original_global.encode()).hexdigest(), global_entry)

        ignore_entry = _find_entry(manifest, custom_ignore)
        self.assertIsNotNone(ignore_entry, manifest)
        self.assertEqual(ignore_entry["type"], "file", ignore_entry)
        self.assertEqual(
            ignore_entry["sha256"],
            hashlib.sha256(original_ignore.encode()).hexdigest(), ignore_entry)


if __name__ == "__main__":
    unittest.main(verbosity=2)
