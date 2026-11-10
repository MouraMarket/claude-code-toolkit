import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import index_lib


class IndexTestCase(unittest.TestCase):

    def setUp(self):
        self._orig_index = index_lib.INDEX_PATH
        self._orig_handover_dir = index_lib.HANDOVER_DIR
        self._orig_grace_sessions = index_lib.GRACE_SESSIONS
        self._orig_grace_days = index_lib.GRACE_DAYS
        self._tmp = tempfile.mkdtemp()
        self.handover_dir = os.path.join(self._tmp, "specs")
        os.makedirs(self.handover_dir)
        index_lib.INDEX_PATH = os.path.join(self._tmp, "index.json")
        index_lib.HANDOVER_DIR = self.handover_dir
        # Pinned rather than inherited: these tests assert on the grace
        # behaviour itself, and must not change meaning because whoever runs
        # them configured a different period.
        index_lib.GRACE_SESSIONS = 3
        index_lib.GRACE_DAYS = 7

    def tearDown(self):
        index_lib.INDEX_PATH = self._orig_index
        index_lib.HANDOVER_DIR = self._orig_handover_dir
        index_lib.GRACE_SESSIONS = self._orig_grace_sessions
        index_lib.GRACE_DAYS = self._orig_grace_days

    def make_handover(self, name="2026-08-10-demo-handover.md", body="# handover"):
        path = os.path.join(self.handover_dir, name)
        with open(path, "w") as handle:
            handle.write(body)
        return path

    def consume(self, path, session="B", days_ago=30):
        """Mark `path` consumed by `session`, `days_ago` days in the past.

        Backdating is the point: claim() always stamps now, so a test that
        wants to exercise anything past the day floor has to rewrite it.
        """
        stamp = (datetime.now() - timedelta(days=days_ago)).isoformat(
            timespec="seconds")
        records = index_lib.load_index()
        for record in records:
            if record.get("path") == path:
                record["consumed_at"] = stamp
                record["consumed_session_id"] = session
        index_lib.save_index(records)

    def witnesses(self, path):
        for record in index_lib.load_index():
            if record.get("path") == path:
                return record.get("witness_session_ids")
        return None

    def sweep_sessions(self, *session_ids):
        """Run a sweep per session id; return every path retired, in order."""
        retired = []
        for session in session_ids:
            retired.extend(index_lib.sweep(session))
        return retired

    def archived(self, name="2026-08-10-demo-handover.md"):
        """Absolute path `name` should occupy in the archive."""
        return os.path.join(index_lib.archive_dir(), name)

    def archive_names(self):
        try:
            return sorted(os.listdir(index_lib.archive_dir()))
        except OSError:
            return []


class TestSafeRetire(IndexTestCase):

    def test_valid_path_is_safe(self):
        path = self.make_handover()
        self.assertTrue(index_lib.is_safe_to_retire(path, {path}))

    def test_path_not_in_index_is_unsafe(self):
        path = self.make_handover()
        self.assertFalse(index_lib.is_safe_to_retire(path, set()))

    def test_path_outside_specs_dir_is_unsafe(self):
        outside = os.path.join(self._tmp, "2026-08-10-x-handover.md")
        with open(outside, "w") as handle:
            handle.write("x")
        self.assertFalse(index_lib.is_safe_to_retire(outside, {outside}))

    def test_wrong_basename_is_unsafe(self):
        path = self.make_handover(name="2026-08-10-thread-handover-design.md")
        self.assertFalse(index_lib.is_safe_to_retire(path, {path}))

    def test_missing_file_is_unsafe(self):
        path = os.path.join(self.handover_dir, "2026-08-10-gone-handover.md")
        self.assertFalse(index_lib.is_safe_to_retire(path, {path}))

    def test_symlink_inside_specs_is_unsafe(self):
        # The surrogate-delete case: a link that passes every realpath-based
        # gate while the delete lands on a different, still-pending handover.
        victim = self.make_handover(name="2026-08-10-victim-handover.md")
        link = os.path.join(self.handover_dir, "2026-08-10-attacker-handover.md")
        os.symlink(victim, link)
        self.assertFalse(index_lib.is_safe_to_retire(link, {link}))
        self.assertTrue(os.path.isfile(victim))

    def test_traversal_is_unsafe(self):
        path = os.path.join(self.handover_dir, "..", "2026-08-10-evil-handover.md")
        with open(os.path.join(self._tmp, "2026-08-10-evil-handover.md"), "w") as h:
            h.write("x")
        self.assertFalse(index_lib.is_safe_to_retire(path, {path}))


