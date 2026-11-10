#!/usr/bin/env python3
"""Check the repository's plugin layout: the marketplace, manifests, VERSION,
the absence of a plugin version, dependencies, symlinks and hook targets.

    python3 scripts/check_layout.py [--root DIR]

Exit 0 when the layout is sound, 1 with one line per problem otherwise. A
plugin whose `skills` directory holds no skill is a problem: it cannot ship
empty.

No plugin the marketplace lists sets `version`, in its plugin.json or its
entry. An unlisted manifest is not checked: nothing can install it, and listing
it brings it under this check. An install from GitHub (`claude plugin marketplace add`) caches a copy keyed by
that string and takes no update until it changes, so a forgotten bump leaves
users on the old copy without a word. Without one, the version is the commit,
which is also what `./install.sh --version` reports. VERSION is the release
tag only: the one the PR-review Action template pins (`@v<VERSION>`).

Two kinds of plugin are listed. `toolkit-<profile>` is a profile plugin. `skill-<x>`
is a single-skill plugin: source `./plugins/<x>`, no dependencies, hooks or agents,
and one skill directory `<x>` whose SKILL.md names itself `<x>` and whose
`references/` mentions all exist.
"""
import argparse
import json
import os
import re
import sys

MARKETPLACE = "claude-code-toolkit"
PLUGIN_PREFIX = "toolkit-"
SKILL_PREFIX = "skill-"

VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
# A references/ path inside a SKILL.md, backticked or bare.
REFERENCE_RE = re.compile(r"references/[A-Za-z0-9_./-]*[A-Za-z0-9_]")
NAME_LINE_RE = re.compile(r"^name:\s*(.*?)\s*$")
HOOK_PATH_RE = re.compile(r'^"?\$\{CLAUDE_PLUGIN_ROOT\}"?/(\S+)')


def _load_json(path, problems):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError) as error:
        problems.append("%s: unreadable JSON (%s)" % (path, type(error).__name__))
        return None


def _inside(root, path):
    root = os.path.realpath(root)
    return os.path.commonpath([root, os.path.realpath(path)]) == root


def _lexically_inside(root, link_dir, target):
    """True when a relative link target stays under root without following links."""
    if os.path.isabs(target):
        return False
    joined = os.path.normpath(os.path.join(link_dir, target))
    return joined == root or joined.startswith(root + os.sep)


