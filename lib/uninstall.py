#!/usr/bin/env python3
"""Ownership-aware uninstall for claude-code-toolkit: what we may remove, and why.

`gather` reads every surface the installer writes to, once, into a plain dict.
`plan` is a pure function of that dict: it returns the Actions the uninstall may
take, what it keeps and why, and what blocks it. Nothing is removed unless the
install record or an exact-match rule proves the toolkit put it there.

`apply` performs the Actions and `main` is the `install.sh --uninstall [--yes]`
entry point. Stdlib plus the sibling lib modules; importable by path.
"""

import copy
import errno
import json
import os
import re
import subprocess
import sys
from collections import namedtuple

LIB = os.path.dirname(os.path.realpath(__file__))
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import auto_memory  # noqa: E402
import claude_imports  # noqa: E402
import hook_manifest  # noqa: E402
import install_record  # noqa: E402
import plugin_deps  # noqa: E402
import plugin_paths  # noqa: E402
import team_policy  # noqa: E402
import toolkit_config  # noqa: E402
import workflows_setting  # noqa: E402
from settings_io import Action, apply_settings_actions, write_settings  # noqa: E402

Kept = namedtuple("Kept", "surface detail reason")
Blocker = namedtuple("Blocker", "surface detail remediation")

MARKETPLACE = hook_manifest.MARKETPLACE
LEGACY_DIRS = plugin_paths.LEGACY_DIRS
_MISSING = (FileNotFoundError, NotADirectoryError)
_write_settings = write_settings


# --------------------------------------------------------------------------
# gather
# --------------------------------------------------------------------------

def _inside(repo, path):
    """True when `path` (non-strict realpath) sits inside `repo` by component."""
    try:
        repo_real = os.path.realpath(repo)
        return os.path.commonpath([repo_real, os.path.realpath(path)]) == repo_real
    except (ValueError, TypeError, AttributeError):
        return False


def _link_target(link_path, value):
    return value if os.path.isabs(value) else os.path.join(os.path.dirname(link_path), value)


def _entries(directory):
    """[(path, is_symlink, readlink_value)] for a directory; [] when it is missing."""
    try:
        names = sorted(os.listdir(directory))
    except _MISSING:
        return []
    found = []
    for name in names:
        path = os.path.join(directory, name)
        if os.path.islink(path):
            found.append((path, True, os.readlink(path)))
        else:
            found.append((path, False, None))
    return found


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _cli_list(run_cli, argv):
    """The parsed JSON array a `claude` call prints. Raises ValueError otherwise."""
    try:
        ok, out = run_cli(argv)
    except Exception as exc:  # a missing binary, a timeout
        raise ValueError(str(exc) or type(exc).__name__)
    if not ok:
        raise ValueError((out or "").strip() or "claude %s failed" % " ".join(argv))
    try:
        data = json.loads(out)
    except ValueError:
        raise ValueError("claude %s did not print JSON" % " ".join(argv))
    if not isinstance(data, list):
        raise ValueError("claude %s did not print a JSON array" % " ".join(argv))
    return data


def _git_list(run_git, argv):
    """[(origin, "key=value")]; [] when the global config file does not exist."""
    returncode, out, err = run_git(argv)
    if returncode == 128 and "unable to read config file" in err and "No such file or directory" in err:
        return []
    if returncode != 0:
        raise ValueError("git %s exited %s: %s" % (" ".join(argv), returncode, (err or "").strip()))
    pairs = []
    for line in out.splitlines():
        origin, tab, rest = line.partition("\t")
        pairs.append((origin, rest) if tab else ("", line))
    return pairs


