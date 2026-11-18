#!/usr/bin/env python3
"""Tests for the model dispatch guard.

Two halves, deliberately: `TestDecide` asserts the pure verdict including WHICH
route allowed it, and `TestHookExecutable` drives the real hook through
`subprocess`. This repo has shipped 93 green unit tests with zero lines of any
hook exercised, so the second half is not optional.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)

import guard_lib as guard  # noqa: E402

HOOK = os.path.join(TOOL, "claude-model-guard")


class TestDecide(unittest.TestCase):
    """The pure verdict. `path` is asserted, never inferred from `allowed`."""

    def test_an_explicit_model_allows(self):
        v = guard.decide("Agent", {"subagent_type": "Explore",
                                   "model": "sonnet"}, None)
        self.assertIs(v.allowed, True)
        # The ROUTE matters: a definition-pinned allow here would mean the
        # explicit branch never ran and the two mechanisms are hiding each
        # other. That is the exact defect the module docstring names.
        self.assertEqual(v.path, guard.ALLOW_EXPLICIT)

    def test_a_pinned_definition_allows_without_an_explicit_model(self):
        v = guard.decide("Agent", {"subagent_type": "executor"}, "sonnet")
        self.assertIs(v.allowed, True)
        self.assertEqual(v.path, guard.ALLOW_PINNED)

    def test_no_model_anywhere_denies(self):
        v = guard.decide("Agent", {"subagent_type": "general-purpose"}, None)
        self.assertIs(v.allowed, False)
        self.assertEqual(v.path, guard.DENY_INHERITS)
        self.assertIn("general-purpose", v.reason)

    def test_inherit_is_not_a_pin(self):
        """`model: inherit` means "use the session model" — the waste case.

        Plugin agent definitions commonly declare it. If this reads as a pin
        the guard is silently off for all of them.
        """
        v = guard.decide("Agent", {"subagent_type": "patch-verifier"},
                         "inherit")
        self.assertIs(v.allowed, False)
        self.assertEqual(v.path, guard.DENY_INHERITS)

    def test_inherit_is_not_a_pin_at_the_call_site_either(self):
        v = guard.decide("Agent", {"subagent_type": "x", "model": "inherit"},
                         None)
        self.assertIs(v.allowed, False)

    def test_inherit_is_matched_case_and_space_insensitively(self):
        for value in ("Inherit", "  INHERIT  ", "inherits"):
            with self.subTest(value=value):
                v = guard.decide("Agent", {"subagent_type": "x"}, value)
                self.assertIs(v.allowed, False)

    def test_a_blank_or_non_string_model_is_not_a_pin(self):
        for value in ("", "   ", None, True, 5, ["sonnet"]):
            with self.subTest(value=value):
                v = guard.decide("Agent", {"subagent_type": "x",
                                           "model": value}, None)
                self.assertIs(v.allowed, False)

    def test_workflow_is_never_judged(self):
        """A Workflow's per-agent models are inside a JS string.

        Denying one would be a denial on a guess, which is how a guard earns a
        permanent override.
        """
        v = guard.decide("Workflow", {"script": "..."}, None)
        self.assertIs(v.allowed, True)
        self.assertEqual(v.path, guard.ALLOW_NOT_GUARDED)

    def test_an_unrelated_tool_is_never_judged(self):
        v = guard.decide("Bash", {"command": "ls"}, None)
        self.assertIs(v.allowed, True)
        self.assertEqual(v.path, guard.ALLOW_NOT_GUARDED)

    def test_an_unreadable_tool_input_allows(self):
        v = guard.decide("Agent", "not a dict", None)
        self.assertIs(v.allowed, True)

    def test_the_reason_survives_a_missing_subagent_type(self):
        v = guard.decide("Agent", {}, None)
        self.assertIs(v.allowed, False)
        self.assertIn("this agent", v.reason)

    def test_the_reason_names_the_guard(self):
        """Like claude-danger-guard's refusals, so the user can look it up
        (row 76d)."""
        v = guard.decide("Agent", {"subagent_type": "Plan"}, None)
        self.assertTrue(v.reason.startswith("BLOCKED by claude-model-guard: "),
                        v.reason)

    def test_the_reason_names_the_override_and_the_inherit_trap(self):
        """The denial has to be actionable without reading this repo."""
        v = guard.decide("Agent", {"subagent_type": "Plan"}, None)
        self.assertIn("MODEL_GUARD_ALLOW=1", v.reason)
        self.assertIn("inherit", v.reason)
        # It must NOT promise a per-call override. A PreToolUse hook reads the
        # environment it inherited from the Claude Code process, so this is a
        # session-wide, launch-time switch. This text is what reaches Claude on
        # every denial, so an instruction it cannot follow is worse than none.
        self.assertIn("re-run the session", v.reason)
        self.assertNotIn("for one call", v.reason)


class TestFrontmatter(unittest.TestCase):
    def test_reads_a_pinned_model(self):
        self.assertEqual(guard.model_from_frontmatter(
            "---\nname: x\nmodel: sonnet\n---\nbody"), "sonnet")

    def test_inherit_reads_as_no_pin(self):
        self.assertIsNone(guard.model_from_frontmatter(
            "---\nname: x\nmodel: inherit\n---\nbody"))

    def test_absent_model_key(self):
        self.assertIsNone(guard.model_from_frontmatter(
            "---\nname: x\n---\nbody"))

    def test_absent_frontmatter(self):
        self.assertIsNone(guard.model_from_frontmatter("no frontmatter here"))

    def test_quotes_and_trailing_comments_are_stripped(self):
        self.assertEqual(guard.model_from_frontmatter(
            '---\nmodel: "opus"  # why\n---\n'), "opus")

    def test_a_hash_inside_a_value_is_not_a_comment(self):
        """YAML only starts a comment at whitespace-then-#."""
        self.assertEqual(guard.model_from_frontmatter(
            "---\nmodel: claude-x#1\n---\n"), "claude-x#1")

    def test_a_model_line_outside_frontmatter_is_ignored(self):
        """Prose in the body must not be read as configuration."""
        self.assertIsNone(guard.model_from_frontmatter(
            "---\nname: x\n---\nmodel: opus\n"))

    def test_non_string_input(self):
        self.assertIsNone(guard.model_from_frontmatter(None))


class TestReadPinnedModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _write(self, directory, name, body):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(body))
        return path

    def test_finds_a_definition(self):
        d = os.path.join(self.tmp, "agents")
        self._write(d, "executor.md", "---\nmodel: sonnet\n---\n")
        self.assertEqual(guard.read_pinned_model("executor", [d]), "sonnet")

    def test_missing_definition_is_no_pin(self):
        self.assertIsNone(guard.read_pinned_model(
            "general-purpose", [os.path.join(self.tmp, "agents")]))

    def test_a_plugin_namespaced_type_resolves_on_the_bare_name(self):
        d = os.path.join(self.tmp, "agents")
        self._write(d, "agent-creator.md", "---\nmodel: sonnet\n---\n")
        self.assertEqual(
            guard.read_pinned_model("plugin-dev:agent-creator", [d]), "sonnet")

    def test_the_first_directory_wins_even_when_it_pins_nothing(self):
        """Precedence, and the reason this is not a "keep looking" loop.

        The harness resolves the FIRST matching definition and uses whatever it
        says. If a project-scope definition declares `inherit`, a user-scope
        file of the same name must NOT supply a pin the dispatch will never
        receive — that would allow a call that really does inherit.
        """
        first = os.path.join(self.tmp, "a")
        second = os.path.join(self.tmp, "b")
        self._write(first, "dup.md", "---\nmodel: inherit\n---\n")
        self._write(second, "dup.md", "---\nmodel: opus\n---\n")
        self.assertIsNone(guard.read_pinned_model("dup", [first, second]))

    def test_a_later_directory_is_reached_when_the_first_has_no_file(self):
        first = os.path.join(self.tmp, "a")
        second = os.path.join(self.tmp, "b")
        os.makedirs(first, exist_ok=True)
        self._write(second, "only.md", "---\nmodel: opus\n---\n")
        self.assertEqual(guard.read_pinned_model("only", [first, second]),
                         "opus")

    def test_a_traversing_subagent_type_reads_nothing(self):
        d = os.path.join(self.tmp, "agents")
        self._write(d, "executor.md", "---\nmodel: sonnet\n---\n")
        for hostile in ("../agents/executor", "..", ".", "a/b", "a\\b", ""):
            with self.subTest(hostile=hostile):
                self.assertIsNone(guard.read_pinned_model(hostile, [d]))

    def test_an_empty_name_does_not_read_a_bare_dot_md_file(self):
        """`"plugin:"` splits to an empty name, and `"" + ".md"` is `.md`.

        A stray hidden `.md` file in an agents directory would then be read as
        that dispatch's definition and could supply a pin. This is the ONLY
        observable the `if not name` guard has — the exact-match check subsumes
        everything else it used to do — so without this test its mutation
        survives and the guard is unreachable code inside a guard, which this
        repo rates worse than a constant.
        """
        d = os.path.join(self.tmp, "agents")
        self._write(d, ".md", "---\nmodel: sonnet\n---\n")
        self.assertIsNone(guard.read_pinned_model("plugin:", [d]))
        # Control: the same directory DOES resolve a properly named agent, so
        # the assertion above cannot pass merely because nothing is readable.
        self._write(d, "real.md", "---\nmodel: opus\n---\n")
        self.assertEqual(guard.read_pinned_model("real", [d]), "opus")

    def test_a_non_string_type_reads_nothing(self):
        self.assertIsNone(guard.read_pinned_model(None, [self.tmp]))

    def test_an_unreadable_directory_does_not_raise(self):
        self.assertIsNone(guard.read_pinned_model("x", ["/nonexistent/nope"]))


