#!/usr/bin/env python3
"""`/onboard` Phase 2 asks which profiles to install before any tool question.

These tests read the document's own `##` heading offsets and fenced ```bash
blocks, as `test_onboard_setup_audit.py` does -- never a whole-document substring
search. One test EXECUTES the documented `--profiles` command against a temp
config, so the step is doable, not just present.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import claude_imports  # noqa: E402

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
ONBOARD_MD = os.path.join(REPO, "skills", "core", "onboard", "SKILL.md")
ONBOARDING_MD = os.path.join(REPO, "ONBOARDING.md")
README_MD = os.path.join(REPO, "README.md")
INSTALL_SH = os.path.join(REPO, "install.sh")
CATALOGUE = "docs/catalogue.md"

_FENCE_RE = re.compile(r"```bash\n(.*?)```", re.S)
_HEADING_RE = re.compile(r"^(##\s+.*)$", re.M)
_SUBHEADING_RE = re.compile(r"^#{2,3}\s.*$", re.M)
CONFIG_PATH_TEXT = "~/.config/claude-code-toolkit/config.json"
UNINSTALL_PHRASE = "is no longer selected (would uninstall)"

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _section(text, heading_prefix):
    """From the `## <heading_prefix>` heading to the next `## ` heading. A
    heading inside a fence is quoted text: it neither starts nor ends one."""
    headings = claude_imports.matches_outside_fences(_HEADING_RE, text)
    for index, match in enumerate(headings):
        if match.group(1).startswith(heading_prefix):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return text[match.start():end]
    raise AssertionError("no heading starting %r" % heading_prefix)


def _subsection(section, heading_prefix):
    """From a `### <heading_prefix>` heading to the next `##`/`###` heading or
    the end. Headings inside a fence are quoted text, not headings."""
    headings = claude_imports.matches_outside_fences(_SUBHEADING_RE, section)
    for index, match in enumerate(headings):
        if match.group(0).startswith(heading_prefix):
            end = headings[index + 1].start() if index + 1 < len(headings) else len(section)
            return section[match.start():end]
    raise AssertionError("no heading starting %r" % heading_prefix)


def _bullet(section, phrase):
    """The list bullet (its wrapped lines included) holding `phrase`, and its
    offset in `section`."""
    lines = section.splitlines(keepends=True)
    offset = 0
    for i, line in enumerate(lines):
        if phrase in line:
            start = offset
            while not lines[i].startswith("- ") and i > 0:
                i -= 1
                start -= len(lines[i])
            text = lines[i]
            for follow in lines[i + 1:]:
                if not follow.strip() or follow.startswith(("- ", "#")):
                    break
                text += follow
            return text, start
        offset += len(line)
    return None, None


def _child_env(config, home):
    """Explicit allowlist: nothing else from the parent leaks into a child."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": home,
           "USERPROFILE": home, "CLAUDE_TOOLKIT_CONFIG": config,
           "PYTHONDONTWRITEBYTECODE": "1"}
    if os.environ.get("LANG"):
        env["LANG"] = os.environ["LANG"]
    return env


