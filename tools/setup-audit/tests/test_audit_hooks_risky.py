#!/usr/bin/env python3
"""Tests for `audit_lib`'s hook classification and risky rows.

Every sandbox is a fresh, empty `tempfile.mkdtemp()` -- nothing here
touches the real machine. Most assertions call `audit_lib.build_report`
in-process with `home=<sandbox>` and a clean `environ={}`; the tests
that exercise a real process-environment escape hatch or the CLI's own output
go through `fixtures.run_cli`, a real subprocess with a hermetic environment
(`fixtures.env_for`).

`CLAUDE_TOOLKIT_CONFIG` is forced to a guaranteed-nonexistent path below,
before any test runs -- same reasoning as `test_audit_links.py`.
"""

import json
import os
import re
import shutil
import stat
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
# The hooksPath check honours GIT_CONFIG_GLOBAL, as setup-git.sh does. An
# ambient value would point it at the runner's own git config; tests that
# exercise it set it explicitly with mock.patch.dict.
os.environ.pop("GIT_CONFIG_GLOBAL", None)

import fixtures  # noqa: E402  (first: it sets up the import path)
import audit_lib  # noqa: E402
import secret_lib  # noqa: E402


def _legacy():
    """The repo's legacy hook commands, `__HOME__` unexpanded."""
    path = os.path.join(REPO, "claude", "legacy-hook-commands.json")
    with open(path) as handle:
        return json.load(handle)["commands"]


def _sub(command, home):
    return command.replace("__HOME__", home)


def _report(home, environ=None):
    return audit_lib.build_report(
        home=home, repo=REPO,
        environ=environ if environ is not None else {})


def _find_risky(rows, risky_id, where=None):
    """The one risky row matching `risky_id` (and `where`, when given).
    Fails loudly if there isn't exactly one -- callers use this to assert on
    `severity`, so a missing or duplicated row must not be silently ignored."""
    matches = [r for r in rows if r["id"] == risky_id
              and (where is None or r["where"] == where)]
    assert len(matches) == 1, "expected exactly one %r row, found %r" % (
        risky_id, matches)
    return matches[0]


class SandboxCase(unittest.TestCase):
    """A throwaway, empty HOME per test. Nothing here touches the real machine."""

    def _home(self):
        home = fixtures.sandbox_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home


class TestHookClassification(SandboxCase):
    """A legacy command is ours; everything else is theirs_kept."""

    def test_legacy_is_ours_and_foreign_is_kept_wherever_it_sits(self):
        home = self._home()
        legacy = _legacy()
        stop_command = _sub(legacy[0], home)
        pre_command = _sub(legacy[1], home)

        settings_path = os.path.join(home, ".claude", "settings.json")
        fixtures.write(settings_path, json.dumps({
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": stop_command}]}],
                "Notification": [{"hooks": [
                    {"type": "command", "command": "echo hi"}]}],
                "PreToolUse": [{"hooks": [
                    {"type": "command", "command": "/bin/echo custom"},
                    {"type": "command", "command": pre_command},
                ]}],
            }
        }))

        report = _report(home)
        rows = {(row["event"], row["command"]): row for row in report["hooks"]}

        self.assertEqual(rows[("Stop", stop_command)]["state"], "ours", rows)
        self.assertEqual(rows[("Notification", "echo hi")]["state"],
                         "theirs_kept", rows)
        # A foreign handler inside an event the toolkit also uses is kept.
        self.assertEqual(rows[("PreToolUse", "/bin/echo custom")]["state"],
                         "theirs_kept", rows)
        self.assertEqual(rows[("PreToolUse", pre_command)]["state"],
                         "ours", rows)


