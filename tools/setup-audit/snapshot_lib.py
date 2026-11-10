"""`claude-setup-audit snapshot` -- an append-only, timestamped copy of every
path the toolkit's installers touch, taken before any of them run.

Nothing here ever deletes or rewrites a PAST snapshot. `take_snapshot` always
creates a brand-new `<home>/.config/claude-code-toolkit/snapshots/<stamp>/`
directory (`os.makedirs(..., exist_ok=False)`) and only ever writes inside it
-- including never reading INTO another snapshot: the walk over
`~/.config/claude-code-toolkit/` below prunes `snapshots/` at its own top
level, both so a new snapshot can never nest an old one inside itself and so
the directory this call is about to create is never visited by its own walk.
`apply_lib.apply_safe` is the one caller that deletes anything on
the machine, and only after a fresh snapshot of exactly what it is about to
remove succeeds.

Two kinds of paths, matching the design doc:

- **Kind 1 (link directories)** -- `~/.local/bin`, `~/bin`, and the four
  `~/.claude/{commands,agents,build-lens,skills}` dirs, which now hold only
  legacy links `install.sh` removes. Every symlink entry is
  recorded as a symlink (never followed -- its referent's bytes are NOT
  captured here, only the `readlink` string). A real file or directory is
  copied wholesale under the four `~/.claude/*` dirs; in the two `bin`
  directories a real file/dir is only copied when it is a manifest
  destination (`audit_lib.load_manifest`), since anything else there is
  theirs and install.sh never touches it.
- **Kind 2 (written in place)** -- a fixed list of individual paths
  (`~/.claude/settings.json` and install.sh's backups of it, `~/.claude/CLAUDE.md`,
  `~/.gitconfig`, the git excludes file, the workspace's own CLAUDE.md/
  TOOLING.md, every top-level `*.json` under `~/.claude/plugins/`, and every
  path under `~/.config/claude-code-toolkit/` except `snapshots/`). Missing
  -> an `absent` entry. A symlink here ALSO captures its referent's bytes
  when the referent is a file -- unlike Kind 1 -- because these are exactly
  the paths install.sh writes, and it writes through a symlink to what it
  names (a temp file beside the referent, then a rename over it) rather than
  touching the link itself (`~/.claude/settings.json` as a dotfiles symlink
  is the case this exists for).

`manifest.json` rows always carry all four optional keys (`sha256`,
`link_target`, `referent`), `null` where not applicable -- the same
"every key present, null where absent" convention the audit report uses, so
a reader never has to guard a `KeyError`.
"""

import glob
import hashlib
import json
import os
import shutil
import stat
import subprocess
from datetime import datetime

import audit_lib

# Kind 1: every symlink entry is recorded regardless of directory; a real
# file/dir is copied wholesale ONLY in these four -- the two `bin`
# directories additionally require the path be a manifest destination
# (checked separately, since local/personal executables theirs_only never
# touches must not be swept in wholesale).
LINK_DIRECTORIES = (
    os.path.join(".local", "bin"),
    "bin",
    os.path.join(".claude", "commands"),
    os.path.join(".claude", "agents"),
    os.path.join(".claude", "build-lens"),
    os.path.join(".claude", "skills"),
)
COPY_REAL_CONTENT_DIRS = frozenset((
    os.path.join(".claude", "commands"),
    os.path.join(".claude", "agents"),
    os.path.join(".claude", "build-lens"),
    os.path.join(".claude", "skills"),
))


class SnapshotError(Exception):
    """The snapshot could not be taken safely, or at all. The CLI maps this
    to exit 3."""


def _entry_kind(path):
    """The `lstat`-only kind of `path`: "symlink", "dir", "file", or `None`
    for anything else (FIFO, socket, device, door, ...). This NEVER opens
    the path and never follows a symlink -- every classification in this
    module goes through this before `open()` / `shutil.copy2` /
    `shutil.copytree` ever touches a path, because opening a FIFO with
    nothing writing to it blocks forever (confirmed by execution: it hung
    `take_snapshot` indefinitely)."""
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return None


def _refuse_unsafe(path):
    raise SnapshotError(
        "refusing to snapshot %s: not a regular file, directory or symlink "
        "-- a FIFO, socket or device node would hang or crash open()" % path)


