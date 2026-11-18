#!/usr/bin/env python3
"""Who owns a tool call: the claude process it ultimately came from.

Identity is the claude PROCESS, never the session id. A subagent's tool calls
may carry a session id distinct from its parent's; keying on that would make
every Sonnet executor dispatched into the parent's checkout look like a second
session and get relocated, breaking /build on its first run. Subagents run
inside the parent's process, so the PID is the same and the question does not
arise.

Every function here returns None or False rather than raising. An unresolved
owner means the caller ALLOWS -- never block on identity we could not establish.
"""

# Design decisions about this file that look like accidental complexity are
# recorded in docs/decisions.md. Read the relevant one before simplifying
# anything here.

import json
import math
import os
import subprocess

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")

# Depth bound on the ancestry walk. Generous for hook -> shell -> claude, and
# it is what stops a malformed or cyclic process table from hanging a hook that
# runs on every tool call.
MAX_HOPS = 12

# Every `ps` call in this module is bounded by this. It is a named constant
# because it is charged against the calling hook's own timeout, and the budget
# test in tests/test_claim_lib.py reads it rather than duplicating a literal --
# the previous accounting left these terms out entirely and understated the
# worst case by two thirds. One second is generous for `ps`, which is local and
# has no network or lock to wait on; a `ps` that has not answered in a second
# is not going to, and returning "unknown" makes the caller ALLOW, which is the
# right answer anyway.
PS_TIMEOUT = 1


def _ps_timeout():
    """The timeout for a `ps` call: PS_TIMEOUT unless a sandbox raises it.

    Test seam, inert outside a sandbox for the same reason as
    CLAIM_TEST_OWNER_PID: it only takes effect when CLAIM_CLAIMS_PATH is also
    set. A loaded machine can push `ps` past one second and flip a hook test
    to the allow-uncertain branch. Production budgets (TestTimeoutBudget) use
    the PS_TIMEOUT constant, which keeps the value 1.
    """
    if not os.environ.get("CLAIM_CLAIMS_PATH"):
        return PS_TIMEOUT
    try:
        value = float(os.environ["CLAIM_TEST_PS_TIMEOUT"])
    except (KeyError, ValueError):
        return PS_TIMEOUT
    # Finite only: subprocess.run(timeout=inf) raises OverflowError, which the
    # callers swallow as "table unavailable", i.e. the guard allows.
    return value if math.isfinite(value) and value > 0 else PS_TIMEOUT


def process_table():
    """{pid: (ppid, executable)} for every process. {} on any failure.

    One `ps` invocation rather than one per hop: the walk is 2-3 deep in
    practice, and several forks add latency to a hook that fires on every
    write. One fork also stops the cost scaling with process depth.

    `comm=`, not `command=`: comm is the executable path ALONE, so a path with
    a space in it survives whole. The desktop app's Claude Code lives under
    `~/Library/Application Support/Claude/claude-code/...`; reading `command=`
    and splitting argv to find argv[0] cut that at "Application", so no desktop
    session ever resolved and the guard allowed every edit (2026-10-21).
    """
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,ppid=,comm="],
                             capture_output=True, text=True, encoding="utf-8",
                             timeout=_ps_timeout())
    except Exception:
        return {}
    return parse_process_table(out.stdout)


def parse_process_table(text):
    """Parse `ps -Ao pid=,ppid=,comm=` output into {pid: (ppid, executable)}.

    maxsplit=2 because the executable path legitimately contains spaces, and
    everything after the ppid is that one path.
    """
    table = {}
    for line in text.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            table[int(parts[0])] = (int(parts[1]), parts[2].strip())
        except ValueError:
            continue
    return table


def _is_claude_executable(executable):
    """True when the executable's basename is exactly `claude`.

    The whole path's basename, never a split of it: `.../Application
    Support/.../MacOS/claude` is claude. An exact basename also keeps `vim`
    (editing claude-notes.md) and a `claude-notes` binary from resolving to a
    claude owner. Same test as `caffeinate_lib.is_claude`.
    """
    if not executable or not executable.strip():
        return False
    return os.path.basename(executable.strip()) == "claude"


def walk_to_claude(start_pid, table):
    """The nearest ancestor of start_pid that is a claude process, or None.

    `tools/caffeinate/caffeinate_lib.find_claude_pid` is the same walk, with
    the same reader (`ps comm=`) and the same basename test. Deliberately
    not shared (see the note there), but if you change what counts as a claude
    process, change both.
    """
    pid = start_pid
    seen = set()
    for _ in range(MAX_HOPS):
        if pid is None or pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        entry = table.get(pid)
        if entry is None:
            return None
        ppid, executable = entry
        if _is_claude_executable(executable):
            return pid
        pid = ppid
    return None


def proc_start(pid):
    """`ps -o lstart=` for pid, or None.

    Same format Claude Code's own registry stores as procStart
    ("Wed Aug 12 02:58:26 2026"), so the two are directly comparable.

    One fork per call. When you need this for MORE THAN ONE pid, use
    start_times() instead -- see the note there.
    """
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)],
                             capture_output=True, text=True, encoding="utf-8",
                             timeout=_ps_timeout())
    except Exception:
        return None
    if out.returncode != 0:
        # A failed ps is "unknown", never a start time: is_pid_live reads None
        # as "could not inspect" and falls back to existence.
        return None
    value = out.stdout.strip()
    return value or None


