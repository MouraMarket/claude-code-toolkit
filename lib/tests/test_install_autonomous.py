#!/usr/bin/env python3
"""`./install.sh --autonomous`: the opt-in wide Bash grant.

Pinned here:
  - the grant lands (Bash(*) allowed, defaultMode "auto", on top of the
    baseline) only after "I understand" is typed at a terminal, and the
    install record lists what it added so a removal can take it out
  - anything else typed skips the grant and applies the baseline only
  - with no terminal it refuses the grant, says so, and applies the baseline
  - a config that declines an always-on core guard refuses the WHOLE run
    before anything changes, even with the confirmation typed: the grant is
    only defensible with the danger guard in front of it
  - a plain install never grants it

These drive the REAL install.sh against a fixture repo, with HOME sandboxed.
The terminal is a pseudo-terminal; the reply is written only after the prompt
has been printed, so nothing earlier in the run can read it first.
"""

import json
import os
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest

import install_fixture as fx
from test_install_permissions_opt_out import DENY, make_fixture_repo, seed_home
from test_install_preflight import snapshot

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROMPT = 'Type "I understand" to apply it'
GRANT = "Bash(*)"


class AutonomousTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.repo = make_fixture_repo(self.root)
        # The real overlay, so these tests pin what users actually receive.
        shutil.copyfile(os.path.join(REPO_ROOT, "claude", "permissions.autonomous.json"),
                        os.path.join(self.repo, "claude", "permissions.autonomous.json"))
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        self.workspace = os.path.join(self.root, "workspace")
        os.makedirs(self.workspace)
        self.settings_path = os.path.join(self.home, ".claude", "settings.json")
        self.record_path = os.path.join(self.home, ".config", "claude-code-toolkit",
                                        "install-record.json")

    def env(self):
        env = dict(os.environ)
        env["HOME"] = self.home
        env.pop("CLAUDE_TOOLKIT_CONFIG", None)
        env.pop("ALLOW_WORKTREE_INSTALL", None)
        return fx.shim_env_for_home(env, self.home)

    def run_piped(self, *args):
        """No terminal: stdin is /dev/null."""
        return subprocess.run([os.path.join(self.repo, "install.sh")] + list(args),
                              cwd=self.repo, env=self.env(), capture_output=True,
                              stdin=subprocess.DEVNULL, timeout=120,
                              universal_newlines=True)

    def run_at_terminal(self, reply, *args):
        """stdin is a pseudo-terminal. `reply` is typed once PROMPT appears;
        returns (returncode, combined output, whether the prompt appeared)."""
        master, slave = pty.openpty()
        proc = subprocess.Popen([os.path.join(self.repo, "install.sh")] + list(args),
                                cwd=self.repo, env=self.env(), stdin=slave,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.close(slave)
        out = b""
        prompted = False
        deadline = time.monotonic() + 120
        try:
            fd = proc.stdout.fileno()
            while time.monotonic() < deadline:
                ready, _, _ = select.select([fd], [], [], 0.5)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                out += chunk
                if not prompted and PROMPT.encode() in out:
                    prompted = True
                    os.write(master, (reply + "\n").encode())
            proc.wait(timeout=max(1, deadline - time.monotonic()))
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stdout.close()
            os.close(master)
        return proc.returncode, out.decode("utf-8", "replace"), prompted

    def permissions(self):
        with open(self.settings_path, encoding="utf-8") as handle:
            return json.load(handle).get("permissions") or {}

    def record(self):
        with open(self.record_path, encoding="utf-8") as handle:
            return json.load(handle)

    def assertNoGrant(self, output):
        perms = self.permissions()
        self.assertIn(DENY, perms.get("deny") or [],
                      "fixture bug: the baseline itself was not applied\n" + output)
        self.assertNotIn(GRANT, perms.get("allow") or [], output)
        self.assertNotEqual(perms.get("defaultMode"), "auto", output)


class TestTypedConfirmationAppliesTheGrant(AutonomousTestCase):

    def test_grant_lands_on_top_of_the_baseline_and_is_recorded(self):
        seed_home(self.home, {"workspace_root": self.workspace})
        rc, out, prompted = self.run_at_terminal("I understand", "--autonomous")
        self.assertTrue(prompted, out)
        self.assertEqual(rc, 0, out)
        perms = self.permissions()
        self.assertIn(GRANT, perms.get("allow") or [], out)
        self.assertEqual(perms.get("defaultMode"), "auto", out)
        self.assertIn(DENY, perms.get("deny") or [], "the overlay replaced the baseline")
        self.assertIn("allow += " + GRANT, out)
        self.assertIn("defaultMode = auto", out)
        record = self.record()
        self.assertIn(GRANT, record["permissions_added"]["allow"])
        self.assertEqual(record["default_mode"]["set"], "auto")

    def test_the_warning_names_the_guard_and_the_file_to_read(self):
        seed_home(self.home, {"workspace_root": self.workspace})
        rc, out, prompted = self.run_at_terminal("no", "--autonomous")
        self.assertTrue(prompted, out)
        warning = out[:out.index(PROMPT)]
        self.assertIn("claude-danger-guard runs first", warning)
        self.assertIn("claude/permissions.autonomous.json", warning)
        self.assertIn("policy/CLAUDE.md", warning)

    def test_a_rerun_with_the_grant_present_reports_it_covered(self):
        seed_home(self.home, {"workspace_root": self.workspace})
        self.run_at_terminal("I understand", "--autonomous")
        rc, out, _ = self.run_at_terminal("I understand", "--autonomous")
        self.assertEqual(rc, 0, out)
        self.assertIn("permissions already cover the autonomous set", out)


class TestAnythingElseSkipsTheGrant(AutonomousTestCase):

    def test_other_replies_apply_the_baseline_only(self):
        for reply in ("", "i understand", "yes", "I understand."):
            with self.subTest(reply=reply):
                shutil.rmtree(self.home)
                os.makedirs(self.home)
                seed_home(self.home, {"workspace_root": self.workspace})
                rc, out, prompted = self.run_at_terminal(reply, "--autonomous")
                self.assertTrue(prompted, out)
                self.assertEqual(rc, 0, out)
                self.assertIn("skipped the autonomous grant", out)
                self.assertNoGrant(out)


class TestNoTerminalRefusesTheGrant(AutonomousTestCase):

    def test_unattended_run_applies_the_baseline_and_says_why(self):
        seed_home(self.home, {"workspace_root": self.workspace})
        proc = self.run_piped("--autonomous")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no TTY — refusing to apply the autonomous grant unattended",
                      proc.stdout)
        self.assertNotIn(PROMPT, proc.stdout)
        self.assertNoGrant(proc.stdout)


