#!/usr/bin/env python3
"""install.sh reports a selected profile's outside prerequisite, and never acts on it.

No shipped profile declares one today, so each test gives the maintainer
profile a synthetic prerequisite in a fixture registry. Apply and --check say
whether it is enabled at user scope and print the command to fix it if not. It
is a report only: no install, no enable, no change to the exit status. These
drive the REAL install.sh through the shared fixture and `claude` shim.
"""

import copy
import json
import re
import unittest

import install_fixture as fx
from test_install_profiles import ProfilesTestCase

PREREQ = "companion@example-market"
WHY = "a fixture prerequisite"
MAINTAINER = {"profiles": ["maintainer"]}
INSTALL_LINE = "claude plugin install %s --scope user" % PREREQ
ENABLE_LINE = "claude plugin enable %s --scope user" % PREREQ


def with_prerequisite():
    registry = copy.deepcopy(fx.real_profiles())
    registry["profiles"]["maintainer"]["prerequisites"] = [
        {"id": PREREQ, "why": WHY}]
    return registry


class PrerequisiteTestCase(ProfilesTestCase):

    def setUp(self):
        super().setUp()
        self.use_registry(with_prerequisite())

    def seed(self, enabled, scope="user"):
        """Add the prerequisite to the shim's installed plugins."""
        with open(self.state) as handle:
            state = json.load(handle)
        state["plugins"].append({"id": PREREQ, "enabled": enabled, "scope": scope,
                                 "installPath": "/shim/" + PREREQ})
        with open(self.state, "w") as handle:
            json.dump(state, handle)

    def prerequisite_lines(self, proc):
        plain = re.sub(r"\x1b\[[0-9;]*m", "", proc.stdout)
        return [line.strip() for line in plain.splitlines() if PREREQ in line]

    def assert_never_acted(self):
        for line in self.log():
            self.assertNotIn(PREREQ.split("@")[0], line.replace(
                "plugin list", ""), "the shim was asked to act on the prerequisite: " + line)

    def modes(self):
        return ((), ("--check",))

    def settled(self):
        """A first apply that installs the maintainer profile; later runs see a settled machine."""
        proc = self.run_with(MAINTAINER)
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual([], self.prerequisite_lines(proc)[1:], "one line for one prerequisite")


class TestAbsent(PrerequisiteTestCase):

    def test_apply_prints_the_install_line_and_the_exit_matches_a_run_without_it(self):
        proc = self.run_with(MAINTAINER)
        lines = self.prerequisite_lines(proc)
        self.assertEqual(1, len(lines), proc.stdout)
        self.assertIn("!!", lines[0])
        self.assertIn("maintainer needs " + PREREQ, lines[0])
        self.assertIn(WHY, lines[0])
        self.assertIn("not installed. Install it: " + INSTALL_LINE, lines[0])
        self.assert_never_acted()
        self.use_registry(copy.deepcopy(fx.real_profiles()))
        baseline = self.run_with(MAINTAINER)
        self.assertEqual([], self.prerequisite_lines(baseline))
        self.assertEqual(baseline.returncode, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(0, proc.returncode)

    def test_check_prints_the_install_line_and_the_exit_matches_a_run_without_it(self):
        self.settled()
        proc = self.run_with(MAINTAINER, "--check")
        lines = self.prerequisite_lines(proc)
        self.assertEqual(1, len(lines), proc.stdout)
        self.assertIn("not installed. Install it: " + INSTALL_LINE, lines[0])
        self.assert_never_acted()
        self.use_registry(copy.deepcopy(fx.real_profiles()))
        baseline = self.run_with(MAINTAINER, "--check")
        self.assertEqual([], self.prerequisite_lines(baseline))
        self.assertEqual(baseline.returncode, proc.returncode, proc.stdout + proc.stderr)
        self.assertEqual(0, proc.returncode)

    def test_a_project_scope_install_counts_as_absent(self):
        self.settled()
        self.seed(True, scope="project")
        for args in self.modes():
            with self.subTest(args=args):
                proc = self.run_with(MAINTAINER, *args)
                lines = self.prerequisite_lines(proc)
                self.assertEqual(1, len(lines), proc.stdout)
                self.assertIn("not installed. Install it:", lines[0])
                self.assertNotIn("OK", lines[0])
        self.assert_never_acted()


class TestPresent(PrerequisiteTestCase):

    def test_enabled_prints_ok(self):
        self.settled()
        self.seed(True)
        for args in self.modes():
            with self.subTest(args=args):
                proc = self.run_with(MAINTAINER, *args)
                lines = self.prerequisite_lines(proc)
                self.assertEqual(1, len(lines), proc.stdout)
                self.assertIn("OK prerequisite %s (for maintainer)" % PREREQ, lines[0])
                self.assertNotIn("!!", lines[0])
                self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assert_never_acted()

    def test_disabled_prints_the_enable_line(self):
        self.settled()
        self.seed(False)
        for args in self.modes():
            with self.subTest(args=args):
                proc = self.run_with(MAINTAINER, *args)
                lines = self.prerequisite_lines(proc)
                self.assertEqual(1, len(lines), proc.stdout)
                self.assertIn("!!", lines[0])
                self.assertIn("which is disabled. Enable it: " + ENABLE_LINE, lines[0])
                self.assertNotIn("OK", lines[0])
                self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        self.assert_never_acted()


class TestNotSelected(PrerequisiteTestCase):

    def test_no_line_when_maintainer_is_not_selected(self):
        for config in ({}, {"profiles": ["builder"]}):
            for args in self.modes():
                with self.subTest(config=config, args=args):
                    proc = self.run_with(config, *args)
                    self.assertEqual([], self.prerequisite_lines(proc), proc.stdout)
                    self.assertNotIn("prerequisite", proc.stdout)
        self.assert_never_acted()


if __name__ == "__main__":
    unittest.main()
