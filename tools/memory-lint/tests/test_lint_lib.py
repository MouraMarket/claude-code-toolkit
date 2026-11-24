"""Tests for the memory-index drift notice.

Like `memory-routing` next door, the interesting property is PRECISION: a notice
that fires on a healthy index trains the reader to skip it, so the silence cases
below carry more weight than the firing ones.

Hermetic: every case builds its own memory directory in a temp dir, and the
config path is pinned below so nothing reads this machine's real memory, roster
or config. The suite passes on a fresh clone with no config at all.
"""

import atexit
import datetime
import json
import os
import pty
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

# Pin the config BEFORE lint_lib can import toolkit_config, whose CONFIG_PATH is
# resolved once at import time from this variable.
#
# This is load-bearing, not tidiness. `lint_lib._config()` imports the real
# toolkit_config, so passing `config=None` means "read whatever is installed on
# this machine" — NOT "no config". Without this line the suite passed only
# because the config on the machine that wrote it happened to carry no
# `memory_lint` section: a user who takes the `enabled: false` opt-out that
# config.example.json documents got failures and errors, and `install.sh` runs
# this suite and exits 1, so following the documentation broke the install.
# The same sentence is already written at
# tools/usage-watch/tests/test_estimate_lib.py:701 for the same reason.
os.environ["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent-memory-lint-test.json"

# `build()` stays silent when auto memory is switched off, and every subprocess
# here inherits this process's environment through `_env()`. Run under a
# `claude -p` child that sets the switch, every "the notice fires" case would go
# quiet without this line. Tests that want the switch set pass it explicitly.
os.environ.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)

sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO, "lib"))
import lint_lib

HOOK = os.path.join(TOOL_DIR, "claude-memory-lint")

# An empty sandbox HOME for every subprocess in this file, unless a test
# builds its own fixture home. `dict(os.environ, ...)` otherwise hands a
# subprocess this machine's REAL HOME, and since commit 8a73b75 lint_lib
# resolves the user-level `autoMemoryDirectory` from `<HOME>/.claude/
# settings.json` first — so once install.sh writes that setting, every
# hook-mode test here silently follows it to this machine's real memory
# folder instead of the transcript-derived one it means to test.
_SANDBOX_HOME = tempfile.mkdtemp(prefix="memlint-sandbox-home-")
atexit.register(shutil.rmtree, _SANDBOX_HOME, ignore_errors=True)
# In-process too: `build()` reads `<HOME>/.claude/settings.json` for
# `autoMemoryEnabled`, so a machine whose real settings turn auto memory off
# would silence every in-process "the notice fires" case.
os.environ["HOME"] = _SANDBOX_HOME
# Row 104: `build()` also reads project and local settings, from the payload's
# `cwd`, else `CLAUDE_PROJECT_DIR`, else the process cwd (this repo). Pin the
# fallback to an empty, git-free sandbox and drop `CLAUDE_CONFIG_DIR`, so the
# session running these tests cannot reach in with its own settings.
_SANDBOX_PROJECT = tempfile.mkdtemp(prefix="memlint-sandbox-project-")
atexit.register(shutil.rmtree, _SANDBOX_PROJECT, ignore_errors=True)
os.environ["CLAUDE_PROJECT_DIR"] = _SANDBOX_PROJECT
os.environ.pop("CLAUDE_CONFIG_DIR", None)


def _env(**overrides):
    """A subprocess env with HOME sandboxed to a settings-free temp dir."""
    return dict(os.environ, HOME=_SANDBOX_HOME, **overrides)


def row(name, hook):
    return "- [Title](%s) — %s" % (name, hook)


class MemoryDir(object):
    """A throwaway memory directory. Files default to trivial valid bodies."""

    def __init__(self, rows, files=None, bodies=None):
        self.path = tempfile.mkdtemp(prefix="memlint-")
        names = files if files is not None else [name for name, _ in rows]
        for name in names:
            body = (bodies or {}).get(name, "---\nname: x\n---\n\nbody\n")
            with open(os.path.join(self.path, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        with open(os.path.join(self.path, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(row(n, h) for n, h in rows) + "\n")

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)


class TestParsing(unittest.TestCase):
    def test_parses_rows_and_ignores_prose(self):
        text = (
            "Some heading prose that is not a row\n"
            "\n"
            + row("a.md", "short hook")
            + "\n"
            + row("b.md", "another hook")
            + "\n"
            "- a bare bullet that is not an index row\n"
        )
        rows = lint_lib.parse_index(text)
        self.assertEqual([r["file"] for r in rows], ["a.md", "b.md"])
        self.assertEqual(rows[0]["hook"], "short hook")

    def test_row_requires_an_em_dash_separator(self):
        """A hyphen separator must not parse — it would swallow the next bullet."""
        self.assertEqual(lint_lib.parse_index("- [T](a.md) - hook"), [])


class TestSilenceOnHealthy(unittest.TestCase):
    """The load-bearing half: a healthy index must produce nothing at all."""

    def test_healthy_index_is_silent(self):
        d = MemoryDir([("a.md", "x" * 120), ("b.md", "y" * 140)])
        self.addCleanup(d.cleanup)
        self.assertIsNone(lint_lib.build(d.path))

    def test_hook_exactly_at_the_limit_is_silent(self):
        """The per-hook threshold is exclusive: 200 is fine, 201 is not.

        Padded with short hooks deliberately. A lone 200-char hook has a MEAN of
        200 and would trip the separate mean rule, so testing the boundary in
        isolation requires holding the mean down — otherwise this asserts the
        wrong mechanism and passes for the wrong reason.
        """
        limit = lint_lib.DEFAULT_MAX_HOOK_CHARS
        pad = [("p%d.md" % i, "x" * 100) for i in range(20)]

        at_limit = MemoryDir(pad + [("a.md", "x" * limit)])
        self.addCleanup(at_limit.cleanup)
        self.assertEqual(lint_lib.scan(at_limit.path)["long_hooks"], [])
        self.assertIsNone(lint_lib.build(at_limit.path))

        over = MemoryDir(pad + [("a.md", "x" * (limit + 1))])
        self.addCleanup(over.cleanup)
        self.assertEqual(
            [f for f, _ in lint_lib.scan(over.path)["long_hooks"]], ["a.md"]
        )
        self.assertIsNotNone(lint_lib.build(over.path))

    def test_a_big_but_tidy_index_is_silent_about_HOOK_drift(self):
        """Scale-free, for the thing that is scale-free: hook length.

        A corpus of short hooks has no hook drift however many rows it has, so
        no hook finding fires here at any size. It is kept under the index
        budget so that the only property under test is the hook one.
        """
        d = MemoryDir([("m%03d.md" % i, "x" * 130) for i in range(100)])
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertLess(scan["index_chars"], lint_lib.DEFAULT_MAX_INDEX_CHARS)
        self.assertIsNone(lint_lib.build(d.path))

    def test_a_big_tidy_index_STILL_reports_the_whole_index_budget(self):
        """The half of the old test that the measurement falsified.

        This test used to assert that 300 short hooks stay silent at >40,000
        bytes, on the stated reasoning that "a flat index-size threshold would
        get this wrong, and it is the reason the tool measures hook length
        instead."

        That reasoning was written before the truncation was measured, and the
        harness imposes exactly the flat threshold it rejected: past ~25,000
        CHARACTERS the tail of the index stops arriving, silently. A 300-row
        index of 130-char hooks is ~46,000 chars, so more than a third of it
        never reaches a session. Calling that healthy is the alarm this whole
        mission exists to stop deleting.

        Tidy hooks and an over-budget index are independent: this asserts the
        budget fires while no hook finding does.

        N3b: 300 rows is ALSO 300 lines -- genuinely past
        INDEX_TRUNCATION_LINES as well as INDEX_TRUNCATION_CHARS, so the new
        lines finding now fires here too, correctly (both truncations are
        real on this fixture). Updated from asserting one issue to two, and
        pinned that the chars finding still comes LAST (AC1.6).
        """
        d = MemoryDir([("m%03d.md" % i, "x" * 130) for i in range(300)])
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertGreater(scan["index_chars"], lint_lib.INDEX_TRUNCATION_CHARS)
        self.assertGreater(scan["index_lines"], lint_lib.INDEX_TRUNCATION_LINES)
        self.assertIn("silently drops rows", lint_lib.build(d.path))
        # Assert on the FINDINGS, not the notice text: build() wraps them in
        # standing advice that itself mentions hooks, so a substring check over
        # the whole notice tests the boilerplate rather than this property.
        issues = lint_lib.findings(scan)
        self.assertEqual(len(issues), 2)
        self.assertIn("silently drops the END of the index", issues[0])
        self.assertIn("silently drops rows", issues[1])

    def test_index_chars_is_characters_not_bytes(self):
        """The unit the cap is compared against, pinned.

        Measuring bytes would put this threshold on the wrong side: a typical
        index carries multi-byte em dashes, so the byte count reads high. The separator the index format REQUIRES is
        itself the multi-byte character, so this gap is structural, not
        incidental.
        """
        d = MemoryDir([("m%03d.md" % i, "hook with an em dash — in it") for i in range(20)])
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertLess(scan["index_chars"], scan["index_bytes"])
        text = open(os.path.join(d.path, "MEMORY.md"), encoding="utf-8").read()
        self.assertEqual(scan["index_chars"], len(text))

    def test_missing_directory_is_silent_and_does_not_raise(self):
        self.assertIsNone(lint_lib.build("/nonexistent/memory"))
        self.assertIs(lint_lib.scan("/nonexistent/memory")["readable"], False)

    def test_directory_without_an_index_is_silent(self):
        path = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        with open(os.path.join(path, "orphan.md"), "w") as fh:
            fh.write("body")
        self.assertIs(lint_lib.scan(path)["readable"], False)
        self.assertIsNone(lint_lib.build(path))


class TestDriftDetection(unittest.TestCase):
    def test_reports_long_hooks_worst_first(self):
        d = MemoryDir([("a.md", "x" * 260), ("b.md", "y" * 400), ("c.md", "z" * 90)])
        self.addCleanup(d.cleanup)
        result = lint_lib.scan(d.path)
        self.assertEqual([f for f, _ in result["long_hooks"]], ["b.md", "a.md"])
        self.assertIn("b.md", lint_lib.build(d.path))

    def test_mean_fires_even_when_no_single_hook_does(self):
        """Uniform 190-char hooks: under the per-hook cap, over the mean cap."""
        d = MemoryDir([("m%d.md" % i, "x" * 190) for i in range(10)])
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["long_hooks"], [])
        notice = lint_lib.build(d.path)
        self.assertIsNotNone(notice)
        self.assertIn("mean hook", notice)

    def test_mean_threshold_boundary_is_exclusive(self):
        """170 is fine, 171 is not — the boundary, not just the middle.

        Held under the per-hook cap throughout so this asserts the mean rule and
        not its neighbour. Without this, flipping the mean comparison to `>=`
        changed behaviour and no test noticed.
        """
        limit = lint_lib.DEFAULT_MAX_MEAN_HOOK_CHARS
        at = MemoryDir([("m%d.md" % i, "x" * limit) for i in range(10)])
        self.addCleanup(at.cleanup)
        self.assertEqual(lint_lib.scan(at.path)["mean_hook_chars"], limit)
        self.assertIsNone(lint_lib.build(at.path))

        over = MemoryDir([("m%d.md" % i, "x" * (limit + 1)) for i in range(10)])
        self.addCleanup(over.cleanup)
        notice = lint_lib.build(over.path)
        self.assertIsNotNone(notice)
        self.assertIn("mean hook", notice)

    def test_orphan_file_is_reported(self):
        d = MemoryDir([("a.md", "hook")], files=["a.md", "unlisted.md"])
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["orphans"], ["unlisted.md"])
        self.assertIn("unlisted.md", lint_lib.build(d.path))

    def test_dead_link_is_reported(self):
        d = MemoryDir([("a.md", "hook"), ("gone.md", "hook")], files=["a.md"])
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["dead_links"], ["gone.md"])

    def test_a_row_naming_an_md_directory_is_a_dead_link(self):
        # Merge Review finding: a `*.md` DIRECTORY was inventoried as a leaf.
        d = MemoryDir([("a.md", "hook"), ("dir.md", "hook")], files=["a.md"])
        self.addCleanup(d.cleanup)
        os.mkdir(os.path.join(d.path, "dir.md"))
        self.assertEqual(lint_lib.scan(d.path)["dead_links"], ["dir.md"])

    def test_structural_faults_report_on_an_otherwise_tidy_index(self):
        """Orphans are defects, not drift, so short hooks must not mask them."""
        d = MemoryDir([("a.md", "tiny")], files=["a.md", "unlisted.md"])
        self.addCleanup(d.cleanup)
        self.assertIsNotNone(lint_lib.build(d.path))


class TestMalformedRows(unittest.TestCase):
    """A row that does not parse must never be reported as a missing row."""

    def _index(self, line, files):
        path = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        for name in files:
            with open(os.path.join(path, name), "w") as fh:
                fh.write("body")
        with open(os.path.join(path, "MEMORY.md"), "w") as fh:
            fh.write(line + "\n")
        return path

    def test_en_dash_row_is_malformed_not_an_orphan(self):
        path = self._index("- [Title](alpha.md) – hook prose", ["alpha.md"])
        result = lint_lib.scan(path)
        self.assertEqual(result["orphans"], [], "file is linked; it is not an orphan")
        self.assertEqual([f for f, _ in result["malformed_rows"]], ["alpha.md"])
        notice = lint_lib.build(path)
        self.assertIn("does not parse", notice)
        self.assertNotIn("invisible to every session", notice)

    def test_row_with_no_hook_is_malformed_not_an_orphan(self):
        path = self._index("- [Title](alpha.md)", ["alpha.md"])
        result = lint_lib.scan(path)
        self.assertEqual(result["orphans"], [])
        self.assertEqual([f for f, _ in result["malformed_rows"]], ["alpha.md"])

    def test_a_genuinely_unlisted_file_is_still_an_orphan(self):
        """The fix must not swallow the real case it sits next to."""
        path = self._index("- [Title](alpha.md) — hook", ["alpha.md", "beta.md"])
        result = lint_lib.scan(path)
        self.assertEqual(result["orphans"], ["beta.md"])
        self.assertEqual(result["malformed_rows"], [])

    def test_prose_is_not_treated_as_a_malformed_row(self):
        path = self._index("Some ordinary sentence about things.", [])
        self.assertEqual(lint_lib.scan(path)["malformed_rows"], [])
        self.assertIsNone(lint_lib.build(path))


