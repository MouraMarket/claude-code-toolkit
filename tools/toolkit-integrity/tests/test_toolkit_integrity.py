#!/usr/bin/env python3
"""Behavioural tests for claude-toolkit-integrity.

Hermetic throughout: every test builds a sandbox `$HOME` with its own
`.local/bin` and `.claude` tree, and a throwaway git repo with a real remote.
Nothing here reads the machine's own `~/.local/bin`, its settings, or its
dedupe state -- a check about whether an installation is intact is exactly the
kind that passes vacuously if it is allowed to look at a healthy real one.

The two integrity checks are asserted **separately**, through
`unrunnable_hooks` and `broken_links` directly rather than only through the
rendered notice. They overlap on a dangling `~/.local/bin` link that a hook
names, and this repo has twice shipped a defect where two mechanisms with one
observable outcome covered for each other.

`TestHookWiring` is the class that matters most. The lib functions can all be
correct while the hook fails to persist its own baseline -- which is a real
defect this build had: `save_state` sat behind an `if not out: return`, so the
drift baseline, recorded on a run that by design prints nothing, was thrown
away every time and drift could never fire.
"""

import contextlib
import json
import os
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, TOOL_DIR)
# lib/ first: the integrity lib imports hook_manifest at import time.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(TOOL_DIR)), "lib"))
import toolkit_integrity_lib as lib  # noqa: E402
import hook_manifest  # noqa: E402
import install_record  # noqa: E402

HOOK = os.path.join(TOOL_DIR, "claude-toolkit-integrity")


def git(*args, cwd):
    return subprocess.run(["git"] + list(args), cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class Sandbox(unittest.TestCase):
    """A fake $HOME plus a real git repo standing in for the toolkit."""

    # Spelled out rather than read from `lib.LINK_DIRS`. Building the fixture
    # from the constant under test makes every test in the file depend on it:
    # a mutation narrowing LINK_DIRS then breaks the SANDBOX rather than the
    # behaviour, and 50 tests fail for a reason unrelated to what was mutated.
    # A blast radius of the wrong size is a finding, not a pass.
    SANDBOX_DIRS = (".local/bin", ".claude/commands", ".claude/agents",
                    ".claude/skills", ".claude/build-lens")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        for relative in self.SANDBOX_DIRS:
            os.makedirs(os.path.join(self.home, relative), exist_ok=True)
        self.bin = os.path.join(self.home, ".local", "bin")
        self.real = os.path.join(self.tmp, "repo")
        os.makedirs(self.real, exist_ok=True)
        self.state = os.path.join(self.tmp, "state.json")
        self.settings = os.path.join(self.home, ".claude", "settings.json")
        self.write_settings({})

    # --- fixture helpers ---------------------------------------------------

    def make_tool(self, name, executable=True):
        """A real file in the fixture repo, plus the ~/.local/bin link."""
        path = os.path.join(self.real, name)
        with open(path, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        if executable:
            os.chmod(path, 0o755)
        else:
            os.chmod(path, 0o644)
        link = os.path.join(self.bin, name)
        os.symlink(path, link)
        return path, link

    def break_link(self, link):
        """Delete what a link points at, leaving the link behind."""
        os.unlink(os.readlink(link))

    def write_settings(self, payload):
        with open(self.settings, "w") as handle:
            json.dump(payload, handle)

    def hooks_settings(self, *commands, event="PreToolUse"):
        self.write_settings({"hooks": {event: [
            {"hooks": [{"type": "command", "command": c} for c in commands]}]}})

    # --- git fixture -------------------------------------------------------

    def make_repo(self):
        repo = os.path.join(self.tmp, "toolkit")
        git("init", "-q", "-b", "main", repo, cwd=self.tmp)
        for key, value in (("user.email", "t@example.com"), ("user.name", "T"),
                           ("commit.gpgsign", "false")):
            git("config", key, value, cwd=repo)
        self.commit_file(repo, "README.md", "hello\n", "init")
        return repo

    def commit_file(self, repo, name, text, message):
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
        with open(path, "w") as handle:
            handle.write(text)
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", message, cwd=repo)
        return git("rev-parse", "HEAD", cwd=repo)

    # --- session-claim's canary needs a live owner and a working `ps` -------

    def ps_works(self):
        try:
            done = subprocess.run(["ps", "-Ao", "pid="], capture_output=True,
                                  timeout=10)
        except Exception:
            return False
        return done.returncode == 0

    def requires_ps(self):
        # session-claim blocks only when its `ps -Ao pid=,lstart=` start table
        # works; where ps is refused it logs allow-uncertain and allows.
        if not self.ps_works():
            self.skipTest("ps is unavailable here, so session-claim cannot block")

    def owner_pid(self):
        """A live process standing in for the claude ancestor session-claim
        would otherwise look up (CLAIM_TEST_OWNER_PID)."""
        if getattr(self, "_owner", None) is None:
            self._owner = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(120)"])

            def stop(proc=self._owner):
                proc.kill()
                proc.wait()
            self.addCleanup(stop)
        return self._owner.pid

    # --- running the real hook ---------------------------------------------

    def run_hook(self, session="s1", root=None, extra_env=None,
                 event=None, repo=None, cwd=None):
        env = dict(os.environ)
        env["TOOLKIT_INTEGRITY_HOME"] = self.home
        env["TOOLKIT_INTEGRITY_STATE_PATH"] = self.state
        # Hermetic AND acceptance criterion 9 at the same time: every hook run
        # in this suite is a fresh-clone run with no config at all.
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(self.tmp, "no-config.json")
        if root:
            env["TOOLKIT_INTEGRITY_ROOT"] = root
        if repo:
            env["TOOLKIT_INTEGRITY_REPO"] = repo
        env.update(extra_env or {})
        payload = {"session_id": session, "cwd": self.tmp}
        if event:
            payload["hook_event_name"] = event
        done = subprocess.run([sys.executable, HOOK], input=json.dumps(
            payload),
            capture_output=True, text=True, env=env, cwd=cwd)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout


# ============================================================ check A, part 1

class TestUnrunnableHooks(Sandbox):
    """What settings.json says will run, against what can."""

    def test_a_healthy_hook_is_not_a_problem(self):
        _, link = self.make_tool("claude-guard")
        self.hooks_settings(link)
        self.assertEqual(lib.unrunnable_hooks(self.home), [])

    def test_a_hook_whose_link_never_existed_is_reported(self):
        """The case directory enumeration structurally cannot see.

        There is no entry left to enumerate, so a check built only on the
        install directories reports a clean machine while the hook is gone.
        """
        self.hooks_settings(os.path.join(self.bin, "claude-never-installed"))
        found = lib.unrunnable_hooks(self.home)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["why"], "missing")
        self.assertEqual(lib.broken_links(self.home), [])   # the control

    def test_a_dangling_hook_link_is_reported_with_its_target(self):
        target, link = self.make_tool("claude-guard")
        self.hooks_settings(link)
        self.break_link(link)
        found = lib.unrunnable_hooks(self.home)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["why"], "missing")
        self.assertEqual(found[0]["target"], target)

    def test_a_hook_that_is_not_executable_is_reported(self):
        _, link = self.make_tool("claude-guard", executable=False)
        self.hooks_settings(link)
        found = lib.unrunnable_hooks(self.home)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["why"], "not executable")

    def test_the_status_line_counts_too(self):
        link = os.path.join(self.bin, "claude-usage-line")
        self.write_settings({"statusLine": {"type": "command",
                                            "command": link}})
        found = lib.unrunnable_hooks(self.home)
        self.assertEqual([r["where"] for r in found], ["statusLine"])

    def test_a_command_with_arguments_resolves_to_its_executable(self):
        _, link = self.make_tool("claude-caffeinate")
        self.hooks_settings("%s release" % link)
        self.assertEqual(lib.unrunnable_hooks(self.home), [])
        self.break_link(link)
        self.assertEqual(len(lib.unrunnable_hooks(self.home)), 1)

    def test_an_unreadable_settings_file_is_silence_not_a_raise(self):
        os.unlink(self.settings)
        self.assertIsNone(lib.load_settings(self.settings))
        self.assertEqual(lib.unrunnable_hooks(self.home), [])

    def test_none_and_empty_settings_are_not_collapsed(self):
        with open(self.settings, "w") as handle:
            handle.write("{ not json")
        self.assertIsNone(lib.load_settings(self.settings))
        self.write_settings({})
        self.assertEqual(lib.load_settings(self.settings), {})


class TestPluginRegistrations(Sandbox):
    """What plugin/hooks/hooks.json says will run, against what can."""

    def plugin_repo(self, files):
        """A fixture repo; `files` maps plugin-relative path -> executable?
        (None leaves the file absent). Every file gets one registration."""
        repo = os.path.join(self.tmp, "pluginrepo")
        os.makedirs(os.path.join(repo, "plugin", "hooks"), exist_ok=True)
        blocks = []
        for relpath, mode in files.items():
            if mode is not None:
                path = os.path.join(repo, "plugin", relpath)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as handle:
                    handle.write("#!/bin/sh\nexit 0\n")
                os.chmod(path, 0o755 if mode else 0o644)
            blocks.append({"hooks": [{"type": "command", "command":
                           '"${CLAUDE_PLUGIN_ROOT}"/' + relpath}]})
        with open(os.path.join(repo, "plugin", "hooks", "hooks.json"),
                  "w") as handle:
            json.dump({"hooks": {"PreToolUse": blocks}}, handle)
        return repo

    def test_a_healthy_plugin_registration_is_not_a_problem(self):
        repo = self.plugin_repo({"tools/g/claude-guard": True})
        self.assertEqual(lib.unrunnable_hooks(self.home, repo=repo), [])

    def test_a_registration_pointing_at_a_missing_file_is_reported(self):
        repo = self.plugin_repo({"tools/g/claude-guard": None})
        found = lib.unrunnable_hooks(self.home, repo=repo)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["where"], "plugin:PreToolUse")
        self.assertEqual(found[0]["why"], "missing")
        self.assertTrue(found[0]["path"].endswith("plugin/tools/g/claude-guard"))

    def test_a_non_executable_registration_is_reported(self):
        repo = self.plugin_repo({"tools/g/claude-guard": False})
        found = lib.unrunnable_hooks(self.home, repo=repo)
        self.assertEqual([r["why"] for r in found], ["not executable"])

    def test_an_unreadable_manifest_yields_one_unreadable_record(self):
        repo = os.path.join(self.tmp, "emptyrepo")
        os.makedirs(repo)
        found = lib.unrunnable_hooks(self.home, repo=repo)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["where"], "plugin")
        self.assertEqual(found[0]["why"], "unreadable")

    def test_without_a_repo_the_plugin_is_not_consulted(self):
        self.plugin_repo({"tools/g/claude-guard": None})
        self.assertEqual(lib.unrunnable_hooks(self.home), [])

    def test_integrity_problems_forwards_the_repo(self):
        repo = self.plugin_repo({"tools/g/claude-guard": None})
        problems = lib.integrity_problems(self.home, repo=repo)
        self.assertEqual(len(problems["hooks"]), 1)

    def test_the_executable_names_a_missing_plugin_registration(self):
        """The hook as a subprocess, in a fixture repo it derives from its own
        location, with its two lib dependencies copied beside it."""
        repo = self.plugin_repo({"tools/g/claude-guard": None})
        tool = os.path.join(repo, "tools", "toolkit-integrity")
        os.makedirs(tool)
        os.makedirs(os.path.join(repo, "lib"))
        for src, dst in ((HOOK, tool), (os.path.join(TOOL_DIR,
                          "toolkit_integrity_lib.py"), tool)):
            shutil.copy(src, dst)
        libdir = os.path.join(os.path.dirname(os.path.dirname(TOOL_DIR)), "lib")
        for name in ("toolkit_config.py", "hook_manifest.py"):
            shutil.copy(os.path.join(libdir, name), os.path.join(repo, "lib"))
        env = dict(os.environ)
        env["TOOLKIT_INTEGRITY_HOME"] = self.home
        env["TOOLKIT_INTEGRITY_STATE_PATH"] = self.state
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(self.tmp, "no-config.json")
        done = subprocess.run(
            [sys.executable, os.path.join(tool, "claude-toolkit-integrity")],
            input=json.dumps({"session_id": "s1", "cwd": self.tmp}),
            capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("plugin:PreToolUse", done.stdout)
        self.assertIn("claude-guard", done.stdout)
        self.assertIn("already dark", done.stdout)

    def test_a_malformed_manifest_does_not_hide_a_missing_settings_hook(self):
        """A manifest shape that once raised AttributeError out of the lib took
        the whole report with it: the settings hook that WAS missing went
        unnamed. The executable must name both."""
        repo = self.plugin_repo({"tools/g/claude-guard": True})
        with open(os.path.join(repo, "plugin", "hooks", "hooks.json"), "w") as handle:
            json.dump({"hooks": {"PreToolUse": [None]}}, handle)
        self.hooks_settings(os.path.join(self.bin, "claude-settings-gone"))
        tool = os.path.join(repo, "tools", "toolkit-integrity")
        os.makedirs(tool)
        os.makedirs(os.path.join(repo, "lib"))
        for src in (HOOK, os.path.join(TOOL_DIR, "toolkit_integrity_lib.py")):
            shutil.copy(src, tool)
        libdir = os.path.join(os.path.dirname(os.path.dirname(TOOL_DIR)), "lib")
        for name in ("toolkit_config.py", "hook_manifest.py"):
            shutil.copy(os.path.join(libdir, name), os.path.join(repo, "lib"))
        env = dict(os.environ)
        env["TOOLKIT_INTEGRITY_HOME"] = self.home
        env["TOOLKIT_INTEGRITY_STATE_PATH"] = self.state
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(self.tmp, "no-config.json")
        done = subprocess.run(
            [sys.executable, os.path.join(tool, "claude-toolkit-integrity")],
            input=json.dumps({"session_id": "s1", "cwd": self.tmp}),
            capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("claude-settings-gone", done.stdout)
        self.assertIn("unreadable", done.stdout)


class TestDeclaredCommands(unittest.TestCase):

    def test_every_event_and_matcher_is_walked(self):
        found = lib.declared_commands({"hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"command": "/a"},
                                              {"command": "/b"}]},
                {"matcher": "*", "hooks": [{"command": "/c"}]}],
            "SessionEnd": [{"hooks": [{"command": "/d"}]}]}})
        self.assertEqual(sorted(c for _, c in found), ["/a", "/b", "/c", "/d"])

    def test_malformed_shapes_yield_a_short_list_not_a_raise(self):
        for payload in ({"hooks": "nope"},
                        {"hooks": {"E": "nope"}},
                        {"hooks": {"E": ["nope"]}},
                        {"hooks": {"E": [{"hooks": "nope"}]}},
                        {"hooks": {"E": [{"hooks": [42]}]}},
                        {"hooks": {"E": [{"hooks": [{"command": 42}]}]}},
                        {"hooks": {"E": [{"hooks": [{"command": "  "}]}]}},
                        {"statusLine": "nope"},
                        None, [], "text"):
            self.assertEqual(lib.declared_commands(payload), [], repr(payload))


