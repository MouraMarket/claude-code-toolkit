#!/usr/bin/env python3
"""Tests for the interactive setup's validation and detection logic.

The prompting loop itself is thin; what matters is that a bad answer is caught
while the person who typed it is still looking at the screen. These cover the
refusals in particular — a workspace root of $HOME, or one inside a cloud-synced
folder, would quietly defeat the danger guard.
"""

import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import claude_imports
import setup_config

HOME = os.path.expanduser("~")
LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(LIB)


class TestValidateWorkspaceRoot(unittest.TestCase):

    def test_accepts_an_existing_directory(self):
        path = tempfile.mkdtemp()
        ok, result = setup_config.validate_workspace_root(path)
        self.assertTrue(ok)
        self.assertEqual(result, os.path.realpath(path))

    def test_expands_tilde(self):
        ok, result = setup_config.validate_workspace_root("~")
        # ~ IS home, so this must be refused — but for the right reason.
        self.assertFalse(ok)
        self.assertIn("home directory", result)

    def test_refuses_home_itself(self):
        ok, result = setup_config.validate_workspace_root(HOME)
        self.assertFalse(ok)
        self.assertIn("home directory", result)

    def test_refuses_filesystem_root(self):
        ok, result = setup_config.validate_workspace_root("/")
        self.assertFalse(ok)
        self.assertIn("filesystem root", result)

    def test_refuses_nonexistent_directory(self):
        ok, result = setup_config.validate_workspace_root("/nope/not/here")
        self.assertFalse(ok)
        self.assertIn("no such directory", result)

    def test_refuses_empty(self):
        self.assertFalse(setup_config.validate_workspace_root("")[0])
        self.assertFalse(setup_config.validate_workspace_root(None)[0])

    def test_refuses_cloud_synced_paths(self):
        # The important one: a delete inside a synced folder propagates to every
        # other machine, so it must never be a trusted root.
        for path in ("~/Google Drive/My Drive/Work",
                     "~/Library/CloudStorage/GoogleDrive-x/Work",
                     "~/Dropbox/Work",
                     "~/OneDrive/Work"):
            ok, result = setup_config.validate_workspace_root(path)
            self.assertFalse(ok, "should refuse %s" % path)
            self.assertIn("cloud-synced", result)

    def test_refuses_a_file(self):
        fd, path = tempfile.mkstemp()
        os.close(fd)
        self.addCleanup(os.unlink, path)
        self.assertFalse(setup_config.validate_workspace_root(path)[0])


class TestGuessWorkspaceRoot(unittest.TestCase):
    """Run against a throwaway home: these tests used to create directories
    in the real one."""

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home, ignore_errors=True))
        self._real_home = setup_config.HOME
        setup_config.HOME = self.home
        self.addCleanup(setattr, setup_config, "HOME", self._real_home)

    def test_walks_up_past_a_container_directory(self):
        # A clone at <root>/repos/<name> implies <root> is the workspace,
        # when <root> holds other work too (row 73).
        root = tempfile.mkdtemp(dir=self.home)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        repo = os.path.join(root, "repos", "claude-code-toolkit")
        os.makedirs(repo)
        os.makedirs(os.path.join(root, "repos", "my-site"))
        self.assertEqual(setup_config.guess_workspace_root(repo), root)

    def test_uses_direct_parent_when_not_a_container(self):
        root = tempfile.mkdtemp(dir=self.home)
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        repo = os.path.join(root, "claude-code-toolkit")
        os.makedirs(repo)
        with open(os.path.join(root, "notes.md"), "w") as handle:
            handle.write("x")
        self.assertEqual(setup_config.guess_workspace_root(repo), root)

    # Row 73 (28b F-D1a): `~/My Tools`, holding nothing but the clone, was
    # offered as "(Recommended)" on the screen granting free deletes inside it.

    def _toolkit_only(self, *parts):
        root = os.path.join(self.home, *parts[:-1])
        repo = os.path.join(self.home, *parts)
        os.makedirs(repo)
        return root, repo

    def test_never_guesses_a_folder_holding_only_the_checkout(self):
        root, repo = self._toolkit_only("My Tools", "claude-code-toolkit")
        self.assertTrue(setup_config.holds_only_the_checkout(root, repo))
        self.assertIsNone(setup_config.guess_workspace_root(repo))

    def test_hidden_entries_do_not_make_it_a_work_folder(self):
        root, repo = self._toolkit_only("My Tools", "claude-code-toolkit")
        with open(os.path.join(root, ".DS_Store"), "w") as handle:
            handle.write("x")
        self.assertIsNone(setup_config.guess_workspace_root(repo))

    def test_never_guesses_a_toolkit_only_folder_through_a_container(self):
        _, repo = self._toolkit_only("Tools", "repos", "claude-code-toolkit")
        self.assertTrue(setup_config.holds_only_the_checkout(
            os.path.join(self.home, "Tools"), repo))
        self.assertIsNone(setup_config.guess_workspace_root(repo))

    def test_a_toolkit_only_fallback_candidate_is_skipped(self):
        _, repo = self._toolkit_only("Work", "claude-code-toolkit")
        os.makedirs(os.path.join(self.home, "Projects", "site"))
        self.assertEqual(setup_config.guess_workspace_root(repo),
                         os.path.join(self.home, "Projects"))

    def test_a_folder_with_other_work_beside_the_checkout_is_still_guessed(self):
        root, repo = self._toolkit_only("Work", "claude-code-toolkit")
        os.makedirs(os.path.join(root, "client-site"))
        self.assertFalse(setup_config.holds_only_the_checkout(root, repo))
        self.assertEqual(setup_config.guess_workspace_root(repo), root)

    def test_never_guesses_home_itself(self):
        # A clone straight into $HOME must not make $HOME the workspace root.
        self.assertNotEqual(
            setup_config.guess_workspace_root(os.path.join(self.home, "toolkit")),
            self.home)

    def test_returns_none_or_existing_dir_for_paths_outside_home(self):
        guess = setup_config.guess_workspace_root("/opt/somewhere/toolkit")
        if guess is not None:
            self.assertTrue(os.path.isdir(guess))
            self.assertNotEqual(os.path.realpath(guess), self.home)


class TestDeriveProtectedPaths(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_includes_immediate_children(self):
        os.makedirs(os.path.join(self.root, "repos"))
        os.makedirs(os.path.join(self.root, "scratch"))
        found = setup_config.derive_protected_paths(self.root)
        self.assertIn(os.path.join(self.root, "repos"), found)
        self.assertIn(os.path.join(self.root, "scratch"), found)

    def test_includes_git_repos_one_level_down(self):
        os.makedirs(os.path.join(self.root, "repos", "app-one", ".git"))
        os.makedirs(os.path.join(self.root, "repos", "app-two", ".git"))
        found = setup_config.derive_protected_paths(self.root)
        self.assertIn(os.path.join(self.root, "repos", "app-one"), found)
        self.assertIn(os.path.join(self.root, "repos", "app-two"), found)

    def test_excludes_non_git_grandchildren(self):
        # Ordinary subdirectories two levels down are work areas, not roots.
        os.makedirs(os.path.join(self.root, "repos", "just-a-folder"))
        found = setup_config.derive_protected_paths(self.root)
        self.assertNotIn(os.path.join(self.root, "repos", "just-a-folder"), found)

    def test_excludes_hidden_directories(self):
        os.makedirs(os.path.join(self.root, ".cache"))
        found = setup_config.derive_protected_paths(self.root)
        self.assertNotIn(os.path.join(self.root, ".cache"), found)

    def test_excludes_files(self):
        with open(os.path.join(self.root, "notes.md"), "w") as handle:
            handle.write("x")
        self.assertEqual(setup_config.derive_protected_paths(self.root), [])

    def test_unreadable_root_returns_empty(self):
        self.assertEqual(setup_config.derive_protected_paths("/nope/nowhere"), [])

    def test_empty_root_returns_empty(self):
        self.assertEqual(setup_config.derive_protected_paths(self.root), [])


class TestNonInteractiveWrite(unittest.TestCase):
    """The path Claude uses when it drives onboarding conversationally.

    It must apply the SAME validation as the prompts — otherwise onboarding could
    write a config that quietly defeats the danger guard.
    """

    SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "setup_config.py")
    REPO = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "config.json")
        self.workspace = os.path.join(self.dir, "Workspace")
        os.makedirs(os.path.join(self.workspace, "repos", "app", ".git"))
        os.makedirs(os.path.join(self.workspace, "scratch"))

    def run_write(self, *extra):
        import subprocess
        return subprocess.run(
            ["python3", self.SCRIPT, self.config, self.REPO, "--write"] + list(extra),
            capture_output=True, text=True)

    def test_writes_a_valid_config(self):
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            data = json.load(handle)
        self.assertEqual(data["owner_name"], "Sam")
        self.assertEqual(data["workspace_root"], os.path.realpath(self.workspace))

    def test_write_refuses_an_existing_config_untouched(self):
        with open(self.config, "wb") as handle:
            handle.write(b'{"owner_name": "Kept"}\n')
        before = os.stat(self.config).st_mtime_ns
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace)
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("--update", proc.stderr)
        with open(self.config, "rb") as handle:
            self.assertEqual(handle.read(), b'{"owner_name": "Kept"}\n')
        self.assertEqual(os.stat(self.config).st_mtime_ns, before)

    def test_an_existing_config_is_named_before_any_flag_is_validated(self):
        # The write-time exclusive create would refuse too, but only after
        # validation, so a bad flag would hide the real problem: the file exists.
        with open(self.config, "wb") as handle:
            handle.write(b'{"owner_name": "Kept"}\n')
        proc = self.run_write("--name", "Sam", "--workspace-root", "/")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("already exists", proc.stderr)
        self.assertNotIn("workspace_root rejected", proc.stderr)

    def test_derives_protected_paths(self):
        self.run_write("--name", "Sam", "--workspace-root", self.workspace)
        with open(self.config) as handle:
            protected = json.load(handle)["danger_guard"]["extra_protected_paths"]
        # Compare realpaths: the config stores resolved paths, and on macOS the
        # temp directory lives under /var, which is a symlink to /private/var.
        root = os.path.realpath(self.workspace)
        for name in (("repos",), ("repos", "app"), ("scratch",)):
            self.assertIn(os.path.join(root, *name), protected)

    def test_no_protected_flag_skips_derivation(self):
        self.run_write("--name", "Sam", "--workspace-root", self.workspace,
                       "--no-protected")
        with open(self.config) as handle:
            self.assertEqual(
                json.load(handle)["danger_guard"]["extra_protected_paths"], [])

    def _write_with_checkout(self, checkout, *extra):
        import subprocess
        os.makedirs(checkout, exist_ok=True)
        return subprocess.run(
            ["python3", self.SCRIPT, self.config, checkout, "--write", "--name", "Sam",
             "--workspace-root", self.workspace] + list(extra),
            capture_output=True, text=True)

    def _protected(self):
        with open(self.config) as handle:
            return [os.path.realpath(p) for p in
                    json.load(handle)["danger_guard"]["extra_protected_paths"]]

    def test_a_nested_checkout_is_protected_even_with_no_protected(self):
        """Row 73: a checkout deeper than derive_protected_paths reaches, with
        the derived list declined, is still an exact protected path."""
        checkout = os.path.join(self.workspace, "tools", "vendor", "claude-code-toolkit")
        proc = self._write_with_checkout(checkout, "--no-protected")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._protected(), [os.path.realpath(checkout)])

    def test_a_derived_checkout_is_listed_once(self):
        checkout = os.path.join(self.workspace, "repos", "claude-code-toolkit")
        os.makedirs(os.path.join(checkout, ".git"))
        proc = self._write_with_checkout(checkout)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self._protected().count(os.path.realpath(checkout)), 1)

    def test_a_checkout_outside_the_work_folder_is_not_added(self):
        self.assertEqual(setup_config.protect_the_checkout(
            [], self.workspace, os.path.join(self.dir, "elsewhere")), [])
        # A prefix-sharing sibling is not "under" the work folder.
        self.assertEqual(setup_config.protect_the_checkout(
            [], self.workspace, self.workspace + "-toolkit"), [])

    def test_the_danger_guard_refuses_deleting_the_written_checkout(self):
        """End to end, the property itself: with the config this writes, the real
        guard blocks `rm -rf <checkout>` and still allows a sibling beside it."""
        import subprocess
        checkout = os.path.join(self.workspace, "tools", "vendor", "claude-code-toolkit")
        sibling = os.path.join(self.workspace, "tools", "vendor", "scratch-copy")
        os.makedirs(sibling)
        proc = self._write_with_checkout(checkout, "--no-protected")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        guard = os.path.join(self.REPO, "tools", "danger-guard", "claude-danger-guard")
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=self.config)
        env.pop("DANGER_ALLOW", None)

        def verdict(target):
            payload = json.dumps({"tool_name": "Bash",
                                  "tool_input": {"command": "rm -rf %s" % target},
                                  "cwd": self.workspace})
            done = subprocess.run([guard], input=payload, capture_output=True,
                                  text=True, env=env)
            self.assertNotIn("claude-danger-guard crashed", done.stdout)
            return done.returncode

        self.assertEqual(verdict(sibling), 0, "fixture: the sibling should be routine")
        self.assertEqual(verdict(checkout), 2, "the checkout was deletable wholesale")

    def test_roster_flag_is_refused_and_writes_nothing(self):
        # The bundled project-code roster does not ship, so the flag that
        # pointed a config at it is gone; argparse refuses it (exit 2).
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace,
                              "--roster", "no")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(self.config))

    def test_writes_no_session_titler_section(self):
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            self.assertNotIn("session_titler", json.load(handle))

    def test_specs_dir_is_unset_unless_chosen(self):
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            self.assertNotIn("spec_archive", json.load(handle))
        self.assertIn("specs_dir      : not set", proc.stdout)

    def test_specs_dir_flag_writes_the_shortened_realpath(self):
        specs = os.path.join(self.dir, "plans")
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace,
                              "--specs-dir", specs)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            written = json.load(handle)["spec_archive"]["specs_dir"]
        self.assertEqual(written, setup_config.shorten(os.path.realpath(specs)))

    def test_writes_an_explicit_null_canonical_root(self):
        # Rule C off, recorded as a decision: what tells --report this config
        # did not come from an older copy that inferred a root.
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            guard = json.load(handle)["worktree_guard"]
        self.assertIn("canonical_root", guard)
        self.assertIsNone(guard["canonical_root"])

    def test_user_data_dirs_inside_the_checkout_are_refused(self):
        for flag in ("--handover-dir", "--specs-dir"):
            inside = os.path.join(self.REPO, "lib", "my-files")
            proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace,
                                  flag, inside)
            self.assertEqual(proc.returncode, 3, flag)
            self.assertIn("inside the toolkit checkout", proc.stderr, flag)
            self.assertFalse(os.path.exists(self.config), flag)

    def test_relative_handover_dir_is_refused(self):
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace,
                              "--handover-dir", "handovers")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("not an absolute path", proc.stderr)
        self.assertFalse(os.path.exists(self.config))

    def test_refuses_home_and_writes_nothing(self):
        proc = self.run_write("--name", "Sam", "--workspace-root", HOME)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("home directory", proc.stderr)
        self.assertFalse(os.path.exists(self.config))

    def test_refuses_cloud_synced_and_writes_nothing(self):
        proc = self.run_write("--name", "Sam",
                              "--workspace-root", "~/Dropbox/Work")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("cloud-synced", proc.stderr)
        self.assertFalse(os.path.exists(self.config))

    def test_refuses_nonexistent_and_writes_nothing(self):
        proc = self.run_write("--name", "Sam", "--workspace-root", "/nope/nowhere")
        self.assertEqual(proc.returncode, 3)
        self.assertFalse(os.path.exists(self.config))

    def test_missing_required_flag_fails(self):
        proc = self.run_write("--workspace-root", self.workspace)
        self.assertNotEqual(proc.returncode, 0)

    def test_review_mode_full_power_writes_the_build_key(self):
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace,
                              "--review-mode", "full-power")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            data = json.load(handle)
        self.assertEqual(data["build"]["review"], "full-power")

    def test_review_mode_full_is_refused_and_writes_nothing(self):
        # Setup never writes the pre-rename spelling -- only build_review_mode()
        # accepts "full" as an alias. argparse refuses it outright (exit 2).
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace,
                              "--review-mode", "full")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(os.path.exists(self.config))

    def test_review_mode_omitted_writes_no_build_key(self):
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            data = json.load(handle)
        self.assertNotIn("build", data)

    def test_review_mode_rejects_an_unrecognised_value(self):
        proc = self.run_write("--name", "Sam", "--workspace-root", self.workspace,
                              "--review-mode", "bogus")
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(self.config))


