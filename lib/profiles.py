"""The profile registry (claude/profiles.json) and which profiles are selected.

Stdlib only and importable by path, like hook_manifest. One profile is one
plugin in the `claude-code-toolkit` marketplace; the `profiles` key in the toolkit
config picks which ones a machine installs.

The registry also names the Claude Code version the release was proven on
(`tested_claude_code`, see tested_version) and, per profile, the Claude Code
features it needs: the hook events in its hooks.json plus any optional
`features` strings (see required_features).

CLI (used by install.sh):
  profiles.py selected <repo>     selected profile names, one per line
  profiles.py components <repo>   ok/missing line per required component dir
"""

import json
import os
import re
import sys
from collections import OrderedDict

MARKETPLACE = "claude-code-toolkit"
REGISTRY = os.path.join("claude", "profiles.json")

_NAME = re.compile(r"[a-z]+")
# A plugin directory is `plugin` or `plugin-<name>`: the shape the integrity
# hook's LIVE_MANIFEST_RE watches for hook manifests, so a profile in any other
# directory would have hooks that go unnoticed when they change.
_DIR = re.compile(r"plugin(-[a-z]+)?")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_DEFAULT_NOTE = '# default: set "profiles" in your toolkit config to choose'
_USAGE = "usage: profiles.py selected <repo> | profiles.py components <repo>"


class RegistryError(ValueError):
    """claude/profiles.json is missing, unreadable or breaks a registry rule."""


def _strip_comments(mapping):
    return OrderedDict((k, v) for k, v in mapping.items()
                       if not (isinstance(k, str) and k.startswith("//")))


def _prerequisites(name, raw):
    """Validate a profile's optional `prerequisites`: [{id, why}], ids <plugin>@<marketplace>."""
    if not isinstance(raw, list):
        raise RegistryError("profile %s: `prerequisites` must be a list" % name)
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            raise RegistryError("profile %s: each prerequisite must be an object" % name)
        item = _strip_comments(item)
        pid, why = item.get("id"), item.get("why")
        if (not isinstance(pid, str) or pid.count("@") != 1
                or pid.endswith("@" + MARKETPLACE)):
            raise RegistryError("profile %s: prerequisite `id` must be <plugin>@<marketplace> "
                                "naming a marketplace other than %s, not %r"
                                % (name, MARKETPLACE, pid))
        if not pid.split("@")[0] or not pid.split("@")[1]:
            raise RegistryError("profile %s: prerequisite `id` must be <plugin>@<marketplace>, "
                                "not %r" % (name, pid))
        if not isinstance(why, str) or not why:
            raise RegistryError("profile %s: prerequisite `why` must be a non-empty string"
                                % name)
        rows.append({"id": pid, "why": why})
    return rows


def load_registry(repo):
    """Parse and validate <repo>/claude/profiles.json, in file order."""
    path = os.path.join(repo, REGISTRY)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle, object_pairs_hook=OrderedDict)
    except (OSError, ValueError) as exc:
        raise RegistryError("cannot read %s: %s" % (path, exc))
    return parse_registry(data, path)


def parse_registry(data, source):
    """Validate a parsed profiles.json (`source` names it in messages), in file order."""
    raw = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        raise RegistryError("%s: `profiles` must be an object" % source)

    registry = OrderedDict()
    for name, fields in _strip_comments(raw).items():
        if not _NAME.fullmatch(name):
            raise RegistryError("profile name %r must match ^[a-z]+$" % name)
        if not isinstance(fields, dict):
            raise RegistryError("profile %s must be an object" % name)
        fields = _strip_comments(fields)
        for key in ("plugin", "dir", "summary"):
            if not isinstance(fields.get(key), str) or not fields[key]:
                raise RegistryError("profile %s: `%s` must be a non-empty string" % (name, key))
        if "/" in fields["dir"] or "\\" in fields["dir"]:
            raise RegistryError("profile %s: `dir` must have no path separator" % name)
        if not _DIR.fullmatch(fields["dir"]):
            raise RegistryError("profile %s: `dir` must be `plugin` or `plugin-<name>` "
                                "(lowercase letters), not %r" % (name, fields["dir"]))
        for key in ("default", "agents"):
            if not isinstance(fields.get(key), bool):
                raise RegistryError("profile %s: `%s` must be true or false" % (name, key))
        requires = fields.get("requires")
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise RegistryError("profile %s: `requires` must be a list of profile names" % name)
        features = fields.get("features", [])
        if (not isinstance(features, list)
                or not all(isinstance(f, str) and f for f in features)):
            raise RegistryError("profile %s: `features` must be a list of non-empty strings"
                                % name)
        entry = dict(fields)
        entry["features"] = list(features)
        entry["prerequisites"] = _prerequisites(name, fields.get("prerequisites", []))
        entry["name"] = name
        registry[name] = entry

    if "core" not in registry:
        raise RegistryError("the registry must define `core`")
    if registry["core"]["default"] is not True:
        raise RegistryError("`core` must have default: true")
    if registry["core"]["dir"] != "plugin":
        raise RegistryError("`core` must have dir: plugin")
    for key in ("plugin", "dir"):
        seen = {}
        for name, entry in registry.items():
            if entry[key] in seen:
                raise RegistryError("profiles %s and %s share %s %r"
                                    % (seen[entry[key]], name, key, entry[key]))
            seen[entry[key]] = name
    for name, entry in registry.items():
        for required in entry["requires"]:
            if required not in registry:
                raise RegistryError("profile %s requires unknown profile %s" % (name, required))
    return registry


