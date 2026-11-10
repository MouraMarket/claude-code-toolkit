import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import archive_lib


class TestParseFrontMatter(unittest.TestCase):

    def test_basic_block(self):
        text = "---\nstatus: shipped\nsubject: toolkit\n---\nbody text\n"
        self.assertEqual(archive_lib.parse_front_matter(text),
                         {"status": "shipped", "subject": "toolkit"})

    def test_inline_comment_is_stripped(self):
        text = "---\nstatus: live          # live | shipped | superseded\n---\n"
        self.assertEqual(archive_lib.parse_front_matter(text), {"status": "live"})

    def test_quoted_value_is_unquoted(self):
        text = '---\nsubject: "toolkit"\n---\n'
        self.assertEqual(archive_lib.parse_front_matter(text), {"subject": "toolkit"})

    def test_optional_archive_group_is_parsed(self):
        text = "---\nstatus: shipped\nsubject: toolkit\narchive_group: shipped-build-plans\n---\n"
        self.assertEqual(archive_lib.parse_front_matter(text),
                         {"status": "shipped", "subject": "toolkit",
                          "archive_group": "shipped-build-plans"})

    def test_missing_closing_delimiter_is_malformed(self):
        text = "---\nstatus: shipped\nsubject: toolkit\nno closing fence\n"
        self.assertEqual(archive_lib.parse_front_matter(text), {})

    def test_missing_opening_delimiter_is_malformed(self):
        text = "status: shipped\nsubject: toolkit\n"
        self.assertEqual(archive_lib.parse_front_matter(text), {})

    def test_empty_string_is_malformed(self):
        self.assertEqual(archive_lib.parse_front_matter(""), {})

    def test_non_string_input_never_raises(self):
        self.assertEqual(archive_lib.parse_front_matter(None), {})
        self.assertEqual(archive_lib.parse_front_matter(12345), {})

    def test_blank_lines_inside_block_are_skipped(self):
        text = "---\nstatus: shipped\n\nsubject: toolkit\n---\n"
        self.assertEqual(archive_lib.parse_front_matter(text),
                         {"status": "shipped", "subject": "toolkit"})


class TestLoadManifest(unittest.TestCase):

    def test_missing_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(archive_lib.load_manifest(tmp), {})

    def test_corrupt_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, archive_lib.MANIFEST_NAME), "w") as h:
                h.write("{not json")
            self.assertEqual(archive_lib.load_manifest(tmp), {})

    def test_non_dict_json_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, archive_lib.MANIFEST_NAME), "w") as h:
                json.dump(["a", "b"], h)
            self.assertEqual(archive_lib.load_manifest(tmp), {})

    def test_valid_manifest_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = {"2026-08-19-lens-analysis": {"status": "shipped", "subject": "toolkit"}}
            with open(os.path.join(tmp, archive_lib.MANIFEST_NAME), "w") as h:
                json.dump(data, h)
            self.assertEqual(archive_lib.load_manifest(tmp), data)