def _sha256_file(path):
    """Reads THROUGH a symlink -- the caller decides whether that is the
    referent's bytes (Kind 2) or simply how a real file's own content is
    hashed."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stored_rel(path, home):
    """Where a path's bytes live under a snapshot's `files/` directory:
    relative to `home` when it is inside it, else `_abs/<path without its
    leading separator>` (the design's rule for anything outside `$HOME`,
    such as a repo-side referent)."""
    home_norm = os.path.normpath(home)
    path_norm = os.path.normpath(path)
    if path_norm == home_norm or path_norm.startswith(home_norm + os.sep):
        return os.path.relpath(path_norm, home_norm)
    return os.path.join("_abs", path_norm.lstrip(os.sep))


def _expand_home(value, home):
    """`~` expansion against the SANDBOX `home`, never the real machine's --
    `os.path.expanduser` reads the process's own `$HOME`, which is exactly
    the value a hermetic caller must not depend on."""
    if value == "~":
        return home
    if value.startswith("~" + os.sep):
        return os.path.join(home, value[2:])
    return value


def _git_excludes_file(home):
    """`git config --global --get core.excludesfile`, read with `HOME`
    pointed at `home` and every `GIT_*` var removed -- mirrors
    `audit_lib._foreign_hooks_path`. Falls back to `~/.gitignore_global`
    (this repo's `scripts/setup-git.sh` own default) whenever git has
    nothing to say, rather than an empty string.

    **`GIT_CONFIG_GLOBAL` is the one `GIT_*` var kept, when the real process
    environment sets it to a non-empty value.** `scripts/setup-git.sh`
    inherits it too and both reads and writes THAT file as "the global
    config" -- stripping it here would make this discovery call ask about
    `~/.gitconfig` while `setup-git.sh` acts on a different file entirely,
    misreporting `core.excludesfile` (confirmed by execution:
    a custom `GIT_CONFIG_GLOBAL` pointing at a config whose own
    `core.excludesfile` names a custom ignore file -- `setup-git.sh` changed
    both, and neither had a manifest row to restore from)."""
    env = dict(os.environ)
    keep_global = env.get("GIT_CONFIG_GLOBAL")
    for key in list(env):
        if key.startswith("GIT_") and not (key == "GIT_CONFIG_GLOBAL" and keep_global):
            del env[key]
    env["HOME"] = home
    default = os.path.join(home, ".gitignore_global")
    try:
        proc = subprocess.run(
            ["git", "config", "--global", "--get", "core.excludesfile"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
            env=env, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return default
    if proc.returncode != 0:
        return default
    value = proc.stdout.strip()
    if not value:
        return default
    return _expand_home(value, home)


def _raise_onerror(exc):
    """`os.walk`'s `onerror` callback: re-raise whatever `os.scandir`/
    `os.listdir` raised, rather than `os.walk`'s own default of silently
    skipping the unlistable directory and everything under it.

    Confirmed by execution: `~/.config/claude-code-toolkit/git-hooks` at
    mode 0300 (no read permission, so it cannot be LISTED) made the config
    walk skip it entirely with no error -- `snapshot` exited 0 having never
    recorded `git-hooks/pre-commit` at all, and `scripts/setup-git.sh` then
    replaced that file with no backup to restore it from. Every `os.walk` in
    this module passes this, so that failure mode surfaces as a
    `SnapshotError` (via the `OSError` wrapper below) instead."""
    raise exc


def _walk_config_dir(config_dir):
    """Every path under `config_dir`, top-down, excluding `snapshots/`
    itself at the top level -- the ONE thing this snapshot must never read
    into (it would otherwise nest every past snapshot inside the new one,
    and eventually the one currently being written)."""
    entries = []
    if not os.path.isdir(config_dir):
        return entries
    for base, dirs, files in os.walk(
            config_dir, followlinks=False, onerror=_raise_onerror):
        if base == config_dir:
            dirs[:] = [d for d in dirs if d != "snapshots"]
        for name in sorted(dirs):
            entries.append(os.path.join(base, name))
        for name in sorted(files):
            entries.append(os.path.join(base, name))
    return entries


def _xdg_git_config_dir(home):
    """Where git reads/writes global config when `XDG_CONFIG_HOME` governs
    it: `$XDG_CONFIG_HOME/git` if that variable is set to a non-empty
    absolute path, else `<home>/.config/git` -- git's own default when the
    variable is unset. Confirmed by execution: with `~/.config/git/config`
    present and `~/.gitconfig` absent, `git config --global` WRITES to the
    former, which the fixed Kind-2 list did not cover at all -- a snapshot
    taken just before `scripts/setup-git.sh` runs recorded nothing to
    restore that file from.

    Deliberately reads `os.environ` directly, matching every other
    ambient-git-config read in this module (`_git_excludes_file`) -- there
    is no `environ` parameter on `take_snapshot` to thread this through."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg and os.path.isabs(xdg):
        return os.path.join(xdg, "git")
    return os.path.join(home, ".config", "git")


def _git_config_global_path(home):
    """The custom global git config `GIT_CONFIG_GLOBAL` names, `~` expanded
    against the sandbox `home` -- or `None` when it is unset or empty.
    Deliberately reads `os.environ` directly, matching every other
    ambient-git-config read in this module (`_git_excludes_file`,
    `_xdg_git_config_dir`) -- there is no `environ` parameter on
    `take_snapshot` to thread this through."""
    value = os.environ.get("GIT_CONFIG_GLOBAL")
    if not value:
        return None
    return _expand_home(value, home)


def _fixed_kind2_paths(home, workspace_root):
    paths = [
        os.path.join(home, ".claude", "settings.json"),
        # install.sh's first-run copy, written once when absent; recorded so
        # a restore knows to remove it again.
        os.path.join(home, ".claude", "settings.json.pre-claude-code-toolkit.bak"),
        os.path.join(home, ".claude", "CLAUDE.md"),
        os.path.join(home, ".gitconfig"),
        _git_excludes_file(home),
        os.path.join(_xdg_git_config_dir(home), "config"),
    ]
    # install.sh's per-run timed snapshots, which it prunes to the newest few.
    paths.extend(sorted(glob.glob(os.path.join(
        glob.escape(os.path.join(home, ".claude")),
        "settings.json.claude-code-toolkit-*.bak"))))
    custom_global = _git_config_global_path(home)
    if custom_global:
        paths.append(custom_global)
    if workspace_root:
        paths.append(os.path.join(workspace_root, "CLAUDE.md"))
        paths.append(os.path.join(workspace_root, "TOOLING.md"))
    return paths


def _plugin_json_paths(home):
    plugins_dir = os.path.join(home, ".claude", "plugins")
    if not os.path.isdir(plugins_dir):
        return []
    return [os.path.join(plugins_dir, name)
            for name in sorted(os.listdir(plugins_dir))
            if name.endswith(".json")]


class _Recorder:
    """Builds one snapshot's `manifest.json` and its `files/`/`referents/`
    trees. Every `_add_*` method appends exactly one manifest row."""

    def __init__(self, snap_root, home):
        self.snap_root = snap_root
        self.home = home
        self.files_root = os.path.join(snap_root, "files")
        self.referents_root = os.path.join(snap_root, "referents")
        self.manifest = []

    def _stored_path(self, path):
        return os.path.join(self.files_root, _stored_rel(path, self.home))

    def add_absent(self, path):
        self.manifest.append({"path": path, "type": "absent", "sha256": None,
                              "link_target": None, "referent": None})

    def add_symlink(self, path, capture_referent):
        link_target = os.readlink(path)
        entry = {"path": path, "type": "symlink", "sha256": None,
                 "link_target": link_target, "referent": None}
        dest = self._stored_path(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.lexists(dest):
            os.remove(dest)
        os.symlink(link_target, dest)

        if capture_referent:
            # The referent's path as `readlink` names it, not `realpath`:
            # `realpath` resolves every symlinked ancestor directory too
            # (macOS's own `/var` -> `/private/var` is one), which can spell
            # the same file differently from how the caller's own path
            # strings do. Restoring by `readlink`'s spelling is also simply
            # correct: it is where the SYMLINK says the referent lives.
            ref_path = (link_target if os.path.isabs(link_target)
                       else os.path.normpath(
                           os.path.join(os.path.dirname(path), link_target)))
            # `os.path.isfile`/`isdir` follow the symlink -- true only when
            # the referent exists and is that shape; a missing referent
            # captures nothing (Kind 2's own rule).
            if os.path.isfile(path):
                sha = _sha256_file(path)
                os.makedirs(self.referents_root, exist_ok=True)
                ref_dest = os.path.join(self.referents_root, sha)
                if not os.path.exists(ref_dest):
                    shutil.copy2(path, ref_dest)
                entry["referent"] = {
                    "path": ref_path, "sha256": sha,
                    "stored_as": os.path.join("referents", sha)}
            elif os.path.isdir(path):
                # A symlink to a DIRECTORY (`~/.config/claude-code-toolkit/
                # git-hooks` -> `~/dotfiles/git-hooks` is the confirmed
                # case): `scripts/setup-git.sh`'s `ln -sfn` replaces the
                # link, but whatever install.sh or a hand edit wrote INSIDE
                # the referent -- `git-hooks/pre-commit` -- would otherwise
                # never be backed up at all. Pre-flight the referent's own
                # subtree the same way `add_dir_copy` does (a FIFO inside a
                # referent hangs `copytree` exactly like one inside an
                # ordinary directory), then copy it whole. There is no
                # single sha256 for a directory, so `sha256` stays `None`;
                # `stored_as` is content-addressed on the referent's PATH
                # instead, since two different referents could otherwise
                # collide on an empty/identical tree.
                self._check_subtree_is_safe(path)
                dir_key = hashlib.sha256(ref_path.encode("utf-8")).hexdigest()
                stored_rel = os.path.join("referents", "dir-%s" % dir_key)
                ref_dest = os.path.join(self.snap_root, stored_rel)
                if not os.path.exists(ref_dest):
                    os.makedirs(os.path.dirname(ref_dest), exist_ok=True)
                    shutil.copytree(path, ref_dest, symlinks=True)
                entry["referent"] = {
                    "path": ref_path, "sha256": None, "stored_as": stored_rel}
        self.manifest.append(entry)

    def add_file(self, path):
        sha = _sha256_file(path)
        dest = self._stored_path(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(path, dest)
        self.manifest.append({"path": path, "type": "file", "sha256": sha,
                              "link_target": None, "referent": None})

    def add_dir_copy(self, path):
        """A whole subtree, copied in one shot (Kind 1's four `~/.claude/*`
        dirs, and any real directory that happens to land in a Kind 2
        path) -- ONE manifest row for the top-level entry.

        `shutil.copytree` opens every regular file it finds, so a FIFO
        nested anywhere inside would hang it exactly like a top-level one
        hangs `add_file` -- the whole subtree is checked BEFORE anything is
        copied, never discovered mid-copy."""
        self._check_subtree_is_safe(path)
        dest = self._stored_path(path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(path, dest, symlinks=True)
        self.manifest.append({"path": path, "type": "dir", "sha256": None,
                              "link_target": None, "referent": None})

    @staticmethod
    def _check_subtree_is_safe(root):
        # `os.walk` (default, `followlinks=False` only affects recursion)
        # puts a symlink pointing at a directory in `dirs`, and everything
        # else -- a regular file, a dangling symlink, a symlink to a file,
        # a FIFO -- in `files`; `_entry_kind` then tells a real symlink
        # apart from a genuinely unsafe node in either bucket.
        for base, dirs, files in os.walk(
                root, followlinks=False, onerror=_raise_onerror):
            for name in dirs:
                full = os.path.join(base, name)
                if _entry_kind(full) not in ("dir", "symlink"):
                    _refuse_unsafe(full)
            for name in files:
                full = os.path.join(base, name)
                if _entry_kind(full) not in ("file", "symlink"):
                    _refuse_unsafe(full)

    def add_dir_placeholder(self, path):
        """A directory encountered mid-walk (the `.config/claude-code-toolkit`
        walk) -- its own children get their own rows from that same walk, so
        this only records that the directory itself existed, without a
        redundant second copy of everything under it."""
        dest = self._stored_path(path)
        os.makedirs(dest, exist_ok=True)
        self.manifest.append({"path": path, "type": "dir", "sha256": None,
                              "link_target": None, "referent": None})

    def add_kind2(self, path):
        if not os.path.lexists(path):
            self.add_absent(path)
            return
        kind = _entry_kind(path)
        if kind == "symlink":
            self.add_symlink(path, capture_referent=True)
        elif kind == "dir":
            self.add_dir_copy(path)
        elif kind == "file":
            self.add_file(path)
        else:
            _refuse_unsafe(path)


_RESTORE_MD = """\
# Restoring from this snapshot

Everything here was captured by `claude-setup-audit snapshot` before
install.sh (or a related script) changed anything. Nothing in this directory
is ever modified after the fact -- a later snapshot is a new, separate
directory.

`manifest.json` lists every path this snapshot knows about, one row per path,
with a `type`:

- `absent` -- the path did not exist. To restore: delete it again if
  something has since created it.
- `file` -- a plain file. Its bytes are stored at
  `files/<path relative to $HOME>` (or `files/_abs/<path>` for anything
  outside `$HOME`, such as a repo-side file). Copy that file back over the
  original path.
- `dir` -- a directory. Its contents are stored the same way under `files/`,
  either as a full copy or, for a directory recorded while walking
  `~/.config/claude-code-toolkit/`, as an empty placeholder whose own files
  are separate rows in this same manifest. Copy the whole tree back.
- `symlink` -- a symlink. `link_target` is the exact string `readlink`
  returned; recreate it with `ln -sfn '<link_target>' '<path>'`. Never follow
  it when restoring -- the point is to put the LINK back, not its target.

A symlink row may also carry a `referent`: the file the symlink pointed at,
whose bytes may have changed since (for example `~/.claude/settings.json`
being a symlink to a dotfiles copy that install.sh then rewrites
through the link). Its `path` is where it lived on the machine, and its
bytes are stored at `referent.stored_as` (a sha256-named file under
`referents/`). Restoring it means copying that file back over
`referent.path` -- not over the symlink's own path. A directory referent
(`referent.sha256` is `null`) is restored the same way, by copying the
stored tree at `referent.stored_as` back over `referent.path` whole.
"""


def take_snapshot(home, repo, workspace_root, now=None):
    """See the module docstring for the two kinds of paths this records.
    Returns the new snapshot directory's path.

    `audit_lib.load_manifest`'s `AuditError` propagates -- a snapshot cannot
    know which real files in `~/.local/bin`/`~/bin` are the installer's to
    copy without it, so refusing outright is correct, same as the audit
    report itself."""
    manifest_dests = {row["dest"] for row in audit_lib.load_manifest(repo, home)}

    stamp = now or datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
    snap_root = os.path.join(
        home, ".config", "claude-code-toolkit", "snapshots", stamp)
    try:
        os.makedirs(snap_root, exist_ok=False)
    except FileExistsError as exc:
        raise SnapshotError(
            "a snapshot already exists at %s: %s" % (snap_root, exc))

    # Everything from here on writes ONLY inside `snap_root` (never deletes
    # it, never deletes anything else) -- so an `OSError` partway through
    # (a permission-denied file is the confirmed case) leaves a real, if
    # partial, directory behind rather than removing evidence. It is
    # reported as `SnapshotError` so the CLI's exit-3 contract holds and a
    # caller never sees a raw traceback, but the message says the snapshot
    # is INCOMPLETE rather than claiming success.
    try:
        rec = _Recorder(snap_root, home)

        # Kind 1 -------------------------------------------------------
        for reldir in LINK_DIRECTORIES:
            directory = os.path.join(home, reldir)
            if not os.path.isdir(directory):
                continue
            for name in sorted(os.listdir(directory)):
                full = os.path.join(directory, name)
                kind = _entry_kind(full)
                if kind == "symlink":
                    rec.add_symlink(full, capture_referent=False)
                    continue
                should_copy = (reldir in COPY_REAL_CONTENT_DIRS
                              or full in manifest_dests)
                if not should_copy:
                    continue
                if kind == "dir":
                    rec.add_dir_copy(full)
                elif kind == "file":
                    rec.add_file(full)
                else:
                    _refuse_unsafe(full)

        # Kind 2 ---------------------------------------------------------
        for path in _fixed_kind2_paths(home, workspace_root):
            rec.add_kind2(path)
        for path in _plugin_json_paths(home):
            rec.add_kind2(path)

        config_dir = os.path.join(home, ".config", "claude-code-toolkit")
        for full in _walk_config_dir(config_dir):
            kind = _entry_kind(full)
            if kind == "symlink":
                rec.add_symlink(full, capture_referent=True)
            elif kind == "dir":
                rec.add_dir_placeholder(full)
            elif kind == "file":
                rec.add_file(full)
            else:
                _refuse_unsafe(full)

        with open(os.path.join(snap_root, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(rec.manifest, handle, indent=2)
            handle.write("\n")
        with open(os.path.join(snap_root, "RESTORE.md"), "w", encoding="utf-8") as handle:
            handle.write(_RESTORE_MD)
    except SnapshotError:
        raise
    except OSError as exc:
        # Covers `shutil.Error` too (it subclasses `OSError`) -- the shape
        # `shutil.copytree` raises when one file inside a subtree it is
        # copying cannot be opened (confirmed by execution: a chmod-000
        # file under a real skill directory).
        raise SnapshotError(
            "the snapshot at %s is INCOMPLETE and must not be relied on: %s"
            % (snap_root, exc))

    return snap_root
