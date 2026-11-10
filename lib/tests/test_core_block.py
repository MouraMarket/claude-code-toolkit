#!/usr/bin/env python3
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import core_block


def wrap(start, body, end):
    return "intro\n%s\n%s\n%s\ntail\n" % (start, body, end)


class TestExtract(unittest.TestCase):
    def test_digest_markers_extract(self):
        text = wrap(core_block.DIGEST_START, "row one\nrow two", core_block.DIGEST_END)
        self.assertEqual(core_block.extract(text), "row one\nrow two")

    def test_core_markers_extract_identically(self):
        digest = wrap(core_block.DIGEST_START, "row one\nrow two", core_block.DIGEST_END)
        core = wrap(core_block.CORE_START, "row one\nrow two", core_block.CORE_END)
        self.assertEqual(core_block.extract(core), core_block.extract(digest))

    def test_the_earliest_pair_wins_regardless_of_kind(self):
        # Position decides, not marker kind.
        digest_first = (wrap(core_block.DIGEST_START, "real", core_block.DIGEST_END)
                        + wrap(core_block.CORE_START, "later", core_block.CORE_END))
        self.assertEqual(core_block.extract(digest_first), "real")
        self.assertEqual(core_block.marker_label(digest_first), "DIGEST")

        core_first = (wrap(core_block.CORE_START, "real", core_block.CORE_END)
                      + wrap(core_block.DIGEST_START, "later", core_block.DIGEST_END))
        self.assertEqual(core_block.extract(core_first), "real")
        self.assertEqual(core_block.marker_label(core_first), "CORE")

    def test_a_core_pair_in_a_body_code_fence_does_not_beat_the_real_block(self):
        # THE finding this rule exists for, measured through the real hook on the
        # pre-split draft: a CORE pair anywhere outranked the file's actual
        # digest, so a document explaining the Core contract hijacked what got
        # injected into every session. The design spec itself prints this pair
        # inside a fence, and slice 3 writes Core blocks into files whose bodies
        # discuss Core blocks.
        # The fence is built from chr(96) on purpose: a literal triple-backtick
        # inside this string would close the plan's own markdown code block and
        # hand the executor a truncated file.
        fence = chr(96) * 3
        text = ("intro\n"
                + wrap(core_block.DIGEST_START, "the real digest", core_block.DIGEST_END)
                + "\n## The Core contract\n\n" + fence + "markdown\n"
                + core_block.CORE_START + "\n...the rules that must fire...\n"
                + core_block.CORE_END + "\n" + fence + "\n")
        self.assertEqual(core_block.extract(text), "the real digest")
        self.assertEqual(core_block.marker_label(text), "DIGEST")

    def test_an_empty_earlier_block_falls_through_to_a_populated_later_one(self):
        # The pre-split draft returned `body or None` on the FIRST match, so an
        # empty CORE block above a populated DIGEST block reported "no block at
        # all" on a file that visibly has one — and render_broken then told the
        # reader to add markers the file already carries.
        text = (wrap(core_block.CORE_START, "   ", core_block.CORE_END)
                + wrap(core_block.DIGEST_START, "the real digest", core_block.DIGEST_END))
        self.assertEqual(core_block.extract(text), "the real digest")
        self.assertEqual(core_block.marker_label(text), "DIGEST")

    def test_no_markers_returns_none(self):
        self.assertIsNone(core_block.extract("a document with no markers"))
        self.assertIsNone(core_block.marker_label("a document with no markers"))

    def test_unclosed_marker_returns_none_not_the_whole_document(self):
        # Load-bearing: a deleted END marker must degrade to no output, never to
        # dumping a 300-line reference doc into every session's context.
        text = "intro\n%s\nrow one\nrow two\n" % core_block.CORE_START
        self.assertIsNone(core_block.extract(text))

    def test_empty_block_returns_none(self):
        self.assertIsNone(core_block.extract(wrap(core_block.CORE_START, "   ", core_block.CORE_END)))

    def test_falsy_input_returns_none(self):
        self.assertIsNone(core_block.extract(None))
        self.assertIsNone(core_block.extract(""))


