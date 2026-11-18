#!/usr/bin/env python3
"""Charge growth in a context file to the session that caused it.

**Why this hook is the load-bearing piece of the context contract.**
A workspace root is usually not a git repository, so `claude-context-lint
--staged` can never see `<workspace_root>/CLAUDE.md` or
`<workspace_root>/TOOLING.md`. **Unreachability is the reason this hook exists,
not any particular file being over cap today.** A file no check can reach is the
one free to grow past its cap unnoticed (for `TOOLING.md` the budget that matters
is the digest's `tooling_digest.max_lines`, NOT the `core_max_lines` that governs
the CLAUDE.md family, which is a different number), and this is the only
mechanism that reaches them.

Two design decisions, both forced by BLOCKING findings, and neither safe to
"simplify":

**D1 — a file outside git needs a path-keyed cap map of its own.**
`context_lint_lib.cap_for` opens `if not key: return default_max` — it returns
BEFORE consulting overrides, and `cap_key` is `None` for anything outside a repo
by construction. So no per-file cap was expressible for either target file, and
the design's ratchet did not exist for them. Do NOT "fix" `cap_for`: its
exactness is load-bearing and its docstring spends thirty lines on the three ways
suffix matching failed. A repo-keyed map and a path-keyed map have different key
shapes and stay separate maps.

**D2 — the hook fires on a TRANSITION, not on a state.** A file over its cap
stays over until its content is cut, so a state check would fire on every edit
to it — including the edits that shrink it, and every edit the cut itself
makes. That is the notice-nobody-reads failure the contract indicts, rebuilt as
a hook. `PostToolUse` receives the tool input, not the pre-edit content, so the
prior size must be RECORDED.

Advisory always. `PostToolUse` cannot block, and it should not: the point is
attribution, not refusal.

**This module never imports `toolkit_config`.** `config=None` means shipped
defaults, exactly as in `context_lint_lib`; the executable loads the config and
passes it down. That is what makes this hermetic by construction rather than by
test discipline. The tooling-map Core cap obeys the same rule and now lives in
`context_lint_lib.core_cap_for`, shared with the pre-commit check so the two can
never disagree about the number.
"""

import json
import os
import re
import sys
import tempfile

# realpath, not abspath: this is reached through a ~/.local/bin symlink, and
# abspath would land on /Users and fail at runtime while passing every test.
_HERE = os.path.dirname(os.path.realpath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_ROOT, "lib"),
           os.path.join(_ROOT, "tools", "context-lint"),
           os.path.join(_ROOT, "tools", "tooling-digest"),
           os.path.join(_ROOT, "tools", "memory-routing"),
           os.path.join(_ROOT, "tools", "memory-lint"),
           os.path.join(_ROOT, "tools", "context-budget")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import claude_home  # noqa: E402
import context_lint_lib  # noqa: E402
import core_block  # noqa: E402
import digest_lib  # noqa: E402
import lint_lib  # noqa: E402
import routing_lib  # noqa: E402

DEFAULT_STATE_PATH = "~/.claude/context-growth-state.json"

# Exists so the test suite does not write to the real state file. A suite that
# mutates live state is the ambient-state defect in another costume.
STATE_ENV = "CLAUDE_CONTEXT_GROWTH_STATE"

# N2's per-memory-directory misplaced-set baseline lives in the SAME state
# dict, under this key prefix plus the memory dir's realpath -- not a file
# path, so `write_state`'s existence-based pruning has its own carve-out for
# this prefix (below). Shared here so the two cannot drift apart.
MISPLACED_DIR_PREFIX = "misplaced-dir:"

# N6's always-on-total ratchet lives in the SAME state dict, under this
# prefix plus the workspace root's own realpath -- not a file path either,
# so `write_state`'s existence-based pruning needs the same carve-out as
# `MISPLACED_DIR_PREFIX` (below).
ALWAYS_ON_PREFIX = "always-on-total:"

# D-M1: memory entries get a whole-file cap and no Core cap at all.
DEFAULT_MEMORY_MAX_LINES = 280

# memory-distill's Step 0 triage: a file this many lines or more over its cap
# gets a full distillation, one closer to the cap the cheaper delete pass. At
# the default cap that is 281-299 vs 300+. See `memory_route`.
DISTILL_MARGIN = 20

# A rules file without `paths:` loads at session_start in every session,
# exactly like CLAUDE.md, so it gets the same whole-file-only treatment as a
# memory entry: a cap and no Core cap at all (see caps_for_path).
DEFAULT_RULES_MAX_LINES = 200


def is_memory_file(path):
    """True for a memory entry (`memory/<name>.md`). Never raises.

    Delegates to `routing_lib.is_memory_write` for the same reason
    `_tooling_name` delegates to `context_lint_lib`: this and `memory-routing`
    must never disagree about which files are memory entries. This function
    used to carry its own copy of the regex and the `MEMORY.md` exclusion —
    byte-identical to routing_lib's, with nothing relating the two — and a
    Merge Review called it under the repo's one-source-of-truth rule. The
    `lib/worktree_detect.sh` entry in CLAUDE.md is what that shape costs when
    one copy gets fixed and the sibling does not.

    What stays here is this function's own contract, which routing_lib's does
    not make: normalize `os.sep` so a Windows-shaped path still matches, and
    never raise (the caller is a hook).
    """
    if not path:
        return False
    try:
        normalized = str(path).replace(os.sep, "/")
    except Exception:
        return False
    return routing_lib.is_memory_write(normalized)


_TOPIC_FILE_RE = re.compile(r"/memory/" + lint_lib.TOPICS_DIR + r"/[^/]+\.md$")


def is_topic_file(path):
    """True for a topic file (`memory/topics/<name>.md`). Never raises.

    Same shape as `is_memory_index`: normalize `os.sep`, then match on suffix.
    `is_memory_file` (and `routing_lib.is_memory_write`, which it delegates
    to) stays False for a topic file -- `MEMORY_DIR_RE` requires exactly one
    path segment between `memory/` and the filename, and `topics/<name>.md`
    is two.
    """
    if not path:
        return False
    try:
        normalized = str(path).replace(os.sep, "/")
    except Exception:
        return False
    return bool(_TOPIC_FILE_RE.search(normalized))


def is_rules_file(path):
    """True for any `.md` under a `.claude/rules/` directory, at any depth,
    OR under a relocated `$CLAUDE_CONFIG_DIR/rules/` (see `lib/claude_home.py`
    -- the shared resolver). Never raises.

    Normalizes `os.sep` like `is_memory_file`, and matches on the segment
    `/.claude/rules/` rather than a basename, since the file's own name
    carries no signal here. The relocated-home branch tries TWO compares,
    either of which is enough: the LOGICAL one first (normalized path
    against `claude_home.user_rules_dir()` as a trailing-slash-terminated
    prefix, so `rules-old/x.md` sitting next to the real `rules/` dir is NOT
    a match), then a REALPATH one against the realpath of `user_rules_dir()`
    -- needed when `CLAUDE_CONFIG_DIR` itself is a symlink (Merge Review):
    the logical compare only matches a path reached THROUGH the symlink, not
    the physical path underneath it. Reading `CLAUDE_CONFIG_DIR` here does
    not conflict with this module's "never imports toolkit_config" rule --
    that rule is about the toolkit's OWN config, not Claude Code's. An
    exception from either resolver falls back to the other check alone.
    """
    if not path:
        return False
    try:
        normalized = str(path).replace(os.sep, "/")
    except Exception:
        return False
    if not normalized.endswith(".md"):
        return False
    if "/.claude/rules/" in normalized:
        return True
    try:
        candidate = os.path.normpath(os.path.abspath(str(path))).replace(os.sep, "/")
        prefix = os.path.normpath(
            os.path.abspath(claude_home.user_rules_dir())
        ).replace(os.sep, "/").rstrip("/") + "/"
        if candidate.startswith(prefix):
            return True
    except Exception:
        pass
    try:
        real_candidate = os.path.realpath(str(path)).replace(os.sep, "/")
        real_prefix = os.path.realpath(
            claude_home.user_rules_dir()
        ).replace(os.sep, "/").rstrip("/") + "/"
        return real_candidate.startswith(real_prefix)
    except Exception:
        return False


