#!/usr/bin/env python3
"""The install and hook path matrix.

The other install suites run a minimal fixture repo at an ASCII path. This one
copies the REAL working tree to a repo path and a HOME that carry a space, an
ʻokina, or an NFD accent, runs the real `install.sh`, and then executes every
hook command registered by every profile plugin exactly as the harness does:
the `hooks.json` string handed to `/bin/sh -c` with `CLAUDE_PLUGIN_ROOT` set.
An unquoted `${CLAUDE_PLUGIN_ROOT}` passed every structural check while
failing exit 127 in a real shell, and only a run catches that.

Each path class runs under two surfaces:

  terminal  CLAUDE_CODE_ENTRYPOINT=cli, a UTF-8 LANG, the caller's PATH.
  vscode    CLAUDE_CODE_ENTRYPOINT=claude-vscode (measured: hooks inherit
            it), with no LANG/LC_* and a bare system PATH. The
            locale and PATH are a MODEL, not a measurement. They are
            deliberately harsher than the extension's real hook environment,
            so a pass here covers the real one, whatever it holds.

`install.sh` runs once per class under the vscode surface, because `/onboard`
runs it through the Bash tool. `--check` runs under the terminal surface.

macOS only, like the installer.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import install_fixture as fx  # noqa: E402

# (label, repo directory name, home directory name). "ascii" is the control.
# "okina" has no space, so an encoding fault cannot hide behind a quoting
# fault. "mixed" carries all three hazards; its accent is NFD (e + U+0301),
# the decomposed form macOS file APIs can hand back for a name typed as NFC.
PATH_CLASSES = (
    ("ascii", "repo", "home"),
    ("space", "my repo", "my home"),
    ("okina", "Oʻahu", "Kauaʻi"),
    ("mixed", "Oʻahu Café repo", "Kauaʻi Café home"),
)

SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
# What a hook prints on stdout when something it owns is broken but it still
# exits 0: a crashed guard's report, and toolkit-integrity's dark-hook alarm.
STDOUT_ALARMS = ("crashed and did NOT check", "SOMETHING INSTALLED CANNOT RUN")
ADD = "plugin marketplace add "
DANGER_HOOK = "/tools/danger-guard/claude-danger-guard"

# Commands a hook may reach for that act on the real machine. Each gets a
# no-op shim ahead of the system PATH. `claude` is the fixture's own shim.
NOOP_COMMANDS = ("caffeinate", "pmset", "osascript", "gh", "codex")

COPY_IGNORE = shutil.ignore_patterns(".git", ".claude", "__pycache__", "*.pyc",
                                     "node_modules")


def clean_env(home):
    """The caller's environment minus anything that could point a run at the
    real toolkit, the real repo or the real Claude Code session."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "CLAUDE"))
           and k not in ("LANG", "LC_ALL", "LC_CTYPE")}
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def git(cwd, *args, env):
    subprocess.run(["git"] + list(args), cwd=cwd, env=env, check=True,
                   stdin=subprocess.DEVNULL, capture_output=True)


def registered_hooks(repo):
    """[(plugin dir, event, handler)] for every handler in every profile
    plugin the registry names, read from the copy under test."""
    with open(os.path.join(repo, "claude", "profiles.json"), encoding="utf-8") as handle:
        registry = json.load(handle)["profiles"]
    rows = []
    for entry in registry.values():
        if not isinstance(entry, dict):
            continue
        path = os.path.join(repo, entry["dir"], "hooks", "hooks.json")
        with open(path, encoding="utf-8") as handle:
            hooks = json.load(handle)["hooks"]
        for event, groups in hooks.items():
            for group in groups:
                for handler in group["hooks"]:
                    rows.append((entry["dir"], event, handler))
    return rows


def payload(event, cwd, home, command="ls"):
    """One benign JSON payload carrying every field any toolkit hook reads."""
    return json.dumps({
        "session_id": "path-matrix", "hook_event_name": event, "cwd": cwd,
        "transcript_path": os.path.join(home, "transcript.jsonl"),
        "tool_name": "Bash", "tool_input": {"command": command},
        "prompt": "hello", "source": "startup", "stop_hook_active": False,
    })


