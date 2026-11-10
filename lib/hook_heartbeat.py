"""Reads the records tools/hook-heartbeat/claude-hook-heartbeat writes, and judges
whether a given Claude Code session's core (toolkit-core) plugin hooks fired from THIS
checkout on the CURRENT plugin/hooks/hooks.json (verdict). A profile plugin
(plugin-build, plugin-continuity, ...) is judged separately by profile_verdict, from its
own <session>.<plugin-dir>.start record and its own hooks.json; only core also writes a
.fired record, so a profile verdict proves its SessionStart hooks were dispatched, not
its PreToolUse ones. Stdlib only; importable by path like hook_manifest.py.

This proves hooks are dispatched. It does not prove any hook refuses anything.
"""

import datetime
import hashlib
import json
import os
import re
import sys
import time

RECORD_DIR = os.path.join(".claude", "claude-code-toolkit", "heartbeat")

WINDOW_DAYS = 14
SHELL_TOOLS = ("Bash", "PowerShell", "Monitor")
CHECK_EVERY_LINES = 500

_SESSION_ID = re.compile(r"^[A-Za-z0-9-]+$")
_PLUGIN_DIR = re.compile(r"^plugin-[a-z]+$")


def read(path):
    """Parse a key=value record (split on the FIRST '='); None if missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    record = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            record[key] = value
    return record


def _is_int(value):
    return isinstance(value, str) and re.match(r"^[0-9]+$", value) is not None


def _same_root(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _hooks_sha256(path):
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def verdict(repo, home, session_id):
    """Return (state, message); the first matching state wins."""
    if not session_id or not _SESSION_ID.match(session_id):
        return ("no_session",
                "run this through Claude Code (ask Claude to run it) — only a command "
                "run inside a session can prove that session's hooks fire")
    base = os.path.join(home, RECORD_DIR, session_id)
    fired = read(base + ".fired")
    if fired is None:
        return ("never", "core's (toolkit-core) hooks did not fire in this session — the "
                         "core plugin's hooks are not running here")
    start = read(base + ".start")
    if start is None:
        return ("not_fresh",
                "this session was not started fresh since the heartbeat was installed "
                "(resumed, cleared or reloaded) — start a new session, then re-run")
    if (not _is_int(fired.get("fired_at")) or not fired.get("plugin_root")
            or not _is_int(start.get("started_at")) or not start.get("plugin_root")):
        return ("unreadable", "this session's heartbeat records are incomplete or "
                              "malformed — start a new session, then re-run")
    expected = os.path.join(repo, "plugin")
    for name, record in (("fired", fired), ("start", start)):
        if not _same_root(record["plugin_root"], expected):
            return ("foreign",
                    "this session's hooks ran from %s (%s record), not %s — run this "
                    "from the checkout the plugin is registered from"
                    % (record["plugin_root"], name, expected))
    current = _hooks_sha256(os.path.join(expected, "hooks", "hooks.json"))
    recorded = start.get("hooks_sha256", "")
    if not recorded or recorded != current:
        return ("stale", "the hooks changed after this session loaded them — start a "
                         "new session, then re-run")
    return ("proven", "core's (toolkit-core) hooks fired, from %s, on the current "
                      "plugin/hooks/hooks.json; the other profile plugins' hooks are "
                      "not covered by this check" % expected)


def profile_verdict(repo, home, session_id, plugin_dir):
    """Return (state, message) for a profile plugin dir such as "plugin-build".

    Judged on the session's <plugin_dir>.start record alone. States: no_session,
    never (no record: the plugin's SessionStart hook did not run in this session),
    unreadable, foreign, stale, proven."""
    if not isinstance(plugin_dir, str) or not _PLUGIN_DIR.match(plugin_dir):
        raise ValueError("plugin_dir must look like plugin-<name>: %r" % (plugin_dir,))
    if not session_id or not _SESSION_ID.match(session_id):
        return ("no_session",
                "run this through Claude Code (ask Claude to run it) — only a command "
                "run inside a session can prove that session's hooks fire")
    start = read(os.path.join(home, RECORD_DIR, "%s.%s.start" % (session_id, plugin_dir)))
    if start is None:
        return ("never",
                "%s's hooks did not run in this session (no start record) — the plugin "
                "is not enabled here, or the session was not started fresh since it was "
                "installed; start a new session, then re-run" % plugin_dir)
    if not _is_int(start.get("started_at")) or not start.get("plugin_root"):
        return ("unreadable", "this session's %s heartbeat record is incomplete or "
                              "malformed — start a new session, then re-run" % plugin_dir)
    expected = os.path.join(repo, plugin_dir)
    if not _same_root(start["plugin_root"], expected):
        return ("foreign",
                "this session's %s hooks ran from %s, not %s — run this from the "
                "checkout the plugin is registered from"
                % (plugin_dir, start["plugin_root"], expected))
    current = _hooks_sha256(os.path.join(expected, "hooks", "hooks.json"))
    recorded = start.get("hooks_sha256", "")
    if not recorded or recorded != current:
        return ("stale", "the %s hooks changed after this session loaded them — start a "
                         "new session, then re-run" % plugin_dir)
    return ("proven", "%s's SessionStart hooks ran, from %s, on the current "
                      "%s/hooks/hooks.json; its other hooks are not covered by this check"
            % (plugin_dir, expected, plugin_dir))


def _lazy_imports():
    lib = os.path.dirname(os.path.realpath(__file__))
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import claude_home
    import install_record
    return claude_home, install_record


def era(home):
    """Epoch float of the install record's first_installed_at, or None.

    A record written before that key existed falls back to its installed_at: a
    real install time, never earlier than the truth, so it cannot admit a session
    from before the hooks were installed -- and a git pull alone (no re-install)
    does not leave the audit switched off."""
    _, install_record = _lazy_imports()
    record = install_record.load(install_record.record_path(home)) or {}
    value = record.get("first_installed_at") or record.get("installed_at")
    try:
        parsed = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=datetime.timezone.utc).timestamp()


def _epoch(stamp):
    """Epoch float of a transcript's ISO-8601 'Z' timestamp, or None."""
    try:
        parsed = datetime.datetime.fromisoformat(stamp.rstrip("Z"))
    except (AttributeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.timestamp()


def _result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(item["text"] for item in content
                       if isinstance(item, dict) and isinstance(item.get("text"), str))
    return ""


def _items(obj):
    message = obj.get("message") if isinstance(obj, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    return [item for item in content if isinstance(item, dict)] if isinstance(content, list) else []


def _judge(path, window_start, deadline):
    """Stream one transcript. Returns ("deadline"|"unreadable", started_at) or
    ("judged", (start, ran))."""
    start = None
    shell_ids = set()
    ran = 0
    try:
        with open(path, "rb") as handle:
            for number, line in enumerate(handle, 1):
                if number % CHECK_EVERY_LINES == 0 and time.monotonic() >= deadline:
                    return "deadline", start
                marked = b'"tool_use"' in line or b'"tool_result"' in line
                if start is not None and not marked:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if start is None and isinstance(obj, dict) and isinstance(
                        obj.get("timestamp"), str):
                    start = obj["timestamp"]
                    began = _epoch(start)
                    if began is None or began < window_start:
                        return "judged", (start, 0)
                if not marked:
                    continue
                for item in _items(obj):
                    if item.get("type") == "tool_use" and item.get("name") in SHELL_TOOLS \
                            and isinstance(item.get("id"), str):
                        shell_ids.add(item["id"])
                    elif item.get("type") == "tool_result" \
                            and isinstance(item.get("tool_use_id"), str) \
                            and item["tool_use_id"] in shell_ids:
                        if item.get("is_error") is not True or _result_text(
                                item.get("content")).startswith("Exit code "):
                            ran += 1
    except OSError:
        return "unreadable", start
    return "judged", (start, ran)


def audit(home, *, exclude_session, deadline, cache=None, skip=(), now=None):
    """Find past sessions that ran a shell command but left no .fired record.

    Returns {"window_start", "flagged", "unchecked", "judged"}; never raises on
    malformed transcript content."""
    claude_home, _ = _lazy_imports()
    now = time.time() if now is None else now
    cache = cache or {}
    result = {"window_start": None, "flagged": [], "unchecked": [], "judged": {}}
    first = era(home)
    if first is None:
        return result
    window_start = max(first, now - WINDOW_DAYS * 86400)
    result["window_start"] = window_start
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
            # A regular file only: the writer never makes anything else here.
            if os.path.isfile(os.path.join(home, RECORD_DIR, stem + ".fired")):
                continue
            cached = cache.get(stem)
            if (isinstance(cached, dict) and cached.get("mtime") == info.st_mtime
                    and cached.get("size") == info.st_size):
                result["judged"][stem] = cached
                continue
            candidates.append((info.st_size, path, stem, project, info.st_mtime))
    candidates.sort(key=lambda c: (c[0], c[1]))

    for index, (size, path, stem, project, mtime) in enumerate(candidates):
        if time.monotonic() >= deadline:
            for rest in candidates[index:]:
                note(rest[2], rest[3], "deadline", rest[0])
            break
        status, detail = _judge(path, window_start, deadline)
        if status == "deadline":
            for rest in candidates[index:]:
                note(rest[2], rest[3], "deadline", rest[0],
                     detail if rest is candidates[index] else None)
            break
        if status == "unreadable":
            note(stem, project, "unreadable", size, detail)
            continue
        start, ran = detail
        # No start means the session cannot be placed in the window: ok, never flagged.
        flagged = ran >= 1 and start is not None
        verdict_name = "flagged" if flagged else "ok"
        if flagged:
            result["flagged"].append({"session": stem, "project": project,
                                      "started_at": start, "ran": ran})
        result["judged"][stem] = {"mtime": mtime, "size": size, "verdict": verdict_name}
    return result
