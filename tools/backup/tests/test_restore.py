"""Restore tests (hermetic: tempfile only). Round trip uses real stage/manifest/publish."""
import json
import ntpath
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import backup_lib  # noqa: E402

NOW = datetime(2026, 10, 8, 14, 2, 3)
HOST = "testhost"
CONTENT = {"a.txt": b"alpha\n", os.path.join("sub", "b.txt"): b"bravo\n"}


class RestoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = os.path.join(self.tmp, "src")
        for rel, data in CONTENT.items():
            full = os.path.join(self.src, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "wb") as handle:
                handle.write(data)
        os.symlink("a.txt", os.path.join(self.src, "link"))
        item = {"source": self.src, "dest_rel": "ws", "required": True,
                "status": "pending", "skip_paths": []}
        staging = os.path.join(self.tmp, "staging")
        parts = backup_lib.stage([item], staging, [])
        manifest = backup_lib.write_manifest(staging, [item], parts, HOST,
                                             "2026-10-08T14:02:03Z", None)
        self.dest = os.path.join(self.tmp, "dest")
        os.makedirs(self.dest)
        self.folder = backup_lib.publish(staging, self.dest, HOST, manifest, NOW)
        self.target = os.path.join(self.tmp, "restored")

    def rewrite_manifest(self, mutate):
        path = os.path.join(self.folder, "MANIFEST.json")
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        mutate(data)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)


class TestRestore(RestoreBase):
    def test_round_trip_byte_identical(self):
        code, lines = backup_lib.restore(self.folder, self.target)
        self.assertEqual(code, 0, lines)
        for rel, data in CONTENT.items():
            with open(os.path.join(self.target, "ws", rel), "rb") as handle:
                self.assertEqual(handle.read(), data)
        self.assertTrue(any("→" in l and os.path.join(self.target, "ws") in l
                            for l in lines), lines)

    def test_existing_target_refused_nothing_written(self):
        os.makedirs(self.target)
        code, lines = backup_lib.restore(self.folder, self.target)
        self.assertEqual(code, 2)
        self.assertEqual(os.listdir(self.target), [])

    def test_tampered_file_exit_1_names_it(self):
        with open(os.path.join(self.folder, "ws", "a.txt"), "wb") as handle:
            handle.write(b"tampered")
        code, lines = backup_lib.restore(self.folder, self.target)
        self.assertEqual(code, 1)
        self.assertTrue(any("ws/a.txt" in l for l in lines), lines)

    def test_deleted_file_exit_1_names_it(self):
        os.remove(os.path.join(self.folder, "ws", "sub", "b.txt"))
        code, lines = backup_lib.restore(self.folder, self.target)
        self.assertEqual(code, 1)
        self.assertTrue(any("ws/sub/b.txt" in l for l in lines), lines)

    def test_malformed_manifest_exit_2(self):
        with open(os.path.join(self.folder, "MANIFEST.json"), "w") as handle:
            handle.write("{not json")
        code, lines = backup_lib.restore(self.folder, self.target)
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.target))

    def test_symlinks_only_in_symlinks_txt(self):
        code, _ = backup_lib.restore(self.folder, self.target)
        self.assertEqual(code, 0)
        self.assertFalse(os.path.lexists(os.path.join(self.target, "ws", "link")))
        for _root, dirs, files in os.walk(self.target):
            for name in dirs + files:
                self.assertFalse(os.path.islink(os.path.join(_root, name)))
        with open(os.path.join(self.target, "SYMLINKS.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "ws/link -> a.txt")

    def test_hostile_manifest_paths_rejected(self):
        for bad in ("../escape", "a/../../escape", "/abs", "C:../escape",
                    "C:\\escape", "..\\escape"):
            with self.subTest(path=bad):
                if os.path.exists(self.target):
                    shutil.rmtree(self.target)
                self.rewrite_manifest(lambda d, b=bad: d["files"].append(
                    {"path": b, "type": "file", "size": 1, "sha256": "0" * 64}))
                code, lines = backup_lib.restore(self.folder, self.target)
                self.assertEqual(code, 1, lines)
                self.assertTrue(any(bad in l for l in lines), lines)
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "escape")))
                self.assertFalse(os.path.exists(os.path.join(self.tmp, "abs")))
                self.rewrite_manifest(lambda d: d["files"].pop())


class TestSafeRel(unittest.TestCase):
    def setUp(self):
        self.target = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.target, True)

    def test_rejects_every_spelling(self):
        for bad in ("../escape", "a/../../escape", "/abs", "C:../escape",
                    "C:\\escape", "..\\escape", "a\\..\\b", "\\rooted", ""):
            with self.subTest(path=bad):
                with self.assertRaises(ValueError):
                    backup_lib.safe_rel(bad, self.target)

    def test_windows_spellings_use_ntpath(self):
        self.assertTrue(ntpath.splitdrive("C:../escape")[0])
        self.assertTrue(ntpath.isabs("C:\\escape"))

    def test_accepts_normal_path(self):
        dest = backup_lib.safe_rel("ws/sub/b.txt", self.target)
        self.assertEqual(dest, os.path.join(self.target, "ws", "sub", "b.txt"))

    def test_rejects_symlink_escape(self):
        outside = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        os.symlink(outside, os.path.join(self.target, "hop"))
        with self.assertRaises(ValueError):
            backup_lib.safe_rel("hop/x", self.target)


if __name__ == "__main__":
    unittest.main()
