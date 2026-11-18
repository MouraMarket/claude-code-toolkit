"""The memory-index distillation claim: one session at a time, machine-wide.

The claim is a DIRECTORY because `os.mkdir` is the one atomic, exclusive create
that works on macOS and Windows alike. A file made with O_EXCL and written
afterwards has an empty window that a second claimant could misread as free.
The holder's details live in `<claim_dir>/holder.json`. A directory with no
readable holder.json is a claimant caught between `mkdir` and the write: it is
HELD, never free, until its mtime passes STALE_SECONDS.

Residual, accepted: a live holder whose claim crosses 24 hours mid-distillation
can be taken over. That is the design's stale rule.

Stdlib only.
"""

import datetime
import json
import os
import shutil
import tempfile
import uuid

STALE_SECONDS = 24 * 3600
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_ATTEMPTS = 3


def claim_dir():
    override = os.environ.get("CLAUDE_INDEX_DISTILL_CLAIM")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "index-distill",
                        "claim")


def _now(now):
    return now if now is not None else datetime.datetime.now(
        datetime.timezone.utc)


def _parse(stamp):
    return datetime.datetime.strptime(stamp, _STAMP_FORMAT).replace(
        tzinfo=datetime.timezone.utc)


def read_live(now=None):
    """The live holder dict, or None when there is no claim or it is stale."""
    return _live_at(claim_dir(), now)


def _live_at(path, now=None):
    if not os.path.isdir(path):
        return None
    now = _now(now)
    holder = None
    try:
        with open(os.path.join(path, "holder.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        if (isinstance(data, dict) and "session_id" in data
                and isinstance(data.get("claimed_at"), str)):
            _parse(data["claimed_at"])
            holder = {"session_id": data["session_id"],
                      "claimed_at": data["claimed_at"]}
    except Exception:
        holder = None
    if holder is not None:
        age = (now - _parse(holder["claimed_at"])).total_seconds()
        return None if age > STALE_SECONDS else holder
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    stamp = datetime.datetime.fromtimestamp(mtime, datetime.timezone.utc)
    if (now - stamp).total_seconds() > STALE_SECONDS:
        return None
    return {"session_id": None, "claimed_at": stamp.strftime(_STAMP_FORMAT)}


def _write_holder(path, holder):
    fd, tmp = tempfile.mkstemp(prefix=".holder-", dir=path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(holder, fh)
        os.replace(tmp, os.path.join(path, "holder.json"))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _rename_away(path):
    """Rename `path` to a fresh tombstone name; None if it was already gone."""
    tomb = "%s.stale-%d-%s" % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        os.rename(path, tomb)
    except FileNotFoundError:
        return None
    return tomb


def _put_back(tomb, path):
    try:
        os.rename(tomb, path)
    except OSError:
        pass  # claim_dir exists again: leave the tombstone, never delete it


def _take_stale(path, now):
    """Remove `path` only if what the rename caught is still stale.

    Between our stale read and our rename, another claimant may have taken
    the stale claim over and made a fresh one: renaming that away would let
    both of us believe we hold the claim. So the tombstone is re-read, and a
    live claim goes straight back.
    """
    tomb = _rename_away(path)
    if tomb is None:
        return
    if _live_at(tomb, now) is not None:
        _put_back(tomb, path)
        return
    shutil.rmtree(tomb, ignore_errors=True)


def claim(session_id, now=None):
    """(True, holder) when this session holds the claim, else (False, holder)."""
    if not session_id:
        return False, None
    path = claim_dir()
    for _ in range(_ATTEMPTS):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            os.mkdir(path)
        except FileExistsError:
            holder = read_live(now)
            if holder is not None:
                if holder["session_id"] == session_id:
                    return True, holder
                return False, holder
            # Stale: take it over, then retry the create.
            _take_stale(path, now)
            continue
        holder = {"session_id": session_id,
                  "claimed_at": _now(now).strftime(_STAMP_FORMAT)}
        _write_holder(path, holder)
        return True, holder
    return False, read_live(now)


def release(session_id):
    """True iff the claim directory removed was this session's own.

    Read before renaming, so a non-owner never moves a live claim aside (a
    claimant arriving in that gap would take over a claim still held). Then
    re-read the tombstone: a successor may have replaced the claim between
    that read and the rename, and a successor's claim must never be deleted.
    """
    if not session_id:
        return False
    path = claim_dir()
    if not _holder_is(path, session_id):
        return False
    tomb = _rename_away(path)
    if tomb is None:
        return False
    if _holder_is(tomb, session_id):
        shutil.rmtree(tomb, ignore_errors=True)
        return True
    _put_back(tomb, path)
    return False


def _holder_is(path, session_id):
    try:
        with open(os.path.join(path, "holder.json"), encoding="utf-8") as fh:
            holder = json.load(fh)
    except (OSError, ValueError):
        return False
    return isinstance(holder, dict) and holder.get("session_id") == session_id


def local_hhmm(holder):
    """The holder's `claimed_at`, converted to local time, as HH:MM."""
    return _parse(holder["claimed_at"]).astimezone().strftime("%H:%M")
