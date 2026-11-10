#!/usr/bin/env python3
"""Tests for the CLAUDE.md size cap.

The one that matters most is test_block_actually_blocks: a cap that never fires
is indistinguishable from headroom, so every "under cap passes" test here is
worthless without a positive control proving the over-cap path returns a finding.
That is the exact defect shape this tool was built in response to.
"""

import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(TOOL)), "lib"))

import context_lint_lib as lib  # noqa: E402
# Importable because context_lint_lib puts tools/tooling-digest on sys.path —
# it has to, since the map's Core cap is the digest budget.
import digest_lib  # noqa: E402

EXE = os.path.join(TOOL, "claude-context-lint")

# The override MACHINERY's test fixture, deliberately NOT `lib.DEFAULT_OVERRIDES`.
#
# Until 2026-09-15 the shipped dict carried a per-repo cap each, and every test
# below that exercises owner-qualification, precedence, repo-name collision or
# the bare-key fallback used it as its fixture. That coupled two independent
# things: "does the resolver work" and "what numbers do we happen to ship". When
# the shipped dict was emptied — one standardized cap, no per-repo entries — 27
# tests went red having found no defect at all, and the tempting fix (delete
# them) would have retired the resolver's entire test surface at the moment it
# stopped being covered by shipped data.
#
# So the machinery keeps a fixture of its own. These are the values the shipped
# dict used to hold, kept precisely because they reproduce the real historical
# defects: 630 vs the default is the C6 ratchet that leaked onto the personal
# clone, and the owner prefix is what stopped it.
MACHINERY_OVERRIDES = {
    "example-org/claude-code-toolkit/CLAUDE.md": 630,
    "example-org/claude-code-toolkit/CLAUDE.md.template": 540,
    "example-org/notes-repo/CLAUDE.md": 400,
    "example-org/infra/CLAUDE.md": 135,
}


def write_config(root, overrides=None, **sections):
    """A real config.json on disk, for tests that drive the EXECUTABLE.

    The executable reads $CLAUDE_TOOLKIT_CONFIG, not a Python object, so a test
    that needs an override to reach `--staged` has to put one in a file. Before
    the shipped dict was emptied these tests could lean on it and pass
    /nonexistent.json; now the override has to come from where a user's
    actually does, which makes this the more faithful path anyway.
    """
    import json
    data = dict(sections)
    if overrides is not None:
        data.setdefault("context_lint", {})["overrides"] = dict(overrides)
    path = os.path.join(root, "config.json")
    with open(path, "w") as handle:
        json.dump(data, handle)
    return path


class FakeConfig(object):
    """Stands in for toolkit_config. Raises on demand to test the fallback."""

    def __init__(self, data=None, blow_up=False):
        self.data = data or {}
        self.blow_up = blow_up

    def get(self, *keys, **kwargs):
        if self.blow_up:
            raise RuntimeError("config is on fire")
        node = self.data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return kwargs.get("default")
            node = node[key]
        return node


def reader(mapping):
    def read(path):
        return mapping[path]
    return read


