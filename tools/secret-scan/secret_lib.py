"""Detection logic for claude-secret-scan.

Scans text for credentials that must never reach git history. Kept separate from
the executable so the rules can be tested directly.

Two design choices worth knowing before you change anything here:

**Prefix-anchored patterns only, never entropy.** Every rule below matches a
vendor's documented key *shape* — `sk-ant-`, `ghp_`, `AKIA`, `xoxb-`. An
entropy heuristic would catch more in theory and would also flag every git SHA,
base64 asset, minified bundle and UUID in the repo. The danger-guard lesson
applies directly: a guard that misfires on ordinary work gets disabled, and a
disabled guard protects nothing. Precision is the whole product here.

**Findings are redacted.** A finding reports the rule, the file and the line —
never the secret. Printing it would copy the credential into terminal
scrollback, CI logs and, if this ever runs in Actions, a publicly readable log.
A scanner that leaks what it finds is worse than no scanner.
"""

import os
import re
import sys

# An inline escape hatch, borrowed from detect-secrets. Put it in a comment on
# the offending line when a match is genuinely not a secret — a documented
# example key, a test fixture, a vendor's own sample from their docs.
ALLOW_MARKER = "allowlist secret"

# Credential shapes. (rule name, compiled pattern).
#
# One table ships, and it lives in the leak gate (`leak_gate_lib.SECRET_RULES`):
# a vendor shape added there is enforced here at commit time with no second
# edit, and the two can never disagree about what a key looks like. The leak
# gate matches raw bytes; this scanner matches decoded lines, so each pattern is
# recompiled from the same source as text. Every source is ASCII, so the two
# compile to the same match.
#
# What stays here and NOT in the gate is policy: the inline allow marker and the
# placeholder filter below. A commit hook can afford them, because the person
# committing sees the line. A publication gate cannot: a line carrying the
# marker would pass a real key.
#
# The import resolves from this file's REAL path, so it works when the scanner
# runs through a ~/.local/bin symlink or the global pre-commit chain. It raises
# when the leak gate is missing; the executable turns that into exit 2. A
# scanner with no rules would report every commit clean.
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "leak-gate"))

from leak_gate_lib import SECRET_RULES  # noqa: E402

PATTERNS = tuple(
    (name, re.compile(pattern.pattern.decode("ascii"))) for name, pattern in SECRET_RULES
)
if not PATTERNS:
    raise ImportError("leak_gate_lib.SECRET_RULES is empty: nothing to scan with")

# There is deliberately no "this line references a credential, so skip it" rule.
#
# One existed here and was removed. The idea was that a line mentioning `op://`,
# `$VAR` or `os.environ` is assigning FROM a reference and therefore safe. It
# turned out to buy nothing and cost something:
#
#   - It prevented no real false positives. A line like
#     `token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}` contains no string that
#     matches any pattern above, so it was never going to be flagged anyway.
#   - It opened a hole in the one case that matters most:
#     `os.environ["ANTHROPIC_API_KEY"] = "sk-ant-api03-<real key>"` was
#     suppressed by the `os.environ` shape while containing a live credential.
#     That is the canonical way people hardcode a key, and Anthropic's own
#     cookbook flags it as a critical review finding.
#
# It was caught by mutation-testing: disabling the rule changed no test result,
# which meant the tests covering it passed for a reason unrelated to the rule.
# If you are tempted to add it back, write the failing test first.


