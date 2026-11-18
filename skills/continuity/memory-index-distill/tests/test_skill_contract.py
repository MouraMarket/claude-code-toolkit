"""Contract tests for the memory-index-distill SKILL.md (S1-S6)."""

import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
SKILL_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(SKILL_DIR)))
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
SIBLING = os.path.join(REPO_ROOT, "skills", "continuity", "memory-distill", "SKILL.md")
SIBLING_REL = "skills/continuity/memory-distill/SKILL.md"

# A pinned copy of the skill's ## Manifest block. A drift between the two is a
# failure, so an edit to the example is a deliberate two-place change.
PLAN_MANIFEST = json.loads(r'''{
  "version": 1,
  "pages": [
    {"file": ".claude/rules/release-notes.md", "bucket": "path",
     "content": "---\npaths:\n  - \"docs/releases/**\"\n---\n\n# Release notes — rules carried from memory\n\n## Release notes are written for users (`release-notes-for-users`)\n- Describe what changed for the user; never cite pull request or ticket numbers.\n",
     "base_sha256": null,
     "probe": {"path": "docs/releases/v2.4/notes.md"}},
    {"file": ".claude/tool-rules/github-api.md", "bucket": "tool",
     "content": "---\ntools:\n  - \"Bash(*api.github.com*)\"\n---\n\n# GitHub API — rules carried from memory\n\n## List endpoints are paginated (`github-api-paginate`)\n- Pass per_page=100 and follow the Link header: one page silently truncates a list.\n",
     "base_sha256": null,
     "probe": {"tool_name": "Bash", "tool_input": {"command": "python3 _probe/github.py api.github.com"}}}
  ],
  "moves": [
    {"memory": "release-notes-for-users.md", "page": ".claude/rules/release-notes.md",
     "topic": "topics/engineering.md",
     "row": "- [Release Notes For Users](release-notes-for-users.md) — release notes describe user-visible change, never PR or ticket numbers",
     "base_sha256": null},
    {"memory": "github-api-paginate.md", "page": ".claude/tool-rules/github-api.md",
     "topic": "topics/engineering.md",
     "row": "- [GitHub API Paginate](github-api-paginate.md) — list endpoints are paginated; follow the Link header or the list is silently cut",
     "base_sha256": null}
  ],
  "behaviour": [
    {"memory": "release-notes-for-users.md",
     "files": {"docs/releases/v2.4/changes.csv": "pr,change\n412,Search is faster on large projects\n415,Exporting an empty report no longer fails\n"},
     "prompt": "Write docs/releases/v2.4/notes.md: a short release note for users from changes.csv.",
     "pass_if": "notes.md describes both changes and cites neither pull request number (412 or 415)."}
  ]
}''')

LEAF = ("---\nname: %s\nmetadata:\n  node_type: memory\n  type: feedback\n---\n\n"
        "Body of %s.\n")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def split_frontmatter(text):
    lines = text.split("\n")
    assert lines[0] == "---", "no frontmatter"
    end = lines.index("---", 1)
    return lines[1:end], "\n".join(lines[end + 1:])


def description_of(text):
    front, _ = split_frontmatter(text)
    start = next(i for i, ln in enumerate(front) if ln.startswith("description:"))
    folded = []
    for ln in front[start + 1:]:
        if ln and not ln.startswith(" "):
            break
        folded.append(ln.strip())
    return " ".join(part for part in folded if part)


def name_of(text):
    front, _ = split_frontmatter(text)
    for ln in front:
        if ln.startswith("name:"):
            return ln.split(":", 1)[1].strip()
    return None


def section(body, heading):
    """Lines under `heading` up to the next `## ` heading."""
    lines = body.split("\n")
    start = lines.index(heading)
    out = []
    for ln in lines[start + 1:]:
        if ln.startswith("## "):
            break
        out.append(ln)
    return out


def parse_steps(body):
    """{n: [lines]} for each numbered step line under ## Steps."""
    steps = {}
    order = []
    current = None
    for ln in section(body, "## Steps"):
        m = re.match(r"^(\d+)\. ", ln)
        if m:
            current = int(m.group(1))
            order.append(current)
            steps[current] = [ln]
        elif current is not None:
            steps[current].append(ln)
    return order, steps


def sub_bullets(lines):
    bullets = []
    for ln in lines[1:]:
        if re.match(r"^\s+- ", ln):
            bullets.append(ln.strip())
        elif bullets and ln.strip():
            bullets[-1] += " " + ln.strip()
    return bullets


