"""Shared fixture for every install.sh suite.

install.sh registers the `toolkit-core` plugin through the `claude` CLI, so a
fixture run needs three things the old template-merge did not:

  1. the plugin layout in the fixture repo (`add_plugin_layout`),
  2. a `claude` on PATH that behaves like the real CLI for exactly the
     subcommands install.sh calls (`install_claude_shim`), and
  3. an environment that puts that shim first (`shim_env`).

The shim never touches the real CLI or the real HOME. It emulates the shapes
and the write behaviour VERIFIED against CLI 2.1.280:
`plugin list --json` and `plugin marketplace list --json` print arrays, and
`marketplace add` / `plugin install|enable|disable|uninstall` write
`extraKnownMarketplaces` / `enabledPlugins` into `$HOME/.claude/settings.json`
BEFORE any later step can fail.
"""

import atexit
import hashlib
import json
import os
import shutil
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PLUGIN_ID = "toolkit-core@claude-code-toolkit"

_SHIM = r'''#!/usr/bin/env python3
import json, os, sys

STATE = os.environ.get("CLAUDE_SHIM_STATE") or %(state)r
argv = sys.argv[1:]

with open(STATE + ".log", "a") as log:
    log.write(" ".join(argv) + "\n")

fail = os.environ.get("CLAUDE_SHIM_FAIL")
if fail and fail in argv:
    sys.stderr.write("claude shim: forced failure of %%s\n" %% fail)
    sys.exit(1)
noop = os.environ.get("CLAUDE_SHIM_NOOP")
bad_json = os.environ.get("CLAUDE_SHIM_BAD_JSON") == "1"

with open(STATE) as handle:
    state = json.load(handle)


def save():
    with open(STATE, "w") as handle:
        json.dump(state, handle, indent=2)


def edit_settings(mutate):
    path = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    try:
        with open(path) as handle:
            data = json.load(handle)
    except FileNotFoundError:
        data = {}
    except ValueError:
        return  # unparseable settings: leave them alone, as a careful CLI would
    mutate(data)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def emit(value):
    sys.stdout.write("not json {\n" if bad_json else json.dumps(value) + "\n")


if argv[:2] == ["plugin", "list"] and "--json" in argv:
    emit(state["plugins"])
elif argv[:3] == ["plugin", "marketplace", "list"] and "--json" in argv:
    emit(state["marketplaces"])
elif argv[:3] == ["plugin", "marketplace", "add"]:
    path = argv[3]
    if not (noop and noop in argv):
        if os.path.isdir(path):
            with open(os.path.join(path, ".claude-plugin", "marketplace.json")) as handle:
                name = json.load(handle)["name"]
            entry = {"name": name, "source": "directory",
                     "path": path, "installLocation": path}
            source = {"source": "directory", "path": path}
        else:
            # A GitHub source: only the repos named in the env map are reachable.
            known = json.loads(os.environ.get("CLAUDE_SHIM_GITHUB_MARKETPLACES") or "{}")
            if path not in known:
                sys.stderr.write("repository not found\n")
                sys.exit(1)
            name = known[path]
            entry = {"name": name, "source": "github", "repo": path, "path": None}
            source = {"source": "github", "repo": path}
        state["marketplaces"] = [m for m in state["marketplaces"] if m["name"] != name]
        state["marketplaces"].append(entry)
        save()
        edit_settings(lambda d: d.setdefault("extraKnownMarketplaces", {}).update(
            {name: {"source": source}}))
    print("Successfully added marketplace")
elif argv[:3] == ["plugin", "marketplace", "remove"]:
    name = argv[3]
    if not (noop and noop in argv):
        state["marketplaces"] = [m for m in state["marketplaces"] if m["name"] != name]
        state["plugins"] = [p for p in state["plugins"]
                            if not p["id"].endswith("@" + name)]
        save()
        edit_settings(lambda d: d.get("extraKnownMarketplaces", {}).pop(name, None))
    print("Removed marketplace")
elif argv[:2] in (["plugin", "install"], ["plugin", "enable"], ["plugin", "disable"]):
    # Like the real CLI: `--scope/-s <s>`, defaulting to user.
    rest, scope, skip = [], "user", False
    for index, arg in enumerate(argv[2:], 2):
        if skip:
            skip = False
        elif arg in ("--scope", "-s"):
            scope = argv[index + 1] if index + 1 < len(argv) else ""
            skip = True
        elif arg.startswith("--scope="):
            scope = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            rest.append(arg)
    pid = rest[0]
    if argv[1] == "install":
        if not (noop and noop in argv):
            market = pid.split("@", 1)[1]
            if not any(m["name"] == market for m in state["marketplaces"]):
                sys.stderr.write("marketplace not found\n")
                sys.exit(1)
            state["plugins"] = [p for p in state["plugins"]
                                if not (p["id"] == pid and p.get("scope") == scope)]
            state["plugins"].append({"id": pid, "enabled": True, "scope": scope,
                                     "installPath": "/shim/" + pid})
            save()
            if scope == "user":
                edit_settings(lambda d: d.setdefault("enabledPlugins", {}).update({pid: True}))
        print("Successfully installed")
    else:
        on = argv[1] == "enable"
        if not (noop and noop in argv):
            for plugin in state["plugins"]:
                if plugin["id"] == pid and plugin.get("scope") == scope:
                    plugin["enabled"] = on
            save()
            if scope == "user":
                edit_settings(lambda d: d.setdefault("enabledPlugins", {}).update({pid: on}))
        print("Done")
elif argv[:3] == ["plugin", "marketplace", "update"]:
    name = argv[3] if len(argv) > 3 else None
    if name is not None and not any(m["name"] == name for m in state["marketplaces"]):
        sys.stderr.write("marketplace not found\n")
        sys.exit(1)
    print("Successfully updated marketplace")
elif argv[:2] == ["plugin", "update"]:
    # Like the real CLI: `--scope/-s <s>`, defaulting to user. The version it
    # moves to comes from CLAUDE_SHIM_LATEST ({id: version}); absent = unchanged.
    rest, scope, skip = [], "user", False
    for index, arg in enumerate(argv[2:], 2):
        if skip:
            skip = False
        elif arg in ("--scope", "-s"):
            scope = argv[index + 1] if index + 1 < len(argv) else ""
            skip = True
        elif not arg.startswith("-"):
            rest.append(arg)
    pid = rest[0]
    match = [p for p in state["plugins"] if p["id"] == pid and p.get("scope") == scope]
    if not match:
        sys.stderr.write("plugin not installed\n")
        sys.exit(1)
    latest = json.loads(os.environ.get("CLAUDE_SHIM_LATEST") or "{}")
    if pid in latest and not (noop and noop in argv):
        match[0]["version"] = latest[pid]
        save()
    print("Updated " + pid)
elif argv[:2] in (["plugin", "uninstall"], ["plugin", "remove"]):
    # Like the real CLI: scope-aware, and never refuses over a dependent plugin.
    rest, scope, skip = [], "user", False
    for index, arg in enumerate(argv[2:], 2):
        if skip:
            skip = False
        elif arg in ("--scope", "-s"):
            scope = argv[index + 1] if index + 1 < len(argv) else ""
            skip = True
        elif arg.startswith("--scope="):
            scope = arg.split("=", 1)[1]
        elif not arg.startswith("-"):
            rest.append(arg)
    pid = rest[0]
    if not any(p["id"] == pid and p.get("scope") == scope for p in state["plugins"]):
        sys.stderr.write("plugin not installed\n")
        sys.exit(1)
    if not (noop and noop in argv):
        state["plugins"] = [p for p in state["plugins"]
                            if not (p["id"] == pid and p.get("scope") == scope)]
        save()
        if scope == "user":
            edit_settings(lambda d: d.get("enabledPlugins", {}).pop(pid, None))
    print("Successfully uninstalled plugin: " + pid)
else:
    sys.stderr.write("claude shim: unsupported: %%s\n" %% " ".join(argv))
    sys.exit(2)
'''


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def real_legacy_commands():
    with open(os.path.join(REPO_ROOT, "claude", "legacy-hook-commands.json")) as handle:
        return json.load(handle)["commands"]


