#!/usr/bin/env python3
"""Guard library for claude-danger-guard.

`tokenize` reproduces zsh's `${(Z+C+)text}` -- the exact word-splitting the
zsh original ran the guard's rules against -- not a general shell grammar.
`unquote` reproduces zsh's `${(Q)token}`, one level of quote removal. Both
are hand-written because `shlex` differs where the rules depend on it: it
removes quotes eagerly (the guard needs them attached until `unquote` runs),
and it splits `$(...)` into pieces where zsh keeps it as one word.

`evaluate` is the entry point's whole decision: heredoc-body stripping, one
tokenization pass, and the text-shape rules that were never legitimate here
(sudo, a download piped to a shell, a force push, history rewrite, an
empty-source refspec delete, a world-writable chmod, a raw disk write,
reading credential material) -- followed by the rm/find/cd target judging
(`resolve`, `judge_delete`, `judge_target`) and, on Windows, path
canonicalisation (`canonical`) so an `rm -rf` is judged by where it resolves
to rather than by its flags.
"""

import collections
import fnmatch
import json
import os
import re
import sys

# --- tokenize -----------------------------------------------------------
#
# zsh recognises a fixed set of operator tokens, several of which fold a
# leading file-descriptor digit or a trailing `&` into the SAME token
# (`2>&1` tokenizes as `2>&` then `1`, not `2`, `>`, `&`, `1` -- confirmed
# against the real zsh binary, since this is the one place its behaviour is
# not obvious from the manual). Longest match wins, so `<<<` is tried before
# `<<` and `;;` before `;`.
_OPERATORS_3 = ("<<<", "<<-", ">>&", "&>>")
_OPERATORS_2 = ("<<", "<>", "<&", ">>", ">|", ">&", "&>",
                ";;", "&&", "||", "|&",
                # Case-statement clause terminators (`;|`/`;&` fall through
                # to the next pattern) and the two spellings of "background,
                # disowned" (`&|` and `&!` are the same operator to zsh, like
                # `&>>`/`>>&` below) -- all verified against the real binary.
                ";|", ";&", "&|", "&!")
_OPERATORS_1 = (";", "&", "|", "(", ")", "<", ">")

# `&>>` and `>>&` are the same operator to zsh, and it always reprints the
# token as `>>&` regardless of which spelling was typed -- verified against
# the real zsh binary; every other operator reprints as typed. `&!` reprints
# as `&|` for the same reason.
_CANONICAL = {"&>>": ">>&", "&!": "&|"}

# Operators that can absorb a single leading fd digit into the SAME token
# (`2>b` -> `2>`, `b`; `12>b` does NOT absorb -- only exactly one digit
# does, verified against zsh). All of them start with `<` or `>`; the
# `&`-led forms (`&>`, `&>>`) never take a leading digit.
_FD_LEADABLE_FIRST_CHARS = ("<", ">")

# Plain space and tab are the only characters zsh's tokenizer treats as
# ordinary field-splitting whitespace. Form feed and vertical tab are NOT
# whitespace here -- they are embedded as ordinary word characters,
# verified against zsh (this differs from the `[[:space:]]` class the
# text-shape rules translate, which is a separate concern: that
# class describes what `grep -E` treats as whitespace inside a rule's
# pattern, not what this tokenizer treats as a field separator).
_WHITESPACE = (" ", "\t")


class _Unterminated(Exception):
    """Raised internally when a quoted or substitution span never closes."""


def _match_operator(text, i):
    """Return (matched_text, length) for the operator at `text[i:]`, or
    (None, 0). Tries the longest candidates first."""
    for length, table in ((3, _OPERATORS_3), (2, _OPERATORS_2), (1, _OPERATORS_1)):
        candidate = text[i:i + length]
        if candidate in table:
            return candidate, length
    return None, 0


def _skip_single_quote(text, i):
    """`text[i] == "'"`. Return the index just past the matching `'`, or
    None if it never closes. No escape processing -- zsh single quotes are
    fully literal."""
    j = text.find("'", i + 1)
    return None if j == -1 else j + 1


def _skip_double_quote(text, i):
    """`text[i] == '"'`. Return the index just past the matching `"`, or
    None if it never closes. A backslash escapes the next character for
    the purpose of finding the close (what that escape MEANS is `unquote`'s
    concern, not this scan's).

    A `` ` `` or `$(`/`${` INSIDE the string still starts a real
    substitution -- verified against zsh: its content is parsed as embedded
    shell, so a quote inside it (a nested `"..."`, which can itself carry
    another backtick) is scanned with its own rules rather than ending
    THIS string early. Skipping that recursion was the bug: `echo
    "$(echo "a b")"` used to read the bare `"` before `a` as the string's
    own close. If the nested span never closes, this whole string is
    unterminated too (`_Unterminated` propagates up the same as an
    unbalanced quote always did) -- an unbalanced backtick inside "..."
    absorbs everything after it, including what would have been this
    string's own closing `"`, exactly like an unterminated quote at any
    other level."""
    j = i + 1
    n = len(text)
    while j < n:
        ch = text[j]
        if ch == '"':
            return j + 1
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        if ch == "`":
            end = _skip_nested_backtick(text, j)
            if end is None:
                return None
            j = end
            continue
        if ch == "$" and text[j:j + 2] == "$(":
            end = _skip_dollar_paren(text, j)
            if end is None:
                return None
            j = end
            continue
        if ch == "$" and text[j:j + 2] == "${":
            end = _skip_dollar_brace(text, j)
            if end is None:
                return None
            j = end
            continue
        j += 1
    return None


def _skip_backtick(text, i):
    """`text[i] == '`'`. Return the index just past the matching backtick,
    or None if it never closes. `` \\` `` escapes the next character.

    Used only where a backtick opens at the TOP level (a bare word, or
    inside `$(...)`/`${...}` via `_skip_balanced`) -- it does not look for
    a nested quote inside itself, unlike `_skip_nested_backtick` below.
    None of this guard's real targets nest a quote inside a *top-level*
    backtick, so that gap is accepted here the same way `unquote`'s own
    docstring already accepts it for `${...}` recursion."""
    j = i + 1
    n = len(text)
    while j < n:
        ch = text[j]
        if ch == "`":
            return j + 1
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        j += 1
    return None


def _skip_nested_backtick(text, i):
    """`text[i] == '`'`, where the backtick was found INSIDE a double-quoted
    string. Return the index just past the matching closing backtick, or
    None if it never closes. Unlike the top-level `_skip_backtick` above,
    this recognises a nested `"..."` (which can itself carry another
    backtick, hence the recursion through `_skip_double_quote`) so that a
    quote inside the substitution's text is never mistaken for this span's
    own close. A nested `'...'` is skipped literally, same as anywhere
    else. `` \\` `` escapes the next character."""
    j = i + 1
    n = len(text)
    while j < n:
        ch = text[j]
        if ch == "`":
            return j + 1
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        if ch == "'":
            end = _skip_single_quote(text, j)
            if end is None:
                return None
            j = end
            continue
        if ch == '"':
            end = _skip_double_quote(text, j)
            if end is None:
                return None
            j = end
            continue
        j += 1
    return None


def _skip_dollar_squote(text, i):
    """`text[i:i+2] == "$'"`. Return the index just past the matching `'`,
    or None if it never closes. A backslash escapes the next character,
    same as inside the quotes themselves (`\\'` does not close it)."""
    j = i + 2
    n = len(text)
    while j < n:
        ch = text[j]
        if ch == "'":
            return j + 1
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        j += 1
    return None


def _skip_balanced(text, i, open_ch, close_ch):
    """`text[i] == open_ch`. Return the index just past the matching
    `close_ch`, tracking nesting depth and skipping over quoted spans (so a
    `)` or `}` inside a quoted string never counts), or None if it never
    balances. Shared by `$(...)`, `${...}` and bare `(...)` grouping inside
    a substitution."""
    j = i + 1
    n = len(text)
    depth = 1
    while j < n:
        ch = text[j]
        if ch == "$" and text[j:j + 2] == "$'":
            # CR-1: this must be tried BEFORE the plain "'" check below.
            # `$'...'` is ANSI-C quoting, which processes `\'` as an escaped
            # quote rather than a closer; `_skip_single_quote` has no
            # escape processing at all (real `'...'` never needs any), so
            # treating a `$'` span as a plain `'...'` span closed on the
            # FIRST embedded `\'`, leaving the true closing `'` unmatched
            # and the rest of the line absorbed as if it were still inside
            # this substitution -- verified against zsh:
            # `$(printf '%s' $'a\'b')` used to swallow a trailing
            # `; rm -rf ~/Documents` into one token.
            end = _skip_dollar_squote(text, j)
            if end is None:
                return None
            j = end
            continue
        if ch == "'":
            end = _skip_single_quote(text, j)
            if end is None:
                return None
            j = end
            continue
        if ch == '"':
            end = _skip_double_quote(text, j)
            if end is None:
                return None
            j = end
            continue
        if ch == "`":
            end = _skip_backtick(text, j)
            if end is None:
                return None
            j = end
            continue
        if ch == "\\" and j + 1 < n:
            j += 2
            continue
        if (ch == "#" and open_ch == "("
                and text[j - 1] in " \t\n(;&|"):
            # A `#` starting a word inside `(...)` opens a comment to the end
            # of the line, and a quote inside it quotes nothing -- verified
            # against zsh: `$(echo x # ' apostrophe<LF>)` closes at the `)`.
            # Read as live quoting, the apostrophe used to swallow a trailing
            # `; rm -rf ~/Documents`. Mid-word (`a#b`, `2#101`) it is a plain
            # character, and inside `${...}` it is an operator (`${x#pat}`),
            # hence the `open_ch` test. The newline itself is left to the loop.
            while j < n and text[j] != "\n":
                j += 1
            continue
        if ch == open_ch:
            depth += 1
            j += 1
            continue
        if ch == close_ch:
            depth -= 1
            j += 1
            if depth == 0:
                return j
            continue
        j += 1
    return None


def _skip_dollar_paren(text, i):
    """`text[i:i+2] == "$("`. Return the index just past the matching `)`,
    or None if it never balances."""
    return _skip_balanced(text, i + 1, "(", ")")


def _skip_dollar_brace(text, i):
    """`text[i:i+2] == "${"`. Return the index just past the matching `}`,
    or None if it never balances."""
    return _skip_balanced(text, i + 1, "{", "}")


def _skip_process_subst(text, i):
    """`text[i:i+2]` is `<(`, `>(` or `=(`. Return the index just past the
    matching `)`, or None if it never balances -- process/pipe substitution
    absorbs exactly like `$(...)`, just without the `$`."""
    return _skip_balanced(text, i + 1, "(", ")")


def _skip_arithmetic_command(text, i):
    """`text[i:i+2] == "(("`. Return the index just past the matching `))`,
    or None if it never balances. Ordinary depth tracking on `(`/`)`
    naturally consumes both closing parens (the inner one brings depth from
    2 to 1, the outer from 1 to 0), so this is the same scan `$((...))`
    already runs via `_skip_dollar_paren` -- only the trigger differs (no
    leading `$`, and it fires at the start of a word rather than anywhere)."""
    return _skip_balanced(text, i, "(", ")")


