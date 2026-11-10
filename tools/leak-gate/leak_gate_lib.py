"""Tree leak gate: prove an export tree carries none of a private profile's terms.

Pure functions over a directory on disk. Nothing here touches git, the network,
or any file outside the three inputs it is handed: the tree root, a private term
profile, and an expected inventory (plus an optional exceptions file).

THIS GATE FAILS CLOSED. Most guards in this toolkit allow what they do not
recognise, because a guard that misfires on ordinary work gets disabled. Here
the asymmetry runs the other way: a leaked identifier is permanent once pushed,
and a false refusal costs one exact exception or one re-authored file. So
anything this module cannot positively prove clean is a finding or a refusal,
never a pass.

Exit contract (the CLI maps these):

    0  complete clean coverage
    1  findings (blocked)
    2  refused: missing or invalid input, an empty scope, an unreadable entry,
       a stale exception, or a detector that cannot run

What one run inspects:

- **Paths** of every file, link and directory, including generated and
  untracked ones. A path finding is reported once, on the shortest entry that
  contains it, so one bad directory name is one finding.
- **Content** of every regular file as raw bytes, never decoded first, so a
  term inside binary or invalid UTF-8 data is still seen.
- **Symlink text**, and whether the link resolves inside the tree. A link is
  never followed while walking.
- **The inventory**: every file, executable bit, link target and content hash
  must match the expected inventory exactly. Anything extra is unexpected.
- **Opaque content**: a file that is not UTF-8 text, or that carries a long
  base64-like run, is reported, because compression and encoding hide terms
  from any byte scan. It needs an exact exception to pass.
- **Named people**: "Given Surname" pairs whose given name is in the system
  given-name list, so a name that no profile term knows is still flagged.
- **Credentials**: vendor key shapes (`SECRET_RULES`) in paths, content and
  link text, so a key that is no profile term is still caught.

The history gate (`history_lib.py`) reuses these detectors on every ref name,
historical path, blob, commit and tag.

What it does NOT establish, stated so nobody reads a 0 as more than it is:

- The profile is finite. An identifier nobody put in it is invisible to the
  term scan.
- The person detector cannot see a given name that is also an ordinary word
  (Rose, Grant), a single name, or a non-ASCII one. Its hits need human review.
- The top-level `.git` is skipped. History, refs, messages and identities are
  the history gate's job, not this one.
"""

import collections
import hashlib
import json
import os
import re
import stat
import unicodedata

SCHEMA_VERSION = 1

# Boundary classes, ASCII-only and on raw bytes, matching how the profile was
# built: a term is bounded by the byte next to it, never by a decode.
_ALNUM = b"[A-Za-z0-9]"
_ALNUM_US = b"[A-Za-z0-9_]"

# mode -> (edge class, or None for no boundary; case-insensitive?)
MODES = {
    "substring": (None, True),
    "word": (_ALNUM, True),
    "token": (_ALNUM, False),
    "ident": (_ALNUM_US, False),
}

# Only content findings take an exception. A structural finding (an unexpected
# file, an escaping link, a nested repository, a wrong identity) is fixed at the
# source, not excused.
EXCEPTABLE = frozenset(("term", "person", "opaque", "encoded", "secret"))