def _looks_like_placeholder(value):
    """True for the fake keys that live in docs and .env.example files.

    Kept conservative on purpose. A real key is long and character-diverse, so
    the tells here are structural rather than word-based: no substring check for
    'test' or 'example', because a 95-character random key can contain either
    and silently suppressing a real credential is the one failure this tool
    cannot have.

    Only shapes that a PATTERNS match can actually contain belong here. `value`
    always comes from one of those patterns, and every one of them is built from
    `[A-Za-z0-9_-]`-style classes — so `<`, `>` and `.` can never appear in it.
    Branches for `<your-key-here>` and `sk-ant-api03-key...here` used to sit here
    and were dead code: those lines are not flagged because the pattern never
    matches them, not because they were filtered. Both are covered by
    `test_placeholder_shapes_never_match_a_pattern` instead.
    """
    if "xxxx" in value.lower():
        return True
    # Real credentials are high-entropy. Five distinct characters across a long
    # string means something like `sk-aaaaaaaabbbbbbbb`, not a key.
    body = value.split("-")[-1].split("_")[-1]
    if len(body) >= 12 and len(set(body)) <= 5:
        return True
    return False


def redact(value):
    """A finding must identify the secret without reproducing it."""
    if len(value) <= 8:
        return "%s… (%d chars)" % (value[:2], len(value))
    return "%s…%s (%d chars)" % (value[:6], value[-2:], len(value))


class Finding(object):
    """One credential-shaped string, located but not reproduced."""

    def __init__(self, path, line_number, rule, redacted):
        self.path = path
        self.line_number = line_number
        self.rule = rule
        self.redacted = redacted

    def __repr__(self):
        return "Finding(%r, %r, %r)" % (self.path, self.line_number, self.rule)

    def format(self):
        # A tracked name can hold a newline or tab; escape non-printables so one
        # finding stays one output line and cannot forge another.
        path = "".join(c if c.isprintable() else repr(c)[1:-1] for c in self.path)
        where = "%s:%s" % (path, self.line_number) if self.line_number else path
        return "  %-24s %s  [%s]" % (self.rule, where, self.redacted)


def scan_line(line, path="", line_number=0):
    """Findings in a single line. Empty list means clean."""
    if ALLOW_MARKER in line:
        return []
    findings = []
    for rule, pattern in PATTERNS:
        for match in pattern.finditer(line):
            value = match.group(0)
            if _looks_like_placeholder(value):
                continue
            findings.append(Finding(path, line_number, rule, redact(value)))
    return findings


def scan_text(text, path=""):
    """Findings across a whole file's contents."""
    findings = []
    for index, line in enumerate(text.splitlines(), start=1):
        findings.extend(scan_line(line, path, index))
    return findings


def scan_diff(diff_text):
    """Findings in the ADDED lines of a unified diff.

    Only added lines are scanned. Context and removed lines are already in
    history — flagging them would mean every commit to a file that once held a
    secret is blocked forever, with no action that clears it, which is how a
    check gets bypassed permanently rather than fixed.

    The parser tracks whether it is inside a hunk. Outside one, `+++ ` is a file
    header; inside one, a line starting `+` is added content whatever follows,
    so `+` + `++TOKEN=...` is scanned rather than mistaken for a header. Lines
    split on `\\n` only: `splitlines()` also breaks on U+2028, U+0085 and a lone
    `\\r`, which would orphan the tail of an added line without its `+`.
    """
    findings = []
    path = ""
    line_number = 0
    in_hunk = False
    for line in diff_text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if line.startswith("diff --git "):
            in_hunk = False
            path = ""
            continue
        if line.startswith("@@"):
            # @@ -old,count +new,count @@ — we want the new-side start line.
            match = re.search(r"\+(\d+)", line)
            line_number = int(match.group(1)) if match else 0
            in_hunk = True
            continue
        if not in_hunk:
            if line.startswith("+++ "):
                target = line[4:].strip()
                # `+++ b/path/to/file`, or `/dev/null` for a deletion.
                path = target[2:] if target.startswith(("a/", "b/")) else target
            continue
        if line.startswith("+"):
            findings.extend(scan_line(line[1:], path, line_number))
            line_number += 1
        elif line.startswith(("-", "\\")):
            # A removed line is not in the new file; a `\ No newline` marker is
            # not a line at all. Neither advances the new-side counter.
            continue
        else:
            # Context line: advances the new-side counter.
            line_number += 1
    return findings