class TestProfileChooser(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.doc = _read(ONBOARD_MD)
        cls.phase2 = _section(cls.doc, "## Phase 2")
        cls.phase0 = _section(cls.doc, "## Phase 0")

    def _documented_command(self):
        blocks = [b for b in _FENCE_RE.findall(self.phase2) if "--profiles" in b]
        self.assertEqual(len(blocks), 1, "expected one bash block with --profiles")
        command = re.sub(r"\\\n\s*", " ", blocks[0]).strip()
        self.assertIn(CONFIG_PATH_TEXT, command)
        self.assertIn('"$(pwd)"', command)
        return command

    def _run_documented_command(self, repo=REPO):
        # `"$(pwd)"` is quoted in the document, so its stand-in is quoted too: a
        # checkout path with a space must stay one argument.
        command = self._documented_command()
        with tempfile.TemporaryDirectory() as tmp:
            config = os.path.join(tmp, "config.json")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump({"owner_name": "Sam"}, handle)
            command = command.replace(CONFIG_PATH_TEXT, shlex.quote(config)) \
                .replace('"$(pwd)"', shlex.quote(repo))
            result = subprocess.run(["bash", "-c", command], cwd=repo,
                                    env=_child_env(config, tmp),
                                    stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=120)
            written = None
            if result.returncode == 0:
                written = json.loads(_read(config))
        return result, written

    def test_profiles_question_comes_before_any_tool_toggle(self):
        text = self.phase2
        profiles = text.find("### Profiles")
        tier2 = text.find("**Tier 2 — one question per group")
        enable = text.find("--enable")
        command = text.find("--profiles")
        self.assertNotEqual(profiles, -1, "no ### Profiles heading in Phase 2")
        self.assertNotEqual(tier2, -1, "no Tier 2 paragraph in Phase 2")
        self.assertNotEqual(enable, -1, "no --enable in Phase 2")
        self.assertLess(profiles, tier2)
        self.assertLess(tier2, enable)
        self.assertNotEqual(command, -1, "no --profiles command in Phase 2")
        self.assertTrue(profiles < command < tier2,
                        "the --profiles command must sit inside the Profiles subsection")

    def test_the_documented_profiles_command_runs(self):
        # The recommendation is exactly these three: an extra name (mac) would be
        # preselected for everyone who accepts it.
        csv = re.findall(r"--profiles\s+(\S+)", self._documented_command())
        self.assertEqual(csv, ["core,continuity,builder"])
        result, written = self._run_documented_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(written["profiles"][:2], ["core", "continuity"])
        self.assertEqual(sorted(written["profiles"]), ["builder", "continuity", "core"])

    def test_the_documented_profiles_command_runs_from_a_checkout_path_with_a_space(self):
        # The live install proof uses such a path; a link stands in for the move.
        with tempfile.TemporaryDirectory() as tmp:
            spaced = os.path.join(tmp, "My Tools", "claude-code-toolkit")
            os.makedirs(os.path.dirname(spaced))
            os.symlink(REPO, spaced)
            result, written = self._run_documented_command(spaced)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(written["profiles"]), ["builder", "continuity", "core"])

    def test_the_child_env_is_an_allowlist(self):
        """Positive control for the env rule: a BASH_ENV in the PARENT that
        would kill any bash inheriting it must not reach the documented command."""
        with tempfile.TemporaryDirectory() as tmp:
            poison = os.path.join(tmp, "poison.sh")
            with open(poison, "w", encoding="utf-8") as handle:
                handle.write("exit 73\n")
            with mock.patch.dict(os.environ, {"BASH_ENV": poison}):
                # the poison is real: a bash that inherits it dies with 73
                inherited = subprocess.run(["bash", "-c", "true"], env=dict(os.environ),
                                           stdin=subprocess.DEVNULL, capture_output=True,
                                           timeout=60)
                self.assertEqual(inherited.returncode, 73)
                result, written = self._run_documented_command()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(written["profiles"][:2], ["core", "continuity"])

    def test_caffeinate_is_never_offered_as_a_tool(self):
        self.assertNotIn("offer `caffeinate` alone", self.phase2)
        self.assertIn("Tier 3 has no question of its own", self.phase2)

    def test_mac_is_offered_only_on_darwin(self):
        start = self.phase2.index("### Profiles")
        mac = re.search(r"\bMac\b", self.phase2[start:])
        self.assertIsNotNone(mac, "no Mac profile mention in the Profiles subsection")
        window = self.phase2[start + mac.start():start + mac.start() + 300]
        self.assertIn("Darwin", window)

    def test_no_profile_is_described_as_needing_an_outside_plugin(self):
        # Nothing the toolkit ships needs superpowers; onboarding must not
        # tell anyone to install it as a requirement.
        self.assertNotIn("superpowers", self.doc)

    def test_phase_0_stops_on_a_non_darwin_host(self):
        bullet, _ = _bullet(self.phase0, "`uname -s`")
        self.assertIsNotNone(bullet, "no Phase 0 bullet about a non-Darwin host")
        flat = " ".join(self.phase0.split())
        self.assertIn("macOS-only", " ".join(bullet.split()))
        self.assertIn("stop", " ".join(bullet.split()))
        self.assertIn("macOS-only", flat)
        for gone in ("MINGW", "WSL"):
            self.assertNotIn(gone, flat)

    def test_phase_2_links_the_catalogue(self):
        self.assertIn(CATALOGUE, self.phase2)
        self.assertTrue(os.path.isfile(os.path.join(REPO, CATALOGUE)))

    def test_onboarding_and_readme_link_the_catalogue(self):
        for label, path in (("ONBOARDING.md", ONBOARDING_MD), ("README.md", README_MD)):
            self.assertIn(CATALOGUE, _read(path), label)

    def test_now_install_names_profile_plugins(self):
        self.assertIn("plugin for each profile", _subsection(self.phase2, "### Now install"))

    def test_a_declined_but_installed_profile_gets_a_keep_or_disable_question(self):
        """The skill must look for the exact line install.sh prints."""
        phrase = "enabled outside your profiles — left as is"
        self.assertIn(phrase, _read(INSTALL_SH))
        self.assertIn(phrase, self.phase2)
        self.assertIn("claude plugin disable <plugin>@claude-code-toolkit", self.phase2)

    def test_a_profile_the_install_would_uninstall_gets_a_keep_or_let_go_question(self):
        self.assertIn(UNINSTALL_PHRASE, _read(INSTALL_SH))
        self.assertIn(UNINSTALL_PHRASE, self.phase2)
        bullet, offset = _bullet(self.phase2, UNINSTALL_PHRASE)
        self.assertIsNotNone(bullet)
        flat = " ".join(bullet.split())
        for needle in ("let it go", "keep", "re-run the `--profiles` command",
                       "then the preview"):
            self.assertIn(needle, flat)
        self.assertLess(offset, self.phase2.index("### Now install"))


