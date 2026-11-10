#!/usr/bin/env python3
"""Tests for `apply_lib.apply_safe` and the `apply-safe` CLI subcommand.

Every sandbox is a fresh, empty `tempfile.mkdtemp()` -- nothing here
touches the real machine.

**`REPO` (this worktree) is itself a linked git worktree** -- confirmed by
execution: `bash -c '. lib/worktree_detect.sh && is_linked_worktree "$(pwd)"'`
exits 0 from any linked worktree of this repo. Since `apply_safe` refuses a
linked worktree, every test that needs `apply_safe`
(or `snapshot_lib.take_snapshot`, or `build_report`) to actually proceed uses
a `fixtures.make_fixture_repo` primary checkout instead of `REPO` --
`FixtureRepoCase` builds one per TestCase class (`setUpClass`), shared across
that class's tests, since building one (`git init`, a commit, a full manifest
probe) is not free.

Every in-process `apply_safe(...)`/`build_report(...)` call below passes an
explicit `environ={}` -- never the ambient `os.environ`.

`CLAUDE_TOOLKIT_CONFIG` is forced to a guaranteed-nonexistent path below,
before any test runs -- same reasoning as the other setup-audit suites.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
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
import apply_lib  # noqa: E402
import audit_lib  # noqa: E402
import snapshot_lib  # noqa: E402


def _link_row(repo, home):
    """The first `link`-kind manifest row for `repo` -- picked from the real
    manifest rather than hardcoded, so a renamed tool cannot go stale."""
    for row in audit_lib.load_manifest(repo, home):
        if row["kind"] == "link":
            return row
    raise AssertionError("no link-kind row in the manifest")


def _nth_link_rows(repo, home, n):
    rows = [r for r in audit_lib.load_manifest(repo, home) if r["kind"] == "link"]
    assert len(rows) >= n, "manifest has fewer than %d link rows" % n
    return rows[:n]


def _manifest_entry(snap, path):
    with open(os.path.join(snap, "manifest.json")) as handle:
        manifest = json.load(handle)
    for entry in manifest:
        if entry["path"] == path:
            return entry
    return None


_git_worktree_add = fixtures.git_worktree_add


class SandboxCase(unittest.TestCase):
    """A throwaway, empty HOME per test. Nothing here touches the real machine."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home


class FixtureRepoCase(SandboxCase):
    """Base for tests that need `apply_safe`/`take_snapshot`/`build_report`
    to run against an on-disk repo that is NOT a linked worktree -- `REPO`
    (see module docstring) no longer works for this. One fixture is shared
    per TestCase class (`setUpClass`), since building one is not free."""

    @classmethod
    def setUpClass(cls):
        cls._fixture_root = tempfile.mkdtemp(prefix="setup-audit-fixture-")
        cls.fixture = fixtures.make_fixture_repo(cls._fixture_root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._fixture_root, ignore_errors=True)


class TestRemovesOnlyIdenticalLinks(FixtureRepoCase):
    """An identical link-row file is removed and its bytes survive in
    the snapshot; a `differs` file, a dangling link
    and a theirs-only file are all left alone."""

    def test_removes_only_the_identical_file(self):
        home = self._home()
        row_identical, row_differs, row_dangling = _nth_link_rows(self.fixture, home, 3)

        os.makedirs(os.path.dirname(row_identical["dest"]), exist_ok=True)
        shutil.copy2(row_identical["target"], row_identical["dest"])
        with open(row_identical["dest"], "rb") as handle:
            original_bytes = handle.read()

        os.makedirs(os.path.dirname(row_differs["dest"]), exist_ok=True)
        shutil.copy2(row_differs["target"], row_differs["dest"])
        with open(row_differs["dest"], "ab") as handle:
            handle.write(b"\n# one extra byte for the differs test\n")
        with open(row_differs["dest"], "rb") as handle:
            differs_bytes = handle.read()

        os.makedirs(os.path.dirname(row_dangling["dest"]), exist_ok=True)
        os.symlink(os.path.join(home, "does-not-exist"), row_dangling["dest"])

        mine_md = os.path.join(home, ".claude", "commands", "mine.md")
        fixtures.write(mine_md, "# mine\n")

        result = apply_lib.apply_safe(home, self.fixture, None, environ={})

        self.assertEqual(result["removed"], [row_identical["dest"]], result)
        self.assertFalse(os.path.lexists(row_identical["dest"]),
                         "the identical file was not removed")

        self.assertTrue(os.path.exists(row_differs["dest"]), "a differs file was removed")
        with open(row_differs["dest"], "rb") as handle:
            self.assertEqual(handle.read(), differs_bytes, "a differs file was touched")

        self.assertTrue(os.path.islink(row_dangling["dest"]), "a dangling link was touched")

        self.assertTrue(os.path.exists(mine_md), "a theirs-only file was touched")

        entry = _manifest_entry(result["snapshot"], row_identical["dest"])
        self.assertIsNotNone(entry, "no snapshot manifest row for the removed file")
        self.assertEqual(entry["type"], "file", entry)
        stored = os.path.join(result["snapshot"], "files",
                              os.path.relpath(row_identical["dest"], home))
        with open(stored, "rb") as handle:
            self.assertEqual(handle.read(), original_bytes,
                             "the snapshot did not keep the removed file's bytes")


