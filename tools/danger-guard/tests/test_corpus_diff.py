#!/usr/bin/env python3
"""Tests for corpus_diff.py.

Runs the real harness as a subprocess (black-box, like test_guard.py) against
throwaway transcript directories and stub guards written for each test. No
test reads or writes anything outside its own temp directory, and no test
depends on a real claude-danger-guard build.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

CORPUS_DIFF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "corpus_diff.py")

STUB_TEMPLATE = """#!/usr/bin/env python3
import json, sys
payload = json.loads(sys.stdin.read())
command = payload.get("tool_input", {}).get("command", "")
%s
"""

ALLOW_ALL = "sys.exit(0)"
EXIT_127 = "sys.exit(127)"
CRASH_STUB = ('print(json.dumps({"systemMessage": '
             '"claude-danger-guard crashed: RuntimeError: boom"}))\n'
             'sys.exit(0)')


def _block_on(needle):
    return "if %r in command:\n    sys.exit(2)\nsys.exit(0)" % (needle,)


def write_stub(path, body):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(STUB_TEMPLATE % body)


def write_jsonl(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def bash_line(cwd, command):
    return json.dumps({"type": "assistant", "cwd": cwd,
                       "message": {"content": [
                           {"type": "tool_use", "name": "Bash",
                            "input": {"command": command}}]}})


def edit_line(cwd, name, file_path):
    return json.dumps({"type": "assistant", "cwd": cwd,
                       "message": {"content": [
                           {"type": "tool_use", "name": name,
                            "input": {"file_path": file_path}}]}})


def run_corpus_diff(reference, candidate, dirs, limit=None, tools=None,
                    crash_marker=None, env=None):
    """Invoke corpus_diff.py as a subprocess; return (returncode, stdout)."""
    argv = [sys.executable, CORPUS_DIFF,
           "--reference", reference, "--candidate", candidate]
    if limit is not None:
        argv += ["--limit", str(limit)]
    if tools is not None:
        argv += ["--tools", tools]
    if crash_marker is not None:
        argv += ["--crash-marker", crash_marker]
    argv += list(dirs)
    done = subprocess.run(argv, capture_output=True, text=True, env=env)
    return done.returncode, done.stdout


class CorpusDiffTestCase(unittest.TestCase):
    """Base: a temp dir per test, torn down automatically."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def make_stub(self, name, body):
        path = os.path.join(self.root, name)
        write_stub(path, body)
        return path

    def make_corpus(self):
        """The Build's fixture: one malformed line, one without cwd, a
        duplicate, and a subagent file in a nested dir. Two distinct
        (command, cwd) pairs should survive: "echo two" and "echo three".
        """
        corpus = os.path.join(self.root, "corpus")
        write_jsonl(os.path.join(corpus, "session.jsonl"), [
            "{not valid json",
            json.dumps({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Bash",
                 "input": {"command": "echo one"}}]}}),  # no top-level cwd
            bash_line("/work", "echo two"),
            bash_line("/work", "echo two"),  # duplicate, deduped
        ])
        write_jsonl(os.path.join(corpus, "subagents", "agent-1.jsonl"), [
            bash_line("/work2", "echo three"),
        ])
        return corpus


class TestAgreeingGuardsPass(CorpusDiffTestCase):

    def test_agreeing_stubs_pass_and_count_is_exact(self):
        corpus = self.make_corpus()
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", ALLOW_ALL)
        code, stdout = run_corpus_diff(reference, candidate, [corpus])
        self.assertEqual(code, 0)
        self.assertIn("commands judged: 2", stdout)


class TestDisagreementFails(CorpusDiffTestCase):

    def test_disagreement_is_reported_and_fails(self):
        corpus = self.make_corpus()
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", _block_on("echo two"))
        code, stdout = run_corpus_diff(reference, candidate, [corpus])
        self.assertEqual(code, 1)
        self.assertIn("echo two", stdout)


class TestCandidateCrashIsNotAnAgreement(CorpusDiffTestCase):

    def test_candidate_crash_counted_not_agreement(self):
        corpus = self.make_corpus()
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", CRASH_STUB)
        code, stdout = run_corpus_diff(reference, candidate, [corpus])
        self.assertEqual(code, 1)
        # Exit codes agree (both 0) -- the crash must not be swallowed by
        # that agreement.
        self.assertIn("disagreements: 0", stdout)
        self.assertIn("candidate crashes: 2", stdout)


