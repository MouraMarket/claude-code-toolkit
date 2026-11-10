#!/usr/bin/env python3
"""Tests for claude_imports.py: line-start @imports, fences, path resolution."""

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import claude_imports


class IterImportsTestCase(unittest.TestCase):
    def test_line_start_import_is_found(self):
        text = "hello\n@~/policy/CLAUDE.md\nbye\n"
        self.assertEqual(claude_imports.iter_imports(text),
                         [(2, "~/policy/CLAUDE.md")])

    def test_indented_import_is_found(self):
        self.assertEqual(claude_imports.iter_imports("   @a/b.md\n"),
                         [(1, "a/b.md")])

    def test_backtick_fence_hides_import_and_closing_reveals_next(self):
        text = "```\n@inside.md\n```\n@after.md\n"
        self.assertEqual(claude_imports.iter_imports(text), [(4, "after.md")])

    def test_tilde_fence_hides_import(self):
        text = "~~~\n@inside.md\n~~~\n@after.md\n"
        self.assertEqual(claude_imports.iter_imports(text), [(4, "after.md")])

    def test_other_fence_marker_does_not_close(self):
        text = "```\n~~~\n@still-inside.md\n```\n@out.md\n"
        self.assertEqual(claude_imports.iter_imports(text), [(5, "out.md")])

    def test_mid_line_at_is_not_found(self):
        self.assertEqual(claude_imports.iter_imports("see @foo.md here\n"), [])

    def test_bare_at_is_not_found(self):
        self.assertEqual(claude_imports.iter_imports("@ foo\n@\n"), [])

    def test_escaped_space_unescaped_and_stops_at_first_unescaped_space(self):
        text = "@/a\\ b/c.md trailing words\n"
        self.assertEqual(claude_imports.iter_imports(text), [(1, "/a b/c.md")])

    def test_empty_and_none_are_total(self):
        self.assertEqual(claude_imports.iter_imports(""), [])
        self.assertEqual(claude_imports.iter_imports(None), [])


class ResolveTestCase(unittest.TestCase):
    def test_home_relative(self):
        self.assertEqual(claude_imports.resolve("~/x/y.md", "/h/.claude/CLAUDE.md", "/h"),
                         os.path.join("/h", "x/y.md"))

    def test_absolute_is_normalised(self):
        self.assertEqual(claude_imports.resolve("/a/b/../c.md", "/h/f.md", "/h"),
                         os.path.normpath("/a/c.md"))

    def test_relative_to_importing_file(self):
        self.assertEqual(claude_imports.resolve("../p/q.md", "/h/.claude/CLAUDE.md", "/h"),
                         os.path.normpath("/h/p/q.md"))


class FenceLengthTestCase(unittest.TestCase):
    def test_shorter_backtick_run_does_not_close(self):
        text = "````\n```\n@inside.md\n"
        self.assertEqual(claude_imports.iter_imports(text), [])

    def test_four_backticks_close(self):
        text = "````\n@inside.md\n````\n@after.md\n"
        self.assertEqual(claude_imports.iter_imports(text), [(4, "after.md")])

    def test_five_backticks_close(self):
        text = "````\n@inside.md\n`````\n@after.md\n"
        self.assertEqual(claude_imports.iter_imports(text), [(4, "after.md")])

    def test_tilde_run_does_not_close_backtick_fence(self):
        text = "````\n~~~~\n@inside.md\n"
        self.assertEqual(claude_imports.iter_imports(text), [])

    def test_open_fence_line_reports_long_opener_left_open(self):
        self.assertEqual(claude_imports.open_fence_line("a\n````\nx\n```\n"), 2)

    def test_lines_outside_fences_respects_length(self):
        text = "````\n```\nx\n````\ny"
        self.assertEqual(claude_imports.lines_outside_fences(text), [(5, "y")])


class MatchesOutsideFencesTestCase(unittest.TestCase):
    HEADING = re.compile(r"^#+ .*$", re.M)

    def headings(self, text):
        return [m.group(0) for m in claude_imports.matches_outside_fences(self.HEADING, text)]

    def test_headings_quoted_in_any_fence_are_not_matched(self):
        for name, fenced in (("backtick", "```\n# q\n```"), ("tilde", "~~~\n# q\n~~~"),
                             ("nested", "````\n```\n# q\n```\n# q2\n````")):
            with self.subTest(fence=name):
                text = "# a\nx\n%s\n# b\n" % fenced
                self.assertEqual(self.headings(text), ["# a", "# b"])

    def test_offsets_slice_the_original_text_fences_included(self):
        text = "# a\n```\n# q\n```\n# b\n"
        first, second = claude_imports.matches_outside_fences(self.HEADING, text)
        self.assertEqual(text[first.start():second.start()], "# a\n```\n# q\n```\n")

    def test_total_on_empty_and_none(self):
        self.assertEqual(claude_imports.matches_outside_fences(self.HEADING, ""), [])
        self.assertEqual(claude_imports.matches_outside_fences(self.HEADING, None), [])


class OpenFenceLineTestCase(unittest.TestCase):
    def test_unclosed_returns_opener_line(self):
        self.assertEqual(claude_imports.open_fence_line("a\nb\n```\nc\n"), 3)

    def test_balanced_returns_none(self):
        self.assertIsNone(claude_imports.open_fence_line("```\nx\n```\ny\n"))

    def test_tilde_unclosed(self):
        self.assertEqual(claude_imports.open_fence_line("~~~\nx\n"), 1)

    def test_no_fence_returns_none(self):
        self.assertIsNone(claude_imports.open_fence_line("plain\n"))
        self.assertIsNone(claude_imports.open_fence_line(""))


if __name__ == "__main__":
    unittest.main()
