#!/usr/bin/env python3
"""The plugin ships the toolkit's skills and agents, so the layout it ships
them through has to hold: the two symlinks, no leftover `commands/`, and
frontmatter that names each skill after its directory."""

import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import claude_imports  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
SKILLS = os.path.join(REPO, "skills")
WRAP_UP_DESCRIPTION_LIMIT = 1536


def frontmatter_lines(path):
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().split("\n")
    if lines[0] != "---":
        raise AssertionError("{} has no frontmatter".format(path))
    end = lines.index("---", 1)
    return lines[1:end]


def frontmatter_keys(path):
    """Top-level keys (column 0) mapped to the inline text after the colon."""
    keys = {}
    for line in frontmatter_lines(path):
        if line and not line[0].isspace() and ":" in line:
            key, _, value = line.partition(":")
            keys[key] = value.strip()
    return keys


def folded_scalar(path, key):
    """The value of a `key: >-` folded scalar, continuation lines joined with
    single spaces."""
    lines = frontmatter_lines(path)
    start = lines.index("{}: >-".format(key))
    parts = []
    for line in lines[start + 1:]:
        if line and not line[0].isspace():
            break
        parts.append(line.strip())
    return " ".join(part for part in parts if part)


def skill_dirs(root=SKILLS):
    """(group, name) for every `<group>/<name>/SKILL.md` under `root`."""
    found = []
    for group in sorted(os.listdir(root)):
        group_dir = os.path.join(root, group)
        if not os.path.isdir(group_dir):
            continue
        for name in sorted(os.listdir(group_dir)):
            if os.path.isfile(os.path.join(group_dir, name, "SKILL.md")):
                found.append((group, name))
    return found


def skill_names():
    return sorted(name for _, name in skill_dirs())


def skill_path(name):
    """The SKILL.md of the skill called `name`, wherever its group is."""
    for group, found in skill_dirs():
        if found == name:
            return os.path.join(SKILLS, group, name, "SKILL.md")
    raise AssertionError("no skill named " + name)


def ungrouped_skills(root):
    """`SKILL.md` files under `root` that do not sit at <group>/<name>/SKILL.md."""
    bad = []
    for current, _dirs, files in os.walk(root):
        if "SKILL.md" in files:
            depth = os.path.relpath(current, root).count(os.sep) + 1
            if os.path.relpath(current, root) == "." or depth != 2:
                bad.append(os.path.relpath(os.path.join(current, "SKILL.md"), root))
    return sorted(bad)


def registry():
    with open(os.path.join(REPO, "claude", "profiles.json"), encoding="utf-8") as handle:
        return {k: v for k, v in json.load(handle)["profiles"].items() if k != "//"}


class TestEverySkillInOneProfileGroup(unittest.TestCase):
    def test_every_skill_md_sits_at_group_name(self):
        self.assertEqual(ungrouped_skills(SKILLS), [])

    def test_every_group_is_a_registry_profile(self):
        for group, name in skill_dirs():
            self.assertIn(group, registry(), "skills/%s/%s" % (group, name))

    def test_no_skill_sits_one_level_deep(self):
        for entry in os.listdir(SKILLS):
            self.assertFalse(os.path.exists(os.path.join(SKILLS, entry, "SKILL.md")), entry)

    def test_every_registry_profile_has_a_group_with_a_skill(self):
        groups = {group for group, _ in skill_dirs()}
        for profile in registry():
            self.assertIn(profile, groups)

    def test_positive_control_the_walker_reports_an_ungrouped_skill(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "x"))
            open(os.path.join(root, "x", "SKILL.md"), "w").close()
            os.makedirs(os.path.join(root, "g", "ok"))
            open(os.path.join(root, "g", "ok", "SKILL.md"), "w").close()
            self.assertEqual(ungrouped_skills(root), ["x/SKILL.md"])


