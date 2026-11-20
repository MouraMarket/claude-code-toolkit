"""Tests for claude-index-distill manifest, check, claim, release (C1-C7)."""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
for _p in (TOOL_DIR, os.path.join(REPO_ROOT, "lib"),
           os.path.join(REPO_ROOT, "tools", "memory-lint")):
    sys.path.insert(0, _p)

EXE = os.path.join(TOOL_DIR, "claude-index-distill")

PATH_PAGE = "---\npaths:\n  - \"**/*.py\"\n---\n\nA rule.\n"
TOOL_PAGE = "---\ntools:\n  - \"Bash(*git push*)\"\n---\n\nPush rule.\n"
MCP_PAGE = "---\ntools:\n  - \"mcp__x__*\"\n---\n\nMCP rule.\n"

LEAF = ("---\nname: %s\nmetadata:\n  node_type: memory\n  type: feedback\n---\n\n"
        "Body of %s.\n")

KEEP_ROW = "- [Keep](keep.md) — keep rule"
MOVE_ROW = "- [Move](move.md) — move rule"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def make_world(tmp):
    """(ws, mem, manifest) for a lint-clean sandbox."""
    ws = os.path.join(tmp, "ws")
    mem = os.path.join(tmp, "mem")
    write(os.path.join(ws, ".claude", "rules", "engineering.md"), PATH_PAGE)
    os.makedirs(os.path.join(ws, ".claude", "tool-rules"))
    write(os.path.join(mem, "MEMORY.md"),
          "## Always\n\n%s\n%s\n\n## Topics\n\n"
          "- [Build](topics/build.md) — build topic\n" % (KEEP_ROW, MOVE_ROW))
    write(os.path.join(mem, "keep.md"), LEAF % ("keep", "keep"))
    write(os.path.join(mem, "move.md"), LEAF % ("move", "move"))
    write(os.path.join(mem, "topics", "build.md"),
          "# Build\n\n- [Other](../other.md) — other row\n")
    write(os.path.join(mem, "other.md"),
          (LEAF % ("other", "other")).replace("feedback", "reference"))
    manifest = {
        "version": 1,
        "pages": [{"file": ".claude/rules/new.md", "bucket": "path",
                   "content": PATH_PAGE, "base_sha256": None,
                   "probe": {"path": "src/a.py"}}],
        "moves": [{"memory": "move.md", "page": ".claude/rules/new.md",
                   "topic": "topics/build.md", "row": MOVE_ROW,
                   "base_sha256": None}],
        "behaviour": [],
    }
    import distill_lib
    manifest["moves"][0]["base_sha256"] = distill_lib.sha256_file(
        os.path.join(mem, "move.md"))
    return ws, mem, manifest


