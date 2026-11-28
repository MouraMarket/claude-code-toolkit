"""Decide whether a Bash command starts a heavy tool outside `claude-heavy-run`.

Split from the hook executable on purpose, like `tools/model-guard/guard_lib.py`:
`decide` reports WHICH route produced a verdict (`path`), so a test asserts the
route and not only allow/deny. Two allow routes that look identical (a command
that is not heavy, and one that is heavy but wrapped) can hide each other.

Everything here is pure: no clock, no filesystem, no environment, no config.
The hook reads config and passes the extra program names into `heavy_set`.

Parsing: the command is split into segments on `;`, `&&`, `||`, `|`, `&`,
newline (and subshell parentheses) OUTSIDE quotes, then each segment is
tokenised with `shlex`. The split is done by a small quote-aware scan rather
than by shlex's own `punctuation_chars`, because shlex drops the quotes and so
cannot tell a quoted `";"` argument from a real separator. For each segment the
program word is found by skipping `VAR=value` assignments and the prefixes
`env nohup time nice caffeinate command exec sudo` with their own flags, and its
basename is compared with the heavy set. An interpreter (`python`, `node`,
`bash`, ...) also has the basename of its first non-flag argument checked, with
`.py`/`.sh`/`.js` stripped, so a personal `reel_extract` entry matches
`python3 reel_extract.py`.

The guard FAILS OPEN: anything unparseable (an unbalanced quote) is allowed with
path `unparseable`. A guard that misfires on ordinary work gets switched off.

KNOWN LIMITS, not caught:
- `bash -c "ffmpeg ..."` (the heavy tool is inside a quoted string)
- `xargs ffmpeg`, `find -exec ffmpeg`
- a heavy tool called from inside a script that is not on the list
- anything run outside Claude (the person's own terminal)
The wrapper only serializes jobs that go through it.
"""
import json
import os
import re
import shlex
from collections import namedtuple

# `path` distinguishes the routes; see the module docstring.
Verdict = namedtuple("Verdict", "allowed path reason matched")

ALLOW_NOT_HEAVY = "not_heavy"
ALLOW_WRAPPED = "wrapped"
ALLOW_VERSION_PROBE = "version_probe"
ALLOW_UNPARSEABLE = "unparseable"
DENY_HEAVY = "deny_heavy"

WRAPPER = "claude-heavy-run"

BUILTIN_HEAVY = frozenset({
    "whisper-cli", "whisper", "ffmpeg", "yt-dlp", "llama-server", "llama-cli",
    "ollama", "mlx_lm.generate", "mlx_lm.server", "mlx_lm.lora",
})

INTERPRETERS = frozenset({"python", "python3", "node", "bash", "sh", "zsh"})
SCRIPT_SUFFIXES = (".py", ".sh", ".js")
PROBE_ARGS = frozenset({"--version", "-version", "--help", "-h"})

# Prefixes that run the next word, mapped to the flags that consume a value.
PREFIXES = {
    "env": frozenset({"-u", "-C", "-S"}),
    "nohup": frozenset(),
    "time": frozenset(),
    "nice": frozenset({"-n"}),
    "caffeinate": frozenset({"-t", "-w"}),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "sudo": frozenset({"-u", "-g", "-h", "-p", "-C", "-D", "-r", "-t", "-U"}),
}

_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEPARATORS = ";&|\n()"


def heavy_set(extra):
    """The built-in heavy programs plus any extra names from config.

    Non-string and blank entries are ignored: a hand-edited config must not be
    able to crash the hook or add an empty name that matches nothing useful.
    """
    names = set(BUILTIN_HEAVY)
    if isinstance(extra, (list, tuple, set, frozenset)):
        for name in extra:
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
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


