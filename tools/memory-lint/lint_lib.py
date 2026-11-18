"""Notice when the memory INDEX has drifted into being a second copy of the memories.

`MEMORY.md` is injected into every session; the memory files behind it are read
lazily and cost nothing until opened. So the index is the only part of the memory
system carrying a standing token price, and a hook's whole job is to answer
"should I open this file?" — roughly 120-160 characters of routing prose.

The drift this catches is specific and it recurs: a correction gets appended to
the *hook* instead of to the file the hook points at. Each append is individually
reasonable and invisible, and the index quietly becomes a competing copy of the
corpus. Left alone, the hook text grows steadily, and the worst single hook can end up
longer than many of the memory files it points at.

Why length and not total size: the index legitimately grows as memories are added,
so a flat byte cap would fire on a healthy corpus that simply got bigger and stay
silent on a small corpus full of bloated hooks. Hook length is scale-free, and it
names the thing a reader can actually fix.

Precision over recall, following `memory-routing` next door. A notice that fires
on an ordinary session trains the reader to skip it, so the defaults sit well
above the 160-character target: a single hook has to reach 200, or the mean has
to reach 170, before this says anything. On a healthy index it is silent.

Structural faults are reported at any size, because they are defects rather than
drift: an orphaned memory file is invisible to every future session, and a hook
pointing at a file that does not exist is a dead end.

A `topics/` subdirectory under the memory directory switches these checks into
their two-level form: a memory is reachable when its row sits in the Always
section of MEMORY.md, or in a topic file that a row in the Topics section
nominates. `routed: false` exempts nothing there, because nothing needs to stay
dark once a topic file exists to hold it. Without a `topics/` subdirectory the
tool behaves exactly as it always has.
"""

import os
import re

# Deliberately above the 120-160 routing target. See the precision note above.
DEFAULT_MAX_HOOK_CHARS = 200
DEFAULT_MAX_MEAN_HOOK_CHARS = 170

# The whole-index budget, in CHARACTERS.
#
# The harness truncates MEMORY.md at the TAIL, silently, with no error
# anywhere -- rows simply do not arrive. It measures CHARACTERS, not bytes; you
# can check the unit on your own index by comparing the size the session notice
# prints with the file's character and byte counts. The harness names its limit
# "24.4KB", i.e. 25,000/1024 = 24.41 -- so the constant is INFERRED to be 25,000
# from the rounding, while the UNIT is measured. `index_chars` below is therefore
# `len(text)` and never `len(text.encode())`: an index full of multi-byte em
# dashes reads high in bytes and would put the threshold on the wrong side.
#
# 24,000 sits below the whole inferred band, so the notice fires while there is
# still room to act rather than after rows have already gone missing.
DEFAULT_MAX_INDEX_CHARS = 24000
# Second consumer: tools/context-growth/growth_lib.index_max_chars reads
# this through max_index_chars(), for the edit-time notice. A change here
# is a change to both tools.

# Where rows actually start disappearing. Only used to tell "over budget" from
# "losing data" in the notice -- never as the cap, because a budget set AT the
# cliff gives a session no warning before it goes over.
INDEX_TRUNCATION_CHARS = 25000

# N3b, LINES axis. P1 VERIFIED (binary read 2026-09-29): the 2.1.280 binary
# carries `WO=200` beside `zW=25000`. P2 VERIFIED (same read, `GWe`/`GCt`): the
# harness trims the text, then counts `trimmed.count("\n") + 1`, and truncates
# only when that count is strictly greater than 200 -- so a 200-line index
# loses nothing. See `harness_line_count()` below for the exact rule; every
# line figure in this module means THAT count, never `len(text.splitlines())`.
INDEX_TRUNCATION_LINES = 200

# Sits below INDEX_TRUNCATION_LINES for the same reason DEFAULT_MAX_INDEX_CHARS
# sits below INDEX_TRUNCATION_CHARS: the notice must fire while a session can
# still act, not only after rows are already gone.
DEFAULT_MAX_INDEX_LINES = 190

INDEX_NAME = "MEMORY.md"

# A memory directory switches to the two-level layout by CONTAINING this
# subdirectory -- never by configuration. See `layout()` below.
TOPICS_DIR = "topics"

# N3b: the harness's own 25,000-char truncation point (P2), not half of
# anything -- an earlier version of this comment claimed "half the index
# budget", which was arbitrary. A topic file is read whole with Read and NEVER
# truncated by the harness, so unlike the index (whose cap sits BELOW its
# anchor to warn before loss) this cap sits ON the anchor: there is no
# "losing data" band to warn ahead of. Characters, never bytes -- same unit as
# DEFAULT_MAX_INDEX_CHARS and for the same reason.
TOPIC_MAX_CHARS = 25000

# Same anchor, LINES axis (P1/P2, see INDEX_TRUNCATION_LINES above). No config
# key: unlike the index, a topic file is not injected every session, so there
# is nothing here for a session to tune away.
TOPIC_MAX_LINES = 200

# `- [Title](file.md) — hook`, the shape the index is actually written in. The
# em dash is required as VALIDATION, not for disambiguation: a looser separator
# would quietly accept a malformed row and measure a hook that is not there.
# Requiring it means a row written wrong is caught by _LOOSE_ROW_RE below and
# named as malformed, rather than passing as a healthy short hook.
#
# (An earlier comment here claimed a hyphen "would match the leading bullet of
# the next line and swallow the whole row." That is false — parsing is per-line
# via splitlines(), the pattern carries neither MULTILINE nor DOTALL, and `.`
# cannot cross a newline. The requirement is right; that reason was not.)
_ROW_RE = re.compile(r"^- \[(?P<title>.+?)\]\((?P<file>.+?)\)\s*—\s*(?P<hook>.*)$")

# Anything that LOOKS like an index row — a bullet, a title, a link — however
# its separator or hook is written. The gap between this and _ROW_RE is the set
# of malformed rows, and naming that gap is load-bearing: without it a row the
# strict pattern rejects leaves its file out of `linked`, and the file is then
# reported as an orphan "invisible to every session" while the row sits right
# there in the index. That is the notice asserting the opposite of the truth.
_LOOSE_ROW_RE = re.compile(r"^\s*[-*+]\s*\[(?P<title>.+?)\]\((?P<file>[^)]+?)\)")

