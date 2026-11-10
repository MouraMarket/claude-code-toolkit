#!/usr/bin/env python3
"""Re-verify a Claude Code version against the memory design's harness
assumptions, per version, with an advisory notice while it is unverified.

This file holds the tool's logic: reading a transcript's version, the
on-disk state, the pure decision of whether a hook should speak, the probe
fixture, the evaluator, and the one subprocess runner.

Two files, one writer each
---------------------------
`harness-check-results.json` (`{"versions": {...}}`) is written only by the
runner (`claude-harness-check`, no flag); `harness-check-noticed.json`
(`{"noticed": {...}}`) is written only by the hook (`--hook`). Splitting them
is what keeps a hook that loaded a stale snapshot of `results` from ever
clobbering a runner's write: the hook physically cannot save over
`versions[...]`, because it never opens that file to write. A single shared
file would not have this property — see
`TestTwoWritersOneFileEach` in the tests, which demonstrates the race this
design avoids rather than merely asserting the two paths differ.

The residual race this does NOT close: two hooks racing to save `noticed` at
the same instant can lose one entry (last `os.replace` wins), costing one
repeat notice for whichever session lost. That is accepted — a rare, cheap,
self-correcting cost — because closing it would need a lock shared across
sessions for a file only advisory-notice dedupe reads.

Why the tail read
------------------
`transcript_version` reads at most the last `TRANSCRIPT_TAIL_BYTES` bytes of
the transcript rather than the whole file. A long-running session's
transcript grows without bound, and every `PostToolBatch`/`UserPromptSubmit`
hook call would otherwise re-read the whole thing just to find one field
near the end. Reading from the tail can start mid-line; a truncated first
line simply fails `json.loads` and is skipped, which is correct because the
scan proceeds from the END anyway and returns on the first hit.
"""

import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------- transcript --

# Bytes read from the END of a transcript file before scanning for `version`.
# Large enough to comfortably span the tail of an ordinary tool-batch turn;
# see the module docstring for why this is a tail read rather than a full one.
TRANSCRIPT_TAIL_BYTES = 262144


def transcript_version(path: Optional[str]) -> Optional[str]:
    """The most recent `version` field in a Claude Code transcript.

    Reads at most the last `TRANSCRIPT_TAIL_BYTES` bytes, splits into lines,
    and scans from the end for the first line that parses as JSON and
    carries a non-empty string `version`. `None` when `path` is falsy,
    missing, unreadable, or no such line exists. Never raises — this is
    called from inside a best-effort hook.
    """
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - TRANSCRIPT_TAIL_BYTES))
            chunk = handle.read()
    except Exception:
        return None
    text = chunk.decode("utf-8", "replace")
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        version = record.get("version")
        if isinstance(version, str) and version:
            return version
    return None


# ------------------------------------------------------- load_json/save_json --

