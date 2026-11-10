#!/usr/bin/env python3
"""Where Claude Code keeps its config -- one resolver, shared by every
toolkit tool that needs it.

`$CLAUDE_CONFIG_DIR` when set (env read at CALL time, never cached) wins,
expanded; else `<home>/.claude` when a sandboxed `home` is given, else
`~/.claude` expanded. An empty-string env value counts as unset.
"""

import os


def claude_home(home=None):
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.expanduser(env)
    if home:
        return os.path.join(home, ".claude")
    return os.path.expanduser("~/.claude")


def user_rules_dir(home=None):
    return os.path.join(claude_home(home), "rules")
