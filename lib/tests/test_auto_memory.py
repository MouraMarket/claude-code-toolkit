#!/usr/bin/env python3
"""Tests for auto_memory.py, the one resolver behind `autoMemoryDirectory`.

Every function here is total and never raises, so these tests assert return
values for every boundary the module's docstrings promise, not just the happy
path. Hermetic: no test reads the real `~` or `~/.claude/settings.json`.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import auto_memory


class PlanTestCase(unittest.TestCase):
    """One test per row of the `plan()` table, plus present-null in both modes."""

    def test_on_set_absent_sets(self):
        self.assertEqual(
            auto_memory.plan(auto_memory.ABSENT, True, "~/target"),
            ("set", "~/target"))

    def test_on_set_matches_ok(self):
        self.assertEqual(
            auto_memory.plan("~/target", True, "~/target"),
            ("ok", "~/target"))

    def test_on_set_other_is_foreign(self):
        self.assertEqual(
            auto_memory.plan("/some/other", True, "~/target"),
            ("foreign", "/some/other"))

    def test_on_none_absent_skips(self):
        self.assertEqual(
            auto_memory.plan(auto_memory.ABSENT, True, None),
            ("skip", None))

    def test_on_none_other_is_foreign(self):
        self.assertEqual(
            auto_memory.plan("/some/other", True, None),
            ("foreign", "/some/other"))

    def test_off_set_matches_removes(self):
        self.assertEqual(
            auto_memory.plan("~/target", False, "~/target"),
            ("remove", "~/target"))

    def test_off_any_absent_ok(self):
        self.assertEqual(
            auto_memory.plan(auto_memory.ABSENT, False, "~/target"),
            ("ok", None))
        self.assertEqual(
            auto_memory.plan(auto_memory.ABSENT, False, None),
            ("ok", None))

    def test_off_set_other_is_foreign(self):
        self.assertEqual(
            auto_memory.plan("/some/other", False, "~/target"),
            ("foreign", "/some/other"))

    def test_off_none_other_is_foreign(self):
        self.assertEqual(
            auto_memory.plan("/some/other", False, None),
            ("foreign", "/some/other"))

    def test_present_null_on_is_foreign(self):
        self.assertEqual(
            auto_memory.plan(None, True, "~/target"),
            ("foreign", None))

    def test_present_null_off_is_foreign(self):
        self.assertEqual(
            auto_memory.plan(None, False, "~/target"),
            ("foreign", None))


class ProjectSlugLiteralTestCase(unittest.TestCase):
    """Independent literal-expectation checks (Finding 1): every other slug
    test compares `project_slug` against itself or against the same regex, so
    a regex change (e.g. one that keeps dots and spaces) passes unnoticed.
    These assert a hand-written literal instead. Both paths are nonexistent,
    so `os.path.realpath` leaves them unchanged and the literal is exact."""

    def test_measured_harness_key_shape(self):
        self.assertEqual(
            auto_memory.project_slug("/Users/x/_Work"),
            "-Users-x--Work")

    def test_dots_and_spaces_are_replaced(self):
        self.assertEqual(
            auto_memory.project_slug("/nonexistent/a_b.c d"),
            "-nonexistent-a-b-c-d")


class ManagedTargetTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        # A real temp dir whose name contains "_" and "." so the slug build is
        # exercised against a realistic, non-alphanumeric path.
        self.workspace = os.path.join(self.home, "work_space.root")
        os.makedirs(self.workspace)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_workspace_root_configured(self):
        self.assertEqual(
            auto_memory.managed_target(None, self.home),
            (None, "no workspace_root configured"))
        self.assertEqual(
            auto_memory.managed_target("", self.home),
            (None, "no workspace_root configured"))

    def test_non_string_workspace_root_returns_not_a_directory(self):
        # Finding 2: a workspace_root that is truthy but not a str (e.g. a
        # JSON list from a malformed config) must not reach `.startswith` --
        # that raised AttributeError and made a real `install.sh` apply exit
        # 1. Falsy non-strings (`[]`, `{}`, `0`, `None`) already return "no
        # workspace_root configured" via the `not workspace_root` check above,
        # so only truthy non-strings exercise the new type check.
        for value in (["workspace"], {"a": 1}, 5, True):
            self.assertEqual(
                auto_memory.managed_target(value, self.home),
                (None, "workspace_root is not a directory"),
                "value=%r" % (value,))

    def test_workspace_root_not_a_directory(self):
        missing = os.path.join(self.home, "does-not-exist")
        self.assertEqual(
            auto_memory.managed_target(missing, self.home),
            (None, "workspace_root is not a directory"))

    def test_slug_over_200_chars_is_hashed_by_the_harness(self):
        long_dir = os.path.join(self.home, "x" * 250)
        os.makedirs(long_dir)
        target, reason = auto_memory.managed_target(long_dir, self.home)
        self.assertIsNone(target)
        self.assertEqual(reason, "project key over 200 chars is hashed by the harness")

    def test_no_session_has_run_yet(self):
        target, reason = auto_memory.managed_target(self.workspace, self.home)
        self.assertIsNone(target)
        slug = auto_memory.project_slug(self.workspace)
        self.assertEqual(
            reason,
            "no session has run in workspace_root yet "
            "(~/.claude/projects/%s missing)" % slug)

    def test_success(self):
        slug = auto_memory.project_slug(self.workspace)
        os.makedirs(os.path.join(self.home, ".claude", "projects", slug))
        target, reason = auto_memory.managed_target(self.workspace, self.home)
        self.assertIsNone(reason)
        self.assertEqual(target, "~/.claude/projects/%s/memory" % slug)

    def test_expandvars_expands_env_vars_in_workspace_root(self):
        # Merge Review finding 2: `toolkit_config` paths honour `$VARS` via
        # `os.path.expandvars`, but `managed_target` only expanded `~` -- so a
        # configured `workspace_root: "$N5_WS"` silently failed the isdir
        # check below instead of resolving. `$N5_WS` must resolve to the same
        # outcome as passing the literal path.
        slug = auto_memory.project_slug(self.workspace)
        os.makedirs(os.path.join(self.home, ".claude", "projects", slug))
        with mock.patch.dict(os.environ, {"N5_WS": self.workspace}):
            target, reason = auto_memory.managed_target("$N5_WS", self.home)
        literal_target, literal_reason = auto_memory.managed_target(
            self.workspace, self.home)
        self.assertIsNone(reason)
        self.assertEqual((target, reason), (literal_target, literal_reason))

    def test_expands_leading_tilde_against_home_not_expanduser(self):
        # A "~/" workspace_root must resolve against the `home` argument, not
        # os.path.expanduser -- otherwise the test isn't hermetic.
        rel = "work_space.root"
        slug = auto_memory.project_slug(self.workspace)
        os.makedirs(os.path.join(self.home, ".claude", "projects", slug))
        target, reason = auto_memory.managed_target("~/" + rel, self.home)
        self.assertIsNone(reason)
        self.assertEqual(target, "~/.claude/projects/%s/memory" % slug)


class ExpandTestCase(unittest.TestCase):

    def setUp(self):
        self.home = "/Users/fixture-home"

    def test_none_and_empty_and_non_string(self):
        self.assertIsNone(auto_memory.expand(None, self.home))
        self.assertIsNone(auto_memory.expand("", self.home))
        self.assertIsNone(auto_memory.expand(123, self.home))

    def test_dotdot_normalises_to_root(self):
        self.assertIsNone(auto_memory.expand("/tmp/../", self.home))

    def test_tilde_dotdot_name_is_valid(self):
        self.assertEqual(
            auto_memory.expand("~/..cache", self.home),
            self.home + "/..cache")

    def test_tilde_dotdot_slash_x_is_rejected(self):
        self.assertIsNone(auto_memory.expand("~/../x", self.home))

    def test_bare_tilde_slash_is_rejected(self):
        self.assertIsNone(auto_memory.expand("~/", self.home))

    def test_tilde_a_slash_strips_trailing_slash(self):
        self.assertEqual(
            auto_memory.expand("~/a/", self.home),
            self.home + "/a")

    def test_relative_path_is_rejected(self):
        self.assertIsNone(auto_memory.expand("rel/x", self.home))

    def test_nul_byte_is_rejected(self):
        self.assertIsNone(auto_memory.expand("/tmp/\x00x", self.home))

    def test_under_three_characters_is_rejected(self):
        self.assertIsNone(auto_memory.expand("/a", self.home))
        self.assertEqual(auto_memory.expand("/ab", self.home), "/ab")


class UserSettingTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        os.makedirs(os.path.join(self.home, ".claude"))
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, raw):
        with open(self.settings_path, "w") as handle:
            handle.write(raw)

    def test_missing_file(self):
        self.assertIsNone(auto_memory.user_setting(self.home))

    def test_bad_json(self):
        self.write("{not json")
        self.assertIsNone(auto_memory.user_setting(self.home))

    def test_list_json(self):
        self.write("[]")
        self.assertIsNone(auto_memory.user_setting(self.home))

    def test_non_string_value(self):
        self.write(json.dumps({"autoMemoryDirectory": 42}))
        self.assertIsNone(auto_memory.user_setting(self.home))

    def test_absent_key(self):
        self.write(json.dumps({"other": "value"}))
        self.assertIsNone(auto_memory.user_setting(self.home))

    def test_happy_path(self):
        self.write(json.dumps({"autoMemoryDirectory": "~/.claude/projects/x/memory"}))
        self.assertEqual(
            auto_memory.user_setting(self.home),
            "~/.claude/projects/x/memory")


class ResolveUserDirTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        os.makedirs(os.path.join(self.home, ".claude"))
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data):
        with open(self.settings_path, "w") as handle:
            json.dump(data, handle)

    def test_dir_present(self):
        target = os.path.join(self.home, "memdir")
        os.makedirs(target)
        self.write({"autoMemoryDirectory": "~/memdir"})
        self.assertEqual(auto_memory.resolve_user_dir(self.home), target)

    def test_dir_absent(self):
        self.write({"autoMemoryDirectory": "~/nonexistent-memdir"})
        self.assertIsNone(auto_memory.resolve_user_dir(self.home))


class ConfiguredDirTestCase(unittest.TestCase):
    """`configured_dir` -- Merge Review finding 1: "configured but missing"
    must not read as "not configured". Unlike `resolve_user_dir`, a
    missing-but-valid path is still returned."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        os.makedirs(os.path.join(self.home, ".claude"))
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data):
        with open(self.settings_path, "w") as handle:
            json.dump(data, handle)

    def test_missing_but_valid_path_is_still_returned(self):
        target = os.path.join(self.home, "not-yet-created")
        self.write({"autoMemoryDirectory": "~/not-yet-created"})
        self.assertFalse(os.path.isdir(target))
        self.assertEqual(auto_memory.configured_dir(self.home), target)

    def test_absent_setting_is_none(self):
        self.assertIsNone(auto_memory.configured_dir(self.home))

    def test_invalid_value_is_none(self):
        self.write({"autoMemoryDirectory": "~/../x"})
        self.assertIsNone(auto_memory.configured_dir(self.home))


