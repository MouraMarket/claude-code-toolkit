"""Tests for distill_guard.py, the mtime/size guard behind memory-distill.

Run through subprocess against a real temporary directory, per the sibling
convention (spec-archive's test_cli.py, thread-handover's test_deliver_hook.py
redirect HOME the same way). Nothing here ever touches this machine's real
~/.claude or any real memory/ directory -- $HOME is always a tempdir, and the
"memory" tree the guard operates on is a second, separate tempdir under it.
"""
import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
GUARD = os.path.join(SCRIPTS_DIR, "distill_guard.py")

sys.path.insert(0, SCRIPTS_DIR)
import distill_guard  # noqa: E402

ORIGINAL_BODY = "incident one happened on 2026-01-01 and cost an afternoon.\n"
DISTILLED_BODY = "## Rule\n\nDo the thing that would have prevented incident one.\n"


class DistillGuardTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = self._tmp.name
        # $HOME is a sandbox distinct from the "memory" tree below, so a test
        # that accidentally wrote under either would be caught immediately by
        # the other's absence.
        self.home = os.path.join(tmp, "home")
        os.makedirs(self.home)
        self.mem_dir = os.path.join(tmp, "workspace", "memory")
        os.makedirs(self.mem_dir)
        self.target = os.path.join(self.mem_dir, "example.md")
        self.write(self.target, self.fact_file(ORIGINAL_BODY))

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def fact_file(body):
        return "---\nname: example\ntype: user\n---\n\n%s" % body

    @staticmethod
    def write(path, content):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    @staticmethod
    def read(path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def stage(self, body=DISTILLED_BODY, name="staged.md"):
        path = os.path.join(self._tmp.name, name)
        self.write(path, body)
        return path

    def run_guard(self, *args):
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home,
                   CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        return subprocess.run([sys.executable, GUARD, *args], env=env,
                              capture_output=True, text=True, timeout=20)

    def stamp_token(self, target=None):
        """Run `stamp` and return the token it printed on its own last line."""
        proc = self.run_guard("stamp", target or self.target)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip().splitlines()[-1]

    def history_path(self):
        return os.path.join(self.mem_dir, "history", "example.history.md")


class TestStampStdoutIsCaptureSafe(DistillGuardTest):

    def test_stamp_puts_only_the_token_on_stdout(self):
        # So `TOKEN=$(... stamp <target>)` is safe. The confirmation line goes
        # to stderr; if it returns to stdout, every documented capture idiom
        # silently captures two lines and cmd_install aborts on a token that
        # was never malformed.
        proc = self.run_guard("stamp", self.target)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), distill_guard.fingerprint(self.target))
        self.assertIn("stamped", proc.stderr)


class TestCleanInstall(DistillGuardTest):

    def test_a_clean_install_succeeds(self):
        token = self.stamp_token()
        staged = self.stage()
        install_proc = self.run_guard("install", self.target, staged, token)
        self.assertEqual(install_proc.returncode, 0, install_proc.stderr)
        self.assertEqual(self.read(self.target), DISTILLED_BODY)

    def test_history_gets_the_pre_edit_content_on_a_successful_install(self):
        token = self.stamp_token()
        staged = self.stage()
        proc = self.run_guard("install", self.target, staged, token)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        hist = self.read(self.history_path())
        self.assertIn(ORIGINAL_BODY.strip(), hist)
        self.assertNotIn(DISTILLED_BODY.strip(), hist)


