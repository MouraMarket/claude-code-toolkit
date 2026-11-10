"""skill_trigger_lib — stream parsing, classification, subprocess running, and
control orchestration for the skill-trigger eval CLI. Single module — the
repo's other tools pair one lib with one executable.

skill-trigger measures which skill a prompt actually routes to, against
*today's* loaded sibling set: the skills and plugins visible to this Claude
Code session (see `extract_init`). A result is a measurement of this machine's
session, not a durable fact — the same prompt can route differently once a
sibling skill is added, renamed, or removed from the plugin marketplace.
`claude-skill-trigger --help` carries the same caveat for anyone invoking the
CLI directly.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, Optional

MATCH = "MATCH"
COLLISION = "COLLISION"
NO_SKILL = "NO_SKILL"
FORBIDDEN = "FORBIDDEN"
RUNNER_ERROR = "RUNNER_ERROR"
INCOMPLETE = "INCOMPLETE"

# The harness control proves the instrument can see a Skill call at all. It
# uses the build skill's own positive trigger prompt because the build skill
# is installed wherever skill authoring is (the maintainer profile requires
# builder); a test keeps this string equal to the trigger-cases.json one.
HARNESS_PROMPT = (
    "In this toolkit repo, add a new SessionStart hook that warns when the "
    "current repo has no AGENTS.md, with tests, its hooks.json registration "
    "and an install.sh --check line."
)

# Written over a skill's description during the positive control. Never
# select this -- if a real skill somehow shipped with this text, that alone
# is a bug.
INERT_DESCRIPTION = "Inert placeholder written by a trigger-eval control. Never select this."

# Sidecar suffix for the positive control's durability backup.
BACKUP_SUFFIX = ".skill-trigger-backup"

_SKILL_KEY_RE = re.compile(r'"skill"\s*:\s*"')


def iter_events(lines: Iterable[str]) -> Iterator[Dict[str, Any]]:
    """Parse each line as a JSON outer object, skipping unparseable lines."""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue


def _try_extract_skill_value(buf: str) -> Optional[str]:
    """Return the `skill` string value once its closing quote is present.

    Never `json.loads` the whole (possibly incomplete) object, and never
    regex across an unterminated string: scan character by character from
    the opening quote, honouring backslash escapes, and return only once a
    real closing quote is found. Returns None if the value is not yet
    complete, or if the key isn't in the buffer at all.
    """
    match = _SKILL_KEY_RE.search(buf)
    if match is None:
        return None
    start = match.end()
    i = start
    while i < len(buf):
        ch = buf[i]
        if ch == "\\":
            if i + 1 >= len(buf):
                return None  # escape sequence not yet complete
            i += 2
            continue
        if ch == '"':
            raw = buf[start:i]
            try:
                return json.loads('"' + raw + '"')
            except (json.JSONDecodeError, ValueError):
                return None
        i += 1
    return None  # no closing quote yet


class _SkillNameScanner:
    """Incremental core of `find_skill_name`'s scanning logic, shared by the
    batch function below and `run_case`'s streaming reader.

    Feed it outer event objects one at a time via `.feed()`. `.resolved`
    becomes `True` once the first Skill block's answer is settled — a name
    extracted, or its own `content_block_stop` reached with no value — and
    `.value` holds the name at that point (`None` if it never resolved to
    one). This is exactly the "moment `find_skill_name` resolves" that lets
    `run_case` kill the subprocess early rather than paying for the whole
    skill body: `run_case` can check `.resolved and .value is not None`
    after every fed line and stop reading the instant that is true.

    The `content_block_stop` terminator is mandatory, not tidiness: MEASURED
    in `tests/fixtures/skill-call.jsonl`, the stream reuses block indices
    across message turns (index 1 carries the Skill call's
    `input_json_delta` events and, in a later turn, an unrelated `text`
    block's `text_delta` events, with two separate `content_block_stop`s at
    that index). A parser that kept concatenating past the terminator would
    read the later block's data as if it belonged to the first.

    First Skill block wins: a later one is never consulted, even when the
    first block's own value never resolved before its stop.
    """

    def __init__(self) -> None:
        self.target_index: Optional[int] = None
        self.buf = ""
        self.resolved = False
        self.value: Optional[str] = None

    def feed(self, obj: Dict[str, Any]) -> None:
        if self.resolved:
            return
        event = obj.get("event") if isinstance(obj, dict) else None
        if not isinstance(event, dict):
            return
        event_type = event.get("type")

        if self.target_index is None:
            if event_type == "content_block_start":
                content_block = event.get("content_block")
                if (
                    isinstance(content_block, dict)
                    and content_block.get("name") == "Skill"
                ):
                    self.target_index = event.get("index")
                    self.buf = ""
            return

        if event.get("index") != self.target_index:
            return

        if event_type == "content_block_delta":
            delta = event.get("delta")
            fragment = delta.get("partial_json") if isinstance(delta, dict) else None
            if isinstance(fragment, str):
                self.buf += fragment
                value = _try_extract_skill_value(self.buf)
                if value is not None:
                    self.value = value
                    self.resolved = True
        elif event_type == "content_block_stop":
            # Terminator reached with no value extracted: this block is
            # done. First-block-wins means we stop here rather than
            # scanning ahead for a later Skill block.
            self.resolved = True


def find_skill_name(events: Iterable[Dict[str, Any]]) -> Optional[str]:
    """Return the first Skill call's skill name, or None.

    Locates the first outer object whose `.event.type ==
    "content_block_start"` and `.event.content_block.name == "Skill"`, notes
    its `.event.index`, then concatenates `.event.delta.partial_json` from
    subsequent objects whose `.event.index` matches — stopping at the first
    `content_block_stop` carrying that index. After each fragment, attempts
    extraction and returns as soon as the value is unambiguous. See
    `_SkillNameScanner` for the full reasoning; this is a thin batch wrapper
    over it.
    """
    scanner = _SkillNameScanner()
    for obj in events:
        scanner.feed(obj)
        if scanner.resolved:
            break
    return scanner.value


def extract_init(events: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return `{"skills": [...], "plugins": [...], "claude_code_version": str}`
    from the `{"type": "system", "subtype": "init"}` line, or None if absent.
    """
    for obj in events:
        if (
            isinstance(obj, dict)
            and obj.get("type") == "system"
            and obj.get("subtype") == "init"
        ):
            return {
                "skills": obj.get("skills") or [],
                "plugins": obj.get("plugins") or [],
                "claude_code_version": obj.get("claude_code_version") or "",
            }
    return None