def is_memory_index(path):
    """True for the memory index itself (`memory/MEMORY.md`). Never raises.

    Deliberately separate from `is_memory_file` -- `MEMORY.md` is a routing
    index, not a memory entry, and gets a different cap on a different unit
    (characters, via `index_max_chars`, not lines). `is_memory_file` stays
    False for it.
    """
    if not path:
        return False
    try:
        normalized = str(path).replace(os.sep, "/")
    except Exception:
        return False
    return normalized.endswith("/memory/" + lint_lib.INDEX_NAME)


_PATHS_KEY_RE = re.compile(r"^paths\s*:")


def has_paths_frontmatter(text):
    """True iff `text` opens with a line-1 `---` block containing a `paths:`
    key. Never raises.

    Detects *key present inside a line-1 `---` block*, nothing more: `paths:
    []` counts as scoped (an accepted limitation — Claude Code's own
    frontmatter semantics for an empty value are unverified), an indented
    `  paths:` does not count (it must open the line), and `paths:` appearing
    only in the body after the block closes does not count either.

    One malformation IS detected: Anthropic's rule reference says
    malformed YAML frontmatter is ignored and the rule loads as if it had no
    `paths`, and the toolkit is stdlib-only, so no YAML parser. When the
    matched `paths:` line's value (the text after the first `:`, stripped of
    whitespace and a trailing `\\r`) starts with `[` and does not end with
    `]`, that is an unclosed flow list — the whole file counts as pathless.
    Every other invalid-YAML shape still counts as scoped; this is a narrow,
    single-case detector, not a parser.
    """
    try:
        if not isinstance(text, str):
            return False
        lines = text.split("\n")
        if not lines or lines[0].rstrip("\r") != "---":
            return False
        found_paths = False
        for line in lines[1:]:
            stripped = line.rstrip("\r")
            if stripped == "---":
                return found_paths
            if _PATHS_KEY_RE.match(stripped):
                found_paths = True
                _, _, value = stripped.partition(":")
                value = value.strip()
                if value.startswith("[") and not value.endswith("]"):
                    return False  # unclosed flow list -- malformed
        return False  # never closed -- an unclosed block is not frontmatter
    except Exception:
        return False


def is_claude_md(path):
    """True for a CLAUDE.md-family file: the file itself or the template.

    Deliberately NOT the tooling map, which is also in_scope but has a
    different remedy -- its body is already on-demand, so it is trimmed
    downward rather than distilled to an archive. And deliberately not
    `CLAUDE.history.md`, which is the archive: basename equality, never a
    prefix, or the notice would fire on the thing it tells you to write to.
    """
    if not path:
        return False
    try:
        name = os.path.basename(str(path).replace(os.sep, "/"))
    except Exception:
        return False
    return name in ("CLAUDE.md", "CLAUDE.md.template")


def _tooling_name(config=None):
    """Basename of the tooling map. Never raises.

    Delegates: this and `context_lint` must never disagree about which file is
    the map, because they must never disagree about its Core cap.
    """
    return context_lint_lib.tooling_name(config)


def in_scope(path, config=None):
    """True for a file the growth notice applies to.

    Basename-only for the CLAUDE.md / TOOLING.md family, no read — the
    original promise. A memory entry is matched on the fuller `.../memory/
    <name>.md` shape instead (still no read; see `is_memory_file`), since a
    basename check alone cannot tell a memory entry from any other `.md`.
    A rules file is matched the same way, on the `.../.claude/rules/...`
    shape (see `is_rules_file`) — it, too, loads at session start whenever
    it lacks `paths:` frontmatter, exactly like CLAUDE.md. The memory index
    (`memory/MEMORY.md`) is also in scope, on the `is_memory_index` shape --
    it is injected every session, same as CLAUDE.md, just capped in
    characters rather than lines. A topic file (`memory/topics/<name>.md`) is
    also in scope, on the `is_topic_file` shape -- it is read whole whenever
    it is opened, so its own char cap belongs on the same edit-time backstop.
    """
    if not path:
        return False
    try:
        name = os.path.basename(str(path).replace(os.sep, "/"))
    except Exception:
        return False
    return (name in ("CLAUDE.md", "CLAUDE.md.template", _tooling_name(config))
            or is_memory_file(path)
            or is_rules_file(path)
            or is_memory_index(path)
            or is_topic_file(path))


def is_enabled(config=None):
    """Defaults to True. A missing key must never turn the hook off silently."""
    if config is None:
        return True
    try:
        value = config.get("context_growth", "enabled", default=True)
    except Exception:
        return True
    return value is not False


def memory_max_lines(config=None):
    """The whole-file cap for a memory entry (D-M1). Defaults to 280.

    Mirrors `is_enabled`'s defensive shape: a missing or unusable config
    means the default, and never raises. A configured value that is not a
    positive `int` is treated as absent rather than honoured verbatim — a
    malformed config narrows what it trusts, per this repo's own rule.
    """
    if config is None:
        return DEFAULT_MEMORY_MAX_LINES
    try:
        value = config.get_int("context_growth", "memory_max_lines",
                               default=DEFAULT_MEMORY_MAX_LINES)
    except Exception:
        return DEFAULT_MEMORY_MAX_LINES
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return DEFAULT_MEMORY_MAX_LINES
    return value


def rules_max_lines(config=None):
    """The whole-file cap for a `.claude/rules/` entry. Defaults to 200.

    Exact mirror of `memory_max_lines`: a missing or unusable config means the
    default, never raises, and a configured value that is not a positive
    `int` is treated as absent rather than honoured verbatim.
    """
    if config is None:
        return DEFAULT_RULES_MAX_LINES
    try:
        value = config.get_int("context_growth", "rules_max_lines",
                               default=DEFAULT_RULES_MAX_LINES)
    except Exception:
        return DEFAULT_RULES_MAX_LINES
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        return DEFAULT_RULES_MAX_LINES
    return value


def index_max_chars(config=None):
    """The whole-index cap for `memory/MEMORY.md`, in CHARACTERS. Defaults to
    `lint_lib.DEFAULT_MAX_INDEX_CHARS`.

    The number is memory-lint's, read through `lint_lib.max_index_chars` from
    `memory_lint.max_index_chars` -- one config key shared by both tools, so
    they cannot disagree. No re-validation of the value happens here:
    `lint_lib._threshold` already maps a non-positive configured value to the
    default.

    `config is None` returns the shipped default directly, and must NEVER
    call `lint_lib.max_index_chars(None)` -- that call does
    `config = config or _config()`, which imports `toolkit_config` and reads
    the LIVE user config. This module never imports `toolkit_config` (see the
    module docstring); the executable loads config and passes it down, and a
    `None` here means "no config was passed", not "go look one up".
    """
    if config is None:
        return lint_lib.DEFAULT_MAX_INDEX_CHARS
    try:
        return lint_lib.max_index_chars(config)
    except Exception:
        return lint_lib.DEFAULT_MAX_INDEX_CHARS


def index_max_lines(config=None):
    """The whole-index cap for `memory/MEMORY.md`, in LINES -- the harness's
    own count (`lint_lib.harness_line_count`), never `len(text.splitlines())`.
    Defaults to `lint_lib.DEFAULT_MAX_INDEX_LINES`.

    Exact mirror of `index_max_chars`: the number is memory-lint's, read
    through `lint_lib.max_index_lines` from `memory_lint.max_index_lines` --
    the SAME config key `lint_lib` itself reads for its own line check, so the
    two tools cannot disagree. `config is None` returns the shipped default
    directly, and must NEVER call `lint_lib.max_index_lines(None)` -- that
    call does `config = config or _config()`, which imports `toolkit_config`
    and reads the LIVE user config. This module never imports
    `toolkit_config` (see the module docstring); the executable loads config
    and passes it down, and a `None` here means "no config was passed", not
    "go look one up".
    """
    if config is None:
        return lint_lib.DEFAULT_MAX_INDEX_LINES
    try:
        return lint_lib.max_index_lines(config)
    except Exception:
        return lint_lib.DEFAULT_MAX_INDEX_LINES


