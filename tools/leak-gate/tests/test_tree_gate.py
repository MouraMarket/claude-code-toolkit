"""The fail-closed tree leak gate.

Hermetic: every fixture is built in a temporary directory with a SYNTHETIC
profile and synthetic name lists. No real term, name or path appears here.

The gate's failure is silent: a tree it misses looks clean and ships. So this
suite does not stop at "the gate fires". TestDisabledDetectors replaces each
detector with a no-op and asserts the positive controls then FAIL. A green
control is not evidence until it has been seen to fail.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)
import leak_gate_lib as lib  # noqa: E402

CLI = os.path.join(TOOL, "leak-gate")

# Synthetic terms, one per mode, plus a non-ASCII one.
TERMS = [
    {"term": "widgetco", "mode": "substring", "kind": "org"},
    {"term": "Fernvale", "mode": "word", "kind": "client-name"},
    {"term": "ZQX", "mode": "token", "kind": "client-code"},
    {"term": "QCAP", "mode": "ident", "kind": "client-code"},
    {"term": "Zoé", "mode": "word", "kind": "person"},
]
MODES = {m: "test" for m in lib.MODES}
SECRET_WORDS = ("widgetco", "fernvale", "zqx", "qcap", "zoé", "zoe", "zorblat")


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="leakgate-")
        self.addCleanup(self._cleanup)
        self.root = os.path.join(self.base, "tree")
        os.mkdir(self.root)
        self.priv = os.path.join(self.base, "private")
        os.mkdir(self.priv)
        self.profile = self.write_json("profile.json", {
            "schema_version": 1, "modes": MODES, "terms": TERMS})
        self.names = self.write_text("names", "Zorblat\nMark\nClaude\n")
        self.words = self.write_text("words", "mark\nzebra\nthe\n")

    def _cleanup(self):
        for dirpath, dirnames, _files in os.walk(self.base):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        shutil.rmtree(self.base, ignore_errors=True)

    def write_json(self, name, data):
        path = os.path.join(self.priv, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        return path

    def write_text(self, name, text):
        path = os.path.join(self.priv, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def put(self, rel, data=b"clean\n", executable=False):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(data)
        if executable:
            os.chmod(path, 0o755)
        return path

    def link(self, rel, target):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)

    def freeze(self):
        """Inventory the tree as it stands (a reviewed tree), outside the root."""
        return self.write_json("inventory.json", lib.make_inventory(self.root))

    def gate(self, inventory=None, exceptions=None, link_root=None):
        inventory = inventory or self.freeze()
        return lib.run_tree(self.root, self.profile, inventory, exceptions,
                            self.names, self.words, link_root=link_root)

    def classes(self, **kw):
        _code, findings, _lines, _summary = self.gate(**kw)
        return sorted(f.cls for f in findings)

    def only(self, cls, **kw):
        code, findings, _lines, _summary = self.gate(**kw)
        self.assertEqual(code, 1)
        return [f for f in findings if f.cls == cls]


# --------------------------------------------------------------------------


class TestMatchModes(Base):
    def hits(self, text):
        self.put("a.md", text)
        _code, findings, _lines, _summary = self.gate()
        return sorted(f.term.value for f in findings if f.cls == "term")

    def test_substring_is_case_insensitive_and_unbounded(self):
        self.assertEqual(self.hits(b"xxWIDGETCOyy"), ["widgetco"])

    def test_word_is_case_insensitive_and_bounded_by_alnum_only(self):
        self.assertEqual(self.hits(b"FERNVALE-x and x_fernvale"), ["Fernvale", "Fernvale"])

    def test_word_does_not_hit_inside_a_longer_word(self):
        self.assertEqual(self.hits(b"Fernvalex afernvale"), [])

    def test_token_is_case_sensitive_and_hits_at_underscore(self):
        self.assertEqual(self.hits(b"ZQX_ROSTER"), ["ZQX"])

    def test_token_misses_lowercase_and_embedded(self):
        self.assertEqual(self.hits(b"zqx AZQX ZQX9"), [])

    def test_ident_hits_standalone_and_at_hyphen(self):
        self.assertEqual(self.hits(b"QCAP here, x-QCAP"), ["QCAP", "QCAP"])

    def test_ident_misses_constant_names(self):
        self.assertEqual(self.hits(b"CORE_QCAP QCAP_LIMIT qcap"), [])

    def test_non_ascii_term_matches_composed_decomposed_and_uppercase(self):
        text = "zoé / Zoé / ZOÉ".encode("utf-8")
        self.assertEqual(self.hits(text), ["Zoé"] * 3)


class TestSurfaces(Base):
    def test_a_content_hit_reports_its_line(self):
        self.put("a.md", b"one\ntwo\nwidgetco\n")
        [f] = self.only("term")
        self.assertEqual((f.surface, f.line), ("content", 3))

    def test_a_filename_hit_is_a_path_finding(self):
        self.put("docs/widgetco-notes.md")
        [f] = self.only("term")
        self.assertEqual((f.surface, f.path), ("path", b"docs/widgetco-notes.md"))

    def test_a_directory_name_is_one_finding_not_one_per_file(self):
        self.put("widgetco/a.md")
        self.put("widgetco/b.md")
        [f] = self.only("term")
        self.assertEqual(f.path, b"widgetco")

    def test_a_separator_breaks_a_substring_term(self):
        self.put("x/widget/co.md")
        self.assertEqual(self.gate()[0], 0)

    def test_a_term_in_binary_content_is_found_and_the_file_is_opaque(self):
        self.put("img.bin", b"\x89PNG\x00\xffwidgetco\x00")
        self.assertEqual(self.classes(), ["opaque", "term"])

    def test_invalid_utf8_without_nul_is_opaque(self):
        self.put("a.txt", b"caf\xe9\n")
        self.assertEqual(self.classes(), ["opaque"])

    def test_symlink_text_is_scanned(self):
        self.put("real/widgetco.md")
        self.link("pointer", "real/widgetco.md")
        surfaces = sorted(f.surface for f in self.only("term"))
        self.assertEqual(surfaces, ["link", "path"])

    def test_a_long_base64_run_is_encoded(self):
        self.put("a.md", b"data: " + b"QUJD" * 60 + b"\n")
        self.assertEqual(self.classes(), ["encoded"])

    def test_a_short_base64_like_run_is_not(self):
        self.put("a.md", b"sha " + b"ab" * 32 + b"\n")
        self.assertEqual(self.gate()[0], 0)


class TestStructure(Base):
    def test_clean_tree_with_an_inside_link_passes(self):
        self.put("a.md")
        self.put("bin/tool", b"#!/bin/sh\n", executable=True)
        self.link("docs/a.md", "../a.md")
        code, findings, lines, summary = self.gate()
        self.assertEqual((code, findings, lines), (0, [], []))
        self.assertEqual(summary["files"], 2)

    def test_absolute_link_escapes(self):
        self.put("a.md")
        self.link("out", "/etc")
        self.assertEqual(self.classes(), ["symlink-escape"])

    def test_relative_link_climbing_above_root_escapes(self):
        self.put("a.md")
        self.link("sub/out", "../../elsewhere")
        self.assertEqual(self.classes(), ["symlink-escape"])

    def test_a_link_that_leaves_and_re_enters_the_tree_escapes(self):
        # It resolves inside here only because the root's own directory name is
        # spelled into the target; checked out under any other name, it dangles.
        self.put("a.md")
        self.link("x", "../tree/a.md")
        self.assertEqual(self.classes(), ["symlink-escape"])

    def test_link_escaping_only_after_resolution_is_caught(self):
        # Lexically "sub/up/../.." never climbs above the root; on disk sub/up
        # is the root itself, so it lands outside. Only realpath sees it.
        self.put("a.md")
        self.link("sub/up", "..")
        self.link("x", "sub/up/../..")
        self.assertEqual(self.classes(), ["symlink-escape"])

    def test_dangling_link(self):
        self.put("a.md")
        self.link("gone", "nothing-here")
        self.assertEqual(self.classes(), ["symlink-dangling"])

    def make_link_root(self, *names):
        link_root = os.path.join(self.base, "linkroot")
        os.mkdir(link_root)
        for name in names:
            with open(os.path.join(link_root, name), "wb") as fh:
                fh.write(b"clean\n")
        return link_root

    def test_a_link_to_a_file_only_in_the_link_root_is_not_dangling(self):
        self.put("a.md")
        self.link("l", "b.md")
        self.assertEqual(self.classes(), ["symlink-dangling"])
        self.assertEqual(self.classes(link_root=self.make_link_root("b.md")), [])

    def test_a_link_absent_from_root_and_link_root_is_dangling(self):
        self.put("a.md")
        self.link("l", "b.md")
        self.assertEqual(self.classes(link_root=self.make_link_root("c.md")),
                         ["symlink-dangling"])

    def test_a_link_root_does_not_excuse_an_escape(self):
        self.put("a.md")
        self.link("sub/up", "../../x")
        self.link("abs", "/etc")
        link_root = self.make_link_root("x", "a.md")
        self.assertEqual(self.classes(link_root=link_root),
                         ["symlink-escape", "symlink-escape"])

    def test_a_link_that_leaves_the_link_root_escapes(self):
        # Inside the root, d is a real directory; in the link root, d is a link
        # out of it, so the same target lands outside the link root.
        outside = os.path.join(self.base, "outside")
        os.mkdir(outside)
        with open(os.path.join(outside, "b.md"), "wb") as fh:
            fh.write(b"clean\n")
        self.put("d/b.md")
        self.link("l", "d/b.md")
        link_root = self.make_link_root()
        os.symlink(outside, os.path.join(link_root, "d"))
        findings = self.only("symlink-escape", link_root=link_root)
        self.assertEqual([f.path for f in findings], [b"l"])

    def test_the_receipt_records_the_link_root(self):
        self.put("a.md")
        self.assertIsNone(self.gate()[3]["link_root"])
        link_root = self.make_link_root()
        self.assertEqual(self.gate(link_root=link_root)[3]["link_root"],
                         os.path.realpath(link_root))

    def test_a_link_root_that_is_not_a_directory_refuses(self):
        self.put("a.md")
        not_dir = self.write_text("file", "x")
        for bad in (not_dir, os.path.join(self.base, "missing")):
            with self.assertRaises(lib.Refused):
                self.gate(link_root=bad)

    def test_the_walk_never_follows_a_link_out_of_the_tree(self):
        outside = os.path.join(self.base, "outside")
        os.mkdir(outside)
        with open(os.path.join(outside, "leak.md"), "wb") as fh:
            fh.write(b"widgetco\n")
        self.put("a.md")
        self.link("ext", outside)
        _code, findings, _l, _s = self.gate()
        self.assertEqual([f.cls for f in findings], ["symlink-escape"])

    def test_an_untracked_file_is_unexpected(self):
        self.put("a.md")
        inventory = self.freeze()
        self.put("scratch.txt")
        [f] = self.only("unexpected", inventory=inventory)
        self.assertEqual(f.path, b"scratch.txt")

    def test_a_missing_file(self):
        self.put("a.md")
        self.put("b.md")
        inventory = self.freeze()
        os.unlink(os.path.join(self.root, "b.md"))
        self.assertEqual(self.classes(inventory=inventory), ["missing"])

    def test_an_executable_bit_change_is_a_kind_mismatch(self):
        path = self.put("run.sh")
        inventory = self.freeze()
        os.chmod(path, 0o755)
        self.assertEqual(self.classes(inventory=inventory), ["kind-mismatch"])

    def test_changed_content_is_a_mismatch(self):
        self.put("a.md")
        inventory = self.freeze()
        self.put("a.md", b"changed\n")
        self.assertEqual(self.classes(inventory=inventory), ["content-mismatch"])

    def test_a_changed_link_target_is_a_mismatch(self):
        self.put("a.md")
        self.put("b.md")
        self.link("l", "a.md")
        inventory = self.freeze()
        os.unlink(os.path.join(self.root, "l"))
        self.link("l", "b.md")
        self.assertEqual(self.classes(inventory=inventory), ["content-mismatch"])

    def test_an_empty_directory_is_unexpected(self):
        self.put("a.md")
        os.mkdir(os.path.join(self.root, "empty"))
        self.assertEqual(self.classes(), ["unexpected"])

    def test_generated_artifacts_are_findings_even_when_inventoried(self):
        self.put("a.md")
        self.put(".DS_Store", b"\x00\x00\x00\x01Bud1")
        self.put("tools/__pycache__/x.cpython-39.pyc", b"\x00")
        found = self.classes()
        self.assertEqual(found.count("generated"), 3)  # .DS_Store, the dir, the .pyc

    def test_a_nested_repository_is_a_finding(self):
        self.put("a.md")
        self.put("vendor/lib/.git/config", b"[core]\n")
        self.assertIn("nested-repo", self.classes())

    def test_the_top_level_git_directory_is_skipped(self):
        self.put("a.md")
        os.makedirs(os.path.join(self.root, ".git"))
        with open(os.path.join(self.root, ".git", "widgetco"), "wb") as fh:
            fh.write(b"widgetco")
        self.assertEqual(self.gate()[0], 0)

    def test_submodules_and_lfs_are_findings(self):
        self.put("a.md")
        self.put(".gitmodules", b"[submodule \"x\"]\n")
        self.put(".gitattributes", b"*.bin filter=lfs diff=lfs\n")
        self.put("big.bin", b"version https://git-lfs.github.com/spec/v1\noid sha256:00\n")
        self.assertEqual(self.classes(), ["lfs", "lfs", "submodule"])

    def test_a_fifo_is_special(self):
        self.put("a.md")
        inventory = self.freeze()
        os.mkfifo(os.path.join(self.root, "pipe"))
        self.assertEqual(self.classes(inventory=inventory), ["special"])

    def test_a_group_execute_bit_alone_is_a_mismatch(self):
        path = self.put("a.sh")
        os.chmod(path, 0o644)
        inventory = self.freeze()
        os.chmod(path, 0o654)
        self.assertEqual(self.classes(inventory=inventory), ["kind-mismatch"])

    def test_a_permission_change_without_execute_is_a_mismatch(self):
        path = self.put("a.md")
        os.chmod(path, 0o644)
        inventory = self.freeze()
        os.chmod(path, 0o600)
        self.assertEqual(self.classes(inventory=inventory), ["content-mismatch"])

    def test_setuid_is_a_finding_even_when_inventoried(self):
        path = self.put("tool", b"#!/bin/sh\n")
        os.chmod(path, 0o4755)
        if not os.stat(path).st_mode & 0o4000:
            self.skipTest("this filesystem drops setuid")
        self.assertEqual(self.classes(), ["mode"])


class TestPersonDetector(Base):
    def test_a_listed_given_name_with_a_surname_is_flagged(self):
        self.put("a.md", b"Written by Zorblat Quux.\n")
        [f] = self.only("person")
        self.assertEqual(f.line, 1)

    def test_a_given_name_that_is_an_ordinary_word_is_not(self):
        self.put("a.md", b"Mark Done when finished.\n")
        self.assertEqual(self.gate()[0], 0)

    def test_the_product_name_is_not_a_person(self):
        self.put("a.md", b"Use Claude Code.\n")
        self.assertEqual(self.gate()[0], 0)

    def test_a_name_in_a_path_is_flagged(self):
        self.put("docs/Zorblat Quux.md")
        [f] = self.only("person")
        self.assertEqual(f.surface, "path")

    def test_surnames_with_apostrophes_hyphens_and_accents(self):
        self.put("a.md", "Zorblat O'Quux, Zorblat Smith-Jones, Zorblat N\u00fa\u00f1ez\n"
                 .encode("utf-8"))
        self.assertEqual(len(self.only("person")), 3)

    def test_a_lowercase_second_word_is_not_a_surname(self):
        self.put("a.md", b"Zorblat said hello\n")
        self.assertEqual(self.gate()[0], 0)

    def test_claude_with_a_surname_is_a_person(self):
        # Split so this file itself carries no name-shaped pair.
        self.put("a.md", b"Thanks to Claude " + b"Quux.\n")
        [f] = self.only("person")
        self.assertEqual(f.offset, 10)

    def test_product_possessives_and_acronyms_are_not_people(self):
        self.put("a.md", "Claude Code's hooks, Claude Code\u2019s docs, a Claude PR, "
                 "Zorblat API.\n".encode("utf-8"))
        self.assertEqual(self.gate()[0], 0)

    def test_a_missing_name_list_refuses(self):
        os.unlink(self.names)
        self.put("a.md")
        with self.assertRaises(lib.Refused):
            self.gate()


class TestExceptions(Base):
    def exception(self, finding, **override):
        item = {
            "class": finding.cls, "surface": finding.surface,
            "path": os.fsdecode(finding.path), "sha256": finding.sha256,
            "offset": finding.offset,
            "term": finding.term.value if finding.term else None,
            "mode": finding.term.mode if finding.term else None,
            "reason": "synthetic third-party value",
        }
        item.update(override)
        return item

    def exceptions(self, *items):
        return self.write_json("exceptions.json",
                               {"schema_version": 1, "exceptions": list(items)})

    def test_an_exact_exception_clears_exactly_its_finding(self):
        self.put("a.md", b"widgetco and widgetco\n")
        first, second = self.only("term")
        inventory = self.freeze()
        code, findings, _l, summary = self.gate(
            inventory=inventory, exceptions=self.exceptions(self.exception(first)))
        self.assertEqual(code, 1)
        self.assertEqual([f.offset for f in findings], [second.offset])
        self.assertEqual(summary["exceptions_applied"], 1)

    def test_a_file_change_makes_the_exception_stale_and_refuses(self):
        self.put("a.md", b"widgetco\n")
        [f] = self.only("term")
        exc = self.exceptions(self.exception(f))
        self.put("a.md", b"widgetco \n")
        with self.assertRaises(lib.Refused):
            self.gate(exceptions=exc)

    def test_a_wrong_offset_does_not_match(self):
        self.put("a.md", b"widgetco\n")
        [f] = self.only("term")
        with self.assertRaises(lib.Refused):
            self.gate(exceptions=self.exceptions(self.exception(f, offset=1)))

    def test_an_opaque_exception_does_not_excuse_a_term_inside_the_blob(self):
        self.put("img.bin", b"\x00widgetco")
        [f] = self.only("opaque")
        _code, findings, _l, _s = self.gate(exceptions=self.exceptions(self.exception(f)))
        self.assertEqual([x.cls for x in findings], ["term"])

    def test_no_global_allowance_can_be_written(self):
        self.put("a.md", b"widgetco\n")
        [f] = self.only("term")
        broad = self.exception(f)
        del broad["path"]
        with self.assertRaises(lib.Refused):
            self.gate(exceptions=self.exceptions(broad))

    def test_a_reason_is_required(self):
        self.put("a.md", b"widgetco\n")
        [f] = self.only("term")
        with self.assertRaises(lib.Refused):
            self.gate(exceptions=self.exceptions(self.exception(f, reason=" ")))

    def test_structural_classes_cannot_be_excepted(self):
        self.put("a.md")
        self.link("out", "/etc")
        [f] = self.only("symlink-escape")
        item = self.exception(f, sha256="0" * 64, offset=0)
        with self.assertRaises(lib.Refused):
            self.gate(exceptions=self.exceptions(item))

    def test_duplicate_exceptions_refuse(self):
        self.put("a.md", b"widgetco\n")
        [f] = self.only("term")
        item = self.exception(f)
        with self.assertRaises(lib.Refused):
            self.gate(exceptions=self.exceptions(item, dict(item)))


class TestRefusals(Base):
    def refuses(self, **kw):
        with self.assertRaises(lib.Refused):
            self.gate(**kw)

    def profile_with(self, **fields):
        data = {"schema_version": 1, "modes": MODES, "terms": TERMS}
        data.update(fields)
        self.profile = self.write_json("profile.json", data)

    def setUp(self):
        super().setUp()
        self.put("a.md")

    def test_missing_profile(self):
        self.profile = os.path.join(self.priv, "nope.json")
        self.refuses()

    def test_profile_not_json(self):
        self.profile = self.write_text("profile.json", "{")
        self.refuses()

    def test_profile_wrong_schema(self):
        self.profile_with(schema_version=2)
        self.refuses()

    def test_profile_with_no_terms(self):
        self.profile_with(terms=[])
        self.refuses()

    def test_profile_declaring_an_unimplemented_mode(self):
        self.profile_with(modes=dict(MODES, regex="x"))
        self.refuses()

    def test_profile_term_with_an_undeclared_mode(self):
        self.profile_with(modes={"substring": "x"})
        self.refuses()

    def test_profile_with_an_empty_or_blank_term(self):
        for blank in ("", "  "):
            self.profile_with(terms=TERMS + [{"term": blank, "mode": "substring"}])
            self.refuses()

    def test_missing_inventory(self):
        self.refuses(inventory=os.path.join(self.priv, "nope.json"))

    def test_empty_inventory(self):
        self.refuses(inventory=self.write_json("i.json", {"schema_version": 1, "entries": {}}))

    def test_inventory_with_a_duplicate_key(self):
        rec = '{"kind": "file", "mode": "0644", "sha256": "%s"}' % ("0" * 64)
        single = '{"schema_version": 1, "entries": {"a.md": %s}}' % rec
        lib.load_inventory(self.write_text("i.json", single))  # well-formed: accepted
        double = '{"schema_version": 1, "entries": {"a.md": %s, "a.md": %s}}' % (rec, rec)
        self.refuses(inventory=self.write_text("i.json", double))

    def test_inventory_with_bad_paths_or_fields(self):
        good = {"kind": "file", "mode": "0644", "sha256": "0" * 64}
        lib.load_inventory(self.write_json("i.json", {"schema_version": 1,
                                                      "entries": {"a.md": good}}))
        for entries in ({"../x": good}, {"/abs": good}, {"x/.git/config": good},
                        {"a.md": {"kind": "file"}}, {"a.md": dict(good, extra=1)},
                        {"a.md": dict(good, mode="644")}, {"a.md": dict(good, mode=420)},
                        {"a.md": {"kind": "dir", "sha256": "0" * 64}}):
            self.refuses(inventory=self.write_json("i.json", {"schema_version": 1,
                                                              "entries": entries}))

    def test_an_empty_tree(self):
        inventory = self.freeze()
        os.unlink(os.path.join(self.root, "a.md"))
        os.makedirs(os.path.join(self.root, ".git"))
        self.refuses(inventory=inventory)

    def test_inputs_inside_the_tree(self):
        inventory = self.freeze()
        inside = os.path.join(self.root, "profile.json")
        shutil.copy(self.profile, inside)
        with self.assertRaises(lib.Refused):
            lib.run_tree(self.root, inside, inventory, None, self.names, self.words)
        with self.assertRaises(lib.Refused):
            lib.run_tree(self.root, self.profile, inventory, inside, self.names, self.words)
        with self.assertRaises(lib.Refused):
            lib.run_tree(self.root, self.profile, inventory, None, self.names, self.words,
                         receipt_path=os.path.join(self.root, "r.json"))

    def test_root_not_a_directory(self):
        with self.assertRaises(lib.Refused):
            lib.run_tree(self.profile, self.profile, self.freeze(), None,
                         self.names, self.words)

    @unittest.skipIf(os.geteuid() == 0, "root reads everything")
    def test_an_unreadable_file(self):
        inventory = self.freeze()
        os.chmod(os.path.join(self.root, "a.md"), 0)
        self.refuses(inventory=inventory)

    @unittest.skipIf(os.geteuid() == 0, "root reads everything")
    def test_an_unlistable_directory(self):
        self.put("sub/b.md")
        inventory = self.freeze()
        os.chmod(os.path.join(self.root, "sub"), 0)
        self.refuses(inventory=inventory)


class TestRedaction(Base):
    def test_no_output_carries_a_matched_value(self):
        self.put("widgetco/Fernvale-ZQX.md", "QCAP zoé Zorblat Quux\n".encode("utf-8"))
        self.link("Zorblat Quux link", "widgetco/Fernvale-ZQX.md")
        os.mkdir(os.path.join(self.root, "ZQX-empty"))
        self.put("ZQX/unexpected.md")
        inventory = self.freeze()
        os.unlink(os.path.join(self.root, "ZQX", "unexpected.md"))
        self.put("ZQX/unexpected.md", b"changed\n")
        code, findings, lines, summary = self.gate(inventory=inventory)
        self.assertEqual(code, 1)
        self.assertGreaterEqual(len(findings), 8)
        blob = ("\n".join(lines) + json.dumps(summary, ensure_ascii=False)).lower()
        for word in SECRET_WORDS:
            self.assertNotIn(word, blob)
        self.assertIn("<#1>", blob)

    def test_a_profile_kind_carrying_a_term_is_masked(self):
        terms = [dict(TERMS[0], kind="widgetco-accounts")] + TERMS[1:]
        self.profile = self.write_json("profile.json", {
            "schema_version": 1, "modes": MODES, "terms": terms})
        self.put("a.md", b"widgetco\n")
        _c, _f, lines, summary = self.gate()
        blob = ("\n".join(lines) + json.dumps(summary)).lower()
        self.assertNotIn("widgetco", blob)

    def test_no_input_path_is_printed_in_a_refusal(self):
        secret_dir = os.path.join(self.base, "widgetco-inputs")
        os.mkdir(secret_dir)
        self.put("a.md")
        inventory = self.freeze()
        for kwargs in ({"names": os.path.join(secret_dir, "names")},
                       {"profile": os.path.join(secret_dir, "profile.json")}):
            names = kwargs.get("names", self.names)
            profile = kwargs.get("profile", self.profile)
            with self.assertRaises(lib.Refused) as ctx:
                lib.run_tree(self.root, profile, inventory, None, names, self.words)
            self.assertNotIn("widgetco", str(ctx.exception).lower())

    def test_a_clean_path_stays_readable(self):
        self.put("docs/tooling.md", b"widgetco\n")
        _c, _f, [line], _s = self.gate()
        self.assertIn("docs/tooling.md:1", line)

    def test_a_refusal_message_does_not_print_a_path_term(self):
        self.put("widgetco/a.md")
        inventory = self.freeze()
        os.chmod(os.path.join(self.root, "widgetco", "a.md"), 0)
        try:
            self.gate(inventory=inventory)
        except lib.Refused as exc:
            self.assertNotIn("widgetco", str(exc).lower())
            self.assertIn("<#1>", str(exc))
        else:
            self.fail("an unreadable file must refuse")


class TestDeterminism(Base):
    def test_two_runs_are_identical(self):
        self.put("b.md", b"widgetco ZQX\n")
        self.put("a/Fernvale.md", b"Zorblat Quux\n")
        inventory = self.freeze()
        first = self.gate(inventory=inventory)
        second = self.gate(inventory=inventory)
        self.assertEqual(first[2], second[2])
        self.assertEqual(first[3], second[3])


class TestCli(Base):
    def cli(self, *args):
        return subprocess.run([sys.executable, CLI] + list(args), stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=60)

    def tree_args(self, inventory, *extra):
        return ["tree", "--root", self.root, "--profile", self.profile, "--inventory",
                inventory, "--names-file", self.names, "--words-file", self.words] + list(extra)

    def test_exit_codes_and_receipt(self):
        self.put("a.md")
        receipt = os.path.join(self.priv, "receipt.json")
        inventory = self.freeze()
        ok = self.cli(*self.tree_args(inventory, "--receipt", receipt))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertIn(b"CLEAN", ok.stdout)
        with open(receipt) as fh:
            clean = json.load(fh)
        self.assertEqual((clean["exit"], clean["profile_terms"]), (0, len(TERMS)))

        self.put("a.md", b"widgetco\n")
        dirty = self.cli(*self.tree_args(self.freeze(), "--receipt", receipt))
        self.assertEqual(dirty.returncode, 1)
        self.assertNotIn(b"widgetco", dirty.stdout.lower())
        with open(receipt) as fh:
            self.assertNotIn("widgetco", fh.read().lower())

    def test_a_refused_run_overwrites_an_earlier_clean_receipt(self):
        self.put("a.md")
        receipt = os.path.join(self.priv, "receipt.json")
        inventory = self.freeze()
        self.assertEqual(self.cli(*self.tree_args(inventory, "--receipt", receipt)).returncode, 0)
        os.unlink(self.profile)
        bad = self.cli(*self.tree_args(inventory, "--receipt", receipt))
        self.assertEqual(bad.returncode, 2)
        self.assertIn(b"REFUSED", bad.stderr)
        with open(receipt) as fh:
            self.assertEqual(json.load(fh)["exit"], 2)

    def test_a_bad_link_root_exits_2_and_a_good_one_is_recorded(self):
        self.put("a.md")
        self.link("l", "b.md")
        inventory = self.freeze()
        receipt = os.path.join(self.priv, "receipt.json")
        for bad in (self.write_text("file", "x"), os.path.join(self.base, "missing")):
            run = self.cli(*self.tree_args(inventory, "--link-root", bad))
            self.assertEqual(run.returncode, 2)
            self.assertIn(b"REFUSED", run.stderr)
        link_root = os.path.join(self.base, "linkroot")
        os.mkdir(link_root)
        with open(os.path.join(link_root, "b.md"), "wb") as fh:
            fh.write(b"clean\n")
        run = self.cli(*self.tree_args(inventory, "--link-root", link_root,
                                       "--receipt", receipt))
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        with open(receipt) as fh:
            self.assertEqual(json.load(fh)["link_root"], os.path.realpath(link_root))

    def test_usage_errors_exit_2(self):
        self.assertEqual(self.cli().returncode, 2)
        self.assertEqual(self.cli("tree", "--root", self.root).returncode, 2)

    def test_inventory_round_trip(self):
        self.put("a.md")
        self.put("bin/x", b"#!/bin/sh\n", executable=True)
        self.link("l", "a.md")
        out = os.path.join(self.priv, "inv.json")
        made = self.cli("inventory", "--root", self.root, "--out", out)
        self.assertEqual(made.returncode, 0, made.stderr)
        self.assertEqual(self.cli(*self.tree_args(out)).returncode, 0)
        inside = self.cli("inventory", "--root", self.root,
                          "--out", os.path.join(self.root, "inv.json"))
        self.assertEqual(inside.returncode, 2)


# --------------------------------------------------------------------------
# Disabled detectors must make the positive controls fail
# --------------------------------------------------------------------------


class TestDisabledDetectors(Base):
    """Each control plants one defect; the gate must report its class.

    `run_controls` returns the controls that did NOT see their defect. The real
    gate must return none; each disabled detector must return the controls it
    owns. A detector whose removal no control notices is undefended.
    """

    CONTROLS = {
        "content-term": lambda t: t.put("a.md", b"widgetco\n"),
        "path-term": lambda t: t.put("widgetco.md"),
        "link-term": lambda t: (t.put("a.md"), t.link("l", "a.md#widgetco")),
        "word-mode": lambda t: t.put("a.md", b"FERNVALE\n"),
        "token-mode": lambda t: t.put("a.md", b"ZQX_X\n"),
        "nfd-form": lambda t: t.put("a.md", "Zoé\n".encode("utf-8")),
        "binary-term": lambda t: t.put("a.bin", b"\x00widgetco"),
        "opaque": lambda t: t.put("a.bin", b"\x00\x01"),
        "encoded": lambda t: t.put("a.md", b"QUJD" * 60),
        "person": lambda t: t.put("a.md", b"Zorblat Quux\n"),
        "escape": lambda t: (t.put("a.md"), t.link("out", "/etc")),
        "dangling": lambda t: (t.put("a.md"), t.link("gone", "nope")),
        "generated": lambda t: (t.put("a.md"), t.put(".DS_Store", b"x")),
        "nested-repo": lambda t: (t.put("a.md"), t.put("v/.git/HEAD", b"x")),
    }
    EXPECT = {
        "content-term": "term", "path-term": "term", "link-term": "term",
        "word-mode": "term", "token-mode": "term", "nfd-form": "term",
        "binary-term": "term", "opaque": "opaque", "encoded": "encoded",
        "person": "person", "escape": "symlink-escape",
        "dangling": "symlink-dangling", "generated": "generated",
        "nested-repo": "nested-repo",
    }
    # Negative controls: a correct gate reports these clean.
    CLEAN = {
        "word-boundary": lambda t: t.put("a.md", b"afernvalex\n"),
        "token-case": lambda t: t.put("a.md", b"zqx\n"),
        "ident-underscore": lambda t: t.put("a.md", b"CORE_QCAP\n"),
    }

    def fresh(self):
        shutil.rmtree(self.root)
        os.mkdir(self.root)

    def run_controls(self):
        missed = []
        for name, plant in sorted(self.CONTROLS.items()):
            self.fresh()
            plant(self)
            try:
                _c, findings, _l, _s = self.gate()
            except lib.Refused:
                missed.append(name)
                continue
            if self.EXPECT[name] not in {f.cls for f in findings}:
                missed.append(name)
        for name, plant in sorted(self.CLEAN.items()):
            self.fresh()
            plant(self)
            if self.gate()[0] != 0:
                missed.append(name)
        return missed

    @contextlib.contextmanager
    def patched(self, attr, value):
        real = getattr(lib, attr)
        setattr(lib, attr, value)
        try:
            yield
        finally:
            setattr(lib, attr, real)

    def test_the_real_gate_passes_every_control(self):
        self.assertEqual(self.run_controls(), [])

    def assertMisses(self, attr, value, owned):
        with self.patched(attr, value):
            missed = self.run_controls()
        for name in owned:
            self.assertIn(name, missed, "disabling %s went unnoticed by %s" % (attr, name))

    def test_no_term_matching(self):
        self.assertMisses("term_hits", lambda data, terms: iter(()),
                          ["content-term", "path-term", "link-term", "binary-term"])

    def test_no_person_matching(self):
        self.assertMisses("person_hits", lambda data, lists: iter(()), ["person"])

    def test_everything_reads_as_text(self):
        self.assertMisses("is_text", lambda data: True, ["opaque"])

    def test_no_encoded_detector(self):
        import re
        self.assertMisses("ENCODED_RUN", re.compile(b"(?!x)x"), ["encoded"])

    def test_no_link_resolution(self):
        self.assertMisses("link_problem", lambda root, rel, target, link_root=None: None,
                          ["escape", "dangling"])

    def test_no_structural_checks(self):
        self.assertMisses("structural", lambda entry: [], ["generated", "nested-repo"])

    def test_no_inventory_comparison(self):
        with self.patched("compare_inventory", lambda e, d, i: []):
            self.fresh()
            self.put("a.md")
            inventory = self.freeze()
            self.put("extra.md")
            self.assertEqual(self.gate(inventory=inventory)[0], 0)
        self.assertEqual(self.gate(inventory=inventory)[0], 1)  # and the real one sees it

    def test_dropping_case_folding_misses_the_word_mode(self):
        modes = dict(lib.MODES, word=(lib.MODES["word"][0], False))
        self.assertMisses("MODES", modes, ["word-mode"])

    def test_dropping_boundaries_breaks_the_negative_controls(self):
        modes = dict(lib.MODES, word=(None, True), ident=(lib.MODES["token"][0], False))
        self.assertMisses("MODES", modes, ["word-boundary", "ident-underscore"])

    def test_case_folding_tokens_breaks_the_negative_control(self):
        modes = dict(lib.MODES, token=(lib.MODES["token"][0], True))
        self.assertMisses("MODES", modes, ["token-case"])

    def test_compiling_only_the_composed_form_misses_nfd(self):
        real = lib.compile_term

        def nfc_only(index, value, mode, kind):
            term = real(index, value, mode, kind)
            return term._replace(patterns=term.patterns[-1:])
        self.assertMisses("compile_term", nfc_only, ["nfd-form"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
