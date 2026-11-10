#!/usr/bin/env python3
"""Behavioural tests for install.sh's "Auto memory -> ~/.claude/settings.json"
section, which writes the harness's `autoMemoryDirectory` setting so every
session — repo, worktree, headless — loads one memory folder instead of
whichever empty one its start directory happens to key.

The section is a thin shell around `lib/auto_memory.py`: it computes
`managed_target()` and `unify_enabled()` from the toolkit config, reads the
raw `autoMemoryDirectory` key out of `~/.claude/settings.json` with the
`auto_memory.ABSENT` sentinel, calls `auto_memory.plan()`, and reports or
writes per the returned action. These tests drive the REAL `install.sh`
through `subprocess` (this repo's rule: test the executables, not just the
lib) against a fixture repo built fresh per test, with `$HOME` sandboxed and
`CLAUDE_TOOLKIT_CONFIG` pointed at a fixture config file — never the real `~`.

Case (i) is the one Plan Review flagged directly: `lib/tests/test_install_
worktree.py`'s own fixture ships only `lib/worktree_detect.sh`, no
`toolkit_config.py` or `auto_memory.py`. An unconditional import would make
that suite's real-apply installs exit 1. The section catches ImportError,
warns, and exits 0 instead — this class is what proves that path works, by
building the same minimal shape on purpose.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import install_fixture as fx

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.dirname(TESTS_DIR)
REPO_ROOT = os.path.dirname(LIB_DIR)
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")
WORKTREE_DETECT_SH = os.path.join(LIB_DIR, "worktree_detect.sh")
TOOLKIT_CONFIG_PY = os.path.join(LIB_DIR, "toolkit_config.py")
AUTO_MEMORY_PY = os.path.join(LIB_DIR, "auto_memory.py")


def expected_slug(path):
    """The project-key slug these fixtures expect, computed INDEPENDENTLY of
    `auto_memory.project_slug`. Every earlier slug fixture called
    the real resolver to build its own expectation, so a regex change in the
    code under test (e.g. one that starts keeping dots and spaces) would have
    passed unnoticed here too. Deliberately not importing `auto_memory` in
    this file -- there is nothing left in it to import for."""
    return "".join(
        c if (c.isascii() and c.isalnum()) else "-"
        for c in os.path.realpath(path))


def git(*args, cwd):
    """Every fixture git call, with the machine's own git config neutralised
    (see the identical helper's docstring in test_install_plugin.py)."""
    env = dict(os.environ)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, env=env,
                          universal_newlines=True)