def classify(observed: Optional[str], expected: str) -> str:
    """Classify an observed skill name against what was expected.

    Returns exactly one of the string constants MATCH, COLLISION, NO_SKILL.
    NO_SKILL is a distinct third value: no code path may collapse it onto
    COLLISION or onto a generic failure.
    """
    if observed is None:
        return NO_SKILL
    if observed == expected:
        return MATCH
    return COLLISION


@dataclass
class CaseRun:
    """The result of running one prompt through `claude -p` (the case
    reports consume this by these exact field names).
    """

    observed: Optional[str]  # skill name, or None
    init: Optional[Dict[str, Any]]  # from extract_init
    status: str  # "OK" | "RUNNER_ERROR" | "INCOMPLETE"
    error: Optional[str]  # populated iff status != "OK"


# A prompt whose lstrip() starts with '/' directly invokes
# a command or skill, and its '!' preprocessing runs before any tool call --
# entirely outside the read-only PreToolUse hook. `load_cases` and `run_case`
# each refuse it independently (the scope correction in the plan: the hook
# contains the model's tool calls, not the CLI's own input handling).
SLASH_COMMAND_REASON = (
    "prompt starts with '/' -- a direct slash command runs outside the "
    "read-only hook; trigger evals measure routing from plain prompts")


_GRACE_S = 3  # per-signal wait after killpg before escalating/giving up


_INTERRUPTS = (signal.SIGINT, signal.SIGTERM)


def _hold_interrupts():
    """Block SIGINT and SIGTERM for this thread and return the previous mask,
    or None where the platform has no `pthread_sigmask` (Windows). A blocked
    signal stays pending and is delivered by `_release_interrupts`."""
    if not hasattr(signal, "pthread_sigmask"):
        return None
    return signal.pthread_sigmask(signal.SIG_BLOCK, _INTERRUPTS)


def _release_interrupts(held_mask) -> None:
    """Restore the mask `_hold_interrupts` returned. No-op for None."""
    if held_mask is not None:
        signal.pthread_sigmask(signal.SIG_SETMASK, held_mask)


def _kill_process_group(proc: "subprocess.Popen[str]") -> None:
    """Terminate `proc`'s whole process group: SIGTERM, wait, then
    SIGKILL. Only signals and waits for exit -- draining stdout is the
    reader thread's job in `run_case`, which keeps the pipe from filling
    while the child is still running rather than this function reading it
    after the fact.

    `start_new_session=True` guarantees the child's process group id equals
    its own pid -- POSIX `setsid()` semantics, already applied by the time
    `Popen()` returns successfully (a failure there would have surfaced as
    an exception in the parent via the pre-exec error pipe). So `proc.pid`
    doubles as the pgid: no `os.getpgid` call, and no race against it, is
    needed.

    Signalling only `proc.pid` directly (skipping `killpg`) is exactly the
    bug this guards against: `claude` may fork descendants that share the
    group but are not the direct child, and those survive a plain `kill`,
    left reparented and unreachable.

    MEASURED on this machine (Darwin): once the group's leader has already
    exited and no live member remains, `os.killpg` raises `PermissionError`
    here, not `ProcessLookupError` -- an orphaned zombie process group is
    apparently un-signallable rather than absent, on this kernel. Both mean
    the same thing for cleanup purposes (nothing left to kill), so both are
    treated as "already gone."
    """
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break  # already gone -- nothing left to signal
        try:
            proc.wait(timeout=_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue

    # Reached either as the catastrophe fallback (SIGKILL didn't reap in
    # time) or because the group was already gone above. Either way, reap
    # now: an un-waited Popen object leaves a zombie and, later, a
    # ResourceWarning -- and never blocks the runner forever.
    try:
        proc.wait(timeout=_GRACE_S)
    except subprocess.TimeoutExpired:
        pass


# The read-only PreToolUse hook that every trigger-eval child session must
# carry (see `readonly_hook.py`). `realpath`, not `abspath` -- these tools
# run through `~/.local/bin` symlinks, so resolve the real path.
READONLY_HOOK = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "readonly_hook.py")


def readonly_settings() -> str:
    """The `--settings` payload `run_case` hands to every trigger-eval
    child: a `PreToolUse` hook that denies everything outside a small
    read-only allowlist, with `disableAllHooks` pinned `False`. Flag settings
    outrank user and project settings, so
    the pin keeps the hook armed even where a user's `true` would otherwise
    disable it -- but it also re-enables every hook that user switched off.
    That is why `claude-skill-trigger` refuses to run when `hooks_disabled_by`
    finds a `true`: the pin is safe only where nobody set the key."""
    return json.dumps({
        "disableAllHooks": False,
        "hooks": {
            "PreToolUse": [{
                "matcher": "*",
                "hooks": [{
                    "type": "command",
                    "command": (
                        shlex.quote(sys.executable) + " "
                        + shlex.quote(READONLY_HOOK)),
                }],
            }],
        },
    }, separators=(",", ":"))


def hooks_disabled_by(home: str, cwd: str) -> Optional[str]:
    """The settings file that sets `disableAllHooks: true`, if any. The
    `False` pin in `readonly_settings()` outranks it, so a trigger-eval
    child would silently run every hook that user chose to disable. There
    is no way to enable only the read-only hook, so the CLI refuses to run
    instead.

    Checks, in order, `<home>/.claude/settings.json`,
    `<cwd>/.claude/settings.json`, `<cwd>/.claude/settings.local.json`, and
    returns the first path whose JSON top level has `disableAllHooks is
    True`. A file that is missing, unreadable, or not valid JSON is
    skipped, not treated as a match. Returns `None` if none of the three
    disables hooks -- the normal case, where this only ever overrides the
    value nobody set.
    """
    candidates = (
        os.path.join(home, ".claude", "settings.json"),
        os.path.join(cwd, ".claude", "settings.json"),
        os.path.join(cwd, ".claude", "settings.local.json"),
    )
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("disableAllHooks") is True:
            return path
    return None


