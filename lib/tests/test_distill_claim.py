#!/usr/bin/env python3
"""Tests for distill_claim.py, the directory-based distillation claim.

Hermetic: HOME, USERPROFILE and CLAUDE_INDEX_DISTILL_CLAIM are sandboxed in a
temp dir for every test, so nothing touches the real ~/.claude.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

LIB = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, LIB)
import distill_claim


def _iso_ago(hours):
    stamp = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(hours=hours))
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


class ClaimCase(unittest.TestCase):
    def setUp(self):
        self.sandbox = tempfile.mkdtemp(prefix="distill-claim-")
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.claim_path = os.path.join(self.sandbox, "sub", "claim")
        saved = {k: os.environ.get(k) for k in
                 ("HOME", "USERPROFILE", "CLAUDE_INDEX_DISTILL_CLAIM")}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ["HOME"] = self.sandbox
        os.environ["USERPROFILE"] = self.sandbox
        os.environ["CLAUDE_INDEX_DISTILL_CLAIM"] = self.claim_path

    def write_holder(self, session_id, hours_ago):
        os.makedirs(self.claim_path, exist_ok=True)
        with open(os.path.join(self.claim_path, "holder.json"), "w") as fh:
            json.dump({"session_id": session_id,
                       "claimed_at": _iso_ago(hours_ago)}, fh)

    def age_dir(self, hours):
        old = time.time() - hours * 3600
        os.utime(self.claim_path, (old, old))


class TestClaimDir(ClaimCase):
    def test_env_override(self):
        self.assertEqual(distill_claim.claim_dir(), self.claim_path)

    def test_default_under_home(self):
        del os.environ["CLAUDE_INDEX_DISTILL_CLAIM"]
        self.assertEqual(
            distill_claim.claim_dir(),
            os.path.join(os.path.expanduser("~"), ".claude", "index-distill",
                         "claim"))

    def test_stale_constant(self):
        self.assertEqual(distill_claim.STALE_SECONDS, 24 * 3600)


class TestB8(ClaimCase):
    def test_first_claim_wins_and_writes_holder(self):
        ok, holder = distill_claim.claim("a")
        self.assertTrue(ok)
        self.assertEqual(holder["session_id"], "a")
        self.assertRegex(holder["claimed_at"],
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        on_disk = json.load(open(os.path.join(self.claim_path, "holder.json")))
        self.assertEqual(on_disk, holder)

    def test_second_session_is_refused_with_the_holder(self):
        distill_claim.claim("a")
        ok, holder = distill_claim.claim("b")
        self.assertFalse(ok)
        self.assertEqual(holder["session_id"], "a")

    def test_same_session_reclaims(self):
        distill_claim.claim("a")
        ok, holder = distill_claim.claim("a")
        self.assertTrue(ok)
        self.assertEqual(holder["session_id"], "a")

    def test_stale_claim_is_taken(self):
        self.write_holder("a", 25)
        ok, holder = distill_claim.claim("b")
        self.assertTrue(ok)
        self.assertEqual(holder["session_id"], "b")
        leftovers = [n for n in os.listdir(os.path.dirname(self.claim_path))
                     if ".stale-" in n]
        self.assertEqual(leftovers, [])

    def test_read_live_stale_is_none_fresh_is_dict(self):
        self.assertIsNone(distill_claim.read_live())
        self.write_holder("a", 25)
        self.assertIsNone(distill_claim.read_live())
        shutil.rmtree(self.claim_path)
        self.write_holder("a", 1)
        self.assertEqual(distill_claim.read_live()["session_id"], "a")

    def test_release_by_non_owner_is_false_and_dir_remains(self):
        distill_claim.claim("a")
        self.assertFalse(distill_claim.release("b"))
        self.assertTrue(os.path.isdir(self.claim_path))

    def test_e2_release_never_deletes_a_successors_claim(self):
        from unittest import mock
        self.write_holder("new", 0)
        with mock.patch.object(distill_claim, "read_live",
                               return_value={"session_id": "old",
                                             "claimed_at": _iso_ago(0)}):
            self.assertFalse(distill_claim.release("old"))
        with open(os.path.join(self.claim_path, "holder.json")) as fh:
            self.assertEqual(json.load(fh)["session_id"], "new")

    def tombstones(self):
        return [n for n in os.listdir(os.path.dirname(self.claim_path))
                if ".stale-" in n]

    def test_mr1_stale_takeover_never_removes_a_fresh_claim(self):
        """A sees the claim stale; before A renames it, B takes it over and
        holds a fresh claim. A's rename must not remove B's: exactly one wins."""
        from unittest import mock
        self.write_holder("a-old", 25)
        real = distill_claim.read_live
        result = {}

        def interleaved(now=None):
            seen = real(now)
            if "b" not in result:
                with mock.patch.object(distill_claim, "read_live", real):
                    result["b"] = distill_claim.claim("b")[0]
            return seen

        with mock.patch.object(distill_claim, "read_live", interleaved):
            ok_a, holder = distill_claim.claim("a")
        self.assertTrue(result["b"])
        self.assertFalse(ok_a)
        self.assertEqual(holder["session_id"], "b")
        self.assertEqual(real()["session_id"], "b")
        self.assertEqual(self.tombstones(), [])

    def test_mr1_non_owner_release_never_moves_the_claim(self):
        from unittest import mock
        distill_claim.claim("a")
        real_rename = os.rename
        calls = []

        def recording(src, dst):
            calls.append((src, dst))
            return real_rename(src, dst)

        with mock.patch.object(distill_claim.os, "rename", recording):
            self.assertFalse(distill_claim.release("b"))
        self.assertEqual(calls, [])
        self.assertEqual(distill_claim.read_live()["session_id"], "a")

    def test_mr1_release_puts_back_a_claim_replaced_after_the_read(self):
        """The pre-read says 'mine' but a successor replaced the claim before
        the rename: the tombstone re-read must catch it and put it back."""
        from unittest import mock
        self.write_holder("new", 0)
        real = distill_claim._holder_is
        calls = []

        def stale_first(path, session_id):
            calls.append(path)
            return True if len(calls) == 1 else real(path, session_id)

        with mock.patch.object(distill_claim, "_holder_is", stale_first):
            self.assertFalse(distill_claim.release("old"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(distill_claim.read_live()["session_id"], "new")
        self.assertEqual(self.tombstones(), [])

    def test_release_by_owner_removes_the_dir(self):
        distill_claim.claim("a")
        self.assertTrue(distill_claim.release("a"))
        self.assertFalse(os.path.exists(self.claim_path))
        self.assertIsNone(distill_claim.read_live())

    def test_release_with_no_claim_is_false(self):
        self.assertFalse(distill_claim.release("a"))

    def test_claim_none_or_empty_is_refused(self):
        for falsy in (None, ""):
            self.assertEqual(distill_claim.claim(falsy), (False, None))
        self.assertFalse(os.path.exists(self.claim_path))

    def test_local_hhmm(self):
        text = distill_claim.local_hhmm(
            {"session_id": "a", "claimed_at": "2026-10-31T00:00:00Z"})
        self.assertRegex(text, r"^\d\d:\d\d$")
        utc = datetime.datetime(2026, 10, 31, 0, 0,
                                tzinfo=datetime.timezone.utc)
        self.assertEqual(text, utc.astimezone().strftime("%H:%M"))


class TestB9HalfWrittenClaim(ClaimCase):
    def test_dir_without_holder_is_live_and_held(self):
        os.makedirs(self.claim_path)
        live = distill_claim.read_live()
        self.assertIsNotNone(live)
        self.assertIsNone(live["session_id"])
        ok, _holder = distill_claim.claim("x")
        self.assertFalse(ok)
        self.assertTrue(os.path.isdir(self.claim_path))

    def test_unparseable_holder_is_live_too(self):
        os.makedirs(self.claim_path)
        with open(os.path.join(self.claim_path, "holder.json"), "w") as fh:
            fh.write("{not json")
        self.assertIsNone(distill_claim.read_live()["session_id"])
        self.assertFalse(distill_claim.claim("x")[0])

    def test_holder_missing_a_key_is_treated_as_unparseable(self):
        os.makedirs(self.claim_path)
        with open(os.path.join(self.claim_path, "holder.json"), "w") as fh:
            json.dump({"session_id": "a"}, fh)
        self.assertIsNone(distill_claim.read_live()["session_id"])

    def test_old_dir_without_holder_is_taken(self):
        os.makedirs(self.claim_path)
        self.age_dir(25)
        self.assertIsNone(distill_claim.read_live())
        ok, holder = distill_claim.claim("x")
        self.assertTrue(ok)
        self.assertEqual(holder["session_id"], "x")


RACER = r"""
import os, sys, time
sys.path.insert(0, sys.argv[1])
import distill_claim
start, out, me = sys.argv[2], sys.argv[3], sys.argv[4]
deadline = time.time() + 20
while not os.path.exists(start) and time.time() < deadline:
    time.sleep(0.001)
ok, _holder = distill_claim.claim(me)
with open(out, "w") as fh:
    fh.write("1" if ok else "0")
"""


class TestB10Race(ClaimCase):
    def test_exactly_one_of_eight_wins_five_times(self):
        env = dict(os.environ)
        for run in range(5):
            if os.path.exists(self.claim_path):
                shutil.rmtree(self.claim_path)
            start = os.path.join(self.sandbox, "go-%d" % run)
            procs, outs = [], []
            for n in range(8):
                out = os.path.join(self.sandbox, "out-%d-%d" % (run, n))
                outs.append(out)
                procs.append(subprocess.Popen(
                    [sys.executable, "-c", RACER, LIB, start, out,
                     "s%d" % n], env=env))
            open(start, "w").close()
            for proc in procs:
                self.assertEqual(proc.wait(timeout=60), 0)
            results = [open(o).read() for o in outs]
            self.assertEqual(results.count("1"), 1, (run, results))
            self.assertEqual(results.count("0"), 7, (run, results))


if __name__ == "__main__":
    unittest.main(verbosity=2)