class CapResolution(unittest.TestCase):

    def test_default_when_no_config(self):
        default, overrides = lib.caps(None)
        self.assertEqual(default, lib.DEFAULT_MAX_LINES)
        self.assertEqual(overrides, lib.DEFAULT_OVERRIDES)

    def test_config_overrides_default(self):
        default, _ = lib.caps(FakeConfig({"context_lint": {"max_lines": 250}}))
        self.assertEqual(default, 250)

    def test_config_adds_path_overrides(self):
        _, overrides = lib.caps(FakeConfig(
            {"context_lint": {"overrides": {"myrepo/CLAUDE.md": 90}}}))
        self.assertEqual(overrides["myrepo/CLAUDE.md"], 90)
        # A configured override is now the ONLY way an entry gets into this map,
        # since the shipped dict is empty by design. It no longer asserts
        # that shipped entries survived alongside configured ones; that merge
        # still happens (`caps` starts from a copy of DEFAULT_OVERRIDES) and is
        # pinned by test_a_configured_override_merges_onto_the_shipped_dict.
        self.assertEqual(len(overrides), len(lib.DEFAULT_OVERRIDES) + 1)

    def test_a_configured_override_merges_onto_the_shipped_dict(self):
        """The merge itself, independent of the shipped dict being empty.

        `caps` copies DEFAULT_OVERRIDES and updates it. With the shipped dict
        empty that copy is invisible — a `caps` that ignored it entirely would
        pass every other test here — so seed the module constant and check the
        configured entry lands ON TOP rather than replacing the map.
        """
        original = lib.DEFAULT_OVERRIDES
        lib.DEFAULT_OVERRIDES = {"shipped/CLAUDE.md": 111}
        try:
            _, overrides = lib.caps(FakeConfig(
                {"context_lint": {"overrides": {"mine/CLAUDE.md": 222}}}))
        finally:
            lib.DEFAULT_OVERRIDES = original
        self.assertEqual(overrides["shipped/CLAUDE.md"], 111)
        self.assertEqual(overrides["mine/CLAUDE.md"], 222)

    def test_malformed_config_falls_back_to_shipped(self):
        """Acceptance #4. A bad config must not wedge every commit."""
        default, overrides = lib.caps(FakeConfig(blow_up=True))
        self.assertEqual(default, lib.DEFAULT_MAX_LINES)
        self.assertEqual(overrides, lib.DEFAULT_OVERRIDES)

    def test_a_json_true_is_not_a_cap_of_one(self):
        """JSON `true` loads as Python True, which IS an int equal to 1.

        Before the fix a hand-edited `"max_lines": true` became a one-line cap
        that blocked every commit touching a CLAUDE.md, and the same held for
        an override, the Core cap and a Core override. Each falls back as any
        other invalid value does. A real integer beside it still applies, so a
        resolver that dropped every value would fail the positive half.
        """
        config = FakeConfig({"context_lint": {
            "max_lines": True, "overrides": {"a/CLAUDE.md": True, "b/CLAUDE.md": 7},
            "core_max_lines": True, "core_overrides": {"a/CLAUDE.md": True,
                                                       "b/CLAUDE.md": 9}}})
        default, overrides = lib.caps(config)
        self.assertEqual(default, lib.DEFAULT_MAX_LINES)
        self.assertEqual(overrides, {"b/CLAUDE.md": 7})
        core_default, core_overrides = lib.core_caps(config)
        self.assertEqual(core_default, lib.DEFAULT_CORE_MAX_LINES)
        self.assertEqual(core_overrides, {"b/CLAUDE.md": 9})

    def test_nonsense_values_are_ignored_not_applied(self):
        default, overrides = lib.caps(FakeConfig(
            {"context_lint": {"max_lines": -5, "overrides": {"a/CLAUDE.md": 0}}}))
        self.assertEqual(default, lib.DEFAULT_MAX_LINES)
        self.assertNotIn("a/CLAUDE.md", overrides)

    def test_the_canonical_key_matches_its_override(self):
        overrides = MACHINERY_OVERRIDES
        self.assertEqual(
            lib.cap_for("example-org/claude-code-toolkit/CLAUDE.md",
                        400, overrides), 630)
        self.assertEqual(
            lib.cap_for("example-org/claude-code-toolkit/CLAUDE.md.template",
                        400, overrides), 540)
        # The BARE spelling must NOT reach an owner-qualified override: that is
        # the collision this qualification exists to break. A repo merely NAMED
        # claude-code-toolkit is a repo we have no opinion about.
        self.assertEqual(
            lib.cap_for("claude-code-toolkit/CLAUDE.md", 400, overrides), 400)

    def test_the_shipped_override_map_is_empty_and_that_is_deliberate(self):
        """One standardized cap, ruled 2026-09-15 — no per-repo entries at all.

        Asserted rather than left implicit because the failure is silent in the
        direction that matters: re-adding a shipped entry for one repo restores
        exactly the per-repo negotiation this removed, and nothing else in the
        suite would notice. The resolver that reads this map is still fully
        tested, against MACHINERY_OVERRIDES.
        """
        self.assertEqual(lib.DEFAULT_OVERRIDES, {})

    def test_a_subdirectory_never_inherits_another_repos_ratchet(self):
        """The suffix defect: `infra` as a repo vs as a directory.

        A suffix match cannot tell them apart, and through a global
        core.hooksPath this tool runs against every repo on the machine — so an
        unrelated repo containing an `infra/` directory was blocked at the
        infra ratchet (120 when this defect was found; 135 today).
        """
        overrides = MACHINERY_OVERRIDES
        self.assertEqual(
            lib.cap_for("some-unrelated-repo/infra/CLAUDE.md", 400, overrides),
            400)
        self.assertEqual(lib.cap_for("example-org/infra/CLAUDE.md", 400, overrides), 135)

    def test_precedence_is_an_order_not_a_set(self):
        """An owner-qualified override beats a bare one for the SAME file.

        `cap_key` attaches candidates most-specific-first and `cap_for` takes
        the first hit. If it ever iterated the overrides dict instead, the
        winner would depend on insertion order — green today, wrong later.
        """
        _, overrides = lib.caps(None)
        key = lib._QualifiedKey("claude-code-toolkit/CLAUDE.md")
        key.candidates = ("example-org/claude-code-toolkit/CLAUDE.md",
                          "claude-code-toolkit/CLAUDE.md")
        both = {"claude-code-toolkit/CLAUDE.md": 999,
                "example-org/claude-code-toolkit/CLAUDE.md": 111}
        self.assertEqual(lib.cap_for(key, 400, both), 111)
        # ...and reversing the dict's insertion order changes nothing.
        self.assertEqual(lib.cap_for(key, 400, dict(reversed(list(both.items())))), 111)

    def test_a_hand_written_bare_override_still_matches(self):
        """Back-compat, and its absence would be SILENT.

        Overrides already written in a person's config use the bare spelling.
        Dropping the fallback would not error — it would hand that file the
        shipped default while the config still looks correct.
        """
        key = lib._QualifiedKey("claude-code-toolkit/CLAUDE.md")
        key.candidates = ("example-org/claude-code-toolkit/CLAUDE.md",
                          "claude-code-toolkit/CLAUDE.md")
        self.assertEqual(
            lib.cap_for(key, 400, {"claude-code-toolkit/CLAUDE.md": 777}), 777)

    def test_a_plain_string_key_still_resolves(self):
        """`cap_for` is called with bare strings from several places."""
        self.assertEqual(lib.cap_for("r/CLAUDE.md", 400, {"r/CLAUDE.md": 42}), 42)
        self.assertEqual(lib.cap_for("r/CLAUDE.md", 400, {}), 400)

    def test_the_example_config_agrees_with_the_shipped_defaults(self):
        """`config.example.json` EXECUTES — install.sh copies it to the live
        config on the no-TTY path, and README points people at it.

        So a stale spelling there is not a documentation nit: it is a shipped
        override map that silently disagrees with `DEFAULT_OVERRIDES`. This
        diff's first cut updated the prose beside these values and not the
        values, leaving the comment asserting "SHIPPED ENTRIES ARE
        OWNER-QUALIFIED" directly above four bare keys — which re-armed the
        exact collision, while the comment vouched for it.

        Asserts both halves: every key is a key the shipped map also has
        (spelling), and every value matches (number — the example held a stale
        infra 120 against the shipped 135).
        """
        import json
        root = os.path.dirname(os.path.dirname(TOOL))
        example = os.path.join(root, "config.example.json")
        if not os.path.exists(example):     # a consumer's checkout, not ours
            self.skipTest("not the toolkit checkout")
        with open(example) as handle:
            shipped = json.load(handle)["context_lint"]["overrides"]
        for key, value in shipped.items():
            self.assertIn(key, lib.DEFAULT_OVERRIDES,
                          "config.example.json ships override key %r, which "
                          "DEFAULT_OVERRIDES does not have — one of the two is "
                          "stale and the example is the one that executes" % key)
            self.assertEqual(
                value, lib.DEFAULT_OVERRIDES[key],
                "config.example.json caps %r at %d, shipped default is %d"
                % (key, value, lib.DEFAULT_OVERRIDES[key]))

    def test_a_file_outside_any_repo_gets_the_default(self):
        _, overrides = lib.caps(None)
        self.assertEqual(lib.cap_for(None, 400, overrides), 400)

    def test_unknown_repo_gets_the_default(self):
        """Acceptance #5."""
        _, overrides = lib.caps(None)
        self.assertEqual(
            lib.cap_for("some-new-repo/CLAUDE.md", 400, overrides), 400)

    def test_the_shipped_default_is_the_one_standardized_cap(self):
        """800, and the same number for every CLAUDE.md on the machine.

        Ruled 2026-09-15. This is a DELIBERATE departure from the rule that a
        looser personal number belongs in personal config while the shipped
        default stays strict — that rule governs SAFETY keys, and a context cap
        is an attention budget, not a safety boundary. The number is shipped
        rather than personal because the files it governs are loaded by every
        session that opens the repo.

        Pinned so the value cannot drift back quietly: the whole point of the
        change is that there is ONE number, and a second one appearing anywhere
        is the regression.
        """
        self.assertEqual(lib.DEFAULT_MAX_LINES, 800)

    def test_the_standard_cap_is_far_below_the_harness_hard_limit(self):
        """The only limit Claude Code actually enforces is 4 MiB — above it the
        file is SKIPPED ENTIRELY, not truncated (code.claude.com/docs/en/memory).

        800 lines is far under the 4 MiB limit. Asserted because the
        failure mode of crossing it is total and silent: no warning, no partial
        load, just a CLAUDE.md that stopped applying. Anyone raising this cap
        should have to walk past this test.
        """
        worst_case_bytes_per_line = 200
        self.assertLess(lib.DEFAULT_MAX_LINES * worst_case_bytes_per_line,
                        4 * 1024 * 1024)

    def test_this_repos_own_context_files_are_under_their_own_caps(self):
        """Both files this repo ships stay UNDER their own caps.

        Covers BOTH, because a test naming only one lets the other drift over
        its cap unnoticed -- each was separately allowed to reach its ceiling
        (CLAUDE.md at 870/870 before C6, the template at 598/600 before C7).

        Scope, stated because a previous reading of this docstring got it
        wrong: this detects a file OVER its cap. It does NOT detect a file
        frozen AT its cap -- 465/465 passes here, deliberately, for the reason
        in the next paragraph. Mutating a cap to its file's exact length
        therefore SURVIVES and proves nothing; the mutation that establishes
        this test is `cap -> len(<file>) - 1`, named at the end of this
        docstring.

        Deliberately asserts ONE threshold per file, the cap itself. A second,
        tighter "headroom" assertion here would be two numbers governing one
        file -- a commit taking CLAUDE.md to 620 would satisfy the 630 cap and
        fail the test -- which is the defect shape of the two Core caps that
        produced a BLOCKING finding on #131. The headroom POLICY (a cap lands
        above the cut, never at it) is enforced by the human setting the
        number, and its reasoning is in DEFAULT_OVERRIDES.

        `assertLessEqual`, NOT `assertLess`, and the difference is the whole
        point of the paragraph above. `check()` blocks on `lines > cap`, so a
        file exactly AT its cap is allowed. `assertLess` here asserted cap - 1
        while claiming to assert the cap -- reintroducing the second threshold
        one line tighter, and failing a commit the tool permits. It also
        corrupted the mutation evidence: `cap -> <file length>` killed this
        test through the off-by-one rather than through the cap contract, so
        the mutation that establishes it is `cap -> len(<file>) - 1`, the first
        value at which `check()` itself blocks.
        """
        _, overrides = lib.caps(None)
        here = os.path.dirname(os.path.realpath(__file__))
        repo = os.path.dirname(os.path.dirname(os.path.dirname(here)))
        checked = present = 0
        for name in ("CLAUDE.md", "CLAUDE.md.template"):
            path = os.path.join(repo, name)
            if not os.path.exists(path):   # a consumer's checkout, not ours
                continue
            present += 1
            # cap_key(path), NOT a hand-built string: the hand-built one asserted
            # a spelling instead of the resolution the hook actually performs,
            # and it is why the work/personal key collision went unseen here.
            key = lib.cap_key(path)
            # NO OWNER GUARD, and none is needed. The key carries the remote
            # owner (ambient state), but with no shipped override the shipped
            # resolution is DEFAULT_MAX_LINES for every key, so a fork, a mirror
            # or a no-origin export resolves the same cap as this checkout. An
            # owner guard here would also be a silent skip waiting to happen: a
            # hard-coded owner that stops matching (a renamed account, a fork
            # that becomes the canonical copy) reports `skipped` on every
            # machine while standing inside the checkout. That is how the
            # previous proxy guard failed when the override map was emptied.
            cap = lib.cap_for(key, lib.DEFAULT_MAX_LINES, overrides)
            with open(path) as fh:
                actual = sum(1 for _ in fh)
            self.assertLessEqual(actual, cap,
                                 "%s is %d lines against its own %d cap"
                                 % (name, actual, cap))
            checked += 1
        # A shipped file that exists but was not checked is a FAILURE, never a
        # skip: a guard reintroduced above would otherwise turn this test into
        # a silent pass on every machine.
        self.assertEqual(checked, present, "a shipped context file went unchecked")
        if not present:
            self.skipTest("no shipped context file in this checkout")

    def test_a_raised_default_does_not_leak_into_pinned_repos(self):
        # The workspace CLAUDE.md can only be reached via the DEFAULT, so
        # raising it on one machine must not quietly raise a pinned repo too.
        # Exercised against MACHINERY_OVERRIDES rather than the shipped dict,
        # which is empty since 2026-09-15 — with no pinned repos to leak into,
        # reading `lib.caps(None)` here would assert nothing at all.
        overrides = MACHINERY_OVERRIDES
        for key, expected in (("example-org/notes-repo/CLAUDE.md", 400),
                              ("example-org/infra/CLAUDE.md", 135)):
            self.assertEqual(lib.cap_for(key, 500, overrides), expected)
            # ...and a genuinely raised default does reach an UNpinned repo,
            # which is the control: without it a cap_for that ignored its
            # default argument would pass the loop above.
        self.assertEqual(lib.cap_for("unpinned/CLAUDE.md", 500, overrides), 500)