class TestTouchedTargetAborts(DistillGuardTest):

    def test_a_touched_target_aborts_with_exit_1(self):
        token = self.stamp_token()
        # Simulate a peer session writing to the target between the stamp
        # and the install.
        self.write(self.target, self.fact_file(ORIGINAL_BODY + "a peer wrote this line\n"))

        proc = self.run_guard("install", self.target, self.stage(), token)
        self.assertEqual(proc.returncode, 1)

    def test_the_target_is_unclobbered_after_an_abort(self):
        token = self.stamp_token()
        touched = self.fact_file(ORIGINAL_BODY + "a peer wrote this line\n")
        self.write(self.target, touched)

        self.run_guard("install", self.target, self.stage(), token)
        self.assertEqual(self.read(self.target), touched)

    def test_the_staged_file_is_not_consumed_after_an_abort(self):
        token = self.stamp_token()
        self.write(self.target, self.fact_file(ORIGINAL_BODY + "a peer wrote this line\n"))

        staged = self.stage()
        self.run_guard("install", self.target, staged, token)
        self.assertTrue(os.path.isfile(staged), "staged file was moved despite the abort")
        self.assertEqual(self.read(staged), DISTILLED_BODY)


class TestTheTokenIsPerReaderNotPerPath(DistillGuardTest):
    """The stamp must answer "changed since *I* read it", not "since anybody
    last stamped it".

    The first cut kept stamps in a shared `~/.claude/memory-distill-stamps.json`
    keyed by target path, so a peer session stamping the same file silently
    re-armed this session's pending install and the guard reported `ok` while
    destroying the peer's write. These tests pin the property that removed it.
    """

    def test_a_peer_stamp_after_a_peer_write_does_not_rearm_a_stale_install(self):
        mine = self.stamp_token()                      # session A reads the file
        peer_text = self.fact_file(ORIGINAL_BODY + "a peer added a whole new rule\n")
        self.write(self.target, peer_text)             # session B writes
        self.stamp_token()                             # session B stamps it too

        # Session A now installs the distillation it made from the OLD content.
        proc = self.run_guard("install", self.target, self.stage(), mine)
        self.assertEqual(proc.returncode, 1,
                         "a peer's stamp re-armed a stale install")
        self.assertEqual(self.read(self.target), peer_text,
                         "the peer's write was overwritten")

    def test_no_shared_state_file_is_written_anywhere(self):
        # G7, strengthened: the guard used to write a shared stamps file under
        # ~/.claude. There is now no cross-session state at all, so assert the
        # absence rather than the location -- a shared file is the defect.
        self.stamp_token()
        claude_dir = os.path.join(self.home, ".claude")
        self.assertFalse(os.path.exists(claude_dir),
                         "stamp wrote shared state under HOME: %s" %
                         (os.path.isdir(claude_dir) and os.listdir(claude_dir),))

        repo_skill_dir = os.path.dirname(SCRIPTS_DIR)
        for root, _dirs, files in os.walk(repo_skill_dir):
            for name in files:
                self.assertNotIn("stamps", name.lower(),
                                 "stamp-like file found inside the skill checkout: %s"
                                 % os.path.join(root, name))

    def test_a_missing_or_malformed_token_aborts_rather_than_installing(self):
        for bad in ("", "not-a-token", "12345"):
            with self.subTest(token=bad):
                staged = self.stage(name="staged-%s.md" % (bad or "empty"))
                proc = self.run_guard("install", self.target, staged, bad)
                self.assertEqual(proc.returncode, 1)
                self.assertIn(ORIGINAL_BODY.strip(), self.read(self.target))
                self.assertFalse(os.path.exists(self.history_path()),
                                 "a rejected token still appended to history")

    def test_a_malformed_token_is_not_reported_as_a_peer_write(self):
        """The two aborts must be distinguishable, and this is not message-golf.

        A malformed token fails the fingerprint compare too, so the *safety*
        assertions above pass whether or not the shape check exists -- measured:
        a mutation degrading `token_size` to return 0 instead of None survived
        them all. What the shape check actually buys is the diagnosis. "A peer
        wrote to it. re-read and redo this one" sends the caller off to redo a
        distillation that was never stale, over a typo in an argument they still
        have. Naming the real cause is the behaviour under test.
        """
        proc = self.run_guard("install", self.target, self.stage(), "not-a-token")
        out = proc.stdout + proc.stderr
        self.assertIn("is not a token from", out)
        self.assertNotIn("A peer wrote to it", out,
                         "a mistyped token was reported as a concurrent write")

    def test_a_genuinely_stale_token_still_reports_a_peer_write(self):
        """The control for the test above: without it, a guard that called
        everything a bad token would pass, and the real concurrent-write
        diagnosis -- the whole point of the tool -- would have gone silent."""
        token = self.stamp_token()
        self.write(self.target, self.fact_file(ORIGINAL_BODY + "a peer wrote this\n"))

        proc = self.run_guard("install", self.target, self.stage(), token)
        out = proc.stdout + proc.stderr
        self.assertIn("A peer wrote to it", out)
        self.assertNotIn("is not a token from", out)


