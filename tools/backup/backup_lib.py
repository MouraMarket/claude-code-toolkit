"""claude-backup logic: inventory, staging, secret scan, manifest.

No argparse and no printing: callers (the CLI, the tests) get return values and
BackupAbort.
"""
import fnmatch
import hashlib
import json
import ntpath
import os
import posixpath
import re
import shutil
import stat
import subprocess
import socket
import sys
from datetime import datetime, timedelta, timezone

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.join(_REPO, "lib"))
sys.path.insert(0, os.path.join(_REPO, "tools", "secret-scan"))
import toolkit_config  # noqa: E402,F401
import claude_home  # noqa: E402,F401
import auto_memory  # noqa: E402
import secret_lib  # noqa: E402

REV = 1
NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{6}Z$")
INCOMPLETE_PREFIX = ".incomplete-"
BUILTIN_EXCLUDES = (".venv*", "venv", "node_modules", ".git", "__pycache__",
                    ".mypy_cache", ".pytest_cache", ".DS_Store", "*.pyc",
                    ".env", "*.pem", "*.p12", "*.key")
DEFAULT_KEEP = 8
# Required-item statuses that make a backup incomplete.
INCOMPLETE_STATUSES = ("missing", "unreadable", "not-a-directory")
DEFAULT_MAX_AGE_DAYS = 7


class BackupAbort(Exception):
    """Run must stop: exit 2, nothing published."""


def host_name(platform=None, environ=None, run=None):
    """A STABLE name for this machine -- it names the folder backups live in.

    Not gethostname() alone: on macOS that is whatever the network handed out
    ("Mac.lan" at home, something else elsewhere), so --status would find "no
    backup yet" after a change of Wi-Fi. macOS's LocalHostName and Windows'
    COMPUTERNAME are set by the person and do not move.
    """
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    run = subprocess.run if run is None else run
    raw = ""
    if platform == "darwin":
        try:
            raw = run(["scutil", "--get", "LocalHostName"], capture_output=True,
                      text=True, encoding="utf-8", errors="replace",
                      timeout=5).stdout.strip()
        except Exception:
            raw = ""
    elif platform == "win32":
        raw = environ.get("COMPUTERNAME", "")
    if not raw:
        raw = socket.gethostname()
    name = re.sub(r"[^A-Za-z0-9._-]", "-", raw)
    return name or "unknown-host"


