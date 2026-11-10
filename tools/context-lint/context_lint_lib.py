#!/usr/bin/env python3
"""Size caps for CLAUDE.md files, resolved and checked.

A CLAUDE.md is injected into every session that loads it, so a line added there
is paid forever while costing nothing at the moment it is written. That asymmetry
is the whole reason these files only grow: nothing measures them and nothing
pushes back.

Why this exists rather than a style guide: a CLAUDE.md that was just cut can
regrow within the hour, from a session doing correct work with no signal that
a budget existed.

Two design choices worth not re-litigating:

**Caps live in config, not here, and per-path overrides are a RATCHET.** A file
over the default today gets an override sized just above it, so the check fires
on GROWTH rather than screaming about a state nobody can fix in one commit. An
override is lowered as the file is cut. It is not a permanent exemption, and a
check that fires constantly is a check that gets ignored.

**A cap is keyed on `<repo-name>/<path-within-repo>` and matched EXACTLY, and the
key is computed by the CALLER — identically for both of them.** The first cut let
each caller pass whatever path shape it had and matched by suffix, which failed
three ways at once and shipped through 24 green tests:

  - `git diff --cached --name-only` emits repo-root-relative paths (`CLAUDE.md`),
    so no key carrying a repo-directory component could ever match. Every
    override was dead at commit time and every file fell to the default of the day —
    including a long real `CLAUDE.md`, which would have become
    uncommittable the moment it landed.
  - `--report` walks absolute paths, so overrides DID resolve there. The human
    view certified `ok` on the exact file the hook refused, which is what would
    have kept the first defect from being noticed.
  - A suffix cannot tell "a repo named `docs`" from "a directory named
    `docs`", so an unrelated repo on this machine inherited someone else's
    ratchet.

The repo name comes from `--git-common-dir`, not `--show-toplevel`, so every
worktree resolves to the same key as the primary checkout. That is the property
suffix matching was reaching for, and it is real here rather than assumed: a
worktree of this repo and the primary checkout both key as
`claude-code-toolkit/CLAUDE.md`.
"""

import os
import subprocess
import sys

# realpath, not abspath: this module is reached through symlinked executables in
# ~/.local/bin, and abspath would land on /Users and fail at runtime while
# passing every test. Resolved here rather than trusting a caller, same reason
# digest_lib does it: there are three importers now.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
_LIB = os.path.join(_ROOT, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)
# digest_lib owns the number past which the tooling map's Core is WITHHELD
# ENTIRELY (it trimmed to that number until 2026-09-14),
# and `core_cap_for` below has to agree with it. Importing it here is what makes
# that one number instead of two. digest_lib itself imports only core_block and
# stdlib at module level, so this adds no config dependency and no cycle.
_DIGEST = os.path.join(_ROOT, "tools", "tooling-digest")
if _DIGEST not in sys.path:
    sys.path.insert(0, _DIGEST)

import core_block  # noqa: E402
import digest_lib  # noqa: E402

# ONE NUMBER FOR EVERY CLAUDE.md ON THE MACHINE. Ruled 2026-09-15, and the
# uniformity is the point rather than the number.
#
# It was 400 with a per-repo override dict above it. That shape cost more than it
# bought: every cap was set at "current size + ~8%", so each one sat days from
# full, and the only way to land a legitimate new rule was to cut something or
# raise the cap in the same commit. Headroom was consumed within days in
# every repo. The ratchet
# then moved the WRONG way, raising caps, because a cap that
# admits no further rule routes the next lesson somewhere nobody greps. A
# ratchet that only ever goes up is not a ratchet.
#
# 800 is a real trade and it should be read as one. Anthropic's own guidance is
# "target under 200 lines per CLAUDE.md file. Longer files consume more context
# and reduce adherence" (code.claude.com/docs/en/memory, read 2026-09-15), so
# this is deliberately ~4x the recommendation. What buys that: these files are
# the operating contract for many concurrent sessions, the alternative shape
# was a cut-to-add treadmill nobody was winning, and the
# STRUCTURAL fix is path-scoped rules (`.claude/rules/` with `paths:`
# frontmatter), which load only when Claude touches matching files and cost
# nothing at session start. Until a repo moves its rules there, 800 stops the
# ceremony without pretending the attention cost is free.
#
# The only hard limit is 4 MiB — Claude Code loads a CLAUDE.md up to that size in
# full and SKIPS a larger one entirely. 800 lines of prose is nowhere near it, so
# nothing here risks a silently unloaded file.
DEFAULT_MAX_LINES = 800

