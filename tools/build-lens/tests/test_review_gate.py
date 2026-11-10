#!/usr/bin/env python3
"""`claude-review-gate`: the Agent runtime for /build's review gates.

Where the `Workflow` tool is absent (Pro plans default it off; settings,
environment variables, a server flag or an org policy can remove it), a gate
dispatches one Agent instead of `blinded-review.js`. The Agent path has to be
the same review, so this suite holds the helper to two things:

1. **The same prompt.** `claude-review-gate prompt` must print exactly the
   prompt `blinded-review.js` builds for the same arguments, compared against
   the script itself running under Node, and refuse every argument set the
   script refuses.
2. **The same refusal.** `claude-review-gate check` must refuse every report
   the Workflow path refuses (`tests/semantic_cases.json`, the one case file
   both runtimes are held to) plus what the runtime's schema forcing would
   have prevented, and exit 2 rather than a traceback on a report that is not
   there to read.

Run: python3 tests/test_review_gate.py   (from tools/build-lens/)
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
LENS = HERE.parent
sys.path.insert(0, str(LENS))
sys.path.insert(0, str(HERE))

import review_lib  # noqa: E402
from test_build_lens import run_blinded_review  # noqa: E402

CLI = LENS / "claude-review-gate"
CASES = HERE / "semantic_cases.json"
CLEAN_REPORT = {
    "recommendation": "APPROVE",
    "executed": {"ran": True, "commands": ["python3 tests/test_x.py"],
                 "why_not": ""},
    "findings": [],
}


def run_cli(*args, cli=CLI):
    return subprocess.run(
        [sys.executable, str(cli), *args], capture_output=True, text=True,
        stdin=subprocess.DEVNULL)


class _TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="review-gate-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def write_report(self, value, name="report.json"):
        path = self.tmp / name
        if isinstance(value, bytes):
            path.write_bytes(value)
        elif isinstance(value, str):
            path.write_text(value, encoding="utf-8")
        else:
            path.write_text(json.dumps(value), encoding="utf-8")
        return path


class TestPromptParity(_TempDir):
    """The Agent prompt is the Workflow prompt, byte for byte."""

    GATE_ARGS = (
        ("design", {}),
        ("plan", {}),
        ("code", {"planPath": "/work/plan.md"}),
        ("merge", {}),
    )

    def _js_prompt(self, gate, extra):
        args = json.dumps({"gate": gate, "briefPath": "/work/brief.md",
                           "lensDir": "/plugin/tools/build-lens", **extra})
        result = run_blinded_review(self, args)
        self.assertNotIn("error", result, result)
        self.assertEqual(len(result["calls"]), 1, result)
        return result["calls"][0]["prompt"]

    def _cli(self, gate, extra, report):
        argv = ["prompt", "--gate", gate, "--brief", "/work/brief.md",
                "--lens-dir", "/plugin/tools/build-lens",
                "--report", str(report)]
        if "planPath" in extra:
            argv += ["--plan", extra["planPath"]]
        return run_cli(*argv)

    def test_every_gate_prints_the_workflow_prompt(self):
        for gate, extra in self.GATE_ARGS:
            with self.subTest(gate=gate):
                report = self.tmp / ("%s-report-1.json" % gate)
                proc = self._cli(gate, extra, report)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                prompt, sep, clause = proc.stdout.partition("\n\n")
                self.assertEqual(sep, "\n\n", proc.stdout)
                self.assertEqual(prompt, self._js_prompt(gate, extra))
                self.assertIn(str(report), clause)
                self.assertIn("merge-review-report.schema.json", clause)

    def test_the_library_prompt_is_the_cli_prompt(self):
        report = self.tmp / "plan-report-1.json"
        proc = self._cli("plan", {}, report)
        self.assertEqual(
            proc.stdout.partition("\n\n")[0],
            review_lib.build_prompt("plan", "/work/brief.md",
                                    "/plugin/tools/build-lens"))

    def test_the_agent_types_match_the_workflow_script(self):
        for gate, extra in self.GATE_ARGS:
            with self.subTest(gate=gate):
                args = json.dumps({"gate": gate, "briefPath": "/work/b.md",
                                   "lensDir": "/l", **extra})
                opts = run_blinded_review(self, args)["calls"][0]["opts"]
                self.assertEqual(review_lib.AGENT_TYPES[gate],
                                 opts.get("agentType"))


class TestPromptRefusals(_TempDir):
    """Every argument set the Workflow script refuses, refused here too:
    exit 2, nothing on stdout, so no prompt can be dispatched."""

    def _refused(self, *argv):
        proc = run_cli("prompt", *argv)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertTrue(proc.stderr.strip())
        return proc

    def _base(self, **over):
        values = {"--gate": "plan", "--brief": "/work/b.md",
                  "--lens-dir": "/lens",
                  "--report": str(self.tmp / "r.json")}
        values.update(over)
        argv = []
        for key, value in values.items():
            if value is not None:
                argv += [key, value]
        return argv

    def test_an_unknown_gate(self):
        proc = self._refused(*self._base(**{"--gate": "review"}))
        self.assertIn("gate must be one of", proc.stderr)

    def test_a_missing_gate(self):
        self._refused(*self._base(**{"--gate": None}))

    def test_a_relative_or_empty_brief(self):
        for bad in ("b.md", ""):
            with self.subTest(brief=bad):
                proc = self._refused(*self._base(**{"--brief": bad}))
                self.assertIn("briefPath must be an absolute path", proc.stderr)

    def test_a_backslash_brief(self):
        self._refused(*self._base(**{"--brief": r"C:\x\b.md"}))

    def test_a_drive_letter_brief_is_accepted(self):
        proc = run_cli("prompt", *self._base(**{"--brief": "C:/Users/x/b.md"}))
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_a_missing_lens_dir(self):
        self._refused(*self._base(**{"--lens-dir": None}))

    def test_a_relative_or_empty_lens_dir(self):
        for bad in ("build-lens", "", "~/.claude/build-lens", r"C:\lens"):
            with self.subTest(lens_dir=bad):
                proc = self._refused(*self._base(**{"--lens-dir": bad}))
                self.assertIn("lensDir must be an absolute path", proc.stderr)

    def test_code_without_a_plan(self):
        proc = self._refused(*self._base(**{"--gate": "code"}))
        self.assertIn('gate "code" requires planPath', proc.stderr)

    def test_a_plan_at_a_non_code_gate(self):
        for gate in ("design", "plan", "merge"):
            with self.subTest(gate=gate):
                proc = self._refused(*self._base(**{"--gate": gate}),
                                     "--plan", "/work/plan.md")
                self.assertIn("planPath is accepted only at gate", proc.stderr)

    def test_a_relative_report_path(self):
        self._refused(*self._base(**{"--report": "report.json"}))

    def test_an_existing_report_path_is_refused_and_left_alone(self):
        stale = self.write_report(b"stale bytes \x00\x01", "stale.json")
        before = stale.read_bytes()
        proc = self._refused(*self._base(**{"--report": str(stale)}))
        self.assertIn("exists", proc.stderr)
        self.assertEqual(stale.read_bytes(), before)

    def test_a_fresh_report_path_is_accepted_and_not_created(self):
        """Positive control for the refusal above: the same call on an unused
        path succeeds, and `prompt` itself writes nothing there."""
        fresh = self.tmp / "fresh.json"
        proc = run_cli("prompt", *self._base(**{"--report": str(fresh)}))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(fresh.exists())


class TestCheckExitClasses(_TempDir):
    """2 = nothing readable to judge; 1 = a report this gate cannot accept;
    0 = accepted, with a summary line the build ledger copies."""

    def _check(self, path):
        return run_cli("check", "--report", str(path))

    def test_unreadable_reports_exit_2(self):
        missing = self.tmp / "missing.json"
        directory = self.tmp / "adir"
        directory.mkdir()
        cases = {
            "missing": missing,
            "directory": directory,
            "empty": self.write_report("", "empty.json"),
            "not utf-8": self.write_report(b"\xff\xfe{", "latin.json"),
            "not json": self.write_report("{not json", "bad.json"),
        }
        for name, path in cases.items():
            with self.subTest(case=name):
                proc = self._check(path)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.stdout, "")

    def test_an_extra_key_at_the_root_exits_1(self):
        report = dict(CLEAN_REPORT, verdict="fine")
        proc = self._check(self.write_report(report))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("schema:", proc.stderr)

    def test_an_extra_key_inside_a_finding_exits_1(self):
        report = dict(CLEAN_REPORT, recommendation="REQUEST_CHANGES",
                      findings=[{"where": "a.py:1", "defect": "d",
                                 "consequence": "c", "severity": "IMPORTANT",
                                 "evidence": "", "confidence": "high"}])
        proc = self._check(self.write_report(report))
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("schema:", proc.stderr)

    def test_a_wrong_enum_exits_1(self):
        report = dict(CLEAN_REPORT, recommendation="LGTM")
        proc = self._check(self.write_report(report))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("schema:", proc.stderr)

    def test_a_clean_report_exits_0_with_the_summary_line(self):
        report = dict(CLEAN_REPORT, recommendation="REQUEST_CHANGES",
                      findings=[
                          {"where": "a.py:1", "defect": "d", "consequence": "c",
                           "severity": "BLOCKING", "evidence": "repro"},
                          {"where": "b.py:2", "defect": "d", "consequence": "c",
                           "severity": "IMPORTANT", "evidence": ""},
                          {"where": "c.py:3", "defect": "d", "consequence": "c",
                           "severity": "IMPORTANT", "evidence": ""}])
        proc = self._check(self.write_report(report))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            proc.stdout.strip(),
            "accepted: REQUEST_CHANGES, executed.ran=true, BLOCKING=1, "
            "IMPORTANT=2")


class TestSemanticCases(_TempDir):
    """The one case file both runtimes are held to (the JS copy is held to it
    in test_build_lens.py)."""

    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(CASES.read_text(encoding="utf-8"))

    def test_the_library_rule_matches_every_case(self):
        self.assertEqual(len(self.cases), 13)
        for case in self.cases:
            with self.subTest(name=case["name"]):
                reason = review_lib.semantic_violation(case["report"])
                if case["expect"] == "accept":
                    self.assertIsNone(reason)
                else:
                    self.assertIsNotNone(reason)
                    self.assertIn(case["reason"], reason)

    def test_the_cli_refuses_and_accepts_every_case(self):
        schema = json.loads(review_lib.REPORT_SCHEMA_PATH.read_text())
        for case in self.cases:
            with self.subTest(name=case["name"]):
                path = self.write_report(case["report"],
                                         "%s.json" % case["name"])
                proc = run_cli("check", "--report", str(path))
                if case["expect"] == "accept":
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    continue
                self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
                if review_lib.validate_against_schema(schema, case["report"]):
                    # Structure is checked first, as the full-power wrapper
                    # does: a report missing `executed` never reaches the
                    # semantic rule.
                    self.assertIn("schema:", proc.stderr)
                else:
                    self.assertIn("semantic: ", proc.stderr)
                    self.assertIn(case["reason"], proc.stderr)

    def test_structure_is_checked_before_semantics(self):
        missing = next(c for c in self.cases if c["name"] == "executed_missing")
        proc = run_cli("check", "--report",
                       str(self.write_report(missing["report"])))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("schema:", proc.stderr)
        self.assertNotIn("semantic:", proc.stderr)


class TestRunsThroughASymlink(_TempDir):
    """install.sh links the CLI into ~/.local/bin; its import must resolve
    through the link."""

    def test_check_runs_through_a_symlink(self):
        link = self.tmp / "bin" / "claude-review-gate"
        link.parent.mkdir()
        os.symlink(CLI, link)
        proc = run_cli("check", "--report",
                       str(self.write_report(CLEAN_REPORT)), cli=link)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("accepted: APPROVE"))


if __name__ == "__main__":
    unittest.main()