class TestNothingIsArchivedForAnInstallThatCannotHappen(DistillGuardTest):
    """The history append is the one irreversible side effect, so every input
    is validated before it runs. A retry must not leave duplicate sections."""

    def test_a_missing_staged_file_leaves_the_history_untouched(self):
        token = self.stamp_token()
        missing = os.path.join(self._tmp.name, "typo-in-this-path.md")

        proc = self.run_guard("install", self.target, missing, token)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ABORT:", proc.stdout + proc.stderr,
                      "failed through a raw traceback instead of the abort convention")
        self.assertFalse(os.path.exists(self.history_path()),
                         "a snapshot was archived for an install that never happened")

    def test_retrying_after_a_bad_staged_path_writes_exactly_one_section(self):
        token = self.stamp_token()
        missing = os.path.join(self._tmp.name, "typo-in-this-path.md")
        self.run_guard("install", self.target, missing, token)
        self.run_guard("install", self.target, missing, token)

        proc = self.run_guard("install", self.target, self.stage(), token)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.read(self.history_path()).count("## Snapshot -- "), 1)


class TestG1SecondInstallDoesNotClobberHistory(DistillGuardTest):

    def test_a_second_install_does_not_clobber_an_existing_history(self):
        # First distillation: target starts as ORIGINAL_BODY, gets replaced
        # by a first distilled version.
        token = self.stamp_token()
        first_staged = self.stage(body=DISTILLED_BODY, name="staged1.md")
        proc1 = self.run_guard("install", self.target, first_staged, token)
        self.assertEqual(proc1.returncode, 0, proc1.stderr)

        size_after_first = os.path.getsize(self.history_path())
        self.assertGreater(size_after_first, 0)

        # Time passes, more incidents accumulate on top of the first
        # distillation, and it is distilled again. Padded well past the size
        # of the existing history file (header + first snapshot): a
        # mode="w" regression that replaces the whole history with just this
        # section would still come out LARGER than before, not smaller, so a
        # bare size-grew check cannot be what catches the mutation below --
        # only the content assertions can.
        second_round_body = (
            DISTILLED_BODY + "\nincident two happened on 2026-02-02.\n" +
            "padding to outsize the whole existing history file on its own.\n" * 15
        )
        self.write(self.target, self.fact_file(second_round_body))
        second_token = self.stamp_token()
        second_staged = self.stage(body="## Rule\n\nEven tighter rule.\n", name="staged2.md")
        proc2 = self.run_guard("install", self.target, second_staged, second_token)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)

        hist_after_second = self.read(self.history_path())
        size_after_second = os.path.getsize(self.history_path())

        # G1: assert on CONTENT, not existence or mtime -- an existence check
        # passes against a truncating writer just as easily.
        self.assertIn(ORIGINAL_BODY.strip(), hist_after_second,
                      "the first run's original body must survive verbatim")
        self.assertIn("incident two happened on 2026-02-02.", hist_after_second)
        self.assertGreater(size_after_second, size_after_first,
                           "history must GROW on a second install, never shrink")