class TestPluginDirsShipTheirGroup(unittest.TestCase):
    def test_every_profile_dir_links_its_group_tools_lib_and_agents(self):
        for profile, entry in registry().items():
            base = os.path.join(REPO, entry["dir"])
            for link, target in (("skills", "../skills/" + profile),
                                 ("tools", "../tools"), ("lib", "../lib")):
                path = os.path.join(base, link)
                with self.subTest(profile=profile, link=link):
                    self.assertTrue(os.path.islink(path), path)
                    self.assertEqual(os.readlink(path), target)
                    if link != "skills":  # its group: test_every_skills_link_resolves
                        self.assertTrue(os.path.isdir(path), path)
            agents = os.path.join(base, "agents")
            with self.subTest(profile=profile, link="agents"):
                self.assertEqual(os.path.lexists(agents), entry["agents"] is True)
                if entry["agents"]:
                    self.assertEqual(os.readlink(agents), "../agents")
            with self.subTest(profile=profile, link="hooks"):
                with open(os.path.join(base, "hooks", "hooks.json"), encoding="utf-8") as handle:
                    json.load(handle)

    def test_every_skills_link_resolves(self):
        for profile, entry in registry().items():
            path = os.path.join(REPO, entry["dir"], "skills")
            with self.subTest(profile=profile):
                self.assertTrue(os.path.isdir(path), path)


def skill_plugin_names():
    with open(os.path.join(REPO, ".claude-plugin", "marketplace.json"),
              encoding="utf-8") as handle:
        entries = json.load(handle)["plugins"]
    return {e["name"] for e in entries if e["source"].startswith("./plugins/")}


class TestDependenciesMatchRequires(unittest.TestCase):
    def test_plugin_json_dependencies_equal_the_required_profiles_plugins(self):
        reg = registry()
        for profile, entry in reg.items():
            with open(os.path.join(REPO, entry["dir"], ".claude-plugin", "plugin.json"),
                      encoding="utf-8") as handle:
                manifest = json.load(handle)
            if profile == "core":
                self.assertNotIn("dependencies", manifest)
                continue
            with self.subTest(profile=profile):
                profile_plugins = {r["plugin"] for r in reg.values()}
                deps = set(manifest["dependencies"])
                self.assertEqual(deps & profile_plugins,
                                 {reg[r]["plugin"] for r in entry["requires"]})
                for extra in sorted(deps - profile_plugins):
                    self.assertIn(extra, skill_plugin_names(),
                                  "%s depends on %s, which is no ./plugins/ marketplace entry"
                                  % (profile, extra))

    def test_maintainer_depends_on_prompt_optimizer(self):
        with open(os.path.join(REPO, "plugin-maintainer", ".claude-plugin", "plugin.json"),
                  encoding="utf-8") as handle:
            deps = json.load(handle)["dependencies"]
        self.assertEqual(deps, ["toolkit-core", "toolkit-build", "skill-prompt-optimizer"])


class TestSkillBuilderEvalsPointsAtRealFile(unittest.TestCase):
    def test_trigger_cases_path_resolves_from_evals_md(self):
        evals = os.path.join(SKILLS, "maintainer", "skill-builder",
                             "references", "evals.md")
        with open(evals, encoding="utf-8") as handle:
            text = handle.read()
        # The path is written as a relative one in backticks; read it from the line.
        match = re.search(r"`((?:\.\./)+tools/build-route/trigger-cases\.json)`", text)
        self.assertIsNotNone(match, "evals.md names no relative trigger-cases.json path")
        resolved = os.path.normpath(os.path.join(os.path.dirname(evals), match.group(1)))
        self.assertTrue(os.path.isfile(resolved), resolved)


class TestNoCommandsDirectory(unittest.TestCase):
    def test_commands_is_gone(self):
        self.assertFalse(os.path.exists(os.path.join(REPO, "commands")))


class TestSkillFrontmatter(unittest.TestCase):
    def test_positive_control_the_sweep_sees_build(self):
        self.assertIn("build", skill_names())

    def test_name_equals_directory_name(self):
        for name in skill_names():
            keys = frontmatter_keys(skill_path(name))
            self.assertEqual(keys.get("name"), name, name)

    def test_the_six_former_commands_are_skills(self):
        for name in ("build", "caffeinate", "handover", "onboard",
                     "review-pr-ci", "wrap-up"):
            self.assertIn(name, skill_names())


class TestWrapUpDescription(unittest.TestCase):
    def test_fits_the_listing_cap_and_carries_both_phrase_sets(self):
        description = folded_scalar(
            skill_path("wrap-up"), "description")
        self.assertLessEqual(len(description), WRAP_UP_DESCRIPTION_LIMIT)
        self.assertIn("done for the day", description)
        self.assertIn("wrap up this function", description)