class TestNoticeStaysBounded(unittest.TestCase):
    """The notice must never cost more per session than the drift it reports."""

    def test_wholesale_parse_failure_produces_a_bounded_notice(self):
        path = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        names = ["m%03d.md" % i for i in range(96)]
        for name in names:
            with open(os.path.join(path, name), "w") as fh:
                fh.write("body")
        with open(os.path.join(path, "MEMORY.md"), "w") as fh:
            # `*` bullets: every row loose-matches, none parses strictly.
            fh.write("\n".join("* [T](%s) — hook" % n for n in names) + "\n")

        notice = lint_lib.build(path)
        self.assertIsNotNone(notice)
        self.assertLess(
            len(notice.splitlines()), 10,
            "96 malformed rows must summarise, not enumerate",
        )
        self.assertLess(len(notice), 1000, "notice must not become the drift")
        self.assertIn("96", notice)
        self.assertIn("+93 more", notice)

    def test_many_orphans_summarise(self):
        path = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        for i in range(40):
            with open(os.path.join(path, "orphan%02d.md" % i), "w") as fh:
                fh.write("body")
        with open(os.path.join(path, "MEMORY.md"), "w") as fh:
            fh.write("")
        notice = lint_lib.build(path)
        self.assertIn("40 files have no row", notice)
        self.assertIn("+37 more", notice)

    def test_a_single_item_reads_naturally(self):
        """Summarising must not make the one-item case ugly or wrong."""
        d = MemoryDir([("a.md", "hook")], files=["a.md", "lonely.md"])
        self.addCleanup(d.cleanup)
        notice = lint_lib.build(d.path)
        self.assertIn("lonely.md has no row in MEMORY.md", notice)
        self.assertNotIn("more)", notice)


class TestWikilinks(unittest.TestCase):
    def test_broken_wikilink_is_reported(self):
        d = MemoryDir(
            [("a.md", "hook")],
            bodies={"a.md": "see [[does-not-exist]]\n"},
        )
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["broken_wikilinks"], [("a.md", "does-not-exist")])

    def test_resolving_wikilink_is_silent(self):
        d = MemoryDir(
            [("a.md", "hook"), ("b.md", "hook")],
            bodies={"a.md": "see [[b]]\n"},
        )
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["broken_wikilinks"], [])

    def test_bash_double_brackets_in_code_are_not_wikilinks(self):
        """A real false positive from a real memory corpus.

        A memory file can carry `[[ -L "$entry" ]]` inside a
        fenced example. Reading that as a wikilink reports a broken link that
        nobody can fix, which is exactly the noise that gets a notice ignored.
        """
        d = MemoryDir(
            [("a.md", "hook")],
            bodies={"a.md": 'text\n\n```bash\nif [[ -L "$entry" ]]; then :; fi\n```\n'},
        )
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["broken_wikilinks"], [])

    def test_inline_code_double_brackets_are_not_wikilinks(self):
        d = MemoryDir([("a.md", "hook")], bodies={"a.md": "use `[[ -f x ]]` here\n"})
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["broken_wikilinks"], [])