def gather(repo, home, settings_path, run_cli, run_git, manifest_rows):
    """Read every surface once into a dict. Never raises.

    `run_cli(argv) -> (ok, stdout)` runs `claude <argv>`. `run_git(argv) ->
    (returncode, stdout, stderr)` runs `git <argv>` (argv without the leading
    `git`). Absence is not an error; any other failure lands in
    `state["errors"]` as `(surface, message)` and `plan` turns it into a Blocker.
    `manifest_rows` is a list of `(target, linkname)`.
    """
    state = {
        "errors": [], "settings": {}, "record": None, "record_unreadable": False,
        "plugins": [], "marketplaces": [], "plugin_dependencies": {},
        "retained_plugin_dependencies": {}, "local_bin": [], "home_bin": [],
        "legacy_links": [], "managed_links": list(manifest_rows),
        "claude_md_path": os.path.join(home, ".claude", "CLAUDE.md"), "claude_md": None,
        "claude_md_is_symlink": False, "claude_md_imports": [], "policy_real": None,
        "git_global": [], "hooks_path": None, "hooks_path_origin": None,
        "global_config_origin": None,
        "hooks_dir": os.path.join(home, ".config", "claude-code-toolkit", "git-hooks"),
        "hooks_dir_entries": [], "hooks_dir_exists": True,
        "wanted_status_line": None, "legacy_commands": set(), "auto_memory_target": None,
        "baseline_rules": {}, "autonomous_default_mode": None,
    }
    for step in (_gather_settings, _gather_record, _gather_plugins, _gather_dependencies,
                 _gather_links, _gather_claude_md, _gather_git, _gather_manifest_values,
                 _gather_baseline_rules):
        try:
            step(state, repo, home, settings_path, run_cli, run_git)
        except Exception as exc:  # a gap in one surface must not hide the rest
            state["errors"].append((step.__name__[len("_gather_"):], "%s: %s" % (type(exc).__name__, exc)))
    return state


def _gather_settings(state, repo, home, settings_path, run_cli, run_git):
    try:
        data = _read_json(settings_path)
    except _MISSING:
        return
    except (OSError, ValueError) as exc:
        state["errors"].append(("settings", "%s is unreadable: %s" % (settings_path, exc)))
        return
    if not isinstance(data, dict):
        state["errors"].append(("settings", "%s is not a JSON object" % settings_path))
        return
    state["settings"] = data


def _gather_record(state, repo, home, settings_path, run_cli, run_git):
    path = install_record.record_path(home)
    state["record"] = install_record.load(path)
    state["record_unreadable"] = state["record"] is None and os.path.lexists(path)


def _gather_plugins(state, repo, home, settings_path, run_cli, run_git):
    for key, argv in (("plugins", ["plugin", "list", "--json"]),
                      ("marketplaces", ["plugin", "marketplace", "list", "--json"])):
        try:
            state[key] = _cli_list(run_cli, argv)
        except ValueError as exc:
            state["errors"].append((key, str(exc)))
    retained, errors = plugin_deps.installed_dependencies(
        home, state["plugins"], state["marketplaces"], MARKETPLACE)
    state["errors"].extend(("retained-plugins", message) for message in errors)
    state["retained_plugin_dependencies"] = retained


def _gather_dependencies(state, repo, home, settings_path, run_cli, run_git):
    try:
        state["plugin_dependencies"] = plugin_deps.toolkit_dependencies(repo)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        state["errors"].append(("plugin-dependencies", "cannot read the toolkit's plugin manifests: %s" % exc))


def _gather_links(state, repo, home, settings_path, run_cli, run_git):
    for key, directory in (("local_bin", os.path.join(home, ".local", "bin")),
                           ("home_bin", os.path.join(home, "bin"))):
        try:
            state[key] = _entries(directory)
        except OSError as exc:
            state["errors"].append((key.replace("_", "-"), "cannot list %s: %s" % (directory, exc)))
    links = []
    for sub in LEGACY_DIRS:
        directory = os.path.join(home, ".claude", sub)
        try:
            for path, is_link, value in _entries(directory):
                if is_link:
                    links.append((path, value, _inside(repo, _link_target(path, value))))
        except OSError as exc:
            state["errors"].append(("legacy-links", "cannot list %s: %s" % (directory, exc)))
    state["legacy_links"] = links


def _gather_claude_md(state, repo, home, settings_path, run_cli, run_git):
    path = state["claude_md_path"]
    state["policy_real"] = os.path.realpath(team_policy.policy_path(repo))
    state["claude_md_is_symlink"] = os.path.islink(path)
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except _MISSING:
        return
    except (OSError, UnicodeDecodeError) as exc:
        state["errors"].append(("claude-md", "cannot read %s: %s" % (path, exc)))
        return
    state["claude_md"] = text
    lines = text.split("\n")
    state["claude_md_imports"] = [
        (number, os.path.realpath(claude_imports.resolve(token, path, home)),
         claude_imports.is_import_only(lines[number - 1], token))
        for number, token in claude_imports.iter_imports(text)]