class TestCommandTarget(unittest.TestCase):

    def test_a_bare_absolute_path(self):
        self.assertEqual(lib.command_target("/usr/bin/thing"), "/usr/bin/thing")

    def test_arguments_are_dropped(self):
        self.assertEqual(lib.command_target("/bin/x release"), "/bin/x")

    def test_a_quoted_path_containing_a_space_survives_whole(self):
        """`.split()` would truncate it to a prefix and report it missing.

        A false alarm on a healthy hook is the direction that gets a notice
        turned off, so it costs more than a miss here.
        """
        self.assertEqual(lib.command_target('"/opt/my tools/x" go'),
                         "/opt/my tools/x")

    def test_a_relative_or_bare_command_is_not_our_business(self):
        for command in ("pgrep", "./x", "x/y", ""):
            self.assertIsNone(lib.command_target(command))

    def test_unbalanced_quotes_fall_back_rather_than_raising(self):
        self.assertEqual(lib.command_target('/bin/x "unclosed'), "/bin/x")


# ============================================================ check A, part 2

class TestBrokenLinks(Sandbox):
    """What is on disk, independent of anything declared."""

    def test_a_healthy_link_is_not_a_problem(self):
        self.make_tool("claude-guard")
        self.assertEqual(lib.broken_links(self.home), [])

    def test_a_dangling_link_is_reported_with_link_and_target(self):
        target, link = self.make_tool("claude-guard")
        self.break_link(link)
        found = lib.broken_links(self.home)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["link"], link)
        self.assertEqual(found[0]["target"], target)
        self.assertIn("missing", found[0]["why"])

    def test_a_link_in_local_bin_that_is_not_executable_is_reported(self):
        self.make_tool("claude-guard", executable=False)
        found = lib.broken_links(self.home)
        self.assertEqual(len(found), 1)
        self.assertIn("not executable", found[0]["why"])

    def test_a_markdown_command_is_never_required_to_be_executable(self):
        """The control for the test above. Without it, moving the +x check up
        one level would report every agent and command on the machine."""
        source = os.path.join(self.real, "build.md")
        with open(source, "w") as handle:
            handle.write("# build\n")
        os.chmod(source, 0o644)
        os.symlink(source, os.path.join(self.home, ".claude", "commands",
                                        "build.md"))
        self.assertEqual(lib.broken_links(self.home), [])

    def test_a_dangling_markdown_command_IS_reported(self):
        source = os.path.join(self.real, "build.md")
        with open(source, "w") as handle:
            handle.write("# build\n")
        link = os.path.join(self.home, ".claude", "commands", "build.md")
        os.symlink(source, link)
        os.unlink(source)
        self.assertEqual([r["link"] for r in lib.broken_links(self.home)],
                         [link])

    def test_a_link_this_repo_does_not_own_is_still_reported(self):
        """install.sh's prune loop skips these because it DELETES. This only
        reports, and a dangling entry in ~/.local/bin is a defect regardless
        of who made it."""
        os.symlink("/nowhere/at/all", os.path.join(self.bin, "someone-elses"))
        self.assertEqual(len(lib.broken_links(self.home)), 1)

    def test_a_real_file_is_not_a_link_and_is_ignored(self):
        path = os.path.join(self.bin, "plain")
        with open(path, "w") as handle:
            handle.write("x")
        os.chmod(path, 0o644)
        self.assertEqual(lib.broken_links(self.home), [])

    def test_an_absent_install_directory_is_not_a_problem(self):
        shutil.rmtree(os.path.join(self.home, ".claude", "skills"))
        self.assertEqual(lib.broken_links(self.home), [])

    def test_the_skills_directory_is_walked(self):
        """It is a per-DIRECTORY link, unlike the others, and leaving it out
        would reproduce the autonomy-permissions defect one level over."""
        source = os.path.join(self.real, "some-skill")
        os.makedirs(source)
        link = os.path.join(self.home, ".claude", "skills", "some-skill")
        os.symlink(source, link)
        self.assertEqual(lib.broken_links(self.home), [])
        shutil.rmtree(source)
        self.assertEqual([r["link"] for r in lib.broken_links(self.home)],
                         [link])

    def test_the_build_lens_directory_is_walked(self):
        """build-lens is the dispatch path for all four /build review gates,
        so a dangling link there is as silent as a dangling hook link --
        leaving it out would reproduce the exact defect this repo's own
        CLAUDE.md records for `skills` one directory over."""
        source = os.path.join(self.real, "blinded-review.js")
        with open(source, "w") as handle:
            handle.write("// review lens\n")
        link = os.path.join(self.home, ".claude", "build-lens",
                            "blinded-review.js")
        os.symlink(source, link)
        self.assertEqual(lib.broken_links(self.home), [])
        os.unlink(source)
        self.assertEqual([r["link"] for r in lib.broken_links(self.home)],
                         [link])


class TestSignature(Sandbox):

    def test_a_clean_machine_signs_as_empty(self):
        self.assertEqual(lib.problems_signature(
            lib.integrity_problems(self.home)), "")

    def test_a_new_problem_changes_the_signature(self):
        _, first = self.make_tool("claude-a")
        _, second = self.make_tool("claude-b")
        self.break_link(first)
        one = lib.problems_signature(lib.integrity_problems(self.home))
        self.break_link(second)
        two = lib.problems_signature(lib.integrity_problems(self.home))
        self.assertTrue(one)
        self.assertNotEqual(one, two)

    def test_the_signature_is_stable_across_calls(self):
        _, link = self.make_tool("claude-a")
        self.break_link(link)
        self.assertEqual(
            lib.problems_signature(lib.integrity_problems(self.home)),
            lib.problems_signature(lib.integrity_problems(self.home)))


# ==================================================================== check B

class TestDrift(Sandbox):

    def test_root_and_head_reads_a_real_repo(self):
        repo = self.make_repo()
        pair = lib.root_and_head(repo)
        self.assertIsNotNone(pair)
        root, head = pair
        self.assertEqual(os.path.realpath(root), os.path.realpath(repo))
        self.assertEqual(head, git("rev-parse", "HEAD", cwd=repo))

    def test_a_directory_that_is_not_a_repo_returns_none(self):
        """A tarball install has the identical layout and no git. Check B must
        be silent there rather than wrong."""
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertIsNone(lib.root_and_head(plain))

    def test_an_empty_cwd_returns_none_rather_than_describing_this_process(self):
        self.assertIsNone(lib.root_and_head(""))

    def test_changed_paths_lists_what_moved(self):
        repo = self.make_repo()
        old = git("rev-parse", "HEAD", cwd=repo)
        new = self.commit_file(repo, "tools/x/y.py", "x\n", "add")
        self.assertEqual(lib.changed_paths(repo, old, new),
                         [("A", "tools/x/y.py")])

    def test_changed_paths_is_none_when_git_cannot_answer(self):
        """None and [] mean opposite things and the caller acts differently."""
        repo = self.make_repo()
        head = git("rev-parse", "HEAD", cwd=repo)
        self.assertIsNone(lib.changed_paths(repo, "0" * 40, head))
        self.assertIsNone(lib.changed_paths(repo, "", head))
        self.assertEqual(lib.changed_paths(repo, head, head), [])

    def test_live_changes_keeps_only_what_a_session_executes(self):
        self.assertEqual(
            lib.live_changes([("M", "docs/a.md"), ("M", "README.md"),
                              ("M", "lib/tests/t.py"),
                              ("A", "tools/base-pin/tests/test_base_pin.py"),
                              ("M", "CLAUDE.md.template"),
                              ("M", "tools/danger-guard/g.py"),
                              ("M", "agents/x.md"),
                              ("M", "skills/builder/build/SKILL.md"),
                              ("M", "commands/old.md"),
                              ("M", "skills/s/SKILL.md"),
                              ("M", "lib/toolkit_config.py"),
                              ("M", "plugin/hooks/hooks.json")]),
            [("M", "tools/danger-guard/g.py"), ("M", "agents/x.md"),
             ("M", "skills/builder/build/SKILL.md"), ("M", "skills/s/SKILL.md"),
             ("M", "lib/toolkit_config.py"),
             ("M", "plugin/hooks/hooks.json")])

    def test_lib_is_live_because_it_is_imported_at_RUN_time(self):
        """`lib/toolkit_config.py` is read by fifteen live hooks, this one
        included, so a lib-only merge really does change hook behaviour
        mid-session. It was omitted at first as "build-time", with a comment
        that said so."""
        self.assertEqual(lib.live_changes([("M", "lib/toolkit_config.py")]),
                         [("M", "lib/toolkit_config.py")])

    def test_a_test_file_under_a_live_prefix_is_not_live(self):
        """The control for the entry above. Nothing imports a test at runtime,
        and including them would put the notice back in skim-past territory."""
        self.assertEqual(
            lib.live_changes([("M", "lib/tests/test_x.py"),
                              ("A", "tools/git-tidy/tests/test_git_tidy.py")]),
            [])

    def test_a_path_that_merely_starts_with_a_live_word_is_not_live(self):
        """`tools/` with the slash, not `tools`. Without the slash, a file
        called `toolsmith.md` at the repo root would read as live."""
        self.assertEqual(
            lib.live_changes([("M", "toolsmith.md"), ("A", "agentsy/x"),
                              ("M", "plugin/hooks/hooks.json.bak")]), [])

    def test_no_live_change_renders_nothing(self):
        self.assertEqual(lib.render_drift("a" * 40, "b" * 40, []), "")

    def test_a_hooks_settings_change_adds_the_install_line(self):
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("M", lib.PLUGIN_HOOKS)])
        self.assertIn("plugin/hooks/hooks.json changed: run /reload-plugins "
                      "or start a new", text)
        self.assertIn("session before it takes effect.", text)
        self.assertNotIn("./install.sh\nfrom the primary", text)
        self.assertIn("session before it takes effect", text)

    def test_an_ordinary_live_change_does_not_claim_a_reinstall_is_needed(self):
        """The control. Without it the install line could be unconditional."""
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("M", "tools/danger-guard/g.py")])
        self.assertIn("tools/danger-guard/g.py", text)
        self.assertNotIn("new session", text)

    def test_an_undiffable_move_says_so_rather_than_going_quiet(self):
        text = lib.render_drift("a" * 40, "b" * 40, [], undiffable=True)
        self.assertIn("could not be listed", text)


class TestAnAddedFileIsNotAlreadyLive(Sandbox):
    """The defect this whole added/changed split exists for.

    The notice once told every reader "the code is already live"
    for any live path. That is true behind an existing symlink and FALSE for a
    file the pull just added, because a pull creates no symlink -- and neither
    integrity check can see that case: there is no directory entry to enumerate
    and no settings.json command naming it, so the machine reports clean.
    """

    def test_git_reports_an_added_path_as_added(self):
        """Against the real git, not a hand-built pair: the status letters are
        git's to choose, and a test that supplied its own would pass while the
        parser read the wrong field."""
        repo = self.make_repo()
        old = git("rev-parse", "HEAD", cwd=repo)
        new = self.commit_file(repo, "tools/x/y.py", "x\n", "add")
        self.assertEqual(lib.changed_paths(repo, old, new),
                         [("A", "tools/x/y.py")])

    def test_git_reports_an_edited_path_as_modified(self):
        """The control for the entry above. Without it, a parser that hardcoded
        'A' would pass every added-file test in this class."""
        repo = self.make_repo()
        self.commit_file(repo, "tools/x/y.py", "x\n", "add")
        old = git("rev-parse", "HEAD", cwd=repo)
        new = self.commit_file(repo, "tools/x/y.py", "changed\n", "edit")
        self.assertEqual(lib.changed_paths(repo, old, new),
                         [("M", "tools/x/y.py")])

    def test_a_rename_reports_the_DESTINATION_not_the_vanished_source(self):
        """`R100\told\tnew` has three fields. Reading the second one names the
        path that no longer exists as the thing needing a link, and the file
        that actually has no symlink goes unmentioned."""
        repo = self.make_repo()
        # Ambient state, and it turns someone's `install.sh` red rather than
        # failing here: with `diff.renames = false` in ~/.gitconfig git emits
        # `D old` / `A new` and the source path never vanishes from the list.
        # Set LOCAL, which beats global, rather than asserting the machine's.
        # The production code is correct either way -- `A new` still marks the
        # destination added -- so this is test-only. Do not "fix" the lib.
        git("config", "diff.renames", "true", cwd=repo)
        self.commit_file(repo, "tools/x/y.py", "x" * 200 + "\n", "add")
        old = git("rev-parse", "HEAD", cwd=repo)
        os.rename(os.path.join(repo, "tools/x/y.py"),
                  os.path.join(repo, "tools/x/z.py"))
        git("add", "-A", cwd=repo)
        git("commit", "-q", "-m", "rename", cwd=repo)
        new = git("rev-parse", "HEAD", cwd=repo)
        pairs = lib.changed_paths(repo, old, new)
        paths = [path for _, path in pairs]
        self.assertIn("tools/x/z.py", paths)
        self.assertNotIn("tools/x/y.py", paths)
        self.assertTrue(any(lib.is_added(status) for status, _ in pairs),
                        "a rename destination has no symlink either: %r"
                        % (pairs,))

    def test_is_added_scores_the_first_letter_so_similarity_does_not_break_it(
            self):
        """git suffixes rename and copy statuses with a similarity score. An
        exact-match test classifies every one of them as an ordinary edit."""
        self.assertTrue(lib.is_added("A"))
        self.assertTrue(lib.is_added("R100"))
        self.assertTrue(lib.is_added("C075"))
        self.assertFalse(lib.is_added("M"))
        self.assertFalse(lib.is_added("D"))
        self.assertFalse(lib.is_added(""))
        self.assertFalse(lib.is_added(None))

    def test_an_added_live_file_is_NOT_called_already_live(self):
        """The assertion that would have caught the original defect."""
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("A", "tools/build-lens/merge-review.js")])
        self.assertIn("install.sh --links-only", text)
        self.assertIn("(new)", text)
        self.assertIn("creates no symlink", text)

    def test_a_new_file_is_not_declared_inert_when_it_may_not_be(self):
        """`lib/` is in LIVE_PREFIXES and has ZERO `link` calls in install.sh,
        so a new lib module renders (new) while being live on import. An
        unconditional "newly added is inert until the installer runs" is false
        there, and its named remedy provably cannot apply: --links-only answers
        "every link is in place". Line 443 of the lib forbids exactly that --
        a remedy that does nothing is how a notice trains its reader to skip
        it. So the text must state the condition, not the state."""
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("A", "lib/new_helper.py")])
        self.assertNotIn("is inert on this machine", text)
        self.assertIn("A new\n~/.local/bin entry point needs the installer", text)
        self.assertIn("needs nothing", text)

    def test_the_notice_describes_plugin_delivery_not_symlinks(self):
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("M", "tools/danger-guard/g.py")])
        self.assertIn("load from that checkout through the toolkit's plugins", text)
        self.assertNotIn("symlinks into that checkout", text)

    def test_the_new_paragraph_says_reload_for_skills_and_agents(self):
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("A", "skills/new/SKILL.md")])
        self.assertIn("/reload-plugins", text)
        self.assertIn("skill or agent", text)
        self.assertIn("install.sh --links-only", text)

    def test_the_added_branch_names_the_primary_checkout(self):
        """`--links-only` compares each link against $REPO, so run from a
        linked worktree it reports all 44 absent and exits 1 on a fully
        installed machine. This repo's own hard rule pushes concurrent sessions
        into worktrees, so the notice has to say where to run it."""
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("A", "tools/build-lens/merge-review.js")])
        self.assertIn("PRIMARY", text)

    def test_the_reassuring_sentence_is_scoped_to_CHANGED_files(self):
        """Found by mutation: with only the tests above, reverting the sentence
        to the blanket "the code is already live" survived untouched, because
        every one of them asserts what the notice ADDS and none pins what it
        stopped claiming. The added lines are the remedy; this sentence is the
        defect, and a notice that hands you a command while still telling you
        nothing is wrong is one you act on the wrong half of."""
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("A", "tools/build-lens/merge-review.js")])
        self.assertNotIn("the code is already live", text)
        self.assertIn("CHANGED files are already live", text)

    def test_a_changed_live_file_still_says_it_is_already_live(self):
        """The control. Without it the added-file wording could be
        unconditional -- which is the same defect pointed the other way, and
        would put the notice straight back into skim-past territory."""
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("M", "tools/danger-guard/g.py")])
        self.assertIn("already", text)
        self.assertNotIn("--links-only", text)
        self.assertNotIn("(new)", text)

    def test_a_mixed_range_marks_only_the_added_one(self):
        """A real pull is mostly edits with one new tool in it. The notice has
        to point at that one rather than at all of them."""
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("M", "tools/danger-guard/g.py"),
                                 ("A", "tools/build-lens/merge-review.js")])
        self.assertIn("tools/build-lens/merge-review.js   (new)", text)
        self.assertIn("tools/danger-guard/g.py\n", text)
        self.assertNotIn("tools/danger-guard/g.py   (new)", text)

    def test_the_hooks_settings_line_still_fires_from_a_pair(self):
        """`HOOKS_SETTINGS in live` was a membership test against bare strings
        and silently stopped matching once `live` held tuples -- a line that
        vanishes is invisible in a rendered-output test that only asserts what
        IS present."""
        text = lib.render_drift("a" * 40, "b" * 40,
                                [("M", lib.PLUGIN_HOOKS)])
        self.assertIn("session before it takes effect", text)

    def test_a_malformed_pair_is_dropped_rather_than_crashing_the_hook(self):
        """This runs on every prompt submit. An unpacking error here takes out
        the loud integrity half that is written to stdout alongside it."""
        self.assertEqual(
            lib.live_changes([("M", "tools/a.py"), "tools/b.py", None,
                              ("M",), ("M", "tools/c.py", "extra"),
                              (None, "tools/d.py"), ("M", None)]),
            [("M", "tools/a.py")])


