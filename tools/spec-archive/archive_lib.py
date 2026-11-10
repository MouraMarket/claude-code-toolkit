#!/usr/bin/env python3
"""Spec archival: pure classification and path logic, no filesystem mutation.

`classify()` is the single source of truth for the safety contract described
in the plan: a handover-named file, a symlink, or anything that would land
outside `specs_dir` is refused outright regardless of its declared status; an
undeclared or unrecognised status is reported rather than guessed at; and
`status: live` is never archived, at any age. The executable
(`claude-spec-archive`) is the only thing in this tool that actually moves a
file -- nothing here touches the filesystem except to read it.
"""

# Design decisions about this file that look like accidental complexity are
# recorded in docs/decisions.md. Read the relevant one before simplifying
# anything here.

# Design decisions referenced below as D1-D11 (D6 is not referenced anywhere):
#   D1  The stale-live flip is off by default: stale_flip_days absent or 0
#       means no flip.
#   D2  The flip: a `status: live` spec idle longer than stale_flip_days
#       becomes `shipped`. Skip reasons are checked in a fixed order, the first
#       that applies is reported, and flip_verdict() never raises. A flip
#       resets the file's mtime on purpose, so a spec flipped this run is not
#       archived in the same run.
#   D3  The "Waiting on a human" table grammar: every table in the section
#       needs a Status column and at least one data row, and every status cell
#       must be in a closed vocabulary. Separator rows are skipped, never data.
#       An escaped "\|" is not special, so a mis-split can only land on
#       "open", never on a false "clear".
#   D4  Mission-shaped means a heading starting "Backlog" or "MVP"; a
#       mission-shaped spec with no "Waiting on a human" section never flips.
#   D5  A flip replaces the `status: live` front-matter line with a marker
#       line; the match is permissive (quoted or not, trailing comment) so a
#       hand-edited line still counts.
#   D7  The hook is bounded by hook_deadline_seconds: on a timeout it reports
#       what completed and ends the whole process, so a stalled syscall in the
#       worker cannot hold up the session.
#   D8  The hook takes an exclusive lock; a session that cannot get it exits
#       silently because another session is doing the work. A flip re-stats
#       the file around its read and write so a concurrent edit is not
#       clobbered.
#   D9  The hook always exits 0 and fails open, loudly: it is silent in
#       exactly four cases (archiving disabled, no specs_dir configured, lock
#       held elsewhere, the run did nothing) and every other outcome emits a
#       notice.
#   D10 Subjects are slugged (a-z0-9 runs become one hyphen, capped at 40), so
#       a computed destination cannot contain "/" or "..". A subject that
#       slugs to "" is reported undeclared; an archive_group that does falls
#       back to the default group.
#   D11 A flip rewrites only the status line, byte-exact elsewhere, reading
#       with newline="" so a CRLF file stays CRLF.

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

MANIFEST_NAME = ".spec-status.json"
ARCHIVE_DIR_NAME = "archive"

KNOWN_STATUSES = ("live", "shipped", "superseded")

# Handovers are the thread-handover sweep's job (tools/thread-handover/index_lib.py):
# that file has its own lifecycle -- grace gates, an index row, a consuming
# session -- and two owners of one file is how a document gets lost. So this tool
# refuses a handover-named file even when it carries an archivable status.
#
# The sweep ARCHIVES rather than deletes as of 2026-08-26, so "one deletes, one
# moves" is no longer the reason. The refusal is not weaker for that: the sweep
# files into <handover_dir>/archive/handovers/, which this tool skips along with
# the rest of archive/, and that separation is what keeps the two apart on a
# machine where handover_dir and specs_dir are the same directory.
HANDOVER_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-.+-handover\.md$")

# The filename convention every spec follows: YYYY-MM-DD-<slug>[.ext].
DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})-(.+)$")

FRONT_MATTER_DELIM = "---"

# --- D10: subject slugging -------------------------------------------------

# Any run of characters that is not a-z or 0-9 becomes one hyphen. This is
# what makes destination_for() safe against a traversal subject (D10): the
# slug can never contain "/" or "..", so a computed destination cannot walk
# outside specs_dir by construction.
SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")
SLUG_MAX_LEN = 40

# --- D3/D4: flip-eligibility text analysis ----------------------------------

# A markdown ATX heading, any level 1-6. Matched against a stripped line.
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")

