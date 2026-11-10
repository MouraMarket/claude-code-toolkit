#!/usr/bin/env python3
"""Thread handover watch — measurement, staging, boundary detection, session state.

Read by the claude-thread-watch UserPromptSubmit hook. Every function here is
best-effort: it returns a safe empty/zero value rather than raising, because a
broken watcher must never block a prompt.
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

# Machine-local settings live outside the repo. See lib/toolkit_config.py.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "lib"))
import toolkit_config as _config

DEFAULT_WINDOW = 200_000

# First matching marker wins, so "haiku" must precede the 1M entries.
WINDOW_MARKERS = (
    ("haiku", 200_000),
    ("[1m]", 1_000_000),
    ("opus-5", 1_000_000),
    ("opus-4-8", 1_000_000),
    ("opus-4-7", 1_000_000),
    ("opus-4-6", 1_000_000),
    ("sonnet-5", 1_000_000),
    ("sonnet-4-6", 1_000_000),
    ("fable-5", 1_000_000),
)


def window_for(model: Optional[str]) -> int:
    """Context window size in tokens for a model id.

    Unknown models fall back to 200k, which arms early rather than never —
    the fail-safe direction for a tool whose job is to warn.
    """
    if not model:
        return DEFAULT_WINDOW
    lowered = model.lower()
    for marker, size in WINDOW_MARKERS:
        if marker in lowered:
            return size
    return DEFAULT_WINDOW


def iter_entries(path: str, include_sidechains: bool = False) -> Iterator[Dict[str, Any]]:
    """Yield JSONL entries from a transcript. Unreadable file or line -> skipped.

    Sidechain filtering is load-bearing for the watcher and defaults ON:
    subagent turns land in the same transcript with isSidechain=true, and
    counting them makes the context reading jump around during /build.

    Pass include_sidechains=True to get every entry — that is what
    claude-build-cost does, because separating main-loop spend from subagent
    spend is the entire measurement.
    """
    try:
        handle = open(path, errors="ignore", encoding="utf-8")
    except OSError:
        return
    with handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if not isinstance(entry, dict):
                continue
            # Sidechain entries are subagent turns. The handover watcher must
            # skip them — a subagent's context is its own, not this thread's, and
            # counting it would arm the watcher on someone else's tokens.
            # claude-build-cost needs the opposite: the subagent spend IS the
            # measurement. Hence the parameter rather than a second reader —
            # one parse loop, two callers, default unchanged.
            if entry.get("isSidechain") and not include_sidechains:
                continue
            yield entry


def read_context(path: str) -> Tuple[int, Optional[str]]:
    """True context occupancy and model id from the last usage-bearing message."""
    context = 0
    model = None
    for entry in iter_entries(path):
        message = entry.get("message")
        # A truthy non-dict message (a bare string) would make `or {}` pass the
        # string through and .get() raise. Skip it instead — this function
        # promises never to raise.
        if not isinstance(message, dict):
            continue
        usage = message.get("usage")
        if isinstance(usage, dict):
            context = ((usage.get("input_tokens") or 0)
                       + (usage.get("cache_creation_input_tokens") or 0)
                       + (usage.get("cache_read_input_tokens") or 0))
            if message.get("model"):
                model = message["model"]
    return context, model


# --- staging -----------------------------------------------------------------
#
# 250k arms on long sessions only (most sessions peak far below it); 700k sits
# well under Claude Code's auto-compact line on a 1M window, leaving room to
# write the handover before compaction fires. thresholds() floors both by a
# fraction of the window, so a 200k model arms at 70k.

ARM_CAP = _config.get_int("thread_handover", "arm_cap", default=250_000)
URGENT_CAP = _config.get_int("thread_handover", "urgent_cap", default=700_000)
SNOOZE_FRACTION = 0.10

QUIET = "quiet"
ARMED = "armed"
URGENT = "urgent"


def thresholds(window: int) -> Tuple[int, int]:
    """(arm_at, urgent_at) in tokens — absolute caps, floored by window fraction."""
    return (min(ARM_CAP, int(window * 0.35)),
            min(URGENT_CAP, int(window * 0.70)))


def stage_for(context: int, window: int,
              snoozed_at: Optional[int] = None) -> str:
    """Which state this context reading puts the session in.

    snoozed_at is the context reading at which the user declined a proposal;
    the armed state stays suppressed until context grows another
    SNOOZE_FRACTION of the window past it. Urgent ignores snooze.
    """
    arm_at, urgent_at = thresholds(window)
    if context >= urgent_at:
        return URGENT
    if context < arm_at:
        return QUIET
    if snoozed_at is not None and context < snoozed_at + int(window * SNOOZE_FRACTION):
        return QUIET
    return ARMED


# --- boundary detection ------------------------------------------------------

IDLE_GAP_SECONDS = 30 * 60
MERGE_COMMANDS = ("gh pr merge", "git tidy")
MERGE_RESULT_MARKERS = ("Squashed and merged", "Merged pull request")
HANDOFF_SUBAGENTS = frozenset(("executor", "task-reviewer"))
# A plugin agent is recorded as `<plugin>:<name>`. Only the toolkit's own
# build plugin counts: another plugin's `executor` may say DONE about anything.
PLUGIN_PREFIXES = ("toolkit-build:",)


def _bare_agent_type(name):
    """The agent name without the toolkit's plugin prefix, if it carries one."""
    for prefix in PLUGIN_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _message_of(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The entry's message dict, or an empty dict for any other shape.

    Guards the same failure `read_context` guards: a truthy non-dict message
    would survive an `or {}` fallback and raise on .get().
    """
    message = entry.get("message")
    return message if isinstance(message, dict) else {}


def _blocks(entry: Dict[str, Any]) -> List[Any]:
    content = _message_of(entry).get("content")
    return content if isinstance(content, list) else []


def _text_of(value: Any) -> str:
    """Flatten a tool_result content field, which may be str or block list."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(parts)
    return ""


def _is_prompt(entry: Dict[str, Any]) -> bool:
    """True for a real human prompt, not a tool result.

    Claude Code records tool results as type "user" entries whose content is a
    block list containing tool_result blocks. Most "user" entries in a real
    transcript are tool results -- so timing gaps between raw "user"
    entries measure how long a tool took, not how long the user was away.
    """
    if entry.get("type") != "user":
        return False
    content = _message_of(entry).get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                return False
    return True


def mentions_path(path: str, entries: List[Dict[str, Any]]) -> bool:
    """True when a real human prompt names `path` — i.e. the user attached it.

    Restricted to prompts via `_is_prompt` on purpose. A handover's basename
    turns up constantly in tool calls and tool results — every `ls` of specs/,
    every grep, every session that works on this tool — and counting those
    would repeat the merge-marker false positive: reading *about* a document is
    not continuing the work it describes.
    """
    base = os.path.basename(path or "")
    if not base:
        return False
    for entry in entries:
        if not _is_prompt(entry):
            continue
        if base in _text_of(_message_of(entry).get("content")):
            return True
    return False


def detect_boundaries(entries: List[Dict[str, Any]]) -> List[str]:
    """Deterministic boundary evidence found in these entries.

    Returns a sorted list drawn from {pr_merged, idle_gap, phase_done}.
    This function reports facts; it never decides whether to hand over.
    """
    found = set()
    handoff_tool_ids = set()
    bash_tool_ids = set()
    user_times = []

    for entry in entries:
        if _is_prompt(entry):
            stamp = _parse_ts(entry.get("timestamp"))
            if stamp is not None:
                user_times.append(stamp)

        for block in _blocks(entry):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")

            if kind == "tool_use":
                name = block.get("name") or ""
                params = block.get("input")
                params = params if isinstance(params, dict) else {}

                if name == "Bash":
                    if block.get("id"):
                        bash_tool_ids.add(block["id"])
                    command = str(params.get("command") or "")
                    if any(marker in command for marker in MERGE_COMMANDS):
                        found.add("pr_merged")

                elif name in ("Task", "Agent"):
                    subagent = str(params.get("subagent_type") or "")
                    if _bare_agent_type(subagent) in HANDOFF_SUBAGENTS and block.get("id"):
                        handoff_tool_ids.add(block["id"])

                elif name == "TodoWrite":
                    todos = params.get("todos")
                    if (isinstance(todos, list) and todos
                            and all(isinstance(item, dict)
                                    and item.get("status") == "completed"
                                    for item in todos)):
                        found.add("phase_done")

            elif kind == "tool_result":
                text = _text_of(block.get("content"))
                # Merge markers only count as evidence when they came out of a
                # shell command. Otherwise merely READING a file that documents
                # these markers -- this plan, or the test file -- reports a
                # merge that never happened. It self-triggered before this gate.
                if (block.get("tool_use_id") in bash_tool_ids
                        and any(marker in text for marker in MERGE_RESULT_MARKERS)):
                    found.add("pr_merged")
                if block.get("tool_use_id") in handoff_tool_ids and "DONE" in text:
                    found.add("phase_done")

    if len(user_times) >= 2:
        user_times.sort()
        if (user_times[-1] - user_times[-2]).total_seconds() > IDLE_GAP_SECONDS:
            found.add("idle_gap")

    return sorted(found)


def boundaries_for(path: str, since_iso: Optional[str] = None) -> List[str]:
    """All boundary evidence for a transcript, scanned only since `since_iso`
    (the arming time) so old evidence doesn't re-fire.
    """
    all_entries = list(iter_entries(path))
    since = _parse_ts(since_iso) if since_iso else None
    def is_recent(entry):
        stamp = _parse_ts(entry.get("timestamp"))
        if stamp is None:
            return True          # undated entry — include it, conservatively
        try:
            return stamp >= since
        except TypeError:
            return True          # naive/aware mismatch — include it

    recent = all_entries if since is None else [e for e in all_entries
                                                if is_recent(e)]

    return detect_boundaries(recent)


# --- outstanding work ---------------------------------------------------------
#
# Whether there is anything genuinely left to hand over. Two independent
# local checks, no network. Every function below returns None on "cannot
# tell" rather than guessing: an unknown must never look like a positive
# finding of no-work, or a suppression could fire on nothing outstanding at all
# (should_inject explains why this check fails toward silence).

def last_todo_status(path: str) -> Optional[bool]:
    """Whether the transcript's LAST non-empty TodoWrite list has open items.

    LAST, not first: scanning the whole file and overwriting on every match
    is the entire correctness of this function. A session with an incomplete
    list at turn 10 and an all-completed list at turn 90 is done -- stopping
    at the first match would report it as outstanding forever.

    Scans the WHOLE transcript, not from scan_floor. The floor exists so
    boundary *evidence* is reported once; completion is a current fact, not
    an event, and only the latest list describes now.

    Mirrors the all-completed test in detect_boundaries's TodoWrite branch
    (isinstance(todos, list) and todos, items must be dicts) rather than
    inventing a second interpretation of the payload -- this is that test's
    inverse over a different scan window.
    """
    last_todos = None
    for entry in iter_entries(path):
        for block in _blocks(entry):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "TodoWrite":
                continue
            params = block.get("input")
            params = params if isinstance(params, dict) else {}
            todos = params.get("todos")
            if isinstance(todos, list) and todos:
                last_todos = todos
    if last_todos is None:
        return None
    return not all(isinstance(item, dict) and item.get("status") == "completed"
                   for item in last_todos)


# Bounded to about a second per call -- this runs on UserPromptSubmit, in
# front of the human, and a hang here costs someone a prompt.
GIT_TIMEOUT = 1.0


def _git(cwd: str, *args: str) -> Optional[str]:
    """stdout of a local git command, or None on any failure. Never raises.

    No network flag is ever passed here -- see checkout_holds_work.
    """
    try:
        result = subprocess.run(("git", "-C", cwd) + args,
                                capture_output=True, text=True, encoding="utf-8",
                                timeout=GIT_TIMEOUT)
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def checkout_holds_work(cwd: str) -> Optional[bool]:
    """Whether this ONE checkout has anything not recorded anywhere else.

    True  -- a dirty working tree, or HEAD ahead of its upstream, or (no
             upstream) ahead of a locally-known main/master.
    False -- clean and not ahead.
    None  -- not a git checkout, git missing, a non-zero exit, or a timeout.

    Local only: no `gh`, no `git fetch`, no `ls-remote` -- ruled out
    deliberately, not to be revisited. Two or three cheap calls, each bounded
    by GIT_TIMEOUT, because this runs in the prompt path. Always the cwd from
    the hook payload -- never os.getcwd(), which is the hook process's own
    directory, not the session's.
    """
    try:
        if not cwd or not os.path.isdir(cwd):
            return None

        status = _git(cwd, "status", "--porcelain")
        if status is None:
            return None
        if status.strip():
            return True

        base = "@{u}"
        if _git(cwd, "rev-parse", "--abbrev-ref",
                "--symbolic-full-name", "@{u}") is None:
            # No upstream configured -- fall back to a locally-known default
            # branch. This is the normal shape for a claim worktree, whose
            # branch is created fresh off HEAD with no upstream at all.
            base = None
            for candidate in ("main", "master"):
                if _git(cwd, "rev-parse", "--verify",
                        "--quiet", candidate) is not None:
                    base = candidate
                    break
            if base is None:
                # No upstream AND no main/master, so nothing local can tell us
                # whether HEAD carries commits that exist nowhere else. A clean
                # tree is NOT the answer to that question -- the commits would
                # be clean too. Unknown, not False: only a positive finding of
                # no-work may suppress. A repo whose default branch is neither
                # main nor master lands here.
                return None

        ahead = _git(cwd, "rev-list", "--count", "%s..HEAD" % base)
        if ahead is None:
            return None  # non-zero exit or timeout -- unknown, per the docstring
        try:
            return int(ahead.strip()) > 0
        except ValueError:
            return None  # unparseable count is not evidence of a clean state
    except Exception:
        return None


# Mirrors claim_lib.CLAIMS_PATH (tools/session-claim). Not imported at hook
# runtime -- a cross-tool import in the prompt path would couple two tools'
# load paths for one string -- but a test imports claim_lib directly and
# asserts the two agree, so drift is caught rather than silently reading a
# file that moved.
CLAIMS_PATH = os.path.expanduser("~/.claude/session-claims/claims.json")

# A stale or hand-edited claims file must not turn one prompt into forty git
# calls.
MAX_CANDIDATES = 8


def _claimed_checkouts(session_id: str) -> List[str]:
    """Checkouts session-claim has on record for this session_id. [] on any
    failure -- a missing, corrupt, or foreign-session-only claims file
    contributes no candidates rather than raising.

    Known coupling: when session_claim.enabled is false nothing writes this
    file, so this always returns [] and outstanding_work falls back to cwd
    alone -- weaker (returns None more often), never wrong, which is the
    correct direction for a check that fails toward silence.
    """
    try:
        with open(CLAIMS_PATH, encoding="utf-8") as handle:
            records = json.load(handle)
    except Exception:
        return []
    if not isinstance(records, list):
        return []
    out = []
    for record in records:
        if not isinstance(record, dict) or record.get("session_id") != session_id:
            continue
        checkout = record.get("checkout")
        if isinstance(checkout, str) and checkout:
            out.append(checkout)
        if len(out) >= MAX_CANDIDATES:
            break
    return out


def outstanding_work(path: str, cwd: str, session_id: str) -> Optional[bool]:
    """Whether ANYTHING says this session still has work to continue.

    Combines last_todo_status(path) with checkout_holds_work() over the
    candidate checkouts: the hook's own cwd, plus every checkout session-claim
    has on record for this session_id. The hook's payload cwd is routinely the
    workspace root, not a git checkout at all, so cwd alone would miss the
    checkout a session actually worked in.

    True  -- any signal is True.
    False -- no signal is True, and at least one is False.
    None  -- every signal is None.

    The middle rule is deliberate, not a simplification: requiring BOTH
    dimensions to be False (rather than "no True and >=1 False") would mean a
    session with no TodoWrite at all -- last_todo_status is None, not False --
    could never suppress even with a demonstrably clean, non-ahead checkout.
    That is the common case this gate exists for.

    Short-circuits on the first True: once anything says there is outstanding
    work, the remaining git calls are wasted, and this runs in the prompt path.
    """
    todo_signal = last_todo_status(path)
    if todo_signal is True:
        return True

    candidates = []
    if cwd:
        candidates.append(cwd)
    for checkout in _claimed_checkouts(session_id):
        if checkout not in candidates:
            candidates.append(checkout)
    candidates = candidates[:MAX_CANDIDATES]

    saw_false = todo_signal is False
    for candidate in candidates:
        if not os.path.isdir(candidate):
            continue
        result = checkout_holds_work(candidate)
        if result is True:
            return True
        if result is False:
            saw_false = True

    return False if saw_false else None


# --- per-session state -------------------------------------------------------

STATE_DIR = os.path.expanduser("~/.claude/thread-handover/sessions")
INJECT_EVERY = 5

_DEFAULT_STATE = {
    "stage": QUIET,
    "armed_at": None,
    "signals": [],
    "turn": 0,
    "last_inject_turn": 0,
    "last_inject_at": None,
    "snoozed_at": None,
}


def _state_path(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session_id or "unknown")
    return os.path.join(STATE_DIR, safe + ".json")


def load_state(session_id: str) -> Dict[str, Any]:
    """Session state, with defaults filled in. Never raises."""
    state = dict(_DEFAULT_STATE)
    state["signals"] = []
    try:
        with open(_state_path(session_id), encoding="utf-8") as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            for key in _DEFAULT_STATE:
                if key in stored:
                    state[key] = stored[key]
    except Exception:
        pass
    return state


def save_state(session_id: str, state: Dict[str, Any]) -> None:
    """Persist session state. Never raises."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(_state_path(session_id), "w", encoding="utf-8") as handle:
            json.dump(state, handle)
    except Exception:
        pass


def scan_floor(state: Dict[str, Any]) -> Optional[str]:
    """The ISO timestamp boundary evidence should be scanned from.

    Evidence is reported once. Rescanning from armed_at forever means a single
    completed todo list re-proposes a handover every INJECT_EVERY turns for the
    rest of the session -- the surest way to get this tool switched off.
    """
    return state.get("last_inject_at") or state.get("armed_at")


def should_inject(state: Dict[str, Any], stage: str,
                  signals: List[str],
                  outstanding: Optional[bool] = None) -> bool:
    """Whether this turn should print a reminder.

    Re-injection is the anti-drift mechanism — a one-time instruction scrolls
    out of context and is forgotten. Injecting every turn is noise. So: on
    arming, on state change, on new evidence, every INJECT_EVERY turns, and
    unconditionally while urgent.

    `outstanding` defaults to None so every existing caller and test is
    unchanged — this is what keeps the gate additive. It suppresses an ARMED
    suggestion only when outstanding_work() has POSITIVELY determined there is
    nothing to hand over (`outstanding is False`, never plain falsiness --
    None means "couldn't tell" and must not suppress).

    This inverts every other guard's fail direction on purpose, and that
    inversion is deliberate, not an oversight: the danger guard allows what it
    cannot classify, the claim guard allows on timeout, hooks stay quiet on
    bad input -- all of those fail toward ACTING. This one fails toward
    SILENCE, because the two costs here are not symmetric: a wrong
    suppression costs one manual `/handover`, a wrong suggestion costs
    attention every INJECT_EVERY turns. That asymmetry is only safe *because*
    `/handover` stays available manually — do not "fix" this into failing
    loud without re-reading why it doesn't.

    Order matters: URGENT is checked, and returns True, before `outstanding`
    is even consulted. URGENT is context exhaustion — a different failure
    with a different cost — and it must fire even mid-task, never suppressed.
    """
    if stage == QUIET:
        return False
    if stage == URGENT:
        return True
    if outstanding is False:
        return False
    if stage != state.get("stage"):
        return True
    if set(signals) - set(state.get("signals") or []):
        return True
    turn = state.get("turn") or 0
    last = state.get("last_inject_turn") or 0
    return (turn - last) >= INJECT_EVERY
