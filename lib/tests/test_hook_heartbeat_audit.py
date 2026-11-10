#!/usr/bin/env python3
"""lib/hook_heartbeat.py's audit: which past sessions ran a shell command yet left no
`.fired` heartbeat record, judged from their transcripts. Hermetic: a temp HOME, a
temp CLAUDE_CONFIG_DIR, real-shape transcript lines, and a fixed `now`.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, LIB)
import hook_heartbeat as hb  # noqa: E402
import install_record as ir  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.abspath(LIB))
ERA = "2026-10-01T00:00:00Z"
NOW = datetime.datetime(2026, 10, 13, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()
DAY = 86400
FAR = 10 ** 12  # a monotonic deadline that never passes
ITEM_KEYS = {
    "flagged": {"session", "project", "started_at", "ran"},
    "unchecked": {"session", "project", "reason", "size", "started_at"},
    "judged": {"mtime", "size", "verdict"},
}


def can_chmod_deny():
    return os.name != "nt" and os.geteuid() != 0


class AuditTestCase(unittest.TestCase):

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = os.path.join(self.root, "home")
        self.cfg = os.path.join(self.root, "cfg")
        os.makedirs(os.path.join(self.home, hb.RECORD_DIR))
        patcher = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.cfg})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.write_era(ERA)

    def write_era(self, stamp):
        ir.finish(ir.record_path(self.home), "/checkout", "abc123", ["core"], stamp)

    def projects(self, base=None):
        return os.path.join(base or self.cfg, "projects")

    def transcript(self, sid, project="proj-a", start="2026-10-10T08:00:00.000Z",
                   tools=(("Bash", False, "ok"),), mtime=None, base=None, list_form=False):
        """Write a real-shape transcript: user line, then per tool an assistant tool_use
        line and a user tool_result line. Returns the path."""
        folder = os.path.join(self.projects(base), project)
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
            content = [{"type": "text", "text": text}] if list_form else text
            lines.append({"type": "user", "timestamp": start,
                          "message": {"role": "user", "content": [
                              {"type": "tool_result", "tool_use_id": tool_id,
                               "is_error": is_error, "content": content}]}})
        with open(path, "w") as handle:
            handle.write("".join(json.dumps(line) + "\n" for line in lines))
        self.touch(path, mtime)
        return path

    def touch(self, path, mtime=None):
        when = NOW - DAY if mtime is None else mtime
        os.utime(path, (when, when))

    def fired(self, sid):
        with open(os.path.join(self.home, hb.RECORD_DIR, sid + ".fired"), "w") as handle:
            handle.write("fired_at=1\n")

    def audit(self, **kwargs):
        kwargs.setdefault("exclude_session", "current")
        kwargs.setdefault("deadline", FAR)
        kwargs.setdefault("now", NOW)
        return hb.audit(self.home, **kwargs)

    def flagged_ids(self, result):
        return [item["session"] for item in result["flagged"]]

    def deny(self, path):
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o755 if os.path.isdir(path) else 0o644)


class TestAudit(AuditTestCase):

    def test_1_ran_without_fired_is_flagged(self):
        self.transcript("s1")
        result = self.audit()
        self.assertEqual(self.flagged_ids(result), ["s1"])
        self.assertEqual(result["flagged"][0]["ran"], 1)
        self.assertEqual(result["flagged"][0]["project"], "proj-a")
        self.assertEqual(result["flagged"][0]["started_at"], "2026-10-10T08:00:00.000Z")
        self.assertEqual(result["judged"]["s1"]["verdict"], "flagged")
        self.assertEqual(result["unchecked"], [])

    def test_2_fired_record_skips_without_a_read(self):
        self.transcript("s1")
        self.fired("s1")
        result = self.audit()
        self.assertEqual(result["flagged"], [])
        self.assertNotIn("s1", result["judged"])

    def test_3_refused_call_is_not_flagged(self):
        self.transcript("s1", tools=(("Bash", True, "Hook PreToolUse:Bash denied"),))
        result = self.audit()
        self.assertEqual(result["flagged"], [])
        self.assertEqual(result["judged"]["s1"]["verdict"], "ok")

    def test_4_nonzero_exit_counts_as_ran(self):
        self.transcript("s1", tools=(("Bash", True, "Exit code 1\nboom"),))
        self.assertEqual(self.flagged_ids(self.audit()), ["s1"])

    def test_5_start_outside_the_window_is_not_flagged(self):
        before = self.transcript("before-era", start="2026-09-30T23:00:00.000Z")
        result = self.audit()
        self.assertEqual(result["flagged"], [])
        self.assertEqual(result["judged"]["before-era"]["verdict"], "ok")
        os.remove(before)
        # Older than 14 days but after the era: move the era back so the 14-day
        # bound (2026-09-29T12:00Z) is the binding one.
        os.remove(ir.record_path(self.home))  # first_installed_at is immutable
        self.write_era("2026-09-01T00:00:00Z")
        self.transcript("too-old", start="2026-09-20T08:00:00.000Z")
        result = self.audit()
        self.assertEqual(result["flagged"], [])
        self.assertEqual(result["judged"]["too-old"]["verdict"], "ok")
        self.assertEqual(result["window_start"], NOW - 14 * DAY)

    def test_6_excluded_session_is_not_judged(self):
        self.transcript("current")
        result = self.audit(exclude_session="current")
        self.assertEqual(result["flagged"], [])
        self.assertEqual(result["judged"], {})

    def test_7_no_install_record_means_no_audit(self):
        os.remove(ir.record_path(self.home))
        self.transcript("s1")
        self.assertEqual(self.audit(), {"window_start": None, "flagged": [],
                                        "unchecked": [], "judged": {}})

    def test_8_shell_tools_flag_and_others_do_not(self):
        self.transcript("ps", tools=(("PowerShell", False, "ok"),))
        self.transcript("mon", tools=(("Monitor", False, "ok"),))
        self.transcript("rd", tools=(("Read", False, "ok"),))
        result = self.audit()
        self.assertEqual(sorted(self.flagged_ids(result)), ["mon", "ps"])
        self.assertEqual(result["judged"]["rd"]["verdict"], "ok")

    def test_9_cache_hit_skips_the_read_and_a_changed_size_rejudges(self):
        path = self.transcript("s1")
        first = self.audit()
        if can_chmod_deny():
            self.deny(path)
            second = self.audit(cache=first["judged"])
            self.assertEqual(second["unchecked"], [])
            self.assertEqual(second["judged"]["s1"], first["judged"]["s1"])
            os.chmod(path, 0o644)
        # Changed size (mtime restored) is a miss: the file is judged afresh.
        with open(path, "a") as handle:
            handle.write("\n")
        self.touch(path)
        stale = {"s1": dict(first["judged"]["s1"], verdict="ok")}
        third = self.audit(cache=stale)
        self.assertEqual(self.flagged_ids(third), ["s1"])
        self.assertNotEqual(third["judged"]["s1"]["size"], first["judged"]["s1"]["size"])

    def test_10_deadline_already_past_checks_nothing(self):
        self.transcript("a")
        self.transcript("b")
        result = self.audit(deadline=-1)
        self.assertEqual(result["judged"], {})
        self.assertEqual(result["flagged"], [])
        self.assertEqual(sorted(item["session"] for item in result["unchecked"]), ["a", "b"])
        self.assertTrue(all(item["reason"] == "deadline" for item in result["unchecked"]))

    def test_11_smaller_file_is_judged_first(self):
        self.transcript("small", tools=(("Bash", False, "ok"),))
        self.transcript("large", tools=(("Bash", False, "x" * 5000),))
        calls = []

        def clock():
            calls.append(1)
            return 0 if len(calls) == 1 else 100

        with mock.patch.object(hb.time, "monotonic", side_effect=clock):
            result = self.audit(deadline=50)
        self.assertEqual(list(result["judged"]), ["small"])
        self.assertEqual([item["session"] for item in result["unchecked"]], ["large"])
        self.assertEqual(result["unchecked"][0]["reason"], "deadline")

    def test_12_deadline_mid_file_leaves_it_unchecked_and_unjudged(self):
        self.transcript("s1")
        calls = []

        def clock():
            calls.append(1)
            return 0 if len(calls) == 1 else 100

        with mock.patch.object(hb, "CHECK_EVERY_LINES", 1), \
                mock.patch.object(hb.time, "monotonic", side_effect=clock):
            result = self.audit(deadline=50)
        self.assertEqual(result["judged"], {})
        self.assertEqual(result["flagged"], [])
        self.assertEqual([(i["session"], i["reason"]) for i in result["unchecked"]],
                         [("s1", "deadline")])

    @unittest.skipUnless(can_chmod_deny(), "needs POSIX permissions and a non-root user")
    def test_13_unreadable_file_is_unchecked_and_the_scan_continues(self):
        bad = self.transcript("bad", tools=())
        self.transcript("good", tools=(("Bash", False, "x" * 500),))
        self.deny(bad)
        result = self.audit()
        self.assertEqual([(i["session"], i["reason"]) for i in result["unchecked"]],
                         [("bad", "unreadable")])
        self.assertEqual(self.flagged_ids(result), ["good"])
        self.assertNotIn("bad", result["judged"])

    def test_14_malformed_lines_and_list_content_are_tolerated(self):
        path = self.transcript("s1", tools=(("Bash", True, "Exit code 2\nboom"),),
                               list_form=True)
        with open(path, "ab") as handle:
            handle.write(b'not json "tool_use"\n{"type": "tool_result"\n\xff\xfe "tool_result"\n')
            handle.write(b'[1, 2] "tool_use"\n{"message": 5, "x": "tool_result"}\n')
        self.touch(path)
        result = self.audit()
        self.assertEqual(self.flagged_ids(result), ["s1"])
        self.assertEqual(result["unchecked"], [])

    def test_15_window_is_shorter_than_the_heartbeat_prune_age(self):
        with open(os.path.join(REPO_ROOT, "tools", "hook-heartbeat",
                               "claude-hook-heartbeat")) as handle:
            match = re.search(r"-mtime \+(\d+)", handle.read())
        self.assertIsNotNone(match)
        self.assertLess(hb.WINDOW_DAYS, int(match.group(1)))

    def test_15b_reader_finds_what_the_real_writer_writes(self):
        # The writer and this reader each name the record directory. If they
        # disagree, every session reads as hookless and every verdict "never",
        # while each side's own tests stay green.
        writer = os.path.join(REPO_ROOT, "tools", "hook-heartbeat",
                              "claude-hook-heartbeat")
        env = dict(os.environ, HOME=self.home, CLAUDE_PLUGIN_ROOT="/plugin")
        subprocess.run([writer], input=b'{"session_id": "s9"}', env=env,
                       check=True, timeout=30)
        path = os.path.join(self.home, hb.RECORD_DIR, "s9.fired")
        self.assertTrue(os.path.isfile(path), path)
        self.assertEqual(hb.read(path).get("plugin_root"), "/plugin")

    @unittest.skipUnless(can_chmod_deny(), "needs POSIX permissions and a non-root user")
    def test_16_unreadable_project_dir_is_reported_and_siblings_still_scanned(self):
        self.transcript("hidden", project="locked")
        self.transcript("seen", project="open")
        self.deny(os.path.join(self.projects(), "locked"))
        result = self.audit()
        self.assertEqual([(i["session"], i["project"], i["reason"])
                          for i in result["unchecked"]], [(None, "locked", "unreadable")])
        self.assertEqual(self.flagged_ids(result), ["seen"])

    def test_17_skipped_session_is_not_judged(self):
        self.transcript("s1")
        result = self.audit(skip=("s1",))
        self.assertEqual(result["flagged"], [])
        self.assertEqual(result["judged"], {})

    def test_a_transcript_with_no_start_is_never_flagged(self):
        """No timestamp means the session cannot be placed in the window."""
        path = self.transcript("s1")
        with open(path) as handle:
            lines = [json.loads(line) for line in handle]
        for line in lines:
            line.pop("timestamp", None)
        with open(path, "w") as handle:
            handle.write("".join(json.dumps(line) + "\n" for line in lines))
        self.touch(path)
        result = self.audit()
        self.assertEqual(result["flagged"], [])
        self.assertEqual(result["judged"]["s1"]["verdict"], "ok")

    def test_a_directory_at_the_fired_path_is_not_evidence(self):
        self.transcript("s1")
        os.makedirs(os.path.join(self.home, hb.RECORD_DIR, "s1.fired"))
        self.assertEqual(self.flagged_ids(self.audit()), ["s1"])

    def test_a_legacy_record_without_first_installed_at_still_audits(self):
        path = ir.record_path(self.home)
        with open(path) as handle:
            record = json.load(handle)
        del record["first_installed_at"]
        with open(path, "w") as handle:
            json.dump(record, handle)
        self.transcript("s1")
        result = self.audit()
        self.assertIsNotNone(result["window_start"])
        self.assertEqual(self.flagged_ids(result), ["s1"])

    def test_a_non_string_tool_use_id_is_tolerated(self):
        path = self.transcript("s1")
        with open(path, "a") as handle:
            handle.write(json.dumps({"message": {"content": [
                {"type": "tool_result", "tool_use_id": []}]}}) + "\n")
        self.touch(path)
        self.assertEqual(self.flagged_ids(self.audit()), ["s1"])

    def test_18_items_carry_exactly_the_schema_keys(self):
        self.transcript("flag")
        self.transcript("fine", tools=(("Read", False, "ok"),))
        deadline_result = self.audit(deadline=-1)
        result = self.audit()
        self.assertEqual(set(result), {"window_start", "flagged", "unchecked", "judged"})
        for item in result["flagged"]:
            self.assertEqual(set(item), ITEM_KEYS["flagged"])
        for item in deadline_result["unchecked"]:
            self.assertEqual(set(item), ITEM_KEYS["unchecked"])
        self.assertTrue(result["flagged"] and deadline_result["unchecked"])
        for entry in result["judged"].values():
            self.assertEqual(set(entry), ITEM_KEYS["judged"])

    def test_claude_home_falls_back_to_dot_claude_when_config_dir_is_unset(self):
        os.environ.pop("CLAUDE_CONFIG_DIR")
        self.transcript("s1", base=os.path.join(self.home, ".claude"))
        self.assertEqual(self.flagged_ids(self.audit()), ["s1"])

    def test_no_projects_directory_is_a_machine_with_no_history(self):
        result = self.audit()
        self.assertEqual((result["flagged"], result["unchecked"], result["judged"]),
                         ([], [], {}))
        self.assertEqual(result["window_start"], datetime.datetime(
            2026, 10, 1, tzinfo=datetime.timezone.utc).timestamp())


if __name__ == "__main__":
    unittest.main()