class FileSelection(unittest.TestCase):

    def test_recognises_both_context_files(self):
        self.assertTrue(lib.is_context_file("a/b/CLAUDE.md"))
        self.assertTrue(lib.is_context_file("CLAUDE.md.template"))

    def test_ignores_everything_else(self):
        for path in ("README.md", "claude.md", "docs/CLAUDE.md.bak", "x.py"):
            self.assertFalse(lib.is_context_file(path), path)

    def test_non_context_files_are_skipped_entirely(self):
        """Acceptance #2 — a commit with no CLAUDE.md yields nothing."""
        found = lib.check([(None, "a.py"), (None, "README.md")], reader({}), None)
        self.assertEqual(found, [])


class Checking(unittest.TestCase):

    def test_block_actually_blocks(self):
        """POSITIVE CONTROL. Without this, every passing test below is vacuous.

        Acceptance #1. An over-cap file must produce a finding with the right
        numbers — not merely 'not empty', since a finding with the wrong cap or
        count would render a message that misleads.
        """
        cap = lib.DEFAULT_MAX_LINES
        content = "\n".join("line" for _ in range(cap + 100))
        found = lib.check([("new-repo/CLAUDE.md", "new-repo/CLAUDE.md")],
                          reader({"new-repo/CLAUDE.md": content}), None)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["lines"], cap + 100)
        self.assertEqual(found[0]["cap"], cap)
        self.assertEqual(found[0]["over"], 100)

    def test_under_cap_passes(self):
        content = "\n".join("line" for _ in range(lib.DEFAULT_MAX_LINES - 1))
        self.assertEqual(
            lib.check([("r/CLAUDE.md", "r/CLAUDE.md")], reader({"r/CLAUDE.md": content}), None), [])

    def test_exactly_at_cap_passes(self):
        """The boundary is <=, not <. Off-by-one here blocks a compliant commit.

        Sized from `lib.DEFAULT_MAX_LINES`, never a literal. These three used
        hardcoded 399/400/401 against a 400 default; when the default became 800
        on 2026-09-15 the two 'passes' cases stayed GREEN while testing nothing —
        399 and 400 are both a long way under 800, so neither sat on a boundary
        any more and an off-by-one in `check` would have sailed through both. A
        boundary test pinned to a literal stops being a boundary test the moment
        the boundary moves, and it does so silently.
        """
        content = "\n".join("line" for _ in range(lib.DEFAULT_MAX_LINES))
        self.assertEqual(
            lib.check([("r/CLAUDE.md", "r/CLAUDE.md")], reader({"r/CLAUDE.md": content}), None), [])

    def test_one_over_cap_blocks(self):
        """The other side of the boundary — proves <= is not <."""
        content = "\n".join("line" for _ in range(lib.DEFAULT_MAX_LINES + 1))
        found = lib.check([("r/CLAUDE.md", "r/CLAUDE.md")], reader({"r/CLAUDE.md": content}), None)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["over"], 1)

    def test_unreadable_file_is_skipped_not_blocked(self):
        def boom(path):
            raise IOError("gone")
        self.assertEqual(lib.check([("r/CLAUDE.md", "r/CLAUDE.md")], boom, None), [])

    def test_override_is_honoured_over_default(self):
        """An override beats the default in BOTH directions, tighter and looser.

        Overrides come from a config now rather than the shipped dict, which is
        empty. Both halves matter: a `cap_for` that ignored overrides entirely
        would satisfy the looser half on its own.
        """
        config = FakeConfig({"context_lint": {"overrides": MACHINERY_OVERRIDES}})
        content = "\n".join("line" for _ in range(500))
        path = "example-org/notes-repo/CLAUDE.md"
        # 500 > the 400 skills override -> blocks, though it is under the default
        self.assertEqual(len(lib.check([(path, path)], reader({path: content}), config)), 1)
        path2 = "example-org/claude-code-toolkit/CLAUDE.md"
        # 500 < the 630 toolkit override -> passes
        self.assertEqual(lib.check([(path2, path2)], reader({path2: content}), config), [])
        # ...and the same 500 lines under NO override passes on the default,
        # which is what makes the first assertion about the override rather
        # than about the size.
        path3 = "some-other-repo/CLAUDE.md"
        self.assertEqual(lib.check([(path3, path3)], reader({path3: content}), config), [])

    def test_message_names_the_file_and_the_override_path(self):
        found = lib.check([("r/CLAUDE.md", "r/CLAUDE.md")],
                          reader({"r/CLAUDE.md": "x\n" * (lib.DEFAULT_MAX_LINES + 1)}),
                          None)
        message = lib.format_block(found)
        self.assertIn("r/CLAUDE.md", message)
        self.assertIn("--no-verify", message)
        self.assertIn("context_lint.overrides", message)