class TestBuildConfig(unittest.TestCase):

    def test_shortens_home_paths(self):
        config = setup_config.build_config(
            "Sam", os.path.join(HOME, "Work"), os.path.join(HOME, ".claude/handovers"),
            [os.path.join(HOME, "Work", "repos")], os.path.join(HOME, "plans"))
        self.assertEqual(config["workspace_root"], "~/Work")
        self.assertEqual(config["thread_handover"]["handover_dir"],
                         "~/.claude/handovers")
        self.assertEqual(config["danger_guard"]["extra_protected_paths"],
                         ["~/Work/repos"])
        self.assertEqual(config["spec_archive"]["specs_dir"], "~/plans")

    def test_specs_dir_is_never_derived_from_the_workspace(self):
        # The spec tools move files, so an unchosen directory stays unset
        # rather than guessed from a workspace layout.
        config = setup_config.build_config(
            "Sam", os.path.join(HOME, "Work"), "/h", [], None)
        self.assertNotIn("spec_archive", config)

    def test_no_roster_key_is_written(self):
        config = setup_config.build_config("Sam", "/w", "/h", [], "/s")
        self.assertNotIn("session_titler", config)

    def test_review_mode_written_when_given(self):
        config = setup_config.build_config("Sam", "/w", "/h", [], None,
                                           review_mode="full-power")
        self.assertEqual(config["build"]["review"], "full-power")

    def test_review_mode_omitted_when_not_given(self):
        # No "build" key at all -- not even an explicit "economical" -- so a
        # caller that never asked the question writes byte-identical output
        # to before this key existed. `build_review_mode()` already treats
        # absence as "economical".
        config = setup_config.build_config("Sam", "/w", "/h", [], None)
        self.assertNotIn("build", config)


class TestShorten(unittest.TestCase):

    def test_collapses_home(self):
        self.assertEqual(setup_config.shorten(os.path.join(HOME, "Work")), "~/Work")

    def test_leaves_other_paths_alone(self):
        self.assertEqual(setup_config.shorten("/opt/x"), "/opt/x")


EXPECTED_CATALOGUE_TOOLS = {
    "danger-guard", "worktree-guard", "secret-scan",
    "repo-freshness", "tooling-digest", "memory-routing", "build-cost",
    "session-claim", "session-titler", "thread-handover", "usage-watch",
    "caffeinate", "base-pin",
}

BANNED_WORDS = ("hook", "symlink", "config.json", "~/.config")


class TestListTools(unittest.TestCase):
    """Criteria 1-3: the catalogue and its `--list-tools` dump."""

    SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "setup_config.py")

    def run_list_tools(self):
        import subprocess
        return subprocess.run(["python3", self.SCRIPT, "--list-tools"],
                              capture_output=True, text=True)

    def test_emits_valid_json_and_exits_0_with_no_config_file(self):
        # No config path is even passed — the catalogue is static repo data.
        proc = self.run_list_tools()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertIn("groups", data)

    def test_covers_every_tool_in_the_catalogue_table(self):
        data = json.loads(self.run_list_tools().stdout)
        names = {tool["name"] for group in data["groups"] for tool in group["tools"]}
        self.assertEqual(names, EXPECTED_CATALOGUE_TOOLS)

    def test_every_tool_has_a_non_empty_plain_language_explanation(self):
        data = json.loads(self.run_list_tools().stdout)
        for group in data["groups"]:
            for tool in group["tools"]:
                explanation = tool.get("explanation", "")
                self.assertTrue(explanation.strip(), "empty explanation for %s" % tool["name"])
                lowered = explanation.lower()
                for banned in BANNED_WORDS:
                    self.assertNotIn(banned, lowered,
                                     "%s explanation leaks %r" % (tool["name"], banned))

    def test_every_group_has_a_title_and_a_tier(self):
        data = json.loads(self.run_list_tools().stdout)
        for group in data["groups"]:
            self.assertTrue(str(group.get("title", "")).strip(), group)
            self.assertIn(group.get("tier"), (1, 2, 3), group.get("title"))

    def test_tier_2_groups_have_at_most_4_tools(self):
        # The AskUserQuestion single-select cap. A fifth tool in a tier-2
        # group silently breaks the prompt it is meant to feed.
        data = json.loads(self.run_list_tools().stdout)
        tier_2_groups = [g for g in data["groups"] if g["tier"] == 2]
        self.assertTrue(tier_2_groups)
        for group in tier_2_groups:
            self.assertLessEqual(len(group["tools"]), 4,
                                 "%s has more than 4 tools" % group["title"])


class TestEnableDisable(unittest.TestCase):
    """Criteria 4-6 and 8: --enable/--disable on the --write path."""

    SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "setup_config.py")
    REPO = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "config.json")
        self.workspace = os.path.join(self.dir, "Workspace")
        os.makedirs(self.workspace)

    def run_write(self, *extra):
        import subprocess
        return subprocess.run(
            ["python3", self.SCRIPT, self.config, self.REPO, "--write",
             "--name", "Sam", "--workspace-root", self.workspace] + list(extra),
            capture_output=True, text=True)

    def test_disable_merges_into_the_section_without_dropping_other_keys(self):
        proc = self.run_write("--disable", "session_titler,thread_handover")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            data = json.load(handle)
        self.assertEqual(data["session_titler"]["enabled"], False)
        self.assertEqual(data["thread_handover"]["enabled"], False)
        # handover_dir came from build_config, not from --disable — a merge
        # must leave it in place rather than replacing the whole section.
        self.assertIn("handover_dir", data["thread_handover"])
        self.assertTrue(data["thread_handover"]["handover_dir"])

    def test_handover_dir_defaults_inside_the_workspace(self):
        # Not ~/.claude/handovers: Claude Code asks before every write under
        # ~/.claude, and a non-interactive session cannot write there at all.
        proc = self.run_write()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            data = json.load(handle)
        self.assertEqual(data["thread_handover"]["handover_dir"],
                         os.path.realpath(os.path.join(self.workspace, "handovers")))

    def test_disable_accepts_hyphenated_tool_names_too(self):
        proc = self.run_write("--disable", "session-titler")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            data = json.load(handle)
        self.assertEqual(data["session_titler"]["enabled"], False)

    def test_disable_tier1_section_exits_3_and_writes_nothing(self):
        proc = self.run_write("--disable", "danger_guard")
        self.assertEqual(proc.returncode, 3)
        self.assertTrue(proc.stderr.strip())
        self.assertFalse(os.path.exists(self.config))

    def test_enable_unknown_section_exits_3_and_writes_nothing(self):
        proc = self.run_write("--enable", "nonsense")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("nonsense", proc.stderr)
        self.assertFalse(os.path.exists(self.config))

    def test_same_name_in_both_flags_exits_3_and_writes_nothing(self):
        proc = self.run_write("--enable", "session_titler", "--disable", "session_titler")
        self.assertEqual(proc.returncode, 3)
        self.assertFalse(os.path.exists(self.config))

    def test_enable_creates_a_section_absent_from_build_config(self):
        # caffeinate isn't on this branch — build_config() never writes it —
        # but the catalogue lists it (tier 3), so --enable must still work once
        # the config selects the profile that owns it. A fresh --write has no
        # "profiles" key, so this goes through --update on a config naming "mac".
        with open(self.config, "w") as handle:
            json.dump({"profiles": ["continuity", "mac"]}, handle)
        import subprocess
        proc = subprocess.run(
            ["python3", self.SCRIPT, self.config, self.REPO, "--update",
             "--enable", "caffeinate"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            data = json.load(handle)
        self.assertEqual(data["caffeinate"]["enabled"], True)

    def test_neither_flag_given_writes_no_enabled_key_anywhere_new(self):
        proc = self.run_write()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            data = json.load(handle)
        self.assertNotIn("caffeinate", data)
        self.assertNotIn("enabled", data["thread_handover"])


class TestWriteOutputBaseline(unittest.TestCase):
    """Golden-master for `--write` with neither `--enable` nor `--disable`.

    Built by hand, independently of build_config(), so a change to the
    non_interactive() path shows up here even if every narrower test above
    happens to still pass. This is the call the installer and /onboard make.
    """

    SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "setup_config.py")
    REPO = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "config.json")
        self.workspace = os.path.join(self.dir, "Workspace")
        self.handovers = os.path.join(self.dir, "handovers")
        os.makedirs(self.workspace)

    def test_output_unchanged_with_neither_flag(self):
        import subprocess
        proc = subprocess.run(
            ["python3", self.SCRIPT, self.config, self.REPO, "--write",
             "--name", "Sam", "--workspace-root", self.workspace,
             "--handover-dir", self.handovers, "--no-protected"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        # Built independently of build_config()/write_config() — reproducing
        # their shape by hand is what makes this a real regression check
        # rather than a tautology against the code it is meant to catch.
        real_workspace = os.path.realpath(self.workspace)
        expected_config = {
            "//": ("Written by claude-code-toolkit setup. See config.example.json in "
                   "the repo for every available key and its default."),
            "owner_name": "Sam",
            "workspace_root": real_workspace,
            "danger_guard": {
                "extra_safe_roots": [],
                "extra_protected_paths": [],
            },
            "worktree_guard": {
                "canonical_root": None,
            },
            "thread_handover": {
                "handover_dir": os.path.realpath(self.handovers),
            },
        }
        expected_json = json.dumps(expected_config, indent=2) + "\n"
        with open(self.config) as handle:
            self.assertEqual(handle.read(), expected_json)

        expected_stdout = (
            "wrote %s\n"
            "  owner_name     : Sam\n"
            "  workspace_root : %s\n"
            "  protected      : 0 path(s)\n"
            "  handover_dir   : %s\n"
            "  specs_dir      : not set\n"
        ) % (self.config, real_workspace, os.path.realpath(self.handovers))
        self.assertEqual(proc.stdout, expected_stdout)


class TestUpdateMode(unittest.TestCase):
    """`--update` modifies a config in place.

    Unlike `--write`, `--update` never creates a config and never calls
    `validate_workspace_root` — that is exactly what "never hand-write the
    config to bypass the check" exists to prevent applied to a second
    writer. Every test that starts from an existing file asserts on the raw
    bytes when the outcome should be "nothing written," not just the exit
    code — a rejected `--update` that quietly rewrote the file with
    identical values would still look like a pass on returncode alone.
    """

    SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "setup_config.py")
    REPO = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "config.json")

    def run_update(self, *extra):
        import subprocess
        return subprocess.run(
            ["python3", self.SCRIPT, self.config, self.REPO, "--update"] + list(extra),
            capture_output=True, text=True)

    def write_raw_config(self, data):
        with open(self.config, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")

    def test_preserves_every_untouched_key_and_adds_only_the_toggle(self):
        original = {
            "//": "Written by claude-code-toolkit setup.",
            "owner_name": "Sam",
            "workspace_root": "/Users/sam/Work",
            "danger_guard": {
                "extra_safe_roots": [],
                "extra_protected_paths": ["/Users/sam/Work/repos"],
            },
            "thread_handover": {"handover_dir": "~/.claude/handovers"},
        }
        self.write_raw_config(original)

        proc = self.run_update("--disable", "session_titler")
        self.assertEqual(proc.returncode, 0, proc.stderr)

        with open(self.config) as handle:
            updated = json.load(handle)

        # Compare key by key, not by string diff, per the acceptance criterion.
        for key, value in original.items():
            self.assertEqual(updated[key], value, "key %r changed" % key)
        self.assertEqual(updated["session_titler"], {"enabled": False})
        self.assertEqual(set(updated) - set(original), {"session_titler"})

    def test_missing_config_exits_3_and_creates_no_file(self):
        proc = self.run_update("--disable", "session_titler")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("--write", proc.stderr)
        self.assertFalse(os.path.exists(self.config))

    def test_invalid_json_exits_3_and_leaves_bytes_unchanged(self):
        with open(self.config, "wb") as handle:
            handle.write(b"{ this is not json")
        with open(self.config, "rb") as handle:
            before = handle.read()

        proc = self.run_update("--disable", "session_titler")
        self.assertEqual(proc.returncode, 3)

        with open(self.config, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)

    def test_name_alongside_update_exits_2_without_writing(self):
        self.write_raw_config({"owner_name": "Sam"})
        with open(self.config, "rb") as handle:
            before = handle.read()

        proc = self.run_update("--disable", "session_titler", "--name", "X")
        self.assertEqual(proc.returncode, 2)

        with open(self.config, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)

    def test_workspace_root_alongside_update_exits_2_without_writing(self):
        self.write_raw_config({"owner_name": "Sam"})
        with open(self.config, "rb") as handle:
            before = handle.read()

        proc = self.run_update("--disable", "session_titler",
                               "--workspace-root", "/tmp/whatever")
        self.assertEqual(proc.returncode, 2)

        with open(self.config, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)

    def test_disable_tier1_section_exits_3_without_writing(self):
        self.write_raw_config({"owner_name": "Sam"})
        with open(self.config, "rb") as handle:
            before = handle.read()

        proc = self.run_update("--disable", "danger_guard")
        self.assertEqual(proc.returncode, 3)

        with open(self.config, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)

    def test_neither_flag_exits_2_without_writing(self):
        self.write_raw_config({"owner_name": "Sam"})
        with open(self.config, "rb") as handle:
            before = handle.read()

        proc = self.run_update()
        self.assertEqual(proc.returncode, 2)

        with open(self.config, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)

    def test_unknown_section_exits_3_without_writing(self):
        # Same catalogue validation as --write, reused rather than
        # reimplemented — resolve_toggle_sections() is the one source.
        self.write_raw_config({"owner_name": "Sam"})
        with open(self.config, "rb") as handle:
            before = handle.read()

        proc = self.run_update("--enable", "nonsense")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("nonsense", proc.stderr)

        with open(self.config, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)

    def test_same_name_in_both_flags_exits_3_without_writing(self):
        self.write_raw_config({"owner_name": "Sam"})
        with open(self.config, "rb") as handle:
            before = handle.read()

        proc = self.run_update("--enable", "session_titler",
                               "--disable", "session_titler")
        self.assertEqual(proc.returncode, 3)

        with open(self.config, "rb") as handle:
            after = handle.read()
        self.assertEqual(before, after)

    def test_round_trip_write_then_disable_then_enable(self):
        import subprocess
        workspace = os.path.join(self.dir, "Workspace")
        os.makedirs(workspace)
        write_proc = subprocess.run(
            ["python3", self.SCRIPT, self.config, self.REPO, "--write",
             "--name", "Sam", "--workspace-root", workspace],
            capture_output=True, text=True)
        self.assertEqual(write_proc.returncode, 0, write_proc.stderr)

        with open(self.config) as handle:
            original = json.load(handle)

        disable = self.run_update("--disable", "session_titler,thread_handover")
        self.assertEqual(disable.returncode, 0, disable.stderr)

        enable = self.run_update("--enable", "session_titler,thread_handover")
        self.assertEqual(enable.returncode, 0, enable.stderr)

        with open(self.config) as handle:
            final = json.load(handle)

        # Nothing else moved: the result is the original plus enabled=True
        # on exactly the two sections that went through disable-then-enable.
        expected = dict(original)
        expected["session_titler"] = dict(original.get("session_titler", {}), enabled=True)
        expected["thread_handover"] = dict(original["thread_handover"], enabled=True)
        self.assertEqual(final, expected)


class TestToolPresence(unittest.TestCase):
    """`present` must be derived from the filesystem, not hardcoded.

    `tool_presence()` is exercised directly against a temporary directory
    holding a chosen subset of `tools/<name>/` — never against the real
    `tools/` listing alone, which would make the ground truth the same data
    the assertion checks and pass no matter what the code does.
    """

    def test_present_follows_a_fixture_directory_not_the_real_repo(self):
        fixture = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(fixture, ignore_errors=True))
        # In the fixture, only these two tool directories exist.
        os.makedirs(os.path.join(fixture, "tools", "danger-guard"))
        os.makedirs(os.path.join(fixture, "tools", "repo-freshness"))

        presence = setup_config.tool_presence(fixture)

        # Present in the fixture.
        self.assertTrue(presence["danger-guard"])
        self.assertTrue(presence["repo-freshness"])
        # Exists in the REAL repo's tools/ but was deliberately left out of
        # the fixture — this is the case that can only pass if the flag is
        # actually derived from the directory it was pointed at, rather than
        # from whether the real repo happens to have it.
        self.assertFalse(presence["session-titler"])

    def test_root_that_cannot_be_determined_reports_everything_present(self):
        presence = setup_config.tool_presence(None)
        self.assertTrue(all(presence.values()))

    def test_unreadable_tools_directory_reports_everything_present(self):
        # A directory with no `tools/` subdirectory at all: os.listdir raises.
        fixture = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(fixture, ignore_errors=True))
        presence = setup_config.tool_presence(fixture)
        self.assertTrue(all(presence.values()))


