#!/usr/bin/env python3
"""Tests for the context budget report.

Every assertion is against a FIXTURE tree. The user's live files are ambient
state that other work changes daily, so a test asserting against them passes or
fails for reasons unrelated to this code -- the mistake the parent mission's
own C1 finding named.
"""

import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import shutil
import unittest
from unittest import mock

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

# Pin the config away from this machine's, as the sibling suites do: with
# config=None the libs read whatever is installed here, so without this the
# suite would pass on the author's machine and fail on anyone else's.
os.environ["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent-context-budget-test.json"

sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(REPO, "tools", "memory-lint"))
sys.path.insert(0, os.path.join(REPO, "tools", "context-growth"))
sys.path.insert(0, os.path.join(REPO, "tools", "context-lint"))
sys.path.insert(0, os.path.join(REPO, "tools", "tooling-digest"))
import budget_lib
import context_lint_lib
import digest_lib
import growth_lib
import hook_manifest
import instruction_lib
import lint_lib
import readings_lib
import toolkit_config


EXE = os.path.join(TOOL_DIR, "claude-context-budget")



def load_cli_module():
    """Import the extensionless `claude-context-budget` executable as a
    module, so `main()` can be called IN-PROCESS with `budget_lib` functions
    monkeypatched (TestRecordShortCircuit's acceptance-7 test). Same technique
    as tools/spec-archive/tests/test_cli.py's `load_cli_module`."""
    loader = importlib.machinery.SourceFileLoader("claude_context_budget_cli", EXE)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module

# A `paths:` frontmatter block, so a rules-file fixture is scoped rather than
# pathless. Body lines are appended after it.
SCOPED_FRONTMATTER = "---\npaths:\n  - \"**\"\n---\n"


def _hermetic_env(**overrides):
    """A `dict(os.environ, ...)` for a subprocess, minus `CLAUDE_CONFIG_DIR`
    (Code Review FIX 3). A test's sandboxed `HOME` is meant to be the whole
    of the executable's config home; an inherited `CLAUDE_CONFIG_DIR` wins
    over `HOME` (see `lib/claude_home.py`) and silently redirects every rules
    read to whatever the RUNNING SHELL happens to be relocated to -- the same
    ambient-surface trap the repo's own CLAUDE.md warns about for `GIT_*`
    vars. A test that relocates CLAUDE_CONFIG_DIR on purpose sets it back
    into the env dict this returns, after the fact."""
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    env.update(overrides)
    return env


class Tree(object):
    """A throwaway workspace."""

    def __init__(self):
        self.path = tempfile.mkdtemp(prefix="ctxbudget-")

    def write(self, rel, text):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        return full

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)


class TestClaudeMdDiscovery(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)

    def test_finds_every_claude_md_not_a_fixed_list(self):
        """The first draft of this tool enumerated six and there are seven."""
        for rel in (
            "CLAUDE.md",
            "workspace/CLAUDE.md",
            "workspace/project/CLAUDE.md",
            "repos/a/CLAUDE.md",
            "repos/b/CLAUDE.md",
        ):
            self.t.write(rel, "x\n")
        found = budget_lib.claude_md_surfaces(self.t.path)
        self.assertEqual(len(found), 5)

    def test_skips_vcs_and_worktree_noise(self):
        """Each skipped directory gets its OWN reachable fixture.

        The first version nested the worktree fixture under `.claude`, so
        `.claude` and `worktrees` shadowed each other: deleting EITHER from the
        skip set left this test green, and both mutations survived. A fixture
        that two rules can catch tests neither of them.
        """
        self.t.write("CLAUDE.md", "x\n")
        self.t.write(".git/CLAUDE.md", "x\n")
        self.t.write(".claude/CLAUDE.md", "x\n")
        self.t.write("worktrees/wt/CLAUDE.md", "x\n")
        self.t.write("node_modules/pkg/CLAUDE.md", "x\n")
        self.t.write(".venv/lib/CLAUDE.md", "x\n")
        found = budget_lib.claude_md_surfaces(self.t.path)
        self.assertEqual(
            found, [os.path.realpath(os.path.join(self.t.path, "CLAUDE.md"))]
        )

    def test_missing_root_is_empty_not_an_exception(self):
        self.assertEqual(budget_lib.claude_md_surfaces("/nonexistent/root"), [])
        self.assertEqual(budget_lib.claude_md_surfaces(None), [])


class TestUnits(unittest.TestCase):
    """The units are per-surface and must not be swapped."""

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)

    def test_measure_reports_lines_chars_and_bytes_separately(self):
        lines, chars, byts = budget_lib._measure("a — b\nc\n")
        self.assertEqual(lines, 2)
        self.assertEqual(chars, 8)
        self.assertGreater(byts, chars)  # the em dash is 3 bytes, 1 char

    def test_the_index_is_reported_in_CHARS_not_bytes(self):
        """The defect that put two earlier sessions on the wrong side."""
        mem = os.path.join(self.t.path, "memory")
        os.makedirs(mem)
        with open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("- [A](a.md) — hook with an em dash\n")
        with open(os.path.join(mem, "a.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: a\n---\n\nbody\n")
        surfaces = budget_lib.scan(self.t.path, mem)
        index = [s for s in surfaces if s["name"] == "MEMORY.md index"]
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["unit"], "chars")
        with open(os.path.join(mem, "MEMORY.md"), encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(index[0]["value"], len(text))
        self.assertNotEqual(index[0]["value"], len(text.encode("utf-8")))

    def test_injected_total_never_adds_lines_to_chars(self):
        """A line is not a number of characters.

        Summing them would produce a confident total in no unit at all, which
        is the 'derived number dressed as a reading' rule this repo already has.
        """
        surfaces = [
            {"unit": "chars", "value": 100},
            {"unit": "lines", "value": 900},
            {"unit": "chars", "value": 50},
        ]
        self.assertEqual(budget_lib.injected_total(surfaces), 150)


class TestIndexLinesRow(unittest.TestCase):
    """`MEMORY.md index lines`. Beside the existing chars row,
    a lines row measured against lint_lib's harness-anchored line cap."""

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)
        self.mem = os.path.join(self.t.path, "memory")
        os.makedirs(self.mem)

    def _write_index(self, line_count):
        """An index whose harness_line_count is exactly `line_count`: no
        trailing blank line, so text.count("\\n") + 1 == line_count."""
        text = ("- [A](a.md) — hook\n") * line_count
        path = os.path.join(self.mem, "MEMORY.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        self.assertEqual(lint_lib.harness_line_count(text), line_count)
        return text

    def _lines_row(self, surfaces):
        rows = [s for s in surfaces if s["name"] == "MEMORY.md index lines"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_AC3_1_row_exists_well_under_cap(self):
        cap = lint_lib.max_index_lines(None)
        self.assertEqual(cap, 190)  # no config -> DEFAULT_MAX_INDEX_LINES
        line_count = cap - 100
        self._write_index(line_count)
        surfaces = budget_lib.scan(self.t.path, self.mem)
        row = self._lines_row(surfaces)
        self.assertEqual(row["unit"], "lines")
        self.assertEqual(row["value"], line_count)
        self.assertEqual(row["cap"], cap)
        self.assertEqual(row["over"], 0)

    def test_AC3_2_over_cap_reports_the_overage(self):
        cap = lint_lib.max_index_lines(None)
        line_count = 195
        self.assertGreater(line_count, cap)  # precondition: this fixture IS over
        self._write_index(line_count)
        surfaces = budget_lib.scan(self.t.path, self.mem)
        row = self._lines_row(surfaces)
        self.assertEqual(row["over"], line_count - cap)

    def test_AC3_3_existing_chars_row_is_unchanged(self):
        text = self._write_index(50)
        surfaces = budget_lib.scan(self.t.path, self.mem)
        chars_rows = [s for s in surfaces if s["name"] == "MEMORY.md index"]
        self.assertEqual(len(chars_rows), 1)
        chars_row = chars_rows[0]
        self.assertEqual(chars_row["unit"], "chars")
        self.assertEqual(chars_row["value"], len(text))
        self.assertEqual(chars_row["cap"], lint_lib.max_index_chars(None))
        # The lines row is present alongside it, not in place of it.
        self._lines_row(surfaces)


class TestDisabledPluginsAreNotCounted(unittest.TestCase):
    """A disabled plugin's skills are injected into nothing.

    Skills from plugins set `false` in settings can be a large share of the
    cache. This figure is what a decision to
    prune skills would be made from, so counting a skill that costs zero argues
    for cutting the wrong thing.
    """

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)
        self.cache = os.path.join(self.t.path, "cache")
        for market, plugin, skill, desc in (
            ("mkt", "liveplug", "alpha", "a" * 100),
            ("mkt", "deadplug", "beta", "b" * 300),
        ):
            self.t.write(
                os.path.join(
                    "cache", market, plugin, "1.0.0", "skills", skill, "SKILL.md"
                ),
                "---\nname: %s\ndescription: %s\n---\n\nbody\n" % (skill, desc),
            )
        self.settings = self.t.write(
            "settings.json",
            '{"enabledPlugins": {"liveplug@mkt": true, "deadplug@mkt": false}}',
        )

    def test_a_disabled_plugins_skills_are_excluded_and_counted_separately(self):
        total, count, off, off_chars = budget_lib.skill_description_chars(
            [self.cache], self.settings
        )
        self.assertEqual(count, 1)
        self.assertEqual(total, 100)
        self.assertEqual(off, 1)
        self.assertEqual(off_chars, 300)

    def test_absent_from_the_map_means_ENABLED_not_disabled(self):
        """Otherwise one unlisted plugin silently zeroes the whole surface."""
        settings = self.t.write("s2.json", '{"enabledPlugins": {}}')
        total, count, off, _ = budget_lib.skill_description_chars(
            [self.cache], settings
        )
        self.assertEqual(count, 2)
        self.assertEqual(total, 400)
        self.assertEqual(off, 0)

    def test_unreadable_or_absent_settings_disables_nothing(self):
        total, count, off, _ = budget_lib.skill_description_chars(
            [self.cache], os.path.join(self.t.path, "nonexistent.json")
        )
        self.assertEqual((count, total, off), (2, 400, 0))

    def test_a_temp_clone_copy_does_not_resurrect_a_disabled_plugin(self):
        """The defect the FIRST version of this fix shipped with.

        The cache also holds transient clone dirs shaped
        `<cache>/temp_git_<n>_<slug>/skills/<skill>/` — no marketplace, no
        version. Read with a 3-segment assumption they yield a key in no
        settings map, so a disabled plugin's skill that also has a scratch copy
        counted as enabled, understating the excluded count.
        """
        self.t.write(
            os.path.join("cache", "temp_git_1789211522602_l9baxe", "skills",
                         "beta", "SKILL.md"),
            "---\nname: beta\ndescription: %s\n---\n\nbody\n" % ("b" * 300),
        )
        total, count, off, off_chars = budget_lib.skill_description_chars(
            [self.cache], self.settings
        )
        self.assertEqual((count, total), (1, 100))
        self.assertEqual((off, off_chars), (1, 300))

    def test_only_the_newest_cached_version_of_a_skill_is_counted(self):
        """546 SKILL.md live in this cache, up to 16 versions of one plugin;
        one is loaded. Counting per file made the total depend on walk order."""
        # The NEWEST copy is deliberately the one os.walk reaches FIRST
        # (`aaa` sorts before `zzz`). With last-one-wins the answer would be
        # `zzz`'s 100 — so this fails unless mtime actually decides. Ordered the
        # other way the test passed under a last-wins mutation, which is how it
        # survived the first mutation run.
        # A skill name of its own: setUp's `alpha` is written with an mtime of
        # NOW, so reusing that name makes setUp's copy the newest and the test
        # fails for a reason unrelated to what it checks.
        for version, size, mtime in (("aaa", 250, 1_700_000_000),
                                     ("zzz", 100, 1_600_000_000)):
            path = self.t.write(
                os.path.join("cache", "mkt", "liveplug", version, "skills",
                             "gamma", "SKILL.md"),
                "---\nname: gamma\ndescription: %s\n---\n\nbody\n" % ("g" * size),
            )
            os.utime(path, (mtime, mtime))
        total, count, _off, _c = budget_lib.skill_description_chars(
            [self.cache], self.settings
        )
        self.assertEqual(count, 2)  # alpha from setUp, plus gamma
        self.assertEqual(total, 100 + 250)

    def test_the_report_says_what_it_excluded(self):
        """A figure that silently differs from a hand count is worse than a
        bigger one: the reader cannot reconcile it and stops trusting it."""
        text = budget_lib.report(
            self.t.path, None, [self.cache], None, self.settings
        )
        self.assertIn("excluded as disabled plugins", text)


class TestReport(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)

    def test_over_cap_surfaces_are_named_by_PATH_in_the_summary(self):
        """Seven surfaces are called CLAUDE.md; naming one identifies nothing.

        The summary is the line someone acts on without scrolling back through
        the table, so it has to say WHICH file.
        """
        # Sized off the cap, not a literal. This was 401 against a 400 default;
        # when the default became 800 on 2026-09-15 the "over cap" file was
        # under cap, the summary section did not exist, and the assertion below
        # searched an empty string. A fixture that names a threshold by literal
        # stops being a fixture for that threshold the moment it moves.
        over = self.t.write("repos/skills/CLAUDE.md",
                            "x\n" * (context_lint_lib.DEFAULT_MAX_LINES + 1))
        self.t.write("repos/tidy/CLAUDE.md", "x\n" * 10)
        text = budget_lib.report(self.t.path, None, [])
        summary = text.split("over cap")[-1]
        self.assertIn(os.path.realpath(over), summary)
        self.assertNotIn(os.path.realpath(self.t.path + "/repos/tidy/CLAUDE.md"),
                         summary)

    def test_a_tidy_tree_says_no_surface_over_cap(self):
        self.t.write("CLAUDE.md", "x\n" * 10)
        self.assertIn("no surface over cap", budget_lib.report(self.t.path, None, []))

    def test_report_on_an_empty_tree_does_not_raise(self):
        self.assertIsInstance(budget_lib.report(self.t.path, None, []), str)

    def test_an_unreadable_surface_is_REPORTED_not_omitted(self):
        """A silently missing row reads as a surface that costs nothing."""
        path = self.t.write("CLAUDE.md", "x\n")
        os.chmod(path, 0)
        self.addCleanup(os.chmod, path, 0o644)
        if os.access(path, os.R_OK):  # running as root: the premise cannot hold
            self.skipTest("cannot make a file unreadable here")
        text = budget_lib.report(self.t.path, None, [])
        self.assertIn("unreadable", text)


class TestRulesSurfaces(unittest.TestCase):
    """`rules_surfaces` — every `*.md` under each given directory."""

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)

    def test_finds_files_at_top_level_and_in_a_subdirectory(self):
        a = self.t.write(".claude/rules/a.md", "x\n")
        b = self.t.write(".claude/rules/b.md", "x\n")
        c = self.t.write(".claude/rules/sub/c.md", "x\n")
        found = budget_lib.rules_surfaces(
            [os.path.join(self.t.path, ".claude", "rules")])
        self.assertEqual(found, sorted((a, b, c)))

    def test_a_non_md_file_is_ignored(self):
        self.t.write(".claude/rules/a.md", "x\n")
        self.t.write(".claude/rules/notes.txt", "x\n")
        found = budget_lib.rules_surfaces(
            [os.path.join(self.t.path, ".claude", "rules")])
        self.assertEqual(len(found), 1)

    def test_a_missing_directory_yields_no_files(self):
        self.assertEqual(budget_lib.rules_surfaces(["/nonexistent/dir"]), [])
        self.assertEqual(budget_lib.rules_surfaces([None]), [])
        self.assertEqual(budget_lib.rules_surfaces(None), [])

    def test_the_same_directory_twice_yields_no_duplicates(self):
        self.t.write(".claude/rules/a.md", "x\n")
        d = os.path.join(self.t.path, ".claude", "rules")
        found = budget_lib.rules_surfaces([d, d])
        self.assertEqual(len(found), 1)

    def test_a_symlinked_subdirectory_is_found_and_the_path_stays_logical(self):
        """Claude Code follows a symlinked shared-rules subdirectory, so the
        walk must too — and the returned path must still contain
        `/.claude/rules/` so `growth_lib` classifies it as a rules file
        (Fix A: a realpath'd return strips that segment)."""
        real_file = self.t.write("shared_real/topic.md", "x\n")
        rules_dir = os.path.join(self.t.path, ".claude", "rules")
        os.makedirs(rules_dir, exist_ok=True)
        os.symlink(os.path.dirname(real_file),
                   os.path.join(rules_dir, "shared"))
        found = budget_lib.rules_surfaces([rules_dir])
        self.assertEqual(len(found), 1)
        self.assertIn("/.claude/rules/", found[0].replace(os.sep, "/"))
        self.assertTrue(found[0].endswith("topic.md"))

    def test_a_symlink_cycle_terminates_and_lists_each_real_file_once(self):
        rules_dir = os.path.join(self.t.path, ".claude", "rules")
        self.t.write(".claude/rules/a.md", "x\n")
        os.symlink(rules_dir, os.path.join(rules_dir, "loop"))
        found = budget_lib.rules_surfaces([rules_dir])
        self.assertEqual(len(found), 1)

    def test_the_walk_never_enters_a_real_directory_twice(self):
        """The cycle guard, measured on the WALK rather than on the result.

        The test above cannot see the guard: without it, macOS stops the loop
        at ELOOP (~32 levels) and the realpath dedupe hides every repeat, so
        the output is identical. Two loop links in one directory make that
        unguarded walk branch 2^depth — a hang, not a wrong answer. Counting
        the directories `os.walk` yields catches the guard's removal without
        ever running the hang.
        """
        rules_dir = os.path.join(self.t.path, ".claude", "rules")
        self.t.write(".claude/rules/a.md", "x\n")
        os.symlink(rules_dir, os.path.join(rules_dir, "loop"))
        entered = []
        real_walk = os.walk

        def counting_walk(*args, **kwargs):
            for step in real_walk(*args, **kwargs):
                entered.append(os.path.realpath(step[0]))
                yield step

        budget_lib.os.walk = counting_walk
        try:
            budget_lib.rules_surfaces([rules_dir])
        finally:
            budget_lib.os.walk = real_walk
        self.assertEqual(len(entered), len(set(entered)), entered)