class TestIsWithin(unittest.TestCase):

    def test_identical_path_is_within(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(archive_lib.is_within(tmp, tmp))

    def test_child_path_is_within(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = os.path.join(tmp, "a", "b.md")
            os.makedirs(os.path.dirname(child))
            open(child, "w").close()
            self.assertTrue(archive_lib.is_within(child, tmp))

    def test_sibling_with_shared_prefix_is_not_within(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = os.path.join(tmp, "specs")
            sibling = os.path.join(tmp, "specs-other", "x.md")
            os.makedirs(parent)
            os.makedirs(os.path.dirname(sibling))
            open(sibling, "w").close()
            self.assertFalse(archive_lib.is_within(sibling, parent))

    def test_unrelated_path_is_not_within(self):
        with tempfile.TemporaryDirectory() as tmp:
            other = tempfile.mkdtemp()
            try:
                self.assertFalse(archive_lib.is_within(other, tmp))
            finally:
                os.rmdir(other)


class TestDestinationFor(unittest.TestCase):

    def test_matches_the_convention(self):
        dest = archive_lib.destination_for(
            "/x/2026-08-12-session-claim-design.md", "toolkit", "shipped-specs", "/x")
        self.assertEqual(dest, os.path.join(
            "/x", "archive", "202608 - toolkit - shipped-specs",
            "20260812 - toolkit - session-claim-design.md"))

    def test_directory_basename_keeps_no_extension(self):
        dest = archive_lib.destination_for(
            "/x/2026-08-19-lens-analysis", "toolkit", "shipped-specs", "/x")
        self.assertEqual(dest, os.path.join(
            "/x", "archive", "202608 - toolkit - shipped-specs",
            "20260819 - toolkit - lens-analysis"))

    def test_slug_keeps_internal_dashes(self):
        dest = archive_lib.destination_for(
            "/x/2026-08-12-a-b-c.md", "toolkit", "shipped-specs", "/x")
        self.assertTrue(dest.endswith("20260812 - toolkit - a-b-c.md"))

    def test_deterministic_for_the_same_inputs(self):
        args = ("/x/2026-08-12-a.md", "toolkit", "shipped-specs", "/x")
        self.assertEqual(archive_lib.destination_for(*args), archive_lib.destination_for(*args))

    def test_no_date_prefix_raises(self):
        with self.assertRaises(ValueError):
            archive_lib.destination_for("/x/no-date-here.md", "toolkit", "shipped-specs", "/x")


class TestRefusesOverwrite(unittest.TestCase):

    def test_existing_destination_refuses(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "already-here.md")
            open(dest, "w").close()
            self.assertTrue(archive_lib.refuses_overwrite(dest))

    def test_missing_destination_does_not_refuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "not-here-yet.md")
            self.assertFalse(archive_lib.refuses_overwrite(dest))


class ClassifyTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.specs_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name, body="", days_ago=None):
        path = os.path.join(self.specs_dir, name)
        with open(path, "w") as handle:
            handle.write(body)
        if days_ago is not None:
            stamp = (datetime.now().timestamp()) - days_ago * 86400
            os.utime(path, (stamp, stamp))
        return path

    def md_body(self, status, subject="toolkit", extra=""):
        return f"---\nstatus: {status}\nsubject: {subject}\n{extra}---\nbody\n"


class TestClassifyMdVsManifestSplit(ClassifyTestCase):

    def test_md_path_never_consults_the_manifest(self):
        # Manifest has a fully valid, old-enough, archivable entry keyed to
        # this exact basename -- but the .md file's OWN front matter has
        # nothing, so classify() must ignore the manifest entirely.
        name = "2026-01-01-fully-manifested.md"
        manifest = {name: {"status": "shipped", "subject": "toolkit"}}
        path = self.write(name, body="no front matter here", days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, manifest, min_age_days=14)
        self.assertEqual(verdict.action, "undeclared")

    def test_non_md_path_never_consults_front_matter(self):
        # The file's own content LOOKS like valid front matter, but it is not
        # .md, so only the manifest may be consulted -- and the manifest is
        # empty, so this must be undeclared.
        name = "2026-01-01-lens-analysis.json"
        body = "---\nstatus: shipped\nsubject: toolkit\n---\n"
        path = self.write(name, body=body, days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "undeclared")

    def test_non_md_path_uses_manifest_when_present(self):
        name = "2026-01-01-lens-analysis.json"
        manifest = {name: {"status": "shipped", "subject": "toolkit"}}
        path = self.write(name, body="{}", days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, manifest, min_age_days=14)
        self.assertEqual(verdict.action, "archive")


class TestClassifyStatus(ClassifyTestCase):

    def test_live_is_kept_regardless_of_age(self):
        path = self.write("2020-01-01-ancient.md", self.md_body("live"), days_ago=3000)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "keep")
        self.assertIsNone(verdict.destination)

    def test_no_status_is_undeclared(self):
        path = self.write("2026-01-01-nostatus.md", "---\nsubject: toolkit\n---\n", days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "undeclared")

    def test_unknown_status_value_is_undeclared(self):
        path = self.write("2026-01-01-wip.md", self.md_body("wip"), days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "undeclared")

    def test_missing_subject_is_undeclared(self):
        path = self.write("2026-01-01-nosubject.md", "---\nstatus: shipped\n---\n", days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "undeclared")

    def test_shipped_and_old_enough_is_archivable(self):
        path = self.write("2026-01-01-shipped.md", self.md_body("shipped"), days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "archive")
        self.assertIsNotNone(verdict.destination)

    def test_subject_of_only_punctuation_is_undeclared(self):
        # D10: a subject that slugs to "" ("subject has no usable
        # characters") can build no destination, so classify() must catch
        # this before ever calling destination_for().
        path = self.write("2026-01-01-punct.md", self.md_body("shipped", subject="—"),
                           days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "undeclared")


