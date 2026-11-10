#!/usr/bin/env python3
"""Offline tests for the eval graders.

These make no API calls, so they run for free. That split is the point: a
grader nobody can trust makes the eval worse than no eval, because a false pass
reads as evidence. The graders are pure functions over text, so they can be
tested exactly, which means the only uncertainty left in an eval run is the
model's output, not the measurement of it.

Run: python3 evals/tests/test_eval_lib.py
"""

import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO_ROOT, "evals"))

import eval_lib  # noqa: E402
import run_eval  # noqa: E402


SAMPLE_OUTPUT = """## Name terms: Harbor Lane Inn

### Exact name
- Harbor Lane Inn
- Harborlane Inn

### Spacing variations
- HarborLaneInn
- Harbor Lane Inn Cafe

### Abbreviations
- HLI
- harborlane.example

### Plausible misspellings
- Harbour Lane Inn
- Harbor Lain Inn

This list covers the main variations guests search for.
"""


class TestExtractTerms(unittest.TestCase):

    def test_pulls_terms_and_drops_structure(self):
        terms = eval_lib.extract_terms(SAMPLE_OUTPUT)
        self.assertIn("Harbor Lane Inn", terms)
        self.assertIn("HLI", terms)
        self.assertNotIn("Exact name", terms)
        for term in terms:
            self.assertFalse(term.startswith("#"), term)

    def test_drops_trailing_prose(self):
        terms = eval_lib.extract_terms(SAMPLE_OUTPUT)
        self.assertNotIn("This list covers the main variations guests search for.", terms)

    def test_dedupes_case_insensitively(self):
        terms = eval_lib.extract_terms("- Harbor Lane\n- harbor lane\n- HARBOR LANE\n")
        self.assertEqual(len(terms), 1)

    def test_keeps_spacing_variants_as_distinct_terms(self):
        """'harbor lane' and 'harborlane' are different keywords.

        Regression guard: dedupe must not use the space-insensitive presence
        key, which would collapse a run-together spacing variant and risk a
        false failure on correct output.
        """
        terms = eval_lib.extract_terms("- harbor lane\n- harborlane\n")
        self.assertEqual(len(terms), 2, terms)

    def test_strips_match_type_notation(self):
        terms = eval_lib.extract_terms('- "harbor lane"\n- [harborlane]\n')
        self.assertIn("harbor lane", terms)
        self.assertIn("harborlane", terms)

    def test_strips_numbered_lists_and_trailing_notes(self):
        terms = eval_lib.extract_terms("1. Harbor Lane Inn (official name)\n2) HLI\n")
        self.assertIn("Harbor Lane Inn", terms)
        self.assertIn("HLI", terms)

    def test_empty_output_yields_no_terms(self):
        self.assertEqual(eval_lib.extract_terms(""), [])


class TestNormalize(unittest.TestCase):

    def test_ignores_case_punctuation_and_spacing(self):
        for variant in ("Cafe Lumen", "cafe lumen", "CAFE  LUMEN",
                        "Cafe-Lumen", "cafelumen", "Cafe' Lumen"):
            self.assertEqual(eval_lib.normalize(variant), "cafelumen", variant)


class TestStructureLines(unittest.TestCase):
    """Category labels are not keywords, whichever emphasis they use."""

    def test_single_asterisk_labels_are_not_terms(self):
        terms = eval_lib.extract_terms(
            "*Transpositions:*\n- Cafe Lumen Bistro\n*Brand word*\n")
        self.assertEqual(terms, ["Cafe Lumen Bistro"])

    def test_bold_labels_are_still_not_terms(self):
        terms = eval_lib.extract_terms("**Misspellings:**\n- Cafe Lumne\n")
        self.assertEqual(terms, ["Cafe Lumne"])

    def test_an_asterisk_bullet_is_still_a_term(self):
        """`* Cafe Lumen` is a list marker, not emphasis: it never closes."""
        self.assertEqual(eval_lib.extract_terms("* Cafe Lumen\n"), ["Cafe Lumen"])

    def test_an_emphasized_bullet_label_is_dropped_after_the_marker(self):
        """The label survives bullet-stripping, so it is re-checked afterwards."""
        terms = eval_lib.extract_terms("- *Transpositions:*\n- Cafe Lumne\n")
        self.assertEqual(terms, ["Cafe Lumne"])


