#!/usr/bin/env python3
"""Toolkit policy loads through one import line in ~/.claude/CLAUDE.md.

`status` reads that file and reports one of STATES; `ensure` adds (or repairs)
the two-line owned block; `remove` takes that import (and its marker) back out.
Every function is total: no exception escapes `status`, `ensure` or `remove`.

The module and its config key keep the name `team_policy` for compatibility;
what it manages is the toolkit's `policy/CLAUDE.md`.

CLI (run by path, so lib/ is sys.path[0]):
  team_policy.py --status <checkout>   one JSON line {state, path, detail}
  team_policy.py ensure <checkout>     one JSON line {result, path, detail}; exit 1 on refused
  team_policy.py remove <checkout>     one JSON line {state, path, detail}; exit 1 on refused
"""

import json
import os
import sys
import tempfile

import claude_imports

MARKER = ("<!-- claude-code-toolkit policy (managed by /onboard; to opt out, delete "
          "these two lines and set team_policy.import to false in your toolkit config) -->")
# Heading TEXT, matched at any level: an older copied template put "Gate at
# decisions" at level 1, and the policy has it at level 2.
SENTINEL_HEADINGS = (
    "Gate at decisions, not execution",
    "Usage budget — stop at a boundary, not mid-task",
    "Working conventions",
)
STATES = ("ok", "missing", "dangling", "legacy-copy", "opted-out", "error")

_OURS_SUFFIX = os.sep + os.path.join("policy", "CLAUDE.md")


def canonical_checkout(checkout):
    """The primary checkout for a linked worktree; otherwise the checkout's realpath.

    A linked worktree's `.git` is a file ("gitdir: <G>") and `<G>/commondir` points
    at the primary `.git`. A worktree is deleted after its merge, so an import into
    it would silently lose toolkit policy. Never raises, runs no subprocess.
    """
    plain = os.path.realpath(checkout)
    try:
        git_file = os.path.join(checkout, ".git")
        if not os.path.isfile(git_file):
            return plain
        with open(git_file, "r", encoding="utf-8") as handle:
            first = handle.readline().strip()
        if not first.startswith("gitdir:"):
            return plain
        gitdir = first[len("gitdir:"):].strip()
        if not os.path.isabs(gitdir):
            gitdir = os.path.join(checkout, gitdir)
        with open(os.path.join(gitdir, "commondir"), "r", encoding="utf-8") as handle:
            common = handle.readline().strip()
        common = os.path.realpath(os.path.join(gitdir, common))
        if os.path.basename(common) == ".git":
            return os.path.dirname(common)
    except Exception:
        pass
    return plain


def policy_path(checkout):
    return os.path.join(canonical_checkout(checkout), "policy", "CLAUDE.md")


def import_line(checkout):
    return "@" + policy_path(checkout).replace(" ", "\\ ")


def user_file(home):
    return os.path.join(home, ".claude", "CLAUDE.md")


def import_enabled(config_module):
    """False only for a literal false; absent or unreadable config means checked."""
    try:
        return config_module.get("team_policy", "import", default=True) is not False
    except Exception:
        return True


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def _ours_imports(text, file_path, home):
    """Resolved paths of every import that points at a `policy/CLAUDE.md`."""
    resolved = (claude_imports.resolve(token, file_path, home)
                for _n, token in claude_imports.iter_imports(text))
    return [path for path in resolved if path.endswith(_OURS_SUFFIX)]


def _readable_file(path):
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb"):
            return True
    except OSError:
        return False


def _has_good_import(ours, checkout):
    want = os.path.realpath(policy_path(checkout))
    return any(os.path.realpath(path) == want and _readable_file(path) for path in ours)


def _is_managed_import(line, file_path, home):
    """True for a line that is only an import of some checkout's `policy/CLAUDE.md`.

    That is the line `ensure` wrote under its marker (a checkout that has since
    moved leaves a stale one to repair). Any other import there is the user's.
    """
    found = claude_imports.iter_imports(line)
    if len(found) != 1:
        return False
    token = found[0][1]
    return (claude_imports.is_import_only(line, token)
            and claude_imports.resolve(token, file_path, home).endswith(_OURS_SUFFIX))


