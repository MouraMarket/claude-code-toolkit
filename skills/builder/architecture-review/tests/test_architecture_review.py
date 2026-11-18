#!/usr/bin/env python3
"""Tests for the architecture-review skill's bundle and its wiring into /build.

Every test reads the files it needs itself and fails via an assertion when one
is missing -- nothing relies on setUp, so a missing file is a FAILURE, never an
ERROR or a skip.

Step checks are scoped to the step's OWN text (from its `N. **` prefix to the
next step's), not the whole document: a phrase moved to the wrong step must
fail, which a whole-document search cannot see.
"""

import importlib.machinery
import importlib.util
import os
import re
import sys
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(SKILL_DIR)))
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
TEMPLATE_MD = os.path.join(SKILL_DIR, "references", "brief-template.md")
TRIGGER_CASES = os.path.join(SKILL_DIR, "tests", "trigger-cases.json")
WRAPPER = os.path.join(REPO, "tools", "codex-review", "claude-codex-review")
FIXED_BRIEF = os.path.join(REPO, "tools", "codex-review", "architecture-brief.md")
SCHEMA = os.path.join(REPO, "tools", "build-lens",
                      "architecture-review-report.schema.json")
AGENT = os.path.join(REPO, "agents", "architecture-reviewer.md")
BUILD_SKILL = os.path.join(REPO, "skills", "builder", "build", "SKILL.md")
BRAINSTORMING = os.path.join(REPO, "tools", "build-lens", "brainstorming.md")
DESIGN_REVIEW = os.path.join(REPO, "tools", "build-lens", "design-review.md")
SKILL_TRIGGER_LIB = os.path.join(REPO, "tools", "skill-trigger",
                                 "skill_trigger_lib.py")

LENSES = ("coherence", "elegance", "rebuild", "unaccounted", "wrong-premise",
          "simpler-alternative", "silent-failure", "already-exists", "seams")
SCHEMA_PROPERTIES = (
    "status", "round", "moment", "context_requests", "lens_verdicts",
    "principles", "changes", "implementation_sketch", "unverified", "executed",
    "id", "question", "why_needed", "lens", "verdict", "evidence", "statement",
    "why", "title", "kind", "what", "serves_objective", "preserve",
    "done_when", "size", "ran", "commands", "why_not")

# The content each numbered step must carry, inside its own text.
STEP_CONTENT = {
    0: ('toolkit_config.py" specs-dir', "Exit 3"),
    2: ("skill-prompt-optimizer:prompt-optimizer", "verbatim"),
    3: ("references/brief-template.md", "--check-brief"),
    4: ("approve", "Nothing is dispatched before this approval"),
    5: ("AskUserQuestion", "Astra", "Fable", "unmeasured"),
    6: ("--usage", "Exit 3", "claude-usage-anchor", "Account & Usage",
        "estimator is off", "weekly", "5-hour figure only"),
    7: ("--architecture-review", "toolkit-build:architecture-reviewer",
        "--validate-report"),
    8: ("at most two", "--resume", "SendMessage", "Evidence only"),
    9: ("confirmed", "refuted", "unchecked", "cannot back a binding"),
    10: ("Design moment", "Final moment", "backlog row"),
}

STEP_RE = re.compile(r"^(\d+)\. \*\*", re.MULTILINE)


def _read(test, path):
    test.assertTrue(os.path.isfile(path), "missing: %s" % path)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _frontmatter(text):
    """The `key: value` lines between the opening `---` and the next `---`.
    One-line values only -- every frontmatter this file reads uses them."""
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        return None
    fields = {}
    for line in lines[1:]:
        if line == "---":
            return fields
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return None


def _section(text, heading):
    """The text under `heading` up to the next `## ` heading, or None."""
    start = text.find(heading + "\n")
    if start < 0:
        return None
    body = text[start + len(heading) + 1:]
    nxt = body.find("\n## ")
    return body if nxt < 0 else body[:nxt]


