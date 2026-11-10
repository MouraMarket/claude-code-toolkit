"""Inventory, staging and scan tests for backup_lib (hermetic: tempfile only)."""
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import backup_lib  # noqa: E402
import secret_lib  # noqa: E402

# Built at runtime so this file itself is never flagged; high-entropy on purpose.
SECRET = "AKIA" + "J7QZ3MWK9LP2RTX4"
SECRET_LINE = "key = " + SECRET + "\n"


def write(path, content="hello\n", binary=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if binary else "w") as handle:
        handle.write(content)


def paths(entries):
    return sorted(e["path"] for e in entries)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        self.claude = os.path.join(self.home, ".claude")
        self.staging = os.path.join(self.tmp, "staging")
        os.makedirs(self.claude)
        # Positive control: the planted secret must really be flagged.
        self.assertTrue(secret_lib.scan_text(SECRET_LINE, "x"))

    def stage_dir(self, root, skip=(), excludes=()):
        item = {"source": root, "dest_rel": "ws", "required": True,
                "status": "pending", "skip_paths": list(skip)}
        parts = backup_lib.stage([item], self.staging, list(excludes))
        return item, parts


class TestScan(Base):
    def test_secret_unscannable_and_excluded_never_staged(self):
        root = os.path.join(self.tmp, "ws")
        write(os.path.join(root, "ok.txt"))
        write(os.path.join(root, "leak.txt"), SECRET_LINE)
        write(os.path.join(root, "bin.dat"), b"\xff\xfe\x00\x80", binary=True)
        write(os.path.join(root, ".env"), "A=1\n")
        write(os.path.join(root, "node_modules", "m.js"), "x")
        item, parts = self.stage_dir(root)
        self.assertEqual(item["status"], "copied")
        reasons = dict((e["path"], e["reason"]) for e in parts["excluded"])
        self.assertEqual(reasons["ws/leak.txt"], "secret")
        self.assertEqual(reasons["ws/bin.dat"], "unscannable")
        self.assertEqual(reasons["ws/.env"], "pattern")
        self.assertEqual(reasons["ws/node_modules"], "pattern")
        detail = [e for e in parts["excluded"] if e["reason"] == "secret"][0]["detail"]
        self.assertIn("line 1", detail)
        self.assertNotIn(SECRET, detail)
        for name in ("leak.txt", "bin.dat", ".env", "node_modules"):
            self.assertFalse(os.path.lexists(os.path.join(self.staging, "ws", name)), name)
        self.assertTrue(os.path.isfile(os.path.join(self.staging, "ws", "ok.txt")))
        self.assertEqual(paths(parts["files"]), ["ws/ok.txt"])
        self.assertEqual(parts["files"][0]["size"], 6)
        self.assertEqual(len(parts["files"][0]["sha256"]), 64)

    def test_nothing_unvetted_written_anywhere(self):
        root = os.path.join(self.tmp, "ws")
        write(os.path.join(root, "leak.txt"), SECRET_LINE)
        self.stage_dir(root)
        for dirpath, _, names in os.walk(self.staging):
            for name in names:
                with open(os.path.join(dirpath, name), "rb") as handle:
                    self.assertNotIn(SECRET.encode(), handle.read())

    def test_cli_skips_do_not_apply(self):
        root = os.path.join(self.tmp, "ws")
        write(os.path.join(root, "big.txt"), "x" * (3 * 1024 * 1024) + "\n" + SECRET_LINE)
        write(os.path.join(root, "build", "out.txt"), SECRET_LINE)
        for i in range(20):
            write(os.path.join(root, "bins", "b%d.bin" % i), b"\xff\x00" * 50, binary=True)
        write(os.path.join(root, "bins", "notes.txt"), SECRET_LINE)
        _, parts = self.stage_dir(root)
        secret_paths = [e["path"] for e in parts["excluded"] if e["reason"] == "secret"]
        self.assertEqual(sorted(secret_paths),
                         ["ws/big.txt", "ws/bins/notes.txt", "ws/build/out.txt"])

    def test_user_patterns_basename_and_relpath(self):
        root = os.path.join(self.tmp, "ws")
        write(os.path.join(root, "a", "keep.txt"))
        write(os.path.join(root, "a", "skip.log"))
        write(os.path.join(root, "b", "x.txt"))
        _, parts = self.stage_dir(root, excludes=["*.log", "b"])
        self.assertEqual(paths(parts["files"]), ["ws/a/keep.txt"])
        self.assertEqual(paths(parts["excluded"]), ["ws/a/skip.log", "ws/b"])

    def test_symlinks_recorded_not_followed(self):
        root = os.path.join(self.tmp, "ws")
        outside = os.path.join(self.tmp, "outside")
        write(os.path.join(outside, "secret-doc.txt"), "private\n")
        write(os.path.join(root, "real.txt"))
        os.symlink(outside, os.path.join(root, "linkdir"))
        os.symlink(os.path.join(outside, "secret-doc.txt"), os.path.join(root, "linkfile"))
        _, parts = self.stage_dir(root)
        links = dict((f["path"], f["link_target"]) for f in parts["files"]
                     if f["type"] == "symlink")
        self.assertEqual(links, {"ws/linkdir": outside,
                                 "ws/linkfile": os.path.join(outside, "secret-doc.txt")})
        self.assertFalse(os.path.lexists(os.path.join(self.staging, "ws", "linkdir")))
        self.assertFalse(os.path.lexists(os.path.join(self.staging, "ws", "linkfile")))
        self.assertNotIn("ws/linkdir/secret-doc.txt", paths(parts["files"]))

    def test_file_item_lands_at_dest_rel(self):
        cfg = os.path.join(self.tmp, "config.json")
        write(cfg, '{"a": 1}\n')
        item = {"source": cfg, "dest_rel": "toolkit-config/config.json",
                "required": False, "status": "pending", "skip_paths": []}
        parts = backup_lib.stage([item], self.staging, [])
        self.assertTrue(os.path.isfile(
            os.path.join(self.staging, "toolkit-config", "config.json")))
        self.assertEqual(paths(parts["files"]), ["toolkit-config/config.json"])
        self.assertEqual(item["status"], "copied")

    def test_utf16_without_bom_is_unscannable(self):
        """NUL is valid UTF-8, so BOM-less UTF-16 decodes and hides every
        secret pattern behind interleaved NULs. It must never be staged."""
        root = os.path.join(self.tmp, "ws")
        write(os.path.join(root, "wide.txt"), SECRET_LINE.encode("utf-16-le"), binary=True)
        with open(os.path.join(root, "wide.txt"), "rb") as handle:
            data = handle.read()
        data.decode("utf-8")  # control: it really does decode
        self.assertFalse(secret_lib.scan_text(data.decode("utf-8")))  # and hides the key
        _, parts = self.stage_dir(root)
        self.assertEqual([(e["path"], e["reason"]) for e in parts["excluded"]],
                         [("ws/wide.txt", "unscannable")])
        self.assertFalse(os.path.lexists(os.path.join(self.staging, "ws", "wide.txt")))

    def test_file_item_is_excluded_by_its_own_name(self):
        """A file named directly (an `extra_paths` entry) gets the same exclude
        rules as a file found in a walk -- built-in, user glob, and a symlink
        whose target is an excluded name. Contents are clean, so only the name
        rule can keep them out."""
        env = os.path.join(self.tmp, ".env")
        write(env, "A=1\n")
        note = os.path.join(self.tmp, "notes.private")
        write(note, "clean\n")
        alias = os.path.join(self.tmp, "innocent.txt")
        os.symlink(env, alias)
        items = [{"source": src, "dest_rel": "extra/%d-%s" % (i, os.path.basename(src)),
                  "required": False, "status": "pending", "skip_paths": []}
                 for i, src in enumerate((env, note, alias), start=1)]
        parts = backup_lib.stage(items, self.staging, ["*.private"])
        self.assertEqual(parts["files"], [])
        self.assertEqual(sorted((e["path"], e["reason"]) for e in parts["excluded"]),
                         [("extra/1-.env", "pattern"),
                          ("extra/2-notes.private", "pattern"),
                          ("extra/3-innocent.txt", "pattern")])
        self.assertFalse(os.path.exists(os.path.join(self.staging, "extra")))

    def test_file_item_with_secret_excluded_at_dest_rel(self):
        cfg = os.path.join(self.tmp, "config.json")
        write(cfg, SECRET_LINE)
        item = {"source": cfg, "dest_rel": "toolkit-config/config.json",
                "required": False, "status": "pending", "skip_paths": []}
        parts = backup_lib.stage([item], self.staging, [])
        self.assertEqual(parts["excluded"][0]["path"], "toolkit-config/config.json")
        self.assertEqual(parts["excluded"][0]["reason"], "secret")
        self.assertFalse(os.path.exists(os.path.join(self.staging, "toolkit-config")))


