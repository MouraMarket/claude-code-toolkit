"""Facts about what is on this machine, versus what the toolkit manages.

`claude-setup-audit` exists so onboarding (and anyone else) can ask "what is
already here, and does it match the repo?" without re-deriving the installer's
own list of managed destinations by hand -- the hazard `install.sh`'s own
`EXECUTABLE_NAMES` shows, which is trusted only because a test checks it
against the `link` calls. `load_manifest` runs the real
`install.sh --manifest` so this can never drift from what `install.sh`
actually does.

**This reports facts. It never says whether a difference matters** -- the same
design choice `install.sh`'s own hooks-merge report makes. `audit`/`--json`
is read-only; `snapshot` only ever appends; `apply-safe` only ever removes a file
byte-identical to its repo counterpart, after a snapshot.

**Malformed input never raises and never hides.** A settings file, or a
section inside one, that is the wrong JSON type is never trusted and never
silently dropped: it earns an `UnreadableRow` naming `<file>#<key path>` and
the section is treated as absent for every purpose downstream. `_check_section`
is the one place that rule lives; every settings-shaped read in this module
goes through it (the `~/.claude.json` reads reuse it too).

The only two `AuditError` cases are the manifest itself (`load_manifest`) and
the repo's own hook template (`_load_template`, consumed by `status_line` and
by `classify_hooks`) -- both are inputs this tool cannot proceed
without, unlike a person's own settings file, which it must always be able to
report on, however broken.
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
REPO = os.path.realpath(os.path.join(HERE, "..", ".."))

# `lib/` for `toolkit_config`; the other two for `classify_hooks` /
# `risky_rows`, which reuse `toolkit_integrity_lib.unrunnable_hooks` and
# `secret_lib.scan_text` rather than re-implementing either.
for _path in (os.path.join(REPO, "lib"),
              os.path.join(REPO, "tools", "secret-scan"),
              os.path.join(REPO, "tools", "toolkit-integrity")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# `classify_hooks` reuses `unrunnable_hooks` rather than re-implementing its
# path checks; `risky_rows` reuses `scan_text` rather than re-implementing
# secret detection.
import hook_manifest  # noqa: E402
import plugin_paths  # noqa: E402
import toolkit_integrity_lib  # noqa: E402
import secret_lib  # noqa: E402

# The three guard escape hatches this repo has (`claude-danger-guard`,
# `claude-worktree-guard`). `risky_rows` flags
# any of these set to "1", in a settings `env` block or in the process
# environment; `fixtures.env_for` also strips them so a test never inherits
# one ambiently.
GUARD_ESCAPE_HATCHES = ("DANGER_ALLOW", "WORKTREE_ALLOW", "WT_ALLOW_MAIN")


class AuditError(Exception):
    """The report cannot be trusted at all. The CLI maps this to exit 2."""


# --------------------------------------------------------------- manifest ---

def load_manifest(repo, home):
    """Every destination `install.sh` manages, as `install.sh` itself reports
    it -- never re-derived by hand.

    Runs the real `install.sh --manifest` with `HOME` pointed at `home`: the
    manifest's destinations are built from `$HOME`, so auditing one home with
    another's manifest would mis-classify every row. Raises `AuditError` on a
    non-zero exit, a timeout, or zero `link` rows -- any of those means the
    report that follows cannot be trusted.
    """
    env = dict(os.environ)
    env["HOME"] = home
    install_sh = os.path.join(repo, "install.sh")
    try:
        proc = subprocess.run(
            ["bash", install_sh, "--manifest"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            env=env, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise AuditError("install.sh --manifest timed out: %s" % exc)
    if proc.returncode != 0:
        raise AuditError(
            "install.sh --manifest exited %d: %s"
            % (proc.returncode, (proc.stderr or proc.stdout).strip()))

    rows = []
    link_count = 0
    for line in proc.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3:
            continue
        kind, target, dest = fields
        if kind != "link":
            continue
        link_count += 1
        rows.append({"kind": kind, "target": target, "dest": dest})
    if link_count == 0:
        raise AuditError(
            "install.sh --manifest produced no link rows -- cannot trust "
            "this report")
    return rows


# -------------------------------------------------------------- worktree ---

_WORKTREE_HELPER_MISSING = 66  # a sentinel exit code, not one the helper itself ever returns


def is_linked_worktree(repo):
    """True when `repo` is a linked git worktree rather than a primary
    checkout -- delegates to `repo`'s OWN `lib/worktree_detect.sh`, whose
    detection is git-based, never a path pattern, and is never
    reimplemented. That helper's `is_linked_worktree` returns 0 for "is a
    linked worktree, refuse" and 1 for "proceed"; chaining the `source` with
    `&&` would make a MISSING helper (also exit 1, via bash's own "No such
    file or directory") indistinguishable from a healthy "not a worktree"
    answer, so the `source` step carries its own sentinel exit code instead.

    Raises `AuditError` when the helper is missing/unreadable/unsourceable,
    or on a timeout, or on any exit code this convention does not define --
    failing CLOSED, the same posture `install.sh`/`scripts/setup-git.sh` take
    when this same helper cannot be read (both refuse before their first
    write rather than proceed unguarded).
    """
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE"):
        env.pop(key, None)
    script = ('. "$1/lib/worktree_detect.sh" || exit %d; is_linked_worktree "$1"'
             % _WORKTREE_HELPER_MISSING)
    try:
        proc = subprocess.run(
            ["bash", "-c", script, "_", repo],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            env=env, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise AuditError("is_linked_worktree timed out for %s: %s" % (repo, exc))
    if proc.returncode == _WORKTREE_HELPER_MISSING:
        raise AuditError(
            "%s/lib/worktree_detect.sh is missing or could not be sourced: %s"
            % (repo, (proc.stderr or proc.stdout).strip()))
    if proc.returncode not in (0, 1):
        raise AuditError(
            "is_linked_worktree gave an unexpected exit code (%d) for %s: %s"
            % (proc.returncode, repo, (proc.stderr or proc.stdout).strip()))
    return proc.returncode == 0


# ------------------------------------------------------------------ links ---

def _collect_tree(root):
    """Every entry under `root`, relative to it: `{rel: ("file", bytes) |
    ("dir", None) | ("symlink", readlink_string) | ("unreadable", None)}`, plus
    a list of `{"path", "error"}` for any file an `OSError` stopped this from
    reading (e.g. permission denied). `followlinks=False` so a symlinked
    subdirectory is recorded as a symlink, never walked into.

    An unreadable file never crashes the walk -- it is marked so the tree can
    never spuriously compare `==` to a tree that read it fine, and the problem
    is reported alongside so the caller can surface it as an `UnreadableRow`.
    A directory the walk cannot enter is a problem too, never silently empty."""
    tree = {}
    problems = []

    def _walk_error(exc):
        problems.append({"path": exc.filename or root, "error": str(exc)})

    for base, dirs, files in os.walk(root, followlinks=False,
                                     onerror=_walk_error):
        for name in dirs + files:
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                tree[rel] = ("symlink", os.readlink(full))
            elif os.path.isdir(full):
                tree[rel] = ("dir", None)
            else:
                try:
                    with open(full, "rb") as handle:
                        tree[rel] = ("file", handle.read())
                except OSError as exc:
                    tree[rel] = ("unreadable", None)
                    problems.append({"path": full, "error": str(exc)})
    return tree, problems


def _same_content(dest, source):
    """`(True, [])` when `dest` and `source` are byte-identical -- a directory
    compares the same set of relative paths, byte-equal files, and equal
    `readlink` strings for any symlinks inside; a file compares its bytes.
    The second element is a list of `{"path","error"}` problems: whenever it
    is non-empty the byte-identity of at least one path could not actually be
    verified, so the first element is always `False` too."""
    if os.path.isdir(source):
        if not os.path.isdir(dest):
            return False, []
        dest_tree, dest_problems = _collect_tree(dest)
        source_tree, source_problems = _collect_tree(source)
        problems = dest_problems + source_problems
        if problems:
            return False, problems
        return dest_tree == source_tree, []
    if os.path.isdir(dest):
        return False, []
    try:
        with open(dest, "rb") as a, open(source, "rb") as b:
            return a.read() == b.read(), []
    except OSError as exc:
        return False, [{"path": dest, "error": str(exc)}]


def classify_link(row, repo):
    """One manifest row's real-world state (see the `LinkRow` schema).

    An unreadable file encountered while comparing a directory (or the file
    itself) never raises: the row comes back `differs`, with `detail` naming
    the unreadable path, and the returned dict carries a private `_unreadable`
    key -- a list of `{"path","error"}` problems for `build_report` to fold
    into the report's own `unreadable` list and then strip, since it is not
    part of the public `LinkRow` schema. This keeps `classify_link`'s own
    signature exactly `(row, repo)`.
    """
    dest = row["dest"]
    kind = row["kind"]
    target = row.get("target")
    source = target

    out = {"dest": dest, "target": target, "kind": kind, "state": None,
           "points_to": None, "will_repoint": False, "detail": None}

    if not os.path.lexists(dest):
        out["state"] = "absent"
        if not os.path.exists(source):
            out["detail"] = "no repo source"
        return out

    if os.path.islink(dest):
        out["points_to"] = os.readlink(dest)
        source_real = os.path.realpath(source) if os.path.exists(source) else None
        if source_real is not None and os.path.realpath(dest) == source_real:
            out["state"] = "linked"
            return out
        if not os.path.exists(dest):
            out["state"] = "dangling"
            return out
        out["state"] = "linked_elsewhere"
        out["will_repoint"] = (kind == "link")
        return out

    # A real file or directory at `dest`.
    if not os.path.exists(source):
        out["state"] = "differs"
        out["detail"] = "no repo source"
        return out

    same, problems = _same_content(dest, source)
    out["state"] = "identical" if same else "differs"
    if problems:
        first = problems[0]
        out["detail"] = "unreadable: %s (%s)" % (first["path"], first["error"])
        out["_unreadable"] = problems
    return out


# ------------------------------------------------------------- theirs-only --

_PLAIN_AREAS = {
    "commands": (".claude", "commands"),
    "agents": (".claude", "agents"),
    "skills": (".claude", "skills"),
    "build_lens": (".claude", "build-lens"),
}


def _entry_type(path):
    if os.path.islink(path):
        return "symlink"
    if os.path.isdir(path):
        return "dir"
    return "file"


def _list_or_report(directory, unreadable):
    if not os.path.isdir(directory):
        return []
    try:
        return sorted(os.listdir(directory))
    except OSError as exc:
        unreadable.append({"path": directory, "error": str(exc)})
        return []


def theirs_only(home, repo, manifest_dests, unreadable):
    """Every path in a managed directory that the manifest does not name.

    `will_prune` mirrors what `install.sh` removes: in `home_bin`, a symlink
    into the repo whose basename equals its target's basename; in the four
    `~/.claude` areas, which now hold only legacy links, every symlink
    `plugin_paths.owned_legacy_links` calls owned (live or dangling). A
    missing directory contributes nothing; an unlistable one adds an
    `UnreadableRow` instead of a row.
    """
    dests = set(manifest_dests)
    repo_prefix = repo + "/"
    rows = []

    def add(area, full):
        etype = _entry_type(full)
        points_to = os.readlink(full) if etype == "symlink" else None
        rows.append({"path": full, "area": area, "type": etype,
                     "points_to": points_to, "will_prune": False})

    for area, parts in _PLAIN_AREAS.items():
        directory = os.path.join(home, *parts)
        for name in _list_or_report(directory, unreadable):
            full = os.path.join(directory, name)
            if full not in dests:
                add(area, full)

    local_bin = os.path.join(home, ".local", "bin")
    for name in _list_or_report(local_bin, unreadable):
        full = os.path.join(local_bin, name)
        if full in dests:
            continue
        is_repo_link = (os.path.islink(full)
                         and os.readlink(full).startswith(repo_prefix))
        if name.startswith("claude-") or is_repo_link:
            add("local_bin", full)

    home_bin = os.path.join(home, "bin")
    for name in _list_or_report(home_bin, unreadable):
        full = os.path.join(home_bin, name)
        if full in dests:
            continue
        if not os.path.islink(full):
            continue
        if os.readlink(full).startswith(repo_prefix):
            add("home_bin", full)

    owned_legacy = {path for path, owned
                    in plugin_paths.owned_legacy_links(home, repo) if owned}
    for row in rows:
        if row["type"] != "symlink":
            continue
        if row["area"] in _PLAIN_AREAS:
            row["will_prune"] = row["path"] in owned_legacy
            continue
        if not row["points_to"].startswith(repo_prefix):
            continue
        if row["area"] == "home_bin":
            row["will_prune"] = (
                os.path.basename(row["path"]) == os.path.basename(row["points_to"]))
        elif row["area"] == "local_bin":
            row["will_prune"] = not os.path.exists(row["path"])

    return rows


# -------------------------------------------------------- instruction files -

def instruction_files(home, workspace_root, unreadable):
    """Rows for `~/.claude/CLAUDE.md` and, when a workspace root is set,
    `<ws>/CLAUDE.md` and `<ws>/TOOLING.md`. Sizes describe the referent (a
    symlink's target); 0 for anything absent."""
    paths = [os.path.join(home, ".claude", "CLAUDE.md")]
    if workspace_root:
        paths.append(os.path.join(workspace_root, "CLAUDE.md"))
        paths.append(os.path.join(workspace_root, "TOOLING.md"))

    rows = []
    for path in paths:
        present = os.path.lexists(path)
        symlink = os.path.islink(path)
        size = 0
        lines = 0
        if present and os.path.exists(path):
            try:
                with open(path, "rb") as handle:
                    content = handle.read()
                size = len(content)
                lines = content.count(b"\n")
                if content and not content.endswith(b"\n"):
                    lines += 1
            except OSError as exc:
                unreadable.append({"path": path, "error": str(exc)})
        rows.append({"path": path, "present": present, "symlink": symlink,
                     "bytes": size, "lines": lines})
    return rows


# ------------------------------------------------------------ settings i/o -

def _is_object(value):
    return isinstance(value, dict)


def _is_object_of_lists(value):
    return isinstance(value, dict) and all(isinstance(v, list) for v in value.values())


def _is_list_of_strings(value):
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _check_section(container, key, validator, default, file_label, key_path, unreadable):
    """The shared malformed-input helper (every settings-shaped read calls this).

    `container[key]` absent -> `default`, no row -- absence is not malformed.
    Present but failing `validator` -> an `UnreadableRow` naming
    `<file_label>#<key_path>`, and `default` stands in for that section.
    Present and valid -> the value itself, unchanged.
    """
    if not isinstance(container, dict) or key not in container:
        return default
    value = container[key]
    if validator(value):
        return value
    unreadable.append({"path": "%s#%s" % (file_label, key_path),
                        "error": "malformed (wrong type)"})
    return default


def load_settings(path, unreadable):
    """A settings-shaped JSON object, never raising.

    Missing file -> `{}`, no row. Unreadable, invalid JSON, or valid JSON that
    is not an object -> `{}` plus an `UnreadableRow`. A present-but-wrong-typed
    `hooks` / `permissions` / `permissions.allow` / `env` section is likewise
    reported and treated as absent -- everything else in the file passes
    through untouched.
    """
    label = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        unreadable.append({"path": label, "error": str(exc)})
        return {}

    try:
        data = json.loads(text)
    except ValueError as exc:
        unreadable.append({"path": label, "error": "invalid JSON: %s" % exc})
        return {}

    if not isinstance(data, dict):
        unreadable.append({"path": label, "error": "not a JSON object"})
        return {}

    settings = dict(data)
    settings["hooks"] = _check_section(
        data, "hooks", _is_object_of_lists, {}, label, "hooks", unreadable)
    permissions = _check_section(
        data, "permissions", _is_object, {}, label, "permissions", unreadable)
    if permissions:
        permissions = dict(permissions)
        permissions["allow"] = _check_section(
            permissions, "allow", _is_list_of_strings, [], label,
            "permissions.allow", unreadable)
    settings["permissions"] = permissions
    settings["env"] = _check_section(
        data, "env", _is_object, {}, label, "env", unreadable)
    return settings


def _load_template(repo):
    """What the repo ships, in the shape the rest of this module reads:
    `hooks` (the plugin's manifest, not used for classification), `statusLine`
    (from `claude/settings.host.json`) and `legacy` (every command the old
    settings template ever wrote, `__HOME__` unexpanded). A missing or
    unreadable host file or legacy list is one of the only `AuditError`
    cases, since a report cannot proceed without them."""
    host_path = os.path.join(repo, "claude", "settings.host.json")
    try:
        with open(host_path, encoding="utf-8") as handle:
            host = json.load(handle)
    except (OSError, ValueError) as exc:
        raise AuditError("cannot read host settings %s: %s" % (host_path, exc))
    if not isinstance(host, dict):
        raise AuditError("host settings %s is not an object" % host_path)
    try:
        legacy = sorted(hook_manifest.legacy_commands(repo, "__HOME__"))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AuditError("cannot read legacy hook list in %s: %s"
                         % (os.path.join(repo, hook_manifest.LEGACY_COMMANDS), exc))
    try:
        hooks = hook_manifest.load(repo)
    except hook_manifest.ManifestError as exc:
        raise AuditError("cannot read plugin hook manifest: %s" % exc)
    return {"hooks": hooks, "statusLine": host.get("statusLine"),
            "legacy": legacy}


def status_line(settings, repo, home):
    """"absent" (nobody has one), "ours" (matches the repo's template after
    `__HOME__` -> `home`), or "theirs" (present and different)."""
    template = _load_template(repo)
    wanted = template.get("statusLine")
    if wanted is not None:
        wanted = json.loads(json.dumps(wanted).replace("__HOME__", home))
    current = settings.get("statusLine")
    if current is None:
        return "absent"
    if current == wanted:
        return "ours"
    return "theirs"


def _flatten_event_commands_reporting(event_value, file_label, event, unreadable):
    """Every command one event's matcher blocks declare in the USER's settings:
    a wrong-typed matcher, `hooks` value, or hook entry earns an
    `UnreadableRow` naming the exact key path and is treated as absent,
    rather than silently dropped (a bare string iterates into individual
    characters, none of them dicts) or raising (a bare int is not iterable
    at all) -- "malformed input never raises and never hides" (module docstring).

    A hook dict that lacks a string `command` is NOT malformed by this
    rule -- `install.sh`'s own `event_commands` silently skips it too when
    deciding what to WRITE, but it must never vanish from this report:
    a settings file whose only custom hook is shaped like this (a Stop
    prompt hook is the confirmed real-world case) used to
    read `empty_ground` true, and `install.sh` then dropped it with nothing
    said. Every well-formed hook dict earns exactly one entry: its own
    `command` string when present, else a stable `json.dumps(hook,
    sort_keys=True)` -- `command` stays a `str` either way, and
    the JSON form can only ever equal a legacy command by literal
    coincidence, so a non-command hook can be `ours` only in that
    (unreachable in practice) case; the usual outcome is `theirs_kept`,
    and `unrunnable` is `None` for it (its `command` is
    not a path, so no path check runs)."""
    commands = set()
    if not isinstance(event_value, list):
        return commands
    for i, matcher in enumerate(event_value):
        if not isinstance(matcher, dict):
            unreadable.append({
                "path": "%s#hooks.%s[%d]" % (file_label, event, i),
                "error": "malformed (wrong type)"})
            continue
        hooks_value = matcher.get("hooks")
        if hooks_value is None:
            continue
        if not isinstance(hooks_value, list):
            unreadable.append({
                "path": "%s#hooks.%s[%d].hooks" % (file_label, event, i),
                "error": "malformed (wrong type)"})
            continue
        for j, hook in enumerate(hooks_value):
            if not isinstance(hook, dict):
                unreadable.append({
                    "path": "%s#hooks.%s[%d].hooks[%d]" % (file_label, event, i, j),
                    "error": "malformed (wrong type)"})
                continue
            command = hook.get("command")
            if isinstance(command, str):
                commands.add(command)
            else:
                commands.add(json.dumps(hook, sort_keys=True))
    return commands


def classify_hooks(settings, template, home, unreadable=None):
    """Every hook command the user's settings declare, classified against
    the repo's legacy command list: `ours` (an old registration, after
    `__HOME__` -> `home`, that the installer removes because the plugin
    carries it) or `theirs_kept` (every other command, wherever it sits --
    the installer leaves it alone). `theirs_dropped` is retired and no longer
    produced.

    Raises `AuditError` when the template's own `hooks` key is not an object
    of lists -- one of only two `AuditError` cases in this module, since a
    report cannot classify anything against a template it cannot read.

    `unreadable` collects an `UnreadableRow` for any malformed piece found
    while walking the USER's own hooks (a wrong-typed matcher, `hooks`
    value, or hook entry) -- the repo's own template is exempt, since it is
    already either well-formed or a raised `AuditError`, never a value this
    function accepts and quietly narrows. Defaults to a fresh list so an
    existing 3-argument call keeps working; `build_report` passes its own
    accumulating list so these rows land in the report."""
    if unreadable is None:
        unreadable = []

    hooks_template = template.get("hooks")
    if not isinstance(hooks_template, dict) or not all(
            isinstance(value, list) for value in hooks_template.values()):
        raise AuditError("hook template's \"hooks\" is not an object of lists")

    legacy = {command.replace("__HOME__", home)
              for command in template.get("legacy") or []}

    unrunnable_by_command = {
        record["command"]: record["why"]
        for record in toolkit_integrity_lib.unrunnable_hooks(home, settings)}

    user_hooks = settings.get("hooks")
    rows = []
    if isinstance(user_hooks, dict):
        for event, matchers in user_hooks.items():
            for command in _flatten_event_commands_reporting(
                    matchers, os.path.join(home, ".claude", "settings.json"),
                    event, unreadable):
                state = "ours" if command in legacy else "theirs_kept"
                rows.append({"event": event, "command": command, "state": state,
                            "unrunnable": unrunnable_by_command.get(command)})
    return rows


# --------------------------------------------------------------- ~/.claude.json --

def _check_mcp_servers(container, file_label, key_path, unreadable):
    """`container["mcpServers"]`, sanitised into `{name: {"env": {...},
    "headers": {...}}}`. Malformed at any level -- not an object, an entry
    that isn't one, or an `env`/`headers` that isn't -- earns an
    `UnreadableRow` and that entry (or section) is treated as absent, per
    the module docstring's malformed-input rule. Shared by the top-level
    `mcpServers` and
    each project's own."""
    servers = _check_section(container, "mcpServers", _is_object, {},
                             file_label, key_path, unreadable)
    out = {}
    for name, server in servers.items():
        if not isinstance(server, dict):
            unreadable.append({"path": "%s#%s.%s" % (file_label, key_path, name),
                               "error": "malformed (wrong type)"})
            continue
        entry_path = "%s.%s" % (key_path, name)
        out[name] = {
            "env": _check_section(server, "env", _is_object, {}, file_label,
                                  "%s.env" % entry_path, unreadable),
            "headers": _check_section(server, "headers", _is_object, {}, file_label,
                                      "%s.headers" % entry_path, unreadable),
        }
    return out


def _load_claude_json(home, unreadable):
    """`~/.claude.json`'s `mcpServers` (top-level and per-project), sanitised
    the same way `load_settings` sanitises a settings file. Missing -> `None`
    (skipped entirely -- not even an `UnreadableRow`, since a machine with no
    MCP config at all is not malformed). Unreadable, invalid JSON, or valid
    JSON that is not an object -> an `UnreadableRow`, also `None`."""
    path = os.path.join(home, ".claude.json")
    label = os.path.basename(path)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        unreadable.append({"path": label, "error": str(exc)})
        return None

    try:
        data = json.loads(text)
    except ValueError as exc:
        unreadable.append({"path": label, "error": "invalid JSON: %s" % exc})
        return None
    if not isinstance(data, dict):
        unreadable.append({"path": label, "error": "not a JSON object"})
        return None

    mcp_servers = _check_mcp_servers(data, label, "mcpServers", unreadable)
    projects_out = {}
    projects = _check_section(data, "projects", _is_object, {}, label,
                              "projects", unreadable)
    for proj_path, proj in projects.items():
        if not isinstance(proj, dict):
            unreadable.append({"path": "%s#projects.%s" % (label, proj_path),
                               "error": "expected an object"})
            continue
        key_path = "projects.%s.mcpServers" % proj_path
        projects_out[proj_path] = _check_mcp_servers(
            proj, label, key_path, unreadable)
    return {"mcpServers": mcp_servers, "projects": projects_out}


# ------------------------------------------------------------------- risky --

def _settings_scan_sources(home, workspace_root, settings, unreadable):
    """(path, settings dict) pairs `risky_rows` scans: the home settings
    `build_report` already loaded, plus -- when a workspace root is set --
    the workspace's own `settings.json` and `settings.local.json`, each read
    fresh through `load_settings` so a workspace-only defect earns its own
    `UnreadableRow` rather than being silently skipped."""
    sources = [(os.path.join(home, ".claude", "settings.json"), settings)]
    if workspace_root:
        for name in ("settings.json", "settings.local.json"):
            path = os.path.join(workspace_root, ".claude", name)
            sources.append((path, load_settings(path, unreadable)))
    return sources


def _scan_env_like(mapping, where_prefix, rows):
    """One `plaintext_secret` row per finding in an env-shaped mapping (a
    settings `env` block, or an MCP server's `env`/`headers`). `detail`
    carries only the finding's rule name -- never any part of the value,
    redacted or not."""
    for key, value in (mapping or {}).items():
        for finding in secret_lib.scan_text(str(value)):
            rows.append({"id": "plaintext_secret", "severity": "their_call",
                        "where": "%s.%s" % (where_prefix, key),
                        "detail": finding.rule})


def _scan_mcp_secrets(servers, file_label, key_prefix, rows):
    for name, server in (servers or {}).items():
        for sub_key in ("env", "headers"):
            _scan_env_like(server.get(sub_key, {}),
                           "%s#%s.%s.%s" % (file_label, key_prefix, name, sub_key),
                           rows)


def _foreign_hooks_path(home, environ):
    """`git config --global --get core.hooksPath`, read with `HOME` pointed
    at `home`. `None` when git has nothing to say -- missing, unset, or the
    call itself failing -- rather than an empty string, so the caller's
    truthiness check is the one place that decides.

    A non-empty `GIT_CONFIG_GLOBAL` is kept: `scripts/setup-git.sh`
    inherits it and decides whether secret scanning is active from THAT
    file, so the audit must read the same one or report a clean machine the
    installer calls unprotected. It is taken from `environ` (the caller's
    view of the environment, per `risky_rows`), never from `os.environ`."""
    env = dict(os.environ)
    env["HOME"] = home
    env.pop("GIT_CONFIG_GLOBAL", None)
    global_config = environ.get("GIT_CONFIG_GLOBAL")
    if global_config:
        env["GIT_CONFIG_GLOBAL"] = global_config
    try:
        proc = subprocess.run(
            ["git", "config", "--global", "--get", "core.hooksPath"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def risky_rows(home, workspace_root, settings, hook_rows, unreadable, environ):
    """`stop`/`their_call` rows: disabled hooks, bypassed permissions, a
    guard escape hatch left "1", a blanket Bash allow, a plaintext secret, a
    foreign git hooksPath, and a dead hook. The body below is the
    definition of what each id checks.

    `environ` is the ONLY source of process-environment state this function
    reads (`guard_bypass_env`'s process-environment row) -- never `os.environ`
    directly, so an in-process caller can exercise it hermetically.
    """
    rows = []
    sources = _settings_scan_sources(home, workspace_root, settings, unreadable)

    for path, source in sources:
        if source.get("disableAllHooks") is True:
            rows.append({"id": "hooks_disabled", "severity": "stop",
                        "where": path, "detail": "disableAllHooks is true"})

        permissions = source.get("permissions") or {}
        if permissions.get("defaultMode") == "bypassPermissions":
            rows.append({"id": "bypass_permissions", "severity": "stop",
                        "where": path,
                        "detail": "permissions.defaultMode is \"bypassPermissions\""})

        env = source.get("env") or {}
        for name in GUARD_ESCAPE_HATCHES:
            if name in env and str(env[name]) == "1":
                rows.append({"id": "guard_bypass_env", "severity": "stop",
                            "where": path, "detail": "%s=1" % name})

        for entry in permissions.get("allow") or []:
            if isinstance(entry, str) and entry.strip() in (
                    "Bash", "Bash(*)", "Bash(:*)"):
                rows.append({"id": "blanket_bash_allow", "severity": "their_call",
                            "where": path, "detail": entry.strip()})

        _scan_env_like(env, "%s#env" % path, rows)

    for name in GUARD_ESCAPE_HATCHES:
        if environ.get(name) == "1":
            rows.append({"id": "guard_bypass_env", "severity": "stop",
                        "where": "process environment", "detail": "%s=1" % name})

    claude_json = _load_claude_json(home, unreadable)
    if claude_json is not None:
        _scan_mcp_secrets(claude_json["mcpServers"], ".claude.json",
                         "mcpServers", rows)
        for proj_path, servers in claude_json["projects"].items():
            _scan_mcp_secrets(servers, ".claude.json",
                             "projects.%s.mcpServers" % proj_path, rows)

    foreign = _foreign_hooks_path(home, environ)
    if foreign:
        want = os.path.realpath(
            os.path.join(home, ".config", "claude-code-toolkit", "git-hooks"))
        # A leading ~ is the audited home, as setup-git.sh's
        # `${current_hooks/#\~/$HOME}` reads it; never this process's home.
        if foreign == "~" or foreign.startswith("~" + os.sep):
            expanded = os.path.join(home, foreign[2:])
        else:
            expanded = foreign
        have = os.path.realpath(expanded)
        if have != want:
            rows.append({"id": "foreign_hooks_path", "severity": "their_call",
                        "where": "git config --global core.hooksPath",
                        "detail": foreign})

    for row in hook_rows:
        if row.get("unrunnable"):
            rows.append({"id": "dead_hook", "severity": "their_call",
                        "where": "%s: %s" % (row["event"], row["command"]),
                        "detail": row["unrunnable"]})

    return rows


# ------------------------------------------------------------------ report --

def _workspace_root():
    try:
        import toolkit_config
        return toolkit_config.workspace_root()
    except Exception:
        return None


# A sentinel distinct from `None`: `None` is itself a legitimate
# `workspace_root` value (explicitly "no workspace configured"), so it cannot
# double as "the caller didn't pass one". Only the sentinel means "resolve it
# via `toolkit_config`, as before" -- an explicit `workspace_root=None` bypasses
# `toolkit_config` entirely, which is what makes `build_report` testable
# without ever touching a real config file.
_UNSET = object()


def build_report(home=None, repo=REPO, environ=None, workspace_root=_UNSET):
    """The whole audit, as one JSON-serialisable dict (see the `Report`
    schema). `home` defaults to `$HOME` at CALL time, not at import; `environ`
    defaults to `os.environ`. `workspace_root` defaults to resolving via
    `toolkit_config.workspace_root()`; pass it explicitly (a path, or `None`
    for "no workspace") to test the workspace-root-dependent branches without
    `toolkit_config` in the loop at all. `load_manifest`'s `AuditError`
    propagates -- everything downstream of it degrades to an `UnreadableRow`
    instead."""
    if home is None:
        home = os.path.expanduser("~")
    if environ is None:
        environ = os.environ
    if workspace_root is _UNSET:
        workspace_root = _workspace_root()

    unreadable = []

    manifest = load_manifest(repo, home)
    links = []
    for row in manifest:
        link = classify_link(row, repo)
        extra = link.pop("_unreadable", None)
        if extra:
            unreadable.extend(extra)
        links.append(link)
    manifest_dests = {row["dest"] for row in manifest}

    theirs = theirs_only(home, repo, manifest_dests, unreadable)

    settings_path = os.path.join(home, ".claude", "settings.json")
    settings = load_settings(settings_path, unreadable)

    template = _load_template(repo)
    line = status_line(settings, repo, home)
    hook_rows = classify_hooks(settings, template, home, unreadable)
    risky = risky_rows(home, workspace_root, settings, hook_rows, unreadable, environ)

    instr = instruction_files(home, workspace_root, unreadable)

    empty_ground = (
        all(row["state"] == "absent" for row in links)
        and not theirs and not hook_rows and not risky and not unreadable
        and line == "absent"
        and not any(row["present"] for row in instr))

    return {
        "version": 1,
        "repo": repo,
        "home": home,
        "empty_ground": empty_ground,
        "unreadable": sorted(unreadable, key=lambda r: r["path"]),
        "links": links,
        "theirs_only": sorted(theirs, key=lambda r: r["path"]),
        "hooks": sorted(hook_rows, key=lambda r: (r["event"], r["command"])),
        "risky": sorted(risky, key=lambda r: (r["id"], r["where"])),
        "status_line": line,
        "instruction_files": sorted(instr, key=lambda r: r["path"]),
        "from_worktree": is_linked_worktree(repo),
    }


# ---------------------------------------------------------------------- CLI-

_SUBCOMMANDS = ("audit", "snapshot", "apply-safe")


_WORKTREE_WARNING = (
    "run from a linked worktree: every link compares against it, so "
    "repoint and linked_elsewhere counts are not real drift. Re-run "
    "from the primary checkout.")


def _human_summary(report):
    lines = []
    if report["from_worktree"]:
        lines.append(_WORKTREE_WARNING)
    counts = {}
    for row in report["links"]:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    if counts:
        lines.append("links: " + ", ".join(
            "%d %s" % (n, s) for s, n in sorted(counts.items())))
    lines.append("theirs-only: %d" % len(report["theirs_only"]))

    stop = [r for r in report["risky"] if r["severity"] == "stop"]
    their_call = [r for r in report["risky"] if r["severity"] != "stop"]
    for row in stop + their_call:
        lines.append("risky (%s) %s: %s"
                      % (row["severity"], row["id"], row["detail"]))

    if report["empty_ground"]:
        lines.append("empty ground")
    elif (not report["risky"] and not report["theirs_only"]
          and not report["unreadable"]
          and all(row["state"] in ("linked", "identical")
                  for row in report["links"])):
        lines.append("every managed path linked")
    else:
        lines.append("needs reconciliation")
    return "\n".join(lines[:15])


def main(argv, repo=REPO):
    """Argparse, parsed before anything else; never reads stdin. `audit` is
    the default subcommand when none is named."""
    parser = argparse.ArgumentParser(prog="claude-setup-audit")
    sub = parser.add_subparsers(dest="command")

    audit_parser = sub.add_parser("audit")
    audit_parser.add_argument("--json", action="store_true")
    sub.add_parser("snapshot")
    sub.add_parser("apply-safe")

    args = list(argv)
    if not args or args[0] not in _SUBCOMMANDS:
        args = ["audit"] + args
    parsed = parser.parse_args(args)
    command = parsed.command or "audit"

    if command == "audit":
        try:
            report = build_report(repo=repo)
        except AuditError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if parsed.json:
            print(json.dumps(report, indent=2))
        else:
            print(_human_summary(report))
        return 0

    if command == "snapshot":
        import snapshot_lib
        try:
            path = snapshot_lib.take_snapshot(
                os.path.expanduser("~"), repo, _workspace_root())
        except snapshot_lib.SnapshotError as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except AuditError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(path)
        return 0

    if command == "apply-safe":
        import apply_lib
        try:
            result = apply_lib.apply_safe(
                os.path.expanduser("~"), repo, _workspace_root())
        except apply_lib.ApplyError as exc:
            # A partial run (some files already removed, a snapshot already
            # taken) is not a bare failure -- print what already happened
            # before the error, so a partial apply is never unreported.
            for dest in exc.removed:
                print("removed %s" % dest)
            if exc.snapshot:
                print("snapshot: %s" % exc.snapshot)
            print(str(exc), file=sys.stderr)
            return 3
        except AuditError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        for dest in result["removed"]:
            print("removed %s" % dest)
        print("snapshot: %s" % result["snapshot"])
        return 0

    return 2


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv[1:]))