class TestTranscriptResolution(unittest.TestCase):
    """`memory_dir_from_transcript`.

    The user-level `autoMemoryDirectory` setting (see `lib/auto_memory.py`)
    wins whenever it resolves to a real directory, even over a transcript
    whose own `memory/` folder exists. `home` is always a FIXTURE here so no
    case reads this machine's real `~/.claude/settings.json`.
    """

    def _fixture_home(self):
        """An empty home: no `.claude/settings.json` at all."""
        home = tempfile.mkdtemp(prefix="memlint-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        return home

    def _settings_home(self, auto_memory_directory):
        """A home whose `.claude/settings.json` sets `autoMemoryDirectory`."""
        home = tempfile.mkdtemp(prefix="memlint-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        os.makedirs(os.path.join(home, ".claude"))
        with open(os.path.join(home, ".claude", "settings.json"), "w") as fh:
            json.dump({"autoMemoryDirectory": auto_memory_directory}, fh)
        return home

    def test_resolves_sibling_memory_dir(self):
        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        os.mkdir(os.path.join(project, "memory"))
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()
        # realpath, not the raw path: resolution is symlink-resolving by design
        # (on macOS /var is a symlink to /private/var), and that is the same
        # rule the executables follow for their own __file__ lookups.
        self.assertEqual(
            lint_lib.memory_dir_from_transcript(
                transcript, home=self._fixture_home()
            ),
            os.path.join(os.path.realpath(project), "memory"),
        )

    def test_absent_memory_dir_resolves_to_none(self):
        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()
        self.assertIsNone(
            lint_lib.memory_dir_from_transcript(
                transcript, home=self._fixture_home()
            )
        )

    def test_missing_transcript_path_resolves_to_none(self):
        home = self._fixture_home()
        self.assertIsNone(lint_lib.memory_dir_from_transcript(None, home=home))
        self.assertIsNone(lint_lib.memory_dir_from_transcript("", home=home))

    def test_no_settings_file_falls_back_to_the_transcript_folder(self):
        """No settings file at all -- existing behaviour, unaffected by N5."""
        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        os.mkdir(os.path.join(project, "memory"))
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()
        home = self._fixture_home()
        self.assertFalse(
            os.path.exists(os.path.join(home, ".claude", "settings.json"))
        )
        self.assertEqual(
            lint_lib.memory_dir_from_transcript(transcript, home=home),
            os.path.join(os.path.realpath(project), "memory"),
        )

    def test_a_corrupt_settings_file_falls_back_like_an_absent_one(self):
        """A settings.json that is not valid JSON is read as "not configured".

        That matches the harness. MEASURED on Claude Code 2.1.280 with an
        isolated CLAUDE_CONFIG_DIR: a valid user `autoMemoryDirectory` made
        the session create that folder and no `projects/<slug>/memory`, while
        the same key in a truncated, invalid file left the setting unapplied
        and the session created the default `projects/<slug>/memory`. So the
        transcript's own folder is the one the harness is really using, and
        linting it is the right answer. Surfacing the corrupt file instead
        would point at a folder no session writes to.
        """
        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        os.mkdir(os.path.join(project, "memory"))
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()
        home = self._fixture_home()
        os.makedirs(os.path.join(home, ".claude"))
        elsewhere = os.path.join(home, "elsewhere")
        os.mkdir(elsewhere)
        with open(os.path.join(home, ".claude", "settings.json"), "w") as fh:
            fh.write('{"autoMemoryDirectory": "%s", ' % elsewhere)
        self.assertEqual(
            lint_lib.memory_dir_from_transcript(transcript, home=home),
            os.path.join(os.path.realpath(project), "memory"),
        )
        # Control: the same value in a VALID file wins, so the fallback above
        # is about the corruption, not about the value.
        with open(os.path.join(home, ".claude", "settings.json"), "w") as fh:
            json.dump({"autoMemoryDirectory": elsewhere}, fh)
        self.assertEqual(
            os.path.realpath(lint_lib.memory_dir_from_transcript(transcript, home=home)),
            os.path.realpath(elsewhere),
        )

    def test_the_setting_wins_over_a_present_transcript_memory_dir(self):
        """The setting's target is returned even though the transcript's own
        `memory/` folder also exists -- N5's whole point."""
        setting_dir = tempfile.mkdtemp(prefix="memlint-setting-")
        self.addCleanup(shutil.rmtree, setting_dir, ignore_errors=True)
        with open(os.path.join(setting_dir, "MEMORY.md"), "w") as fh:
            fh.write("# setting memory\n")
        home = self._settings_home(setting_dir)

        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        os.mkdir(os.path.join(project, "memory"))
        with open(os.path.join(project, "memory", "MEMORY.md"), "w") as fh:
            fh.write("# transcript memory\n")
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()

        # `auto_memory.expand()` normalises but does NOT realpath, so the
        # setting's own spelling comes back unchanged -- unlike the
        # transcript-derived fallback below, which is symlink-resolving.
        self.assertEqual(
            lint_lib.memory_dir_from_transcript(transcript, home=home),
            setting_dir,
        )

    def test_the_setting_pointing_at_a_missing_dir_is_still_returned(self):
        """Merge Review finding 1: "configured but missing" must not read as
        "not configured". This replaces the earlier assertion that a missing
        setting target fell back to the transcript folder -- `install.sh`
        can write `autoMemoryDirectory` before the harness has ever created
        that folder's contents, and falling back there silently reported a
        DIFFERENT project's memory as healthy (see the executable case
        below). The configured path wins even though it does not exist."""
        missing = "/nonexistent/memlint-setting-dir"
        home = self._settings_home(missing)

        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        os.mkdir(os.path.join(project, "memory"))
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()

        self.assertEqual(
            lint_lib.memory_dir_from_transcript(transcript, home=home),
            missing,
        )

    def test_the_setting_wins_even_with_no_transcript_path(self):
        setting_dir = tempfile.mkdtemp(prefix="memlint-setting-")
        self.addCleanup(shutil.rmtree, setting_dir, ignore_errors=True)
        home = self._settings_home(setting_dir)
        self.assertEqual(
            lint_lib.memory_dir_from_transcript(None, home=home),
            setting_dir,
        )


class TestDefaultMemoryDirPrefersTheSetting(unittest.TestCase):
    """`claude-memory-lint --report` with no directory argument. The user-level setting must win over the "most recently
    written index wins" fallback, even when a DIFFERENT project's memory was
    written more recently.
    """

    def test_report_names_the_setting_not_the_newer_folder(self):
        home = tempfile.mkdtemp(prefix="memlint-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)

        # Folder A: the setting's target, holds a MEMORY.md.
        folder_a = os.path.join(home, ".claude", "projects", "-a", "memory")
        os.makedirs(folder_a)
        with open(os.path.join(folder_a, "x.md"), "w") as fh:
            fh.write("body")
        with open(os.path.join(folder_a, "MEMORY.md"), "w") as fh:
            fh.write(row("x.md", "hook") + "\n")

        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w") as fh:
            json.dump({"autoMemoryDirectory": folder_a}, fh)

        # A second project's memory, written NEWER than folder A.
        folder_b = os.path.join(home, ".claude", "projects", "-x", "memory")
        os.makedirs(folder_b)
        with open(os.path.join(folder_b, "y.md"), "w") as fh:
            fh.write("body")
        with open(os.path.join(folder_b, "MEMORY.md"), "w") as fh:
            fh.write(row("y.md", "hook") + "\n")
        os.utime(os.path.join(folder_a, "MEMORY.md"), (1000, 1000))
        os.utime(os.path.join(folder_b, "MEMORY.md"), (2000, 2000))

        proc = subprocess.run(
            [sys.executable, HOOK, "--report"],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
            env=dict(
                os.environ,
                HOME=home,
                CLAUDE_TOOLKIT_CONFIG=os.path.join(
                    home, "nonexistent-memlint-config.json"
                ),
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertIn(folder_a, proc.stdout)
        self.assertNotIn(folder_b, proc.stdout)

    def test_report_names_a_configured_but_missing_folder_not_healthy(self):
        """Merge Review finding 1 (ii): the setting points at a folder that
        does not exist yet, and a DIFFERENT, newer project memory does. The
        report must name the configured folder -- never call it healthy,
        since it holds no readable MEMORY.md at all -- and still exit 0."""
        home = tempfile.mkdtemp(prefix="memlint-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)

        missing_dir = os.path.join(home, ".claude", "projects", "-a", "memory")
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w") as fh:
            json.dump({"autoMemoryDirectory": missing_dir}, fh)
        self.assertFalse(os.path.isdir(missing_dir))

        # A second, unrelated project's memory -- newer than anything, and
        # exactly what the pre-fix fallback ("most recently written index
        # wins") would have reported instead.
        newer_dir = os.path.join(home, ".claude", "projects", "-x", "memory")
        os.makedirs(newer_dir)
        with open(os.path.join(newer_dir, "y.md"), "w") as fh:
            fh.write("body")
        with open(os.path.join(newer_dir, "MEMORY.md"), "w") as fh:
            fh.write(row("y.md", "hook") + "\n")

        proc = subprocess.run(
            [sys.executable, HOOK, "--report"],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
            env=dict(
                os.environ,
                HOME=home,
                CLAUDE_TOOLKIT_CONFIG=os.path.join(
                    home, "nonexistent-memlint-config.json"
                ),
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertIn(missing_dir, proc.stdout)
        self.assertNotIn("healthy", proc.stdout)
        self.assertNotIn(newer_dir, proc.stdout)

    def test_hook_mode_with_configured_folder_missing_is_silent(self):
        """Merge Review finding 1 (iii): hook mode must stay silent and exit
        0 even when the configured `autoMemoryDirectory` does not exist."""
        home = tempfile.mkdtemp(prefix="memlint-home-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)

        missing_dir = os.path.join(home, ".claude", "projects", "-a", "memory")
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        with open(os.path.join(home, ".claude", "settings.json"), "w") as fh:
            json.dump({"autoMemoryDirectory": missing_dir}, fh)
        self.assertFalse(os.path.isdir(missing_dir))

        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()

        proc = subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"transcript_path": transcript}),
            capture_output=True, text=True, timeout=15,
            env=dict(
                os.environ,
                HOME=home,
                CLAUDE_TOOLKIT_CONFIG=os.path.join(
                    home, "nonexistent-memlint-config.json"
                ),
            ),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertEqual(proc.stdout, "")


class TestExecutable(unittest.TestCase):
    """Through the executable, not the lib. A hook is what actually ships."""

    def _run(self, args, stdin_text, timeout=15):
        return subprocess.run(
            [sys.executable, HOOK] + args,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_env(CLAUDE_TOOLKIT_CONFIG="/nonexistent.json"),
        )

    def test_hook_is_silent_on_a_healthy_index(self):
        d = MemoryDir([("a.md", "x" * 120)])
        self.addCleanup(d.cleanup)
        transcript = os.path.join(os.path.dirname(d.path), "s.jsonl")
        # The memory dir must be named `memory` beside the transcript.
        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        shutil.copytree(d.path, os.path.join(project, "memory"))
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()

        proc = self._run([], json.dumps({"transcript_path": transcript}))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_hook_fires_with_sessionstart_shape_on_drift(self):
        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        mem = os.path.join(project, "memory")
        os.mkdir(mem)
        with open(os.path.join(mem, "a.md"), "w") as fh:
            fh.write("body")
        with open(os.path.join(mem, "MEMORY.md"), "w") as fh:
            fh.write(row("a.md", "x" * 400) + "\n")
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()

        proc = self._run([], json.dumps({"transcript_path": transcript}))
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "SessionStart"
        )
        self.assertIn("a.md", payload["hookSpecificOutput"]["additionalContext"])

    def test_runs_correctly_through_a_symlink(self):
        """The realpath rule, exercised rather than asserted.

        These tools ship as ~/.local/bin symlinks, and this repo's CLAUDE.md
        requires `realpath` over `abspath` for anything `__file__`-relative.

        The assertion has to be about CONFIG, and that is the whole subtlety.
        Running the symlink and checking it produces output does NOT catch
        `abspath` here: since Python 3.11 sys.path[0] is already symlink-resolved,
        so `import lint_lib` succeeds either way and the report looks perfect.
        What breaks is the SIBLING path — `dirname(dirname(here))/lib` computed
        from the link's directory misses `toolkit_config`, `_config()` swallows
        the ImportError by design, and the tool silently falls back to compiled
        defaults. The user's configured thresholds stop applying and nothing
        says so. Asserting a configured value survives the symlink is the only
        version of this test that fails when the rule is broken.
        """
        link_dir = tempfile.mkdtemp(prefix="memlint-bin-")
        self.addCleanup(shutil.rmtree, link_dir, ignore_errors=True)
        link = os.path.join(link_dir, "claude-memory-lint")
        os.symlink(HOOK, link)

        cfg = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, prefix="memlint-link-"
        )
        self.addCleanup(os.unlink, cfg.name)
        json.dump({"memory_lint": {"max_hook_chars": 417}}, cfg)
        cfg.close()

        d = MemoryDir([("a.md", "x" * 120)])
        self.addCleanup(d.cleanup)
        proc = subprocess.run(
            [sys.executable, link, "--report", d.path],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
            env=_env(CLAUDE_TOOLKIT_CONFIG=cfg.name),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        self.assertIn("hook chars", proc.stdout)
        self.assertIn(
            "notice at 417", proc.stdout,
            "config did not survive the symlink — the lib path is computed from "
            "the link instead of its target, so thresholds silently reverted",
        )

    def test_garbage_stdin_exits_zero_and_says_nothing(self):
        proc = self._run([], "not json at all")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_empty_stdin_exits_zero(self):
        proc = self._run([], "")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_report_ignores_stdin_content(self):
        """--report must not PARSE stdin: garbage there still yields a report."""
        d = MemoryDir([("a.md", "x" * 400)])
        self.addCleanup(d.cleanup)
        proc = self._run(["--report", d.path], "}{ not json")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("hook chars", proc.stdout)
        self.assertIn("a.md", proc.stdout)

    def test_report_does_not_BLOCK_on_stdin_from_a_real_tty(self):
        """--report must not READ stdin. Driven from a pty, because a pipe cannot show this.

        The obvious version of this test — feed garbage through
        `subprocess.run(input=...)` and check a report still comes out — passes
        even when the tool calls `sys.stdin.read()`, because that pipe hits EOF
        immediately. It therefore proves the tool does not *parse* stdin while
        saying nothing about whether it *blocks* on it, which is the failure that
        matters: a person running this by hand at a terminal gets a hang with no
        output and no way to know why.

        A pty with the slave closed and the master held open never sends EOF, so
        any read blocks. Verified against the mutation: with `sys.stdin.read()`
        inserted at the top of `run_report`, this times out; as shipped it
        returns immediately. The test above keeps the parse half honest.
        """
        d = MemoryDir([("a.md", "x" * 400)])
        self.addCleanup(d.cleanup)

        master, slave = pty.openpty()
        proc = subprocess.Popen(
            [sys.executable, HOOK, "--report", d.path],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_env(CLAUDE_TOOLKIT_CONFIG="/nonexistent.json"),
        )
        os.close(slave)  # the child holds the only other end; master stays open
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            self.fail(
                "--report blocked on a TTY stdin. It must never read stdin: "
                "running it by hand is the invocation this command exists for."
            )
        finally:
            os.close(master)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("hook chars", out)

    def test_report_on_a_healthy_index_says_so_explicitly(self):
        d = MemoryDir([("a.md", "x" * 120)])
        self.addCleanup(d.cleanup)
        proc = self._run(["--report", d.path], "")
        self.assertIn("healthy", proc.stdout)

    def test_report_on_a_missing_directory_does_not_crash(self):
        proc = self._run(["--report", "/nonexistent/memory"], "")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("No readable MEMORY.md", proc.stdout)


class TestConfig(unittest.TestCase):
    def test_absent_config_means_enabled(self):
        """A fresh clone has no config; the notice must still work.

        `config=None` makes lint_lib import the real toolkit_config, so this
        asserts the ABSENT-FILE branch only because the module docstring pins
        CLAUDE_TOOLKIT_CONFIG at a path that does not exist. Previously this
        named a branch its body never reached — it read whatever config the
        machine happened to have.
        """
        import toolkit_config

        self.assertFalse(
            os.path.exists(toolkit_config.CONFIG_PATH),
            "the pinned config path must not exist, or this tests nothing",
        )
        self.assertIs(lint_lib.is_enabled(config=None), True)

    def test_thresholds_fall_back_to_defaults_without_config(self):
        self.assertEqual(lint_lib.max_hook_chars(None), lint_lib.DEFAULT_MAX_HOOK_CHARS)
        self.assertEqual(
            lint_lib.max_mean_hook_chars(None), lint_lib.DEFAULT_MAX_MEAN_HOOK_CHARS
        )

    def test_explicit_disable_silences_a_drifted_index(self):
        class Off(object):
            def get(self, section, key, default=None):
                return False if (section, key) == ("memory_lint", "enabled") else default

            def get_int(self, section, key, default=None):
                return default

        d = MemoryDir([("a.md", "x" * 400)])
        self.addCleanup(d.cleanup)
        self.assertIsNotNone(lint_lib.build(d.path, config=None))
        self.assertIsNone(lint_lib.build(d.path, config=Off()))

    def test_a_real_config_FILE_is_honoured_end_to_end(self):
        """Pinning the path away must not leave the config plumbing untested.

        Drives the executable with an actual JSON file, so this covers the path
        a user exercises: toolkit_config resolving CONFIG_PATH at import,
        reading the file, and lint_lib applying the value.
        """
        d = MemoryDir([("a.md", "x" * 250)])
        self.addCleanup(d.cleanup)
        cfg = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, prefix="memlint-cfg-"
        )
        self.addCleanup(os.unlink, cfg.name)
        # BOTH thresholds, deliberately: raising only max_hook_chars leaves the
        # separate mean rule at its default, and a lone 250-char hook has a mean
        # of 250 — so the report would still show a finding and this would prove
        # nothing about whether the file was read.
        json.dump(
            {"memory_lint": {"max_hook_chars": 400, "max_mean_hook_chars": 380}}, cfg
        )
        cfg.close()

        proc = subprocess.run(
            [sys.executable, HOOK, "--report", d.path],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
            env=_env(CLAUDE_TOOLKIT_CONFIG=cfg.name),
        )
        self.assertIn("notice at 400", proc.stdout)
        self.assertIn("healthy", proc.stdout)

        # Negative control: the SAME index without the config file must report,
        # or the assertions above could be passing for an unrelated reason.
        bare = subprocess.run(
            [sys.executable, HOOK, "--report", d.path],
            capture_output=True, text=True, timeout=15,
            stdin=subprocess.DEVNULL,
            env=_env(CLAUDE_TOOLKIT_CONFIG="/nonexistent.json"),
        )
        self.assertIn("finding", bare.stdout)

    # NOTE: there is deliberately no test here that re-runs this suite under a
    # `{"memory_lint": {"enabled": false}}` config to prove hermeticity. It
    # cannot work and it would look like it does: the module-scope pin above
    # re-fires in the child and overwrites the very config the test set, so the
    # child would pass without ever seeing the opt-out — while also recursing.
    # Hermeticity here is STRUCTURAL, guaranteed by that pin and asserted by
    # test_absent_config_means_enabled. Verify it from outside instead:
    #
    #   for c in off tuned; do CLAUDE_TOOLKIT_CONFIG=/tmp/cfg-$c.json \
    #     python3 tests/test_lint_lib.py; done

    def test_threshold_of_zero_falls_back_to_the_default(self):
        """0 is treated as unset, and that is deliberate — see config.example.json.

        Read literally it would be the opposite of what someone reaching for it
        wants: every hook exceeds 0, so the whole index would be reported at
        every session start. `enabled: false` is the off switch.
        """

        class Zero(object):
            def get(self, section, key, default=None):
                return default

            def get_int(self, section, key, default=None):
                return 0

        self.assertEqual(
            lint_lib.max_hook_chars(Zero()), lint_lib.DEFAULT_MAX_HOOK_CHARS
        )
        self.assertEqual(
            lint_lib.max_mean_hook_chars(Zero()),
            lint_lib.DEFAULT_MAX_MEAN_HOOK_CHARS,
        )
        d = MemoryDir([("a.md", "x" * 120)])
        self.addCleanup(d.cleanup)
        self.assertIsNone(
            lint_lib.build(d.path, config=Zero()),
            "a 0 threshold must not turn a healthy index into a full report",
        )

    def test_a_broken_config_fails_toward_enabled(self):
        """This only ever prints advice, so failing to ON costs one line."""

        class Broken(object):
            def get(self, *a, **k):
                raise RuntimeError("config moved")

            def get_int(self, *a, **k):
                raise RuntimeError("config moved")

        self.assertIs(lint_lib.is_enabled(config=Broken()), True)
        self.assertEqual(
            lint_lib.max_hook_chars(Broken()), lint_lib.DEFAULT_MAX_HOOK_CHARS
        )


def _index_of_chars(target):
    """Rows whose index totals at least `target` characters."""
    rows, size, i = [], 0, 0
    while size < target:
        name, hook = "m%04d.md" % i, "h" * 140
        rows.append((name, hook))
        size += len(row(name, hook)) + 1
        i += 1
    return rows


def _lines_of(n):
    """Plain text of exactly `n` harness-counted lines and few characters.

    Deliberately NOT index rows: a bullet-shaped line would also feed
    orphan/malformed-row/hook checks, and the lines-axis tests below want the
    line-budget finding in isolation. Total chars is `2n - 1`, well under
    DEFAULT_MAX_INDEX_CHARS for every n this file uses.
    """
    return "\n".join(["x"] * n)


def _index_of_lines_and_chars(n_lines, n_chars):
    """Plain text totalling exactly `n_lines` harness-counted lines and
    exactly `n_chars` characters -- for pinning the lines and chars axes to
    independent values in the same fixture (AC1.2c)."""
    pad = n_chars - 2 * (n_lines - 1)
    assert pad > 0, "fixture too small for the requested char total"
    return "\n".join(["x"] * (n_lines - 1) + ["y" * pad])


class TestIndexCharBudget(unittest.TestCase):
    """The whole-index budget. The unit is CHARACTERS; see the constant."""

    def test_under_budget_says_nothing_about_size(self):
        d = MemoryDir(_index_of_chars(5000))
        self.addCleanup(d.cleanup)
        self.assertIsNone(lint_lib.build(d.path))

    def test_over_budget_but_under_truncation_says_nothing_is_dropping_yet(self):
        """The window that exists so a session can act BEFORE losing a row."""
        d = MemoryDir(_index_of_chars(lint_lib.DEFAULT_MAX_INDEX_CHARS + 400))
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertGreater(scan["index_chars"], lint_lib.DEFAULT_MAX_INDEX_CHARS)
        self.assertLess(scan["index_chars"], lint_lib.INDEX_TRUNCATION_CHARS)
        notice = lint_lib.build(d.path)
        self.assertIn("Nothing is being dropped yet", notice)
        self.assertNotIn("silently drops rows", notice)

    def test_past_truncation_says_rows_are_being_lost_now(self):
        """The loud band. This wording is the only signal the failure has."""
        d = MemoryDir(_index_of_chars(lint_lib.INDEX_TRUNCATION_CHARS + 1200))
        self.addCleanup(d.cleanup)
        notice = lint_lib.build(d.path)
        self.assertIn("silently drops rows", notice)
        self.assertIn("no error anywhere", notice)

    def test_an_unparsable_index_never_prints_a_FABRICATED_row_count(self):
        """`rows == 0` on a large index is not exotic, and it broke the numbers.

        An earlier version divided by `mean_row or 1`, so with no parsable row
        the divisor became ONE CHARACTER and the character count was printed as
        a row count: thousands of rows "not reaching sessions" on an index a
        fraction of that length, beside "a row is worth ~0 chars". One wrong separator in
        a distill pass produces exactly this state — it is the premise the
        malformed-row check is built on — and this notice is the only signal the
        silent truncation has.
        """
        path = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        with open(os.path.join(path, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("- [X](x.md) - hyphen, not an em dash\n" * 900)
        scan = lint_lib.scan(path)
        self.assertEqual(scan["rows"], 0)
        self.assertGreater(scan["index_chars"], lint_lib.INDEX_TRUNCATION_CHARS)
        budget = [f for f in lint_lib.findings(scan) if "chars against" in f]
        self.assertEqual(len(budget), 1)
        self.assertNotIn("row(s)", budget[0])
        self.assertNotIn("~0 chars", budget[0])
        self.assertIn("cannot be estimated", budget[0])

    def test_report_never_shows_a_NEGATIVE_overage(self):
        """`OVER by -23970` printed two lines above `status healthy`.

        The over/under branch keyed on the row count as well as the comparison,
        so a readable index with no parsable row took the over-budget branch
        while being far under it. install.sh provisions this tool, so this was
        a new user's first impression of it.
        """
        path = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        with open(os.path.join(path, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write("just prose, no rows at all\n")
        text = lint_lib.report(path)
        budget = [l for l in text.splitlines() if "index budget" in l]
        self.assertEqual(len(budget), 1)
        self.assertNotIn("OVER by -", budget[0])
        self.assertIn("spare", budget[0])

    def test_scan_returns_every_key_even_on_an_early_return(self):
        """`unrouted` was set only at the end, past three early returns, and
        `budget_lib` consumes this dict directly.

        Covers the two-level keys: an early return must
        satisfy every consumer of the new keys too, not just the old ones.
        """
        result = lint_lib.scan("/nonexistent/memory")
        for key in (
            "unrouted", "orphans", "rows", "index_chars",
            "layout", "leaf_rows_outside_always", "missing_topics",
            "unnominated_topics", "duplicate_placements", "bad_targets",
            "oversized_topics", "malformed_topic_rows", "topics",
        ):
            self.assertIn(key, result)
        self.assertEqual(result["layout"], "flat")

    def test_the_budget_is_configurable_and_a_zero_does_not_silence_it(self):
        class Config(object):
            def __init__(self, value):
                self.value = value

            def get_int(self, _section, key, default=None):
                return self.value if key == "max_index_chars" else default

            def get(self, *_a, **kw):
                return kw.get("default")

        self.assertEqual(lint_lib.max_index_chars(Config(9000)), 9000)
        # 0 reads as "unset", never as "a cap everything exceeds" -- the same
        # trap the hook thresholds document.
        self.assertEqual(
            lint_lib.max_index_chars(Config(0)), lint_lib.DEFAULT_MAX_INDEX_CHARS
        )

    def test_a_cap_at_or_past_truncation_is_clamped_below_it(self):
        """A cap the index cannot cross before rows are lost is no cap."""
        class Config(object):
            def __init__(self, value):
                self.value = value

            def get_int(self, _section, key, default=None):
                return self.value if key == "max_index_chars" else default

            def get(self, *_a, **kw):
                return kw.get("default")

        edge = lint_lib.INDEX_TRUNCATION_CHARS - 1
        for configured in (30000, lint_lib.INDEX_TRUNCATION_CHARS, edge):
            self.assertEqual(lint_lib.max_index_chars(Config(configured)), edge,
                             configured)
        # Positive control: a looser value still UNDER truncation is honoured.
        self.assertEqual(lint_lib.max_index_chars(Config(24500)), 24500)


class _LinesConfig(object):
    """A config double for `max_index_lines`, same shape as the chars one."""

    def __init__(self, value):
        self.value = value

    def get_int(self, _section, key, default=None):
        return self.value if key == "max_index_lines" else default

    def get(self, *_a, **kw):
        return kw.get("default")


class TestMaxIndexLines(unittest.TestCase):
    """AC1.1 -- max_index_lines(), same shape and clamp as max_index_chars()."""

    def test_no_config_is_the_default(self):
        self.assertEqual(lint_lib.max_index_lines(), lint_lib.DEFAULT_MAX_INDEX_LINES)
        self.assertEqual(lint_lib.max_index_lines(None), 190)

    def test_configured_value_under_the_clamp_is_honoured(self):
        self.assertEqual(lint_lib.max_index_lines(_LinesConfig(150)), 150)

    def test_configured_value_past_the_clamp_is_capped_below_truncation(self):
        edge = lint_lib.INDEX_TRUNCATION_LINES - 1
        self.assertEqual(edge, 199)
        self.assertEqual(lint_lib.max_index_lines(_LinesConfig(500)), edge)
        self.assertEqual(lint_lib.max_index_lines(_LinesConfig(edge)), edge)

    def test_zero_or_negative_reads_as_unset(self):
        self.assertEqual(
            lint_lib.max_index_lines(_LinesConfig(0)),
            lint_lib.DEFAULT_MAX_INDEX_LINES,
        )
        self.assertEqual(
            lint_lib.max_index_lines(_LinesConfig(-3)),
            lint_lib.DEFAULT_MAX_INDEX_LINES,
        )


class TestAnchoredCapValues(unittest.TestCase):
    """The anchors are pinned as LITERALS, on purpose.

    Every boundary fixture elsewhere is written relative to these constants so a
    cap change cannot quietly turn an over-cap test into an under-cap one. That
    makes the fixtures blind to the constant's VALUE: restoring the old 12,000
    topic cap left every suite green (N3b Code Review). These pins are the only
    place the harness anchors (200 lines / 25,000 chars, read from the 2.1.280
    binary) are asserted independently of the code that uses them.
    """

    def test_topic_caps_sit_on_the_harness_anchor(self):
        self.assertEqual(lint_lib.TOPIC_MAX_CHARS, 25000)
        self.assertEqual(lint_lib.TOPIC_MAX_LINES, 200)

    def test_index_line_truncation_is_the_harness_limit(self):
        self.assertEqual(lint_lib.INDEX_TRUNCATION_LINES, 200)
        self.assertEqual(lint_lib.DEFAULT_MAX_INDEX_LINES, 190)


class TestHarnessLineCount(unittest.TestCase):
    """AC1.2b -- the harness's own count, never `len(text.splitlines())`."""

    def test_trailing_blank_lines_do_not_count(self):
        text = "x\n" * 199 + "\n\n"
        self.assertEqual(lint_lib.harness_line_count(text), 199)
        # What splitlines() would have said, named so the contrast is explicit.
        self.assertEqual(len(text.splitlines()), 201)

    def test_leading_blank_lines_do_not_count_and_the_index_reads_not_past(self):
        text = "\n\n" + "x\n" * 200
        self.assertEqual(lint_lib.harness_line_count(text), 200)
        path = tempfile.mkdtemp(prefix="memlint-harnesscount-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        with open(os.path.join(path, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
        scan = lint_lib.scan(path)
        self.assertEqual(scan["index_lines"], 200)
        notice = lint_lib.build(path)
        self.assertNotIn("PAST", notice)
        self.assertIn("Nothing is being dropped yet", notice)

    def test_empty_text_is_one_line(self):
        self.assertEqual(lint_lib.harness_line_count(""), 1)


class TestIndexLineBudget(unittest.TestCase):
    """AC1.2 / AC1.2c / AC1.6 -- the lines axis, and its interaction with chars."""

    def _scan_path(self, text):
        path = tempfile.mkdtemp(prefix="memlint-linebudget-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        with open(os.path.join(path, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_at_the_cap_is_silent_about_lines(self):
        cap = lint_lib.max_index_lines()
        text = _lines_of(cap)
        self.assertEqual(lint_lib.harness_line_count(text), cap)
        self.assertLess(len(text), lint_lib.DEFAULT_MAX_INDEX_CHARS)
        self.assertIsNone(lint_lib.build(self._scan_path(text)))

    def test_one_over_the_cap_says_nothing_dropped_yet(self):
        cap = lint_lib.max_index_lines()
        text = _lines_of(cap + 1)
        notice = lint_lib.build(self._scan_path(text))
        self.assertIn("Nothing is being dropped yet", notice)
        self.assertNotIn("PAST", notice)

    def test_past_truncation_names_the_lost_line_count(self):
        text = _lines_of(lint_lib.INDEX_TRUNCATION_LINES + 1)
        notice = lint_lib.build(self._scan_path(text))
        self.assertIn("PAST the 200-line point", notice)
        self.assertIn("the last 1 line(s) are not reaching sessions", notice)
        self.assertIn("Cut rows now", notice)

    def test_an_index_with_html_comments_says_its_count_may_read_high(self):
        # Merge Review: the loader strips comments before counting, so 200
        # content lines + a 5-line comment load whole while this counts 205.
        # The count stays an upper bound; the finding must say so.
        text = _lines_of(200) + "\n<!--\na\nb\nc\n-->"
        self.assertEqual(lint_lib.harness_line_count(text), 205)
        notice = lint_lib.build(self._scan_path(text))
        self.assertIn("PAST the 200-line point", notice)
        self.assertIn(lint_lib.COMMENT_CAVEAT.strip(), notice)

    def test_an_index_without_comments_carries_no_caveat(self):
        notice = lint_lib.build(self._scan_path(_lines_of(205)))
        self.assertIn("PAST the 200-line point", notice)
        self.assertNotIn("HTML comments", notice)

    def test_at_truncation_is_the_not_past_form(self):
        text = _lines_of(lint_lib.INDEX_TRUNCATION_LINES)
        notice = lint_lib.build(self._scan_path(text))
        self.assertNotIn("PAST", notice)
        self.assertIn("Nothing is being dropped yet", notice)

    def test_lines_past_truncation_and_chars_over_cap_but_not_past(self):
        """201 lines (PAST) + 24,001 chars (over cap, not past truncation)."""
        n_lines = lint_lib.INDEX_TRUNCATION_LINES + 1
        n_chars = lint_lib.DEFAULT_MAX_INDEX_CHARS + 1
        text = _index_of_lines_and_chars(n_lines, n_chars)
        self.assertEqual(lint_lib.harness_line_count(text), n_lines)
        self.assertEqual(len(text), n_chars)
        findings = lint_lib.findings(lint_lib.scan(self._scan_path(text)))
        self.assertEqual(len(findings), 2)
        lines_finding, chars_finding = findings
        self.assertIn("PAST the 200-line point", lines_finding)
        self.assertIn("ALREADY losing its tail on lines", chars_finding)
        self.assertNotIn("Nothing is being dropped yet", lines_finding)
        self.assertNotIn("Nothing is being dropped yet", chars_finding)

    def test_lines_over_cap_but_not_past_and_chars_past_truncation(self):
        """191 lines (over cap, not past) + 25,100 chars (PAST truncation)."""
        n_lines = lint_lib.max_index_lines() + 1
        n_chars = lint_lib.INDEX_TRUNCATION_CHARS + 100
        text = _index_of_lines_and_chars(n_lines, n_chars)
        self.assertEqual(lint_lib.harness_line_count(text), n_lines)
        self.assertEqual(len(text), n_chars)
        findings = lint_lib.findings(lint_lib.scan(self._scan_path(text)))
        self.assertEqual(len(findings), 2)
        lines_finding, chars_finding = findings
        self.assertIn("ALREADY losing its tail on characters", lines_finding)
        self.assertNotIn("Nothing is being dropped yet", lines_finding)
        self.assertIn("silently drops rows", chars_finding)  # unchanged PAST form
        self.assertNotIn("Nothing is being dropped yet", chars_finding)


class TestExampleConfigMatchesTheCode(unittest.TestCase):
    """Every documented threshold equals the default the code actually uses.

    `install.sh` copies config.example.json to a machine's live config, so a
    number that drifts from the code is not a documentation bug — it is a
    SHIPPED setting that silently overrides the default while the comment
    beside it vouches for something else. That exact drift was the BLOCKING
    finding on #143, one key over, where the prose was updated and the values
    were not; nothing compared them, so nothing caught it.

    Asserted by spelling AND value, for every threshold rather than the one
    being added today.
    """

    THRESHOLDS = {
        "max_hook_chars": "DEFAULT_MAX_HOOK_CHARS",
        "max_mean_hook_chars": "DEFAULT_MAX_MEAN_HOOK_CHARS",
        "max_index_chars": "DEFAULT_MAX_INDEX_CHARS",
    }

    def test_documented_thresholds_match_their_code_defaults(self):
        path = os.path.join(REPO, "config.example.json")
        section = json.load(open(path, encoding="utf-8"))["memory_lint"]
        for key, const in self.THRESHOLDS.items():
            self.assertIn(key, section, "%s is undocumented in %s" % (key, path))
            self.assertEqual(
                section[key],
                getattr(lint_lib, const),
                "config.example.json ships %s=%r but the code default (%s) is %r"
                % (key, section[key], const, getattr(lint_lib, const)),
            )

    def test_every_documented_threshold_has_a_comment_key(self):
        section = json.load(
            open(os.path.join(REPO, "config.example.json"), encoding="utf-8")
        )["memory_lint"]
        for key in self.THRESHOLDS:
            self.assertIn(
                "//" + key, section, "%s ships with no explanation" % key
            )


class TestUnroutedFiles(unittest.TestCase):
    """`routed: false` — a file kept on purpose with no index row."""

    def _dir(self, body):
        d = MemoryDir(
            [("kept.md", "a hook")],
            files=["kept.md", "retired.md"],
            bodies={"retired.md": body},
        )
        self.addCleanup(d.cleanup)
        return d

    def test_a_marked_file_is_not_an_orphan(self):
        d = self._dir("---\nname: retired\nrouted: false\n---\n\nbody\n")
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["orphans"], [])
        self.assertEqual(scan["unrouted"], ["retired.md"])
        self.assertIsNone(lint_lib.build(d.path))

    def test_a_trailing_yaml_comment_does_not_break_the_marker(self):
        """The defect this was shipped with, first time.

        The original pattern anchored `$` straight after `false`, so every one
        of the 13 files it was written for -- each carrying a comment saying
        WHY the row was retired -- went on reporting as an orphan. A marker
        that fails to parse looks exactly like a file that never had one.
        """
        d = self._dir(
            "---\nname: retired\nrouted: false  # retired 2026-09-14, budget\n"
            "---\n\nbody\n"
        )
        self.assertEqual(lint_lib.scan(d.path)["orphans"], [])

    def test_the_marker_is_ignored_outside_frontmatter(self):
        """Otherwise any file DISCUSSING the marker exempts itself silently."""
        d = self._dir(
            "---\nname: retired\n---\n\nA file may declare\nrouted: false\n"
            "in its frontmatter to opt out.\n"
        )
        self.assertEqual(lint_lib.scan(d.path)["orphans"], ["retired.md"])

    def test_an_unmarked_file_with_no_row_is_still_an_orphan(self):
        d = self._dir("---\nname: retired\n---\n\nbody\n")
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["orphans"], ["retired.md"])
        self.assertIn("no row in MEMORY.md", lint_lib.build(d.path))


class TestLayout(unittest.TestCase):
    """layout() detects two-level purely from a `topics/` SUBDIRECTORY.

    Never configured, per the format section -- so this is the only place the
    distinction is decided, and it must never raise on a bad path.
    """

    def test_two_level_when_topics_is_a_directory(self):
        path = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        os.mkdir(os.path.join(path, lint_lib.TOPICS_DIR))
        self.assertEqual(lint_lib.layout(path), "two-level")

    def test_flat_with_no_topics_entry_at_all(self):
        path = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        self.assertEqual(lint_lib.layout(path), "flat")

    def test_flat_when_topics_is_a_regular_file(self):
        """A `topics` FILE, not a directory, must not switch on two-level rules."""
        path = tempfile.mkdtemp(prefix="memlint-")
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        with open(os.path.join(path, lint_lib.TOPICS_DIR), "w") as fh:
            fh.write("not a directory")
        self.assertEqual(lint_lib.layout(path), "flat")

    def test_flat_for_none_and_a_nonexistent_path(self):
        self.assertEqual(lint_lib.layout(None), "flat")
        self.assertEqual(lint_lib.layout("/nonexistent/memory"), "flat")


class TestParseSections(unittest.TestCase):
    """Section tagging for the two-level rooted graph.

    A row belongs to the section of the nearest heading ABOVE it; nothing
    above the first heading belongs to any section at all.
    """

    def test_rows_above_the_first_heading_have_no_section(self):
        text = row("a.md", "hook") + "\n## Always\n" + row("b.md", "hook") + "\n"
        rows = lint_lib.parse_sections(text)
        self.assertEqual(rows[0]["file"], "a.md")
        self.assertIsNone(rows[0]["section"])
        self.assertEqual(rows[1]["section"], "always")

    def test_always_heading_tags_always(self):
        text = "## Always\n" + row("a.md", "hook") + "\n"
        self.assertEqual(lint_lib.parse_sections(text)[0]["section"], "always")

    def test_always_heading_with_trailing_prose_still_tags_always(self):
        text = "## Always — every rule\n" + row("a.md", "hook") + "\n"
        self.assertEqual(lint_lib.parse_sections(text)[0]["section"], "always")

    def test_topics_heading_at_any_level_tags_topics(self):
        text = "### topics\n" + row("topics/t.md", "hook") + "\n"
        self.assertEqual(lint_lib.parse_sections(text)[0]["section"], "topics")

    def test_always_dash_on_heading_is_pinned_to_always(self):
        """`## Always-on` matches the regex -- `\\b` falls between `s` and `-`.

        Pinned exactly as the format section requires: this is what the regex
        does, not a case anyone would choose to write by hand.
        """
        text = "## Always-on\n" + row("a.md", "hook") + "\n"
        self.assertEqual(lint_lib.parse_sections(text)[0]["section"], "always")

    def test_an_unnamed_heading_tags_other_and_does_not_inherit_always(self):
        text = (
            "## Always\n" + row("a.md", "hook") + "\n"
            "## Notes\n" + row("b.md", "hook") + "\n"
        )
        rows = lint_lib.parse_sections(text)
        self.assertEqual(rows[0]["section"], "always")
        self.assertEqual(rows[1]["section"], "other")

    def test_en_dash_row_is_returned_unparsed(self):
        text = "## Always\n- [T](a.md) – hook\n"
        rows = lint_lib.parse_sections(text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["file"], "a.md")
        self.assertIs(rows[0]["parsed"], False)

    def test_a_bare_bullet_and_plain_prose_are_not_rows(self):
        text = "## Always\n- a bare bullet that is not an index row\nJust prose.\n"
        self.assertEqual(lint_lib.parse_sections(text), [])


class TestClassifyIndexTarget(unittest.TestCase):
    """Target classification for a row in MEMORY.md itself."""

    def test_topic_refs(self):
        self.assertEqual(lint_lib.classify_index_target("topics/a.md"), ("topic", "a.md"))
        self.assertEqual(lint_lib.classify_index_target("./topics/a.md"), ("topic", "a.md"))

    def test_leaf_refs(self):
        self.assertEqual(lint_lib.classify_index_target("a.md"), ("leaf", "a.md"))
        self.assertEqual(lint_lib.classify_index_target("./a.md"), ("leaf", "a.md"))

    def test_bad_targets(self):
        for target in ("MEMORY.md", "topics/x/a.md", "../a.md", "/abs/a.md", "a.txt"):
            self.assertEqual(
                lint_lib.classify_index_target(target)[0], "bad", target
            )


class TestClassifyTopicTarget(unittest.TestCase):
    """Target classification for a row inside topics/<x>.md.

    Targets there are written relative to the TOPIC FILE's own directory, so
    `../foo.md` is the leaf `foo.md` and a same-directory `foo.md` (no `../`)
    resolves to `topics/foo.md` -- correctly bad, since nothing under
    `topics/` is itself a leaf reference target.
    """

    def test_leaf_ref_resolves_against_the_topic_files_own_directory(self):
        self.assertEqual(lint_lib.classify_topic_target("../a.md"), ("leaf", "a.md"))

    def test_bad_targets(self):
        for target in ("a.md", "../MEMORY.md", "../../a.md", "../topics/b.md", "/abs/a.md"):
            self.assertEqual(
                lint_lib.classify_topic_target(target)[0], "bad", target
            )


class TestLeafRowsOutsideAlways(unittest.TestCase):
    """`scan()`'s own `leaf_rows_outside_always` key is computed
    by CALLING this helper, so the two tools (memory-lint and context-growth,
    which calls it directly on an edited MEMORY.md) cannot disagree."""

    def test_topics_notes_and_above_the_first_heading_are_returned(self):
        text = (
            row("above.md", "hook") + "\n"
            "## Topics\n" + row("under-topics.md", "hook") + "\n"
            "## Notes\n" + row("under-notes.md", "hook") + "\n"
        )
        self.assertEqual(
            lint_lib.leaf_rows_outside_always(text),
            sorted(["above.md", "under-notes.md", "under-topics.md"]),
        )

    def test_always_rows_and_topic_refs_are_excluded(self):
        text = (
            "## Always\n" + row("a.md", "hook") + "\n"
            "## Topics\n" + row("topics/t.md", "hook") + "\n"
        )
        self.assertEqual(lint_lib.leaf_rows_outside_always(text), [])

    def test_duplicates_collapse_and_order_is_sorted(self):
        text = (
            "## Topics\n"
            + row("z.md", "hook") + "\n"
            + row("a.md", "hook") + "\n"
            + row("a.md", "hook again") + "\n"
        )
        self.assertEqual(lint_lib.leaf_rows_outside_always(text), ["a.md", "z.md"])


class TwoLevelDir(object):
    """A throwaway two-level memory directory: MEMORY.md, leaf files, topics/.

    Deliberately lower-level than `MemoryDir`: two-level fixtures vary rows,
    headings and row order per AC (a topic ref nominated from the wrong
    section, a malformed Topics row, an oversized topic file), so a fixed
    row-list API would fight more of these than it would help. Callers pass
    MEMORY.md's raw text and each topics/<name>.md file's raw text directly.
    """

    def __init__(self, memory_md, topic_files=None, leaf_files=None, leaf_bodies=None):
        self.path = tempfile.mkdtemp(prefix="memlint-2lvl-")
        with open(os.path.join(self.path, "MEMORY.md"), "w", encoding="utf-8") as fh:
            fh.write(memory_md)
        for name in (leaf_files or []):
            body = (leaf_bodies or {}).get(name, "---\nname: x\n---\n\nbody\n")
            with open(os.path.join(self.path, name), "w", encoding="utf-8") as fh:
                fh.write(body)
        os.mkdir(os.path.join(self.path, lint_lib.TOPICS_DIR))
        for name, topic_text in (topic_files or {}).items():
            with open(
                os.path.join(self.path, lint_lib.TOPICS_DIR, name),
                "w", encoding="utf-8",
            ) as fh:
                fh.write(topic_text)

    def cleanup(self):
        shutil.rmtree(self.path, ignore_errors=True)


class TestTwoLevelScan(unittest.TestCase):
    """The rooted graph: reachability from Always plus nominated topic files.

    """

    def test_ac1_clean_fixture_is_silent(self):
        d = TwoLevelDir(
            "## Always\n" + row("a.md", "hook a") + "\n"
            "## Topics\n" + row("topics/t.md", "hook t") + "\n",
            topic_files={"t.md": row("../b.md", "hook b") + "\n"},
            leaf_files=["a.md", "b.md"],
        )
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["layout"], "two-level")
        self.assertEqual(scan["orphans"], [])
        self.assertEqual(lint_lib.findings(scan), [])

    def test_ac2_orphan_with_routed_false_is_still_an_orphan(self):
        d = TwoLevelDir(
            "## Always\n\n## Topics\n",
            leaf_files=["orphan.md"],
            leaf_bodies={"orphan.md": "---\nname: x\nrouted: false\n---\n\nbody\n"},
        )
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["orphans"], ["orphan.md"])
        self.assertEqual(scan["unrouted"], [])

    def test_ac3_leaf_only_in_an_unnominated_topic_is_an_orphan(self):
        d = TwoLevelDir(
            "## Always\n\n## Topics\n",
            topic_files={"t.md": row("../b.md", "hook b") + "\n"},
            leaf_files=["b.md"],
        )
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["orphans"], ["b.md"])
        self.assertEqual(scan["unnominated_topics"], ["t.md"])

    def test_ac4_topic_nominated_from_always_not_topics_is_unnominated(self):
        d = TwoLevelDir(
            "## Always\n" + row("topics/t.md", "hook") + "\n## Topics\n",
            topic_files={"t.md": row("../x.md", "hook") + "\n"},
            leaf_files=["x.md"],
        )
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["unnominated_topics"], ["t.md"])
        self.assertEqual(scan["orphans"], ["x.md"])

    def test_ac5_missing_topic_is_reported(self):
        d = TwoLevelDir("## Topics\n" + row("topics/gone.md", "hook") + "\n")
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["missing_topics"], ["gone.md"])

    def test_a_nominated_md_directory_is_a_missing_topic_not_a_topic_file(self):
        # Code Review finding: a `*.md` DIRECTORY under topics/ was inventoried
        # as an existing, empty topic, so the row pointing at it read healthy.
        d = TwoLevelDir("## Topics\n" + row("topics/t.md", "hook") + "\n")
        self.addCleanup(d.cleanup)
        os.mkdir(os.path.join(d.path, lint_lib.TOPICS_DIR, "t.md"))
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["missing_topics"], ["t.md"])
        self.assertEqual(scan["topics"], [])
        self.assertNotEqual(lint_lib.findings(scan), [])

    def test_ac6_leaf_in_two_topics_is_a_duplicate_placement(self):
        d = TwoLevelDir(
            "## Topics\n"
            + row("topics/t.md", "hook") + "\n"
            + row("topics/u.md", "hook") + "\n",
            topic_files={
                "t.md": row("../b.md", "hook") + "\n",
                "u.md": row("../b.md", "hook") + "\n",
            },
            leaf_files=["b.md"],
        )
        self.addCleanup(d.cleanup)
        self.assertEqual(
            lint_lib.scan(d.path)["duplicate_placements"],
            [("b.md", ["topics/t.md", "topics/u.md"])],
        )

    def test_ac6_leaf_in_always_and_a_topic_lists_always_first(self):
        d = TwoLevelDir(
            "## Always\n" + row("b.md", "hook") + "\n"
            "## Topics\n" + row("topics/t.md", "hook") + "\n",
            topic_files={"t.md": row("../b.md", "hook") + "\n"},
            leaf_files=["b.md"],
        )
        self.addCleanup(d.cleanup)
        self.assertEqual(
            lint_lib.scan(d.path)["duplicate_placements"],
            [("b.md", ["Always", "topics/t.md"])],
        )

    def test_ac7_leaf_row_under_topics_is_outside_always_not_an_orphan(self):
        d = TwoLevelDir("## Topics\n" + row("x.md", "hook") + "\n", leaf_files=["x.md"])
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["leaf_rows_outside_always"], ["x.md"])
        self.assertEqual(scan["orphans"], [])

    def test_ac7_leaf_row_above_the_first_heading_is_outside_always_too(self):
        d = TwoLevelDir(row("y.md", "hook") + "\n## Always\n", leaf_files=["y.md"])
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["leaf_rows_outside_always"], ["y.md"])
        self.assertEqual(scan["orphans"], [])

    def test_ac8_topic_row_missing_the_dotdot_prefix_is_a_bad_target(self):
        d = TwoLevelDir(
            "## Topics\n" + row("topics/t.md", "hook") + "\n",
            topic_files={"t.md": row("b.md", "hook") + "\n"},
            leaf_files=["b.md"],
        )
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["bad_targets"], [("topics/t.md", "b.md")])
        self.assertEqual(scan["orphans"], ["b.md"])

    def test_ac9_topic_row_naming_a_missing_leaf_is_a_dead_link(self):
        d = TwoLevelDir(
            "## Topics\n" + row("topics/t.md", "hook") + "\n",
            topic_files={"t.md": row("../gone.md", "hook") + "\n"},
        )
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["dead_links"], ["gone.md"])

    def test_an_always_row_naming_an_md_directory_is_a_dead_link(self):
        # Merge Review finding: the two-level reachability set trusted a
        # `*.md` DIRECTORY as an openable leaf, so the scan read clean.
        d = TwoLevelDir("## Always\n" + row("leaf.md", "hook") + "\n## Topics\n")
        self.addCleanup(d.cleanup)
        os.mkdir(os.path.join(d.path, "leaf.md"))
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["dead_links"], ["leaf.md"])
        self.assertNotEqual(lint_lib.findings(scan), [])

    def test_ac10_topic_file_at_exactly_the_cap_is_not_oversized(self):
        """AC1.3 -- exactly 25,000 chars AND exactly 200 lines, still not over."""
        text = _lines_of(lint_lib.TOPIC_MAX_LINES)
        text = text + "x" * (lint_lib.TOPIC_MAX_CHARS - len(text))
        self.assertEqual(len(text), lint_lib.TOPIC_MAX_CHARS)
        self.assertEqual(lint_lib.harness_line_count(text), lint_lib.TOPIC_MAX_LINES)
        d = TwoLevelDir("## Topics\n", topic_files={"t.md": text})
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["oversized_topics"], [])

    def test_ac10_topic_file_one_char_over_the_cap_is_oversized(self):
        text = "x" * (lint_lib.TOPIC_MAX_CHARS + 1)
        self.assertGreater(len(text), lint_lib.TOPIC_MAX_CHARS)  # precondition
        d = TwoLevelDir("## Topics\n", topic_files={"t.md": text})
        self.addCleanup(d.cleanup)
        self.assertEqual(
            lint_lib.scan(d.path)["oversized_topics"],
            [("t.md", lint_lib.TOPIC_MAX_CHARS + 1, "chars", lint_lib.TOPIC_MAX_CHARS)],
        )

    def test_ac10_oversize_is_measured_in_characters_not_bytes(self):
        """A padded em dash is 3 bytes each; TOPIC_MAX_CHARS chars must not be
        oversized even though the byte count is."""
        text = "—" * lint_lib.TOPIC_MAX_CHARS
        self.assertEqual(len(text), lint_lib.TOPIC_MAX_CHARS)
        self.assertGreater(len(text.encode("utf-8")), lint_lib.TOPIC_MAX_CHARS)
        d = TwoLevelDir("## Topics\n", topic_files={"t.md": text})
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["oversized_topics"], [])

    def test_ac1_3_topic_one_line_over_the_lines_cap_is_oversized_on_lines(self):
        """AC1.3 -- 201 short lines: over on LINES, well under the char cap."""
        text = _lines_of(lint_lib.TOPIC_MAX_LINES + 1)
        self.assertEqual(
            lint_lib.harness_line_count(text), lint_lib.TOPIC_MAX_LINES + 1
        )
        self.assertLess(len(text), lint_lib.TOPIC_MAX_CHARS)  # precondition
        d = TwoLevelDir("## Topics\n", topic_files={"t.md": text})
        self.addCleanup(d.cleanup)
        self.assertEqual(
            lint_lib.scan(d.path)["oversized_topics"],
            [
                (
                    "t.md",
                    lint_lib.TOPIC_MAX_LINES + 1,
                    "lines",
                    lint_lib.TOPIC_MAX_LINES,
                )
            ],
        )

    def test_ac1_3_topic_over_on_both_axes_reports_the_chars_entry_only(self):
        """AC1.3 -- both over -> ONE entry, the chars one."""
        n_lines = lint_lib.TOPIC_MAX_LINES + 1
        n_chars = lint_lib.TOPIC_MAX_CHARS + 1000
        text = _index_of_lines_and_chars(n_lines, n_chars)
        self.assertGreater(len(text), lint_lib.TOPIC_MAX_CHARS)  # precondition
        self.assertGreater(
            lint_lib.harness_line_count(text), lint_lib.TOPIC_MAX_LINES
        )  # precondition
        d = TwoLevelDir("## Topics\n", topic_files={"t.md": text})
        self.addCleanup(d.cleanup)
        self.assertEqual(
            lint_lib.scan(d.path)["oversized_topics"],
            [("t.md", n_chars, "chars", lint_lib.TOPIC_MAX_CHARS)],
        )

    def test_ac11_en_dash_row_in_a_topic_file_is_malformed_not_an_orphan(self):
        d = TwoLevelDir(
            "## Topics\n" + row("topics/t.md", "hook") + "\n",
            topic_files={"t.md": "- [B](../b.md) – hook\n"},
            leaf_files=["b.md"],
        )
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["malformed_topic_rows"], [("t.md", "../b.md")])
        self.assertEqual(scan["orphans"], [])
        self.assertTrue(
            any(
                "a row in a topic file that does not parse" in f
                for f in lint_lib.findings(scan)
            )
        )

    def test_ac11b_en_dash_topics_row_still_nominates_its_topic(self):
        d = TwoLevelDir(
            "## Topics\n- [T](topics/t.md) – hook\n",
            topic_files={"t.md": row("../x.md", "hook") + "\n"},
            leaf_files=["x.md"],
        )
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["unnominated_topics"], [])
        self.assertTrue(
            any(
                "has a row in MEMORY.md that does not parse" in f
                for f in lint_lib.findings(scan)
            )
        )

    def test_ac12_topics_report_shows_nominated_status_and_row_count(self):
        t_text = row("../x.md", "hook") + "\n"
        u_text = row("../y.md", "hook") + "\n"
        d = TwoLevelDir(
            "## Topics\n" + row("topics/t.md", "hook") + "\n",
            topic_files={"t.md": t_text, "u.md": u_text},
            leaf_files=["x.md", "y.md"],
        )
        self.addCleanup(d.cleanup)
        self.assertEqual(
            lint_lib.scan(d.path)["topics"],
            [
                {
                    "name": "t.md",
                    "chars": len(t_text),
                    "lines": lint_lib.harness_line_count(t_text),
                    "rows": 1,
                    "nominated": True,
                },
                {
                    "name": "u.md",
                    "chars": len(u_text),
                    "lines": lint_lib.harness_line_count(u_text),
                    "rows": 1,
                    "nominated": False,
                },
            ],
        )

    def test_ac13_flat_invariance_with_headings_present(self):
        """Flat mode ignores Always/Topics headings entirely.

        NOTE: a flat-invariance test may
        legitimately pass on code that predates the two-level layout, since that
        layout does not touch the flat code path at all. This is that case, named as such
        rather than contorted to go red before implementation.
        """
        def build(memory_md):
            path = tempfile.mkdtemp(prefix="memlint-flatinv-")
            self.addCleanup(shutil.rmtree, path, ignore_errors=True)
            for name in ("a.md", "x.md"):
                with open(os.path.join(path, name), "w", encoding="utf-8") as fh:
                    fh.write("body\n")
            with open(os.path.join(path, "retired.md"), "w", encoding="utf-8") as fh:
                fh.write("---\nname: retired\nrouted: false\n---\n\nbody\n")
            with open(os.path.join(path, "MEMORY.md"), "w", encoding="utf-8") as fh:
                fh.write(memory_md)
            return path

        with_headings = build(
            "## Always\n" + row("a.md", "hook a") + "\n"
            "## Topics\n" + row("x.md", "hook x") + "\n"
        )
        without_headings = build(
            row("a.md", "hook a") + "\n" + row("x.md", "hook x") + "\n"
        )

        scan_with = lint_lib.scan(with_headings)
        self.assertEqual(scan_with["layout"], "flat")
        self.assertEqual(scan_with["unrouted"], ["retired.md"])
        self.assertEqual(scan_with["orphans"], [])
        self.assertEqual(scan_with["leaf_rows_outside_always"], [])
        self.assertEqual(
            lint_lib.findings(scan_with),
            lint_lib.findings(lint_lib.scan(without_headings)),
        )