class TestDisableModelInvocation(unittest.TestCase):
    def test_only_autonomy_permissions_is_slash_only(self):
        flagged = set()
        for name in skill_names():
            keys = frontmatter_keys(skill_path(name))
            if keys.get("disable-model-invocation") == "true":
                flagged.add(name)
        self.assertEqual(flagged, {"autonomy-permissions"})


class TestNoBareAgentDispatch(unittest.TestCase):
    """The agents ship in the builder plugin, so every dispatch names them
    `toolkit-build:<name>`. A bare name resolves to nothing outside the repo."""

    STALE_PREFIX = re.compile(
        r"toolkit-core:(executor|task-reviewer|code-reviewer|build|skill-builder)\b")
    BARE_PROSE = re.compile(r"`(executor|task-reviewer|code-reviewer)`")
    BARE_JS = re.compile(r"agentType: '(executor|task-reviewer|code-reviewer)'")
    JS = os.path.join(REPO, "tools", "build-lens", "blinded-review.js")

    def _prose_files(self):
        files = glob.glob(os.path.join(REPO, "skills", "*", "*", "SKILL.md"))
        files += glob.glob(os.path.join(REPO, "agents", "*.md"))
        # build-measurements.md is the corpus, a symlink into docs/: it records
        # what past gates ran, so it is history rather than a dispatch site.
        files += [f for f in glob.glob(os.path.join(REPO, "tools", "build-lens", "*.md"))
                  if not os.path.islink(f)]
        files.append(os.path.join(REPO, "policy", "CLAUDE.md"))
        return sorted(files)

    def _hits(self, path, pattern):
        with open(path, encoding="utf-8") as handle:
            return ["{}:{}".format(os.path.relpath(path, REPO), number)
                    for number, line in enumerate(handle, 1) if pattern.search(line)]

    def test_the_scan_covers_the_files_that_dispatch(self):
        names = {os.path.relpath(f, REPO) for f in self._prose_files()}
        for expected in (os.path.join("skills", "builder", "build", "SKILL.md"),
                         os.path.join("skills", "maintainer", "review-pr-ci", "SKILL.md"),
                         os.path.join("agents", "executor.md"),
                         os.path.join("tools", "build-lens", "code-review.md"),
                         os.path.join("policy", "CLAUDE.md")):
            self.assertIn(expected, names)

    def test_no_prose_site_names_a_bare_agent(self):
        hits = []
        for path in self._prose_files():
            hits += self._hits(path, self.BARE_PROSE)
        self.assertEqual([], hits)

    def test_no_site_names_the_old_plugin_prefix(self):
        hits = []
        for path in self._prose_files() + [self.JS]:
            hits += self._hits(path, self.STALE_PREFIX)
        self.assertEqual([], hits)

    def test_the_review_script_dispatches_by_plugin_name(self):
        self.assertEqual([], self._hits(self.JS, self.BARE_JS))

    def test_positive_control_the_script_names_the_namespaced_reviewer(self):
        self.assertTrue(self._hits(
            self.JS, re.compile(r"agentType: 'toolkit-build:code-reviewer'")))


class TestDispatchedAgentsShipInTheBuilderPlugin(unittest.TestCase):
    """Every `toolkit-build:<agent>` a dispatch site names resolves to a real agent
    file, through the builder plugin's `agents` symlink, with its model pinned."""

    NAMED = re.compile(r"toolkit-build:([a-z][a-z-]*)")
    PINS = {"executor": "sonnet", "task-reviewer": "sonnet", "code-reviewer": "opus",
            "architecture-reviewer": "fable", "spec-reviewer": "opus"}

    def _named(self):
        helper = TestNoBareAgentDispatch()
        found = set()
        for path in helper._prose_files() + [helper.JS]:
            with open(path, encoding="utf-8") as handle:
                found.update(self.NAMED.findall(handle.read()))
        return found

    def _builder(self):
        holders = [e for p, e in registry().items()
                   if os.path.isfile(os.path.join(SKILLS, p, "build", "SKILL.md"))]
        self.assertEqual(1, len(holders))
        return holders[0]

    def test_the_dispatch_sites_name_agents(self):
        self.assertTrue(self._named())

    def test_the_prefix_is_the_builder_plugin(self):
        self.assertEqual("toolkit-build", self._builder()["plugin"])
        with open(TestNoBareAgentDispatch.JS, encoding="utf-8") as handle:
            self.assertIn(self._builder()["plugin"] + ":code-reviewer", handle.read())

    def test_each_named_agent_ships_and_pins_its_model(self):
        agents = os.path.join(REPO, self._builder()["dir"], "agents")
        for agent in sorted(self._named()):
            path = os.path.join(agents, agent + ".md")
            self.assertTrue(os.path.isfile(path), path)
            with open(path, encoding="utf-8") as handle:
                self.assertIn("model: " + self.PINS[agent] + "\n", handle.read(), path)


    EFFORT_PINS = {"code-reviewer": "max", "spec-reviewer": "max"}

    def test_reviewer_agents_pin_effort_max(self):
        """The gate prose promises these reviewers run at max; the pin is the
        only thing that makes it so."""
        agents = os.path.join(REPO, self._builder()["dir"], "agents")
        for agent, effort in sorted(self.EFFORT_PINS.items()):
            path = os.path.join(agents, agent + ".md")
            with open(path, encoding="utf-8") as handle:
                frontmatter = handle.read().split("---\n")[1]
            self.assertIn("effort: " + effort, frontmatter.splitlines(), path)


