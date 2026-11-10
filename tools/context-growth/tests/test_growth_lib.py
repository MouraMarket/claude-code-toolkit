#!/usr/bin/env python3
"""Tests for the context-growth PostToolUse hook.

The class that carries the design is TestTransition. `build` must fire on
GROWTH, not on state: both files this hook exists for stay over cap until the
slice-3 cut, so a state check would fire on every edit to them — including the
edits that shrink them. That is the notice-nobody-reads failure this hook was
built to replace, and it is not detectable by any test of a single edit.
"""

import ast, inspect, json, os, re, shutil, subprocess, sys, tempfile, textwrap, unittest
from unittest import mock

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TOOLS = os.path.dirname(HERE)
REPO = os.path.dirname(TOOLS)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "lib"))
sys.path.insert(0, os.path.join(TOOLS, "context-lint"))
sys.path.insert(0, os.path.join(TOOLS, "tooling-digest"))
sys.path.insert(0, os.path.join(TOOLS, "memory-lint"))
sys.path.insert(0, os.path.join(TOOLS, "context-budget"))
import context_lint_lib
import core_block
import digest_lib
import lint_lib
import growth_lib
import toolkit_config  # on sys.path via the lib/ insert above
import routing_lib  # on sys.path via growth_lib's own insert, above
import budget_lib  # on sys.path via the context-budget insert above

EXE = os.path.join(HERE, "claude-context-growth")
ENV = dict(os.environ, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")

# The two Core caps are DIFFERENT NUMBERS, and that is what makes the
# "Two Core caps, deliberately" decision testable at all. Asserted rather than
# hardcoded: if they ever converge, the tests that discriminate between them
# stop discriminating and must be rewritten rather than silently passing.
CLAUDE_CORE_CAP = context_lint_lib.DEFAULT_CORE_MAX_LINES   # 80
TOOLING_CORE_CAP = digest_lib.DEFAULT_MAX_LINES             # 60


class TestTheCapsAreDistinguishable(unittest.TestCase):
    def test_the_two_core_caps_differ(self):
        self.assertNotEqual(
            CLAUDE_CORE_CAP, TOOLING_CORE_CAP,
            "the Core-cap tests below discriminate by cap VALUE; if these "
            "converge those tests are vacuous and must be rewritten")
        self.assertLess(TOOLING_CORE_CAP, CLAUDE_CORE_CAP)


class TestInScope(unittest.TestCase):
    def test_claude_md_and_template_are_in_scope(self):
        self.assertTrue(growth_lib.in_scope("/a/b/CLAUDE.md"))
        self.assertTrue(growth_lib.in_scope("/a/b/CLAUDE.md.template"))

    def test_tooling_md_is_in_scope(self):
        self.assertTrue(growth_lib.in_scope("/a/b/TOOLING.md"))

    def test_an_ordinary_file_is_not(self):
        for path in ("/a/b/README.md", "/a/b/notes.md", "/a/b/claude.py",
                     "/a/b/MEMORY.md", "", None):
            self.assertFalse(growth_lib.in_scope(path), repr(path))


class TestMemoryInScope(unittest.TestCase):
    """`in_scope`: a memory entry is now in
    scope, and the history exclusion and MEMORY.md exclusion still hold."""

    def test_a_memory_entry_is_in_scope(self):
        self.assertTrue(growth_lib.in_scope("/x/memory/foo.md"))

    def test_a_memory_history_entry_is_not_in_scope(self):
        self.assertFalse(growth_lib.in_scope("/x/memory/history/foo.history.md"))

    def test_memory_md_in_a_memory_dir_is_in_scope(self):
        # MEMORY.md IS in scope now -- as the index, not as a memory entry
        # (is_memory_file stays False for it; see TestIsMemoryFile below).
        self.assertTrue(growth_lib.in_scope("/x/memory/MEMORY.md"))

    def test_a_plain_md_outside_memory_stays_out(self):
        self.assertFalse(growth_lib.in_scope("/x/notes.md"))


class TestIsMemoryFile(unittest.TestCase):
    """Direct coverage of the predicate `in_scope` and `caps_for_path` both
    delegate to, so the mutation below has somewhere to be caught even if a
    future change decouples the two callers."""

    def test_a_memory_entry_is_true(self):
        self.assertTrue(growth_lib.is_memory_file("/x/memory/foo.md"))

    def test_a_history_entry_is_false(self):
        self.assertFalse(
            growth_lib.is_memory_file("/x/memory/history/foo.history.md"))

    def test_memory_md_is_false(self):
        self.assertFalse(growth_lib.is_memory_file("/x/memory/MEMORY.md"))

    def test_none_and_empty_are_false(self):
        self.assertFalse(growth_lib.is_memory_file(None))
        self.assertFalse(growth_lib.is_memory_file(""))


class TestIsMemoryIndex(unittest.TestCase):
    """Separate from `is_memory_file` on purpose: the index
    is a routing artifact, not a memory entry, and gets a different cap on a
    different unit."""

    def test_the_index_itself_is_true(self):
        self.assertTrue(growth_lib.is_memory_index("/x/memory/MEMORY.md"))

    def test_an_ordinary_file_outside_memory_is_false(self):
        self.assertFalse(growth_lib.is_memory_index("/a/b/MEMORY.md"))

    def test_an_ordinary_memory_entry_is_false(self):
        self.assertFalse(growth_lib.is_memory_index("/x/memory/foo.md"))

    def test_a_history_copy_of_the_index_is_false(self):
        self.assertFalse(
            growth_lib.is_memory_index("/x/memory/history/MEMORY.md"))

    def test_a_backup_suffixed_name_is_false(self):
        self.assertFalse(growth_lib.is_memory_index("/x/memory/MEMORY.md.bak"))

    def test_none_and_empty_are_false(self):
        self.assertFalse(growth_lib.is_memory_index(None))
        self.assertFalse(growth_lib.is_memory_index(""))


class TestIsTopicFile(unittest.TestCase):
    """Mirrors TestIsMemoryIndex's shape: matched on path shape
    alone, no read, via a `/memory/topics/<name>.md` suffix."""

    def test_a_topic_file_is_true(self):
        self.assertTrue(growth_lib.is_topic_file("/x/memory/topics/a.md"))

    def test_an_ordinary_memory_entry_is_false(self):
        self.assertFalse(growth_lib.is_topic_file("/x/memory/a.md"))

    def test_the_index_itself_is_false(self):
        self.assertFalse(growth_lib.is_topic_file("/x/memory/MEMORY.md"))

    def test_a_nested_topics_subdirectory_is_false(self):
        self.assertFalse(growth_lib.is_topic_file("/x/memory/topics/sub/a.md"))

    def test_a_history_entry_is_false(self):
        self.assertFalse(
            growth_lib.is_topic_file("/x/memory/history/a.history.md"))

    def test_a_topics_dir_outside_memory_is_false(self):
        self.assertFalse(growth_lib.is_topic_file("/x/topics/a.md"))

    def test_none_and_empty_are_false(self):
        self.assertFalse(growth_lib.is_topic_file(None))
        self.assertFalse(growth_lib.is_topic_file(""))


class TestTopicFileIsNotAMemoryEntry(unittest.TestCase):
    """Pinned by a test, not assumed: a topic file has two path
    segments between `memory/` and the filename, and both predicates below
    require exactly one (Premise B: `routing_lib.MEMORY_DIR_RE`)."""

    def test_is_memory_file_is_false_for_a_topic_file(self):
        self.assertFalse(growth_lib.is_memory_file("/x/memory/topics/a.md"))

    def test_routing_lib_is_memory_write_is_false_for_a_topic_file(self):
        self.assertFalse(routing_lib.is_memory_write("/x/memory/topics/a.md"))


class TestTopicInScope(unittest.TestCase):
    """Not a numbered AC on its own, but stated in the task's own bullets
    (`in_scope` also returns True for `is_topic_file`) and cheap to pin
    directly rather than only through the executable in AC6."""

    def test_a_topic_file_is_in_scope(self):
        self.assertTrue(growth_lib.in_scope("/x/memory/topics/a.md"))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="growth-")
        self.state = {}

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def rewrite(self, path, text):
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def edit(self, path, config=None):
        """One edit through build(), threading the in-memory state forward."""
        return growth_lib.build({"file_path": path}, config, state=self.state)

    def repo(self, name, owner="example-org"):
        """A real git repo inside the fixture dir.

        Needed because `cap_key` returns None for anything outside a repo, so
        a suite built only from mkdtemp() paths exercises exactly one of the
        two branches of the resolver.

        `owner` becomes its `origin`, and shipped overrides are keyed
        `<owner>/<repo>/<path>` — a fixture with no remote resolves only the
        bare key and quietly gets the default, which is a different test.
        """
        path = os.path.join(self.dir, name)
        os.makedirs(path)
        for args in (("init", "-q"), ("config", "user.email", "t@e.com"),
                     ("config", "user.name", "t")):
            subprocess.run(("git",) + args, cwd=path, check=True,
                           capture_output=True, stdin=subprocess.DEVNULL)
        if owner:
            subprocess.run(("git", "remote", "add", "origin",
                            "https://github.com/%s/%s.git" % (owner, name)),
                           cwd=path, check=True, capture_output=True,
                           stdin=subprocess.DEVNULL)
        return path

    def core(self, rows, marker="CORE"):
        return ("<!-- %s:START -->\n%s\n<!-- %s:END -->\n"
                % (marker, "\n".join("row %d" % i for i in range(rows)), marker))

    def memory_file(self, root=None, name="a-decision.md", text="x\n"):
        """A memory entry at `<root or self.dir>/memory/<name>`."""
        d = os.path.join(root or self.dir, "memory")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def topic_file(self, root=None, name="t.md", text="x\n"):
        """A topic file at `<root or self.dir>/memory/topics/<name>`."""
        d = os.path.join(root or self.dir, "memory", "topics")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def claude_md(self, name="CLAUDE.md", text="x\n"):
        """A CLAUDE.md-family file at `<self.dir>/<name>`, outside any repo."""
        path = os.path.join(self.dir, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def rules_file(self, text, name="topic.md", sub=""):
        """A rules file at `<self.dir>/.claude/rules/<sub>/<name>`."""
        d = os.path.join(self.dir, ".claude", "rules", sub)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        with open(path, "w") as handle:
            handle.write(text)
        return path

    def scoped(self, n_lines, extra_front=""):
        """`n_lines` of body under a `paths:` frontmatter block."""
        return ("---\npaths:\n  - \"**\"\n%s---\n" % extra_front
                + "row\n" * n_lines)

    def pathless(self, n_lines):
        """`n_lines` of body with no frontmatter at all."""
        return "row\n" * n_lines


class TestTheFirstCoreIsNotFreeGrowth(Fixture):
    """A Core appearing for the FIRST time must be measured like any other.

    `observe` used to record a file with no Core as `core_lines: None`, and
    `_rose_over_cap` reads `None` as "no prior observation" and returns False.
    So "had no Core, now has one" was indistinguishable from "never seen this
    file", and the hook stayed silent at ANY size. Both target files have no
    Core today and the entire contract is about the Core they are about to get,
    so the very first — and largest — Core write was the one case guaranteed to
    go unreported. `0` for absent makes it an ordinary rise.
    """

    def test_a_files_FIRST_Core_speaks_when_it_lands_over_cap(self):
        # Edit 1: no Core at all, and the file is observed. This is what made
        # the old sentinel look harmless — the record exists, so `prior is
        # None` does not fire on edit 2.
        path = self.write("CLAUDE.md", "rule\n" * 20)
        self.assertIsNone(self.edit(path))
        self.assertEqual(self.state[os.path.realpath(path)]["core_lines"], 0,
                         "absent Core must record 0, not None — None is "
                         "'unknown' and is silent forever")
        # Edit 2: a Core lands far over cap.
        self.rewrite(path, self.core(CLAUDE_CORE_CAP + 220))
        notice = self.edit(path)
        self.assertIsNotNone(
            notice, "a first Core %d lines over cap was silent"
                    % (CLAUDE_CORE_CAP + 220 - CLAUDE_CORE_CAP))
        self.assertIn("Core:", notice)
        self.assertIn("cap %d" % CLAUDE_CORE_CAP, notice)

    def test_a_first_Core_UNDER_cap_stays_silent(self):
        """Discriminator. Without it the test above passes for a version that
        simply speaks on every appearing Core, which is a state check — the
        thing D2 exists to reject."""
        path = self.write("CLAUDE.md", "rule\n" * 20)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, self.core(CLAUDE_CORE_CAP - 5))
        self.assertIsNone(self.edit(path))

    def test_deleting_a_Core_does_not_bank_free_growth_when_it_returns(self):
        """The ratchet leak the sentinel opened, and the more dangerous half:
        it is permanent. Under the old code the delete recorded `None`, and
        every subsequent re-add — at any size — was read as first sight."""
        path = self.write("CLAUDE.md", self.core(CLAUDE_CORE_CAP - 30))
        self.assertIsNone(self.edit(path))            # first sight, silent
        self.rewrite(path, "no core here\n" * 5)      # Core deleted
        self.assertIsNone(self.edit(path), "a deletion is a shrink and shrinks "
                                           "are silent")
        self.assertEqual(self.state[os.path.realpath(path)]["core_lines"], 0)
        self.rewrite(path, self.core(CLAUDE_CORE_CAP + 300))
        notice = self.edit(path)
        self.assertIsNotNone(notice, "re-adding a Core 300 lines over cap after "
                                     "a delete was silent — the ratchet reset")
        self.assertIn("Core:", notice)


class TestTheNoticeClaimsOnlyWhatHappened(Fixture):
    """A notice that overstates its own finding is the first step to being
    read as noise, which is the failure this whole contract indicts.

    `build` fires on EITHER a size rise or a new dangling cross-reference. The
    header and trailer were fixed strings asserting growth, so a ref-only
    notice announced "grew past its budget" with no size row under it — the
    reader looks for the number and there is none.
    """

    def _ref_only(self):
        """A Core well under cap whose only change is a new dangling ref."""
        path = self.write("CLAUDE.md",
                          "<!-- CORE:START -->\nA rule.\n<!-- CORE:END -->\n")
        self.assertIsNone(self.edit(path))
        self.rewrite(path,
                     "<!-- CORE:START -->\nA rule; see below.\n"
                     "<!-- CORE:END -->\n")
        return self.edit(path)

    def test_a_ref_only_notice_does_not_claim_the_file_grew(self):
        notice = self._ref_only()
        self.assertIsNotNone(notice, "the ref itself must still be reported — "
                                     "this fixture is the positive control")
        self.assertIn("cross-reference", notice)
        self.assertNotIn("grew past its budget", notice)
        # And no size claim in the trailer either.
        self.assertNotIn("paid in every session", notice)
        # No size row was rendered, which is WHY the size header was wrong.
        self.assertNotIn("cap ", notice)

    def test_a_size_notice_still_claims_growth(self):
        """Discriminator: the header is conditional, not deleted.

        DEVIATION from pre-existing text: the remediation half used to be
        "paid in every session" (the "Trim it" trailer) for every in-scope
        file. The CLAUDE.md family has a distinct
        claude-md-distill trailer, so the remediation assertion below now
        checks THAT wording rather than the trim wording it replaced for
        this file.
        """
        path = self.write("CLAUDE.md", self.core(CLAUDE_CORE_CAP - 5))
        self.assertIsNone(self.edit(path))
        self.rewrite(path, self.core(CLAUDE_CORE_CAP + 40))
        notice = self.edit(path)
        self.assertIn("grew past its budget", notice)
        self.assertIn("claude-md-distill", notice)

    def test_the_delta_is_labelled_since_the_last_check_not_this_edit(self):
        """PostToolUse sees only Edit/Write/MultiEdit, so a `Bash` heredoc's
        lines are never observed and land on whatever edit comes next. The
        span between two observations is the only honest claim."""
        path = self.write("CLAUDE.md", self.core(CLAUDE_CORE_CAP - 5))
        self.assertIsNone(self.edit(path))
        self.rewrite(path, self.core(CLAUDE_CORE_CAP + 40))
        notice = self.edit(path)
        self.assertIn("since last check", notice)
        self.assertNotIn("this edit", notice)


