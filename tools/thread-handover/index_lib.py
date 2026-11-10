#!/usr/bin/env python3
"""Handover document index: registration, delivery selection, safe retirement.

The index is the only record of which handover files exist and whether they
have been consumed. Retirement is gated on several independent conditions
because the handover directory is unversioned scratch.

**The sweep ARCHIVES; it does not delete.** A successor thread is the only
thing that carries an unresolved item forward, and nothing checks that it did,
so deleting the predecessor on a clock keyed to *attachment* rather than to
resolution would make a dropped item unrecoverable on a timer. Moving the file
costs a directory nobody looks in and makes that whole class of loss a `mv`
back, the same trade `spec-archive` makes.

The grace gates stay regardless. They are cheap, and they still do the thing
they were built for: keeping a document in place while the session that
attached it is still working from it.
"""

# Design decisions about this file that look like accidental complexity are
# recorded in docs/decisions.md. Read the relevant one before simplifying
# anything here.

import contextlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# Machine-local settings live outside the repo. See lib/toolkit_config.py.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "lib"))
import toolkit_config as _config
try:
    import portable
except Exception:
    # A missing lock must not break a hook, so it fails open -- the same
    # contract as claim_lib.locked.
    portable = None

INDEX_PATH = os.path.expanduser("~/.claude/thread-handover/index.json")

# The one directory the sweep may act on. Configurable, and defaulting to a
# dedicated directory rather than a shared docs folder: giving it its own
# directory bounds the blast radius by construction instead of by filename regex.
HANDOVER_DIR = _config.handover_dir()

HANDOVER_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-.+-handover\.md$")

# Where a retired handover goes, under HANDOVER_DIR. `spec-archive` already owns
# `<specs>/archive/`, and it refuses any handover-named file outright precisely
# so the two tools cannot both claim one -- so a subdirectory of its archive is
# free real estate rather than a collision.
ARCHIVE_DIR_NAME = "archive"
ARCHIVE_SUBDIR = "handovers"


def archive_dir() -> str:
    """The retirement directory, resolved from HANDOVER_DIR at CALL time.

    Deliberately a function, not a module constant. HANDOVER_DIR is rebound --
    by the tests, and in principle by anything that reloads config -- and a
    constant computed at import would still point at the directory that was
    configured when this module first loaded. That failure is silent: the sweep
    would archive into a stale path and report success.
    """
    return os.path.join(HANDOVER_DIR, ARCHIVE_DIR_NAME, ARCHIVE_SUBDIR)

# How long a consumed handover stays put before the sweep may retire it into
# archive_dir(). BOTH gates must clear, and they cover different failure modes.
#
# The session count answers "have I moved on?", but on its own it is not a
# grace period at all when several windows are open in one tree. SessionStart
# fires per window and per /clear, so three tabs opening in the same minute
# would retire a document the session that attached it is still working from.
#
# The day floor is what makes the delay real, because wall-clock time cannot be
# accelerated by concurrency. Keep both: the floor alone would retire a document
# whether or not the work ever moved on, and the count alone protects nothing on
# a busy afternoon.
#
# In practice, read `sweep_grace_days` as THE grace period. With several windows
# open the session count is spent within minutes, and `_witness` caps the list
# at GRACE_SESSIONS, so it stays cleared. The day gate is exact wall clock:
# `_days_elapsed_since` returns fractional days and `_grace_cleared` tests `>=`,
# so 3 means 72.0 hours from the attachment, and any session other than the
# consuming one can run the sweep that moves it.
#
# The compiled-in floor is 7: at 7 a handover attached Friday afternoon stays
# put until the following Friday afternoon; at 3 it is eligible Monday. A
# shorter value is a legitimate preference and belongs in config, not in the
# default, which is what an unconfigured install gets.
GRACE_SESSIONS = _config.get_int("thread_handover", "sweep_grace_sessions",
                                 default=3)
GRACE_DAYS = _config.get_int("thread_handover", "sweep_grace_days", default=7)


LOCK_TIMEOUT = 2.0
# add_record waits longer than the hooks: a registration that is dropped is
# visible to the /handover snippet, while a skipped sweep or claim is not.
ADD_LOCK_TIMEOUT = 10.0


class CorruptIndex(Exception):
    """The index file exists but is not a JSON list."""


class IndexBusy(Exception):
    """The index lock stayed contended for the whole timeout."""