class UnifyEnabledTestCase(unittest.TestCase):

    class FakeConfig:
        def __init__(self, value):
            self._value = value

        def get(self, *keys, default=None):
            if self._value is _UNSET:
                return default
            return self._value

    def test_absent_is_true(self):
        config_module = self.FakeConfig(_UNSET)
        self.assertTrue(auto_memory.unify_enabled(config_module))

    def test_literal_false_is_false(self):
        config_module = self.FakeConfig(False)
        self.assertIs(auto_memory.unify_enabled(config_module), False)

    def test_zero_is_true(self):
        config_module = self.FakeConfig(0)
        self.assertTrue(auto_memory.unify_enabled(config_module))

    def test_none_module_is_true(self):
        self.assertTrue(auto_memory.unify_enabled(None))


_UNSET = object()


class StubConfig:
    """A config module stand-in: `get(*keys, default=None)` over a nested dict."""

    def __init__(self, data):
        self.data = data

    def get(self, *keys, default=None):
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node if node is not None else default


class RaisingConfig:
    def get(self, *keys, default=None):
        raise RuntimeError("config blew up")


def independent_slug(path):
    """Slug computed without `auto_memory.project_slug`, so a regex change in
    the code under test cannot rewrite its own expectation."""
    return "".join(
        c if (c.isascii() and c.isalnum()) else "-"
        for c in os.path.realpath(path))


