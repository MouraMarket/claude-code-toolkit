"""Positive controls: plant a term, then prove the gate catches it there.

A clean gate result is only evidence once the same gate has been seen to catch
a planted term through the same traversal. Each control here builds its own
disposable repository (or tree), plants ONE value on ONE surface, runs the real
gate on it, and checks for the expected finding class on that surface. One
repository per control is deliberate: in a shared repository, a planted term
that the gate still reached by another route would let a broken traversal pass.

Two sets run:

- **Synthetic**: a built-in synthetic profile, on every surface, plus a
  credential, a wrong identity, a named person, the ordinary-word boundary,
  a clean control (must exit 0) and incomplete repositories (must refuse).
- **Real**: one plantable term per match mode from the caller's private
  profile, on every surface a term can sit on. Output names the term only by
  its profile index; fixtures live in a temporary directory deleted at the end.
"""

import collections
import json
import os
import re
import shutil
import subprocess
import tempfile

import history_lib as hist
import leak_gate_lib as lib
from leak_gate_lib import Refused

IDENTITY = "leak-gate-selftest <selftest@example.invalid>"
OTHER_IDENTITY = "other-author <other@example.invalid>"

SYNTHETIC_TERMS = [
    {"term": "widgetco", "mode": "substring", "kind": "synthetic"},
    {"term": "Fernvale", "mode": "word", "kind": "synthetic"},
    {"term": "ZQX", "mode": "token", "kind": "synthetic"},
    {"term": "QCAP", "mode": "ident", "kind": "synthetic"},
]
SYNTHETIC_NAMES = "Zorblat\n"
SYNTHETIC_WORDS = "the\n"
# A synthetic credential, assembled at run time so this file is not one.
SYNTHETIC_SECRET = "gh" + "p_" + "Zq7" * 12

PLANTABLE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
DATE = "1700000000 +0000"

Result = collections.namedtuple("Result", "set name ok detail")


def _split_ident(ident):
    name, email = ident.rsplit(" <", 1)
    return name, email[:-1]