class TestUnrunnableHooks(SandboxCase):
    """Not-executable and missing hooks, plus a positive control, through the
    real `unrunnable_hooks`."""

    def test_not_executable_and_missing(self):
        home = self._home()
        script = os.path.join(home, "hook.sh")
        fixtures.write(script, "#!/bin/sh\necho hi\n")
        os.chmod(script, 0o644)
        not_exec_cmd = "%s --flag" % script
        missing_cmd = "%s/missing-hook" % home

        settings_path = os.path.join(home, ".claude", "settings.json")
        fixtures.write(settings_path, json.dumps({
            "hooks": {"Notification": [{"hooks": [
                {"type": "command", "command": not_exec_cmd},
                {"type": "command", "command": missing_cmd},
            ]}]}
        }))

        report = _report(home)
        rows = {row["command"]: row for row in report["hooks"]}
        self.assertEqual(rows[not_exec_cmd]["unrunnable"], "not executable", rows)
        self.assertEqual(rows[missing_cmd]["unrunnable"], "missing", rows)

        dead_rows = [row for row in report["risky"] if row["id"] == "dead_hook"]
        dead_wheres = [row["where"] for row in dead_rows]
        self.assertTrue(any(not_exec_cmd in w for w in dead_wheres), dead_wheres)
        self.assertTrue(any(missing_cmd in w for w in dead_wheres), dead_wheres)
        for row in dead_rows:
            self.assertEqual(row["severity"], "their_call", row)

    def test_executable_positive_control(self):
        home = self._home()
        script = os.path.join(home, "hook.sh")
        fixtures.write(script, "#!/bin/sh\necho hi\n")
        os.chmod(script, 0o755)

        settings_path = os.path.join(home, ".claude", "settings.json")
        fixtures.write(settings_path, json.dumps({
            "hooks": {"Notification": [{"hooks": [
                {"type": "command", "command": script}]}]}
        }))

        report = _report(home)
        row = next(r for r in report["hooks"] if r["command"] == script)
        self.assertIsNone(row["unrunnable"], row)
        self.assertFalse(
            any(r["id"] == "dead_hook" for r in report["risky"]), report["risky"])


class TestClassifyHooksMalformedTemplate(SandboxCase):
    """A template whose "hooks" isn't an object of lists raises."""

    def test_hooks_list_raises_audit_error(self):
        home = self._home()
        with self.assertRaises(audit_lib.AuditError):
            audit_lib.classify_hooks({}, {"hooks": []}, home)


class TestRiskyIndividualIds(SandboxCase):
    """One test per risky id, each with a positive control."""

    def test_hooks_disabled(self):
        home = self._home()
        fixtures.write(os.path.join(home, ".claude", "settings.json"),
                       json.dumps({"disableAllHooks": True}))
        report = _report(home)
        row = _find_risky(report["risky"], "hooks_disabled")
        self.assertEqual(row["severity"], "stop", row)

        report_control = _report(self._home())
        self.assertFalse(
            any(r["id"] == "hooks_disabled" for r in report_control["risky"]))

    def test_bypass_permissions(self):
        home = self._home()
        fixtures.write(os.path.join(home, ".claude", "settings.json"),
                       json.dumps({"permissions": {"defaultMode": "bypassPermissions"}}))
        report = _report(home)
        row = _find_risky(report["risky"], "bypass_permissions")
        self.assertEqual(row["severity"], "stop", row)

        report_control = _report(self._home())
        self.assertFalse(
            any(r["id"] == "bypass_permissions" for r in report_control["risky"]))

    def test_guard_bypass_env_via_settings(self):
        home = self._home()
        fixtures.write(os.path.join(home, ".claude", "settings.json"),
                       json.dumps({"env": {"DANGER_ALLOW": "1"}}))
        report = _report(home)
        row = _find_risky(report["risky"], "guard_bypass_env")
        self.assertEqual(row["severity"], "stop", row)

        report_control = _report(self._home())
        self.assertFalse(
            any(r["id"] == "guard_bypass_env" for r in report_control["risky"]))

    def test_guard_bypass_env_via_process_environment(self):
        home = self._home()
        rc, out, err = fixtures.run_cli(
            ["--json"], home, extra_env={"WORKTREE_ALLOW": "1"})
        self.assertEqual(rc, 0, out + err)
        report = json.loads(out)
        row = _find_risky(report["risky"], "guard_bypass_env",
                          where="process environment")
        self.assertEqual(row["severity"], "stop", row)

        rc2, out2, err2 = fixtures.run_cli(["--json"], home)
        self.assertEqual(rc2, 0, out2 + err2)
        report_control = json.loads(out2)
        self.assertFalse(
            any(r["id"] == "guard_bypass_env" for r in report_control["risky"]))

    def test_blanket_bash_allow(self):
        home = self._home()
        fixtures.write(os.path.join(home, ".claude", "settings.json"),
                       json.dumps({"permissions": {"allow": ["Bash(*)"]}}))
        report = _report(home)
        row = _find_risky(report["risky"], "blanket_bash_allow")
        self.assertEqual(row["severity"], "their_call", row)

        report_control = _report(self._home())
        self.assertFalse(
            any(r["id"] == "blanket_bash_allow" for r in report_control["risky"]))