def run_case(prompt: str, *, timeout_s: float) -> CaseRun:
    """Run one prompt through `claude -p` and report what it routed to.

    Launches `claude -p <prompt> --include-partial-messages --output-format
    stream-json --verbose` with `CLAUDECODE` removed from its environment
    and stdin closed (`</dev/null`), **reading stdout line by line as it
    arrives** on a background reader thread, feeding each parsed line to a
    `_SkillNameScanner` as it comes in. The moment the scanner resolves to a
    name, the process group is killed immediately -- `subprocess.
    communicate()` is never used here, because it blocks until the child
    exits, which pays for the whole skill body rather than just the routing
    decision (a stub emitting a complete Skill
    call still ran the full timeout under a `communicate()`-based runner).
    `timeout_s` remains a ceiling on the whole read loop, not per line.

    `start_new_session=True` makes the child lead its own process group so
    a timeout -- or an early kill -- can be enforced against the whole
    group, never just the direct child claude may not be.

    `status="RUNNER_ERROR"` is a harness failure, and `init is None` is the
    discriminator for it: a missing `claude` binary is caught before the
    child ever runs, and otherwise **no init event at all** means the run
    never really started, whatever else happened (a clean exit with no
    output, a timeout with no output, or output that parsed as nothing).
    Every real run emits the init line first (both fixtures), so its
    absence is reliable evidence. This is never conflated with `NO_SKILL`
    (a clean run that produced no Skill call is a real, reportable routing
    result) -- only a run that produced an init event yields `status="OK"`;
    `classify()` is what turns `observed` into MATCH/COLLISION/NO_SKILL,
    downstream of this.

    A timeout with an init event already seen and no Skill call is
    `status="INCOMPLETE"`, which is neither a harness failure nor a routing
    result: the run was cut off before it could route. Only a run that
    *finished* with no Skill call is `status="OK"`, `observed=None`.

    A second, narrower discriminator covers a child that emitted init and
    then crashed on its own (auth/network failure) rather than being killed
    by us: only when the read loop ended because the child closed stdout by
    itself (EOF, not our own early-kill-on-resolution and not a timeout) do
    we wait up to `_GRACE_S` for its *natural* exit status, before
    `_kill_process_group` can turn that into a signal-induced one. A
    non-zero natural return code there is `status="RUNNER_ERROR"` even
    though init was seen -- a crash, not a routing result. If the child
    hasn't exited within the grace period, its status is simply unknown
    (not a failure by itself) and existing cleanup handles it. This never
    applies to our own early kill or a timeout: a negative returncode we
    produced by signalling the process group must never become
    `RUNNER_ERROR`.

    Refuses a slash-command prompt (`prompt.lstrip().startswith("/")`)
    before launching anything -- independently of `load_cases`'s own refusal,
    since a caller could reach `run_case` directly. See `SLASH_COMMAND_REASON`.
    """
    if prompt.lstrip().startswith("/"):
        return CaseRun(
            observed=None, init=None, status="RUNNER_ERROR",
            error=SLASH_COMMAND_REASON)

    cmd = [
        "claude",
        "-p",
        prompt,
        "--include-partial-messages",
        "--output-format",
        "stream-json",
        "--verbose",
        "--settings",
        readonly_settings(),
    ]
    env = dict(os.environ)
    env.pop("CLAUDECODE", None)
    # N5: `install.sh` now points user-level `autoMemoryDirectory` at every
    # session, so evals must opt out explicitly to stay neutral -- N6's
    # canary eval will need its own opt-out flag.
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"

    line_queue: "queue.Queue[Optional[str]]" = queue.Queue()
    proc = None
    reader_thread = None
    deadline = time.monotonic() + timeout_s
    scanner = _SkillNameScanner()
    events = []
    timed_out = False
    eof_reached = False

    # The launch and the read loop are wrapped in try/finally so the
    # process-group kill runs on ANY exit -- not only a normal fall-through.
    # A KeyboardInterrupt (SIGINT) or an exception raised by the SIGTERM
    # handler (installed by `run_positive_control` for its window) would
    # otherwise skip `_kill_process_group` and orphan the child.
    #
    # The launch is INSIDE the try: with the try opening
    # after the reader thread started, an interrupt landing between
    # `Popen()` and the try left `claude` running -- reproduced by delaying
    # `Thread.start`, which is what a loaded machine does. And interrupts
    # are HELD for the launch itself: one landing after the child exists but
    # before `proc` is assigned (Popen waits for the child's exec) would find
    # `proc` None and leave the child running (Code Review, demonstrated).
    try:
        held_mask = _hold_interrupts()
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                env=env,
                start_new_session=True,
                text=True, encoding="utf-8",
                # A signal mask survives exec, so the child restores the
                # original one -- held, our own cleanup SIGTERM could not
                # reach it.
                preexec_fn=(None if held_mask is None
                            else lambda: _release_interrupts(held_mask)),
            )
        except OSError as exc:
            return CaseRun(observed=None, init=None, status="RUNNER_ERROR", error=str(exc))
        finally:
            # A held interrupt is delivered here, with `proc` recorded, so
            # the outer finally reaps the child.
            _release_interrupts(held_mask)

        def _reader() -> None:
            # Runs on its own thread so the main loop can enforce timeout_s as
            # a wall-clock deadline via Queue.get(timeout=...) rather than
            # blocking on a read that may never return. Iterating proc.stdout
            # also keeps draining the pipe continuously -- including after an
            # early kill decision, up until the process group actually dies --
            # so the child never blocks on a full pipe buffer while waiting to
            # be signalled.
            try:
                for line in proc.stdout:
                    line_queue.put(line)
            except (ValueError, OSError):
                pass
            finally:
                line_queue.put(None)  # sentinel: EOF, or the pipe broke

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                line = line_queue.get(timeout=remaining)
            except queue.Empty:
                timed_out = True
                break
            except RuntimeError as exc:
                # An interrupt landing inside the queue's lock re-acquire can
                # leave the lock released, and the `with` exiting it then
                # raises "release unlocked lock" with the interrupt as its
                # context -- seen on Python 3.9 under local CI:
                # exit 1 instead of a SIGINT death. Re-raise the
                # interrupt (KeyboardInterrupt, or _ControlSigterm --
                # BaseException, never Exception) so it is what propagates.
                interrupt = exc.__context__
                if isinstance(interrupt, BaseException) and not isinstance(interrupt, Exception):
                    raise interrupt from None
                raise
            if line is None:
                eof_reached = True
                break  # EOF: the child closed stdout
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            events.append(obj)
            if not scanner.resolved:
                scanner.feed(obj)
                if scanner.resolved and scanner.value is not None:
                    # The kill point: the name is known. Nothing later
                    # in the stream can change the answer, so stop paying
                    # for it.
                    break
    finally:
        # Only when the child closed stdout by itself (EOF) -- never on our
        # own early kill or a timeout, both of which produce a
        # signal-induced returncode that must never read as a failure -- do
        # we give the child a chance to report its own natural exit status
        # before `_kill_process_group` can turn that into one. A child that
        # closed stdout but hasn't exited within the grace period is simply
        # unknown, not a failure by itself; the kill below still reaps it.
        # `proc` is None only when `Popen()` itself failed or was interrupted
        # -- there is no child to reap, and nothing below runs.
        natural_rc = None
        if proc is not None:
            if eof_reached:
                try:
                    proc.wait(timeout=_GRACE_S)
                    natural_rc = proc.returncode
                except subprocess.TimeoutExpired:
                    natural_rc = None
            _kill_process_group(proc)
            # `ident` is set once the thread has started; an interrupt inside
            # `start()` can leave it unstarted, and joining that raises.
            if reader_thread is not None and reader_thread.ident is not None:
                reader_thread.join(timeout=_GRACE_S)
            try:
                proc.stdout.close()
            except OSError:
                pass

    init = extract_init(events)
    if init is None:
        if timed_out:
            error = "claude timed out before producing an init event"
        else:
            error = f"claude exited {proc.returncode} with no init event"
        return CaseRun(observed=None, init=None, status="RUNNER_ERROR", error=error)

    if eof_reached and natural_rc is not None and natural_rc != 0:
        return CaseRun(
            observed=None,
            init=init,
            status="RUNNER_ERROR",
            error=f"claude exited {natural_rc} after init",
        )

    if timed_out and scanner.value is None:
        return CaseRun(observed=None, init=init, status=INCOMPLETE,
                       error=f"timed out after {timeout_s}s with no Skill call "
                             "(run incomplete, not a routing result)")

    return CaseRun(
        observed=scanner.value,
        init=init,
        status="OK",
        error=None,
    )


