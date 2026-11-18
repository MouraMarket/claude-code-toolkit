#!/usr/bin/env python3
"""The public `/onboard` skill keeps the literals the public build depends on.

Each test pins one property of `skills/core/onboard/SKILL.md` (or its
`references/`): the frontmatter limits, the macOS-only stop, that the one
`--write` block belongs to the no-configuration branch, that every builder
profile list keeps `continuity`, the Workflow-setting offer, the preview
wording, the machine-wide git-hooks warning, the TOOLING.md hand-off, the
checkout lookup, the close's commands bullet, and that nothing the agency
wrote for itself leaks into the public skill.

Sections are located from parsed `##` headings and fenced ```bash blocks, never
a whole-document substring search (L26's leak check is whole-file by
definition). Phrase checks run on whitespace-flattened text. Where a check
could pass vacuously, a positive control feeds it a fixture that must trip it.
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(HERE))
SKILL_DIR = os.path.join(REPO_ROOT, "skills", "core", "onboard")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")
REFERENCES_DIR = os.path.join(SKILL_DIR, "references")
INSTALL_SH = os.path.join(REPO_ROOT, "install.sh")

sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, os.path.join(REPO_ROOT, "lib"))
import check_bundles  # noqa: E402
import claude_imports  # noqa: E402

_FENCE_RE = re.compile(r"```bash\n(.*?)```", re.S)
_HEADING_RE = re.compile(r"^(##\s+.*)$", re.M)
_SUBHEADING_RE = re.compile(r"^#{2,3}\s.*$", re.M)
UNINSTALL_PHRASE = "is no longer selected (would uninstall)"
OUTSIDE_PHRASE = "enabled outside your profiles — left as is"

# (name, opener, closer); the nested pair defeats a scanner that toggles on any ```.
FENCES = (("backtick", "```", "```"), ("tilde", "~~~", "~~~"),
          ("nested", "````\n```", "```\n````"))


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _flat(text):
    return " ".join(text.split())


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


def _blocks(text):
    """[(offset, body)] for every fenced bash block in `text`."""
    return [(m.start(), m.group(1)) for m in _FENCE_RE.finditer(text)]


def _paragraphs(text):
    """[(offset, paragraph)]: blank-line-separated chunks of `text`."""
    out = []
    offset = 0
    for chunk in text.split("\n\n"):
        out.append((offset, chunk))
        offset += len(chunk) + 2
    return out


def _paragraph_starting(text, prefix):
    """(offset, paragraph) of the paragraph whose first characters are `prefix`."""
    for offset, chunk in _paragraphs(text):
        if chunk.lstrip("\n").startswith(prefix):
            return offset, chunk
    raise AssertionError("no paragraph starting %r" % prefix)


def _bullet(section, phrase):
    """(text, offset) of the list bullet (wrapped lines included) holding `phrase`."""
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


# ---------------------------------------------------------------- L9 helper

_PROFILES_CSV_RE = re.compile(r"--profiles\s+([A-Za-z<>,_-]+)")
_NEEDS_CONTINUITY = {"builder", "maintainer", "mac"}


def _csv_lists_missing_continuity(text):
    """Every `--profiles <csv>` list that names builder, maintainer or mac but
    not continuity (a list REPLACES the default set, so dropping it is silent)."""
    bad = []
    for match in _PROFILES_CSV_RE.finditer(text):
        names = {name.strip().lower() for name in match.group(1).split(",")}
        if names & _NEEDS_CONTINUITY and "continuity" not in names:
            bad.append(match.group(0))
    return bad


# ---------------------------------------------------------------- L26 helpers

# Generic shapes only. The source's organization names are private terms, so
# naming them here would leak them; the private leak gate checks every
# exported tree for those instead.
_FORBIDDEN_CASE_SENSITIVE = (
    "MINGW", "MSYS", "CYGWIN", "WSL",
    "windows-smoke", "setup-repos", "Ignition", "Netlify", "SEMrush",
    "railway", "gcloud",
)
_FORBIDDEN_CASE_INSENSITIVE = ("cowork", "clickup")
_REFERENCE_NAME_RE = re.compile(r"references/([A-Za-z0-9_.-]+\.md)")


def _forbidden_hits(text):
    hits = [term for term in _FORBIDDEN_CASE_SENSITIVE if term in text]
    lowered = text.lower()
    hits += [term for term in _FORBIDDEN_CASE_INSENSITIVE if term in lowered]
    return hits


def _reference_problems(skill_text, files_on_disk):
    """(named_but_missing, on_disk_but_unnamed), both sorted."""
    named = set(_REFERENCE_NAME_RE.findall(skill_text))
    on_disk = set(files_on_disk)
    return sorted(named - on_disk), sorted(on_disk - named)


def _skill_dir_texts():
    """{relative path: text} for SKILL.md and every references/*.md."""
    texts = {"SKILL.md": _read(SKILL_MD)}
    for name in sorted(os.listdir(REFERENCES_DIR)):
        if name.endswith(".md"):
            texts["references/" + name] = _read(os.path.join(REFERENCES_DIR, name))
    return texts


class TestOnboardPublicSkill(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _read(SKILL_MD)

    # L1 ----------------------------------------------------------------
    def test_frontmatter_name_description_and_hint(self):
        lines = self.text.split("\n")
        self.assertEqual(lines[0].strip(), "---")
        end = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
        front = lines[1:end]
        self.assertIn("name: onboard", [line.strip() for line in front])
        hints = [line for line in front if line.startswith("argument-hint:")]
        self.assertEqual(len(hints), 1)
        self.assertIn("verify", hints[0])
        description = check_bundles.read_description(self.text)
        self.assertIsNotNone(description, "description not readable by check_bundles")
        self.assertLessEqual(len(description), 1024)

    def test_check_bundles_accepts_the_skill_dir(self):
        path = os.path.join(REPO_ROOT, "skills", "core", "onboard")
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            code = check_bundles.main([path])
        self.assertEqual(code, 0, captured.getvalue())

    # L4 ----------------------------------------------------------------
    def test_phase_0_stops_off_darwin_and_treats_gh_as_optional(self):
        phase = _section(self.text, "## Phase 0")
        blocks = [body for _, body in _blocks(phase)]
        self.assertTrue(any("uname -s" in body for body in blocks))
        bullet, _ = _bullet(phase, "is not `Darwin`")
        self.assertIsNotNone(bullet, "no bullet about a non-Darwin host")
        flat = _flat(bullet)
        self.assertIn("uname -s", flat)
        self.assertIn("macOS-only", flat)
        self.assertIn("stop", flat)
        gh_at = phase.index("`gh`")
        self.assertIn("optional", _flat(phase[gh_at:gh_at + 200]))
        self.assertTrue(any("claude-setup-audit snapshot" in body for body in blocks))

    # L6 ----------------------------------------------------------------
    def test_the_only_write_block_belongs_to_the_no_configuration_branch(self):
        phase = _section(self.text, "## Phase 1 ")
        offset, _ = _paragraph_starting(phase, "**No configuration yet**")
        after = [body for pos, body in _blocks(phase) if pos > offset]
        self.assertTrue(after, "no bash block after the No configuration paragraph")
        self.assertIn("--write", after[0])
        write_blocks = [body for _, body in _blocks(self.text) if "--write" in body]
        self.assertEqual(write_blocks, [after[0]],
                         "--write must appear in exactly one bash block, the one "
                         "right after `**No configuration yet**`")

    def test_an_existing_configuration_is_never_rewritten(self):
        phase = _section(self.text, "## Phase 1 ")
        start, _ = _paragraph_starting(phase, "**A configuration already exists**")
        nxt = re.search(r"\n\n(?:\*\*|#)", phase[start + 2:])
        span = phase[start:start + 2 + nxt.start()] if nxt else phase[start:]
        self.assertFalse(any("--write" in body for _, body in _blocks(span)))
        self.assertRegex(_flat(span), r"--update`? [^.]{0,40}refuses `--workspace-root`")
        self.assertNotIn("--specs-dir", self.text)
        flat = _flat(span)
        self.assertIn("never run the `--write` block", flat)
        self.assertRegex(flat, r"printed `None`.{0,120}stop and get help")

    def test_the_existing_config_probe_tells_absent_from_incomplete(self):
        """Run Phase 1's first block under a scratch HOME. A file with no
        workspace_root must not read like no file at all, or the --write
        branch replaces it (Merge Review finding, 2026-10-23)."""
        phase = _section(self.text, "## Phase 1 ")
        probe = _blocks(phase)[0][1]
        self.assertIn("config.json", probe)
        with tempfile.TemporaryDirectory() as home:
            def run():
                result = subprocess.run(
                    ["bash", "-c", probe], cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                    env={"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    capture_output=True, text=True, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            self.assertEqual(run(), "NO CONFIG")
            config_dir = os.path.join(home, ".config", "claude-code-toolkit")
            os.makedirs(config_dir)
            config = os.path.join(config_dir, "config.json")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump({"owner_name": "Sam", "team_policy": {"x": 1}}, handle)
            self.assertEqual(run(), "None")
            with open(config, "w", encoding="utf-8") as handle:
                json.dump({"owner_name": "Sam", "workspace_root": "/synthetic/work"}, handle)
            self.assertEqual(run(), "/synthetic/work")

    def test_the_folder_promise_does_not_claim_a_wall(self):
        """The guard blocks known delete commands; it is not a filesystem
        boundary (an interpreter's os.remove passes), so the promise says so."""
        phase = _flat(_section(self.text, "## Phase 1 "))
        self.assertNotRegex(phase, r"outside it, deleting anything")
        self.assertIn("not a wall around your files", phase)

    # L9 ----------------------------------------------------------------
    def test_every_builder_profile_list_keeps_continuity(self):
        self.assertTrue(_PROFILES_CSV_RE.findall(self.text), "no --profiles list found")
        self.assertEqual(_csv_lists_missing_continuity(self.text), [])

    def test_the_csv_check_flags_a_list_without_continuity(self):
        self.assertEqual(_csv_lists_missing_continuity("--profiles core,builder"),
                         ["--profiles core,builder"])
        self.assertEqual(_csv_lists_missing_continuity("--profiles core,continuity,builder"), [])
        self.assertEqual(_csv_lists_missing_continuity("--profiles core,maintainer"),
                         ["--profiles core,maintainer"])
        self.assertEqual(_csv_lists_missing_continuity("--profiles core,mac"),
                         ["--profiles core,mac"])

    # L10 ---------------------------------------------------------------
    def test_the_workflow_setting_offer(self):
        phase = _section(self.text, "## Phase 2")
        profiles_at = phase.index("### Profiles")
        tier2_at = phase.index("**Tier 2")
        self.assertLess(profiles_at, tier2_at)
        # The offer sits inside `### Profiles`, before the Tier 2 paragraph.
        profiles = phase[profiles_at:tier2_at]
        offer_at, offer = _paragraph_starting(profiles, "**The Workflow setting")
        flat = _flat(offer)
        trigger = (r"`Workflow` tool.{0,80}\bor\b.{0,80}build\.enable_workflows: true"
                   r"|build\.enable_workflows: true`?,? or .{0,80}`Workflow` tool")
        self.assertRegex(flat, trigger)
        self.assertIn("every session", flat)
        self.assertIn("works without it", flat)
        set_blocks = [(pos, body) for pos, body in _blocks(profiles)
                      if "--set build.enable_workflows=true" in body and pos > offer_at]
        self.assertTrue(set_blocks, "no --set build.enable_workflows=true block after the offer")
        managed_at, managed = _paragraph_starting(profiles, "When the config already turns it on")
        self.assertGreater(managed_at, set_blocks[0][0])
        managed_flat = _flat(managed)
        for needed in ("keep it", "turn it off", "build.enable_workflows=false", "./install.sh"):
            self.assertIn(needed, managed_flat)
        self.assertFalse(any("settings.json" in body for _, body in _blocks(self.text)),
                         "no bash block may touch settings.json")

    # L12 ---------------------------------------------------------------
    def test_the_preview_changes_no_setup_and_the_install_script_prints_its_lines(self):
        phase = _section(self.text, "## Phase 2")
        preview = _subsection(phase, "### Preview first")
        self.assertTrue(any(body.strip() == "./install.sh --check"
                            for _, body in _blocks(preview)))
        flat = _flat(preview)
        self.assertIn("changes no setup", flat)
        self.assertIn("Tests", flat)
        # A finished preview is told apart by install.sh's own last lines, never
        # the `Tests` heading, which prints before any suite runs.
        install_text = _read(INSTALL_SH)
        markers = re.findall(r"`([^`]+)`", flat[flat.index("Trust it only when"):])[:4]
        self.assertEqual(markers, ["Check complete. Re-run without --check to apply.",
                                   "Some suites failed", "Hooks are NOT protected",
                                   "Required components failed:"])
        for marker in markers:
            self.assertIn(marker, install_text)
        self.assertIn(UNINSTALL_PHRASE, preview)
        self.assertIn(OUTSIDE_PHRASE, preview)
        self.assertIn("claude plugin disable <plugin>@claude-code-toolkit", preview)
        install = _read(INSTALL_SH)
        self.assertIn(UNINSTALL_PHRASE, install)
        self.assertIn(OUTSIDE_PHRASE, install)
        bullet, bullet_at = _bullet(phase, UNINSTALL_PHRASE)
        self.assertIsNotNone(bullet)
        bullet_flat = _flat(bullet)
        for needed in ("let it go", "keep", "re-run the `--profiles` command", "then the preview"):
            self.assertIn(needed, bullet_flat)
        self.assertLess(bullet_at, phase.index("### Now install"))

    def test_no_line_claims_the_preview_writes_nothing(self):
        self.assertNotIn("writes nothing", self.text.lower())

    # L14 ---------------------------------------------------------------
    def test_the_machine_wide_hooks_warning_comes_before_setup_git(self):
        phase = _section(self.text, "## Phase 3")
        warning_at = None
        for offset, chunk in _paragraphs(phase):
            lowered = _flat(chunk).lower()
            if "core.hookspath" in lowered and "secret scanning" in lowered:
                warning_at = offset
                break
        self.assertIsNotNone(warning_at, "no paragraph naming core.hooksPath and secret scanning")
        script_at = [pos for pos, body in _blocks(phase) if "scripts/setup-git.sh" in body]
        self.assertTrue(script_at, "no bash block runs scripts/setup-git.sh")
        self.assertLess(warning_at, script_at[0])
        self.assertNotIn("setup-repos", self.text)

    # L16 ---------------------------------------------------------------
    def test_tooling_map_handoff(self):
        phase = _section(self.text, "## Phase 5b")
        self.assertTrue(any(
            'cp -n TOOLING.md.template "<workspace_root>/TOOLING.md"' in body
            for _, body in _blocks(phase)))
        flat = _flat(phase)
        self.assertIn("references/tooling-map.md", flat)
        self.assertRegex(
            flat, r"digest.{0,60}notice.{0,40}every session.{0,80}map exists.{0,40}"
                  r"`tooling_digest\.enabled` is false")

    # L18 ---------------------------------------------------------------
    def test_the_checkout_comes_from_the_marketplace_registration(self):
        phase = _section(self.text, "## Phase 6b")
        step = re.search(r"^1\. .*?(?=^2\. )", phase, re.S | re.M)
        self.assertIsNotNone(step, "no step 1")
        blocks = [body for _, body in _blocks(step.group(0))]
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        self.assertIn("claude plugin marketplace list --json", block)
        self.assertRegex(block, r'get\("name"\)\s*==\s*"claude-code-toolkit"')
        self.assertRegex(block, r'get\("source"\)\s*==\s*"directory"')

    # L25 ---------------------------------------------------------------
    def test_the_close_names_each_command_with_its_profile(self):
        phase = _section(self.text, "## Phase 6b")
        close = _subsection(phase, "### Close")
        match = re.search(r"^- \*\*[^*]*[Cc]ommands worth knowing\*\*.*?(?=^- \*\*)",
                          close, re.S | re.M)
        self.assertIsNotNone(match, "no commands-worth-knowing bullet")
        flat = _flat(match.group(0))
        self.assertIn("lib/profiles.py selected", flat)
        self.assertRegex(flat, r"`/handover`[^;]*`continuity`")
        self.assertRegex(flat, r"`/build`[^.;]*`builder`")
        self.assertIn("ONBOARDING.md", _flat(close))

    # L26 ---------------------------------------------------------------
    def test_no_agency_term_in_the_skill_or_its_references(self):
        for rel, text in _skill_dir_texts().items():
            self.assertEqual(_forbidden_hits(text), [], rel)

    def test_the_forbidden_term_check_catches_each_term(self):
        for term in _FORBIDDEN_CASE_SENSITIVE:
            self.assertEqual(_forbidden_hits("see %s here" % term), [term], term)
        self.assertEqual(_forbidden_hits("a Cowork seat"), ["cowork"])
        self.assertEqual(_forbidden_hits("open ClickUp"), ["clickup"])
        self.assertEqual(_forbidden_hits("a clean line about hooks"), [])

    def test_every_named_reference_exists_and_every_reference_is_named(self):
        files = [n for n in os.listdir(REFERENCES_DIR)]
        missing, unnamed = _reference_problems(self.text, files)
        self.assertEqual(missing, [])
        self.assertEqual(unnamed, [])
        self.assertTrue(files, "references/ is empty")

    def test_the_reference_check_catches_both_directions(self):
        missing, unnamed = _reference_problems("read `references/ghost.md` first", [])
        self.assertEqual(missing, ["ghost.md"])
        self.assertEqual(unnamed, [])
        missing, unnamed = _reference_problems("nothing named", ["extra.md"])
        self.assertEqual(missing, [])
        self.assertEqual(unnamed, ["extra.md"])
        self.assertEqual(_reference_problems("`references/a.md`", ["a.md"]), ([], []))


# Row 74 (28b F-SEC): two verify summaries said secrets were blocked while
# setup-git.sh had been declined and --check said NOT active. The source was a
# Tier 1 line listing "stop a secret being saved into code" as always on.
_SECRET_ON_CLAIM = re.compile(
    r"secret[^.]*\balways on\b|\balways on\b[^.]*secret"
    r"|\b(?:stops?|blocks?)\b[^.]*\bsecrets?\b(?![^.]*\bunless\b)", re.I)


def _secret_on_claims(text):
    """Sentences that call secret scanning on unconditionally."""
    flat = _flat(text)
    return [s for s in re.split(r"(?<=[.!?])\s+", flat) if _SECRET_ON_CLAIM.search(s)]


class TestSecretScanningIsNeverClaimedOn(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.text = _read(SKILL_MD)

    def test_the_old_tier1_line_trips_the_check(self):
        """Positive control: the shipped 0766077e wording must be caught."""
        old = ("block genuinely dangerous commands, keep Claude working in the right copy "
               "of a project, and stop a secret being saved into code. They are always on: "
               "they make it safe to let Claude work unprompted.")
        self.assertTrue(_secret_on_claims(old.replace("stop a", "always on: stop a")))
        self.assertTrue(_secret_on_claims("It blocks secrets from being saved into code."))
        self.assertTrue(_secret_on_claims("It stops secrets from being saved into code."))

    def test_no_sentence_claims_secret_scanning_is_on(self):
        self.assertEqual(_secret_on_claims(self.text), [])

    def test_tier1_says_scanning_is_off_until_setup_git(self):
        tier1 = _flat(_paragraph_starting(_section(self.text, "## Phase 2"),
                                          "**Tier 1")[1])
        self.assertNotIn("stop a secret", tier1)
        self.assertIn("off until `setup-git.sh` runs", tier1)

    def test_verify_reports_scanning_from_the_probe(self):
        verify = _section(self.text, "## Phase 6b")
        probes = [b for _, b in _blocks(verify) if "lib/secret_scan_status.py" in b]
        self.assertEqual(len(probes), 1, "Phase 6b needs the secret-scan probe block")
        self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, "lib", "secret_scan_status.py")))
        flat = _flat(verify)
        self.assertIn("Only `active` means commits are scanned", flat)
        self.assertIn("secret scanning is off", flat)

    def test_close_names_scanning_only_when_active(self):
        close = _subsection(_section(self.text, "## Phase 6b"), "### Close")
        bullet = _flat(_bullet(close, "What's now watching out")[0])
        self.assertIn("only if `lib/secret_scan_status.py` printed `active`", bullet)
        self.assertIn("otherwise say it is off", bullet)


class TestWorkFolderRecommendation(unittest.TestCase):
    """Row 73 (28b F-D1a): the model labelled the toolkit-only `~/My Tools`
    "(Recommended)". The skill recommends only the guess, which skips such a
    folder (behaviour pinned in test_setup_config.TestGuessWorkspaceRoot)."""

    def test_phase1_recommends_only_the_printed_guess(self):
        phase1 = _flat(_section(_read(SKILL_MD), "## Phase 1 "))
        self.assertIn("Recommend only the printed guess, never a folder of your own choosing",
                      phase1)
        self.assertIn("An empty guess means no recommendation", phase1)
        self.assertIn("setup_config.guess_workspace_root", phase1)


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



class TestOnboardingConduct(unittest.TestCase):
    """Row 76 (28b findings F-D1c/F-D1d): what the onboarding session promised
    and did, against what the skill says."""

    @classmethod
    def setUpClass(cls):
        with open(SKILL_MD, encoding="utf-8") as fh:
            cls.skill = fh.read()
        with open(os.path.join(REPO_ROOT, "ONBOARDING.md"), encoding="utf-8") as fh:
            cls.onboarding = fh.read()

    def test_the_time_promise_is_not_ten_minutes(self):
        """The measured install took ~1.5 h (three suite sweeps); "about ten
        minutes" set a stranger up to think it had hung."""
        for label, text in (("SKILL.md", self.skill), ("ONBOARDING.md", self.onboarding)):
            with self.subTest(doc=label):
                self.assertNotRegex(text, r"(?i)\b(ten|10) minutes\b")
                self.assertRegex(text, r"an hour to an hour and a half")

    def test_the_workflow_question_carries_no_recommendation(self):
        """The 28b session tagged "Leave it off (Recommended)" on its own and
        the operator submitted it inside a four-tab form."""
        start = self.skill.index("**The Workflow setting, builder only.**")
        # Through the next bold-led block (Tier 2): the offer spans prose,
        # the turn-on command and the keep/turn-off case, and a label
        # anywhere in it is the defect. Whitespace-normalised, since the
        # prose re-wraps.
        section = " ".join(
            self.skill[start:self.skill.index("\n**Tier 2", start)].split())
        self.assertIn("tag neither", section)
        self.assertIn("never as one tab of a multi-question form", section)
        self.assertNotIn("(Recommended)", section)
        # The general consent rule must not still promise the Workflow
        # setting a one-keypress recommended answer (Codex, row 81 PR C).
        rule = self.skill[self.skill.index("**The deliberate exception is the consent prompts"):]
        rule = rule[:rule.index("\n\n")]
        self.assertIn("the Workflow setting has no recommended answer",
                      " ".join(rule.split()))

    def test_both_template_copies_forbid_writing_the_file_by_hand(self):
        """The 28b session wrote ~/Work/CLAUDE.md and TOOLING.md directly
        because the folder was empty."""
        for heading in ("## Phase 4", "## Phase 5b"):
            with self.subTest(phase=heading):
                start = self.skill.index(heading)
                body = self.skill[start:self.skill.index("\n## ", start + 1)]
                self.assertIn("cp -n ", body)
                self.assertIn("never hand-write the file", body)


if __name__ == "__main__":
    unittest.main()