class TestWorkspaceSettingsScanned(SandboxCase):
    """With a workspace root, its own `.claude/settings.json` and
    `settings.local.json` are scanned like the home settings."""

    def test_workspace_settings_and_local_settings_rows(self):
        home = self._home()
        ws = os.path.join(home, "workspace")
        shared = os.path.join(ws, ".claude", "settings.json")
        local = os.path.join(ws, ".claude", "settings.local.json")
        fixtures.write(shared, json.dumps({"disableAllHooks": True}))
        fixtures.write(local, json.dumps(
            {"permissions": {"defaultMode": "bypassPermissions"}}))

        report = audit_lib.build_report(
            home=home, repo=REPO, environ={}, workspace_root=ws)
        self.assertEqual(_find_risky(report["risky"], "hooks_disabled",
                                     where=shared)["severity"], "stop")
        self.assertEqual(_find_risky(report["risky"], "bypass_permissions",
                                     where=local)["severity"], "stop")

        # Positive control: no workspace root, no workspace rows.
        control = audit_lib.build_report(
            home=home, repo=REPO, environ={}, workspace_root=None)
        self.assertEqual(control["risky"], [], control["risky"])


class TestSeverityOrderingInCliOutput(SandboxCase):
    """A `stop` row must lead a
    `their_call` row in the human summary, and `--json` must carry the exact
    severity -- the property a severity-erasing mutation would break."""

    def test_stop_row_precedes_their_call_row(self):
        home = self._home()
        fixtures.write(os.path.join(home, ".claude", "settings.json"),
                       json.dumps({"permissions": {"allow": ["Bash(*)"]}}))

        rc, out, err = fixtures.run_cli(
            ["--json"], home, extra_env={"DANGER_ALLOW": "1"})
        self.assertEqual(rc, 0, out + err)
        report = json.loads(out)
        stop_row = _find_risky(report["risky"], "guard_bypass_env",
                               where="process environment")
        self.assertEqual(stop_row["severity"], "stop", stop_row)
        their_call_row = _find_risky(report["risky"], "blanket_bash_allow")
        self.assertEqual(their_call_row["severity"], "their_call", their_call_row)

        rc2, human_out, err2 = fixtures.run_cli(
            [], home, extra_env={"DANGER_ALLOW": "1"})
        self.assertEqual(rc2, 0, human_out + err2)
        self.assertIn("guard_bypass_env", human_out)
        self.assertIn("blanket_bash_allow", human_out)
        stop_index = human_out.index("guard_bypass_env")
        their_call_index = human_out.index("blanket_bash_allow")
        self.assertLess(stop_index, their_call_index, human_out)


# Assembled at runtime so no credential-shaped literal sits in this file:
# the repo's own pre-commit secret scan blocks one, correctly.
SECRET_VALUE = "AK" + "IA" + 'ABCDEFGHIJKLMNOP'


