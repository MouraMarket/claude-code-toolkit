"""History leak gate: prove everything a repository's refs reach is clean.

The tree gate proves one export tree. This gate proves the git repository that
will be pushed: every object any ref can reach, because a push of a ref sends
its whole history, not just its last tree.

It fails closed, like the tree gate, and shares its detectors and exit
contract (leak_gate_lib.py): 0 complete clean coverage, 1 findings, 2 refused.

What one run inspects:

- **Refs**: every ref under `refs/` (branches, tags, notes, stash, anything
  else), HEAD whether attached or detached, and the pseudo-refs a merge,
  rebase or fetch leaves behind. Each ref NAME is scanned, as are symbolic
  ref targets.
- **Objects**: everything reachable from those refs, walked here object by
  object: commits and their parents down to every root commit, trees, blobs,
  annotated tags (including tags of tags), and refs that point straight at a
  tree or a blob. Notes are commits under `refs/notes/`, so their blobs are
  scanned like any other.
- **Paths**: every path that ever existed in any reachable tree. A blob is
  scanned once however many paths hold it, and every path that holds it is
  kept (an alias is never dropped), so a structural finding such as a
  generated file names each path it lived at.
- **Commits and tags**: the whole raw object (message, author, committer,
  tagger, embedded merge tags, signatures). Every author, committer and
  tagger must equal the one approved identity exactly; anything else is an
  identity finding, which no exception can clear.
- **Enumeration is checked against git**: the walked object set must equal
  `git rev-list --objects --all` over the same roots, and the walked ref set
  must equal `git show-ref --head`. A traversal that skipped a ref, a parent
  or a subtree refuses rather than reporting clean.

Refused (2): not a repository, a linked worktree or a repository that has
them, a shallow or partial clone, alternates, grafts, a missing or
mistyped object, an unsupported object format or tree mode, a malformed
commit or tag (a tag with no tagger included), no refs at all, and any disagreement with git's own inventory.
"""

import collections
import hashlib
import os
import re
import subprocess

import leak_gate_lib as lib
from leak_gate_lib import Finding, Refused

# Files in the git directory that name objects without being refs. A push of
# named refs never sends them; they are covered anyway, because a candidate
# that carries one has had a merge, rebase or fetch, and its object is local.
PSEUDO_REFS = ("ORIG_HEAD", "FETCH_HEAD", "MERGE_HEAD", "CHERRY_PICK_HEAD",
               "REVERT_HEAD", "REBASE_HEAD", "BISECT_HEAD", "AUTO_MERGE")

ENTRY_KINDS = {b"100644": "file", b"100755": "executable", b"120000": "symlink",
               b"40000": "tree", b"160000": "gitlink"}
HASH_BYTES = {"sha1": 20, "sha256": 32}

IDENT_HEADERS = (b"author", b"committer", b"tagger")
IDENT = re.compile(rb"(.*) <([^<>]*)> (-?\d+) ([+-]\d{4})\Z", re.S)
APPROVED_IDENT = re.compile(r"[^<>\n]*[^<>\s] <[^<>\s]+>\Z")

LIMITS = tuple(x for x in lib.LIMITS if "history gate" not in x) + (
    "reflogs, the index and unreachable objects are not scanned; a push of "
    "named refs cannot send them",
    "historical symlinks are scanned as text; whether a link resolves inside "
    "the tree is the tree gate's check",
    "repository config, hooks and remote URLs are not scanned; they are never "
    "pushed",
    "what a remote host exposes (pull-request refs, other branches) is the "
    "remote audit's job",
)