class TestSecondRunIsANoOp(FixtureRepoCase):
    """Running twice removes nothing the second time, creates a
    second, distinct snapshot, and never touches the first."""

    def test_second_run_removes_nothing_and_snapshots_again(self):
        home = self._home()
        row = _link_row(self.fixture, home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        shutil.copy2(row["target"], row["dest"])

        result1 = apply_lib.apply_safe(home, self.fixture, None, environ={})
        self.assertEqual(result1["removed"], [row["dest"]])
        with open(os.path.join(result1["snapshot"], "manifest.json"), "rb") as handle:
            snap1_before = handle.read()

        result2 = apply_lib.apply_safe(home, self.fixture, None, environ={})
        self.assertEqual(result2["removed"], [], "a second run removed something")
        self.assertNotEqual(result1["snapshot"], result2["snapshot"])

        with open(os.path.join(result1["snapshot"], "manifest.json"), "rb") as handle:
            snap1_after = handle.read()
        self.assertEqual(snap1_before, snap1_after, "an earlier snapshot changed")


class TestBeforeDeleteSeam(FixtureRepoCase):
    """`_before_delete` rewriting the file's bytes must make the
    byte-equality validation genuinely fail -- `ApplyError`, file kept.
    Positive control: without the seam the same setup removes cleanly."""

    def test_seam_blocks_removal_when_it_tampers_the_file(self):
        home = self._home()
        row = _link_row(self.fixture, home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        shutil.copy2(row["target"], row["dest"])

        def _tamper(dest):
            with open(dest, "ab") as handle:
                handle.write(b"tampered by the test seam\n")

        with self.assertRaises(apply_lib.ApplyError):
            apply_lib.apply_safe(home, self.fixture, None, environ={},
                                 _before_delete=_tamper)

        self.assertTrue(os.path.exists(row["dest"]),
                        "file removed despite a failed validation")
        with open(row["dest"], "rb") as handle:
            self.assertIn(b"tampered by the test seam", handle.read())

    def test_positive_control_without_the_seam(self):
        home = self._home()
        row = _link_row(self.fixture, home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        shutil.copy2(row["target"], row["dest"])

        result = apply_lib.apply_safe(home, self.fixture, None, environ={})
        self.assertEqual(result["removed"], [row["dest"]])
        self.assertFalse(os.path.lexists(row["dest"]))


class TestEachValidationStandsAlone(FixtureRepoCase):
    """The three validations overlap, so a tamper the seam can reach trips
    more than one. Each test here opens the window only ONE check covers
    (found by mutation testing: the target-bytes and symlink checks each
    survived removal until these existed)."""

    def test_file_changed_before_the_snapshot_is_caught_by_target_bytes(self):
        # Changed after the report called it identical but before the
        # snapshot: the snapshot records the NEW bytes, so the sha check
        # passes and only the byte comparison with the target can refuse.
        home = self._home()
        row = _link_row(self.fixture, home)
        dest = row["dest"]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(row["target"], dest)
        real_take = snapshot_lib.take_snapshot

        def _change_then_snapshot(*args, **kwargs):
            with open(dest, "ab") as handle:
                handle.write(b"changed before the snapshot\n")
            return real_take(*args, **kwargs)

        with mock.patch.object(snapshot_lib, "take_snapshot",
                               side_effect=_change_then_snapshot):
            with self.assertRaises(apply_lib.ApplyError) as ctx:
                apply_lib.apply_safe(home, self.fixture, None, environ={})
        self.assertIn("no longer matches its repo target", str(ctx.exception))
        self.assertTrue(os.path.isfile(dest), "changed file was removed")

    def test_file_swapped_for_a_symlink_is_caught_by_the_real_file_check(self):
        # A symlink to the target reads byte-equal and hashes to the
        # snapshot's sha, so only the real-file check can refuse.
        home = self._home()
        row = _link_row(self.fixture, home)
        dest = row["dest"]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(row["target"], dest)

        def _swap_for_symlink(path):
            os.remove(path)
            os.symlink(row["target"], path)

        with self.assertRaises(apply_lib.ApplyError) as ctx:
            apply_lib.apply_safe(home, self.fixture, None, environ={},
                                 _before_delete=_swap_for_symlink)
        self.assertIn("is not a real file", str(ctx.exception))
        self.assertTrue(os.path.islink(dest), "the swapped-in symlink was removed")


class TestSnapshotShaMustMatch(FixtureRepoCase):
    """Extra test (beyond the plan's four): a file whose bytes equal the
    target but whose snapshot manifest row is missing must still block
    removal -- this pins the snapshot-sha check independently of the
    target-bytes check, which passes here since the file itself is untouched.
    `snapshot_lib.take_snapshot` is monkeypatched, in-process, to return a
    snapshot directory whose manifest has had the row stripped."""

    def test_missing_snapshot_row_blocks_removal(self):
        home = self._home()
        row = _link_row(self.fixture, home)
        dest = row["dest"]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(row["target"], dest)

        real_snap = snapshot_lib.take_snapshot(home, self.fixture, None)
        manifest_path = os.path.join(real_snap, "manifest.json")
        with open(manifest_path) as handle:
            manifest = json.load(handle)
        stripped = [entry for entry in manifest if entry["path"] != dest]
        self.assertLess(len(stripped), len(manifest),
                        "dest had no manifest row to strip in the first place")
        with open(manifest_path, "w") as handle:
            json.dump(stripped, handle)

        with mock.patch.object(snapshot_lib, "take_snapshot", return_value=real_snap):
            with self.assertRaises(apply_lib.ApplyError):
                apply_lib.apply_safe(home, self.fixture, None, environ={})

        self.assertTrue(os.path.exists(dest),
                        "file removed despite a missing snapshot manifest row")

    def test_positive_control_with_the_row_intact(self):
        home = self._home()
        row = _link_row(self.fixture, home)
        dest = row["dest"]
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(row["target"], dest)

        result = apply_lib.apply_safe(home, self.fixture, None, environ={})
        self.assertEqual(result["removed"], [dest])


class TestApplyThenRealInstall(unittest.TestCase):
    """Apply-safe, then a real `bash <fixture>/install.sh` -- the
    formerly identical file becomes a symlink to its repo target, the
    theirs-only file survives untouched,
    and a will_prune symlink (same shape as `test_audit_links`'s TestTheirsOnly)
    is gone -- matching what the report already said `will_prune` meant."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def _fixture(self):
        root = tempfile.mkdtemp(prefix="setup-audit-fixture-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        return fixtures.make_fixture_repo(root)

    def test_apply_safe_matches_a_real_install(self):
        home = self._home()
        fixture = self._fixture()
        ws = os.path.join(home, "workspace")
        os.makedirs(ws, exist_ok=True)

        row = _link_row(fixture, home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        shutil.copy2(row["target"], row["dest"])

        mine_md = os.path.join(home, ".claude", "commands", "mine.md")
        fixtures.write(mine_md, "# mine\n")

        # A dangling symlink INTO the fixture repo, at a file it does not
        # have -- same shape as `test_audit_links`'s `will_prune` case.
        retired = os.path.join(home, ".claude", "commands", "retired.md")
        os.makedirs(os.path.dirname(retired), exist_ok=True)
        os.symlink(os.path.join(fixture, "commands", "retired.md"), retired)

        report_before = audit_lib.build_report(
            home=home, repo=fixture, environ={}, workspace_root=ws)
        prune_row = next(r for r in report_before["theirs_only"] if r["path"] == retired)
        self.assertTrue(prune_row["will_prune"], prune_row)

        result = apply_lib.apply_safe(home, fixture, ws, environ={})
        self.assertEqual(result["removed"], [row["dest"]])

        fixtures.write_config(fixture, home, ws)
        fixtures.run_install(fixture, home)

        self.assertTrue(os.path.islink(row["dest"]),
                        "install.sh did not symlink the reclaimed path")
        self.assertEqual(os.path.realpath(row["dest"]), os.path.realpath(row["target"]))
        self.assertTrue(os.path.exists(mine_md), "the theirs-only file was disturbed")
        self.assertFalse(os.path.lexists(retired),
                         "the will_prune link was not pruned by the real install")


class TestRefusesLinkedWorktree(unittest.TestCase):
    """`repo` being a linked git
    worktree of a fixture -- ApplyError, the staged file stays, and no
    snapshot directory is created at all."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def _worktree_of_a_fresh_fixture(self):
        fixture_root = tempfile.mkdtemp(prefix="setup-audit-fixture-")
        self.addCleanup(shutil.rmtree, fixture_root, ignore_errors=True)
        fixture = fixtures.make_fixture_repo(fixture_root)
        wt_root = tempfile.mkdtemp(prefix="setup-audit-worktree-")
        self.addCleanup(shutil.rmtree, wt_root, ignore_errors=True)
        wt_path = os.path.join(wt_root, "wt")
        _git_worktree_add(fixture, wt_path)
        return fixture, wt_path

    def test_apply_safe_refuses_from_a_linked_worktree(self):
        _fixture, wt_path = self._worktree_of_a_fresh_fixture()
        home = self._home()

        row = _link_row(wt_path, home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        shutil.copy2(row["target"], row["dest"])

        snapshots_dir = os.path.join(
            home, ".config", "claude-code-toolkit", "snapshots")
        before = (sorted(os.listdir(snapshots_dir))
                 if os.path.isdir(snapshots_dir) else [])

        with self.assertRaises(apply_lib.ApplyError) as ctx:
            apply_lib.apply_safe(home, wt_path, None, environ={})
        self.assertIn("linked git worktree", str(ctx.exception))
        self.assertEqual(ctx.exception.removed, [])
        self.assertIsNone(ctx.exception.snapshot)

        self.assertTrue(os.path.exists(row["dest"]),
                        "file removed from a linked worktree run")
        after = (sorted(os.listdir(snapshots_dir))
                if os.path.isdir(snapshots_dir) else [])
        self.assertEqual(after, before,
                         "a new snapshot dir appeared despite the worktree refusal")


class TestIsLinkedWorktreeHelper(unittest.TestCase):
    """`is_linked_worktree`
    itself, positive control, and the missing-helper fail-closed case."""

    def test_true_for_a_worktree_false_for_the_primary_checkout(self):
        fixture_root = tempfile.mkdtemp(prefix="setup-audit-fixture-")
        self.addCleanup(shutil.rmtree, fixture_root, ignore_errors=True)
        fixture = fixtures.make_fixture_repo(fixture_root)
        wt_root = tempfile.mkdtemp(prefix="setup-audit-worktree-")
        self.addCleanup(shutil.rmtree, wt_root, ignore_errors=True)
        wt_path = os.path.join(wt_root, "wt")
        _git_worktree_add(fixture, wt_path)

        self.assertTrue(audit_lib.is_linked_worktree(wt_path))
        self.assertFalse(audit_lib.is_linked_worktree(fixture))

    def test_missing_helper_raises_audit_error(self):
        fixture_root = tempfile.mkdtemp(prefix="setup-audit-fixture-")
        self.addCleanup(shutil.rmtree, fixture_root, ignore_errors=True)
        fixture = fixtures.make_fixture_repo(fixture_root)
        os.remove(os.path.join(fixture, "lib", "worktree_detect.sh"))

        with self.assertRaises(audit_lib.AuditError):
            audit_lib.is_linked_worktree(fixture)


class TestPartialRemovalIsReported(FixtureRepoCase):
    """An `ApplyError` on candidate N must report
    what was ALREADY removed and where the snapshot is, not just the failing
    path."""

    def test_apply_safe_attaches_removed_and_snapshot_to_the_error(self):
        home = self._home()
        row1, row2 = _nth_link_rows(self.fixture, home, 2)
        for row in (row1, row2):
            os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
            shutil.copy2(row["target"], row["dest"])

        def _tamper_only_the_second(dest):
            if dest == row2["dest"]:
                with open(dest, "ab") as handle:
                    handle.write(b"tampered\n")

        with self.assertRaises(apply_lib.ApplyError) as ctx:
            apply_lib.apply_safe(home, self.fixture, None, environ={},
                                 _before_delete=_tamper_only_the_second)

        self.assertEqual(ctx.exception.removed, [row1["dest"]])
        self.assertIsNotNone(ctx.exception.snapshot)
        self.assertTrue(os.path.isdir(ctx.exception.snapshot))
        self.assertFalse(os.path.lexists(row1["dest"]), "the first candidate was not removed")
        self.assertTrue(os.path.exists(row2["dest"]), "the second candidate was wrongly removed")

    def test_cli_prints_the_partial_removal_before_the_error(self):
        # Isolates the CLI's mapping of ApplyError -> stdout/stderr/exit code
        # from the removal logic itself (covered above, in-process):
        # `apply_lib.apply_safe` is patched to raise a controlled ApplyError
        # carrying `removed`/`snapshot`, so what is under test here is only
        # `audit_lib.main`'s own print-then-exit mapping.
        home = self._home()
        fake_snap = os.path.join(
            home, ".config", "claude-code-toolkit", "snapshots", "fake")
        os.makedirs(fake_snap, exist_ok=True)
        removed_path = os.path.join(home, ".local", "bin", "removed-one")

        def _fake_apply_safe(*args, **kwargs):
            raise apply_lib.ApplyError(
                "boom", removed=[removed_path], snapshot=fake_snap)

        buf = io.StringIO()
        with mock.patch.object(apply_lib, "apply_safe", side_effect=_fake_apply_safe):
            with mock.patch.dict(os.environ, {"HOME": home}):
                with redirect_stdout(buf):
                    rc = audit_lib.main(["apply-safe"], repo=self.fixture)
        out = buf.getvalue()
        self.assertEqual(rc, 3, out)
        self.assertIn("removed %s" % removed_path, out, out)
        self.assertIn("snapshot: %s" % fake_snap, out, out)


class TestOSErrorDuringRemovalBecomesApplyError(FixtureRepoCase):
    """An `OSError` while validating or
    removing a candidate -- a `PermissionError` from `os.remove` is the
    confirmed case, simulated here by patching it -- used to escape
    `apply_safe` raw. Uncaught, it skipped past both the print loop and the
    function's own return, so after N-1 removals nothing was printed at
    all. It must become an `ApplyError` naming the path, carrying what was
    already removed and the snapshot that already exists."""

    def _two_rows_and_a_failing_remove(self, home):
        """Two `link` rows with real, identical files at their destinations,
        plus a stand-in for `os.remove` that raises `PermissionError` for the
        SECOND row's `dest` only and delegates to the real function for
        everything else. Every link row now sits in `~/.local/bin`, so a
        chmod on one parent directory would block both; patching the call
        `apply_lib` makes is what isolates one row."""
        row1, row2 = _nth_link_rows(self.fixture, home, 2)
        for row in (row1, row2):
            os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
            shutil.copy2(row["target"], row["dest"])

        real_remove = os.remove

        def failing_remove(path, *args, **kwargs):
            if path == row2["dest"]:
                raise PermissionError(13, "Permission denied", path)
            return real_remove(path, *args, **kwargs)

        return row1, row2, mock.patch.object(apply_lib.os, "remove", failing_remove)

    def test_removal_permission_error_becomes_apply_error(self):
        home = self._home()
        row1, row2, failing = self._two_rows_and_a_failing_remove(home)

        with failing:
            with self.assertRaises(apply_lib.ApplyError) as ctx:
                apply_lib.apply_safe(home, self.fixture, None, environ={})

        self.assertEqual(ctx.exception.removed, [row1["dest"]])
        self.assertIsNotNone(ctx.exception.snapshot)
        self.assertTrue(os.path.isdir(ctx.exception.snapshot))
        self.assertFalse(os.path.lexists(row1["dest"]), "the first candidate was not removed")
        self.assertTrue(os.path.exists(row2["dest"]), "the second candidate was wrongly removed")

    def test_cli_prints_the_partial_removal_then_returns_3(self):
        # Driven for REAL against the fixture repo, HOME patched -- not a
        # mocked `apply_safe` (that seam is `TestPartialRemovalIsReported`'s
        # own test, isolating the print mapping alone). This one proves the
        # OSError-to-ApplyError conversion above actually reaches the CLI.
        home = self._home()
        row1, row2, failing = self._two_rows_and_a_failing_remove(home)

        buf = io.StringIO()
        with failing:
            with mock.patch.dict(os.environ, {"HOME": home}):
                with redirect_stdout(buf):
                    rc = audit_lib.main(["apply-safe"], repo=self.fixture)
        out = buf.getvalue()
        self.assertEqual(rc, 3, out)
        self.assertIn("removed %s" % row1["dest"], out, out)
        self.assertTrue(any(line.startswith("snapshot: ") for line in out.splitlines()), out)


class TestApplySafeCli(FixtureRepoCase):
    """CLI: `apply-safe` prints one `removed <dest>` line per deletion and a
    final `snapshot: <path>` line; `ApplyError`/`AuditError` map to 3/2."""

    def test_success_prints_removed_and_snapshot_lines(self):
        home = self._home()
        row = _link_row(self.fixture, home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        shutil.copy2(row["target"], row["dest"])

        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": home}):
            with redirect_stdout(buf):
                rc = audit_lib.main(["apply-safe"], repo=self.fixture)
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("removed %s" % row["dest"], out, out)
        self.assertTrue(any(line.startswith("snapshot: ") for line in out.splitlines()), out)

    def test_apply_error_returns_3(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores file permission bits")
        home = self._home()
        skill_dir = os.path.join(home, ".claude", "skills", "blocked-skill")
        blocked = os.path.join(skill_dir, "blocked.txt")
        fixtures.write(blocked, "secret\n")
        os.chmod(blocked, 0o000)
        self.addCleanup(os.chmod, blocked, 0o644)

        with mock.patch.dict(os.environ, {"HOME": home}):
            self.assertEqual(audit_lib.main(["apply-safe"], repo=self.fixture), 3)

    def test_failing_manifest_repo_returns_2(self):
        # The bad repo carries the real host settings, legacy list, plugin
        # manifest and worktree helper so
        # the broken manifest is the ONLY thing that can raise: without them
        # the template's own AuditError returned 2 whatever load_manifest did.
        home = self._home()
        self.assertNotEqual(home, fixtures.REAL_HOME)
        bad_repo = tempfile.mkdtemp(prefix="setup-audit-badrepo-")
        self.addCleanup(shutil.rmtree, bad_repo, ignore_errors=True)
        fixtures.write(os.path.join(bad_repo, "install.sh"), "#!/bin/sh\nexit 1\n")
        for rel in (os.path.join("claude", "settings.host.json"),
                    os.path.join("claude", "legacy-hook-commands.json"),
                    os.path.join("plugin", "hooks", "hooks.json"),
                    os.path.join("lib", "worktree_detect.sh")):
            os.makedirs(os.path.dirname(os.path.join(bad_repo, rel)), exist_ok=True)
            shutil.copy2(os.path.join(REPO, rel), os.path.join(bad_repo, rel))
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": home}), \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", err):
            self.assertEqual(audit_lib.main(["apply-safe"], repo=bad_repo), 2)
        self.assertIn("--manifest", err.getvalue())

    def test_run_cli_from_this_worktree_refuses(self):
        """`fixtures.run_cli` always drives the real installed
        `claude-setup-audit` against `audit_lib.REPO`'s own default. That is
        a linked worktree when THIS suite itself runs from one -- true in
        working from a linked worktree, but not a property
        of the code, and false in CI's primary checkout, where apply-safe
        correctly proceeds and this assumption made the test fail on green
        code (same pattern as `test_from_worktree.py`'s own skip). The
        refusal itself stays covered either way, by the fixture-worktree
        test above, which builds its own linked worktree regardless of
        where this suite happens to run from."""
        if not audit_lib.is_linked_worktree(REPO):
            self.skipTest("not running from a linked worktree")
        home = self._home()
        rc, out, err = fixtures.run_cli(["apply-safe"], home)
        self.assertEqual(rc, 3, out + err)
        self.assertIn("linked git worktree", out + err, out + err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