class TestPlaintextSecret(SandboxCase):
    """A secret in settings `env` and in a `~/.claude.json` MCP
    header, never leaking any fragment of the value into either output."""

    def test_secret_flagged_and_never_leaked(self):
        findings = secret_lib.scan_text(SECRET_VALUE)
        self.assertTrue(findings, "test secret value is not flagged by scan_text")

        home = self._home()
        fixtures.write(os.path.join(home, ".claude", "settings.json"),
                       json.dumps({"env": {"MY_KEY": SECRET_VALUE}}))
        fixtures.write(os.path.join(home, ".claude.json"), json.dumps({
            "mcpServers": {"srv": {"headers": {"Authorization": SECRET_VALUE}}}
        }))

        rc, out, err = fixtures.run_cli(["--json"], home)
        self.assertEqual(rc, 0, out + err)
        report = json.loads(out)
        secret_rows = [r for r in report["risky"] if r["id"] == "plaintext_secret"]
        self.assertGreaterEqual(len(secret_rows), 2, secret_rows)
        for row in secret_rows:
            self.assertEqual(row["severity"], "their_call", row)

        rc2, human_out, err2 = fixtures.run_cli([], home)
        self.assertEqual(rc2, 0, human_out + err2)

        self.assertNotIn(SECRET_VALUE, out)
        self.assertNotIn(SECRET_VALUE, human_out)
        for i in range(len(SECRET_VALUE) - 7):
            chunk = SECRET_VALUE[i:i + 8]
            self.assertNotIn(chunk, out, chunk)
            self.assertNotIn(chunk, human_out, chunk)

    def test_positive_control(self):
        report = _report(self._home())
        self.assertFalse(
            any(r["id"] == "plaintext_secret" for r in report["risky"]), report["risky"])


class TestGuardEscapeHatchDrift(unittest.TestCase):
    """Every escape-hatch-shaped name in the two guards is a subset
    of GUARD_ESCAPE_HATCHES."""

    def test_drift(self):
        # `$NAME`/`${NAME}` in a shell guard; a quoted "NAME" in a Python one
        # (`os.environ.get("DANGER_ALLOW")`). Both guards are Python now and
        # read their environment in a sibling library module, not the entry
        # point (guard_lib.py / worktree_lib.py), so every top-level file in
        # EACH guard's directory is scanned -- a new module reading a new
        # hatch cannot slip past by living in a new file.
        pattern = re.compile(r"(?:\$\{?|[\"'])([A-Z][A-Z0-9_]*ALLOW[A-Z0-9_]*)")
        by_guard = {}
        for guard in ("danger-guard", "worktree-guard"):
            guard_dir = os.path.join(REPO, "tools", guard)
            found = set()
            for entry in sorted(os.listdir(guard_dir)):
                path = os.path.join(guard_dir, entry)
                if not os.path.isfile(path):
                    continue
                with open(path) as handle:
                    found.update(m.group(1) for m in pattern.finditer(handle.read()))
            by_guard[guard] = found
        names = set().union(*by_guard.values())

        # One positive control per guard, on that guard's own names: a scan
        # that read nothing from one directory must not pass on the other's.
        # (danger-guard's corpus tool names the worktree hatches too, so a
        # check on the merged set could not tell.)
        self.assertIn("DANGER_ALLOW", by_guard["danger-guard"], by_guard)
        self.assertIn("WORKTREE_ALLOW", by_guard["worktree-guard"], by_guard)
        self.assertIn("WT_ALLOW_MAIN", by_guard["worktree-guard"], by_guard)

        unexpected = names - set(audit_lib.GUARD_ESCAPE_HATCHES)
        self.assertEqual(
            unexpected, set(),
            "found escape-hatch name(s) not in GUARD_ESCAPE_HATCHES: %s -- "
            "this is a BLOCKED finding, do not widen the constant"
            % sorted(unexpected))