class TestTransition(Fixture):
    """D2 — the finding that mattered."""

    def test_first_sight_of_an_over_cap_file_is_SILENT(self):
        # With no prior record, growth cannot be told from a pre-existing state.
        # Speaking here would be the state check this design rejects.
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.assertIsNone(self.edit(path))

    def test_the_first_sight_guard_is_PRESENT(self):
        """STRUCTURAL, and labelled as such because it cannot be behavioural.

        MEASURED while executing this plan: deleting `if prior is None: return
        None` from build() leaves test_first_sight_of_an_over_cap_file_is_SILENT
        GREEN. With the guard gone, `prior.get("lines")` raises
        `AttributeError: 'NoneType' object has no attribute 'get'`, and build's
        own fail-safe `except Exception: return None` swallows it into exactly
        the silence the guard produces deliberately. Two mechanisms, one
        observable outcome — so no behavioural test can separate them, and the
        mutation that was supposed to grade D2's first-sight rule survived.

        This does NOT prove first sight is silent; the behavioural test above
        does that, over-determined as it is. It only fails if someone deletes
        the guard, which is worth having on its own — an accidental silence via
        an exception on every first edit is fragile in a way the explicit
        return is not. The transition rule itself is graded behaviourally by
        test_a_SHRINKING_edit_on_an_over_cap_file_is_silent, which a state
        check DOES kill.
        """
        tree = ast.parse(inspect.getsource(growth_lib.build))
        guards = [n for n in ast.walk(tree)
                  if isinstance(n, ast.If)
                  and isinstance(n.test, ast.Compare)
                  and isinstance(n.test.ops[0], ast.Is)
                  and isinstance(n.test.left, ast.Name)
                  and n.test.left.id == "prior"
                  and any(isinstance(c, ast.Constant) and c.value is None
                          for c in n.test.comparators)]
        self.assertEqual(len(guards), 1,
                         "build() must carry exactly one `if prior is None` "
                         "guard; first-sight silence is otherwise an artifact "
                         "of the fail-safe exception handler")
        self.assertTrue(
            any(isinstance(n, ast.Return) for n in ast.walk(guards[0])),
            "the first-sight guard must RETURN, not fall through")

    def test_growth_that_CROSSES_the_cap_speaks(self):
        """The one notice this hook exists to produce, and the pre-split
        suite had no test for it.

        Every speaking test there was already over cap BEFORE the edit
        (500->510, 450->453), and every silence test was under cap on both
        sides. So a predicate of "rose AND `prior` was already over cap" — a
        plausible misreading of "the parts that both rose and are over their
        cap" — passed all seven. This is the case that separates them.
        """
        path = self.write("CLAUDE.md", "rule\n" * 790)   # UNDER the 800 default
        self.assertIsNone(self.edit(path), "first sight must be silent")
        self.rewrite(path, "rule\n" * 810)               # OVER it
        out = self.edit(path)
        self.assertIsNotNone(out, "an under-cap -> over-cap crossing must speak")
        self.assertIn("CLAUDE.md", out)

    def test_growth_past_the_cap_names_the_new_count_cap_overage_and_delta(self):
        # 900 -> 947 is chosen so all four numbers are DISTINCT STRINGS and no
        # assertion is entailed by another. The pre-split version used
        # 500 -> 510 and asserted `assertIn("10")` as "the delta" directly
        # after `assertIn("510")` — "10" is a substring of "510" AND of the
        # overage "110", so the delta was proven by nothing, three ways over.
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.edit(path)                                   # record, silent
        self.rewrite(path, "rule\n" * 947)
        out = self.edit(path)
        self.assertIsNotNone(out)
        self.assertIn("CLAUDE.md", out)
        self.assertIn("947", out)          # the new count
        self.assertIn("800", out)          # the cap
        self.assertIn("147", out)          # the overage, 947 - 800
        self.assertIn("+47", out)          # the DELTA, in the form D2 mandates
        # Independence, asserted rather than assumed: none of the four strings
        # is a substring of any other. A future renumbering that breaks this
        # reintroduces the vacuous assertion.
        for a in ("947", "800", "147", "+47"):
            others = [b for b in ("947", "800", "147", "+47") if b != a]
            self.assertFalse([b for b in others if a in b],
                             "%r is a substring of another asserted value" % a)

    def test_the_notice_says_what_to_do_about_it(self):
        # Slice 2d requires the notice to name the file, the overage, AND a
        # remediation. The pre-split plan's notice contract dropped the
        # remediation half without declaring it a deviation, which is how a
        # requirement disappears quietly.
        #
        # The "Trim it" trailer says "before the session ends". The
        # CLAUDE.md family has its own claude-md-distill trailer, so this
        # asserts THAT remediation is present rather than the trim wording it
        # replaced for this file.
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.edit(path)
        self.rewrite(path, "rule\n" * 947)
        out = self.edit(path)
        self.assertIn("claude-md-distill", out)

    def test_a_SHRINKING_edit_on_an_over_cap_file_is_silent(self):
        # The whole point. Slice 3 will make hundreds of these.
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.edit(path)
        self.rewrite(path, "rule\n" * 850)
        self.assertIsNone(self.edit(path))

    def test_an_unchanged_edit_on_an_over_cap_file_is_silent(self):
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.edit(path)
        self.assertIsNone(self.edit(path))

    def test_a_shrink_lowers_the_record_so_regrowth_is_caught(self):
        # The ratchet leak: without recording the shrink, cutting 50 and adding
        # 3 back stays under the old high-water mark and passes unseen.
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.edit(path)
        self.rewrite(path, "rule\n" * 850)
        self.edit(path)
        self.rewrite(path, "rule\n" * 853)
        self.assertIsNotNone(self.edit(path))

    def test_growth_that_stays_UNDER_cap_is_silent(self):
        path = self.write("CLAUDE.md", "rule\n" * 100)
        self.edit(path)
        self.rewrite(path, "rule\n" * 200)
        self.assertIsNone(self.edit(path))

    def test_a_claude_md_core_crossing_ITS_cap_speaks(self):
        """The CLAUDE.md Core branch — `cap_for(key, *core_caps(config))`.

        The pre-split suite's only Core test used a TOOLING.md fixture, which
        resolves through an entirely different branch, so this one had zero
        coverage. 70 -> 85 crosses 80 and would NOT cross 60, so it also
        proves a CLAUDE.md is not wired to the tooling cap.
        """
        path = self.write("CLAUDE.md", self.core(70))
        self.assertIsNone(self.edit(path))
        self.rewrite(path, self.core(85))
        out = self.edit(path)
        self.assertIsNotNone(out)
        self.assertIn("Core", out)
        self.assertIn(str(CLAUDE_CORE_CAP), out)

    def test_a_tooling_md_core_crossing_ITS_cap_speaks(self):
        """The tooling-map Core branch — `digest_lib` numbers, not
        `context_lint`'s.

        50 -> 70 crosses 60 and stays UNDER 80, so this test FAILS if
        TOOLING.md is wired to `context_lint.core_max_lines`. That is what
        makes the "Two Core caps, deliberately / Do not unify them" decision
        verified rather than merely written down. The pre-split fixture went
        100 -> 110, which is over both caps and therefore discriminates
        nothing.
        """
        path = self.write("TOOLING.md", self.core(50, marker="DIGEST"))
        self.assertIsNone(self.edit(path))
        self.rewrite(path, self.core(70, marker="DIGEST"))
        out = self.edit(path)
        self.assertIsNotNone(out)
        self.assertIn("Core", out)
        self.assertIn(str(TOOLING_CORE_CAP), out)

    def test_a_tooling_md_growing_past_the_whole_file_cap_is_silent(self):
        """`whole_file_cap is None` for the tooling map, behaviourally.

        A real TOOLING.md is a long reference whole-file. Wire it to the
        context_lint default and every single edit reports far over the
        cap — a notice that fires constantly is a notice nobody reads,
        which is the failure this whole build indicts. No fixture in the
        pre-split suite was over 120 lines, so none could tell None from a cap.

        THE FIXTURE MUST EXCEED THE LIMIT IT IS EXEMPT FROM. At 500 lines it
        did while the default was 400; when the default became 800 on
        2026-09-15 the same fixture sat comfortably UNDER it, so "silent"
        would have been true of an exempt file and a capped one alike — the
        test would have kept passing while testing nothing. Sized off
        DEFAULT_MAX_LINES here rather than a literal so it cannot happen again.
        """
        over_cap = context_lint_lib.DEFAULT_MAX_LINES + 100
        path = self.write("TOOLING.md", "row\n" * over_cap)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "row\n" * (over_cap + 100))
        self.assertIsNone(self.edit(path),
                          "the tooling map has no whole-file cap")


# Repo-keyed caps for the tests that need the repo-keyed BRANCH to produce a
# value the default branch cannot. `context_lint_lib.DEFAULT_OVERRIDES` has been
# empty since 2026-09-15 (one standardized cap), so reading a number out of it
# no longer works — and, more importantly, with no per-repo entry anywhere the
# two branches of `caps_for_path` return the SAME number and these tests stop
# distinguishing them at all. Supplying the overrides through config keeps the
# branch covered and matches how a real override reaches the resolver now.
REPO_CAPS = {
    "example-org/claude-code-toolkit/CLAUDE.md": 630,
    "example-org/infra/CLAUDE.md": 135,
}


def repo_caps_config(extra=None):
    """stub_config carrying REPO_CAPS under context_lint.overrides."""
    mapping = {("context_lint", "overrides"): dict(REPO_CAPS)}
    mapping.update(extra or {})
    return stub_config(mapping)


def stub_config(mapping=None, ints=None, paths=None):
    """A config stub implementing the WHOLE surface the lib may reach.

    `get_int` is not optional. `digest_lib.max_lines` calls `config.get_int()`
    inside a bare `except Exception: return DEFAULT_MAX_LINES`, so a stub that
    defines only `get` makes it swallow an AttributeError and silently return
    the default — every "configured cap" test then passes while testing the
    fallback. Same shape for `get_path`.
    """
    mapping, ints, paths = mapping or {}, ints or {}, paths or {}

    class Cfg(object):
        def get(self, *keys, **kwargs):
            return mapping.get(keys, kwargs.get("default"))

        def get_int(self, *keys, **kwargs):
            return ints.get(keys, kwargs.get("default", 0))

        def get_path(self, *keys, **kwargs):
            return paths.get(keys, kwargs.get("default"))
    return Cfg()


class TestCapsForPath(Fixture):
    """D1 — a file outside git must still get a per-file cap."""

    def test_a_file_outside_any_repo_falls_to_the_default(self):
        path = self.write("CLAUDE.md", "x\n")
        whole, _core = growth_lib.caps_for_path(path)
        self.assertEqual(whole, context_lint_lib.DEFAULT_MAX_LINES)

    def test_the_premise_holds_this_fixture_really_is_outside_a_repo(self):
        # Every other test in this class depends on it, so assert it once
        # rather than assuming it. If mkdtemp ever lands inside a repo, the
        # None-key branch stops being exercised and nothing says so.
        path = self.write("CLAUDE.md", "x\n")
        self.assertIsNone(context_lint_lib.cap_key(path))

    def test_a_path_keyed_override_applies_outside_a_repo(self):
        # This is the capability that did not exist. cap_for() returns
        # default_max before consulting overrides when the key is None, and the
        # key is None by construction for every file outside a repo.
        path = self.write("CLAUDE.md", "x\n")
        cfg = stub_config({("context_growth", "overrides"): {path: 650}})
        whole, _core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(whole, 650)

    def test_a_path_keyed_override_survives_symlink_resolution(self):
        # macOS: /var is a symlink to /private/var, so a tmpdir path resolves
        # differently on each side. Realpath BOTH or the key matches nothing,
        # silently, in every temp directory — the exact trap cap_key documents.
        path = self.write("CLAUDE.md", "x\n")
        cfg = stub_config(
            {("context_growth", "overrides"): {os.path.realpath(path): 650}})
        whole, _core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(whole, 650)

    def test_a_tilde_prefixed_override_key_is_expanded(self):
        # toolkit_config.get() does NOT expand paths — only get_path() does,
        # and only for a scalar string. So `context_growth.overrides` comes
        # back with its keys verbatim and the lib must expanduser each one
        # itself. A config written the way a human writes it ("~/workspace/
        # CLAUDE.md") otherwise matches nothing, silently.
        path = self.write("CLAUDE.md", "x\n")
        saved = os.environ.get("HOME")
        os.environ["HOME"] = self.dir
        try:
            cfg = stub_config({("context_growth", "overrides"):
                               {"~/CLAUDE.md": 650}})
            whole, _core = growth_lib.caps_for_path(path, cfg)
        finally:
            if saved is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved
        self.assertEqual(whole, 650)

    def test_a_core_override_applies_outside_a_repo(self):
        path = self.write("TOOLING.md", "x\n")
        cfg = stub_config(
            {("context_growth", "core_overrides"): {os.path.realpath(path): 120}})
        whole, core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(core, 120)
        # `_whole` was DISCARDED in the pre-split version of this test, which
        # is why nothing anywhere asserted the tooling map has no whole-file
        # cap. Assert it here, where the value is already in hand.
        self.assertIsNone(whole, "the tooling map has no whole-file cap")

    def test_a_claude_md_gets_a_whole_file_cap_AND_the_context_lint_core_cap(self):
        path = self.write("CLAUDE.md", "x\n")
        whole, core = growth_lib.caps_for_path(path)
        self.assertEqual(whole, context_lint_lib.DEFAULT_MAX_LINES)
        self.assertEqual(core, CLAUDE_CORE_CAP)

    def test_a_tooling_md_gets_no_whole_file_cap_AND_the_digest_core_cap(self):
        path = self.write("TOOLING.md", "x\n")
        whole, core = growth_lib.caps_for_path(path)
        self.assertIsNone(whole)
        self.assertEqual(core, TOOLING_CORE_CAP)

    def test_the_template_is_NOT_exempt_from_the_whole_file_cap(self):
        """The asymmetry with `core_cap_for`, asserted so it is not "fixed".

        Both predicates used to read the same, and since 2026-09-15 they
        deliberately differ: `core_cap_for` resolves the TEMPLATE's Core
        through the digest budget (a template is capped by what it germinates
        into), while the whole-file exemption here stays map-only. Exempting
        the template would remove the only ceiling it has — the live map is a
        long reference that no whole-file cap can sensibly bound; the
        template is a bounded document well under DEFAULT_MAX_LINES.

        Without this test, collapsing the two into agreement looks like
        removing a duplicate and reads clean in review.
        """
        path = self.write("TOOLING.md.template", "x\n")
        whole, core = growth_lib.caps_for_path(path)
        self.assertEqual(whole, context_lint_lib.DEFAULT_MAX_LINES,
                         "the template keeps an ordinary whole-file cap")
        self.assertIsNotNone(whole)
        # And the other half of the asymmetry, so the pair reads as one rule.
        self.assertEqual(core, TOOLING_CORE_CAP,
                         "but its CORE is capped by the digest budget")

    def test_a_configured_tooling_max_lines_is_actually_consulted(self):
        # Requires get_int on the stub. Without it max_lines() swallows an
        # AttributeError and returns 60, so this test would assert the
        # fallback while claiming to test the configured path.
        path = self.write("TOOLING.md", "x\n")
        cfg = stub_config(ints={("tooling_digest", "max_lines"): 42})
        _whole, core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(core, 42)

    def test_a_broken_config_falls_back_rather_than_raising(self):
        path = self.write("CLAUDE.md", "x\n")

        class Broken(object):
            def get(self, *keys, **kwargs):
                raise ValueError("malformed")

            def get_int(self, *keys, **kwargs):
                raise ValueError("malformed")

            def get_path(self, *keys, **kwargs):
                raise ValueError("malformed")
        whole, _core = growth_lib.caps_for_path(path, Broken())
        self.assertEqual(whole, context_lint_lib.DEFAULT_MAX_LINES)


class TestCapsForPathInsideARepo(Fixture):
    """D1 rule 2 — the OTHER half of the resolver.

    Every fixture in the pre-split suite lived in a bare mkdtemp(), so
    `cap_key` returned None in every single test and the repo-keyed branch —
    `cap_for(key, *caps(config))`, half the resolver — had zero coverage.
    Step-8 mutation D only targeted the None-key branch too. These fixtures
    are real git repos, named to hit the shipped override map, so the
    assertion is on a value only a correct call can produce.
    """

    def test_a_repo_keyed_override_resolves_through_context_lint(self):
        repo = self.repo("claude-code-toolkit")
        path = os.path.join(repo, "CLAUDE.md")
        with open(path, "w") as handle:
            handle.write("x\n")
        self.assertEqual(context_lint_lib.cap_key(path),
                         "claude-code-toolkit/CLAUDE.md")
        whole, _core = growth_lib.caps_for_path(path, repo_caps_config())
        # The repo's own override (630), NOT DEFAULT_MAX_LINES: only
        # cap_for(key, *caps(config)) yields this. The assertNotEqual is what
        # keeps the test honest — with a per-repo cap equal to the default,
        # this could not tell the repo-keyed branch from the default one.
        self.assertEqual(
            whole, REPO_CAPS["example-org/claude-code-toolkit/CLAUDE.md"])
        self.assertNotEqual(whole, context_lint_lib.DEFAULT_MAX_LINES)

    def test_a_tighter_repo_override_also_resolves(self):
        # Negative control. Without it, a resolver returning one fixed large
        # number for every repo-keyed file satisfies the test above.
        repo = self.repo("infra")
        path = os.path.join(repo, "CLAUDE.md")
        with open(path, "w") as handle:
            handle.write("x\n")
        whole, _core = growth_lib.caps_for_path(path, repo_caps_config())
        self.assertEqual(whole, REPO_CAPS["example-org/infra/CLAUDE.md"])
        self.assertNotEqual(whole, context_lint_lib.DEFAULT_MAX_LINES)

    def test_an_unknown_repo_falls_to_the_default_not_to_None(self):
        repo = self.repo("some-repo-we-have-no-opinion-about")
        path = os.path.join(repo, "CLAUDE.md")
        with open(path, "w") as handle:
            handle.write("x\n")
        whole, _core = growth_lib.caps_for_path(path)
        self.assertEqual(whole, context_lint_lib.DEFAULT_MAX_LINES)

    def test_a_versioned_file_does_NOT_get_a_second_path_keyed_ratchet(self):
        # D1 rule 2 in words: "A versioned file already has a working ratchet;
        # it does not get a second one." A context_growth.overrides entry for
        # an in-repo path must be ignored, or the two maps silently compete.
        repo = self.repo("claude-code-toolkit")
        path = os.path.join(repo, "CLAUDE.md")
        with open(path, "w") as handle:
            handle.write("x\n")
        cfg = repo_caps_config({("context_growth", "overrides"):
                                {os.path.realpath(path): 9999}})
        whole, _core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(
            whole, REPO_CAPS["example-org/claude-code-toolkit/CLAUDE.md"])

    def test_a_repo_keyed_core_cap_resolves_through_core_caps(self):
        repo = self.repo("claude-code-toolkit")
        path = os.path.join(repo, "CLAUDE.md")
        with open(path, "w") as handle:
            handle.write(self.core(10))
        _whole, core = growth_lib.caps_for_path(path)
        self.assertEqual(core, CLAUDE_CORE_CAP)


class TestCapsForPathMemory(Fixture):
    """D-M1 — a memory entry gets a whole-file cap of 280 and NO Core cap,
    resolved BEFORE the CLAUDE.md / TOOLING.md branching in `caps_for_path`."""

    def test_a_memory_entry_gets_280_and_the_core_half_is_None(self):
        path = self.memory_file()
        whole, core = growth_lib.caps_for_path(path)
        self.assertEqual(whole, 280)
        # assertIsNone, not assertFalse: this repo's own documented trap —
        # assertFalse passes for 0 and False too, and neither means "there is
        # no Core cap for this file."
        self.assertIsNone(core)

    def test_a_memory_entry_inside_a_git_repo_still_resolves_to_280_none(self):
        # The branch where cap_key is non-None — the memory check must win
        # before the repo-keyed CLAUDE.md-family caps are ever consulted.
        repo = self.repo("some-repo")
        path = self.memory_file(root=repo)
        whole, core = growth_lib.caps_for_path(path)
        self.assertEqual(whole, 280)
        self.assertIsNone(core)

    def test_an_overrides_entry_raises_a_memory_files_whole_file_cap(self):
        path = self.memory_file()
        cfg = stub_config({("context_growth", "overrides"):
                           {os.path.realpath(path): 500}})
        whole, core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(whole, 500)
        self.assertIsNone(core)

    def test_a_core_overrides_entry_does_not_reintroduce_a_core_cap(self):
        # core_overrides raises the Core cap for CLAUDE.md/TOOLING.md; for a
        # memory file it must be a no-op, since D-M1 gives it no Core cap at
        # all — not "no Core cap unless someone configures one."
        path = self.memory_file()
        cfg = stub_config({("context_growth", "core_overrides"):
                           {os.path.realpath(path): 120}})
        whole, core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(whole, 280)
        self.assertIsNone(core)

    def test_a_configured_memory_max_lines_is_actually_consulted(self):
        # G5. Copies test_a_configured_tooling_max_lines_is_actually_consulted's
        # shape: without get_int on the stub, memory_max_lines() would swallow
        # an AttributeError and return 280, so this test would assert the
        # fallback while claiming to test the configured path.
        path = self.memory_file()
        cfg = stub_config(ints={("context_growth", "memory_max_lines"): 150})
        whole, _core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(whole, 150)

    def test_a_nonsense_memory_max_lines_is_ignored_not_applied(self):
        # Merge Review: the clamp in memory_max_lines had no test at all, and
        # deleting it left the whole suite green. It is reachable from a real
        # config, not just a stub — toolkit_config.get_int returns 0 verbatim
        # for {"context_growth": {"memory_max_lines": 0}} — and a cap of 0
        # makes _rose_over_cap true for every memory edit that adds a line,
        # which is the constant-nag failure this repo treats as a defect.
        #
        # One case per clause, so mutating any single clause is caught rather
        # than being covered by a neighbour:
        #   0, -5   -> `value <= 0`
        #   True    -> `isinstance(value, bool)`   (bool is an int, and > 0)
        #   "280"   -> `not isinstance(value, int)`
        for nonsense in (0, -5, True, "280"):
            with self.subTest(configured=nonsense):
                cfg = stub_config(
                    ints={("context_growth", "memory_max_lines"): nonsense})
                self.assertEqual(growth_lib.memory_max_lines(cfg),
                                 growth_lib.DEFAULT_MEMORY_MAX_LINES)

    def test_the_memory_cap_clamp_reaches_the_resolved_cap(self):
        # The clause above asserts the function. This asserts the wiring, so a
        # clamp that is correct but bypassed by caps_for_path still fails.
        path = self.memory_file()
        cfg = stub_config(ints={("context_growth", "memory_max_lines"): 0})
        whole, core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(whole, growth_lib.DEFAULT_MEMORY_MAX_LINES)
        self.assertIsNone(core)


class TestMemoryRoute(unittest.TestCase):
    """memory-distill's Step 0 bands, relative to whatever the cap is."""

    def test_default_cap_keeps_281_to_299_vs_300_plus(self):
        cap = growth_lib.DEFAULT_MEMORY_MAX_LINES
        self.assertEqual(growth_lib.memory_route(280, cap), "under-cap")
        self.assertEqual(growth_lib.memory_route(281, cap), "delete-pass")
        self.assertEqual(growth_lib.memory_route(299, cap), "delete-pass")
        self.assertEqual(growth_lib.memory_route(300, cap), "distill")

    def test_a_300_line_file_under_a_400_cap_is_not_over_cap(self):
        self.assertEqual(growth_lib.memory_route(300, 400), "under-cap")

    def test_the_bands_move_with_a_lower_cap(self):
        self.assertEqual(growth_lib.memory_route(151, 150), "delete-pass")
        self.assertEqual(growth_lib.memory_route(170, 150), "distill")