class TestTwoLevelReport(unittest.TestCase):
    """report()'s per-topic rows."""

    def test_ac1_report_shows_layout_and_a_topic_line(self):
        d = TwoLevelDir(
            "## Always\n" + row("a.md", "hook a") + "\n"
            "## Topics\n" + row("topics/t.md", "hook t") + "\n",
            topic_files={"t.md": row("../b.md", "hook b") + "\n"},
            leaf_files=["a.md", "b.md"],
        )
        self.addCleanup(d.cleanup)
        text = lint_lib.report(d.path)
        self.assertIn(
            "layout          two-level — 1 topic file(s) under topics/", text
        )
        # AC1.5 -- a new "index lines" report line, distinct from "index budget".
        index_lines_lines = [
            l for l in text.splitlines() if l.startswith("index lines")
        ]
        self.assertEqual(len(index_lines_lines), 1)
        self.assertIn("lines", index_lines_lines[0])
        topic_lines = [l for l in text.splitlines() if l.startswith("topic ")]
        self.assertEqual(len(topic_lines), 1)
        self.assertIn("t.md", topic_lines[0])
        # N3b: 12000 was replaced by the anchored constant -- assert the
        # CONSTANT, never the literal, so a future cap change cannot silently
        # decouple this test from the code (item 6).
        self.assertIn(str(lint_lib.TOPIC_MAX_CHARS), topic_lines[0])
        self.assertIn(str(lint_lib.TOPIC_MAX_LINES), topic_lines[0])
        self.assertIn("1 row(s)", topic_lines[0])

    def test_ac2_oversized_topic_shows_OVER_naming_the_unit(self):
        text_over_chars = "x" * (lint_lib.TOPIC_MAX_CHARS + 1)
        self.assertGreater(len(text_over_chars), lint_lib.TOPIC_MAX_CHARS)
        d = TwoLevelDir("## Topics\n", topic_files={"t.md": text_over_chars})
        self.addCleanup(d.cleanup)
        text = lint_lib.report(d.path)
        topic_line = [l for l in text.splitlines() if l.startswith("topic ")][0]
        self.assertIn("OVER", topic_line)
        self.assertIn("chars", topic_line.split("—")[1])

    def test_ac2_oversized_topic_on_lines_shows_OVER_naming_lines(self):
        text_over_lines = _lines_of(lint_lib.TOPIC_MAX_LINES + 1)
        self.assertLess(len(text_over_lines), lint_lib.TOPIC_MAX_CHARS)
        d = TwoLevelDir("## Topics\n", topic_files={"t.md": text_over_lines})
        self.addCleanup(d.cleanup)
        text = lint_lib.report(d.path)
        topic_line = [l for l in text.splitlines() if l.startswith("topic ")][0]
        self.assertIn("OVER", topic_line)
        self.assertIn("lines", topic_line.split("—")[1])

    def test_ac2_unnominated_topic_ends_NOT_NOMINATED(self):
        d = TwoLevelDir(
            "## Topics\n",
            topic_files={"t.md": row("../x.md", "hook") + "\n"},
            leaf_files=["x.md"],
        )
        self.addCleanup(d.cleanup)
        text = lint_lib.report(d.path)
        topic_line = [l for l in text.splitlines() if l.startswith("topic ")][0]
        self.assertTrue(topic_line.rstrip().endswith("NOT NOMINATED"))

    def test_ac3_flat_report_has_no_layout_or_topic_lines(self):
        d = MemoryDir([("a.md", "hook")])
        self.addCleanup(d.cleanup)
        text = lint_lib.report(d.path)
        for line in text.splitlines():
            self.assertFalse(line.startswith("layout"), line)
            self.assertFalse(line.startswith("topic "), line)

    def test_ac4_through_the_executable_two_level_orphan_and_clean(self):
        orphan_dir = TwoLevelDir("## Always\n## Topics\n", leaf_files=["orphan.md"])
        self.addCleanup(orphan_dir.cleanup)
        project = tempfile.mkdtemp(prefix="memlint-2lvl-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        shutil.copytree(orphan_dir.path, os.path.join(project, "memory"))
        transcript = os.path.join(project, "s.jsonl")
        open(transcript, "w").close()

        proc = subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"transcript_path": transcript}),
            capture_output=True,
            text=True,
            timeout=15,
            env=_env(CLAUDE_TOOLKIT_CONFIG="/nonexistent.json"),
        )
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(
            payload["hookSpecificOutput"]["hookEventName"], "SessionStart"
        )
        notice = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("orphan.md", notice)
        self.assertIn("not in any topic file", notice)

        clean_dir = TwoLevelDir(
            "## Always\n" + row("a.md", "hook a") + "\n## Topics\n",
            leaf_files=["a.md"],
        )
        self.addCleanup(clean_dir.cleanup)
        project2 = tempfile.mkdtemp(prefix="memlint-2lvl-proj2-")
        self.addCleanup(shutil.rmtree, project2, ignore_errors=True)
        shutil.copytree(clean_dir.path, os.path.join(project2, "memory"))
        transcript2 = os.path.join(project2, "s.jsonl")
        open(transcript2, "w").close()

        proc2 = subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"transcript_path": transcript2}),
            capture_output=True,
            text=True,
            timeout=15,
            env=_env(CLAUDE_TOOLKIT_CONFIG="/nonexistent.json"),
        )
        self.assertEqual(proc2.returncode, 0)
        self.assertEqual(proc2.stdout, "")