def make_fixture_repo(root, name="fixture", include_auto_memory=True):
    """A minimal git repo shaped just enough like this toolkit for install.sh
    to run all the way through the Auto memory section: a symlink to the REAL
    install.sh, the worktree guard's helper, the two `claude/*.json` files
    the Hooks and Permissions sections read unconditionally, and symlinks to
    the REAL `lib/toolkit_config.py` and `lib/auto_memory.py` -- symlinked
    rather than copied so the code under test can never drift from what Task
    1 actually shipped.

    `include_auto_memory=False` reproduces case (i): a fixture that carries
    `toolkit_config.py` but not `auto_memory.py`, the exact shape of this
    repo's own `test_install_worktree.py` fixture.
    """
    repo = os.path.join(root, name)
    os.makedirs(os.path.join(repo, "claude"))
    os.makedirs(os.path.join(repo, "tools"))
    os.makedirs(os.path.join(repo, "lib"))
    os.symlink(INSTALL_SH, os.path.join(repo, "install.sh"))
    os.symlink(WORKTREE_DETECT_SH, os.path.join(repo, "lib", "worktree_detect.sh"))
    os.symlink(TOOLKIT_CONFIG_PY, os.path.join(repo, "lib", "toolkit_config.py"))
    if include_auto_memory:
        os.symlink(AUTO_MEMORY_PY, os.path.join(repo, "lib", "auto_memory.py"))
    fx.add_plugin_layout(repo)
    if not include_auto_memory:
        # add_plugin_layout copies auto_memory.py for toolkit_update's sake;
        # case (i) needs a lib/ without it.
        os.remove(os.path.join(repo, "lib", "auto_memory.py"))
    # A link whose source is missing from the repo is a required failure.
    fx.add_link_targets(repo, root)
    with open(os.path.join(repo, "claude", "permissions.baseline.json"), "w") as handle:
        handle.write('{"permissions": {}}\n')
    git("init", "-q", "-b", "main", repo, cwd=root)
    git("config", "user.email", "t@example.com", cwd=repo)
    git("config", "user.name", "Test", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def seed_home(home):
    """Pre-seed `$HOME/.config/claude-code-toolkit/config.json` with an empty
    (but valid) config, so install.sh's interactive first-time setup never
    triggers. This is deliberately a DIFFERENT file from the fixture config
    `CLAUDE_TOOLKIT_CONFIG` points at below: install.sh's own bash-level
    first-run check always reads `$HOME/.config/...`, while the Auto memory
    section's python reads `toolkit_config`, which honours
    `CLAUDE_TOOLKIT_CONFIG` when set. Both must exist for a real apply to
    reach the Auto memory section with the config we actually want it to see.
    """
    config_dir = os.path.join(home, ".config", "claude-code-toolkit")
    os.makedirs(config_dir)
    with open(os.path.join(config_dir, "config.json"), "w") as handle:
        json.dump({}, handle)


def write_config(path, data):
    with open(path, "w") as handle:
        json.dump(data, handle)


def write_settings(home, data):
    claude_dir = os.path.join(home, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    with open(os.path.join(claude_dir, "settings.json"), "w") as handle:
        json.dump(data, handle)


def settings_path_for(home):
    return os.path.join(home, ".claude", "settings.json")


def assert_no_auto_memory_key(test, home):
    """The Auto memory section wrote nothing. Scoped to its own key, not to
    whether settings.json exists at all: a later section (CLAUDE.md
    excludes) creates the file on every fresh install, and this section's
    contract says nothing about the others."""
    path = settings_path_for(home)
    if not os.path.exists(path):
        return
    with open(path) as handle:
        test.assertNotIn("autoMemoryDirectory", json.load(handle))


def projects_dir_for(home, workspace_dir):
    """Where install.sh's real apply must find `<home>/.claude/projects/<slug>`
    for the fixture workspace to have a usable managed target -- the slug
    computed by `expected_slug`, independently of `auto_memory.project_slug`
    so this fixture can't share a regex bug with the code under
    test."""
    slug = expected_slug(workspace_dir)
    return os.path.join(home, ".claude", "projects", slug)


def run_install(repo, home, config_path, args=()):
    env = dict(os.environ)
    env["HOME"] = home
    env["CLAUDE_TOOLKIT_CONFIG"] = config_path
    env = fx.shim_env_for_home(env, home)
    # Hermetic per this repo's testing rule: never inherit the worktree-install
    # escape hatch from the runner's own shell.
    env.pop("ALLOW_WORKTREE_INSTALL", None)
    # stdin=DEVNULL is load-bearing: capture_output does NOT redirect stdin,
    # so an interactive branch would inherit the runner's terminal and block
    # forever. The timeout is the backstop for every other way a real apply
    # could hang.
    return subprocess.run([os.path.join(repo, "install.sh")] + list(args),
                          cwd=repo, env=env, capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=120,
                          universal_newlines=True)


def auto_memory_section(stdout):
    """Scope assertions to this section's own reporting lines, the same way
    `hooks_section` does in test_install_plugin.py -- so an unrelated
    warning in a neighbouring section (the CLAUDE.md check's "no
    workspace_root configured", for one) can never be mistaken for an Auto
    memory finding."""
    marker = "Auto memory -> ~/.claude/settings.json"
    start = stdout.index(marker)
    end = stdout.index("\n\n", start)
    return stdout[start:end]


class FixtureTestCase(unittest.TestCase):
    """Common plumbing: a fresh fixture repo, a sandboxed HOME seeded against
    first-time setup, a fixture workspace_root directory, and a fixture
    toolkit config path the Auto memory section's python will actually read
    (via CLAUDE_TOOLKIT_CONFIG)."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        workspace_parent = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, workspace_parent, ignore_errors=True)
        # The workspace's own directory name carries "." and " " -- a bare
        # tempfile.mkdtemp() path never does, so a slug regex mutation that
        # stops replacing those characters would otherwise
        # produce an IDENTICAL slug under both the real `project_slug` and
        # this file's independent `expected_slug`, and no case here would
        # ever see the two disagree.
        self.workspace = os.path.join(workspace_parent, "work space.root")
        os.makedirs(self.workspace)
        self.repo = make_fixture_repo(self.root)
        self.config_path = os.path.join(self.root, "toolkit-config.json")
        seed_home(self.home)


class TestCaseA_SetWhenAbsent(FixtureTestCase):
    """(a) No `auto_memory` key, projects folder present -> key set to
    ~/.claude/projects/<slug>/memory; other keys preserved."""

    def test_key_is_set_and_other_keys_survive(self):
        write_config(self.config_path, {"workspace_root": self.workspace})
        os.makedirs(projects_dir_for(self.home, self.workspace))
        write_settings(self.home, {"otherKey": "keep-me"})

        result = run_install(self.repo, self.home, self.config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        expected = "~/.claude/projects/%s/memory" % expected_slug(self.workspace)
        self.assertIn("\033[33m->\033[0m autoMemoryDirectory = %s" % expected, section)

        with open(settings_path_for(self.home)) as handle:
            written = json.load(handle)
        self.assertEqual(expected, written["autoMemoryDirectory"])
        self.assertEqual("keep-me", written["otherKey"])


class TestCaseB_ReRunIsIdempotent(FixtureTestCase):
    """(b) Re-run -> `OK autoMemoryDirectory =`; file unchanged."""

    def test_second_run_reports_ok_and_writes_nothing_new(self):
        write_config(self.config_path, {"workspace_root": self.workspace})
        os.makedirs(projects_dir_for(self.home, self.workspace))
        first = run_install(self.repo, self.home, self.config_path)
        self.assertEqual(0, first.returncode, first.stderr)

        with open(settings_path_for(self.home)) as handle:
            before = handle.read()

        result = run_install(self.repo, self.home, self.config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        self.assertIn("\033[32mOK\033[0m autoMemoryDirectory =", section)
        with open(settings_path_for(self.home)) as handle:
            after = handle.read()
        self.assertEqual(before, after)


class TestCaseC_UnifyFalseRemoves(FixtureTestCase):
    """(c) `"unify": false` after (a) -> key removed."""

    def test_turning_unify_off_removes_the_written_key(self):
        write_config(self.config_path, {"workspace_root": self.workspace})
        os.makedirs(projects_dir_for(self.home, self.workspace))
        first = run_install(self.repo, self.home, self.config_path)
        self.assertEqual(0, first.returncode, first.stderr)
        with open(settings_path_for(self.home)) as handle:
            self.assertIn("autoMemoryDirectory", json.load(handle))

        write_config(self.config_path, {
            "workspace_root": self.workspace,
            "auto_memory": {"unify": False},
        })
        result = run_install(self.repo, self.home, self.config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        self.assertIn("removing autoMemoryDirectory", section)
        with open(settings_path_for(self.home)) as handle:
            written = json.load(handle)
        self.assertNotIn("autoMemoryDirectory", written)


class TestCaseD_ForeignStringLeftAlone(FixtureTestCase):
    """(d) Foreign string value with unify on -> unchanged; the warning
    names it."""

    def test_foreign_value_is_named_and_untouched(self):
        write_config(self.config_path, {"workspace_root": self.workspace})
        os.makedirs(projects_dir_for(self.home, self.workspace))
        write_settings(self.home, {"autoMemoryDirectory": "/some/other/path"})

        result = run_install(self.repo, self.home, self.config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        self.assertIn("/some/other/path", section)
        self.assertIn("leaving it alone", section)
        with open(settings_path_for(self.home)) as handle:
            written = json.load(handle)
        self.assertEqual("/some/other/path", written["autoMemoryDirectory"])


class TestCaseE_ProjectsFolderMissing(FixtureTestCase):
    """(e) Projects folder missing -> no key written; the "no session has
    run" warning appears."""

    def test_missing_projects_folder_warns_and_writes_nothing(self):
        write_config(self.config_path, {"workspace_root": self.workspace})
        # Deliberately no <home>/.claude/projects/<slug> directory.

        result = run_install(self.repo, self.home, self.config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        self.assertIn("no session has run in workspace_root yet", section)
        assert_no_auto_memory_key(self, self.home)


class TestCaseF_CheckWritesNothing(FixtureTestCase):
    """(f) `--check` from case (a)'s starting state -> prints `->`; file
    unchanged."""

    def test_check_reports_and_leaves_the_file_byte_identical(self):
        write_config(self.config_path, {"workspace_root": self.workspace})
        os.makedirs(projects_dir_for(self.home, self.workspace))
        write_settings(self.home, {"otherKey": "keep-me"})
        with open(settings_path_for(self.home)) as handle:
            before = handle.read()
        # `--check` exits 1 on a machine whose hooks are not protected, so the
        # fixture is a protected one; this case is about what --check WRITES.
        fx.seed_home_installed(self.home, self.repo)
        fx.install_links(self.repo, self.home)

        result = run_install(self.repo, self.home, self.config_path, args=["--check"])

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        self.assertIn("\033[33m->\033[0m autoMemoryDirectory =", section)
        with open(settings_path_for(self.home)) as handle:
            after = handle.read()
        self.assertEqual(before, after)
        self.assertFalse(os.path.exists(settings_path_for(self.home) + ".bak"))


class TestCaseG_NoWorkspaceRootSkips(FixtureTestCase):
    """(g) No `workspace_root` -> skip warning; nothing written."""

    def test_no_workspace_root_configured_skips(self):
        write_config(self.config_path, {})

        result = run_install(self.repo, self.home, self.config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        self.assertIn(
            "autoMemoryDirectory not set: no workspace_root configured", section)
        self.assertIn(
            "sessions started in a repo or worktree will load an empty "
            "memory folder", section)
        assert_no_auto_memory_key(self, self.home)


class TestCaseH_PresentNullIsForeign(FixtureTestCase):
    """(h) `"autoMemoryDirectory": null` present, unify on -> left as `null`;
    foreign warning."""

    def test_present_null_is_left_as_null(self):
        write_config(self.config_path, {"workspace_root": self.workspace})
        os.makedirs(projects_dir_for(self.home, self.workspace))
        write_settings(self.home, {"autoMemoryDirectory": None})

        result = run_install(self.repo, self.home, self.config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        self.assertIn("leaving it alone", section)
        with open(settings_path_for(self.home)) as handle:
            written = json.load(handle)
        self.assertIsNone(written["autoMemoryDirectory"])


class TestCaseI_MissingAutoMemoryLibIsHandled(unittest.TestCase):
    """(i) Fixture WITHOUT `lib/auto_memory.py` -> exit 0, the "toolkit lib
    not importable" warning, settings untouched. Reproduces the exact shape
    Plan Review flagged: test_install_worktree.py's fixture ships
    toolkit_config.py-less, auto_memory.py-less lib/ directories entirely."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.workspace = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)
        self.repo = make_fixture_repo(self.root, include_auto_memory=False)
        self.config_path = os.path.join(self.root, "toolkit-config.json")
        seed_home(self.home)

    def test_import_failure_warns_and_exits_0_leaving_settings_untouched(self):
        write_config(self.config_path, {"workspace_root": self.workspace})
        os.makedirs(projects_dir_for(self.home, self.workspace))

        result = run_install(self.repo, self.home, self.config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        self.assertIn("auto memory skipped: toolkit lib not importable", section)
        assert_no_auto_memory_key(self, self.home)


class TestCaseJ_NonStringWorkspaceRootIsHandled(FixtureTestCase):
    """(j) `workspace_root` truthy but not a string (a JSON list,
    from a malformed config) must not crash a real apply. Before
    `managed_target`'s type check, `.startswith` on the raw list raised
    AttributeError and install.sh exited 1 instead of reporting a skip."""

    def test_list_workspace_root_warns_and_writes_nothing(self):
        write_config(self.config_path, {"workspace_root": ["workspace"]})

        result = run_install(self.repo, self.home, self.config_path)

        self.assertEqual(0, result.returncode, result.stderr)
        section = auto_memory_section(result.stdout)
        self.assertIn(
            "autoMemoryDirectory not set: workspace_root is not a directory",
            section)
        assert_no_auto_memory_key(self, self.home)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def classify_install_output(result):
    """Map a `--check` run of the real install.sh to `(action, value)` using
    the report lines only. Fails loudly on a section line it cannot classify
    -- a classifier that defaults would make this parity test unable to fail.
    `value` is the `autoMemoryDirectory = <v>` text for set/ok-managed, else
    None."""
    plain = ANSI_RE.sub("", result.stdout)
    marker = "Auto memory -> ~/.claude/settings.json"
    # Malformed settings stop install.sh in its Hooks section, before this
    # section prints at all.
    if (result.returncode != 0 and marker not in plain
            and ("is not valid JSON" in plain
                 or "is not a JSON object" in plain)):
        return "error", None
    lines = auto_memory_section(plain).splitlines()[1:]
    action = value = None
    for line in lines:
        text = line.strip()
        if text.startswith("sessions started in a repo or worktree"):
            continue  # the skip report's continuation line
        found = None
        match = re.match(r"^OK autoMemoryDirectory = (.+)$", text)
        if match:
            found, value = "ok", match.group(1)
        elif text.startswith("OK auto memory not managed"):
            found = "ok"
        elif text.startswith("!! autoMemoryDirectory not set:"):
            found = "skip"
        elif text.startswith("!! autoMemoryDirectory is "):
            found = "foreign"
        else:
            match = re.match(r"^-> autoMemoryDirectory = (.+)$", text)
            if match:
                found, value = "set", match.group(1)
            elif text.startswith("-> removing autoMemoryDirectory"):
                found = "remove"
        if found is None or action is not None:
            raise AssertionError("unclassifiable install.sh line: %r in %r"
                                 % (line, lines))
        action = found
    if action is None:
        raise AssertionError("no report line in section: %r" % lines)
    return action, value


class TestStatusMatchesInstallCheck(FixtureTestCase):
    """`auto_memory.py --status` must name the same action `install.sh
    --check` reports, for every state the Auto memory section can be in."""

    def run_status(self):
        env = dict(os.environ)
        env["HOME"] = self.home
        env["USERPROFILE"] = self.home
        env["CLAUDE_TOOLKIT_CONFIG"] = self.config_path
        env = fx.shim_env_for_home(env, self.home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("PYTHONPYCACHEPREFIX", None)
        proc = subprocess.run(
            [sys.executable, os.path.join(self.repo, "lib", "auto_memory.py"),
             "--status"],
            env=env, stdin=subprocess.DEVNULL, capture_output=True,
            universal_newlines=True, cwd=self.root, timeout=60)
        self.assertEqual(0, proc.returncode, proc.stderr)
        return json.loads(proc.stdout)

    def check_state(self, expected_action, config, settings, projects=True,
                    raw_settings=None):
        write_config(self.config_path, config)
        if projects:
            os.makedirs(projects_dir_for(self.home, self.workspace))
        if raw_settings is not None:
            os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
            with open(settings_path_for(self.home), "w") as handle:
                handle.write(raw_settings)
        elif settings is not None:
            write_settings(self.home, settings)
        result = run_install(self.repo, self.home, self.config_path,
                             ["--check"])
        install_action, install_value = classify_install_output(result)
        status = self.run_status()
        self.assertEqual(expected_action, install_action)
        self.assertEqual(install_action, status["action"])
        if install_action in ("set", "ok") and install_value is not None:
            self.assertEqual(install_value, status["detail"])

    def target(self):
        return "~/.claude/projects/%s/memory" % expected_slug(self.workspace)

    def test_all_states(self):
        root_cfg = {"workspace_root": self.workspace}
        unify_off = {"workspace_root": self.workspace,
                     "auto_memory": {"unify": False}}
        states = {
            "set": ("set", root_cfg, {}, True, None),
            "ok-managed": ("ok", root_cfg,
                           {"autoMemoryDirectory": self.target()}, True, None),
            "ok-unmanaged": ("ok", unify_off, {}, True, None),
            "remove": ("remove", unify_off,
                       {"autoMemoryDirectory": self.target()}, True, None),
            "skip": ("skip", root_cfg, {}, False, None),
            "skip-no-root": ("skip", {}, {}, True, None),
            "foreign": ("foreign", root_cfg,
                        {"autoMemoryDirectory": "/some/other/path"}, True,
                        None),
            "foreign-null": ("foreign", root_cfg,
                             {"autoMemoryDirectory": None}, True, None),
            "error-invalid": ("error", root_cfg, None, True, "{not json"),
            "error-non-object": ("error", root_cfg, None, True, "[1, 2]"),
            "missing-settings": ("set", root_cfg, None, True, None),
        }
        for name, (action, config, settings, projects, raw) in states.items():
            with self.subTest(state=name):
                # A fresh home per state: the fixture's own is reused only
                # for the first, so wipe what the previous state left.
                shutil.rmtree(os.path.join(self.home, ".claude"),
                              ignore_errors=True)
                self.check_state(action, config, settings, projects, raw)


if __name__ == "__main__":
    unittest.main()
