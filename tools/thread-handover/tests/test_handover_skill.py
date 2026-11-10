"""Tests that /handover's shell blocks run as written.

The skill is the only writer of handover records, and each block is pasted
into a session verbatim, so a block that no longer compiles, or that registers
a file the sweep will never act on, fails in front of the user. These run the
Python heredocs with CLAUDE_PLUGIN_ROOT substituted and HOME redirected.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(TOOL_DIR))
PLUGIN_ROOT = os.path.join(REPO, "plugin-continuity")
SKILL_DIR = os.path.join(REPO, "skills", "continuity", "handover")
SKILL = os.path.join(SKILL_DIR, "SKILL.md")
REFERENCES = os.path.join(SKILL_DIR, "references")

BLOCK_RE = re.compile(r"python3 - <<'PY'\n(.*?)\n *PY\n", re.S)
PLACEHOLDER = "FULL_PATH_TO_THE_FILE_YOU_JUST_WROTE"


def reference_paths():
    if not os.path.isdir(REFERENCES):
        return []
    return [os.path.join(REFERENCES, name)
            for name in sorted(os.listdir(REFERENCES)) if name.endswith(".md")]


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def blocks():
    found = []
    for path in [SKILL] + reference_paths():
        found += blocks_in(read(path))
    return found


def blocks_in(text):
    found = []
    for body in BLOCK_RE.findall(text):
        # Blocks nested in a list item are indented; strip the common margin.
        lines = body.splitlines()
        margin = min(len(l) - len(l.lstrip()) for l in lines if l.strip())
        found.append("\n".join(l[margin:] for l in lines)
                     .replace("${CLAUDE_PLUGIN_ROOT}", PLUGIN_ROOT))
    return found


def block_containing(marker):
    matches = [b for b in blocks() if marker in b]
    if len(matches) != 1:
        raise AssertionError("%d blocks contain %r" % (len(matches), marker))
    return matches[0]


class TestBlocksCompile(unittest.TestCase):

    def test_there_are_blocks(self):
        # scope, disposition, grace numbers, register, snooze
        self.assertEqual(len(blocks()), 5)

    def test_every_block_compiles(self):
        for body in blocks():
            compile(body, "<handover block>", "exec")



class TestPathSection(unittest.TestCase):

    def test_the_path_section_resolves_the_directory_instead_of_quoting_one(self):
        with open(SKILL, encoding="utf-8") as handle:
            text = handle.read()
        start = text.index("### The path")
        section = text[start:text.index("\n## ", start)]
        self.assertNotIn("~/.claude/handovers", section)
        self.assertIn("Resolve it before you write", section)

class TestRegisterBlock(unittest.TestCase):

    def setUp(self):
        self.home = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.handover_dir = os.path.join(self.home, "handovers")
        os.makedirs(self.handover_dir)
        self.config = os.path.join(self.home, "config.json")
        with open(self.config, "w", encoding="utf-8") as handle:
            json.dump({"thread_handover": {"handover_dir": self.handover_dir,
                                           "sweep_grace_days": 2}}, handle)

    def run_block(self, body):
        env = {"PATH": "/usr/bin:/bin", "HOME": self.home,
               "CLAUDE_TOOLKIT_CONFIG": self.config,
               "CLAUDE_CODE_SESSION_ID": "WRITER"}
        return subprocess.run([sys.executable, "-c", body], env=env, cwd=self.home,
                              capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)

    def index(self):
        path = os.path.join(self.home, ".claude", "thread-handover", "index.json")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def write(self, directory, name):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# handover\n")
        return path

    def register(self, path):
        return self.run_block(block_containing(PLACEHOLDER).replace(PLACEHOLDER, path))

    def test_registers_a_handover_in_the_configured_directory(self):
        path = self.write(self.handover_dir, "2026-08-14-demo--first-handover.md")
        proc = self.register(path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("registered: " + path, proc.stdout)
        records = self.index()
        self.assertEqual([r["path"] for r in records], [path])
        self.assertEqual(records[0]["created_session_id"], "WRITER")

    def test_refuses_a_handover_outside_the_directory(self):
        path = self.write(self.home, "2026-08-14-demo--first-handover.md")
        proc = self.register(path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must live in", proc.stderr)
        self.assertEqual(self.index(), [])

    def test_refuses_a_name_the_sweep_will_not_match(self):
        path = self.write(self.handover_dir, "notes.md")
        proc = self.register(path)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.index(), [])

    def test_grace_block_prints_the_configured_numbers(self):
        proc = self.run_block(block_containing("index_lib.GRACE_DAYS"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("handover_dir: " + self.handover_dir, proc.stdout)
        self.assertIn("days: 2", proc.stdout)


class TestReferences(unittest.TestCase):

    def test_every_named_reference_exists_and_every_reference_is_named(self):
        named = set(re.findall(r"references/([\w-]+\.md)", read(SKILL)))
        present = {os.path.basename(p) for p in reference_paths()}
        self.assertEqual(named, present)

    def test_no_reference_uses_the_plugin_root(self):
        """Claude Code substitutes ${CLAUDE_PLUGIN_ROOT} in the loaded SKILL.md
        text only; a file read through the Read tool gets no substitution, so a
        command that needs it must stay in SKILL.md."""
        for path in reference_paths():
            self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", read(path), path)


# Generated by a throwaway script from `git show 71f2bdd:skills/continuity/
# handover/SKILL.md`: every `#`-heading line outside a code fence, then every
# `**bold span**` (whitespace collapsed to single spaces), de-duplicated in
# first-seen order.
RULES_AT_71F2BDD = (
    '## 0. Establish your scope before gathering anything',
    '### Then say what happens to the worktree',
    '### What leaks, and what to do instead',
    '## 1. Identify the mission, then choose the path',
    '### The mission',
    '### The path',
    '## 2. First, drain everything durable into memory',
    '### The mission backlog lives in the spec, not in this file',
    '### Waiting on a human: the section the spec must have',
    '## 3. Write the document',
    '### First: if you attached a handover, reconcile with it',
    '### Then: the sections',
    '### Never assert what the user has not said',
    '## 4. Register it in the index',
    '### How the next session picks it up',
    '## 5. Report back',
    '**This template is the single source of truth for handover format.**',
    '**Run this in the main loop.**',
    '**write the handover without git facts and say why**',
    '**Read the `source` line before you trust the checkout.**',
    '**guess**',
    "**do not report git facts as this thread's work**",
    '**This block skips a dead claim; it must never delete one.**',
    '**Skip it entirely if you are unsure**',
    '**more**',
    '**detour**',
    '**The mission is what the FIRST thread in this chain set out to deliver.**',
    '**You attached a handover?**',
    '**Inherit it verbatim.**',
    '**No handover attached?**',
    '**The mission shipped this thread?**',
    '**and a decider**',
    "**`mission_status: shipped` requires the spec's `## Waiting on a human` section to exist and carry no outstanding row.**",
    "**The same test gates the spec's own `status:`, and that is the flip that actually loses rows.**",
    '**A missing section fails this check; it does not pass it.**',
    '**before**',
    '**Resolve it before you write the file**',
    '**inherited, not re-derived**',
    '**this thread**',
    '**must**',
    '**Never omit the leg**',
    '**Do this before writing a single line of the handover.**',
    '**would this still be true next month?**',
    '**A task discovered this thread that belongs to the mission and is not done**',
    "**the mission spec's Backlog**",
    "**The mission's ordered backlog itself**",
    '**the mission spec**',
    '**the handover**',
    '**now**',
    '**update that memory**',
    '**Where the spec is:**',
    "**That flip takes the same test as §1's `mission_status`: `## Waiting on a human` must exist and carry no outstanding row.**",
    '**If the mission has no spec, write one now.**',
    "**This is the mission's second handover.**",
    '**The build produced work someone must do later**',
    '**On a multi-session build, that spec also needs a `## MVP` directly above its `## Backlog`**',
    '**Trigger 2 is about the mission, not this thread.**',
    '**A copy committed to a feature branch is not the mission spec**',
    '**Every mission spec gets a `## Waiting on a human` section, and it is not part of the backlog.**',
    '**the step, who owns it, what silently breaks if it is skipped, and its status.**',
    '**The status column is what makes the section checkable, so do not drop it.**',
    '**The section passes when every row is marked done, or explicitly decided as > no-action. It fails while any row is outstanding, or unmarked, which is not > the same as done.**',
    '**Every row fails silently by construction.**',
    "**A plan's ops appendix is not a home for it.**",
    '**It keeps the mission honest.**',
    '**Rows leave this section only when the human confirms they are done**',
    '**Skip this only if this thread attached no handover.**',
    '**and its mission front matter**',
    '**every**',
    '**State**',
    '**Open work**',
    '**Migrating an item to the spec is the point, not extra work.**',
    '**The mission is itself an item to reconcile.**',
    '**This step is the only thing that carries a non-mission item forward.**',
    "**Read the predecessor's open items, not its narrative.**",
    "**This does not license reporting other sessions' work as yours.**",
    '**Epistemic header**',
    '**any claim about what the user has or has not said must be checked against memory before it is acted on.**',
    '**Mission: what this chain is for, and whether it moved.**',
    '**who decided**',
    '**A thread that spent every token on discovered side work still writes "the mission did not move" here.**',
    '**Name the checkout §0 resolved, then the branch and commit**',
    '**Immediate next step**',
    '**Say whether it serves the mission or the detour.**',
    '**Standing instructions, as pointers, not content.**',
    '**Credentials, paths, and gotchas**',
    "**Open work: this thread's leftovers only, in priority order.**",
    '**Corrected wrong beliefs**',
    '**Escape hatch note**',
    '**moved into `<handover_dir>/archive/handovers/`**',
    '**Say that the mission spec is NOT swept by this.**',
    '**Resolve the real numbers first; never write the defaults.**',
    '**Banned:**',
    '**Write instead:**',
    '**Do not pad it.**',
    '**A handover you write but do not register is invisible.**',
    '**not**',
    '**both**',
    '**and**',
    '**Retiring means moving, not deleting**',
    "**Run every one of this skill's shell blocks in the main loop; never delegate them to a subagent.**",
    '**a single copy-pasteable line, on its own, in a fenced block**',
    '**full path**',
    '**Do not tell them to attach it with `@`.**',
    '**basename appearing in a real human prompt**',
    '**new**',
    '**Do not end the session yourself**',
)


class TestMovedRulesInventory(unittest.TestCase):
    """RULES_AT_71F2BDD is the token inventory for the trim that moved rules into references/: each
    heading and bold rule the skill carried before rules moved into
    references/. Every entry must still appear in SKILL.md or a reference
    file. A later edit may remove an entry deliberately; delete it here too."""

    def test_every_rule_is_in_the_skill_or_a_reference(self):
        corpus = " ".join(" ".join(read(p).split())
                          for p in [SKILL] + reference_paths())
        missing = [r for r in RULES_AT_71F2BDD if r not in corpus]
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