class TestDeclinedCoreGuardRefusesTheGrant(AutonomousTestCase):
    """P05: the grant is only defensible behind the danger guard, so a config
    that declines a core guard must not reach it. The public installer is
    stricter than the copy this came from (which applied the baseline and
    refused only the grant): it refuses the whole run before any change."""

    def test_refused_before_any_change_even_with_the_confirmation_typed(self):
        for section in ("danger_guard", "worktree_guard", "secret_scan"):
            with self.subTest(section=section):
                shutil.rmtree(self.home)
                os.makedirs(self.home)
                seed_home(self.home, {"workspace_root": self.workspace,
                                      section: {"installed": False}})
                before = snapshot(self.home)
                rc, out, prompted = self.run_at_terminal("I understand", "--autonomous")
                self.assertEqual(rc, 1, out)
                self.assertFalse(prompted, "the grant was offered despite the decline")
                self.assertIn("declines guards this toolkit's core always installs", out)
                self.assertIn("  - " + section, out)
                self.assertFalse(os.path.exists(self.settings_path), out)
                self.assertEqual(snapshot(self.home), before, out)


class TestPlainInstallNeverGrants(AutonomousTestCase):

    def test_plain_install_at_a_terminal_never_offers_or_applies_it(self):
        seed_home(self.home, {"workspace_root": self.workspace})
        rc, out, prompted = self.run_at_terminal("I understand")
        self.assertEqual(rc, 0, out)
        self.assertFalse(prompted, out)
        self.assertNotIn("--autonomous grants", out)
        self.assertNoGrant(out)


if __name__ == "__main__":
    unittest.main()