def real_host_settings():
    with open(os.path.join(REPO_ROOT, "claude", "settings.host.json")) as handle:
        return json.load(handle)


def real_profiles():
    with open(os.path.join(REPO_ROOT, "claude", "profiles.json")) as handle:
        return json.load(handle)


def add_plugin_layout(repo, hooks=None, host=None, legacy=None, profiles=None):
    """Give a fixture repo everything install.sh's plugin step reads.

    `profiles` replaces the registry (a dict shaped like claude/profiles.json);
    each profile whose dir is not `plugin` gets a minimal plugin directory and
    a marketplace entry."""
    registry = profiles if profiles is not None else real_profiles()
    market_plugins = [{"name": "toolkit-core", "source": "./plugin"}]
    for entry in registry["profiles"].values():
        if not isinstance(entry, dict) or entry.get("dir") == "plugin":
            continue
        directory = entry["dir"]
        _write_json(os.path.join(repo, directory, ".claude-plugin", "plugin.json"),
                    {"name": entry["plugin"]})
        _write_json(os.path.join(repo, directory, "hooks", "hooks.json"), {"hooks": {}})
        os.makedirs(os.path.join(repo, directory, "skills"), exist_ok=True)
        open(os.path.join(repo, directory, "skills", ".keep"), "w").close()
        if entry.get("agents"):
            os.makedirs(os.path.join(repo, directory, "agents"), exist_ok=True)
            open(os.path.join(repo, directory, "agents", ".keep"), "w").close()
        market_plugins.append({"name": entry["plugin"], "source": "./" + directory})
    _write_json(os.path.join(repo, ".claude-plugin", "marketplace.json"), {
        "name": "claude-code-toolkit", "owner": {"name": "fixture-owner"},
        "plugins": market_plugins})
    _write_json(os.path.join(repo, "claude", "profiles.json"), registry)
    _write_json(os.path.join(repo, "plugin", ".claude-plugin", "plugin.json"),
                {"name": "toolkit-core"})
    _write_json(os.path.join(repo, "plugin", "hooks", "hooks.json"),
                {"hooks": hooks if hooks is not None else {}})
    _write_json(os.path.join(repo, "claude", "settings.host.json"),
                host if host is not None else real_host_settings())
    _write_json(os.path.join(repo, "claude", "legacy-hook-commands.json"),
                {"commands": legacy if legacy is not None else real_legacy_commands()})
    dest = os.path.join(repo, "lib", "hook_manifest.py")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.lexists(dest):
        os.remove(dest)
    shutil.copyfile(os.path.join(REPO_ROOT, "lib", "hook_manifest.py"), dest)
    # Only the modules install.sh imports. setup_config.py answers the
    # declined-core preflight, which fails closed without it once a config
    # exists; hook_heartbeat.py answers --verify-live and the Live and Guards
    # lines and readiness.py their per-profile readiness block; toolkit_update.py
    # answers --version, --update and --rollback;
    # plugin_deps.py answers which plugins the deselected-profile step must keep;
    # settings_io.py and auto_memory.py are what toolkit_update.py imports;
    # secret_scan_status.py answers the Secret scanning section.
    for module in ("auto_memory.py", "claude_home.py", "hook_heartbeat.py", "install_record.py",
                   "plugin_deps.py", "plugin_paths.py", "profiles.py", "readiness.py",
                   "secret_scan_status.py", "settings_io.py", "setup_config.py",
                   "toolkit_config.py", "toolkit_update.py", "workflows_setting.py"):
        dest = os.path.join(repo, "lib", module)
        if os.path.lexists(dest):
            os.remove(dest)
        shutil.copyfile(os.path.join(REPO_ROOT, "lib", module), dest)
    # The plugin ships `skills/` and `agents/` through relative symlinks. A
    # `.keep` in each keeps the directory in a fixture's git commit.
    for component in ("skills", "agents"):
        os.makedirs(os.path.join(repo, component), exist_ok=True)
        keep = os.path.join(repo, component, ".keep")
        if not os.path.lexists(keep):
            open(keep, "w").close()
        link = os.path.join(repo, "plugin", component)
        if os.path.lexists(link):
            os.remove(link)
        os.symlink("../" + component, link)


