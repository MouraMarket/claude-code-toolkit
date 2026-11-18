#!/usr/bin/env python3
"""Shape checks for evals/model-routing/trigger-cases.json, the case file
claude-skill-trigger reads for the model-routing skill plugin.

The live trigger eval is run by hand; this only keeps the case file from
rotting into something the harness would misread, or pointing at a skill name
the marketplace no longer ships.
"""

import json
import os
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
CASES = os.path.join(os.path.dirname(HERE), "trigger-cases.json")
SKILL_ID = "skill-model-routing:model-routing"


class TestTriggerCases(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(CASES, encoding="utf-8") as handle:
            cls.cases = json.load(handle)

    def test_each_case_has_a_prompt_and_exactly_one_of_expect_forbid(self):
        self.assertIsInstance(self.cases, list)
        for case in self.cases:
            with self.subTest(prompt=case.get("prompt")):
                self.assertTrue(case.get("prompt", "").strip())
                self.assertEqual(("expect" in case) + ("forbid" in case), 1)

    def test_at_least_five_expect_and_five_forbid(self):
        self.assertGreaterEqual(sum("expect" in c for c in self.cases), 5)
        self.assertGreaterEqual(sum("forbid" in c for c in self.cases), 5)

    def test_exactly_one_positive_control_on_an_expect_case(self):
        controls = [c for c in self.cases if c.get("control") == "positive"]
        self.assertEqual(len(controls), 1)
        self.assertIn("expect", controls[0])

    def test_no_prompt_starts_with_a_slash(self):
        for case in self.cases:
            with self.subTest(prompt=case["prompt"]):
                self.assertFalse(case["prompt"].lstrip().startswith("/"))

    def test_every_case_names_the_shipped_skill(self):
        for case in self.cases:
            with self.subTest(prompt=case["prompt"]):
                self.assertEqual(case.get("expect", case.get("forbid")), SKILL_ID)
        plugin, skill = SKILL_ID.split(":")
        with open(os.path.join(REPO, ".claude-plugin", "marketplace.json"), encoding="utf-8") as handle:
            sources = {p["name"]: p["source"] for p in json.load(handle)["plugins"]}
        self.assertIn(plugin, sources)
        self.assertTrue(os.path.isfile(os.path.join(
            REPO, sources[plugin], "skills", skill, "SKILL.md")))


if __name__ == "__main__":
    unittest.main()
