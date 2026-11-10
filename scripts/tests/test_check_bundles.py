"""Tests for scripts/check_bundles.py.

Run: python3 scripts/tests/test_check_bundles.py

The checker guards silent failures, so its own false-positive rate decides
whether it survives: a check that blocks a good skill gets switched off. The
tests pin exactly what is and is not looked at, and drive main() so a scan
that is never called cannot pass.
"""
import contextlib
import glob
import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import check_bundles as cb


def _skill_md(description_block):
    return "---\nname: demo\n{0}\n---\n\n# Demo\n".format(description_block)


class TestReadDescription(unittest.TestCase):
    """The reader is hand-rolled because the suites are stdlib-only.

    Every expectation below was adjudicated by PyYAML before being written
    down, and the chomping cases are here because the first version of this
    reader got them wrong by exactly one character -- in the permissive
    direction, which is the one that lets an over-long description through.
    """

    def test_folded_strip_joins_with_spaces_and_drops_trailing_newline(self):
        self.assertEqual(
            cb.read_description(_skill_md("description: >-\n  a\n  b")), "a b")

    def test_folded_clip_keeps_exactly_one_trailing_newline(self):
        # A bare `>` is CLIP, not STRIP. Bare `>` is common in real SKILL.md files,
        # and treating it as strip under-counts every one of them.
        self.assertEqual(
            cb.read_description(_skill_md("description: >\n  a\n  b")), "a b\n")

    def test_folded_keep_retains_every_trailing_newline(self):
        self.assertEqual(
            cb.read_description(_skill_md("description: >+\n  a\n  b\n\n")),
            "a b\n\n\n")

    def test_literal_block_preserves_line_breaks(self):
        self.assertEqual(
            cb.read_description(_skill_md("description: |-\n  a\n  b")), "a\nb")

    def test_blank_line_in_folded_becomes_a_newline(self):
        self.assertEqual(
            cb.read_description(_skill_md("description: >\n  a\n\n  b")), "a\nb\n")

    def test_inline_and_quoted_scalars(self):
        self.assertEqual(
            cb.read_description(_skill_md("description: plain words")), "plain words")
        self.assertEqual(
            cb.read_description(_skill_md('description: "quoted words"')), "quoted words")

    def test_missing_description_returns_none(self):
        self.assertIsNone(cb.read_description("---\nname: demo\n---\n"))

    def test_no_frontmatter_returns_none(self):
        self.assertIsNone(cb.read_description("# Just a heading\n"))

    def test_unterminated_frontmatter_returns_none(self):
        self.assertIsNone(cb.read_description("---\nname: demo\ndescription: x\n"))


class TestUnmeasurableBlockIsUnreadable(unittest.TestCase):
    """YAML keeps a block line's extra indentation and its trailing spaces.
    Stripping them under-counts, so such a block must read as unreadable (and
    fail check_skill), never as a shorter, passing description. The first
    case is the Codex Code Review repro: PyYAML reads 1,025 characters."""

    OVER = "description: |-\n  z\n    " + "x" * 1021

    def test_a_more_indented_line_is_unreadable_and_fails(self):
        self.assertIsNone(cb.read_description(_skill_md(self.OVER)))
        failures = cb.check_skill("demo/SKILL.md", _skill_md(self.OVER))
        self.assertTrue(any("no readable description" in f for f in failures), failures)

    def test_trailing_spaces_on_a_block_line_are_unreadable(self):
        self.assertIsNone(cb.read_description(_skill_md("description: >-\n  one  \n  two")))

    def test_positive_control_an_even_block_is_measured_exactly(self):
        even = "description: |-\n  z\n  " + "x" * 1021
        self.assertEqual(cb.read_description(_skill_md(even)), "z\n" + "x" * 1021)
        self.assertEqual(cb.check_skill("demo/SKILL.md", _skill_md(even)), [])