class TestFrontmatterStrippingNeverEatsTheBody(DistillGuardTest):
    """`strip_frontmatter` decides what gets archived, and the fact file is
    overwritten in the same call on a directory with no version control and at
    most a days-old claude-backup copy. So it must strip only what it can positively recognise as frontmatter."""

    def install_and_read_history(self, target_text):
        self.write(self.target, target_text)
        token = self.stamp_token()
        proc = self.run_guard("install", self.target, self.stage(), token)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return self.read(self.history_path())

    def test_a_leading_horizontal_rule_is_not_treated_as_frontmatter(self):
        # No frontmatter at all -- the file opens on a markdown rule, and the
        # next rule sits below real evidence. Cutting between them loses it.
        hist = self.install_and_read_history(
            "---\n\n# Title\n\nIMPORTANT INCIDENT EVIDENCE A\n\n---\n\nSECTION B\n")
        self.assertIn("IMPORTANT INCIDENT EVIDENCE A", hist)
        self.assertIn("SECTION B", hist)

    def test_a_closing_fence_with_trailing_whitespace_still_closes_the_block(self):
        hist = self.install_and_read_history(
            "---\nname: example\ntype: user\n--- \n\nBODY KEPT\n")
        self.assertIn("BODY KEPT", hist)
        self.assertNotIn("name: example", hist)

    def test_well_formed_frontmatter_is_still_stripped(self):
        # The positive control. Without it, a strip_frontmatter that simply
        # returned its input unchanged would pass every test above.
        hist = self.install_and_read_history(
            "---\nname: example\ntype: user\n---\n\nBODY KEPT\n")
        self.assertIn("BODY KEPT", hist)
        self.assertNotIn("name: example", hist)

    def test_an_unclosed_leading_fence_passes_the_whole_file_through(self):
        hist = self.install_and_read_history(
            "---\nname: example\nno closing fence anywhere\nEVIDENCE C\n")
        self.assertIn("EVIDENCE C", hist)


class TestHistoryPathForSiblingForm(DistillGuardTest):

    def test_a_memory_target_keeps_the_history_subdir(self):
        self.assertEqual(
            distill_guard.history_path_for("/x/memory/foo.md"),
            "/x/memory/history/foo.history.md")

    def test_a_claude_md_target_gets_a_sibling_archive(self):
        # No history/ directory in a workspace root or a shared repo root.
        self.assertEqual(
            distill_guard.history_path_for("/x/CLAUDE.md"),
            "/x/CLAUDE.history.md")

    def test_the_branch_keys_on_the_memory_DIRECTORY_not_the_name(self):
        # guard-exclusions-key-on-behaviour: derived from what the target IS,
        # never a maintained list. A file merely NAMED memory.md is not one.
        self.assertEqual(
            distill_guard.history_path_for("/x/memory.md"),
            "/x/memory.history.md")
        self.assertEqual(
            distill_guard.history_path_for("/a/memory/b/deep.md"),
            "/a/memory/b/deep.history.md")


class TestHeaderForSiblingForm(DistillGuardTest):

    def test_a_memory_archive_keeps_its_incident_wording(self):
        h = distill_guard.header_for("/x/memory/foo.md")
        self.assertIn("incident history", h)
        self.assertIn("../foo.md", h)     # archive is one level down

    def test_a_sibling_archive_says_rationale_and_points_alongside(self):
        h = distill_guard.header_for("/x/CLAUDE.md")
        self.assertIn("rationale history", h)
        self.assertIn("`CLAUDE.md`", h)
        self.assertNotIn("../", h)        # sibling, not one level down

    def test_a_template_archive_names_itself_not_claude_md(self):
        # FINDING 2: os.path.splitext("CLAUDE.md.template") gives stem
        # "CLAUDE.md" -- titling the template's archive "# CLAUDE.md" (which
        # a reader would expect to be CLAUDE.md's OWN archive) and pointing
        # its back-reference at the nonexistent "CLAUDE.md.md". Both must
        # name the template itself, using the filename rather than the stem.
        h = distill_guard.header_for("/x/CLAUDE.md.template")
        self.assertIn("CLAUDE.md.template", h)
        self.assertNotIn("CLAUDE.md.md", h)


