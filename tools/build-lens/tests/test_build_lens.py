#!/usr/bin/env python3
"""The review script behind /build's gates is real code in prose.

It lives in tools/build-lens/blinded-review.js, and the gate mechanics that
surround it live in tools/build-lens/{design,plan,code,merge}-review.md.
skills/builder/build/SKILL.md keeps the decisions.

Nothing imports it, nothing lints it, and nothing runs it until a `/build`
spends real tokens discovering that it is broken. That is a bad place to find a
typo in a guard, because the failure mode is not a crash — it is a gate that
silently stops gating.

Two layers here:

1. **Structural checks, always run, no dependencies.** They read the script as
   text and assert the properties that regress silently — chiefly that no agent
   is left without an explicit `model`, which is how work slides back onto Opus
   without anyone deciding to.
2. **Behavioural checks via `run_blinded_review`**, which executes the script
   under Node against a stubbed `agent`. Needs `node`; skipped where it is
   absent, per this repo's rule about unavoidable environment dependencies.

Run: python3 tests/test_build_lens.py   (from tools/build-lens/)
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(REPO / "lib" / "tests"))
BUILD_MD = REPO / "skills" / "builder" / "build" / "SKILL.md"
SCRIPTS_DIR = REPO / "tools" / "build-lens"
DIRECTIVES_BASELINE = (
    REPO / "tools" / "build-lens" / "tests" / "fixtures"
    / "directives-baseline.txt")
# The gate references. The directive survival test reads the skill plus
# whichever of these exist, so a directive may move between them freely but
# may not disappear from all of them.
REFERENCE_FILES = tuple(
    SCRIPTS_DIR / name
    for name in (
        "plan-review.md", "code-review.md", "merge-review.md",
        "design-review.md"))
BLINDED_REVIEW_JS = SCRIPTS_DIR / "blinded-review.js"
MERGE_REVIEW_SCHEMA_JSON = SCRIPTS_DIR / "merge-review-report.schema.json"
CODE_REVIEWER_MD = REPO / "agents" / "code-reviewer.md"

# Evaluate the MERGE_REVIEW_REPORT object literal with the real JS
# engine rather than hand-parsing it in Python -- the literal has comments,
# unquoted keys and single-quoted strings, none of which is JSON. Both
# loud-failure assertions (anchor occurs exactly once; evaluated top-level
# keys are exactly {type, required, additionalProperties, properties}) run
# INSIDE this script, not after it in Python -- a silent mis-slice must not
# be able to produce a partial object that a deep-equality check then passes
# against. `additionalProperties` is at the top level because OpenAI strict
# mode (the optional Codex reviewer's `--output-schema`) needs it at every
# object level, not just `required == properties`.
EXTRACT_MERGE_REVIEW_REPORT_JS = r"""
const fs = require('node:fs');
const src = fs.readFileSync(process.argv[1], 'utf8');
const START = 'const MERGE_REVIEW_REPORT = ';
const occurrences = src.split(START).length - 1;
if (occurrences !== 1) {
  console.error('anchor "' + START + '" occurs ' + occurrences +
    ' times in blinded-review.js -- expected exactly 1');
  process.exit(1);
}
const i = src.indexOf(START);
const rest = src.slice(i + START.length);
const m = rest.match(/^\}/m);
if (!m) {
  console.error('no column-0 "}" found after the anchor -- could not ' +
    'locate the literal\'s closing brace');
  process.exit(1);
}
const literal = rest.slice(0, m.index + 1);
let value;
try {
  value = new Function('return ' + literal)();
} catch (e) {
  console.error('node could not evaluate the MERGE_REVIEW_REPORT literal: ' + e.message);
  process.exit(1);
}
const keys = Object.keys(value).sort();
const expected = ['additionalProperties', 'properties', 'required', 'type'];
if (JSON.stringify(keys) !== JSON.stringify(expected)) {
  console.error('MERGE_REVIEW_REPORT top-level keys were ' + JSON.stringify(keys) +
    ', expected ' + JSON.stringify(expected) +
    ' -- a mis-slice would silently yield a partial object here');
  process.exit(1);
}
process.stdout.write(JSON.stringify(value));
"""


def extract_merge_review_report_constant(case):
    """Evaluate `MERGE_REVIEW_REPORT` (blinded-review.js:7) with Node itself
    and return the plain Python value, via `json.loads` on Node's own
    `JSON.stringify` -- Python never interprets the JS.

    `blinded-review.js` is NOT modified to make this possible: it carries a
    top-level
    `return report`, so it cannot be `import()`ed as an ES
    module even with an `export` added, and adding one anyway would break
    the workflow runtime, which strips only `export const meta` -- see
    `TestBlindedReviewScript.test_script_parses_as_javascript` above.

    Skips (does not fail) when `node` is absent, matching this file's
    other node-dependent checks. A non-zero exit from the script below --
    meaning either loud-failure assertion tripped, or Node could not
    evaluate the literal at all -- raises `AssertionError` rather than
    skipping or falling back to a weaker check.
    """
    if not shutil.which("node"):
        case.skipTest("node not installed; schema drift check skipped")
    proc = subprocess.run(
        ["node", "-e", EXTRACT_MERGE_REVIEW_REPORT_JS, str(BLINDED_REVIEW_JS)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            "could not extract MERGE_REVIEW_REPORT from blinded-review.js:\n"
            + proc.stderr)
    return json.loads(proc.stdout)


def run_blinded_review(case, args_literal, agent_returns_null=False,
                        agent_returns=None):
    """Execute `blinded-review.js` under Node with a stubbed `agent`/`phase`/`log`.

    Built the way the retired fan-out's harness built its runner: strip the
    leading `export const meta` so the source is plain JS, wrap it in an async
    IIFE so its top-level `await` and `return` are legal, and hand it `agent`,
    `phase`, `log` and `args` as parameters -- the same globals the Workflow
    runtime supplies. `blinded-review.js` itself is never modified to make
    this possible.

    `args_literal` is a snippet of JS source assigned to the script's `args`
    binding -- either a JSON object literal (JSON is a legal JS subset) or a
    JS string literal wrapping a JSON-encoded object, for the "args arrives
    pre-serialised" case; the caller decides which.

    The stub `agent(prompt, opts)` records every call (`prompt` and `opts`)
    and returns a well-formed clean report, unless `agent_returns_null` asks
    it to return `null` instead -- simulating a reviewer that returned
    nothing -- or `agent_returns` supplies a specific report dict, JSON-encoded
    verbatim into the stub -- simulating a reviewer that returned a
    structurally valid but semantically empty report (the Merge Review fix
    round's post-dispatch check).

    Returns `{"calls": [...], "report": <value>}` on a clean return, or
    `{"calls": [...], "error": <message>}` if the script threw or its promise
    rejected. Skips (does not fail) when `node` is absent, matching this
    file's other node-dependent checks.
    """
    if not shutil.which("node"):
        case.skipTest("node not installed; blinded-review behaviour skipped")
    src = BLINDED_REVIEW_JS.read_text()
    if agent_returns_null:
        agent_return_js = "return null;"
    elif agent_returns is not None:
        agent_return_js = "return " + json.dumps(agent_returns) + ";"
    else:
        agent_return_js = (
            "return {recommendation: 'APPROVE', "
            "executed: {ran: true, commands: ['stub'], why_not: ''}, findings: []};")
    script = (
        "const src = " + json.dumps(src) + ";\n"
        "const body = 'return (async()=>{' + "
        "src.replace(/^export const meta/m, 'const meta') + '})';\n"
        "let make;\n"
        "try {\n"
        "  make = new Function('agent', 'phase', 'log', 'args', body);\n"
        "} catch (e) {\n"
        "  console.log(JSON.stringify({syntaxError: e.message}));\n"
        "  process.exit(0);\n"
        "}\n"
        "const calls = [];\n"
        "const agent = async (prompt, opts) => {\n"
        "  calls.push({prompt, opts});\n"
        "  " + agent_return_js + "\n"
        "};\n"
        "const phase = () => {};\n"
        "const log = () => {};\n"
        "const args = " + args_literal + ";\n"
        "make(agent, phase, log, args)().then((report) => {\n"
        "  console.log(JSON.stringify({report, calls}));\n"
        "}).catch((e) => {\n"
        "  console.log(JSON.stringify({error: e.message, calls}));\n"
        "});\n"
    )
    proc = subprocess.run(["node", "-e", script], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            "node harness for blinded-review.js crashed:\n"
            + proc.stdout + proc.stderr)
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            "node harness for blinded-review.js printed non-JSON output:\n"
            + proc.stdout + proc.stderr)


def extract_named_script(workflow_name: str) -> str:
    """The workflow script whose `meta.name` matches, read from its own file.

    build.md used to carry more than one workflow embedded as ```javascript
    blocks, and the Plan Review / Code Review script was merely the first.
    Selecting the others by index would have silently re-pointed every
    assertion at a different workflow the moment a block was inserted above
    them, and the symptom would be a green suite guarding the wrong script.

    Each workflow now lives in its own file, and the same danger persists in
    a smaller form — a file could be renamed, or edited to declare a
    different `meta.name`, without anything else in the repo failing loudly.
    So this still resolves the file by name and then asserts the file's own
    `meta.name` actually matches, rather than trusting the filename alone.
    """
    path = SCRIPTS_DIR / "{}.js".format(workflow_name)
    if not path.is_file():
        raise AssertionError(
            "no tools/build-lens/{}.js — the workflow is gone or was "
            "renamed".format(workflow_name))
    src = path.read_text()
    if not re.search(r"name:\s*'" + re.escape(workflow_name) + r"'", src):
        raise AssertionError(
            "tools/build-lens/{}.js does not declare name: '{}' — the "
            "workflow is gone or was renamed".format(workflow_name, workflow_name))
    return src


def assert_parses_as_workflow(case, src, what):
    """Assert `src` is valid JavaScript *as the workflow runtime evaluates it*.

    `node --check` cannot judge a workflow script under either module mode: the
    body uses top-level `await` (which CommonJS rejects) AND top-level `return`
    (which ESM rejects). Both are legal in the runtime, which wraps the body in an
    async function. Wrap it the same way — checking it any other way reports a
    syntax error on a script that runs fine, which is worse than not checking.

    Shared by every workflow in build.md rather than copied per class: a second
    copy of this wrapper would be a second opinion on what "valid" means.
    """
    if not shutil.which("node"):
        case.skipTest("node not installed; structural checks still ran")
    # delete=False, then cleaned up explicitly: the file has to be closed before
    # node can read it on every platform, so the context manager cannot own it.
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(src)
        path = fh.name
    case.addCleanup(os.unlink, path)
    checker = (
        "const fs=require('node:fs');"
        "const src=fs.readFileSync(process.argv[1],'utf8');"
        "try{new Function('agent','parallel','pipeline','log','phase',"
        "'args','workflow','budget',"
        "'return (async()=>{'+src.replace(/^export const meta/m,'const meta')+'})');}"
        "catch(e){console.error(e.message);process.exit(1);}"
    )
    proc = subprocess.run(
        ["node", "-e", checker, path], capture_output=True, text=True)
    case.assertEqual(
        proc.returncode, 0,
        "{} is not valid JavaScript:\n{}".format(what, proc.stderr))


class TestBlindedReviewScript(unittest.TestCase):
    """Merge Review's workflow is prose-embedded code too, and it gates the merge surface.

    Every property a workflow script must hold applies here, plus two properties
    that are specific to Merge Review and that regress without a symptom:

    * **Execution must be a REQUIRED field.** It was an instruction before, and an
      instruction a reviewer can skip quietly is a hope. If `executed` slips out
      of `required`, a reviewer that ran nothing returns a report that reads
      exactly like one that ran the suite.
    * **The reviewer must be reused, not restated.** `agentType: 'toolkit-build:code-reviewer'`
      keeps one source of truth for the reviewer prompt. Inlining a copy is the
      duplication this repo's hard rules ban, and the copy would then drift.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = extract_named_script("blinded-review")
        cls.code = "\n".join(
            ln for ln in cls.src.splitlines() if not ln.strip().startswith("//"))

    def test_script_parses_as_javascript(self):
        assert_parses_as_workflow(self, self.src, "the Merge Review script")

    def test_the_agent_call_pins_a_model(self):
        """An omitted model inherits the session's, silently. Same failure as the
        review script's, and here it is one call site rather than a fan-out, so
        there is no `...opts` route to allow for.

        **The option-line count is asserted first, and that is the whole test.**
        Without it the loop below is vacuous: dropping `label:` in the same edit
        that drops `model:` leaves nothing to iterate, so the test goes green
        while the agent inherits the session model — the exact failure this
        docstring names. That is how it shipped, and a blinded review found it by
        mutating both keys together rather than one at a time. The retired fan-out's
        sibling test always carried this guard; this copy first took the loop
        and left the guard behind.
        """
        options = [ln for ln in self.code.splitlines() if "label:" in ln]
        call_sites = sum(ln.count("agent(") for ln in self.code.splitlines())
        self.assertEqual(
            len(options), call_sites,
            "found {} option lines for {} agent() call sites — a call site with "
            "no options line is a call site this test cannot see".format(
                len(options), call_sites))
        self.assertGreater(call_sites, 0, "Merge Review dispatches no agent at all")
        for line in options:
            self.assertIn(
                "model:", line,
                "Merge Review's agent options name no model, so it would inherit the "
                "session's: {!r}".format(line.strip()[:80]))

    def test_it_reuses_the_code_reviewer_agent_rather_than_restating_it(self):
        self.assertIn(
            "agentType: 'toolkit-build:code-reviewer'", self.code,
            "Merge Review must dispatch the existing code-reviewer agent — a second copy "
            "of its prompt is a second source of truth that will drift")

    def test_execution_is_a_required_field_not_an_instruction(self):
        """Condition (b). 'Nothing was executed' has to be a result the architect
        reads, in the same family as `unread` — a silence and a calm reading must
        never look the same."""
        self.assertRegex(
            self.code, r"required:\s*\[[^\]]*'executed'",
            "the Merge Review schema must REQUIRE `executed`; as an optional field it is "
            "the instruction it was meant to replace")
        self.assertIn(
            "schema: MERGE_REVIEW_REPORT", self.code,
            "the agent call must pass the schema, or nothing forces the field")

    def test_a_reviewer_that_ran_nothing_must_say_why(self):
        """`ran: false` with an empty explanation is the failure this guards.

        A repository with no suite makes `ran: false` routinely correct —
        which is exactly why it must carry a reason. A field that
        reads as expected noise stops being read at all.
        """
        executed = re.search(
            r"executed:\s*\{\s*type:\s*'object',\s*required:\s*\[([^\]]*)\]",
            self.code)
        self.assertIsNotNone(
            executed, "the `executed` object declares no `required` list")
        required = {k.strip().strip("'\"") for k in executed.group(1).split(",") if k.strip()}
        self.assertEqual(
            {"ran", "commands", "why_not"}, required,
            "`executed` must require all three of ran/commands/why_not — "
            "`commands` is what makes the claim checkable and `why_not` is what "
            "stops a bare `false` passing as an answer; got {}".format(sorted(required)))

    def test_findings_carry_the_evidence_the_stop_rule_reads(self):
        """The STOP rule's evidence half is only enforceable if evidence is a
        field. Without it, every BLOCKING sustains another fix round and the
        loop has no bound but patience."""
        self.assertRegex(
            self.code, r"required:\s*\[[^\]]*'evidence'",
            "Merge Review findings must require `evidence`; the STOP rule reads it to "
            "decide whether a BLOCKING sustains another fix round")

    def test_the_brief_is_named_as_the_only_framing(self):
        """Blinding is the dispatcher's job — the agent's own description offers
        to take a PR number, and given one it reads the author's summary and
        defeats the mechanism."""
        self.assertIn("briefPath", self.code,
                      "Merge Review must dispatch against a brief built by script")
        self.assertRegex(
            self.src, r"ONLY framing",
            "the prompt must say the brief is the reviewer's only framing")


class TestBlindedReviewSemanticCasesParity(unittest.TestCase):
    """The JavaScript copy of the semantic rule, run against the same case
    file `review_lib.semantic_violation` is tested against
    (`tests/test_review_gate.py`)."""

    CASES_PATH = REPO / "tools" / "build-lens" / "tests" / "semantic_cases.json"
    EXPECTED_NAMES = {
        "clean_approve", "executed_missing", "ran_false_no_why_not_no_findings",
        "ran_false_honest_why_not_no_findings",
        "ran_false_blank_why_not_with_finding", "ran_false_honest_with_finding",
        "ran_true_no_commands", "ran_true_whitespace_command",
        "ran_true_one_blank_among_real", "approve_with_blocking",
        "approve_with_important_then_blocking", "approve_with_important_only",
        "request_changes_with_blocking",
    }

    @classmethod
    def setUpClass(cls):
        cls.cases = json.loads(cls.CASES_PATH.read_text())

    def test_every_case_matches_its_expectation(self):
        args = TestBlindedReviewBehaviour._args(
            {"gate": "merge", "briefPath": "/tmp/b"})
        for case in self.cases:
            with self.subTest(name=case["name"]):
                result = run_blinded_review(
                    self, args, agent_returns=case["report"])
                if case["expect"] == "accept":
                    self.assertNotIn("error", result, result)
                    self.assertEqual(result["report"], case["report"])
                else:
                    self.assertIn("error", result, result)
                    self.assertIn(case["reason"], result["error"])

    def test_the_case_set_is_exactly_the_agreed_names(self):
        self.assertEqual({c["name"] for c in self.cases}, self.EXPECTED_NAMES)

    def test_the_cases_live_only_in_build_lens(self):
        """One case file for every copy of the rule: a second copy elsewhere
        could drift while each runtime stays green against its own."""
        self.assertTrue(self.CASES_PATH.is_file())
        stray = REPO / "tools" / "codex-review" / "tests" / "semantic_cases.json"
        self.assertFalse(stray.exists(), stray)


class TestBlindedReviewBehaviour(unittest.TestCase):
    """Executes `blinded-review.js`'s actual control flow under Node against a
    stubbed `agent`, so a gate's guard or its prompt content regresses here
    instead of in a real `/build` paying to discover it. One behaviour per
    test, each asserting an OBSERVED outcome --
    call count and prompt content -- not an exit code.
    """

    # Every call carries a valid absolute `lensDir`, so a negative test below
    # still reaches the branch it is named for instead of the lensDir refusal.
    LENS_DIR_FIXTURE = "/lens"

    @classmethod
    def _args(cls, obj):
        return json.dumps({"lensDir": cls.LENS_DIR_FIXTURE, **obj})

    @classmethod
    def _args_as_json_string(cls, obj):
        # The script's own `typeof args === 'string' ? JSON.parse(args) : ...`
        # branch only runs if `args` genuinely arrives as a JS string, so this
        # wraps the encoded JSON a second time -- a JS string literal whose
        # contents are the JSON text, not the object itself.
        return json.dumps(json.dumps({"lensDir": cls.LENS_DIR_FIXTURE, **obj}))

    def test_an_unknown_gate_throws_with_no_agent_calls(self):
        result = run_blinded_review(
            self, self._args({"gate": "review", "briefPath": "/tmp/b"}))
        self.assertIn("error", result, result)
        self.assertEqual(result["calls"], [])

    def test_a_missing_gate_throws_with_no_agent_calls(self):
        result = run_blinded_review(self, self._args({"briefPath": "/tmp/b"}))
        self.assertIn("error", result, result)
        self.assertEqual(result["calls"], [])

    def test_a_relative_brief_path_throws_with_no_agent_calls(self):
        result = run_blinded_review(
            self, self._args({"gate": "plan", "briefPath": "b.md"}))
        self.assertIn("error", result, result)
        self.assertEqual(result["calls"], [])

    def test_an_empty_brief_path_throws_with_no_agent_calls(self):
        result = run_blinded_review(
            self, self._args({"gate": "plan", "briefPath": ""}))
        self.assertIn("error", result, result)
        self.assertEqual(result["calls"], [])

    def test_a_missing_lens_dir_throws_with_no_agent_calls(self):
        for gate, extra in (("design", {}), ("plan", {}),
                            ("code", {"planPath": "/tmp/p"}),
                            ("merge", {})):
            with self.subTest(gate=gate):
                result = run_blinded_review(self, json.dumps(
                    {"gate": gate, "briefPath": "/tmp/b", **extra}))
                self.assertIn("lensDir must be an absolute path",
                              result.get("error", ""), result)
                self.assertEqual(result["calls"], [])

    def test_a_relative_or_empty_lens_dir_throws_with_no_agent_calls(self):
        for bad in ("build-lens", "", "~/.claude/build-lens", r"C:\lens"):
            with self.subTest(lens_dir=bad):
                result = run_blinded_review(self, json.dumps(
                    {"gate": "plan", "briefPath": "/tmp/b", "lensDir": bad}))
                self.assertIn("lensDir must be an absolute path",
                              result.get("error", ""), result)
                self.assertEqual(result["calls"], [])

    def test_the_checklist_names_the_lens_dir_it_was_given(self):
        result = run_blinded_review(self, json.dumps(
            {"gate": "plan", "briefPath": "/tmp/b",
             "lensDir": "/plugin/tools/build-lens"}))
        self.assertIn("/plugin/tools/build-lens/plan-review.md",
                      result["calls"][0]["prompt"])
        self.assertNotIn("$HOME", result["calls"][0]["prompt"])

    def test_a_drive_letter_brief_path_is_accepted(self):
        """Windows paths take a drive letter, not a leading `/` -- a brief
        path shaped like `C:/Users/x/brief.md` (forward slashes, as the
        reviewer's tools and bash both use on Windows) must dispatch, not
        throw before any agent runs."""
        result = run_blinded_review(
            self, self._args({"gate": "plan", "briefPath": "C:/Users/x/brief.md"}))
        self.assertNotIn("error", result, result)
        self.assertGreaterEqual(len(result["calls"]), 1)

    def test_a_backslash_brief_path_throws(self):
        """A backslash path names a real Windows file but stays rejected --
        forward slashes only, same reason as the docstring above."""
        result = run_blinded_review(
            self, self._args({"gate": "plan", "briefPath": r"C:\x\b.md"}))
        self.assertIn("error", result, result)
        self.assertEqual(result["calls"], [])

    def test_code_gate_without_a_plan_path_throws_with_no_agent_calls(self):
        result = run_blinded_review(
            self, self._args({"gate": "code", "briefPath": "/tmp/b"}))
        self.assertIn("error", result, result)
        self.assertEqual(result["calls"], [])

    def test_a_plan_path_at_a_non_code_gate_throws_with_no_agent_calls(self):
        result = run_blinded_review(
            self, self._args(
                {"gate": "plan", "briefPath": "/tmp/b", "planPath": "/tmp/p"}))
        self.assertIn("error", result, result)
        self.assertEqual(result["calls"], [])

    def test_each_gate_dispatches_exactly_one_correctly_pinned_call(self):
        cases = {
            "design": {"gate": "design", "briefPath": "/tmp/b"},
            "plan": {"gate": "plan", "briefPath": "/tmp/b"},
            "code": {"gate": "code", "briefPath": "/tmp/b", "planPath": "/tmp/p"},
            "merge": {"gate": "merge", "briefPath": "/tmp/b"},
        }
        for gate, args in cases.items():
            with self.subTest(gate=gate):
                result = run_blinded_review(self, self._args(args))
                self.assertNotIn("error", result, result)
                calls = result["calls"]
                self.assertEqual(len(calls), 1, calls)
                prompt, opts = calls[0]["prompt"], calls[0]["opts"]
                self.assertEqual(opts.get("model"), "opus")
                self.assertEqual(opts.get("effort"), "max")
                self.assertIn("executed", opts.get("schema", {}).get("required", []))
                if gate in ("code", "merge"):
                    self.assertEqual(opts.get("agentType"), "toolkit-build:code-reviewer")
                else:
                    self.assertNotIn("agentType", opts)
                self.assertIn("/tmp/b", prompt)
                self.assertIn("Never return an empty findings list", prompt)
                self.assertIn("executed.why_not naming the file", prompt)

    def test_the_code_prompt_names_the_plan_as_the_contract(self):
        result = run_blinded_review(
            self, self._args(
                {"gate": "code", "briefPath": "/tmp/b", "planPath": "/tmp/p"}))
        prompt = result["calls"][0]["prompt"]
        self.assertIn("/tmp/p", prompt)
        self.assertIn("the contract", prompt)

    def test_the_merge_prompt_is_blinded_to_the_plan(self):
        result = run_blinded_review(
            self, self._args({"gate": "merge", "briefPath": "/tmp/b"}))
        prompt = result["calls"][0]["prompt"]
        self.assertIn("do not read the plan", prompt)
        self.assertNotIn("planPath", prompt)
        self.assertNotIn("/tmp/p", prompt)

    def test_design_and_plan_checklists_name_their_own_heading_and_file(self):
        expectations = {
            "design": ("design-review.md", "### The lens table"),
            "plan": ("plan-review.md", "### Plan Review lenses"),
        }
        for gate, (filename, heading) in expectations.items():
            with self.subTest(gate=gate):
                result = run_blinded_review(
                    self, self._args({"gate": gate, "briefPath": "/tmp/b"}))
                prompt = result["calls"][0]["prompt"]
                self.assertIn(filename, prompt)
                self.assertIn(heading, prompt)
                self.assertIn("Apply EVERY row", prompt)

    def test_the_code_checklist_names_its_own_heading_and_file(self):
        result = run_blinded_review(
            self, self._args(
                {"gate": "code", "briefPath": "/tmp/b", "planPath": "/tmp/p"}))
        prompt = result["calls"][0]["prompt"]
        self.assertIn("code-review.md", prompt)
        self.assertIn("### Code Review lenses", prompt)

    def test_args_arriving_as_a_json_string_still_dispatches(self):
        result = run_blinded_review(
            self, self._args_as_json_string(
                {"gate": "merge", "briefPath": "/tmp/b"}))
        self.assertNotIn("error", result, result)
        self.assertEqual(len(result["calls"]), 1, result["calls"])

    def test_a_reviewer_that_returns_null_throws(self):
        result = run_blinded_review(
            self, self._args({"gate": "merge", "briefPath": "/tmp/b"}),
            agent_returns_null=True)
        self.assertIn("error", result, result)

    # A structurally valid report can still describe a review that never
    # happened -- the semantic rule (`semantic_violation` in review_lib.py is
    # its Python copy) catches what the `report == null` check above does
    # not.

    ONE_FINDING = {"where": "x", "defect": "y", "consequence": "z",
                   "severity": "BLOCKING", "evidence": ""}

    def test_a_report_missing_executed_throws(self):
        result = run_blinded_review(
            self, self._args({"gate": "merge", "briefPath": "/tmp/b"}),
            agent_returns={"recommendation": "APPROVE"})
        self.assertIn("error", result, result)

    def test_ran_false_with_empty_findings_and_no_why_not_throws(self):
        result = run_blinded_review(
            self, self._args({"gate": "merge", "briefPath": "/tmp/b"}),
            agent_returns={
                "recommendation": "APPROVE",
                "executed": {"ran": False, "commands": [], "why_not": ""},
                "findings": [],
            })
        self.assertIn("error", result, result)

    def test_ran_false_with_a_why_not_but_empty_findings_throws(self):
        result = run_blinded_review(
            self, self._args({"gate": "merge", "briefPath": "/tmp/b"}),
            agent_returns={
                "recommendation": "APPROVE",
                "executed": {"ran": False, "commands": [], "why_not": "no suite"},
                "findings": [],
            })
        self.assertIn("error", result, result)

    def test_ran_false_with_findings_but_a_blank_why_not_throws(self):
        result = run_blinded_review(
            self, self._args({"gate": "merge", "briefPath": "/tmp/b"}),
            agent_returns={
                "recommendation": "APPROVE",
                "executed": {"ran": False, "commands": [], "why_not": "   "},
                "findings": [self.ONE_FINDING],
            })
        self.assertIn("error", result, result)

    def test_ran_true_with_no_commands_throws(self):
        result = run_blinded_review(
            self, self._args({"gate": "merge", "briefPath": "/tmp/b"}),
            agent_returns={
                "recommendation": "APPROVE",
                "executed": {"ran": True, "commands": [], "why_not": ""},
                "findings": [],
            })
        self.assertIn("error", result, result)

    def test_ran_false_with_a_real_why_not_and_findings_returns_the_report(self):
        result = run_blinded_review(
            self, self._args({"gate": "merge", "briefPath": "/tmp/b"}),
            agent_returns={
                "recommendation": "REQUEST_CHANGES",
                "executed": {"ran": False, "commands": [],
                             "why_not": "docs-only diff, no suite"},
                "findings": [self.ONE_FINDING],
            })
        self.assertNotIn("error", result, result)
        self.assertEqual(
            result["report"]["executed"]["why_not"], "docs-only diff, no suite")

    def test_ran_true_with_commands_returns_the_report(self):
        result = run_blinded_review(
            self, self._args({"gate": "merge", "briefPath": "/tmp/b"}),
            agent_returns={
                "recommendation": "APPROVE",
                "executed": {"ran": True, "commands": ["python3 t.py"],
                             "why_not": ""},
                "findings": [],
            })
        self.assertNotIn("error", result, result)


class TestNoTmpLiteralOnTheBuildSurface(unittest.TestCase):
    """Scratch lives inside the repo, never in `/tmp` -- worktrees under
    `<repo>/.claude/worktrees/`, files under the git dir -- so a path built by
    the skill or any `tools/build-lens/` reference does not depend on the
    platform's temporary directory. Scans the surface a reader follows: this
    file's own fixture literals (`briefPath: "/tmp/b"`, above) live one
    directory below `tools/build-lens/` and are deliberately out of scope --
    they are test input, never an instruction anyone follows.
    """

    def test_no_tmp_literal_in_build_or_its_references(self):
        files = [BUILD_MD] + sorted(
            p for p in SCRIPTS_DIR.iterdir()
            if p.is_file() and p.suffix in (".md", ".js"))
        offenders = []
        for path in files:
            for lineno, line in enumerate(path.read_text().splitlines(), start=1):
                if "/tmp/" in line:
                    offenders.append("{}:{}: {}".format(path, lineno, line.strip()))
        self.assertEqual(
            offenders, [],
            "found a /tmp/ literal on the build surface:\n" + "\n".join(offenders))


class TestBlindedReviewChecklistHeadingsExist(unittest.TestCase):
    """`blinded-review.js` names a heading and a file for each gate's
    checklist rather than restating it -- so a renamed heading reddens this
    test instead of silently emptying a checklist. Parses the `checklist:`
    strings out of the script rather than trusting the count by eye, the same
    anti-drift reasoning `extract_named_script` applies to `meta.name`.
    """

    CHECKLIST_PATTERN = re.compile(
        r'"(#{3} [^"]+)"\s+in\s+\$\{LENS_DIR\}/([\w.-]+\.md)')

    @classmethod
    def setUpClass(cls):
        cls.src = BLINDED_REVIEW_JS.read_text()
        cls.pairs = cls.CHECKLIST_PATTERN.findall(cls.src)

    def test_exactly_three_checklist_pairs_are_declared(self):
        self.assertEqual(
            len(self.pairs), 3,
            "expected exactly 3 gates to name a checklist heading+file pair, "
            "found {}: {}".format(len(self.pairs), self.pairs))

    def test_every_named_heading_exists_as_a_whole_line_in_its_named_file(self):
        for heading, filename in self.pairs:
            with self.subTest(heading=heading, filename=filename):
                path = SCRIPTS_DIR / filename
                self.assertTrue(
                    path.is_file(),
                    "{} names {}, which does not exist".format(
                        BLINDED_REVIEW_JS.name, filename))
                lines = {ln.strip() for ln in path.read_text().splitlines()}
                self.assertIn(
                    heading, lines,
                    "{!r} does not occur as a whole line in {}".format(
                        heading, filename))


class TestMergeReviewReportSchemaDrift(unittest.TestCase):
    """`tools/build-lens/merge-review-report.schema.json` IS a second copy of
    `MERGE_REVIEW_REPORT` -- say so plainly. `blinded-review.js` cannot read
    the JSON (exporting the constant would break the workflow runtime), and
    `claude-review-gate check` validates against the JSON, so this
    deep-equality test is the only thing holding the two runtimes' schemas
    together. A hand-maintained parallel list drifts unnoticed; a by-value
    comparison rules that out.
    """

    def test_schema_file_parses_as_a_json_schema_object(self):
        with open(MERGE_REVIEW_SCHEMA_JSON, encoding="utf-8") as handle:
            schema = json.load(handle)
        self.assertIsInstance(schema, dict)
        self.assertEqual(schema.get("type"), "object")
        self.assertIsInstance(schema.get("required"), list)
        self.assertIsInstance(schema.get("properties"), dict)

    def test_schema_file_matches_the_js_constant_by_deep_equality(self):
        """Deep equality, not a field-name/enum spot check -- deep equality
        subsumes a field-names/enums/descriptions list and cannot drift past
        it the way a spot check could."""
        js_value = extract_merge_review_report_constant(self)
        with open(MERGE_REVIEW_SCHEMA_JSON, encoding="utf-8") as handle:
            json_value = json.load(handle)
        self.assertEqual(json_value, js_value)


class TestMergeReviewReportSchemaStrictMode(unittest.TestCase):
    """OpenAI's structured-output strict mode rejects any schema with a
    property missing from `required`, at ANY level, with HTTP 400
    `invalid_json_schema`, before any model tokens are spent. `MERGE_REVIEW_REPORT` satisfies this today by luck, not by test;
    this is the test.

    `required == properties` alone is not enough: strict mode ALSO requires
    `additionalProperties: false` on every object schema, and without it a
    structured-output run can fall back to prose with exit 0 and no error
    surfaced. This is the guard that detects its removal.
    """

    def _assert_required_equals_properties_everywhere(self, node, path="<root>"):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                required = set(node.get("required") or [])
                properties = set(node["properties"].keys())
                self.assertEqual(
                    required, properties,
                    "at {}: required={} properties={} -- OpenAI strict mode "
                    "rejects any mismatch with HTTP 400 invalid_json_schema".format(
                        path, sorted(required), sorted(properties)))
                for key, subschema in node["properties"].items():
                    self._assert_required_equals_properties_everywhere(
                        subschema, "{}.{}".format(path, key))
            if "items" in node:
                self._assert_required_equals_properties_everywhere(
                    node["items"], path + "[]")

    def test_required_equals_properties_at_every_level(self):
        with open(MERGE_REVIEW_SCHEMA_JSON, encoding="utf-8") as handle:
            schema = json.load(handle)
        self._assert_required_equals_properties_everywhere(schema)

    def _assert_additional_properties_false_everywhere(self, node, path="<root>"):
        if isinstance(node, dict):
            if isinstance(node.get("properties"), dict):
                self.assertIs(
                    node.get("additionalProperties"), False,
                    "at {}: additionalProperties is {!r}, not False -- OpenAI "
                    "strict mode rejects any object schema missing this, with "
                    "HTTP 400 invalid_json_schema".format(
                        path, node.get("additionalProperties")))
                for key, subschema in node["properties"].items():
                    self._assert_additional_properties_false_everywhere(
                        subschema, "{}.{}".format(path, key))
            if "items" in node:
                self._assert_additional_properties_false_everywhere(
                    node["items"], path + "[]")

    def test_additional_properties_is_false_at_every_level(self):
        with open(MERGE_REVIEW_SCHEMA_JSON, encoding="utf-8") as handle:
            schema = json.load(handle)
        self._assert_additional_properties_false_everywhere(schema)


class TestShipByDefault(unittest.TestCase):
    """`/build`'s review policy lives in prose, and both of its rules have a
    half that drifts back to the expensive default by OMISSION rather than by
    edit.

    The older jurisdiction-plus-evidence STOP rule gave way to a hard
    one-round cap, and the reasoning carries over: a rule re-derived from
    memory keeps its intuitive half and drops the other — and *a doc that
    carries only the intuitive half reads complete*. Both rules below have
    exactly that shape, so the guard is worth having.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = BUILD_MD.read_text()

    def test_the_merge_review_trigger_carries_BOTH_failure_modes(self):
        """`silent -> gate` is the intuitive half and re-derives on its own.
        `loud -> ship` is the half that pays for the change, and dropping it
        restores the unconditional gate silently: a doc saying only "gate
        silent-failure surfaces" reads complete while giving a reader no
        licence to skip anything.
        """
        for half in ("Loud", "Silent"):
            self.assertIn(
                "- **{}**".format(half), self.text,
                "Merge Review's trigger is missing its `{}` half — without both, the "
                "gate reverts to unconditional by omission and its full cost "
                "comes back on every build".format(half.lower()))

    def test_the_loud_half_actually_says_to_skip_the_gate(self):
        """Naming the loud case is not the mechanism; *instructing the skip* is.

        A file-wide search for
        "ship" is satisfied by any of a dozen other mentions, so the assertion
        is scoped to the Loud bullet itself. Without this, the bullet can be
        softened to "loud failures are cheaper to catch in flight" — true,
        advisory, and not a rule — while the test stays green.
        """
        start = self.text.find("- **Loud**")
        self.assertNotEqual(start, -1, "the Loud bullet is gone entirely")
        end = self.text.find("- **Silent**", start)
        self.assertNotEqual(end, -1, "the Silent bullet is gone entirely")
        bullet = self.text[start:end]
        self.assertRegex(
            bullet, r"[Ss]kip Merge Review",
            "the Loud bullet must itself say to SKIP Merge Review. Describing loud "
            "failures as cheap to catch in flight is an observation; the "
            "policy is the instruction, and only the instruction changes "
            "what a build costs")

    def test_the_fix_round_cap_is_unconditional(self):
        """The cap is one round, with no escape clause. Any qualifier is the
        loop coming back — the retired STOP rule WAS the qualified version, and
        it did not terminate: fixes carry their own defect rate and the
        artifact grows, so 'unless there are still blockers' is unbounded in
        practice.
        """
        self.assertIn(
            "There is no second Merge Review pass, under any condition.", self.text,
            "the fix-round cap has been qualified or removed. A conditional "
            "cap is the STOP rule again under another name, and that is the "
            "loop this replaced")

    def test_merge_review_states_the_same_cap(self):
        """merge-review.md explains the cap, and must not describe it as a
        round of RE-review: read that way, an architect dispatches a second
        Merge Review after the fixes, which is the loop the cap forbids."""
        text = " ".join((SCRIPTS_DIR / "merge-review.md").read_text().split())
        start = text.find("### Why the fix-round cap is what it is")
        self.assertNotEqual(start, -1, "merge-review.md lost its cap section")
        section = text[start:]
        self.assertIn("There is no second Merge Review pass.", section)
        self.assertNotIn("re-review", section)
        self.assertNotIn("Round one", section)

    def test_the_cap_ships_with_its_compensating_control(self):
        """One round instead of N is only defensible because the author
        mutation-tests the fixes. Drop that and this is not a cheaper policy,
        it is an unreviewed one — a fix's own test is the thing in this repo
        most likely to be vacuous, and a review round routinely finds one.
        """
        start = self.text.find("### Fix rounds")
        self.assertNotEqual(start, -1, "the fix-round section is gone entirely")
        end = self.text.find("\n## ", start)
        section = self.text[start:end if end != -1 else len(self.text)]

        # Asserted on the INSTRUCTION, not on the topic. A `[Mm]utation-test`
        # regex over this section passes even with the requirement deleted,
        # because the section also *discusses* mutation-testing a paragraph
        # later ("Mutation-testing the fixes is the load-bearing half"). Caught
        # by mutation: removing the requirement failed nothing. This is the
        # third narrowing of the same flaw in this file's history — a test that
        # searches a region proves something about the region rather than about
        # the thing it names.
        self.assertIn(
            "mutation-test each fix", section,
            "the fix-round section must instruct mutation-testing each fix. It "
            "is what replaces the second reviewer; without it the one-round "
            "cap removes a control and substitutes nothing. Discussing "
            "mutation-testing nearby is not the requirement")


class TestDosageRule(unittest.TestCase):
    """The failure-shape dosage rule.

    It is the largest token lever in the file and it lives entirely in prose,
    with the same drift shape as the rules `TestShipByDefault` guards: every one
    of its clauses can be softened into an observation while the file still
    reads complete. Each assertion below pins one clause that could otherwise
    be deleted with every other test still green.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = BUILD_MD.read_text()

    def loud_bullet(self):
        start = self.text.find("- **Loud**")
        self.assertNotEqual(start, -1, "the Loud bullet is gone entirely")
        end = self.text.find("- **Silent**", start)
        self.assertNotEqual(end, -1, "the Silent bullet is gone entirely")
        return self.text[start:end]

    def silent_bullet(self):
        start = self.text.find("- **Silent**")
        self.assertNotEqual(start, -1, "the Silent bullet is gone entirely")
        end = self.text.find("\n\n", start)
        self.assertNotEqual(end, -1, "the Silent bullet has no terminator")
        return self.text[start:end]

    def test_the_loud_bullet_skips_merge_review(self):
        """The saving IS this clause, now that the fan-out (and the tiers it
        gated) is retired: dosage decides only whether Merge Review runs.
        Naming loud builds as cheap to fix in flight is an observation;
        instructing the skip is the policy, and only the instruction changes
        what a build costs.

        Scoped to each bullet, not the file: `/build` discusses Merge Review
        constantly, so a file-wide search is satisfied by other mentions — the
        narrowing failure this file has hit before.
        """
        self.assertIn(
            "Skip Merge Review", self.loud_bullet(),
            "the Loud bullet must instruct skipping Merge Review. Without the "
            "instruction this is a description of loud failures, and Merge "
            "Review runs on every build")
        self.assertIn(
            "Run Merge Review", self.silent_bullet(),
            "the Silent bullet must instruct running Merge Review — the only "
            "gate dosage still decides")

    def test_the_dosage_table_carries_both_shapes(self):
        """The table is what an architect actually reads before dispatching.
        A single-row table reads complete and silently restores the expensive
        default for whichever shape went missing.
        """
        for shape in ("**Loud**", "**Silent**"):
            self.assertRegex(
                self.text, r"\|\s*" + re.escape(shape) + r"\s*\|",
                "the dosage table is missing its {} row — a table with one "
                "shape gives no licence to dose anything down".format(shape))

    def test_the_dosage_tables_say_which_gates_run(self):
        """The row labels existing is not the same as the rows being right.
        Flipping a silent row's Merge Review from `run` to `skip` disarms the
        gate on exactly the surfaces it exists for, and every other test stays
        green. Re-dosing is a real decision, so it has to come through here.
        """
        rows = (
            "| **Loud** | architectural only | run | run | skip |",
            "| **Silent** | architectural only | run | run | run |",
            "| **Loud**, under ~6 tasks | architectural | run | skip | run | skip |",
            "| **Loud**, ~6+ tasks | architectural | run | run | run | skip |",
            "| **Silent**, under ~6 tasks | architectural | run | skip | run | run |",
            "| **Silent**, ~6+ tasks | architectural | run | run | run | run |",
        )
        for row in rows:
            self.assertIn(
                row, self.text,
                "a dosage table row changed or is gone: {!r} — which gates "
                "run for each failure shape is a decision, not a wording "
                "choice".format(row))

        # Survives a reformat: read the cells, not the exact spacing.
        loud, silent = [], []
        for line in self.text.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells[0].startswith("**Loud**"):
                loud.append(cells)
            elif cells[0].startswith("**Silent**"):
                silent.append(cells)
        self.assertGreaterEqual(
            len(loud), 2, "fewer than 2 Loud dosage rows were found to check")
        self.assertGreaterEqual(
            len(silent), 2, "fewer than 2 Silent dosage rows were found to check")
        for cells in loud:
            self.assertEqual(
                cells[-1], "skip",
                "Loud row {!r} must skip Merge Review: shipping a loud "
                "failure unreviewed is the saving the rule exists for".format(
                    cells[0]))
        for cells in silent:
            self.assertEqual(
                cells[-1], "run",
                "Silent row {!r} must run Merge Review: a silent failure "
                "announces nothing, so the gate is the only reviewer".format(
                    cells[0]))

    def test_the_single_reviewer_keeps_its_unmeasured_caveat(self):
        """**The most important guard in this class.**

        How much one blinded reviewer misses compared with several is an
        unmeasured bet. A rule stated without its caveat reads MEASURED, which
        is strictly worse than the rule not existing: the next session finds a
        confident-looking policy, extends it by the same logic, and has no way
        to know the evidence was never there.
        """
        flat = " ".join(self.text.split())
        self.assertIn(
            "How much one reviewer misses compared with several is UNMEASURED.",
            flat,
            "the sentence naming one-vs-many recall as unmeasured is gone. "
            "Without it the single reviewer reads as measured-equivalent "
            "when no paired comparison has ever been run")
        self.assertIn(
            "Do not describe the single reviewer as measured-equivalent.",
            flat,
            "the explicit prohibition on calling the single reviewer "
            "measured-equivalent is gone. It is the half with no evidence "
            "behind it, and it is the half a future session will most want to "
            "cite")

    def test_the_dosage_definition_is_not_duplicated(self):
        """The loud/silent bullets were hoisted OUT of Merge Review so three
        gates read one definition. Re-adding a local copy is the obvious edit
        and it recreates the drift the hoist removed: two triggers, one updated,
        and the stale one is the one an architect reads first.
        """
        self.assertEqual(
            self.text.count("- **Loud**"), 1,
            "the loud/silent definition appears more than once. It governs "
            "three gates and must live in exactly one place — a second copy "
            "drifts, and nothing makes the divergence visible")

    def test_the_plan_review_re_round_cap_is_bounded(self):
        """Mirror of `test_the_fix_round_cap_is_unconditional`, for the gate
        that runs on every build. The unbounded version ("rewrite it and run it
        again") has no terminating condition: a fix pass grows the plan, cost is lenses x brief size, so
        round N+1 costs more than round N forever.
        """
        self.assertIn(
            "There is no third Plan Review pass.", self.text,
            "the Plan Review re-round cap has been removed or qualified. An "
            "unbounded re-gate is the loop the Merge Review cap already "
            "retired, on the same measured arithmetic")

    def test_the_pre_dispatch_quote_tells_the_user_to_measure(self):
        """A gate's cost has to be visible BEFORE it is spent, or the dosage
        rule has nothing to act on.

        Two halves, and both regress by softening rather than by deletion. The
        instruction ("say the quote before you dispatch") degrades into a
        description of what gates cost. The number degrades into a figure
        measured on someone else's machine, which reads exact and is wrong for
        every other plan, model and repository. So the table must send the user
        to their own measurement, say "unknown" until they have one, and carry
        no token figure of its own.
        """
        self.assertIn(
            "Say the quote out loud before you dispatch a gate.", self.text,
            "the pre-dispatch quote instruction is gone. Without it gate spend "
            "is only ever visible after the fact")
        start = self.text.find("What you are about to run")
        self.assertNotEqual(start, -1, "the pre-dispatch quote table is gone")
        end = self.text.find("\n\n", start)
        table = self.text[start:end]
        self.assertIn("`claude-build-cost`", table,
                      "the economical row no longer says how to measure a gate")
        self.assertIn("unknown", table,
                      "the economical row no longer says what to quote before "
                      "the user has measured a gate")
        self.assertIsNone(
            re.search(r"\b\d+(?:–\d+)?k\b", table),
            "the quote table carries a token figure; a figure measured on one "
            "machine is not an estimate for another")

    def test_the_window_percentage_names_its_provenance(self):
        """A derived number dressed as a reading is the defect this guards. A
        percentage is arithmetic on a measured ratio, and the sentence saying
        so is what stops the next reader treating it as a reading.

        The floor caveat is the load-bearing half. The scan sees one machine of
        an account-wide window, so every figure understates — dropping that
        makes the whole table read reassuring in the one direction that costs a
        build.
        """
        self.assertRegex(
            self.text, r"counts this\s+machine only",
            "the window percentages have lost the sentence saying they count "
            "one machine. They understate by construction, and a figure that "
            "reads exact is worse than no figure")
        flat = " ".join(self.text.split())
        self.assertIn(
            "your own seat's ratio", flat,
            "the percentage no longer says whose ratio it needs. A borrowed "
            "seat's percentage is wrong for every other plan")
        self.assertIn(
            "stated in your own `CLAUDE.md`", flat,
            "the percentage no longer says where the user's own ratio lives")
        self.assertIn(
            "`claude-build-cost`", flat,
            "the cost section no longer names the tool that measures a gate")

    def test_the_plan_size_threshold_names_a_number(self):
        """A threshold without a number is advice ("if a build feels too
        expensive to gate, split the build"), and advice is what a long plan
        gets gated under anyway. The number is what makes it a step.
        """
        start = self.text.find("**Split a plan before you gate it")
        self.assertNotEqual(start, -1, "the plan-size rule is gone entirely")
        section = self.text[start:start + 1200]
        self.assertRegex(
            section, r"800 lines or 8 tasks",
            "the plan-size threshold must name its numbers. Splitting plans is "
            "the only in-gate cost lever measured to work, and an unquantified "
            "version is the sentence that was already there and never acted on")

    def test_every_gate_applies_its_full_checklist(self):
        """With the fan-out retired there is no tier left to withhold — a gate
        that runs applies every lens question every time. Without this
        sentence a reader could believe some vestige of default/escalation
        staging survived the retirement.
        """
        self.assertIn(
            "**Every gate that runs applies its full checklist, whatever the "
            "shape.**", self.text,
            "the sentence saying every running gate applies its full "
            "checklist is gone — without it dosage reads as if it still "
            "withholds lenses from a fan-out that no longer exists")


class TestCorpusPointersResolve(unittest.TestCase):
    """`/build` points at `docs/build-measurements.md` for how to measure a
    gate. It runs from whatever repository is being built, so the pointer has
    to resolve from anywhere, and the document it names holds methods only:
    nothing may send a reader to a figure or section it does not have."""

    @classmethod
    def setUpClass(cls):
        cls.build_md = BUILD_MD.read_text()
        corpus_path = REPO / "docs" / "build-measurements.md"
        if not corpus_path.is_file():
            raise AssertionError(
                "docs/build-measurements.md is missing — build.md points at it "
                "for how to measure a gate")
        cls.corpus = corpus_path.read_text()

    def test_the_corpus_pointers_resolve_from_any_repo(self):
        """A bare relative pointer resolves only inside this repository; the
        plugin path resolves everywhere the builder plugin is loaded."""
        bare = self.build_md.count("`docs/build-measurements.md`")
        self.assertEqual(
            bare, 0,
            "{} pointer(s) to the corpus use a bare relative path. /build runs "
            "from other repos, where that path does not exist — use the "
            "${{CLAUDE_PLUGIN_ROOT}}/tools/build-lens/ path".format(bare))
        self.assertIn(
            "`${CLAUDE_PLUGIN_ROOT}/tools/build-lens/build-measurements.md`",
            self.build_md,
            "build.md no longer points at the corpus by its plugin path")
        self.assertNotIn(
            "~/.claude/build-lens/", self.build_md,
            "build.md still names the retired ~/.claude/build-lens/ path")

    def test_the_corpus_does_not_gesture_at_sections_it_lacks(self):
        """An in-file cross-reference that names a section the document does
        not have points nowhere, while stating an instruction the reader then
        cannot check."""
        headings = "\n".join(
            l for l in self.corpus.splitlines() if l.startswith("#"))
        for phrase, missing in (
                ("at the top of this\nfile", "Dosage"),
                ("Phase 5 below", "Phase 5"),
                ("immediately below", "Merge Review")):
            self.assertNotIn(
                phrase, self.corpus,
                "the corpus says {!r}, but it has no {} section, so the phrase "
                "points nowhere. (Headings present: {})"
                .format(phrase, missing, headings[:200]))

    def test_the_corpus_never_tells_a_re_round_to_carry_prior_findings(self):
        """The gate references hand a Plan Review reviewer the plan and its
        requirements only, and `claude-merge-brief --plan` refuses a findings
        payload. A methods document saying a re-round brief carries the prior
        findings contradicts both: a reader either breaks blinding or hits a
        refused gate."""
        flat = " ".join(self.corpus.split())
        for phrase in ("carries the previous round's findings",
                       "keep the prior findings"):
            self.assertNotIn(phrase, flat)

    def test_no_build_lens_file_cites_a_record_the_corpus_lacks(self):
        """The corpus holds methods, not measurements. A reference that sends
        the reader there for "figures", a seat anchor or a named section the
        corpus does not carry is a pointer to nothing."""
        for path in [BUILD_MD, *REFERENCE_FILES]:
            text = " ".join(path.read_text().split())
            for phrase in ("Figures: `build-measurements.md`", "seat anchor",
                           "full record is in"):
                self.assertNotIn(phrase, text, "{} says {!r}".format(path.name, phrase))


class TestDirectiveInventorySurvives(unittest.TestCase):
    """The mechanical half of the defence against a rule getting lost when
    `/build`'s text is rewritten or moved between the skill and its gate
    references.

    `directives-baseline.txt` was captured from `skills/builder/build/SKILL.md`
    and the four gate references at a single commit -- every `**bold**` span
    and every non-separator table row, whitespace-normalised, one per line.
    This test asserts every one of those lines still appears,
    whitespace-normalised, somewhere in the skill and the references, unless
    it is declared below: a rewrite names its replacement (which must be
    present), a retirement names its reason (and the line must really be
    gone). Never regenerate the fixture to make a deletion pass: a baseline
    rebuilt from the edited text proves only that the edit agrees with itself.

    What this test CANNOT see: a directive that survives verbatim into a
    reference file nobody is told to read. That is the gate's job.
    """

    SANCTIONED_REWRITES = (
        # The Codex calibration table ships empty, so the wrapper prints the
        # meter after a run, not an estimated share of it.
        ("| `full-power` — Codex | 0 Claude | Codex quota; the wrapper prints "
         "its share after each run |",
         "| `full-power` — Codex | 0 Claude | Codex quota; the wrapper prints "
         "its meter after each run |"),
    )
    SANCTIONED_RETIREMENTS = ()

    @classmethod
    def setUpClass(cls):
        if not DIRECTIVES_BASELINE.is_file():
            raise AssertionError(
                "tools/build-lens/tests/fixtures/directives-baseline.txt is "
                "missing — the directive inventory this test reads from was "
                "never captured")
        raw = DIRECTIVES_BASELINE.read_text().splitlines()
        cls.baseline = [
            line for line in raw if line and not line.startswith("#")]
        assert cls.baseline, "the baseline fixture has no directive lines"
        union_parts = [BUILD_MD.read_text()]
        union_parts.extend(
            path.read_text() for path in REFERENCE_FILES if path.is_file())
        cls.flat_union = " ".join(" ".join(union_parts).split())

    @staticmethod
    def _lost(baseline, flat_union, rewrites, retirements):
        rewritten = dict(rewrites)
        retired = {old for old, _reason in retirements}
        missing = []
        for line in baseline:
            if line in flat_union or line in retired:
                continue
            replacement = rewritten.get(line)
            if replacement is not None:
                if " ".join(replacement.split()) in flat_union:
                    continue
                missing.append(
                    "{!r} was rewritten, but its declared replacement {!r} is "
                    "not present either -- the rule is gone, not repaired."
                    .format(line, replacement))
                continue
            missing.append(line)
        return missing

    @staticmethod
    def _bad_retirements(baseline, flat_union, retirements):
        baseline_set = set(baseline)
        bad = []
        for line, reason in retirements:
            if line not in baseline_set:
                bad.append("{!r} is declared retired ({}) but is not a line in "
                           "the baseline fixture at all.".format(line, reason))
            elif line in flat_union:
                bad.append("{!r} is declared retired ({}) but is still present "
                           "-- it came back, or never left.".format(line, reason))
        return bad

    def test_every_baseline_directive_survives(self):
        missing = self._lost(self.baseline, self.flat_union,
                             self.SANCTIONED_REWRITES, self.SANCTIONED_RETIREMENTS)
        self.assertFalse(
            missing,
            "{} directive(s) from the baseline are no longer found, "
            "whitespace-normalised, anywhere in the skill or the gate "
            "references: {}".format(len(missing), missing))

    def test_every_retirement_is_real(self):
        """A stale retirement could hide a line that came back, or one that was
        never in the baseline, behind a reason that sounds authoritative."""
        self.assertFalse(self._bad_retirements(
            self.baseline, self.flat_union, self.SANCTIONED_RETIREMENTS))

    def test_positive_control_a_deleted_directive_is_caught(self):
        line = self.baseline[0]
        self.assertIn(line, self.flat_union)
        self.assertEqual(
            self._lost([line], self.flat_union.replace(line, ""), (), ()), [line])

    def test_positive_control_a_rewrite_without_its_replacement_is_caught(self):
        line = self.baseline[0]
        union = self.flat_union.replace(line, "")
        self.assertEqual(
            len(self._lost([line], union, ((line, "a replacement nobody wrote"),), ())), 1)

    def test_positive_control_a_fabricated_retirement_is_caught(self):
        self.assertEqual(len(self._bad_retirements(
            self.baseline, self.flat_union,
            (("**a directive that was never in the baseline**", "made up"),))), 1)
        self.assertEqual(len(self._bad_retirements(
            self.baseline, self.flat_union,
            ((self.baseline[0], "still present"),))), 1)


class TestLedgerRecordsTheSnapshotVerbatim(unittest.TestCase):
    """`claude-codex-review` prints a `reviewed snapshot:` line, the
    only record of what a diff gate reviewed outside the model's report. The
    ledger template must say to copy it VERBATIM -- a paraphrase or a subset
    of its fields can no longer tie a gate result to an immutable tree."""

    def test_the_ledger_block_has_a_verbatim_snapshot_line(self):
        text = BUILD_MD.read_text()
        start = text.index("### The Build Ledger")
        fence = text.index("```\n", start)
        block = text[fence + 4:text.index("```", fence + 4)]
        snapshot = [line for line in block.splitlines()
                    if line.startswith("Snapshot:")]
        self.assertEqual(len(snapshot), 1, block)
        self.assertIn("reviewed snapshot:", snapshot[0])
        self.assertIn("verbatim", snapshot[0])


class TestBuildMdStaysCompressed(unittest.TestCase):
    """The skill is read once and never re-read, and such a file loses its
    later steps first as it grows. A cap makes every addition pay for itself:
    cut before you add, and register a directive that leaves the skill in
    `TestDirectiveInventorySurvives`' SANCTIONED_RETIREMENTS or _REWRITES."""

    MAX_WORDS = 3500

    def test_build_md_is_at_most_max_words(self):
        # Body only: the frontmatter description is listing metadata, not
        # read-once instructions.
        text = BUILD_MD.read_text()
        body = text.split("\n---\n", 1)[1]
        words = len(body.split())
        self.assertLessEqual(
            words, self.MAX_WORDS,
            "the build skill is {} words, over the {}-word cap. Cut "
            "before you add, and register any directive you remove in "
            "SANCTIONED_RETIREMENTS or SANCTIONED_REWRITES".format(
                words, self.MAX_WORDS))


class TestEveryReferencePointerNamesThePluginPath(unittest.TestCase):
    """Every pointer to a reference names the plugin path
    (`${CLAUDE_PLUGIN_ROOT}/tools/build-lens/<name>.md`), never a bare
    relative one or the retired `~/.claude/build-lens/`. `/build` runs from
    whatever repo is being built, so a bare relative pointer resolves only
    inside this checkout -- the same reasoning
    `test_the_corpus_pointers_resolve_from_any_repo` already applies to
    `build-measurements.md`, extended here to the four gate references.
    """

    @classmethod
    def setUpClass(cls):
        cls.build_md = BUILD_MD.read_text()

    def test_every_reference_pointer_names_the_plugin_path(self):
        self.assertNotIn(
            "~/.claude/build-lens/", self.build_md,
            "build.md still names the retired ~/.claude/build-lens/ path")
        for path in REFERENCE_FILES:
            plugin_pointer = "`${{CLAUDE_PLUGIN_ROOT}}/tools/build-lens/{}`".format(
                path.name)
            self.assertIn(
                plugin_pointer, self.build_md,
                "build.md does not point at {} using the plugin path "
                "{}".format(path.name, plugin_pointer))
            bare_pointer = "`{}`".format(path.name)
            self.assertNotIn(
                bare_pointer, self.build_md,
                "build.md points at {} with a bare relative pointer ({}), "
                "which resolves only from this checkout -- every pointer "
                "must use the ${{CLAUDE_PLUGIN_ROOT}}/tools/build-lens/ form"
                .format(path.name, bare_pointer))


class TestTheReferencesDoNotGestureAtSectionsTheyLack(unittest.TestCase):
    """Sibling of `TestCorpusPointersResolve`'s
    `test_the_corpus_does_not_gesture_at_sections_it_lacks`, for the gate
    references. Each reference holds text that once lived in the skill, and
    moving text verbatim carries along any in-file cross-reference it makes
    -- "below", "above", "see **Dosage**", or similar -- left pointing at a
    section that is no longer in the same file.

    A bare word search for "below"/"above" is not safe here: both files carry
    legitimate SAME-file uses (e.g. plan-review.md's "Each lens below" and "see
    below" both point at sections that genuinely are further down in
    plan-review.md itself, and "above the effort you wrote it at" in
    code-review.md is a comparison, not a pointer). This asserts against the
    specific carriers that were actually verified to point at a section their
    file lacks -- confirmed by reading each reference's own headings.

    A carrier counts as RESOLVED when the gesture NAMES THE FILE the section
    moved to, not when the phrase is deleted: "see **Dosage**" is broken while
    "see **Dosage** in the build skill's file" is fixed, so the check is a
    window after the phrase rather than a plain `phrase in text`. A standing
    regression guard, since a future move can strand these same phrases
    again.
    """

    # A gesture is resolved by naming THE FILE THAT ACTUALLY HOLDS the section,
    # within this many characters after it. Wide enough to survive a line break
    # mid-sentence, narrow enough that an unrelated later mention cannot
    # launder a bare gesture.
    #
    # The expected target is per-carrier and not a generic "any file path".
    # The first cut accepted any marker in the window, and a mutation proved
    # that vacuous in the other direction: repointing plan-review.md's
    # `see **Dosage**` at `code-review.md` -- a file with
    # no Dosage section -- left every test green. A pointer at the wrong file
    # is worse than a bare gesture, because it reads as already fixed.
    RESOLUTION_WINDOW = 60

    CARRIERS = (
        # (reference filename, phrase, the section it gestures at that this
        # file does not contain, the EXACT marker naming the file that holds
        # that section)
        ("code-review.md",
         "see the measurement table above",
         "the pre-dispatch quote table (in skills/builder/build/SKILL.md)",
         "skills/builder/build/SKILL.md"),
        ("code-review.md",
         "the standing merge-ref rule uses — see below",
         "Merge Review (now tools/build-lens/merge-review.md)",
         "`merge-review.md` in the same directory as this file"),
        ("plan-review.md",
         "Omit entirely on a **loud** surface — see **Dosage**",
         "Dosage (only in skills/builder/build/SKILL.md)",
         "skills/builder/build/SKILL.md"),
        ("plan-review.md",
         "**loud** one pass no escalation lenses at all — see "
         "**Dosage**",
         "Dosage (only in skills/builder/build/SKILL.md)",
         "skills/builder/build/SKILL.md"),
    )

    @classmethod
    def setUpClass(cls):
        # Whitespace-normalised, so a gesture that wraps across a line break
        # is still matched -- the same `flat` view the survival test uses.
        cls.texts = {
            path.name: " ".join(path.read_text().split())
            for path in REFERENCE_FILES if path.is_file()}

    def test_the_references_do_not_gesture_at_sections_they_lack(self):
        failures = []
        for filename, phrase, missing, expected in self.CARRIERS:
            text = self.texts.get(filename, "")
            flat_phrase = " ".join(phrase.split())
            start = text.find(flat_phrase)
            while start != -1:
                after = text[start + len(flat_phrase):][
                    :self.RESOLUTION_WINDOW]
                if expected not in after:
                    failures.append(
                        "{}: {!r} points at {}, which this file does not "
                        "contain. The gesture must name {!r} within {} "
                        "characters; it says {!r}."
                        .format(filename, phrase, missing, expected,
                                self.RESOLUTION_WINDOW, after))
                start = text.find(flat_phrase, start + 1)
        self.assertFalse(
            failures,
            "{} in-file cross-reference(s) survived the move and now point "
            "at a section that file does not have:\n{}"
            .format(len(failures), "\n".join(failures)))


class TestNoSuperpowersDependency(unittest.TestCase):
    """`/build` must not invoke a plugin it does not ship.

    The skill's design, plan and execution phases came from the superpowers
    plugin's `brainstorming`, `writing-plans` and
    `subagent-driven-development` skills, and now carry their own steps. A
    `superpowers:` invocation left behind would tell the architect to run a
    skill that a user without that plugin does not have. It fails SILENTLY:
    the model improvises the phase and the build looks normal.

    It reads the whole dispatchable surface, not just the skill, because the
    same dependency can reappear in an agent or a gate reference.
    """

    # Each entry pairs a directory with a file that MUST be found inside it.
    # The sentinel is what stops a renamed directory or a typo'd entry from
    # silently dropping out of the sweep -- see the positive control below.
    SURFACE = (
        ("skills", "skills/builder/build/SKILL.md"),
        ("agents", "agents/code-reviewer.md"),
        ("tools/build-lens", "tools/build-lens/blinded-review.js"),
        ("scripts", "scripts/setup-plugins.sh"),
        ("docs", "docs/decisions.md"),
    )
    # `.js` is not optional: tools/build-lens's dispatched half is
    # blinded-review.js, the file `/build` runs
    # through Workflow. A sweep that reads only the documentation half of a
    # directory it claims to protect is a guard with a hole in it. `.sh` is
    # not optional either: every file under scripts/ is a shell script, so
    # without it the scripts/ half of SURFACE sweeps zero files and its
    # positive control could never turn red.
    SUFFIXES = (".md", ".js", ".sh")

    def _files(self):
        # docs/build-measurements.md discusses the retirement as history and
        # must keep doing so -- it is the one file this sweep deliberately
        # does not read.
        excluded = REPO / "docs" / "build-measurements.md"
        for rel, _sentinel in self.SURFACE:
            root = REPO / rel
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if path.suffix not in self.SUFFIXES or not path.is_file():
                    continue
                if path == excluded:
                    continue
                # The tests directory is allowed to name the string it guards.
                if "tests" in path.relative_to(REPO).parts:
                    continue
                if self._declares_superpowers(path):
                    continue
                yield path

    @staticmethod
    def _superpowers_groups():
        """Skill groups whose profile declares superpowers as a prerequisite.

        Read from claude/profiles.json rather than listed here: a profile that
        declares it gets install.sh's report when it is missing, so its skills
        may name it. Every other group, builder included, may not."""
        with (REPO / "claude" / "profiles.json").open(encoding="utf-8") as handle:
            registry = json.load(handle)["profiles"]
        return {name for name, entry in registry.items()
                if any(str(item.get("id", "")).startswith("superpowers@")
                       for item in entry.get("prerequisites", []))}

    def _declares_superpowers(self, path):
        parts = path.relative_to(REPO).parts
        return (len(parts) > 2 and parts[0] == "skills"
                and parts[1] in self._superpowers_groups())

    def test_no_profile_is_exempt_and_the_maintainer_skills_are_swept(self):
        """No shipped profile declares superpowers, so the exemption reaches
        nothing: maintainer's skill-builder is swept like builder's /build."""
        self.assertEqual(set(), self._superpowers_groups())
        read = {p.relative_to(REPO).as_posix() for p in self._files()}
        self.assertIn("skills/builder/build/SKILL.md", read)
        self.assertIn("skills/maintainer/skill-builder/SKILL.md", read)
        self.assertIn("skills/maintainer/skill-builder/references/authoring.md",
                      read)

    def test_no_superpowers_reference_survives_on_the_build_surface(self):
        offenders = []
        for path in self._files():
            for lineno, line in enumerate(
                    path.read_text().splitlines(), start=1):
                if "superpowers:" in line:
                    offenders.append(
                        "{}:{}: {}".format(
                            path.relative_to(REPO), lineno, line.strip()))
        self.assertFalse(
            offenders,
            "{} reference(s) to the superpowers plugin survive on the build "
            "surface. This toolkit does not ship that plugin, so for a user "
            "without it each of these is a silent no-op instruction:"
            "\n{}".format(len(offenders), "\n".join(offenders)))

    def test_the_guard_actually_reads_every_declared_surface(self):
        """Positive control over every SURFACE entry, as one aggregate
        assertion whose message names each entry the sweep missed.

        The test above passes trivially if `_files()` yields nothing, so a
        rename or a typo'd entry would make a green result mean "nothing was
        checked". Asserting only the skill would leave the rest of the surface
        able to drop out silently -- the same failure the control exists to
        prevent, just moved.

        `.js` is in the sentinel set deliberately: it fails if SUFFIXES stops
        covering the dispatched scripts.
        """
        read = {p.relative_to(REPO).as_posix() for p in self._files()}
        missing = [s for _rel, s in self.SURFACE if s not in read]
        self.assertFalse(
            missing,
            "the surface sweep did not reach {}, so a green result above "
            "proves nothing about {}. Files read: {}"
            .format(missing, "that entry" if len(missing) == 1 else
                    "those entries", sorted(read) or "NONE"))

    # Pins the four merge-brief rubric rules and the three build-skill rules
    # that replaced the superpowers phases, with one assertion each, rather than one aggregate
    # assertion over a collected list -- so deleting a single rule reds only
    # the test that names it, and the other six stay green. Whitespace is
    # normalized on both sides of every comparison: all seven strings live in
    # wrapped prose, and a future reflow must not red these assertions.

    PLAN_REQUIREMENTS = REPO / "tools" / "merge-brief" / "plan-requirements.md"

    @staticmethod
    def _normalize(text):
        return " ".join(text.split())

    def test_g1_forbids_a_name_agreed_nowhere(self):
        text = self._normalize(self.PLAN_REQUIREMENTS.read_text())
        needle = self._normalize(
            "a name agreed nowhere is a name it invents")
        self.assertIn(
            needle, text,
            "plan-requirements.md no longer states the G1 rubric rule -- "
            "its naming-provenance requirement is gone")

    def test_g2_requires_identical_naming_across_tasks(self):
        text = self._normalize(self.PLAN_REQUIREMENTS.read_text())
        needle = self._normalize(
            "spelled identically in every other task that references it")
        self.assertIn(
            needle, text,
            "plan-requirements.md no longer states the G2 rubric rule -- "
            "its cross-task naming-consistency requirement is gone")

    def test_g3_requires_the_file_manifest_before_the_first_task(self):
        text = self._normalize(self.PLAN_REQUIREMENTS.read_text())
        needle = self._normalize(
            "with each file's responsibility, before its first task")
        self.assertIn(
            needle, text,
            "plan-requirements.md no longer states the G3 rubric rule -- "
            "its file-manifest-placement requirement is gone")

    def test_g4_requires_a_failing_test_observed_before_implementation(self):
        text = self._normalize(self.PLAN_REQUIREMENTS.read_text())
        needle = self._normalize(
            "writing the failing test and observing it fail before the "
            "implementation step")
        self.assertIn(
            needle, text,
            "plan-requirements.md no longer states the G4 rubric rule -- "
            "its test-first-ordering requirement is gone")

    def test_g7_flags_bounded_work_that_is_architectural(self):
        lines = self.PLAN_REQUIREMENTS.read_text().splitlines()
        start = None
        end = len(lines)
        for i, line in enumerate(lines):
            if start is None:
                if line.strip() == "## Structure":
                    start = i + 1
                continue
            if line.startswith("## "):
                end = i
                break
        self.assertIsNotNone(
            start, "plan-requirements.md has no '## Structure' section")
        section_lines = lines[start:end]

        bullets = []
        current = None
        for line in section_lines:
            if line.startswith("- "):
                if current is not None:
                    bullets.append(current)
                current = [line]
            elif current is not None:
                current.append(line)
        if current is not None:
            bullets.append(current)
        self.assertTrue(bullets, "## Structure section has no bullets")

        last_bullet = self._normalize("\n".join(bullets[-1]))
        expected = self._normalize(
            "- Work the plan labels `Classification: bounded` that adds a "
            "new subsystem, or restructures how existing components fit "
            "together, is architectural: it needs an approved design, and "
            "the bounded label is a finding.")
        self.assertEqual(
            last_bullet, expected,
            "plan-requirements.md's ## Structure list no longer ends with "
            "the bounded-misclassification rule")

    def test_build_md_names_g5_the_merged_tree_suite_gate(self):
        text = self._normalize(BUILD_MD.read_text())
        needle = self._normalize("G5 — run the suite on the merged tree.")
        self.assertIn(
            needle, text,
            "build.md no longer names G5, the merged-tree suite gate -- "
            "the rule is gone")

    def test_build_md_requires_verifying_a_finding_against_the_repo(self):
        text = self._normalize(BUILD_MD.read_text())
        needle = self._normalize(
            "verify it against the repo before it earns a fix")
        self.assertIn(
            needle, text,
            "build.md no longer requires verifying a finding against the "
            "repo before it earns a fix -- the rule is gone")

    def test_build_md_names_the_red_flags_section(self):
        text = self._normalize(BUILD_MD.read_text())
        needle = self._normalize(
            "Red Flags — the rationalizations that defeat this "
            "classification.")
        self.assertIn(
            needle, text,
            "build.md no longer names the Red Flags section -- the rule "
            "is gone")

    def test_g5_names_a_runnable_way_to_build_the_merged_tree(self):
        """G5 must name a runnable way to build the merged tree.

        Telling the architect to run the suite on "the merged tree" before
        dispatching the gate is not enough: under `full` the ONLY thing that builds
        that tree is `claude-codex-review --merge-with`, which creates its
        scratch worktree inside the dispatch and removes it again on the way
        out. So at the moment G5 fires there is nothing to test, and an
        architect satisfies it by running the suite on the UNMERGED branch:
        the step reports green while never exercising the merge result, which
        is the exact silent integration failure G5 was added to catch.

        Asserting the prose alone cannot see that -- such a step reads
        perfectly and is unexecutable. This pins the command that makes it
        runnable, so deleting the procedure reds here rather than quietly
        turning G5 back into advice.
        """
        text = self._normalize(BUILD_MD.read_text())
        self.assertIn(
            self._normalize("git -C <repo> worktree add"), text,
            "build.md's G5 no longer names a command that BUILDS the merged "
            "tree, so it cannot be carried out at the point it is asked for")


class TestFullModeWiringProseExists(unittest.TestCase):
    """`build.md` and its two gate references (`code-review.md`,
    `merge-review.md`) must each say, in prose, how to dispatch the `full`
    review mode: resolve it with `claude-build-route --review-mode`, then run
    `claude-codex-review`.

    **This is a PROSE check and proves nothing about dispatch.** It asserts a
    document contains strings; it cannot observe a gate actually running,
    because no program routes gates -- `build.md`'s gate sections are read and
    acted on by Claude at run time, not executed by anything this suite can
    shim. The limitation is named here rather than papered over by a test
    that implies otherwise.

    `--merge-with` is the load-bearing string: without it the two gates'
    invocations are identical and Code and Merge Review collapse back into one
    instrument. `-o` is load-bearing differently: it is REQUIRED on the real
    wrapper, so an invocation documented without it does not run at all.
    """

    REQUIRED_STRINGS = ("claude-codex-review", "--review-mode", "--merge-with", "-o")

    GATE_REFERENCES = (
        SCRIPTS_DIR / "code-review.md",
        SCRIPTS_DIR / "merge-review.md",
    )

    def test_build_md_names_the_resolver_and_the_full_dispatch(self):
        text = BUILD_MD.read_text()
        missing = [s for s in self.REQUIRED_STRINGS if s not in text]
        self.assertFalse(
            missing,
            "build.md is missing {} -- the full-mode wiring prose (mode "
            "resolver, `claude-codex-review`, `--merge-with`, `-o`) is "
            "incomplete".format(missing))

    def test_each_gate_reference_names_the_same_wiring(self):
        for path in self.GATE_REFERENCES:
            text = path.read_text()
            missing = [s for s in self.REQUIRED_STRINGS if s not in text]
            self.assertFalse(
                missing,
                "{} is missing {} -- it must name the same full-mode wiring "
                "as build.md".format(path.name, missing))


class TestCodeReviewerPlanRefusalIsScoped(unittest.TestCase):
    """`code-reviewer` must refuse to stand in as the sole reviewer of a
    plan as the artifact under review -- Plan Review runs the dispatch
    `plan-review.md` describes (Workflow, or its plain Agent fallback), never
    this agent. But the refusal cannot be blanket: the build skill's
    Progress Check gate dispatches this very agent with "the plan,
    the diff so far, and every OBSERVATIONS block" -- a live caller that
    legitimately hands it a plan as context alongside a diff.

    Two assertions, deliberately separate. One without the other re-opens
    the gap: a description could drop the refusal entirely (silently reinstating
    code-reviewer as a plan reviewer), or it could keep a refusal but widen
    it to a blanket "never for plans" that would contradict -- and could
    cause a refusal of -- the Progress Check gate this repo actively runs.
    """

    def _description(self):
        text = CODE_REVIEWER_MD.read_text()
        match = re.search(r"^description:\s*(.*)$", text, re.M)
        self.assertIsNotNone(
            match, "agents/code-reviewer.md has no `description:` line")
        return match.group(1)

    def test_the_scoped_plan_refusal_is_present(self):
        description = self._description()
        self.assertRegex(
            description,
            r"sole reviewer of a plan as the artifact under review",
            "agents/code-reviewer.md's description no longer refuses to "
            "stand in as the sole reviewer of a plan -- Plan Review runs "
            "the dispatch plan-review.md describes, never this agent, and "
            "that scoped refusal must be named in the frontmatter.")

    def test_the_progress_check_exception_is_preserved(self):
        description = self._description()
        self.assertRegex(
            description,
            r"Progress Check",
            "agents/code-reviewer.md's description dropped the Progress "
            "Check exception -- the build skill's Progress Check dispatches this "
            "agent with a plan as context alongside a diff for the "
            "Progress Check gate, and a blanket 'not for plans' refusal "
            "would contradict a gate this repo actively runs.")

    def test_the_code_review_exception_is_named(self):
        description = self._description()
        self.assertRegex(
            description,
            r"Code Review",
            "agents/code-reviewer.md's description no longer names Code "
            "Review as a second exception to the plan-reading refusal.")
        self.assertRegex(
            description,
            r"blinded-review\.js",
            "agents/code-reviewer.md's description no longer names "
            "blinded-review.js as the dispatcher that hands this agent a "
            "plan path at the economical Code Review gate.")
        body = CODE_REVIEWER_MD.read_text()
        self.assertIn(
            "One named exception: Code Review.", body,
            "agents/code-reviewer.md's body no longer names Code Review as "
            "the one exception under which this agent may read a plan file "
            "named by its dispatch.")


AGENTS_DIR = REPO / "agents"


class TestExecutorSplitContract(unittest.TestCase):
    """The skill dispatches each task twice -- implement, then prove in a
    fresh executor -- and that contract lives in four files: the skill, the
    executor agent, the task-reviewer's description and the policy. Each
    reads fine alone if another drifts back to the old one-pass flow, so
    these pins hold every side with literal strings, and an edit to one side
    goes red."""

    def _read(self, relative):
        return (REPO / relative).read_text(encoding="utf-8")

    def test_build_md_names_the_mode_in_the_dispatch(self):
        text = self._read("skills/builder/build/SKILL.md")
        self.assertIn("plan path, task identifier and mode", text)
        self.assertIn("implement, then prove", text)

    def test_executor_implement_mode_runs_no_verification(self):
        text = self._read("agents/executor.md")
        self.assertIn("**A mode** — `implement` or `prove`", text)
        self.assertIn("Do not run the verification command or\n"
                      "     any suite", text)
        self.assertIn("**`implement` mode:** implement, then go to step 7.",
                      text)

    def test_executor_log_example_is_unique_per_dispatch(self):
        # A fixed log name lets concurrent executors overwrite each other's
        # verification output.
        text = self._read("agents/executor.md")
        self.assertIn('LOG=$(mktemp "$HOME/.cache/executor.XXXXXX")', text)

    def test_executor_escalates_guard_secret_and_force_push(self):
        text = self._read("agents/executor.md")
        self.assertIn("disabling a guard, reading a secret, or a\n"
                      "  force-push", text)

    def test_executor_escalates_an_unplanned_public_interface_change(self):
        text = self._read("agents/executor.md")
        self.assertIn("change the public interface of something other tasks "
                      "in the\n  plan depend on", text)

    def test_executor_report_has_concerns_distinct_from_observations(self):
        text = self._read("agents/executor.md")
        lines = text.split("\n")
        start = lines.index("## Report format")
        fence_open = lines.index("```", start)
        fence_close = lines.index("```", fence_open + 1)
        block = lines[fence_open + 1:fence_close]
        self.assertIn("OBSERVATIONS", block)
        self.assertIn("CONCERNS", block)
        self.assertGreater(block.index("CONCERNS"), block.index("OBSERVATIONS"))
        self.assertIn("do not collapse them", text)

    def test_executor_withholds_web_tools_and_says_why(self):
        text = self._read("agents/executor.md")
        tools_line = re.search(r"^tools: (.*)$", text, re.M).group(1)
        names = [name.strip() for name in tools_line.split(",")]
        for banned in ("WebSearch", "WebFetch", "Skill"):
            self.assertNotIn(banned, names)
        self.assertIn("### Why `WebSearch` and `WebFetch` are withheld", text)

    def test_executor_prove_mode_shows_each_new_test_failing(self):
        text = self._read("agents/executor.md")
        self.assertIn("show each test the\n"
                      "   task added failing for its stated reason", text)
        self.assertIn("A test you\n"
                      "   never saw fail is not evidence that it can.", text)

    def test_task_reviewer_description_says_off_by_default(self):
        text = self._read("agents/task-reviewer.md")
        description = re.search(r"^description: (.*)$", text, re.M).group(1)
        self.assertIn("Off by default", description)
        self.assertNotIn("Use after an executor reports DONE", description)

    def test_policy_describes_the_split(self):
        text = self._read("policy/CLAUDE.md")
        self.assertIn("then a\n  fresh `toolkit-build:executor` proves it", text)
        self.assertNotIn("implements one plan task and runs its", text)


class TestArchitectReviewerIsOptional(unittest.TestCase):
    """The whole-branch reviewer is dispatched by hand. Nothing in the build
    skill or its references may route to it by default, and its own text must
    not describe it as part of a gate, or an optional reader turns into a
    silent requirement."""

    FORBIDDEN = ("light path", "light-path", "commands/build.md", "fan-out",
                 "escalation", "SHOULD-FIX", "latch")

    def setUp(self):
        self.text = (AGENTS_DIR / "architect-reviewer.md").read_text(encoding="utf-8")
        self.frontmatter = _frontmatter_lines(self.text)
        self.assertIsNotNone(self.frontmatter, "no frontmatter block")

    def _field(self, name):
        for line in self.frontmatter:
            if line.startswith(name + ":"):
                return line[len(name) + 1:].strip()
        self.fail("frontmatter has no %s: line" % name)

    def test_frontmatter_identity_and_read_only_tools(self):
        self.assertEqual("architect-reviewer", self._field("name"))
        self.assertEqual("opus", self._field("model"))
        tools = {t.strip() for t in self._field("tools").split(",")}
        self.assertEqual({"Read", "Grep", "Glob", "Bash"}, tools)

    def test_description_says_it_is_not_a_gate(self):
        description = self._field("description")
        self.assertIn("not a /build gate", description)
        self.assertIn("never stands in for Code Review or Merge Review", description)

    def test_body_carries_no_gate_or_light_path_language(self):
        for forbidden in self.FORBIDDEN:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.text)

    def test_no_default_fan_out_names_it(self):
        scanned = []
        tests_dir = SCRIPTS_DIR / "tests"
        candidates = list((REPO / "skills" / "builder").rglob("*"))
        candidates += [p for p in SCRIPTS_DIR.rglob("*") if tests_dir not in p.parents]
        for path in candidates:
            if not path.is_file():
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            scanned.append(path)
            self.assertNotIn("architect-reviewer", body,
                             "%s routes to the optional reviewer" % path)
        self.assertGreater(len(scanned), 5, "the scan found almost nothing")


