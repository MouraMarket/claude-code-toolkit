#!/usr/bin/env python3
"""PreToolUse hook for a trigger-eval child session (skill-trigger's
`run_case`): the child must have no side effect outside its own session, so
this hook denies every tool except a small read-only allowlist.

Delivered via `--settings` (see `skill_trigger_lib.readonly_settings`), not
installed for ordinary sessions. It deliberately fails CLOSED (exit 2 on any
error) -- the inverse of this toolkit's usual "hooks exit 0" rule -- because
it only ever runs inside a disposable child session where fail-open would
defeat the whole containment.
"""

import json
import sys

ALLOWED_TOOLS = frozenset({
    "Read", "Grep", "Glob", "ToolSearch", "TodoWrite", "WebFetch", "WebSearch",
})


def decide(payload):
    """True only when `payload` is a dict whose `tool_name` is a str in
    ALLOWED_TOOLS."""
    if not isinstance(payload, dict):
        return False
    tool_name = payload.get("tool_name")
    return isinstance(tool_name, str) and tool_name in ALLOWED_TOOLS


def _tool_name_repr(payload):
    if isinstance(payload, dict):
        tool_name = payload.get("tool_name")
        return tool_name if isinstance(tool_name, str) else repr(tool_name)
    return "<unparseable input>"


def main():
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        payload = None
        parsed = False
    else:
        parsed = True

    if parsed and decide(payload):
        return 0

    tool_name = _tool_name_repr(payload) if parsed else "<unparseable input>"
    sys.stderr.write(
        "skill-trigger: %s is blocked -- trigger-eval runs are read-only\n"
        % tool_name)
    return 2


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    try:
        code = main()
    except BaseException:
        sys.stderr.write("skill-trigger: readonly_hook crashed -- denying\n")
        code = 2
    sys.exit(code)