class TestSiblingArchiveWiring(DistillGuardTest):
    """FINDING 1: `history_path_for` and `header_for` were only exercised as
    pure functions. Every subprocess `install` test above targets self.target
    inside mem_dir, so hardcoding the old memory-shaped path back into
    `cmd_install`, or a fixed `MEMORY_HEADER` back into `snapshot_history`,
    would leave the whole suite green while reverting the sibling-archive
    feature entirely. These drive the real CLI against a non-memory target."""

    def setUp(self):
        super().setUp()
        self.claude_md = os.path.join(self._tmp.name, "CLAUDE.md")
        self.write(self.claude_md, "# rules\n\nrule one\n")

    def claude_history_path(self):
        return os.path.join(self._tmp.name, "CLAUDE.history.md")

    def test_install_against_a_claude_md_creates_a_sibling_archive(self):
        token = self.stamp_token(self.claude_md)
        staged = self.stage(body="# rules\n\nrule one, tightened\n",
                            name="staged1.md")
        proc = self.run_guard("install", self.claude_md, staged, token)
        self.assertEqual(proc.returncode, 0, proc.stderr)

        self.assertTrue(os.path.isfile(self.claude_history_path()),
                        "CLAUDE.history.md sibling was never created")
        self.assertFalse(
            os.path.isdir(os.path.join(self._tmp.name, "history")),
            "a history/ subdirectory was created for a non-memory target")

        first_line = self.read(self.claude_history_path()).splitlines()[0]
        self.assertIn("rationale history", first_line)

    def test_a_second_install_appends_with_the_header_written_once(self):
        token = self.stamp_token(self.claude_md)
        staged1 = self.stage(body="# rules\n\nrule one, tightened\n",
                             name="staged1.md")
        proc1 = self.run_guard("install", self.claude_md, staged1, token)
        self.assertEqual(proc1.returncode, 0, proc1.stderr)

        token2 = self.stamp_token(self.claude_md)
        staged2 = self.stage(body="# rules\n\nrule one, tightened again\n",
                             name="staged2.md")
        proc2 = self.run_guard("install", self.claude_md, staged2, token2)
        self.assertEqual(proc2.returncode, 0, proc2.stderr)

        hist = self.read(self.claude_history_path())
        self.assertEqual(hist.count("rationale history"), 1,
                         "the header was written more than once")
        self.assertEqual(hist.count("## Snapshot -- "), 2)