def _frontmatter_lines(text):
    """Return the raw lines between the two `---` fences of an agent file's
    frontmatter block, or None if it has none."""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        return None
    return match.group(1).splitlines()


class TestAgentFrontmatterIsValidYaml(unittest.TestCase):
    """An agent's frontmatter stops being VALID YAML the moment its unquoted
    `description:` plain scalar gains a literal colon-space (`Exception: `,
    say). A plain scalar cannot contain `": "`; YAML reads that as a
    nested mapping value and errors:

        ruby -ryaml -e 'p YAML.load_file("agents/code-reviewer.md")'
        -> Psych::SyntaxError: mapping values are not allowed in this
           context

    The agents ship in the builder plugin, and a test that regex-matches the
    raw text does not notice. Swept across every file in `agents/`, not just
    `code-reviewer.md`: any agent's description can break the same way, and
    a check hard-coded to one file would not catch a SECOND file breaking
    it."""

    def _agent_files(self):
        files = sorted(AGENTS_DIR.glob("*.md"))
        self.assertTrue(files, "agents/ has no *.md files -- sweep is vacuous")
        return files

    def test_no_unquoted_frontmatter_value_contains_a_colon_space(self):
        """Primary, stdlib-only, always runs regardless of whether PyYAML
        is installed. This is the exact YAML rule a plain scalar violates
        by carrying `": "` -- reproduced directly rather than depending on
        a third-party parser being present."""
        for path in self._agent_files():
            text = path.read_text(encoding="utf-8")
            lines = _frontmatter_lines(text)
            self.assertIsNotNone(
                lines, "%s has no --- frontmatter block" % path.name)
            for line in lines:
                match = re.match(r"^(\w[\w-]*):\s*(.*)$", line)
                if not match:
                    continue
                value = match.group(2)
                if not value or value[0] in ("'", '"'):
                    continue  # quoted scalar: ": " inside it is legal YAML
                self.assertNotIn(
                    ": ", value,
                    "%s frontmatter key %r has an unquoted value "
                    "containing a literal ': ' -- that terminates a YAML "
                    "plain scalar early and breaks the frontmatter as YAML"
                    % (path.name, match.group(1)))

    def test_frontmatter_parses_as_yaml_with_expected_keys(self):
        """Secondary, belt and braces. PyYAML is installed on THIS machine
        but is NOT stdlib and will be absent on a fresh clone and possibly
        CI, so it can never be the only check -- skip, don't fail, on
        ImportError, and the primary structural test above is what must
        still catch the class of bug on a machine without it."""
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed -- covered by the "
                           "stdlib-only structural test above")
        for path in self._agent_files():
            text = path.read_text(encoding="utf-8")
            lines = _frontmatter_lines(text)
            self.assertIsNotNone(
                lines, "%s has no --- frontmatter block" % path.name)
            parsed = yaml.safe_load("\n".join(lines))
            self.assertIsInstance(
                parsed, dict,
                "%s frontmatter did not parse to a mapping" % path.name)
            self.assertIn(
                "name", parsed, "%s frontmatter missing 'name'" % path.name)
            self.assertIn(
                "description", parsed,
                "%s frontmatter missing 'description'" % path.name)