class Fixture(object):
    """One disposable git repository, written with a fixed identity and clock."""

    def __init__(self, path, init=True):
        self.path = path
        if init:
            os.makedirs(path)
            self.git("init", "-q", "-b", "main")
            self.write("a.txt", b"x\n")
            self.commit("base")

    def git(self, *args, author=IDENTITY, committer=IDENTITY, stdin=None):
        env = hist.git_env()
        a_name, a_mail = _split_ident(author)
        c_name, c_mail = _split_ident(committer)
        env.update(GIT_AUTHOR_NAME=a_name, GIT_AUTHOR_EMAIL=a_mail, GIT_AUTHOR_DATE=DATE,
                   GIT_COMMITTER_NAME=c_name, GIT_COMMITTER_EMAIL=c_mail,
                   GIT_COMMITTER_DATE=DATE)
        proc = subprocess.run(["git", "-C", self.path] + list(args), input=stdin,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        if proc.returncode != 0:
            raise Refused("selftest fixture: git %s failed" % args[0])
        return proc.stdout.strip()

    def write(self, rel, data):
        full = os.path.join(self.path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as fh:
            fh.write(data)
        self.git("add", "--", rel)

    def commit(self, message, author=IDENTITY, committer=IDENTITY):
        self.git("commit", "-q", "--allow-empty", "-m", message,
                 author=author, committer=committer)

    def copy(self, path):
        shutil.copytree(self.path, path, symlinks=True)
        return Fixture(path, init=False)


# --------------------------------------------------------------------------
# Plants: each puts `v` (text) on exactly one surface of a fresh fixture
# --------------------------------------------------------------------------


def _content(v):
    return ("see %s here\n" % v).encode()


def plant_file(fx, v):
    fx.write("notes.txt", _content(v))
    fx.commit("add notes")


def plant_deleted(fx, v):
    fx.write("old.txt", _content(v))
    fx.commit("add old")
    fx.git("rm", "-q", "old.txt")
    fx.commit("remove old")


def plant_filename(fx, v):
    fx.write("x-%s-y.txt" % v, b"x\n")
    fx.commit("add file")


def plant_branch(fx, v):
    fx.git("checkout", "-q", "-b", "side")
    fx.write("side.txt", _content(v))
    fx.commit("side work")
    fx.git("checkout", "-q", "main")


def plant_orphan_root(fx, v):
    fx.git("checkout", "-q", "--orphan", "other")
    fx.git("rm", "-q", "-r", "--cached", ".")
    fx.write("root.txt", _content(v))
    fx.commit("second root")
    fx.git("checkout", "-q", "-f", "main")


def plant_detached(fx, v):
    fx.git("checkout", "-q", "--detach")
    fx.write("detached.txt", _content(v))
    fx.commit("detached work")


def plant_tag_message(fx, v):
    fx.git("tag", "-a", "t1", "-m", "release %s" % v)


def plant_tagged_only(fx, v):
    # A commit that only an annotated tag reaches: its branch is deleted.
    fx.git("checkout", "-q", "-b", "tmp")
    fx.write("tagged.txt", _content(v))
    fx.commit("tagged work")
    fx.git("tag", "-a", "t2", "-m", "release")
    fx.git("checkout", "-q", "main")
    fx.git("branch", "-q", "-D", "tmp")


def plant_tagger(fx, v):
    fx.git("tag", "-a", "t1", "-m", "release", committer="Tagger %s <t@example.invalid>" % v)


def plant_commit_message(fx, v):
    fx.commit("fix %s thing" % v)


def plant_author(fx, v):
    fx.commit("work", author="Auth %s <a@example.invalid>" % v)


def plant_committer(fx, v):
    fx.commit("work", committer="Comm %s <c@example.invalid>" % v)


def plant_merge_only(fx, v):
    fx.git("checkout", "-q", "-b", "side")
    fx.write("s.txt", b"s\n")
    fx.commit("side")
    fx.git("checkout", "-q", "main")
    fx.write("m.txt", b"m\n")
    fx.commit("main")
    fx.git("merge", "-q", "--no-ff", "--no-commit", "side")
    fx.write("merged.txt", _content(v))
    fx.commit("merge side")
    fx.git("branch", "-q", "-D", "side")


def plant_binary(fx, v):
    fx.write("blob.bin", b"\0\x01\x02" + v.encode() + b"\0\xff")
    fx.commit("add binary")


def plant_note(fx, v):
    # -f: the clean base already carries a note on this commit.
    fx.git("notes", "add", "-f", "-m", "note %s" % v, "HEAD")


def plant_blob_ref(fx, v):
    oid = fx.git("hash-object", "-w", "--stdin", stdin=_content(v)).decode()
    fx.git("update-ref", "refs/blobs/x", oid)


def plant_ref_name(fx, v):
    fx.git("branch", "x-%s" % v)


def plant_orig_head(fx, v):
    # After the reset, only the ORIG_HEAD pseudo-ref (and the reflog) reaches it.
    fx.write("dropped.txt", _content(v))
    fx.commit("dropped work")
    fx.git("reset", "-q", "--hard", "HEAD~1")


def plant_stash(fx, v):
    with open(os.path.join(fx.path, "a.txt"), "wb") as fh:
        fh.write(_content(v))
    fx.git("stash", "-q")


# (name, plant, expected class, expected surface)
TERM_CONTROLS = (
    ("file", plant_file, "term", "content"),
    ("deleted-file", plant_deleted, "term", "content"),
    ("filename", plant_filename, "term", "path"),
    ("other-branch", plant_branch, "term", "content"),
    ("second-root-commit", plant_orphan_root, "term", "content"),
    ("detached-head", plant_detached, "term", "content"),
    ("tag-message", plant_tag_message, "term", "tag"),
    ("tagged-only-commit", plant_tagged_only, "term", "content"),
    ("tagger", plant_tagger, "term", "tag"),
    ("commit-message", plant_commit_message, "term", "commit"),
    ("author", plant_author, "term", "commit"),
    ("committer", plant_committer, "term", "commit"),
    ("merge-only-path", plant_merge_only, "term", "content"),
    ("non-text-blob", plant_binary, "term", "content"),
    ("note", plant_note, "term", "content"),
    ("direct-blob-ref", plant_blob_ref, "term", "content"),
    ("ref-name", plant_ref_name, "term", "ref"),
    ("stash", plant_stash, "term", "content"),
    ("orig-head-pseudo-ref", plant_orig_head, "term", "content"),
)


# --------------------------------------------------------------------------
# Running controls
# --------------------------------------------------------------------------


class Runner(object):
    def __init__(self, base, names_path, words_path):
        self.base = base
        self.names, self.words = names_path, words_path
        self.clean = Fixture(os.path.join(base, "clean"))
        self.clean.git("tag", "-a", "t0", "-m", "clean tag")
        self.clean.git("notes", "add", "-m", "clean note", "HEAD")
        self.count = 0

    def fresh(self):
        self.count += 1
        return self.clean.copy(os.path.join(self.base, "c%03d" % self.count))

    def history(self, fx, profile_path):
        return hist.run_history(fx.path, profile_path, IDENTITY, names_path=self.names,
                                words_path=self.words)

    def expect(self, fx, profile_path, cls, surface, index=None):
        """None if the gate found `cls` on `surface` (and term `index`), else why not."""
        try:
            code, findings, _lines, _summary = self.history(fx, profile_path)
        except Refused:
            return "the gate refused"
        hit = [f for f in findings if f.cls == cls and f.surface == surface
               and (index is None or (f.term is not None and f.term.index == index))]
        if code != 1 or not hit:
            return "expected exit 1 with %s on %s, got exit %d and %s" % (
                cls, surface, code, ", ".join(sorted({"%s/%s" % (f.cls, f.surface)
                                                      for f in findings})) or "nothing")
        return None

    def expect_refused(self, fx, profile_path):
        try:
            code = self.history(fx, profile_path)[0]
        except Refused:
            return None
        return "expected a refusal, got exit %d" % code


def _synthetic(runner, profile_path):
    out = []

    def record(name, problem):
        out.append(Result("synthetic", name, problem is None, problem or ""))

    try:
        code = runner.history(runner.clean, profile_path)[0]
        record("clean-repository", None if code == 0 else "expected exit 0, got %d" % code)
    except Refused:
        record("clean-repository", "the gate refused a clean repository")

    # One synthetic term per surface, rotating through the modes, so every mode
    # sits on several surfaces. Every real mode still covers every surface.
    for number, (name, plant, cls, surface) in enumerate(TERM_CONTROLS):
        index = number % len(SYNTHETIC_TERMS) + 1
        term = SYNTHETIC_TERMS[index - 1]
        fx = runner.fresh()
        plant(fx, term["term"])
        record("%s/%s" % (term["mode"], name),
               runner.expect(fx, profile_path, cls, surface, index))

    fx = runner.fresh()
    plant_deleted(fx, SYNTHETIC_SECRET)
    record("credential-in-deleted-file", runner.expect(fx, profile_path, "secret", "content"))

    fx = runner.fresh()
    fx.commit("work", author=OTHER_IDENTITY)
    record("unapproved-author", runner.expect(fx, profile_path, "identity", "commit"))

    fx = runner.fresh()
    fx.commit("thanks to Zorblat Quux")
    record("named-person", runner.expect(fx, profile_path, "person", "commit"))

    # The ordinary-word boundary: "Fernvale" in word mode must not hit inside a
    # longer word, and must still hit on its own. Exactly one finding.
    fx = runner.fresh()
    fx.write("words.txt", b"Fernvalex afernvale and Fernvale\n")
    fx.commit("words")
    head = fx.git("rev-parse", "HEAD^{tree}")
    code, findings, _l, _s = runner.history(fx, profile_path)
    terms = [f for f in findings if f.cls == "term"]
    unchanged = fx.git("rev-parse", "HEAD^{tree}") == head
    record("ordinary-word-boundary",
           None if code == 1 and len(terms) == 1 and unchanged
           else "expected one bounded hit and an untouched repository")

    for name, make in (
        ("shallow-clone", lambda p: _clone(runner, p, "--depth", "1")),
        ("borrowed-objects", lambda p: _clone(runner, p, "--shared")),
        ("missing-object", lambda p: _drop_object(runner.fresh())),
        ("no-refs", lambda p: _empty(p)),
        ("linked-worktree", lambda p: _with_worktree(runner.fresh())),
    ):
        runner.count += 1
        path = os.path.join(runner.base, "r%03d" % runner.count)
        record(name, runner.expect_refused(make(path), profile_path))

    out += _tree_controls(runner, profile_path, SYNTHETIC_TERMS[0]["term"], 1, "synthetic")
    return out


def _clone(runner, path, *flags):
    # --depth needs a transport (a plain path ignores it); --shared needs a
    # plain path (over file:// it silently copies instead of borrowing).
    source = runner.clean.path if "--shared" in flags else "file://" + runner.clean.path
    subprocess.run(["git", "clone", "-q"] + list(flags) + [source, path],
                   env=hist.git_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True)
    fx = Fixture(path, init=False)
    if "--shared" in flags and not os.path.exists(
            os.path.join(path, ".git", "objects", "info", "alternates")):
        raise Refused("selftest fixture: the borrowed-objects clone has no alternates")
    if "--depth" in flags and not os.path.exists(os.path.join(path, ".git", "shallow")):
        raise Refused("selftest fixture: the shallow clone is not shallow")
    return fx


def _drop_object(fx):
    fx.write("gone.txt", b"gone\n")
    fx.commit("to be damaged")
    oid = fx.git("rev-parse", "HEAD:gone.txt").decode()
    os.remove(os.path.join(fx.path, ".git", "objects", oid[:2], oid[2:]))
    return fx


def _empty(path):
    os.makedirs(path)
    fx = Fixture(path, init=False)
    fx.git("init", "-q", "-b", "main")
    return fx


def _with_worktree(fx):
    fx.git("worktree", "add", "-q", fx.path + "-wt")
    return fx


def _tree_controls(runner, profile_path, value, index, label):
    """The tree gate over the same detector: a planted term blocks, clean passes."""
    out = []
    for name, data, cls in (("tree-clean", b"x\n", None),
                            ("tree-term", _content(value), "term"),
                            ("tree-credential", _content(SYNTHETIC_SECRET), "secret")):
        if label != "synthetic" and cls != "term":
            continue
        runner.count += 1
        root = os.path.join(runner.base, "t%03d" % runner.count)
        os.makedirs(root)
        with open(os.path.join(root, "a.txt"), "wb") as fh:
            fh.write(data)
        inventory = os.path.join(runner.base, "t%03d.json" % runner.count)
        with open(inventory, "w", encoding="utf-8") as fh:
            json.dump(lib.make_inventory(root), fh)
        try:
            code, findings, _l, _s = lib.run_tree(root, profile_path, inventory, None,
                                                  runner.names, runner.words)
        except Refused:
            out.append(Result(label, name, False, "the tree gate refused"))
            continue
        if cls is None:
            ok = code == 0
        else:
            ok = code == 1 and any(f.cls == cls and (cls != "term" or f.term.index == index)
                                   for f in findings)
        out.append(Result(label, name, ok, "" if ok else "unexpected tree gate result"))
    return out


def plantable_terms(profile, check_ref):
    """{mode: first term that can sit on every surface}, by profile order."""
    chosen = {}
    for term in profile.terms:
        if term.mode in chosen or not PLANTABLE.match(term.value):
            continue
        if ".." in term.value or term.value.endswith((".", ".lock")):
            continue
        if not check_ref("refs/heads/x-%s" % term.value):
            continue
        chosen[term.mode] = term
    return chosen


def _check_ref(name):
    return subprocess.run(["git", "check-ref-format", name], env=hist.git_env(),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                          ).returncode == 0


def _real(runner, profile_path, profile, chosen):
    out = []
    try:
        code = runner.history(runner.clean, profile_path)[0]
        out.append(Result("real", "clean-repository", code == 0,
                          "" if code == 0 else "expected exit 0, got %d" % code))
    except Refused:
        out.append(Result("real", "clean-repository", False, "the gate refused"))
    for mode, term in sorted(chosen.items()):
        label = "term #%d (%s)" % (term.index, mode)
        for name, plant, cls, surface in TERM_CONTROLS:
            fx = runner.fresh()
            plant(fx, term.value)
            problem = runner.expect(fx, profile_path, cls, surface, term.index)
            out.append(Result("real", "%s/%s" % (label, name), problem is None,
                              lib.scrub(problem or "", profile, None)))
        out += _tree_controls(runner, profile_path, term.value, term.index, "real")
    return out


def run_selftest(profile_path, require_real_control=False):
    """(exit code, [Result], summary). Raises Refused.

    Exit 0: every control behaved. 1: at least one did not, so the gate cannot
    be trusted. 2 (Refused): the profile is unusable, or some match mode in the
    profile has no plantable term, so no real control can run for it.

    A real control for every mode is ALWAYS required: a pass that skipped a mode
    would be evidence for a mode nobody tested. `require_real_control` is kept
    so the plan's command contract (`--require-real-control`) still parses; it
    no longer changes anything.
    """
    del require_real_control  # always on; see above
    profile = lib.load_profile(profile_path)
    used_modes = sorted({t.mode for t in profile.terms})
    chosen = plantable_terms(profile, _check_ref)
    missing = [m for m in used_modes if m not in chosen]
    if missing:
        raise Refused("no plantable term for match mode(s) %s, so a real control "
                      "cannot run for them" % ", ".join(missing))
    base = tempfile.mkdtemp(prefix="leak-gate-selftest-")
    try:
        synthetic_profile = os.path.join(base, "synthetic-profile.json")
        with open(synthetic_profile, "w", encoding="utf-8") as fh:
            json.dump({"schema_version": lib.SCHEMA_VERSION,
                       "modes": {m: "synthetic" for m in lib.MODES},
                       "terms": SYNTHETIC_TERMS}, fh)
        names = os.path.join(base, "names")
        words = os.path.join(base, "words")
        with open(names, "w", encoding="utf-8") as fh:
            fh.write(SYNTHETIC_NAMES)
        with open(words, "w", encoding="utf-8") as fh:
            fh.write(SYNTHETIC_WORDS)
        runner = Runner(os.path.join(base, "repos"), names, words)

        results = _synthetic(runner, synthetic_profile)
        results += _real(runner, profile_path, profile, chosen)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    failed = [r for r in results if not r.ok]
    summary = {
        "gate": "selftest",
        "schema_version": lib.SCHEMA_VERSION,
        "exit": 1 if failed else 0,
        "profile_sha256": profile.sha256,
        "profile_terms": len(profile.terms),
        "controls": len(results),
        "failed": len(failed),
        "real_controls": sum(1 for r in results if r.set == "real"),
        "real_terms": {m: t.index for m, t in sorted(chosen.items())},
        "modes_without_real_control": missing,
        "results": [r._asdict() for r in results],
    }
    return summary["exit"], results, summary
