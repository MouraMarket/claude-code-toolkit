"""Shared helpers for Claude Code session auto-titling.

Standard library only; must run on system Python 3.9.
"""

import json
import os
from typing import Any, Dict, List, Optional

# Machine-local settings live outside the repo. See lib/toolkit_config.py.
import sys as _sys
_sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "lib"))
import toolkit_config as _config


def is_enabled() -> bool:
    """False only when explicitly disabled. Absent config means on."""
    try:
        return _config.get("session_titler", "enabled", default=True) is not False
    except Exception:
        return True


# Wrapper markers that pollute the /resume picker today. A user message that
# starts with one of these carries no information about what the session did.
JUNK_MARKERS = (
    "<system-reminder>",
    "<local-command-caveat>",
    "<command-message>",
    "<command-name>",
    "<user-prompt-submit-hook>",
)

# Lines longer than this are giant tool results, never useful for titling.
# Skipping them unparsed keeps memory bounded on multi-MB transcripts.
MAX_LINE_BYTES = 200_000

MAX_USER_MESSAGES = 3



def extract_text(content: Any) -> str:
    """Flatten a message `content` field (str or content-block list) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if text:
                    parts.append(text)
        return " ".join(parts)
    return ""


def is_junk(text: str) -> bool:
    """True if this text is a wrapper artifact rather than real user intent."""
    stripped = text.strip()
    if not stripped:
        return True
    for marker in JUNK_MARKERS:
        if stripped.startswith(marker):
            return True
    return False


def _tool_names_from(content: Any) -> List[str]:
    names = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name")
                if name:
                    names.append(name)
    return names


def sample_transcript(path: str, max_chars: int = 6000) -> Dict[str, Any]:
    """Stream a transcript and return a small, bounded sample of it.

    Memory is O(1) regardless of transcript size — real sessions reach tens
    of megabytes, so nothing may accumulate per-line.
    """
    sample = {
        "user_messages": [],
        "last_assistant": "",
        "tool_names": [],
        "user_count": 0,
    }  # type: Dict[str, Any]

    per_message_cap = max(200, max_chars // (MAX_USER_MESSAGES + 1))
    seen_tools = set()

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(line) > MAX_LINE_BYTES:
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(entry, dict):
                    continue

                entry_type = entry.get("type")
                message = entry.get("message") or {}
                if not isinstance(message, dict):
                    continue
                content = message.get("content")

                if entry_type == "user":
                    if entry.get("isMeta"):
                        continue
                    text = extract_text(content)
                    if is_junk(text):
                        continue
                    normalized = " ".join(text.split())
                    sample["user_count"] += 1
                    if len(sample["user_messages"]) < MAX_USER_MESSAGES:
                        sample["user_messages"].append(normalized[:per_message_cap])

                elif entry_type == "assistant":
                    text = extract_text(content)
                    if text.strip():
                        sample["last_assistant"] = " ".join(text.split())[:per_message_cap]
                    for name in _tool_names_from(content):
                        if name not in seen_tools:
                            seen_tools.add(name)
                            sample["tool_names"].append(name)
    except (IOError, OSError):
        return sample

    return sample


def is_titleworthy(sample: Dict[str, Any]) -> bool:
    """Sessions with fewer than two real user messages aren't worth a title.

    This doubles as the recursion backstop: the headless `claude -p` session
    the worker spawns has exactly one user message, so even if the env-var
    guard were bypassed, it would never be titled.
    """
    return sample.get("user_count", 0) >= 2


import re

_PROMPT_TEMPLATE = """You are titling a Claude Code session so it can be found later in a session browser.

Working directory: {cwd}
Tools used: {tools}

First user messages:
{user_messages}

Final assistant message:
{last_assistant}

Write ONE title following these rules:
- A short phrase saying what happened, at most 60 characters.
- Describe the OUTCOME, not the request. Write "Fixed the retry bug in the upload script", not "User asked about a bug".
- Sentence case: capital first letter, no trailing period.
- Nothing before the phrase: no project label, code, prefix or quotes.

