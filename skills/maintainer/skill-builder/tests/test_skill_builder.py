#!/usr/bin/env python3
"""Dogfood tests for the skill-builder skill's own bundle.

Every test method reads the files it needs itself and fails via an assertion
when one is missing — none of this relies on setUp, because an exception
raised there is reported as an ERROR, not a FAILURE, and the red-run contract
(only tests/ added, no SKILL.md, no references/) requires every method to
FAIL, never error and never skip.

Frontmatter is parsed by hand, no PyYAML: the lines between a '---' on line 1
and the next line that is exactly '---'. A key's value is either
'key: <text>' on one line, or 'key: >' (or '>-') followed by lines indented at
least one space, folded by stripping each line and joining with single
spaces. Any other shape raises FrontmatterError, naming the offending line.

"Bundle" means SKILL.md plus every *.md file under references/.
"""

import os
import re
import unittest

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
REFERENCES_DIR = os.path.join(SKILL_DIR, "references")
AUTHORING_MD = os.path.join(REFERENCES_DIR, "authoring.md")
EVALS_MD = os.path.join(REFERENCES_DIR, "evals.md")

LINK_RE = re.compile(r"\]\(([^)]+)\)")
MODE_BLOCK_MARKERS = ("**M0 —", "**M1 —", "**M2 —", "**M3 —")

SKILL_MD_TOKENS = (
    "M0", "M1", "M2", "M3", "M4",
    "skill-prompt-optimizer:prompt-optimizer",
    "/build",
    "references/authoring.md",
    "500",
    "1024",
    "claude-skill-trigger",
    "check_bundles.py",
    "version",
    "first match wins",
)
AUTHORING_MD_TOKENS = (
    "when should this skill load?",
    "Say when, not how.",
    "Shortest that still routes.",
    "1024",
    "Do NOT use",
)
EVALS_MD_TOKENS = (
    "--runs",
    "--harness-control",
    "--positive-control",
    "sibling_set",
    "run_eval.py",
    "--skill-file",
    '"expect"',
    "sandbox",
    "read-only",
    "--dry-run",
    "--live",
    "ANTHROPIC_API_KEY",
    "--cases",
    "--price",
)
FORBIDDEN_SDO_STRINGS = (
    "Rich Description Field",
    "Keyword Coverage",
    "Token Efficiency (Critical)",
    "Cross-Referencing Other Skills",
)


class FrontmatterError(Exception):
    """Raised when a frontmatter line is not one of the two shapes the spec
    allows: 'key: value' on one line, or 'key: >'/'>-' folded scalar."""


def read_optional(path):
    """Return a file's UTF-8 contents, or None if it doesn't exist."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def bundle_files():
    """SKILL.md plus every *.md under references/, as (label, path) pairs.
    Missing files are still named -- callers decide how to fail on them."""
    files = [("SKILL.md", SKILL_MD)]
    # os.walk, not os.listdir: a nested reference must not escape the link,
    # portability and no-SDO-copy checks.
    for root, dirs, names in os.walk(REFERENCES_DIR):
        dirs.sort()
        for name in sorted(names):
            if name.endswith(".md"):
                path = os.path.join(root, name)
                files.append((os.path.relpath(path, SKILL_DIR), path))
    return files


def parse_frontmatter(content):
    """Parse the '---'-delimited frontmatter block into a dict of strings."""
    lines = content.splitlines()
    if not lines or lines[0] != "---":
        raise FrontmatterError("frontmatter does not start with '---' on line 1")
    end = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            end = i
            break
    if end is None:
        raise FrontmatterError("frontmatter has no closing '---' line")
    fm_lines = lines[1:end]
    result = {}
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        if not line.strip():
            i += 1
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if not m:
            raise FrontmatterError(f"frontmatter line is not 'key: value': {line!r}")
        key, rest = m.group(1), m.group(2)
        if rest.rstrip() in (">", ">-"):
            folded = []
            i += 1
            while i < len(fm_lines) and fm_lines[i][:1] in (" ", "\t") and fm_lines[i].strip():
                folded.append(fm_lines[i].strip())
                i += 1
            result[key] = " ".join(folded)
            continue
        result[key] = rest
        i += 1
    return result


def section_between(content, heading):
    """Text between `heading` and the next '## ' heading, or None if
    `heading` isn't present. Runs to end of file if there is no next '## '."""
    idx = content.find(heading)
    if idx == -1:
        return None
    start = idx + len(heading)
    next_idx = content.find("\n## ", start)
    if next_idx == -1:
        return content[start:]
    return content[start:next_idx]


