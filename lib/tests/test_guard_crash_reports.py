#!/usr/bin/env python3
"""Cross-guard suite: a PreToolUse guard that fails to complete its check must
say so on two channels (systemMessage for the human, additionalContext for
Claude), distinctly from a checked allow -- while still failing open.

Each observation is a SEPARATE `python3 -c` process: one fault is applied, then
the real guard file runs through `runpy` as `__main__`. The child's real exit
code and stdout are what is asserted; no test seam exists in any guard.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

LIB_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(LIB_TESTS_DIR))
LIB_DIR = os.path.join(REPO, "lib")

# name -> (tool dir, guard file, GUARD, NOUN, lib module, evaluate target)
GUARDS = {
    "danger": ("danger-guard", "claude-danger-guard", "claude-danger-guard",
               "command", "guard_lib", ("guard_lib", "evaluate")),
    "worktree": ("worktree-guard", "claude-worktree-guard",
                 "claude-worktree-guard", "command", "worktree_lib",
                 ("worktree_lib", "evaluate")),
    "session": ("session-claim", "claude-session-claim", "claude-session-claim",
                "edit", "claim_lib", ("claim_lib", "find_checkout")),
    "model": ("model-guard", "claude-model-guard", "claude-model-guard",
              "dispatch", "guard_lib", ("guard_lib", "decide")),
    "distill": ("distill-guard", "claude-distill-write-guard",
                "claude-distill-write-guard", "write", "write_guard_lib",
                ("write_guard_lib", "decide")),
    "dispatch": ("usage-watch", "claude-usage-dispatch",
                 "claude-usage-dispatch", "dispatch", "dispatch_lib",
                 ("dispatch_lib", "verdict")),
}

# PreToolUse hooks that are NOT guards: they never refuse, so a crash that
# stays silent costs an advisory, not a check. Each names the
# permissionDecision values it may emit ("ask" at most). The coverage test
# scans each one's shipped source for every refusal channel a PreToolUse hook
# has -- a "deny" or "block" decision, a permissionDecision it did not
# declare, a non-zero exit from its entry file -- so one that gains a refusal
# fails until it joins GUARDS. A source scan is a tripwire, not a proof: a
# refusal built from a computed value is invisible to it.
ADVISORY = {
    "hook-heartbeat/claude-hook-heartbeat":
        ("records that the hook layer ran; decides nothing", ()),
    "prompt-optimizer/claude-prompt-optimizer":
        ("adds context to a dispatch; decides nothing", ()),
}

HOOK_PREFIX = '"${CLAUDE_PLUGIN_ROOT}"/tools/'

# A non-zero exit is a PreToolUse refusal (exit 2 blocks the call). Python:
# any exit() whose argument is not a literal 0. Shell: any `exit` outside a
# comment not followed by 0.
NONZERO_EXIT = {
    "python": re.compile(r"\bexit\s*\((?!\s*0\s*\))"),
    "shell": re.compile(r"(?m)^[^#\n]*\bexit\b(?!\s+0\b)"),
}
REFUSALS = ('"deny"', "'deny'", '"block"', "'block'")

SCRUBBED = ("DANGER_ALLOW", "WORKTREE_ALLOW", "WT_ALLOW_MAIN", "CLAIM_ALLOW",
            "MODEL_GUARD_ALLOW")

DEEP_JSON = '{"a":' * 100000 + '1' + '}' * 100000

# Whether the guards' interpreter parses DEEP_JSON instead of raising
# RecursionError: None until probed, then True/False. Python 3.14+ bounds
# recursion by the real stack, so a larger stack (`ulimit -s 65520`, measured:
# 3.14.7 parses there, raises at the default 8176) or an interpreter built
# differently (Homebrew 3.15.2, H11) parses this depth, and the decode fault
# has no crash to report. The probe runs in a child on sys.executable, the
# interpreter Harness.run uses, so it sees the same stack the guard would.
_DEEP_JSON_PARSES = None
# scripts/check-capabilities.py VERSIONS, as (major, minor).
RELEASE_INTERPRETERS = ((3, 9), (3, 13))


def deep_json_parses():
    """True only when a child parses DEEP_JSON cleanly. A child that fails any
    other way returns False, so the decode test still runs and shows it."""
    global _DEEP_JSON_PARSES
    if _DEEP_JSON_PARSES is None:
        proc = subprocess.run(
            [sys.executable, "-c", "import json, sys; json.loads(sys.stdin.read())"],
            input=DEEP_JSON, capture_output=True, text=True, timeout=120)
        _DEEP_JSON_PARSES = proc.returncode == 0
    return _DEEP_JSON_PARSES


class Harness(object):
    """Owns a temp dir and runs one guard in a child process."""

    def __init__(self, key):
        self.key = key
        tool_dir, guard_file, self.guard, self.noun, self.lib, target = GUARDS[key]
        self.tool_dir = os.path.join(REPO, "tools", tool_dir)
        self.guard_path = os.path.join(self.tool_dir, guard_file)
        self.target = target
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)

    def close(self):
        self._tmp.cleanup()

    def payload(self):
        if self.key in ("danger", "worktree"):
            body = {"tool_name": "Bash", "tool_input": {"command": "echo hi"},
                    "cwd": self.tmp}
        elif self.key in ("session", "distill"):
            body = {"tool_name": "Write",
                    "tool_input": {"file_path": os.path.join(self.tmp, "x.txt")}}
        else:
            body = {"tool_name": "Agent",
                    "tool_input": {"subagent_type": "general-purpose",
                                   "prompt": "x"},
                    "cwd": self.tmp}
        return json.dumps(body)

    def env(self):
        env = {k: v for k, v in os.environ.items()
               if k not in SCRUBBED and not k.startswith("GIT_")}
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(self.tmp, "no-such-config.json")
        env["HOME"] = self.tmp
        env["USERPROFILE"] = self.tmp
        if self.key == "session":
            env["CLAIM_CLAIMS_PATH"] = os.path.join(self.tmp, "claims.json")
            env["CLAIM_BREAKER_PATH"] = os.path.join(self.tmp, "breaker.json")
            env["CLAIM_LOG_PATH"] = os.path.join(self.tmp, "claim.log")
        if self.key == "dispatch":
            # The advisory is off by default; turn it on so the faulted
            # function is reached.
            config = os.path.join(self.tmp, "config.json")
            with open(config, "w") as fh:
                json.dump({"usage_watch": {"dispatch_advisory": True}}, fh)
            env["CLAUDE_TOOLKIT_CONFIG"] = config
        return env

    def _script(self, fault):
        lines = ["import runpy, sys",
                 "sys.path.insert(0, %r)" % self.tool_dir,
                 "sys.path.insert(0, %r)" % LIB_DIR]
        if fault == "import":
            lines.append("sys.modules[%r] = None" % self.lib)
        elif fault == "evaluate":
            module, func = self.target
            lines += ["import %s" % module,
                      "def _boom(*args, **kwargs):",
                      "    raise RuntimeError('boom')",
                      "%s.%s = _boom" % (module, func)]
        lines.append("runpy.run_path(%r, run_name='__main__')" % self.guard_path)
        return "\n".join(lines) + "\n"

    def run(self, fault=None, stdin=None):
        """fault: None | 'evaluate' | 'import' | 'decode'."""
        if stdin is None:
            stdin = DEEP_JSON if fault == "decode" else self.payload()
        return subprocess.run(
            [sys.executable, "-c", self._script(fault)], input=stdin,
            capture_output=True, text=True, env=self.env(), timeout=120)

    def expected_prefix(self):
        return "%s crashed and did NOT check this %s: " % (self.guard, self.noun)


class CrashReportMixin(object):
    # A mixin, not a TestCase: unittest must not collect the base itself.
    KEY = None

    def setUp(self):
        self.h = Harness(self.KEY)
        self.addCleanup(self.h.close)

    def assert_crash_report(self, fault, exc_text):
        proc = self.h.run(fault)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("permissionDecision", proc.stdout)
        obj = json.loads(proc.stdout)  # ONE JSON object, or this raises
        self.assertIsInstance(obj, dict)
        self.assertEqual(set(obj), {"systemMessage", "hookSpecificOutput"})
        hso = obj["hookSpecificOutput"]
        self.assertEqual(set(hso), {"hookEventName", "additionalContext"})
        self.assertEqual(hso["hookEventName"], "PreToolUse")
        message = obj["systemMessage"]
        self.assertTrue(message.startswith(self.h.expected_prefix() + exc_text),
                        message)
        self.assertTrue(hso["additionalContext"].startswith(message))
        self.assertIn("Tell the user", hso["additionalContext"])
        return message

    def test_evaluate_crash_reports_on_both_channels(self):
        self.assert_crash_report("evaluate", "RuntimeError: boom")

    def test_import_failure_reports_on_both_channels(self):
        # A None entry in sys.modules raises ModuleNotFoundError, the
        # ImportError subclass, and the report names the concrete type.
        self.assert_crash_report("import", "ModuleNotFoundError")

    def test_decode_crash_reports_on_both_channels(self):
        # Same skip as the worktree- and danger-guard deep-JSON controls (row
        # 93): with no RecursionError there is no crash, so the guard's silent
        # allow would read as a missing report. The crash boundary is proven
        # on the release interpreters (scripts/check-capabilities.py VERSIONS).
        # Those two raise at any stack size (3.9 counts frames, 3.13 has a
        # fixed C limit; measured at `ulimit -s 65520`), so a skip there is
        # a broken probe or skip, never a real parse: fail instead.
        if deep_json_parses():
            if sys.version_info[:2] in RELEASE_INTERPRETERS:
                self.fail("the decode control would skip on a release "
                          "interpreter, Python %d.%d" % sys.version_info[:2])
            self.skipTest(
                "Python %d.%d parses 100000 levels of nesting on this stack"
                % sys.version_info[:2])
        self.assert_crash_report("decode", "RecursionError")

    def test_positive_control_unfaulted_run_is_not_a_crash(self):
        proc = self.h.run(None)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertNotIn("crashed", proc.stdout)

    def test_silence_control_malformed_json_is_a_silent_allow(self):
        proc = self.h.run(None, stdin="{not json")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")


class TestDangerGuardCrashReports(CrashReportMixin, unittest.TestCase):
    KEY = "danger"


class TestWorktreeGuardCrashReports(CrashReportMixin, unittest.TestCase):
    KEY = "worktree"


class TestSessionClaimCrashReports(CrashReportMixin, unittest.TestCase):
    KEY = "session"


class TestModelGuardCrashReports(CrashReportMixin, unittest.TestCase):
    KEY = "model"


class TestDistillGuardCrashReports(CrashReportMixin, unittest.TestCase):
    KEY = "distill"


class TestUsageDispatchCrashReports(CrashReportMixin, unittest.TestCase):
    KEY = "dispatch"


class TestEveryShippedGuardIsCovered(unittest.TestCase):
    """Every PreToolUse hook a plugin ships is either a guard in GUARDS or a
    named ADVISORY with no deny path -- so a new guard cannot ship outside the
    crash contract without this test noticing."""

    def shipped_pretooluse(self):
        found = set()
        plugins = sorted(d for d in os.listdir(REPO) if d.startswith("plugin"))
        for plugin in plugins:
            path = os.path.join(REPO, plugin, "hooks", "hooks.json")
            if not os.path.isfile(path):
                continue
            with open(path, encoding="utf-8") as fh:
                hooks = json.load(fh)["hooks"]
            for group in hooks.get("PreToolUse", []):
                for hook in group["hooks"]:
                    command = hook["command"]
                    self.assertTrue(command.startswith(HOOK_PREFIX), command)
                    found.add(command[len(HOOK_PREFIX):].split()[0])
        return found

    def test_every_pretooluse_hook_is_a_guard_or_a_named_advisory(self):
        shipped = self.shipped_pretooluse()
        guards = {"%s/%s" % (g[0], g[1]) for g in GUARDS.values()}
        self.assertGreaterEqual(len(shipped), len(guards) + len(ADVISORY))
        self.assertEqual(shipped, guards | set(ADVISORY))

    def test_no_advisory_hook_has_a_refusal_channel(self):
        for entry, (_why, decisions) in ADVISORY.items():
            tool_dir = os.path.join(REPO, "tools", entry.split("/")[0])
            scanned = 0
            for dirpath, dirnames, filenames in os.walk(tool_dir):
                dirnames[:] = [d for d in dirnames if d != "tests"]
                for name in filenames:
                    with open(os.path.join(dirpath, name), "rb") as fh:
                        text = fh.read().decode("utf-8", "replace")
                    scanned += 1
                    for refusal in REFUSALS:
                        self.assertNotIn(refusal, text, (entry, name))
                    if not decisions:
                        self.assertNotIn("permissionDecision", text,
                                         (entry, name))
            self.assertGreater(scanned, 0, entry)

            with open(os.path.join(REPO, "tools", entry), "rb") as fh:
                source = fh.read().decode("utf-8", "replace")
            shebang = source.split("\n", 1)[0]
            language = "python" if "python" in shebang else "shell"
            self.assertTrue(shebang.startswith("#!"), entry)
            match = NONZERO_EXIT[language].search(source)
            self.assertIsNone(match, (entry, match and match.group(0)))


class TestMessagesMatchTheGuardNounTable(unittest.TestCase):
    def test_the_evaluate_messages_are_exactly_the_table(self):
        expected = {
            "danger": "claude-danger-guard crashed and did NOT check this command: RuntimeError: boom",
            "worktree": "claude-worktree-guard crashed and did NOT check this command: RuntimeError: boom",
            "session": "claude-session-claim crashed and did NOT check this edit: RuntimeError: boom",
            "model": "claude-model-guard crashed and did NOT check this dispatch: RuntimeError: boom",
            "distill": "claude-distill-write-guard crashed and did NOT check this write: RuntimeError: boom",
            "dispatch": "claude-usage-dispatch crashed and did NOT check this dispatch: RuntimeError: boom",
        }
        observed = {}
        for key in GUARDS:
            h = Harness(key)
            try:
                proc = h.run("evaluate")
                try:
                    observed[key] = json.loads(proc.stdout)["systemMessage"]
                except (ValueError, KeyError, TypeError):
                    observed[key] = "<no systemMessage: stdout=%r>" % proc.stdout
            finally:
                h.close()
        self.assertEqual(observed, expected)


if __name__ == "__main__":
    unittest.main()