class TestAnArchiveClaudeCodeWouldLoadIsRefused(DistillGuardTest):
    """A rules or topic target's sibling archive would itself be LOADED, so
    `install` refuses before touching anything. Every refusal asserts the whole
    no-side-effect contract -- nonzero, the archive path named, target intact,
    staged file not consumed, no archive written -- because a refusal that
    still appended the snapshot would be the silent cost this exists to stop.
    """

    def assert_refused_untouched(self, target, archive):
        before = self.read(target)
        token = self.stamp_token(target)
        staged = self.stage(body="distilled\n", name="staged-refused.md")
        proc = self.run_guard("install", target, staged, token)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(os.path.realpath(archive), proc.stderr)
        self.assertIn("nothing installed", proc.stderr)
        self.assertEqual(self.read(target), before)
        self.assertTrue(os.path.isfile(staged), "the staged file was consumed")
        self.assertFalse(os.path.exists(archive), "an archive was written")

    def make(self, *parts, content="# rules\n\nrule one\n"):
        path = os.path.join(self._tmp.name, *parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.write(path, content)
        return path

    def test_a_rules_file_is_refused(self):
        target = self.make("proj", ".claude", "rules", "style.md")
        self.assert_refused_untouched(
            target, os.path.join(os.path.dirname(target), "style.history.md"))

    def test_a_nested_rules_file_is_refused(self):
        target = self.make("proj", ".claude", "rules", "sub", "style.md")
        self.assert_refused_untouched(
            target, os.path.join(os.path.dirname(target), "style.history.md"))

    def test_a_topic_file_is_refused(self):
        target = os.path.join(self.mem_dir, "topics", "ops.md")
        os.makedirs(os.path.dirname(target))
        self.write(target, "- [A](../a.md) -- hook\n")
        self.assert_refused_untouched(
            target, os.path.join(self.mem_dir, "topics", "ops.history.md"))

    def test_a_non_md_target_in_the_rules_tree_is_refused_by_its_archive(self):
        # `is_rules_file` requires `.md`, so the TARGET here does not match --
        # only the archive (`CLAUDE.md.history.md`) does. Pins that the check
        # is asked of the archive, not the target.
        target = self.make("proj", ".claude", "rules", "CLAUDE.md.template")
        self.assert_refused_untouched(
            target,
            os.path.join(os.path.dirname(target), "CLAUDE.md.history.md"))

    def test_a_claude_md_inside_a_dot_claude_dir_still_installs(self):
        # Positive control: `.claude/CLAUDE.md` is a real CLAUDE.md location
        # and its archive is not loaded. A check keyed on `/.claude/` rather
        # than `/.claude/rules/` would refuse it.
        target = self.make("proj", ".claude", "CLAUDE.md")
        token = self.stamp_token(target)
        staged = self.stage(body="# rules\n\ntightened\n", name="staged-ok.md")
        proc = self.run_guard("install", target, staged, token)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(os.path.isfile(
            os.path.join(os.path.dirname(target), "CLAUDE.history.md")))

    def test_a_topics_dir_outside_memory_still_installs(self):
        # Positive control for the topic predicate's `memory/` anchor.
        target = self.make("proj", "topics", "CLAUDE.md")
        token = self.stamp_token(target)
        staged = self.stage(body="# rules\n\ntightened\n", name="staged-ok.md")
        proc = self.run_guard("install", target, staged, token)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_an_unimportable_growth_lib_refuses_rather_than_guessing(self):
        # A lone copy of the script has no tools/context-growth beside it.
        lone_dir = os.path.join(self._tmp.name, "a", "b", "c")
        os.makedirs(lone_dir)
        lone = os.path.join(lone_dir, "distill_guard.py")
        self.write(lone, self.read(GUARD))
        before = self.read(self.target)
        token = self.stamp_token()
        staged = self.stage()
        env = dict(os.environ, HOME=self.home, USERPROFILE=self.home,
                   CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        proc = subprocess.run(
            [sys.executable, lone, "install", self.target, staged, token],
            env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("growth_lib import failed", proc.stderr)
        self.assertEqual(self.read(self.target), before)
        self.assertFalse(os.path.exists(self.history_path()))


class TestG3MemoryDirectoryIsNotHardcoded(DistillGuardTest):

    def test_the_memory_directory_is_not_hardcoded(self):
        with open(GUARD, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("/Users/", source)

    def test_a_nonexistent_target_exits_nonzero_and_says_so(self):
        missing = os.path.join(self.mem_dir, "does-not-exist.md")
        proc = self.run_guard("stamp", missing)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(missing, proc.stdout + proc.stderr)

        install_proc = self.run_guard("install", missing, self.stage(), "1:1")
        self.assertNotEqual(install_proc.returncode, 0)
        self.assertIn(missing, install_proc.stdout + install_proc.stderr)


def load_portable():
    """lib/portable.py by file path, the way distill_guard itself loads it."""
    path = os.path.join(os.path.dirname(os.path.dirname(SCRIPTS_DIR)),
                        "lib", "portable.py")
    spec = importlib.util.spec_from_file_location("portable_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestConcurrentInstalls(DistillGuardTest):
    """Installs run in-process, so $HOME AND $USERPROFILE are sandboxed here:
    cmd_install writes its lock under the home directory, and on Windows
    `expanduser` reads USERPROFILE, not HOME."""

    def setUp(self):
        super().setUp()
        for name in ("HOME", "USERPROFILE"):
            self.addCleanup(self._restore_env, name, os.environ.get(name))
            os.environ[name] = self.home
        self.portable = load_portable()

    @staticmethod
    def _restore_env(name, value):
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    @staticmethod
    def snapshot(path):
        """The file's bytes, or None when it does not exist."""
        if not os.path.exists(path):
            return None
        with open(path, "rb") as handle:
            return handle.read()

    def install(self, target, staged, token):
        """cmd_install, returning the SystemExit message (None on success)."""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                distill_guard.cmd_install(target, staged, token)
        except SystemExit as exc:
            return str(exc.code)
        return None

    def tmp_files(self):
        return [n for n in os.listdir(self.mem_dir) if ".distill-tmp-" in n]

    def test_second_install_on_the_same_token_waits_then_aborts(self):
        token = distill_guard.fingerprint(self.target)
        staged_a = self.stage("body A\n", "a.md")
        staged_b = self.stage("body B\n", "b.md")
        a_snapshotted = threading.Event()
        resume_a = threading.Event()
        real_snapshot = distill_guard.snapshot_history

        def paused_snapshot(target, hist_path):
            real_snapshot(target, hist_path)
            if threading.current_thread().name == "A":
                a_snapshotted.set()
                resume_a.wait(10)

        results = {}

        def run(name, staged):
            results[name] = self.install(self.target, staged, token)

        thread_a = threading.Thread(target=run, name="A", args=("A", staged_a))
        thread_b = threading.Thread(target=run, name="B", args=("B", staged_b))
        with mock.patch.object(distill_guard, "snapshot_history",
                               paused_snapshot):
            thread_a.start()
            self.assertTrue(a_snapshotted.wait(10))
            thread_b.start()
            thread_b.join(timeout=0.3)
            blocked = thread_b.is_alive()
            resume_a.set()
            thread_a.join(timeout=10)
            thread_b.join(timeout=10)
        self.assertTrue(blocked, "B ran while A held the install")
        self.assertIsNone(results["A"])
        self.assertIn("changed since it was read", results["B"] or "")
        self.assertEqual(self.read(self.target), "body A\n")
        hist = self.read(self.history_path())
        self.assertEqual(hist.count(ORIGINAL_BODY.strip()), 1)
        self.assertNotIn("body B", hist)

    def test_install_aborts_when_the_lock_is_held(self):
        token = distill_guard.fingerprint(self.target)
        staged = self.stage()
        before = self.snapshot(self.target)
        hist_before = self.snapshot(self.history_path())
        lock = distill_guard._lock_path(os.path.realpath(self.target))
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        old_timeout = distill_guard.INSTALL_LOCK_TIMEOUT
        self.addCleanup(setattr, distill_guard, "INSTALL_LOCK_TIMEOUT",
                        old_timeout)
        distill_guard.INSTALL_LOCK_TIMEOUT = 0.2
        with self.portable.exclusive_lock(lock) as held:
            self.assertTrue(held)
            message = self.install(self.target, staged, token)
        self.assertIn("still running", message or "")
        self.assertEqual(self.snapshot(self.target), before)
        self.assertEqual(self.snapshot(self.history_path()), hist_before)
        self.assertTrue(os.path.isfile(staged))

    def test_staged_file_that_is_the_target_is_refused(self):
        token = distill_guard.fingerprint(self.target)
        before = self.snapshot(self.target)
        hist_before = self.snapshot(self.history_path())
        message = self.install(self.target, self.target, token)
        self.assertIn("IS the target", message or "")
        self.assertEqual(self.snapshot(self.target), before)
        self.assertEqual(self.snapshot(self.history_path()), hist_before)

    def test_interrupted_copy_leaves_the_target_intact(self):
        token = distill_guard.fingerprint(self.target)
        staged = self.stage()
        before = self.snapshot(self.target)

        def half_copy(src, dst, *args, **kwargs):
            data = self.snapshot(src)
            with open(dst, "wb") as handle:
                handle.write(data[:len(data) // 2])
            raise OSError("disk full")

        with mock.patch.object(distill_guard.shutil, "copyfile", half_copy):
            message = self.install(self.target, staged, token)
        self.assertIn("could not move", message or "")
        self.assertEqual(self.snapshot(self.target), before)
        self.assertEqual(self.tmp_files(), [])

    def test_external_edit_during_the_copy_is_not_overwritten(self):
        """A writer that takes no lock edits the target mid-install. The
        install must abort rather than rename over the edit."""
        token = distill_guard.fingerprint(self.target)
        staged = self.stage()
        real_copyfile = distill_guard.shutil.copyfile
        external = "an external edit landed during the copy\n"

        def copy_then_edit(src, dst, *args, **kwargs):
            result = real_copyfile(src, dst, *args, **kwargs)
            self.write(self.target, external)
            return result

        with mock.patch.object(distill_guard.shutil, "copyfile", copy_then_edit):
            message = self.install(self.target, staged, token)
        self.assertIn("changed during the install", message or "")
        self.assertEqual(self.read(self.target), external)
        self.assertEqual(self.tmp_files(), [])

    def test_install_from_another_directory_replaces_and_removes_the_staged_file(self):
        token = distill_guard.fingerprint(self.target)
        other = os.path.join(self._tmp.name, "elsewhere")
        os.makedirs(other)
        staged = os.path.join(other, "staged.md")
        self.write(staged, DISTILLED_BODY)
        self.assertIsNone(self.install(self.target, staged, token))
        self.assertEqual(self.read(self.target), DISTILLED_BODY)
        self.assertFalse(os.path.exists(staged))
        self.assertEqual(self.tmp_files(), [])

    def _case_insensitive_alias(self):
        """The target's swapcased path, or skip when the filesystem is
        case-sensitive (the alias would then name a different, absent file)."""
        alias = os.path.join(self.mem_dir,
                             os.path.basename(self.target).swapcase())
        if not os.path.exists(alias):
            self.skipTest("temp filesystem is case-sensitive")
        return alias

    def test_case_alias_of_the_target_takes_the_same_lock(self):
        alias = self._case_insensitive_alias()
        self.assertEqual(distill_guard._lock_path(self.target),
                         distill_guard._lock_path(alias))

    def test_unicode_normalisation_alias_takes_the_same_lock(self):
        composed = os.path.join(self.mem_dir, "caf\u00e9.md")
        decomposed = os.path.join(self.mem_dir, "cafe\u0301.md")
        self.assertNotEqual(composed, decomposed)
        self.assertEqual(distill_guard._lock_path(composed),
                         distill_guard._lock_path(decomposed))

    def test_case_alias_of_the_target_as_staged_file_is_refused(self):
        alias = self._case_insensitive_alias()
        token = distill_guard.fingerprint(self.target)
        target_before = self.snapshot(self.target)
        history_before = self.snapshot(self.history_path())
        message = self.install(self.target, alias, token)
        self.assertIn("IS the target", message or "")
        self.assertEqual(self.snapshot(self.target), target_before)
        self.assertEqual(self.snapshot(self.history_path()), history_before)


class TestImportability(unittest.TestCase):
    """distill_guard.py must be importable with no side effects at import."""

    def test_the_module_imports_cleanly_and_writes_nothing_on_import(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            old_home = os.environ.get("HOME")
            sys.path.insert(0, SCRIPTS_DIR)
            try:
                os.environ["HOME"] = home
                sys.modules.pop("distill_guard", None)
                import distill_guard  # noqa: F401
                self.assertEqual(os.listdir(home), [],
                                 "importing the module wrote something under HOME")
            finally:
                sys.path.remove(SCRIPTS_DIR)
                if old_home is not None:
                    os.environ["HOME"] = old_home


if __name__ == "__main__":
    unittest.main(verbosity=2)
