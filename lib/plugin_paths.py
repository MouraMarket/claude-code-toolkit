#!/usr/bin/env python3
"""Finds a toolkit plugin on disk, and the legacy links and bare-name
clashes that `install.sh --check` and the install prune need to know about.

Stdlib only, Python 3.9. No function raises: any OSError, ValueError or
unexpected shape yields the documented empty result.

CLI (exit 0; any other argv prints usage to stderr and exits 2):
    plugin_paths.py legacy <home> <repo>    one `owned|kept<TAB>path` per link
    plugin_paths.py clashes <home> <repo>   one clash message per line
"""

import json
import os
import sys

LEGACY_DIRS = ("commands", "agents", "build-lens", "skills")


def _load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _is_text(value):
    return isinstance(value, str) and bool(value)


def _directory_plugin_root(entry, plugin):
    """`plugin`'s realpath inside one `known_marketplaces.json` entry, or None
    when the entry is not a directory source or does not list the plugin."""
    try:
        if entry["source"]["source"] != "directory":
            return None
        location = entry["installLocation"]
        manifest = _load_json(os.path.join(
            location, ".claude-plugin", "marketplace.json"))
        for item in manifest["plugins"]:
            if item["name"] != plugin or not isinstance(item["source"], str):
                continue
            root = os.path.realpath(os.path.join(location, item["source"]))
            if os.path.isdir(root):
                return root
    except (KeyError, TypeError, ValueError, OSError):
        pass
    return None


def plugin_root(home, plugin):
    """The directory holding `plugin`'s files, or None.

    A directory-source marketplace (how a checkout is installed) is found
    through `known_marketplaces.json` and that marketplace's own
    `marketplace.json`; a hosted one through `marketplaces/*/plugins/<plugin>`.
    `installed_plugins.json` is never read: for a directory-source plugin its
    `installPath` is a stale cache copy, not the checkout.
    """
    if not _is_text(home) or not _is_text(plugin):
        return None
    plugins_dir = os.path.join(home, ".claude", "plugins")
    known = _load_json(os.path.join(plugins_dir, "known_marketplaces.json"))
    if isinstance(known, dict):
        for key in sorted(known):
            root = _directory_plugin_root(known[key], plugin)
            if root is not None:
                return root
    hosted = os.path.join(plugins_dir, "marketplaces")
    try:
        for marketplace in sorted(os.listdir(hosted)):
            candidate = os.path.join(hosted, marketplace, "plugins", plugin)
            if os.path.isdir(candidate):
                return candidate
    except OSError:
        pass
    return None


def directory_plugin_roots(home):
    """Every plugin root of every directory-source marketplace, sorted and
    de-duplicated (realpaths). A reader's list: the leftover scan restores a
    sidecar wherever one exists. Nothing uses it to choose a file to edit.
    A non-string home, a missing or malformed file, or a wrong shape yields
    `[]`, or skips that one entry.
    """
    roots = set()
    try:
        if not isinstance(home, str) or not home:
            return []
        known = _load_json(os.path.join(
            home, ".claude", "plugins", "known_marketplaces.json"))
        if not isinstance(known, dict):
            return []
        for entry in known.values():
            try:
                if entry["source"]["source"] != "directory":
                    continue
                location = entry["installLocation"]
                manifest = _load_json(os.path.join(
                    location, ".claude-plugin", "marketplace.json"))
                for item in manifest["plugins"]:
                    try:
                        if not isinstance(item["source"], str):
                            continue
                        root = os.path.realpath(os.path.join(location, item["source"]))
                        if os.path.isdir(root):
                            roots.add(root)
                    except (KeyError, TypeError, ValueError, OSError):
                        continue
            except (KeyError, TypeError, ValueError, OSError):
                continue
    except (TypeError, ValueError, OSError):
        return []
    return sorted(roots)


