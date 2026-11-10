"""Tests for write_guard_lib.decide -- the pure judgement behind
claude-distill-write-guard.

Written test-first per the plan: run once against a stub `decide` that
raises `NotImplementedError` (the RED baseline), then against the real
implementation. `exists`/`in_scope`/`install_safe` are injected as plain
membership predicates over explicit path sets here -- decide() owns the
`in_scope(path) or in_scope(realpath(path))` widening, so the fakes must
NOT do that resolution themselves, or a test could pass by luck rather than
by exercising decide()'s own logic (see the realpath test below).
"""
import os
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, TOOLS_DIR)
import write_guard_lib  # noqa: E402

GUARD_PATH = "/abs/tools/distill-guard/distill_guard.py"


def _predicates(in_scope_set=(), exists_set=(), safe_set=()):
    """exists/in_scope/install_safe callables over explicit path sets --
    plain membership, no filesystem or basename logic of their own."""
    in_scope_set = set(in_scope_set)
    exists_set = set(exists_set)
    safe_set = set(safe_set)
    return (lambda p: p in exists_set,
            lambda p: p in in_scope_set,
            lambda p: p in safe_set)


def _raiser(*_args, **_kwargs):
    raise RuntimeError("boom")


class DecideTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # realpath'd up front: decide() now resolves the given path's own
        # realpath internally (Fix 1), and on macOS a bare TemporaryDirectory
        # path sits under a `/var` -> `/private/var` symlink, so an
        # unresolved self.target would never equal decide()'s internal
        # `real` and every safe_set membership below would silently miss.
        self.target = os.path.realpath(
            os.path.join(self._tmp.name, "MEMORY.md"))
        with open(self.target, "w", encoding="utf-8") as handle:
            handle.write("x")

    def tearDown(self):
        self._tmp.cleanup()

    # AC1.1 -- existing, in-scope, install-safe + Write -> denied, full route
    def test_existing_in_scope_install_safe_write_is_denied_with_full_route(self):
        exists, in_scope, install_safe = _predicates(
            in_scope_set={self.target}, exists_set={self.target},
            safe_set={self.target})
        v = write_guard_lib.decide(
            "Write", {"file_path": self.target},
            exists, in_scope, install_safe, GUARD_PATH)
        self.assertIs(v.allowed, False)
        self.assertIn(self.target, v.reason)
        self.assertIn("Edit", v.reason)
        self.assertIn("stamp", v.reason)
        self.assertIn("install", v.reason)
        self.assertIn(GUARD_PATH, v.reason)
        self.assertIn("re-read", v.reason)
        i_stamp = v.reason.index("stamp")
        i_reread = v.reason.index("re-read")
        i_install = v.reason.index("install")
        self.assertLess(i_stamp, i_reread)
        self.assertLess(i_reread, i_install)

    # AC1.1b -- existing, in-scope, NOT install-safe + Write -> denied, Edit only
    def test_existing_in_scope_not_install_safe_write_is_denied_edit_only(self):
        exists, in_scope, install_safe = _predicates(
            in_scope_set={self.target}, exists_set={self.target},
            safe_set=set())
        v = write_guard_lib.decide(
            "Write", {"file_path": self.target},
            exists, in_scope, install_safe, GUARD_PATH)
        self.assertIs(v.allowed, False)
        self.assertIn(self.target, v.reason)
        self.assertIn("Edit", v.reason)
        self.assertNotIn("stamp", v.reason)
        self.assertNotIn("install", v.reason)
        self.assertNotIn(GUARD_PATH, v.reason)

    # AC1.2 -- same path, target absent -> allowed, reason None
    def test_target_absent_is_allowed(self):
        exists, in_scope, install_safe = _predicates(
            in_scope_set={self.target}, exists_set=set(),
            safe_set={self.target})
        v = write_guard_lib.decide(
            "Write", {"file_path": self.target},
            exists, in_scope, install_safe, GUARD_PATH)
        self.assertIs(v.allowed, True)
        self.assertIsNone(v.reason)

    # AC1.3 -- Edit / MultiEdit are never judged
    def test_edit_and_multiedit_are_always_allowed(self):
        exists, in_scope, install_safe = _predicates(
            in_scope_set={self.target}, exists_set={self.target},
            safe_set={self.target})
        for name in ("Edit", "MultiEdit"):
            v = write_guard_lib.decide(
                name, {"file_path": self.target},
                exists, in_scope, install_safe, GUARD_PATH)
            self.assertIs(v.allowed, True)
            self.assertIsNone(v.reason)

    # AC1.4 -- existing, out-of-scope + Write -> allowed
    def test_out_of_scope_write_is_allowed(self):
        exists, in_scope, install_safe = _predicates(
            in_scope_set=set(), exists_set={self.target}, safe_set=set())
        v = write_guard_lib.decide(
            "Write", {"file_path": self.target},
            exists, in_scope, install_safe, GUARD_PATH)
        self.assertIs(v.allowed, True)
        self.assertIsNone(v.reason)

    # AC1.5 -- given form out of scope, realpath in scope (real symlink) -> denied
    def test_realpath_widens_scope_through_a_real_symlink(self):
        real_target = os.path.join(self._tmp.name, "real_note.md")
        with open(real_target, "w", encoding="utf-8") as handle:
            handle.write("x")
        link_path = os.path.join(self._tmp.name, "link_note.md")
        os.symlink(real_target, link_path)
        resolved = os.path.realpath(real_target)
        # in_scope is true ONLY for the resolved realpath -- never for the
        # given (symlinked) path -- so this exercises decide()'s own
        # "or in_scope(realpath(path))" widening, not in_scope's.
        exists, in_scope, install_safe = _predicates(
            in_scope_set={resolved}, exists_set={link_path}, safe_set=set())
        v = write_guard_lib.decide(
            "Write", {"file_path": link_path},
            exists, in_scope, install_safe, GUARD_PATH)
        self.assertIs(v.allowed, False)

    # AC1.2 drop of realpath scoping also must not fire when NEITHER form
    # is in scope, kept as a companion to AC1.5 so the widening is bounded.
    def test_a_symlink_whose_realpath_is_also_out_of_scope_is_allowed(self):
        real_target = os.path.join(self._tmp.name, "real_note.md")
        with open(real_target, "w", encoding="utf-8") as handle:
            handle.write("x")
        link_path = os.path.join(self._tmp.name, "link_note.md")
        os.symlink(real_target, link_path)
        exists, in_scope, install_safe = _predicates(
            in_scope_set=set(), exists_set={link_path}, safe_set=set())
        v = write_guard_lib.decide(
            "Write", {"file_path": link_path},
            exists, in_scope, install_safe, GUARD_PATH)
        self.assertIs(v.allowed, True)
        self.assertIsNone(v.reason)

    # Fix 1(a) -- install-safety is judged on the REALPATH only. A symlink
    # whose GIVEN form looks like a memory file (install-safe by name) but
    # whose REALPATH is a rules file must NOT offer the stamp/install route
    # -- `distill_guard.py install` resolves `os.path.realpath(target)` and
    # derives the archive from THAT, so an install keyed on the given path
    # would archive to the rules tree, not the memory tree.
    def test_install_route_is_judged_on_the_realpath_not_the_given_path(self):
        real_target = os.path.join(self._tmp.name, "repo", ".claude", "rules")
        os.makedirs(real_target, exist_ok=True)
        real_target = os.path.join(real_target, "r.md")
        with open(real_target, "w", encoding="utf-8") as handle:
            handle.write("x")
        link_dir = os.path.join(self._tmp.name, "proj", "memory")
        os.makedirs(link_dir, exist_ok=True)
        link_path = os.path.join(link_dir, "note.md")
        os.symlink(real_target, link_path)
        resolved = os.path.realpath(real_target)

        # in_scope: true only via the realpath widening (a rules file) --
        # exercises decide()'s existing "or in_scope(realpath(path))" half,
        # left unchanged by this fix.
        # install_safe: true only for the GIVEN (memory-shaped) path, never
        # for the realpath (a rules file) -- this is what distinguishes the
        # old "install_safe(path) or install_safe(real)" from the fixed
        # "install_safe(real)" alone.
        exists, in_scope, install_safe = _predicates(
            in_scope_set={resolved}, exists_set={link_path},
            safe_set={link_path})
        v = write_guard_lib.decide(
            "Write", {"file_path": link_path},
            exists, in_scope, install_safe, GUARD_PATH)
        self.assertIs(v.allowed, False)
        self.assertNotIn("stamp", v.reason)
        self.assertNotIn("install", v.reason)

    # AC1.6 -- malformed tool_input shapes -> allowed
    def test_malformed_tool_input_is_allowed(self):
        exists, in_scope, install_safe = _predicates(
            in_scope_set={self.target}, exists_set={self.target},
            safe_set={self.target})
        for payload in (None, "x", {}, {"file_path": 7}, {"file_path": ""}):
            v = write_guard_lib.decide(
                "Write", payload, exists, in_scope, install_safe, GUARD_PATH)
            self.assertIs(v.allowed, True)
            self.assertIsNone(v.reason)

    # AC1.7 -- a raising predicate never escapes decide()
    def test_a_raising_exists_predicate_is_swallowed(self):
        _ok_exists, ok_in_scope, ok_safe = _predicates(
            in_scope_set={self.target}, exists_set={self.target},
            safe_set={self.target})
        v = write_guard_lib.decide(
            "Write", {"file_path": self.target},
            _raiser, ok_in_scope, ok_safe, GUARD_PATH)
        self.assertIs(v.allowed, True)
        self.assertIsNone(v.reason)

    def test_a_raising_in_scope_predicate_is_swallowed(self):
        ok_exists, _ok_in_scope, ok_safe = _predicates(
            in_scope_set={self.target}, exists_set={self.target},
            safe_set={self.target})
        v = write_guard_lib.decide(
            "Write", {"file_path": self.target},
            ok_exists, _raiser, ok_safe, GUARD_PATH)
        self.assertIs(v.allowed, True)
        self.assertIsNone(v.reason)

    # AC1.8 -- allowed/denied verdicts compared with assertIs, not truthiness
    def test_verdict_allowed_field_is_a_real_bool_not_a_truthy_value(self):
        exists, in_scope, install_safe = _predicates(
            in_scope_set={self.target}, exists_set={self.target},
            safe_set={self.target})
        allowed_verdict = write_guard_lib.decide(
            "Edit", {"file_path": self.target},
            exists, in_scope, install_safe, GUARD_PATH)
        denied_verdict = write_guard_lib.decide(
            "Write", {"file_path": self.target},
            exists, in_scope, install_safe, GUARD_PATH)
        self.assertIs(allowed_verdict.allowed, True)
        self.assertIs(denied_verdict.allowed, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
