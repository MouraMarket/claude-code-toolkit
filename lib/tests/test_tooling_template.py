"""TOOLING.md.template must not ship access claims as verified, or anyone's accounts.

Run: python3 lib/tests/test_tooling_template.py
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = (ROOT / "TOOLING.md.template").read_text(encoding="utf-8")

START = "<!-- DIGEST:START -->"
END = "<!-- DIGEST:END -->"
DEFAULT_MAX_LINES = 60  # tooling_digest.max_lines default, which the template quotes


def digest_text():
    assert TEMPLATE.count(START) == 1, "DIGEST:START must appear exactly once"
    assert TEMPLATE.count(END) == 1, "DIGEST:END must appear exactly once"
    return TEMPLATE.split(START, 1)[1].split(END, 1)[0]


def entry(name):
    """The digest line starting with `name` plus its indented continuation lines."""
    lines = digest_text().splitlines()
    out = []
    for i, line in enumerate(lines):
        if line.startswith(name):
            out.append(line)
            for cont in lines[i + 1:]:
                if cont[:1].isspace() and cont.strip():
                    out.append(cont)
                else:
                    break
            break
    return "\n".join(out)


def section(n):
    """Text from the line starting `## n.` up to the next line that is exactly `---`."""
    lines = TEMPLATE.splitlines()
    out = []
    started = False
    for line in lines:
        if not started:
            if line.startswith(f"## {n}."):
                started = True
                out.append(line)
        elif line == "---":
            break
        else:
            out.append(line)
    return "\n".join(out)


class ToolingTemplateTest(unittest.TestCase):
    def test_1_secrets_marked_unverified(self):
        self.assertIn("UNVERIFIED", entry("Secrets"),
                      "digest Secrets entry would ship access as verified")

    def test_2_digest_has_no_api_over_mcp_default(self):
        self.assertNotIn("Prefer API over MCP", digest_text(),
                         "digest would ship an API-over-MCP default")

    def test_3_platforms_has_no_checkmark(self):
        self.assertIn("## 2.", section(2), "section parser misses section 2; test 3 is vacuous")
        self.assertNotIn("✅", section(2),
                         "section 2 would ship a verified-access checkmark")

    def test_4_rule_is_per_task(self):
        sec = section(2)
        self.assertNotIn("supersedes", sec,
                         "section 2 would ship the API-supersedes-MCP rule")
        self.assertIn("decide per task", sec,
                      "section 2 is missing the per-task API/MCP rule")

    def test_5_ships_no_organization_identity(self):
        self.assertEqual(re.findall(r"[\w.-]+@[\w-]+\.iam\.gserviceaccount\.com", TEMPLATE), [],
                         "a real service-account identity would ship")
        for marker in ("shared-infrastructure", "service account", "repos/automations",
                       "1password.com", "MCC"):
            self.assertNotIn(marker, TEMPLATE, "%s would ship" % marker)

    def test_6_parser_reaches_a_continuation_line(self):
        self.assertIn("environment variables", entry("Secrets"),
                      "entry parser misses continuation lines; test 1 is vacuous")

    def test_7_digest_block_fits_its_own_cap(self):
        self.assertLessEqual(len(digest_text().strip("\n").splitlines()), DEFAULT_MAX_LINES)
        self.assertIn("`tooling_digest.max_lines`, %d by" % DEFAULT_MAX_LINES, TEMPLATE)

    def test_9_layer_section_survives_the_plugin_move(self):
        sec = section(6)
        self.assertIn("## 6.", sec, "section parser misses section 6; test 9 is vacuous")
        self.assertNotIn("~/.claude/{commands,agents,skills}", sec,
                         "section 6 names a per-component symlink layout the plugin replaces")
        self.assertIn("`toolkit-*` plugins", sec, "section 6 omits the plugins that carry the hooks")
        self.assertIn("`claude-code-toolkit`", sec, "section 6 omits the marketplace entry")

    def test_10_canonical_docs_point_at_the_toolkit(self):
        # Retired private names are the leak gate's job: naming them here
        # would ship them.
        sec = section(7)
        self.assertIn("## 7.", sec, "section parser misses section 7; test 10 is vacuous")
        self.assertIn("toolkit checkout's `CLAUDE.md`", sec)


if __name__ == "__main__":
    unittest.main()