DESIGN_REVIEW_MD = SCRIPTS_DIR / "design-review.md"

# The five design-review lenses, byte for byte. This is the contract
# design-review.md must carry, and this constant is what makes
# "byte-identical" a checkable property rather than a claim.
EXPECTED_DESIGN_LENSES = (
    ('wrong-premise',
     "You are reading a design, not a plan. Every design rests on claims "
     "about how something that already exists behaves — a file's "
     "contents, a tool's flags, a config key, an API's response, what a "
     "command builds and when it builds it. For each such claim, decide "
     "whether the author READ it or REASONED to it, and flag every one "
     "that reads as measured but is not sourced. An inferred premise is "
     "not a defect; an inferred premise dressed as a measured one is. "
     "Name the file or command that would settle each."),
    ('simpler-alternative',
     "Describe the smallest thing that would solve the problem this "
     "design states — not the problem it implies. Then, for each piece "
     "of the design that goes beyond that, name what specifically breaks "
     "if the piece is cut. Anything you cannot name a concrete failure "
     "for is unjustified complexity and is a finding. The house order of "
     "preference is: no new file over a new file, one function over a "
     "framework, a config line over a script, an existing tool over a "
     "built one."),
    ('silent-failure',
     "For each component of this design, answer one question: when it "
     "is wrong, what tells anybody? Name the observer — a crash, a "
     "non-zero exit, a blank render, a human seeing an obviously wrong "
     "answer on first use. Flag every component whose failure produces "
     "a confident wrong answer, or no answer where one was expected, "
     "with nothing downstream complaining. A guard that fails open, a "
     "scheduled job nobody watches, a test that cannot fail, and a "
     "number a human trusts unchecked are all this class."),
    ('already-exists',
     "Search the target repository for something that already does "
     "what this design proposes, in whole or in part, and flag any "
     "duplication you find. Then read the repository's `CLAUDE.md` "
     "\"Decisions not to re-litigate\" section and `docs/decisions.md`, "
     "and flag anything this design re-proposes that has already been "
     "measured and rejected. Quote the decision and its date."),
    ('seams',
     "This design will be called by things that do not exist yet. Name "
     "every seam — what a caller passes in, what it gets back, what "
     "happens when an input is absent or malformed, and how a caller "
     "selects behaviour. Flag any seam described in prose but given no "
     "concrete shape, and any place where the design requires a caller "
     "to know something the design never hands them."),
)


