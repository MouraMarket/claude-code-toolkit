"""Tests for `claude-index-distill prove`: CLI wiring end to end with a fake binary (Part B Task 3, C1-C7).

No test invokes a real `claude` binary: CLAUDE_CODE_EXECPATH points at a fake script
that simulates the harness honestly (it runs the fixture's PostToolUse hook itself).
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import signal
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
for _p in (HERE, TOOL_DIR, os.path.join(REPO_ROOT, "lib"),
           os.path.join(REPO_ROOT, "tools", "memory-lint"),
           os.path.join(REPO_ROOT, "tools", "tool-rules"),
           os.path.join(REPO_ROOT, "tools", "harness-check")):
    sys.path.insert(0, _p)

from test_distill_lib import EXE, make_world, read, tool_page, write  # noqa: E402

TOOL_COMMAND = "python3 _probe/m.py graph.facebook.com"
TOOL_CONTENT = ("---\ntools:\n  - \"Bash(*graph.facebook.com*)\"\n---\n\n"
                "Facebook rule.\n")

FAKE_CLAUDE = r'''#!%(python)s
import json, os, re, subprocess, sys

argv = sys.argv
print(json.dumps({"type": "system", "subtype": "init", "session_id": "fake",
                  "scratchpad_path": None}))
print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
    "five_hour": {"utilization": float(os.environ.get("FAKE_UTIL", "0.1"))}},
    "isUsingOverage": False}}))
text = "OK"
tools = argv[argv.index("--tools") + 1] if "--tools" in argv else None
if tools == "Bash":
    command = re.search(r"`([^`]+)`", argv[2]).group(1)
    text = "denied"
    if "--allowedTools" in argv and \
            argv[argv.index("--allowedTools") + 1] == "Bash(%%s)" %% command:
        settings = json.load(open(os.path.join(os.getcwd(), ".claude", "settings.json")))
        hook = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        payload = {"session_id": "canary", "hook_event_name": "PostToolUse",
                   "tool_name": "Bash", "tool_input": {"command": command}}
        done = subprocess.run(hook, shell=True, input=json.dumps(payload),
                              cwd=os.getcwd(), capture_output=True, text=True)
        context = ""
        if done.stdout.strip():
            context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        text = " ".join(re.findall(r"Fixture check token: (\S+)", context)) or "nothing"
print(json.dumps({"type": "result", "subtype": "success", "result": text}))
'''

# Writes its pid, then sleeps on its first call (the tool canary).
SLEEPY_CLAUDE = r'''#!%(python)s
import os, time
with open(os.environ["FAKE_PID_FILE"], "w") as fh:
    fh.write(str(os.getpid()))
time.sleep(30)
'''

GARBAGE_CLAUDE = "#!%(python)s\nprint('garbage, not json')\n"


class CliBase(unittest.TestCase):
    SESSION = "mine"

    def setUp(self):
        import distill_lib
        self.lib = distill_lib
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self.ws, self.mem, self.manifest = make_world(self.tmp)
        write(os.path.join(self.home, ".claude", "settings.json"),
              json.dumps({"autoMemoryDirectory": self.mem}))
        self.config = os.path.join(self.tmp, "config.json")
        write(self.config, json.dumps({"workspace_root": self.ws, "owner_name": "Sam"}))
        # One tool page and one move into it.
        self.manifest["pages"] = [tool_page(TOOL_CONTENT, probe=TOOL_COMMAND)]
        self.manifest["moves"][0]["page"] = ".claude/tool-rules/t.md"
        self.mpath = os.path.join(self.tmp, "draft.json")
        write(self.mpath, json.dumps(self.manifest))
        self.empty_bin = os.path.join(self.tmp, "emptybin")
        os.makedirs(self.empty_bin)
        self.tmpdir = os.path.join(self.tmp, "tmpdir")
        os.makedirs(self.tmpdir)
        self.fake = self.make_script("fake-claude", FAKE_CLAUDE)

    def tearDown(self):
        self._tmp.cleanup()

    def make_script(self, name, template):
        path = os.path.join(self.tmp, name)
        write(path, template % {"python": sys.executable})
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def env(self, session="mine", **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        for name in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_EXECPATH", "FAKE_UTIL"):
            env.pop(name, None)
        env.update(
            HOME=self.home, USERPROFILE=self.home, TMPDIR=self.tmpdir,
            CLAUDE_TOOLKIT_CONFIG=self.config,
            CLAUDE_INDEX_DISTILL_CLAIM=os.path.join(self.tmp, "claim"),
            CLAUDE_CODE_EXECPATH=self.fake,
            PATH=os.pathsep.join([self.empty_bin, os.path.dirname(sys.executable)]))
        if session:
            env["CLAUDE_CODE_SESSION_ID"] = session
        env.update(extra)
        return env

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, EXE] + list(args),
                              env=self.env() if env is None else env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def claim(self, session="mine"):
        done = self.cli("claim", env=self.env(session=session))
        self.assertEqual(done.returncode, 0, done.stderr)

    def verdict(self):
        return json.loads(read(self.mpath + ".proof.json"))["verdict"]


class TestC1Roots(CliBase):
    def test_c1_no_auto_memory_directory_is_exit_2(self):
        write(os.path.join(self.home, ".claude", "settings.json"), "{}")
        done = self.cli("prove", self.mpath)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertTrue(done.stderr.strip())
        self.assertNotIn("Traceback", done.stderr)


class TestC2Claim(CliBase):
    def test_c2_no_session_id_is_exit_1(self):
        done = self.cli("prove", self.mpath, env=self.env(session=None))
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)

    def test_c2_claimed_by_another_session_is_exit_1(self):
        self.claim(session="other")
        done = self.cli("prove", self.mpath)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("not held by this session", done.stdout)


class TestC3Chain(CliBase):
    def test_c3_prove_pass_then_apply(self):
        self.claim()
        done = self.cli("prove", self.mpath)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(self.verdict(), "PASS")
        self.assertIn("verdict: PASS", done.stdout)
        applied = self.cli("apply", self.mpath)
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.ws, ".claude", "tool-rules", "t.md")))

    def test_c3_keep_flag_keeps_the_fixture(self):
        self.claim()
        done = self.cli("prove", self.mpath, "--keep")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("fixture kept at", done.stdout)


class TestC4Halted(CliBase):
    def test_c4_high_utilization_halts(self):
        self.claim()
        done = self.cli("prove", self.mpath, env=self.env(FAKE_UTIL="0.9"))
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertEqual(self.verdict(), "HALTED")

    def test_c4_garbage_binary_is_halted_not_an_exception(self):
        self.claim()
        garbage = self.make_script("garbage-claude", GARBAGE_CLAUDE)
        done = self.cli("prove", self.mpath, env=self.env(CLAUDE_CODE_EXECPATH=garbage))
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertEqual(self.verdict(), "HALTED")
        self.assertNotIn("harness fault", done.stderr)


class TestC5NoBinary(CliBase):
    def test_c5_no_binary_halts(self):
        self.claim()
        env = self.env()
        env.pop("CLAUDE_CODE_EXECPATH")
        env["PATH"] = self.empty_bin
        done = self.cli("prove", self.mpath, env=env)
        self.assertEqual(done.returncode, 3, done.stdout + done.stderr)
        self.assertIn("proof cannot run", done.stdout)
        self.assertEqual(self.verdict(), "HALTED")


class TestC6Usage(CliBase):
    def test_c6_no_manifest_is_exit_2(self):
        done = self.cli("prove")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("usage", done.stderr)

    def test_c6_unknown_flag_is_exit_2(self):
        done = self.cli("prove", self.mpath, "--bogus")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("usage", done.stderr)

    def test_c6_two_manifests_is_exit_2(self):
        done = self.cli("prove", self.mpath, self.mpath)
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)


class TestP2Terminated(CliBase):
    def test_p2_sigterm_disposes_the_fixture(self):
        self.claim()
        sleepy = self.make_script("sleepy-claude", SLEEPY_CLAUDE)
        pid_file = os.path.join(self.tmp, "child.pid")
        base = os.path.join(self.tmpdir, "claude-index-distill")
        env = self.env(CLAUDE_CODE_EXECPATH=sleepy, FAKE_PID_FILE=pid_file)
        proc = subprocess.Popen([sys.executable, EXE, "prove", self.mpath], env=env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            for _ in range(200):  # 0.1 s polls, 20 s at most
                if os.path.exists(pid_file) and read(pid_file).strip():
                    break
                time.sleep(0.1)
            else:
                self.fail("the fake binary never started")
            # Positive control: the fixture exists while the child runs.
            self.assertTrue([n for n in os.listdir(base) if n.startswith("index-distill-")])
            proc.send_signal(signal.SIGTERM)
            out, err = proc.communicate(timeout=20)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 3, out + err)
        self.assertIn("terminated by signal", err)
        self.assertEqual([n for n in os.listdir(base) if n.startswith("index-distill-")], [])


class TestC7HarnessFault(CliBase):
    def load_cli(self):
        loader = importlib.machinery.SourceFileLoader("claude_index_distill_cli", EXE)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        return module

    def test_c7_unexpected_exception_is_exit_3(self):
        import prove_lib
        cli = self.load_cli()
        err = io.StringIO()
        with mock.patch.object(self.lib, "resolve_roots",
                               return_value=(self.mem, self.ws)), \
                mock.patch.object(prove_lib, "prove",
                                  side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            code = cli.main(["prove", self.mpath])
        self.assertEqual(code, 3)
        self.assertIn("harness fault: RuntimeError: boom", err.getvalue())

    def test_c7_session_and_keep_reach_prove(self):
        import prove_lib
        cli = self.load_cli()
        with mock.patch.object(self.lib, "resolve_roots",
                               return_value=(self.mem, self.ws)), \
                mock.patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-x"}), \
                mock.patch.object(prove_lib, "prove", return_value=0) as fake:
            self.assertEqual(cli.main(["prove", self.mpath, "--keep"]), 0)
        fake.assert_called_once_with(self.mpath, self.mem, self.ws,
                                     session_id="sess-x", keep=True)


if __name__ == "__main__":
    unittest.main()