class TestCaseSensitivity(unittest.TestCase):
    """A case-folding filesystem must not resolve one agent onto another.

    Asserted at `read_pinned_model` with an explicit directory list, SEPARATELY
    from the namespace routing below. Both fixes deny the same live dispatch
    (`Explore`), and this repo's own rule is that two mechanisms with one
    observable outcome hide each other — so each gets an assertion that the
    other cannot satisfy.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "agents")
        os.makedirs(self.dir)
        with open(os.path.join(self.dir, "explore.md"), "w") as fh:
            fh.write("---\nname: explore\nmodel: sonnet\n---\n")

    def test_a_differently_cased_name_does_not_match(self):
        """The live defect: built-in `Explore` read claude-security's
        `explore.md` and was ALLOWED on its `model: sonnet`."""
        self.assertIsNone(guard.read_pinned_model("Explore", [self.dir]))

    def test_the_exactly_cased_name_still_matches(self):
        """The positive control. Without it, a lookup that resolves NOTHING
        would pass the assertion above for free."""
        self.assertEqual(guard.read_pinned_model("explore", [self.dir]),
                         "sonnet")

    def test_case_folding_is_refused_on_a_case_insensitive_volume(self):
        """Pin the property against the filesystem actually in use.

        On APFS `os.path.exists(".../Explore.md")` is True, which is what made
        the bug reachable here; on a case-sensitive volume it is False and the
        guard was already correct. Either way the verdict must be the same, so
        this asserts the verdict rather than the filesystem — and skips
        nothing, because there is nothing environment-dependent left to skip.
        """
        folds = os.path.exists(os.path.join(self.dir, "Explore.md"))
        # Recorded, not asserted: this is ambient state. The verdict below is
        # what must hold on both kinds of volume.
        self.assertIn(folds, (True, False))
        self.assertIsNone(guard.read_pinned_model("Explore", [self.dir]))


class TestSearchPath(unittest.TestCase):
    """Which directories a `subagent_type` may be resolved against."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.plugins = os.path.join(
            self.home, ".claude", "plugins", "marketplaces", "mp", "plugins")
        for plugin in ("alpha", "beta"):
            os.makedirs(os.path.join(self.plugins, plugin, "agents"))

    def test_a_bare_type_never_reaches_plugin_directories(self):
        """A built-in or user agent is bare. Reaching into plugins for one is
        how a built-in picks up an unrelated plugin agent's model."""
        path = guard.search_path("Explore", "/proj", self.home)
        self.assertEqual(path, ["/proj/.claude/agents",
                                self.home + "/.claude/agents"])
        for entry in path:
            self.assertNotIn("plugins", entry)

    def test_a_namespaced_type_is_scoped_to_its_own_plugin(self):
        path = guard.search_path("alpha:helper", "/proj", self.home)
        self.assertEqual(
            path, [os.path.join(self.plugins, "alpha", "agents")])

    def test_a_namespaced_type_does_not_see_another_plugin(self):
        """`alpha:helper` must not resolve against beta's `helper.md`."""
        with open(os.path.join(self.plugins, "beta", "agents", "helper.md"),
                  "w") as fh:
            fh.write("---\nmodel: sonnet\n---\n")
        path = guard.search_path("alpha:helper", None, self.home)
        self.assertIsNone(guard.read_pinned_model("alpha:helper", path))

    def test_an_unknown_plugin_resolves_to_nothing(self):
        self.assertEqual(guard.search_path("nope:helper", None, self.home), [])

    def test_plugin_dirs_unscoped_still_returns_all(self):
        """`plugin=None` is the enumeration path, kept for tests."""
        self.assertEqual(len(guard.plugin_agent_dirs(self.home)), 2)


