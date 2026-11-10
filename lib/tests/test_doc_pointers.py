"""The repository's own instruction files carry what the code points at.

Code comments send a reader to `docs/decisions.md` ("Design decisions about
this file ... are recorded in docs/decisions.md", or a quoted entry title), and
review lenses send one to a named `CLAUDE.md` section. A pointer into a file
that no longer says the thing reads exactly like a pointer that does: nothing
fails, and the reader who obeys it finds nothing and simplifies the code it was
defending. Every pointer here is read FROM the code, so a new pointer is
checked without anyone listing it.

Run: python3 lib/tests/test_doc_pointers.py
"""
import glob
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "tools", "merge-brief"))

import test_suite_discovery as discovery  # noqa: E402
import merge_brief_lib  # noqa: E402

DECISIONS = "docs/decisions.md"
DOCS = ("CLAUDE.md", "AGENTS.md", DECISIONS)
INDEX_POINTER = "recorded in docs/decisions.md"
# `docs/decisions.md`, "Some title" / see docs/decisions.md ("some title")
QUOTED_TITLE_RE = re.compile(r"docs/decisions\.md`?\s*[,(]?\s*\(?\"([^\"]{4,120})\"")
# The section a review lens tells a reader to open by name.
LENS_SECTION_RE = re.compile(r"`CLAUDE\.md` \"([^\"]+)\" section")
LENS_FILES = ("tools/build-lens/design-review.md", "tools/codex-review/design-brief.md")
# A backticked repo-relative path: segments of path characters, at least one `/`
# or a file suffix. Globs allowed; placeholders (`<x>`), `~`, `$` are not paths.
PATH_SPAN_RE = re.compile(r"`([A-Za-z0-9_.*-]+(?:/[A-Za-z0-9_.*-]+)*/?)`")


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as fh:
        return fh.read()


def headings(text):
    """Markdown headings outside fenced blocks, as plain text."""
    out, fenced = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            m = re.match(r"#{1,6}\s+(.*?)\s*#*\s*$", line)
            if m:
                out.append(m.group(1).replace("`", ""))
    return out


def flatten(text):
    """Join comment and docstring lines into one run of words, so a pointer
    wrapped across lines (with `#` or indentation on each) reads whole."""
    lines = [re.sub(r"^\s*(#+|//|\*)?\s?", "", l) for l in text.splitlines()]
    return re.sub(r"\s+", " ", " ".join(lines))


def code_bodies():
    """Text of every tracked or untracked file except the docs, tests and data."""
    bodies = {}
    for rel in discovery.repo_files():
        if rel in DOCS or "/tests/" in "/" + rel or rel.endswith((".json", ".jsonl")):
            continue
        try:
            bodies[rel] = flatten(read(rel))
        except (UnicodeDecodeError, OSError):
            continue
    return bodies


def unindexed(bodies, decisions_text):
    """Files that defer to docs/decisions.md as a whole but are never named in it."""
    return sorted(rel for rel, body in bodies.items()
                  if INDEX_POINTER in body and rel not in decisions_text)


def unresolved_titles(bodies, heads):
    """(file, title) for each quoted decision title that matches no heading."""
    low = [h.lower() for h in heads]
    return sorted((rel, t) for rel, body in bodies.items()
                  for t in QUOTED_TITLE_RE.findall(body)
                  if not any(t.lower() in h for h in low))


def missing_paths(text, root=REPO):
    """Backticked paths whose first segment exists at the root but which do not."""
    top = set(os.listdir(root))
    bad = []
    for span in PATH_SPAN_RE.findall(text):
        if "/" not in span and not re.search(r"\.(md|py|sh|json|js)$", span):
            continue
        if span.split("/")[0] not in top:
            continue
        hits = glob.glob(os.path.join(root, span.rstrip("/")))
        if not hits:
            bad.append(span)
    return bad


class TestDecisionsPointersResolve(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.bodies = code_bodies()

    def test_every_file_that_defers_to_decisions_is_named_in_it(self):
        self.assertTrue([r for r, b in self.bodies.items() if INDEX_POINTER in b],
                        "found no deferring file: the scan is broken, not clean")
        self.assertEqual(unindexed(self.bodies, read(DECISIONS)), [],
                         "these files tell a reader the relevant decision is in "
                         "docs/decisions.md, which never names them")

    def test_every_quoted_decision_title_is_a_heading(self):
        self.assertTrue([t for b in self.bodies.values() for t in QUOTED_TITLE_RE.findall(b)],
                        "found no quoted title: the pattern is broken, not clean")
        self.assertEqual(unresolved_titles(self.bodies, headings(read(DECISIONS))), [])

    def test_an_unindexed_file_and_a_missing_title_are_reported(self):
        """Negative controls, on wrapped comment text like the real pointers."""
        bodies = {
            "tools/a/a.py": flatten("# Design decisions about this file are\n"
                                    "# recorded in docs/decisions.md. Read one."),
            "tools/b/b.py": flatten('# see `docs/decisions.md`, "Gate is\n#   ANDed"'),
            "tools/c/c.py": flatten('# see docs/decisions.md ("refuses the X tool")'),
        }
        self.assertEqual(unindexed(bodies, "## About tools/b/b.py"), ["tools/a/a.py"])
        self.assertEqual(unindexed(bodies, "tools/a/a.py"), [])
        self.assertEqual(unresolved_titles(bodies, ["The gate is ANDed, not one"]),
                         [("tools/c/c.py", "refuses the X tool")])
        self.assertEqual(unresolved_titles(bodies, ["Gate is ANDed",
                                                    "It refuses the X tool"]), [])


class TestClaudeMdCarriesWhatReadersOpen(unittest.TestCase):

    def test_sections_named_by_the_review_lenses_exist(self):
        names = {n for f in LENS_FILES for n in LENS_SECTION_RE.findall(read(f))}
        self.assertTrue(names, "no lens names a CLAUDE.md section: pattern broken")
        heads = headings(read("CLAUDE.md"))
        for name in names:
            self.assertIn(name, heads, "a review lens opens CLAUDE.md's %r section" % name)

    def test_the_testing_section_resolves_to_the_full_suite_loop(self):
        """merge-brief hands a reviewer this command. It must run every suite
        the runners run, and keep the queue off the suites' stdin."""
        command = merge_brief_lib.find_suite_command(REPO)
        self.assertIsNotNone(command, "merge-brief resolves no command from CLAUDE.md")
        self.assertIn("</dev/null", command)
        m = re.search(r"find ((?:[\w.-]+ )+)-path '\*/tests/test_\*\.py'", command)
        self.assertIsNotNone(m, "the resolved command is not the find-fed loop")
        runner_roots = {r for s in discovery.local_root_sets() for r in s}
        self.assertEqual(set(m.group(1).split()), runner_roots)


class TestDocPathsExist(unittest.TestCase):

    def test_every_backticked_repo_path_exists(self):
        for doc in DOCS:
            with self.subTest(doc=doc):
                self.assertEqual(missing_paths(read(doc)), [])

    def test_a_missing_path_is_reported(self):
        self.assertEqual(missing_paths("`lib/no_such_file.py` and `lib/` and "
                                       "`skills/*/*/SKILL.md` and `tests/x.py`"),
                         ["lib/no_such_file.py"])


if __name__ == "__main__":
    unittest.main()
