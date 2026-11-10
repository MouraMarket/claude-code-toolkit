"""Measure every context surface a session is charged for, on its OWN unit.

This is a REPORT, and now a RECORDER too. The per-instant contract below still
needs no history at all:

    every injected surface has a cap on the unit its loader FAILS on,
    headroom below that cap, and crossing it is loud.

("Fails on", not "truncates on": since 2026-09-14 the digest does not truncate
at all — over cap it withholds the whole block. The unit is unchanged, which is
the only thing this report needs from it.)

R2 wants a growth RATE too, and a rate needs history a single scan cannot
supply on its own -- that is what `readings_lib`'s one-line-per-day reading
log is for. `claude-context-budget --record` appends one line per day, run
from a SessionStart hook, and it is silent on every path: the notice-fatigue
argument against a hook mode (see that executable's docstring) does not apply
to a recorder nobody has to read. `--report` reads the log back and turns it
into a rate and a days-to-cap forecast, alongside the per-instant table
(`instructions_section`). The enforcement of the caps themselves lives in
the tools that already own each surface (`context_lint_lib`, `growth_lib`,
`lint_lib`, `digest_lib`); this only puts their numbers side by side, because
nothing did, and a mission to shrink the total ran with the total
unmeasured.

THE UNIT IS PER-SURFACE AND THEY ARE NOT INTERCHANGEABLE. Each loader fails on
a different quantity, and measuring the convenient one instead of the real one
is the specific mistake this file exists to stop:

  - CLAUDE.md family  LINES   -- what context_lint caps and pre-commit blocks on
  - TOOLING.md digest LINES   -- what digest_lib.cap_block() counts
  - MEMORY.md index   CHARS   -- what the harness truncates on (see lint_lib)
  - skill frontmatter CHARS   -- injected verbatim, capped by nothing
  - .claude/rules files LINES -- what growth_lib caps, per file

A rules file with `paths:` frontmatter loads only when a matching file is
read, so its row is a conditional cost; one without `paths:` loads at session
start, exactly like CLAUDE.md, and its row is a standing cost.

Bytes appear only as a convenience column for anyone cross-checking with
`wc -c`, and never as a cap.

NOT measured here, and deliberately: memory file BODIES and the TOOLING.md body.
Both load on demand, so they are an attention cost rather than a standing one,
and `claude-memory-lint` already reports the memory corpus. An earlier version
of this docstring listed memory files as a measured surface and said the bodies
were "reported, but separately" — neither was true of any code in this file.
"""

import json
import os
import re
import subprocess
import sys

import claude_home
import instruction_lib
import readings_lib

try:
    import claude_imports
except Exception:  # lib/ not on the path: imports are simply not followed
    claude_imports = None

# A direct import is hop 1; Claude Code follows imports to 4 hops.
MAX_IMPORT_HOPS = 4