def split_segments(command):
    """Split on unquoted separators. Raises ValueError on an unbalanced quote."""
    segments, current = [], []
    quote = None
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            current.append(ch)
            if ch == "'":
                quote = None
        elif quote == '"':
            current.append(ch)
            if ch == "\\" and i + 1 < n:
                i += 1
                current.append(command[i])
            elif ch == '"':
                quote = None
        elif ch == "\\" and i + 1 < n:
            current.append(ch)
            i += 1
            current.append(command[i])
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch in _SEPARATORS and not _is_redirect_ampersand(command, i):
            segments.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if quote is not None:
        raise ValueError("unbalanced quote")
    segments.append("".join(current))
    return segments


def _is_redirect_ampersand(command, i):
    """`2>&1` and `&>file` use `&` without ending the command."""
    if command[i] != "&":
        return False
    before = command[i - 1] if i > 0 else ""
    after = command[i + 1] if i + 1 < len(command) else ""
    return before in "<>" or after == ">"


def _program_index(tokens):
    """Index of the program word in a token list, or None when there is none."""
    i = 0
    while i < len(tokens):
        word = tokens[i]
        if _ASSIGNMENT.match(word):
            i += 1
            continue
        prefix = os.path.basename(word)
        if prefix in PREFIXES:
            value_flags = PREFIXES[prefix]
            i += 1
            while i < len(tokens):
                nxt = tokens[i]
                if prefix == "command" and nxt in ("-v", "-V"):
                    return None  # a lookup, not an execution
                if nxt.startswith("-"):
                    i += 2 if nxt in value_flags else 1
                elif prefix == "env" and _ASSIGNMENT.match(nxt):
                    i += 1
                else:
                    break
            continue
        return i
    return None


def _judge_segment(tokens, heavy):
    """(kind, matched, args) for one segment; kind is a verdict path or None."""
    idx = _program_index(tokens)
    if idx is None:
        return None, None, None
    program = os.path.basename(tokens[idx])
    if program == WRAPPER:
        return ALLOW_WRAPPED, program, None
    if program in heavy:
        return DENY_HEAVY, program, tokens[idx + 1:]
    if program in INTERPRETERS:
        rest = tokens[idx + 1:]
        for j, arg in enumerate(rest):
            if arg == "-c":
                return None, None, None  # inline code: not inspected
            if arg == "-m" and j + 1 < len(rest):
                name = rest[j + 1]
                if name in heavy:
                    return DENY_HEAVY, name, rest[j + 2:]
                return None, None, None
            if arg.startswith("-"):
                continue
            name = os.path.basename(arg)
            for suffix in SCRIPT_SUFFIXES:
                if name.endswith(suffix):
                    name = name[:-len(suffix)]
                    break
            if name in heavy:
                return DENY_HEAVY, name, rest[j + 1:]
            break
    return None, None, None


def decide(command, heavy):
    """Allow or deny one Bash command. `heavy` comes from `heavy_set`."""
    if not isinstance(command, str):
        return Verdict(True, ALLOW_NOT_HEAVY, None, None)
    try:
        segments = [shlex.split(seg) for seg in split_segments(command)]
    except ValueError:
        return Verdict(True, ALLOW_UNPARSEABLE, None, None)

    wrapped = probe = False
    for tokens in segments:
        kind, matched, args = _judge_segment(tokens, heavy)
        if kind == ALLOW_WRAPPED:
            wrapped = True
        elif kind == DENY_HEAVY:
            if len(args) == 1 and args[0] in PROBE_ARGS:
                probe = True
                continue
            return Verdict(False, DENY_HEAVY, _reason(matched), matched)
    if wrapped:
        return Verdict(True, ALLOW_WRAPPED, None, None)
    if probe:
        return Verdict(True, ALLOW_VERSION_PROBE, None, None)
    return Verdict(True, ALLOW_NOT_HEAVY, None, None)


def _reason(matched):
    return (
        "BLOCKED by claude-load-guard: `%s` is a heavy local job, and several at "
        "once have crashed this Mac. Re-run it as `claude-heavy-run %s ...` "
        "(same arguments): the wrapper holds a lock so only one heavy job runs at "
        "a time, and refuses below the memory floor. LOAD_GUARD_ALLOW=1 skips "
        "this guard, but it is the human's to set, never Claude's."
    ) % (matched, matched)
