#!/usr/bin/env python3
"""Tests for claude-build-route's `--review-mode` CLI path, the retirement of
its SessionStart hook (no registration, a quiet bare invocation), and the drift
guard pinning the build skill's description against the skill body.

Everything runs the executable through `subprocess`, per this repo's rule
("Test the executables, not just the lib" — `usage-watch` shipped 93 green
unit tests and zero coverage of any hook, and a review reintroduced two
already-fixed bugs while the suite stayed green).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
EXE = os.path.join(HERE, "claude-build-route")
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
BUILD_MD = os.path.join(REPO_ROOT, "skills", "builder", "build", "SKILL.md")
ENV = dict(os.environ, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")

sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "skill-trigger"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib", "tests"))
import hook_manifest  # noqa: E402
import route_lib  # noqa: E402

import skill_trigger_lib  # noqa: E402


def run(argv, env=None, input_text=None):
    return subprocess.run([sys.executable, EXE] + list(argv), env=env or ENV,
                          stdin=subprocess.DEVNULL if input_text is None else None,
                          input=input_text, capture_output=True,
                          text=True, timeout=10)


def codex_env(config_path, codex=True, auth=True, cleanup=None):
    """An environment where Codex is, or is not, usable: a PATH holding only a
    shim `codex` (or nothing), and a HOME that holds, or lacks, the auth file.
    The real PATH is never inherited, so a machine that has Codex installed
    cannot make an "unavailable" case pass for the wrong reason."""
    home = tempfile.mkdtemp(prefix="build-route-home-")
    bin_dir = tempfile.mkdtemp(prefix="build-route-bin-")
    if cleanup:
        cleanup(shutil.rmtree, home, ignore_errors=True)
        cleanup(shutil.rmtree, bin_dir, ignore_errors=True)
    if codex:
        shim = os.path.join(bin_dir, "codex")
        with open(shim, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(shim, 0o755)
    if auth:
        os.makedirs(os.path.join(home, ".codex"))
        with open(os.path.join(home, ".codex", "auth.json"), "w",
                  encoding="utf-8") as handle:
            handle.write("{}")
    return dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config_path, HOME=home,
                PATH=bin_dir)


class TestReviewMode(unittest.TestCase):
    """Hermetic: every case sets CLAUDE_TOOLKIT_CONFIG explicitly rather than
    inheriting whatever config happens to be installed on this machine."""

    def test_no_config_prints_economical(self):
        result = run(["--review-mode"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "economical")

    def test_full_config_prints_full_power(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"build": {"review": "full"}}, handle)
            path = handle.name
        self.addCleanup(os.unlink, path)
        env = codex_env(path, cleanup=self.addCleanup)

        result = run(["--review-mode"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "full-power")

    def test_full_power_config_prints_full_power(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump({"build": {"review": "full-power"}}, handle)
            path = handle.name
        self.addCleanup(os.unlink, path)
        env = codex_env(path, cleanup=self.addCleanup)

        result = run(["--review-mode"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "full-power")

    def test_malformed_config_prints_economical(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            handle.write("not json")
            path = handle.name
        self.addCleanup(os.unlink, path)
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=path)

        result = run(["--review-mode"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "economical")


class TestReviewModeRefusesARepoWithoutAgentsMd(unittest.TestCase):
    """`--review-mode --repo PATH`: under full-power a repo with no non-empty
    AGENTS.md is refused at Phase 0 (every Codex gate refuses it anyway).
    Under economical the file is irrelevant -- no Codex runs -- and the
    present-file case is the control that stops "always refuse" passing."""

    def _env(self, review):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        with handle:
            json.dump({"build": {"review": review}}, handle)
        self.addCleanup(os.unlink, handle.name)
        return codex_env(handle.name, cleanup=self.addCleanup)

    def _repo(self, agents_text=None):
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo)
        if agents_text is not None:
            with open(os.path.join(repo, "AGENTS.md"), "w", encoding="utf-8") as handle:
                handle.write(agents_text)
        return repo

    def test_full_power_refuses_a_repo_with_no_agents_md(self):
        result = run(["--review-mode", "--repo", self._repo()], env=self._env("full"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("no AGENTS.md", result.stderr)

    def test_full_power_refuses_a_blank_agents_md(self):
        result = run(["--review-mode", "--repo", self._repo("  \n")], env=self._env("full"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_full_power_passes_a_repo_with_agents_md(self):
        result = run(["--review-mode", "--repo", self._repo("# rules\n")],
                     env=self._env("full"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "full-power")

    def test_economical_ignores_a_missing_agents_md(self):
        result = run(["--review-mode", "--repo", self._repo()], env=self._env("economical"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "economical")

    def test_repo_without_a_path_is_a_usage_error(self):
        result = run(["--review-mode", "--repo"])
        self.assertEqual(result.returncode, 2)


class TestFullPowerRefusesUnusableCodex(unittest.TestCase):
    """Under full-power, Phase 0 refuses when Codex cannot run, so a machine
    without it learns at the start rather than at the first gate. Economical
    never checks Codex."""

    def _config(self, review):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        with handle:
            json.dump({"build": {"review": review}}, handle)
        self.addCleanup(os.unlink, handle.name)
        return handle.name

    def test_no_codex_on_path_refuses_with_the_switch_command(self):
        path = self._config("full")
        env = codex_env(path, codex=False, cleanup=self.addCleanup)
        result = run(["--review-mode"], env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Codex is unavailable", result.stderr)
        self.assertIn("not on PATH", result.stderr)
        self.assertIn("--set build.review=economical", result.stderr)
        self.assertIn(os.path.join(REPO_ROOT, "lib", "setup_config.py"),
                      result.stderr)
        self.assertIn(REPO_ROOT, result.stderr)
        self.assertIn(path, result.stderr)

    def test_the_check_runs_with_a_repo_that_has_agents_md_too(self):
        repo = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, repo)
        with open(os.path.join(repo, "AGENTS.md"), "w", encoding="utf-8") as fh:
            fh.write("# rules\n")
        env = codex_env(self._config("full-power"), codex=False,
                        cleanup=self.addCleanup)
        result = run(["--review-mode", "--repo", repo], env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Codex is unavailable", result.stderr)

    def test_codex_without_auth_refuses_and_names_the_auth_path(self):
        env = codex_env(self._config("full"), auth=False,
                        cleanup=self.addCleanup)
        result = run(["--review-mode"], env=env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Codex is unavailable", result.stderr)
        self.assertIn(os.path.join(env["HOME"], ".codex", "auth.json"),
                      result.stderr)

    def test_economical_prints_economical_with_no_codex(self):
        env = codex_env(self._config("economical"), codex=False, auth=False,
                        cleanup=self.addCleanup)
        result = run(["--review-mode"], env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "economical")


class TestReviewModeNeverReadsStdin(unittest.TestCase):
    """The no-hang test — the only assertion in this suite that can catch the
    stdin drain being moved above argument parsing. A dual-mode tool that
    reads stdin unconditionally passes every piped test above and then hangs
    on the one invocation that matters: a person running it from a terminal.
    """

    def test_does_not_block_on_an_open_unwritten_stdin(self):
        proc = subprocess.Popen([sys.executable, EXE, "--review-mode"],
                                env=ENV, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail("--review-mode blocked reading an open, unwritten "
                      "stdin; it must never touch stdin at all")
        finally:
            if proc.stdin:
                proc.stdin.close()

        self.assertEqual(proc.returncode, 0)
        with proc.stdout, proc.stderr:
            self.assertEqual(proc.stdout.read().strip(), "economical")


class TestThroughTheInstalledSymlink(unittest.TestCase):
    """`realpath`, not `abspath` — the bug that passes every test run at the
    real repo path, where `realpath` and `abspath` are identical.

    This runs through a `~/.local/bin` symlink in the real install; a temp
    symlink reproduces that shape without touching `./install.sh`, which is
    forbidden from a worktree.
    """

    def test_the_symlinked_binary_still_resolves_a_mode(self):
        tmp = tempfile.mkdtemp(prefix="build-route-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        bin_dir = os.path.join(tmp, "bin")
        os.makedirs(bin_dir)
        link = os.path.join(bin_dir, "claude-build-route")
        os.symlink(EXE, link)

        result = subprocess.run([sys.executable, link, "--review-mode"],
                                env=ENV, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "economical")


class TestExtractExclusionClausePositiveControl(unittest.TestCase):
    """Positive control on the extraction: the real build.md's clause is
    found and non-empty."""

    def test_extracts_a_non_empty_clause_containing_a_short_document(self):
        with open(BUILD_MD) as handle:
            text = handle.read()
        clause = route_lib.extract_exclusion_clause(text)
        self.assertTrue(clause)
        self.assertIn("a short document", clause)

    def test_the_build_skill_description_carries_the_exclusion_clause(self):
        # The description is what the model sees when deciding whether to
        # invoke the skill, so the exclusion list has to be in it too.
        with open(BUILD_MD) as handle:
            text = handle.read()
        frontmatter = text.split("\n---\n", 1)[0]
        lines = [line for line in frontmatter.splitlines()
                 if line.startswith("description: ")]
        self.assertEqual(len(lines), 1)
        description = lines[0][len("description: "):]
        self.assertTrue(route_lib.clause_appears_in(
            description, route_lib.extract_exclusion_clause(text)))


class TestExtractExclusionClauseNegativeControl(unittest.TestCase):
    """Negative control on the extraction: a build.md whose heading has been
    renamed must RAISE, never return "" — an empty string is a substring of
    everything, so a guard built on `assertIn("", text)` can never fail."""

    def test_raises_when_the_heading_is_missing(self):
        fixture_build_md = "## When to skip this instead\n\nsome other text\n"
        with self.assertRaises(ValueError):
            route_lib.extract_exclusion_clause(fixture_build_md)


class TestExtractDescriptionClause(unittest.TestCase):
    """`extract_description_clause` takes the text after the LAST em dash. It
    must RAISE, never return "": an empty string is a substring of everything,
    so a drift guard built on it can never fail."""

    def test_the_real_description_yields_a_clause_containing_a_short_document(self):
        clause = route_lib.extract_description_clause(_build_description())
        self.assertIn("a short document", clause)

    def test_a_string_with_no_em_dash_raises(self):
        with self.assertRaises(ValueError):
            route_lib.extract_description_clause("no dash here at all.")

    def test_a_string_ending_in_an_em_dash_raises(self):
        with self.assertRaises(ValueError):
            route_lib.extract_description_clause("Use it \u2014 ")

    def test_takes_the_text_after_the_last_dash_and_strips_the_period(self):
        self.assertEqual(
            route_lib.extract_description_clause("a \u2014 b \u2014  c\n d."),
            "c d")


def _build_description():
    with open(BUILD_MD, encoding="utf-8") as handle:
        text = handle.read()
    frontmatter = text.split("\n---\n", 1)[0]
    lines = [line for line in frontmatter.splitlines()
             if line.startswith("description: ")]
    assert len(lines) == 1
    return lines[0][len("description: "):]


class TestTwoDirectionalDriftGuard(unittest.TestCase):
    """The skill description must match the skill body in BOTH directions --
    editing either side alone must break a test. One direction alone goes
    blind when the source side is reworded. The description is the only
    router."""

    @classmethod
    def setUpClass(cls):
        with open(BUILD_MD, encoding="utf-8") as handle:
            cls.build_md_text = handle.read()
        cls.description = _build_description()
        cls.body_from_exclusion = cls.build_md_text[
            cls.build_md_text.index(route_lib.EXCLUSION_HEADING):]

    def test_the_description_contains_the_clause_extracted_from_the_body(self):
        clause = route_lib.extract_exclusion_clause(self.build_md_text)
        self.assertTrue(route_lib.clause_appears_in(self.description, clause))

    def test_the_clause_the_description_carries_is_present_in_the_body(self):
        clause = route_lib.extract_description_clause(self.description)
        self.assertTrue(
            route_lib.clause_appears_in(self.body_from_exclusion, clause),
            f"{clause!r} is not present from the exclusion heading on")

    def test_the_guard_survives_a_whitespace_only_reflow_of_the_body(self):
        """Positive control on clause_appears_in's normalisation: a pure reflow
        must red NEITHER direction, or the guard reports drift that did not
        occur. Both directions route through the same helper, so removing the
        normalisation reds this."""
        # Reflow inside the exclusion section only: the same words appear
        # first in the frontmatter description, and reflowing THAT leaves the
        # body untouched, so the control would never run.
        head_end = self.build_md_text.index(route_lib.EXCLUSION_HEADING)
        reflowed_tail = self.body_from_exclusion.replace(
            "a short document, a recap", "a short\n  document, a recap", 1)
        self.assertNotEqual(reflowed_tail, self.body_from_exclusion,
                            "the reflow fixture no longer matches the body")
        reflowed = self.build_md_text[:head_end] + reflowed_tail
        clause = route_lib.extract_exclusion_clause(reflowed)
        self.assertTrue(route_lib.clause_appears_in(self.description, clause))
        description_clause = route_lib.extract_description_clause(self.description)
        self.assertTrue(
            route_lib.clause_appears_in(reflowed_tail, description_clause),
            "a whitespace-only reflow of the body reddened the drift guard")

    def test_the_description_still_carries_the_routing_trigger(self):
        # This text used to live in the SessionStart notice.
        self.assertTrue(route_lib.clause_appears_in(
            self.description,
            "a multi-file change, a new tool, hook or skill, or anything "
            "with real verification"))


class TestTheHookIsRetired(unittest.TestCase):
    """claude-build-route is a `--review-mode` CLI only. A registration left
    in any plugin's hooks/hooks.json would run the bare invocation on every
    SessionStart for nothing."""

    def setUp(self):
        self.registrations = hook_manifest.registrations(
            hook_manifest.load(REPO_ROOT))

    def test_it_is_registered_under_no_event(self):
        found = [r["event"] for r in self.registrations
                 if r["exe"] == "claude-build-route"]
        self.assertEqual(found, [],
                         "claude-build-route is still registered as a hook")

    def test_positive_control_another_session_start_tool_is_found(self):
        # An empty or unparseable manifest would pass the test above.
        found = [r for r in self.registrations
                 if r["event"] == "SessionStart"
                 and r["exe"] == "claude-spec-archive"]
        self.assertEqual(len(found), 1)


class TestModeDispatch(unittest.TestCase):
    """An unknown argument must NOT fall through to hook mode, whose first act
    is a blocking stdin read. Before this, `--help`, `-h` and every typo hung
    forever on a terminal — measured — on the one tool whose documented reason
    for being an executable is that a person runs it by hand."""

    def _run_with_stdin_held_open(self, *argv):
        proc = subprocess.Popen([sys.executable, EXE, *argv], env=ENV,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            self.fail(f"{argv!r} blocked on an open, unwritten stdin — an "
                      f"unknown argument fell through to hook mode")
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream:
                    stream.close()
        return proc.returncode

    def test_unknown_arguments_do_not_hang_and_exit_non_zero(self):
        for argv in (["--help"], ["-h"], ["--review-mod"], ["nonsense"]):
            with self.subTest(argv=argv):
                self.assertEqual(self._run_with_stdin_held_open(*argv), 2)

    def test_the_usage_text_goes_to_stderr(self):
        result = run(["--help"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: claude-build-route", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_bare_invocation_exits_0_without_reading_stdin(self):
        # A session loaded before the retirement still holds the old
        # registration and runs this bare at its next SessionStart.
        self.assertEqual(self._run_with_stdin_held_open(), 0)

    def test_bare_invocation_prints_usage_to_stderr_and_nothing_to_stdout(self):
        result = run([])
        self.assertEqual(result.returncode, 0)
        self.assertIn("usage: claude-build-route", result.stderr)
        self.assertEqual(result.stdout, "")


class TestTriggerCaseFile(unittest.TestCase):
    """The committed live-eval case file loads and covers both directions:
    build-shaped prompts that must route to /build, and the build skill's
    own exclusion examples that must not."""

    CASES = os.path.join(HERE, "trigger-cases.json")

    def setUp(self):
        self.cases = skill_trigger_lib.load_cases(self.CASES)

    @staticmethod
    def build_identity():
        """`<plugin>:build`, derived from the registry profile whose group holds build."""
        with open(os.path.join(REPO_ROOT, "claude", "profiles.json"), encoding="utf-8") as fh:
            registry = {k: v for k, v in json.load(fh)["profiles"].items() if k != "//"}
        holders = [p for p in registry
                   if os.path.isfile(os.path.join(REPO_ROOT, "skills", p, "build", "SKILL.md"))]
        assert len(holders) == 1, holders
        return registry[holders[0]]["plugin"] + ":build"

    def test_exactly_one_positive_control_and_it_expects_build(self):
        controls = [c for c in self.cases if c.get("control") == "positive"]
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0]["expect"], self.build_identity())

    def test_every_case_names_the_build_skills_current_identity(self):
        named = [c.get("expect") or c.get("forbid") for c in self.cases]
        self.assertTrue(named)
        for case in self.cases:
            for key in ("expect", "forbid"):
                if case.get(key):
                    self.assertEqual(case[key], self.build_identity(), case["prompt"])

    def test_has_expect_and_forbid_cases(self):
        self.assertTrue([c for c in self.cases if c.get("expect")])
        self.assertTrue([c for c in self.cases if c.get("forbid")])

    def test_every_forbid_case_forbids_build(self):
        for case in self.cases:
            if case.get("forbid"):
                self.assertEqual(case["forbid"], self.build_identity())

    # One forbid prompt per example in the skill's exclusion clause, in its
    # order. A count alone let every prompt be swapped for something unrelated
    # with this class still green; the mapping pins what each case stands for,
    # and a reworded clause reddens it until the mapping is revisited.
    FORBID_PROMPT_FOR = (
        ("analysis", "Last month: 1,200 signups from 9,800 visits. The month before: 1,050 signups from 8,900 visits. What changed, and what should we look at first?"),
        ("a short document", "Write a one-paragraph announcement that the library's opening hours change next month."),
        ("a recap", "Turn these notes into a three-bullet recap: release moved to Friday, two bugs still open, the docs page has no owner yet."),
        ("one file", "Fix the off-by-one in this function: def last_n(items, n): return items[-n-1:]"),
    )

    def test_forbid_prompts_cover_every_exclusion_example(self):
        with open(BUILD_MD) as handle:
            clause = route_lib.extract_exclusion_clause(handle.read())
        examples = [item.strip() for item in clause.split(",") if item.strip()]
        self.assertEqual(examples, [k for k, _ in self.FORBID_PROMPT_FOR])
        forbids = [c["prompt"] for c in self.cases if c.get("forbid")]
        self.assertEqual(forbids, [p for _, p in self.FORBID_PROMPT_FOR])


if __name__ == "__main__":
    unittest.main()