class TestResolveRepoRoot(unittest.TestCase):
    """`resolve_repo_root()` must resolve through a symlink, not just find
    SOME directory two levels up. `tool_presence()`'s own fixture test above
    is handed an already-resolved root directly, so it cannot see a broken
    `realpath` — this repo's hard rule ("realpath, not abspath, for anything
    `__file__`-relative") is only distinguishable from `abspath` when the
    file is actually reached through a symlink, so that is what this test
    builds.
    """

    def test_resolves_to_the_real_repo_through_a_symlinked_invocation(self):
        real_script = os.path.realpath(setup_config.__file__)

        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        fake_lib = os.path.join(tmp, "lib")
        os.makedirs(fake_lib)
        link = os.path.join(fake_lib, "setup_config.py")
        os.symlink(real_script, link)
        # A tools/ directory exists at the WRONG (symlink's own) location
        # too, but deliberately holds none of the catalogue's tools — so if
        # resolution used this directory instead of resolving through the
        # symlink to the real repo, every catalogue tool would report absent.
        os.makedirs(os.path.join(tmp, "tools", "decoy"))

        import subprocess
        proc = subprocess.run(["python3", link, "--list-tools"],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        seen = {tool["name"]: tool["present"]
               for group in data["groups"] for tool in group["tools"]}
        # Every catalogue tool present in the real repo must read present.
        # True here can only mean resolution found the REAL repo's tools/
        # through the symlink, not the symlink's own (decoy-only) directory.
        real = [name for name in seen
                if os.path.isdir(os.path.join(REPO_ROOT, "tools", name))]
        self.assertTrue(real, "no catalogue tool exists in this checkout")
        self.assertTrue(all(seen[name] for name in real),
                        "resolved to the symlink's own directory instead of "
                        "through it to the real repo — realpath was not applied")


class TestListToolsPresentField(unittest.TestCase):
    """The `present` field on --list-tools output."""

    SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "setup_config.py")
    REPO = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    def run_list_tools(self):
        import subprocess
        return subprocess.run(["python3", self.SCRIPT, "--list-tools"],
                              capture_output=True, text=True)

    def all_tools(self, data):
        return [tool for group in data["groups"] for tool in group["tools"]]

    def test_every_tool_object_has_the_expected_keys(self):
        # Named without a count on purpose. This assertion is the contract for
        # what `--list-tools` emits, so it is meant to be updated deliberately
        # when a field is added -- `doc_keywords` was the first one, and a
        # count in the name meant renaming the test to change the set.
        data = json.loads(self.run_list_tools().stdout)
        for tool in self.all_tools(data):
            self.assertEqual(set(tool.keys()),
                             {"name", "section", "tier", "explanation",
                              "present", "doc_keywords", "profile",
                              "profile_selected"},
                             tool.get("name"))

    def test_present_matches_the_real_tools_directory_for_every_tool(self):
        # Ground truth computed HERE, independently of setup_config's own
        # presence logic, straight off the real repo's `tools/` directory —
        # deliberately not pinned to any one tool by name, since which tools
        # exist changes as tools are added.
        data = json.loads(self.run_list_tools().stdout)
        for tool in self.all_tools(data):
            expected = os.path.isdir(os.path.join(self.REPO, "tools", tool["name"]))
            self.assertEqual(tool["present"], expected, tool["name"])

    # "--enable caffeinate still succeeds and creates the section while
    # present is false" is covered by
    # `test_enable_creates_a_section_absent_from_build_config` in
    # TestEnableDisable above, left untouched: it never reads `present` at
    # all, which is exactly the point — the write path stays unaware of it.