def memory_route(lines, cap):
    """memory-distill's triage route for a file of `lines` under `cap`.

    `under-cap` at or below the cap -- the notice's own test is `lines > cap`
    (`_rose_over_cap`), so a file AT its cap is not over it. `delete-pass`
    for fewer than DISTILL_MARGIN lines over, `distill` beyond. The skill
    follows this route rather than restating the numbers, so a configured
    cap moves the skill and the notice together.
    """
    if lines <= cap:
        return "under-cap"
    if lines - cap < DISTILL_MARGIN:
        return "delete-pass"
    return "distill"


def memory_cap_report(path, config=None):
    """`(lines, cap, route)` for a memory entry, or None.

    Read through the same `observe` and `caps_for_path` the notice uses, so
    the count and the cap are the notice's own, per-file overrides included.
    None when `path` is not a memory entry or cannot be read.
    """
    if not is_memory_file(path):
        return None
    current = observe(path, config)
    if current is None:
        return None
    cap, _core = caps_for_path(path, config)
    if cap is None:
        return None
    return current["lines"], cap, memory_route(current["lines"], cap)


def _override_lookup(path, table):
    """`table[path]`, comparing realpath'd and expanduser'd keys on BOTH sides.

    Both sides, for the reason `cap_key` documents: on macOS `/var` is a symlink
    to `/private/var`, so resolving only one side makes the key match nothing,
    silently, in any temp directory. And `expanduser` on the CONFIGURED key
    because `toolkit_config.get()` does not expand paths — only `get_path()`
    does, and only for a scalar string — so a map written the way a human writes
    it ("~/workspace/CLAUDE.md") arrives verbatim and must be expanded here.
    """
    if not isinstance(table, dict):
        return None
    try:
        target = os.path.realpath(os.path.expanduser(str(path)))
    except Exception:
        return None
    for candidate, limit in table.items():
        if not isinstance(limit, int) or limit <= 0:
            continue
        try:
            resolved = os.path.realpath(os.path.expanduser(str(candidate)))
        except Exception:
            continue
        if resolved == target:
            return limit
    return None


def caps_for_path(path, config=None):
    """`(whole_file_cap, core_cap)`; `None` where that cap does not apply.

    D1's resolution order:

      1. `cap_key(path)` — the canonical `<repo>/<path-within-repo>` form.
      2. Key PRESENT -> resolve through `context_lint`'s repo-keyed maps. A
         versioned file already has a working ratchet; it does not get a second
         one, so `context_growth.overrides` is deliberately NOT consulted here.
      3. Key `None` (unversioned) -> the path-keyed `context_growth` maps,
         falling back to the `context_lint` defaults.

    The tooling map has NO whole-file cap. Wiring it to the `context_lint`
    default would report a long map far over the cap on every single edit, and
    a notice that fires constantly is a notice nobody reads. (Deliberately not
    a figure: the overage is the file's length minus whatever DEFAULT_MAX_LINES
    currently is, and a hardcoded difference goes stale silently every time
    the cap moves.)
    Its Core cap is `tooling_digest.max_lines` — the number that already exists
    and is where `digest_lib.cap_block()` stops injecting the block at all.
    (It trimmed to that number until 2026-09-14; now it withholds the whole
    block, which makes this notice the only warning that arrives BEFORE the
    map goes dark rather than with it.)

    Never raises; any malformed config falls back to shipped defaults.

    The memory INDEX is resolved before even the memory-entry branch: `(None,
    None)` always -- no whole-file LINE cap (ruled 2026-09-22 -- chars
    only; lines cannot bind first), no Core cap, and `context_growth.overrides`
    / `core_overrides` are deliberately not consulted because both are LINE
    maps. The char cap lives in `index_max_chars`, not here, because this
    function's contract is lines.

    A topic file is resolved the same way, right after the index: `(None,
    None)` always. Its char cap is the fixed `lint_lib.TOPIC_MAX_CHARS` (no
    config key at all), read directly in `build`, not through this function.

    D-M1 — a memory entry is resolved FIRST, before `is_map` / `cap_key` are
    even computed, so it can never fall through to the CLAUDE.md family's
    caps. Its Core cap is always `None`: the `context_growth.core_overrides`
    map is never consulted for one, on purpose, so a stray `CORE:START`/`END`
    pair pasted into a memory file cannot start billing it against the
    CLAUDE.md family's Core budget. The path-keyed `context_growth.overrides`
    ratchet DOES still apply — memory files are outside git, which is exactly
    what rule 3 below already covers.
    """
    try:
        if is_memory_index(path):
            return None, None
    except Exception:
        pass

    # A topic file, resolved right after the index for the same reason: no
    # whole-file LINE cap, no Core cap, and `context_growth.overrides` /
    # `core_overrides` are LINE maps so neither applies here either. Its char
    # cap is the fixed `lint_lib.TOPIC_MAX_CHARS` (no config key), read
    # directly in `build`, not through this function -- this function's
    # contract is lines.
    try:
        if is_topic_file(path):
            return None, None
    except Exception:
        pass

    try:
        if is_memory_file(path):
            whole_cap = memory_max_lines(config)
            if config is not None:
                try:
                    raised = _override_lookup(
                        path, config.get("context_growth", "overrides",
                                        default=None))
                    if raised is not None:
                        whole_cap = raised
                except Exception:
                    pass
            return whole_cap, None
    except Exception:
        pass

    # A rules file, same shape as the memory branch immediately above and
    # resolved before `is_map` / `cap_key` for the same reason: it must never
    # fall through to the CLAUDE.md family's caps, even when it sits inside a
    # git repo and `cap_key` would otherwise resolve. No Core cap, ever --
    # `core_overrides` is never consulted for one, so a stray CORE:START/END
    # pair pasted into a rules file cannot start billing it against the
    # CLAUDE.md family's Core budget.
    try:
        if is_rules_file(path):
            whole_cap = rules_max_lines(config)
            if config is not None:
                try:
                    raised = _override_lookup(
                        path, config.get("context_growth", "overrides",
                                        default=None))
                    if raised is not None:
                        whole_cap = raised
                except Exception:
                    pass
            return whole_cap, None
    except Exception:
        pass

    # THE MAP ITSELF ONLY — deliberately NOT its template, and this asymmetry
    # with `context_lint_lib.core_cap_for` is intended rather than a stale
    # copy. The two predicates answer DIFFERENT questions:
    #
    #   core_cap_for: "whose CORE cap is the digest budget?"  map AND template
    #     — a template's Core germinates into an injected block, so it is
    #       capped by what it becomes.
    #   here:         "who is exempt from a WHOLE-FILE cap?"  map only
    #     — the live map is a long reference and any whole-file cap
    #       would fire on every edit forever. The template is a bounded
    #       document (well under DEFAULT_MAX_LINES), so the ordinary cap
    #       is both harmless and a useful ceiling on it.
    #
    # So do NOT "fix" these into agreement — exempting the template from the
    # whole-file cap would remove the only ceiling it has. The shared number
    # the module docstring promises is the CORE cap, and that is still shared:
    # `core_cap_for` is called for it a few lines below.
    # Pinned by test_the_template_is_NOT_exempt_from_the_whole_file_cap.
    is_map = False
    try:
        is_map = os.path.basename(str(path)) == _tooling_name(config)
    except Exception:
        pass

    try:
        key = context_lint_lib.cap_key(path)
    except Exception:
        key = None

    # --- Core cap ---------------------------------------------------------
    # One resolver, shared with context-lint. The map's Core is capped at the
    # digest budget because that is where it is withheld; the CLAUDE.md family
    # is capped at core_max_lines. Two files, two numbers — not two numbers for
    # one file, which would be a defect.
    # `cap_key` is None for anything outside a repo, and `cap_for` returns the
    # default on a falsy key before it ever reads overrides — so the repo-keyed
    # map simply cannot match here, and no guard is needed to stop it. The
    # path-keyed map below is the one that reaches an unversioned file (D1).
    core_default, core_over = context_lint_lib.core_caps(config)
    core_cap = context_lint_lib.core_cap_for(
        key, path, core_default, core_over, config)

    # --- whole-file cap ---------------------------------------------------
    if is_map:
        whole_cap = None
    elif key:
        default_max, overrides = context_lint_lib.caps(config)
        whole_cap = context_lint_lib.cap_for(key, default_max, overrides)
    else:
        default_max, _overrides = context_lint_lib.caps(config)
        whole_cap = default_max

    # --- path-keyed overrides, unversioned files only (D1 rule 3) ---------
    if not key and config is not None:
        try:
            raised = _override_lookup(
                path, config.get("context_growth", "overrides", default=None))
            if raised is not None and not is_map:
                whole_cap = raised
            raised_core = _override_lookup(
                path, config.get("context_growth", "core_overrides", default=None))
            if raised_core is not None:
                core_cap = raised_core
        except Exception:
            pass

    return whole_cap, core_cap


