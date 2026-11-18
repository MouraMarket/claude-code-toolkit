"""Tests for prove_lib's child runs, stages, grader and proof file (Part B Task 2, R1-R14).

No test invokes a real `claude` binary: `prove` gets a FakeRunner that answers by
inspecting argv, and `run_child` is exercised against a throwaway fake script.
"""

import contextlib
import fnmatch
import glob
import hashlib
import io
import json
import math
import os
import random
import re
import stat
import subprocess
import sys
import tempfile
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

from test_distill_lib import read, write  # noqa: E402
from test_prove_fixture import Cfg, FixtureBase, tree_hashes  # noqa: E402

TOOL_COMMAND = "python3 _probe/m.py graph.facebook.com"
GOOD_WINDOW = {"five_hour": {"utilization": 0.1}}
PASS_TEXT = '{"verdict": "pass", "why": "ok"}'
TOKEN_RE = re.compile(r"Fixture check token: (\S+)")


class FakeRunner(object):
    """Answers by inspecting argv; the tool canary simulates the harness honestly."""

    def __init__(self, tool_mode="honest", path_mode="ok", graders=(PASS_TEXT,),
                 window=GOOD_WINDOW, overage=False, raise_first=None,
                 opus_files=None, touch_surface=False, key_source="none", provider=None):
        self.calls = []
        self.tool_mode, self.path_mode = tool_mode, path_mode
        self.graders, self.window, self.overage = list(graders), window, overage
        self.raise_first = raise_first
        self.opus_files = opus_files if opus_files is not None else {"src/out.txt": "done\n"}
        self.touch_surface = touch_surface
        self.key_source, self.provider = key_source, provider
        self.opus_seen = []
        self.grader_calls = 0

    def __call__(self, argv, cwd, env, timeout):
        self.calls.append((list(argv), cwd, dict(env)))
        if self.raise_first is not None and len(self.calls) == 1:
            raise self.raise_first
        tools = argv[argv.index("--tools") + 1]
        model = argv[argv.index("--model") + 1]
        if self.touch_surface and len(self.calls) == 1:
            with open(os.path.join(cwd, "mem", "MEMORY.md"), "a", encoding="utf-8") as fh:
                fh.write("\n- sneaky\n")
        if tools == "":
            index = min(self.grader_calls, len(self.graders) - 1)
            self.grader_calls += 1
            text = self.graders[index]
        elif model == "haiku" and tools == "Bash":
            text = self.tool_canary(argv, cwd, env)
        elif model == "haiku" and tools == "Read":
            text = self.path_canary(argv, cwd)
        else:
            text = self.opus(cwd)
        return {"cwd": cwd, "exit": 0, "result_text": text, "window": self.window,
                "overage": self.overage, "pdir_preexisting": True,
                "model": model, "api_key_source": self.key_source,
                "api_provider": self.provider,
                "init": {"type": "system", "subtype": "init", "apiKeySource": self.key_source,
                         "session_id": "fake-%d" % len(self.calls)}}

    def tool_canary(self, argv, cwd, env):
        if self.tool_mode == "drop":
            return "nothing"
        if self.tool_mode == "blocked":
            write(os.path.join(cwd, "_proof", "blocked.jsonl"),
                  json.dumps({"tool_name": "Bash", "tool_input": {"command": "typo"}}) + "\n")
            return "nothing"
        if self.tool_mode == "cheat":
            tokens = []
            for path in sorted(glob.glob(os.path.join(cwd, ".claude", "tool-rules", "*.md"))):
                tokens += TOKEN_RE.findall(read(path))
            return " ".join(tokens) or "nothing"
        command = re.search(r"`([^`]+)`", argv[2]).group(1)
        if "--allowedTools" not in argv or \
                argv[argv.index("--allowedTools") + 1] != "Bash(%s)" % command:
            return "denied"
        settings = json.loads(read(os.path.join(cwd, ".claude", "settings.json")))
        hook = settings["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        payload = {"session_id": "canary-%d" % len(self.calls),
                   "hook_event_name": "PostToolUse", "tool_name": "Bash",
                   "tool_input": {"command": command}}
        done = subprocess.run(hook, shell=True, input=json.dumps(payload), env=env,
                              cwd=cwd, capture_output=True, text=True)
        context = ""
        if done.stdout.strip():
            context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        return " ".join(TOKEN_RE.findall(context)) or "nothing"

    def path_canary(self, argv, cwd):
        prompt = argv[2]
        target = prompt[len("Read "):prompt.index(", then")]
        log = os.path.join(cwd, "_proof", "loaded.jsonl")
        for page in sorted(glob.glob(os.path.join(cwd, ".claude", "rules", "*.md"))):
            globs = re.findall(r'^\s*-\s*"(.*)"\s*$', read(page), re.M)
            if any(fnmatch.fnmatch(target, g) for g in globs):
                reasons = ["path_glob_match"]
                if self.path_mode == "session_start":
                    reasons.append("session_start")
                for reason in reasons:
                    with open(log, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({"file_path": page, "load_reason": reason}) + "\n")
        return "OK"

    def opus(self, cwd):
        self.opus_seen.append(sorted(
            os.path.relpath(os.path.join(b, n), cwd)
            for b, _d, names in os.walk(os.path.join(cwd, "src")) for n in names))
        for rel, text in self.opus_files.items():
            write(os.path.join(cwd, rel), text)
        return "done"


