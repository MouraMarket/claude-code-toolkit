#!/usr/bin/env python3
"""Mutation controls for the capability evidence checker (lib/capability_evidence.py).

The silent failure this guards: the checker calls a capability evidenced when it
is not (a suite that ran nothing, skipped everything, or never loaded the
recorder), and nothing downstream complains. Each control builds a synthetic
mini-repo with one defect, runs it through the REAL run_suite subprocess layer
under the interpreter running this test, and asserts both the state and that
the reason names the thing that is wrong.

Run: python3 scripts/tests/test_check_capabilities.py
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.join(REPO, "lib"))
import capability_evidence as ev  # noqa: E402

# The real ~/.cache, never TMPDIR or /tmp (the danger-guard trap).
CACHE = os.path.join(os.path.expanduser("~"), ".cache")
VERSION = "%d.%d" % sys.version_info[:2]
OTHER = "3.13" if VERSION == "3.9" else "3.9"

PASSING = '''
import unittest
class T(unittest.TestCase):
    def test_a(self):
        self.assertTrue(True)
if __name__ == "__main__":
    unittest.main()
'''
ONE_SKIP = '''
import unittest
class T(unittest.TestCase):
    def test_a(self):
        self.assertTrue(True)
    def test_skipme(self):
        self.skipTest("no such thing here")
if __name__ == "__main__":
    unittest.main(verbosity=2)
'''
SUITE = "tools/alpha/tests/test_alpha.py"


def source(text):
    return textwrap.dedent(text).lstrip("\n")


def cap(ident, *tests, **extra):
    entry = {"id": ident, "kind": ident.split(":")[0], "tests": list(tests)}
    entry.update(extra)
    return entry


def good_live(repo, entry, caps):
    """A clean live record for `entry`, bound to its identity in `repo`."""
    identity = ev.skill_identity(repo, entry, {c["id"]: c for c in caps})
    return {"by_id": {entry["id"]: {"problems": [], "notes": [],
                                     "identity": identity["closure_sha256"]}}, "errors": []}


def skip_entry(test, python="any", suite=SUITE):
    return {"suite": suite, "test": test, "python": python, "reason": "synthetic"}


class EvidenceBase(unittest.TestCase):
    def setUp(self):
        os.makedirs(CACHE, exist_ok=True)
        self.base = tempfile.mkdtemp(prefix="check-capabilities-test-%d-" % os.getpid(),
                                     dir=CACHE)
        self.addCleanup(shutil.rmtree, self.base, True)
        self.repo = os.path.join(self.base, "repo")
        self.root = os.path.join(self.base, "runs")

    def write(self, rel, text, base=None):
        path = os.path.join(base or self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        later = time.time() + 2  # stale-bytecode trap: a rewrite must not look unchanged
        os.utime(path, (later, later))
        return path

    def run_suites(self, suites, recorder=True, timeout=120):
        results = []
        for rel, text in suites.items():
            self.write(rel, source(text))
            results.append(ev.run_suite(self.repo, rel, sys.executable, VERSION, self.root,
                                        timeout=timeout, recorder=recorder))
        return results

    def judge(self, caps, results, allowlist=(), live=None, selected=None, check_unowned=False):
        registry = {"capabilities": caps}
        return ev.judge(registry, results, list(allowlist),
                        live or {"by_id": {}, "errors": []}, selected, check_unowned, self.repo)

    def verdict(self, suites, allowlist=(), recorder=True, ident="tool:alpha"):
        results = self.run_suites(suites, recorder=recorder)
        out = self.judge([cap(ident, "tools/alpha/tests")], results, allowlist)
        return out["capabilities"][ident], results, out

    def assertFailedNaming(self, state, *needles):
        self.assertEqual(state["state"], "failed", state)
        text = " | ".join(state["reasons"])
        for needle in needles:
            self.assertIn(needle, text)


class StatesTest(EvidenceBase):
    def test_positive_control_evidenced(self):
        state, results, _out = self.verdict({SUITE: PASSING})
        self.assertEqual(state, {"state": "evidenced", "reasons": [], "notes": []})
        self.assertEqual(results[0]["rc"], 0)
        self.assertEqual([t["outcome"] for t in results[0]["record"]], ["pass"])

    def test_p1_zero_tests(self):
        state, _r, _o = self.verdict({SUITE: '''
            import unittest
            class T(unittest.TestCase):
                pass
            if __name__ == "__main__":
                unittest.main()
        '''})
        self.assertFailedNaming(state, "no test passed")

    def test_p2_all_skipped_even_when_listed(self):
        state, _r, _o = self.verdict({SUITE: '''
            import unittest
            class T(unittest.TestCase):
                def test_one(self):
                    self.skipTest("one")
                def test_two(self):
                    self.skipTest("two")
            if __name__ == "__main__":
                unittest.main()
        '''}, allowlist=[skip_entry("T.test_one"), skip_entry("T.test_two")])
        self.assertFailedNaming(state, "no test passed")

    def test_p3_unlisted_skip_names_the_test(self):
        state, _r, _o = self.verdict({SUITE: ONE_SKIP})
        self.assertFailedNaming(state, "unlisted skip", SUITE + "::T.test_skipme",
                                "no such thing here")

    def test_p4_listed_skip_is_evidenced(self):
        state, _r, _o = self.verdict({SUITE: ONE_SKIP},
                                     allowlist=[skip_entry("T.test_skipme", VERSION)])
        self.assertEqual(state, {"state": "evidenced", "reasons": [], "notes": []})

    def test_p4_any_entry_matches(self):
        state, _r, _o = self.verdict({SUITE: ONE_SKIP},
                                     allowlist=[skip_entry("T.test_skipme", "any")])
        self.assertEqual(state["state"], "evidenced")

    def test_p5_entry_for_the_other_interpreter_does_not_cover(self):
        state, _r, _o = self.verdict({SUITE: ONE_SKIP},
                                     allowlist=[skip_entry("T.test_skipme", OTHER)])
        self.assertFailedNaming(state, "unlisted skip", "python " + VERSION)

    def test_p6_stale_entry(self):
        state, _r, out = self.verdict({SUITE: PASSING},
                                      allowlist=[skip_entry("T.test_gone")])
        self.assertFailedNaming(state, "stale skip entry", "T.test_gone")
        self.assertEqual(len(out["stale"]), 1)

    def test_p7_failing_test(self):
        state, results, _o = self.verdict({SUITE: '''
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    self.assertTrue(True)
                def test_broken(self):
                    self.assertEqual(1, 2)
            if __name__ == "__main__":
                unittest.main()
        '''})
        self.assertNotEqual(results[0]["rc"], 0)
        self.assertFailedNaming(state, SUITE + "::T.test_broken", "test failed", "exited")

    def test_errored_test_and_unexpected_success(self):
        state, _r, _o = self.verdict({SUITE: '''
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    self.assertTrue(True)
                def test_error(self):
                    raise RuntimeError("boom")
                @unittest.expectedFailure
                def test_xpass(self):
                    self.assertTrue(True)
            if __name__ == "__main__":
                unittest.main()
        '''})
        self.assertFailedNaming(state, "T.test_error", "test errored", "T.test_xpass",
                                "unexpected success")

    def test_failing_subtest_is_a_failure(self):
        state, _r, _o = self.verdict({SUITE: '''
            import unittest
            class T(unittest.TestCase):
                def test_ok(self):
                    self.assertTrue(True)
                def test_sub(self):
                    with self.subTest("x"):
                        self.fail("sub")
            if __name__ == "__main__":
                unittest.main()
        '''})
        self.assertFailedNaming(state, "T.test_sub", "test failed")

    def test_p8_recorder_absent_with_exit_zero(self):
        state, results, _o = self.verdict({SUITE: PASSING}, recorder=False)
        self.assertEqual(results[0]["rc"], 0)
        self.assertIsNone(results[0]["record"])
        self.assertFailedNaming(state, "recorder did not load", SUITE)

    def test_timeout_is_a_failure(self):
        results = []
        self.write(SUITE, source('''
            import time
            time.sleep(60)
        '''))
        results.append(ev.run_suite(self.repo, SUITE, sys.executable, VERSION, self.root,
                                    timeout=1))
        self.assertTrue(results[0]["timed_out"])
        out = self.judge([cap("tool:alpha", "tools/alpha/tests")], results)
        self.assertFailedNaming(out["capabilities"]["tool:alpha"], "timed out", SUITE)

    def test_suite_missing_for_a_version_fails(self):
        self.write(SUITE, source(PASSING))
        out = self.judge([cap("tool:alpha", "tools/alpha/tests")],
                         [{"suite": SUITE, "version": "3.9", "rc": 0, "timed_out": False,
                           "record": [{"id": "__main__.T.test_a", "outcome": "pass",
                                       "reason": ""}], "log": ""},
                          {"suite": "tools/other/tests/test_o.py", "version": "3.13", "rc": 0,
                           "timed_out": False, "record": [], "log": ""}])
        self.assertFailedNaming(out["capabilities"]["tool:alpha"],
                                "no result for suite " + SUITE + " on python 3.13")

    def test_capability_without_a_suite_fails(self):
        out = self.judge([cap("tool:alpha", "tools/alpha/tests")], [])
        self.assertFailedNaming(out["capabilities"]["tool:alpha"], "no test passed",
                                "tools/alpha/tests")

    def test_a_real_suite_does_not_cover_a_sibling_that_ran_nothing(self):
        empty = "tools/alpha/tests/test_empty.py"
        state, _r, _o = self.verdict({SUITE: PASSING, empty: '''
            import unittest
            class T(unittest.TestCase):
                pass
            if __name__ == "__main__":
                unittest.main()
        '''})
        self.assertFailedNaming(state, "no test passed in suite " + empty)

    def test_a_sibling_whose_every_test_is_a_listed_skip_is_allowed(self):
        opt_in = "tools/alpha/tests/test_live.py"
        live_suite = '''
            import unittest
            class Live(unittest.TestCase):
                def test_against_the_real_cli(self):
                    self.skipTest("opt-in")
            if __name__ == "__main__":
                unittest.main()
        '''
        entry = skip_entry("Live.test_against_the_real_cli", suite=opt_in)
        state, _r, _o = self.verdict({SUITE: PASSING, opt_in: live_suite}, allowlist=[entry])
        self.assertEqual(state, {"state": "evidenced", "reasons": [], "notes": []})
        # Unlisted, the same sibling is a failure.
        state, _r, _o = self.verdict({SUITE: PASSING, opt_in: live_suite})
        self.assertFailedNaming(state, "unlisted skip " + opt_in, "no test passed in suite " + opt_in)

    def test_class_level_skip_is_keyed_without_main(self):
        opt_in = "tools/alpha/tests/test_live.py"
        state, results, _o = self.verdict({SUITE: PASSING, opt_in: '''
            import unittest
            class Live(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest("opt-in")
                def test_x(self):
                    pass
            if __name__ == "__main__":
                unittest.main()
        '''}, allowlist=[skip_entry("setUpClass (Live)", suite=opt_in)])
        self.assertEqual(state, {"state": "evidenced", "reasons": [], "notes": []}, results)

    def test_declared_instructions_only_skill_is_judged_on_its_live_record_alone(self):
        ident = "skill:handover"
        self.assertIn(ident, ev.LIVE_ONLY_SKILLS)
        entry = cap(ident, entry_points=["skills/continuity/handover/SKILL.md"])
        self.write("skills/continuity/handover/SKILL.md", "# handover\n")
        out = self.judge([entry], [])
        self.assertEqual(out["capabilities"][ident]["state"], "awaiting-live")
        good = self.judge([entry], [], live=good_live(self.repo, entry, [entry]))
        self.assertEqual(good["capabilities"][ident],
                         {"state": "evidenced", "reasons": [], "notes": []})

    def test_any_other_skill_with_no_suite_fails_even_with_a_good_live_record(self):
        # Removing a tested skill's `tests` from the registry must not move it onto
        # its live record alone.
        ident = "skill:build"
        self.assertNotIn(ident, ev.LIVE_ONLY_SKILLS)
        entry = cap(ident, entry_points=["skills/builder/build/SKILL.md"])
        self.write("skills/builder/build/SKILL.md", "# build\n")
        out = self.judge([entry], [], live=good_live(self.repo, entry, [entry]))
        self.assertFailedNaming(out["capabilities"][ident], "capability has no suite")

    def test_live_only_set_is_exactly_the_registry_skills_without_a_suite(self):
        with open(os.path.join(REPO, "docs", "capabilities.json"), encoding="utf-8") as handle:
            registry = json.load(handle)
        untested = {e["id"] for e in registry["capabilities"]
                    if e["kind"] == "skill" and not e["tests"]}
        self.assertEqual(untested, set(ev.LIVE_ONLY_SKILLS))

    def test_judge_fails_unowned_suites_only_when_asked(self):
        # Structural mode reports them too (check_unowned_suites, CliStructuralTest).
        self.write("tools/alpha/tests/test_alpha.py", source(PASSING))
        self.write("lib/tests/test_orphan.py", source(PASSING))
        caps = [cap("tool:alpha", "tools/alpha/tests")]
        results = [ev.run_suite(self.repo, SUITE, sys.executable, VERSION, self.root)]
        release = self.judge(caps, results, check_unowned=True)
        self.assertEqual(release["unowned"], ["lib/tests/test_orphan.py"])
        self.assertIn("lib/tests/test_orphan.py", " ".join(release["global"]))
        partial = self.judge(caps, results, check_unowned=False)
        self.assertEqual(partial["global"], [])
        self.assertEqual(partial["unowned"], ["lib/tests/test_orphan.py"])


class RunnerTest(EvidenceBase):
    def test_p9_stdin_is_dev_null(self):
        # Our own stdin is an open pipe for the duration, so a suite that inherited
        # it would block on read() and time out; a DEVNULL stdin returns at once.
        # (Whatever runs this file usually gives it a closed stdin, which would
        # hide an inherited one.)
        self.write(SUITE, source('''
            import sys
            import unittest
            class T(unittest.TestCase):
                def test_stdin(self):
                    self.assertEqual(sys.stdin.read(), "")
            if __name__ == "__main__":
                unittest.main()
        '''))
        read_end, write_end = os.pipe()
        saved = os.dup(0)
        os.dup2(read_end, 0)
        try:
            results = [ev.run_suite(self.repo, SUITE, sys.executable, VERSION, self.root,
                                    timeout=15)]
        finally:
            os.dup2(saved, 0)
            for fd in (saved, read_end, write_end):
                os.close(fd)
        state = self.judge([cap("tool:alpha", "tools/alpha/tests")], results)["capabilities"]["tool:alpha"]
        self.assertFalse(results[0]["timed_out"])
        self.assertEqual(results[0]["rc"], 0)
        self.assertIsNotNone(results[0]["record"])
        self.assertEqual(state["state"], "evidenced")

    def test_records_the_launched_process_not_its_child(self):
        # A child python inherits the recorder env and writes its own record file,
        # here with a failing test. Only the launched pid's record may be judged.
        state, results, _o = self.verdict({SUITE: '''
            import os
            import subprocess
            import sys
            import unittest
            CHILD = (
                "import unittest\\n"
                "class C(unittest.TestCase):\\n"
                "    def test_child_bad(self):\\n"
                "        self.fail('child')\\n"
                "unittest.main(argv=['x'], exit=False)\\n"
            )
            class T(unittest.TestCase):
                def test_parent(self):
                    subprocess.run([sys.executable, "-c", CHILD], stdin=subprocess.DEVNULL,
                                   capture_output=True)
                    mine = "%d-" % os.getpid()
                    others = [n for n in os.listdir(os.environ["CAPABILITY_EVIDENCE_DIR"])
                              if not n.startswith(mine)]
                    self.assertTrue(others, "child wrote no record, control is vacuous")
            if __name__ == "__main__":
                unittest.main()
        '''})
        self.assertEqual(state, {"state": "evidenced", "reasons": [], "notes": []})
        self.assertEqual([t["id"] for t in results[0]["record"]], ["__main__.T.test_parent"])

    def test_outermost_run_wins_over_an_inner_in_process_runner(self):
        state, results, _o = self.verdict({SUITE: '''
            import io
            import unittest
            class T(unittest.TestCase):
                def test_inner(self):
                    class Inner(unittest.TestCase):
                        def test_inner_bad(self):
                            self.fail("inner")
                    suite = unittest.defaultTestLoader.loadTestsFromTestCase(Inner)
                    result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
                    self.assertFalse(result.wasSuccessful())
                def test_outer(self):
                    self.assertTrue(True)
            if __name__ == "__main__":
                unittest.main()
        '''})
        ids = sorted(t["id"] for t in results[0]["record"])
        self.assertEqual(ids, ["__main__.T.test_inner", "__main__.T.test_outer"])
        self.assertEqual(state, {"state": "evidenced", "reasons": [], "notes": []})

    def test_refuses_root_under_tmpdir_or_tmp(self):
        self.write(SUITE, source(PASSING))
        for root in (os.path.join(tempfile.gettempdir(), "capability-evidence-refused"),
                     "/tmp/capability-evidence-refused",
                     "/private/tmp/capability-evidence-refused"):
            with self.assertRaises(ValueError, msg=root):
                ev.run_suite(self.repo, SUITE, sys.executable, VERSION, root)
            self.assertFalse(os.path.exists(root))

    def test_suite_runs_in_its_component_with_own_home_and_python3_shim(self):
        state, _r, _o = self.verdict({SUITE: '''
            import os
            import shutil
            import sys
            import unittest
            class T(unittest.TestCase):
                def test_env(self):
                    self.assertEqual(os.path.realpath(os.getcwd()),
                                     os.path.realpath(os.path.dirname(os.path.dirname(
                                         os.path.abspath(__file__)))))
                    self.assertIn("/home", os.environ["HOME"])
                    self.assertNotIn("GITHUB_TOKEN", os.environ)
                    self.assertEqual(os.path.dirname(shutil.which("python3")),
                                     os.environ["PATH"].split(os.pathsep)[0])
            if __name__ == "__main__":
                unittest.main()
        '''})
        self.assertEqual(state["state"], "evidenced", state)

    def test_suite_and_discovery_helpers(self):
        self.write("tools/a/tests/test_x.py", PASSING)
        self.write("tools/a/tests/helper.py", "")
        self.write("tools/a/tests/sub/test_deep.py", PASSING)
        self.write("lib/tests/test_y.py", PASSING)
        self.write("docs/tests/test_z.py", PASSING)
        self.assertEqual(ev.suites_for(self.repo, "tools/a"), ["tools/a/tests/test_x.py"])
        self.assertEqual(ev.suites_for(self.repo, "tools/a/tests"), ["tools/a/tests/test_x.py"])
        self.assertEqual(ev.suites_for(self.repo, "tools/a/tests/test_x.py"),
                         ["tools/a/tests/test_x.py"])
        self.assertEqual(ev.suites_for(self.repo, "tools/a/tests/helper.py"), [])
        self.assertEqual(ev.suites_for(self.repo, "tools/nope"), [])
        self.assertEqual(ev.all_suites(self.repo),
                         ["lib/tests/test_y.py", "tools/a/tests/test_x.py"])

    def test_allowlist_schema_names_the_bad_entry(self):
        path = self.write("skips.json", json.dumps(
            {"schema": 1, "skips": [skip_entry("T.ok"), {"suite": SUITE, "test": "T.bad"}]}))
        with self.assertRaises(ValueError) as caught:
            ev.load_allowlist(path)
        self.assertIn("T.bad", str(caught.exception))
        good = self.write("good.json", json.dumps({"schema": 1, "skips": [skip_entry("T.ok")]}))
        self.assertEqual(ev.load_allowlist(good), [skip_entry("T.ok")])


# A skill that names a reference, a script, a tool and that tool's helper, requires
# one more tool and one skill; plus files it does not run, at every level.
FIXTURE_SKILL = {
    "skills/core/gamma/SKILL.md":
        "# gamma\nRead references/guide.md, run ${CLAUDE_SKILL_DIR}/scripts/run.py,\n"
        "then `claude-zeta --check` (not claude-code, not a tool).\n",
    "skills/core/gamma/references/guide.md": "guide\n",
    "skills/core/gamma/scripts/run.py": "import json\nimport helper\n",
    "skills/core/gamma/tests/test_gamma.py": "import unittest\n",
    "skills/core/delta/SKILL.md": "# delta\n",
    "lib/helper.py": "TOOLS = ['claude-theta']  # names a tool; does not run it\nX = 1\n",
    "lib/other.py": "Y = 1\n",
    "tools/zeta/claude-zeta": "#!/usr/bin/env python3\nimport zeta_lib\n",
    "tools/zeta/zeta_lib.py": "import omega_lib\nZ = 1\n",
    "tools/omega/omega_lib.py": "W = 1\n",
    "tools/other-dir/zeta_lib.py": "SHADOWED = 1\n",
    "tools/eta/claude-eta": "#!/usr/bin/env python3\n",
    "tools/theta/claude-theta": "#!/usr/bin/env python3\n",
    "plugin-core/.claude-plugin/plugin.json": '{"name": "core"}\n',
    "README.md": "# repo\n",
}


class LiveRecordTest(EvidenceBase):
    SKILL = "skill:gamma"

    def setUp(self):
        super().setUp()
        self.live_dir = os.path.join(self.base, "ledger")
        self.write(SUITE, source(PASSING))
        self.results = [ev.run_suite(self.repo, SUITE, sys.executable, VERSION, self.root)]
        for rel, text in FIXTURE_SKILL.items():
            self.write(rel, text)
        os.symlink(os.path.join("..", "skills", "core"),
                   os.path.join(self.repo, "plugin-core", "skills"))
        self.caps = [cap(self.SKILL, "tools/alpha/tests",
                         entry_points=["skills/core/gamma/SKILL.md"],
                         requires=["tool:eta", "skill:delta"]),
                     cap("tool:eta", entry_points=["tools/eta/claude-eta"]),
                     cap("skill:delta", entry_points=["skills/core/delta/SKILL.md"])]
        self.identity = self.closure_identity()

    def closure_identity(self):
        return ev.skill_identity(self.repo, self.caps[0],
                                 {c["id"]: c for c in self.caps})["closure_sha256"]

    def transcript(self, name, text="transcript body"):
        self.write(name, text, base=self.live_dir)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def live_file(self, cases, ident=None, identity=None):
        record = {"id": ident or self.SKILL, "cases": cases,
                  "identity": {"closure_sha256": identity or self.identity}}
        return self.write("live.json", json.dumps(
            {"schema": 2, "claude_version": "test", "records": [record]}), base=self.live_dir)

    def good_cases(self, outcome="completed"):
        cases = [{"kind": kind, "observed": "pass", "transcript": kind + ".txt",
                  "sha256": self.transcript(kind + ".txt")}
                 for kind in ("positive", "out-of-scope", "reference")]
        cases[0]["outcome"] = outcome
        return cases

    def assertIdentityMismatch(self, after_edit):
        path = self.live_file(self.good_cases())
        after_edit()
        out, _live = self.judged(path)
        self.assertFailedNaming(out["capabilities"][self.SKILL],
                                "implementation identity mismatch", self.identity[:12])

    def judged(self, path, ids=None):
        live = ev.load_live(path, ids or [self.SKILL])
        return self.judge(self.caps, self.results, live=live), live

    def test_p10_skill_without_live_record_awaits(self):
        out = self.judge(self.caps, self.results)
        state = out["capabilities"][self.SKILL]
        self.assertEqual(state["state"], "awaiting-live")
        self.assertIn("live record", " ".join(state["reasons"]))

    def test_skill_with_failing_tests_is_failed_not_awaiting(self):
        self.write(SUITE, source('''
            import unittest
            class T(unittest.TestCase):
                def test_broken(self):
                    self.fail("x")
            if __name__ == "__main__":
                unittest.main()
        '''))
        results = [ev.run_suite(self.repo, SUITE, sys.executable, VERSION, self.root)]
        state = self.judge(self.caps, results)["capabilities"][self.SKILL]
        self.assertFailedNaming(state, "T.test_broken")

    def test_p12_good_live_record_is_evidenced(self):
        out, live = self.judged(self.live_file(self.good_cases()))
        self.assertEqual(live, {"by_id": {self.SKILL: {
            "problems": [], "notes": [], "identity": self.identity}}, "errors": []})
        self.assertEqual(out["capabilities"][self.SKILL], {"state": "evidenced", "reasons": [], "notes": []})
        self.assertEqual(out["global"], [])

    def test_p11_missing_kind(self):
        out, _live = self.judged(self.live_file(self.good_cases()[:2]))
        self.assertFailedNaming(out["capabilities"][self.SKILL], "no reference case")

    def test_p11_observed_fail(self):
        cases = self.good_cases()
        cases[0]["observed"] = "fail"
        out, _live = self.judged(self.live_file(cases))
        self.assertFailedNaming(out["capabilities"][self.SKILL], "positive case observed fail")

    def test_p11_sha_mismatch(self):
        cases = self.good_cases()
        cases[1]["sha256"] = "0" * 64
        out, _live = self.judged(self.live_file(cases))
        self.assertFailedNaming(out["capabilities"][self.SKILL], "out-of-scope",
                                "does not match its sha256")

    def test_p11_missing_transcript(self):
        cases = self.good_cases()
        cases[2]["transcript"] = "gone.txt"
        out, _live = self.judged(self.live_file(cases))
        self.assertFailedNaming(out["capabilities"][self.SKILL], "reference", "gone.txt",
                                "missing")

    def test_p11_unknown_id_fails_the_run(self):
        out, live = self.judged(self.live_file(self.good_cases(), ident="skill:ghost"))
        self.assertTrue(any("skill:ghost" in e for e in live["errors"]))
        self.assertTrue(any("skill:ghost" in g for g in out["global"]))
        self.assertEqual(out["capabilities"][self.SKILL]["state"], "awaiting-live")

    def test_live_record_for_a_non_skill_fails_the_run(self):
        live = {"by_id": {"tool:alpha": {"problems": [], "notes": [], "identity": "0" * 64}},
                "errors": []}
        out = self.judge([cap("tool:alpha", "tools/alpha/tests")], self.results, live=live)
        self.assertTrue(any("tool:alpha" in g and "not a skill" in g for g in out["global"]))

    def test_malformed_live_file_raises(self):
        def record(cases, identity=None):
            return {"id": self.SKILL, "cases": cases,
                    "identity": {"closure_sha256": identity or self.identity}}

        def doc(*records):
            return json.dumps({"schema": 2, "claude_version": "t", "records": list(records)})

        no_outcome = self.good_cases()
        del no_outcome[0]["outcome"]
        bad_outcome = self.good_cases(outcome="skipped")
        no_identity = record(self.good_cases())
        del no_identity["identity"]
        for text in ("not json", json.dumps({"schema": 2, "records": []}),
                     doc(record([{"kind": "bogus"}])),
                     doc(record(no_outcome)), doc(record(bad_outcome)),
                     doc(no_identity), doc(record(self.good_cases(), identity="abc"))):
            path = self.write("bad.json", text, base=self.live_dir)
            with self.assertRaises(ValueError, msg=text):
                ev.load_live(path, [self.SKILL])

    def test_schema_1_is_refused_naming_the_missing_identity(self):
        # Otherwise well-formed, identity included: the schema number alone refuses it.
        path = self.write("old.json", json.dumps(
            {"schema": 1, "claude_version": "t", "records": [
                {"id": self.SKILL, "cases": self.good_cases(),
                 "identity": {"closure_sha256": self.identity}}]}), base=self.live_dir)
        with self.assertRaises(ValueError) as caught:
            ev.load_live(path, [self.SKILL])
        self.assertIn("schema 1 receipts carry no implementation identity",
                      str(caught.exception))

    def test_env_blocked_positive_is_not_a_pass(self):
        out, _live = self.judged(self.live_file(self.good_cases(outcome="env-blocked")))
        self.assertFailedNaming(out["capabilities"][self.SKILL], "positive case",
                                "environment-blocked")

    def test_gate_stop_passes_and_says_what_it_certifies(self):
        out, _live = self.judged(self.live_file(self.good_cases(outcome="gate-stop")))
        self.assertEqual(out["capabilities"][self.SKILL],
                         {"state": "evidenced", "reasons": [], "notes": [ev.GATE_STOP_NOTE]})
        self.assertIn("certifies stopping only", ev.GATE_STOP_NOTE)

    def test_wrong_identity_is_rejected(self):
        out, _live = self.judged(self.live_file(self.good_cases(), identity="0" * 64))
        self.assertFailedNaming(out["capabilities"][self.SKILL],
                                "implementation identity mismatch", "receipt 000000000000")

    def test_identity_follows_what_the_skill_runs(self):
        edits = {
            "SKILL.md": "skills/core/gamma/SKILL.md",
            "named reference": "skills/core/gamma/references/guide.md",
            "skill script": "skills/core/gamma/scripts/run.py",
            "helper the script imports": "lib/helper.py",
            "tool named in the text": "tools/zeta/claude-zeta",
            "helper that tool imports": "tools/zeta/zeta_lib.py",
            "another tool's module it imports": "tools/omega/omega_lib.py",
            "required tool": "tools/eta/claude-eta",
            "plugin manifest": "plugin-core/.claude-plugin/plugin.json",
        }
        files = ev.skill_identity(self.repo, self.caps[0],
                                  {c["id"]: c for c in self.caps})["files"]
        for label, rel in edits.items():
            with self.subTest(label):
                self.assertIn(rel, files)
                # A receipt recorded on the tree as it is now, then the edit: the
                # judge, not just the digest, must reject it.
                self.identity = self.closure_identity()
                path = self.live_file(self.good_cases())

                def edit():
                    with open(os.path.join(self.repo, rel), "a", encoding="utf-8") as handle:
                        handle.write("\n# changed\n")
                edit()
                out, _live = self.judged(path)
                self.assertFailedNaming(out["capabilities"][self.SKILL],
                                        "implementation identity mismatch",
                                        self.identity[:12])

    def test_entry_point_spelled_through_the_plugin_symlink(self):
        via_plugin = dict(self.caps[0], entry_points=["plugin-core/skills/gamma/SKILL.md"])
        caps = {c["id"]: c for c in self.caps}
        self.assertEqual(ev.skill_identity(self.repo, via_plugin, caps),
                         ev.skill_identity(self.repo, self.caps[0], caps))
        self.assertIn("tools/zeta/claude-zeta",
                      ev.skill_identity(self.repo, via_plugin, caps)["files"])

    def test_non_cache_bytecode_in_the_skill_folder_counts(self):
        self.write("skills/core/gamma/scripts/legacy.pyc", "old")
        before = self.closure_identity()
        self.write("skills/core/gamma/scripts/legacy.pyc", "new")
        self.assertNotEqual(self.closure_identity(), before)

    def test_identity_ignores_what_the_skill_does_not_run(self):
        unrelated = ["README.md", "skills/core/gamma/tests/test_gamma.py", "lib/other.py",
                     "tools/theta/claude-theta", "skills/core/delta/SKILL.md",
                     "tools/other-dir/zeta_lib.py"]
        files = ev.skill_identity(self.repo, self.caps[0],
                                  {c["id"]: c for c in self.caps})["files"]
        for rel in unrelated:
            with self.subTest(rel):
                self.assertNotIn(rel, files)
                before = self.closure_identity()
                with open(os.path.join(self.repo, rel), "a", encoding="utf-8") as handle:
                    handle.write("\n# changed\n")
                self.assertEqual(self.closure_identity(), before, rel)
        out, _live = self.judged(self.live_file(self.good_cases()))
        self.assertEqual(out["capabilities"][self.SKILL]["state"], "evidenced")

    def test_an_edit_after_the_live_run_fails_the_receipt(self):
        def edit():
            with open(os.path.join(self.repo, "tools/zeta/zeta_lib.py"), "a") as handle:
                handle.write("X = 2\n")
        self.assertIdentityMismatch(edit)

    def test_missing_entry_point_is_a_reason_not_a_crash(self):
        self.caps[0]["entry_points"] = ["skills/core/gone/SKILL.md"]
        out, _live = self.judged(self.live_file(self.good_cases()))
        self.assertFailedNaming(out["capabilities"][self.SKILL],
                                "cannot compute implementation identity", "gone")


# ---- CLI controls: scripts/check-capabilities.py on a disposable copy of this repo ----

CLI_PATH = os.path.join(REPO, "scripts", "check-capabilities.py")
_cli_spec = importlib.util.spec_from_file_location("check_capabilities_cli", CLI_PATH)
cli = importlib.util.module_from_spec(_cli_spec)
_cli_spec.loader.exec_module(cli)


def git(repo, *args):
    result = subprocess.run(["git", "-C", repo] + list(args), stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), result.stderr.strip()))
    return result.stdout


def real_state():
    return git(REPO, "rev-parse", "HEAD").strip(), git(REPO, "status", "--porcelain")


def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    later = time.time() + 2  # stale-bytecode trap: a rewrite must not look unchanged
    os.utime(path, (later, later))


def make_copy(dest):
    """Copy this working tree (uncommitted work included, no .git) and commit it."""
    shutil.copytree(REPO, dest, symlinks=True,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".claude"))
    skips = os.path.join(dest, "docs", "capability-skips.json")
    if not os.path.isfile(skips):
        write_file(skips, '{"schema": 1, "skips": []}\n')
    git(dest, "init", "-q")
    git(dest, "config", "user.name", "Capability Test")
    git(dest, "config", "user.email", "capability-test@example.invalid")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "disposable copy")


def release_environment_missing():
    """What the release preflight in evidence_run() refuses without: Node and both
    interpreters. Plan §F needs them for maintainer release verification only, so
    the cases that get past that preflight skip on a machine without them (an
    ordinary install's suite sweep). A release run has them, or it refuses, so these
    skips never fire there."""
    missing = [] if ev.node_version() else ["node"]
    return missing + ["python" + v for v in cli.VERSIONS if not ev.resolve_interpreter(v)]


RELEASE_MISSING = release_environment_missing()
needs_release_environment = unittest.skipIf(
    RELEASE_MISSING, "release verification only; missing %s" % ", ".join(RELEASE_MISSING))


def run_cli(*argv):
    """main() in-process; returns (exit code, stdout)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    return code, out.getvalue()


class CliBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = real_state()
        os.makedirs(CACHE, exist_ok=True)
        cls.base = tempfile.mkdtemp(prefix="check-capabilities-cli-%d-" % os.getpid(),
                                    dir=CACHE)
        cls.copy = os.path.join(cls.base, "copy")
        make_copy(cls.copy)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.base, ignore_errors=True)
        after = real_state()
        if after != cls.before:
            raise AssertionError("the real checkout changed: %r -> %r" % (cls.before, after))

    def fresh_copy(self, name):
        dest = os.path.join(self.base, name)
        make_copy(dest)
        self.addCleanup(shutil.rmtree, dest, True)
        return dest