@unittest.skipUnless(sys.platform == "darwin", "macOS only, like the installer")
class PathClassBase(unittest.TestCase):
    label = repo_name = home_name = None

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="path-matrix-")
        cls.repo = os.path.join(cls.root, cls.repo_name)
        cls.home = os.path.join(cls.root, cls.home_name)
        os.makedirs(cls.home)
        shutil.copytree(REPO_ROOT, cls.repo, symlinks=True, ignore=COPY_IGNORE)
        base = clean_env(cls.home)
        # A primary checkout, so install.sh's worktree refusal does not fire.
        git(cls.repo, "init", "-q", "-b", "main", env=base)
        git(cls.repo, "add", "-A", env=base)
        git(cls.repo, "-c", "user.email=t@example.com", "-c", "user.name=T",
            "commit", "-q", "-m", "init", env=base)
        cls.bin_dir = os.path.join(cls.root, "shim bin")
        cls.state = os.path.join(cls.root, "shim-state.json")
        fx.install_claude_shim(cls.bin_dir, cls.state)
        for name in NOOP_COMMANDS:
            path = os.path.join(cls.bin_dir, name)
            with open(path, "w") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(path, 0o755)
        # A valid empty config, so first-time setup never prompts.
        config_dir = os.path.join(cls.home, ".config", "claude-code-toolkit")
        os.makedirs(config_dir)
        with open(os.path.join(config_dir, "config.json"), "w") as handle:
            handle.write("{}\n")
        shimmed = fx.shim_env(base, cls.bin_dir, cls.state)
        cls.surfaces = {
            "terminal": dict(shimmed, CLAUDE_CODE_ENTRYPOINT="cli",
                             LANG="en_US.UTF-8"),
            "vscode": dict(shimmed, CLAUDE_CODE_ENTRYPOINT="claude-vscode",
                           PATH=cls.bin_dir + os.pathsep + SYSTEM_PATH),
        }
        cls.install = cls.run_in(["bash", os.path.join(cls.repo, "install.sh")], "vscode")
        cls.check = cls.run_in(["bash", os.path.join(cls.repo, "install.sh"), "--check"],
                               "terminal")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    @classmethod
    def run_in(cls, argv, surface, stdin_text=None, extra=None, timeout=300):
        env = dict(cls.surfaces[surface], **(extra or {}))
        return subprocess.run(argv, cwd=cls.repo, env=env, input=stdin_text,
                              stdin=None if stdin_text is not None else subprocess.DEVNULL,
                              capture_output=True, timeout=timeout,
                              universal_newlines=True, encoding="utf-8",
                              errors="replace")

    def run_hook(self, plugin_dir, command, surface, stdin_text, timeout):
        root = os.path.join(self.repo, plugin_dir)
        return self.run_in(["/bin/sh", "-c", command], surface, stdin_text,
                           extra={"CLAUDE_PLUGIN_ROOT": root}, timeout=timeout)

    def danger_command(self):
        for plugin_dir, event, handler in registered_hooks(self.repo):
            if handler["command"].endswith(DANGER_HOOK):
                return plugin_dir, event, handler["command"]
        self.fail("no registered handler runs the danger guard")

    # -- install ----------------------------------------------------------

    def test_paths_carry_the_class_hazard(self):
        """Guards the fixture itself: a class whose name lost its hazard
        would pass every other test without testing anything."""
        hazards = {"ascii": "", "space": " ", "okina": "ʻ", "mixed": " ʻ́"}
        for char in hazards[self.label]:
            self.assertIn(char, self.repo)
            self.assertIn(char, self.home)
        if self.label == "okina":
            self.assertNotIn(" ", self.repo)

    def test_install_exits_zero(self):
        self.assertEqual(self.install.returncode, 0,
                         self.install.stdout[-3000:] + self.install.stderr[-3000:])

    def test_check_proposes_nothing(self):
        self.assertEqual(self.check.returncode, 0,
                         self.check.stdout[-3000:] + self.check.stderr[-3000:])

    def test_marketplace_registered_at_the_exact_repo_path(self):
        adds = [line[len(ADD):] for line in fx.shim_log(self.state) if line.startswith(ADD)]
        self.assertTrue(adds, fx.shim_log(self.state))
        for path in adds:
            # Same file, and the same name, byte for byte: an NFC rewrite of
            # the NFD name would still resolve on APFS but not compare equal.
            self.assertTrue(os.path.samefile(path, self.repo), path)
            self.assertEqual(os.path.basename(path), self.repo_name)

    def test_every_installed_link_resolves_into_the_repo(self):
        bin_dir = os.path.join(self.home, ".local", "bin")
        names = os.listdir(bin_dir)
        self.assertTrue(names)
        repo_real = os.path.realpath(self.repo) + os.sep
        for name in names:
            link = os.path.join(bin_dir, name)
            self.assertTrue(os.path.islink(link), link)
            self.assertTrue(os.path.exists(link), "dangling: " + link)
            self.assertTrue(os.path.realpath(link).startswith(repo_real), link)

    def installed_status_line(self):
        with open(os.path.join(self.home, ".claude", "settings.json"), encoding="utf-8") as handle:
            return json.load(handle)["statusLine"]["command"]

    def test_installed_status_line_command_runs_on_both_surfaces(self):
        """install.sh writes this one command itself (hooks come from the
        plugin), so it carries its own quoting. Unquoted, a spaced HOME split
        it at the space and the status line went dark."""
        command = self.installed_status_line()
        for surface in self.surfaces:
            with self.subTest(surface=surface):
                result = self.run_in(["/bin/sh", "-c", command], surface, stdin_text="{}",
                                     timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, "")

    def test_status_line_unchanged_where_home_needs_no_quoting(self):
        """Machines installed before the quoting fix must see no diff."""
        if " " in self.home:
            self.skipTest("this class's HOME needs quoting")
        self.assertEqual(self.installed_status_line(),
                         self.home + "/.local/bin/claude-usage-line")

    def test_team_policy_import_reads_ok_on_both_surfaces(self):
        policy = os.path.join(self.repo, "lib", "team_policy.py")
        wrote = self.run_in(["python3", policy, "ensure", self.repo], "terminal")
        self.assertEqual(wrote.returncode, 0, wrote.stderr)
        for surface in self.surfaces:
            with self.subTest(surface=surface):
                status = self.run_in(["python3", policy, "--status", self.repo], surface)
                self.assertEqual(status.returncode, 0, status.stderr)
                self.assertEqual(json.loads(status.stdout)["state"], "ok", status.stdout)

    # -- hooks ------------------------------------------------------------

    def test_every_registered_hook_runs_clean_on_both_surfaces(self):
        """Clean means exit 0, NOTHING on stderr, and no alarm on stdout.

        Exit 0 alone proves little: every hook is best-effort and exits 0 on
        its own failures, each with a diagnostic in its own words (the distill
        guard prints "ModuleNotFoundError ... (allowing the write)"). A
        per-hook list of failure phrasings would rot, so the test asserts
        the property every hook shares instead: on this benign payload,
        all registered hooks write nothing to stderr, on both
        surfaces and every path class."""
        rows = registered_hooks(self.repo)
        self.assertTrue(rows)
        for plugin_dir, event, handler in rows:
            for surface in self.surfaces:
                with self.subTest(plugin=plugin_dir, event=event, surface=surface,
                                  command=handler["command"]):
                    result = self.run_hook(plugin_dir, handler["command"], surface,
                                           payload(event, self.repo, self.home),
                                           handler.get("timeout", 60) + 10)
                    self.assertEqual(result.returncode, 0, result.stderr[-2000:])
                    self.assertEqual(result.stderr, "")
                    for alarm in STDOUT_ALARMS:
                        self.assertNotIn(alarm, result.stdout)

    def test_positive_control_danger_guard_blocks_through_the_registered_string(self):
        """The clean-run test passes for a hook that never started if the
        shell swallowed it. Here the same registered string must REFUSE a
        command aimed at this class's HOME, so the guard really ran here."""
        plugin_dir, event, command = self.danger_command()
        for surface in self.surfaces:
            with self.subTest(surface=surface):
                result = self.run_hook(plugin_dir, command, surface,
                                       payload(event, self.repo, self.home,
                                               command="rm -rf ~/Documents"), 60)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("BLOCKED by claude-danger-guard", result.stderr)
                self.assertIn(os.path.join(self.home, "Documents"), result.stderr)

    def test_positive_control_unquoted_root_fails_where_the_path_has_a_space(self):
        """Proves this harness would see the unquoted-root defect: the registered
        string with its quotes stripped must break on a spaced path."""
        if " " not in self.repo:
            self.skipTest("no space in this class's path")
        plugin_dir, event, command = self.danger_command()
        self.assertIn('"${CLAUDE_PLUGIN_ROOT}"', command)
        broken = command.replace('"${CLAUDE_PLUGIN_ROOT}"', "${CLAUDE_PLUGIN_ROOT}")
        result = self.run_hook(plugin_dir, broken, "terminal",
                               payload(event, self.repo, self.home), 60)
        self.assertEqual(result.returncode, 127, result.stderr)


for _label, _repo, _home in PATH_CLASSES:
    _name = "Test%sPaths" % _label.capitalize()
    globals()[_name] = type(_name, (PathClassBase,),
                            {"label": _label, "repo_name": _repo, "home_name": _home})
del _label, _repo, _home, _name


class TestMatrixShape(unittest.TestCase):
    """The base class must never run on its own (it has no paths), and every
    class in PATH_CLASSES must exist as a test case."""

    def test_base_is_not_collected_and_every_class_is(self):
        loader = unittest.TestLoader()
        names = set(loader.getTestCaseNames(PathClassBase))
        self.assertTrue(names)
        for label, _, _ in PATH_CLASSES:
            case = globals()["Test%sPaths" % label.capitalize()]
            self.assertEqual(case.label, label)


def load_tests(loader, tests, pattern):
    """Run every class except the path-less base."""
    suite = unittest.TestSuite()
    for name, value in sorted(globals().items()):
        if (isinstance(value, type) and issubclass(value, unittest.TestCase)
                and value is not PathClassBase):
            suite.addTests(loader.loadTestsFromTestCase(value))
    return suite


if __name__ == "__main__":
    unittest.main()