def _marker_lines(text):
    """Line numbers (1-based) of MARKER lines outside any fence."""
    return [n for n, line in claude_imports.lines_outside_fences(text)
            if line.rstrip("\r") == MARKER]


def _heading_text(line):
    """The text of a Markdown `#`..`######` heading line, else None."""
    stripped = line.strip()
    level = len(stripped) - len(stripped.lstrip("#"))
    if not 1 <= level <= 6 or stripped[level:level + 1] != " ":
        return None
    return stripped[level:].strip()


def _headings(lines):
    return {text for text in (_heading_text(line) for line in lines) if text}


def _opted_out_detail(checkout, home):
    plain = "team_policy.import is false"
    try:
        text = _read(user_file(home))
        if _marker_lines(text) or _ours_imports(text, user_file(home), home):
            return ("team_policy.import is false, but ~/.claude/CLAUDE.md still imports "
                    "the toolkit policy; delete the two marked lines to stop loading it")
    except Exception:
        pass
    return plain


def _legacy_copy(checkout, config_module):
    """(path, headings) when the workspace CLAUDE.md still carries policy sections."""
    root = config_module.get("workspace_root")
    if not isinstance(root, str) or not root:
        return None
    import toolkit_config
    root = toolkit_config.expand_path(root)
    if not root:
        return None
    path = os.path.join(root, "CLAUDE.md")
    if not os.path.isfile(path):
        return None
    headings = _headings(line for _n, line in claude_imports.lines_outside_fences(_read(path)))
    found = [h for h in SENTINEL_HEADINGS if h in headings]
    if len(found) < 2:
        return None
    return (path, _copied_headings(checkout, headings) or found)


def _copied_headings(checkout, workspace_headings):
    """Every #/## heading of the live policy (minus its title) whose text the workspace file carries."""
    try:
        with open(policy_path(checkout), encoding="utf-8") as handle:
            policy_lines = [line.rstrip() for _n, line
                            in claude_imports.lines_outside_fences(handle.read())]
    except Exception:
        return []
    headings = [_heading_text(line) for line in policy_lines
                if line.startswith("# ") or line.startswith("## ")]
    return [h for h in headings[1:] if h in workspace_headings]


def status(checkout, home, config_module):
    path = user_file(home)
    try:
        if not import_enabled(config_module):
            return ("opted-out", path, _opted_out_detail(checkout, home))
        if not os.path.exists(path):
            if os.path.islink(path):
                return ("missing", path, "~/.claude/CLAUDE.md is a broken symlink")
            return ("missing", path, "no ~/.claude/CLAUDE.md")
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError) as exc:
            return ("error", path, "cannot read ~/.claude/CLAUDE.md: %s" % exc)
        want = policy_path(checkout)
        ours = _ours_imports(text, path, home)
        if not ours:
            return ("missing", path, "~/.claude/CLAUDE.md does not import %s" % want)
        if not _has_good_import(ours, checkout):
            first = ours[0]
            if not os.path.isfile(first):
                why = "does not exist"
            elif os.path.realpath(first) == os.path.realpath(want):
                why = "is not readable"
            else:
                why = "is not this checkout's policy (%s)" % want
            return ("dangling", path, "imports %s, which %s" % (first, why))
        try:
            legacy = _legacy_copy(checkout, config_module)
        except Exception:
            legacy = None
        if legacy:
            ws_path, found = legacy
            return ("legacy-copy", ws_path,
                    "carries %d toolkit-policy sections that policy/CLAUDE.md now owns: %s"
                    % (len(found), "; ".join(found)))
        return ("ok", path, "imports %s" % want)
    except Exception as exc:
        return ("error", path, "unexpected failure: %s" % exc)


