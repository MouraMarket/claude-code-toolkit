#!/usr/bin/env python3
"""Tests for `audit_lib`'s links, theirs-only, instruction files, status line
and CLI.

Every sandbox is a fresh, empty `tempfile.mkdtemp()` -- nothing here
touches the real machine. Most assertions call `audit_lib.build_report`
in-process with `home=<sandbox>` and a clean `environ={}`; a few that
exercise the CLI itself go through `fixtures.run_cli`, a real subprocess with
a hermetic environment (`fixtures.env_for`).

`CLAUDE_TOOLKIT_CONFIG` is forced to a guaranteed-nonexistent path below,
before any test runs. `toolkit_config` caches its config path at import time,
so this is the only point that can make `toolkit_config.workspace_root()`
hermetic for an in-process call -- otherwise the "plain" half of this file's
double run (tests must pass with no config at all) would read whatever config.json happens to exist on the
machine running the suite. `fixtures.run_cli`'s subprocess env is already
hermetic on its own via `fixtures.env_for`, so this only matters for the
direct `build_report()` calls below.
"""

import json
import os
import shutil
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

import fixtures  # noqa: E402  (first: it sets up the import path)
import audit_lib  # noqa: E402


def _report(home):
    return audit_lib.build_report(home=home, repo=REPO, environ={})


def _link_row(home):
    """The first `link`-kind manifest row -- any one will do; picked from the
    real manifest rather than hardcoded, so a renamed tool cannot go stale."""
    for row in audit_lib.load_manifest(REPO, home):
        if row["kind"] == "link":
            return row
    raise AssertionError("no link-kind row in the manifest")


def _find_link(report, dest):
    for row in report["links"]:
        if row["dest"] == dest:
            return row
    raise AssertionError("no link row for %r" % dest)


