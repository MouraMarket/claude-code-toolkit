"""`claude-setup-audit apply-safe` -- removes exactly the files this repo can
prove are byte-identical to the toolkit's own copy, so a subsequent
`install.sh` can symlink them in cleanly, and only ever after a fresh
snapshot of exactly what it is about to remove.

Only `link`-kind manifest rows in state `identical` are ever candidates --
never `theirs_only`, never a hook or a settings key. A snapshot is taken first every single run
(never reused from a past run), and `take_snapshot` failing for any reason --
`SnapshotError` or the `AuditError` it can itself raise via `load_manifest`
-- becomes `ApplyError` with nothing deleted. `build_report`'s own
`AuditError` (the report step, before any snapshot) is NOT caught here: a
report this broken cannot be trusted to say which file is safe to touch, so
it propagates exactly like every other `AuditError` in this CLI.

For each candidate, in this exact order: the `_before_delete` test seam (if
given) runs first; only then is `dest` validated -- a real file, never a
symlink; byte-equal to its repo `target`; and its own sha256 equal to what
the just-taken snapshot's `manifest.json` recorded for that exact path (a
missing row is a failure, same as a mismatched one, since either means the
snapshot cannot back up what is about to be deleted). Nothing runs between a
passing validation and the `os.remove` -- the seam exists so a test can prove
the validation genuinely blocks, not merely that it runs.
"""

import hashlib
import json
import os

import audit_lib
import snapshot_lib


class ApplyError(Exception):
    """apply-safe could not proceed, or a validation failed just before a
    deletion. The CLI maps this to exit 3.

    `removed` (a list, default empty) and `snapshot` (a path, default `None`)
    carry whatever already happened before this error -- a validation
    failure on candidate N leaves N-1 files already gone and a real snapshot
    already sitting on disk, and both must be reported, not just the failing
    path. Every raise site in `apply_safe` below sets them explicitly once
    they exist."""

    def __init__(self, message, removed=None, snapshot=None):
        super().__init__(message)
        self.removed = removed if removed is not None else []
        self.snapshot = snapshot


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_manifest_shas(snapshot_dir):
    """`{path: sha256}` for every `file`-typed row in a snapshot's own
    `manifest.json` -- a path absent from this dict (no row at all, or a row
    for a symlink/dir with `sha256: null`) can never back up a deletion."""
    with open(os.path.join(snapshot_dir, "manifest.json"), encoding="utf-8") as handle:
        manifest = json.load(handle)
    return {entry["path"]: entry["sha256"] for entry in manifest
            if entry.get("sha256") is not None}


def apply_safe(home, repo, workspace_root, environ=None, _before_delete=None):
    """See the module docstring. Returns `{"snapshot": <path>, "removed":
    [<dest>, ...]}`."""
    if environ is None:
        environ = os.environ

    # Step 1: the report. Its own `AuditError` propagates unchanged -- a
    # report this broken cannot even be trusted to name the file this
    # function is about to consider deleting.
    report = audit_lib.build_report(
        home=home, repo=repo, environ=environ, workspace_root=workspace_root)

    # Step 1.5: refuse a linked worktree, before anything is deleted or
    # snapshotted. `install.sh` refuses to run from a linked worktree, so a
    # file removed here
    # would never be re-linked, leaving `~/.local/bin/claude-danger-guard`
    # and friends gone with nothing to put them back.
    if audit_lib.is_linked_worktree(repo):
        raise ApplyError(
            "%s is a linked git worktree -- run apply-safe from the primary "
            "checkout; install.sh refuses a linked worktree, so nothing "
            "removed here would be re-linked" % repo)

    # Step 2: the snapshot. ANY failure here -- `SnapshotError`, or the
    # `AuditError` `take_snapshot` can itself raise via `load_manifest` --
    # becomes `ApplyError`. Nothing is deleted without a fresh snapshot.
    try:
        snap = snapshot_lib.take_snapshot(home, repo, workspace_root)
    except (snapshot_lib.SnapshotError, audit_lib.AuditError) as exc:
        raise ApplyError(
            "could not take a snapshot before applying -- nothing removed: %s"
            % exc)

    manifest_shas = _load_manifest_shas(snap)

    removed = []
    for row in report["links"]:
        if row["kind"] != "link" or row["state"] != "identical":
            continue
        dest = row["dest"]
        target = row["target"]

        if _before_delete is not None:
            _before_delete(dest)

        # Validate, in order: a real file (never a symlink); byte-equal to
        # its repo target; its own sha256 equal to what the snapshot just
        # recorded for this exact path. Nothing runs between a passing
        # validation and the `os.remove` below. Every raise here carries
        # `removed`/`snapshot` so a partial run (N-1 files already gone, a
        # real snapshot already on disk) is never reported as a bare failure.
        #
        # An `OSError` here (a `PermissionError` from `os.remove` in a
        # mode-0555 parent directory is the confirmed case)
        # must NOT escape raw -- uncaught, it skips straight past both the
        # print loop and this function's own return, so a partial run (N-1
        # files already gone) reports NOTHING. The `try` starts after
        # `_before_delete` deliberately: that seam is test-only and must
        # keep raising whatever it raises, unwrapped.
        try:
            if os.path.islink(dest) or not os.path.isfile(dest):
                raise ApplyError(
                    "%s is not a real file -- refusing to remove it" % dest,
                    removed=removed, snapshot=snap)
            with open(dest, "rb") as a, open(target, "rb") as b:
                if a.read() != b.read():
                    raise ApplyError(
                        "%s no longer matches its repo target -- refusing to "
                        "remove it" % dest, removed=removed, snapshot=snap)
            recorded_sha = manifest_shas.get(dest)
            if recorded_sha is None or recorded_sha != _sha256_file(dest):
                raise ApplyError(
                    "%s has no matching sha256 in the snapshot just taken -- "
                    "refusing to remove it" % dest, removed=removed, snapshot=snap)

            os.remove(dest)
        except OSError as exc:
            raise ApplyError(
                "%s could not be validated or removed: %s" % (dest, exc),
                removed=removed, snapshot=snap)
        removed.append(dest)

    return {"snapshot": snap, "removed": removed}