class TestScanDescription(unittest.TestCase):

    PATH = "skills/x/demo/SKILL.md"

    def test_description_at_the_limit_passes(self):
        body = "x" * cb.DESCRIPTION_LIMIT
        findings = cb.scan_description(
            self.PATH, _skill_md("description: >-\n  " + body))
        self.assertEqual(findings, [])

    def test_one_character_over_the_limit_fails(self):
        # The boundary is the whole point: an off-by-one here is the difference
        # between catching a broken skill and shipping one.
        body = "x" * (cb.DESCRIPTION_LIMIT + 1)
        findings = cb.scan_description(
            self.PATH, _skill_md("description: >-\n  " + body))
        self.assertEqual(len(findings), 1)
        self.assertIn(str(cb.DESCRIPTION_LIMIT + 1), findings[0])
        self.assertIn("Cut 1 character", findings[0])

    def test_a_description_well_over_the_limit_is_caught(self):
        findings = cb.scan_description(
            "plugins/p/skills/demo/SKILL.md",
            _skill_md("description: >-\n  " + "x" * 1171))
        self.assertEqual(len(findings), 1)
        self.assertIn("Cut 147 character", findings[0])

    def test_only_skill_md_is_checked(self):
        over = _skill_md("description: >-\n  " + "x" * 2000)
        self.assertEqual(
            cb.scan_description("skills/x/demo/references/x.md", over), [])

    def test_file_without_a_description_is_not_flagged(self):
        self.assertEqual(
            cb.scan_description(self.PATH, "# no frontmatter here\n"), [])


def _lines(n, newline_at_end=True):
    """A SKILL.md body of exactly `n` lines as splitlines() counts them."""
    body = "\n".join("line {0}".format(i) for i in range(n))
    return body + ("\n" if newline_at_end else "")


class TestScanSize(unittest.TestCase):
    """The boundaries, pinned. An off-by-one here either blocks a compliant
    skill or waves an over-cap one through, and neither announces itself."""

    PATH = "skills/x/demo/SKILL.md"

    def test_exactly_the_limit_passes(self):
        self.assertEqual(cb.scan_size(self.PATH, _lines(cb.LINE_LIMIT)), [])

    def test_one_line_over_fails(self):
        failures = cb.scan_size(self.PATH, _lines(cb.LINE_LIMIT + 1))
        self.assertEqual(len(failures), 1)
        self.assertIn("501 lines", failures[0])

    def test_a_missing_final_newline_does_not_buy_a_line(self):
        failures = cb.scan_size(
            self.PATH, _lines(cb.LINE_LIMIT + 1, newline_at_end=False))
        self.assertEqual(len(failures), 1)

    def test_a_huge_non_skill_file_is_ignored(self):
        self.assertEqual(
            cb.scan_size("skills/x/demo/references/big.md", _lines(5000)), [])


