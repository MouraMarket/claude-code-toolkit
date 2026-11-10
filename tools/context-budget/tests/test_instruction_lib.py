#!/usr/bin/env python3
"""Tests for instruction_lib, the instruction-meter's unit (sentences, v2).

Every assertion is against a minimal, hand-derived fixture -- never against
the user's live CLAUDE.md or MEMORY.md, which other work changes daily.
"""

import os
import sys
import textwrap
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

# Pin the config away from this machine's, as the sibling suites do.
os.environ["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent-instruction-lib-test.json"

sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO, "tools", "memory-lint"))

import instruction_lib
import lint_lib

# Verbatim fixture.
# Hand-derived count under unit v2 (sentences): 16.
FIXTURE_MD = """---
name: x
---
# Heading

Para one line one
line two of para one.

- item a
  continuation of a
- item b
  - nested c
1. numbered d
2) numbered e

| h1 | h2 |
|---|---|
| r1 | x |
| r2 | y |

```
- not an item
para not counted
```

<!-- a comment
- not an item -->
> quoted para
> - quoted item

---

Final para.
"""


class TestCountMarkdownFixture(unittest.TestCase):
    def test_the_full_section_2_fixture_counts_16(self):
        self.assertEqual(instruction_lib.count_markdown(FIXTURE_MD), 16)


class TestCountMarkdownSentenceFixtures(unittest.TestCase):
    """One assertion per fixture in the plan's "Unit v2: the rules" table."""

    def test_01_terminated_sentences_split_three_ways(self):
        self.assertEqual(instruction_lib.count_markdown("One. Two. Three.\n"), 3)

    def test_02_unterminated_continuation_is_one_sentence(self):
        text = "One line\nstill the same sentence.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 1)

    def test_03_heading_counts_one_not_zero(self):
        self.assertEqual(instruction_lib.count_markdown("# A heading\n"), 1)

    def test_04_heading_with_a_number_still_counts_one(self):
        self.assertEqual(instruction_lib.count_markdown("## 5. The MVP\n"), 1)

    def test_05_list_items_split_on_sentence_terminators(self):
        text = "- item a. More a.\n- item b\n"
        self.assertEqual(instruction_lib.count_markdown(text), 3)

    def test_06_list_continuation_joins_before_splitting(self):
        text = "- item a\n  continuation. Still a.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 2)

    def test_07_table_cells_split_on_sentence_terminators(self):
        text = "| Head | Other |\n|---|---|\n| cell one. cell two. | x |\n"
        self.assertEqual(instruction_lib.count_markdown(text), 5)

    def test_08_table_cells_with_no_letters_count_zero(self):
        text = "| 1 | 2 |\n|---|---|\n| 3 | 4 |\n"
        self.assertEqual(instruction_lib.count_markdown(text), 0)

    def test_08b_an_escaped_pipe_inside_a_cell_is_not_a_cell_boundary(self):
        # Merge Review 2026-09-28: splitting on every `|` cut `cat a \| sort`
        # in two, before code-span masking, so one command counted twice.
        text = "| Command | Action |\n|---|---|\n| `cat a \\| sort` | Run it. |\n"
        self.assertEqual(instruction_lib.count_markdown(text), 4)

    def test_09_backtick_fence_contents_count_zero(self):
        text = "```\nNot. Counted.\n```\n"
        self.assertEqual(instruction_lib.count_markdown(text), 0)

    def test_10_tilde_fence_contents_count_zero(self):
        text = "~~~\nNot. Counted.\n~~~\n"
        self.assertEqual(instruction_lib.count_markdown(text), 0)

    def test_11_front_matter_at_byte_zero_is_stripped(self):
        text = "---\nname: x\n---\nBody.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 1)

    def test_12_html_comment_is_removed(self):
        text = "<!-- Hidden. Text. -->\nShown.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 1)

    def test_13_code_span_does_not_split_on_its_own_periods(self):
        text = "Run `a. b. c` now.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 1)

    def test_14_eg_abbreviation_does_not_split(self):
        text = "Use tools, e.g. grep. Then stop.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 2)

    def test_15_vs_abbreviation_does_not_split(self):
        text = "A vs. B is fine.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 1)

    def test_16_digit_dot_digit_does_not_split(self):
        text = "Pinned to 2.1.280 today. Next.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 2)

    def test_17_url_trailing_punctuation_still_terminates(self):
        text = "See https://example.com/a.b. Then go.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 2)

    def test_18_markdown_link_target_is_dropped_before_splitting(self):
        text = "[the doc](https://x.io/y.md). Next.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 2)

    def test_19_bold_markers_do_not_block_the_terminator(self):
        text = "**Bold sentence.** Next one.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 2)

    def test_20_parenthetical_close_does_not_block_the_terminator(self):
        text = "(Parenthetical.) After.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 2)

    def test_21_blockquote_paragraph_and_list_item(self):
        text = "> Quoted one. Quoted two.\n> - quoted item\n"
        self.assertEqual(instruction_lib.count_markdown(text), 3)

    def test_22_horizontal_rule_between_paragraphs(self):
        text = "Above.\n\n---\n\nBelow.\n"
        self.assertEqual(instruction_lib.count_markdown(text), 2)

    def test_23_mixed_terminators_and_ellipsis(self):
        text = "Why? Because! Done...\n"
        self.assertEqual(instruction_lib.count_markdown(text), 3)

    def test_24_non_ascii_letters_count(self):
        self.assertEqual(instruction_lib.count_markdown("Ōʻahu\n"), 1)

    def test_25_semicolons_do_not_split_pins_the_documented_residual(self):
        self.assertEqual(instruction_lib.count_markdown("One; two; three.\n"), 1)

    def test_27_empty_string_and_none_count_zero(self):
        self.assertEqual(instruction_lib.count_markdown(""), 0)
        self.assertEqual(instruction_lib.count_markdown(None), 0)