def tested_version(data):
    """The validated top-level `tested_claude_code` of a parsed profiles.json,
    or None when absent. Kept out of parse_registry so a registry without it
    still parses."""
    version = data.get("tested_claude_code") if isinstance(data, dict) else None
    if version is None:
        return None
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise RegistryError("`tested_claude_code` must look like 2.1.306, not %r" % (version,))
    return version


def load_tested_version(repo):
    """tested_version() of <repo>/claude/profiles.json."""
    path = os.path.join(repo, REGISTRY)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RegistryError("cannot read %s: %s" % (path, exc))
    return tested_version(data)


def required_features(repo, entry):
    """What a profile needs from Claude Code: the hook event names in its
    hooks/hooks.json, in file order, then the entry's `features`. A missing
    hooks.json contributes no events."""
    path = os.path.join(repo, entry["dir"], "hooks", "hooks.json")
    events = []
    try:
        with open(path, encoding="utf-8") as handle:
            hooks = json.load(handle, object_pairs_hook=OrderedDict).get("hooks")
        if isinstance(hooks, dict):
            events = list(hooks)
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        raise RegistryError("cannot read %s: %s" % (path, exc))
    return events + list(entry.get("features", []))


def plugin_id(entry):
    """The id Claude Code knows a profile's plugin by: <plugin>@claude-code-toolkit."""
    return "%s@%s" % (entry["plugin"], MARKETPLACE)


def prerequisites(registry, names):
    """[(id, why, [profile names needing it])] for the selected names, one row
    per id in first-seen order; `why` is the first profile's."""
    rows = OrderedDict()
    for name in names:
        for item in registry[name]["prerequisites"]:
            if item["id"] not in rows:
                rows[item["id"]] = (item["why"], [])
            rows[item["id"]][1].append(name)
    return [(pid, why, needing) for pid, (why, needing) in rows.items()]


def resolve_selection(registry, config, config_error):
    """(names, error, defaulted) for a parsed config. A config error wins; an absent key selects
    the registry's defaults; otherwise every listed name must be a known
    profile. `core` and every `requires` are always added."""
    if config_error is not None:
        return None, "config file %s" % config_error, False
    if "profiles" not in config:
        listed = [n for n, e in registry.items() if e["default"]]
        defaulted = True
    else:
        listed = config["profiles"]
        if not isinstance(listed, list) or not all(isinstance(n, str) for n in listed):
            return None, "profiles must be a list of profile names", False
        unknown = [n for n in listed if n not in registry]
        if unknown:
            return (None, "unknown profile(s): %s; valid: %s"
                    % (", ".join(unknown), ", ".join(registry)), False)
        defaulted = False
    # Both paths close over `requires`: a default profile needing a
    # non-default helper must not install without it.
    wanted = set()
    pending = ["core"] + listed
    while pending:
        name = pending.pop()
        if name not in wanted:
            wanted.add(name)
            pending.extend(registry[name]["requires"])
    return [n for n in registry if n in wanted], None, defaulted


def missing_defaults(registry, names, defaulted):
    """Default profiles, in registry order, that an explicit list leaves out.

    `core` is always implied, so it is never reported. A defaulted selection
    leaves nothing out.
    """
    if defaulted:
        return []
    return [n for n, e in registry.items()
            if e["default"] and n != "core" and n not in names]


def selected(repo):
    """(names, error, defaulted) from the registry and the toolkit config.

    Never raises: a broken registry, config or missing sibling is an error
    string, never a silent fall back to defaults.
    """
    try:
        registry = load_registry(repo)
    except RegistryError as exc:
        return None, str(exc), False
    lib = os.path.dirname(os.path.realpath(__file__))
    if lib not in sys.path:
        sys.path.insert(0, lib)
    try:
        import toolkit_config
    except ImportError:
        return None, "lib/toolkit_config.py unavailable", False
    config, config_error = toolkit_config.load_status()
    return resolve_selection(registry, config, config_error)


def enabled_profiles(registry, settings):
    """(set of enabled profile names, error) from a parsed settings.json."""
    if settings is None:
        return None, "cannot read ~/.claude/settings.json"
    enabled = settings.get("enabledPlugins", {})
    if not isinstance(enabled, dict):
        return None, "enabledPlugins is not an object"
    return {n for n, e in registry.items() if enabled.get(plugin_id(e)) is True}, None


def main(argv):
    if len(argv) != 2 or argv[0] not in ("selected", "components"):
        sys.stderr.write(_USAGE + "\n")
        return 2
    command, repo = argv
    names, error, defaulted = selected(repo)
    if command == "selected":
        if error:
            sys.stderr.write(error + "\n")
            return 1
        print("\n".join(names))
        if defaulted:
            print(_DEFAULT_NOTE)
        return 0
    if error:
        sys.stderr.write(error + "\n")
        return 2
    registry = load_registry(repo)  # selected() succeeded, so this does too
    lines = []
    for name in names:
        lines.append(os.path.join(registry[name]["dir"], "skills"))
    for name in names:
        if registry[name]["agents"]:
            lines.append(os.path.join(registry[name]["dir"], "agents"))
    missing = False
    for rel in lines:
        ok = os.path.isdir(os.path.join(repo, rel))
        missing = missing or not ok
        print("%s\t%s" % ("ok" if ok else "missing", rel))
    return 1 if missing else 0


if __name__ == "__main__":
    # Same as plugin_paths.py: pin UTF-8 so a non-ASCII profile summary or path
    # never fails on a console set to a legacy code page.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
