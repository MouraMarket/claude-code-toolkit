"""Grading and cost accounting for skill evals.

Skills in this repo are prompts. We test the Python *around* some of them, but
nothing else measures whether a skill still produces the output it claims to.
This is that measurement: run a skill against cases with known-good answers,
grade the output with code, and report pass rate alongside token cost.

**Code-based grading, not a model judge.** Every check here is a set, string or
regex operation. A model judge would grade more nuance and would also cost a
second API call per case, take longer, and introduce its own variance, which is
exactly the tax that stops an eval from being run. Where a skill's quality bar
is already written as a countable rule ("at least 15 variations", "no em
dashes"), code can check it exactly.

**Pass rate AND cost.** Per-token price is not per-task cost: a model that needs
fewer output tokens can be cheaper at a higher sticker price. No price table
ships here, because prices change and a stale table reads as fact. Token counts
are always reported, and a dollar figure appears only when the caller supplies
their own per-million-token rates.

Stdlib only, on purpose: no venv, no install step, nothing to keep in sync.
"""

import json
import os
import re
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"

class EvalError(Exception):
    """Anything that stops a case from being graded at all."""


def resolve_api_key():
    """The key from ANTHROPIC_API_KEY, and nowhere else.

    An eval spends the caller's money, so the key is never looked up through any
    other channel. It is never written to disk or echoed.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EvalError("set ANTHROPIC_API_KEY to run --live")
    return key


def call_model(system, user, model, api_key, max_tokens=8000):
    """One Messages API call. Returns (text, usage dict).

    Raw urllib rather than the anthropic SDK so the harness has no dependencies
    and runs from a clean checkout. The request shape is the documented minimum:
    model, max_tokens, messages, plus a system prompt.
    """
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            body = json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise EvalError("HTTP %s: %s" % (error.code, detail))
    except urllib.error.URLError as error:
        raise EvalError("network error: %s" % error.reason)

    # A refusal returns HTTP 200 with an empty or partial content array, so
    # check stop_reason before indexing into content.
    if body.get("stop_reason") == "refusal":
        raise EvalError("model refused the request")

    text = "".join(block.get("text", "")
                   for block in body.get("content", [])
                   if block.get("type") == "text")
    return text, body.get("usage", {})


def usage_cost(usage, rates):
    """Dollar cost of one call at the caller's own rates.

    `rates` is (input_usd_per_mtok, output_usd_per_mtok), or None when the
    caller gave none. None returns None, so an unpriced run reports 'unknown'
    rather than a confident $0.00.
    """
    if rates is None:
        return None
    input_rate, output_rate = rates
    # cache_read/cache_creation are priced differently; this harness sends no
    # cache_control, so they are absent. Counted as plain input if ever present.
    input_tokens = (usage.get("input_tokens", 0)
                    + usage.get("cache_read_input_tokens", 0)
                    + usage.get("cache_creation_input_tokens", 0))
    output_tokens = usage.get("output_tokens", 0)
    return (input_tokens * input_rate + output_tokens * output_rate) / 1_000_000


# --- Output parsing -------------------------------------------------------

# Lines that are structure, not terms: markdown headers, horizontal rules,
# emphasized category labels, and the numbered/bulleted list markers around a
# term.
#
# `\*{1,2}` rather than `\*\*`: a pattern that only caught **bold** labels would
# extract a single-asterisk *Transpositions:* or *Brand word* as a keyword, and
# inflate every term count. A `* bullet` is safe from this: it has no closing
# asterisk.
_HEADER = re.compile(r"^\s*(#{1,6}\s|---+\s*$|\*{1,2}[^*]+\*{1,2}\s*:?\s*$)")
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_TRAILING_NOTE = re.compile(r"\s*[(\[].*?[)\]]\s*$")


def extract_terms(text):
    """The keyword terms in a skill's output, in order, deduped case-insensitively.

    Deliberately permissive: the skill emits a categorized list and a flat list,
    and formatting varies between runs. Grading on "every line that looks like a
    term" is stable across that variation, where grading on a specific section
    heading would fail for a reason unrelated to term quality.

    One deliberate exception to the permissiveness: a line that is *entirely*
    emphasized is read as a category label, not a term. That would drop a real
    term someone wrapped in asterisks — but the skill's output format asks for
    plain lists, and letting `*Transpositions:*` through as a keyword is the
    worse of the two errors, because a label would be graded as a keyword.
    """
    terms = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _HEADER.match(line):
            continue
        line = _BULLET.sub("", line).strip()
        # Keyword match-type notation: "phrase" and [exact]. Keep the inner term.
        if len(line) > 2 and line[0] in '"[' and line[-1] in '"]':
            line = line[1:-1].strip()
        line = _TRAILING_NOTE.sub("", line).strip().strip(",")
        if not line or _HEADER.match(line):
            continue
        # Prose, not a term. Real terms are short and unpunctuated.
        if len(line) > 80 or line.endswith((".", ":", "?", "!")):
            continue
        key = dedupe_key(line)
        if key and key not in seen:
            seen.add(key)
            terms.append(line)
    return terms


def normalize(term):
    """Presence key: case-, punctuation- and spacing-insensitive.

    "Cafe Lumen", "cafe-lumen" and "CafeLumen" all collapse to one value, which
    is what a coverage check wants: "did the name appear in some form?" Never use this to decide whether two terms are the same
    *keyword*; see dedupe_key.
    """
    return re.sub(r"[^a-z0-9]", "", term.lower())


def dedupe_key(term):
    """Identity key: case- and punctuation-insensitive, but SPACING-SENSITIVE.

    This is deliberately not `normalize`. 'harbor lane' and 'harborlane' are
    the same name but different keywords: the run-together form is a spacing
    variation a skill may be asked to generate, and an ad platform treats them
    as distinct. Deduping on the space-insensitive key would collapse them into
    one, which silently deflates the term count and could fail a min_terms check
    against output that was exactly right.
    """
    return re.sub(r"[^a-z0-9 ]", "", term.lower().replace("-", " ")).strip()


# --- Graders --------------------------------------------------------------
#
# Each returns a list of failure strings; empty means the check passed. They are
# declarative so a new skill is a new JSON case file, not new Python.

def check_min_terms(terms, minimum):
    if len(terms) < minimum:
        return ["produced %d terms, expected at least %d" % (len(terms), minimum)]
    return []


def check_must_include_all(terms, required):
    keys = {normalize(t) for t in terms}
    return ["missing required term: %s" % r for r in required if normalize(r) not in keys]


def check_must_include_any_of(terms, groups):
    """Each group is a set of acceptable spellings; at least one must appear.

    This is how variant coverage is graded without over-specifying: an accent
    check wants "Cafe Lumen" OR "Café Lumen" present, not one exact string.
    """
    keys = {normalize(t) for t in terms}
    failures = []
    for group in groups:
        if not any(normalize(option) in keys for option in group):
            failures.append("no term from required group: %s" % ", ".join(group))
    return failures


def check_must_not_include(terms, forbidden):
    keys = {normalize(t) for t in terms}
    return ["produced forbidden term: %s" % f for f in forbidden if normalize(f) in keys]


def check_no_duplicates(terms):
    """The skill promises a case-insensitively deduplicated list.

    extract_terms already dedupes by normalized key, so this compares against a
    looser key — case and spacing only — to catch a list that shipped the same
    term twice in different case.
    """
    seen = {}
    failures = []
    for term in terms:
        key = re.sub(r"\s+", " ", term.strip().lower())
        if key in seen:
            failures.append("duplicate term: %r and %r" % (seen[key], term))
        else:
            seen[key] = term
    return failures


# Every check key `grade` understands. A key outside this set is a typo or a
# retired check, and would otherwise be ignored: the case would pass while
# testing less than it claims.
KNOWN_CHECKS = frozenset({
    "min_terms", "must_include_all", "must_include_any_of", "must_not_include",
    "no_duplicates", "must_not_match", "must_contain", "no_new_numbers",
    "grade_until",
})

_LIST_MARKER = re.compile(r"(?m)^\s*\d+[.)]\s")
_DIGIT_RUN = re.compile(r"\d+")


def validate_case(case):
    """Problems that would make a case meaningless. Empty list means usable."""
    if not isinstance(case, dict):
        return ["case is not an object"]
    problems = []
    if not case.get("name"):
        problems.append("case has no name")
    if not case.get("prompt"):
        problems.append("case %r has no prompt" % case.get("name"))
    checks = case.get("checks")
    if not checks or not isinstance(checks, dict):
        problems.append("case %r has no checks" % case.get("name"))
    else:
        unknown = sorted(set(checks) - KNOWN_CHECKS)
        if unknown:
            problems.append("case %r has unknown check(s): %s"
                            % (case.get("name"), ", ".join(unknown)))
        # A check switched off (false, empty) grades nothing, so a case made
        # only of those would pass whatever the skill produced.
        elif not any(value not in (False, None, "", [], {}) for value in checks.values()):
            problems.append("case %r has no active checks" % case.get("name"))
    return problems


def check_must_not_match(text, patterns):
    failures = []
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            failures.append("matched forbidden pattern %r: %r" % (pattern, match.group(0)))
    return failures


def check_must_contain(text, required):
    return ["missing required text: %r" % r for r in required if r not in text]


def check_no_new_numbers(text, prompt):
    """Every digit run in the output must already be in the prompt.

    Catches a rewrite that invents or alters a figure. Ordered-list markers
    ("1. ", "2) ") are structure, not figures, so they are removed first.
    """
    allowed = set(_DIGIT_RUN.findall(prompt))
    found = set(_DIGIT_RUN.findall(_LIST_MARKER.sub("", text)))
    return ["number not in the prompt: %s" % n for n in sorted(found - allowed)]


def grade(text, case):
    """Run every check a case declares. Returns (passed, failures, terms)."""
    checks = case.get("checks", {})
    failures = []

    # grade_until: only the text before the marker is graded, so a trailing
    # section (a change log, say) may quote what the rules forbid.
    marker = checks.get("grade_until")
    if marker is not None:
        if marker in text:
            text = text.split(marker, 1)[0]
        else:
            failures.append("output has no %r section" % marker)

    terms = extract_terms(text)

    if "min_terms" in checks:
        failures += check_min_terms(terms, checks["min_terms"])
    if "must_include_all" in checks:
        failures += check_must_include_all(terms, checks["must_include_all"])
    if "must_include_any_of" in checks:
        failures += check_must_include_any_of(terms, checks["must_include_any_of"])
    if "must_not_include" in checks:
        failures += check_must_not_include(terms, checks["must_not_include"])
    if checks.get("no_duplicates"):
        failures += check_no_duplicates(terms)
    if "must_not_match" in checks:
        failures += check_must_not_match(text, checks["must_not_match"])
    if "must_contain" in checks:
        failures += check_must_contain(text, checks["must_contain"])
    if checks.get("no_new_numbers"):
        failures += check_no_new_numbers(text, case.get("prompt", ""))

    return (not failures), failures, terms