# --- case file loading, per-case reports, and the verdict --------------

VALID_CONTROLS = (None, "positive")


class CaseFileError(Exception):
    """Raised for any malformed case file -- always naming the file and, for
    an element-level defect, the offending index. The CLI's contract is that
    nothing runs before this is raised: every malformed-input path must be
    caught here, before the first `run_case` call.
    """


def load_cases(path):
    """Load and validate the JSON case file at `path`.

    Schema: a non-empty JSON list of objects, each carrying `prompt` (str,
    required), exactly one of `expect` (str) or `forbid` (non-empty str), and
    `control` (`None` or `"positive"`, optional, default `None`). An `expect`
    case passes when the model calls that skill; a `forbid` case passes when
    the model calls anything else, including no skill. A `control ==
    "positive"` case must be an `expect` case, and its `expect` must be
    fully-qualified `plugin:skill` -- it has to resolve to a file the positive
    control can mutate, and a bare toolkit skill name does not live in the plugin cache.
    Bare names remain legal on every other case.

    Raises `CaseFileError` for anything malformed: a missing/unreadable
    file, invalid JSON, a non-list top level, an empty list, a non-object
    element, a missing `prompt`, both or neither of `expect`/`forbid`, a
    non-string `expect` or non-string/empty `forbid`, an out-of-schema
    `control` value, a `forbid` case with `control: "positive"`, a
    positive-control case whose `expect` has no `:`, or a `prompt` whose
    `lstrip()` starts with `/` (`SLASH_COMMAND_REASON`).
    Returns a list of dicts: `{"prompt": str, "expect": str|None, "forbid":
    str|None, "control": None|"positive"}`; the absent one of
    `expect`/`forbid` is `None`.
    """
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        raise CaseFileError(f"{path}: cannot read case file: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CaseFileError(f"{path}: not valid JSON: {exc}") from exc

    if not isinstance(data, list):
        raise CaseFileError(f"{path}: case file must be a JSON list")

    if len(data) == 0:
        raise CaseFileError(
            f"{path}: case file is empty -- an eval set with no cases "
            "cannot fail and must not report a pass")

    cases = []
    for index, element in enumerate(data):
        if not isinstance(element, dict):
            raise CaseFileError(f"{path}: case {index}: not a JSON object")

        prompt = element.get("prompt")
        if not isinstance(prompt, str):
            raise CaseFileError(
                f"{path}: case {index}: missing or invalid 'prompt'")

        if prompt.lstrip().startswith("/"):
            raise CaseFileError(f"{path}: case {index}: {SLASH_COMMAND_REASON}")

        if "expect" in element and "forbid" in element:
            raise CaseFileError(
                f"{path}: case {index}: carries both 'expect' and 'forbid' "
                "-- exactly one is allowed")
        if "expect" not in element and "forbid" not in element:
            raise CaseFileError(
                f"{path}: case {index}: missing 'expect' or 'forbid'")

        expect = element.get("expect")
        forbid = element.get("forbid")
        if "expect" in element and not isinstance(expect, str):
            raise CaseFileError(
                f"{path}: case {index}: missing or invalid 'expect'")
        if "forbid" in element and (
                not isinstance(forbid, str) or forbid == ""):
            raise CaseFileError(
                f"{path}: case {index}: 'forbid' must be a non-empty string")

        control = element.get("control")
        if control not in VALID_CONTROLS:
            raise CaseFileError(
                f"{path}: case {index}: 'control' must be null or "
                f"\"positive\", got {control!r}")

        if control == "positive" and forbid is not None:
            raise CaseFileError(
                f"{path}: case {index}: control \"positive\" is legal only "
                "on an 'expect' case, not a 'forbid' case")

        if control == "positive" and ":" not in expect:
            raise CaseFileError(
                f"{path}: case {index}: a positive-control case's 'expect' "
                f"must be fully-qualified plugin:skill, got {expect!r}")

        cases.append({"prompt": prompt, "expect": expect, "forbid": forbid,
                      "control": control})

    return cases


def build_case_report(case, runs):
    """Aggregate `runs` -- a list of `CaseRun`, all repeated executions
    of `case["prompt"]` -- into one per-case report dict.

    Routing is stochastic, so one run measures nothing reliably. This
    counts across every run rather than trusting any single one.

    `state`, evaluated top to bottom:
      1. Any run `RUNNER_ERROR` -> `RUNNER_ERROR`, `error` = the first such
         run's message. Never averaged over a broken harness.
      2. Fewer than half the runs completed (`completed * 2 < len(runs)`,
         `completed` = runs with status `OK`) -> `INCOMPLETE`, `error` = the
         first incomplete run's message. A timeout is not a routing result.
      3. `matches * 2 >= completed` -> `MATCH` (at least half of the runs
         that finished).
      4. A forbid case -> `FORBIDDEN`.
      5. Any run observed a skill other than `expected` -> `COLLISION`.
      6. Otherwise -> `NO_SKILL`.

    A run is a hit when `status == "OK"` and: for an expect case,
    `classify(observed, expect) == MATCH`; for a forbid case,
    `observed != forbid` (no Skill call is a hit). `matches` counts hits.
    The report carries `"forbidden"` (the forbid value, `None` on an expect
    case); `"expected"` is `None` on a forbid case.

    At `runs == [<one CaseRun>]` this reduces exactly to the single-run
    `classify()` result -- the property the repeat-run design depends on.

    `observed_counts`: one entry per distinct observed value from runs with
    `status == "OK"` (`None` for no Skill call), sorted by count descending,
    ties in first-seen order. `observed` is the first entry's value, or
    `None` if no run was `OK`.
    """
    expected = case["expect"]
    forbidden = case.get("forbid")
    n = len(runs)

    error_run = next((r for r in runs if r.status == RUNNER_ERROR), None)
    incomplete_run = next((r for r in runs if r.status == INCOMPLETE), None)
    completed = sum(1 for r in runs if r.status == "OK")
    incomplete = sum(1 for r in runs if r.status == INCOMPLETE)
    if forbidden is not None:
        matches = sum(
            1 for r in runs
            if r.status == "OK" and r.observed != forbidden)
    else:
        matches = sum(
            1 for r in runs
            if r.status == "OK" and classify(r.observed, expected) == MATCH)

    order = []
    counts = {}
    for r in runs:
        if r.status != "OK":
            continue
        if r.observed not in counts:
            counts[r.observed] = 0
            order.append(r.observed)
        counts[r.observed] += 1
    ordered_keys = sorted(order, key=lambda k: -counts[k])
    observed_counts = [{"skill": k, "count": counts[k]} for k in ordered_keys]
    observed = observed_counts[0]["skill"] if observed_counts else None

    if error_run is not None:
        state = RUNNER_ERROR
        error = error_run.error
    elif completed * 2 < n:
        state = INCOMPLETE
        error = incomplete_run.error if incomplete_run is not None else None
    elif matches * 2 >= completed:
        state = MATCH
        error = None
    elif forbidden is not None:
        state = FORBIDDEN
        error = None
    elif any(
            r.status == "OK" and r.observed is not None
            and r.observed != expected
            for r in runs):
        state = COLLISION
        error = None
    else:
        state = NO_SKILL
        error = None

    return {
        "prompt": case["prompt"],
        "expected": expected,
        "forbidden": forbidden,
        "control": case["control"],
        "runs": n,
        "matches": matches,
        "incomplete": incomplete,
        "observed_counts": observed_counts,
        "observed": observed,
        "state": state,
        "error": error,
    }


# --- the two controls --------------------------------------------------


class ControlError(Exception):
    """Raised for any positive-control defect that must exit 2, with the
    CLI running nothing further: skill-name registry resolution, frontmatter
    validation, or a leftover backup blocking a fresh control run at the
    exact target path. Always names the file, plugin, or path involved.
    """


class FrontmatterError(ControlError):
    """A `SKILL.md`'s frontmatter is malformed, or carries no `description:`
    key -- a `ControlError` subtype so callers can catch either with one
    `except ControlError` if they don't need to distinguish.
    """


class LeftoverRestoreError(Exception):
    """A leftover `*.skill-trigger-backup` sidecar (from an interrupted
    prior run) could not be restored to its target. The CLI must exit 4
    immediately on this -- a real skill may still be disarmed.
    """


def _atomic_write(path, data):
    """Write `data` to `path` atomically: a temp file in the SAME directory,
    fsync'd, then `os.replace`d over the target. Same-directory is
    load-bearing -- `os.replace` is only atomic within one filesystem, and a
    temp file elsewhere risks a non-atomic cross-device fallback.
    """
    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".skill-trigger-tmp-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _import_plugin_paths():
    """Import `lib/plugin_paths.py` (repo root `lib/`, three `dirname`s up
    from this file), lazily so the rest of the tool loads without it.
    Raises `ImportError` if it cannot be loaded; each caller decides
    whether that is fatal (a writer) or skippable (a reader).
    """
    lib_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.realpath(__file__)))), "lib")
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import plugin_paths
    return plugin_paths