def _parse_design_lens_table(text):
    """Parse the `| `key` | prompt |` rows of design-review.md's
    lens table into (key, prompt) tuples. The first-cell backtick
    anchor keeps this from picking up an unrelated table row elsewhere in
    the file."""
    pattern = re.compile(
        r"^\|\s*`([a-z-]+)`\s*\|\s*(.+?)\s*\|\s*$",
        re.MULTILINE)
    rows = []
    for key, prompt in pattern.findall(text):
        rows.append((key, prompt))
    return rows


class TestDesignReviewLensSet(unittest.TestCase):
    """design-review.md's five lens prompts are the judgement half of the
    Design Review gate, and this class is what makes "byte-identical to the
    contract" a checkable property rather than a claim.
    """

    @classmethod
    def setUpClass(cls):
        if not DESIGN_REVIEW_MD.is_file():
            raise AssertionError(
                "tools/build-lens/design-review.md is missing")
        cls.text = DESIGN_REVIEW_MD.read_text()
        cls.flat = " ".join(cls.text.split())
        cls.rows = _parse_design_lens_table(cls.text)

    def test_all_five_lens_keys_are_present(self):
        found = {key for key, _ in self.rows}
        expected = {key for key, _ in EXPECTED_DESIGN_LENSES}
        self.assertEqual(
            found, expected,
            "design-review.md's lens table does not carry exactly the "
            "five contract keys: found {}, expected {}"
            .format(sorted(found), sorted(expected)))

    def test_each_prompt_is_byte_identical_to_the_contract(self):
        """Compares the PARSED TABLE CELLS against the contract by key, not
        a document-wide substring search. With a substring search, replacing all five table prompts with `INVALID
        PROMPT` and appending the five originals as prose after the table
        left this test green, because the real prompt text still appeared
        *somewhere* in the file -- just not in the table. Whitespace-
        normalized on BOTH sides -- a plain match lies on wrapped prose,
        since a prompt straddling a line break returns nothing on a table
        cell that contains it.

        Also checked here, not just in the sibling key test: a row present
        in one side and absent from the other is a failure in both
        directions, so a table that dropped or added a key fails this test
        too, not only `test_all_five_lens_keys_are_present`.
        """
        by_key = {key: prompt for key, prompt in self.rows}
        expected_keys = {key for key, _ in EXPECTED_DESIGN_LENSES}
        found_keys = set(by_key)
        self.assertEqual(
            found_keys, expected_keys,
            "design-review.md's lens table rows do not carry exactly the "
            "expected keys: found {}, expected {}"
            .format(sorted(found_keys), sorted(expected_keys)))
        for key, prompt in EXPECTED_DESIGN_LENSES:
            expected = " ".join(prompt.split())
            actual = " ".join(by_key[key].split())
            self.assertEqual(
                actual, expected,
                "design-review.md's {!r} TABLE-CELL prompt does not match "
                "the plan's contract, even whitespace-normalized -- the "
                "five lens prompts must be copied byte-for-byte into the "
                "table itself, not merely appear somewhere in the document"
                .format(key))

    def test_the_file_resolves_the_review_mode(self):
        for needle in (
                "claude-build-route --review-mode", "economical", "full"):
            self.assertIn(
                needle, self.flat,
                "design-review.md does not mention {!r} -- it must "
                "resolve the review mode before dispatching"
                .format(needle))

    def test_full_mode_uses_the_wrapper_and_prohibits_plain_codex_exec(self):
        self.assertIn(
            "claude-codex-review --design-review", self.flat,
            "design-review.md never mentions dispatching the wrapper's "
            "design mode under full mode")
        # Scope the negation to the SENTENCE the mention sits in, and match
        # whole words. A +/-150-character window with `"not" in window` is a
        # vacuous guard twice over: the window reaches into unrelated prose,
        # and the substring "not" occurs inside "nothing", "cannot", "note"
        # and "another". With such a window, inserting the literal sentence
        # "Dispatch claude-codex-review for this gate." leaves this test GREEN,
        # which is the exact wrong instruction it exists to forbid.
        sentences = re.split(r"(?<=[.!?:])\s+", self.flat)
        hits = [s for s in sentences if "codex exec" in s]
        self.assertTrue(
            hits,
            "design-review.md no longer prohibits plain `codex exec` at all "
            "-- the sentence that forbids the retired path must stay")
        for sentence in hits:
            self.assertRegex(
                sentence, r"\b(never|not|Do not)\b",
                "`codex exec` appears in a sentence that does not "
                "prohibit it: {!r}".format(sentence))

    def test_independence_is_pointed_at_not_restated(self):
        self.assertIn(
            "`plan-review.md` in the same directory as this file", self.flat,
            "design-review.md does not point at plan-review.md for the "
            "four independence mechanisms")
        for phrase in ("Invert the burden of proof",
                        "Diverse lenses, not more copies",
                        "Effort asymmetry"):
            self.assertNotIn(
                phrase, self.text,
                "design-review.md restates {!r} from plan-review.md's "
                "four mechanisms instead of pointing at it"
                .format(phrase))

    def test_effort_asymmetry_is_numbered_as_the_third_mechanism(self):
        """Effort asymmetry is the THIRD of plan-review.md's independence
        mechanisms. A stray "fourth" here points a reader at a mechanism the
        gate does not use."""
        self.assertIn("collapses the third mechanism", self.flat)
        self.assertNotIn("fourth mechanism", self.flat)



