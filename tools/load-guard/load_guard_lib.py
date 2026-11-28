"""Pure helpers for the load guard: the shim set, the memory reading, the holder record.

`claude-heavy-run` is the only consumer. Everything here is pure: no clock, no
filesystem, no environment, no config; the wrapper reads config and passes the
extra tool names into `heavy_set`.

The guard works by PATH shims: `~/.local/bin/<tool>` is a symlink to
`claude-heavy-run`, which then runs the real tool under the lock. A call by
absolute path (`/opt/homebrew/bin/ffmpeg`) bypasses the shim.
"""
import json
import os
import re

# The tools that get a shim by default. yt-dlp is absent on purpose: its heavy
# step is an ffmpeg merge, which the ffmpeg shim catches.
BUILTIN_HEAVY = frozenset({
    "ffmpeg", "whisper-cli", "llama-server", "llama-cli", "llama-bench",
})

# argv after the tool that is exactly one of these is a light call: no lock.
LIGHT_ARGS = frozenset({"--version", "-version", "--help", "-h", "-help"})


def heavy_set(extra):
    """The built-in heavy programs plus any extra names from config.

    Non-string and blank entries are ignored: a hand-edited config must not be
    able to crash the wrapper or add an empty name that matches nothing useful.
    Each name is reduced to its basename, because a shim is named by basename.
    """
    names = set(BUILTIN_HEAVY)
    if isinstance(extra, (list, tuple, set, frozenset)):
        for name in extra:
            if isinstance(name, str) and os.path.basename(name.strip()):
                names.add(os.path.basename(name.strip()))
    return frozenset(names)


def parse_level(text):
    """The integer percent in `sysctl -n kern.memorystatus_level` output, or None.

    None for anything but a bare integer 0-100: the wrapper treats an
    unreadable floor as a refusal, so a loose parse here would turn garbage
    into a number it then trusts.
    """
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if not re.fullmatch(r"[0-9]{1,3}", cleaned):
        return None
    value = int(cleaned)
    return value if value <= 100 else None


def format_holder(pid, argv, started):
    """The one-line record the wrapper writes into the lock file."""
    return json.dumps({"pid": pid, "argv": list(argv), "started": started}) + "\n"


def parse_holder(text):
    """A dict with `pid`, `argv`, `started` from a lock file's text, or None.

    None for an empty or foreign file, which is what a lock file looks like
    between creation and the holder writing its record.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    pid, argv, started = data.get("pid"), data.get("argv"), data.get("started")
    if isinstance(pid, bool) or not isinstance(pid, int):
        return None
    if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
        return None
    if isinstance(started, bool) or not isinstance(started, (int, float)):
        return None
    return {"pid": pid, "argv": argv, "started": started}