def resolve_skill_md(expect, *, home, cwd):
    """Resolve `expect == "<plugin>:<skill>"` to an installed `SKILL.md`
    path via the plugin registry `$HOME/.claude/plugins/installed_plugins.json`
    -- the copy the runtime itself recorded as installed, not a cache glob
    (the plugin cache can hold several versions of one skill, so a
    glob-and-abort-on-ambiguity rule would abort on a real skill).

    Raises `ControlError`, naming the file, plugin, or path involved, on any
    of: an unreadable/unparseable registry, zero or more-than-one matching
    `<plugin>@<marketplace>` key, a project-scoped entry whose `projectPath`
    is `cwd` or an ancestor of it (we do not model which copy the runtime
    would load there -- INFERRED, and the abort is the safe answer either
    way), zero or more than one `scope == "user"` entry, an unloadable
    `lib/plugin_paths.py`, no install root for the key, or a missing
    `SKILL.md` at the resolved install root.

    The registry picks the KEY (which `<plugin>@<marketplace>` is installed)
    but not the PATH: for a directory-source marketplace such as
    `claude-code-toolkit`, `installPath` is a stale cache copy, not the checkout the
    runtime loads, and the registry still names the marketplace the
    plugin came from. `plugin_paths.installed_root` resolves the key
    inside its own marketplace only; a hosted key still resolves to the
    user entry's `installPath`.
    """
    plugin, sep, skill = expect.partition(":")
    if not sep:
        raise ControlError(
            f"{expect!r}: not fully-qualified plugin:skill (load_cases "
            "should have refused this already)")

    registry_path = os.path.join(home, ".claude", "plugins", "installed_plugins.json")
    try:
        with open(registry_path, encoding="utf-8") as f:
            raw = f.read()
    except OSError as exc:
        raise ControlError(
            f"{registry_path}: cannot read installed_plugins.json: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ControlError(f"{registry_path}: not valid JSON: {exc}") from exc

    plugins_map = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins_map, dict):
        raise ControlError(f"{registry_path}: missing or invalid 'plugins' map")

    matching_keys = sorted(
        k for k in plugins_map if isinstance(k, str) and k.split("@", 1)[0] == plugin)
    if len(matching_keys) == 0:
        raise ControlError(f"{registry_path}: no installed plugin named {plugin!r}")
    if len(matching_keys) > 1:
        raise ControlError(
            f"{registry_path}: plugin {plugin!r} matches more than one "
            f"registry key: {', '.join(matching_keys)}")

    key = matching_keys[0]
    entries = plugins_map[key]
    if not isinstance(entries, list):
        raise ControlError(f"{registry_path}: {key!r} is not a list of entries")

    cwd_real = os.path.realpath(cwd)
    user_entries = [
        e for e in entries if isinstance(e, dict) and e.get("scope") == "user"]

    def _applies_below_cwd(project_path):
        if not isinstance(project_path, str):
            return False
        project_real = os.path.realpath(project_path)
        return cwd_real == project_real or cwd_real.startswith(project_real + os.sep)

    for entry in entries:
        if not isinstance(entry, dict) or entry.get("scope") == "user":
            continue
        if _applies_below_cwd(entry.get("projectPath")):
            user_install = user_entries[0].get("installPath") if user_entries else None
            raise ControlError(
                f"{registry_path}: a project-scoped pin for {key!r} applies "
                f"below the current directory -- project installPath "
                f"{entry.get('installPath')!r} vs user installPath "
                f"{user_install!r}. Which one the runtime loads here is a "
                "question about the binary; refusing rather than guessing")

    if len(user_entries) == 0:
        raise ControlError(f"{registry_path}: no scope==\"user\" entry for {key!r}")
    if len(user_entries) > 1:
        raise ControlError(
            f"{registry_path}: more than one scope==\"user\" entry for {key!r}")

    try:
        plugin_paths = _import_plugin_paths()
    except ImportError as exc:
        raise ControlError(f"cannot load lib/plugin_paths.py: {exc}") from exc
    install_path = plugin_paths.installed_root(home, key)
    if install_path is None:
        raise ControlError(
            f"{registry_path}: no install root resolves for {key!r}")

    target = os.path.join(install_path, "skills", skill, "SKILL.md")
    if not os.path.isfile(target):
        raise ControlError(f"{target}: not a regular file")
    return target