class TestRealIntegrityModule(unittest.TestCase):
    """`audit_lib` reuses the real toolkit-integrity checks, never a copy."""

    def test_audit_lib_loaded_the_module_on_disk(self):
        loaded = os.path.realpath(audit_lib.toolkit_integrity_lib.__file__)
        real = os.path.realpath(os.path.join(REPO, fixtures.INTEGRITY_LIB))
        self.assertEqual(loaded, real)


class TestMalformedClaudeJson(SandboxCase):
    """A malformed `~/.claude.json` earns an UnreadableRow, exit 0."""

    def test_mcp_servers_is_a_list(self):
        home = self._home()
        fixtures.write(os.path.join(home, ".claude.json"),
                       json.dumps({"mcpServers": []}))
        report = _report(home)
        self.assertTrue(report["unreadable"], report)
        rc, out, err = fixtures.run_cli(["--json"], home)
        self.assertEqual(rc, 0, out + err)

    def test_projects_wrong_type_is_unreadable(self):
        # A malformed `projects` must never read as a clean machine.
        for bad in (["bad"], {"/p": "not-an-object"}):
            home = self._home()
            fixtures.write(os.path.join(home, ".claude.json"),
                           json.dumps({"projects": bad}))
            report = _report(home)
            self.assertTrue(report["unreadable"], (bad, report))
            self.assertIs(report["empty_ground"], False)
        # Positive control: a well-formed empty `projects` adds nothing.
        home = self._home()
        fixtures.write(os.path.join(home, ".claude.json"),
                       json.dumps({"projects": {}}))
        self.assertEqual(_report(home)["unreadable"], [])

    def test_mcp_server_headers_wrong_type(self):
        home = self._home()
        fixtures.write(os.path.join(home, ".claude.json"),
                       json.dumps({"mcpServers": {"x": {"headers": "y"}}}))
        report = _report(home)
        self.assertTrue(report["unreadable"], report)
        rc, out, err = fixtures.run_cli(["--json"], home)
        self.assertEqual(rc, 0, out + err)


