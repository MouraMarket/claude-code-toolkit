#!/usr/bin/env python3
"""Throughput protection, and the record that makes a regression measurable.

A CORRECT block happens exactly once per session per checkout: the session
relocates, and every later write lands in its own worktree where nothing
contends. So repeated blocks for one owner are proof the mechanism is not
working -- EnterWorktree declined, relocation failed silently, identity is
misresolved, or something nobody predicted.

The breaker bounds that. After max_blocks the guard stands down for the rest of
the session, so ANY bug in this tool costs a fixed number of blocked calls
rather than an open-ended tax on every build.
"""

import json
import os
from datetime import datetime

import claim_lib
import identity_lib

BREAKER_PATH = os.path.expanduser("~/.claude/session-claims/breaker.json")
LOG_PATH = os.path.expanduser("~/.claude/session-claims/decisions.log")

# The log is diagnostic, never read by the decision path. Capped so it cannot
# grow without bound on a machine running many sessions for months.
LOG_MAX_BYTES = 1_000_000


def _lock_path():
    """Beside the state file, whatever the state file currently is.

    Computed per call rather than at import so a test that redirects
    BREAKER_PATH also redirects the lock, instead of quietly serialising every
    test run against the real one in ~/.claude.
    """
    return BREAKER_PATH + ".lock"


def _load():
    try:
        with open(BREAKER_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save(data):
    try:
        os.makedirs(os.path.dirname(BREAKER_PATH), exist_ok=True)
        with open(BREAKER_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except Exception:
        pass


def _live(data, starts=None):
    """`data` with every entry whose process is gone dropped.

    Breaker state is keyed by PID, and PIDs are recycled. Without this, a
    session that crashed with 3 blocks against it leaves `{"54321": ...}`
    behind forever -- reset() only runs on the clean SessionEnd path, which is
    exactly the path a crash, a force-quit or a VS Code reload skips. Weeks
    later a new claude process lands on pid 54321 and is handed a TRIPPED
    breaker on its first contended write: the guard is off for that whole
    session before it has been blocked even once, silently. This is the same
    liveness treatment claim_lib.prune() gives claims, and for the same reason.

    It also bounds the file, which otherwise only ever grows.

    Entries are records: {"count": n, "proc_start": s}. An entry in the OLD
    bare-int format carries no proc_start, so it cannot be told apart from a
    recycled pid -- it is treated as stale and dropped. That costs at most one
    live session's accumulated count on the upgrade, which is the safe
    direction: the guard comes back ARMED, never pre-disarmed.
    """
    if not data:
        return {}
    if starts is None:
        starts = identity_lib.start_times()
    kept = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            continue
        count = value.get("count")
        if not isinstance(count, int) or isinstance(count, bool):
            continue
        try:
            pid = int(key)
        except (TypeError, ValueError):
            continue
        proc_start = value.get("proc_start")
        if not identity_lib.is_pid_live(pid, proc_start, starts=starts):
            continue
        kept[str(pid)] = {"count": count, "proc_start": proc_start}
    return kept


def record_block(pid, proc_start=None, starts=None):
    """Count one block against `pid`. Returns the new count.

    Locked, because this is a read-modify-write on a file every session in
    contention is also writing: two hooks blocking in the same instant would
    otherwise both read 2 and both write 3, losing a count and leaving the
    breaker one block further from standing down than it should be. Same
    fail-open contract as the claims lock -- if it cannot be taken in
    LOCK_TIMEOUT, this proceeds unlocked rather than stalling a hook.

    `proc_start` is the owner's recorded start time and is what makes the
    entry survivable across pid reuse. Absent, the entry degrades to a bare
    existence check, matching how claim records behave without one.
    """
    with claim_lib.locked(_lock_path()):
        data = _live(_load(), starts)
        key = str(pid)
        entry = data.get(key) or {}
        count = entry.get("count", 0) + 1
        data[key] = {"count": count,
                     "proc_start": proc_start
                     if proc_start is not None else entry.get("proc_start")}
        _save(data)
    return count


def is_tripped(pid, max_blocks, starts=None):
    """Has this owner been blocked enough times that we should stand down?

    max_blocks of 0 disables the breaker entirely -- documented as permitted,
    and documented as removing the only bound on the cost of an identity bug.

    Read-only: it prunes in memory and never writes, so it needs no lock. The
    pruned copy is not persisted here because record_block and reset both do
    that under the lock, and a hook that runs on every write should not be
    rewriting state on a path where nothing changed.
    """
    try:
        max_blocks = int(max_blocks)
    except (TypeError, ValueError):
        return False
    if max_blocks <= 0:
        return False
    entry = _live(_load(), starts).get(str(pid))
    return bool(entry) and entry["count"] >= max_blocks


def reset(pid, starts=None):
    """Forget this owner's blocks. Called on release.

    Also the moment dead sessions' entries get flushed to disk: the file is
    written back pruned whenever pruning or the removal changed anything.
    """
    with claim_lib.locked(_lock_path()):
        raw = _load()
        data = _live(raw, starts)
        data.pop(str(pid), None)
        if data != raw:
            _save(data)


def _trim_log():
    """Drop the oldest lines until the file is under half the cap.

    Byte-based, because the cap is about disk, and a line-count cap says
    nothing about size once entries vary in length -- which they do, since a
    block entry carries a checkout path and a claim entry does not.

    Trimmed to HALF the cap rather than exactly to it: trimming to the ceiling
    would put the file right back at the threshold, so the next call would
    rewrite it again, turning a rare maintenance operation into a per-write
    cost on a path that runs on every file edit. Halving amortises it.

    The target is computed here rather than stored as a module constant so
    that a test which lowers LOG_MAX_BYTES gets a correspondingly lower
    target -- a derived module-level constant would keep the original value
    and silently defeat the test.
    """
    target = LOG_MAX_BYTES // 2
    with open(LOG_PATH, encoding="utf-8") as handle:
        lines = handle.readlines()
    kept = []
    size = 0
    for line in reversed(lines):
        size += len(line.encode("utf-8"))
        if size > target:
            break
        kept.append(line)
    kept.reverse()
    # kept may legitimately be empty when a single line exceeds the target.
    # Emptying the file is the right answer there: the cap is a hard bound,
    # and this log is diagnostic -- losing history costs nothing a decision
    # depends on.
    with open(LOG_PATH, "w", encoding="utf-8") as handle:
        handle.writelines(kept)


def log_decision(decision, **fields):
    """Append one JSON line. Never raises, never read by the decision path.

    Exists so a misfiring guard is measurable rather than merely felt: without
    it, a build that feels slow cannot be attributed to this tool rather than
    to the model, the network, or the work.
    """
    record = {"at": datetime.now().isoformat(timespec="seconds"),
              "decision": decision}
    record.update(fields)
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        if (os.path.exists(LOG_PATH)
                and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES):
            _trim_log()
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
    except Exception:
        pass