class TestCoverageGraders(unittest.TestCase):

    def test_must_include_all_reports_each_miss(self):
        failures = eval_lib.check_must_include_all(["harbor lane"],
                                                   ["harbor lane", "harbor lane inn"])
        self.assertEqual(len(failures), 1)
        self.assertIn("harbor lane inn", failures[0])

    def test_must_include_all_matches_across_punctuation(self):
        self.assertEqual(
            eval_lib.check_must_include_all(["Harbor-Lane Inn"], ["harbor lane inn"]), [])

    def test_any_of_passes_on_one_member(self):
        groups = [["cafe lumen", "café lumen"]]
        self.assertEqual(eval_lib.check_must_include_any_of(["Cafe Lumen"], groups), [])

    def test_any_of_fails_when_the_whole_group_is_missing(self):
        groups = [["cafe lumen", "cafe lumen bistro"]]
        failures = eval_lib.check_must_include_any_of(["some other term"], groups)
        self.assertEqual(len(failures), 1)

    def test_must_not_include_catches_scope_violations(self):
        failures = eval_lib.check_must_not_include(
            ["harbor lane", "harbor lane deals"], ["harborlane deals"])
        self.assertEqual(len(failures), 1)

    def test_min_terms(self):
        self.assertEqual(eval_lib.check_min_terms(["a"] * 15, 15), [])
        self.assertEqual(len(eval_lib.check_min_terms(["a"] * 14, 15)), 1)

    def test_no_duplicates_catches_case_variants(self):
        failures = eval_lib.check_no_duplicates(["Harbor Lane", "harbor lane"])
        self.assertEqual(len(failures), 1)


class TestGradeIntegration(unittest.TestCase):

    CASE = {
        "name": "sample",
        "prompt": "x",
        "checks": {
            "min_terms": 5,
            "must_include_any_of": [["harbor lane inn", "harborlane inn"]],
            "must_not_include": ["harbor lane resort"],
            "no_duplicates": True,
        },
    }

    def test_good_output_passes(self):
        passed, failures, terms = eval_lib.grade(SAMPLE_OUTPUT, self.CASE)
        self.assertTrue(passed, failures)
        self.assertGreaterEqual(len(terms), 5)

    def test_forbidden_term_fails(self):
        bad = SAMPLE_OUTPUT + "- Harbor Lane Resort\n"
        passed, failures, _ = eval_lib.grade(bad, self.CASE)
        self.assertFalse(passed)
        self.assertTrue(any("forbidden" in f for f in failures), failures)

    def test_thin_output_fails_on_count(self):
        passed, failures, _ = eval_lib.grade("- Harbor Lane Inn\n", self.CASE)
        self.assertFalse(passed)
        self.assertTrue(any("at least" in f for f in failures), failures)


HUMAN_PROMPT = "Humanize this:\n\nWe reviewed 37 projects and kept 14 in Hawai\u02bbi."
HUMAN_CASE = {
    "name": "edit",
    "prompt": HUMAN_PROMPT,
    "checks": {"grade_until": "What changed", "must_not_match": ["\u2014", " \u2013 "],
               "must_contain": ["37", "14", "Hawai\u02bbi"], "no_new_numbers": True},
}
GOOD_HUMAN = ("We reviewed 37 projects and kept 14 in Hawai\u02bbi.\n\n"
              "What changed\n- Removed the dash \u2014 and the filler.\n")


