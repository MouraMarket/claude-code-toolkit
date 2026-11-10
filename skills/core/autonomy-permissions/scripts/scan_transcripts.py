#!/usr/bin/env python3
"""Mine Claude Code transcripts for evidence about what permissions to grant.

The point of this script is NOT to enumerate every command you have ever run.
It is to answer four questions that actually determine your settings:

  1. Which command families do you use enough to be worth a standing grant?
  2. Which of those are *already* free (Claude Code has a built-in read-only
     set that never prompts), so a rule for them would be pure noise?
  3. What destructive command shapes appear in your real history, so the deny
     side is grounded in your behaviour rather than in a generic scare list?
  4. **Which non-Bash tool calls -- MCP above all -- are uncovered by any
     rule?** This one was invisible until 2026-08-29, and its absence is what
     let hand-enumerated MCP rules pile up across three settings files.

## What "would prompt" means here, and what it cannot mean

Transcripts record **rejections** but not **approvals**: a call you approved
once looks identical to a call that never prompted. So this script cannot count
the prompts you actually saw, and it does not claim to. It models which calls
are uncovered by any rule in your merged settings -- which is better evidence
anyway, because it also surfaces the tools you approved once and forgot.

Under `permissions.defaultMode: auto` an uncovered call is then judged by the
classifier, which may well allow it silently. Read the uncovered count as "this
is where a prompt CAN happen", not as a tally of interruptions.

Usage:
    python3 scan_transcripts.py [--limit N] [--project DIR] [--json]

    --limit N     analyse the most recent N Bash invocations (default 100).
                  The tool-call census below covers the same recent window.
    --project DIR transcript directory (default: derived from your configured
                  workspace_root, falling back to the most recently used one).
                  Worktree sessions get their OWN project dir, so a single
                  --project misses them; pass each one to see full coverage.
    --workspace D workspace root, for locating project/local settings scopes
    --json        emit machine-readable findings instead of the report
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import permission_rules as rules_lib  # noqa: E402

# ---------------------------------------------------------------------------
# Claude Code runs these without a prompt in EVERY permission mode. Writing
# allow rules for them is the single most common way a mined allowlist ends up
# looking impressive while changing nothing. Source: docs "Read-only commands".
# ---------------------------------------------------------------------------
ALWAYS_FREE = {
    "ls", "cat", "echo", "pwd", "head", "tail", "grep", "wc",
    "which", "diff", "stat", "du", "cd", "true", "false", "test",
}

# Read-only git is also in the built-in set, but the verdict is per ARGUMENTS,
# not per subcommand family (R4-05): `git branch -D x` and `git config k v`
# mutate while `git branch` and `git config k` read, so a family list called
# destructive forms free. Anything this cannot positively recognise counts as
# not free -- the native verdict is unknown, and unsure must mean "prompts".
_GIT_GLOBAL_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace",
                          "--exec-path"}
_GIT_PLAIN_READS = {"log", "status", "diff", "show", "rev-parse", "describe",
                    "blame", "shortlog", "ls-files", "merge-base"}
_BRANCH_SHORT = set("arvlq")
_BRANCH_LONG = {"--list", "--all", "--remotes", "--verbose", "--quiet",
                "--show-current", "--column", "--no-column", "--color",
                "--no-color", "--abbrev", "--no-abbrev", "--ignore-case",
                "--omit-empty"}
_BRANCH_VALUE = {"--contains", "--no-contains", "--merged", "--no-merged",
                 "--points-at", "--sort", "--format"}
_CONFIG_READ = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l",
                "--list"}
_CONFIG_VALUE = {"-f", "--file", "--blob", "--type", "--default"}
_CONFIG_MODIFIER = {"--global", "--system", "--local", "--worktree",
                    "--show-origin", "--show-scope", "-z", "--null",
                    "--name-only", "--includes", "--no-includes", "--bool",
                    "--int", "--bool-or-int", "--path", "--expiry-date"}


def git_subcommand(args: list[str]) -> tuple[str, list[str]]:
    """Split the tokens after `git` into (subcommand, rest), skipping global options."""
    # Callers split on whitespace, so `"--output=/x"` arrives with its quotes;
    # the shell would strip them, and every check below must see what git sees.
    args = [a.strip("\"'") for a in args]
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _GIT_GLOBAL_VALUE_OPTS else 1
    if i >= len(args):
        return "", []
    return args[i].strip("\"'"), args[i + 1:]


def _branch_is_free(rest: list[str]) -> bool:
    list_mode = False
    positionals = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        i += 1
        if tok.startswith("--"):
            name = tok.split("=", 1)[0]
            if name in _BRANCH_LONG:
                list_mode = list_mode or name == "--list"
            elif name in _BRANCH_VALUE:
                if "=" not in tok and i < len(rest) and not rest[i].startswith("-"):
                    i += 1
            else:
                return False
        elif tok.startswith("-") and len(tok) > 1:
            letters = set(tok[1:])
            if not letters <= _BRANCH_SHORT:
                return False
            list_mode = list_mode or "l" in letters
        else:
            positionals.append(tok)
    return list_mode or not positionals


def _config_is_free(rest: list[str]) -> bool:
    read_flag = False
    positionals = []
    i = 0
    while i < len(rest):
        tok = rest[i]
        i += 1
        if tok == "--":
            positionals.extend(rest[i:])
            break
        if not tok.startswith("-"):
            positionals.append(tok)
        elif tok in _CONFIG_READ:
            read_flag = True
        elif tok in _CONFIG_VALUE:
            i += 1  # the operand is never inspected
        elif tok.startswith("--") and tok.split("=", 1)[0] in _CONFIG_VALUE and "=" in tok:
            pass
        elif tok in _CONFIG_MODIFIER:
            pass
        else:
            return False  # destructive options and git's abbreviations of them
    if read_flag:
        return True
    if positionals and positionals[0] in ("get", "list"):
        return True
    return len(positionals) == 1


def git_is_free(args: list[str]) -> bool:
    """Is this git invocation (tokens after `git`) in the built-in read-only set?"""
    # A config override can name a program git runs (core.fsmonitor,
    # core.pager), so `git -c k=v status` is not a plain read.
    norm = [a.strip("\"'") for a in args]
    i = 0
    while i < len(norm) and norm[i].startswith("-"):
        if norm[i].startswith("-c") or norm[i].startswith("--config-env"):
            return False
        i += 2 if norm[i] in _GIT_GLOBAL_VALUE_OPTS else 1
    sub, rest = git_subcommand(args)
    if sub in _GIT_PLAIN_READS:
        # git accepts unambiguous abbreviations of long options, so
        # `--outp=x` writes a file just like `--output=x`.
        for tok in rest:
            name = tok.split("=", 1)[0]
            if len(name) >= 4 and "--output".startswith(name):
                return False
        return True
    if sub == "branch":
        return _branch_is_free(rest)
    if sub == "remote":
        while rest and rest[0] in ("-v", "--verbose"):
            rest = rest[1:]
        return not rest or rest[0] in ("show", "get-url")
    if sub == "config":
        return _config_is_free(rest)
    return False


# `find` is judged by its arguments too, not by its head: `find . -delete` and
# `find . -exec rm {} +` delete files or run arbitrary code, and Claude Code
# never lets a prefix rule cover them (only an exact-match rule does). This
# mirrors the native read-only check of 2.1.292: the same refused primaries, the
# BSD combined-flag form (`-sf`), and an unquoted glob. One deliberate
# divergence: native skips the token after a value-taking primary (`-name`), so
# natively `find . -name -delete` is read-only; we check EVERY token, so that
# spelling reads not free. Unsure (substitution, open quote) means not free.
_FIND_MUTATING = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-rm",
                  "-fprint", "-fprint0", "-fprintf", "-fls", "-files0-from"}
_FIND_BSD_F = re.compile(r"^-[dsx]+f$")


def _shell_words(text: str) -> list[tuple[str, bool]] | None:
    """Split `text` the way the shell would; each word carries an unquoted-glob flag.

    Returns None (unsure) when the text holds `$`, a backtick, `<(` or `>(`, an
    unescaped `(` or `)`, ends inside an open quote, or ends on a backslash.
    """
    if "$" in text or "`" in text or "<(" in text or ">(" in text:
        return None
    words: list[tuple[str, bool]] = []
    current: list[str] = []
    glob = False
    in_word = False
    quote = ""  # "", "'" or '"'
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            elif ch == "\\" and quote == '"' and i + 1 < len(text):
                i += 1
                current.append(text[i])
            else:
                current.append(ch)
        elif ch in "'\"":
            quote = ch
            in_word = True
        elif ch == "\\":
            # A dangling escape is a line continuation the segment splitter
            # cut (`find . -de\<newline>lete`): what follows is unknown.
            if i + 1 >= len(text):
                return None
            in_word = True
            i += 1
            current.append(text[i])
        elif ch.isspace():
            if in_word:
                words.append(("".join(current), glob))
                current, glob, in_word = [], False, False
        elif ch in "()":
            return None  # unescaped grouping is shell syntax, not a find word
        else:
            in_word = True
            if ch in "*?[":
                glob = True
            current.append(ch)
        i += 1
    if quote:
        return None
    if in_word:
        words.append(("".join(current), glob))
    return words


def find_is_free(args: list[str]) -> bool:
    """Is this find invocation (tokens after `find`) in the built-in read-only set?"""
    words = _shell_words(" ".join(args))
    if words is None:
        return False
    for word, has_glob in words:
        if has_glob or word in _FIND_MUTATING or _FIND_BSD_F.match(word):
            return False
    return True

# Commands that execute whatever you hand them. A prefix rule on these is
# equivalent to allowing arbitrary code, because Claude Code does not strip
# them as wrappers -- `Bash(npx *)` permits `npx <anything>`.
EXEC_WRAPPERS = {
    "npx", "uvx", "bunx", "docker", "devbox", "mise", "direnv",
    "sh", "bash", "zsh", "perl", "ruby", "osascript", "eval",
    "sudo", "xargs",
}

# What Claude Code strips before matching a rule, and what a prefix rule can
# never auto-approve. Source: https://code.claude.com/docs/en/permissions,
# sections "Wrappers" and "Exec wrappers and `find` actions", read 2026-10-25.
# `sudo` is absent on purpose: Claude Code matches `sudo rm x` as a `sudo`
# command, so `Bash(rm *)` does not cover it. Bare `xargs` (no flags) is also
# stripped; see `strip_wrappers`.
STRIPPED_WRAPPERS = {"timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin", "noglob"}
PREFIX_UNAPPROVABLE = {"watch", "setsid", "ionice", "flock"}

# Multiplexed tools where the subcommand, not the binary, is the useful unit.
SUBCOMMAND_TOOLS = {
    "git", "gh", "npm", "pnpm", "yarn", "brew", "cargo", "docker",
    "pip", "pip3", "uv", "wt", "kubectl", "terraform",
}

DANGER_SHAPES = [
    ("recursive-delete",   r"\brm\s+(?:-\w+\s+)*-\w*[rf]\w*\b"),
    ("force-push",         r"\bgit\s+push\b[^\n]*(?:--force|-f)\b"),
    ("hard-reset",         r"\bgit\s+reset\b[^\n]*--hard\b"),
    ("git-clean",          r"\bgit\s+clean\b[^\n]*-\w*[fdx]"),
    ("history-rewrite",    r"\bgit\s+(?:filter-branch|push[^\n]*--mirror)\b"),
    ("pipe-to-shell",      r"\b(?:curl|wget)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z)?sh\b"),
    ("sudo",               r"(?:^|[;&|]\s*)sudo\s"),
    ("permissive-chmod",   r"\bchmod\s+(?:-\w+\s+)*(?:777|a\+w)\b"),
    ("disk-write",         r"\b(?:dd|mkfs|fdisk|diskutil)\b"),
    ("credential-path",    r"(?:\.ssh/|\.aws/credentials|\.netrc|id_rsa|\.gnupg/|credentials\.json)"),
    ("curl-to-file",       r"\b(?:curl|wget)\b[^\n]*\s>\s*[^\s]"),
]

# Heredoc bodies are data, not commands. Left in, they dominate the token
# counts with prose and make the whole scan useless. Group 2 is the rest of the
# opening line (`> out`, `| python3`), which is shell syntax and is kept.
HEREDOC = re.compile(
    r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?([^\n]*)\n.*?^\s*\1\s*$",
    re.DOTALL | re.MULTILINE,
)
# Claude Code's separators: `&&`, `||`, `;`, `|`, `|&`, `&`, newline. `|&` is
# matched before `|`, or its `&` starts the next segment and the command after
# it reads as quote bleed. A lone `&` is a separator only when it is not part of
# a redirect (`2>&1`, `>&2`, `&>file`), which is what the lookarounds exclude.
SEPARATORS = re.compile(r"\|\||&&|\|&|[;\n]|(?<![>&])&(?![>&])|(?<!>)\|(?!\|)")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Allow rules match past a leading assignment only for these names; ask and deny
# rules match past any. Read out of the 2.1.280 binary (`vF`) on 2026-10-15;
# re-read it before trusting a change in this behaviour.
SAFE_ENV_VARS = frozenset((
    "GOEXPERIMENT", "GOOS", "GOARCH", "CGO_ENABLED", "GO111MODULE", "RUST_BACKTRACE",
    "RUST_LOG", "NODE_ENV", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_DEBUG", "ANTHROPIC_API_KEY", "LANG",
    "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_TIME", "CHARSET", "TERM", "COLORTERM",
    "NO_COLOR", "FORCE_COLOR", "TZ", "LS_COLORS", "LSCOLORS", "GREP_COLOR",
    "GREP_COLORS", "GCC_COLORS", "TIME_STYLE", "BLOCK_SIZE", "BLOCKSIZE", "COLUMNS",
    "LINES", "CLICOLOR", "CLICOLOR_FORCE", "CI", "DEBIAN_FRONTEND", "GIT_TERMINAL_PROMPT",
))

# Each stripped wrapper's own arguments, as Claude Code strips them: read out of
# the 2.1.306 binary (`Fk`) on 2026-10-25; re-read it before trusting a change
# in this behaviour. `timeout` and `stdbuf` go only when their arguments parse,
# otherwise the wrapper stays the head. `time`, `nice` and `nohup` lose the word
# either way, so `time -p x` is matched as `-p x`. `command`, `builtin` and
# `noglob` are never stripped before a flag, which keeps `command -v` a command.
# Every form needs a command after it.
_TIMEOUT_VALUE = re.compile(r"^[A-Za-z0-9_.+-]+$")
_TIMEOUT_GLUED = re.compile(r"^(?:--(?:kill-after|signal)=|-[ks])[A-Za-z0-9_.+-]+$")
_DURATION = re.compile(r"^\d+(?:\.\d+)?[smhd]?$")
_STDBUF_MODE = re.compile(r"^-[ioe][LN0-9]+$")


def _wrapper_width(tokens: list[str]) -> int:
    """How many leading tokens the wrapper at `tokens[0]` spans, or 0 if not stripped."""
    head, n = tokens[0], 1

    def at(i: int) -> str | None:
        return tokens[i] if i < len(tokens) else None

    if head == "timeout":
        while at(n) is not None:
            if at(n) in ("--foreground", "--preserve-status", "--verbose", "-v") \
                    or _TIMEOUT_GLUED.match(at(n)):
                n += 1
            elif at(n) in ("--kill-after", "--signal", "-k", "-s") \
                    and at(n + 1) is not None and _TIMEOUT_VALUE.match(at(n + 1)):
                n += 2
            else:
                break
        if at(n) == "--":
            n += 1
        if at(n) is None or not _DURATION.match(at(n)):
            return 0
        n += 1
    elif head == "stdbuf":
        while at(n) is not None and _STDBUF_MODE.match(at(n)):
            n += 1
        if n == 1:
            return 0
        if at(n) == "--" and at(n + 1) is not None:
            n += 1
    elif head == "nice":
        if at(1) == "-n" and at(2) is not None and re.match(r"^-?\d+$", at(2)) and at(3) is not None:
            n = 3
        elif at(1) is not None and re.match(r"^-\d+$", at(1)) and at(2) is not None:
            n = 2
        if at(n) == "--" and at(n + 1) is not None:
            n += 1
    elif head in ("time", "nohup"):
        if at(1) == "--" and at(2) is not None:
            n = 2
    elif head in ("command", "builtin", "noglob"):
        while head == "command" and at(n) is not None and re.match(r"^-p+$", at(n)):
            n += 1
        if head != "noglob" and at(n) == "--":
            n += 1
        if at(n) is None or at(n).startswith("-"):
            return 0
    else:
        return 0
    return n if at(n) is not None else 0


def strip_wrappers(tokens: list[str], safe_env_only: bool = False,
                   bare_xargs: bool = False) -> list[str]:
    """Drop what Claude Code strips from the front of a command before matching.

    Leading assignments and `STRIPPED_WRAPPERS` with their own arguments (see
    `_wrapper_width`), repeated until none applies.
    `safe_env_only` is the allow-rule view: an assignment is popped only if its
    name is in `SAFE_ENV_VARS`, and the strip stops at the first unsafe one.
    `bare_xargs` also pops `xargs` when a next token exists and is not a flag,
    and stops there: the matcher accepts only `xargs ` plus the literal rule,
    so nothing after it is stripped. Only classification sets it: rule matching
    leaves `xargs` to `bash_rule_matches`, which models it per bucket.
    """
    tokens = list(tokens)
    while tokens:
        head = tokens[0]
        width = 1
        if ENV_ASSIGN.match(head):
            if safe_env_only and head.split("=", 1)[0] not in SAFE_ENV_VARS:
                break
        elif head in STRIPPED_WRAPPERS:
            width = _wrapper_width(tokens)
            if not width:
                break
        elif bare_xargs and head == "xargs" and len(tokens) > 1 and not tokens[1].startswith("-"):
            return tokens[1:]
        else:
            break
        del tokens[:width]
    return tokens


PLAUSIBLE = re.compile(r"^[a-z0-9][a-z0-9._+-]*$", re.IGNORECASE)


def load_events(project_dir: str) -> list[tuple[str, str, dict]]:
    """Return (timestamp, tool_name, tool_input) in chronological order."""
    events: list[tuple[str, str, dict]] = []
    files = sorted(glob.glob(os.path.join(project_dir, "*.jsonl")),
                   key=os.path.getmtime)
    for path in files:
        with open(path, errors="replace", encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                content = (rec.get("message") or {}).get("content")
                if not isinstance(content, list):
                    continue
                ts = rec.get("timestamp", "")
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        events.append((ts, block.get("name", ""), block.get("input") or {}))
    return events


def subcommands(command: str):
    """Split a shell command into the pieces permission rules match against.

    Claude Code evaluates each subcommand of a compound command independently,
    so this mirrors that: a rule that covers `git status` does not cover
    `git status && rm -rf x`.
    """
    stripped = HEREDOC.sub(lambda m: " " + m.group(2), command)
    for part in SEPARATORS.split(stripped):
        part = part.strip()
        if part and not part.startswith("#"):
            yield part


def output_redirects(segment: str) -> list[str] | None:
    """Targets of the output redirects in one subcommand, or None if unreadable.

    Claude Code judges a redirect's write separately from the command, so a
    command with a write is never free and never prefix-covered. `[]` means no
    write. `None` means a redirect was seen whose target cannot be read
    (`>(...)`, a bare trailing `>`); callers treat it as a write.
    Exempt: `/dev/null` and an fd copy (`>&` with digits or `-`).
    """
    targets: list[str] = []
    n = len(segment)
    i = 0
    quote = ""
    last_amp = -2  # index of the latest unquoted, unescaped `&`
    while i < n:
        ch = segment[i]
        if quote == "'":
            if ch == "'":
                quote = ""
            i += 1
            continue
        if quote == '"':
            if ch == "\\" and i + 1 < n and segment[i + 1] in '"\\$`':
                i += 2
                continue
            if ch == '"':
                quote = ""
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "#" and (i == 0 or segment[i - 1] in " \t;&|("):
            return targets  # an unquoted word-initial `#` starts a comment
        if ch == "&":
            last_amp = i
            i += 1
            continue
        if ch != ">":
            i += 1
            continue
        # An unquoted `>`: find the operator.
        amp_prefixed = (last_amp == i - 1 and (i < 2 or segment[i - 2] not in "&>"))
        j = i + 1
        fd_copy_op = False
        if j < n and segment[j] == ">":
            j += 1
        elif j < n and segment[j] == "|":
            j += 1
        elif j < n and segment[j] == "&" and not amp_prefixed:
            fd_copy_op = True
            j += 1
        if j < n and segment[j] == "(":
            return None
        while j < n and segment[j] in " \t":
            j += 1
        word = ""
        wq = ""
        while j < n:
            c = segment[j]
            if wq == "'":
                if c == "'":
                    wq = ""
                else:
                    word += c
            elif wq == '"':
                if c == "\\" and j + 1 < n and segment[j + 1] in '"\\$`':
                    word += segment[j + 1]
                    j += 1
                elif c == '"':
                    wq = ""
                else:
                    word += c
            elif c == "\\" and j + 1 < n:
                word += segment[j + 1]
                j += 1
            elif c in "'\"":
                wq = c
            elif c in " \t;|&<>()":
                break
            else:
                word += c
            j += 1
        if wq:
            return targets  # unterminated quote: the tail is quoted text
        if not word:
            return None
        i = j
        if fd_copy_op and (word.isdigit() or word == "-"):
            continue
        if word == "/dev/null":
            continue
        targets.append(word)
    return targets


def classify(segment: str) -> tuple[str, str] | None:
    """Return (family, reason) for a subcommand, or None if it is not parseable.

    A write through an output redirect overrides every reason: the family gains
    a trailing ` >` and the reason becomes `no-prefix`.
    """
    result = _classify_command(segment)
    if result is None:
        return None
    family, _ = result
    writes = output_redirects(segment)
    if writes is None or writes:
        return family + " >", "no-prefix"
    return result


def _classify_command(segment: str) -> tuple[str, str] | None:
    """The command half of `classify`: (family, reason), ignoring redirects.

    `family` is the unit a permission rule should target -- `git commit`, not
    `git`, because `git` spans both free reads and prompting writes.
    """
    tokens = strip_wrappers(segment.split(), bare_xargs=True)
    if not tokens:
        return None
    result = _classify_tokens(tokens)
    # Under a bare `xargs` the inner family's prefix rule covers the command
    # (`Bash(grep *)` matches `xargs grep x`), but nothing models whether the
    # built-in read-only set does, and replay says it prompts. So propose the
    # rule rather than call it free.
    if result and result[1] == "already-free" and tokens != strip_wrappers(segment.split()):
        return result[0], "grantable"
    return result


def _classify_tokens(tokens: list[str]) -> tuple[str, str] | None:
    """`_classify_command` past the wrapper strip."""
    raw = tokens[0].strip("\"'()`")

    # A command invoked by path -- `/Users/me/.claude/plugins/.../task-brief`,
    # `$SDD/scripts/review-package`, `./build.sh` -- is NOT a command family.
    # Collapsing it to its basename (the old behaviour here) made these look
    # like ordinary grantable families, which is how an enumerated allowlist
    # ends up silently missing the single largest source of real prompts:
    # no `Bash(<name> *)` rule matches a command spelled as a path, and the
    # path is frequently a variable that only resolves at runtime.
    if raw.startswith("$("):
        return None  # command substitution, not a command being invoked
    if raw.startswith(("/", "./", "../", "~", "$")) or raw.startswith('"$'):
        return raw, "path-invoked"

    head = os.path.basename(raw).strip("$")
    if not PLAUSIBLE.match(head):
        return None  # quote/heredoc bleed, not a real command

    if head in SUBCOMMAND_TOOLS:
        if head == "git":
            sub, _ = git_subcommand(tokens[1:])
            family = f"git {sub}".strip()
            return family, "already-free" if git_is_free(tokens[1:]) else "grantable"
        sub = ""
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            sub = tok
            break
        family = f"{head} {sub}".strip()
        return family, "exec-wrapper" if head in EXEC_WRAPPERS else "grantable"

    if head in PREFIX_UNAPPROVABLE:
        return head, "no-prefix"
    if head == "find":
        return "find", ("already-free" if find_is_free(tokens[1:]) else "no-prefix")
    if head in ALWAYS_FREE:
        return head, "already-free"
    # A stripped wrapper still at the head is one whose arguments did not parse
    # (`stdbuf -o L x`). It runs its argument all the same, so `Bash(stdbuf *)`
    # would approve `stdbuf -o L rm -rf x`.
    if head in EXEC_WRAPPERS or head in STRIPPED_WRAPPERS:
        return head, "exec-wrapper"
    return head, "grantable"


PROJECTS_DIR = os.path.expanduser("~/.claude/projects")


def _configured_workspace() -> str | None:
    """The configured workspace root, used to locate the settings scopes.

    `realpath`, not `abspath` -- these scripts are reached through a symlinked
    skill directory, and `abspath` would not resolve it, landing the `lib`
    lookup somewhere that does not exist.
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.realpath(__file__)))))), "lib"))
    try:
        import toolkit_config
        return toolkit_config.workspace_root()
    except Exception:
        return None


