#!/usr/bin/env python3
"""A fresh clone's first instruction must work before anything is installed.

`/onboard` is a skill in the `toolkit-core` plugin, and only onboarding itself
(via install.sh and setup-plugins.sh) installs that plugin. Someone who clones
and types `/onboard` gets nothing. So the first-run instructions send a plain
message that points Claude at ONBOARDING.md, whose "If you are Claude reading
this" section hands it `skills/core/onboard/SKILL.md`. These tests pin each
surface that carries that first instruction, and the premise that makes it
necessary. ONBOARDING.md, README.md and the onboard skill all exist, and each
is checked.
"""

import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import claude_imports  # noqa: E402


REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILL = os.path.join("skills", "core", "onboard", "SKILL.md")

_H2_RE = re.compile(r"^## (.*)$", re.M)

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))


def read(relpath):
    with open(os.path.join(REPO, relpath), encoding="utf-8") as handle:
        return handle.read()


def section(text, heading):
    """The body under a `## ` heading, up to the next `## ` heading. A `## `
    line inside a fence is quoted text: it neither starts nor ends a section."""
    headings = claude_imports.matches_outside_fences(_H2_RE, text)
    for index, match in enumerate(headings):
        if match.group(1).rstrip() == heading:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            return text[match.end():end]
    raise AssertionError("no '## %s' section" % heading)


def fenced_blocks(text):
    """The body of every fenced block, by claude_imports' one fence rule: ``` or
    ~~~, and a longer opener holds shorter runs as body, so a ```` fence quoting
    ``` lines is one block. An unclosed fence is not a block."""
    blocks, body, fence = [], [], None
    for line in text.split("\n"):
        stripped = line.strip()
        if fence is None:
            fence = claude_imports._fence_open(stripped)
            body = []
        elif claude_imports._fence_closes(stripped, fence):
            blocks.append("".join(part + "\n" for part in body))
            fence = None
        else:
            body.append(line)
    return blocks


class TestPremise(unittest.TestCase):
    def test_the_checkout_ships_no_project_level_onboard(self):
        """If this ever fails, a fresh clone CAN resolve /onboard and the docs below
        could go back to it: the premise behind every other test here changed."""
        tracked = subprocess.run(
            ["git", "-C", REPO, "ls-files", ".claude/commands", ".claude/skills"],
            capture_output=True, text=True, check=True, stdin=subprocess.DEVNULL,
        ).stdout.split()
        self.assertEqual(tracked, [])

    def test_the_onboard_skill_is_where_the_docs_send_claude(self):
        self.assertTrue(os.path.isfile(os.path.join(REPO, SKILL)))


class TestNativeCli(unittest.TestCase):
    """Step 2 runs `claude` in Terminal and setup-plugins.sh shells out to `claude
    plugin`, but the VS Code extension does not put `claude` on PATH (its own docs:
    "you also need the standalone CLI install"). So the Mac path installs the CLI."""

    INSTALLER = "https://claude.ai/install.sh"

    def test_step1_installs_the_native_cli(self):
        step1 = section(read("ONBOARDING.md"), "Step 1: Install Claude Code")
        self.assertTrue(any(self.INSTALLER in b for b in fenced_blocks(step1)),
                        "Step 1 has no code block running the native installer")

    def _path_blocks(self):
        step1 = section(read("ONBOARDING.md"), "Step 1: Install Claude Code")
        return [b for b in fenced_blocks(step1)
                if self.INSTALLER not in b and ".local/bin" in b]

    def test_step1_puts_local_bin_on_path_when_claude_is_not_found(self):
        """Row 71 (28b F-B1): on a brand-new macOS user the installer wrote no rc
        file, so `claude` was "command not found" in a new window. Step 1's fix,
        pasted as written into a home with no rc file, must make a NEW Terminal
        window find the `claude` in ~/.local/bin, in zsh (the default) and bash
        (28b measured both failing). Executed, not grepped. The fixture home is
        a plain temp dir: nothing here writes the runner's own HOME."""
        blocks = self._path_blocks()
        self.assertEqual(len(blocks), 1,
                         "Step 1 needs exactly one block that puts ~/.local/bin on PATH")
        # A new Terminal window: an interactive zsh (reads ~/.zshrc), or bash,
        # which macOS Terminal starts as a login shell (reads ~/.bash_profile).
        new_windows = (("zsh", ["/bin/zsh", "-ic", "command -v claude"]),
                       ("bash", ["/bin/bash", "-lc", "command -v claude"]))
        with tempfile.TemporaryDirectory() as home:
            bindir = os.path.join(home, ".local", "bin")
            os.makedirs(bindir)
            fake = os.path.join(bindir, "claude")
            with open(fake, "w", encoding="utf-8") as handle:
                handle.write("#!/bin/sh\necho fake\n")
            os.chmod(fake, 0o755)
            env = {"HOME": home, "PATH": "/usr/bin:/bin", "ZDOTDIR": home}

            def run(argv):
                return subprocess.run(argv, env=env, capture_output=True, text=True,
                                      stdin=subprocess.DEVNULL)

            for shell, argv in new_windows:
                self.assertNotEqual(run(argv).returncode, 0,
                                    "fixture: claude already on PATH in %s" % shell)
            pasted = run(["/bin/zsh", "-c", blocks[0]])
            self.assertEqual(pasted.returncode, 0, pasted.stderr)
            for shell, argv in new_windows:
                after = run(argv)
                self.assertEqual(after.stdout.strip(), fake,
                                 "%s still cannot find claude: %s" % (shell, after.stderr))

    def test_step1_says_when_to_paste_the_path_line(self):
        step1 = " ".join(section(read("ONBOARDING.md"), "Step 1: Install Claude Code").split())
        self.assertIn("command not found", step1)
        self.assertIn("open another new window", step1)

    def test_readme_install_points_command_not_found_at_step1(self):
        body = " ".join(section(read("README.md"), "Install").split())
        self.assertRegex(body, r"`command not found`.*ONBOARDING\.md Step 1")