def load_json(path: Optional[str], empty: Any) -> Any:
    """The parsed JSON object at `path`, or `empty` when it cannot be read.

    Missing file, unreadable file, malformed JSON, and a top-level value
    that is not a dict all collapse to `empty` — this is a state cache, and
    every one of those cases is best handled the same way a fresh install
    would be: as though nothing had been recorded yet. Never raises.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return empty
    return data if isinstance(data, dict) else empty


def save_json(path: str, obj: Dict[str, Any]) -> bool:
    """Write `obj` to `path` atomically. `False` on any failure, never raises.

    Written to a unique temp file in the SAME directory, then `os.replace`d
    into place — same directory so the replace is on one filesystem and
    therefore atomic, and a unique name (`tempfile.mkstemp`) so two writers
    of the same path never collide on the temp file itself, only on the
    final rename (last one wins, which is the accepted residual race
    documented at the top of this file).
    """
    directory = os.path.dirname(path) or "."
    tmp = None
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".harness-check-tmp-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(obj, handle)
        os.replace(tmp, path)
        return True
    except Exception:
        if tmp:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return False


# ------------------------------------------------------------------- results --
# Written only by the runner. The hook reads this file and never saves it.

# Bumped when the meaning of a stored result changes. Rev 2: an H5(c) that
# could not be checked is recorded `partial`; before, it was stored as `pass`.
# Rev 3: H6 (the 200-line index cut) joined the probe set; a rev-2 pass never
# checked it.
RECORD_REV = 3


def record_result(results: Dict[str, Any], version: str, status: str,
                  failures: List[str], now_iso: str,
                  unverified: Optional[List[str]] = None) -> Dict[str, Any]:
    """Set `results["versions"][version]` to this check's outcome.

    `unverified` names the probe ids a `partial` result could not check; it
    is stored only when non-empty. Every record carries `rev: RECORD_REV`.

    Mutates and returns `results` — the same shape `load_json` hands back,
    so a caller does `record_result(load_json(path, {}), ...)` and saves the
    result.
    """
    if not isinstance(results, dict):
        results = {}
    versions = results.get("versions")
    if not isinstance(versions, dict):
        versions = {}
        results["versions"] = versions
    record = {"result": status, "checked_at": now_iso,
              "failures": list(failures or []), "rev": RECORD_REV}
    if unverified:
        record["unverified"] = list(unverified)
    versions[version] = record
    return results


def _is_current_rev(record: Dict[str, Any]) -> bool:
    rev = record.get("rev")
    return isinstance(rev, int) and not isinstance(rev, bool) and rev >= RECORD_REV


def _last_verified_version(versions: Dict[str, Any]) -> str:
    """The version with the latest `checked_at` among `result == "pass"`
    entries, or the literal `"none"` when there are none.

    Only a full pass counts: `partial` was not fully verified, and a pass
    with no `rev` (or `rev < RECORD_REV`) was written before an unchecked
    H5(c) was recorded as partial, so it may hide one.

    ISO-8601 timestamps sort correctly as plain strings, so a lexical max is
    enough here without parsing dates.
    """
    best_version = None
    best_checked_at = None
    for candidate, record in (versions or {}).items():
        if not isinstance(record, dict) or record.get("result") != "pass":
            continue
        if not _is_current_rev(record):
            continue
        checked_at = record.get("checked_at")
        if not isinstance(checked_at, str) or not checked_at:
            continue
        if best_checked_at is None or checked_at > best_checked_at:
            best_checked_at = checked_at
            best_version = candidate
    return best_version if best_version is not None else "none"


# ------------------------------------------------------------------- noticed --
# Written only by the hook. The runner never saves this file.

# The most recent sessions kept in `noticed`. Older entries are dropped so
# the file cannot grow without bound across a machine's lifetime; 200 is
# comfortably more sessions than any dedupe window needs to span.
NOTICED_CAP = 200


def mark_noticed(doc: Dict[str, Any], session_id: str, version: str) -> Dict[str, Any]:
    """Record that `session_id` has been notified about `version`.

    `doc` is the WHOLE noticed document, `{"noticed": {...}}` — not just the
    inner map — so this can create `doc["noticed"]` when it is absent. A
    re-insert of an already-present `session_id` moves it to the end of
    insertion order, so the 200-cap below evicts by recency of NOTICE, not
    by the session id's first appearance.
    """
    if not isinstance(doc, dict):
        doc = {}
    noticed = doc.get("noticed")
    if not isinstance(noticed, dict):
        noticed = {}
    noticed.pop(session_id, None)          # move-to-end on re-insert
    noticed[session_id] = version
    if len(noticed) > NOTICED_CAP:
        overflow = len(noticed) - NOTICED_CAP
        for key in list(noticed.keys())[:overflow]:
            del noticed[key]
    doc["noticed"] = noticed
    return doc


# --------------------------------------------------------------- notice text --

_NOTICE_UNVERIFIED = (
    "[harness-check] Claude Code {v} is running and has not been verified "
    "against the memory design's {n} harness assumptions (last verified: "
    "{last}). Run claude-harness-check (3 Haiku runs, about a minute)."
)

_NOTICE_FAILED = (
    "[harness-check] Claude Code {v} FAILED harness check {failures} on "
    "{date}. memory-lint and context-budget numbers may be wrong until "
    "fixed. Run claude-harness-check --status for detail, and re-run "
    "claude-harness-check after fixing."
)


def decide_notice(results: Dict[str, Any], noticed: Dict[str, Any],
                  session_id: str, version: Optional[str], enabled: bool,
                  agent_id: Optional[str]) -> Optional[str]:
    """The notice text to print, or `None` — PURE, mutates nothing.

    Silent, in this order: not `enabled`; `agent_id` truthy (a subagent's
    batch is not the place for a session-facing notice); `version` is
    `None` (the transcript has not appeared yet, or is unreadable); the
    version is recorded `pass` or `partial` at the current record rev (a
    legacy record without `rev` may hide an unchecked H5(c), so it is not
    trusted quiet); this session has already been noticed for
    this exact version. Otherwise it returns the unverified text, or — when
    the version is recorded `fail` — the failed text, which re-fires every
    session rather than once, because a known-broken version should stay
    visible until it is fixed.
    """
    if not enabled:
        return None
    if agent_id:
        return None
    if version is None:
        return None

    versions = (results or {}).get("versions")
    versions = versions if isinstance(versions, dict) else {}
    record = versions.get(version)

    # Quiet only for a current-rev pass or partial. A legacy pass (no rev)
    # speaks the ordinary unverified text until one re-run; a legacy fail
    # still falls through to the FAILED text below.
    if (isinstance(record, dict) and record.get("result") in ("pass", "partial")
            and _is_current_rev(record)):
        return None

    noticed_map = (noticed or {}).get("noticed")
    noticed_map = noticed_map if isinstance(noticed_map, dict) else {}
    if noticed_map.get(session_id) == version:
        return None

    if isinstance(record, dict) and record.get("result") == "fail":
        failures = record.get("failures")
        failures = failures if isinstance(failures, list) else []
        checked_at = record.get("checked_at")
        checked_at = checked_at if isinstance(checked_at, str) else ""
        return _NOTICE_FAILED.format(v=version, failures=", ".join(failures),
                                     date=checked_at[:10])

    return _NOTICE_UNVERIFIED.format(v=version, n=len(_PROBE_IDS),
                                     last=_last_verified_version(versions))


# ------------------------------------------------------------------ fixture --
# `build_fixture` writes a fresh, tokenised probe fixture to disk --
# the files the probes read, and the two hooks the probe runs' Claude Code
# invocations run against -- and `projects_dir_for` answers where the
# harness itself would put that fixture's auto-memory projects directory.
#
# The fixture is deliberately NOT a git repository and sits outside any real
# CLAUDE.md ancestor (a fresh `tempfile.mkdtemp`), so nothing it reads or
# writes can be contaminated by, or contaminate, an ambient workspace.


class FixtureError(Exception):
    """Raised when a built `mem/MEMORY.md` fails its own offset/line-count
    assertions -- see `_build_memory_md`. Never raised for any other reason;
    every other write in `build_fixture` is plain local filesystem I/O with
    nothing to fail on a healthy machine.
    """


@dataclass
class Fixture:
    """One probe fixture. `root` and `tokens` are set at build time; the
    other three are left at their defaults here and filled in by the runner
    once it has something to record -- `loaded`/`blocked` from the
    per-run hook logs, `preexisting_projects_dir` from the ownership check
    that decides whether cleanup may remove the projects directory.
    """
    root: str
    tokens: Dict[str, str]
    loaded: List[Any] = field(default_factory=list)
    blocked: List[Any] = field(default_factory=list)
    preexisting_projects_dir: bool = False


# The twelve token keys, in the order the Files table in the plan lists their
# homes: CLAUDE.md, CLAUDE.history.md, the two rule files, the three
# mem/MEMORY.md tokens, mem/topics/t.md, mem/history/MEMORY.history.md, and
# the three mem-lines/MEMORY.md tokens.
_TOKEN_KEYS = ("CTRL", "HIST", "RULE_ON", "RULE_SCOPED", "MEM",
              "IDX_EARLY", "IDX_LATE", "TOPIC", "MEMHIST",
              "LINE_HEAD", "LINE_EARLY", "LINE_LATE")

# A fixed built-in word list for tokens -- deliberately not `random.choice`
# over an external corpus, so a fixture build never depends on anything
# outside this file. >=40 words, all uppercase, no two identical.
_FIXTURE_WORDS = (
    "ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL",
    "INDIA", "JULIET", "KILO", "LIMA", "MIKE", "NOVEMBER", "OSCAR", "PAPA",
    "QUEBEC", "ROMEO", "SIERRA", "TANGO", "UNIFORM", "VICTOR", "WHISKEY",
    "XRAY", "YANKEE", "ZULU", "AMBER", "BIRCH", "CEDAR", "DRIFT", "EMBER",
    "FLINT", "GRANITE", "HARBOR", "IVORY", "JASPER", "KELP", "LANTERN",
    "MAPLE", "NORTH", "OPAL", "PEBBLE", "QUARTZ", "RIDGE", "SUMMIT", "TIDE",
    "UMBER", "VALLEY", "WILLOW", "YIELD",
)


def _fresh_token(rng, used):
    """One `WORD-WORD-NNN` string not already in `used`, and adds it."""
    while True:
        word_a, word_b = rng.sample(_FIXTURE_WORDS, 2)
        number = rng.randint(100, 999)
        token = "{0}-{1}-{2}".format(word_a, word_b, number)
        if token not in used:
            used.add(token)
            return token


def _build_tokens(rng):
    """Fresh, mutually distinct tokens keyed by `_TOKEN_KEYS`."""
    used = set()
    return {key: _fresh_token(rng, used) for key in _TOKEN_KEYS}


def _write(root, rel_path, content):
    """Write `content` as UTF-8 to `root/rel_path`, creating parents."""
    path = os.path.join(root, *rel_path.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


# ---- mem/MEMORY.md: straddling the 25,000-char/25,000-byte truncation bracket --
#
# The harness cuts its own MEMORY.md at 200 lines, THEN 25,000 -- but
# whether that 25,000 counts characters or bytes is inferred, not measured.
# H1 settles it live: if the cut is really at 25,000 UTF-8 bytes,
# an IDX_EARLY row placed just under 25,000 *characters* would already have
# been cut, and H1 must see that as a FAIL rather than a false PASS. So the
# fixture is built to straddle both readings at once -- IDX_EARLY ends
# early enough in characters to survive a char-based cut, but late enough in
# bytes (via multibyte em dashes) that it would NOT survive a byte-based
# cut at the same nominal number. IDX_LATE sits past either reading, so it
# must never appear no matter which cut is real.

# Targets sit comfortably inside the required bounds (see `_build_memory_md`)
# so a different word list or token length still clears them with margin --
# the assertions below are the contract, these are just how the recipe hits them.
_MEMORY_EARLY_TARGET_CHARS = 24500
_MEMORY_LATE_TARGET_CHARS = 25800

_TOKEN_SHAPE_RE = re.compile(r"[A-Z]+-[A-Z]+-\d{3}")


def _filler_row(n):
    """One `mem/MEMORY.md` filler row: long, and carrying several em dashes
    so it is meaningfully wider in UTF-8 bytes than in characters -- the
    multibyte margin the byte-floor assertion below depends on. A test
    monkeypatches this module-level function to an ASCII-only row to prove
    that floor is enforced rather than incidental.
    """
    return ("- Filler entry {0} \u2014 padding \u2014 padding \u2014 padding "
            "\u2014 padding \u2014 padding \u2014 padding \u2014 padding for "
            "the harness-check fixture body row, existing only to occupy "
            "space near the truncation boundary.\n").format(n)


def _idx_row(label, token):
    return ("- [{0}](fixture.md) \u2014 {1} marks the {2} index cut boundary "
            "for the harness-check fixture.\n").format(label, token, label.lower())


def _build_memory_md(tokens):
    """The full `mem/MEMORY.md` text: the `## Always` header carrying `MEM`,
    filler rows, the `IDX_EARLY` row, more filler, the `IDX_LATE` row, then
    one trailing filler row.

    Raises `FixtureError` unless all four assertions hold -- these, not the
    filler recipe above, are the contract:
    - the `IDX_EARLY` row ends at <= 24,900 characters
    - and at >= 25,400 UTF-8 bytes at that same character offset
    - the `IDX_LATE` row starts at >= 25,300 characters
    - the whole file is <= 180 lines (`text.count("\\n")`)
    No filler row contains a token-shaped substring (`[A-Z]+-[A-Z]+-\\d{3}`).
    """
    text = "## Always\n\n- [Fixture](fixture.md) \u2014 memory token {0}\n".format(
        tokens["MEM"])

    n = 0
    idx_early_row = _idx_row("Idx Early", tokens["IDX_EARLY"])
    while len(text) + len(idx_early_row) <= _MEMORY_EARLY_TARGET_CHARS:
        n += 1
        text += _filler_row(n)
    text += idx_early_row

    early_end_chars = len(text)
    early_end_bytes = len(text[:early_end_chars].encode("utf-8"))
    if early_end_chars > 24900:
        raise FixtureError(
            "mem/MEMORY.md: IDX_EARLY row ends at {0} characters, over the "
            "24,900 ceiling".format(early_end_chars))
    if early_end_bytes < 25400:
        raise FixtureError(
            "mem/MEMORY.md: IDX_EARLY row ends at {0} UTF-8 bytes, under "
            "the 25,400 floor -- filler is not carrying enough multibyte "
            "characters".format(early_end_bytes))

    while len(text) < _MEMORY_LATE_TARGET_CHARS:
        n += 1
        text += _filler_row(n)

    late_start_chars = len(text)
    if late_start_chars < 25300:
        raise FixtureError(
            "mem/MEMORY.md: IDX_LATE row starts at {0} characters, under "
            "the 25,300 floor".format(late_start_chars))

    text += _idx_row("Idx Late", tokens["IDX_LATE"])
    n += 1
    text += _filler_row(n)  # one trailing filler row, past IDX_LATE

    line_count = text.count("\n")
    if line_count > 180:
        raise FixtureError(
            "mem/MEMORY.md: {0} lines, over the 180-line cap".format(line_count))

    for line in text.splitlines():
        if "Filler entry" in line and _TOKEN_SHAPE_RE.search(line):
            raise FixtureError(
                "mem/MEMORY.md: a filler row matches the token shape: "
                "{0!r}".format(line))

    return text


# ---- mem-lines/MEMORY.md: the 200-line cut, on a fixture of its own -------
#
# The character cut and the line cut cannot be probed on one file. The
# `mem/MEMORY.md` above is built so its 25,000-char boundary falls inside its
# first 180 lines -- the line cut never bites there. So H6 gets a second file
# that is long in LINES and short in bytes: LINE_EARLY (line 195) must
# survive the harness's 200-line cut and LINE_LATE (line 206) must not.

_LINES_HEAD_LINE = 1
_LINES_EARLY_LINE = 195
_LINES_LATE_LINE = 206
_LINES_TOTAL = 220


def _line_filler_row(n):
    """One short ASCII `mem-lines/MEMORY.md` filler row, no newline. A test
    monkeypatches this to a long row to prove the byte cap is enforced."""
    return "- Line filler {0}.".format(n)


def _build_lines_memory_md(tokens):
    """The full `mem-lines/MEMORY.md` text: 220 rows, `LINE_HEAD` on line 1,
    `LINE_EARLY` on line 195, `LINE_LATE` on line 206, short ASCII filler
    elsewhere, ending in a newline (1-based lines over `text.split("\\n")`).

    Raises `FixtureError` unless all of these hold -- they, not the recipe,
    are the contract:
    - the three tokens sit on lines 1, 195 and 206
    - the file has >= 215 lines (the native count is over 200)
    - it is <= 20,000 UTF-8 bytes, so a 25,000-char or 25,000-byte cut
      ALONE would keep `LINE_LATE`: only the 200-line cut can drop it.
      That is the independence property H6 depends on.
    - the text is ASCII and no filler row matches `_TOKEN_SHAPE_RE`
    """
    special = {
        _LINES_HEAD_LINE: "- Lines head row: {0}".format(tokens["LINE_HEAD"]),
        _LINES_EARLY_LINE: "- Lines early row: {0}".format(tokens["LINE_EARLY"]),
        _LINES_LATE_LINE: "- Lines late row: {0}".format(tokens["LINE_LATE"]),
    }
    rows = [special.get(number) or _line_filler_row(number)
            for number in range(1, _LINES_TOTAL + 1)]
    text = "\n".join(rows) + "\n"

    lines = text.split("\n")
    for number, key in ((_LINES_HEAD_LINE, "LINE_HEAD"),
                        (_LINES_EARLY_LINE, "LINE_EARLY"),
                        (_LINES_LATE_LINE, "LINE_LATE")):
        if tokens[key] not in lines[number - 1]:
            raise FixtureError(
                "mem-lines/MEMORY.md: {0} is not on line {1}".format(key, number))
    line_count = text.strip().count("\n") + 1
    if line_count < 215:
        raise FixtureError(
            "mem-lines/MEMORY.md: {0} lines, under the 215-line floor".format(
                line_count))
    byte_count = len(text.encode("utf-8"))
    if byte_count > 20000:
        raise FixtureError(
            "mem-lines/MEMORY.md: {0} UTF-8 bytes, over the 20,000 cap -- a "
            "character or byte cut alone could drop LINE_LATE".format(byte_count))
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        raise FixtureError("mem-lines/MEMORY.md: not ASCII")
    for number, line in enumerate(lines, start=1):
        if number not in special and _TOKEN_SHAPE_RE.search(line):
            raise FixtureError(
                "mem-lines/MEMORY.md: a filler row matches the token shape: "
                "{0!r}".format(line))

    return text


# ---- the two containment hooks, baked to files at build time --------------
#
# Standard library only, and each reads its payload from stdin -- these run
# as real subprocesses inside the probed Claude Code invocations, not as
# Python objects this process can hand anything to directly.

def _log_loaded_script(root):
    """`hooks/log_loaded.py`'s text: on `InstructionsLoaded`, append the
    payload's `file_path`/`load_reason` to `<root>/loaded.jsonl`. `root` is
    baked in as a literal at build time via `repr`, so the script needs
    nothing from its environment to find its own fixture.
    """
    return (
        "#!/usr/bin/env python3\n"
        "# Fixture hook (InstructionsLoaded): appends file_path/load_reason\n"
        "# to loaded.jsonl under the fixture root. Stdlib only; payload on stdin.\n"
        "import json\n"
        "import sys\n"
        "\n"
        "for _stream in (sys.stdout, sys.stderr):\n"
        "    try:\n"
        "        _stream.reconfigure(encoding=\"utf-8\", errors=_stream.errors)\n"
        "    except (AttributeError, ValueError):\n"
        "        pass\n"
        "\n"
        "ROOT = " + repr(root) + "\n"
        "\n"
        "try:\n"
        "    payload = json.loads(sys.stdin.buffer.read())\n"
        "except Exception:\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    with open(ROOT + \"/loaded.jsonl\", \"a\", encoding=\"utf-8\") as handle:\n"
        "        handle.write(json.dumps({\"file_path\": payload.get(\"file_path\"),\n"
        "                                  \"load_reason\": payload.get(\"load_reason\")}) + \"\\n\")\n"
        "except Exception:\n"
        "    pass\n"
        "sys.exit(0)\n"
    )


def _contain_script(root):
    """`hooks/contain.py`'s text: exit 0 iff the tool is `Read` of the
    fixture's own `src/x.py`; otherwise log `tool_name`/`tool_input` to
    `<root>/blocked.jsonl`, print a reason to stderr, and exit 2. `root` and
    the allowed path are baked in as literals at build time via `repr`.
    """
    allowed = os.path.join(root, "src", "x.py")
    return (
        "#!/usr/bin/env python3\n"
        "# Fixture hook (PreToolUse *): allows only Read of the fixture's own\n"
        "# src/x.py; blocks and logs everything else. Stdlib only; payload on stdin.\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "\n"
        "for _stream in (sys.stdout, sys.stderr):\n"
        "    try:\n"
        "        _stream.reconfigure(encoding=\"utf-8\", errors=_stream.errors)\n"
        "    except (AttributeError, ValueError):\n"
        "        pass\n"
        "\n"
        "ROOT = " + repr(root) + "\n"
        "ALLOWED = " + repr(allowed) + "\n"
        "\n"
        "try:\n"
        "    payload = json.loads(sys.stdin.buffer.read())\n"
        "except Exception:\n"
        "    sys.exit(0)\n"
        "tool_name = payload.get(\"tool_name\")\n"
        "tool_input = payload.get(\"tool_input\") or {}\n"
        "file_path = tool_input.get(\"file_path\")\n"
        "ok = False\n"
        "if tool_name == \"Read\" and file_path:\n"
        "    try:\n"
        "        ok = os.path.realpath(file_path) == ALLOWED\n"
        "    except Exception:\n"
        "        ok = False\n"
        "if ok:\n"
        "    sys.exit(0)\n"
        "try:\n"
        "    with open(ROOT + \"/blocked.jsonl\", \"a\", encoding=\"utf-8\") as handle:\n"
        "        handle.write(json.dumps({\"tool_name\": tool_name,\n"
        "                                  \"tool_input\": tool_input}) + \"\\n\")\n"
        "except Exception:\n"
        "    pass\n"
        "sys.stderr.write(\"harness-check fixture: blocked \" + str(tool_name) + \"\\n\")\n"
        "sys.exit(2)\n"
    )


def _settings_json(root):
    """The fixture's `.claude/settings.json`: `InstructionsLoaded` ->
    `log_loaded.py`, `PreToolUse "*"` -> `contain.py`, both invoked with
    `sys.executable` (quoted) rather than a bare `python3` so the child runs
    the same interpreter as this process.
    """
    python_exe = sys.executable or "python3"

    def command(script_name):
        script_path = os.path.join(root, "hooks", script_name)
        return "\"{0}\" \"{1}\"".format(python_exe, script_path)

    return {
        "hooks": {
            "InstructionsLoaded": [
                {"hooks": [{"type": "command",
                           "command": command("log_loaded.py"), "timeout": 5}]}
            ],
            "PreToolUse": [
                {"matcher": "*",
                 "hooks": [{"type": "command",
                           "command": command("contain.py"), "timeout": 5}]}
            ],
        }
    }


def build_fixture(parent_dir, rng):
    """Build one fresh probe fixture under `parent_dir` and return the
    `Fixture` describing it.

    `root` is the REALPATH of a fresh `tempfile.mkdtemp(prefix="harness-check-",
    dir=parent_dir)` -- on macOS `/var/...` resolves to `/private/var/...`,
    and the harness slugs the realpath, so a fixture keyed on the raw
    mkdtemp path would compute a `projects_dir_for` no run ever writes to.

    Writes `CLAUDE.md`, `CLAUDE.history.md`, the two `.claude/rules/` files,
    `src/x.py`, `mem/MEMORY.md` (see `_build_memory_md`), `mem-lines/MEMORY.md`
    (see `_build_lines_memory_md`), `mem/topics/t.md`,
    `mem/history/MEMORY.history.md`, the two hook scripts, and the
    `.claude/settings.json` that registers them. Raises `FixtureError` only
    from `_build_memory_md` and `_build_lines_memory_md`; every other write is plain local filesystem I/O.
    """
    raw_root = tempfile.mkdtemp(prefix="harness-check-", dir=parent_dir)
    root = os.path.realpath(raw_root)

    tokens = _build_tokens(rng)
    memory_md = _build_memory_md(tokens)
    lines_memory_md = _build_lines_memory_md(tokens)

    _write(root, "CLAUDE.md", "Control token: {0}".format(tokens["CTRL"]))
    _write(root, "CLAUDE.history.md", "Archive token: {0}".format(tokens["HIST"]))
    _write(root, ".claude/rules/always.md",
          "Always-on rule token: {0}".format(tokens["RULE_ON"]))
    _write(root, ".claude/rules/scoped.md",
          "---\npaths:\n  - \"src/**\"\n---\nScoped rule token: {0}".format(
              tokens["RULE_SCOPED"]))
    _write(root, "src/x.py", "x = 1")
    _write(root, "mem/MEMORY.md", memory_md)
    _write(root, "mem-lines/MEMORY.md", lines_memory_md)
    _write(root, "mem/topics/t.md", "Topic token: {0}".format(tokens["TOPIC"]))
    _write(root, "mem/history/MEMORY.history.md",
          "Memory archive token: {0}".format(tokens["MEMHIST"]))
    _write(root, "hooks/log_loaded.py", _log_loaded_script(root))
    _write(root, "hooks/contain.py", _contain_script(root))
    for name in ("log_loaded.py", "contain.py"):
        os.chmod(os.path.join(root, "hooks", name), 0o755)
    _write(root, ".claude/settings.json", json.dumps(_settings_json(root), indent=2))

    return Fixture(root=root, tokens=tokens)


def _lib_dir():
    """`lib/`'s path, resolved two directories above this file's own
    directory via `realpath(__file__)` -- toolkit `CLAUDE.md`'s hard rule,
    so this works identically whether `harness_lib` is imported directly (as
    the tests do) or loaded through the `~/.local/bin` symlink.
    """
    tool_dir = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(os.path.dirname(os.path.dirname(tool_dir)), "lib")


def claude_home():
    """Where Claude Code keeps `settings.json` and `projects/`:
    `$CLAUDE_CONFIG_DIR` when set, else `~/.claude`, read at call time.

    The rest of the toolkit ignores `CLAUDE_CONFIG_DIR`, but this tool
    cannot: every child `claude -p` inherits it and writes its transcript
    under it, so anything read back from a child must resolve the same way
    or the runner looks in one place while the child writes in another
    (Merge Review MR2).
    """
    lib_dir = _lib_dir()
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import claude_home as _claude_home  # noqa: E402

    return _claude_home.claude_home()


def _projects_root():
    return (os.environ.get("HARNESS_CHECK_PROJECTS_ROOT")
            or os.path.join(claude_home(), "projects"))


def projects_dir_for(root):
    """`<projects root>/<slug>` -- the directory the harness's own
    `autoMemoryDirectory` resolution would use for a project at `root`.

    `<projects root>` is `HARNESS_CHECK_PROJECTS_ROOT`, read from the
    environment at call time (default `claude_home()/projects`);
    `<slug>` is `auto_memory.project_slug(root)`, which realpaths `root`
    itself -- so passing a raw, non-realpath'd path here still slugs
    correctly.
    """
    lib_dir = _lib_dir()
    if lib_dir not in sys.path:
        sys.path.insert(0, lib_dir)
    import auto_memory  # noqa: E402  (path is set up immediately above)

    return os.path.join(_projects_root(), auto_memory.project_slug(root))


# -------------------------------------------------------------------- evaluate --
# `evaluate` is the pure judge: one `Fixture` plus four run records
# (R1, R2, the calling session, R3) in, `(version, [ProbeResult, ...])` out --
# always all six of H1..H6, always in that order. It reads none of the
# fixture's files and runs no subprocess of its own; `projects_dir_for` is
# called only to NAME a directory inside one void reason, never to look at
# one on disk.
#
# Void rules run first and are exhaustive. A probe that a rule applies to
# is marked VOID with THAT rule's reason; a probe already voided keeps its
# FIRST reason -- a later rule never overwrites it. Verdict logic (the
# H1..H6 functions below) only ever runs for a probe no void rule touched.

ProbeResult = namedtuple("ProbeResult", ["id", "status", "reason"])

_PROBE_IDS = ("H1", "H2", "H3", "H4", "H5", "H6")


def _entries_by_type(instructions, type_name):
    """The entries of an `instructions` `files` list (or `None`) whose
    `type` is `type_name`. Never raises on a malformed shape -- `None` and
    anything that is not a list of dicts collapse to no entries.
    """
    return [e for e in (instructions or [])
           if isinstance(e, dict) and e.get("type") == type_name]


def automem(instructions):
    """The `AutoMem`-typed entries of an `instructions` list -- H1, H2 and
    H5's `automem(instr)`.
    """
    return _entries_by_type(instructions, "AutoMem")


def _unusable_reason(run, fx):
    """`None` if `run` (R1, R2 or R3) is usable evidence; otherwise a short
    reason naming which condition failed. Shared by all three runs'
    unusability, which the plan defines identically -- including R2's own positive
    control (a `Project` instructions entry containing `CTRL`), which
    proves R2's capture worked even though R2 is itself the negative
    control for auto-memory.
    """
    if not isinstance(run, dict):
        return "run record missing"
    if run.get("exit") != 0:
        return "exit {0}".format(run.get("exit"))
    init = run.get("init")
    if not isinstance(init, dict):
        return "init missing"
    if not isinstance(run.get("result"), dict):
        return "result missing"
    model = init.get("model")
    if not isinstance(model, str) or not model.startswith("claude-haiku"):
        return "init.model is not a Haiku model ({0!r})".format(model)
    if run.get("transcript") is None:
        return "transcript missing"
    ctrl = fx.tokens.get("CTRL")
    projects = _entries_by_type(run.get("instructions"), "Project")
    if not any(isinstance(e.get("content"), str) and ctrl in e["content"]
              for e in projects):
        return "no Project instructions entry containing CTRL"
    return None


def _h3_void_reason(fx, r1):
    """`None` if H3's own evidence is intact; otherwise why it is not --
    the containment hook logged a block, or R1's `tool_uses` never show
    the one `Read` of `src/x.py` the prompt asked for. `r1` may not be a
    dict at all (R1 unusable, e.g. missing entirely) -- `evaluate` still
    calls this unconditionally, since a probe already voided by the R1
    unusability check keeps its FIRST reason regardless of what this
    returns, so this must not raise on that shape either.
    """
    if not isinstance(r1, dict):
        return "R1 record missing"
    if fx.blocked:
        return "the containment hook blocked a tool call in R1"
    target = os.path.realpath(os.path.join(fx.root, "src", "x.py"))
    for use in (r1.get("tool_uses") or []):
        if not isinstance(use, dict) or use.get("name") != "Read":
            continue
        file_path = (use.get("input") or {}).get("file_path")
        if file_path and os.path.realpath(file_path) == target:
            return None
    return "R1's tool_uses carry no Read of src/x.py"


def _h1(fx, r1):
    idx_early = fx.tokens["IDX_EARLY"]
    idx_late = fx.tokens["IDX_LATE"]
    transcript = r1.get("transcript") or ""
    contents = [e.get("content") for e in automem(r1.get("instructions"))
               if isinstance(e.get("content"), str)]
    early_present = any(idx_early in c for c in contents)
    if idx_late in transcript:
        return "FAIL", ("index cut moved above ~25,300 chars; "
                        "lint_lib.INDEX_TRUNCATION_CHARS is stale")
    if not early_present:
        return "FAIL", ("index cut moved below ~24,900 chars, or is now "
                        "in bytes; lint_lib.INDEX_TRUNCATION_CHARS is "
                        "stale")
    return "PASS", "IDX_EARLY present in AutoMem; IDX_LATE absent from the R1 transcript"


def _h2(fx, r1):
    topic = fx.tokens["TOPIC"]
    mem = fx.tokens["MEM"]
    transcript = r1.get("transcript") or ""
    contents = [e.get("content") for e in automem(r1.get("instructions"))
               if isinstance(e.get("content"), str)]
    if not any(mem in c for c in contents):
        return "VOID", "MEM absent from R1's AutoMem: no positive control"
    if topic in transcript:
        return "FAIL", "TOPIC (mem/topics/t.md) appears in the R1 transcript"
    return "PASS", "TOPIC absent from the R1 transcript; MEM present in AutoMem"


def _h3(fx):
    def logged(suffix, reason):
        for entry in (fx.loaded or []):
            if not isinstance(entry, dict):
                continue
            file_path = entry.get("file_path")
            if (isinstance(file_path, str) and file_path.endswith(suffix)
                    and entry.get("load_reason") == reason):
                return True
        return False

    always_on_time = logged("always.md", "session_start")
    scoped_on_time = logged("scoped.md", "path_glob_match")
    scoped_early = logged("scoped.md", "session_start")

    failed = []
    if not always_on_time:
        failed.append("always.md never logged at session_start")
    if not scoped_on_time:
        failed.append("scoped.md never logged at path_glob_match")
    if scoped_early:
        failed.append("scoped.md logged at session_start")
    if failed:
        return "FAIL", "; ".join(failed)
    return "PASS", "always.md loaded at session_start; scoped.md only at path_glob_match"


def _h4(fx, r1):
    hist = fx.tokens["HIST"]
    memhist = fx.tokens["MEMHIST"]
    transcript = r1.get("transcript") or ""
    history_loaded = any(
        isinstance(e, dict) and isinstance(e.get("file_path"), str)
        and e["file_path"].endswith("CLAUDE.history.md")
        for e in (fx.loaded or []))

    failed = []
    if hist in transcript:
        failed.append("HIST (CLAUDE.history.md) appears in the R1 transcript")
    if memhist in transcript:
        failed.append("MEMHIST (mem/history/MEMORY.history.md) appears in the R1 transcript")
    if history_loaded:
        failed.append("CLAUDE.history.md was loaded")
    if failed:
        return "FAIL", "; ".join(failed)
    return "PASS", "HIST/MEMHIST absent from the R1 transcript; CLAUDE.history.md never loaded"


def _h5(fx, r1, r2, calling, version):
    mem = fx.tokens["MEM"]
    target = os.path.realpath(os.path.join(fx.root, "mem", "MEMORY.md"))

    a_ok = any(
        isinstance(e.get("path"), str) and isinstance(e.get("content"), str)
        and os.path.realpath(e["path"]) == target and mem in e["content"]
        for e in automem(r1.get("instructions")))
    if not a_ok:
        return "FAIL", "(a) R1's AutoMem carries no mem/MEMORY.md entry containing MEM"

    r2_automem = automem(r2.get("instructions"))
    r2_transcript = r2.get("transcript") or ""
    if r2_automem or mem in r2_transcript:
        return "FAIL", ("(b) R2 (auto-memory disabled) leaked an AutoMem "
                        "entry or MEM into its transcript")

    skip_reason = calling.get("skip")
    calling_version = calling.get("version")
    if not skip_reason and calling_version is not None and calling_version != version:
        skip_reason = "calling session runs {0}, probed binary {1}".format(
            calling_version, version)

    if skip_reason:
        # A mismatch-composed reason is a couldn't-check; a missing kind is
        # strict, so an unclassified skip can never read as healthy.
        if calling.get("skip") and calling.get("skip_kind") == "not_applicable":
            return "PASS", ("(a) R1 AutoMem holds MEM; (b) R2 clean; "
                            "(c) user-level: SKIP ({0})").format(skip_reason)
        return "PARTIAL", ("(a) R1 AutoMem holds MEM; (b) R2 clean; "
                           "(c) user-level: NOT VERIFIED ({0})").format(skip_reason)

    expected_dir = calling.get("expected_dir")
    expected_real = os.path.realpath(expected_dir) if expected_dir else None
    c_ok = expected_real is not None and any(
        isinstance(e.get("path"), str) and isinstance(e.get("content"), str)
        and e["content"] and os.path.dirname(os.path.realpath(e["path"])) == expected_real
        for e in automem(calling.get("instructions")))
    if not c_ok:
        return "FAIL", ("(c) the calling session's AutoMem entry does not "
                        "resolve to the user-level autoMemoryDirectory")

    return "PASS", ("(a) R1 AutoMem holds MEM; (b) R2 clean; (c) calling "
                    "session's AutoMem resolves to the user-level directory")


def _h6(fx, r3):
    """The 200-line index cut, on R3 and the `mem-lines` fixture: LINE_EARLY
    (line 195) must survive it, LINE_LATE (line 206) must not."""
    target = os.path.realpath(os.path.join(fx.root, "mem-lines", "MEMORY.md"))
    contents = [e["content"] for e in automem(r3.get("instructions"))
               if isinstance(e.get("path"), str)
               and isinstance(e.get("content"), str)
               and os.path.realpath(e["path"]) == target]
    if not any(fx.tokens["LINE_HEAD"] in c for c in contents):
        return "VOID", ("R3's AutoMem carries no mem-lines/MEMORY.md entry "
                        "containing LINE_HEAD: no positive control")
    if fx.tokens["LINE_LATE"] in (r3.get("transcript") or ""):
        return "FAIL", ("index line cut moved above ~205 lines; "
                        "lint_lib.INDEX_TRUNCATION_LINES is stale")
    if not any(fx.tokens["LINE_EARLY"] in c for c in contents):
        return "FAIL", ("index line cut moved below ~195 lines; "
                        "lint_lib.INDEX_TRUNCATION_LINES is stale")
    return "PASS", ("LINE_EARLY present in R3's AutoMem; LINE_LATE absent "
                    "from the R3 transcript")


_VERDICTS = {
    "H1": lambda fx, r1, r2, calling, r3, version: _h1(fx, r1),
    "H2": lambda fx, r1, r2, calling, r3, version: _h2(fx, r1),
    "H3": lambda fx, r1, r2, calling, r3, version: _h3(fx),
    "H4": lambda fx, r1, r2, calling, r3, version: _h4(fx, r1),
    "H5": lambda fx, r1, r2, calling, r3, version: _h5(fx, r1, r2, calling, version),
    "H6": lambda fx, r1, r2, calling, r3, version: _h6(fx, r3),
}


def evaluate(fx, r1, r2, calling, r3):
    """Judge one probe run: `(version, [ProbeResult, ...])`, H1..H6, always
    six, always in that order.

    `version` is R1's `init.claude_code_version`, or `None` -- computed
    unconditionally, before any void check, so a version can still be
    named even when every probe is VOID.
    """
    init1 = r1.get("init") if isinstance(r1, dict) else None
    version = None
    if isinstance(init1, dict):
        candidate = init1.get("claude_code_version")
        if isinstance(candidate, str) and candidate:
            version = candidate

    void_reason = {}

    def void(probe_id, reason):
        void_reason.setdefault(probe_id, reason)

    if fx.preexisting_projects_dir:
        pdir = projects_dir_for(fx.root)
        for probe_id in _PROBE_IDS:
            void(probe_id, "cannot own {0}".format(pdir))

    r1_bad = _unusable_reason(r1, fx)
    if r1_bad is not None:
        for probe_id in _PROBE_IDS:
            void(probe_id, "R1 unusable: {0}".format(r1_bad))

    r2_bad = _unusable_reason(r2, fx)
    if r2_bad is not None:
        void("H5", "R2 unusable: {0}".format(r2_bad))

    r3_bad = _unusable_reason(r3, fx)
    if r3_bad is not None:
        void("H6", "R3 unusable: {0}".format(r3_bad))

    h3_bad = _h3_void_reason(fx, r1)
    if h3_bad is not None:
        void("H3", h3_bad)

    results = []
    for probe_id in _PROBE_IDS:
        if probe_id in void_reason:
            results.append(ProbeResult(probe_id, "VOID", void_reason[probe_id]))
        else:
            status, reason = _VERDICTS[probe_id](fx, r1, r2, calling, r3, version)
            results.append(ProbeResult(probe_id, status, reason))
    return version, results


def aggregate(version, results):
    """`(status | None, failures, exit_code)` from `evaluate`'s output.

    Any FAIL wins outright -- `"fail"`, the failing ids in H1..H6 order,
    exit 1 -- even alongside a VOID neighbour, because a demonstrated
    failure must never be masked by unreadable evidence elsewhere. Absent
    a FAIL, any VOID means nothing is recorded and it exits 2. Absent both,
    any PARTIAL records `"partial"` with the unverified ids, exit 0. The
    second element is the ids behind the status (failing ids for `fail`,
    unverified ids for `partial`). A `None`
    version (R1's own `init` never resolved) exits 2 before any of that,
    since there is no version to record a result against.
    """
    if version is None:
        return None, [], 2
    failures = [r.id for r in results if r.status == "FAIL"]
    if failures:
        return "fail", failures, 1
    if any(r.status == "VOID" for r in results):
        return None, [], 2
    partial = [r.id for r in results if r.status == "PARTIAL"]
    if partial:
        return "partial", partial, 0
    return "pass", [], 0


# ----------------------------------------------------------------- run_probes --
# The only subprocess this module owns: build one fixture, run R1, R2
# and R3 against the probed binary, and read whatever the CALLING session's own
# transcript says about user-level resolution -- no run of its own for that
# leg, so none of the user's own hooks ever fire because of a probe.
# This builds exactly the record shapes `evaluate` already defines;
# it adds no new judgment.

# A run that never happened (the preexisting-projects-dir short-circuit, or a
# subprocess that could not even be started) -- shaped exactly like a real
# run record so `evaluate`'s `_unusable_reason` handles it the same way an
# `exit != 0` run would, with no special-casing needed on that side.
_EMPTY_RUN = {
    "exit": -1, "init": None, "result": None, "tool_uses": [],
    "transcript": None, "instructions": None,
}


def _cleanup_scratchpad(run, fx, pdir_basename):
    """Remove one child `claude -p`'s per-session scratchpad directory, and
    its parent slug directory when now empty.

    Every child this tool spawns has Claude Code create a scratchpad at
    `<tmp root>/<slug>/<session_id>/scratchpad`, per `init.scratchpad_path`
    on 2.1.280, and never removes it -- left alone this leaks one `<slug>`
    directory per probe run. `run_probes` is the only caller in a position to
    judge ownership of BOTH path segments, so this asserts both before
    touching anything and never guesses a path (per the toolkit's writing-
    tools rule): `os.path.basename(sess)` must equal the run's OWN
    `session_id`, and `os.path.basename(slugdir)` must equal `pdir_basename`
    (this fixture's own slug, from `projects_dir_for`). Either mismatch skips
    silently -- `run` may also be `None`, or a real run whose `init` never
    resolved, and neither is an error here.
    """
    if not isinstance(run, dict):
        return
    init = run.get("init")
    if not isinstance(init, dict):
        return
    scratchpad_path = init.get("scratchpad_path")
    session_id = init.get("session_id")
    if not isinstance(scratchpad_path, str) or not isinstance(session_id, str):
        return

    sess = os.path.dirname(scratchpad_path)
    slugdir = os.path.dirname(sess)
    if os.path.basename(sess) != session_id:
        return
    if os.path.basename(slugdir) != pdir_basename:
        return

    shutil.rmtree(sess, ignore_errors=True)
    try:
        os.rmdir(slugdir)
    except OSError:
        pass  # not empty (another session's scratchpad still lives there)


def r2_env(env_base):
    """R2's subprocess environment: `env_base` plus
    `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` -- the negative control, auto-memory
    OFF. A module-level function on purpose, not inlined into `run_probes`,
    so a test of H5 has a seam: monkeypatching this to return
    `env_base` unchanged leaves auto-memory ON in R2 and must turn H5 FAIL.
    """
    env = dict(env_base)
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    return env


def _r1_env(env_base):
    """R1's subprocess environment: `env_base` with
    `CLAUDE_CODE_DISABLE_AUTO_MEMORY` removed, so auto-memory loads
    regardless of whatever the calling process's own environment carried.
    """
    env = dict(env_base)
    env.pop("CLAUDE_CODE_DISABLE_AUTO_MEMORY", None)
    return env


def _probe_argv(binary, fx, mem_dir="mem"):
    """R1, R2 and R3's argv -- identical but for `mem_dir` (R3 loads the
    `mem-lines` fixture); R1 and R2 differ only in environment (`_r1_env` /
    `r2_env`).
    """
    return [
        binary, "-p", "Read src/x.py, then reply OK.",
        "--model", "haiku",
        "--setting-sources", "project",
        "--settings", json.dumps({"autoMemoryDirectory": fx.root + "/" + mem_dir}),
        "--strict-mcp-config",
        "--tools", "Read",
        "--max-turns", "4",
        "--max-budget-usd", "0.5",
        "--output-format", "stream-json",
        "--verbose",
    ]


def _parse_stream_json(stdout_text):
    """`(init, result, tool_uses)` off a `claude -p --output-format
    stream-json` stdout. `init` is the FIRST `{"type": "system", "subtype":
    "init"}` line; `result` is the first `{"type": "result"}` line;
    `tool_uses` collects every `{"name", "input"}` pair off `tool_use`
    content items across every `assistant` line, in order. A line that does
    not parse as a JSON object is skipped -- never raises.
    """
    init = None
    result = None
    tool_uses = []
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict):
            continue
        rtype = record.get("type")
        if rtype == "system" and record.get("subtype") == "init" and init is None:
            init = record
        elif rtype == "result" and result is None:
            result = record
        elif rtype == "assistant":
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for item in (content or []):
                if isinstance(item, dict) and item.get("type") == "tool_use":
                    tool_uses.append({"name": item.get("name"),
                                      "input": item.get("input") or {}})
    return init, result, tool_uses


def transcript_instructions(transcript_text):
    """The `files` list off the FIRST transcript line whose `type` is
    `"attachment"` and whose `attachment.type` is `"instructions"`. `None`
    when `transcript_text` is falsy, no such line exists, or the shape does
    not match -- the extraction `evaluate`'s `r1`/`r2`/`calling` records all
    share (M16).
    """
    if not transcript_text:
        return None
    for line in transcript_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        if not isinstance(record, dict) or record.get("type") != "attachment":
            continue
        attachment = record.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("type") != "instructions":
            continue
        files = attachment.get("files")
        return files if isinstance(files, list) else None
    return None


def _read_text(path):
    """The raw text at `path`, or `None` if it does not exist or cannot be
    read. Never raises.
    """
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return None


def _run_once(binary, fx, env, pdir, mem_dir="mem"):
    """Run one probe invocation (R1, R2 or R3 -- argv differs only in
    `mem_dir`, and R1/R2 only in `env`) and return its record in the exact
    shape `evaluate` expects. Persistence is on for all three: none ever passes
    `--no-session-persistence`, so a transcript exists afterward to read.
    Any failure to even start the subprocess collapses to `_EMPTY_RUN` --
    `evaluate`'s `_unusable_reason` reads `exit != 0` / missing `init` the
    same way it reads a real non-zero exit.
    """
    try:
        proc = subprocess.run(
            _probe_argv(binary, fx, mem_dir), cwd=fx.root, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
            timeout=300)
        exit_code = proc.returncode
        stdout_text = (proc.stdout.decode("utf-8", "replace")
                      if isinstance(proc.stdout, bytes) else (proc.stdout or ""))
    except Exception:
        return dict(_EMPTY_RUN)

    init, result, tool_uses = _parse_stream_json(stdout_text)
    transcript = None
    session_id = init.get("session_id") if isinstance(init, dict) else None
    if session_id:
        transcript = _read_text(os.path.join(pdir, "{0}.jsonl".format(session_id)))
    instructions = transcript_instructions(transcript)

    return {
        "exit": exit_code, "init": init, "result": result,
        "tool_uses": tool_uses, "transcript": transcript,
        "instructions": instructions,
    }


def _rename_if_present(src, dst):
    if os.path.exists(src):
        os.rename(src, dst)


def _rename_run_logs(root, suffix):
    """`loaded.jsonl` -> `loaded-<suffix>.jsonl`, `blocked.jsonl` ->
    `blocked-<suffix>.jsonl`, only when the source file exists -- a run that
    triggered neither hook leaves no log to rename at all.
    """
    _rename_if_present(os.path.join(root, "loaded.jsonl"),
                      os.path.join(root, "loaded-{0}.jsonl".format(suffix)))
    _rename_if_present(os.path.join(root, "blocked.jsonl"),
                      os.path.join(root, "blocked-{0}.jsonl".format(suffix)))


def _load_jsonl(path):
    """Every parsed JSON line at `path`, or `[]` when it is missing,
    unreadable, or holds nothing parseable -- these are fixture hook logs,
    not authoritative state, so a bad line is skipped rather than fatal.
    """
    if not path or not os.path.exists(path):
        return []
    out = []
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out


# Sentinel default for `load_json`: distinguishes "could not read" from `{}`.
_UNREADABLE = object()


def _calling_lookup(calling_session_id):
    """`calling` -- the user-level check reads the CALLING
    session's own transcript rather than running anything, so none of
    the user's own hooks ever fire because of a probe. `skip`, with the
    printed reason, covers the first three cases: no session id, no
    user-level `autoMemoryDirectory`, or the calling transcript not found by
    exactly one glob match. The fourth case -- the calling session runs a
    different version than R1's probed binary -- is decided later, inside
    `evaluate`'s H5, because it needs R1's resolved version too.

    Also `skip` when the calling transcript carries no `AutoMem` entry AND
    the session had nothing to load: auto-memory
    disabled (the binary's own two switches, `CLAUDE_CODE_DISABLE_AUTO_MEMORY`
    with its truthy set, then `autoMemoryEnabled: false`) or no non-empty
    MEMORY.md in the directory. A healthy harness injects no `AutoMem` entry
    then, so judging (c) would record a false FAIL (Merge Review MR1).
    """
    def skip(reason, kind):
        # `kind`: "unchecked" (could not look) or "not_applicable" (looked,
        # and there is nothing to verify). Only the latter may read as pass.
        return {"skip": reason, "skip_kind": kind, "version": None,
                "instructions": None, "expected_dir": None}

    if not calling_session_id:
        return skip("not run from a Claude session", "unchecked")

    settings_path = os.path.expanduser(os.environ.get(
        "HARNESS_CHECK_USER_SETTINGS", os.path.join(claude_home(), "settings.json")))
    # `load_json` collapses a malformed or unreadable file to its default, so
    # absence must be proven: a sentinel result on a file that exists is a
    # read failure, not an absent setting.
    settings = load_json(settings_path, _UNREADABLE)
    if settings is _UNREADABLE:
        if os.path.exists(settings_path):
            return skip("user settings could not be read", "unchecked")
        settings = {}
    auto_mem_dir = settings.get("autoMemoryDirectory")
    if not isinstance(auto_mem_dir, str) or not auto_mem_dir:
        return skip("no user-level autoMemoryDirectory", "not_applicable")

    matches = glob.glob(os.path.join(
        _projects_root(), "*", "{0}.jsonl".format(calling_session_id)))
    if len(matches) != 1:
        return skip("calling transcript not found", "unchecked")

    transcript_path = matches[0]
    instructions = transcript_instructions(_read_text(transcript_path))
    # `None` means the transcript was unreadable, unparseable or carried no
    # instructions record -- so "nothing was loaded" below cannot be shown,
    # and a wrong-directory AutoMem would be invisible. Unchecked, not N/A.
    if instructions is None:
        return skip("calling transcript has no readable instructions record",
                    "unchecked")

    # Only when nothing was loaded: an AutoMem entry from the WRONG directory
    # is exactly what (c) exists to catch, whatever the switches say.
    if not automem(instructions):
        disabled = os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "")
        if disabled.strip().lower() in ("1", "true", "yes", "on"):
            return skip("auto-memory disabled in the calling session", "not_applicable")
        if settings.get("autoMemoryEnabled") is False:
            return skip("autoMemoryEnabled is false", "not_applicable")
        memory_md = os.path.join(os.path.expanduser(auto_mem_dir), "MEMORY.md")
        memory_text = _read_text(memory_md)
        if memory_text is None and os.path.exists(memory_md):
            return skip("MEMORY.md could not be read", "unchecked")
        if not (memory_text or "").strip():
            return skip("no MEMORY.md to load in the user-level autoMemoryDirectory",
                        "not_applicable")

    return {
        "skip": None,
        "skip_kind": None,
        "version": transcript_version(transcript_path),
        "instructions": instructions,
        "expected_dir": os.path.expanduser(auto_mem_dir),
    }


def run_probes(binary, parent_dir, calling_session_id, env_base):
    """Build one fixture, run R1, R2 and R3 against `binary`, and read the
    calling session's own transcript -- `(fx, r1, r2, calling, r3)`, the exact
    inputs `evaluate` takes.

    Ownership is established, not assumed (M17/design Sec.3): slugs collide,
    so if this fixture's own projects directory already exists before this
    call, this run cannot tell its own writes apart from whatever put it
    there. All three runs are skipped entirely -- the binary is never invoked --
    and `evaluate`'s own preexisting-dir rule VOIDs every probe; this
    function never inspects that flag itself, only sets it.

    Cleanup always runs, in `finally`: each child's own scratchpad directory
    is removed first (`_cleanup_scratchpad`, keyed on R1's, R2's and R3's own
    `init`), then the fixture root unconditionally, then the projects
    directory only when this run created it -- never one that was already
    there before this call, which is exactly the directory the ownership
    check above refused to touch.
    """
    fx = build_fixture(parent_dir, random.SystemRandom())
    pdir = projects_dir_for(fx.root)
    fx.preexisting_projects_dir = os.path.exists(pdir)

    r1 = None
    r2 = None
    r3 = None
    try:
        if fx.preexisting_projects_dir:
            r1 = dict(_EMPTY_RUN)
            r2 = dict(_EMPTY_RUN)
            r3 = dict(_EMPTY_RUN)
        else:
            r1 = _run_once(binary, fx, _r1_env(env_base), pdir)
            _rename_run_logs(fx.root, "r1")
            fx.loaded = _load_jsonl(os.path.join(fx.root, "loaded-r1.jsonl"))
            fx.blocked = _load_jsonl(os.path.join(fx.root, "blocked-r1.jsonl"))

            r2 = _run_once(binary, fx, r2_env(env_base), pdir)
            _rename_run_logs(fx.root, "r2")

            r3 = _run_once(binary, fx, _r1_env(env_base), pdir, mem_dir="mem-lines")
            _rename_run_logs(fx.root, "r3")

        calling = _calling_lookup(calling_session_id)
        return fx, r1, r2, calling, r3
    finally:
        # Each child claude -p left a scratchpad directory behind (see
        # `_cleanup_scratchpad`'s docstring); clean all three before the fixture
        # itself is torn down, so a wrong ownership guess would surface
        # against a fixture still on disk to inspect.
        pdir_basename = os.path.basename(pdir)
        _cleanup_scratchpad(r1, fx, pdir_basename)
        _cleanup_scratchpad(r2, fx, pdir_basename)
        _cleanup_scratchpad(r3, fx, pdir_basename)
        shutil.rmtree(fx.root, ignore_errors=True)
        if not fx.preexisting_projects_dir:
            shutil.rmtree(pdir, ignore_errors=True)