class TestTextChecks(unittest.TestCase):

    def test_compliant_output_passes(self):
        passed, failures, _ = eval_lib.grade(GOOD_HUMAN, HUMAN_CASE)
        self.assertTrue(passed, failures)

    def test_must_not_match_fails_on_an_em_dash(self):
        text = "We kept 14 \u2014 and 37 in Hawai\u02bbi.\n\nWhat changed\n- x"
        passed, failures, _ = eval_lib.grade(text, HUMAN_CASE)
        self.assertFalse(passed)
        self.assertTrue(any("forbidden pattern" in f for f in failures), failures)

    def test_must_not_match_fails_on_a_spaced_en_dash(self):
        text = "We kept 14 \u2013 and 37 in Hawai\u02bbi.\n\nWhat changed\n- x"
        passed, failures, _ = eval_lib.grade(text, HUMAN_CASE)
        self.assertFalse(passed)
        self.assertTrue(any("forbidden pattern" in f for f in failures), failures)

    def test_an_unspaced_en_dash_range_is_allowed(self):
        text = "We kept 14 of 37 in Hawai\u02bbi, 2\u20133 weeks.\n\nWhat changed\n- x"
        case = dict(HUMAN_CASE, prompt=HUMAN_PROMPT + " 2 3")
        passed, failures, _ = eval_lib.grade(text, case)
        self.assertTrue(passed, failures)

    def test_must_contain_fails_when_the_okina_word_is_flattened(self):
        text = "We kept 14 of 37 in Hawai'i.\n\nWhat changed\n- x"
        passed, failures, _ = eval_lib.grade(text, HUMAN_CASE)
        self.assertFalse(passed)
        self.assertTrue(any("missing required text" in f for f in failures), failures)

    def test_must_contain_fails_when_a_number_is_dropped(self):
        text = "We kept 14 in Hawai\u02bbi.\n\nWhat changed\n- x"
        passed, failures, _ = eval_lib.grade(text, HUMAN_CASE)
        self.assertFalse(passed)
        self.assertIn("missing required text: '37'", failures)

    def test_no_new_numbers_fails_on_an_invented_figure(self):
        text = "We kept 14 of 37 in Hawai\u02bbi, up 52%.\n\nWhat changed\n- x"
        passed, failures, _ = eval_lib.grade(text, HUMAN_CASE)
        self.assertFalse(passed)
        self.assertIn("number not in the prompt: 52", failures)

    def test_no_new_numbers_lists_each_new_figure_once_sorted(self):
        failures = eval_lib.check_no_new_numbers("9 and 8 and 9", "none")
        self.assertEqual(failures, ["number not in the prompt: 8",
                                    "number not in the prompt: 9"])

    def test_no_new_numbers_ignores_list_markers(self):
        text = "1. We kept 14\n2) of 37\n  3. in Hawai\u02bbi\n\nWhat changed\n- x"
        passed, failures, _ = eval_lib.grade(text, HUMAN_CASE)
        self.assertTrue(passed, failures)

    def test_a_number_inside_a_sentence_is_not_a_list_marker(self):
        failures = eval_lib.check_no_new_numbers("We kept 14 of 37 in 2 groups.", "14 37")
        self.assertEqual(failures, ["number not in the prompt: 2"])

    def test_grade_until_ignores_the_trailing_section(self):
        """The change log may quote the dash it removed."""
        text = GOOD_HUMAN + "- Replaced the \u2014 with a comma, then 99 more.\n"
        passed, failures, _ = eval_lib.grade(text, HUMAN_CASE)
        self.assertTrue(passed, failures)

    def test_grade_until_marker_absent_is_a_failure(self):
        text = "We kept 14 of 37 in Hawai\u02bbi.\n"
        passed, failures, _ = eval_lib.grade(text, HUMAN_CASE)
        self.assertFalse(passed)
        self.assertIn("output has no 'What changed' section", failures)

    def test_marker_absent_still_runs_the_other_checks_on_the_whole_output(self):
        text = "We kept 14 \u2014 of 37 in Hawai\u02bbi.\n"
        passed, failures, _ = eval_lib.grade(text, HUMAN_CASE)
        self.assertFalse(passed)
        self.assertTrue(any("no 'What changed'" in f for f in failures), failures)
        self.assertTrue(any("forbidden pattern" in f for f in failures), failures)

    def test_term_checks_run_on_the_graded_text(self):
        case = {"name": "t", "prompt": "p",
                "checks": {"grade_until": "STOP", "must_not_include": ["bad term"]}}
        passed, failures, _ = eval_lib.grade("- good term\nSTOP\n- bad term\n", case)
        self.assertTrue(passed, failures)


class TestValidateCase(unittest.TestCase):

    GOOD = {"name": "n", "prompt": "p", "checks": {"min_terms": 1}}

    def test_a_good_case_has_no_problems(self):
        self.assertEqual(eval_lib.validate_case(self.GOOD), [])

    def test_a_case_whose_checks_are_all_switched_off_is_a_problem(self):
        for checks in ({"no_duplicates": False}, {"no_new_numbers": False, "must_contain": []}):
            with self.subTest(checks=checks):
                problems = eval_lib.validate_case(dict(self.GOOD, checks=checks))
                self.assertTrue(any("no active checks" in p for p in problems), problems)

    def test_one_active_check_among_switched_off_ones_is_enough(self):
        case = dict(self.GOOD, checks={"no_duplicates": False, "must_contain": ["x"]})
        self.assertEqual(eval_lib.validate_case(case), [])

    def test_a_case_with_no_checks_is_a_problem(self):
        for checks in ({}, None):
            with self.subTest(checks=checks):
                case = dict(self.GOOD, checks=checks)
                self.assertTrue(eval_lib.validate_case(case))

    def test_an_unknown_check_is_a_problem(self):
        case = dict(self.GOOD, checks={"min_terms": 1, "no_truncated_stems_of": ["x"]})
        problems = eval_lib.validate_case(case)
        self.assertTrue(any("no_truncated_stems_of" in p for p in problems), problems)

    def test_missing_name_or_prompt_is_a_problem(self):
        self.assertTrue(eval_lib.validate_case({"prompt": "p", "checks": {"min_terms": 1}}))
        self.assertTrue(eval_lib.validate_case({"name": "n", "checks": {"min_terms": 1}}))

    def test_every_key_grade_understands_is_known(self):
        self.assertEqual(eval_lib.KNOWN_CHECKS, {
            "min_terms", "must_include_all", "must_include_any_of", "must_not_include",
            "no_duplicates", "must_not_match", "must_contain", "no_new_numbers",
            "grade_until"})