class TestEmptyCorpusFails(CorpusDiffTestCase):

    def test_empty_corpus_fails(self):
        corpus = os.path.join(self.root, "empty-corpus")
        write_jsonl(os.path.join(corpus, "session.jsonl"), [
            json.dumps({"type": "assistant", "cwd": "/work",
                       "message": {"content": [
                           {"type": "text", "text": "no bash here"}]}}),
        ])
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", ALLOW_ALL)
        code, stdout = run_corpus_diff(reference, candidate, [corpus])
        self.assertEqual(code, 1)
        self.assertIn("commands judged: 0", stdout)


class TestAgreeingOddExitCodesFail(CorpusDiffTestCase):

    def test_agreeing_127s_still_fail(self):
        corpus = self.make_corpus()
        reference = self.make_stub("reference.py", EXIT_127)
        candidate = self.make_stub("candidate.py", EXIT_127)
        code, stdout = run_corpus_diff(reference, candidate, [corpus])
        self.assertEqual(code, 1)
        self.assertIn("disagreements: 0", stdout)
        self.assertIn("exit codes outside {0, 2}: 2", stdout)


class TestMalformedShapesDoNotCrash(CorpusDiffTestCase):
    """message.content absent, a string, or carrying non-Bash/empty-command
    blocks must all be skipped rather than raising -- corpus_diff.py must
    never itself produce a traceback on transcript-shaped noise."""

    def test_malformed_lines_do_not_crash_the_harness(self):
        corpus = os.path.join(self.root, "noisy-corpus")
        write_jsonl(os.path.join(corpus, "session.jsonl"), [
            "not json at all",
            json.dumps({"type": "assistant", "cwd": "/work",
                       "message": {"content": "just a string"}}),
            json.dumps({"type": "assistant", "cwd": "/work",
                       "message": {}}),
            json.dumps({"type": "assistant", "cwd": "/work", "message": {
                "content": [{"type": "text", "text": "hi"}]}}),
            json.dumps({"type": "assistant", "cwd": "/work", "message": {
                "content": [{"type": "tool_use", "name": "Read",
                            "input": {"file_path": "/x"}}]}}),
            json.dumps({"type": "assistant", "cwd": "/work", "message": {
                "content": [{"type": "tool_use", "name": "Bash",
                            "input": {"command": ""}}]}}),
            json.dumps({"type": "assistant", "cwd": "/work", "message": {
                "content": [{"type": "tool_use", "name": "Bash",
                            "input": {"command": 5}}]}}),
        ])
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", ALLOW_ALL)
        done = subprocess.run(
            [sys.executable, CORPUS_DIFF, "--reference", reference,
             "--candidate", candidate, corpus],
            capture_output=True, text=True)
        self.assertEqual(done.stderr, "")
        self.assertIn("commands judged: 0", done.stdout)
        self.assertEqual(done.returncode, 1)  # zero judged, not a crash


class TestLimitCapsCommandsJudged(CorpusDiffTestCase):

    def test_limit_caps_the_judged_count(self):
        corpus = self.make_corpus()
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", ALLOW_ALL)
        code, stdout = run_corpus_diff(reference, candidate, [corpus],
                                       limit=1)
        self.assertEqual(code, 0)
        self.assertIn("commands judged: 1", stdout)


class TestEditFamilyOptInByTool(CorpusDiffTestCase):
    """An Edit payload is only collected when --tools names it -- the
    default (Bash only) must not see it at all."""

    def make_edit_only_corpus(self):
        corpus = os.path.join(self.root, "edit-corpus")
        write_jsonl(os.path.join(corpus, "session.jsonl"), [
            edit_line("/work", "Edit", "/work/file.txt"),
        ])
        return corpus

    def test_edit_payload_ignored_by_default_tools(self):
        corpus = self.make_edit_only_corpus()
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", ALLOW_ALL)
        code, stdout = run_corpus_diff(reference, candidate, [corpus])
        # default --tools is Bash; the Edit block is invisible, so zero
        # commands are judged and the run fails on that count alone.
        self.assertEqual(code, 1)
        self.assertIn("commands judged: 0", stdout)

    def test_edit_payload_collected_when_tools_names_it(self):
        corpus = self.make_edit_only_corpus()
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", ALLOW_ALL)
        code, stdout = run_corpus_diff(reference, candidate, [corpus],
                                       tools="Edit")
        self.assertEqual(code, 0)
        self.assertIn("commands judged: 1", stdout)