@contextlib.contextmanager
def _transaction(timeout):
    """Serialise load -> mutate -> save on the index across processes.

    Yields True when the body may run and False when it must not. Two
    different failures are told apart:

    - The lock MECHANISM is unavailable (module missing, or the lock file
      cannot be created or opened): yields True and the body runs unlocked,
      as it did before locking existed. Fails open, like claim_lib.locked.
    - The lock is CONTENDED (not acquired within `timeout`): yields False.
      A timeout is positive evidence of a concurrent writer, so the caller
      must not read-modify-write.

    Not nestable: a second flock on a second fd in the same process would
    wait on the first.
    """
    with contextlib.ExitStack() as stack:
        acquired = True
        try:
            # Computed here, not at import: tests repoint INDEX_PATH.
            lock_path = INDEX_PATH + ".lock"
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)
            if portable is not None:
                acquired = stack.enter_context(
                    portable.exclusive_lock(lock_path, timeout=timeout))
        except Exception:
            acquired = True
        yield bool(acquired)


def _read_for_update() -> List[Dict[str, Any]]:
    """The records to modify and write back. Missing file -> [].

    Unlike load_index, a file that is present but unreadable raises: writing
    back an empty list over it would wipe every registration.
    """
    try:
        with open(INDEX_PATH, encoding="utf-8") as handle:
            records = json.load(handle)
    except FileNotFoundError:
        return []
    except Exception as exc:
        raise CorruptIndex(str(exc))
    if not isinstance(records, list):
        raise CorruptIndex("index is not a list")
    return records


def _write_index(records: List[Dict[str, Any]]) -> None:
    """Write via a temp file and rename, so a failed write never truncates."""
    os.makedirs(os.path.dirname(INDEX_PATH), exist_ok=True)
    temp_path = INDEX_PATH + ".tmp-%d" % os.getpid()
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2)
        os.replace(temp_path, INDEX_PATH)
    except BaseException:
        try:
            os.remove(temp_path)
        except OSError:
            pass
        raise


def load_index() -> List[Dict[str, Any]]:
    """All handover records. Corrupt or missing index -> empty list."""
    try:
        with open(INDEX_PATH, encoding="utf-8") as handle:
            records = json.load(handle)
    except Exception:
        return []
    return records if isinstance(records, list) else []


def save_index(records: List[Dict[str, Any]]) -> None:
    """Persist the index. Never raises."""
    try:
        _write_index(records)
    except Exception:
        pass


def add_record(path: str, project_dir: str, session_id: str,
               created_at: str) -> None:
    """Register a newly written handover document.

    Raises on a corrupt index, a failed write, or (IndexBusy) a lock that
    stayed contended: a registration that did not land must be visible to the
    caller.
    """
    with _transaction(ADD_LOCK_TIMEOUT) as locked:
        if not locked:
            raise IndexBusy("index lock held by another writer")
        records = _read_for_update()
        records.append({
            "path": path,
            "project_dir": project_dir,
            "created_at": created_at,
            "created_session_id": session_id,
            "consumed_at": None,
            "consumed_session_id": None,
        })
        _write_index(records)


def is_safe_to_retire(path: str, indexed_paths: set) -> bool:
    """All five conditions must hold, or the file is left alone.

    Still strict now that retirement archives rather than deletes. Moving the
    wrong file is cheaper than deleting it, but it is not free -- the file
    vanishes from where its owner left it, and the symlink case below would
    move somebody else's still-pending handover out from under them.
    """
    if path not in indexed_paths:
        return False
    if os.path.islink(path):
        # Every other gate below resolves through realpath, and sweep() acts on
        # realpath(path) -- so a symlink inside specs/ pointing at a different
        # indexed handover would pass all of them and retire the TARGET.
        # Nothing this tool writes is ever a symlink; reject the whole class.
        return False
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(HANDOVER_DIR) + os.sep):
        return False
    if not HANDOVER_NAME_RE.match(os.path.basename(real)):
        return False
    return os.path.isfile(real)


def _days_elapsed_since(stamp: Optional[str], now: Optional[datetime] = None
                        ) -> Optional[float]:
    """Days between `stamp` and now, or None when `stamp` is unusable.

    None is the strict answer, and every caller must read it as "not eligible".
    A record whose `consumed_at` is missing or malformed is a record we cannot
    date, and an undatable record must never be deleted — clutter is the
    documented trade everywhere else in this tool.
    """
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    reference = now or datetime.now()
    try:
        return (reference - when).total_seconds() / 86400.0
    except TypeError:
        # Naive/aware mismatch. Retry against the same offset rather than
        # guessing a timezone for the older, naive side.
        try:
            return (reference.replace(tzinfo=when.tzinfo)
                    - when).total_seconds() / 86400.0
        except Exception:
            return None