class TestCapCommandMatchesTheNotice(unittest.TestCase):
    """`claude-context-growth --cap` and the PostToolUse notice, both run as
    the real executable against one config file, agree on where "over cap"
    starts -- default, lower, higher and per-file caps."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="growth-cap-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        memory = os.path.join(self.dir, "proj", "memory")
        os.makedirs(memory)
        self.path = os.path.join(memory, "a-decision.md")
        self.config = os.path.join(self.dir, "config.json")
        self.env = dict(os.environ, HOME=self.dir,
                        CLAUDE_TOOLKIT_CONFIG=self.config,
                        CLAUDE_CONTEXT_GROWTH_STATE=os.path.join(
                            self.dir, "state.json"))

    def configure(self, section):
        if section is None:
            return
        with open(self.config, "w") as handle:
            json.dump({"context_growth": section}, handle)

    def lines(self, n):
        with open(self.path, "w") as handle:
            handle.write("x\n" * n)

    def notice(self):
        proc = subprocess.run(
            [sys.executable, EXE], input=json.dumps(
                {"tool_input": {"file_path": self.path}}),
            capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return "over the memory distillation cap" in proc.stdout

    def cap(self):
        proc = subprocess.run(
            [sys.executable, EXE, "--cap", self.path],
            capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        match = re.search(r": (\d+) lines, cap (\d+) -> (\S+)$",
                          proc.stdout.strip())
        self.assertIsNotNone(match, proc.stdout)
        return int(match.group(1)), int(match.group(2)), match.group(3)

    def assert_agree(self, section, expected_cap):
        self.configure(section)
        self.lines(1)
        _lines, cap, _route = self.cap()
        self.assertEqual(cap, expected_cap)
        self.lines(cap - 1)
        self.notice()  # seeds the notice's state below the cap
        self.lines(cap)
        self.assertFalse(self.notice())
        self.assertEqual(self.cap(), (cap, cap, "under-cap"))
        self.lines(cap + 1)
        self.assertTrue(self.notice())
        self.assertEqual(self.cap(), (cap + 1, cap, "delete-pass"))
        self.lines(cap + growth_lib.DISTILL_MARGIN)
        self.assertEqual(self.cap()[2], "distill")

    def test_default_cap(self):
        self.assert_agree(None, 280)

    def test_lower_cap(self):
        self.assert_agree({"memory_max_lines": 150}, 150)

    def test_higher_cap(self):
        self.assert_agree({"memory_max_lines": 400}, 400)

    def test_per_file_override(self):
        self.assert_agree({"overrides": {self.path: 500}}, 500)

    def test_300_lines_under_a_400_cap_is_silent_and_under_cap(self):
        self.configure({"memory_max_lines": 400})
        self.lines(299)
        self.notice()
        self.lines(300)
        self.assertFalse(self.notice())
        self.assertEqual(self.cap(), (300, 400, "under-cap"))

    def test_a_non_memory_path_is_refused_loudly(self):
        other = os.path.join(self.dir, "CLAUDE.md")
        with open(other, "w") as handle:
            handle.write("x\n")
        proc = subprocess.run(
            [sys.executable, EXE, "--cap", other],
            capture_output=True, text=True, env=self.env, timeout=60)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")
        self.assertIn("not a readable memory entry", proc.stderr)


class TestMemoryDistillReadsTheCap(unittest.TestCase):
    """The skill follows the command's route instead of a fixed number."""

    def setUp(self):
        with open(os.path.join(REPO, "skills", "continuity", "memory-distill",
                               "SKILL.md"), encoding="utf-8") as handle:
            text = handle.read()
        start = text.index("## Step 0")
        self.step0 = text[start:text.index("\n## ", start + 1)]

    def test_step_0_runs_the_cap_command(self):
        self.assertIn("tools/context-growth/claude-context-growth\" --cap",
                      self.step0)

    def test_step_0_names_every_route(self):
        for route in ("under-cap", "delete-pass", "distill"):
            self.assertIn("`%s`" % route, self.step0)

    def test_no_fixed_cap_is_left_in_the_skill(self):
        self.assertNotIn("280", self.step0)
        self.assertNotIn("281", self.step0)


class TestCapsForPathIndex(Fixture):
    """The index gets NO whole-file cap and NO Core cap:
    the char cap lives in `index_max_chars`, not here, because this
    function's contract is lines."""

    def test_outside_a_repo_is_none_none(self):
        path = self.memory_file(name="MEMORY.md")
        whole, core = growth_lib.caps_for_path(path)
        self.assertIsNone(whole)
        self.assertIsNone(core)

    def test_inside_a_git_repo_is_still_none_none(self):
        repo = self.repo("some-repo")
        path = self.memory_file(root=repo, name="MEMORY.md")
        whole, core = growth_lib.caps_for_path(path)
        self.assertIsNone(whole)
        self.assertIsNone(core)

    def test_line_overrides_do_not_apply_to_the_index(self):
        # context_growth.overrides / core_overrides are LINE maps; the index
        # has exactly one knob, memory_lint.max_index_chars, and it is not
        # consulted here at all.
        path = self.memory_file(name="MEMORY.md")
        cfg = stub_config(mapping={
            ("context_growth", "overrides"): {os.path.realpath(path): 50},
            ("context_growth", "core_overrides"): {os.path.realpath(path): 5},
        })
        whole, core = growth_lib.caps_for_path(path, cfg)
        self.assertIsNone(whole)
        self.assertIsNone(core)


class TestCapsForPathTopic(Fixture):
    """(None, None) always: no whole-file cap, no Core cap.
    The char cap is the fixed `lint_lib.TOPIC_MAX_CHARS`, not a `caps_for_path`
    return, checked immediately after the `is_memory_index` early return."""

    def test_outside_a_repo_is_none_none(self):
        path = self.topic_file()
        whole, core = growth_lib.caps_for_path(path)
        self.assertIsNone(whole)
        self.assertIsNone(core)

    def test_inside_a_git_repo_is_still_none_none(self):
        repo = self.repo("some-repo")
        path = self.topic_file(root=repo)
        whole, core = growth_lib.caps_for_path(path)
        self.assertIsNone(whole)
        self.assertIsNone(core)

    def test_overrides_do_not_apply_to_a_topic_file(self):
        # context_growth.overrides / core_overrides are LINE maps; a topic
        # file has exactly one knob, the fixed TOPIC_MAX_CHARS, and it is not
        # consulted here at all.
        path = self.topic_file()
        cfg = stub_config(mapping={
            ("context_growth", "overrides"): {os.path.realpath(path): 50},
            ("context_growth", "core_overrides"): {os.path.realpath(path): 5},
        })
        whole, core = growth_lib.caps_for_path(path, cfg)
        self.assertIsNone(whole)
        self.assertIsNone(core)


class TestIndexMaxChars(unittest.TestCase):
    """The index's char cap, shared with memory-lint
    through `lint_lib.max_index_chars`."""

    def test_no_config_is_the_shipped_default(self):
        self.assertEqual(growth_lib.index_max_chars(None),
                         lint_lib.DEFAULT_MAX_INDEX_CHARS)

    def test_a_configured_value_is_consulted(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 100})
        self.assertEqual(growth_lib.index_max_chars(cfg), 100)

    def test_nonpositive_values_fall_back_to_the_default(self):
        # lint_lib._threshold's own clamp reaches here -- no re-validation
        # needed in growth_lib.
        for nonsense in (0, -5):
            with self.subTest(configured=nonsense):
                cfg = stub_config(
                    ints={("memory_lint", "max_index_chars"): nonsense})
                self.assertEqual(growth_lib.index_max_chars(cfg),
                                 lint_lib.DEFAULT_MAX_INDEX_CHARS)

    def test_hermeticity_with_a_positive_control(self):
        # P4: index_max_chars(None) must NOT reach the live config, unlike
        # lint_lib.max_index_chars(None). The positive control proves the
        # None path DOES reach the live config in THIS test -- without it, a
        # version of index_max_chars that also read live config would pass
        # this test for the wrong reason (nothing would distinguish "never
        # reads live config" from "config happens to say the default").
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False)
        json.dump({"memory_lint": {"max_index_chars": 10}}, handle)
        handle.close()
        orig_path = toolkit_config.CONFIG_PATH
        toolkit_config.CONFIG_PATH = handle.name
        toolkit_config.load(refresh=True)
        try:
            self.assertEqual(lint_lib.max_index_chars(None), 10,
                             "control: the None path DOES reach the live "
                             "config in this test")
            self.assertEqual(growth_lib.index_max_chars(None),
                             lint_lib.DEFAULT_MAX_INDEX_CHARS,
                             "growth_lib must never call "
                             "lint_lib.max_index_chars(None) -- it reads the "
                             "live config, and growth_lib's contract is that "
                             "the executable passes config down")
        finally:
            toolkit_config.CONFIG_PATH = orig_path
            toolkit_config.load(refresh=True)
            os.unlink(handle.name)


class TestIndexMaxLines(unittest.TestCase):
    """The index's LINE cap, mirroring
    `TestIndexMaxChars` exactly. `lint_lib.max_index_lines` shares
    `memory_lint.max_index_lines` with memory-lint, so the two tools cannot
    disagree."""

    def test_no_config_is_the_shipped_default(self):
        self.assertEqual(growth_lib.index_max_lines(None),
                         lint_lib.DEFAULT_MAX_INDEX_LINES)

    def test_a_configured_value_is_consulted(self):
        cfg = stub_config(ints={("memory_lint", "max_index_lines"): 150})
        self.assertEqual(growth_lib.index_max_lines(cfg), 150)

    def test_nonpositive_values_fall_back_to_the_default(self):
        # lint_lib._threshold's own clamp reaches here -- no re-validation
        # needed in growth_lib.
        for nonsense in (0, -3):
            with self.subTest(configured=nonsense):
                cfg = stub_config(
                    ints={("memory_lint", "max_index_lines"): nonsense})
                self.assertEqual(growth_lib.index_max_lines(cfg),
                                 lint_lib.DEFAULT_MAX_INDEX_LINES)

    def test_a_config_whose_lookup_raises_falls_back_to_the_default(self):
        # AC2.5's third case: a broken config must not propagate the
        # exception, and must not silently read as "no config at all"
        # either -- both paths land on the same shipped default here, so the
        # observable behaviour is identical, but the exception must be
        # swallowed rather than raised.
        class Broken(object):
            def get_int(self, *keys, **kwargs):
                raise ValueError("malformed")
        self.assertEqual(growth_lib.index_max_lines(Broken()),
                         lint_lib.DEFAULT_MAX_INDEX_LINES)

    def test_hermeticity_with_a_positive_control(self):
        # P4: index_max_lines(None) must NOT reach the live config, unlike
        # lint_lib.max_index_lines(None). The positive control proves the
        # None path DOES reach the live config in THIS test -- without it, a
        # version of index_max_lines that also read live config would pass
        # this test for the wrong reason.
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False)
        json.dump({"memory_lint": {"max_index_lines": 11}}, handle)
        handle.close()
        orig_path = toolkit_config.CONFIG_PATH
        toolkit_config.CONFIG_PATH = handle.name
        toolkit_config.load(refresh=True)
        try:
            self.assertEqual(lint_lib.max_index_lines(None), 11,
                             "control: the None path DOES reach the live "
                             "config in this test")
            self.assertEqual(growth_lib.index_max_lines(None),
                             lint_lib.DEFAULT_MAX_INDEX_LINES,
                             "growth_lib must never call "
                             "lint_lib.max_index_lines(None) -- it reads the "
                             "live config, and growth_lib's contract is that "
                             "the executable passes config down")
        finally:
            toolkit_config.CONFIG_PATH = orig_path
            toolkit_config.load(refresh=True)
            os.unlink(handle.name)


class TestObserveMemoryIndex(Fixture):
    """`observe` records `chars` for the index only."""

    def test_the_index_records_chars_in_characters_not_bytes(self):
        text = "— row\n" * 10  # em dash: makes chars and bytes differ
        path = self.memory_file(name="MEMORY.md", text=text)
        result = growth_lib.observe(path)
        self.assertEqual(result["chars"], len(text))
        self.assertNotEqual(
            len(text), len(text.encode("utf-8")),
            "fixture must be non-ASCII, or this test cannot tell chars from "
            "bytes")
        self.assertNotEqual(result["chars"], len(text.encode("utf-8")))

    def test_an_ordinary_memory_entry_has_no_chars_key(self):
        path = self.memory_file(name="a-decision.md", text="x\n")
        result = growth_lib.observe(path)
        self.assertNotIn("chars", result)

    def test_the_index_records_harness_lines_distinct_from_lines(self):
        # AC2.7 -- the harness count is used, and it disagrees with the
        # pre-existing `lines` field exactly at the boundary this cap fires
        # on: two trailing blank lines do not count for the harness.
        text = "x\n" * 199 + "\n\n"
        path = self.memory_file(name="MEMORY.md", text=text)
        result = growth_lib.observe(path)
        self.assertEqual(result["harness_lines"], 199)
        self.assertEqual(result["lines"], 201)