class TestSkips(Base):
    def make_ws(self):
        ws = os.path.join(self.tmp, "ws-real")
        write(os.path.join(ws, "CLAUDE.md"))
        write(os.path.join(ws, "repos", "r1", "f.txt"))
        write(os.path.join(ws, "workspace", "notes.md"))
        return ws

    def inventory(self, ws, repos):
        return backup_lib.build_inventory(ws, self.home, self.claude, None, [], repos)

    def test_repos_absent_and_nothing_else_skipped(self):
        ws = self.make_ws()
        items = self.inventory(ws, os.path.join(ws, "repos"))
        parts = backup_lib.stage(items, self.staging, [])
        got = paths(parts["files"])
        self.assertIn("workspace-root/CLAUDE.md", got)
        self.assertIn("workspace-root/workspace/notes.md", got)
        self.assertFalse([p for p in got if "/repos/" in p])
        self.assertEqual(paths(parts["excluded"]), ["workspace-root/repos"])
        self.assertEqual(items[0]["skip_paths"], [os.path.join(ws, "repos")])

    def test_a_repos_root_at_or_above_the_workspace_root_skips_all_of_it(self):
        # The walk compares only entries inside the root, never the root itself.
        ws = self.make_ws()
        for repos in (ws, os.path.dirname(ws)):
            with self.subTest(repos=repos):
                staging = os.path.join(self.tmp, "staging-" + str(len(repos)))
                items = self.inventory(ws, repos)
                parts = backup_lib.stage(items, staging, [])
                self.assertFalse([p for p in paths(parts["files"])
                                  if p.startswith("workspace-root")])
                self.assertEqual(paths(parts["excluded"]), ["workspace-root"])
                self.assertEqual(items[0]["status"], "copied")

    def test_symlinked_workspace_root_still_skips_repos(self):
        ws = self.make_ws()
        link = os.path.join(self.tmp, "ws-link")
        os.symlink(ws, link)
        items = self.inventory(link, os.path.join(link, "repos"))
        parts = backup_lib.stage(items, self.staging, [])
        got = paths(parts["files"])
        self.assertIn("workspace-root/CLAUDE.md", got)
        self.assertFalse([p for p in got if "/repos/" in p])