class TestBuildLensResolvesInsideThePlugin(unittest.TestCase):
    """`/build` reads its lens files through `${CLAUDE_PLUGIN_ROOT}`; a lens file
    is read with Read, where nothing is substituted, so it must not use it."""

    LENS = os.path.join(REPO, "tools", "build-lens")
    ROOT_REF = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)")

    def _skill_references(self):
        found = []
        for name in skill_names():
            path = skill_path(name)
            with open(path, encoding="utf-8") as handle:
                for rel in self.ROOT_REF.findall(handle.read()):
                    found.append((name, rel.rstrip(".,;:)")))
        return found

    def test_no_lens_file_uses_the_plugin_root_variable(self):
        names = sorted(glob.glob(os.path.join(self.LENS, "*.md")))
        self.assertTrue(names)
        for path in names:
            with open(path, encoding="utf-8") as handle:
                self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", handle.read(), path)

    def test_the_corpus_is_a_relative_symlink_into_docs(self):
        link = os.path.join(self.LENS, "build-measurements.md")
        self.assertTrue(os.path.islink(link), link)
        self.assertEqual(os.readlink(link), "../../docs/build-measurements.md")
        self.assertEqual(os.path.realpath(link),
                         os.path.realpath(os.path.join(REPO, "docs", "build-measurements.md")))

    def test_every_plugin_root_reference_in_a_skill_resolves(self):
        for name, rel in self._skill_references():
            with self.subTest(skill=name, rel=rel):
                self.assertTrue(os.path.exists(os.path.join(REPO, "plugin", rel)),
                                "${CLAUDE_PLUGIN_ROOT}/" + rel)

    def test_positive_control_the_build_skill_references_the_lens_files(self):
        refs = [rel for name, rel in self._skill_references() if name == "build"]
        self.assertIn("tools/build-lens/plan-review.md", refs)


class TestSkillsReachToolsThroughPluginVariables(unittest.TestCase):
    """A skill body is read where it sits, so a path relative to it, or one
    through the old `~/.claude/commands` link, reaches nothing."""

    # Prose that names the pattern on purpose: skill-builder tells its
    # reader to run a skills repository's own script from that repo.
    ALLOWED = {("skill-builder", "python3 scripts/")}

    def test_no_skill_uses_a_relative_tool_path_or_the_commands_link(self):
        for name in skill_names():
            with open(skill_path(name), encoding="utf-8") as handle:
                text = handle.read()
            for needle in ("../../tools/", "~/.claude/commands/", "python3 scripts/"):
                if (name, needle) in self.ALLOWED:
                    continue
                with self.subTest(skill=name, needle=needle):
                    self.assertNotIn(needle, text)


