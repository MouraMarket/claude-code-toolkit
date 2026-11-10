#!/usr/bin/env python3
"""The one settings.json transformer, shared by `uninstall.py` and the rollback restore.

`apply_settings_actions` turns a settings dict and a list of `settings` Actions
into a new dict; `write_settings` re-reads the file, applies them once and
writes atomically. Stdlib only, importable by path.
"""

import copy
import json
import os
import shutil
import tempfile
from collections import namedtuple

_MISSING = (FileNotFoundError, NotADirectoryError)

Action = namedtuple("Action", "surface kind target data")


def _read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def apply_settings_actions(settings, actions):
    """`settings` after every `settings` Action, as a new dict (the input is untouched).

    The one transformer: the independence scan calls it to see what would be
    left, and `apply` calls it to do the removal, so the two cannot disagree.
    """
    after = copy.deepcopy(settings)
    for action in actions:
        if action.surface != "settings":
            continue
        data = action.data
        if action.kind == "remove-rule":
            perms = after.get("permissions")
            rules = perms.get(action.target) if isinstance(perms, dict) else None
            if isinstance(rules, list) and data["rule"] in rules:
                rules.remove(data["rule"])
                if not rules:
                    del perms[action.target]
                if not perms:
                    del after["permissions"]
        elif action.kind == "restore-mode":
            perms = after.get("permissions")
            if isinstance(perms, dict):
                if data["previous"] is None:
                    perms.pop("defaultMode", None)
                    if not perms:
                        del after["permissions"]
                else:
                    perms["defaultMode"] = data["previous"]
        elif action.kind == "set-key":
            present = action.target in after
            if present == data["expected_present"] and (
                    not present or (type(after[action.target]) is type(data["expected"])
                                    and after[action.target] == data["expected"])):
                after[action.target] = copy.deepcopy(data["value"])
        elif action.kind == "set-mode":
            perms = after.get("permissions")
            if isinstance(perms, dict) and "defaultMode" in perms and perms["defaultMode"] == data["expected"]:
                if data["present"]:
                    perms["defaultMode"] = copy.deepcopy(data["value"])
                else:
                    del perms["defaultMode"]
                    if not perms:
                        del after["permissions"]
        elif action.kind == "remove-key":
            if (action.target in after and type(after[action.target]) is type(data["expected"])
                    and after[action.target] == data["expected"]):
                del after[action.target]
        elif action.kind == "remove-exclude":
            current = after.get("claudeMdExcludes")
            if isinstance(current, list) and data["pattern"] in current:
                current.remove(data["pattern"])
                if not current and data["created_key"]:
                    del after["claudeMdExcludes"]
        elif action.kind == "remove-hook":
            hooks = after.get("hooks")
            blocks = hooks.get(action.target) if isinstance(hooks, dict) else None
            if isinstance(blocks, list):
                for block in blocks:
                    if isinstance(block, dict):
                        block["hooks"] = [h for h in block.get("hooks", [])
                                          if not (isinstance(h, dict) and h.get("command") == data["command"])]
                blocks[:] = [b for b in blocks if not (isinstance(b, dict) and not b.get("hooks"))]
                if not blocks:
                    del hooks[action.target]
                if not hooks:
                    del after["hooks"]
        elif action.kind == "remove-enabled":
            enabled = after.get("enabledPlugins")
            if isinstance(enabled, dict):
                enabled.pop(action.target, None)
        elif action.kind == "remove-known-marketplace":
            known = after.get("extraKnownMarketplaces")
            if isinstance(known, dict):
                known.pop(action.target, None)
    return after


def write_json_atomic(path, data, ensure_ascii=False):
    """Write `data` as indented JSON to `path` through a temp file and a rename.

    Writes through a symlink (the referent is replaced, the link stays). An
    existing file keeps its mode; a new one gets `0o666 & ~umask`.
    """
    target = os.path.realpath(path)
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory,
                                         prefix="." + os.path.basename(target) + ".",
                                         suffix=".tmp", delete=False)
    try:
        with handle:
            json.dump(data, handle, indent=2, ensure_ascii=ensure_ascii)
            handle.write("\n")
        try:
            mode = os.stat(target).st_mode & 0o7777
        except FileNotFoundError:
            mask = os.umask(0)
            os.umask(mask)
            mode = 0o666 & ~mask
        os.chmod(handle.name, mode)
        os.replace(handle.name, target)
    except BaseException:
        if os.path.exists(handle.name):
            os.unlink(handle.name)
        raise


def create_atomic(path, data, stat_from=None):
    """Create `path` holding `data` (bytes) unless it exists; True when created, False when it did.

    The bytes go to a temp file beside `path` first, then `os.link` puts them
    in place: it fails if `path` exists, so the no-clobber check is atomic.
    """
    directory = os.path.dirname(os.path.abspath(path))
    handle = tempfile.NamedTemporaryFile("wb", dir=directory, prefix="." + os.path.basename(path) + ".",
                                         suffix=".tmp", delete=False)
    try:
        with handle:
            handle.write(data)
        if stat_from is not None:
            shutil.copystat(stat_from, handle.name)
        os.link(handle.name, path)
        return True
    except FileExistsError:
        return False
    finally:
        if os.path.exists(handle.name):
            os.unlink(handle.name)


def write_settings(settings_path, actions):
    """Re-read settings.json (the CLI edits it), apply every settings Action once, write atomically."""
    target = os.path.realpath(settings_path)
    try:
        data = _read_json(target)
    except _MISSING:
        data = {}
    if not isinstance(data, dict):
        raise ValueError("%s is not a JSON object" % settings_path)
    after = apply_settings_actions(data, actions)
    # Compare serialized, not with ==: Python has True == 1 == 1.0, so a change
    # of JSON type alone (a rollback putting back a `1`) would read as no change.
    if json.dumps(after, sort_keys=True) == json.dumps(data, sort_keys=True):
        return
    write_json_atomic(target, after)