def hash_live(*dirs):
    out = {}
    for d in dirs:
        out[d] = tree_hashes(d)
    return out


class ProveBase(FixtureBase):
    def setUp(self):
        super().setUp()
        import distill_claim
        self.claimmod = distill_claim
        self.claimmod.claim("s1")
        self.manifest["behaviour"] = [{
            "memory": "move.md", "files": {"src/a.py": "x = 1\n"},
            "prompt": "Edit src/a.py.", "pass_if": "a file was written"}]
        self.mpath = os.path.join(self.tmp, "draft.json")
        write(self.mpath, json.dumps(self.manifest))
        self.cfg = Cfg(fixture_root=self.base)
        self.env = dict(os.environ)
        for name in self.prove.PAID_AUTH_VARS:
            self.env[name] = "secret"
        self.env["CLAUDECODE"] = "1"
        self.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "0"
        self.proof_path = self.mpath + ".proof.json"

    def live(self):
        return hash_live(self.mem, os.path.join(self.ws, ".claude", "rules"),
                         os.path.join(self.ws, ".claude", "tool-rules"))

    def run_prove(self, runner, **kw):
        args = dict(manifest_path=self.mpath, memory_dir=self.mem, workspace=self.ws,
                    session_id="s1", config=self.cfg, config_doc=self.config_doc,
                    runner=runner, binary="/fake/claude", today="2026-11-01",
                    rng=random.Random(1), base_env=self.env)
        args.update(kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.prove.prove(**args)
        return code, buf.getvalue()

    def proof(self):
        return json.loads(read(self.proof_path))

    def left(self):
        """What the base holds besides prove's lock file, which stays by design."""
        return sorted(n for n in os.listdir(self.base) if n != self.prove.PROVE_LOCK)

    def apply(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            return self.lib.apply(self.mpath, self.mem, self.ws, session_id="s1")


class TestR1Pass(ProveBase):
    def test_r1_pass_and_apply_accepts(self):
        before = self.live()
        runner = FakeRunner()
        code, out = self.run_prove(runner)
        self.assertEqual(code, 0, out)
        after = self.live()  # before apply (R13)
        self.assertEqual(before, after)
        proof = self.proof()
        self.assertEqual(proof["verdict"], "PASS")
        self.assertEqual(list(proof), ["verdict", "manifest_sha256", "roots",
                                       "dependencies", "checks", "proved_at"])
        with open(self.mpath, "rb") as fh:
            self.assertEqual(proof["manifest_sha256"], hashlib.sha256(fh.read()).hexdigest())
        # R14: the dependency set is the live roots'.
        expected = json.loads(json.dumps(
            self.lib.dependency_set(self.manifest, self.mem, self.ws)))
        self.assertEqual(proof["dependencies"], expected)
        stages = [c["stage"] for c in proof["checks"]]
        self.assertEqual(stages, ["structural", "negative control", "tool canary",
                                  "path canary", "behaviour"])
        self.assertTrue(all(c["result"] == "PASS" for c in proof["checks"]))
        self.assertIn("evidence, not a guarantee", out)
        self.assertIn("verdict: PASS", out)
        argv = runner.calls[0][0]
        self.assertEqual(argv[argv.index("--tools") + 1], "Bash")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Bash(%s)" % TOOL_COMMAND)
        self.assertEqual(self.apply(), 0)

    def test_r11_disposal(self):
        grader_glob = os.path.join(tempfile.gettempdir(), "index-distill-grader-*")
        existing = set(glob.glob(grader_glob))
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 0, out)
        self.assertEqual(self.left(), [])
        self.assertEqual(set(glob.glob(grader_glob)), existing)

    def test_r11_keep(self):
        code, out = self.run_prove(FakeRunner(), keep=True)
        self.assertEqual(code, 0, out)
        kept = self.left()
        self.assertEqual(len(kept), 1)
        root = os.path.join(self.base, kept[0])
        self.assertIn("fixture kept at %s" % os.path.realpath(root), out)

    def test_r11_runner_raises_mid_stage(self):
        with self.assertRaises(RuntimeError):
            self.run_prove(FakeRunner(raise_first=RuntimeError("boom")))
        self.assertEqual(self.left(), [])


class TestCodeReviewInterruptedRun(ProveBase):
    """Code Review (Part B): a run interrupted by a signal never returns a
    record, so its projects dir must be known from BEFORE the call."""

    def interrupted_at(self, call_number):
        projects = os.path.join(self.tmp, "projects-root")
        os.makedirs(projects)
        with mock.patch.dict(os.environ, {"HARNESS_CHECK_PROJECTS_ROOT": projects}):
            made = []

            def runner(argv, cwd, env, timeout):
                if len(made) + 1 == call_number:
                    pdir = self.prove.harness_lib.projects_dir_for(cwd)
                    os.makedirs(pdir)
                    made.append(pdir)
                    raise self.prove.Terminated()
                made.append(None)
                return FakeRunner()(argv, cwd, env, timeout)

            with self.assertRaises(self.prove.Terminated):
                self.run_prove(runner)
            pdir = made[-1]
            self.assertTrue(pdir and pdir.startswith(projects))
            self.assertFalse(os.path.exists(pdir), pdir)
            self.assertEqual(self.left(), [])

    def test_interrupted_canary_run_leaves_no_projects_dir(self):
        self.interrupted_at(1)

    def test_interrupted_grader_run_leaves_no_projects_dir(self):
        # Calls: tool canary, path canary, behaviour task, grader.
        self.interrupted_at(4)


class TestOrphanSweep(ProveBase):
    """prove starts by removing proof dirs an interrupted run left behind."""

    def orphan(self, name, data="real memory copy\n"):
        path = os.path.join(self.base, name)
        write(os.path.join(path, "mem", "MEMORY.md"), data)
        return path

    def test_orphans_are_swept_and_named(self):
        fixture = self.orphan("index-distill-ab12_xyz")
        grader = self.orphan("index-distill-grader-q9w8e7r6")
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(fixture))
        self.assertFalse(os.path.exists(grader))
        self.assertIn("left behind: %s" % fixture, out)
        self.assertIn("left behind: %s" % grader, out)
        self.assertEqual(self.left(), [])

    def test_only_exact_mkdtemp_names_are_swept(self):
        keep = [self.orphan(name) for name in (
            "index-distill-short", "index-distill-toolongname1", "index-distill-AB12CDEF",
            "index-distill-grader-short", "index-distill-ab12cdef-copy", "other-ab12cdef",
            "index-distill-ab12cdef\n", "index-distill-grader-ab12cdef\n")]
        loose = os.path.join(self.base, "index-distill-filefile")
        write(loose, "a file, not a dir\n")
        outside = os.path.join(self.tmp, "outside")
        write(os.path.join(outside, "keep.txt"), "target\n")
        link = os.path.join(self.base, "index-distill-linklink")
        os.symlink(outside, link)
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 0, out)
        for path in keep + [loose, link]:
            self.assertTrue(os.path.lexists(path), path)
        self.assertEqual(read(os.path.join(outside, "keep.txt")), "target\n")
        self.assertNotIn("left behind", out)
        self.assertNotIn("could NOT remove", out)  # the symlink is never even tried

    def test_no_sweep_without_the_claim(self):
        fixture = self.orphan("index-distill-ab12cdef")
        self.claimmod.release("s1")
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 1, out)
        self.assertTrue(os.path.isdir(fixture))

    def test_a_running_prove_is_never_swept(self):
        # Repro: the claim admits the same session twice.
        live = self.orphan("index-distill-ab12cdef")
        import portable
        with portable.exclusive_lock(os.path.join(self.base, self.prove.PROVE_LOCK)) as held:
            self.assertTrue(held)
            runner = FakeRunner()
            code, out = self.run_prove(runner)
        self.assertEqual(code, 1, out)
        self.assertIn("another prove is running against", out)
        self.assertTrue(os.path.isdir(live))
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(self.proof_path))  # the running proof's file
        code, out = self.run_prove(FakeRunner())  # released: the next prove sweeps
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.exists(live))

    def test_the_lock_file_is_all_that_stays(self):
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 0, out)
        self.assertEqual(os.listdir(self.base), [self.prove.PROVE_LOCK])
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 0, out)
        self.assertNotIn("left behind", out)

    def test_a_dir_that_cannot_be_removed_is_reported(self):
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("needs POSIX permissions and a non-root user")
        fixture = self.orphan("index-distill-ab12cdef")
        locked = os.path.join(fixture, "mem")
        os.chmod(locked, 0o500)
        # tearDown's TemporaryDirectory cleanup can run first and remove it.
        self.addCleanup(lambda: os.path.isdir(locked) and os.chmod(locked, 0o700))
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 0, out)
        self.assertIn("could NOT remove a leftover proof dir", out)
        self.assertIn(fixture, out)

    def test_grader_cwd_is_under_the_fixture_base(self):
        runner = FakeRunner()
        code, out = self.run_prove(runner)
        self.assertEqual(code, 0, out)
        graders = [cwd for argv, cwd, env in runner.calls
                   if argv[argv.index("--tools") + 1] == ""]
        self.assertEqual(len(graders), 1)
        self.assertEqual(os.path.dirname(graders[0]), os.path.realpath(self.base))
        self.assertTrue(os.path.basename(graders[0]).startswith("index-distill-grader-"))


