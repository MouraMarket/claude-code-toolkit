#!/usr/bin/env python3
"""Toolkit version, update and rollback, behind `./install.sh --version`,
`--update` and `--rollback`.

    python3 lib/toolkit_update.py <version|update|rollback> <repo>

`version` only reads. `update` fast-forwards the checkout to origin's default
branch and `rollback` detaches HEAD at the commit `--update` recorded in the
install record; both record what they did before anything local moves.
`update` exits 0 (moved, run the install), 10 (already current, no install),
11 (no move, but run the install) or 1 (refused, nothing local changed).
`rollback` exits 0 (moved, run the install) or 1 (refused).

`update` first checks that origin is the toolkit's public repository
(`PUBLIC_ORIGIN`), or the one `update.origin` in the toolkit config names for a
fork, and refuses before it contacts origin when it is not: the installer runs
whatever code an update brings.
"""

import copy
import json
import os
import re
import shlex
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, timezone
from urllib.parse import unquote

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import auto_memory  # noqa: E402
import hook_manifest  # noqa: E402
import install_record  # noqa: E402
import plugin_deps  # noqa: E402
import profiles  # noqa: E402
import settings_io  # noqa: E402
import toolkit_config  # noqa: E402
import workflows_setting  # noqa: E402

USAGE = "usage: toolkit_update.py <version|update|rollback> <repo>"

PUBLIC_ORIGIN = "https://github.com/mouramarket/claude-code-toolkit"
# Hosts where one owner/repo path is one repository, whatever the transport.
_GITHUB_HOSTS = ("github.com", "www.github.com", "ssh.github.com")
_DEFAULT_PORTS = {"ssh": "22", "https": "443", "http": "80", "git": "9418"}
_SCHEME_ALIASES = {"git+ssh": "ssh", "ssh+git": "ssh"}
_URL_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*)://(?:[^@/]*@)?"
                     r"(?P<host>\[[^\]]*\]|[^:/]*)(?::(?P<port>[0-9]*))?(?P<path>/.*)?$")
# git's rule: scp-like only when no slash comes before the first colon.
_SCP_RE = re.compile(r"^(?:[^@/:]*@)?(?P<host>[^@/:]+):(?P<path>.*)$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[/\\]")


def _strip_repo_path(path):
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-len(".git")].rstrip("/")
    return path


def _canonical(url, base):
    """A comparable form of a remote URL or path, or None when it names nothing.

    github.com: one owner/repo is one repository over any transport, and GitHub
    names are case-insensitive. Any other host keeps scheme, port (the default
    filled in) and path case: different transports or ports can be different
    services. A local path resolves (relative to `base`, the checkout git runs
    in) and keeps any `.git`, which is part of a directory's name."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    match = _URL_RE.match(url)
    if match and match.group("scheme").lower() == "file":
        return _local(unquote(match.group("path") or ""), base)
    if match:
        scheme = match.group("scheme").lower()
        scheme = _SCHEME_ALIASES.get(scheme, scheme)
        host = match.group("host").lower()
        port = match.group("port") or _DEFAULT_PORTS.get(scheme, "")
        path = _strip_repo_path(match.group("path") or "")
    elif _DRIVE_RE.match(url) or url.startswith(("/", "~", ".")):
        return _local(url, base)
    else:
        scp = _SCP_RE.match(url)
        if not scp:
            return _local(url, base)
        scheme, host, port = "ssh", scp.group("host").lower(), _DEFAULT_PORTS["ssh"]
        path = _strip_repo_path(scp.group("path"))
    if not host or not path:
        return None
    if host in _GITHUB_HOSTS:
        return ("github", path.lower())
    return ("url", scheme, host, port, path)


def _local(path, base):
    if not path:
        return None
    path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return ("local", os.path.realpath(path))


def origin_matches(actual, expected, base="."):
    """True when two remote URLs or paths name the same repository."""
    left, right = _canonical(actual, base), _canonical(expected, base)
    return left is not None and left == right


def configured_origin():
    """(origin, error): `update.origin` from the toolkit config, else PUBLIC_ORIGIN.

    A key that is present but not a non-empty string is an error, never the
    default: a typo must not quietly pin the public URL and refuse a fork's own
    origin without saying why."""
    config, error = toolkit_config.load_status()
    if error:
        return None, "cannot read your toolkit config: %s" % error
    if "update" not in config:
        return PUBLIC_ORIGIN, None
    section = config["update"]
    if not isinstance(section, dict):
        return None, "update.origin in your toolkit config must be a URL or path"
    if "origin" not in section:
        return PUBLIC_ORIGIN, None
    value = section["origin"]
    if not isinstance(value, str) or not value.strip():
        return None, "update.origin in your toolkit config must be a URL or path"
    return value.strip(), None


def origin_urls(repo):
    """Every URL `git fetch origin` would use (insteadOf applied), or [] with no origin."""
    done = _git(repo, "remote", "get-url", "--all", "origin")
    if done.returncode != 0:
        return []
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def _git(repo, *args, timeout=60):
    """`git -C repo ...` with every GIT_* variable stripped; never raises."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        return subprocess.run(["git", "-C", repo] + list(args), capture_output=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL,
                              env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(args, 124, "", str(exc))


