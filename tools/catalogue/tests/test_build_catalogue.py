#!/usr/bin/env python3
"""docs/catalogue.md must be generated from the repo, and every hook must say
what it writes on its own.

The silent failures pinned here:
  * a committed catalogue that has drifted from the registry, the hook
    manifests or the installer -- it reads fine and is simply wrong;
  * a hook with no `writers` entry (or an entry for a hook that is gone), so the
    disclosure the catalogue promises has a hole nobody sees;
  * a CLI list holding this machine's absolute paths, which differs per machine
    and fails local CI for everyone else.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(TOOL))
SCRIPT = os.path.join(TOOL, "build_catalogue.py")
sys.path.insert(0, TOOL)
sys.path.insert(0, os.path.join(REPO, "lib"))
import build_catalogue  # noqa: E402
import hook_manifest  # noqa: E402
import profiles  # noqa: E402

STALE_MESSAGE = "python3 tools/catalogue/build_catalogue.py"


def clean_env():
    """os.environ without GIT_*: install.sh runs git, and a hook-inherited
    GIT_DIR would point it at the wrong repository."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def run_cli(*args):
    return subprocess.run([sys.executable, SCRIPT] + list(args), env=clean_env(),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True)


def real_writers():
    with open(os.path.join(REPO, "claude", "profiles.json"), encoding="utf-8") as handle:
        return json.load(handle)["writers"]


def real_relpaths():
    return {r["relpath"] for r in hook_manifest.plugin_registrations(REPO)}


