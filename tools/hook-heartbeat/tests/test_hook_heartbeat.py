#!/usr/bin/env python3
"""Pins claude-hook-heartbeat: record formats, the session-id path-safety rule,
startup-only .start records, silent exit 0 on failure, atomic writes, and pruning."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import time
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(TOOL_DIR, "claude-hook-heartbeat")
DAY = 86400


class HeartbeatCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.dir = os.path.join(self.home, ".claude", "claude-code-toolkit", "heartbeat")

    def run_script(self, stdin, args=(), plugin_root=None, home=None, script=SCRIPT,
                   path_prefix=None):
        # Environment built from scratch: nothing inherited from the runner, so a
        # surrounding Claude Code session cannot leak CLAUDE_* variables in.
        path = os.environ.get("PATH", "/usr/bin:/bin")
        if path_prefix:
            path = path_prefix + os.pathsep + path
        env = {"PATH": path, "HOME": home or self.home}
        if plugin_root is not None:
            env["CLAUDE_PLUGIN_ROOT"] = plugin_root
        proc = subprocess.run(
            [script, *args], input=stdin, env=env, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")
        return proc

    def record(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        return [tuple(line.split("=", 1)) for line in lines]

    def files_under(self, root):
        found = []
        for base, _dirs, names in os.walk(root):
            found.extend(os.path.join(base, n) for n in names)
        return found


class TestFired(HeartbeatCase):
    def test_env_root_record(self):
        self.run_script('{"session_id":"abc-1","tool_name":"Bash"}', plugin_root="/x/plugin")
        rows = self.record("abc-1.fired")
        self.assertEqual([k for k, _ in rows], ["fired_at", "plugin_root", "root_source"])
        values = dict(rows)
        self.assertEqual(values["plugin_root"], "/x/plugin")
        self.assertEqual(values["root_source"], "env")
        self.assertLessEqual(abs(int(values["fired_at"]) - time.time()), 5)

    def test_path_root_record(self):
        layout = os.path.join(self.tmp, "layout")
        os.makedirs(os.path.join(layout, "plugin"))
        tools = os.path.join(layout, "tools", "hook-heartbeat")
        os.makedirs(tools)
        script = os.path.join(tools, "claude-hook-heartbeat")
        shutil.copy2(SCRIPT, script)
        os.symlink("../tools", os.path.join(layout, "plugin", "tools"))
        via_plugin = os.path.join(layout, "plugin", "tools", "hook-heartbeat", "claude-hook-heartbeat")
        self.run_script('{"session_id":"abc-1"}', script=via_plugin)
        values = dict(self.record("abc-1.fired"))
        self.assertEqual(values["plugin_root"], os.path.join(layout, "plugin"))
        self.assertEqual(values["root_source"], "path")


class TestSessionStart(HeartbeatCase):
    def make_plugin(self):
        root = os.path.join(self.tmp, "plugin")
        os.makedirs(os.path.join(root, "hooks"))
        path = os.path.join(root, "hooks", "hooks.json")
        with open(path, "wb") as handle:
            handle.write(b'{"hooks": {}}\n')
        return root, path

    def test_startup_record_with_hash(self):
        root, path = self.make_plugin()
        self.run_script('{"session_id":"abc-1","source":"startup"}',
                        args=["--session-start"], plugin_root=root)
        rows = self.record("abc-1.start")
        self.assertEqual([k for k, _ in rows],
                         ["started_at", "plugin_root", "root_source", "hooks_sha256"])
        values = dict(rows)
        with open(path, "rb") as handle:
            self.assertEqual(values["hooks_sha256"], hashlib.sha256(handle.read()).hexdigest())
        self.assertEqual(values["plugin_root"], root)
        self.assertLessEqual(abs(int(values["started_at"]) - time.time()), 5)

    def start_files(self):
        return sorted(n for n in os.listdir(self.dir) if n.endswith(".start"))

    def test_profile_plugin_root_gets_its_own_start_record(self):
        for name in ("plugin-build", "plugin-continuity", "plugin-mac", "plugin-maintainer"):
            root = os.path.join(self.tmp, name)
            os.makedirs(os.path.join(root, "hooks"))
            with open(os.path.join(root, "hooks", "hooks.json"), "wb") as handle:
                handle.write(b"{}\n")
            self.run_script('{"session_id":"abc-1","source":"startup"}',
                            args=["--session-start"], plugin_root=root)
        self.assertEqual(self.start_files(), [
            "abc-1.plugin-build.start", "abc-1.plugin-continuity.start",
            "abc-1.plugin-mac.start", "abc-1.plugin-maintainer.start"])
        values = dict(self.record("abc-1.plugin-mac.start"))
        self.assertEqual(values["plugin_root"], os.path.join(self.tmp, "plugin-mac"))
        self.assertNotEqual(values["hooks_sha256"], "")

    def test_profile_root_with_trailing_slash_is_named_by_basename(self):
        self.run_script('{"session_id":"abc-1","source":"startup"}', args=["--session-start"],
                        plugin_root=os.path.join(self.tmp, "plugin-build") + "/")
        self.assertEqual(self.start_files(), ["abc-1.plugin-build.start"])

    def test_other_roots_keep_the_plain_start_name(self):
        for index, name in enumerate(("plugin", "plugin-", "plugin-Build", "plugin-build2",
                                      "plugin-b-c", "my-plugin-build", "x")):
            sid = "s%d" % index
            self.run_script('{"session_id":"%s","source":"startup"}' % sid,
                            args=["--session-start"],
                            plugin_root=os.path.join(self.tmp, name))
            with self.subTest(root=name):
                self.assertEqual(self.start_files().count(sid + ".start"), 1)
        self.assertEqual([n for n in self.start_files() if n.count(".") > 1], [])

    def test_profile_root_does_not_change_the_fired_record(self):
        self.run_script('{"session_id":"abc-1"}',
                        plugin_root=os.path.join(self.tmp, "plugin-build"))
        self.assertEqual(os.listdir(self.dir), ["abc-1.fired"])

    def test_profile_start_records_are_pruned_after_30_days(self):
        os.makedirs(self.dir)
        old = os.path.join(self.dir, "old-1.plugin-build.start")
        with open(old, "w") as handle:
            handle.write("started_at=1\n")
        stamp = time.time() - 31 * DAY
        os.utime(old, (stamp, stamp))
        self.run_script('{"session_id":"abc-1","source":"startup"}', args=["--session-start"],
                        plugin_root=os.path.join(self.tmp, "plugin-build"))
        self.assertFalse(os.path.exists(old))
        self.assertEqual(self.start_files(), ["abc-1.plugin-build.start"])

    def test_missing_hooks_json_leaves_hash_empty(self):
        self.run_script('{"session_id":"abc-1","source":"startup"}',
                        args=["--session-start"], plugin_root=os.path.join(self.tmp, "nope"))
        self.assertEqual(dict(self.record("abc-1.start"))["hooks_sha256"], "")

    def test_only_startup_writes(self):
        root, _ = self.make_plugin()
        for payload in (
            '{"session_id":"abc-1","source":"resume"}',
            '{"session_id":"abc-1","source":"clear"}',
            '{"session_id":"abc-1","source":"compact"}',
            '{"session_id":"abc-1"}',
        ):
            with self.subTest(payload=payload):
                self.run_script(payload, args=["--session-start"], plugin_root=root)
                self.assertEqual(self.files_under(self.home), [])


class TestSessionIdSafety(HeartbeatCase):
    def test_unsafe_ids_write_nothing(self):
        for sid in ('""', None, '"../evil"', '"a/b"', '"a b"'):
            body = '{"tool_name":"Bash"}' if sid is None else '{"session_id":%s}' % sid
            start = '{"source":"startup"}' if sid is None else '{"session_id":%s,"source":"startup"}' % sid
            with self.subTest(session_id=sid):
                self.run_script(body, plugin_root="/x/plugin")
                self.run_script(start, args=["--session-start"], plugin_root="/x/plugin")
                self.assertEqual(self.files_under(self.tmp), [])


class TestFailureIsSilent(HeartbeatCase):
    def test_read_only_home(self):
        if os.geteuid() == 0:
            self.skipTest("root ignores directory permissions")
        ro = os.path.join(self.tmp, "ro")
        os.makedirs(ro)
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        self.run_script('{"session_id":"abc-1"}', plugin_root="/x/plugin", home=ro)
        self.run_script('{"session_id":"abc-1","source":"startup"}',
                        args=["--session-start"], plugin_root="/x/plugin", home=ro)
        self.assertEqual(self.files_under(ro), [])

    def test_no_tmp_file_left_behind(self):
        self.run_script('{"session_id":"abc-1"}', plugin_root="/x/plugin")
        self.run_script('{"session_id":"abc-1","source":"startup"}',
                        args=["--session-start"], plugin_root="/x/plugin")
        leftovers = [f for f in self.files_under(self.home) if ".tmp." in os.path.basename(f)]
        self.assertEqual(leftovers, [])
        self.assertEqual(len(self.files_under(self.home)), 2)

    def test_failed_write_removes_its_tmp_file(self):
        # A directory squatting on the final name makes the rename fail after the tmp
        # file was written, which is the path that must clean up.
        os.makedirs(os.path.join(self.dir, "abc-1.fired"))
        self.run_script('{"session_id":"abc-1"}', plugin_root="/x/plugin")
        leftovers = [f for f in self.files_under(self.dir) if ".tmp." in os.path.basename(f)]
        self.assertEqual(leftovers, [])

    def test_failed_rename_removes_its_tmp_file(self):
        # The squatting-directory case above never creates a tmp file (the -d guard
        # short-circuits first), so it cannot see the cleanup. A failing `mv` on
        # PATH reaches it: the tmp file exists, the rename fails, rm must run.
        shim = os.path.join(self.tmp, "shim")
        os.makedirs(shim)
        log = os.path.join(self.tmp, "mv.log")
        with open(os.path.join(shim, "mv"), "w") as handle:
            handle.write('#!/bin/sh\nfor a in "$@"; do [ -f "$a" ] && echo "$a" >> %s; done\nexit 1\n'
                         % log)
        os.chmod(os.path.join(shim, "mv"), 0o755)
        self.run_script('{"session_id":"abc-1"}', plugin_root="/x/plugin", path_prefix=shim)
        # Positive control: the shim ran while the tmp file existed.
        with open(log) as handle:
            seen = handle.read().split()
        self.assertTrue(any(".tmp." in p for p in seen), seen)
        leftovers = [f for f in self.files_under(self.dir) if ".tmp." in os.path.basename(f)]
        self.assertEqual(leftovers, [])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "abc-1.fired")))


class TestPrune(HeartbeatCase):
    def test_old_records_pruned_on_startup(self):
        os.makedirs(self.dir)
        old = os.path.join(self.dir, "old.fired")
        recent = os.path.join(self.dir, "recent.fired")
        for path, age in ((old, 31), (recent, 29)):
            with open(path, "w") as handle:
                handle.write("fired_at=1\n")
            stamp = time.time() - age * DAY
            os.utime(path, (stamp, stamp))
        self.run_script('{"session_id":"abc-1","source":"startup"}',
                        args=["--session-start"], plugin_root="/x/plugin")
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(recent))


if __name__ == "__main__":
    unittest.main()