class TestP1Disposal(ProveBase):
    def test_p1_apply_failure_in_build_propagates_and_disposes(self):
        for exc in (self.lib.ApplyFailure("boom"), KeyboardInterrupt()):
            with mock.patch.object(self.lib, "apply_manifest", side_effect=exc):
                with self.assertRaises(type(exc)):
                    self.run_prove(FakeRunner())
            self.assertEqual([n for n in self.left()
                              if n.startswith("index-distill-")], [])
            self.assertEqual(self.proof()["verdict"], "INCOMPLETE")


class TestP3Auth(ProveBase):
    def test_p3_subscription_passes_and_records_runs(self):
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 0, out)
        runs = [r for c in self.proof()["checks"] for r in c["runs"]]
        self.assertEqual([r["model"] for r in runs], ["haiku", "haiku", "opus", "opus"])
        self.assertEqual(runs[0], {"model": "haiku", "api_key_source": "none",
                                   "api_provider": None, "five_hour": 0.1})
        self.assertIn("[haiku key=none provider=None 5h=0.1]", out)

    def assert_paid(self, **kw):
        runner = FakeRunner(**kw)
        code, out = self.run_prove(runner)
        self.assertEqual(code, 3, out)
        self.assertIn("paid auth", out)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.proof()["verdict"], "HALTED")

    def test_p3_api_key_source_halts(self):
        self.assert_paid(key_source="ANTHROPIC_API_KEY")

    def test_p3_managed_key_halts(self):
        self.assert_paid(key_source="/login managed key")

    def test_p3_bedrock_halts(self):
        self.assert_paid(provider="bedrock")

    def test_p3_first_party_provider_is_fine(self):
        code, out = self.run_prove(FakeRunner(provider="firstParty"))
        self.assertEqual(code, 0, out)

    def test_p3_grader_run_is_checked_too(self):
        class GraderPaid(FakeRunner):
            def __call__(self, argv, cwd, env, timeout):
                rec = super().__call__(argv, cwd, env, timeout)
                if argv[argv.index("--tools") + 1] == "":
                    rec["api_key_source"] = "ANTHROPIC_API_KEY"
                return rec
        code, out = self.run_prove(GraderPaid())
        self.assertEqual(code, 3, out)
        self.assertIn("paid auth", out)