# ===================================================================== state

class TestState(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "state.json")

    def test_round_trip(self):
        state = lib.record_reported({}, "s1", "head", "abc", now=1000.0)
        self.assertTrue(lib.save_state(state, self.path))
        self.assertTrue(lib.already_reported(
            lib.load_state(self.path), "s1", "head", "abc"))

    def test_a_different_value_is_not_already_reported(self):
        state = lib.record_reported({}, "s1", "head", "abc", now=1000.0)
        self.assertFalse(lib.already_reported(state, "s1", "head", "def"))

    def test_keys_do_not_collide(self):
        state = lib.record_reported({}, "s1", "head", "abc", now=1000.0)
        state = lib.record_reported(state, "s1", "integrity", "sig", now=1000.0)
        self.assertTrue(lib.already_reported(state, "s1", "head", "abc"))
        self.assertTrue(lib.already_reported(state, "s1", "integrity", "sig"))

    def test_sessions_do_not_collide(self):
        state = lib.record_reported({}, "s1", "head", "abc", now=1000.0)
        self.assertFalse(lib.already_reported(state, "s2", "head", "abc"))

    def test_prune_drops_expired_and_malformed_records(self):
        now = 10_000_000.0
        state = {
            "fresh": {"head": {"value": "a", "at": now - 10}},
            "stale": {"head": {"value": "a", "at": now - lib.STATE_TTL_SECONDS - 1}},
            "notadict": "nope",
            "badrecord": {"head": "nope"},
            "nostamp": {"head": {"value": "a"}},
            "novalue": {"head": {"at": now}},
        }
        self.assertEqual(lib.prune_state(state, now=now),
                         {"fresh": {"head": {"value": "a", "at": now - 10}}})

    def test_an_unreadable_state_file_loads_as_empty(self):
        self.assertEqual(lib.load_state(os.path.join(self.tmp, "nope")), {})

    def test_saving_into_an_unwritable_place_returns_false(self):
        self.assertFalse(lib.save_state({}, "/dev/null/not-a-path/x.json"))


# ============================================================= the executable

class TestHookWiring(Sandbox):
    """The hook as a real subprocess. Lib correctness does not imply this."""

    def read_state(self):
        with open(self.state) as handle:
            return json.load(handle)

    # --- integrity ---------------------------------------------------------

    def test_a_dangling_hook_link_is_reported_naming_link_and_target(self):
        target, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.break_link(link)
        out = self.run_hook()
        self.assertIn("claude-danger-guard", out)
        self.assertIn(target, out)
        self.assertIn("already dark", out)

    def test_a_non_executable_hook_is_reported_BY_THE_HOOK_CHECK(self):
        """Asserts the hooks section specifically, not just the words.

        Both checks see a non-executable `~/.local/bin` entry -- one because
        settings.json names it, one because it is a link without +x. A test
        that only greps for "not executable" therefore passes with the hook
        check deleted, via the link check's wording. Measured: that is exactly
        what happened, and the mutation deleting the hook check's `os.access`
        branch survived here while dying in the lib test.

        `PreToolUse:` is the `where` label, which only the hooks section emits.
        """
        _, link = self.make_tool("claude-danger-guard", executable=False)
        self.hooks_settings(link, event="PreToolUse")
        out = self.run_hook()
        self.assertIn("PreToolUse:", out)
        self.assertIn("not executable", out)
        self.assertIn("already dark", out)

    def test_a_healthy_machine_is_silent(self):
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.assertEqual(self.run_hook(), "")

    def test_the_same_problem_is_reported_once_per_session(self):
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.break_link(link)
        self.assertIn("claude-danger-guard", self.run_hook())
        self.assertEqual(self.run_hook(), "")

    def test_a_second_problem_appearing_mid_session_speaks_again(self):
        """The reason the dedupe key is a signature and not a boolean. A link
        breaking mid-session is the case the whole mid-session half exists
        for."""
        _, first = self.make_tool("claude-a")
        _, second = self.make_tool("claude-b")
        self.hooks_settings(first, second)
        self.break_link(first)
        self.assertIn("claude-a", self.run_hook())
        self.break_link(second)
        self.assertIn("claude-b", self.run_hook())

    def test_a_problem_that_is_fixed_and_then_returns_is_announced_again(self):
        """Why the empty signature is recorded rather than skipped.

        Record only when something was PRINTED and the stored signature stays
        the old problem set forever: fixing the link writes nothing, so when it
        breaks again the new signature equals the stored one and the notice is
        suppressed — silently, on the check whose entire job is to not be
        silent. No other test in this class reaches that path.
        """
        target, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.break_link(link)
        self.assertIn("claude-danger-guard", self.run_hook())
        with open(target, "w") as handle:            # fixed
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(target, 0o755)
        self.assertEqual(self.run_hook(), "")
        os.unlink(target)                            # broken again
        self.assertIn("claude-danger-guard", self.run_hook())

    def test_a_new_session_hears_it_again(self):
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.break_link(link)
        self.assertIn("claude-danger-guard", self.run_hook(session="s1"))
        self.assertIn("claude-danger-guard", self.run_hook(session="s2"))

    # --- drift -------------------------------------------------------------

    def test_the_baseline_persists_across_invocations(self):
        """The defect this build actually had.

        `save_state` sat behind `if not out: return`, so the baseline -- which
        is recorded on a run that by design prints nothing -- was discarded
        every time. Every prompt then re-recorded the CURRENT head as the
        baseline, and drift could never fire. Every lib test still passed.
        """
        repo = self.make_repo()
        head = git("rev-parse", "HEAD", cwd=repo)
        self.assertEqual(self.run_hook(root=repo), "")
        self.assertEqual(self.read_state()["s1"]["head"]["value"], head)

    def test_a_live_change_is_reported_once(self):
        repo = self.make_repo()
        self.assertEqual(self.run_hook(root=repo), "")          # baseline
        self.commit_file(repo, "tools/danger-guard/guard.py", "x\n", "move")
        out = self.run_hook(root=repo)
        self.assertIn("MOVED UNDER THIS SESSION", out)
        self.assertIn("tools/danger-guard/guard.py", out)
        self.assertEqual(self.run_hook(root=repo), "")           # deduped

    def test_THE_HOOK_tells_you_an_added_file_needs_the_installer(self):
        """Wiring, not the lib. `render_drift` is mutation-covered above, and
        this repo has shipped a correct lib behind wiring that never reached it
        -- `save_state` behind an `if not out: return` killed the whole drift
        baseline with every lib test green. The status has to survive
        changed_paths -> live_changes -> render_drift through the real hook, and
        an unpacking error anywhere in that chain is caught by `except: exit(0)`
        and looks exactly like a clean machine."""
        repo = self.make_repo()
        self.assertEqual(self.run_hook(root=repo), "")           # baseline
        self.commit_file(repo, "tools/build-lens/merge-review.js", "x\n", "add")
        out = self.run_hook(root=repo)
        self.assertIn("tools/build-lens/merge-review.js   (new)", out)
        self.assertIn("./install.sh --links-only", out)

    def test_THE_HOOK_does_not_say_that_about_an_edit(self):
        """The control. Without it the added-file branch could fire on every
        drift notice and this class would still be green -- which is the notice
        going back to a claim you cannot act on, pointed the other way."""
        repo = self.make_repo()
        self.commit_file(repo, "tools/danger-guard/guard.py", "v1\n", "add")
        self.assertEqual(self.run_hook(root=repo), "")           # baseline
        self.commit_file(repo, "tools/danger-guard/guard.py", "v2\n", "edit")
        out = self.run_hook(root=repo)
        self.assertIn("MOVED UNDER THIS SESSION", out)
        self.assertNotIn("--links-only", out)
        self.assertNotIn("(new)", out)

    def test_a_docs_only_change_is_silent(self):
        repo = self.make_repo()
        self.assertEqual(self.run_hook(root=repo), "")
        self.commit_file(repo, "docs/notes.md", "x\n", "docs")
        self.assertEqual(self.run_hook(root=repo), "")

    def test_a_docs_only_change_still_advances_the_baseline(self):
        """Otherwise a later live change is diffed from a stale base and
        reports the docs commit alongside it -- and the silent path is exactly
        where a missing write is invisible."""
        repo = self.make_repo()
        self.run_hook(root=repo)
        docs = self.commit_file(repo, "docs/notes.md", "x\n", "docs")
        self.run_hook(root=repo)
        self.assertEqual(self.read_state()["s1"]["head"]["value"], docs)

    def test_a_hooks_settings_change_names_the_extra_step(self):
        repo = self.make_repo()
        self.run_hook(root=repo)
        self.commit_file(repo, "plugin/hooks/hooks.json", "{}\n", "hooks")
        out = self.run_hook(root=repo)
        self.assertIn("plugin/hooks/hooks.json", out)
        self.assertIn("/reload-plugins", out)

    def test_a_move_after_a_move_speaks_again(self):
        repo = self.make_repo()
        self.run_hook(root=repo)
        self.commit_file(repo, "tools/a/x.py", "1\n", "one")
        self.assertIn("tools/a/x.py", self.run_hook(root=repo))
        self.commit_file(repo, "tools/b/y.py", "2\n", "two")
        self.assertIn("tools/b/y.py", self.run_hook(root=repo))

    def test_an_unreachable_baseline_reports_rather_than_going_quiet(self):
        """The undiffable branch, through the HOOK rather than in pieces.

        `changed_paths` returning None and `render_drift(undiffable=True)` were
        each tested alone, and nothing asserted they were connected -- the
        mutation `if paths is None: pass` left the whole suite green. That is
        the wiring-not-predicate shape CLAUDE.md records as base-pin's BLOCKING
        defect.

        A baseline sha that no longer exists is the realistic way in: a
        force-push or a gc between two prompts.
        """
        repo = self.make_repo()
        self.run_hook(root=repo)                              # real baseline
        with open(self.state) as handle:
            state = json.load(handle)
        state["s1"]["head"]["value"] = "0" * 40               # unreachable now
        with open(self.state, "w") as handle:
            json.dump(state, handle)
        self.commit_file(repo, "tools/a/x.py", "1\n", "one")
        out = self.run_hook(root=repo)
        self.assertIn("could not be listed", out)
        # And it advances, so it is not re-attempted every prompt forever.
        self.assertEqual(self.run_hook(root=repo), "")

    def test_a_non_git_root_is_silent_and_exits_zero(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(self.run_hook(root=plain), "")

    # --- refusals ----------------------------------------------------------

    def test_garbage_on_stdin_exits_zero_and_says_nothing(self):
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.break_link(link)
        env = dict(os.environ)
        env["TOOLKIT_INTEGRITY_HOME"] = self.home
        env["TOOLKIT_INTEGRITY_STATE_PATH"] = self.state
        done = subprocess.run([sys.executable, HOOK], input="not json",
                              capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_a_payload_with_no_session_says_nothing(self):
        """No session means no dedupe key, and a check that cannot dedupe would
        repeat on every prompt submit for as long as the condition held."""
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.break_link(link)
        env = dict(os.environ)
        env["TOOLKIT_INTEGRITY_HOME"] = self.home
        env["TOOLKIT_INTEGRITY_STATE_PATH"] = self.state
        done = subprocess.run([sys.executable, HOOK], input=json.dumps({}),
                              capture_output=True, text=True, env=env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, "")

    def test_the_escape_hatch_silences_both_halves(self):
        repo = self.make_repo()
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.break_link(link)
        self.assertEqual(
            self.run_hook(root=repo,
                          extra_env={"TOOLKIT_INTEGRITY_ALLOW": "1"}), "")
        # The control: without it, the same state speaks.
        self.assertIn("claude-danger-guard", self.run_hook(root=repo))

    def test_drift_can_be_silenced_without_silencing_integrity(self):
        """The two halves are not worth the same, so they turn off separately.

        Without this test `drift_enabled()` has no caller any test reaches, and
        replacing the `if integrity.drift_enabled():` guard with `if True:`
        survives — a config key that reads as supported and does nothing.
        """
        repo = self.make_repo()
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        config = os.path.join(self.tmp, "config.json")
        with open(config, "w") as handle:
            json.dump({"toolkit_integrity": {"drift_notice": False}}, handle)
        env = {"CLAUDE_TOOLKIT_CONFIG": config}

        self.run_hook(root=repo, extra_env=env)              # baseline
        self.commit_file(repo, "tools/a/x.py", "1\n", "one")
        self.break_link(link)
        out = self.run_hook(root=repo, extra_env=env)
        self.assertIn("CANNOT RUN", out)                     # kept
        self.assertNotIn("MOVED UNDER THIS SESSION", out)    # silenced

    def test_disabling_the_tool_silences_it_entirely(self):
        repo = self.make_repo()
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.break_link(link)
        config = os.path.join(self.tmp, "config.json")
        with open(config, "w") as handle:
            json.dump({"toolkit_integrity": {"enabled": False}}, handle)
        self.assertEqual(
            self.run_hook(root=repo,
                          extra_env={"CLAUDE_TOOLKIT_CONFIG": config}), "")

    def test_an_unwritable_state_path_still_delivers_the_notice(self):
        """The notice is the product; the dedupe is a convenience. Losing the
        write must cost a repeat, never the report."""
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.break_link(link)
        self.state = "/dev/null/not-a-path/state.json"
        self.assertIn("claude-danger-guard", self.run_hook())


class TestTimeoutBudget(unittest.TestCase):
    """Registration, and a git budget that fits the timeout it is given.

    Neither is a style check. A tool nothing invokes is installed inert and
    looks exactly like a healthy machine; and if the harness kills this hook
    mid-git it produces no output, which for a notice tool is
    indistinguishable from "nothing to report". Both failures are SILENT, on
    the tool whose entire job is to not be silent.

    Same shape as `base-pin`'s and `session-claim`'s. If you change one of the
    numbers, run this.
    """

    EVENTS = ("SessionStart", "UserPromptSubmit")

    def _manifest(self):
        return hook_manifest.load(os.path.dirname(os.path.dirname(TOOL_DIR)))

    def _declared_timeout(self, event):
        for row in hook_manifest.registrations(self._manifest()):
            if row["event"] == event and row["exe"] == "claude-toolkit-integrity":
                self.assertEqual(
                    "tools/toolkit-integrity/claude-toolkit-integrity",
                    row["relpath"])
                return row["timeout"]
        return None

    def test_the_hook_is_registered_on_both_events(self):
        """A tool nothing invokes is installed inert, and looks fine.

        Measured before this existed: deleting BOTH entries from
        the plugin manifest left the whole suite green.
        """
        for event in self.EVENTS:
            self.assertIsNotNone(
                self._declared_timeout(event),
                "claude-toolkit-integrity missing from the plugin manifest "
                "%s" % event)

    def test_the_run_budget_fits_every_declared_timeout(self):
        hook = runpy.run_path(HOOK, run_name="integrity_hook_under_test")
        total = hook["RUN_BUDGET_SECONDS"] + hook["HOST_MARGIN_SECONDS"]
        for event in self.EVENTS:
            declared = self._declared_timeout(event)
            self.assertIsNotNone(declared)
            self.assertLessEqual(
                total, declared,
                "%s: run budget + host margin = %ds exceeds the declared %ss "
                "timeout" % (event, total, declared))
        worst = lib.MAX_GIT_CALLS * lib.GIT_TIMEOUT_SECONDS
        self.assertLessEqual(
            worst, total,
            "%d git calls x %ds = %ds exceeds the run budget + margin (%ds)"
            % (lib.MAX_GIT_CALLS, lib.GIT_TIMEOUT_SECONDS, worst, total))

    def test_max_git_calls_matches_the_code(self):
        """Re-derived by counting call sites, never declared in prose.

        A hardcoded count is prose in Python: a new lib function calling `_git`
        on the reporting path would leave the budget claim untouched and the
        suite green. Counting over-counts if a `_git` caller is off the
        reporting path, which errs toward a stricter budget.
        """
        with open(os.path.join(TOOL_DIR, "toolkit_integrity_lib.py")) as handle:
            source = handle.read()
        sites = [line for line in source.splitlines()
                 if "_git(" in line
                 and not line.lstrip().startswith("#")
                 and not line.lstrip().startswith("def _git")]
        self.assertEqual(
            len(sites), lib.MAX_GIT_CALLS,
            "MAX_GIT_CALLS is %d but the lib has %d _git call site(s):\n%s"
            % (lib.MAX_GIT_CALLS, len(sites), "\n".join(sites)))

    def test_the_loud_half_spends_no_git_at_all(self):
        """Pins ORDERING only: the integrity notice is written before git.

        Not a delivery guarantee: the host discards all output when it kills
        a hook on timeout, printed or not. The watchdog (`TestWatchdog`) is
        what bounds the process. Asserted by position in the hook rather than
        by timing, which would be an assertion against the machine.
        """
        with open(HOOK) as handle:
            body = handle.read()
        printed = body.index("sys.stdout.write(notice)")
        first_git_user = body.index("integrity.root_and_head(")
        self.assertLess(
            printed, first_git_user,
            "the integrity notice is written after the drift half's git work; "
            "a slow git would take the loud notice with it")


class TestWatchdog(Sandbox):
    """The whole process is bounded, wherever it blocks.

    `timeout=8` below is a catastrophe bound so a broken watchdog fails the
    test instead of hanging it. It is not the budget under test (1s).
    """

    NOTICE = "ran out of time after 1s and did NOT finish"

    def watchdog_env(self, state):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("GIT_")}
        env["TOOLKIT_INTEGRITY_HOME"] = self.home
        env["TOOLKIT_INTEGRITY_ROOT"] = self.real
        env["TOOLKIT_INTEGRITY_STATE_PATH"] = state
        env["TOOLKIT_INTEGRITY_RUN_BUDGET"] = "1"
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(self.tmp, "no-config.json")
        env.pop("TOOLKIT_INTEGRITY_ALLOW", None)
        return env

    PAYLOAD = json.dumps({"session_id": "s1"})

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs os.mkfifo")
    def test_a_blocked_state_read_is_cut_off_by_the_watchdog(self):
        fifo = os.path.join(self.tmp, "state.fifo")
        os.mkfifo(fifo)
        done = subprocess.run(
            [sys.executable, HOOK], input=self.PAYLOAD, capture_output=True,
            text=True, env=self.watchdog_env(fifo), timeout=8)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(self.NOTICE, done.stdout)

    def test_a_stdin_that_never_closes_is_cut_off(self):
        proc = subprocess.Popen(
            [sys.executable, HOOK], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=self.watchdog_env(self.state))
        try:
            # Never written, never closed: communicate() with no input would
            # close it. The notice is tiny, so it sits in the pipe buffer
            # while wait() carries the catastrophe bound.
            proc.wait(timeout=8)
            out = proc.stdout.read()
        finally:
            proc.stdin.close()
            proc.stdout.close()
            proc.stderr.close()
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0)
        self.assertIn(self.NOTICE, out)

    def test_a_healthy_run_says_nothing_about_time(self):
        done = subprocess.run(
            [sys.executable, HOOK], input=self.PAYLOAD, capture_output=True,
            text=True, env=self.watchdog_env(self.state), timeout=8)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("ran out of time", done.stdout)

    def test_the_seam_cannot_lengthen_the_budget(self):
        hook = runpy.run_path(HOOK, run_name="integrity_hook_under_test")
        default = hook["RUN_BUDGET_SECONDS"]
        for value in ("100", "0", "-1", "abc"):
            with mock.patch.dict(os.environ,
                                 {"TOOLKIT_INTEGRITY_RUN_BUDGET": value}):
                self.assertEqual(hook["_run_budget"](), default, value)
        with mock.patch.dict(os.environ,
                             {"TOOLKIT_INTEGRITY_RUN_BUDGET": "0.5"}):
            self.assertEqual(hook["_run_budget"](), 0.5)


    def test_a_hung_git_is_not_orphaned(self):
        if os.name == "nt":
            self.skipTest("needs a POSIX shell shim")
        shim_dir = os.path.join(self.tmp, "shim")
        os.makedirs(shim_dir)
        pidfile = os.path.join(self.tmp, "git.pid")
        shim = os.path.join(shim_dir, "git")
        with open(shim, "w") as handle:
            handle.write('#!/bin/sh\necho $$ > "$GIT_SHIM_PIDFILE"\n'
                         'exec sleep 30\n')
        os.chmod(shim, 0o755)
        env = self.watchdog_env(self.state)
        env["PATH"] = shim_dir + os.pathsep + env.get("PATH", "")
        env["GIT_SHIM_PIDFILE"] = pidfile
        # The production budget, not a short one. The budget is armed
        # before the lib import, and _git refuses with under
        # MIN_GIT_SECONDS + GIT_KILL_MARGIN_SECONDS left, so a 2s budget let
        # the pre-git work have 1s: that can run past 1s at
        # high load average, where git was (correctly) skipped and the shim
        # never ran. 6s leaves 5s. The wider catastrophe bound is still not
        # the budget under test.
        env["TOOLKIT_INTEGRITY_RUN_BUDGET"] = str(
            runpy.run_path(HOOK, run_name="integrity_hook_budget")["RUN_BUDGET_SECONDS"])
        subprocess.run([sys.executable, HOOK], input=self.PAYLOAD,
                       capture_output=True, text=True, env=env, timeout=30)
        # The pidfile is the positive proof the shim ran at all.
        self.assertTrue(os.path.exists(pidfile), "the git shim never ran")
        with open(pidfile) as handle:
            pid = int(handle.read().strip())

        def reap():
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass
        self.addCleanup(reap)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class TestGitDeadline(unittest.TestCase):
    """_git only gets the time left before the watchdog."""

    def setUp(self):
        self.addCleanup(setattr, lib, "_deadline", None)

    def run_git(self, deadline):
        lib.set_deadline(deadline)
        stub = mock.Mock(returncode=0, stdout=b"x")
        with mock.patch.object(lib.subprocess, "run",
                               return_value=stub) as run:
            result = lib._git("/tmp", "status")
        return result, run

    def test_git_gets_only_the_time_left_before_the_watchdog(self):
        _, run = self.run_git(time.monotonic() + 2.0)
        self.assertEqual(run.call_count, 1)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 1.5)

    def test_a_spent_deadline_spawns_no_git(self):
        result, run = self.run_git(time.monotonic() + 0.2)
        self.assertIsNone(result)
        run.assert_not_called()

    def test_no_deadline_keeps_the_full_git_timeout(self):
        result, run = self.run_git(None)
        self.assertEqual(result, "x")
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["timeout"],
                         lib.GIT_TIMEOUT_SECONDS)