# A heading line in MEMORY.md, two-level mode only. Its captured text decides
# which section the rows below it belong to -- see `parse_sections()`.
_HEADING_RE = re.compile(r"^#{1,6}\s+(\S.*?)\s*$")

# How many items to name per category before summarising. A memory directory is
# prose a human edits, so a single bad edit can make every row unparsable at
# once; enumerating 96 of them would inject more into every session than the
# hook drift this tool exists to prevent.
MAX_NAMED_PER_CATEGORY = 3

# Wiki-style links are for MEMORIES only. Anything else — a skill, a doc, a
# Python module — belongs in backticks, so a non-resolving link is a real fault.
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")

# A fenced block can carry bash `[[ -f x ]]` tests, which are not wikilinks.
_FENCE_RE = re.compile(r"```.*?```", re.S)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def _config():
    """The toolkit config module, or None if it cannot be imported.

    Imported lazily and defensively: a hook that crashes because a config helper
    moved is worse than a hook that silently does nothing.
    """
    try:
        import toolkit_config

        return toolkit_config
    except Exception:
        return None


def is_enabled(config=None):
    """False only on an explicit opt-out. Absent config means on."""
    config = config or _config()
    if config is None:
        return True
    try:
        return config.get("memory_lint", "enabled", default=True) is not False
    except Exception:
        return True


def _threshold(key, default, config=None):
    config = config or _config()
    if config is None:
        return default
    try:
        value = config.get_int("memory_lint", key, default=default)
    except Exception:
        return default
    # A non-positive threshold is treated as unset, NOT as "report everything".
    # Someone reaching for `0` is trying to silence the notice, and 0 would do
    # the opposite — every hook exceeds it, so the index would be reported in
    # full at every session start. `enabled: false` is the off switch, and
    # config.example.json says so. Same family as the `sweep_grace_sessions: 0`
    # trap recorded in this repo's CLAUDE.md, where the setting someone reaches
    # for to stop deletion caused it.
    return value if value > 0 else default


def max_hook_chars(config=None):
    return _threshold("max_hook_chars", DEFAULT_MAX_HOOK_CHARS, config)


def max_mean_hook_chars(config=None):
    return _threshold("max_mean_hook_chars", DEFAULT_MAX_MEAN_HOOK_CHARS, config)


def max_index_chars(config=None):
    # Clamped BELOW the truncation point. A cap at or above it can never be
    # crossed before rows are already being dropped, so a configured 30000
    # would silence both this notice and context-growth's while the harness
    # loses the tail -- a looser config must not be able to disarm the alarm.
    return min(_threshold("max_index_chars", DEFAULT_MAX_INDEX_CHARS, config),
               INDEX_TRUNCATION_CHARS - 1)


DEFAULT_DISTILL_AT_CHARS = 22000