class TestNoLensTableCarriesATierColumn(unittest.TestCase):
    """No lens table carries a `tier` column: one reviewer applies every
    row, so a tier cell would be a staging instruction for a panel of
    reviewers that does not exist -- and a reader will act on it."""

    # A header cell `tier`, or a row cell that IS a tier value. Exact
    # cell equality, so prose that merely mentions a default is untouched.
    TIER_CELLS = {"tier", "**default**", "escalation"}

    def test_no_reference_file_has_a_tier_column(self):
        for path in sorted(SCRIPTS_DIR.glob("*.md")):
            for line in path.read_text().splitlines():
                if not line.lstrip().startswith("|"):
                    continue
                cells = {c.strip() for c in line.strip().strip("|").split("|")}
                self.assertFalse(
                    cells & self.TIER_CELLS,
                    "{} still has a `tier` column or tier cell: {!r}"
                    .format(path.name, line))


class TestPlanReviewReferenceResolvesTheMode(unittest.TestCase):
    """plan-review.md must resolve `claude-build-route --review-mode` and
    route `full` mode through `claude-codex-review --plan-review`, not plain
    `codex exec` with a hand-written prompt: the wrapper takes paths only, so
    a prior round's findings cannot reach the reviewer through it.
    """

    PLAN_REVIEW_MD = SCRIPTS_DIR / "plan-review.md"

    @classmethod
    def setUpClass(cls):
        cls.text = cls.PLAN_REVIEW_MD.read_text()
        cls.flat = " ".join(cls.text.split())

    def test_plan_review_reference_resolves_the_mode(self):
        # The mode-resolution vocabulary is present.
        for needle in (
                "claude-build-route --review-mode", "economical", "full",
                "claude-codex-review --plan-review <plan file> --repo <repo> -o <report path>"):
            self.assertIn(
                needle, self.flat,
                "plan-review.md does not mention {!r} -- it must resolve "
                "the review mode before dispatching".format(needle))
        # The section sits before the lens table it precedes.
        self.assertLess(
            self.text.index("### Which reviewer runs"),
            self.text.index("### Plan Review lenses"),
            "the 'Which reviewer runs' section must appear before "
            "'### Plan Review lenses'")
        # Every sentence mentioning plain `codex exec` prohibits it.
        sentences = re.split(r"(?<=[.!?:])\s+", self.flat)
        self.assertTrue(
            [s for s in sentences if "codex exec" in s],
            "plan-review.md no longer prohibits plain `codex exec` at all "
            "-- the sentence that forbids the retired path must stay")
        for sentence in sentences:
            if "codex exec" in sentence:
                self.assertRegex(
                    sentence, r"\b(never|not|Do not)\b",
                    "plan-review.md mentions `codex exec` in a sentence "
                    "that does not prohibit it: {!r}".format(sentence))


