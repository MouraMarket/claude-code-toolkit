#!/usr/bin/env python3
"""`/onboard` finishes the auto memory setting: Phase 6 sends the teammate to
reopen Claude Code in their work folder, and Phase 6b step 5 runs
`auto_memory.py --status` and acts on it. These tests parse the document's own
fenced ```bash blocks and `##` heading offsets, as `test_onboard_setup_audit.py`
does -- never a whole-document substring search. Assertion 3 EXECUTES the
documented status command against fixtures, so the step is doable, not just
present. The ONBOARDING.md and README.md halves are checked too.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
LIB = os.path.join(REPO, "lib")
ONBOARD_MD = os.path.join(REPO, "skills", "core", "onboard", "SKILL.md")
ONBOARDING_MD = os.path.join(REPO, "ONBOARDING.md")
README_MD = os.path.join(REPO, "README.md")

sys.path.insert(0, HERE)

if LIB not in sys.path:
    sys.path.insert(0, LIB)
import auto_memory  # noqa: E402
import claude_imports  # noqa: E402

_FENCE_RE = re.compile(r"```bash\n(.*?)```", re.S)
_HEADING_RE = re.compile(r"^(##\s+.*)$", re.M)

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))

STATUS_NEEDLE = 'auto_memory.py" --status'
INSTALL_NEEDLE = 'CLAUDE_INSTALL_SKIP_SUITES=1 "<checkout>/install.sh"'
REASON_TEXT = "no session has run in workspace_root yet"


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _section(text, prefix):
    """Text of the `##` section whose heading line starts with `prefix`, up to
    the next `##` heading (or end of file), plus its start offset. Located
    from parsed heading lines; a `##` line inside a fence is quoted text."""
    headings = claude_imports.matches_outside_fences(_HEADING_RE, text)
    for i, match in enumerate(headings):
        if match.group(1).startswith(prefix):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
            return text[match.start():end], match.start()
    return None, None


def _blocks_with_offsets(section):
    """(offset in section, body) for every fenced ```bash block."""
    return [(m.start(), m.group(1)) for m in _FENCE_RE.finditer(section)]


def _first_block(section, needle):
    for offset, body in _blocks_with_offsets(section):
        if needle in body:
            return offset, body
    return None, None


def _paragraphs(text):
    """(offset, paragraph) pairs, split on blank lines."""
    out = []
    pos = 0
    for chunk in re.split(r"\n\s*\n", text):
        start = text.index(chunk, pos)
        out.append((start, chunk))
        pos = start + len(chunk)
    return out


class OnboardCase(unittest.TestCase):

    def setUp(self):
        self.text = _read(ONBOARD_MD)
        self.p6b, _ = _section(self.text, "## Phase 6b")
        self.p6, _ = _section(self.text, "## Phase 6 ")
        self.assertIsNotNone(self.p6b, "no ## Phase 6b heading")
        self.assertIsNotNone(self.p6, "no ## Phase 6 heading")


class TestStatusBlockPlacement(OnboardCase):
    """1 and 2: the status block is Phase 6b step 5; the installer block
    follows it, in the same section."""

    def test_status_block_is_in_6b_after_step_4(self):
        offset, _ = _first_block(self.p6b, STATUS_NEEDLE)
        self.assertIsNotNone(offset, "no Phase 6b block runs auto_memory.py --status")
        step4 = re.search(r"^4\. \*\*", self.p6b, re.M)
        self.assertIsNotNone(step4, "no Phase 6b line starting `4. **`")
        self.assertGreater(offset, step4.start())
        # and not in Phase 6, where the same command would run too early
        self.assertIsNone(_first_block(self.p6, STATUS_NEEDLE)[0])

    def test_installer_block_follows_status_block_in_6b(self):
        status_offset, _ = _first_block(self.p6b, STATUS_NEEDLE)
        install_offset, _ = _first_block(self.p6b, INSTALL_NEEDLE)
        self.assertIsNotNone(status_offset)
        self.assertIsNotNone(install_offset, "no Phase 6b block re-runs the installer")
        self.assertGreater(install_offset, status_offset)


class TestStatusCommandIsDoable(OnboardCase):
    """3: run the documented command, through bash, against fixtures."""

    def _run(self, config, make_projects_dir):
        _, body = _first_block(self.p6b, STATUS_NEEDLE)
        self.assertIsNotNone(body, "no status block to run")
        command = body.replace("<checkout>", REPO)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = os.path.realpath(tmp)
            home = os.path.join(tmp, "home")
            work = os.path.join(tmp, "work")
            os.makedirs(home)
            os.makedirs(work)
            if config.get("workspace_root") == "<work>":
                config = dict(config, workspace_root=work)
            if make_projects_dir:
                slug = re.sub(r"[^A-Za-z0-9]", "-", work)
                os.makedirs(os.path.join(home, ".claude", "projects", slug))
            config_path = os.path.join(tmp, "config.json")
            with open(config_path, "w") as handle:
                json.dump(config, handle)
            # Explicit allowlist: nothing else from the parent leaks in.
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "HOME": home, "USERPROFILE": home,
                   "CLAUDE_TOOLKIT_CONFIG": config_path,
                   "PYTHONDONTWRITEBYTECODE": "1"}
            if os.environ.get("LANG"):
                env["LANG"] = os.environ["LANG"]
            proc = subprocess.run(
                ["bash", "-c", command], env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout.strip().splitlines()[-1])

    def test_set_fixture_reports_set(self):
        result = self._run({"workspace_root": "<work>"}, make_projects_dir=True)
        self.assertEqual(result["action"], "set")

    def test_skip_fixture_reports_skip(self):
        result = self._run({}, make_projects_dir=False)
        self.assertEqual(result["action"], "skip")


class TestEveryActionHasARow(OnboardCase):
    """4: a new action without a doc row fails."""

    def test_each_action_is_a_backticked_token_in_6b(self):
        for action in auto_memory.STATUS_ACTIONS:
            self.assertIn("`%s`" % action, self.p6b,
                          "Phase 6b has no row for action `%s`" % action)


class TestPhase6Restart(OnboardCase):
    """5: Phase 6 gives the terminal route into the work folder."""

    def test_phase_6_has_cd_and_claude_block(self):
        offset, _ = _first_block(self.p6, 'cd "<workspace_root>" && claude')
        self.assertIsNotNone(
            offset, 'no Phase 6 block runs `cd "<workspace_root>" && claude`')


class TestOnboardingMd(unittest.TestCase):
    """6: the restart paragraph names the work folder."""

    def test_verify_paragraph_mentions_work_folder(self):
        text = _read(ONBOARDING_MD)
        launch = re.search(r"^claude\n```", text, re.M)
        self.assertIsNotNone(launch, "no `claude` launch block")
        found = [p for off, p in _paragraphs(text)
                 if off > launch.start() and "/toolkit-core:onboard verify" in p]
        self.assertTrue(found, "no paragraph mentions /toolkit-core:onboard verify")
        self.assertIn("<workspace_root>", found[0])