# install.sh content hash -> [(target relative to repo, destination relative to
# home)]. Every `link` call is top-level and unconditional, so `--manifest`
# depends only on the script's content, REPO and HOME; caching it per process
# saves a bash run per setUp, which was slowing whole CI legs enough to
# starve unrelated timing tests.
_LINK_ROWS = {}


def link_rows(repo, home):
    """(target, destination) for every `link` call in the fixture's own
    install.sh, read with `--manifest` against a sandbox `home`. The caller
    copies install.sh into `repo` first, as the suites already do."""
    with open(os.path.join(repo, "install.sh"), "rb") as handle:
        key = hashlib.sha256(handle.read()).hexdigest()
    cached = _LINK_ROWS.get(key)
    if cached is not None:
        return [(os.path.join(repo, t), os.path.join(home, d)) for t, d in cached]
    rows = _manifest_link_rows(repo, home)
    relative = []
    for target, linkname in rows:
        if not (target.startswith(repo + os.sep) and linkname.startswith(home + os.sep)):
            return rows    # a path we cannot re-root: serve it, cache nothing
        relative.append((target[len(repo) + 1:], linkname[len(home) + 1:]))
    _LINK_ROWS[key] = relative
    return rows


def _manifest_link_rows(repo, home):
    env = dict(os.environ)
    env["HOME"] = home
    out = subprocess.run(["bash", os.path.join(repo, "install.sh"), "--manifest"],
                         env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, universal_newlines=True, check=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "link":
            rows.append((parts[1], parts[2]))
    return rows


def add_link_targets(repo, home):
    """Create every link TARGET install.sh names that is missing, as an empty
    executable file. Never overwrites. Without this a minimal fixture repo
    reads every link as 'missing in repo', which is now a required failure."""
    for target, _ in link_rows(repo, home):
        if os.path.lexists(target):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w"):
            pass
        os.chmod(target, 0o755)


def stub_pending_components(repo):
    """In a COPY of the real repo, stand in for what later port rows ship: an
    empty executable for every hook target a profile's hooks.json names that
    is absent, and an empty directory behind every profile `skills`/`agents`
    link whose target is absent. Never overwrites, and returns what it made,
    so a suite can tell a stub from the real thing.

    install.sh itself stays strict: on the real checkout a missing hook target
    or component directory is still a failure. Only a sandbox that copies the
    whole repo (to exercise the prune or the component checks) calls this, the
    same way `add_link_targets` stands in for link targets in a stub repo."""
    import sys
    sys.path.insert(0, os.path.join(repo, "lib"))
    try:
        import hook_manifest
        import profiles
    finally:
        sys.path.pop(0)
    made = []
    for row in hook_manifest.plugin_registrations(repo):
        target = hook_manifest.resolve(repo, row["relpath"], row["profile"])
        if os.path.lexists(target):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w") as handle:
            handle.write("#!/bin/sh\nexit 0\n")
        os.chmod(target, 0o755)
        made.append(target)
    for entry in profiles.load_registry(repo).values():
        for component in ("skills", "agents"):
            link = os.path.join(repo, entry["dir"], component)
            if not os.path.islink(link) or os.path.isdir(link):
                continue
            target = os.path.join(os.path.dirname(link), os.readlink(link))
            os.makedirs(target, exist_ok=True)
            made.append(target)
    return made


def install_links(repo, home):
    """A fully installed HOME without an apply: symlink every link DESTINATION
    to its target unless something already exists there."""
    for target, linkname in link_rows(repo, home):
        if os.path.lexists(linkname):
            continue
        os.makedirs(os.path.dirname(linkname), exist_ok=True)
        os.symlink(target, linkname)


def install_claude_shim(bin_dir, state_path):
    """Write an executable `claude` into bin_dir and an empty state file."""
    os.makedirs(bin_dir, exist_ok=True)
    shim = os.path.join(bin_dir, "claude")
    with open(shim, "w") as handle:
        handle.write(_SHIM % {"state": state_path})
    os.chmod(shim, 0o755)
    _write_json(state_path, {"marketplaces": [], "plugins": []})
    log = state_path + ".log"
    if os.path.exists(log):
        os.remove(log)
    return shim


def seed_installed(state_path, repo):
    """State as if install.sh's steps 2-3 had already run for `repo`: core and
    the default-on continuity plugin, which a no-config install selects."""
    path = os.path.realpath(repo)
    _write_json(state_path, {
        "marketplaces": [{"name": "claude-code-toolkit", "source": "directory",
                          "path": path, "installLocation": path}],
        "plugins": [{"id": plugin_id, "enabled": True, "scope": "user",
                     "installPath": "/shim/" + plugin_id}
                    for plugin_id in (PLUGIN_ID, "toolkit-continuity@claude-code-toolkit")]})


def seed_project_scoped(state_path, repo, enabled=True):
    """State with the marketplace here and the plugin installed at PROJECT
    scope only: `plugin list --json` shows it, but it is not the user's."""
    seed_installed(state_path, repo)
    with open(state_path) as handle:
        state = json.load(handle)
    state["plugins"] = [{"id": PLUGIN_ID, "enabled": enabled, "scope": "project",
                         "installPath": "/shim/" + PLUGIN_ID}]
    _write_json(state_path, state)


def seed_plugins(state_path, marketplaces, plugins):
    """Write an arbitrary shim state: `marketplaces` is a list of names (seeded
    as GitHub sources), `plugins` a list of (id, enabled, scope) or (id, enabled, scope, version) tuples."""
    rows = []
    for plugin in plugins:
        row = {"id": plugin[0], "enabled": plugin[1], "scope": plugin[2],
               "installPath": "/shim/" + plugin[0]}
        if len(plugin) > 3:
            row["version"] = plugin[3]
        rows.append(row)
    _write_json(state_path, {
        "marketplaces": [{"name": n, "source": "github", "repo": "seed/" + n,
                          "path": None} for n in marketplaces],
        "plugins": rows})


def shim_log(state_path):
    """The shim's argv log as a list of argument strings, one per call."""
    try:
        with open(state_path + ".log") as handle:
            return [line.rstrip("\n") for line in handle]
    except FileNotFoundError:
        return []


def shim_env(base_env, bin_dir, state_path, skip_suites=True):
    """base_env with the shim first on PATH, and the suite sweep skipped unless
    the caller opts out (a fixture run must not run the toolkit's suites)."""
    env = dict(base_env)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["CLAUDE_SHIM_STATE"] = state_path
    # Managed settings are read from here, never from /Library/..., so a real
    # machine-wide file can not leak into a fixture run. Callers that test
    # managed settings override it after this call.
    env.setdefault("CLAUDE_TOOLKIT_MANAGED_DIR", bin_dir.rstrip(os.sep) + ".managed")
    if skip_suites:
        env["CLAUDE_INSTALL_SKIP_SUITES"] = "1"
    return env


def path_without_claude(scratch_dir, base_path=None):
    """A PATH on which `claude` does not resolve but everything else does.

    A real `claude` may sit on the machine's PATH. Any directory holding one is
    replaced by a shadow directory of symlinks to its other entries."""
    parts = (base_path if base_path is not None else os.environ.get("PATH", "")).split(os.pathsep)
    out = []
    for index, directory in enumerate(parts):
        if not directory or not os.path.isdir(directory):
            continue
        if not os.path.lexists(os.path.join(directory, "claude")):
            out.append(directory)
            continue
        shadow = os.path.join(scratch_dir, "noclaude-%d" % index)
        os.makedirs(shadow, exist_ok=True)
        for entry in os.listdir(directory):
            if entry != "claude":
                try:
                    os.symlink(os.path.join(directory, entry), os.path.join(shadow, entry))
                except OSError:
                    pass
        out.append(shadow)
    return os.pathsep.join(out)


def shim_for_home(home):
    """(bin_dir, state_path) of a shim that lives BESIDE a sandbox `home`, so
    suites with their own run_install helper need one call and no new setup.
    Created on first use and never reset, so a seeded state survives later runs
    against the same home."""
    bin_dir = home.rstrip(os.sep) + ".shim-bin"
    state_path = home.rstrip(os.sep) + ".shim-state.json"
    if not os.path.exists(os.path.join(bin_dir, "claude")):
        install_claude_shim(bin_dir, state_path)
        atexit.register(shutil.rmtree, bin_dir, True)
        atexit.register(lambda: [os.path.exists(p) and os.remove(p)
                                 for p in (state_path, state_path + ".log")])
    return bin_dir, state_path


def shim_env_for_home(base_env, home, skip_suites=True):
    bin_dir, state_path = shim_for_home(home)
    return shim_env(base_env, bin_dir, state_path, skip_suites=skip_suites)


def seed_home_installed(home, repo):
    """seed_installed for a suite that uses `shim_for_home`."""
    seed_installed(shim_for_home(home)[1], repo)