class TestFullModeCodeReviewCarriesThePlan(unittest.TestCase):
    """Code Review's dispatch line carries `--plan` in both
    gate references it appears in, and Merge Review's never does -- the
    wrapper refuses `--plan` together with `--merge-with`, so the reference
    must not show the pair as a valid invocation.
    """

    def test_code_review_row_carries_plan_and_merge_review_row_does_not(self):
        for path in (SCRIPTS_DIR / "code-review.md", SCRIPTS_DIR / "merge-review.md"):
            text = path.read_text()
            code_line = next(
                (line for line in text.splitlines()
                 if line.startswith("| Code Review | `claude-codex-review")),
                None)
            self.assertIsNotNone(
                code_line, "{} has no Code Review row".format(path.name))
            self.assertIn(
                "--plan <plan file>", code_line,
                "{}'s Code Review row does not carry --plan".format(path.name))
            merge_line = next(
                (line for line in text.splitlines()
                 if line.startswith("| Merge Review | `claude-codex-review")),
                None)
            self.assertIsNotNone(
                merge_line, "{} has no Merge Review row".format(path.name))
            self.assertNotIn(
                "--plan", merge_line,
                "{}'s Merge Review row must not carry --plan -- the wrapper "
                "refuses --plan together with --merge-with".format(path.name))

    def test_build_md_names_both_wrapper_plan_flags_and_drops_the_old_name(self):
        text = BUILD_MD.read_text()
        flat = " ".join(text.split())
        self.assertIn("claude-codex-review --plan-review", flat)
        self.assertIn("claude-codex-review --plan <plan file>", flat)
        self.assertNotIn("plan-against-diff", flat)