def _strip_bare_continuations(s):
    """CR-1: remove every backslash immediately followed by a literal LF
    from `s` -- a line continuation, same as at the top level (see
    `tokenize`'s own `ch == "\\"` handling) -- while leaving a nested
    `'...'`, `$'...'` or `$(...)`/process-substitution span completely
    untouched, continuation and all.

    THE GENERAL RULE, probed against the real zsh binary: a backslash-
    newline is joined away everywhere zsh reads characters through the
    same low-level stream as ordinary text -- a bare word, `"..."`,
    `` `...` `` and `${...}` all agree (`echo "a\\<LF>b"` -> `echo "ab"`;
    `` `r\\<LF>m x` `` -> `` `rm x` ``; `${x\\<LF>y}` -> `${xy}`) -- but
    NOT inside a span the shell defers to a later, separate read: `'...'`
    and `$'...'` are fully literal by definition, and `$(...)`/process
    substitution's body is handed to a nested parse rather than joined at
    this level (`$(r\\<LF>m x)` keeps the backslash and the newline both,
    confirmed against zsh, unlike the otherwise-identical backtick form).
    Called only on text already known to be entirely inside one `"..."`,
    `` `...` `` or `${...}` span; nesting a FURTHER `"..."`/backtick/
    `${...}` inside needs no special case, since those want the same
    stripping applied right through them -- only the three deferred
    spans above need protecting, so those are the only ones this skips
    over. A bare `((...))` inside such a span is not protected the same
    way (accepted gap: real targets do not nest one inside a string)."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "'":
            end = _skip_single_quote(s, i)
            end = n if end is None else end
            out.append(s[i:end])
            i = end
            continue
        if ch == "$" and s[i:i + 2] == "$'":
            end = _skip_dollar_squote(s, i)
            end = n if end is None else end
            out.append(s[i:end])
            i = end
            continue
        if ch == "$" and s[i:i + 2] == "$(":
            end = _skip_dollar_paren(s, i)
            end = n if end is None else end
            out.append(s[i:end])
            i = end
            continue
        if ch in ("<", ">", "=") and s[i:i + 2] == ch + "(":
            end = _skip_process_subst(s, i)
            end = n if end is None else end
            out.append(s[i:end])
            i = end
            continue
        if ch == "\\" and i + 1 < n:
            if s[i + 1] == "\n":
                i += 2
                continue
            out.append(s[i:i + 2])
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _ends_word(text, i):
    """Is position `i` a token boundary -- whitespace, a newline/CR, an
    operator, or the end of input -- rather than the middle of a word?
    This is what decides whether a `}` at `i` is a standalone word (see
    `tokenize`'s command-position comment) or just a literal character
    glued to whatever follows."""
    if i >= len(text):
        return True
    if text[i] in _WHITESPACE or text[i] == "\n":
        return True
    return _match_operator(text, i)[0] is not None


_Scan = collections.namedtuple("_Scan", (
    "tokens", "resume_offset", "resume_brace_depth",
    "resume_expect_command", "resume_token_count", "stop_offset"))


def _scan(text, brace_depth=0, expect_command=True, stop_after_first=False):
    """`tokenize`'s loop (see its docstring for the grammar), generalised
    two ways: it can resume mid-scan, and it can stop as soon as the
    first token is known.

    Resume property: for any string `P` and any string `X`,
    `tokenize(P + "\\n" + X)` equals
    `r.tokens[:r.resume_token_count] + _scan(P[r.resume_offset:] + "\\n" + X,
    r.resume_brace_depth, r.resume_expect_command).tokens`, where
    `r = _scan(P)` (modulo `tokenize`'s `[""]` fallback for an empty
    result). The resume point is either the end of `P` with no word in
    progress (`resume_offset == len(P)`), or the start of the word still
    open at the end of `P` -- everything before that point was already
    decided by text inside `P` alone.

    `stop_after_first`: stop as soon as a complete token exists, without
    scanning the rest of `text`. `stop_offset` is then the offset right
    after that first token (and whatever closed it -- an operator or a
    newline's `;`), i.e. the smallest prefix of `text` guaranteed to
    reproduce that one token; it is `None` if the scan ran out of `text`
    before any token completed. Under an early stop the `resume_*` fields
    describe nothing useful; only `stop_offset` is meaningful.
    """
    tokens = []
    # `cur` is a list of pieces whose concatenation is the word in progress
    # (joined once, with `"".join`, when the word ends), not a string grown
    # by repeated `+` on a cell variable. CPython 3.13 does not grow such a
    # string in place, so the old style of append copied the whole word every
    # time -- quadratic in the word's length. A list's `append` is O(1)
    # regardless.
    cur = None
    i = 0
    n = len(text)
    stop_offset = None
    # `mark` holds (offset, brace_depth, expect_command, token_count) as of
    # the last point `cur` was `None` -- i.e. if a word is still open when
    # the loop ends, `mark` is exactly where that word started.
    mark = (0, brace_depth, expect_command, 0)
    # expect_command: True only when the next character would start a word
    # in COMMAND POSITION -- see `tokenize`'s docstring's `{` rule. Building
    # an ordinary word (any branch that extends `cur`, or absorbs a
    # substitution into it) turns this off; only emitting a token turns it
    # back on. Whitespace alone does neither: argument position stays
    # argument position between two words.

    def flush():
        nonlocal cur
        if cur is not None:
            tokens.append("".join(cur))
            cur = None

    def extend(piece):
        nonlocal cur
        if cur is None:
            cur = [piece]
        else:
            cur.append(piece)

    def absorb_rest(start):
        # An unterminated quote/substitution: everything from `start` to
        # the end of input becomes part of the word already in progress.
        nonlocal cur, i
        extend(text[start:])
        i = n

    while i < n:
        if cur is None:
            mark = (i, brace_depth, expect_command, len(tokens))
        if stop_after_first and tokens:
            stop_offset = i
            break
        ch = text[i]

        if ch in _WHITESPACE:
            flush()
            i += 1
            continue

        if ch == "\n":
            flush()
            tokens.append(";")
            i += 1
            expect_command = True
            continue

        if ch == "#" and cur is None:
            while i < n and text[i] != "\n":
                i += 1
            continue

        if ch == "{" and expect_command:
            flush()
            tokens.append("{")
            brace_depth += 1
            i += 1
            expect_command = True
            continue

        if ch == "}" and brace_depth > 0 and cur is None and _ends_word(text, i + 1):
            tokens.append("}")
            brace_depth -= 1
            i += 1
            expect_command = True
            continue

        if ch == "(" and text[i:i + 2] == "()":
            flush()
            tokens.append("()")
            i += 2
            expect_command = True
            continue

        if ch == "(" and cur is None and text[i:i + 2] == "((":
            end = _skip_arithmetic_command(text, i)
            if end is None:
                absorb_rest(i)
                break
            tokens.append(text[i:end])
            i = end
            expect_command = True
            continue

        if ch == "'":
            end = _skip_single_quote(text, i)
            if end is None:
                absorb_rest(i)
                break
            extend(text[i:end])
            i = end
            expect_command = False
            continue

        if ch == '"':
            end = _skip_double_quote(text, i)
            if end is None:
                absorb_rest(i)
                break
            # CR-1: a "..." span reads from the same character stream as
            # ordinary text, so a backslash-newline inside it is stripped
            # too -- see `_strip_bare_continuations`.
            extend(_strip_bare_continuations(text[i:end]))
            i = end
            expect_command = False
            continue

        if ch == "`":
            end = _skip_backtick(text, i)
            if end is None:
                absorb_rest(i)
                break
            extend(_strip_bare_continuations(text[i:end]))
            i = end
            expect_command = False
            continue

        if ch == "$" and text[i:i + 2] == "$'":
            end = _skip_dollar_squote(text, i)
            if end is None:
                absorb_rest(i)
                break
            extend(text[i:end])
            i = end
            expect_command = False
            continue

        if ch == "$" and text[i:i + 2] == "$(":
            end = _skip_dollar_paren(text, i)
            if end is None:
                absorb_rest(i)
                break
            extend(text[i:end])
            i = end
            expect_command = False
            continue

        if ch == "$" and text[i:i + 2] == "${":
            end = _skip_dollar_brace(text, i)
            if end is None:
                absorb_rest(i)
                break
            extend(_strip_bare_continuations(text[i:end]))
            i = end
            expect_command = False
            continue

        if ch in ("<", ">", "=") and text[i:i + 2] == ch + "(":
            end = _skip_process_subst(text, i)
            if end is None:
                absorb_rest(i)
                break
            extend(text[i:end])
            i = end
            expect_command = False
            continue

        if ch == "\\" and i + 1 < n and text[i + 1] == "\n":
            # CR-1: a backslash immediately followed by a literal LF is a
            # line continuation -- zsh drops both characters and does not
            # even leave a token boundary, so "r\<LF>m" tokenizes as one
            # word "rm", verified against the real binary. A backslash
            # before a CR is NOT a continuation: CR is an ordinary word
            # character to zsh -- never whitespace, never a separator, never
            # folded to LF -- so `\<CR>` is an ordinary escaped character
            # (the generic branch below keeps both).
            i += 2
            continue

        if ch == "\\":
            extend(text[i:i + 2])
            i += 2
            expect_command = False
            continue

        op, oplen = _match_operator(text, i)
        if op is not None:
            canon = _CANONICAL.get(op, op)
            word = "".join(cur) if cur is not None else None
            if (word is not None and len(word) == 1 and word in "0123456789"
                    and op[0] in _FD_LEADABLE_FIRST_CHARS):
                tokens.append(word + canon)
                cur = None
            else:
                flush()
                tokens.append(canon)
            i += oplen
            expect_command = True
            continue

        extend(ch)
        i += 1
        expect_command = False

    if stop_after_first and stop_offset is None and tokens:
        stop_offset = min(i, n)

    if cur is None:
        (resume_offset, resume_brace_depth,
         resume_expect_command, resume_token_count) = (
            len(text), brace_depth, expect_command, len(tokens))
    else:
        (resume_offset, resume_brace_depth,
         resume_expect_command, resume_token_count) = mark

    flush()
    return _Scan(tokens, resume_offset, resume_brace_depth,
                 resume_expect_command, resume_token_count, stop_offset)


def tokenize(text):
    """The token list zsh's `${(Z+C+)text}` produces. Does no I/O; imports
    only stdlib (none, in fact -- this module has no imports).

    Words keep their quote characters attached; `$(...)`, `${...}`,
    `<(...)`, `>(...)`, `=(...)` (process/pipe substitution -- absorbed the
    same way `$(...)` is, just with no leading `$`), backticks and nested
    quoting inside them stay part of one word. Operators are their own
    tokens. An unquoted newline becomes a `;` token. `#` at the start of a
    word begins a comment to end of line, which is dropped. A top-level
    unbalanced quote or substitution collapses the rest of the input into
    one token, rather than failing -- verified against zsh: it swallows
    from the START of the word currently being built, not just from the
    unterminated character.

    `{`, `}` and empty `()` are reserved-word-like tokens, not ordinary
    operators, and needed their own probe against the real zsh binary
    rather than a shell-grammar reference -- this is the one place zsh's
    tokenizer disagrees with a flat "operators always split" model, and the
    reason a Bash tool running under zsh (as it does on this Mac) actually
    EXECUTES `{rm -rf ~/Documents;}` as a delete rather than treating it as
    one inert word:

    - `{` splits off its own token ONLY in COMMAND POSITION: the start of
      input, or right after an operator that starts a new command, or
      right after a `{`/`}`/`()` token just emitted (tracked below as
      `expect_command`). `echo {rm x;}`, `f {rm x;}` and
      `function f {rm x;}` all keep `{rm` glued -- `{` is the SECOND word
      there (an argument), never recognised, however dangerous the body.
    - `}` closes the innermost still-open `{` (`brace_depth > 0`), but only
      when it would otherwise stand as its OWN word: nothing accumulated
      yet (`cur is None`) and the next character is itself a boundary
      (`_ends_word`). `{rm y;}extra` glues `}extra` into one word for this
      reason -- with more text attached on the right, `}` never isolates
      into its own word, so it is never recognised as the closer. Unlike
      `{`, this needs no command-position check: `{ x }` closes fine even
      though the ordinary word `x` (not an operator) precedes it.
    - `()` with NOTHING between the parens (`f()`, `f ()`, bare `()`) is
      the empty-parens function-definition marker: it splits off as one
      `()` token regardless of position -- mid-word (`f(){…}`) or after
      whitespace (`f () {…}`) -- and puts the tokenizer back in command
      position for the `{` that follows. The instant anything (even one
      space) sits between the parens this rule does not apply, and plain
      `(`/`)` fall back to being unconditional operators, matching every
      fixture exercised here (`f( ){…}`-style non-empty-but-attached parens
      are not one, so that gap is not closed).

    A bare `((...))` -- the arithmetic COMMAND, no leading `$` -- is
    recognised at the START of a word (`cur is None`) in ANY position, not
    just command position (`echo ((i++))` recognises it as an argument).
    Unlike `$((...))`, which continues the surrounding word like any other
    substitution, the bare form is a complete command by itself:
    `((a+b))c` splits `c` into a new word with no separating whitespace, so
    it is absorbed and immediately flushed rather than left open for more
    text to attach to.
    """
    tokens = _scan(text).tokens
    return tokens if tokens else [""]


# --- unquote --------------------------------------------------------------
#
# zsh `${(Q)token}`: one level of quote removal. On a malformed token --
# any of `'...'`, `"..."` or `$'...'` failing to close -- zsh returns the
# token COMPLETELY UNCHANGED rather than partially processed; verified
# against the real binary. This is a different failure shape from
# `tokenize`'s "absorb the rest", and deliberately so: `unquote` runs on
# one already-extracted token, so there is no "rest of the command" left
# to absorb into.

_SIMPLE_DOLLAR_ESCAPES = {
    "a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f",
    "n": "\n", "r": "\r", "t": "\t", "v": "\v",
    "\\": "\\", "'": "'", '"': '"',
}

_HEX_DIGITS = "0123456789abcdefABCDEF"


def _decode_dollar_escape(s, i):
    """`s[i]` is the character right after a backslash inside `$'...'`.
    Return (decoded_text, chars_consumed_from_i). Unrecognised escapes
    pass the character through literally with the backslash dropped --
    verified against zsh (e.g. `\\c` alone decodes to `c`, not a control
    character; zsh's `$'...'` does not implement `\\C-x` from `print`)."""
    ch = s[i]
    if ch in _SIMPLE_DOLLAR_ESCAPES:
        return _SIMPLE_DOLLAR_ESCAPES[ch], 1
    if ch in "01234567":
        j = i
        digits = ""
        while j < len(s) and len(digits) < 3 and s[j] in "01234567":
            digits += s[j]
            j += 1
        return chr(int(digits, 8) & 0xFF), len(digits)
    if ch == "x":
        j = i + 1
        digits = ""
        while j < len(s) and len(digits) < 2 and s[j] in _HEX_DIGITS:
            digits += s[j]
            j += 1
        if digits:
            return chr(int(digits, 16)), 1 + len(digits)
        return "x", 1
    if ch == "u":
        j = i + 1
        digits = ""
        while j < len(s) and len(digits) < 4 and s[j] in _HEX_DIGITS:
            digits += s[j]
            j += 1
        if digits:
            return chr(int(digits, 16)), 1 + len(digits)
        return "u", 1
    if ch == "U":
        j = i + 1
        digits = ""
        while j < len(s) and len(digits) < 8 and s[j] in _HEX_DIGITS:
            digits += s[j]
            j += 1
        if digits:
            return chr(int(digits, 16)), 1 + len(digits)
        return "U", 1
    return ch, 1


def _unquote_strict(token):
    """Do the removal; raise `_Unterminated` on any quote that never
    closes, so the caller can fall back to returning the token unchanged."""
    out = []
    i = 0
    n = len(token)
    while i < n:
        ch = token[i]

        # CR-1: a TOP-LEVEL $(...) is a command substitution, left
        # COMPLETELY untouched by (Q) -- same as this function's existing
        # handling of one nested inside "..." below, just missing at this
        # level. Without it, a quote INSIDE the substitution (the $'...'
        # CR-1 exists to fix) read as this token's OWN quoting and mangled
        # it -- verified against zsh, which returns such a token
        # byte-identical to its input.
        if ch == "$" and token[i:i + 2] == "$(":
            end = _skip_dollar_paren(token, i)
            if end is None:
                raise _Unterminated
            out.append(token[i:end])
            i = end
            continue

        if ch == "'":
            j = token.find("'", i + 1)
            if j == -1:
                raise _Unterminated
            out.append(token[i + 1:j])
            i = j + 1
            continue

        if ch == '"':
            i += 1
            while True:
                if i >= n:
                    raise _Unterminated
                c = token[i]
                if c == '"':
                    i += 1
                    break
                if c == "\\" and i + 1 < n:
                    nxt = token[i + 1]
                    if nxt == "\n":
                        i += 2
                    elif nxt in ('"', "\\", "$", "`"):
                        out.append(nxt)
                        i += 2
                    else:
                        out.append(c)
                        i += 1
                    continue
                # A `` ` `` or `$(...)` inside the string is a real
                # substitution, left COMPLETELY untouched -- copied
                # verbatim, quote characters and all -- same as at the top
                # level (see `unquote`'s own docstring). Finding its close
                # still has to be quote-aware (`_skip_nested_backtick`
                # recurses through a nested "..."), or a quote inside the
                # substitution reads as this string's own close, same bug
                # as the tokenizer's `_skip_double_quote` had.
                if c == "`":
                    end = _skip_nested_backtick(token, i)
                    if end is None:
                        raise _Unterminated
                    out.append(token[i:end])
                    i = end
                    continue
                if c == "$" and token[i:i + 2] == "$(":
                    end = _skip_dollar_paren(token, i)
                    if end is None:
                        raise _Unterminated
                    out.append(token[i:end])
                    i = end
                    continue
                out.append(c)
                i += 1
            continue

        if ch == "$" and token[i:i + 2] == "$'":
            i += 2
            while True:
                if i >= n:
                    raise _Unterminated
                c = token[i]
                if c == "'":
                    i += 1
                    break
                if c == "\\" and i + 1 < n:
                    decoded, consumed = _decode_dollar_escape(token, i + 1)
                    out.append(decoded)
                    i += 1 + consumed
                    continue
                out.append(c)
                i += 1
            continue

        if ch == "\\":
            if i + 1 >= n:
                # A bare trailing backslash escapes nothing and vanishes --
                # verified against zsh (`ab\` unquotes to `ab`).
                i += 1
                continue
            nxt = token[i + 1]
            if nxt == "\n":
                i += 2
            else:
                out.append(nxt)
                i += 2
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def unquote(token):
    """zsh `${(Q)token}`: removes one level of `'...'`, `"..."` (with
    `\\"`, `\\\\`, `\\$`, `` \\` `` escapes inside), `$'...'` (with the
    standard backslash escapes) and bare backslash escapes. Does no I/O;
    imports only stdlib.

    `$(...)`, `` `...` `` and bare `(...)` grouping are left untouched --
    verified against zsh: quote characters INSIDE a command substitution
    are not this token's quoting and `(Q)` does not reach into them, unlike
    `${...}`, which it does recurse into. Since none of this guard's real
    targets nest a quote inside a substitution, that recursion is not
    reproduced here; the plain character-by-character scan already agrees
    with the oracle wherever `${...}` carries no embedded quote.
    """
    try:
        return _unquote_strict(token)
    except _Unterminated:
        return token


# --- evaluate: context, config, heredoc stripping, text-shape rules -------
#
# Ports the zsh claude-danger-guard's context, config, heredoc and text-shape
# rules faithfully; the rm/find/cd target judging and Windows path canonicalisation follow
# further down.

Verdict = collections.namedtuple("Verdict", "blocked message")
# blocked: bool. message: the full stderr text when blocked, "" when allowed.

Context = collections.namedtuple(
    "Context", "home config_path windows native_temp danger_allow environ",
    defaults=(None,))
# home: str -- what `~` and `$HOME` in a command expand to.
# config_path: str -- the config file (may not exist).
# windows: bool -- canonicalise paths; False leaves every path as written.
# native_temp: str or None -- Windows user temp root, an extra safe root.
# danger_allow: bool.
# environ: mapping or None -- variables config paths may name; None resolves none.


def context_from_environment():
    """Build a Context from the live process. The ONLY function in this
    module that reads os.environ."""
    home = os.environ.get("HOME") or os.path.expanduser("~")
    config_path = os.environ.get("CLAUDE_TOOLKIT_CONFIG") or os.path.join(
        home, ".config", "claude-code-toolkit", "config.json")
    windows = os.name == "nt"
    native_temp = None
    if windows:
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            native_temp = os.path.join(userprofile, "AppData", "Local", "Temp")
    danger_allow = os.environ.get("DANGER_ALLOW") == "1"
    return Context(home=home, config_path=config_path, windows=windows,
                   native_temp=native_temp, danger_allow=danger_allow,
                   environ=dict(os.environ))


# --- machine-local config ----------------------------------------------------
# Read from the toolkit config rather than hardcode anything person-specific
# here, and only when the file exists. A missing or broken config is the
# STRICT case, not the permissive one: with no workspace_root, the only
# trusted roots are ~/.claude, ~/bin and the temp directories, so an
# unconfigured install refuses deletions in a project tree rather than
# guessing which tree to trust (repo CLAUDE.md, "The guard fails open; the
# config fails strict").
_Config = collections.namedtuple(
    "_Config", "owner_name workspace_root extra_safe_roots extra_protected_paths")

_STRICT_CONFIG = _Config(owner_name="the human", workspace_root="",
                         extra_safe_roots=[], extra_protected_paths=[])


_toolkit_config_module = None


def _toolkit_config():
    """lib/toolkit_config.py, loaded by file path so lib/ can never shadow a
    guard module. Raises on failure; every caller sits inside _load_config's
    try, which turns any failure into _STRICT_CONFIG."""
    global _toolkit_config_module
    if _toolkit_config_module is None:
        module = sys.modules.get("toolkit_config")
        if module is None:
            import importlib.util
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.realpath(__file__)))), "lib", "toolkit_config.py")
            spec = importlib.util.spec_from_file_location("toolkit_config", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        _toolkit_config_module = module
    return _toolkit_config_module


def _expand_path(p, home, environ=None):
    """Delegates to `toolkit_config.expand_path` (one implementation),
    then trims a trailing slash -- it breaks the prefix match
    `inside_safe_root` runs -- except the root `/` itself, which must stay
    whole rather than becoming empty. Returns None for an unresolved path.
    With no `environ`, no variables are resolved (never the process's)."""
    p = _toolkit_config().expand_path(
        p, home=home, environ=environ if environ is not None else {},
        strict=True)
    if p is None:
        return None
    if p != "/" and p.endswith("/"):
        p = p[:-1]
    return p


def _load_config(config_path, home, environ=None):
    """Read `config_path` into a `_Config`, or `_STRICT_CONFIG` on ANY
    failure -- a missing file, invalid JSON, or a value shaped nothing like
    what is documented (a truthy non-list `extra_safe_roots`, say). A
    config-driven crash would allow every command while it exists, which is
    the one failure mode this guard cannot have: the zsh original blocks
    `sudo ls` under the same malformed config, so a crash here would be a
    REGRESSION, not a stricter guard.

    Fields that only ever ADD trust are read per field: an
    `extra_safe_roots` that is not a JSON array is ignored, a non-string or
    unexpandable element inside it is dropped, and a `workspace_root` or
    `owner_name` that is not a string is ignored. Dropping any of those can
    only refuse more. Protection is the opposite: an `extra_protected_paths`
    that is present (not null) but is not an array of strings that all
    expand, or a `danger_guard` section that is present but not an object,
    makes the whole config strict. Dropping it per field would silently
    make the repo roots someone listed deletable; going strict refuses
    deletions loudly until the config is fixed. A top-level value that is
    not an object is no config at all."""
    try:
        return _load_config_unsafe(config_path, home, environ)
    except Exception:
        return _STRICT_CONFIG


def _load_config_unsafe(config_path, home, environ=None):
    owner_name = "the human"
    workspace_root = ""
    extra_safe_roots = []
    extra_protected_paths = []
    try:
        # The ambient locale (cp1252 on Windows, ASCII under LC_ALL=C) would
        # read a non-ASCII config as absent (strict) or misread a protected
        # path. setup_config writes it ASCII-escaped; a hand edit is UTF-8.
        with open(config_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        data = None
    if not isinstance(data, dict):
        return _STRICT_CONFIG
    ws = data.get("workspace_root")
    if isinstance(ws, str) and ws:
        workspace_root = _expand_path(ws, home, environ) or ""
    owner = data.get("owner_name")
    if isinstance(owner, str) and owner:
        owner_name = owner
    danger_guard_cfg = data.get("danger_guard")
    if danger_guard_cfg is not None and not isinstance(danger_guard_cfg, dict):
        # Whatever protection the section held cannot be read: strict.
        return _STRICT_CONFIG
    if isinstance(danger_guard_cfg, dict):
        raw_safe = danger_guard_cfg.get("extra_safe_roots")
        if isinstance(raw_safe, list):
            extra_safe_roots = [
                expanded for expanded in (
                    _expand_path(item, home, environ) for item in raw_safe
                    if isinstance(item, str)) if expanded is not None]
        raw_protected = danger_guard_cfg.get("extra_protected_paths")
        if raw_protected is not None:
            # Never per field: a protected path the guard cannot honour
            # would otherwise vanish without a word (see `_load_config`).
            if not isinstance(raw_protected, list):
                return _STRICT_CONFIG
            for item in raw_protected:
                expanded = (_expand_path(item, home, environ)
                            if isinstance(item, str) else None)
                if expanded is None:
                    return _STRICT_CONFIG
                extra_protected_paths.append(expanded)
    return _Config(owner_name=owner_name, workspace_root=workspace_root,
                   extra_safe_roots=extra_safe_roots,
                   extra_protected_paths=extra_protected_paths)


# --- heredoc bodies out first, THEN one tokenization -------------------------
#
# Heredoc bodies are data, not commands. A commit message or a written file
# that merely mentions `curl | sh` is not an attempt to run it, and a guard
# that can't tell the difference gets in the way of ordinary work -- which is
# how guards end up disabled. Drop the bodies; the block message still shows
# the original command, so it stays readable.
#
# THE ORDER HERE IS THE WHOLE DESIGN, and getting it wrong shipped a false
# ALLOW twice in the zsh original. Two different questions need two different
# instruments: where a heredoc STARTS is a shell-structure question, so it
# needs a tokenizer -- a `<<` inside quotes or a comment is not an operator,
# and only the tokenizer knows that. Where a heredoc ENDS is not a shell
# question at all: the body is raw text, compared whole LINE against the
# delimiter, never parsed. So bodies are cut LINE-WISE, and cut BEFORE
# anything is tokenized, which also means a body's quoting can never leak
# into the tokenizer at all.
#
# Re-tokenizing the whole kept prefix on every opener line was quadratic in
# the number of openers -- enough `cat <<EOF` blocks ran far past the hook's
# timeout, and a killed hook lets the command run. So `_strip_heredocs`
# RESUMES instead: `_scan` reports where a later scan can pick up (see its
# resume property), and each opener line tokenizes only the text since that
# point. That alone leaves one quadratic shape: a quote left open pins the
# resume point at the quote, and every later opener re-scans the growing
# span. `_heredoc_skip_through` closes it. While the first token after the
# resume point is still open, every opener line sees that same one token, so
# no new delimiter appears and nothing is cut -- the kept lines ARE the raw
# lines. So one look ahead over the raw lines finds where that token ends,
# and every opener line before it skips tokenization, exactly.
def _line_opens_heredoc(line):
    """Is `<<` or `<<-` a real operator token on this line (rather than
    inside a quote or a comment)? A quote opened on an EARLIER line cannot be
    seen by tokenizing this line alone, so this may answer "yes" where the
    full prefix would say no -- that costs one extra prefix tokenization and
    nothing else. It cannot answer "no" for a real opener: an opener is a
    bare `<<`/`<<-` token in its own line's tokenization regardless of what
    came before."""
    if "<<" not in line:
        return False
    for tok in tokenize(line):
        if tok == "<<" or tok == "<<-":
            return True
    return False


_HEREDOC_STRIP_CHARS = " \t\f\v\r"


def _heredoc_skip_through(head, brace_depth, expect_command, src_lines, j0):
    """`head` is the in-progress word from the resume point to the end of
    the current kept text, i.e. `text[resume_offset:]`. Returns the largest
    raw line index `j` such that opener lines `j0 .. j` may skip
    tokenization -- their tokenization is known to cut nothing, because
    the first token after the resume point does not end until after line
    `j`. Returns `j0 - 1` when no line may be skipped.

    Gallops by line count (1, 2, 4, ...) rather than scanning one line at a
    time, so the number of `_scan` calls is logarithmic in how far ahead the
    first token ends, not linear in it.
    """
    n = len(src_lines)
    count = 1
    while True:
        hi = min(n, j0 + count)
        chunk = src_lines[j0:hi]
        F = head + "\n" + "\n".join(chunk) if chunk else head
        r = _scan(F, brace_depth, expect_command, stop_after_first=True)
        if r.stop_offset is not None:
            off = len(head)
            skip = j0 - 1
            for t, line in enumerate(chunk):
                off += 1 + len(line)  # off = index of the end of line j0+t in F
                # A line ending strictly before stop_offset ends inside the
                # first token, or exactly where it ends (when its boundary
                # is the newline). So the prefix through that line holds one
                # token from the resume point, and its tokenization cuts
                # nothing. This is the safe direction.
                if off < r.stop_offset:
                    skip = j0 + t
                else:
                    break
            return skip
        if hi >= n:
            return n - 1  # the first token never ends: skip every remaining line
        count *= 2


def _strip_heredocs(raw_cmd):
    """Return the command's lines with every heredoc body (and its
    terminator line) removed, in source order. `${(@f)raw_cmd}` and Python's
    `str.split("\\n")` agree exactly on how a string becomes a line list,
    trailing newline included -- verified against zsh."""
    src_lines = raw_cmd.split("\n")
    n = len(src_lines)
    kept = []
    tail = []                    # kept text since the resume point, as "\n"-joined parts
    bd, ec = 0, True             # tokenizer state at the resume point
    committed = 0                # len(delims) produced by tokens before the resume point
    expect, expect_dash = False, False   # delim-scanner state at the resume point
    delims = []
    dashed = []
    handled = 0
    skip_through = -1
    li = 0
    while li < n:
        line = src_lines[li]
        kept.append(line)
        tail.append(line)
        if li > skip_through and _line_opens_heredoc(line):
            text = "\n".join(tail)
            r = _scan(text, bd, ec)
            del delims[committed:]
            del dashed[committed:]
            e, ed = expect, expect_dash
            snap = None
            for k, tok in enumerate(r.tokens):
                if k == r.resume_token_count:
                    snap = (len(delims), e, ed)
                if e:
                    delims.append(unquote(tok))
                    dashed.append(ed)
                    e = False
                elif tok == "<<":
                    e = True
                    ed = False
                elif tok == "<<-":
                    e = True
                    ed = True
            if snap is None:
                snap = (len(delims), e, ed)
            committed, expect, expect_dash = snap
            # `<<<` is a here-string and never reaches this list: it is its
            # own token, so it never sets `expect`. Several openers on one
            # line are consumed in order, which is the order the shell reads
            # their bodies in.
            while handled < len(delims):
                delim = delims[handled]
                is_dashed = dashed[handled]
                handled += 1
                li += 1
                while li < n:
                    # ONLY `<<-` allows an indented terminator. Stripping
                    # indentation for a plain `<<` ends the body early, and
                    # the rest of the body is then scanned as if it were
                    # shell.
                    if is_dashed:
                        candidate = src_lines[li].lstrip(_HEREDOC_STRIP_CHARS)
                    else:
                        candidate = src_lines[li]
                    if candidate == delim:
                        break
                    li += 1
                # An unterminated heredoc runs off the end and the rest is
                # dropped -- what the shell does with it too.
            head = text[r.resume_offset:]
            tail = [head]
            bd, ec = r.resume_brace_depth, r.resume_expect_command
            if r.resume_offset < len(text):
                skip_through = _heredoc_skip_through(head, bd, ec, src_lines, li + 1)
        li += 1
    return kept


# --- heredoc-in-substitution hint --------------------------------------------
#
# A heredoc opened INSIDE a command substitution is the one shape whose body
# this guard cannot separate from real commands: `tokenize` hands back the
# whole `$(...)` as a single token, body included, so no bare `<<` token ever
# appears, the body is never stripped, and the shape rules below read prose as
# if it were a command. It is a false BLOCK and it fails safe, so this adds a
# HINT and changes no verdict. Only the text-shape rules can reach a
# substitution's contents; the rm/find/cd rules are token- and
# segment-based and cannot see into one either way.
def _heredoc_in_substitution(raw_cmd):
    if "<<" not in raw_cmd:
        return False
    return "$(" in raw_cmd or "`" in raw_cmd


def _closing_lines(owner_name):
    """The two lines every block message ends with -- factored out so the
    PowerShell refusal (a differently-shaped message, no `Command:` line, no
    heredoc hint) shares the exact text and OWNER_NAME substitution rather
    than a hand copy that could drift from this one."""
    return [
        "If this is genuinely what you want, hand it to %s to run," % owner_name,
        "or re-run this session with DANGER_ALLOW=1 set.",
    ]


def _build_message(rule_message, raw_cmd, owner_name):
    lines = [
        "BLOCKED by claude-danger-guard: %s" % rule_message,
        "",
        "Command: %s" % raw_cmd,
        "",
    ]
    if _heredoc_in_substitution(raw_cmd):
        lines += [
            "If that matched PROSE rather than a command you meant to run: a heredoc",
            "opened inside $(...) is the one shape whose body this guard cannot tell",
            "apart from real commands, so it scans the body too. Put the text in a",
            "file and use  git commit -F <file>  — the body is then never scanned.",
            "",
        ]
    lines += _closing_lines(owner_name)
    return "\n".join(lines) + "\n"


# --- the PowerShell tool has no guard of its own -----------------------------
#
# Claude Code can invoke the PowerShell tool directly, which none of the rules
# above ever see -- they all key on tool_name == "Bash". Rather than build and
# maintain a second tokenizer for PowerShell syntax, refuse the tool outright,
# whatever the command. This is the one verdict that does not depend on the
# command text at all.
def _build_powershell_message(owner_name):
    lines = [
        "BLOCKED by claude-danger-guard: the PowerShell tool is not guarded "
        "— use the Bash tool instead.",
        "",
        'To stop Claude trying PowerShell, set "CLAUDE_CODE_USE_POWERSHELL_TOOL": '
        '"0" under "env" in ~/.claude/settings.json.',
        "",
    ]
    lines += _closing_lines(owner_name)
    return "\n".join(lines) + "\n"


# --- shapes that are never legitimate here -----------------------------------
#
# Each rule's regex is tested against each line of the reconstructed command
# separately, exactly as `grep -E` tested each line of `$cmd` in the zsh
# original -- a quoted token can carry an embedded newline, and testing the
# whole string (or `re.MULTILINE` over it) would let a negated class like
# `[^|]*` cross that embedded newline and misfire on prose that was never a
# pipeline (`TestDoesNotMisfire.test_a_download_pipe_split_across_a_quoted_newline_is_not_a_shape`).
# POSIX character classes are translated explicitly: `[[:space:]]` is
# `[ \t\f\v\r]` here, matching the class `grep -E` uses -- a different concern
# from `tokenize`'s own field-separating whitespace (space and tab only), so
# the characters are spelled out again rather than shared with `_WHITESPACE`.

_SP = r"[ \t\f\v\r]"          # [[:space:]]

_SUDO_RE = re.compile(r"(^|[;&|(`]|" + _SP + r")sudo" + _SP)

_CURL_WGET_RE = re.compile(
    r"(curl|wget)[^|]*\|" + _SP + r"*(sudo" + _SP + r"+)?(ba|z)?sh", re.IGNORECASE)

# `_git`/`_gitopt`/`_gitval` are composed exactly as the zsh original builds
# them, so the two lists cannot drift apart. Every git global
# option here is MEASURED against real git, not copied from its usage line --
# see docs/decisions.md and the zsh original for the incidents that found
# `--no-literal-pathspecs` (accepted) versus `--noliteral-pathspecs` (rejected)
# and the missing `--shallow-file`. A value may be quoted and contain spaces,
# so `_gitval` is a run of bare characters and quoted chunks, not `[^\s]+`.
_gitval = r'''([^ \t\f\v\r"']|"[^"]*"|'[^']*')+'''
_gitopt = (
    r"((-[cC]|--(git-dir|work-tree|namespace|exec-path|config-env|attr-source|"
    r"shallow-file))(" + _SP + r"+|=)" + _gitval + r"|-[pP]|--(paginate|no-pager|"
    r"bare|exec-path|html-path|man-path|info-path|no-replace-objects|"
    r"no-optional-locks|no-lazy-fetch|no-advice|literal-pathspecs|"
    r"no-literal-pathspecs|glob-pathspecs|noglob-pathspecs|icase-pathspecs))"
)
_q = r'''['"]'''
_git = _q + r"?git" + _q + r"?(" + _SP + r"+" + _gitopt + r")*"

# `--force-with-lease` is deliberately NOT matched (the `[^-]` after
# `--force` excludes it); `-f` is matched as a bundle (`git push -fd origin x`
# really force-deletes); the leading-`+` refspec form is a force push wearing
# a disguise; `[$]?` (written here as `\$?`) catches zsh's `$'...'` quoting,
# which a raw grep never expands.
_FORCE_PUSH_RE = re.compile(
    _git + _SP + r"+push[^;&|]*(--force([^-]|$)|" +
    _SP + r"-[a-zA-Z]*f[a-zA-Z]*(" + _SP + r"|$)|" +
    _SP + r"\+[A-Za-z0-9_][A-Za-z0-9_./-]*(:|" + _SP + r"|$)|" +
    _SP + r"\$?" + _q + r"?\+:" + _q + r"?(" + _SP + r"|$))")

_HISTORY_REWRITE_RE = re.compile(
    _git + _SP + r"+(filter-branch|push[^;&|]*--mirror)")

# Matches ONLY the refspec spelling of a remote-branch delete (an empty
# source side), deliberately NOT `--delete`/`-d` -- see docs/decisions.md and
# the zsh original for why that asymmetry is a decision, not
# a gap. The destination must begin with a non-quote character, so a
# matching push (`git push origin ':'`) is not caught.
_EMPTY_SRC_REFSPEC_RE = re.compile(
    _git + _SP + r"+push[^;&|]*" + _SP + r"\$?" + _q + r"?\+?:"
    r"""[^ \t\f\v\r'"][^ \t\f\v\r]*""")

_CHMOD_RE = re.compile(r"chmod" + _SP + r"+(-[a-zA-Z]+" + _SP + r"+)*(777|a\+w)")

_RAW_DISK_RE = re.compile(
    r"(^|[;&|(`]|" + _SP + r")(mkfs|fdisk)(" + _SP + r"|$)|diskutil" + _SP +
    r"+(erase|partition)|(^|[;&|(`]|" + _SP + r")dd" + _SP + r"+[^|]*of=/dev/")

_CREDENTIALS_RE = re.compile(
    r"(\.ssh/(id_|.*key)|\.aws/credentials|\.gnupg/|\.netrc|\.credentials\.json)")

# CR-3: today's regexes, kept verbatim as the ORACLE every linear matcher
# below must agree with -- never applied to a hook's input (Python's `re`
# backtracks; `grep -E` does not, and several of these are exponential on an
# adversarial line, e.g. a repeated `git --exec-path` fragment). Aligned
# 1:1 with _TEXT_SHAPE_RULES below; used only by tests/test_rule_linearity.py
# and by the small subset of rules (sudo, chmod, mkfs/fdisk/diskutil) whose
# regex is already linear and so is wrapped rather than reimplemented.
_REFERENCE_PATTERNS = (
    _SUDO_RE, _CURL_WGET_RE, _FORCE_PUSH_RE, _HISTORY_REWRITE_RE,
    _EMPTY_SRC_REFSPEC_RE, _CHMOD_RE, _RAW_DISK_RE, _CREDENTIALS_RE,
)


# --- CR-3: linear matchers ---------------------------------------------------
#
# Each function below returns EXACTLY `bool(_REFERENCE_PATTERNS[i].search(line))`
# for every line, in time linear in `len(line)` -- verified by the random
# agreement test in tests/test_rule_linearity.py, which is the arbiter: a
# disagreement is a bug in the matcher below, never in the reference regex.
#
# sudo, chmod and the mkfs/fdisk/diskutil half of "raw disk" are already
# linear (no ambiguous nested quantifier, no unbounded class that can
# straddle another unbounded class) and are wrapped rather than
# reimplemented -- they are still covered by the tests below.

_SPACE_CHARS = " \t\f\v\r"           # _SP, as a plain string for `in` tests
_QUOTE_CHARS = "'\""                 # _q
_BOUNDARY_CHARS = ";&|"              # what `[^;&|]*` (the git rules) excludes
_ASCII_LETTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")
_ASCII_WORD_FIRST = _ASCII_LETTERS | frozenset("0123456789_")
_ASCII_WORD_REST = _ASCII_WORD_FIRST | frozenset("./-")


def _sudo_matches(line):
    return _SUDO_RE.search(line) is not None


def _chmod_run_end(line, i, chars):
    """Index just past the run of `chars` starting at `i` (== i if none)."""
    n = len(line)
    while i < n and line[i] in chars:
        i += 1
    return i


_ASCII_LETTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def _chmod_matches(line):
    """`chmod _SP+ (-[a-zA-Z]+ _SP+)* (777|a\\+w)` in linear time.

    The regex went quadratic on overlapping starts (`chmod `
    + a long run of `-chmod ` ran far past the hook's timeout), because every
    embedded `chmod` re-walked the rest of the option run. The walk is
    deterministic -- each letter run must be maximal (only whitespace may
    follow it) and so must each whitespace run (no alternative starts with
    whitespace) -- so from any position there is exactly one path, and
    `outcome` memoises every position a path visits: each is walked once."""
    n = len(line)
    outcome = {}

    def mode_follows(p):
        path = []
        result = False
        while True:
            if p in outcome:
                result = outcome[p]
                break
            path.append(p)
            if line.startswith("777", p) or line.startswith("a+w", p):
                result = True
                break
            if p < n and line[p] == "-":
                q = _chmod_run_end(line, p + 1, _ASCII_LETTERS)
                if q > p + 1 and q < n and line[q] in _SPACE_CHARS:
                    p = _chmod_run_end(line, q, _SPACE_CHARS)
                    continue
            result = False
            break
        for visited in path:
            outcome[visited] = result
        return result

    start = line.find("chmod")
    while start != -1:
        p = start + len("chmod")
        if p < n and line[p] in _SPACE_CHARS:
            if mode_follows(_chmod_run_end(line, p, _SPACE_CHARS)):
                return True
        start = line.find("chmod", start + 1)
    return False


# --- curl/wget piped to a shell ---------------------------------------------
#
# `(curl|wget)[^|]*\|` means the pipe is the FIRST `|` after the download
# word: `[^|]*` cannot cross one, so for a given curl/wget occurrence the
# only reachable pipe is the very next one. Splitting the (lower-cased, for
# the original's re.IGNORECASE) line on literal `|` turns that into a plain
# adjacent-segment test.
def _curl_pipe_tail_matches(seg):
    """`_SP* (sudo _SP+)? (ba|z)? sh`, anchored at the start of `seg`
    (already lower-cased). Each optional piece is tried greedily and,
    since none of these pieces can overlap another's own spelling, that
    greedy choice is the only one that can ever succeed -- no backtracking
    needed."""
    n = len(seg)
    i = 0
    while i < n and seg[i] in _SPACE_CHARS:
        i += 1
    if seg[i:i + 4] == "sudo":
        j = i + 4
        sp_end = j
        while sp_end < n and seg[sp_end] in _SPACE_CHARS:
            sp_end += 1
        if sp_end > j:
            i = sp_end
    if seg[i:i + 2] == "ba":
        i += 2
    elif seg[i:i + 1] == "z":
        i += 1
    return seg[i:i + 2] == "sh"


def _curl_wget_matches(line):
    lower = line.lower()
    segments = lower.split("|")
    for i in range(len(segments) - 1):
        if ("curl" in segments[i] or "wget" in segments[i]) and \
                _curl_pipe_tail_matches(segments[i + 1]):
            return True
    return False


# --- raw disk: the "dd ... of=/dev/" alternative ----------------------------
#
# `[^|]*` cannot cross a literal `|`, so split on it. Within one
# pipe-bounded segment the FIRST valid `dd` trigger dominates every later
# one there: a later trigger's remaining search window is always a subset
# of an earlier one's (both end at the same segment boundary), so if
# `of=/dev/` is not in the larger window it cannot be in the smaller one.
_DD_LEAD_CONTEXT = ";&|(`"      # [;&|(`]


def _dd_of_dev_matches(line):
    n = len(line)
    seg_start = 0
    while seg_start <= n:
        seg_end = line.find("|", seg_start)
        if seg_end == -1:
            seg_end = n
        idx = seg_start
        while True:
            d = line.find("dd", idx, seg_end)
            if d == -1:
                break
            leading_ok = (d == 0 or line[d - 1] in _SPACE_CHARS
                         or line[d - 1] in _DD_LEAD_CONTEXT)
            if leading_ok:
                after = d + 2
                sp_end = after
                while sp_end < seg_end and line[sp_end] in _SPACE_CHARS:
                    sp_end += 1
                if sp_end > after:
                    if "of=/dev/" in line[sp_end:seg_end]:
                        return True
                    break  # earliest valid trigger tested; later ones only shrink the window
            idx = d + 1
        if seg_end == n:
            break
        seg_start = seg_end + 1
    return False


_RAW_DISK_SIMPLE_RE = re.compile(
    r"(^|[;&|(`]|" + _SP + r")(mkfs|fdisk)(" + _SP + r"|$)|diskutil" + _SP +
    r"+(erase|partition)")


def _raw_disk_matches(line):
    if _RAW_DISK_SIMPLE_RE.search(line) is not None:
        return True
    return _dd_of_dev_matches(line)


# --- credentials -------------------------------------------------------------
#
# `\.ssh/(id_|.*key)`: the `.*key` half is unbounded and, since it only
# needs to find `key` ANYWHERE afterward, the FIRST `.ssh/` occurrence's
# remaining suffix is a superset of every later occurrence's -- so it alone
# decides that half. The `id_` half has no wildcard before it at all (it
# must sit immediately after ITS OWN `.ssh/`), so every occurrence needs its
# own O(1) check; that adds no risk since each check is bounded.
def _credentials_matches(line):
    if (".aws/credentials" in line or ".gnupg/" in line or ".netrc" in line
            or ".credentials.json" in line):
        return True
    idx = line.find(".ssh/")
    if idx == -1:
        return False
    if "key" in line[idx + 5:]:
        return True
    pos = idx
    while pos != -1:
        if line.startswith("id_", pos + 5):
            return True
        pos = line.find(".ssh/", pos + 1)
    return False


# --- the git rules: force push, history rewrite, empty-source refspec ------
#
# `_git`/`_gitopt`/`_gitval` are shared by all three reference regexes, so
# the reachability graph below is shared too: the option list is walked as
# a graph over string positions with a VISITED SET, so each position is
# expanded once rather than backtracking over every partition of the option
# list -- the measured exponential blowup this replaces (`--exec-path` is
# in BOTH `_gitopt` alternatives, so a failing match used to try every
# flag/value partition). Only an option end followed by whitespace can
# continue (every continuation starts with `_SP+`), and `_gitval`'s greedy
# end is the only such end (a quote character is excluded from the bare-char
# alternative, so an unterminated quote simply ends the scan rather than
# branching), so one match per alternative suffices.
_VALUE_GIT_OPTS = ("-c", "-C", "--git-dir", "--work-tree", "--namespace",
                   "--exec-path", "--config-env", "--attr-source",
                   "--shallow-file")
_FLAG_GIT_OPTS = ("-p", "-P", "--paginate", "--no-pager", "--bare",
                  "--exec-path", "--html-path", "--man-path", "--info-path",
                  "--no-replace-objects", "--no-optional-locks",
                  "--no-lazy-fetch", "--no-advice", "--literal-pathspecs",
                  "--no-literal-pathspecs", "--glob-pathspecs",
                  "--noglob-pathspecs", "--icase-pathspecs")


def _gitval_end(line, i):
    """Greedy end of `_gitval` starting at `i`, or None if zero characters
    could be consumed. A single forward scan -- no backtracking, since a
    quote is excluded from the bare-char alternative, so hitting one either
    starts a quoted chunk (find the matching close) or, if unterminated,
    simply ends the scan."""
    n = len(line)
    j = i
    while j < n:
        ch = line[j]
        if ch in _SPACE_CHARS:
            break
        if ch in _QUOTE_CHARS:
            end = line.find(ch, j + 1)
            if end == -1:
                break
            j = end + 1
            continue
        j += 1
    return j if j > i else None


def _git_option_ends(line, pos):
    """Every end position reachable by matching exactly one `_gitopt`
    alternative anchored at `pos`. `--exec-path` is in BOTH option shapes,
    so up to two edges leave one position; every other name is single-shaped."""
    ends = []
    n = len(line)
    for name in _VALUE_GIT_OPTS:
        if line.startswith(name, pos):
            after = pos + len(name)
            if after < n and line[after] == "=":
                end = _gitval_end(line, after + 1)
                if end is not None:
                    ends.append(end)
            else:
                sp_end = after
                while sp_end < n and line[sp_end] in _SPACE_CHARS:
                    sp_end += 1
                if sp_end > after:
                    end = _gitval_end(line, sp_end)
                    if end is not None:
                        ends.append(end)
    for name in _FLAG_GIT_OPTS:
        if line.startswith(name, pos):
            ends.append(pos + len(name))
    return ends


def _git_heads(line):
    """Every position right after `git`/'git'/"git" for every occurrence of
    the substring "git" -- a leading optional quote never changes what is
    reachable AFTER the match (it only shifts where the match nominally
    started), so only the trailing optional quote matters here."""
    heads = set()
    start = 0
    while True:
        j = line.find("git", start)
        if j == -1:
            break
        heads.add(j + 3)
        if j + 3 < len(line) and line[j + 3] in _QUOTE_CHARS:
            heads.add(j + 4)
        start = j + 1
    return heads


def _git_reachable(line):
    """Every position reachable from some git head by zero or more
    `_SP+ _gitopt` steps, visited once each."""
    visited = set()
    stack = list(_git_heads(line))
    n = len(line)
    while stack:
        p = stack.pop()
        if p in visited:
            continue
        visited.add(p)
        sp_end = p
        while sp_end < n and line[sp_end] in _SPACE_CHARS:
            sp_end += 1
        if sp_end == p:
            continue
        for end in _git_option_ends(line, sp_end):
            if end not in visited:
                stack.append(end)
    return visited


def _sp_plus_literal_end(line, pos, literal):
    """Does `_SP+` then `literal` match anchored at `pos`? Returns the end
    position (right after `literal`), or None."""
    n = len(line)
    sp_end = pos
    while sp_end < n and line[sp_end] in _SPACE_CHARS:
        sp_end += 1
    if sp_end == pos:
        return None
    if line.startswith(literal, sp_end):
        return sp_end + len(literal)
    return None


def _run_boundaries(line):
    """Sorted positions of every `;`, `&` or `|` in `line` -- what
    `[^;&|]*` cannot cross, splitting the line into "runs" for the git
    rules' tails."""
    return [i for i, ch in enumerate(line) if ch in _BOUNDARY_CHARS]


def _run_end_at(line, boundaries, pos):
    lo, hi = 0, len(boundaries)
    while lo < hi:
        mid = (lo + hi) // 2
        if boundaries[mid] < pos:
            lo = mid + 1
        else:
            hi = mid
    return boundaries[lo] if lo < len(boundaries) else len(line)


def _earliest_per_run(line, boundaries, ends):
    """Group `ends` (each a push_end position) by the run it falls in,
    keeping only the earliest per run -- see the force-push tail comment
    below for why the earliest always dominates."""
    by_run = {}
    for e in ends:
        run_end = _run_end_at(line, boundaries, e)
        if run_end not in by_run or e < by_run[run_end]:
            by_run[run_end] = e
    return by_run


def _force_push_tail_matches(line, push_end, run_end):
    """`(--force([^-]|$) | _SP-[a-zA-Z]*f[a-zA-Z]*(_SP|$) |
    _SP\\+[A-Za-z0-9_][A-Za-z0-9_./-]*(:|_SP|$) |
    _SP\\$?_q?\\+:_q?(_SP|$))`, reachable from `push_end` through
    `[^;&|]*` bounded by `run_end` (the first `;`/`&`/`|` at or after
    `push_end`, or the end of the line).

    ONLY the first alternative (`--force`) can extend past `run_end`: its
    trailing class is `[^-]`, which happily matches the boundary character
    itself (";" is not "-") -- `[^;&|]*` cannot consume that character, but
    `--force`'s OWN trailing class can examine it right where `[^;&|]*`
    left off. The other three alternatives all require a LITERAL leading
    space (never a bare `+`), so their own start position must itself be a
    real space character inside the run -- a boundary character is never a
    space, so those three can never begin at or past `run_end`, and their
    own trailing classes (letters, `[A-Za-z0-9_./-]`, `:"`/space/end) do
    not include `;`, `&` or `|` either, so they cannot straddle it."""
    n = len(line)
    idx = push_end
    while True:
        f = line.find("--force", idx, run_end)
        if f == -1:
            break
        after = f + 7
        if after >= n or line[after] != "-":
            return True
        idx = f + 1

    for s in range(push_end, run_end):
        if line[s] not in _SPACE_CHARS:
            continue
        # -[a-zA-Z]*f[a-zA-Z]*(_SP|$)
        if s + 1 < n and line[s + 1] == "-":
            j = s + 2
            has_f = False
            while j < n and line[j] in _ASCII_LETTERS:
                if line[j] == "f":
                    has_f = True
                j += 1
            if has_f and (j >= n or line[j] in _SPACE_CHARS):
                return True
        # \+[A-Za-z0-9_][A-Za-z0-9_./-]*(:|_SP|$)
        if s + 1 < n and line[s + 1] == "+":
            j = s + 2
            if j < n and line[j] in _ASCII_WORD_FIRST:
                j += 1
                while j < n and line[j] in _ASCII_WORD_REST:
                    j += 1
                if j >= n or line[j] == ":" or line[j] in _SPACE_CHARS:
                    return True
        # \$?_q?\+:_q?(_SP|$)
        j = s + 1
        if j < n and line[j] == "$":
            j += 1
        if j < n and line[j] in _QUOTE_CHARS:
            j += 1
        if line.startswith("+:", j):
            j += 2
            if j < n and line[j] in _QUOTE_CHARS:
                j += 1
            if j >= n or line[j] in _SPACE_CHARS:
                return True
    return False


def _empty_src_refspec_tail_matches(line, push_end, run_end):
    """`_SP\\$?_q?\\+?:[^ \\t\\f\\v\\r'"][^ \\t\\f\\v\\r]*`, reachable from
    `push_end` through `[^;&|]*` bounded by `run_end`. The destination's
    OWN char classes exclude only whitespace (not `;&|`), so unlike the
    force-push tail, existence needs nothing past the mandatory leading
    space and the one required destination character -- the rest of the
    destination is optional (`*`) and adds no new requirement."""
    n = len(line)
    for s in range(push_end, run_end):
        if line[s] not in _SPACE_CHARS:
            continue
        j = s + 1
        if j < n and line[j] == "$":
            j += 1
        if j < n and line[j] in _QUOTE_CHARS:
            j += 1
        if j < n and line[j] == "+":
            j += 1
        if j < n and line[j] == ":":
            d = j + 1
            if d < n and line[d] not in _SPACE_CHARS and line[d] not in _QUOTE_CHARS:
                return True
    return False


def _force_push_matches(line):
    reachable = _git_reachable(line)
    push_ends = set()
    for p in reachable:
        e = _sp_plus_literal_end(line, p, "push")
        if e is not None:
            push_ends.add(e)
    if not push_ends:
        return False
    boundaries = _run_boundaries(line)
    for run_end, push_end in _earliest_per_run(line, boundaries, push_ends).items():
        if _force_push_tail_matches(line, push_end, run_end):
            return True
    return False


def _history_rewrite_matches(line):
    reachable = _git_reachable(line)
    push_ends = set()
    for p in reachable:
        if _sp_plus_literal_end(line, p, "filter-branch") is not None:
            return True
        e = _sp_plus_literal_end(line, p, "push")
        if e is not None:
            push_ends.add(e)
    if not push_ends:
        return False
    boundaries = _run_boundaries(line)
    for run_end, push_end in _earliest_per_run(line, boundaries, push_ends).items():
        if "--mirror" in line[push_end:run_end]:
            return True
    return False


def _empty_src_refspec_matches(line):
    reachable = _git_reachable(line)
    push_ends = set()
    for p in reachable:
        e = _sp_plus_literal_end(line, p, "push")
        if e is not None:
            push_ends.add(e)
    if not push_ends:
        return False
    boundaries = _run_boundaries(line)
    for run_end, push_end in _earliest_per_run(line, boundaries, push_ends).items():
        if _empty_src_refspec_tail_matches(line, push_end, run_end):
            return True
    return False


# Order matches the zsh original exactly: sudo, a download
# piped to a shell, force push, history rewrite/mirror, an empty-source
# refspec delete, a world-writable chmod, a raw disk write, then credential
# material.
_TEXT_SHAPE_RULES = (
    (_sudo_matches,
     "sudo — this workspace never needs root, and root is where mistakes "
     "stop being recoverable."),
    (_curl_wget_matches,
     "piping a download straight into a shell — the code is never "
     "reviewed before it runs."),
    (_force_push_matches,
     "force push — it discards remote commits irrecoverably. Matches "
     "--force, -f including bundled forms like -fd, and the leading-+ "
     "refspec forms (git push origin +main, git push origin +:). Use "
     "--force-with-lease, which refuses when the remote moved."),
    (_history_rewrite_matches,
     "history rewrite / mirror push — it rewrites published history "
     "for every clone."),
    (_empty_src_refspec_matches,
     "empty-source refspec — this DELETES that branch from the remote, "
     "which is what an empty source side means to git; it is the same "
     "operation as 'git push --delete <branch>' with nothing in it that "
     "says so. For a merged branch that is harmless cleanup; for an "
     "unmerged one the remote held the only published copy. Write it as "
     "--delete, which prompts and says what it does, or delete it in the "
     "GitHub UI, which shows you which case you are in."),
    (_chmod_matches,
     "world-writable chmod — any local process could then modify "
     "these files."),
    (_raw_disk_matches,
     "raw disk write — this operates below the filesystem and past "
     "any backup granularity."),
    (_credentials_matches,
     "touching credential material — reading or moving keys is never "
     "part of ordinary work here."),
)


# --- rm, find and cd: judged by where they point, not by their flags --------
#
# Ports the zsh claude-danger-guard's rm/find/cd judging. `rm -rf build` is routine;
# `rm -rf ~/Documents` is not. Only the resolved target tells them apart,
# which is precisely what a permission rule cannot see. On macOS/Linux
# (ctx.windows False) every comparison below is a plain string comparison,
# exactly as the original zsh ran; `canonical()` is what makes the
# same comparisons correct on Windows, where one directory can be spelled
# `C:\Users\me`, `C:/Users/me` or `/c/Users/me` (Git Bash's mount form) in
# a single command.


class _Blocked(Exception):
    """Raised by `judge_target`/`judge_delete` to unwind the walk the moment
    a delete is refused. The original's `block` calls `exit` in the CURRENT
    shell so the refusal is real rather than confined to a subshell; this is
    the same property reached a different way -- the exception propagates
    out of `_walk_rm_find_cd` to the one `except` in `evaluate`, rather than
    a subcommand's own local failure being swallowed."""

    def __init__(self, rule_message):
        super().__init__(rule_message)
        self.rule_message = rule_message


_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_WINDOWS_ROOT_LETTER_RE = re.compile(r"^/([A-Za-z])($|/.*)$")
_WINDOWS_BARE_DRIVE_RE = re.compile(r"^[a-z]:/?$")


def canonical(path, windows):
    """Fold every spelling of the same Windows path onto one comparable
    form. The identity function when `windows` is False, so nothing on
    macOS/Linux changes shape at all -- no code path there differs from
    the zsh original's.

    Backslashes become `/`. `X:` or `X:/...` is recognised as a drive path
    as-is; a leading `/x/...` or a bare `/x` (Git Bash's mount form for
    drive `x`, exactly one ASCII letter) is rewritten to the same `x:/...`
    shape -- this is the one place a Bash tool running on Windows and its
    own guard could otherwise disagree about whether two spellings name the
    same place. The whole result is then case-folded: Windows paths are
    case-insensitive, and a config or a command can spell the same
    directory either way (a real blocks case: `rm -rf /C/USERS/ME` against
    a lowercase `home`).

    A trailing `/` is trimmed -- it breaks the prefix match
    `inside_safe_root` runs -- EXCEPT that `/` stays `/` and a bare drive
    root stays `x:/`: both are already maximally short, and trimming either
    to `""` would make `resolve` read a root delete as relative to the cwd
    instead of anchored at the filesystem root, turning a root delete into
    an allow.

    A form this does not recognise (a UNC share, say) is left alone but
    still case-folded -- it will not match any safe root or protected path,
    so it blocks by falling outside every configured root, never by being
    mistaken for one."""
    if not windows:
        return path
    p = path.replace("\\", "/")
    if not _WINDOWS_DRIVE_RE.match(p):
        m = _WINDOWS_ROOT_LETTER_RE.match(p)
        if m:
            p = m.group(1) + ":" + m.group(2)
    p = p.lower()
    if p == "/":
        return p
    if _WINDOWS_BARE_DRIVE_RE.match(p):
        return p[:2] + "/"
    if p.endswith("/"):
        p = p.rstrip("/")
    return p


def resolve(p, base, home, windows=False):
    """Collapse `.`/`..` textually and anchor a relative path at `base`. The
    path may not exist yet (rm of a stale artifact), so `os.path.realpath`
    is not an option.

    `p` is canonicalised here (a no-op when `windows` is False); `base` and
    `home` are expected to be canonical ALREADY -- every caller passes a
    `cwd`/cd-base/`ctx.home` canonicalised once, at the point it entered
    `evaluate`, rather than re-canonicalising it on every call. A path is
    absolute when it starts with `/` (true either way) or, when `windows`,
    matches a canonical drive prefix (`^[a-z]:`) -- so a Windows absolute
    path is never joined onto `base`, and the segment walk below keeps a
    leading drive letter out of the segment list so `..` can never walk
    past it.

    LINEAR IN SEGMENT COUNT, and that bound is a security property, not
    tidiness -- see the original's comment at this function (zsh lines
    536-551) for the measurement: an O(segments^2) `..` handling, multiplied
    by up to CD_BASE_LIMIT bases per delete target, can run past this
    hook's timeout -- a false ALLOW reachable from
    ONE argument, because a killed hook returns no verdict and the command
    runs unguarded. Building the segment list with `list.append` and
    collapsing `..` with `list.pop()` is O(1) amortized per segment, so the
    whole walk stays O(segments) -- no rebuilding a slice on every `..` the
    way a naive port of the zsh array truncation would."""
    # Expand BEFORE canonicalising: canonical() case-folds, and a folded
    # `$HOME` reads `$home`, which the substitution below would never match.
    if p.startswith("~"):
        p = home + p[1:]
    p = p.replace("$HOME", home)
    p = canonical(p, windows)
    is_windows_absolute = windows and _WINDOWS_DRIVE_RE.match(p)
    if not p.startswith("/") and not is_windows_absolute:
        p = base + "/" + p
    # CR-2: a UNC path's leading "//" must never collapse to a single "/".
    # The segment walk below drops every empty segment, which is exactly
    # how an ordinary "//" inside a path collapses -- applying that
    # uniformly turned "//tmp/share/x" into "/tmp/share/x", landing right
    # inside the POSIX safe root "/tmp". Only on Windows: `canonical`
    # already keeps "//" as a no-op when `windows` is False, and a POSIX
    # "//" collapsing to "/" is the zsh original's own behaviour, unchanged
    # here (test_guard.py pins the ALLOW that depends on it). Stripping
    # exactly one of the two leading slashes before splitting leaves one
    # ordinary leading "/" for the walk below (so an empty first segment is
    # dropped the usual way); re-attaching "//" afterward, rather than "/",
    # is what a UNC path can never equal or fall under a POSIX-shaped root.
    is_unc = windows and p.startswith("//")
    if is_unc:
        p = p[1:]
    segs = p.split("/")
    drive = ""
    if windows and segs and _WINDOWS_DRIVE_RE.match(segs[0]):
        drive = segs[0]
        segs = segs[1:]
    out = []
    for seg in segs:
        if seg == "" or seg == ".":
            continue
        if seg == "..":
            # ".." must never walk above the UNC anchor itself, same as it
            # never walks above the filesystem root -- `out` is already
            # empty at that point, so `if out: out.pop()` leaves it empty
            # rather than popping a segment that belongs to the anchor.
            if out:
                out.pop()
            continue
        out.append(seg)
    if drive:
        return drive + "/" + "/".join(out)
    return ("//" if is_unc else "/") + "/".join(out)


def anchor(p, base, home, windows=False):
    """`resolve` without the `.`/`..` collapsing: the same `~`/`$HOME`
    expansion, `canonical` and join onto `base`. A `..` after a symlinked
    component must be applied to the link's TARGET, which only the
    filesystem knows, so the textual collapse `resolve` does has already
    lost it."""
    if p.startswith("~"):
        p = home + p[1:]
    p = p.replace("$HOME", home)
    p = canonical(p, windows)
    if not p.startswith("/") and not (windows and _WINDOWS_DRIVE_RE.match(p)):
        p = base + "/" + p
    return p


def _cached_realpath(path, cache):
    real = cache.get(path)
    if real is None:
        real = cache[path] = os.path.realpath(path)
    return real


def physical(tok, base, follow_last, wctx):
    """Where `tok` really lands once symlinks are followed, or None when that
    cannot be known here (not host-native, or the filesystem refused). The
    last component is NOT followed unless `follow_last` or it is `.`/`..`/
    empty: deleting a symlink removes the link, not its target."""
    if not wctx.host_native:
        return None
    a = anchor(tok, base, wctx.home, wctx.windows)
    head, _, last = a.rpartition("/")
    try:
        if follow_last or last in ("", ".", ".."):
            real = _cached_realpath(a, wctx.realpath_cache)
        else:
            parent = _cached_realpath(head or "/", wctx.realpath_cache)
            real = parent.rstrip("/") + "/" + last
    except (OSError, ValueError):
        return None
    return canonical(real, wctx.windows)


# Cloud-sync mounts (~/Google Drive, ~/Library/CloudStorage, Dropbox, iCloud)
# are deliberately NOT safe roots and must never be added as one: a local
# delete there propagates to the cloud and to every other machine.
_TEMP_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")


def _host_native(windows):
    """Does this Context describe the machine the guard is running on? Only
    then can a path be resolved against the filesystem: a simulated-Windows
    path on macOS names nothing here, and resolving it would be resolving a
    different path."""
    return windows == (os.name == "nt")


def _with_real_forms(paths, windows):
    """`paths` plus the canonical realpath of each entry not already present.
    Only lets a target that is physically inside a configured root be
    recognised as such; never adds a root that is not the real location of a
    configured one. A no-op when this Context is not host-native."""
    if not _host_native(windows):
        return paths
    out = list(paths)
    for path in paths:
        try:
            real = canonical(os.path.realpath(path), windows)
        except (OSError, ValueError):
            continue
        if real not in out:
            out.append(real)
    return out


def _safe_roots(ctx, config):
    home = ctx.home
    roots = [
        home + "/.claude",
        home + "/bin", home + "/.local/bin",
    ] + list(_TEMP_ROOTS) + [
        # macOS puts this interpreter's bytecode cache here rather than in a
        # __pycache__ beside the source. The repo's CLAUDE.md documents the
        # same cache and prescribes `os.utime` on the rewritten file; it
        # never mandates clearing this directory, so that is not why the
        # entry exists. What earns it: refusing `find ... -delete` here
        # fires inside mutation runs, where the only escape would be
        # DANGER_ALLOW=1 for the whole session. Narrow on purpose: this ONE
        # directory, not ~/Library/Caches, whose other contents are not
        # merely derived from source.
        home + "/Library/Caches/com.apple.python",
    ]
    if config.workspace_root:
        roots.append(config.workspace_root)
    roots.extend(config.extra_safe_roots)
    # The Windows user temp root, gated on ctx.windows so a stray non-None
    # native_temp (unreachable through context_from_environment, but not
    # otherwise enforced) never widens what a POSIX guard trusts.
    if ctx.windows and ctx.native_temp is not None:
        roots.append(ctx.native_temp)
    return _with_real_forms([canonical(root, ctx.windows) for root in roots],
                            ctx.windows)


# Directories that must never be the target itself, even though work happens
# inside them. Deleting a whole repo or a workspace root is a decision, not a
# build step. Configured repo roots come in via extra_protected_paths.
def _protected_exact(ctx, config):
    home = ctx.home
    protected = [
        "/", home,
        home + "/.claude", home + "/bin", home + "/.local/bin",
        home + "/Documents", home + "/Desktop", home + "/Downloads",
    ]
    if config.workspace_root:
        protected.append(config.workspace_root)
    protected.extend(config.extra_protected_paths)
    return _with_real_forms([canonical(path, ctx.windows) for path in protected],
                            ctx.windows)


CD_BASE_LIMIT = 16


class _CdBases:
    """Every directory a `cd`/`pushd` earlier in this command entered, in the
    order seen -- populated by `record_cd_target` as segments are walked.

    The cap bounds a product: every relative delete target is judged against
    every base, so N cds and N deletes in one command is N^2 work inside a
    hook killed at 10 seconds -- and a killed hook returns no verdict, so the
    command runs unguarded. Without the cap, a few hundred `cd x && rm -rf y`
    pairs ran far past the timeout. 16 is a value to re-measure on your own
    machine: the same pair shape grows steeply with the cap -- raising it
    re-opens the size-defeats-the-guard hole by degrees (see
    TestCdBasesAreBounded.test_the_cap_is_not_raised_without_re_measuring).

    Past the cap the list stops growing and SAYS SO via `truncated`, rather
    than silently holding fewer bases than the command has -- a truncated
    list that stayed quiet would fail OPEN, the exact failure this rule
    exists to remove. `judge_delete` turns the flag into a refusal, and only
    for a relative target."""

    def __init__(self):
        self.bases = []
        # Where an `env -C` base physically lands when that differs from its
        # lexical form. Kept apart so the cap counts DIRECTORIES entered, not
        # their spellings: `last_or` must keep returning the lexical base, and
        # a command at the limit must not tip over it on a /tmp -> /private/tmp
        # link. At most one per counted base, so it is bounded by the same cap.
        self.physical_bases = []
        self.truncated = False

    def last_or(self, default):
        return self.bases[-1] if self.bases else default

    def add(self, resolved, real=None):
        # A repeated destination adds no verdict, so dropping duplicates is
        # free; the cap is what bounds the rest.
        if resolved in self.bases:
            return
        if len(self.bases) >= CD_BASE_LIMIT:
            self.truncated = True
            return
        self.bases.append(resolved)
        if (real is not None and real != resolved
                and real not in self.bases and real not in self.physical_bases):
            self.physical_bases.append(real)


def inside_safe_root(target, exact_ok, safe_roots):
    """Is `target` under one of the safe roots? `exact_ok` also accepts a
    target that IS a safe root, which separates the two kinds of delete
    target below (a node being removed outright vs. a container something
    is removed FROM under)."""
    for root in safe_roots:
        if target.startswith(root + "/"):
            return True
        if exact_ok and target == root:
            return True
    return False


def judge_target(target, what, kind, wctx, pattern=None):
    """Judge one already-resolved path. `what` names the mechanism for the
    message; the verdict is identical however the delete is spelled.

    `kind` picks which of two questions is being asked, and they are NOT the
    same:

      node   the path is the thing being removed, as with `rm`. A safe root
             is not an acceptable answer -- deleting the workspace root is
             the case PROTECTED_EXACT exists for.
      root   the path is a directory something will delete UNDER but cannot
             delete ITSELF, because a predicate restricts what matches.
             Equality with a safe root has to pass here, or `find . -name
             '*.pyc' -delete` run from the workspace root resolves to the
             root itself and refuses the most ordinary cleanup there is.

    Which of the two a find search root gets is decided by its predicates
    (see `inspect_find_segment`), read over the WHOLE expression -- an
    unfiltered find deletes its own starting point (verified against real
    `find`), so an unrecognised predicate leaves the search looking
    unfiltered and judges the root as a node: loud and recoverable, the
    opposite of a missing rm prefix, which is a silent ALLOW.

    `pattern` is only passed for an rm glob (see `glob_confinement`): the
    glob's own segment and every segment after it. A glob can match a
    protected path UNDER `target`, and that has to refuse before a safe-root
    acceptance, for both kinds."""
    if pattern is not None:
        for p in wctx.protected_exact:
            if target == "/" and p != "/":
                rel = p[1:]
            elif p.startswith(target + "/"):
                rel = p[len(target) + 1:]
            else:
                continue
            rel = rel.split("/")
            # A trailing segment of only */? empties whatever the segments
            # before it match, so `ws/repo*/*` empties the protected
            # `ws/repos` exactly as the literal `ws/repos/*` would.
            empties = set(pattern[-1]) <= {"*", "?"}
            if (len(rel) == len(pattern) or
                    (empties and len(rel) == len(pattern) - 1)) and all(
                    fnmatch.fnmatchcase(r, pat) for r, pat in zip(rel, pattern)):
                raise _Blocked(
                    "%s targeting %s — a glob that can match %s, a root of "
                    "your workspace or home." % (what, target, p))
    if kind == "node":
        for p in wctx.protected_exact:
            if target == p:
                raise _Blocked(
                    "%s targeting %s — that is a root of your workspace or "
                    "home, not a build artifact." % (what, target))
        if inside_safe_root(target, False, wctx.safe_roots):
            return
    else:
        if inside_safe_root(target, True, wctx.safe_roots):
            return

    if wctx.workspace_root:
        raise _Blocked(
            "%s targeting %s, which is outside %s and the temp "
            "directories. Deletions there aren't covered by the "
            "working-directory scoping you rely on — and inside a "
            "cloud-synced folder they would propagate to every "
            "machine." % (what, target, wctx.workspace_root))
    raise _Blocked(
        "%s targeting %s, and no workspace_root is configured — so the "
        "only trusted roots are ~/.claude, ~/bin and the temp directories. "
        "Set workspace_root in %s if deletions in this tree should be "
        "routine." % (what, target, wctx.config_path))


def _ends_in_a_separator(tok, windows):
    """A trailing `/` or `/.` makes the shell traverse a symlink it would
    otherwise treat as the node itself."""
    seps = ("/", "/.", "\\", "\\.") if windows else ("/", "/.")
    return tok.endswith(seps)


def judge_delete(tok, what, kind, wctx, cd_bases, pattern=None,
                 env_bases=None, follow_last=False):
    """Judge one delete target against the payload cwd AND every `cd` base,
    because which one the shell will actually use is not knowable from the
    text alone.

    THE DIRECTION OF THE OVER-APPROXIMATION IS THE DESIGN: `cd ~/Documents &&
    rm -rf notes` resolved against the payload cwd alone concluded it was
    inside the workspace and ALLOWED a command that really does delete
    ~/Documents/notes -- measured, and the shape is ordinary cleanup rather
    than anything adversarial. Judging against every base can only ever ADD
    a block. Only `cd`s SEEN SO FAR count -- applying a later one would
    block `rm -rf build && cd ~/Documents`, where the delete provably ran in
    the original directory.

    `env_bases` is the same idea for one segment's own `env -C DIR` options
    (see `_command_word`): judged in addition to everything above, and its
    `truncated` flag refuses a relative target exactly as `cd_bases` does.

    Every lexical judgement is followed by a PHYSICAL one -- the same target
    with symlinks followed (see `physical`) -- and either blocks. That can
    only ADD a block; the one allow it restores is a configured root that is
    itself reached through a symlink (`_with_real_forms`). `follow_last` says
    the final component is traversed rather than removed (an rm glob's
    prefix, `find -H`/`-L`); a trailing `/` or `/.` says so by itself."""
    # A `${...}` expansion is refused outright, like `$(...)`: its value is
    # not in the text. Below, an unresolved variable falls back to the
    # payload cwd, and `${HOME}/Documents` joined onto a workspace cwd read
    # as a workspace delete -- `rm -rf ${HOME}/Documents` was ALLOWED while
    # `$HOME/Documents` blocked, and so was `rm -rf ${X:-/}`. Every `${...}`
    # is refused rather than `${HOME}` alone being expanded, because the
    # other forms (`${X:-/}`, `${X%/*}`) share the hole and this guard does
    # not parse parameter expansion. Accepted costs: `rm -rf "${TMPDIR}/x"`
    # is refused, and so is a quoted literal `'${HOME}/x'` (quote state is
    # not tracked, as with `'$(x)'` in `_judge_rm_word`). A bare `$T` still
    # falls back, so the `rm -rf "$T"` cleanup idiom below keeps working.
    # Here, not in `_judge_rm_word`, so rm targets and find roots both pass
    # through it. An rm glob holding `${` never gets this far:
    # `glob_confinement` refuses any token with a `{` first, and this check
    # is the backstop should that rule ever narrow.
    if "${" in tok:
        raise _Blocked(
            "%s on a target holding a ${...} expansion — its value isn't in "
            "the command, so where it lands can't be determined before it "
            "runs. Write the path out, or use $VAR without braces." % what)
    has_variable = "$" in tok.replace("$HOME", "")
    follow_last = follow_last or _ends_in_a_separator(tok, wctx.windows)

    def judge_at(base):
        resolved = resolve(tok, base, wctx.home, wctx.windows)
        judge_target(resolved, what, kind, wctx, pattern=pattern)
        # An unresolved variable has no knowable value, physical or not.
        if has_variable:
            return
        real = physical(tok, base, follow_last, wctx)
        if real is not None and real != resolved:
            judge_target(real, what, kind, wctx, pattern=pattern)

    judge_at(wctx.cwd)

    # An absolute target ignores the bases entirely, so skip the loop rather
    # than judging the identical path once per cd base.
    if tok.startswith("/") or tok.startswith("~") or tok.startswith("$HOME"):
        return

    # A target carrying an unresolved variable is not a relative path -- it
    # is an UNKNOWN one, and no base makes it knowable. Judging it against
    # the bases prefixes a base onto a literal `$T`, which produced false
    # BLOCKs against real transcripts, every one of them the same shape:
    #
    #   T=$(mktemp -d); cd "$T" && …; cd /; rm -rf "$T"
    #
    # The trailing `cd /` supplies `/` as a base, `$T` resolves to `/$T`,
    # and the guard refused a delete of a temp directory -- that idiom is
    # how nearly every verification script in this repo cleans up after
    # itself. Skipping the bases returns these to exactly what the payload
    # cwd said before this rule existed: no protection lost, since `cd
    # ~/Documents && rm -rf $X` was allowed then and is allowed now.
    if "$" in tok.replace("$HOME", ""):
        return

    if cd_bases.truncated or (env_bases is not None and env_bases.truncated):
        raise _Blocked(
            "%s on a relative path, in a command that enters more than %d "
            "directories — where it would actually land can't be worked "
            "out, so this is refused rather than guessed at. Use an "
            "absolute path, or put the sequence in a script."
            % (what, CD_BASE_LIMIT))

    all_bases = list(cd_bases.bases)
    if env_bases is not None:
        all_bases.extend(env_bases.bases)
        all_bases.extend(env_bases.physical_bases)
    for base in all_bases:
        judge_at(base)


# Reserved words and command prefixes that may sit in front of the command
# itself. `for d in *; do rm -rf ~/Documents; done` splits on `;`, so the
# segment starts with `do` -- and requiring `rm` to be the FIRST token meant
# the whole segment was skipped and the delete was never judged at all.
# Measured ALLOW for `do`, `then`, `else` and a `while` loop.
#
# The list is deliberately CLOSED. Skipping unknown words instead would make
# `echo rm -rf /` look like a delete, which is a false BLOCK on the most
# ordinary command there is, so anything not named here ends the search.
RM_PREFIXES = frozenset((
    "do", "then", "else", "elif", "!",              # reserved words before a command
    "time", "command", "builtin", "exec", "nohup",  # prefixes that take a command
))

# A digit-led redirect operator ("2>", "2>>", ...) optionally followed by a
# trailing "&"/"|" ("2>&", ">|"). Only `<`/`>`-led operators ever take a
# leading digit -- the `&`-led forms ("&>", "&>>"/">>&") never do, so they
# are matched separately below, exactly as the original's two-part check.
_REDIRECT_OP_RE = re.compile(r"^[0-9]*(<<<|<<-?|>>|>|<)[&|]?$")


def glob_confinement(token, windows):
    """(prefix, kind, pattern) for an unquoted rm argument containing * or ?,
    or None when no fixed directory confines it.

    A glob can only match paths under the directory written before its first
    glob character, so that directory is what gets judged. Nearly all
    genuine glob deletes aim at a temp root or the workspace, where the same
    delete without a glob is allowed. A typo such as
    `rm -f /tmp_portable_backup_*.py` has the prefix `/`, so it still
    refuses.

    A segment made only of `*` and `?` (`dir/*`, `dir/?*`, `dir/*/`) is a
    "node": it empties the directory, which is not a distinction to sell as
    safe (same reasoning as `-mindepth` in FIND_FILTER_PREDICATES). `?*`
    matches every ordinary name `*` does, so testing for `*` alone let
    `~/.claude/?*` through. Anything else is a "root".

    Refused (None) when the prefix itself cannot be pinned down: `**` (zsh,
    and bash with globstar, let it match ZERO directories, so
    `ws/**/repos` is `ws/repos` while the protected-path match counts
    segments), `{` (brace
    expansion can rewrite the prefix), `[` (a class can sit in the prefix,
    `.[.]` is `..`, and fnmatch has no POSIX classes), `$` or a leading `~`
    at or after the glob (no fixed value), `..` after the glob, or a glob
    segment that can match `..` -- macOS bash 3.2 lets `.*` match `..`, so
    `/private/tmp/.*/*` walks out of /private/tmp. The leading-`.` condition
    is required: fnmatchcase("..", "*") is True, but a shell never lets `*`
    or `?` match a leading dot."""
    if "{" in token or "[" in token or "**" in token:
        return None
    # Shell pattern syntax fnmatch does not implement: zsh `(a|b)` (on by
    # default, so it can sit in the "fixed" prefix too), extendedglob `~`
    # exclusion, `^` and `#`, `<1-10>` ranges, and bash extglob `@(...)`.
    # Only a LEADING `~` is a home directory.
    if any(ch in token for ch in "()|^#<>") or "~" in token[1:]:
        return None
    seps = "/\\" if windows else "/"
    first = min(i for i, ch in enumerate(token) if ch in "*?")
    cut = max([token.rfind(sep, 0, first) for sep in seps])
    ends = [i for i in (token.find(sep, first) for sep in seps) if i != -1]
    end = min(ends) if ends else len(token)
    seg = token[cut + 1:end]
    rest = token[end:]

    def split(text):
        for sep in seps[1:]:
            text = text.replace(sep, "/")
        return text.split("/")

    if "$" in token[cut + 1:] or seg.startswith("~"):
        return None
    if any(part == ".." for part in split(rest)):
        return None
    for part in split(seg + rest):
        if ("*" in part or "?" in part) and part.startswith(".") \
                and fnmatch.fnmatchcase("..", part):
            return None

    if cut == -1:
        prefix = "."
    elif cut == 0:
        prefix = token[:1]
    else:
        prefix = token[:cut]

    kind = "node" if (set(seg) <= {"*", "?"}
                      and not any(split(rest))) else "root"
    pattern = [part for part in split(token[cut + 1:]) if part not in ("", ".")]
    if windows:
        pattern = [part.lower() for part in pattern]
    return prefix, kind, pattern


BRACE_WORD_LIMIT = 64    # expanded words per token
BRACE_TOTAL_LIMIT = 256  # expanded words per command, across every target
# The expander looks for each `{`'s partner by scanning forward, so an
# unmatched `{` costs a scan to the end of the token: quadratic in brace
# count. Before these caps, thousands of unmatched `{` ran past this hook's
# timeout inside brace_expand alone -- and a
# killed hook returns no verdict, so `rm -rf x/{{{… ~/Documents` would run
# unjudged. A token past either cap is refused, never scanned.
BRACE_MAX_OPENERS = 32       # `{` characters per token
BRACE_MAX_TOKEN_CHARS = 4096  # length of a token that contains any `{`

_BRACE_INT_SEQ_RE = re.compile(
    r"^([+-]?[0-9]+)\.\.([+-]?[0-9]+)(?:\.\.([+-]?[0-9]+))?$")
_BRACE_LETTER_SEQ_RE = re.compile(
    r"^([A-Za-z])\.\.([A-Za-z])(?:\.\.([+-]?[0-9]+))?$")


def _skip_literal(text, k):
    """If the construct at `text[k]` makes its contents literal for brace
    purposes -- a backslash pair, single or double quotes, `$'...'`, or the
    parameter expansion `${...}` -- return the index just past it, else None.
    An unterminated quote swallows the rest of the text."""
    ch = text[k]
    n = len(text)
    if ch == "\\":
        return min(k + 2, n)
    if ch == "'":
        end = text.find("'", k + 1)
        return n if end == -1 else end + 1
    if ch == '"' or (ch == "$" and text[k + 1:k + 2] == "'"):
        quote = text[k + (1 if ch == "$" else 0)]
        j = k + (2 if ch == "$" else 1)
        while j < n:
            if text[j] == "\\":
                j += 2
            elif text[j] == quote:
                return j + 1
            else:
                j += 1
        return n
    if ch == "$" and text[k + 1:k + 2] == "{":
        depth = 0
        j = k + 1
        while j < n:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    return j + 1
            j += 1
        return n
    return None


def _brace_sequence(body, limit):
    """The words of a `{X..Y}` / `{X..Y..N}` body: [] when `body` is not a
    sequence, None when it would exceed `limit` words (counted before any
    word is built, so `{1..99999999999}` costs nothing)."""
    match = _BRACE_INT_SEQ_RE.match(body)
    if match:
        start, stop = int(match.group(1)), int(match.group(2))
        digits = [g.lstrip("+-") for g in match.groups()[:2]]
        pad = (max(len(match.group(1)), len(match.group(2)))
               if any(len(d) > 1 and d.startswith("0") for d in digits) else 0)
        render = (lambda value: "%0*d" % (pad, value)) if pad else str
    else:
        match = _BRACE_LETTER_SEQ_RE.match(body)
        if not match:
            return []
        start, stop = ord(match.group(1)), ord(match.group(2))
        render = chr
    step = abs(int(match.group(3) or 1)) or 1
    if abs(stop - start) // step + 1 > limit:
        return None
    direction = 1 if stop >= start else -1
    return [render(v) for v in range(start, stop + direction, direction * step)]


def _brace_expand(text, limit):
    """Worker for `brace_expand`; None once a result would pass `limit`."""
    n = len(text)
    k = 0
    while k < n:
        skipped = _skip_literal(text, k)
        if skipped is not None:
            k = skipped
            continue
        if text[k] != "{":
            k += 1
            continue
        # Find this brace's partner, noting the commas at its own level.
        depth = 1
        commas = []
        j = k + 1
        while j < n:
            skipped = _skip_literal(text, j)
            if skipped is not None:
                j = skipped
                continue
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            elif text[j] == "," and depth == 1:
                commas.append(j)
            j += 1
        if j >= n:
            k += 1  # unmatched: a literal `{`
            continue
        if commas:
            cuts = [k] + commas + [j]
            alternatives = [text[cuts[m] + 1:cuts[m + 1]]
                            for m in range(len(cuts) - 1)]
        else:
            alternatives = _brace_sequence(text[k + 1:j], limit)
            if alternatives is None:
                return None
            if not alternatives:
                k += 1  # `{a}` or `{}`: literal; a group inside is still found
                continue
        suffixes = _brace_expand(text[j + 1:], limit)
        if suffixes is None:
            return None
        words = []
        for alternative in alternatives:
            expanded = _brace_expand(alternative, limit)
            if expanded is None \
                    or len(words) + len(expanded) * len(suffixes) > limit:
                return None
            words.extend(text[:k] + e + x for e in expanded for x in suffixes)
        return words
    return [text]


def brace_expand(raw_token, limit):
    """Bash brace expansion of a RAW token (quotes still on it), or None when
    it would produce more than `limit` words. A token with no brace group
    comes back as `[raw_token]`. Quoted or escaped braces and `${...}` are
    literal. Callers judge the raw token as well, so this only has to be
    right about what the shell can expand it to. Also None for a token past
    BRACE_MAX_OPENERS / BRACE_MAX_TOKEN_CHARS, before any scanning."""
    if "{" not in raw_token:
        return [raw_token]
    if raw_token.count("{") > BRACE_MAX_OPENERS \
            or len(raw_token) > BRACE_MAX_TOKEN_CHARS:
        return None
    return _brace_expand(raw_token, limit)


def _expand_target(tok, wctx):
    """The brace-expanded words of one rm/find target token, [] when
    expansion changes nothing. Counted against the per-command total; raises
    `_Blocked` past either limit."""
    words = brace_expand(tok, BRACE_WORD_LIMIT)
    if words is None:
        raise _Blocked(
            "a brace expansion too large to judge — more than %d words, or too "
            "long or brace-heavy to scan, in one argument." % BRACE_WORD_LIMIT)
    if words == [tok]:
        return []
    wctx.brace_total[0] += len(words)
    if wctx.brace_total[0] > BRACE_TOTAL_LIMIT:
        raise _Blocked(
            "brace expansion too large to judge — more than %d expanded "
            "words in one command." % BRACE_TOTAL_LIMIT)
    return words


def inspect_rm_segment(tokens, first, wctx, cd_bases, env_bases=None):
    """Inspect one subcommand's tokens for an `rm`. `first` is the index of
    the command word itself; this walks every token AFTER it. Each token is
    judged as written AND as each word its brace expansion produces."""
    skip_target = False
    for tok in tokens[first + 1:]:
        # A redirect's target is not something rm deletes. The redirection
        # OPERATOR is its own token, so it and the token after it are both
        # dropped -- never the rest of the segment, so `rm -rf 2>/dev/null
        # ~/Documents` still blocks.
        if skip_target:
            skip_target = False
            continue
        if _REDIRECT_OP_RE.match(tok) or tok == "&>" or tok == "&>>":
            skip_target = True
            continue
        _judge_rm_word(tok, wctx, cd_bases, env_bases)
        for word in _expand_target(tok, wctx):
            _judge_rm_word(word, wctx, cd_bases, env_bases)


def _judge_rm_word(tok, wctx, cd_bases, env_bases):
    """Judge one non-redirect argument of an `rm`: a raw token, or one word
    of its brace expansion."""
    # Substitutions make the real targets unknowable before execution,
    # and so do globs whose directory can't be pinned down (see
    # `glob_confinement`). Checked on the RAW token, BEFORE `unquote`, on
    # purpose: `unquote` turns `$'\x2a'` into a literal `*`, and checking
    # after would refuse a filename that merely contains the ANSI-C
    # escape for an asterisk, never a glob at all.
    if "$(" in tok or "`" in tok:
        raise _Blocked(
            "rm with a command substitution — the actual targets can't "
            "be determined before it runs.")
    if "*" in tok or "?" in tok:
        unquoted = unquote(tok)
        if "*" in unquoted or "?" in unquoted:
            confined = glob_confinement(unquoted, wctx.windows)
            if confined is None:
                raise _Blocked(
                    "rm with a glob whose directory can't be pinned down "
                    "before it runs (a variable, ~, brace or .. at or "
                    "after the glob).")
            prefix, kind, pattern = confined
            # The glob expands INSIDE the prefix, so a symlinked prefix
            # is traversed.
            judge_delete(prefix, "rm", kind, wctx, cd_bases,
                         pattern=pattern, env_bases=env_bases,
                         follow_last=True)
            return

    # Both checks above run BEFORE unquote, on purpose: quoting is what
    # separates an operator from a filename (`rm -rf '>' x` names a FILE
    # called ">"), and once quotes are stripped that reads as a redirect
    # and swallows the argument after it.
    tok = unquote(tok)
    if tok == "":
        return
    if tok.startswith("-"):
        return
    judge_delete(tok, "rm", "node", wctx, cd_bases, env_bases=env_bases)


# `find <paths> ... -exec rm ...` and `find <paths> ... -delete` delete
# without ever putting `rm` at the head of a segment, so `inspect_rm_segment`
# never sees them. NOT covered, and deliberately: `sh -c '...'`, `eval` and
# `xargs rm` -- reaching those means parsing an argument that happens to be
# shell, the second-parser cost declined for the same reason at the segment
# splitter below.
FIND_EXEC_PRIMARIES = frozenset(("-exec", "-execdir", "-ok", "-okdir"))

# Predicates that restrict WHICH paths match, and therefore keep the search
# root itself out of the delete set. Deliberately excludes `-maxdepth`,
# which does not: depth 0 IS the start point, so `find <root> -maxdepth 1
# -delete` still removes root. `-mindepth` is excluded too -- it spares the
# root but still empties it, which is not a distinction this guard should
# sell as safe.
FIND_FILTER_PREDICATES = frozenset((
    "-name", "-iname", "-path", "-ipath", "-regex", "-iregex", "-lname", "-ilname",
    "-type", "-size", "-empty", "-perm", "-links", "-inum", "-samefile",
    "-mtime", "-atime", "-ctime", "-mmin", "-amin", "-cmin",
    "-newer", "-newermt", "-newerat", "-anewer", "-cnewer",
    "-user", "-group", "-uid", "-gid", "-nouser", "-nogroup",
    "-flags", "-xattrname", "-acl", "-prune",
))

_FIND_OPTION_FLAG_RE = re.compile(r"^-[HLPEXdsxf]$")


def inspect_find_segment(tokens, first, wctx, cd_bases, env_bases=None):
    """Inspect one subcommand's tokens for a `find` that deletes. What is
    judged is the SEARCH ROOT, not the individual files: which files match
    is decided at run time by predicates this guard cannot evaluate, so the
    root is the only thing knowable beforehand and it bounds the blast
    radius."""
    # Does it delete at all? `-delete` on its own, or `rm` as the command of
    # an -exec-family primary (`rm` may be invoked by path: `-exec /bin/rm
    # -rf {} +` is the same delete). `filtered` decides node-vs-root below
    # and must be read over the WHOLE expression, so this loop cannot break
    # early the way an existence test would.
    deletes = False
    filtered = False
    prev = ""
    for raw_tok in tokens[first + 1:]:
        tok = unquote(raw_tok)
        if tok == "-delete":
            deletes = True
        if prev in FIND_EXEC_PRIMARIES and (tok == "rm" or tok.endswith("/rm")):
            deletes = True
        # Only BEFORE the action: `-exec rm -rf {} \;` carries tokens of its
        # own, and none of them narrow what find matched.
        if not deletes and tok in FIND_FILTER_PREDICATES:
            filtered = True
        prev = tok
    if not deletes:
        return

    # Search roots: the operands between find's own leading option flags and
    # the first predicate. BSD find takes `-H -L -P` and the `-EXdsx`
    # cluster there.
    #
    # NO GLOB CHECK HERE, unlike rm, and the asymmetry is the point. For rm
    # the glob IS the target, so what it expands to decides the blast
    # radius. For find it is a search ROOT, and a glob expands to segments
    # inside the pattern's own fixed prefix, so resolving the pattern
    # literally is already the conservative answer.
    roots = []
    i = first + 1
    n = len(tokens)
    follow_roots = False  # -H/-L: a symlink given as a root is traversed
    while i < n and _FIND_OPTION_FLAG_RE.match(unquote(tokens[i])):
        if unquote(tokens[i]) in ("-H", "-L"):
            follow_roots = True
        i += 1
    while i < n:
        tok = unquote(tokens[i])
        if tok.startswith("-"):
            break
        roots.append(tokens[i])
        i += 1
    if not roots:
        roots = ["."]  # no operand means find defaults to the current directory

    # An unfiltered find deletes its own start point, so the root is a NODE
    # and PROTECTED_EXACT applies to it exactly as it would to `rm -rf
    # <root>`.
    kind = "root" if filtered else "node"
    # Each root is judged as written AND as each word its brace expansion
    # produces.
    for raw_root in roots:
        for word in [raw_root] + _expand_target(raw_root, wctx):
            judge_delete(unquote(word), "find, deleting under", kind, wctx,
                         cd_bases, env_bases=env_bases,
                         follow_last=follow_roots)


_CD_OPTION_RE = re.compile(r"^-[a-zA-Z@]")


def record_cd_target(tokens, first, wctx, cd_bases):
    """A `cd`/`pushd` in one segment moves where every LATER segment's
    relative paths point. `cd` with no operand is `$HOME`, which is why `cd
    && rm -rf Documents` needs it as much as the explicit form does. `cd -`
    adds no base."""
    operands = []
    cd_modes = []
    for raw_tok in tokens[first + 1:]:
        # A command substitution's text is not a path and cannot become one
        # here. Checked on the RAW token, before unquote, same reasoning as
        # the rm glob check. Past the first operand it only ends the search
        # for a second one.
        if "$(" in raw_tok or "`" in raw_tok:
            if operands:
                break
            return
        # zsh brace-expands `cd {a,b}` into the two-operand `cd a b`.
        words = brace_expand(raw_tok, BRACE_WORD_LIMIT)
        if words is None:
            raise _Blocked(
                "a brace expansion too large to judge — more than %d words, or "
                "too long or brace-heavy to scan, in one argument." % BRACE_WORD_LIMIT)
        for word in words:
            tok = unquote(word)
            if tok == "":
                continue
            if operands:
                operands.append(tok)
                break
            # `-` is cd's previous-directory operand, not an option: stop,
            # add no base.
            if tok == "-":
                return
            # Skip cd's options by SHAPE rather than by name -- listing `-L
            # -P -@` by hand let `cd -q ~/Documents` and `cd -- ~/Documents`
            # register no base at all, one token and the rule was off. A
            # token wrongly skipped here can only cost a base, so the miss
            # direction is unchanged and the hit direction is strictly
            # better.
            if tok == "--" or _CD_OPTION_RE.match(tok):
                # `-P`/`-L` in any token or cluster: bash lets the LAST of
                # them win (`cd -P -L` and `cd -PL` are logical), so record
                # every letter in order and decide after the walk.
                if tok != "--":
                    cd_modes += [ch for ch in tok if ch in "PL"]
                continue
            operands.append(tok)
        if len(operands) == 2:
            break
    operand = operands[0] if operands else ""
    if not operand:
        operand = wctx.home

    # `$HOME` and `~` are the two expansions `resolve` performs, so `cd
    # "$HOME/Documents"` is as resolvable as the bare path and must not be
    # discarded -- refusing every `$` allowed exactly that shape through.
    # Anything else still containing `$` is a variable whose value is not in
    # the text; treating it literally can only produce a base BELOW the cwd,
    # which can only ever look safer, so it is cheap defence rather than a
    # tested guarantee.
    if "$" in operand.replace("$HOME", ""):
        return

    # Relative to the base in force when this cd runs, which is the last one
    # recorded -- `cd repos && cd toolkit` lands two levels down, not one.
    base = cd_bases.last_or(wctx.cwd)
    resolved = resolve(operand, base, wctx.home, wctx.windows)
    # A plain `cd` is LOGICAL (`..` is collapsed before the chdir), so the
    # lexical base is where it lands. With any `-P` in play BOTH bases are
    # judged -- the shells disagree on precedence, and judging both can only
    # add a block -- and the one added LAST, which the next relative `cd`
    # starts from, is bash's answer: physical only when `P` came last.
    real = physical(operand, base, True, wctx) if "P" in cd_modes else None
    if real is None or real == resolved:
        cd_bases.add(resolved)
    elif cd_modes[-1] == "P":
        cd_bases.add(resolved)
        cd_bases.add(real)
    else:
        cd_bases.add(real)
        cd_bases.add(resolved)

    # zsh's two-operand `cd OLD NEW` swaps the first OLD in $PWD for NEW (bash
    # refuses two operands, so this base can only be one a bash run never
    # reaches: an over-approximation, which only adds blocks). Nothing moves
    # when OLD is not in $PWD.
    if len(operands) == 2 and operands[0] in base \
            and "$" not in operands[1].replace("$HOME", ""):
        swapped = base.replace(operands[0], operands[1], 1)
        cd_bases.add(resolve(swapped, base, wctx.home, wctx.windows))


# Separators are matched as TOKENS, not characters, so `2>&` (one token) does
# not split the command the way a bare `&` would, and `(` starts a new
# segment so `(rm -rf /)` is inspected rather than skipped as one word
# beginning with `(`.
#
# KNOWN GAP, ACCEPTED: separators are not looked for inside a quoted token,
# so `bash -c 'cd /tmp; rm -rf ~/Documents'` is one token and is never
# inspected. Closing this means recursively parsing an argument that happens
# to be shell, which is the second-parser cost declined throughout this file.
# `eval`, `xargs rm` and `env -S '…'` (env's split-string) are the same gap.
#
# `&|` and `&!` are zsh's background-and-disown terminators and `;|`/`;&` its
# case-clause fall-throughs; the tokenizer emits each as one token, so each
# must be listed here or the command after it becomes an argument of the one
# before (`true &| rm -rf ~/Documents` ran the delete while this set lacked
# them -- the zsh original had the same gap).
_SEGMENT_SEPARATORS = frozenset((
    ";", "&", "&&", "|", "||", "|&", ";;", "(", ")", "{", "}", "!",
    "&|", "&!", ";|", ";&",
))


_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _normalize_word(raw_tok, windows):
    """The command word as the shell will find it: unquoted, reduced to its
    basename (`/bin/rm` -> `rm`; a backslash also separates on Windows),
    `.exe` stripped on Windows, and lower-cased on every platform -- macOS's
    default filesystem is case-insensitive, so `RM` runs `/bin/rm`; on a
    case-sensitive Linux the only cost is judging a command literally named
    `RM`. Over-approximation direction: this can only add a block."""
    word = unquote(raw_tok)
    cut = word.rfind("/")
    if windows:
        cut = max(cut, word.rfind("\\"))
    word = word[cut + 1:]
    if windows and word.lower().endswith(".exe"):
        word = word[:-4]
    return word.lower()


# What each prefix word may carry before the command itself. Only the
# options that take no argument, plus `-a NAME` for exec, are listed.
_EXEC_FLAGS = frozenset(("-c", "-l", "-cl", "-lc"))
_ENV_FLAGS = frozenset(("-i", "-", "-0", "-v", "--ignore-environment", "--"))
_ENV_TAKES_VALUE = frozenset(("-u", "--unset", "-P"))
_JUDGED_COMMANDS = frozenset(("rm", "find", "cd", "pushd"))
# Prefix words that accept `--` to end their own options.
_DASHDASH_PREFIXES = frozenset(("exec", "command", "builtin", "time", "nohup"))


def _command_word(tokens, wctx, cd_bases):
    """(first_index, name, env_bases): the command word of one segment after
    every prefix, assignment and `env`/`exec`/`command`/`time` option has
    been skipped, normalized by `_normalize_word`. `name` is None when the
    segment executes no command this guard judges (including `command -v`,
    which is a lookup, not an execution).

    `env -C DIR` / `--chdir DIR` run the command in DIR, so DIR is recorded
    in `env_bases` -- a FRESH `_CdBases` per segment, never merged into the
    command-wide `cd_bases`, because it applies to this segment only. The cap
    is what keeps a long chain of `-C` options from becoming a base-by-target
    product that outruns the hook timeout (a killed hook allows).

    `env -S`/`--split-string` is NOT parsed: it turns a string into a command
    line, the same second-parser gap as `bash -c`."""
    env_bases = _CdBases()
    n = len(tokens)
    i = 0
    while i < n:
        raw = tokens[i]
        word = unquote(raw)
        if _ASSIGNMENT_RE.match(word):
            i += 1
            continue
        name = _normalize_word(raw, wctx.windows)
        if name == "env":
            i += 1
            while i < n:
                opt = unquote(tokens[i])
                if opt in _ENV_FLAGS or _ASSIGNMENT_RE.match(opt):
                    i += 1
                elif opt in _ENV_TAKES_VALUE:
                    i += 2
                elif opt in ("-C", "--chdir") or opt.startswith("--chdir=") \
                        or (opt.startswith("-C") and len(opt) > 2):
                    if opt in ("-C", "--chdir"):
                        dir_raw = tokens[i + 1] if i + 1 < n else ""
                        i += 2
                    else:
                        dir_raw = (opt.split("=", 1)[1]
                                   if opt.startswith("--chdir=") else opt[2:])
                        i += 1
                    # Same rules as `record_cd_target`: a substitution or a
                    # variable other than $HOME has no knowable value.
                    dir_text = unquote(dir_raw)
                    if ("$(" in dir_raw or "`" in dir_raw or not dir_text
                            or "$" in dir_text.replace("$HOME", "")):
                        continue
                    # `env -C` is a plain chdir(2): physical, so the real
                    # landing is recorded beside the lexical base.
                    env_base = cd_bases.last_or(wctx.cwd)
                    env_bases.add(
                        resolve(dir_text, env_base, wctx.home, wctx.windows),
                        physical(dir_text, env_base, True, wctx))
                elif opt.startswith("-u") or opt.startswith("--unset="):
                    i += 1
                else:
                    break
            continue
        if name in RM_PREFIXES:
            i += 1
            while i < n:
                opt = unquote(tokens[i])
                if name == "exec" and opt in _EXEC_FLAGS:
                    i += 1
                elif name == "exec" and opt == "-a":
                    i += 2
                elif name == "command" and re.match(r"^-[pvV]+$", opt):
                    if "v" in opt or "V" in opt:
                        return i, None, env_bases
                    i += 1
                elif name == "time" and opt == "-p":
                    i += 1
                elif opt == "--" and name in _DASHDASH_PREFIXES:
                    # End of the prefix's own options: `command -- rm -rf x`
                    # runs rm in bash and zsh alike (measured).
                    i += 1
                    break
                else:
                    break
            continue
        return i, (name if name in _JUDGED_COMMANDS else None), env_bases
    return n, None, env_bases


def inspect_segment(tokens, wctx, cd_bases):
    """One prefix walk, then dispatch on the command word."""
    if not tokens:
        return
    first, name, env_bases = _command_word(tokens, wctx, cd_bases)
    if name == "rm":
        inspect_rm_segment(tokens, first, wctx, cd_bases, env_bases)
    elif name == "find":
        inspect_find_segment(tokens, first, wctx, cd_bases, env_bases)
    elif name in ("cd", "pushd"):
        record_cd_target(tokens, first, wctx, cd_bases)


_WalkCtx = collections.namedtuple(
    "_WalkCtx",
    "cwd home safe_roots protected_exact workspace_root config_path windows "
    "host_native realpath_cache brace_total")


# How far `_find_statement_deletes` reads past a `(`. Past it the find is
# treated as deleting (refused): a statement that long is not a read-only
# listing anyone writes, and an unbounded look-ahead per `(` is quadratic.
_ALTERNATION_LOOKAHEAD_LIMIT = 4096
_ALTERNATION_LOOKAHEADS_PER_COMMAND = 16


def _find_statement_deletes(segment, scan_tokens, paren_index):
    """Does the `find` whose segment `(` just closed delete anything? The
    tokenizer cuts `find ws/(a|b) -delete` at the `(` and `|`, so its
    `-delete` sits past the `)`. Read on from the `(` to the end of the
    statement -- a separator at paren depth 0, other than the parens -- and
    apply `inspect_find_segment`'s own test: `-delete`, or rm as the command
    of an -exec-family primary. A read-only `find build/(a|b) -print` is
    then not refused."""
    words = [unquote(t) for t in segment]
    depth = 0
    for offset, tok in enumerate(scan_tokens[paren_index:]):
        if offset > _ALTERNATION_LOOKAHEAD_LIMIT:
            return True
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif tok in _SEGMENT_SEPARATORS and depth <= 0:
            break
        else:
            words.append(unquote(tok))
    prev = ""
    for word in words:
        if word == "-delete":
            return True
        if prev in FIND_EXEC_PRIMARIES and (word == "rm" or word.endswith("/rm")):
            return True
        prev = word
    return False


def _walk_rm_find_cd(scan_tokens, wctx):
    """Split the tokenized command into subcommands on the original's
    separator tokens and inspect each one. Each completed segment is
    inspected for a delete FIRST and only THEN recorded as a possible `cd`,
    processing segments strictly in the order they appear -- so a segment
    never moves the base it is itself judged against, which is what keeps
    `cd ~/Documents` from applying to the `cd` segment while still applying
    to everything after it. Raises `_Blocked` the moment any segment
    refuses; the caller in `evaluate` is the only place that catches it."""
    cd_bases = _CdBases()
    segment = []
    lookaheads = 0
    for index, tok in enumerate(scan_tokens):
        if tok in _SEGMENT_SEPARATORS:
            if tok == "(" and segment:
                # zsh glob alternation, `rm -rf repos/(a|b)` or `find
                # (repos|x) -delete`: the tokenizer cuts it at the `(` and the
                # `|`, so the matches can't be worked out. No function
                # definition reaches here: `rm()` and `rm ()` tokenize as one
                # `()` token, never a lone `(` -- so there is no exemption for
                # a bare command word, which would let `find (a|b) -delete`
                # through.
                _, name, _ = _command_word(segment, wctx, cd_bases)
                if name == "find":
                    # Each look-ahead reads up to the limit, so their NUMBER
                    # is bounded too, or many of them is quadratic again.
                    lookaheads += 1
                if name == "rm" or (name == "find" and (
                        lookaheads > _ALTERNATION_LOOKAHEADS_PER_COMMAND
                        or _find_statement_deletes(segment, scan_tokens,
                                                   index))):
                    raise _Blocked(
                        "an rm/find argument followed by `(`: zsh glob "
                        "alternation, whose matches can't be worked out "
                        "before it runs.")
            inspect_segment(segment, wctx, cd_bases)
            segment = []
        else:
            segment.append(tok)
    inspect_segment(segment, wctx, cd_bases)


_TOO_DEEP_MESSAGE = (
    "a command nested too deeply to parse — its substitutions and quotes "
    "go deeper than this guard can follow, so nothing after them could be "
    "judged.")


def evaluate(payload, ctx):
    """The guard's whole decision for one PreToolUse payload. Reads no
    environment and no stdin -- only ctx.config_path, if it exists.

    The quote and substitution scanners recurse once per nesting level, so
    a few hundred nested `"$(...)"` exhaust Python's recursion limit. Any
    other exception is a crash, which the entry point reports and ALLOWS;
    this one is caught here and BLOCKS instead, because it is reachable
    from input alone -- a review built one (400 levels, then
    `; rm -rf ~/Documents`) that the zsh original blocked and a crash would
    have let through. No human writes that nesting, so the over-block costs
    nothing, and a crash-allow reachable by construction would be a bypass.
    """
    try:
        return _evaluate(payload, ctx)
    except RecursionError:
        config = _load_config(ctx.config_path, ctx.home, ctx.environ)
        raw_cmd = payload["tool_input"]["command"]
        return Verdict(True, _build_message(_TOO_DEEP_MESSAGE, raw_cmd,
                                            config.owner_name))


def _evaluate(payload, ctx):
    if ctx.danger_allow:
        return Verdict(False, "")

    if not isinstance(payload, dict):
        return Verdict(False, "")

    tool_name = payload.get("tool_name")
    if tool_name == "PowerShell":
        # Regardless of tool_input: there is nothing to inspect that would
        # make this safe, since no rule above ever runs against PowerShell
        # syntax. A deliberate exception to "the guard fails open", ruled
        # 2026-09-29 -- see docs/decisions.md ("refuses the PowerShell tool").
        config = _load_config(ctx.config_path, ctx.home, ctx.environ)
        return Verdict(True, _build_powershell_message(config.owner_name))
    # Monitor runs a shell command like Bash does; a WebSocket
    # monitor carries no `command` and falls out as ALLOW below.
    if tool_name not in ("Bash", "Monitor"):
        return Verdict(False, "")

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return Verdict(False, "")
    raw_cmd = tool_input.get("command")
    if not isinstance(raw_cmd, str) or raw_cmd == "":
        return Verdict(False, "")

    # Canonicalise ctx.home BEFORE anything downstream expands `~`
    # or `$HOME` from it -- config loading, resolve()'s tilde substitution,
    # and the hardcoded safe/protected lists all key off this one value, so
    # it is done once here rather than at each of those call sites. A no-op
    # when ctx.windows is False.
    ctx = ctx._replace(home=canonical(ctx.home, ctx.windows))

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or cwd == "":
        cwd = os.getcwd()
    cwd = canonical(cwd, ctx.windows)

    config = _load_config(ctx.config_path, ctx.home, ctx.environ)

    kept = _strip_heredocs(raw_cmd)
    scan = tokenize("\n".join(kept))
    cmd = " ".join(scan)
    lines = cmd.split("\n")

    for matcher, rule_message in _TEXT_SHAPE_RULES:
        if any(matcher(line) for line in lines):
            return Verdict(True, _build_message(rule_message, raw_cmd, config.owner_name))

    wctx = _WalkCtx(cwd=cwd, home=ctx.home,
                    safe_roots=_safe_roots(ctx, config),
                    protected_exact=_protected_exact(ctx, config),
                    workspace_root=config.workspace_root,
                    config_path=ctx.config_path,
                    windows=ctx.windows,
                    host_native=_host_native(ctx.windows),
                    realpath_cache={},
                    brace_total=[0])
    try:
        _walk_rm_find_cd(scan, wctx)
    except _Blocked as exc:
        return Verdict(True, _build_message(exc.rule_message, raw_cmd, config.owner_name))

    return Verdict(False, "")