class TestObserveTopicFile(unittest.TestCase):
    """`observe` records `chars` for a topic file, the same as it
    does for the index (this class's setUp mirrors TestObserveMemoryIndex,
    which is a Fixture; this one only needs a throwaway temp dir)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="growth-topic-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_topic_file_records_chars(self):
        text = "— row\n" * 10  # em dash: makes chars and bytes differ
        d = os.path.join(self.dir, "memory", "topics")
        os.makedirs(d)
        path = os.path.join(d, "t.md")
        with open(path, "w") as handle:
            handle.write(text)
        result = growth_lib.observe(path)
        self.assertEqual(result["chars"], len(text))

    def test_a_topic_file_records_harness_lines(self):
        text = "x\n" * 199 + "\n\n"
        d = os.path.join(self.dir, "memory", "topics")
        os.makedirs(d)
        path = os.path.join(d, "t.md")
        with open(path, "w") as handle:
            handle.write(text)
        result = growth_lib.observe(path)
        self.assertEqual(result["harness_lines"], 199)
        self.assertEqual(result["lines"], 201)


class TestIndexNotice(Fixture):
    """The index's own PostToolUse notice. Fires on the same
    `_rose_over_cap` contract as every other surface this hook watches, but in
    CHARACTERS: the count must RISE and land over `memory_lint.max_index_chars`.
    """

    def index(self, text):
        return self.memory_file(name="MEMORY.md", text=text)

    def test_first_sight_at_1500_is_silent_and_records_chars(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 1500)
        self.assertIsNone(self.edit(path, cfg))
        self.assertEqual(self.state[os.path.realpath(path)]["chars"], 1500)

    def test_900_to_1100_speaks_with_the_expected_wording(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 900)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 1100)
        notice = self.edit(path, cfg)
        self.assertIsNotNone(notice)
        self.assertIn("MEMORY.md is over its index budget", notice)
        self.assertIn("1100 chars, cap 1000", notice)
        self.assertIn("100 over", notice)
        self.assertIn("+200", notice)
        self.assertIn("routed: false", notice)
        self.assertIn("~25000", notice)

    def test_900_to_990_is_silent_rose_but_under_cap(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 900)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 990)
        self.assertIsNone(self.edit(path, cfg))

    def test_1200_to_1100_is_silent_a_shrink_while_over(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 1200)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 1100)
        self.assertIsNone(self.edit(path, cfg))

    def test_1200_to_1300_speaks_grew_while_over(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 1200)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 1300)
        self.assertIsNotNone(self.edit(path, cfg))

    def test_1200_to_1200_identical_rewrite_is_silent(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 1200)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 1200)
        self.assertIsNone(self.edit(path, cfg))

    def test_default_cap_no_config_23990_to_24010_speaks(self):
        path = self.index("x" * 23990)
        self.assertIsNone(self.edit(path, None))
        self.rewrite(path, "x" * 24010)
        notice = self.edit(path, None)
        self.assertIsNotNone(notice)
        self.assertIn("cap 24000", notice)

    def test_past_truncation_says_rows_are_being_lost(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 24990)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 25010)
        notice = self.edit(path, cfg)
        self.assertIsNotNone(notice)
        self.assertIn("rows are being lost", notice)
        self.assertNotIn("~25000", notice)

    def test_a_configured_cap_past_truncation_cannot_silence_the_loss(self):
        """Merge Review's repro: a cap of 30000 must not let the index cross
        25,000 silently. lint_lib clamps it to 24999, so the crossing speaks
        and names the loss."""
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 30000})
        path = self.index("x" * 24900)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 25100)
        notice = self.edit(path, cfg)
        self.assertIsNotNone(notice)
        self.assertIn("cap %d" % (lint_lib.INDEX_TRUNCATION_CHARS - 1), notice)
        self.assertIn("rows are being lost", notice)

    def test_under_25000_does_not_say_rows_are_being_lost(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 900)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 1100)
        notice = self.edit(path, cfg)
        self.assertIsNotNone(notice)
        self.assertNotIn("rows are being lost", notice)

    def test_the_exact_boundary_24999_to_25000_is_past(self):
        # 25,000 counts as past it (the `>=` boundary, matching
        # lint_lib.findings' own comparison).
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 24999)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 25000)
        notice = self.edit(path, cfg)
        self.assertIsNotNone(notice)
        self.assertIn("rows are being lost", notice)

    def test_no_line_row_no_core_row_no_ref_row(self):
        # N3b: the body is now a small number of very long lines rather than
        # many short ones -- growing the CHAR count without also crossing
        # the new index LINES cap, which would otherwise legitimately fire
        # alongside chars and falsify this test's own "no lines row" claim.
        core1 = "<!-- CORE:START -->\nA rule.\n<!-- CORE:END -->\n"
        core2 = "<!-- CORE:START -->\nA rule; see below.\n<!-- CORE:END -->\n"
        first = core1 + "x" * 700 + "\n"
        grown = core2 + "x" * 650 + "\n" + "x" * 650 + "\n"
        # Preconditions -- so the fixture is proven to carry what the test
        # claims, rather than trusted from the plan's own measurement.
        self.assertTrue(len(first) < 1300 < len(grown))
        self.assertGreater(grown.count("\n"), first.count("\n"))
        self.assertEqual(core_block.dangling_refs(core_block.extract(first)), [])
        self.assertNotEqual(
            core_block.dangling_refs(core_block.extract(grown)), [])
        self.assertLessEqual(lint_lib.harness_line_count(grown),
                             lint_lib.DEFAULT_MAX_INDEX_LINES,
                             "the fixture must stay under the lines cap, or "
                             "the new lines axis legitimately fires too")

        cfg13 = stub_config(ints={("memory_lint", "max_index_chars"): 1300})
        path = self.index(first)
        self.assertIsNone(self.edit(path, cfg13))
        self.rewrite(path, grown)
        notice = self.edit(path, cfg13)
        self.assertIsNotNone(notice)
        self.assertIn("chars", notice)
        self.assertNotIn("lines", notice)
        self.assertNotIn("Core", notice)
        self.assertNotIn("cross-reference", notice)
        self.assertGreater(growth_lib.observe(path)["core_lines"], 0,
                           "the fixture must carry a Core the suppressed "
                           "rows could have reported on")

    def test_a_stale_prior_with_no_chars_key_is_silent_then_speaks(self):
        # P12: a state entry written before this change has no `chars` key.
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 1500)
        self.state[os.path.realpath(path)] = {
            "lines": 1, "core_lines": 0, "refs": []}
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 1600)
        self.assertIsNotNone(self.edit(path, cfg))

    # -- N3b, LINES axis (AC2.1-AC2.3, AC2.6, AC2.8) ------------------------
    # Content here uses "row\n" repeats, not "x" * N: the harness count needs
    # real newlines to move at all, and the char count of a few thousand
    # "row\n"s stays far under the default 24,000-char cap, so these fixtures
    # exercise the lines axis in isolation.

    def test_index_lines_189_to_191_speaks_with_the_expected_wording(self):
        # AC2.1 -- chars well under cap; only the lines axis crosses.
        path = self.index("row\n" * 189)
        self.assertIsNone(self.edit(path, None))
        self.rewrite(path, "row\n" * 191)
        notice = self.edit(path, None)
        self.assertIsNotNone(notice)
        self.assertIn("MEMORY.md is over its index budget", notice)
        self.assertIn("191 lines, cap 190 — 1 over (+2 since last check)",
                      notice)
        self.assertIn(
            "the harness drops the END of the index past ~200 lines, "
            "silently, with no error.", notice)
        self.assertNotIn(" chars,", notice)

    def test_index_lines_180_to_190_is_silent_at_the_cap(self):
        # AC2.2 -- at the cap, not over it.
        path = self.index("row\n" * 180)
        self.assertIsNone(self.edit(path, None))
        self.rewrite(path, "row\n" * 190)
        self.assertIsNone(self.edit(path, None))

    def test_index_lines_191_to_195_speaks(self):
        # AC2.2 -- _rose_over_cap's existing semantics, unchanged.
        path = self.index("row\n" * 191)
        self.assertIsNone(self.edit(path, None), "first sight is silent")
        self.rewrite(path, "row\n" * 195)
        self.assertIsNotNone(self.edit(path, None))

    def test_index_lines_195_to_193_is_silent_a_shrink_while_over(self):
        # AC2.2 -- fell, even though still over cap.
        path = self.index("row\n" * 195)
        self.assertIsNone(self.edit(path, None), "first sight is silent")
        self.rewrite(path, "row\n" * 193)
        self.assertIsNone(self.edit(path, None))

    def test_index_lines_199_to_202_shows_the_rows_are_being_lost_band(self):
        # AC2.3
        path = self.index("row\n" * 199)
        self.assertIsNone(self.edit(path, None), "first sight is silent")
        self.rewrite(path, "row\n" * 202)
        notice = self.edit(path, None)
        self.assertIsNotNone(notice)
        self.assertIn("rows are being lost in every session now", notice)

    def test_index_lines_at_exactly_200_is_not_the_lost_band_and_201_is(self):
        # The harness cuts only PAST 200 (strict `>`, read from 2.1.280): a
        # 200-line index loses nothing, so it must get the not-yet band.
        path = self.index("row\n" * 195)
        self.assertIsNone(self.edit(path, None), "first sight is silent")
        self.rewrite(path, "row\n" * 200)
        notice = self.edit(path, None)
        self.assertIsNotNone(notice)
        self.assertIn("with no error", notice)
        self.assertNotIn("rows are being lost", notice)
        self.rewrite(path, "row\n" * 201)
        notice = self.edit(path, None)
        self.assertIn("rows are being lost in every session now", notice)

    def test_an_index_with_html_comments_gets_the_caveat_on_its_lines_band(self):
        # Merge Review: the loader strips comments before counting, so the
        # count is an upper bound over commented text, and the band says so.
        path = self.index("row\n" * 189)
        self.assertIsNone(self.edit(path, None))
        self.rewrite(path, "row\n" * 191 + "<!--\na\nb\n-->\n")
        notice = self.edit(path, None)
        self.assertIsNotNone(notice)
        self.assertIn(lint_lib.COMMENT_CAVEAT.strip(), notice)

    def test_an_index_without_comments_gets_no_caveat(self):
        path = self.index("row\n" * 189)
        self.assertIsNone(self.edit(path, None))
        self.rewrite(path, "row\n" * 195)
        notice = self.edit(path, None)
        self.assertIsNotNone(notice)
        self.assertNotIn("HTML comments", notice)

    def test_an_index_edit_crossing_both_caps_shows_both_bands(self):
        # AC2.6
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("row\n" * 189)
        self.assertIsNone(self.edit(path, cfg))
        grown = "row12345\n" * 195
        self.assertGreater(len(grown), 1000)
        self.rewrite(path, grown)
        notice = self.edit(path, cfg)
        self.assertIsNotNone(notice)
        self.assertIn("195 lines, cap 190 — 5 over (+6 since last check)",
                      notice)
        self.assertIn(
            "1755 chars, cap 1000 — 755 over (+999 since last check)",
            notice)
        self.assertIn(
            "the harness drops the END of the index past ~200 lines, "
            "silently, with no error.", notice)
        self.assertIn(
            "the harness drops rows off the END past ~25000 chars, "
            "silently, with no error.", notice)

    def test_a_stale_prior_with_no_harness_lines_key_is_silent_then_speaks(
            self):
        # AC2.8 -- a state entry written before this change has no
        # `harness_lines` key at all; that must not crash, and must be
        # silent on the lines axis for the one edit it costs.
        path = self.index("row\n" * 195)
        self.state[os.path.realpath(path)] = {
            "lines": 1, "core_lines": 0, "refs": []}
        self.assertIsNone(self.edit(path, None))
        self.rewrite(path, "row\n" * 196)
        self.assertIsNotNone(self.edit(path, None))

    def test_the_index_notice_is_not_the_memory_notice(self):
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 900)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 1100)
        notice = self.edit(path, cfg)
        self.assertNotIn("memory-distill", notice.lower())
        self.assertNotIn("trim", notice.lower())

        # The existing memory-entry branch stays untouched.
        mem_path = self.memory_file(name="a.md", text="x\n" * 100)
        self.assertIsNone(self.edit(mem_path))
        self.rewrite(mem_path, "x\n" * 300)
        mem_notice = self.edit(mem_path)
        self.assertIn("memory-distill", mem_notice)


class TestOutsideAlwaysNotice(Fixture):
    """The new-leaf-row-outside-Always backstop, and the
    two-level swap of the index-budget trailer. `TestExecutable` below carries
    the same thing through the hook subprocess."""

    def index(self, text):
        return self.memory_file(name="MEMORY.md", text=text)

    def test_a_new_leaf_row_outside_always_speaks(self):
        self.topic_file(name="t.md", text="x\n")  # gives this fixture a topics/ dir
        path = self.index("## Always\n\n## Topics\n")
        self.assertIsNone(self.edit(path), "first sight must be silent")
        self.rewrite(path, "## Always\n\n## Topics\n- [X](x.md) — h\n")
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("new leaf row outside Always", notice)
        self.assertIn(
            "x.md — its row belongs in a topic file under memory/topics/ "
            "(t.md)", notice)
        self.assertIn(
            "In the two-level layout a new memory's row goes in its topic "
            "file; MEMORY.md takes leaf rows only in its Always section.",
            notice)

    def test_the_topic_list_skips_an_md_directory(self):
        # Code Review finding: a `*.md` DIRECTORY under topics/ was offered to
        # the reader as a destination topic file.
        topic = self.topic_file(name="t.md", text="x\n")
        os.mkdir(os.path.join(os.path.dirname(topic), "d.md"))
        path = self.index("## Always\n\n## Topics\n")
        self.assertIsNone(self.edit(path), "first sight must be silent")
        self.rewrite(path, "## Always\n\n## Topics\n- [X](x.md) — h\n")
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn(
            "x.md — its row belongs in a topic file under memory/topics/ "
            "(t.md)", notice)
        self.assertNotIn("d.md", notice)

    def test_the_same_row_added_under_always_instead_is_silent(self):
        self.topic_file(name="t.md", text="x\n")
        path = self.index("## Always\n\n## Topics\n")
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "## Always\n- [X](x.md) — h\n\n## Topics\n")
        self.assertIsNone(self.edit(path))

    def test_a_second_edit_leaving_the_outside_always_set_unchanged_is_silent(self):
        # AC3 -- fires on the TRANSITION, not the state: the leaf set ({x.md})
        # is identical across both edits, only its hook text changed.
        self.topic_file(name="t.md", text="x\n")
        path = self.index("## Always\n\n## Topics\n- [X](x.md) — h\n")
        self.assertIsNone(self.edit(path), "first sight must be silent")
        self.rewrite(path, "## Always\n\n## Topics\n- [X](x.md) — h, more\n")
        self.assertIsNone(self.edit(path))

    def test_a_flat_era_prior_with_no_outside_always_key_is_silent_once(self):
        # AC4 -- a state entry written before this change has no
        # `outside_always` key at all; a missing key on EITHER side means
        # "not observed in this layout", not "nothing here".
        self.topic_file(name="t.md", text="x\n")
        path = self.index("## Always\n\n## Topics\n- [X](x.md) — h\n")
        self.state[os.path.realpath(path)] = {
            "lines": 1, "core_lines": 0, "refs": [],
            "chars": len(open(path).read())}
        self.assertIsNone(self.edit(path), "a flat-era prior must not read "
                                           "every current name as new")
        self.assertIn("outside_always", self.state[os.path.realpath(path)],
                      "the key must be recorded for the NEXT edit")

    def test_flat_layout_has_no_outside_always_key_and_stays_silent(self):
        # AC5 -- no topics/ dir at all.
        path = self.index("## Always\n\n## Topics\n")
        self.assertIsNone(self.edit(path), "first sight must be silent")
        self.rewrite(path, "## Always\n\n## Topics\n- [X](x.md) — h\n")
        self.assertIsNone(self.edit(path), "flat layout has no backstop here")
        self.assertNotIn("outside_always", growth_lib.observe(path))

    def test_flat_layout_keeps_the_retire_a_row_trailer_verbatim(self):
        # AC5, other half -- the string must not have moved a single byte.
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("x" * 900)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "x" * 1100)
        notice = self.edit(path, cfg)
        self.assertIsNotNone(notice)
        self.assertIn(
            "Retire a row for every row you add — prefer one whose rule a "
            "CLAUDE.md already states. The memory file itself stays: put "
            "`routed: false` in its frontmatter so memory-lint does not "
            "report it as an orphan.", notice)

    def test_two_level_index_crossing_cap_gets_the_two_level_trailer(self):
        # AC6
        self.topic_file(name="t.md", text="x\n")
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("## Always\n\n## Topics\n" + "x" * 900)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path, "## Always\n\n## Topics\n" + "x" * 1100)
        notice = self.edit(path, cfg)
        self.assertIsNotNone(notice)
        self.assertIn(
            "Move rows out of MEMORY.md into topic files — every row here "
            "is paid in every session, and a topic file costs nothing "
            "until it is opened.", notice)
        self.assertNotIn("routed: false", notice)

    def test_size_growth_and_a_new_leaf_row_in_the_same_edit_are_one_notice(self):
        # AC7
        self.topic_file(name="t.md", text="x\n")
        cfg = stub_config(ints={("memory_lint", "max_index_chars"): 1000})
        path = self.index("## Always\n\n## Topics\n" + "x" * 900)
        self.assertIsNone(self.edit(path, cfg))
        self.rewrite(path,
                     "## Always\n\n## Topics\n- [X](x.md) — h\n" + "x" * 1100)
        notice = self.edit(path, cfg)
        self.assertIsNotNone(notice)
        self.assertIn("is over its index budget", notice)   # the SIZE header
        self.assertIn("cap 1000", notice)                    # the size row
        self.assertIn(
            "x.md — its row belongs in a topic file under memory/topics/ "
            "(t.md)", notice)                                # the leaf-row line
        # Both fixes apply, so both trailers are printed -- and neither is
        # the flat layout's retire-a-row advice.
        self.assertIn("Move rows out of MEMORY.md into topic files", notice)
        self.assertIn("MEMORY.md takes leaf rows only in its Always section",
                      notice)
        self.assertNotIn("routed: false", notice)


class TestTopicNotice(Fixture):
    """The topic-file cap notice, on both the CHARS and LINES axes.
    Every fixture here is rebased relative to
    `lint_lib.TOPIC_MAX_CHARS` (25000, no config key) rather than a literal --
    a future cap change must not quietly turn an over-cap fixture into an
    under-cap one, so each over/at-cap test asserts its own precondition."""

    LIMIT = lint_lib.TOPIC_MAX_CHARS

    def test_just_under_cap_to_just_over_speaks_with_the_expected_wording(self):
        path = self.topic_file(text="x" * (self.LIMIT - 10))
        self.assertIsNone(self.edit(path))
        grown = "x" * (self.LIMIT + 1)
        self.assertGreater(len(grown), self.LIMIT)
        self.rewrite(path, grown)
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("over its topic-file cap", notice)
        self.assertIn(
            "%d chars, cap %d — 1 over (+11 since last check)"
            % (self.LIMIT + 1, self.LIMIT),
            notice)
        self.assertIn("Split the topic", notice)

    def test_at_the_boundary_is_silent(self):
        path = self.topic_file(text="x" * (self.LIMIT - 10))
        self.assertIsNone(self.edit(path))
        at_cap = "x" * self.LIMIT
        self.assertEqual(len(at_cap), self.LIMIT)
        self.rewrite(path, at_cap)
        self.assertIsNone(self.edit(path))

    def test_shrink_while_over_is_silent(self):
        over = "x" * (self.LIMIT + 500)
        self.assertGreater(len(over), self.LIMIT)
        path = self.topic_file(text=over)
        self.assertIsNone(self.edit(path))
        shrunk = "x" * (self.LIMIT + 400)
        self.assertGreater(len(shrunk), self.LIMIT,
                           "must stay OVER the cap, or this stops testing "
                           "the shrink-while-over branch")
        self.rewrite(path, shrunk)
        self.assertIsNone(self.edit(path))

    def test_first_sight_over_cap_is_silent(self):
        over = "x" * (self.LIMIT + 1000)
        self.assertGreater(len(over), self.LIMIT)
        path = self.topic_file(text=over)
        self.assertIsNone(self.edit(path))

    def test_the_notice_names_no_memory_distill_or_index_wording(self):
        path = self.topic_file(text="x" * (self.LIMIT - 10))
        self.assertIsNone(self.edit(path))
        grown = "x" * (self.LIMIT + 1)
        self.assertGreater(len(grown), self.LIMIT)
        self.rewrite(path, grown)
        notice = self.edit(path)
        self.assertNotIn("distill", notice)
        self.assertNotIn("routed: false", notice)
        self.assertNotIn("Retire a row", notice)

    def test_topic_lines_199_to_201_speaks(self):
        # AC2.4, lines axis.
        path = self.topic_file(text="row\n" * 199)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "row\n" * 201)
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("over its topic-file cap", notice)
        self.assertIn("201 lines, cap 200 — 1 over (+2 since last check)",
                      notice)
        self.assertIn("Split the topic", notice)

    def test_topic_lines_at_the_boundary_is_silent(self):
        path = self.topic_file(text="row\n" * 199)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "row\n" * lint_lib.TOPIC_MAX_LINES)
        self.assertIsNone(self.edit(path))


class TestMisplacedNotice(Fixture):
    """The edit-time notice: a memory landing in (or leaving) the
    wrong tier in THIS edit, two-level layout only. Mirrors
    `TestOutsideAlwaysNotice`'s shape, which this reuses `lint_lib`'s type
    machinery alongside."""

    def index(self, text):
        return self.memory_file(name="MEMORY.md", text=text)

    def _leaf(self, name="leaf.md", mtype="feedback"):
        return self.memory_file(
            root=None, name=name, text="---\ntype: %s\n---\nbody\n" % mtype)

    def test_design_review_probe_removing_the_always_row_speaks(self):
        # AC1 -- a feedback leaf in both Always and a topic; removing its
        # Always row makes it reachable ONLY from the topic, which is now
        # misplaced for a rule type.
        self._leaf("leaf.md", "feedback")
        self.topic_file(name="t.md", text="- [Leaf](../leaf.md) — hook\n")
        path = self.index(
            "## Always\n- [Leaf](leaf.md) — hook\n\n## Topics\n"
            "- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(path), "first sight must be silent")
        self.rewrite(
            path, "## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn(
            "[context-growth] MEMORY.md leaves a memory in the wrong tier.",
            notice)
        self.assertIn("leaf.md", notice)
        self.assertIn("type: feedback", notice)
        self.assertIn(
            "reachable only from a topic — it never fires there", notice)

    def test_a_second_identical_index_write_is_silent(self):
        # AC3 -- transition, not state: once the leaf's misplacement is
        # already reflected in both prior and current, an unchanged write
        # produces no new diff.
        self._leaf("leaf.md", "feedback")
        self.topic_file(name="t.md", text="- [Leaf](../leaf.md) — hook\n")
        path = self.index(
            "## Always\n- [Leaf](leaf.md) — hook\n\n## Topics\n"
            "- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(path))
        self.rewrite(
            path, "## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        self.assertIsNotNone(self.edit(path))
        self.rewrite(
            path, "## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(path))

    def test_a_topic_edit_adding_a_feedback_row_with_no_always_speaks(self):
        # AC2(a)
        self._leaf("leaf.md", "feedback")
        self.index("## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        topic = self.topic_file(name="t.md", text="")
        self.assertIsNone(self.edit(topic), "first sight of the topic")
        self.rewrite(topic, "- [Leaf](../leaf.md) — hook\n")
        notice = self.edit(topic)
        self.assertIsNotNone(notice)
        self.assertIn("leaves a memory in the wrong tier", notice)
        self.assertIn("leaf.md", notice)

    def test_a_topic_edit_adding_a_project_row_with_no_always_is_silent(self):
        # AC2(b) -- a project (fact) memory with no Always row is correctly
        # placed; adding its topic row must not speak.
        self._leaf("leaf.md", "project")
        self.index("## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        topic = self.topic_file(name="t.md", text="")
        self.assertIsNone(self.edit(topic))
        self.rewrite(topic, "- [Leaf](../leaf.md) — hook\n")
        self.assertIsNone(self.edit(topic))

    def test_index_edit_adding_an_always_row_for_a_project_memory_speaks(self):
        # AC3a -- facts direction: a project memory gaining an Always row
        # becomes newly misplaced (a fact paid every session).
        self._leaf("leaf.md", "project")
        self.topic_file(name="t.md", text="- [Leaf](../leaf.md) — hook\n")
        path = self.index("## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(path), "first sight")
        self.rewrite(
            path,
            "## Always\n- [Leaf](leaf.md) — hook\n\n## Topics\n"
            "- [T](topics/t.md) — hook\n")
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("leaf.md", notice)
        self.assertIn("type: project", notice)

    def test_index_edit_adding_an_always_row_for_a_feedback_memory_is_silent(self):
        # AC3a, other half -- adding an Always row FIXES a feedback memory's
        # placement rather than breaking it, so nothing new fires.
        self._leaf("leaf.md", "feedback")
        self.topic_file(name="t.md", text="- [Leaf](../leaf.md) — hook\n")
        path = self.index("## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(path), "first sight")
        self.rewrite(
            path,
            "## Always\n- [Leaf](leaf.md) — hook\n\n## Topics\n"
            "- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(path))

    def test_memory_entry_retype_to_feedback_while_only_in_a_topic_speaks(self):
        # AC3b -- retyping the edited file's OWN entry, no Always row.
        leaf = self._leaf("leaf.md", "reference")
        self.topic_file(name="t.md", text="- [Leaf](../leaf.md) — hook\n")
        self.index("## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(leaf), "first sight of the memory entry")
        self.rewrite(leaf, "---\ntype: feedback\n---\nbody\n")
        notice = self.edit(leaf)
        self.assertIsNotNone(notice)
        self.assertIn("leaf.md", notice)
        self.assertIn("leaves a memory in the wrong tier", notice)

    def test_memory_entry_retype_to_feedback_with_an_always_row_is_silent(self):
        # AC3b, other half -- the same retype where the file ALSO has an
        # Always row: feedback + Always is correctly placed.
        leaf = self._leaf("leaf.md", "reference")
        self.topic_file(name="t.md", text="x\n")  # just needs topics/ to exist
        self.index(
            "## Always\n- [Leaf](leaf.md) — hook\n\n## Topics\n"
            "- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(leaf), "first sight of the memory entry")
        self.rewrite(leaf, "---\ntype: feedback\n---\nbody\n")
        self.assertIsNone(self.edit(leaf))

    def test_flat_layout_never_gets_a_misplaced_key_or_notice(self):
        # AC4 -- no topics/ dir at all.
        path = self.index("## Always\n- [Leaf](leaf.md) — hook\n")
        self.assertIsNone(self.edit(path))
        self.assertNotIn("misplaced", growth_lib.observe(path))
        self.rewrite(path, "## Always\n")
        self.assertIsNone(self.edit(path))

    def test_the_baseline_is_PER_DIRECTORY_not_per_edited_file(self):
        # FIX 3 (Code Review) -- Design Review's probe extended: once the
        # index's OWN notice fires and updates its prior snapshot, the
        # TOPIC's and the index's own NEXT snapshot were still stale under
        # the old per-edited-file comparison, and an unchanged write to
        # either re-announced the same placement. Warm all three surfaces
        # first, so none of the three edits below is a first-file-sight.
        leaf = self._leaf("leaf.md", "reference")
        topic = self.topic_file(name="t.md", text="- [Leaf](../leaf.md) — hook\n")
        index = self.index("## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(leaf), "warm: first sight of the memory entry")
        self.assertIsNone(self.edit(topic), "warm: first sight of the topic")
        self.assertIsNone(self.edit(index), "warm: first sight of the index")

        self.rewrite(leaf, "---\ntype: feedback\n---\nbody\n")
        notice = self.edit(leaf)
        self.assertIsNotNone(notice)
        self.assertIn("leaf.md", notice)
        self.assertEqual(
            notice.count("leaves a memory in the wrong tier"), 1,
            "exactly one notice for this one retype")

        # Unchanged writes to the OTHER two surfaces, with no new
        # placement since the retype: both silent under the per-directory
        # baseline. The old per-edited-file comparison re-announced here,
        # because neither the topic's nor the index's own prior snapshot
        # had been updated by the leaf's own notice above.
        self.assertIsNone(self.edit(topic))
        self.assertIsNone(self.edit(index))

    def test_a_misplacement_filtered_out_on_a_topic_write_is_pending_for_the_index(
            self):
        # A misplacement the dir-wide scan turns up while editing a topic
        # that does NOT own the leaf is filtered out of THAT topic's own
        # notice, but must not be treated as already announced -- it stays
        # pending until an edit that CAN speak for it (here, the index)
        # comes along.
        self.memory_file(name="x.md", text="---\ntype: feedback\n---\nbody\n")
        t1 = self.topic_file(name="t1.md", text="x\n")  # does not own x.md
        self.topic_file(name="t2.md", text="- [X](../x.md) — hook\n")
        index = self.index(
            "## Always\n- [X](x.md) — hook\n\n## Topics\n"
            "- [T1](topics/t1.md) — hook\n- [T2](topics/t2.md) — hook\n")
        self.assertIsNone(self.edit(index), "warm: first sight of the index")
        self.assertIsNone(self.edit(t1), "warm: first sight of t1")

        # x.md loses its Always row: now reachable only via t2, misplaced.
        self.rewrite(
            index,
            "## Always\n\n## Topics\n"
            "- [T1](topics/t1.md) — hook\n- [T2](topics/t2.md) — hook\n")

        # t1 doesn't own x.md, so its own notice is filtered to nothing --
        # even though the dir-wide scan now sees x.md as misplaced.
        self.assertIsNone(self.edit(t1))

        notice = self.edit(index)
        self.assertIsNotNone(notice)
        self.assertIn("x.md", notice)

    def test_fixing_then_rebreaking_a_misplacement_is_announced_again(self):
        self._leaf("leaf.md", "feedback")
        self.topic_file(name="t.md", text="- [Leaf](../leaf.md) — hook\n")
        path = self.index(
            "## Always\n- [Leaf](leaf.md) — hook\n\n## Topics\n"
            "- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(path), "first sight")

        # Break it.
        self.rewrite(path, "## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("leaf.md", notice)

        # Fix it: move the row back into Always.
        self.rewrite(
            path, "## Always\n- [Leaf](leaf.md) — hook\n\n## Topics\n"
            "- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(path), "fixing it must not itself speak")

        # Re-break it, the same way.
        self.rewrite(path, "## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        notice_again = self.edit(path)
        self.assertIsNotNone(notice_again)
        self.assertIn("leaf.md", notice_again)

    def test_a_suppressed_first_sight_notice_leaves_the_misplacement_pending(
            self):
        # FIX 1 (Merge Review) -- a placement computed as `announced` must
        # join the per-directory baseline ONLY if the notice carrying it is
        # actually returned. Here the topic's own first-ever write already
        # references the feedback leaf, so its dir-wide scan sees the new
        # misplacement -- but build() suppresses THAT edit's notice under
        # the first-sight guard. Consuming it into the baseline anyway would
        # leave a following index edit silent forever, even though nothing
        # was ever announced.
        self._leaf("leaf.md", "feedback")
        topic = self.topic_file(name="t.md", text="x\n")  # topics/ exists,
                                                            # no ref yet
        index = self.index(
            "## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        self.assertIsNone(self.edit(index), "seed: first sight of the index")

        # The topic's OWN first-ever write already references the leaf --
        # suppressed by the first-sight guard, so this must return None.
        self.rewrite(topic, "- [Leaf](../leaf.md) — hook\n")
        self.assertIsNone(self.edit(topic), "first sight of the topic")

        # The misplacement must still be pending: the index can speak for
        # it now that both surfaces have a prior observation.
        notice = self.edit(index)
        self.assertIsNotNone(
            notice, "the misplacement was consumed by the suppressed "
                    "topic edit and never announced")
        self.assertIn("leaf.md", notice)


class TestSufficiencyInTheHook(Fixture):
    """C14's other half — no lint path reaches the unversioned TOOLING.md."""

    def _tooling(self, body):
        return "<!-- CORE:START -->\n%s\n<!-- CORE:END -->\n" % body

    def test_a_new_dangling_ref_in_a_core_speaks(self):
        path = self.write("TOOLING.md", self._tooling("A rule."))
        self.edit(path)
        with open(path, "w") as handle:
            handle.write(self._tooling("A rule; see below."))
        out = self.edit(path)
        self.assertIsNotNone(out)
        self.assertIn("see below", out)

    def test_a_standing_dangling_ref_does_not_speak_twice(self):
        path = self.write("TOOLING.md", self._tooling("A rule; see below."))
        self.edit(path)
        self.assertIsNone(self.edit(path))