Output only the title. No preamble, no explanation."""


def build_prompt(sample: Dict[str, Any], cwd: str) -> str:
    user_messages = "\n".join(
        "- " + m for m in sample.get("user_messages", [])
    ) or "(none)"
    tools = ", ".join(sample.get("tool_names", [])[:12]) or "(none)"
    return _PROMPT_TEMPLATE.format(
        cwd=cwd or "(unknown)",
        tools=tools,
        user_messages=user_messages,
        last_assistant=sample.get("last_assistant") or "(none)",
    )


# A model handed a contentless transcript answers in prose ("I don't have any
# information to title this session..."). These openers mark an answer about
# the request rather than a title of the session. A bare "i" covers every
# contraction ("I'm", "I've", straight or curly): an apostrophe is a word
# boundary.
_PROSE_OPENER_RE = re.compile(
    r"^(i|sorry|apolog\w*|my apologies|unfortunately|regrettably|as an ai"
    r"|there (is|are|was|were) no|no (title|information|content|messages?))\b",
    re.IGNORECASE)

# Refusal wording wherever it sits ("Apologies, I cannot title this"). A list
# of phrases is never complete, so this narrows what gets through rather than
# closing it; the prompt asking for a bare phrase is the first defence.
_REFUSAL_RE = re.compile(
    r"\b(cannot|can['\u2019]t|unable to|not able to"
    r"|not enough (information|context|content))\b",
    re.IGNORECASE)

# A sentence end followed by more text: a title is one phrase, not two sentences.
_SECOND_SENTENCE_RE = re.compile(r"[.!?]\s+\S")


def clean_title(raw: Optional[str], max_len: int = 60) -> Optional[str]:
    """Normalize a model response into a single-line title, or None.

    A title is a plain outcome phrase with no prefix. There is no fixed shape
    to check, so prose is rejected by what prose looks like instead: longer
    than the limit (never truncated, because truncation is how a refusal was
    once stored as a title), opening in the first person or with an apology,
    carrying refusal wording ("cannot", "unable to") anywhere, or running to a
    second sentence. Rejecting is safe: an untitled session is
    strictly better than a lie.
    """
    if not raw:
        return None
    text = " ".join(str(raw).split())
    text = text.strip().strip('"').strip("'").strip()
    # Models occasionally prefix a label despite instructions.
    for prefix in ("Title:", "title:", "TITLE:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    text = text.strip('"').strip("'").strip()
    if text.endswith(".") and not text.endswith(".."):
        text = text[:-1].rstrip()
    if not text or len(text) > max_len:
        return None
    if (_PROSE_OPENER_RE.match(text) or _REFUSAL_RE.search(text)
            or _SECOND_SENTENCE_RE.search(text)):
        return None
    return text


import datetime

INDEX_PATH = _config.get_path(
    "session_titler", "index_path",
    default=os.path.expanduser("~/.claude/session-index.tsv"))
LOG_PATH = os.path.expanduser("~/.claude/title-hook.log")


def log(message: str) -> None:
    """Best-effort logging. Never raises — a logging failure must not
    propagate into session teardown."""
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write("[{0}] {1}\n".format(stamp, message))
    except Exception:
        pass


def append_title_record(transcript_path: str, session_id: str, title: str) -> bool:
    """Append a custom-title record. Transcripts are user data: append only,
    never rewrite. Titles resolve via findLast, so re-titling is also an
    append."""
    if not os.path.exists(transcript_path):
        log("append_title_record: no such transcript {0}".format(transcript_path))
        return False
    record = {
        "type": "custom-title",
        "customTitle": title,
        "sessionId": session_id,
    }
    try:
        with open(transcript_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except (IOError, OSError) as exc:
        log("append_title_record failed for {0}: {1}".format(transcript_path, exc))
        return False


def has_title(transcript_path: str) -> bool:
    """True if the transcript already carries a custom-title record."""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"type":"custom-title"' in line or '"type": "custom-title"' in line:
                    return True
    except (IOError, OSError):
        return False
    return False


def append_index_row(session_id: str, cwd: str, title: str,
                     index_path: Optional[str] = None,
                     date_str: Optional[str] = None) -> bool:
    """Append one tab-separated row: date, session id, cwd, title."""
    target = index_path if index_path is not None else INDEX_PATH
    stamp = date_str or datetime.date.today().isoformat()
    fields = [
        stamp,
        session_id,
        " ".join(str(cwd or "").split()),
        " ".join(str(title or "").split()),
    ]
    row = "\t".join(f.replace("\t", " ") for f in fields)
    try:
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(row + "\n")
        return True
    except (IOError, OSError) as exc:
        log("append_index_row failed: {0}".format(exc))
        return False