class TestSweep(IndexTestCase):

    def test_archives_handover_after_both_grace_gates_clear(self):
        path = self.make_handover(body="# the open items nobody carried")
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)

        retired = self.sweep_sessions("C", "D", "E")
        self.assertEqual(retired, [path])
        self.assertFalse(os.path.exists(path))
        self.assertEqual(index_lib.load_index(), [])

        # The whole point of the change: it is recoverable, with its content.
        self.assertTrue(os.path.isfile(self.archived()))
        with open(self.archived()) as handle:
            self.assertEqual(handle.read(), "# the open items nobody carried")

    def test_keeps_handover_consumed_by_current_session(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)

        self.assertEqual(index_lib.sweep("B"), [])
        self.assertTrue(os.path.exists(path))

    def test_keeps_unconsumed_handover(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.assertEqual(index_lib.sweep("C"), [])
        self.assertTrue(os.path.exists(path))

    def test_empty_index_is_safe(self):
        self.assertEqual(index_lib.sweep("C"), [])


class TestGracePeriod(IndexTestCase):
    """The two gates, and the concurrency failure that motivated them."""

    def test_one_later_session_does_not_retire(self):
        # The old behaviour, and the bug: a second window opening was enough.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)

        self.assertEqual(index_lib.sweep("C"), [])
        self.assertTrue(os.path.isfile(path))

    def test_survives_until_the_session_count_is_reached(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)

        self.assertEqual(self.sweep_sessions("C", "D"), [])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(index_lib.sweep("E"), [path])

    def test_repeated_sweeps_from_one_session_never_reach_the_count(self):
        # SessionStart also fires on compact and resume, which keep the same
        # id. If those counted, one window could retire a handover by itself.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)

        self.assertEqual(self.sweep_sessions("C", "C", "C", "C", "C"), [])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(self.witnesses(path), ["C"])

    def test_day_floor_holds_when_the_session_count_is_exceeded(self):
        # The concurrency case this fix exists for: five windows open in one
        # tree on the day the handover was attached.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=0)

        self.assertEqual(self.sweep_sessions("C", "D", "E", "F", "G"), [])
        self.assertTrue(os.path.isfile(path))

    def test_day_floor_is_not_cleared_one_day_early(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=6)

        self.assertEqual(self.sweep_sessions("C", "D", "E"), [])
        self.assertTrue(os.path.isfile(path))

    def test_archives_once_the_day_floor_is_also_cleared(self):
        # Same record, same witnesses, one day later: the only thing that
        # changed is elapsed time, and that alone must flip the decision.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=6)
        self.assertEqual(self.sweep_sessions("C", "D", "E"), [])

        self.consume(path, "B", days_ago=8)
        self.assertEqual(index_lib.sweep("F"), [path])
        self.assertFalse(os.path.exists(path))

    def test_witness_list_is_capped_at_the_threshold(self):
        # Unbounded, a record waiting out the day floor accumulates a session
        # id per window for a week.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=0)

        self.sweep_sessions(*["S%d" % n for n in range(20)])
        self.assertEqual(self.witnesses(path), ["S0", "S1", "S2"])

    def test_consuming_session_is_not_counted_as_a_witness(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)

        self.sweep_sessions("B", "C", "B", "D")
        self.assertEqual(self.witnesses(path), ["C", "D"])
        self.assertTrue(os.path.isfile(path))

    def test_witnesses_persist_across_sweeps(self):
        # The count is meaningless unless it survives the process. Each sweep
        # is a separate short-lived hook run.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)

        index_lib.sweep("C")
        self.assertEqual(self.witnesses(path), ["C"])
        index_lib.sweep("D")
        self.assertEqual(self.witnesses(path), ["C", "D"])

    def test_undatable_record_is_never_retired(self):
        # consumed_at absent or malformed means we cannot date the record, and
        # an undatable record is kept forever. Clutter over loss.
        for stamp in (None, "", "not-a-date", "2026-13-45T99:00:00"):
            with self.subTest(stamp=stamp):
                path = self.make_handover(
                    name="2026-08-10-undated-handover.md")
                index_lib.save_index([])
                index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
                records = index_lib.load_index()
                records[0]["consumed_at"] = stamp
                records[0]["consumed_session_id"] = "B"
                index_lib.save_index(records)

                self.assertEqual(self.sweep_sessions("C", "D", "E", "F"), [])
                self.assertTrue(os.path.isfile(path))

    def test_future_consumed_at_is_not_retired(self):
        # Clock skew or a hand-edited index must not read as "long expired".
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=-30)

        self.assertEqual(self.sweep_sessions("C", "D", "E"), [])
        self.assertTrue(os.path.isfile(path))

    def test_timezone_aware_consumed_at_is_handled(self):
        # Nothing writes one today, but a naive/aware mismatch must degrade to
        # a decision, not an exception that takes the SessionStart hook down.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        records = index_lib.load_index()
        records[0]["consumed_at"] = "2020-01-01T00:00:00+00:00"
        records[0]["consumed_session_id"] = "B"
        index_lib.save_index(records)

        self.assertEqual(self.sweep_sessions("C", "D", "E"), [path])

    def test_legacy_record_without_witness_field_starts_counting(self):
        # Records written before this field existed must not be deleted on the
        # next sweep just because the key is absent.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=365)
        records = index_lib.load_index()
        records[0].pop("witness_session_ids", None)
        index_lib.save_index(records)

        self.assertEqual(index_lib.sweep("C"), [])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(self.witnesses(path), ["C"])

    def test_corrupt_witness_field_is_rebuilt_not_trusted(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)
        records = index_lib.load_index()
        records[0]["witness_session_ids"] = "C,D,E"   # not a list
        index_lib.save_index(records)

        self.assertEqual(index_lib.sweep("F"), [])
        self.assertEqual(self.witnesses(path), ["F"])

    def test_unconsumed_handover_accrues_no_witnesses(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.sweep_sessions("C", "D", "E", "F")
        self.assertIsNone(self.witnesses(path))
        self.assertTrue(os.path.isfile(path))

    def test_grace_is_tracked_per_record(self):
        # Two handovers consumed at different times must expire independently;
        # witnessing one must not advance the other.
        old = self.make_handover(name="2026-08-01-old-handover.md")
        new = self.make_handover(name="2026-08-10-new-handover.md")
        index_lib.add_record(old, "/proj", "A", "2026-08-01T10:00:00")
        index_lib.add_record(new, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(old, "B", days_ago=30)
        self.consume(new, "B", days_ago=1)

        self.assertEqual(self.sweep_sessions("C", "D", "E"), [old])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isfile(new))

    def test_stale_record_for_a_missing_file_is_dropped_immediately(self):
        # A file the user deleted by hand should not keep an index row alive
        # for the whole grace period.
        ghost = os.path.join(self.handover_dir, "2026-08-10-ghost-handover.md")
        index_lib.add_record(ghost, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(ghost, "B", days_ago=0)

        self.assertEqual(index_lib.sweep("C"), [])
        self.assertEqual(index_lib.load_index(), [])

    def test_a_file_that_fails_a_safety_check_is_kept_and_tracked(self):
        # Moved out of the handover dir: never deleted, never forgotten.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)
        index_lib.HANDOVER_DIR = os.path.join(self._tmp, "elsewhere")
        os.makedirs(index_lib.HANDOVER_DIR)

        self.assertEqual(self.sweep_sessions("C", "D", "E", "F"), [])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(len(index_lib.load_index()), 1)


class TestArchive(IndexTestCase):
    """Retirement moves the file; it does not delete it."""

    def retire(self, path, name=None):
        """Push `path` all the way through both grace gates."""
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)
        return self.sweep_sessions("C", "D", "E")

    def test_archive_dir_follows_a_rebound_handover_dir(self):
        # archive_dir() is a function precisely so this holds. Computed at
        # import it would point at whatever was configured then, and the sweep
        # would file into a stale directory while reporting success.
        moved = os.path.join(self._tmp, "somewhere-else")
        index_lib.HANDOVER_DIR = moved
        self.assertEqual(index_lib.archive_dir(),
                         os.path.join(moved, "archive", "handovers"))

    def test_archive_dir_is_created_on_demand(self):
        self.assertFalse(os.path.isdir(index_lib.archive_dir()))
        path = self.make_handover()
        self.assertEqual(self.retire(path), [path])
        self.assertTrue(os.path.isdir(index_lib.archive_dir()))

    def test_archived_name_still_matches_the_handover_pattern(self):
        # Load-bearing, not cosmetic: `spec-archive` refuses handover-named
        # files, and that refusal is what keeps two archivers off one document.
        #
        # Retires TWO same-named handovers so the collision branch -- the only
        # branch that can produce a non-matching name -- is actually reached.
        # With one file this asserted a tautology (is_safe_to_retire already
        # required the pattern, and a plain move preserves it) over a loop that
        # also passed on zero iterations.
        first = self.make_handover()
        self.assertEqual(self.retire(first), [first])
        index_lib.save_index([])
        second = self.make_handover()
        self.assertEqual(self.retire(second), [second])

        names = self.archive_names()
        self.assertEqual(len(names), 2, "both must be archived, or the loop "
                                        "below proves nothing")
        for name in names:
            self.assertRegex(name, index_lib.HANDOVER_NAME_RE)

    def test_a_same_named_handover_does_not_clobber_its_predecessor(self):
        # Two threads on two days can genuinely pick the same slug. The first
        # one archived must survive the second.
        first = self.make_handover(body="first")
        self.assertEqual(self.retire(first), [first])

        index_lib.save_index([])
        second = self.make_handover(body="second")
        self.assertEqual(self.retire(second), [second])

        self.assertEqual(self.archive_names(),
                         ["2026-08-10-demo-2-handover.md",
                          "2026-08-10-demo-handover.md"])
        with open(self.archived()) as handle:
            self.assertEqual(handle.read(), "first")
        with open(self.archived("2026-08-10-demo-2-handover.md")) as handle:
            self.assertEqual(handle.read(), "second")

    def test_collision_suffix_goes_before_the_handover_token(self):
        # Asserted directly rather than inferred from the sweep, so that a
        # rename to `-handover-2.md` -- which would opt the file back into
        # spec-archive -- fails here and names the reason.
        directory = index_lib.archive_dir()
        os.makedirs(directory)
        open(os.path.join(directory, "2026-08-10-demo-handover.md"), "w").close()
        chosen = index_lib._archive_destination(
            "2026-08-10-demo-handover.md", directory)
        self.assertEqual(os.path.basename(chosen),
                         "2026-08-10-demo-2-handover.md")
        self.assertRegex(os.path.basename(chosen), index_lib.HANDOVER_NAME_RE)

    def test_a_failed_move_keeps_the_record_and_the_file(self):
        # If the move fails the handover is NOT retired. Dropping the row would
        # strand it in the handover dir with nothing tracking it.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)

        original = index_lib._archive_destination

        def fail_move(basename, directory):
            raise OSError("no space left on device")

        index_lib._archive_destination = fail_move
        try:
            self.assertEqual(self.sweep_sessions("C", "D", "E"), [])
        finally:
            index_lib._archive_destination = original

        self.assertTrue(os.path.isfile(path))
        self.assertEqual(len(index_lib.load_index()), 1)

    def test_a_failed_move_is_retried_by_the_next_sweep(self):
        # The record survives a failure, so recovery must not need a human.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=30)

        original = index_lib._archive_destination
        index_lib._archive_destination = lambda b, d: (_ for _ in ()).throw(
            OSError("transient"))
        try:
            self.assertEqual(self.sweep_sessions("C", "D", "E"), [])
        finally:
            index_lib._archive_destination = original

        self.assertEqual(index_lib.sweep("F"), [path])
        self.assertTrue(os.path.isfile(self.archived()))

    def test_an_archived_handover_is_never_offered_again(self):
        # Pin WHERE it went as well as that it is unoffered. Asserting only the
        # find_* calls is green whether the file was archived, deleted, or
        # orphaned in place -- the index row is gone either way, so the
        # assertion cannot tell those apart and is not evidence of archiving.
        path = self.make_handover()
        self.retire(path)

        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.isfile(self.archived()))
        self.assertIsNone(index_lib.find_pending("/proj", "Z"))
        self.assertEqual(index_lib.find_all_pending("/proj", "Z"), [])

    def test_later_sweeps_leave_the_archive_alone(self):
        # The archive lives INSIDE the handover dir, so containment alone does
        # not keep the sweep out of it. What does is that retirement drops the
        # index row, and nothing ever registers a file in the archive.
        #
        # A live record is kept in the index throughout, deliberately: without
        # one the later sweeps return at `if not records` and the test passes
        # without reaching a single gate -- which is what the first version of
        # this test did.
        retired = self.make_handover()
        self.retire(retired)

        live = self.make_handover(name="2026-08-11-live-handover.md")
        index_lib.add_record(live, "/proj", "A", "2026-08-11T10:00:00")
        self.consume(live, "B", days_ago=0)          # consumed, nowhere near the floor

        before = self.archive_names()
        self.assertEqual(before, ["2026-08-10-demo-handover.md"])

        self.sweep_sessions("F", "G", "H")

        self.assertEqual(self.archive_names(), before)
        self.assertTrue(os.path.isfile(live), "the live record must still be "
                                              "in play, or the sweep never "
                                              "iterated and this proves nothing")
        self.assertEqual(len(index_lib.load_index()), 1)


