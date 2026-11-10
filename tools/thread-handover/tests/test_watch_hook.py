"""Tests for claude-thread-watch's claim path — the hook, not just its helpers.

`claim_attached_handover` had no tests, which is how it shipped only ever
looking at the NEWEST pending handover. With several sessions open under one
workspace root, the newest pending document is frequently not the one the user
attaches, and the attachment was then invisible: nothing claimed, nothing swept,
the index growing a permanently-pending record every time.

The hook has no .py extension (it runs as an executable), so it is loaded
here by path. `watch_lib` is a module global assigned in `main()`; the fixture
injects it, which is exactly what `main()` would have done.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TOOL_DIR)

import index_lib  # noqa: E402
import watch_lib  # noqa: E402


def _load_hook():
    """Import the extension-less hook script as a module."""
    path = os.path.join(TOOL_DIR, "claude-thread-watch")
    spec = importlib.util.spec_from_loader(
        "claude_thread_watch",
        importlib.machinery.SourceFileLoader("claude_thread_watch", path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.watch_lib = watch_lib      # main() does this; tests must too
    return module


hook = _load_hook()


class WatchHookTestCase(unittest.TestCase):

    def setUp(self):
        self._orig_index = index_lib.INDEX_PATH
        self._orig_dir = index_lib.HANDOVER_DIR
        self._tmp = tempfile.mkdtemp()
        self.handover_dir = os.path.join(self._tmp, "specs")
        os.makedirs(self.handover_dir)
        index_lib.INDEX_PATH = os.path.join(self._tmp, "index.json")
        index_lib.HANDOVER_DIR = self.handover_dir
        self.root = os.path.join(self._tmp, "workspace")
        os.makedirs(self.root)

    def tearDown(self):
        index_lib.INDEX_PATH = self._orig_index
        index_lib.HANDOVER_DIR = self._orig_dir

    def register(self, name, created):
        path = os.path.join(self.handover_dir, name)
        with open(path, "w") as handle:
            handle.write("# handover")
        index_lib.add_record(path, self.root, "AUTHOR", created)
        return path

    def transcript_naming(self, *paths):
        """A transcript whose single user prompt attaches `paths`."""
        target = os.path.join(self._tmp, "transcript.jsonl")
        text = " ".join("@" + os.path.basename(p) for p in paths)
        with open(target, "w") as handle:
            handle.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": text},
            }) + "\n")
        return target

    def consumed(self, path):
        for record in index_lib.load_index():
            if record.get("path") == path:
                return record.get("consumed_session_id")
        raise AssertionError("no record for " + path)

    def run_hook(self, transcript, session="READER"):
        hook.claim_attached_handover({"cwd": self.root}, session, transcript)


class TestClaimOnAttach(WatchHookTestCase):

    def test_attaching_the_only_pending_handover_claims_it(self):
        path = self.register("2026-08-10-only-handover.md", "2026-08-10T10:00:00")
        self.run_hook(self.transcript_naming(path))
        self.assertEqual(self.consumed(path), "READER")

    def test_attaching_the_newest_of_several_claims_it(self):
        self.register("2026-08-10-old-handover.md", "2026-08-10T10:00:00")
        new = self.register("2026-08-10-new-handover.md", "2026-08-10T12:00:00")
        self.run_hook(self.transcript_naming(new))
        self.assertEqual(self.consumed(new), "READER")

    def test_attaching_an_older_handover_still_claims_it(self):
        # THE DEFECT. Two threads each leave a handover; the user attaches the
        # older one because that is the work being continued. The hook only
        # ever looked at the newest, saw no mention of it, and claimed nothing —
        # so the attachment was silently invisible.
        old = self.register("2026-08-10-old-handover.md", "2026-08-10T10:00:00")
        self.register("2026-08-10-new-handover.md", "2026-08-10T12:00:00")
        self.run_hook(self.transcript_naming(old))
        self.assertEqual(self.consumed(old), "READER")

    def test_attaching_nothing_claims_nothing(self):
        path = self.register("2026-08-10-only-handover.md", "2026-08-10T10:00:00")
        empty = os.path.join(self._tmp, "empty.jsonl")
        with open(empty, "w") as handle:
            handle.write(json.dumps({
                "type": "user",
                "message": {"role": "user", "content": "unrelated prompt"},
            }) + "\n")
        self.run_hook(empty)
        self.assertIsNone(self.consumed(path))

    def test_claiming_one_leaves_the_others_pending(self):
        # A session continuing thread A must not consume thread B's handover.
        a = self.register("2026-08-10-alpha-handover.md", "2026-08-10T10:00:00")
        b = self.register("2026-08-10-bravo-handover.md", "2026-08-10T12:00:00")
        self.run_hook(self.transcript_naming(a))
        self.assertEqual(self.consumed(a), "READER")
        self.assertIsNone(self.consumed(b))

    def test_a_session_never_claims_its_own_handover(self):
        # SessionStart also fires on compact and resume with the same id.
        path = self.register("2026-08-10-mine-handover.md", "2026-08-10T10:00:00")
        self.run_hook(self.transcript_naming(path), session="AUTHOR")
        self.assertIsNone(self.consumed(path))

    def test_an_unreadable_transcript_does_not_raise(self):
        # A failure to claim must never cost the user their prompt.
        path = self.register("2026-08-10-only-handover.md", "2026-08-10T10:00:00")
        self.run_hook(os.path.join(self._tmp, "does-not-exist.jsonl"))
        self.assertIsNone(self.consumed(path))



class TestSignalLabels(unittest.TestCase):

    def test_labels_name_exactly_the_boundaries_watch_lib_reports(self):
        # No client or topic signal (ruling: no client codes in the toolkit).
        self.assertEqual(set(hook.SIGNAL_LABELS),
                         {"pr_merged", "idle_gap", "phase_done"})

    def test_profile_description_says_a_prompt_claims_it(self):
        # SessionStart only announces; a prompt naming the file claims it.
        profiles = os.path.join(TOOL_DIR, "..", "..", "claude", "profiles.json")
        with open(profiles, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("when a prompt in this session names its file", text)
        self.assertNotIn("when this session reads it", text)

class TestEnabledGate(unittest.TestCase):
    """`thread_handover.enabled` gates claude-thread-watch's main() inline,
    the way `claude-memory-routing` does. Run through subprocess with HOME
    redirected -- INDEX_PATH and STATE_DIR are hardcoded under ~/.claude, so a
    real subprocess run must never touch this machine's real handovers or
    session state. The claim path itself is exercised in full above; these
    only prove the gate's own boolean logic."""

    HOOK = os.path.join(TOOL_DIR, "claude-thread-watch")

    def _armed_transcript(self, tmp):
        """One assistant turn carrying enough usage tokens to arm, at the
        default window (no `model` field -> DEFAULT_WINDOW, arm_at=70_000)."""
        path = os.path.join(tmp, "armed.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant",
                           "usage": {"input_tokens": 100_000},
                           "content": [{"type": "text", "text": "ok"}]},
            }) + "\n")
        return path

    def _run(self, config_blob, tmp, session_id):
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w") as handle:
            json.dump(config_blob, handle)
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = config_path
        env["HOME"] = tmp  # isolate INDEX_PATH / STATE_DIR from the real machine
        payload = json.dumps({"session_id": session_id, "cwd": tmp,
                              "transcript_path": self._armed_transcript(tmp)})
        return subprocess.run([sys.executable, self.HOOK], input=payload, env=env,
                              capture_output=True, text=True, timeout=20)

    def test_disabled_exits_zero_with_empty_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run({"thread_handover": {"enabled": False}}, tmp, "gate-off")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")

    def test_key_absent_still_reaches_the_normal_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run({}, tmp, "gate-absent")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("context", proc.stdout.lower())

    def test_a_non_boolean_value_still_reaches_the_normal_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = self._run({"thread_handover": {"enabled": "no"}}, tmp, "gate-garbage")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("context", proc.stdout.lower())