class TestMemory(Base):
    def settings(self, value):
        write(os.path.join(self.claude, "settings.json"), json.dumps(value))

    def inv(self):
        return backup_lib.build_inventory(None, self.home, self.claude, None, [], None)

    def test_per_project_memory_without_configured_dir(self):
        write(os.path.join(self.claude, "projects", "p1", "memory", "a.md"))
        write(os.path.join(self.claude, "projects", "p2", "memory", "b.md"))
        items = self.inv()
        rels = [i["dest_rel"] for i in items]
        self.assertEqual(rels.count("memory/p1"), 1)
        self.assertIn("memory/p2", rels)
        self.assertNotIn("memory", rels)
        parts = backup_lib.stage(items, self.staging, [])
        self.assertEqual(paths(parts["files"]), ["memory/p1/a.md", "memory/p2/b.md"])

    def test_no_memory_anywhere_is_one_missing_required_item(self):
        items = self.inv()
        mem = [i for i in items if i["dest_rel"] == "memory"]
        self.assertEqual(len(mem), 1)
        self.assertEqual((mem[0]["source"], mem[0]["status"], mem[0]["required"]),
                         (None, "missing", True))

    def test_configured_equal_to_project_dir_staged_once(self):
        proj = os.path.join(self.claude, "projects", "p1", "memory")
        write(os.path.join(proj, "a.md"))
        self.settings({"autoMemoryDirectory": proj})
        items = self.inv()
        self.assertEqual([i["dest_rel"] for i in items
                          if i["dest_rel"].startswith("memory")], ["memory/configured"])
        parts = backup_lib.stage(items, self.staging, [])
        self.assertEqual([p for p in paths(parts["files"]) if p.startswith("memory")],
                         ["memory/configured/a.md"])

    def test_custom_claude_dir_with_outside_configured_dir(self):
        custom = os.path.join(self.tmp, "custom-claude")
        active = os.path.join(self.tmp, "elsewhere", "memory")
        write(os.path.join(active, "new.md"))
        write(os.path.join(custom, "projects", "x", "memory", "old.md"))
        write(os.path.join(custom, "settings.json"),
              json.dumps({"autoMemoryDirectory": active}))
        # A decoy under <home>/.claude must NOT be read.
        write(os.path.join(self.claude, "settings.json"),
              json.dumps({"autoMemoryDirectory": os.path.join(self.tmp, "decoy")}))
        items = backup_lib.build_inventory(None, self.home, custom, None, [], None)
        parts = backup_lib.stage(items, self.staging, [])
        got = paths(parts["files"])
        self.assertIn("memory/configured/new.md", got)
        self.assertIn("memory/x/old.md", got)
        self.assertFalse([i for i in items if "decoy" in str(i["source"])])

    def test_configured_value_bad_shapes_mean_not_configured(self):
        sp = os.path.join(self.claude, "settings.json")
        self.assertIsNone(backup_lib.configured_memory_dir(self.claude, self.home))
        for raw in ("{not json", "[1]", json.dumps({"autoMemoryDirectory": 5}),
                    json.dumps({})):
            write(sp, raw)
            self.assertIsNone(backup_lib.configured_memory_dir(self.claude, self.home), raw)

    def test_configured_missing_dir_is_a_missing_item(self):
        self.settings({"autoMemoryDirectory": os.path.join(self.tmp, "gone", "mem")})
        items = self.inv()
        cfg = [i for i in items if i["dest_rel"] == "memory/configured"][0]
        self.assertEqual(cfg["status"], "missing")
        self.assertNotIn("memory", [i["dest_rel"] for i in items])

    def test_tilde_value_expands_against_home(self):
        mem = os.path.join(self.home, "mem")
        write(os.path.join(mem, "a.md"))
        self.settings({"autoMemoryDirectory": "~/mem"})
        items = self.inv()
        cfg = [i for i in items if i["dest_rel"] == "memory/configured"][0]
        self.assertEqual(cfg["source"], mem)