class TestMemoryType(unittest.TestCase):
    """`memory_type()` -- only frontmatter starting at byte 0 counts."""

    def test_top_level_type(self):
        self.assertEqual(
            lint_lib.memory_type("---\nname: x\ntype: feedback\n---\n\nbody\n"),
            "feedback",
        )

    def test_nested_under_metadata(self):
        self.assertEqual(
            lint_lib.memory_type(
                "---\nmetadata:\n  type: project\n---\n\nbody\n"
            ),
            "project",
        )

    def test_lower_cased(self):
        self.assertEqual(
            lint_lib.memory_type("---\ntype: Feedback\n---\n\nbody\n"), "feedback"
        )

    def test_no_frontmatter_is_none(self):
        self.assertIsNone(lint_lib.memory_type("just a body\ntype: feedback\n"))

    def test_type_in_the_body_not_frontmatter_is_none(self):
        """AC2: a file merely discussing `type:` in its body must not count."""
        self.assertIsNone(
            lint_lib.memory_type(
                "---\nname: x\n---\n\nThis memory has no\ntype: feedback\nfield.\n"
            )
        )

    def test_frontmatter_with_no_type_is_none(self):
        self.assertIsNone(lint_lib.memory_type("---\nname: x\n---\n\nbody\n"))

    def test_unrecognised_type_is_returned_as_is(self):
        """memory_type() itself does not judge the value -- RULE/FACT_TYPES do."""
        self.assertEqual(
            lint_lib.memory_type("---\ntype: bogus\n---\n\nbody\n"), "bogus"
        )

    def test_none_text_is_none(self):
        self.assertIsNone(lint_lib.memory_type(None))

    def test_double_quoted_value(self):
        # FIX 3 (Merge Review) -- `type: "feedback"`.
        self.assertEqual(
            lint_lib.memory_type('---\ntype: "feedback"\n---\n\nbody\n'),
            "feedback",
        )

    def test_single_quoted_value(self):
        self.assertEqual(
            lint_lib.memory_type("---\ntype: 'feedback'\n---\n\nbody\n"),
            "feedback",
        )

    def test_trailing_yaml_comment(self):
        self.assertEqual(
            lint_lib.memory_type("---\ntype: feedback  # rule\n---\n\nbody\n"),
            "feedback",
        )

    def test_mismatched_quotes_do_not_match(self):
        # Still front-matter only, still no special-casing: a quote that
        # never closes is simply not one of the three accepted shapes, so
        # the line falls through exactly as an unrecognised token would if
        # nothing else in the frontmatter matched.
        self.assertIsNone(
            lint_lib.memory_type("---\ntype: \"feedback'\n---\n\nbody\n")
        )


