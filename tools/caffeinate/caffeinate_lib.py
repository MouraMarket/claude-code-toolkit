#!/usr/bin/env python3
"""Hold a macOS sleep assertion while Claude Code is actually working.

The screensaver is a red herring: processes run perfectly well behind it. What
kills a long build is SYSTEM SLEEP, and the stock power settings make the two
look linked — the display sleeps on its idle timer and the system follows a
minute later, so a session left working while you walk away freezes mid-tool-call
about half an hour in and never comes back on its own.

CONCURRENT SESSIONS NEED NO COORDINATION HERE, and that is worth stating because
it looks like a missing feature. macOS power assertions are additive: each
session holds its own, and the machine sleeps only once the LAST one is released
(verified — two `caffeinate` processes, kill one, `PreventUserIdleSystemSleep`
stays at 1). So "every session has been idle for settle_seconds" is what the OS
already computes. A cross-session supervisor would re-implement it and get it
wrong.

## Hooks and what each one does

    UserPromptSubmit, PostToolBatch  ->  session hold re-armed to now + hold_seconds;
                                         new background jobs each get a job hold
    Stop, StopFailure                ->  left alone while the session is alive
    SessionEnd                       ->  every hold for the session released
    claude exits for any other reason -> `-w` drops the session hold, no hook needed
    a background job exits           -> `-w` drops its job hold, no hook needed

`Stop` USED TO pull the deadline in to `settle_seconds`, which meant an idle-but-
open session slept five minutes after Claude stopped talking, which is wrong for a
background task that must re-invoke Claude twenty minutes later. The session hold was first
bound to `claude` with `-w` and given no idle timer beyond a 2h ceiling; it was later narrowed to a
30-minute idle timer, and the "keep running work alive" job moved to the per-job holds below.
An open session with nothing running now sleeps 30 minutes after its last activity.
Assertions are additive, so the machine sleeps when the LAST hold of any kind ends.

`settle` still exists and still matters, for the case where the hold is NOT bound
to a pid. `find_claude_pid` returning None is legitimate (see below), and an
unbound hold has nothing but its deadline, so that one is downgraded exactly as
before. Both shapes are tested; the asymmetry is the point.

## What it is for, and the two holds that serve it (ruled 2026-10-20)

The goal is letting Claude's BACKGROUND work finish — a Codex gate, a test run, a
`run_in_background` job — while nobody is at the keyboard. Two kinds of hold:

  SESSION HOLD  an idle timer, `hold_seconds` (30 min) from the session's last
                activity: a prompt or a tool batch, subagents' included. Re-armed
                at most once per `RENEW_INTERVAL_SECONDS`, so it can end up to
                that much early — 28–30 minutes at the defaults.
  JOB HOLD      one per detected background job, bound to that job's process
                with `-w`, so it ends the moment the job exits, capped at
                `job_hold_seconds` (1 hour) in case the job hangs.

The idle timer is the FALLBACK for work that is running but undetected — a job
the detector cannot see gets 30 minutes past the last activity, not forever.
The maintainer set both numbers. Do not remove either deadline: a hold with neither a
pid nor a deadline is the mistake nobody notices.

A background job is a Bash-tool shell that is a direct child of the session's
`claude` and still running (measured 2026-10-20: `claude -> /bin/zsh -c source
.../shell-snapshots/snapshot-zsh-... && eval '<cmd>'`; there is no persistent
shell, so a live one IS a running command). Hook shells are children of `claude`
too, so the snapshot marker in the command line is what tells them apart. If
Claude Code changes that shape, detection finds nothing and every job falls back
to the idle timer — the releasing direction, never a stuck hold.

Until 2026-10-20 the session hold was a 2h block re-armed only past HALF its life,
so the machine slept 1–2 hours after the last activity, and a background job got
no hold of its own at all.

`-t` and `-w` compose — verified empirically, whichever fires first releases.

## On battery, two things bound the hold instead of one

`require_ac` defaults TRUE: out of the box this holds only on wall power, because
a laptop that will not sleep on battery is a flat laptop. `/caffeinate on` flips
it machine-wide, which is the whole reason the slash command exists.

That makes a second bound necessary, since `-w` alone would happily hold through
a session left open overnight. `battery_floor_percent` (20 by default, `0`
disables) releases the hold below the threshold and declines to re-take it. It is
consulted only on battery — on AC there is nothing to run flat — and it is
checked BEFORE `acquire`'s fast path, because under a long-lived hold the fast
path would otherwise skip it for an hour at a time.

## Two things the OS already does, which this deliberately does not

Closing the lid sleeps the Mac regardless. Clamshell sleep ignores both `-i` and
`-s`; only `pmset disablesleep` overrides it, and nothing here touches that.
And the screensaver is orthogonal to all of it — an assertion holds straight
through one, which is the entire point.

Everything here fails toward RELEASING. A hold that should exist and does not
costs one interrupted build; a hold that should not exist and does keeps a
machine awake indefinitely, which is the mistake nobody notices.
"""

# Design decisions about this file that look like accidental complexity are
# recorded in docs/decisions.md. Read the relevant one before simplifying
# anything here.

import contextlib
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import tempfile
import time

DEFAULT_HOLD_SECONDS = 1800      # 30 min idle timer on the session hold
DEFAULT_JOB_HOLD_SECONDS = 3600  # 1h cap on the hold for one background job
DEFAULT_SETTLE_SECONDS = 300     # how long an UNBOUND hold lasts after a turn
DEFAULT_BATTERY_FLOOR = 20       # release below this on battery; 0 disables
DEFAULT_OVERRIDE_HOURS = 24      # `/caffeinate on` lapses after this; 0 = never
DEFAULT_STATE_DIR = "~/.claude/caffeinate"

# How stale a hold may get before activity re-arms it. The deadline is meant to
# sit `hold_seconds` after the LAST activity, and a `caffeinate -t` cannot be
# extended, only replaced — so this is the precision of that timer: the machine
# sleeps between `hold_seconds - RENEW_INTERVAL_SECONDS` and `hold_seconds`
# after the last activity. It bounds the respawn rate under busy work to one per
# interval, which is what keeps the fast path free on PostToolBatch. Capped at
# half the ceiling in `renew_interval`, so a small `hold_seconds` still renews
# before it expires.
RENEW_INTERVAL_SECONDS = 120