class TestCostAccounting(unittest.TestCase):

    def test_rates_price_input_and_output_exactly(self):
        cost = eval_lib.usage_cost({"input_tokens": 1_000_000,
                                    "output_tokens": 1_000_000}, (2.0, 10.0))
        self.assertAlmostEqual(cost, 12.0, places=6)

    def test_no_rates_returns_none_not_zero(self):
        """A confident $0.00 with no rates would read as 'free'."""
        self.assertIsNone(eval_lib.usage_cost({"input_tokens": 1000}, None))

    def test_missing_usage_fields_do_not_raise(self):
        self.assertEqual(eval_lib.usage_cost({}, (2.0, 10.0)), 0.0)

    def test_cache_tokens_count_as_input(self):
        cost = eval_lib.usage_cost({"cache_read_input_tokens": 500_000,
                                    "cache_creation_input_tokens": 500_000}, (2.0, 10.0))
        self.assertAlmostEqual(cost, 2.0, places=6)


class TestApiKey(unittest.TestCase):

    def test_reads_only_the_environment_variable(self):
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(eval_lib.EvalError) as raised:
                eval_lib.resolve_api_key()
            self.assertIn("ANTHROPIC_API_KEY", str(raised.exception))
            os.environ["ANTHROPIC_API_KEY"] = "k"
            self.assertEqual(eval_lib.resolve_api_key(), "k")
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved

    def test_the_library_has_no_credential_lookup_besides_the_variable(self):
        self.assertNotIn("subprocess", vars(eval_lib))
        self.assertFalse(hasattr(eval_lib, "PRICING"))
        self.assertFalse(hasattr(eval_lib, "DEFAULT_MODEL"))


class TestCaseFilesAreValid(unittest.TestCase):
    """Every case file parses, and every check it names is one the grader runs.

    A typo'd check key would otherwise be silently ignored: the case would pass
    while testing less than it claims, which is the quietest way an eval rots.
    """

    CASES_DIR = os.path.join(REPO_ROOT, "evals", "cases")

    def test_all_case_files(self):
        files = [f for f in os.listdir(self.CASES_DIR) if f.endswith(".json")]
        self.assertTrue(files, "no case files found")
        for filename in files:
            with self.subTest(file=filename):
                with open(os.path.join(self.CASES_DIR, filename), encoding="utf-8") as handle:
                    cases = json.load(handle)
                self.assertTrue(cases, "%s is empty" % filename)
                for case in cases:
                    self.assertEqual(eval_lib.validate_case(case), [])

    def test_every_case_file_names_a_real_skill(self):
        for filename in os.listdir(self.CASES_DIR):
            if not filename.endswith(".json"):
                continue
            with self.subTest(skill=filename[:-5]):
                run_eval.find_skill(filename[:-5])  # raises EvalError if absent

    def test_the_humanizer_case_has_the_pieces_its_checks_grade(self):
        cases = run_eval.load_cases("humanizer")
        prompt = cases[0]["prompt"]
        self.assertIn("\u2014", prompt)
        self.assertIn(" \u2013 ", prompt)
        self.assertIn("Hawai\u02bbi", prompt)
        for number in ("37", "14"):
            self.assertIn(number, prompt)

    def test_the_humanizer_case_fails_the_unedited_draft(self):
        """The draft itself must violate its own checks, or the case is vacuous."""
        case = run_eval.load_cases("humanizer")[0]
        draft = case["prompt"] + "\n\nWhat changed\n- nothing\n"
        passed, failures, _ = eval_lib.grade(draft, case)
        self.assertFalse(passed)
        self.assertTrue(any("forbidden pattern" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main(verbosity=1)
