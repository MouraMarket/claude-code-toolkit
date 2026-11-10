"""The docs/capabilities.json registry checks, shared by lib/tests/test_capabilities.py
and scripts/check-capabilities.py."""
import glob
import json
import os
import re
import shutil
import subprocess

import capability_evidence
import hook_manifest

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
KINDS = ("tool", "skill", "agent", "component")
KEYS = {"id", "kind", "summary", "delivery", "entry_points", "tests", "commands",
        "hooks", "config", "requires"}
CACHE = os.path.join(os.path.expanduser("~"), ".cache")


def _load(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as handle:
        return json.load(handle)


def load_registry():
    return _load("docs/capabilities.json")


def manifest_commands():
    """{command name: source path relative to the repo} from install.sh --manifest.

    Runs under a throwaway HOME inside the real ~/.cache (not TMPDIR or /tmp),
    with no config file and no GIT_* variables, and removes it afterwards.
    """
    os.makedirs(CACHE, exist_ok=True)
    home = os.path.join(CACHE, "capabilities-test-%d" % os.getpid())
    os.makedirs(home)
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["HOME"] = home
        env["USERPROFILE"] = home
        env["CLAUDE_TOOLKIT_CONFIG"] = os.path.join(home, "no-such-config.json")
        result = subprocess.run(["bash", os.path.join(REPO, "install.sh"), "--manifest"],
                                cwd=REPO, env=env, stdin=subprocess.DEVNULL,
                                capture_output=True, text=True, encoding="utf-8")
    finally:
        shutil.rmtree(home, ignore_errors=True)
    if result.returncode != 0:
        raise RuntimeError("install.sh --manifest exited %d: %s"
                           % (result.returncode, result.stderr.strip()[-300:]))
    root = os.path.realpath(REPO)
    rows = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if parts[0] == "link" and len(parts) >= 3:
            rel = os.path.relpath(os.path.realpath(parts[1]), root).replace(os.sep, "/")
            rows[os.path.basename(parts[2])] = rel
    if not rows:
        raise RuntimeError("install.sh --manifest yielded no link rows")
    return rows


def hook_relpaths():
    return {row["relpath"] for row in hook_manifest.plugin_registrations(REPO)}


def exists(rel):
    return os.path.exists(os.path.join(REPO, rel))


# ---- checks: each returns a list of problem strings ----

def check_schema(registry):
    problems = []
    entries = registry.get("capabilities")
    if not isinstance(entries, list) or not entries:
        return ["capabilities is not a non-empty list"]
    for entry in entries:
        name = entry.get("id", "?") if isinstance(entry, dict) else "?"
        if not isinstance(entry, dict) or set(entry) != KEYS:
            problems.append("%s: keys are not exactly %s" % (name, sorted(KEYS)))
            continue
        for key in ("entry_points", "tests", "commands", "hooks", "config", "requires"):
            if not isinstance(entry[key], list):
                problems.append("%s: %s is not a list" % (name, key))
        summary = entry["summary"]
        if not isinstance(summary, str) or not summary.strip():
            problems.append("%s: empty summary" % name)
        elif not summary.endswith(".") or re.search(r"[.!?]\s+\S", summary):
            problems.append("%s: summary is not one sentence" % name)
    return problems


def check_ids(registry):
    """An id repeats, or its prefix disagrees with its kind."""
    problems = []
    seen = set()
    for entry in registry["capabilities"]:
        ident, kind = entry["id"], entry["kind"]
        if ident in seen:
            problems.append("%s: id repeats" % ident)
        seen.add(ident)
        if kind not in KINDS:
            problems.append("%s: unknown kind %r" % (ident, kind))
        elif kind == "component":
            if ":" in ident:
                problems.append("%s: a component id has no prefix" % ident)
        elif not ident.startswith(kind + ":") or not ident.split(":", 1)[1]:
            problems.append("%s: prefix does not match kind %s" % (ident, kind))
    return problems


def _has_test(rel):
    path = os.path.join(REPO, rel)
    if os.path.isdir(path):
        return any(name.startswith("test_") and name.endswith(".py")
                   for _, _, names in os.walk(path) for name in names)
    name = os.path.basename(rel)
    return os.path.isfile(path) and name.startswith("test_") and name.endswith(".py")


def check_paths(registry):
    """An entry_points or tests path is missing, or a tests entry holds no test_*.py."""
    problems = []
    for entry in registry["capabilities"]:
        for rel in entry["entry_points"]:
            if not exists(rel):
                problems.append("%s: entry point %s does not exist" % (entry["id"], rel))
        for rel in entry["tests"]:
            if not exists(rel):
                problems.append("%s: tests path %s does not exist" % (entry["id"], rel))
            elif not _has_test(rel):
                problems.append("%s: tests path %s holds no test_*.py" % (entry["id"], rel))
    return problems


def _plugins_by_dir():
    return {os.path.normpath(p["source"]): p["name"]
            for p in _load(".claude-plugin/marketplace.json")["plugins"]}


def _skill_home(entry):
    """The delivery a skill's location implies, from its SKILL.md entry point."""
    for rel in entry["entry_points"]:
        parts = rel.split("/")
        if rel.endswith("/SKILL.md") and parts[0] == "skills" and len(parts) == 4:
            return "profile:" + parts[1]
        if rel.endswith("/SKILL.md") and parts[0] == "plugins" and len(parts) == 5:
            name = _plugins_by_dir().get("plugins/" + parts[1])
            return "plugin:" + name if name else None
    return None


def check_delivery(registry, registrations=None):
    problems = []
    profile_requires = {n: e.get("requires", []) for n, e in
                        _load("claude/profiles.json")["profiles"].items()
                        if isinstance(e, dict)}
    profiles = set(profile_requires)
    plugins = set(_plugins_by_dir().values())
    if registrations is None:
        registrations = hook_manifest.plugin_registrations(REPO)
    hook_profile = {}
    for row in registrations:
        hook_profile.setdefault(row["relpath"], set()).add(row["profile"])
    for entry in registry["capabilities"]:
        ident, delivery = entry["id"], entry["delivery"]
        kind, _, name = delivery.partition(":")
        if delivery == "checkout":
            pass
        elif kind == "profile" and name in profiles:
            pass
        elif kind == "plugin" and name in plugins:
            pass
        else:
            problems.append("%s: delivery %r names no real profile or plugin" % (ident, delivery))
            continue
        if entry["kind"] == "skill":
            home = _skill_home(entry)
            if home != delivery:
                problems.append("%s: delivery %s but the skill lives where %s" % (ident, delivery, home))
        if entry["kind"] == "agent" and delivery != "profile:builder":
            problems.append("%s: an agent ships in profile:builder" % ident)
        if entry["hooks"]:
            wanted = set()
            for rel in entry["hooks"]:
                wanted |= {"profile:" + p for p in hook_profile.get(rel, ())}
            # A hook may also register in a profile that directly requires the
            # delivery profile (the heartbeat runs in every profile, shipped by
            # core); any other placement means the registry names the wrong home.
            extra = {p for p in wanted - {delivery}
                     if name not in profile_requires.get(p.partition(":")[2], [])}
            if delivery not in wanted or extra:
                problems.append("%s: delivery %s but its hooks register under %s"
                                % (ident, delivery, sorted(wanted)))
    return problems


def check_config(registry, example_keys=None):
    if example_keys is None:
        example_keys = set(_load("config.example.json"))
    return ["%s: config key %r is not a top-level key of config.example.json" % (e["id"], k)
            for e in registry["capabilities"] for k in e["config"] if k not in example_keys]


def check_requires(registry):
    ids = {e["id"] for e in registry["capabilities"]}
    return ["%s: requires unknown id %s" % (e["id"], r)
            for e in registry["capabilities"] for r in e["requires"] if r not in ids]


def expected_capabilities():
    """Every id the repo's layout demands an entry for."""
    wanted = set()
    for rel in glob.glob(os.path.join(REPO, "skills", "*", "*", "SKILL.md")):
        wanted.add("skill:" + os.path.basename(os.path.dirname(rel)))
    for rel in glob.glob(os.path.join(REPO, "plugins", "*", "skills", "*", "SKILL.md")):
        wanted.add("skill:" + os.path.basename(os.path.dirname(rel)))
    for rel in glob.glob(os.path.join(REPO, "agents", "*.md")):
        wanted.add("agent:" + os.path.basename(rel)[:-3])
    for name in os.listdir(os.path.join(REPO, "tools")):
        if os.path.isdir(os.path.join(REPO, "tools", name)):
            wanted.add("tool:" + name)
    return wanted


def check_coverage(registry):
    have = {e["id"] for e in registry["capabilities"]}
    return ["%s has no entry" % ident for ident in sorted(expected_capabilities() - have)]


def check_unowned_suites(registry):
    """A suite under a CI root that no capability's `tests` covers. The release
    run reports these too, but only this check runs in local CI, so it is what
    fails the PR that adds one."""
    owned = {suite for entry in registry["capabilities"] for rel in entry["tests"]
             for suite in capability_evidence.suites_for(REPO, rel)}
    return ["%s is owned by no capability" % suite
            for suite in capability_evidence.all_suites(REPO) if suite not in owned]


def check_tool_entry_points(registry):
    """Each tool entry points only into its own tools/<dir>/, and each tool dir has one entry."""
    problems = []
    for entry in registry["capabilities"]:
        if entry["kind"] != "tool":
            continue
        prefix = "tools/%s/" % entry["id"].split(":", 1)[1]
        if not entry["entry_points"]:
            problems.append("%s: no entry points" % entry["id"])
        problems += ["%s: entry point %s is outside %s" % (entry["id"], rel, prefix)
                     for rel in entry["entry_points"] if not rel.startswith(prefix)]
    return problems


def check_commands(registry, commands):
    """Every manifest command is listed under the tool its source lives in; every listed one exists."""
    problems = []
    listed = {}
    for entry in registry["capabilities"]:
        for name in entry["commands"]:
            if name in listed:
                problems.append("command %s listed twice" % name)
            listed[name] = entry["id"]
    for name, source in sorted(commands.items()):
        if name not in listed:
            problems.append("command %s is unlisted" % name)
        elif source.startswith("tools/") and listed[name] != "tool:" + source.split("/")[1]:
            problems.append("command %s is listed under %s, not its tool" % (name, listed[name]))
    problems += ["listed command %s is not in install.sh --manifest" % name
                 for name in sorted(set(listed) - set(commands))]
    return problems


def check_hooks(registry, relpaths):
    problems = []
    listed = {rel for e in registry["capabilities"] for rel in e["hooks"]}
    problems += ["hook %s is unlisted" % rel for rel in sorted(relpaths - listed)]
    problems += ["listed hook %s is not registered" % rel for rel in sorted(listed - relpaths)]
    problems += ["listed hook %s does not exist" % rel for rel in sorted(listed) if not exists(rel)]
    return problems


PRIVATE = re.compile(r"^(/|[A-Za-z]:[\\/])|\s/[A-Za-z]|~/|[Rr]ow [0-9]|PENDING|\b20[0-9]{2}-[0-9]{2}")


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            for found in _strings(item):
                yield found
    elif isinstance(node, dict):
        for value in node.values():
            for found in _strings(value):
                yield found


def check_private(node):
    """No absolute path, home path, date or plan row in any string of the registry.

    Walks parsed values, not raw text: the "//" comment key is JSON syntax.
    """
    return ["private-looking text: %r" % text for text in _strings(node)
            if PRIVATE.search(text)]