class TestOwnership(Sandbox):
    """install.sh only removes links pointing into its own checkout."""

    def test_a_link_into_the_repo_is_ours(self):
        self.assertIs(lib.owns("/repo", "/home/.local/bin/x", "/repo/tools/x"),
                      True)

    def test_a_link_elsewhere_is_not_ours(self):
        self.assertIs(lib.owns("/repo", "/home/.local/bin/x", "/opt/other/x"),
                      False)

    def test_a_sibling_directory_is_not_ours(self):
        """`/repo-other` starts with `/repo` and is a different tree."""
        self.assertIs(lib.owns("/repo", "/home/.local/bin/x",
                               "/repo-other/tools/x"), False)

    def test_a_relative_target_resolves_against_the_links_own_directory(self):
        """`os.readlink` returns it as written; a raw prefix compare would call
        every relative link foreign."""
        self.assertIs(lib.owns("/repo", "/repo/sub/link", "../tools/x"), True)

    def test_an_unknown_repo_answers_none_rather_than_guessing(self):
        self.assertIsNone(lib.owns(None, "/home/.local/bin/x", "/repo/x"))
        self.assertIsNone(lib.owns("/repo", "/home/.local/bin/x", ""))

    def test_a_foreign_broken_link_says_install_sh_will_not_fix_it(self):
        """The remedy differs, so the notice must not give the same one.

        install.sh's prune loop skips a foreign target as "not ours", so
        telling someone to rerun it is advice that provably cannot work --
        repeated in every new session, because dedupe is per session.
        """
        os.symlink("/nowhere/at/all", os.path.join(self.bin, "foreign"))
        problems = lib.integrity_problems(self.home, repo=self.real)
        self.assertIs(problems["links"][0]["ours"], False)
        text = lib.render_integrity(problems, self.home)
        self.assertIn("NOT this toolkit's", text)
        self.assertIn("by hand", text)

    def test_an_owned_broken_link_does_not_get_that_line(self):
        """The control. Without it the caveat could be unconditional."""
        _, link = self.make_tool("claude-guard")
        self.break_link(link)
        problems = lib.integrity_problems(self.home, repo=self.real)
        self.assertIs(problems["links"][0]["ours"], True)
        text = lib.render_integrity(problems, self.home)
        self.assertIn("install.sh", text)
        self.assertNotIn("by hand", text)


# ================================================================ coverage

# Spelled out, like SANDBOX_DIRS: the fixture must not be built from
# `lib.REQUIRED_GUARDS`, or a mutation of that tuple breaks the fixture instead
# of the behaviour.
FIXTURE_GUARDS = (
    ("danger-guard", "claude-danger-guard", "Bash|PowerShell|Monitor"),
    ("worktree-guard", "claude-worktree-guard",
     "Bash|Monitor|Edit|Write|MultiEdit|NotebookEdit"),
    ("distill-guard", "claude-distill-write-guard", "Write"),
    ("model-guard", "claude-model-guard", "Agent"),
    ("session-claim", "claude-session-claim", "*"),
)