def default_project_dir() -> str:
    """Transcript directory for the configured workspace, or the newest one.

    Claude Code names a project directory after its path with `/` and `_` both
    replaced by `-`, so /Users/you/_Work becomes -Users-you--Work. Deriving it
    beats hardcoding one person's path; if the derived directory does not exist
    (no session has run there yet) fall back to whichever project was used most
    recently, which is nearly always the one the user means.
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.realpath(__file__)))))), "lib"))
    root = None
    try:
        import toolkit_config
        root = toolkit_config.workspace_root()
    except Exception:
        pass
    if root:
        derived = os.path.join(
            PROJECTS_DIR, root.replace("/", "-").replace("_", "-"))
        if os.path.isdir(derived):
            return derived
    try:
        candidates = [os.path.join(PROJECTS_DIR, name)
                      for name in os.listdir(PROJECTS_DIR)]
        candidates = [c for c in candidates if os.path.isdir(c)]
        if candidates:
            return max(candidates, key=os.path.getmtime)
    except OSError:
        pass
    return os.path.join(PROJECTS_DIR, "unknown")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--project", default=None)
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.project is None:
        args.project = default_project_dir()
    if args.workspace is None:
        args.workspace = _configured_workspace()

    if not os.path.isdir(args.project):
        print(f"No transcript directory at {args.project}", file=sys.stderr)
        return 1

    events = load_events(args.project)
    bash = [(t, i) for t, n, i in events if n == "Bash"]
    edits = [(t, n, i) for t, n, i in events
             if n in {"Edit", "Write", "MultiEdit", "NotebookEdit"}]
    recent = bash[-args.limit:]

    buckets: dict[str, Counter] = defaultdict(Counter)
    for _, inp in recent:
        for seg in subcommands(inp.get("command", "")):
            hit = classify(seg)
            if hit:
                family, reason = hit
                buckets[reason][family] += 1

    all_families: Counter = Counter()
    for _, inp in bash:
        for seg in subcommands(inp.get("command", "")):
            hit = classify(seg)
            if hit:
                all_families[hit[0]] += 1

    # Destructive shapes, reported with the line that actually matched -- a
    # multi-line command's first line is almost never the interesting part.
    danger: dict[str, list[str]] = defaultdict(list)
    for _, inp in bash:
        cmd = inp.get("command", "")
        for label, pattern in DANGER_SHAPES:
            for line in cmd.splitlines():
                if re.search(pattern, line):
                    danger[label].append(line.strip()[:160])
                    break

    write_roots: Counter = Counter()
    home = os.path.expanduser("~")
    for _, _, inp in edits:
        path = (inp.get("file_path") or inp.get("notebook_path") or "")
        if path:
            write_roots["/".join(path.replace(home, "~").split("/")[:4])] += 1

    # ---------------------------------------------------------------------
    # The non-Bash surface. Everything above this line looks only at Bash,
    # which is how the MCP allowlist grew to dozens of hand-written rules without
    # anything reporting it.
    # ---------------------------------------------------------------------
    perm_rules = rules_lib.load_rules(workspace=args.workspace)
    non_bash = [(t, n, i) for t, n, i in events if n != "Bash"]
    # ONE window for every non-Bash figure below. It was `limit * 5` here and
    # all-history for the MCP section, under a header announcing `limit`, so
    # the two sections disagreed with each other about the same server.
    recent_tools = non_bash[-max(args.limit, 1):]

    tool_verdicts: dict[str, Counter] = defaultdict(Counter)
    for _, name, _ in recent_tools:
        if name in rules_lib.ALWAYS_FREE_TOOLS:
            tool_verdicts["already-free"][name] += 1
        else:
            tool_verdicts[rules_lib.verdict(name, perm_rules)][name] += 1

    mcp_calls = [n for _, n, _ in recent_tools if n.startswith("mcp__")]
    servers = rules_lib.propose_server_rules(mcp_calls, perm_rules)
    mcp_rule_count = sum(
        1 for b in perm_rules.values() for r in b if r.raw.startswith("mcp__"))

    if args.json:
        json.dump({
            "bash_calls": len(bash),
            "analysed": len(recent),
            "mcp_rule_count": mcp_rule_count,
            "tool_verdicts": {k: v.most_common()
                              for k, v in tool_verdicts.items()},
            "mcp_servers": {
                s: {"calls": d["calls"],
                    "tools": sorted(d["tools"]),
                    "prompting": sorted(d["prompting"]),
                    "destructive_seen": d["destructive_seen"],
                    "server_rule_present": d["server_rule_present"],
                    "proposed_rule": d["proposed_rule"]}
                for s, d in servers.items()},
            "grantable": buckets["grantable"].most_common(),
            "already_free": buckets["already-free"].most_common(),
            "exec_wrappers": buckets["exec-wrapper"].most_common(),
            "no_prefix_rule": buckets["no-prefix"].most_common(),
            "path_invoked": buckets["path-invoked"].most_common(),
            "distinct_families_recent": len(set().union(*[set(b) for b in buckets.values()]) if buckets else set()),
            "distinct_families_all": len(all_families),
            "danger": {k: v[:5] for k, v in danger.items()},
            "write_roots": write_roots.most_common(20),
        }, sys.stdout, indent=2)
        return 0

    free_total = sum(buckets["already-free"].values())
    grant_total = sum(buckets["grantable"].values())
    exec_total = sum(buckets["exec-wrapper"].values())
    path_total = sum(buckets["path-invoked"].values())
    no_prefix_total = sum(buckets["no-prefix"].values())
    observed = free_total + grant_total + exec_total + path_total + no_prefix_total

    print(f"Transcripts: {len(events)} tool calls, {len(bash)} Bash, {len(edits)} file writes")
    print(f"Analysed the most recent {len(recent)} Bash invocations "
          f"({observed} subcommands after splitting compounds)\n")

    print("ALREADY FREE — built-in read-only set (git and find judged by their arguments; unsure counts as not free), needs no rule")
    print(f"  {free_total} subcommands ({free_total * 100 // max(observed, 1)}%) "
          f"across {len(buckets['already-free'])} families")
    for fam, n in buckets["already-free"].most_common(12):
        print(f"    {n:5d}  {fam}")

    print("\nWORTH GRANTING — these are the calls that actually prompt you today")
    for fam, n in buckets["grantable"].most_common(25):
        print(f"    {n:5d}  {fam}")

    if buckets["exec-wrapper"]:
        print("\nEXEC WRAPPERS — a prefix rule here allows arbitrary code")
        print("  Claude Code does not strip these, so Bash(npx *) permits npx <anything>.")
        for fam, n in buckets["exec-wrapper"].most_common(10):
            print(f"    {n:5d}  {fam}")

    if buckets["no-prefix"]:
        print("\nPROMPTS REGARDLESS — no prefix rule covers these")
        print("  find with -exec/-delete/-fprint* (or an unquoted glob): only an exact-match rule covers it.")
        print("  A write through an output redirect ('<family> >'): only a blanket Bash grant covers it.")
        for fam, n in buckets["no-prefix"].most_common(10):
            print(f"    {n:5d}  {fam}")

    if buckets["path-invoked"]:
        print("\nPATH-INVOKED — commands spelled as a path, NOT a command family")
        print("  No Bash(<name> *) rule can match these, and a rule written against")
        print("  today's path breaks when a plugin version bumps or the variable")
        print("  resolves elsewhere. If this bucket is non-trivial, an enumerated")
        print("  allowlist cannot succeed -- prefer Bash(*) plus a PreToolUse hook.")
        print(f"  {path_total} subcommands "
              f"({path_total * 100 // max(observed, 1)}% of all observed)")
        for fam, n in buckets["path-invoked"].most_common(10):
            print(f"    {n:5d}  {fam[:88]}")

    print(f"\nLONG TAIL — {len(all_families)} distinct families across all "
          f"{len(bash)} Bash calls")
    print("  This is the number that decides allowlist vs. broad grant. An enumerated")
    print("  allowlist must cover all of them and will still miss tomorrow's.")

    print("\nDESTRUCTIVE SHAPES IN YOUR ACTUAL HISTORY")
    for label, _ in DANGER_SHAPES:
        hits = danger.get(label, [])
        marker = "  " if hits else "  "
        print(f"{marker}{len(hits):5d}  {label}")
        for h in hits[:2]:
            print(f"           {h}")

    print("\nFILE-WRITE TARGETS — what a broad Edit grant would cover")
    for root, n in write_roots.most_common(12):
        print(f"    {n:5d}  {root}")

    # ---------------------------------------------------------------------
    print("\n" + "=" * 68)
    print("NON-BASH TOOLS — the surface this scanner used to discard entirely")
    print("=" * 68)
    print(f"Window: the most recent {len(recent_tools)} non-Bash tool calls "
          f"(--limit {args.limit}). Every figure below uses this same window.")
    print("Rules merged from every settings scope that exists"
          f"{' under ' + args.workspace if args.workspace else ''}.")
    print("Transcripts record REJECTIONS but not APPROVALS, so this is not a")
    print("count of prompts you saw — it is where a prompt CAN happen. Under")
    print("defaultMode: auto the classifier judges anything uncovered.\n")

    uncovered = tool_verdicts["prompt"]
    for label, bucket in (("uncovered by any rule", uncovered),
                          ("explicitly allowed", tool_verdicts["allow"]),
                          ("ask rule", tool_verdicts["ask"]),
                          ("deny rule", tool_verdicts["deny"])):
        if bucket:
            print(f"  {sum(bucket.values()):5d} calls — {label} "
                  f"({len(bucket)} distinct tools)")

    if uncovered:
        print("\n  Most-called uncovered tools:")
        for name, n in uncovered.most_common(15):
            print(f"    {n:5d}  {name}")

    if servers:
        print(f"\nMCP SERVERS — {mcp_rule_count} mcp__ rules in your settings today")
        print("  A rule naming only a server matches EVERY tool on it, verified")
        print("  against Claude Code's own matcher. So the right unit is one rule")
        print("  per server, not one per tool — see permission_rules.py.\n")
        for server, d in sorted(servers.items(),
                                key=lambda kv: -len(kv[1]["prompting"])):
            state = ("server-wide rule present" if d["server_rule_present"]
                     else f"{len(d['prompting'])} of {len(d['tools'])} tools uncovered")
            print(f"  {server}")
            print(f"      {d['calls']:5d} calls, {state}")
            if not d["server_rule_present"] and d["prompting"]:
                print(f"      proposed:  {d['proposed_rule']}")
            if d["destructive_seen"]:
                print("      a whole-server grant also covers these, which look")
                print("      outward-facing or destructive — decide each one:")
                for t in d["destructive_seen"][:8]:
                    print(f"        - {t}")
        print("\n  The destructive lists above are a KEYWORD heuristic on the tool")
        print("  name, offered to prompt your judgement. They are not a classifier:")
        print("  a dangerous tool whose name lacks the keyword will not appear, so")
        print("  read each server's full tool list before granting it whole.")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