class TestClassifyAgeBoundary(ClassifyTestCase):

    def test_exactly_at_the_boundary_is_not_archivable(self):
        now = datetime(2026, 8, 25, 12, 0, 0)
        path = self.write("2026-08-11-boundary.md", self.md_body("shipped"))
        stamp = (now.timestamp()) - 14 * 86400
        os.utime(path, (stamp, stamp))
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14, now=now)
        self.assertEqual(verdict.action, "keep")

    def test_one_second_past_the_boundary_is_archivable(self):
        now = datetime(2026, 8, 25, 12, 0, 0)
        path = self.write("2026-08-11-just-past.md", self.md_body("shipped"))
        stamp = (now.timestamp()) - 14 * 86400 - 1
        os.utime(path, (stamp, stamp))
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14, now=now)
        self.assertEqual(verdict.action, "archive")


class TestClassifySafetyRefusals(ClassifyTestCase):

    def test_handover_named_file_is_unmanaged_even_when_shipped(self):
        name = "2026-01-01-something-handover.md"
        path = self.write(name, self.md_body("shipped"), days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "unmanaged")
        self.assertIsNone(verdict.destination)

    def test_hidden_path_is_unmanaged_not_undeclared(self):
        # UNDECLARED is a worklist of specs still owed a status. Another tool's
        # state directory can never be given one, so it must not land there.
        path = os.path.join(self.specs_dir, ".remember")
        os.makedirs(path)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "unmanaged")
        self.assertIn("hidden", verdict.reason)
        self.assertIsNone(verdict.destination)

    def test_hidden_markdown_file_is_unmanaged_even_when_shipped(self):
        # The dotfile check sits ahead of the front-matter read, so a declared
        # status inside a hidden file cannot pull it back into scope.
        path = self.write(".2026-01-01-hidden.md", self.md_body("shipped"), days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "unmanaged")
        self.assertIsNone(verdict.destination)

    def test_symlink_is_unmanaged_regardless_of_target_content(self):
        target = self.write("2026-01-01-target.md", self.md_body("shipped"), days_ago=100)
        link = os.path.join(self.specs_dir, "2026-01-01-link.md")
        os.symlink(target, link)
        verdict = archive_lib.classify(link, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "unmanaged")

    def test_path_outside_specs_dir_is_unmanaged(self):
        # A sibling directory sharing a name prefix must not be mistaken for
        # a subdirectory of specs_dir.
        sibling_dir = self.specs_dir + "-other"
        os.makedirs(sibling_dir)
        path = os.path.join(sibling_dir, "2026-01-01-outsider.md")
        with open(path, "w") as h:
            h.write(self.md_body("shipped"))
        stamp = datetime.now().timestamp() - 100 * 86400
        os.utime(path, (stamp, stamp))
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "unmanaged")

    def test_traversal_subject_is_slugged_into_one_folder_inside_specs_dir(self):
        # D10: destination_for() now slugs the subject before it ever touches
        # a path, so containment holds by construction rather than depending
        # on is_within() catching a traversal after the fact. The traversal
        # collapses to one harmless "etc" folder -- classify()'s own
        # is_within(destination, ...) check stays in place as a second line
        # of defence even though no subject can reach it any more.
        traversal = "../" * 30 + "etc"
        body = f"---\nstatus: shipped\nsubject: {traversal}\n---\nbody\n"
        path = self.write("2026-01-01-traversal.md", body, days_ago=100)
        verdict = archive_lib.classify(path, self.specs_dir, {}, min_age_days=14)
        self.assertEqual(verdict.action, "archive")
        self.assertTrue(archive_lib.is_within(verdict.destination, self.specs_dir))
        archive_root = os.path.join(self.specs_dir, archive_lib.ARCHIVE_DIR_NAME)
        parent = os.path.dirname(verdict.destination)
        self.assertEqual(os.path.dirname(parent), archive_root)
        for part in verdict.destination.split(os.sep):
            self.assertNotEqual(part, "..")