class Invocation(unittest.TestCase):
    """The executable itself. These are the modes a person and a hook actually use."""

    def _run(self, args, cwd=None, stdin_closed=True):
        return subprocess.run(
            [sys.executable, EXE] + args, capture_output=True, text=True,
            cwd=cwd, timeout=30,
            stdin=subprocess.DEVNULL if stdin_closed else None)

    def test_report_does_not_hang_without_stdin(self):
        """Acceptance #3.

        capture_output does NOT redirect stdin, so a tool that reads it passes
        every piped test and hangs for the human running it by hand. Closing
        stdin here is what makes the test meaningful; the timeout is the assert.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "CLAUDE.md"), "w") as handle:
                handle.write("x\n" * 10)
            done = self._run(["--report", tmp])
        self.assertEqual(done.returncode, 0)
        self.assertIn("CLAUDE.md", done.stdout)

    def test_report_flags_an_over_cap_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "CLAUDE.md"), "w") as handle:
                handle.write("x\n" * (lib.DEFAULT_MAX_LINES + 100))
            done = self._run(["--report", tmp])
        self.assertIn("OVER by 100", done.stdout)

    def test_staged_outside_a_repo_allows_the_commit(self):
        """A git failure means 'could not tell', never 'blocked'."""
        with tempfile.TemporaryDirectory() as tmp:
            done = self._run(["--staged"], cwd=tmp)
        self.assertEqual(done.returncode, 0)

    def test_staged_blocks_a_real_over_cap_commit(self):
        """End-to-end positive control, through git, exactly as the hook runs it.

        This is the one test that proves the whole path works: lib, executable,
        git plumbing and exit code together. Everything else could pass with the
        block wired to nothing.
        """
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git"] + list(args), cwd=tmp, check=True,
                               capture_output=True, stdin=subprocess.DEVNULL)
            git("init", "-q")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            with open(os.path.join(tmp, "CLAUDE.md"), "w") as handle:
                handle.write("x\n" * (lib.DEFAULT_MAX_LINES + 100))
            git("add", "CLAUDE.md")
            done = self._run(["--staged"], cwd=tmp)
            self.assertEqual(done.returncode, 1, done.stderr)
            self.assertIn("BLOCKED", done.stderr)

            # And the negative control on the SAME repo: shrink it, and the
            # identical invocation must now pass. A block that fires regardless
            # of content would satisfy the assert above.
            with open(os.path.join(tmp, "CLAUDE.md"), "w") as handle:
                handle.write("x\n" * 10)
            git("add", "CLAUDE.md")
            done = self._run(["--staged"], cwd=tmp)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_staged_judges_the_staged_blob_not_the_worktree(self):
        """A big unstaged edit must not block a commit that stages a small one."""
        with tempfile.TemporaryDirectory() as tmp:
            def git(*args):
                subprocess.run(["git"] + list(args), cwd=tmp, check=True,
                               capture_output=True, stdin=subprocess.DEVNULL)
            git("init", "-q")
            git("config", "user.email", "t@example.com")
            git("config", "user.name", "t")
            path = os.path.join(tmp, "CLAUDE.md")
            with open(path, "w") as handle:
                handle.write("x\n" * 10)
            git("add", "CLAUDE.md")
            # Now blow up the WORKING TREE copy without staging it.
            with open(path, "w") as handle:
                handle.write("x\n" * 5000)
            done = self._run(["--staged"], cwd=tmp)
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_no_args_explains_itself_and_does_not_block(self):
        done = self._run([])
        self.assertEqual(done.returncode, 2)


class OverrideWiring(unittest.TestCase):
    """Overrides through a REAL git repo, which is where they were dead.

    Every cap assertion used to hand `cap_for` a synthetic path string, so no
    mutation of the override constant could reach the wiring — `git diff
    --cached --name-only` reports repo-root-relative paths (`CLAUDE.md`), every
    override key carried a repo component, and nothing matched on any commit in
    any repo. The lib was fine. The join between it and git was not. Same shape
    as the `base-pin` background-fetch gap named in CLAUDE.md's Testing section:
    an untested path has no mutation to survive.
    """

    def _repo(self, root, name, owner="example-org"):
        """A throwaway repo. `owner` becomes its `origin`, and that MATTERS.

        Shipped overrides are keyed `<owner>/<repo>/<path>`, so a fixture with
        no `origin` resolves only the bare key and silently gets the default —
        which is a real behaviour worth testing (see the no-remote test) but is
        NOT what most of these tests mean by "our repo". Pass `owner=None` to
        get a remote-less repo deliberately.
        """
        path = os.path.join(root, name)
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

    def _stage(self, repo, rel, lines):
        full = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write("x\n" * lines)
        subprocess.run(("git", "add", "-A"), cwd=repo, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)

    def _staged(self, repo, overrides=MACHINERY_OVERRIDES):
        """Run `--staged` in `repo` with `overrides` supplied through a real config.

        These tests used to pass CLAUDE_TOOLKIT_CONFIG=/nonexistent.json and lean
        on the shipped DEFAULT_OVERRIDES for their 630/400/135 numbers. That dict
        is empty since 2026-09-15 (one standardized cap), so the overrides now
        arrive the way a user's actually do — from a config file on disk.
        Every assertion below is unchanged; only the delivery path moved, and it
        moved onto the path people really use.

        Pass `overrides=None` for a genuinely config-less run.

        The file is written OUTSIDE the repo (its parent directory), or `git add
        -A` in `_stage` would stage the config as part of the fixture commit and
        the linter would then be counting its own settings file.
        """
        if overrides is None:
            config_path = "/nonexistent.json"
        else:
            config_path = write_config(os.path.dirname(repo), overrides=overrides)
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config_path)
        return subprocess.run([sys.executable, EXE, "--staged"], cwd=repo,
                              capture_output=True, text=True, timeout=60,
                              env=env, stdin=subprocess.DEVNULL)

    def test_two_repos_sharing_a_directory_name_do_not_share_a_cap(self):
        """THE DEFECT, reproduced end to end, through the executable.

        Two clones of different repos, both named `claude-code-toolkit`, both
        keyed as `claude-code-toolkit/CLAUDE.md`, so one repo's
        ratchet landed on the other clone too and put it far over the cap. Nothing
        announced it: `core.hooksPath` is global, so the check really runs
        there, and the block message names a cap that repo never set.

        Both halves are asserted, and the PASSING one is the control — without
        it a tool that blocked everything would satisfy the failing half.
        """
        with tempfile.TemporaryDirectory() as root:
            ours = self._repo(os.path.join(root, "a"), "claude-code-toolkit",
                              owner="example-org")
            theirs = self._repo(os.path.join(root, "b"), "claude-code-toolkit",
                                owner="someone-else")
            # 700 lines: OVER our 630 override, UNDER the 800 default. Only a
            # correct resolution separates the two repos at this size.
            #
            # The direction inverted on 2026-09-15 and the test means the same
            # thing. The override (630) is now TIGHTER than the default (800),
            # where it used to be looser than a 400 default — so "our repo is
            # judged by its own override" now shows as OURS BLOCKING and theirs
            # passing, rather than the reverse. Both halves are still here, and
            # the passing one is still the control: without it a tool that
            # blocked everything would satisfy the failing half.
            for repo in (ours, theirs):
                self._stage(repo, "CLAUDE.md", 700)
            mine = self._staged(ours)
            self.assertEqual(mine.returncode, 1,
                             "our own repo lost its override:\n"
                             + mine.stdout + mine.stderr)
            self.assertIn("cap 630", mine.stderr)
            other = self._staged(theirs)
            self.assertEqual(other.returncode, 0,
                             "a repo that merely SHARES THE NAME inherited our "
                             "630 cap:\n" + other.stdout + other.stderr)

    def test_a_bare_config_override_matches_a_key_cap_key_really_built(self):
        """Back-compat, through the REAL construction path.

        There is a sibling unit test that hands `cap_for` a `_QualifiedKey`
        built by hand. It passes even with the bare fallback deleted — caught
        by mutation, not by review — because it never exercises `cap_key`.
        This one resolves a genuine repo-with-a-remote and then asserts the
        BARE spelling still matches, which is the only form an override
        already sitting in someone's config uses.
        """
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "somerepo", owner="some-owner")
            path = os.path.join(repo, "CLAUDE.md")
            with open(path, "w") as handle:
                handle.write("x\n")
            key = lib.cap_key(path)
            self.assertEqual(
                list(key.candidates),
                ["some-owner/somerepo/CLAUDE.md", "somerepo/CLAUDE.md"],
                "cap_key did not offer the bare key as a fallback candidate")
            self.assertEqual(
                lib.cap_for(key, 400, {"somerepo/CLAUDE.md": 777}), 777,
                "a bare override stopped matching — silent for every config "
                "written before the owner prefix existed")

    def test_a_repo_with_no_remote_falls_back_to_the_bare_key(self):
        """No `origin` is not an error — it is a repo we cannot name.

        It gets the bare key and therefore the default, rather than crashing
        or silently matching someone else's override.

        The OBSERVABLE flipped on 2026-09-15 while the property did not, and the
        reason is worth stating so nobody "restores" the old assertion. The
        override in play is 630 and the default is now 800, so the default is the
        LOOSER of the two — falling back to it means the commit PASSES at 700
        lines. Before the standardized cap the default was 400, tighter than 630,
        so the same fallback showed up as a block. Asserting `returncode == 1`
        here today would pass only if the repo had wrongly INHERITED the 630
        override, i.e. it would assert the defect.

        The 900-line control is what keeps this from being vacuous: without it,
        a linter that had stopped checking entirely would satisfy the 700 case.
        """
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "claude-code-toolkit", owner=None)
            self.assertEqual(lib.repo_owner(repo), None)
            # 700: over the 630 override this repo must NOT match, under the 800
            # default it must. Only the correct resolution passes.
            self._stage(repo, "CLAUDE.md", 700)
            done = self._staged(repo)
            self.assertEqual(done.returncode, 0,
                             "a remote-less repo inherited the qualified 630 "
                             "override:\n" + done.stdout + done.stderr)
            # Control: the cap it DID get is real and still fires.
            self._stage(repo, "CLAUDE.md", 900)
            blocked = self._staged(repo)
            self.assertEqual(blocked.returncode, 1,
                             "the default cap never fires at all")
            self.assertIn("cap 800", blocked.stderr)

    def test_owner_matching_is_case_insensitive(self):
        """Forge URL segments are case-insensitive; git stores the case typed.

        So `github.com/Example-Org/infra` is the same repo as the
        canonical spelling and must get the same cap. The direction is what
        makes this worth a test: for a LOOSER override the mismatch is loud
        (commits blocked at 400), but for a TIGHTER one — infra at 135 —
        the cap silently stops applying and an over-cap file becomes
        indistinguishable from a file under cap. That is this guard's own
        failure shape.
        """
        overrides = MACHINERY_OVERRIDES
        for spelling in ("Example-Org", "EXAMPLE-ORG",
                         "example-org"):
            with tempfile.TemporaryDirectory() as root:
                repo = self._repo(root, "infra", owner=spelling)
                path = os.path.join(repo, "CLAUDE.md")
                with open(path, "w") as handle:
                    handle.write("x\n")
                self.assertEqual(lib.repo_owner(repo), "example-org",
                                 "owner not normalised for %r" % spelling)
                self.assertEqual(
                    lib.cap_for(lib.cap_key(path), 400, overrides), 135,
                    "the infra ratchet stopped applying for a remote "
                    "spelled %r — silently" % spelling)

    def test_a_global_remote_origin_url_is_not_mistaken_for_the_repos(self):
        """`git config --get` falls through to ~/.gitconfig; `--local` does not.

        Without `--local`, a machine carrying a global `remote.origin.url`
        hands EVERY remote-less repo an owner it does not have — and makes the
        no-remote test below pass or fail depending on whose laptop it runs on.
        """
        with tempfile.TemporaryDirectory() as root:
            home = os.path.join(root, "home")
            os.makedirs(home)
            with open(os.path.join(home, ".gitconfig"), "w") as handle:
                handle.write('[remote "origin"]\n\turl = '
                             'https://github.com/not-this-repo/nope.git\n')
            repo = self._repo(root, "somerepo", owner=None)
            probe = ("import sys; sys.path.insert(0, %r); "
                     "import context_lint_lib as l; print(l.repo_owner(%r))"
                     % (TOOL, repo))
            done = subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, text=True,
                env=dict(os.environ, HOME=home,
                         XDG_CONFIG_HOME=os.path.join(home, ".config")),
                stdin=subprocess.DEVNULL)
            self.assertEqual(done.stdout.strip(), "None",
                             "a GLOBAL remote.origin.url leaked in as this "
                             "repo's owner: " + done.stdout + done.stderr)

    def test_ssh_and_https_remotes_yield_the_same_owner(self):
        """A machine using SSH for one clone and HTTPS for another would
        otherwise key the same repo two different ways."""
        with tempfile.TemporaryDirectory() as root:
            https = self._repo(os.path.join(root, "h"), "repo1",
                               owner="example-org")
            ssh = self._repo(os.path.join(root, "s"), "repo2", owner=None)
            subprocess.run(("git", "remote", "add", "origin",
                            "git@github.com:example-org/repo2.git"),
                           cwd=ssh, check=True, capture_output=True,
                           stdin=subprocess.DEVNULL)
            self.assertEqual(lib.repo_owner(https), "example-org")
            self.assertEqual(lib.repo_owner(ssh), "example-org")

    def test_the_repos_own_override_actually_applies_at_commit_time(self):
        """The BLOCKING defect: every override was once dead at commit time.

        The fixture must sit BETWEEN the override and the default or this test
        asserts nothing. It was 500 lines — over the then-400 default, under the
        630 override. When the default became 800 on 2026-09-15, 500 was under
        BOTH, so `returncode == 0` held whether or not the override resolved.
        Mutation-proven vacuous: a `cap_for` rewritten to ignore every override
        survived it.

        700 restores the gap, with the sign flipped: the override (630) is now
        the TIGHTER number, so a resolved override BLOCKS and an unresolved one
        falls to 800 and passes. Same property, opposite observable.
        """
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "claude-code-toolkit")
            self._stage(repo, "CLAUDE.md", 700)
            done = self._staged(repo)
        self.assertEqual(done.returncode, 1,
                         "the repo's own override did not apply: %s" % done.stderr)
        self.assertIn("cap 630", done.stderr)

    def test_that_override_still_blocks_past_its_own_ceiling(self):
        """Negative control. Without it, a cap_for that returned a huge number
        for everything would satisfy the test above."""
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "claude-code-toolkit")
            self._stage(repo, "CLAUDE.md", 1200)
            done = self._staged(repo)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("cap 630", done.stderr)

    def test_a_tighter_override_applies_to_its_own_repo(self):
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "infra")
            self._stage(repo, "CLAUDE.md", 200)   # over the 135 ratchet
            done = self._staged(repo)
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("cap 135", done.stderr)

    def test_an_unrelated_repo_does_not_inherit_that_ratchet(self):
        """The suffix collision, end to end. This hook runs on EVERY repo on the
        machine, including ones this toolkit has never heard of."""
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "some-unrelated-repo")
            self._stage(repo, "infra/CLAUDE.md", 200)
            done = self._staged(repo)
        self.assertEqual(done.returncode, 0,
                         "someone else's 135-line ratchet was applied: %s" % done.stderr)

    def test_a_nested_claude_md_does_not_inherit_the_root_files_cap(self):
        """The key carries the full path within the repo, not just the basename.

        Found by a surviving mutant: collapsing `rel` to a basename let
        `claude-code-toolkit/sub/CLAUDE.md` key as the root file and take its
        630 ceiling. A nested CLAUDE.md is a different file with a different
        budget, and it gets the default.

        Direction inverted 2026-09-15 with the standardized 800 default — the
        root override (630) is now the TIGHTER number, so borrowing it shows up
        as a block rather than as a pass. 700 is the only size that separates
        them; the 900 control proves the default cap is real and still fires.
        """
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "claude-code-toolkit")
            self._stage(repo, "sub/CLAUDE.md", 700)
            done = self._staged(repo)
            self.assertEqual(done.returncode, 0,
                             "a nested CLAUDE.md borrowed the root file's 630 "
                             "cap:\n" + done.stdout + done.stderr)
            self._stage(repo, "sub/CLAUDE.md", 900)
            blocked = self._staged(repo)
        self.assertEqual(blocked.returncode, 1,
                         "the nested file has no working cap at all")
        self.assertIn("cap 800", blocked.stderr)

    def test_a_linked_worktree_keys_the_same_as_the_primary_checkout(self):
        """`--git-common-dir`, not `--show-toplevel`. The worktree's own basename
        is a branch nickname; keying on it makes the same commit pass or fail
        depending on where it was made."""
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "claude-code-toolkit")
            self._stage(repo, "seed.txt", 1)
            subprocess.run(("git", "commit", "-qm", "seed", "--no-verify"),
                           cwd=repo, check=True, capture_output=True,
                           stdin=subprocess.DEVNULL)
            wt = os.path.join(root, "wt-nickname")
            subprocess.run(("git", "worktree", "add", "-q", wt, "-b", "b"),
                           cwd=repo, check=True, capture_output=True,
                           stdin=subprocess.DEVNULL)
            # 700 sits BETWEEN the 630 override and the 800 default, which is
            # the only band where the two keyings differ. At 500 — under both —
            # a worktree keying as `wt-nickname/CLAUDE.md` and one keying as
            # `claude-code-toolkit/CLAUDE.md` both passed, so the historical
            # regression this test is named for was invisible to it.
            self._stage(wt, "CLAUDE.md", 700)
            done = self._staged(wt)
        self.assertEqual(done.returncode, 1,
                         "the worktree resolved a different cap — it keyed on its "
                         "own basename instead of the common dir: %s" % done.stderr)
        self.assertIn("cap 630", done.stderr)

    def test_report_and_staged_agree_on_the_same_file(self):
        """They disagreed: --report said `ok` on a file --staged refused, which
        is what would have hidden the blocking defect from anyone looking.

        BOTH MODES MUST READ THE SAME CONFIG or this test compares two different
        cap sets and proves nothing about agreement. It used to pass
        /nonexistent.json to `--report` while `--staged` leaned on the shipped
        override dict; those happened to coincide only because the shipped dict
        WAS the no-config answer. Once overrides moved into a real config file
        that coincidence broke, and the test would have gone green again the
        moment someone "fixed" it by loosening the assertion.
        """
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "infra")
            self._stage(repo, "CLAUDE.md", 200)   # over the 135 infra entry
            staged = self._staged(repo)
            config_path = write_config(os.path.dirname(repo),
                                       overrides=MACHINERY_OVERRIDES)
            env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG=config_path)
            report = subprocess.run([sys.executable, EXE, "--report", repo],
                                    capture_output=True, text=True, timeout=60,
                                    env=env, stdin=subprocess.DEVNULL)
            check = subprocess.run([sys.executable, EXE, "--check", repo],
                                   capture_output=True, text=True, timeout=60,
                                   env=env, stdin=subprocess.DEVNULL)
        self.assertEqual(staged.returncode, 1, staged.stderr)
        self.assertIn("OVER", report.stdout,
                      "--report certified a file --staged refused:\n%s" % report.stdout)
        # THREE modes now, and --check is the one on the merge path. A --check
        # that disagreed with --staged would let a merge land what a commit
        # refused, which is the same defect one level up.
        self.assertEqual(check.returncode, 1,
                         "--check passed a file --staged refused:\n"
                         + check.stdout + check.stderr)


class CheckMode(unittest.TestCase):
    """`--check`: the CI gate. The mode that can actually turn a merge red.

    It exists because NEITHER other mode can. Observed against a
    repo genuinely committed over its cap:
    `--staged` in a clean checkout found nothing staged and exited 0, and
    `--report` detected the file correctly and exited 0 by design. A CI job wired
    to either would have been permanently, vacuously green — which is worse than
    no job, because it reports enforcement that is not happening.
    """

    def _run(self, args, env=None):
        run_env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        run_env.update(env or {})
        return subprocess.run([sys.executable, EXE] + args, capture_output=True,
                              text=True, timeout=60, env=run_env,
                              stdin=subprocess.DEVNULL)

    def _tree(self, tmp, lines, name="CLAUDE.md"):
        path = os.path.join(tmp, name)
        with open(path, "w") as handle:
            handle.write("x\n" * lines)
        return path

    def test_over_cap_is_red(self):
        """THE POSITIVE CONTROL. Every 'passes' assertion below is vacuous
        without it — a --check wired to nothing satisfies all of them."""
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp, lib.DEFAULT_MAX_LINES + 1)
            done = self._run(["--check", tmp])
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("BLOCKED", done.stderr)
        self.assertIn("1 over", done.stderr)

    def test_at_cap_is_green(self):
        """The boundary is <=, matching --staged. Off-by-one here turns every
        compliant PR in three repos red, which is how a gate gets deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp, lib.DEFAULT_MAX_LINES)
            done = self._run(["--check", tmp])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("within cap", done.stdout)

    def test_a_walk_that_found_nothing_is_RED_not_green(self):
        """A check that examined nothing has not established anything.

        The failure it catches is a wrong path or a changed filter, and both
        look EXACTLY like a clean repo from the exit code alone. Same trap
        ci/local-checks.sh guards one level up with its `ran -eq 0` check.
        """
        with tempfile.TemporaryDirectory() as tmp:
            done = self._run(["--check", tmp])
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("found no context file", done.stderr)

    def test_a_core_over_cap_is_red_too(self):
        """Not just the whole-file cap — the Core cap gates the merge as well."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "CLAUDE.md"), "w") as handle:
                handle.write("<!-- CORE:START -->\n")
                handle.write("x\n" * (lib.DEFAULT_CORE_MAX_LINES + 10))
                handle.write("<!-- CORE:END -->\n")
            done = self._run(["--check", tmp])
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("Core is", done.stderr)

    def test_an_advisory_finding_alone_does_not_turn_the_build_red(self):
        """Mirrors --staged's asymmetry exactly, and for the same reason.

        A dangling cross-reference is a heuristic on PROSE. A heuristic that
        fails a merge is one somebody routes around — and the counted size caps
        go with it. Printed, never fatal.

        THE FIXTURE MUST ACTUALLY TRIP THE ADVISORY, and the first version of
        this test did not — mutation caught it, review would not have. It used
        "See the section below for the full rules", which matches none of
        `core_block._DANGLING_RES`: the patterns are qualified (`see below`,
        `the section on`, `§`), deliberately, because a bare "below" is a
        preposition far more often than a cross-reference. With no finding
        produced, the test asserted `returncode == 0` on an ordinary clean tree
        — true of every possible implementation, including one that failed the
        build on every advisory.
        """
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "CLAUDE.md"), "w") as handle:
                handle.write("<!-- CORE:START -->\n")
                handle.write("Run the tests before committing; see below.\n")
                handle.write("<!-- CORE:END -->\n")
            done = self._run(["--check", tmp])
        # POSITIVE CONTROL first: prove the advisory fired at all. Without this
        # line the assertion beneath it is free.
        self.assertIn("ADVISORY", done.stdout,
                      "the fixture produced no advisory finding, so the exit "
                      "code below proves nothing:\n" + done.stdout)
        self.assertEqual(done.returncode, 0,
                         "an advisory finding turned the build red:\n"
                         + done.stdout + done.stderr)

    def test_check_FAILS_CLOSED_where_report_fails_open(self):
        """THE ASYMMETRY, and the whole reason --check is a third mode.

        Same crash, two directions: on the merge path an unexpected exception is
        a gate that stopped checking and must be RED; in a pre-commit hook it
        must never wedge a commit over a bug in a line-counting tool.

        Driven through `cli()` with `_collect` monkeypatched to raise — a real
        exception on the real code path, not a simulated one. Both halves are
        asserted in one test on purpose: they are one decision, and a test that
        checked only the fail-closed half would still pass if the fail-OPEN
        branch had been broken into failing closed too, silently turning every
        commit on the machine into a refusal.
        """
        module = load_executable_module("check_mode_exe")

        def boom(root):
            raise RuntimeError("the walker is on fire")

        original = module._collect
        module._collect = boom
        try:
            self.assertEqual(module.cli(["claude-context-lint", "--check", "/x"]), 1,
                             "--check swallowed a crash and reported success")
            self.assertEqual(module.cli(["claude-context-lint", "--report", "/x"]), 0,
                             "--report stopped failing open, which wedges commits")
        finally:
            module._collect = original

    def test_an_UNREADABLE_context_file_is_RED_not_skipped(self):
        """The fail-open hole `--check` inherited from the human report.

        `_collect` skips a file that raises on read, which is right for
        `--report` and a hole in a gate: the empty-walk guard only fires when
        the WHOLE set is empty, so a tree with one readable in-cap file and one
        unreadable over-cap file reported "all within cap" and exited 0.

        Three independent causes, because they reach different layers of the
        read path and a fix for one need not fix the others — a non-UTF-8 byte
        (UnicodeDecodeError, inside decode), a dangling symlink
        (FileNotFoundError, at open), and mode 000 (PermissionError, at open).

        The in-cap readable file is load-bearing: without it the tree would trip
        the found-nothing guard instead, and this test would pass for the wrong
        reason while the actual hole stayed open.
        """
        for kind in ("undecodable", "dangling-symlink", "mode-000"):
            with tempfile.TemporaryDirectory() as tmp:
                self._tree(tmp, 100)                    # readable, within cap
                sub = os.path.join(tmp, "sub")
                os.makedirs(sub)
                target = os.path.join(sub, "CLAUDE.md")
                if kind == "undecodable":
                    with open(target, "wb") as handle:
                        handle.write(b"\xff\xfe" + b"x\n" * 2000)
                elif kind == "dangling-symlink":
                    os.symlink(os.path.join(tmp, "nope"), target)
                else:
                    with open(target, "w") as handle:
                        handle.write("x\n" * 2000)
                    os.chmod(target, 0o000)
                try:
                    done = self._run(["--check", tmp])
                finally:
                    if kind == "mode-000":
                        os.chmod(target, 0o600)
                self.assertEqual(done.returncode, 1,
                                 "%s: an unmeasurable file reported as within "
                                 "cap:\n%s%s" % (kind, done.stdout, done.stderr))
                self.assertIn("could not read", done.stderr, kind)

    def test_an_unreadable_CORE_ONLY_file_is_RED_too(self):
        """TOOLING.md reaches `unreadable` through a DIFFERENT loop.

        `is_context_file` is CLAUDE.md-only while `is_core_file` also covers
        TOOLING.md and its template, so the whole-file candidate set is a strict
        SUBSET of the Core one. Every CLAUDE.md is therefore appended to
        `unreadable` twice — once per loop — and mutating either append alone
        survives, which is exactly what the mutation harness reported.

        That redundancy is harmless (the result is de-duplicated) but it hides
        the Core loop: a core-ONLY file is reachable through nothing else. This
        is the case that covers it, and without it the Core append has no test
        of its own.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp, 100)                      # a readable CLAUDE.md
            with open(os.path.join(tmp, "TOOLING.md"), "wb") as handle:
                handle.write(b"\xff\xfe" + b"x\n" * 2000)
            done = self._run(["--check", tmp])
        self.assertEqual(done.returncode, 1,
                         "an unreadable core-only file was skipped:\n"
                         + done.stdout + done.stderr)
        self.assertIn("TOOLING.md", done.stderr)

    def test_report_still_SKIPS_an_unreadable_file(self):
        """The other half of the same asymmetry, and it must not move.

        `--report` is the human view and fails open: it shows what it could
        read rather than refusing. Asserted explicitly because the fix above is
        a one-line change away from making the report refuse too, which would
        be a regression nothing else here would catch.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp, 100)
            sub = os.path.join(tmp, "sub")
            os.makedirs(sub)
            with open(os.path.join(sub, "CLAUDE.md"), "wb") as handle:
                handle.write(b"\xff\xfe" + b"x\n" * 2000)
            done = self._run(["--report", tmp])
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("CLAUDE.md", done.stdout)

    def test_usage_names_every_mode(self):
        """A mode absent from the usage text is a mode nobody finds."""
        done = self._run([])
        self.assertEqual(done.returncode, 2)
        for mode in ("--staged", "--report", "--check"):
            self.assertIn(mode, done.stderr, "usage does not mention %s" % mode)

    def test_check_and_report_walk_the_same_tree(self):
        """One walk, two modes. A report that says `ok` on a file the gate
        refuses is what hides the refusal — the defect context_lint_lib's module
        docstring records, which is why _collect is shared."""
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp, lib.DEFAULT_MAX_LINES + 50)
            report = self._run(["--report", tmp])
            check = self._run(["--check", tmp])
        self.assertIn("OVER by 50", report.stdout)
        self.assertIn("OVER by 50", check.stdout)
        self.assertEqual(report.returncode, 0)
        self.assertEqual(check.returncode, 1)

    def test_check_does_not_read_stdin(self):
        """Run by hand it must not hang. `communicate()` is deliberately NOT
        used here: it closes stdin immediately, which is the same instant EOF as
        DEVNULL, so a `sys.stdin.read()` in main() would pass it — that exact
        vacuous test shipped in PR #147 and mutation caught it, not review.
        An inherited terminal stdin is what makes the timeout meaningful."""
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp, 10)
            done = subprocess.run(
                [sys.executable, EXE, "--check", tmp], capture_output=True,
                text=True, timeout=30,
                env=dict(os.environ, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json"))
        self.assertEqual(done.returncode, 0)


def load_executable_module(name="claude_context_lint_exe"):
    """Import `claude-context-lint` despite it having no .py extension.

    The executable is not importable by `import`, which is the whole reason
    the loader helper exists. SourceFileLoader is how you reach into one anyway, and it is
    needed here to assert a property of the executable's own loader — one that
    no subprocess can observe from outside. `__name__` is not "__main__", so
    the module's `sys.exit(main(...))` block does not fire on import.
    """
    import importlib.machinery
    import importlib.util
    loader = importlib.machinery.SourceFileLoader(name, EXE)
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestKeyFunctionsAreImportable(unittest.TestCase):
    """C6: plan A3's growth_lib needs these, and an executable cannot be imported.

    The point is not that they work — OverrideWiring already proves that
    through the executable. It is that they are reachable from a second
    consumer at all.
    """

    def test_cap_key_and_repo_dirs_are_on_the_lib(self):
        self.assertTrue(callable(lib.cap_key))
        self.assertTrue(callable(lib.repo_dirs))

    def test_the_executable_no_longer_defines_them(self):
        # Criterion 1, asserted on CONTENTS rather than on an rg exit code.
        # `rg ... returns nothing` is satisfied by failure: a wrong cwd, a
        # missing path and a typo all exit 2 with empty stdout, so the command
        # that proves the three functions were removed passes trivially from
        # any directory that lacks tools/context-lint/.
        with open(EXE) as handle:
            source = handle.read()
        for name in ("def _git", "def repo_dirs", "def cap_key"):
            self.assertNotIn(name, source, "%s is still defined in %s" % (name, EXE))
        # Positive control: the file we just read is the real one, not an empty
        # string that would satisfy every assertion above.
        self.assertIn("def run_staged", source)

    def test_load_libs_is_memoized_so_sys_path_grows_once(self):
        """Criterion 6, with teeth.

        `_staged_reader` is passed by reference into `lib.check()` and called
        once per staged file. An unmemoized `_load_libs` therefore inserts two
        sys.path entries per staged file. No subprocess can see that; this can.
        """
        module = load_executable_module()
        depth = len(sys.path)
        try:
            first = module._load_libs()
            after_first = len(sys.path)
            second = module._load_libs()
            self.assertIs(first, second, "_load_libs is not memoized")
            self.assertEqual(len(sys.path), after_first,
                             "a second _load_libs() call grew sys.path")
            self.assertEqual(after_first, depth + 2,
                             "the loader no longer inserts its two paths")
        finally:
            del sys.path[:len(sys.path) - depth]

    def test_cap_key_returns_none_outside_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "CLAUDE.md")
            with open(path, "w") as handle:
                handle.write("x\n")
            # A temp dir is not a git repo. This is the exact state of a
            # workspace-root CLAUDE.md outside any repo, and the reason
            # context_growth has its own path-keyed override map.
            self.assertIsNone(lib.cap_key(path))

    def test_cap_key_resolves_inside_a_repo(self):
        with tempfile.TemporaryDirectory() as root:
            repo = os.path.join(root, "some-repo")
            os.makedirs(repo)
            for args in (("init", "-q"), ("config", "user.email", "t@e.com"),
                         ("config", "user.name", "t")):
                subprocess.run(("git",) + args, cwd=repo, check=True,
                               capture_output=True, stdin=subprocess.DEVNULL)
            path = os.path.join(repo, "CLAUDE.md")
            with open(path, "w") as handle:
                handle.write("x\n")
            self.assertEqual(lib.cap_key(path), "some-repo/CLAUDE.md")

CORE_FIXTURE = ("# Title\n"
                "<!-- CORE:START -->\n"
                + "\n".join("rule %d" % i for i in range(90))
                + "\n<!-- CORE:END -->\n"
                "body line\n")


def counting_reader(mapping):
    """A reader that records what it was asked for.

    The call log is the only way to assert "filtered BEFORE reading". A reader
    that merely raises cannot: `core_findings` swallows read errors by design,
    so a read-then-filter implementation would still return [] and pass.
    """
    calls = []

    def read(path):
        calls.append(path)
        return mapping[path]
    return read, calls


class TestCoreFileScope(unittest.TestCase):
    """C14: the filename list, and that it is applied before any read.

    Two lists, two jobs. `is_context_file` gates the whole-file cap and stays
    CLAUDE.md-only; `is_core_file` gates both new Core checks and includes
    TOOLING.md, which is the only file that has a Core today.
    """

    def test_the_four_names_are_in_scope(self):
        for name in ("CLAUDE.md", "CLAUDE.md.template",
                     "TOOLING.md", "TOOLING.md.template"):
            self.assertTrue(lib.is_core_file("some/dir/" + name), name)

    def test_an_ordinary_file_is_not(self):
        for name in ("README.md", "notes.md", "TOOLING.md.bak", "CLAUDE.mdx"):
            self.assertFalse(lib.is_core_file("some/dir/" + name), name)

    def test_is_context_file_is_unchanged_and_still_excludes_tooling(self):
        # The two lists must not be conflated. Widening is_context_file would
        # put a long TOOLING.md under the CLAUDE.md whole-file cap
        # and report "a four-figure overage" on every edit.
        self.assertTrue(lib.is_context_file("a/CLAUDE.md"))
        self.assertFalse(lib.is_context_file("a/TOOLING.md"))
        self.assertFalse(lib.is_context_file("a/TOOLING.md.template"))

    def test_core_files_falls_back_on_a_broken_config(self):
        class Broken(object):
            def get(self, *keys, **kwargs):
                raise ValueError("malformed")
        self.assertEqual(tuple(lib.core_files(Broken())), lib.DEFAULT_CORE_FILES)

    def test_core_findings_does_not_read_an_out_of_scope_file(self):
        # THE point of the list. `entries` in --staged is every staged path,
        # and `_staged_reader` forks `git show :<path>` with a 10s timeout per
        # call, on every repo on this machine via a global core.hooksPath.
        read, calls = counting_reader({"a/README.md": CORE_FIXTURE,
                                       "a/vendor/thing.lock": "x\n"})
        entries = [("a/README.md", "a/README.md"),
                   ("a/vendor/thing.lock", "a/vendor/thing.lock")]
        self.assertEqual(lib.core_findings(entries, read), [])
        self.assertEqual(calls, [], "read an out-of-scope file: %r" % calls)

    def test_sufficiency_findings_does_not_read_an_out_of_scope_file(self):
        read, calls = counting_reader({"a/README.md": "The rule; see below.\n"})
        entries = [("a/README.md", "a/README.md")]
        self.assertEqual(lib.sufficiency_findings(entries, read), [])
        self.assertEqual(calls, [], "read an out-of-scope file: %r" % calls)


class TestCoreCaps(unittest.TestCase):

    def test_the_fixture_is_the_shape_the_other_tests_assume(self):
        # Stated once, asserted, never repeated in prose. The pre-split draft
        # said "93 lines" three times about a 94-line fixture, which sends the
        # debug at the code under test instead of at the fixture.
        import core_block
        self.assertEqual(lib.count_lines(CORE_FIXTURE), 94)
        self.assertEqual(lib.count_lines(core_block.extract(CORE_FIXTURE)), 90)
        self.assertLess(94, lib.DEFAULT_MAX_LINES)

    def test_core_over_cap_is_flagged_while_whole_file_is_under(self):
        # 94 lines total, so the whole-file whole-file cap is not in play. This
        # combination IS the new behaviour, so it is the one to assert.
        entries = [("alpha/CLAUDE.md", "alpha/CLAUDE.md")]
        read = reader({"alpha/CLAUDE.md": CORE_FIXTURE})
        found = lib.core_findings(entries, read)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["core_lines"], 90)
        self.assertEqual(found[0]["cap"], lib.DEFAULT_CORE_MAX_LINES)
        self.assertEqual(found[0]["over"], 90 - lib.DEFAULT_CORE_MAX_LINES)
        self.assertEqual(lib.check(entries, read), [])

    def test_a_tooling_md_core_over_cap_IS_flagged(self):
        # The BLOCKING half of C14. The pre-split draft gated this on
        # is_context_file, exempting the one file that has a Core today from
        # the one cap slice 2e was written to enforce.
        entries = [("r/TOOLING.md", "r/TOOLING.md")]
        found = lib.core_findings(entries, reader({"r/TOOLING.md": CORE_FIXTURE}))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["core_lines"], 90)
        # And it is NOT under the whole-file cap, which is CLAUDE.md's.
        self.assertEqual(lib.check(entries, reader({"r/TOOLING.md": "x\n" * 5000})), [])

    def test_a_tooling_md_template_core_over_cap_IS_flagged(self):
        # Tracked, carries DIGEST markers, and seeds every new user's map via
        # /onboard — so an over-cap Core here propagates to machines where
        # digest_lib.cap_block() withholds the whole block.
        self.assertEqual(len(lib.core_findings(
            [("r/TOOLING.md.template", "r/TOOLING.md.template")],
            reader({"r/TOOLING.md.template": CORE_FIXTURE}))), 1)

    def test_core_under_cap_is_not_flagged(self):
        text = "<!-- CORE:START -->\nrule one\n<!-- CORE:END -->\n"
        self.assertEqual(lib.core_findings(
            [("a/CLAUDE.md", "a/CLAUDE.md")], reader({"a/CLAUDE.md": text})), [])

    def test_a_file_with_no_core_block_is_not_flagged(self):
        self.assertEqual(lib.core_findings(
            [("a/CLAUDE.md", "a/CLAUDE.md")], reader({"a/CLAUDE.md": "plain\n" * 500})), [])

    def test_an_out_of_scope_name_is_skipped_even_carrying_a_core(self):
        self.assertEqual(lib.core_findings(
            [("a/README.md", "a/README.md")], reader({"a/README.md": CORE_FIXTURE})), [])

    def test_an_unreadable_file_is_skipped_not_crashed(self):
        def boom(path):
            raise IOError("nope")
        self.assertEqual(lib.core_findings(
            [("a/CLAUDE.md", "a/CLAUDE.md")], boom), [])

    def test_a_none_content_read_is_skipped(self):
        self.assertEqual(lib.core_findings(
            [("a/CLAUDE.md", "a/CLAUDE.md")], lambda p: None), [])

    def test_core_caps_fall_back_to_shipped_defaults_on_a_broken_config(self):
        class Broken(object):
            def get(self, *keys, **kwargs):
                raise ValueError("malformed")
        default, overrides = lib.core_caps(Broken())
        self.assertEqual(default, lib.DEFAULT_CORE_MAX_LINES)
        self.assertIsInstance(overrides, dict)

    def test_a_core_override_is_honoured_for_an_exact_key(self):
        class Cfg(object):
            def get(self, *keys, **kwargs):
                if keys[:2] == ("context_lint", "core_overrides"):
                    return {"alpha/CLAUDE.md": 200}
                return kwargs.get("default")
        self.assertEqual(lib.core_findings(
            [("alpha/CLAUDE.md", "alpha/CLAUDE.md")],
            reader({"alpha/CLAUDE.md": CORE_FIXTURE}), Cfg()), [])


class TestSufficiency(unittest.TestCase):
    def test_flags_a_dangling_ref_inside_the_core(self):
        text = "<!-- CORE:START -->\nThe rule holds; see below.\n<!-- CORE:END -->\n"
        found = lib.sufficiency_findings(
            [("a/CLAUDE.md", "a/CLAUDE.md")], reader({"a/CLAUDE.md": text}))
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0]["refs"])

    def test_does_not_flag_the_same_string_in_the_body(self):
        # The check must extract the Core first. Scanning the whole file makes
        # every elaborated rule a finding, which is the noise that kills it.
        text = ("<!-- CORE:START -->\nThe rule holds.\n<!-- CORE:END -->\n"
                "Elaboration; see below.\n")
        self.assertEqual(lib.sufficiency_findings(
            [("a/CLAUDE.md", "a/CLAUDE.md")], reader({"a/CLAUDE.md": text})), [])

    def test_a_file_with_no_core_is_not_flagged(self):
        self.assertEqual(lib.sufficiency_findings(
            [("a/CLAUDE.md", "a/CLAUDE.md")], reader({"a/CLAUDE.md": "see below\n"})), [])

    def test_a_tooling_md_with_a_core_IS_in_scope(self):
        # C14: is_context_file excludes TOOLING.md, which is the only file that
        # has a Core today. The Core checks use their own filename list.
        text = "<!-- DIGEST:START -->\nRule; see below.\n<!-- DIGEST:END -->\n"
        found = lib.sufficiency_findings(
            [("r/TOOLING.md", "r/TOOLING.md")], reader({"r/TOOLING.md": text}))
        self.assertEqual(len(found), 1)


class CoreThroughTheExecutable(OverrideWiring):
    """C5 — the asymmetry, driven through the real binary.

    Nothing in the pre-split draft tested that either new check is ever OUTPUT.
    `--report` exits 0 by construction, so asserting exit 0 there proves nothing
    — and neither does asserting the path appears in stdout, because the
    whole-file table already prints it. Both halves of the `--report` assertion
    below are therefore on CONTENT the unmodified tool cannot produce: the
    counted Core lines, the Core cap, and the overage.

    Inherits OverrideWiring for its `_repo` / `_stage` / `_staged` helpers, which
    already pin CLAUDE_TOOLKIT_CONFIG=/nonexistent.json on the subprocess (C16).
    """

    def _stage_text(self, repo, rel, text):
        full = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as handle:
            handle.write(text)
        subprocess.run(("git", "add", "-A"), cwd=repo, check=True,
                       capture_output=True, stdin=subprocess.DEVNULL)

    def _report(self, root):
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        return subprocess.run([sys.executable, EXE, "--report", root],
                              capture_output=True, text=True, timeout=60,
                              env=env, stdin=subprocess.DEVNULL)

    def test_a_core_over_cap_blocks_the_commit(self):
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "some-repo")
            self._stage_text(repo, "CLAUDE.md", CORE_FIXTURE)
            done = self._staged(repo)
        # 94 lines total: well under DEFAULT_MAX_LINES, so only the Core cap can
        # be producing this. The overage is the part that proves it counted.
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("CLAUDE.md", done.stderr)
        self.assertIn("10 over", done.stderr)

    def test_a_staged_tooling_md_core_over_cap_blocks_the_commit(self):
        # C14's blocking half, through the real binary. This is the case the
        # pre-split draft could not produce at all.
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "some-repo")
            self._stage_text(repo, "TOOLING.md", CORE_FIXTURE)
            done = self._staged(repo)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        self.assertIn("TOOLING.md", done.stderr)

    def test_a_sufficiency_finding_alone_does_not_block(self):
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "some-repo")
            self._stage_text(
                repo, "CLAUDE.md",
                "<!-- CORE:START -->\nThe rule holds; see below.\n<!-- CORE:END -->\n")
            done = self._staged(repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("see below", done.stdout + done.stderr)

    def test_report_prints_the_counted_core_row_and_stays_zero(self):
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "some-repo")
            with open(os.path.join(repo, "CLAUDE.md"), "w") as handle:
                handle.write(CORE_FIXTURE)
            done = self._report(repo)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # The Core row: counted lines, cap, overage. None of these three
        # numbers can appear on unmodified code — the whole-file table for
        # this file reads "94  <DEFAULT_MAX_LINES>  ok".
        self.assertRegex(done.stdout, r"CLAUDE\.md\s+90\s+80\s+OVER by 10")
        # Negative control: the pre-existing whole-file table is still there,
        # so the Core section was ADDED rather than replacing it.
        self.assertRegex(done.stdout,
                         r"CLAUDE\.md\s+94\s+%d\s+ok" % lib.DEFAULT_MAX_LINES)

    def test_report_and_staged_agree_that_tooling_md_is_core_scoped(self):
        """The report/staged disagreement, on the new checks.

        `context_lint_lib.py`'s module docstring (lines 32-34) records a
        `--report` that certified `ok` on the exact file the hook refused as
        the defect that "would have kept the first defect from being noticed."
        `run_report` collecting Core rows on a narrower list than `--staged`
        uses would rebuild it.

        **The cap here is the DIGEST budget, not `core_max_lines`.** The map's
        Core is withheld by `digest_lib` past `tooling_digest.max_lines`, so a
        Core longer than that costs every session the WHOLE map, not just its
        tail. Capping it at 80 while the block is withheld at 60 shipped two
        numbers for one file, and the 61-to-80 band passed the only check that
        can block a commit and went dark anyway. The paired test
        above asserts `CLAUDE.md` is still 80: together they pin two caps for
        two FILES, which is the design, and fail on any attempt to collapse
        them to one number in either direction.
        """
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "some-repo")
            with open(os.path.join(repo, "TOOLING.md"), "w") as handle:
                handle.write(CORE_FIXTURE)
            report = self._report(repo)
            self._stage_text(repo, "TOOLING.md", CORE_FIXTURE)
            staged = self._staged(repo)
        self.assertRegex(
            report.stdout,
            r"TOOLING\.md\s+90\s+%d\s+OVER by %d"
            % (digest_lib.DEFAULT_MAX_LINES, 90 - digest_lib.DEFAULT_MAX_LINES))
        # Explicit, because the regex above would still pass if the two caps
        # were unified ONTO the digest number by accident rather than by the
        # rule: the map must not be reported at the CLAUDE.md Core cap.
        self.assertNotRegex(
            report.stdout,
            r"TOOLING\.md\s+90\s+%d\s" % lib.DEFAULT_CORE_MAX_LINES)
        self.assertNotEqual(
            digest_lib.DEFAULT_MAX_LINES,
            lib.DEFAULT_CORE_MAX_LINES,
            "the two shipped caps have converged, so this test can no longer "
            "tell which one the map resolved through — re-derive it")
        self.assertEqual(staged.returncode, 1, staged.stdout + staged.stderr)
        # And TOOLING.md must NOT have been dragged into the whole-file table,
        # where the CLAUDE.md whole-file cap does not apply to it.
        self.assertNotRegex(
            report.stdout,
            r"TOOLING\.md\s+94\s+%d" % lib.DEFAULT_MAX_LINES)

    def test_the_TEMPLATE_resolves_through_the_DIGEST_cap_not_core_max(self):
        """A template is capped by what it GERMINATES INTO.

        `DEFAULT_CORE_FILES` Core-checks the template, but until 2026-09-15
        only `TOOLING.md` itself resolved to the digest budget — so a template
        Core in the 61-to-80 band passed lint at `core_max_lines` and /onboard
        copied it into a real map already over the 60-line digest budget. That
        cost a new user a trimmed tail; since the digest began
        withholding the whole block it costs them their ENTIRE map, on a
        machine they cannot yet debug.

        Mirrors the map's own test above deliberately, including the
        convergence guard: without it, unifying the two shipped caps would
        make this pass while proving nothing.
        """
        with tempfile.TemporaryDirectory() as root:
            repo = self._repo(root, "some-repo")
            with open(os.path.join(repo, "TOOLING.md.template"), "w") as handle:
                handle.write(CORE_FIXTURE)
            report = self._report(repo)
        self.assertRegex(
            report.stdout,
            r"TOOLING\.md\.template\s+90\s+%d\s+OVER by %d"
            % (digest_lib.DEFAULT_MAX_LINES, 90 - digest_lib.DEFAULT_MAX_LINES))
        # The failing-before assertion: on unfixed code this row read
        # "TOOLING.md.template  90  80  OVER by 10".
        self.assertNotRegex(
            report.stdout,
            r"TOOLING\.md\.template\s+90\s+%d\s" % lib.DEFAULT_CORE_MAX_LINES)
        self.assertNotEqual(
            digest_lib.DEFAULT_MAX_LINES,
            lib.DEFAULT_CORE_MAX_LINES,
            "the two shipped caps have converged, so this test can no longer "
            "tell which one the template resolved through — re-derive it")


if __name__ == "__main__":
    unittest.main(verbosity=0)