def _out(repo, *args):
    """Stripped stdout of a git call, or None when it fails."""
    done = _git(repo, *args)
    return done.stdout.strip() if done.returncode == 0 else None


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(sha):
    return sha[:12]


def _abs_git_dir(repo, value):
    path = value if os.path.isabs(value) else os.path.join(repo, value)
    return os.path.realpath(path)


def remote_default(repo):
    """origin's default branch as origin reports it now, or None (offline,
    no HEAD symref). A fetch never refreshes origin/HEAD, so the cached
    answer goes stale when a remote changes its default branch."""
    done = _git(repo, "ls-remote", "--symref", "origin", "HEAD", timeout=120)
    if done.returncode != 0:
        return None
    for line in done.stdout.splitlines():
        if line.startswith("ref: refs/heads/") and line.endswith("\tHEAD"):
            return line[len("ref: refs/heads/"):-len("\tHEAD")] or None
    return None


def read_state(repo, record_file, default=None):
    """`default` overrides the cached origin/HEAD (update passes origin's live answer)."""
    head = _out(repo, "rev-parse", "HEAD")
    branch = _out(repo, "symbolic-ref", "--quiet", "--short", "HEAD") or None
    if default is None:
        default = _out(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
        default = (default[len("origin/"):] if default and default.startswith("origin/")
                   else (default or "main"))
    status = _out(repo, "--no-optional-locks", "status", "--porcelain", "--untracked-files=no")
    dirty_files = [line for line in (status or "").splitlines() if line.strip()]
    record = install_record.load(record_file)
    point = install_record.rollback(record)
    pin = point if (point is not None and isinstance(point["pinned_at"], str)
                    and branch is None and head == point["commit"]) else None
    behind = _out(repo, "rev-list", "--count", "HEAD..refs/remotes/origin/" + default)
    git_dir = _out(repo, "rev-parse", "--git-dir")
    common_dir = _out(repo, "rev-parse", "--git-common-dir")
    worktree = bool(git_dir and common_dir
                    and _abs_git_dir(repo, git_dir) != _abs_git_dir(repo, common_dir))
    return {"head": head, "branch": branch, "default": default, "dirty": bool(dirty_files),
            "dirty_count": len(dirty_files), "record": record, "pin": pin,
            "behind": int(behind) if behind is not None else None, "worktree": worktree}


def _origin_line(repo, expected):
    urls = origin_urls(repo)
    if not urls:
        return "Origin: none"
    if expected is None:
        expected, error = configured_origin()
        if error:
            return "Origin: %s (cannot check it: %s)" % (urls[0], error)
    stray = [url for url in urls if not origin_matches(url, expected, repo)]
    if not stray:
        return "Origin: %s (matches the pinned origin)" % urls[0]
    return ("Origin: %s (does NOT match %s; ./install.sh --update will refuse)"
            % (stray[0], expected))


def _check_origin(repo, expected):
    """None when every origin URL is the expected one, else the refusal text."""
    if expected is None:
        expected, error = configured_origin()
        if error:
            return error
    urls = origin_urls(repo)
    if not urls:
        return "this checkout has no origin remote"
    for url in urls:
        if not origin_matches(url, expected, repo):
            # A runnable command, quoted for a checkout path with a space.
            setter = "python3 %s %s %s --update --set update.origin=%s" % tuple(
                shlex.quote(part) for part in (
                    os.path.join(repo, "lib", "setup_config.py"),
                    toolkit_config.CONFIG_PATH, repo, url))
            return ('origin is %s, not %s. If you meant to update from %s, run: '
                    '%s ; otherwise run git remote set-url origin %s'
                    % (url, expected, url, setter, expected))
    return None


def version(repo, record_file, expected_origin=None):
    state = read_state(repo, record_file)
    head, default, record = state["head"], state["default"], state["record"]
    info = (_out(repo, "log", "-1", "--format=%cs%x00%s", "HEAD") or "\0").split("\0", 1)
    print("Toolkit version: %s (%s) %s" % (_short(head or ""), info[0], info[1]))
    # The release number, read from VERSION AT THE COMMIT reported above, so a
    # locally edited VERSION file cannot claim a release the checkout is not
    # (row 76e, 28b F-D1e: --version printed only the commit).
    release = _out(repo, "show", "HEAD:VERSION")
    print("Release: %s" % (release or "unknown (no VERSION file at this commit)"))
    if state["branch"] is None:
        if state["pin"] is not None:
            print("State: pinned at %s by ./install.sh --rollback since %s; "
                  "./install.sh --update returns to %s"
                  % (_short(head), state["pin"]["pinned_at"], default))
        else:
            print("State: detached HEAD, not a rollback pin")
    elif state["branch"] == default:
        print("State: on %s" % default)
    else:
        print("State: on branch %s (updates track %s)" % (state["branch"], default))
    print("Local changes: %s" % ("%d tracked file(s) changed" % state["dirty_count"]
                                 if state["dirty"] else "none"))
    commit = record.get("commit") if record else None
    stamp = record.get("installed_at") if record else None
    if isinstance(commit, str) and isinstance(stamp, str):
        if commit == head:
            print("Last install: %s at %s (this commit)" % (_short(commit), stamp))
        else:
            print("Last install: %s at %s; the checkout has moved since, run ./install.sh"
                  % (_short(commit), stamp))
    else:
        print("Last install: none recorded")
    if state["behind"] is None:
        print("Behind origin/%s: unknown (no origin/%s ref)" % (default, default))
    else:
        print("Behind origin/%s: %d commit(s), as of the last fetch" % (default, state["behind"]))
    print(_origin_line(repo, expected_origin))
    point = install_record.rollback(record)
    print("Rollback point: %s" % ("%s recorded %s" % (_short(point["commit"]), point["recorded_at"])
                                  if point else "none"))
    if state["worktree"]:
        print("This is a linked worktree; the installed toolkit is the primary checkout.")
    return 0


def _refuse(prefix, message):
    print("%s%s" % (prefix, message), file=sys.stderr)
    return 1


def _settings_path(home):
    return os.path.join(home, ".claude", "settings.json")


def read_settings(home):
    """(settings, error): the parsed `~/.claude/settings.json`, `({}, None)` when
    there is none, `(None, reason)` when it cannot be read as a JSON object.
    Follows a symlinked file."""
    path = _settings_path(home)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (FileNotFoundError, NotADirectoryError):
        return {}, None
    except ValueError as exc:
        return None, "%s is not valid JSON (%s)" % (path, exc)
    except OSError as exc:
        return None, "cannot read %s (%s)" % (path, exc)
    if not isinstance(data, dict):
        return None, "%s is not a JSON object" % path
    return data, None


_VALUE_KEYS = ("claudeMdExcludes", "statusLine", "autoMemoryDirectory")


def _entry(container, key):
    """{"present": ...} for `key` in a dict (presence by key, so a JSON null is present)."""
    if isinstance(container, dict) and key in container:
        return {"present": True, "value": copy.deepcopy(container[key])}
    return {"present": False}


def _rule_lists(settings):
    perms = settings.get("permissions")
    lists = {}
    for kind in ("allow", "ask", "deny"):
        rules = perms.get(kind) if isinstance(perms, dict) else None
        lists[kind] = [r for r in rules if isinstance(r, str)] if isinstance(rules, list) else []
    return lists


def snapshot_state(record, settings):
    """The rollback entry's `state` (pure): the record's owned fields as they are,
    plus the live settings the installer writes (rule lists in full, each value
    key with its presence)."""
    record = record if isinstance(record, dict) else {}
    added = record.get("permissions_added")
    added = added if isinstance(added, dict) else {}
    state = {"permissions_added": {kind: list(added.get(kind, [])) for kind in ("allow", "ask", "deny")}}
    for key in ("default_mode", "claude_md_excludes_added", "enable_workflows_added"):
        if key in record:
            state[key] = copy.deepcopy(record[key])
    # Snapshot-only (restore_owned never writes it back): the value rollback
    # compares the live setting against.
    state["enable_workflows_base"] = _entry(settings, "enableWorkflows")
    perms = settings.get("permissions")
    live = {"permissions": _rule_lists(settings), "defaultMode": _entry(perms, "defaultMode")}
    for key in _VALUE_KEYS:
        live[key] = _entry(settings, key)
    state["settings"] = live
    return state


def update(repo, record_file, home, expected_origin=None):
    refuse = lambda message: _refuse("Not updated: ", message)  # noqa: E731
    # Before anything talks to origin: an origin that is not the pinned one is
    # never contacted, so its code never reaches the checkout.
    wrong_origin = _check_origin(repo, expected_origin)
    if wrong_origin is not None:
        return refuse(wrong_origin)
    # Ask origin for its default branch rather than trusting the cached
    # origin/HEAD; offline, the cached answer stands and the fetch below
    # refuses on its own.
    state = read_state(repo, record_file, remote_default(repo))
    head, branch, default, pin = state["head"], state["branch"], state["default"], state["pin"]
    if state["dirty"]:
        return refuse("tracked files have local changes; commit or discard them, then re-run")
    if branch is not None and branch != default:
        return refuse("on branch %s; updates track %s. Run `git fetch origin %s` and "
                      "`git checkout %s` first" % (branch, default, default, default))
    if branch is None and pin is None:
        return refuse("HEAD is detached and is not a rollback pin; "
                      "run `git checkout %s` first" % default)
    # Before the fetch, so the already-current returns below cannot bypass it: B's
    # install refuses an unreadable settings file, and an update with no
    # snapshot cannot be rolled back in full.
    settings, settings_error = read_settings(home)
    if settings_error is not None:
        return refuse("%s; fix it, then re-run" % settings_error)
    fetched = _git(repo, "fetch", "--quiet", "origin", default, timeout=120)
    if fetched.returncode != 0:
        lines = fetched.stderr.strip().splitlines()
        return refuse("could not fetch origin/%s: %s" % (default, lines[0] if lines else "unknown error"))
    target = _out(repo, "rev-parse", "refs/remotes/origin/" + default)
    if target is None:
        return refuse("origin/%s does not exist after the fetch" % default)
    # Cache origin's answer, so --version (which never asks origin) agrees. A
    # failed cache write does not stop the update, but it must not pass silently.
    if _git(repo, "remote", "set-head", "origin", default).returncode != 0:
        print("Warning: could not record origin's default branch (%s); --version may "
              "name the old one until `git remote set-head origin %s` succeeds."
              % (default, default), file=sys.stderr)
    # Advice that works on the branch and from a pin: keep the local commits
    # under a new name, then point the default branch at origin's.
    diverged = ("%s has commits origin/%s does not; keep them with `git branch <name> %s`, "
                "then `git checkout -B %s origin/%s` and re-run"
                % (default, default, default, default, default))
    if branch is not None:
        if head == target:
            record = state["record"]
            if record is not None and record.get("commit") == head:
                print("Already up to date at %s." % _short(head))
                return 10
            print("Already at origin/%s; running the install it never had." % default)
            return 11
        if _git(repo, "merge-base", "--is-ancestor", head, target).returncode != 0:
            return refuse(diverged)
    else:
        local = _out(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + default)
        if local is not None and _git(repo, "merge-base", "--is-ancestor",
                                      "refs/heads/" + default, target).returncode != 0:
            return refuse(diverged)
    # Record before anything local moves: a failed write must leave HEAD alone.
    # The earlier point is kept so a move git refuses can put it back.
    previous = install_record.rollback(state["record"])
    previous = dict(previous) if previous is not None else None
    snapshot = snapshot_state(state["record"], settings)
    try:
        if branch is not None:
            install_record.set_rollback(record_file, head, _now(), snapshot)
        else:
            install_record.refresh_state(record_file, snapshot)
    except (OSError, ValueError) as exc:
        return refuse("could not write the install record: %s" % exc)
    if branch is not None:
        moved = _git(repo, "merge", "--ff-only", "--quiet", target)
    else:
        moved = _git(repo, "checkout", "--quiet", "-B", default, target)
    if moved.returncode != 0:
        sys.stderr.write(moved.stderr)
        # A refused merge moved nothing, so the point written above names the
        # commit we are still on; put the earlier one back.
        if branch is not None and _git(repo, "rev-parse", "HEAD").stdout.strip() == head:
            try:
                install_record.restore_rollback(record_file, previous)
            except OSError as exc:
                print("Warning: could not restore the earlier rollback point: %s" % exc,
                      file=sys.stderr)
        return 1
    if branch is None:
        try:
            install_record.clear_pinned(record_file)
        except OSError as exc:
            print("Warning: could not clear the pin in the install record: %s" % exc,
                  file=sys.stderr)
    print("Updating %s -> %s" % (_short(head), _short(target)))
    if _git(repo, "diff", "--quiet", head, target, "--", "scripts/setup-git.sh").returncode == 1:
        print("scripts/setup-git.sh changed: run ./scripts/setup-git.sh once from this folder.")
    return 0


def _note(key, current):
    if current["present"]:
        return "%s changed since the update; left as you set it" % key
    return ("%s was removed since the update; left removed "
            "(the install that follows may add it back)" % key)


def _has_pattern(entry, pattern):
    return entry["present"] and isinstance(entry["value"], list) and pattern in entry["value"]


def restore_plan(record, state, settings, installed):
    """(actions, notes) that put the installer's changes since the snapshot back (pure).

    One three-way rule per item. base = the snapshot's live state, installed =
    what the update's install writes, current = `settings` now. Current equals
    base: nothing. Current equals installed: put base back, presence included.
    Anything else is the user's edit, kept and named in a note. `installed` maps
    "statusLine" and "autoMemoryDirectory" to a {"present", "value"} entry, or
    None when this checkout's value cannot be told (never restored)."""
    record = record if isinstance(record, dict) else {}
    live = state["settings"]
    actions, notes = [], []

    # A rule only ever moves from present to absent: remove what the record now
    # owns, that is live now, and was not live before the update.
    added = record.get("permissions_added")
    added = added if isinstance(added, dict) else {}
    current_rules = _rule_lists(settings)
    for kind in ("allow", "ask", "deny"):
        for rule in added.get(kind, []):
            if rule in current_rules[kind] and rule not in live["permissions"][kind]:
                actions.append(settings_io.Action("settings", "remove-rule", kind, {"rule": rule}))

    mode = record.get("default_mode")
    base = live["defaultMode"]
    current = _entry(settings.get("permissions"), "defaultMode")
    if current != base:
        wanted = {"present": True, "value": mode["set"]} if isinstance(mode, dict) else None
        if wanted is not None and current == wanted:
            actions.append(settings_io.Action("settings", "set-mode", "defaultMode", {
                "expected": current["value"], "present": base["present"], "value": base.get("value")}))
        else:
            notes.append(_note("defaultMode", current))

    excluded = record.get("claude_md_excludes_added")
    if isinstance(excluded, dict):
        pattern = excluded["pattern"]
        base = live["claudeMdExcludes"]
        current = _entry(settings, "claudeMdExcludes")
        in_current, in_base = _has_pattern(current, pattern), _has_pattern(base, pattern)
        if in_current and not in_base:
            actions.append(settings_io.Action("settings", "remove-exclude", "claudeMdExcludes", {
                "pattern": pattern, "created_key": not base["present"]}))
        elif in_base and not in_current:
            notes.append("claudeMdExcludes was removed since the update; left removed "
                         "(the install that follows may add it back)")

    for key in ("statusLine", "autoMemoryDirectory"):
        base = live[key]
        current = _entry(settings, key)
        if current == base:
            continue
        wanted = installed.get(key)
        if wanted is not None and current == wanted:
            if base["present"]:
                actions.append(settings_io.Action("settings", "set-key", key, {
                    "expected_present": current["present"], "expected": current.get("value"),
                    "value": base["value"]}))
            else:
                actions.append(settings_io.Action("settings", "remove-key", key,
                                                  {"expected": current["value"]}))
        else:
            notes.append(_note(key, current))

    # enableWorkflows: the installer wrote `true` only when the record owns it now.
    # Compared type-strictly, so a hand-edited `1` is never taken for the install's `true`.
    def same(a, b):
        if a["present"] != b["present"]:
            return False
        return not a["present"] or (type(a["value"]) is type(b["value"]) and a["value"] == b["value"])

    current = _entry(settings, "enableWorkflows")
    now_ours = workflows_setting.owned(record)
    if "enable_workflows_base" in state:
        base = state["enable_workflows_base"]
        if same(current, base):
            pass
        elif now_ours and current["present"] and current["value"] is True:
            if base["present"]:
                actions.append(settings_io.Action("settings", "set-key", "enableWorkflows", {
                    "expected_present": True, "expected": True, "value": base["value"]}))
            else:
                actions.append(settings_io.Action("settings", "remove-key", "enableWorkflows",
                                                  {"expected": True}))
        else:
            notes.append(_note("enableWorkflows", current))
    elif now_ours and current["present"]:
        # A snapshot from before the toolkit managed this: its value then is unknown.
        notes.append("enableWorkflows: this rollback point predates the toolkit managing it; "
                     "left as is \u2014 remove it yourself if you do not want it")
    return actions, notes


def _installed_values(repo, home):
    """({"statusLine": entry|None, "autoMemoryDirectory": entry|None}, notes): what
    this checkout's install leaves in those keys; None where it cannot be told."""
    notes = []
    try:
        wanted = hook_manifest.wanted_status_line(repo, home)
    except (OSError, ValueError) as exc:
        wanted = None
        notes.append("statusLine: cannot tell what this checkout installs (%s); left as is" % exc)
    status_line = {"present": True, "value": wanted} if wanted is not None else None
    if auto_memory.unify_enabled(toolkit_config):
        target = auto_memory.managed_target(toolkit_config.get("workspace_root"), home)[0]
        memory = {"present": True, "value": target} if target else None
    else:
        memory = {"present": False}
    return {"statusLine": status_line, "autoMemoryDirectory": memory}, notes


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


def _describe(action):
    data = action.data
    if action.kind == "remove-rule":
        return "removing the %s rule %s" % (action.target, data["rule"])
    if action.kind == "set-mode":
        return "restoring permissions.defaultMode"
    if action.kind == "remove-exclude":
        return "removing %s from claudeMdExcludes" % data["pattern"]
    if action.kind == "remove-key":
        return "removing %s" % action.target
    return "restoring %s" % action.target


def _registry_at(repo, commit):
    """(registry, error) for claude/profiles.json at `commit`."""
    shown = _git(repo, "show", "%s:%s" % (commit, "claude/profiles.json"))
    if shown.returncode != 0:
        return None, (shown.stderr.strip().splitlines() or ["git show failed"])[0]
    try:
        return profiles.parse_registry(json.loads(shown.stdout, object_pairs_hook=OrderedDict),
                                       "claude/profiles.json at " + _short(commit)), None
    except (ValueError, profiles.RegistryError) as exc:
        return None, str(exc)


def _plan_plugin_removal(repo, home, record, registry, target_registry, run_cli):
    """(order, dropped, kept, error): the B-only profiles' plugins to uninstall.

    error is set when the plugin lists cannot be read (nothing may change)."""
    previous = record.get("profiles") if isinstance(record, dict) else None
    b_only = [name for name in (previous if isinstance(previous, list) else [])
              if name in registry and name not in target_registry]
    if not b_only:
        return [], {}, {}, None
    try:
        plugins = _cli_list(run_cli, ["plugin", "list", "--json"])
        markets = _cli_list(run_cli, ["plugin", "marketplace", "list", "--json"])
    except ValueError as exc:
        return [], {}, {}, str(exc)
    dropped = {}
    for name in b_only:
        pid = profiles.plugin_id(registry[name])
        if any(isinstance(e, dict) and e.get("id") == pid and e.get("scope") == "user" for e in plugins):
            dropped[pid] = name
    if not dropped:
        return [], {}, {}, None
    errors = []
    try:
        toolkit_deps = plugin_deps.toolkit_dependencies(repo)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        toolkit_deps = {}
        errors.append("cannot read the toolkit's plugin manifests: %s" % exc)
    other_deps, other_errors = plugin_deps.installed_dependencies(home, plugins, markets, profiles.MARKETPLACE)
    errors.extend(other_errors)
    if errors:
        # Unproven is not safe: a plugin we could not read might need any of them.
        return [], dropped, {pid: errors[0] for pid in dropped}, None

    def deps_of(key):
        if key.endswith("@" + profiles.MARKETPLACE):
            return toolkit_deps.get(key.partition("@")[0], [])
        return other_deps.get(key, [])

    stays = {e["id"] for e in plugins
             if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"] not in dropped}
    order, kept = plugin_deps.plan_removal(dropped, stays, deps_of)
    return order, dropped, kept, None


def rollback(repo, record_file, home, run_cli=None):
    refuse = lambda message: _refuse("Not rolled back: ", message)  # noqa: E731
    run_cli = run_cli or _real_run_cli
    state = read_state(repo, record_file)
    point = install_record.rollback(state["record"])
    if point is None:
        return refuse("no rollback point is recorded; one is recorded by ./install.sh --update")
    commit = point["commit"]
    if _git(repo, "cat-file", "-e", commit + "^{commit}").returncode != 0:
        return refuse("the recorded commit %s is not in this repo" % _short(commit))
    if state["dirty"]:
        return refuse("tracked files have local changes; commit or discard them, then re-run")
    branch, default = state["branch"], state["default"]
    if branch is not None and branch != default:
        return refuse("on branch %s; rollback works from %s" % (branch, default))
    record = state["record"]
    try:
        snapshot = install_record.rollback_state(record)
    except ValueError as exc:
        return refuse("the configuration snapshot in the install record is unreadable (%s). "
                      "To move the checkout only: git -C %s checkout --detach %s && %s/install.sh"
                      % (exc, repo, commit, repo))
    if snapshot is None:
        print("No configuration snapshot was recorded with this rollback point; "
              "only the checkout moves.")
        try:
            install_record.mark_pinned(record_file, _now())
        except (OSError, ValueError) as exc:
            return refuse("could not write the install record: %s" % exc)
        moved = _git(repo, "checkout", "--quiet", "--detach", commit)
        if moved.returncode != 0:
            sys.stderr.write(moved.stderr)
            return 1
        print("Rolling back to %s" % _short(commit))
        return 0

    settings, settings_error = read_settings(home)
    if settings_error is not None:
        return refuse("%s; fix it, then re-run" % settings_error)
    try:
        registry = profiles.load_registry(repo)
    except ValueError as exc:
        return refuse("cannot read this checkout's profile registry: %s" % exc)
    target_registry, error = _registry_at(repo, commit)
    if error is not None:
        return refuse("cannot read the profile registry at %s: %s" % (_short(commit), error))
    order, dropped, kept, error = _plan_plugin_removal(repo, home, record, registry,
                                                       target_registry, run_cli)
    if error is not None:
        return refuse("cannot list the installed plugins (%s); nothing was changed" % error)
    installed, notes = _installed_values(repo, home)
    actions, more_notes = restore_plan(record, snapshot, settings, installed)
    notes.extend(more_notes)

    for note in notes:
        print(note)
    for pid in sorted(kept):
        print("kept %s: %s" % (pid, kept[pid]))
    print("Rolling back to %s" % _short(commit))

    for pid in order:
        print("uninstalling %s (profile %s is not in the version being rolled back to)"
              % (pid, dropped[pid]))
        ok, _ = run_cli(["plugin", "uninstall", pid, "--scope", "user"])
        if not ok:
            return refuse("claude plugin uninstall %s failed; re-run ./install.sh --rollback" % pid)
    try:
        if actions:
            for action in actions:
                print(_describe(action))
            settings_io.write_settings(_settings_path(home), actions)
        install_record.restore_owned(record_file, snapshot, _now())
    except (OSError, ValueError) as exc:
        return refuse("%s; re-run ./install.sh --rollback" % exc)
    moved = _git(repo, "checkout", "--quiet", "--detach", commit)
    if moved.returncode != 0:
        sys.stderr.write(moved.stderr)
        print("The configuration was restored, but the checkout did not move. Either run "
              "./install.sh to re-apply this version, or git -C %s checkout --detach %s && "
              "%s/install.sh" % (repo, commit, repo), file=sys.stderr)
        return 1
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    commands = {"version": version, "update": update, "rollback": rollback}
    if len(argv) != 2 or argv[0] not in commands:
        print(USAGE, file=sys.stderr)
        return 2
    home = os.path.expanduser("~")
    record_file = install_record.record_path(home)
    if argv[0] == "version":
        return version(argv[1], record_file)
    return commands[argv[0]](argv[1], record_file, home)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