OTHER_REASONS = ("`no workspace_root configured`",
                 "`workspace_root is not a directory`",
                 "`project key over 200 chars")
OTHER_BRANCH_MARKER = "Any other reason"


class TestReasonBranch(OnboardCase):
    """7: the `skip` bullet has TWO branches, and each says something different.
    Only the wrong-folder reason gets a reopen; every other reason is a setup
    problem that must NOT be sent round a restart, and is fixed from the
    checkout (Phase 1's commands are relative to it, and this session is in the
    work folder). Each branch is asserted on its own half of the bullet: the
    reason text itself contains `workspace_root`, so a check against the whole
    bullet would pass for a bullet that says "always restart"."""

    def _skip_bullet(self):
        match = re.search(r"^ *- `skip`:(.*?)(?=^ *- `|\Z)", self.p6b, re.S | re.M)
        self.assertIsNotNone(match, "no `skip` bullet in Phase 6b")
        return match.group(1)

    def _branches(self):
        bullet = self._skip_bullet()
        self.assertEqual(bullet.count(OTHER_BRANCH_MARKER), 1,
                         "the skip bullet must split once at %r" % OTHER_BRANCH_MARKER)
        return bullet.split(OTHER_BRANCH_MARKER)

    def test_wrong_folder_branch_reopens_in_workspace_root(self):
        restart, _ = self._branches()
        self.assertIn(REASON_TEXT, restart)
        self.assertIn("reopen", restart)
        self.assertIn("`workspace_root`", restart)

    def test_other_reasons_are_fixed_from_the_checkout_without_a_restart(self):
        _, other = self._branches()
        for reason in OTHER_REASONS:
            self.assertIn(reason, other)
        self.assertNotIn(REASON_TEXT, other)
        # Whitespace-normalised: the sentence wraps across lines in the doc.
        self.assertIn("Do not tell them to restart", " ".join(other.split()))
        self.assertIn('`cd "<checkout>"`', other)
        self.assertNotIn("reopen", other)


class TestReadme(unittest.TestCase):
    """8: the README restart paragraph names the work folder."""

    def test_restart_paragraph_mentions_workspace_root(self):
        text = _read(README_MD)
        found = [p for _, p in _paragraphs(text)
                 if p.startswith("Then **restart Claude Code**")]
        self.assertEqual(len(found), 1, "restart paragraph not found exactly once")
        self.assertIn("<your-workspace-root>", found[0])


class TestFencedHeadingsAreQuoted(unittest.TestCase):
    """A `##` line inside a fence is quoted text. `_section` must neither end
    nor start a section on one, and must keep the fenced body (the bash
    blocks this suite reads) in the section that holds it."""

    def test_section_skips_headings_inside_fences(self):
        for name, opener, closer in FENCES:
            with self.subTest(fence=name):
                doc = ("## Alpha\nlive\n%s\n## Beta fake\n%s\ntail\n## Beta\nbody\n"
                       % (opener, closer))
                alpha, offset = _section(doc, "## Alpha")
                self.assertIn("tail", alpha, "a fenced heading ended the section")
                self.assertIn("## Beta fake", alpha, "the fenced body was dropped")
                beta, offset = _section(doc, "## Beta")
                self.assertEqual((beta, offset), ("## Beta\nbody\n", doc.index("## Beta\n")))


if __name__ == "__main__":
    unittest.main()
