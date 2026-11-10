#!/usr/bin/env python3
"""A write through an output redirect is never free and never prefix-covered.

Claude Code judges the redirect target separately from the command: `echo x > a`
prompts under no rule and under `Bash(echo *)`, and is free only under a blanket
`Bash(*)` grant. `/dev/null` and fd copies (`2>&1`) are exempt (measured
2026-10-09 on 2.1.292). Tests call the scripts' own splitter, as both do.
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from scan_transcripts import classify, output_redirects, subcommands  # noqa: E402
from replay_permissions import would_prompt  # noqa: E402
from test_scripts_end_to_end import ScriptCase, tool_use  # noqa: E402

REASON = "output redirect (no prefix rule covers the write)"

TARGETS = [
    ("echo x > f", ["f"]),
    ("echo x >> f", ["f"]),
    ("echo x >| f", ["f"]),
    ("cmd &> f", ["f"]),
    ("cmd &>> f", ["f"]),
    ("cmd >&f", ["f"]),
    ("cmd 2> err.log", ["err.log"]),
    ("cmd >out", ["out"]),
    ('cmd > "a b"', ["a b"]),
    ("cmd > $OUT", ["$OUT"]),
    ("cmd 2>&1", []),
    ("cmd 1>&2", []),
    ("cmd >&-", []),
    ("cmd > /dev/null", []),
    ("cmd 2>/dev/null", []),
    ("cmd &>/dev/null", []),
    ('echo "a > b"', []),
    ("echo 'a > b'", []),
    # a quoted `>` followed by a cleanly ending word pins the quote tracking
    # itself (the two rows above survived with it disabled)
    ('echo "a > b" c', []),
    # a word-initial unquoted `#` starts a comment (Merge Review); a `#`
    # inside a word or inside quotes does not
    ("echo ok # > out.txt", []),
    ("echo ok #> out.txt", []),
    ("echo a#b > out", ["out"]),
    ('echo "#" > out', ["out"]),
    ("echo \\# > out", ["out"]),
    ("echo 'x > y' z > out", ["out"]),
    ("echo a \\> b", []),
    ("cmd > >(tee x)", None),
    ("cmd >", None),
    ("ls", []),
    ('echo "unterminated > x', []),
    ("cmd > a > b", ["a", "b"]),
    ('echo "a\\"b" > out', ["out"]),
    ("echo 'a\\' > out", ["out"]),
    ('echo a\\"b > out', ["out"]),
]


class TestOutputRedirects(unittest.TestCase):
    def test_table(self):
        for cmd, expected in TARGETS:
            with self.subTest(cmd=cmd):
                self.assertEqual(output_redirects(cmd), expected)

    def test_pipe_clobber_is_one_segment(self):
        self.assertEqual(len(list(subcommands("echo x >| f"))), 1)


class TestHeredocs(unittest.TestCase):
    def test_redirect_on_the_opening_line_survives(self):
        segs = list(subcommands("cat <<EOF > out\nhello\nEOF"))
        self.assertEqual([classify(s) for s in segs], [("cat >", "no-prefix")])

    def test_pipe_on_the_opening_line_survives(self):
        segs = list(subcommands("cat <<'EOF' | python3\nx\nEOF"))
        self.assertEqual([classify(s)[0] for s in segs], ["cat", "python3"])

    def test_dash_stdin_with_redirect(self):
        segs = list(subcommands("python3 - <<'EOF' > out.txt\nprint(1)\nEOF"))
        self.assertEqual([classify(s) for s in segs],
                         [("python3 >", "no-prefix")])

    def test_a_redirect_in_the_body_is_not_a_write(self):
        cmd = "git commit -m \"$(cat <<'EOF'\nmsg > x\nEOF\n)\""
        for seg in subcommands(cmd):
            with self.subTest(seg=seg):
                self.assertEqual(output_redirects(seg), [])


class TestProcessSubstitution(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify("find . -name >(cat)"),
                         ("find >", "no-prefix"))

    def test_would_prompt(self):
        cmd = "find . -name >(cat)"
        self.assertEqual(would_prompt(cmd, []), REASON)
        self.assertEqual(would_prompt(cmd, ["find *"]), REASON)
        self.assertIsNone(would_prompt(cmd, ["*"]))


class TestClassify(unittest.TestCase):
    def test_writes(self):
        for cmd, expected in [
            ("echo x > f", ("echo >", "no-prefix")),
            ("cat a > b", ("cat >", "no-prefix")),
            ("git log > out", ("git log >", "no-prefix")),
            ("python3 x.py > out", ("python3 >", "no-prefix")),
        ]:
            with self.subTest(cmd=cmd):
                self.assertEqual(classify(cmd), expected)

    def test_exempt_redirects_stay_free(self):
        self.assertEqual(classify("echo x 2>&1"), ("echo", "already-free"))
        self.assertEqual(classify("echo x > /dev/null"),
                         ("echo", "already-free"))


class TestWouldPrompt(unittest.TestCase):
    def test_a_write_prompts_unless_bash_is_blanket_granted(self):
        self.assertEqual(would_prompt("echo x > f", []), REASON)
        self.assertEqual(would_prompt("echo x > f", ["echo *"]), REASON)
        self.assertIsNone(would_prompt("echo x > f", ["*"]))

    def test_shell_syntax_heads_still_prompt_on_a_write(self):
        # classify() returns None for these heads, yet each writes a file
        # (Code Review: zsh `> f` creates f). Replay must not consult it.
        for seg in ("> f", ": > f", "! echo x > f"):
            with self.subTest(seg=seg):
                self.assertEqual(would_prompt(seg, []), REASON)
                self.assertIsNone(would_prompt(seg, ["*"]))

    def test_a_quote_bleed_fragment_counts_as_a_write(self):
        # Declared divergence, loud direction: a line cut out of a multi-line
        # quoted argument is not a command natively, but replay cannot tell.
        frag = "script)[^>]*>.*?</\\1>','',s)"
        self.assertIsNone(classify(frag))
        self.assertEqual(would_prompt(frag, []), REASON)

    def test_exempt_redirect_is_free(self):
        self.assertIsNone(would_prompt("echo x 2>&1", []))

    def test_a_prefix_rule_does_not_cover_a_write(self):
        self.assertEqual(would_prompt("python3 x.py > out", ["python3 *"]),
                         REASON)


class TestScannerEndToEnd(ScriptCase):
    def test_json_buckets(self):
        self.write_transcript([
            tool_use("Bash", command="echo x > f"),
            tool_use("Bash", command="echo x > f"),
            tool_use("Bash", command="echo hi"),
        ])
        self.write_settings()
        r = self.run_script("scan_transcripts.py", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(dict(out["no_prefix_rule"])["echo >"], 2)
        self.assertEqual(dict(out["already_free"])["echo"], 1)


class TestReplayEndToEnd(ScriptCase):
    def test_a_prefix_rule_does_not_cover_a_write(self):
        self.write_transcript([
            tool_use("Bash", command="echo x > f"),
            tool_use("Bash", command="echo x > f"),
            tool_use("Bash", command="echo hi"),
        ])
        self.write_settings(allow=["Bash(echo *)"])
        r = self.run_script("replay_permissions.py", "--threshold", "100")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(REASON, r.stdout)
        self.assertIn("Would prompt: 2/", r.stdout)

    def test_a_blanket_grant_covers_it(self):
        self.write_transcript([
            tool_use("Bash", command="echo x > f"),
            tool_use("Bash", command="echo x > f"),
            tool_use("Bash", command="echo hi"),
        ])
        self.write_settings(allow=["Bash(*)"])
        r = self.run_script("replay_permissions.py", "--threshold", "100")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Would prompt: 0/", r.stdout)

    def test_quoted_escape_and_heredoc_writes_prompt(self):
        self.write_transcript([
            tool_use("Bash", command='echo "a\\"b" > out'),
            tool_use("Bash", command="cat <<EOF > out\nhello\nEOF"),
        ])
        self.write_settings()
        r = self.run_script("replay_permissions.py", "--threshold", "100")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(REASON, r.stdout)
        self.assertIn("Would prompt: 2/", r.stdout)


if __name__ == "__main__":
    unittest.main()