class TestForeignHooksPath(SandboxCase):
    """A foreign `core.hooksPath` is flagged; the toolkit's own is not."""

    def _write_gitconfig(self, home, hooks_path):
        fixtures.write(os.path.join(home, ".gitconfig"),
                       "[core]\n\thooksPath = %s\n" % hooks_path)

    def test_foreign_hooks_path_flagged(self):
        home = self._home()
        self._write_gitconfig(home, "/tmp/some-other-hooks-dir")
        report = _report(home)
        row = _find_risky(report["risky"], "foreign_hooks_path")
        self.assertEqual(row["severity"], "their_call", row)

    def test_toolkit_hooks_path_not_flagged(self):
        home = self._home()
        our_dir = os.path.join(home, ".config", "claude-code-toolkit", "git-hooks")
        os.makedirs(our_dir, exist_ok=True)
        self._write_gitconfig(home, our_dir)
        report = _report(home)
        self.assertFalse(
            any(r["id"] == "foreign_hooks_path" for r in report["risky"]),
            report["risky"])

    def test_tilde_is_the_audited_home_not_the_process_home(self):
        # setup-git.sh expands a leading ~ against $HOME, the audited home.
        home = self._home()
        os.makedirs(os.path.join(home, ".config", "claude-code-toolkit", "git-hooks"))
        self._write_gitconfig(home, "~/.config/claude-code-toolkit/git-hooks")
        self.assertNotEqual(os.path.expanduser("~"), home)
        self.assertFalse(
            any(r["id"] == "foreign_hooks_path" for r in _report(home)["risky"]))
        # Positive control: a ~ path that is not the toolkit's is flagged.
        other = self._home()
        self._write_gitconfig(other, "~/elsewhere-hooks")
        _find_risky(_report(other)["risky"], "foreign_hooks_path")

    def test_git_config_global_is_the_config_read(self):
        # setup-git.sh reads and writes the file GIT_CONFIG_GLOBAL names, so
        # the audit must judge that one, not ~/.gitconfig.
        home = self._home()
        custom = os.path.join(home, "dotfiles", "gitconfig")
        fixtures.write(custom, "[core]\n\thooksPath = /tmp/some-other-hooks-dir\n")
        # Supplied through build_report's `environ`, the one source of
        # process-environment state risky_rows reads; os.environ stays clean.
        environ = {"GIT_CONFIG_GLOBAL": custom}
        self.assertNotIn("GIT_CONFIG_GLOBAL", os.environ)
        row = _find_risky(_report(home, environ)["risky"], "foreign_hooks_path")
        self.assertEqual(row["detail"], "/tmp/some-other-hooks-dir", row)
        # Positive control: the same file naming the toolkit's own dir.
        ours = os.path.join(home, ".config", "claude-code-toolkit", "git-hooks")
        os.makedirs(ours, exist_ok=True)
        fixtures.write(custom, "[core]\n\thooksPath = %s\n" % ours)
        self.assertFalse(any(r["id"] == "foreign_hooks_path"
                             for r in _report(home, environ)["risky"]))

    def test_the_process_environment_is_not_the_config_read(self):
        # The converse: a GIT_CONFIG_GLOBAL in os.environ that the caller's
        # `environ` does not carry is not the audited machine's config.
        home = self._home()
        custom = os.path.join(home, "dotfiles", "gitconfig")
        fixtures.write(custom, "[core]\n\thooksPath = /tmp/some-other-hooks-dir\n")
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": custom}):
            self.assertFalse(any(r["id"] == "foreign_hooks_path"
                                 for r in _report(home, {})["risky"]))


class TestMalformedUserHooksNeverRaiseOrHide(SandboxCase):
    """`_flatten_event_commands` iterated the
    user's own nested hook shapes unvalidated. `{"hooks": {"PreToolUse":
    [{"hooks": 1}]}}` raised a raw `TypeError` (`for hook in 1` is not
    iterable) -- exit 1, no report at all. `[{"hooks": "invalid"}]`
    iterated the STRING character by character, none of them dicts, and
    silently dropped the whole matcher -- an empty `unreadable` list, even
    though the settings file plainly was not well-formed. The rule:
    "Malformed input never raises and never hides" -- any wrong-typed
    piece earns an `UnreadableRow` naming the exact key path and is
    treated as absent, never raised, never silently skipped."""

    def _settings(self, home, hooks_value):
        fixtures.write(os.path.join(home, ".claude", "settings.json"),
                       json.dumps({"hooks": {"PreToolUse": hooks_value}}))

    def test_non_dict_matcher_is_reported(self):
        home = self._home()
        self._settings(home, [123])
        report = _report(home)  # must not raise
        self.assertTrue(
            any(r["path"] == os.path.join(home, ".claude", "settings.json") + "#hooks.PreToolUse[0]"
                for r in report["unreadable"]), report["unreadable"])
        self.assertIs(report["empty_ground"], False)

    def test_matcher_hooks_wrong_type_int_is_reported_not_raised(self):
        home = self._home()
        self._settings(home, [{"hooks": 1}])
        report = _report(home)  # used to raise TypeError (exit 1)
        self.assertTrue(
            any(r["path"] == os.path.join(home, ".claude", "settings.json") + "#hooks.PreToolUse[0].hooks"
                for r in report["unreadable"]), report["unreadable"])
        self.assertIs(report["empty_ground"], False)

    def test_matcher_hooks_wrong_type_string_is_reported_not_silently_dropped(self):
        home = self._home()
        self._settings(home, [{"hooks": "invalid"}])
        report = _report(home)  # used to silently drop, empty unreadable
        self.assertTrue(
            any(r["path"] == os.path.join(home, ".claude", "settings.json") + "#hooks.PreToolUse[0].hooks"
                for r in report["unreadable"]), report["unreadable"])
        self.assertIs(report["empty_ground"], False)

    def test_non_dict_hook_entry_is_reported(self):
        home = self._home()
        self._settings(home, [{"hooks": [1]}])
        report = _report(home)
        self.assertTrue(
            any(r["path"] == os.path.join(home, ".claude", "settings.json") + "#hooks.PreToolUse[0].hooks[0]"
                for r in report["unreadable"]), report["unreadable"])
        self.assertIs(report["empty_ground"], False)

    def test_hook_missing_command_becomes_a_row_keyed_by_its_json(self):
        """Supersedes the earlier rule that
        this shape "stays silently skipped". It is still not malformed (no
        `UnreadableRow`), but it must no longer vanish from the inventory:
        `command` becomes a stable `json.dumps(hook, sort_keys=True)`."""
        home = self._home()
        hook = {"type": "command"}
        self._settings(home, [{"hooks": [hook]}])
        report = _report(home)
        self.assertEqual(report["unreadable"], [], report["unreadable"])
        rows = [r for r in report["hooks"] if r["event"] == "PreToolUse"]
        self.assertEqual(len(rows), 1, report["hooks"])
        self.assertEqual(rows[0]["command"], json.dumps(hook, sort_keys=True))
        self.assertIsNone(rows[0]["unrunnable"], rows[0])

    def test_positive_control_valid_matcher_list_has_no_unreadable_row(self):
        home = self._home()
        self._settings(home, [{"matcher": "*", "hooks": [
            {"type": "command", "command": "/bin/echo hi"}]}])
        report = _report(home)
        self.assertEqual(report["unreadable"], [], report["unreadable"])
        self.assertIn(
            ("PreToolUse", "/bin/echo hi"),
            {(r["event"], r["command"]) for r in report["hooks"]},
            report["hooks"])