def _witness(record: Dict[str, Any], session_id: str) -> bool:
    """Note that `session_id` has been seen since this record was consumed.

    Returns True when the record changed. The list is capped at GRACE_SESSIONS
    because nothing above the threshold changes any decision — without the cap
    a record waiting out the day floor would accumulate a session id per window
    for a week.
    """
    seen = record.get("witness_session_ids")
    if not isinstance(seen, list):
        seen = []
    if session_id in seen or len(seen) >= GRACE_SESSIONS:
        return False
    seen.append(session_id)
    record["witness_session_ids"] = seen
    return True


def _grace_cleared(record: Dict[str, Any],
                   now: Optional[datetime] = None) -> bool:
    """True when both grace gates have been satisfied for this record."""
    seen = record.get("witness_session_ids")
    if not isinstance(seen, list) or len(seen) < GRACE_SESSIONS:
        return False
    elapsed = _days_elapsed_since(record.get("consumed_at"), now)
    return elapsed is not None and elapsed >= GRACE_DAYS


def sweep(current_session_id: str) -> List[str]:
    """Archive consumed handovers that have cleared both grace gates.

    Returns the ORIGINAL paths of everything retired this call.

    A consumed handover is retired only after GRACE_SESSIONS distinct other
    sessions have started AND GRACE_DAYS have passed since it was attached.
    Before that this call only records the current session as a witness.

    Retiring at the first SessionStart from any session other than
    the consuming one would be wrong, though it reads like "one session of
    recovery time". With several windows open in one tree, and SessionStart firing on
    every new tab and every /clear, retirement would routinely land minutes after
    the attachment, while the session that attached it was still working, and
    the handover directory and the index would both be found empty.

    It also used to DELETE, which is what made that story unrecoverable. It
    archives now — see the module docstring for why the discretionary
    carry-forward step is not something to hang an irreversible action on.
    """
    with _transaction(LOCK_TIMEOUT) as locked:
        if not locked:
            return []
        try:
            records = _read_for_update()
        except CorruptIndex:
            return []

        indexed = {record.get("path") for record in records if record.get("path")}
        retired = []
        surviving = []
        changed = False

        for record in records:
            consumed_by = record.get("consumed_session_id")
            path = record.get("path")
            if consumed_by and consumed_by != current_session_id and path:
                if not os.path.isfile(path):
                    # Already gone — drop the stale record.
                    changed = True
                    continue
                changed = _witness(record, current_session_id) or changed
                if (_grace_cleared(record) and is_safe_to_retire(path, indexed)
                        and _archive(path)):
                    retired.append(path)
                    changed = True
                    continue
                # Still inside the grace period, the file failed a safety check
                # (renamed, moved out of the handover dir), or the archive move
                # failed. Keep tracking it in all three cases.
            surviving.append(record)

        if changed:
            save_index(surviving)
        return retired


def _archive_destination(basename: str, directory: str) -> str:
    """A free path in `directory` for `basename`, never clobbering.

    The suffix goes BEFORE `-handover.md`, not after the extension, so the
    result still matches HANDOVER_NAME_RE. That is load-bearing rather than
    tidy: `spec-archive` refuses to touch a handover-named file, and that
    refusal is the only thing keeping two archivers off the same document.
    A `-2.md` tail would silently opt the file back into the other tool.
    """
    candidate = os.path.join(directory, basename)
    if not os.path.exists(candidate):
        return candidate

    stem = basename[:-len("-handover.md")]
    for n in range(2, 1000):
        candidate = os.path.join(directory, "%s-%d-handover.md" % (stem, n))
        if not os.path.exists(candidate):
            return candidate
    raise OSError("no free archive name for " + basename)


def _archive(path: str) -> bool:
    """Move a retired handover into the archive. True when it landed there.

    False means the file is still where it was, and the caller must keep the
    record: a handover we failed to move is not retired, and dropping its row
    would strand it in the handover directory with nothing tracking it.
    """
    try:
        directory = archive_dir()
        os.makedirs(directory, exist_ok=True)
        real = os.path.realpath(path)
        os.rename(real, _archive_destination(os.path.basename(real), directory))
        return True
    except OSError:
        # Includes the cross-device case, which os.rename cannot do. Left as a
        # failure on purpose: HANDOVER_DIR and its own subdirectory are on one
        # filesystem in every configuration this tool supports, so a raised
        # EXDEV means the layout is not what we think and a copy-then-unlink
        # fallback would be deleting the original on a guess.
        return False