class SandboxCase(unittest.TestCase):
    """A throwaway, empty HOME per test. Nothing here touches the real machine."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home


class TestEmptySandboxIsEmptyGround(SandboxCase):
    """An empty sandbox is exit-0, empty_ground, every link absent."""

    def test_empty_sandbox_reads_empty_ground(self):
        home = self._home()
        report = _report(home)
        self.assertTrue(report["empty_ground"], report)
        self.assertTrue(report["links"], "no link rows at all -- manifest broke")
        self.assertTrue(
            all(row["state"] == "absent" for row in report["links"]),
            report["links"])
        self.assertEqual(report["status_line"], "absent", report)
        self.assertEqual(report["theirs_only"], [])
        self.assertEqual(report["unreadable"], [])


class TestLinkStates(SandboxCase):
    """One state per test, using a real manifest destination/target."""

    def _assert_positive_control(self):
        report = _report(self._home())
        self.assertTrue(report["empty_ground"], report)

    def test_linked(self):
        home = self._home()
        row = _link_row(home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        os.symlink(row["target"], row["dest"])

        report = _report(home)
        link = _find_link(report, row["dest"])
        self.assertEqual(link["state"], "linked", link)
        self.assertEqual(link["points_to"], row["target"], link)
        self.assertFalse(link["will_repoint"], link)
        self.assertFalse(report["empty_ground"], report)
        self._assert_positive_control()

    def test_dangling(self):
        home = self._home()
        row = _link_row(home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        os.symlink(os.path.join(home, "does-not-exist"), row["dest"])

        report = _report(home)
        link = _find_link(report, row["dest"])
        self.assertEqual(link["state"], "dangling", link)
        self.assertFalse(report["empty_ground"], report)
        self._assert_positive_control()

    def test_linked_elsewhere(self):
        home = self._home()
        row = _link_row(home)
        elsewhere = os.path.join(home, "elsewhere-file")
        fixtures.write(elsewhere, "not the repo copy\n")
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        os.symlink(elsewhere, row["dest"])

        report = _report(home)
        link = _find_link(report, row["dest"])
        self.assertEqual(link["state"], "linked_elsewhere", link)
        self.assertTrue(link["will_repoint"], link)
        self.assertFalse(report["empty_ground"], report)
        self._assert_positive_control()

    def test_identical(self):
        home = self._home()
        row = _link_row(home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        shutil.copy2(row["target"], row["dest"])

        report = _report(home)
        link = _find_link(report, row["dest"])
        self.assertEqual(link["state"], "identical", link)
        self.assertFalse(report["empty_ground"], report)
        self._assert_positive_control()

    def test_differs(self):
        home = self._home()
        row = _link_row(home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        shutil.copy2(row["target"], row["dest"])
        with open(row["dest"], "ab") as handle:
            handle.write(b"\n# one extra byte for the differs test\n")

        report = _report(home)
        link = _find_link(report, row["dest"])
        self.assertEqual(link["state"], "differs", link)
        self.assertFalse(report["empty_ground"], report)
        self._assert_positive_control()


class TestTheirsOnly(SandboxCase):
    """Theirs-only rows and will_prune, mirroring install.sh's two
    prune loops exactly."""

    def test_theirs_only_rows_and_will_prune(self):
        home = self._home()

        mine_md = os.path.join(home, ".claude", "commands", "mine.md")
        fixtures.write(mine_md, "# mine\n")

        claude_mine = os.path.join(home, ".local", "bin", "claude-mine")
        fixtures.write(claude_mine, "#!/bin/sh\necho mine\n")

        uv = os.path.join(home, ".local", "bin", "uv")
        fixtures.write(uv, "#!/bin/sh\necho uv\n")

        # A dangling symlink INTO the repo, at a file the repo does not have.
        retired = os.path.join(home, ".claude", "commands", "retired.md")
        os.makedirs(os.path.dirname(retired), exist_ok=True)
        os.symlink(os.path.join(REPO, "commands", "retired.md"), retired)

        # Positive control for the SAME row shape: a symlink to something
        # that exists (not a repo path at all), so nothing is prunable.
        live_link = os.path.join(home, ".claude", "commands", "still-live.md")
        live_target = os.path.join(home, "still-live-source.md")
        fixtures.write(live_target, "# still live\n")
        os.symlink(live_target, live_link)

        # ~/bin symlink into the repo whose basename matches its target's.
        git_tidy = os.path.join(home, "bin", "git-tidy")
        os.makedirs(os.path.dirname(git_tidy), exist_ok=True)
        os.symlink(os.path.join(REPO, "tools", "git-tidy", "git-tidy"), git_tidy)

        report = _report(home)
        rows = {row["path"]: row for row in report["theirs_only"]}

        self.assertIn(mine_md, rows)
        self.assertEqual(rows[mine_md]["area"], "commands")
        self.assertFalse(rows[mine_md]["will_prune"], rows[mine_md])

        self.assertIn(claude_mine, rows)
        self.assertEqual(rows[claude_mine]["area"], "local_bin")
        self.assertFalse(rows[claude_mine]["will_prune"], rows[claude_mine])

        self.assertNotIn(uv, rows, "uv should not be listed at all")

        self.assertIn(retired, rows)
        self.assertTrue(rows[retired]["will_prune"], rows[retired])

        self.assertIn(live_link, rows)
        self.assertFalse(rows[live_link]["will_prune"], rows[live_link])

        self.assertIn(git_tidy, rows)
        self.assertEqual(rows[git_tidy]["area"], "home_bin")
        self.assertTrue(rows[git_tidy]["will_prune"], rows[git_tidy])

        self.assertFalse(report["empty_ground"], report)

    def test_local_bin_repo_link_will_prune_only_when_dangling(self):
        """`install.sh` prunes a dangling `~/.local/bin` link into this repo; a
        live one is kept."""
        home = self._home()
        local_bin = os.path.join(home, ".local", "bin")
        os.makedirs(local_bin, exist_ok=True)
        dangling = os.path.join(local_bin, "claude-retired")
        os.symlink(os.path.join(REPO, "tools", "retired", "claude-retired"), dangling)
        live = os.path.join(local_bin, "claude-live")
        os.symlink(os.path.join(REPO, "install.sh"), live)

        rows = {row["path"]: row for row in _report(home)["theirs_only"]}

        self.assertEqual(rows[dangling]["area"], "local_bin")
        self.assertTrue(rows[dangling]["will_prune"], rows[dangling])
        self.assertFalse(rows[live]["will_prune"], rows[live])

    def test_owned_legacy_link_will_prune_live_or_dangling_and_foreign_does_not(self):
        """The four `~/.claude` dirs hold only legacy links now, and
        `install.sh` removes every one that points into this repo -- live or
        dangling. A link to somewhere else is theirs."""
        home = self._home()
        commands = os.path.join(home, ".claude", "commands")
        os.makedirs(commands, exist_ok=True)

        owned_live = os.path.join(commands, "build.md")
        os.symlink(os.path.join(REPO, "install.sh"), owned_live)
        owned_dangling = os.path.join(commands, "retired.md")
        os.symlink(os.path.join(REPO, "commands", "retired.md"), owned_dangling)
        foreign_live = os.path.join(commands, "foreign.md")
        foreign_target = os.path.join(home, "foreign-source.md")
        fixtures.write(foreign_target, "# foreign\n")
        os.symlink(foreign_target, foreign_live)

        rows = {row["path"]: row for row in _report(home)["theirs_only"]}

        self.assertTrue(rows[owned_live]["will_prune"], rows[owned_live])
        self.assertTrue(rows[owned_dangling]["will_prune"], rows[owned_dangling])
        self.assertFalse(rows[foreign_live]["will_prune"], rows[foreign_live])

    def test_positive_control(self):
        report = _report(self._home())
        self.assertTrue(report["empty_ground"], report)


class TestMalformedSettings(SandboxCase):
    """Each shape alone, exit 0, non-empty unreadable, empty_ground
    false -- never a crash."""

    def _assert_malformed(self, body):
        home = self._home()
        fixtures.write(os.path.join(home, ".claude", "settings.json"), body)
        report = _report(home)
        self.assertTrue(report["unreadable"], report)
        self.assertFalse(report["empty_ground"], report)

    def test_settings_is_a_list(self):
        self._assert_malformed("[]")

    def test_settings_is_null(self):
        self._assert_malformed("null")

    def test_hooks_event_value_is_not_a_list(self):
        self._assert_malformed('{"hooks": {"PreToolUse": {}}}')

    def test_permissions_is_not_an_object(self):
        self._assert_malformed('{"permissions": []}')

    def test_env_is_not_an_object(self):
        self._assert_malformed('{"env": "x"}')

    def test_positive_control(self):
        report = _report(self._home())
        self.assertTrue(report["empty_ground"], report)


class TestInstructionFilesAndStatusLine(SandboxCase):
    """A home CLAUDE.md, and a custom statusLine, each alone."""

    def test_home_claude_md_alone(self):
        home = self._home()
        claude_md = os.path.join(home, ".claude", "CLAUDE.md")
        fixtures.write(claude_md, "# hello\n")

        report = _report(home)
        self.assertFalse(report["empty_ground"], report)
        row = next(r for r in report["instruction_files"] if r["path"] == claude_md)
        self.assertTrue(row["present"], row)
        self.assertFalse(row["symlink"], row)
        self.assertGreater(row["bytes"], 0, row)
        self.assertEqual(row["lines"], 1, row)

    def test_custom_status_line_alone(self):
        home = self._home()
        settings_path = os.path.join(home, ".claude", "settings.json")
        fixtures.write(settings_path, json.dumps(
            {"statusLine": {"type": "command", "command": "echo hi"}}))

        report = _report(home)
        self.assertEqual(report["status_line"], "theirs", report)
        self.assertFalse(report["empty_ground"], report)

    def test_positive_control(self):
        report = _report(self._home())
        self.assertTrue(report["empty_ground"], report)


class TestInstructionFilesWorkspaceRoot(SandboxCase):
    """The `instruction_files` workspace-root branch, exercised
    directly with an explicit sandbox `workspace_root` -- never a real one."""

    def test_workspace_root_rows_present_and_absent(self):
        home = self._home()
        ws = os.path.join(home, "_workspace")
        os.makedirs(ws, exist_ok=True)
        ws_claude_md = os.path.join(ws, "CLAUDE.md")
        fixtures.write(ws_claude_md, "# workspace claude\n")
        # TOOLING.md is deliberately left absent.

        unreadable = []
        rows = {r["path"]: r for r in
                audit_lib.instruction_files(home, ws, unreadable)}

        ws_tooling_md = os.path.join(ws, "TOOLING.md")
        self.assertIn(ws_claude_md, rows)
        claude_row = rows[ws_claude_md]
        self.assertTrue(claude_row["present"], claude_row)
        self.assertFalse(claude_row["symlink"], claude_row)
        self.assertGreater(claude_row["bytes"], 0, claude_row)
        self.assertEqual(claude_row["lines"], 1, claude_row)

        self.assertIn(ws_tooling_md, rows)
        tooling_row = rows[ws_tooling_md]
        self.assertFalse(tooling_row["present"], tooling_row)
        self.assertFalse(tooling_row["symlink"], tooling_row)
        self.assertEqual(tooling_row["bytes"], 0, tooling_row)
        self.assertEqual(tooling_row["lines"], 0, tooling_row)

        # The home-level row is still there regardless of workspace_root.
        self.assertIn(os.path.join(home, ".claude", "CLAUDE.md"), rows)
        self.assertEqual(unreadable, [])

    def test_symlinked_workspace_file_reports_symlink_true(self):
        home = self._home()
        ws = os.path.join(home, "_workspace")
        os.makedirs(ws, exist_ok=True)
        referent = os.path.join(home, "real-tooling.md")
        fixtures.write(referent, "# real tooling\nsecond line\n")
        ws_tooling_md = os.path.join(ws, "TOOLING.md")
        os.symlink(referent, ws_tooling_md)

        unreadable = []
        rows = {r["path"]: r for r in
                audit_lib.instruction_files(home, ws, unreadable)}

        row = rows[ws_tooling_md]
        self.assertTrue(row["present"], row)
        self.assertTrue(row["symlink"], row)
        self.assertGreater(row["bytes"], 0, row)
        self.assertEqual(row["lines"], 2, row)

    def test_build_report_workspace_root_keyword_affects_empty_ground(self):
        """`build_report`'s `workspace_root` keyword bypasses `toolkit_config`
        entirely: a present `<ws>/CLAUDE.md` makes `empty_ground`
        false; the same sandbox with the file absent (positive control) reads
        `empty_ground` true."""
        home = self._home()
        ws = os.path.join(home, "_workspace_absent")
        os.makedirs(ws, exist_ok=True)
        report = audit_lib.build_report(
            home=home, repo=REPO, environ={}, workspace_root=ws)
        self.assertTrue(report["empty_ground"], report)

        home2 = self._home()
        ws2 = os.path.join(home2, "_workspace_present")
        os.makedirs(ws2, exist_ok=True)
        fixtures.write(os.path.join(ws2, "CLAUDE.md"), "# workspace claude\n")
        report2 = audit_lib.build_report(
            home=home2, repo=REPO, environ={}, workspace_root=ws2)
        self.assertFalse(report2["empty_ground"], report2)
        row = next(r for r in report2["instruction_files"]
                    if r["path"] == os.path.join(ws2, "CLAUDE.md"))
        self.assertTrue(row["present"], row)


class TestInvalidJsonSettings(SandboxCase):
    """Invalid JSON settings.json -> an unreadable row, no crash."""

    def test_invalid_json(self):
        home = self._home()
        fixtures.write(os.path.join(home, ".claude", "settings.json"), "{not json")

        report = _report(home)
        self.assertTrue(report["unreadable"], report)
        self.assertFalse(report["empty_ground"], report)

    def test_positive_control(self):
        report = _report(self._home())
        self.assertTrue(report["empty_ground"], report)


class TestMainAuditError(unittest.TestCase):
    """A broken manifest source turns into exit 2, never a crash.

    `main` has no `home` parameter (`main(argv, repo=REPO) -> int`),
    and `build_report` defaults `home` to `os.path.expanduser("~")` at call
    time -- i.e. whatever `os.environ["HOME"]` is when `main` runs. Patching
    it here is the only way to keep this call from resolving to the
    real machine home, even though the fixture `install.sh` never touches the
    filesystem either way.
    """

    def _fixture_repo(self, install_sh_body):
        # Carries the repo's real hook template and worktree helper, so the
        # manifest is the ONLY thing that can fail: without them both tests
        # passed on the template's (or helper's) own AuditError, whatever
        # load_manifest did (found by mutation testing).
        root = tempfile.mkdtemp(prefix="setup-audit-fixture-repo-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        fixtures.write(os.path.join(root, "install.sh"), install_sh_body)
        for rel in (os.path.join("claude", "settings.host.json"),
                    os.path.join("claude", "legacy-hook-commands.json"),
                    os.path.join("plugin", "hooks", "hooks.json"),
                    os.path.join("lib", "worktree_detect.sh")):
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            shutil.copy2(os.path.join(fixtures.REPO, rel), os.path.join(root, rel))
        return root

    def _sandbox_home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        self.assertNotEqual(home, fixtures.REAL_HOME)
        return home

    def _main(self, repo, home):
        import contextlib
        import io
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HOME": home}), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = audit_lib.main(["--json"], repo=repo)
        return rc, err.getvalue()

    def test_nonzero_exit(self):
        repo = self._fixture_repo("#!/bin/sh\nexit 1\n")
        rc, err = self._main(repo, self._sandbox_home())
        self.assertEqual(rc, 2)
        self.assertIn("--manifest", err)

    def test_nonzero_exit_after_link_rows(self):
        # A run that printed rows and then failed is a partial manifest:
        # the exit code alone must refuse it, not the zero-rows check.
        repo = self._fixture_repo(
            '#!/bin/sh\nprintf \'link\\t%s\\t%s\\n\' "$0" "$HOME/.local/bin/x"\nexit 1\n')
        rc, err = self._main(repo, self._sandbox_home())
        self.assertEqual(rc, 2)
        self.assertIn("exited 1", err)

    def test_zero_link_rows(self):
        repo = self._fixture_repo("#!/bin/sh\nexit 0\n")
        rc, err = self._main(repo, self._sandbox_home())
        self.assertEqual(rc, 2)
        self.assertIn("no link rows", err)

    def test_positive_control_one_link_row_returns_0(self):
        repo = self._fixture_repo(
            '#!/bin/sh\nprintf \'link\\t%s\\t%s\\n\' "$0" "$HOME/.local/bin/x"\n')
        rc, err = self._main(repo, self._sandbox_home())
        self.assertEqual(rc, 0, err)


class TestHumanSummary(unittest.TestCase):
    """The human summary names "empty ground" only when it applies."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def test_empty_sandbox_says_empty_ground(self):
        home = self._home()
        rc, out, err = fixtures.run_cli([], home)
        self.assertEqual(rc, 0, out + err)
        self.assertIn("empty ground", out)

    def test_linked_fixture_does_not_say_empty_ground(self):
        home = self._home()
        row = _link_row(home)
        os.makedirs(os.path.dirname(row["dest"]), exist_ok=True)
        os.symlink(row["target"], row["dest"])

        rc, out, err = fixtures.run_cli([], home)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn("empty ground", out)