class TestRulesRows(unittest.TestCase):
    """`scan`/`report` — a row per rules file, pathless files in the summary."""

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)

    def test_a_scoped_files_row_has_lines_cap_200_and_path_scoped_note(self):
        self.t.write(".claude/rules/topic.md", SCOPED_FRONTMATTER + "row\n" * 5)
        surfaces = budget_lib.scan(self.t.path, None, [])
        rows = [s for s in surfaces if s["name"] == "rules file"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["unit"], "lines")
        self.assertEqual(rows[0]["cap"], growth_lib.DEFAULT_RULES_MAX_LINES)
        self.assertEqual(rows[0]["note"], "path-scoped")

    def test_a_pathless_files_row_note_starts_NO_paths(self):
        self.t.write(".claude/rules/topic.md", "row\n" * 5)
        surfaces = budget_lib.scan(self.t.path, None, [])
        rows = [s for s in surfaces if s["name"] == "rules file"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["note"].startswith("NO paths:"))

    def test_an_over_cap_scoped_file_appears_in_the_summary_by_path(self):
        over = self.t.write(
            ".claude/rules/big.md",
            SCOPED_FRONTMATTER + "row\n" * (growth_lib.DEFAULT_RULES_MAX_LINES + 50))
        text = budget_lib.report(self.t.path, None, [])
        summary = text.split("over cap")[-1]
        self.assertIn(over, summary)

    def test_the_pathless_summary_line_names_only_the_pathless_file(self):
        scoped = self.t.write(".claude/rules/scoped.md",
                              SCOPED_FRONTMATTER + "row\n" * 5)
        pathless = self.t.write(".claude/rules/pathless.md", "row\n" * 5)
        text = budget_lib.report(self.t.path, None, [])
        self.assertIn("no paths:", text)
        no_paths_section = text[text.index("no paths:"):]
        self.assertIn(pathless, no_paths_section)
        self.assertNotIn(scoped, no_paths_section)

    def test_a_tree_of_only_scoped_rules_has_no_pathless_summary_line(self):
        self.t.write(".claude/rules/scoped.md", SCOPED_FRONTMATTER + "row\n" * 5)
        text = budget_lib.report(self.t.path, None, [])
        self.assertNotIn("no paths:", text)

    def test_a_symlinked_file_gets_the_rules_cap_not_the_claude_md_cap(self):
        """The bug Fix A closes: `rules_surfaces` used to return a REALPATH,
        which for a symlinked file strips the `/.claude/rules/` segment and
        makes `caps_for_path` resolve the CLAUDE.md-family cap (800) instead
        of the rules cap (200)."""
        real = self.t.write("outside/linked.md",
                            SCOPED_FRONTMATTER + "row\n" * 5)
        rules_dir = os.path.join(self.t.path, ".claude", "rules")
        os.makedirs(rules_dir, exist_ok=True)
        os.symlink(real, os.path.join(rules_dir, "linked.md"))
        surfaces = budget_lib.scan(self.t.path, None, [])
        rows = [s for s in surfaces if s["name"] == "rules file"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cap"], growth_lib.DEFAULT_RULES_MAX_LINES)

    def test_a_file_in_a_separate_user_rules_dir_gets_a_row(self):
        user = Tree()
        self.addCleanup(user.cleanup)
        user.write("topic.md", SCOPED_FRONTMATTER + "row\n" * 5)
        surfaces = budget_lib.scan(self.t.path, None, [],
                                   user_rules_dir=user.path)
        rows = [s for s in surfaces if s["name"] == "rules file"]
        self.assertEqual(len(rows), 1)


class TestClaudeConfigDirUserRules(unittest.TestCase):
    """`measure_instructions`/`always_on_candidates`
    honour a relocated `$CLAUDE_CONFIG_DIR` through `claude_home.
    user_rules_dir(home)`, in place of the hard-coded `<home>/.claude/rules`
    join. CLAUDE_CONFIG_DIR is set/unset explicitly and restored in
    tearDown -- hermetic, never inheriting the ambient value."""

    def setUp(self):
        self._previous = os.environ.get("CLAUDE_CONFIG_DIR")
        self.t = Tree()
        self.addCleanup(self.t.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)
        self.cfg = Tree()
        self.addCleanup(self.cfg.cleanup)

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._previous

    def test_ac2_4b_measure_instructions_and_candidates_honour_config_dir(self):
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg.path
        cfg_rule = self.cfg.write("rules/r.md", SCOPED_FRONTMATTER + "row\n" * 5)
        old_rule = self.home.write(".claude/rules/old.md",
                                   SCOPED_FRONTMATTER + "row\n" * 5)

        records = budget_lib.measure_instructions(
            self.t.path, None, None, None, self.home.path)
        keys = {r["key"] for r in records}
        self.assertIn(cfg_rule, keys)
        self.assertNotIn(old_rule, keys)

        candidates = budget_lib.always_on_candidates(
            self.t.path, None, None, self.home.path)
        self.assertIn(os.path.realpath(cfg_rule), candidates)


class TestExecutable(unittest.TestCase):
    """Driven through the executable, not only the lib."""

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)
        # A sandbox HOME for every subprocess in this class: with the real
        # HOME inherited, `--report` reaches this machine's own ~/.claude
        # through `resolve_memory_dir` and the always-on
        # CLAUDE.md family, neither of which any test here is asserting
        # against.
        self.home = Tree()
        self.addCleanup(self.home.cleanup)

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, EXE] + list(args),
            capture_output=True,
            text=True,
            env=_hermetic_env(
                HOME=self.home.path,
                CLAUDE_TOOLKIT_CONFIG="/nonexistent.json",
            ),
        )

    def test_report_exits_zero_and_prints(self):
        self.t.write("CLAUDE.md", "x\n" * 10)
        result = self._run("--report", self.t.path)
        self.assertEqual(result.returncode, 0)
        self.assertIn("CLAUDE.md", result.stdout)

    def test_report_does_not_read_stdin(self):
        """A --report that reads stdin HANGS when a human runs it.

        The control has to be an OPEN pipe that never closes. The first version
        of this test passed `stdin=subprocess.DEVNULL`, which returns EOF
        instantly: a `sys.stdin.read()` inserted at the top of `main()` sailed
        through it. Worse, it also passed when stdin was merely inherited, so
        whether the test could fail at all depended on the surface the suite ran
        on -- and in CI, where stdin is /dev/null, it could not.

        Here the pipe is held open and never written to, so a tool that reads
        stdin blocks until the timeout and the test fails on TimeoutExpired.
        """
        self.t.write("CLAUDE.md", "x\n")
        # NOT `communicate()`: with no input to send it CLOSES stdin straight
        # away, which is the same instant EOF as DEVNULL — a `sys.stdin.read()`
        # inserted into main() sailed through that too. The pipe is held open
        # here for the whole wait, and stdout goes to a FILE so an unread pipe
        # buffer cannot deadlock the child instead.
        out_path = os.path.join(self.t.path, "out.txt")
        with open(out_path, "w") as out_fh:
            proc = subprocess.Popen(
                [sys.executable, EXE, "--report", self.t.path],
                stdin=subprocess.PIPE,
                stdout=out_fh,
                stderr=subprocess.STDOUT,
                env=_hermetic_env(
                    HOME=self.home.path,
                    CLAUDE_TOOLKIT_CONFIG="/nonexistent.json",
                ),
            )
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail("claude-context-budget --report blocked on stdin")
            finally:
                proc.stdin.close()
        self.assertEqual(proc.returncode, 0)
        with open(out_path, encoding="utf-8") as fh:
            self.assertIn("CLAUDE.md", fh.read())

    def test_a_nonexistent_root_still_exits_zero(self):
        result = self._run("--report", "/nonexistent/workspace")
        self.assertEqual(result.returncode, 0)

    def test_help_exits_zero(self):
        self.assertEqual(self._run("--help").returncode, 0)

    def test_rules_files_from_workspace_and_user_home_both_appear(self):
        """`HOME` and `CLAUDE_TOOLKIT_CONFIG` set explicitly, not inherited —
        the environment IS the ambient state the rule in CLAUDE.md warns
        about, so this test builds it rather than trusting `os.environ`."""
        home = Tree()
        self.addCleanup(home.cleanup)
        user_rules = home.write(".claude/rules/user-topic.md",
                                SCOPED_FRONTMATTER + "row\n" * 5)
        ws_rules = self.t.write(".claude/rules/ws-topic.md", "row\n" * 5)
        env = _hermetic_env(HOME=home.path,
                             CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        result = subprocess.run(
            [sys.executable, EXE, "--report", self.t.path],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0)
        # user_rules stays LOGICAL (never realpath'd): `user_rules_dir` is
        # built with plain `os.path.expanduser`, never `os.path.realpath`, so
        # its rows carry the path as given. ws_rules, by contrast, is
        # realpath'd upstream of `rules_surfaces` -- `_workspace_root` in
        # this executable resolves the workspace-root argument before it
        # ever reaches `scan`/`rules_surfaces` -- so it is compared resolved.
        self.assertIn(user_rules, result.stdout)
        self.assertIn(os.path.realpath(ws_rules), result.stdout)
        self.assertIn("no paths:", result.stdout)
        # Bounded to the blank line that ends this block: the report appends the
        # instructions-meter section after a blank line, and that section's
        # own "conditional" line legitimately names `user_rules` (it is a
        # scoped, always-loaded-on-demand surface) — a slice running to the
        # end of stdout would catch that unrelated mention instead of testing
        # this block's own pathless-only claim.
        no_paths_section = result.stdout[result.stdout.index("no paths:"):].split(
            "\n\n"
        )[0]
        self.assertIn(os.path.realpath(ws_rules), no_paths_section)
        self.assertNotIn(user_rules, no_paths_section)

    def test_ac2_4_claude_config_dir_relocates_the_user_rules_row(self):
        """AC2.4 -- CLAUDE_CONFIG_DIR wins over the hard-coded
        <home>/.claude/rules for the executable's own report, not just the
        lib. CLAUDE_CONFIG_DIR is built into this subprocess's env
        explicitly, never inherited from the running shell.

        Code Review FIX 2 -- asserts the SURFACE TABLE row specifically,
        not "anywhere in stdout": `measure_instructions` also prints the
        relocated path in the always-on-instructions section below the
        table, so a stdout-wide search for `cfg_rule` survives dropping
        the table row entirely (e.g. `user_rules_dir=None` in the
        surface-table call). The table row's own shape -- confirmed
        against a real `--report` run -- is two lines: `rules file ...`
        then an indented path line directly below it. This must be found
        before the "always-on instructions" section header, which is
        where that section's own (legitimate) mention of the same path
        lives."""
        cfg = Tree()
        self.addCleanup(cfg.cleanup)
        cfg_rule = cfg.write("rules/r.md", SCOPED_FRONTMATTER + "row\n" * 5)
        home_rule = self.home.write(".claude/rules/old.md",
                                    SCOPED_FRONTMATTER + "row\n" * 5)
        env = _hermetic_env(HOME=self.home.path,
                             CLAUDE_CONFIG_DIR=cfg.path,
                             CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        result = subprocess.run(
            [sys.executable, EXE, "--report", self.t.path],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(home_rule, result.stdout)
        header_idx = result.stdout.index("always-on instructions")
        table_block = result.stdout[:header_idx]
        lines = table_block.split("\n")
        row_idx = next(
            (i for i, line in enumerate(lines) if line.startswith("rules file")),
            None,
        )
        self.assertIsNotNone(
            row_idx, "no 'rules file' row in the surface table:\n" + table_block)
        self.assertIn(cfg_rule, lines[row_idx + 1])


class _FakeConfig(object):
    """The narrow slice of `toolkit_config`'s interface `resolve_memory_dir`
    reads: `get_path("context_budget", "memory_dir")`."""

    def __init__(self, memory_dir=None):
        self._override = memory_dir

    def get_path(self, *keys, **kwargs):
        if keys == ("context_budget", "memory_dir") and self._override:
            return self._override
        return kwargs.get("default")


class TestAlwaysOnFiles(unittest.TestCase):
    """`always_on_files` — the fixed always-on CLAUDE.md-family inventory."""

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)

    def test_returns_all_six_managed_first_a_deep_descendant_is_excluded(self):
        root = os.path.join(self.t.path, "root")
        os.makedirs(root)
        self.t.write("CLAUDE.md", "ancestor\n")  # one level ABOVE root
        with open(os.path.join(root, "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write("root\n")
        os.makedirs(os.path.join(root, ".claude"))
        with open(os.path.join(root, ".claude", "CLAUDE.md"), "w", encoding="utf-8") as fh:
            fh.write("nested\n")
        with open(os.path.join(root, "CLAUDE.local.md"), "w", encoding="utf-8") as fh:
            fh.write("local\n")
        self.home.write(".claude/CLAUDE.md", "user\n")
        managed = self.home.write("managed/CLAUDE.md", "managed\n")
        deep = os.path.join(root, "sub1", "sub2", "CLAUDE.md")
        os.makedirs(os.path.dirname(deep))
        with open(deep, "w", encoding="utf-8") as fh:
            fh.write("too deep\n")

        found = budget_lib.always_on_files(root, self.home.path, managed_path=managed)
        self.assertEqual(len(found), 6)
        self.assertEqual(found[0], managed)
        self.assertNotIn(deep, [os.path.realpath(f) for f in found])

    def test_missing_root_or_home_does_not_raise(self):
        found = budget_lib.always_on_files(None, None, managed_path="/nonexistent")
        self.assertEqual(found, [])


class TestAlwaysOnCandidates(unittest.TestCase):
    """`always_on_candidates` -- the cheap gate the always-on branch checks an
    edited file against, without measuring anything. `F` is `self.f.path`;
    the workspace lives at `F/outer/ws`, an ancestor `CLAUDE.md` at
    `F/outer`, a memory dir NOT named `memory` at `F/mem` (so a growth_lib
    test on the same shape can show `in_scope` is False for it while this
    function still counts it), and `HOME` at `F/home`.

    `managed_path` is ALWAYS passed explicitly (P9): the shipped default is
    a fixed system path this suite must not depend on.
    """

    def setUp(self):
        self.f = Tree()
        self.addCleanup(self.f.cleanup)
        self.ws = os.path.join(self.f.path, "outer", "ws")
        self.home = os.path.join(self.f.path, "home")
        self.mem = os.path.join(self.f.path, "mem")

        self.ancestor_claude_md = self.f.write("outer/CLAUDE.md", "ancestor\n")
        self.claude_md = self.f.write("outer/ws/CLAUDE.md", "- item\n")
        self.claude_local = self.f.write("outer/ws/CLAUDE.local.md", "local\n")
        self.tooling = self.f.write(
            "outer/ws/TOOLING.md",
            "<!-- DIGEST:START -->\nrow\n<!-- DIGEST:END -->\n",
        )
        self.pathless_rule = self.f.write(
            "outer/ws/.claude/rules/pathless.md", "row\n"
        )
        self.scoped_rule = self.f.write(
            "outer/ws/.claude/rules/scoped.md", SCOPED_FRONTMATTER + "row\n"
        )
        self.mem_index = self.f.write("mem/MEMORY.md", "- [A](a.md) — hook\n")
        self.home_claude_md = self.f.write("home/.claude/CLAUDE.md", "user\n")

        self.config = _FakeConfig(memory_dir=self.mem)

    def _under_f(self, paths):
        real_f = os.path.realpath(self.f.path) + os.sep
        return {p for p in paths if p.startswith(real_f)}

    def test_managed_path_none_the_set_under_F_is_exactly_these_eight(self):
        candidates = budget_lib.always_on_candidates(
            self.ws, self.mem, self.config, self.home, managed_path=None
        )
        filtered = self._under_f(candidates)
        expected = {
            os.path.realpath(p)
            for p in (
                self.ancestor_claude_md,
                self.claude_md,
                self.claude_local,
                self.pathless_rule,
                self.scoped_rule,
                self.mem_index,
                self.tooling,
                self.home_claude_md,
            )
        }
        self.assertEqual(filtered, expected)

    def test_an_explicit_managed_path_is_in_the_set(self):
        managed = self.f.write("managed/CLAUDE.md", "managed\n")
        candidates = budget_lib.always_on_candidates(
            self.ws, self.mem, self.config, self.home, managed_path=managed
        )
        self.assertIn(os.path.realpath(managed), candidates)

    def test_a_config_whose_get_path_raises_returns_a_set_not_an_exception(self):
        class RaisingConfig(object):
            def get_path(self, *args, **kwargs):
                raise RuntimeError("boom")

            def get(self, *args, **kwargs):
                raise RuntimeError("boom")

        result = budget_lib.always_on_candidates(
            self.ws, self.mem, RaisingConfig(), self.home, managed_path=None
        )
        self.assertIsInstance(result, set)


class TestProjectSlug(unittest.TestCase):
    def test_non_alnum_characters_become_dashes(self):
        slug = budget_lib.project_slug("/Users/e/_Work")
        self.assertEqual(slug, re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath("/Users/e/_Work")))
        self.assertNotIn("/", slug)
        self.assertNotIn("_", slug)

    def test_matches_auto_memory_project_slug_for_underscore_dot_and_space(self):
        """`project_slug` delegates to `auto_memory.project_slug`;
        this fixture name carries `_`, `.` and a space so a delegation that
        subtly changed the regex would show up here."""
        import auto_memory

        t = Tree()
        self.addCleanup(t.cleanup)
        path = os.path.join(t.path, "a_dir.name with space")
        os.makedirs(path)
        self.assertEqual(
            budget_lib.project_slug(path), auto_memory.project_slug(path)
        )


class TestResolveMemoryDir(unittest.TestCase):
    def setUp(self):
        self.root = Tree()
        self.addCleanup(self.root.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)

    def test_branch1_config_override_wins_outright(self):
        config = _FakeConfig(memory_dir="/explicit/memory/dir")
        path, reason = budget_lib.resolve_memory_dir(self.root.path, config, self.home.path)
        self.assertEqual(path, "/explicit/memory/dir")
        self.assertIsNone(reason)

    def test_branch1_beats_a_present_autoMemoryDirectory(self):
        self.home.write(".claude/settings.json", '{"autoMemoryDirectory": true}')
        config = _FakeConfig(memory_dir="/explicit/memory/dir")
        path, reason = budget_lib.resolve_memory_dir(self.root.path, config, self.home.path)
        self.assertEqual(path, "/explicit/memory/dir")
        self.assertIsNone(reason)

    def test_no_workspace_root_and_no_override(self):
        path, reason = budget_lib.resolve_memory_dir(None, None, self.home.path)
        self.assertIsNone(path)
        self.assertEqual(reason, "no workspace root")

    def test_branch2_autoMemoryDirectory_in_home_settings(self):
        """A user-level key is resolved (through
        `auto_memory.resolve_user_dir`), not refused outright. "/x" doesn't
        exist, so this is the new "set but does not resolve" refusal -- the
        OLD refusal text ("autoMemoryDirectory is set; set
        context_budget.memory_dir") moves to the project-level case just
        below, unchanged."""
        self.home.write(".claude/settings.json", '{"autoMemoryDirectory": "/x"}')
        path, reason = budget_lib.resolve_memory_dir(self.root.path, None, self.home.path)
        self.assertIsNone(path)
        self.assertEqual(
            reason,
            "autoMemoryDirectory is set but does not resolve to a directory; "
            "set context_budget.memory_dir",
        )

    def test_user_level_key_resolving_to_an_existing_dir_is_returned(self):
        target = self.home.write(
            ".claude/projects/x/memory/MEMORY.md", "- [A](a.md) — hook\n"
        )
        target_dir = os.path.dirname(target)
        self.home.write(
            ".claude/settings.json",
            json.dumps({"autoMemoryDirectory": target_dir}),
        )
        path, reason = budget_lib.resolve_memory_dir(self.root.path, None, self.home.path)
        self.assertIsNone(reason)
        self.assertEqual(path, target_dir)

    def test_a_corrupt_user_settings_file_reads_as_no_user_key(self):
        """Invalid JSON is not a configured key: the harness ignores the file
        (MEASURED, Claude Code 2.1.280, isolated CLAUDE_CONFIG_DIR: the key in
        a truncated file left the default `projects/<slug>/memory` in use), so
        resolution falls through exactly as with no key at all. Here that is
        step 4's "no workspace root"; the control below shows the same value
        in a valid file resolves, so the fall-through is about the corruption.
        """
        target = self.home.write(
            ".claude/somewhere/memory/MEMORY.md", "- [A](a.md) — hook\n"
        )
        target_dir = os.path.dirname(target)
        self.home.write(".claude/settings.json",
                        '{"autoMemoryDirectory": %s, ' % json.dumps(target_dir))
        self.assertEqual(budget_lib.resolve_memory_dir(None, None, self.home.path),
                         (None, "no workspace root"))
        self.home.write(".claude/settings.json",
                        json.dumps({"autoMemoryDirectory": target_dir}))
        self.assertEqual(budget_lib.resolve_memory_dir(None, None, self.home.path),
                         (target_dir, None))

    def test_user_level_key_resolves_even_with_no_workspace_root(self):
        """Step 3 needs no workspace root at all -- N5 writes this key
        user-level, so it must still resolve a bare `--report` with no root
        passed in."""
        target = self.home.write(
            ".claude/somewhere/memory/MEMORY.md", "- [A](a.md) — hook\n"
        )
        target_dir = os.path.dirname(target)
        self.home.write(
            ".claude/settings.json",
            json.dumps({"autoMemoryDirectory": target_dir}),
        )
        path, reason = budget_lib.resolve_memory_dir(None, None, self.home.path)
        self.assertIsNone(reason)
        self.assertEqual(path, target_dir)

    def test_project_level_key_wins_the_old_refusal_even_when_user_level_is_valid(self):
        target = self.home.write(
            ".claude/somewhere/memory/MEMORY.md", "- [A](a.md) — hook\n"
        )
        target_dir = os.path.dirname(target)
        self.home.write(
            ".claude/settings.json",
            json.dumps({"autoMemoryDirectory": target_dir}),
        )
        self.root.write(".claude/settings.json", '{"autoMemoryDirectory": "/x"}')
        path, reason = budget_lib.resolve_memory_dir(self.root.path, None, self.home.path)
        self.assertIsNone(path)
        self.assertEqual(
            reason, "autoMemoryDirectory is set; set context_budget.memory_dir"
        )

    def test_branch2_autoMemoryDirectory_in_root_settings(self):
        self.root.write(".claude/settings.json", '{"autoMemoryDirectory": "/x"}')
        path, reason = budget_lib.resolve_memory_dir(self.root.path, None, self.home.path)
        self.assertIsNone(path)
        self.assertEqual(
            reason, "autoMemoryDirectory is set; set context_budget.memory_dir"
        )

    def test_branch2_autoMemoryDirectory_in_root_local_settings(self):
        self.root.write(".claude/settings.local.json", '{"autoMemoryDirectory": "/x"}')
        path, reason = budget_lib.resolve_memory_dir(self.root.path, None, self.home.path)
        self.assertIsNone(path)
        self.assertEqual(
            reason, "autoMemoryDirectory is set; set context_budget.memory_dir"
        )

    def test_invalid_json_reads_as_key_absent(self):
        self.home.write(".claude/settings.json", "{not json")
        path, reason = budget_lib.resolve_memory_dir(self.root.path, None, self.home.path)
        # Falls through to branch 4: no MEMORY.md at the default location.
        self.assertIsNone(path)
        self.assertTrue(reason.startswith("no MEMORY.md at"))

    def test_branch3_a_201_char_slug_is_refused(self):
        long_name = "y" * 250
        deep_root = os.path.join(self.root.path, long_name)
        os.makedirs(deep_root)
        self.assertGreater(len(budget_lib.project_slug(deep_root)), 200)
        path, reason = budget_lib.resolve_memory_dir(deep_root, None, self.home.path)
        self.assertIsNone(path)
        self.assertEqual(
            reason,
            "project key over 200 chars is hashed by the harness; set "
            "context_budget.memory_dir",
        )

    def test_branch4_no_MEMORY_md_at_the_default_location(self):
        path, reason = budget_lib.resolve_memory_dir(self.root.path, None, self.home.path)
        self.assertIsNone(path)
        self.assertTrue(reason.startswith("no MEMORY.md at"))

    def test_branch4_resolves_when_MEMORY_md_is_present(self):
        slug = budget_lib.project_slug(self.root.path)
        index = self.home.write(
            os.path.join(".claude", "projects", slug, "memory", "MEMORY.md"),
            "- [A](a.md) — hook\n",
        )
        path, reason = budget_lib.resolve_memory_dir(self.root.path, None, self.home.path)
        self.assertIsNone(reason)
        self.assertEqual(os.path.realpath(path), os.path.realpath(os.path.dirname(index)))

    def test_resolves_to_the_roots_own_dir_even_when_an_unrelated_project_is_newer(self):
        """Design Review's probe: the retired most-recently-modified fallback
        picked whichever project's MEMORY.md was touched most recently, so an
        unrelated project edited today could shadow the root's own, older
        one."""
        own_slug = budget_lib.project_slug(self.root.path)
        own = self.home.write(
            os.path.join(".claude", "projects", own_slug, "memory", "MEMORY.md"),
            "- [A](a.md) — hook\n",
        )
        os.utime(own, (1_000_000, 1_000_000))
        unrelated = self.home.write(
            os.path.join(".claude", "projects", "unrelated-slug", "memory", "MEMORY.md"),
            "- [B](b.md) — hook\n",
        )
        os.utime(unrelated, (2_000_000_000, 2_000_000_000))
        path, reason = budget_lib.resolve_memory_dir(self.root.path, None, self.home.path)
        self.assertIsNone(reason)
        self.assertEqual(os.path.realpath(path), os.path.realpath(os.path.dirname(own)))


class TestMeasureInstructions(unittest.TestCase):
    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)
        self.claude_md = self.t.write(
            "CLAUDE.md", "# Heading\n\n- item one\n- item two\n\n@x.md\n"
        )
        self.scoped_rule = self.t.write(
            ".claude/rules/scoped.md", SCOPED_FRONTMATTER + "row one\nrow two\n"
        )
        self.pathless_rule = self.t.write(
            ".claude/rules/pathless.md", "row one\nrow two\n"
        )
        self.mem = os.path.join(self.t.path, "memory")
        os.makedirs(self.mem)
        with open(os.path.join(self.mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("- [A](a.md) — hook one\n- [B](b.md) — hook two\n")

    def _records(self):
        return budget_lib.measure_instructions(
            self.t.path, self.mem, None, None, self.home.path
        )

    def _by_realpath(self, records):
        return {
            os.path.realpath(r["key"]): r
            for r in records
            if r["kind"] in ("claude_md", "rules")
        }

    def test_a_scoped_rules_file_is_conditional_a_pathless_one_always_on(self):
        by_key = self._by_realpath(self._records())
        self.assertEqual(
            by_key[os.path.realpath(self.scoped_rule)]["class"], "conditional"
        )
        self.assertEqual(
            by_key[os.path.realpath(self.pathless_rule)]["class"], "always-on"
        )

    def test_an_at_import_line_sets_partial_true(self):
        by_key = self._by_realpath(self._records())
        self.assertTrue(by_key[os.path.realpath(self.claude_md)]["partial"])

    def test_an_unreadable_always_on_claude_md_makes_the_total_none(self):
        os.chmod(self.claude_md, 0)
        self.addCleanup(os.chmod, self.claude_md, 0o644)
        if os.access(self.claude_md, os.R_OK):
            self.skipTest("cannot make a file unreadable here")
        records = self._records()
        by_key = self._by_realpath(records)
        self.assertFalse(by_key[os.path.realpath(self.claude_md)]["ok"])
        self.assertIsNone(budget_lib.always_on_total(records))

    def test_a_readable_tree_totals_the_sum_of_always_on_parts(self):
        records = self._records()
        total = budget_lib.always_on_total(records)
        expected = sum(
            r["instructions"] for r in records if r["class"] == "always-on"
        )
        self.assertIsNotNone(total)
        self.assertEqual(total, expected)

    def test_an_unimportable_growth_lib_makes_rules_records_unknown_not_always_on(self):
        """Without `growth_lib` a rules file cannot be classed scoped or
        pathless. Defaulting it to always-on over-counted silently (a count
        well above the real one); it must fail the record instead, so the
        total reads unknown. The positive control is the readable-tree test
        above: same fixture, import available, total not None."""
        with mock.patch.dict(sys.modules, {"growth_lib": None}):
            records = self._records()
        by_key = self._by_realpath(records)
        for path in (self.scoped_rule, self.pathless_rule):
            rec = by_key[os.path.realpath(path)]
            self.assertIs(rec["ok"], False)
            self.assertIn("growth_lib", rec["note"])
            self.assertIsNone(rec["instructions"])
        self.assertIsNone(budget_lib.always_on_total(records))
        # The CLAUDE.md record needs no classification and stays counted.
        self.assertIs(by_key[os.path.realpath(self.claude_md)]["ok"], True)


class TestConditionalUnreadableRendersUnreadableNotZero(unittest.TestCase):
    """FIX 1: a conditional record with `ok False` must render as
    `<path> unreadable`, never `<path> 0` -- `0` reads as a verified count of
    zero instructions, indistinguishable from an empty but readable file."""

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)
        # A CLAUDE.md at the root is always-on; one further down is
        # conditional (claude_md_surfaces minus the always-on list).
        self.t.write("CLAUDE.md", "# root\n")
        self.deep = self.t.write("workspace/sub/CLAUDE.md", "# deep\n- item\n")
        self.mem = os.path.join(self.t.path, "memory")
        os.makedirs(self.mem)
        with open(os.path.join(self.mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("- [A](a.md) — hook\n")

    def test_unreadable_conditional_renders_unreadable_not_zero(self):
        os.chmod(self.deep, 0)
        self.addCleanup(os.chmod, self.deep, 0o644)
        if os.access(self.deep, os.R_OK):
            self.skipTest("cannot make a file unreadable here (running as root?)")
        records = budget_lib.measure_instructions(
            self.t.path, self.mem, None, None, self.home.path
        )
        deep_real = os.path.realpath(self.deep)
        by_key = {r["key"]: r for r in records if r["kind"] == "claude_md"}
        self.assertEqual(by_key[deep_real]["class"], "conditional")
        self.assertFalse(by_key[deep_real]["ok"])

        lines = budget_lib.instructions_section(records, [], None)
        cond_line = next(line for line in lines if line.startswith("conditional"))
        self.assertIn("%s unreadable" % deep_real, cond_line)
        self.assertNotIn("%s 0" % deep_real, cond_line)


class _ConfigStandIn(object):
    """The narrow slice of `toolkit_config`'s interface `always_on_budget`
    reads -- `get(*keys, default=None)` -- returning a fixed value
    regardless of the keys asked for. Enough to drive `always_on_budget`
    against every value under test without a real config file."""

    def __init__(self, value):
        self._value = value

    def get(self, *keys, **kwargs):
        return self._value


def _claude_md_record(key, instructions, value, klass="always-on", ok=True):
    """One hand-built `claude_md` record, the shape `measure_instructions`
    produces -- for the budget/anchor line tests, which assert against
    `instructions_section` directly rather than a real tree."""
    return {
        "key": key,
        "kind": "claude_md",
        "class": klass,
        "ok": ok,
        "note": None if ok else "unreadable",
        "partial": False,
        "instructions": instructions if ok else None,
        "unit": "lines",
        "value": value if ok else None,
        "cap": None,
    }


class TestAlwaysOnBudgetFunction(unittest.TestCase):
    """`budget_lib.always_on_budget(config)` -- the (value, state) pair the
    budget line is built from."""

    def test_config_none_is_unset(self):
        self.assertEqual(budget_lib.always_on_budget(None), (None, "unset"))

    def test_no_key_is_unset(self):
        self.assertEqual(
            budget_lib.always_on_budget(_ConfigStandIn(None)), (None, "unset")
        )

    def test_a_positive_int_is_ok(self):
        self.assertEqual(
            budget_lib.always_on_budget(_ConfigStandIn(10)), (10, "ok")
        )

    def test_a_bool_is_invalid(self):
        self.assertEqual(
            budget_lib.always_on_budget(_ConfigStandIn(True)), (True, "invalid")
        )

    def test_a_string_is_invalid(self):
        self.assertEqual(
            budget_lib.always_on_budget(_ConfigStandIn("500")), ("500", "invalid")
        )

    def test_zero_is_invalid(self):
        self.assertEqual(
            budget_lib.always_on_budget(_ConfigStandIn(0)), (0, "invalid")
        )

    def test_a_float_is_invalid(self):
        self.assertEqual(
            budget_lib.always_on_budget(_ConfigStandIn(10.5)), (10.5, "invalid")
        )

    def test_a_list_is_invalid(self):
        self.assertEqual(
            budget_lib.always_on_budget(_ConfigStandIn([1])), ([1], "invalid")
        )

    def test_a_negative_int_is_invalid(self):
        self.assertEqual(
            budget_lib.always_on_budget(_ConfigStandIn(-3)), (-3, "invalid")
        )


class TestBudgetLineInProcess(unittest.TestCase):
    """The budget line appended after `always-on total`, in-process against
    `instructions_section` with hand-built records and a tiny config
    stand-in -- one always-on `claude_md`, `ok: True`, instructions 10,
    value 10 (T2 acceptance criteria)."""

    def _records(self):
        return [_claude_md_record("CLAUDE.md", 10, 10)]

    def _budget_line(self, lines):
        return next(line for line in lines if line.startswith("budget"))

    def test_no_key_prints_not_set(self):
        lines = budget_lib.instructions_section(
            self._records(), [], None, config=_ConfigStandIn(None)
        )
        self.assertEqual(
            self._budget_line(lines),
            "budget  not set — set context_budget.always_on_budget",
        )

    def test_config_none_prints_not_set(self):
        lines = budget_lib.instructions_section(
            self._records(), [], None, config=None
        )
        self.assertEqual(
            self._budget_line(lines),
            "budget  not set — set context_budget.always_on_budget",
        )

    def test_budget_equal_to_total_is_zero_spare_not_over(self):
        lines = budget_lib.instructions_section(
            self._records(), [], None, config=_ConfigStandIn(10)
        )
        self.assertEqual(self._budget_line(lines), "budget  10 / 10  0 spare")

    def test_budget_below_total_is_over(self):
        lines = budget_lib.instructions_section(
            self._records(), [], None, config=_ConfigStandIn(9)
        )
        self.assertEqual(self._budget_line(lines), "budget  10 / 9  OVER by 1")

    def test_a_string_budget_is_invalid_and_ignored(self):
        lines = budget_lib.instructions_section(
            self._records(), [], None, config=_ConfigStandIn("500")
        )
        self.assertEqual(
            self._budget_line(lines),
            "budget  invalid context_budget.always_on_budget: '500' — ignored",
        )

    def test_a_bool_budget_is_invalid_and_ignored(self):
        lines = budget_lib.instructions_section(
            self._records(), [], None, config=_ConfigStandIn(True)
        )
        self.assertEqual(
            self._budget_line(lines),
            "budget  invalid context_budget.always_on_budget: True — ignored",
        )

    def test_a_zero_budget_is_invalid_and_ignored(self):
        lines = budget_lib.instructions_section(
            self._records(), [], None, config=_ConfigStandIn(0)
        )
        self.assertEqual(
            self._budget_line(lines),
            "budget  invalid context_budget.always_on_budget: 0 — ignored",
        )


class TestAnchorLineInProcess(unittest.TestCase):
    """The anchor line appended after the budget line, in-process against
    `instructions_section` with hand-built records: two always-on
    `claude_md` records (instructions 60/value 100, instructions 30/value
    50), one always-on `memory_index` (instructions 20/value 9999, must NOT
    enter S or L), one CONDITIONAL `claude_md` (instructions 500/value 500,
    must NOT enter them either). S = 90, L = 150, A = round(200*90/150) =
    120, total (always-on only) = 60+30+20 = 110 (T2 acceptance criteria)."""

    def _records(self, memory_ok=True):
        return [
            _claude_md_record("CLAUDE.md-1", 60, 100),
            _claude_md_record("CLAUDE.md-2", 30, 50),
            {
                "key": "MEMORY.md",
                "kind": "memory_index",
                "class": "always-on",
                "ok": memory_ok,
                "note": None if memory_ok else "unreadable",
                "partial": False,
                "instructions": 20 if memory_ok else None,
                "unit": "chars",
                "value": 9999 if memory_ok else None,
                "cap": None,
            },
            _claude_md_record(
                "conditional/CLAUDE.md", 500, 500, klass="conditional"
            ),
        ]

    def _anchor_line(self, lines):
        return next(line for line in lines if line.startswith("anchor"))

    def test_anchor_arithmetic_and_total_ratio(self):
        lines = budget_lib.instructions_section(
            self._records(), [], None, config=None
        )
        anchor_line = self._anchor_line(lines)
        self.assertIn(
            "≈ 120 sentences at these files' own 90 sentences / 150 lines",
            anchor_line,
        )
        self.assertIn(" — total is 0.9× that", anchor_line)

    def test_no_claude_md_records_is_unavailable(self):
        records = [r for r in self._records() if r["kind"] != "claude_md"]
        lines = budget_lib.instructions_section(records, [], None, config=None)
        self.assertEqual(
            self._anchor_line(lines),
            "anchor  unavailable — no readable always-on CLAUDE.md to take "
            "a ratio from",
        )

    def test_total_none_leaves_the_budget_line_unavailable_and_drops_the_ratio(
        self,
    ):
        records = self._records(memory_ok=False)
        lines = budget_lib.instructions_section(
            records, [], None, config=_ConfigStandIn(500)
        )
        budget_line = next(line for line in lines if line.startswith("budget"))
        self.assertEqual(
            budget_line, "budget  500 — total unavailable, not compared"
        )
        anchor_line = self._anchor_line(lines)
        self.assertNotIn(" — total is", anchor_line)


def _digest_fixture(header_lines):
    """A minimal `<!-- DIGEST:START -->...<!-- DIGEST:END -->` block with
    exactly `header_lines` column-0 lines inside it, so a test can tell which
    of two candidate TOOLING.md files actually got measured by comparing the
    digest record's `value` (line count) rather than its content."""
    body = "\n".join("line %d" % i for i in range(header_lines))
    return "<!-- DIGEST:START -->\n%s\n<!-- DIGEST:END -->\n" % body


class _FakeToolingDigestConfig(object):
    """FIX 2 -- the narrow slice of `toolkit_config`'s interface the digest
    path helper and `digest_lib` itself read: `tooling_digest.path`,
    `tooling_digest.enabled`, `tooling_digest.max_lines`. Anything not
    overridden falls through to the caller's own default, matching a real
    `toolkit_config` reading an absent key."""

    def __init__(self, path=None, enabled=None, max_lines=None):
        self._path = path
        self._enabled = enabled
        self._max_lines = max_lines

    def get_path(self, *keys, **kwargs):
        if keys == ("tooling_digest", "path") and self._path is not None:
            return self._path
        return kwargs.get("default")

    def get(self, *keys, **kwargs):
        if keys == ("tooling_digest", "enabled") and self._enabled is not None:
            return self._enabled
        return kwargs.get("default")

    def get_int(self, *keys, **kwargs):
        if keys == ("tooling_digest", "max_lines") and self._max_lines is not None:
            return self._max_lines
        return kwargs.get("default")


class TestDigestPathResolution(unittest.TestCase):
    """FIX 2 (Merge Review) -- both `measure_instructions` and `scan` must
    resolve the TOOLING.md digest path the way the SessionStart loader
    (`digest_lib`) does, not by hardcoding `<workspace_root>/TOOLING.md`."""

    def setUp(self):
        self.t = Tree()
        self.addCleanup(self.t.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)
        self.mem = os.path.join(self.t.path, "memory")
        os.makedirs(self.mem)
        with open(os.path.join(self.mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("- [A](a.md) — hook\n")
        # Two DISTINGUISHABLE fixtures: different line counts, so a test can
        # tell which one was actually read without inspecting paths alone.
        self.root_tooling = self.t.write("TOOLING.md", _digest_fixture(3))
        outside = Tree()
        self.addCleanup(outside.cleanup)
        self.custom_tooling = outside.write(
            "elsewhere-tooling.md", _digest_fixture(7)
        )

    def _digest_record(self, config):
        records = budget_lib.measure_instructions(
            self.t.path, self.mem, None, config, self.home.path
        )
        digests = [r for r in records if r["kind"] == "digest"]
        return digests[0] if digests else None

    def _digest_row(self, config):
        surfaces = budget_lib.scan(
            self.t.path, self.mem, [], config=config
        )
        rows = [s for s in surfaces if s["name"] == "TOOLING digest"]
        return rows[0] if rows else None

    def test_a_custom_path_outside_the_root_is_measured_not_the_roots_own(self):
        config = _FakeToolingDigestConfig(path=self.custom_tooling)

        record = self._digest_record(config)
        self.assertIsNotNone(record)
        self.assertEqual(os.path.realpath(record["key"]),
                          os.path.realpath(self.custom_tooling))
        self.assertEqual(record["value"], 7)

        row = self._digest_row(config)
        self.assertIsNotNone(row)
        self.assertEqual(os.path.realpath(row["path"]),
                          os.path.realpath(self.custom_tooling))
        self.assertEqual(row["value"], 7)

    def test_enabled_false_means_no_digest_record_or_row_at_all(self):
        config = _FakeToolingDigestConfig(enabled=False)

        self.assertIsNone(self._digest_record(config))
        self.assertIsNone(self._digest_row(config))

    def test_no_tooling_digest_config_falls_back_to_root_tooling_md(self):
        config = _FakeToolingDigestConfig()

        record = self._digest_record(config)
        self.assertIsNotNone(record)
        self.assertEqual(os.path.realpath(record["key"]),
                          os.path.realpath(self.root_tooling))
        self.assertEqual(record["value"], 3)

        row = self._digest_row(config)
        self.assertIsNotNone(row)
        self.assertEqual(os.path.realpath(row["path"]),
                          os.path.realpath(self.root_tooling))
        self.assertEqual(row["value"], 3)

    def _output_row(self, config):
        surfaces = budget_lib.scan(self.t.path, self.mem, [], config=config)
        rows = [s for s in surfaces if s["name"] == "TOOLING digest output"]
        return rows[0] if rows else None

    def test_a_map_under_its_LINE_cap_but_over_the_CHAR_limit_reads_over(self):
        """The report certified 40/80 lines while the
        hook withheld the map. The chars row must show what the hook sees."""
        body = "\n".join("row %d %s" % (i, "x" * 300) for i in range(40))
        wide = self.t.write("wide.md", "<!-- DIGEST:START -->\n%s\n"
                                       "<!-- DIGEST:END -->\n" % body)
        config = _FakeToolingDigestConfig(path=wide, max_lines=80)

        self.assertLessEqual(self._digest_row(config)["value"], 80)
        row = self._output_row(config)
        self.assertEqual(row["unit"], "chars")
        self.assertEqual(row["cap"], digest_lib.HARNESS_MAX_CHARS)
        self.assertGreater(row["value"], digest_lib.HARNESS_MAX_CHARS)
        # The report and the hook agree on the SAME quantity.
        self.assertTrue(digest_lib.build(config).startswith("DIGEST OVER CHARACTER LIMIT"))

    def test_a_small_map_reads_the_hooks_exact_output_length(self):
        config = _FakeToolingDigestConfig(path=self.custom_tooling)
        row = self._output_row(config)
        self.assertEqual(row["value"], len(digest_lib.build(config)))
        self.assertLess(row["value"], digest_lib.HARNESS_MAX_CHARS)


class TestReportShowsResolverReasonOnce(unittest.TestCase):
    """The MEMORY.md row, through the executable, when no memory dir resolves."""

    def test_unreadable_reason_appears_exactly_once_on_that_row(self):
        root = Tree()
        self.addCleanup(root.cleanup)
        root.write("CLAUDE.md", "x\n")
        home = Tree()
        self.addCleanup(home.cleanup)
        # An UNRELATED project dir, so the resolver's default lookup for
        # THIS root's own slug still finds nothing.
        home.write(
            os.path.join(".claude", "projects", "unrelated-slug", "memory", "MEMORY.md"),
            "- [A](a.md) — hook\n",
        )
        env = _hermetic_env(HOME=home.path, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        result = subprocess.run(
            [sys.executable, EXE, "--report", root.path],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0)
        lines = result.stdout.splitlines()
        idx = next(i for i, line in enumerate(lines) if "MEMORY.md index" in line)
        row_text = "\n".join(lines[idx:idx + 2])
        self.assertIn("unreadable: no MEMORY.md at", row_text)
        self.assertEqual(row_text.count("unreadable"), 1)


class TestRecordCLI(unittest.TestCase):
    """`claude-context-budget --record` -- the SessionStart recorder."""

    def setUp(self):
        self.root = Tree()
        self.addCleanup(self.root.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)

    def _write_config(self, workspace_root=None, readings_path=None):
        config = {}
        if workspace_root is not None:
            config["workspace_root"] = workspace_root
        if readings_path is not None:
            config["context_budget"] = {"readings_path": readings_path}
        return self.home.write("config.json", json.dumps(config))

    def _run(self, config_path, timeout=15):
        env = _hermetic_env(HOME=self.home.path, CLAUDE_TOOLKIT_CONFIG=config_path)
        return subprocess.run(
            [sys.executable, EXE, "--record"],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout,
        )

    def test_no_workspace_root_records_nothing_and_is_silent(self):
        config_path = self._write_config()
        result = self._run(config_path)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertFalse(
            os.path.exists(
                os.path.join(self.home.path, ".claude", "context-budget",
                             "readings.jsonl")
            )
        )

    def test_a_configured_root_writes_one_line_a_second_run_stays_one(self):
        self.root.write("CLAUDE.md", "- item one\n- item two\n")
        readings_file = os.path.join(self.home.path, "readings.jsonl")
        config_path = self._write_config(
            workspace_root=self.root.path, readings_path=readings_file
        )

        result1 = self._run(config_path)
        self.assertEqual(result1.returncode, 0)
        self.assertEqual(result1.stdout, "")
        self.assertEqual(result1.stderr, "")
        with open(readings_file, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 1)

        result2 = self._run(config_path)
        self.assertEqual(result2.returncode, 0)
        self.assertEqual(result2.stdout, "")
        self.assertEqual(result2.stderr, "")
        with open(readings_file, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 1)

    def test_record_does_not_read_stdin(self):
        """Same control as --report's stdin test: an OPEN pipe never written
        to, so a blocking `sys.stdin.read()` fails on TimeoutExpired rather
        than racing an instant EOF (DEVNULL, or an inherited closed pipe)."""
        self.root.write("CLAUDE.md", "- item\n")
        readings_file = os.path.join(self.home.path, "readings.jsonl")
        config_path = self._write_config(
            workspace_root=self.root.path, readings_path=readings_file
        )
        env = _hermetic_env(HOME=self.home.path, CLAUDE_TOOLKIT_CONFIG=config_path)
        out_path = os.path.join(self.root.path, "out.txt")
        with open(out_path, "w") as out_fh:
            proc = subprocess.Popen(
                [sys.executable, EXE, "--record"],
                stdin=subprocess.PIPE,
                stdout=out_fh,
                stderr=subprocess.STDOUT,
                env=env,
            )
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                self.fail("claude-context-budget --record blocked on stdin")
            finally:
                proc.stdin.close()
        self.assertEqual(proc.returncode, 0)
        with open(out_path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "")


class TestRecordShortCircuit(unittest.TestCase):
    """Acceptance 7: the today's-line check runs BEFORE any measurement.

    In-process, per the plan: load the executable as a module, wrap
    `budget_lib.measure_instructions` to count calls, call `main()` twice for
    `--record` on the same day, and assert the wrapper ran exactly once.
    """

    def setUp(self):
        self.root = Tree()
        self.addCleanup(self.root.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)
        self.root.write("CLAUDE.md", "- item\n")

    def test_measure_instructions_runs_exactly_once_across_two_records(self):
        readings_file = os.path.join(self.home.path, "readings.jsonl")
        root_path = self.root.path
        real_get_path = toolkit_config.get_path

        def fake_get_path(*keys, **kwargs):
            if keys == ("context_budget", "readings_path"):
                return readings_file
            if keys == ("context_budget", "memory_dir"):
                return kwargs.get("default")
            return real_get_path(*keys, **kwargs)

        module = load_cli_module()
        calls = []
        real_measure = budget_lib.measure_instructions

        def counting_measure(*args, **kwargs):
            calls.append(1)
            return real_measure(*args, **kwargs)

        old_argv = sys.argv
        with mock.patch.object(
            toolkit_config, "workspace_root", new=lambda: root_path
        ), mock.patch.object(
            toolkit_config, "get_path", new=fake_get_path
        ), mock.patch.object(
            budget_lib, "measure_instructions", new=counting_measure
        ), mock.patch.dict(os.environ, {"HOME": self.home.path}):
            # mock.patch.dict only ADDS/overrides HOME -- an ambient
            # CLAUDE_CONFIG_DIR stays in os.environ otherwise and wins over
            # the sandboxed HOME inside claude_home.claude_home() (Code
            # Review FIX 3). The pop is restored on __exit__ along with
            # everything else this context manager snapshotted.
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            sys.argv = [EXE, "--record"]
            try:
                module.main()
                module.main()
            finally:
                sys.argv = old_argv

        self.assertEqual(len(calls), 1)
        with open(readings_file, encoding="utf-8") as fh:
            self.assertEqual(len(fh.readlines()), 1)


class TestInstructionsSectionThroughReport(unittest.TestCase):
    """`--report`'s instruction-meter section (design 4.7), driven by a real,
    test-seeded reading log."""

    def setUp(self):
        self.root = Tree()
        self.addCleanup(self.root.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)
        self.root.write("CLAUDE.md", "- item one\n- item two\n- item three\n")
        self.root_real = os.path.realpath(self.root.path)
        self.claude_md_key = os.path.join(self.root_real, "CLAUDE.md")
        self.readings_file = os.path.join(self.home.path, "readings.jsonl")
        self.config_path = self.home.write(
            "config.json",
            json.dumps({"context_budget": {"readings_path": self.readings_file}}),
        )

    def _seed(self, entries):
        with open(self.readings_file, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")

    def _reading(self, date_str, instructions, value):
        return {
            "v": instruction_lib.UNIT_VERSION,
            "date": date_str,
            "ts": date_str + "T00:00:00",
            "workspace_root": self.root_real,
            "surfaces": [
                {
                    "key": self.claude_md_key,
                    "kind": "claude_md",
                    "class": "always-on",
                    "ok": True,
                    "note": None,
                    "partial": False,
                    "instructions": instructions,
                    "unit": "lines",
                    "value": value,
                    "cap": 800,
                }
            ],
            "always_on_total": instructions,
        }

    def _run(self):
        env = _hermetic_env(HOME=self.home.path, CLAUDE_TOOLKIT_CONFIG=self.config_path)
        return subprocess.run(
            [sys.executable, EXE, "--report", self.root.path],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )

    def test_growing_readings_show_a_plus_rate_and_a_days_forecast(self):
        """FIX 2: the forecast must come from THIS run's LIVE value against
        the live cap, never from whatever the log's newest reading recorded
        -- a session between two `--record` ticks has moved on from the log's
        last snapshot. Rate: 100 -> 170 lines over 7 days = +70.0/wk. Live
        file: 190 lines, cap overridden to 200 -- (200 - 190) / (70.0 / 7) =
        1 day, exactly the design's own probe
        (`readings_lib.forecast(70.0, 190, 200) == 1.0`). Had the code used
        the LOGGED latest value (170) instead of the live one, it would read
        (200 - 170) / 10 = 3 days -- a distinct, wrong number this test can
        tell apart from the right one.
        """
        import datetime

        # Override the whole-file cap to a small, exact number so the
        # forecast's arithmetic is checkable by hand rather than depending on
        # whatever DEFAULT_MAX_LINES happens to be.
        claude_md_real = os.path.realpath(self.claude_md_key)
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "context_budget": {"readings_path": self.readings_file},
                        "context_growth": {"overrides": {claude_md_real: 200}},
                    }
                )
            )

        # 190 list items, one per line: count_markdown counts each as 1, and
        # the line count is the same 190 -- instructions and value agree, so
        # neither hides a units mismatch.
        self.root.write("CLAUDE.md", "".join("- item %d\n" % i for i in range(190)))

        today = datetime.date.today()
        day1 = (today - datetime.timedelta(days=7)).isoformat()
        day2 = today.isoformat()
        self._seed([self._reading(day1, 100, 100), self._reading(day2, 170, 170)])

        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "always-on instructions — unit v2: sentences (a proxy)",
            result.stdout,
        )
        lines = result.stdout.splitlines()
        row = next(
            line for line in lines if self.claude_md_key in line and "instr" in line
        )
        self.assertIn("+70.0/wk", row)
        self.assertIn("~1 days to cap", row)
        self.assertNotIn("~3 days to cap", row)

    def test_a_five_day_old_log_shows_recorder_stale(self):
        import datetime

        stale_date = (datetime.date.today() - datetime.timedelta(days=5)).isoformat()
        self._seed([self._reading(stale_date, 3, 3)])

        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RECORDER STALE", result.stdout)

    def test_no_log_at_all_shows_no_readings_yet(self):
        result = self._run()  # self.readings_file was never written
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no readings yet", result.stdout)

    def test_budget_line_forwards_config_through_the_executable(self):
        """`report()` must pass its own `config` through to
        `instructions_section` (M12's target). This fixture's tree has no
        MEMORY.md, so its live total is always `None` -- the branch this
        exercises is `ok and total is None`."""
        with open(self.config_path, "w", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "context_budget": {
                            "readings_path": self.readings_file,
                            "always_on_budget": 500,
                        }
                    }
                )
            )
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "budget  500 — total unavailable, not compared", result.stdout
        )

    def test_budget_line_prints_not_set_with_no_key_through_the_executable(self):
        result = self._run()  # self.config_path carries no always_on_budget
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "budget  not set — set context_budget.always_on_budget",
            result.stdout,
        )

    def test_anchor_line_is_present_through_the_executable(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertTrue(any(line.startswith("anchor  ") for line in lines))


class TestReportWithNoRoot(unittest.TestCase):
    def test_no_root_and_no_config_prints_the_no_root_line(self):
        home = Tree()
        self.addCleanup(home.cleanup)
        env = _hermetic_env(HOME=home.path, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        result = subprocess.run(
            [sys.executable, EXE, "--report"],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "always-on instructions: no workspace root — pass one or set "
            "workspace_root",
            result.stdout,
        )


class TestHookRegistration(unittest.TestCase):
    # Pinned: the repo's own plugin-continuity/hooks/hooks.json must actually
    # wire --record into SessionStart, or the instruction meter never runs --
    # a symptom install.sh's own payload-surface check would not catch, since
    # it only verifies the hooks key MERGES, not that this entry is IN it.
    # Modelled on tools/spec-archive/tests/test_cli.py:747's TestHookRegistration.

    def _record_hook_entries(self):
        return [
            row for row in hook_manifest.registrations(hook_manifest.load(REPO))
            if row["event"] == "SessionStart"
            and row["exe"] == "claude-context-budget"
            and row["args"] == ["--record"]
        ]

    def test_exactly_one_record_hook_is_registered_under_session_start(self):
        entries = self._record_hook_entries()
        self.assertEqual(len(entries), 1, entries)

    def test_its_command_points_into_its_own_tool_directory(self):
        entry = self._record_hook_entries()[0]
        self.assertEqual(
            "tools/context-budget/claude-context-budget", entry["relpath"]
        )

    def test_its_timeout_is_an_int_between_5_and_10(self):
        entry = self._record_hook_entries()[0]
        self.assertIsInstance(entry["timeout"], int)
        self.assertGreaterEqual(entry["timeout"], 5)
        self.assertLessEqual(entry["timeout"], 10)


def _git_available():
    try:
        subprocess.run(("git", "--version"), check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


class GitFixture(object):
    """A bare remote + one clone, for `checkout_behind`.

    HEAD is pinned to `main` on BOTH the seed and the bare remote -- the
    `init.defaultBranch` trap this repo's own CLAUDE.md documents (a bare
    remote otherwise leaks the ambient default through `clone`) -- and the
    clone's branch is asserted right here, at fixture build time, so a future
    leak fails where it happens rather than downstream in an assertion that
    has nothing to do with git config.

    Every git call strips inherited `GIT_*` vars except the `GIT_CONFIG_*`
    triplet this whole suite may deliberately be run under (reproducing a
    `master`-default runner) -- a stray host `GIT_DIR` must never leak into a
    fixture repo (repo CLAUDE.md, "A subprocess test inherits the SURFACE it
    runs on").
    """

    _KEEP_GIT_VARS = {"GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"}

    def __init__(self):
        self._tmp = tempfile.mkdtemp(prefix="ctxbudget-git-")
        self.seed = os.path.join(self._tmp, "seed")
        self.remote = os.path.join(self._tmp, "remote.git")
        self.clone = os.path.join(self._tmp, "clone")

        os.makedirs(self.seed)
        self._git(self.seed, "init", "-q")
        # Before the first commit: redirects the unborn HEAD to refs/heads/main
        # regardless of an ambient init.defaultBranch=master.
        self._git(self.seed, "symbolic-ref", "HEAD", "refs/heads/main")
        self._git(self.seed, "config", "user.email", "t@example.com")
        self._git(self.seed, "config", "user.name", "T")
        self._write(self.seed, "CLAUDE.md", "seed\n")
        self._git(self.seed, "add", "CLAUDE.md")
        self._git(self.seed, "commit", "-q", "-m", "first")

        self._run("git", "clone", "-q", "--bare", self.seed, self.remote)
        # Belt and braces per the CLAUDE.md trap note: pin the bare remote's
        # own HEAD explicitly too, not only inherited from the seed clone.
        self._git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")

        self._run("git", "clone", "-q", self.remote, self.clone)
        self._git(self.clone, "config", "user.email", "t@example.com")
        self._git(self.clone, "config", "user.name", "T")

        branch = self._output(self.clone, "rev-parse", "--abbrev-ref", "HEAD")
        assert branch == "main", (
            "fixture clone landed on %r, not main -- the init.defaultBranch "
            "pin did not take" % branch
        )

    def _env(self):
        return {k: v for k, v in os.environ.items()
                if not k.startswith("GIT_") or k in self._KEEP_GIT_VARS}

    def _run(self, *argv):
        subprocess.run(argv, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env=self._env())

    def _git(self, repo, *args):
        self._run("git", "-C", repo, *args)

    def _output(self, repo, *args):
        result = subprocess.run(
            ("git", "-C", repo) + args, check=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self._env(),
        )
        return result.stdout.decode("utf-8").strip()

    def _write(self, repo, name, text):
        with open(os.path.join(repo, name), "w", encoding="utf-8") as handle:
            handle.write(text)

    def push_commits(self, n, prefix="extra"):
        """Push `n` more commits to the remote from a THROWAWAY clone, never
        from `self.clone` -- the checkout under test must not see them until
        it explicitly fetches."""
        pusher = os.path.join(self._tmp, "pusher-%s-%d" % (prefix, n))
        self._run("git", "clone", "-q", self.remote, pusher)
        self._git(pusher, "config", "user.email", "t@example.com")
        self._git(pusher, "config", "user.name", "T")
        for i in range(n):
            name = "%s-%d.txt" % (prefix, i)
            self._write(pusher, name, name)
            self._git(pusher, "add", name)
            self._git(pusher, "commit", "-q", "-m", name)
        self._git(pusher, "push", "-q", "origin", "main")

    def fetch(self):
        self._git(self.clone, "fetch", "-q", "origin")

    def ff_to_origin(self):
        self._git(self.clone, "merge", "-q", "--ff-only", "origin/main")

    def head_sha(self, repo=None):
        return self._output(repo or self.clone, "rev-parse", "HEAD")

    def claude_md(self):
        return os.path.join(self.clone, "CLAUDE.md")

    def add_worktree(self, rel_path, branch, start_point):
        path = os.path.join(self.clone, rel_path)
        self._git(self.clone, "worktree", "add", "-q", "-b", branch, path,
                  start_point)
        return path

    def cleanup(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestCheckoutBehind(unittest.TestCase):
    """`checkout_behind` resolves the ACTUAL git checkout
    containing a CLAUDE.md (worktrees included) and says how far it is
    behind `origin/<branch>`, no network, silent when unknown."""

    def setUp(self):
        if not _git_available():
            self.skipTest("git not available")
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)

    def test_ac3_1_two_commits_behind_after_fetch(self):
        self.fixture.push_commits(2)
        self.fixture.fetch()
        self.assertEqual(
            budget_lib.checkout_behind(self.fixture.claude_md()), (2, "main")
        )

    def test_ac3_1_scan_and_report_carry_the_note(self):
        self.fixture.push_commits(2)
        self.fixture.fetch()
        surfaces = budget_lib.scan(self.fixture.clone, None, [])
        rows = [s for s in surfaces if s["name"] == "CLAUDE.md"]
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(
            rows[0]["note"],
            "local checkout 2 behind origin/main (as of last fetch)",
        )
        text = budget_lib.report(self.fixture.clone, None, [])
        self.assertIn(
            "local checkout 2 behind origin/main (as of last fetch)", text
        )

    def test_ac3_2_not_fetched_means_no_note(self):
        self.fixture.push_commits(2)
        # Deliberately no fetch(): the count is 0 as of the last fetch.
        self.assertIsNone(budget_lib.checkout_behind(self.fixture.claude_md()))

    def test_ac3_3_plain_directory_no_git(self):
        t = Tree()
        self.addCleanup(t.cleanup)
        path = t.write("CLAUDE.md", "x\n")
        self.assertIsNone(budget_lib.checkout_behind(path))

    def test_ac3_4_commits_behind_unknown_means_no_note(self):
        import freshness_lib

        self.fixture.push_commits(2)
        self.fixture.fetch()
        with mock.patch("freshness_lib.commits_behind", return_value=None):
            result = budget_lib.checkout_behind(self.fixture.claude_md())
        self.assertIsNone(result)

    def test_ac3_4b_linked_worktree_resolves_its_own_checkout(self):
        # Primary: commit1 (seed) -> fetch commit2 -> fast-forward -> current.
        self.fixture.push_commits(1, prefix="second")
        self.fixture.fetch()
        self.fixture.ff_to_origin()
        commit2 = self.fixture.head_sha()

        # origin/main advances to commit3; the primary does not fast-forward
        # yet, but the fetch already moved the shared refs/remotes/origin/main.
        self.fixture.push_commits(1, prefix="third")
        self.fixture.fetch()

        # A linked worktree nested under the primary, on a NEW branch still at
        # commit2 -- one commit behind the now-fetched origin/main.
        worktree = self.fixture.add_worktree(
            os.path.join(".claude", "worktrees", "w"), "w", commit2
        )

        # Fast-forward the primary itself, so IT reads current.
        self.fixture.ff_to_origin()

        self.assertIsNone(
            budget_lib.checkout_behind(self.fixture.claude_md())
        )
        self.assertEqual(
            budget_lib.checkout_behind(os.path.join(worktree, "CLAUDE.md")),
            (1, "main"),
        )

    def test_ambient_git_dir_pointed_at_a_different_repo_does_not_leak(self):
        """Merge Review FIX 1 -- an ambient `GIT_DIR` naming an UNRELATED
        repo must never override `git -C <dir>`: without a sanitized env,
        every git call this function makes (and every one `freshness_lib`
        makes on its behalf) would silently answer about the second repo
        instead of the checkout containing the CLAUDE.md under test."""
        self.fixture.push_commits(2)
        self.fixture.fetch()

        other = GitFixture()
        self.addCleanup(other.cleanup)

        prior = os.environ.get("GIT_DIR")
        os.environ["GIT_DIR"] = os.path.join(other.clone, ".git")
        try:
            result = budget_lib.checkout_behind(self.fixture.claude_md())
        finally:
            if prior is None:
                os.environ.pop("GIT_DIR", None)
            else:
                os.environ["GIT_DIR"] = prior
        self.assertEqual(result, (2, "main"))


class TestExecutableMarksBehindCheckout(unittest.TestCase):
    """AC3.4c -- through the executable, sandboxed. This is the test that
    fails if `tools/repo-freshness` is not actually wired onto budget_lib's
    own sys.path (M7's target)."""

    def setUp(self):
        if not _git_available():
            self.skipTest("git not available")
        self.fixture = GitFixture()
        self.addCleanup(self.fixture.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)
        self.cfg_dir = Tree()
        self.addCleanup(self.cfg_dir.cleanup)

    def test_ac3_4c_report_prints_the_behind_note(self):
        self.fixture.push_commits(2)
        self.fixture.fetch()
        cfg_path = self.cfg_dir.write(
            "config.json",
            json.dumps({"workspace_root": self.fixture._tmp}),
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.update(HOME=self.home.path, CLAUDE_TOOLKIT_CONFIG=cfg_path)
        result = subprocess.run(
            [sys.executable, EXE, "--report"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("local checkout 2 behind origin/main", result.stdout)


class TestImportsAreFollowed(unittest.TestCase):
    """An `@path` import in an always-on CLAUDE.md is followed, so the
    imported file's instructions count as always-on. Trees are built in temp
    dirs with an explicit `home` and a nonexistent `managed_path`; the real
    `~/.claude` is never read."""

    TEXT = (
        "- Always answer in plain language.\n"
        "- Never guess at numbers.\n"
        "- Lead with the answer.\n"
        "- Cut before adding.\n"
    )

    def setUp(self):
        self.ws = Tree()
        self.addCleanup(self.ws.cleanup)
        self.home = Tree()
        self.addCleanup(self.home.cleanup)
        self.managed = os.path.join(self.home.path, "no-managed.md")
        # A resolved memory dir, so the always-on total is a number, not None.
        self.mem = os.path.join(self.home.path, "memory")
        os.makedirs(self.mem)
        with open(os.path.join(self.mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("- [A](a.md) — hook one\n")

    def _records(self):
        return budget_lib.measure_instructions(
            self.ws.path, self.mem, None, None, self.home.path,
            managed_path=self.managed,
        )

    def _by_real(self, records):
        return {
            os.path.realpath(r["key"]): r
            for r in records
            if r["kind"] == "claude_md"
        }

    def _user_import(self, target):
        return self.home.write(".claude/CLAUDE.md", "@%s\n" % target)

    def test_workspace_text_and_imported_text_count_the_same(self):
        target_a = self.home.write("policy-a.md", "")
        self._user_import(target_a)
        self.ws.write("CLAUDE.md", self.TEXT)
        total_a = budget_lib.always_on_total(self._records())

        target_b = self.home.write("policy-b.md", self.TEXT)
        self._user_import(target_b)
        self.ws.write("CLAUDE.md", "")
        total_b = budget_lib.always_on_total(self._records())

        self.home.write(".claude/CLAUDE.md", "")
        total_b_without_import = budget_lib.always_on_total(self._records())

        self.assertIsNotNone(total_a)
        self.assertEqual(total_a, total_b)
        self.assertGreater(total_b, total_b_without_import)

    def test_target_is_an_always_on_record_and_importer_is_not_partial(self):
        target = self.home.write("policy.md", self.TEXT)
        user_md = self._user_import(target)
        by_real = self._by_real(self._records())
        self.assertEqual(by_real[os.path.realpath(target)]["class"], "always-on")
        self.assertIs(by_real[os.path.realpath(user_md)]["partial"], False)
        self.assertIs(by_real[os.path.realpath(target)]["partial"], False)

    def test_dangling_import_is_partial_and_adds_no_target_record(self):
        missing = os.path.join(self.home.path, "gone.md")
        user_md = self._user_import(missing)
        records = self._records()
        by_real = self._by_real(records)
        self.assertIs(by_real[os.path.realpath(user_md)]["partial"], True)
        self.assertNotIn(os.path.realpath(missing), by_real)
        self.assertEqual(
            [r for r in records if r["kind"] == "claude_md"],
            [by_real[os.path.realpath(user_md)]],
        )

    def test_conditional_claude_md_import_stays_partial(self):
        """Conditional records are not traversed: an import there is not
        followed, so the record keeps `partial` (even one that would resolve
        is not followed -- `@missing.md` here dangles either way)."""
        sub = self.ws.write("sub/CLAUDE.md", "@missing.md\n")
        self.ws.write("CLAUDE.md", self.TEXT)
        by_real = self._by_real(self._records())
        rec = by_real[os.path.realpath(sub)]
        self.assertEqual(rec["class"], "conditional")
        self.assertIs(rec["partial"], True)

    def test_conditional_import_that_resolves_is_still_partial(self):
        self.ws.write("sub/real.md", self.TEXT)
        sub = self.ws.write("sub/CLAUDE.md", "@real.md\n")
        by_real = self._by_real(self._records())
        self.assertIs(by_real[os.path.realpath(sub)]["partial"], True)

    def test_a_chain_of_five_imports_counts_hops_one_to_four(self):
        files = [self.home.write("c%d.md" % i, "- item %d\n" % i) for i in range(6)]
        # c0 imports c1 ... c4 imports c5; the user file imports c0 is hop 1.
        for i in range(5):
            self.home.write("c%d.md" % i, "- item %d\n@%s\n" % (i, files[i + 1]))
        user_md = self._user_import(files[0])
        records = self._records()
        by_real = self._by_real(records)
        # hops: c0=1, c1=2, c2=3, c3=4; c4 (hop 5) is not counted.
        for i in range(4):
            self.assertIn(os.path.realpath(files[i]), by_real, "hop %d" % (i + 1))
        self.assertNotIn(os.path.realpath(files[4]), by_real)
        self.assertNotIn(os.path.realpath(files[5]), by_real)
        # c3 sits at hop 4 with an import of its own: partial.
        self.assertIs(by_real[os.path.realpath(files[3])]["partial"], True)
        self.assertIs(by_real[os.path.realpath(files[2])]["partial"], False)
        self.assertIs(by_real[os.path.realpath(user_md)]["partial"], False)

    def test_a_two_file_cycle_terminates_and_counts_each_file_once(self):
        a = os.path.join(self.home.path, "a.md")
        b = os.path.join(self.home.path, "b.md")
        self.home.write("a.md", "- a\n@%s\n" % b)
        self.home.write("b.md", "- b\n@%s\n" % a)
        self._user_import(a)
        records = self._records()
        keys = [os.path.realpath(r["key"]) for r in records if r["kind"] == "claude_md"]
        self.assertEqual(keys.count(os.path.realpath(a)), 1)
        self.assertEqual(keys.count(os.path.realpath(b)), 1)
        self.assertIsNotNone(budget_lib.always_on_total(records))

    def test_an_imported_file_inside_the_workspace_is_counted_once_always_on(self):
        # Named CLAUDE.md and below the workspace root -- the real shape
        # (<workspace_root>/repos/claude-code-toolkit/policy/CLAUDE.md) -- so the
        # conditional pass (claude_md_surfaces) DOES find it; only the
        # already_realpaths dedupe keeps it from being counted twice.
        inside = self.ws.write("repos/kit/policy/CLAUDE.md", self.TEXT)
        self.ws.write("CLAUDE.md", "@repos/kit/policy/CLAUDE.md\n")
        records = self._records()
        matches = [
            r for r in records
            if os.path.realpath(r["key"]) == os.path.realpath(inside)
        ]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["class"], "always-on")

    def test_always_on_candidates_contains_the_targets_realpath(self):
        target = self.home.write("policy.md", self.TEXT)
        self._user_import(target)
        found = budget_lib.always_on_candidates(
            self.ws.path, None, None, self.home.path, managed_path=self.managed
        )
        self.assertIn(os.path.realpath(target), found)

    def test_an_imported_rules_file_is_counted_once(self):
        shared = self.home.write(".claude/rules/shared.md", self.TEXT)
        self._user_import(shared)
        records = self._records()
        matches = [r for r in records
                   if os.path.realpath(r["key"]) == os.path.realpath(shared)]
        self.assertEqual(len(matches), 1)
        single = budget_lib.always_on_total(records)
        self.assertEqual(matches[0]["class"], "always-on")
        self.assertIsNotNone(single)
        counted = sum(r["value"] for r in records
                      if r["class"] == "always-on" and r["ok"]
                      and os.path.realpath(r["key"]) == os.path.realpath(shared))
        self.assertEqual(counted, matches[0]["value"])

    def test_an_import_inside_a_fence_is_not_followed(self):
        target = self.home.write("policy.md", self.TEXT)
        self.home.write(".claude/CLAUDE.md", "```\n@%s\n```\n" % target)
        records = self._records()
        by_real = self._by_real(records)
        self.assertNotIn(os.path.realpath(target), by_real)

    def test_imported_files_returns_targets_and_unresolved(self):
        target = self.home.write("policy.md", self.TEXT)
        user_md = self._user_import(target)
        targets, unresolved = budget_lib.imported_files([user_md], self.home.path)
        self.assertEqual([os.path.realpath(t) for t in targets],
                         [os.path.realpath(target)])
        self.assertEqual(unresolved, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
