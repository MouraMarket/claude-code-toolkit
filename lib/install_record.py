#!/usr/bin/env python3
"""The install record: what install.sh added to the user's config.

`~/.config/claude-code-toolkit/install-record.json` lists the permission rules,
default mode and CLAUDE.md exclude the installer wrote, so an uninstall removes
only what the installer owns. install.sh calls the writers through this module
and ends a run with `python3 lib/install_record.py finish <repo>` (or `stamp`
when the run is red, so the audit's window still starts).

Writes are atomic (temp file + os.replace). An unreadable record is moved to
`<path>.corrupt-<UTC stamp>` and a new one started. OSError from a write
propagates; the caller decides what it means.

`first_installed_at` is stamped by the first `finish` or `stamp` and never changed after.

The `rollback` entry is written by `--update` and `--rollback`
(`lib/toolkit_update.py`) and read only through `rollback()`.
"""

import copy
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

SCHEMA = 1
_KINDS = ("allow", "ask", "deny")


def record_path(home):
    return os.path.join(home, ".config", "claude-code-toolkit", "install-record.json")


def _str_list(value):
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _mode_ok(mode):
    if not isinstance(mode, dict) or not isinstance(mode.get("set"), str):
        return False
    return "previous" in mode and (mode["previous"] is None or isinstance(mode["previous"], str))


def _exclude_ok(exclude):
    return (isinstance(exclude, dict) and isinstance(exclude.get("pattern"), str)
            and isinstance(exclude.get("created_key"), bool))


def _valid(data):
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        return False
    perms = data.get("permissions_added")
    if not isinstance(perms, dict):
        return False
    if any(kind in perms and not _str_list(perms[kind]) for kind in _KINDS):
        return False
    # Absent, or a complete object: an explicit null is malformed, not "absent".
    if "default_mode" in data and not _mode_ok(data["default_mode"]):
        return False
    if "claude_md_excludes_added" in data and not _exclude_ok(data["claude_md_excludes_added"]):
        return False
    # Absent means "not ours": False, null and a string are malformed.
    if "enable_workflows_added" in data and data["enable_workflows_added"] is not True:
        return False
    for key in ("checkout", "commit", "installed_at",
                "first_installed_at"):
        if data.get(key) is not None and not isinstance(data[key], str):
            return False
    profiles = data.get("profiles")
    if profiles is not None and not _str_list(profiles):
        return False
    return True


def load(path):
    """The record as a dict, or None if absent, unreadable or malformed."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if _valid(data) else None


def _fresh():
    return {"schema": SCHEMA, "permissions_added": {"allow": [], "ask": [], "deny": []}}


def _open_for_update(path):
    """The existing record, a fresh one if absent, or a fresh one after
    moving an unreadable file aside."""
    record = load(path)
    if record is not None:
        for kind in _KINDS:
            record["permissions_added"].setdefault(kind, [])
        return record
    if os.path.lexists(path):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        aside = "%s.corrupt-%s" % (path, stamp)
        os.replace(path, aside)
        print("install record unreadable; moved to %s, starting a new one"
              % os.path.basename(aside), file=sys.stderr)
    return _fresh()


_SETTING_VALUES = ("defaultMode", "claudeMdExcludes", "statusLine", "autoMemoryDirectory")


def _buckets_ok(buckets):
    return (isinstance(buckets, dict) and set(buckets) == set(_KINDS)
            and all(_str_list(buckets[kind]) for kind in _KINDS))


def _value_entry_ok(entry):
    if not isinstance(entry, dict):
        return False
    if entry.get("present") is True:
        return set(entry) == {"present", "value"}
    return entry.get("present") is False and set(entry) == {"present"}


def _state_ok(state):
    """The snapshot shape: exactly the keys design section 1 names, no others."""
    if not isinstance(state, dict):
        return False
    if not set(state) <= {"permissions_added", "default_mode", "claude_md_excludes_added",
                          "enable_workflows_added", "enable_workflows_base", "settings"}:
        return False
    if "permissions_added" not in state or "settings" not in state:
        return False
    if not _buckets_ok(state["permissions_added"]):
        return False
    if "default_mode" in state and not _mode_ok(state["default_mode"]):
        return False
    if "claude_md_excludes_added" in state and not _exclude_ok(state["claude_md_excludes_added"]):
        return False
    if "enable_workflows_added" in state and state["enable_workflows_added"] is not True:
        return False
    if "enable_workflows_base" in state and not _value_entry_ok(state["enable_workflows_base"]):
        return False
    settings = state["settings"]
    if not isinstance(settings, dict) or set(settings) != {"permissions", *_SETTING_VALUES}:
        return False
    if not _buckets_ok(settings["permissions"]):
        return False
    return all(_value_entry_ok(settings[key]) for key in _SETTING_VALUES)


def _write(path, record):
    # Never write a shape load() would reject: the next writer would move the
    # whole record aside as corrupt and lose what the installer owns. The
    # record came from load() or _fresh(), so a failure here is always the
    # current writer's input. Raises before anything touches the disk.
    if not _valid(record):
        raise ValueError("refusing to write an install record load() would reject")
    if "rollback" in record and rollback(record) is None:
        raise ValueError("refusing to write a rollback entry rollback() would reject")
    if "rollback" in record and "state" in record["rollback"] and not _state_ok(record["rollback"]["state"]):
        raise ValueError("refusing to write a rollback state with a malformed shape")
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=2)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def add_permissions(path, added, mode_set=None, mode_previous=None):
    record = _open_for_update(path)
    perms = record["permissions_added"]
    for kind in _KINDS:
        for rule in added.get(kind, []):
            if rule not in perms[kind]:
                perms[kind].append(rule)
    if mode_set is not None:
        if "default_mode" in record:
            record["default_mode"]["set"] = mode_set
        else:
            record["default_mode"] = {"set": mode_set, "previous": mode_previous}
    _write(path, record)


def add_exclude(path, pattern, created_key):
    record = _open_for_update(path)
    if "claude_md_excludes_added" not in record:
        record["claude_md_excludes_added"] = {"pattern": pattern, "created_key": created_key}
    _write(path, record)


def set_workflows_owned(path, owned):
    record = _open_for_update(path)
    if owned:
        record["enable_workflows_added"] = True
    else:
        record.pop("enable_workflows_added", None)
    _write(path, record)


def _stamp_first(record, installed_at):
    # Set once, never overwritten: the first install's stamp outlives re-runs.
    first = record.get("first_installed_at")
    if not isinstance(first, str) or not first:
        # A record from before this key keeps its last install as the earliest
        # install time known; only a record with neither takes this run's stamp.
        previous = record.get("installed_at")
        record["first_installed_at"] = (previous if isinstance(previous, str) and previous
                                        else installed_at)


def stamp_first_install(path, installed_at):
    """Set `first_installed_at` if absent and write no other key: the hooks are
    installed, so the guard audit's window starts now, even on a run that is
    not clean enough for `finish`."""
    record = _open_for_update(path)
    _stamp_first(record, installed_at)
    _write(path, record)


def finish(path, checkout, commit, profiles, installed_at):
    record = _open_for_update(path)
    _stamp_first(record, installed_at)
    record.update({"checkout": checkout, "commit": commit,
                   "profiles": profiles, "installed_at": installed_at})
    _write(path, record)


def rollback(record):
    """The record's rollback entry, or None if absent or malformed."""
    if not isinstance(record, dict):
        return None
    entry = record.get("rollback")
    if not isinstance(entry, dict):
        return None
    commit = entry.get("commit")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        return None
    if not isinstance(entry.get("recorded_at"), str):
        return None
    if "pinned_at" not in entry or (entry["pinned_at"] is not None
                                    and not isinstance(entry["pinned_at"], str)):
        return None
    return entry


