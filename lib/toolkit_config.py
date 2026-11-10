#!/usr/bin/env python3
"""Machine-local configuration for claude-code-toolkit.

Everything machine-specific or person-specific lives in ONE json file outside
this repo, so the repo itself carries no paths, names, or data that only
apply to one person:

    ~/.config/claude-code-toolkit/config.json     (yours; never committed)
    config.example.json                  (in the repo; copy it to start)

Outside the repo on purpose — it cannot be committed by accident, and it
survives a re-clone or a fresh machine.

Every getter degrades to a documented default when the file, the key, or the
whole config directory is missing. That matters because these values are read
from inside hooks: a config typo must never take a hook down, and a missing
config must never make a *guard* more permissive than its default.
"""

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

CONFIG_PATH = os.path.expanduser(
    os.environ.get("CLAUDE_TOOLKIT_CONFIG", "~/.config/claude-code-toolkit/config.json"))

_cache: Optional[Dict[str, Any]] = None


_VAR_RE = re.compile(r"\$(\w+|\{[^}]*\})", re.ASCII)
_SEPS = "/\\" if os.name == "nt" else "/"


def _native_expand(path: str) -> str:
    """The native stdlib expansion, kept for Windows default calls only."""
    return os.path.expanduser(os.path.expandvars(path))


def _collapse_separators(path: str, windows: bool) -> str:
    """Fold every run of separators to one, so a variable that ends in a
    separator ("$R/x" with R="/" or R="C:\\work\\") cannot leave a doubled
    one behind. Guards compare config paths as text, and a doubled separator
    silently stops a protected path matching its target. On Windows a run
    mixing / and \\ folds to its first character, and a leading double
    separator (a UNC prefix) is kept."""
    if not windows:
        return re.sub(r"/{2,}", "/", path)
    lead = path[:2] if path[:2] in ("//", "\\\\") else ""
    return lead + re.sub(r"[/\\]{2,}", lambda m: m.group(0)[0],
                         path[len(lead):])


def expand_path(path: Optional[str], home: Optional[str] = None,
                environ: Optional[Dict[str, str]] = None,
                strict: bool = False) -> Optional[str]:
    """The ONE implementation of config-path expansion: $NAME and
    ${NAME}, then a leading ~ -- the order expanduser(expandvars(p)) uses.

    Default call (home=None, environ=None) is byte-identical to
    os.path.expanduser(os.path.expandvars(path)) on POSIX: variables come from
    os.environ, and a leading ~ goes to os.path.expanduser. On Windows the
    default call IS that native expansion (%VAR% included); strict guard calls
    there resolve only $NAME / ${NAME}.
    environ: a mapping to read variables from instead of os.environ.
    home: an explicit home for ~, ~/..., $HOME and ${HOME}; trailing separators
      are stripped first (root stays root), so all four spellings agree.
    strict: return None if any variable is unset or a ~ form does not resolve.
    Guards pass home, an explicit environ and strict=True, so a guard never
    reads os.environ through this function. None and "" return None."""
    if not path:
        return None
    if (os.name == "nt" and home is None and environ is None
            and not strict):
        # Windows legacy callers (expand(), get_path()) keep the native
        # semantics they always had, %VAR% included;
        # parity with the code below is proven on POSIX only.
        return _native_expand(path)
    env = os.environ if environ is None else environ
    if home is not None:
        home = home.rstrip(_SEPS)  # a root home becomes ""
    unresolved = False
    result = path

    if "$" in result:
        def substitute(match):
            nonlocal unresolved
            name = match.group(1)
            if name.startswith("{") and name.endswith("}"):
                name = name[1:-1]
            if name == "HOME" and home is not None:
                return home or "/"
            if name in env:
                return env[name]
            unresolved = True
            return match.group(0)
        result = _VAR_RE.sub(substitute, result)

    if result.startswith("~"):
        if home is None:
            expanded = os.path.expanduser(result)
        else:
            rest = result[1:]
            first_sep = next((k for k, ch in enumerate(rest) if ch in _SEPS),
                             len(rest))
            if first_sep == 0:  # bare ~ or ~/...
                expanded = (home + rest) or "/"
            else:  # ~user...
                expanded = os.path.expanduser(result)
        if expanded == result:
            unresolved = True
        result = expanded

    # Root-home collapse: "$HOME/x" with home "" would otherwise be "//x".
    # Deliberately no general `result or "/"`: an empty variable stays "".
    if home == "" and result.startswith("//"):
        result = result[1:]

    # Strict (guard) paths are compared as text: "$R/tmp/x" with R="/" has to
    # name /tmp/x, or a protected path silently stops matching its target.
    if strict:
        result = _collapse_separators(result, os.name == "nt")

    # An empty result is absent in strict mode (an empty trusted root could
    # prefix-match every path); non-strict keeps "" for stdlib parity.
    if strict and (unresolved or result == ""):
        return None
    return result