class TestProfileOwnedTools(unittest.TestCase):
    """A tool whose profile is not selected is not offered or enabled."""

    SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "setup_config.py")
    REPO = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "config.json")
        self.workspace = os.path.join(self.dir, "Workspace")
        os.makedirs(self.workspace)

    def run_script(self, args, env_config=None):
        import subprocess
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = env_config or os.path.join(self.dir, "absent.json")
        return subprocess.run(["python3", self.SCRIPT] + args,
                              capture_output=True, text=True, env=env)

    def tools_by_name(self, env_config=None):
        proc = self.run_script(["--list-tools"], env_config)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return {tool["name"]: tool for group in json.loads(proc.stdout)["groups"]
                for tool in group["tools"]}

    def write_selection(self, data):
        path = os.path.join(self.dir, "selection.json")
        with open(path, "w") as handle:
            json.dump(data, handle)
        return path

    def test_profile_per_tool_in_the_real_repo(self):
        expected = {
            "build-cost": "builder", "base-pin": "builder", "session-claim": "builder",
            "caffeinate": "mac",
            "session-titler": "continuity", "thread-handover": "continuity",
            "usage-watch": "continuity", "memory-routing": "continuity",
            "danger-guard": "core", "repo-freshness": "core",
            "tooling-digest": "core", "secret-scan": "core",
        }
        tools = self.tools_by_name()
        for name, profile in expected.items():
            self.assertEqual(tools[name]["profile"], profile, name)

    def test_tool_profiles_fails_toward_core_when_the_repo_is_unreadable(self):
        owners = setup_config.tool_profiles(os.path.join(self.dir, "no-such-repo"))
        self.assertTrue(owners)
        self.assertEqual(set(owners.values()), {"core"})

    def test_selected_follows_the_default_set_with_no_config(self):
        tools = self.tools_by_name()
        for name in ("build-cost", "base-pin", "session-claim", "caffeinate"):
            self.assertIs(tools[name]["profile_selected"], False, name)
        for name in ("session-titler", "thread-handover", "usage-watch",
                     "memory-routing", "danger-guard", "secret-scan"):
            self.assertIs(tools[name]["profile_selected"], True, name)

    def test_selected_follows_a_profiles_list(self):
        path = self.write_selection({"profiles": ["continuity", "builder"]})
        tools = self.tools_by_name(path)
        for name in ("build-cost", "base-pin", "session-claim"):
            self.assertIs(tools[name]["profile_selected"], True, name)
        self.assertIs(tools["caffeinate"]["profile_selected"], False)

    def test_a_bad_profiles_list_exits_3_with_nothing_on_stdout(self):
        path = self.write_selection({"profiles": ["nope"]})
        proc = self.run_script(["--list-tools"], path)
        self.assertEqual(proc.returncode, 3)
        self.assertIn("nope", proc.stderr)
        self.assertEqual(proc.stdout, "")

    def run_update(self, *extra):
        return self.run_script([self.config, self.REPO, "--update"] + list(extra))

    def write_raw(self, data):
        with open(self.config, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")

    def test_update_enable_refuses_a_tool_whose_profile_is_not_selected(self):
        self.write_raw({"owner_name": "Sam"})
        with open(self.config, "rb") as handle:
            before = handle.read()
        proc = self.run_update("--enable", "base_pin")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("builder", proc.stderr)
        with open(self.config, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_update_enable_passes_once_the_profile_is_selected(self):
        self.write_raw({"owner_name": "Sam", "profiles": ["continuity", "builder"]})
        proc = self.run_update("--enable", "base_pin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(self.config) as handle:
            self.assertIs(json.load(handle)["base_pin"]["enabled"], True)

    def test_update_disable_is_allowed_without_the_profile(self):
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--disable", "base_pin")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_write_enable_of_a_mac_tool_exits_3_and_writes_no_file(self):
        proc = self.run_script([self.config, self.REPO, "--write", "--name", "Sam",
                                "--workspace-root", self.workspace,
                                "--enable", "caffeinate"])
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("mac", proc.stderr)
        self.assertFalse(os.path.exists(self.config))


class TestProfilesFlag(unittest.TestCase):
    """--profiles on --write and --update: validated first, written with the Mac rule."""

    SCRIPT = TestProfileOwnedTools.SCRIPT
    REPO = TestProfileOwnedTools.REPO

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.config = os.path.join(self.dir, "config.json")
        self.workspace = os.path.join(self.dir, "Workspace")
        os.makedirs(self.workspace)

    def run_script(self, args):
        import subprocess
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = self.config
        return subprocess.run(["python3", self.SCRIPT, self.config, self.REPO] + args,
                              capture_output=True, text=True, env=env)

    def run_write(self, *extra):
        return self.run_script(["--write", "--name", "Sam",
                                "--workspace-root", self.workspace] + list(extra))

    def run_update(self, *extra):
        return self.run_script(["--update"] + list(extra))

    def write_raw(self, data):
        with open(self.config, "w") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")

    def read(self):
        with open(self.config) as handle:
            return json.load(handle)

    def read_bytes(self):
        with open(self.config, "rb") as handle:
            return handle.read()

    def test_write_profiles_adds_core_and_turns_caffeinate_off(self):
        proc = self.run_write("--profiles", "continuity,builder")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        config = self.read()
        self.assertEqual(config["profiles"], ["core", "continuity", "builder"])
        self.assertIs(config["caffeinate"]["enabled"], False)

    def test_write_profiles_with_mac_turns_caffeinate_on(self):
        proc = self.run_write("--profiles", "continuity,mac")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(self.read()["caffeinate"]["enabled"], True)

    def test_explicit_disable_caffeinate_wins_over_mac(self):
        proc = self.run_write("--profiles", "continuity,mac", "--disable", "caffeinate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        config = self.read()
        self.assertIs(config["caffeinate"]["enabled"], False)
        self.assertIn("mac", config["profiles"])

    def test_write_profiles_applies_the_requires_closure_in_registry_order(self):
        proc = self.run_write("--profiles", "maintainer,continuity")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read()["profiles"],
                         ["core", "continuity", "builder", "maintainer"])

    def test_write_unknown_profile_exits_3_naming_valid_ones_and_writes_nothing(self):
        proc = self.run_write("--profiles", "nope")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("nope", proc.stderr)
        self.assertIn("builder", proc.stderr)
        self.assertFalse(os.path.exists(self.config))

    def test_update_empty_profiles_exits_3_and_leaves_the_file_alone(self):
        self.write_raw({"owner_name": "Sam"})
        before = self.read_bytes()
        proc = self.run_update("--profiles", "")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.read_bytes(), before)

    def test_update_judges_enable_against_the_new_profiles(self):
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--profiles", "core,continuity,builder",
                               "--enable", "base_pin")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        config = self.read()
        self.assertIs(config["base_pin"]["enabled"], True)
        self.assertEqual(config["profiles"], ["core", "continuity", "builder"])

    def test_update_profiles_alone_warns_when_a_default_is_left_out(self):
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--profiles", "builder")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("continuity", proc.stderr)
        self.assertEqual(self.read()["profiles"], ["core", "builder"])

    def test_update_unknown_profile_exits_3_and_leaves_the_file_alone(self):
        self.write_raw({"owner_name": "Sam", "profiles": ["continuity"]})
        before = self.read_bytes()
        proc = self.run_update("--profiles", "nope")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.read_bytes(), before)

    def test_update_profiles_preserves_every_other_key(self):
        original = {"owner_name": "Sam", "//note": "keep", "thread_handover": {"x": 1},
                    "profiles": ["continuity"]}
        self.write_raw(original)
        proc = self.run_update("--profiles", "core,continuity")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = self.read()
        for key in ("profiles", "caffeinate"):
            after.pop(key, None)
            original.pop(key, None)
        self.assertEqual(after, original)

    def test_update_dropping_mac_turns_an_enabled_caffeinate_off(self):
        self.write_raw({"profiles": ["continuity", "mac"],
                        "caffeinate": {"enabled": True, "hold_seconds": 60}})
        proc = self.run_update("--profiles", "core,continuity")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read()["caffeinate"],
                         {"enabled": False, "hold_seconds": 60})


    def test_update_profiles_refuses_a_caffeinate_section_that_is_not_an_object(self):
        """Code Review F2: `"caffeinate": null` must exit 3, never crash mid-write."""
        self.write_raw({"owner_name": "Sam", "caffeinate": None})
        with open(self.config, "rb") as handle:
            before = handle.read()
        proc = self.run_update("--profiles", "core,continuity")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("caffeinate", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        with open(self.config, "rb") as handle:
            self.assertEqual(handle.read(), before)


def unregistered_tools(catalogue, rows):
    """Names in `catalogue` with no hook row whose relpath is under tools/<name>/."""
    return {tool["name"] for group in catalogue for tool in group["tools"]
            if not any(row["relpath"].startswith("tools/%s/" % tool["name"])
                       for row in rows)}


class TestEveryCatalogueToolIsRegistered(unittest.TestCase):
    """A catalogue tool with no hook registration is silently reported `core`."""

    REPO = TestProfileOwnedTools.REPO

    def rows(self):
        manifest = setup_config._sibling("hook_manifest")
        self.assertIsNotNone(manifest)
        return manifest.plugin_registrations(self.REPO)

    def test_only_secret_scan_is_unregistered(self):
        self.assertEqual(unregistered_tools(setup_config.TOOL_CATALOGUE, self.rows()),
                         {"secret-scan"})

    def test_the_helper_reports_a_tool_with_no_registration(self):
        fake = [{"tools": [{"name": "no-such-tool"}]}]
        self.assertEqual(unregistered_tools(fake, self.rows()), {"no-such-tool"})


def _missing_doc_coverage(tools, doc_text):
    """[(name, keywords)] for every tool whose `doc_keywords` all fail to
    appear in `doc_text`, case-insensitively.

    Plain raw-substring containment — the same comparison the
    onboarding verification script uses verbatim, so a tool that passes here
    also passes there. `doc_keywords` are chosen to sit on a single wrapped
    line in `ONBOARDING.md` for exactly this reason.

    Shared by the real-file check and the fixture check below so both run
    the identical comparison — the fixture is what proves this function
    actually discriminates, rather than something that reports "covered"
    no matter what it is handed.
    """
    lowered = doc_text.lower()
    missing = []
    for tool in tools:
        keywords = tool.get("doc_keywords") or []
        if not any(kw.lower() in lowered for kw in keywords):
            missing.append((tool["name"], keywords))
    return missing


class TestDocKeywordsCoverage(unittest.TestCase):
    """Every catalogue tool must have real ONBOARDING.md phrases,
    and the coverage check itself must be provably non-vacuous.

    A check whose ground truth is the file it verifies passes no matter
    what the code does — that is the exact failure this test exists to
    close, one level up from a hand-walked verification. So the
    "it actually catches a gap" proof below runs against a temporary
    fixture, never against ONBOARDING.md itself.
    """

    def all_tools(self):
        return [tool for group in setup_config.TOOL_CATALOGUE for tool in group["tools"]]

    def test_every_tool_has_non_empty_doc_keywords(self):
        for tool in self.all_tools():
            self.assertTrue(tool.get("doc_keywords"),
                            "%s has no doc_keywords" % tool["name"])

    def test_no_keyword_is_too_generic(self):
        # A crude guard, but it stops something like "sessions" being
        # added later — a keyword must be distinctive enough on its own.
        for tool in self.all_tools():
            for keyword in tool["doc_keywords"]:
                words = keyword.split()
                self.assertTrue(
                    len(words) >= 3 or len(keyword) >= 15,
                    "%s: keyword %r is too generic (fewer than ~3 words "
                    "or ~15 characters)" % (tool["name"], keyword))

    def test_onboarding_md_covers_every_catalogue_tool(self):
        # skipTest only when the repo root cannot be resolved. ONBOARDING.md
        # ships, so a missing file fails, and so does a phrase that IS present
        # but doesn't match.
        repo_root = setup_config.resolve_repo_root()
        if not repo_root:
            self.skipTest("could not resolve repo root")
        doc_path = os.path.join(repo_root, "ONBOARDING.md")
        with open(doc_path) as handle:
            doc_text = handle.read()

        missing = _missing_doc_coverage(self.all_tools(), doc_text)
        self.assertEqual(
            missing, [],
            "tools with no plain-terms coverage in ONBOARDING.md: " +
            ", ".join("%s (looked for %r)" % (name, keywords)
                     for name, keywords in missing))

    def test_coverage_check_catches_a_genuinely_missing_tool(self):
        # Non-vacuous by construction: the fixture, not ONBOARDING.md, is
        # the ground truth. Keep every other tool's first keyword in the
        # fixture text and omit the rest — the omitted half is exactly what
        # must come back as missing. If the comparison were stubbed to
        # always report "all covered", this assertion catches it.
        tools = self.all_tools()
        covered_tools = tools[::2]
        omitted_tools = tools[1::2]
        self.assertTrue(omitted_tools, "fixture needs at least one omitted tool")

        fixture_text = "\n".join(tool["doc_keywords"][0] for tool in covered_tools)

        missing = _missing_doc_coverage(tools, fixture_text)
        missing_names = {name for name, _ in missing}
        expected_missing = {tool["name"] for tool in omitted_tools}
        self.assertEqual(missing_names, expected_missing)
        # And the covered half must NOT show up as missing.
        covered_names = {tool["name"] for tool in covered_tools}
        self.assertEqual(missing_names & covered_names, set())


# Matches a `cp` of a *.template into a destination, inside a fenced block, at
# the start of a line, or inside a quoted string a script prints (`warn "   cp
# ..."`). Deliberately loose on the destination -- what matters is the flags.
# The `cp` may follow whitespace or a quote, and the source may be followed by
# the closing escaped quote of that string.
_TEMPLATE_CP = re.compile(
    r"(?:^|[\s\"'])cp\s+(?P<flags>(?:-\S+\s+)*)(?P<src>\S*?\.template)[\\\"']*\s+\S+",
    re.MULTILINE)


def clobbering_template_copies(text):
    """[(src, flags)] for every template `cp` that could OVERWRITE its target.

    Shared by the real-file check and the fixture check below, so both run the
    identical predicate -- the fixture is what proves this discriminates rather
    than reporting "all safe" no matter what it is handed. Same discipline as
    `missing_doc_keyword_tools` above.
    """
    bad = []
    for m in _TEMPLATE_CP.finditer(text):
        flags = m.group("flags") or ""
        if "-n" not in flags:
            bad.append((m.group("src"), flags.strip()))
    return bad


def _missing_onboard_coverage(tools, onboard_text):
    """[(name, tier)] for every catalogue tool `skills/core/onboard/SKILL.md` does not
    visibly account for.

    Finding C (whole-branch review): unlike ONBOARDING.md, the onboard skill is
    instructions for the AGENT running /onboard, not prose read by the end
    user, and it deliberately does NOT restate each tool's explanation --
    its own rule is "read this yourself... don't restate its explanations
    from memory or from this file." So the doc_keywords/name comparison
    `_missing_doc_coverage` uses for ONBOARDING.md is the wrong ground truth
    here: `secret-scan`, for one, is paraphrased in Tier 1's shared
    explanation and never named literally, by design, and that is correct.

    What the skill DOES have to guarantee, for every tool, is that Phase 2
    would actually offer or explain it. Tiers 1 and 2 do this by being
    genuinely dynamic -- "For each tier-2 group `--list-tools` returns, ask
    ..." -- which covers every tool in that tier regardless of how many
    there are or what they're called, and tier 1 explains its WHOLE group
    in one shared paragraph rather than tool by tool (its own heading says
    so: "explain it, don't offer it"), so tier 1 is never checked per tool.
    Tier 2/3 tools are "covered" if EITHER their name appears literally in
    their tier's own paragraph, OR that paragraph reads as dynamic --
    contains "every" or "each" (case-insensitive) -- rather than naming a
    fixed subset. This is exactly the shape of Finding C's bug: the old
    Tier 3 paragraph named `caffeinate` and nothing else, and contained
    neither "every" nor "each", so prompt-optimizer and workspace-sync
    (since retired) would have come back missing against the pre-fix text.
    """
    sections = {}
    for marker in ("**Tier 1", "**Tier 2", "**Tier 3", "**Tier 4", "**Tier 5"):
        start = onboard_text.find(marker)
        if start == -1:
            continue
        tier = int(marker[len("**Tier "):])
        # A tier's own paragraph ends at the next blank line -- Markdown
        # paragraphs are blank-line-separated, and each tier here is one
        # paragraph. Stopping only at the next "**Tier" marker or "## "
        # heading is too loose: the skill's shared write-back instructions
        # ("Write what they chose...") sit in the very next paragraph after
        # Tier 3's own, before either boundary, and contain "each" -- which
        # let the check pass against the true pre-fix text on a first cut
        # of this function despite the bug being present.
        end = onboard_text.find("\n\n", start)
        if end == -1:
            end = len(onboard_text)
        sections[tier] = onboard_text[start:end]

    missing = []
    for tool in tools:
        tier = tool["tier"]
        if tier == 1:
            # Explained collectively, never offered per tool -- see
            # docstring above.
            continue
        section = sections.get(tier, "")
        lowered = section.lower()
        name_hit = tool["name"].lower() in lowered
        dynamic_hit = "every" in lowered or "each" in lowered
        if not (name_hit or dynamic_hit):
            missing.append((tool["name"], tier))
    return missing


class TestOnboardMdCoverage(unittest.TestCase):
    """Mirrors TestDocKeywordsCoverage.test_onboarding_md_covers_every_catalogue_tool,
    but against skills/core/onboard/SKILL.md -- the file that actually decides what
    gets OFFERED during /onboard, which nothing checked before Finding C.
    The old Tier 3 instruction hardcoded "offer `caffeinate` alone" from
    back when tier 3 held one tool; two more (prompt-optimizer, and
    workspace-sync, since retired) were added later, both `enabled: false`
    by default, so
    following the old instruction literally would install both and leave
    them permanently inert -- tests green, `--check` clean, nothing visibly
    wrong."""

    def all_tools(self):
        return [tool for group in setup_config.TOOL_CATALOGUE for tool in group["tools"]]

    def test_onboard_md_covers_every_catalogue_tool(self):
        # Reads the skill straight from REPO_ROOT: no skip, so a missing
        # skill fails loudly rather than passing as a skip.
        doc_path = os.path.join(REPO_ROOT, "skills", "core", "onboard", "SKILL.md")
        with open(doc_path, encoding="utf-8") as handle:
            onboard_text = handle.read()

        missing = _missing_onboard_coverage(self.all_tools(), onboard_text)
        self.assertEqual(
            missing, [],
            "tools skills/core/onboard/SKILL.md does not visibly offer (by name or "
            "by dynamic tier language): " +
            ", ".join("%s (tier %d)" % (name, tier) for name, tier in missing))

    def test_coverage_check_catches_a_hardcoded_partial_tier(self):
        # Non-vacuous by construction, mirroring
        # TestDocKeywordsCoverage.test_coverage_check_catches_a_genuinely_missing_tool:
        # this fixture is exactly Finding C's original bug shape -- a tier-3
        # paragraph naming only ONE of several tier-3 tools, with no
        # "every"/"each" language -- and the other tools in that tier must
        # come back missing.
        tools = [
            {"name": "tool-a", "tier": 3},
            {"name": "tool-b", "tier": 3},
            {"name": "tool-c", "tier": 3},
        ]
        fixture_text = "**Tier 3 — offer `tool-a` alone**, nothing pre-selected."
        missing = _missing_onboard_coverage(tools, fixture_text)
        self.assertEqual({name for name, _ in missing}, {"tool-b", "tool-c"})
        # tool-a itself must NOT show up as missing.
        self.assertNotIn("tool-a", {name for name, _ in missing})

    def test_coverage_check_passes_generic_tier_language(self):
        # The fixed shape: a tier paragraph that reads as dynamic covers
        # every tool in that tier without naming any of them.
        tools = [
            {"name": "tool-a", "tier": 3},
            {"name": "tool-b", "tier": 3},
        ]
        fixture_text = ("**Tier 3 — offer every tier-3 tool** `--list-tools` "
                        "returns, nothing pre-selected.")
        missing = _missing_onboard_coverage(tools, fixture_text)
        self.assertEqual(missing, [])

    def test_tier_1_tools_are_never_flagged_missing(self):
        # Tier 1 is explained collectively, never offered per tool -- a
        # tier-1 tool must never show up as missing regardless of whether
        # its name or the words "every"/"each" appear anywhere.
        tools = [{"name": "some-safety-tool", "tier": 1}]
        missing = _missing_onboard_coverage(tools, "**Tier 1 — says nothing about it.")
        self.assertEqual(missing, [])


_PHASE_HEADING_RE = re.compile(r'^##\s*Phase\s+(\d+[A-Za-z]?)\b', re.MULTILINE)
_PHASE_REFERENCE_RE = re.compile(r'\bPhase\s+(\d+[A-Za-z]?)\b', re.IGNORECASE)


def _missing_phase_references(onboarding_text, onboard_text):
    """[reference, ...] for every `Phase <N>` reference in `onboarding_text`
    (ONBOARDING.md prose) that does not resolve to a real `## Phase <N>`
    heading in `onboard_text` (skills/core/onboard/SKILL.md, which OWNS the
    phase numbering). Letter-suffixed phases (`9a`, `9b`) are handled the same
    way as plain numbers -- the suffix is just part of the phase's identity.

    Comparison is case-insensitive on the suffix letter only (`9a` and `9A`
    are the same phase); duplicates in `onboarding_text` are reported once,
    in first-seen order.

    What this catches: a `Phase <N>` reference left pointing at a number no
    heading owns any more -- the shape you get when a phase is deleted, or
    the two files fall out of sync outright.

    What it does NOT catch: a renumber-by-insertion. Inserting a new
    `## Phase 4` and shifting the later phases leaves every number
    ONBOARDING.md referenced still resolving to a heading, just a different
    one than before, so this check stays green and a human has to notice by
    hand that ONBOARDING.md's prose now means something else. Resolving to
    *a* heading is not the same as resolving to the *right* one, and this
    function only ever checks the former.
    """
    # A `## Phase <N>` line quoted in a fence is text, not a heading.
    headings = {match.group(1).lower() for match in
                claude_imports.matches_outside_fences(_PHASE_HEADING_RE, onboard_text)}

    missing = []
    seen = set()
    for match in _PHASE_REFERENCE_RE.finditer(onboarding_text):
        reference = match.group(1)
        if reference.lower() in headings:
            continue
        if reference.lower() not in seen:
            seen.add(reference.lower())
            missing.append(reference)
    return missing


class TestPhaseReferencesAgree(unittest.TestCase):
    """Pins that every `Phase <N>` in ONBOARDING.md still resolves to a real
    `## Phase <N>` heading in skills/core/onboard/SKILL.md, which would
    otherwise drift silently because nothing else compares the two files'
    phase numbering.
    """

    def test_onboarding_md_phase_references_resolve_to_real_headings(self):
        # skipTest only for the unavoidable environment dependency (no repo
        # root); a missing file fails with the open() error.
        repo_root = setup_config.resolve_repo_root()
        if not repo_root:
            self.skipTest("could not resolve repo root")
        onboarding_path = os.path.join(repo_root, "ONBOARDING.md")
        onboard_path = os.path.join(repo_root, "skills", "core", "onboard", "SKILL.md")
        with open(onboarding_path, encoding="utf-8") as handle:
            onboarding_text = handle.read()
        with open(onboard_path, encoding="utf-8") as handle:
            onboard_text = handle.read()

        missing = _missing_phase_references(onboarding_text, onboard_text)
        self.assertEqual(
            missing, [],
            "ONBOARDING.md references phase(s) with no matching '## Phase "
            "<N>' heading in skills/core/onboard/SKILL.md: " + ", ".join(missing))

    def test_catches_a_reference_to_a_phase_that_does_not_exist(self):
        # The fixture, not the real files, is the ground truth here.
        onboard_text = "## Phase 0 \u2014 X\n\n## Phase 1 \u2014 Y\n\n## Phase 9a \u2014 Z\n"
        onboarding_text = ("See Phase 1 and Phase 9a for details, and also "
                           "Phase 42, which was never renumbered.")
        missing = _missing_phase_references(onboarding_text, onboard_text)
        self.assertEqual(missing, ["42"])

    def test_letter_suffixed_phases_are_not_false_positives(self):
        # 9a/9b must resolve just like a plain number -- this is the shape
        # that's easy to get wrong with a naive digits-only regex.
        onboard_text = "## Phase 9a \u2014 X\n\n## Phase 9b \u2014 Y\n"
        onboarding_text = "Do Phase 9a first, then Phase 9b."
        missing = _missing_phase_references(onboarding_text, onboard_text)
        self.assertEqual(missing, [])

    def test_lowercase_phase_reference_is_still_caught(self):
        # References appear in ordinary prose and a writer capitalizes them
        # inconsistently, so "phase 99" must not slip past a capital-P regex.
        onboard_text = "## Phase 1 \u2014 X\n"
        onboarding_text = "See phase 99 for details."
        missing = _missing_phase_references(onboarding_text, onboard_text)
        self.assertEqual(missing, ["99"])

    def test_a_missing_phase_spelled_two_ways_is_reported_once(self):
        # The suffix compares case-insensitively, so 9Z and 9z are one phase:
        # one entry, in its first-seen spelling.
        onboard_text = "## Phase 1 — X\n"
        onboarding_text = "Phase 9Z and phase 9z are both missing."
        missing = _missing_phase_references(onboarding_text, onboard_text)
        self.assertEqual(missing, ["9Z"])

    def test_a_phase_heading_quoted_in_a_fence_does_not_resolve(self):
        # A `## Phase 42` inside a code block is quoted text, so a reference
        # to Phase 42 is still dead. The nested pair defeats a scanner that
        # toggles on any ```.
        for name, opener, closer in (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
                                     ("nested", "````\n```", "```\n````")):
            with self.subTest(fence=name):
                onboard_text = "## Phase 1 — X\n%s\n## Phase 42 — Q\n%s\n" % (opener, closer)
                missing = _missing_phase_references("See Phase 1 and Phase 42.", onboard_text)
                self.assertEqual(missing, ["42"])
                # Positive control: unfenced, the same line IS a heading.
                live = "## Phase 1 — X\n\n## Phase 42 — Q\n"
                self.assertEqual(_missing_phase_references("See Phase 1 and Phase 42.", live), [])


class TestOnboardingPlainLanguage(unittest.TestCase):
    """Pins that ONBOARDING.md never uses a BANNED_WORDS term, since its
    reader has never used a terminal on purpose and the wording would drift
    back to jargon unnoticed.
    """

    def test_onboarding_md_never_uses_the_banned_words(self):
        repo_root = setup_config.resolve_repo_root()
        if not repo_root:
            self.skipTest("could not resolve repo root")
        doc_path = os.path.join(repo_root, "ONBOARDING.md")
        with open(doc_path, encoding="utf-8") as handle:
            lines = handle.readlines()

        hits = []
        for lineno, line in enumerate(lines, 1):
            lowered = line.lower()
            for banned in BANNED_WORDS:
                if banned in lowered:
                    hits.append("line %d: contains %r -> %s"
                                % (lineno, banned, line.strip()))
        self.assertEqual(
            hits, [],
            "ONBOARDING.md must stay plain-language -- found banned "
            "word(s):\n" + "\n".join(hits))


# name -> the bold heading text it owns in ONBOARDING.md, WITHOUT the marker.
# The topic phrase alone, not the whole heading. Adding a catalogue tool with
# no entry here fails `test_every_catalogue_tool_is_mapped` immediately.
_TOOL_CONSENT_HEADINGS = {
    "danger-guard": "A safety net on file deletion",
    "worktree-guard": "A second check on which project copy work lands in",
    "secret-scan": "A check that stops passwords reaching GitHub",
    "repo-freshness": "A heads-up when a project you follow moves ahead",
    "tooling-digest": "A short summary at the start of every conversation",
    "memory-routing": "A nudge when a note lands in the wrong place",
    "build-cost": "A running record of what work actually costs",
    "base-pin": "A heads-up when the shared version moves ahead of this worktree",
    "session-claim": "One session per folder",
    "session-titler": "Named sessions",
    "thread-handover": "Clean handoffs on long conversations",
    "usage-watch": "A live look at your usage budget",
    "caffeinate": "Keeping your Mac awake for a long build",
}

# The consent marker each tier's heading must carry on its own line.
_TIER_MARKERS = {1: "always on", 2: "if you say yes", 3: "off unless you asked for it"}


def _missing_consent_markers(tool_headings, tool_tiers, onboarding_text):
    """[name, ...] for every tool in `tool_headings` (name -> the bold
    heading text it owns in ONBOARDING.md, without the marker) whose heading
    line does not carry the marker for its tier (`_TIER_MARKERS`), or whose
    heading cannot be found in `onboarding_text` at all.

    Matched line by line, deliberately, rather than against the whole file
    at once: the marker has to share the heading's OWN line with the topic
    phrase, not merely appear somewhere later in the block, and it has to be
    the marker for the tool's own tier, not any tier's.
    """
    missing = []
    lines = onboarding_text.splitlines()
    for name, heading in tool_headings.items():
        line = next((candidate for candidate in lines if heading in candidate), None)
        if line is None or _TIER_MARKERS[tool_tiers[name]] not in line:
            missing.append(name)
    return missing


class TestConsentMarkersEnforced(unittest.TestCase):
    """Pins that each catalogue tool's ONBOARDING.md heading carries the
    consent marker for its own tier, since a marker quietly dropped or
    swapped leaves every coverage check green while a declinable tool reads
    like a guarantee.
    """

    def test_every_catalogue_tool_is_mapped(self):
        # The forcing function: a new tool with no entry here fails right
        # away, rather than shipping an unmarked heading nothing would flag.
        catalogue_names = {tool["name"] for group in setup_config.TOOL_CATALOGUE
                           for tool in group["tools"]}
        self.assertEqual(catalogue_names, set(_TOOL_CONSENT_HEADINGS))

    def test_onboarding_md_headings_all_carry_their_tier_marker(self):
        repo_root = setup_config.resolve_repo_root()
        if not repo_root:
            self.skipTest("could not resolve repo root")
        doc_path = os.path.join(repo_root, "ONBOARDING.md")
        with open(doc_path, encoding="utf-8") as handle:
            onboarding_text = handle.read()
        tiers = {tool["name"]: tool["tier"] for group in setup_config.TOOL_CATALOGUE
                 for tool in group["tools"]}

        missing = _missing_consent_markers(_TOOL_CONSENT_HEADINGS, tiers, onboarding_text)
        self.assertEqual(
            missing, [],
            "tool heading(s) with no tier marker in ONBOARDING.md: " +
            ", ".join(missing))

    def test_catches_a_heading_with_its_marker_stripped(self):
        # The fixture, not ONBOARDING.md, is the ground truth.
        headings = {
            "danger-guard": "A safety net on file deletion",
            "session-claim": "One session per folder",
        }
        tiers = {"danger-guard": 1, "session-claim": 2}
        text = ("**A safety net on file deletion, always on.** Text.\n\n"
                "**One session per folder.** Text.\n")
        missing = _missing_consent_markers(headings, tiers, text)
        self.assertEqual(missing, ["session-claim"])

    def test_the_marker_must_match_the_tier_not_just_exist(self):
        # Tier 2's heading carries tier 1's marker: a marker is present, but
        # it is the wrong one.
        headings = {
            "danger-guard": "A safety net on file deletion",
            "session-claim": "One session per folder",
        }
        tiers = {"danger-guard": 1, "session-claim": 2}
        text = ("**A safety net on file deletion, always on.** x\n\n"
                "**One session per folder, always on.** y\n")
        missing = _missing_consent_markers(headings, tiers, text)
        self.assertEqual(missing, ["session-claim"])


class TestOnboardNeverClobbersAContextFile(unittest.TestCase):
    """`/onboard` copies `CLAUDE.md.template` and `TOOLING.md.template` into the
    user's workspace root. A bare `cp` OVERWRITES, and `/onboard` is a
    re-runnable path -- so on a machine with a mature `CLAUDE.md` or `TOOLING.md`
    that one line destroys it. Both files are unversioned by design, so there is
    no recovery.

    Prose alone is not the fix: with a bare fenced command and a "check first"
    paragraph after it, anyone -- human or Claude -- skimming the fence still
    runs the destructive form, so the guarantee lives entirely in whether the
    reader keeps reading.

    Here the command itself is non-clobbering, and this test is what holds it
    that way. The prose still matters and is still there: `cp -n` alone would
    silently skip on a re-run, which loses every template improvement since they
    onboarded. `-n` removes the DESTRUCTIVE outcome; the prose is what turns a
    skip into a merge.

    The scan covers every text file in the repo, not just the onboard skill:
    `install.sh` prints a `cp` of the template for the user to paste when no
    CLAUDE.md exists, and a reader pastes that line exactly as printed, so it
    destroys a file just as the skill's command would. Test and fixture
    directories are skipped because they hold deliberate bare-`cp` samples.
    """

    SKIPPED_DIRS = (".git", "__pycache__", "node_modules", "tests", "fixtures")

    def onboard_text(self):
        """Every text file under the repo that could tell a reader to copy a
        template, concatenated."""
        chunks = []
        for root, dirs, files in os.walk(REPO_ROOT):
            dirs[:] = [d for d in dirs
                       if d not in self.SKIPPED_DIRS and not d.startswith(".venv")]
            for name in sorted(files):
                try:
                    with open(os.path.join(root, name), encoding="utf-8") as handle:
                        chunks.append(handle.read())
                except UnicodeDecodeError:
                    continue
        return "\n".join(chunks)

    def test_the_parser_found_the_copies_at_all(self):
        # A regex that matches nothing makes the assertion below pass
        # vacuously, which is the failure mode this file exists to refuse.
        found = _TEMPLATE_CP.findall(self.onboard_text())
        self.assertGreaterEqual(
            len(found), 3,
            "expected at least the two onboard-skill template copies and the "
            "one install.sh prints; found %d -- the regex, the phases or the "
            "walk changed" % len(found))

    def test_no_template_copy_can_overwrite(self):
        bad = clobbering_template_copies(self.onboard_text())
        self.assertEqual(
            bad, [],
            "these template copies can destroy a real file on a re-run; "
            "use `cp -n`: %r" % (bad,))

    def test_the_check_actually_catches_a_bare_cp(self):
        # Positive control against a fixture, never against the real file: a
        # check whose ground truth is the thing it verifies proves nothing.
        fixture = "```bash\ncp CLAUDE.md.template ~/root/CLAUDE.md\n```\n"
        self.assertEqual(
            clobbering_template_copies(fixture),
            [("CLAUDE.md.template", "")],
            "the predicate did not flag a bare template cp, so a green run "
            "above would mean nothing")

    def test_the_check_accepts_the_guarded_form(self):
        fixture = "```bash\ncp -n TOOLING.md.template ~/root/TOOLING.md\n```\n"
        self.assertEqual(clobbering_template_copies(fixture), [])

    def test_the_check_catches_a_bare_cp_printed_from_a_shell_string(self):
        # The line-anchored pattern matched nothing here, which is how
        # install.sh's printed advice shipped unguarded.
        fixture = '  warn "   cp \\"$REPO/CLAUDE.md.template\\" \\"$WS/CLAUDE.md\\""\n'
        self.assertEqual(
            clobbering_template_copies(fixture),
            [("\\\"$REPO/CLAUDE.md.template", "")],
            "the predicate did not flag a bare cp inside a printed shell "
            "string, so install.sh's advice would go unchecked")

    def test_the_check_accepts_the_guarded_shell_string_form(self):
        fixture = '  warn "   cp -n \\"$REPO/CLAUDE.md.template\\" \\"$WS/CLAUDE.md\\""\n'
        self.assertEqual(clobbering_template_copies(fixture), [])


class TestExistingConfigIsDecodedAsUtf8(unittest.TestCase):
    """load_existing_config() must not open the config with the ambient
    locale's encoding. Under a non-UTF-8 locale (cp1252 on Windows, ASCII
    under LC_ALL=C) any non-ASCII value raised a UnicodeDecodeError, which
    load_existing_config() catches as a ValueError and reports as "could not
    parse" -- indistinguishable from an actually-corrupt config.

    A subprocess is required because the locale is fixed at interpreter
    start, so an in-process call cannot exercise it independently of this
    test's own encoding.
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
            "import setup_config\n"
            "print(json.dumps(setup_config.load_existing_config(%r)))\n"
        ) % (self.LIB_DIR, self.config_path)
        proc = subprocess.run(
            ["python3", "-c", script], capture_output=True, text=True,
            env=dict(os.environ, **env))
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
        ok, data = self._load_in_subprocess(self.NON_UTF8)
        self.assertTrue(ok)
        self.assertEqual(data["owner_name"], "Oʻahu Tester")

    def test_same_config_loads_under_utf8(self):
        # Control for the fixture itself: the config must load correctly
        # when the locale is not in play.
        ok, data = self._load_in_subprocess({"PYTHONUTF8": "1"})
        self.assertTrue(ok)
        self.assertEqual(data["owner_name"], "Oʻahu Tester")



# --- Config maintenance: --set, --migrate, --report, --accept-core ----------

import subprocess  # noqa: E402

SCRIPT = os.path.join(LIB, "setup_config.py")


class _ConfigFile(unittest.TestCase):
    """A throwaway config path plus helpers. Every refusal test compares raw
    bytes, never just the exit code: a refused call that quietly rewrote the
    file with identical values would still pass on returncode alone."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        self.config = os.path.join(self.dir, "config.json")

    def run_cli(self, *args):
        return subprocess.run(["python3", SCRIPT, self.config, REPO_ROOT] + list(args),
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def run_update(self, *extra):
        return self.run_cli("--update", *extra)

    def write_raw(self, data):
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")

    def read(self):
        with open(self.config, encoding="utf-8") as handle:
            return json.load(handle)

    def read_bytes(self):
        with open(self.config, "rb") as handle:
            return handle.read()

    def tempdir(self):
        path = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(path, ignore_errors=True))
        return path


class TestUpdateSet(_ConfigFile):
    """`--update --set PATH=VALUE`: one validated key. config.example.json's
    own value is the type contract; KEY_VALIDATORS mirror readers that
    silently discard some type-valid values."""

    def test_repo_root_rejects_a_nonexistent_directory(self):
        self.write_raw({"owner_name": "Sam"})
        before = self.read_bytes()
        proc = self.run_update("--set", "repo_freshness.sync.repo_root=/does/not/exist")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.read_bytes(), before)

    def test_throttle_hours_rejects_zero(self):
        self.write_raw({"owner_name": "Sam"})
        before = self.read_bytes()
        proc = self.run_update("--set", "repo_freshness.sync.throttle_hours=0")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.read_bytes(), before)

    def test_repo_root_stores_the_shortened_realpath(self):
        target = self.tempdir()
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--set", "repo_freshness.sync.repo_root=%s" % target)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read()["repo_freshness"]["sync"]["repo_root"],
                         setup_config.shorten(os.path.realpath(target)))

    def test_set_writes_two_keys_and_every_other_key_survives(self):
        target = self.tempdir()
        original = {
            "owner_name": "Sam",
            "workspace_root": "/Users/sam/Work",
            "repo_freshness": {"sync": {"enabled": True, "gh_owner": "sam"}},
        }
        self.write_raw(original)
        proc = self.run_update(
            "--set", "repo_freshness.sync.repo_root=%s" % target,
            "--set", "repo_freshness.sync.throttle_hours=12")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.read()
        sync = data["repo_freshness"]["sync"]
        self.assertEqual(sync["repo_root"], setup_config.shorten(os.path.realpath(target)))
        self.assertEqual(sync["throttle_hours"], 12)
        self.assertEqual(sync["enabled"], True)
        self.assertEqual(sync["gh_owner"], "sam")
        self.assertEqual(data["owner_name"], "Sam")
        self.assertEqual(data["workspace_root"], "/Users/sam/Work")

    def test_refusals_exit_3_and_leave_the_file_byte_identical(self):
        cases = [
            'repo_freshness.sync.enabled="true"',          # string for a bool
            "repo_freshness.sync.throttle_hours=true",     # bool for an int
            "repo_freshness.sync.throttle_hours=1.5",      # float for an int
            "repo_freshness.sync=%s" % json.dumps({}),     # a section
            "repo_freshness.nope=1",                       # no such key
            "repo_freshness.enabled=false",                # use --disable
            "repo_freshness.installed=false",              # retired key
            "danger_guard.extra_safe_roots=%s" % json.dumps(["~"]),
            "danger_guard.extra_safe_roots=%s" % json.dumps(["~/Dropbox/Work"]),
            "worktree_guard.canonical_root=/does/not/exist",
            "memory_lint.max_index_chars=25000",           # past the harness cut
            "caffeinate.battery_floor_percent=101",
            "context_budget.always_on_budget=0",
            'context_budget.always_on_budget="500"',
            "thread_handover.handover_dir=relative/dir",
            "spec_archive.specs_dir=%s" % os.path.join(REPO_ROOT, "specs"),
            "owner_name=Sam",                              # one segment
            "nope",                                        # no =
            'update.origin=""',                            # its reader refuses blank
            'update.origin="   "',
            "update.origin=null",
            "update.origin=5",
            "update.nope=x",                               # absent keys are named, not a section
        ]
        for case in cases:
            self.write_raw({"owner_name": "Sam"})
            before = self.read_bytes()
            proc = self.run_update("--set", case)
            self.assertEqual(proc.returncode, 3, "case %r: rc=%s err=%s"
                             % (case, proc.returncode, proc.stderr))
            self.assertEqual(self.read_bytes(), before, "case %r changed the file" % case)

    def test_enabled_refusal_names_enable_disable_flags(self):
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--set", "repo_freshness.enabled=false")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("--enable/--disable", proc.stderr)

    def test_installed_refusal_says_it_is_not_read(self):
        # An older copy's consent key: the refusal must say why, not just
        # "no such key", so a migrating user is pointed at the right flags.
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--set", "session_titler.installed=false")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("not read by this toolkit", proc.stderr)
        self.assertIn("--profiles", proc.stderr)

    def test_update_origin_is_settable_stripped_and_read_back(self):
        # config.example.json leaves `update` out of its live keys; `--set`
        # still writes it, and the reader `--update` uses accepts the result.
        self.write_raw({"owner_name": "Sam"})
        fork = "https://github.com/you/claude-code-toolkit"
        proc = self.run_update("--set", "update.origin=  %s " % fork)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.read()
        self.assertEqual(data["update"], {"origin": fork})
        self.assertEqual(data["owner_name"], "Sam")
        read_back = subprocess.run(
            ["python3", "-c", "import toolkit_update as t; print(t.configured_origin())"],
            cwd=os.path.join(REPO_ROOT, "lib"), capture_output=True, text=True,
            stdin=subprocess.DEVNULL,
            env=dict(os.environ, CLAUDE_TOOLKIT_CONFIG=self.config, PYTHONDONTWRITEBYTECODE="1"))
        self.assertEqual(read_back.returncode, 0, read_back.stderr)
        self.assertEqual(read_back.stdout.strip(), repr((fork, None)))

    def test_a_non_object_config_is_refused_untouched(self):
        with open(self.config, "w") as handle:
            handle.write("[1, 2]\n")
        before = self.read_bytes()
        proc = self.run_update("--disable", "session_titler")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("not a JSON object", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.read_bytes(), before)

    def test_valid_extra_safe_root_is_written_with_guard_setting_line(self):
        target = self.tempdir()
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--set",
                               "danger_guard.extra_safe_roots=%s" % json.dumps([target]))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("guard setting", proc.stdout)
        self.assertEqual(self.read()["danger_guard"]["extra_safe_roots"],
                         [setup_config.shorten(os.path.realpath(target))])

    def test_max_lines_refuses_a_value_its_reader_discards(self):
        self.write_raw({"owner_name": "Sam"})
        before = self.read_bytes()
        proc = self.run_update("--set", "tooling_digest.max_lines=-1")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(self.read_bytes(), before)
        proc = self.run_update("--set", "tooling_digest.max_lines=40")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read()["tooling_digest"]["max_lines"], 40)

    def test_a_null_example_int_key_takes_an_int_and_null(self):
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--set", "context_budget.always_on_budget=500")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read()["context_budget"]["always_on_budget"], 500)
        proc = self.run_update("--set", "context_budget.always_on_budget=null")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(self.read()["context_budget"]["always_on_budget"])

    def test_canonical_root_null_is_written_as_an_explicit_decision(self):
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--set", "worktree_guard.canonical_root=null")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        guard = self.read()["worktree_guard"]
        self.assertIn("canonical_root", guard)
        self.assertIsNone(guard["canonical_root"])

    def test_user_data_dir_outside_the_checkout_is_written(self):
        target = os.path.join(self.tempdir(), "handovers")
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--set", "thread_handover.handover_dir=%s" % target)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read()["thread_handover"]["handover_dir"],
                         setup_config.shorten(os.path.realpath(target)))