class TestCheckMode(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_committed_catalogue_is_fresh(self):
        result = run_cli("--check")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_check_fails_on_a_stale_file(self):
        rendered = build_catalogue.render(REPO)
        fresh = os.path.join(self.tmp, "fresh.md")
        stale = os.path.join(self.tmp, "stale.md")
        with open(fresh, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered)
        with open(stale, "w", encoding="utf-8", newline="") as handle:
            handle.write(rendered + "an extra line\n")
        # Positive control: the same render, untouched, passes.
        control = run_cli("--check", "--path", fresh)
        self.assertEqual(control.returncode, 0, control.stdout + control.stderr)
        result = run_cli("--check", "--path", stale)
        self.assertEqual(result.returncode, 1)
        self.assertIn(STALE_MESSAGE, result.stdout + result.stderr)

    def test_check_fails_when_the_file_is_missing(self):
        result = run_cli("--check", "--path", os.path.join(self.tmp, "nope.md"))
        self.assertEqual(result.returncode, 1)


class TestDescriptionParsing(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def description(self, front_matter):
        path = os.path.join(self.tmp, "x.md")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(front_matter)
        return build_catalogue.front_matter_description(path)

    def test_folded_block(self):
        self.assertEqual(self.description(
            "---\nname: x\ndescription: >-\n  One two.\n  Three four.\nmodel: y\n---\nbody\n"),
            "One two. Three four.")

    def test_literal_block(self):
        self.assertEqual(self.description(
            "---\ndescription: |+\n  Alpha\n  Beta\n---\n"), "Alpha Beta")

    def test_inline_and_quoted(self):
        self.assertEqual(self.description("---\ndescription: Plain words. More.\n---\n"),
                         "Plain words. More.")
        self.assertEqual(self.description('---\ndescription: "Quoted: yes."\n---\n'),
                         "Quoted: yes.")
        self.assertEqual(self.description("---\ndescription: 'Single.'\n---\n"), "Single.")

    def test_absent(self):
        self.assertEqual(self.description("---\nname: x\n---\nbody\n"), "")
        self.assertEqual(self.description("no front matter\n"), "")

    def test_first_sentence(self):
        first = build_catalogue.first_sentence
        self.assertEqual(first("One. Two."), "One.")
        self.assertEqual(first("Is it? Yes."), "Is it?")
        self.assertEqual(first("Wow! Next."), "Wow!")
        self.assertEqual(first("Version 1.5 is fine. Next."), "Version 1.5 is fine.")
        self.assertEqual(first("No terminator"), "No terminator")
        self.assertEqual(first("Ends here."), "Ends here.")
        self.assertEqual(first(""), "")


class TestWriters(unittest.TestCase):
    def test_every_hook_has_a_writers_entry(self):
        self.assertEqual(build_catalogue.writer_problems(real_writers(), real_relpaths()), [])

    def test_writer_problems_reports_missing_and_extra(self):
        writers = dict(real_writers())
        relpaths = sorted(real_relpaths())
        dropped = relpaths[0]
        del writers[dropped]
        writers["tools/bogus/claude-bogus"] = "nothing"
        problems = build_catalogue.writer_problems(writers, set(relpaths))
        self.assertIn("no writers entry for " + dropped, problems)
        self.assertIn("writers entry for tools/bogus/claude-bogus matches no hook", problems)
        self.assertEqual(len(problems), 2)

    def test_a_writers_gap_exits_2_and_writes_nothing(self):
        fixture = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, fixture, True)
        os.makedirs(os.path.join(fixture, "claude"))
        for manifest in glob.glob(os.path.join(REPO, "plugin*", "hooks", "hooks.json")):
            rel = os.path.relpath(manifest, REPO)
            os.makedirs(os.path.join(fixture, os.path.dirname(rel)), exist_ok=True)
            shutil.copy(manifest, os.path.join(fixture, rel))
        with open(os.path.join(REPO, "claude", "profiles.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        victim = sorted(real_relpaths())[0]
        registry_path = os.path.join(fixture, "claude", "profiles.json")
        out = os.path.join(fixture, "out.md")

        def write_registry(writers):
            patched = dict(data)
            patched["writers"] = writers
            with open(registry_path, "w", encoding="utf-8") as handle:
                json.dump(patched, handle)

        # Positive control: with the key present the writers check passes (the
        # fixture has no install.sh, so it fails later, for another reason).
        write_registry(data["writers"])
        control = run_cli("--repo", fixture, "--path", out)
        self.assertNotIn("writers", control.stderr)

        writers = {k: v for k, v in data["writers"].items() if k != victim}
        write_registry(writers)
        result = run_cli("--repo", fixture, "--path", out)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn(victim, result.stderr)
        self.assertFalse(os.path.exists(out))


class TestRender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = build_catalogue.render(REPO)
        cls.registry = profiles.load_registry(REPO)

    def test_every_profile_appears_once(self):
        headings = re.findall(r"^## (\S+) — ", self.text, re.M)
        headings = [h for h in headings if h in self.registry]
        self.assertEqual(headings, list(self.registry))

    def test_every_skill_plugin_is_listed_with_its_install_command(self):
        self.assertIn("## Skill plugins — optional, one skill each", self.text)
        dirs = sorted(os.listdir(os.path.join(REPO, "plugins")))
        self.assertTrue(dirs)
        with open(os.path.join(REPO, ".claude-plugin", "marketplace.json"),
                  encoding="utf-8") as handle:
            market = json.load(handle)
        by_source = {e["source"]: e["name"] for e in market["plugins"]}
        for directory in dirs:
            name = by_source["./plugins/" + directory]
            self.assertIn("| `%s` | `claude plugin install %s@claude-code-toolkit` |"
                          % (name, name), self.text)

    def test_a_profile_names_the_skill_plugins_it_installs(self):
        """A profile whose manifest depends on a skill plugin installs it too;
        the catalogue says so in that profile's own section."""
        for profile, entry in self.registry.items():
            with open(os.path.join(REPO, entry["dir"], ".claude-plugin", "plugin.json"),
                      encoding="utf-8") as handle:
                deps = json.load(handle).get("dependencies", [])
            section = self.text.split("## %s — " % profile, 1)[1].split("\n## ", 1)[0]
            for dep in (d for d in deps if d.startswith("skill-")):
                with self.subTest(profile=profile, dep=dep):
                    self.assertIn("Also installs: `%s`." % dep, section)
        self.assertIn("Also installs: `skill-prompt-optimizer`.",
                      self.text.split("## maintainer — ", 1)[1].split("\n## ", 1)[0])

    def test_cli_rows_are_machine_independent(self):
        rows = build_catalogue.cli_rows(REPO)
        self.assertTrue(rows)
        home = os.path.expanduser("~")
        for name, source in rows:
            self.assertFalse(source.startswith("/"), source)
            self.assertNotIn(home, source)
            self.assertNotIn("/var/folders", source)
        self.assertIn("claude-danger-guard", [name for name, _ in rows])

    def test_hook_rows_cover_every_relpath(self):
        sections = {}
        parts = re.split(r"^## ", self.text, flags=re.M)
        for part in parts[1:]:
            sections[part.split(" — ", 1)[0]] = part
        rows = hook_manifest.plugin_registrations(REPO)
        self.assertTrue(rows)
        # A hook may be registered by several profiles (the heartbeat is, in every one);
        # each of those sections lists it once and every other section not at all.
        homes = {}
        for row in rows:
            homes.setdefault(row["relpath"], set()).add(row["profile"])
        for row in rows:
            marker = "| `%s` |" % row["exe"]
            for name, part in sections.items():
                if name in homes[row["relpath"]]:
                    self.assertEqual(part.count(marker), 1, (name, row["relpath"]))
                elif name in self.registry:
                    self.assertNotIn(marker, part, (name, row["relpath"]))


if __name__ == "__main__":
    unittest.main()