class TestSilence(Fixture):
    def test_silent_for_an_out_of_scope_path(self):
        """TWO edits, with growth between them.

        MEASURED while executing this plan: with ONE edit this test passed
        even with `in_scope` shadowed to always return True, because a single
        invocation returns at the first-sight branch and never reaches the cap
        comparison. It was asserting first-sight silence under a name claiming
        to cover scope — the same defect shape as the in-cap executable test,
        and the mutation that should have graded the scope gate survived it.
        """
        path = self.write("README.md", "x\n" * 900)
        self.assertIsNone(self.edit(path), "first sight is silent for anything")
        self.rewrite(path, "x\n" * 9000)          # grew, and far over any cap
        self.assertIsNone(self.edit(path),
                          "an out-of-scope file must stay silent even on "
                          "growth past every cap in the config")
        # And the scope gate must have kept it out of state entirely.
        self.assertEqual(self.state, {},
                         "an out-of-scope file was recorded in state: %r"
                         % self.state)

    def test_silent_on_an_IN_SCOPE_file_that_does_not_exist(self):
        # The pre-split version passed "gone.md", which `in_scope` rejects on
        # basename before `observe` is ever called — so it tested the
        # out-of-scope branch a second time under a name claiming to cover the
        # unreadable-file branch. An in-scope name that is absent is what
        # reaches `observe` returning None.
        missing = os.path.join(self.dir, "CLAUDE.md")
        self.assertTrue(growth_lib.in_scope(missing), "premise: name is in scope")
        self.assertFalse(os.path.exists(missing), "premise: file is absent")
        self.assertIsNone(self.edit(missing))

    def test_silent_on_an_in_scope_file_that_cannot_be_decoded(self):
        path = os.path.join(self.dir, "CLAUDE.md")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe\x00binary\x00")
        self.assertIsNone(self.edit(path))

    def test_silent_on_a_malformed_tool_input(self):
        for bad in (None, {}, {"file_path": None}, {"file_path": ""},
                    "not a dict", [], 7):
            self.assertIsNone(growth_lib.build(bad, state=self.state), repr(bad))


class TestStateFile(Fixture):
    def _cfg(self):
        target = os.path.join(self.dir, "state.json")
        return stub_config(paths={("context_growth", "state_path"): target}), target

    def test_round_trips_through_disk(self):
        # The path must EXIST. `write_state` is specified to drop entries whose
        # file no longer exists, so the pre-split version of this test — which
        # wrote {"/a": ...} and read it back — specified behaviour mutually
        # exclusive with `test_entries_for_deleted_files_are_pruned_on_write`
        # in this same class. No implementation could satisfy both.
        cfg, target = self._cfg()
        alive = self.write("CLAUDE.md", "x\n" * 3)
        self.assertTrue(growth_lib.write_state({alive: {"lines": 3}}, cfg))
        self.assertEqual(growth_lib.read_state(cfg)[alive]["lines"], 3)
        self.assertTrue(os.path.exists(target))

    def test_a_missing_state_file_reads_as_empty_not_a_crash(self):
        cfg, _target = self._cfg()
        self.assertEqual(growth_lib.read_state(cfg), {})

    def test_a_corrupt_state_file_reads_as_empty_not_a_crash(self):
        cfg, target = self._cfg()
        with open(target, "w") as handle:
            handle.write("{not json")
        self.assertEqual(growth_lib.read_state(cfg), {})

    def test_a_non_dict_state_file_reads_as_empty_not_a_crash(self):
        cfg, target = self._cfg()
        with open(target, "w") as handle:
            handle.write("[1, 2, 3]")
        self.assertEqual(growth_lib.read_state(cfg), {})

    def test_entries_for_deleted_files_are_pruned_on_write(self):
        cfg, _target = self._cfg()
        alive = self.write("CLAUDE.md", "x\n")
        growth_lib.write_state({alive: {"lines": 1},
                                "/nope/gone/CLAUDE.md": {"lines": 1}}, cfg)
        self.assertEqual(sorted(growth_lib.read_state(cfg)), [alive])

    def test_an_unwritable_state_path_returns_false_and_does_not_raise(self):
        cfg = stub_config(
            paths={("context_growth", "state_path"): "/proc/nonexistent/state.json"})
        alive = self.write("CLAUDE.md", "x\n")
        self.assertFalse(growth_lib.write_state({alive: {"lines": 1}}, cfg))

    def test_the_env_var_wins_over_config(self):
        # state_path resolves env FIRST. Nothing in the pre-split suite
        # discriminated the env branch from the shipped default: the four
        # tests that set the env var also pinned CLAUDE_TOOLKIT_CONFIG=
        # /nonexistent.json, so config carried no state_path either and the
        # two branches were observationally identical.
        cfg, from_config = self._cfg()
        from_env = os.path.join(self.dir, "from-env.json")
        saved = os.environ.get("CLAUDE_CONTEXT_GROWTH_STATE")
        os.environ["CLAUDE_CONTEXT_GROWTH_STATE"] = from_env
        try:
            resolved = growth_lib.state_path(cfg)
        finally:
            if saved is None:
                os.environ.pop("CLAUDE_CONTEXT_GROWTH_STATE", None)
            else:
                os.environ["CLAUDE_CONTEXT_GROWTH_STATE"] = saved
        self.assertEqual(resolved, from_env)
        self.assertNotEqual(resolved, from_config)

    def test_config_wins_over_the_shipped_default(self):
        cfg, from_config = self._cfg()
        saved = os.environ.pop("CLAUDE_CONTEXT_GROWTH_STATE", None)
        try:
            resolved = growth_lib.state_path(cfg)
        finally:
            if saved is not None:
                os.environ["CLAUDE_CONTEXT_GROWTH_STATE"] = saved
        self.assertEqual(resolved, from_config)
        self.assertNotIn(".claude/context-growth-state.json", resolved)

    def test_an_explicit_EMPTY_state_dict_is_not_replaced_by_the_state_file(self):
        """The falsy-sentinel trap, and it reaches the user's live state file.

        `build` is specified to use the caller's `state` when given, else
        `read_state(config)`. Written as `state = state or read_state(config)`,
        an empty dict — which is what every fixture passes on its first edit —
        is falsy, so the first `edit()` of every test in this suite reaches for
        `state_path(config)`. With `config=None` that is
        `~/.claude/context-growth-state.json`: the live file. Written as
        `state if state is not None else ...` it does not. No pre-split test
        could tell the two apart, because every fixture path was a fresh
        mkdtemp with no entry in the live state, so both branches returned
        None and both passed.
        """
        cfg, target = self._cfg()
        path = self.write("CLAUDE.md", "rule\n" * 947)
        with open(target, "w") as handle:
            json.dump({os.path.realpath(path):
                       {"lines": 500, "core_lines": None, "refs": []}}, handle)
        # If {} were replaced by the file, prior would be 500 lines and this
        # would SPEAK. An honoured empty dict means first sight, so: silence.
        self.assertIsNone(growth_lib.build({"file_path": path}, cfg, state={}))
        # Positive control: the file's contents ARE reachable, so the silence
        # above is the sentinel and not an unreadable state file.
        self.assertEqual(growth_lib.read_state(cfg)[os.path.realpath(path)]["lines"],
                         500)


class TestExecutable(Fixture):
    """The hook end to end. C16: env pinned, like every sibling suite."""

    def setUp(self):
        Fixture.setUp(self)
        self.state_file = os.path.join(self.dir, "s.json")

    def _state_env(self, **extra):
        env = dict(ENV, CLAUDE_CONTEXT_GROWTH_STATE=self.state_file)
        env.update(extra)
        return env

    def run_exe(self, stdin, env=None, exe=None):
        """Run the hook. `env` DEFAULTS TO the temp state path.

        There is deliberately no `or ENV` fallback. In the pre-split version
        the default was the bare module-level ENV — which pins the config but
        NOT the state path — and two tests took it, so they wrote to the user's
        real ~/.claude/context-growth-state.json. Making the hermetic env the
        default is what makes that structurally impossible rather than a thing
        each new test has to remember.
        """
        return subprocess.run([sys.executable, exe or EXE], input=stdin,
                              capture_output=True, text=True, timeout=60,
                              env=env if env is not None else self._state_env())

    def _payload(self, path, tool="Edit"):
        return json.dumps({"tool_name": tool, "tool_input": {"file_path": path}})

    def test_run_exe_cannot_silently_fall_back_to_an_unpinned_env(self):
        """The enforcement the old docstring only claimed.

        Its predecessor asserted that `_state_env()` contains the key and that
        its value starts with `self.dir` — both true by construction of the
        helper, in every state of the code including an empty growth_lib. So
        it could not fail, and its claim ("every subprocess test above passes
        `_state_env()`; this fails loudly if one stops") was false on arrival:
        two tests already did not. This reads the helper instead.

        PARSE, DO NOT GREP. The first cut of this test was a substring ban on
        the fallback expression, and it FAILED against correct code — because
        the prose in run_exe explaining why there is no such fallback contains
        that very expression. Same root cause as a guard satisfied BY the
        comment explaining it, just in the loud direction: a text search over
        source cannot tell code from prose about the code.
        """
        tree = ast.parse(textwrap.dedent(inspect.getsource(TestExecutable.run_exe)))
        fn = tree.body[0]
        # The module-level ENV must not be REFERENCED anywhere in the body.
        # A `env or ENV` fallback is an ast.Name; a docstring is not.
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        self.assertNotIn("ENV", names,
                         "run_exe references the module-level ENV, which pins "
                         "the config but NOT the state path")
        # And the default it DOES fall back to is the hermetic helper.
        attrs = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        self.assertIn("_state_env", attrs)

    def test_the_env_var_actually_redirects_where_state_is_written(self):
        # Hermeticity, behaviourally: the hook must write to the temp path.
        # If state_path ignored CLAUDE_CONTEXT_GROWTH_STATE, this file would
        # never appear and the write would land on the live state instead.
        self.assertFalse(os.path.exists(self.state_file))
        path = self.write("CLAUDE.md", "rule\n" * 900)
        done = self.run_exe(self._payload(path))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(os.path.exists(self.state_file),
                        "the hook did not honour CLAUDE_CONTEXT_GROWTH_STATE")
        self.assertIn(os.path.realpath(path), json.load(open(self.state_file)))

    def test_garbage_stdin_exits_zero_and_says_nothing(self):
        done = self.run_exe("not json")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "")

    def test_empty_payload_exits_zero_and_says_nothing(self):
        done = self.run_exe("{}")
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "")

    def test_an_in_cap_edit_that_GREW_produces_no_output_at_all(self):
        # Not an empty hookSpecificOutput — that trains the reader to skip this
        # hook's output entirely.
        #
        # Two invocations, and growth between them. The pre-split version
        # invoked the hook ONCE on a 10-line file, so `build` returned at the
        # first-sight branch and never reached the cap comparison the test is
        # named for.
        path = self.write("CLAUDE.md", "rule\n" * 10)
        self.run_exe(self._payload(path))              # record
        self.rewrite(path, "rule\n" * 700)             # grew, still under 800
        done = self.run_exe(self._payload(path))
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "",
                         "growth under cap must produce nothing at all")

    def test_growth_over_cap_emits_additional_context_naming_the_file(self):
        path = self.write("CLAUDE.md", "rule\n" * 900)
        first = self.run_exe(self._payload(path, "Write"))
        self.assertEqual(first.stdout.strip(), "", "first sight must be silent")
        self.rewrite(path, "rule\n" * 947)
        done = self.run_exe(self._payload(path, "Write"))
        self.assertEqual(done.returncode, 0)
        # Assert there IS output before parsing it, so a silent hook fails with
        # its own message rather than a JSONDecodeError from the parse below.
        self.assertNotEqual(done.stdout.strip(), "",
                            "the hook emitted nothing on growth past cap; "
                            "stderr: %s" % done.stderr)
        block = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(block["hookEventName"], "PostToolUse")
        context = block["additionalContext"]
        self.assertIn("CLAUDE.md", context)
        self.assertIn("947", context)      # new count
        self.assertIn("147", context)      # overage
        self.assertIn("+47", context)      # delta

    def test_state_persists_across_processes_so_GROWTH_is_caught(self):
        """The transition rule is worthless if state does not survive the
        process, and each hook invocation is its own process.

        The pre-split version recorded 500, shrank to 450, and asserted
        silence — but a shrink is silent in BOTH worlds: with state nothing
        rose, without it every invocation is first sight. It passed with
        `write_state` deleted entirely, which is exactly the property mutation
        C is supposed to grade. Growth is the discriminating direction: it can
        only speak if the prior count came back off disk.
        """
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.assertEqual(self.run_exe(self._payload(path)).stdout.strip(), "")
        self.rewrite(path, "rule\n" * 947)
        done = self.run_exe(self._payload(path))
        self.assertNotEqual(done.stdout.strip(), "",
                            "state did not survive the process")
        self.assertIn("947", json.loads(done.stdout)
                      ["hookSpecificOutput"]["additionalContext"])

    def test_a_shrink_stays_silent_across_processes(self):
        # Kept as its own test rather than as the persistence proof, which is
        # what it could never be.
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.run_exe(self._payload(path))
        self.rewrite(path, "rule\n" * 850)
        self.assertEqual(self.run_exe(self._payload(path)).stdout.strip(), "")

    def _config_env(self, section):
        target = os.path.join(self.dir, "config.json")
        with open(target, "w") as handle:
            json.dump(section, handle)
        return self._state_env(CLAUDE_TOOLKIT_CONFIG=target)

    def test_enabled_false_silences_the_hook(self):
        # `context_growth.enabled` existed only in prose: acceptance criterion
        # 10 checked that it was DOCUMENTED, and no fixture ever set it.
        env = self._config_env({"context_growth": {"enabled": False}})
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.run_exe(self._payload(path), env)
        self.rewrite(path, "rule\n" * 947)
        done = self.run_exe(self._payload(path), env)
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "",
                         "enabled:false did not silence the hook")

    def test_an_absent_enabled_key_leaves_the_hook_SPEAKING(self):
        # The other half, and the one that matters more: a default of False
        # would ship a hook that never fires, and every silence test above
        # would still pass.
        env = self._config_env({"context_growth": {}})
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.run_exe(self._payload(path), env)
        self.rewrite(path, "rule\n" * 947)
        done = self.run_exe(self._payload(path), env)
        self.assertNotEqual(done.stdout.strip(), "",
                            "enabled must default to True")

    def test_a_new_pathless_rules_file_speaks_through_the_hook(self):
        path = self.rules_file("row\n" * 10)
        payload = self._payload(path, "Write")
        first = self.run_exe(payload)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertNotEqual(first.stdout.strip(), "",
                            "a new pathless rules file must speak on first "
                            "sight; stderr: %s" % first.stderr)
        context = json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("has no paths:", context)
        second = self.run_exe(payload)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), "",
                         "a second identical run must be silent — warned once")

    def test_the_index_notice_fires_through_the_executable(self):
        env = self._config_env({"memory_lint": {"max_index_chars": 1000}})
        path = self.memory_file(name="MEMORY.md", text="x" * 900)
        self.run_exe(self._payload(path), env)
        self.rewrite(path, "x" * 1100)
        done = self.run_exe(self._payload(path), env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual(done.stdout.strip(), "", done.stderr)
        context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("index budget", context)

    def test_the_topic_notice_fires_through_the_executable(self):
        # Two successive payloads for a topic file crossing
        # the cap; the second prints a PostToolUse additionalContext.
        # Rebased relative to lint_lib.TOPIC_MAX_CHARS.
        path = self.topic_file(text="x" * (lint_lib.TOPIC_MAX_CHARS - 10))
        first = self.run_exe(self._payload(path))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.strip(), "", "first sight must be silent")
        grown = "x" * (lint_lib.TOPIC_MAX_CHARS + 1)
        self.assertGreater(len(grown), lint_lib.TOPIC_MAX_CHARS)
        self.rewrite(path, grown)
        done = self.run_exe(self._payload(path))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual(done.stdout.strip(), "", done.stderr)
        block = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(block["hookEventName"], "PostToolUse")
        self.assertIn("over its topic-file cap", block["additionalContext"])

    def test_the_outside_always_notice_fires_through_the_executable(self):
        # Two successive payloads for a two-level MEMORY.md,
        # the second adding a leaf row under Topics.
        self.topic_file(name="t.md", text="x\n")
        path = self.memory_file(name="MEMORY.md", text="## Always\n\n## Topics\n")
        first = self.run_exe(self._payload(path))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.strip(), "", "first sight must be silent")
        self.rewrite(path, "## Always\n\n## Topics\n- [X](x.md) — h\n")
        done = self.run_exe(self._payload(path))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual(done.stdout.strip(), "", done.stderr)
        block = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(block["hookEventName"], "PostToolUse")
        self.assertIn("new leaf row outside Always",
                     block["additionalContext"])

    def test_the_misplaced_notice_fires_through_the_executable(self):
        # Case (1) (the Design Review probe) through the
        # hook subprocess, with a sandbox state path.
        self.memory_file(name="leaf.md",
                         text="---\ntype: feedback\n---\nbody\n")
        self.topic_file(name="t.md", text="- [Leaf](../leaf.md) — hook\n")
        path = self.memory_file(
            name="MEMORY.md",
            text="## Always\n- [Leaf](leaf.md) — hook\n\n## Topics\n"
                 "- [T](topics/t.md) — hook\n")
        first = self.run_exe(self._payload(path))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.strip(), "", "first sight must be silent")
        self.rewrite(
            path, "## Always\n\n## Topics\n- [T](topics/t.md) — hook\n")
        done = self.run_exe(self._payload(path))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual(done.stdout.strip(), "", done.stderr)
        block = json.loads(done.stdout)["hookSpecificOutput"]
        self.assertEqual(block["hookEventName"], "PostToolUse")
        self.assertIn("leaves a memory in the wrong tier",
                     block["additionalContext"])
        self.assertIn("leaf.md", block["additionalContext"])

        # The baseline's pending -> announced promotion must reach the STATE
        # FILE, not just the in-memory dict. Every in-process test passes
        # `state=`, so `persist` is False there and only a real subprocess
        # sequence can see a promotion made after `write_state` ran: without
        # it, this unchanged third write re-announces the same leaf forever.
        again = self.run_exe(self._payload(path))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(again.stdout.strip(), "",
                         "an announced placement must not re-announce on an "
                         "unchanged write through the executable")