class TestReadmeConfigCommandsRun(unittest.TestCase):
    """Every `setup_config.py --update` line in README's bash blocks, executed
    against a temp config from the checkout, as a reader would run it. README
    once showed one without the config path and repo, which made
    `setup_config.py` read `--update` as the config path."""

    EXPECTED = {
        "--profiles": lambda written: sorted(written["profiles"]) == [
            "builder", "continuity", "core"],
        "--set update.origin=": lambda written: written["update"] == {
            "origin": "https://github.com/you/claude-code-toolkit"},
    }

    def _commands(self):
        lines = [line.strip() for block in _FENCE_RE.findall(_read(README_MD))
                 for line in block.splitlines()
                 if "setup_config.py" in line and "--update" in line]
        self.assertEqual(len(lines), len(self.EXPECTED), lines)
        return lines

    def test_each_documented_update_command_runs_as_written(self):
        for line in self._commands():
            flag = [f for f in self.EXPECTED if f in line]
            self.assertEqual(len(flag), 1, line)
            self.assertIn(CONFIG_PATH_TEXT, line)
            self.assertIn('"$(pwd)"', line)
            with tempfile.TemporaryDirectory() as tmp:
                config = os.path.join(tmp, "config.json")
                with open(config, "w", encoding="utf-8") as handle:
                    json.dump({"owner_name": "Sam"}, handle)
                command = line.replace(CONFIG_PATH_TEXT, shlex.quote(config)) \
                    .replace('"$(pwd)"', shlex.quote(REPO))
                result = subprocess.run(["bash", "-c", command], cwd=REPO,
                                        env=_child_env(config, tmp), stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=120)
                self.assertEqual(result.returncode, 0, "%s\n%s" % (line, result.stderr))
                self.assertTrue(self.EXPECTED[flag[0]](json.loads(_read(config))), line)


class TestFencedHeadingsAreQuoted(unittest.TestCase):
    """A heading-shaped line inside a fence is quoted text. `_section` and
    `_subsection` must neither end nor start on one, and must keep the fenced
    body in the (sub)section that holds it."""

    def test_section_and_subsection_skip_headings_inside_fences(self):
        for name, opener, closer in FENCES:
            with self.subTest(fence=name):
                doc = ("## Alpha\n### One\nlive\n%s\n## Beta fake\n### Two fake\n%s\n"
                       "tail\n### Two\nbody\n## Beta\nend\n" % (opener, closer))
                alpha = _section(doc, "## Alpha")
                self.assertIn("### Two\nbody", alpha, "a fenced heading ended the section")
                self.assertEqual(_section(doc, "## Beta"), "## Beta\nend\n")
                one = _subsection(alpha, "### One")
                self.assertIn("tail", one, "a fenced heading ended the subsection")
                self.assertIn("### Two fake", one, "the fenced body was dropped")
                self.assertEqual(_subsection(alpha, "### Two"), "### Two\nbody\n")


if __name__ == "__main__":
    unittest.main()