# D4: mission-shaped means a heading whose text starts with "Backlog" or
# "MVP", word-bounded so "MVPish" does not count.
MISSION_HEADING_RE = re.compile(r"(?i)^(backlog|mvp)\b")

WAITING_ON_HUMAN_PREFIX = "waiting on a human"

# An opening or closing fence line, per strip_fenced_blocks().
FENCE_MARK_RE = re.compile(r"^(```|~~~)")

# A table separator row: every cell matches this (optional leading/trailing
# ":", one or more "-"). D3 says a separator is skipped, never a data row.
SEPARATOR_CELL_RE = re.compile(r"^:?-+:?$")

# D3's closed vocabulary. Kept as its own module-level constant (rather than
# inlined in _is_closed) so a mutation test can flip it in isolation --
# CLAUDE.md's mutation-testing convention -- to prove TestHumanRowsState
# actually depends on this exact set rather than merely on "non-empty".
CLOSED_STATUS_RE = re.compile(
    r"^(done|closed|no-action|no action|decided,? no[- ]action|not applicable|n/a)\b"
)

# Unfinished-work check (blocks the stale flip). Backlog tables in practice say
# shipped / merged / superseded, which D3's vocabulary lacks, so this is a
# wider set -- D3's words plus those. A separate module-level constant so a
# mutation can target it without touching the human-rows vocabulary.
BACKLOG_CLOSED_STATUS_RE = re.compile(
    r"^(done|closed|no-action|no action|decided,? no[- ]action|not applicable|n/a"
    r"|shipped|merged|superseded|dropped|cut)\b"
)
UNTICKED_TASK_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+\[ \]")
BACKLOG_HEADING_RE = re.compile(r"(?i)^backlog\b")

# D5: the marker line a flip writes, and the front-matter line it replaces.
# Deliberately permissive on the RHS (quoted or not, trailing comment or
# not) so a hand-edited "live" line still gets picked up.
STATUS_LIVE_RE = re.compile(r'^status:\s*["\']?live["\']?\s*(#.*)?$')


@dataclass
class Verdict:
    """The outcome of classifying one path under specs_dir."""
    action: str  # "archive" | "keep" | "undeclared" | "unmanaged"
    reason: str
    destination: Optional[str] = None