class TestSlugify(unittest.TestCase):

    def test_already_slug_shaped_subject_is_unchanged(self):
        for value in ("toolkit", "brand-term-builder", "data-desk-meta-b4-registry"):
            self.assertEqual(archive_lib.slugify(value), value)

    def test_uppercase_is_lowered(self):
        self.assertEqual(archive_lib.slugify("TOOLKIT"), "toolkit")

    def test_run_of_non_alnum_chars_collapses_to_one_hyphen(self):
        self.assertEqual(archive_lib.slugify("Hello, World!"), "hello-world")
        self.assertEqual(archive_lib.slugify("a__b  c"), "a-b-c")

    def test_leading_and_trailing_hyphens_are_stripped(self):
        self.assertEqual(archive_lib.slugify("--abc--"), "abc")

    def test_slash_in_subject_collapses_to_one_hyphen(self):
        # This is what makes a folder path segment "one level": the slug
        # itself can never contain a path separator (D10).
        self.assertEqual(archive_lib.slugify("clients/subject"), "clients-subject")

    def test_90_char_prose_subject_truncates_to_40_with_no_trailing_hyphen(self):
        prose = ("This is a very long prose style subject line someone typed "
                  "instead of a real slug!!!!!!!!!")
        self.assertEqual(len(prose), 90)
        slug = archive_lib.slugify(prose)
        self.assertLessEqual(len(slug), 40)
        self.assertFalse(slug.endswith("-"))

    def test_subject_of_only_punctuation_is_empty(self):
        self.assertEqual(archive_lib.slugify("—"), "")

    def test_empty_string_is_empty(self):
        self.assertEqual(archive_lib.slugify(""), "")


class TestStripFencedBlocks(unittest.TestCase):

    def test_backtick_fence_is_removed_inclusive(self):
        text = "before\n```\ncode\nmore code\n```\nafter\n"
        self.assertEqual(archive_lib.strip_fenced_blocks(text), "before\nafter\n")

    def test_tilde_fence_is_removed_inclusive(self):
        text = "before\n~~~\ncode\n~~~\nafter\n"
        self.assertEqual(archive_lib.strip_fenced_blocks(text), "before\nafter\n")

    def test_unclosed_fence_removes_to_eof(self):
        text = "before\n```\ncode\nmore code\n"
        self.assertEqual(archive_lib.strip_fenced_blocks(text), "before\n")

    def test_multiple_fenced_blocks_are_each_removed(self):
        text = "a\n```\none\n```\nb\n```\ntwo\n```\nc\n"
        self.assertEqual(archive_lib.strip_fenced_blocks(text), "a\nb\nc\n")

    def test_no_fence_is_unchanged(self):
        text = "just prose\nno fences here\n"
        self.assertEqual(archive_lib.strip_fenced_blocks(text), text)


class TestIsMissionShaped(unittest.TestCase):

    def test_backlog_heading_at_any_level_is_mission_shaped(self):
        self.assertTrue(archive_lib.is_mission_shaped("## Backlog\nsome text\n"))
        self.assertTrue(archive_lib.is_mission_shaped("### backlog\nsome text\n"))

    def test_mvp_heading_case_insensitive_is_mission_shaped(self):
        self.assertTrue(archive_lib.is_mission_shaped("## MVP\ngoal\n"))
        self.assertTrue(archive_lib.is_mission_shaped("## mvp\ngoal\n"))

    def test_word_boundary_excludes_a_prefix_match(self):
        self.assertFalse(archive_lib.is_mission_shaped("## MVPish\nnot really\n"))

    def test_no_matching_heading_is_not_mission_shaped(self):
        text = "# Plan\n## Tasks\nsome work\n"
        self.assertFalse(archive_lib.is_mission_shaped(text))

    def test_heading_inside_a_fenced_block_is_ignored(self):
        text = "# Plan\n```\n## Backlog\nexample only\n```\nno real backlog here\n"
        self.assertFalse(archive_lib.is_mission_shaped(text))


WAITING_TABLE_ALL_CLOSED = (
    "## Waiting on a human\n"
    "| Item | Status |\n"
    "| a | **done 2026-09-22** — x |\n"
    "| b | ✅ done |\n"
    "| c | no-action |\n"
    "| d | decided, no action |\n"
    "| e | not applicable |\n"
)