def installed_root(home, key):
    """The directory Claude Code loads the enabled plugin `key` from, or None.

    `key` is the exact `<plugin>@<marketplace>` from `enabledPlugins`. A
    directory-source marketplace resolves through its own `marketplace.json`
    only, never another marketplace's copy (its `installPath` is a stale
    cache). Hosted plugins load from their
    versioned copy under `~/.claude/plugins/cache/`, so that is the copy whose
    skills can clash: it is named by `installed_plugins.json`, and a `url` or
    `git-subdir` source never appears under `marketplaces/<m>/plugins/<p>`.
    """
    try:
        if not isinstance(home, str) or not home or not isinstance(key, str):
            return None
        plugin, _, marketplace = key.partition("@")
        plugins_dir = os.path.join(home, ".claude", "plugins")
        known = _load_json(os.path.join(plugins_dir, "known_marketplaces.json"))
        try:
            is_directory = known[marketplace]["source"]["source"] == "directory"
        except (KeyError, TypeError):
            is_directory = False
        if is_directory:
            return _directory_plugin_root(known[marketplace], plugin)
        installed = _load_json(os.path.join(plugins_dir, "installed_plugins.json"))
        entries = [e for e in installed["plugins"][key] if isinstance(e, dict)]
        for entry in [e for e in entries if e.get("scope") == "user"] + entries:
            path = entry.get("installPath")
            if isinstance(path, str) and os.path.isdir(path):
                return path
            break
    except (KeyError, TypeError, ValueError, OSError):
        pass
    return None


def owned_legacy_links(home, repo):
    """[(path, owned)] for each symlink in the four legacy `~/.claude` dirs.

    Owned means the link's target, normalized with a non-strict realpath (so a
    dangling target still normalizes and `..` cannot escape), sits inside
    `repo` by path component. A sibling prefix like `<repo>-old` is not owned.
    Real files and directories are never listed.
    """
    found = []
    if not _is_text(home) or not _is_text(repo):
        return found
    try:
        repo_real = os.path.realpath(repo)
    except (OSError, ValueError, TypeError):
        return found
    for sub in LEGACY_DIRS:
        directory = os.path.join(home, ".claude", sub)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                if not os.path.islink(path):
                    continue
                target = os.readlink(path)
                if not os.path.isabs(target):
                    target = os.path.join(directory, target)
                target_real = os.path.realpath(target)
                try:
                    owned = os.path.commonpath([repo_real, target_real]) == repo_real
                except ValueError:
                    owned = False
            except (OSError, ValueError):
                continue
            found.append((path, owned))
    return found


def _profiles_module():
    """lib/profiles.py, imported by path like its siblings, or None."""
    lib = os.path.dirname(os.path.realpath(__file__))
    if lib not in sys.path:
        sys.path.insert(0, lib)
    try:
        import profiles
    except ImportError:
        return None
    return profiles


def _shipped_skills(repo):
    """([(skill name, owning plugin)], error | None) for every registered profile.

    Read through each plugin dir's `skills` symlink, which is what the plugin
    really ships. A registry that cannot be read, or a profile whose `skills`
    cannot be listed, is an error, never an empty inventory."""
    profiles = _profiles_module()
    if profiles is None:
        return [], "lib/profiles.py unavailable"
    try:
        registry = profiles.load_registry(repo)
    except profiles.RegistryError as exc:
        return [], str(exc)
    shipped = []
    unlisted = []
    for entry in registry.values():
        skills = os.path.join(repo, entry["dir"], "skills")
        try:
            names = sorted(d for d in os.listdir(skills)
                           if os.path.isfile(os.path.join(skills, d, "SKILL.md")))
        except OSError:
            unlisted.append("%s/skills" % entry["dir"])
            continue
        shipped.extend((name, entry["plugin"]) for name in names)
    if unlisted:
        return shipped, "cannot list %s" % ", ".join(unlisted)
    return shipped, None