class TestUpdateSetCodexModel(_ConfigFile):
    """`codex_review.models.<gate>`: a model name or null, nothing else, so
    `--set` cannot store a value the wrapper would refuse at dispatch."""

    def test_a_model_name_is_written(self):
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--set", "codex_review.models.code=my-model")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read()["codex_review"]["models"]["code"], "my-model")

    def test_null_is_written_as_null(self):
        self.write_raw({"owner_name": "Sam", "codex_review": {"models": {"code": "x"}}})
        proc = self.run_update("--set", "codex_review.models.code=null")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        models = self.read()["codex_review"]["models"]
        self.assertIn("code", models)
        self.assertIsNone(models["code"])

    def test_every_gate_key_accepts_a_name(self):
        for gate in ("code", "merge", "plan", "design"):
            self.write_raw({"owner_name": "Sam"})
            proc = self.run_update("--set", "codex_review.models.%s=m-1" % gate)
            self.assertEqual(proc.returncode, 0, "gate %s: %s" % (gate, proc.stderr))
            self.assertEqual(self.read()["codex_review"]["models"][gate], "m-1")

    def test_unusable_values_are_refused_and_name_the_key(self):
        for value in ("", "a b", "-x", "true", "1"):
            self.write_raw({"owner_name": "Sam"})
            before = self.read_bytes()
            proc = self.run_update("--set", "codex_review.models.code=%s" % value)
            self.assertNotEqual(proc.returncode, 0, "value %r was accepted" % value)
            self.assertIn("codex_review.models.code", proc.stderr)
            self.assertEqual(self.read_bytes(), before, "value %r changed the file" % value)

    def test_a_key_that_is_not_a_gate_is_refused(self):
        self.write_raw({"owner_name": "Sam"})
        before = self.read_bytes()
        proc = self.run_update("--set", "codex_review.models.other=m-1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.read_bytes(), before)


class TestUpdateSetEnableWorkflows(_ConfigFile):
    """`build.enable_workflows` is type-checked against the example's leaf, so
    a JSON boolean is accepted and `yes` is refused with no validator code."""

    def test_the_example_leaf_is_boolean_false(self):
        with open(os.path.join(REPO_ROOT, "config.example.json"), encoding="utf-8") as handle:
            leaf = json.load(handle)["build"]["enable_workflows"]
        self.assertIs(leaf, False)

    def test_true_is_written_as_a_json_boolean(self):
        self.write_raw({"owner_name": "Sam"})
        proc = self.run_update("--set", "build.enable_workflows=true")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(self.read()["build"]["enable_workflows"], True)

    def test_yes_is_refused_and_the_file_is_untouched(self):
        self.write_raw({"owner_name": "Sam"})
        before = self.read_bytes()
        proc = self.run_update("--set", "build.enable_workflows=yes")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertEqual(self.read_bytes(), before)


class TestNoTtyWritesNothing(_ConfigFile):

    def test_setup_exits_2_with_no_tty_and_writes_nothing(self):
        proc = self.run_cli()          # stdin is /dev/null: not a TTY
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("no TTY", proc.stdout)
        self.assertFalse(os.path.exists(self.config))

    def test_the_example_names_no_live_path_a_copy_would_trust(self):
        # The installer copies the example verbatim when it cannot prompt; a
        # path here would widen the danger guard or point a file-moving tool
        # at a directory nobody chose.
        with open(os.path.join(REPO_ROOT, "config.example.json"), encoding="utf-8") as h:
            example = json.load(h)
        self.assertIsNone(example["workspace_root"])
        self.assertIsNone(example["spec_archive"]["specs_dir"])
        self.assertIsNone(example["worktree_guard"]["canonical_root"])

    def test_the_example_ships_no_project_code_roster(self):
        with open(os.path.join(REPO_ROOT, "config.example.json"), encoding="utf-8") as h:
            text = h.read().lower()
        for word in ("roster", "clickup", "1password", "clients_py"):
            self.assertNotIn(word, text)

    def test_the_example_records_no_install_decision_anywhere(self):
        # install.sh copies the example verbatim on a no-TTY install; it must
        # carry no `installed` key, which this toolkit never reads anyway.
        def keys(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    for inner in keys(value):
                        yield inner
        with open(os.path.join(REPO_ROOT, "config.example.json"), encoding="utf-8") as h:
            self.assertNotIn("installed", set(keys(json.load(h))))


class TestExistingConfigIsNeverOverwritten(_ConfigFile):
    """First-run setup refuses an existing config; only --replace (what
    `install.sh --setup` passes) redoes the questions."""

    EXISTING = b'{"owner_name": "Kept"}\n'

    def seed(self):
        with open(self.config, "wb") as handle:
            handle.write(self.EXISTING)

    def test_interactive_without_replace_refuses_before_the_tty_check(self):
        self.seed()
        before = os.stat(self.config).st_mtime_ns
        proc = self.run_cli()          # stdin is /dev/null: not a TTY
        # 3, not 2: the refusal comes before the no-TTY exit.
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("--update", proc.stderr)
        self.assertEqual(self.read_bytes(), self.EXISTING)
        self.assertEqual(os.stat(self.config).st_mtime_ns, before)

    def test_interactive_with_replace_and_no_tty_still_exits_2(self):
        self.seed()
        proc = self.run_cli("--replace")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("no TTY", proc.stdout)
        self.assertEqual(self.read_bytes(), self.EXISTING)

    def test_exclusive_write_raises_and_leaves_the_bytes(self):
        self.seed()
        with self.assertRaises(FileExistsError):
            setup_config.write_config(self.config, {"owner_name": "Clobber"},
                                      replace=False)
        self.assertEqual(self.read_bytes(), self.EXISTING)
        # The default still overwrites: --update depends on it.
        setup_config.write_config(self.config, {"owner_name": "New"})
        self.assertEqual(self.read(), {"owner_name": "New"})


class TestValidateUserDataDir(unittest.TestCase):

    def test_refusals(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        a_file = os.path.join(tmp, "f")
        open(a_file, "w").close()
        for value, reason in (
                ("", "empty"),
                ("rel/dir", "not an absolute path"),
                ("$TOOLKIT_TEST_SURELY_UNSET/x", "unset variable"),
                ("/", "filesystem root"),
                (HOME, "home directory"),
                (REPO_ROOT, "inside the toolkit checkout"),
                (os.path.join(REPO_ROOT, "lib", "x"), "inside the toolkit checkout"),
                (a_file, "not a directory")):
            ok, result = setup_config.validate_user_data_dir(value, REPO_ROOT)
            self.assertIs(ok, False, value)
            self.assertIn(reason, result, value)

    def test_a_sibling_whose_name_extends_the_checkout_is_not_inside_it(self):
        # Prefix, not containment: <repo>-data is outside <repo>.
        ok, _ = setup_config.validate_user_data_dir(REPO_ROOT + "-data", REPO_ROOT)
        self.assertIs(ok, True)

    def test_a_missing_directory_outside_the_checkout_is_accepted(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        ok, result = setup_config.validate_user_data_dir(os.path.join(tmp, "new"), REPO_ROOT)
        self.assertIs(ok, True)
        self.assertEqual(result, setup_config.shorten(
            os.path.join(os.path.realpath(tmp), "new")))


class TestUpdateMigrate(_ConfigFile):
    """`--update --migrate`: a retired section's values are copied into the
    successor, a successor value that differs is kept, nothing is removed."""

    def test_copies_every_mapped_key_and_keeps_the_retired_section(self):
        target = self.tempdir()
        self.write_raw({"owner_name": "Sam", "workspace_sync": {
            "enabled": True, "repo_root": target, "gh_owner": "me", "throttle_hours": 12}})
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("migrated", proc.stdout)
        data = self.read()
        sync = data["repo_freshness"]["sync"]
        self.assertEqual(sync["enabled"], True)
        # Copied byte for byte, not re-canonicalised by the validator, so the
        # report's comparison clears after a migration.
        self.assertEqual(sync["repo_root"], target)
        self.assertEqual(sync["gh_owner"], "me")
        self.assertEqual(sync["throttle_hours"], 12)
        self.assertEqual(data["workspace_sync"]["repo_root"], target)

    def test_skips_an_invalid_repo_root_and_copies_the_rest(self):
        self.write_raw({"owner_name": "Sam", "workspace_sync": {
            "enabled": True, "repo_root": "/does/not/exist", "gh_owner": "me",
            "throttle_hours": 12}})
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skipped", proc.stdout)
        sync = self.read()["repo_freshness"]["sync"]
        self.assertEqual(sync["enabled"], True)
        self.assertEqual(sync["gh_owner"], "me")
        self.assertEqual(sync["throttle_hours"], 12)
        self.assertNotIn("repo_root", sync)

    def test_skips_a_type_invalid_value(self):
        target = self.tempdir()
        self.write_raw({"owner_name": "Sam", "workspace_sync": {
            "enabled": "true", "repo_root": target, "gh_owner": "me", "throttle_hours": 12}})
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skipped", proc.stdout)
        sync = self.read()["repo_freshness"]["sync"]
        self.assertNotIn("enabled", sync)
        self.assertEqual(sync["repo_root"], target)

    def test_a_conflict_keeps_the_successors_value(self):
        target, other = self.tempdir(), self.tempdir()
        self.write_raw({"owner_name": "Sam",
                        "workspace_sync": {"enabled": True, "repo_root": target},
                        "repo_freshness": {"sync": {"repo_root": other}}})
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("kept", proc.stdout)
        sync = self.read()["repo_freshness"]["sync"]
        self.assertEqual(sync["repo_root"], other)
        self.assertEqual(sync["enabled"], True)

    def test_an_explicit_null_successor_wins(self):
        # gh_owner: null turns discovery off; a migration must not re-enable
        # it from the retired section, and the report must not nag about it.
        self.write_raw({"workspace_sync": {"gh_owner": "old-owner"},
                        "repo_freshness": {"sync": {"gh_owner": None}}})
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIsNone(self.read()["repo_freshness"]["sync"]["gh_owner"])
        self.assertIn("kept", proc.stdout)
        self.assertEqual(setup_config.config_report(self.read(), REPO_ROOT), [])

    def test_nothing_to_migrate_prints_the_line_and_writes_nothing(self):
        self.write_raw({"owner_name": "Sam"})
        before = self.read_bytes()
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nothing to migrate", proc.stdout)
        self.assertEqual(self.read_bytes(), before)

    def test_a_second_migrate_changes_nothing(self):
        target = self.tempdir()
        self.write_raw({"owner_name": "Sam",
                        "workspace_sync": {"enabled": True, "repo_root": target},
                        "usage_watch": {"installed": False}})
        self.assertEqual(self.run_update("--migrate").returncode, 0)
        after_first = self.read_bytes()
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nothing to migrate", proc.stdout)
        self.assertEqual(self.read_bytes(), after_first)


class TestRecordedConsent(unittest.TestCase):
    """The older copy's install decision is three-state; None is never False."""

    def test_states(self):
        rc = setup_config.recorded_consent
        self.assertIs(rc({"x": {"installed": True}}, "x"), True)
        self.assertIs(rc({"x": {"installed": False}}, "x"), False)
        for config in ({}, {"x": {}}, {"x": {"installed": None}},
                       {"x": {"installed": "false"}}, {"x": {"installed": 0}},
                       {"x": "not a dict"}):
            self.assertIsNone(rc(config, "x"), config)


class TestDeclinedStatesStayDeclined(_ConfigFile):
    """An older config's `installed: false` must not become "on" here, where
    nothing reads `installed` (plan section B, the P table)."""

    def test_every_catalogue_section_has_a_declared_successor(self):
        # A new catalogue tool needs a decision about what an old refusal of
        # it means; without one it would silently fall through.
        lookup = setup_config.catalogue_lookup()
        sections = {tool["section"] for tool in lookup.values() if tool["section"]}
        self.assertTrue(sections)
        self.assertEqual(sections - set(setup_config.DECLINED_SUCCESSORS), set())

    def test_every_enabled_kind_is_a_default_on_section_of_the_example(self):
        with open(os.path.join(REPO_ROOT, "config.example.json"), encoding="utf-8") as h:
            example = json.load(h)
        for section, kind in setup_config.DECLINED_SUCCESSORS.items():
            if kind == "enabled":
                self.assertIs(example.get(section, {}).get("enabled"), True, section)

    def test_migrate_disables_a_declined_default_on_tool(self):
        self.write_raw({"owner_name": "Sam", "usage_watch": {"installed": False},
                        "session_titler": {"installed": True}})
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.read()
        self.assertIs(data["usage_watch"]["enabled"], False)
        self.assertIs(data["usage_watch"]["installed"], False)   # never removed
        self.assertNotIn("enabled", data["session_titler"])      # consent, not a refusal
        self.assertIn("disabled", proc.stdout)

    def test_an_explicit_enabled_true_is_kept_and_reported(self):
        self.write_raw({"usage_watch": {"installed": False, "enabled": True}})
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(self.read()["usage_watch"]["enabled"], True)
        self.assertIn("kept", proc.stdout)
        warnings = setup_config.config_report(self.read(), REPO_ROOT)
        self.assertTrue(any("usage_watch" in w and "--disable" in w for w in warnings),
                        warnings)

    def test_report_names_an_unhonoured_decline_until_migrated(self):
        self.write_raw({"usage_watch": {"installed": False}})
        before = setup_config.config_report(self.read(), REPO_ROOT)
        self.assertEqual(len(before), 1, before)
        self.assertIn("usage_watch", before[0])
        self.assertIn("--migrate", before[0])
        self.assertEqual(self.run_update("--migrate").returncode, 0)
        self.assertEqual(setup_config.config_report(self.read(), REPO_ROOT), [])

    def test_default_off_and_manual_declines_need_nothing(self):
        config = {"caffeinate": {"installed": False}, "git_tidy": {"installed": False},
                  "toolkit_sync": {"installed": False}}
        self.assertEqual(setup_config.config_report(config, REPO_ROOT), [])
        self.assertEqual(setup_config.unhonoured_declines(config), [])


class TestCoreConflicts(_ConfigFile):
    """A declined always-on guard cannot be honoured, so it is never silently
    overridden: the report names it and only --accept-core records the new
    choice."""

    def test_core_conflicts_lists_only_declined_guards(self):
        config = {"danger_guard": {"installed": False},
                  "secret_scan": {"installed": None},
                  "worktree_guard": {"installed": True}}
        self.assertEqual(setup_config.core_conflicts(config), ["danger_guard"])

    def test_report_names_the_conflict_and_both_ways_out(self):
        warnings = setup_config.config_report({"secret_scan": {"installed": False}},
                                              REPO_ROOT)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("secret_scan", warnings[0])
        self.assertIn("--accept-core", warnings[0])
        self.assertIn("skills only", warnings[0])

    def test_migrate_never_resolves_a_core_conflict(self):
        self.write_raw({"danger_guard": {"installed": False}})
        before = self.read_bytes()
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("needs a choice", proc.stdout)
        self.assertEqual(self.read_bytes(), before)

    def test_accept_core_records_the_choice_and_clears_the_report(self):
        self.write_raw({"danger_guard": {"installed": False, "extra_safe_roots": []},
                        "secret_scan": {"installed": False}})
        proc = self.run_update("--accept-core")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = self.read()
        self.assertIs(data["danger_guard"]["installed"], True)
        self.assertIs(data["secret_scan"]["installed"], True)
        self.assertEqual(data["danger_guard"]["extra_safe_roots"], [])
        self.assertIn("core accepted  : danger_guard", proc.stdout)
        self.assertEqual(setup_config.core_conflicts(data), [])
        self.assertEqual(setup_config.config_report(data, REPO_ROOT), [])

    def test_accept_core_with_nothing_declined_writes_nothing(self):
        self.write_raw({"owner_name": "Sam"})
        before = self.read_bytes()
        proc = self.run_update("--accept-core")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nothing to accept", proc.stdout)
        self.assertEqual(self.read_bytes(), before)


class TestInferredCanonicalRoot(_ConfigFile):
    """An older worktree guard inferred its canonical root from
    workspace_root; this toolkit never does. The report shows the root it
    would have used and the two explicit commands, and writes nothing."""

    def workspace(self, with_repos):
        root = self.tempdir()
        if with_repos:
            os.makedirs(os.path.join(root, "repos"))
        return root

    def test_prefers_repos_when_it_exists(self):
        root = self.workspace(True)
        self.assertEqual(setup_config.inferred_canonical_root({"workspace_root": root}),
                         setup_config.shorten(os.path.realpath(os.path.join(root, "repos"))))

    def test_falls_back_to_the_workspace_root(self):
        root = self.workspace(False)
        self.assertEqual(setup_config.inferred_canonical_root({"workspace_root": root}),
                         setup_config.shorten(os.path.realpath(root)))

    def test_nothing_when_the_key_is_present_even_as_null(self):
        root = self.workspace(True)
        for guard in ({"canonical_root": None}, {"canonical_root": root}):
            self.assertIsNone(setup_config.inferred_canonical_root(
                {"workspace_root": root, "worktree_guard": guard}), guard)

    def test_nothing_without_a_usable_workspace_root(self):
        for config in ({}, {"workspace_root": ""}, {"workspace_root": "/does/not/exist"},
                       {"workspace_root": "$TOOLKIT_TEST_SURELY_UNSET/w"},
                       {"workspace_root": "relative/w"}):
            self.assertIsNone(setup_config.inferred_canonical_root(config), config)

    def test_report_shows_the_root_and_both_commands_and_writes_nothing(self):
        root = self.workspace(True)
        self.write_raw({"workspace_root": root})
        before = self.read_bytes()
        proc = self.run_cli("--report")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        expected = setup_config.shorten(os.path.realpath(os.path.join(root, "repos")))
        self.assertIn("worktree_guard.canonical_root=%s" % expected, proc.stdout)
        self.assertIn("worktree_guard.canonical_root=null", proc.stdout)
        self.assertEqual(self.read_bytes(), before)

    def test_the_printed_set_command_clears_the_report(self):
        root = self.workspace(True)
        self.write_raw({"workspace_root": root})
        expected = setup_config.shorten(os.path.realpath(os.path.join(root, "repos")))
        proc = self.run_update("--set", "worktree_guard.canonical_root=%s" % expected)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.run_cli("--report").stdout, "")

    def test_migrate_never_writes_the_inferred_root(self):
        root = self.workspace(True)
        self.write_raw({"workspace_root": root})
        before = self.read_bytes()
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read_bytes(), before)


class TestInferredSyncRoot(_ConfigFile):
    """P15: an older repo-freshness sync phase pulled in <workspace_root>/repos
    when no repo_root was set; this toolkit never derives where to write. The
    report names the directory and the two explicit commands, and writes
    nothing, so an intentional root migrates only by choice."""

    def sync_config(self, root, **sync):
        sync.setdefault("enabled", True)
        return {"workspace_root": root, "repo_freshness": {"sync": sync}}

    def workspace(self):
        root = self.tempdir()
        os.makedirs(os.path.join(root, "repos"))
        return root

    def expected(self, root):
        return setup_config.shorten(os.path.realpath(os.path.join(root, "repos")))

    def test_sync_on_with_no_repo_root_names_the_old_fallback(self):
        root = self.workspace()
        self.assertEqual(setup_config.inferred_sync_root(self.sync_config(root)),
                         self.expected(root))

    def test_only_a_literal_true_relied_on_the_fallback(self):
        root = self.workspace()
        for enabled in (False, None, "true", 1):
            self.assertIsNone(setup_config.inferred_sync_root(
                self.sync_config(root, enabled=enabled)), enabled)
        self.assertIsNone(setup_config.inferred_sync_root({"workspace_root": root}))

    def test_nothing_when_repo_root_is_present_even_as_null(self):
        root = self.workspace()
        for value in (None, root):
            self.assertIsNone(setup_config.inferred_sync_root(
                self.sync_config(root, repo_root=value)), value)

    def test_nothing_when_a_retired_repo_root_will_migrate(self):
        root = self.workspace()
        config = self.sync_config(root)
        config["workspace_sync"] = {"repo_root": root}
        self.assertIsNone(setup_config.inferred_sync_root(config))

    def test_nothing_when_the_fallback_directory_does_not_exist(self):
        # The old phase was inert there too: no repos/, and never the bare root.
        root = self.tempdir()
        os.makedirs(os.path.join(root, "some-clone", ".git"))
        self.assertIsNone(setup_config.inferred_sync_root(self.sync_config(root)))
        for workspace in ("", "/does/not/exist", "relative/w"):
            self.assertIsNone(setup_config.inferred_sync_root(
                self.sync_config(workspace)), workspace)

    def test_report_shows_the_root_and_both_commands_and_writes_nothing(self):
        root = self.workspace()
        self.write_raw(self.sync_config(root))
        before = self.read_bytes()
        proc = self.run_cli("--report")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("repo_freshness.sync.repo_root=%s" % self.expected(root), proc.stdout)
        self.assertIn("repo_freshness.sync.repo_root=null", proc.stdout)
        self.assertEqual(self.read_bytes(), before)

    def test_the_printed_set_command_clears_the_report(self):
        root = self.workspace()
        self.write_raw(dict(self.sync_config(root),
                            worktree_guard={"canonical_root": None}))
        proc = self.run_update(
            "--set", "repo_freshness.sync.repo_root=%s" % self.expected(root))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.run_cli("--report").stdout, "")

    def test_migrate_never_writes_the_inferred_root(self):
        root = self.workspace()
        self.write_raw(self.sync_config(root))
        before = self.read_bytes()
        proc = self.run_update("--migrate")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read_bytes(), before)