class TestSameContentWalkErrors(unittest.TestCase):
    """A directory the walk cannot enter is a problem, never `identical`."""

    def _trees(self, dest_text, source_text):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        dirs = []
        for name, text in (("dest", dest_text), ("source", source_text)):
            sub = os.path.join(root, name, "sub")
            os.makedirs(sub)
            with open(os.path.join(sub, "f.txt"), "w") as handle:
                handle.write(text)
            dirs.append((os.path.join(root, name), sub))
        return dirs

    def test_readable_identical_trees_are_identical(self):
        (dest, _), (source, _) = self._trees("a", "a")
        self.assertEqual(audit_lib._same_content(dest, source), (True, []))

    def test_unreadable_subdirectory_is_not_identical(self):
        if os.geteuid() == 0:
            self.skipTest("root reads through mode 0o000")
        (dest, dest_sub), (source, source_sub) = self._trees("a", "b")
        for sub in (dest_sub, source_sub):
            os.chmod(sub, 0o000)
            self.addCleanup(os.chmod, sub, 0o755)
        same, problems = audit_lib._same_content(dest, source)
        self.assertIs(same, False)
        self.assertTrue(problems)
        self.assertTrue(any(p["path"].endswith("sub") for p in problems), problems)


if __name__ == "__main__":
    unittest.main(verbosity=2)