class TestThroughTheInstalledSymlink(Fixture):
    """`realpath`, not `abspath` — the bug that passes every test.

    The toolkit's own verification rule: "After
    any change to a hook, exercise it through `~/.local/bin/`, not just its
    tests. The `realpath` bug above passed every test in the suite and crashed
    every hook." Nothing in the pre-split plan did: `TestExecutable` spawns the
    executable at its real repo path, where `realpath` and `abspath` are
    identical, and no step ran `./install.sh` (correctly — that is forbidden
    from a worktree).

    A temp symlink reproduces the real installed shape without install.sh.
    """

    def test_the_hook_works_when_invoked_through_a_symlink(self):
        bin_dir = os.path.join(self.dir, "bin")
        os.makedirs(bin_dir)
        link = os.path.join(bin_dir, "claude-context-growth")
        os.symlink(EXE, link)

        env = dict(ENV, CLAUDE_CONTEXT_GROWTH_STATE=os.path.join(self.dir, "s.json"))
        path = self.write("CLAUDE.md", "rule\n" * 900)
        payload = json.dumps({"tool_name": "Edit",
                              "tool_input": {"file_path": path}})

        def run():
            return subprocess.run([sys.executable, link], input=payload,
                                  capture_output=True, text=True, timeout=60,
                                  env=env)

        first = run()
        # An abspath-based sys.path block imports nothing and the outer
        # try/except swallows it, so the tell is a silent exit 0 on a payload
        # that MUST speak on the second call.
        self.assertEqual(first.returncode, 0, first.stderr)
        self.rewrite(path, "rule\n" * 947)
        done = run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual(
            done.stdout.strip(), "",
            "the hook produced nothing through a symlink — the classic "
            "abspath-instead-of-realpath failure. stderr: %s" % done.stderr)
        self.assertIn("CLAUDE.md", json.loads(done.stdout)
                      ["hookSpecificOutput"]["additionalContext"])

    def test_the_index_notice_fires_through_a_symlink(self):
        """This is the test that proves P8 -- the `lint_lib` import resolves
        through `~/.local/bin`, not just through the repo-relative test run."""
        bin_dir = os.path.join(self.dir, "bin")
        os.makedirs(bin_dir)
        link = os.path.join(bin_dir, "claude-context-growth")
        os.symlink(EXE, link)

        config_path = os.path.join(self.dir, "config.json")
        with open(config_path, "w") as handle:
            json.dump({"memory_lint": {"max_index_chars": 1000}}, handle)

        env = dict(ENV, CLAUDE_CONTEXT_GROWTH_STATE=os.path.join(self.dir, "s.json"),
                   CLAUDE_TOOLKIT_CONFIG=config_path)
        path = self.memory_file(name="MEMORY.md", text="x" * 900)
        payload = json.dumps({"tool_name": "Edit",
                              "tool_input": {"file_path": path}})

        def run():
            return subprocess.run([sys.executable, link], input=payload,
                                  capture_output=True, text=True, timeout=60,
                                  env=env)

        first = run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.rewrite(path, "x" * 1100)
        done = run()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotEqual(
            done.stdout.strip(), "",
            "the index notice produced nothing through a symlink; "
            "stderr: %s" % done.stderr)
        self.assertIn("index budget", json.loads(done.stdout)
                      ["hookSpecificOutput"]["additionalContext"])


class TestMemoryNotice(Fixture):
    """The DISTILL notice, memory-scoped.

    A memory file gets its own header and trailer: DISTILL via the
    `memory-distill` skill, never "trim" — trimming is the behaviour this
    notice exists to replace. The TOOLING.md branch is asserted UNCHANGED
    here too (see `TestClaudeMdNotice` for the CLAUDE.md-family branch,
    which deliberately uses its own distill
    skill) — so a change to the memory branch cannot silently
    rewrite the context-file branch.
    """

    def test_a_memory_file_that_rises_over_cap_gets_a_distill_notice(self):
        path = self.memory_file(text="x\n" * 100)
        self.assertIsNone(self.edit(path), "first sight must be silent")
        self.rewrite(path, "x\n" * 300)
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("distill", notice.lower())
        self.assertIn("memory-distill", notice)

    def test_the_distill_notice_never_says_trim(self):
        path = self.memory_file(text="x\n" * 100)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "x\n" * 300)
        notice = self.edit(path)
        self.assertNotIn("trim", notice.lower())

    def test_a_rise_from_100_to_200_lines_is_silent(self):
        path = self.memory_file(text="x\n" * 100)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "x\n" * 200)
        self.assertIsNone(self.edit(path))

    def test_a_rise_from_200_to_250_lines_is_silent_the_DM2_test(self):
        """At the rejected 80% threshold (224 of 280) this rise WOULD fire —
        this is the assertion that fails if anyone reintroduces the
        percentage the design correction rejected."""
        path = self.memory_file(text="x\n" * 200)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "x\n" * 250)
        self.assertIsNone(self.edit(path))

    def test_an_over_cap_memory_file_that_shrinks_is_silent(self):
        path = self.memory_file(text="x\n" * 300)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "x\n" * 285)
        self.assertIsNone(self.edit(path))

    def test_an_over_cap_memory_file_that_grows_further_speaks(self):
        path = self.memory_file(text="x\n" * 300)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "x\n" * 320)
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("distill", notice.lower())

    def test_first_sight_of_a_memory_file_is_silent_and_state_records_it(self):
        path = self.memory_file(text="x\n" * 300)
        self.assertIsNone(self.edit(path))
        self.assertIn(os.path.realpath(path), self.state)

    def test_the_claude_md_trailer_now_points_at_the_distill_skill(self):
        # "Trim it before the session ends" is false for CLAUDE.md
        # (TestClaudeMdNotice requires that) — the
        # CLAUDE.md family branches to claude-md-distill instead.
        path = self.write("CLAUDE.md", "rule\n" * 900)
        self.edit(path)
        self.rewrite(path, "rule\n" * 947)
        notice = self.edit(path)
        self.assertIn("claude-md-distill", notice)
        self.assertNotIn("Trim it before the session ends", notice)

    def test_a_memory_file_gets_no_core_row_and_no_dangling_ref_row(self):
        """D-M1: a stray CORE:START/END pair inside a memory file must not
        bill it against the CLAUDE.md family's Core budget, and must not
        spawn a dangling-reference row either — both are asserted here even
        though the fixture's own text carries a dangling ref ("see below")."""
        first = "<!-- CORE:START -->\nA rule.\n<!-- CORE:END -->\n" + "x\n" * 290
        path = self.memory_file(text=first)
        self.assertIsNone(self.edit(path))
        grown = ("<!-- CORE:START -->\nA rule; see below.\n<!-- CORE:END -->\n"
                 + "x\n" * 310)
        self.rewrite(path, grown)
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertNotIn("Core", notice)
        self.assertNotIn("cross-reference", notice)


class TestClaudeMdNotice(Fixture):
    """The CLAUDE.md family points at `claude-md-distill`, never at
    the "Trim it" wording; `TOOLING.md` keeps "Trim it" because its remedy
    (demoting into its own body) is different and the skill does not apply.
    """

    def test_a_claude_md_notice_names_the_skill_and_the_archive(self):
        path = self.claude_md(text="x\n" * 100)
        self.assertIsNone(self.edit(path), "first sight must be silent")
        self.rewrite(path, "x\n" * 900)
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("claude-md-distill", notice)
        self.assertIn("CLAUDE.history.md", notice)
        self.assertNotIn("Trim it before the session ends", notice)

    def test_the_template_gets_the_same_branch(self):
        # NB os.path.splitext("CLAUDE.md.template") -> ("CLAUDE.md", ".template"),
        # so history_path_for gives CLAUDE.md.history.md, NOT
        # CLAUDE.md.template.history.md. Assert whatever the implementation
        # actually produces and make the two agree -- do not assume either.
        path = self.claude_md(name="CLAUDE.md.template", text="x\n" * 100)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "x\n" * 900)
        notice = self.edit(path)
        self.assertIn("claude-md-distill", notice)
        self.assertIn("CLAUDE.md.history.md", notice)

    def test_tooling_md_keeps_the_trim_wording(self):
        # Its archive is its own body and it has no distill skill. Pointing it
        # at one would be a notice naming a tool that does not apply.
        #
        # DEVIATION from the brief's literal fixture: the brief grew this file
        # by whole-file lines alone (100 -> 3000), which can never speak — the
        # tooling map has NO whole-file cap by design (`caps_for_path`'s `is_map`
        # branch), asserted independently by
        # `test_a_tooling_md_growing_past_the_whole_file_cap_is_silent` above. A
        # Core-crossing rewrite is the only axis that can produce a notice for
        # this file at all, so that is what is exercised here instead.
        path = self.write("TOOLING.md", self.core(50, marker="DIGEST"))
        if not growth_lib.in_scope(path, None):
            self.skipTest("TOOLING.md is not the configured tooling map here")
        self.assertIsNone(self.edit(path))
        self.rewrite(path, self.core(70, marker="DIGEST"))
        notice = self.edit(path)
        self.assertIn("Trim it before the session ends", notice)
        self.assertNotIn("claude-md-distill", notice)

    def test_a_memory_notice_is_untouched(self):
        path = self.memory_file(text="x\n" * 100)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "x\n" * 300)
        notice = self.edit(path)
        self.assertIn("memory-distill", notice)
        self.assertNotIn("claude-md-distill", notice)


class TestIsClaudeMd(unittest.TestCase):
    """`assertIs(..., False)` deliberately, not `assertFalse` — the repo's
    `Optional[bool]` rule: `assertFalse` passes for `None` too and would erase
    the distinction under test."""

    def test_is_claude_md_never_raises_and_excludes_the_archive(self):
        self.assertIs(growth_lib.is_claude_md("/x/CLAUDE.md"), True)
        self.assertIs(growth_lib.is_claude_md("/x/CLAUDE.md.template"), True)
        self.assertIs(growth_lib.is_claude_md("/x/CLAUDE.history.md"), False)
        # The TEMPLATE's own archive: history_path_for("CLAUDE.md.template")
        # yields stem "CLAUDE.md" -> "CLAUDE.md.history.md", so this file
        # really appears in the toolkit root once the template is distilled.
        # It is the ONLY archive name that discriminates `in` from
        # `startswith` -- "CLAUDE.history.md" is False under both, so a test
        # using only that one cannot catch a prefix-match implementation.
        self.assertIs(growth_lib.is_claude_md("/x/CLAUDE.md.history.md"), False)
        self.assertIs(growth_lib.is_claude_md("/x/TOOLING.md"), False)
        self.assertIs(growth_lib.is_claude_md(None), False)


class TestIsRulesFile(unittest.TestCase):
    """Change 2 — every True/False example from the plan, verbatim."""

    def test_true_examples(self):
        for path in ("/w/.claude/rules/a.md",
                     "/w/.claude/rules/sub/b.md",
                     "/Users/x/.claude/rules/c.md"):
            self.assertTrue(growth_lib.is_rules_file(path), path)

    def test_false_examples(self):
        for path in ("/w/.claude/rules/a.txt",
                     "/w/claude/rules/a.md",
                     "/w/.claude/rulesX/a.md",
                     "/w/.claude/a.md",
                     "", None):
            self.assertFalse(growth_lib.is_rules_file(path), repr(path))


class TestIsRulesFileConfigDir(unittest.TestCase):
    """`is_rules_file`/`in_scope` also recognise a
    relocated `$CLAUDE_CONFIG_DIR/rules/` (see lib/claude_home.py).

    Every test here sets/unsets CLAUDE_CONFIG_DIR explicitly and restores
    whatever was ambient in tearDown -- hermetic, per the repo's own rule on
    tests touching environment state."""

    def setUp(self):
        self._previous = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.tmp = tempfile.mkdtemp(prefix="growth-cfgdir-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = os.path.join(self.tmp, "cfg")

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self._previous

    def test_ac2_1_relocated_rules_dir(self):
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
        self.assertTrue(growth_lib.is_rules_file(
            os.path.join(self.cfg, "rules", "a.md")))
        self.assertFalse(growth_lib.is_rules_file(
            os.path.join(self.cfg, "rules-old", "a.md")))
        self.assertFalse(growth_lib.is_rules_file(
            os.path.join(self.cfg, "rules", "a.txt")))

    def test_ac2_2_env_unset_old_rule_still_holds(self):
        self.assertFalse(growth_lib.is_rules_file(
            os.path.join(self.cfg, "rules", "a.md")))
        self.assertTrue(growth_lib.is_rules_file("/any/.claude/rules/a.md"))

    def test_ac2_3_in_scope_delegates_to_is_rules_file(self):
        os.environ["CLAUDE_CONFIG_DIR"] = self.cfg
        path = os.path.join(self.cfg, "rules", "a.md")
        self.assertEqual(growth_lib.in_scope(path), growth_lib.is_rules_file(path))
        self.assertTrue(growth_lib.in_scope(path))

    def test_config_dir_with_a_dot_segment_still_matches(self):
        """Code Review FIX 1 -- CLAUDE_CONFIG_DIR=<tmp>/./cfg must still
        recognise a real rules file under it (the un-normalized string
        prefix compare misses it: user_rules_dir() carries the literal
        "/./" while a caller's path is already clean), and must still
        reject the rules-old sibling."""
        os.environ["CLAUDE_CONFIG_DIR"] = os.path.join(self.tmp, ".", "cfg")
        self.assertTrue(growth_lib.is_rules_file(
            os.path.join(self.cfg, "rules", "r.md")))
        self.assertFalse(growth_lib.is_rules_file(
            os.path.join(self.cfg, "rules-old", "r.md")))

    def test_symlinked_config_dir_matches_both_logical_and_physical_path(self):
        """Merge Review FIX 2 -- CLAUDE_CONFIG_DIR pointed at a SYMLINK to
        the real config dir. The logical path (through the symlink) already
        matches via the existing normalized-prefix compare; the PHYSICAL
        path (the symlink's target) only matches once `is_rules_file` also
        compares realpaths. `rules-old` sitting next to the real `rules/`
        dir must still be rejected on the physical side too."""
        real_cfg = os.path.join(self.tmp, "real-cfg")
        os.makedirs(os.path.join(real_cfg, "rules"))
        with open(os.path.join(real_cfg, "rules", "r.md"), "w") as handle:
            handle.write("body\n")
        link_cfg = os.path.join(self.tmp, "link-cfg")
        os.symlink(real_cfg, link_cfg)
        os.environ["CLAUDE_CONFIG_DIR"] = link_cfg

        self.assertTrue(growth_lib.is_rules_file(
            os.path.join(link_cfg, "rules", "r.md")))
        self.assertTrue(growth_lib.is_rules_file(
            os.path.join(real_cfg, "rules", "r.md")))
        self.assertFalse(growth_lib.is_rules_file(
            os.path.join(real_cfg, "rules-old", "r.md")))


class TestHasPathsFrontmatter(unittest.TestCase):
    """Change 3. The [I] limitation — an empty `paths:` value counts as
    scoped — is pinned by test_an_empty_paths_value_is_true_the_stated_limitation
    rather than left as an untested docstring claim."""

    def test_a_scoped_file_is_true(self):
        text = "---\npaths:\n  - foo\n---\nbody\n"
        self.assertTrue(growth_lib.has_paths_frontmatter(text))

    def test_no_frontmatter_at_all_is_false(self):
        text = "just a body\npaths: foo\n"
        self.assertFalse(growth_lib.has_paths_frontmatter(text))

    def test_an_unclosed_block_is_false(self):
        text = "---\npaths:\n  - foo\nbody with no closing fence\n"
        self.assertFalse(growth_lib.has_paths_frontmatter(text))

    def test_paths_only_in_the_body_is_false(self):
        text = "---\nkey: val\n---\npaths: foo\n"
        self.assertFalse(growth_lib.has_paths_frontmatter(text))

    def test_an_indented_paths_key_is_false(self):
        text = "---\n  paths: foo\n---\nbody\n"
        self.assertFalse(growth_lib.has_paths_frontmatter(text))

    def test_a_crlf_scoped_file_is_true(self):
        text = "---\r\npaths:\r\n  - foo\r\n---\r\nbody\r\n"
        self.assertTrue(growth_lib.has_paths_frontmatter(text))

    def test_an_empty_paths_value_is_true_the_stated_limitation(self):
        text = "---\npaths: []\n---\nbody\n"
        self.assertTrue(growth_lib.has_paths_frontmatter(text))

    def test_an_unclosed_flow_list_with_no_bracket_at_all_is_false(self):
        text = "---\npaths: [\n---\nbody\n"
        self.assertFalse(growth_lib.has_paths_frontmatter(text))

    def test_an_unclosed_flow_list_with_content_is_false(self):
        text = '---\npaths: ["a/**"\n---\nbody\n'
        self.assertFalse(growth_lib.has_paths_frontmatter(text))

    def test_a_closed_flow_list_is_true(self):
        text = '---\npaths: ["a/**"]\n---\nbody\n'
        self.assertTrue(growth_lib.has_paths_frontmatter(text))


class TestRulesInScope(unittest.TestCase):
    def test_a_rules_file_is_in_scope(self):
        self.assertTrue(growth_lib.in_scope("/w/.claude/rules/a.md"))

    def test_a_plain_md_in_dot_claude_is_not(self):
        self.assertFalse(growth_lib.in_scope("/w/.claude/a.md"))


class TestCapsForPathRules(Fixture):
    """Change 6 — the rules branch, resolved before `is_map` / `cap_key`."""

    def test_outside_a_repo_gets_200_and_no_core_cap(self):
        path = self.rules_file("x\n")
        whole, core = growth_lib.caps_for_path(path)
        self.assertEqual(whole, growth_lib.DEFAULT_RULES_MAX_LINES)
        self.assertIsNone(core)

    def test_inside_a_repo_still_gets_the_rules_cap_not_context_lints(self):
        # A rules file inside a git repo must NOT fall through to
        # context_lint's cap_key/cap_for resolution.
        repo = self.repo("some-repo")
        path = os.path.join(repo, ".claude", "rules", "topic.md")
        os.makedirs(os.path.dirname(path))
        with open(path, "w") as handle:
            handle.write("x\n")
        whole, core = growth_lib.caps_for_path(path)
        self.assertEqual(whole, growth_lib.DEFAULT_RULES_MAX_LINES)
        self.assertIsNone(core)

    def test_a_configured_rules_max_lines_is_honoured(self):
        path = self.rules_file("x\n")
        cfg = stub_config(ints={("context_growth", "rules_max_lines"): 250})
        whole, _core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(whole, 250)

    def test_a_numeric_string_mirrors_memory_max_lines(self):
        # Premise: toolkit_config.get_int coerces a numeric string with
        # int(), so a real config's "250" must be ACCEPTED — the stub's
        # get_int does not replicate that coercion, so this goes through the
        # real toolkit_config module, exactly like the executable does.
        path = self.rules_file("x\n")
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, dir=self.dir)
        json.dump({"context_growth": {"rules_max_lines": "250"}}, handle)
        handle.close()
        orig_path = toolkit_config.CONFIG_PATH
        toolkit_config.CONFIG_PATH = handle.name
        toolkit_config.load(refresh=True)
        try:
            whole, _core = growth_lib.caps_for_path(path, toolkit_config)
        finally:
            toolkit_config.CONFIG_PATH = orig_path
            toolkit_config.load(refresh=True)
        self.assertEqual(whole, 250)

    def test_nonsense_values_fall_back_to_200(self):
        path = self.rules_file("x\n")
        for nonsense in (0, -1, True, "x"):
            with self.subTest(configured=nonsense):
                cfg = stub_config(
                    ints={("context_growth", "rules_max_lines"): nonsense})
                whole, _core = growth_lib.caps_for_path(path, cfg)
                self.assertEqual(whole, growth_lib.DEFAULT_RULES_MAX_LINES)

    def test_an_overrides_entry_raises_the_cap(self):
        path = self.rules_file("x\n")
        cfg = stub_config({("context_growth", "overrides"):
                           {os.path.realpath(path): 500}})
        whole, core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(whole, 500)
        self.assertIsNone(core)

    def test_a_core_overrides_entry_does_not_introduce_a_core_cap(self):
        path = self.rules_file("x\n")
        cfg = stub_config({("context_growth", "core_overrides"):
                           {os.path.realpath(path): 120}})
        whole, core = growth_lib.caps_for_path(path, cfg)
        self.assertEqual(whole, growth_lib.DEFAULT_RULES_MAX_LINES)
        self.assertIsNone(core)


