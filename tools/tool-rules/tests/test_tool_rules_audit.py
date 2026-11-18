"""Tests for tool_rules_lib.audit(): judging earlier sessions from transcripts."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

import tool_rules_lib as lib  # noqa: E402

EXE = os.path.join(TOOL_DIR, "claude-tool-rules")
DAY = 86400
MCP = "mcp__claude_ai_Tracker__tracker_get_task"


def stamp(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch))


def page_text(patterns):
    items = "\n".join("  - %s" % p for p in patterns)
    return "---\ntools:\n%s\n---\n\nRULES\n" % items


class AuditBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.home = os.path.join(self.tmp, "home")
        self.ws = os.path.join(self.tmp, "ws")
        self.state = os.path.join(self.tmp, "state")
        self.pages = os.path.join(self.ws, ".claude", "tool-rules")
        for d in (self.home, self.ws, self.state, self.pages):
            os.makedirs(d)
        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CONFIG_DIR"}
        env["CLAUDE_TOOL_RULES_STATE_DIR"] = self.state
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.now = time.time()
        self.chmodded = []

    def tearDown(self):
        for path in self.chmodded:
            os.chmod(path, 0o600)
        self._tmp.cleanup()

    def can_deny(self):
        return os.name != "nt" and os.geteuid() != 0

    def write_page(self, name="a.md", patterns=("Bash(*gads*)",), age=2 * DAY):
        path = os.path.join(self.pages, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(page_text(patterns))
        when = self.now - age
        os.utime(path, (when, when))
        return path

    def fired(self, sid):
        folder = os.path.join(self.home, ".claude", "claude-code-toolkit", "heartbeat")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, sid + ".fired"), "w") as fh:
            fh.write("fired_at=1\n")

    def state_file(self, sid):
        with open(os.path.join(self.state, sid + ".json"), "w") as fh:
            json.dump({"emitted": []}, fh)

    def transcript(self, sid, calls=None, project="proj-a", mtime=None, extra_lines=()):
        """calls: list of (name, input, is_error, age_seconds). Returns the path."""
        if calls is None:
            calls = [("Bash", {"command": "gads pull"}, False, DAY)]
        folder = os.path.join(self.home, ".claude", "projects", project)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid + ".jsonl")
        first = self.now - max(c[3] for c in calls)
        lines = [{"type": "user", "timestamp": stamp(first),
                  "message": {"role": "user", "content": "hello"}}]
        lines.extend(extra_lines)
        for index, (name, tool_input, is_error, age) in enumerate(calls):
            tool_id = "toolu_%s_%d" % (sid, index)
            when = stamp(self.now - age)
            lines.append({"type": "assistant", "timestamp": when,
                          "message": {"role": "assistant", "content": [
                              {"type": "tool_use", "id": tool_id, "name": name,
                               "input": tool_input}]}})
            lines.append({"type": "user", "timestamp": when,
                          "message": {"role": "user", "content": [
                              {"type": "tool_result", "tool_use_id": tool_id,
                               "is_error": is_error, "content": "ok"}]}})
        with open(path, "w") as fh:
            fh.write("".join(json.dumps(line) + "\n" for line in lines))
        when = self.now - min(c[3] for c in calls) if mtime is None else mtime
        os.utime(path, (when, when))
        return path

    def audit(self, **kwargs):
        args = dict(workspace_root=self.ws, exclude_session="current",
                    deadline=time.monotonic() + 60, now=self.now)
        args.update(kwargs)
        return lib.audit(self.home, **args)

    def flagged_sessions(self, result):
        return [f["session"] for f in result["flagged"]]


class TestAudit(AuditBase):
    def test_flags_matching_call_without_state(self):
        self.write_page()
        self.fired("s1")
        self.transcript("s1")
        result = self.audit()
        self.assertTrue(result["active"])
        self.assertEqual(len(result["flagged"]), 1)
        entry = result["flagged"][0]
        self.assertEqual(entry["session"], "s1")
        self.assertEqual(entry["calls"], 1)
        self.assertEqual(entry["pages"], ["a.md"])
        self.assertEqual(result["unchecked"], [])

    def test_state_file_present_is_healthy(self):
        self.write_page()
        self.fired("s1")
        path = self.transcript("s1")
        self.state_file("s1")
        if self.can_deny():
            os.chmod(path, 0)
            self.chmodded.append(path)
        result = self.audit()
        self.assertEqual(result["flagged"], [])
        self.assertEqual(result["unchecked"], [])

    def test_no_fired_record_is_not_judged(self):
        self.write_page()
        self.transcript("s1")
        result = self.audit()
        self.assertEqual(result["flagged"], [])
        self.assertEqual(result["judged"], {})

    def test_call_before_page_went_live_does_not_count(self):
        self.write_page()
        self.fired("s1")
        self.transcript("s1", calls=[("Bash", {"command": "gads pull"}, False, 3 * DAY)],
                        mtime=self.now - DAY)
        self.assertEqual(self.audit()["flagged"], [])
        self.transcript("s1", calls=[("Bash", {"command": "gads pull"}, False, 3 * DAY),
                                     ("Bash", {"command": "gads pull"}, False, DAY)])
        self.assertEqual(self.flagged_sessions(self.audit()), ["s1"])

    def test_errored_call_does_not_count(self):
        self.write_page()
        self.fired("s1")
        self.transcript("s1", calls=[("Bash", {"command": "gads pull"}, True, DAY)])
        self.assertEqual(self.audit()["flagged"], [])

    def test_non_matching_calls_do_not_count(self):
        self.write_page()
        self.fired("s1")
        self.transcript("s1", calls=[("Bash", {"command": "ls"}, False, DAY)])
        self.assertEqual(self.audit()["flagged"], [])
        # A tool outside HOOK_MATCHER is never judged: the hook cannot have seen it.
        self.write_page("a.md", patterns=("Edit",))
        self.fired("s2")
        self.transcript("s2", calls=[("Edit", {"file_path": "/x"}, False, DAY)])
        self.assertEqual(self.audit()["flagged"], [])

    def test_read_grep_glob_pages_match(self):
        self.write_page(patterns=("Read(*job-automation*)", "Grep(*job-automation*)",
                                  "Glob(*job-automation*)"))
        calls = {"s1": ("Read", {"file_path": "/Users/x/job-automation/app.ts"}),
                 "s2": ("Grep", {"pattern": "def", "path": "/Users/x/job-automation"}),
                 "s3": ("Glob", {"pattern": "**/job-automation/**/*.md"})}
        for sid, (name, tool_input) in calls.items():
            self.fired(sid)
            self.transcript(sid, calls=[(name, tool_input, False, DAY)])
        self.fired("s4")
        self.transcript("s4", calls=[("Grep", {"pattern": "job-automation"}, False, DAY)])
        self.assertEqual(sorted(self.flagged_sessions(self.audit())), ["s1", "s2", "s3"])

    def test_mcp_page_matches(self):
        self.write_page(patterns=("mcp__claude_ai_Tracker__*",))
        self.fired("s1")
        self.transcript("s1", calls=[(MCP, {"task_id": "1"}, False, DAY)])
        self.assertEqual(self.flagged_sessions(self.audit()), ["s1"])

    def test_inactive_without_pages(self):
        self.fired("s1")
        self.transcript("s1")
        result = self.audit(workspace_root=None)
        self.assertEqual(result, {"active": False, "flagged": [], "unchecked": [],
                                  "judged": {}})
        with open(os.path.join(self.pages, "junk.md"), "w") as fh:
            fh.write("no frontmatter")
        result = self.audit()
        self.assertFalse(result["active"])
        self.assertEqual(result["flagged"], [])

    def test_window(self):
        self.write_page(age=10 * DAY)
        self.fired("old")
        self.transcript("old", mtime=self.now - 8 * DAY)
        result = self.audit()
        self.assertEqual(result["flagged"], [])
        self.assertNotIn("old", result["judged"])
        self.fired("fresh")
        self.transcript("fresh", calls=[("Bash", {"command": "gads pull"}, False, 7 * DAY)],
                        mtime=self.now)
        result = self.audit()
        self.assertEqual(result["flagged"], [])
        self.assertIn("fresh", result["judged"])

    def test_cache_hit_and_signature_invalidation(self):
        self.write_page()
        self.fired("s1")
        path = self.transcript("s1")
        first = self.audit()
        self.assertEqual(self.flagged_sessions(first), ["s1"])
        if not self.can_deny():
            self.skipTest("cannot deny reads as this user")
        os.chmod(path, 0)
        self.chmodded.append(path)
        second = self.audit(cache=first["judged"])
        self.assertEqual(self.flagged_sessions(second), ["s1"])
        self.assertEqual(second["unchecked"], [])
        self.write_page("b.md", patterns=("Bash(zzz*)",))
        third = self.audit(cache=first["judged"])
        self.assertEqual(third["flagged"], [])
        self.assertEqual([(u["session"], u["reason"]) for u in third["unchecked"]],
                         [("s1", "unreadable")])

    def test_exclude_and_skip(self):
        self.write_page()
        self.fired("s1")
        self.transcript("s1")
        self.assertEqual(self.audit(exclude_session="s1")["flagged"], [])
        self.assertEqual(self.audit(skip={"s1"})["flagged"], [])
        self.assertEqual(self.flagged_sessions(self.audit()), ["s1"])

    def test_deadline(self):
        self.write_page()
        self.fired("s1")
        self.transcript("s1")
        result = self.audit(deadline=time.monotonic() - 1)
        self.assertEqual(result["flagged"], [])
        self.assertEqual([(u["session"], u["reason"]) for u in result["unchecked"]],
                         [("s1", "deadline")])

    def test_malformed_ids_are_skipped(self):
        self.write_page()
        self.fired("s1")
        when = stamp(self.now - DAY)

        def line(content):
            return {"type": "user", "timestamp": when,
                    "message": {"role": "user", "content": content}}
        bad = [
            line([{"type": "tool_result", "tool_use_id": [], "is_error": False}]),
            line([{"type": "tool_result", "tool_use_id": {}, "is_error": False}]),
            line([{"type": "tool_use", "id": 7, "name": "Bash",
                   "input": {"command": "gads pull"}}]),
        ]
        self.transcript("s1", extra_lines=bad)
        result = self.audit()
        self.assertEqual(self.flagged_sessions(result), ["s1"])
        self.assertEqual(result["flagged"][0]["calls"], 1)


class TestWriterAgreement(AuditBase):
    def setUp(self):
        super().setUp()
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w") as fh:
            json.dump({"workspace_root": self.ws}, fh)
        self.write_page(patterns=("Bash(*gads*)",))

    def run_exe(self, payload, args=()):
        env = dict(os.environ)
        env.update(HOME=self.home, USERPROFILE=self.home,
                   CLAUDE_TOOL_RULES_STATE_DIR=self.state,
                   CLAUDE_TOOLKIT_CONFIG=self.config)
        proc = subprocess.run([sys.executable, EXE] + list(args), input=json.dumps(payload),
                              capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc

    def hook_call(self, sid):
        proc = self.run_exe({"session_id": sid, "tool_name": "Bash",
                             "tool_input": {"command": "gads pull"}})
        self.assertIn("RULES", proc.stdout)

    def test_same_location_as_the_writer(self):
        for sid in ("S", "T"):
            self.fired(sid)
            self.transcript(sid)
        self.hook_call("S")
        self.assertTrue(os.path.isfile(os.path.join(self.state, "S.json")))
        self.assertEqual(self.flagged_sessions(self.audit()), ["T"])

    def test_compaction_end_to_end(self):
        self.fired("S")
        self.transcript("S")
        self.hook_call("S")
        self.run_exe({"session_id": "S", "source": "compact"}, args=("--reset",))
        self.assertEqual(self.audit()["flagged"], [])


if __name__ == "__main__":
    unittest.main()