class TestConfigReport(_ConfigFile):
    """`config_report()` and `--report`: never empty for an absent or
    unparseable config, since an empty report is what the installer reads as
    healthy."""

    def test_absent_config_reports_no_config(self):
        proc = self.run_cli("--report")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("no config at", proc.stdout)

    def test_unparseable_config_reports_could_not_be_parsed(self):
        for raw in (b"{ not json", b"[1, 2]"):
            with open(self.config, "wb") as handle:
                handle.write(raw)
            proc = self.run_cli("--report")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("could not be parsed", proc.stdout, raw)

    def test_unmigrated_workspace_sync_produces_one_warning(self):
        target = self.tempdir()
        warnings = setup_config.config_report(
            {"workspace_sync": {"enabled": True, "repo_root": target,
                                "gh_owner": "me", "throttle_hours": 12}}, REPO_ROOT)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("workspace_sync", warnings[0])

    def test_the_printed_command_names_the_real_config_path(self):
        self.write_raw({"workspace_sync": {"gh_owner": "me"}})
        proc = self.run_cli("--report")
        self.assertIn(setup_config.shorten(self.config), proc.stdout)
        self.assertNotIn("<config>", proc.stdout)

    def test_report_clears_after_the_migrate_cli(self):
        target = self.tempdir()
        self.write_raw({"workspace_sync": {"enabled": True, "repo_root": target,
                                           "gh_owner": "me", "throttle_hours": 12}})
        self.assertEqual(self.run_update("--migrate").returncode, 0)
        proc = self.run_cli("--report")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_unknown_section_produces_one_warning(self):
        warnings = setup_config.config_report({"foo_tool": {"enabled": True}}, REPO_ROOT)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("foo_tool", warnings[0])

    def test_documented_top_level_keys_are_known(self):
        # owner_name and profiles appear in the example only as `//` notes.
        config = {"owner_name": "Sam", "profiles": ["core"], "workspace_root": "/w",
                  "worktree_guard": {"canonical_root": None}}
        self.assertEqual(setup_config.config_report(config, REPO_ROOT), [])

    def test_an_old_section_with_more_than_a_decision_is_still_unknown(self):
        warnings = setup_config.config_report(
            {"git_tidy": {"installed": True, "alias": "x"}}, REPO_ROOT)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("git_tidy", warnings[0])

    def test_example_config_reports_nothing(self):
        with open(os.path.join(REPO_ROOT, "config.example.json"), encoding="utf-8") as h:
            self.assertEqual(setup_config.config_report(json.load(h), REPO_ROOT), [])

    def test_a_freshly_written_config_reports_nothing(self):
        workspace = self.tempdir()
        os.makedirs(os.path.join(workspace, "repos"))
        proc = self.run_cli("--write", "--name", "Sam", "--workspace-root", workspace,
                            "--handover-dir", os.path.join(self.dir, "h"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(setup_config.config_report(self.read(), REPO_ROOT), [])


class TestRetiredAndValidatorTables(unittest.TestCase):
    """The tables must describe real config shape."""

    def example(self):
        with open(os.path.join(REPO_ROOT, "config.example.json"), encoding="utf-8") as h:
            return json.load(h)

    def test_every_successor_path_and_key_is_a_real_leaf(self):
        example = self.example()
        for old_name, info in setup_config.RETIRED_SECTIONS.items():
            for key in info["keys"]:
                value, error = setup_config._resolve_example_leaf(
                    example, info["successor"].split(".") + [key])
                self.assertIsNone(error, "%s -> %s.%s" % (old_name, info["successor"], key))

    def test_no_retired_name_is_a_top_level_example_key(self):
        example = self.example()
        for old_name in setup_config.RETIRED_SECTIONS:
            self.assertNotIn(old_name, example)

    def _leaves(self, node, path):
        found = []
        if isinstance(node, dict):
            for key, value in node.items():
                if not key.startswith("//"):
                    found.extend(self._leaves(value, path + [key]))
        else:
            found.append((".".join(path), node))
        return found

    def test_every_numeric_key_is_validated_or_declared(self):
        # Each numeric leaf refuses what its reader would discard, or is
        # declared type-only with the reader named. A new numeric key with
        # neither fails here instead of `--set` reporting success on a value
        # its reader ignores.
        numeric = [path for path, value in self._leaves(self.example(), [])
                   if isinstance(value, int) and not isinstance(value, bool)]
        numeric += list(setup_config.NULL_EXAMPLE_TYPES)
        self.assertTrue(numeric)
        for leaf in numeric:
            in_validators = leaf in setup_config.KEY_VALIDATORS
            in_type_only = leaf in setup_config.TYPE_ONLY_KEYS
            self.assertTrue(in_validators or in_type_only, leaf)
            self.assertFalse(in_validators and in_type_only, leaf)

    def test_every_table_key_is_a_real_leaf(self):
        example = self.example()
        for table in (setup_config.KEY_VALIDATORS, setup_config.TYPE_ONLY_KEYS,
                      setup_config.NULL_EXAMPLE_TYPES):
            for path in table:
                if path in setup_config.ABSENT_EXAMPLE_KEYS:
                    continue  # documented-but-absent: the next test holds those
                _, error = setup_config._resolve_example_leaf(example, path.split("."))
                self.assertIsNone(error, path)

    def test_absent_example_keys_are_absent_and_documented(self):
        # Absent from the live keys (a copied example must not pin them), and
        # named in their section's `//` comment, so the exception is the
        # example's own documented state rather than a hidden key.
        example = self.example()
        self.assertTrue(setup_config.ABSENT_EXAMPLE_KEYS)
        for path in setup_config.ABSENT_EXAMPLE_KEYS:
            section, _, key = path.partition(".")
            _, error = setup_config._resolve_example_leaf(example, path.split("."))
            self.assertIsNotNone(error, path)
            comment = " ".join(example.get("//" + section, []))
            self.assertIn('"%s": {"%s":' % (section, key), comment, path)
            self.assertIn("--set " + path, comment, path)

    def test_null_example_types_name_null_leaves(self):
        example = self.example()
        for path in setup_config.NULL_EXAMPLE_TYPES:
            value, _ = setup_config._resolve_example_leaf(example, path.split("."))
            self.assertIsNone(value, path)


if __name__ == "__main__":
    unittest.main()
