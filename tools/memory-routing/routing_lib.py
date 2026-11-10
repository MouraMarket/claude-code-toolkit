"""Notice when a memory write looks like it should have gone to TOOLING.md.

Two systems hold durable knowledge and they have different jobs. Memory is a
lazily-read index of prose, which suits judgment — decisions, preferences, the
why behind a correction. TOOLING.md is a table injected every session, which
suits lookup — where a credential lives, whether a platform is connected, which
venv has a library. Put a lookup fact in memory and it is not a harmless
duplicate: it is a second copy that will eventually disagree with the first.

This is a NUDGE, not a gate. It runs PostToolUse, so the write has already
happened and nothing is blocked. That is deliberate. The only way to actually
prevent the write would be a PreToolUse deny driven by a keyword heuristic, and
a guard that rejects legitimate memory writes gets switched off — at which point
it protects nothing.

Precision over recall, for the same reason. A reminder that fires on ordinary
memory writes trains the reader to skip it, so the signals below are deliberately
concrete: an `op://` reference, a named CLI, a connector, a venv. A fact about a
*rule* that merely mentions a tool should stay silent, and usually does.
"""

import os
import re

# Concrete artifacts, not topics. "token" alone would fire on every memory that
# mentions a token in passing; `op://` only appears when a credential location
# is actually being recorded.
SIGNALS = [
    # A resolvable reference (two path segments), not the bare scheme. Memories
    # that merely say "read the op:// ref" are pointing at TOOLING.md, which is
    # the behaviour we want, not a violation of it.
    # Two `/`-separated segments, which segments may contain spaces — 1Password
    # vault names routinely do ("API Keys"). `\S+` would fail on exactly the
    # references we most want to catch.
    (r"op://[^/\n]+/[^/\n]+", "a resolvable `op://` credential reference"),
    (r"\biam\.gserviceaccount\.com\b", "a service-account identity"),
    (r"\.venv\b|\bvirtualenv\b", "a virtualenv location"),
    (r"~/\.local/bin", "an install path"),
    (r"\bgh auth\b|\brailway whoami\b|\bop whoami\b", "which account a CLI is authenticated as"),
    (r"\bmounted at\b|CloudStorage", "a local mount path"),
    (r"\b(gcloud|ripgrep|rg|jq|uv) is (not )?installed\b|\bnot installed\b",
     "whether a tool is installed"),
    (r"\brequires authentication\b", "a connector's authorization state"),
]

# Phrases that mean the memory is recording a RULE or a DECISION that happens to
# name a tool. These are exactly what memory is for, so their presence cancels a
# signal rather than adding to it.
RULE_MARKERS = [
    r"\*\*Why:\*\*",
    r"\*\*How to apply:\*\*",
    r"\bprefer\b.{0,40}\bover\b",
    r"\bdecided\b|\bdecision\b",
    r"\bcorrection\b|\bcorrected\b",
    r"\brule\b",
]

# `[^/]+` is what excludes `memory/history/<name>.history.md` — a history file
# has an extra `/` before its final segment, and a single slash-free segment
# cannot span it. It looks like an incidental character class and it is the
# ENTIRE history exclusion.
#
# This regex and `is_memory_write` below have a second consumer since
# 2026-09-09: `tools/context-growth/growth_lib.is_memory_file` delegates here
# rather than keeping the copy it used to have. Both exclusions — history and
# MEMORY.md — are now defined once, here. A change to either is a change to
# what context-growth caps, so weigh it on both tools.
MEMORY_DIR_RE = re.compile(r"/memory/[^/]+\.md$")


def is_memory_write(path):
    """True for a write to a memory entry, false for MEMORY.md itself.

    The index is a routing artifact — it names what lives elsewhere and is
    supposed to mention tooling. Nudging on it would fire on every edit.

    Also the shared predicate behind `growth_lib.is_memory_file` — see the
    note on `MEMORY_DIR_RE`.
    """
    if not path:
        return False
    if os.path.basename(path) == "MEMORY.md":
        return False
    return bool(MEMORY_DIR_RE.search(path))


def extract_content(tool_input):
    """The text being written, across Write and Edit payload shapes."""
    if not isinstance(tool_input, dict):
        return ""
    parts = [
        tool_input.get("content"),
        tool_input.get("new_string"),
    ]
    return "\n".join(p for p in parts if isinstance(p, str))


def matched_signals(text):
    """Config-fact signals present in the text, as human-readable labels."""
    if not text:
        return []
    found = []
    for pattern, label in SIGNALS:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(label)
    return found


def looks_like_a_rule(text):
    """True when the text reads as a rule or decision rather than a config fact."""
    if not text:
        return False
    hits = sum(
        1 for pattern in RULE_MARKERS if re.search(pattern, text, re.IGNORECASE)
    )
    # Two markers, not one: a single "Why:" is present in almost every
    # well-formed memory, including ones that are purely config facts.
    return hits >= 2


def render(signals):
    """The nudge, or '' when there is nothing worth saying."""
    if not signals:
        return ""
    listed = "; ".join(signals[:3])
    return (
        "This memory records %s. Facts about the machine, accounts, or "
        "connections belong in TOOLING.md — it is a lookup table injected every "
        "session, where memory is prose read lazily. Two copies of the same fact "
        "will eventually disagree, and TOOLING.md wins.\n"
        "If this is a RULE about how to work, or the WHY behind a decision, it is "
        "in the right place — carry on. Otherwise move the config half across and "
        "leave the rule half here." % listed
    )


def build(tool_input):
    """Full pipeline: a tool payload to nudge text. '' means stay silent."""
    if not isinstance(tool_input, dict):
        return ""
    if not is_memory_write(tool_input.get("file_path")):
        return ""
    text = extract_content(tool_input)
    signals = matched_signals(text)
    if not signals:
        return ""
    if looks_like_a_rule(text):
        return ""
    return render(signals)
