"""Tests for the claude-spec-archive executable itself, not the library.

Run through subprocess against a real temporary directory, per the sibling
convention (thread-handover's test_deliver_hook.py). Nothing here ever
touches this machine's real config or workspace/specs.
"""
import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import unittest
import tempfile
from datetime import datetime
from unittest import mock

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI = os.path.join(TOOL_DIR, "claude-spec-archive")

REPO = os.path.dirname(os.path.dirname(TOOL_DIR))
sys.path.insert(0, os.path.join(REPO, "lib"))
import hook_manifest  # noqa: E402

# Subprocess timeout for every test in this file (repo convention, sharpened
# here: "every subprocess call in tests passes timeout=15, so a
# hang can never stall the suite" -- most load-bearing for TestHookDeadline's
# mutation check, where join() with no timeout would otherwise hang forever).
SUBPROCESS_TIMEOUT = 15


def load_cli_module():
    """Import the extensionless `claude-spec-archive` executable as a module.

    importlib.util.spec_from_file_location() can't guess a loader for a file
    with no ".py" suffix, so the loader has to be named explicitly. Used only
    by the race-skip test to call _apply_flip() directly (an in-process unit
    test of the executable's own flip executor, per the plan).
    """
    loader = importlib.machinery.SourceFileLoader("claude_spec_archive_cli", CLI)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def write_crlf_spec(path, status, subject="toolkit", days_ago=100, extra=""):
    """Same shape as SpecArchiveCliTest.write_spec, but with real \\r\\n line
    endings and written in binary mode so nothing translates them."""
    content = f"---\r\nstatus: {status}\r\nsubject: {subject}\r\n{extra}---\r\nbody\r\n"
    with open(path, "wb") as handle:
        handle.write(content.encode("utf-8"))
    stamp = datetime.now().timestamp() - days_ago * 86400
    os.utime(path, (stamp, stamp))


def snapshot(root):
    """{relpath: bytes} for every file under root, plus the set of directories.

    Used to assert a dry run is byte-identical: comparing file contents alone
    would miss a directory created (but left empty) by a dry run that should
    never touch the filesystem at all.
    """
    files = {}
    dirs = set()
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirs.add(rel_dir)
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as handle:
                files[rel] = handle.read()
    return files, dirs


class SpecArchiveCliTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.specs_dir = os.path.join(self._tmp.name, "specs")
        os.makedirs(self.specs_dir)
        self.config_home = os.path.join(self._tmp.name, "config-home")
        os.makedirs(self.config_home)

    def tearDown(self):
        self._tmp.cleanup()

    def write_spec(self, name, status, subject="toolkit", days_ago=100, extra=""):
        path = os.path.join(self.specs_dir, name)
        with open(path, "w") as handle:
            handle.write(f"---\nstatus: {status}\nsubject: {subject}\n{extra}---\nbody\n")
        stamp = datetime.now().timestamp() - days_ago * 86400
        os.utime(path, (stamp, stamp))
        return path

    def run_cli(self, *args, config_blob=None, specs_dir=None):
        env = dict(os.environ)
        if config_blob is None:
            env["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent.json"
        else:
            config_path = os.path.join(self.config_home, "config.json")
            import json
            with open(config_path, "w") as handle:
                json.dump(config_blob, handle)
            env["CLAUDE_TOOLKIT_CONFIG"] = config_path
        cli_args = [sys.executable, CLI]
        if specs_dir is not False:
            cli_args += ["--specs-dir", specs_dir or self.specs_dir]
        cli_args += list(args)
        return subprocess.run(cli_args, env=env, capture_output=True, text=True,
                              timeout=SUBPROCESS_TIMEOUT)


class TestNotConfigured(SpecArchiveCliTest):
    # Safety contract item 8.

    def test_no_specs_dir_reports_not_configured_and_exits_zero(self):
        proc = self.run_cli(specs_dir=False)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("not configured", proc.stdout)


class TestDryRunIsDefault(SpecArchiveCliTest):
    # Safety contract items 1 and 6: nothing moves without --apply.

    def test_dry_run_leaves_the_fixture_byte_identical(self):
        self.write_spec("2026-01-01-shipped.md", "shipped", days_ago=100)
        self.write_spec("2026-01-01-live.md", "live", days_ago=100)
        before = snapshot(self.specs_dir)

        proc = self.run_cli()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ARCHIVE (1)", proc.stdout)

        after = snapshot(self.specs_dir)
        self.assertEqual(before, after)

    def test_dry_run_without_apply_flag_moves_nothing_even_if_reported(self):
        # Same fixture, explicitly asserting the ARCHIVE candidate is still
        # sitting exactly where it started.
        path = self.write_spec("2026-01-01-shipped.md", "shipped", days_ago=100)
        self.run_cli()
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(os.path.isdir(os.path.join(self.specs_dir, "archive")))


class TestApplyMovesArchiveOnly(SpecArchiveCliTest):

    def test_apply_moves_exactly_one_and_leaves_others_untouched(self):
        shipped = self.write_spec("2026-01-01-shipped.md", "shipped", days_ago=100)
        live = self.write_spec("2026-01-02-live.md", "live", days_ago=100)
        handover = self.write_spec("2026-01-03-something-handover.md", "shipped", days_ago=100)

        proc = self.run_cli("--apply")
        self.assertEqual(proc.returncode, 0)

        self.assertFalse(os.path.exists(shipped))
        self.assertTrue(os.path.isfile(live))
        self.assertTrue(os.path.isfile(handover))

        destination = os.path.join(
            self.specs_dir, "archive", "202601 - toolkit - shipped-specs",
            "20260101 - toolkit - shipped.md")
        self.assertTrue(os.path.isfile(destination))
        with open(destination) as handle:
            self.assertIn("body", handle.read())

        # Exactly one file changed location: the specs_dir top level should
        # now hold only the two untouched files plus the new archive/ tree.
        remaining = set(os.listdir(self.specs_dir))
        self.assertEqual(remaining, {os.path.basename(live), os.path.basename(handover), "archive"})


class TestRefusesExistingDestination(SpecArchiveCliTest):
    # Safety contract item 5.

    def test_apply_refuses_and_exits_one_when_destination_exists(self):
        source = self.write_spec("2026-01-01-shipped.md", "shipped", days_ago=100)
        destination_dir = os.path.join(
            self.specs_dir, "archive", "202601 - toolkit - shipped-specs")
        os.makedirs(destination_dir)
        destination = os.path.join(destination_dir, "20260101 - toolkit - shipped.md")
        with open(destination, "w") as handle:
            handle.write("already archived by someone else")

        proc = self.run_cli("--apply")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("REFUSED", proc.stdout)

        # Neither side moved: the source is still in place and the existing
        # destination content was not overwritten.
        self.assertTrue(os.path.isfile(source))
        with open(destination) as handle:
            self.assertEqual(handle.read(), "already archived by someone else")


class TestReportFlag(SpecArchiveCliTest):

    def test_report_omits_the_archive_section_but_keeps_the_count(self):
        self.write_spec("2026-01-01-shipped.md", "shipped", days_ago=100)
        proc = self.run_cli("--report")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ARCHIVE (1)", proc.stdout)
        self.assertNotIn(" -> ", proc.stdout)


class TestUndeclaredAndUnmanagedAreAlwaysExplained(SpecArchiveCliTest):

    def test_undeclared_and_unmanaged_reasons_are_printed(self):
        path = os.path.join(self.specs_dir, "2026-01-01-nostatus.md")
        with open(path, "w") as handle:
            handle.write("no front matter at all\n")
        self.write_spec("2026-01-02-something-handover.md", "shipped", days_ago=100)

        proc = self.run_cli()
        self.assertIn("2026-01-01-nostatus.md:", proc.stdout)
        self.assertIn("2026-01-02-something-handover.md:", proc.stdout)


class TestFlipStaleManual(SpecArchiveCliTest):
    # --flip-stale, manual (report + --apply).

    def test_crlf_flip_end_to_end_preserves_every_byte_but_the_status_line(self):
        # D11, exercised through the CLI rather than the library directly.
        path = os.path.join(self.specs_dir, "2026-01-01-crlf.md")
        write_crlf_spec(path, "live", subject="toolkit", days_ago=30)
        with open(path, "rb") as handle:
            before = handle.read()

        proc = self.run_cli("--flip-stale", "--apply",
                            config_blob={"spec_archive": {"stale_flip_days": 21}})
        self.assertEqual(proc.returncode, 0)

        with open(path, "rb") as handle:
            after = handle.read()

        self.assertNotEqual(before, after)
        # Every line, before and after, ends \r\n: this only holds if
        # splitting on the two-byte terminator leaves no bare \n anywhere.
        self.assertNotIn(b"\n", after.replace(b"\r\n", b""))
        before_lines = before.split(b"\r\n")
        after_lines = after.split(b"\r\n")
        self.assertEqual(len(before_lines), len(after_lines))
        changed = [i for i in range(len(before_lines)) if before_lines[i] != after_lines[i]]
        self.assertEqual(len(changed), 1)
        self.assertTrue(before_lines[changed[0]].startswith(b"status: live"))
        self.assertTrue(after_lines[changed[0]].startswith(b"status: shipped"))

    def test_flip_stale_without_apply_changes_no_bytes(self):
        self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        before = snapshot(self.specs_dir)

        proc = self.run_cli("--flip-stale",
                            config_blob={"spec_archive": {"stale_flip_days": 21}})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FLIP (1)", proc.stdout)

        after = snapshot(self.specs_dir)
        self.assertEqual(before, after)

    def test_disabled_flip_reports_flip_zero_and_the_disabled_line(self):
        self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        proc = self.run_cli("--flip-stale")  # no config_blob -> stale_flip_days default 0
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FLIP (0)", proc.stdout)
        self.assertIn("flip disabled (spec_archive.stale_flip_days is 0 or unset)", proc.stdout)

    def test_race_skip_leaves_the_file_byte_identical(self):
        # Unit-level, importing the executable directly, per the plan.
        path = self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        with open(path, "rb") as handle:
            before = handle.read()

        module = load_cli_module()
        stale_mtime = os.path.getmtime(path) - 999999
        verdict = module.archive_lib.FlipVerdict("flip", "test-only stale mtime", stale_mtime)
        event = module._apply_flip(path, verdict, "2026-09-23", 21)

        self.assertEqual(event, ("flip-skipped", path, "changed since scanned"))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_race_skip_during_the_read_leaves_the_editors_edit_in_place(self):
        # Narrows the race further than the scan-time check above: an edit
        # landing AFTER _apply_flip's own read but BEFORE its post-read
        # re-stat must be caught there ("changed while reading"), not slip
        # through to a write built from the now-stale text it just read.
        path = self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        with open(path, encoding="utf-8") as handle:
            original_text = handle.read()
        edited_text = original_text + "EDITED BY HUMAN DURING THE READ\n"
        edit_mtime = os.path.getmtime(path) + 12345

        module = load_cli_module()
        verdict = module.archive_lib.FlipVerdict(
            "flip", "test-only", os.path.getmtime(path))

        real_open = open

        def sneaky_open(target, *args, **kwargs):
            handle = real_open(target, *args, **kwargs)
            if os.path.abspath(target) != os.path.abspath(path):
                return handle

            class Proxy:
                def __enter__(self_proxy):
                    return self_proxy

                def __exit__(self_proxy, *exc_info):
                    return handle.__exit__(*exc_info)

                def read(self_proxy, *a, **kw):
                    text = handle.read(*a, **kw)
                    # The edit lands here: after our own read has returned
                    # the pre-edit text, before the post-read re-stat runs.
                    with real_open(path, "w", encoding="utf-8", newline="") as out:
                        out.write(edited_text)
                    os.utime(path, (edit_mtime, edit_mtime))
                    return text

            return Proxy()

        with mock.patch.object(module, "open", sneaky_open, create=True):
            event = module._apply_flip(path, verdict, "2026-09-23", 21)

        self.assertEqual(event, ("flip-skipped", path, "changed while reading"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), edited_text)
        self.assertIn("status: live", edited_text)
        leftover = [name for name in os.listdir(self.specs_dir)
                   if name.endswith(".spec-archive-tmp")]
        self.assertEqual(leftover, [])

    def test_race_skip_just_before_replace_leaves_the_editors_edit_in_place(self):
        # Narrows the race to the last possible check: an edit landing after
        # the temp file is fully written but before the atomic replace must
        # be caught by the re-stat immediately preceding os.replace
        # ("changed before replace"), not overwritten by it.
        path = self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        with open(path, encoding="utf-8") as handle:
            original_text = handle.read()
        edited_text = original_text + "EDITED BY HUMAN JUST BEFORE REPLACE\n"
        edit_mtime = os.path.getmtime(path) + 54321

        module = load_cli_module()
        verdict = module.archive_lib.FlipVerdict(
            "flip", "test-only", os.path.getmtime(path))

        real_chmod = os.chmod

        def sneaky_chmod(target, mode, *a, **kw):
            # os.chmod(tmp_path, mode) is the last thing _apply_flip does
            # while writing the temp file -- the edit lands right after it,
            # immediately before the final re-stat.
            real_chmod(target, mode, *a, **kw)
            with open(path, "w", encoding="utf-8", newline="") as out:
                out.write(edited_text)
            os.utime(path, (edit_mtime, edit_mtime))

        with mock.patch("os.chmod", side_effect=sneaky_chmod):
            event = module._apply_flip(path, verdict, "2026-09-23", 21)

        self.assertEqual(event, ("flip-skipped", path, "changed before replace"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), edited_text)
        self.assertIn("status: live", edited_text)
        leftover = [name for name in os.listdir(self.specs_dir)
                   if name.endswith(".spec-archive-tmp")]
        self.assertEqual(leftover, [])

    def test_flip_refused_is_printed_end_to_end_through_apply(self):
        # specs_dir loses its write bit AFTER the spec is written, so listing
        # and reading still work but _apply_flip()'s temp-file write cannot
        # -- a real, deterministic flip-refused reached through the CLI
        # itself, not a synthesized event.
        path = self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        os.chmod(self.specs_dir, 0o555)
        try:
            proc = self.run_cli("--flip-stale", "--apply",
                                config_blob={"spec_archive": {"stale_flip_days": 21}})
        finally:
            os.chmod(self.specs_dir, 0o755)

        self.assertEqual(proc.returncode, 1)
        self.assertIn("spec-archive: REFUSED flip", proc.stdout)
        self.assertIn(path, proc.stdout)

    def test_flip_never_follows_a_symlink_planted_at_the_old_predictable_tmp_name(self):
        # D11/safety fix: the old temp path, ".<basename>.spec-archive-tmp",
        # was predictable. If an attacker (or a leftover) plants a symlink
        # there pointing outside specs_dir, the flip must never write
        # through it -- the victim's content must be untouched, and the
        # spec must still flip via a real (unpredictable) temp file.
        path = self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        victim_dir = os.path.join(self._tmp.name, "victim")
        os.makedirs(victim_dir)
        victim = os.path.join(victim_dir, "outside.txt")
        with open(victim, "w") as handle:
            handle.write("ORIGINAL\n")

        tmp_path = os.path.join(
            self.specs_dir, f".{os.path.basename(path)}.spec-archive-tmp")
        os.symlink(victim, tmp_path)

        proc = self.run_cli("--flip-stale", "--apply",
                            config_blob={"spec_archive": {"stale_flip_days": 21}})
        self.assertEqual(proc.returncode, 0)

        with open(victim) as handle:
            self.assertEqual(handle.read(), "ORIGINAL\n")
        with open(path) as handle:
            self.assertIn("status: shipped # auto-flipped", handle.read())

    def test_no_tmp_file_left_in_specs_dir_after_a_successful_flip(self):
        self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        proc = self.run_cli("--flip-stale", "--apply",
                            config_blob={"spec_archive": {"stale_flip_days": 21}})
        self.assertEqual(proc.returncode, 0)

        leftover = [name for name in os.listdir(self.specs_dir)
                   if name.endswith(".spec-archive-tmp")]
        self.assertEqual(leftover, [])

    def test_a_spec_flipped_this_run_is_not_archived_in_the_same_manual_run(self):
        # main() runs --flip-stale --apply's flip pass and then the archive
        # pass in the same invocation -- with min_age_days: 0 the freshly-
        # reset mtime from the flip's own write would otherwise clear the
        # archive floor immediately. Same D2 race as --hook (TestHookMode),
        # exercised through the manual CLI path.
        path = self.write_spec("2026-01-01-live.md", "live", days_ago=30)

        proc = self.run_cli(
            "--flip-stale", "--apply",
            config_blob={"spec_archive": {"stale_flip_days": 21, "min_age_days": 0}})
        self.assertEqual(proc.returncode, 0)

        self.assertTrue(os.path.isfile(path))
        with open(path) as handle:
            self.assertIn("status: shipped # auto-flipped", handle.read())
        self.assertFalse(os.path.isdir(os.path.join(self.specs_dir, "archive")))
        self.assertNotIn("spec-archive: moved", proc.stdout)

    def test_flip_skipped_and_flip_refused_have_dedicated_manual_wording(self):
        # Unit-level check of the print wording itself, per the same
        # importlib pattern the plan sanctions for _apply_flip() above --
        # constructing a genuine race between the scan and the apply loop
        # from outside a single CLI invocation isn't practical.
        module = load_cli_module()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            module._print_flip_apply_event(("flip-skipped", "/x/y.md", "changed since scanned"))
            module._print_flip_apply_event(("flip-refused", "/x/z.md", "OSError(13, 'boom')"))
        output = buf.getvalue()
        self.assertIn("spec-archive: flip skipped /x/y.md (changed since scanned)", output)
        self.assertIn("spec-archive: REFUSED flip /x/z.md (OSError(13, 'boom'))", output)


class TestHookMode(SpecArchiveCliTest):
    # --hook: the happy paths and the loud-failure paths.

    def write_mission_shaped_live_spec(self, name, days_ago):
        # write_spec()'s own `extra` lands INSIDE the front-matter block, so
        # a real body section needs its own helper.
        path = os.path.join(self.specs_dir, name)
        with open(path, "w") as handle:
            handle.write(
                "---\nstatus: live\nsubject: toolkit\n---\n"
                "## Backlog\n### Blocked on the owner\nsome note\n")
        stamp = datetime.now().timestamp() - days_ago * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_shared_default_disables_flip_and_hook_says_nothing(self):
        path = self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        with open(path, "rb") as handle:
            before = handle.read()

        proc = self.run_cli("--hook")  # config_blob=None -> /nonexistent.json
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_hook_flips_one_archives_one_and_leaves_the_other_two_untouched(self):
        flip_target = self.write_spec("2026-01-01-live-old.md", "live", days_ago=30)
        live_recent = self.write_spec("2026-01-02-live-recent.md", "live", days_ago=10)
        mission = self.write_mission_shaped_live_spec("2026-01-03-mission.md", days_ago=30)
        shipped_old = self.write_spec("2026-01-04-shipped-old.md", "shipped", days_ago=100)

        with open(live_recent, "rb") as handle:
            recent_before = handle.read()
        with open(mission, "rb") as handle:
            mission_before = handle.read()

        proc = self.run_cli("--hook", config_blob={"spec_archive": {"stale_flip_days": 21}})
        self.assertEqual(proc.returncode, 0)

        payload = json.loads(proc.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("archived 1", ctx)
        self.assertIn("flipped 1", ctx)
        self.assertIn(os.path.basename(flip_target), ctx)

        # The flip target: marker present, still in place (not archived this
        # run -- its own write reset mtime, per D2), mtime within 60s of now.
        with open(flip_target) as handle:
            self.assertIn("status: shipped # auto-flipped", handle.read())
        self.assertTrue(os.path.isfile(flip_target))
        self.assertLess(abs(datetime.now().timestamp() - os.path.getmtime(flip_target)), 60)

        # Untouched by age (10d) and by mission-shape-with-no-section.
        with open(live_recent, "rb") as handle:
            self.assertEqual(handle.read(), recent_before)
        with open(mission, "rb") as handle:
            self.assertEqual(handle.read(), mission_before)

        # The already-old shipped spec: moved under archive/, renamed per
        # destination_for()'s "YYYYMMDD - subject - slug.ext" convention.
        self.assertFalse(os.path.exists(shipped_old))
        archived_names = []
        for _, _, filenames in os.walk(os.path.join(self.specs_dir, "archive")):
            archived_names.extend(filenames)
        self.assertIn("20260104 - toolkit - shipped-old.md", archived_names)

    def test_a_second_hook_run_is_a_silent_noop(self):
        self.write_spec("2026-01-01-live-old.md", "live", days_ago=30)
        self.write_spec("2026-01-04-shipped-old.md", "shipped", days_ago=100)

        config_blob = {"spec_archive": {"stale_flip_days": 21}}
        first = self.run_cli("--hook", config_blob=config_blob)
        self.assertEqual(first.returncode, 0)

        before = snapshot(self.specs_dir)
        second = self.run_cli("--hook", config_blob=config_blob)
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second.stdout, "")
        after = snapshot(self.specs_dir)
        self.assertEqual(before, after)

    def test_specs_dir_not_a_directory_gives_a_loud_notice(self):
        not_a_dir = os.path.join(self._tmp.name, "not-a-dir.txt")
        with open(not_a_dir, "w") as handle:
            handle.write("just a file")

        proc = self.run_cli("--hook", specs_dir=not_a_dir)
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("is not a directory", ctx)

    def test_refusal_is_loud(self):
        source = self.write_spec("2026-01-01-shipped.md", "shipped", days_ago=100)
        destination_dir = os.path.join(
            self.specs_dir, "archive", "202601 - toolkit - shipped-specs")
        os.makedirs(destination_dir)
        with open(os.path.join(destination_dir, "20260101 - toolkit - shipped.md"), "w") as handle:
            handle.write("already archived by someone else")

        proc = self.run_cli("--hook")
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("1 refused", ctx)
        self.assertTrue(os.path.isfile(source))

    def test_a_spec_flipped_this_run_is_not_archived_in_the_same_run(self):
        # min_age_days: 0 makes the freshly-reset mtime from the flip's own
        # write satisfy the archive floor trivially -- the exact race D2's
        # "sits the normal min_age_days floor" promise depends on not
        # happening.
        path = self.write_spec("2026-01-01-live.md", "live", days_ago=30)

        proc = self.run_cli(
            "--hook",
            config_blob={"spec_archive": {"stale_flip_days": 21, "min_age_days": 0}})
        self.assertEqual(proc.returncode, 0)

        payload = json.loads(proc.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("archived 0", ctx)
        self.assertIn("flipped 1", ctx)

        self.assertTrue(os.path.isfile(path))
        with open(path) as handle:
            self.assertIn("status: shipped # auto-flipped", handle.read())
        self.assertFalse(os.path.isdir(os.path.join(self.specs_dir, "archive")))

    def test_a_flip_refused_event_is_counted_in_the_refused_tally_too(self):
        # The lock file is pre-created, writable, BEFORE specs_dir loses its
        # write bit: reopening an existing file in append mode needs write
        # permission on the FILE, not the directory, so the lock still
        # succeeds and the worker reaches the flip phase, where the
        # temp-file write then fails.
        self.write_spec("2026-01-01-live.md", "live", days_ago=30)
        lockpath = os.path.join(self.specs_dir, ".spec-archive.lock")
        open(lockpath, "a").close()
        os.chmod(self.specs_dir, 0o555)
        try:
            proc = self.run_cli("--hook", config_blob={"spec_archive": {"stale_flip_days": 21}})
        finally:
            os.chmod(self.specs_dir, 0o755)

        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("1 refused", ctx)


class TestHookLock(SpecArchiveCliTest):
    # D8: the lock itself.

    def test_lock_path_is_a_directory_gives_a_failed_notice(self):
        os.makedirs(os.path.join(self.specs_dir, ".spec-archive.lock"))
        proc = self.run_cli("--hook")
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("FAILED:", ctx)

    def test_held_lock_blocks_the_hook_and_releasing_it_lets_the_hook_proceed(self):
        lockpath = os.path.join(self.specs_dir, ".spec-archive.lock")
        lock_fd = open(lockpath, "a")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            path = self.write_spec("2026-01-01-shipped.md", "shipped", days_ago=100)
            before = snapshot(self.specs_dir)

            proc = self.run_cli("--hook")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")

            after = snapshot(self.specs_dir)
            self.assertEqual(before, after)
            self.assertTrue(os.path.isfile(path))
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()

        # Positive control: same fixture, lock released -- the file moves.
        proc = self.run_cli("--hook")
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(path))


class TestHookDeadline(SpecArchiveCliTest):
    # D7: the deadline, plus its positive control.

    def test_a_stalled_fifo_hits_the_deadline_and_the_hook_still_exits_0(self):
        os.mkfifo(os.path.join(self.specs_dir, "2026-01-01-stall.md"))

        start = time.monotonic()
        proc = self.run_cli("--hook", config_blob={"spec_archive": {"hook_deadline_seconds": 1}})
        elapsed = time.monotonic() - start

        self.assertEqual(proc.returncode, 0)
        self.assertLess(elapsed, 6)
        payload = json.loads(proc.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("Did not finish within 1", ctx)

    def test_positive_control_without_the_fifo_finishes_with_no_deadline_notice(self):
        proc = self.run_cli("--hook", config_blob={"spec_archive": {"hook_deadline_seconds": 1}})
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("Did not finish", proc.stdout)

    def test_a_stdin_that_never_reaches_eof_does_not_block_the_hook(self):
        # D7: the real SessionStart harness pipes a JSON payload on stdin and
        # may not close it promptly. A stdin read that blocks the WHOLE hook
        # (not just its own drain) would let this stall past the declared
        # 10s hook timeout with no output at all -- the silent failure D7
        # exists to prevent. run_cli() can't exercise this: subprocess.run's
        # communicate() needs stdin closed or written to, which is exactly
        # what this test must never do.
        self.write_spec("2026-01-01-shipped.md", "shipped", days_ago=100)
        config_path = os.path.join(self.config_home, "config.json")
        with open(config_path, "w") as handle:
            json.dump({}, handle)
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = config_path

        proc = subprocess.Popen(
            [sys.executable, CLI, "--specs-dir", self.specs_dir, "--hook"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, text=True)
        try:
            # Deliberately never written to and never closed.
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            self.fail("--hook did not exit within 10s while stdin stayed open with no EOF")

        stdout = proc.stdout.read()
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertIn("archived 1", ctx)

        proc.stdin.close()
        proc.stdout.close()
        proc.stderr.close()

    def test_a_closed_stdin_still_exits_0_and_reports(self):
        # D9: spawned with fd 0 closed, CPython sets sys.stdin = None. An
        # unguarded isatty() there exits 1 with no output -- silent. The
        # shipped spec gives the run something to say, so an empty stdout
        # here cannot pass as "nothing happened".
        self.write_spec("2026-01-01-shipped.md", "shipped", days_ago=100)
        env = dict(os.environ)
        env["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent.json"
        proc = subprocess.run(
            [sys.executable, CLI, "--specs-dir", self.specs_dir, "--hook"],
            stdin=None, capture_output=True, text=True, env=env,
            preexec_fn=lambda: os.close(0), timeout=SUBPROCESS_TIMEOUT)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        ctx = json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("archived 1", ctx)


class TestNoticeSilenceGate(unittest.TestCase):
    # D9's fourth silence case: a run whose only event is a flip-skipped race
    # did nothing reportable, so the hook must say nothing.

    def setUp(self):
        self.cli = load_cli_module()

    def test_a_lone_flip_skipped_event_is_silent(self):
        notice = self.cli._build_notice(
            "/specs", [("flip-skipped", "/specs/a.md", "changed since scanned")],
            None, False, 21, 4)
        self.assertIsNone(notice)

    def test_positive_control_a_flip_is_reported(self):
        notice = self.cli._build_notice(
            "/specs", [("flipped", "/specs/a.md")], None, False, 21, 4)
        self.assertIn("flipped 1", notice)


class TestHookRegistration(unittest.TestCase):
    # Pinned: the repo's own plugin-build/hooks/hooks.json must actually
    # wire --hook into SessionStart, or the whole build ships inert -- a
    # symptom install.sh's own payload-surface check would not catch, since
    # it only verifies the hooks key MERGES, not that this tool's entry is
    # IN it. Reads the manifest through lib/hook_manifest.py.

    def setUp(self):
        self.cli = load_cli_module()
        self._prev_config = os.environ.get("CLAUDE_TOOLKIT_CONFIG")
        os.environ["CLAUDE_TOOLKIT_CONFIG"] = "/nonexistent.json"

    def tearDown(self):
        if self._prev_config is None:
            os.environ.pop("CLAUDE_TOOLKIT_CONFIG", None)
        else:
            os.environ["CLAUDE_TOOLKIT_CONFIG"] = self._prev_config

    def _spec_archive_hook_entries(self):
        return [
            row for row in hook_manifest.registrations(hook_manifest.load(REPO))
            if row["event"] == "SessionStart"
            and row["exe"] == "claude-spec-archive"
            and row["args"] == ["--hook"]
        ]

    def test_exactly_one_spec_archive_hook_is_registered_under_session_start(self):
        entries = self._spec_archive_hook_entries()
        self.assertEqual(len(entries), 1, entries)

    def test_its_timeout_exceeds_the_default_hook_deadline(self):
        entry = self._spec_archive_hook_entries()[0]
        deadline = self.cli._hook_deadline_seconds()
        self.assertIsInstance(entry["timeout"], int)
        self.assertGreater(entry["timeout"], deadline)

    def test_its_command_points_into_its_own_tool_directory(self):
        entry = self._spec_archive_hook_entries()[0]
        self.assertEqual("tools/spec-archive/claude-spec-archive",
                         entry["relpath"])


if __name__ == "__main__":
    unittest.main()