def parse_front_matter(text: str) -> Dict[str, str]:
    """Parse the leading `---` block of a spec's front matter.

    Deliberately not a YAML parser: `key: value` pairs only, with an optional
    trailing ` # comment` and optional quoting -- everything the convention
    actually uses. Anything else (no opening delimiter, no closing delimiter,
    non-string input) returns {}. Never raises: a malformed spec must be
    reported as undeclared, not crash the whole run.
    """
    if not isinstance(text, str):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONT_MATTER_DELIM:
        return {}
    result: Dict[str, str] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == FRONT_MATTER_DELIM:
            return result
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", stripped)
        if not match:
            continue
        key, raw_value = match.groups()
        value = raw_value.split(" #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            result[key] = value
    # No closing delimiter was ever found -- the block never actually closed,
    # so nothing read from inside it can be trusted.
    return {}


def load_manifest(specs_dir: str) -> Dict[str, Any]:
    """`<specs_dir>/.spec-status.json`, or {} when missing or corrupt.

    Consulted only for non-.md paths -- directories, .json, .py -- which
    cannot carry front matter of their own.

    **Neither source is a fallback for the other, and that is the point.** A
    .md path never consults this manifest; a non-.md path never consults front
    matter. Two mechanisms that can produce the same verdict on the same input
    hide each other's failures -- this repo already paid for that in
    install.sh's worktree detection, where a dangling-worktree fallback
    silently took over the whole decision after a hardening pass broke the
    primary comparison, and all 18 tests stayed green. Splitting by file type
    means a broken manifest reader can never quietly cover for broken
    front-matter parsing, or the reverse. Do not add a fallback here.
    """
    try:
        with open(os.path.join(specs_dir, MANIFEST_NAME), encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def is_within(child: str, parent: str) -> bool:
    """True when `child` is `parent` itself or lives underneath it.

    Resolved through realpath, and os.sep-suffixed so /a/b does not match a
    path at /a/b-other.
    """
    try:
        child_real = os.path.realpath(child)
        parent_real = os.path.realpath(parent)
    except Exception:
        return False
    return child_real == parent_real or child_real.startswith(parent_real + os.sep)


def slugify(value: str) -> str:
    """Lowercase, collapse any non a-z0-9 run to one "-", trim, cap at 40 (D10).

    Never raises: a non-string or empty input just produces "". An already
    slug-shaped value ("toolkit") is unchanged -- every existing archive
    folder name is one (verified: all 20 folders under archive/ are
    lowercase-hyphen), so this must be a true identity on that input class,
    not merely "close enough".

    The consequence this exists for: a traversal subject ("../../etc") can
    no longer walk a computed path outside specs_dir, because the slug it
    produces can never contain "/" or "..". See classify()'s and
    destination_for()'s docstrings for where this is used.
    """
    if not value:
        return ""
    try:
        lowered = str(value).lower()
    except Exception:
        return ""
    slug = SLUG_INVALID_RE.sub("-", lowered)
    slug = slug.strip("-")
    slug = slug[:SLUG_MAX_LEN]
    # A truncation can land mid-run and leave a trailing "-" behind (D10:
    # "Truncate to 40 characters, then strip trailing '-' again").
    slug = slug.rstrip("-")
    return slug


def strip_fenced_blocks(text: str) -> str:
    """Remove every ``` or ~~~ fenced block, opening and closing line included.

    An unclosed fence removes to EOF -- there is no partial-fence state to
    recover from, so the safest reading of a malformed file is "everything
    after this point might be example text, not real content". Used to keep
    is_mission_shaped() and human_rows_state() from being fooled by a
    documentation example that merely *shows* a heading or a table.
    """
    lines = text.splitlines(keepends=True)
    out: List[str] = []
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if not in_fence:
            if FENCE_MARK_RE.match(stripped):
                in_fence = True
                continue
            out.append(line)
        else:
            if FENCE_MARK_RE.match(stripped):
                in_fence = False
            # Every line while inside a fence is dropped, including the one
            # that closes it.
    return "".join(out)


def _headings(text: str) -> List[Tuple[int, int, str]]:
    """(line_index, level, heading text) for every ATX heading, by line."""
    result = []
    for i, line in enumerate(text.splitlines()):
        match = HEADING_RE.match(line.strip())
        if match:
            result.append((i, len(match.group(1)), match.group(2).strip()))
    return result


def is_mission_shaped(text: str) -> bool:
    """True when a heading (any level) starts with "Backlog" or "MVP" (D4).

    Fenced blocks are stripped first, so an example heading inside a code
    block never counts -- this is what keeps a `### Blocked on the owner`
    example in a *documentation* file from being read as a real mission
    section.
    """
    stripped_text = strip_fenced_blocks(text)
    for _, _, heading_text in _headings(stripped_text):
        if MISSION_HEADING_RE.match(heading_text):
            return True
    return False


def _split_row_cells(line: str) -> List[str]:
    """One `|`-delimited table row into cells, per D3's table grammar.

    Strip the line, drop one leading and one trailing "|" if present, split
    on "|", strip each cell. An escaped "\\|" is deliberately not special
    (D3: "this is accepted, and it can only mis-split toward open") --
    treating it specially would risk mis-splitting the other way, toward a
    false clear.
    """
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


def _is_closed(cell: str) -> bool:
    """A status cell reads as closed per D3's exact vocabulary."""
    normalized = cell.strip(" \t\r\n*_`✅").lower()
    return bool(CLOSED_STATUS_RE.match(normalized))


def _is_backlog_closed(cell: str) -> bool:
    """A Backlog status cell reads as closed per the wider vocabulary."""
    normalized = cell.strip(" \t\r\n*_`✅").lower()
    return bool(BACKLOG_CLOSED_STATUS_RE.match(normalized))


def _find_tables(lines: List[str]) -> List[List[str]]:
    """Maximal runs of consecutive "|"-prefixed lines, each one table (D3)."""
    tables: List[List[str]] = []
    current: List[str] = []
    for line in lines:
        if line.strip().startswith("|"):
            current.append(line)
        else:
            if current:
                tables.append(current)
                current = []
    if current:
        tables.append(current)
    return tables


def _table_state(rows: List[str],
                 is_closed: Callable[[str], bool] = _is_closed) -> Tuple[bool, str]:
    """(closed, reason) for one table's rows, per D3.

    Every table in a section must independently have a Status column and at
    least one data row, and every data row's status cell must be closed --
    "strict on purpose: an explanatory table with no Status column blocks
    the flip" (D3).
    """
    header_cells = _split_row_cells(rows[0])
    status_index = None
    for i, cell in enumerate(header_cells):
        if cell.strip(" \t\r\n*_").lower() == "status":
            status_index = i
            break
    if status_index is None:
        return False, "table has no Status column"

    data_rows = rows[1:]
    if data_rows:
        separator_cells = _split_row_cells(data_rows[0])
        if separator_cells and all(SEPARATOR_CELL_RE.match(c) for c in separator_cells):
            data_rows = data_rows[1:]
    if not data_rows:
        return False, "table has no data rows"

    for row in data_rows:
        cells = _split_row_cells(row)
        if len(cells) <= status_index:
            return False, "data row missing its Status cell"
        if not is_closed(cells[status_index]):
            return False, f"open status cell: {cells[status_index]!r}"
    return True, "all rows closed"


def _section_state(lines: List[str]) -> Tuple[str, str]:
    """("open"|"clear", reason) for one "Waiting on a human" section's body."""
    tables = _find_tables(lines)
    if not tables:
        return "open", "no table"
    for rows in tables:
        closed, reason = _table_state(rows)
        if not closed:
            return "open", reason
    return "clear", "all tables closed"


def human_rows_state(text: str) -> Tuple[str, str]:
    """("absent"|"open"|"clear", reason) per D3.

    Fenced blocks are stripped first (same reason as is_mission_shaped():
    a `## Waiting on a human` shown as a documentation example must not
    count as a real section). A file can carry more than one matching
    heading; if any of them is open, the whole file is open -- this follows
    handover.md's "a missing section fails this check" and "a section with
    no status column cannot pass at all".
    """
    stripped_text = strip_fenced_blocks(text)
    lines = stripped_text.splitlines()
    headings = _headings(stripped_text)
    targets = [h for h in headings if h[2].lower().startswith(WAITING_ON_HUMAN_PREFIX)]
    if not targets:
        return "absent", "no 'Waiting on a human' heading"

    open_reasons = []
    for line_no, level, _heading_text in targets:
        end = len(lines)
        for other_no, other_level, _ in headings:
            if other_no > line_no and other_level <= level:
                end = other_no
                break
        state, reason = _section_state(lines[line_no + 1:end])
        if state == "open":
            open_reasons.append(reason)

    if open_reasons:
        return "open", "; ".join(open_reasons)
    return "clear", "all sections clear"


def unfinished_work_state(text: str) -> Tuple[str, str]:
    """("open"|"clear", reason): does the text carry unfinished work?

    Open when a line is an unticked task box, or when a Backlog section has
    no table or a table with a row the wider Backlog vocabulary does not
    call closed. Fenced blocks are stripped first. Never raises: an error
    reads as open, the safe direction (it blocks a flip, never forces one).
    """
    try:
        stripped_text = strip_fenced_blocks(text)
        lines = stripped_text.splitlines()
        for line in lines:
            if UNTICKED_TASK_RE.match(line):
                return "open", f"unticked task: {line.strip()[:60]!r}"

        headings = _headings(stripped_text)
        for line_no, level, heading_text in headings:
            if not BACKLOG_HEADING_RE.match(heading_text):
                continue
            end = len(lines)
            for other_no, other_level, _ in headings:
                if other_no > line_no and other_level <= level:
                    end = other_no
                    break
            tables = _find_tables(lines[line_no + 1:end])
            if not tables:
                return "open", "Backlog section has no table"
            for rows in tables:
                closed, reason = _table_state(rows, _is_backlog_closed)
                if not closed:
                    return "open", "Backlog: " + reason
        return "clear", "no unfinished work found"
    except Exception as exc:
        return "open", f"unexpected error: {exc!r}"


@dataclass
class FlipVerdict:
    """The outcome of considering one path for the D2 stale flip."""
    action: str  # "flip" | "skip"
    reason: str
    mtime: Optional[float] = None


def flip_verdict(path: str, specs_dir: str, stale_days: int,
                  now: Optional[datetime] = None) -> FlipVerdict:
    """Decide whether one path should flip status: live -> shipped (D2).

    Never raises: a malformed or unreadable file is reported as a skip, not
    a crash. Skip reasons are checked in the exact order D2 lists them, so
    the first one that applies is the one reported -- a handover-named file
    is always refused as "handover-named", never as "not old enough", even
    when both are true.

    Read-only: this is the one function in this module allowed to touch the
    filesystem (read the file's text and its mtime). It never writes.
    """
    now = now or datetime.now()
    try:
        if stale_days <= 0:
            return FlipVerdict("skip", "flip disabled (stale_flip_days <= 0)")

        basename = os.path.basename(os.path.normpath(path))

        if HANDOVER_NAME_RE.match(basename):
            return FlipVerdict("skip", "handover-named file -- owned by the "
                               "thread-handover sweep, not this tool")

        if basename.startswith("."):
            return FlipVerdict("skip", "dotfile -- not a spec")

        if os.path.islink(path):
            return FlipVerdict("skip", "symlink refused outright")

        if not is_within(path, specs_dir):
            return FlipVerdict("skip", "path escapes specs_dir")

        if not basename.lower().endswith(".md"):
            return FlipVerdict("skip", "not a markdown file")

        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except Exception as exc:
            return FlipVerdict("skip", f"unreadable: {exc!r}")

        meta = parse_front_matter(text)
        status = meta.get("status")
        if status != "live":
            return FlipVerdict("skip", f"status is not live (got {status!r})")

        subject = meta.get("subject")
        subject_slug = slugify(subject) if subject else ""
        if not subject or not subject_slug:
            return FlipVerdict("skip", "no subject or empty slug")

        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return FlipVerdict("skip", "cannot read mtime")
        age_days = (now - datetime.fromtimestamp(mtime)).total_seconds() / 86400.0

        # Strict ">", same as classify()'s min_age_days boundary: exactly at
        # the boundary is not old enough yet.
        if not (age_days > stale_days):
            return FlipVerdict("skip", f"{age_days:.1f}d old "
                               f"(stale_flip_days is {stale_days})")

        rows_state, rows_reason = human_rows_state(text)
        if rows_state == "open":
            return FlipVerdict("skip", f"human rows open: {rows_reason}")

        if is_mission_shaped(text) and rows_state == "absent":
            return FlipVerdict("skip", "mission-shaped with no 'Waiting on "
                               "a human' section (D4)")

        work_state, work_reason = unfinished_work_state(text)
        if work_state == "open":
            return FlipVerdict("skip", f"unfinished work: {work_reason}")

        return FlipVerdict("flip", f"idle {age_days:.1f}d, human rows "
                           f"{rows_state}", mtime)
    except Exception as exc:
        # Belt and braces on top of the per-step guards above: this
        # function's contract is "never raises", full stop.
        return FlipVerdict("skip", f"unexpected error: {exc!r}")


def rewrite_status_shipped(text: str, today: str, stale_days: int) -> Optional[str]:
    """Replace the front matter's `status: live` line with the D5 marker.

    Byte-exact everywhere except that one line: uses splitlines(keepends=True)
    so every other line, including its own line ending, survives untouched
    (D11 -- this is what keeps a CRLF file CRLF). Returns None when the
    front-matter block never closes, or closes without a matching status
    line -- both mean there is nothing safe to rewrite.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != FRONT_MATTER_DELIM:
        return None

    closing_index = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONT_MATTER_DELIM:
            closing_index = i
            break
    if closing_index is None:
        return None

    marker = (f"status: shipped # auto-flipped {today} by "
              f"claude-spec-archive: idle >{stale_days}d, set back to live to keep")

    for i in range(1, closing_index):
        line = lines[i]
        line_match = re.search(r"(\r\n|\r|\n)$", line)
        ending = line_match.group(1) if line_match else ""
        content = line[:len(line) - len(ending)] if ending else line
        if STATUS_LIVE_RE.match(content):
            lines[i] = marker + ending
            return "".join(lines)

    return None


def destination_for(path: str, subject: str, group: str, specs_dir: str) -> str:
    """`archive/YYYYMM - <subject> - <group>/YYYYMMDD - <subject> - <slug>.<ext>`.

    YYYYMM/YYYYMMDD and the filename's own slug all come from the filename's
    date prefix -- nothing here reads mtime or any other clock, so the same
    inputs always produce the same destination. `subject` and `group` are
    slugified here (D10), not by the caller: a subject with "/" or ".."
    cannot walk the destination outside specs_dir, because the slug it
    produces can contain neither. Raises ValueError if the basename carries
    no YYYY-MM-DD prefix; classify() treats that as undeclared rather than
    letting it crash a whole run.
    """
    basename = os.path.basename(os.path.normpath(path))
    stem, ext = os.path.splitext(basename)
    match = DATE_PREFIX_RE.match(stem)
    if not match:
        raise ValueError(f"{basename!r} has no YYYY-MM-DD date prefix")
    year, month, day, filename_slug = match.groups()
    subject_slug = slugify(subject)
    group_slug = slugify(group)
    folder = f"{year}{month} - {subject_slug} - {group_slug}"
    filename = f"{year}{month}{day} - {subject_slug} - {filename_slug}{ext}"
    return os.path.join(specs_dir, ARCHIVE_DIR_NAME, folder, filename)


def refuses_overwrite(destination: str) -> bool:
    """True when something already sits at `destination`.

    Checked immediately before every move rather than baked into classify():
    this tool's own earlier moves within a single run can create the very
    destination a later file's move would collide with, so the check has to
    be live at move time, not a snapshot taken during classification.
    """
    return os.path.exists(destination)


def classify(path: str, specs_dir: str, manifest: Dict[str, Any],
             min_age_days: int, now: Optional[datetime] = None) -> Verdict:
    """Decide what happens to one path under specs_dir. Never raises.

    Order matters: the safety refusals (handover name, symlink, containment)
    are decided before anything about the declared status is even read, so a
    handover file carrying `status: shipped` still never reaches "archive".
    """
    now = now or datetime.now()
    basename = os.path.basename(os.path.normpath(path))

    if HANDOVER_NAME_RE.match(basename):
        return Verdict("unmanaged", "handover-named file -- owned by the "
                       "thread-handover sweep, not this tool")

    # UNDECLARED means "a spec still owed a status". A dotfile is not a spec and
    # never will be -- another tool's state directory, an editor's leavings --
    # so reporting it as undeclared would put something permanently unresolvable
    # on a list whose whole purpose is to be worked down to empty.
    if basename.startswith("."):
        return Verdict("unmanaged", "hidden path -- not a spec")

    if os.path.islink(path):
        return Verdict("unmanaged", "symlink refused outright")

    if not is_within(path, specs_dir):
        return Verdict("unmanaged", "path escapes specs_dir")

    is_markdown = basename.lower().endswith(".md")
    if is_markdown:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except Exception:
            text = ""
        meta = parse_front_matter(text)
    else:
        entry = manifest.get(basename)
        meta = entry if isinstance(entry, dict) else {}

    status = meta.get("status")
    if status not in KNOWN_STATUSES:
        return Verdict("undeclared", f"no declared status (got {status!r})")

    if status == "live":
        return Verdict("keep", "status: live")

    subject = meta.get("subject")
    if not subject:
        return Verdict("undeclared", f"status: {status} but no subject declared")

    # D10: a subject that slugs to "" ("—" alone, say) can build no usable
    # destination. Caught here rather than inside destination_for() so the
    # verdict is "undeclared" (a spec still owed a real subject), not a
    # ValueError bubbling up from path construction.
    if not slugify(subject):
        return Verdict("undeclared", "subject has no usable characters")

    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return Verdict("undeclared", "cannot read mtime")
    age_days = (now - datetime.fromtimestamp(mtime)).total_seconds() / 86400.0

    # Strict `>`, not `>=`: a file exactly at the boundary is not archivable.
    if not (age_days > min_age_days):
        return Verdict("keep", f"status: {status}, {age_days:.1f}d old "
                       f"(min_age_days is {min_age_days})")

    group = meta.get("archive_group") or f"{status}-specs"
    # D10: an archive_group that slugs to "" falls back to the same default
    # as a group that was never declared at all.
    if not slugify(group):
        group = f"{status}-specs"
    try:
        destination = destination_for(path, subject, group, specs_dir)
    except ValueError as exc:
        return Verdict("undeclared", str(exc))

    if not is_within(destination, specs_dir):
        return Verdict("unmanaged", "computed destination escapes specs_dir")

    return Verdict("archive", f"status: {status}, {age_days:.1f}d old", destination)