class TestFindPending(IndexTestCase):

    def test_returns_unconsumed_without_marking_it(self):
        # The core of the explicit-claim model: merely looking must not consume.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")

        self.assertEqual(index_lib.find_pending("/proj", "B"), path)
        record = index_lib.load_index()[0]
        self.assertIsNone(record["consumed_session_id"])
        self.assertIsNone(record["consumed_at"])

    def test_is_idempotent_across_concurrent_sessions(self):
        # Three windows start in the same tree; none of them claims it, so all
        # three still see it. Under the old delivery model the first won.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        for session in ("B", "C", "D"):
            self.assertEqual(index_lib.find_pending("/proj", session), path)

    def test_returns_none_when_already_consumed(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        index_lib.claim(path, "B")
        self.assertIsNone(index_lib.find_pending("/proj", "C"))

    def test_ignores_other_project_dirs(self):
        path = self.make_handover()
        index_lib.add_record(path, "/other", "A", "2026-08-10T10:00:00")
        self.assertIsNone(index_lib.find_pending("/proj", "B"))

    def test_returns_newest_of_several(self):
        old = self.make_handover(name="2026-08-09-old-handover.md")
        new = self.make_handover(name="2026-08-10-new-handover.md")
        index_lib.add_record(old, "/proj", "A", "2026-08-09T10:00:00")
        index_lib.add_record(new, "/proj", "A", "2026-08-10T10:00:00")
        self.assertEqual(index_lib.find_pending("/proj", "B"), new)

    def test_find_all_pending_returns_every_candidate_newest_first(self):
        # find_pending answers "which one do we announce?". Deciding what the
        # user ATTACHED has to consider all of them — checking only the newest
        # is how an attachment of an older document went unnoticed.
        old = self.make_handover(name="2026-08-09-old-handover.md")
        new = self.make_handover(name="2026-08-10-new-handover.md")
        index_lib.add_record(old, "/proj", "A", "2026-08-09T10:00:00")
        index_lib.add_record(new, "/proj", "A", "2026-08-10T10:00:00")
        self.assertEqual(index_lib.find_all_pending("/proj", "B"), [new, old])

    def test_find_all_pending_is_empty_when_nothing_is_pending(self):
        self.assertEqual(index_lib.find_all_pending("/proj", "B"), [])

    def test_find_all_pending_excludes_consumed_and_own_records(self):
        mine = self.make_handover(name="2026-08-08-mine-handover.md")
        taken = self.make_handover(name="2026-08-09-taken-handover.md")
        free = self.make_handover(name="2026-08-10-free-handover.md")
        index_lib.add_record(mine, "/proj", "B", "2026-08-08T10:00:00")
        index_lib.add_record(taken, "/proj", "A", "2026-08-09T10:00:00")
        index_lib.add_record(free, "/proj", "A", "2026-08-10T10:00:00")
        index_lib.claim(taken, "C")
        self.assertEqual(index_lib.find_all_pending("/proj", "B"), [free])

    def test_find_pending_agrees_with_the_head_of_find_all_pending(self):
        # The two must never disagree about which document is newest.
        old = self.make_handover(name="2026-08-09-old-handover.md")
        new = self.make_handover(name="2026-08-10-new-handover.md")
        index_lib.add_record(old, "/proj", "A", "2026-08-09T10:00:00")
        index_lib.add_record(new, "/proj", "A", "2026-08-10T10:00:00")
        self.assertEqual(index_lib.find_pending("/proj", "B"),
                         index_lib.find_all_pending("/proj", "B")[0])

    def test_missing_file_is_skipped(self):
        ghost = os.path.join(self.handover_dir, "2026-08-10-ghost-handover.md")
        index_lib.add_record(ghost, "/proj", "A", "2026-08-10T10:00:00")
        self.assertIsNone(index_lib.find_pending("/proj", "B"))

    def test_corrupt_index_returns_none(self):
        with open(index_lib.INDEX_PATH, "w") as handle:
            handle.write("{not json")
        self.assertIsNone(index_lib.find_pending("/proj", "B"))

    def test_creating_session_does_not_see_own_handover(self):
        # SessionStart fires on compact/resume with the SAME session id. The
        # writing session must never be pointed at its own document.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.assertIsNone(index_lib.find_pending("/proj", "A"))
        self.assertEqual(index_lib.find_pending("/proj", "B"), path)

    def test_subdirectory_cwd_sees_handover(self):
        path = self.make_handover()
        index_lib.add_record(path, self._tmp, "A", "2026-08-10T10:00:00")
        nested = os.path.join(self._tmp, "repos", "automations")
        os.makedirs(nested)
        self.assertEqual(index_lib.find_pending(nested, "B"), path)

    def test_sibling_dir_does_not_see_handover(self):
        # A prefix match must not leak across siblings: /tmp/x must not match
        # a record registered at /tmp/x-other.
        path = self.make_handover()
        index_lib.add_record(path, self._tmp + "-other", "A",
                             "2026-08-10T10:00:00")
        self.assertIsNone(index_lib.find_pending(self._tmp, "B"))


class TestClaim(IndexTestCase):

    def test_marks_record_consumed(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")

        self.assertTrue(index_lib.claim(path, "B"))
        record = index_lib.load_index()[0]
        self.assertEqual(record["consumed_session_id"], "B")
        self.assertIsNotNone(record["consumed_at"])

    def test_claim_starts_the_grace_clock(self):
        # End to end: claim, then wait out both gates, then it is swept.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        index_lib.claim(path, "B")

        # Freshly claimed: no session count, no elapsed time.
        self.assertEqual(self.sweep_sessions("B", "C", "D", "E"), [])
        self.assertTrue(os.path.isfile(path))

        self.consume(path, "B", days_ago=30)
        self.assertEqual(index_lib.sweep("F"), [path])
        self.assertFalse(os.path.exists(path))

    def test_unclaimed_handover_is_never_swept(self):
        # The cost of explicit claim: nobody attaches it, nobody deletes it.
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        for session in ("B", "C", "D"):
            self.assertEqual(index_lib.sweep(session), [])
        self.assertTrue(os.path.isfile(path))

    def test_claiming_newest_leaves_older_pending(self):
        # `claim` used to consume every older handover under the same root, on
        # the reasoning that the newer one superseded it. `project_dir` is always
        # the workspace root in practice, so that meant every handover on the
        # machine — and the sweep deleted them a session later, unread.
        old = self.make_handover(name="2026-08-09-old-handover.md")
        new = self.make_handover(name="2026-08-10-new-handover.md")
        index_lib.add_record(old, "/proj", "A", "2026-08-09T10:00:00")
        index_lib.add_record(new, "/proj", "A", "2026-08-10T10:00:00")

        self.assertTrue(index_lib.claim(new, "B"))
        by_path = {r["path"]: r for r in index_lib.load_index()}
        self.assertEqual(by_path[new]["consumed_session_id"], "B")
        self.assertIsNone(by_path[old]["consumed_session_id"])

    def test_claiming_older_leaves_newer_pending(self):
        # Attaching an older document must not silently discard a newer one.
        old = self.make_handover(name="2026-08-09-old-handover.md")
        new = self.make_handover(name="2026-08-10-new-handover.md")
        index_lib.add_record(old, "/proj", "A", "2026-08-09T10:00:00")
        index_lib.add_record(new, "/proj", "A", "2026-08-10T10:00:00")

        self.assertTrue(index_lib.claim(old, "B"))
        by_path = {r["path"]: r for r in index_lib.load_index()}
        self.assertEqual(by_path[old]["consumed_session_id"], "B")
        self.assertIsNone(by_path[new]["consumed_session_id"])

    def test_claim_touches_no_other_record_whatever_the_root(self):
        # Same root or different, older or newer: exactly one record changes.
        mine = self.make_handover(name="2026-08-10-mine-handover.md")
        same_root = self.make_handover(name="2026-08-09-same-handover.md")
        other_root = self.make_handover(name="2026-08-09-theirs-handover.md")
        index_lib.add_record(mine, "/proj", "A", "2026-08-10T10:00:00")
        index_lib.add_record(same_root, "/proj", "A", "2026-08-09T10:00:00")
        index_lib.add_record(other_root, "/other", "A", "2026-08-09T10:00:00")

        index_lib.claim(mine, "B")
        by_path = {r["path"]: r for r in index_lib.load_index()}
        self.assertEqual(by_path[mine]["consumed_session_id"], "B")
        self.assertIsNone(by_path[same_root]["consumed_session_id"])
        self.assertIsNone(by_path[other_root]["consumed_session_id"])

    def test_an_unclaimed_sibling_survives_the_sweep_that_retires_the_claimed_one(self):
        # End to end, the failure this fix exists to prevent: continuing thread
        # A must not destroy thread B's handover.
        a = self.make_handover(name="2026-08-09-alpha-handover.md")
        b = self.make_handover(name="2026-08-10-bravo-handover.md")
        index_lib.add_record(a, "/proj", "A", "2026-08-09T10:00:00")
        index_lib.add_record(b, "/proj", "A", "2026-08-10T10:00:00")

        index_lib.claim(b, "B")
        self.consume(b, "B", days_ago=30)
        self.sweep_sessions("L1", "L2", "L3")
        self.assertFalse(os.path.isfile(b))
        self.assertTrue(os.path.isfile(a))

    def test_unknown_path_claims_nothing(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")

        self.assertFalse(index_lib.claim("/nope/2026-08-10-x-handover.md", "B"))
        self.assertIsNone(index_lib.load_index()[0]["consumed_session_id"])

    def test_already_consumed_path_claims_nothing(self):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        index_lib.claim(path, "B")
        self.assertFalse(index_lib.claim(path, "C"))
        self.assertEqual(index_lib.load_index()[0]["consumed_session_id"], "B")

    def test_empty_index_claims_nothing(self):
        self.assertFalse(index_lib.claim("/whatever-handover.md", "B"))


class ShippedDefaultsTest(unittest.TestCase):
    """The two grace numbers a fresh clone gets are policy, not incidentals.

    Nothing pinned them before, which meant the floor could be changed — or
    silently reverted — with every suite still green. `IndexTestCase` overrides
    both on purpose, so it cannot serve as this pin: its values prove the gate
    LOGIC and are deliberately not the shipped ones.

    Run in a subprocess against a nonexistent config, because both constants are
    resolved at import time from whatever config the machine happens to have.
    Asserting them in-process would grade the runner's config, not the repo.
    """

    def _defaults(self):
        env = dict(os.environ, CLAUDE_TOOLKIT_CONFIG="/nonexistent.json")
        tool_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.check_output(
            [sys.executable, "-c",
             "import index_lib as i; print(i.GRACE_SESSIONS, i.GRACE_DAYS)"],
            cwd=tool_dir, env=env, universal_newlines=True)
        return [int(part) for part in out.split()]

    def test_grace_defaults_are_three_sessions_and_seven_days(self):
        # The DEFAULT day floor stays 7 on purpose: absent config is what every
        # teammate gets, and it decides how long a document stays where people
        # will look for it. (The original ground -- an unrecoverable delete --
        # expired 2026-08-26 when the sweep began archiving; the pin stands on
        # the replacement, see the GRACE_DAYS comment.) Lowering it is a config
        # choice, and this pin is what stops the lowering leaking into the
        # shared default. With several windows open the day floor is in practice
        # the ONLY gate with effect — see the comment on GRACE_DAYS.
        #
        # This pins the numbers and nothing else. A companion test asserting
        # "both gates still exist" was written here and removed: reading two
        # constants and checking they are positive cannot detect the gate
        # collapse it named — mutation-verified, `_grace_cleared` returning
        # True unconditionally left it green — and it was already implied by
        # the assertEqual above. The collapse is covered by the behavioural
        # tests in IndexTestCase, which do catch that mutation.
        self.assertEqual(self._defaults(), [3, 7])


class TestIndexTransactions(IndexTestCase):
    """Read-modify-write on the index is one transaction, and a bad write is
    never visible. Race tests pause writer A at a seam, start B, and assert B
    is blocked -- deterministic, no sleeps-and-hope."""

    def _run_paused_race(self, first, second):
        """Run `first` on thread A, paused right after it has read the index;
        run `second` on thread B. Returns (b_was_blocked, errors)."""
        a_loaded = threading.Event()
        resume_a = threading.Event()
        real_read = getattr(index_lib, "_read_for_update", None)
        errors = []

        def wrapper(*args, **kwargs):
            result = real_read(*args, **kwargs) if real_read else None
            if threading.current_thread().name == "A":
                a_loaded.set()
                resume_a.wait(10)
            return result

        def guarded(fn):
            def run():
                try:
                    fn()
                except Exception as exc:
                    errors.append(exc)
            return run

        # create=True so a baseline run (no such function) fails on behaviour,
        # not on the patch itself.
        with mock.patch.object(index_lib, "_read_for_update", wrapper,
                               create=True):
            thread_a = threading.Thread(target=guarded(first), name="A")
            thread_b = threading.Thread(target=guarded(second), name="B")
            thread_a.start()
            a_loaded.wait(5)
            thread_b.start()
            thread_b.join(timeout=0.3)
            b_was_blocked = thread_b.is_alive()
            resume_a.set()
            thread_a.join(timeout=10)
            thread_b.join(timeout=10)
            self.assertFalse(thread_a.is_alive() or thread_b.is_alive())
        return b_was_blocked, errors

    def _register(self, name):
        path = self.make_handover(name)
        return path, lambda: index_lib.add_record(
            path, self._tmp, "creator", "2026-08-10T10:00:00")

    def test_concurrent_registrations_both_survive(self):
        path_a, register_a = self._register("2026-08-10-a-handover.md")
        path_b, register_b = self._register("2026-08-10-b-handover.md")
        blocked, errors = self._run_paused_race(register_a, register_b)
        self.assertTrue(blocked, "B was not blocked while A held the index")
        self.assertEqual(errors, [])
        paths = [r["path"] for r in index_lib.load_index()]
        self.assertIn(path_a, paths)
        self.assertIn(path_b, paths)

    def test_sweep_does_not_drop_a_concurrent_registration(self):
        old = self.make_handover("2026-08-01-old-handover.md")
        index_lib.add_record(old, self._tmp, "creator", "2026-08-01T10:00:00")
        self.consume(old, session="B", days_ago=30)
        path_b, register_b = self._register("2026-08-10-b-handover.md")
        blocked, errors = self._run_paused_race(
            lambda: index_lib.sweep("S"), register_b)
        self.assertTrue(blocked, "B was not blocked while the sweep held the index")
        self.assertEqual(errors, [])
        self.assertIn(path_b, [r["path"] for r in index_lib.load_index()])

    def test_failed_write_leaves_the_old_index_intact(self):
        first = self.make_handover("2026-08-10-a-handover.md")
        index_lib.add_record(first, self._tmp, "creator", "2026-08-10T10:00:00")

        def half_write(records, handle, *args, **kwargs):
            handle.write("{")
            raise OSError("disk full")

        with mock.patch.object(index_lib.json, "dump", half_write):
            with self.assertRaises(OSError):
                index_lib.add_record("/x/2026-08-11-b-handover.md", self._tmp,
                                     "creator", "2026-08-11T10:00:00")
        self.assertEqual([r["path"] for r in index_lib.load_index()], [first])

    def _write_corrupt(self):
        with open(index_lib.INDEX_PATH, "w", encoding="utf-8") as handle:
            handle.write("{not json")
        with open(index_lib.INDEX_PATH, "rb") as handle:
            return handle.read()

    def _index_bytes(self):
        with open(index_lib.INDEX_PATH, "rb") as handle:
            return handle.read()

    def test_add_record_refuses_to_overwrite_a_corrupt_index(self):
        before = self._write_corrupt()
        with self.assertRaises(index_lib.CorruptIndex):
            index_lib.add_record("/x/2026-08-10-a-handover.md", self._tmp,
                                 "creator", "2026-08-10T10:00:00")
        self.assertEqual(self._index_bytes(), before)

    def test_hooks_leave_a_corrupt_index_untouched(self):
        before = self._write_corrupt()
        self.assertEqual(index_lib.sweep("S"), [])
        self.assertIs(index_lib.claim("/x/2026-08-10-a-handover.md", "S"), False)
        self.assertEqual(self._index_bytes(), before)

    def test_lock_is_beside_the_index(self):
        index_lib.add_record("/x/2026-08-10-a-handover.md", self._tmp,
                             "creator", "2026-08-10T10:00:00")
        self.assertTrue(os.path.exists(index_lib.INDEX_PATH + ".lock"))
        leftovers = [n for n in os.listdir(os.path.dirname(index_lib.INDEX_PATH))
                     if ".tmp-" in n]
        self.assertEqual(leftovers, [])

    def test_contended_lock_does_not_mutate(self):
        index_lib.add_record("/x/2026-08-10-a-handover.md", self._tmp,
                             "creator", "2026-08-10T10:00:00")
        before = self._index_bytes()
        orig = (index_lib.LOCK_TIMEOUT,
                getattr(index_lib, "ADD_LOCK_TIMEOUT", None))
        index_lib.LOCK_TIMEOUT = 0.2
        index_lib.ADD_LOCK_TIMEOUT = 0.2
        try:
            with index_lib.portable.exclusive_lock(
                    index_lib.INDEX_PATH + ".lock") as held:
                self.assertTrue(held)
                with self.assertRaises(index_lib.IndexBusy):
                    index_lib.add_record("/x/2026-08-11-b-handover.md",
                                         self._tmp, "creator",
                                         "2026-08-11T10:00:00")
                self.assertEqual(index_lib.sweep("S"), [])
                self.assertIs(
                    index_lib.claim("/x/2026-08-10-a-handover.md", "S"), False)
                self.assertEqual(self._index_bytes(), before)
        finally:
            index_lib.LOCK_TIMEOUT = orig[0]
            if orig[1] is None:
                if hasattr(index_lib, "ADD_LOCK_TIMEOUT"):
                    del index_lib.ADD_LOCK_TIMEOUT
            else:
                index_lib.ADD_LOCK_TIMEOUT = orig[1]
        self.assertEqual(self._index_bytes(), before)

    def test_unavailable_lock_mechanism_still_registers(self):
        with mock.patch.object(index_lib, "portable", None):
            index_lib.add_record("/x/2026-08-10-a-handover.md", self._tmp,
                                 "creator", "2026-08-10T10:00:00")
        self.assertEqual([r["path"] for r in index_lib.load_index()],
                         ["/x/2026-08-10-a-handover.md"])


class TestZeroGraceSessions(IndexTestCase):
    """`sweep_grace_sessions: 0` is unpredictable, not lenient.

    With no session gate, `_witness` never appends, so whether a record is
    ever archived depends only on whether it already carries a witness list:
    one that does is archived at the day floor, one that does not is kept
    forever. That is why the config documents `enabled: false`, not 0, as the
    off switch; a fresh record surviving a sweep must not read as "0 means off".
    """

    def setUp(self):
        super().setUp()
        index_lib.GRACE_SESSIONS = 0
        index_lib.GRACE_DAYS = 7

    def consumed_record(self, days_ago, witness_list=None):
        path = self.make_handover()
        index_lib.add_record(path, "/proj", "A", "2026-08-10T10:00:00")
        self.consume(path, "B", days_ago=days_ago)
        if witness_list is not None:
            records = index_lib.load_index()
            for record in records:
                if record.get("path") == path:
                    record["witness_session_ids"] = witness_list
            index_lib.save_index(records)
        return path

    def test_a_record_with_a_witness_list_is_archived_at_the_day_floor(self):
        path = self.consumed_record(days_ago=8, witness_list=[])

        self.assertEqual(index_lib.sweep("C"), [path])
        self.assertFalse(os.path.exists(path))

    def test_a_record_with_a_witness_list_waits_for_the_day_floor(self):
        path = self.consumed_record(days_ago=1, witness_list=[])

        self.assertEqual(index_lib.sweep("C"), [])
        self.assertTrue(os.path.isfile(path))

    def test_a_record_with_no_witness_list_is_kept_forever(self):
        path = self.consumed_record(days_ago=400)

        self.assertEqual(index_lib.sweep("C"), [])
        self.assertTrue(os.path.isfile(path))
        # _witness never adds the key when the session gate is 0.
        self.assertIsNone(self.witnesses(path))


if __name__ == "__main__":
    unittest.main()