class TestHumanRowsState(unittest.TestCase):

    def test_no_heading_at_all_is_absent(self):
        text = "# Plan\nSome prose with no relevant heading.\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "absent")

    def test_section_with_no_table_is_open(self):
        text = "## Waiting on a human\nJust prose, no table.\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_table_with_no_status_column_is_open(self):
        text = "## Waiting on a human\n| Item | Owner |\n| a | bob |\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_header_only_table_is_open(self):
        text = "## Waiting on a human\n| Item | Status |\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_every_closed_vocabulary_word_makes_the_section_clear(self):
        state, _ = archive_lib.human_rows_state(WAITING_TABLE_ALL_CLOSED)
        self.assertEqual(state, "clear")

    def test_single_row_em_dash_is_open(self):
        text = "## Waiting on a human\n| Item | Status |\n| a | — |\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_single_row_outstanding_is_open(self):
        text = "## Waiting on a human\n| Item | Status |\n| a | **outstanding** |\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_single_row_open_by_design_is_open(self):
        text = "## Waiting on a human\n| Item | Status |\n| a | open by design |\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_single_row_reframed_is_open(self):
        text = "## Waiting on a human\n| Item | Status |\n| a | reframed |\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_status_column_not_last_is_read_correctly(self):
        text = "## Waiting on a human\n| Status | Item |\n| done | a |\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "clear")

    def test_waiting_heading_inside_a_fence_with_no_real_section_is_absent(self):
        text = ("Some prose.\n\n```\n" + WAITING_TABLE_ALL_CLOSED +
                "```\n\nMore prose, no real heading outside the fence.\n")
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "absent")

    def test_two_sections_one_clear_one_open_is_open(self):
        text = ("## Waiting on a human\n| Item | Status |\n| a | done |\n\n"
                "## Waiting on a human (round two)\nNothing here, no table.\n")
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_section_stops_at_next_same_or_higher_heading(self):
        # The "## Other" table below is only picked up if the section
        # boundary is computed wrong -- it must not rescue the (tableless)
        # "Waiting on a human" section it follows.
        text = ("## Waiting on a human\nSome text but no table here.\n\n"
                "## Other\n| Item | Status |\n| a | done |\n")
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_explanatory_table_plus_status_table_in_one_section_is_open(self):
        text = ("## Waiting on a human\n| Item | Owner |\n| a | bob |\n\n"
                "| Item | Status |\n| a | done |\n")
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")

    def test_two_all_done_status_tables_in_one_section_is_clear(self):
        text = ("## Waiting on a human\n| Item | Status |\n| a | done |\n\n"
                "| Thing | Status |\n| b | done |\n")
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "clear")

    def test_separator_row_is_skipped_not_read_as_data(self):
        # If the separator were misread as a data row, its "---" cells
        # would not match the closed vocabulary and this would read open.
        text = ("## Waiting on a human\n| Item | Status |\n|---|:---:|\n"
                "| a | done |\n| b | done |\n")
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "clear")

    def test_data_row_missing_its_status_cell_is_open(self):
        text = "## Waiting on a human\n| Item | Status |\n| a |\n"
        state, _ = archive_lib.human_rows_state(text)
        self.assertEqual(state, "open")


