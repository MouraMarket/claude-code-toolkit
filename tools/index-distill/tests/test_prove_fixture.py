"""Tests for prove_lib's proof fixture (Part B Task 1, F2b-F11)."""

import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.dirname(os.path.dirname(TOOL_DIR))
for _p in (HERE, TOOL_DIR, os.path.join(REPO_ROOT, "lib"),
           os.path.join(REPO_ROOT, "tools", "memory-lint"),
           os.path.join(REPO_ROOT, "tools", "tool-rules")):
    sys.path.insert(0, _p)

from test_distill_lib import LEAF, PATH_PAGE, make_world, read, tool_page, write  # noqa: E402

TOOL_CMD = "python3 _probe/m.py graph.facebook.com"
TOOL_CONTENT = ("---\ntools:\n  - \"Bash(*graph.facebook.com*)\"\n---\n\n"
                "Facebook rule.\n")
MOVE2_ROW = "- [Move2](move2.md) — second rule"
SETTINGS_MARKER = "LIVE-SETTINGS-MARKER-7"


class Cfg(object):
    """A config interface double (not a dict): G10."""

    def __init__(self, **values):
        self.values = values

    def get(self, *keys, default=None):
        return self.values.get(keys[-1], default)

    def get_int(self, *keys, default=None):
        return self.values.get(keys[-1], default)


