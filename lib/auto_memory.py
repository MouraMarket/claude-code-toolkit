#!/usr/bin/env python3
"""The one resolver behind the harness's `autoMemoryDirectory` setting.

`install.sh`, `memory-lint` and `context-budget` each need to answer the same
two questions -- "what would the harness resolve this value to?" and "what
should `install.sh` write, given what's there today?" -- so both answers live
here once rather than three times.

Every function is total and never raises: a config typo or a missing file
degrades to a documented fallback, because these run from inside hooks and an
install step, and neither may go down over a malformed setting.

Standard library only: `json`, `os`, `re`, `sys`.
"""

import json
import os
import re
import sys

# Sentinel for "the autoMemoryDirectory key is absent from settings.json".
# A present JSON `null` is NOT this -- it's a foreign value, same as any other
# non-matching present value. Callers must read the raw setting with
# `settings.get("autoMemoryDirectory", auto_memory.ABSENT)`.
ABSENT = object()


def project_slug(path):
    """The harness's project-key slug: realpath with every non-alphanumeric
    character replaced by "-". Matches `budget_lib.project_slug`'s documented
    contract (verified against Claude Code 2.1.280's own slugging).

    Does NOT replicate the harness's over-200-char hashing -- callers that
    care (`managed_target`) check the length themselves.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(path))


def managed_target(workspace_root, home):
    """The `autoMemoryDirectory` value install.sh manages, or why it can't yet.

    Returns `("~/.claude/projects/<slug>/memory", None)` on success, or
    `(None, reason)` with one of four exact reason strings -- see the four
    early returns below. `install.sh` prints the reason verbatim. A
    `workspace_root` that is truthy but not a `str` (a list, dict, int, or
    bool from a malformed config) shares the "workspace_root is not a
    directory" reason -- it plainly isn't one.

    A leading `~` in `workspace_root` is expanded against `home` by hand
    (never `os.path.expanduser`), so a test can pass a fixture home and never
    touch the real `~`.
    """
    if not workspace_root:
        return None, "no workspace_root configured"

    if not isinstance(workspace_root, str):
        # A malformed config can hand this a list, dict, int or bool -- all
        # truthy, none of them a path. Without this check `.startswith` below
        # raises AttributeError and a real `install.sh` apply exits 1.
        return None, "workspace_root is not a directory"

    # `toolkit_config` paths support `$VARS` via `os.path.expandvars` -- this
    # is the one caller that did not, so `workspace_root: "$SOME_VAR"` silently
    # failed the isdir check below instead of resolving. Expand before the `~`
    # handling so a value like `$HOME/repo` or `~/$SUBDIR` still works either
    # order a user happens to write it.
    workspace_root = os.path.expandvars(workspace_root)

    if workspace_root == "~":
        expanded_root = home
    elif workspace_root.startswith("~/"):
        expanded_root = os.path.join(home, workspace_root[2:])
    else:
        expanded_root = workspace_root

    if not os.path.isdir(expanded_root):
        return None, "workspace_root is not a directory"

    slug = project_slug(expanded_root)
    if len(slug) > 200:
        return None, "project key over 200 chars is hashed by the harness"

    projects_dir = os.path.join(home, ".claude", "projects", slug)
    if not os.path.isdir(projects_dir):
        return None, (
            "no session has run in workspace_root yet "
            "(~/.claude/projects/%s missing)" % slug)

    return "~/.claude/projects/%s/memory" % slug, None


def expand(value, home):
    """The harness's own `cu(value, true)` expansion rule (verified 2.1.280),
    in the harness's order:

    1. Non-empty string only.
    2. A leading `~/` is expanded against `home`; the rest, normalised, is
       rejected if it's empty, `..`, or escapes upward (a check on path
       *components*, so a name merely starting with dots -- `"..cache"` --
       is fine).
    3. Otherwise normalise, then reject a non-absolute result, anything under
       3 characters, or an embedded NUL.

    The harness also runs a check it calls `Hd()` that could not be decoded
    from the binary. That gap is INFERRED, not verified: this function may be
    stricter or looser than the harness on some input `Hd()` alone decides.
    """
    if not isinstance(value, str) or not value:
        return None

    if value.startswith("~/"):
        rest = value[2:]
        h = os.path.normpath(rest or ".")
        if h == "." or h == ".." or h.startswith("../"):
            return None
        value = os.path.join(home, rest)

    s = os.path.normpath(value).rstrip("/")
    if not os.path.isabs(s) or len(s) < 3 or "\x00" in s:
        return None
    return s


def user_setting(home):
    """The raw, non-empty-string `autoMemoryDirectory` from
    `<home>/.claude/settings.json`. `None` for a missing file, invalid JSON, a
    non-object document, an absent key, or a non-string value.
    """
    path = os.path.join(home, ".claude", "settings.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("autoMemoryDirectory")
    return value if isinstance(value, str) and value else None


def configured_dir(home):
    """The user-level `autoMemoryDirectory`, expanded but NOT dir-checked.

    `None` only when the setting is absent, or present but invalid (rejected
    by `expand()` -- a `..` escape, too short, a NUL, or simply not a
    string). A value that is well-formed but points at a directory that does
    not exist YET is still returned: the harness creates a memory folder's
    contents on first write, so a missing folder is not itself evidence the
    setting is wrong -- only a malformed value is. Distinguishing "configured
    but missing" from "not configured" is the whole point of this function;
    conflating them made a caller fall back and silently report a DIFFERENT
    project's memory as healthy.
    """
    return expand(user_setting(home), home)


def resolve_user_dir(home):
    """The user-level `autoMemoryDirectory`, expanded and dir-checked.

    `None` unless the setting is present, expands to something usable, and
    that path actually exists as a directory today.
    """
    expanded = expand(user_setting(home), home)
    if expanded and os.path.isdir(expanded):
        return expanded
    return None


def plan(current, unify, target):
    """What `install.sh` should do to `autoMemoryDirectory`, and the value to
    act on. Pure -- no I/O, never raises.

    `current` is the raw settings value, or `ABSENT` (the module sentinel)
    when the key is missing -- callers must read it with
    `settings.get("autoMemoryDirectory", auto_memory.ABSENT)` so a present
    JSON `null` is never confused with an absent key. `target` is the managed
    value from `managed_target()`, or `None` when it's unavailable.

    Returns `(action, detail)`:
    - `("set", target)` / `("remove", target)` -- install.sh should write.
    - `("ok", detail)` -- already correct; `detail` is `target` when managed,
      `None` when auto memory isn't managed at all.
    - `("skip", None)` -- unify is on but there's no target yet.
    - `("foreign", current)` -- a present value that isn't the managed target;
      never touched, in either mode.

    Comparison against `target` is exact string equality -- `"~/x"` and its
    expanded absolute form count as different on purpose, since only the
    `~/` spelling this module writes is ever recognised as "ours".
    """
    if unify:
        if target is not None:
            if current is ABSENT:
                return "set", target
            if current == target:
                return "ok", target
            return "foreign", current
        else:
            if current is ABSENT:
                return "skip", None
            return "foreign", current
    else:
        if target is not None and current == target:
            return "remove", target
        if current is ABSENT:
            return "ok", None
        return "foreign", current


def unify_enabled(config_module):
    """Whether `auto_memory.unify` is on: absent or anything other than the
    literal boolean `False` means on -- this deliberately inverts the
    toolkit's usual "absent = off" convention, deliberately.

    `True` if `config_module` is `None` or raises -- a config problem must
    never silently turn OFF a setting meant to be on by default.
    """
    if config_module is None:
        return True
    try:
        return config_module.get("auto_memory", "unify", default=True) is not False
    except Exception:
        return True


# The `action` values `status()` can return: `plan()`'s five, plus "error" for
# a settings file install.sh would refuse to touch.
STATUS_ACTIONS = ("ok", "set", "remove", "skip", "foreign", "error")


def status(home, config_module):
    """What `install.sh` would do to `autoMemoryDirectory` right now, without
    doing it. Read-only and total -- never raises.

    Returns `(action, detail, reason, workspace_root)`:
    - `action` / `detail` are `plan()`'s pair for `ok`, `set`, `remove` and
      `foreign`; `reason` is `None`.
    - `skip`: `detail` is `None`, `reason` is `managed_target()`'s string.
    - `error`: `<home>/.claude/settings.json` exists but is unreadable, not
      valid JSON, or not a JSON object -- install.sh refuses that file, so
      this reports it rather than treating it as empty. `detail` is `None`.
    - `workspace_root` is the configured string as written (unexpanded, since
      a human reads it), or `None` when absent or not a non-empty `str`.

    A missing settings file is `{}`, as in install.sh. A config module that
    raises reads as "no workspace_root" and unify on (`unify_enabled`).
    """
    workspace_root = None
    try:
        try:
            raw_root = config_module.get("workspace_root")
        except Exception:
            raw_root = None
        if isinstance(raw_root, str) and raw_root:
            workspace_root = raw_root

        path = os.path.join(home, ".claude", "settings.json")
        try:
            with open(path, encoding="utf-8") as handle:
                settings = json.load(handle)
        except FileNotFoundError:
            settings = {}
        except (OSError, ValueError) as exc:
            return ("error", None,
                    "settings.json is not readable JSON: %s" % exc,
                    workspace_root)
        if not isinstance(settings, dict):
            return ("error", None,
                    "settings.json is not a JSON object", workspace_root)

        target, reason = managed_target(raw_root, home)
        action, detail = plan(
            settings.get("autoMemoryDirectory", ABSENT),
            unify_enabled(config_module), target)
        if action == "skip":
            return action, None, reason, workspace_root
        return action, detail, None, workspace_root
    except Exception as exc:
        return ("error", None, "unexpected failure: %s" % exc, workspace_root)


def main(argv):
    """`--status`: print `status()` as one JSON line. Anything else is usage."""
    if argv != ["--status"]:
        sys.stderr.write("usage: auto_memory.py --status\n")
        return 2
    import toolkit_config  # lib/ is on sys.path[0] when run by path
    action, detail, reason, workspace_root = status(
        os.path.expanduser("~"), toolkit_config)
    print(json.dumps({"action": action, "detail": detail, "reason": reason,
                      "workspace_root": workspace_root}))
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
