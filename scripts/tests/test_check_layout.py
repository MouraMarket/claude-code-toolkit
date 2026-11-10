"""Tests for scripts/check_layout.py. Each rule gets a synthetic layout that
breaks exactly it, so disabling the rule fails its test."""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import check_layout  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


SKILL_MD = "plugins/demo/skills/demo/SKILL.md"


class Layout(unittest.TestCase):
    """A valid three-plugin layout in a temp dir; each test breaks one thing."""

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="layout-"))
        self.addCleanup(shutil.rmtree, self.root)
        write(self.path("VERSION"), "1.2.3\n")
        self.market = {"name": "claude-code-toolkit", "plugins": [
            {"name": "toolkit-core", "source": "./plugin"},
            {"name": "toolkit-extra", "source": "./plugin-extra"},
            {"name": "skill-demo", "source": "./plugins/demo"},
        ]}
        self.save_market()
        self.manifest("plugin", {"name": "toolkit-core"})
        self.manifest("plugin-extra", {"name": "toolkit-extra",
                                       "dependencies": ["toolkit-core"]})
        self.manifest("plugins/demo", {"name": "skill-demo"})
        write(self.path(SKILL_MD), "---\nname: demo\ndescription: x\n---\nSee `references/guide.md`.\n")
        write(self.path("plugins/demo/skills/demo/references/guide.md"), "guide\n")
        write(self.path("tools/demo/run"), "#!/bin/sh\n")
        os.chmod(self.path("tools/demo/run"), 0o755)
        os.symlink("../tools", self.path("plugin-extra/tools"))
        self.hooks([{"command": '"${CLAUDE_PLUGIN_ROOT}"/tools/demo/run --hook'}])

    def path(self, name):
        return os.path.join(self.root, name)

    def save_market(self):
        write(self.path(".claude-plugin/marketplace.json"), json.dumps(self.market))

    def manifest(self, directory, body):
        write(self.path(directory + "/.claude-plugin/plugin.json"), json.dumps(body))

    def hooks(self, hook_list):
        write(self.path("plugin-extra/hooks/hooks.json"),
              json.dumps({"hooks": {"SessionStart": [{"hooks": hook_list}]}}))

    def problems(self):
        return check_layout.check(self.root)

    def assertProblem(self, fragment):
        found = self.problems()
        self.assertTrue(any(fragment in p for p in found), "%r not in %r" % (fragment, found))

    # The baseline must be clean, or every other test proves nothing.
    def test_valid_layout_is_clean(self):
        self.assertEqual(self.problems(), [])

    def test_version_missing(self):
        os.remove(self.path("VERSION"))
        self.assertProblem("VERSION: missing")

    def test_version_malformed(self):
        write(self.path("VERSION"), "1.2\n")
        self.assertProblem("VERSION: not MAJOR.MINOR.PATCH")

    # A version pins GitHub installs to one cached copy; any value, even an
    # empty one, is refused, in each place the CLI reads it from.
    def test_manifest_sets_version(self):
        for value in ("1.2.3", ""):
            self.manifest("plugins/demo", {"name": "skill-demo", "version": value})
            self.assertProblem("plugins/demo: plugin.json sets a version")

    def test_marketplace_entry_sets_version(self):
        self.market["plugins"][2]["version"] = "1.2.3"
        self.save_market()
        self.assertProblem("marketplace: skill-demo sets a version")

    def test_marketplace_name(self):
        self.market["name"] = "other"
        self.save_market()
        self.assertProblem("marketplace: name is not claude-code-toolkit")

    def test_marketplace_empty(self):
        self.market["plugins"] = []
        self.save_market()
        self.assertProblem("marketplace: no plugins listed")

    def test_plugin_prefix(self):
        self.market["plugins"][0]["name"] = "core"
        self.save_market()
        self.manifest("plugin", {"name": "core"})
        self.assertProblem("lacks the toolkit- or skill- prefix")

    def test_duplicate_plugin(self):
        self.market["plugins"].append({"name": "toolkit-core", "source": "./plugin"})
        self.save_market()
        self.assertProblem("plugin toolkit-core listed twice")

    def test_source_not_relative(self):
        self.market["plugins"][1]["source"] = self.path("plugin-extra")
        self.save_market()
        self.assertProblem("toolkit-extra source is not a ./ path")

    def test_source_escapes(self):
        self.market["plugins"][1]["source"] = "./../elsewhere"
        self.save_market()
        self.assertProblem("toolkit-extra source escapes the root")

    def test_manifest_name_mismatch(self):
        self.manifest("plugin", {"name": "toolkit-other"})
        self.assertProblem("plugin: plugin.json name does not match the marketplace")

    def test_manifest_unreadable(self):
        write(self.path("plugin/.claude-plugin/plugin.json"), "{not json")
        self.assertProblem("unreadable JSON")

    def test_unknown_dependency(self):
        self.manifest("plugin-extra", {"name": "toolkit-extra",
                                       "dependencies": ["toolkit-ghost"]})
        self.assertProblem("toolkit-extra: depends on unknown plugin toolkit-ghost")

    def test_dependency_cycle(self):
        self.manifest("plugin", {"name": "toolkit-core",
                                 "dependencies": ["toolkit-extra"]})
        self.assertProblem("dependency cycle")

    def test_absolute_link(self):
        os.symlink(self.path("tools"), self.path("plugin/tools"))
        self.assertProblem("link escapes the root: plugin/tools")

    def test_link_escapes_lexically(self):
        os.symlink("../../outside", self.path("plugin/out"))
        self.assertProblem("link escapes the root: plugin/out")

    def test_link_chain_resolves_outside(self):
        # plugin/a -> b stays lexically inside, but b points out of the root.
        outside = os.path.realpath(tempfile.mkdtemp(prefix="outside-"))
        self.addCleanup(shutil.rmtree, outside)
        os.symlink(outside, self.path("plugin/b"))
        os.symlink("b", self.path("plugin/a"))
        self.assertProblem("link resolves outside the root: plugin/a")

    def test_dangling_link(self):
        os.symlink("../lib", self.path("plugin/lib"))
        self.assertProblem("missing: lib")

    def test_hook_target_missing(self):
        self.hooks([{"command": '"${CLAUDE_PLUGIN_ROOT}"/tools/gone/run'}])
        self.assertProblem("missing: tools/gone/run")

    def test_hook_target_not_executable(self):
        os.chmod(self.path("tools/demo/run"), stat.S_IRUSR | stat.S_IWUSR)
        self.assertProblem("not executable: tools/demo/run")

    def test_hook_command_not_plugin_root(self):
        self.hooks([{"command": "/usr/bin/true"}])
        self.assertProblem("hook command does not start at ${CLAUDE_PLUGIN_ROOT}")

    def test_empty_skills_dir_refuses(self):
        os.makedirs(self.path("plugin-extra/skills/notes"))
        write(self.path("plugin-extra/skills/.pending"), "placeholder\n")
        self.assertProblem("toolkit-extra ships no skill")

    def test_skills_dir_with_a_skill_passes(self):
        write(self.path("plugin-extra/skills/demo/SKILL.md"), "---\nname: demo\n---\n")
        self.assertEqual(self.problems(), [])

    def test_skill_plugin_source(self):
        self.market["plugins"][2]["source"] = "./plugin-extra"
        self.save_market()
        self.assertProblem("skill-demo source is not ./plugins/demo")

    def test_skill_plugin_declares_dependencies(self):
        self.manifest("plugins/demo", {"name": "skill-demo",
                                       "dependencies": ["toolkit-core"]})
        self.assertProblem("skill-demo: skill plugin declares dependencies")

    def test_skill_plugin_has_hooks(self):
        write(self.path("plugins/demo/hooks/hooks.json"), "{}")
        self.assertProblem("skill-demo: skill plugin has a hooks/ directory")

    def test_skill_plugin_has_agents(self):
        os.makedirs(self.path("plugins/demo/agents"))
        self.assertProblem("skill-demo: skill plugin has a agents/ directory")

    def test_skill_plugin_without_skills_dir(self):
        shutil.rmtree(self.path("plugins/demo/skills"))
        self.assertProblem("skill-demo: skills/ must hold exactly one entry, demo (found none)")

    def test_skill_plugin_with_two_skills(self):
        write(self.path("plugins/demo/skills/other/SKILL.md"), "---\nname: other\n---\n")
        self.assertProblem("skill-demo: skills/ must hold exactly one entry, demo (found demo, other)")

    def test_skill_plugin_wrong_skill_directory(self):
        os.rename(self.path("plugins/demo/skills/demo"), self.path("plugins/demo/skills/other"))
        self.assertProblem("skill-demo: skills/ must hold exactly one entry, demo (found other)")

    def test_skill_plugin_without_skill_md(self):
        os.remove(self.path(SKILL_MD))
        self.assertProblem("skill-demo: skills/demo/SKILL.md is missing")

    def test_skill_plugin_front_matter_name(self):
        write(self.path(SKILL_MD), "---\nname: other\n---\nSee `references/guide.md`.\n")
        self.assertProblem("skill-demo: SKILL.md front matter name is not demo")

    def test_skill_plugin_without_front_matter(self):
        write(self.path(SKILL_MD), "name: demo\nSee `references/guide.md`.\n")
        self.assertProblem("skill-demo: SKILL.md front matter name is not demo")

    def test_skill_plugin_reference_missing(self):
        os.remove(self.path("plugins/demo/skills/demo/references/guide.md"))
        self.assertProblem("SKILL.md mentions references/guide.md, which is not a file")

    def test_skill_plugin_bare_reference_missing(self):
        write(self.path(SKILL_MD), "---\nname: demo\n---\nRead references/gone.md first.\n")
        self.assertProblem("SKILL.md mentions references/gone.md, which is not a file")

    def test_skill_plugin_reference_escapes_skill(self):
        write(self.path("plugins/demo/secret.md"), "x\n")
        write(self.path(SKILL_MD), "---\nname: demo\n---\nSee `references/../../../secret.md`.\n")
        self.assertProblem("SKILL.md mentions references/../../../secret.md, which is not a file")

    def test_cli_exit_codes(self):
        self.assertEqual(check_layout.main(["--root", self.root]), 0)
        self.manifest("plugin", {"name": "toolkit-core", "version": "1.2.3"})
        self.assertEqual(check_layout.main(["--root", self.root]), 1)


class RealTree(unittest.TestCase):
    def test_repository_layout(self):
        self.assertEqual(check_layout.check(REPO), [])
        self.assertEqual(check_layout.main(["--root", REPO]), 0)


if __name__ == "__main__":
    unittest.main()