class TestP5Blocked(ProveBase):
    def test_p5_blocked_call_is_a_harness_fault_not_a_fail(self):
        code, out = self.run_prove(FakeRunner(tool_mode="blocked"))
        self.assertEqual(code, 3, out)
        self.assertIn("harness fault: the child's call was blocked by containment", out)
        self.assertEqual(self.proof()["verdict"], "HALTED")

    def test_p5_plain_drop_still_fails(self):
        code, out = self.run_prove(FakeRunner(tool_mode="drop"))
        self.assertEqual(code, 1, out)


class TestMR4OneRead(ProveBase):
    def test_mr4_manifest_is_read_exactly_once(self):
        import builtins
        alt = os.path.join(self.tmp, "alt.json")
        write(alt, json.dumps({"version": 99}))
        with open(self.mpath, "rb") as fh:
            first = fh.read()
        real_open = builtins.open
        reads = []

        def fake_open(file, *args, **kw):
            if isinstance(file, (str, bytes, os.PathLike)) and os.fspath(file) == self.mpath:
                reads.append(file)
                if len(reads) > 1:
                    return real_open(alt, *args, **kw)
            return real_open(file, *args, **kw)

        with mock.patch.object(builtins, "open", fake_open):
            code, out = self.run_prove(FakeRunner())
        self.assertEqual(len(reads), 1)
        self.assertEqual(code, 0, out)
        proof = self.proof()
        self.assertEqual(proof["manifest_sha256"], hashlib.sha256(first).hexdigest())

    def test_mr4_bad_bytes_fail_with_load_manifest_text(self):
        for raw, text in ((b"[1]", "manifest must be a JSON object"),
                          (b"\xff\xfe", "manifest is not valid JSON"),
                          (b"{", "manifest is not valid JSON")):
            with open(self.mpath, "wb") as fh:
                fh.write(raw)
            code, out = self.run_prove(FakeRunner())
            self.assertEqual(code, 1, out)
            self.assertIn(text, out)
            self.assertEqual(self.proof()["verdict"], "FAIL")