def expand(path: Optional[str]) -> Optional[str]:
    """Expand ~ and $VARS in a configured path. None and "" pass through.
    Delegates to expand_path, the one implementation."""
    return expand_path(path)


def load(refresh: bool = False) -> Dict[str, Any]:
    """The parsed config, or {} if it is missing or unreadable.

    Cached: hooks are short-lived processes, and re-reading per getter would
    multiply file I/O on a path that runs on every prompt.
    """
    global _cache
    if _cache is not None and not refresh:
        return _cache
    try:
        # Explicit UTF-8: the ambient locale (cp1252 on Windows, ASCII under
        # LC_ALL=C) would misdecode or raise on any non-ASCII value and the
        # except below would then read a VALID config as absent.
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        _cache = data if isinstance(data, dict) else {}
    except Exception:
        # Missing file, bad JSON, unreadable — all the same to a caller that
        # must keep working. Callers supply their own defaults.
        _cache = {}
    return _cache


def load_status() -> Tuple[Dict[str, Any], Optional[str]]:
    """(config, error): `load()` with the failure kept instead of folded away.

    `load()` reports a broken config as {} -- right for a hook, which must keep
    working, and wrong for a caller choosing which guards to require, which
    must tell "no config" from "config I could not read". A missing file is
    ({}, None): a fresh clone has none. Bad JSON, an unreadable file or a
    non-object root is ({}, reason). Uncached, and never touches `_cache`.
    """
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}, None
    except ValueError:
        return {}, "%s is not valid JSON" % CONFIG_PATH
    except OSError as exc:
        return {}, "%s could not be read (%s)" % (CONFIG_PATH, type(exc).__name__)
    if not isinstance(data, dict):
        return {}, "%s is not a JSON object" % CONFIG_PATH
    return data, None


def get(*keys: str, default: Any = None) -> Any:
    """Walk nested keys: get("session_titler", "index_path")."""
    node: Any = load()
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node if node is not None else default


def get_path(*keys: str, default: Optional[str] = None) -> Optional[str]:
    """Like get(), but ~-expanded. Returns `default` (unexpanded) if unset."""
    value = get(*keys)
    return expand(value) if isinstance(value, str) and value else default


def get_list(*keys: str) -> List[str]:
    """A list-valued key, ignoring non-string members. [] when absent."""
    value = get(*keys, default=[])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def get_int(*keys: str, default: int = 0) -> int:
    """An int-valued key. A non-numeric value yields `default`, never raises.

    JSON `Infinity` loads as a float that int() rejects with OverflowError, and
    hooks read these keys at import, so it falls back like any other bad value.
    """
    value = get(*keys)
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


# --- Well-known values, each with the default it falls back to ---------------

def workspace_root() -> Optional[str]:
    """The tree this person's work lives in. None when unconfigured.

    Deliberately has no default. A guessed workspace root would either be
    wrong or, worse, silently widen what the danger guard treats as safe.
    """
    return get_path("workspace_root")