# `tools/repo-freshness` is not on sys.path already -- neither the
# executable's own `_load()` nor this test suite's setup adds it, unlike
# lib/, context-growth, context-lint etc. Self-contained, realpath-derived,
# same shape as growth_lib.py:52-60 -- checkout_behind's caller must not have
# to know this module needs a sibling tool.
_HERE = os.path.dirname(os.path.realpath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_FRESHNESS_DIR = os.path.join(_REPO, "tools", "repo-freshness")
if _FRESHNESS_DIR not in sys.path:
    sys.path.insert(0, _FRESHNESS_DIR)

try:
    import freshness_lib
except Exception:
    freshness_lib = None

CLAUDE_MD_NAMES = ("CLAUDE.md",)

# Anthropic's documented macOS location for the managed policy file -- not
# re-read from the binary, unlike the ancestor-walk shape below (see
# `always_on_files`).
MANAGED_CLAUDE_MD = "/Library/Application Support/ClaudeCode/CLAUDE.md"

_DESCRIPTION_RE = re.compile(
    r"^description:\s*(.+?)(?=\n[A-Za-z_-]+:|\n---)", re.S | re.M
)


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return None


def _measure(text):
    """(lines, chars, bytes) for one surface. Chars and bytes are not the same
    number and the gap is structural, not incidental -- see lint_lib."""
    return (
        len(text.splitlines()),
        len(text),
        len(text.encode("utf-8")),
    )


def _digest_path(workspace_root, config):
    """Where the TOOLING.md digest lives, resolved the way the SessionStart
    loader (`digest_lib.resolve_path`) does -- minus its fallback to
    `config.workspace_root()`. `None` means "no digest record/row at all":
    either `tooling_digest.enabled` is explicitly `false`, or there is
    neither an explicit path nor a workspace root to fall back to.

    Deliberately NOT a call to `digest_lib.resolve_path(config)` as-is: that
    function's own fallback reads `config.workspace_root()`, which would make
    `--report <root>` measure the machine's CONFIGURED root instead of the
    root it was actually asked to report on. This borrows `digest_lib`'s
    enabled-check and explicit-path lookup, but keeps THIS function's own
    `workspace_root` argument for the fallback.
    """
    try:
        import digest_lib
    except Exception:
        digest_lib = None

    if digest_lib is not None:
        try:
            if not digest_lib.is_enabled(config):
                return None
        except Exception:
            pass

    explicit = None
    if config is not None:
        try:
            explicit = config.get_path("tooling_digest", "path")
        except Exception:
            explicit = None
    if explicit:
        return explicit
    return os.path.join(workspace_root, "TOOLING.md") if workspace_root else None


def checkout_behind(path):
    """How far the git checkout CONTAINING `path` is behind
    `origin/<default>`, per the last fetch (no network) -- or `None` when
    that cannot be determined. Unknown must never read as current, so this
    returns a pair only when the count is a positive int.

    Resolves the checkout with `git -C <dirname(path)> rev-parse
    --show-toplevel`, never `freshness_lib.repo_root` -- that walks up to the
    first `.git` DIRECTORY, and a linked worktree's own `.git` is a FILE, so
    it would keep walking past the worktree into its primary checkout (see
    the plan's premise). `--show-toplevel` resolves the ACTUAL checkout,
    worktrees included.

    Never raises: a missing git, a non-repo path, a timeout, or `freshness_lib`
    failing to import all fall through to None.
    """
    if freshness_lib is None:
        return None
    try:
        # env=freshness_lib.clean_git_env(): an ambient GIT_DIR/etc would
        # override `-C` outright, so it must be stripped -- the `-C` target
        # above is what must win. One resolver, shared with `freshness_lib`'s
        # own `_git`, so the two can never drift on which vars are stripped.
        result = subprocess.run(
            ("git", "-C", os.path.dirname(path), "rev-parse", "--show-toplevel"),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=2,
            env=freshness_lib.clean_git_env(),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.decode("utf-8", "replace").strip()
    if not top:
        return None
    try:
        branch = freshness_lib.default_branch(top)
        n = freshness_lib.commits_behind(top, branch)
    except Exception:
        return None
    if isinstance(n, int) and n > 0:
        return (n, branch)
    return None


def claude_md_surfaces(workspace_root):
    """Every CLAUDE.md under the workspace, with the cap that applies to it.

    Walks rather than guessing a fixed list: the first draft of this enumerated
    six files from memory and there were seven -- two project subdirectories
    each carried one. A hand-kept list of paths is the shape this repo has
    already paid for twice.
    """
    found = []
    if not workspace_root or not os.path.isdir(workspace_root):
        return found
    skip = {".git", "node_modules", ".claude", "worktrees", ".venv"}
    for dirpath, dirs, files in os.walk(workspace_root):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if name in CLAUDE_MD_NAMES:
                found.append(os.path.realpath(os.path.join(dirpath, name)))
    return sorted(found)


def rules_surfaces(rules_dirs):
    """Sorted list of LOGICAL paths of every `*.md` under each directory in
    `rules_dirs`, recursive, deduped by realpath. A missing or `None`
    directory contributes nothing. Never raises.

    Claude Code follows a symlink inside `.claude/rules/` — a symlinked
    subdirectory of shared rules, or a single symlinked file — so the walk
    follows links too (`followlinks=True`), guarding against a cycle by
    tracking the realpath of every directory already entered and pruning any
    child whose realpath was already visited.

    The path returned for each file is the LOGICAL one, as walked from the
    given rules dir — NOT its realpath. A symlinked file's realpath can point
    anywhere, including outside every `.claude/rules/` directory, and
    `growth_lib` classifies and caps a file by that segment being present in
    its own path. A realpath'd rules file would be silently priced at the
    CLAUDE.md-family caps instead of the rules cap.

    Deduped by realpath so the same underlying file reached by two routes is
    one row, first logical path in sorted-walk order wins; the returned list
    itself is sorted by logical path.

    One source of truth: this only enumerates paths. Whether a given file is
    a rules file at all, and what it is capped at, are `growth_lib`'s
    predicates, not a second copy here.
    """
    found = {}  # realpath -> first logical path seen for it
    for rules_dir in rules_dirs or []:
        if not rules_dir or not os.path.isdir(rules_dir):
            continue
        try:
            visited_dirs = set()
            try:
                visited_dirs.add(os.path.realpath(rules_dir))
            except Exception:
                pass
            for dirpath, dirs, files in os.walk(rules_dir, followlinks=True):
                kept = []
                for name in sorted(dirs):
                    try:
                        real_dir = os.path.realpath(os.path.join(dirpath, name))
                    except Exception:
                        continue
                    if real_dir in visited_dirs:
                        continue  # already entered -- a cycle, skip it
                    visited_dirs.add(real_dir)
                    kept.append(name)
                dirs[:] = kept
                for name in sorted(files):
                    if not name.endswith(".md"):
                        continue
                    logical = os.path.join(dirpath, name)
                    try:
                        real_file = os.path.realpath(logical)
                    except Exception:
                        real_file = logical
                    if real_file not in found:
                        found[real_file] = logical
        except Exception:
            continue
    return sorted(found.values())


def _ancestor_dirs(root):
    """Every directory from `/` down to `realpath(root)`, inclusive, in that
    order -- the shape the 2.1.280 loader walks when it builds a session's
    CLAUDE.md family for a launch at `root` (Design Review, read from the
    binary)."""
    real = os.path.realpath(root)
    dirs = []
    current = real
    while True:
        dirs.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    dirs.reverse()
    return dirs


def always_on_files(workspace_root, home, managed_path=MANAGED_CLAUDE_MD):
    """Every instruction file the harness loads at launch for a session at
    `workspace_root`, in loader order: the managed policy file, the user's
    own `~/.claude/CLAUDE.md`, then -- for each directory from `/` down to
    `workspace_root` inclusive -- its `CLAUDE.md`, `.claude/CLAUDE.md` and
    `CLAUDE.local.md`.

    Existing regular files only, deduped by realpath (first occurrence
    wins). Returns the LOGICAL (as-built) path for each, not its realpath.
    Never raises.
    """
    candidates = []
    if managed_path:
        candidates.append(managed_path)
    if home:
        candidates.append(os.path.join(home, ".claude", "CLAUDE.md"))
    if workspace_root:
        for directory in _ancestor_dirs(workspace_root):
            candidates.append(os.path.join(directory, "CLAUDE.md"))
            candidates.append(os.path.join(directory, ".claude", "CLAUDE.md"))
            candidates.append(os.path.join(directory, "CLAUDE.local.md"))

    found = []
    seen_real = set()
    for path in candidates:
        try:
            if not os.path.isfile(path):
                continue
            real = os.path.realpath(path)
        except Exception:
            continue
        if real in seen_real:
            continue
        seen_real.add(real)
        found.append(path)
    return found


def project_slug(path):
    """The slug the harness derives for a project directory under
    `~/.claude/projects/`: every non-alphanumeric character of the realpath
    replaced with `-`. Verified against 2.1.280 (Design Review).

    Delegates to `auto_memory.project_slug` -- the one resolver, imported
    defensively -- so this and `install.sh`'s own slugging can never drift
    apart. Falls back to the identical inline regex if the import fails (a
    checkout missing `lib/auto_memory.py`), since this function must never
    raise.
    """
    try:
        import auto_memory

        return auto_memory.project_slug(path)
    except Exception:
        return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(path))


def _json_has_key(path, key):
    """True iff `path` parses as a JSON object carrying the top-level `key`.
    Unreadable or invalid JSON reads as the key being absent -- never raises."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return False
    return isinstance(data, dict) and key in data


def resolve_memory_dir(workspace_root, config, home):
    """`(path, None)` or `(None, reason)` -- design section 4.2a, in order:

    1. an explicit `context_budget.memory_dir` in config wins outright,
       regardless of `workspace_root` or `autoMemoryDirectory`.
    2. `autoMemoryDirectory` present (any value) at the top level of
       `<root>/.claude/settings.json` or `<root>/.claude/settings.local.json`
       -- refuse and ask for an override, exactly as before. Needs
       `workspace_root`; the harness's precedence between the two files is
       not reproduced, so presence in either is enough to refuse.
    3. `autoMemoryDirectory` present at the top level of
       `<home>/.claude/settings.json` (when `home` is given): resolved
       through `auto_memory.resolve_user_dir` (the one resolver,
       imported defensively), returned if it resolves to a real directory.
       If it's set but doesn't resolve, refuse with a DIFFERENT reason than
       step 2's, so the two are never confused. This step needs NO
       `workspace_root` at all -- N5 (`install.sh`) writes this key
       user-level, so it must resolve a bare `--report` with no root passed
       in.
    4. no `workspace_root` -> `(None, "no workspace root")`. This return used
       to sit at the top, before any settings check; it moved here because
       step 3 above must run even with no workspace root.
    5. a realpath whose slug (see `project_slug`) is over 200 chars, which the
       harness itself hashes rather than uses directly.
    6. otherwise `<home>/.claude/projects/<slug>/memory`, refused if it holds
       no `MEMORY.md`.

    Never raises; the caller passes the result straight through to a report.
    """
    if config is not None:
        try:
            override = config.get_path("context_budget", "memory_dir")
        except Exception:
            override = None
        if override:
            return (override, None)

    if workspace_root:
        root_for_settings = os.path.realpath(workspace_root)
        project_settings_paths = (
            os.path.join(root_for_settings, ".claude", "settings.json"),
            os.path.join(root_for_settings, ".claude", "settings.local.json"),
        )
        for settings_path in project_settings_paths:
            if _json_has_key(settings_path, "autoMemoryDirectory"):
                return (
                    None,
                    "autoMemoryDirectory is set; set context_budget.memory_dir",
                )

    if home:
        home_settings_path = os.path.join(home, ".claude", "settings.json")
        if _json_has_key(home_settings_path, "autoMemoryDirectory"):
            try:
                import auto_memory

                resolved = auto_memory.resolve_user_dir(home)
            except Exception:
                resolved = None
            if resolved:
                return (resolved, None)
            return (
                None,
                "autoMemoryDirectory is set but does not resolve to a "
                "directory; set context_budget.memory_dir",
            )

    if not workspace_root:
        return (None, "no workspace root")

    root = os.path.realpath(workspace_root)

    slug = project_slug(root)
    if len(slug) > 200:
        return (
            None,
            "project key over 200 chars is hashed by the harness; set "
            "context_budget.memory_dir",
        )

    candidate = os.path.join(home or "", ".claude", "projects", slug, "memory")
    index_path = os.path.join(candidate, "MEMORY.md")
    if not os.path.isfile(index_path):
        return (None, "no MEMORY.md at %s" % index_path)
    return (candidate, None)


def disabled_plugins(settings_path=None):
    """`{"plugin@marketplace", ...}` explicitly set false in Claude settings.

    Absent means enabled: the map only lists plugins someone has toggled, so
    treating "not listed" as disabled would silently zero the whole surface.
    Only an explicit `false` suppresses one.
    """
    path = settings_path or os.path.expanduser("~/.claude/settings.json")
    text = _read(path)
    if not text:
        return set()
    try:
        import json

        enabled = json.loads(text).get("enabledPlugins") or {}
    except Exception:
        return set()
    return {key for key, value in enabled.items() if value is False}


def _plugin_key(path, cache_root):
    """`plugin@marketplace` for a skill inside the plugin cache, else None.

    Cache layout is `<cache>/<marketplace>/<plugin>/<version>/<skills-dir>/<skill>/`,
    and `enabledPlugins` is keyed the other way round as `plugin@marketplace`.

    **The version segment is REQUIRED, and that is the whole point of the depth
    check.** The cache also holds transient clone directories shaped
    `<cache>/temp_git_<digits>_<slug>/skills/<skill>/` -- no marketplace, no
    version. Read with the 3-segment assumption those yielded a key like
    `skills@temp_git_1789211522602_l9baxe`, which appears in no settings map, so
    skills belonging to a DISABLED plugin were counted as enabled because a
    scratch copy of each also existed, which understated the excluded count.

    A path that does not match the shape is not a plugin install, so callers
    skip it rather than guessing.
    """
    try:
        rel = os.path.relpath(path, cache_root).split(os.sep)
    except Exception:
        return None
    # marketplace / plugin / version / skills-dir / skill  -- at least 4 before
    # the skill's own directory name is meaningful.
    if len(rel) < 4 or rel[0] in ("", ".."):
        return None
    return "%s@%s" % (rel[1], rel[0])


def skill_description_chars(roots, settings_path=None):
    """Chars of `description:` frontmatter across skills that ACTUALLY LOAD.

    Injected verbatim into every session's prompt and capped by nothing, which
    is why it is measured here at all.

    **A DISABLED plugin's skills are not injected and must not be counted.**
    Skills from plugins set `false` in settings can make up a large share of
    the cache. Counting them
    inflates the one figure a shrink decision is made from, and would argue for
    cutting skills that cost nothing. Returns the disabled tally too, so the
    report can say what it excluded rather than silently differing from a
    hand count of the directory.

    Counted per skill NAME: a skill reachable by two paths is one injection.
    """
    off = disabled_plugins(settings_path)
    live, dead = {}, {}

    def keep(bucket, name, mtime, size):
        """Newest wins. The cache keeps EVERY version it has ever fetched,
        and only one of them is loaded. Without this, the total
        silently depended on which stale copy `os.walk` happened to reach last,
        and the excluded count was per-FILE, so it could exceed the number of
        skills."""
        if name not in bucket or mtime > bucket[name][0]:
            bucket[name] = (mtime, size)

    for root in roots or []:
        if not root or not os.path.isdir(root):
            continue
        is_cache = os.path.basename(os.path.realpath(root)) == "cache"
        for dirpath, _dirs, files in os.walk(root):
            if "SKILL.md" not in files:
                continue
            path = os.path.join(dirpath, "SKILL.md")
            text = _read(path)
            if not text:
                continue
            match = _DESCRIPTION_RE.search(text[:4000])
            if not match:
                continue
            size = len(" ".join(match.group(1).split()))
            try:
                mtime = os.path.getmtime(path)
            except Exception:
                mtime = 0
            if is_cache:
                key = _plugin_key(dirpath, root)
                if key is None:
                    # Inside the cache but not shaped like a plugin install:
                    # a transient clone directory. Not a loaded skill, and
                    # treating it as one resurrects a disabled plugin's skills.
                    continue
            else:
                key = None
            keep(dead if (key and key in off) else live, os.path.basename(dirpath),
                 mtime, size)

    # A skill served by BOTH an enabled and a disabled source is still injected,
    # so it is not excluded.
    excluded = {n: v for n, v in dead.items() if n not in live}
    return (
        sum(v[1] for v in live.values()),
        len(live),
        len(excluded),
        sum(v[1] for v in excluded.values()),
    )


def imported_files(paths, home):
    """Follow `@path` imports from every file in `paths`.

    Returns `(targets, unresolved)`. `targets` is the logical path of every
    existing regular file reachable within `MAX_IMPORT_HOPS` hops (a direct
    import is hop 1), deduped by realpath, excluding anything already in
    `paths`; a cycle stops. `unresolved` is the set of realpaths of files
    (sources or targets) with an import that is not an existing regular file,
    or that sit at the hop limit with imports of their own -- they are only
    partly counted. If `claude_imports` is unavailable, returns `([], None)`
    and callers keep the old behaviour. Never raises.
    """
    if claude_imports is None:
        return [], None
    try:
        targets = []
        unresolved = set()
        seen = set()
        queue = []
        for path in paths:
            real = os.path.realpath(path)
            if real not in seen:
                seen.add(real)
                queue.append((path, 0))
        while queue:
            path, depth = queue.pop(0)
            text = _read(path)
            if text is None:
                continue
            imports = claude_imports.iter_imports(text)
            if not imports:
                continue
            if depth >= MAX_IMPORT_HOPS:
                unresolved.add(os.path.realpath(path))
                continue
            for _line, token in imports:
                target = claude_imports.resolve(token, path, home)
                if not os.path.isfile(target):
                    unresolved.add(os.path.realpath(path))
                    continue
                real = os.path.realpath(target)
                if real in seen:
                    continue
                seen.add(real)
                targets.append(target)
                queue.append((target, depth + 1))
        return targets, unresolved
    except Exception:
        return [], None


def _markdown_record(path, kind, klass, config):
    """One instruction record for a markdown surface (CLAUDE.md or a rules
    file): `count_markdown`/`has_import` from `instruction_lib`, capped by
    `growth_lib.caps_for_path` -- the same source `scan()` uses, so there is
    one place a cap can come from. `ok: False` (with no instructions/value)
    when the file exists but cannot be read."""
    text = _read(path)
    if text is None:
        return {
            "key": path,
            "kind": kind,
            "class": klass,
            "ok": False,
            "note": "unreadable",
            "partial": False,
            "instructions": None,
            "unit": None,
            "value": None,
            "cap": None,
        }
    cap = None
    try:
        import growth_lib

        cap = growth_lib.caps_for_path(path, config)[0]
    except Exception:
        cap = None
    return {
        "key": path,
        "kind": kind,
        "class": klass,
        "ok": True,
        "note": None,
        "partial": instruction_lib.has_import(text),
        "instructions": instruction_lib.count_markdown(text),
        "unit": "lines",
        "value": len(text.splitlines()),
        "cap": cap,
    }


def measure_instructions(workspace_root, memory_dir, memory_reason, config, home,
                          managed_path=MANAGED_CLAUDE_MD):
    """One record per instruction surface -- design section 4.2.

    Every record carries: `key`, `kind` (`claude_md`/`rules`/`memory_index`/
    `digest`), `class` (`always-on`/`conditional`), `ok`, `note`, `partial`,
    `instructions`, `unit`, `value`, `cap`. `ok: False` means the surface
    exists but could not be read, or (memory_index only) no memory directory
    resolved -- either way `instructions`/`value` are `None` and `note` says
    why. Never raises: every read and every optional import is guarded.
    """
    records = []

    # --- always-on CLAUDE.md family ----------------------------------------
    always_on_claude_paths = always_on_files(workspace_root, home, managed_path)
    for path in always_on_claude_paths:
        records.append(_markdown_record(path, "claude_md", "always-on", config))

    # --- @imports from the always-on CLAUDE.md files, followed ---------------
    # Only these traversed records take `partial` from the unresolved set;
    # conditional and rules records are not traversed and keep `has_import`.
    import_targets, unresolved = imported_files(always_on_claude_paths, home)
    for target in import_targets:
        records.append(_markdown_record(target, "claude_md", "always-on", config))
    if unresolved is not None:
        for rec in records:
            if rec["kind"] == "claude_md" and rec["ok"]:
                rec["partial"] = os.path.realpath(rec["key"]) in unresolved

    # --- .claude/rules files: pathless is always-on, scoped is conditional -
    rules_dirs = []
    if workspace_root:
        rules_dirs.append(os.path.join(workspace_root, ".claude", "rules"))
    if home:
        rules_dirs.append(claude_home.user_rules_dir(home))

    import_target_realpaths = {os.path.realpath(p) for p in import_targets}
    for path in rules_surfaces(rules_dirs):
        # Already an always-on import target: its record stands, no second one.
        if os.path.realpath(path) in import_target_realpaths:
            continue
        text = _read(path)
        try:
            import growth_lib

            scoped = growth_lib.has_paths_frontmatter(text)
        except Exception:
            # Unclassifiable is unknown, not always-on: defaulting here
            # over-counted every scoped rules file with no error (566 vs a
            # real 437, 09-28). `ok: False` makes always_on_total() None.
            records.append(
                {
                    "key": path,
                    "kind": "rules",
                    "class": "always-on",
                    "ok": False,
                    "note": "unclassified: growth_lib unavailable",
                    "partial": False,
                    "instructions": None,
                    "unit": None,
                    "value": None,
                    "cap": None,
                }
            )
            continue
        klass = "conditional" if scoped else "always-on"
        records.append(_markdown_record(path, "rules", klass, config))

    # --- conditional CLAUDE.md: everything else claude_md_surfaces finds ---
    already_realpaths = {os.path.realpath(p) for p in always_on_claude_paths}
    already_realpaths |= {os.path.realpath(p) for p in import_targets}
    for path in claude_md_surfaces(workspace_root):
        if os.path.realpath(path) in already_realpaths:
            continue
        records.append(_markdown_record(path, "claude_md", "conditional", config))

    # --- MEMORY.md Always rows, always-on -----------------------------------
    if memory_dir is None:
        records.append(
            {
                "key": "MEMORY.md",
                "kind": "memory_index",
                "class": "always-on",
                "ok": False,
                "note": memory_reason,
                "partial": False,
                "instructions": None,
                "unit": None,
                "value": None,
                "cap": None,
            }
        )
    else:
        index_path = os.path.join(memory_dir, "MEMORY.md")
        text = _read(index_path)
        if text is None:
            records.append(
                {
                    "key": index_path,
                    "kind": "memory_index",
                    "class": "always-on",
                    "ok": False,
                    "note": "unreadable",
                    "partial": False,
                    "instructions": None,
                    "unit": None,
                    "value": None,
                    "cap": None,
                }
            )
        else:
            try:
                import lint_lib

                index_layout = lint_lib.layout(memory_dir)
                index_cap = lint_lib.max_index_chars(config)
            except Exception:
                index_layout = "flat"
                index_cap = None
            records.append(
                {
                    "key": index_path,
                    "kind": "memory_index",
                    "class": "always-on",
                    "ok": True,
                    "note": None,
                    "partial": False,
                    "instructions": instruction_lib.count_always_rows(
                        text, index_layout
                    ),
                    "unit": "chars",
                    "value": len(text),
                    "cap": index_cap,
                }
            )

    # --- the TOOLING.md digest, always-on, only when the file exists -------
    tooling_path = _digest_path(workspace_root, config)
    if tooling_path:
        if os.path.isfile(tooling_path):
            text = _read(tooling_path)
            if text is None:
                records.append(
                    {
                        "key": tooling_path,
                        "kind": "digest",
                        "class": "always-on",
                        "ok": False,
                        "note": "unreadable",
                        "partial": False,
                        "instructions": None,
                        "unit": None,
                        "value": None,
                        "cap": None,
                    }
                )
            else:
                try:
                    import digest_lib

                    block = digest_lib.extract(text)
                    digest_cap = digest_lib.max_lines(config)
                except Exception:
                    block = None
                    digest_cap = None
                records.append(
                    {
                        "key": tooling_path,
                        "kind": "digest",
                        "class": "always-on",
                        "ok": True,
                        "note": None,
                        "partial": False,
                        "instructions": instruction_lib.count_digest(block),
                        "unit": "lines",
                        "value": len(block.splitlines()) if block else 0,
                        "cap": digest_cap,
                    }
                )

    return records


def always_on_budget(config):
    """`(value, state)` for `context_budget.always_on_budget` -- the ceiling
    the budget line in `--report` compares the always-on total against.

    `state` is one of:
      "unset"   -- `config` is `None`, or the key is absent/`null`.
      "ok"      -- a positive `int` that is not a `bool`.
      "invalid" -- anything else present: a `bool`, a `float`, a `str`, an
                   `int` <= 0, a list, etc. `value` is the raw config value,
                   for the caller to render with `%r`.

    Any exception reading `config` (a broken stand-in, a malformed file)
    counts as unset, matching the rest of this module's config-reading
    contract -- a missing budget is never worse than an absent one.
    """
    if config is None:
        return (None, "unset")
    try:
        raw = config.get("context_budget", "always_on_budget")
    except Exception:
        return (None, "unset")
    if raw is None:
        return (None, "unset")
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        return (raw, "invalid")
    return (raw, "ok")


def always_on_total(records):
    """Sum of `instructions` across always-on records; `None` if any of them
    is `ok: False` -- an unreadable surface makes the total unknown, not
    merely smaller."""
    always_on = [r for r in records if r.get("class") == "always-on"]
    if any(not r.get("ok") for r in always_on):
        return None
    return sum(r.get("instructions") or 0 for r in always_on)


def always_on_candidates(workspace_root, memory_dir, config, home,
                          managed_path=MANAGED_CLAUDE_MD):
    """The paths `measure_instructions` COULD class always-on, without
    counting anything -- the cheap gate `growth_lib`'s always-on branch
    checks an edited file against before ever calling `measure_instructions`
    (N6 design, "The contract").

    A `set` of realpaths: `always_on_files(workspace_root, home,
    managed_path)` -- the CLAUDE.md family -- union every file that family
    `@`-imports (`imported_files`, up to 4 hops), union every `*.md` under
    either rules directory (`rules_surfaces`), union `memory_dir/MEMORY.md`
    when `memory_dir` is given, union the TOOLING.md digest path
    (`_digest_path`) when it resolves to something. Built from the SAME
    functions `measure_instructions` itself calls, so the two can never
    silently drift apart.

    Deliberately NOT filtered to pathless rules files: a `paths:`-scoped
    rules file is still a candidate here (this function never reads a
    rules file, so it cannot yet tell scoped from pathless) -- the real
    always-on/conditional classification happens downstream, against the
    measured records, at the point that actually reads them.

    Reads the CLAUDE.md family's text to follow `@` imports, and nothing else.
    Never raises: any exception anywhere in this
    function returns an empty set rather than a partial one.
    """
    try:
        found = set()
        family = always_on_files(workspace_root, home, managed_path)
        for path in family:
            found.add(os.path.realpath(path))
        # Imports are followed, so an imported file is a candidate too.
        for path in imported_files(family, home)[0]:
            found.add(os.path.realpath(path))

        rules_dirs = []
        if workspace_root:
            rules_dirs.append(os.path.join(workspace_root, ".claude", "rules"))
        if home:
            rules_dirs.append(claude_home.user_rules_dir(home))
        for path in rules_surfaces(rules_dirs):
            found.add(os.path.realpath(path))

        if memory_dir:
            found.add(os.path.realpath(os.path.join(memory_dir, "MEMORY.md")))

        digest = _digest_path(workspace_root, config)
        if digest:
            found.add(os.path.realpath(digest))

        return found
    except Exception:
        return set()


def scan(workspace_root, memory_dir, skill_roots=None, config=None,
         settings_path=None, user_rules_dir=None, memory_reason=None):
    """Every surface, measured. Best-effort: a surface that cannot be read is
    reported as unreadable rather than omitted, because a silently missing row
    reads as a surface that costs nothing."""
    surfaces = []

    def add(name, path, unit, measured, cap, note=""):
        surfaces.append(
            {
                "name": name,
                "path": path,
                "unit": unit,
                "value": measured,
                "cap": cap,
                "over": (measured - cap) if (cap and measured > cap) else 0,
                "note": note,
            }
        )

    # --- the CLAUDE.md family, in LINES -----------------------------------
    caps_for_path = None
    has_paths_frontmatter = None
    try:
        import growth_lib

        caps_for_path = growth_lib.caps_for_path
        has_paths_frontmatter = growth_lib.has_paths_frontmatter
    except Exception:
        pass

    for path in claude_md_surfaces(workspace_root):
        text = _read(path)
        if text is None:
            add("CLAUDE.md", path, "lines", 0, None, "unreadable")
            continue
        lines, _chars, _b = _measure(text)
        cap = None
        if caps_for_path is not None:
            try:
                cap = caps_for_path(path, config)[0]
            except Exception:
                cap = None
        note = ""
        behind = checkout_behind(path)
        if behind:
            behind_n, behind_branch = behind
            note = "local checkout %d behind origin/%s (as of last fetch)" % (
                behind_n,
                behind_branch,
            )
        add("CLAUDE.md", path, "lines", lines, cap, note)

    # --- .claude/rules files, in LINES ------------------------------------
    rules_dirs = []
    if workspace_root:
        rules_dirs.append(os.path.join(workspace_root, ".claude", "rules"))
    if user_rules_dir:
        rules_dirs.append(user_rules_dir)

    for path in rules_surfaces(rules_dirs):
        text = _read(path)
        if text is None:
            add("rules file", path, "lines", 0, None, "unreadable")
            continue
        lines, _chars, _b = _measure(text)
        cap = None
        if caps_for_path is not None:
            try:
                cap = caps_for_path(path, config)[0]
            except Exception:
                cap = None
        if has_paths_frontmatter is not None:
            try:
                note = ("path-scoped" if has_paths_frontmatter(text)
                        else "NO paths: — loads at session start")
            except Exception:
                note = "unchecked"
        else:
            note = "unchecked"
        add("rules file", path, "lines", lines, cap, note)

    # --- the TOOLING.md digest block, in LINES ----------------------------
    tooling = _digest_path(workspace_root, config)
    if tooling:
        text = _read(tooling)
        if text is not None:
            try:
                import digest_lib

                block = digest_lib.extract(text)
                cap = digest_lib.max_lines(config)
                add(
                    "TOOLING digest",
                    tooling,
                    "lines",
                    len(block.splitlines()) if block else 0,
                    cap,
                    # Not "truncates at the tail" — that was true until
                    # 2026-09-14 and is the failure M3 removed. Over cap the
                    # whole block is withheld, so the cost of crossing is the
                    # entire map rather than its last rows. A report that
                    # still said "tail" would understate the one surface whose
                    # overage is now total.
                    "withheld ENTIRELY over cap",
                )
            except Exception:
                add("TOOLING digest", tooling, "lines", 0, None, "unreadable")

    # --- the MEMORY.md index, in CHARACTERS -------------------------------
    if memory_dir:
        try:
            import lint_lib

            result = lint_lib.scan(memory_dir, config)
            if result.get("readable"):
                add(
                    "MEMORY.md index",
                    os.path.join(memory_dir, "MEMORY.md"),
                    "chars",
                    result["index_chars"],
                    lint_lib.max_index_chars(config),
                    "%d rows; harness drops rows past ~%d"
                    % (result["rows"], lint_lib.INDEX_TRUNCATION_CHARS),
                )
                add(
                    "MEMORY.md index lines",
                    os.path.join(memory_dir, "MEMORY.md"),
                    "lines",
                    result["index_lines"],
                    lint_lib.max_index_lines(config),
                    "harness drops lines past ~%d" % lint_lib.INDEX_TRUNCATION_LINES,
                )
        except Exception:
            pass
    elif memory_reason:
        # No memory dir resolved -- the row still needs to exist so the
        # reason gets seen, rather than the surface silently vanishing.
        add(
            "MEMORY.md index",
            "MEMORY.md",
            "chars",
            0,
            None,
            "unreadable: %s" % memory_reason,
        )

    # --- skill descriptions, in CHARACTERS, capped by nothing -------------
    total, count, off_count, off_chars = skill_description_chars(
        skill_roots, settings_path
    )
    if count or off_count:
        note = "injected every session, no cap anywhere"
        if off_count:
            # Said out loud so the figure can be reconciled against a hand
            # count of the directory, which will be larger.
            note += "; %d skill(s) / %d chars excluded as disabled plugins" % (
                off_count,
                off_chars,
            )
        add("skill descriptions", "%d skills" % count, "chars", total, None, note)

    return surfaces


def injected_total(surfaces):
    """Chars across the CHAR-MEASURED surfaces — not across everything injected.

    The CLAUDE.md family is injected too and is excluded here, because it is
    measured in lines and a line is not a number of characters. Converting one
    to the other needs a ratio nobody measured, which is how a derived figure
    starts being quoted as a reading. The rendered label says "char-measured
    surfaces" for the same reason; this total is a floor on the injected cost,
    never the whole of it."""
    return sum(s["value"] for s in surfaces if s["unit"] == "chars")


def report(workspace_root, memory_dir, skill_roots=None, config=None,
           settings_path=None, user_rules_dir=None, memory_reason=None,
           instruction_records=None, readings=None, readings_path=None,
           today=None):
    """`instruction_records`/`readings`/`readings_path`/`today` are the
    instruction-meter section's inputs (design 4.7): when `instruction_records`
    is given, `instructions_section` is appended after the existing table,
    even when that table itself is empty (an empty workspace can still carry
    an instruction reading, e.g. from MEMORY.md alone)."""
    surfaces = scan(workspace_root, memory_dir, skill_roots, config,
                    settings_path, user_rules_dir, memory_reason)
    if not surfaces:
        lines = ["No context surfaces found under %s" % (workspace_root or "<none>")]
        return _append_instructions_section(
            lines, instruction_records, readings, readings_path, today, config
        )

    width = max(len(s["name"]) for s in surfaces)
    lines = ["%-*s  %9s  %8s  %s" % (width, "surface", "measured", "cap", "state")]
    for s in surfaces:
        unreadable = bool(s["note"]) and s["note"].startswith("unreadable")
        if unreadable:
            # The full "unreadable" / "unreadable: <reason>" string, shown
            # exactly once -- the "append the note" step below is skipped for
            # this row so the reason never doubles up.
            state = s["note"]
        elif s["cap"] is None:
            state = "no cap"
        elif s["over"]:
            state = "OVER by %d" % s["over"]
        else:
            state = "%d spare" % (s["cap"] - s["value"])
        if s["note"] and not unreadable:
            state += " — " + s["note"]
        lines.append(
            "%-*s  %6d %s  %8s  %s"
            % (
                width,
                s["name"],
                s["value"],
                s["unit"][:5],
                s["cap"] if s["cap"] is not None else "-",
                state,
            )
        )
        lines.append("%-*s    %s" % (width, "", s["path"]))

    over = [s for s in surfaces if s["over"]]
    lines.append("")
    lines.append(
        "char-measured surfaces total %d chars across %d surface(s)"
        % (injected_total(surfaces), sum(1 for s in surfaces if s["unit"] == "chars"))
    )
    if over:
        # By PATH, never by name: seven surfaces here are called CLAUDE.md, so
        # a summary naming one of them identifies nothing. The summary line is
        # the part someone acts on without scrolling back up.
        lines.append("%d surface(s) over cap:" % len(over))
        for s in over:
            lines.append(
                "  %s — %d %s over %s" % (s["path"], s["over"], s["unit"], s["cap"])
            )
    else:
        lines.append("no surface over cap")

    # Named separately from the over-cap summary above: a pathless rules file
    # is a standing cost whether or not it happens to be over its own size
    # cap, so it earns its own line rather than riding on "over cap".
    pathless = [s for s in surfaces if s["note"].startswith("NO paths:")]
    if pathless:
        lines.append("%d rules file(s) with no paths: — always loaded:"
                     % len(pathless))
        for s in pathless:
            lines.append("  %s" % s["path"])
    return _append_instructions_section(
        lines, instruction_records, readings, readings_path, today, config
    )


def _append_instructions_section(lines, instruction_records, readings,
                                  readings_path, today, config=None):
    """Shared tail of `report()`'s two return paths (empty table and full
    table): append `instructions_section`'s lines when `instruction_records`
    is given, then join. A helper rather than duplicated logic at both call
    sites, so the two paths cannot drift on how the section is attached.

    `config` is `report()`'s own, forwarded so the budget line can read
    `context_budget.always_on_budget`."""
    if instruction_records is not None:
        lines = list(lines)
        lines.append("")
        lines.extend(
            instructions_section(
                instruction_records, readings or [], today, readings_path,
                config,
            )
        )
    return "\n".join(lines)


def _format_rate(rate, reason):
    """`%+.1f/wk` when a rate exists, else the insufficient-history reason
    `rate_per_week` already produced (design 4.7's row format)."""
    if rate is None:
        return reason
    return "%+.1f/wk" % rate


def _format_forecast(result):
    """`forecast()`'s three shapes rendered for a row: a float becomes a
    day count, the two sentinel strings ("over", "not growing") pass through,
    and `None` (no cap, no rate, or no live reading) renders nothing."""
    if result is None:
        return None
    if isinstance(result, str):
        return result
    return "~%.0f days to cap" % result


def instructions_section(records, readings, today, readings_path=None,
                          config=None):
    """The instruction-meter section appended after the existing table
    (design 4.7). One row per always-on surface: its live count (or why it is
    unreadable), a rate/wk from the log, a days-to-cap forecast built from the
    log's slope and this run's own live value/cap, and any partial/staleness
    flags. Then the always-on total, the budget line, the anchor line, the
    conditional surfaces (never totalled), the fixed excluded line, and the
    log line.

    `readings_path` is not part of the (records, readings, today) triple the
    plan names for this function, because the log line needs to name the
    file readings came from and nothing else passed in carries that path;
    it defaults to None (rendered as "-") for a caller that has no path to
    give. `config` is likewise appended rather than folded into the triple,
    read only for the budget line's `context_budget.always_on_budget`.
    """
    always_on = [r for r in records if r.get("class") == "always-on"]
    conditional = [r for r in records if r.get("class") == "conditional"]

    lines = ["always-on instructions — unit v2: sentences (a proxy)"]

    status, newest = readings_lib.log_status(readings, today) if today is not None \
        else ("none", None)
    if status == "stale":
        lines.append(
            "RECORDER STALE — last reading %s; rates below are frozen"
            % newest.isoformat()
        )
    elif status == "none":
        lines.append("no readings yet — has the SessionStart recorder run?")

    for r in always_on:
        key = r.get("key")
        if not r.get("ok"):
            lines.append("%s  unreadable: %s" % (key, r.get("note")))
            continue

        if today is not None:
            rate, reason = readings_lib.rate_per_week(
                readings_lib.series(readings, key, "instructions"), today
            )
            value_rate, _value_reason = readings_lib.rate_per_week(
                readings_lib.series(readings, key, "value"), today
            )
        else:
            rate, reason, value_rate = None, "no readings", None

        pieces = [
            "%s  %d instr  %s"
            % (key, r.get("instructions") or 0, _format_rate(rate, reason))
        ]
        forecast_text = _format_forecast(
            readings_lib.forecast(value_rate, r.get("value"), r.get("cap"))
        )
        if forecast_text:
            pieces.append(forecast_text)
        if r.get("partial"):
            pieces.append("partial — @imports not followed")
        stale = readings_lib.surface_stale_since(readings, key) if readings else None
        if stale:
            pieces.append("stale since %s" % stale.isoformat())
        lines.append("  ".join(pieces))

    total = always_on_total(records)
    if total is None:
        n_unreadable = sum(1 for r in always_on if not r.get("ok"))
        lines.append(
            "always-on total  unavailable — %d surface(s) unreadable" % n_unreadable
        )
    else:
        if today is not None:
            total_rate, total_reason = readings_lib.rate_per_week(
                readings_lib.total_series(readings), today
            )
        else:
            total_rate, total_reason = None, "no readings"
        lines.append(
            "always-on total  %d  %s" % (total, _format_rate(total_rate, total_reason))
        )

    budget, budget_state = always_on_budget(config)
    if budget_state == "unset":
        lines.append("budget  not set — set context_budget.always_on_budget")
    elif budget_state == "invalid":
        lines.append(
            "budget  invalid context_budget.always_on_budget: %r — ignored"
            % budget
        )
    else:  # "ok"
        if total is None:
            lines.append("budget  %d — total unavailable, not compared" % budget)
        elif total <= budget:
            lines.append(
                "budget  %d / %d  %d spare" % (total, budget, budget - total)
            )
        else:
            lines.append(
                "budget  %d / %d  OVER by %d" % (total, budget, total - budget)
            )

    # The anchor ratio: only always-on, readable CLAUDE.md-family records with
    # a positive line count contribute -- never memory_index (a different
    # unit, chars) and never a conditional CLAUDE.md (loaded on demand, not
    # part of the standing cost the anchor is measuring).
    anchor_records = [
        r for r in always_on
        if r.get("kind") == "claude_md"
        and r.get("ok")
        and isinstance(r.get("value"), int)
        and not isinstance(r.get("value"), bool)
        and r.get("value") > 0
    ]
    if not anchor_records:
        lines.append(
            "anchor  unavailable — no readable always-on CLAUDE.md to take "
            "a ratio from"
        )
    else:
        anchor_s = sum(r.get("instructions") or 0 for r in anchor_records)
        anchor_l = sum(r.get("value") for r in anchor_records)
        anchor_a = round(200 * anchor_s / anchor_l)
        anchor_line = (
            "anchor  Anthropic \"under 200 lines per CLAUDE.md\" ≈ %d "
            "sentences at these files' own %d sentences / %d lines "
            "(inferred ratio; guidance, not a limit)"
            % (anchor_a, anchor_s, anchor_l)
        )
        if total is not None and anchor_a > 0:
            anchor_line += " — total is %.1f× that" % (total / anchor_a)
        lines.append(anchor_line)

    def _cond_piece(r):
        if not r.get("ok"):
            note = r.get("note")
            if note and note != "unreadable":
                return "%s unreadable: %s" % (r.get("key"), note)
            return "%s unreadable" % r.get("key")
        return "%s %d" % (r.get("key"), r.get("instructions") or 0)

    if conditional:
        cond_text = ", ".join(_cond_piece(r) for r in conditional)
    else:
        cond_text = "none"
    lines.append("conditional (loaded on demand, not totalled): " + cond_text)

    lines.append(
        "excluded: skill listing, .remember, hook-injected text, "
        "MEMORY.md Topics rows"
    )

    if readings:
        first = readings[0]["date"].isoformat()
        last = readings[-1]["date"].isoformat()
    else:
        first = last = "-"
    lines.append(
        "log: %d readings %s → %s (%s)"
        % (len(readings), first, last, readings_path or "-")
    )

    return lines
