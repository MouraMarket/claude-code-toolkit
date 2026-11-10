"""Content checks on the skill-<name> plugins listed in the marketplace, run
against the real repository. Each check also has a synthetic negative case (a
temp copy of a valid skill plugin with one violation), so it can fail."""
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MAX_DESCRIPTION = 1024
MAX_SKILL_LINES = 500

# Skills that are not shipped, so a shipped skill must not route to one. A hit
# is a whole token: not preceded or followed by a letter, digit, underscore or
# hyphen. Private names (an organisation, its bundles) are not listed here:
# this file is public, so the leak gate's private profile catches those.
EXCLUDED = (
    "grill-me", "google-ads-builder", "meta-ads-builder", "brand-term-builder",
    "creative-brief", "meeting-recap", "client-onboarding", "customer-list-formatter",
    "brand-guidelines", "brand-voice", "client-data", "campaign-report",
    "tracking-check", "company-enrichment-lite", "scope-sync", "social-draft",
    "social-ideate", "social-qa", "voice-profile.md", "_clients",
)


def _term_re(term):
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(term) + r"(?![A-Za-z0-9_-])", re.IGNORECASE)


EXCLUDED_RES = [(term, _term_re(term)) for term in EXCLUDED]


def skill_plugins(root):
    """{plugin name: directory} for every skill- plugin the marketplace lists."""
    with open(os.path.join(root, ".claude-plugin", "marketplace.json"), encoding="utf-8") as handle:
        entries = json.load(handle)["plugins"]
    return {e["name"]: os.path.normpath(os.path.join(root, e["source"]))
            for e in entries if e["name"].startswith("skill-")}


