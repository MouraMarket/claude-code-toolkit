"""Tool-rules pages: rules that load when a matching tool is called.

A page is a markdown file in <workspace_root>/.claude/tool-rules/ whose
frontmatter lists `tools:` patterns. The first matching call in a session (per
agent) injects the page body as PostToolUse additionalContext.

A pattern is a tool-name glob (`mcp__*tracker*`) or `Tool(<glob>)` for one of
Bash, Read, Grep or Glob, matched against that tool's input fields (see
_SCOPED_FIELDS). A Grep or Glob call that omits `path` searches the cwd, which
no field names, so only its explicit fields can match.

An over-cap page emits an overage line INSTEAD of its text, never a truncated
body: the harness drops hook output above ~10KB, and a clipped rule reads as a
complete one.
"""

import fnmatch
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path

import toolkit_config

DEFAULT_MAX_BYTES = 9500
STATE_MAX_AGE_SECONDS = 7 * 86400
HOOK_MATCHER = "Bash|Read|Grep|Glob|mcp__.*"
AUDIT_WINDOW_SECONDS = STATE_MAX_AGE_SECONDS - 86400
_BASH_PATTERN = re.compile(r"^Bash\((.*)\)$", re.S)
_SCOPED_PATTERN = re.compile(r"^(Bash|Read|Grep|Glob)\((.*)\)$", re.S)
# The fields that say WHERE a call works. Grep's `pattern` is the regex it
# searches for, not a location, so it is left out.
_SCOPED_FIELDS = {"Bash": ("command",), "Read": ("file_path",),
                  "Grep": ("path", "glob"), "Glob": ("path", "pattern")}
_LIST_ITEM = re.compile(r"^\s*-\s+(.*?)\s*$")


def tool_rules_dir(workspace_root):
    """The only resolver for the pages directory; None without a workspace root."""
    if not workspace_root:
        return None
    return Path(workspace_root) / ".claude" / "tool-rules"