def gut_description(original_bytes, path):
    """Return `original_bytes` with the frontmatter `description:` span
    replaced by `INERT_DESCRIPTION`. Every byte outside the span is
    unchanged, including line endings.

    Raises `FrontmatterError`, naming `path`, if: the file doesn't open with
    a line exactly `---`; there's no later line exactly `---` closing the
    frontmatter (both compared with trailing `\\r\\n` stripped); or there is
    no line beginning `description:` at column 0 inside the frontmatter.

    The description SPAN is that line plus every following line that is
    blank or begins with a space or tab, stopping at the first line that
    does not, or at the closing `---` -- this covers both a single-line
    value and a `>-` block-scalar with indented continuation lines.

    Pure and side-effect free: callers decide whether and where to write
    the result. `main`'s control validation calls this once, on the
    original bytes, purely to raise before any run happens; `run_positive_
    control` calls it again, on its own fresh read, to get the bytes it
    actually writes.
    """
    try:
        text = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FrontmatterError(f"{path}: not valid UTF-8: {exc}") from exc

    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        raise FrontmatterError(
            f"{path}: malformed frontmatter (must begin with a line "
            "exactly '---')")

    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == "---":
            close_idx = i
            break
    if close_idx is None:
        raise FrontmatterError(
            f"{path}: malformed frontmatter (no closing '---' line)")

    desc_start = None
    for i in range(1, close_idx):
        if lines[i].startswith("description:"):
            desc_start = i
            break
    if desc_start is None:
        raise FrontmatterError(f"{path}: no 'description:' key in frontmatter")

    desc_end = desc_start + 1
    while desc_end < close_idx:
        line = lines[desc_end]
        if line.rstrip("\r\n") == "" or line[:1] in (" ", "\t"):
            desc_end += 1
        else:
            break

    new_line = "description: " + INERT_DESCRIPTION + "\n"
    new_lines = lines[:desc_start] + [new_line] + lines[desc_end:]
    return "".join(new_lines).encode("utf-8")


_LEFTOVER_SCAN_SUBDIRS = ("cache", "marketplaces")


def scan_and_restore_leftover_backups(*, home):
    """Scan `$HOME/.claude/plugins/{cache,marketplaces}/`, recursively, and
    `<root>/skills` for every directory-source plugin root, for
    `*.skill-trigger-backup` sidecars left by a run that never reached its
    own restore (crash, SIGKILL, power loss). The directory roots are
    scanned because the positive control now writes there (a `claude-code-toolkit`
    skill is edited in the checkout itself); `cache/` and `marketplaces/`
    stay so a resolver change cannot orphan a backup in another tree. A
    missing root is skipped, not an error; an unloadable `plugin_paths`
    skips only the directory roots.

    Restores each to its target with the same atomic-write-plus-byte-
    compare as `run_positive_control`'s own restore, printing a loud
    stderr line per success, and removes the sidecar. Returns the list of
    restored target paths (empty if none were found).

    A **zero-byte** sidecar is never restored: it is the signature of a
    create-before-read crash rather than a real backup, and writing it
    over the target would silently truncate a real, disarmed skill file to
    nothing -- worse than leaving both in place. Both files are left
    untouched, a loud stderr line names both paths, and this is treated as
    the same failure class as a byte-compare mismatch below.

    Raises `LeftoverRestoreError`, naming the file, on the FIRST restore
    that fails to verify (or a zero-byte sidecar) -- the caller must exit 4
    immediately rather than continue scanning: a control that hunts past a
    failure proves nothing about the one that already failed.
    """
    restored = []
    scan_roots = [os.path.join(home, ".claude", "plugins", subdir)
                  for subdir in _LEFTOVER_SCAN_SUBDIRS]
    try:
        scan_roots += [os.path.join(plugin_root, "skills") for plugin_root in
                       _import_plugin_paths().directory_plugin_roots(home)]
    except ImportError:
        pass
    for root in scan_roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for filename in sorted(filenames):
                if not filename.endswith(BACKUP_SUFFIX):
                    continue
                backup_path = os.path.join(dirpath, filename)
                target_path = backup_path[: -len(BACKUP_SUFFIX)]
                try:
                    with open(backup_path, "rb") as f:
                        original_bytes = f.read()
                except OSError as exc:
                    raise LeftoverRestoreError(
                        f"{target_path}: failed to restore from leftover "
                        f"backup {backup_path}: {exc}") from exc

                if len(original_bytes) == 0:
                    sys.stderr.write(
                        f"{backup_path}: backup is EMPTY (0 bytes) -- "
                        f"refusing to restore over {target_path}; leaving "
                        "both files in place\n")
                    raise LeftoverRestoreError(
                        f"{target_path}: refusing to restore from empty "
                        f"backup {backup_path}")

                try:
                    _atomic_write(target_path, original_bytes)
                    with open(target_path, "rb") as f:
                        restored_bytes = f.read()
                    if restored_bytes != original_bytes:
                        raise OSError(
                            "byte-compare after restore did not match")
                    os.remove(backup_path)
                except OSError as exc:
                    raise LeftoverRestoreError(
                        f"{target_path}: failed to restore from leftover "
                        f"backup {backup_path}: {exc}") from exc
                sys.stderr.write(
                    f"restored {target_path} from a backup left by an "
                    "interrupted run\n")
                restored.append(target_path)
    return restored