class TestNoLiveLegacyInstallPath(unittest.TestCase):
    """Nothing live names the paths install.sh used to link into, or tells
    anyone to hand-link a toolkit skill."""

    HISTORY = {"CLAUDE.history.md", "CLAUDE.md.history.md",
               "tools/build-lens/tests/fixtures/directives-baseline.txt"}
    # The installer and the resolver must name the legacy paths: they remove
    # what was linked there and say so when a personal copy shadows a skill.
    NAMES_LEGACY = {"install.sh", "lib/plugin_paths.py", "docs/decisions.md"}
    PATHS = ("~/.claude/commands", "~/.claude/agents", "~/.claude/build-lens",
             "note_managed_link")
    PHRASES = ("hand-link", "not linked by default")

    def scanned_files(self):
        """Tracked plus untracked-but-not-ignored, so an uncommitted edit is seen."""
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=REPO, capture_output=True, text=True, check=True).stdout
        for rel in sorted(set(out.splitlines())):
            parts = rel.split("/")
            if (rel in self.HISTORY or rel in self.NAMES_LEGACY
                    or "tests" in parts[:-1] or rel.startswith("docs/plans/")):
                continue
            if os.path.isfile(os.path.join(REPO, rel)):
                yield rel

    def hits(self, rels):
        found = []
        for rel in rels:
            with open(os.path.join(REPO, rel), encoding="utf-8", errors="ignore") as handle:
                text = handle.read()
            lowered = text.lower()
            found += ["{}: {}".format(rel, n) for n in self.PATHS if n in text]
            found += ["{}: {}".format(rel, n) for n in self.PHRASES if n in lowered]
        return found

    def test_no_live_file_names_a_legacy_path_or_the_hand_link_flow(self):
        self.assertEqual(self.hits(self.scanned_files()), [])

    def test_scan_reaches_live_build_lens_files(self):
        files = list(self.scanned_files())
        self.assertTrue([f for f in files if f.startswith("tools/build-lens/") and f.endswith(".md")])

    def test_positive_control_the_scan_can_find_a_legacy_path(self):
        self.assertIn("lib/plugin_paths.py: ~/.claude/commands",
                      self.hits(["lib/plugin_paths.py"]))


class TestNoLiveMovedCommandPath(unittest.TestCase):
    """The six commands moved to skills/<n>/SKILL.md; no live file may still
    point at commands/<n>.md. A line citing a commit sha is history."""

    MOVED = ("build", "caffeinate", "handover", "onboard", "review-pr-ci", "wrap-up")
    # Its remaining mentions are dated history.
    DATED_HISTORY = {"docs/build-measurements.md"}
    HISTORY = TestNoLiveLegacyInstallPath.HISTORY
    NAMES_LEGACY = TestNoLiveLegacyInstallPath.NAMES_LEGACY
    scanned_files = TestNoLiveLegacyInstallPath.scanned_files
    PATTERN = re.compile(r"commands/(?:{})\.md".format("|".join(MOVED)))
    SHA = re.compile(r"`[0-9a-f]{7}`")

    def hits(self, rels):
        found = []
        for rel in rels:
            with open(os.path.join(REPO, rel), encoding="utf-8", errors="ignore") as handle:
                for number, line in enumerate(handle, 1):
                    if self.PATTERN.search(line) and not self.SHA.search(line):
                        found.append("{}:{}".format(rel, number))
        return found

    def test_no_live_file_points_at_a_moved_command(self):
        # tools/build-lens/build-measurements.md is a symlink to the same file.
        history = {os.path.realpath(os.path.join(REPO, r)) for r in self.DATED_HISTORY}
        rels = [r for r in self.scanned_files()
                if os.path.realpath(os.path.join(REPO, r)) not in history]
        self.assertEqual(self.hits(rels), [])

    def test_positive_control_a_sha_line_is_the_allowed_history(self):
        # Through hits() itself, on a scratch file: the sha line is exempt,
        # the bare pointer beside it is still caught.
        with tempfile.TemporaryDirectory() as scratch:
            path = os.path.join(scratch, "notes.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("measured against `commands/build.md` at `1a2b3c4`\n"
                             "see `commands/build.md` Phase 1\n")
            self.assertEqual(self.hits([path]), ["{}:2".format(path)])

    def test_positive_control_a_bare_pointer_is_caught(self):
        self.assertTrue(self.PATTERN.search("see `commands/build.md` Phase 1")
                        and not self.SHA.search("see `commands/build.md` Phase 1"))