class TestCoverage(Sandbox):

    BUILDER_GUARD_DIRS = ("model-guard", "session-claim")

    def make_plugin(self, overrides=None, drop=(), event="PreToolUse",
                    builder=False, select=True, misplaced=()):
        """A fixture repo that registers all five guards.

        `overrides` maps a tool dir to a matcher (None = omit the key, a list =
        one registration per matcher); `drop` removes a guard's registration
        while leaving its file.

        Default: no registry (the pre-profile layout, only core is required),
        every guard in `<repo>/plugin/hooks/hooks.json`. With `builder=True` the
        repo has a registry (core in plugin/, builder in plugin-build/) and the
        builder guards (model-guard, session-claim) are registered in
        `plugin-build/hooks/hooks.json` with their files under
        `plugin-build/tools/`. `select` puts builder in the toolkit config
        (patched for the test's life); False leaves no config, so only
        enablement in settings can require it. `misplaced` names builder guards
        registered in core's file instead of builder's.
        """
        overrides = overrides or {}
        repo = os.path.join(self.tmp, "fixture")
        blocks = {"plugin": [], "plugin-build": []}
        if builder:
            TestCoverage.write_builder_registry(self, repo, select)
        for tool_dir, exe, matcher in FIXTURE_GUARDS:
            home_dir = ("plugin-build" if builder and tool_dir in
                        TestCoverage.BUILDER_GUARD_DIRS else "plugin")
            path = os.path.join(repo, home_dir, "tools", tool_dir, exe)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(path, 0o755)
            self.assertTrue(os.path.exists(hook_manifest.resolve(
                repo, "tools/%s/%s" % (tool_dir, exe),
                "builder" if home_dir == "plugin-build" else "core")))
            if tool_dir in drop:
                continue
            if tool_dir in misplaced:
                home_dir = "plugin"
            matchers = overrides.get(tool_dir, matcher)
            if not isinstance(matchers, list):
                matchers = [matchers]
            for one in matchers:
                block = {"hooks": [{"type": "command", "timeout": 10,
                                    "command": '"${CLAUDE_PLUGIN_ROOT}"/tools/%s/%s'
                                    % (tool_dir, exe)}]}
                if one is not None:
                    block["matcher"] = one
                blocks[home_dir].append(block)
        for home_dir in ("plugin", "plugin-build") if builder else ("plugin",):
            manifest = os.path.join(repo, home_dir, "hooks", "hooks.json")
            os.makedirs(os.path.dirname(manifest), exist_ok=True)
            with open(manifest, "w") as handle:
                json.dump({"hooks": {event: blocks[home_dir]}}, handle)
        return repo

    def write_builder_registry(self, repo, select):
        """claude/profiles.json with core + builder, and the toolkit config
        patched to select builder (or to be absent)."""
        entry = {"default": False, "requires": ["core"], "agents": False,
                 "summary": "s"}
        os.makedirs(os.path.join(repo, "claude"), exist_ok=True)
        with open(os.path.join(repo, "claude", "profiles.json"), "w") as handle:
            json.dump({"profiles": {
                "core": dict(entry, plugin="toolkit-core", dir="plugin",
                             default=True, requires=[]),
                "builder": dict(entry, plugin="toolkit-build", dir="plugin-build"),
            }}, handle)
        config = os.path.join(self.tmp, "builder-config.json")
        if select:
            with open(config, "w") as handle:
                json.dump({"profiles": ["builder"]}, handle)
        patcher = mock.patch.object(toolkit_config, "CONFIG_PATH", config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def copy_real_registrations(self, name="realcopy"):
        """A temp repo with the real registry and the five real hooks.json
        files; each plugin dir's `tools` is a symlink to the real tools."""
        real = os.path.dirname(os.path.dirname(TOOL_DIR))
        repo = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(repo, "claude"))
        shutil.copy(os.path.join(real, "claude", "profiles.json"),
                    os.path.join(repo, "claude", "profiles.json"))
        for directory in ("plugin", "plugin-continuity", "plugin-build",
                          "plugin-maintainer", "plugin-mac"):
            os.makedirs(os.path.join(repo, directory, "hooks"))
            shutil.copy(os.path.join(real, directory, "hooks", "hooks.json"),
                        os.path.join(repo, directory, "hooks", "hooks.json"))
            os.symlink(os.path.join(real, "tools"),
                       os.path.join(repo, directory, "tools"))
        return repo

    def edit_registrations(self, repo, directory, mutate):
        path = os.path.join(repo, directory, "hooks", "hooks.json")
        with open(path) as handle:
            data = json.load(handle)
        mutate(data["hooks"])
        with open(path, "w") as handle:
            json.dump(data, handle)

    def pull_model_guard(self, repo, into_core=False):
        """Remove model-guard's block from plugin-build's file; with
        `into_core`, append that block to core's PreToolUse instead."""
        moved = []

        def take(hooks):
            keep = []
            for block in hooks["PreToolUse"]:
                (moved if "claude-model-guard" in json.dumps(block)
                 else keep).append(block)
            hooks["PreToolUse"] = keep
        self.edit_registrations(repo, "plugin-build", take)
        self.assertEqual(len(moved), 1)
        if into_core:
            self.edit_registrations(
                repo, "plugin", lambda hooks: hooks["PreToolUse"].extend(moved))

    def enable_plugins(self, *plugins):
        self.write_settings({"enabledPlugins": {
            "%s@claude-code-toolkit" % name: True for name in plugins}})

    def write_selection(self, profiles):
        """Patch the toolkit config to select `profiles` (None = no config)."""
        config = os.path.join(self.tmp, "selection.json")
        if profiles is not None:
            with open(config, "w") as handle:
                json.dump({"profiles": profiles}, handle)
        patcher = mock.patch.object(toolkit_config, "CONFIG_PATH", config)
        patcher.start()
        self.addCleanup(patcher.stop)

    ALL_FIVE = ("toolkit-core", "toolkit-continuity", "toolkit-build",
                "toolkit-maintainer", "toolkit-mac")
    ALL_EXTRAS = ["continuity", "builder", "maintainer", "mac"]

    def test_the_shipped_manifest_covers_every_required_guard(self):
        # All five plugins enabled and every extra selected, so builder is
        # required and the real plugin-build file is what is checked.
        real = os.path.dirname(os.path.dirname(TOOL_DIR))
        self.enable_plugins(*self.ALL_FIVE)
        self.write_selection(self.ALL_EXTRAS)
        self.assertEqual(lib.coverage_problems(real, self.home), [])

    def test_the_shipped_manifest_without_builder_checks_only_core(self):
        # The default (no config, core only enabled) must stay clean too.
        real = os.path.dirname(os.path.dirname(TOOL_DIR))
        self.enable_plugins("toolkit-core")
        self.write_selection(None)
        self.assertEqual(lib.coverage_problems(real, self.home), [])

    def assert_model_guard_unregistered(self, found):
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0]["why"], "unregistered")
        self.assertEqual(found[0]["guard"], "tools/model-guard/claude-model-guard")

    def test_e1_builder_required_by_selection_only(self):
        repo = self.copy_real_registrations()
        self.pull_model_guard(repo)
        self.enable_plugins("toolkit-core")
        self.write_selection(self.ALL_EXTRAS)
        self.assert_model_guard_unregistered(
            lib.coverage_problems(repo, self.home))

    def test_e2_builder_required_by_enablement_only(self):
        repo = self.copy_real_registrations()
        self.pull_model_guard(repo)
        self.enable_plugins("toolkit-core", "toolkit-build")
        self.write_selection(None)
        self.assert_model_guard_unregistered(
            lib.coverage_problems(repo, self.home))

    def test_e3_builder_neither_selected_nor_enabled(self):
        repo = self.copy_real_registrations()
        self.pull_model_guard(repo)
        self.enable_plugins("toolkit-core")
        self.write_selection(None)
        self.assertEqual(lib.coverage_problems(repo, self.home), [])
        # Control: the same repo, intact, is clean only because builder is not
        # required; selecting builder makes the removal show.
        self.write_selection(["builder"])
        self.assert_model_guard_unregistered(
            lib.coverage_problems(repo, self.home))

    def test_e4_a_registration_moved_into_core_does_not_count(self):
        repo = self.copy_real_registrations()
        self.pull_model_guard(repo, into_core=True)
        self.enable_plugins("toolkit-core")
        self.write_selection(self.ALL_EXTRAS)
        self.assert_model_guard_unregistered(
            lib.coverage_problems(repo, self.home))

    def test_builder_guards_unrequired_without_builder(self):
        repo = self.make_plugin(builder=True, select=False,
                                drop=("model-guard",))
        self.enable_plugins("toolkit-core")
        self.assertEqual(lib.coverage_problems(repo, self.home), [])
        # Positive control: with builder selected the same repo reports it.
        self.write_selection(["builder"])
        self.assert_model_guard_unregistered(
            lib.coverage_problems(repo, self.home))

    def test_builder_guard_registered_only_in_core_is_unregistered(self):
        repo = self.make_plugin(builder=True, misplaced=("model-guard",))
        self.enable_plugins("toolkit-core")
        self.assert_model_guard_unregistered(
            lib.coverage_problems(repo, self.home))

    def test_a_full_fixture_manifest_has_no_coverage_problems(self):
        self.assertEqual(lib.coverage_problems(self.make_plugin()), [])

    def test_a_removed_registration_is_reported_unregistered(self):
        found = lib.coverage_problems(self.make_plugin(drop=("danger-guard",)))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["why"], "unregistered")
        self.assertEqual(found[0]["missing"], ["Bash", "PowerShell", "Monitor"])

    def test_required_guards_match_the_expected_contract(self):
        """REQUIRED_GUARDS is pinned against a set spelled out here.

        The shipped-manifest test only checks that hooks.json satisfies the
        tuple, so a shrunken tuple passed every test.
        """
        self.assertEqual(set(lib.REQUIRED_GUARDS), {
            ("core", "tools/danger-guard/claude-danger-guard", "PreToolUse",
             ("Bash", "PowerShell", "Monitor")),
            ("core", "tools/worktree-guard/claude-worktree-guard", "PreToolUse",
             ("Bash", "Monitor", "Edit", "Write", "MultiEdit",
              "NotebookEdit")),
            ("core", "tools/distill-guard/claude-distill-write-guard",
             "PreToolUse", ("Write",)),
            ("builder", "tools/model-guard/claude-model-guard", "PreToolUse",
             ("Agent",)),
            ("builder", "tools/session-claim/claude-session-claim",
             "PreToolUse", ("*",)),
        })

    def test_removing_any_required_registration_is_reported(self):
        for tool_dir, exe, _ in FIXTURE_GUARDS:
            with self.subTest(guard=tool_dir):
                found = lib.coverage_problems(
                    self.make_plugin(drop=(tool_dir,), builder=True))
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["guard"],
                                 "tools/%s/%s" % (tool_dir, exe))
                self.assertEqual(found[0]["why"], "unregistered")

    def test_a_narrowed_matcher_is_reported(self):
        found = lib.coverage_problems(
            self.make_plugin({"danger-guard": "Bash|Monitor"}))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["why"], "matcher too narrow")
        self.assertEqual(found[0]["missing"], ["PowerShell"])

    def test_session_claim_requires_a_wildcard(self):
        found = lib.coverage_problems(self.make_plugin(
            {"session-claim": "Bash|Edit|Write|MultiEdit|NotebookEdit"},
            builder=True))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["why"], "matcher too narrow")
        self.assertEqual(found[0]["missing"], ["*"])

    def test_a_star_or_empty_matcher_covers_everything(self):
        self.assertEqual(lib.coverage_problems(
            self.make_plugin({"session-claim": "*"}, builder=True)), [])
        self.assertEqual(lib.coverage_problems(
            self.make_plugin({"session-claim": None}, builder=True)), [])
        # Vacuity control: builder IS required on this fixture, so dropping
        # session-claim from it is reported.
        found = lib.coverage_problems(
            self.make_plugin({"session-claim": "*"}, builder=True,
                             drop=("session-claim",)))
        self.assertEqual(len(found), 1, found)
        self.assertEqual(found[0]["why"], "unregistered")
        self.assertEqual(found[0]["guard"],
                         "tools/session-claim/claude-session-claim")

    def test_split_registrations_union(self):
        repo = self.make_plugin({"worktree-guard": [
            "Bash|Monitor", "Edit|Write|MultiEdit|NotebookEdit"]})
        self.assertEqual(lib.coverage_problems(repo), [])

    def test_wrong_event_is_unregistered(self):
        repo = self.make_plugin(builder=True)
        manifest = os.path.join(repo, "plugin-build", "hooks", "hooks.json")
        with open(manifest) as handle:
            data = json.load(handle)
        blocks = data["hooks"]["PreToolUse"]
        model = [b for b in blocks if "model-guard" in json.dumps(b)]
        data["hooks"]["PreToolUse"] = [b for b in blocks if b not in model]
        data["hooks"]["PostToolUse"] = model
        with open(manifest, "w") as handle:
            json.dump(data, handle)
        found = lib.coverage_problems(repo)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["why"], "unregistered")
        self.assertEqual(found[0]["guard"], "tools/model-guard/claude-model-guard")

    def test_an_invalid_regex_matcher_does_not_raise(self):
        found = lib.coverage_problems(self.make_plugin({"model-guard": "("}, builder=True))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["missing"], ["Agent"])

    def test_unreadable_manifest_gives_no_coverage_records(self):
        repo = self.make_plugin()
        with open(os.path.join(repo, "plugin", "hooks", "hooks.json"), "w") as handle:
            handle.write("{not json")
        self.assertEqual(lib.coverage_problems(repo), [])
        found = lib.unrunnable_hooks(self.home, repo=repo)
        self.assertEqual([r["why"] for r in found], ["unreadable"])

    def test_coverage_changes_the_signature_and_renders(self):
        healthy = lib.integrity_problems(
            self.home, repo=self.make_plugin())
        broken = lib.integrity_problems(
            self.home, repo=self.make_plugin(drop=("danger-guard",)))
        self.assertNotEqual(lib.problems_signature(healthy),
                            lib.problems_signature(broken))
        text = lib.render_integrity(broken, self.home)
        self.assertIn("Guards the plugin no longer runs on every tool they "
                      "must cover:", text)
        self.assertIn("claude-danger-guard", text)
        self.assertIn("PowerShell", text)
        self.assertIn("/reload-plugins", text)
        # The repair advice names install.sh only to say it does NOT fix this;
        # what must never appear is the instruction to run it.
        self.assertNotIn("Fix: run ./install.sh", text)

    def test_the_hook_reports_a_removed_registration(self):
        repo = self.make_plugin(drop=("danger-guard",))
        out = self.run_hook(repo=repo, event="UserPromptSubmit")
        self.assertIn("Guards the plugin no longer runs on every tool they "
                      "must cover:", out)
        self.assertIn("claude-danger-guard", out)
        for tool in ("Bash", "PowerShell", "Monitor"):
            self.assertIn(tool, out)

    def test_the_hook_reports_a_narrowed_matcher(self):
        repo = self.make_plugin({"danger-guard": "Bash|Monitor"})
        out = self.run_hook(repo=repo, event="UserPromptSubmit")
        line = [l for l in out.splitlines() if "claude-danger-guard" in l]
        self.assertEqual(len(line), 1, out)
        self.assertIn("PowerShell", line[0])
        self.assertNotIn("Bash", line[0])


REAL_REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

# A stand-in for model-guard that behaves like the real one on the canary
# payloads: deny a dispatch with no "model", say nothing otherwise.
FAKE_MODEL_GUARD = (
    "#!/bin/sh\n"
    "if cat | grep -q '\"model\"'; then exit 0; fi\n"
    "echo '{\"hookSpecificOutput\": {\"permissionDecision\": \"deny\"}}'\n"
)
DENY_JSON = '{"hookSpecificOutput": {"permissionDecision": "deny"}}'
CANARY_HEADER = "A GUARD FAILED ITS CANARY"
DANGER = "tools/danger-guard/claude-danger-guard"
MODEL = "tools/model-guard/claude-model-guard"