def skill_clashes(home, repo):
    """Messages for every bare name that a toolkit plugin ships as a skill and
    something else also defines. Fails strict: an inventory that cannot be
    completed adds a line saying so rather than reporting a clean pass."""
    messages = []
    if not _is_text(home) or not _is_text(repo):
        return messages
    shipped, error = _shipped_skills(repo)
    # Two toolkit plugins shipping one name is a clash on its own, enabled or
    # not: the enabled-plugin loop below skips the toolkit's own marketplace,
    # so this is the only place a sibling-vs-sibling clash can be seen.
    owners = {}
    for name, plugin in shipped:
        owners.setdefault(name, [])
        if plugin not in owners[name]:
            owners[name].append(plugin)
    for name in sorted(owners):
        if len(owners[name]) > 1:
            messages.append(
                "%s share a bare name: /%s runs whichever plugin loaded first. "
                "Ship it from one profile only."
                % (" and ".join("%s:%s" % (p, name) for p in owners[name]), name))
    if error is not None:
        messages.append("plugin inventory unreadable (%s): the plugin clash check "
                        "is incomplete" % error)
    for name, owner in shipped:
        if os.path.lexists(os.path.join(home, ".claude", "skills", name)):
            messages.append(
                "~/.claude/skills/%s shadows %s:%s: a bare /%s runs the "
                "personal copy. Remove it (./install.sh removes it if it links "
                "into this checkout)." % (name, owner, name, name))
        if os.path.lexists(os.path.join(home, ".claude", "commands", name + ".md")):
            messages.append(
                "~/.claude/commands/%s.md shadows %s:%s: a bare /%s may "
                "run the personal command. Remove it (./install.sh removes it if "
                "it links into this checkout)." % (name, owner, name, name))

    settings_path = os.path.join(home, ".claude", "settings.json")
    if os.path.lexists(settings_path):
        settings = _load_json(settings_path)
        enabled = settings.get("enabledPlugins") if isinstance(settings, dict) else None
        if not isinstance(settings, dict) or (
                "enabledPlugins" in settings and not isinstance(enabled, dict)):
            messages.append("cannot read enabledPlugins from ~/.claude/settings.json: "
                            "the plugin clash check is incomplete")
        elif enabled:
            # Every plugin in our own marketplace is a toolkit profile: its
            # skills are the `shipped` ones, never a clash with themselves.
            profiles = _profiles_module()
            marketplace = profiles.MARKETPLACE if profiles else "claude-code-toolkit"
            keys = sorted(str(k) for k, v in enabled.items() if v is True
                          and str(k).partition("@")[2] != marketplace)
            reported = set()
            for key in keys:
                other = key.split("@", 1)[0]
                root = installed_root(home, key)
                if root is None:
                    if other not in reported:
                        reported.add(other)
                        messages.append("enabled plugin %s could not be located: the "
                                        "plugin clash check is incomplete" % other)
                    continue
                for name, owner in shipped:
                    if os.path.isfile(os.path.join(root, "skills", name, "SKILL.md")):
                        messages.append(
                            "%s:%s and %s:%s share a bare name: /%s runs "
                            "whichever plugin loaded first. Retire one copy."
                            % (other, name, owner, name, name))
                    if os.path.isfile(os.path.join(root, "commands", name + ".md")):
                        messages.append(
                            "%s:%s (a command) and %s:%s share a bare name: "
                            "/%s runs whichever plugin loaded first. Retire one copy."
                            % (other, name, owner, name, name))
    return sorted(messages)


def main(argv):
    if len(argv) == 4 and argv[1] in ("legacy", "clashes"):
        home, repo = argv[2], argv[3]
        if argv[1] == "legacy":
            for path, owned in owned_legacy_links(home, repo):
                print("%s\t%s" % ("owned" if owned else "kept", path))
        else:
            for message in skill_clashes(home, repo):
                print(message)
        return 0
    sys.stderr.write("usage: plugin_paths.py legacy|clashes <home> <repo>\n")
    return 2


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv))
