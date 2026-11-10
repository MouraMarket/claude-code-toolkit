"""The fail-closed history gate, its self-test and the shared credential detector.

Hermetic: every repository is built in a temporary directory, with a SYNTHETIC
profile, synthetic name lists and a fixed synthetic identity. No real term,
name or path appears here, and no credential: each one is assembled at run
time.

Like the tree gate's suite, this one does not stop at "the gate fires".
TestDisabledTraversal turns off one traversal, detector or refusal at a time
and asserts that the control covering it then FAILS. A green control is not
evidence until it has been seen to fail.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)
import history_lib as hist  # noqa: E402
import leak_gate_lib as lib  # noqa: E402
import selftest_lib as st  # noqa: E402

CLI = os.path.join(TOOL, "leak-gate")
TERMS = st.SYNTHETIC_TERMS
SUB, WORD, TOKEN, IDENT = (t["term"] for t in TERMS)
SECRET = st.SYNTHETIC_SECRET


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="leakgate-history-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.priv = os.path.join(self.base, "private")
        os.mkdir(self.priv)
        self.profile = self.write_json("profile.json", {
            "schema_version": 1, "modes": {m: "t" for m in lib.MODES}, "terms": TERMS})
        self.names = self.write_text("names", st.SYNTHETIC_NAMES)
        self.words = self.write_text("words", st.SYNTHETIC_WORDS)
        self.runner = st.Runner(os.path.join(self.base, "repos"), self.names, self.words)

    def one_term_profile(self):
        """A one-term profile: a fast stand-in for the private one in CLI runs."""
        return self.write_json("one.json", {
            "schema_version": 1, "modes": {"word": "t"},
            "terms": [{"term": WORD, "mode": "word"}]})

    def write_json(self, name, data):
        path = os.path.join(self.priv, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def write_text(self, name, text):
        path = os.path.join(self.priv, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def fresh(self):
        return self.runner.fresh()

    def gate(self, fx, identity=st.IDENTITY, exceptions=None, **kw):
        return hist.run_history(fx.path, self.profile, identity, exceptions,
                                names_path=self.names, words_path=self.words, **kw)

    def found(self, fx, **kw):
        _code, findings, _lines, _summary = self.gate(fx, **kw)
        return sorted("%s/%s" % (f.cls, f.surface) for f in findings)

    def caught(self, plant, value, cls="term", surface="content"):
        """True when the gate reports `cls` on `surface` for `value` planted by `plant`."""
        fx = self.fresh()
        plant(fx, value)
        return self.runner.expect(fx, self.profile, cls, surface) is None


# --------------------------------------------------------------------------


class TestCleanRepository(Base):
    def test_a_clean_repository_with_a_tag_and_a_note_exits_0(self):
        code, findings, lines, summary = self.gate(self.runner.clean)
        self.assertEqual((code, findings, lines), (0, [], []))
        self.assertEqual(summary["tags"], 1)
        self.assertGreaterEqual(summary["commits"], 2)  # main plus the notes commit

    def test_the_walk_counts_equal_gits_inventory(self):
        _code, _f, _l, summary = self.gate(self.runner.clean)
        total = summary["commits"] + summary["trees"] + summary["blobs"] + summary["tags"]
        self.assertEqual(total, summary["git_objects"])

    def test_the_receipt_names_digests_and_limits_never_the_identity(self):
        _code, _f, _l, summary = self.gate(self.runner.clean)
        text = json.dumps(summary)
        self.assertNotIn("selftest@example.invalid", text)
        self.assertEqual(len(summary["identity_sha256"]), 64)
        self.assertTrue(any("reflog" in x for x in summary["limits"]))
        self.assertFalse(any("history gate" in x for x in summary["limits"]))


class TestSurfaces(Base):
    """Every surface section E names, one planted value each."""

    def test_each_term_control_is_caught_for_every_mode(self):
        for term in TERMS:
            for name, plant, cls, surface in st.TERM_CONTROLS:
                with self.subTest(mode=term["mode"], control=name):
                    self.assertTrue(self.caught(plant, term["term"], cls, surface))

    def test_a_deleted_credential_outside_the_term_list_is_caught(self):
        self.assertTrue(self.caught(st.plant_deleted, SECRET, "secret", "content"))

    def test_a_credential_in_a_commit_message_is_caught(self):
        self.assertTrue(self.caught(st.plant_commit_message, SECRET, "secret", "commit"))

    def test_the_merge_only_blob_is_reported_at_its_merge_only_path(self):
        fx = self.fresh()
        st.plant_merge_only(fx, SUB)
        _c, findings, _l, _s = self.gate(fx)
        [f] = [f for f in findings if f.cls == "term"]
        self.assertEqual(f.path, b"merged.txt")
        self.assertEqual(len(f.oid), 40)

    def test_a_blob_at_two_paths_keeps_both_aliases(self):
        baseline = self.gate(self.runner.clean)[3]["path_aliases"]
        fx = self.fresh()
        fx.write("a/__pycache__/m.pyc", b"x")
        fx.write("b/__pycache__/m.pyc", b"x")
        fx.commit("caches")
        _c, findings, _l, summary = self.gate(fx)
        generated = sorted(f.path for f in findings if f.cls == "generated")
        self.assertIn(b"a/__pycache__/m.pyc", generated)
        self.assertIn(b"b/__pycache__/m.pyc", generated)
        self.assertIn(b"a/__pycache__", generated)  # the directory itself
        self.assertIn(b"b/__pycache__", generated)
        self.assertEqual(summary["path_aliases"], baseline + 2)  # one blob, two paths

    def test_a_directory_hit_is_one_finding_on_the_directory(self):
        fx = self.fresh()
        fx.write("%s/one.txt" % SUB, b"x\n")
        fx.write("%s/two.txt" % SUB, b"x\n")
        fx.commit("dir")
        _c, findings, _l, _s = self.gate(fx)
        self.assertEqual([f.path for f in findings if f.cls == "term"], [SUB.encode()])

    def test_a_submodule_entry_is_a_finding(self):
        fx = self.fresh()
        fake = "1" * 40
        fx.git("update-index", "--add", "--cacheinfo", "160000,%s,sub" % fake)
        fx.commit("submodule")
        self.assertIn("submodule/history", self.found(fx))

    def test_an_opaque_blob_is_a_finding(self):
        fx = self.fresh()
        fx.write("x.bin", b"\0\x01\x02")
        fx.commit("bin")
        self.assertEqual(self.found(fx), ["opaque/content"])

    def test_a_named_person_in_a_message_is_a_finding(self):
        fx = self.fresh()
        fx.commit("thanks Zorblat Quux")
        self.assertEqual(self.found(fx), ["person/commit"])


class TestIdentities(Base):
    def test_every_identity_header_must_be_the_approved_one(self):
        cases = (
            ("author", lambda fx: fx.commit("w", author=st.OTHER_IDENTITY)),
            ("committer", lambda fx: fx.commit("w", committer=st.OTHER_IDENTITY)),
            ("tagger", lambda fx: fx.git("tag", "-a", "t9", "-m", "m",
                                         committer=st.OTHER_IDENTITY)),
        )
        for name, make in cases:
            with self.subTest(header=name):
                fx = self.fresh()
                make(fx)
                surface = "tag" if name == "tagger" else "commit"
                self.assertEqual(self.found(fx), ["identity/" + surface])

    def test_the_email_alone_differing_is_still_a_finding(self):
        fx = self.fresh()
        fx.commit("w", author="leak-gate-selftest <someone-else@example.invalid>")
        self.assertEqual(self.found(fx), ["identity/commit"])

    def test_a_tag_with_no_tagger_refuses_as_malformed(self):
        fx = self.fresh()
        head = fx.git("rev-parse", "HEAD").decode()
        raw = ("object %s\ntype commit\ntag bare\n\nno tagger\n" % head).encode()
        oid = fx.git("hash-object", "-t", "tag", "-w", "--stdin", "--literally",
                     stdin=raw).decode()
        fx.git("update-ref", "refs/tags/bare", oid)
        with self.assertRaises(lib.Refused) as ctx:
            self.gate(fx)
        self.assertIn("no tagger", str(ctx.exception))

    def test_a_person_named_in_the_approved_identity_is_not_a_person_finding(self):
        fx = self.fresh()
        approved = "Zorblat Quux <z@example.invalid>"
        fx.commit("w", author=approved, committer=approved)
        findings = self.found(fx, identity=approved)
        # The base commits are under the selftest identity, so they are identity
        # findings; the approved person's own header lines raise no person finding.
        self.assertNotIn("person/commit", findings)
        fx.commit("thanks Zorblat Quux", author=approved, committer=approved)
        self.assertIn("person/commit", self.found(fx, identity=approved))

    def test_identity_findings_cannot_be_excepted(self):
        fx = self.fresh()
        fx.commit("w", author=st.OTHER_IDENTITY)
        exc = self.write_json("exc.json", {"schema_version": 1, "exceptions": [{
            "class": "identity", "surface": "commit", "path": "a", "sha256": "0" * 64,
            "offset": 0, "term": None, "mode": None, "reason": "r"}]})
        with self.assertRaises(lib.Refused):
            self.gate(fx, exceptions=exc)

    def test_a_malformed_approved_identity_refuses(self):
        for bad in ("no email", "<only@email>", "Name <a b@x>", ""):
            with self.subTest(identity=bad), self.assertRaises(lib.Refused):
                self.gate(self.runner.clean, identity=bad)


class TestRefusals(Base):
    def refuses(self, fx, **kw):
        with self.assertRaises(lib.Refused) as ctx:
            self.gate(fx, **kw)
        return str(ctx.exception)

    def test_a_shallow_clone_refuses(self):
        fx = st._clone(self.runner, os.path.join(self.base, "shallow"), "--depth", "1")
        self.assertIn("shallow", self.refuses(fx))

    def test_borrowed_objects_refuse(self):
        fx = st._clone(self.runner, os.path.join(self.base, "shared"), "--shared")
        self.assertIn("alternates", self.refuses(fx))

    def test_a_missing_object_refuses(self):
        self.assertIn("missing", self.refuses(st._drop_object(self.fresh())))

    def test_no_refs_refuses(self):
        self.assertIn("no refs", self.refuses(st._empty(os.path.join(self.base, "e"))))

    def test_linked_worktrees_refuse_from_either_side(self):
        fx = st._with_worktree(self.fresh())
        self.refuses(fx)
        self.refuses(st.Fixture(fx.path + "-wt", init=False))

    def test_a_partial_clone_refuses(self):
        fx = self.fresh()
        fx.git("config", "remote.origin.promisor", "true")
        self.assertIn("partial", self.refuses(fx))

    def test_grafts_refuse(self):
        fx = self.fresh()
        os.makedirs(os.path.join(fx.path, ".git", "info"), exist_ok=True)
        with open(os.path.join(fx.path, ".git", "info", "grafts"), "w") as fh:
            fh.write("")
        self.assertIn("grafts", self.refuses(fx))

    def test_a_scan_without_all_refs_refuses(self):
        self.assertIn("--all-refs", self.refuses(self.runner.clean, all_refs=False))

    def test_inputs_inside_the_repository_refuse(self):
        fx = self.fresh()
        inside = os.path.join(fx.path, "profile.json")
        shutil.copy(self.profile, inside)
        with self.assertRaises(lib.Refused):
            hist.run_history(fx.path, inside, st.IDENTITY, names_path=self.names,
                             words_path=self.words)
        with self.assertRaises(lib.Refused):
            self.gate(fx, receipt_path=os.path.join(fx.path, "r.json"))

    def test_not_a_repository_refuses(self):
        plain = os.path.join(self.base, "plain")
        os.mkdir(plain)
        self.refuses(st.Fixture(plain, init=False))

    def test_a_missing_name_list_refuses(self):
        with self.assertRaises(lib.Refused):
            hist.run_history(self.runner.clean.path, self.profile, st.IDENTITY,
                             names_path=os.path.join(self.base, "nope"),
                             words_path=self.words)

    def test_a_ref_whose_walk_was_skipped_refuses(self):
        # The walk itself drops one ref's objects; git's inventory still has
        # them, so the comparison must refuse rather than report clean.
        fx = self.fresh()
        st.plant_branch(fx, SUB)
        side = fx.git("rev-parse", "side").decode()
        real_run = hist.Walk.run

        def skipping(walk, roots):
            return real_run(walk, {r for r in roots if r != side})
        with mock.patch.object(hist.Walk, "run", skipping):
            self.assertIn("enumeration is incomplete", self.refuses(fx))

    def test_a_ref_dropped_from_the_list_refuses(self):
        fx = self.fresh()
        fx.git("branch", "extra")  # same commit: its objects are reached anyway
        real = hist.list_refs

        def dropping(*a):
            return [r for r in real(*a) if r.name != b"refs/heads/extra"]
        with mock.patch.object(hist, "list_refs", dropping):
            self.assertIn("refs", self.refuses(fx))

    def test_a_tag_claiming_a_commit_for_a_blob_refuses(self):
        fx = self.fresh()
        blob = fx.git("hash-object", "-w", "--stdin", stdin=b"plain\n").decode()
        raw = ("object %s\ntype commit\ntag liar\ntagger %s %s\n\nm\n"
               % (blob, st.IDENTITY, st.DATE)).encode()
        oid = fx.git("hash-object", "-t", "tag", "-w", "--stdin", "--literally",
                     stdin=raw).decode()
        fx.git("update-ref", "refs/tags/liar", oid)
        self.assertIn("claimed", self.refuses(fx))

    def test_an_inherited_git_dir_does_not_redirect_the_scan(self):
        dirty = self.fresh()
        st.plant_file(dirty, SUB)
        with mock.patch.dict(os.environ, {"GIT_DIR": os.path.join(dirty.path, ".git")}):
            self.assertEqual(self.gate(self.runner.clean)[0], 0)

    def test_a_replace_ref_cannot_hide_a_commit(self):
        fx = self.fresh()
        st.plant_commit_message(fx, SUB)
        bad = fx.git("rev-parse", "HEAD").decode()
        fx.commit("clean stand-in")
        good = fx.git("rev-parse", "HEAD").decode()
        fx.git("reset", "-q", "--hard", bad)
        fx.git("replace", bad, good)
        self.assertIn("term/commit", self.found(fx))

    def test_a_blob_ref_to_a_tree_claimed_as_blob_refuses(self):
        fx = self.fresh()
        tree = fx.git("rev-parse", "HEAD^{tree}").decode()
        raw = ("object %s\ntype blob\ntag liar\ntagger %s %s\n\nm\n"
               % (tree, st.IDENTITY, st.DATE)).encode()
        oid = fx.git("hash-object", "-t", "tag", "-w", "--stdin", "--literally",
                     stdin=raw).decode()
        fx.git("update-ref", "refs/tags/liar", oid)
        self.refuses(fx)


class TestExceptions(Base):
    def exception(self, finding, **override):
        item = {"class": finding.cls, "surface": finding.surface,
                "path": os.fsdecode(finding.path), "sha256": finding.sha256,
                "offset": finding.offset,
                "term": finding.term.value if finding.term else finding.rule,
                "mode": finding.term.mode if finding.term else None, "reason": "test"}
        item.update(override)
        return self.write_json("exc.json", {"schema_version": 1, "exceptions": [item]})

    def test_an_exact_exception_clears_one_commit_finding(self):
        fx = self.fresh()
        st.plant_commit_message(fx, SUB)
        _c, [f], _l, _s = self.gate(fx)
        code, findings, _l, summary = self.gate(fx, exceptions=self.exception(f))
        self.assertEqual((code, findings, summary["exceptions_applied"]), (0, [], 1))

    def test_a_secret_exception_names_its_rule(self):
        fx = self.fresh()
        st.plant_file(fx, SECRET)
        _c, [f], _l, _s = self.gate(fx)
        self.assertEqual(f.rule, "github-token")
        self.assertEqual(self.gate(fx, exceptions=self.exception(f))[0], 0)
        with self.assertRaises(lib.Refused):  # a real rule, but not this one: stale
            self.gate(fx, exceptions=self.exception(f, term="aws-access-key"))
        for bad in ({"term": "no-such-rule"}, {"mode": "word"}):
            with self.subTest(bad=bad), self.assertRaises(lib.Refused) as ctx:
                self.gate(fx, exceptions=self.exception(f, **bad))
            self.assertIn("names its rule", str(ctx.exception))

    def test_an_exception_for_other_bytes_is_stale_and_refuses(self):
        fx = self.fresh()
        st.plant_commit_message(fx, SUB)
        _c, [f], _l, _s = self.gate(fx)
        with self.assertRaises(lib.Refused):
            self.gate(fx, exceptions=self.exception(f, sha256="0" * 64))

    def test_a_ref_name_exception_uses_the_ref_surface(self):
        fx = self.fresh()
        st.plant_ref_name(fx, TOKEN)
        _c, [f], _l, _s = self.gate(fx)
        self.assertEqual((f.surface, f.path), ("ref", ("refs/heads/x-%s" % TOKEN).encode()))
        self.assertEqual(self.gate(fx, exceptions=self.exception(f))[0], 0)


class TestCredentialDetector(Base):
    """The shared table, on the tree surface too."""

    def keys(self):
        # Assembled at run time; none of these strings is a credential as written.
        return {
            "anthropic-key": "sk-" + "ant-" + "api03-" + "Ab1" * 10,
            "private-key-block": "-----BEGIN " + "RSA PRIVATE KEY-----",
            "google-api-key": "AI" + "za" + "B" * 35,
            "github-token": SECRET,
            "aws-access-key": "AK" + "IA" + "Q" * 16,
            "slack-token": "xo" + "xb-" + "1234567890ab",
            "generic-sk-key": "sk-" + "a1" * 21,
        }

    def test_each_shape_is_caught_on_raw_bytes(self):
        for rule, key in self.keys().items():
            with self.subTest(rule=rule):
                hits = [r for r, _s, _e in lib.secret_hits(b"\0x " + key.encode() + b" \xff")]
                self.assertIn(rule, hits)

    def test_no_inline_marker_or_placeholder_shape_suppresses_a_hit(self):
        line = ("token = %s  # allowlist secret" % SECRET).encode()
        self.assertEqual([r for r, _s, _e in lib.secret_hits(line)], ["github-token"])
        fake = ("gh" + "p_" + "x" * 36).encode()
        self.assertEqual([r for r, _s, _e in lib.secret_hits(fake)], ["github-token"])

    def test_ordinary_text_is_not_a_credential(self):
        text = b"sk-learn task-runner ghp_short AKIA_lower a1b2c3d4" + b"0" * 40
        self.assertEqual(list(lib.secret_hits(text)), [])

    def tree(self, rel, data):
        root = os.path.join(self.base, "tree")
        os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
        with open(os.path.join(root, rel), "wb") as fh:
            fh.write(data)
        inv = self.write_json("inv.json", lib.make_inventory(root))
        return lib.run_tree(root, self.profile, inv, None, self.names, self.words)

    def test_the_tree_gate_catches_a_credential_in_content(self):
        code, findings, lines, _s = self.tree("a.txt", ("k=%s\n" % SECRET).encode())
        self.assertEqual((code, [f.cls for f in findings]), (1, ["secret"]))
        self.assertNotIn(SECRET, "\n".join(lines))
        self.assertIn("github-token", lines[0])

    def test_a_credential_in_a_path_is_masked_in_output(self):
        code, findings, lines, summary = self.tree("%s.txt" % SECRET, b"x\n")
        self.assertEqual(code, 1)
        self.assertIn("secret", [f.cls for f in findings])
        self.assertNotIn(SECRET, "\n".join(lines) + json.dumps(summary))


class TestDisabledTraversal(Base):
    """Turn one traversal, detector or refusal off; its control must then FAIL."""

    def no_enumeration_check(self):
        return mock.patch.object(hist, "check_against_git", lambda *a: (0, 0))

    def test_without_parents_and_the_inventory_check_a_deleted_file_escapes(self):
        real = hist.Walk._commit

        def no_parents(walk, oid, data, objects, trees):
            return real(walk, oid, data, [], trees)
        with mock.patch.object(hist.Walk, "_commit", no_parents), self.no_enumeration_check():
            self.assertFalse(self.caught(st.plant_deleted, SUB))
        # The inventory check alone turns the same blind walk into a refusal.
        with mock.patch.object(hist.Walk, "_commit", no_parents):
            fx = self.fresh()
            st.plant_deleted(fx, SUB)
            with self.assertRaises(lib.Refused):
                self.gate(fx)

    def test_without_head_a_detached_commit_escapes(self):
        real = hist.list_refs

        def no_head(*a):
            return [r for r in real(*a) if r.name != b"HEAD"]
        with mock.patch.object(hist, "list_refs", no_head), self.no_enumeration_check():
            self.assertFalse(self.caught(st.plant_detached, SUB))

    def test_without_pseudo_refs_orig_head_escapes(self):
        with mock.patch.object(hist, "PSEUDO_REFS", ()):
            self.assertFalse(self.caught(st.plant_orig_head, SUB))

    def test_without_tag_scanning_a_tag_message_escapes(self):
        real = hist.scan_walk

        def no_tags(walk, *a):
            walk.tags = []
            return real(walk, *a)
        with mock.patch.object(hist, "scan_walk", no_tags):
            self.assertFalse(self.caught(st.plant_tag_message, SUB, "term", "tag"))

    def test_without_ref_name_scanning_a_branch_name_escapes(self):
        real = lib.scan_path

        def no_refs(path, profile, lists, surface="path"):
            return [] if surface == "ref" else real(path, profile, lists, surface)
        with mock.patch.object(lib, "scan_path", no_refs):
            self.assertFalse(self.caught(st.plant_ref_name, SUB, "term", "ref"))

    def test_without_the_credential_table_a_deleted_key_escapes(self):
        with mock.patch.object(lib, "SECRET_RULES", ()):
            self.assertFalse(self.caught(st.plant_deleted, SECRET, "secret", "content"))

    def test_without_the_identity_check_a_foreign_author_escapes(self):
        with mock.patch.object(hist, "_ident_findings", lambda *a: ([], [])):
            self.assertFalse(self.caught(lambda fx, v: fx.commit("w", author=v),
                                         st.OTHER_IDENTITY, "identity", "commit"))

    def test_without_every_tree_a_merge_only_path_escapes(self):
        real = hist.Walk._commit

        def first_parent_tree_only(walk, oid, data, objects, trees):
            parents = [ln for ln in data.split(b"\n\n")[0].split(b"\n")
                       if ln.startswith(b"parent ")]
            if len(parents) > 1:  # a merge: walk its parents, not its own tree
                return real(walk, oid, data, objects, [])
            return real(walk, oid, data, objects, trees)
        with mock.patch.object(hist.Walk, "_commit", first_parent_tree_only), \
                self.no_enumeration_check():
            self.assertFalse(self.caught(st.plant_merge_only, SUB))

    def test_without_the_shallow_check_a_shallow_clone_passes(self):
        real = hist._git

        def never_shallow(repo, env, *args, **kw):
            proc = real(repo, env, *args, **kw)
            if args[:2] == ("rev-parse", "--is-shallow-repository"):
                proc.stdout = b"false\n"
            return proc
        fx = st._clone(self.runner, os.path.join(self.base, "sh"), "--depth", "1")
        with mock.patch.object(hist, "_git", never_shallow):
            self.assertEqual(self.gate(fx)[0], 0)  # the control's refusal is gone

    def test_selftest_reports_a_failed_control_when_a_detector_is_off(self):
        with mock.patch.object(lib, "SECRET_RULES", ()):
            code, results, _summary = st.run_selftest(self.one_term_profile())
        self.assertEqual(code, 1)
        failed = {r.name for r in results if not r.ok}
        self.assertIn("credential-in-deleted-file", failed)


class TestSelftest(Base):
    def test_every_control_passes_with_a_profile_used_as_real(self):
        # A private temp root, so a concurrent copy of this suite cannot leave
        # its own fixtures where this check looks. In-process only: the fixture
        # is made by tempfile, and git subprocesses may write their own files
        # under TMPDIR (xcrun_db on macOS).
        tmp = os.path.join(self.base, "tmp")
        os.mkdir(tmp)
        with mock.patch.object(tempfile, "tempdir", tmp):
            code, results, summary = st.run_selftest(self.profile, require_real_control=True)
        self.assertEqual([r for r in results if not r.ok], [])
        self.assertEqual(code, 0)
        self.assertEqual(sorted(summary["real_terms"]), sorted(lib.MODES))
        per_mode = len(st.TERM_CONTROLS) + 1  # every surface plus the tree control
        self.assertEqual(summary["real_controls"], 1 + per_mode * len(lib.MODES))
        # Every synthetic surface ran too.
        names = {r.name.split("/")[-1] for r in results if r.set == "synthetic"}
        self.assertTrue({c[0] for c in st.TERM_CONTROLS} <= names)
        # The fixtures, which held the planted terms, are gone.
        self.assertEqual([d for d in os.listdir(tmp) if d.startswith("leak-gate-selftest-")], [])

    def test_a_tree_gate_that_blocks_a_clean_tree_fails_the_clean_control(self):
        spurious = lib.Finding("opaque", "content", b"a.txt", None, None, None, "", "0" * 64)
        with mock.patch.object(lib, "run_tree", lambda *a: (1, [spurious], [], {})):
            results = st._tree_controls(self.runner, self.profile, SUB, 1, "synthetic")
        self.assertFalse({r.name: r.ok for r in results}["tree-clean"])

    def test_a_control_requires_the_planted_term_not_just_any_term(self):
        fx = self.fresh()
        st.plant_file(fx, SUB)  # term #1
        self.assertIsNone(self.runner.expect(fx, self.profile, "term", "content", 1))
        self.assertIsNotNone(self.runner.expect(fx, self.profile, "term", "content", 2))

    def test_a_mode_with_no_plantable_term_refuses_with_or_without_the_flag(self):
        profile = self.write_json("p2.json", {
            "schema_version": 1, "modes": {"word": "t", "token": "t"},
            "terms": [{"term": "Fernvale", "mode": "word"},
                      {"term": "has space", "mode": "token"}]})
        for flag in (True, False):
            with self.subTest(flag=flag), self.assertRaises(lib.Refused) as ctx:
                st.run_selftest(profile, require_real_control=flag)
            self.assertIn("token", str(ctx.exception))
        self.assertEqual(st.plantable_terms(lib.load_profile(profile), st._check_ref).keys(),
                         {"word"})


class TestCli(Base):
    def run_cli(self, *args):
        proc = subprocess.run([sys.executable, CLI] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=dict(os.environ,
                                                              PYTHONDONTWRITEBYTECODE="1"))
        return proc.returncode, proc.stdout.decode() + proc.stderr.decode()

    def history(self, fx, *extra):
        return self.run_cli("history", "--repo", fx.path, "--all-refs", "--profile",
                            self.profile, "--identity", st.IDENTITY, "--names-file",
                            self.names, "--words-file", self.words, *extra)

    def test_exit_codes_and_no_value_in_output(self):
        self.assertEqual(self.history(self.runner.clean)[0], 0)
        fx = self.fresh()
        st.plant_filename(fx, SUB)
        st.plant_commit_message(fx, WORD)
        code, out = self.history(fx)
        self.assertEqual(code, 1)
        for value in (SUB, WORD, SUB.lower(), WORD.lower()):
            self.assertNotIn(value, out)
        self.assertIn("<#1>", out)

    def test_missing_all_refs_is_refused(self):
        code, out = self.run_cli("history", "--repo", self.runner.clean.path, "--profile",
                                 self.profile, "--identity", st.IDENTITY)
        self.assertEqual(code, 2)
        self.assertIn("--all-refs", out)

    def test_a_refused_run_overwrites_an_earlier_clean_receipt(self):
        receipt = os.path.join(self.priv, "r.json")
        self.assertEqual(self.history(self.runner.clean, "--receipt", receipt)[0], 0)
        with open(receipt) as fh:
            self.assertEqual(json.load(fh)["exit"], 0)
        shallow = st._clone(self.runner, os.path.join(self.base, "sh"), "--depth", "1")
        self.assertEqual(self.history(shallow, "--receipt", receipt)[0], 2)
        with open(receipt) as fh:
            self.assertEqual(json.load(fh)["exit"], 2)

    def test_selftest_cli_passes_and_writes_a_receipt(self):
        receipt = os.path.join(self.priv, "st.json")
        code, out = self.run_cli("selftest", "--profile", self.one_term_profile(),
                                 "--require-real-control", "--receipt", receipt)
        self.assertEqual(code, 0, out)
        self.assertIn("PASS", out)
        with open(receipt) as fh:
            data = json.load(fh)
        self.assertEqual((data["exit"], data["failed"]), (0, 0))
        self.assertNotIn(SUB, out)


if __name__ == "__main__":
    unittest.main()
