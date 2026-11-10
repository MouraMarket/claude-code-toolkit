#!/usr/bin/env python3
"""The Core block: the part of a context file guaranteed to be read.

A Core is authoritative on its own — if Core and body disagree, Core wins — so a
rule inside it may not depend on the body having been read. `dangling_refs` is
the machine-checkable half of that contract.

**Both marker pairs are live, and the EARLIEST one in the document wins** —
position, never marker kind. `DIGEST` predates the name and is still what
TOOLING.md carries; accepting both is what makes this landable without a
migration. A rename would touch digest_lib, install.sh, the config key and the
file itself for no behavioural gain.

Kind-based precedence was tried and is dead: it let a CORE pair inside a body
code fence outrank the file's real digest, because the documentation for this
very feature prints that pair. Position-based precedence is what the shipped
single-pattern code had for free, and losing it was the defect.

Shared rather than copied because both `digest_lib` and `context_lint_lib` need
it, and this repo has already paid once for a duplicated live tool.
"""

import re
from typing import List, Optional

CORE_START = "<!-- CORE:START -->"
CORE_END = "<!-- CORE:END -->"
DIGEST_START = "<!-- DIGEST:START -->"
DIGEST_END = "<!-- DIGEST:END -->"

# Two separate patterns, never one alternation: an alternation would match
# `DIGEST:START ... CORE:END`, pairing markers that were never a pair.
_CORE_RE = re.compile(re.escape(CORE_START) + r"(.*?)" + re.escape(CORE_END), re.DOTALL)
_DIGEST_RE = re.compile(re.escape(DIGEST_START) + r"(.*?)" + re.escape(DIGEST_END), re.DOTALL)

_PATTERNS = ((_CORE_RE, "CORE"), (_DIGEST_RE, "DIGEST"))


def _blocks(text):
    """Every complete pair as (start_offset, label, body), earliest first.

    ONE function behind extract(), marker_label() and block_count(), so the
    three cannot disagree about which pair is the file's block. Deriving the
    label from a second, independent scan is how a diagnostic comes to name
    markers the file does not carry.

    Ordered by position and NOT by marker kind (module docstring). Kind-based precedence
    let a CORE pair inside a body code fence outrank the file's real digest,
    measured through the real hook. Sorting by offset restores the property the
    shipped single-pattern code had for free.
    """
    if not text:
        return []
    found = []
    for pattern, label in _PATTERNS:
        for match in pattern.finditer(text):
            found.append((match.start(), label, match.group(1).strip()))
    found.sort(key=lambda item: item[0])
    return found

# Every pattern carries a qualifier. A bare `below` is a preposition far more
# often than a cross-reference — "below the thresholds" is real prose in a real
# CLAUDE.md — and this check is advisory precisely because it reads prose.
_DANGLING_RES = [
    re.compile(r"\bsee\s+(?:below|above)\b", re.IGNORECASE),
    re.compile(r"\b(?:described|detailed|listed|noted|outlined|shown|stated)"
               r"\s+(?:below|above)\b", re.IGNORECASE),
    re.compile(r"\bas\s+(?:noted|described|discussed)"
               r"\s+(?:earlier|later|above|below)\b", re.IGNORECASE),
    re.compile(r"\bthe\s+section\s+on\b", re.IGNORECASE),
    re.compile(r"§"),
]


def _first_populated(text):
    """The earliest complete pair with a non-empty body, or None.

    Empty pairs are SKIPPED rather than terminating the search: an empty CORE
    block above a populated DIGEST block previously reported "no block at all"
    on a file that visibly has one, and the broken-file notice then told the
    reader to add markers already present.
    """
    for _offset, label, body in _blocks(text):
        if body:
            return label, body
    return None


def extract(text: Optional[str]) -> Optional[str]:
    """The block body, stripped. None when the document has no usable pair.

    Returning None rather than the whole document is load-bearing: a renamed or
    deleted marker must degrade to no output, never to dumping a reference doc
    into every session's context.
    """
    hit = _first_populated(text)
    return hit[1] if hit else None


def marker_label(text: Optional[str]) -> Optional[str]:
    """Which pair carried the body extract() returned: "CORE", "DIGEST", None.

    Callers name markers in diagnostics with this. Telling someone to trim a
    CORE block in a file that only has DIGEST markers sends them looking for
    something that is not there.
    """
    hit = _first_populated(text)
    return hit[0] if hit else None


def block_count(text: Optional[str]) -> int:
    """How many complete pairs the document carries, of either kind.

    Position-based precedence is not airtight: a documentation fence above
    the real block still wins. More than one pair means the file is ambiguous
    about what gets injected, and a caller that can say so out loud beats a
    silent pick.
    """
    return len(_blocks(text))


def dangling_refs(core_text: Optional[str]) -> List[str]:
    """Cross-references inside a Core that depend on the body being read.

    Advisory by design, and the callers must keep it that way: this is a
    heuristic on prose, and a heuristic that blocks a commit is a heuristic
    someone routes around with --no-verify — taking the size cap with it.
    """
    if not core_text:
        return []
    found = []  # type: List[str]
    for pattern in _DANGLING_RES:
        found.extend(match.group(0) for match in pattern.finditer(core_text))
    return found
