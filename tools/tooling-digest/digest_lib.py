"""Extract the injectable digest block from a personal TOOLING.md.

The map itself is personal — it describes one machine and one set of accounts —
so it lives outside this repo, at <workspace_root>/TOOLING.md by default. This
module only knows how to find it and pull one delimited block out of it.

Why a delimited block inside the document rather than a separate summary file:
the digest and the detail cannot drift if they are the same file. A second file
would be one more thing to keep in sync, and keeping things in sync by hand is
the exact failure this feature exists to fix.

Everything here fails toward printing NOTHING. There are three exceptions, and
all are the same judgement: an ENABLED digest that resolves no file at all, or
one it cannot read; a file that exists but carries no usable block; and a block
that is over its line limit. Each is a broken or unfinished setup rather than a
legitimate absence, and staying silent about it would reproduce the failure
mode the whole feature is meant to remove. An enabled digest with no file once
injected nothing for sixteen days and said nothing about it; the only quiet
opt-out is `enabled: false`, not a missing file.

The over-cap case prints the overage INSTEAD of the block, never beside it —
see `cap_block`. A partial map is the one output that looks exactly like a
working one.
"""

import os
import shlex
import sys
import time

# realpath, not abspath: consumers reach this module through symlinked
# executables in ~/.local/bin, and abspath would resolve against /Users and fail
# at runtime while passing every test. This module resolves lib/ ITSELF rather
# than trusting a caller to have done it: as of the Core-block split it has
# three importers, and "some caller inserted the path first" is not a contract.
_LIB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import core_block  # noqa: E402

# Markers now live in lib/core_block.py, shared with context_lint_lib. They are
# re-exported here because existing callers and tests import them from this
# module, and narrowing a shared name silently breaks every one of them.
START_MARKER = core_block.DIGEST_START
END_MARKER = core_block.DIGEST_END

DEFAULT_FILENAME = "TOOLING.md"
DEFAULT_MAX_LINES = 60
DEFAULT_STALE_AFTER_DAYS = 90

# Past ~10,000 characters the harness saves hook output to a file and shows the
# model a 2KB preview (an 11,161-char digest arrived as its
# first 2KB; a 9,871-char PostToolUse injection arrived whole). The line cap
# never saw it: the live block was 147 lines against a 200-line cap. A constant,
# not config, because no setting moves the harness's limit.
HARNESS_MAX_CHARS = 9500


def _config():
    """Return the toolkit config module, or None if it cannot be imported.

    Imported lazily and defensively: a hook that crashes because a config
    helper moved is worse than a hook that silently does nothing.
    """
    try:
        import toolkit_config

        return toolkit_config
    except Exception:
        return None


def is_enabled(config=None):
    """True unless explicitly disabled. Absent config means on."""
    config = config or _config()
    if config is None:
        return True
    try:
        value = config.get("tooling_digest", "enabled", default=True)
    except Exception:
        return True
    return value is not False


def resolve_path(config=None):
    """Where the personal tooling map lives, or None if it cannot be located.

    Order: an explicit `tooling_digest.path`, then <workspace_root>/TOOLING.md.
    Bare `workspace_root` is what makes the default portable — on any machine
    where a workspace root was set during onboarding, this feature works without
    naming a path, and no machine inherits a path that is personal to another.

    Returns None when neither is available. `build` says so while the digest is
    enabled (see `render_missing`); `enabled: false` is the quiet opt-out.
    """
    config = config or _config()
    if config is None:
        return None

    try:
        explicit = config.get_path("tooling_digest", "path")
    except Exception:
        explicit = None
    if explicit:
        return explicit

    try:
        root = config.workspace_root()
    except Exception:
        root = None
    if not root:
        return None
    return os.path.join(root, DEFAULT_FILENAME)


def max_lines(config=None):
    """Cap on injected lines. Absent or unusable config means the default."""
    config = config or _config()
    if config is None:
        return DEFAULT_MAX_LINES
    try:
        value = config.get_int(
            "tooling_digest", "max_lines", default=DEFAULT_MAX_LINES
        )
    except Exception:
        return DEFAULT_MAX_LINES
    return value if value > 0 else DEFAULT_MAX_LINES


def stale_after_days(config=None):
    """Days of no edits before the map is called out as possibly stale.

    0 disables the check.
    """
    config = config or _config()
    if config is None:
        return DEFAULT_STALE_AFTER_DAYS
    try:
        value = config.get_int(
            "tooling_digest", "stale_after_days", default=DEFAULT_STALE_AFTER_DAYS
        )
    except Exception:
        return DEFAULT_STALE_AFTER_DAYS
    return value if value >= 0 else DEFAULT_STALE_AFTER_DAYS