class TestUnfinishedWorkState(unittest.TestCase):

    TABLE = ("| Item | Status |\n|---|---|\n"
             "| a | shipped 2026-10-01 |\n| b | **done** |\n"
             "| c | merged |\n| d | superseded |\n")

    def state(self, text):
        return archive_lib.unfinished_work_state(text)[0]

    def test_dash_unticked_box_is_open(self):
        self.assertEqual(self.state("intro\n- [ ] Implement export\n"), "open")

    def test_numbered_unticked_box_is_open(self):
        self.assertEqual(self.state("1. [ ] step\n"), "open")

    def test_star_unticked_box_is_open(self):
        self.assertEqual(self.state("* [ ] x\n"), "open")

    def test_only_ticked_boxes_is_clear(self):
        self.assertEqual(self.state("- [x] done\n- [X] also\n"), "clear")

    def test_unticked_box_inside_fence_is_clear(self):
        self.assertEqual(self.state("```\n- [ ] x\n```\n"), "clear")

    def test_backlog_all_closed_is_clear(self):
        self.assertEqual(self.state("## Backlog\n" + self.TABLE), "clear")

    def test_backlog_with_open_row_is_open(self):
        text = "## Backlog\n" + self.TABLE + "| e | open |\n"
        self.assertEqual(self.state(text), "open")

    def test_backlog_with_in_progress_row_is_open(self):
        text = "## Backlog\n" + self.TABLE + "| e | in progress \u2014 lane B |\n"
        self.assertEqual(self.state(text), "open")

    def test_backlog_table_without_status_column_is_open(self):
        text = "## Backlog\n| Item | Note |\n|---|---|\n| a | b |\n"
        self.assertEqual(self.state(text), "open")

    def test_backlog_bullets_without_table_is_open(self):
        self.assertEqual(self.state("## Backlog\n- one\n- two\n"), "open")

    def test_nested_subheading_table_with_open_row_is_open(self):
        text = ("## Backlog\nIntro.\n### Rows\n| Item | Status |\n|---|---|\n"
                "| a | shipped |\n| b | open |\n")
        self.assertEqual(self.state(text), "open")

    def test_no_backlog_no_boxes_is_clear(self):
        self.assertEqual(self.state("# Plan\nJust prose.\n"), "clear")