class TestSkill(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = read(SKILL)
        cls.body = split_frontmatter(cls.text)[1]

    def test_s1_frontmatter(self):
        self.assertEqual(name_of(self.text), "memory-index-distill")
        desc = description_of(self.text)
        self.assertLessEqual(len(desc), 1024)
        self.assertIn("use memory-distill", desc)
        self.assertIn("MEMORY INDEX NEAR CHARACTER LIMIT", desc)

    def test_s2_steps(self):
        order, steps = parse_steps(self.body)
        self.assertEqual(order, list(range(1, 10)))
        text = {n: "\n".join(lines) for n, lines in steps.items()}
        expected = [(1, "claude-index-distill claim"), (2, "ListAgents"),
                    (2, "SendMessage"), (2, "claude-memory-lint --report"),
                    (6, "claude-index-distill check"), (6, "claude-index-distill prove"),
                    (7, "AskUserQuestion"), (8, "claude-index-distill apply"),
                    (9, "claude-index-distill release")]
        last = (0, -1)
        for step, needle in expected:
            self.assertIn(needle, text[step], "%r missing from step %d" % (needle, step))
            # In the expected order: each needle sits at or after the previous one.
            pos = (step, text[step].index(needle))
            if step == last[0]:
                self.assertGreaterEqual(pos[1], last[1], needle)
            last = pos

    def test_s3_exit_codes(self):
        _, steps = parse_steps(self.body)
        bullets = sub_bullets(steps[8])
        for code in (0, 1, 2, 4, 5, 6):
            self.assertTrue(any(("**%d**" % code) in b.split("→")[0] for b in bullets),
                            "step 8 has no bullet for exit %d" % code)
        phrase = "loads the new layout"
        holders = [b for b in bullets if phrase in b]
        self.assertEqual(len(holders), 1, holders)
        self.assertIn("**4**", holders[0].split("→")[0])
        self.assertNotIn("**5**", holders[0].split("→")[0])
        nine = "\n".join(steps[9])
        for needle in ("Never", "apply", "release"):
            self.assertIn(needle, nine)
        for code in ("1", "2", "5"):
            self.assertRegex(nine, r"\b%s\b" % code)

    def test_s4_manifest_matches_plan_and_validates(self):
        block = re.search(r"## Manifest\n(.*?)(?=\n## )", self.body, re.S).group(1)
        fenced = re.search(r"```json\n(.*?)\n```", block, re.S)
        self.assertIsNotNone(fenced, "no ```json fence under ## Manifest")
        manifest = json.loads(fenced.group(1))
        self.assertEqual(manifest, PLAN_MANIFEST)

        tool_dir = os.path.join(REPO_ROOT, "tools", "index-distill")
        for p in (tool_dir, os.path.join(REPO_ROOT, "lib"),
                  os.path.join(REPO_ROOT, "tools", "memory-lint")):
            if p not in sys.path:
                sys.path.insert(0, p)
        old = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
        with tempfile.TemporaryDirectory() as raw:
            tmp = os.path.realpath(raw)
            home = os.path.join(tmp, "home")
            os.makedirs(home)
            os.environ["HOME"] = os.environ["USERPROFILE"] = home
            try:
                import distill_lib
                ws = os.path.join(tmp, "ws")
                mem = os.path.join(tmp, "mem")
                os.makedirs(os.path.join(ws, ".claude", "rules"))
                os.makedirs(os.path.join(mem, "topics"))
                rows = [m["row"] for m in manifest["moves"]]
                topic = manifest["moves"][0]["topic"]
                with open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8") as fh:
                    fh.write("## Always\n\n%s\n\n## Topics\n\n"
                             "- [Topic](%s) — topic\n" % ("\n".join(rows), topic))
                with open(os.path.join(mem, topic), "w", encoding="utf-8") as fh:
                    fh.write("# Topic\n\n- [Other](../other.md) — other row\n")
                with open(os.path.join(mem, "other.md"), "w", encoding="utf-8") as fh:
                    fh.write((LEAF % ("other", "other")).replace("feedback", "reference"))
                for move in manifest["moves"]:
                    stem = move["memory"][:-3]
                    path = os.path.join(mem, move["memory"])
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(LEAF % (stem, stem))
                    move["base_sha256"] = distill_lib.sha256_file(path)
                self.assertEqual(distill_lib.validate(manifest, mem, ws), [])
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

        page = next(p for p in manifest["pages"] if p["bucket"] == "path")
        globs = re.findall(r'^\s+- "(.*)"$', page["content"], re.M)
        self.assertTrue(globs)
        self.assertTrue(any(fnmatch.fnmatchcase(page["probe"]["path"], g) for g in globs))
        for case in manifest["behaviour"]:
            for key in case["files"]:
                self.assertTrue(any(fnmatch.fnmatchcase(key, g) for g in globs), key)

    def test_s7_first_call_rules_stay_in_index(self):
        _, steps = parse_steps(self.body)
        step3 = "\n".join(steps[3])
        self.assertIn("first", step3)
        self.assertIn("stays `index`", step3)
        self.assertIn("A rule that must govern the **first** matching call", step3)
        block = re.search(r"## Manifest\n(.*?)(?=\n## )", self.body, re.S).group(1)
        manifest = json.loads(re.search(r"```json\n(.*?)\n```", block, re.S).group(1))
        for move in manifest["moves"]:
            self.assertNotIn("approval", move["memory"])
        self.assertIn("from the second call on", self.body)

    def test_s8_draft_path_is_not_guarded(self):
        # Claude Code guards writes under ~/.claude/: a draft there stops at a
        # permission prompt, or fails outright in an unattended run (a live run
        # was refused as a "sensitive file"). The draft lives in the scratchpad
        # or under the current directory.
        _, steps = parse_steps(self.body)
        step4 = "\n".join(steps[4])
        self.assertNotRegex(step4, r"Draft `~/\.claude/")
        self.assertIn("session scratchpad", step4)
        self.assertIn("`index-distill-drafts/` under the current directory", step4)
        self.assertIn("Never under `~/.claude/`", step4)

    def test_s5_size(self):
        with open(SKILL, "rb") as fh:
            raw = fh.read()
        self.assertLess(raw.count(b"\n"), 500)
        self.assertLess(len(raw), 12000)

    def test_s6_sibling_exclusion(self):
        sibling = read(SIBLING)
        self.assertIn("use memory-index-distill", description_of(sibling))
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        try:
            proc = subprocess.run(["git", "show", "HEAD:" + SIBLING_REL], cwd=REPO_ROOT,
                                  env=env, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL)
        except OSError:
            self.skipTest("git unavailable")
        if proc.returncode != 0:
            self.skipTest("git show failed: %s" % proc.stderr.strip())
        self.assertEqual(split_frontmatter(sibling)[1], split_frontmatter(proc.stdout)[1])


if __name__ == "__main__":
    unittest.main()