def _run_main(argv):
    """main()'s exit code and stdout."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cb.main(argv)
    return code, out.getvalue()


def _write(root, rel, text):
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as handle:
        handle.write(text)


def _described(length):
    return _skill_md("description: >-\n  " + "x" * length)


class TestFindSkills(unittest.TestCase):

    def test_a_missing_path_raises(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                cb.find_skills([os.path.join(root, "nope")])

    def test_a_skill_md_file_path_is_itself(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "skills/x/demo/SKILL.md", _described(5))
            target = os.path.join(root, "skills/x/demo/SKILL.md")
            self.assertEqual(cb.find_skills([target]), [target])

    def test_a_skill_directory_is_found(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "skills/x/demo/SKILL.md", _described(5))
            found = cb.find_skills([os.path.join(root, "skills/x/demo")])
            self.assertEqual(
                found, [os.path.join(root, "skills/x/demo/SKILL.md")])

    def test_a_nested_skill_md_inside_a_skill_is_content(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "skills/x/demo/SKILL.md", _described(5))
            _write(root, "skills/x/demo/examples/x/SKILL.md", _lines(900))
            found = cb.find_skills([root])
            self.assertEqual(
                found, [os.path.join(root, "skills/x/demo/SKILL.md")])

    def test_results_are_sorted(self):
        with tempfile.TemporaryDirectory() as root:
            for name in ("b", "a", "c"):
                _write(root, "skills/x/%s/SKILL.md" % name, _described(5))
            found = cb.find_skills([root])
            self.assertEqual(found, sorted(found))
            self.assertEqual(len(found), 3)


class TestCheckSkill(unittest.TestCase):

    PATH = "skills/x/demo/SKILL.md"

    def test_a_clean_skill_has_no_failures(self):
        self.assertEqual(cb.check_skill(self.PATH, _described(10)), [])

    def test_no_description_fails_with_the_reason(self):
        failures = cb.check_skill(self.PATH, "# no frontmatter\n")
        self.assertEqual(len(failures), 1)
        self.assertIn("never triggers", failures[0])


class TestMain(unittest.TestCase):

    def setUp(self):
        self._saved_root = cb.REPO_ROOT

    def tearDown(self):
        cb.REPO_ROOT = self._saved_root

    def test_no_paths_and_an_empty_root_exits_two(self):
        with tempfile.TemporaryDirectory() as root:
            cb.REPO_ROOT = root
            code, out = _run_main([])
        self.assertEqual(code, 2, out)
        self.assertIn("nothing was checked", out)

    def test_an_empty_directory_path_exits_two(self):
        with tempfile.TemporaryDirectory() as root:
            code, out = _run_main([root])
        self.assertEqual(code, 2, out)
        self.assertIn("nothing was checked", out)

    def test_a_missing_path_exits_two_and_names_it(self):
        with tempfile.TemporaryDirectory() as root:
            missing = os.path.join(root, "nope")
            code, out = _run_main([missing])
        self.assertEqual(code, 2, out)
        self.assertIn("nope", out)

    def test_an_untracked_new_skill_is_found_and_counted(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "skills/x/fresh/SKILL.md", _described(10))
            code, out = _run_main([root])
        self.assertEqual(code, 0, out)
        self.assertIn("Checked 1 skill(s).", out)

    def test_an_untracked_over_cap_skill_fails(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "skills/x/fresh/SKILL.md",
                   _skill_md("description: short") + _lines(cb.LINE_LIMIT))
            code, out = _run_main([root])
        self.assertEqual(code, 1, out)
        self.assertIn("lines, over the 500-line", out)

    def test_an_over_cap_description_fails_once(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "plugins/p/skills/demo/SKILL.md",
                   _described(cb.DESCRIPTION_LIMIT + 1))
            code, out = _run_main([root])
        self.assertEqual(code, 1, out)
        self.assertEqual(out.count("Cut 1 character"), 1, out)

    def test_a_description_at_the_limit_passes(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "plugins/p/skills/demo/SKILL.md",
                   _described(cb.DESCRIPTION_LIMIT))
            code, out = _run_main([root])
        self.assertEqual(code, 0, out)

    def test_a_missing_description_fails(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "skills/x/demo/SKILL.md", "# nothing\n")
            code, out = _run_main([root])
        self.assertEqual(code, 1, out)
        self.assertIn("no readable description", out)

    def test_a_nested_example_is_not_checked(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "skills/x/demo/SKILL.md", _described(10))
            _write(root, "skills/x/demo/examples/x/SKILL.md", _lines(900))
            code, out = _run_main([root])
        self.assertEqual(code, 0, out)
        self.assertIn("Checked 1 skill(s).", out)

    def test_a_file_path_to_a_skill_md_works(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "skills/x/demo/SKILL.md", _described(cb.DESCRIPTION_LIMIT + 5))
            code, out = _run_main([os.path.join(root, "skills/x/demo/SKILL.md")])
        self.assertEqual(code, 1, out)
        self.assertIn("Checked 1 skill(s).", out)

    def test_the_real_repo_default_run_counts_every_skill(self):
        """The default paths reach every skill in this repository. Whether the
        repository's own skills all pass is TestRealRepo's job."""
        expected = (
            len(glob.glob(os.path.join(cb.REPO_ROOT, "skills", "*", "*", "SKILL.md")))
            + len(glob.glob(os.path.join(
                cb.REPO_ROOT, "plugins", "*", "skills", "*", "SKILL.md"))))
        saved = os.getcwd()
        try:
            os.chdir(cb.REPO_ROOT)
            code, out = _run_main([])
        finally:
            os.chdir(saved)
        self.assertIn(code, (0, 1), out)
        counted = re.search(r"Checked (\d+) skill\(s\)\.", out)
        self.assertIsNotNone(counted, out)
        self.assertEqual(int(counted.group(1)), expected)


class TestRealRepo(unittest.TestCase):

    def test_the_repo_passes_with_no_path(self):
        """The check local CI runs: no arguments, cwd = the repo root. Fails
        if any skill is over a limit, and if the scan reached nothing."""
        saved = os.getcwd()
        try:
            os.chdir(cb.REPO_ROOT)
            code, out = _run_main([])
        finally:
            os.chdir(saved)
        self.assertEqual(code, 0, out)
        counted = re.search(r"Checked (\d+) skill\(s\)\.", out)
        self.assertIsNotNone(counted, out)
        self.assertGreaterEqual(int(counted.group(1)), 13, out)


if __name__ == "__main__":
    unittest.main()
