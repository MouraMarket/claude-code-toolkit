#!/usr/bin/env python3
"""Pure helpers for the build skill's drift guard.

The guard pins the `build` skill's description against the skill body: the
description is the only router now (the SessionStart hook is retired), so the
exclusion list it carries must match the "When NOT to use this" section.
`extract_exclusion_clause` reads the body, `extract_description_clause` reads
the description; both are used ONLY by tests. This module holds no
runtime-critical text.
"""

import re

EXCLUSION_HEADING = "## When NOT to use this"


def _extract_em_dash_clause(text: str) -> str:
    """The text between the first two em dashes in `text`, whitespace
    collapsed to single spaces. Raises ValueError if no such pair exists, or
    if what lies between them is empty."""
    match = re.search(r"—(.+?)—", text, re.DOTALL)
    if not match:
        raise ValueError("no em-dash-delimited clause found in the given text")
    clause = re.sub(r"\s+", " ", match.group(1)).strip()
    if not clause:
        raise ValueError("the em-dash-delimited clause was empty")
    return clause


def extract_exclusion_clause(build_md_text: str) -> str:
    """The example list from build.md's "## When NOT to use this" section,
    e.g. "analysis, a short document, a recap, one file".

    Raises ValueError if the heading or the em-dash-delimited clause after it
    cannot be found -- it must NEVER return "", because an empty string is a
    substring of everything and a drift guard built on
    `assertIn("", text)` can never fail: a guard that stops looking at a line
    precisely when someone edits it is not a guard.
    """
    heading_index = build_md_text.find(EXCLUSION_HEADING)
    if heading_index == -1:
        raise ValueError(
            "heading {!r} not found in build.md".format(EXCLUSION_HEADING))
    return _extract_em_dash_clause(build_md_text[heading_index:])


def extract_description_clause(description: str) -> str:
    """The text after the LAST em dash in a skill `description`, whitespace
    collapsed and the trailing period stripped, e.g.
    "analysis, a short document, a recap, one file".

    Raises ValueError when there is no em dash or nothing follows it -- same
    never-return-"" rule as `extract_exclusion_clause`.
    """
    if "—" not in description:
        raise ValueError("no em dash found in the given description")
    clause = re.sub(r"\s+", " ", description.rsplit("—", 1)[1]).strip()
    clause = clause.rstrip(".").strip()
    if not clause:
        raise ValueError("the text after the last em dash was empty")
    return clause


def clause_appears_in(text: str, clause: str) -> bool:
    """Whether `clause` occurs in `text`, ignoring how `text` is line-wrapped.

    The needle always arrives whitespace-collapsed, because
    `_extract_em_dash_clause` collapses it. Searching a RAW haystack therefore
    silently tests something narrower than wording agreement: whether the
    clause happens to sit on one unbroken source line. Reflowing build.md's
    paragraph at 80 columns, with no wording change at all, reddened the guard
    and pointed at drift that had not occurred -- whose obvious "fix" is to
    edit the other side, introducing the real drift the guard exists to catch.

    Both directions of the drift guard route through here so that a single
    mutation of this function reddens the reflow control in the test suite.
    """
    return clause in re.sub(r"\s+", " ", text)
