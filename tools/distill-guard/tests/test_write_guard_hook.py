"""Subprocess tests for claude-distill-write-guard, the PreToolUse hook that
denies a whole-file `Write` over an existing in-scope context file.

Written test-first per the plan: run once against a stub executable that
exits 0 and prints nothing (the RED baseline -- every deny-shaped assertion
below must fail against it), then against the real implementation.

Drives the REAL executable via `subprocess.run([sys.executable, HOOK], ...)`
(or the executable directly for the symlink case, AC2.7 -- that is the
`~/.local/bin` invocation shape it exists to guard). `HOME` is a sandbox
distinct from any repo path, `CLAUDE_TOOLKIT_CONFIG` is set explicitly per
test (never left to whatever the running machine has configured), and every
`GIT_*` env var is stripped -- see smoke_lib.sandbox_env's docstring for why
an inherited GIT_DIR is not a hypothetical.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

TOOLS_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HOOK = os.path.join(TOOLS_DIR, "claude-distill-write-guard")
GUARD_PATH = os.path.realpath(os.path.join(TOOLS_DIR, "distill_guard.py"))


def _sandbox_env(tmp, config_path=None):
    """A fresh env for one subprocess call: every GIT_* var dropped, HOME
    pointed at a tempdir under `tmp`, CLAUDE_TOOLKIT_CONFIG set explicitly
    (a nonexistent path by default, never the running machine's real config).
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    home = os.path.join(tmp, "home")
    os.makedirs(home, exist_ok=True)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["CLAUDE_TOOLKIT_CONFIG"] = config_path or os.path.join(tmp, "nonexistent.json")
    return env


def run_hook(tmp, stdin_text, config_path=None, executable=HOOK, via_python=True):
    """Run `executable` with `stdin_text` on stdin; return the CompletedProcess.

    `via_python=True` (the default) invokes `[sys.executable, executable]`,
    per the plan. `via_python=False` runs `executable` directly -- the
    ~/.local/bin symlink invocation shape AC2.7 guards.
    """
    argv = [sys.executable, executable] if via_python else [executable]
    return subprocess.run(argv, input=stdin_text, capture_output=True,
                          text=True, env=_sandbox_env(tmp, config_path))


def _payload(tool_name, file_path, extra_input=None):
    tool_input = {"file_path": file_path, "content": "x"}
    if extra_input:
        tool_input.update(extra_input)
    return json.dumps({"tool_name": tool_name, "tool_input": tool_input})


class WriteGuardHookTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, relpath, text="x"):
        path = os.path.join(self.tmp, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def _deny_reason(self, proc):
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        out = payload["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")
        return out["permissionDecisionReason"]

    # AC2.1 -- existing memory entry + Write -> denied, reason names the
    # realpath of distill_guard.py
    def test_existing_memory_entry_write_is_denied(self):
        target = self._write("proj/memory/note.md")
        proc = run_hook(self.tmp, _payload("Write", target))
        reason = self._deny_reason(proc)
        self.assertIn(GUARD_PATH, reason)
        self.assertIn(target, reason)

    # AC2.2 -- a second scope branch: existing CLAUDE.md + Write -> denied
    def test_existing_claude_md_write_is_denied(self):
        target = self._write("repo/CLAUDE.md")
        proc = run_hook(self.tmp, _payload("Write", target))
        reason = self._deny_reason(proc)
        self.assertIn(target, reason)

    # AC2.3 -- non-existent target + Write -> exit 0, empty stdout
    def test_new_file_write_is_allowed_silently(self):
        target = os.path.join(self.tmp, "proj", "memory", "new.md")
        proc = run_hook(self.tmp, _payload("Write", target))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    # AC2.4 -- existing in-scope file + Edit -> exit 0, empty stdout
    def test_edit_over_in_scope_file_is_allowed_silently(self):
        target = self._write("proj/memory/note.md")
        proc = run_hook(self.tmp, _payload("Edit", target))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    # AC2.5 -- existing out-of-scope file + Write -> exit 0, empty stdout
    def test_out_of_scope_write_is_allowed_silently(self):
        target = self._write("notes.txt")
        proc = run_hook(self.tmp, _payload("Write", target))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    # AC2.6 -- malformed stdin -> exit 0, empty stdout, and (Fix 2) EMPTY
    # stderr -- a parse failure / non-dict payload is one of the two
    # expected-silent paths, not an "unexpected exception".
    def test_malformed_stdin_is_allowed_silently(self):
        for stdin_text in ("not json", "[]", ""):
            proc = run_hook(self.tmp, stdin_text)
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertEqual(proc.stderr, "")

    # AC2.7 -- through a symlink (the ~/.local/bin shape): realpath must
    # resolve to the REAL script directory, not the symlink's directory.
    def test_through_a_symlink_the_realpath_rule_still_finds_distill_guard(self):
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir, exist_ok=True)
        link = os.path.join(bin_dir, "claude-distill-write-guard")
        os.symlink(HOOK, link)
        target = self._write("proj/memory/note.md")
        proc = run_hook(self.tmp, _payload("Write", target),
                        executable=link, via_python=False)
        reason = self._deny_reason(proc)
        self.assertIn(GUARD_PATH, reason)

    # AC2.8 -- a deny's stdout is exactly one JSON document and no value in
    # it is "allow". Checked on the PARSED values, never a substring of raw
    # stdout: the deny reason embeds the hook's absolute path, so a raw
    # search failed in any checkout whose path contains "allow".
    def test_deny_stdout_is_exactly_one_json_document_and_never_says_allow(self):
        target = self._write("proj/memory/note.md")
        proc = run_hook(self.tmp, _payload("Write", target))
        self._deny_reason(proc)  # already asserts json.loads succeeds

        def values(node):
            if isinstance(node, dict):
                for child in node.values():
                    yield from values(child)
            elif isinstance(node, list):
                for child in node:
                    yield from values(child)
            else:
                yield node

        self.assertNotIn("allow", list(values(json.loads(proc.stdout))))

    # AC2.9 -- config is wired: a configured tooling-map name reaches
    # in_scope, and the no-config run (a different default name) allows the
    # same path -- the pair shows the config made the difference.
    def test_config_reaches_in_scope_and_carries_no_growth_enable_key(self):
        ws = os.path.join(self.tmp, "ws")
        os.makedirs(ws, exist_ok=True)
        target = os.path.join(ws, "MAP.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")

        config_path = os.path.join(self.tmp, "config.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({
                "tooling_digest": {"path": target},
                "context_growth": {"enabled": False},
            }, handle)

        proc = run_hook(self.tmp, _payload("Write", target),
                        config_path=config_path)
        self._deny_reason(proc)

        proc_no_config = run_hook(self.tmp, _payload("Write", target))
        self.assertEqual(proc_no_config.returncode, 0)
        self.assertEqual(proc_no_config.stdout, "")

    # AC2.10 -- unexpected failure (a missing sibling import) still allows the
    # write (exit 0, no permissionDecision) but reports the crash in the
    # shared guard crash contract: copy ONLY the hook and write_guard_lib.py,
    # no tools/context-growth, no lib/ -- `import growth_lib` fails inside the
    # wrapper.
    def test_a_missing_growth_lib_import_is_reported_and_allowed(self):
        tree = os.path.join(self.tmp, "tree", "tools", "distill-guard")
        os.makedirs(tree, exist_ok=True)
        import shutil
        shutil.copy(HOOK, os.path.join(tree, "claude-distill-write-guard"))
        shutil.copy(os.path.join(TOOLS_DIR, "write_guard_lib.py"),
                    os.path.join(tree, "write_guard_lib.py"))
        os.chmod(os.path.join(tree, "claude-distill-write-guard"), 0o755)

        target = self._write("proj/memory/note.md")
        copy_path = os.path.join(tree, "claude-distill-write-guard")
        proc = run_hook(self.tmp, _payload("Write", target), executable=copy_path)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assertNotIn("permissionDecision", proc.stdout)
        report = json.loads(proc.stdout)  # ONE JSON object, or this raises
        self.assertEqual(set(report), {"systemMessage", "hookSpecificOutput"})
        self.assertTrue(report["systemMessage"].startswith(
            "claude-distill-write-guard crashed and did NOT check this write: "
            "ModuleNotFoundError"), report["systemMessage"])

    # AC2.11 -- a rules-file target: denied, reason offers Edit only (no
    # stamp/install -- rules files are not install-safe)
    def test_rules_file_write_is_denied_edit_only(self):
        target = self._write("repo/.claude/rules/r.md")
        proc = run_hook(self.tmp, _payload("Write", target))
        reason = self._deny_reason(proc)
        self.assertIn(target, reason)
        self.assertIn("Edit", reason)
        self.assertNotIn("stamp", reason)
        self.assertNotIn("install", reason)

    # Every refusal, both shapes, closes the Bash route in words.
    # Asserted on the reason's LAST line: the line belongs after the archive
    # explanation, and a document-wide search would pass it anywhere.
    def test_every_refusal_ends_by_closing_the_bash_route(self):
        for rel in ("proj/memory/note.md", "repo/.claude/rules/r.md"):
            with self.subTest(rel=rel):
                target = self._write(rel)
                reason = self._deny_reason(
                    run_hook(self.tmp, _payload("Write", target)))
                last = reason.strip().splitlines()[-1]
                self.assertIn("Do not route around this with Bash", last)
                self.assertIn("cat >", last)
                self.assertIn("cp`/`mv", last)

    # Fix 1(b) -- a target that is nominally install-safe by basename
    # (CLAUDE.md) but sits inside a rules tree must NOT offer stamp/install:
    # `install`'s sibling archive there would land in `.claude/rules/`,
    # itself loaded as a rule.
    def test_claude_md_inside_a_rules_tree_is_denied_edit_only(self):
        target = self._write("repo/.claude/rules/CLAUDE.md")
        proc = run_hook(self.tmp, _payload("Write", target))
        reason = self._deny_reason(proc)
        self.assertIn(target, reason)
        self.assertIn("Edit", reason)
        self.assertNotIn("stamp", reason)
        self.assertNotIn("install", reason)

    # Fix 1(c) -- a topic file is in scope but never install-safe.
    def test_topic_file_write_is_denied_edit_only(self):
        target = self._write("proj/memory/topics/t.md")
        proc = run_hook(self.tmp, _payload("Write", target))
        reason = self._deny_reason(proc)
        self.assertIn(target, reason)
        self.assertIn("Edit", reason)
        self.assertNotIn("stamp", reason)
        self.assertNotIn("install", reason)

    # Fix 1 (this round) -- a CLAUDE.md-family file sitting inside a rules
    # tree but named `CLAUDE.md.template` (not `.md`) escapes `is_rules_file`
    # on the TARGET alone, since that predicate requires a `.md` suffix. The
    # hook must still refuse the stamp/install route here: `p` itself
    # contains a `/.claude/rules/` segment.
    def test_claude_md_template_inside_a_rules_tree_is_denied_edit_only(self):
        target = self._write("repo/.claude/rules/CLAUDE.md.template")
        proc = run_hook(self.tmp, _payload("Write", target))
        reason = self._deny_reason(proc)
        self.assertIn(target, reason)
        self.assertIn("Edit", reason)
        self.assertNotIn("stamp", reason)
        self.assertNotIn("install", reason)

    # Companion control: the SAME basename at the repo root (not under
    # .claude/rules/) is unaffected and still offers the stamp/install route.
    def test_claude_md_template_outside_a_rules_tree_still_offers_the_route(self):
        target = self._write("repo/CLAUDE.md.template")
        proc = run_hook(self.tmp, _payload("Write", target))
        reason = self._deny_reason(proc)
        self.assertIn(target, reason)
        self.assertIn("stamp", reason)
        self.assertIn("install", reason)

    # Fix 2 -- the emitted stamp/install command lines are shell-quoted, so a
    # target path containing spaces still runs correctly when pasted as-is.
    def test_emitted_stamp_command_is_shell_quoted_and_runs(self):
        target = self._write("project with spaces/CLAUDE.md")
        proc = run_hook(self.tmp, _payload("Write", target))
        reason = self._deny_reason(proc)

        stamp_line = None
        for line in reason.split("\n"):
            if "1. " in line:
                stamp_line = line.split("1. ", 1)[1]
                break
        self.assertIsNotNone(stamp_line, "no '1. ' line found in reason:\n%s" % reason)

        stamp_proc = subprocess.run(stamp_line, shell=True, capture_output=True,
                                    text=True, env=_sandbox_env(self.tmp))
        self.assertEqual(stamp_proc.returncode, 0, stamp_proc.stderr)
        self.assertRegex(stamp_proc.stdout.strip(), r"^\d+:\d+$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
