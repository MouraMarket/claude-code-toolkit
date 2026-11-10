#!/usr/bin/env python3
"""Tests for team_policy.py: the toolkit-policy import line in ~/.claude/CLAUDE.md.

Hermetic: every test builds its own home, checkout and workspace in a temp
directory and passes a stand-in config; none reads the real `~` or config.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

LIB = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, LIB)
import team_policy as tp


class FakeConfig:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, *keys, default=None):
        cur = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


class RaisingConfig:
    def get(self, *keys, default=None):
        raise RuntimeError("boom")


class Base(unittest.TestCase):
    checkout_name = "checkout"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.realpath(self.tmp.name)
        self.root = root
        self.home = os.path.join(root, "home")
        os.makedirs(self.home)
        self.checkout = os.path.join(root, self.checkout_name)
        os.makedirs(os.path.join(self.checkout, "policy"))
        with open(os.path.join(self.checkout, "policy", "CLAUDE.md"), "w") as f:
            f.write("# policy\n")
        self.cfg = FakeConfig()
        self.file = tp.user_file(self.home)

    def write_user(self, text):
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def read_user(self):
        with open(self.file, "rb") as f:
            return f.read()

    def status(self, cfg=None):
        return tp.status(self.checkout, self.home, cfg or self.cfg)

    def ensure(self, cfg=None):
        return tp.ensure(self.checkout, self.home, cfg or self.cfg)

    def make_workspace(self, headings):
        ws = os.path.join(self.root, "ws")
        os.makedirs(ws, exist_ok=True)
        with open(os.path.join(ws, "CLAUDE.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(headings) + "\n")
        return FakeConfig({"workspace_root": ws}), os.path.join(ws, "CLAUDE.md")


class StatusStatesTestCase(Base):
    def test_missing_when_no_file(self):
        self.assertEqual(self.status()[0], "missing")

    def test_missing_when_file_has_no_ours_import(self):
        self.write_user("hello\n@~/other/notes.md\n")
        state, path, _ = self.status()
        self.assertEqual((state, path), ("missing", self.file))

    def test_ok(self):
        self.write_user(tp.import_line(self.checkout) + "\n")
        state, path, detail = self.status()
        self.assertEqual((state, path), ("ok", self.file))
        self.assertIn(tp.policy_path(self.checkout), detail)

    def test_dangling_for_missing_target(self):
        self.write_user("@" + os.path.join(self.root, "gone", "policy", "CLAUDE.md") + "\n")
        state, _, detail = self.status()
        self.assertEqual(state, "dangling")
        self.assertIn("does not exist", detail)

    def test_dangling_for_existing_file_in_other_checkout(self):
        other = os.path.join(self.root, "other")
        os.makedirs(os.path.join(other, "policy"))
        with open(os.path.join(other, "policy", "CLAUDE.md"), "w") as f:
            f.write("x\n")
        self.write_user(tp.import_line(other) + "\n")
        state, _, detail = self.status()
        self.assertEqual(state, "dangling")
        self.assertIn("is not this checkout's policy", detail)

    def test_opted_out(self):
        cfg = FakeConfig({"team_policy": {"import": False}})
        state, path, detail = self.status(cfg)
        self.assertEqual((state, path), ("opted-out", self.file))
        self.assertEqual(detail, "team_policy.import is false")

    def test_error_on_undecodable_file(self):
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file, "wb") as f:
            f.write(b"\xff\xfe\x00bad")
        self.assertEqual(self.status()[0], "error")

    def test_states_constant_covers_all(self):
        self.assertEqual(set(tp.STATES),
                         {"ok", "missing", "dangling", "legacy-copy", "opted-out", "error"})


class LegacyCopyTestCase(Base):
    def test_fires_with_two_sentinels(self):
        cfg, wsfile = self.make_workspace(["## " + h for h in tp.SENTINEL_HEADINGS[:2]])
        self.write_user(tp.import_line(self.checkout) + "\n")
        state, path, detail = self.status(cfg)
        self.assertEqual((state, path), ("legacy-copy", wsfile))
        self.assertIn("carries 2 toolkit-policy sections", detail)
        self.assertIn(tp.SENTINEL_HEADINGS[0], detail)

    def test_fires_at_any_heading_level(self):
        # An older copied template put "Gate at decisions" at level 1 and
        # "Usage budget" at level 2; the policy has both at level 2.
        cfg, wsfile = self.make_workspace(["# " + tp.SENTINEL_HEADINGS[0],
                                           "### " + tp.SENTINEL_HEADINGS[1]])
        self.write_user(tp.import_line(self.checkout) + "\n")
        self.assertEqual(self.status(cfg)[:2], ("legacy-copy", wsfile))

    def test_sentinel_text_that_is_not_a_heading_does_not_count(self):
        for lines in ([tp.SENTINEL_HEADINGS[0], "## " + tp.SENTINEL_HEADINGS[1]],
                      ["#" + tp.SENTINEL_HEADINGS[0], "## " + tp.SENTINEL_HEADINGS[1]],
                      ["- " + tp.SENTINEL_HEADINGS[0], "## " + tp.SENTINEL_HEADINGS[1]],
                      ["```", "## " + tp.SENTINEL_HEADINGS[0], "```",
                       "## " + tp.SENTINEL_HEADINGS[1]]):
            with self.subTest(lines=lines):
                cfg, _ = self.make_workspace(lines)
                self.write_user(tp.import_line(self.checkout) + "\n")
                self.assertEqual(self.status(cfg)[0], "ok")

    def test_not_with_one_sentinel(self):
        cfg, _ = self.make_workspace(["## " + tp.SENTINEL_HEADINGS[0]])
        self.write_user(tp.import_line(self.checkout) + "\n")
        self.assertEqual(self.status(cfg)[0], "ok")

    def test_not_reported_while_import_missing(self):
        cfg, _ = self.make_workspace(["## " + h for h in tp.SENTINEL_HEADINGS])
        self.assertEqual(self.status(cfg)[0], "missing")
        self.write_user("@" + os.path.join(self.root, "gone", "policy", "CLAUDE.md") + "\n")
        self.assertEqual(self.status(cfg)[0], "dangling")


class FenceAndHeadingTestCase(Base):
    def test_import_inside_nested_fence_is_not_a_good_import(self):
        self.write_user("````\n```\n" + tp.import_line(self.checkout) + "\n```\n````\n")
        self.assertEqual(self.status()[0], "missing")
        self.assertEqual(self.ensure()[0], "wrote")

    def test_legacy_copy_detail_names_every_copied_section(self):
        headings = ["# Toolkit policy"] + ["## " + h for h in tp.SENTINEL_HEADINGS] + [
            "# Contradiction handling",
            "# Model roles \u2014 Opus architects, Sonnet executes",
        ]
        with open(os.path.join(self.checkout, "policy", "CLAUDE.md"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(headings) + "\n")
        cfg, _ = self.make_workspace(headings[1:])
        self.write_user(tp.import_line(self.checkout) + "\n")
        state, _, detail = self.status(cfg)
        self.assertEqual(state, "legacy-copy")
        self.assertIn("carries 5 toolkit-policy sections", detail)
        for heading in headings[1:]:
            self.assertIn(heading.lstrip("# "), detail)
        self.assertNotIn("Toolkit policy", detail)
        self.assertIn("; ", detail)


class OptOutDetailTestCase(Base):
    off = FakeConfig({"team_policy": {"import": False}})

    def test_detail_says_still_loads_with_marker(self):
        self.write_user(tp.MARKER + "\n" + tp.import_line(self.checkout) + "\n")
        state, _, detail = self.status(self.off)
        self.assertEqual(state, "opted-out")
        self.assertIn("still imports the toolkit policy", detail)

    def test_detail_says_still_loads_with_bare_ours_import(self):
        self.write_user(tp.import_line(self.checkout) + "\n")
        self.assertIn("still imports the toolkit policy", self.status(self.off)[2])

    def test_detail_plain_when_no_import(self):
        self.write_user("just text\n")
        self.assertEqual(self.status(self.off)[2], "team_policy.import is false")

    def test_import_enabled_defaults(self):
        self.assertTrue(tp.import_enabled(None))
        self.assertTrue(tp.import_enabled(RaisingConfig()))
        self.assertTrue(tp.import_enabled(FakeConfig({"team_policy": {"import": "no"}})))
        self.assertFalse(tp.import_enabled(self.off))


class EnsureTestCase(Base):
    def test_creates_missing_file_and_status_ok(self):
        result, path, _ = self.ensure()
        self.assertEqual((result, path), ("wrote", self.file))
        self.assertEqual(self.read_user().decode(),
                         tp.MARKER + "\n" + tp.import_line(self.checkout) + "\n")
        self.assertEqual(self.status()[0], "ok")

    def test_second_run_is_unchanged_and_bytes_identical(self):
        self.ensure()
        first = self.read_user()
        self.assertEqual(self.ensure()[0], "unchanged")
        self.assertEqual(self.read_user(), first)

    def test_preexisting_lines_preserved_with_trailing_newline(self):
        original = "# mine\r\nline two  \n\n  indented\n"
        self.write_user(original)
        self.assertEqual(self.ensure()[0], "wrote")
        self.assertTrue(self.read_user().startswith(original.encode()))
        self.assertEqual(self.status()[0], "ok")

    def test_preexisting_lines_preserved_without_trailing_newline(self):
        original = "# mine\nno newline at end"
        self.write_user(original)
        self.assertEqual(self.ensure()[0], "wrote")
        self.assertTrue(self.read_user().startswith(original.encode() + b"\n"))
        self.assertEqual(self.status()[0], "ok")

    def test_refuses_symlink_and_touches_nothing(self):
        target = os.path.join(self.root, "real-claude.md")
        with open(target, "w") as f:
            f.write("target\n")
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        os.symlink(target, self.file)
        result, _, detail = self.ensure()
        self.assertEqual(result, "refused")
        self.assertIn("symlink", detail)
        self.assertTrue(os.path.islink(self.file))
        self.assertEqual(os.readlink(self.file), target)
        with open(target) as f:
            self.assertEqual(f.read(), "target\n")

    def test_refuses_unclosed_fence(self):
        self.write_user("text\n```\ncode\n")
        before = self.read_user()
        result, _, detail = self.ensure()
        self.assertEqual(result, "refused")
        self.assertIn("unclosed code fence opened at line 2", detail)
        self.assertEqual(self.read_user(), before)

    def test_balanced_fences_get_block(self):
        self.write_user("```\ncode\n```\n")
        self.assertEqual(self.ensure()[0], "wrote")
        self.assertEqual(self.status()[0], "ok")

    def test_refuses_undecodable_file(self):
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        with open(self.file, "wb") as f:
            f.write(b"\xff\xfe\x00bad")
        self.assertEqual(self.ensure()[0], "refused")

    def test_owned_block_with_old_path_rewrites_only_that_line(self):
        old = "@" + os.path.join(self.root, "old", "policy", "CLAUDE.md")
        self.write_user("before\n" + tp.MARKER + "\n" + old + "\nafter\n")
        result, _, detail = self.ensure()
        self.assertEqual(result, "wrote")
        self.assertIn("updated", detail)
        self.assertEqual(self.read_user().decode(),
                         "before\n" + tp.MARKER + "\n" + tp.import_line(self.checkout) + "\nafter\n")
        self.assertEqual(self.status()[0], "ok")

    def test_marker_as_last_line_gets_import_appended(self):
        for tail in ("", "\n"):
            self.write_user("before\n" + tp.MARKER + tail)
            self.assertEqual(self.ensure()[0], "wrote")
            self.assertEqual(self.status()[0], "ok")
            self.assertEqual(self.read_user().decode(),
                             "before\n" + tp.MARKER + "\n" + tp.import_line(self.checkout) + "\n")

    def test_marker_inside_fence_is_not_an_owned_block(self):
        self.write_user("```\n" + tp.MARKER + "\nkeep me\n```\n")
        self.assertEqual(self.ensure()[0], "wrote")
        self.assertIn(b"keep me\n```\n", self.read_user())
        self.assertEqual(self.status()[0], "ok")

    def test_opt_out_writes_nothing(self):
        cfg = FakeConfig({"team_policy": {"import": False}})
        result, _, _ = self.ensure(cfg)
        self.assertEqual(result, "opted-out")
        self.assertFalse(os.path.exists(self.file))

    def test_existing_permission_bits_kept(self):
        self.write_user("x\n")
        os.chmod(self.file, 0o640)
        self.ensure()
        self.assertEqual(os.stat(self.file).st_mode & 0o777, 0o640)

    def test_no_temp_files_left_behind(self):
        self.ensure()
        self.assertEqual(os.listdir(os.path.dirname(self.file)), ["CLAUDE.md"])


class MarkerFollowedByUserLineTestCase(Base):

    def test_user_line_after_marker_is_kept_and_import_inserted(self):
        self.write_user(tp.MARKER + "\n- Always reply in Portuguese.\n")
        result, _, _ = self.ensure()
        self.assertEqual(result, "wrote")
        self.assertEqual(self.read_user().decode(),
                         tp.MARKER + "\n" + tp.import_line(self.checkout)
                         + "\n- Always reply in Portuguese.\n")
        self.assertEqual(self.status()[0], "ok")

    def test_user_import_after_marker_is_kept_and_import_inserted(self):
        # Only a policy/CLAUDE.md import under the marker is ours to repair.
        for user_line in ("@~/notes/rules.md",
                          "@" + os.path.join(self.root, "old", "policy", "CLAUDE.md") + " and my notes"):
            with self.subTest(user_line=user_line):
                self.write_user(tp.MARKER + "\n" + user_line + "\n")
                self.assertEqual(self.ensure()[0], "wrote")
                self.assertEqual(self.read_user().decode(),
                                 tp.MARKER + "\n" + tp.import_line(self.checkout)
                                 + "\n" + user_line + "\n")
                self.assertEqual(self.status()[0], "ok")


class BrokenSymlinkTestCase(Base):

    def test_status_missing_with_broken_symlink_detail(self):
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        os.symlink(os.path.join(self.root, "nowhere"), self.file)
        state, _, detail = self.status()
        self.assertEqual(state, "missing")
        self.assertIn("broken symlink", detail)
        self.assertEqual(self.ensure()[0], "refused")


class CanonicalCheckoutTestCase(Base):

    def test_plain_dir_returns_realpath(self):
        self.assertEqual(tp.canonical_checkout(self.checkout), os.path.realpath(self.checkout))

    def test_git_file_without_commondir_returns_realpath(self):
        gitdir = os.path.join(self.root, "gd")
        os.makedirs(gitdir)
        with open(os.path.join(self.checkout, ".git"), "w") as f:
            f.write("gitdir: %s\n" % gitdir)
        self.assertEqual(tp.canonical_checkout(self.checkout), os.path.realpath(self.checkout))

    def test_git_directory_returns_realpath(self):
        os.makedirs(os.path.join(self.checkout, ".git"))
        self.assertEqual(tp.canonical_checkout(self.checkout), os.path.realpath(self.checkout))


class WorktreeMappingTestCase(Base):

    def setUp(self):
        super().setUp()
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
                         "GIT_CONFIG_NOSYSTEM": "1"})
        self.repo = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.repo, "policy"))
        with open(os.path.join(self.repo, "policy", "CLAUDE.md"), "w") as f:
            f.write("# policy\n")
        self.git("init", "-b", "main")
        self.git("symbolic-ref", "HEAD", "refs/heads/main")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "T")
        self.git("add", "-A")
        self.git("commit", "-m", "init")
        self.worktree = os.path.join(self.root, "wt")
        self.git("worktree", "add", "-b", "feat", self.worktree)

    def git(self, *args):
        subprocess.run(["git", "-C", self.repo] + list(args), env=self.env, check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)

    def test_worktree_maps_to_primary_checkout(self):
        self.assertEqual(tp.canonical_checkout(self.worktree), os.path.realpath(self.repo))

    def test_ensure_from_worktree_imports_primary_policy(self):
        result, _, _ = tp.ensure(self.worktree, self.home, self.cfg)
        self.assertEqual(result, "wrote")
        with open(self.file, encoding="utf-8") as f:
            text = f.read()
        self.assertIn("@" + os.path.join(os.path.realpath(self.repo), "policy", "CLAUDE.md"), text)
        self.assertNotIn(self.worktree, text)

    def test_status_ok_from_worktree_and_primary(self):
        tp.ensure(self.worktree, self.home, self.cfg)
        self.assertEqual(tp.status(self.worktree, self.home, self.cfg)[0], "ok")
        self.assertEqual(tp.status(self.repo, self.home, self.cfg)[0], "ok")


class OddPathTestCase(Base):
    checkout_name = "my ʻchecko ut"

    def test_space_and_okina_round_trip(self):
        result, _, _ = self.ensure()
        self.assertEqual(result, "wrote")
        text = self.read_user().decode("utf-8")
        self.assertIn("\\ ", text)
        self.assertIn("ʻ", text)
        self.assertEqual(self.status()[0], "ok")
        self.assertEqual(self.ensure()[0], "unchanged")


class CliTestCase(Base):
    def run_cli(self, *args, config=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["HOME"] = self.home
        env["CLAUDE_TOOLKIT_CONFIG"] = config or os.path.join(self.root, "none.json")
        return subprocess.run(
            [sys.executable, os.path.join(LIB, "team_policy.py"), *args],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8")

    def test_status_prints_one_json_line_with_exact_keys(self):
        r = self.run_cli("--status", self.checkout)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(set(data), {"state", "path", "detail"})
        self.assertEqual(data["state"], "missing")

    def test_ensure_writes_then_status_ok(self):
        r = self.run_cli("ensure", self.checkout)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout.splitlines()[0])
        self.assertEqual(set(data), {"result", "path", "detail"})
        self.assertEqual(data["result"], "wrote")
        self.assertEqual(json.loads(self.run_cli("--status", self.checkout).stdout)["state"], "ok")

    def test_ensure_exits_1_on_refused(self):
        self.write_user("```\nopen\n")
        r = self.run_cli("ensure", self.checkout)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["result"], "refused")

    def test_opt_out_via_config_file(self):
        cfgpath = os.path.join(self.root, "cfg.json")
        with open(cfgpath, "w") as f:
            json.dump({"team_policy": {"import": False}}, f)
        r = self.run_cli("--status", self.checkout, config=cfgpath)
        self.assertEqual(json.loads(r.stdout)["state"], "opted-out")

    def test_bad_argv_exits_2(self):
        for args in ([], ["--status"], ["bogus", self.checkout], ["ensure"]):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 2, args)
            self.assertIn("usage", r.stderr)


class RemoveTests(Base):
    def other_import(self):
        other = os.path.join(self.root, "other")
        os.makedirs(os.path.join(other, "policy"))
        with open(os.path.join(other, "policy", "CLAUDE.md"), "w") as f:
            f.write("x\n")
        return tp.import_line(other)

    def test_removes_marker_and_import_keeps_personal_text(self):
        self.write_user("above\n\n%s\n%s\nbelow\n" % (tp.MARKER, tp.import_line(self.checkout)))
        state, path, _ = tp.remove(self.checkout, self.home)
        self.assertEqual((state, path), ("removed", self.file))
        self.assertEqual(self.read_user(), b"above\n\nbelow\n")

    def test_ensure_then_remove_restores_the_file_byte_for_byte(self):
        original = "# My global rules\n\n- Never push without asking.\n"
        self.write_user(original)
        self.assertEqual(self.ensure()[0], "wrote")
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "removed")
        self.assertEqual(self.read_user(), original.encode())

    def test_blank_line_above_marker_kept_when_text_follows(self):
        self.write_user("above\n\n%s\n%s\n\nbelow\n" % (tp.MARKER, tp.import_line(self.checkout)))
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "removed")
        self.assertEqual(self.read_user(), b"above\n\n\nbelow\n")

    def test_unmarked_import_keeps_line_above(self):
        self.write_user("mine\n%s\nafter\n" % tp.import_line(self.checkout))
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "removed")
        self.assertEqual(self.read_user(), b"mine\nafter\n")

    def test_other_checkouts_import_is_untouched(self):
        text = "a\n%s\n%s\n" % (tp.MARKER, self.other_import())
        self.write_user(text)
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "absent")
        self.assertEqual(self.read_user(), text.encode())

    def test_fenced_import_is_absent(self):
        text = "```\n%s\n```\n" % tp.import_line(self.checkout)
        self.write_user(text)
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "absent")
        self.assertEqual(self.read_user(), text.encode())

    def test_symlinked_file_refused(self):
        target = os.path.join(self.root, "real.md")
        body = tp.import_line(self.checkout) + "\n"
        with open(target, "w") as f:
            f.write(body)
        os.makedirs(os.path.dirname(self.file), exist_ok=True)
        os.symlink(target, self.file)
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "refused")
        with open(target) as f:
            self.assertEqual(f.read(), body)

    def test_crlf_file_keeps_line_endings(self):
        self.write_user("a\r\n%s\r\n%s\r\nb\r\n" % (tp.MARKER, tp.import_line(self.checkout)))
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "removed")
        self.assertEqual(self.read_user(), b"a\r\nb\r\n")

    def test_missing_file_is_absent(self):
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "absent")

    def test_line_with_other_text_is_left_untouched(self):
        text = "a\n%s Always ask before touching production.\nb\n" % tp.import_line(self.checkout)
        self.write_user(text)
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "absent")
        self.assertEqual(self.read_user(), text.encode())

    def test_import_only_line_with_surrounding_whitespace_is_removed(self):
        self.write_user("a\n  %s  \nb\n" % tp.import_line(self.checkout))
        self.assertEqual(tp.remove(self.checkout, self.home)[0], "removed")
        self.assertEqual(self.read_user(), b"a\nb\n")

    def test_cli_remove(self):
        self.write_user("a\n%s\n" % tp.import_line(self.checkout))
        r = self.run_cli("remove", self.checkout)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["state"], "removed")
        self.write_user("x\n")
        os.remove(self.file)
        os.symlink(os.path.join(self.root, "nope"), self.file)
        r = self.run_cli("remove", self.checkout)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["state"], "refused")

    def run_cli(self, *args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["HOME"] = self.home
        return subprocess.run(
            [sys.executable, os.path.join(LIB, "team_policy.py"), *args],
            env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