class _ControlSigterm(BaseException):
    """Raised by `run_positive_control`'s SIGTERM handler so the same
    `finally` that performs the restore runs regardless of whether the
    window ended normally or was interrupted. Subclasses `BaseException`,
    not `Exception`, so a stray `except Exception` elsewhere can never
    accidentally swallow it.
    """


def _gutted_collapsed(gutted_runs, expected, baseline_matches):
    """Pure predicate behind the positive control's `collapsed`. Returns
    `(gutted_matches, collapsed)`. A RUNNER_ERROR or INCOMPLETE gutted run
    is never read as a collapse: a broken or cut-off run proves nothing.
    """
    gutted_error = any(
        r.status in (RUNNER_ERROR, INCOMPLETE) for r in gutted_runs)
    gutted_matches = sum(
        1 for r in gutted_runs
        if r.status == "OK" and classify(r.observed, expected) == MATCH)
    collapsed = (
        gutted_matches == 0 and baseline_matches >= 1 and not gutted_error)
    return gutted_matches, collapsed


def run_positive_control(
        case, case_index, skill_md_path, *, timeout_s, runs, baseline_matches):
    """Gut `skill_md_path`'s description, run `case["prompt"]` `runs` times
    to see whether the route collapses, then restore -- durably and
    under interrupt (SIGINT/SIGTERM).

    Routing is close to a coin flip at one run, so a single gutted run
    proves nothing. This runs `runs` times in the gutted window and again
    after a verified restore, and counts matches rather than trusting one
    result. `baseline_matches` is the selected case's own `matches` from the
    main loop -- no extra runs spent measuring the un-gutted baseline.

    Sequence, exactly:
    1. Read the target's bytes FIRST -- before any sidecar exists, so a read
       failure (unreadable file, process death) never leaves a partial or
       zero-byte backup behind. Write those bytes to a temp file in the
       same directory (`<backup path>.tmp-<pid>`, `O_CREAT|O_EXCL|O_WRONLY`),
       flush, `os.fsync`, close. `os.link` the temp file to
       `<skill_md_path>.skill-trigger-backup` -- atomic, and it raises
       `FileExistsError` if a backup already exists there, mapped to
       `ControlError` naming it, editing nothing (another run is live or
       crashed). `fsync` the containing directory too, best-effort (ignore
       `OSError` from that one call only). The temp file is unlinked in a
       `finally` regardless of outcome, so a crash at any point leaves at
       most the `.tmp-<pid>` file, never a partial sidecar.
    2. Install a SIGTERM handler for the window that raises `_ControlSigterm`
       so the same `finally` performs the restore. SIGINT already raises
       `KeyboardInterrupt`, which needs no handler of its own.
    3. Gut the file, run the case `runs` times, derive `gutted_matches`.
       `collapsed` = `gutted_matches == 0 and baseline_matches >= 1 and no
       gutted run was RUNNER_ERROR or INCOMPLETE` -- all three, so a baseline of 0 never
       "collapses" for free and a broken harness is never read as a
       collapse.
    4. `finally`: restore the sidecar's bytes, byte-compare, and only on a
       verified match remove the sidecar -- otherwise leave it in place
       (it is now the only copy of the original) and report on stderr.
       Restore the previous SIGTERM handler.
    5. If the window was interrupted: `KeyboardInterrupt` propagates
       naturally past this function; for SIGTERM, re-signal ourselves with
       `SIG_DFL` installed so the process actually terminates.
    6. If the restore verified, run the case `runs` times more for
       `reprove_matches`; `reproved = reprove_matches >= 1`. Restore not
       verified -> no reprove runs, `reprove_matches: 0`, `reproved: False`.
    """
    backup_path = skill_md_path + BACKUP_SUFFIX

    # Read FIRST, before any sidecar exists: a read failure here (or the
    # process dying before this line returns) leaves nothing behind at all.
    # Reading AFTER creating the sidecar is exactly the zero-byte-backup
    # defect -- a crash between create and write leaves an empty sidecar
    # that a later run's leftover scan would "restore" over the real file.
    with open(skill_md_path, "rb") as f:
        original_bytes = f.read()

    tmp_path = backup_path + ".tmp-" + str(os.getpid())
    tmp_fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        with os.fdopen(tmp_fd, "wb") as tf:
            tf.write(original_bytes)
            tf.flush()
            os.fsync(tf.fileno())
        try:
            # os.link is atomic: it either creates backup_path pointing at
            # the fully-written temp file's data, or fails outright -- there
            # is no window in which backup_path exists but is incomplete.
            os.link(tmp_path, backup_path)
        except FileExistsError as exc:
            raise ControlError(
                f"{backup_path}: a backup already exists at this exact "
                "path -- another run is live or crashed; editing "
                "nothing") from exc
        backup_dir = os.path.dirname(backup_path) or "."
        try:
            dir_fd = os.open(backup_dir, os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            except OSError:
                pass  # best-effort: the link itself is already durable
            finally:
                os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    result = {
        "ran": True,
        "skill": case["expect"],
        "path_edited": os.path.abspath(skill_md_path),
        "case_index": case_index,
        "runs": runs,
        "baseline_matches": baseline_matches,
        "gutted_matches": 0,
        "reprove_matches": 0,
        "collapsed": False,
        "restored_verified": False,
        "reproved": False,
    }

    previous_handler = signal.getsignal(signal.SIGTERM)
    sigterm_seen = [False]

    def _handle_sigterm(signum, frame):
        sigterm_seen[0] = True
        raise _ControlSigterm()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        try:
            gutted_bytes = gut_description(original_bytes, skill_md_path)
            _atomic_write(skill_md_path, gutted_bytes)
            gutted_runs = [
                run_case(case["prompt"], timeout_s=timeout_s)
                for _ in range(runs)]
            gutted_matches, collapsed = _gutted_collapsed(
                gutted_runs, case["expect"], baseline_matches)
            result["gutted_matches"] = gutted_matches
            result["collapsed"] = collapsed
        except _ControlSigterm:
            pass  # handled below, after the restore in `finally` runs
    finally:
        # A SIGTERM landing DURING the restore must not abort it: the raising
        # handler above would throw out of this `finally` with the file still
        # gutted. From here on a SIGTERM only records itself, and the
        # re-signal below delivers it once the restore has run.
        def _record_sigterm(signum, frame):
            sigterm_seen[0] = True
        signal.signal(signal.SIGTERM, _record_sigterm)
        try:
            _atomic_write(skill_md_path, original_bytes)
            with open(skill_md_path, "rb") as f:
                restored_bytes = f.read()
            if restored_bytes == original_bytes:
                result["restored_verified"] = True
                os.remove(backup_path)
            else:
                result["restored_verified"] = False
                sys.stderr.write(
                    f"skill-trigger: restore did not verify byte-for-byte, "
                    f"backup left at {backup_path}\n")
        except OSError as exc:
            result["restored_verified"] = False
            sys.stderr.write(
                f"skill-trigger: restore failed ({exc}), backup left at "
                f"{backup_path}\n")
        signal.signal(signal.SIGTERM, previous_handler)

    if sigterm_seen[0]:
        # Re-signal ourselves with the default handler installed so the
        # process actually terminates by SIGTERM, rather than merely having
        # raised-and-caught a Python exception that looks like nothing
        # happened to anything watching the process from outside.
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)

    if result["restored_verified"]:
        reprove_runs = [
            run_case(case["prompt"], timeout_s=timeout_s)
            for _ in range(runs)]
        reprove_matches = sum(
            1 for r in reprove_runs
            if r.status == "OK"
            and classify(r.observed, case["expect"]) == MATCH)
        result["reprove_matches"] = reprove_matches
        result["reproved"] = reprove_matches >= 1

    return result