class TestMonitorIsJudgedLikeBash(CorpusDiffTestCase):
    """A Monitor block is a shell command, so --tools Monitor collects it
    like Bash; a WebSocket-only Monitor has no command and yields nothing."""

    def test_describe_payload_returns_the_command_text(self):
        sys.path.insert(0, os.path.dirname(CORPUS_DIFF))
        import corpus_diff
        self.assertEqual(
            corpus_diff._describe_payload("Monitor", {"command": "rm -rf x"}),
            "rm -rf x")

    def test_command_monitor_is_judged_and_websocket_monitor_is_not(self):
        corpus = os.path.join(self.root, "monitor-corpus")
        write_jsonl(os.path.join(corpus, "session.jsonl"), [
            json.dumps({"type": "assistant", "cwd": "/work",
                        "message": {"content": [
                            {"type": "tool_use", "name": "Monitor",
                             "input": {"command": "rm -rf x"}},
                            {"type": "tool_use", "name": "Monitor",
                             "input": {"ws": "wss://x", "description": "d"}}]}}),
        ])
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", _block_on("rm -rf x"))
        code, stdout = run_corpus_diff(reference, candidate, [corpus],
                                       tools="Monitor")
        self.assertEqual(code, 1)
        self.assertIn("commands judged: 1", stdout)
        self.assertIn("rm -rf x", stdout)


class TestCrashMarkerIsConfigurable(CorpusDiffTestCase):
    """A candidate printing claude-worktree-guard's crash marker with exit 0
    is counted as a crash only when --crash-marker names that text."""

    WORKTREE_CRASH_STUB = (
        'print(json.dumps({"systemMessage": '
        '"claude-worktree-guard crashed and did NOT check this command: '
        'RuntimeError: boom"}))\n'
        'sys.exit(0)')

    def test_unrecognised_marker_text_is_not_counted_as_a_crash(self):
        corpus = self.make_corpus()
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", self.WORKTREE_CRASH_STUB)
        code, stdout = run_corpus_diff(reference, candidate, [corpus])
        # default --crash-marker is the danger-guard's own text, which is
        # not a substring of the worktree-guard's crash message.
        self.assertEqual(code, 0)
        self.assertIn("candidate crashes: 0", stdout)

    def test_named_marker_text_is_counted_as_a_crash(self):
        corpus = self.make_corpus()
        reference = self.make_stub("reference.py", ALLOW_ALL)
        candidate = self.make_stub("candidate.py", self.WORKTREE_CRASH_STUB)
        code, stdout = run_corpus_diff(
            reference, candidate, [corpus],
            crash_marker="claude-worktree-guard crashed")
        self.assertEqual(code, 1)
        self.assertIn("candidate crashes: 2", stdout)


class TestEscapeHatchEnvVarsDoNotLeakToGuards(CorpusDiffTestCase):
    """A reference that blocks everything unless the leaked worktree-guard
    escape hatch is set, against a candidate that always allows, must still
    disagree even when the CALLER's own environment carries
    WORKTREE_ALLOW=1 and WT_ALLOW_MAIN=1 -- corpus_diff must strip both
    before spawning either guard. Both stubs honour the escape hatch (the
    candidate trivially, since it already always allows), so a leak that
    flips the reference to allow as well collapses the disagreement to
    zero and makes the leak observable."""

    ESCAPE_HATCH_CHECK = (
        'import os\n'
        'if os.environ.get("WORKTREE_ALLOW") == "1" '
        'or os.environ.get("WT_ALLOW_MAIN") == "1":\n'
        '    sys.exit(0)\n')

    def test_disagreement_survives_a_leaked_caller_environment(self):
        corpus = self.make_corpus()
        reference = self.make_stub(
            "reference.py", self.ESCAPE_HATCH_CHECK + "sys.exit(2)")
        candidate = self.make_stub(
            "candidate.py", self.ESCAPE_HATCH_CHECK + "sys.exit(0)")
        env = dict(os.environ)
        env["WORKTREE_ALLOW"] = "1"
        env["WT_ALLOW_MAIN"] = "1"
        code, stdout = run_corpus_diff(reference, candidate, [corpus],
                                       env=env)
        self.assertEqual(code, 1)
        self.assertIn("disagreements: 2", stdout)


if __name__ == "__main__":
    unittest.main(verbosity=1)
