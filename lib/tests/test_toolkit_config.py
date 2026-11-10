#!/usr/bin/env python3
"""Tests for the config reader every tool depends on.

The contract under test is "never raises, always has a default". These run
inside hooks, so a config typo must degrade to a documented fallback rather than
take a hook down — and for the danger guard specifically, the fallback must be
the strict direction.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import toolkit_config


class ConfigTestCase(unittest.TestCase):

    def setUp(self):
        self._original = toolkit_config.CONFIG_PATH
        toolkit_config._cache = None

    def tearDown(self):
        toolkit_config.CONFIG_PATH = self._original
        toolkit_config._cache = None

    def write(self, data, raw=None):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as handle:
            handle.write(raw if raw is not None else json.dumps(data))
        self.addCleanup(os.unlink, path)
        toolkit_config.CONFIG_PATH = path
        toolkit_config._cache = None
        return path


class TestLoad(ConfigTestCase):

    def test_reads_a_valid_file(self):
        self.write({"owner_name": "Sam"})
        self.assertEqual(toolkit_config.load()["owner_name"], "Sam")

    def test_missing_file_is_empty_dict(self):
        toolkit_config.CONFIG_PATH = "/nope/config.json"
        self.assertEqual(toolkit_config.load(), {})

    def test_invalid_json_is_empty_dict(self):
        self.write(None, raw="{not json at all")
        self.assertEqual(toolkit_config.load(), {})

    def test_non_dict_top_level_is_empty_dict(self):
        self.write(["a", "list"])
        self.assertEqual(toolkit_config.load(), {})

    def test_refresh_rereads(self):
        path = self.write({"owner_name": "First"})
        self.assertEqual(toolkit_config.get("owner_name"), "First")
        with open(path, "w") as handle:
            json.dump({"owner_name": "Second"}, handle)
        self.assertEqual(toolkit_config.get("owner_name"), "First")  # cached
        toolkit_config.load(refresh=True)
        self.assertEqual(toolkit_config.get("owner_name"), "Second")


class TestLoadStatus(ConfigTestCase):
    """load_status() tells a broken config from an absent one; load() cannot."""

    def test_missing_file_is_empty_and_not_an_error(self):
        toolkit_config.CONFIG_PATH = "/nope/config.json"
        self.assertEqual(toolkit_config.load_status(), ({}, None))

    def test_valid_object_is_returned_without_error(self):
        self.write({"profiles": ["builder"]})
        self.assertEqual(toolkit_config.load_status(),
                         ({"profiles": ["builder"]}, None))

    def test_empty_object_is_not_an_error(self):
        self.write({})
        self.assertEqual(toolkit_config.load_status(), ({}, None))

    def test_truncated_json_is_an_error_naming_the_path(self):
        path = self.write(None, raw='{"profiles": ["bui')
        data, error = toolkit_config.load_status()
        self.assertEqual(data, {})
        self.assertTrue(error)
        self.assertIn(path, error)

    def test_non_object_root_is_an_error_naming_the_path(self):
        path = self.write([1, 2])
        data, error = toolkit_config.load_status()
        self.assertEqual(data, {})
        self.assertTrue(error)
        self.assertIn(path, error)

    def test_unreadable_file_is_an_error(self):
        # A directory at CONFIG_PATH: open() raises an OSError that is not
        # not-found.
        directory = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, directory)
        toolkit_config.CONFIG_PATH = directory
        data, error = toolkit_config.load_status()
        self.assertEqual(data, {})
        self.assertTrue(error)
        self.assertIn(directory, error)

    def test_never_touches_the_cache_when_it_is_none(self):
        self.write({"owner_name": "Sam"})
        toolkit_config._cache = None
        toolkit_config.load_status()
        self.assertIsNone(toolkit_config._cache)

    def test_never_touches_a_populated_cache(self):
        self.write({"owner_name": "Sam"})
        sentinel = {"owner_name": "cached"}
        toolkit_config._cache = sentinel
        toolkit_config.load_status()
        self.assertIs(toolkit_config._cache, sentinel)

    def test_rereads_every_call(self):
        path = self.write({"owner_name": "First"})
        self.assertEqual(toolkit_config.load_status()[0]["owner_name"], "First")
        with open(path, "w") as handle:
            json.dump({"owner_name": "Second"}, handle)
        self.assertEqual(toolkit_config.load_status()[0]["owner_name"], "Second")

    def test_load_still_folds_broken_into_empty(self):
        self.write(None, raw="{broken")
        self.assertEqual(toolkit_config.load(), {})
        self.write([1, 2])
        self.assertEqual(toolkit_config.load(), {})


class TestGet(ConfigTestCase):

    def test_nested_keys(self):
        self.write({"a": {"b": {"c": 42}}})
        self.assertEqual(toolkit_config.get("a", "b", "c"), 42)

    def test_missing_key_returns_default(self):
        self.write({"a": {}})
        self.assertEqual(toolkit_config.get("a", "b", default="fb"), "fb")

    def test_walking_into_a_non_dict_returns_default(self):
        self.write({"a": "string"})
        self.assertEqual(toolkit_config.get("a", "b", default="fb"), "fb")

    def test_explicit_null_returns_default(self):
        # null is how the example says "off", and must not shadow the default.
        self.write({"a": None})
        self.assertEqual(toolkit_config.get("a", default="fb"), "fb")


class TestGetPath(ConfigTestCase):

    def test_expands_tilde(self):
        self.write({"p": "~/somewhere"})
        self.assertEqual(toolkit_config.get_path("p"),
                         os.path.expanduser("~/somewhere"))

    def test_expands_env_vars(self):
        os.environ["_TOOLKIT_TEST_VAR"] = "/tmp/xyz"
        self.addCleanup(os.environ.pop, "_TOOLKIT_TEST_VAR", None)
        self.write({"p": "$_TOOLKIT_TEST_VAR/sub"})
        self.assertEqual(toolkit_config.get_path("p"), "/tmp/xyz/sub")

    def test_missing_returns_default_unexpanded(self):
        self.write({})
        self.assertEqual(toolkit_config.get_path("p", default="/fallback"),
                         "/fallback")

    def test_empty_string_returns_default(self):
        self.write({"p": ""})
        self.assertEqual(toolkit_config.get_path("p", default="/fallback"),
                         "/fallback")

    def test_non_string_returns_default(self):
        self.write({"p": 17})
        self.assertEqual(toolkit_config.get_path("p", default="/fallback"),
                         "/fallback")


class TestGetList(ConfigTestCase):

    def test_returns_strings(self):
        self.write({"l": ["a", "b"]})
        self.assertEqual(toolkit_config.get_list("l"), ["a", "b"])

    def test_drops_non_strings_and_blanks(self):
        self.write({"l": ["a", 1, None, "", "b"]})
        self.assertEqual(toolkit_config.get_list("l"), ["a", "b"])

    def test_missing_returns_empty(self):
        self.write({})
        self.assertEqual(toolkit_config.get_list("l"), [])

    def test_non_list_returns_empty(self):
        self.write({"l": "not a list"})
        self.assertEqual(toolkit_config.get_list("l"), [])


class TestGetInt(ConfigTestCase):

    def test_reads_an_int(self):
        self.write({"n": 250000})
        self.assertEqual(toolkit_config.get_int("n", default=1), 250000)

    def test_coerces_a_numeric_string(self):
        self.write({"n": "300"})
        self.assertEqual(toolkit_config.get_int("n", default=1), 300)

    def test_non_numeric_returns_default(self):
        self.write({"n": "many"})
        self.assertEqual(toolkit_config.get_int("n", default=99), 99)

    def test_bool_returns_default(self):
        # True would otherwise coerce to 1 and silently become a threshold.
        self.write({"n": True})
        self.assertEqual(toolkit_config.get_int("n", default=99), 99)

    def test_missing_returns_default(self):
        self.write({})
        self.assertEqual(toolkit_config.get_int("n", default=99), 99)

    def test_non_finite_returns_default(self):
        # json writes these as Infinity / -Infinity / NaN; int() raises
        # OverflowError on the first two, which used to escape at hook import.
        for value in (float("inf"), float("-inf"), float("nan")):
            self.write({"n": value})
            self.assertEqual(toolkit_config.get_int("n", default=99), 99, value)


class TestWellKnownDefaults(ConfigTestCase):

    def test_workspace_root_has_no_default(self):
        # Load-bearing: a guessed root would widen what the guard trusts.
        self.write({})
        self.assertIsNone(toolkit_config.workspace_root())

    def test_owner_name_falls_back_to_a_role(self):
        self.write({})
        self.assertEqual(toolkit_config.owner_name(), "the human")

    def test_owner_name_ignores_blank(self):
        self.write({"owner_name": "   "})
        self.assertEqual(toolkit_config.owner_name(), "the human")

    def test_handover_dir_defaults_to_its_own_directory(self):
        self.write({})
        self.assertEqual(toolkit_config.handover_dir(),
                         os.path.expanduser("~/.claude/handovers"))

    def test_no_roster_accessor(self):
        # Client-code session titling does not ship, and with it the roster.
        self.assertFalse(hasattr(toolkit_config, "roster_path"))

    def test_env_var_overrides_config_path(self):
        path = self.write({"owner_name": "FromFile"})
        os.environ["CLAUDE_TOOLKIT_CONFIG"] = path
        self.addCleanup(os.environ.pop, "CLAUDE_TOOLKIT_CONFIG", None)
        import importlib
        importlib.reload(toolkit_config)
        try:
            self.assertEqual(toolkit_config.CONFIG_PATH, path)
            self.assertEqual(toolkit_config.owner_name(), "FromFile")
        finally:
            os.environ.pop("CLAUDE_TOOLKIT_CONFIG", None)
            importlib.reload(toolkit_config)


class TestReposRoot(ConfigTestCase):
    """`repos_root()`: the one reader claude-wrap-up-survey and claude-backup share."""

    def setUp(self):
        super().setUp()
        os.environ["REPOS_ROOT_TEST_VAR"] = "/from/var"
        os.environ.pop("REPOS_ROOT_UNSET_VAR", None)
        self.addCleanup(os.environ.pop, "REPOS_ROOT_TEST_VAR", None)

    def resolve(self, value=None, workspace="/ws", set_key=True):
        data = {"workspace_root": workspace}
        if set_key:
            data["wrap_up"] = {"repos_root": value}
        self.write(data)
        return toolkit_config.repos_root()

    def test_unset_defaults_under_the_workspace_root(self):
        self.assertEqual(self.resolve(set_key=False), "/ws/repos")
        self.assertEqual(self.resolve(None), "/ws/repos")
        self.assertEqual(self.resolve(""), "/ws/repos")

    def test_unset_with_no_workspace_root_is_none(self):
        self.assertIsNone(self.resolve(set_key=False, workspace=None))

    def test_variables_and_tilde_are_expanded(self):
        self.assertEqual(self.resolve("$REPOS_ROOT_TEST_VAR/repos"), "/from/var/repos")
        self.assertEqual(self.resolve("${REPOS_ROOT_TEST_VAR}/repos"), "/from/var/repos")
        self.assertEqual(self.resolve("~/code"), os.path.expanduser("~/code"))

    def test_an_unusable_value_is_refused_not_defaulted(self):
        # Falling back to <workspace_root>/repos would survey a tree nobody named.
        for value in ("relative/repos", "$REPOS_ROOT_UNSET_VAR/repos",
                      "/$REPOS_ROOT_UNSET_VAR/repos", 7, ["/a"], True):
            with self.subTest(value=value):
                self.assertIsNone(self.resolve(value))


class TestBuildReviewMode(ConfigTestCase):

    def test_no_file_falls_back_to_economical(self):
        toolkit_config.CONFIG_PATH = "/nope/config.json"
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_no_build_object_falls_back_to_economical(self):
        self.write({})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_no_review_key_falls_back_to_economical(self):
        self.write({"build": {}})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_null_review_falls_back_to_economical(self):
        self.write({"build": {"review": None}})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_unrecognised_string_falls_back_to_economical(self):
        # A typo must never select the expensive mode that needs Codex.
        self.write({"build": {"review": "tier1"}})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_non_string_falls_back_to_economical(self):
        self.write({"build": {"review": 42}})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_list_value_falls_back_to_economical(self):
        self.write({"build": {"review": []}})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_wrong_case_falls_back_to_economical(self):
        self.write({"build": {"review": "Full"}})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_full_aliases_to_full_power(self):
        self.write({"build": {"review": "full"}})
        self.assertEqual(toolkit_config.build_review_mode(), "full-power")

    def test_exactly_economical_returns_economical(self):
        self.write({"build": {"review": "economical"}})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_exactly_full_power_returns_full_power(self):
        self.write({"build": {"review": "full-power"}})
        self.assertEqual(toolkit_config.build_review_mode(), "full-power")

    def test_full_power_spoken_form_aliases_to_full_power(self):
        self.write({"build": {"review": "full power"}})
        self.assertEqual(toolkit_config.build_review_mode(), "full-power")

    def test_wrong_case_full_power_falls_back_to_economical(self):
        self.write({"build": {"review": "Full-Power"}})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")

    def test_trailing_space_falls_back_to_economical(self):
        self.write({"build": {"review": "full-power "}})
        self.assertEqual(toolkit_config.build_review_mode(), "economical")


class TestExampleConfigMatchesTheCode(ConfigTestCase):
    """`config.example.json` EXECUTES -- install.sh copies it verbatim onto a
    no-TTY install, so a shipped value that drifts from the code default is
    not a doc nit, it is a silent override on every machine. This is
    the test that stops a future edit shipping "full-power" to every no-TTY install.
    """

    def _shipped_config(self):
        lib_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        root = os.path.dirname(lib_dir)
        example = os.path.join(root, "config.example.json")
        with open(example) as handle:
            return json.load(handle)

    def test_shipped_example_names_no_one(self):
        # install.sh copies the example verbatim on a no-TTY install; a live
        # placeholder there became the name every guard block addressed.
        self.write(self._shipped_config())
        self.assertEqual(toolkit_config.owner_name(), "the human")

    def test_shipped_example_sets_no_backup_or_repos_folder(self):
        # A sample path here would replace <workspace_root>/repos, and name a
        # backup folder that does not exist, on every no-TTY install.
        shipped = self._shipped_config()
        self.assertIsNone(shipped["wrap_up"]["repos_root"])
        self.assertIsNone(shipped["backup"]["destination"])
        shipped["workspace_root"] = "/ws"
        self.write(shipped)
        self.assertEqual(toolkit_config.repos_root(), "/ws/repos")

    def test_shipped_build_review_matches_the_code_default(self):
        toolkit_config.CONFIG_PATH = "/nope/config.json"
        shipped = self._shipped_config()
        self.assertEqual(shipped["build"]["review"],
                         toolkit_config.build_review_mode())


class TestConfigIsDecodedAsUtf8(unittest.TestCase):
    """load() opened the config with the ambient locale's encoding.
    Under a non-UTF-8 locale (cp1252 on Windows, ASCII under LC_ALL=C) any
    non-ASCII value decoded wrong or raised, and load() swallowed the error
    and returned {} -- indistinguishable from a missing config.

    A subprocess is required because CONFIG_PATH and the locale are both
    fixed at interpreter start, so an in-process reload cannot exercise them
    independently of this test's own encoding.
    """

    NON_UTF8 = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0",
                "PYTHONCOERCECLOCALE": "0"}

    LIB_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.mkdtemp()
        cls.config_path = os.path.join(cls._dir, "config.json")
        with open(cls.config_path, "w", encoding="utf-8") as handle:
            json.dump({"owner_name": "Oʻahu Tester"}, handle, ensure_ascii=False)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls._dir, ignore_errors=True)

    def _load_in_subprocess(self, env):
        import subprocess
        script = (
            "import sys, json\n"
            "sys.path.insert(0, %r)\n"
            "import toolkit_config\n"
            "print(json.dumps(toolkit_config.load()))\n"
        ) % self.LIB_DIR
        proc = subprocess.run(
            ["python3", "-c", script], capture_output=True, text=True,
            env=dict(os.environ, CLAUDE_TOOLKIT_CONFIG=self.config_path, **env))
        return json.loads(proc.stdout)

    def test_non_ascii_config_loads_under_a_non_utf8_locale(self):
        import subprocess
        # Positive control: the locale override must actually take the
        # subprocess's interpreter off UTF-8, or this test cannot fail.
        probe = subprocess.run(
            ["python3", "-c",
             "import locale; print(locale.getpreferredencoding(False))"],
            capture_output=True, text=True,
            env=dict(os.environ, **self.NON_UTF8))
        if probe.stdout.strip().lower().replace("-", "") == "utf8":
            self.skipTest("this interpreter defaults to UTF-8 under LC_ALL=C")
        self.assertEqual(
            self._load_in_subprocess(self.NON_UTF8)["owner_name"],
            "Oʻahu Tester")

    def test_same_config_loads_under_utf8(self):
        # Control for the fixture itself: the config must load correctly
        # when the locale is not in play.
        self.assertEqual(
            self._load_in_subprocess({"PYTHONUTF8": "1"})["owner_name"],
            "Oʻahu Tester")


class TestExpandPath(unittest.TestCase):

    def _with_env(self, **values):
        """Set (str) or unset (None) os.environ keys; restored on cleanup."""
        for key, value in values.items():
            saved = os.environ.get(key)
            self.addCleanup(
                lambda k=key, v=saved: os.environ.pop(k, None) if v is None
                else os.environ.__setitem__(k, v))
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    @unittest.skipIf(os.name == "nt", "parity is with POSIX expanduser/expandvars")
    def test_default_call_matches_stdlib(self):
        paths = ["~", "~/a", "~/", "$HOME/a", "${HOME}/a", "$TK_PARITY/a",
                 "${TK_PARITY}/b", "$TK_UNSET_XYZ/a", "a/$/b", "${}/a",
                 "plain/path", "/abs/path"]
        home_states = {"set /h": "/h", "set /h/": "/h/", "set /": "/",
                       "set empty": "", "unset": None}
        for state, home_value in home_states.items():
            for p in paths:
                with self.subTest(path=p, home_state=state):
                    self._with_env(HOME=home_value, TK_PARITY="/p",
                                   TK_UNSET_XYZ=None)
                    want = os.path.expanduser(os.path.expandvars(p))
                    self.assertEqual(toolkit_config.expand_path(p), want)
                    self.assertEqual(toolkit_config.expand(p), want)

    def test_injected_home_agrees_across_spellings(self):
        for home in ["/h", "/h/"]:
            for spelling in ["~/x", "$HOME/x", "${HOME}/x"]:
                with self.subTest(home=home, spelling=spelling):
                    self.assertEqual(toolkit_config.expand_path(
                        spelling, home=home, environ={}), "/h/x")
            with self.subTest(home=home, spelling="~"):
                self.assertEqual(
                    toolkit_config.expand_path("~", home=home, environ={}), "/h")
        self.assertEqual(
            toolkit_config.expand_path("~/x", home="/", environ={}), "/x")
        self.assertEqual(
            toolkit_config.expand_path("~", home="/", environ={}), "/")
        self.assertEqual(
            toolkit_config.expand_path("$HOME/x", home="/", environ={}), "/x")
        self.assertEqual(
            toolkit_config.expand_path("${HOME}", home="/", environ={}), "/")

    def test_empty_variable_never_becomes_root(self):
        self.assertEqual(toolkit_config.expand_path(
            "$TK_EMPTY", home="/h", environ={"TK_EMPTY": ""}), "")
        self.assertIsNone(toolkit_config.expand_path(
            "$TK_EMPTY", home="/h", environ={"TK_EMPTY": ""}, strict=True))

    def test_strict_collapses_separators_a_variable_composes(self):
        # Guards compare config paths as text: "$R/tmp/x" with R="/" must be
        # /tmp/x, not //tmp/x. Non-strict keeps stdlib's "//" for parity.
        self.assertEqual(toolkit_config.expand_path(
            "$R/tmp/x", home="/h", environ={"R": "/"}, strict=True), "/tmp/x")
        self.assertEqual(toolkit_config.expand_path(
            "$D/x", home="/h", environ={"D": "/data/"}, strict=True), "/data/x")
        self.assertEqual(toolkit_config.expand_path(
            "$R/tmp/x", home="/h", environ={"R": "/"}), "//tmp/x")

    def test_collapse_separators(self):
        collapse = toolkit_config._collapse_separators
        self.assertEqual(collapse("/a//b///c", windows=False), "/a/b/c")
        self.assertEqual(collapse("C:\\w\\\\x", windows=False), "C:\\w\\\\x")
        # R="C:\work\" composes "C:\work\/protected".
        self.assertEqual(collapse("C:\\work\\/protected", windows=True),
                         "C:\\work\\protected")
        self.assertEqual(collapse("C:/work//protected", windows=True),
                         "C:/work/protected")
        # A UNC prefix keeps its leading pair.
        self.assertEqual(collapse("\\\\srv\\\\share", windows=True),
                         "\\\\srv\\share")
        self.assertEqual(collapse("//srv//share", windows=True), "//srv/share")

    def test_windows_default_call_keeps_native_expansion(self):
        # On Windows, expand() must keep %VAR% (native
        # ntpath.expandvars); only explicit guard-style calls use $NAME alone.
        from unittest import mock
        with mock.patch.object(toolkit_config.os, "name", "nt"), \
                mock.patch.object(toolkit_config, "_native_expand",
                                  return_value="NATIVE") as native:
            self.assertEqual(toolkit_config.expand("%USERPROFILE%/Work"),
                             "NATIVE")
            native.assert_called_once_with("%USERPROFILE%/Work")
            native.reset_mock()
            toolkit_config.expand_path("$V/x", home="/h", environ={"V": "/v"},
                                       strict=True)
            toolkit_config.expand_path("$V/x", environ={"V": "/v"})
            native.assert_not_called()

    def test_injected_home_wins_over_environ_home(self):
        self.assertEqual(toolkit_config.expand_path(
            "$HOME/x", home="/h", environ={"HOME": "/other"}), "/h/x")

    def test_injected_environ(self):
        for spelling in ["$V/x", "${V}/x"]:
            self.assertEqual(toolkit_config.expand_path(
                spelling, home="/h", environ={"V": "/v"}), "/v/x")

    def test_given_environ_never_consults_os_environ(self):
        self._with_env(TK_LEAK="/leak")
        self.assertEqual(toolkit_config.expand_path(
            "$TK_LEAK/x", home="/h", environ={}), "$TK_LEAK/x")

    def test_strict(self):
        self.assertIsNone(toolkit_config.expand_path(
            "$NOPE/x", home="/h", environ={}, strict=True))
        self.assertIsNone(toolkit_config.expand_path(
            "~nosuchuser_tk_xyz/x", home="/h", environ={}, strict=True))
        self.assertEqual(toolkit_config.expand_path(
            "$V/x", home="/h", environ={"V": "/v"}, strict=True), "/v/x")
        self.assertEqual(toolkit_config.expand_path(
            "$NOPE/x", home="/h", environ={}), "$NOPE/x")

    def test_empty_inputs(self):
        self.assertIsNone(toolkit_config.expand_path(None))
        self.assertIsNone(toolkit_config.expand_path(""))


class TestSpecsDirCommand(ConfigTestCase):
    """`toolkit_config.py specs-dir`: the one resolver /build and /handover call."""

    LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    SCRIPT = os.path.join(LIB, "toolkit_config.py")
    REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

    def setUp(self):
        super().setUp()
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, True)

    def run_cli(self, args, data=None, raw=None, script=None, cwd=None,
                extra_env=None, missing=False, config_path=None):
        if config_path:
            path = config_path
        elif missing:
            path = os.path.join(self.home, "no-such-config.json")
        else:
            fd, path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as handle:
                handle.write(raw if raw is not None else json.dumps(data))
            self.addCleanup(os.unlink, path)
        env = dict(os.environ)
        env.pop("SPECS_UNSET_VAR", None)
        env["CLAUDE_TOOLKIT_CONFIG"] = path
        env["HOME"] = self.home
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, script or self.SCRIPT] + list(args),
            env=env, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)

    @staticmethod
    def cfg(value, **archive):
        return {"spec_archive": dict({"specs_dir": value}, **archive)}

    def test_absolute_path_printed(self):
        result = self.run_cli(["specs-dir"], self.cfg("/abs/specs"))
        self.assertEqual((result.stdout, result.stderr, result.returncode),
                         ("/abs/specs\n", "", 0))

    def test_non_ascii_path_prints_as_utf8_under_an_ascii_locale(self):
        """A workspace under a HOME with an \u02bbokina must print, not crash,
        when the ambient stdout encoding is ASCII (LC_ALL=C, or cp1252 on
        Windows)."""
        target = "/abs/Hawai\u02bbi/specs"
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self.cfg(target), handle)
        self.addCleanup(os.unlink, path)
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=path, HOME=self.home,
                   PYTHONIOENCODING="ascii")
        result = subprocess.run([sys.executable, self.SCRIPT, "specs-dir"], env=env,
                                capture_output=True, stdin=subprocess.DEVNULL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode("utf-8"), target + "\n")

    def test_tilde_expands_against_home(self):
        result = self.run_cli(["specs-dir"], self.cfg("~/x/specs"))
        self.assertEqual(result.stdout, os.path.join(self.home, "x/specs") + "\n")
        self.assertEqual(result.returncode, 0)

    def test_env_var_expands(self):
        result = self.run_cli(["specs-dir"], self.cfg("$SPECS_TEST_ROOT/specs"),
                              extra_env={"SPECS_TEST_ROOT": "/abs/root"})
        self.assertEqual(result.stdout, "/abs/root/specs\n")
        self.assertEqual(result.returncode, 0)

    def test_unusable_values_are_cwd_independent(self):
        for value in ("specs", "$SPECS_UNSET_VAR/specs", "/$SPECS_UNSET_VAR/specs"):
            outcomes = []
            # One config file for both runs, so stderr (which names it) can match.
            fd, config_path = tempfile.mkstemp(suffix=".json")
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps(self.cfg(value)))
            self.addCleanup(os.unlink, config_path)
            for _ in range(2):
                cwd = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, cwd, True)
                result = self.run_cli(["specs-dir"], cwd=cwd,
                                      config_path=config_path)
                outcomes.append((result.stdout, result.stderr, result.returncode))
            with self.subTest(value=value):
                self.assertEqual(outcomes[0], outcomes[1])
                stdout, stderr, code = outcomes[0]
                self.assertEqual(stdout, "")
                self.assertEqual(code, 3)
                self.assertIn("not an absolute path", stderr)
                self.assertEqual(len(stderr.strip().splitlines()), 1)

    def test_unset_cases_exit_three(self):
        cases = {
            "key absent": dict(data={"spec_archive": {}}),
            "section absent": dict(data={}),
            "empty string": dict(data=self.cfg("")),
            "null": dict(data=self.cfg(None)),
            "number": dict(data=self.cfg(7)),
            "config missing": dict(missing=True),
            "config not json": dict(raw="{not json"),
        }
        for name, kwargs in cases.items():
            with self.subTest(case=name):
                result = self.run_cli(["specs-dir"], **kwargs)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.returncode, 3)
                self.assertIn("spec_archive.specs_dir", result.stderr)
                self.assertEqual(len(result.stderr.strip().splitlines()), 1)

    def test_bad_arguments_exit_two(self):
        for args in ([], ["bogus"], ["specs-dir", "extra"]):
            with self.subTest(args=args):
                result = self.run_cli(args, self.cfg("/abs/specs"))
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")
                self.assertIn("usage: toolkit_config.py specs-dir", result.stderr)

    def test_disabled_archiver_does_not_hide_the_path(self):
        result = self.run_cli(["specs-dir"], self.cfg("/abs/specs", enabled=False))
        self.assertEqual(result.stdout, "/abs/specs\n")
        self.assertEqual(result.returncode, 0)

    def test_runs_through_the_plugin_symlinks(self):
        for plugin in ("plugin-build", "plugin-continuity"):
            script = os.path.join(self.REPO, plugin, "lib", "toolkit_config.py")
            with self.subTest(plugin=plugin):
                self.assertTrue(os.path.islink(os.path.dirname(script)))
                result = self.run_cli(["specs-dir"], self.cfg("/abs/specs"),
                                      script=script)
                self.assertEqual(result.stdout, "/abs/specs\n")
                self.assertEqual(result.returncode, 0)

    def test_in_process_getter(self):
        self.write({})
        self.assertIsNone(toolkit_config.specs_dir())
        self.write(self.cfg("~/x/specs"))
        self.assertEqual(toolkit_config.specs_dir(),
                         os.path.expanduser("~/x/specs"))


if __name__ == "__main__":
    unittest.main()