def _gather_git(state, repo, home, settings_path, run_cli, run_git):
    hooks_dir = state["hooks_dir"]
    try:
        state["hooks_dir_entries"] = _entries(hooks_dir)
        state["hooks_dir_exists"] = os.path.isdir(hooks_dir)
    except OSError as exc:
        state["errors"].append(("git", "cannot list %s: %s" % (hooks_dir, exc)))
    # --includes: without it a value set in an included file is invisible.
    try:
        pairs = _git_list(run_git, ["config", "--global", "--includes", "--show-origin", "--list"])
        top = _git_list(run_git, ["config", "--global", "--show-origin", "--list"])
    except ValueError as exc:
        state["errors"].append(("git", str(exc)))
        return
    state["git_global"] = pairs
    for origin, line in pairs:
        if line.lower().startswith("core.hookspath="):
            state["hooks_path"] = line.split("=", 1)[1]
            state["hooks_path_origin"] = origin
    if top:
        state["global_config_origin"] = top[0][0]
    else:
        state["global_config_origin"] = "file:" + (os.environ.get("GIT_CONFIG_GLOBAL")
                                                   or os.path.join(home, ".gitconfig"))
    # git also reads $XDG_CONFIG_HOME/git/config as global config.
    xdg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "git", "config")
    if os.path.isfile(xdg) and "file:" + xdg != state["global_config_origin"]:
        try:
            extra = _git_list(run_git, ["config", "--file", xdg, "--includes", "--show-origin", "--list"])
        except ValueError as exc:
            state["errors"].append(("git", str(exc)))
            return
        # `--global --list` may already have listed these lines: add only the new ones.
        state["git_global"] = pairs + [pair for pair in extra if pair not in pairs]
        for origin, line in state["git_global"]:
            if line.lower().startswith("core.hookspath="):
                state["hooks_path"] = line.split("=", 1)[1]
                state["hooks_path_origin"] = origin


def _gather_manifest_values(state, repo, home, settings_path, run_cli, run_git):
    for key, surface, compute in (
            ("wanted_status_line", "statusLine", lambda: hook_manifest.wanted_status_line(repo, home)),
            ("legacy_commands", "legacy-hooks", lambda: hook_manifest.legacy_commands(repo, home)),
            ("auto_memory_target", "autoMemoryDirectory", lambda: auto_memory.managed_target(
                toolkit_config.get("workspace_root"), home)[0])):
        try:
            state[key] = compute()
        except Exception as exc:
            state["errors"].append((surface, "cannot compute what the toolkit installs: %s" % exc))


def _gather_baseline_rules(state, repo, home, settings_path, run_cli, run_git):
    rules = {}
    for name in ("baseline", "autonomous"):
        path = os.path.join(repo, "claude", "permissions.%s.json" % name)
        try:
            perms = _read_json(path)["permissions"]
        except _MISSING:
            continue
        except (OSError, ValueError, KeyError, TypeError) as exc:
            state["errors"].append(("baseline-rules", "cannot read %s: %s" % (path, exc)))
            continue
        if not isinstance(perms, dict):
            state["errors"].append(("baseline-rules", "%s: permissions is not an object" % path))
            continue
        for kind in ("allow", "ask", "deny"):
            for rule in perms.get(kind) if isinstance(perms.get(kind), list) else []:
                if isinstance(rule, str) and rule not in rules.setdefault(kind, []):
                    rules[kind].append(rule)
        if name == "autonomous" and isinstance(perms.get("defaultMode"), str):
            state["autonomous_default_mode"] = perms["defaultMode"]
    state["baseline_rules"] = rules


# --------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------

def _refs(repo, home):
    """The checkout path spellings: as given, resolved, and each with `~` for HOME."""
    spellings = [repo, os.path.realpath(repo)]
    if home:
        for path in list(spellings):
            if path == home or path.startswith(home.rstrip("/") + "/"):
                spellings.append("~" + path[len(home.rstrip("/")):])
    return list(dict.fromkeys(spellings))


def _token_pattern(names):
    return [(name, re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])")) for name in names]


def _needle(text, refs, tokens):
    """The first checkout spelling or managed link name found in `text`, or None."""
    for ref in refs:
        if ref in text:
            return ref
    for name, pattern in tokens:
        if pattern.search(text):
            return name
    return None