class TestRulesNotice(Fixture):
    """Change 7/8 — the missing-`paths:` notice and the rules whole-file cap."""

    def test_a_new_pathless_file_speaks_on_first_sight(self):
        path = self.rules_file(self.pathless(10))
        notice = self.edit(path)
        self.assertIsNotNone(notice, "a brand-new pathless rules file must "
                                     "speak on its first write")
        self.assertIn("has no paths:", notice)
        self.assertIn("session start", notice)

    def test_an_unclosed_flow_list_speaks_on_first_sight(self):
        # Fix C: a malformed `paths: [` flow list is treated as pathless.
        path = self.rules_file("---\npaths: [\n---\n" + "row\n" * 10)
        notice = self.edit(path)
        self.assertIsNotNone(notice, "an unclosed flow list must speak on "
                                     "first write, like any pathless file")
        self.assertIn("has no paths:", notice)

    def test_a_second_edit_to_the_same_pathless_file_is_silent(self):
        path = self.rules_file(self.pathless(10))
        self.edit(path)
        self.rewrite(path, self.pathless(11))
        self.assertIsNone(self.edit(path), "warned once; must not repeat")

    def test_a_scoped_to_pathless_transition_speaks(self):
        path = self.rules_file(self.scoped(10))
        self.assertIsNone(self.edit(path), "first sight of a scoped file is "
                                           "silent")
        self.rewrite(path, self.pathless(10))
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("has no paths:", notice)

    def test_pathless_to_scoped_to_pathless_speaks_again(self):
        path = self.rules_file(self.pathless(10))
        self.assertIsNotNone(self.edit(path), "first sight, warns")
        self.rewrite(path, self.scoped(10))
        self.assertIsNone(self.edit(path), "now scoped: silent")
        self.rewrite(path, self.pathless(10))
        notice = self.edit(path)
        self.assertIsNotNone(notice, "pathless again: must speak")
        self.assertIn("has no paths:", notice)

    def test_first_sight_of_a_scoped_file_is_silent_and_records_has_paths(self):
        path = self.rules_file(self.scoped(10))
        self.assertIsNone(self.edit(path))
        self.assertIs(self.state[os.path.realpath(path)]["has_paths"], True)

    def test_a_scoped_file_growing_150_to_210_speaks_with_cap_200(self):
        path = self.rules_file(self.scoped(150))
        self.assertIsNone(self.edit(path))
        self.rewrite(path, self.scoped(210))
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("cap 200", notice)
        self.assertIn("Add a paths: list", notice)
        self.assertNotIn("Core", notice)

    def test_a_scoped_file_growing_100_to_190_is_silent(self):
        path = self.rules_file(self.scoped(100))
        self.assertIsNone(self.edit(path))
        self.rewrite(path, self.scoped(190))
        self.assertIsNone(self.edit(path))

    def test_pathless_and_over_cap_on_the_same_edit_is_one_notice(self):
        # ONE notice, size header (not the missing-paths-only header), both
        # rows present: the whole-file size row AND the "no paths: —" body
        # row from change 8.
        path = self.rules_file(self.scoped(100))
        self.assertIsNone(self.edit(path))
        self.rewrite(path, self.pathless(210))
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertIn("grew past its budget in this session", notice)
        self.assertIn("cap 200", notice)
        self.assertIn("no paths: —", notice)

    def test_a_core_block_inside_a_rules_file_produces_no_core_or_ref_row(self):
        first = self.scoped(100) + self.core(10)
        path = self.rules_file(first)
        self.assertIsNone(self.edit(path))
        grown = self.scoped(210) + self.core(10).replace(
            "row 0", "row 0; see below")
        self.rewrite(path, grown)
        notice = self.edit(path)
        self.assertIsNotNone(notice)
        self.assertNotIn("Core", notice)
        self.assertNotIn("cross-reference", notice)

    def test_a_memory_notice_is_unaffected_by_this_change(self):
        path = self.memory_file(text="x\n" * 100)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "x\n" * 300)
        notice = self.edit(path)
        self.assertIn("memory-distill", notice)
        self.assertNotIn("paths:", notice)

    def test_a_claude_md_notice_is_unaffected_by_this_change(self):
        path = self.claude_md(text="x\n" * 100)
        self.assertIsNone(self.edit(path))
        self.rewrite(path, "x\n" * 900)
        notice = self.edit(path)
        self.assertIn("claude-md-distill", notice)
        self.assertNotIn("paths:", notice)


class _AlwaysOnConfig(object):
    """The narrow slice of `toolkit_config`'s interface the always-on branch
    reads: `workspace_root()` (step 3), `get()` for
    `context_budget.always_on_budget` and (optionally) `context_growth.
    overrides` -- so a test can force a small per-file cap on ONE fixture
    file without a second stub class -- and `get_path()` for
    `context_budget.memory_dir`. Every other key falls through to the
    caller's own `default`, matching a real config reading an absent key.
    """

    def __init__(self, root, budget=None, memory_dir=None, overrides=None):
        self._root = root
        self._budget = budget
        self._memory_dir = memory_dir
        self._overrides = overrides or {}

    def workspace_root(self):
        return self._root

    def get(self, *keys, **kwargs):
        if keys == ("context_budget", "always_on_budget"):
            return self._budget
        if keys == ("context_growth", "overrides"):
            return self._overrides
        return kwargs.get("default")

    def get_path(self, *keys, **kwargs):
        if keys == ("context_budget", "memory_dir") and self._memory_dir:
            return self._memory_dir
        return kwargs.get("default")

    def get_int(self, *keys, **kwargs):
        return kwargs.get("default", 0)


class TestAlwaysOnBudget(Fixture):
    """The always-on branch in `growth_lib.build()`: steps
    1-8 of the always-on contract. `F` is `self.dir` (`Fixture`'s own
    temp root); the workspace lives at `F/outer/ws`, an ancestor `CLAUDE.md`
    at `F/outer`, a memory dir NOT literally named `memory` at `F/mem` (so
    `in_scope` is False for its index -- P10 -- while the always-on branch
    still counts it), and `HOME` at `F/home` -- patched for the whole test,
    since the branch reads `os.path.expanduser("~")` directly (contract
    step 4), not a parameter.

    Every budget is set RELATIVE to a baseline total this class measures
    itself, live, with `budget_lib` on this same fixture -- never a literal
    -- so an ambient managed CLAUDE.md (P9) shifts both sides equally
    (plan, Tests part B).
    """

    def setUp(self):
        Fixture.setUp(self)
        self.outer = os.path.join(self.dir, "outer")
        self.ws = os.path.join(self.outer, "ws")
        os.makedirs(self.ws)

        self.claude_md = os.path.join(self.ws, "CLAUDE.md")
        self._write(self.claude_md, "- item 0\n- item 1\n")
        self.claude_local = os.path.join(self.ws, "CLAUDE.local.md")
        self._write(self.claude_local, "- local 0\n")
        self.tooling = os.path.join(self.ws, "TOOLING.md")
        self._write(
            self.tooling, "<!-- DIGEST:START -->\n- row\n<!-- DIGEST:END -->\n"
        )
        os.makedirs(os.path.join(self.ws, ".claude", "rules"))
        self.pathless_rule = os.path.join(
            self.ws, ".claude", "rules", "pathless.md")
        self._write(self.pathless_rule, "- row\n")
        self.scoped_rule = os.path.join(self.ws, ".claude", "rules", "scoped.md")
        self._write(self.scoped_rule, self.scoped(1))
        self.ancestor_claude_md = os.path.join(self.outer, "CLAUDE.md")
        self._write(self.ancestor_claude_md, "- ancestor 0\n")

        self.mem = os.path.join(self.dir, "mem")
        os.makedirs(self.mem)
        self.mem_index = os.path.join(self.mem, "MEMORY.md")
        self._write(self.mem_index, "- [A](a.md) — hook\n")

        self.home = os.path.join(self.dir, "home")
        os.makedirs(os.path.join(self.home, ".claude"))
        self.home_claude_md = os.path.join(self.home, ".claude", "CLAUDE.md")
        self._write(self.home_claude_md, "- user 0\n")

        self._saved_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.addCleanup(self._restore_home)

        self.state_file = os.path.join(self.dir, "growth-state.json")
        self._saved_state_env = os.environ.get(growth_lib.STATE_ENV)
        os.environ[growth_lib.STATE_ENV] = self.state_file
        self.addCleanup(self._restore_state_env)

        self.root = os.path.realpath(self.ws)

    def _write(self, path, text):
        with open(path, "w") as handle:
            handle.write(text)

    def _restore_home(self):
        if self._saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._saved_home

    def _restore_state_env(self):
        if self._saved_state_env is None:
            os.environ.pop(growth_lib.STATE_ENV, None)
        else:
            os.environ[growth_lib.STATE_ENV] = self._saved_state_env

    def _config(self, budget=None, overrides=None):
        return _AlwaysOnConfig(
            self.root, budget=budget, memory_dir=self.mem, overrides=overrides
        )

    def _baseline_total(self, config):
        """The SAME reading `budget_lib` itself would take, on THIS
        fixture, right now -- never a hand-derived number (plan, Tests
        part B)."""
        home = os.path.expanduser("~")
        memory_dir, memory_reason = budget_lib.resolve_memory_dir(
            self.root, config, home
        )
        records = budget_lib.measure_instructions(
            self.root, memory_dir, memory_reason, config, home
        )
        total = budget_lib.always_on_total(records)
        self.assertIsNotNone(total, "fixture premise: every always-on "
                             "surface must be readable")
        return total

    def _grow(self, path, n=10, tag="grown"):
        with open(path, "a") as handle:
            for i in range(n):
                handle.write("- %s item %d\n" % (tag, i))

    def _build(self, path, config, state=None):
        return growth_lib.build(
            {"file_path": path}, config,
            state=self.state if state is None else state,
        )

    # -- case 1 --------------------------------------------------------
    def test_1_unset_budget_is_silent_and_state_untouched(self):
        config = self._config(budget=None)
        self.assertIsNone(self._build(self.claude_md, config))
        self.assertFalse(
            any(k.startswith(growth_lib.ALWAYS_ON_PREFIX) for k in self.state)
        )

    # -- case 2 --------------------------------------------------------
    def test_2_invalid_budget_values_are_silent_and_state_untouched(self):
        # "State untouched" is about the ALWAYS-ON branch's own ratchet key —
        # the per-file branch legitimately records its own first-sight
        # observation under a DIFFERENT key regardless of the budget's
        # validity, so this asserts the absence of the ratchet key rather
        # than an empty dict.
        for bad in ("1400", True, 0):
            state = {}
            config = self._config(budget=bad)
            self.assertIsNone(self._build(self.claude_md, config, state=state),
                              repr(bad))
            self.assertNotIn(growth_lib.ALWAYS_ON_PREFIX + self.root, state,
                             repr(bad))

    # -- case 3 --------------------------------------------------------
    def test_3_first_sight_over_budget_is_silent_but_seeds_state(self):
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline - 1)
        self.assertIsNone(self._build(self.claude_md, config))
        self.assertEqual(
            self.state[growth_lib.ALWAYS_ON_PREFIX + self.root], baseline
        )

    # -- case 4 --------------------------------------------------------
    def test_4_a_rise_across_the_budget_speaks(self):
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline + 5)
        self.assertIsNone(self._build(self.claude_md, config))  # first sight
        self._grow(self.claude_md)
        new_total = self._baseline_total(config)
        self.assertGreater(new_total, baseline + 5, "fixture growth premise")
        notice = self._build(self.claude_md, config)
        self.assertIsNotNone(notice)
        lines = notice.splitlines()
        self.assertEqual(
            lines[0], "[context-growth] always-on instructions are over budget."
        )
        self.assertEqual(lines[1], "")
        self.assertEqual(
            lines[2],
            "  %d instructions, budget %d — %d over (+%d since last check)"
            % (new_total, baseline + 5, new_total - (baseline + 5),
               new_total - baseline),
        )
        self.assertIn("Consolidation mode", notice)

    # -- case 5 --------------------------------------------------------
    def test_5_a_rise_landing_exactly_at_budget_is_silent(self):
        with open(self.claude_md) as handle:
            original = handle.read()
        baseline = self._baseline_total(self._config())
        grown_text = original + "".join(
            "- extra item %d\n" % i for i in range(7)
        )
        self._write(self.claude_md, grown_text)
        exact_budget = self._baseline_total(self._config())
        self.assertGreater(exact_budget, baseline, "fixture growth premise")
        self._write(self.claude_md, original)

        config = self._config(budget=exact_budget)
        self.assertIsNone(self._build(self.claude_md, config))  # first sight
        self._write(self.claude_md, grown_text)
        self.assertIsNone(self._build(self.claude_md, config))
        self.assertEqual(
            self.state[growth_lib.ALWAYS_ON_PREFIX + self.root], exact_budget
        )

    # -- case 6 --------------------------------------------------------
    def test_6_an_unchanged_total_while_over_budget_is_silent(self):
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline - 1)
        self.assertIsNone(self._build(self.claude_md, config))  # first sight
        self.assertIsNone(self._build(self.claude_md, config))  # no change

    # -- case 7 --------------------------------------------------------
    def test_7_editing_the_scoped_rules_file_does_not_move_the_ratchet(self):
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline - 1)
        self.assertIsNone(self._build(self.claude_md, config))  # seeds ratchet
        self._grow(self.ancestor_claude_md)  # an UNRELATED always-on surface
        grown_total = self._baseline_total(config)
        self.assertGreater(grown_total, baseline, "fixture growth premise")
        notice = self._build(self.scoped_rule, config)
        self.assertIsNone(notice)
        self.assertEqual(
            self.state[growth_lib.ALWAYS_ON_PREFIX + self.root], baseline,
            "the ratchet moved on an edit to a file that is not an "
            "always-on record",
        )

    # -- case 7b (Merge Review) -----------------------------------------
    def test_7b_a_rule_turning_conditional_lowers_the_ratchet(self):
        """Adding `paths:` to a pathless rules file drops the total through an
        edit to a file that is no longer an always-on record. The drop must be
        recorded, or the next real crossing is compared against the stale
        higher total and stays silent."""
        self._grow(self.pathless_rule, n=10, tag="rule")
        high = self._baseline_total(self._config())
        self.assertIsNone(self._build(self.claude_md, self._config(budget=high + 1000)))
        self.assertEqual(self.state[growth_lib.ALWAYS_ON_PREFIX + self.root], high)

        with open(self.pathless_rule) as handle:
            body = handle.read()
        self._write(self.pathless_rule, '---\npaths:\n  - "x/**"\n---\n' + body)
        low = self._baseline_total(self._config())
        self.assertLess(low, high, "fixture premise: scoping the rule drops the total")
        config = self._config(budget=low)

        self.assertIsNone(self._build(self.pathless_rule, config))
        self.assertEqual(self.state[growth_lib.ALWAYS_ON_PREFIX + self.root], low,
                         "the drop through a now-conditional file was not recorded")

        self._grow(self.claude_md, n=1)
        grown = self._baseline_total(config)
        self.assertTrue(low < grown < high,
                        "fixture premise: the crossing stays under the stale total")
        notice = self._build(self.claude_md, config)
        self.assertIsNotNone(notice, "a real crossing was hidden by a stale baseline")
        self.assertIn("always-on instructions are over budget", notice)

    # -- case 8 --------------------------------------------------------
    def test_8_editing_the_memory_index_speaks_even_though_in_scope_is_false(self):
        self.assertFalse(
            growth_lib.in_scope(self.mem_index),
            "premise: a memory dir not literally named 'memory' fails "
            "in_scope's basename check",
        )
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline + 5)
        self.assertIsNone(self._build(self.mem_index, config))  # first sight
        self._grow(self.claude_md)
        new_total = self._baseline_total(config)
        self.assertGreater(new_total, baseline + 5, "fixture growth premise")
        notice = self._build(self.mem_index, config)
        self.assertIsNotNone(notice)
        self.assertIn("Consolidation mode", notice)

    # -- case 9 --------------------------------------------------------
    def test_9_editing_claude_local_md_speaks_even_though_in_scope_is_false(self):
        self.assertFalse(growth_lib.in_scope(self.claude_local))
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline + 5)
        self.assertIsNone(self._build(self.claude_local, config))  # first sight
        self._grow(self.claude_md)
        new_total = self._baseline_total(config)
        self.assertGreater(new_total, baseline + 5, "fixture growth premise")
        notice = self._build(self.claude_local, config)
        self.assertIsNotNone(notice)
        self.assertIn("Consolidation mode", notice)

    # -- case 10 -------------------------------------------------------
    def test_10_speaks_on_the_edited_files_own_first_sight_if_total_has_a_prior(
        self,
    ):
        # `self.home_claude_md` (never edited yet in this test) is used
        # rather than `self.pathless_rule`: a pathless rules file speaks on
        # ITS OWN first sight regardless (the "no paths: frontmatter"
        # per-file notice), which would pass this assertion for the wrong
        # reason -- a CLAUDE.md-family file's own per-file notice stays
        # silent on first sight, so only the always-on branch can speak
        # here.
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline + 5)
        self.assertIsNone(self._build(self.claude_md, config))  # seeds ratchet
        self._grow(self.ancestor_claude_md)
        new_total = self._baseline_total(config)
        self.assertGreater(new_total, baseline + 5, "fixture growth premise")
        self.assertNotIn(os.path.realpath(self.home_claude_md), self.state)
        notice = self._build(self.home_claude_md, config)
        self.assertIsNotNone(notice)
        self.assertIn("Consolidation mode", notice)

    # -- case 11 -------------------------------------------------------
    def test_11_both_notices_fire_per_file_first_then_always_on(self):
        small_cap = 3
        overrides = {os.path.realpath(self.claude_md): small_cap}
        baseline = self._baseline_total(self._config(overrides=overrides))
        config = self._config(budget=baseline + 2, overrides=overrides)
        self.assertIsNone(self._build(self.claude_md, config))  # first sight
        self._grow(self.claude_md, n=10)
        new_total = self._baseline_total(config)
        self.assertGreater(new_total, baseline + 2, "fixture growth premise")
        notice = self._build(self.claude_md, config)
        self.assertIsNotNone(notice)
        self.assertIn("grew past its budget in this session", notice)
        self.assertIn(
            "[context-growth] always-on instructions are over budget.", notice
        )
        per_file_idx = notice.index("grew past its budget in this session")
        always_on_idx = notice.index(
            "[context-growth] always-on instructions are over budget."
        )
        self.assertLess(
            per_file_idx, always_on_idx, "the per-file notice must come FIRST"
        )
        self.assertIn(
            "\n\n[context-growth] always-on instructions are over budget.",
            notice,
        )

    # -- case 12 -------------------------------------------------------
    def test_12_unreadable_always_on_surface_is_silent_and_ratchet_unchanged(
        self,
    ):
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline - 1)
        self.assertIsNone(self._build(self.claude_md, config))  # seeds ratchet
        real_measure = budget_lib.measure_instructions

        def broken_measure(*args, **kwargs):
            records = real_measure(*args, **kwargs)
            records.append(
                {
                    "key": "/nonexistent/CLAUDE.md",
                    "kind": "claude_md",
                    "class": "always-on",
                    "ok": False,
                    "note": "unreadable",
                    "partial": False,
                    "instructions": None,
                    "unit": "lines",
                    "value": None,
                    "cap": None,
                }
            )
            return records

        with mock.patch.object(
            budget_lib, "measure_instructions", broken_measure
        ):
            self._grow(self.claude_md)
            notice = self._build(self.claude_md, config)
        self.assertIsNone(notice)
        self.assertEqual(
            self.state[growth_lib.ALWAYS_ON_PREFIX + self.root], baseline,
            "an unreadable surface must leave the ratchet untouched",
        )

    # -- case 13 -------------------------------------------------------
    def test_13_measure_instructions_raising_does_not_suppress_the_per_file_notice(
        self,
    ):
        small_cap = 3
        overrides = {os.path.realpath(self.claude_md): small_cap}
        config = self._config(budget=1, overrides=overrides)
        self.assertIsNone(self._build(self.claude_md, config))  # first sight
        self._grow(self.claude_md, n=10)
        with mock.patch.object(
            budget_lib, "measure_instructions",
            side_effect=RuntimeError("boom"),
        ):
            notice = self._build(self.claude_md, config)
        self.assertIsNotNone(notice)
        self.assertIn("grew past its budget in this session", notice)
        self.assertNotIn("always-on instructions are over budget", notice)

    # -- case 14 -------------------------------------------------------
    def test_14_a_file_outside_every_candidate_never_calls_measure_instructions(
        self,
    ):
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline - 1)
        notes = os.path.join(self.dir, "notes.md")
        self._write(notes, "not a candidate\n")
        mocked = mock.Mock(
            side_effect=AssertionError(
                "measure_instructions must not be called past the "
                "candidate gate"
            )
        )
        with mock.patch.object(budget_lib, "measure_instructions", mocked):
            notice = self._build(notes, config)
        self.assertIsNone(notice)
        self.assertFalse(mocked.called)

    # -- case 15 -------------------------------------------------------
    def test_15_persistence_the_carve_out_survives_a_write(self):
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline + 5)
        self.assertFalse(os.path.exists(self.state_file))

        self.assertIsNone(
            growth_lib.build({"file_path": self.claude_md}, config, state=None)
        )
        self.assertTrue(os.path.exists(self.state_file))
        with open(self.state_file) as handle:
            seeded = json.load(handle)
        key = growth_lib.ALWAYS_ON_PREFIX + self.root
        self.assertEqual(seeded.get(key), baseline)

        self._grow(self.claude_md)
        notice = growth_lib.build(
            {"file_path": self.claude_md}, config, state=None
        )
        self.assertIsNotNone(notice)
        with open(self.state_file) as handle:
            after = json.load(handle)
        self.assertIn(key, after)
        self.assertGreater(after[key], baseline)

    # -- Architect review follow-up: no state I/O for a file that is
    # neither in_scope nor an always-on candidate ------------------------
    def test_a_budget_unset_edit_outside_every_candidate_does_no_state_io(self):
        notes = os.path.join(self.dir, "notes.py")
        self._write(notes, "print('hi')\n")
        config = self._config(budget=None)
        self.assertFalse(os.path.exists(self.state_file))
        real_read_state = growth_lib.read_state
        mock_read = mock.Mock(side_effect=real_read_state)
        with mock.patch.object(growth_lib, "read_state", mock_read):
            notice = growth_lib.build({"file_path": notes}, config, state=None)
        self.assertIsNone(notice)
        self.assertFalse(mock_read.called)
        self.assertFalse(os.path.exists(self.state_file))

    def test_b_budget_set_edit_outside_every_candidate_does_no_state_io(self):
        notes = os.path.join(self.dir, "notes.py")
        self._write(notes, "print('hi')\n")
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline + 5)
        self.assertFalse(os.path.exists(self.state_file))
        real_read_state = growth_lib.read_state
        mock_read = mock.Mock(side_effect=real_read_state)
        with mock.patch.object(growth_lib, "read_state", mock_read):
            notice = growth_lib.build({"file_path": notes}, config, state=None)
        self.assertIsNone(notice)
        self.assertFalse(mock_read.called)
        self.assertFalse(os.path.exists(self.state_file))

    def test_c_control_edit_to_scoped_rules_file_writes_state(self):
        """Positive control: a's and b's silence is the CANDIDATE gate, not
        an accident of never writing anything at all."""
        baseline = self._baseline_total(self._config())
        config = self._config(budget=baseline + 5)
        self.assertFalse(os.path.exists(self.state_file))
        growth_lib.build({"file_path": self.scoped_rule}, config, state=None)
        self.assertTrue(os.path.exists(self.state_file))

    def test_d_workspace_root_empty_is_silent_no_ratchet_key(self):
        config = self._config(budget=100)
        config._root = ""
        notice = growth_lib.build(
            {"file_path": self.home_claude_md}, config, state=None
        )
        self.assertIsNone(notice)
        with open(self.state_file) as handle:
            state = json.load(handle)
        self.assertFalse(
            any(k.startswith(growth_lib.ALWAYS_ON_PREFIX) for k in state)
        )

    def test_d_workspace_root_raising_is_silent_no_ratchet_key(self):
        class _RaisingRootConfig(_AlwaysOnConfig):
            def workspace_root(self):
                raise RuntimeError("boom")

        config = _RaisingRootConfig(self.root, budget=100, memory_dir=self.mem)
        notice = growth_lib.build(
            {"file_path": self.home_claude_md}, config, state=None
        )
        self.assertIsNone(notice)
        with open(self.state_file) as handle:
            state = json.load(handle)
        self.assertFalse(
            any(k.startswith(growth_lib.ALWAYS_ON_PREFIX) for k in state)
        )