class TestNoReferenceStrandsAPlanPremiseCitation(unittest.TestCase):
    """An installed reference must not cite a plan's internal premise numbers.

    A plan lives in unversioned scratch and is archived once it ships; these
    references ship in the plugin's `tools/build-lens/` and are read indefinitely.
    A "(P4)" carried out of a plan's prose into a reference therefore points
    at nothing the moment the plan is retired -- the stranded-antecedent
    class this repo has recorded before.

    It arrives as a PLAN defect rather than an executor one: a plan that
    writes "(P4)" inline in the prose it tells executors to write gets it
    transcribed faithfully, into as many files as the plan names. That is
    why this guard globs every reference instead of pinning known lines.
    """

    # Glob rather than REFERENCE_FILES: this must cover any future reference
    # without anyone remembering to register it here.
    PATTERN = re.compile(r"\(P\d+[a-z]?\)")

    def test_no_reference_cites_a_plan_premise_number(self):
        checked = sorted(SCRIPTS_DIR.glob("*-review.md"))
        self.assertGreaterEqual(
            len(checked), 3,
            "fixture premise: expected at least the three original gate "
            "references, found {!r}".format([p.name for p in checked]))
        offenders = {}
        for path in checked:
            hits = self.PATTERN.findall(path.read_text())
            if hits:
                offenders[path.name] = hits
        self.assertFalse(
            offenders,
            "reference file(s) cite plan premise numbers, which strand when "
            "the plan is archived: {!r}".format(offenders))


class TestEachModeResolvingGateCarriesItsOwnBlock(unittest.TestCase):
    """The Plan Review gate must carry its own
    `### Which reviewer runs` subsection, strictly between its own
    `## Gate: Plan Review` heading and `### What this costs` -- and it must
    land there, not in the Design Review gate above it.

    This asserts by GATE BOUNDARY, not by a bare document-wide count of 3:
    a count is satisfied by putting the third heading in the wrong gate
    entirely. A count
    check alone cannot distinguish "one per gate" from "three, all in the
    same gate" -- this test locates each heading against the gate span that
    contains it.
    """

    GATE_HEADING = re.compile(r"^## Gate: .+$", re.M)
    RESOLVER_HEADING = re.compile(r"^### Which reviewer runs.*$", re.M)

    @classmethod
    def setUpClass(cls):
        cls.text = BUILD_MD.read_text()

    def _gate_span(self, gate_heading_text):
        """Return (start, end) for the named gate: from its own heading to
        the next top-level (`## `) heading, or end of file."""
        start = self.text.index(gate_heading_text)
        top_level = re.compile(r"^## .+$", re.M)
        next_headings = [
            m.start() for m in top_level.finditer(self.text)
            if m.start() > start]
        end = min(next_headings) if next_headings else len(self.text)
        return start, end

    def _resolver_headings_in_span(self, start, end):
        return [
            m for m in self.RESOLVER_HEADING.finditer(self.text)
            if start <= m.start() < end]

    def test_each_mode_resolving_gate_carries_its_own_block(self):
        design_start, design_end = self._gate_span("## Gate: Design Review")
        plan_start, plan_end = self._gate_span("## Gate: Plan Review")
        code_start, code_end = self._gate_span("## Gate: Code Review")
        merge_start, merge_end = self._gate_span("## Gate: Merge Review")

        # Gate-scoped: exactly one resolver heading strictly inside the Plan
        # Review gate, and it must NOT be inside the Design Review gate that
        # precedes it (the wrong placement a bare count accepts).
        design_hits = self._resolver_headings_in_span(design_start, design_end)
        plan_hits = self._resolver_headings_in_span(plan_start, plan_end)
        self.assertEqual(
            len(design_hits), 0,
            "the Design Review gate must not carry a 'Which reviewer runs' "
            "block -- found {} inside it".format(len(design_hits)))
        self.assertEqual(
            len(plan_hits), 1,
            "expected exactly one 'Which reviewer runs' heading inside the "
            "Plan Review gate, found {}".format(len(plan_hits)))

        # It must also sit before '### What this costs', within that span.
        cost_index = self.text.index(
            "### What this costs", plan_start, plan_end)
        self.assertLess(
            plan_hits[0].start(), cost_index,
            "the Plan Review gate's 'Which reviewer runs' heading must "
            "come before '### What this costs'")

        # Code Review and Merge Review each carry their own -- confirms the
        # count below is one-per-gate, not three-in-one-gate.
        code_hits = self._resolver_headings_in_span(code_start, code_end)
        merge_hits = self._resolver_headings_in_span(merge_start, merge_end)
        self.assertEqual(
            len(code_hits), 1,
            "expected exactly one 'Which reviewer runs' heading inside the "
            "Code Review gate, found {}".format(len(code_hits)))
        self.assertEqual(
            len(merge_hits), 1,
            "expected exactly one 'Which reviewer runs' heading inside the "
            "Merge Review gate, found {}".format(len(merge_hits)))

        # Exactly three such headings in the whole file.
        all_hits = list(self.RESOLVER_HEADING.finditer(self.text))
        self.assertEqual(
            len(all_hits), 3,
            "expected exactly 3 'Which reviewer runs' headings in "
            "build.md, found {}".format(len(all_hits)))


class TestGateReferenceCountAgreesWithReferenceFiles(unittest.TestCase):
    """`build.md` states the number of gate references in prose ("The four
    gate references ship in the plugin..."), and that number goes stale
    silently -- a prose "three" survives a fourth reference being added. `REFERENCE_FILES` is the count that actually matters; this
    test reads the number WORD out of build.md's prose, maps it to an
    integer, and asserts the two agree -- so the next person to add a fifth
    reference cannot leave the prose stale the same way.
    """

    NUMBER_WORDS = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    }

    def test_the_stated_count_agrees_with_reference_files(self):
        flat = " ".join(BUILD_MD.read_text().split())
        match = re.search(
            r"The (\w+) gate references ship in the plugin", flat)
        self.assertIsNotNone(
            match,
            "build.md no longer states 'The <count> gate references "
            "ship in the plugin' -- this test has nothing to check "
            "against; update the regex if the sentence was reworded")
        word = match.group(1)
        self.assertIn(
            word.lower(), self.NUMBER_WORDS,
            "build.md's gate-reference count {!r} is not a recognised "
            "number word -- add it to NUMBER_WORDS or fix the prose"
            .format(word))
        stated = self.NUMBER_WORDS[word.lower()]
        actual = len(REFERENCE_FILES)
        self.assertEqual(
            stated, actual,
            "build.md states {!r} ({}) gate references, but REFERENCE_FILES "
            "has {} entries -- the prose count is stale"
            .format(word, stated, actual))


BRAINSTORMING_MD = SCRIPTS_DIR / "brainstorming.md"


class TestBrainstormingCarriesThePhase1Process(unittest.TestCase):
    """`brainstorming.md` is the Phase 1 process each classification path
    follows, ported from superpowers 6.3.0 `brainstorming`. The skill points
    at `brainstorming.md` and at its "step 7", so this class pins the
    structure those pointers depend on: the numbered architectural
    steps in order, the design-review handoff at step 7, the `Approved:`
    line at step 8, and the classification header each path writes.

    Every assertion here parses STRUCTURE -- a numbered item, a table row --
    never a whole-document substring search, per this repo's CLAUDE.md
    Testing section.
    """

    EXPECTED_STEPS = (
        "Explore context",
        "Clarifying questions",
        "Two or three approaches",
        "Present the design in sections",
        "Write the design doc",
        "Self-review",
        "Design Review",
        "The user reviews the written spec",
        "Phase 2",
    )

    # The five quoted thoughts from the Red Flags table, in order. Straight
    # ASCII apostrophes throughout.
    EXPECTED_RED_FLAGS = (
        "It's bounded and the design is obvious — I'll start while "
        "they read it",
        "I understand this kind of app, so it's bounded",
        "It grew but I'm almost done",
        "They approved the spike so the follow-up is approved",
        "The spike works, so I'll keep the code",
    )

    ITEM_PATTERN = re.compile(r"^(\d+)\. \*\*(.+?)\*\*")

    @classmethod
    def setUpClass(cls):
        if not BRAINSTORMING_MD.is_file():
            raise AssertionError(
                "tools/build-lens/brainstorming.md is missing")
        cls.text = BRAINSTORMING_MD.read_text()

    @staticmethod
    def _normalize(text):
        return " ".join(text.split())

    def _section(self, heading):
        """Lines from `heading` (exact match) to the next line starting
        `## `, or the end of the file."""
        lines = self.text.splitlines()
        start = None
        end = len(lines)
        for i, line in enumerate(lines):
            if start is None:
                if line.strip() == heading:
                    start = i + 1
                continue
            if line.startswith("## "):
                end = i
                break
        self.assertIsNotNone(
            start, "brainstorming.md has no {!r} section".format(heading))
        return lines[start:end]

    def _architectural_items(self):
        """[(number, bold lead, body-lines-including-the-item-line), ...]
        from `## Architectural path`. An item's body runs to the next line
        matching ITEM_PATTERN, not just the next numbered-looking line."""
        section = self._section("## Architectural path")
        items = []
        current = None
        for line in section:
            match = self.ITEM_PATTERN.match(line)
            if match:
                if current is not None:
                    items.append(current)
                current = [int(match.group(1)), match.group(2), [line]]
            elif current is not None:
                current[2].append(line)
        if current is not None:
            items.append(current)
        return items

    def _step_body(self, step_number):
        for number, _lead, body in self._architectural_items():
            if number == step_number:
                return self._normalize("\n".join(body))
        raise AssertionError(
            "no step {} found in ## Architectural path".format(step_number))

    def test_architectural_path_has_nine_steps_in_order_with_the_right_leads(self):
        items = self._architectural_items()
        numbers = [number for number, _lead, _body in items]
        self.assertEqual(
            numbers, list(range(1, 10)),
            "## Architectural path's numbered items are not exactly 1-9 in "
            "order: {}".format(numbers))
        leads = [lead for _number, lead, _body in items]
        self.assertEqual(
            leads, list(self.EXPECTED_STEPS),
            "## Architectural path's bold leads do not match the expected "
            "step names in order: {}".format(leads))

    def test_step_5_states_no_review_findings_in_the_design_doc(self):
        self.assertIn(
            "No review findings", self._step_body(5),
            "step 5's body no longer states 'No review findings' -- the "
            "design doc must stay free of adjudication")

    def test_step_7_points_at_the_sibling_design_review(self):
        self.assertIn(
            "`design-review.md` in the same directory as this file",
            self._step_body(7),
            "step 7 no longer points at design-review.md in the same "
            "directory as brainstorming.md")

    def test_step_8_carries_the_approved_line_and_the_material_change_clause(self):
        body = self._step_body(8)
        self.assertIn(
            "`Approved: YYYY-MM-DD`", body,
            "step 8 no longer carries the `Approved: YYYY-MM-DD` line")
        self.assertIn(
            "re-runs step 7 once", body,
            "step 8 no longer states that a material change re-runs step "
            "7 once before asking again")

    def test_step_8_asks_for_approval_in_the_final_message_not_a_tool_call(self):
        """The approval message is a link, ~2 sentences and one callout
        table, written in the turn's final text message. A design stuffed into
        an `AskUserQuestion` preview collapses out of view, so step 8 must not
        route through it at all."""
        body = self._step_body(8)
        for term in ("final text message", "as a link", "one table"):
            self.assertIn(
                term, body,
                "step 8 no longer names {!r} in the approval message".format(
                    term))
        self.assertNotIn(
            "AskUserQuestion", body,
            "step 8 routes the design approval through AskUserQuestion "
            "again -- its content collapses out of view in VS Code")

    def test_step_9_names_the_architectural_classification(self):
        self.assertIn(
            "`Classification: architectural`", self._step_body(9),
            "step 9 no longer names Classification: architectural")

    def test_bounded_path_carries_its_classification_and_the_stop_clause(self):
        section = self._normalize(
            "\n".join(self._section("## Bounded path")))
        self.assertIn(
            "`Classification: bounded`", section,
            "## Bounded path no longer carries Classification: bounded")
        self.assertIn(
            "stop until the user says yes", section,
            "## Bounded path no longer states the stop-until-yes gate")
        self.assertIn(
            "final text message", section,
            "## Bounded path no longer puts the short design in the turn's "
            "final text message")

    def _red_flags_data_rows(self):
        """Table rows under `## Red Flags`, header and separator dropped."""
        section = self._section("## Red Flags")
        rows = []
        for line in section:
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            rows.append(cells)
        self.assertGreater(
            len(rows), 2, "## Red Flags has no table to parse")
        return rows[2:]

    def test_red_flags_first_cells_match_the_five_quoted_thoughts_in_order(self):
        rows = self._red_flags_data_rows()
        first_cells = [row[0].strip('"') for row in rows]
        self.assertEqual(
            first_cells, list(self.EXPECTED_RED_FLAGS),
            "## Red Flags table's first cells do not match the expected "
            "five quoted thoughts in order: {}".format(first_cells))

    def test_the_closeout_format_rule_sits_in_the_intro_and_covers_every_path(self):
        """Phase 1 messages read like the closeout. The rule governs all three paths,
        so it must be an intro paragraph -- before the first `## ` heading --
        not inside one path's section, where the other two never read it."""
        intro = []
        for line in self.text.splitlines():
            if line.startswith("## "):
                break
            intro.append(line)
        paragraphs = [self._normalize(p)
                      for p in "\n".join(intro).split("\n\n")]
        lead = "**Every message in this phase reads like the closeout.**"
        matches = [p for p in paragraphs if p.startswith(lead)]
        self.assertEqual(
            len(matches), 1,
            "brainstorming.md's intro has no paragraph opening {!r}".format(
                lead))
        for term in ("table", "three lines", "`AskUserQuestion`",
                     "final text message"):
            self.assertIn(
                term, matches[0],
                "the closeout-format rule no longer names {!r}".format(term))

    def test_brainstorming_md_is_not_a_reference_file(self):
        self.assertNotIn(
            BRAINSTORMING_MD, REFERENCE_FILES,
            "brainstorming.md is the architect's own Phase 1 process, not "
            "a gate reference -- it must stay out of REFERENCE_FILES, like "
            "merge-review-report.schema.json")