class TestFlipVerdict(ClassifyTestCase):

    def test_stale_days_zero_skips(self):
        path = self.write("2026-01-01-a.md", self.md_body("live"), days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=0)
        self.assertEqual(verdict.action, "skip")
        self.assertIsNone(verdict.mtime)

    def test_stale_days_negative_skips(self):
        path = self.write("2026-01-01-a.md", self.md_body("live"), days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=-5)
        self.assertEqual(verdict.action, "skip")

    def test_handover_named_file_skips(self):
        name = "2026-01-01-something-handover.md"
        path = self.write(name, self.md_body("live"), days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_dotfile_skips(self):
        path = self.write(".2026-01-01-hidden.md", self.md_body("live"), days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_symlink_skips(self):
        target = self.write("2026-01-01-target.md", self.md_body("live"), days_ago=100)
        link = os.path.join(self.specs_dir, "2026-01-01-link.md")
        os.symlink(target, link)
        verdict = archive_lib.flip_verdict(link, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_path_outside_specs_dir_skips(self):
        sibling_dir = self.specs_dir + "-other"
        os.makedirs(sibling_dir)
        path = os.path.join(sibling_dir, "2026-01-01-outsider.md")
        with open(path, "w") as h:
            h.write(self.md_body("live"))
        stamp = datetime.now().timestamp() - 100 * 86400
        os.utime(path, (stamp, stamp))
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_non_markdown_file_skips(self):
        path = self.write("2026-01-01-a.json", self.md_body("live"), days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_unreadable_path_skips(self):
        path = os.path.join(self.specs_dir, "2026-01-01-adir.md")
        os.makedirs(path)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_status_not_live_skips(self):
        path = self.write("2026-01-01-shipped.md", self.md_body("shipped"), days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_missing_subject_skips(self):
        path = self.write("2026-01-01-nosubject.md", "---\nstatus: live\n---\n", days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_empty_slug_subject_skips(self):
        body = "---\nstatus: live\nsubject: —\n---\n"
        path = self.write("2026-01-01-punct.md", body, days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_not_old_enough_skips(self):
        path = self.write("2026-01-01-fresh.md", self.md_body("live"), days_ago=5)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_exactly_at_the_boundary_skips(self):
        now = datetime(2026, 8, 25, 12, 0, 0)
        path = self.write("2026-08-11-boundary.md", self.md_body("live"))
        stamp = now.timestamp() - 14 * 86400
        os.utime(path, (stamp, stamp))
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=14, now=now)
        self.assertEqual(verdict.action, "skip")

    def test_human_rows_open_skips(self):
        body = ("---\nstatus: live\nsubject: toolkit\n---\n"
                 "## Waiting on a human\n| Item | Status |\n| a | open |\n")
        path = self.write("2026-01-01-open-rows.md", body, days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_mission_shaped_absent_skips_lowercase_backlog(self):
        body = ("---\nstatus: live\nsubject: toolkit\n---\n"
                 "## Backlog\n### Blocked on the owner\nSome text.\n")
        path = self.write("2026-01-01-mission.md", body, days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_mission_shaped_absent_skips_uppercase_backlog(self):
        body = ("---\nstatus: live\nsubject: toolkit\n---\n"
                 "## BACKLOG\n### Blocked on the owner\nSome text.\n")
        path = self.write("2026-01-01-mission-upper.md", body, days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")

    def test_non_mission_file_with_no_section_flips(self):
        body = "---\nstatus: live\nsubject: toolkit\n---\nJust prose, no special headings.\n"
        path = self.write("2026-01-01-flippable.md", body, days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "flip")
        self.assertIsNotNone(verdict.mtime)

    def test_unticked_task_blocks_flip(self):
        body = ("---\nstatus: live\nsubject: toolkit\n---\n"
                "# Plan\n- [ ] Implement export\n")
        path = self.write("2026-01-01-unfinished.md", body, days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "skip")
        self.assertIn("unfinished work", verdict.reason)

    def test_ticked_task_still_flips(self):
        body = ("---\nstatus: live\nsubject: toolkit\n---\n"
                "# Plan\n- [x] Implement export\n")
        path = self.write("2026-01-01-finished.md", body, days_ago=100)
        verdict = archive_lib.flip_verdict(path, self.specs_dir, stale_days=21)
        self.assertEqual(verdict.action, "flip")


class TestRewriteStatusShipped(unittest.TestCase):

    def test_crlf_file_keeps_crlf(self):
        text = "---\r\nstatus: live\r\nsubject: toolkit\r\n---\r\nbody\r\n"
        result = archive_lib.rewrite_status_shipped(text, "2026-09-23", 21)
        self.assertIsNotNone(result)
        # No bare LF anywhere -- every line ending is still \r\n.
        self.assertIsNone(re.search(r'(?<!\r)\n', result))
        self.assertIn("status: shipped # auto-flipped 2026-09-23", result)

    def test_quoted_live_is_rewritten(self):
        text = '---\nstatus: "live"\nsubject: toolkit\n---\nbody\n'
        result = archive_lib.rewrite_status_shipped(text, "2026-09-23", 21)
        self.assertIsNotNone(result)
        expected_line = ("status: shipped # auto-flipped 2026-09-23 by "
                          "claude-spec-archive: idle >21d, set back to live to keep")
        self.assertIn(expected_line, result)
        meta = archive_lib.parse_front_matter(result)
        self.assertEqual(meta.get("status"), "shipped")
        self.assertEqual(meta.get("subject"), "toolkit")

    def test_inline_comment_live_is_rewritten(self):
        text = "---\nstatus: live # live | shipped | superseded\nsubject: toolkit\n---\nbody\n"
        result = archive_lib.rewrite_status_shipped(text, "2026-09-23", 21)
        self.assertIsNotNone(result)
        meta = archive_lib.parse_front_matter(result)
        self.assertEqual(meta.get("status"), "shipped")
        self.assertEqual(meta.get("subject"), "toolkit")

    def test_body_status_live_line_is_untouched(self):
        text = ("---\nstatus: live\nsubject: toolkit\n---\n"
                "Some text.\nstatus: live\nMore text.\n")
        result = archive_lib.rewrite_status_shipped(text, "2026-09-23", 21)
        self.assertIsNotNone(result)
        self.assertIn("\nSome text.\nstatus: live\nMore text.\n", result)

    def test_missing_status_live_line_returns_none(self):
        text = "---\nstatus: shipped\nsubject: toolkit\n---\nbody\n"
        result = archive_lib.rewrite_status_shipped(text, "2026-09-23", 21)
        self.assertIsNone(result)

    def test_unclosed_front_matter_returns_none(self):
        text = "---\nstatus: live\nsubject: toolkit\nno closing fence\n"
        result = archive_lib.rewrite_status_shipped(text, "2026-09-23", 21)
        self.assertIsNone(result)

    def test_only_the_first_matching_line_is_replaced(self):
        text = "---\nstatus: live\nstatus: live\nsubject: toolkit\n---\nbody\n"
        result = archive_lib.rewrite_status_shipped(text, "2026-09-23", 21)
        self.assertIsNotNone(result)
        self.assertEqual(result.count("auto-flipped"), 1)
        self.assertIn("\nstatus: live\nsubject: toolkit\n", result)


if __name__ == "__main__":
    unittest.main()