def distill_at_chars(config=None):
    """Index size at which a startup/clear session is told to distill.

    Clamped below the cap, like max_index_chars(): a value at or past the cap
    would fire after the warning it is meant to precede.
    """
    cap = max_index_chars(config)
    config = config or _config()
    value = DEFAULT_DISTILL_AT_CHARS
    if config is not None:
        try:
            configured = config.get("memory_lint", "distill_at_chars",
                                    default=None)
        except Exception:
            configured = None
        if (isinstance(configured, int) and not isinstance(configured, bool)
                and configured > 0):
            value = configured
    if value < cap:
        return value
    # At or past the cap: 1000 under it, but never below half the cap (or 1).
    # A plain `cap - 1000` reaches 0 or less on a small configured cap, and a
    # threshold of 0 reads every index as due.
    return max(cap - 1000, cap // 2, 1)


def distill_skill_shipped():
    """True iff the skill the notice names exists in this toolkit checkout.

    Until Part B ships it the distill block is never emitted; it turns on by
    itself when the SKILL.md lands.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    return os.path.isfile(os.path.join(
        root, "skills", "continuity", "memory-index-distill", "SKILL.md"))


def max_index_lines(config=None):
    # Same shape and same clamp reason as max_index_chars(): a configured cap
    # at or past INDEX_TRUNCATION_LINES could never fire before the harness
    # has already dropped the tail.
    return min(_threshold("max_index_lines", DEFAULT_MAX_INDEX_LINES, config),
               INDEX_TRUNCATION_LINES - 1)


def harness_line_count(text):
    """The harness's own line count for `text` -- NOT `len(text.splitlines())`.

    P2 (binary read 2026-09-29, `GWe`/`GCt`): the harness trims the text, then
    counts `trimmed.count("\\n") + 1`, truncating only when that is strictly
    greater than 200. Blank lines at either end do not count:
    `"x\\n"*199 + "\\n\\n"` reads 199 here (`splitlines()` would say 201), and
    `""` reads 1.

    An UPPER BOUND when the text holds HTML comments: the AutoMem loader
    (`q7e`) lexes the file and strips comments before `GCt` counts it, keeping
    any inside code fences. That lexer is not reproduced here -- a second
    implementation of its rules would be its own silent defect -- so a count
    over text containing `<!--` reads high, and every finding built on it says
    so (`COMMENT_CAVEAT`). Erring high warns early; it never warns late.
    """
    t = text.strip()
    return t.count("\n") + 1


# Appended to a line finding whose index contains `<!--`. See harness_line_count.
COMMENT_CAVEAT = (
    " (Counted with HTML comments included; the harness strips them before "
    "counting, so the real count may be lower.)"
)


# `routed: false` in a memory file's own frontmatter: "this file is kept, but
# deliberately has no row in MEMORY.md." Matched only inside the leading
# frontmatter block so the same words in prose -- including this file's own
# explanation of the marker -- cannot silence an orphan by accident.
#
# The trailing `#...` is not decoration: YAML permits a comment after a scalar,
# the marker is exactly the kind of line a human annotates ("why is this file
# not routed?"), and the first version of this pattern required end-of-line
# straight after `false`. It therefore matched nothing on the files it was
# written for -- silently, because a marker that fails to parse is
# indistinguishable from a file that never carried one.
_UNROUTED_RE = re.compile(
    r"^routed:\s*false\s*(?:#.*)?$", re.IGNORECASE | re.MULTILINE
)


def _declares_unrouted(text):
    """True when a memory file's FRONTMATTER declares `routed: false`.

    Deliberately strict about where it looks. An earlier draft searched the
    whole body, which would have let any file quoting the marker -- a memory
    file documenting this very convention, for instance -- exempt itself from
    the orphan check silently. The marker is configuration, so it is only read
    where configuration lives.
    """
    if not text.startswith("---\n"):
        return False
    end = text.find("\n---", 4)
    if end == -1:
        return False
    return bool(_UNROUTED_RE.search(text[4:end]))


# N2 -- rule-placement check. `type:` decides where a memory belongs:
# feedback/user are rules (they fire every session and belong in Always);
# project/reference are facts (routing prose or elaboration, and belong in a
# topic). Anything else -- missing or unrecognised -- is left alone by this
# check entirely.
RULE_TYPES = ("feedback", "user")
FACT_TYPES = ("project", "reference")

# Matches `type: feedback` whether it sits at the top level of the frontmatter
# or nested under `metadata:` (any leading indent) -- the leading `\s*` covers
# both shapes with one pattern rather than two. The value itself may be bare,
# double-quoted, or single-quoted (three alternatives, exactly one group each
# fills), and an optional trailing YAML comment (`  # rule`) is allowed after
# it -- a MISMATCHED quote pair (`"feedback'`) matches none of the three
# alternatives and so is simply not a value this regex recognises, same as
# any other malformed line.
_TYPE_RE = re.compile(
    r'^\s*type:\s*(?:"([A-Za-z_]+)"|\'([A-Za-z_]+)\'|([A-Za-z_]+))\s*(?:#.*)?$'
)


def memory_type(text):
    """The `type:` a memory file's FRONTMATTER declares, lower-cased, or None.

    Only frontmatter starting at byte 0 counts -- the same rule
    `_declares_unrouted` uses, for the same reason: a file merely discussing
    `type:` in its body must not be misread as declaring one.
    """
    if not text or not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end == -1:
        return None
    for line in text[4:end].splitlines():
        match = _TYPE_RE.match(line)
        if match:
            value = match.group(1) or match.group(2) or match.group(3)
            return value.lower()
    return None


def _auto_memory():
    """The `auto_memory` module, or None if it cannot be imported.

    Imported lazily and defensively, same pattern as `_config()`: a hook that
    crashes because a sibling module moved is worse than a hook that silently
    falls back to its pre-N5 behaviour.
    """
    try:
        import auto_memory

        return auto_memory
    except Exception:
        return None


def memory_dir_from_transcript(transcript_path, home=None):
    """The memory directory this session should read.

    N5: the user-level `autoMemoryDirectory` setting (see `lib/auto_memory.py`)
    is consulted FIRST and wins whenever it is CONFIGURED -- even when it does
    not exist on disk yet, and even over a transcript whose own `memory/`
    folder still exists. `install.sh` points every session at one shared
    folder and a transcript's project-local folder is stale once that setting
    is in place. Deliberately `configured_dir()`, not `resolve_user_dir()`:
    the harness creates a memory folder's contents on first write, so a
    missing folder is the normal state right after `install.sh` runs and
    before any session has written to it -- treating that as "not configured"
    would fall through to a stale or unrelated fallback instead of the folder
    the setting actually names (Merge Review finding 1). The setting wins even
    when `transcript_path` is falsy: N5's whole point is that a session need
    not have a transcript-local memory folder at all. Only project- and
    local-level `autoMemoryDirectory` values are a genuinely different
    source, and they are NOT consulted here, because N5 writes the user level
    only.

    Falls back to the pre-N5 behaviour otherwise (setting absent or
    invalid): `<project-dir>/memory` for a session transcript, or None.
    Derived from the transcript rather than from `cwd` on purpose: the
    project directory name is a slug Claude Code computes, and re-deriving
    that slug here would be a second implementation of someone else's rule.
    The transcript path already sits inside the directory we want.
    """
    auto_memory = _auto_memory()
    if auto_memory is not None:
        configured = auto_memory.configured_dir(home or os.path.expanduser("~"))
        if configured is not None:
            return configured

    if not transcript_path:
        return None
    try:
        project_dir = os.path.dirname(os.path.realpath(transcript_path))
    except Exception:
        return None
    candidate = os.path.join(project_dir, "memory")
    return candidate if os.path.isdir(candidate) else None


def parse_index(text):
    """Every well-formed `- [Title](file.md) — hook` row, in order.

    Prose, headings and blank lines are ignored: the index is a document a human
    edits, and a stray note is not a defect worth a notice. A line that *looks*
    like a row but does not parse is a different thing entirely — see
    `malformed_rows`, which exists so such a line is never mistaken for its file
    being unlisted.
    """
    rows = []
    for line in (text or "").splitlines():
        match = _ROW_RE.match(line)
        if match:
            rows.append(
                {
                    "title": match.group("title"),
                    "file": match.group("file"),
                    "hook": match.group("hook").strip(),
                }
            )
    return rows


def malformed_rows(text):
    """Lines that look like index rows but do not parse, as (file, line).

    The whole point is to keep these out of the orphan count. A row written with
    an en dash, or with no hook after the link, still links its file — reporting
    that file as having "no row in MEMORY.md" would send a reader looking for a
    row that is sitting in front of them.
    """
    bad = []
    for line in (text or "").splitlines():
        if _ROW_RE.match(line):
            continue
        loose = _LOOSE_ROW_RE.match(line)
        if loose:
            bad.append((loose.group("file"), line.strip()))
    return bad


def layout(memory_dir):
    """"two-level" when `memory_dir` contains a `topics/` SUBDIRECTORY, else "flat".

    Never configured and never raises: `None`, a nonexistent path, or a plain
    file named `topics` all read as "flat", which is exactly today's
    behaviour -- and what a user who never migrates keeps.
    """
    if not memory_dir:
        return "flat"
    try:
        return "two-level" if os.path.isdir(os.path.join(memory_dir, TOPICS_DIR)) else "flat"
    except Exception:
        return "flat"


def parse_sections(text):
    """Every row -- parsed or malformed-but-linking -- tagged with its section.

    A row's section is decided by the nearest heading ABOVE it: "always" or
    "topics" when that heading's text starts with the matching word (case
    insensitive, word-boundary -- so `## Always-on` counts, because `\\b`
    falls between `s` and `-`), "other" for any other heading, and `None`
    above the first heading. This is the two-level format's routing rule; in
    flat mode nothing reads `section` and rows behave exactly as
    `parse_index` already describes them.

    Kept separate from `parse_index` rather than folded into it: flat mode's
    callers have no use for a section and must not change shape underneath
    them.
    """
    rows = []
    section = None
    for line in (text or "").splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            title = heading.group(1)
            if re.match(r"always\b", title, re.I):
                section = "always"
            elif re.match(r"topics\b", title, re.I):
                section = "topics"
            else:
                section = "other"
            continue
        match = _ROW_RE.match(line)
        if match:
            rows.append({"file": match.group("file"), "section": section, "parsed": True})
            continue
        loose = _LOOSE_ROW_RE.match(line)
        if loose:
            rows.append({"file": loose.group("file"), "section": section, "parsed": False})
    return rows


def classify_index_target(target):
    """("topic", name) | ("leaf", name) | ("bad", target) for a MEMORY.md row.

    A topic ref is exactly `topics/<name>.md` -- one path separator, nothing
    nested underneath. A leaf ref has no separator at all and is not
    `MEMORY.md` itself. Everything else -- an absolute path, `../x.md`,
    `topics/x/y.md`, a non-`.md` extension -- is bad.
    """
    t = os.path.normpath(target)
    if t.count(os.sep) == 1:
        head, name = t.split(os.sep, 1)
        if head == TOPICS_DIR and os.sep not in name and name.endswith(".md"):
            return ("topic", name)
        return ("bad", target)
    if os.sep not in t and t.endswith(".md") and t != INDEX_NAME:
        return ("leaf", t)
    return ("bad", target)


def classify_topic_target(target):
    """("leaf", name) | ("bad", target) for a row inside `topics/<x>.md`.

    Targets there are written relative to the TOPIC FILE's own directory, so
    resolving against `topics/` first is what turns `../foo.md` into the leaf
    `foo.md` -- and turns a same-directory `foo.md` (no `../`) into
    `topics/foo.md`, correctly bad: nothing under `topics/` is itself a leaf
    reference target. An absolute target resolves through unchanged and is
    bad here too.
    """
    u = os.path.normpath(os.path.join(TOPICS_DIR, target))
    if os.sep not in u and u.endswith(".md") and u != INDEX_NAME:
        return ("leaf", u)
    return ("bad", target)


def leaf_rows_outside_always(text):
    """Sorted, de-duplicated leaf names from rows in MEMORY.md that sit
    OUTSIDE the Always section (Topics, "other", or above the first heading).

    A row here still links its target -- see `parse_sections` and
    `classify_index_target` -- but does NOT make the leaf reachable; that is
    `_scan_two_level`'s job, and it computes its own `leaf_rows_outside_always`
    result by calling this function rather than reimplementing it alongside
    it. `growth_lib`'s edit-time backstop calls it directly on the text it
    just wrote, so the two tools cannot disagree about the set.
    """
    names = set()
    for entry in parse_sections(text):
        if entry["section"] == "always":
            continue
        kind, name = classify_index_target(entry["file"])
        if kind == "leaf":
            names.add(name)
    return sorted(names)


def _strip_code(text):
    return _INLINE_CODE_RE.sub(" ", _FENCE_RE.sub(" ", text))


def _read(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return None


def _scan_two_level(memory_dir, text, on_disk, result):
    """The rooted graph for a two-level memory dir -- writes straight into `result`.

    Only ever called once `layout()` has already said "two-level". `on_disk` is
    the same top-level leaf-file set the flat branch uses; `text` is
    `MEMORY.md`'s own content, already read once by the caller.

    Reachability, dead-link and duplicate-placement checks are scoped to
    NOMINATED, EXISTING topic files only. Bad targets, malformed rows, the
    per-topic report and the oversize check run over every `topics/*.md` file
    on disk, nominated or not -- a topic file's own defects are worth reporting
    whether or not anything currently points at it.
    """
    sections = parse_sections(text)

    nominated_topics = set()
    always_leaf_targets = []
    bad_targets = []
    for entry in sections:
        kind, name = classify_index_target(entry["file"])
        if kind == "topic":
            # A topic ref nominates only from the Topics section -- one row
            # under `## Always` naming a topic file does not make it reachable.
            if entry["section"] == "topics":
                nominated_topics.add(name)
        elif kind == "leaf":
            if entry["section"] == "always":
                always_leaf_targets.append(name)
        else:
            bad_targets.append(("MEMORY.md", entry["file"]))

    # Computed by the same helper `growth_lib` calls directly on an edited
    # MEMORY.md, so the two tools cannot disagree about which leaves sit
    # outside Always.
    outside_leaf_names = set(leaf_rows_outside_always(text))

    # A `*.md` DIRECTORY is not a topic file: counting it would let a Topics
    # row point at something no session can open while the scan reads clean.
    topics_path = os.path.join(memory_dir, TOPICS_DIR)
    try:
        topic_names = sorted(
            name
            for name in os.listdir(topics_path)
            if name.endswith(".md")
            and os.path.isfile(os.path.join(topics_path, name))
        )
    except Exception:
        topic_names = []

    malformed_topic = []
    oversized = []
    topics_report = []
    topic_leaf_targets = {}

    for topic_name in topic_names:
        # An unreadable topic file is treated as empty text and still listed
        # in `topics` -- not skipped, so a permissions problem shows up as an
        # empty, un-oversized entry rather than vanishing silently.
        topic_text = _read(os.path.join(memory_dir, TOPICS_DIR, topic_name))
        if topic_text is None:
            topic_text = ""
        topic_rows = parse_sections(topic_text)
        leaves_here = []
        for trow in topic_rows:
            kind, name = classify_topic_target(trow["file"])
            if not trow["parsed"]:
                malformed_topic.append((topic_name, trow["file"]))
            if kind == "leaf":
                leaves_here.append(name)
            else:
                bad_targets.append(("topics/%s" % topic_name, trow["file"]))
        is_nominated = topic_name in nominated_topics
        topic_chars = len(topic_text)
        topic_lines = harness_line_count(topic_text)
        topics_report.append(
            {
                "name": topic_name,
                "chars": topic_chars,
                "lines": topic_lines,
                "rows": len(topic_rows),
                "nominated": is_nominated,
            }
        )
        # A topic over on BOTH axes gets one entry, the chars one -- see
        # `findings()`'s oversized-topics block, which names only one unit
        # per topic.
        if topic_chars > TOPIC_MAX_CHARS:
            oversized.append((topic_name, topic_chars, "chars", TOPIC_MAX_CHARS))
        elif topic_lines > TOPIC_MAX_LINES:
            oversized.append((topic_name, topic_lines, "lines", TOPIC_MAX_LINES))
        if is_nominated:
            topic_leaf_targets[topic_name] = leaves_here

    # Placements, in file order: Always first, then topics sorted by name.
    places_by_leaf = {}
    for name in always_leaf_targets:
        places_by_leaf.setdefault(name, []).append("Always")
    for topic_name in sorted(topic_leaf_targets):
        for name in topic_leaf_targets[topic_name]:
            places_by_leaf.setdefault(name, []).append("topics/%s" % topic_name)

    reachable = set(places_by_leaf)

    # N2 -- rule-placement check. Scoped to reachable, on-disk leaves only: an
    # orphan or a dead link is already reported by the checks above, and
    # reading a file that is neither on disk nor reachable would just repeat
    # that finding under a different name.
    rules_outside_always = []
    facts_in_always = []
    for name in sorted(reachable & on_disk):
        body = _read(os.path.join(memory_dir, name))
        mtype = memory_type(body) if body is not None else None
        if mtype is None:
            continue
        places = places_by_leaf.get(name, [])
        if mtype in RULE_TYPES and "Always" not in places:
            rules_outside_always.append((name, mtype))
        elif mtype in FACT_TYPES and "Always" in places:
            facts_in_always.append((name, mtype))

    all_leaf_refs = set(always_leaf_targets) | outside_leaf_names
    for leaves in topic_leaf_targets.values():
        all_leaf_refs |= set(leaves)

    # `routed: false` exempts nothing here -- in the two-level layout nothing
    # needs to be dark, so the marker is never even read.
    result["unrouted"] = []
    result["orphans"] = sorted(on_disk - reachable - outside_leaf_names)
    result["dead_links"] = sorted(all_leaf_refs - on_disk)
    result["leaf_rows_outside_always"] = sorted(outside_leaf_names)
    result["missing_topics"] = sorted(nominated_topics - set(topic_names))
    result["unnominated_topics"] = sorted(set(topic_names) - nominated_topics)
    result["duplicate_placements"] = sorted(
        (name, places) for name, places in places_by_leaf.items() if len(places) > 1
    )
    result["bad_targets"] = sorted(bad_targets)
    result["oversized_topics"] = sorted(oversized)
    result["malformed_topic_rows"] = sorted(malformed_topic)
    result["topics"] = sorted(topics_report, key=lambda t: t["name"])
    result["rules_outside_always"] = sorted(rules_outside_always)
    result["facts_in_always"] = sorted(facts_in_always)


def scan(memory_dir, config=None):
    """Measure one memory directory. Returns a findings dict, never raises.

    An unreadable or index-less directory yields `readable: False` rather than an
    exception — the caller is a hook, and a memory directory that does not exist
    yet is the normal state of a fresh machine, not a fault.
    """
    result = {
        "readable": False,
        "index_chars": 0,
        "index_bytes": 0,
        "index_lines": 0,
        # Every key scan() can return must be initialised HERE, not only on the
        # path that computes it: there are three early returns above the
        # assignment, and `budget_lib` consumes this dict directly. `unrouted`
        # was set only at the end, so scan("/nonexistent") raised KeyError for
        # any consumer that read it.
        "unrouted": [],
        "rows": 0,
        "long_hooks": [],
        "mean_hook_chars": 0,
        "max_hook_chars_seen": 0,
        "orphans": [],
        "dead_links": [],
        "broken_wikilinks": [],
        "malformed_rows": [],
        # Two-level layout only (see `layout()`). Flat mode leaves every one of
        # these at its empty default and never touches it again -- that is what
        # makes flat findings() byte-identical to today.
        "layout": "flat",
        "leaf_rows_outside_always": [],
        "missing_topics": [],
        "unnominated_topics": [],
        "duplicate_placements": [],
        "bad_targets": [],
        "oversized_topics": [],
        "malformed_topic_rows": [],
        "topics": [],
        # N2, two-level only -- see `memory_type()` and `_scan_two_level`.
        "rules_outside_always": [],
        "facts_in_always": [],
    }
    if not memory_dir or not os.path.isdir(memory_dir):
        return result

    result["layout"] = layout(memory_dir)

    index_path = os.path.join(memory_dir, INDEX_NAME)
    text = _read(index_path)
    if text is None:
        return result
    result["readable"] = True
    # Both, and they are not interchangeable: `index_chars` is what the
    # harness truncates on and what the cap is compared against;
    # `index_bytes` is reported for humans reading `ls`/`wc -c` and must
    # never be substituted for it. See DEFAULT_MAX_INDEX_CHARS.
    result["index_chars"] = len(text)
    result["index_bytes"] = len(text.encode("utf-8"))
    result["index_lines"] = harness_line_count(text)
    result["index_has_comments"] = "<!--" in text

    rows = parse_index(text)
    result["rows"] = len(rows)

    limit = max_hook_chars(config)
    lengths = []
    for row in rows:
        length = len(row["hook"])
        lengths.append(length)
        if length > limit:
            result["long_hooks"].append((row["file"], length))
    if lengths:
        result["mean_hook_chars"] = sum(lengths) // len(lengths)
        result["max_hook_chars_seen"] = max(lengths)
    result["long_hooks"].sort(key=lambda pair: -pair[1])

    # Regular files only. No session can open a `*.md` directory, so counting
    # one would let a row naming it read as live instead of as a dead link.
    try:
        on_disk = {
            name
            for name in os.listdir(memory_dir)
            if name.endswith(".md") and name != INDEX_NAME
            and os.path.isfile(os.path.join(memory_dir, name))
        }
    except Exception:
        return result

    malformed = malformed_rows(text)
    result["malformed_rows"] = malformed

    # A malformed row still LINKS its file, so it must count toward `linked` for
    # the orphan test even though its hook could not be measured. Otherwise the
    # notice reports the file as unlisted while the row is visibly present.
    linked = {row["file"] for row in rows} | {name for name, _ in malformed}

    if result["layout"] == "flat":
        # A file may declare that it is deliberately not routed, and then it is
        # not an orphan.
        #
        # The index has a hard character budget (see DEFAULT_MAX_INDEX_CHARS),
        # so retiring a ROW while keeping the FILE is a real and correct
        # outcome: the fact stays readable, reachable by name and by
        # [[wikilink]], and stops costing every session. Without this, an index
        # cut leaves every such file reported as an orphan in
        # perpetuity -- a permanent finding nobody can action, which is
        # precisely how a notice stops being read.
        #
        # Keyed on what the FILE declares, never on a list of names kept beside
        # the code: a parallel list drifts away from the thing it describes,
        # which this repo has already paid for once. Cheap because it only
        # reads files that are otherwise about to be reported -- normally none.
        unrouted = set()
        for name in sorted(on_disk - linked):
            head = _read(os.path.join(memory_dir, name))
            if head and _declares_unrouted(head):
                unrouted.add(name)
        result["unrouted"] = sorted(unrouted)
        result["orphans"] = sorted(on_disk - linked - unrouted)
        result["dead_links"] = sorted(linked - on_disk)
    else:
        _scan_two_level(memory_dir, text, on_disk, result)

    slugs = {name[: -len(".md")] for name in on_disk}
    broken = []
    for name in sorted(on_disk):
        body = _read(os.path.join(memory_dir, name))
        if body is None:
            continue
        for target in _WIKILINK_RE.findall(_strip_code(body)):
            if target not in slugs:
                broken.append((name, target))
    result["broken_wikilinks"] = broken
    return result


def findings(result, config=None):
    """The lines worth saying, most actionable first. Empty when healthy."""
    if not result.get("readable"):
        return []

    lines = []
    long_hooks = result.get("long_hooks") or []
    if long_hooks:
        worst_file, worst_len = long_hooks[0]
        lines.append(
            "%d hook%s over %d chars (worst: %s at %d). A hook answers "
            "\"should I open this file?\" — corrections belong in the FILE, "
            "not appended to its hook."
            % (
                len(long_hooks),
                "" if len(long_hooks) == 1 else "s",
                max_hook_chars(config),
                worst_file,
                worst_len,
            )
        )

    mean_limit = max_mean_hook_chars(config)
    mean = result.get("mean_hook_chars") or 0
    if mean > mean_limit:
        lines.append(
            "mean hook is %d chars against a ~120-160 target — the index is "
            "becoming a second copy of the corpus." % mean
        )

    def summarise(items, one, many):
        """Name a few, then count the rest.

        Uncapped enumeration is how this tool would become the thing it detects:
        one bad edit can invalidate every row at once, and a very long notice
        costs more per session than the drift it is reporting.
        """
        if not items:
            return
        named = items[: MAX_NAMED_PER_CATEGORY]
        if len(items) == 1:
            lines.append(one % named[0])
            return
        rest = len(items) - len(named)
        text = many % (len(items), ", ".join(named))
        lines.append(text + (" (+%d more)" % rest if rest > 0 else ""))

    summarise(
        result.get("malformed_rows") and [n for n, _ in result["malformed_rows"]] or [],
        "%s has a row in MEMORY.md that does not parse — check its separator "
        "(the index uses an em dash) and that a hook follows it.",
        "%d rows in MEMORY.md do not parse (%s) — check their separators; the "
        "index uses an em dash, and a hook must follow it.",
    )

    # Two-level-only categories. Every list here stays empty in flat mode (see
    # `scan()`'s dict literal), so each `summarise()` call below is a no-op
    # there and flat findings() output is unaffected.
    summarise(
        ["topics/%s: %s" % pair for pair in (result.get("malformed_topic_rows") or [])],
        "%s — a row in a topic file that does not parse; check its separator "
        "(the index uses an em dash) and that a hook follows it.",
        "%d rows in topic files do not parse (%s) — check their separators.",
    )
    summarise(
        result.get("leaf_rows_outside_always") or [],
        "%s has a row in MEMORY.md outside the Always section — move it to its "
        "topic file, or into Always if it is a rule.",
        "%d leaf rows in MEMORY.md sit outside the Always section (%s) — move "
        "each to its topic file, or into Always if it is a rule.",
    )
    summarise(
        ["%s (type: %s)" % pair for pair in (result.get("rules_outside_always") or [])],
        "%s is a rule reachable only from a topic — it never fires there. Move "
        "its row to Always, or retype it reference if CLAUDE.md carries the "
        "rule.",
        "%d rule memories are reachable only from a topic (%s) — they never "
        "fire there. Move each row to Always, or retype it reference if "
        "CLAUDE.md carries the rule.",
    )
    summarise(
        ["%s (type: %s)" % pair for pair in (result.get("facts_in_always") or [])],
        "%s is a fact with a row in Always — it is paid every session. Move "
        "its row to its topic, or retype it feedback if it is a rule.",
        "%d fact memories have a row in Always (%s) — each is paid every "
        "session. Move each row to its topic, or retype it feedback if it is "
        "a rule.",
    )
    summarise(
        ["topics/%s" % name for name in (result.get("missing_topics") or [])],
        "MEMORY.md's Topics section points at %s, which does not exist.",
        "MEMORY.md's Topics section points at %d topic files that do not "
        "exist (%s).",
    )
    summarise(
        ["topics/%s" % name for name in (result.get("unnominated_topics") or [])],
        "%s has no row in MEMORY.md's Topics section — nothing it lists is "
        "reachable.",
        "%d topic files have no row in MEMORY.md's Topics section (%s) — "
        "nothing they list is reachable.",
    )
    summarise(
        [
            "%s (%s)" % (leaf, ", ".join(places))
            for leaf, places in (result.get("duplicate_placements") or [])
        ],
        "%s is listed in more than one place — keep one row.",
        "%d memories are listed in more than one place (%s) — keep one row "
        "each.",
    )
    summarise(
        ["%s: %s" % pair for pair in (result.get("bad_targets") or [])],
        "%s — a row whose link is not a memory in this directory (topic files "
        "link leaves as ../<name>.md).",
        "%d rows link to something that is not a memory in this directory "
        "(%s) — topic files link leaves as ../<name>.md.",
    )
    oversized_topics = result.get("oversized_topics") or []
    if oversized_topics:
        # Bespoke rather than summarise(): both forms need the unit AND the
        # cap as extra placeholders per item, and summarise()'s
        # (item) / (count, joined) signature has no slot for either.
        named = [
            "topics/%s at %d %s" % (name, count, unit)
            for name, count, unit, _cap in oversized_topics[:MAX_NAMED_PER_CATEGORY]
        ]
        if len(oversized_topics) == 1:
            _name, count, unit, cap = oversized_topics[0]
            lines.append(
                "topics/%s at %d %s, over the %d-%s topic cap — split the topic."
                % (_name, count, unit, cap, unit)
            )
        else:
            rest = len(oversized_topics) - len(named)
            text = (
                "%d topic files are over their cap (%s) — split them."
                % (len(oversized_topics), ", ".join(named))
            )
            lines.append(text + (" (+%d more)" % rest if rest > 0 else ""))

    # Orphan and dead-link wording differ by layout: flat mode names the single
    # file that could carry a row (MEMORY.md); two-level mode has two places a
    # row could live, so "no row in MEMORY.md" would be misleading -- a leaf
    # can have no row in MEMORY.md and still be perfectly reachable from a
    # topic file.
    if result.get("layout") == "two-level":
        orphan_one = (
            "%s is reachable from no row — not in Always, not in any topic "
            "file. Invisible to every session."
        )
        orphan_many = (
            "%d files are reachable from no row (%s) — not in Always, not in "
            "any topic file. Invisible to every session."
        )
        dead_one = "a row points at %s, which does not exist."
        dead_many = "rows point at %d files that do not exist (%s)."
    else:
        orphan_one = "%s has no row in MEMORY.md — invisible to every session."
        orphan_many = (
            "%d files have no row in MEMORY.md (%s) — invisible to every session."
        )
        dead_one = "MEMORY.md points at %s, which does not exist."
        dead_many = "MEMORY.md points at %d files that do not exist (%s)."

    summarise(result.get("orphans") or [], orphan_one, orphan_many)
    summarise(result.get("dead_links") or [], dead_one, dead_many)

    broken = ["%s -> [[%s]]" % pair for pair in (result.get("broken_wikilinks") or [])]
    summarise(
        broken,
        "%s resolves to no memory.",
        "%d wikilinks resolve to no memory (%s).",
    )

    # The whole-index budget, reported LAST so it is the line left on screen.
    #
    # Two bands, and conflating them is the mistake this replaces: over the
    # budget is a thing to fix soon, over the truncation point is data being
    # lost right now. The second says so in those words, because the failure
    # is otherwise invisible -- the rows do not arrive and nothing errors.
    #
    # N3b adds a second axis (lines) with the SAME two bands. Loss is decided
    # across BOTH axes before either finding is worded, so a mixed case never
    # says "Nothing is being dropped yet" on one axis while the other axis is
    # already losing the tail.
    chars = result.get("index_chars") or 0
    cap_chars = max_index_chars(config)
    index_lines = result.get("index_lines") or 0
    cap_lines = max_index_lines(config)
    losing_lines = index_lines > INDEX_TRUNCATION_LINES
    losing_chars = chars >= INDEX_TRUNCATION_CHARS

    if index_lines > cap_lines:
        over_lines = index_lines - cap_lines
        if losing_lines:
            past_lines = index_lines - INDEX_TRUNCATION_LINES
            lines.append(
                "MEMORY.md is %d lines against a %d cap and PAST the "
                "200-line point where the harness silently drops the END of "
                "the index -- the last %d line(s) are not reaching sessions "
                "at all, with no error anywhere. Cut rows now."
                % (index_lines, cap_lines, past_lines)
            )
        elif losing_chars:
            lines.append(
                "MEMORY.md is %d lines against a %d cap (%d over). The index "
                "is ALREADY losing its tail on characters -- see the next "
                "line." % (index_lines, cap_lines, over_lines)
            )
        else:
            lines.append(
                "MEMORY.md is %d lines against a %d cap (%d over). Nothing "
                "is being dropped yet -- the harness truncates past 200 "
                "lines -- so this is the window in which to retire a row "
                "rather than lose one off the tail."
                % (index_lines, cap_lines, over_lines)
            )
        if result.get("index_has_comments"):
            lines[-1] += COMMENT_CAVEAT

    if chars > cap_chars:
        rows = result.get("rows") or 0
        # A row estimate needs a row count. With `rows == 0` an earlier version
        # fell back to a divisor of 1 CHARACTER and printed the character count
        # as a row count -- "the last ~N row(s) are not reaching sessions"
        # with N far larger than the index, alongside "a row is worth ~0 chars". `rows == 0`
        # on a big index is not exotic: one wrong separator in a distill pass
        # makes every row unparsable at once, which is the premise the
        # malformed-row check above is built on. This notice is the ONLY signal
        # the truncation has, so it says less rather than something false.
        mean_row = (chars // rows) if rows else 0
        if mean_row:
            past = max(1, (chars - INDEX_TRUNCATION_CHARS) // mean_row + 1)
            over = max(1, (chars - cap_chars) // mean_row)
            loud_tail = (
                " -- the last ~%d row(s) are not reaching sessions at all, with "
                "no error anywhere. Cut rows now; a row is worth ~%d chars."
                % (past, mean_row)
            )
            soft_tail = " (%d over, ~%d row(s))" % (chars - cap_chars, over)
        else:
            loud_tail = (
                " -- rows are being lost off the end with no error anywhere. No "
                "row in this index parses, so the number of lost rows cannot be "
                "estimated; fix the malformed rows above first."
            )
            soft_tail = " (%d over)" % (chars - cap_chars)

        if chars >= INDEX_TRUNCATION_CHARS:
            lines.append(
                "MEMORY.md is %d chars against a %d cap and PAST the ~%d point "
                "where the harness silently drops rows off the END of the index"
                "%s" % (chars, cap_chars, INDEX_TRUNCATION_CHARS, loud_tail)
            )
        elif losing_lines:
            # The lines finding above already carries the loud wording; this
            # axis is merely over budget, not yet past ITS OWN truncation
            # point, so it points at the line above rather than repeating it.
            lines.append(
                "MEMORY.md is %d chars against a %d cap%s. The index is "
                "ALREADY losing its tail on lines -- see the line above."
                % (chars, cap_chars, soft_tail)
            )
        else:
            lines.append(
                "MEMORY.md is %d chars against a %d cap%s. Nothing is being "
                "dropped yet -- the harness truncates around %d -- so this is "
                "the window in which to retire a row rather than lose one off "
                "the tail." % (chars, cap_chars, soft_tail, INDEX_TRUNCATION_CHARS)
            )
    return lines


def _distill_claim():
    try:
        import distill_claim

        return distill_claim
    except Exception:
        return None


_DISTILL_IN_PROGRESS = (
    "MEMORY INDEX NEAR CHARACTER LIMIT — distillation in progress")


def _distill_block(result, config, session_id):
    """The near-cap block, or None when the index is below the trigger."""
    if not result.get("readable"):
        return None
    chars = result.get("index_chars") or 0
    threshold = distill_at_chars(config)
    if chars < threshold:
        return None
    holder = None
    claims = _distill_claim()
    if claims is not None:
        try:
            holder = claims.read_live()
        except Exception:
            holder = None
    if holder is not None and holder.get("session_id") != session_id:
        return (
            _DISTILL_IN_PROGRESS + " in another "
            "session since %s. Nothing to do here. Don't write memory until "
            "it finishes." % claims.local_hhmm(holder)
        )
    return (
        "MEMORY INDEX NEAR CHARACTER LIMIT — MEMORY.md is %d / %d chars (distill at %d). "
        "Past ~25,000 the harness drops rows silently. In your FIRST reply, "
        "tell the user and offer three choices: (1) make this the "
        "distillation session (run /toolkit-continuity:memory-index-distill); "
        "(2) hand off: give a one-line kickoff for a dedicated session and "
        "carry on here; (3) later. Distillation pauses the other running "
        "sessions via /handover, so it runs in a session of its own."
        % (chars, max_index_chars(config), threshold)
    )


def build(memory_dir, config=None, source=None, session_id=None):
    """The notice text for a session, or None when there is nothing to say."""
    if not is_enabled(config):
        return None
    result = scan(memory_dir, config)
    block = None
    if source in ("startup", "clear") and distill_skill_shipped():
        block = _distill_block(result, config, session_id)
    lines = findings(result, config)
    drift = None
    if lines:
        body = "\n".join("- " + line for line in lines)
        drift = (
            "MEMORY INDEX DRIFT — MEMORY.md is injected every session, so its "
            "hooks are the only part of memory with a standing token cost.\n"
            + body
            + "\nCompress from the FILES, never from the old hooks: a hook "
            "rewritten from a hook re-blesses whatever framing its file "
            "already corrected."
        )
    if block and block.startswith(_DISTILL_IN_PROGRESS):
        # Another session is distilling: the drift notice's "compress" would
        # tell this one to write memory while it is told not to.
        return block
    if block and drift:
        return block + "\n\n" + drift
    return block or drift


def _budget_state(result, cap):
    """The budget column: spare, or over, and a row estimate only when it exists.

    The over/under branch keys on the COMPARISON, never on the row count. An
    earlier version guarded both with `chars <= cap and rows`, so a readable
    index with no parsable row fell through to the over-budget branch and
    printed `OVER by -23970` -- a negative overage, two lines above
    `status healthy`. `install.sh` provisions this tool, so that was a
    new user's first impression of it.
    """
    chars = result.get("index_chars") or 0
    rows = result.get("rows") or 0
    mean_row = (chars // rows) if rows else 0
    if chars > cap:
        return "OVER by %d" % (chars - cap)
    spare = cap - chars
    if mean_row:
        return "%d spare, ~%d row(s)" % (spare, spare // mean_row)
    return "%d spare" % spare


def _spare_or_over(value, cap):
    """The simple spare/over column, with no row estimate -- used by the LINES
    rows, where a value already counts lines directly rather than needing one
    derived from a mean row length."""
    if value > cap:
        return "OVER by %d" % (value - cap)
    return "%d spare" % (cap - value)


def _distill_state(result, config):
    threshold = distill_at_chars(config)
    chars = result.get("index_chars") or 0
    if chars >= threshold:
        return "%d (due)" % threshold
    return "%d (%d to go)" % (threshold, threshold - chars)


def report(memory_dir, config=None):
    """Full human-readable measurement, printed whether or not anything is wrong."""
    result = scan(memory_dir, config)
    if not result.get("readable"):
        return "No readable MEMORY.md in %s" % (memory_dir or "<no directory>")

    lines = [
        "memory dir      %s" % memory_dir,
        "index           %d chars across %d rows (%d bytes on disk)"
        % (result["index_chars"], result["rows"], result["index_bytes"]),
        "index budget    %d / %d chars — %s (harness drops rows past ~%d)"
        % (
            result["index_chars"],
            max_index_chars(config),
            _budget_state(result, max_index_chars(config)),
            INDEX_TRUNCATION_CHARS,
        ),
        "distill at      %s" % _distill_state(result, config),
        "index lines     %d / %d lines — %s (harness drops lines past ~%d)"
        % (
            result["index_lines"],
            max_index_lines(config),
            _spare_or_over(result["index_lines"], max_index_lines(config)),
            INDEX_TRUNCATION_LINES,
        ),
        "hook chars      mean %d, max %d (target ~120-160, notice at %d)"
        % (
            result["mean_hook_chars"],
            result["max_hook_chars_seen"],
            max_hook_chars(config),
        ),
    ]
    if result.get("layout") == "two-level":
        topics = result.get("topics") or []
        lines.append(
            "layout          two-level — %d topic file(s) under topics/"
            % len(topics)
        )
        for topic in topics:
            chars = topic["chars"]
            topic_lines = topic["lines"]
            over_units = []
            if chars > TOPIC_MAX_CHARS:
                over_units.append("chars")
            if topic_lines > TOPIC_MAX_LINES:
                over_units.append("lines")
            if over_units:
                state = "OVER (%s)" % ", ".join(over_units)
            else:
                state = "%d chars spare" % (TOPIC_MAX_CHARS - chars)
            lines.append(
                "topic           %s  %d / %d chars, %d / %d lines — %s, "
                "%d row(s)%s"
                % (
                    topic["name"],
                    chars,
                    TOPIC_MAX_CHARS,
                    topic_lines,
                    TOPIC_MAX_LINES,
                    state,
                    topic["rows"],
                    "" if topic["nominated"] else ", NOT NOMINATED",
                )
            )
    issues = findings(result, config)
    if not issues:
        lines.append("status          healthy — nothing to report")
    else:
        lines.append("status          %d finding(s)" % len(issues))
        lines.extend("  - " + issue for issue in issues)
    if result["long_hooks"]:
        lines.append("over-length hooks:")
        lines.extend(
            "  %5d  %s" % (length, name) for name, length in result["long_hooks"]
        )
    return "\n".join(lines)