class TestPhase1AndDesignReviewKeyOnClassification(unittest.TestCase):
    """Phase 1's skip clause and the Design Review gate key on the work's
    classification (bounded/architectural/spike), not on build SIZE ("large
    builds only", "roughly six tasks"): Phase 1 skips only on a design file
    whose front matter is actually `Approved:`, and Design Review runs on
    every architectural build rather than being staged by task count.

    Every assertion here parses STRUCTURE -- a section between headings, a
    table row -- never a whole-document substring search, per this repo's
    CLAUDE.md Testing section.
    """

    @classmethod
    def setUpClass(cls):
        cls.text = BUILD_MD.read_text()
        if not DESIGN_REVIEW_MD.is_file():
            raise AssertionError(
                "tools/build-lens/design-review.md is missing")
        cls.design_review_text = DESIGN_REVIEW_MD.read_text()

    @staticmethod
    def _normalize(text):
        return " ".join(text.split())

    def _section(self, heading, exact=True):
        """Lines from a heading to the next line starting `## `, or EOF.
        `exact=True` matches the whole stripped line; otherwise the
        stripped line only needs to START WITH `heading` -- the Design
        Review gate's heading text changes under this same task."""
        lines = self.text.splitlines()
        start = None
        end = len(lines)
        for i, line in enumerate(lines):
            if start is None:
                stripped = line.strip()
                matched = (
                    stripped == heading if exact
                    else stripped.startswith(heading))
                if matched:
                    self.heading_line = stripped
                    start = i + 1
                continue
            if line.startswith("## "):
                end = i
                break
        self.assertIsNotNone(
            start,
            "build.md has no section matching {!r}".format(heading))
        return lines[start:end]

    @staticmethod
    def _first_paragraph(lines):
        para = []
        for line in lines:
            if line.strip() == "":
                if para:
                    break
                continue
            para.append(line)
        return para

    def _table_rows(self, header_row):
        """Data rows of the table whose exact header line is `header_row`,
        cells stripped, separator row dropped."""
        lines = self.text.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == header_row:
                rows = []
                for data_line in lines[i + 2:]:
                    stripped = data_line.strip()
                    if not stripped.startswith("|"):
                        break
                    cells = [
                        c.strip() for c in stripped.strip("|").split("|")]
                    rows.append(cells)
                return rows
        self.fail(
            "build.md has no table with header row {!r}".format(header_row))

    def test_phase_1_skip_clause_names_approved_and_points_at_brainstorming(self):
        section = self._section("## Phase 1 — design (you, Opus)")
        first_para = self._normalize(
            "\n".join(self._first_paragraph(section)))
        self.assertIn(
            "Approved:", first_para,
            "Phase 1's first paragraph no longer names an `Approved:` "
            "line as the skip condition")
        self.assertIn(
            "handover", first_para,
            "Phase 1's first paragraph no longer rules out a handover as "
            "an approved design")
        self.assertIn(
            "`${CLAUDE_PLUGIN_ROOT}/tools/build-lens/brainstorming.md`",
            self._normalize("\n".join(section)),
            "Phase 1 no longer points at the plugin's "
            "brainstorming.md process")
        self.assertNotIn(
            "If this work already has an approved design, skip this "
            "phase", self.text,
            "build.md still carries the old unconditional "
            "skip-this-phase sentence, which skipped on any design "
            "regardless of approval")

    def test_design_review_gate_keys_on_architectural_not_task_count(self):
        section = self._section("## Gate: Design Review", exact=False)
        self.assertIn(
            "(architectural builds)", self.heading_line,
            "the Design Review gate's heading no longer marks it "
            "(architectural builds)")
        section_flat = self._normalize("\n".join(section))
        self.assertNotIn(
            "six tasks", section_flat,
            "the Design Review gate still gestures at task count "
            "(\"six tasks\") instead of classification")
        self.assertNotIn(
            "~6", section_flat,
            "the Design Review gate still gestures at task count "
            "(\"~6\") instead of classification")

    def test_four_column_dosage_table_marks_design_review_architectural_only(self):
        rows = self._table_rows(
            "| | Design Review | Plan Review | Code Review | Merge Review |")
        self.assertEqual(
            len(rows), 2,
            "the four-column dosage table does not have exactly two data "
            "rows (Loud, Silent)")
        for row in rows:
            self.assertEqual(
                row[1], "architectural only",
                "the four-column dosage table's Design Review cell for "
                "{!r} is not exactly 'architectural only': {!r}"
                .format(row[0], row[1]))

    def test_five_column_table_marks_design_review_architectural_every_row(self):
        rows = self._table_rows(
            "| | Design Review | Plan Review | Progress Check | Code "
            "Review | Merge Review |")
        self.assertEqual(
            len(rows), 4,
            "the five-column gates table does not have exactly four data "
            "rows")
        design_review_cells = [row[1] for row in rows]
        self.assertEqual(
            design_review_cells, ["architectural"] * 4,
            "the five-column gates table's Design Review column is not "
            "'architectural' on every row: {}".format(design_review_cells))
        progress_check_cells = [row[3] for row in rows]
        self.assertEqual(
            progress_check_cells, ["skip", "run", "skip", "run"],
            "the five-column gates table's Progress Check column changed "
            "-- it must stay skip, run, skip, run in row order")

    def test_phase_2_states_plan_review_refuses_a_plan_without_the_header(self):
        section = self._section("## Phase 2 — write the plan (you, Opus)")
        section_flat = self._normalize("\n".join(section))
        self.assertIn(
            "Plan Review refuses a plan without them", section_flat,
            "Phase 2 no longer states that Plan Review refuses a plan "
            "missing its Classification/Design header")

    def test_design_review_md_points_at_brainstorming_step_7(self):
        lines = self.design_review_text.splitlines()
        start = None
        for i, line in enumerate(lines):
            if line.strip() == "### What this gate reviews, and when":
                start = i + 1
                break
        self.assertIsNotNone(
            start,
            "design-review.md has no '### What this gate reviews, and "
            "when' heading")
        para = self._normalize(
            "\n".join(self._first_paragraph(lines[start:])))
        self.assertIn(
            "`brainstorming.md` in the same directory as this file", para,
            "design-review.md's first paragraph no longer points at "
            "brainstorming.md")
        self.assertNotIn(
            "six tasks", para,
            "design-review.md's first paragraph still gestures at task "
            "count (\"six tasks\") instead of classification")


class TestDecisionMessagesFollowOneShape(unittest.TestCase):
    """Phase 1's approval message has a shape, and every other place
    `/build` asks the user to decide gets the same one. VS Code collapses text written before a tool call, so a
    content-heavy decision has to live in the turn's final message.

    Every assertion is scoped to one parsed section, so moving the text to a
    neighbouring phase fails it, per this repo's CLAUDE.md Testing section.
    """

    NUMBER_WORDS = {"six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
    PHASE_6_PARTS = ("Built", "Gates", "Rulings", "Left out",
                     "Changed how it works?")

    @classmethod
    def setUpClass(cls):
        cls.text = BUILD_MD.read_text()

    def _section(self, heading_prefix):
        lines = self.text.splitlines()
        start = None
        for i, line in enumerate(lines):
            if start is None:
                if line.startswith(heading_prefix):
                    start = i + 1
                continue
            if line.startswith("## "):
                return lines[start:i]
        self.assertIsNotNone(
            start, "build.md has no section starting {!r}".format(
                heading_prefix))
        return lines[start:]

    def _obligations(self):
        """[(number, normalised item text)] from the standing obligations."""
        items = []
        for line in self._section("## Standing obligations"):
            match = re.match(r"^(\d+)\. (.*)", line)
            if match:
                items.append([int(match.group(1)), match.group(2)])
            elif items and line.startswith("   "):
                items[-1][1] += " " + line.strip()
        return items

    def test_the_stated_obligation_count_matches_the_list(self):
        section = " ".join(self._section("## Standing obligations"))
        match = re.search(r"these (\w+) recur all build", section)
        self.assertIsNotNone(
            match, "the obligations no longer state how many recur")
        stated = self.NUMBER_WORDS.get(match.group(1))
        numbers = [number for number, _text in self._obligations()]
        self.assertEqual(
            numbers, list(range(1, len(numbers) + 1)),
            "the obligations are not numbered 1..n in order: {}".format(
                numbers))
        self.assertEqual(
            stated, len(numbers),
            "build.md says {!r} obligations recur but lists {}".format(
                match.group(1), len(numbers)))

    def test_an_obligation_routes_every_decision_through_the_message_rule(self):
        matches = [text for _number, text in self._obligations()
                   if text.startswith("**Every message asking the user to "
                                      "decide follows the message rule")]
        self.assertEqual(
            len(matches), 1,
            "no standing obligation routes decision messages through the "
            "message rule")
        for term in ("`brainstorming.md`", "Phase 1", "mid-build", "Phase 6"):
            self.assertIn(
                term, matches[0],
                "the message-rule obligation no longer names {!r}".format(
                    term))

    def test_phase_6_is_one_shaped_report_ending_on_the_ship_call(self):
        section = self._section("## Phase 6")
        rows = [[c.strip() for c in line.strip().strip("|").split("|")]
                for line in section if line.strip().startswith("|")]
        self.assertGreater(len(rows), 2, "Phase 6 has no report table")
        parts = [row[0] for row in rows[2:]]
        self.assertEqual(
            parts, list(self.PHASE_6_PARTS),
            "Phase 6's report table rows are not {}: {}".format(
                self.PHASE_6_PARTS, parts))
        gates = dict((row[0], row[1]) for row in rows[2:])["Gates"]
        self.assertIn(
            "`Quote:`", gates,
            "Phase 6's Gates row no longer reads back each gate's quote")
        prose = " ".join(" ".join(section).split())
        self.assertIn(
            "final text message", prose,
            "Phase 6 no longer puts its report in the turn's final message")

    def test_the_quote_is_ledgered_for_phase_6(self):
        ledger = self._section("## Phase 3")
        fence = "\n".join(ledger)
        block = fence[fence.find("```"):fence.find("```", fence.find("```") + 3)]
        self.assertRegex(
            block, r"(?m)^Quote: ",
            "the ledger format no longer carries a `Quote:` line")
        quote = " ".join(" ".join(
            self._section("## Gate: Plan Review")).split())
        self.assertIn(
            "add it to the ledger as a `Quote:` line", quote,
            "the pre-dispatch quote is no longer ledgered for Phase 6")


class TestNoNamedApprover(unittest.TestCase):
    """`/build` and its lens files never name the approver.

    Whoever runs `/build` approves its gates, so a named approver tells that
    user's session to wait for someone who is not there. Nothing may carry a
    `(Name, YYYY-MM-DD)` provenance mark, and nothing may use a gendered
    pronoun for the approver.
    """

    @staticmethod
    def _files():
        paths = [BUILD_MD, SCRIPTS_DIR / "brainstorming.md"]
        paths += [p for p in REFERENCE_FILES if p.is_file()]
        return paths

    PROVENANCE = re.compile(r"\([A-Z][a-z]+, \d{4}-\d{2}-\d{2}")

    def test_no_provenance_mark_names_a_person(self):
        for path in self._files():
            for number, line in enumerate(
                    path.read_text().splitlines(), start=1):
                self.assertIsNone(
                    self.PROVENANCE.search(line),
                    "{}:{} carries a `(Name, date)` provenance mark".format(
                        path.name, number))

    def test_positive_control_a_provenance_mark_is_caught(self):
        self.assertIsNotNone(
            self.PROVENANCE.search("one reviewer per gate (Alex, 2001-01-02)."))

    def test_no_gendered_pronoun_for_the_approver(self):
        for path in self._files():
            for number, line in enumerate(
                    path.read_text().splitlines(), start=1):
                self.assertIsNone(
                    re.search(r"\b(he|his|him)\b", line),
                    "{}:{} uses a gendered pronoun".format(path.name, number))


AGENT_LEAD = "**If `Workflow` is not in your tools**"


def _heading_sections(text):
    """[(heading line, [body lines])] for every `##`/`###` heading, with the
    text before the first heading under the heading ""."""
    sections = [("", [])]
    for line in text.splitlines():
        if line.startswith("## ") or line.startswith("### "):
            sections.append((line, []))
        else:
            sections[-1][1].append(line)
    return sections


def agent_block_problems(text, gate):
    """What is missing from a gate reference's Agent dispatch, judged inside
    the one section that holds its `Workflow(` block. [] means complete.

    The block runs from its bold lead to the next heading. Scoping every
    check to that section is what tells "the Agent dispatch is where the
    reader dispatches the Workflow" from "it is somewhere in the file".
    """
    holding = [body for _, body in _heading_sections(text)
               if any("Workflow(" in line for line in body)]
    if len(holding) != 1:
        return ["expected one section with a Workflow( block, found %d" % len(holding)]
    body = holding[0]
    starts = [i for i, line in enumerate(body) if line.startswith(AGENT_LEAD)]
    if len(starts) != 1:
        return ["expected one paragraph led by %s in the Workflow section, found %d"
                % (AGENT_LEAD, len(starts))]
    block = "\n".join(body[starts[0]:])
    flat = " ".join(block.split())
    problems = []
    required = [
        "never skip the gate", "never review",
        # Row 94: Workflow refuses a scriptPath outside the working
        # directories (an installed plugin's), and its error suggests
        # inlining the script. The refusal must route here, and the
        # suggestion must be named and declined, at every gate.
        "is refused or fails before a reviewer runs",
        "never pass the script inline",
        "the report is the call's return value",
        "claude-review-gate prompt --gate %s" % gate,
        "Agent(", 'model: "opus"', 'effort: "max"',
        "claude-review-gate check --report",
        '"enableWorkflows": true',
        "%s-report-<attempt>.json" % gate, "incremented for every retry",
    ]
    for needle in required:
        if needle not in flat:
            problems.append("missing %r" % needle)
    prompt_lines = [line for line in block.splitlines()
                    if "claude-review-gate prompt" in line]
    has_plan = any("--plan" in line for line in prompt_lines)
    if has_plan != (gate == "code"):
        problems.append("--plan must appear on the prompt line only at gate code")
    agent_type = 'subagent_type: "toolkit-build:code-reviewer"'
    if gate in ("code", "merge"):
        if agent_type not in flat:
            problems.append("missing %r" % agent_type)
    elif "subagent_type" in flat:
        problems.append("gate %s names a subagent_type; it uses a plain agent" % gate)
    return problems


class TestEveryGateCarriesBothRuntimes(unittest.TestCase):
    """Workflow is absent on Pro plans by default and can be removed by a
    setting, an environment variable, a server flag or an org policy. Each
    gate reference must carry the Agent dispatch beside its Workflow one, or
    a reader without Workflow has a gate that cannot run."""

    def _assert_complete(self, gate):
        text = (SCRIPTS_DIR / ("%s-review.md" % gate)).read_text()
        self.assertEqual(agent_block_problems(text, gate), [])

    def test_design(self):
        self._assert_complete("design")

    def test_plan(self):
        self._assert_complete("plan")

    def test_code(self):
        self._assert_complete("code")

    def test_merge(self):
        self._assert_complete("merge")

    def test_positive_control_a_moved_agent_block_fails(self):
        text = (SCRIPTS_DIR / "plan-review.md").read_text()
        self.assertEqual(agent_block_problems(text, "plan"), [])
        lines = text.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith(AGENT_LEAD))
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].startswith(("## ", "### "))), len(lines))
        moved = lines[:start] + lines[end:] + ["", "### Elsewhere", ""] + lines[start:end]
        self.assertNotEqual(agent_block_problems("\n".join(moved), "plan"), [])

    def test_positive_control_a_missing_rule_fails(self):
        text = (SCRIPTS_DIR / "code-review.md").read_text()
        self.assertEqual(agent_block_problems(text, "code"), [])
        # The phrase wraps across a line break in the file, so match it the
        # way the check reads it: across any whitespace.
        mutated, count = re.subn(r"never\s+skip\s+the\s+gate", "skip", text)
        self.assertEqual(count, 1)
        self.assertNotEqual(agent_block_problems(mutated, "code"), [])


class TestTheRuntimeIsLedgered(unittest.TestCase):
    """Under Workflow the runtime refuses a bad report itself; under Agent the
    refusal is `claude-review-gate check`, which the architect has to run. The
    ledger is where a skipped check, or a gate that silently fell back, shows."""

    @classmethod
    def setUpClass(cls):
        cls.sections = _heading_sections(BUILD_MD.read_text())

    def _section(self, prefix):
        found = [body for heading, body in self.sections
                 if heading.startswith(prefix)]
        self.assertEqual(len(found), 1, prefix)
        return found[0]

    def test_phase_0_notes_the_expected_runtime(self):
        flat = " ".join(" ".join(self._section("## Phase 0")).split())
        self.assertIn("expected runtime", flat)
        self.assertIn("`workflow`", flat)
        self.assertIn("`agent`", flat)

    def test_the_quote_and_snapshot_lines_carry_the_runtime(self):
        body = self._section("### The Build Ledger")
        quote = [line for line in body if line.startswith("Quote:")]
        snapshot = [line for line in body if line.startswith("Snapshot:")]
        self.assertEqual(len(quote), 1)
        self.assertEqual(len(snapshot), 1)
        self.assertIn("| <runtime>", quote[0])
        self.assertIn("| <runtime>", snapshot[0])

    def test_the_actual_runtime_and_a_fall_back_are_written(self):
        flat = " ".join(" ".join(self._section("### The Build Ledger")).split())
        self.assertIn("runtime that actually ran", flat)
        self.assertIn("Workflow refusal or absence falls back to `agent`", flat)
        self.assertIn("never inline", flat)
        self.assertIn("`Ruling:`", flat)
        self.assertIn("`accepted:` line", flat)


class TestAgentRetryUsesANewReportPath(unittest.TestCase):
    """`claude-review-gate prompt` refuses an existing report path, so a retry
    after a refused report needs a new one; reusing or deleting the old one
    would let a stale report stand in for the retry's."""

    def test_every_reference_numbers_its_attempts(self):
        for gate in ("design", "plan", "code", "merge"):
            with self.subTest(gate=gate):
                text = (SCRIPTS_DIR / ("%s-review.md" % gate)).read_text()
                problems = agent_block_problems(text, gate)
                # The block must exist for the retry wording to be judged.
                self.assertFalse(
                    [p for p in problems if p.startswith("expected one")], problems)
                self.assertEqual(
                    [p for p in problems if "report-<attempt>" in p or "retry" in p], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