def owner_name() -> str:
    """Who to name when a guard tells Claude to hand a command back.

    Defaults to a role, not a name, so an unconfigured install reads correctly
    for whoever is running it.
    """
    value = get("owner_name")
    return value if isinstance(value, str) and value.strip() else "the human"


def handover_dir() -> str:
    """Where handover documents are written and swept.

    Defaults to a dedicated directory rather than a shared specs/ folder: the
    sweep moves files out of it, and its own directory means the blast radius
    is bounded by construction instead of by a filename regex.
    """
    return get_path("thread_handover", "handover_dir",
                    default=os.path.expanduser("~/.claude/handovers"))


def specs_dir() -> Optional[str]:
    """Where plans and ledgers are written: `spec_archive.specs_dir`, expanded.

    None unless the value is a string that is absolute after expansion. A
    relative value, or one with an unexpanded variable, would resolve against
    the current directory -- the defect this getter exists to remove.

    Deliberately has no default: a tool that writes specs must not guess where
    they go. Ignores `spec_archive.enabled`, which gates the archiver, not
    where specs live.
    """
    raw = get("spec_archive", "specs_dir")
    # strict: an unset variable yields None even inside an absolute-looking
    # value ("/$UNSET/specs"), which non-strict expansion would keep verbatim.
    value = expand_path(raw, strict=True) if isinstance(raw, str) else None
    return value if value and os.path.isabs(value) else None


def repos_root() -> Optional[str]:
    """Where this person's git checkouts live: `wrap_up.repos_root`, expanded.

    The one reader for both tools that need it: claude-wrap-up-survey surveys
    the checkouts under it, and claude-backup leaves it out of the workspace
    copy (the checkouts' remotes hold them).

    Unset or empty: `<workspace_root>/repos`, or None with no workspace root.
    Set: the value when it is a string that is absolute after strict
    expansion, else None. A set but unusable value never falls back to the
    default, because the survey would then report on a tree nobody named.
    """
    raw = get("wrap_up", "repos_root")
    if raw is None or raw == "":
        workspace = workspace_root()
        return os.path.join(workspace, "repos") if workspace else None
    value = expand_path(raw, strict=True) if isinstance(raw, str) else None
    return value if value and os.path.isabs(value) else None


REVIEW_MODES = ("economical", "full-power")

# Spellings that resolve to a canonical mode. "full" is the earlier
# name of "full-power"; "full power" is how the mode is said out loud and so
# how it gets hand-typed. Without these a config written before the rename
# drops to the default silently, and its owner is never told which reviewer
# now runs.
REVIEW_MODE_ALIASES = {"full": "full-power", "full power": "full-power"}


def build_review_mode() -> str:
    """Which reviewer /build's four gates dispatch: "economical" or "full-power".

    "economical" is one blinded Claude reviewer per gate; "full-power" is
    Codex. Claude builds in both -- the mode picks only the gate reviewer.

    Anything unrecognised -- the file, the `build` object or the `review` key
    absent, a typo, the wrong case, a non-string -- resolves to "economical".
    That default is safe on TWO dimensions: it needs no Codex install
    (availability), and it is one reviewer per gate rather than a fan-out
    (cost). Never raises.
    """
    value = get("build", "review")
    if isinstance(value, str):
        value = REVIEW_MODE_ALIASES.get(value, value)
    return value if value in REVIEW_MODES else "economical"


def main(argv: List[str]) -> int:
    """CLI: `specs-dir` prints the configured specs directory. Never raises."""
    if argv != ["specs-dir"]:
        print("usage: toolkit_config.py specs-dir", file=sys.stderr)
        return 2
    path = specs_dir()
    if path is not None:
        print(path)
        return 0
    raw = get("spec_archive", "specs_dir")
    if isinstance(raw, str) and raw:
        print("spec_archive.specs_dir in %s is not an absolute path once expanded (%s); "
              "ask the user where specs should go" % (CONFIG_PATH, raw),
              file=sys.stderr)
    else:
        print("spec_archive.specs_dir is not set in %s; "
              "ask the user where specs should go" % CONFIG_PATH,
              file=sys.stderr)
    return 3


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