def read_source(path):
    """File contents, or None if it cannot be read for any reason."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        return None


def extract(text):
    """Delegates to core_block. See there for position-based precedence."""
    return core_block.extract(text)


def cap_block(body, limit, label="DIGEST", filename=None):
    """Under the cap, the block. Over it, the overage INSTEAD of the block.

    Ruled 2026-09-14: fail loud, not lossy. Until then this kept
    `lines[:limit]` and appended a notice, and that shape is what let a live
    map bleed its tail for days — a trimmed block reads exactly like a
    complete one, and the notice sat at the bottom of a long block that
    looked fine. Making the failure survivable is what removed the alarm.

    So the whole block is withheld. The accepted cost is one session with no
    tooling map, which is loud in a way a trimmed tail never is: nothing can be
    looked up, so nothing gets looked up WRONG.

    Deliberately does NOT name `tooling_digest.max_lines`. Raising the limit is
    the treadmill this design exists to stop, and a notice that advertises the
    escape hatch is a notice that gets escaped rather than actioned. The only
    instruction offered is to trim.

    `label` names the markers the file actually carries, and `filename` the
    file it carries them in, so both halves of the instruction point at
    something the reader can find. `label` defaults to DIGEST because that is
    what the template still ships; `filename` to `DEFAULT_FILENAME` because that
    is where the default path resolves. `build` overrides both from the file it
    actually read — `tooling_digest.path` may name any file, and this notice is
    the only thing in the session when it fires, so a hardcoded name would send
    the one reader who needs it looking for a file that is not there.
    """
    lines = body.splitlines()
    if len(lines) <= limit:
        return body
    return (
        "%s OVER LIMIT — %d/%d lines. The block was NOT injected, so this "
        "session has NO tooling map.\n"
        "Look nothing up from memory — open %s and read it. To restore the "
        "digest, trim the %s block to %d lines or fewer."
        % (label, len(lines), limit, filename or DEFAULT_FILENAME, label, limit)
    )


def cap_chars(output, label="DIGEST", path=None):
    """Under the harness limit, the output. Over it, the overage INSTEAD.

    Same ruling as `cap_block`: a 2KB preview reads like a complete map, so
    the whole output is withheld and the notice is the only thing injected.
    Measured on everything the hook prints, not the block alone, because
    the harness measures everything the hook prints.

    Names the FULL path, unlike `cap_block`: that notice still gets render's
    `(Source: <path>)` line beside it, and this one replaces the output that
    carried it. The session's cwd is any repo, so a bare filename would not
    resolve.
    """
    if len(output) <= HARNESS_MAX_CHARS:
        return output
    return (
        "%s OVER CHARACTER LIMIT — %d/%d characters. The hook's output was NOT injected, "
        "so this session has NO tooling map.\n"
        "Look nothing up from memory — open %s and read it. To restore the "
        "digest, trim the %s block until the output is %d characters or "
        "fewer; past that the harness shows only a 2KB preview.\n"
        % (label, len(output), HARNESS_MAX_CHARS,
           path or DEFAULT_FILENAME, label, HARNESS_MAX_CHARS)
    )


def days_since_modified(path, now=None):
    """Whole days since the file was last written, or None if unknowable."""
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        return None
    now = time.time() if now is None else now
    delta = now - mtime
    if delta < 0:
        return 0
    return int(delta // 86400)


def is_stale(path, threshold_days, now=None):
    """True only when we positively know the file is older than the threshold.

    An unknowable mtime is not staleness. Failing toward 'fresh' keeps the hook
    from nagging about a file it cannot actually assess.
    """
    if not threshold_days:
        return False
    age = days_since_modified(path, now=now)
    if age is None:
        return False
    return age >= threshold_days


def render(body, path, stale_days=None):
    """The text injected into the session, or '' when there is nothing to say.

    The staleness notice names the file from `path` rather than a hardcoded
    `TOOLING.md`, for the same reason `cap_block` takes a `filename`:
    `tooling_digest.path` may name any file. Both notices can appear in ONE
    session (a stale map that is also over cap), so a hardcoded name here
    would have contradicted the one beside it.
    """
    if not body:
        return ""
    parts = [body]
    if stale_days is not None:
        parts.append(
            "\n[%s has not been edited in %d days — treat its statuses as "
            "unverified and re-check before relying on one.]"
            % (os.path.basename(str(path)) or DEFAULT_FILENAME, stale_days)
        )
    parts.append(
        "\n(Source: %s — read it for detail, and fix any row you find wrong in "
        "the same turn.)" % path
    )
    return "\n".join(parts) + "\n"


def render_broken(path, text=None):
    """Notice for a map that exists but yields no usable block.

    Deliberately not silent. A missing block means the digest silently stopped
    being injected, which looks identical to everything working — and a memory
    system that fails invisibly is the problem this feature was built to solve.

    Names BOTH marker pairs, because either is valid and the reader's file may
    carry the one this message does not mention. Distinguishes "no block" from
    "empty block": telling someone to add markers to a file that already has
    them sends them looking for a defect that is not there.
    """
    if text is not None and core_block.block_count(text):
        return ("[%s carries a marker block but it is empty, so no tooling "
                "digest was injected. Put the summary back between the "
                "markers.]\n" % path)
    return (
        "[%s exists but has no %s / %s block (either pair is accepted), so no "
        "tooling digest was injected. Add one pair back around the summary "
        "block.]\n" % (path, core_block.CORE_START, core_block.DIGEST_START)
    )


def _disable_command():
    """The exact, pasteable command that turns tooling-digest off.

    Every printed instruction must be runnable as written: a bare
    `setup_config.py --update --disable tooling_digest` only runs from inside
    `lib/` with the config path memorised.

    `<repo>` is derived from this file's own location rather than any config
    lookup, since `render_missing` fires exactly when config resolution has
    already come up empty — `digest_lib.py` sits at `<repo>/tools/tooling-digest/`.
    `<config>` mirrors `toolkit_config`'s own default resolution
    (`$CLAUDE_TOOLKIT_CONFIG`, else `~/.config/claude-code-toolkit/config.json`).

    Both paths are `shlex.quote`d: a macOS username with a space in it puts a
    space in `$HOME`, and an unquoted path splits into two argv tokens the
    moment someone pastes this and runs it.

    `--update` refuses when no config exists yet ("run --write first", exit 3),
    and `--write` needs a name and a workspace root, so on a machine with no
    config the command first copies `config.example.json` into place, exactly
    as the installer's no-terminal fallback does, then disables. Without that,
    the one machine most likely to see this notice (nothing set up) was handed
    an opt-out that fails.
    """
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    config_path = os.path.expanduser(
        os.environ.get("CLAUDE_TOOLKIT_CONFIG",
                       "~/.config/claude-code-toolkit/config.json")
    )
    update = ("python3 %s/lib/setup_config.py %s %s --update --disable "
              "tooling_digest" % (shlex.quote(repo), shlex.quote(config_path),
                                  shlex.quote(repo)))
    if os.path.exists(config_path):
        return update
    return "mkdir -p %s && cp %s %s && %s" % (
        shlex.quote(os.path.dirname(config_path) or "."),
        shlex.quote(os.path.join(repo, "config.example.json")),
        shlex.quote(config_path), update)


def render_missing(path):
    """Notice for an enabled digest that has nothing to inject.

    Same shape as `render_broken`, and deliberately not silent: an enabled
    digest with no file injected nothing for sixteen days and said nothing.
    The opt-out is `enabled: false`, not a missing file, so the notice names
    the exact command for it.
    """
    disable_command = _disable_command()
    if path:
        return (
            "[tooling-digest is on, but %s does not exist or cannot be read, "
            "so no tooling digest was injected. Create it from "
            "TOOLING.md.template in the toolkit (or re-run /onboard), point "
            "tooling_digest.path at the real file, or turn this off with "
            "%s.]\n" % (path, disable_command)
        )
    return (
        "[tooling-digest is on, but no file is configured — set "
        "workspace_root or tooling_digest.path — so no tooling digest was "
        "injected. Create it from TOOLING.md.template in the toolkit (or "
        "re-run /onboard), point tooling_digest.path at the real file, or "
        "turn this off with %s.]\n" % disable_command
    )


def missing_source(config=None):
    """The resolved path (or "<unconfigured>") when `build` would print a
    missing-file notice instead of a digest. For an installer's config-health
    check, which needs the condition without the session text.

    None means: disabled, the file exists and can be read, or the config
    module itself could not be imported (kept quiet, the same as `build`).
    """
    config = config or _config()
    if config is None:
        return None
    if not is_enabled(config):
        return None
    path = resolve_path(config)
    if not path:
        return "<unconfigured>"
    if read_source(path) is not None:
        return None
    return path


def build(config=None, now=None):
    """Full pipeline: config to injectable text. '' means print nothing."""
    config = config or _config()
    if config is None:
        # A broken import is not this machine opting in to anything: stay as
        # quiet as the rest of this module does on the same case.
        return ""
    if not is_enabled(config):
        return ""

    path = resolve_path(config)
    if not path:
        return render_missing(None)

    text = read_source(path)
    if text is None:
        # Enabled with no readable file is an unfinished setup, not an opt-out
        # (`enabled: false` is the opt-out). Say so rather than inject nothing.
        return render_missing(path)

    return cap_chars(assemble(text, path, config, now=now),
                     label=core_block.marker_label(text) or "DIGEST", path=path)


def assemble(text, path, config=None, now=None):
    """Everything the hook would print for this map, BEFORE `cap_chars`.

    The one measurement of the harness-limited quantity: context-budget and
    context-growth call this too, so a report or an edit-time notice can never
    pass a map the hook then withholds.
    """
    body = extract(text)
    if not body:
        return render_broken(path, text)

    label = core_block.marker_label(text) or "DIGEST"
    filename = os.path.basename(path) or DEFAULT_FILENAME
    body = cap_block(body, max_lines(config), label=label, filename=filename)
    if core_block.block_count(text) > 1:
        # The earliest pair wins deterministically, but the file
        # is still ambiguous about what it injects, and a second pair is almost
        # always an unfinished migration or a marker pasted into the body.
        body += ("\n[This file carries %d marker blocks; the earliest one is "
                 "injected. Delete the others.]" % core_block.block_count(text))
    threshold = stale_after_days(config)
    stale_days = None
    if is_stale(path, threshold, now=now):
        stale_days = days_since_modified(path, now=now)
    return render(body, path, stale_days=stale_days)