class TestStopsAndVerdicts(ProveBase):
    def test_r2_dropped_token_fails_and_stops(self):
        before = self.live()
        runner = FakeRunner(tool_mode="drop")
        code, out = self.run_prove(runner)
        self.assertEqual(code, 1, out)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(self.proof()["verdict"], "FAIL")
        self.assertEqual(before, self.live())

    def test_r2b_token_without_hook_record(self):
        code, out = self.run_prove(FakeRunner(tool_mode="cheat"))
        self.assertEqual(code, 1, out)
        self.assertIn("hook never recorded", out)

    def test_r2b_inert_hook_command(self):
        inert = {"matcher": "Bash|mcp__.*",
                 "hooks": [{"type": "command", "command": "true", "timeout": 5}]}
        with mock.patch.object(self.prove, "production_posttooluse", return_value=inert):
            code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 1, out)

    def test_r3_session_start_fails(self):
        code, out = self.run_prove(FakeRunner(path_mode="session_start"))
        self.assertEqual(code, 1, out)
        self.assertEqual(self.proof()["verdict"], "FAIL")

    def test_r4_grader_fail(self):
        code, out = self.run_prove(
            FakeRunner(graders=['{"verdict": "fail", "why": "x"}']))
        self.assertEqual(code, 1, out)
        self.assertIn("x", self.proof()["checks"][-1]["detail"])

    def test_r4_restore_between_cases(self):
        self.manifest["behaviour"].append(dict(self.manifest["behaviour"][0]))
        write(self.mpath, json.dumps(self.manifest))
        runner = FakeRunner()
        code, out = self.run_prove(runner)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(runner.opus_seen), 2)
        self.assertEqual(runner.opus_seen[0], ["src/a.py"])
        self.assertEqual(runner.opus_seen[1], ["src/a.py"])

    def test_r5_unparseable_grader_halts(self):
        code, out = self.run_prove(FakeRunner(graders=["sure, pass"]))
        self.assertEqual(code, 3, out)
        self.assertEqual(self.proof()["verdict"], "HALTED")

    def test_r5_fenced_grader_parses(self):
        code, out = self.run_prove(FakeRunner(
            graders=['```json\n{"verdict": "pass", "why": "y"}\n```']))
        self.assertEqual(code, 0, out)

    def test_r6_budget_halts_first_run(self):
        before = self.live()
        runner = FakeRunner(window={"five_hour": {"utilization": 0.9}})
        code, out = self.run_prove(runner)
        self.assertEqual(code, 3, out)
        self.assertEqual(len(runner.calls), 1)
        self.assertEqual(before, self.live())

    def test_r6_unreadable_windows_halt(self):
        windows = [None, {}, {"seven_day": {"utilization": 0.1}}, {"five_hour": {}},
                   {"five_hour": {"utilization": float("nan")}},
                   {"five_hour": {"utilization": 1.5}},
                   {"five_hour": {"utilization": "0.1"}},
                   {"five_hour": {"utilization": True}},
                   {"five_hour": None}, "junk"]
        for window in windows:
            with self.subTest(window=window):
                runner = FakeRunner(window=window)
                code, out = self.run_prove(runner)
                self.assertEqual(code, 3, out)
                self.assertEqual(len(runner.calls), 1)

    def test_r6_overage_halts(self):
        code, out = self.run_prove(FakeRunner(overage=True))
        self.assertEqual(code, 3, out)

    def test_r6_positive_control(self):
        code, out = self.run_prove(FakeRunner(window={"five_hour": {"utilization": 0.1}}))
        self.assertEqual(code, 0, out)

    def test_r7_surface_change_halts(self):
        code, out = self.run_prove(FakeRunner(touch_surface=True))
        self.assertEqual(code, 3, out)
        self.assertIn("instruction surface changed", out)

    def test_r10_structural_failure_runs_nothing(self):
        runner = FakeRunner()
        code, out = self.run_prove(
            runner, config=Cfg(fixture_root=self.base, distill_at_chars=10))
        self.assertEqual(code, 1, out)
        self.assertEqual(len(runner.calls), 0)
        code, out = self.run_prove(
            FakeRunner(), config=Cfg(fixture_root=self.base, distill_at_chars=5000))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.proof()["checks"][0]["result"], "PASS")

    def test_r12_children_never_get_paid_vars(self):
        self.env["CLAUDE_CODE_USE_GATEWAY"] = "1"
        self.env["CLAUDE_CODE_USE_SOMETHING_NEW"] = "1"
        runner = FakeRunner()
        code, out = self.run_prove(runner)
        self.assertEqual(code, 0, out)
        self.assertEqual(len(runner.calls), 4)  # tool canary, path canary, behaviour, grader
        for argv, _cwd, env in runner.calls:
            for name in self.prove.PAID_AUTH_VARS:
                self.assertNotIn(name, env)
            self.assertFalse([k for k in env if k.startswith("CLAUDE_CODE_USE_")])
            self.assertNotIn("CLAUDECODE", env)
            if argv[argv.index("--tools") + 1] == "":
                self.assertEqual(env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
            else:
                self.assertNotIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY", env)
                self.assertIn("CLAUDE_TOOLKIT_CONFIG", env)
                self.assertIn("CLAUDE_TOOL_RULES_STATE_DIR", env)

    def test_r14_invalid_manifest(self):
        bad = dict(self.manifest, extra=1)
        write(self.mpath, json.dumps(bad))
        runner = FakeRunner()
        code, out = self.run_prove(runner)
        self.assertEqual(code, 1, out)
        self.assertIn("manifest keys must be exactly", out)
        self.assertEqual(len(runner.calls), 0)
        proof = self.proof()
        self.assertEqual(proof["verdict"], "FAIL")
        self.assertEqual(proof["dependencies"], {})
        self.assertEqual(list(proof)[-1], "proved_at")

    def test_no_binary_halts(self):
        with mock.patch.object(self.prove, "resolve_binary", return_value=None):
            code, out = self.run_prove(FakeRunner(), binary=None)
        self.assertEqual(code, 3, out)
        self.assertIn("proof cannot run here", out)
        self.assertEqual(self.proof()["verdict"], "HALTED")


class TestClaimAndInvalidate(ProveBase):
    def test_r8_claim_not_held(self):
        write(self.proof_path, json.dumps({"verdict": "PASS", "old": True}))
        with open(self.proof_path, "rb") as fh:
            old = fh.read()
        self.claimmod.release("s1")
        self.claimmod.claim("other")
        runner = FakeRunner()
        code, out = self.run_prove(runner)
        self.assertEqual(code, 1, out)
        self.assertIn("claude-index-distill claim first", out)
        self.assertEqual(len(runner.calls), 0)
        with open(self.proof_path, "rb") as fh:
            self.assertEqual(fh.read(), old)
        code, out = self.run_prove(runner, session_id=None)
        self.assertEqual(code, 1, out)
        with open(self.proof_path, "rb") as fh:
            self.assertEqual(fh.read(), old)

    def test_r9_no_stale_pass(self):
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 0, out)
        code, out = self.run_prove(FakeRunner(tool_mode="drop"))
        self.assertEqual(code, 1, out)
        self.assertEqual(self.proof()["verdict"], "FAIL")
        self.assertEqual(self.apply(), 1)

    def test_r9b_interrupted_rerun(self):
        code, out = self.run_prove(FakeRunner())
        self.assertEqual(code, 0, out)
        with self.assertRaises(KeyboardInterrupt):
            self.run_prove(FakeRunner(raise_first=KeyboardInterrupt()))
        self.assertEqual(self.proof()["verdict"], "INCOMPLETE")
        self.assertEqual(self.apply(), 1)
        self.assertEqual(self.left(), [])