# What a Bash-tool shell looks like, as opposed to a hook's shell — both are
# `/bin/zsh` children of `claude`, and only the command line separates them.
SHELLS = ("zsh", "bash", "sh")
BASH_TOOL_MARKER = "/shell-snapshots/snapshot-"
JOB_RECORD_INFIX = ".job-"

# The battery override's clock lives here, NOT in config.json. The refresh rule
# The maintainer chose on 2026-08-26 — any hold actually taken on battery restarts the
# 24 hours — means the HOOK has to record it, and config.json is shared with
# every other tool in this toolkit and written by a human by hand. A hook that
# edits it, from six concurrent sessions, to record a routine event, is a class
# of risk worth avoiding for a convenience feature. The state directory is this
# tool's own, already written on every hold, and losing it costs one fresh
# window rather than someone's configuration.
OVERRIDE_STAMP = "battery-override.json"

# Don't rewrite the stamp more than this often. `acquire` runs after every tool
# batch, and the clock it maintains has a 24-hour resolution — a write per batch
# would be pure noise on the filesystem to record something already recorded.
STAMP_THROTTLE_SECONDS = 300

# At most three blocking subprocesses run per invocation — one `pmset`, the
# process table, and the command lines of a few candidate job shells — and the
# hook's declared timeout in plugin-mac/hooks/hooks.json must exceed their sum
# with margin. TestTimeoutBudget enforces that relationship against both this
# module and that file, because a comment describing a budget like this one can
# silently go wrong. Reading the whole process table
# in ONE `ps`, rather than a call per generation while walking it, is what keeps
# the bound provable.
PS_TIMEOUT = 2                   # each of the two `ps` calls
PMSET_TIMEOUT = 3
HOOK_TIMEOUT = 10


def _subprocess_timeout(real):
    """Bound for one blocking subprocess, seconds: `real`, unless
    `CAFFEINATE_TEST_SUBPROCESS_TIMEOUT` overrides it in a test sandbox.

    The hook tests drive the real executable as a subprocess against shimmed
    `ps`/`pmset` scripts, and under local-ci's concurrent suites a shim
    overran these bounds: `ps` raised (the hook swallowed it, so no verdict)
    and `pmset` read as battery (a stray override stamp). That is load, not
    the code under test. Gated on `CLAUDE_TOOLKIT_CONFIG` being set, the same
    sandbox signal as repo-freshness's `SYNC_TEST_GIT_TIMEOUT`, AND on that
    path resolving inside the system temp directory: the variable alone is
    also a real runtime override, and a live hook that took a longer bound
    would overrun its 10 s budget and be killed without a verdict. Only a
    finite value above zero counts: anything else falls back to the real
    bound, because `subprocess.run(timeout=inf)` raises. The production
    values above, and TestTimeoutBudget's sum over them, are unchanged.
    """
    if _config_is_a_test_fixture(os.environ.get("CLAUDE_TOOLKIT_CONFIG", "")):
        try:
            value = float(os.environ.get("CAFFEINATE_TEST_SUBPROCESS_TIMEOUT", ""))
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    return real


def _config_is_a_test_fixture(config_path):
    """True only for an absolute config path that resolves inside the system
    temp directory, which is where every test fixture writes its config and
    where no installed toolkit keeps one."""
    if not config_path or not os.path.isabs(config_path):
        return False
    temp_root = os.path.realpath(tempfile.gettempdir())
    return os.path.realpath(config_path).startswith(temp_root + os.sep)


class System:
    """Every OS fact this tool needs, in one place tests can replace."""

    def now(self):
        return int(time.time())

    def process_table(self):
        """{pid: (ppid, command)} for every process, from a single `ps`."""
        result = subprocess.run(
            ["ps", "-Ao", "pid=,ppid=,comm="],
            capture_output=True, text=True, encoding="utf-8",
            timeout=_subprocess_timeout(PS_TIMEOUT))
        return parse_process_table(result.stdout)

    def command_lines(self, pids):
        """{pid: full command line} for a few pids, from a single `ps`.

        A second call rather than `args=` in `process_table`, because `comm`
        paths contain spaces (`Code Helper (Plugin)`) and nothing would separate
        one from its arguments. A pid that has exited is simply absent.
        """
        if not pids:
            return {}
        result = subprocess.run(
            ["ps", "-o", "pid=,args=", "-p", ",".join(str(int(p)) for p in pids)],
            capture_output=True, text=True, encoding="utf-8",
            timeout=_subprocess_timeout(PS_TIMEOUT))
        lines = {}
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                lines[int(parts[0])] = parts[1]
        return lines

    def power_state(self):
        """(on_ac, battery_percent) from ONE `pmset` call.

        Deliberately one call rather than two. The hook's timeout budget is
        asserted against 2 * PS_TIMEOUT + PMSET_TIMEOUT, and a hook killed on its
        timeout returns no verdict — which for this tool means the hold it was
        about to take never happens, silently. Reading the source and the charge
        from the same output keeps that sum where TestTimeoutBudget can prove it.
        """
        try:
            result = subprocess.run(
                ["pmset", "-g", "ps"],
                capture_output=True, text=True, encoding="utf-8",
                timeout=_subprocess_timeout(PMSET_TIMEOUT))
        except Exception:
            # A MISSING OR HUNG `pmset` reads exactly like unreadable output
            # from a working one: battery, unknown charge. Both are the
            # hold-less direction, so the two cannot disagree about safety.
            #
            # This is caught here rather than left to raise because of the CLI.
            # The hook path swallows everything, so raising cost it nothing —
            # but `run_cli("on")` writes the config FIRST and reads the power
            # state afterwards, purely to print a warning. An exception there
            # exits 1 after the write has already happened, telling a human the
            # toggle failed when it succeeded. That is worse than any wrong
            # answer about the battery.
            return False, None
        return parse_power_state(result.stdout)

    def spawn(self, argv):
        """Start a detached process and return its pid.

        start_new_session matters: the hook exits immediately, and without its
        own session the assertion would take the hook's signals and die with it.
        """
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True)
        return process.pid

    def terminate(self, pid):
        os.kill(pid, signal.SIGTERM)


