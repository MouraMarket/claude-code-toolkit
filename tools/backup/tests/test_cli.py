"""CLI tests: subprocess against the real executable, fixture HOME and config."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
CLI = os.path.join(TOOL_DIR, "claude-backup")
SURVEY = os.path.join(os.path.dirname(TOOL_DIR), "wrap-up", "claude-wrap-up-survey")
sys.path.insert(0, TOOL_DIR)
import backup_lib  # noqa: E402
import secret_lib  # noqa: E402

SECRET = "AKIA" + "J7QZ3MWK9LP2RTX4"
TIMEOUT = 60


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.workspace = os.path.join(self.tmp, "ws")
        self.dest = os.path.join(self.tmp, "dest")
        self.tempdir = os.path.join(self.tmp, "tmpdir")
        for path in (self.home, self.workspace, self.dest, self.tempdir):
            os.makedirs(path)
        self.write(os.path.join(self.workspace, "CLAUDE.md"), "hello\n")
        self.write(os.path.join(self.workspace, "notes", "n.txt"), "note\n")
        os.symlink("notes/n.txt", os.path.join(self.workspace, "link-to-note"))
        memory = os.path.join(self.home, ".claude", "projects", "p1", "memory")
        self.write(os.path.join(memory, "MEMORY.md"), "memory\n")
        self.config_path = os.path.join(self.tmp, "config.json")

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def write_config(self, **overrides):
        data = {"workspace_root": self.workspace, "backup": {"destination": self.dest}}
        data.update(overrides)
        for key in [k for k, v in data.items() if v is None]:
            del data[key]
        with open(self.config_path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def run_cli(self, *args, config_path=None, extra_env=None, tool=CLI):
        env = dict(os.environ)
        for key in [k for k in env if k.startswith("GIT_")]:
            del env[key]
        env.pop("CLAUDE_CONFIG_DIR", None)
        env["HOME"] = self.home
        env["USERPROFILE"] = self.home
        env["TMPDIR"] = self.tempdir
        env["TEMP"] = self.tempdir
        env["TMP"] = self.tempdir
        env["CLAUDE_TOOLKIT_CONFIG"] = config_path or self.config_path
        env.update(extra_env or {})
        return subprocess.run([sys.executable, tool] + list(args),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=env, timeout=TIMEOUT,
                              universal_newlines=True)

    def stamps(self):
        host_dir = os.path.join(self.dest, backup_lib.host_name())
        if not os.path.isdir(host_dir):
            return []
        return sorted(n for n in os.listdir(host_dir) if backup_lib.NAME_RE.match(n))

    def all_dest_entries(self):
        found = []
        for root, dirs, files in os.walk(self.dest):
            found.extend(dirs + files)
        return found


class RunTests(CliBase):
    def test_no_config_exits_2_and_writes_nothing(self):
        result = self.run_cli(config_path="/nonexistent.json")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("backup.destination is not set", result.stderr)
        self.assertEqual(os.listdir(self.dest), [])

    def test_full_run_status_restore_round_trip(self):
        self.write_config()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        self.assertIn("copied to", lines[0])
        self.assertIn("secret-scan drops", lines[0])
        self.assertNotIn("backed up to the cloud", result.stdout + result.stderr)
        stamps = self.stamps()
        self.assertEqual(len(stamps), 1)
        folder = os.path.join(self.dest, backup_lib.host_name(), stamps[0])
        with open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertIs(manifest["complete"], True)
        # The count is of files actually copied, never of record-only symlinks.
        copied = sum(1 for f in manifest["files"] if f["type"] == "file")
        self.assertTrue(any(f["type"] == "symlink" for f in manifest["files"]))
        self.assertIn(" %d files," % copied, lines[0])
        # No staging dir is left in the temp root.
        self.assertEqual([n for n in os.listdir(self.tempdir)
                          if n.startswith("claude-backup-")], [])

        status = self.run_cli("--status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)

        target = os.path.join(self.tmp, "restored")
        restore = self.run_cli("--restore", folder, "--to", target)
        self.assertEqual(restore.returncode, 0, restore.stdout + restore.stderr)
        restored = os.path.join(target, "workspace-root", "CLAUDE.md")
        with open(restored, "rb") as got, open(
                os.path.join(self.workspace, "CLAUDE.md"), "rb") as want:
            self.assertEqual(got.read(), want.read())

    def test_non_finite_numbers_fall_back_to_the_defaults(self):
        # json.dump writes Infinity and NaN; int() raises OverflowError on the first.
        self.write_config(backup={"destination": self.dest, "keep": float("inf"),
                                  "max_age_days": float("nan")})
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertIn("Infinity", handle.read())
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.run_cli("--status")
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)

    def test_a_secret_in_a_symlink_target_never_reaches_destination(self):
        os.symlink("notes/" + SECRET, os.path.join(self.workspace, "odd-link"))
        self.write_config()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        needle = SECRET.encode("ascii")
        for root, _dirs, files in os.walk(self.dest):
            for name in files:
                with open(os.path.join(root, name), "rb") as handle:
                    self.assertNotIn(needle, handle.read(), os.path.join(root, name))
        folder = os.path.join(self.dest, backup_lib.host_name(), self.stamps()[0])
        with open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual([e["path"] for e in manifest["excluded"] if e["reason"] == "secret"],
                         ["workspace-root/odd-link"])
        # The ordinary link is still recorded with its target.
        self.assertIn("workspace-root/link-to-note",
                      [f["path"] for f in manifest["files"] if f["type"] == "symlink"])

    def test_a_workspace_root_that_is_a_file_is_incomplete(self):
        not_a_dir = os.path.join(self.tmp, "ws-file")
        self.write(not_a_dir, "not a folder\n")
        self.write_config(workspace_root=not_a_dir)
        result = self.run_cli()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("INCOMPLETE: workspace-root not-a-directory", result.stderr)
        folder = os.path.join(self.dest, backup_lib.host_name(), self.stamps()[0])
        with open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertIs(manifest["complete"], False)
        self.assertNotIn("workspace-root", [f["path"] for f in manifest["files"]])
        self.assertEqual(self.run_cli("--status").returncode, 1)

    def test_skips_the_same_repos_root_the_survey_reports(self):
        # One reader (toolkit_config.repos_root) for both tools: a $VAR in
        # wrap_up.repos_root used to expand here and not in the survey.
        repos = os.path.join(self.workspace, "code")
        self.write(os.path.join(repos, "r1", "f.txt"), "checkout\n")
        self.write_config(wrap_up={"repos_root": "$BACKUP_TEST_REPOS"})
        env = {"BACKUP_TEST_REPOS": repos}
        result = self.run_cli(extra_env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        folder = os.path.join(self.dest, backup_lib.host_name(), self.stamps()[0])
        with open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertIn("workspace-root/code",
                      [e["path"] for e in manifest["excluded"]])
        self.assertFalse([f for f in manifest["files"]
                          if f["path"].startswith("workspace-root/code/")])
        survey = self.run_cli(extra_env=env, tool=SURVEY)
        self.assertEqual(survey.returncode, 0, survey.stderr)
        self.assertEqual(json.loads(survey.stdout)["repos_root"], repos)

    def test_a_refused_repos_root_is_refused_by_both_tools(self):
        # A set but unusable value is never replaced by <workspace_root>/repos:
        # the survey says it could not look, and backup skips nothing for it.
        self.write(os.path.join(self.workspace, "repos", "r1", "f.txt"), "checkout\n")
        self.write_config(wrap_up={"repos_root": 7})
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        folder = os.path.join(self.dest, backup_lib.host_name(), self.stamps()[0])
        with open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertIn("workspace-root/repos/r1/f.txt",
                      [f["path"] for f in manifest["files"]])
        survey = json.loads(self.run_cli(tool=SURVEY).stdout)
        self.assertIs(survey["surveyed"], False)
        self.assertIn("wrap_up.repos_root", survey["reason"])

    def test_planted_secret_never_reaches_destination(self):
        self.assertTrue(secret_lib.scan_text("key = " + SECRET + "\n"),
                        "positive control: the scanner must flag the planted secret")
        self.write(os.path.join(self.workspace, "notes", "creds.txt"),
                   "aws_access_key_id = " + SECRET + "\n")
        self.write_config()
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        needle = SECRET.encode("ascii")
        for root, _dirs, files in os.walk(self.dest):
            for name in files:
                with open(os.path.join(root, name), "rb") as handle:
                    self.assertNotIn(needle, handle.read(), os.path.join(root, name))
        folder = os.path.join(self.dest, backup_lib.host_name(), self.stamps()[0])
        with open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        secrets = [e for e in manifest["excluded"] if e["reason"] == "secret"]
        self.assertEqual(len(secrets), 1, manifest["excluded"])
        self.assertTrue(secrets[0]["path"].endswith("creds.txt"))
        self.assertTrue(secrets[0]["detail"])
        self.assertNotIn(SECRET, secrets[0]["detail"])

    def test_unreadable_file_aborts_and_leaves_no_folder(self):
        if os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0):
            self.skipTest("permissions are not enforced here")
        locked = os.path.join(self.workspace, "notes", "locked.txt")
        self.write(locked, "secret stuff\n")
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o644)
        self.write_config()
        result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        for name in self.all_dest_entries():
            self.assertIsNone(backup_lib.NAME_RE.match(name), name)
            self.assertFalse(name.startswith(backup_lib.INCOMPLETE_PREFIX), name)
        self.assertEqual([n for n in os.listdir(self.tempdir)
                          if n.startswith("claude-backup-")], [])

    def test_workspace_root_unset_is_incomplete(self):
        self.write_config(workspace_root=None)
        result = self.run_cli()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("workspace-root", result.stderr)
        folder = os.path.join(self.dest, backup_lib.host_name(), self.stamps()[0])
        with open(os.path.join(folder, "MANIFEST.json"), encoding="utf-8") as handle:
            self.assertIs(json.load(handle)["complete"], False)
        status = self.run_cli("--status")
        self.assertEqual(status.returncode, 1, status.stdout + status.stderr)


class RefusalTests(CliBase):
    def test_missing_destination_exits_2(self):
        self.write_config(backup={"destination": os.path.join(self.tmp, "nope")})
        result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "nope")))

    def test_destination_inside_workspace_exits_2(self):
        inside = os.path.join(self.workspace, "backups")
        os.makedirs(inside)
        self.write_config(backup={"destination": inside})
        result = self.run_cli()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(os.listdir(inside), [])

    def test_restore_into_existing_directory_exits_2(self):
        self.write_config()
        self.assertEqual(self.run_cli().returncode, 0)
        folder = os.path.join(self.dest, backup_lib.host_name(), self.stamps()[0])
        existing = os.path.join(self.tmp, "already")
        os.makedirs(existing)
        result = self.run_cli("--restore", folder, "--to", existing)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertEqual(os.listdir(existing), [])

    def test_to_without_restore_exits_2(self):
        self.write_config()
        result = self.run_cli("--to", os.path.join(self.tmp, "x"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(os.listdir(self.dest), [])


if __name__ == "__main__":
    unittest.main()