class TestDirectorySourcePlugin(unittest.TestCase):
    """`toolkit-build` ships from a DIRECTORY marketplace: its agents live in the checkout,
    not under `marketplaces/*/plugins/`, so only `plugin_paths` finds them."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.home = os.path.join(self.tmp, "home")
        self.checkout = os.path.join(self.tmp, "checkout")
        os.makedirs(os.path.join(self.home, ".claude", "plugins"))
        os.makedirs(os.path.join(self.checkout, "plugin-build", "agents"))
        os.makedirs(os.path.join(self.checkout, ".claude-plugin"))
        with open(os.path.join(self.checkout, "plugin-build", "agents",
                               "executor.md"), "w") as fh:
            fh.write("---\nmodel: sonnet\n---\n")
        with open(os.path.join(self.checkout, ".claude-plugin",
                               "marketplace.json"), "w") as fh:
            json.dump({"name": "claude-code-toolkit", "plugins": [
                {"name": "toolkit-build", "source": "./plugin-build"}]}, fh)
        with open(os.path.join(self.home, ".claude", "plugins",
                               "known_marketplaces.json"), "w") as fh:
            json.dump({"claude-code-toolkit": {
                "source": {"source": "directory", "path": self.checkout},
                "installLocation": self.checkout}}, fh)

    def test_a_directory_source_plugin_resolves_its_agents(self):
        self.assertEqual(
            guard.search_path("toolkit-build:executor", None, self.home),
            [os.path.join(self.checkout, "plugin-build", "agents")])

    def test_the_pin_is_read_from_the_checkout(self):
        path = guard.search_path("toolkit-build:executor", None, self.home)
        self.assertEqual(guard.read_pinned_model("toolkit-build:executor", path),
                         "sonnet")

    def test_without_an_agents_dir_the_plugin_resolves_nothing(self):
        shutil.rmtree(os.path.join(self.checkout, "plugin-build", "agents"))
        self.assertEqual(
            guard.search_path("toolkit-build:executor", None, self.home), [])

    def test_a_different_plugin_never_resolves_to_that_dir(self):
        self.assertEqual(
            guard.search_path("other-plugin:executor", None, self.home), [])


class TestPinnedModelSeam(unittest.TestCase):
    """The hook's `_pinned_model`, which had no test and held both defects.

    Driven through the real executable rather than imported, because the seam
    IS the wiring: it is where the search path is chosen and handed to the pure
    function, and that choice is invisible to any test of the pure function.
    """

    def test_a_user_definition_pinning_nothing_is_not_rescued_by_a_plugin(self):
        """The two-call defect, end to end.

        `read_pinned_model` returns None both for "no file" and for "file found,
        pins nothing", so a second call over plugin dirs could resurrect a pin
        the harness would never have consulted. One ordered search fixes it —
        and a bare name no longer reaches plugins at all, which is the second,
        independent reason this denies.
        """
        home = tempfile.mkdtemp()
        user = os.path.join(home, ".claude", "agents")
        os.makedirs(user)
        with open(os.path.join(user, "researcher.md"), "w") as fh:
            fh.write("---\nmodel: inherit\n---\n")
        plugin = os.path.join(home, ".claude", "plugins", "marketplaces",
                              "mp", "plugins", "p", "agents")
        os.makedirs(plugin)
        with open(os.path.join(plugin, "researcher.md"), "w") as fh:
            fh.write("---\nmodel: sonnet\n---\n")

        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "researcher"}},
                        home=home)
        self.assertEqual(code, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"],
                         "deny")

    def test_the_control_a_real_user_pin_still_allows(self):
        """Positive control for the test above: without it, a seam that
        resolved NOTHING would satisfy the denial for free."""
        home = tempfile.mkdtemp()
        user = os.path.join(home, ".claude", "agents")
        os.makedirs(user)
        with open(os.path.join(user, "researcher.md"), "w") as fh:
            fh.write("---\nmodel: sonnet\n---\n")
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "researcher"}},
                        home=home)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_a_case_folded_collision_is_denied_through_the_hook(self):
        """The `Explore` defect, through the executable and against a $HOME
        laid out the way the real machine is."""
        home = tempfile.mkdtemp()
        plugin = os.path.join(home, ".claude", "plugins", "marketplaces",
                              "official", "plugins", "claude-security",
                              "agents")
        os.makedirs(plugin)
        with open(os.path.join(plugin, "explore.md"), "w") as fh:
            fh.write("---\nname: explore\nmodel: sonnet\n---\n")
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "Explore"}},
                        home=home)
        self.assertEqual(code, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"],
                         "deny")


class TestAgentDirs(unittest.TestCase):
    def test_project_scope_precedes_user_scope(self):
        dirs = guard.agent_dirs("/proj", "/home/u")
        self.assertEqual(dirs, ["/proj/.claude/agents",
                                "/home/u/.claude/agents"])

    def test_a_missing_cwd_still_yields_user_scope(self):
        self.assertEqual(guard.agent_dirs(None, "/home/u"),
                         ["/home/u/.claude/agents"])

    def test_plugin_dirs_are_empty_when_there_are_none(self):
        self.assertEqual(guard.plugin_agent_dirs(tempfile.mkdtemp()), [])

    def test_plugin_dirs_are_found(self):
        home = tempfile.mkdtemp()
        d = os.path.join(home, ".claude", "plugins", "marketplaces",
                         "mp", "plugins", "p", "agents")
        os.makedirs(d)
        self.assertEqual(guard.plugin_agent_dirs(home), [d])


def run(payload, env=None, home=None):
    """Drive the real hook. Returns (returncode, parsed stdout or None).

    The environment is built explicitly rather than inherited. This repo has
    twice shipped suites that asserted a different contract per machine because
    a subprocess inherited something ambient — an env var once, the power source
    once. MODEL_GUARD_ALLOW and CLAUDE_TOOLKIT_CONFIG are exactly the kind of
    thing a developer may have set in their own shell.
    """
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": home or tempfile.mkdtemp(),
        # No config file: the fresh-clone case, and the one the repo requires
        # every suite to pass under.
        "CLAUDE_TOOLKIT_CONFIG": "/nonexistent.json",
    }
    environment.update(env or {})
    done = subprocess.run([sys.executable, HOOK],
                          input=json.dumps(payload), text=True,
                          capture_output=True, timeout=30,
                          env=environment)
    out = done.stdout.strip()
    return done.returncode, (json.loads(out) if out else None)


class TestHookExecutable(unittest.TestCase):
    def test_a_dispatch_with_no_model_is_denied(self):
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "general-purpose"}})
        self.assertEqual(code, 0)
        specific = out["hookSpecificOutput"]
        self.assertEqual(specific["permissionDecision"], "deny")
        self.assertIn("general-purpose", specific["permissionDecisionReason"])

    def test_a_dispatch_naming_a_model_emits_nothing(self):
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "general-purpose",
                                        "model": "sonnet"}})
        self.assertEqual(code, 0)
        # Nothing at all, not an empty hookSpecificOutput — printing one on
        # every allowed dispatch trains a reader to skip this hook's output.
        self.assertIsNone(out)

    def test_a_pinned_definition_emits_nothing(self):
        home = tempfile.mkdtemp()
        d = os.path.join(home, ".claude", "agents")
        os.makedirs(d)
        with open(os.path.join(d, "executor.md"), "w") as fh:
            fh.write("---\nname: executor\nmodel: sonnet\n---\nbody\n")
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "executor"}},
                        home=home)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_a_definition_declaring_inherit_is_denied(self):
        """The end-to-end version of the trap, through the real hook."""
        home = tempfile.mkdtemp()
        d = os.path.join(home, ".claude", "agents")
        os.makedirs(d)
        with open(os.path.join(d, "hollow.md"), "w") as fh:
            fh.write("---\nname: hollow\nmodel: inherit\n---\nbody\n")
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "hollow"}},
                        home=home)
        self.assertEqual(code, 0)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"],
                         "deny")

    def test_a_plugin_definition_is_resolved(self):
        home = tempfile.mkdtemp()
        d = os.path.join(home, ".claude", "plugins", "marketplaces",
                         "mp", "plugins", "p", "agents")
        os.makedirs(d)
        with open(os.path.join(d, "helper.md"), "w") as fh:
            fh.write("---\nmodel: sonnet\n---\nbody\n")
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "p:helper"}},
                        home=home)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_workflow_is_not_denied(self):
        code, out = run({"tool_name": "Workflow",
                         "tool_input": {"script": "export const meta = {}"}})
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_the_override_allows(self):
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "general-purpose"}},
                        env={"MODEL_GUARD_ALLOW": "1"})
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_the_override_is_exact(self):
        """Anything other than "1" is not the override."""
        for value in ("0", "true", "yes", ""):
            with self.subTest(value=value):
                code, out = run(
                    {"tool_name": "Agent",
                     "tool_input": {"subagent_type": "general-purpose"}},
                    env={"MODEL_GUARD_ALLOW": value})
                self.assertIsNotNone(out)

    def test_config_can_disable_the_guard(self):
        home = tempfile.mkdtemp()
        cfg = os.path.join(home, "config.json")
        with open(cfg, "w") as fh:
            json.dump({"model_guard": {"enabled": False}}, fh)
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "general-purpose"}},
                        env={"CLAUDE_TOOLKIT_CONFIG": cfg}, home=home)
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_an_unparseable_config_leaves_the_guard_ON(self):
        """A config that will not parse must not be a way to disarm a guard.

        NAMED FOR THE BRANCH IT ACTUALLY REACHES. `toolkit_config.load` catches
        every error — missing, malformed, unreadable — and returns {}, so
        `get()` cannot raise and this exercises the `value is None` path, NOT
        the hook's `except`. An earlier name claimed the exception branch and a
        mutation of that branch survived because of it: the body never went
        near it. The repo rule is that a test's name is part of its contract.
        """
        home = tempfile.mkdtemp()
        cfg = os.path.join(home, "config.json")
        with open(cfg, "w") as fh:
            fh.write("{not json")
        code, out = run({"tool_name": "Agent",
                         "tool_input": {"subagent_type": "general-purpose"}},
                        env={"CLAUDE_TOOLKIT_CONFIG": cfg}, home=home)
        self.assertEqual(code, 0)
        self.assertIsNotNone(out)

    def test_an_unimportable_config_module_leaves_the_guard_ON(self):
        """The hook's `except` branch, exercised for real.

        Reachable, not theoretical: `~/.local/bin/claude-model-guard` points
        into the repo, so a partial or half-deleted checkout gives a tool
        directory with no sibling `lib/` and `import toolkit_config` raises.
        A guard must not quietly switch itself off because the config module
        went missing — that is indistinguishable from a guard with nothing to
        say. Copying the tool WITHOUT a lib/ sibling is what reaches it; no
        amount of config content can, per the test above.
        """
        sandbox = tempfile.mkdtemp()
        tool = os.path.join(sandbox, "tools", "model-guard")
        os.makedirs(tool)
        for name in ("claude-model-guard", "guard_lib.py"):
            with open(os.path.join(TOOL, name)) as src, \
                    open(os.path.join(tool, name), "w") as dst:
                dst.write(src.read())
        os.chmod(os.path.join(tool, "claude-model-guard"), 0o755)
        self.assertFalse(os.path.exists(os.path.join(sandbox, "lib")),
                         "the whole point is that lib/ is absent")

        done = subprocess.run(
            [sys.executable, os.path.join(tool, "claude-model-guard")],
            input=json.dumps({"tool_name": "Agent",
                              "tool_input": {"subagent_type": "gp"}}),
            text=True, capture_output=True, timeout=30,
            env={"PATH": os.environ.get("PATH", ""),
                 "HOME": tempfile.mkdtemp(),
                 "CLAUDE_TOOLKIT_CONFIG": "/nonexistent.json"})
        self.assertEqual(done.returncode, 0)
        self.assertEqual(
            json.loads(done.stdout)["hookSpecificOutput"]["permissionDecision"],
            "deny")

    def test_malformed_stdin_exits_zero_and_says_nothing(self):
        done = subprocess.run(
            [sys.executable, HOOK], input="{not json", text=True,
            capture_output=True, timeout=30,
            env={"PATH": os.environ.get("PATH", ""),
                 "HOME": tempfile.mkdtemp(),
                 "CLAUDE_TOOLKIT_CONFIG": "/nonexistent.json"})
        self.assertEqual(done.returncode, 0)
        self.assertEqual(done.stdout.strip(), "")

    def test_a_non_dict_payload_exits_zero(self):
        code, out = run(["not", "a", "dict"])
        self.assertEqual(code, 0)
        self.assertIsNone(out)

    def test_an_empty_payload_exits_zero(self):
        code, out = run({})
        self.assertEqual(code, 0)
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