def parse_process_table(text):
    """Parse `ps -Ao pid=,ppid=,comm=` output.

    Split with maxsplit=2 because a command path legitimately contains spaces
    (`.../Code Helper (Plugin)`), and splitting naively would truncate it to
    `.../Code` and break the basename match this all rests on.
    """
    table = {}
    for line in text.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        table[pid] = (ppid, parts[2])
    return table


def parse_power_state(text):
    """Parse `pmset -g ps` into (on_ac, battery_percent).

    Output in its real format, battery:

        Now drawing from 'Battery Power'
         -InternalBattery-0 (id=12345678)\t66%; discharging; 10:49 remaining

    A desktop Mac has no battery line at all, and a percentage can be absent or
    unparseable on a machine this was never run on. `None` is the honest answer
    there, and every caller reads None as "no floor to enforce" rather than as
    zero — a machine that cannot report its charge must not be treated as flat.

    `on_ac` is the safe half to get wrong in only one direction, so it tests for
    the AC string rather than for the absence of the battery one: an unreadable
    `pmset` reads as battery, which is the state that holds LESS.
    """
    on_ac = "AC Power" in text
    percent = None
    match = re.search(r"(\d{1,3})%", text)
    if match:
        value = int(match.group(1))
        if 0 <= value <= 100:
            percent = value
    return on_ac, percent


def is_claude(table, pid):
    """Whether `pid` is still the live `claude` that a hold was bound to.

    Same basename test `find_claude_pid` used to record it, deliberately: if the
    two disagreed, a hold could be bound at acquire and unrecognised at settle,
    and the session would sleep five minutes after every turn while looking
    correct. Pids are reused, so this is also what stops an unrelated process
    inheriting the number and keeping the machine awake indefinitely.
    """
    if not pid:
        return False
    entry = table.get(pid)
    return bool(entry) and os.path.basename(entry[1]) == "claude"


def find_claude_pid(table, start_pid, limit=12):
    """Walk up from this hook toward the `claude` binary that spawned it.

    DUPLICATES the shape of `tools/session-claim/identity_lib.walk_to_claude`,
    deliberately and with the cost known: change how Claude Code presents in the
    process table and BOTH need fixing. Both read `ps comm=` (the executable
    path alone, spaces intact) and match an exact basename `claude`; never move
    either to `command=` and an argv split, which cut the desktop app's
    ".../Application Support/..." path and left session-claim allowing every edit
    (2026-10-21). Kept as two copies rather than shared because sharing
    means putting a hand inside a guard that stops two sessions clobbering each
    other's work. Revisit if a third caller appears. docs/decisions.md has it.

    Verified shape in a VS Code session: hook process -> /bin/zsh -> claude ->
    Code Helper. The walk is capped and refuses to revisit a pid, because `ps`
    reads the table non-atomically and can present a cycle.

    None is a legitimate answer, not an error. It costs the `-w` backstop and
    nothing else: the hold is bounded either way, which is precisely what the
    first version got wrong by treating the two cases as different kinds of hold.

    THIS IS THE ONE HOLD WITH NO KERNEL BACKSTOP, and it is worth knowing before
    you reason about whether an assertion can be stranded. Read `-w` in the code
    and the obvious conclusion is that it cannot — measured 2026-08-27, a
    `caffeinate -w <pid>` releases in ~0s when its target is SIGKILLed, so even
    an abruptly killed session cannot leak one. That guarantee does not cover the
    hold taken HERE, when the walk returned None: it has no `-w`, so its `-t` is
    the only thing that ends it, and nothing sweeps a `caffeinate` whose state
    record was deleted out from under it.

    In practice the window is one turn — the next prompt or tool batch re-walks
    and rebinds — so this is a narrow residual rather than a defect. It is
    written down because the next person to ask "can this strand an assertion?"
    will read `-w`, answer no, and be right about every hold except this one.
    """
    seen = set()
    pid = start_pid
    for _ in range(limit):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        entry = table.get(pid)
        if entry is None:
            return None
        ppid, command = entry
        if os.path.basename(command) == "claude":
            return pid
        pid = ppid
    return None


def job_candidates(table, claude_pid, self_pid):
    """Shells directly under `claude`, minus the one this hook runs in.

    Every hook is `claude -> zsh -> hook`, so our own shell is always a child of
    `claude` and must be excluded by walking up from `self_pid`. Other hooks'
    shells running in parallel are NOT excluded here — only their command lines
    can tell them from a job, which is `is_job_command`'s half.
    """
    own = set()
    pid = self_pid
    for _ in range(12):
        if pid <= 1 or pid in own or pid == claude_pid:
            break
        own.add(pid)
        entry = table.get(pid)
        if entry is None:
            break
        pid = entry[0]
    return sorted(
        pid for pid, (ppid, command) in table.items()
        if ppid == claude_pid and pid not in own
        and os.path.basename(command) in SHELLS)


def is_job_command(command_line):
    """Whether a shell's command line is a Bash-tool command, not a hook."""
    return BASH_TOOL_MARKER in (command_line or "")


def job_pid_of(name, session_prefix):
    """The job pid encoded in a record's filename, or None if it is not one."""
    if not (name.startswith(session_prefix) and name.endswith(".json")):
        return None
    tail = name[len(session_prefix):-len(".json")]
    return int(tail) if tail.isdigit() else None


def is_caffeinate(table, pid):
    """Whether `pid` is really one of our assertions, before we signal it.

    A state file names a pid, and pids are reused. Without this check a stale
    file is a licence to kill whatever inherited that number.
    """
    entry = table.get(pid)
    return bool(entry) and os.path.basename(entry[1]) == "caffeinate"


# --- Configuration -----------------------------------------------------------

def _flag(config, key, default):
    value = config.get("caffeinate", key)
    return value if isinstance(value, bool) else default