class StatusFixture(unittest.TestCase):
    """A temp home with a workspace dir; helpers to seed the projects dir and
    settings file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = self._tmp.name
        self.workspace = os.path.join(self.home, "work_space.root")
        os.makedirs(self.workspace)
        self.slug = independent_slug(self.workspace)
        self.target = "~/.claude/projects/%s/memory" % self.slug

    def tearDown(self):
        self._tmp.cleanup()

    def seed_projects_dir(self):
        os.makedirs(os.path.join(self.home, ".claude", "projects", self.slug))

    def write_settings(self, text):
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        with open(os.path.join(self.home, ".claude", "settings.json"), "w",
                  encoding="utf-8") as handle:
            handle.write(text)

    def config(self, **extra):
        data = {"workspace_root": self.workspace}
        data.update(extra)
        return StubConfig(data)


class TestStatus(StatusFixture):

    def test_set(self):
        self.seed_projects_dir()
        self.write_settings("{}")
        self.assertEqual(
            auto_memory.status(self.home, self.config()),
            ("set", self.target, None, self.workspace))

    def test_ok(self):
        self.seed_projects_dir()
        self.write_settings(json.dumps({"autoMemoryDirectory": self.target}))
        self.assertEqual(
            auto_memory.status(self.home, self.config()),
            ("ok", self.target, None, self.workspace))

    def test_ok_unmanaged(self):
        self.seed_projects_dir()
        self.write_settings("{}")
        cfg = self.config(auto_memory={"unify": False})
        self.assertEqual(
            auto_memory.status(self.home, cfg),
            ("ok", None, None, self.workspace))

    def test_remove(self):
        self.seed_projects_dir()
        self.write_settings(json.dumps({"autoMemoryDirectory": self.target}))
        cfg = self.config(auto_memory={"unify": False})
        self.assertEqual(
            auto_memory.status(self.home, cfg),
            ("remove", self.target, None, self.workspace))

    def test_skip_no_session_yet(self):
        self.write_settings("{}")
        action, detail, reason, root = auto_memory.status(
            self.home, self.config())
        self.assertEqual(action, "skip")
        self.assertIsNone(detail)
        self.assertIn("no session has run in workspace_root yet", reason)
        self.assertEqual(root, self.workspace)

    def test_foreign(self):
        self.seed_projects_dir()
        self.write_settings(json.dumps({"autoMemoryDirectory": "/somewhere/else"}))
        self.assertEqual(
            auto_memory.status(self.home, self.config()),
            ("foreign", "/somewhere/else", None, self.workspace))

    def test_foreign_null(self):
        self.seed_projects_dir()
        self.write_settings('{"autoMemoryDirectory": null}')
        self.assertEqual(
            auto_memory.status(self.home, self.config()),
            ("foreign", None, None, self.workspace))

    def test_missing_settings_file_is_empty(self):
        self.seed_projects_dir()
        self.assertEqual(
            auto_memory.status(self.home, self.config()),
            ("set", self.target, None, self.workspace))

    def test_error_invalid_json(self):
        self.seed_projects_dir()
        self.write_settings("{not json")
        action, detail, reason, root = auto_memory.status(
            self.home, self.config())
        self.assertEqual(action, "error")
        self.assertIsNone(detail)
        self.assertIsInstance(reason, str)
        self.assertTrue(reason)
        self.assertEqual(root, self.workspace)

    def test_error_non_object(self):
        self.seed_projects_dir()
        self.write_settings("[1, 2]")
        action, detail, reason, root = auto_memory.status(
            self.home, self.config())
        self.assertEqual(action, "error")
        self.assertIsNone(detail)
        # Named for what it is, not caught later as an unexpected failure.
        self.assertEqual(reason, "settings.json is not a JSON object")

    def test_skip_no_workspace_root(self):
        self.assertEqual(
            auto_memory.status(self.home, StubConfig({})),
            ("skip", None, "no workspace_root configured", None))

    def test_skip_not_a_directory(self):
        missing = os.path.join(self.home, "nope")
        self.assertEqual(
            auto_memory.status(self.home, StubConfig({"workspace_root": missing})),
            ("skip", None, "workspace_root is not a directory", missing))

    def test_non_string_workspace_root_reports_none(self):
        self.assertEqual(
            auto_memory.status(self.home, StubConfig({"workspace_root": ["x"]})),
            ("skip", None, "workspace_root is not a directory", None))

    def test_raising_config_never_raises(self):
        # unify_enabled treats a raising config as ON; the workspace_root read
        # degrades to "not configured".
        self.assertEqual(
            auto_memory.status(self.home, RaisingConfig()),
            ("skip", None, "no workspace_root configured", None))

    def test_none_config_never_raises(self):
        self.assertEqual(
            auto_memory.status(self.home, None),
            ("skip", None, "no workspace_root configured", None))

    def test_status_actions_constant(self):
        self.assertEqual(
            auto_memory.STATUS_ACTIONS,
            ("ok", "set", "remove", "skip", "foreign", "error"))


AUTO_MEMORY_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "auto_memory.py")


def snapshot_tree(root):
    """Every path under root, with size and mtime for files."""
    found = {}
    for current, dirs, files in os.walk(root):
        for name in dirs:
            found[os.path.join(current, name)] = "dir"
        for name in files:
            full = os.path.join(current, name)
            stat = os.stat(full)
            found[full] = (stat.st_size, stat.st_mtime_ns)
    return found


class TestStatusCli(StatusFixture):

    def run_cli(self, *args, config_data=None):
        config_path = os.path.join(self._tmp.name + "-config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"workspace_root": self.workspace} if config_data is None
                else config_data, handle)
        self.addCleanup(lambda: os.path.exists(config_path)
                        and os.remove(config_path))
        env = dict(os.environ)
        env["HOME"] = self.home
        env["USERPROFILE"] = self.home
        env["CLAUDE_TOOLKIT_CONFIG"] = config_path
        # macOS Python caches bytecode under ~/Library/Caches, i.e. inside the
        # fixture home; that is the interpreter writing, not the CLI.
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTHONPYCACHEPREFIX", None)
        return subprocess.run(
            [sys.executable, AUTO_MEMORY_PY] + list(args),
            env=env, stdin=subprocess.DEVNULL, capture_output=True,
            text=True, cwd=tempfile.gettempdir())

    def test_set_case_prints_one_json_line_and_writes_nothing(self):
        self.seed_projects_dir()
        self.write_settings("{}")
        before = snapshot_tree(self.home)
        proc = self.run_cli("--status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("\n"), 1)
        self.assertEqual(json.loads(proc.stdout), {
            "action": "set", "detail": self.target, "reason": None,
            "workspace_root": self.workspace})
        self.assertEqual(snapshot_tree(self.home), before)

    def test_skip_case(self):
        self.write_settings("{}")
        before = snapshot_tree(self.home)
        proc = self.run_cli("--status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.count("\n"), 1)
        result = json.loads(proc.stdout)
        self.assertEqual(result["action"], "skip")
        self.assertIsNone(result["detail"])
        self.assertIn("no session has run in workspace_root yet",
                      result["reason"])
        self.assertEqual(result["workspace_root"], self.workspace)
        self.assertEqual(snapshot_tree(self.home), before)

    def test_reads_settings_from_home_not_cwd(self):
        # The cwd (a temp dir) holds no settings; a foreign value in HOME's
        # settings must still be reported.
        self.seed_projects_dir()
        self.write_settings(json.dumps({"autoMemoryDirectory": "/x/y"}))
        proc = self.run_cli("--status")
        self.assertEqual(json.loads(proc.stdout)["action"], "foreign")

    def test_invalid_settings_json_is_error_exit_0(self):
        self.seed_projects_dir()
        self.write_settings("{not json")
        proc = self.run_cli("--status")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["action"], "error")

    def test_unknown_argument_exits_2(self):
        proc = self.run_cli("--bogus")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("usage", proc.stderr.lower())

    def test_no_argument_exits_2(self):
        self.assertEqual(self.run_cli().returncode, 2)


if __name__ == "__main__":
    unittest.main()