class TestMemoryTypeSpellingsProducePlacementFindings(unittest.TestCase):
    """FIX 3 -- each accepted spelling of `type: feedback` must drive the SAME
    placement finding through a real `scan()`, not just `memory_type()` in
    isolation: a topic-only leaf (no Always row) is a misplaced rule."""

    SPELLINGS = {
        "unquoted": "type: feedback",
        "double_quoted": 'type: "feedback"',
        "single_quoted": "type: 'feedback'",
        "trailing_comment": "type: feedback  # rule",
    }

    def _topic_only_leaf(self, type_line):
        d = TwoLevelDir(
            "## Topics\n" + row("topics/t.md", "hook") + "\n",
            topic_files={"t.md": row("../a.md", "hook") + "\n"},
            leaf_files=["a.md"],
            leaf_bodies={"a.md": "---\n%s\n---\n\nbody\n" % type_line},
        )
        self.addCleanup(d.cleanup)
        return d

    def test_each_spelling_yields_feedback_and_the_placement_finding(self):
        for label, type_line in self.SPELLINGS.items():
            with self.subTest(spelling=label):
                d = self._topic_only_leaf(type_line)
                scan = lint_lib.scan(d.path)
                self.assertEqual(
                    scan["rules_outside_always"], [("a.md", "feedback")]
                )


