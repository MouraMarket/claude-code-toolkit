#!/usr/bin/env python3
"""Offline tests for the eval runner.

Covers the explicit opt-in (`--dry-run` or `--live`, nothing ambient), the
refusals that must happen before any call, cost labelling, and the two coverage
affordances: terms are kept, and an older skill revision is gradeable by
today's grader.

No API calls: `call_model` is replaced with a stub.

Run: python3 evals/tests/test_run_eval.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO_ROOT, "evals"))

import eval_lib  # noqa: E402
import run_eval  # noqa: E402


MODEL_OUTPUT = """### Exact name
- Harbor Lane Inn
- harborlaneinn

### Spacing variations
- Harbor Lane
"""

CASE = {"name": "stub", "prompt": "irrelevant", "checks": {"min_terms": 1}}
USAGE = {"input_tokens": 10, "output_tokens": 20}


def stub(text=MODEL_OUTPUT, usage=None):
    """A call_model replacement returning fixed output."""
    return mock.Mock(return_value=(text, USAGE if usage is None else usage))


def run_main(argv, env=None, model=None):
    """Run main() with call_model patched and a controlled environment.

    Returns (exit code, stdout, stderr, the call_model mock).
    """
    model = model or stub()
    out, err = io.StringIO(), io.StringIO()
    environ = {} if env is None else env
    with mock.patch.object(eval_lib, "call_model", model), \
            mock.patch.dict(os.environ, environ, clear=True), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = run_eval.main(argv)
        except SystemExit as exit_:  # argparse exits on a usage error
            code = exit_.code
    return code, out.getvalue(), err.getvalue(), model


@contextlib.contextmanager
def temp_file(text, suffix=".md"):
    with tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False,
                                     encoding="utf-8") as handle:
        handle.write(text)
        path = handle.name
    try:
        yield path
    finally:
        os.unlink(path)


SKILL = "---\nname: x\n---\nOLD SKILL BODY\n"
GOOD_CASES = json.dumps([CASE])


class TestOptIn(unittest.TestCase):

    def test_neither_flag_exits_2_before_any_call(self):
        code, _, _, model = run_main(["humanizer"], env={"ANTHROPIC_API_KEY": "k"})
        self.assertEqual(code, 2)
        model.assert_not_called()

    def test_both_flags_exit_2(self):
        code, _, _, model = run_main(["humanizer", "--dry-run", "--live", "--model", "m"],
                                     env={"ANTHROPIC_API_KEY": "k"})
        self.assertEqual(code, 2)
        model.assert_not_called()

    def test_zero_trials_exits_2_instead_of_passing_nothing(self):
        for mode in (["--dry-run"], ["--live", "--model", "m"]):
            with self.subTest(mode=mode):
                code, _, err, model = run_main(["humanizer", "--trials", "0"] + mode,
                                               env={"ANTHROPIC_API_KEY": "k"})
                self.assertEqual(code, 2)
                self.assertIn("--trials", err)
                model.assert_not_called()

    def test_live_without_a_key_exits_2_and_never_calls(self):
        code, _, err, model = run_main(["humanizer", "--live", "--model", "m"], env={})
        self.assertEqual(code, 2)
        self.assertIn("ANTHROPIC_API_KEY", err)
        model.assert_not_called()

    def test_live_without_a_model_exits_2_and_never_calls(self):
        code, _, err, model = run_main(["humanizer", "--live"],
                                       env={"ANTHROPIC_API_KEY": "k"})
        self.assertEqual(code, 2)
        self.assertIn("--live needs --model", err)
        model.assert_not_called()

    def test_dry_run_never_calls_and_never_reads_the_key(self):
        with mock.patch.object(eval_lib, "resolve_api_key",
                               side_effect=AssertionError("key read")):
            code, out, _, model = run_main(["humanizer", "--dry-run"], env={})
        self.assertEqual(code, 0, out)
        model.assert_not_called()

    def test_the_real_humanizer_case_dry_runs_clean(self):
        code, out, _, _ = run_main(["humanizer", "--dry-run"], env={})
        self.assertEqual(code, 0)
        self.assertIn("1 cases", out)

    def test_live_passes_the_key_and_model_to_the_call(self):
        with temp_file(SKILL) as skill, temp_file(GOOD_CASES, ".json") as cases:
            _, _, _, model = run_main(
                ["x", "--live", "--model", "some-model", "--skill-file", skill,
                 "--cases", cases], env={"ANTHROPIC_API_KEY": "secret"})
        model.assert_called_once()
        args = model.call_args[0]
        self.assertEqual(args[2:], ("some-model", "secret"))


class TestCaseValidation(unittest.TestCase):

    def run_with_cases(self, text):
        with temp_file(SKILL) as skill, temp_file(text, ".json") as cases:
            return run_main(["x", "--dry-run", "--skill-file", skill, "--cases", cases])

    def test_zero_cases_exit_2(self):
        code, _, err, _ = self.run_with_cases("[]")
        self.assertEqual(code, 2)
        self.assertIn("non-empty", err)

    def test_a_non_list_exits_2(self):
        code, _, _, _ = self.run_with_cases('{"name": "n"}')
        self.assertEqual(code, 2)

    def test_a_case_with_no_checks_exits_2(self):
        code, _, err, _ = self.run_with_cases('[{"name": "n", "prompt": "p"}]')
        self.assertEqual(code, 2)
        self.assertIn("no checks", err)

    def test_an_unknown_check_key_exits_2(self):
        code, _, err, _ = self.run_with_cases(
            '[{"name": "n", "prompt": "p", "checks": {"min_term": 1}}]')
        self.assertEqual(code, 2)
        self.assertIn("min_term", err)

    def test_invalid_json_exits_2(self):
        code, _, _, _ = self.run_with_cases("not json")
        self.assertEqual(code, 2)

    def test_a_missing_case_file_exits_2(self):
        with temp_file(SKILL) as skill:
            code, _, _, _ = run_main(["x", "--dry-run", "--skill-file", skill,
                                      "--cases", "/nonexistent/cases.json"])
        self.assertEqual(code, 2)


class TestFindSkill(unittest.TestCase):

    def test_resolves_the_humanizer_from_this_repo(self):
        self.assertTrue(run_eval.find_skill("humanizer").endswith("SKILL.md"))

    def test_an_unknown_skill_names_the_remedy(self):
        with self.assertRaises(eval_lib.EvalError) as raised:
            run_eval.find_skill("no-such-skill-here")
        self.assertIn("--skill-file", str(raised.exception))

    def test_an_unknown_skill_exits_2(self):
        code, _, err, _ = run_main(["no-such-skill-here", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("--skill-file", err)


class TestCostReporting(unittest.TestCase):

    def live(self, extra):
        with temp_file(SKILL) as skill, temp_file(GOOD_CASES, ".json") as cases:
            return run_main(["x", "--live", "--model", "m", "--skill-file", skill,
                             "--cases", cases] + extra, env={"ANTHROPIC_API_KEY": "k"})

    def test_without_price_cost_is_unknown_and_tokens_are_reported(self):
        code, out, _, _ = self.live([])
        self.assertEqual(code, 0, out)
        self.assertIn("cost unknown", out)
        self.assertIn("10 in, 20 out", out)
        self.assertNotIn("$", out)

    def test_with_price_the_figure_is_labelled_as_your_rates(self):
        code, out, _, _ = self.live(["--price", "1000000", "2000000"])
        self.assertEqual(code, 0, out)
        self.assertIn("at your --price rates", out)
        self.assertIn("$50.0000", out)  # 10 in at $1/token + 20 out at $2/token
        self.assertIn("10 in, 20 out", out)

    def test_run_case_cost_uses_the_rates(self):
        with mock.patch.object(eval_lib, "call_model", stub()):
            priced = run_eval.run_case(CASE, "s", "m", "k", (1000000.0, 2000000.0), False)
            unpriced = run_eval.run_case(CASE, "s", "m", "k", None, False)
        self.assertAlmostEqual(priced["cost"], 50.0)
        self.assertIsNone(unpriced["cost"])


class TestTermPersistence(unittest.TestCase):

    def run_case(self, call_model, dry_run=False):
        with mock.patch.object(eval_lib, "call_model", call_model):
            return run_eval.run_case(CASE, "system", "m", "k", None, dry_run)

    def test_records_the_terms_not_just_the_count(self):
        result = self.run_case(stub())
        self.assertEqual(result["term_list"],
                         ["Harbor Lane Inn", "harborlaneinn", "Harbor Lane"])

    def test_count_still_matches_the_list(self):
        """`terms` stayed an int on purpose: the printed table and saved
        artifacts read it that way."""
        result = self.run_case(stub())
        self.assertEqual(result["terms"], len(result["term_list"]))
        self.assertIsInstance(result["terms"], int)

    def test_term_list_present_when_the_call_fails(self):
        """A consumer that iterates term_list must not hit a KeyError on the
        one result shape produced by a failure."""
        result = self.run_case(mock.Mock(side_effect=eval_lib.EvalError("nope")))
        self.assertEqual(result["term_list"], [])
        self.assertFalse(result["passed"])

    def test_term_list_present_on_a_dry_run(self):
        model = stub()
        result = self.run_case(model, dry_run=True)
        self.assertEqual(result["term_list"], [])
        model.assert_not_called()


class TestSkillFileOverride(unittest.TestCase):
    """--skill-file is what makes a before/after diff comparable.

    Checking an old commit out would revert eval_lib too, so the two sides
    would be graded by different code. These assert the override is actually
    read, not merely accepted.
    """

    def test_dry_run_reports_the_overridden_file(self):
        with temp_file(SKILL) as path:
            code, out, _, _ = run_main(["humanizer", "--dry-run", "--skill-file", path])
        self.assertEqual(code, 0)
        self.assertIn(path, out)

    def test_the_overridden_body_is_what_gets_sent(self):
        with temp_file(SKILL) as path, temp_file(GOOD_CASES, ".json") as cases:
            _, _, _, model = run_main(
                ["x", "--live", "--model", "m", "--json", "--skill-file", path,
                 "--cases", cases], env={"ANTHROPIC_API_KEY": "k"})
        self.assertEqual(model.call_args[0][0].strip(), "OLD SKILL BODY")

    def test_a_missing_skill_file_is_a_clean_failure(self):
        """Exit 2 means 'could not run'. Falling back to the in-tree skill
        would silently measure the wrong thing, which is worse than an error."""
        code, _, err, _ = run_main(["humanizer", "--dry-run",
                                    "--skill-file", "/nonexistent/SKILL.md"])
        self.assertEqual(code, 2)
        self.assertIn("/nonexistent/SKILL.md", err)

    def test_json_output_carries_the_terms(self):
        with temp_file(SKILL) as path, temp_file(GOOD_CASES, ".json") as cases:
            _, out, _, _ = run_main(
                ["x", "--live", "--model", "m", "--json", "--skill-file", path,
                 "--cases", cases], env={"ANTHROPIC_API_KEY": "k"})
        results = json.loads(out)
        self.assertTrue(results)
        for result in results:
            self.assertIn("term_list", result)


if __name__ == "__main__":
    unittest.main()