class TestNonCommandHookBecomesARow(SandboxCase):
    """A hook dict with no string
    `command` -- a Stop prompt hook is the confirmed real-world shape --
    used to be silently dropped from the inventory entirely, so a settings
    file whose only custom hook was shaped like this read `empty_ground`
    true, and `install.sh` then removed it with nothing reported. Every
    well-formed hook dict is now a `HookRow`: `command` is its own command
    string when present, else a stable `json.dumps(hook, sort_keys=True)`."""

    def test_stop_prompt_hook_becomes_a_theirs_kept_row(self):
        home = self._home()
        prompt_hook = {"type": "prompt", "prompt": "x"}
        fixtures.write(os.path.join(home, ".claude", "settings.json"), json.dumps(
            {"hooks": {"Stop": [{"hooks": [prompt_hook]}]}}))
        report = _report(home)

        self.assertIs(report["empty_ground"], False, report)
        rows = [r for r in report["hooks"] if r["event"] == "Stop"]
        self.assertEqual(len(rows), 1, report["hooks"])
        self.assertEqual(rows[0]["command"], json.dumps(prompt_hook, sort_keys=True))
        self.assertEqual(rows[0]["state"], "theirs_kept", rows[0])
        self.assertIsNone(rows[0]["unrunnable"], rows[0])

    def test_positive_control_legacy_command_hook_reads_ours(self):
        home = self._home()
        stop_command = _sub(_legacy()[0], home)
        fixtures.write(os.path.join(home, ".claude", "settings.json"), json.dumps(
            {"hooks": {"Stop": [{"hooks": [
                {"type": "command", "command": stop_command}]}]}}))
        report = _report(home)

        rows = [r for r in report["hooks"] if r["event"] == "Stop"]
        self.assertEqual(len(rows), 1, report["hooks"])
        self.assertEqual(rows[0]["command"], stop_command, rows[0])
        self.assertEqual(rows[0]["state"], "ours", rows[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