class TestMarkerLabel(unittest.TestCase):
    def test_digest_only(self):
        self.assertEqual(core_block.marker_label(
            wrap(core_block.DIGEST_START, "x", core_block.DIGEST_END)), "DIGEST")

    def test_core_only(self):
        self.assertEqual(core_block.marker_label(
            wrap(core_block.CORE_START, "x", core_block.CORE_END)), "CORE")

    def test_falsy_input(self):
        self.assertIsNone(core_block.marker_label(None))
        self.assertIsNone(core_block.marker_label(""))

    def test_label_names_the_pair_whose_body_extract_returned(self):
        # These two must never disagree: a notice that says CORE about a DIGEST
        # block sends the reader to markers that are not in the file. Asserted
        # in BOTH orders, because a label derived from a second, independent
        # scan agrees with extract() by luck whenever one kind is absent.
        for first, second, body, label in (
            (core_block.DIGEST_START, core_block.DIGEST_END, "d", "DIGEST"),
            (core_block.CORE_START, core_block.CORE_END, "c", "CORE"),
        ):
            text = wrap(first, body, second) + wrap(
                core_block.CORE_START if label == "DIGEST" else core_block.DIGEST_START,
                "later",
                core_block.CORE_END if label == "DIGEST" else core_block.DIGEST_END)
            self.assertEqual(core_block.extract(text), body)
            self.assertEqual(core_block.marker_label(text), label)


class TestBlockCount(unittest.TestCase):
    """The escape hatch: more than one pair is ambiguous, and must be sayable.

    Position-based precedence is not airtight — a documentation fence placed
    ABOVE the real block still wins — so the rule is backed by a count the
    caller can warn on rather than by a silent pick.
    """

    def test_zero_for_no_markers_and_falsy_input(self):
        self.assertEqual(core_block.block_count("plain document"), 0)
        self.assertEqual(core_block.block_count(None), 0)
        self.assertEqual(core_block.block_count(""), 0)

    def test_one_for_a_single_pair_of_either_kind(self):
        self.assertEqual(core_block.block_count(
            wrap(core_block.DIGEST_START, "x", core_block.DIGEST_END)), 1)
        self.assertEqual(core_block.block_count(
            wrap(core_block.CORE_START, "x", core_block.CORE_END)), 1)

    def test_counts_both_kinds_together(self):
        text = (wrap(core_block.DIGEST_START, "a", core_block.DIGEST_END)
                + wrap(core_block.CORE_START, "b", core_block.CORE_END))
        self.assertEqual(core_block.block_count(text), 2)

    def test_an_empty_pair_still_counts(self):
        # extract() skips an empty pair; the count must not, or a file with a
        # stray empty pair above its real block reads as unambiguous.
        text = (wrap(core_block.CORE_START, "", core_block.CORE_END)
                + wrap(core_block.DIGEST_START, "real", core_block.DIGEST_END))
        self.assertEqual(core_block.block_count(text), 2)

    def test_an_unclosed_marker_is_not_a_block(self):
        self.assertEqual(core_block.block_count(
            "intro\n%s\nrow\n" % core_block.CORE_START), 0)


class TestDanglingRefs(unittest.TestCase):
    def test_flags_see_below_and_see_above(self):
        self.assertTrue(core_block.dangling_refs("The rule is strict; see below."))
        self.assertTrue(core_block.dangling_refs("As see above, this holds."))

    def test_flags_section_sign(self):
        self.assertTrue(core_block.dangling_refs("Covered in §Testing."))

    def test_flags_qualified_below_and_above_forms(self):
        for phrase in ("described below", "detailed below", "listed below",
                       "noted above", "outlined below", "stated above",
                       "as noted earlier", "the section on hooks"):
            self.assertTrue(core_block.dangling_refs("x %s y" % phrase), phrase)

    def test_does_not_flag_bare_below_as_a_preposition(self):
        # A real CLAUDE.md contains "below the thresholds". A heuristic
        # that fires on ordinary prose is a heuristic that gets switched off.
        self.assertEqual(core_block.dangling_refs("Do not relay it below the thresholds."), [])
        self.assertEqual(core_block.dangling_refs("Anything above 85% gates."), [])

    def test_does_not_flag_a_pointer_to_another_file(self):
        # Pointing at a FILE is the contract working, not a dangling reference.
        self.assertEqual(core_block.dangling_refs("Read TOOLING.md for detail."), [])
        self.assertEqual(core_block.dangling_refs("Full reasoning: docs/decisions.md."), [])

    def test_returns_every_match_not_just_the_first(self):
        found = core_block.dangling_refs("see below, and also described above")
        self.assertEqual(len(found), 2)

    def test_empty_and_none_are_self_sufficient(self):
        self.assertEqual(core_block.dangling_refs(None), [])
        self.assertEqual(core_block.dangling_refs(""), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