def configured_memory_dir(claude_dir, home):
    """The memory dir named by <claude_dir>/settings.json, or None.

    Reads claude_dir, not <home>/.claude, so $CLAUDE_CONFIG_DIR is honoured.
    """
    try:
        with open(os.path.join(claude_dir, "settings.json"), "r",
                  encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("autoMemoryDirectory")
    if not isinstance(value, str):
        return None
    return auto_memory.expand(value, home)


def _item(source, dest_rel, required, skip_paths=None, want_dir=False):
    status = "pending" if source is not None and os.path.exists(source) else "missing"
    # A directory-only source that is a file would copy as that one file and
    # still read as complete.
    if status == "pending" and want_dir and not os.path.isdir(source):
        status = "not-a-directory"
    return {"source": source, "dest_rel": dest_rel, "required": required,
            "status": status, "skip_paths": list(skip_paths or [])}


def build_inventory(workspace_root, home, claude_dir, config_path, extra_paths,
                    repos_root):
    items = []
    if workspace_root is None:
        items.append(_item(None, "workspace-root", True))
    else:
        # The checkouts' remotes hold them; /wrap-up's survey covers what is
        # not pushed yet.
        items.append(_item(workspace_root, "workspace-root", True,
                           [repos_root] if repos_root else [], want_dir=True))

    configured = configured_memory_dir(claude_dir, home)
    configured_real = None
    if configured is not None:
        configured_real = os.path.realpath(configured)
        items.append(_item(configured, "memory/configured", True, want_dir=True))

    # Enumerated by hand, not glob: glob silently skips a directory it cannot
    # read, which would drop that project's memory from a "complete" backup.
    project_count = 0
    projects_dir = os.path.join(claude_dir, "projects")
    try:
        entries = sorted(os.listdir(projects_dir))
    except FileNotFoundError:
        entries = []
    except OSError:
        entries = None
        items.append({"source": projects_dir, "dest_rel": "memory", "required": True,
                      "status": "unreadable", "skip_paths": []})
    for project in entries or []:
        path = os.path.join(projects_dir, project, "memory")
        try:
            mode = os.stat(path).st_mode
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            items.append({"source": path, "dest_rel": "memory/" + project,
                          "required": True, "status": "unreadable", "skip_paths": []})
            project_count += 1
            continue
        if not stat.S_ISDIR(mode):
            continue
        if configured_real is not None and os.path.realpath(path) == configured_real:
            continue
        items.append(_item(path, "memory/" + project, True))
        project_count += 1
    if project_count == 0 and configured is None and entries is not None:
        items.append({"source": None, "dest_rel": "memory", "required": True,
                      "status": "missing", "skip_paths": []})

    if config_path:
        items.append(_item(config_path, "toolkit-config/config.json", False))
    items.append(_item(os.path.join(claude_dir, "settings.json"),
                       "claude-settings/settings.json", False))
    for index, path in enumerate(extra_paths or [], start=1):
        base = os.path.basename(os.path.normpath(path))
        items.append(_item(path, "extra/%d-%s" % (index, base), False))
    return items


def _contains(outer, inner):
    try:
        return os.path.commonpath([outer, inner]) == outer
    except ValueError:  # different drives
        return False


def check_destination(destination, host, items):
    if not destination or not os.path.isdir(destination):
        raise BackupAbort("destination %r is not an existing directory" % (destination,))
    publish = os.path.realpath(os.path.join(destination, host))
    for item in items:
        if item.get("source") is None:
            continue
        source = os.path.realpath(item["source"])
        if _contains(publish, source) or _contains(source, publish):
            raise BackupAbort(
                "destination %s overlaps backup source %s" % (publish, source))


def _join_rel(base, name):
    return base + "/" + name if base else name


def _walk_error(error):
    raise BackupAbort("cannot read %s: %s" % (getattr(error, "filename", ""), error))


def _is_excluded(name, rel_posix, abs_path, patterns, user_patterns, skip_set):
    if abs_path in skip_set:
        return True
    for pat in patterns:
        if fnmatch.fnmatch(name, pat):
            return True
    for pat in user_patterns:
        if fnmatch.fnmatch(rel_posix, pat):
            return True
    return False


def _stage_file(abs_path, manifest_path, dest_rel, rel, staging_dir, parts):
    """Read, decode, scan, and only then write the same bytes."""
    try:
        with open(abs_path, "rb") as handle:
            data = handle.read()
    except OSError as error:
        raise BackupAbort("cannot read %s: %s" % (abs_path, error))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    # NUL is valid UTF-8, so BOM-less UTF-16 decodes "fine" with a NUL between
    # every letter and no secret pattern can match it. Not scannable text.
    if text is None or "\x00" in text:
        parts["excluded"].append({"path": manifest_path, "reason": "unscannable",
                                  "detail": ""})
        return
    findings = secret_lib.scan_text(text, manifest_path)
    if findings:
        detail = "; ".join("%s line %s [%s]" % (f.rule, f.line_number, f.redacted)
                           for f in findings)
        parts["excluded"].append({"path": manifest_path, "reason": "secret",
                                  "detail": detail})
        return
    target = os.path.join(staging_dir, *manifest_path.split("/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "wb") as handle:
        handle.write(data)
    parts["files"].append({"path": manifest_path, "type": "file", "size": len(data),
                           "sha256": hashlib.sha256(data).hexdigest()})


def _classify_entry(abs_path, manifest_path, parts):
    """Handle symlinks and odd types. True when the entry is fully handled."""
    if os.path.islink(abs_path):
        target = os.readlink(abs_path)
        # The target is text that lands in MANIFEST.json, so it is scanned
        # like file content; a hit records the link as excluded, target omitted.
        findings = secret_lib.scan_text(target, manifest_path)
        if findings:
            detail = "; ".join("%s in link target [%s]" % (f.rule, f.redacted)
                               for f in findings)
            parts["excluded"].append({"path": manifest_path, "reason": "secret",
                                      "detail": detail})
            return True
        parts["files"].append({"path": manifest_path, "type": "symlink",
                               "link_target": target})
        return True
    return False


def stage(items, staging_dir, user_excludes):
    parts = {"files": [], "excluded": []}
    user_patterns = list(user_excludes or [])
    patterns = list(BUILTIN_EXCLUDES) + user_patterns
    for item in items:
        source = item.get("source")
        if source is None or item["status"] != "pending":
            continue
        dest_rel = item["dest_rel"]
        root = os.path.realpath(source)
        if os.path.isfile(root):
            # A file item is excluded by its own name, the same as a file found
            # in a walk: an `extra_paths` entry naming a `.env` must not ship.
            # Both spellings: the name as configured and the one it resolves to.
            names = {os.path.basename(os.path.normpath(source)), os.path.basename(root)}
            if any(_is_excluded(name, dest_rel, root, patterns, user_patterns, set())
                   for name in names):
                parts["excluded"].append({"path": dest_rel, "reason": "pattern",
                                          "detail": ""})
                item["status"] = "copied"
                continue
            try:
                _stage_file_item(root, dest_rel, staging_dir, parts)
            except _Unreadable:
                item["status"] = "unreadable"
                continue
            item["status"] = "copied"
            continue
        try:
            os.listdir(root)
        except OSError:
            item["status"] = "unreadable"
            continue
        skip_set = set(os.path.realpath(p) for p in item.get("skip_paths", []))
        # The walk compares only what it finds inside the root, so a skip path
        # equal to (or above) the root itself must be caught here.
        if any(_contains(skip, root) for skip in skip_set):
            parts["excluded"].append({"path": dest_rel, "reason": "pattern",
                                      "detail": "inside the repos root"})
            item["status"] = "copied"
            continue
        for dirpath, dirnames, filenames in os.walk(
                root, topdown=True, followlinks=False, onerror=_walk_error):
            rel_dir = os.path.relpath(dirpath, root)
            rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
            keep = []
            for name in sorted(dirnames):
                abs_path = os.path.join(dirpath, name)
                rel = _join_rel(rel_dir, name)
                manifest_path = dest_rel + "/" + rel
                if _classify_entry(abs_path, manifest_path, parts):
                    continue
                if _is_excluded(name, rel, abs_path, patterns, user_patterns, skip_set):
                    parts["excluded"].append({"path": manifest_path,
                                              "reason": "pattern", "detail": ""})
                    continue
                keep.append(name)
            dirnames[:] = keep
            for name in sorted(filenames):
                abs_path = os.path.join(dirpath, name)
                rel = _join_rel(rel_dir, name)
                manifest_path = dest_rel + "/" + rel
                if _classify_entry(abs_path, manifest_path, parts):
                    continue
                if _is_excluded(name, rel, abs_path, patterns, user_patterns, skip_set):
                    parts["excluded"].append({"path": manifest_path,
                                              "reason": "pattern", "detail": ""})
                    continue
                if not os.path.isfile(abs_path):
                    parts["excluded"].append({"path": manifest_path,
                                              "reason": "unscannable", "detail": ""})
                    continue
                _stage_file(abs_path, manifest_path, dest_rel, rel, staging_dir, parts)
        item["status"] = "copied"
    return parts


class _Unreadable(Exception):
    pass


def _stage_file_item(path, dest_rel, staging_dir, parts):
    try:
        open(path, "rb").close()
    except OSError:
        raise _Unreadable()
    _stage_file(path, dest_rel, dest_rel, "", staging_dir, parts)


def write_manifest(staging_dir, items, parts, host, created_at, toolkit_commit):
    complete = not any(i["required"] and i["status"] in INCOMPLETE_STATUSES
                       for i in items)
    manifest = {
        "rev": REV,
        "host": host,
        "created_at": created_at,
        "toolkit_commit": toolkit_commit,
        "complete": complete,
        "items": [{"source": i["source"], "dest_rel": i["dest_rel"],
                   "required": i["required"], "status": i["status"]} for i in items],
        "files": parts["files"],
        "excluded": parts["excluded"],
    }
    os.makedirs(staging_dir, exist_ok=True)
    with open(os.path.join(staging_dir, "MANIFEST.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
    return manifest


# ---- publish, prune, status -------------------------------------------------

class ManifestError(Exception):
    """A MANIFEST.json that is missing or fails the shared contract."""


def _naive_utc(moment):
    """`now` may be naive-UTC or UTC-aware; everything here compares naive-UTC."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def utc_stamp(now):
    return _naive_utc(now).strftime("%Y-%m-%dT%H%M%SZ")


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _check_manifest(data):
    if not isinstance(data, dict):
        raise ManifestError("manifest is not a JSON object")
    if not _is_int(data.get("rev")) or data["rev"] != REV:
        raise ManifestError("unsupported rev %r" % (data.get("rev"),))
    if not isinstance(data.get("host"), str):
        raise ManifestError("host is not a string")
    created = data.get("created_at")
    if not isinstance(created, str):
        raise ManifestError("created_at is not a string")
    try:
        parsed = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ManifestError("bad created_at %r" % (created,))
    # strptime alone accepts unpadded fields ("2026-1-2T3:4:5Z").
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != created:
        raise ManifestError("bad created_at %r" % (created,))
    if not isinstance(data.get("complete"), bool):
        raise ManifestError("complete is not a bool")
    items = data.get("items")
    if not isinstance(items, list):
        raise ManifestError("items is not a list")
    for item in items:
        if not (isinstance(item, dict)
                and isinstance(item.get("dest_rel"), str)
                and isinstance(item.get("required"), bool)
                and isinstance(item.get("status"), str)
                and (item.get("source") is None or isinstance(item.get("source"), str))
                and "source" in item):
            raise ManifestError("bad item entry %r" % (item,))
    files = data.get("files")
    if not isinstance(files, list):
        raise ManifestError("files is not a list")
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise ManifestError("bad file entry %r" % (entry,))
        kind = entry.get("type")
        if kind == "file":
            sha = entry.get("sha256")
            if not _is_int(entry.get("size")):
                raise ManifestError("file entry without int size: %r" % (entry,))
            if not (isinstance(sha, str) and re.match(r"^[0-9a-fA-F]{64}$", sha)):
                raise ManifestError("file entry without valid sha256: %r" % (entry,))
        elif kind == "symlink":
            if not isinstance(entry.get("link_target"), str):
                raise ManifestError("symlink entry without link_target: %r" % (entry,))
        else:
            raise ManifestError("unknown file type %r" % (kind,))
    if not isinstance(data.get("excluded"), list):
        raise ManifestError("excluded is not a list")


def load_manifest(folder):
    """Parsed, validated MANIFEST.json in folder; ManifestError otherwise."""
    try:
        with open(os.path.join(folder, "MANIFEST.json"), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:  # UnicodeDecodeError is a ValueError
        raise ManifestError("cannot read manifest in %s: %s" % (folder, error))
    _check_manifest(data)
    return data


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def publish(staging_dir, destination, host, manifest, now):
    """Copy staging to <destination>/<host>/<stamp>, verified, atomically named."""
    stamp = utc_stamp(now)
    host_dir = os.path.join(destination, host)
    os.makedirs(host_dir, exist_ok=True)
    temp = os.path.join(host_dir, INCOMPLETE_PREFIX + stamp)
    final = os.path.join(host_dir, stamp)
    if os.path.lexists(final):
        raise BackupAbort("backup %s already exists" % final)
    try:
        shutil.copytree(staging_dir, temp, symlinks=True)
    except (OSError, shutil.Error) as error:
        raise BackupAbort("cannot copy staging to %s: %s" % (temp, error))
    for entry in manifest["files"]:
        if entry.get("type") != "file":
            continue
        copy = os.path.join(temp, *entry["path"].split("/"))
        try:
            ok = _sha256_file(copy) == entry["sha256"]
        except OSError:
            ok = False
        if not ok:
            shutil.rmtree(temp, ignore_errors=True)
            raise BackupAbort("verification failed for %s" % entry["path"])
    if os.path.lexists(final):
        raise BackupAbort("backup %s already exists" % final)
    try:
        os.rename(temp, final)
    except OSError as error:
        raise BackupAbort("cannot rename %s to %s: %s" % (temp, final, error))
    return final


def valid_backups(host_dir):
    """[(name, manifest)] newest first; anything else is invisible."""
    try:
        names = os.listdir(host_dir)
    except OSError:
        return []
    found = []
    for name in names:
        path = os.path.join(host_dir, name)
        if not NAME_RE.match(name) or not os.path.isdir(path):
            continue
        try:
            found.append((name, load_manifest(path)))
        except ManifestError:
            continue
    found.sort(key=lambda pair: pair[0], reverse=True)
    return found


def prune(host_dir, keep, now):
    """Delete valid backups beyond the newest `keep` and stale .incomplete- folders."""
    deleted = []
    backups = valid_backups(host_dir)
    # Never evict the newest COMPLETE backup: a run of incomplete ones (a
    # workspace gone missing) must not age out the last recoverable copy.
    newest_complete = next((n for n, m in backups if m["complete"]), None)
    for name, _manifest in backups[max(keep, 0):]:
        if name == newest_complete:
            continue
        shutil.rmtree(os.path.join(host_dir, name))
        deleted.append(name)
    cutoff = _naive_utc(now) - timedelta(hours=24)
    try:
        names = os.listdir(host_dir)
    except OSError:
        return deleted
    for name in sorted(names):
        if not name.startswith(INCOMPLETE_PREFIX):
            continue
        stamp = name[len(INCOMPLETE_PREFIX):]
        path = os.path.join(host_dir, name)
        if not NAME_RE.match(stamp) or not os.path.isdir(path):
            continue
        try:
            # NAME_RE admits impossible dates (month 13); those are not ours.
            made = datetime.strptime(stamp, "%Y-%m-%dT%H%M%SZ")
        except ValueError:
            continue
        if made < cutoff:
            shutil.rmtree(path)
            deleted.append(name)
    return deleted


def status(destination, host, max_age_days, now):
    """(exit_code, lines): 0 fresh and complete, 1 stale/incomplete/none, 2 unusable."""
    if not destination:
        return 2, ["backup destination is not set"]
    if not os.path.isdir(destination) or not os.access(destination, os.R_OK | os.X_OK):
        return 2, ["backup destination %s is not a readable directory" % destination]
    host_dir = os.path.join(destination, host)
    backups = valid_backups(host_dir)
    if not backups:
        return 1, ["no backup yet in %s" % host_dir]
    name, manifest = backups[0]
    created = datetime.strptime(manifest["created_at"], "%Y-%m-%dT%H:%M:%SZ")
    age = _naive_utc(now) - created
    size = sum(e["size"] for e in manifest["files"] if e["type"] == "file")
    line = "newest %s, age %.1f days, %.1f MB, %d excluded, complete=%s" % (
        os.path.join(host_dir, name), age.total_seconds() / 86400.0,
        size / (1024.0 * 1024.0), len(manifest["excluded"]),
        "true" if manifest["complete"] else "false")
    fresh = manifest["complete"] and age <= timedelta(days=max_age_days)
    return (0 if fresh else 1), [line]


# ---- restore ----------------------------------------------------------------

def safe_rel(path, target):
    """Return the absolute destination for a manifest path, or raise ValueError.

    A downloaded manifest is untrusted: reject absolute, drive-qualified and
    `..`-bearing paths (in posix or Windows spelling), then confirm the resolved
    destination really sits under target.
    """
    if not path:
        raise ValueError("empty path")
    # Python 3.13's ntpath.isabs no longer counts a rooted "\\x" as absolute.
    if path[0] in "/\\" or posixpath.isabs(path) or ntpath.isabs(path):
        raise ValueError("absolute path")
    if ntpath.splitdrive(path)[0]:
        raise ValueError("drive-qualified path")
    if any(part == ".." for part in re.split(r"[/\\]", path)):
        raise ValueError("'..' component")
    root = os.path.realpath(target)
    dest = os.path.realpath(os.path.join(target, *path.split("/")))
    try:
        inside = os.path.commonpath([root, dest]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("resolves outside the restore target")
    return dest


def restore(folder, target):
    """Copy a backup folder to a new target, re-hashing. Returns (exit_code, lines)."""
    try:
        manifest = load_manifest(folder)
    except ManifestError as error:
        return 2, ["cannot restore %s: %s" % (folder, error)]
    if os.path.lexists(target):
        return 2, ["restore target %s already exists; choose a new path" % target]
    try:
        os.makedirs(target)
        shutil.copyfile(os.path.join(folder, "MANIFEST.json"),
                        os.path.join(target, "MANIFEST.json"))
    except OSError as error:
        return 2, ["cannot create restore target %s: %s" % (target, error)]
    failures = []
    symlinks = []
    for entry in manifest["files"]:
        path = entry["path"]
        if entry["type"] == "symlink":
            symlinks.append("%s -> %s" % (path, entry["link_target"]))
            continue
        try:
            dest = safe_rel(path, target)
        except ValueError as error:
            failures.append("REJECTED %s: %s" % (path, error))
            continue
        source = os.path.join(folder, *path.split("/"))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copyfile(source, dest)
            ok = _sha256_file(dest) == entry["sha256"]
        except FileNotFoundError:
            failures.append("MISSING %s" % path)
            continue
        except OSError as error:
            failures.append("UNREADABLE %s: %s" % (path, error))
            continue
        if not ok:
            failures.append("MISMATCH %s (hash differs from manifest)" % path)
    if symlinks:
        with open(os.path.join(target, "SYMLINKS.txt"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(symlinks) + "\n")
    lines = ["%s → %s" % (i["source"], os.path.join(target, *i["dest_rel"].split("/")))
             for i in manifest["items"] if i["status"] == "copied"]
    lines.extend(failures)
    return (1 if failures else 0), lines