class TestSentenceCountIsInvariantToLayout(unittest.TestCase):
    """Reflowing, folding bullets or moving text into a heading must not
    lower the count -- this is the whole point of unit v2."""

    def test_g1_joining_paragraphs_does_not_change_the_count(self):
        joined = "Alpha one.\n\nBeta two.\n\nGamma three.\n"
        folded = "Alpha one.\nBeta two.\nGamma three.\n"
        self.assertEqual(instruction_lib.count_markdown(joined), 3)
        self.assertEqual(instruction_lib.count_markdown(folded), 3)

    def test_g2_folding_bullets_into_a_paragraph_does_not_change_the_count(self):
        bullets = "- Alpha one.\n- Beta two.\n"
        folded = "Alpha one. Beta two.\n"
        self.assertEqual(instruction_lib.count_markdown(bullets), 2)
        self.assertEqual(instruction_lib.count_markdown(folded), 2)

    def test_g3_moving_a_line_into_a_heading_does_not_change_the_count(self):
        para = "Pointer text here.\n"
        heading = "## Pointer text here.\n"
        self.assertEqual(instruction_lib.count_markdown(para), 1)
        self.assertEqual(instruction_lib.count_markdown(heading), 1)

    def test_g4_rewrapping_a_paragraph_does_not_change_the_count(self):
        text = "Alpha one is here. Beta two is here. Gamma three is here.\n"
        rewrapped = textwrap.fill(text, width=20) + "\n"
        self.assertEqual(instruction_lib.count_markdown(text), 3)
        self.assertEqual(instruction_lib.count_markdown(rewrapped), 3)

    def test_g5_rewrapping_an_unterminated_line_does_not_change_the_count(self):
        one_line = "Alpha one is here and Beta two\n"
        two_lines = "Alpha one is here\nand Beta two\n"
        self.assertEqual(instruction_lib.count_markdown(one_line), 1)
        self.assertEqual(instruction_lib.count_markdown(two_lines), 1)

    def test_g6_folding_bullets_ending_in_curly_quotes_does_not_change_the_count(self):
        # Code Review 2026-09-28: a closing class without ” ’ left `.”`
        # unterminated, so folding these two bullets read 2 -> 1.
        bullets = "- Say “Stop.”\n- Say ‘Go.’\n"
        folded = "Say “Stop.” Say ‘Go.’\n"
        self.assertEqual(instruction_lib.count_markdown(bullets), 2)
        self.assertEqual(instruction_lib.count_markdown(folded), 2)

    def test_g7_folding_a_bold_url_sentence_does_not_change_the_count(self):
        # Code Review 2026-09-28: the URL mask swallowed `.**`, taking the
        # sentence's own terminator with it.
        bullets = "- **See https://example.com.**\n- Then stop.\n"
        folded = "**See https://example.com.** Then stop.\n"
        self.assertEqual(instruction_lib.count_markdown(bullets), 2)
        self.assertEqual(instruction_lib.count_markdown(folded), 2)