def start_times():
    """{pid: start-time string} for every process. {} on any failure.

    {} is the "table unavailable" signal is_pid_live() relies on. A working
    table is never empty, because `ps` always lists at least itself, so an
    empty result can only mean the inspection failed. That includes a `ps`
    that exited non-zero, even if it printed lines first: a failed run's
    output is not trusted.

    The same values proc_start() returns -- byte-identical format, since both
    come from `ps` `lstart` -- but for every process in ONE fork.

    This exists because liveness is checked once per claim record, and a fork
    per record adds up with the record count, on a code path that
    runs on every file write and holds a cross-process lock while it does.
    That cost is also unbounded, since nothing caps how many records the file
    can hold. One fork for the whole table removes both problems.

    Deliberately separate from process_table(): that table answers "who is my
    parent", this one answers "is this the same process I saw before", and the
    two are wanted at different moments. Merging them would mean parsing a
    five-token date out of the middle of every `ps` line -- brittle for no gain.
    """
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,lstart="],
                             capture_output=True, text=True, encoding="utf-8",
                             timeout=_ps_timeout())
    except Exception:
        return {}
    if out.returncode != 0:
        return {}
    table = {}
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        try:
            table[int(parts[0])] = parts[1].strip()
        except ValueError:
            continue
    return table


def registry_entry(pid):
    """Claude Code's own ~/.claude/sessions/<pid>.json, or None.

    Undocumented internal state, so it is used only as a positive cross-check
    and for a friendly name -- never as the sole basis for a decision.
    """
    try:
        with open(os.path.join(SESSIONS_DIR, "%d.json" % pid), encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def is_pid_live(pid, expected_proc_start, starts=None):
    """Is this exact process still running?

    `expected_proc_start` None means "no recorded start" and degrades to a bare
    existence check. When it IS recorded, it must match: without it a recycled
    PID would make a dead session's claim look alive indefinitely.

    `starts` is an optional start_times() map. Pass it when checking more than
    one pid and the whole loop costs one fork instead of one per pid; omit it
    and this forks for itself, so every existing caller keeps working.

    A failed inspection must not be a "dead" verdict, because callers PERSIST
    it: claude-session-release rewrites claims.json without every other live
    claim, and the breaker drops live sessions' counts. So only a POSITIVE
    answer may clear a record -- the same rule as git-tidy's process_state
    (R2-01, tools/git-tidy/worktree_lib.py). When the start time cannot be
    read (an empty `starts` table, or proc_start() returning None) this falls
    back to the existence check above, the same degradation a recorded
    proc_start of None already gets. A NON-empty table that lacks the pid is a
    positive answer: the process started after the snapshot, so the pid is
    recycled.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError, PermissionError):
        # PermissionError means the pid exists but is not ours. Not our session.
        return False
    if expected_proc_start is None:
        return True
    if starts is not None:
        if not starts:
            return True
        return starts.get(pid) == expected_proc_start
    actual = proc_start(pid)
    if actual is None:
        return True
    return actual == expected_proc_start


def resolve_owner(start_pid, table=None):
    """{"pid","session_id","name"} for the owning claude, or None.

    The owner's start time is NOT resolved here. The claim hook takes it from
    the same start_times() table it uses for every liveness check, so the start
    has one source and costs no extra `ps`.

    None means identity was NOT established, and every caller must treat that
    as allow. Two independent things must agree: the ancestry walk finds a
    claude process, AND the registry knows that pid. A walk result the registry
    has never heard of is not trusted -- that is the cross-check that keeps a
    process-model change from silently producing wrong owners.
    """
    if table is None:
        table = process_table()
    if not table:
        return None
    pid = walk_to_claude(start_pid, table)
    if pid is None:
        return None
    entry = registry_entry(pid)
    if entry is None:
        return None
    session_id = entry.get("sessionId") or ""
    name = entry.get("name") or (session_id[:8] if session_id else str(pid))
    return {
        "pid": pid,
        "session_id": session_id,
        "name": name,
    }


def resolve_hook_owner():
    """The owning claude process for the CALLING hook, or None.

    None means identity was NOT established, and every caller must treat that
    as allow. Both hooks call this and nothing else -- it lived as a private
    copy in each of them, and a hardened security seam that has to be changed
    in two places at once is the same shape as the duplicated-tool mistake this
    repo has a hard rule against.

    CLAIM_TEST_OWNER_PID is a test seam, and the reason one is needed: identity
    here is derived from the PROCESS TREE, so the blocking and release paths can
    otherwise only be exercised from inside a real Claude session -- which would
    make the most important tests in this suite non-hermetic, passing on one
    machine and skipping everywhere else.

    It is deliberately INERT unless CLAIM_CLAIMS_PATH is also set. An
    unconditional identity override in a guard is a backdoor; one that only
    works once the claims file has already been redirected away from the real
    one can only ever affect a sandbox. Set on its own, this variable does
    nothing at all.
    """
    override = os.environ.get("CLAIM_TEST_OWNER_PID")
    if override and os.environ.get("CLAIM_CLAIMS_PATH"):
        try:
            return {"pid": int(override),
                    "session_id": "test-owner", "name": "test-owner"}
        except ValueError:
            return None
    return resolve_owner(os.getpid())