class TestPureHelpers(ProveBase):
    def test_r12_argv_and_env(self):
        prove = self.prove
        argv = prove.child_argv("/b/claude", "PROMPT", "/m", ["--tools", "Bash"])
        self.assertEqual(argv[:3], ["/b/claude", "-p", "PROMPT"])
        self.assertLess(argv.index("PROMPT"), argv.index("--tools"))
        self.assertIn("--no-session-persistence", argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project")
        self.assertNotIn("--dir", argv)
        self.assertEqual(json.loads(argv[argv.index("--settings") + 1]),
                         {"autoMemoryDirectory": "/m"})
        self.assertEqual(prove.grader_argv("/b/claude", "P")[-2:], ["--tools", ""])
        fx = self.build()
        env = prove.child_env(fx, self.env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertNotIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY", env)
        self.assertEqual(env["CLAUDE_TOOLKIT_CONFIG"], os.path.join(fx.proof, "config.json"))
        self.assertEqual(env["CLAUDE_TOOL_RULES_STATE_DIR"], os.path.join(fx.proof, "state"))
        genv = prove.grader_env(self.env)
        self.assertEqual(genv["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertNotIn("CLAUDECODE", genv)
        for name in prove.PAID_AUTH_VARS:
            self.assertNotIn(name, env)
            self.assertNotIn(name, genv)
            self.assertIn(name, self.env)  # the copy, not the original

    def test_resolve_binary(self):
        exe = os.path.join(self.tmp, "fakeclaude")
        write(exe, "#!/bin/sh\n")
        os.chmod(exe, stat.S_IRWXU)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_EXECPATH": exe}):
            self.assertEqual(self.prove.resolve_binary(), exe)
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with mock.patch.dict(os.environ, {"CLAUDE_CODE_EXECPATH": "/nonexistent/x",
                                          "PATH": empty}):
            self.assertIsNone(self.prove.resolve_binary())

    def test_must_stop_and_unusable(self):
        stop = self.prove.must_stop
        ok = {"window": GOOD_WINDOW, "overage": False}
        self.assertIsNone(stop(ok))
        self.assertEqual(stop(dict(ok, overage=True)), "overage flipped on")
        self.assertEqual(stop(dict(ok, window={"five_hour": {"utilization": 0.85}})),
                         "5h window at 85%")
        self.assertEqual(stop(dict(ok, window=None)), "usage unreadable")
        self.assertEqual(stop(dict(ok, window={"five_hour": {"utilization": math.inf}})),
                         "usage unreadable")
        self.assertEqual(stop(dict(ok, window={"five_hour": {"utilization": -0.1}})),
                         "usage unreadable")
        unusable = self.prove.unusable
        good = {"exit": 0, "init": {}, "result_text": "x"}
        self.assertIsNone(unusable(good))
        self.assertTrue(unusable(dict(good, exit=1)))
        self.assertTrue(unusable(dict(good, init=None)))
        self.assertTrue(unusable(dict(good, result_text=None)))

    def test_parse_verdict(self):
        parse = self.prove.parse_verdict
        self.assertEqual(parse(' {"verdict": "pass", "why": "y"} '),
                         {"verdict": "pass", "why": "y"})
        self.assertEqual(parse('```\n{"verdict": "fail", "why": "y"}\n```')["verdict"], "fail")
        for bad in ["sure, pass", '{"verdict": "pass"}', '{"verdict": "maybe", "why": "y"}',
                    '{"verdict": "pass", "why": 1}', '[1]',
                    '{"verdict": "pass", "why": "y", "x": 1}',
                    '```json\n{"verdict": "pass", "why": "y"}', "", None]:
            with self.subTest(bad=bad):
                self.assertIsNone(parse(bad))

    def test_grader_prompt(self):
        text = self.prove.grader_prompt("crit", "the reply", {"a.txt": "AAA"})
        self.assertIn("Criterion: crit", text)
        self.assertIn("<<<\nthe reply\n>>>", text)
        self.assertIn("=== a.txt ===\nAAA", text)
        self.assertIn("(none)", self.prove.grader_prompt("c", "r", {}))
        self.assertTrue(text.rstrip().endswith(
            '{"verdict": "pass" or "fail", "why": "<one sentence>"}'))


class TestRunChild(ProveBase):
    def script(self, body):
        path = os.path.join(self.tmp, "fake.py")
        write(path, "#!%s\n%s" % (sys.executable, body))
        os.chmod(path, stat.S_IRWXU)
        return path

    def test_parses_stream(self):
        body = (
            "import json\n"
            "for rec in [{'type': 'system', 'subtype': 'init', 'session_id': 's'},\n"
            "            {'type': 'rate_limit_event', 'rate_limit_info': {'unifiedWindows': "
            "{'five_hour': {'utilization': 0.2}}, 'isUsingOverage': False}},\n"
            "            {'type': 'rate_limit_event', 'rate_limit_info': {'unifiedWindows': "
            "{'five_hour': {'utilization': 0.3}}, 'isUsingOverage': True}},\n"
            "            {'type': 'result', 'result': 'hello'}]:\n"
            "    print(json.dumps(rec))\n")
        cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(cwd)
        rec = self.prove.run_child([self.script(body)], cwd, dict(os.environ), 20)
        self.assertEqual(rec["exit"], 0)
        self.assertEqual(rec["cwd"], cwd)
        self.assertEqual(rec["init"]["session_id"], "s")
        self.assertEqual(rec["result_text"], "hello")
        self.assertEqual(rec["window"], {"five_hour": {"utilization": 0.3}})
        self.assertTrue(rec["overage"])
        self.assertIsInstance(rec["pdir_preexisting"], bool)

    def test_p3_records_model_and_auth(self):
        body = (
            "import json\n"
            "print(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': 's',\n"
            "                  'apiKeySource': 'none', 'apiProvider': 'firstParty'}))\n"
            "print(json.dumps({'type': 'result', 'result': 'hi'}))\n")
        cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(cwd)
        rec = self.prove.run_child([self.script(body), "--model", "haiku"], cwd,
                                   dict(os.environ), 20)
        self.assertEqual((rec["model"], rec["api_key_source"], rec["api_provider"]),
                         ("haiku", "none", "firstParty"))
        rec = self.prove.run_child([self.script("print('x')\n")], cwd, dict(os.environ), 20)
        self.assertEqual((rec["model"], rec["api_key_source"], rec["api_provider"]),
                         (None, None, None))
        self.assertIsNone(self.prove.paid_auth(rec))

    def test_failures_are_exit_minus_one(self):
        cwd = os.path.join(self.tmp, "cwd")
        os.makedirs(cwd)
        slow = self.script("import time\ntime.sleep(30)\n")
        rec = self.prove.run_child([slow], cwd, dict(os.environ), 1)
        self.assertEqual(rec["exit"], -1)
        rec = self.prove.run_child(["/nonexistent/claude"], cwd, dict(os.environ), 5)
        self.assertEqual(rec["exit"], -1)
        rec = self.prove.run_child([self.script("print('not json')\n")], cwd,
                                   dict(os.environ), 5)
        self.assertIsNone(rec["result_text"])
        self.assertIsNone(rec["window"])


if __name__ == "__main__":
    unittest.main()