class TestNoLiveUngroupedSkillPath(unittest.TestCase):
    """Skills live at skills/<group>/<name>/; a live file naming skills/<name>/
    points at nothing. A line citing a commit sha is history."""

    # Dated plans and records.
    # build-measurements.md is NOT history here: /build reads it through
    # tools/build-lens/ and its skill pointers are live.
    DATED_HISTORY = {"CLAUDE.history.md"}
    HISTORY = TestNoLiveLegacyInstallPath.HISTORY
    NAMES_LEGACY = TestNoLiveLegacyInstallPath.NAMES_LEGACY
    scanned_files = TestNoLiveLegacyInstallPath.scanned_files
    SHA = re.compile(r"`[0-9a-f]{7}`")

    @staticmethod
    def build_pattern(names):
        """skills/<name> straight after skills/ (a grouped path has the group
        there), or the literal placeholder skills/<name>/."""
        # Also the one-level glob skills/*/SKILL.md, which finds nothing now.
        return re.compile(r"skills/(?:(?:{})(?![\w-])|<name>/|\*/SKILL\.md)".format(
            "|".join(re.escape(n) for n in sorted(names))))

    @classmethod
    def disk_pattern(cls):
        with open(os.path.join(REPO, "claude", "profiles.json"), encoding="utf-8") as handle:
            groups = list(json.load(handle)["profiles"])
        names = set()
        for group in groups:
            for path in glob.glob(os.path.join(REPO, "skills", group, "*", "SKILL.md")):
                names.add(os.path.basename(os.path.dirname(path)))
        return cls.build_pattern(names)

    def hits(self, rels):
        pattern = self.disk_pattern()
        found = []
        for rel in rels:
            with open(os.path.join(REPO, rel), encoding="utf-8", errors="ignore") as handle:
                for number, line in enumerate(handle, 1):
                    if pattern.search(line) and not self.SHA.search(line):
                        found.append("{}:{}".format(rel, number))
        return found

    def test_no_live_file_names_an_ungrouped_skill_path(self):
        history = {os.path.realpath(os.path.join(REPO, r)) for r in self.DATED_HISTORY}
        rels = [r for r in self.scanned_files()
                if os.path.realpath(os.path.join(REPO, r)) not in history]
        self.assertEqual(self.hits(rels), [])

    def test_pattern_covers_every_skill_on_disk(self):
        # Derived, not counted: every grouped skill dir name is in the pattern.
        on_disk = {os.path.basename(os.path.dirname(p)) for p in
                   glob.glob(os.path.join(REPO, "skills", "*", "*", "SKILL.md"))}
        self.assertTrue(on_disk)
        pattern = self.disk_pattern()
        for name in sorted(on_disk):
            self.assertTrue(pattern.search("skills/%s/SKILL.md" % name), name)

    def test_positive_control_a_bare_skill_path_is_caught(self):
        pattern = self.disk_pattern()
        self.assertTrue(pattern.search("see skills/build/SKILL.md"))
        self.assertTrue(pattern.search("see skills/handover/SKILL.md"))
        self.assertTrue(pattern.search("copy skills/<name>/ here"))
        self.assertTrue(pattern.search("skills/skill-builder"))
        self.assertTrue(pattern.search("list skills/*/SKILL.md here"))
        self.assertFalse(pattern.search("list skills/*/*/SKILL.md here"))

    def test_positive_control_a_grouped_path_is_allowed(self):
        pattern = self.disk_pattern()
        self.assertFalse(pattern.search("see skills/builder/build/SKILL.md"))
        self.assertFalse(pattern.search("skills/<profile>/<name>/"))
        self.assertFalse(pattern.search("skills/<group>/<name>/"))
        self.assertFalse(pattern.search("skills/core/onboard/SKILL.md"))

    def test_positive_control_a_sha_line_is_the_allowed_history(self):
        line = "was `abcdef1` skills/build/SKILL.md"
        self.assertTrue(self.disk_pattern().search(line) and self.SHA.search(line))


class TestOptionalCommandsNameTheirProfile(unittest.TestCase):
    """A default install has no /build or /caffeinate, so
    onboarding must not recommend them as if it does."""

    def test_onboarding_table_names_each_commands_profile(self):
        with open(os.path.join(REPO, "ONBOARDING.md"), encoding="utf-8") as handle:
            text = handle.read()
        rows = {}
        for line in text.splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) == 3 and re.fullmatch(r"`/[a-z-]+`", cells[0]):
                rows[cells[0].strip("`")] = cells[2]
        self.assertEqual(rows.get("/build"), "builder")
        self.assertEqual(rows.get("/caffeinate"), "mac")
        self.assertEqual(rows.get("/handover"), "default")

    def test_onboard_summary_offers_build_only_with_builder(self):
        with open(os.path.join(REPO, "skills", "core", "onboard", "SKILL.md"),
                  encoding="utf-8") as handle:
            text = handle.read()
        match = re.search(r"^- \*\*[^*]*[Cc]ommands worth knowing\*\*.*?(?=^- \*\*)",
                          text, re.S | re.M)
        self.assertTrue(match, "the commands bullet is missing")
        bullet = match.group(0)
        self.assertIn("/build", bullet)
        self.assertIn("builder", bullet)
        self.assertIn("lib/profiles.py selected", bullet)