def observe(path, config=None):
    """Current counts, or None if the file cannot be read.

    `refs` is the LIST of matched strings, not a count: `build` names them in
    the notice, and detection is by set difference so that swapping one dangling
    ref for another still speaks. A count would go silent on that.

    **A file with no Core records `0`, NEVER `None`.** `_rose_over_cap` reads
    `None` as "no prior observation" and stays silent on it, so a `None` here
    would conflate "this file has no Core" with "I have never seen this file" —
    two states that must behave differently. Both target files have no Core
    today and the whole point of the contract is that they are about to get
    one, so the very first Core write is the case that must speak: at `None`,
    adding a 300-line Core under an 80-line cap is silent, and deleting a Core
    then re-adding it at any size resets the ratchet permanently. `0` makes
    "no Core -> Core" an ordinary rise, and leaves a genuinely absent record
    the only thing that means unknown.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except Exception:
        return None
    try:
        core = core_block.extract(text)
        result = {"lines": context_lint_lib.count_lines(text),
                  "core_lines": (context_lint_lib.count_lines(core)
                                 if core else 0),
                  "refs": list(core_block.dangling_refs(core)) if core else []}
        if os.path.basename(str(path)) == _tooling_name(config):
            # The hook's second refusal is in CHARS of its whole output, and
            # the line-based Core check above cannot see it. Measured
            # by digest_lib itself so the two can never disagree.
            result["digest_chars"] = len(digest_lib.assemble(text, path, config))
        if is_rules_file(path):
            result["has_paths"] = has_paths_frontmatter(text)
        if is_memory_index(path) or is_topic_file(path):
            # Characters, never len(text.encode()) -- see lint_lib's
            # DEFAULT_MAX_INDEX_CHARS comment for the measurement pinning the
            # unit. A topic file gets the same treatment as the index: it is
            # read whole whenever it is opened, so its own cap is in chars.
            result["chars"] = len(text)
            # N3b, LINES axis. The harness's own count (P2), NOT the `lines`
            # field above (`context_lint_lib.count_lines` ==
            # `len(text.splitlines())`) -- the two disagree at exactly the
            # boundary this cap fires on. See `lint_lib.harness_line_count`.
            result["harness_lines"] = lint_lib.harness_line_count(text)
            # The count reads high over HTML comments (the loader strips them
            # first); the lines band says so when this is set.
            result["has_comments"] = "<!--" in text
        if is_memory_index(path):
            # Two-level only: `outside_always` is THIS edit's set of leaf
            # rows sitting outside MEMORY.md's Always section, so `build` can
            # detect a NEW one on the next observation. The key's
            # ABSENCE in flat layout -- never an empty list -- is what tells
            # `build` "not observed in this layout", the same distinction
            # `core_lines: 0` draws against a truly unseen file above.
            # Isolated in its own try/except: a failure here must leave the
            # rest of `result` exactly as it would have been without this
            # block, never fall through to the outer handler and erase it.
            try:
                if lint_lib.layout(os.path.dirname(path)) == "two-level":
                    result["outside_always"] = lint_lib.leaf_rows_outside_always(text)
            except Exception:
                pass

        # N2 -- rule-placement transition backstop (design §5), same shape as
        # the `outside_always` block above and isolated in its own
        # try/except for the same reason: a failure here must leave the rest
        # of `result` untouched. Two-level only. The memory dir is the
        # index's own directory, a topic file's grandparent (it sits at
        # `<memory_dir>/topics/<name>.md`), or a memory entry's own
        # directory -- the same three shapes `is_memory_index`/`is_topic_file`
        # /`is_memory_file` already distinguish.
        if is_memory_index(path) or is_topic_file(path) or is_memory_file(path):
            try:
                if is_topic_file(path):
                    mem_dir = os.path.dirname(os.path.dirname(path))
                else:
                    mem_dir = os.path.dirname(path)
                if lint_lib.layout(mem_dir) == "two-level":
                    scanned = lint_lib.scan(mem_dir, config)
                    pairs = (list(scanned.get("rules_outside_always") or [])
                             + list(scanned.get("facts_in_always") or []))
                    result["misplaced"] = sorted(
                        "%s:%s" % pair for pair in pairs)
            except Exception:
                pass
        return result
    except Exception:
        return None


def state_path(config=None):
    """Env, then config, then the shipped default. Always absolute."""
    from_env = os.environ.get(STATE_ENV)
    if from_env:
        return os.path.expanduser(from_env)
    configured = DEFAULT_STATE_PATH
    if config is not None:
        try:
            configured = config.get_path("context_growth", "state_path",
                                         default=DEFAULT_STATE_PATH)
        except Exception:
            configured = DEFAULT_STATE_PATH
    return os.path.expanduser(configured or DEFAULT_STATE_PATH)


def read_state(config=None):
    """Last-seen counts per realpath. `{}` for missing, unreadable, corrupt or
    non-dict. Never raises: a caller is a best-effort hook."""
    try:
        with open(state_path(config), encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state, config=None):
    """Persist `state`, pruning entries whose file no longer exists.

    That pruning IS the whole bounding policy for state growth. Two key
    shapes are not a file path at all, and both are exempted from the
    existence prune rather than pruned by it: a `MISPLACED_DIR_PREFIX`-keyed
    entry names a memory DIRECTORY (N2), left to be superseded the next time
    that same directory is observed; an `ALWAYS_ON_PREFIX`-keyed entry names
    a workspace ROOT (N6), left to be superseded the next time that root's
    always-on total is measured. `os.path.exists` on either is always False
    (both carry a prefix, never a bare path), so without the carve-out the
    prune would silently drop the ratchet on every write.

    Atomic via `mkstemp` in the TARGET's own directory plus `os.replace`,
    copying `usage_lib._atomic_write`: `os.replace` is only atomic within one
    filesystem, so a shared temp dir would defeat the point. NO LOCK — two
    sessions may edit at once, and a lost update costs one missed advisory
    notice, while a lock in a best-effort PostToolUse hook can wedge an editor
    for a message nobody is blocked on.
    """
    target = state_path(config)
    try:
        pruned = {k: v for k, v in dict(state).items()
                  if k.startswith(MISPLACED_DIR_PREFIX)
                  or k.startswith(ALWAYS_ON_PREFIX)
                  or os.path.exists(k)}
    except Exception:
        return False
    try:
        directory = os.path.dirname(target) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".context-growth-")
    except Exception:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(pruned, handle)
        os.replace(tmp_path, target)
        return True
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return False


def _topics_summary(memory_dir):
    """Sorted `topics/*.md` basenames under `memory_dir`, comma-joined and
    capped at `lint_lib.MAX_NAMED_PER_CATEGORY`, or `"none yet"`. Never raises.

    Points a reader at an actual topic file rather than the abstract "a topic
    file" -- used only in the new-leaf-row-outside-Always notice.
    """
    topics_path = os.path.join(memory_dir, lint_lib.TOPICS_DIR)
    try:
        names = sorted(
            n for n in os.listdir(topics_path)
            if n.endswith(".md") and os.path.isfile(os.path.join(topics_path, n))
        )
    except Exception:
        names = []
    if not names:
        return "none yet"
    shown = names[:lint_lib.MAX_NAMED_PER_CATEGORY]
    rest = len(names) - len(shown)
    text = ", ".join(shown)
    if rest > 0:
        text += ", +%d more" % rest
    return text


def _misplaced_findings(new_misplaced):
    """The lint sentence(s) for a filtered `new_misplaced` list.

    Reuses `lint_lib.findings`'s own rendering rather than re-deriving the
    wording here. `new_misplaced` is a list of
    `"<name>:<type>"` strings (see `observe`'s `misplaced` key); each pair is
    routed back into whichever of `lint_lib`'s two categories its type
    belongs to, and `findings()` is called on a minimal stand-in result
    carrying only those two keys -- every other key `findings()` reads is
    absent, and its own `.get(...)  or []` / `.get(...) or 0` calls make that
    safe rather than a KeyError.
    """
    rules = []
    facts = []
    for entry in new_misplaced:
        mname, _, mtype = entry.partition(":")
        if mtype in lint_lib.RULE_TYPES:
            rules.append((mname, mtype))
        elif mtype in lint_lib.FACT_TYPES:
            facts.append((mname, mtype))
    return lint_lib.findings({"readable": True,
                              "rules_outside_always": sorted(rules),
                              "facts_in_always": sorted(facts)})


def _rose_over_cap(prior, current, cap):
    """True when the count ROSE and the result is over `cap`.

    It says NOTHING about whether `prior` was over cap: an under-cap ->
    over-cap crossing is the canonical case and must speak. A `None` cap means
    this part never qualifies, which is how the tooling map is exempt from the
    whole-file check.

    A `None` PRIOR means "never observed" and only that. `observe` records a
    file with no Core as `0`, so "had no Core, now has one" arrives here as a
    rise from zero and speaks. The one remaining `None` is a state entry
    written before that rule existed; it costs one silent edit and then
    self-corrects.
    """
    if cap is None or prior is None or current is None:
        return False
    return current > prior and current > cap


def _always_on_context(path, config):
    """Steps 1-5 of N6's always-on-budget branch (design's "The contract"):
    whether this call could possibly speak for the always-on total,
    resolved BEFORE any state I/O.

    **Why this is its own function, separate from step 6 onward.** The
    candidate gate (step 5) is `measure_instructions`-free and reads no
    state, but everything through step 4 (resolving `root`/`home`/
    `memory_dir`) has to run before it can be evaluated at all. `build()`
    calls this FIRST, and only reads or writes `~/.claude/context-growth-
    state.json` if either this returns something or the edited file is
    `in_scope` -- an edit to a file neither configured for nor eligible for
    this feature (an ordinary `.py` file, say, with no `always_on_budget`
    set) must cost zero state I/O, not a read-then-discard.

    Returns `None` when any of steps 1-5 fails: `config` is `None`, the
    configured budget is not `"ok"`, `config.workspace_root()` is empty or
    raises, or the edited file's realpath is not in
    `always_on_candidates(...)`. Otherwise returns `(config, budget, root,
    home, memory_dir, memory_reason)` -- `config` travels with it so
    `_always_on_notice` never needs a second parameter for it.

    `budget_lib` is imported LAZILY, here rather than at module load: it
    imports `growth_lib` back (to classify a rules file by its `paths:`
    frontmatter -- see `budget_lib.measure_instructions`), and an import
    reached from inside a function THIS module already finished loading
    resolves that back-import to the already-cached module rather than
    re-entering this file (P6).

    Reads no file contents of its own beyond what `resolve_memory_dir` and
    `always_on_candidates` already do, and never raises.
    """
    try:
        if config is None:
            return None

        import budget_lib

        budget, budget_state = budget_lib.always_on_budget(config)
        if budget_state != "ok":
            return None

        try:
            root_raw = config.workspace_root()
        except Exception:
            return None
        if not root_raw:
            return None
        root = os.path.realpath(os.path.expanduser(str(root_raw)))

        home = os.path.expanduser("~")
        memory_dir, memory_reason = budget_lib.resolve_memory_dir(
            root, config, home)

        edited = os.path.realpath(os.path.expanduser(str(path)))
        candidates = budget_lib.always_on_candidates(root, memory_dir, config,
                                                      home)
        if edited not in candidates:
            return None  # the cheap gate (P4) -- nothing measured below it

        return (config, budget, root, home, memory_dir, memory_reason)
    except Exception:
        return None


def _always_on_notice(path, context, state):
    """Steps 6-8 of N6's always-on-budget branch, given the `context`
    `_always_on_context` already resolved (steps 1-5).

    Runs whenever `context` is not `None` -- independent of `in_scope` and
    of the per-file first-sight/no-parts guards in `_per_file_notice` --
    because the target is the always-on TOTAL, not one file's own cap: a
    memory dir not literally named `memory`, or `CLAUDE.local.md`, both fail
    `in_scope` yet must still be able to speak for it (P10).

    Returns `(notice_or_None, changed)`. `changed` is `True` iff `state` was
    actually mutated -- step 8's `state[key] = total`, reached only once
    steps 6 and 7 both pass -- so `build()` can decide whether a write is
    owed at ALL: an always-on candidate whose total came back unknown (step
    6) or whose edited file is not itself an always-on record (step 7)
    costs no write, exactly as it costs no notice.

    Never raises: any exception here silences this branch alone (returning
    `(None, False)`), and the caller's own per-file notice, if any, is
    unaffected.
    """
    try:
        config, budget, root, home, memory_dir, memory_reason = context
        import budget_lib

        edited = os.path.realpath(os.path.expanduser(str(path)))
        records = budget_lib.measure_instructions(
            root, memory_dir, memory_reason, config, home)
        total = budget_lib.always_on_total(records)
        if total is None:
            return None, False

        always_on_keys = {
            os.path.realpath(r["key"])
            for r in records
            if r.get("class") == "always-on" and r.get("key")
        }
        key = ALWAYS_ON_PREFIX + root
        if edited not in always_on_keys:
            # A candidate that is not (or is no longer) always-on -- a rules
            # file that just gained `paths:` -- may still have LOWERED the
            # total. Record a drop, so the next crossing is measured from the
            # real total rather than a stale higher one (Merge Review). Never
            # record a rise here: that would consume a crossing this edit
            # cannot speak for (case 7).
            prior = state.get(key)
            if prior is not None and total < prior:
                state[key] = total
                return None, True
            return None, False


        prior = state.get(key)
        state[key] = total
        if not _rose_over_cap(prior, total, budget):
            return None, True

        lines = [
            "[context-growth] always-on instructions are over budget.",
            "",
            "  %d instructions, budget %d — %d over (+%d since last check)"
            % (total, budget, total - budget, total - prior),
            "",
            "Consolidate with the claude-md-distill skill's Consolidation "
            "mode: it finds stale and duplicate blocks, moves rows already "
            "carried elsewhere one hop out, and asks before cutting "
            "anything. `claude-context-budget --report` shows where the "
            "total comes from.",
        ]
        return "\n".join(lines), True
    except Exception:
        return None, False


def build(tool_input, config=None, state=None):
    """The growth notice, or None for silence. Never raises.

    Two independent branches, run alongside each other: the pre-existing
    per-file notice (the nested `_per_file_notice`, gated on `in_scope`) and
    N6's always-on-total notice (`_always_on_notice`, gated on
    `_always_on_context` -- P10: a memory dir not literally named `memory`,
    or `CLAUDE.local.md`, both fail `in_scope` yet must still be able to
    speak for the total). Both read and write the SAME `state` dict --
    disjoint keys, one per edited file's own realpath for the first,
    `ALWAYS_ON_PREFIX + workspace root` for the second.

    **State I/O is gated on need, not run on every call.** An edit to a file
    that is neither `in_scope` nor an always-on candidate -- an ordinary
    `.py` file on a machine with no `context_budget.always_on_budget` set,
    say -- must cost ZERO reads and ZERO writes of
    `~/.claude/context-growth-state.json`, not a read that finds nothing
    followed by a write of nothing new. `_always_on_context` (steps 1-5 of
    the contract) is resolved BEFORE any state I/O for exactly this reason:
    it is state-free itself, so `build()` can decide whether state is
    needed at all before touching it. The write at the end is similarly
    gated: state is written only when the per-file branch ran (it always
    records `state[key] = current` for an in-scope file) or the always-on
    branch actually reached step 8 -- an always-on candidate whose total
    came back unknown, or whose edited file isn't itself an always-on
    record, returns silently without writing anything.
    """
    try:
        if not isinstance(tool_input, dict):
            return None
        path = tool_input.get("file_path")
        if not path:
            return None
        if not is_enabled(config):
            return None

        scoped = in_scope(path, config)
        always_on_ctx = None
        try:
            always_on_ctx = _always_on_context(path, config)
        except Exception:
            always_on_ctx = None

        # Whether THIS call has anything to do with state at all, resolved
        # before any state I/O -- both `in_scope` and `_always_on_context`
        # are state-free. An edit that is neither reads/writes NOTHING.
        needs_state = scoped or always_on_ctx is not None

        # `state is not None`, NOT `state or ...`. An empty dict is falsy and
        # an empty dict is what a caller threading state in memory passes on
        # its first edit — so the `or` form would reach for state_path(config),
        # which with config=None is the user's LIVE state file.
        persist = state is None
        if needs_state:
            state = state if state is not None else read_state(config)
        elif state is None:
            state = {}

        # Isolated in its own try/except even though `_always_on_notice`
        # already never raises: a caller that patches it directly (as a
        # mutation-testing run does) must still leave the per-file notice
        # below reachable.
        always_on_notice = None
        always_on_changed = False
        if always_on_ctx is not None:
            try:
                always_on_notice, always_on_changed = _always_on_notice(
                    path, always_on_ctx, state)
            except Exception:
                always_on_notice, always_on_changed = None, False

        def _per_file_notice(path, config, state):
            """The pre-existing per-file growth notice, or None for silence.

            Nested inside `build()` (rather than a sibling top-level
            function) so `growth_lib.build`'s own source still carries the
            first-sight `if prior is None` guard the structural test
            `test_the_first_sight_guard_is_PRESENT` parses for -- `ast.walk`
            recurses into a nested `def`'s body just as it does build()'s
            own. `state` is already resolved by the caller (`build`'s own
            persisted read, or a caller's in-memory dict); this function
            only mutates it -- it never reads or writes the state FILE
            itself, and never calls `write_state` -- `build()` does that
            once, after this function and the always-on branch
            (`_always_on_notice`) have both run.
            """
            try:
                current = observe(path, config)
                if current is None:
                    return None

                key = os.path.realpath(os.path.expanduser(str(path)))
                prior = state.get(key)

                # The missing-`paths:` notice, computed BEFORE the first-sight
                # return below so a brand-new pathless rules file can speak on its
                # own first write. `current.get("has_paths")` is `None` (never
                # `False`) for anything that is not a rules file, so this is False
                # by construction for every other branch -- no `is_rules_file` guard
                # needed here. True on first sight of a pathless file (`prior is
                # None`) and on a has-paths -> no-paths transition; False once a
                # pathless file has already been warned about (`prior.get(
                # "has_paths") is False` too) and False for a scoped file.
                missing_paths = (current.get("has_paths") is False and
                                 (prior is None
                                  or prior.get("has_paths") is not False))

                # D-M1/change 8: a memory entry OR a rules file gets no Core row and
                # no dangling-reference row, even if its text happens to contain
                # CORE:START/END markers. The Core part below already can't fire for
                # either -- caps_for_path returns a None Core cap for both
                # unconditionally, and `_rose_over_cap` requires a non-None cap --
                # but `observe` still extracts refs from any CORE/DIGEST pair it
                # finds, so the ref check needs its own guard or a stray marker
                # pasted into either file type would still speak about refs.
                # Hoisted above the first-sight guard: the per-directory baseline
                # update just below needs `is_topic` to find the memory dir, and
                # needs to run even when THIS file is being seen for the first time.
                is_memory = is_memory_file(path)
                is_rules = is_rules_file(path)
                is_index = is_memory_index(path)
                is_topic = is_topic_file(path)

                # N2 -- the misplaced set `observe()` computed is DIRECTORY-wide (a
                # fresh `lint_lib.scan` of the whole memory dir), so it must be
                # compared against that DIRECTORY's own last-seen set, not against
                # THIS file's prior snapshot of the same directory-wide list.
                # Comparing per-edited-file meant that once the index's own notice
                # updated the index's copy, the TOPIC's (or the index's own NEXT)
                # copy was still stale, and an unchanged write re-announced the same
                # placement (Code Review finding). Keyed on the memory dir's
                # realpath, under `MISPLACED_DIR_PREFIX + <dir>` in the SAME state
                # dict, so it persists exactly as everything else in `state` does
                # (`write_state` carves this prefix out of its existence-based
                # pruning -- see there).
                #
                # Computed and persisted here, BEFORE the first-sight guard below,
                # so a directory's baseline seeds silently even on the edited FILE's
                # own first sight -- otherwise the first real edit after that first
                # sight would see every pre-existing misplacement as "new".
                #
                # A misplacement the edit-type filter drops (e.g. found while
                # editing an unrelated topic) is deliberately NOT folded into the
                # baseline: `announced` (this edit's filtered set) is added, but
                # anything in `current` that is neither in the OLD baseline nor
                # announced now stays out of the NEW one, so it is still "new" the
                # next time an edit that can speak for it comes along. A
                # misplacement no longer in `current` (fixed) drops out via the
                # intersection, so re-breaking it can be announced again.
                announced_misplaced = []
                if current.get("misplaced") is not None:
                    mem_dir = (os.path.dirname(os.path.dirname(path)) if is_topic
                               else os.path.dirname(path))
                    try:
                        dir_key = MISPLACED_DIR_PREFIX + os.path.realpath(mem_dir)
                    except Exception:
                        dir_key = None
                    if dir_key is not None:
                        current_misplaced = current["misplaced"]
                        baseline = state.get(dir_key)
                        if baseline is None:
                            state[dir_key] = sorted(current_misplaced)
                        else:
                            added = [m for m in current_misplaced
                                     if m not in set(baseline)]
                            if is_index:
                                announced_misplaced = added
                            elif is_topic:
                                topic_leaf_names = set()
                                try:
                                    with open(path, encoding="utf-8") as handle:
                                        topic_text = handle.read()
                                    for row in lint_lib.parse_sections(topic_text):
                                        kind, leaf_name = lint_lib.classify_topic_target(
                                            row["file"])
                                        if kind == "leaf":
                                            topic_leaf_names.add(leaf_name)
                                except Exception:
                                    pass
                                announced_misplaced = [
                                    m for m in added
                                    if m.split(":", 1)[0] in topic_leaf_names]
                            elif is_memory:
                                own_name = os.path.basename(str(path))
                                announced_misplaced = [
                                    m for m in added
                                    if m.split(":", 1)[0] == own_name]
                            # Provisional: KEEP PENDING. `announced_misplaced` only
                            # actually reaches a reader if THIS call returns a notice
                            # naming it -- and at this point in the function we do
                            # not yet know that (the first-sight guard below can
                            # still suppress the whole notice). Committing the union
                            # here unconditionally is exactly the Merge Review defect:
                            # a placement gets folded into the baseline as
                            # "announced" on a call whose own notice is later
                            # dropped, so a subsequent edit that COULD have spoken
                            # for it sees no diff and stays silent too -- the
                            # placement is consumed without ever being emitted. The
                            # union is applied instead at the point below where
                            # `new_misplaced` is actually set from
                            # `announced_misplaced`, which is only reached on a path
                            # that goes on to render it.
                            state[dir_key] = sorted(
                                set(baseline) & set(current_misplaced))

                # Record unconditionally, INCLUDING on a shrink: without that, cutting
                # 50 lines and adding 3 back stays under the old high-water mark and the
                # ratchet leaks.
                state[key] = current
                if persist:
                    write_state(state, config)

                # Nested, not `and`-ed into one compare: the structural test
                # `test_the_first_sight_guard_is_PRESENT` requires exactly one bare
                # `if prior is None` guard so first-sight silence stays a deliberate
                # return rather than an artifact of the fail-safe exception handler.
                if prior is None:
                    if not missing_paths:
                        return None

                # Two-level only, and cheaper than re-deriving it: `observe` already
                # decided this when it wrote `current["outside_always"]`, so its mere
                # presence (an empty list still counts) IS the layout reading for
                # this edit.
                is_two_level_index = is_index and current.get("outside_always") is not None

                # The size comparison needs a PRIOR observation to compare against;
                # a rules file can reach here with `prior is None` purely on the
                # strength of `missing_paths`, and there is nothing to diff yet.
                parts = []
                new_refs = []
                new_outside = []
                new_misplaced = []
                if prior is not None:
                    whole_cap, core_cap = caps_for_path(path, config)
                    if _rose_over_cap(prior.get("lines"), current["lines"], whole_cap):
                        parts.append(("whole file", current["lines"], whole_cap,
                                      current["lines"] - prior["lines"]))
                    if _rose_over_cap(prior.get("core_lines"), current["core_lines"],
                                      core_cap):
                        parts.append(("Core", current["core_lines"], core_cap,
                                      current["core_lines"] - prior["core_lines"]))
                    if _rose_over_cap(prior.get("digest_chars"),
                                      current.get("digest_chars"),
                                      digest_lib.HARNESS_MAX_CHARS):
                        parts.append(("digest output", current["digest_chars"],
                                      digest_lib.HARNESS_MAX_CHARS,
                                      current["digest_chars"]
                                      - prior["digest_chars"]))

                    # The index's own axis: CHARACTERS, not lines -- caps_for_path
                    # returns (None, None) for it, so the two checks above can never
                    # fire; this is the only size check that can.
                    if is_index:
                        char_cap = index_max_chars(config)
                        if _rose_over_cap(prior.get("chars"), current.get("chars"),
                                          char_cap):
                            parts.append(("index", current["chars"], char_cap,
                                          current["chars"] - prior["chars"]))

                        # N3b, LINES axis -- same _rose_over_cap contract, on
                        # the harness's own count rather than characters.
                        line_cap = index_max_lines(config)
                        if _rose_over_cap(prior.get("harness_lines"),
                                          current.get("harness_lines"), line_cap):
                            parts.append(("index lines",
                                          current["harness_lines"], line_cap,
                                          current["harness_lines"]
                                          - prior["harness_lines"]))

                        # The other edit-time backstop: a leaf row landing outside
                        # Always in THIS edit, two-level layout only. A missing key on
                        # EITHER side means "not observed in this layout" -- a
                        # flat-era prior, or a layout this edit dropped back to flat
                        # -- and stays silent rather than reading every current name
                        # as new. Fires on the TRANSITION (set difference), never on
                        # the state, so a name already present in both stays quiet.
                        if (current.get("outside_always") is not None
                                and prior.get("outside_always") is not None):
                            new_outside = [
                                leaf for leaf in current["outside_always"]
                                if leaf not in set(prior["outside_always"])
                            ]

                    # A topic file's own axis, same shape as the index: CHARACTERS,
                    # against the fixed `TOPIC_MAX_CHARS` (no config key) rather than
                    # a configurable cap -- caps_for_path returns (None, None) for it
                    # too, so the two whole-file/Core checks above can never fire.
                    if is_topic:
                        if _rose_over_cap(prior.get("chars"), current.get("chars"),
                                          lint_lib.TOPIC_MAX_CHARS):
                            parts.append(("topic", current["chars"],
                                          lint_lib.TOPIC_MAX_CHARS,
                                          current["chars"] - prior["chars"]))
                        # N3b, LINES axis -- same shape as the index's own
                        # lines check above, against the fixed
                        # TOPIC_MAX_LINES (no config key).
                        if _rose_over_cap(prior.get("harness_lines"),
                                          current.get("harness_lines"),
                                          lint_lib.TOPIC_MAX_LINES):
                            parts.append(("topic lines",
                                          current["harness_lines"],
                                          lint_lib.TOPIC_MAX_LINES,
                                          current["harness_lines"]
                                          - prior["harness_lines"]))

                    # N2 transition: a memory landing in (or leaving) the wrong tier
                    # in THIS edit, two-level layout only -- same shape as
                    # `new_outside` above. `announced_misplaced` was computed above,
                    # against the memory DIRECTORY's own baseline rather than this
                    # file's prior snapshot (see the comment there); a missing
                    # `misplaced` key means "not observed in this layout" and stays
                    # silent, same as `new_outside`.
                    if current.get("misplaced") is not None:
                        new_misplaced = announced_misplaced
                        # Promote the per-directory baseline from pending to
                        # committed HERE, not in the block above. Reaching this
                        # line already means `prior is not None` -- the first-sight
                        # `return None` above did not fire -- and nothing between
                        # here and the final `return` clears a non-empty
                        # `new_misplaced` out of the rendered notice (see
                        # `_misplaced_findings` and its call sites below), so
                        # whatever `announced_misplaced` holds now WILL be spoken
                        # for. `baseline is not None` excludes the dir's own
                        # first-sight branch above, which already stored `current`
                        # unconditionally and has nothing to promote.
                        if dir_key is not None and baseline is not None:
                            state[dir_key] = sorted(
                                (set(baseline) & set(current_misplaced))
                                | set(announced_misplaced))
                            # `write_state` already ran above, BEFORE this promotion,
                            # so a hook invocation must write again or the promotion
                            # lives only in memory and the next write re-announces.
                            if persist and announced_misplaced:
                                write_state(state, config)

                    # Set difference, not a rise in count: replacing one dangling ref
                    # with a different one leaves the count equal, and a count-based
                    # check would go silent on a genuinely new defect.
                    new_refs = ([] if (is_memory or is_rules or is_index or is_topic)
                                else [r for r in current["refs"]
                                      if r not in set(prior.get("refs") or [])])

                if (not parts and not new_refs and not missing_paths
                        and not new_outside and not new_misplaced):
                    return None

                # The header and trailer state only what actually happened. The notice
                # fires on EITHER a size rise or a new dangling ref, and a fixed "grew
                # past its budget" header on a ref-only notice is a size claim with no
                # size row under it — the reader looks for the number and there isn't
                # one. A notice that overstates its own finding is the first step to
                # being read as noise.
                #
                # A memory entry gets its own header and trailer (D-M1/D-M2): it tells
                # the reader to DISTILL via the `memory-distill` skill, never to trim,
                # since a memory file has no cheap partial cut and the whole point of
                # the skill is folding incidents into rules rather than shrinking text.
                # The CLAUDE.md / TOOLING.md branches below are unchanged in wording.
                name = os.path.basename(str(path))

                # N2's own section (design §5): its own header line plus the reused
                # lint sentence(s), built once here so a branch below can either lead
                # with it -- when it is the ONLY thing that fired -- or, if it is not
                # consumed as the primary header, it is appended after whatever else
                # did (see the tail of this function).
                misplaced_lines = []
                if new_misplaced:
                    misplaced_lines = ["[context-growth] %s leaves a memory in the "
                                       "wrong tier." % name, ""]
                    misplaced_lines += ["  " + s for s in
                                        _misplaced_findings(new_misplaced)]

                if is_index:
                    if parts:
                        lines = ["[context-growth] %s is over its index budget."
                                 % name, ""]
                    elif new_outside:
                        lines = ["[context-growth] MEMORY.md has a new leaf row "
                                 "outside Always.", ""]
                    else:
                        # `is_index` can only reach here via `new_misplaced` --
                        # `parts` and `new_outside` are both empty (checked above)
                        # and `new_refs`/`missing_paths` are always False for the
                        # index, so `misplaced_lines` is guaranteed non-empty here.
                        lines = misplaced_lines
                        misplaced_lines = []
                elif is_memory:
                    if parts:
                        lines = ["[context-growth] %s is over the memory "
                                 "distillation cap." % name, ""]
                    else:
                        # `is_memory` can only reach here via `new_misplaced` --
                        # `new_refs` is excluded for it and `missing_paths` never
                        # applies to it (it never sets `has_paths`).
                        lines = misplaced_lines
                        misplaced_lines = []
                elif is_topic:
                    if parts:
                        # `is_topic` can only reach here via `parts` or
                        # `new_misplaced` -- `new_refs` is excluded above and
                        # `missing_paths` is always False for it (it never sets
                        # `has_paths`).
                        lines = ["[context-growth] %s is over its topic-file cap."
                                 % name, ""]
                    else:
                        lines = misplaced_lines
                        misplaced_lines = []
                elif is_rules:
                    # `is_rules` can only reach here via `parts` or `missing_paths`
                    # (never `new_refs`, excluded above) -- so "no size part" means
                    # missing-paths is what fired.
                    if parts:
                        lines = ["[context-growth] %s grew past its budget in this "
                                 "session." % name, ""]
                    else:
                        lines = ["[context-growth] %s has no paths: frontmatter."
                                 % name, ""]
                elif parts:
                    lines = ["[context-growth] %s grew past its budget in this "
                             "session." % name, ""]
                else:
                    lines = ["[context-growth] %s: new cross-reference in the Core."
                             % name, ""]
                for label, count, cap, delta in parts:
                    # "+N since last check", NOT "this edit". PostToolUse sees only
                    # Edit/Write/MultiEdit, so a `Bash` heredoc that adds 300 lines is
                    # never observed and its delta is charged to whatever edit comes
                    # next. The honest claim is the span between two observations.
                    if is_index or is_topic:
                        # N3b: the LINES axis shares this row shape, keyed off
                        # the part's own label -- "index lines" / "topic
                        # lines" render "lines"; "index" / "topic" (the
                        # pre-existing char parts) still render "chars".
                        unit = "lines" if label.endswith(" lines") else "chars"
                        lines.append("  %d %s, cap %d — %d over (+%d since last "
                                     "check)" % (count, unit, cap, count - cap,
                                                 delta))
                    elif is_memory:
                        lines.append("  %d lines, cap %d — %d over (+%d since last "
                                     "check)" % (count, cap, count - cap, delta))
                    else:
                        unit = "chars" if label == "digest output" else "lines"
                        lines.append("  %s: %d %s, cap %d — %d over (+%d since "
                                     "last check)" % (label, count, unit, cap,
                                                      count - cap, delta))
                # N3b: each band is keyed on its OWN part having fired, not on
                # `parts` being non-empty -- today's `parts`-only gate would
                # mis-describe a lines-only crossing as a chars one (and vice
                # versa) once both axes exist.
                index_chars_part = any(p[0] == "index" for p in parts)
                index_lines_part = any(p[0] == "index lines" for p in parts)
                if is_index and index_chars_part:
                    # The two-band split lint_lib's own findings() makes: over budget
                    # is a thing to fix soon, past the truncation point is data being
                    # lost right now. `>=`, matching lint_lib.findings' own boundary --
                    # exactly 25,000 counts as past it.
                    if current["chars"] >= lint_lib.INDEX_TRUNCATION_CHARS:
                        lines.append(
                            "  past %d chars the harness drops rows off the END of "
                            "the index, silently — rows are being lost in every "
                            "session now." % lint_lib.INDEX_TRUNCATION_CHARS)
                    else:
                        lines.append(
                            "  the harness drops rows off the END past ~%d chars, "
                            "silently, with no error." % lint_lib.INDEX_TRUNCATION_CHARS)
                if is_index and index_lines_part:
                    # Same two-band split, LINES axis: `>` matches
                    # lint_lib.harness_line_count's own strict boundary (a
                    # 200-line index loses nothing; 201 does).
                    if current["harness_lines"] > lint_lib.INDEX_TRUNCATION_LINES:
                        lines.append(
                            "  past ~%d lines the harness drops the END of the "
                            "index, silently — rows are being lost in every "
                            "session now." % lint_lib.INDEX_TRUNCATION_LINES)
                    else:
                        lines.append(
                            "  the harness drops the END of the index past ~%d "
                            "lines, silently, with no error."
                            % lint_lib.INDEX_TRUNCATION_LINES)
                    if current.get("has_comments"):
                        lines[-1] += lint_lib.COMMENT_CAVEAT
                if is_index and new_outside:
                    # This fires whether or not a size part also did, so an edit
                    # that crosses the cap AND adds a leaf row outside Always gets
                    # the size row above and this row below it, under whichever
                    # header `parts` selected.
                    topics_str = _topics_summary(os.path.dirname(path))
                    shown = new_outside[:lint_lib.MAX_NAMED_PER_CATEGORY]
                    for leaf_name in shown:
                        lines.append(
                            "  %s — its row belongs in a topic file under "
                            "memory/topics/ (%s)" % (leaf_name, topics_str))
                    rest = len(new_outside) - len(shown)
                    if rest > 0:
                        lines.append("  (+%d more)" % rest)
                if new_refs:
                    lines.append("  new cross-reference in the Core, which is read "
                                 "without the body:")
                    for ref in new_refs:
                        lines.append("      %r" % ref)
                if is_rules and missing_paths:
                    lines.append("  no paths: — this rules file loads at session "
                                 "start in EVERY session, like CLAUDE.md, instead of "
                                 "only when a matching file is read.")
                if is_index:
                    # Two-level: one trailer per thing that fired, both when both
                    # did -- an edit that crosses the cap AND adds a leaf row outside
                    # Always needs both fixes. A two-level index is never told to
                    # retire a row: ITS excess is rows that belong in topic files.
                    if is_two_level_index:
                        if parts:
                            lines += ["",
                                      "Move rows out of MEMORY.md into topic files — "
                                      "every row here is paid in every session, and a "
                                      "topic file costs nothing until it is opened."]
                        if new_outside:
                            lines += ["",
                                      "In the two-level layout a new memory's row "
                                      "goes in its topic file; MEMORY.md takes leaf "
                                      "rows only in its Always section."]
                    else:
                        lines += ["",
                                  "Retire a row for every row you add — prefer one "
                                  "whose rule a CLAUDE.md already states. The "
                                  "memory file itself stays: put `routed: false` in "
                                  "its frontmatter so memory-lint does not report "
                                  "it as an orphan."]
                elif is_memory:
                    # Gated on `parts`: `is_memory` can also reach this point via
                    # `new_misplaced` alone (a retype with no size change), and the
                    # distill trailer is about SIZE -- misleading advice for a purely
                    # type/placement finding, which already carries its own
                    # remediation in the lint sentence rendered above.
                    if parts:
                        stem = name[:-3] if name.endswith(".md") else name
                        lines += ["",
                                  "Distill it with the memory-distill skill — "
                                  "collapse what changed into rules instead of "
                                  "carrying it verbatim. The current text is "
                                  "preserved at memory/history/%s.history.md, not "
                                  "lost." % stem]
                elif is_topic:
                    # Gated on `parts` for the same reason as `is_memory` above.
                    if parts:
                        lines += ["",
                                  "Split the topic into two topic files, each with "
                                  "its own row in MEMORY.md's Topics section — a "
                                  "topic file is read whole whenever it is opened."]
                elif is_rules:
                    # Fires whenever ANY part is present (size, missing-paths, or
                    # both) -- `is_rules` reaches this point only via `parts` or
                    # `missing_paths`, never `new_refs` alone, so there is always
                    # something for this trailer to be about.
                    lines += ["",
                              "Add a paths: list to its frontmatter, or move the rule "
                              "into CLAUDE.md if it genuinely must be always-on. A "
                              "rules file is loaded whole each time it fires, so keep "
                              "it to one topic."]
                elif parts and is_claude_md(path):
                    # os.path.splitext, not a bare ".md" strip: this must match
                    # distill_guard.history_path_for's own stem derivation, which is
                    # what actually writes the archive. For "CLAUDE.md.template",
                    # splitext yields stem "CLAUDE.md" (NOT "CLAUDE.md.template") --
                    # deliberate and accepted, so the notice must name the same file
                    # the skill will actually write to.
                    stem, _ext = os.path.splitext(name)
                    lines += ["",
                              "Distill it with the claude-md-distill skill — the rules "
                              "stay, the reasoning behind them moves to %s.history.md, "
                              "which is not loaded. Nothing is lost, so the cut is "
                              "cheap." % stem]
                elif parts:
                    lines += ["",
                              "Trim it before the session ends — a line added here is "
                              "paid in every session that loads the file, and the "
                              "trade is cheapest now while the edit is still in hand."]
                else:
                    lines += ["",
                              "The Core is read without the body, so a reference out "
                              "of it resolves to nothing for the reader who only gets "
                              "the Core. Inline what it needs, or drop the pointer."]

                # N2's own section, appended here whenever it fired ALONGSIDE
                # something else -- when it was the ONLY thing that fired, it was
                # already consumed above as the primary header/body and
                # `misplaced_lines` was cleared, so this is a no-op in that case.
                if misplaced_lines:
                    lines += [""] + misplaced_lines
                return "\n".join(lines)
            except Exception:
                return None

        per_file_notice = None
        if scoped:
            per_file_notice = _per_file_notice(path, config, state)

        # Written only when something actually changed: the per-file branch
        # ran for an in-scope file (it always records `state[key] =
        # current`), or the always-on branch reached step 8
        # (`always_on_changed`). An always-on candidate that returned
        # silently at step 6 or 7 changed nothing and owes no write.
        if persist and (scoped or always_on_changed):
            write_state(state, config)

        if per_file_notice and always_on_notice:
            return per_file_notice + "\n\n" + always_on_notice
        return per_file_notice or always_on_notice
    except Exception:
        return None