def enabled(config):
    """Defaults to FALSE. A hook that stops a machine sleeping is a surprise to
    inherit, so it is opt-in even though the rest of the toolkit defaults on."""
    return _flag(config, "enabled", False)


def require_ac(config):
    return _flag(config, "require_ac", True)


def _seconds(config, key, default):
    """Non-numeric, zero and negative all fall back: a zero deadline would spawn
    an assertion that expires before it is recorded."""
    value = config.get("caffeinate", key)
    if isinstance(value, bool):
        return default
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return default
    return seconds if seconds > 0 else default


def hold_seconds(config):
    return _seconds(config, "hold_seconds", DEFAULT_HOLD_SECONDS)


def job_hold_seconds(config):
    return _seconds(config, "job_hold_seconds", DEFAULT_JOB_HOLD_SECONDS)


def renew_interval(ceiling):
    """How stale a hold of `ceiling` seconds may get before activity re-arms it."""
    return min(RENEW_INTERVAL_SECONDS, ceiling // 2)


def settle_seconds(config):
    return _seconds(config, "settle_seconds", DEFAULT_SETTLE_SECONDS)


def battery_floor_percent(config):
    """The charge below which a hold is dropped on battery. 0 disables it.

    Zero is MEANINGFUL here, unlike in `_seconds` where it falls back — "no
    floor" is a thing someone can legitimately want, and a zero deadline is not.
    So this cannot reuse that reader, and the difference is the whole reason it
    is written out rather than parameterised.

    Out-of-range values fall back to the default rather than clamping. A floor of
    150 is not a request for 100, it is a typo, and honouring it would mean the
    hold releases the instant it is taken — indistinguishable from the feature
    being broken. `bool` is checked first because `True` is an `int` in Python
    and would otherwise read as a 1% floor.
    """
    value = config.get("caffeinate", "battery_floor_percent")
    if isinstance(value, bool):
        return DEFAULT_BATTERY_FLOOR
    try:
        percent = int(value)
    except (TypeError, ValueError):
        return DEFAULT_BATTERY_FLOOR
    return percent if 0 <= percent <= 100 else DEFAULT_BATTERY_FLOOR


def override_hours(config):
    """How long `/caffeinate on` lasts before lapsing. `0` = never lapses.

    Third key where 0 is meaningful rather than a typo, alongside
    `battery_floor_percent` and `hold_seconds`. Negative and non-numeric fall
    back, because those are mistakes.
    """
    value = config.get("caffeinate", "battery_override_hours")
    if isinstance(value, bool):
        return DEFAULT_OVERRIDE_HOURS
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return DEFAULT_OVERRIDE_HOURS
    return hours if hours >= 0 else DEFAULT_OVERRIDE_HOURS


def stamp_path(config):
    return os.path.join(state_dir(config), OVERRIDE_STAMP)


def read_stamp(path):
    """When the battery override was last used, or None.

    None covers every unreadable shape — absent file, bad JSON, a bogus value —
    and the caller treats all of them as `unstamped`, which is DELIBERATELY the
    lenient branch. See `override_state`.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("last_used_at")
    if isinstance(value, bool):
        return None
    try:
        stamped = int(value)
    except (TypeError, ValueError):
        return None
    return stamped if stamped > 0 else None


def write_stamp(path, now):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({"last_used_at": int(now)}, handle)
    os.replace(temporary, path)


def clear_stamp(path):
    try:
        os.remove(path)
    except OSError:
        pass


def override_state(config, now):
    """One of: off | forever | active | lapsed | unstamped. PURE — never writes.

    Kept a pure read with a five-valued answer rather than a bool, because the
    caller does something different for three of them and `acquire` is where the
    writes belong. A predicate that stamps a file as a side effect of being asked
    a question is the shape that makes a guard untestable.

    `unstamped` is the lenient branch and it is a deliberate choice, not an
    oversight. It happens when `require_ac: false` was set by hand, or when the
    state directory was cleared — and in both cases the config says the human
    wants battery holding while nothing on disk says when that started. Refusing
    would make a hand-edited config silently inert, which is the confusing
    failure. Granting starts the clock NOW, so it buys one fresh window.

    THE BOUND IS `acquire`'S, NOT THIS FUNCTION'S, and the distinction is the
    whole reason the first version had a BLOCKING defect. This branch re-grants
    on every call by construction — it has to, since it cannot tell "never
    stamped" from "cannot be stamped". What stops it accumulating is that
    `acquire` refuses to hold when `_refresh_stamp` reports the clock could not
    be written. An earlier docstring here claimed the single window as this
    function's own property; measured, `holds_on_battery` stayed True 365 days
    on when the stamp path was unwritable.
    """
    if require_ac(config):
        return "off"
    hours = override_hours(config)
    if hours <= 0:
        return "forever"
    stamped = read_stamp(stamp_path(config))
    if stamped is None:
        return "unstamped"
    if now - stamped > hours * 3600:
        return "lapsed"
    return "active"


def holds_on_battery(config, now):
    """Whether the battery override is in force right now.

    A separate function rather than a changed `require_ac`, because that one has
    other callers and narrowing a shared function's contract by editing it in
    place is how this repo lost a GATE notice once already.
    """
    return override_state(config, now) in ("forever", "active", "unstamped")


def below_floor(config, on_ac, percent):
    """Whether the battery is under the floor. Never true on AC.

    Returns a plain bool rather than Optional[bool] on purpose: an unknown charge
    (`percent is None`, a desktop or an unreadable `pmset`) is not a third state
    any caller would act on differently — there is no floor to be under. Keeping
    it two-valued is what stops `assertFalse` conflating "above the floor" with
    "could not tell", which this repo has paid for elsewhere.
    """
    if on_ac:
        return False
    floor = battery_floor_percent(config)
    if floor <= 0 or percent is None:
        return False
    return percent <= floor


def state_dir(config):
    return config.get_path(
        "caffeinate", "state_dir",
        default=os.path.expanduser(DEFAULT_STATE_DIR))


def safe_name(session_id):
    """A session id arrives from the payload and becomes a filename."""
    text = str(session_id or "").strip()
    cleaned = "".join(c if (c.isalnum() or c in "-_") else "-" for c in text)
    return cleaned[:64] or "unknown"


def state_path(config, session_id):
    return os.path.join(state_dir(config), safe_name(session_id) + ".json")


def job_prefix(session_id):
    """Filename prefix of a session's job records. `safe_name` maps `.` to `-`,
    so no session's own record can ever start with another's job prefix."""
    return safe_name(session_id) + JOB_RECORD_INFIX


def job_path(config, session_id, job_pid):
    return os.path.join(
        state_dir(config), "%s%d.json" % (job_prefix(session_id), int(job_pid)))


def read_state(path):
    """The recorded hold as {caffeinate_pid, expires_at, claude_pid}, or None.

    A DEADLINE, not a category. The first version recorded a "mode" and set the
    same one on two holds with different lifetimes, so the branch that refuses to
    inherit a stale deadline could not see that it was inheriting one — and the
    machine slept mid-build. A time is not open to that class of mistake: every
    caller compares it against now and gets the same answer.

    `claude_pid` is OPTIONAL and reads as None when absent, unparseable, or <= 1.
    Two shapes produce that and both are real: a hold this version took when the
    process walk failed, and a hold an OLDER version took before the key existed
    — a state file written by the installed toolkit five minutes before an
    upgrade. Absent must therefore mean "unbound", which routes to `settle`'s
    five-minute downgrade: the old behaviour, for a record written by the old
    code. Failing to parse it instead would discard the whole record and orphan a
    live `caffeinate` process nothing would ever signal again.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    record = {}
    for key in ("caffeinate_pid", "expires_at"):
        value = data.get(key)
        if isinstance(value, bool):
            return None
        try:
            record[key] = int(value)
        except (TypeError, ValueError):
            return None
    if record["caffeinate_pid"] <= 1:
        return None

    claude_pid = data.get("claude_pid")
    if isinstance(claude_pid, bool):
        claude_pid = None
    else:
        try:
            claude_pid = int(claude_pid)
        except (TypeError, ValueError):
            claude_pid = None
    record["claude_pid"] = claude_pid if claude_pid and claude_pid > 1 else None
    return record


@contextlib.contextmanager
def records_lock(directory):
    """Exclusive lock on the state directory itself, shared by every writer.

    Exists for `sweep_stale`, which deletes OTHER sessions' records: without it,
    a session renewing its hold between the sweep's read and its unlink has the
    renewed record deleted, orphaning a live assertion that nothing can then
    release. Locking the directory rather than a lock file keeps the listing
    exactly what it was, which several tests and `held_sessions` rely on.
    """
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _write_state_unlocked(path, caffeinate_pid, expires_at, claude_pid=None):
    """The write itself. The caller already holds `records_lock`: flock is not
    re-entrant across descriptors, so taking it again here would deadlock."""
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump({
            "caffeinate_pid": int(caffeinate_pid),
            "expires_at": int(expires_at),
            "claude_pid": int(claude_pid) if claude_pid else None,
        }, handle)
    os.replace(temporary, path)


def write_state(path, caffeinate_pid, expires_at, claude_pid=None):
    directory = os.path.dirname(path)
    # First: `records_lock` opens the directory, so it has to exist.
    os.makedirs(directory, exist_ok=True)
    with records_lock(directory):
        _write_state_unlocked(path, caffeinate_pid, expires_at, claude_pid)


def clear_state(path):
    try:
        os.remove(path)
    except OSError:
        pass


def assertion_argv(on_ac, claude_pid, timeout_seconds):
    """The caffeinate invocation.

    `-i` always; `-s` only on AC, where the man page says it is valid at all.
    No `-d`: the screen should still go dark. Keeping the display awake to
    finish a build is a different and worse request.
    """
    argv = ["caffeinate", "-i"]
    if on_ac:
        argv.append("-s")
    argv += ["-t", str(int(timeout_seconds))]
    if claude_pid:
        argv += ["-w", str(int(claude_pid))]
    return argv


# --- The three modes ---------------------------------------------------------
#
# Each returns a short verdict string. Nothing is printed to stdout by the hook
# itself: `acquire` runs on UserPromptSubmit and PostToolBatch, where anything
# written lands in the model's context. CAFFEINATE_DEBUG=1 sends the verdict to
# stderr instead, which is how a change gets verified through the ~/.local/bin
# symlink without polluting a real session.

def acquire(config, system, session_id, self_pid):
    """Push the deadline out — a turn is starting, or work just happened."""
    if not enabled(config):
        return "disabled"

    now = system.now()
    on_ac, percent = system.power_state()
    if not on_ac and not holds_on_battery(config, now):
        # Unplugging mid-session RELEASES rather than merely declining to renew.
        # "Only while plugged in" has to mean the running hold too, or the answer
        # depends on whether you unplugged before or after the turn started.
        #
        # A LAPSED override lands here too, and releases the same way. An
        # override that stopped applying has to end the holds it was taking, or
        # "it expires after 24 hours" would mean "it expires for holds taken
        # after 24 hours" — and a session already running would keep the machine
        # awake indefinitely, which is the entire thing the expiry exists to
        # stop.
        verdict = "on-battery" if release(config, system, session_id) == "nothing-held" \
            else "released-battery"
        return verdict + "-lapsed" \
            if override_state(config, now) == "lapsed" else verdict

    # BEFORE the fast path, and that ordering is load-bearing. Holds are now
    # bound to the session rather than to a turn, so the fast path can be taken
    # for an hour at a stretch — long enough to run a laptop flat while every
    # invocation returns "already-held" and reads as correct.
    if below_floor(config, on_ac, percent):
        return "on-battery-floor" \
            if release(config, system, session_id) == "nothing-held" \
            else "released-battery-floor"

    # The clock restarts whenever the override actually DOES something — a hold
    # taken on battery — rather than only when `/caffeinate on` is run. The maintainer
    # chose that rule on 2026-08-26: forgetting the setting and not using it are
    # the same event, so refreshing on use targets "forgot" more precisely than
    # a fixed 24 hours from the command would.
    #
    # Stamped BEFORE the fast path, for the same reason the battery floor is:
    # under a session-length hold the fast path is taken for an hour at a time,
    # and a clock that only advanced on the slow path would lapse mid-use.
    if not on_ac and not _refresh_stamp(config, now):
        # The clock could not be written, so the expiry cannot be enforced and
        # the override would hold forever. Decline, and release anything already
        # held — the same shape as a lapse, because the consequence is the same.
        return "on-battery-noclock" \
            if release(config, system, session_id) == "nothing-held" \
            else "released-noclock"

    # ONE process-table read per call, shared by the session hold and the job
    # scan. It is no longer skippable: a background job is launched by a tool
    # batch, and the batch's own PostToolBatch is the only hook guaranteed to
    # see it — a scan only on the slow path would miss most of them. Measured
    # ~29ms on a 700-process table (2026-10-20).
    table = system.process_table()
    verdict = _hold_session(config, system, session_id, self_pid, on_ac, now, table)
    try:
        taken = hold_jobs(config, system, session_id, self_pid, on_ac, now, table)
    except Exception:
        taken = 0
    return "%s+%d-jobs" % (verdict, taken) if taken else verdict


def _hold_session(config, system, session_id, self_pid, on_ac, now, table):
    """The session hold: an idle timer re-armed by activity."""
    path = state_path(config, session_id)
    state = read_state(path)
    ceiling = hold_seconds(config)
    fresh_above = ceiling - renew_interval(ceiling)

    # Fast path: a hold re-armed within the last `renew_interval` needs nothing,
    # and this is the common case on PostToolBatch. It trusts the record without
    # checking its pid — at worst a dead-and-reused pid leaves us believing we
    # hold an assertion we do not, for one interval, and the machine sleeps the
    # way it always did. It stays OUTSIDE the lock: it must cost nothing more.
    if state and state["expires_at"] - now > fresh_above:
        return "already-held"

    # Spawn and record are one transaction. Without the lock, a second `acquire`
    # (or a `release`) landing between the spawn and the write sees no record,
    # spawns again or reports "nothing-held", and the first assertion is never
    # recorded — so nothing can ever release it. The directory is created BEFORE
    # locking, which is what lets `settle` and `release` read "no directory" as
    # "no `acquire` is in flight".
    os.makedirs(state_dir(config), exist_ok=True)
    with records_lock(state_dir(config)):
        # Re-read: a peer may have recorded or renewed while we waited.
        state = read_state(path)
        if state and state["expires_at"] - now > fresh_above:
            return "already-held"

        claude_pid = find_claude_pid(table, self_pid)
        argv = assertion_argv(on_ac, claude_pid, ceiling)
        _write_state_unlocked(path, system.spawn(argv), now + ceiling, claude_pid)
        # The old assertion goes only AFTER its replacement exists. Renewal runs
        # every few minutes under work, often with nobody at the keyboard, and a
        # drop-then-spawn leaves a moment with no assertion at all — on a machine
        # whose idle timer already ran out, that moment is enough to sleep it.
        _drop(system, table, state)

    # AFTER the hold is recorded, so a sweep that fails for any reason cannot
    # cost this session its hold. Only on the slow path, so it runs once per
    # renewal rather than once per tool batch. Outside the `with` because the
    # sweep takes the same lock, and flock is not re-entrant across descriptors.
    try:
        sweep_stale(config, table, now)
    except Exception:
        pass
    return "held"


def hold_jobs(config, system, session_id, self_pid, on_ac, now, table):
    """Give each newly seen background job its own hold. Returns how many.

    Each hold is `caffeinate -w <job> -t job_hold_seconds`: the kernel ends it
    when the job exits, and the cap ends it if the job hangs. Nothing here ever
    has to notice a job finishing.

    A job that already has a record is never re-held once its hold has run
    out. That is what makes the cap a cap: re-holding on expiry would turn one
    hour into "as long as the job runs". The record outlives its assertion for
    exactly that reason, and `sweep_stale` clears it only once the job itself
    is gone. The one exception is a reused PID inside the cap
    (`_job_pid_reused`): a new job that took a finished job's number gets its
    own hold.
    """
    claude_pid = find_claude_pid(table, self_pid)
    if not claude_pid:
        return 0
    candidates = job_candidates(table, claude_pid, self_pid)
    if not candidates:
        return 0
    commands = system.command_lines(candidates)
    jobs = [pid for pid in candidates if is_job_command(commands.get(pid))]
    if not jobs:
        return 0

    directory = state_dir(config)
    os.makedirs(directory, exist_ok=True)
    cap = job_hold_seconds(config)
    taken = 0
    with records_lock(directory):
        for job in jobs:
            path = job_path(config, session_id, job)
            state = read_state(path)
            if state and not _job_pid_reused(state, table, now):
                continue
            argv = assertion_argv(on_ac, job, cap)
            _write_state_unlocked(path, system.spawn(argv), now + cap, None)
            taken += 1
    return taken


def _job_pid_reused(state, table, now):
    """True when a job record's PID now belongs to a different, newer job.

    `caffeinate -w <job> -t <cap>` ends early only when its job exits. So
    before the cap, a dead assertion means the recorded job is gone, and a
    live process on that PID is a new job that reused the number: it gets its
    own hold. After the cap, a dead assertion is the cap doing its job, and
    re-holding would turn the cap into "as long as the job runs".
    """
    return now < state.get("expires_at", 0) and not is_caffeinate(
        table, state.get("caffeinate_pid"))


def _has_job_records(directory, session_id):
    prefix = job_prefix(session_id)
    try:
        return any(job_pid_of(name, prefix) is not None for name in os.listdir(directory))
    except OSError:
        return False


def _drop_jobs_unlocked(config, system, table, session_id):
    """End every job hold this session took. Caller holds `records_lock`."""
    directory = state_dir(config)
    prefix = job_prefix(session_id)
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    dropped = 0
    for name in names:
        if job_pid_of(name, prefix) is None:
            continue
        path = os.path.join(directory, name)
        if _drop(system, table, read_state(path)):
            dropped += 1
        clear_state(path)
    return dropped


def settle(config, system, session_id, self_pid):
    """The turn ended. Release if we must; otherwise leave a live session alone.

    Runs even when the feature is disabled: turning it off mid-session must
    still release whatever an earlier prompt took. Same for unplugging and for
    crossing the battery floor — all three have to be able to end a hold that a
    live session would otherwise keep, which is why they are checked BEFORE the
    `session-alive` branch rather than after it.
    """
    path = state_path(config, session_id)
    directory = state_dir(config)
    if not os.path.isdir(directory):
        # The one lock-free exit, and a sound one: an `acquire` in flight makes
        # the directory BEFORE it locks, so no directory means none is running.
        # An unlocked "no record" is NOT sound — between an `acquire`'s spawn and
        # its write there is no record yet, and answering "nothing-held" then is
        # how a hold goes untracked.
        return "nothing-held"
    # No subprocess happens here: the lock is a flock, so the "nothing held"
    # case stays as cheap as it was.
    with records_lock(directory):
        state = read_state(path)
        has_jobs = _has_job_records(directory, session_id)
        if not state and not has_jobs:
            return "nothing-held"
        table = system.process_table()
        if state and not is_caffeinate(table, state["caffeinate_pid"]):
            clear_state(path)
            state = None
        if not state and not has_jobs:
            return "stale"

        # The three release conditions end the session's JOB holds too: each
        # one is a reason the machine should be allowed to sleep at all, and a
        # job hold left behind would keep it up for another hour regardless.
        #
        # They run when only JOB holds remain, too — the session hold expired
        # after 30 quiet minutes while a job carried on. That is THE case this
        # tool serves, and an early "stale" return here let a job hold ride
        # through the battery floor (caught by Codex review, 2026-10-20).
        if not enabled(config):
            _drop(system, table, state)
            clear_state(path)
            _drop_jobs_unlocked(config, system, table, session_id)
            return "released-disabled"

        on_ac, percent = system.power_state()
        if not on_ac and not holds_on_battery(config, system.now()):
            # Covers both "never held on battery" and "the override has lapsed".
            # Checked ahead of the session-alive branch, like the other two release
            # conditions, because a lapsed override must be able to end a hold that
            # a live session would otherwise keep indefinitely.
            _drop(system, table, state)
            clear_state(path)
            _drop_jobs_unlocked(config, system, table, session_id)
            return "released-battery"
        if below_floor(config, on_ac, percent):
            _drop(system, table, state)
            clear_state(path)
            _drop_jobs_unlocked(config, system, table, session_id)
            return "released-battery-floor"

        if not state:
            return "stale"      # only job holds left, and nothing ends them here

        # The change of 2026-08-26. A hold bound to a living `claude` is released by
        # that process exiting — `-w` does it in the kernel, with no hook involved —
        # so there is nothing for `settle` to do and pulling the deadline in would
        # put the machine to sleep five minutes into a background job that is still
        # running. Leaving the record untouched also means no spawn and no write on
        # the hook that fires at the end of every turn.
        #
        # ONLY the recorded pid is consulted, and NOT a fresh `find_claude_pid` walk
        # from this hook. That looks like the more robust check and it is in fact
        # inert: this hook is itself spawned by `claude`, so the walk succeeds
        # essentially always, `session-alive` would be unconditional, and the unbound
        # branch below — the entire reason `settle` still exists — would become
        # unreachable while every test of it still passed.
        if is_claude(table, state["claude_pid"]):
            return "session-alive"

        now = system.now()
        deadline = now + settle_seconds(config)
        if state["expires_at"] <= deadline:
            # Already expiring sooner than we would set it. Replacing it here would
            # EXTEND the hold, which is the opposite of what settling means.
            return "already-settling"

        _drop(system, table, state)
        # No `-w`, and NOT because there is necessarily no live `claude` to bind to.
        # There may well be one: an old-format record written before this version
        # existed reaches here inside a perfectly healthy session. The reason is that
        # re-walking to find one would make this branch self-erasing — the walk
        # succeeds essentially always from inside a hook, so every unbound hold would
        # become bound at settle, the next settle would answer `session-alive`, and
        # the short-deadline path this whole function exists for would stop being
        # reachable while its tests carried on passing.
        #
        # Binding is `acquire`'s job. An unbound record self-heals on the next prompt
        # or tool batch, which is at most one turn away.
        argv = assertion_argv(on_ac, None, settle_seconds(config))
        _write_state_unlocked(path, system.spawn(argv), deadline, None)
        return "settling"


def release(config, system, session_id, self_pid=None):
    """Drop the hold outright. SessionEnd, and the unplugged path above."""
    path = state_path(config, session_id)
    directory = state_dir(config)
    if not os.path.isdir(directory):
        return "nothing-held"      # see `settle`: no directory, no `acquire` in flight
    with records_lock(directory):
        state = read_state(path)
        has_jobs = _has_job_records(directory, session_id)
        if not state and not has_jobs:
            return "nothing-held"
        table = system.process_table()
        dropped = _drop(system, table, state)
        clear_state(path)
        # Job holds go with the session: this is SessionEnd, unplugging, the
        # floor, or the override lapsing — every one of them means "let it sleep".
        dropped = _drop_jobs_unlocked(config, system, table, session_id) or dropped
    return "released" if dropped else "stale"


# --- The slash command's half: reading and writing the machine's config -------
#
# `/caffeinate on|off` is a MACHINE-WIDE, persistent change to
# ~/.config/claude-code-toolkit/config.json, which is the user's explicit choice
# over a session-scoped override (2026-08-26). Two consequences follow, and both
# are handled here rather than left to the caller:
#
#   - A forgotten `on` outlives the session that set it, which is exactly what
#     `battery_floor_percent` is the backstop for.
#   - The file is hand-edited and full of `//` comment keys. Every one of them is
#     an ordinary JSON key, so a load/dump round-trip preserves them and their
#     order — but only if nothing here rebuilds the document from scratch.

def read_config_file(path):
    """The raw config document, or None if it cannot be read as an object.

    None rather than {} deliberately: writing into a file we failed to parse
    would replace someone's hand-written config — comments, other tools' keys
    and all — with a two-line document. The caller refuses instead.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_config_file(path, data):
    """Atomic, 2-space indented, trailing newline — matching the installer's."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def apply_battery_override(path, turn_on):
    """Flip the battery override in the config file. Returns (ok, changes).

    `on` sets BOTH `require_ac: false` and `enabled: true`. Setting only the
    former is the tempting minimal change and it is the silent-failure shape:
    `enabled` defaults FALSE, so on any machine that has not opted in, the
    command would report success and hold nothing, forever, with the config
    looking correct.

    `off` touches ONLY `require_ac`. It restores AC-only holding rather than
    disabling the feature — the maintainer's correction of 2026-08-26, and the reason
    `off` is not simply the inverse of `on`. Someone who wants it off entirely
    sets `enabled: false` by hand, which is a bigger decision than this command
    should make on their behalf.
    """
    data = read_config_file(path)
    if data is None:
        return False, []
    section = data.get("caffeinate")
    if not isinstance(section, dict):
        section = {}

    changes = []

    def put(key, value):
        if section.get(key) != value:
            changes.append((key, section.get(key), value))
        section[key] = value

    put("require_ac", not turn_on)
    if turn_on:
        put("enabled", True)

    data["caffeinate"] = section
    write_config_file(path, data)
    return True, changes


def held_sessions(config, system):
    """Every live hold this machine is carrying, newest deadline last.

    Reads the state directory rather than `pmset -g assertions`, because the
    question `/caffeinate status` is answering is "what has THIS tool taken",
    and the assertion list cannot tell our `caffeinate` processes from the one
    someone started by hand in another terminal. Records whose process has gone
    are reported as stale rather than hidden — a stale record is how a hold
    silently stops being real, so it is the thing most worth seeing.
    """
    directory = state_dir(config)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []

    table = system.process_table()
    now = system.now()
    rows = []
    for name in names:
        if not name.endswith(".json"):
            continue
        state = read_state(os.path.join(directory, name))
        if not state:
            continue
        stem = name[:-len(".json")]
        session, _, job = stem.partition(JOB_RECORD_INFIX)
        rows.append({
            "session": session,
            "job_pid": int(job) if job.isdigit() else None,
            "live": is_caffeinate(table, state["caffeinate_pid"]),
            "bound": is_claude(table, state["claude_pid"]),
            "claude_pid": state["claude_pid"],
            "expires_in": state["expires_at"] - now,
        })
    return sorted(rows, key=lambda row: row["expires_in"])


def sweep_stale(config, table, now):
    """Delete other sessions' records that can no longer describe a hold.

    A record is otherwise cleared only by ITS OWN session — `settle`, `release`
    or the next `acquire` — so a session that exits without firing `SessionEnd`
    leaves its file behind forever. A burst of short-lived sessions leaves many
    such records at once, each bound to a different `claude` pid.

    Both conditions are required, and each covers a case the other cannot:

      - the deadline has PASSED. `-t` ended the assertion by then, so the record
        describes nothing even if its pid has since been reused. A record whose
        deadline is still ahead is left alone, however dead its process looks —
        it is at most `hold_seconds` from qualifying.
      - its pid is NOT a live `caffeinate`. Never delete the only record of a
        running assertion: that orphans it where `status` cannot see it and no
        hook will ever signal it.

    Only files `read_state` parses are candidates, which is what keeps the
    battery-override stamp — same directory, same extension — out of reach. The
    caller's own record needs no exemption: it was just written with a deadline
    `hold_seconds` ahead.
    """
    directory = state_dir(config)
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    removed = 0
    # Read, check and unlink under the same lock `write_state` takes, so a
    # record cannot be renewed between the check and the delete (see
    # `records_lock`).
    with records_lock(directory):
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(directory, name)
            state = read_state(path)
            if not state or state["expires_at"] >= now:
                continue
            if is_caffeinate(table, state["caffeinate_pid"]):
                continue
            # A capped job's record outlives its hold on purpose — it is what
            # stops `hold_jobs` re-holding the same job (see there). Keep it
            # while the job's shell is still running.
            job = job_pid_of(name, name.split(JOB_RECORD_INFIX)[0] + JOB_RECORD_INFIX) \
                if JOB_RECORD_INFIX in name else None
            if job is not None and table.get(job) \
                    and os.path.basename(table[job][1]) in SHELLS:
                continue
            clear_state(path)
            removed += 1
    return removed


def _refresh_stamp(config, now):
    """Restart the override clock. Returns whether the clock is TRUSTWORTHY.

    Reached only on battery, and only after `holds_on_battery` said yes — so a
    LAPSED override is never refreshed by the very hook that should be letting
    it die. Getting that ordering wrong would make the expiry unreachable while
    every test of the predicate still passed.

    False means the clock could not be recorded, and the caller must then
    DECLINE TO HOLD. That looks harsh for a convenience feature and it is the
    only safe answer: `override_state` reads an unwritable stamp as `unstamped`,
    which is the lenient branch, so a stamp that can never be written re-grants a
    fresh window on every single call — measured, `holds_on_battery` was still
    True 365 days on. An expiry that cannot be recorded is not an expiry, and
    silently holding forever is the exact failure this whole mechanism exists to
    prevent. Swallowing the error here was the first version's BLOCKING defect.

    True when there is nothing to record: no expiry configured, or a stamp
    written recently enough that the throttle skips the write.
    """
    if override_hours(config) <= 0:
        return True                 # nothing to expire, so nothing to record
    path = stamp_path(config)
    stamped = read_stamp(path)
    if stamped is not None and now - stamped < STAMP_THROTTLE_SECONDS:
        return True
    try:
        write_stamp(path, now)
    except OSError:
        return False
    return True


def _drop(system, table, state):
    if not state:
        return False
    pid = state["caffeinate_pid"]
    if not is_caffeinate(table, pid):
        return False
    try:
        system.terminate(pid)
    except Exception:
        return False
    return True