def tool_page(content=TOOL_PAGE, file=".claude/tool-rules/t.md",
              probe="python3 _probe/push.py git push"):
    return {"file": file, "bucket": "tool", "content": content,
            "base_sha256": None,
            "probe": {"tool_name": "Bash", "tool_input": {"command": probe}}}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self._old = {k: os.environ.get(k) for k in
                     ("HOME", "USERPROFILE", "CLAUDE_INDEX_DISTILL_CLAIM")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.home
        self.ws, self.mem, self.manifest = make_world(self.tmp)
        import distill_lib
        self.lib = distill_lib

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def problems(self, manifest=None):
        return self.lib.validate(
            self.manifest if manifest is None else manifest, self.mem, self.ws)

    def assertProblem(self, text, manifest):
        found = self.problems(manifest)
        self.assertTrue(any(text in p for p in found),
                        "%r not in %r" % (text, found))

    def mutated(self):
        return copy.deepcopy(self.manifest)

    # CLI helpers
    def config(self, workspace=True):
        path = os.path.join(self.tmp, "config.json")
        write(path, json.dumps({"workspace_root": self.ws} if workspace else {}))
        return path

    def settings(self):
        write(os.path.join(self.home, ".claude", "settings.json"),
              json.dumps({"autoMemoryDirectory": self.mem}))

    def run_cli(self, *args, **extra):
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(HOME=self.home, USERPROFILE=self.home,
                   CLAUDE_INDEX_DISTILL_CLAIM=os.path.join(self.tmp, "claim"))
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env.update(extra)
        return subprocess.run([sys.executable, EXE] + list(args), env=env,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)


class TestWorld(Base):
    def test_world_is_lint_clean(self):
        import lint_lib
        result = lint_lib.scan(self.mem)
        self.assertTrue(result["readable"])
        self.assertEqual(lint_lib.findings(result), [])

    def test_c1_valid_manifest(self):
        self.assertEqual(self.problems(), [])

    def test_c1_cli_check_ok(self):
        self.settings()
        mpath = os.path.join(self.tmp, "m.json")
        write(mpath, json.dumps(self.manifest))
        r = self.run_cli("check", mpath, CLAUDE_TOOLKIT_CONFIG=self.config())
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertIn("manifest OK", r.stdout)

    def test_cli_check_invalid_json_exit_1(self):
        self.settings()
        mpath = os.path.join(self.tmp, "m.json")
        write(mpath, "{not json")
        r = self.run_cli("check", mpath, CLAUDE_TOOLKIT_CONFIG=self.config())
        self.assertEqual(r.returncode, 1)

    def test_cli_check_problems_prefixed(self):
        self.settings()
        m = self.mutated()
        m["moves"][0]["base_sha256"] = "0" * 64
        mpath = os.path.join(self.tmp, "m.json")
        write(mpath, json.dumps(m))
        r = self.run_cli("check", mpath, CLAUDE_TOOLKIT_CONFIG=self.config())
        self.assertEqual(r.returncode, 1)
        self.assertTrue(any(l.startswith("- ") for l in r.stdout.splitlines()))

    def test_c5_no_roots_exit_2(self):
        mpath = os.path.join(self.tmp, "m.json")
        write(mpath, json.dumps(self.manifest))
        r = self.run_cli("check", mpath, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertTrue(r.stderr.strip())

    def test_resolve_roots_names_missing(self):
        with self.assertRaises(self.lib.RootsError) as cm:
            self.lib.resolve_roots(self.home)
        self.assertIn("memory", str(cm.exception).lower())


class TestViolations(Base):
    def test_dotdot_page_file(self):
        m = self.mutated()
        m["pages"][0]["file"] = ".claude/rules/../x.md"
        self.assertProblem("..", m)

    def test_symlink_escape(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.ws, ".claude", "rules", "link"))
        m = self.mutated()
        m["pages"][0]["file"] = ".claude/rules/link/x.md"
        self.assertProblem("outside", m)

    def test_resolve_inside_symlink_direct(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        os.symlink(outside, os.path.join(self.ws, "link"))
        with self.assertRaises(self.lib.PathError):
            self.lib.resolve_inside(self.ws, "link/x.md")
        with self.assertRaises(self.lib.PathError):
            self.lib.resolve_inside(self.ws, "/abs.md")
        with self.assertRaises(self.lib.PathError):
            self.lib.resolve_inside(self.ws, "")
        with self.assertRaises(self.lib.PathError):
            self.lib.resolve_inside(self.ws, 5)
        self.lib.resolve_inside(self.ws, "ok/new.md")

    def test_path_page_without_paths(self):
        m = self.mutated()
        m["pages"][0]["content"] = "---\nname: x\n---\n\nbody\n"
        self.assertProblem("paths:", m)

    def test_tool_page_over_max_bytes(self):
        m = self.mutated()
        big = "---\ntools:\n  - \"Bash(*git push*)\"\n---\n\n" + "x" * 10000
        m["pages"] = [tool_page(big)]
        m["moves"][0]["page"] = ".claude/tool-rules/t.md"
        self.assertProblem("bytes", m)

    def test_tool_page_in_the_reserve_band_is_rejected(self):
        """9,400 bytes is under max_bytes (9500) but over what the hook can
        deliver (9500 - its reserve): check must reject what the hook can't load."""
        m = self.mutated()
        page_text = "---\ntools:\n  - \"Bash(*git push*)\"\n---\n\n" + "x" * 9400
        m["pages"] = [tool_page(page_text)]
        m["moves"][0]["page"] = ".claude/tool-rules/t.md"
        self.assertProblem("deliverable limit", m)

    def test_mr2_tool_page_in_a_subdirectory_is_rejected(self):
        """The hook globs only tool-rules/*.md: a nested page would never load."""
        m = self.mutated()
        m["pages"] = [tool_page(file=".claude/tool-rules/nested/t.md")]
        m["moves"][0]["page"] = ".claude/tool-rules/nested/t.md"
        self.assertProblem("subdirectory", m)

    def test_tool_probe_matches_nothing(self):
        m = self.mutated()
        m["pages"] = [tool_page(probe="ls -la")]
        m["moves"][0]["page"] = ".claude/tool-rules/t.md"
        self.assertProblem("matches no pattern", m)

    def test_tool_page_only_mcp(self):
        m = self.mutated()
        m["pages"] = [tool_page(MCP_PAGE)]
        m["moves"][0]["page"] = ".claude/tool-rules/t.md"
        self.assertProblem("tool page has no Bash(...) pattern; a rule that "
                           "only matters on MCP calls stays in the index", m)

    def test_probe_reserved_claude_md(self):
        m = self.mutated()
        m["pages"][0]["probe"] = {"path": "sub/CLAUDE.md"}
        self.assertProblem("reserved", m)

    def test_probe_reserved_dot_claude(self):
        m = self.mutated()
        m["pages"][0]["probe"] = {"path": ".claude/x"}
        self.assertProblem("reserved", m)

    def test_behaviour_reserved_claude_md(self):
        m = self.mutated()
        m["behaviour"] = [{"memory": "move.md", "files": {"sub/CLAUDE.md": "x"},
                           "prompt": "p", "pass_if": "q"}]
        self.assertProblem("reserved", m)

    def test_behaviour_reserved_dot_claude(self):
        m = self.mutated()
        m["behaviour"] = [{"memory": "move.md", "files": {".claude/x": "x"},
                           "prompt": "p", "pass_if": "q"}]
        self.assertProblem("reserved", m)

    def test_is_reserved(self):
        r = self.lib.is_reserved
        self.assertTrue(r("a/CLAUDE.local.md", "mem"))
        self.assertTrue(r("_proof/x", "mem"))
        self.assertTrue(r("mem", "mem"))
        self.assertTrue(r("mem/x.md", "mem"))
        self.assertFalse(r("memo/x.md", "mem"))
        self.assertFalse(r("src/a.py", "mem"))

    def test_mr2_dot_claude_reserved_at_any_depth(self):
        for rel in ("src/.claude/rules/x.md", "a/b/.CLAUDE/x"):
            self.assertTrue(self.lib.is_reserved(rel, "mem"), rel)
        self.assertFalse(self.lib.is_reserved("src/claude/x.md", "mem"))
        # _proof and the memdir stay anchored at the root.
        self.assertFalse(self.lib.is_reserved("a/_proof/x", "mem"))
        self.assertFalse(self.lib.is_reserved("a/mem/x", "mem"))
        m = self.mutated()
        m["behaviour"] = [{"memory": "move.md", "files": {"src/.claude/rules/x.md": "x"},
                           "prompt": "p", "pass_if": "q"}]
        self.assertProblem("reserved", m)

    def test_mr3_bad_relpath(self):
        bad = self.lib.bad_relpath
        for rel in ("C:/x/a.md", "C:x.py", "_probe\\p.py", "/x/p.py", "../a", "a/../b",
                    "\\\\srv\\share\\a", "C:\\x"):
            self.assertTrue(bad(rel), rel)
        for rel in ("_probe/m.py", "a/b.md", "a/b:c.md"):
            self.assertFalse(bad(rel), rel)

    def test_mr3_probe_script_rejects_drive_and_backslash(self):
        ps = self.lib.probe_script
        for command in ("python3 C:/x/p.py", "python3 C:x.py", "python3 _probe\\p.py",
                        "python3 /x/p.py"):
            self.assertIsNone(ps(command), command)
        self.assertEqual(ps("python3 _probe/m.py graph.facebook.com https://graph.facebook.com/x"),
                         "_probe/m.py")

    def test_mr3_validate_reports_drive_paths(self):
        m = self.mutated()
        m["pages"][0]["probe"] = {"path": "C:/x/a.md"}
        self.assertProblem("must be a plain relative path", m)
        m = self.mutated()
        m["behaviour"] = [{"memory": "move.md", "files": {"C:/x/a.txt": "x"},
                           "prompt": "p", "pass_if": "q"}]
        self.assertProblem("must be a plain relative path", m)

    def test_move_to_unnominated_topic(self):
        write(os.path.join(self.mem, "topics", "other.md"),
              "# Spare\n\n- [Spare](../other.md) — spare\n")
        m = self.mutated()
        m["moves"][0]["topic"] = "topics/other.md"
        self.assertProblem("topic", m)

    def test_row_absent_from_always(self):
        m = self.mutated()
        m["moves"][0]["row"] = "- [Move](move.md) — not the row"
        self.assertProblem("row", m)

    def test_row_present_twice(self):
        path = os.path.join(self.mem, "MEMORY.md")
        text = read(path).replace(MOVE_ROW + "\n", MOVE_ROW + "\n" + MOVE_ROW + "\n")
        # Duplicate rows are themselves a lint finding; the row rule must still fire.
        write(path, text)
        self.assertProblem("exactly once", self.manifest)

    def test_stale_base_sha(self):
        m = self.mutated()
        m["moves"][0]["base_sha256"] = "0" * 64
        self.assertProblem("base_sha256", m)

    def test_stale_page_base_sha(self):
        m = self.mutated()
        m["pages"][0]["base_sha256"] = "0" * 64
        self.assertProblem("base_sha256", m)

    def test_five_behaviour_cases(self):
        m = self.mutated()
        m["behaviour"] = [{"memory": "move.md", "files": {}, "prompt": "p",
                           "pass_if": "q"}] * 5
        self.assertProblem("at most 4", m)

    def test_leaf_no_frontmatter(self):
        write(os.path.join(self.mem, "move.md"), "no frontmatter\n")
        m = self.mutated()
        m["moves"][0]["base_sha256"] = self.lib.sha256_file(
            os.path.join(self.mem, "move.md"))
        self.assertProblem("leaf move.md has no editable frontmatter type", m)

    def test_leaf_no_type(self):
        write(os.path.join(self.mem, "move.md"), "---\nname: move\n---\n\nbody\n")
        m = self.mutated()
        m["moves"][0]["base_sha256"] = self.lib.sha256_file(
            os.path.join(self.mem, "move.md"))
        self.assertProblem("leaf move.md has no editable frontmatter type", m)

    def test_leaf_already_demoted(self):
        text = (LEAF % ("move", "move")).replace(
            "Body", "> **Rule text lives in `~/x.md`** (moved).\n\nBody")
        write(os.path.join(self.mem, "move.md"), text)
        m = self.mutated()
        m["moves"][0]["base_sha256"] = self.lib.sha256_file(
            os.path.join(self.mem, "move.md"))
        self.assertProblem("leaf move.md is already demoted", m)

    def test_duplicate_page_and_move(self):
        m = self.mutated()
        m["pages"].append(copy.deepcopy(m["pages"][0]))
        m["moves"].append(copy.deepcopy(m["moves"][0]))
        found = self.problems(m)
        self.assertTrue(any("share" in p or "duplicate" in p for p in found), found)

    def test_dirty_start_c3(self):
        path = os.path.join(self.mem, "MEMORY.md")
        write(path, read(path) + "- [Gone](gone.md) — missing file\n")
        # Row sits under ## Topics, a missing leaf is a dead link.
        self.assertProblem("live memory not clean:", self.manifest)

    def test_c4_all_three_reported(self):
        m = self.mutated()
        m["moves"][0]["base_sha256"] = "0" * 64
        m["pages"][0]["content"] = "---\nname: x\n---\n\nbody\n"
        m["behaviour"] = [{"memory": "move.md", "files": {}, "prompt": "p",
                           "pass_if": "q"}] * 5
        found = self.problems(m)
        for text in ("base_sha256", "paths:", "at most 4"):
            self.assertTrue(any(text in p for p in found), (text, found))

    def test_top_level_keys(self):
        found = self.problems({"version": 2, "pages": []})
        self.assertTrue(found)


class TestDependencySet(Base):
    def test_c7(self):
        dep = self.lib.dependency_set(self.manifest, self.mem, self.ws)
        json.dumps(dep)
        page = os.path.join(os.path.realpath(self.ws), ".claude", "rules", "new.md")
        files = {os.path.realpath(k): v for k, v in dep["files"].items()}
        self.assertIsNone(files[page])
        leaf = os.path.join(os.path.realpath(self.mem), "move.md")
        self.assertEqual(files[leaf], self.manifest["moves"][0]["base_sha256"])
        self.assertEqual(len(dep["rows"]), 1)
        self.assertEqual(dep["rows"][0]["row"], MOVE_ROW)
        self.assertTrue(dep["rows"][0]["file"].endswith("MEMORY.md"))
        self.assertEqual(len(dep["topics"]), 1)
        self.assertTrue(list(dep["topics"])[0].endswith("topics/build.md"))


class TestClaimCli(Base):
    def test_c6_claim_twice_and_unset(self):
        r1 = self.run_cli("claim", CLAUDE_CODE_SESSION_ID="s1")
        self.assertEqual(r1.returncode, 0, r1.stderr)
        self.assertIn("claimed", r1.stdout)
        r2 = self.run_cli("claim", CLAUDE_CODE_SESSION_ID="s2")
        self.assertEqual(r2.returncode, 1)
        self.assertIn("held by s1 since", r2.stderr)
        r3 = self.run_cli("claim")
        self.assertEqual(r3.returncode, 1)
        self.assertIn("CLAUDE_CODE_SESSION_ID is not set", r3.stderr)

    def test_release(self):
        self.run_cli("claim", CLAUDE_CODE_SESSION_ID="s1")
        bad = self.run_cli("release", CLAUDE_CODE_SESSION_ID="s2")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("not held by this session", bad.stdout)
        ok = self.run_cli("release", CLAUDE_CODE_SESSION_ID="s1")
        self.assertEqual(ok.returncode, 0)
        self.assertIn("released", ok.stdout)

    def test_claim_oserror_no_traceback(self):
        blocker = os.path.join(self.tmp, "blocker")
        write(blocker, "a file, not a directory")
        r = self.run_cli("claim", CLAUDE_CODE_SESSION_ID="s1",
                         CLAUDE_INDEX_DISTILL_CLAIM=os.path.join(blocker, "claim"))
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertTrue(r.stderr.strip())


class ApplyBase(Base):
    """A sandboxed claim held by session S1, plus proof-file helpers."""

    SESSION = "S1"

    def setUp(self):
        super().setUp()
        import distill_claim
        import lint_lib
        self.claimmod = distill_claim
        self.lint = lint_lib
        os.environ["CLAUDE_INDEX_DISTILL_CLAIM"] = os.path.join(self.tmp, "claim")
        self.claimmod.claim(self.SESSION)
        self.mpath = os.path.join(self.tmp, "manifest.json")

    def write_manifest(self, manifest=None):
        write(self.mpath, json.dumps(self.manifest if manifest is None else manifest))

    def write_proof(self, verdict="PASS", mutate=None):
        with open(self.mpath, "rb") as fh:
            raw = fh.read()
        import hashlib
        manifest = json.loads(raw.decode("utf-8"))
        proof = {
            "verdict": verdict,
            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "roots": {"memory_dir": os.path.realpath(self.mem),
                      "workspace": os.path.realpath(self.ws)},
            "dependencies": json.loads(json.dumps(
                self.lib.dependency_set(manifest, self.mem, self.ws))),
            "proved_at": "2026-10-31T00:00:00Z",
        }
        if mutate:
            mutate(proof)
        write(self.mpath + ".proof.json", json.dumps(proof))

    def ready(self, manifest=None):
        self.write_manifest(manifest)
        self.write_proof()

    def run_apply(self, **kw):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.lib.apply(self.mpath, self.mem, self.ws, self.SESSION,
                                  today="2026-10-31", **kw)
        return code, buf.getvalue()

    def tree(self):
        import hashlib
        out = {}
        for root in (self.ws, self.mem):
            for dirpath, _dirs, names in os.walk(root):
                for name in names:
                    path = os.path.join(dirpath, name)
                    with open(path, "rb") as fh:
                        out[path] = hashlib.sha256(fh.read()).hexdigest()
        return out

    def memory_path(self, rel="MEMORY.md"):
        return os.path.join(self.mem, rel)

    def new_page(self):
        return os.path.join(self.ws, ".claude", "rules", "new.md")


HEADING_PREFIX = "## Rules carried by "


class TestApply(ApplyBase):
    def test_d1_happy_path(self):
        self.ready()
        code, out = self.run_apply()
        self.assertEqual(code, 0, out)
        self.assertNotIn(MOVE_ROW, read(self.memory_path()))
        self.assertIn(KEEP_ROW, read(self.memory_path()))
        topic = read(self.memory_path("topics/build.md"))
        self.assertEqual(topic.count(HEADING_PREFIX), 1)
        self.assertIn("(../move.md)", topic)
        leaf = read(self.memory_path("move.md")).split("\n")
        self.assertIn("  type: reference", leaf)
        self.assertNotIn("  type: feedback", leaf)
        close = [i for i, l in enumerate(leaf) if l == "---"][1]
        self.assertEqual(leaf[close + 1], "")
        self.assertTrue(leaf[close + 2].startswith("> **Rule text lives in `"), leaf[close + 2])
        self.assertIn(self.new_page(), leaf[close + 2])
        self.assertIn("(moved 2026-10-31, index-distill)", leaf[close + 2])
        self.assertEqual(read(self.new_page()), PATH_PAGE)
        self.assertEqual(self.lint.findings(self.lint.scan(self.mem)), [])
        self.assertIsNone(self.claimmod.read_live())
        backups = os.path.join(self.home, ".claude", "index-distill", "backups")
        stamps = os.listdir(backups)
        self.assertEqual(len(stamps), 1)
        with open(os.path.join(backups, stamps[0], "originals.json")) as fh:
            originals = json.load(fh)
        self.assertEqual(originals[self.new_page()], "absent")
        self.assertEqual(originals[self.memory_path("move.md")], "present")

    def test_e1b_apply_revalidates_a_start_gone_dirty(self):
        """Only apply's own validate() call can catch this: a peer adds a
        broken Always row AFTER the proof. Every moved row and file is still
        intact, so the proof's row-level dependencies hold and operations()
        resolves fine -- but the live memory is no longer clean."""
        self.ready()
        index = self.memory_path()
        write(index, read(index).replace(
            MOVE_ROW + "\n", MOVE_ROW + "\n- [Ghost](ghost.md) — a row to nowhere\n", 1))
        before = self.tree()
        code, out = self.run_apply()
        self.assertEqual(code, 1, out)
        self.assertIn("live memory not clean", out)
        self.assertEqual(self.tree(), before)

    def test_e1_apply_refuses_a_move_that_escapes_the_memory_dir(self):
        outside = os.path.join(self.tmp, "outside.md")
        write(outside, LEAF % ("outside", "outside"))
        before = read(outside)
        manifest = self.mutated()
        manifest["moves"][0]["memory"] = "../outside.md"
        manifest["moves"][0]["base_sha256"] = self.lib.sha256_file(outside)
        self.ready(manifest)
        code, out = self.run_apply()
        self.assertEqual(code, 1, out)
        self.assertEqual(read(outside), before)
        self.assertFalse(os.path.exists(
            os.path.join(self.home, ".claude", "index-distill", "backups")), out)

    def test_leaf_note_uses_tilde_for_home(self):
        home_ws = os.path.join(self.home, "wsx")
        os.rename(self.ws, home_ws)
        self.ws = home_ws
        self.manifest["moves"][0]["base_sha256"] = self.lib.sha256_file(
            os.path.join(self.mem, "move.md"))
        self.ready()
        code, out = self.run_apply()
        self.assertEqual(code, 0, out)
        self.assertIn("`~/wsx/.claude/rules/new.md`", read(self.memory_path("move.md")))
        self.assertIn(HEADING_PREFIX + "~/wsx/.claude/rules/new.md", read(
            self.memory_path("topics/build.md")))

    def test_apply_manifest_direct(self):
        self.lib.apply_manifest(self.manifest, self.mem, self.ws, "2026-10-31")
        self.assertNotIn(MOVE_ROW, read(self.memory_path()))
        self.assertEqual(read(self.new_page()), PATH_PAGE)

    def test_d2_second_move_reuses_heading(self):
        self.ready()
        self.assertEqual(self.run_apply()[0], 0)
        page_sha = self.lib.sha256_file(self.new_page())
        second = {
            "version": 1,
            "pages": [{"file": ".claude/rules/new.md", "bucket": "path",
                       "content": None, "base_sha256": page_sha,
                       "probe": {"path": "src/a.py"}}],
            "moves": [{"memory": "keep.md", "page": ".claude/rules/new.md",
                       "topic": "topics/build.md", "row": KEEP_ROW,
                       "base_sha256": self.lib.sha256_file(os.path.join(self.mem, "keep.md"))}],
            "behaviour": [],
        }
        self.claimmod.claim(self.SESSION)
        self.ready(second)
        code, out = self.run_apply()
        self.assertEqual(code, 0, out)
        topic = read(self.memory_path("topics/build.md"))
        self.assertEqual(topic.count(HEADING_PREFIX), 1)
        self.assertIn("(../move.md)", topic)
        self.assertIn("(../keep.md)", topic)
        backups = os.path.join(self.home, ".claude", "index-distill", "backups")
        self.assertEqual(len(os.listdir(backups)), 2)

    def test_d3_peer_append_survives(self):
        peer = "- [Peer](peer.md) — peer row"
        calls = []

        def hook(path):
            if os.path.basename(path) == "MEMORY.md" and not calls:
                calls.append(path)
                # The peer adds its leaf and row together mid-apply; an orphan
                # leaf before apply would make the live memory unclean.
                write(os.path.join(self.mem, "peer.md"), LEAF % ("peer", "peer"))
                text = read(path)
                write(path, text.replace(MOVE_ROW + "\n", MOVE_ROW + "\n" + peer + "\n", 1))

        self.ready()
        code, out = self.run_apply(_hook=hook)
        self.assertEqual(code, 0, out)
        final = read(self.memory_path())
        self.assertIn(peer, final)
        self.assertNotIn(MOVE_ROW, final)

    def _delete_row_hook(self, extra=None):
        calls = []

        def hook(path):
            if os.path.basename(path) == "MEMORY.md" and not calls:
                calls.append(path)
                write(path, read(path).replace(MOVE_ROW + "\n", "", 1))
                if extra:
                    extra()

        return hook

    def test_d4_memory_edit_rolls_back(self):
        self.ready()
        before = {rel: read(self.memory_path(rel))
                  for rel in ("move.md", "topics/build.md", "MEMORY.md")}
        code, out = self.run_apply(_hook=self._delete_row_hook())
        self.assertEqual(code, 5, out)
        self.assertEqual(read(self.memory_path("move.md")), before["move.md"])
        self.assertEqual(read(self.memory_path("topics/build.md")), before["topics/build.md"])
        self.assertFalse(os.path.exists(self.new_page()))
        self.assertEqual(read(self.memory_path()),
                         before["MEMORY.md"].replace(MOVE_ROW + "\n", "", 1))
        self.assertIn("rolled back", out)

    # A Windows-edited (CRLF) memory dir.
    CRLF_FILES = ("MEMORY.md", "move.md", "topics/build.md")

    def raw(self, rel):
        with open(self.memory_path(rel), "rb") as fh:
            return fh.read()

    def to_crlf(self, rel, keep_lf_line=None):
        data = self.raw(rel).replace(b"\n", b"\r\n")
        if keep_lf_line is not None:
            data = data.replace(keep_lf_line + b"\r\n", keep_lf_line + b"\n", 1)
        with open(self.memory_path(rel), "wb") as fh:
            fh.write(data)
        self.manifest["moves"][0]["base_sha256"] = self.lib.sha256_file(
            self.memory_path("move.md"))

    def assertAllCrlf(self, rel):
        data = self.raw(rel)
        self.assertIn(b"\r\n", data, rel)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""), rel)

    def test_crlf_memory_applies_and_keeps_its_endings(self):
        for rel in self.CRLF_FILES:
            self.to_crlf(rel)
        self.assertEqual(self.problems(), [])
        self.ready()
        code, out = self.run_apply()
        self.assertEqual(code, 0, out)
        for rel in self.CRLF_FILES:
            self.assertAllCrlf(rel)
        index = self.raw("MEMORY.md").decode("utf-8")
        self.assertNotIn(MOVE_ROW, index)
        self.assertIn(KEEP_ROW + "\r\n", index)
        topic = self.raw("topics/build.md").decode("utf-8")
        self.assertEqual(topic.count(HEADING_PREFIX), 1)
        self.assertIn(MOVE_ROW.replace("(move.md)", "(../move.md)") + "\r\n", topic)
        leaf = self.raw("move.md").decode("utf-8").split("\r\n")
        self.assertIn("  type: reference", leaf)
        self.assertTrue(any(l.startswith("> **Rule text lives in `") for l in leaf))
        self.assertEqual(self.lint.findings(self.lint.scan(self.mem)), [])

    def test_crlf_rollback_restores_the_exact_bytes(self):
        for rel in self.CRLF_FILES:
            self.to_crlf(rel)
        self.ready()
        before = {rel: self.raw(rel) for rel in self.CRLF_FILES}
        row = (MOVE_ROW + "\r\n").encode("utf-8")

        def hook(path, calls=[]):
            if os.path.basename(path) == "MEMORY.md" and not calls:
                calls.append(path)
                with open(path, "rb") as fh:
                    data = fh.read()
                with open(path, "wb") as fh:
                    fh.write(data.replace(row, b"", 1))

        code, out = self.run_apply(_hook=hook)
        self.assertEqual(code, 5, out)
        self.assertEqual(self.raw("move.md"), before["move.md"])
        self.assertEqual(self.raw("topics/build.md"), before["topics/build.md"])
        self.assertEqual(self.raw("MEMORY.md"), before["MEMORY.md"].replace(row, b"", 1))

    def test_mixed_line_endings_are_refused_by_check(self):
        for rel, line in (("MEMORY.md", b"## Topics"), ("move.md", b"---"),
                          ("topics/build.md", b"# Build")):
            with self.subTest(rel=rel):
                self.tearDown()
                self.setUp()
                self.to_crlf(rel, keep_lf_line=line)
                self.assertProblem("%s mixes Windows (CRLF) and Unix (LF) line endings"
                                   % self.memory_path(rel), self.manifest)
                self.to_crlf(rel)  # one ending throughout: accepted
                self.assertEqual(self.problems(), [])

    def test_d5b_failure_before_any_replace_is_a_refusal(self):
        """Pages are written first, so a peer creating the new page between
        step 1 and its write fails the FIRST write, before anything was
        replaced: exit 1 and a tree changed only by the peer, never exit 5."""
        from unittest import mock
        self.ready()
        page = self.new_page()
        real_backup = self.lib._backup

        def backup_then_peer_edit(*a, **kw):
            result = real_backup(*a, **kw)
            write(page, "peer's page\n")
            return result

        with mock.patch.object(self.lib, "_backup", backup_then_peer_edit):
            before = self.tree()
            code, out = self.run_apply()
        self.assertEqual(code, 1, out)
        self.assertIn("nothing written", out)
        after = self.tree()
        changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
        self.assertEqual(changed, {str(page)}, out)
        self.assertEqual(read(page), "peer's page\n")

    def test_d4d_topic_failure_leaves_memory_md_untouched(self):
        """MEMORY.md is written LAST: a topic write that fails (a peer keeps
        changing it, so the 3-attempt compare never settles) must leave
        MEMORY.md byte-identical, because rollback never touches MEMORY.md."""
        self.ready()
        index_before = read(self.memory_path())
        topic = self.memory_path("topics/build.md")

        def churn(path):
            if os.path.realpath(path) == os.path.realpath(topic):
                write(topic, read(topic) + "\n")

        code, out = self.run_apply(_hook=churn)
        self.assertEqual(code, 5, out)
        self.assertEqual(read(self.memory_path()), index_before)
        self.assertIn(MOVE_ROW, read(self.memory_path()))

    def test_d4b_topic_rollback_keeps_peer_row(self):
        peer = "- [Peer](../peer.md) — peer row\n"

        def extra():
            topic = self.memory_path("topics/build.md")
            write(topic, read(topic) + peer)

        self.ready()
        code, out = self.run_apply(_hook=self._delete_row_hook(extra))
        self.assertEqual(code, 5, out)
        topic = read(self.memory_path("topics/build.md"))
        self.assertIn(peer.strip(), topic)
        self.assertNotIn("(../move.md)", topic)
        if HEADING_PREFIX in topic:
            section = topic.split(HEADING_PREFIX, 1)[1].split("\n", 1)[1]
            self.assertTrue(section.strip(), "empty carried-by heading left behind")

    def test_d4b_rollback_removes_empty_heading(self):
        self.ready()
        before = read(self.memory_path("topics/build.md"))
        code, out = self.run_apply(_hook=self._delete_row_hook())
        self.assertEqual(code, 5, out)
        self.assertEqual(read(self.memory_path("topics/build.md")), before)

    def test_d4c_conflict_exit_6(self):
        def extra():
            write(self.new_page(), "someone else's text\n")

        self.ready()
        code, out = self.run_apply(_hook=self._delete_row_hook(extra))
        self.assertEqual(code, 6, out)
        self.assertIn("ROLLBACK INCOMPLETE", out)
        self.assertIn("new.md", out)
        self.assertEqual(read(self.new_page()), "someone else's text\n")

    def test_d1b_roots_changed(self):
        self.write_manifest()
        self.write_proof(mutate=lambda p: p["roots"].update(workspace="/elsewhere"))
        before = self.tree()
        code, out = self.run_apply()
        self.assertEqual(code, 1)
        self.assertIn("roots changed since the proof", out)
        self.assertIn("workspace was /elsewhere", out)
        self.assertEqual(self.tree(), before)

    def _refused(self):
        before = self.tree()
        code, out = self.run_apply()
        self.assertEqual(code, 1, out)
        self.assertEqual(self.tree(), before)
        return out

    def test_d5_no_proof(self):
        self.write_manifest()
        self._refused()

    def test_d5_verdict_fail(self):
        self.write_manifest()
        self.write_proof(verdict="FAIL")
        self._refused()

    def test_d5_manifest_edited_after_proof(self):
        self.ready()
        with open(self.mpath, "a") as fh:
            fh.write("\n")
        self._refused()

    def test_d5_claim_held_by_other(self):
        self.ready()
        self.claimmod.release(self.SESSION)
        self.claimmod.claim("S2")
        self._refused()

    def test_mr4_no_session_id_never_matches_a_half_written_claim(self):
        """A claim dir with no holder.json reads as session_id None; a caller
        with no session id must not pass as its owner."""
        self.ready()
        self.claimmod.release(self.SESSION)
        os.makedirs(os.environ["CLAUDE_INDEX_DISTILL_CLAIM"])
        self.assertEqual(self.claimmod.read_live()["session_id"], None)
        before = self.tree()
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.lib.apply(self.mpath, self.mem, self.ws, None,
                                  today="2026-10-31")
        self.assertEqual(code, 1, buf.getvalue())
        self.assertIn("not held by this session", buf.getvalue())
        self.assertEqual(self.tree(), before)

    def test_d5_dependencies_path_set_differs(self):
        self.write_manifest()
        self.write_proof(mutate=lambda p: p["dependencies"]["files"].pop(
            self.memory_path("move.md")))
        self._refused()

    def test_d5_carrier_page_edited_after_proof(self):
        carrier = os.path.join(self.ws, ".claude", "rules", "engineering.md")
        manifest = self.mutated()
        manifest["pages"] = [{"file": ".claude/rules/engineering.md", "bucket": "path",
                              "content": None,
                              "base_sha256": self.lib.sha256_file(carrier),
                              "probe": {"path": "src/a.py"}}]
        manifest["moves"][0]["page"] = ".claude/rules/engineering.md"
        self.ready(manifest)
        with open(carrier, "a") as fh:
            fh.write("edited\n")
        self._refused()

    def test_d5_row_gone_after_proof(self):
        self.ready()
        write(self.memory_path(), read(self.memory_path()).replace(MOVE_ROW + "\n", "", 1))
        self._refused()

    def test_d6_over_threshold_returns_4(self):
        class Cfg:
            def get_int(self, *keys, default=0):
                return default

            def get(self, *keys, default=None):
                return 50 if keys == ("memory_lint", "distill_at_chars") else default

        self.ready()
        code, out = self.run_apply(config=Cfg())
        self.assertEqual(code, 4, out)
        self.assertTrue(os.path.exists(self.new_page()))
        self.assertIn("index chars:", out)

    def test_d6_over_threshold_in_bytes_only_returns_4(self):
        # The threshold is one past the post-apply CHAR count, so only a
        # byte-aware check (rows carry a 3-byte em dash) still reports 4.
        import lint_lib
        mem = self.mem
        seen = {}

        class Cfg:
            def get_int(self, *keys, default=0):
                return default

            def get(self, *keys, default=None):
                if keys != ("memory_lint", "distill_at_chars"):
                    return default
                seen.update(lint_lib.scan(mem))
                return seen["index_chars"] + 1

        self.ready()
        code, out = self.run_apply(config=Cfg())
        self.assertIn("index_bytes", seen, "threshold was never read")
        self.assertGreaterEqual(seen["index_bytes"], seen["index_chars"] + 1)
        self.assertEqual(code, 4, out)

    def test_d7_already_demoted_is_a_problem(self):
        leaf = os.path.join(self.mem, "move.md")
        write(leaf, read(leaf).replace("\n---\n\nBody", "\n---\n\n> **Rule text lives in `x`**\n\nBody", 1))
        self.manifest["moves"][0]["base_sha256"] = self.lib.sha256_file(leaf)
        self.assertProblem("already demoted", self.manifest)