def front_matter(text):
    """{key: value} of the top-level scalars in leading YAML front matter.
    Folded (>, >-) and literal (|) blocks and indented continuations are
    joined with single spaces; None when there is no front matter."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields, key = {}, None
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        match = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if match and not line.startswith(" "):
            key, value = match.group(1), match.group(2).strip()
            fields[key] = "" if value[:1] in (">", "|") else value.strip("'\"")
        elif key is not None and line.strip():
            fields[key] = (fields[key] + " " + line.strip()).strip()
    return None  # never closed


def problems_in(plugin_dir):
    """Content problems for one skill plugin directory."""
    problems = []
    skill = os.path.basename(plugin_dir)
    skill_md = os.path.join(plugin_dir, "skills", skill, "SKILL.md")
    try:
        with open(skill_md, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ["%s: SKILL.md missing" % skill]
    fields = front_matter(text)
    if fields is None:
        problems.append("%s: no front matter" % skill)
        fields = {}
    if not fields.get("name"):
        problems.append("%s: front matter has no name" % skill)
    if not fields.get("description"):
        problems.append("%s: front matter has no description" % skill)
    elif len(fields["description"]) > MAX_DESCRIPTION:
        problems.append("%s: description is %d characters, over %d"
                        % (skill, len(fields["description"]), MAX_DESCRIPTION))
    if len(text.splitlines()) > MAX_SKILL_LINES:
        problems.append("%s: SKILL.md is over %d lines" % (skill, MAX_SKILL_LINES))
    for dirpath, _, filenames in os.walk(plugin_dir):
        for filename in sorted(filenames):
            path = os.path.join(dirpath, filename)
            with open(path, encoding="utf-8", errors="replace") as handle:
                body = handle.read()
            for term, pattern in EXCLUDED_RES:
                if pattern.search(body):
                    problems.append("%s: %s mentions excluded term %s"
                                    % (skill, os.path.relpath(path, plugin_dir), term))
    return problems


class RealRepo(unittest.TestCase):
    def test_marketplace_lists_skill_plugins(self):
        self.assertTrue(skill_plugins(REPO), "no skill- plugins in the marketplace")

    def test_every_skill_plugin_is_clean(self):
        for name, plugin_dir in sorted(skill_plugins(REPO).items()):
            self.assertEqual(problems_in(plugin_dir), [], name)


class Synthetic(unittest.TestCase):
    """A valid skill plugin in a temp dir; each test breaks one thing."""

    def setUp(self):
        root = os.path.realpath(tempfile.mkdtemp(prefix="skillplug-"))
        self.addCleanup(shutil.rmtree, root)
        self.dir = os.path.join(root, "demo")
        self.skill_md = os.path.join(self.dir, "skills", "demo", "SKILL.md")
        self.write(self.skill_md, "---\nname: demo\ndescription: Does a demo.\n---\n# Demo\nBody.\n")
        self.write(os.path.join(self.dir, "skills", "demo", "references", "a.md"), "Reference.\n")
        self.write(os.path.join(self.dir, ".claude-plugin", "plugin.json"), '{"name": "skill-demo"}\n')

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def skill(self, front, body="# Demo\nBody.\n"):
        self.write(self.skill_md, "---\n%s---\n%s" % (front, body))

    def assertProblem(self, fragment):
        found = problems_in(self.dir)
        self.assertTrue(any(fragment in p for p in found), "%r not in %r" % (fragment, found))

    def test_valid_is_clean(self):
        self.assertEqual(problems_in(self.dir), [])

    def test_missing_skill_md(self):
        os.remove(self.skill_md)
        self.assertProblem("SKILL.md missing")

    def test_no_front_matter(self):
        self.write(self.skill_md, "# Demo\nname: demo\n")
        self.assertProblem("no front matter")

    def test_no_name(self):
        self.skill("description: Does a demo.\n")
        self.assertProblem("front matter has no name")

    def test_no_description(self):
        self.skill("name: demo\n")
        self.assertProblem("front matter has no description")

    def test_description_at_the_limit_passes(self):
        self.skill("name: demo\ndescription: %s\n" % ("x" * MAX_DESCRIPTION))
        self.assertEqual(problems_in(self.dir), [])

    def test_description_over_the_limit(self):
        self.skill("name: demo\ndescription: %s\n" % ("x" * (MAX_DESCRIPTION + 1)))
        self.assertProblem("over %d" % MAX_DESCRIPTION)

    def test_folded_description_over_the_limit(self):
        # A folded block counts its joined text, not the first line.
        folded = "".join("  %s\n" % ("y" * 100) for _ in range(11))
        self.skill("name: demo\ndescription: >-\n" + folded)
        self.assertProblem("over %d" % MAX_DESCRIPTION)

    def test_skill_md_at_the_limit_passes(self):
        self.skill("name: demo\ndescription: d\n", "x\n" * (MAX_SKILL_LINES - 4))
        self.assertEqual(len(open(self.skill_md, encoding="utf-8").read().splitlines()), MAX_SKILL_LINES)
        self.assertEqual(problems_in(self.dir), [])

    def test_skill_md_over_the_limit(self):
        self.skill("name: demo\ndescription: d\n", "x\n" * (MAX_SKILL_LINES - 3))
        self.assertProblem("over %d lines" % MAX_SKILL_LINES)

    def test_every_excluded_term_is_caught_in_skill_md(self):
        for term in EXCLUDED:
            self.skill("name: demo\ndescription: d\n", "Hand off to %s when needed.\n" % term)
            self.assertProblem("excluded term %s" % term)

    def test_a_client_context_folder_path_is_caught(self):
        # A rewrite that keeps the old per-client context lookup must fail.
        self.skill("name: demo\ndescription: d\n", "Read _clients/<name>/context.md first.\n")
        self.assertProblem("excluded term _clients")

    def test_excluded_term_in_a_reference_file(self):
        self.write(os.path.join(self.dir, "skills", "demo", "references", "a.md"), "See `grill-me`.\n")
        self.assertProblem("references/a.md mentions excluded term grill-me")

    def test_excluded_term_in_the_manifest(self):
        self.write(os.path.join(self.dir, ".claude-plugin", "plugin.json"), '{"d": "use creative-brief"}\n')
        self.assertProblem("plugin.json mentions excluded term creative-brief")

    def test_hyphenated_neighbours_are_not_the_token(self):
        # Hyphen-bounded: a different name that merely contains one is fine.
        self.skill("name: demo\ndescription: d\n",
                   "Use pre-grill-me, brand-voices, upload-files, myclient-data and campaign-reports.\n")
        self.assertEqual(problems_in(self.dir), [])


if __name__ == "__main__":
    unittest.main()