def _expand_tilde(path, home):
    if path == "~":
        return home
    if path.startswith("~/"):
        return os.path.join(home, path[2:])
    return path


def _orphan_blockers(state, blockers):
    """Toolkit plugins whose marketplace at this checkout is not listed: the CLI cannot be
    driven to remove them through the marketplace, so the person must."""
    for entry in state.get("plugins") or []:
        key = entry.get("id") if isinstance(entry, dict) else None
        if isinstance(key, str) and key.endswith("@" + MARKETPLACE):
            scope = entry.get("scope")
            blockers.append(Blocker("plugin", "%s is installed but its marketplace is gone" % key,
                                    "run `claude plugin uninstall %s --scope %s`, then re-run" % (key, scope)))


def _plugin_actions(state, repo, actions, kept, blockers):
    markets = [m for m in state.get("marketplaces") or []
               if isinstance(m, dict) and m.get("name") == MARKETPLACE]
    settings = state.get("settings") or {}
    if not markets:
        for key in sorted(k for k in (settings.get("enabledPlugins") or {}) if k.endswith("@" + MARKETPLACE)):
            actions.append(Action("settings", "remove-enabled", key, {}))
        known = settings.get("extraKnownMarketplaces")
        entry = known.get(MARKETPLACE) if isinstance(known, dict) else None
        source = entry.get("source") if isinstance(entry, dict) else None
        path = source.get("path") if isinstance(source, dict) else None
        if isinstance(path, str) and _inside(repo, path):
            actions.append(Action("settings", "remove-known-marketplace", MARKETPLACE, {}))
        _orphan_blockers(state, blockers)
        return
    market = markets[0]
    path = market.get("path")
    if not (market.get("source") == "directory" and isinstance(path, str) and _inside(repo, path)):
        kept.append(Kept("marketplace", "%s marketplace (%s %s)" % (MARKETPLACE, market.get("source"), path),
                         "not a directory marketplace inside this checkout, so not ours to remove"))
        # Its plugins belong to that other checkout, not this one: listed, never blocked.
        return
    installs = [(e["id"], e.get("scope")) for e in state.get("plugins") or []
                if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"].endswith("@" + MARKETPLACE)]
    found = len(blockers)
    for key, scope in installs:
        if scope != "user":
            blockers.append(Blocker("plugin", "%s is installed at %s scope" % (key, scope),
                                    "claude plugin uninstall %s --scope %s, then re-run" % (key, scope)))
    removal = sorted({key for key, scope in installs if scope == "user"})
    names = {key.partition("@")[0]: key for key in removal}
    for key, deps in sorted((state.get("retained_plugin_dependencies") or {}).items()):
        hit = [d for d in deps if d in names or d in removal]
        if hit:
            blockers.append(Blocker("plugin", "%s depends on %s" % (key, ", ".join(hit)),
                                    "uninstall %s first (or keep the toolkit), then re-run" % key))
    if len(blockers) > found:
        return
    deps_of = {name: [d for d in (state.get("plugin_dependencies") or {}).get(name, []) if d in names]
               for name in names}
    remaining = set(names)
    while remaining:
        # Dependents first: emit a plugin only once nothing left depends on it.
        ready = sorted(n for n in remaining if not any(n in deps_of[m] for m in remaining if m != n))
        name = (ready or sorted(remaining))[0]
        remaining.discard(name)
        actions.append(Action("plugin", "uninstall", names[name], {}))
    actions.append(Action("marketplace", "remove", MARKETPLACE, {}))


def _permission_actions(state, actions, kept):
    settings = state.get("settings") or {}
    record = state.get("record")
    if state.get("record_unreadable"):
        kept.append(Kept("permissions", "permissions, defaultMode and claudeMdExcludes",
                         "install record unreadable: ownership unknown"))
    if not record:
        if not state.get("record_unreadable"):
            kept.append(Kept("permissions", "permissions, defaultMode and claudeMdExcludes",
                             "no install record: ownership unknown"))
    else:
        perms = settings.get("permissions") if isinstance(settings.get("permissions"), dict) else {}
        for key in ("allow", "ask", "deny"):
            present = perms.get(key) if isinstance(perms.get(key), list) else []
            for rule in record["permissions_added"].get(key, []):
                if rule in present:
                    actions.append(Action("settings", "remove-rule", key, {"rule": rule}))
        mode = record.get("default_mode")
        if mode and perms.get("defaultMode") == mode["set"]:
            actions.append(Action("settings", "restore-mode", "defaultMode", {"previous": mode["previous"]}))
    if record:
        perms = settings.get("permissions") if isinstance(settings.get("permissions"), dict) else {}
        reason = ("toolkit rule, not recorded as added by this install "
                  "(installed before the record existed?)")
        baseline = state.get("baseline_rules") or {}
        for key in ("allow", "ask", "deny"):
            present = perms.get(key) if isinstance(perms.get(key), list) else []
            recorded = record["permissions_added"].get(key, [])
            for rule in baseline.get(key, []):
                if rule in present and rule not in recorded:
                    kept.append(Kept("permissions", rule, reason))
        wanted = state.get("autonomous_default_mode")
        if wanted is not None and perms.get("defaultMode") == wanted and not record.get("default_mode"):
            kept.append(Kept("permissions", "defaultMode", reason))
    excluded = (record or {}).get("claude_md_excludes_added")
    current = settings.get("claudeMdExcludes")
    if isinstance(excluded, dict) and isinstance(current, list) and excluded["pattern"] in current:
        actions.append(Action("settings", "remove-exclude", "claudeMdExcludes",
                              {"pattern": excluded["pattern"], "created_key": excluded["created_key"]}))
    elif current:
        kept.append(Kept("claudeMdExcludes", "claudeMdExcludes", "the install record does not name its pattern"))
    if workflows_setting.owned_and_true(record, settings):
        actions.append(Action("settings", "remove-key", workflows_setting.KEY, {"expected": True}))
    elif workflows_setting.owned(record) and workflows_setting.KEY in settings:
        kept.append(Kept(workflows_setting.KEY, workflows_setting.KEY, "changed since the toolkit set it"))


def _value_actions(state, actions, kept):
    settings = state.get("settings") or {}
    for key, wanted in (("statusLine", state.get("wanted_status_line")),
                        ("autoMemoryDirectory", state.get("auto_memory_target"))):
        if key not in settings:
            continue
        if wanted is not None and settings[key] == wanted:
            actions.append(Action("settings", "remove-key", key, {"expected": copy.deepcopy(settings[key])}))
        else:
            kept.append(Kept(key, key, "differs from what the toolkit installs"))


def _hook_actions(state, actions):
    legacy = state.get("legacy_commands") or set()
    hooks = (state.get("settings") or {}).get("hooks")
    found = []
    if isinstance(hooks, dict):
        for event in sorted(hooks):
            for block in hooks[event] if isinstance(hooks[event], list) else []:
                for handler in block.get("hooks", []) if isinstance(block, dict) else []:
                    command = handler.get("command") if isinstance(handler, dict) else None
                    if command in legacy and (event, command) not in found:
                        found.append((event, command))
    for event, command in found:
        actions.append(Action("settings", "remove-hook", event, {"command": command}))


def _bin_actions(state, repo, actions, blockers):
    rows = set(tuple(row) for row in state.get("managed_links") or [])
    for path, is_link, value in state.get("local_bin") or []:
        if not is_link:
            continue
        if (value, path) in rows:
            actions.append(Action("link", "unlink", path, {"expected_target": value}))
        elif _inside(repo, _link_target(path, value)):
            blockers.append(Blocker("link", "%s points into the toolkit but was not installed by it" % path,
                                    "remove or repoint it, then re-run"))
    for path, is_link, value in state.get("home_bin") or []:
        if not is_link:
            continue
        # install.sh's own legacy rule (its `found_stale` loop over ~/bin).
        if value.startswith(repo + "/") and os.path.basename(value) == os.path.basename(path):
            actions.append(Action("link", "unlink", path, {"expected_target": value}))
        elif _inside(repo, _link_target(path, value)):
            blockers.append(Blocker("link", "%s points into the toolkit but was not installed by it" % path,
                                    "remove or repoint it, then re-run"))
    for path, value, owned in state.get("legacy_links") or []:
        if owned:
            actions.append(Action("link", "unlink", path, {"expected_target": value}))


def _policy_actions(state, actions, blockers):
    ours = [n for n, real, only in state.get("claude_md_imports") or []
            if real == state.get("policy_real") and only]
    if not ours:
        return
    path = state.get("claude_md_path")
    if state.get("claude_md_is_symlink"):
        blockers.append(Blocker("policy", "%s is a symlink and imports the toolkit policy" % path,
                                "remove the import line yourself, then re-run"))
        return
    actions.append(Action("policy", "remove-import", path, {}))


def _is_toolkit_hook(entry):
    """scripts/setup-git.sh's own predicate: the links it makes, and the ones it recognizes as its own."""
    path, is_link, value = entry
    if not is_link or value is None:
        return False
    base = os.path.basename(value)
    return base == "git-hook-forward" or (os.path.basename(path) == "pre-commit" and base == "git-pre-commit")


def _git_actions(state, home, actions, blockers):
    hooks_path = state.get("hooks_path")
    hooks_dir = state.get("hooks_dir")
    if hooks_path is None or hooks_dir is None:
        return
    if os.path.normpath(_expand_tilde(hooks_path, home)) != os.path.normpath(hooks_dir):
        return
    origin = state.get("hooks_path_origin")
    if origin != state.get("global_config_origin"):
        blockers.append(Blocker("git", "core.hooksPath is set in %s" % origin,
                                "set in %s; remove it there, then re-run" % origin))
        return
    entries = state.get("hooks_dir_entries") or []
    foreign = [e for e in entries if not _is_toolkit_hook(e)]
    if foreign:
        for path, _, _ in foreign:
            blockers.append(Blocker("git", "%s in %s is not a toolkit hook" % (os.path.basename(path), hooks_dir),
                                    "move or delete it, then re-run"))
        return
    for path, _, value in entries:
        actions.append(Action("git", "unlink-hook", path, {"expected_target": value}))
    actions.append(Action("git", "unset-hookspath", "core.hooksPath", {}))
    if state.get("hooks_dir_exists", True):
        actions.append(Action("git", "rmdir", hooks_dir, {}))


def _independence_scan(state, repo, home, actions, blockers):
    refs = _refs(repo, home)
    names = sorted({os.path.basename(row[1]) for row in state.get("managed_links") or []})
    tokens = _token_pattern(names)
    after = apply_settings_actions(state.get("settings") or {}, actions)
    for key in ("permissions", "enabledPlugins", "extraKnownMarketplaces"):
        after.pop(key, None)
    for key in sorted(after):
        found = _needle(json.dumps(after[key], ensure_ascii=False), refs, tokens)
        if found:
            blockers.append(Blocker("settings", "settings.%s still references the toolkit (%s)" % (key, found),
                                    "remove or repoint it, then re-run"))
    text = state.get("claude_md")
    if text is not None:
        drop = {n - 1 for n, real, only in state.get("claude_md_imports") or []
                if real == state.get("policy_real") and only
                } if any(a.surface == "policy" for a in actions) else set()
        remaining = "\n".join(l for i, l in enumerate(text.split("\n")) if i not in drop)
        found = _needle(remaining, refs, [])
        if found:
            blockers.append(Blocker("claude-md", "%s still references the toolkit (%s)"
                                    % (state.get("claude_md_path"), found),
                                    "remove the reference, then re-run"))
    git_global = state.get("git_global") or []
    skip = None
    if any(a.kind == "unset-hookspath" for a in actions):
        indexes = [i for i, (_, line) in enumerate(git_global) if line.lower().startswith("core.hookspath=")]
        skip = indexes[-1] if indexes else None
    for index, (origin, line) in enumerate(git_global):
        if index == skip:
            continue
        found = _needle(line, refs, tokens)
        if found:
            blockers.append(Blocker("git", "global git config in %s still references the toolkit (%s)"
                                    % (origin, found), "remove it from %s, then re-run" % origin))


def plan(state, repo, home):
    """(actions, kept, blockers) for `state`. Pure: no filesystem, process or environment reads."""
    actions, kept, blockers = [], [], []
    for surface, message in state.get("errors") or []:
        blockers.append(Blocker(surface, message, "fix this, then re-run"))
    _plugin_actions(state, repo, actions, kept, blockers)
    _permission_actions(state, actions, kept)
    _value_actions(state, actions, kept)
    _hook_actions(state, actions)
    _bin_actions(state, repo, actions, blockers)
    _policy_actions(state, actions, blockers)
    _git_actions(state, home, actions, blockers)
    _independence_scan(state, repo, home, actions, blockers)
    return actions, kept, blockers


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def _cli(run_cli, argv):
    """(ok, text) for a `claude` call; an exception becomes a failure."""
    try:
        ok, out = run_cli(argv)
    except Exception as exc:
        return False, str(exc) or type(exc).__name__
    return bool(ok), (out or "").strip()


def _unlink_expected(action):
    try:
        current = os.readlink(action.target)
    except OSError as exc:
        raise OSError("%s: cannot read the link (%s)" % (action.target, exc))
    if current != action.data["expected_target"]:
        raise OSError("%s now points at %s, not %s; leaving it" % (
            action.target, current, action.data["expected_target"]))
    os.unlink(action.target)


def apply(actions, repo, home, settings_path, run_cli, run_git):
    """Perform `actions`; stop at the first failure. Returns (ok, messages).

    Order: plugin uninstalls, a re-list proving they happened, the marketplace,
    ONE settings write (via `apply_settings_actions`), then links, policy and git.
    """
    messages = []

    def fail(text):
        messages.append("FAILED: " + text)
        return False, messages

    plugins = [a for a in actions if a.surface == "plugin"]
    for action in plugins:
        ok, out = _cli(run_cli, ["plugin", "uninstall", action.target, "--scope", "user"])
        if not ok:
            return fail("claude plugin uninstall %s --scope user: %s" % (action.target, out))
        messages.append("uninstalled plugin %s" % action.target)
    if plugins:
        # A CLI that exits 0 and does nothing must not let the marketplace go.
        ok, out = _cli(run_cli, ["plugin", "list", "--json"])
        try:
            listed = json.loads(out) if ok else None
        except ValueError:
            listed = None
        if not isinstance(listed, list):
            return fail("could not re-list plugins to confirm the uninstalls: %s" % out)
        planned = {a.target for a in plugins}
        left = sorted(e["id"] for e in listed if isinstance(e, dict) and e.get("id") in planned
                      and e.get("scope") == "user")
        if left:
            return fail("claude reported success but still lists %s at user scope" % ", ".join(left))
    for action in actions:
        if action.surface == "marketplace":
            ok, out = _cli(run_cli, ["plugin", "marketplace", "remove", action.target])
            if not ok:
                return fail("claude plugin marketplace remove %s: %s" % (action.target, out))
            messages.append("removed marketplace %s" % action.target)
    settings_actions = [a for a in actions if a.surface == "settings"]
    if settings_actions:
        try:
            write_settings(settings_path, settings_actions)
        except (OSError, ValueError) as exc:
            return fail("settings.json: %s" % exc)
        messages.append("updated %s" % settings_path)
    for action in actions:
        if action.surface not in ("link", "policy", "git"):
            continue
        try:
            if action.kind in ("unlink", "unlink-hook"):
                _unlink_expected(action)
            elif action.kind == "remove-import":
                state, path, detail = team_policy.remove(repo, home)
                if state != "removed":
                    return fail("toolkit policy import: %s (%s)" % (state, detail))
            elif action.kind == "unset-hookspath":
                code, out, err = run_git(["config", "--global", "--unset", "core.hooksPath"])
                if code != 0:
                    return fail("git config --global --unset core.hooksPath exited %s: %s"
                                % (code, (err or out or "").strip()))
            elif action.kind == "rmdir":
                os.rmdir(action.target)
            else:
                return fail("unknown action %s %s" % (action.surface, action.kind))
        except OSError as exc:
            return fail(str(exc))
        messages.append("%s %s" % (action.kind, action.target))
    return True, messages


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _real_run_cli(argv):
    try:
        done = subprocess.run(["claude"] + list(argv), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding="utf-8", errors="replace", timeout=180)
    except FileNotFoundError:
        return False, "claude CLI not found"
    except subprocess.TimeoutExpired:
        return False, "claude %s timed out" % " ".join(argv)
    return done.returncode == 0, done.stdout if done.returncode == 0 else (done.stderr or done.stdout)


def _real_run_git(argv):
    try:
        done = subprocess.run(["git"] + list(argv), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              encoding="utf-8", errors="replace", timeout=60)
    except FileNotFoundError:
        return 127, "", "git not found"
    except subprocess.TimeoutExpired:
        return 124, "", "git %s timed out" % " ".join(argv)
    return done.returncode, done.stdout, done.stderr


def _manifest_rows(repo):
    """[(target, linkname)] from `install.sh --manifest`; raises on any failure."""
    done = subprocess.run(["bash", os.path.join(repo, "install.sh"), "--manifest"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          encoding="utf-8", errors="replace", timeout=120)
    if done.returncode != 0:
        raise RuntimeError("install.sh --manifest exited %s: %s" % (done.returncode, done.stderr.strip()))
    rows = []
    for line in done.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0] == "link":
            rows.append((parts[1], parts[2]))
    return rows


def _describe(action):
    if action.surface == "settings":
        extra = action.data.get("rule") or action.data.get("pattern") or action.data.get("command") or ""
        return "settings.json: %s %s %s" % (action.kind, action.target, extra)
    return "%s: %s %s" % (action.surface, action.kind, action.target)


def _print_plan(actions, kept, blockers, repo, home):
    print("Will remove")
    for action in actions:
        print("  - " + _describe(action).rstrip())
    if not actions:
        print("  (nothing)")
    print("Left in place")
    for item in kept:
        print("  - %s: %s" % (item.detail, item.reason))
    if not kept:
        print("  (nothing)")
    print("Blocked")
    for item in blockers:
        print("  - %s [%s]: %s" % (item.detail, item.surface, item.remediation))
    if not blockers:
        print("  (nothing)")
    print("Always kept")
    print("  - the checkout: %s" % repo)
    print("  - your toolkit config: %s" % os.path.join(home, ".config", "claude-code-toolkit", "config.json"))
    print("  - settings snapshots: %s" % os.path.join(home, ".claude", "settings.json.pre-claude-code-toolkit.bak"))
    print("    and %s" % os.path.join(home, ".claude", "settings.json.claude-code-toolkit-*.bak"))
    print("  - your global git settings other than core.hooksPath")


def _still_named(settings):
    enabled = settings.get("enabledPlugins")
    known = settings.get("extraKnownMarketplaces")
    found = []
    if isinstance(enabled, dict):
        found += ["enabledPlugins.%s" % k for k in sorted(enabled) if k.endswith("@" + MARKETPLACE)]
    if isinstance(known, dict) and MARKETPLACE in known:
        found.append("extraKnownMarketplaces.%s" % MARKETPLACE)
    return found


def main(argv):
    args = list(argv)
    yes = "--yes" in args
    preflight = "--preflight" in args
    rest = [a for a in args if a not in ("--yes", "--preflight")]
    if len(rest) != 1 or len(args) - len(rest) > 1:
        sys.stderr.write("usage: uninstall.py <repo> [--yes | --preflight]\n")
        return 2
    repo = rest[0]
    home = os.path.expanduser("~")
    settings_path = os.path.join(home, ".claude", "settings.json")
    try:
        rows = _manifest_rows(repo)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print("Cannot read the installer's link list, so nothing was changed: %s" % exc)
        return 1

    def read():
        state = gather(repo, home, settings_path, _real_run_cli, _real_run_git, rows)
        return (state,) + plan(state, repo, home)

    state, actions, kept, blockers = read()
    if preflight:  # silent: install.sh snapshots only when this exits 0
        return 1 if blockers else 0
    _print_plan(actions, kept, blockers, repo, home)
    if not yes:
        print("Dry run: nothing changed. Re-run with --uninstall --yes to apply.")
        return 1 if blockers else 0
    if blockers:
        print("Refusing: fix the blocked items above, then re-run.")
        return 1
    ok, messages = apply(actions, repo, home, settings_path, _real_run_cli, _real_run_git)
    for message in messages:
        print("  " + message)
    incomplete = "Uninstall incomplete; the install record was kept. Re-run to finish."
    if not ok:
        print(incomplete)
        return 1
    state, actions, kept, blockers = read()
    left = ["still to do: " + _describe(a) for a in actions]
    left += ["still blocked: %s (%s)" % (b.detail, b.surface) for b in blockers]
    left += ["settings.json still names %s" % name for name in _still_named(state["settings"])]
    if left:
        for line in left:
            print("  " + line)
        print(incomplete)
        return 1
    install_record.remove(install_record.record_path(home))
    print("Uninstalled. Restart Claude Code; running sessions keep the old plugins until "
          "/reload-plugins or a new session.")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