def _within(child: str, parent: str) -> bool:
    """True when `child` is `parent` or lives underneath it.

    The os.sep suffix is what stops /a/b from matching a record at /a/b-other.
    """
    if not child or not parent:
        return False
    try:
        child_real = os.path.realpath(child)
        parent_real = os.path.realpath(parent)
    except Exception:
        return False
    return (child_real == parent_real
            or child_real.startswith(parent_real + os.sep))


def _pending_records(records: List[Dict[str, Any]], project_dir: str,
                     session_id: str) -> List[Dict[str, Any]]:
    """Unconsumed records whose registered root contains `project_dir`.

    `project_dir` is the session's `cwd`, matched against each record's
    registered root so a session started in a subdirectory of where the
    handover was written still sees it. Records created by `session_id` itself
    are excluded — SessionStart also fires on compact and resume, which keep
    the same session id, and a session must never consume its own handover.
    """
    return [record for record in records
            if _within(project_dir, record.get("project_dir"))
            and record.get("created_session_id") != session_id
            and not record.get("consumed_session_id")
            and record.get("path")
            and os.path.isfile(record["path"])]


def find_pending(project_dir: str, session_id: str) -> Optional[str]:
    """Path of the newest pending handover, or None. Marks nothing.

    Read-only by design. Consuming a handover just because a session *started*
    is wrong when several sessions are open in the same tree: they race for it,
    and the session that wins is not necessarily the one the user meant to
    continue the work in — the handover is then marked consumed in a window
    that ignores it, and swept before the right window ever sees it.
    Consumption is `claim`'s job, and only happens on an explicit attachment.

    Used for the SessionStart *announcement*, where naming one document is the
    point. Deciding what the user actually attached needs `find_all_pending`
    instead — see the note there.
    """
    pending = find_all_pending(project_dir, session_id)
    return pending[0] if pending else None


def find_all_pending(project_dir: str, session_id: str) -> List[str]:
    """Every pending handover this session could claim, newest first.

    Exists because matching an attachment against only the NEWEST pending
    document is wrong whenever more than one is outstanding — which is the
    normal state for someone running several sessions. The user attaches the
    handover for the work they are continuing, not necessarily the most
    recently written one, and checking only the newest made that attachment
    invisible: nothing claimed, nothing ever swept, the index accumulating a
    permanently-pending record per thread.

    Newest first so a caller that wants a single best guess (`find_pending`)
    takes element 0 and callers matching an explicit attachment iterate.
    """
    pending = _pending_records(load_index(), project_dir, session_id)
    pending.sort(key=lambda record: record.get("created_at") or "", reverse=True)
    return [record["path"] for record in pending]


def claim(path: str, session_id: str) -> bool:
    """Mark the handover at `path` — and ONLY that one — consumed by `session_id`.

    Returns True when a pending record was claimed.

    This does not also consume older pending handovers sharing the target's
    `project_dir`, though one could reason that the newer document supersedes
    them. That reasoning holds only if `project_dir` distinguishes lines of work, and
    it does not: every handover is registered with the workspace root, so in
    practice the discriminator is constant and "same root" means "every handover
    on the machine". Claiming one would therefore consume unrelated threads' handovers,
    which the sweep would then delete a session later — silent loss of a document the
    user had never even been shown.

    Superseding is also the wrong trade for this tool. Its stated bargain
    everywhere else is clutter over loss: an unattached handover is never
    deleted, precisely so nothing disappears unread. A pending handover is only
    an offer, and the user decides which to take; a stale offer costs a line at
    SessionStart, while a wrong supersede costs the document.
    """
    with _transaction(LOCK_TIMEOUT) as locked:
        if not locked:
            # Skipped, not lost: an unclaimed handover is re-offered next session.
            return False
        try:
            records = _read_for_update()
        except CorruptIndex:
            return False
        try:
            real = os.path.realpath(path)
        except Exception:
            return False

        target = None
        for record in records:
            if not record.get("path") or record.get("consumed_session_id"):
                continue
            try:
                if os.path.realpath(record["path"]) == real:
                    target = record
                    break
            except Exception:
                continue
        if target is None:
            return False

        target["consumed_at"] = datetime.now().isoformat(timespec="seconds")
        target["consumed_session_id"] = session_id
        save_index(records)
        return True