# Credential shapes, matched on raw bytes, so a key inside binary data is still
# seen. Each is a vendor's documented key prefix, never an entropy guess: a
# guess would flag every hash and UUID. Unlike a pre-commit scanner, this table
# has no inline allow marker and no placeholder skip. A line carrying the marker
# would pass a real key, so a documented sample key needs an exact exception.
SECRET_RULES = tuple((name, re.compile(pattern)) for name, pattern in (
    ("anthropic-key", rb"sk-ant-[a-z]{3,10}\d{0,2}-[A-Za-z0-9_-]{20,}"),
    ("private-key-block",
     rb"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"),
    ("google-api-key", rb"AIza[0-9A-Za-z_-]{35}"),
    ("google-oauth-secret", rb"GOCSPX-[A-Za-z0-9_-]{20,}"),
    ("github-token", rb"gh[pousr]_[A-Za-z0-9]{36,}"),
    ("github-pat", rb"github_pat_[A-Za-z0-9_]{50,}"),
    ("gitlab-token", rb"glpat-[A-Za-z0-9_-]{20,}"),
    ("slack-token", rb"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("slack-app-token", rb"xapp-\d-[A-Za-z0-9-]{10,}"),
    ("aws-access-key", rb"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    ("clickup-token", rb"pk_\d{4,}_[A-Z0-9]{32}"),
    ("stripe-key", rb"[sr]k_live_[A-Za-z0-9]{20,}"),
    ("npm-token", rb"npm_[A-Za-z0-9]{36}"),
    ("openai-key", rb"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}"),
    # Last and narrow: a bare `sk-` key with no vendor prefix, 40+ characters
    # so it stays off ordinary hyphenated identifiers.
    ("generic-sk-key", rb"(?<![A-Za-z0-9-])sk-[A-Za-z0-9]{40,}"),
))
SECRET_RULE_NAMES = frozenset(name for name, _pattern in SECRET_RULES)

GENERATED_NAMES = frozenset(
    (b".DS_Store", b"__pycache__", b".pytest_cache", b".mypy_cache", b"node_modules")
)
GENERATED_SUFFIXES = (b".pyc", b".pyo", b".swp")

LFS_POINTER = b"version https://git-lfs.github.com/spec/"
ENCODED_RUN = re.compile(rb"[A-Za-z0-9+/]{200,}={0,2}")

# Run on decoded text. The surname may carry apostrophes, hyphens and
# non-ASCII letters (O'Quux, Smith-Jones, Nunez with a tilde); the given name is
# ASCII because the list it is checked against is.
PERSON = re.compile(r"(?<![\w])([A-Z][a-z]+) ([^\W\d_][^\W\d_]*(?:['\u2019-][^\W\d_]+)*)(?![\w])")
DEFAULT_NAMES = "/usr/share/dict/propernames"
DEFAULT_WORDS = "/usr/share/dict/words"
# "Claude" is in the given-name list and is also the product this toolkit is
# for. Only "Claude <product word>" is skipped; "Claude <any other surname>" is
# still flagged, so a real person called Claude is not exempt.
PRODUCT_NAME = "Claude"
PRODUCT_WORDS = frozenset((
    "Code", "Desktop", "Agent", "Opus", "Sonnet", "Haiku", "Fable", "Max", "Pro",
    "Team", "Enterprise", "Skills", "Projects", "Artifacts", "Docs", "Design",
    "Cowork", "GitHub",
))

LIMITS = (
    "the term profile is finite: an identifier it does not list is not detected",
    "the person detector misses given names that are ordinary words, single "
    "names and non-ASCII names; its hits need human review",
    "base64-like runs shorter than 200 characters are not flagged as encoded",
    "credential detection covers the listed vendor key shapes only; a secret "
    "with no documented prefix is not detected",
    "git history, refs, messages and identities are out of scope (history gate)",
)


class Refused(Exception):
    """Coverage cannot be established. Exit 2; never downgrade to a warning."""


Term = collections.namedtuple("Term", "index value mode kind patterns needle")
Profile = collections.namedtuple("Profile", "sha256 terms")
NameLists = collections.namedtuple("NameLists", "names common")
# kind: file | executable | symlink | dir | special
Entry = collections.namedtuple("Entry", "path kind data target mode", defaults=(None,))
# rule: the credential rule of a "secret" finding. oid: the git object a
# history finding was read from.
Finding = collections.namedtuple(
    "Finding", "cls surface path offset line term detail sha256 rule oid",
    defaults=(None, None),
)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def _strict_pairs(pairs):
    # json.loads keeps the LAST of two duplicate keys, silently. In an inventory
    # that drops an expectation; refuse instead. The key is not printed: an
    # inventory key is a path, and a path can be the very thing being hunted.
    out = {}
    for key, value in pairs:
        if key in out:
            raise Refused("duplicate JSON key")
        out[key] = value
    return out


def _read_json(path, what):
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise Refused("cannot read the %s: %s" % (what, exc.strerror))
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_strict_pairs)
    except (UnicodeDecodeError, ValueError):
        raise Refused("the %s is not valid UTF-8 JSON" % what)
    return raw, data


def _alternatives(char, fold):
    # Bytes IGNORECASE folds ASCII only, so a non-ASCII letter carries its own
    # case variants as an alternation.
    forms = {char}
    if fold and not char.isascii():
        forms |= {char.lower(), char.upper()}
    encoded = sorted({f.encode("utf-8") for f in forms}, key=lambda b: (-len(b), b))
    if len(encoded) == 1:
        return re.escape(encoded[0])
    return b"(?:" + b"|".join(re.escape(e) for e in encoded) + b")"


def compile_term(index, value, mode, kind):
    """One term, compiled for both Unicode normal forms.

    macOS can hand back a decomposed (NFD) spelling of an accented name that a
    composed (NFC) profile term would never match byte for byte.
    """
    edge, fold = MODES[mode]
    patterns = []
    forms = {unicodedata.normalize("NFC", value), unicodedata.normalize("NFD", value)}
    for form in sorted(forms):
        body = b"".join(_alternatives(c, fold) for c in form)
        if edge is not None:
            body = b"(?<!" + edge + b")" + body + b"(?!" + edge + b")"
        patterns.append(re.compile(body, re.IGNORECASE if fold else 0))
    # A cheap prefilter, valid for every mode because it is a superset test.
    needle = value.lower().encode("ascii") if value.isascii() else None
    return Term(index, value, mode, kind, tuple(patterns), needle)


def load_profile(path):
    """Load and validate the private profile. Never prints a term value."""
    raw, data = _read_json(path, "profile")
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise Refused("the profile is not schema_version %d" % SCHEMA_VERSION)
    declared = data.get("modes")
    if not isinstance(declared, dict) or not declared:
        raise Refused("the profile declares no match modes")
    unknown = sorted(str(m) for m in declared if m not in MODES)
    if unknown:
        raise Refused(
            "the profile declares match mode(s) this gate does not implement: %s"
            % ", ".join(unknown)
        )
    terms = data.get("terms")
    if not isinstance(terms, list) or not terms:
        raise Refused("the profile has no terms; an empty profile proves nothing")
    out = []
    for index, item in enumerate(terms, 1):
        if not isinstance(item, dict):
            raise Refused("profile term #%d is not an object" % index)
        value, mode, kind = item.get("term"), item.get("mode"), item.get("kind", "")
        if not isinstance(value, str) or not value.strip():
            raise Refused("profile term #%d is empty; it would match everywhere" % index)
        if not isinstance(mode, str) or mode not in declared:
            raise Refused("profile term #%d uses a mode the profile does not declare" % index)
        if not isinstance(kind, str):
            raise Refused("profile term #%d has a non-string kind" % index)
        out.append(compile_term(index, value, mode, kind))
    return Profile(hashlib.sha256(raw).hexdigest(), tuple(out))


def load_name_lists(names_path=DEFAULT_NAMES, words_path=DEFAULT_WORDS):
    """(given names, lowercase ordinary words) for the person detector.

    The words list marks a proper noun with a capital, so only a LOWERCASE
    entry means an ordinary word that happens to be a name (grant, rose).
    A missing list refuses: a detector that cannot run must not read as clean.
    """
    def read(path, what):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = [line.strip() for line in fh if line.strip()]
        except OSError as exc:
            raise Refused("the person detector cannot run without its %s list: %s"
                          % (what, exc.strerror))
        if not lines:
            raise Refused("the person detector's %s list is empty" % what)
        return lines

    names = set(read(names_path, "given-name"))
    common = {w for w in read(words_path, "word") if w.islower()}
    return NameLists(frozenset(names), frozenset(common))


_KINDS = ("file", "executable", "symlink")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_MODE = re.compile(r"[0-7]{4}")


def _valid_rel(path):
    parts = path.split("/")
    return (
        not path.startswith("/")
        and all(p not in ("", ".", "..") for p in parts)
        and ".git" not in parts
    )


def load_inventory(path):
    """{path bytes: record} the tree must match exactly, plus the file's sha256."""
    raw, data = _read_json(path, "inventory")
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise Refused("the inventory is not schema_version %d" % SCHEMA_VERSION)
    entries = data.get("entries")
    if not isinstance(entries, dict) or not entries:
        raise Refused("the inventory lists no entries; an empty scope proves nothing")
    out = {}
    for number, (rel, rec) in enumerate(sorted(entries.items()), 1):
        if not isinstance(rel, str) or not _valid_rel(rel):
            raise Refused("inventory entry %d has an invalid relative path" % number)
        if not isinstance(rec, dict) or rec.get("kind") not in _KINDS:
            raise Refused("inventory entry %d has no valid kind" % number)
        if rec["kind"] == "symlink":
            ok = set(rec) == {"kind", "target"} and isinstance(rec["target"], str)
        else:
            ok = set(rec) == {"kind", "mode", "sha256"} and isinstance(rec["sha256"], str) \
                and _HEX64.fullmatch(rec["sha256"]) is not None \
                and isinstance(rec["mode"], str) and _MODE.fullmatch(rec["mode"]) is not None
        if not ok:
            raise Refused("inventory entry %d has missing or unexpected fields" % number)
        out[os.fsencode(rel)] = rec
    return out, hashlib.sha256(raw).hexdigest()


_EXCEPTION_KEYS = {"class", "surface", "path", "sha256", "offset", "term", "mode", "reason"}
# Tree surfaces, then the history gate's: a ref name, a raw commit or tag object.
EXCEPTION_SURFACES = ("path", "content", "link", "ref", "commit", "tag")


def load_exceptions(path):
    """Exact-scope exceptions: {key: exception number}.

    Every field is required, so an exception can only ever name one finding:
    its class, surface, path, the sha256 of that surface's bytes, the byte
    offset, and (for a term) the term and mode. There is no way to write a
    global or per-word allowance, by construction.
    """
    _raw, data = _read_json(path, "exceptions file")
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise Refused("the exceptions file is not schema_version %d" % SCHEMA_VERSION)
    items = data.get("exceptions")
    if not isinstance(items, list):
        raise Refused("the exceptions file has no exceptions list")
    out = {}
    for number, item in enumerate(items, 1):
        if not isinstance(item, dict) or set(item) != _EXCEPTION_KEYS:
            raise Refused("exception %d must carry exactly: %s"
                          % (number, ", ".join(sorted(_EXCEPTION_KEYS))))
        cls, surface = item["class"], item["surface"]
        if cls not in EXCEPTABLE:
            raise Refused("exception %d names a class that cannot be excepted" % number)
        if surface not in EXCEPTION_SURFACES:
            raise Refused("exception %d has an invalid surface" % number)
        if not isinstance(item["path"], str) or not _valid_rel(item["path"]):
            raise Refused("exception %d has an invalid path" % number)
        if not isinstance(item["sha256"], str) or not _HEX64.fullmatch(item["sha256"]):
            raise Refused("exception %d has no exact sha256" % number)
        if not isinstance(item["reason"], str) or not item["reason"].strip():
            raise Refused("exception %d has no reason" % number)
        if cls == "opaque":
            if item["offset"] is not None or item["term"] is not None or item["mode"] is not None:
                raise Refused("exception %d: an opaque exception takes no offset or term" % number)
        else:
            offset = item["offset"]
            if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
                raise Refused("exception %d has no exact byte offset" % number)
        if cls == "term":
            if not isinstance(item["term"], str) or not item["term"] or item["mode"] not in MODES:
                raise Refused("exception %d: a term exception names its term and mode" % number)
        elif cls == "secret":
            # The rule name stands in the term field, so one exception still
            # names one match: this rule, at this offset, in these bytes.
            if item["term"] not in SECRET_RULE_NAMES or item["mode"] is not None:
                raise Refused("exception %d: a secret exception names its rule as the term, "
                              "with no mode" % number)
        elif item["term"] is not None or item["mode"] is not None:
            raise Refused("exception %d: only a term or secret exception names a term" % number)
        key = (cls, surface, item["path"], item["sha256"], item["offset"],
               item["term"], item["mode"])
        if key in out:
            raise Refused("exceptions %d and %d are identical" % (out[key], number))
        out[key] = number
    return out


# --------------------------------------------------------------------------
# Walking the tree
# --------------------------------------------------------------------------


def walk(root, redact_path):
    """Every entry under `root`, never following a link.

    Returns (entries, empty_dirs). The top-level `.git` is skipped (history is
    another gate's surface); a nested one is returned but not descended into.
    `redact_path` keeps a refusal message from printing an identifier.
    """
    root_b = os.fsencode(root)
    entries, empty_dirs, stack = [], set(), [b""]
    while stack:
        rel = stack.pop()
        full = os.path.join(root_b, rel) if rel else root_b
        try:
            names = sorted(os.listdir(full))
        except OSError as exc:
            raise Refused("cannot list %s: %s" % (redact_path(rel) or ".", exc.strerror))
        if rel and not names:
            empty_dirs.add(rel)
        for name in names:
            if not rel and name == b".git":
                continue
            child_rel = rel + b"/" + name if rel else name
            child = os.path.join(root_b, child_rel)
            try:
                st = os.lstat(child)
                if stat.S_ISLNK(st.st_mode):
                    entries.append(Entry(child_rel, "symlink", None, os.readlink(child)))
                elif stat.S_ISDIR(st.st_mode):
                    entries.append(Entry(child_rel, "dir", None, None))
                    if name != b".git":
                        stack.append(child_rel)
                elif stat.S_ISREG(st.st_mode):
                    with open(child, "rb") as fh:
                        data = fh.read()
                    # Any execute bit makes it executable; the exact
                    # permission bits are compared separately, so 0654 is not 0644.
                    kind = "executable" if st.st_mode & 0o111 else "file"
                    entries.append(Entry(child_rel, kind, data, None,
                                         stat.S_IMODE(st.st_mode)))
                else:
                    entries.append(Entry(child_rel, "special", None, None))
            except OSError as exc:
                raise Refused("cannot read %s: %s" % (redact_path(child_rel), exc.strerror))
    entries.sort(key=lambda e: e.path)
    return entries, empty_dirs


def record(entry):
    if entry.kind == "symlink":
        return {"kind": "symlink", "target": os.fsdecode(entry.target)}
    return {"kind": entry.kind, "mode": "%04o" % entry.mode,
            "sha256": hashlib.sha256(entry.data).hexdigest()}


def make_inventory(root):
    """The inventory of `root` as it stands, for a tree a human has reviewed.

    An inventory generated from the tree it then checks cannot catch an
    unexpected file in that tree. Its value is as a frozen expectation for the
    NEXT run, or when produced independently by the exporter.
    """
    entries, _empty = walk(root, lambda p: "<entry>")
    listed = {os.fsdecode(e.path): record(e) for e in entries if e.kind in _KINDS}
    if not listed:
        raise Refused("the tree has no files or links")
    return {"schema_version": SCHEMA_VERSION, "entries": dict(sorted(listed.items()))}


def tree_digest(entries):
    h = hashlib.sha256()
    for e in entries:
        if e.kind in ("file", "executable"):
            tail = hashlib.sha256(e.data).hexdigest().encode()
        elif e.kind == "symlink":
            tail = e.target
        else:
            tail = b""
        h.update(e.path + b"\0" + e.kind.encode() + b"\0" + tail + b"\n")
    return h.hexdigest()


# --------------------------------------------------------------------------
# Detectors
# --------------------------------------------------------------------------


def term_hits(data, terms):
    """(term, start, end) for every occurrence of every term in raw `data`."""
    lowered = data.lower()
    for term in terms:
        if term.needle is not None and term.needle not in lowered:
            continue
        seen = set()
        for pattern in term.patterns:
            for m in pattern.finditer(data):
                if m.start() not in seen:
                    seen.add(m.start())
                    yield term, m.start(), m.end()


def secret_hits(data):
    """(rule name, start, end) for every credential-shaped run in raw `data`."""
    for name, pattern in SECRET_RULES:
        for m in pattern.finditer(data):
            yield name, m.start(), m.end()


def person_hits(data, lists):
    """(start, end) byte offsets of each "Given Surname" with a listed given name.

    Only called on valid UTF-8 (text content, paths, link text); anything else
    is opaque and reported as such.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return
    for m in PERSON.finditer(text):
        first, last = m.group(1), m.group(2)
        if not last[0].isupper():
            continue
        if len(last) > 1 and last.isupper():  # an acronym (PR, API), not a surname
            continue
        if first not in lists.names or first.lower() in lists.common:
            continue
        bare = re.sub(r"['\u2019]s$", "", last)  # possessive: "Code's"
        if first == PRODUCT_NAME and bare in PRODUCT_WORDS:
            continue
        start = len(text[:m.start()].encode("utf-8"))
        yield start, start + len(m.group(0).encode("utf-8"))


def is_text(data):
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def link_problem(root_real, rel, target, link_root_real=None):
    """None, or a reason this link does not resolve inside the tree.

    Escape is always judged against `root_real`. A link root only moves the
    dangling check: the tree may hold a link to a file that lives elsewhere in
    the published repo, so the target is looked for there instead.
    """
    if target.startswith(b"/"):
        return "escape", "absolute link target"
    depth = rel.count(b"/")
    for part in target.split(b"/"):
        if part in (b"", b"."):
            continue
        depth = depth - 1 if part == b".." else depth + 1
        if depth < 0:
            return "escape", "link target climbs above the tree root"
    resolved = os.path.realpath(os.path.join(root_real, rel))
    if resolved != root_real and not resolved.startswith(root_real + b"/"):
        return "escape", "link resolves outside the tree"
    if link_root_real is not None:
        there = os.path.realpath(os.path.join(link_root_real, os.path.dirname(rel), target))
        if there != link_root_real and not there.startswith(link_root_real + b"/"):
            return "escape", "link resolves outside the link root"
        if not os.path.exists(there):
            return "dangling", "link target does not exist in the link root"
        return None
    if not os.path.exists(resolved):
        return "dangling", "link target does not exist"
    return None


def structural(entry):
    """(class, detail) problems of an entry's shape, independent of terms."""
    out = []
    base = entry.path.rsplit(b"/", 1)[-1]
    if base in GENERATED_NAMES or base.endswith(GENERATED_SUFFIXES):
        out.append(("generated", "generated artifact; it never ships"))
    if base == b".git":
        out.append(("nested-repo", "a nested repository or submodule checkout"))
    if base == b".gitmodules":
        out.append(("submodule", "submodules are not supported"))
    if entry.kind == "special":
        out.append(("special", "not a regular file, directory or symlink"))
    if entry.mode is not None and entry.mode & 0o7000:
        out.append(("mode", "setuid, setgid or sticky bit set"))
    if entry.data is not None:
        if entry.data.startswith(LFS_POINTER):
            out.append(("lfs", "a Git LFS pointer; the payload is not in the tree"))
        if base == b".gitattributes" and b"filter=lfs" in entry.data:
            out.append(("lfs", "routes files through Git LFS"))
    try:
        entry.path.decode("utf-8")
    except UnicodeDecodeError:
        out.append(("encoding", "path is not valid UTF-8"))
    if entry.target is not None:
        try:
            entry.target.decode("utf-8")
        except UnicodeDecodeError:
            out.append(("encoding", "link target is not valid UTF-8"))
    return out


def compare_inventory(entries, empty_dirs, inventory):
    """(class, path, detail) for every difference from the expected inventory."""
    disk = {e.path: e for e in entries if e.kind in _KINDS}
    out = []
    for path in sorted(set(inventory) - set(disk)):
        out.append(("missing", path, "inventoried but absent from the tree"))
    for path in sorted(set(disk) - set(inventory)):
        out.append(("unexpected", path, "in the tree but not in the inventory"))
    for path in sorted(empty_dirs):
        out.append(("unexpected", path, "empty directory; git cannot carry it"))
    for path in sorted(set(disk) & set(inventory)):
        want, have = inventory[path], record(disk[path])
        if want["kind"] != have["kind"]:
            out.append(("kind-mismatch", path,
                        "inventory says %s, tree has %s" % (want["kind"], have["kind"])))
        elif want != have:
            if have["kind"] == "symlink":
                what = "link target"
            elif want["sha256"] != have["sha256"]:
                what = "content sha256"
            else:
                what = "permission mode"
            out.append(("content-mismatch", path, "%s differs from the inventory" % what))
    return out


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def _inside(path, root_real):
    real = os.path.realpath(os.fsencode(path))
    return real == root_real or real.startswith(root_real + b"/")


def _line(data, offset):
    return data.count(b"\n", 0, offset) + 1


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def scan_path(path, profile, lists, surface="path"):
    """Term, person and secret findings in one path or ref name.

    A path hit is reported on the shortest entry containing it: a hit wholly
    inside the parent is the parent directory's own finding. A ref name is one
    string, so it passes surface="ref" and has no parent to defer to.
    """
    findings = []
    if surface == "path" and b"/" in path:
        floor = len(path.rsplit(b"/", 1)[0])
    else:
        floor = -1
    sha = _sha(path)
    for term, start, end in term_hits(path, profile.terms):
        if end > floor:
            findings.append(Finding("term", surface, path, start, None, term, "", sha))
    for start, end in person_hits(path, lists):
        if end > floor:
            findings.append(Finding("person", surface, path, start, None, None, "", sha))
    for rule, start, end in secret_hits(path):
        if end > floor:
            findings.append(Finding("secret", surface, path, start, None, None, "", sha,
                                    rule))
    return findings


def scan_bytes(surface, path, data, profile, lists, oid=None, person_skip=()):
    """Term, secret, person, encoded and opaque findings in one run of bytes.

    `person_skip` is a list of (start, end) spans the person detector ignores:
    the history gate checks commit identities exactly, by its own rule.
    """
    findings = []
    sha = _sha(data)
    for term, start, _end in term_hits(data, profile.terms):
        findings.append(Finding("term", surface, path, start, _line(data, start), term,
                                "", sha, None, oid))
    for rule, start, _end in secret_hits(data):
        findings.append(Finding("secret", surface, path, start, _line(data, start), None,
                                "", sha, rule, oid))
    if is_text(data):
        for start, end in person_hits(data, lists):
            if any(s <= start and end <= e for s, e in person_skip):
                continue
            findings.append(Finding("person", surface, path, start, _line(data, start),
                                    None, "", sha, None, oid))
        for m in ENCODED_RUN.finditer(data):
            findings.append(Finding(
                "encoded", surface, path, m.start(), _line(data, m.start()), None,
                "base64-like run of %d characters" % (m.end() - m.start()), sha, None, oid))
    elif surface != "link":
        findings.append(Finding(
            "opaque", surface, path, None, None, None,
            "not UTF-8 text: no byte scan proves compressed or encoded content clean",
            sha, None, oid))
    return findings


def scan(entries, profile, lists):
    """Every term, person, secret, opaque and encoded finding in a tree."""
    findings = []
    for entry in entries:
        findings += scan_path(entry.path, profile, lists)
        if entry.kind == "symlink":
            findings += scan_bytes("link", entry.path, entry.target, profile, lists)
        elif entry.data is not None:
            findings += scan_bytes("content", entry.path, entry.data, profile, lists)
    return findings


def _structural_findings(entries, empty_dirs, inventory, root_real, link_root_real=None):
    out = []
    for entry in entries:
        for cls, detail in structural(entry):
            out.append(Finding(cls, "tree", entry.path, None, None, None, detail, None))
        if entry.kind == "symlink":
            problem = link_problem(root_real, entry.path, entry.target, link_root_real)
            if problem:
                cls, detail = problem
                out.append(Finding("symlink-" + cls, "tree", entry.path, None, None,
                                   None, detail, None))
    for cls, path, detail in compare_inventory(entries, empty_dirs, inventory):
        out.append(Finding(cls, "tree", path, None, None, None, detail, None))
    return out


def exception_key(finding):
    term = finding.term.value if finding.term else finding.rule
    mode = finding.term.mode if finding.term else None
    return (finding.cls, finding.surface, os.fsdecode(finding.path), finding.sha256,
            finding.offset, term, mode)


def apply_exceptions(findings, exceptions):
    """(kept findings, exception numbers used). Raises Refused on a stale one."""
    used, kept = set(), []
    for finding in findings:
        number = exceptions.get(exception_key(finding)) if finding.cls in EXCEPTABLE else None
        if number is not None:
            used.add(number)
        else:
            kept.append(finding)
    stale = sorted(set(exceptions.values()) - used)
    if stale:
        raise Refused(
            "exception(s) %s matched no finding: the file, its offset or the profile "
            "changed, so each needs a fresh review" % ", ".join(str(n) for n in stale)
        )
    return kept, used


_SURFACE_ORDER = {"tree": 0, "path": 1, "link": 2, "content": 3, "history": 4, "ref": 5,
                  "commit": 6, "tag": 7, "remote": 8}


def _sort_key(f):
    return (f.path, _SURFACE_ORDER[f.surface], f.offset if f.offset is not None else -1,
            f.cls, f.term.index if f.term else 0)


def run_tree(root, profile_path, inventory_path, exceptions_path=None,
             names_path=DEFAULT_NAMES, words_path=DEFAULT_WORDS, receipt_path=None,
             link_root=None):
    """Run the tree gate. Raises Refused.

    Returns (exit code, findings, report lines, summary). The lines and the
    summary's findings are redacted; the findings themselves are not, and
    exist for tests and callers that never print them.
    """
    if not os.path.isdir(root):
        raise Refused("the root is not a directory")
    root_real = os.path.realpath(os.fsencode(root))
    link_root_real = None
    if link_root is not None:
        if not os.path.isdir(link_root):
            raise Refused("the link root is not a directory")
        link_root_real = os.path.realpath(os.fsencode(link_root))
    for what, path in (("profile", profile_path), ("inventory", inventory_path),
                       ("exceptions file", exceptions_path), ("receipt", receipt_path)):
        if path is not None and _inside(path, root_real):
            raise Refused("the %s is inside the tree being scanned; it would ship" % what)

    profile = load_profile(profile_path)
    try:
        return _run(root, root_real, profile, inventory_path, exceptions_path,
                    names_path, words_path, link_root_real)
    except Refused as exc:
        # Defence in depth: no refusal after the profile loads can carry a term.
        raise Refused(scrub(str(exc), profile, None))


def _run(root, root_real, profile, inventory_path, exceptions_path, names_path,
         words_path, link_root_real=None):
    inventory, inventory_sha = load_inventory(inventory_path)
    exceptions = load_exceptions(exceptions_path) if exceptions_path else {}
    lists = load_name_lists(names_path, words_path)

    entries, empty_dirs = walk(root, lambda p: redact(p, profile, lists))
    if not any(e.kind in _KINDS for e in entries):
        raise Refused("the tree has no files or links; an empty scope proves nothing")

    findings = _structural_findings(entries, empty_dirs, inventory, root_real,
                                   link_root_real)
    findings += scan(entries, profile, lists)

    kept, used = apply_exceptions(findings, exceptions)
    kept.sort(key=_sort_key)
    by_class = collections.Counter(f.cls for f in kept)
    summary = {
        "gate": "tree",
        "schema_version": SCHEMA_VERSION,
        "exit": 1 if kept else 0,
        "profile_sha256": profile.sha256,
        "profile_terms": len(profile.terms),
        "inventory_sha256": inventory_sha,
        "link_root": os.fsdecode(link_root_real) if link_root_real is not None else None,
        "tree_sha256": tree_digest(entries),
        "entries": len(entries),
        "files": sum(1 for e in entries if e.data is not None),
        "bytes": sum(len(e.data) for e in entries if e.data is not None),
        "findings_by_class": dict(sorted(by_class.items())),
        "exceptions_applied": len(used),
        "findings": [{k: scrub(v, profile, lists) if isinstance(v, str) else v
                      for k, v in receipt_finding(f, profile, lists).items()}
                     for f in kept],
        "limits": list(LIMITS),
    }
    # The whole line is scrubbed, not just its path: a profile `kind` or any
    # other field could carry a term too.
    lines = [scrub(format_finding(f, profile, lists), profile, lists) for f in kept]
    return summary["exit"], kept, lines, summary


# --------------------------------------------------------------------------
# Reporting: never print a matched value
# --------------------------------------------------------------------------


def redact(path, profile, lists):
    """`path` (bytes) as text, with every term and name occurrence masked."""
    spans = [(s, e, "<#%d>" % t.index) for t, s, e in term_hits(path, profile.terms)]
    spans += [(s, e, "<secret>") for _rule, s, e in secret_hits(path)]
    if lists is not None:
        spans += [(s, e, "<name>") for s, e in person_hits(path, lists)]
    spans.sort()
    out, pos = [], 0
    for start, end, mark in spans:
        if start < pos:  # overlapping hit: extend the mask already written
            if end > pos:
                pos = end
            continue
        out.append(path[pos:start].decode("utf-8", "replace"))
        out.append(mark)
        pos = end
    out.append(path[pos:].decode("utf-8", "replace"))
    return "".join(out)


def scrub(text, profile, lists):
    """`text` with every term (and, given lists, name) occurrence masked."""
    return redact(text.encode("utf-8", "surrogateescape"), profile, lists)


def describe(finding):
    if finding.cls == "term":
        t = finding.term
        what = "term #%d (%s%s)" % (t.index, t.mode, ", " + t.kind if t.kind else "")
    elif finding.cls == "person":
        what = "possible named person"
    elif finding.cls == "secret":
        what = "credential shape (%s)" % finding.rule
    else:
        what = finding.detail
    if finding.offset is not None:
        what += " at byte %d" % finding.offset
    return what


def format_finding(finding, profile, lists):
    shown = redact(finding.path, profile, lists)
    if finding.line is not None:
        shown += ":%d" % finding.line
    return "%-16s %-8s %s  %s" % (finding.cls, finding.surface, shown, describe(finding))


def receipt_finding(finding, profile, lists):
    return {
        "class": finding.cls,
        "surface": finding.surface,
        "path": redact(finding.path, profile, lists),
        "line": finding.line,
        "offset": finding.offset,
        "term_index": finding.term.index if finding.term else None,
        "rule": finding.rule,
        "oid": finding.oid,
        "sha256": finding.sha256,
        "detail": describe(finding),
    }