# The Core is the part of a context file guaranteed to be read, so its budget is
# tighter than the whole file's and separate from it.
DEFAULT_CORE_MAX_LINES = 80

# TWO FILENAME LISTS, TWO JOBS — do not conflate them.
#
# `is_context_file` (below) gates the WHOLE-FILE cap and stays CLAUDE.md-only.
# This list gates the two CORE checks and includes the tooling map, because
# TOOLING.md is the only file that has a Core today — gating the Core cap on
# `is_context_file` exempted it from the one cap the contract was written for.
#
# Widening `is_context_file` instead is the wrong fix: it also gates `check()`,
# so a long TOOLING.md would fall under the CLAUDE.md whole-file cap
# (DEFAULT_MAX_LINES) and report far over it on every edit. A
# notice that fires constantly is a notice nobody reads, which is the failure this
# whole contract indicts. Standardizing that cap in 2026-09 did not change this:
# no plausible whole-file number bounds a long reference document.
DEFAULT_CORE_FILES = ("CLAUDE.md", "CLAUDE.md.template",
                      "TOOLING.md", "TOOLING.md.template")

# EMPTY, AND THAT IS THE CHANGE — every CLAUDE.md now resolves to the single
# DEFAULT_MAX_LINES above. Ruled 2026-09-15: "repos should have a
# standardized limit (so same across all)."
#
# This dict used to carry a per-repo number each. Deleting it removes the bookkeeping that
# made every cap a negotiation: there is no longer a number to ratchet, no
# owner-qualified key to get wrong, and no way for two repos to disagree about
# what a reasonable CLAUDE.md is. The owner-qualification machinery below stays
# — `cap_key` still builds both candidates and `cap_for` still matches most
# specific first — because a HAND-WRITTEN config override must keep working, and
# the case that made qualification necessary (two clones of different repos
# with the same name, of very different sizes, keying identically) is
# exactly such an override. Do not delete `_QualifiedKey` on the strength of this dict being
# empty; they are independent, and a test pins the precedence order.
#
# A repo that genuinely needs more still says so in
# ~/.config/claude-code-toolkit/config.json under context_lint.overrides, where
# the number is visible and personal rather than shipped to everyone.
DEFAULT_OVERRIDES = {}