def _steps(procedure):
    """{step number: that step's own text}."""
    marks = [(int(m.group(1)), m.start()) for m in STEP_RE.finditer(procedure)]
    steps = {}
    for i, (num, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(procedure)
        steps[num] = procedure[start:end]
    return steps


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: a module defining a dataclass (skill_trigger_lib
    # does) looks itself up in sys.modules while the class is built.
    sys.modules[name] = module
    loader.exec_module(module)
    return module


class TestSkillShape(unittest.TestCase):
    def test_under_500_lines(self):
        self.assertLess(len(_read(self, SKILL_MD).splitlines()), 500)

    def test_frontmatter_name_and_description(self):
        fields = _frontmatter(_read(self, SKILL_MD))
        self.assertIsNotNone(fields, "SKILL.md has no parseable frontmatter")
        self.assertEqual(fields.get("name"), "architecture-review")
        description = fields.get("description", "")
        self.assertTrue(description, "empty description")
        self.assertLessEqual(len(description), 1024)
        self.assertIn("Do NOT use", description)

    def test_template_exists_and_is_named(self):
        _read(self, TEMPLATE_MD)
        self.assertIn("references/brief-template.md", _read(self, SKILL_MD))


class TestSkillSteps(unittest.TestCase):
    def _steps(self):
        procedure = _section(_read(self, SKILL_MD), "## The procedure")
        self.assertIsNotNone(procedure, "no '## The procedure' section")
        return _steps(procedure)

    def test_steps_zero_to_ten_present(self):
        self.assertEqual(sorted(self._steps()), list(range(11)))

    def test_each_step_carries_its_content(self):
        steps = self._steps()
        for num, phrases in STEP_CONTENT.items():
            self.assertIn(num, steps, "step %d missing" % num)
            for phrase in phrases:
                self.assertIn(phrase, steps[num],
                              "step %d lacks %r" % (num, phrase))

    def test_three_layer_table(self):
        section = _section(_read(self, SKILL_MD), "## How Opus uses the result")
        self.assertIsNotNone(section, "no '## How Opus uses the result'")
        rows = [line for line in section.splitlines() if line.startswith("| ")]
        firsts = [row.split("|")[1].strip() for row in rows]
        for layer in ("Principles", "Change plan", "Implementation sketch"):
            self.assertIn(layer, firsts, "table row %r missing" % layer)


class TestSkillFlagsExist(unittest.TestCase):
    def test_every_named_flag_is_a_wrapper_option(self):
        text = _read(self, SKILL_MD)
        self.assertTrue(os.path.isfile(WRAPPER), "missing: %s" % WRAPPER)
        wrapper = _load("claude_codex_review_under_test", WRAPPER)
        options = set()
        for action in wrapper.build_parser()._actions:
            options.update(action.option_strings)
        named = set()
        # A flag call can wrap onto the next line, so read each line that
        # names the wrapper together with the line after it.
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if "claude-codex-review" in line:
                window = line + " " + (lines[i + 1] if i + 1 < len(lines) else "")
                named.update(re.findall(r"(?<![\w-])(--[a-z][a-z-]*)", window))
        self.assertTrue(named, "SKILL.md names no claude-codex-review flags")
        self.assertEqual(sorted(named - options), [],
                         "flags SKILL.md uses that the wrapper lacks")


class TestReviewerContract(unittest.TestCase):
    def test_fixed_brief(self):
        text = _read(self, FIXED_BRIEF)
        self.assertEqual(text.count("{DESIGN_LENS_FILE}"), 1)
        for name in SCHEMA_PROPERTIES:
            self.assertIn("`%s`" % name, text, "property %r unnamed" % name)
        for lens in LENSES:
            self.assertIn("`%s`" % lens, text, "lens %r unnamed" % lens)

    def test_agent_is_read_only_fable_at_max(self):
        fields = _frontmatter(_read(self, AGENT))
        self.assertIsNotNone(fields, "agent has no parseable frontmatter")
        self.assertEqual(fields.get("name"), "architecture-reviewer")
        tools = {t.strip() for t in fields.get("tools", "").split(",")}
        self.assertTrue(tools - {""}, "agent lists no tools")
        for banned in ("Bash", "Write", "Edit", "NotebookEdit"):
            self.assertNotIn(banned, tools)
        self.assertEqual(fields.get("model"), "fable")
        self.assertEqual(fields.get("effort"), "max")


class TestBuildWiring(unittest.TestCase):
    def test_design_review_gate_names_the_branch(self):
        # Scoped to the gate's own section: a whole-file search passed while
        # the gate itself still read as unconditional (Code Review finding).
        gate = _section(_read(self, BUILD_SKILL),
                        "## Gate: Design Review — the approach, before a plan "
                        "exists (architectural builds)")
        self.assertIsNotNone(gate, "no Design Review gate section")
        self.assertIn("architecture-review", gate)

    def test_phase_6_offers_the_final_review(self):
        phase6 = _section(_read(self, BUILD_SKILL), "## Phase 6 — gate")
        self.assertIsNotNone(phase6, "no Phase 6 section")
        self.assertIn("Architecture review: yes", phase6)
        self.assertIn("architecture-review", phase6)
        # Evaluated before merge, so it must count the row THIS PR closes
        # (Merge Review finding): "backlog empty" alone never fires.
        self.assertIn("this PR empties", phase6)

    def test_brainstorming_step_7_branches(self):
        text = _read(self, BRAINSTORMING)
        start = text.find("\n7. ")
        end = text.find("\n8. ", start)
        self.assertGreaterEqual(start, 0, "no step 7")
        self.assertGreater(end, start, "no step 8 after step 7")
        self.assertIn("architecture-review", text[start:end])

    def test_brainstorming_step_7_asks_before_dispatching(self):
        # The ask decides WHICH review runs, so it must come before the
        # Design Review dispatch, and `yes` must say Design Review is skipped
        # (Code Review finding: in the old order a `yes` paid for both).
        text = _read(self, BRAINSTORMING)
        start = text.find("\n7. ")
        step7 = " ".join(text[start:text.find("\n8. ", start)].split())
        ask = step7.find("ask once")
        dispatch = step7.find("`design-review.md`")
        self.assertGreaterEqual(ask, 0, "no ask in step 7")
        self.assertGreater(dispatch, ask, "Design Review dispatch precedes the ask")
        self.assertIn("do **not** dispatch Design Review", step7)

    def test_design_review_names_it(self):
        text = _read(self, DESIGN_REVIEW)
        self.assertIn("architecture-review", text)
        # The skip covers the reviewed revision only; a material change still
        # runs this gate (Merge Review finding).
        self.assertIn("material change", text)


class TestTriggerCases(unittest.TestCase):
    def test_loads_with_one_positive_control(self):
        _read(self, TRIGGER_CASES)
        lib = _load("skill_trigger_lib_under_test", SKILL_TRIGGER_LIB)
        cases = lib.load_cases(TRIGGER_CASES)
        controls = [c for c in cases if c["control"] == "positive"]
        self.assertEqual(len(controls), 1)
        self.assertTrue(any(c["expect"] == "toolkit-build:architecture-review"
                            for c in cases))
        self.assertTrue(any(c["forbid"] == "toolkit-build:architecture-review"
                            for c in cases))


if __name__ == "__main__":
    unittest.main()