class TestRulePlacement(unittest.TestCase):
    """N2 -- placement follows `type:`. Two-level layout only.

    """

    TYPES = {
        "feedback": "feedback",
        "user": "user",
        "project": "project",
        "reference": "reference",
        "missing": None,
        "bogus": "bogus",
    }

    def _leaf_body(self, kind):
        vtype = self.TYPES[kind]
        if vtype is None:
            return "just a body, no frontmatter\n"
        return "---\ntype: %s\n---\n\nbody\n" % vtype

    def _fixture(self):
        """Every kind, once in Always and once reachable only from a topic."""
        always_names = ["%s-always.md" % kind for kind in self.TYPES]
        topic_names = ["%s-topic.md" % kind for kind in self.TYPES]
        memory_md = (
            "## Always\n"
            + "".join(row(n, "hook") + "\n" for n in always_names)
            + "## Topics\n"
            + row("topics/t.md", "hook") + "\n"
        )
        topic_text = "".join(row("../" + n, "hook") + "\n" for n in topic_names)
        leaf_bodies = {}
        for kind in self.TYPES:
            leaf_bodies["%s-always.md" % kind] = self._leaf_body(kind)
            leaf_bodies["%s-topic.md" % kind] = self._leaf_body(kind)
        d = TwoLevelDir(
            memory_md,
            topic_files={"t.md": topic_text},
            leaf_files=always_names + topic_names,
            leaf_bodies=leaf_bodies,
        )
        self.addCleanup(d.cleanup)
        return d

    def test_ac1_the_12_case_matrix(self):
        """Rules flagged only when topic-only; facts flagged only when in Always."""
        scan = lint_lib.scan(self._fixture().path)
        self.assertEqual(
            scan["rules_outside_always"],
            sorted([("feedback-topic.md", "feedback"), ("user-topic.md", "user")]),
        )
        self.assertEqual(
            scan["facts_in_always"],
            sorted([("project-always.md", "project"), ("reference-always.md", "reference")]),
        )

    def test_ac2_type_in_body_not_frontmatter_is_none(self):
        # Covered directly on memory_type() in TestMemoryType; asserted again
        # here through the scan, so a leaf with the string "type:" in its
        # BODY (not frontmatter) is neither flagged as a rule nor a fact.
        d = TwoLevelDir(
            "## Topics\n" + row("topics/t.md", "hook") + "\n",
            topic_files={"t.md": row("../a.md", "hook") + "\n"},
            leaf_files=["a.md"],
            leaf_bodies={
                "a.md": "---\nname: x\n---\n\nprose mentioning\ntype: feedback\nhere.\n"
            },
        )
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["rules_outside_always"], [])
        self.assertEqual(scan["facts_in_always"], [])

    def test_ac3_flat_layout_leaves_both_keys_empty(self):
        d = MemoryDir(
            [("a.md", "hook")],
            bodies={"a.md": "---\ntype: feedback\n---\n\nbody\n"},
        )
        self.addCleanup(d.cleanup)
        scan = lint_lib.scan(d.path)
        self.assertEqual(scan["layout"], "flat")
        self.assertEqual(scan["rules_outside_always"], [])
        self.assertEqual(scan["facts_in_always"], [])

    def test_ac4_findings_one_and_many_item_forms(self):
        one = lint_lib.findings(
            {"readable": True, "rules_outside_always": [("a.md", "feedback")]}
        )
        self.assertEqual(len(one), 1)
        self.assertIn(
            "a.md (type: feedback) is a rule reachable only from a topic — "
            "it never fires there. Move its row to Always, or retype it "
            "reference if CLAUDE.md carries the rule.",
            one,
        )

        many = lint_lib.findings(
            {
                "readable": True,
                "rules_outside_always": [
                    ("a.md", "feedback"), ("b.md", "user"), ("c.md", "feedback"),
                ],
            }
        )
        self.assertEqual(len(many), 1)
        self.assertIn("3 rule memories are reachable only from a topic", many[0])
        self.assertIn(
            "a.md (type: feedback), b.md (type: user), c.md (type: feedback)",
            many[0],
        )

        one_fact = lint_lib.findings(
            {"readable": True, "facts_in_always": [("a.md", "project")]}
        )
        self.assertEqual(len(one_fact), 1)
        self.assertIn(
            "a.md (type: project) is a fact with a row in Always — it is "
            "paid every session. Move its row to its topic, or retype it "
            "feedback if it is a rule.",
            one_fact,
        )

        many_facts = lint_lib.findings(
            {
                "readable": True,
                "facts_in_always": [
                    ("a.md", "project"), ("b.md", "reference"), ("c.md", "project"),
                ],
            }
        )
        self.assertEqual(len(many_facts), 1)
        self.assertIn("3 fact memories have a row in Always", many_facts[0])


_REAL_SKILL_SHIPPED = getattr(lint_lib, "distill_skill_shipped", None)


def _index_of_exact_chars(target):
    """Rows whose index (rows joined by newline, plus a final newline) is
    exactly `target` characters. Hooks stay under the length notice."""
    rows, size, i = [], 0, 0
    while True:
        name = "m%04d.md" % i
        step = len(row(name, "h" * 140)) + 1
        if size + step > target:
            break
        rows.append([name, 140])
        size += step
        i += 1
    remainder = target - size
    for entry in rows:
        add = min(remainder, 40)
        entry[1] += add
        remainder -= add
    assert remainder == 0, remainder
    return [(name, "h" * length) for name, length in rows]


def _bytes_of_exact_chars(target):
    """UTF-8 size of the index `_index_of_exact_chars(target)` builds. Rows
    carry an em dash (3 bytes), so bytes run ahead of chars."""
    rows = _index_of_exact_chars(target)
    return len(("\n".join(row(n, h) for n, h in rows) + "\n").encode("utf-8"))


def _largest_chars_under_bytes(limit):
    """The largest exact-chars index whose UTF-8 size is still under `limit`."""
    chars = limit
    while _bytes_of_exact_chars(chars) >= limit:
        chars -= 1
    return chars


class _DistillConfig(object):
    """A config double: explicit values, never this machine's config."""

    def __init__(self, distill=None, cap=None):
        self.distill = distill
        self.cap = cap

    def get_int(self, _section, key, default=None):
        if key == "max_index_chars" and self.cap is not None:
            return self.cap
        return default

    def get(self, _section, key, default=None):
        if key == "distill_at_chars" and self.distill is not None:
            return self.distill
        return default


class DistillEnv(unittest.TestCase):
    """Sandboxed HOME and claim dir for every test that touches the claim."""

    def setUp(self):
        self.sandbox = tempfile.mkdtemp(prefix="memlint-distill-")
        self.addCleanup(shutil.rmtree, self.sandbox, ignore_errors=True)
        self.claim = os.path.join(self.sandbox, "claim")
        saved = {k: os.environ.get(k) for k in
                 ("HOME", "USERPROFILE", "CLAUDE_INDEX_DISTILL_CLAIM")}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ["HOME"] = self.sandbox
        os.environ["USERPROFILE"] = self.sandbox
        os.environ["CLAUDE_INDEX_DISTILL_CLAIM"] = self.claim
        shipped = mock.patch.object(lint_lib, "distill_skill_shipped",
                                    return_value=True)
        shipped.start()
        self.addCleanup(shipped.stop)

    def memory(self, chars):
        d = MemoryDir(_index_of_exact_chars(chars))
        self.addCleanup(d.cleanup)
        self.assertEqual(lint_lib.scan(d.path)["index_chars"], chars)
        return d

    def write_claim(self, session_id, hours_ago):
        stamp = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=hours_ago))
        os.makedirs(self.claim)
        with open(os.path.join(self.claim, "holder.json"), "w") as fh:
            json.dump({"session_id": session_id,
                       "claimed_at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ")}, fh)


class TestDistillTrigger(DistillEnv):
    START = "MEMORY INDEX NEAR CHARACTER LIMIT \u2014 MEMORY.md is 19500 /"

    def test_b1_startup_and_clear_fire_first(self):
        d = self.memory(19500)
        for source in ("startup", "clear"):
            out = lint_lib.build(d.path, source=source, session_id="me")
            self.assertTrue(out.startswith(self.START), (source, out[:80]))

    def test_b1_exact_text(self):
        d = self.memory(19500)
        out = lint_lib.build(d.path, source="startup", session_id="me")
        first = out.split("\n")[0]
        self.assertEqual(
            first,
            "MEMORY INDEX NEAR CHARACTER LIMIT \u2014 MEMORY.md is 19500 / 24000 chars, "
            "%d bytes (distill at 19500). Past ~25,000 the harness drops rows silently. "
            "In your FIRST reply, tell the user and offer three choices: (1) "
            "make this the distillation session (run "
            "/toolkit-continuity:memory-index-distill); (2) hand off: give a "
            "one-line kickoff for a dedicated session and carry on here; (3) "
            "later. Distillation pauses the other running sessions via "
            "/handover, so it runs in a session of its own." % _bytes_of_exact_chars(19500))

    def test_b2_other_sources_stay_silent(self):
        d = self.memory(19500)
        for source in ("compact", "resume", None):
            out = lint_lib.build(d.path, source=source, session_id="me")
            self.assertNotIn("NEAR CHARACTER LIMIT", out or "", source)

    def test_b3_one_under_is_silent(self):
        # "Under" is measured in bytes now; the paired index one char larger
        # reaches the trigger, so this boundary is the real one.
        chars = _largest_chars_under_bytes(19500)
        d = self.memory(chars)
        self.assertLess(lint_lib.scan(d.path)["index_bytes"], 19500)
        out = lint_lib.build(d.path, source="startup", session_id="me")
        self.assertNotIn("NEAR CHARACTER LIMIT", out or "")
        over = self.memory(chars + 1)
        self.assertGreaterEqual(lint_lib.scan(over.path)["index_bytes"], 19500)
        out = lint_lib.build(over.path, source="startup", session_id="me")
        self.assertIn("NEAR CHARACTER LIMIT", out or "")

    def multibyte_memory(self, chars, min_bytes):
        """An index of exactly `chars` characters, but at least `min_bytes`
        UTF-8 bytes: whole hooks rewritten as the okina (2 bytes each)."""
        rows = _index_of_exact_chars(chars)
        added = 0
        for i, (name, hook) in enumerate(rows):
            if chars + added >= min_bytes:
                break
            rows[i] = (name, "\u02bb" * len(hook))
            added += len(hook)
        d = MemoryDir(rows)
        self.addCleanup(d.cleanup)
        scanned = lint_lib.scan(d.path)
        # Preconditions, asserted: under the trigger in chars, over it in bytes.
        self.assertEqual(scanned["index_chars"], chars)
        self.assertGreaterEqual(scanned["index_bytes"], min_bytes)
        return d

    def test_b12_bytes_over_trigger_fires_though_chars_are_under(self):
        # Claude Code's own nag counts bytes; a chars-only trigger lost to it.
        d = self.multibyte_memory(18000, 19500)
        out = lint_lib.build(d.path, source="startup", session_id="me")
        self.assertTrue((out or "").startswith(
            "MEMORY INDEX NEAR CHARACTER LIMIT \u2014 MEMORY.md is 18000 / 24000 chars, "),
            (out or "")[:120])
        self.assertRegex(lint_lib.report(d.path), r"distill at\s+19500 \(due\)")

    def test_b12_same_chars_all_ascii_stays_silent(self):
        # Control for b12: the identical char count, hooks one byte per char.
        d = self.memory(18000)
        out = lint_lib.build(d.path, source="startup", session_id="me")
        self.assertNotIn("NEAR CHARACTER LIMIT", out or "")
        self.assertLess(_bytes_of_exact_chars(18000), 19500)
        self.assertRegex(lint_lib.report(d.path),
                         r"distill at\s+19500 \(%d to go\)"
                         % (19500 - _bytes_of_exact_chars(18000)))

    def test_b4_other_sessions_live_claim(self):
        d = self.memory(19500)
        self.write_claim("other", 1)
        out = lint_lib.build(d.path, source="startup", session_id="me")
        first = out.split("\n")[0]
        self.assertRegex(
            first,
            r"^MEMORY INDEX NEAR CHARACTER LIMIT \u2014 distillation in progress in another "
            r"session since \d\d:\d\d\. Nothing to do here\. Don't write "
            r"memory until it finishes\.$")

    def test_b4_own_claim_gets_the_normal_text(self):
        d = self.memory(19500)
        self.write_claim("me", 1)
        out = lint_lib.build(d.path, source="startup", session_id="me")
        self.assertTrue(out.startswith(self.START))

    def test_b4_stale_claim_gets_the_normal_text(self):
        d = self.memory(19500)
        self.write_claim("other", 25)
        out = lint_lib.build(d.path, source="startup", session_id="me")
        self.assertTrue(out.startswith(self.START))
        self.assertNotIn("in progress", out)

    def test_b5_threshold_clamped_below_cap(self):
        cfg = _DistillConfig(distill=30000, cap=24000)
        self.assertEqual(lint_lib.distill_at_chars(cfg), 23000)

    def test_b5_default_and_bad_values(self):
        self.assertEqual(lint_lib.distill_at_chars(_DistillConfig()), 19500)
        for bad in (0, -5, "x", True, 1.5):
            self.assertEqual(
                lint_lib.distill_at_chars(_DistillConfig(distill=bad)), 19500, bad)
        self.assertEqual(
            lint_lib.distill_at_chars(_DistillConfig(distill=21000)), 21000)

    def test_b6_report_row(self):
        due = self.memory(19500)
        self.assertRegex(lint_lib.report(due.path),
                         r"distill at\s+19500 \(due\)")
        under = self.memory(18500)
        self.assertRegex(lint_lib.report(under.path),
                         r"distill at\s+19500 \(%d to go\)"
                         % (19500 - _bytes_of_exact_chars(18500)))

    def test_b6_report_row_sits_right_after_index_budget_row(self):
        d = self.memory(19500)
        lines = lint_lib.report(d.path).split("\n")
        i = [n for n, l in enumerate(lines) if l.startswith("index budget")][0]
        self.assertTrue(lines[i + 1].startswith("distill at"), lines[i + 1])

    def test_b11_skill_not_shipped_means_no_block(self):
        d = self.memory(19500)
        with mock.patch.object(lint_lib, "distill_skill_shipped",
                               return_value=False):
            out = lint_lib.build(d.path, source="startup", session_id="me")
        self.assertNotIn("NEAR CHARACTER LIMIT", out or "")

    def test_block_precedes_drift_notice(self):
        d = MemoryDir([("a.md", "x" * 400)] + _index_of_exact_chars(19500)[:-1])
        self.addCleanup(d.cleanup)
        out = lint_lib.build(d.path, source="startup", session_id="me")
        # A precondition, asserted: a fixture that slipped under 19500 would
        # otherwise skip every assertion below and pass vacuously.
        self.assertGreaterEqual(lint_lib.scan(d.path)["index_chars"], 19500)
        self.assertTrue(out.startswith("MEMORY INDEX NEAR CHARACTER LIMIT"))
        self.assertIn("\n\nMEMORY INDEX DRIFT", out)

    def test_live_claim_suppresses_the_drift_notice(self):
        # Same fixture as above, so drift findings exist: with another
        # session's live claim, the notice says "don't write memory" and must
        # not go on to say "compress".
        d = MemoryDir([("a.md", "x" * 400)] + _index_of_exact_chars(19500)[:-1])
        self.addCleanup(d.cleanup)
        self.assertTrue(lint_lib.findings(lint_lib.scan(d.path)))
        self.write_claim("other", 1)
        out = lint_lib.build(d.path, source="startup", session_id="me")
        self.assertIn("distillation in progress", out)
        self.assertNotIn("MEMORY INDEX DRIFT", out)
        self.assertNotIn("Compress", out)

    def test_b5_threshold_stays_positive_on_a_small_cap(self):
        for cap, want in ((1000, 500), (1500, 750), (2000, 1000),
                          (3000, 2000), (1, 1)):
            cfg = _DistillConfig(distill=30000, cap=cap)
            self.assertEqual(lint_lib.distill_at_chars(cfg), want, cap)

    def test_skill_shipped_checks_the_real_path(self):
        # The unpatched function, captured before setUp patches it.
        want = os.path.exists(os.path.join(
            REPO, "skills", "continuity", "memory-index-distill", "SKILL.md"))
        self.assertEqual(_REAL_SKILL_SHIPPED(), want)