class TestCanaries(Sandbox):

    def make_canary_repo(self, danger_body="#!/bin/sh\nexit 0\n",
                         model_body=FAKE_MODEL_GUARD):
        """A five-guard fixture whose danger/model guards are the given scripts.

        Real files under <repo>/plugin/tools/; the resolved path is asserted
        to exist so a wrong layout fails as a fixture error, not a verdict.
        """
        repo = TestCoverage.make_plugin(self, builder=True)
        for relpath, body, profile in ((DANGER, danger_body, "core"),
                                       (MODEL, model_body, "builder")):
            path = hook_manifest.resolve(repo, relpath, profile)
            self.assertTrue(os.path.exists(path), path)
            with open(path, "w") as handle:
                handle.write(body)
            os.chmod(path, 0o755)
        return repo

    def probes(self, repo, seconds=5):
        return lib.canary_problems(repo, time.monotonic() + seconds)

    def hermetic_env(self, **extra):
        env = {"CLAUDE_TOOLKIT_CONFIG": os.path.join(self.tmp, "no-config.json"),
               "HOME": self.home, "USERPROFILE": self.home,
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra)
        return env

    def run_recording(self, repo, selection):
        """Real canaries with run_canary wrapped: (problems, basenames run)."""
        config = os.path.join(self.tmp, "selection.json")
        if selection is not None:
            with open(config, "w") as handle:
                json.dump({"profiles": selection}, handle)
        ran = []
        real_run = lib.run_canary

        def recording(path, payload, timeout):
            ran.append(os.path.basename(path))
            return real_run(path, payload, timeout)
        with mock.patch.object(toolkit_config, "CONFIG_PATH", config), \
                mock.patch.object(lib, "run_canary", recording), \
                mock.patch.dict(os.environ, self.hermetic_env()):
            found = lib.canary_problems(repo, time.monotonic() + 20,
                                        home=self.home)
        return found, ran

    def test_real_guards_pass_their_canaries(self):
        # Builder selected, so the real model-guard canary runs; the recording
        # proves it did (a skipped canary also returns []).
        found, ran = self.run_recording(REAL_REPO, ["builder"])
        self.assertEqual(found, [])
        self.assertIn("claude-model-guard", ran)
        self.assertIn("claude-danger-guard", ran)

    def test_the_model_canary_is_not_run_without_builder(self):
        found, ran = self.run_recording(REAL_REPO, None)
        self.assertEqual(found, [])
        self.assertNotIn("claude-model-guard", ran)
        # Control: the core danger-guard canary still runs.
        self.assertIn("claude-danger-guard", ran)

    def test_a_disarmed_guard_is_reported(self):
        # model-guard here is FAKE_MODEL_GUARD (a fixture that denies a
        # no-model dispatch), so the only defect is danger-guard exiting 0.
        found = self.probes(self.make_canary_repo())
        self.assertEqual(found, [{"guard": "claude-danger-guard",
                                  "probe": "deny", "got": "allowed"}])

    def test_an_over_blocking_guard_is_reported(self):
        found = self.probes(self.make_canary_repo(
            danger_body="#!/bin/sh\nexit 2\n"))
        self.assertEqual(found, [{"guard": "claude-danger-guard",
                                  "probe": "allow", "got": "denied"}])

    def test_a_crashing_guard_is_unchecked_not_allowed(self):
        found = self.probes(self.make_canary_repo(
            danger_body="#!/bin/sh\necho '{\"systemMessage\": \"crashed\"}'\n"))
        self.assertEqual(found, [
            {"guard": "claude-danger-guard", "probe": "deny", "got": "unchecked"},
            {"guard": "claude-danger-guard", "probe": "allow", "got": "unchecked"},
        ])

    def test_a_hanging_guard_is_unchecked_within_budget(self):
        # `exec` so the kill lands on the sleeper itself, not a shell whose
        # child would keep the pipe open.
        repo = self.make_canary_repo(danger_body="#!/bin/sh\nexec sleep 30\n")
        started = time.monotonic()
        found = lib.canary_problems(repo, time.monotonic() + 2)
        elapsed = time.monotonic() - started
        # 5 s is a catastrophe bound (a hang would be ~30 s), not a budget.
        self.assertLess(elapsed, 5)
        danger = [r for r in found if r["guard"] == "claude-danger-guard"]
        self.assertEqual(len(danger), 2)
        self.assertTrue(all(r["got"] == "unchecked" for r in danger), found)

    def test_an_exhausted_deadline_runs_nothing(self):
        marker = os.path.join(self.tmp, "ran")
        body = "#!/bin/sh\necho ran >> %s\nexit 0\n" % marker
        repo = self.make_canary_repo(danger_body=body, model_body=body)
        found = lib.canary_problems(repo, time.monotonic() - 1)
        self.assertEqual(len(found), 4)
        self.assertTrue(all(r["got"] == "unchecked" for r in found), found)
        self.assertFalse(os.path.exists(marker), "a guard ran past the deadline")

    def test_canaries_write_no_files(self):
        quiet_home = os.path.join(self.tmp, "quiet-home")
        os.makedirs(quiet_home)
        with mock.patch.dict(os.environ, self.hermetic_env(
                HOME=quiet_home, USERPROFILE=quiet_home)):
            self.assertEqual(self.probes(REAL_REPO), [])
        self.assertEqual(os.listdir(quiet_home), [])

    def test_model_canary_ignores_ambient_definitions(self):
        agents = os.path.join(self.home, ".claude", "agents")
        os.makedirs(agents, exist_ok=True)
        with open(os.path.join(agents, "general-purpose.md"), "w") as handle:
            handle.write("---\nname: general-purpose\nmodel: sonnet\n---\nbody\n")
        path = hook_manifest.resolve(REAL_REPO, MODEL, "builder")
        _, deny_payload, _, _ = [c for c in lib.CANARIES if c[0] == MODEL][0]
        control = dict(deny_payload, cwd=self.home)
        control["tool_input"] = dict(deny_payload["tool_input"],
                                     subagent_type="general-purpose")
        with mock.patch.dict(os.environ, self.hermetic_env()):
            # Positive control: the sandbox definition IS read.
            self.assertEqual(lib.run_canary(path, control, 5), "allowed")
            self.assertEqual(lib.run_canary(
                path, dict(deny_payload, cwd=self.home), 5), "denied")

    def test_escape_hatches_do_not_disarm_the_canary(self):
        with mock.patch.dict(os.environ, self.hermetic_env(
                MODEL_GUARD_ALLOW="1", DANGER_ALLOW="1")):
            self.assertEqual(self.probes(REAL_REPO), [])

    def test_verdict_parsing(self):
        table = (
            ("exit 2", "denied"),
            ("echo '%s'" % DENY_JSON, "denied"),
            ("exit 0", "allowed"),
            ("echo '{}'", "allowed"),
            ("echo '{\"systemMessage\": \"x\"}'", "unchecked"),
            ("exit 1", "unchecked"),
            ("echo 'not json'", "unchecked"),
        )
        for body, expected in table:
            with self.subTest(body=body):
                path = os.path.join(self.tmp, "verdict")
                with open(path, "w") as handle:
                    handle.write("#!/bin/sh\n%s\n" % body)
                os.chmod(path, 0o755)
                self.assertEqual(lib.run_canary(path, {}, 5), expected)

    # --- through the real hook ---------------------------------------------

    def test_session_start_reports_a_disarmed_guard(self):
        repo = self.make_canary_repo()
        out = self.run_hook(repo=repo, event="SessionStart")
        self.assertIn(CANARY_HEADER, out)
        self.assertIn("claude-danger-guard", out)
        self.assertIn("/reload-plugins", out)
        self.assertNotIn("Fix: run ./install.sh", out)

    def test_user_prompt_submit_runs_no_canaries(self):
        marker = os.path.join(self.tmp, "ran")
        repo = self.make_canary_repo(
            danger_body="#!/bin/sh\necho ran >> %s\nexit 0\n" % marker)
        out = self.run_hook(repo=repo, event="UserPromptSubmit")
        self.assertNotIn(CANARY_HEADER, out)
        self.assertFalse(os.path.exists(marker))
        # Positive control: the same fixture IS executed on SessionStart.
        self.run_hook(session="s2", repo=repo, event="SessionStart")
        self.assertTrue(os.path.exists(marker))

    def test_canary_notice_is_deduped_per_session(self):
        repo = self.make_canary_repo()
        first = self.run_hook(repo=repo, event="SessionStart")
        second = self.run_hook(repo=repo, event="SessionStart")
        self.assertEqual(first.count(CANARY_HEADER), 1)
        self.assertEqual(second.count(CANARY_HEADER), 0)

    def test_session_start_on_the_real_repo_is_silent(self):
        out = self.run_hook(event="SessionStart", extra_env={
            "HOME": self.home, "USERPROFILE": self.home,
            "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertNotIn(CANARY_HEADER, out)
        self.assertNotIn("Guards the plugin no longer runs", out)

    def test_session_start_on_the_real_repo_is_silent_with_builder_selected(self):
        """The no-config run above leaves builder unselected, so its model-guard
        half never runs. This one selects builder, and the controls prove the
        selection reaches the hook's model canary."""
        self.requires_ps()
        config = os.path.join(self.tmp, "builder-selected.json")
        with open(config, "w") as handle:
            json.dump({"profiles": ["continuity", "builder"]}, handle)
        # A live owner: with a sandbox HOME session-claim's canary otherwise
        # cannot identify the session and the hook would report it.
        env = {"CLAUDE_TOOLKIT_CONFIG": config,
               "CLAIM_TEST_OWNER_PID": str(
                   self.owner_pid()),
               "HOME": self.home, "USERPROFILE": self.home,
               "PYTHONDONTWRITEBYTECODE": "1"}
        out = self.run_hook(event="SessionStart", extra_env=env)
        self.assertNotIn(CANARY_HEADER, out)
        self.assertNotIn("Guards the plugin no longer runs", out)
        # Positive control: through the hook, this config runs the model canary,
        # so a disarmed model guard is reported...
        repo = self.make_canary_repo(model_body="#!/bin/sh\nexit 0\n")
        disarmed = self.run_hook(session="s2", repo=repo, event="SessionStart",
                                 extra_env=env)
        self.assertIn("claude-model-guard", disarmed)
        # ...and without builder the same fixture never mentions it.
        unselected = self.run_hook(session="s3", repo=repo, event="SessionStart",
                                   extra_env={"HOME": self.home, "USERPROFILE": self.home,
                                              "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertNotIn("claude-model-guard", unselected)

    def test_a_guard_disabled_in_config_is_skipped_not_reported(self):
        self.requires_ps()
        config = os.path.join(self.tmp, "model-guard-off.json")
        with open(config, "w") as handle:
            # builder selected, so the model canary WOULD run but for the switch
            json.dump({"model_guard": {"enabled": False},
                       "profiles": ["builder"]}, handle)
        out = self.run_hook(event="SessionStart", extra_env={
            "CLAUDE_TOOLKIT_CONFIG": config,
            "CLAIM_TEST_OWNER_PID": str(self.owner_pid()),
            "HOME": self.home, "USERPROFILE": self.home,
            "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertNotIn("claude-model-guard", out)
        # Positive control: the config really does disable the guard.
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config)
        path = hook_manifest.resolve(REAL_REPO, MODEL, "builder")
        _, deny_payload, _, _ = [c for c in lib.CANARIES if c[0] == MODEL][0]
        with mock.patch.dict(os.environ, env):
            self.assertEqual(lib.run_canary(
                path, dict(deny_payload, cwd=self.home), 5), "allowed")


class TestStatefulCanaries(Sandbox):
    """Canaries for the three stateful guards, run on a throwaway fixture."""

    WORKTREE = "tools/worktree-guard/claude-worktree-guard"
    DISTILL = "tools/distill-guard/claude-distill-write-guard"
    CLAIM = "tools/session-claim/claude-session-claim"
    CLAIM_NAME = "claude-session-claim"
    NAMES = ("claude-worktree-guard", "claude-distill-write-guard",
             CLAIM_NAME)
    LOADED_CAP = 30  # canary cap raised so machine load cannot trip the 2s default

    def setUp(self):
        super().setUp()
        self.tmp_parent = os.path.join(self.tmp, "parent")
        os.makedirs(self.tmp_parent)

    # --- helpers -----------------------------------------------------------

    def scenario(self, repo=REAL_REPO, owner=True, config=None, parent=None,
                 deadline=120, env=None, patches=()):
        """Real stateful canaries with run_canary wrapped: (problems, ran).

        `config` is the toolkit config written to a file (default: builder
        selected); `patches` are extra context managers entered around the run.
        """
        path = os.path.join(self.tmp, "stateful-selection.json")
        with open(path, "w") as handle:
            json.dump({"profiles": ["builder"]} if config is None else config,
                      handle)
        environment = {"CLAUDE_TOOLKIT_CONFIG": path,
                       "HOME": self.home, "USERPROFILE": self.home,
                       "PYTHONDONTWRITEBYTECODE": "1",
                       "CLAIM_TEST_PS_TIMEOUT": "30"}
        if owner:
            environment["CLAIM_TEST_OWNER_PID"] = str(self.owner_pid())
        environment.update(env or {})
        ran = []
        real_run = lib.run_canary

        def recording(guard, payload, timeout, extra_env=None):
            ran.append(os.path.basename(guard))
            return real_run(guard, payload, timeout, extra_env=extra_env)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(toolkit_config,
                                                  "CONFIG_PATH", path))
            stack.enter_context(mock.patch.object(toolkit_config, "_cache",
                                                  None))
            stack.enter_context(mock.patch.object(lib, "run_canary", recording))
            stack.enter_context(mock.patch.object(
                lib, "CANARY_MAX_SECONDS", self.LOADED_CAP))
            stack.enter_context(mock.patch.dict(os.environ, environment))
            for patch in patches:
                stack.enter_context(patch)
            found = lib.stateful_canary_problems(
                repo, time.monotonic() + deadline, home=self.home,
                tmp_parent=parent or self.tmp_parent)
        return found, ran

    def set_guards(self, repo, bodies):
        """Overwrite stateful guards in a make_plugin fixture: relpath -> body."""
        for relpath, body in bodies.items():
            profile = "builder" if relpath == self.CLAIM else "core"
            path = hook_manifest.resolve(repo, relpath, profile)
            self.assertTrue(os.path.exists(path), path)
            with open(path, "w") as handle:
                handle.write(body)
            os.chmod(path, 0o755)

    def marker_guards(self, marker):
        body = "#!/bin/sh\necho ran >> %s\nexit 0\n" % marker
        return {self.WORKTREE: body, self.DISTILL: body, self.CLAIM: body}

    # --- the real guards ---------------------------------------------------

    def test_real_guards_pass_their_stateful_canaries(self):
        """Builder selected and an owner: all three real guards give the right
        verdict on both probes, and the recording proves all six ran."""
        self.requires_ps()
        found, ran = self.scenario()
        self.assertEqual(found, [])
        self.assertEqual(sorted(ran), sorted(self.NAMES * 2))

    def test_a_disarmed_stateful_guard_is_reported(self):
        """Three guards that always allow: one deny/allowed record each and no
        allow-probe record (the allow probe passes)."""
        repo = TestCoverage.make_plugin(self, builder=True)
        found, _ran = self.scenario(repo=repo)
        self.assertEqual(found, [
            {"guard": name, "probe": "deny", "got": "allowed"}
            for name in self.NAMES])

    def test_an_over_blocking_stateful_guard_is_reported(self):
        """A worktree-guard that always blocks fails its allow probe only."""
        repo = TestCoverage.make_plugin(self, builder=True)
        self.set_guards(repo, {self.WORKTREE: "#!/bin/sh\nexit 2\n"})
        found, _ran = self.scenario(repo=repo)
        mine = [r for r in found if r["guard"] == "claude-worktree-guard"]
        self.assertEqual(mine, [{"guard": "claude-worktree-guard",
                                 "probe": "allow", "got": "denied"}])

    def test_session_claim_without_identity_is_unidentified(self):
        """P6: with a sandbox HOME and no owner override the guard cannot tell
        who is calling and allows everything; the canary says so."""
        found, _ran = self.scenario(owner=False)
        self.assertEqual(len(found), 1, found)
        record = found[0]
        self.assertEqual(record["guard"], self.CLAIM_NAME)
        self.assertEqual(record["probe"], "deny")
        self.assertEqual(record["got"], "unidentified")
        if self.ps_works():
            self.assertEqual(record["detail"], "owner unresolved")

    def test_the_fixture_is_removed(self):
        """Nothing is left in tmp_parent after a full run or an abandoned one.
        Control: with rmtree disabled the directory IS left behind."""
        self.requires_ps()
        self.scenario()
        self.assertEqual(os.listdir(self.tmp_parent), [])
        found, _ran = self.scenario(deadline=-1)
        self.assertEqual(len(found), 6, found)
        self.assertEqual(os.listdir(self.tmp_parent), [])
        with mock.patch.object(shutil, "rmtree"):
            self.scenario()
        self.assertNotEqual(os.listdir(self.tmp_parent), [])

    def test_real_claim_state_is_never_written(self):
        """The canary's session-claim writes only into the fixture. Control: the
        same guard with CLAIM_LOG_PATH unset writes into the sandbox HOME."""
        self.requires_ps()
        self.scenario()
        claims_dir = os.path.join(self.home, ".claude", "session-claims")
        self.assertFalse(os.path.exists(claims_dir))
        repo = os.path.join(self.tmp, "control-repo")
        git("init", "-q", repo, cwd=self.tmp)
        guard = hook_manifest.resolve(REAL_REPO, self.CLAIM, "builder")
        payload = lib._canary_payload("Write", {
            "file_path": os.path.join(repo, "x.txt"), "content": "x"})
        environment = {"HOME": self.home, "USERPROFILE": self.home,
                       "CLAIM_TEST_OWNER_PID": str(self.owner_pid()),
                       "CLAIM_CLAIMS_PATH": os.path.join(self.tmp, "c.json")}
        with mock.patch.dict(os.environ, environment):
            os.environ.pop("CLAIM_LOG_PATH", None)
            lib.run_canary(guard, dict(payload, cwd=repo), 10)
        self.assertTrue(os.path.exists(os.path.join(claims_dir, "decisions.log")))

    def test_escape_hatches_do_not_disarm_the_stateful_canaries(self):
        """A session that exported the hatches is a visible choice, not damage."""
        self.requires_ps()
        found, _ran = self.scenario(env={"WORKTREE_ALLOW": "1",
                                         "WT_ALLOW_MAIN": "1",
                                         "CLAIM_ALLOW": "1"})
        self.assertEqual(found, [])

    def test_an_inherited_git_dir_does_not_leak_into_the_fixture(self):
        """GIT_DIR in the environment must not point the fixture's git calls
        (or the guards') at some other repository."""
        self.requires_ps()
        found, _ran = self.scenario(env={
            "GIT_DIR": os.path.join(self.tmp, "not-a-repo")})
        self.assertEqual(found, [])

    def test_a_spent_deadline_runs_no_stateful_guard(self):
        """No budget left: every probe is recorded unchecked, no guard runs."""
        marker = os.path.join(self.tmp, "ran")
        repo = TestCoverage.make_plugin(self, builder=True)
        self.set_guards(repo, self.marker_guards(marker))
        found, _ran = self.scenario(repo=repo, deadline=-1)
        self.assertEqual(len(found), 6, found)
        self.assertTrue(all(r["got"] == "unchecked" for r in found), found)
        self.assertFalse(os.path.exists(marker), "a guard ran past the deadline")

    def test_session_claim_disabled_in_config_is_skipped(self):
        """session_claim.enabled=false is a visible choice. Control: test 4 is
        the same scenario without the switch and reports unidentified."""
        found, _ran = self.scenario(owner=False, config={
            "profiles": ["builder"], "session_claim": {"enabled": False}})
        self.assertEqual(
            [r for r in found if r["guard"] == self.CLAIM_NAME], [])

    def test_session_claim_is_skipped_where_unsupported(self):
        """Where session-claim cannot identify a session by construction."""
        found, ran = self.scenario(owner=False, patches=(
            mock.patch.object(lib, "_session_claim_canary_supported",
                              return_value=False),))
        self.assertEqual(
            [r for r in found if r["guard"] == self.CLAIM_NAME], [])
        self.assertNotIn(self.CLAIM_NAME, ran)

    def test_no_fixture_without_a_stateful_guard_to_run(self):
        """Builder unselected and both core guards unresolvable: nothing to
        run, so no fixture is built at all."""
        real_resolve = hook_manifest.resolve
        core = (self.WORKTREE, self.DISTILL)

        def resolve(repo, relpath, profile):
            if relpath in core:
                raise hook_manifest.ManifestError("canary test")
            return real_resolve(repo, relpath, profile)
        made = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(*args, **kwargs):
            made.append(args)
            return real_mkdtemp(*args, **kwargs)
        found, ran = self.scenario(config={}, patches=(
            mock.patch.object(hook_manifest, "resolve", resolve),
            mock.patch.object(tempfile, "mkdtemp", mkdtemp)))
        self.assertEqual(found, [])
        self.assertEqual(ran, [])
        self.assertEqual(os.listdir(self.tmp_parent), [])
        self.assertEqual(made, [])

    def test_a_temp_parent_with_a_space_still_denies(self):
        """worktree-guard's deny command quotes every path in it."""
        self.requires_ps()
        spaced = os.path.join(self.tmp, "has space")
        os.makedirs(spaced)
        found, _ran = self.scenario(parent=spaced)
        self.assertEqual(found, [])

    # --- rendering and plumbing ---------------------------------------------

    def test_render_unidentified(self):
        """An unidentified record gets its own line and no checkout-repair
        advice; mixed with a real failure the advice returns."""
        record = {"guard": self.CLAIM_NAME, "probe": "deny",
                  "got": "unidentified", "detail": "owner unresolved"}
        out = lib.render_canaries([record])
        self.assertIn("could not tell which session", out)
        self.assertIn("owner unresolved", out)
        self.assertNotIn(lib._REPAIR_ADVICE, out)
        mixed = lib.render_canaries([record, {
            "guard": "claude-danger-guard", "probe": "deny", "got": "allowed"}])
        self.assertIn("could not tell which session", mixed)
        self.assertIn("claude-danger-guard let a harmless canary", mixed)
        self.assertIn(lib._REPAIR_ADVICE, mixed)

    def test_run_canary_extra_env_reaches_the_guard(self):
        """extra_env is passed to the guard; without it the guard allows."""
        path = os.path.join(self.tmp, "probe-env")
        with open(path, "w") as handle:
            handle.write('#!/bin/sh\n[ "$TOOLKIT_CANARY_PROBE" = yes ] && exit 2\n'
                         "exit 0\n")
        os.chmod(path, 0o755)
        self.assertEqual(lib.run_canary(
            path, {}, 5, extra_env={"TOOLKIT_CANARY_PROBE": "yes"}), "denied")
        self.assertEqual(lib.run_canary(path, {}, 5), "allowed")


    # --- through the real hook ----------------------------------------------

    def hook_env(self, owner=True, config=None):
        path = os.path.join(self.tmp, "hook-selection.json")
        with open(path, "w") as handle:
            json.dump({"profiles": ["builder"]} if config is None else config,
                      handle)
        env = {"CLAUDE_TOOLKIT_CONFIG": path,
               "HOME": self.home, "USERPROFILE": self.home,
               "PYTHONDONTWRITEBYTECODE": "1"}
        if owner:
            env["CLAIM_TEST_OWNER_PID"] = str(self.owner_pid())
        return env

    def test_session_start_reports_a_disarmed_stateful_guard(self):
        """SessionStart runs the stateful canaries: a fixture whose three
        stateful guards allow everything is reported for all three."""
        self.requires_ps()
        repo = TestCoverage.make_plugin(self, builder=True)
        out = self.run_hook(event="SessionStart", repo=repo,
                            extra_env=self.hook_env())
        self.assertIn(CANARY_HEADER, out)
        for name in self.NAMES:
            self.assertIn(name, out)

    # The hook-level "real repo with builder is silent" test was removed: it
    # ran the real canaries through a subprocess under the 2s cap, so load
    # made it flaky. Its assertions are carried in-process (with a raised cap)
    # by test_real_guards_pass_their_stateful_canaries and
    # test_session_claim_without_identity_is_unidentified; the hook running
    # the stateful canaries on SessionStart is proven by
    # test_session_start_reports_a_disarmed_stateful_guard.

    def test_user_prompt_submit_runs_no_stateful_canaries(self):
        """Stateful canaries are SessionStart only: UserPromptSubmit leaves no
        marker, SessionStart (control) does."""
        marker = os.path.join(self.tmp, "marker")
        repo = TestCoverage.make_plugin(self, builder=True)
        self.set_guards(repo, self.marker_guards(marker))
        env = self.hook_env()
        self.run_hook(event="UserPromptSubmit", repo=repo, extra_env=env)
        self.assertFalse(os.path.exists(marker))
        self.run_hook(session="s2", event="SessionStart", repo=repo,
                      extra_env=env)
        self.assertTrue(os.path.exists(marker))


class TestBothHalvesTogether(Sandbox):

    def test_a_broken_link_and_a_live_move_are_both_reported(self):
        repo = self.make_repo()
        _, link = self.make_tool("claude-danger-guard")
        self.hooks_settings(link)
        self.run_hook(root=repo)                    # baseline, all healthy
        self.break_link(link)
        self.commit_file(repo, "tools/a/x.py", "1\n", "one")
        out = self.run_hook(root=repo)
        self.assertIn("CANNOT RUN", out)
        self.assertIn("MOVED UNDER THIS SESSION", out)


# ================================================================ profiles

import toolkit_config  # noqa: E402  (lib/ is on sys.path above)

CORE_GUARD = ("core", "tools/c/claude-c", "PreToolUse", ("Bash",))
EXTRA_GUARD = ("extra", "tools/x/claude-x", "PreToolUse", ("Bash",))
EXTRA_ID = "extra@claude-code-toolkit"
CORE_ID = "toolkit-core@claude-code-toolkit"
PROFILES_WARNING = "profile selection could not be read"


class ProfileFixture(Sandbox):
    """A two-profile fixture repo: `core` in plugin/, `extra` in plugin-extra/."""

    def setUp(self):
        super().setUp()
        self.config = os.path.join(self.tmp, "config.json")  # absent by default
        patcher = mock.patch.object(toolkit_config, "CONFIG_PATH", self.config)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_config(self, text):
        with open(self.config, "w") as handle:
            handle.write(text)

    def enable(self, *plugin_ids):
        self.write_settings({"enabledPlugins": {i: True for i in plugin_ids}})

    def make_two_profile_repo(self, extra_registers=False, core_files=(),
                              extra_files=()):
        """`core` registers tools/c/claude-c; `extra` registers tools/x/claude-x
        only when `extra_registers`. `core_files`/`extra_files` are the relpaths
        that exist as executables under plugin/ and plugin-extra/."""
        repo = os.path.join(self.tmp, "twoprofile")
        os.makedirs(os.path.join(repo, "claude"), exist_ok=True)
        entry = {"dir": "plugin", "default": True, "requires": [],
                 "agents": False, "summary": "s"}
        with open(os.path.join(repo, "claude", "profiles.json"), "w") as handle:
            json.dump({"profiles": {
                "core": dict(entry, plugin="toolkit-core"),
                "extra": dict(entry, plugin="extra", dir="plugin-extra",
                              default=False)}}, handle)
        for directory, files, relpath in (("plugin", core_files, "tools/c/claude-c"),
                                          ("plugin-extra", extra_files, "tools/x/claude-x")):
            os.makedirs(os.path.join(repo, directory, "hooks"), exist_ok=True)
            registers = directory == "plugin" or extra_registers
            blocks = ([{"matcher": "Bash", "hooks": [{
                "type": "command",
                "command": '"${CLAUDE_PLUGIN_ROOT}"/' + relpath}]}]
                if registers else [])
            with open(os.path.join(repo, directory, "hooks", "hooks.json"), "w") as handle:
                json.dump({"hooks": {"PreToolUse": blocks}}, handle)
            for name in files:
                path = os.path.join(repo, directory, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w") as handle:
                    handle.write("#!/bin/sh\nexit 0\n")
                os.chmod(path, 0o755)
        return repo

    def patched_guards(self):
        return mock.patch.object(lib, "REQUIRED_GUARDS", (CORE_GUARD, EXTRA_GUARD))


class TestProfileCoverage(ProfileFixture):

    def guards(self, repo):
        with self.patched_guards():
            return lib.coverage_problems(repo, self.home)

    def test_an_unselected_unenabled_profile_is_not_required(self):
        self.enable(CORE_ID)
        self.assertEqual(self.guards(self.make_two_profile_repo()), [])

    def test_a_selected_profile_is_required(self):
        self.enable(CORE_ID)
        self.write_config('{"profiles": ["extra"]}')
        found = self.guards(self.make_two_profile_repo())
        self.assertEqual([r["guard"] for r in found], ["tools/x/claude-x"])
        self.assertEqual(found[0]["why"], "unregistered")

    def test_an_enabled_but_unselected_profile_is_required(self):
        self.enable(CORE_ID, EXTRA_ID)
        found = self.guards(self.make_two_profile_repo())
        self.assertEqual([r["guard"] for r in found], ["tools/x/claude-x"])

    def test_a_registered_required_profile_is_clean(self):
        self.enable(CORE_ID, EXTRA_ID)
        self.assertEqual(self.guards(
            self.make_two_profile_repo(extra_registers=True)), [])

    def test_a_truncated_config_requires_everything_and_says_so(self):
        self.enable(CORE_ID)
        self.write_config('{"profiles": [')
        repo = self.make_two_profile_repo()
        found = self.guards(repo)
        self.assertEqual([r["guard"] for r in found], ["tools/x/claude-x"])
        names, problems = lib.required_profiles(repo, self.home)
        self.assertIsNone(names)
        self.assertEqual(len(problems), 1)
        self.assertIn("config", problems[0]["why"])

    def test_a_truncated_config_checks_a_guard_only_against_its_own_profile(self):
        # `extra`'s guard is registered ONLY in core's manifest; with the
        # selection unreadable it must still be reported, not satisfied by
        # another profile's registration.
        self.enable(CORE_ID)
        self.write_config('{"profiles": [')
        repo = self.make_two_profile_repo()
        extra_guard = ("extra", "tools/x/claude-x", "PreToolUse", ("Bash",))
        core_reg = {"profile": "core", "relpath": "tools/x/claude-x",
                    "event": "PreToolUse", "matcher": "Bash"}
        with mock.patch.object(lib, "REQUIRED_GUARDS", [extra_guard]), \
                mock.patch.object(lib._manifest, "plugin_registrations",
                                  return_value=[core_reg]):
            found = lib.coverage_problems(repo, self.home)
        self.assertEqual([r["guard"] for r in found], ["tools/x/claude-x"])

    def test_an_unreadable_settings_file_requires_everything_and_says_so(self):
        with open(self.settings, "w") as handle:
            handle.write("{not json")
        repo = self.make_two_profile_repo()
        self.assertEqual([r["guard"] for r in self.guards(repo)],
                         ["tools/x/claude-x"])
        problems = lib.integrity_problems(self.home, repo=repo)["profiles"]
        self.assertEqual(len(problems), 1)
        self.assertIn("settings.json", problems[0]["why"])

    def test_a_missing_settings_file_is_nothing_enabled_not_a_problem(self):
        # A fresh machine has no settings.json until install.sh writes one;
        # that is "nothing enabled", never an unreadable-settings warning.
        os.unlink(self.settings)
        repo = self.make_two_profile_repo()
        names, problems = lib.required_profiles(repo, self.home)
        self.assertEqual(names, {"core"})
        self.assertEqual(problems, [])
        self.assertEqual(self.guards(repo), [])

    def test_a_healthy_selection_has_no_profile_problems(self):
        self.enable(CORE_ID)
        repo = self.make_two_profile_repo()
        self.assertEqual(lib.integrity_problems(self.home, repo=repo)["profiles"], [])
        names, problems = lib.required_profiles(repo, self.home)
        self.assertEqual((names, problems), ({"core"}, []))

    def test_a_profile_problem_changes_the_signature_and_renders(self):
        repo = self.make_two_profile_repo(core_files=("tools/c/claude-c",))
        self.enable(CORE_ID)
        with self.patched_guards():
            healthy = lib.integrity_problems(self.home, repo=repo)
            self.write_config("{")
            broken = lib.integrity_problems(self.home, repo=repo)
        self.assertNotEqual(lib.problems_signature(healthy),
                            lib.problems_signature(broken))
        self.assertEqual(lib.render_integrity(healthy, self.home), "")
        text = lib.render_integrity({"hooks": [], "links": [], "coverage": [],
                                     "profiles": broken["profiles"]}, self.home)
        self.assertIn(PROFILES_WARNING, text)
        self.assertIn("every guard", text)
        self.assertIn(broken["profiles"][0]["why"], text)


    def test_a_profiles_problem_alone_moves_the_signature(self):
        """Isolated from coverage, which a profile problem also tends to move."""
        bare = {"hooks": [], "links": [], "coverage": []}
        one = dict(bare, profiles=[{"why": "config file x"}])
        two = dict(bare, profiles=[{"why": "config file y"}])
        self.assertEqual(lib.problems_signature(bare), "")
        self.assertNotEqual(lib.problems_signature(one), "")
        self.assertNotEqual(lib.problems_signature(one),
                            lib.problems_signature(two))


class TestProfileCanaries(ProfileFixture):

    ROWS = (("tools/c/claude-c", {"p": "deny"}, {"p": "allow"}, None),
            ("tools/x/claude-x", {"p": "deny"}, {"p": "allow"}, None))

    def run_canaries(self, repo):
        ran = []

        def fake(path, payload, timeout):
            ran.append(os.path.basename(path))
            return "denied" if payload["p"] == "deny" else "allowed"
        with self.patched_guards(), \
                mock.patch.object(lib, "CANARIES", self.ROWS), \
                mock.patch.object(lib, "run_canary", fake), \
                mock.patch.dict(os.environ, {"HOME": self.home,
                                             "USERPROFILE": self.home}):
            lib.canary_problems(repo, time.monotonic() + 5, home=self.home)
        return sorted(set(ran))

    def repo(self):
        return self.make_two_profile_repo(
            extra_registers=True, core_files=("tools/c/claude-c",),
            extra_files=("tools/x/claude-x",))

    def test_a_canary_for_an_unrequired_profile_is_skipped(self):
        self.enable(CORE_ID)
        self.assertEqual(self.run_canaries(self.repo()), ["claude-c"])

    def test_a_canary_for_a_required_profile_runs(self):
        self.enable(CORE_ID, EXTRA_ID)
        self.assertEqual(self.run_canaries(self.repo()), ["claude-c", "claude-x"])

    def test_a_selection_error_runs_every_canary(self):
        self.enable(CORE_ID)
        self.write_config("{")
        self.assertEqual(self.run_canaries(self.repo()), ["claude-c", "claude-x"])


class TestProfileResolution(ProfileFixture):

    def problems(self, repo):
        return lib.unrunnable_hooks(self.home, repo=repo)

    def test_an_extra_row_is_checked_under_its_own_plugin_dir(self):
        repo = self.make_two_profile_repo(
            extra_registers=True, core_files=("tools/c/claude-c",),
            extra_files=())
        # The executable exists under plugin/ (the wrong dir) but not plugin-extra/.
        wrong = os.path.join(repo, "plugin", "tools", "x", "claude-x")
        os.makedirs(os.path.dirname(wrong))
        with open(wrong, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(wrong, 0o755)
        found = self.problems(repo)
        self.assertEqual([(r["where"], r["why"]) for r in found],
                         [("plugin-extra:PreToolUse", "missing")])
        self.assertTrue(found[0]["path"].endswith("plugin-extra/tools/x/claude-x"))

    def test_an_extra_row_present_in_its_own_dir_is_clean(self):
        repo = self.make_two_profile_repo(
            extra_registers=True, core_files=("tools/c/claude-c",),
            extra_files=("tools/x/claude-x",))
        self.assertEqual(self.problems(repo), [])

    def test_core_rows_keep_the_plain_where(self):
        repo = self.make_two_profile_repo()
        self.assertEqual([r["where"] for r in self.problems(repo)],
                         ["plugin:PreToolUse"])


class TestProfileLiveChanges(Sandbox):

    def test_every_plugin_manifest_and_the_registry_are_live(self):
        pairs = [("M", "plugin-build/hooks/hooks.json"),
                 ("M", "claude/profiles.json"), ("M", "plugin/hooks/hooks.json"),
                 ("M", "docs/x.md")]
        self.assertEqual(lib.live_changes(pairs), pairs[:3])

    def test_a_lookalike_manifest_is_not_live(self):
        self.assertEqual(lib.live_changes([
            ("M", "plugin-Build/hooks/hooks.json"),
            ("M", "plugin-build/hooks/hooks.json.bak")]), [])

    def test_the_reload_advice_names_each_manifest(self):
        text = lib.render_drift("a" * 40, "b" * 40, [
            ("M", "plugin-build/hooks/hooks.json"),
            ("M", "claude/profiles.json"), ("M", "plugin/hooks/hooks.json")])
        for path in ("plugin-build/hooks/hooks.json", "claude/profiles.json",
                     "plugin/hooks/hooks.json"):
            self.assertIn("%s changed: run /reload-plugins" % path, text)

    def test_other_changes_get_no_reload_advice(self):
        text = lib.render_drift("a" * 40, "b" * 40, [("M", "tools/a/x.py")])
        self.assertNotIn("/reload-plugins changed", text)
        self.assertNotIn("changed: run /reload-plugins", text)


class TestRegistryDirsAreLive(Sandbox):
    """Every dir the registry accepts must be one LIVE_MANIFEST_RE watches."""

    def test_every_accepted_dir_has_a_live_manifest(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "lib"))
        import profiles
        for name in ("extra", "build", "continuity", "a", "zzz"):
            directory = "plugin-" + name
            repo = os.path.join(self.tmp, "reg-" + name)
            os.makedirs(os.path.join(repo, "claude"))
            fields = {"default": False, "requires": [], "agents": False,
                      "summary": "s"}
            with open(os.path.join(repo, "claude", "profiles.json"), "w") as handle:
                json.dump({"profiles": {
                    "core": dict(fields, plugin="toolkit-core", dir="plugin",
                                 default=True),
                    name: dict(fields, plugin=name, dir=directory)}}, handle)
            self.assertEqual(profiles.load_registry(repo)[name]["dir"], directory)
            self.assertTrue(lib.LIVE_MANIFEST_RE.match(
                directory + "/hooks/hooks.json"), directory)
        self.assertTrue(lib.LIVE_MANIFEST_RE.match("plugin/hooks/hooks.json"))


class TestProfilesThroughTheExecutable(ProfileFixture):
    """The executable swallows exceptions and always exits 0, so only its
    stdout proves anything. Run in place against this checkout (which has the
    real registry, lib/ and the sibling modules beside it)."""

    def hook_env(self, **extra):
        env = {"HOME": self.home, "USERPROFILE": self.home,
               "PYTHONDONTWRITEBYTECODE": "1"}
        env.update(extra)
        return env

    def test_a_truncated_config_prints_the_profiles_warning(self):
        self.enable(CORE_ID)
        self.write_config('{"profiles": [')
        out = self.run_hook(event="UserPromptSubmit", extra_env=self.hook_env(
            CLAUDE_TOOLKIT_CONFIG=self.config))
        self.assertIn(PROFILES_WARNING, out)

    def test_a_healthy_machine_prints_no_profiles_warning(self):
        self.enable(CORE_ID)
        out = self.run_hook(event="UserPromptSubmit", extra_env=self.hook_env(
            CLAUDE_TOOLKIT_CONFIG=self.config))   # no file: the fresh-clone case
        self.assertNotIn(PROFILES_WARNING, out)
        self.assertNotIn("TOOLKIT INTEGRITY", out)

    def session_start(self, **env):
        return self.run_hook(event="SessionStart", extra_env=self.hook_env(**env))

    def test_session_start_truncated_config_prints_the_profiles_warning(self):
        self.enable(CORE_ID)
        self.write_config('{"profiles": [')
        out = self.session_start(CLAUDE_TOOLKIT_CONFIG=self.config)
        self.assertIn(PROFILES_WARNING, out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("crashed", out)

    def test_session_start_healthy_machine_prints_no_profiles_warning(self):
        self.enable(CORE_ID)
        out = self.session_start(CLAUDE_TOOLKIT_CONFIG=self.config)
        self.assertNotIn(PROFILES_WARNING, out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("crashed", out)
        self.assertTrue(os.path.exists(self.state))   # reached save_state

    def test_session_start_healthy_config_with_sandbox_home(self):
        self.enable(CORE_ID)
        self.write_config('{"profiles": ["core"]}')
        out = self.session_start(CLAUDE_TOOLKIT_CONFIG=self.config,
                                 TOOLKIT_INTEGRITY_HOME=self.home)
        self.assertNotIn(PROFILES_WARNING, out)
        self.assertNotIn("Traceback", out)
        self.assertNotIn("crashed", out)
        self.assertTrue(os.path.exists(self.state))   # reached save_state


# ================================================================ hook-fires audit

class TestFiresAudit(Sandbox):
    """SessionStart audit: earlier sessions that ran shell commands with no
    `.fired` heartbeat record, judged from their transcripts."""

    def setUp(self):
        super().setUp()
        self.cfg = os.path.join(self.tmp, "cfg")
        self.audit_state = os.path.join(
            self.home, ".claude", "claude-code-toolkit", "heartbeat-audit.json")
        self.write_era()

    def write_era(self):
        when = time.time() - 3 * 86400
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
        install_record.finish(install_record.record_path(self.home),
                              "/checkout", "abc123", ["core"], stamp)

    def transcript(self, sid, project="proj-a", tools=(("Bash", False, "ok"),)):
        """A real-shape transcript, started a day ago. Returns its path."""
        when = time.time() - 86400
        start = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(when))
        folder = os.path.join(self.cfg, "projects", project)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".jsonl")
        lines = [{"type": "user", "timestamp": start,
                  "message": {"role": "user", "content": "hello"}}]
        for index, (name, is_error, text) in enumerate(tools):
            tool_id = "toolu_%s_%d" % (sid, index)
            lines.append({"type": "assistant", "timestamp": start,
                          "message": {"role": "assistant", "content": [
                              {"type": "tool_use", "id": tool_id, "name": name,
                               "input": {"command": "ls"}}]}})
            lines.append({"type": "user", "timestamp": start,
                          "message": {"role": "user", "content": [
                              {"type": "tool_result", "tool_use_id": tool_id,
                               "is_error": is_error, "content": text}]}})
        with open(path, "w") as handle:
            handle.write("".join(json.dumps(line) + "\n" for line in lines))
        os.utime(path, (when, when))
        return path

    def fired(self, sid):
        folder = os.path.join(self.home, ".claude", "claude-code-toolkit", "heartbeat")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, sid + ".fired"), "w") as handle:
            handle.write("fired_at=1\n")

    def start(self, session, extra_env=None, event="SessionStart", **kwargs):
        env = {"CLAUDE_CONFIG_DIR": self.cfg}
        env.update(extra_env or {})
        return self.run_hook(session=session, event=event, extra_env=env, **kwargs)

    def can_deny(self):
        return os.name != "nt" and os.geteuid() != 0

    def test_1_unfired_session_is_named_once(self):
        self.transcript("aaaaaaaa-1111-2222-3333-444444444444")
        first = self.start("now-1")
        self.assertIn("did not fire", first)
        self.assertIn("aaaaaaaa", first)
        self.assertIn("1 shell command(s) ran", first)
        # the route the public /onboard skill ships, plugin-qualified
        self.assertIn("Run /toolkit-core:onboard verify in a fresh session", first)
        second = self.start("now-2")
        self.assertNotIn("aaaaaaaa", second)
        self.assertNotIn("did not fire", second)

    def test_1a_the_verdict_cache_is_where_the_writers_registry_says(self):
        """The audit's cache is the one file this hook writes outside its own
        state, and the profile registry tells the user where it is."""
        self.transcript("acacacac-1111-2222-3333-444444444444")
        self.start("now-1")
        self.assertTrue(os.path.isfile(self.audit_state), self.audit_state)
        repo = os.path.dirname(os.path.dirname(TOOL_DIR))
        with open(os.path.join(repo, "claude", "profiles.json")) as handle:
            writers = json.load(handle)["writers"]
        documented = writers["tools/toolkit-integrity/claude-toolkit-integrity"]
        self.assertIn("~" + self.audit_state[len(self.home):], documented)

    def test_1b_a_reported_session_that_grows_is_not_named_again(self):
        """The verdict cache alone would re-judge a transcript whose size changed;
        `reported` is what keeps a named session from being named twice."""
        path = self.transcript("abababab-1111-2222-3333-444444444444")
        self.assertIn("abababab", self.start("now-1"))
        stamp = os.stat(path).st_mtime
        with open(path, "a") as handle:
            handle.write(json.dumps({"type": "user", "message": {"content": "more"}}) + "\n")
        os.utime(path, (stamp + 60, stamp + 60))
        self.assertNotIn("abababab", self.start("now-2"))

    def test_2_positive_control_fired_session_is_quiet(self):
        sid = "bbbbbbbb-1111-2222-3333-444444444444"
        self.transcript(sid)
        self.fired(sid)
        out = self.start("now-1")
        self.assertNotIn("did not fire", out)
        self.assertNotIn("bbbbbbbb", out)

    def test_3_user_prompt_submit_does_not_audit(self):
        self.transcript("cccccccc-1111-2222-3333-444444444444")
        out = self.start("now-1", event="UserPromptSubmit")
        self.assertNotIn("did not fire", out)
        self.assertFalse(os.path.exists(self.audit_state))

    def test_4_current_session_is_not_reported(self):
        sid = "dddddddd-1111-2222-3333-444444444444"
        self.transcript(sid)
        out = self.start(sid)
        self.assertNotIn("did not fire", out)
        self.assertNotIn("dddddddd", out)

    def test_5_unreadable_transcript_is_reported_once(self):
        if not self.can_deny():
            self.skipTest("chmod cannot deny reads here")
        path = self.transcript("eeeeeeee-1111-2222-3333-444444444444")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        first = self.start("now-1")
        self.assertIn("could not be read", first)
        self.assertIn("eeeeeeee", first)
        self.assertNotIn("did not fire", first)  # unreadable is not evidence of an outage
        self.assertNotIn("could not be read", self.start("now-2"))

    def test_6_out_of_time_session_is_reported_on_the_third_strike(self):
        self.transcript("ffffffff-1111-2222-3333-444444444444")
        slow = {"TOOLKIT_INTEGRITY_AUDIT_SECONDS": "0"}
        elsewhere = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, elsewhere, ignore_errors=True)
        outputs = [self.start("now-%d" % n, extra_env=slow, repo=self.real,
                              cwd=elsewhere) for n in (1, 2, 3, 4)]
        self.assertNotIn("ffffffff", outputs[0])
        self.assertNotIn("ffffffff", outputs[1])
        self.assertIn("ffffffff", outputs[2])
        self.assertIn("could not be checked in 3 tries", outputs[2])
        self.assertIn(self.real + "/install.sh --check", outputs[2])
        self.assertNotIn("ffffffff", outputs[3])

    def test_6b_only_the_interrupted_session_takes_a_strike(self):
        """Sessions queued behind the one the deadline interrupted never ran out of
        time themselves, so they must not strike out for waiting."""
        self.transcript("11111111-1111-2222-3333-444444444444")
        self.transcript("22222222-1111-2222-3333-444444444444",
                        tools=(("Bash", False, "ok"),) * 5)  # larger: queued second
        slow = {"TOOLKIT_INTEGRITY_AUDIT_SECONDS": "0"}
        outputs = [self.start("now-%d" % n, extra_env=slow) for n in (1, 2, 3, 4)]
        self.assertIn("11111111", outputs[2])
        self.assertTrue(all("22222222" not in out for out in outputs))

    def test_7_audit_crash_is_announced(self):
        out = self.start("now-1", extra_env={"TOOLKIT_INTEGRITY_AUDIT_CRASH": "1"})
        self.assertIn("audit crashed (RuntimeError)", out)

    def test_8_no_install_record_no_audit(self):
        os.unlink(install_record.record_path(self.home))
        self.transcript("99999999-1111-2222-3333-444444444444")
        out = self.start("now-1")
        self.assertNotIn("did not fire", out)
        self.assertFalse(os.path.exists(self.audit_state))


if __name__ == "__main__":
    unittest.main(verbosity=2)
