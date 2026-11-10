#!/usr/bin/env python3
"""Pins /onboard's Phase 6b canary to what makes it DOABLE, not to its sentence.

Phase 6b tells Claude to run one command through its Bash tool and expect a refusal
carrying a marker. Both strings live in prose, so an edit to either (or to the
guard's wording) would leave every other suite green while the live check on a
teammate's machine reads "not protected" for a working guard, or "protected" for a
command the guard never judged. This runs the doc's own canary through the real
guard under the configs a teammate can have, and asserts the doc's own marker comes
back. The path is generated per run (a fixed one could exist), so the generator
block is run too, and the `rm -rf` line must be the placeholder form the doc
tells Claude to substitute.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
import claude_imports  # noqa: E402

_H2_RE = re.compile(r"^## .*$", re.M)

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))
ONBOARD = os.path.join(REPO_ROOT, "skills", "core", "onboard", "SKILL.md")
GUARD = os.path.join(REPO_ROOT, "tools", "danger-guard", "claude-danger-guard")
MARKER = "BLOCKED by claude-danger-guard"
PLACEHOLDER = "rm -rf /toolkit-proof-<id>"
PATH_LINE = re.compile(r"^/toolkit-proof-[0-9a-f]{16}$")


def phase_6b_of(text):
    """The `## Phase 6b` section of `text`, up to the next `## ` heading. A
    `## ` line inside a fence is quoted text: it neither starts nor ends one."""
    headings = claude_imports.matches_outside_fences(_H2_RE, text)
    for index, match in enumerate(headings):
        if re.match(r"## Phase 6b\b", match.group(0)):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return text[match.start():end]
    raise AssertionError("SKILL.md has no '## Phase 6b' section")


def phase_6b():
    with open(ONBOARD, encoding="utf-8") as handle:
        return phase_6b_of(handle.read())


def bash_blocks(section):
    return re.findall(r"```bash\n(.*?)```", section, re.S)


def canary_commands(section):
    """Every `rm -rf ...` line inside a fenced bash block of the section."""
    found = []
    for block in bash_blocks(section):
        found.extend(line.strip() for line in block.splitlines()
                     if line.strip().startswith("rm -rf "))
    return found


def child_env(home, config_path=None):
    """Explicit allowlist: nothing else from the parent leaks into a child."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home,
           "USERPROFILE": home, "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("LANG"):
        env["LANG"] = os.environ["LANG"]
    if config_path is not None:
        env["CLAUDE_TOOLKIT_CONFIG"] = config_path
    return env


class TestPhase6bCanary(unittest.TestCase):
    def setUp(self):
        self.section = phase_6b()
        canaries = canary_commands(self.section)
        self.assertEqual(len(canaries), 1, canaries)
        self.canary = canaries[0]
        generators = [b for b in bash_blocks(self.section) if "secrets.token_hex" in b]
        self.assertEqual(len(generators), 1, "expected one generator block")
        self.generator = generators[0]
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)

    def generate(self):
        proc = subprocess.run(["bash", "-c", self.generator], env=child_env(self.home),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, proc.stdout)
        self.assertRegex(lines[0], PATH_LINE)
        return lines[0]

    def run_guard(self, config, command):
        config_path = os.path.join(self.tmp.name, "config.json")
        if config is None:
            config_path = os.path.join(self.tmp.name, "absent.json")
        else:
            with open(config_path, "w") as handle:
                json.dump(config, handle)
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command},
                              "session_id": "canary-test", "cwd": self.home})
        return subprocess.run([GUARD], input=payload, env=child_env(self.home, config_path),
                              capture_output=True, text=True, timeout=60)

    def test_the_doc_names_the_marker_and_the_live_check(self):
        self.assertIn(MARKER, self.section)
        self.assertIn("--verify-live", self.section)

    def test_step_3_says_which_hooks_it_proves(self):
        match = re.search(r"^3\. .*?(?=^4\. )", self.section, re.M | re.S)
        self.assertIsNotNone(match, "no step 3 in Phase 6b")
        step = " ".join(match.group(0).split())
        for needle in ("--verify-live", "each selected profile", "unverified"):
            self.assertIn(needle, step)

    def test_the_generator_prints_a_fresh_path_that_does_not_exist(self):
        first, second = self.generate(), self.generate()
        self.assertNotEqual(first, second)
        for path in (first, second):
            self.assertFalse(os.path.lexists(path), path)
        self.assertIn("os.path.lexists", self.generator)

    def test_the_canary_line_is_the_placeholder_form(self):
        self.assertEqual(self.canary, PLACEHOLDER)

    def test_refused_with_the_marker_under_every_teammate_config(self):
        self.assertEqual(self.canary, PLACEHOLDER)
        path = self.generate()
        self.assertFalse(os.path.lexists(path), path)
        command = self.canary.replace("/toolkit-proof-<id>", path)
        configs = {
            "no config": None,
            "workspace_root unset": {},
            # The case that broke a ~/-rooted canary: a teammate whose whole home
            # is their workspace. The guard must still refuse.
            "workspace_root = home": {"workspace_root": self.home},
            "workspace_root = a project dir": {"workspace_root": os.path.join(self.home, "work")},
        }
        for label, config in configs.items():
            with self.subTest(config=label):
                proc = self.run_guard(config, command)
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertIn(MARKER, proc.stdout + proc.stderr)


class TestFencedHeadingsAreQuoted(unittest.TestCase):
    """A `## ` line inside a fence is quoted text. `phase_6b_of` must neither
    end nor start the section on one, and must keep the fenced body in it."""

    def test_phase_6b_skips_headings_inside_fences(self):
        for name, opener, closer in FENCES:
            with self.subTest(fence=name):
                template = ("## Phase 6a\nbefore\n## Phase 6b\nlive\n%s\n## Phase 6b fake\n"
                            "## Other fake\n%s\ntail\n## Phase 7\nafter\n")
                doc = template % (opener, closer)
                section = phase_6b_of(doc)
                self.assertIn("tail", section, "a fenced heading ended the section")
                self.assertIn("## Other fake", section, "the fenced body was dropped")
                self.assertNotIn("after", section)
                self.assertTrue(section.startswith("## Phase 6b\nlive\n"))
                # Positive control: without the fence markers the same lines ARE headings.
                bare = phase_6b_of(template % ("", ""))
                self.assertNotIn("tail", bare)
                self.assertEqual(bare, "## Phase 6b\nlive\n\n")
                # A quoted heading must not START the section either.
                quoted = "## A\n%s\n## Phase 6b\n%s\n## B\n"
                with self.assertRaises(AssertionError):
                    phase_6b_of(quoted % (opener, closer))
                self.assertEqual(phase_6b_of(quoted % ("", "")), "## Phase 6b\n\n")


if __name__ == "__main__":
    unittest.main()
