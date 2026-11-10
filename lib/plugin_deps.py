#!/usr/bin/env python3
"""Which plugins name which others in their manifest `dependencies`.

Shared by `uninstall.py` (whole-toolkit removal) and install.sh's step that
uninstalls a profile's plugin once the profile is no longer selected: both must
not remove a plugin something that stays installed still needs. Stdlib plus
`plugin_paths`, so a fixture repo needs only these two modules.
"""

import json
import os
import sys

LIB = os.path.dirname(os.path.realpath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import plugin_paths  # noqa: E402


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def dependency_names(manifest):
    """The plugin names a parsed plugin.json lists under `dependencies`."""
    deps = manifest.get("dependencies", [])
    if not isinstance(deps, list):
        raise ValueError("dependencies is not a list")
    names = []
    for dep in deps:
        if isinstance(dep, str):
            names.append(dep)
        elif isinstance(dep, dict) and isinstance(dep.get("name"), str):
            names.append(dep["name"])
    return names


def toolkit_dependencies(repo):
    """{plugin name: [dependency names]} for every plugin this checkout's
    marketplace.json lists. Raises OSError, ValueError, KeyError or TypeError."""
    market = _read_json(os.path.join(repo, ".claude-plugin", "marketplace.json"))
    deps = {}
    for entry in market["plugins"]:
        manifest = _read_json(os.path.join(repo, entry["source"], ".claude-plugin", "plugin.json"))
        deps[entry["name"]] = dependency_names(manifest)
    return deps


def installed_dependencies(home, plugins, marketplaces, marketplace):
    """({plugin key: [dependency names]}, [error]) for every installed plugin
    outside `marketplace` (the toolkit's own, read by `toolkit_dependencies`).

    `plugins` and `marketplaces` are the parsed `claude plugin list --json` and
    `claude plugin marketplace list --json`. A plugin whose manifest cannot be
    found or read is an error, never "no dependencies": unproven is not safe."""
    directory_markets = {m.get("name") for m in marketplaces or []
                         if isinstance(m, dict) and m.get("source") == "directory"}
    found, errors = {}, []
    for entry in plugins or []:
        key = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(key, str) or key.endswith("@" + marketplace) or key in found:
            continue
        # The live directory, never `installPath`: for a directory marketplace
        # that is a stale cache. A plugin outside a directory marketplace (a
        # claude.ai-synced one) has no live directory, so `installPath` is the
        # only place its manifest is.
        root = plugin_paths.installed_root(home, key)
        if root is None and key.partition("@")[2] not in directory_markets:
            root = entry.get("installPath")
        if not isinstance(root, str) or not root:
            errors.append("cannot locate %s on disk, so cannot prove "
                          "it does not depend on the toolkit" % key)
            continue
        try:
            found[key] = dependency_names(_read_json(os.path.join(root, ".claude-plugin", "plugin.json")))
        except (OSError, ValueError, AttributeError) as exc:
            errors.append("cannot read %s's plugin.json (%s), so cannot "
                          "prove it does not depend on the toolkit" % (key, exc))
    return found, errors


def names_plugin(dep, key):
    """True when a `dependencies` entry `dep` names the plugin `key`
    (`<plugin>@<marketplace>`): by bare name or by full key."""
    return dep == key or dep == key.partition("@")[0]


def plan_removal(dropped, stays, deps_of):
    """(order, kept) for removing the plugins in `dropped` ({plugin id: profile name}).

    `stays` is the plugin ids that stay installed; `deps_of(id)` lists a plugin's
    dependency names. `kept` is {id: "<dependent> depends on it"}: every dropped
    plugin something staying names, to a fixed point (a plugin kept because
    another needs it can itself need a third). `order` is the rest, dependents
    first: a plugin comes out only once nothing left depends on it."""
    stays = sorted(stays)
    kept = {}
    changed = True
    while changed:
        changed = False
        for pid in sorted(p for p in dropped if p not in kept):
            needed_by = next((s for s in stays
                              if any(names_plugin(d, pid) for d in deps_of(s))),
                             None)
            if needed_by is not None:
                kept[pid] = "%s depends on it" % needed_by
                stays.append(pid)
                changed = True
    remaining = {pid for pid in dropped if pid not in kept}
    order = []
    while remaining:
        ready = sorted(p for p in remaining
                       if not any(names_plugin(d, p)
                                  for other in remaining if other != p for d in deps_of(other)))
        pid = (ready or sorted(remaining))[0]
        remaining.discard(pid)
        order.append(pid)
    return order, kept
