"""Tests for claude-tool-rules and tool_rules_lib (A1-A10)."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))

EXE = os.path.join(TOOL_DIR, "claude-tool-rules")
HOOKS_JSON = os.path.join(REPO_ROOT, "plugin-continuity", "hooks", "hooks.json")

MCP = "mcp__claude_ai_Tracker__tracker_get_task"


def page(patterns, body):
    items = "\n".join("  - %s" % p for p in patterns)
    return "---\ntools:\n%s\n---\n\n%s" % (items, body)


class Sandbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.home = os.path.join(self.tmp, "home")
        self.state = os.path.join(self.tmp, "state")
        self.pages = os.path.join(self.tmp, "pages")
        for d in (self.home, self.state, self.pages):
            os.makedirs(d)

    def tearDown(self):
        self._tmp.cleanup()

    def write_page(self, name, text):
        with open(os.path.join(self.pages, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=self.home, USERPROFILE=self.home,
                   CLAUDE_TOOL_RULES_STATE_DIR=self.state)
        env.update(extra)
        return env

    def run_exe(self, payload, args=("--dir",), raw=None, env=None):
        args = list(args)
        if args == ["--dir"]:
            args = ["--dir", self.pages]
        stdin = raw if raw is not None else json.dumps(payload)
        return subprocess.run([sys.executable, EXE] + args, input=stdin,
                              capture_output=True, text=True,
                              env=env or self.env(), timeout=30)

    def call(self, tool="Bash", command="ls", session="s1", **extra):
        payload = {"session_id": session, "tool_name": tool,
                   "tool_input": {"command": command}}
        payload.update(extra)
        return self.run_exe(payload)

    def text(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        if not proc.stdout.strip():
            return None
        return json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]


class TestLib(unittest.TestCase):
    def test_tool_rules_dir(self):
        import tool_rules_lib as lib
        self.assertEqual(str(lib.tool_rules_dir("/w")), "/w/.claude/tool-rules")
        self.assertIsNone(lib.tool_rules_dir(None))
        self.assertIsNone(lib.tool_rules_dir(""))

    def test_parse_page(self):
        import tool_rules_lib as lib
        text = "---\ntools:\n- foo\n  - \"bar*\"\n  - 'baz'\nother: 1\n---\n\n\nBody here\n"
        self.assertEqual(lib.parse_page(text), (["foo", "bar*", "baz"], "Body here\n"))
        self.assertIsNone(lib.parse_page("no frontmatter"))
        self.assertIsNone(lib.parse_page("---\nname: x\n---\nbody"))
        self.assertIsNone(lib.parse_page("---\ntools:\n---\nbody"))
        self.assertIsNone(lib.parse_page("\n---\ntools:\n- a\n---\nbody"))
        self.assertIsNone(lib.parse_page("---\ntools:\n- a\nbody"))

    def test_matches(self):
        import tool_rules_lib as lib
        self.assertTrue(lib.matches("Bash(*graph.facebook.com*)", "Bash",
                                    {"command": "curl https://graph.facebook.com/x"}))
        self.assertFalse(lib.matches("Bash(*graph.facebook.com*)", "Bash", {"command": "ls"}))
        self.assertFalse(lib.matches("Bash(*graph.facebook.com*)", "graph.facebook.com", {}))
        self.assertFalse(lib.matches("Bash(*)", "Read", {"command": "x"}))
        self.assertTrue(lib.matches("mcp__a__*", "mcp__a__b", {}))
        self.assertFalse(lib.matches("mcp__a__*", "Read", {}))

    def test_matches_read_grep_glob(self):
        import tool_rules_lib as lib
        pat = "*job-automation*"
        here = "/Users/x/job-automation/lib/a.ts"
        self.assertTrue(lib.matches("Read(%s)" % pat, "Read", {"file_path": here}))
        self.assertFalse(lib.matches("Read(%s)" % pat, "Read", {"file_path": "/Users/x/other.ts"}))
        self.assertFalse(lib.matches("Read(%s)" % pat, "Bash", {"command": here}))
        self.assertFalse(lib.matches("Bash(%s)" % pat, "Read", {"file_path": here}))
        self.assertFalse(lib.matches("Read(%s)" % pat, "Grep", {"path": here}))
        self.assertTrue(lib.matches("Grep(%s)" % pat, "Grep", {"pattern": "x", "path": here}))
        self.assertTrue(lib.matches("Grep(%s)" % pat, "Grep", {"pattern": "x", "glob": "job-automation/**"}))
        # Grep's `pattern` is search text, not a location.
        self.assertFalse(lib.matches("Grep(%s)" % pat, "Grep", {"pattern": "job-automation"}))
        self.assertTrue(lib.matches("Glob(%s)" % pat, "Glob", {"pattern": "*.md", "path": here}))
        self.assertTrue(lib.matches("Glob(%s)" % pat, "Glob", {"pattern": "**/job-automation/*.md"}))
        self.assertFalse(lib.matches("Glob(%s)" % pat, "Glob", {"pattern": "**/*.md"}))
        self.assertFalse(lib.matches("Read(%s)" % pat, "Read", {"file_path": ["job-automation"]}))
        self.assertFalse(lib.matches("Read(%s)" % pat, "Read", {}))

    def test_max_bytes_default_and_override(self):
        import tool_rules_lib as lib
        import toolkit_config
        saved = toolkit_config.CONFIG_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for value, want in ((12000, 12000), (0, 9500), ("x", 9500),
                                    (None, 9500), (True, 9500)):
                    path = os.path.join(tmp, "c.json")
                    with open(path, "w") as fh:
                        json.dump({"tool_rules": {"max_bytes": value}}, fh)
                    toolkit_config.CONFIG_PATH = path
                    toolkit_config.load(refresh=True)
                    self.assertEqual(lib.max_bytes(), want, value)
                toolkit_config.CONFIG_PATH = "/nonexistent.json"
                toolkit_config.load(refresh=True)
                self.assertEqual(lib.max_bytes(), 9500)
        finally:
            toolkit_config.CONFIG_PATH = saved
            toolkit_config.load(refresh=True)


class TestExecutable(Sandbox):
    def test_a1_mcp_glob(self):
        self.write_page("a.md", page(["mcp__claude_ai_Tracker__*"], "TRACKER RULES"))
        self.assertEqual(self.text(self.call(tool=MCP)), "TRACKER RULES")
        self.assertIsNone(self.text(self.call(tool="Read", session="s2")))

    def test_a2_bash_pattern(self):
        self.write_page("a.md", page(["Bash(*graph.facebook.com*)"], "FB RULES"))
        self.assertIn("FB RULES", self.text(
            self.call(command="curl https://graph.facebook.com/v1")))
        self.assertIsNone(self.text(self.call(command="ls", session="s2")))
        self.assertIsNone(self.text(self.call(tool="graph.facebook.com", session="s3")))

    def test_a2b_read_pattern_through_the_hook(self):
        self.write_page("a.md", page(["Bash(*job-automation*)", "Read(*job-automation*)"],
                                     "JOB RULES"))
        read = {"session_id": "s1", "tool_name": "Read",
                "tool_input": {"file_path": "/Users/x/job-automation/app.ts"}}
        self.assertEqual(self.text(self.run_exe(read)), "JOB RULES")
        self.assertIsNone(self.text(self.run_exe(read)))  # once per session
        self.assertIsNone(self.text(self.call(command="cat job-automation/x")))
        other = dict(read, session_id="s2", tool_input={"file_path": "/Users/x/other.ts"})
        self.assertIsNone(self.text(self.run_exe(other)))

    def test_a3_once_per_session_per_agent(self):
        self.write_page("a.md", page(["Bash"], "RULES"))
        self.assertIn("RULES", self.text(self.call()))
        self.assertIsNone(self.text(self.call()))
        self.assertIn("RULES", self.text(self.call(agent_id="a1")))
        self.assertIsNone(self.text(self.call(agent_id="a1")))
        self.assertIn("RULES", self.text(self.call(session="other")))

    def test_a4_reset(self):
        self.write_page("a.md", page(["Bash"], "RULES"))
        self.assertIn("RULES", self.text(self.call()))
        self.run_exe({"session_id": "s1", "source": "startup"}, args=["--reset"])
        self.assertIsNone(self.text(self.call()))
        proc = self.run_exe({"session_id": "s1", "source": "compact"}, args=["--reset"])
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.assertIn("RULES", self.text(self.call()))

    def test_a5_over_cap_page(self):
        body = "".join(chr(97 + i % 26) for i in range(9501))
        self.write_page("big.md", page(["Bash"], body))
        out = self.text(self.call())
        self.assertEqual(out, "[tool-rules] big.md is 9501 bytes, over the 9380 cap "
                              "— its rules did NOT load. Split it.")
        for i in range(0, len(body) - 12, 7):
            self.assertNotIn(body[i:i + 12], out)
        self.assertIsNone(self.text(self.call()))

    def test_a5b_body_in_the_reserve_band_is_over_cap_not_deferred(self):
        """A body between body_limit(cap) and cap can never be delivered, so it
        must get the terminal "split it" line, never the "loads on the next
        matching call" line it would otherwise repeat forever."""
        body = "z" * 9400
        self.write_page("band.md", page(["Bash"], body))
        out = self.text(self.call())
        self.assertEqual(out, "[tool-rules] band.md is 9400 bytes, over the 9380 cap "
                              "— its rules did NOT load. Split it.")
        self.assertIsNone(self.text(self.call()))

    def test_a6_budget_overflow_defers(self):
        self.write_page("a.md", page(["Bash"], "A" * 6000))
        self.write_page("b.md", page(["Bash"], "B" * 6000))
        first = self.text(self.call())
        self.assertIn("A" * 6000, first)
        self.assertNotIn("B", first.replace(
            "[tool-rules] b.md did not fit this call's 9500-byte budget — its rules "
            "did NOT load yet; they load on the next matching call.", ""))
        self.assertIn("b.md did not fit this call's 9500-byte budget", first)
        second = self.text(self.call())
        self.assertIn("B" * 6000, second)
        self.assertNotIn("A" * 100, second)

    def test_e3_every_emitted_byte_counts_toward_the_cap(self):
        body = "x" * 9501
        for i in range(130):
            self.write_page("p%03d.md" % i, page(["Bash"], body))
        first = self.call()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertLessEqual(len(first.stdout.encode("utf-8")), 9500 + 200)
        first_text = self.text(first)
        self.assertIn("more page(s) did not load", first_text)
        self.assertNotIn("p129.md", first_text)
        second = self.text(self.call())
        self.assertIsNotNone(second)
        self.assertIn("p129.md", second)
        self.assertLessEqual(len(second.encode("utf-8")), 9500)

    def test_a7_inert_without_workspace(self):
        self.write_page("p.md", page(["Bash"], "RULES"))
        env = self.env(CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        proc = self.run_exe({"session_id": "s", "tool_name": "Bash",
                             "tool_input": {"command": "ls"}}, args=[], env=env)
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))

    def test_a7_production_resolver(self):
        root = os.path.join(self.tmp, "ws")
        os.makedirs(os.path.join(root, ".claude", "tool-rules"))
        with open(os.path.join(root, ".claude", "tool-rules", "p.md"), "w") as fh:
            fh.write(page(["Bash"], "WS RULES"))
        cfg = os.path.join(self.tmp, "cfg.json")
        with open(cfg, "w") as fh:
            json.dump({"workspace_root": root}, fh)
        env = self.env(CLAUDE_TOOLKIT_CONFIG=cfg)
        proc = self.run_exe({"session_id": "s", "tool_name": "Bash",
                             "tool_input": {"command": "ls"}}, args=[], env=env)
        self.assertIn("WS RULES", self.text(proc))

    def test_a8_malformed_and_unparseable(self):
        proc = self.run_exe(None, raw="not json")
        self.assertEqual((proc.returncode, proc.stdout), (0, ""))
        self.write_page("nofm.md", "just text, no frontmatter")
        self.write_page("notools.md", "---\nname: x\n---\nbody")
        self.assertIsNone(self.text(self.call()))

    def test_a10_output_shape(self):
        self.write_page("a.md", page(["Bash"], "RULES"))
        out = json.loads(self.call().stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertEqual(list(out), ["hookSpecificOutput"])

    def test_state_file_sanitized_and_stale_pruned(self):
        self.write_page("a.md", page(["Bash"], "RULES"))
        old = os.path.join(self.state, "old.json")
        with open(old, "w") as fh:
            fh.write("{}")
        stamp = os.path.getmtime(old) - 8 * 86400
        os.utime(old, (stamp, stamp))
        self.call(session="../evil/id")
        names = os.listdir(self.state)
        self.assertNotIn("old.json", names)
        self.assertIn("evilid.json", names)


class TestStateEvidence(Sandbox):
    """The state file is the audit's evidence that the hook ran for a session."""

    def state_file(self, sid="s1"):
        return os.path.join(self.state, sid + ".json")

    def test_compact_reset_keeps_an_empty_file(self):
        self.write_page("a.md", page(["Bash"], "RULES"))
        self.call()
        self.run_exe({"session_id": "s1", "source": "compact"}, args=["--reset"])
        self.assertTrue(os.path.exists(self.state_file()))
        with open(self.state_file()) as fh:
            self.assertEqual(json.load(fh), {"emitted": []})
        self.assertIn("RULES", self.text(self.call()))

    def test_reset_never_creates_a_file(self):
        self.write_page("a.md", page(["Bash"], "RULES"))
        self.run_exe({"session_id": "fresh", "source": "compact"}, args=["--reset"])
        self.assertFalse(os.path.exists(self.state_file("fresh")))

    def test_matching_call_refreshes_mtime(self):
        self.write_page("a.md", page(["Bash"], "RULES"))
        self.call()
        old = time.time() - 5 * 86400
        os.utime(self.state_file(), (old, old))
        self.assertIsNone(self.text(self.call()))
        self.assertGreaterEqual(os.path.getmtime(self.state_file()), time.time() - 60)

    def test_non_matching_call_leaves_mtime(self):
        self.write_page("a.md", page(["Bash(*gads*)"], "RULES"))
        self.assertIn("RULES", self.text(self.call(command="gads pull")))
        old = time.time() - 5 * 86400
        os.utime(self.state_file(), (old, old))
        self.assertIsNone(self.text(self.call(command="ls")))
        self.assertLess(abs(os.path.getmtime(self.state_file()) - old), 1)


class TestHooksJson(unittest.TestCase):
    def test_a9_registrations(self):
        with open(HOOKS_JSON, encoding="utf-8") as fh:
            hooks = json.load(fh)["hooks"]
        import tool_rules_lib as lib
        post = [b for b in hooks["PostToolUse"] if b.get("matcher") == lib.HOOK_MATCHER]
        self.assertEqual(len(post), 1)
        self.assertTrue(post[0]["hooks"][0]["command"].endswith(
            "tools/tool-rules/claude-tool-rules"))
        start = [h["command"] for b in hooks["SessionStart"] for h in b["hooks"]]
        self.assertTrue(any(c.endswith("claude-tool-rules --reset") for c in start))


if __name__ == "__main__":
    unittest.main()