def run_harness_control(*, timeout_s, runs):
    """Run `HARNESS_PROMPT` (the build skill's positive trigger prompt)
    `runs` times through `run_case` and report how many produced a Skill
    call. Does NOT compare `observed` to any particular name -- which skill
    fires depends on the sibling set; the harness control only proves the
    instrument can see a Skill call at all (a check that cannot pass on a
    working setup proves nothing: if this is false, every other result in
    the run is uninterpretable).

    One run is close to a coin flip, so `runs` repeats it and
    reports a count rather than trusting one result.
    """
    harness_runs = [
        run_case(HARNESS_PROMPT, timeout_s=timeout_s) for _ in range(runs)]
    skill_calls = sum(
        1 for r in harness_runs if r.status == "OK" and r.observed is not None)

    order = []
    counts = {}
    for r in harness_runs:
        if r.status == "OK" and r.observed is not None:
            if r.observed not in counts:
                counts[r.observed] = 0
                order.append(r.observed)
            counts[r.observed] += 1
    observed = max(order, key=lambda k: counts[k]) if order else None

    return {
        "ran": True,
        "runs": runs,
        "skill_calls": skill_calls,
        "observed": observed,
        "produced_skill_call": skill_calls >= 1,
    }


def compute_verdict(controls, cases, *, leftover_backup=False):
    """The complete verdict truth table, evaluated top to bottom, first
    match wins. Returns `(verdict, exit_code)`.

    | # | Condition                                                | verdict      | exit |
    |---|-----------------------------------------------------------|--------------|------|
    | 1 | A leftover backup was found at startup                     | INCONCLUSIVE | 3    |
    | 2 | `positive` non-null and `positive["restored_verified"]` is False | INCONCLUSIVE | 4    |
    | 3 | Either control did not run (None)                          | INCONCLUSIVE | 3    |
    | 4 | `positive["collapsed"]` is False                           | INCONCLUSIVE | 3    |
    | 5 | `positive["reproved"]` is False                            | INCONCLUSIVE | 3    |
    | 6 | `harness["produced_skill_call"]` is False                  | INCONCLUSIVE | 3    |
    | 7 | Any case has `state == RUNNER_ERROR` or `INCOMPLETE`       | INCONCLUSIVE | 3    |
    | 8 | Both controls good, any case COLLISION, NO_SKILL or FORBIDDEN | FAIL      | 1    |
    | 9 | Both controls good, every case MATCH                       | PASS         | 0    |

    Row 2 sits above the null-control row on purpose: with the harness
    control absent and a restore failed, the old order returned exit 3 and
    hid a disarmed real skill behind an ordinary inconclusive. Row 9 is
    load-bearing and must be reachable -- collapsing PASS into INCONCLUSIVE
    would make a broken eval set indistinguishable from bad descriptions,
    the exact failure this tool exists to prevent, one level up.
    """
    if leftover_backup:
        return "INCONCLUSIVE", 3

    positive = controls.get("positive")
    harness = controls.get("harness")

    if positive is not None and positive["restored_verified"] is False:
        return "INCONCLUSIVE", 4

    if positive is None or harness is None:
        return "INCONCLUSIVE", 3

    if positive["collapsed"] is False:
        return "INCONCLUSIVE", 3

    if positive["reproved"] is False:
        return "INCONCLUSIVE", 3

    if harness["produced_skill_call"] is False:
        return "INCONCLUSIVE", 3

    if any(c["state"] in (RUNNER_ERROR, INCOMPLETE) for c in cases):
        return "INCONCLUSIVE", 3

    if any(c["state"] in (COLLISION, NO_SKILL, FORBIDDEN) for c in cases):
        return "FAIL", 1

    return "PASS", 0