def _atomic_write(path, text):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    mode = None
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".CLAUDE.md.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ensure(checkout, home, config_module):
    path = user_file(home)
    try:
        if not import_enabled(config_module):
            return ("opted-out", path, "team_policy.import is false; nothing written")
        if os.path.islink(path):
            return ("refused", path, "~/.claude/CLAUDE.md is a symlink; not touching it")
        text = ""
        if os.path.exists(path):
            try:
                text = _read(path)
            except (OSError, UnicodeDecodeError) as exc:
                return ("refused", path, "cannot read ~/.claude/CLAUDE.md: %s" % exc)
        if _has_good_import(_ours_imports(text, path, home), checkout):
            return ("unchanged", path, "~/.claude/CLAUDE.md already imports toolkit policy")
        opened = claude_imports.open_fence_line(text)
        if opened is not None:
            return ("refused", path,
                    "~/.claude/CLAUDE.md ends inside an unclosed code fence opened at "
                    "line %d; close it, then rerun" % opened)
        line = import_line(checkout)
        markers = _marker_lines(text)
        if markers:
            lines = text.split("\n")
            after = markers[0]  # 0-based index of the line following the marker
            if after < len(lines) - 1:  # a real line follows the marker
                if _is_managed_import(lines[after], path, home):
                    cr = "\r" if lines[after].endswith("\r") else ""
                    lines[after] = line + cr
                else:  # the user's own line: keep it, insert ours before it
                    lines.insert(after, line)
            elif after == len(lines) - 1:  # marker was last line, file ends in newline
                lines[after] = line
                lines.append("")
            else:  # marker was last line, no trailing newline
                lines.append(line)
                lines.append("")
            new_text = "\n".join(lines)
            _atomic_write(path, new_text)
            return ("wrote", path, "updated the toolkit-policy import in ~/.claude/CLAUDE.md")
        new_text = text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        if new_text:
            new_text += "\n"
        new_text += MARKER + "\n" + line + "\n"
        _atomic_write(path, new_text)
        return ("wrote", path, "added the toolkit-policy import to ~/.claude/CLAUDE.md")
    except Exception as exc:
        return ("refused", path, "unexpected failure: %s" % exc)


def remove(checkout, home):
    """Delete this checkout's policy import (and the MARKER line above it) from
    ~/.claude/CLAUDE.md. Returns (state, path, detail), state in removed/absent/refused."""
    path = user_file(home)
    try:
        if os.path.islink(path):
            return ("refused", path, "~/.claude/CLAUDE.md is a symlink; not touching it")
        if not os.path.exists(path):
            return ("absent", path, "no ~/.claude/CLAUDE.md")
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError) as exc:
            return ("refused", path, "cannot read ~/.claude/CLAUDE.md: %s" % exc)
        want = os.path.realpath(policy_path(checkout))
        lines = text.split("\n")
        drop = set()
        for number, token in claude_imports.iter_imports(text):
            if os.path.realpath(claude_imports.resolve(token, path, home)) != want:
                continue
            index = number - 1
            if not claude_imports.is_import_only(lines[index], token):
                continue  # a line carrying other text is the person's, not ours
            drop.add(index)
            if index > 0 and lines[index - 1].rstrip("\r") == MARKER:
                drop.add(index - 1)
        if not drop:
            return ("absent", path, "~/.claude/CLAUDE.md does not import this checkout's toolkit policy")
        # ensure() appends "\n" + MARKER + import at the end of the file; undo the
        # blank separator too, but only when that block is still the file's tail.
        first = min(drop)
        if (lines[first].rstrip("\r") == MARKER and first > 0 and not lines[first - 1].strip()
                and all(not lines[i].strip() for i in range(max(drop) + 1, len(lines)))):
            drop.add(first - 1)
        _atomic_write(path, "\n".join(l for i, l in enumerate(lines) if i not in drop))
        return ("removed", path, "removed the toolkit-policy import from ~/.claude/CLAUDE.md")
    except Exception as exc:
        return ("refused", path, "unexpected failure: %s" % exc)


def main(argv):
    usage = "usage: team_policy.py --status <checkout> | ensure <checkout> | remove <checkout>\n"
    if len(argv) != 2 or argv[0] not in ("--status", "ensure", "remove"):
        sys.stderr.write(usage)
        return 2
    import toolkit_config  # lib/ is on sys.path[0] when run by path
    home = os.path.expanduser("~")
    if argv[0] == "--status":
        state, path, detail = status(argv[1], home, toolkit_config)
        print(json.dumps({"state": state, "path": path, "detail": detail}))
        return 0
    if argv[0] == "remove":
        state, path, detail = remove(argv[1], home)
        print(json.dumps({"state": state, "path": path, "detail": detail}))
        return 1 if state == "refused" else 0
    result, path, detail = ensure(argv[1], home, toolkit_config)
    print(json.dumps({"result": result, "path": path, "detail": detail}))
    return 1 if result == "refused" else 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