class TestOnboardingStep2(unittest.TestCase):
    def setUp(self):
        self.text = read("ONBOARDING.md")
        self.step2 = section(self.text, "Step 2: Hand it over to Claude")

    def test_no_code_block_tells_them_to_type_a_slash_command(self):
        for block in fenced_blocks(self.step2):
            for line in block.splitlines():
                self.assertFalse(line.strip().startswith("/"),
                                 "Step 2 tells a fresh clone to type %r" % line.strip())

    def test_the_first_message_points_claude_at_onboarding_md(self):
        messages = [b for b in fenced_blocks(self.step2) if "ONBOARDING.md" in b]
        self.assertTrue(messages, "Step 2 has no first message naming ONBOARDING.md")

    def test_what_the_message_points_at_leads_to_the_skill(self):
        """What makes the message doable: the file exists and hands Claude the skill."""
        claude_section = section(self.text, "If you are Claude reading this")
        self.assertIn("`%s`" % SKILL.replace(os.sep, "/"), claude_section)


class TestReadme(unittest.TestCase):
    def setUp(self):
        self.text = read("README.md")

    def test_new_here_leads_with_onboarding_md(self):
        body = section(self.text, "New here?").strip()
        first = body.split("\n\n")[0]
        self.assertIn("ONBOARDING.md", first)
        self.assertNotRegex(first, r"^\*\*Run `/onboard`")

    def test_the_skip_it_all_route_names_onboarding_md(self):
        body = section(self.text, "Install")
        skip = re.search(r"Or skip all of that(.*?)(?:\n\n|\Z)", body, re.DOTALL)
        self.assertIsNotNone(skip)
        self.assertIn("ONBOARDING.md", skip.group(1))


class TestFencesAreQuoted(unittest.TestCase):
    """A `## ` line inside a fence is quoted text, and a fence is whatever the
    one fence rule says it is. Either helper getting it wrong lets Step 2's
    no-slash-command check read fewer lines than the doc holds, and pass."""

    def test_section_skips_headings_inside_fences(self):
        for name, opener, closer in FENCES:
            with self.subTest(fence=name):
                template = ("## Step 2\nlive\n%s\n## Fake\n%s\ntail\n## Step 3\nafter\n")
                body = section(template % (opener, closer), "Step 2")
                self.assertIn("tail", body, "a fenced heading ended the section")
                self.assertIn("## Fake", body, "the fenced body was dropped")
                self.assertNotIn("after", body)
                # Positive control: without the fence markers the line IS a heading.
                self.assertEqual(section(template % ("", ""), "Step 2"), "\nlive\n\n")
                # A quoted heading must not START the section either.
                quoted = "## A\n%s\n## Step 2\n%s\n## B\n"
                with self.assertRaises(AssertionError):
                    section(quoted % (opener, closer), "Step 2")
                self.assertEqual(section(quoted % ("", ""), "Step 2"), "\n\n")

    def test_fenced_blocks_reads_every_fence_whole(self):
        self.assertEqual(fenced_blocks("~~~\n/onboard\n~~~\n"), ["/onboard\n"])
        self.assertEqual(fenced_blocks("````\n```\n/onboard\n```\n````\nout\n"),
                         ["```\n/onboard\n```\n"])
        # Positive control: the plain case reads as it always did, and text
        # between two blocks is in neither.
        self.assertEqual(fenced_blocks("```bash\na\n```\nout\n```\nb\n```\n"),
                         ["a\n", "b\n"])
        self.assertEqual(fenced_blocks("```\nnever closed\n"), [])


if __name__ == "__main__":
    unittest.main()
