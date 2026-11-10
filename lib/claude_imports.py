"""Recognise Claude Code `@path` imports in a CLAUDE.md, the way `has_import` does.

Stdlib only. Every function is total and never raises.

Only an import on a line whose first non-space character is `@` counts, outside
``` / ~~~ fences. Claude Code also honours an `@path` mid-line; that is a known
limit here, kept on purpose so this module agrees with
`tools/context-budget/instruction_lib.has_import`.
"""

import os


def _fence_open(stripped):
    """(char, run_length) when `stripped` opens a fence (3+ of ` or ~), else None."""
    if stripped[:1] in ("`", "~"):
        char = stripped[0]
        run = len(stripped) - len(stripped.lstrip(char))
        if run >= 3:
            return char, run
    return None


def _fence_closes(stripped, fence):
    """True when `stripped` is only the fence character, repeated at least as long as the opener."""
    char, run = fence
    return len(stripped) >= run and stripped == char * len(stripped)


def _fence_events(text):
    """Yield (line_number, line, in_fence_before, opens_fence) for each line.

    The one fence rule for every scanner here: a stripped line starting with 3 or
    more of the same character (` or ~) opens a fence of that character and run
    length; only a line made of that character alone, at least as long, closes it.
    """
    fence = None
    for number, line in enumerate((text or "").split("\n"), 1):
        stripped = line.strip()
        if fence is not None:
            closes = _fence_closes(stripped, fence)
            yield number, line, True, False
            if closes:
                fence = None
            continue
        fence = _fence_open(stripped)
        yield number, line, False, fence is not None


def lines_outside_fences(text):
    """(line_number, line) for every line that is neither inside a fence nor a fence line."""
    try:
        return [(n, line) for n, line, inside, opens in _fence_events(text)
                if not inside and not opens]
    except Exception:
        return []


def matches_outside_fences(pattern, text):
    """`pattern`'s matches (a compiled regex) that start on a line outside every
    fence, judged by the same rule as the imports: a heading quoted in a code
    block is text, not a heading. The text between matches is untouched, so a
    caller slicing sections from these offsets keeps the fenced bodies."""
    try:
        live = {n for n, _line in lines_outside_fences(text)}
        return [match for match in pattern.finditer(text or "")
                if text.count("\n", 0, match.start()) + 1 in live]
    except Exception:
        return []


def _token(rest):
    out = []
    i = 0
    while i < len(rest):
        ch = rest[i]
        if ch == "\\" and i + 1 < len(rest) and rest[i + 1] == " ":
            out.append(" ")
            i += 2
            continue
        if ch.isspace():
            break
        out.append(ch)
        i += 1
    return "".join(out)


def iter_imports(text):
    """[(line_number, token)]: line-start `@token` imports outside fences."""
    try:
        found = []
        for number, line in lines_outside_fences(text):
            stripped = line.lstrip()
            if len(stripped) > 1 and stripped[0] == "@" and not stripped[1].isspace():
                token = _token(stripped[1:])
                if token:
                    found.append((number, token))
        return found
    except Exception:
        return []


def is_import_only(line, token):
    """True when `line`, stripped of whitespace, is the import token alone."""
    stripped = (line or "").strip()
    return stripped in ("@" + token, "@" + token.replace(" ", "\\ "))


def resolve(token, importing_file, home):
    """Resolve an import token to a path. Does not touch the disk."""
    try:
        if token.startswith("~/"):
            return os.path.join(home, token[2:])
        if os.path.isabs(token):
            return os.path.normpath(token)
        return os.path.normpath(os.path.join(os.path.dirname(importing_file), token))
    except Exception:
        return token


def open_fence_line(text):
    """1-based line of a fence opener that is never closed, else None."""
    try:
        fence = None
        opener = None
        for number, line in enumerate((text or "").split("\n"), 1):
            stripped = line.strip()
            if fence is not None:
                if _fence_closes(stripped, fence):
                    fence = None
                continue
            fence = _fence_open(stripped)
            if fence is not None:
                opener = number
        return opener if fence is not None else None
    except Exception:
        return None