class TestProseThatChangedMeaning(unittest.TestCase):
    """Template, /onboard verify, /skill-builder and the config doc describe the
    five-plugin split. Each asserts one parsed section, never the whole file."""

    @staticmethod
    def _read(*parts):
        with open(os.path.join(REPO, *parts), encoding="utf-8") as handle:
            return handle.read()

    @staticmethod
    def _sections(text):
        """`# ` heading -> section text, in order. A `# ` line inside a fence
        (a bash comment) is quoted text, not a heading."""
        headings = claude_imports.matches_outside_fences(re.compile(r"^# .*$", re.M), text)
        ends = [match.start() for match in headings[1:]] + [len(text)]
        return {match.group(0): text[match.start():end] for match, end in zip(headings, ends)}

    def test_policy_model_roles_names_the_builder_agents_and_how_to_get_them(self):
        body = self._sections(self._read("policy", "CLAUDE.md"))[
            "# Model roles \u2014 Opus architects, Sonnet executes\n".rstrip("\n")]
        flat = " ".join(body.split())
        self.assertIn("toolkit-build:executor", flat)
        self.assertIn("toolkit-build:task-reviewer", flat)
        self.assertIn("If you have the builder profile", flat)
        self.assertIn('list `"continuity", "builder"` under `"profiles"`', flat)
        self.assertIn("./install.sh", flat)
        self.assertIn("primary toolkit checkout, never a worktree", flat)
        self.assertIn("claude plugin marketplace list", flat)

    def test_onboard_verify_step_4_counts_per_selected_plugin(self):
        text = self._read("skills", "core", "onboard", "SKILL.md")
        match = re.search(r"(?ms)^4\. \*\*Check each selected plugin.*?(?=^5\. )", text)
        self.assertTrue(match, "verify step 4 not found")
        step = match.group(0)
        self.assertIn("lib/profiles.py selected", step)
        self.assertIn("skills/<profile>/*/SKILL.md", step)
        self.assertIn("agents/*.md", step)
        self.assertIn("builder", step)
        self.assertNotIn("skills/*/SKILL.md", step)

    def test_skill_builder_step_2_names_the_group_and_no_link(self):
        text = self._read("skills", "maintainer", "skill-builder", "SKILL.md")
        match = re.search(r"(?ms)^2\. On a new skill:.*?(?=^\d+\. |\n\n)", text)
        self.assertTrue(match, "toolkit step 2 not found")
        step = " ".join(match.group(0).split())
        self.assertIn("skills/<profile>/<name>/", step)
        self.assertIn("its group's plugin ships it", step)
        self.assertIn("no link", step)
        self.assertNotIn("plugin/skills", step)

    def test_config_example_profiles_note_names_the_five_and_the_default(self):
        with open(os.path.join(REPO, "config.example.json"), encoding="utf-8") as handle:
            note = " ".join(json.load(handle)["//profiles"])
        for name in ("core", "continuity", "builder", "maintainer", "mac"):
            self.assertIn(name, note)
        self.assertIn("core + continuity", note)
        self.assertIn("maintainer brings builder", note)


class TestFencedHeadingsAreQuoted(unittest.TestCase):
    """policy/CLAUDE.md quotes commands in fences, and a bash `# comment` is
    shaped like an H1. `_sections` must neither end nor start a section on
    one, and must keep the fenced body in the section that holds it."""

    def test_sections_skip_headings_inside_fences(self):
        for name, opener, closer in (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
                                     ("nested", "````\n```", "```\n````")):
            with self.subTest(fence=name):
                doc = ("# Alpha\nlive\n%s\n# Beta\n%s\ntail\n# Beta\nbody\n"
                       % (opener, closer))
                sections = TestProseThatChangedMeaning._sections(doc)
                self.assertEqual(list(sections), ["# Alpha", "# Beta"])
                self.assertIn("tail", sections["# Alpha"], "a fenced heading ended the section")
                self.assertEqual(sections["# Beta"], "# Beta\nbody\n")


if __name__ == "__main__":
    unittest.main()