class TestDistillExecutable(DistillEnv):
    """B7, through the executable. The shipped-skill gate is real in a
    subprocess, so the test runs a COPY of the tool inside a temp toolkit tree
    that carries (or lacks) the SKILL.md."""

    def _tree(self, with_skill):
        root = tempfile.mkdtemp(prefix="memlint-tree-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        shutil.copytree(os.path.join(REPO, "lib"), os.path.join(root, "lib"),
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
        tool = os.path.join(root, "tools", "memory-lint")
        os.makedirs(tool)
        for name in ("claude-memory-lint", "lint_lib.py"):
            shutil.copy(os.path.join(TOOL_DIR, name), tool)
        if with_skill:
            skill = os.path.join(root, "skills", "continuity",
                                 "memory-index-distill")
            os.makedirs(skill)
            open(os.path.join(skill, "SKILL.md"), "w").write("x\n")
        return os.path.join(tool, "claude-memory-lint")

    def _run(self, hook, payload):
        return subprocess.run(
            [sys.executable, hook], input=json.dumps(payload),
            capture_output=True, text=True, timeout=15,
            env=dict(os.environ, HOME=self.sandbox, USERPROFILE=self.sandbox,
                     CLAUDE_TOOLKIT_CONFIG="/nonexistent.json",
                     CLAUDE_INDEX_DISTILL_CLAIM=self.claim))

    def test_b7_source_gates_the_notice(self):
        d = self.memory(19500)
        claude = os.path.join(self.sandbox, ".claude")
        os.makedirs(claude)
        with open(os.path.join(claude, "settings.json"), "w") as fh:
            json.dump({"autoMemoryDirectory": d.path}, fh)
        hook = self._tree(with_skill=True)
        compact = self._run(hook, {"source": "compact", "session_id": "me"})
        self.assertEqual(compact.returncode, 0)
        self.assertNotIn("NEAR CHARACTER LIMIT", compact.stdout)
        startup = self._run(hook, {"source": "startup", "session_id": "me"})
        self.assertEqual(startup.returncode, 0)
        ctx = json.loads(startup.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("MEMORY INDEX NEAR CHARACTER LIMIT"), ctx[:80])

    def test_b7_no_skill_no_block_through_the_executable(self):
        d = self.memory(19500)
        claude = os.path.join(self.sandbox, ".claude")
        os.makedirs(claude)
        with open(os.path.join(claude, "settings.json"), "w") as fh:
            json.dump({"autoMemoryDirectory": d.path}, fh)
        startup = self._run(self._tree(with_skill=False),
                            {"source": "startup", "session_id": "me"})
        self.assertNotIn("NEAR CHARACTER LIMIT", startup.stdout)


class TestExampleConfigDistill(unittest.TestCase):
    def test_distill_key_documented_with_default(self):
        section = json.load(open(os.path.join(REPO, "config.example.json"),
                                 encoding="utf-8"))["memory_lint"]
        self.assertEqual(section["distill_at_chars"], 19500)
        self.assertIn("//distill_at_chars", section)


class TestAutoMemoryDisabled(unittest.TestCase):
    """No notice in a session that never loaded MEMORY.md.

    With `CLAUDE_CODE_DISABLE_AUTO_MEMORY` set, the harness loads no memory
    index (every `claude -p` child of a live harness runs this way). A notice
    about the index would tell that session to distill a file it cannot see.
    Through the executable, because the hook inherits the session's
    environment, and both ways: a session with auto memory ON must still be
    told about the same drifted index.
    """

    # Read the way the binary reads its env switches (harness-check uses the
    # same set): trimmed, case-insensitive, one of 1/true/yes/on.
    DISABLING = ("1", "true", "TRUE", " yes ", "on")
    NOT_DISABLING = (None, "", "0", "false", "no", "off")

    def setUp(self):
        project = tempfile.mkdtemp(prefix="memlint-proj-")
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)
        mem = os.path.join(project, "memory")
        os.mkdir(mem)
        with open(os.path.join(mem, "a.md"), "w") as fh:
            fh.write("body")
        with open(os.path.join(mem, "MEMORY.md"), "w") as fh:
            fh.write(row("a.md", "x" * 400) + "\n")
        self.transcript = os.path.join(project, "s.jsonl")
        open(self.transcript, "w").close()
        # The session's working directory: a separate, git-free sandbox, so
        # the project and local settings the hook reads are only the test's.
        self.cwd = self._tmp("memlint-cwd-")

    def _tmp(self, prefix):
        path = os.path.realpath(tempfile.mkdtemp(prefix=prefix))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    @staticmethod
    def _write(root, name, text):
        os.makedirs(os.path.join(root, ".claude"), exist_ok=True)
        with open(os.path.join(root, ".claude", name), "w") as fh:
            fh.write(text)

    def _run(self, switch, settings=None, cwd=None, **env_overrides):
        """`settings`: raw text for `<HOME>/.claude/settings.json`, or None.
        `cwd`: the payload's session directory (default: the sandbox)."""
        env = _env(CLAUDE_TOOLKIT_CONFIG="/nonexistent.json", **env_overrides)
        env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
        if switch is not None:
            env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = switch
        if settings is not None:
            home = self._tmp("memlint-home-")
            self._write(home, "settings.json", settings)
            env["HOME"] = home
        return subprocess.run(
            [sys.executable, HOOK],
            input=json.dumps({"transcript_path": self.transcript,
                              "source": "startup", "cwd": cwd or self.cwd}),
            capture_output=True, text=True, timeout=15, env=env,
        )

    def _assert_notice(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertIn("a.md", payload["hookSpecificOutput"]["additionalContext"])

    def _assert_silent(self, proc):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")

    @staticmethod
    def _git(*args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
                       + list(args), check=True, capture_output=True, env=env)

    def test_a_session_with_auto_memory_off_gets_no_notice(self):
        for switch in self.DISABLING:
            with self.subTest(switch=switch):
                proc = self._run(switch)
                self.assertEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout, "")

    def test_a_session_with_auto_memory_on_still_gets_the_notice(self):
        for switch in self.NOT_DISABLING:
            with self.subTest(switch=switch):
                proc = self._run(switch)
                self.assertEqual(proc.returncode, 0)
                payload = json.loads(proc.stdout)
                self.assertIn(
                    "a.md", payload["hookSpecificOutput"]["additionalContext"])

    # The other switch: `autoMemoryEnabled: false` in user settings, read as
    # harness-check reads it (the JSON boolean only). Unreadable settings fail
    # OPEN -- the notice still fires -- because a guess that silences it is the
    # failure this class exists to catch, pointed the other way.
    def test_auto_memory_off_in_user_settings_gets_no_notice(self):
        proc = self._run(None, settings='{"autoMemoryEnabled": false}')
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_other_user_settings_still_get_the_notice(self):
        for settings in ('{"autoMemoryEnabled": true}', '{"autoMemoryEnabled": null}',
                         '{"autoMemoryEnabled": "false"}', '{}', '[false]',
                         '{"autoMemoryEnabled": fal'):
            with self.subTest(settings=settings):
                proc = self._run(None, settings=settings)
                self.assertEqual(proc.returncode, 0)
                payload = json.loads(proc.stdout)
                self.assertIn(
                    "a.md", payload["hookSpecificOutput"]["additionalContext"])

    def test_build_reads_the_switch_in_process_too(self):
        d = MemoryDir([("a.md", "x" * 400)])
        self.addCleanup(d.cleanup)
        with mock.patch.dict(os.environ,
                             {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}):
            self.assertIsNone(lint_lib.build(d.path, source="startup"))
        self.assertIsNotNone(lint_lib.build(d.path, source="startup"))

    # Row 104, read from the binary (2.1.327): the merged `autoMemoryEnabled`
    # decides, user < project < local < flag < managed, and a FALSY env value
    # forces memory on over every setting. The hook silences only when a
    # level it reads says false and none could switch memory on.
    def test_a_falsy_env_value_forces_memory_on_over_settings(self):
        for switch in ("0", "off", " FALSE ", "no"):
            with self.subTest(switch=switch):
                self._assert_notice(self._run(switch, settings='{"autoMemoryEnabled": false}'))

    def test_project_or_local_true_overrides_user_false(self):
        for name in ("settings.json", "settings.local.json"):
            with self.subTest(level=name):
                cwd = self._tmp("memlint-cwd-")
                self._write(cwd, name, '{"autoMemoryEnabled": true}')
                self._assert_notice(self._run(None, settings='{"autoMemoryEnabled": false}', cwd=cwd))

    def test_project_or_local_false_alone_silences(self):
        for name in ("settings.json", "settings.local.json"):
            with self.subTest(level=name):
                cwd = self._tmp("memlint-cwd-")
                self._write(cwd, name, '{"autoMemoryEnabled": false}')
                self._assert_silent(self._run(None, cwd=cwd))

    def test_a_project_or_local_file_that_cannot_be_read_fails_open(self):
        for name in ("settings.json", "settings.local.json"):
            for text in ('{"autoMemoryEnabled": fal', "[false]", '{"autoMemoryEnabled": "false"}'):
                with self.subTest(level=name, text=text):
                    cwd = self._tmp("memlint-cwd-")
                    self._write(cwd, name, text)
                    self._assert_notice(self._run(None, settings='{"autoMemoryEnabled": false}', cwd=cwd))

    # Below a git root, or in a linked worktree, the binary's choice of project
    # and local file turns on checks the hook cannot repeat: a `true` in ANY
    # candidate keeps the notice, a `false` there never silences it, and the
    # user level still decides alone.
    def test_at_a_main_checkouts_root_local_false_silences(self):
        repo = self._tmp("memlint-repo-")
        self._git("init", "-q", repo)
        self._write(repo, "settings.local.json", '{"autoMemoryEnabled": false}')
        self._assert_silent(self._run(None, cwd=repo))

    def test_from_a_subdirectory_the_git_roots_files_count_only_to_keep_the_notice(self):
        repo = self._tmp("memlint-repo-")
        self._git("init", "-q", repo)
        sub = os.path.join(repo, "a", "b")
        os.makedirs(sub)
        self._assert_silent(self._run(None, settings='{"autoMemoryEnabled": false}', cwd=sub))
        for name in ("settings.json", "settings.local.json"):   # Codex F2: project too
            with self.subTest(level=name):
                self._write(repo, name, '{"autoMemoryEnabled": true}')
                self._assert_notice(self._run(None, settings='{"autoMemoryEnabled": false}', cwd=sub))
                self._write(repo, name, '{"autoMemoryEnabled": false}')
                self._assert_notice(self._run(None, cwd=sub))
                os.remove(os.path.join(repo, ".claude", name))

    def test_a_linked_worktree_reads_the_main_checkouts_files_to_keep_the_notice(self):
        main = self._tmp("memlint-main-")
        self._git("init", "-q", main)
        self._git("-C", main, "commit", "-q", "--allow-empty", "-m", "c")
        worktree = os.path.join(self._tmp("memlint-wts-"), "wt")
        self._git("-C", main, "worktree", "add", "-q", worktree)
        self._assert_silent(self._run(None, settings='{"autoMemoryEnabled": false}', cwd=worktree))
        self._write(main, "settings.local.json", '{"autoMemoryEnabled": true}')
        self._assert_notice(self._run(None, settings='{"autoMemoryEnabled": false}', cwd=worktree))
        self._write(main, "settings.local.json", '{"autoMemoryEnabled": false}')
        self._assert_notice(self._run(None, cwd=worktree))

    def test_below_a_root_or_in_a_worktree_a_false_in_cwd_itself_keeps_the_notice(self):
        # The binary may read the git root's (or main checkout's) local file
        # instead of this one, so a false here proves nothing.
        repo = self._tmp("memlint-repo-")
        self._git("init", "-q", repo)
        self._git("-C", repo, "commit", "-q", "--allow-empty", "-m", "c")
        sub = os.path.join(repo, "sub")
        worktree = os.path.join(self._tmp("memlint-wts-"), "wt")
        self._git("-C", repo, "worktree", "add", "-q", worktree)
        os.makedirs(sub)
        for cwd in (sub, worktree):
            for name in ("settings.json", "settings.local.json"):
                with self.subTest(cwd=os.path.basename(cwd), level=name):
                    self._write(cwd, name, '{"autoMemoryEnabled": false}')
                    self._assert_notice(self._run(None, cwd=cwd))
                    os.remove(os.path.join(cwd, ".claude", name))

    def test_an_unreadable_git_file_fails_open(self):
        cwd = self._tmp("memlint-cwd-")
        with open(os.path.join(cwd, ".git"), "w") as fh:
            fh.write("not a gitdir line")
        self._assert_notice(self._run(None, settings='{"autoMemoryEnabled": false}', cwd=cwd))

    def test_claude_config_dir_replaces_the_home_settings(self):
        # Codex F1: with CLAUDE_CONFIG_DIR set the binary never reads
        # ~/.claude/settings.json, so a stale false there must not silence.
        config_dir = self._tmp("memlint-config-")
        self._assert_notice(self._run(None, settings='{"autoMemoryEnabled": false}',
                                      CLAUDE_CONFIG_DIR=config_dir))

    def test_claude_config_dir_settings_count(self):
        config_dir = self._tmp("memlint-config-")
        self._write(config_dir, "settings.json", '{"autoMemoryEnabled": false}')
        os.rename(os.path.join(config_dir, ".claude", "settings.json"),
                  os.path.join(config_dir, "settings.json"))
        self._assert_silent(self._run(None, CLAUDE_CONFIG_DIR=config_dir))
        with open(os.path.join(config_dir, "settings.json"), "w") as fh:
            fh.write('{"autoMemoryEnabled": true}')
        self._assert_notice(self._run(None, settings='{"autoMemoryEnabled": false}',
                                      CLAUDE_CONFIG_DIR=config_dir))


if __name__ == "__main__":
    unittest.main(verbosity=2)