def mode_blocks(section):
    """Split a section into blocks keyed by 'M0'..'M3', each starting at a
    line beginning with one of MODE_BLOCK_MARKERS and running to the next
    such line or the section's end."""
    lines = section.splitlines(keepends=True)
    starts = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        for marker in MODE_BLOCK_MARKERS:
            if stripped.startswith(marker):
                starts.append((i, marker))
                break
    blocks = {}
    for idx, (line_i, marker) in enumerate(starts):
        end_i = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        key = marker[2:4]  # "**M0 —" -> "M0"
        blocks[key] = "".join(lines[line_i:end_i])
    return blocks


class TestSkillBuilderBundle(unittest.TestCase):
    def test_01_skill_md_frontmatter_basics(self):
        content = read_optional(SKILL_MD)
        self.assertIsNotNone(content, f"SKILL.md does not exist at {SKILL_MD}")
        lines = content.splitlines()
        self.assertTrue(lines, "SKILL.md is empty")
        self.assertEqual(lines[0], "---", "SKILL.md line 1 is not '---'")
        try:
            frontmatter = parse_frontmatter(content)
        except FrontmatterError as e:
            self.fail(f"SKILL.md frontmatter is malformed: {e}")
        self.assertEqual(
            frontmatter.get("name"),
            "skill-builder",
            f"SKILL.md frontmatter 'name' is not 'skill-builder': {frontmatter.get('name')!r}",
        )
        description = frontmatter.get("description", "")
        self.assertTrue(
            description.strip(),
            "SKILL.md frontmatter 'description' is empty or missing",
        )

    def test_02_description_under_1024_chars(self):
        content = read_optional(SKILL_MD)
        self.assertIsNotNone(content, f"SKILL.md does not exist at {SKILL_MD}")
        try:
            frontmatter = parse_frontmatter(content)
        except FrontmatterError as e:
            self.fail(f"SKILL.md frontmatter is malformed: {e}")
        description = frontmatter.get("description", "")
        self.assertLessEqual(
            len(description),
            1024,
            f"SKILL.md description is {len(description)} chars, over the 1024 cap",
        )

    def test_03_skill_md_size_budget(self):
        content = read_optional(SKILL_MD)
        self.assertIsNotNone(content, f"SKILL.md does not exist at {SKILL_MD}")
        line_count = len(content.splitlines())
        self.assertLess(
            line_count, 500, f"SKILL.md has {line_count} lines, must be < 500"
        )

    def test_04_references_exist_and_are_linked(self):
        skill_md = read_optional(SKILL_MD)
        self.assertIsNotNone(skill_md, f"SKILL.md does not exist at {SKILL_MD}")
        authoring = read_optional(AUTHORING_MD)
        self.assertIsNotNone(
            authoring, f"references/authoring.md does not exist at {AUTHORING_MD}"
        )
        evals = read_optional(EVALS_MD)
        self.assertIsNotNone(evals, f"references/evals.md does not exist at {EVALS_MD}")
        self.assertIn(
            "references/authoring.md",
            skill_md,
            "SKILL.md does not contain the string 'references/authoring.md'",
        )
        self.assertIn(
            "references/evals.md",
            skill_md,
            "SKILL.md does not contain the string 'references/evals.md'",
        )

    def test_05_relative_links_resolve(self):
        for label, path in bundle_files():
            content = read_optional(path)
            if content is None:
                self.fail(f"{label} does not exist at {path}")
            for match in LINK_RE.finditer(content):
                target = match.group(1).strip()
                if (
                    target.startswith("http")
                    or target.startswith("<")
                    or target.startswith("/")
                    or target.startswith("~")
                ):
                    continue
                fragment_stripped = target.split("#", 1)[0]
                if fragment_stripped == "":
                    continue  # pure #anchor
                resolved = os.path.normpath(
                    os.path.join(os.path.dirname(path), fragment_stripped)
                )
                self.assertTrue(
                    os.path.isfile(resolved),
                    f"{label} links to {target!r}, which does not resolve to an "
                    f"existing file ({resolved})",
                )

    def test_06_load_bearing_tokens(self):
        for label, path, tokens in (
            ("SKILL.md", SKILL_MD, SKILL_MD_TOKENS),
            ("references/authoring.md", AUTHORING_MD, AUTHORING_MD_TOKENS),
            ("references/evals.md", EVALS_MD, EVALS_MD_TOKENS),
        ):
            content = read_optional(path)
            if content is None:
                self.fail(f"{label} does not exist at {path}")
            for token in tokens:
                self.assertIn(
                    token, content, f"{label} is missing load-bearing token {token!r}"
                )

    def test_07_no_absolute_local_paths(self):
        for label, path in bundle_files():
            content = read_optional(path)
            if content is None:
                self.fail(f"{label} does not exist at {path}")
            for prefix in ("/Users/", "/home/"):
                self.assertNotIn(
                    prefix,
                    content,
                    f"{label} contains {prefix!r}; name the place with a placeholder instead",
                )

    def test_08_never_copies_sdo_content(self):
        for label, path in bundle_files():
            content = read_optional(path)
            if content is None:
                self.fail(f"{label} does not exist at {path}")
            for forbidden in FORBIDDEN_SDO_STRINGS:
                self.assertNotIn(
                    forbidden, content, f"{label} copies SDO content: {forbidden!r}"
                )

    def test_08b_needs_no_outside_plugin(self):
        # The description contract is the toolkit's own (references/
        # authoring.md). A read of, or install line for, an outside plugin
        # would stall a maintainer who doesn't have it.
        for label, path in bundle_files():
            content = read_optional(path)
            if content is None:
                self.fail(f"{label} does not exist at {path}")
            self.assertNotIn("superpowers", content.lower(), label)

    def test_09_mode_order_in_step_1(self):
        content = read_optional(SKILL_MD)
        if content is None:
            self.fail(f"SKILL.md does not exist at {SKILL_MD}")
        heading = "## Step 1 — pick the mode"
        section = section_between(content, heading)
        self.assertIsNotNone(section, f"SKILL.md is missing the heading {heading!r}")
        markers = ["| **M4** |", "| **M3** |", "| **M0** |", "| **M1** |", "| **M2** |"]
        positions = []
        for marker in markers:
            count = section.count(marker)
            self.assertEqual(
                count,
                1,
                f"Step 1 section contains {marker!r} {count} times, expected exactly once",
            )
            positions.append(section.index(marker))
        self.assertEqual(
            positions,
            sorted(positions),
            f"Step 1 mode row order is wrong: {markers} landed at indices {positions}, "
            "expected ascending order (M4, M3, M0, M1, M2)",
        )
        m4_idx = section.index("| **M4** |")
        line_start = section.rfind("\n", 0, m4_idx) + 1
        line_end = section.find("\n", m4_idx)
        if line_end == -1:
            line_end = len(section)
        m4_line = section[line_start:line_end]
        self.assertIn(
            "/build", m4_line, "The '| **M4** |' row's line does not contain '/build'"
        )

    def test_10_per_mode_rules_in_step_2(self):
        content = read_optional(SKILL_MD)
        if content is None:
            self.fail(f"SKILL.md does not exist at {SKILL_MD}")
        heading = "## Step 2 — what each mode requires"
        section = section_between(content, heading)
        self.assertIsNotNone(section, f"SKILL.md is missing the heading {heading!r}")
        blocks = mode_blocks(section)
        for key in ("M0", "M1", "M2", "M3"):
            self.assertIn(key, blocks, f"Step 2 section is missing a {key} block")

        self.assertIn(
            "claude-skill-trigger", blocks["M0"], "M0 block is missing 'claude-skill-trigger'"
        )
        self.assertIn("same cases", blocks["M0"], "M0 block is missing 'same cases'")

        for token in (
            "skill-prompt-optimizer:prompt-optimizer",
            "git show HEAD:",
            "token inventory",
            "output eval",
            "description moved",
        ):
            self.assertIn(token, blocks["M2"], f"M2 block is missing {token!r}")

        for token in ("skill-prompt-optimizer:prompt-optimizer", "Step 0", "interview", "Eval cases", "Trigger eval"):
            self.assertIn(token, blocks["M3"], f"M3 block is missing {token!r}")

        self.assertNotIn(
            "prompt-optimizer", blocks["M0"], "M0 block must not contain 'prompt-optimizer'"
        )
        self.assertNotIn(
            "prompt-optimizer", blocks["M1"], "M1 block must not contain 'prompt-optimizer'"
        )

        for key in ("M0", "M1", "M2", "M3"):
            self.assertTrue(
                "check_bundles.py" in blocks[key] or "Ship" in blocks[key],
                f"{key} block is missing both 'check_bundles.py' and 'Ship'",
            )

    def test_11_ship_branches_by_home(self):
        """Mode blocks must route through Ship, and Ship must carry each home's
        own checks: a check that belongs to one home, run for another, validates
        nothing about the skill in hand."""
        content = read_optional(SKILL_MD)
        if content is None:
            self.fail(f"SKILL.md does not exist at {SKILL_MD}")
        step2 = section_between(content, "## Step 2 — what each mode requires")
        self.assertIsNotNone(step2, "SKILL.md is missing the Step 2 heading")
        blocks = mode_blocks(step2)
        for key in ("M0", "M1", "M2"):
            self.assertIn(key, blocks, f"Step 2 section is missing a {key} block")
            self.assertNotIn(
                "check_bundles.py", blocks[key],
                f"{key} block names check_bundles.py directly; it must route "
                "through Ship so a toolkit skill gets the toolkit's checks")
        ship = section_between(content, "## Ship — by the skill's home")
        self.assertIsNotNone(ship, "SKILL.md is missing the Ship heading")
        for token in ("check_bundles.py", "claude-code-toolkit", "plugin.json",
                      "install.sh --check", "build_catalogue.py",
                      "docs/capabilities.json"):
            self.assertIn(token, ship, f"Ship section is missing {token!r}")

    def test_12_review_findings_stay_fixed(self):
        """Each rule below fails silently when it goes missing: an unreviewed
        installed skill, a check that found nothing to check, an M1 rule change
        nobody measured, and an installed copy still serving the old version."""
        content = read_optional(SKILL_MD)
        if content is None:
            self.fail(f"SKILL.md does not exist at {SKILL_MD}")
        m1 = mode_blocks(section_between(content, "## Step 2 — what each mode requires"))["M1"]
        for token, why in (
            ("If an output eval case file exists",
             "M1's output eval lost its when-cases-exist condition"),
            ("git show HEAD:", "M1 no longer snapshots the old file for the A/B"),
            ("output eval A/B", "M1 no longer runs the output eval A/B"),
        ):
            self.assertIn(token, m1, why)
        routing = section_between(content, "## When to read the references")
        evals_line = next((l for l in routing.split("\n- ") if "references/evals.md" in l), "")
        self.assertIn("M1", evals_line,
                      "the references routing no longer sends M1 to references/evals.md")
        ship = section_between(content, "## Ship — by the skill's home")
        for token, why in (
            ("silent-failure", "Ship no longer points at the silent-failure review gate"),
            ("before opening the PR", "Ship no longer orders the gate before the PR"),
            ("count line", "Ship no longer says to read check_bundles.py's count"),
            ("refuses when it finds none", "Ship no longer names the empty-selection refusal"),
            ("claude plugin update", "Ship no longer says the installed copy needs updating"),
            ("cached per version", "Ship no longer says why a plugin skill needs a version bump"),
        ):
            self.assertIn(token, ship, why)
        self.assertIn("hard cap", content, "the size budget no longer says 500 lines is a hard cap")

    def test_13_no_other_runtime_or_org_surface(self):
        """This is a Claude Code skill for one person's own skills: it names no
        chat upload, other runtime, organization bundle or access tier."""
        for label, path in bundle_files():
            content = read_optional(path)
            if content is None:
                self.fail(f"{label} does not exist at {path}")
            for forbidden in ("claude.ai", "Cowork", "access tier", "chat bundle",
                              "<workspace_root>"):
                self.assertNotIn(forbidden, content, f"{label} names {forbidden!r}")


if __name__ == "__main__":
    unittest.main()
