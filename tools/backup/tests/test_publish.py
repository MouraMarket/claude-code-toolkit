"""Publish, prune, status and manifest-validation tests (hermetic: tempfile only)."""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
import backup_lib  # noqa: E402

NOW = datetime(2026, 10, 8, 14, 2, 3)  # naive UTC
HOST = "testhost"


def good_manifest(created="2026-10-08T14:00:00Z", complete=True, files=None):
    return {"rev": 1, "host": HOST, "created_at": created, "toolkit_commit": None,
            "complete": complete,
            "items": [{"source": "/x", "dest_rel": "ws", "required": True,
                       "status": "copied"}],
            "files": files or [], "excluded": []}


def write_json(folder, data):
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "MANIFEST.json"), "w", encoding="utf-8") as handle:
        json.dump(data, handle)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dest = os.path.join(self.tmp, "dest")
        self.host_dir = os.path.join(self.dest, HOST)
        os.makedirs(self.host_dir)

    def make_backup(self, stamp, **kwargs):
        folder = os.path.join(self.host_dir, stamp)
        write_json(folder, good_manifest(**kwargs))
        return folder


class TestPublish(Base):
    def make_staging(self):
        staging = os.path.join(self.tmp, "staging")
        os.makedirs(os.path.join(staging, "ws"))
        data = b"hello\n"
        with open(os.path.join(staging, "ws", "a.txt"), "wb") as handle:
            handle.write(data)
        files = [{"path": "ws/a.txt", "type": "file", "size": len(data),
                  "sha256": hashlib.sha256(data).hexdigest()}]
        manifest = good_manifest(files=files)
        write_json(staging, manifest)
        return staging, manifest

    def test_publish_leaves_one_verified_stamp_folder(self):
        staging, manifest = self.make_staging()
        final = backup_lib.publish(staging, self.dest, HOST, manifest, NOW)
        self.assertEqual(final, os.path.join(self.host_dir, "2026-10-08T140203Z"))
        self.assertEqual(os.listdir(self.host_dir), ["2026-10-08T140203Z"])
        with open(os.path.join(final, "ws", "a.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"hello\n")
        self.assertEqual(backup_lib.load_manifest(final)["rev"], 1)

    def test_aware_and_naive_now_agree(self):
        aware = NOW.replace(tzinfo=timezone.utc)
        self.assertEqual(backup_lib.utc_stamp(aware), backup_lib.utc_stamp(NOW))

    def test_tampered_staging_aborts_and_leaves_no_stamp_folder(self):
        staging, manifest = self.make_staging()
        with open(os.path.join(staging, "ws", "a.txt"), "wb") as handle:
            handle.write(b"tampered\n")
        with self.assertRaises(backup_lib.BackupAbort):
            backup_lib.publish(staging, self.dest, HOST, manifest, NOW)
        self.assertEqual(os.listdir(self.host_dir), [])

    def test_missing_file_aborts(self):
        staging, manifest = self.make_staging()
        os.remove(os.path.join(staging, "ws", "a.txt"))
        with self.assertRaises(backup_lib.BackupAbort):
            backup_lib.publish(staging, self.dest, HOST, manifest, NOW)
        self.assertEqual(os.listdir(self.host_dir), [])

    def test_existing_target_aborts_and_is_untouched(self):
        staging, manifest = self.make_staging()
        target = self.make_backup("2026-10-08T140203Z")
        with self.assertRaises(backup_lib.BackupAbort):
            backup_lib.publish(staging, self.dest, HOST, manifest, NOW)
        self.assertEqual(backup_lib.load_manifest(target)["created_at"],
                         "2026-10-08T14:00:00Z")

    def test_creates_host_dir_when_absent(self):
        staging, manifest = self.make_staging()
        shutil.rmtree(self.host_dir)
        final = backup_lib.publish(staging, self.dest, HOST, manifest, NOW)
        self.assertTrue(os.path.isdir(final))


class TestPrune(Base):
    def test_incomplete_backups_never_evict_the_last_complete_one(self):
        self.make_backup("2026-09-01T120000Z")
        for day in range(2, 10):
            self.make_backup("2026-09-%02dT120000Z" % day, complete=False)
        deleted = backup_lib.prune(self.host_dir, 8, NOW)
        self.assertEqual(deleted, [])
        self.assertTrue(os.path.isdir(os.path.join(self.host_dir, "2026-09-01T120000Z")))
        # Once a newer complete one exists, the old complete one ages out normally.
        self.make_backup("2026-09-10T120000Z")
        deleted = backup_lib.prune(self.host_dir, 8, NOW)
        self.assertEqual(sorted(deleted), ["2026-09-01T120000Z", "2026-09-02T120000Z"])

    def test_keeps_newest_and_ignores_foreign_folders(self):
        for day in range(1, 11):
            self.make_backup("2026-09-%02dT120000Z" % day)
        os.makedirs(os.path.join(self.host_dir, "notes"))
        os.makedirs(os.path.join(self.host_dir, "2026-01-01T000000Z"))  # no manifest
        write_json(os.path.join(self.host_dir, "2026-01-02T000000Z"),
                   dict(good_manifest(), rev=99))
        deleted = backup_lib.prune(self.host_dir, 8, NOW)
        self.assertEqual(sorted(deleted),
                         ["2026-09-01T120000Z", "2026-09-02T120000Z"])
        remaining = sorted(os.listdir(self.host_dir))
        self.assertEqual(len(remaining), 8 + 3)
        for name in ("notes", "2026-01-01T000000Z", "2026-01-02T000000Z",
                     "2026-09-10T120000Z", "2026-09-03T120000Z"):
            self.assertIn(name, remaining)

    def test_incomplete_age_threshold(self):
        old = os.path.join(self.host_dir, ".incomplete-" + backup_lib.utc_stamp(
            NOW - timedelta(hours=25)))
        recent = os.path.join(self.host_dir, ".incomplete-" + backup_lib.utc_stamp(
            NOW - timedelta(hours=1)))
        junk = os.path.join(self.host_dir, ".incomplete-garbage")
        # Matches NAME_RE but is no real date: must be left alone, not crash prune.
        impossible = os.path.join(self.host_dir, ".incomplete-2026-13-45T999999Z")
        for path in (old, recent, junk, impossible):
            os.makedirs(path)
        deleted = backup_lib.prune(self.host_dir, 8, NOW)
        self.assertEqual(deleted, [os.path.basename(old)])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(recent))
        self.assertTrue(os.path.isdir(junk))
        self.assertTrue(os.path.isdir(impossible))

    def test_dated_folder_with_bare_rev_is_not_pruned(self):
        for day in range(1, 4):
            self.make_backup("2026-09-%02dT120000Z" % day)
        bare = os.path.join(self.host_dir, "2026-01-01T000000Z")
        write_json(bare, {"rev": 1})
        backup_lib.prune(self.host_dir, 1, NOW)
        self.assertTrue(os.path.isdir(bare))


class TestStatus(Base):
    def test_fresh_complete_is_zero(self):
        self.make_backup("2026-10-08T140000Z", created="2026-10-08T14:00:00Z")
        code, lines = backup_lib.status(self.dest, HOST, 7, NOW)
        self.assertEqual(code, 0)
        self.assertEqual(len(lines), 1)
        self.assertIn("2026-10-08T140000Z", lines[0])
        self.assertIn("complete=true", lines[0])

    def test_stale_is_one(self):
        self.make_backup("2026-09-01T140000Z", created="2026-09-01T14:00:00Z")
        code, _ = backup_lib.status(self.dest, HOST, 7, NOW)
        self.assertEqual(code, 1)

    def test_exactly_at_max_age_is_fresh(self):
        self.make_backup("2026-10-01T140203Z", created="2026-10-01T14:02:03Z")
        self.assertEqual(backup_lib.status(self.dest, HOST, 7, NOW)[0], 0)

    def test_incomplete_newest_is_one(self):
        self.make_backup("2026-10-08T140000Z", created="2026-10-08T14:00:00Z",
                         complete=False)
        code, lines = backup_lib.status(self.dest, HOST, 7, NOW)
        self.assertEqual(code, 1)
        self.assertIn("complete=false", lines[0])

    def test_none_is_one(self):
        code, lines = backup_lib.status(self.dest, HOST, 7, NOW)
        self.assertEqual(code, 1)
        self.assertEqual(lines, ["no backup yet in %s" % self.host_dir])

    def test_unset_is_two(self):
        self.assertEqual(backup_lib.status(None, HOST, 7, NOW)[0], 2)
        self.assertEqual(backup_lib.status("", HOST, 7, NOW)[0], 2)

    def test_missing_destination_is_two(self):
        missing = os.path.join(self.tmp, "nope")
        self.assertEqual(backup_lib.status(missing, HOST, 7, NOW)[0], 2)

    def test_bare_rev_folder_not_reported(self):
        write_json(os.path.join(self.host_dir, "2026-10-08T140000Z"), {"rev": 1})
        code, lines = backup_lib.status(self.dest, HOST, 7, NOW)
        self.assertEqual(code, 1)
        self.assertIn("no backup yet", lines[0])

    def test_size_and_excluded_reported(self):
        files = [{"path": "ws/a", "type": "file", "size": 2 * 1024 * 1024,
                  "sha256": "a" * 64},
                 {"path": "ws/l", "type": "symlink", "link_target": "x"}]
        data = good_manifest(created="2026-10-08T14:00:00Z", files=files)
        data["excluded"] = [{"path": "ws/s", "reason": "secret", "detail": ""}]
        write_json(os.path.join(self.host_dir, "2026-10-08T140000Z"), data)
        _, lines = backup_lib.status(self.dest, HOST, 7, NOW)
        self.assertIn("2.0 MB", lines[0])
        self.assertIn("1 excluded", lines[0])


class TestLoadManifest(Base):
    def load(self, data):
        folder = os.path.join(self.tmp, "m")
        shutil.rmtree(folder, True)
        write_json(folder, data)
        return backup_lib.load_manifest(folder)

    def test_valid_manifest_loads(self):
        self.assertEqual(self.load(good_manifest())["rev"], 1)

    def test_invalid_shapes_raise_manifest_error_only(self):
        file_ok = {"path": "p", "type": "file", "size": 1, "sha256": "a" * 64}
        cases = {
            "array": [],
            "bare rev": {"rev": 1},
            "wrong rev": dict(good_manifest(), rev=2),
            "bool rev": dict(good_manifest(), rev=True),
            "non-bool complete": dict(good_manifest(), complete="yes"),
            "bad created_at": dict(good_manifest(), created_at="2026-10-08"),
            "unpadded created_at": dict(good_manifest(), created_at="2026-1-2T3:4:5Z"),
            "non-str created_at": dict(good_manifest(), created_at=5),
            "missing sha256": good_manifest(files=[
                {"path": "p", "type": "file", "size": 1}]),
            "short sha256": good_manifest(files=[dict(file_ok, sha256="0f1e2d")]),
            "symlink no target": good_manifest(files=[
                {"path": "p", "type": "symlink"}]),
            "unknown type": good_manifest(files=[dict(file_ok, type="dir")]),
            "item missing status": dict(good_manifest(), items=[
                {"source": None, "dest_rel": "a", "required": True}]),
            "files not list": dict(good_manifest(), files={}),
            "excluded not list": dict(good_manifest(), excluded={}),
            "scalar": 7,
        }
        for label, data in cases.items():
            with self.subTest(label):
                with self.assertRaises(backup_lib.ManifestError):
                    self.load(data)

    def test_unreadable_manifests_raise_manifest_error(self):
        folder = os.path.join(self.tmp, "m2")
        with self.assertRaises(backup_lib.ManifestError):  # missing file
            backup_lib.load_manifest(folder)
        os.makedirs(folder)
        for payload in (b"{not json", b"\xff\xfe\x00"):
            with open(os.path.join(folder, "MANIFEST.json"), "wb") as handle:
                handle.write(payload)
            with self.assertRaises(backup_lib.ManifestError):
                backup_lib.load_manifest(folder)

    def test_null_source_item_is_valid(self):
        data = good_manifest()
        data["items"] = [{"source": None, "dest_rel": "memory", "required": True,
                          "status": "missing"}]
        self.assertEqual(self.load(data)["items"][0]["source"], None)


if __name__ == "__main__":
    unittest.main()