BAD_PROBES = [
    "curl -s https://x", "python3", "python3 -c x", "python3 ../x.py",
    "python3 /abs.py", "python3 .claude/x.py", "python3 mem/x.py",
    "python3 _proof/x.py", "python3 a/CLAUDE.md",
    "python3 _probe/m.py; touch /tmp/x", "python3 _probe/m.py > /tmp/x",
    "python3 _probe/m.py $(id)", "python3 _probe/m.py && rm x",
    "python3 _probe/m.py 'q'", "python3  _probe/m.py", "python3 _probe/m.py *",
    "python3 ././_proof/hooks/contain.py", "python3 ./mem/MEMORY.md",
    "./.claude/x.sh",
]


class TestProbeScript(Base):
    """F1, F2: the stub-able probe rule, the dot-component reserved rule, G7."""

    def test_f1_probe_script_accepts(self):
        ps = self.lib.probe_script
        self.assertEqual(ps("python3 _probe/m.py graph.facebook.com"), "_probe/m.py")
        self.assertEqual(ps("./_probe/x.sh a"), "_probe/x.sh")
        self.assertEqual(ps("bash s/y.sh"), "s/y.sh")
        self.assertEqual(ps("python3 ./_probe/./m.py"), "_probe/m.py")

    def test_f1_probe_script_rejects(self):
        for command in BAD_PROBES:
            self.assertIsNone(self.lib.probe_script(command), command)

    def test_f1_is_reserved_drops_dot_components(self):
        for rel in ("./mem/MEMORY.md", "./.claude/x", "././_proof/x", "./CLAUDE.md"):
            self.assertTrue(self.lib.is_reserved(rel, "mem"), rel)

    def test_p6_is_reserved_is_case_insensitive(self):
        for rel in ("_PROOF/x", "MEM/MEMORY.md", ".Claude/rules/a.md", "docs/claude.md",
                    "x/Claude.Local.md"):
            self.assertTrue(self.lib.is_reserved(rel, "mem"), rel)
        self.assertFalse(self.lib.is_reserved("memory/a.md", "mem"))

    def test_f1_validate_reports_dotted_reserved_paths(self):
        m = self.mutated()
        m["pages"][0]["probe"] = {"path": "./mem/x.md"}
        self.assertProblem("reserved", m)
        m = self.mutated()
        m["behaviour"] = [{"memory": "move.md", "files": {"./.claude/x": "x"},
                           "prompt": "p", "pass_if": "q"}]
        self.assertProblem("reserved", m)

    def tool_manifest(self, probe, pattern="Bash(*graph.facebook.com*)"):
        m = self.mutated()
        content = "---\ntools:\n  - \"%s\"\n---\n\nRule.\n" % pattern
        m["pages"] = [tool_page(content, probe=probe)]
        m["moves"][0]["page"] = ".claude/tool-rules/t.md"
        return m

    def test_f2_unstubbable_probe_is_reported(self):
        m = self.tool_manifest("curl -s https://graph.facebook.com/x")
        self.assertProblem("probe command must run a stub-able script", m)

    def test_f2_stubbable_probe_is_clean(self):
        m = self.tool_manifest("python3 _probe/m.py graph.facebook.com")
        found = self.problems(m)
        self.assertFalse(any("stub-able" in p for p in found), found)
        self.assertEqual(found, [])

    def test_f2_behaviour_on_tool_page_is_reported(self):
        m = self.tool_manifest("python3 _probe/m.py graph.facebook.com")
        m["behaviour"] = [{"memory": "move.md", "files": {}, "prompt": "p",
                           "pass_if": "q"}]
        self.assertProblem("its rule moves to a tool page; behaviour cases "
                           "cover path pages only", m)

    def test_f2_behaviour_on_path_page_is_clean(self):
        m = self.mutated()
        m["behaviour"] = [{"memory": "move.md", "files": {}, "prompt": "p",
                           "pass_if": "q"}]
        found = self.problems(m)
        self.assertFalse(any("path pages only" in p for p in found), found)


if __name__ == "__main__":
    unittest.main()
