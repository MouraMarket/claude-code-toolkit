#!/usr/bin/env python3
"""lib/hook_heartbeat.py's verdict, over hand-built heartbeat records."""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import hook_heartbeat as hb  # noqa: E402

SID = "sess-1"


class VerdictTestCase(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.home = os.path.join(self.root, "home")
        self.repo = os.path.join(self.root, "repo")
        self.plugin = os.path.join(self.repo, "plugin")
        os.makedirs(os.path.join(self.plugin, "hooks"))
        self.write_hooks('{"hooks": {}}')
        os.makedirs(os.path.join(self.home, hb.RECORD_DIR))

    def write_hooks(self, text):
        with open(os.path.join(self.plugin, "hooks", "hooks.json"), "w") as handle:
            handle.write(text)

    def hooks_hash(self):
        with open(os.path.join(self.plugin, "hooks", "hooks.json"), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def record(self, name, body, session=SID):
        path = os.path.join(self.home, hb.RECORD_DIR, "%s.%s" % (session, name))
        with open(path, "w") as handle:
            handle.write(body)

    def fired(self, root=None, session=SID):
        self.record("fired", "fired_at=1\nplugin_root=%s\nroot_source=env\n"
                    % (root or self.plugin), session)

    def start(self, root=None, sha="CURRENT", session=SID):
        sha = self.hooks_hash() if sha == "CURRENT" else sha
        self.record("start", "started_at=1\nplugin_root=%s\nroot_source=env\n"
                    "hooks_sha256=%s\n" % (root or self.plugin, sha), session)

    def state(self, session=SID):
        return hb.verdict(self.repo, self.home, session)[0]

    def healthy(self):
        self.fired()
        self.start()


class TestStates(VerdictTestCase):

    def test_no_session(self):
        for bad in (None, "", "../evil", "a/b", "a b"):
            self.assertEqual("no_session", self.state(bad), repr(bad))
        self.assertIn("through Claude Code", hb.verdict(self.repo, self.home, None)[1])

    def test_never(self):
        self.assertEqual("never", self.state())

    def test_messages_name_core(self):
        # the heartbeat lives in core, so a verdict speaks for core's hooks only
        never = hb.verdict(self.repo, self.home, SID)[1]
        self.healthy()
        proven = hb.verdict(self.repo, self.home, SID)[1]
        for message in (never, proven):
            self.assertIn("core", message)
            self.assertIn("toolkit-core", message)

    def test_not_fresh(self):
        self.fired()
        self.assertEqual("not_fresh", self.state())

    def test_unreadable(self):
        self.record("fired", "fired_at=soon\nplugin_root=%s\n" % self.plugin)
        self.start()
        self.assertEqual("unreadable", self.state())
        self.fired()
        self.record("start", "started_at=1\nhooks_sha256=%s\n" % self.hooks_hash())
        self.assertEqual("unreadable", self.state())

    def test_foreign_fired_root(self):
        self.fired(root=os.path.join(self.root, "other", "plugin"))
        self.start()
        self.assertEqual("foreign", self.state())

    def test_foreign_start_root_only(self):
        self.fired()
        self.start(root=os.path.join(self.root, "other", "plugin"))
        self.assertEqual("foreign", self.state())

    def test_foreign_wins_over_stale(self):
        # Both wrong at once: the root is the actionable fault (a stale hash from
        # another checkout says nothing about this one), so `foreign` must win.
        self.fired(root=os.path.join(self.root, "other", "plugin"))
        self.start(root=os.path.join(self.root, "other", "plugin"), sha="0" * 64)
        self.assertEqual("foreign", self.state())

    def test_symlinked_root_is_proven(self):
        link = os.path.join(self.root, "link")
        os.symlink(self.plugin, link)
        self.fired(root=link)
        self.start(root=link)
        self.assertEqual("proven", self.state())

    def test_stale_when_hooks_edited_after_start(self):
        self.healthy()
        self.write_hooks('{"hooks": {"PreToolUse": []}}')
        self.assertEqual("stale", self.state())

    def test_stale_when_hash_empty(self):
        self.fired()
        self.start(sha="")
        self.assertEqual("stale", self.state())

    def test_proven(self):
        self.healthy()
        state, message = hb.verdict(self.repo, self.home, SID)
        self.assertEqual("proven", state)
        self.assertNotIn("protected", message)

    def test_other_session_does_not_prove_this_one(self):
        self.fired(session="other")
        self.start(session="other")
        self.assertEqual("proven", self.state("other"))
        self.assertEqual("never", self.state(SID))


class TestProfileVerdict(VerdictTestCase):
    DIR = "plugin-build"

    def setUp(self):
        super().setUp()
        self.pdir = os.path.join(self.repo, self.DIR)
        os.makedirs(os.path.join(self.pdir, "hooks"))
        self.write_profile_hooks('{"hooks": {"SessionStart": []}}')

    def write_profile_hooks(self, text):
        with open(os.path.join(self.pdir, "hooks", "hooks.json"), "w") as handle:
            handle.write(text)

    def pstart(self, root=None, sha="CURRENT", session=SID, name=None, body=None):
        if sha == "CURRENT":
            with open(os.path.join(self.pdir, "hooks", "hooks.json"), "rb") as handle:
                sha = hashlib.sha256(handle.read()).hexdigest()
        self.record("%s.start" % (name or self.DIR), body or (
            "started_at=1\nplugin_root=%s\nroot_source=env\nhooks_sha256=%s\n"
            % (root or self.pdir, sha)), session)

    def pstate(self, session=SID, plugin_dir=None):
        return hb.profile_verdict(self.repo, self.home, session, plugin_dir or self.DIR)

    def test_no_session(self):
        for sid in (None, "", "../x", "a.b"):
            self.assertEqual(self.pstate(sid)[0], "no_session", sid)

    def test_never_without_a_record(self):
        state, message = self.pstate()
        self.assertEqual(state, "never")
        self.assertIn(self.DIR, message)

    def test_core_start_record_does_not_prove_a_profile(self):
        self.healthy()
        self.assertEqual(self.pstate()[0], "never")

    def test_another_profiles_record_does_not_prove_this_one(self):
        self.pstart(name="plugin-mac")
        self.assertEqual(self.pstate()[0], "never")

    def test_unreadable(self):
        for body in ("started_at=x\nplugin_root=%s\n", "started_at=1\nplugin_root=\n", "junk\n"):
            self.pstart(body=body % self.pdir if "%s" in body else body)
            self.assertEqual(self.pstate()[0], "unreadable", body)

    def test_foreign(self):
        self.pstart(root=os.path.join(self.root, "elsewhere", self.DIR))
        state, message = self.pstate()
        self.assertEqual(state, "foreign")
        self.assertIn(self.DIR, message)

    def test_foreign_wins_over_stale(self):
        self.pstart(root=os.path.join(self.root, "elsewhere"), sha="0" * 64)
        self.assertEqual(self.pstate()[0], "foreign")

    def test_symlinked_root_is_proven(self):
        link = os.path.join(self.root, "link")
        os.symlink(self.pdir, link)
        self.pstart(root=link)
        self.assertEqual(self.pstate()[0], "proven")

    def test_stale_when_hooks_edited_after_start(self):
        self.pstart()
        self.write_profile_hooks('{"hooks": {"Stop": []}}')
        self.assertEqual(self.pstate()[0], "stale")

    def test_stale_when_hash_empty(self):
        self.pstart(sha="")
        self.assertEqual(self.pstate()[0], "stale")

    def test_core_hooks_edit_does_not_stale_a_profile(self):
        self.pstart()
        self.write_hooks('{"hooks": {"Stop": []}}')
        self.assertEqual(self.pstate()[0], "proven")

    def test_proven(self):
        self.pstart()
        state, message = self.pstate()
        self.assertEqual(state, "proven")
        self.assertIn(self.DIR, message)

    def test_other_session_does_not_prove_this_one(self):
        self.pstart(session="other-sess")
        self.assertEqual(self.pstate()[0], "never")

    def test_refuses_a_non_profile_dir(self):
        for bad in ("plugin", "plugin-", "plugin-Build", "plugin-build2", "../plugin-build",
                    "plugin-build/x", "", None, "skills"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                hb.profile_verdict(self.repo, self.home, SID, bad)

    def test_core_verdict_unchanged_by_a_profile_record(self):
        self.pstart()
        self.assertEqual(self.state(), "never")


class TestRead(VerdictTestCase):

    def test_value_containing_equals_parses_intact(self):
        self.record("fired", "fired_at=1\nplugin_root=/a=b/plugin\n")
        got = hb.read(os.path.join(self.home, hb.RECORD_DIR, SID + ".fired"))
        self.assertEqual("/a=b/plugin", got["plugin_root"])

    def test_missing_is_none(self):
        self.assertIsNone(hb.read(os.path.join(self.home, "nope")))


if __name__ == "__main__":
    unittest.main()