class CliStructuralTest(CliBase):
    def test_structural_mode_is_clean_on_a_copy(self):
        code, out = run_cli("--repo", self.copy)
        self.assertEqual((code, out), (0, ""))

    def test_removed_registry_entry_fails_structurally(self):
        broken = self.fresh_copy("broken")
        path = os.path.join(broken, "docs", "capabilities.json")
        with open(path, encoding="utf-8") as handle:
            registry = json.load(handle)
        registry["capabilities"] = [e for e in registry["capabilities"]
                                    if e["id"] != "tool:git-tidy"]
        write_file(path, json.dumps(registry, indent=2))
        code, out = run_cli("--repo", broken)
        self.assertEqual(code, 1)
        self.assertIn("FAIL structural", out)
        self.assertIn("tool:git-tidy", out)

    def test_unowned_suite_fails_structurally(self):
        orphaned = self.fresh_copy("orphaned")
        write_file(os.path.join(orphaned, "lib", "tests", "test_orphan.py"),
                   "import unittest\n")
        code, out = run_cli("--repo", orphaned)
        self.assertEqual(code, 1)
        self.assertIn("FAIL structural: lib/tests/test_orphan.py is owned by no capability", out)

    def test_private_text_in_the_allowlist_fails_structurally(self):
        leaky = self.fresh_copy("leaky")
        write_file(os.path.join(leaky, "docs", "capability-skips.json"), json.dumps(
            {"schema": 1, "skips": [{"suite": "tools/git-tidy/tests/test_x.py", "test": "T.t",
                                     "python": "any", "reason": "needs /Users/someone/x."}]}))
        code, out = run_cli("--repo", leaky)
        self.assertEqual(code, 1)
        self.assertIn("FAIL structural: docs/capability-skips.json: private-looking text", out)

    def test_all_with_only_is_a_usage_error(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = run_cli("--all", "--only", "tool:git-tidy", "--repo", self.copy)
        self.assertEqual(code, 2)


class CliIdentitiesTest(CliBase):
    def test_identities_on_an_export_without_git(self):
        # The tree only, never a .git (what `git archive` gives the private harness).
        export = os.path.join(self.base, "export")
        shutil.copytree(REPO, export, symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__", ".claude"))
        self.addCleanup(shutil.rmtree, export, True)
        code, out = run_cli("--identities", "--repo", export)
        self.assertEqual(code, 0, out)
        printed = json.loads(out)
        registry = cli.load_capabilities(export).load_registry()
        caps = {c["id"]: c for c in registry["capabilities"]}
        skills = sorted(i for i, c in caps.items() if c["kind"] == "skill")
        self.assertEqual(sorted(printed), skills)
        for ident in skills:
            self.assertEqual(printed[ident], ev.skill_identity(export, caps[ident], caps))
        self.assertIn("tools/context-growth/claude-context-growth",
                      printed["skill:memory-distill"]["files"])

    def test_identities_refuses_with_require_evidence(self):
        code, _out = run_cli("--identities", "--require-evidence", "--repo", self.copy)
        self.assertEqual(code, 2)


class CliRefusalTest(CliBase):
    def test_p13_no_node_refuses(self):
        real = ev.node_version
        ev.node_version = lambda: None
        self.addCleanup(setattr, ev, "node_version", real)
        code, out = run_cli("--require-evidence", "--only", "tool:git-tidy",
                            "--repo", self.copy)
        self.assertEqual(code, 2)
        self.assertIn("check-capabilities: refused: node", out)
        self.assertNotIn("evidenced", out)

    @needs_release_environment
    def test_p14_dirty_tree_refuses(self):
        dirty = self.fresh_copy("dirty")
        write_file(os.path.join(dirty, "untracked.txt"), "x\n")
        code, out = run_cli("--require-evidence", "--only", "tool:git-tidy", "--repo", dirty)
        self.assertEqual(code, 2)
        self.assertIn("refused: the working tree is dirty", out)

    @needs_release_environment
    def test_missing_interpreter_refuses(self):
        real = ev.resolve_interpreter
        ev.resolve_interpreter = lambda v: None if v == "3.13" else real(v)
        self.addCleanup(setattr, ev, "resolve_interpreter", real)
        code, out = run_cli("--require-evidence", "--only", "tool:git-tidy",
                            "--repo", self.copy)
        self.assertEqual(code, 2)
        self.assertIn("refused: python 3.13", out)

    @needs_release_environment
    def test_missing_allowlist_refuses(self):
        bare = self.fresh_copy("noskips")
        git(bare, "rm", "-q", "-f", "docs/capability-skips.json")
        git(bare, "commit", "-q", "-m", "drop allowlist")
        code, out = run_cli("--require-evidence", "--only", "tool:git-tidy", "--repo", bare)
        self.assertEqual(code, 2)
        self.assertIn("capability-skips.json is missing", out)

    @needs_release_environment
    def test_malformed_live_file_refuses(self):
        live = os.path.join(self.base, "live.json")
        write_file(live, "not json")
        code, out = run_cli("--require-evidence", "--only", "tool:git-tidy",
                            "--live-evidence", live, "--repo", self.copy)
        self.assertEqual(code, 2)
        self.assertIn("refused: live evidence file", out)


@needs_release_environment
class CliEndToEndTest(CliBase):
    def test_e2e_git_tidy_is_evidenced_on_both_pythons(self):
        report = os.path.join(self.base, "report.json")
        code, out = run_cli("--require-evidence", "--only", "tool:git-tidy",
                            "--out", report, "--repo", self.copy)
        self.assertEqual(code, 0, out)
        self.assertIn("evidenced tool:git-tidy", out)
        self.assertIn("1 capabilities: 1 evidenced, 0 failed, 0 awaiting-live", out)
        with open(report, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(sorted(data["pythons"]), ["3.13", "3.9"])
        self.assertEqual(data["capabilities"]["tool:git-tidy"]["state"], "evidenced")
        self.assertTrue(data["suites"])
        self.assertTrue(all(s["passed"] > 0 and s["rc"] == 0 for s in data["suites"]))

    def test_p15_suite_that_dirties_the_repo_fails_the_run(self):
        mutant = self.fresh_copy("dirtier")
        write_file(os.path.join(mutant, "tools", "git-tidy", "tests", "test_dirties.py"),
                   source('''
                       import os
                       import unittest
                       ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
                           os.path.dirname(os.path.realpath(__file__)))))
                       class T(unittest.TestCase):
                           def test_writes_into_the_repo_root(self):
                               with open(os.path.join(ROOT, "stray.txt"), "w") as handle:
                                   handle.write("x")
                       if __name__ == "__main__":
                           unittest.main()
                   '''))
        git(mutant, "add", "-A")
        git(mutant, "commit", "-q", "-m", "add a suite that writes into the repo")
        code, out = run_cli("--require-evidence", "--only", "tool:git-tidy", "--repo", mutant)
        self.assertEqual(code, 1, out)
        self.assertIn("changed the repository", out)
        self.assertTrue(os.path.isfile(os.path.join(mutant, "stray.txt")))


@needs_release_environment
class CliReleaseRunTest(unittest.TestCase):
    """A full (--all) release run over a synthetic repo, fast enough to run here.

    The real-repo copies above can only afford --only runs, which switch the
    unowned check off; these pin what only --all does. The structural pass is
    replaced (a synthetic repo has no install.sh), so these say nothing about it.
    """

    def setUp(self):
        os.makedirs(CACHE, exist_ok=True)
        self.repo = tempfile.mkdtemp(prefix="check-capabilities-all-%d-" % os.getpid(), dir=CACHE)
        self.addCleanup(shutil.rmtree, self.repo, True)
        write_file(os.path.join(self.repo, SUITE), source(PASSING))
        write_file(os.path.join(self.repo, "docs", "capability-skips.json"),
                   '{"schema": 1, "skips": []}\n')
        self.registry = {"capabilities": [cap("tool:alpha", "tools/alpha/tests")]}
        self.structural = []
        real = cli.structural_problems
        cli.structural_problems = lambda repo: (list(self.structural), self.registry)
        self.addCleanup(setattr, cli, "structural_problems", real)

    def commit_and_run(self):
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Capability Test")
        git(self.repo, "config", "user.email", "capability-test@example.invalid")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "synthetic")
        return run_cli("--require-evidence", "--repo", self.repo)

    def test_positive_control_full_run_passes(self):
        code, out = self.commit_and_run()
        self.assertEqual(code, 0, out)
        self.assertIn("evidenced tool:alpha", out)

    def test_unowned_suite_fails_a_full_run(self):
        write_file(os.path.join(self.repo, "lib", "tests", "test_orphan.py"), source(PASSING))
        code, out = self.commit_and_run()
        self.assertEqual(code, 1, out)
        self.assertIn("owned by no capability: lib/tests/test_orphan.py", out)

    def test_report_keeps_the_log_tail_of_a_red_suite(self):
        write_file(os.path.join(self.repo, SUITE), source('''
            import unittest
            class T(unittest.TestCase):
                def test_broken(self):
                    self.assertEqual(1, 2, "DISTINCT_FAILURE_MARKER")
            if __name__ == "__main__":
                unittest.main()
        '''))
        report = os.path.join(CACHE, "check-capabilities-report-%d.json" % os.getpid())
        self.addCleanup(lambda: os.path.exists(report) and os.remove(report))
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Capability Test")
        git(self.repo, "config", "user.email", "capability-test@example.invalid")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "synthetic")
        code, out = run_cli("--require-evidence", "--repo", self.repo, "--out", report)
        self.assertEqual(code, 1, out)
        with open(report, encoding="utf-8") as handle:
            suites = json.load(handle)["suites"]
        self.assertEqual(len(suites), 2)  # one suite, two interpreters
        for entry in suites:
            self.assertIn("DISTINCT_FAILURE_MARKER", "\n".join(entry["log_tail"]))

    def test_structural_problem_fails_a_release_run_whose_evidence_passes(self):
        self.structural = ["synthetic structural problem"]
        code, out = self.commit_and_run()
        self.assertEqual(code, 1, out)
        self.assertIn("evidenced tool:alpha", out)
        self.assertIn("FAIL structural: synthetic structural problem", out)


if __name__ == "__main__":
    unittest.main()