def front_matter_name(text):
    """The `name:` in a SKILL.md's leading YAML front matter, or None."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        match = NAME_LINE_RE.match(line)
        if match:
            return match.group(1).strip("'\"")
    return None


def check_skill_plugin(name, plugin_dir, manifest, rel):
    """Problems for one `skill-<x>` plugin whose directory is `plugin_dir`."""
    problems = []
    skill = name[len(SKILL_PREFIX):]
    if "dependencies" in manifest:
        problems.append("%s: skill plugin declares dependencies" % name)
    for forbidden in ("hooks", "agents"):
        if os.path.exists(os.path.join(plugin_dir, forbidden)):
            problems.append("%s: skill plugin has a %s/ directory" % (name, forbidden))
    skills = os.path.join(plugin_dir, "skills")
    entries = sorted(os.listdir(skills)) if os.path.isdir(skills) else []
    if entries != [skill]:
        problems.append("%s: skills/ must hold exactly one entry, %s (found %s)"
                        % (name, skill, ", ".join(entries) or "none"))
        return problems
    skill_dir = os.path.join(skills, skill)
    try:
        with open(os.path.join(skill_dir, "SKILL.md"), encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        problems.append("%s: skills/%s/SKILL.md is missing" % (name, skill))
        return problems
    if front_matter_name(text) != skill:
        problems.append("%s: SKILL.md front matter name is not %s" % (name, skill))
    for ref in sorted(set(REFERENCE_RE.findall(text))):
        target = os.path.join(skill_dir, ref)
        if not (os.path.isfile(target) and _inside(skill_dir, target)):
            problems.append("%s: SKILL.md mentions %s, which is not a file in %s"
                            % (name, ref, rel(skill_dir)))
    return problems


def check(root):
    # realpath, so paths derived through links compare against the same root.
    root = os.path.realpath(root)
    problems = []

    def rel(path):
        return os.path.relpath(path, root)

    def note_missing(path):
        """Record a missing in-tree path as a problem."""
        problems.append("missing: %s" % rel(path))

    # VERSION, the release tag.
    try:
        with open(os.path.join(root, "VERSION"), encoding="utf-8") as handle:
            if not VERSION_RE.match(handle.read().strip()):
                problems.append("VERSION: not MAJOR.MINOR.PATCH")
    except OSError:
        problems.append("VERSION: missing")

    # Marketplace and manifests.
    market = _load_json(os.path.join(root, ".claude-plugin", "marketplace.json"), problems)
    plugins = {}
    if market is not None:
        if market.get("name") != MARKETPLACE:
            problems.append("marketplace: name is not %s" % MARKETPLACE)
        entries = market.get("plugins")
        if not isinstance(entries, list) or not entries:
            problems.append("marketplace: no plugins listed")
            entries = []
        for entry in entries:
            name = entry.get("name", "")
            source = entry.get("source", "")
            if not name.startswith((PLUGIN_PREFIX, SKILL_PREFIX)):
                problems.append("marketplace: plugin %r lacks the %s or %s prefix"
                                % (name, PLUGIN_PREFIX, SKILL_PREFIX))
            if name.startswith(SKILL_PREFIX) and source != "./plugins/" + name[len(SKILL_PREFIX):]:
                problems.append("marketplace: %s source is not ./plugins/%s"
                                % (name, name[len(SKILL_PREFIX):]))
            if name in plugins:
                problems.append("marketplace: plugin %s listed twice" % name)
            if "version" in entry:
                problems.append("marketplace: %s sets a version" % name)
            if not (isinstance(source, str) and source.startswith("./")):
                problems.append("marketplace: %s source is not a ./ path" % name)
                continue
            plugin_dir = os.path.normpath(os.path.join(root, source))
            if not _lexically_inside(root, root, source) or not _inside(root, plugin_dir):
                problems.append("marketplace: %s source escapes the root" % name)
                continue
            manifest = _load_json(os.path.join(plugin_dir, ".claude-plugin", "plugin.json"), problems)
            if manifest is None:
                continue
            if manifest.get("name") != name:
                problems.append("%s: plugin.json name does not match the marketplace" % rel(plugin_dir))
            if "version" in manifest:
                problems.append("%s: plugin.json sets a version" % rel(plugin_dir))
            plugins[name] = (plugin_dir, manifest.get("dependencies", []))
            if name.startswith(SKILL_PREFIX):
                problems.extend(check_skill_plugin(name, plugin_dir, manifest, rel))

    # Dependencies: every one listed, and no cycle.
    for name, (_, deps) in sorted(plugins.items()):
        for dep in deps:
            if dep not in plugins:
                problems.append("%s: depends on unknown plugin %s" % (name, dep))
    state = {}

    def visit(name, trail):
        if state.get(name) == "done":
            return
        if state.get(name) == "active":
            problems.append("dependency cycle: %s" % " -> ".join(trail + [name]))
            return
        state[name] = "active"
        for dep in plugins.get(name, (None, []))[1]:
            if dep in plugins:
                visit(dep, trail + [name])
        state[name] = "done"

    for name in sorted(plugins):
        visit(name, [])

    # Symlinks: relative, inside the root, and resolving.
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for entry in dirnames + filenames:
            path = os.path.join(dirpath, entry)
            if not os.path.islink(path):
                continue
            target = os.readlink(path)
            if not _lexically_inside(root, dirpath, target):
                problems.append("link escapes the root: %s" % rel(path))
                continue
            if not os.path.exists(path):
                note_missing(os.path.normpath(os.path.join(dirpath, target)))
            elif not _inside(root, path):
                problems.append("link resolves outside the root: %s" % rel(path))

    # Hook commands: each ${CLAUDE_PLUGIN_ROOT} target resolves.
    for name, (plugin_dir, _) in sorted(plugins.items()):
        hooks_file = os.path.join(plugin_dir, "hooks", "hooks.json")
        if not os.path.exists(hooks_file):
            continue
        hooks = _load_json(hooks_file, problems)
        if hooks is None:
            continue
        for groups in (hooks.get("hooks") or {}).values():
            for group in groups:
                for hook in group.get("hooks", []):
                    match = HOOK_PATH_RE.match(hook.get("command", ""))
                    if not match:
                        problems.append("%s: hook command does not start at ${CLAUDE_PLUGIN_ROOT}" % name)
                        continue
                    target = os.path.join(plugin_dir, match.group(1))
                    if not os.path.exists(target):
                        # Report the in-root path, through the plugin's link.
                        real_parent = os.path.realpath(os.path.dirname(target))
                        note_missing(os.path.join(real_parent, os.path.basename(target)))
                    elif not os.access(target, os.X_OK):
                        problems.append("not executable: %s" % rel(os.path.realpath(target)))

    # A plugin with a `skills` directory must ship at least one skill.
    for name, (plugin_dir, _) in sorted(plugins.items()):
        skills = os.path.join(plugin_dir, "skills")
        if not os.path.isdir(skills):
            continue
        if not any(os.path.isfile(os.path.join(skills, entry, "SKILL.md"))
                   for entry in os.listdir(skills)):
            problems.append("%s ships no skill" % name)
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir))
    args = parser.parse_args(argv)
    problems = check(args.root)
    for problem in problems:
        print(problem)
    print("layout: %s" % ("ok" if not problems else "%d problem(s)" % len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
