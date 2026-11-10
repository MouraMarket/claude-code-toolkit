import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOOK = os.path.join(ROOT, "claude-title-session")

spec = importlib.util.spec_from_loader(
    "hook", importlib.machinery.SourceFileLoader("hook", HOOK))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class TestRecursionGuard(unittest.TestCase):
    def test_exits_zero_and_silent_when_guard_env_set(self):
        payload = json.dumps({"session_id": "s", "transcript_path": "/tmp/x.jsonl",
                              "cwd": "/tmp", "hook_event_name": "SessionEnd"})
        env = dict(os.environ)
        env["CLAUDE_TITLE_HOOK"] = "1"
        proc = subprocess.run([sys.executable, HOOK], input=payload, env=env,
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")


class TestHookRobustness(unittest.TestCase):
    def _run(self, payload_text):
        env = dict(os.environ)
        env["CLAUDE_TITLE_HOOK"] = "1"  # never spawn a real model call in tests
        return subprocess.run([sys.executable, HOOK], input=payload_text, env=env,
                              capture_output=True, text=True, timeout=20)

    def test_malformed_json_exits_zero(self):
        self.assertEqual(self._run("not json").returncode, 0)

    def test_empty_stdin_exits_zero(self):
        self.assertEqual(self._run("").returncode, 0)

    def test_missing_fields_exit_zero(self):
        self.assertEqual(self._run(json.dumps({})).returncode, 0)


def _fake_claude_bin(tmp, marker, reply):
    """A stand-in `claude` on PATH that records it ran and prints `reply`."""
    bin_dir = os.path.join(tmp, "fakebin")
    os.makedirs(bin_dir)
    fake = os.path.join(bin_dir, "claude")
    with open(fake, "w") as f:
        f.write("#!/usr/bin/env python3\n"
                "open({0!r}, 'w').close()\n"
                "print({1!r})\n".format(marker, reply))
    os.chmod(fake, 0o755)
    return bin_dir


def _titleworthy_transcript(tmp):
    path = os.path.join(tmp, "t.jsonl")
    with open(path, "w") as f:
        for i in range(2):
            f.write(json.dumps({"type": "user",
                                "message": {"content": "do thing {0}".format(i)}}) + "\n")
    return path


class _IsolatedWorker(unittest.TestCase):
    """Runs `run_worker` in-process with the log and index in a temp dir, so
    no test writes to the real ~/.claude."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.index = os.path.join(self.tmp, "index.tsv")
        patches = [
            mock.patch.object(hook.title_lib, "LOG_PATH",
                              os.path.join(self.tmp, "title-hook.log")),
            mock.patch.object(hook.title_lib, "INDEX_PATH", self.index),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_with_reply(self, reply):
        marker = os.path.join(self.tmp, "claude-was-called")
        fake_bin = _fake_claude_bin(self.tmp, marker, reply)
        transcript = _titleworthy_transcript(self.tmp)
        path_env = fake_bin + os.pathsep + os.environ.get("PATH", "")
        with mock.patch.dict(os.environ, {"PATH": path_env}):
            code = hook.run_worker("sess-w", transcript, "/work/dir")
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(marker), "the fake claude never ran")
        with open(transcript) as f:
            records = [json.loads(line) for line in f]
        return [r for r in records if r.get("type") == "custom-title"]


class TestSkipsUnworthySessions(_IsolatedWorker):
    def test_worker_skips_single_message_session(self):
        path = os.path.join(self.tmp, "one.jsonl")
        with open(path, "w") as f:
            f.write(json.dumps({"type": "user",
                                "message": {"content": "echo ok"}}) + "\n")
        code = hook.run_worker("sess-x", path, "/tmp")
        with open(path) as f:
            content = f.read()
        self.assertEqual(code, 0)
        self.assertNotIn("custom-title", content)


class TestWorkerWritesTitles(_IsolatedWorker):
    def test_a_plain_outcome_title_is_appended_and_indexed(self):
        titles = self.run_with_reply("Fixed the upload retry bug")
        self.assertEqual([t["customTitle"] for t in titles],
                         ["Fixed the upload retry bug"])
        with open(self.index) as f:
            row = f.read().rstrip("\n").split("\t")
        self.assertEqual(row[1:], ["sess-w", "/work/dir", "Fixed the upload retry bug"])

    def test_an_apology_reply_writes_nothing(self):
        titles = self.run_with_reply("Apologies, I cannot title this session")
        self.assertEqual(titles, [])
        self.assertFalse(os.path.exists(self.index))

    def test_no_index_row_without_the_transcript_record(self):
        """Gate finding: a failed transcript append still wrote an index row,
        so csearch would return a title the session never got."""
        with mock.patch.object(hook.title_lib, "append_title_record",
                               return_value=False):
            titles = self.run_with_reply("Fixed the upload retry bug")
        self.assertEqual(titles, [])
        self.assertFalse(os.path.exists(self.index))

    def test_a_prose_reply_writes_nothing(self):
        titles = self.run_with_reply(
            "I don't have any information to title this session.")
        self.assertEqual(titles, [])
        self.assertFalse(os.path.exists(self.index))


class TestEnabledFlag(unittest.TestCase):
    """`session_titler.enabled: false` must stop the fork itself, not merely
    return 0 -- `except: exit(0)` guarantees the exit code no matter what, so
    the real assertion is that the worker (and the `claude` call inside it)
    never runs at all."""

    def _run_entry(self, enabled):
        """Run the real hook entry point; True if the worker reached `claude`.

        HOME and the index point into the temp dir, so the worker forked by
        the enabled case writes nothing outside it."""
        with tempfile.TemporaryDirectory() as tmp:
            marker = os.path.join(tmp, "claude-was-called")
            fake_bin = _fake_claude_bin(tmp, marker, "Did a fake thing")
            transcript = _titleworthy_transcript(tmp)
            os.makedirs(os.path.join(tmp, ".claude"))

            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w") as f:
                json.dump({"session_titler": {
                    "enabled": enabled,
                    "index_path": os.path.join(tmp, "index.tsv")}}, f)

            payload = json.dumps({"session_id": "s1", "transcript_path": transcript,
                                  "cwd": tmp, "hook_event_name": "SessionEnd"})

            env = dict(os.environ)
            env.pop("CLAUDE_TITLE_HOOK", None)  # exercise the REAL entry path
            env["CLAUDE_TOOLKIT_CONFIG"] = config_path
            env["PATH"] = fake_bin + os.pathsep + env.get("PATH", "")
            env["HOME"] = tmp

            proc = subprocess.run([sys.executable, HOOK], input=payload, env=env,
                                  capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0)

            for _ in range(50):
                if os.path.exists(marker):
                    break
                time.sleep(0.1)
            called = os.path.exists(marker)
            if called:
                # Let the detached worker finish writing before the temp
                # dir is removed under it.
                for _ in range(50):
                    if os.path.exists(os.path.join(tmp, "index.tsv")):
                        break
                    time.sleep(0.1)
            return called

    def test_disabled_by_config_forks_no_worker(self):
        self.assertFalse(self._run_entry(False),
                         "worker forked and invoked `claude` despite enabled: false")

    def test_enabled_forks_a_worker_that_calls_claude(self):
        """Positive control: the same setup with the flag on does reach
        `claude`, so the disabled test above can fail."""
        self.assertTrue(self._run_entry(True))


if __name__ == "__main__":
    unittest.main()