class TestInventoryShape(Base):
    def test_order_extras_and_workspace_none(self):
        cfg = os.path.join(self.tmp, "config.json")
        write(cfg, "{}")
        write(os.path.join(self.claude, "settings.json"), "{}")
        extra = os.path.join(self.tmp, "notes")
        write(os.path.join(extra, "n.txt"))
        items = backup_lib.build_inventory(None, self.home, self.claude, cfg,
                                           [extra, os.path.join(self.tmp, "nope")], None)
        self.assertEqual([i["dest_rel"] for i in items], [
            "workspace-root", "memory", "toolkit-config/config.json",
            "claude-settings/settings.json", "extra/1-notes", "extra/2-nope"])
        self.assertEqual(items[0]["status"], "missing")
        self.assertIsNone(items[0]["source"])
        self.assertTrue(items[0]["required"])
        self.assertEqual(items[-1]["status"], "missing")
        self.assertFalse(items[-1]["required"])

    def test_workspace_none_makes_incomplete_manifest(self):
        items = backup_lib.build_inventory(None, self.home, self.claude, None, [], None)
        parts = backup_lib.stage(items, self.staging, [])
        manifest = backup_lib.write_manifest(
            self.staging, items, parts, "h", "2026-10-08T14:02:03Z", None)
        self.assertFalse(manifest["complete"])
        with open(os.path.join(self.staging, "MANIFEST.json")) as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk, manifest)
        self.assertNotIn("skip_paths", on_disk["items"][0])

    def test_optional_missing_keeps_complete(self):
        ws = os.path.join(self.tmp, "ws")
        write(os.path.join(ws, "a.txt"))
        write(os.path.join(self.claude, "projects", "p", "memory", "m.md"))
        items = backup_lib.build_inventory(ws, self.home, self.claude, None,
                                           [os.path.join(self.tmp, "nope")], None)
        parts = backup_lib.stage(items, self.staging, [])
        manifest = backup_lib.write_manifest(
            self.staging, items, parts, "h", "2026-10-08T14:02:03Z", "0f1e2d")
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["rev"], 1)
        self.assertEqual(manifest["toolkit_commit"], "0f1e2d")

    def test_host_name(self):
        name = backup_lib.host_name()
        self.assertRegex(name, r"^[A-Za-z0-9._-]+$")

    def test_host_name_prefers_the_stable_name(self):
        class Done(object):
            stdout = "Example-Mac\n"
        calls = []
        def run(cmd, **kwargs):
            calls.append(cmd)
            return Done()
        self.assertEqual(backup_lib.host_name("darwin", {}, run), "Example-Mac")
        self.assertEqual(calls, [["scutil", "--get", "LocalHostName"]])
        self.assertEqual(backup_lib.host_name("win32", {"COMPUTERNAME": "WORK PC"}, run),
                         "WORK-PC")

    def test_host_name_falls_back_when_the_stable_name_is_unavailable(self):
        def broken(cmd, **kwargs):
            raise OSError("no scutil")
        fallback = re.sub(r"[^A-Za-z0-9._-]", "-", socket.gethostname()) or "unknown-host"
        self.assertEqual(backup_lib.host_name("darwin", {}, broken), fallback)
        self.assertEqual(backup_lib.host_name("win32", {}, broken), fallback)
        self.assertEqual(backup_lib.host_name("linux", {}, broken), fallback)