def rollback_state(record):
    """The rollback entry's snapshot, None when it has none; ValueError when
    one is present but malformed."""
    entry = rollback(record)
    if entry is None or "state" not in entry:
        return None
    if not _state_ok(entry["state"]):
        raise ValueError("rollback state is malformed")
    return entry["state"]


def set_rollback(path, commit, recorded_at, state=None):
    record = _open_for_update(path)
    record["rollback"] = {"commit": commit, "recorded_at": recorded_at, "pinned_at": None}
    if state is not None:
        record["rollback"]["state"] = state
    _write(path, record)


def refresh_state(path, state):
    """Replace the rollback entry's snapshot, keeping the rest of the entry."""
    record = _open_for_update(path)
    if rollback(record) is None:
        raise ValueError("no rollback point recorded")
    record["rollback"]["state"] = state
    _write(path, record)


def restore_owned(path, state, pinned_at):
    """Put back what the installer owned as of `state`, in one write. Profiles,
    commit, install stamps and the rollback snapshot are left alone."""
    record = _open_for_update(path)
    if rollback(record) is None:
        raise ValueError("no rollback point recorded")
    if not _state_ok(state):
        raise ValueError("rollback state is malformed")
    record["permissions_added"] = copy.deepcopy(state["permissions_added"])
    for key in ("default_mode", "claude_md_excludes_added", "enable_workflows_added"):
        if key in state:
            record[key] = copy.deepcopy(state[key])
        else:
            record.pop(key, None)
    record["rollback"]["pinned_at"] = pinned_at
    _write(path, record)


def restore_rollback(path, entry):
    """Put back a rollback entry captured before a write: `entry` as it was,
    or no entry at all when it is None. For an update git refused."""
    record = _open_for_update(path)
    if entry is None:
        record.pop("rollback", None)
    else:
        record["rollback"] = dict(entry)
    _write(path, record)


def mark_pinned(path, pinned_at):
    record = _open_for_update(path)
    if rollback(record) is None:
        raise ValueError("no rollback point recorded")
    record["rollback"]["pinned_at"] = pinned_at
    _write(path, record)


def clear_pinned(path):
    record = _open_for_update(path)
    if rollback(record) is not None:
        record["rollback"]["pinned_at"] = None
        _write(path, record)


def ensure_writable(path):
    """Write the record back unchanged: proves it can be written."""
    _write(path, _open_for_update(path))


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _commit_of(repo):
    try:
        out = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                             encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    sha = out.stdout.strip()
    return sha if out.returncode == 0 and sha else None


def _profiles_of(repo):
    try:
        import profiles
        names = profiles.selected(repo)[0]
        return list(names) if names is not None else None
    except Exception:
        return None


def _cli(argv):
    if argv == ["stamp"]:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            stamp_first_install(record_path(os.path.expanduser("~")), now)
        except (OSError, ValueError) as exc:
            print("install record not written: %s" % exc, file=sys.stderr)
            return 1
        return 0
    if len(argv) != 2 or argv[0] != "finish":
        print("usage: install_record.py finish <repo> | stamp", file=sys.stderr)
        return 2
    repo = argv[1]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        finish(record_path(os.path.expanduser("~")), os.path.realpath(repo),
               _commit_of(repo), _profiles_of(repo), stamp)
    except OSError as exc:
        print("install record not written: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(_cli(sys.argv[1:]))