class TestResidualUnterminatedBlockJoin(unittest.TestCase):
    """Pins the one documented gap: folding two UNTERMINATED list items into
    one paragraph lowers the count by one. Deliberate -- see the module
    docstring -- a change here must be a conscious decision, not a fix."""

    def test_folding_two_unterminated_list_items_lowers_the_count_by_one(self):
        bullets = "- Run tests\n- Check output\n"
        folded = "Run tests Check output\n"
        self.assertEqual(instruction_lib.count_markdown(bullets), 2)
        self.assertEqual(instruction_lib.count_markdown(folded), 1)


class TestHasImport(unittest.TestCase):
    def test_at_path_import_at_line_start_is_true(self):
        self.assertTrue(instruction_lib.has_import("@docs/x.md\n"))

    def test_at_sign_inside_a_fence_is_false(self):
        self.assertFalse(instruction_lib.has_import("```\n@x\n```\n"))

    def test_at_sign_mid_line_is_false(self):
        self.assertFalse(instruction_lib.has_import("email me @ noon\n"))


class TestCountDigest(unittest.TestCase):
    BLOCK = (
        "```\n"
        "Secrets  one. two.\n"
        "         TRAP: three.\n"
        "MCP live A B C\n"
        "```\n"
    )

    def test_count_is_4_with_fence_lines_present(self):
        self.assertEqual(instruction_lib.count_digest(self.BLOCK), 4)

    def test_count_is_4_without_the_trap_prefix(self):
        # The v1 TRAP: bias is gone -- v2 gives it no special treatment.
        block = self.BLOCK.replace("TRAP: ", "")
        self.assertEqual(instruction_lib.count_digest(block), 4)

    def test_count_is_4_without_fence_lines(self):
        block = self.BLOCK.replace("```\n", "")
        self.assertEqual(instruction_lib.count_digest(block), 4)

    def test_none_and_empty_count_zero(self):
        self.assertEqual(instruction_lib.count_digest(None), 0)
        self.assertEqual(instruction_lib.count_digest(""), 0)


class TestCountAlwaysRows(unittest.TestCase):
    INDEX_TEXT = (
        "## Always\n\n"
        "- [Title A](a.md) — hook a\n"
        "- [Title B](b.md) — hook b\n"
        "- [Title C](c.md) - hook c, no em dash\n\n"
        "## Topics\n\n"
        "- [Topic X](topics/x.md) — topic hook\n"
    )

    def test_two_level_counts_only_parsed_always_rows(self):
        self.assertEqual(
            instruction_lib.count_always_rows(self.INDEX_TEXT, "two-level"), 2
        )

    def test_flat_counts_every_well_formed_row_in_the_whole_index(self):
        self.assertEqual(
            instruction_lib.count_always_rows(self.INDEX_TEXT, "flat"), 3
        )

    def test_malformed_always_row_is_not_counted_in_two_level(self):
        # Row C has no em dash, so it is malformed (parsed: False) even though
        # it sits in the Always section -- it must not inflate the count.
        only_malformed = "## Always\n\n- [Title C](c.md) - hook c, no em dash\n"
        self.assertEqual(
            instruction_lib.count_always_rows(only_malformed, "two-level"), 0
        )


class TestUnitVersion(unittest.TestCase):
    def test_version_is_2(self):
        self.assertEqual(instruction_lib.UNIT_VERSION, 2)


if __name__ == "__main__":
    unittest.main()
