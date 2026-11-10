"""Reads the toolkit plugins' hook manifests (<profile dir>/hooks/hooks.json).

One plugin per profile in claude/profiles.json; a repo with no registry has
just the core plugin in plugin/.

Stdlib only and importable by path, so install.sh, the tools' tests and the
integrity check all parse registrations the same way.
"""

import json
import os
import shlex
import sys

PLUGIN_HOOKS = os.path.join("plugin", "hooks", "hooks.json")
PREFIX = '"${CLAUDE_PLUGIN_ROOT}"/'
PLUGIN_ID = "toolkit-core@claude-code-toolkit"
MARKETPLACE = "claude-code-toolkit"
LEGACY_COMMANDS = os.path.join("claude", "legacy-hook-commands.json")


class ManifestError(ValueError):
    """The plugin hook manifest, or one of its commands, is malformed."""


def _profiles():
    """The sibling profiles module, or None when it cannot be imported."""
    lib = os.path.dirname(os.path.realpath(__file__))
    if lib not in sys.path:
        sys.path.insert(0, lib)
    try:
        import profiles
    except ImportError:
        return None
    return profiles


def _plugin_dirs(repo):
    """[(profile, dir)] in registry order; core alone when there is no registry."""
    if not os.path.exists(os.path.join(repo, "claude", "profiles.json")):
        return [("core", "plugin")]
    module = _profiles()
    if module is None:
        raise ManifestError("profile registry present but lib/profiles.py unavailable")
    try:
        registry = module.load_registry(repo)
    except module.RegistryError as exc:
        raise ManifestError(str(exc))
    return [(name, entry["dir"]) for name, entry in registry.items()]


def _dir_for(repo, profile):
    for name, directory in _plugin_dirs(repo):
        if name == profile:
            return directory
    raise ManifestError("unknown profile %r" % (profile,))


def _hooks_path(repo, directory):
    return os.path.join(repo, directory, "hooks", "hooks.json")


def load(repo):
    """Every registered plugin's `hooks` object, blocks concatenated per event."""
    merged = {}
    for _name, directory in _plugin_dirs(repo):
        for event, blocks in _load_file(_hooks_path(repo, directory)).items():
            merged.setdefault(event, []).extend(blocks)
    return merged


def load_plugin(repo, profile):
    """One profile's `hooks` object."""
    return _load_file(_hooks_path(repo, _dir_for(repo, profile)))


def _load_file(path):
    """Parse and validate one hooks.json and return its `hooks` object."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ManifestError("cannot read %s: %s" % (path, exc))
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict) or not all(isinstance(v, list) for v in hooks.values()):
        raise ManifestError("%s: `hooks` must be an object of lists" % path)
    for event, blocks in hooks.items():
        for block in blocks:
            where = "%s: %s" % (path, event)
            if not isinstance(block, dict):
                raise ManifestError("%s: each block must be an object" % where)
            if "matcher" in block and not isinstance(block["matcher"], str):
                raise ManifestError("%s: `matcher` must be a string" % where)
            handlers = block.get("hooks", [])
            if not isinstance(handlers, list):
                raise ManifestError("%s: a block's `hooks` must be a list" % where)
            for handler in handlers:
                if not isinstance(handler, dict):
                    raise ManifestError("%s: each handler must be an object" % where)
                if not isinstance(handler.get("command"), str):
                    raise ManifestError("%s: a handler's `command` must be a string" % where)
                parse_command(handler["command"])
                if "timeout" in handler and (not isinstance(handler["timeout"], int)
                                             or isinstance(handler["timeout"], bool)):
                    raise ManifestError("%s: `timeout` must be an integer" % where)
    return hooks


def parse_command(command):
    """Split a plugin hook command into (relpath, args)."""
    if not isinstance(command, str) or not command.startswith(PREFIX):
        raise ManifestError("command must start with %s: %r" % (PREFIX, command))
    try:
        tokens = shlex.split(command[len(PREFIX):])
    except ValueError as exc:
        raise ManifestError("unparseable command %r: %s" % (command, exc))
    if not tokens:
        raise ManifestError("command has no executable: %r" % (command,))
    return tokens[0], tokens[1:]


def registrations(manifest, profile=None):
    """Every handler in file order, with its event, matcher and parsed command.

    With `profile`, each row also carries "profile".
    """
    rows = []
    for event, blocks in manifest.items():
        for block in blocks:
            for hook in block.get("hooks", []):
                relpath, args = parse_command(hook.get("command"))
                row = {
                    "event": event,
                    "matcher": block.get("matcher"),
                    "command": hook["command"],
                    "relpath": relpath,
                    "exe": os.path.basename(relpath),
                    "args": args,
                    "timeout": hook.get("timeout"),
                }
                if profile is not None:
                    row["profile"] = profile
                rows.append(row)
    return rows


def plugin_registrations(repo):
    """registrations() rows for every registered profile, tagged "profile"."""
    rows = []
    for name, directory in _plugin_dirs(repo):
        rows.extend(registrations(_load_file(_hooks_path(repo, directory)), profile=name))
    return rows


def resolve(repo, relpath, profile="core"):
    """Where Claude Code runs a hook from: <repo>/<profile dir>/<relpath>."""
    return os.path.join(repo, _dir_for(repo, profile), relpath)


def legacy_commands(repo, home):
    """Every command the old template ever wrote, with __HOME__ expanded."""
    with open(os.path.join(repo, LEGACY_COMMANDS), encoding="utf-8") as handle:
        data = json.load(handle)
    return {c.replace("__HOME__", home) for c in data["commands"]}


_SHELL_SPECIAL = " \t\n'\"\\$`*?[](){}<>|&;!#~"


def wanted_status_line(repo, home):
    """The statusLine install.sh wants, with __HOME__ expanded, or None.

    None when claude/settings.host.json has no dict `statusLine` with a str
    `command`. OSError/ValueError when the host file is unreadable or not JSON.
    """
    with open(os.path.join(repo, "claude", "settings.host.json"), encoding="utf-8") as handle:
        host = json.load(handle)
    wanted = host.get("statusLine") if isinstance(host, dict) else None
    if not (isinstance(wanted, dict) and isinstance(wanted.get("command"), str)):
        return None
    # The command runs through a shell, so a HOME with a space must be quoted
    # (lib/tests/test_path_surface_matrix.py). Quote ONLY when
    # HOME holds a character the shell treats specially: shlex.quote alone
    # also quotes every non-ASCII letter (an ʻokina), which would change the
    # command on machines whose unquoted one already works, and install.sh
    # would then leave it alone as "yours".
    shell_home = shlex.quote(home) if any(c in _SHELL_SPECIAL for c in home) else home
    return dict(wanted, command=wanted["command"].replace("__HOME__", shell_home))