class TestDestination(Base):
    def items_for(self, source):
        return [{"source": source, "dest_rel": "ws", "required": True,
                 "status": "pending", "skip_paths": []}]

    def test_missing_destination_raises(self):
        with self.assertRaises(backup_lib.BackupAbort):
            backup_lib.check_destination(os.path.join(self.tmp, "nope"), "h", [])

    def test_destination_inside_workspace_raises(self):
        ws = os.path.join(self.tmp, "ws")
        dest = os.path.join(ws, "drive")
        os.makedirs(dest)
        with self.assertRaises(backup_lib.BackupAbort):
            backup_lib.check_destination(dest, "h", self.items_for(ws))

    def test_publish_dir_equal_or_under_source_raises(self):
        dest = os.path.join(self.tmp, "drive")
        os.makedirs(dest)
        for source in (os.path.join(dest, "h"), os.path.join(dest, "h", "sub")):
            os.makedirs(source, exist_ok=True)
            with self.assertRaises(backup_lib.BackupAbort):
                backup_lib.check_destination(dest, "h", self.items_for(source))

    def test_source_under_publish_dir_when_destination_is_inside_source(self):
        # destination itself is clean of the source, but <destination>/<host> is
        # an ancestor of it.
        dest = os.path.join(self.tmp, "drive")
        src = os.path.join(dest, "h", "inner")
        os.makedirs(src)
        with self.assertRaises(backup_lib.BackupAbort):
            backup_lib.check_destination(dest, "h", self.items_for(src))

    def test_publish_dir_under_source_even_if_not_created(self):
        src = os.path.join(self.tmp, "src")
        dest = os.path.join(src, "drive")
        os.makedirs(dest)
        with self.assertRaises(backup_lib.BackupAbort):
            backup_lib.check_destination(dest, "h", self.items_for(src))

    def test_string_prefix_sibling_is_fine(self):
        dest = os.path.join(self.tmp, "drive")
        os.makedirs(dest)
        sibling = os.path.join(dest, "h-other")
        os.makedirs(sibling)
        backup_lib.check_destination(dest, "h", self.items_for(sibling))

    def test_none_source_ignored_and_clean_passes(self):
        dest = os.path.join(self.tmp, "drive")
        os.makedirs(dest)
        src = os.path.join(self.tmp, "src")
        os.makedirs(src)
        items = self.items_for(src) + [{"source": None, "dest_rel": "m",
                                        "required": True, "status": "missing",
                                        "skip_paths": []}]
        backup_lib.check_destination(dest, "h", items)


@unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                 "permission tests need a non-root POSIX user")
class TestUnreadable(Base):
    def setUp(self):
        super(TestUnreadable, self).setUp()
        self.locked = []
        self.addCleanup(self.restore)

    def restore(self):
        for path, mode in self.locked:
            if os.path.lexists(path):
                os.chmod(path, mode)

    def lock(self, path):
        self.locked.append((path, os.stat(path).st_mode))
        os.chmod(path, 0)

    def test_unreadable_project_dir_makes_backup_incomplete(self):
        write(os.path.join(self.claude, "projects", "readable", "memory", "a.md"))
        locked = os.path.join(self.claude, "projects", "locked")
        write(os.path.join(locked, "memory", "b.md"))
        self.lock(locked)
        items = backup_lib.build_inventory(None, self.home, self.claude, None, [], None)
        status = dict((i["dest_rel"], i["status"]) for i in items)
        self.assertEqual(status.get("memory/locked"), "unreadable")
        self.assertEqual(status.get("memory/readable"), "pending")
        parts = backup_lib.stage(items, self.staging, [])
        manifest = backup_lib.write_manifest(self.staging, items, parts, "h",
                                             "2026-10-08T00:00:00Z", None)
        self.assertIs(manifest["complete"], False)

    def test_unreadable_projects_dir_is_an_unreadable_required_item(self):
        projects = os.path.join(self.claude, "projects")
        write(os.path.join(projects, "p1", "memory", "a.md"))
        self.lock(projects)
        items = backup_lib.build_inventory(None, self.home, self.claude, None, [], None)
        mem = [i for i in items if i["dest_rel"] == "memory"]
        self.assertEqual(len(mem), 1)
        self.assertEqual((mem[0]["required"], mem[0]["status"]), (True, "unreadable"))

    def test_unreadable_file_aborts(self):
        root = os.path.join(self.tmp, "ws")
        write(os.path.join(root, "ok.txt"))
        bad = os.path.join(root, "bad.txt")
        write(bad)
        self.lock(bad)
        with self.assertRaises(backup_lib.BackupAbort):
            self.stage_dir(root)

    def test_unreadable_subdirectory_aborts(self):
        root = os.path.join(self.tmp, "ws")
        write(os.path.join(root, "sub", "f.txt"))
        self.lock(os.path.join(root, "sub"))
        with self.assertRaises(backup_lib.BackupAbort):
            self.stage_dir(root)

    def test_unreadable_root_marks_item_unreadable(self):
        root = os.path.join(self.tmp, "ws")
        write(os.path.join(root, "f.txt"))
        self.lock(root)
        item, parts = self.stage_dir(root)
        self.assertEqual(item["status"], "unreadable")
        self.assertEqual(parts, {"files": [], "excluded": []})

    def test_unreadable_required_root_is_incomplete(self):
        root = os.path.join(self.tmp, "ws")
        write(os.path.join(root, "f.txt"))
        self.lock(root)
        item, parts = self.stage_dir(root)
        manifest = backup_lib.write_manifest(
            self.staging, [item], parts, "h", "2026-10-08T14:02:03Z", None)
        self.assertFalse(manifest["complete"])


if __name__ == "__main__":
    unittest.main()