def parse_page(text):
    """(patterns, body), or None if the page has no frontmatter `tools:` list."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        return None
    close = None
    for index in range(1, len(lines)):
        if lines[index].rstrip() == "---":
            close = index
            break
    if close is None:
        return None
    patterns = []
    in_tools = False
    for line in lines[1:close]:
        if not in_tools:
            if line.strip() == "tools:":
                in_tools = True
            continue
        item = _LIST_ITEM.match(line)
        if not item:
            break
        value = item.group(1)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            patterns.append(value)
    if not patterns:
        return None
    body = "\n".join(lines[close + 1:]).lstrip("\n")
    return patterns, body


def matches(pattern, tool_name, tool_input):
    scoped = _SCOPED_PATTERN.match(pattern)
    if scoped:
        if tool_name != scoped.group(1):
            return False
        values = [(tool_input or {}).get(field) for field in _SCOPED_FIELDS[tool_name]]
        return any(isinstance(value, str) and fnmatch.fnmatchcase(value, scoped.group(2))
                   for value in values)
    return fnmatch.fnmatchcase(tool_name or "", pattern)


def max_bytes(config=None):
    value = toolkit_config.get("tool_rules", "max_bytes")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_MAX_BYTES


def state_dir():
    override = os.environ.get("CLAUDE_TOOL_RULES_STATE_DIR")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".claude" / "tool-rules-state"


def _state_path(session_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(session_id or "")) or "none"
    return state_dir() / (safe + ".json")


def _load_emitted(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        emitted = data.get("emitted", [])
        return set(item for item in emitted if isinstance(item, str))
    except Exception:
        return set()


def _save_emitted(path, emitted):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"emitted": sorted(emitted)}, handle)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def prune_stale_state():
    """Delete state files untouched for 7 days. Best-effort."""
    try:
        cutoff = time.time() - STATE_MAX_AGE_SECONDS
        for entry in state_dir().glob("*.json"):
            try:
                if entry.stat().st_mtime < cutoff:
                    entry.unlink()
            except OSError:
                pass
    except Exception:
        pass


def reset_session(session_id):
    # Empty, never delete: the file stays as evidence the hook ran this session.
    # An absent file stays absent.
    try:
        path = _state_path(session_id)
        if path.is_file():
            _save_emitted(path, set())
    except Exception:
        pass


# Bytes held back from every emission for the closing "did not load" line.
RESERVE = 120


def body_limit(cap):
    """The largest page body the hook can ever deliver under `cap`. One
    definition, shared with `claude-index-distill check`, so a page the
    validator accepts is a page the hook can load."""
    return cap - RESERVE


def respond(payload, pages_dir, cap):
    """The additionalContext string for this call, or None."""
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    agent = payload.get("agent_id") or "main"
    state_path = _state_path(payload.get("session_id"))
    emitted = _load_emitted(state_path)

    bodies = []
    overages = []
    total = 0
    changed = False
    matched = False
    skipped = 0
    # Every emitted byte counts: bodies, diagnostics and the "\n\n" between them.
    # 120 bytes stay reserved for the closing "did not load" line.
    room = body_limit(cap)

    def spent(line):
        return len(line.encode("utf-8")) + (2 if bodies or overages else 0)

    for page_path in sorted(Path(pages_dir).glob("*.md"), key=lambda p: p.name):
        if not page_path.is_file():
            continue
        try:
            parsed = parse_page(page_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if parsed is None:
            continue
        patterns, body = parsed
        if not any(matches(p, tool_name, tool_input) for p in patterns):
            continue
        matched = True
        key = "%s|%s" % (agent, os.path.realpath(str(page_path)))
        if key in emitted:
            continue
        if skipped:
            skipped += 1  # a diagnostic already overflowed: count, emit nothing more
            continue
        size = len(body.encode("utf-8"))
        if size > body_limit(cap):
            line = ("[tool-rules] %s is %d bytes, over the %d cap — its rules "
                    "did NOT load. Split it." % (page_path.name, size, body_limit(cap)))
            if total + spent(line) <= room:
                total += spent(line)
                overages.append(line)
                emitted.add(key)
                changed = True
            else:
                skipped += 1
        elif total + spent(body) <= room:
            total += spent(body)
            bodies.append(body)
            emitted.add(key)
            changed = True
        else:
            line = ("[tool-rules] %s did not fit this call's %d-byte budget — "
                    "its rules did NOT load yet; they load on the next matching "
                    "call." % (page_path.name, cap))
            if total + spent(line) <= room:
                total += spent(line)
                overages.append(line)
            else:
                skipped += 1
    if changed:
        _save_emitted(state_path, emitted)
    elif matched:
        # Refresh the mtime so the file outlives the prune while the session is live.
        try:
            os.utime(state_path, None)
        except OSError:
            pass
    parts = bodies + overages
    if skipped:
        parts.append("[tool-rules] %d more page(s) did not load this call "
                     "(output cap %d bytes)." % (skipped, cap))
    return "\n\n".join(parts) if parts else None


def _judge_transcript(path, pages, now, deadline, hook_heartbeat):
    """Stream one main transcript. Returns ("deadline"|"unreadable", started_at) or
    ("judged", (started_at, calls, page names hit))."""
    started_at = None
    pending = {}
    calls = 0
    pages_hit = set()
    cutoff = now - AUDIT_WINDOW_SECONDS
    try:
        with open(path, "rb") as handle:
            for number, line in enumerate(handle, 1):
                if number % hook_heartbeat.CHECK_EVERY_LINES == 0 and time.monotonic() >= deadline:
                    return "deadline", started_at
                marked = b'"tool_use"' in line or b'"tool_result"' in line
                if started_at is not None and not marked:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if started_at is None and isinstance(obj, dict) and isinstance(
                        obj.get("timestamp"), str):
                    started_at = obj["timestamp"]
                if not marked:
                    continue
                for item in hook_heartbeat._items(obj):
                    kind = item.get("type")
                    if kind == "tool_use":
                        name, tool_id = item.get("name"), item.get("id")
                        if not (isinstance(name, str) and isinstance(tool_id, str)
                                and re.fullmatch(HOOK_MATCHER, name)):
                            continue
                        when = hook_heartbeat._epoch(obj.get("timestamp"))
                        if when is None or when < cutoff:
                            continue
                        tool_input = item.get("input")
                        if not isinstance(tool_input, dict):
                            tool_input = {}
                        hit = [page_name for page_name, patterns, mtime in pages
                               if when >= mtime
                               and any(matches(p, name, tool_input) for p in patterns)]
                        if hit:
                            pending[tool_id] = hit
                    elif kind == "tool_result":
                        tool_id = item.get("tool_use_id")
                        if isinstance(tool_id, str) and tool_id in pending \
                                and item.get("is_error") is not True:
                            calls += 1
                            pages_hit.update(pending[tool_id])
    except OSError:
        return "unreadable", started_at
    return "judged", (started_at, calls, pages_hit)


def audit(home, *, workspace_root, exclude_session, deadline, cache=None, skip=(), now=None,
          since=None):
    """Earlier sessions where a live tool-rules page should have loaded and left no record.

    `since` (epoch) judges no call before it: the caller's era, so a session the
    pre-durable hook ran in (compact reset deleted its record) is never named.

    Returns {"active": bool, "flagged": [...], "unchecked": [...], "judged": {...}}.
    Never raises on malformed transcript content."""
    # Lazy: the PostToolUse hook imports this module on every call and needs neither.
    import hook_heartbeat
    import claude_home

    result = {"active": False, "flagged": [], "unchecked": [], "judged": {}}
    pages_dir = tool_rules_dir(workspace_root)
    if pages_dir is None or not pages_dir.is_dir():
        return result
    pages = []
    for page_path in sorted(pages_dir.glob("*.md"), key=lambda p: p.name):
        try:
            if not page_path.is_file():
                continue
            parsed = parse_page(page_path.read_text(encoding="utf-8"))
            mtime = page_path.stat().st_mtime
        except Exception:
            continue
        if parsed is not None:
            pages.append((page_path.name, parsed[0], mtime))
    if not pages:
        return result
    if since is not None:
        # Each page's mtime already bounds which calls count; the era is one more floor.
        pages = [(name, patterns, max(mtime, since)) for name, patterns, mtime in pages]
    result["active"] = True
    sig = hashlib.sha256(json.dumps([[n, m, p] for n, p, m in pages]).encode()).hexdigest()
    now = time.time() if now is None else now
    cache = cache or {}
    window_start = max(now - AUDIT_WINDOW_SECONDS, min(m for _, _, m in pages))
    unchecked = result["unchecked"]

    def note(session, project, reason, size=None, started_at=None):
        unchecked.append({"session": session, "project": project, "reason": reason,
                          "size": size, "started_at": started_at})

    candidates = []
    projects_dir = os.path.join(claude_home.claude_home(home), "projects")
    try:
        with os.scandir(projects_dir) as entries:
            folders = sorted((e.name, e.path) for e in entries if e.is_dir())
    except FileNotFoundError:
        folders = []
    except OSError:
        folders = []
        note(None, "projects", "unreadable")
    for project, folder in folders:
        try:
            with os.scandir(folder) as entries:
                files = sorted((e.name, e.path) for e in entries
                               if e.name.endswith(".jsonl") and e.is_file())
        except OSError:
            note(None, project, "unreadable")
            continue
        for name, path in files:
            stem = name[:-len(".jsonl")]
            try:
                info = os.stat(path)
            except OSError:
                note(stem, project, "unreadable")
                continue
            if info.st_mtime < window_start or stem == exclude_session or stem in skip:
                continue
            if not os.path.isfile(os.path.join(home, hook_heartbeat.RECORD_DIR,
                                               stem + ".fired")):
                continue
            if _state_path(stem).exists():
                continue  # the hook ran for this session: healthy, no read needed
            cached = cache.get(stem)
            if (isinstance(cached, dict) and cached.get("mtime") == info.st_mtime
                    and cached.get("size") == info.st_size and cached.get("sig") == sig):
                result["judged"][stem] = cached
                if cached.get("verdict") == "flagged" and isinstance(cached.get("entry"), dict):
                    result["flagged"].append(cached["entry"])
                continue
            candidates.append((info.st_size, path, stem, project, info.st_mtime))
    candidates.sort(key=lambda c: (c[0], c[1]))

    for index, (size, path, stem, project, mtime) in enumerate(candidates):
        if time.monotonic() >= deadline:
            for rest in candidates[index:]:
                note(rest[2], rest[3], "deadline", rest[0])
            break
        status, detail = _judge_transcript(path, pages, now, deadline, hook_heartbeat)
        if status == "deadline":
            for rest in candidates[index:]:
                note(rest[2], rest[3], "deadline", rest[0],
                     detail if rest is candidates[index] else None)
            break
        if status == "unreadable":
            note(stem, project, "unreadable", size, detail)
            continue
        started_at, calls, pages_hit = detail
        if calls >= 1:
            entry = {"session": stem, "project": project, "started_at": started_at,
                     "calls": calls, "pages": sorted(pages_hit)}
            result["flagged"].append(entry)
            result["judged"][stem] = {"mtime": mtime, "size": size, "sig": sig,
                                      "verdict": "flagged", "entry": entry}
        else:
            result["judged"][stem] = {"mtime": mtime, "size": size, "sig": sig,
                                      "verdict": "ok"}
    return result