def tree_hashes(root):
    out = {}
    for base, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            with open(path, "rb") as fh:
                out[os.path.relpath(path, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


class FixtureBase(unittest.TestCase):
    def setUp(self):
        import distill_lib
        import prove_lib
        self.lib, self.prove = distill_lib, prove_lib
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = os.path.realpath(self._tmp.name)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(self.home)
        self._old = {k: os.environ.get(k) for k in
                     ("HOME", "USERPROFILE", "CLAUDE_INDEX_DISTILL_CLAIM")}
        os.environ["HOME"] = os.environ["USERPROFILE"] = self.home
        os.environ["CLAUDE_INDEX_DISTILL_CLAIM"] = os.path.join(self.tmp, "claim")
        self.ws, self.mem, self.manifest = make_world(self.tmp)
        write(os.path.join(self.ws, "CLAUDE.md"), "# Workspace\n")
        write(os.path.join(self.ws, ".claude", "settings.json"),
              json.dumps({"marker": SETTINGS_MARKER}))
        # A second move, to a tool page.
        write(os.path.join(self.mem, "move2.md"), LEAF % ("move2", "move2"))
        index = os.path.join(self.mem, "MEMORY.md")
        write(index, read(index).replace("\n## Topics", MOVE2_ROW + "\n\n## Topics", 1))
        self.manifest["pages"].append(tool_page(
            TOOL_CONTENT, probe=TOOL_CMD))
        self.manifest["moves"].append({
            "memory": "move2.md", "page": ".claude/tool-rules/t.md",
            "topic": "topics/build.md", "row": MOVE2_ROW,
            "base_sha256": self.lib.sha256_file(os.path.join(self.mem, "move2.md"))})
        self.base = os.path.join(self.tmp, "fixtures")
        os.makedirs(self.base)
        self.config_doc = {"workspace_root": self.ws, "owner_name": "Sam"}
        self.fixtures = []

    def tearDown(self):
        for key, value in self._old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def build(self, manifest=None, config=None, rng_seed=1):
        fx = self.prove.build_fixture(
            self.manifest if manifest is None else manifest, self.mem, self.ws,
            config, self.config_doc, "2026-11-01", self.base,
            random.Random(rng_seed))
        self.fixtures.append(fx)
        return fx

    def contain(self, fx, payload, raw=None):
        data = raw if raw is not None else json.dumps(payload)
        return subprocess.run(
            [sys.executable, os.path.join(fx.proof, "hooks", "contain.py")],
            input=data, capture_output=True, text=True)

    def blocked_lines(self, fx):
        path = os.path.join(fx.proof, "blocked.jsonl")
        return read(path).splitlines() if os.path.isfile(path) else []


class TestContainScript(unittest.TestCase):
    def test_f2b_smuggled_command_is_refused(self):
        import prove_lib
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            os.makedirs(os.path.join(root, "_proof", "hooks"))
            script = os.path.join(root, "_proof", "hooks", "contain.py")
            with open(script, "w") as fh:
                fh.write(prove_lib.contain_script(root, [TOOL_CMD]))
            escaped = os.path.join(root, "escaped")
            for command, code in ((TOOL_CMD, 0),
                                  (TOOL_CMD + "; touch " + escaped + "x", 2),
                                  (TOOL_CMD + "; touch " + escaped, 2)):
                proc = subprocess.run(
                    [sys.executable, script], text=True, capture_output=True,
                    input=json.dumps({"tool_name": "Bash",
                                      "tool_input": {"command": command}}))
                self.assertEqual(proc.returncode, code, command)
            self.assertFalse(os.path.exists(escaped))


class TestBuildFixture(FixtureBase):
    def test_f3_copies_and_applies_without_touching_live(self):
        before = (tree_hashes(self.mem), tree_hashes(self.ws))
        fx = self.build()
        self.assertEqual((tree_hashes(self.mem), tree_hashes(self.ws)), before)
        self.assertEqual(read(os.path.join(fx.root, "CLAUDE.md")), "# Workspace\n")
        for rel in (".claude/rules/engineering.md", ".claude/tool-rules"):
            self.assertTrue(os.path.exists(os.path.join(fx.root, rel)), rel)
        index = read(os.path.join(fx.mem, "MEMORY.md"))
        self.assertNotIn("[Move](move.md)", index)
        self.assertIn("[Move](../move.md)", read(os.path.join(fx.mem, "topics", "build.md")))
        self.assertIn("type: reference", read(os.path.join(fx.mem, "move.md")))
        self.assertTrue(os.path.isfile(os.path.join(fx.root, ".claude", "rules", "new.md")))
        for name in os.listdir(os.path.join(fx.root, ".claude")):
            path = os.path.join(fx.root, ".claude", name)
            if os.path.isfile(path):
                self.assertNotIn(SETTINGS_MARKER, read(path))
        self.assertNotIn(SETTINGS_MARKER, read(os.path.join(fx.root, ".claude", "settings.json")))

    def test_f3b_symlinks_are_copied_as_files_never_linked(self):
        # A shared rules or topics dir linked in from elsewhere is a supported
        # setup, and a live session loads it through the link, so the fixture
        # copies the target's content (the same exposure as a live session). It
        # must never keep the LINK: apply_manifest rewrites topics/build.md, and
        # the proof model may Edit/Write, so a kept link writes into live memory.
        shared = os.path.join(self.tmp, "shared")
        shutil.move(os.path.join(self.mem, "topics"), os.path.join(shared, "topics"))
        os.symlink(os.path.join(shared, "topics"), os.path.join(self.mem, "topics"))
        write(os.path.join(shared, "linked-rule.md"), "# Linked rule\n")
        os.symlink(os.path.join(shared, "linked-rule.md"),
                   os.path.join(self.ws, ".claude", "rules", "linked.md"))
        before = tree_hashes(shared)
        fx = self.build()
        self.assertEqual(tree_hashes(shared), before)
        self.assertIn("[Move](../move.md)", read(os.path.join(fx.mem, "topics", "build.md")))
        self.assertEqual(read(os.path.join(fx.root, ".claude", "rules", "linked.md")),
                         "# Linked rule\n")
        for base, dirs, names in os.walk(fx.root):
            for name in dirs + names:
                self.assertFalse(os.path.islink(os.path.join(base, name)),
                                 os.path.join(base, name))

    def test_f4_posttooluse_is_the_production_entry(self):
        fx = self.build()
        settings = json.loads(read(os.path.join(fx.root, ".claude", "settings.json")))
        entry = settings["hooks"]["PostToolUse"][0]
        self.assertEqual(entry, self.prove.production_posttooluse())
        self.assertEqual(entry["matcher"], "Bash|Read|Grep|Glob|mcp__.*")
        command = entry["hooks"][0]["command"]
        self.assertTrue(command.endswith("/tools/tool-rules/claude-tool-rules"))
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", command)
        self.assertNotIn("--dir", command)

    def test_f4_missing_entry_raises(self):
        plugin = os.path.join(self.tmp, "plugin")
        os.makedirs(os.path.join(plugin, "hooks"))
        real = os.path.join(REPO_ROOT, "plugin-continuity", "hooks", "hooks.json")
        data = json.loads(read(real))
        data["hooks"]["PostToolUse"] = [
            e for e in data["hooks"]["PostToolUse"]
            if not any("tools/tool-rules/claude-tool-rules" in h["command"]
                       for h in e["hooks"])]
        write(os.path.join(plugin, "hooks", "hooks.json"), json.dumps(data))
        with self.assertRaises(self.prove.FixtureError):
            self.prove.production_posttooluse(plugin)

    def test_f5_config_and_production_resolver(self):
        fx = self.build()
        doc = json.loads(read(os.path.join(fx.proof, "config.json")))
        self.assertEqual(doc["workspace_root"], fx.root)
        self.assertEqual(doc["owner_name"], "Sam")
        self.assertTrue(os.path.isdir(os.path.join(fx.proof, "state")))
        command = self.prove.production_posttooluse()["hooks"][0]["command"]
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(CLAUDE_TOOLKIT_CONFIG=os.path.join(fx.proof, "config.json"),
                   CLAUDE_TOOL_RULES_STATE_DIR=os.path.join(fx.proof, "state"))
        proc = subprocess.run(
            command, shell=True, env=env, capture_output=True, text=True,
            input=json.dumps({"session_id": "s1", "tool_name": "Bash",
                              "tool_input": {"command": TOOL_CMD}}))
        out = json.loads(proc.stdout)
        self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        self.assertIn(fx.tokens[".claude/tool-rules/t.md"],
                      out["hookSpecificOutput"]["additionalContext"])

    def test_f6_containment_decisions(self):
        fx = self.build()
        r = fx.root
        cases = [
            ({"tool_name": "Read", "tool_input": {"file_path": os.path.join(r, "CLAUDE.md")}}, 0),
            ({"tool_name": "Read", "tool_input": {"file_path": "/etc/hosts"}}, 2),
            ({"tool_name": "Write", "tool_input": {"file_path": os.path.join(r, "src/a.py")}}, 0),
            ({"tool_name": "Write", "tool_input": {"file_path": os.path.join(r, "CLAUDE.md")}}, 2),
            ({"tool_name": "Write", "tool_input": {"file_path": os.path.join(r, "x/CLAUDE.md")}}, 2),
            ({"tool_name": "Write", "tool_input": {"file_path": os.path.join(r, ".claude/rules/a.md")}}, 2),
            ({"tool_name": "Write", "tool_input": {"file_path": os.path.join(r, "mem/MEMORY.md")}}, 2),
            ({"tool_name": "Write", "tool_input": {"file_path": os.path.join(r, "_proof/x")}}, 2),
            ({"tool_name": "Bash", "tool_input": {"command": TOOL_CMD}}, 0),
            ({"tool_name": "Bash", "tool_input": {"command": TOOL_CMD + " "}}, 2),
            ({"tool_name": "WebFetch", "tool_input": {"url": "http://x"}}, 2),
        ]
        blocked = 0
        for payload, want in cases:
            proc = self.contain(fx, payload)
            self.assertEqual(proc.returncode, want, payload)
            blocked += want == 2
            self.assertEqual(len(self.blocked_lines(fx)), blocked, payload)
        self.assertEqual(self.contain(fx, None, raw="not json").returncode, 2)
        self.assertEqual(len(self.blocked_lines(fx)), blocked + 1)

    def test_f6_glob_grep_scope(self):
        fx = self.build()
        for tool in ("Glob", "Grep"):
            for path, want in ((None, 0), ("", 0), (fx.root, 0), ("/etc", 2)):
                tool_input = {"pattern": "x"}
                if path is not None:
                    tool_input["path"] = path
                proc = self.contain(fx, {"tool_name": tool, "tool_input": tool_input})
                self.assertEqual(proc.returncode, want, (tool, path))

    def test_f6_reserved_table_matches_is_reserved(self):
        fx = self.build()
        rels = ["src/a.py", "CLAUDE.md", "x/CLAUDE.local.md", ".claude/rules/a.md",
                "_proof/x", "mem", "mem/a.md", "memory/a.md",
                "_PROOF/x", "MEM/x", ".Claude/x", "x/claude.md",
                "src/.claude/rules/x.md", "a/b/.CLAUDE/x"]
        for rel in rels:
            proc = self.contain(fx, {"tool_name": "Write",
                                     "tool_input": {"file_path": os.path.join(fx.root, rel)}})
            reserved = self.lib.is_reserved(rel, "mem")
            self.assertEqual(proc.returncode == 2, reserved, rel)

    def test_mr2_nested_dot_claude_write_is_blocked(self):
        fx = self.build()
        for rel in ("src/.claude/rules/x.md", "a/b/.CLAUDE/x"):
            proc = self.contain(fx, {"tool_name": "Write",
                                     "tool_input": {"file_path": os.path.join(fx.root, rel)}})
            self.assertEqual(proc.returncode, 2, rel)

    def test_mr3_build_fixture_never_writes_outside(self):
        import copy
        m = copy.deepcopy(self.manifest)
        m["pages"][0]["probe"] = {"path": "../escape-probe.md"}
        with self.assertRaises(self.lib.PathError):
            self.build(manifest=m)
        self.assertFalse(os.path.exists(os.path.join(self.base, "escape-probe.md")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape-probe.md")))

    def test_mr3_behaviour_file_write_never_escapes(self):
        import types
        fx = self.build()
        case = {"memory": "move.md", "files": {"../escape.txt": "x"},
                "prompt": "p", "pass_if": "q"}
        ctx = types.SimpleNamespace(fixture=fx)
        with self.assertRaises(self.lib.PathError):
            self.prove._stage_behaviour(ctx, case)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(fx.root), "escape.txt")))

    def test_p6_case_variants_are_blocked(self):
        fx = self.build()
        for rel in ("_PROOF/x", "MEM/MEMORY.md", ".Claude/rules/a.md", "docs/claude.md"):
            self.assertTrue(self.lib.is_reserved(rel, "mem"), rel)
            proc = self.contain(fx, {"tool_name": "Write",
                                     "tool_input": {"file_path": os.path.join(fx.root, rel)}})
            self.assertEqual(proc.returncode, 2, rel)

    def test_f7_stubs(self):
        fx = self.build()
        self.assertEqual(read(os.path.join(fx.root, "src", "a.py")), "probe\n")
        proc = subprocess.run(TOOL_CMD, shell=True, cwd=fx.root,
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "ok"))

    def test_f7_non_python_stub_runs(self):
        manifest = json.loads(json.dumps(self.manifest))
        command = "./_probe/x.sh graph.facebook.com"
        manifest["pages"][1]["probe"]["tool_input"]["command"] = command
        fx = self.build(manifest)
        proc = subprocess.run(command, shell=True, cwd=fx.root,
                              capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stdout.strip()), (0, "ok"))

    def test_f8_page_near_the_cap(self):
        import tool_rules_lib
        limit = tool_rules_lib.body_limit(tool_rules_lib.max_bytes(None))
        head = "---\ntools:\n  - \"Bash(*graph.facebook.com*)\"\n---\n\n"
        text = head + "x" * (limit - 20)
        self.assertEqual(len(tool_rules_lib.parse_page(text)[1].encode()) <= limit, True)
        manifest = json.loads(json.dumps(self.manifest))
        manifest["pages"][1]["content"] = text
        with self.assertRaises(self.prove.FixtureError) as ctx:
            self.build(manifest)
        self.assertIn(".claude/tool-rules/t.md", str(ctx.exception))
        self.assertTrue(os.path.isdir(ctx.exception.root))
        shutil.rmtree(ctx.exception.root)

    def test_tokens_unique_and_in_pages(self):
        fx = self.build()
        token = fx.tokens[".claude/tool-rules/t.md"]
        self.assertRegex(token, r"^IDXCHECK-[0-9A-F]{6}$")
        page = [p for p in fx.pages if p[0]["file"] == ".claude/tool-rules/t.md"][0]
        self.assertIn("Fixture check token: " + token, read(page[1]))

    def test_f9_surface_and_snapshots(self):
        fx = self.build()
        write(os.path.join(fx.root, "_proof", "loaded.jsonl"), "x\n")
        surface = self.prove.surface_hashes(fx.root)
        for rel in ("CLAUDE.md", ".claude/settings.json", "mem/MEMORY.md"):
            self.assertIn(rel, surface)
        for rel in ("_proof/loaded.jsonl", "src/a.py"):
            self.assertNotIn(rel, surface)
        self.assertEqual(surface, fx.surface)
        snap = self.prove.snapshot(fx.root)
        self.assertIn("src/a.py", snap)
        self.assertNotIn("CLAUDE.md", snap)
        write(os.path.join(fx.root, "new.txt"), "n")
        write(os.path.join(fx.root, "_probe", "m.py"), "changed\n")
        changed = self.prove.changed_files(fx.root, snap)
        self.assertEqual(sorted(changed), ["_probe/m.py", "new.txt"])
        self.assertEqual(changed["new.txt"], "n")
        self.prove.restore(fx.root, snap)
        self.assertEqual(self.prove.snapshot(fx.root), snap)
        self.assertFalse(os.path.exists(os.path.join(fx.root, "new.txt")))

    def test_f10_structural(self):
        fx = self.build()
        ok, detail = self.prove.structural(fx.mem, None)
        self.assertTrue(ok, detail)
        import lint_lib
        chars = lint_lib.scan(fx.mem)["index_chars"]
        ok, detail = self.prove.structural(fx.mem, Cfg(distill_at_chars=chars))
        self.assertFalse(ok)
        self.assertIn(str(chars), detail)
        ok, detail = self.prove.structural(fx.mem, Cfg(max_hook_chars=5))
        self.assertFalse(ok)
        index = os.path.join(fx.mem, "MEMORY.md")
        write(index, read(index) + "- [Ghost](ghost.md) — no such file\n")
        ok, detail = self.prove.structural(fx.mem, None)
        self.assertFalse(ok)

    def test_f11_fixture_base(self):
        code = ("import sys; sys.path.insert(0, %r); import prove_lib; "
                "print(prove_lib.fixture_base())" % TOOL_DIR)
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        wanted = os.path.join(self.tmp, "chosen")
        write(os.path.join(self.tmp, "c1.json"),
              json.dumps({"index_distill": {"fixture_root": wanted}}))
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(self.tmp, "c1.json")
        out = subprocess.run([sys.executable, "-I", "-c", code], env=env,
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), wanted, out.stderr)
        self.assertTrue(os.path.isdir(wanted))
        write(os.path.join(self.tmp, "c2.json"), "{}")
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(self.tmp, "c2.json")
        out = subprocess.run([sys.executable, "-I", "-c", code], env=env,
                             capture_output=True, text=True)
        default = os.path.join(tempfile.gettempdir(), "claude-index-distill")
        self.assertEqual(out.stdout.strip(), default, out.stderr)
        self.assertTrue(os.path.isdir(default))


class TestDispose(FixtureBase):
    def test_dispose_removes_root_unless_keep_and_never_raises(self):
        fx = self.build()
        self.prove.dispose(fx.root, [], [], True)
        self.assertTrue(os.path.isdir(fx.root))
        self.prove.dispose(fx.root, [{"cwd": "/nonexistent", "init": None}], ["/nonexistent"], False)
        self.assertFalse(os.path.exists(fx.root))
        self.prove.dispose(fx.root, [None], [None], False)


if __name__ == "__main__":
    unittest.main()