class TestOutstandingGate(unittest.TestCase):
    """The whole hook, end to end: criteria 8 and 9. An armed transcript over
    the threshold must stay silent when the checkout has nothing outstanding,
    and must speak when it does.

    HOME is redirected in every run here -- STATE_DIR, INDEX_PATH, and
    watch_lib.CLAIMS_PATH are all computed at import time from ~/, and a
    subprocess run of this hook must never touch this machine's real session
    state, handovers, or session-claim data."""

    HOOK = os.path.join(TOOL_DIR, "claude-thread-watch")

    def _armed_transcript(self, tmp):
        """One assistant turn carrying enough usage tokens to arm at the
        default window (no `model` -> DEFAULT_WINDOW, arm_at=70_000), and no
        TodoWrite anywhere -- so the outstanding-work read comes only from
        the checkout."""
        path = os.path.join(tmp, "armed.jsonl")
        with open(path, "w") as handle:
            handle.write(json.dumps({
                "type": "assistant",
                "message": {"role": "assistant",
                           "usage": {"input_tokens": 100_000},
                           "content": [{"type": "text", "text": "ok"}]},
            }) + "\n")
        return path

    def _init_repo(self, path):
        os.makedirs(path, exist_ok=True)
        for args in (["init", "-q"], ["checkout", "-q", "-b", "main"],
                     ["config", "user.email", "t@example.com"],
                     ["config", "user.name", "Test"]):
            subprocess.run(["git"] + args, cwd=path, check=True,
                           capture_output=True)
        with open(os.path.join(path, "f.txt"), "w") as handle:
            handle.write("hi\n")
        subprocess.run(["git", "add", "."], cwd=path, check=True,
                       capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path,
                       check=True, capture_output=True)

    def _run(self, tmp, cwd, session_id):
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(tmp, "no-config.json")
        env["HOME"] = tmp
        payload = json.dumps({"session_id": session_id, "cwd": cwd,
                              "transcript_path": self._armed_transcript(tmp)})
        # The hook itself, run as __main__, with watch_lib pre-imported and its
        # 1.0s GIT_TIMEOUT raised: under load a fixture git call overran it,
        # read as unknown, and the dirty case went silent.
        # The hook's own `import watch_lib` reuses this module object.
        runner = ("import runpy, sys; sys.path.insert(0, sys.argv[1]); "
                  "import watch_lib; watch_lib.GIT_TIMEOUT = 30.0; "
                  "sys.argv = [sys.argv[2]]; "
                  "runpy.run_path(sys.argv[0], run_name='__main__')")
        return subprocess.run([sys.executable, "-c", runner, TOOL_DIR, self.HOOK],
                              input=payload, env=env, capture_output=True,
                              text=True, timeout=60)

    def test_clean_non_ahead_checkout_prints_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            self._init_repo(repo)
            proc = self._run(tmp, repo, "gate-clean")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")

    def test_dirty_checkout_prints_the_suggestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = os.path.join(tmp, "repo")
            self._init_repo(repo)
            with open(os.path.join(repo, "scratch.txt"), "w") as handle:
                handle.write("wip\n")
            proc = self._run(tmp, repo, "gate-dirty")
            self.assertEqual(proc.returncode, 0)
            self.assertIn("context", proc.stdout.lower())

    def test_malformed_input_still_exits_zero_and_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(tmp, "no-config.json")
            env["HOME"] = tmp
            proc = subprocess.run([sys.executable, self.HOOK], input="not json",
                                  env=env, capture_output=True, text=True,
                                  timeout=20)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")


if __name__ == "__main__":
    unittest.main()
