"""Tests for the tooling-digest SessionStart hook.

Hermetic: every test writes its own TOOLING.md and its own config into a temp
directory. Nothing here reads the real map, whose contents change whenever
someone corrects a row and would make these pass or fail depending on the day.

The executable is exercised through subprocess, not just the lib. A sibling
tool shipped 93 green unit tests with zero lines of any hook executed; the
suite stayed green through two reintroduced bugs.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))

sys.path.insert(0, TOOL_DIR)
sys.path.insert(0, os.path.join(REPO, "lib"))
import digest_lib

HOOK = os.path.join(TOOL_DIR, "claude-tooling-digest")

BLOCK = "Secrets    macOS keychain\nCLI        no gcloud"
DOC = "# Map\n\nintro\n\n%s\n%s\n%s\n\ntail\n" % (
    digest_lib.START_MARKER,
    BLOCK,
    digest_lib.END_MARKER,
)


class FakeConfig:
    """Stands in for toolkit_config with a dict, so tests need no real file."""

    def __init__(self, data=None, root=None):
        self.data = data or {}
        self.root = root

    def get(self, *keys, default=None):
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def get_path(self, *keys, default=None):
        return self.get(*keys, default=default)

    def get_int(self, *keys, default=0):
        value = self.get(*keys, default=default)
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def workspace_root(self):
        return self.root


class TestExtract(unittest.TestCase):
    """Extraction is where a mistake would leak a whole document into context."""

    def test_pulls_the_block_between_markers(self):
        self.assertEqual(digest_lib.extract(DOC), BLOCK)

    def test_missing_markers_yield_nothing_not_the_whole_file(self):
        # The load-bearing case. A renamed marker must degrade to no output,
        # never to injecting a 300-line reference doc every session.
        self.assertIsNone(digest_lib.extract("# Map\n\nplenty of prose\n"))

    def test_start_without_end_yields_nothing(self):
        text = "%s\nbody but never closed\n" % digest_lib.START_MARKER
        self.assertIsNone(digest_lib.extract(text))

    def test_empty_block_is_treated_as_absent(self):
        text = "%s\n   \n%s" % (digest_lib.START_MARKER, digest_lib.END_MARKER)
        self.assertIsNone(digest_lib.extract(text))

    def test_empty_and_none_input_are_safe(self):
        self.assertIsNone(digest_lib.extract(""))
        self.assertIsNone(digest_lib.extract(None))


class TestCapBlock(unittest.TestCase):
    def test_short_block_is_untouched(self):
        self.assertEqual(digest_lib.cap_block("a\nb", 60), "a\nb")

    def test_an_over_cap_block_leaks_NOT_ONE_LINE_of_its_content(self):
        """The whole point of M3, and the only test that kills a revert.

        Until 2026-09-14 this returned `lines[:limit]` plus a note, and that
        shape is what let the live map bleed its tail for days: a
        trimmed block reads exactly like a complete one. Asserting the notice
        is present does NOT catch a revert — the old code printed a notice
        too. Asserting that no content survives does.
        """
        body = "\n".join("SENTINEL_LINE_%d" % i for i in range(100))
        out = digest_lib.cap_block(body, 10)
        for i in range(100):
            self.assertNotIn("SENTINEL_LINE_%d" % i, out)

    def test_an_over_cap_block_reports_BOTH_numbers(self):
        # n/limit, per the ruling. Either number alone is unactionable: the
        # limit without the count hides how far over, and the count without
        # the limit hides what to aim for.
        body = "\n".join("line%d" % i for i in range(84))
        out = digest_lib.cap_block(body, 80)
        self.assertIn("84/80", out)

    def test_the_notice_does_not_advertise_raising_the_cap(self):
        """Deliberate, and the reason is in `cap_block`'s docstring.

        Raising the cap is the treadmill this mission exists to stop. A notice
        that names the config key is a notice that gets escaped rather than
        actioned, so the only instruction offered is to trim.
        """
        body = "\n".join("line%d" % i for i in range(100))
        out = digest_lib.cap_block(body, 10)
        self.assertNotIn("max_lines", out)
        self.assertNotIn("tooling_digest", out)

    def test_the_notice_SAYS_the_block_was_not_injected(self):
        """The notice's persuasive content is load-bearing and was unasserted.

        Every other test here pins a MECHANICAL property — no content leaks,
        both numbers, the label, the filename, the length, no config key. All
        of them stay green if the notice is cut to
        `DIGEST OVER LIMIT — 70/60 lines.` and nothing else. But the mechanical
        half only stops the WRONG map reaching the session; this sentence is
        what stops the session proceeding as though it had a map at all.

        Reword freely — the claim has to survive, not the phrasing.
        """
        body = "\n".join("line%d" % i for i in range(100))
        out = digest_lib.cap_block(body, 10)
        self.assertIn("NOT injected", out)
        self.assertIn("NO tooling map", out)

    def test_the_notice_TELLS_the_reader_not_to_answer_from_memory(self):
        """The other half, and the one that is actually the point.

        A session told only that a cap was exceeded will answer tooling
        questions from whatever it remembers of the map — which is the failure
        the withholding exists to prevent, arriving by a different route. The
        instruction is the mechanism; the numbers are just diagnostics.
        """
        body = "\n".join("line%d" % i for i in range(100))
        out = digest_lib.cap_block(body, 10)
        self.assertIn("Look nothing up from memory", out)

    def test_exactly_at_the_cap_the_whole_block_is_injected(self):
        # Boundary, low side. `<=` not `<`: a block sized exactly to its
        # budget is a block that fits, and refusing it would make the cap
        # one line tighter than every document says it is.
        body = "\n".join("line%d" % i for i in range(10))
        self.assertEqual(digest_lib.cap_block(body, 10), body)

    def test_one_line_over_the_cap_withholds_everything(self):
        # Boundary, high side. Paired with the test above, these two pin the
        # comparison itself — flipping `<=` to `<` fails one or the other.
        body = "\n".join("line%d" % i for i in range(11))
        out = digest_lib.cap_block(body, 10)
        self.assertIn("11/10", out)
        self.assertNotIn("line0", out)


class TestStaleness(unittest.TestCase):
    def test_threshold_of_zero_disables_the_check(self):
        with tempfile.NamedTemporaryFile(suffix=".md") as handle:
            self.assertFalse(digest_lib.is_stale(handle.name, 0))

    def test_fresh_file_is_not_stale(self):
        with tempfile.NamedTemporaryFile(suffix=".md") as handle:
            self.assertFalse(digest_lib.is_stale(handle.name, 90))

    def test_old_file_is_stale(self):
        with tempfile.NamedTemporaryFile(suffix=".md") as handle:
            old = time.time() - (100 * 86400)
            os.utime(handle.name, (old, old))
            self.assertTrue(digest_lib.is_stale(handle.name, 90))
            self.assertGreaterEqual(
                digest_lib.days_since_modified(handle.name), 99
            )

    def test_unknowable_mtime_is_not_staleness(self):
        # Failing toward "fresh" keeps the hook from nagging about a file it
        # cannot actually assess.
        self.assertFalse(digest_lib.is_stale("/nonexistent/TOOLING.md", 1))
        self.assertIsNone(digest_lib.days_since_modified("/nonexistent/x.md"))


class TestResolvePath(unittest.TestCase):
    def test_explicit_path_wins(self):
        config = FakeConfig({"tooling_digest": {"path": "/tmp/custom.md"}},
                            root="/tmp/root")
        self.assertEqual(digest_lib.resolve_path(config), "/tmp/custom.md")

    def test_falls_back_to_workspace_root(self):
        # This is what makes the default portable: a user who set a
        # workspace root gets the feature without naming a personal path.
        config = FakeConfig(root="/tmp/root")
        self.assertEqual(
            digest_lib.resolve_path(config), "/tmp/root/TOOLING.md"
        )

    def test_no_config_at_all_resolves_to_nothing(self):
        self.assertIsNone(digest_lib.resolve_path(FakeConfig()))


class TestBuild(unittest.TestCase):
    """The whole pipeline, which is what the hook actually calls."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "TOOLING.md")

    def write(self, text):
        with open(self.path, "w") as handle:
            handle.write(text)

    def test_happy_path_injects_the_block_and_names_the_source(self):
        self.write(DOC)
        out = digest_lib.build(FakeConfig(root=self.tmp))
        self.assertIn(BLOCK, out)
        self.assertIn(self.path, out)

    def test_disabled_produces_nothing(self):
        self.write(DOC)
        config = FakeConfig(
            {"tooling_digest": {"enabled": False}}, root=self.tmp
        )
        self.assertEqual(digest_lib.build(config), "")

    def test_missing_file_says_so_and_names_the_path(self):
        # P14: an enabled digest with a configured-but-missing file is not an
        # opt-out. It once injected nothing for sixteen days and said
        # nothing. The opt-out is `enabled: false`.
        out = digest_lib.build(FakeConfig(root=self.tmp))
        self.assertIn(self.path, out)
        self.assertIn("does not exist", out)

    def test_missing_file_notice_names_a_runnable_disable_command(self):
        # A bare "setup_config.py --update --disable tooling_digest" only runs
        # from inside lib/. The notice must name a real, pasteable command
        # whose repo path actually holds that script.
        # The command is read as the shell reads it: the repo path is quoted,
        # and a checkout path with a space is still one argument.
        out = digest_lib.build(FakeConfig(root=self.tmp))
        match = re.search(r"turn this off with (.+?)\.\]", out)
        self.assertIsNotNone(match, out)
        scripts = [arg for arg in shlex.split(match.group(1))
                   if arg.endswith("/lib/setup_config.py")]
        self.assertEqual(len(scripts), 1, match.group(1))
        self.assertTrue(os.path.exists(scripts[0]), scripts[0])

    def test_unconfigured_machine_says_so(self):
        # The same contract, when nothing resolves a path at all (no
        # workspace_root, no explicit path).
        out = digest_lib.build(FakeConfig())
        self.assertIn("no file is configured", out)

    def test_disabled_and_missing_is_still_silent(self):
        # The one opt-out left standing: `enabled: false` stays quiet even
        # with no file at all.
        config = FakeConfig({"tooling_digest": {"enabled": False}})
        self.assertEqual(digest_lib.build(config), "")

    def test_disable_command_runs_with_a_space_in_the_config_path(self):
        # A config path with a space in it (a macOS username with a space is
        # common) must not split into two tokens: the printed command has to
        # run as written, and actually turn the digest off.
        space_dir = os.path.join(self.tmp, "has space")
        os.makedirs(space_dir)
        config_path = os.path.join(space_dir, "config.json")
        with open(config_path, "w") as handle:
            json.dump({"tooling_digest": {}}, handle)

        old_env = os.environ.get("CLAUDE_TOOLKIT_CONFIG")
        os.environ["CLAUDE_TOOLKIT_CONFIG"] = config_path

        def _restore():
            if old_env is None:
                os.environ.pop("CLAUDE_TOOLKIT_CONFIG", None)
            else:
                os.environ["CLAUDE_TOOLKIT_CONFIG"] = old_env
        self.addCleanup(_restore)

        out = digest_lib.build(FakeConfig(root=self.tmp))
        match = re.search(r"turn this off with (.+?)\.\]", out)
        self.assertIsNotNone(match, out)
        proc = subprocess.run(shlex.split(match.group(1)), capture_output=True,
                              text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(config_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["tooling_digest"]["enabled"], False)

    def test_disable_command_works_on_a_machine_with_no_config_file(self):
        # The machine most likely to see this notice has nothing set up, and
        # `--update` refuses there ("run --write first", exit 3). The printed
        # command must still turn the digest off, run as written by a shell,
        # with a space in the path.
        config_path = os.path.join(self.tmp, "has space", "cfg", "config.json")
        old_env = os.environ.get("CLAUDE_TOOLKIT_CONFIG")
        os.environ["CLAUDE_TOOLKIT_CONFIG"] = config_path

        def _restore():
            if old_env is None:
                os.environ.pop("CLAUDE_TOOLKIT_CONFIG", None)
            else:
                os.environ["CLAUDE_TOOLKIT_CONFIG"] = old_env
        self.addCleanup(_restore)

        self.assertFalse(os.path.exists(config_path))
        out = digest_lib.build(FakeConfig(root=self.tmp))
        match = re.search(r"turn this off with (.+?)\.\]", out)
        self.assertIsNotNone(match, out)
        proc = subprocess.run(["/bin/sh", "-c", match.group(1)], capture_output=True,
                              text=True, stdin=subprocess.DEVNULL,
                              env=dict(os.environ, HOME=self.tmp))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(config_path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertIs(data["tooling_digest"]["enabled"], False)

    def test_missing_disabled_empty_and_over_limit_are_distinct(self):
        # P14: four outcomes, four different outputs. A pipeline that folded
        # any two together (the old silent "missing", say) fails here.
        outputs = {}
        outputs["missing"] = digest_lib.build(FakeConfig(root=self.tmp))
        self.write(DOC)
        outputs["disabled"] = digest_lib.build(FakeConfig(
            {"tooling_digest": {"enabled": False}}, root=self.tmp))
        self.write("%s\n\n%s\n" % (digest_lib.START_MARKER, digest_lib.END_MARKER))
        outputs["empty"] = digest_lib.build(FakeConfig(root=self.tmp))
        self.write("%s\n%s\n%s\n" % (digest_lib.START_MARKER,
                                     "\n".join("r%d" % i for i in range(9)),
                                     digest_lib.END_MARKER))
        outputs["over"] = digest_lib.build(FakeConfig(
            {"tooling_digest": {"max_lines": 3}}, root=self.tmp))
        self.assertEqual(outputs["disabled"], "")
        self.assertIn("does not exist", outputs["missing"])
        self.assertIn("is empty", outputs["empty"])
        self.assertIn("OVER LIMIT", outputs["over"])
        self.assertEqual(len(set(outputs.values())), 4, outputs)

    def test_file_without_markers_reports_rather_than_going_quiet(self):
        # Silence here would look identical to everything working, which is
        # the exact failure this feature exists to remove.
        self.write("# Map\n\nno markers anywhere\n")
        out = digest_lib.build(FakeConfig(root=self.tmp))
        self.assertIn("no", out.lower())
        self.assertIn(self.path, out)
        self.assertNotIn("no markers anywhere", out)

    def test_stale_file_is_flagged_alongside_the_digest(self):
        self.write(DOC)
        old = time.time() - (200 * 86400)
        os.utime(self.path, (old, old))
        out = digest_lib.build(FakeConfig(root=self.tmp))
        self.assertIn(BLOCK, out)
        self.assertIn("unverified", out)

    def test_fresh_file_carries_no_staleness_warning(self):
        self.write(DOC)
        out = digest_lib.build(FakeConfig(root=self.tmp))
        self.assertNotIn("unverified", out)

    def test_oversized_block_is_withheld_whole_by_config(self):
        body = "\n".join("line%d" % i for i in range(100))
        self.write("%s\n%s\n%s" % (
            digest_lib.START_MARKER, body, digest_lib.END_MARKER))
        config = FakeConfig(
            {"tooling_digest": {"max_lines": 5}}, root=self.tmp
        )
        out = digest_lib.build(config)
        self.assertIn("OVER LIMIT", out)
        self.assertIn("100/5", out)
        # The first five lines are the ones the OLD code kept, so they are the
        # ones a revert would put back. Checking `line99` alone passed under
        # both contracts and proved nothing.
        for i in range(5):
            self.assertNotIn("line%d" % i, out)


class TestHookExecutable(unittest.TestCase):
    """Through subprocess, the way Claude Code actually runs it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "TOOLING.md")
        with open(self.path, "w") as handle:
            handle.write(DOC)
        self.config_path = os.path.join(self.tmp, "config.json")

    def run_hook(self, config=None, payload='{"hook_event_name":"SessionStart"}'):
        env = dict(os.environ)
        if config is None:
            env["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent-config.json"
        else:
            with open(self.config_path, "w") as handle:
                json.dump(config, handle)
            env["CLAUDE_TOOLKIT_CONFIG"] = self.config_path
        return subprocess.run(
            [sys.executable, HOOK], input=payload, env=env,
            capture_output=True, text=True, timeout=20,
        )

    def test_emits_the_digest_when_pointed_at_a_map(self):
        result = self.run_hook({"workspace_root": self.tmp})
        self.assertEqual(result.returncode, 0)
        self.assertIn("no gcloud", result.stdout)

    def test_prints_a_notice_with_no_config_at_all(self):
        # What a fresh clone looks like: enabled by default (absent means
        # on), nothing resolvable. It says so rather than looking identical
        # to everything working (P14).
        result = self.run_hook()
        self.assertEqual(result.returncode, 0)
        self.assertIn("no file is configured", result.stdout)

    def test_disabled_with_no_map_exits_zero_and_silent(self):
        result = self.run_hook({"tooling_digest": {"enabled": False}})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_exits_zero_on_garbage_stdin(self):
        result = self.run_hook({"workspace_root": self.tmp}, payload="not json")
        self.assertEqual(result.returncode, 0)
        self.assertIn("no gcloud", result.stdout)

    def test_exits_zero_on_empty_stdin(self):
        result = self.run_hook({"workspace_root": self.tmp}, payload="")
        self.assertEqual(result.returncode, 0)

    def test_explicit_path_config_reaches_the_executable(self):
        other = os.path.join(self.tmp, "elsewhere.md")
        with open(other, "w") as handle:
            handle.write(DOC.replace("no gcloud", "SENTINEL_VALUE"))
        result = self.run_hook({"tooling_digest": {"path": other}})
        self.assertEqual(result.returncode, 0)
        self.assertIn("SENTINEL_VALUE", result.stdout)


class TestMissingSource(unittest.TestCase):
    """`missing_source` names exactly the case `build()` renders a missing
    notice for, and stays quiet in every other case: an installer's config
    health check reads it to decide whether to warn."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "TOOLING.md")

    def test_returns_the_path_when_the_file_is_missing(self):
        self.assertEqual(
            digest_lib.missing_source(FakeConfig(root=self.tmp)), self.path)

    def test_returns_the_unconfigured_marker_when_nothing_resolves(self):
        self.assertEqual(digest_lib.missing_source(FakeConfig()), "<unconfigured>")

    def test_returns_none_when_the_file_exists(self):
        with open(self.path, "w") as handle:
            handle.write(DOC)
        self.assertIsNone(digest_lib.missing_source(FakeConfig(root=self.tmp)))

    def test_returns_none_when_disabled(self):
        config = FakeConfig({"tooling_digest": {"enabled": False}}, root=self.tmp)
        self.assertIsNone(digest_lib.missing_source(config))


class TestCoreMarkerAlias(unittest.TestCase):
    def test_core_markers_are_accepted(self):
        text = "intro\n<!-- CORE:START -->\nrow one\n<!-- CORE:END -->\ntail\n"
        self.assertEqual(digest_lib.extract(text), "row one")

    def test_digest_markers_still_work(self):
        text = "intro\n<!-- DIGEST:START -->\nrow one\n<!-- DIGEST:END -->\ntail\n"
        self.assertEqual(digest_lib.extract(text), "row one")

    def test_markers_are_re_exported_for_existing_callers(self):
        self.assertEqual(digest_lib.START_MARKER, "<!-- DIGEST:START -->")
        self.assertEqual(digest_lib.END_MARKER, "<!-- DIGEST:END -->")


class TestCapBlockLabel(unittest.TestCase):
    def test_label_defaults_to_digest(self):
        # C13: the live file carries DIGEST markers. A default of "Core" would
        # rename a diagnostic that ships into every session, pointing the reader
        # at markers the file does not contain.
        body = "\n".join("line %d" % i for i in range(10))
        out = digest_lib.cap_block(body, 3)
        self.assertIn("DIGEST OVER LIMIT", out)
        self.assertNotIn("Core", out)

    def test_label_is_honoured_when_passed(self):
        body = "\n".join("line %d" % i for i in range(10))
        self.assertIn("CORE OVER LIMIT", digest_lib.cap_block(body, 3,
                                                           label="CORE"))

    def test_the_notice_is_short_enough_to_be_read(self):
        """A withheld map is only loud if the replacement is not itself a wall.

        Pins the shape rather than the wording: the notice replaces the block,
        so a notice that grew to block size would reintroduce the cost the cap
        exists to control.
        """
        body = "\n".join("line %d" % i for i in range(500))
        self.assertLessEqual(len(digest_lib.cap_block(body, 3).splitlines()), 4)

    def test_a_body_within_the_limit_is_returned_unchanged(self):
        body = "one\ntwo"
        self.assertEqual(digest_lib.cap_block(body, 5), body)


class TestBuildLabelsFromTheMarkersPresent(TestHookExecutable):
    """The wiring, not the predicate.

    `marker_label` being correct proves nothing about `build` calling it. This
    is the class that fails if the label is hardcoded anywhere.

    Subclasses TestHookExecutable for its `run_hook` and its temp dir. That
    helper already pins CLAUDE_TOOLKIT_CONFIG per call (C16); do not reinvent a
    second subprocess runner beside it.
    """

    def _rewrite_map(self, start, end):
        rows = "\n".join("row %d" % i for i in range(40))
        with open(self.path, "w") as handle:
            handle.write("%s\n%s\n%s\n" % (start, rows, end))

    def test_a_digest_marked_file_says_digest(self):
        self._rewrite_map("<!-- DIGEST:START -->", "<!-- DIGEST:END -->")
        out = self.run_hook({"tooling_digest": {"path": self.path,
                                                "max_lines": 5,
                                                "stale_after_days": 0}}).stdout
        self.assertIn("DIGEST OVER LIMIT — 40/5", out)

    def test_a_core_marked_file_says_core(self):
        self._rewrite_map("<!-- CORE:START -->", "<!-- CORE:END -->")
        out = self.run_hook({"tooling_digest": {"path": self.path,
                                                "max_lines": 5,
                                                "stale_after_days": 0}}).stdout
        self.assertIn("CORE OVER LIMIT — 40/5", out)

    def test_THROUGH_THE_HOOK_an_over_cap_map_injects_no_content(self):
        """The wiring, not the predicate — and the one that matters most.

        `cap_block` being correct proves nothing about `build` still calling it
        on the path the hook actually takes. A sibling tool shipped 93 green
        unit tests with zero lines of any hook executed.
        """
        self._rewrite_map("<!-- DIGEST:START -->", "<!-- DIGEST:END -->")
        result = self.run_hook({"tooling_digest": {"path": self.path,
                                                   "max_lines": 5,
                                                   "stale_after_days": 0}})
        self.assertEqual(result.returncode, 0)
        self.assertIn("OVER LIMIT", result.stdout)
        for i in range(40):
            self.assertNotIn("row %d" % i, result.stdout)

    def test_the_notice_names_the_FILE_the_map_actually_lives_in(self):
        """`tooling_digest.path` may name any file, and this is all the reader
        gets. Hardcoding `TOOLING.md` here sends the one person who needs the
        instruction looking for a file that does not exist on their machine.
        """
        other = os.path.join(self.tmp, "MY-STACK.md")
        rows = "\n".join("row %d" % i for i in range(40))
        with open(other, "w") as handle:
            handle.write("<!-- DIGEST:START -->\n%s\n<!-- DIGEST:END -->\n"
                         % rows)
        out = self.run_hook({"tooling_digest": {"path": other,
                                                "max_lines": 5,
                                                "stale_after_days": 0}}).stdout
        self.assertIn("open MY-STACK.md", out)
        self.assertNotIn("open TOOLING.md", out)

    def test_BOTH_notices_in_one_session_name_the_SAME_file(self):
        """A stale map that is also over cap emits two notices at once.

        They are rendered by different functions, so a hardcoded name in
        either contradicts the other in the one session that shows both —
        telling the reader to open `MY-STACK.md` on one line and reporting on
        `TOOLING.md` on the next.
        """
        other = os.path.join(self.tmp, "MY-STACK.md")
        rows = "\n".join("row %d" % i for i in range(40))
        with open(other, "w") as handle:
            handle.write("<!-- DIGEST:START -->\n%s\n<!-- DIGEST:END -->\n"
                         % rows)
        old = time.time() - (200 * 86400)
        os.utime(other, (old, old))
        out = self.run_hook({"tooling_digest": {"path": other,
                                                "max_lines": 5,
                                                "stale_after_days": 90}}).stdout
        self.assertIn("OVER LIMIT", out)
        self.assertIn("has not been edited", out)
        self.assertNotIn("TOOLING.md", out)
        self.assertEqual(out.count("MY-STACK.md"), 3,
                         "both notices and the Source line must name it")

    def test_THROUGH_THE_HOOK_a_map_under_cap_still_injects_normally(self):
        """The positive control.

        Without it, breaking `build` so it injects nothing at all would pass
        every assertion above — a hook that always withholds looks identical
        to a hook that withholds correctly.
        """
        self._rewrite_map("<!-- DIGEST:START -->", "<!-- DIGEST:END -->")
        result = self.run_hook({"tooling_digest": {"path": self.path,
                                                   "max_lines": 80,
                                                   "stale_after_days": 0}})
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("OVER LIMIT", result.stdout)
        self.assertIn("row 39", result.stdout)

    def test_a_broken_core_marked_file_does_not_say_add_DIGEST_back(self):
        # C13 on the sibling diagnostic. render_broken fires exactly when the
        # digest has stopped working, so an instruction naming markers the file
        # does not carry is at its most expensive here.
        with open(self.path, "w") as handle:
            handle.write("intro\n<!-- CORE:START -->\nrow one\nno end marker\n")
        out = self.run_hook({"tooling_digest": {"path": self.path,
                                                "stale_after_days": 0}}).stdout
        self.assertIn("no tooling digest was injected", out)
        self.assertIn("CORE:START", out)          # names the pair the file has
        self.assertIn("either pair is accepted", out)

    def test_an_empty_block_is_reported_as_empty_not_as_missing(self):
        with open(self.path, "w") as handle:
            handle.write("<!-- CORE:START -->\n   \n<!-- CORE:END -->\n")
        out = self.run_hook({"tooling_digest": {"path": self.path,
                                                "stale_after_days": 0}}).stdout
        self.assertIn("empty", out)
        self.assertNotIn("Add one pair back", out)

    def test_a_file_with_two_blocks_says_so(self):
        # The earliest pair wins deterministically, but the file is still
        # ambiguous and a silent pick is what this whole design indicts.
        with open(self.path, "w") as handle:
            handle.write("<!-- DIGEST:START -->\nreal\n<!-- DIGEST:END -->\n"
                         "<!-- CORE:START -->\nstray\n<!-- CORE:END -->\n")
        out = self.run_hook({"tooling_digest": {"path": self.path,
                                                "stale_after_days": 0}}).stdout
        self.assertIn("real", out)
        self.assertNotIn("stray", out)
        self.assertIn("2 marker blocks", out)

if __name__ == "__main__":
    unittest.main(verbosity=2)