def _is_cap(value):
    """True for a usable cap: a positive integer that is not a bool.

    JSON `true` loads as Python True, which IS an int (== 1), so a bare
    `isinstance(value, int)` let a hand-edited `"max_lines": true` become a
    one-line cap that blocks every commit touching a CLAUDE.md.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def caps(config=None):
    """Resolve {path_suffix: max_lines} plus the default, from config.

    Returns (default_max, overrides). Never raises: a caller is a git hook, and
    an unreadable config must fall back to the shipped numbers rather than
    wedging every commit on the machine.
    """
    default = DEFAULT_MAX_LINES
    overrides = dict(DEFAULT_OVERRIDES)
    if config is None:
        return default, overrides
    try:
        configured = config.get("context_lint", "max_lines", default=None)
        if _is_cap(configured):
            default = configured
        extra = config.get("context_lint", "overrides", default=None)
        if isinstance(extra, dict):
            for path, limit in extra.items():
                if _is_cap(limit):
                    overrides[str(path)] = limit
    except Exception:
        # Config present but malformed. Shipped defaults are the safe answer:
        # a cap that is too generous still catches runaway growth, while an
        # exception here would block every commit on the machine.
        return DEFAULT_MAX_LINES, dict(DEFAULT_OVERRIDES)
    return default, overrides


def cap_for(key, default_max, overrides):
    """The cap for a canonical key, matched EXACTLY.

    `key` is either one key or, from `cap_key`, a `_QualifiedKey` carrying
    `.candidates` ordered MOST SPECIFIC FIRST (`<owner>/<repo>/<path>` then
    `<repo>/<path>`). A plain list of candidates is accepted too. The
    first that matches wins; a list is not "any of these", it is a precedence
    order, so an owner-qualified override always beats a bare one for the same
    file. None, or an empty list, means the file is not in a git repo at all.

    Exact, never suffix: a suffix cannot distinguish a repo named `docs`
    from any repo that merely contains a directory of that name, and through a
    global `core.hooksPath` this runs against every repo on the machine. An
    unmatched key is not an error — it is a repo we have no opinion about, and
    it gets the default.
    """
    if not key:
        return default_max
    candidates = list(getattr(key, "candidates", ()) or
                      ([key] if isinstance(key, str) else key))
    normalized_overrides = {
        str(c).replace(os.sep, "/"): limit for c, limit in overrides.items()
    }
    for candidate in candidates:
        if candidate is None:
            continue
        hit = normalized_overrides.get(str(candidate).replace(os.sep, "/"))
        if hit is not None:
            return hit
    return default_max


def is_context_file(path):
    """True for the files the WHOLE-FILE cap applies to.

    Deliberately NOT widened to the tooling map — see DEFAULT_CORE_FILES. This
    gates `check()` and `--report`'s whole-file table, so adding TOOLING.md here
    would put a long map under the CLAUDE.md whole-file cap.
    """
    name = os.path.basename(str(path).replace(os.sep, "/"))
    return name in ("CLAUDE.md", "CLAUDE.md.template")


def core_files(config=None):
    """Filenames the Core checks apply to. Never raises — a caller is a hook."""
    if config is None:
        return DEFAULT_CORE_FILES
    try:
        configured = config.get("context_lint", "core_files", default=None)
        if isinstance(configured, (list, tuple)):
            names = tuple(str(n) for n in configured if str(n).strip())
            if names:
                return names
    except Exception:
        return DEFAULT_CORE_FILES
    return DEFAULT_CORE_FILES


def is_core_file(path, names=None):
    """True for a file the Core contract applies to. A BASENAME TEST, NO READ.

    Pure and cheap on purpose, so the loop can filter BEFORE calling read_text —
    the same position `check()` puts `is_context_file` at, and for the same
    reason. In `--staged` the reader forks `git show :<path>` per call with a 10s
    timeout, `entries` is every staged path unfiltered, and this hook runs
    through a global `core.hooksPath` on every repo on the machine. Scoping on
    "does the file carry a Core block" instead would mean reading every staged
    file to find out, making a lockfile bump or a rebase-squash fork one git
    subprocess per file for a check that can only ever fire on four filenames.
    """
    name = os.path.basename(str(path).replace(os.sep, "/"))
    return name in (DEFAULT_CORE_FILES if names is None else tuple(names))


def core_caps(config=None):
    """(default_core_max, overrides) for Core blocks. Mirrors caps() exactly.

    Same failure behaviour: never raises, and a malformed config falls back to
    the shipped number rather than wedging every commit on the machine.
    """
    default = DEFAULT_CORE_MAX_LINES
    overrides = {}
    if config is None:
        return default, overrides
    try:
        configured = config.get("context_lint", "core_max_lines", default=None)
        if _is_cap(configured):
            default = configured
        extra = config.get("context_lint", "core_overrides", default=None)
        if isinstance(extra, dict):
            for path, limit in extra.items():
                if _is_cap(limit):
                    overrides[str(path)] = limit
    except Exception:
        return DEFAULT_CORE_MAX_LINES, {}
    return default, overrides


def tooling_name(config=None):
    """Basename of the tooling map. Never raises."""
    if config is not None:
        try:
            configured = config.get_path("tooling_digest", "path", default=None)
            if configured:
                return os.path.basename(str(configured))
        except Exception:
            pass
    return digest_lib.DEFAULT_FILENAME


def core_cap_for(key, source, default_max, overrides, config=None):
    """Core cap for ONE file. Every Core check resolves its cap through here.

    **The tooling map is capped where its Core actually FAILS.** `digest_lib`
    caps the injected block at `tooling_digest.max_lines`, and since 2026-09-14
    a Core longer than that is withheld ENTIRELY — the session gets no map, not
    a trimmed one. Capping the map at `core_max_lines` instead shipped TWO
    numbers for one file: at the defaults, a 61-to-80-line Core passed the only
    check that can block a commit and still lost the whole block at 60. The
    check that can refuse must be the one measuring the harm.

    The harm this sentence described changed — it used to be a silently lost
    tail — but the conclusion did not, and got stronger: the cost of crossing
    the digest budget is now the entire map rather than its last few rows, so a
    commit-time check calibrated to a different number is worth less, not more.

    "Two Core caps, do not unify them" remains true and is not what this
    undoes: the two caps are 80 for the CLAUDE.md family and the digest budget
    for the map — two DIFFERENT FILES, one number each. Two numbers for the
    same file was never the design, only the defect.

    `digest_lib.max_lines(None)` is deliberately not called: it falls back to
    ambient config, and this module is hermetic by construction — `config=None`
    means shipped defaults everywhere in it.
    """
    try:
        name = os.path.basename(str(source))
        mapname = tooling_name(config)
        # The TEMPLATE gets the map's cap too, and that is not cosmetic.
        # `DEFAULT_CORE_FILES` Core-checks both, but until 2026-09-15 only the
        # map itself resolved to the digest budget, so a template Core in the
        # 61-to-80 band passed lint at `core_max_lines` and was copied by
        # /onboard into a real map that is over the 60-line digest budget on
        # arrival. That used to cost a new user a silently trimmed tail;
        # since the digest began withholding the whole block it costs them
        # their entire map, on a machine they cannot yet debug.
        # A template is a seed, so it is capped by what it GERMINATES INTO.
        is_map = name in (mapname, mapname + ".template")
    except Exception:
        is_map = False
    if is_map:
        return (digest_lib.DEFAULT_MAX_LINES if config is None
                else digest_lib.max_lines(config))
    return cap_for(key, default_max, overrides)


def core_findings(entries, read_text, config=None):
    """Core blocks over their size cap. BLOCKING half of the asymmetry.

    A counted fact, so it blocks a commit exactly as the whole-file cap does.
    Same shape as `check()`, including the filter-before-read ordering.
    """
    default_max, overrides = core_caps(config)
    names = core_files(config)
    findings = []
    for key, source in entries:
        if not is_core_file(source, names):
            continue
        try:
            content = read_text(source)
        except Exception:
            continue
        if content is None:
            continue
        core = core_block.extract(content)
        if not core:
            continue
        core_lines = count_lines(core)
        cap = core_cap_for(key, source, default_max, overrides, config)
        if core_lines > cap:
            findings.append({"path": str(source), "key": key,
                             "core_lines": core_lines, "cap": cap,
                             "over": core_lines - cap})
    return findings


def sufficiency_findings(entries, read_text, config=None):
    """Cross-references inside a Core that depend on the body. ADVISORY half.

    A heuristic on prose, so it prints and never touches the exit code. A
    heuristic that blocks a commit is one somebody routes around with
    --no-verify — taking the size cap with it.

    Extracts the Core FIRST and scans only that. Scanning the whole file makes
    every elaborated rule a finding, which is the noise that kills the check.
    """
    names = core_files(config)
    findings = []
    for _key, source in entries:
        if not is_core_file(source, names):
            continue
        try:
            content = read_text(source)
        except Exception:
            continue
        if content is None:
            continue
        core = core_block.extract(content)
        if not core:
            continue
        refs = core_block.dangling_refs(core)
        if refs:
            findings.append({"path": str(source), "refs": refs})
    return findings


def count_lines(text):
    """Line count of file content, matching `wc -l` for trailing-newline files."""
    return len(text.splitlines())


def check(entries, read_text, config=None):
    """Check each entry against its cap.

    `entries` is an iterable of `(key, source)`. **The caller computes the key**,
    because only the caller knows how to turn what it has into the canonical
    `<repo-name>/<path-within-repo>` form — and letting each caller pass its own
    path shape into a fuzzy match is exactly the defect this signature exists to
    prevent. `key` may be None, meaning "not in a repo": that gets the default.

    `read_text` is injected so the caller decides where content comes from — the
    working tree for `--report`, `git show :path` for `--staged`. Reading the
    staged blob rather than the file on disk matters: a partially staged file
    would otherwise be judged on its unstaged size, which is wrong in both
    directions at once.

    Returns a list of dicts for OVER-cap files only. Empty means within budget.
    """
    default_max, overrides = caps(config)
    findings = []
    for key, source in entries:
        if not is_context_file(source):
            continue
        try:
            content = read_text(source)
        except Exception:
            # A file we cannot read is not evidence of growth. Skipping is right:
            # blocking a commit because of an unreadable path would punish the
            # wrong thing, and the report path must not crash on a stale entry.
            continue
        if content is None:
            continue
        lines = count_lines(content)
        cap = cap_for(key, default_max, overrides)
        if lines > cap:
            findings.append({"path": str(source), "key": key, "lines": lines,
                             "cap": cap, "over": lines - cap})
    return findings


def format_block(findings):
    """The message shown when a commit is blocked. Names the override explicitly.

    Says what to do, not just what is wrong: the useful response is almost always
    to cut something, and the person needs to know that --no-verify exists without
    being invited to reach for it first.
    """
    lines = ["", "BLOCKED: a CLAUDE.md in this commit is over its size cap.", ""]
    for f in findings:
        lines.append("  %s" % f["path"])
        lines.append("      %d lines, cap %d — %d over"
                     % (f["lines"], f["cap"], f["over"]))
    lines += [
        "",
        "A CLAUDE.md is injected into every session that loads it, so a line added",
        "here is paid forever. The cap exists to make that cost visible now, while",
        "you can still trade against it.",
        "",
        "Cut something of equal size, or — if this file genuinely needs to be",
        "bigger — raise its cap in ~/.config/claude-code-toolkit/config.json under",
        "context_lint.overrides, which records the decision where it can be seen.",
        "",
        "  git commit --no-verify    bypasses this check",
        "",
    ]
    return "\n".join(lines)


def _git(args):
    """Run a git command, returning stdout or None. Never raises."""
    try:
        done = subprocess.run(["git"] + args, capture_output=True, text=True, encoding="utf-8",
                              stdin=subprocess.DEVNULL, timeout=10)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return done.stdout


def repo_dirs(start, _cache={}):
    """(repo-name, this-checkout's-toplevel) for whatever contains `start`.

    TWO git questions, because they answer different halves and swapping them
    breaks a different case each way:

      - The NAME comes from `--git-common-dir`, so every linked worktree keys as
        the repo rather than as its own directory. A worktree's `--show-toplevel`
        basename is a branch nickname (`context-lint`), and keying on it would
        make the same commit pass or fail depending on where it was made.
      - The RELATIVE PATH comes from `--show-toplevel`, because a worktree's
        files are NOT under the primary checkout. Measuring against the common
        dir emits `../wt-nickname/CLAUDE.md` and matches nothing — which is what
        the first version of this fix did, caught by its own worktree test.
    """
    start = os.path.realpath(start)
    if start not in _cache:
        common = _git(["-C", start, "rev-parse", "--git-common-dir"])
        top = _git(["-C", start, "rev-parse", "--show-toplevel"])
        if not (common and common.strip() and top and top.strip()):
            _cache[start] = (None, None)
        else:
            common = common.strip()
            if not os.path.isabs(common):
                common = os.path.join(start, common)
            name = os.path.basename(os.path.dirname(os.path.realpath(common)))
            _cache[start] = (name, os.path.realpath(top.strip()))
    return _cache[start]


def repo_owner(start, _cache={}):
    """The remote's owner, LOWERCASED, for whatever contains `start`, or None.

    `example-org` for `github.com/example-org/tools`. Derived from `origin`'s URL and nothing else: the DIRECTORY NAME cannot tell
    two clones apart, which is the whole reason this exists.

    Returns None for a repo with no `origin` — a local-only or freshly-`git
    init`ed checkout is a repo we cannot name, not an error, and it falls back
    to the bare key like any repo we have no opinion about.

    CACHED PER REPO, not per directory. Keying the cache on the path handed in
    made this fork `git config` once per DIRECTORY containing a staged file —
    which multiplied subprocesses and slowed staging,
    for a value that cannot vary within one checkout. `repo_dirs` is already
    memoised on its own argument, so resolving the top costs nothing new.

    `--local`, deliberately: a bare `--get` falls through to ~/.gitconfig, so a
    machine with a global `remote.origin.url` would hand every remote-less repo
    an owner it does not have. A linked worktree reads the common config here,
    which is the shared value we want.

    LOWERCASED because forge URL segments are case-insensitive while git stores
    the case as typed, so `github.com/Example-Org/...` and the canonical
    spelling are the same repo. Getting this wrong is SILENT in the dangerous
    direction: a tighter override simply stops applying and
    an over-cap file then looks like a file under cap.

    Handles every spelling in use, because a machine using SSH for one clone
    and HTTPS for another would otherwise key the same repo two ways:
      git@github.com:owner/repo.git      ->  owner
      https://github.com/owner/repo      ->  owner
      ssh://git@github.com/owner/repo    ->  owner
    """
    name, top = repo_dirs(os.path.realpath(start))
    if not top:
        return None
    if top not in _cache:
        url = _git(["-C", top, "config", "--local", "--get", "remote.origin.url"])
        owner = None
        if url and url.strip():
            u = url.strip()
            if u.endswith(".git"):
                u = u[:-4]
            # Strip scheme/host: everything up to the last ':' or the host's
            # first '/'. What remains is `<owner>/<repo>`, possibly with extra
            # nesting on self-hosted forges — the owner is the second-to-last.
            if "://" in u:
                u = u.split("://", 1)[1]
                u = u.split("/", 1)[1] if "/" in u else ""
            elif ":" in u:
                u = u.split(":", 1)[1]
            parts = [p for p in u.split("/") if p]
            if len(parts) >= 2:
                owner = parts[-2].lower()
        _cache[top] = owner
    return _cache[top]


class _QualifiedKey(str):
    """The BARE `<repo>/<path>` key, carrying its owner-qualified sibling.

    A `str` subclass whose VALUE is the bare key, deliberately: every existing
    consumer — the findings payload, the block messages, dict lookups, equality
    against a literal in a test — keeps behaving exactly as it did. `cap_for` is
    the only thing that reads `.candidates`.

    The alternative shapes were both worse. Returning a plain list broke the
    `for key, source in entries` unpack and the `"key": key` display in two
    formatters. Returning `<owner>/<repo>/<path>` as a plain string and letting
    `cap_for` strip the first segment is AMBIGUOUS: a path inside a repo can
    itself contain slashes, so `tools/docs/CLAUDE.md` and a qualified
    2-segment key are indistinguishable by counting.
    """
    candidates = ()


def cap_key(abs_path):
    """`<repo-name>/<path-within-repo>`, or None when not inside a repo.

    ONE function, used by both `--staged` and `--report`. They previously each
    passed their own path shape into a suffix match, the two shapes did not
    agree, and the report said `ok` on a file the hook refused.

    This stays the BARE form. It is what findings and messages display, and
    what a hand-written config override is spelled as. It attaches the
    owner-qualified candidate in FRONT of it on `.candidates`, which is what
    `cap_for` matches against; the two spellings are a precedence order.
    """
    # realpath BOTH sides: git returns resolved paths, and on macOS /var is a
    # symlink to /private/var — resolving only one side makes relpath emit
    # `../../..` and the key match nothing, silently, in any temp directory.
    abs_path = os.path.realpath(abs_path)
    name, top = repo_dirs(os.path.dirname(abs_path))
    if not name or not top:
        return None
    rel = os.path.relpath(abs_path, top)
    if rel.startswith(".."):
        return None
    bare = os.path.join(name, rel).replace(os.sep, "/")
    key = _QualifiedKey(bare)
    owner = repo_owner(os.path.dirname(abs_path))
    if owner:
        key.candidates = ("%s/%s" % (owner, bare), bare)
    return key


def format_core_block(findings):
    """The message shown when a Core is over its size cap.

    Mirrors format_block's shape and wording — including "%d over", which the
    executable's own test asserts. Names the override key so the decision gets
    recorded somewhere visible rather than argued in a commit message.
    """
    lines = ["", "BLOCKED: a Core block in this commit is over its size cap.", ""]
    for f in findings:
        lines.append("  %s" % f["path"])
        lines.append("      Core is %d lines, cap %d — %d over"
                     % (f["core_lines"], f["cap"], f["over"]))
    lines += [
        "",
        "A Core block is the part of a context file GUARANTEED to be read, so it",
        "is paid in full in every session that loads the file. That is why its",
        "cap is tighter than the whole file's and separate from it.",
        "",
        "Move something into the body, or — if this Core genuinely needs to be",
        "bigger — raise its cap in ~/.config/claude-code-toolkit/config.json",
        "under context_lint.core_overrides, which records the decision where it",
        "can be seen.",
        "",
        "  git commit --no-verify    bypasses this check",
        "",
    ]
    return "\n".join(lines)


def format_sufficiency_block(findings):
    """Advisory notice for a Core that defers to the body. Never blocks.

    Includes the MATCHED STRINGS. A finding that does not say what it found
    cannot be acted on, and the reader has no other way to locate it.
    """
    lines = ["", "ADVISORY: a Core block defers to the body. Not blocking.", ""]
    for f in findings:
        lines.append("  %s" % f["path"])
        for ref in f["refs"]:
            lines.append("      %r" % ref)
    lines += [
        "",
        "A Core is read in full and the body is skimmed, so a rule that points",
        "at the body is a rule that does not fire. Restate what the reader needs",
        "inside the Core, or point at a named file rather than at a direction.",
        "",
        "This is a heuristic on prose and it does not block the commit.",
        "",
    ]
    return "\n".join(lines)