class TestAlwaysOnHookExecutable(Fixture):
    """The always-on branch through a REAL subprocess, reached via
    a symlink reproducing the installed shape (`TestThroughTheInstalledSymlink`
    above is the model for the symlink; `TestAlwaysOnBudget` above is the
    model for the fixture and the baseline-relative budget). `TestAlwaysOnBudget`
    proves the branch in-process; this proves the `sys.path` insert to
    `tools/context-budget` (P5) actually resolves when `growth_lib` is loaded
    through `~/.local/bin`, not merely through the test file's own repo-relative
    insert at import time.

    The env is built explicitly, never `dict(os.environ)` -- repo CLAUDE.md,
    "A subprocess test inherits the SURFACE it runs on, and that is ambient
    state too": `PATH` is pinned to `/usr/bin:/bin`, `HOME` to a fixture
    directory (so `os.path.expanduser("~")` inside the hook resolves there,
    never to whoever runs this suite), `CLAUDE_TOOLKIT_CONFIG` to a fixture
    JSON config, `CLAUDE_CONTEXT_GROWTH_STATE` to a fixture path.

    Budgets are set relative to a baseline this class measures itself, live,
    with `budget_lib` on this same fixture -- never a literal -- exactly as
    `TestAlwaysOnBudget._baseline_total` does (budget set
    relative to a baseline measured in-test). Reuses
    `_AlwaysOnConfig` for that measurement rather than a second stub class.
    """

    def setUp(self):
        Fixture.setUp(self)
        self.home_dir = os.path.join(self.dir, "home")
        os.makedirs(self.home_dir)

        self.mem_dir = os.path.join(self.dir, "mem")
        os.makedirs(self.mem_dir)
        with open(os.path.join(self.mem_dir, "MEMORY.md"), "w") as handle:
            handle.write("## Always\n- [A](a.md) — hook\n")

        self.root = os.path.join(self.dir, "ws")
        os.makedirs(self.root)
        self.claude_md_path = os.path.join(self.root, "CLAUDE.md")
        self._set_text(self._base_text())

        bin_dir = os.path.join(self.dir, "bin")
        os.makedirs(bin_dir)
        self.link = os.path.join(bin_dir, "claude-context-growth")
        os.symlink(EXE, self.link)

        self.state_file = os.path.join(self.dir, "growth-state.json")
        self.config_path = os.path.join(self.dir, "config.json")

    def _base_text(self):
        return "- item 0\n- item 1\n"

    def _grown_text(self):
        return self._base_text() + "".join(
            "- grown item %d\n" % i for i in range(30))

    def _set_text(self, text):
        with open(self.claude_md_path, "w") as handle:
            handle.write(text)

    def _write_config(self, budget):
        with open(self.config_path, "w") as handle:
            json.dump({
                "workspace_root": self.root,
                "context_budget": {
                    "always_on_budget": budget,
                    "memory_dir": self.mem_dir,
                },
            }, handle)

    def _env(self):
        """Explicitly built, never `dict(os.environ)` -- see the class
        docstring. `PATH` covers everything a pure-stdlib hook needs to run
        `sys.executable` (passed by full path, not looked up on `PATH`
        itself) and nothing more."""
        return {
            "PATH": "/usr/bin:/bin",
            "HOME": self.home_dir,
            "CLAUDE_TOOLKIT_CONFIG": self.config_path,
            "CLAUDE_CONTEXT_GROWTH_STATE": self.state_file,
        }

    def _run(self):
        payload = json.dumps(
            {"tool_input": {"file_path": self.claude_md_path}})
        return subprocess.run(
            [sys.executable, self.link], input=payload,
            capture_output=True, text=True, timeout=60, env=self._env())

    def _measured_total(self):
        """The same reading `budget_lib` itself would take on this fixture,
        right now -- never a hand-derived number. `_AlwaysOnConfig` carries
        no budget here; only `workspace_root`/`memory_dir` matter for a
        measurement, matching `TestAlwaysOnBudget._baseline_total`."""
        config = _AlwaysOnConfig(self.root, memory_dir=self.mem_dir)
        memory_dir, memory_reason = budget_lib.resolve_memory_dir(
            self.root, config, self.home_dir)
        records = budget_lib.measure_instructions(
            self.root, memory_dir, memory_reason, config, self.home_dir)
        total = budget_lib.always_on_total(records)
        self.assertIsNotNone(total, "fixture premise: every always-on "
                             "surface must be readable")
        return total

    def test_run_1_is_silent_run_2_speaks_once_the_budget_is_crossed(self):
        baseline = self._measured_total()
        self._write_config(budget=baseline + 5)

        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.strip(), "", "first sight must be "
                         "silent; stderr: %s" % first.stderr)

        self._set_text(self._grown_text())
        new_total = self._measured_total()
        self.assertGreater(new_total, baseline + 5, "fixture growth premise")

        second = self._run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertNotEqual(
            second.stdout.strip(), "",
            "the hook produced nothing on a crossing edit through the "
            "symlink; stderr: %s" % second.stderr)
        context = json.loads(second.stdout)["hookSpecificOutput"][
            "additionalContext"]
        self.assertIn(
            "[context-growth] always-on instructions are over budget.",
            context)

    def test_positive_control_budget_above_the_grown_total_stays_silent(self):
        # Same two runs as above, budget above the grown total instead of
        # below it -- silence here is the budget, not an accident of a hook
        # that never speaks through this symlink at all.
        self._set_text(self._grown_text())
        grown_total = self._measured_total()
        self._set_text(self._base_text())
        self._write_config(budget=grown_total + 5)

        first = self._run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.strip(), "", "first sight must be "
                         "silent; stderr: %s" % first.stderr)

        self._set_text(self._grown_text())
        second = self._run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(
            second.stdout.strip(), "",
            "a budget still above the grown total must stay silent; "
            "stderr: %s" % second.stderr)


SKILL_MD = os.path.join(REPO, "skills", "continuity", "claude-md-distill", "SKILL.md")


def _skill_frontmatter_lines(text):
    """Same shape as `test_build_lens.py`'s `_frontmatter_lines`: the raw
    lines between the two `---` fences, or None if there is no such block."""
    match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if not match:
        return None
    return match.group(1).splitlines()


class TestConsolidationModeContract(Fixture):
    """`claude-md-distill`'s Consolidation mode: a new entry path
    fired by the always-on-budget notice. Reads SKILL.md straight off disk,
    relative to the repo root (`REPO`, already resolved at the top of this
    file via `os.path.realpath(__file__)`)."""

    NEW_HEADING = "## Consolidation mode — the always-on total is over budget"
    PRECEDING_HEADING = "## What this is NOT for"
    FOLLOWING_HEADING = "## Step 0 — size triage first, always"

    def setUp(self):
        Fixture.setUp(self)
        with open(SKILL_MD, encoding="utf-8") as handle:
            self.text = handle.read()

    # -- frontmatter -----------------------------------------------------
    def test_frontmatter_structural(self):
        """Primary, stdlib-only: the `---` fences, a `name:` line equal to
        `claude-md-distill`, a folded `description: >` block, and the
        description contains the always-on trigger phrase. Must still catch
        the class of bug on a machine with no PyYAML installed."""
        lines = _skill_frontmatter_lines(self.text)
        self.assertIsNotNone(lines, "SKILL.md has no --- frontmatter fences")
        self.assertIn("name: claude-md-distill", lines)
        self.assertIn("description: >", lines)
        desc_start = lines.index("description: >") + 1
        body = []
        for line in lines[desc_start:]:
            if line.startswith("  "):
                body.append(line.strip())
            else:
                break
        description = " ".join(body)
        self.assertIn("always-on instructions are over budget", description)

    def test_frontmatter_parses_as_yaml(self):
        """Secondary, belt and braces -- PyYAML is not stdlib, so skip
        rather than fail on a machine without it (the pattern in
        tools/build-lens/tests/test_build_lens.py:2522-2545)."""
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed -- covered by the "
                           "stdlib-only structural test above")
        lines = _skill_frontmatter_lines(self.text)
        parsed = yaml.safe_load("\n".join(lines))
        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed.get("name"), "claude-md-distill")
        self.assertIn("always-on instructions are over budget",
                      parsed.get("description", ""))

    # -- position ----------------------------------------------------------
    def _headings(self):
        return [line for line in self.text.splitlines()
                if line.startswith("## ")]

    def test_position(self):
        headings = self._headings()
        self.assertEqual(
            headings.count(self.NEW_HEADING), 1,
            "the new heading must appear exactly once")
        new_idx = headings.index(self.NEW_HEADING)
        pre_idx = headings.index(self.PRECEDING_HEADING)
        post_idx = headings.index(self.FOLLOWING_HEADING)
        self.assertEqual(new_idx, pre_idx + 1,
                         "the new heading must come right after "
                         "'## What this is NOT for'")
        self.assertEqual(post_idx, new_idx + 1,
                         "the new heading must come right before "
                         "'## Step 0 -- size triage first, always'")

    # -- contents ------------------------------------------------------
    def _section_lines(self):
        lines = self.text.splitlines()
        start = lines.index(self.NEW_HEADING)
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if lines[i].startswith("## "):
                end = i
                break
        return lines[start:end]

    def test_contents_and_order(self):
        section = self._section_lines()
        section_text = "\n".join(section)
        labels = ["**Entry:**", "**When:**", "**Pass A", "**Pass B",
                  "**Pass C", "**Decide nothing alone", "**Write:**",
                  "**Re-measure:**", "**Proof"]
        positions = []
        for label in labels:
            idx = section_text.find(label)
            self.assertGreaterEqual(idx, 0,
                                    "%r missing from the section" % label)
            positions.append(idx)
        self.assertEqual(positions, sorted(positions),
                         "the bold labels are out of order: %r" % labels)

        pass_b_start = section_text.find("**Pass B")
        pass_c_start = section_text.find("**Pass C")
        pass_b_text = section_text[pass_b_start:pass_c_start]
        for phrase in ("by hand",
                       "claude-memory-lint --report <that directory>",
                       "claude-context-budget --report"):
            self.assertIn(phrase, pass_b_text,
                         "%r missing from the Pass B paragraph" % phrase)

        self.assertLessEqual(
            len(section), 55,
            "the section is %d lines, must be <= 55" % len(section))

    # -- notice <-> skill --------------------------------------------------
    def test_notice_names_the_heading(self):
        """The always-on notice built in an always-on fixture names
        'Consolidation mode', and that phrase begins the heading found
        above. Reuses `TestAlwaysOnBudget`'s own fixture helpers
        (`_config`, `_baseline_total`, `_grow`, `_build`) by instantiating
        that TestCase directly and calling its setUp/tearDown by hand,
        rather than re-deriving a second fixture."""
        helper = TestAlwaysOnBudget(
            methodName="test_4_a_rise_across_the_budget_speaks")
        helper.setUp()
        try:
            baseline = helper._baseline_total(helper._config())
            config = helper._config(budget=baseline + 5)
            self.assertIsNone(helper._build(helper.claude_md, config))
            helper._grow(helper.claude_md)
            new_total = helper._baseline_total(config)
            self.assertGreater(new_total, baseline + 5,
                               "fixture growth premise")
            notice = helper._build(helper.claude_md, config)
        finally:
            helper.tearDown()
            helper.doCleanups()

        self.assertIsNotNone(notice)
        self.assertIn("Consolidation mode", notice)
        heading_text = self.NEW_HEADING[len("## "):]
        self.assertTrue(
            heading_text.startswith("Consolidation mode"),
            "the notice's phrase must begin the section heading: %r"
            % heading_text)


class TestObserveNonUtf8Locale(unittest.TestCase):
    """`observe` decodes a UTF-8 file as UTF-8 whatever the ambient locale is."""

    # Python reads the locale at start-up, so the code under test runs in a child.
    NON_UTF8 = {"LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0"}
    CODE = ("import json, sys; sys.path.insert(0, %r); import growth_lib; "
            "print(json.dumps(growth_lib.observe(sys.argv[1])))" % HERE)

    def _observe(self, path, overrides):
        env = dict(os.environ, **overrides)
        env.pop("PYTHONIOENCODING", None)
        return subprocess.run([sys.executable, "-c", self.CODE, path],
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True)

    def test_observe_reads_a_non_ascii_file_under_a_non_utf8_locale(self):
        probe = subprocess.run(
            [sys.executable, "-c",
             "import locale; print(locale.getpreferredencoding(False))"],
            env=dict(os.environ, **self.NON_UTF8), stdout=subprocess.PIPE,
            universal_newlines=True, check=True)
        if probe.stdout.strip().lower().replace("-", "") == "utf8":
            self.skipTest("could not force a non-UTF-8 locale here")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "CLAUDE.md")
            with open(path, "wb") as handle:
                handle.write("# Hawai\u02bbi\nsecond line\nthird line\n"
                             .encode("utf-8"))
            expected = json.loads(self._observe(path, {}).stdout)
            result = json.loads(self._observe(path, self.NON_UTF8).stdout)
        self.assertIsNotNone(result)
        self.assertEqual(result["lines"], expected["lines"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