def git_env():
    """The environment for every git call: no inherited GIT_* steering.

    An inherited GIT_DIR, GIT_OBJECT_DIRECTORY or GIT_ALTERNATE_OBJECT_DIRECTORIES
    would point the scan at a different object store than the one being
    published; global and system config could add alternate refs or replace
    objects. Replace refs are ignored so every object reads as stored; the
    `refs/replace/` refs themselves are still ordinary refs and still scanned.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", LC_ALL="C")
    return env


def _git(repo, env, *args, ok=(0,)):
    try:
        proc = subprocess.run(["git", "-C", repo] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env)
    except OSError:
        raise Refused("git cannot run here")
    # stderr is never echoed: it can quote a path or ref name.
    if proc.returncode not in ok:
        raise Refused("git %s failed (exit %d)" % (args[0], proc.returncode))
    return proc


class ObjectReader(object):
    """One `git cat-file --batch` process, read object by object."""

    def __init__(self, repo, env):
        try:
            self.proc = subprocess.Popen(
                ["git", "-C", repo, "cat-file", "--batch"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        except OSError:
            raise Refused("git cannot run here")

    def read(self, oid):
        self.proc.stdin.write(oid.encode("ascii") + b"\n")
        self.proc.stdin.flush()
        header = self.proc.stdout.readline()
        parts = header.split()
        if len(parts) != 3 or parts[0].decode("ascii", "replace") != oid:
            raise Refused("object %s is missing or unreadable: the history is incomplete"
                          % oid)
        size = int(parts[2])
        data = self.proc.stdout.read(size)
        if len(data) != size or self.proc.stdout.read(1) != b"\n":
            raise Refused("object %s was cut short" % oid)
        return parts[1].decode("ascii"), data

    def close(self):
        for pipe in (self.proc.stdin, self.proc.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        self.proc.wait()


Ref = collections.namedtuple("Ref", "name oid symref pseudo")


# --------------------------------------------------------------------------
# The repository and its refs
# --------------------------------------------------------------------------


def check_repository(repo, env):
    """(git dir, hex length). Refuses anything whose history is not all local."""
    if not os.path.isdir(repo):
        raise Refused("the repository path is not a directory")
    out = _git(repo, env, "rev-parse", "--path-format=absolute", "--git-dir",
               "--git-common-dir").stdout.split(b"\n")
    git_dir, common = out[0], out[1]
    if os.path.realpath(git_dir) != os.path.realpath(common):
        raise Refused("this is a linked worktree; run against the primary repository")
    listed = _git(repo, env, "worktree", "list", "--porcelain").stdout
    if sum(1 for line in listed.split(b"\n") if line.startswith(b"worktree ")) > 1:
        raise Refused("the repository has linked worktrees, whose own HEADs and refs "
                      "this gate does not enumerate; remove them first")
    if _git(repo, env, "rev-parse", "--is-shallow-repository").stdout.strip() != b"false":
        raise Refused("a shallow clone: its history is incomplete")
    fmt = _git(repo, env, "rev-parse", "--show-object-format").stdout.strip().decode()
    if fmt not in HASH_BYTES:
        raise Refused("unsupported object format")
    objects = os.path.join(git_dir, b"objects")
    for name in (b"info/alternates", b"info/http-alternates"):
        if os.path.lexists(os.path.join(objects, name)):
            raise Refused("the repository borrows objects through alternates; a "
                          "publication repository owns its whole object store")
    if os.path.lexists(os.path.join(git_dir, b"info", b"grafts")):
        raise Refused("the repository has grafts, which rewrite history as walked")
    promisor = _git(repo, env, "config", "--get-regexp",
                    r"^(extensions\.partialclone|remote\..*\.promisor)$", ok=(0, 1)).stdout
    pack_dir = os.path.join(objects, b"pack")
    packs = os.listdir(pack_dir) if os.path.isdir(pack_dir) else []
    if promisor.strip() or any(p.endswith(b".promisor") for p in packs):
        raise Refused("a partial clone: objects it never fetched cannot be scanned")
    return git_dir, HASH_BYTES[fmt] * 2


def list_refs(repo, env, git_dir, hexlen):
    """Every ref, HEAD and pseudo-ref as a Ref. Refuses on anything unreadable."""
    hexref = re.compile(rb"[0-9a-f]{%d}\Z" % hexlen)
    refs = []
    out = _git(repo, env, "for-each-ref",
               "--format=%(objectname)%00%(refname)%00%(symref)").stdout
    for line in out.split(b"\n"):
        if not line:
            continue
        parts = line.split(b"\0")
        if len(parts) != 3 or not hexref.match(parts[0]):
            raise Refused("a ref could not be read")
        refs.append(Ref(parts[1], parts[0].decode(), parts[2], False))

    sym = _git(repo, env, "symbolic-ref", "-q", "HEAD", ok=(0, 1)).stdout.strip()
    head = _git(repo, env, "rev-parse", "-q", "--verify", "HEAD", ok=(0, 1)).stdout.strip()
    if head:
        if not hexref.match(head):
            raise Refused("HEAD could not be read")
        refs.append(Ref(b"HEAD", head.decode(), sym, False))
    elif sym:
        refs.append(Ref(b"HEAD", None, sym, False))  # unborn: its name still counts

    for name in PSEUDO_REFS:
        path = os.path.join(git_dir, name.encode())
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            lines = [ln for ln in fh.read().split(b"\n") if ln.strip()]
        for line in lines:
            token = line.split()[0]
            if hexref.match(token):
                refs.append(Ref(name.encode(), token.decode(), b"", True))
            elif line.startswith(b"ref: "):
                target = line[5:].strip()
                oid = _git(repo, env, "rev-parse", "-q", "--verify", target,
                           ok=(0, 1)).stdout.strip()
                refs.append(Ref(name.encode(), oid.decode() if oid else None, target, True))
            else:
                raise Refused("pseudo-ref %s could not be read" % name)
    return refs


# --------------------------------------------------------------------------
# Walking every reachable object
# --------------------------------------------------------------------------


def parse_tree(oid, data, hashlen):
    """[(mode bytes, name bytes, child oid)] from a raw tree object."""
    entries, pos = [], 0
    while pos < len(data):
        space = data.find(b" ", pos)
        nul = data.find(b"\0", space + 1)
        if space < 0 or nul < 0 or nul + 1 + hashlen > len(data):
            raise Refused("tree %s is malformed" % oid)
        mode, name = data[pos:space], data[space + 1:nul]
        if not name or b"/" in name or name in (b".", b".."):
            raise Refused("tree %s has an invalid entry name" % oid)
        entries.append((mode, name, data[nul + 1:nul + 1 + hashlen].hex()))
        pos = nul + 1 + hashlen
    return entries


def parse_headers(data):
    """[(key, value, start, end)] for each header line, plus continuations folded.

    `start`/`end` are the byte span of the header's first line, so a caller can
    skip exactly that line.
    """
    head = data.split(b"\n\n", 1)[0]
    fields, pos = [], 0
    for line in head.split(b"\n"):
        if line.startswith(b" ") and fields:
            key, value, start, end = fields[-1]
            fields[-1] = (key, value + b"\n" + line[1:], start, end)
        else:
            key, _sep, value = line.partition(b" ")
            fields.append((key, value, pos, pos + len(line)))
        pos += len(line) + 1
    return fields


class Walk(object):
    """The reachable object graph, with every path alias of every blob."""

    def __init__(self, reader, hexlen):
        self.reader = reader
        self.hexlen = hexlen
        self.hexoid = re.compile(r"[0-9a-f]{%d}\Z" % hexlen)
        self.types = {}                # oid -> type, for every object read
        self.expected = []             # (oid, type a pointer claimed for it)
        self.blobs = {}                # blob oid -> {(path, kind)}; empty: no path
        self.paths = {}                # every historical path -> {entry kinds}
        self.gitlinks = []             # paths of submodule entries
        self.odd_modes = []            # (path, mode) of non-standard file modes
        self.commits, self.tags = [], []   # (oid, raw object)
        self._trees = {}
        self._tree_at = set()

    def _oid(self, value, what):
        text = value.decode("ascii", "replace")
        if not self.hexoid.match(text):
            raise Refused("%s carries an invalid object id" % what)
        return text

    def run(self, roots):
        objects = [(oid, None) for oid in sorted(roots)]
        trees = []
        while objects or trees:
            if trees:
                self._tree(*trees.pop(), objects=objects, trees=trees)
                continue
            oid, want = objects.pop()
            if want is not None:
                self.expected.append((oid, want))
            if oid in self.types or oid in self.blobs:
                continue
            if want == "blob":
                self.blobs.setdefault(oid, set())
                continue
            typ, data = self.reader.read(oid)
            if typ == "commit":
                self.types[oid] = typ
                self._commit(oid, data, objects, trees)
            elif typ == "tag":
                self.types[oid] = typ
                self._tag(oid, data, objects)
            elif typ == "tree":
                self.types[oid] = typ
                self._trees[oid] = parse_tree(oid, data, self.hexlen // 2)
                trees.append((oid, b""))
            elif typ == "blob":
                self.blobs.setdefault(oid, set())
            else:
                raise Refused("object %s has an unsupported type" % oid)

    def _commit(self, oid, data, objects, trees):
        fields = parse_headers(data)
        tree = [v for k, v, _s, _e in fields if k == b"tree"]
        parents = [v for k, v, _s, _e in fields if k == b"parent"]
        idents = [k for k, _v, _s, _e in fields if k in (b"author", b"committer")]
        if len(tree) != 1 or sorted(idents) != [b"author", b"committer"]:
            raise Refused("commit %s is malformed" % oid)
        self.commits.append((oid, data))
        tree_oid = self._oid(tree[0], "commit %s" % oid)
        self.expected.append((tree_oid, "tree"))
        trees.append((tree_oid, b""))
        for parent in parents:
            objects.append((self._oid(parent, "commit %s" % oid), "commit"))

    def _tag(self, oid, data, objects):
        fields = dict((k, v) for k, v, _s, _e in parse_headers(data))
        if b"object" not in fields or fields.get(b"type") not in (b"commit", b"tree",
                                                                   b"blob", b"tag"):
            raise Refused("tag %s is malformed" % oid)
        # git itself refuses to create a tag with no tagger ("malformed object",
        # though fsck only warns), and its identity cannot be checked: refuse.
        if b"tagger" not in fields:
            raise Refused("tag %s has no tagger: a malformed tag" % oid)
        self.tags.append((oid, data))
        objects.append((self._oid(fields[b"object"], "tag %s" % oid),
                        fields[b"type"].decode()))

    def _tree(self, oid, prefix, objects, trees):
        # A tree is walked once per path it sits at, so every alias below it is
        # recorded; its entries are parsed once.
        if (oid, prefix) in self._tree_at:
            return
        self._tree_at.add((oid, prefix))
        if oid not in self._trees:
            typ, data = self.reader.read(oid)
            if typ != "tree":
                raise Refused("object %s is not the tree its parent claims" % oid)
            self.types[oid] = typ
            self._trees[oid] = parse_tree(oid, data, self.hexlen // 2)
        for mode, name, child in self._trees[oid]:
            path = prefix + b"/" + name if prefix else name
            kind = ENTRY_KINDS.get(mode)
            if kind is None:
                if not mode.startswith(b"100") or len(mode) != 6:
                    raise Refused("tree %s has an unsupported entry mode" % oid)
                self.odd_modes.append((path, mode))
                kind = "file"
            self.paths.setdefault(path, set()).add(kind)
            if kind == "tree":
                self.expected.append((child, "tree"))
                trees.append((child, path))
            elif kind == "gitlink":
                self.gitlinks.append(path)
            else:
                self.expected.append((child, "blob"))
                self.blobs.setdefault(child, set()).add((path, kind))

    def all_oids(self):
        return set(self.types) | set(self.blobs)


# --------------------------------------------------------------------------
# Scanning what the walk found
# --------------------------------------------------------------------------


def _ident_findings(surface, oid, data, fields, approved):
    out, spans = [], []
    for key, value, start, end in fields:
        if key not in IDENT_HEADERS:
            continue
        spans.append((start, end))
        m = IDENT.match(value)
        if m is None or m.group(1) + b" <" + m.group(2) + b">" != approved:
            out.append(Finding("identity", surface, oid.encode(), start,
                               data.count(b"\n", 0, start) + 1, None,
                               "%s is not the approved identity" % key.decode(),
                               None, None, oid))
    return out, spans


def scan_walk(walk, refs, reader, profile, lists, approved):
    """(findings, bytes scanned) over refs, paths, blobs, commits and tags."""
    findings = []
    names = set()
    for ref in refs:
        names.add(ref.name)
        if ref.symref:
            names.add(ref.symref)
    for name in sorted(names):
        findings += lib.scan_path(name, profile, lists, surface="ref")
        try:
            name.decode("utf-8")
        except UnicodeDecodeError:
            findings.append(Finding("encoding", "ref", name, None, None, None,
                                    "ref name is not valid UTF-8", None))

    for path in sorted(walk.paths):
        findings += lib.scan_path(path, profile, lists)
        if "tree" in walk.paths[path]:
            for cls, detail in lib.structural(lib.Entry(path, "dir", None, None)):
                findings.append(Finding(cls, "history", path, None, None, None, detail, None))
    for path in walk.gitlinks:
        findings.append(Finding("submodule", "history", path, None, None, None,
                                "a submodule entry; its history is not in this repository",
                                None))
    for path, _mode in walk.odd_modes:
        findings.append(Finding("mode", "history", path, None, None, None,
                                "a non-standard file mode", None))

    scanned = 0
    for surface, objects in (("commit", walk.commits), ("tag", walk.tags)):
        for oid, data in objects:
            scanned += len(data)
            fields = parse_headers(data)
            idents, spans = _ident_findings(surface, oid, data, fields, approved)
            findings += idents
            findings += lib.scan_bytes(surface, oid.encode(), data, profile, lists, oid=oid,
                                       person_skip=spans)

    for oid in sorted(walk.blobs):
        typ, data = reader.read(oid)
        if typ != "blob":
            raise Refused("object %s is not the blob its tree claims" % oid)
        walk.types[oid] = typ
        scanned += len(data)
        aliases = sorted(walk.blobs[oid])
        # One content scan per blob, reported at its first path; a blob that
        # only a ref or tag reaches has no path, so its object id stands in.
        where = aliases[0][0] if aliases else oid.encode()
        findings += lib.scan_bytes("content", where, data, profile, lists, oid=oid)
        for path, kind in aliases:
            if kind == "symlink":
                entry = lib.Entry(path, kind, None, data)
            else:
                entry = lib.Entry(path, kind, data, None)
            for cls, detail in lib.structural(entry):
                findings.append(Finding(cls, "history", path, None, None, None, detail,
                                        None, None, oid))
    return findings, scanned


# --------------------------------------------------------------------------
# Agreement with git's own inventory
# --------------------------------------------------------------------------


def check_against_git(repo, env, walk, refs):
    """Refuse unless the walk saw exactly what git says the refs reach."""
    for oid, want in walk.expected:
        have = walk.types.get(oid)
        if have != want:
            raise Refused("object %s is a %s where a %s was claimed"
                          % (oid, have or "missing object", want))

    roots = sorted({r.oid for r in refs if r.pseudo and r.oid})
    listed = _git(repo, env, "rev-list", "--objects", "--no-object-names", "--all",
                  *roots).stdout.split()
    git_objects = {o.decode("ascii") for o in listed}
    mine = walk.all_oids()
    if git_objects != mine:
        raise Refused("the walk saw %d objects and git lists %d (%d only in git, %d only "
                      "in the walk): enumeration is incomplete"
                      % (len(mine), len(git_objects), len(git_objects - mine),
                         len(mine - git_objects)))

    shown = _git(repo, env, "show-ref", "--head", ok=(0, 1)).stdout
    git_refs = {line.split(b" ", 1)[1] for line in shown.split(b"\n") if line}
    my_refs = {r.name for r in refs if not r.pseudo and r.oid}
    if git_refs != my_refs:
        raise Refused("the walk saw %d refs and git lists %d: enumeration is incomplete"
                      % (len(my_refs), len(git_refs)))
    return len(git_objects), len(git_refs)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def parse_identity(identity):
    if not isinstance(identity, str) or not APPROVED_IDENT.match(identity):
        raise Refused('the approved identity must read "Name <email>"')
    return identity.encode("utf-8")


def run_history(repo, profile_path, identity, exceptions_path=None, all_refs=True,
                names_path=lib.DEFAULT_NAMES, words_path=lib.DEFAULT_WORDS,
                receipt_path=None):
    """Run the history gate. Raises Refused.

    Returns (exit code, findings, report lines, summary), as run_tree does.
    """
    if not all_refs:
        raise Refused("only --all-refs is implemented; a scan of some refs is not a "
                      "clean claim about a repository")
    approved = parse_identity(identity)
    if not os.path.isdir(repo):
        raise Refused("the repository path is not a directory")
    repo_real = os.path.realpath(os.fsencode(repo))
    for what, path in (("profile", profile_path), ("exceptions file", exceptions_path),
                       ("receipt", receipt_path)):
        if path is not None and lib._inside(path, repo_real):
            raise Refused("the %s is inside the repository being scanned" % what)

    profile = lib.load_profile(profile_path)
    try:
        return _run(os.fsdecode(repo_real), profile, approved, exceptions_path,
                    names_path, words_path)
    except Refused as exc:
        raise Refused(lib.scrub(str(exc), profile, None))


def _run(repo, profile, approved, exceptions_path, names_path, words_path):
    exceptions = lib.load_exceptions(exceptions_path) if exceptions_path else {}
    lists = lib.load_name_lists(names_path, words_path)
    env = git_env()

    git_dir, hexlen = check_repository(repo, env)
    refs = list_refs(repo, env, git_dir, hexlen)
    roots = {r.oid for r in refs if r.oid}
    if not roots:
        raise Refused("the repository has no refs; an empty scope proves nothing")

    reader = ObjectReader(repo, env)
    try:
        walk = Walk(reader, hexlen)
        walk.run(roots)
        findings, scanned = scan_walk(walk, refs, reader, profile, lists, approved)
    finally:
        reader.close()
    git_objects, git_refs = check_against_git(repo, env, walk, refs)

    kept, used = lib.apply_exceptions(findings, exceptions)
    kept.sort(key=lib._sort_key)
    by_class = collections.Counter(f.cls for f in kept)
    ref_lines = sorted(b"%s %s" % (r.name, (r.oid or "-").encode()) for r in refs)
    types = collections.Counter(walk.types.values())
    summary = {
        "gate": "history",
        "schema_version": lib.SCHEMA_VERSION,
        "exit": 1 if kept else 0,
        "profile_sha256": profile.sha256,
        "profile_terms": len(profile.terms),
        "identity_sha256": hashlib.sha256(approved).hexdigest(),
        "refs": len(refs),
        "refs_sha256": hashlib.sha256(b"\n".join(ref_lines)).hexdigest(),
        "commits": types["commit"],
        "trees": types["tree"],
        "blobs": types["blob"],
        "tags": types["tag"],
        "paths": len(walk.paths),
        "path_aliases": sum(len(a) for a in walk.blobs.values()),
        "bytes": scanned,
        "git_objects": git_objects,
        "git_refs": git_refs,
        "findings_by_class": dict(sorted(by_class.items())),
        "exceptions_applied": len(used),
        "findings": [{k: lib.scrub(v, profile, lists) if isinstance(v, str) else v
                      for k, v in lib.receipt_finding(f, profile, lists).items()}
                     for f in kept],
        "limits": list(LIMITS),
    }
    lines = [lib.scrub(lib.format_finding(f, profile, lists), profile, lists) for f in kept]
    return summary["exit"], kept, lines, summary
