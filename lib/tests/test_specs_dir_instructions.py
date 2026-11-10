#!/usr/bin/env python3
"""Tests that the instruction files resolve the specs directory from config.

`/build`, the handover skill, brainstorming and the toolkit policy once
hard-coded `workspace/specs/`, a cwd-relative path that is wrong on any machine
whose workspace lives elsewhere. They now point at `<specs_dir>`, resolved by
`lib/toolkit_config.py specs-dir`. These tests pin the wording and run the
command exactly as the instructions write it.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

BUILD_REL = "skills/builder/build/SKILL.md"
BRAINSTORMING_REL = "tools/build-lens/brainstorming.md"
HANDOVER_REL = "skills/continuity/handover/SKILL.md"
MEASUREMENTS_REL = "docs/build-measurements.md"

BUILD = os.path.join(REPO, "skills", "builder", "build", "SKILL.md")
BRAINSTORMING = os.path.join(REPO, "tools", "build-lens", "brainstorming.md")
HANDOVER = os.path.join(REPO, "skills", "continuity", "handover", "SKILL.md")
POLICY = os.path.join(REPO, "policy", "CLAUDE.md")
MEASUREMENTS = os.path.join(REPO, "docs", "build-measurements.md")
EXAMPLE_CONFIG = os.path.join(REPO, "config.example.json")

RESOLVER = 'python3 "${CLAUDE_PLUGIN_ROOT}/lib/toolkit_config.py" specs-dir'
CWD_RELATIVE = re.compile(r"(?<![\w/~.>-])workspace/specs")
ALLOWED_EXAMPLE_PREFIXES = ("Plan: ", "Design: ", "mission_spec: ")


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def sections(text):
    """Split markdown at lines starting `## `; returns [(heading, body)]."""
    parts = []
    heading, lines = "", []
    for line in text.splitlines():
        if line.startswith("## "):
            parts.append((heading, "\n".join(lines)))
            heading, lines = line, []
        else:
            lines.append(line)
    parts.append((heading, "\n".join(lines)))
    return parts


def section_starting(text, prefix):
    for heading, body in sections(text):
        if heading.startswith(prefix):
            return body
    raise AssertionError("no section heading starts %r" % prefix)


def resolver_command(text, plugin_dir):
    """First backticked span starting `python3 "${CLAUDE_PLUGIN_ROOT}`, argv-ready."""
    for span in re.findall(r"`([^`\n]+)`", text):
        if span.startswith('python3 "${CLAUDE_PLUGIN_ROOT}'):
            argv = shlex.split(span.replace("${CLAUDE_PLUGIN_ROOT}", plugin_dir))
            argv[0] = sys.executable
            return argv
    raise AssertionError("no backticked resolver command found")


def run_resolver(argv, config):
    with tempfile.TemporaryDirectory() as tmp:
        config_path = os.path.join(tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config_path)
        return subprocess.run(argv, env=env, cwd=tmp, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, timeout=60)


class TestNoCwdRelativeSpecPath(unittest.TestCase):
    def test_a_policy_names_no_cwd_relative_spec_path(self):
        self.assert_only_allowed_examples(POLICY)

    def test_a_handover_names_no_cwd_relative_spec_path(self):
        self.assert_only_allowed_examples(HANDOVER)

    def test_a_build_names_no_cwd_relative_spec_path(self):
        for path in (BUILD, BRAINSTORMING):
            self.assert_only_allowed_examples(path)

    def test_a_check_sees_a_cwd_relative_path(self):
        # positive control: the pattern does catch the path it exists for
        self.assertTrue(CWD_RELATIVE.search("see workspace/specs/plan.md"))
        self.assertFalse(CWD_RELATIVE.search("see <workspace_root>/workspace/specs"))

    def assert_only_allowed_examples(self, path):
        for number, line in enumerate(read(path).splitlines(), 1):
            if not CWD_RELATIVE.search(line):
                continue
            self.assertTrue(
                line.strip().startswith(ALLOWED_EXAMPLE_PREFIXES),
                "%s:%d names a cwd-relative spec path: %s"
                % (os.path.relpath(path, REPO), number, line.strip()))


class TestInstructionWording(unittest.TestCase):
    def test_b_resolver_is_in_phase_0(self):
        text = read(BUILD)
        phase0 = section_starting(text, "## Phase 0")
        self.assertIn(RESOLVER, phase0)
        self.assertIn("Exit 3", phase0)
        phase2 = section_starting(text, "## Phase 2")
        self.assertIn("<specs_dir>/YYYY-MM-DD-<slug>-plan.md", phase2)

    def test_c_handover_names_the_same_command(self):
        self.assertIn(RESOLVER, read(HANDOVER))

    def test_d_brainstorming(self):
        text = read(BRAINSTORMING)
        self.assertIn("<specs_dir>/YYYY-MM-DD-<slug>-design.md", text)
        self.assertIn("<specs_dir>/YYYY-MM-DD-<slug>-requirements.md", text)
        self.assertNotIn("CLAUDE_PLUGIN_ROOT", text)


class TestResolverRunsAsWritten(unittest.TestCase):
    def check(self, text, plugin_dir):
        argv = resolver_command(text, os.path.join(REPO, plugin_dir))
        with tempfile.TemporaryDirectory() as specs:
            specs = os.path.realpath(specs)
            ok = run_resolver(argv, {"spec_archive": {"specs_dir": specs}})
            self.assertEqual(ok.returncode, 0, ok.stderr)
            self.assertEqual(ok.stdout, specs + "\n")
        unset = run_resolver(argv, {"spec_archive": {}})
        self.assertEqual(unset.returncode, 3, unset.stderr)

    def test_e_build_phase_0_command_runs(self):
        self.check(section_starting(read(BUILD), "## Phase 0"), "plugin-build")

    def test_e_handover_command_runs(self):
        self.check(read(HANDOVER), "plugin-continuity")


class TestExampleConfigAndDocs(unittest.TestCase):
    def test_f_example_config_specs_dir(self):
        with open(EXAMPLE_CONFIG, encoding="utf-8") as handle:
            config = json.load(handle)
        # null, so a copy of the example moves nothing; the doc gives an example
        self.assertIsNone(config["spec_archive"]["specs_dir"])
        doc = " ".join(config["spec_archive"]["//specs_dir"])
        self.assertIn("e.g.", doc)
        self.assertFalse(CWD_RELATIVE.search(doc), doc)

    def test_g_policy(self):
        policy = section_starting(read(POLICY), "## Spec-first planning")
        self.assertIn("spec_archive.specs_dir", policy)

    def test_g_measurements(self):
        measurements = read(MEASUREMENTS)
        self.assertIn("rg '^Miss:' <specs_dir>", measurements)
        self.assertNotIn("rg '^Miss:' workspace/specs", measurements)


if __name__ == "__main__":
    unittest.main()
