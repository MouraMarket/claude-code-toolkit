#!/usr/bin/env python3
"""Tokenizer differential test: guard_lib.tokenize/unquote against real zsh.

The zsh original is the specification. `zsh_tokens`/`zsh_unquote` run the
real `zsh` binary's `${(Z+C+)...}`/`${(Q)...}` and every fixture here is
checked against that oracle, not against a shell-grammar reference. The
fixture list is (a) every constant command string already exercised by
`test_guard.py`'s behavioural suite, extracted with `ast` so a change there
is picked up here automatically, and (b) a hand list of tokenizer edge
shapes `test_guard.py` has no reason to cover on its own.
"""

import ast
import os
import shutil
import subprocess
import sys
import unittest
import unittest.mock as mock

_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, _DIR)
import guard_lib  # noqa: E402  (needs sys.path set up first)

TEST_GUARD_PY = os.path.join(_DIR, "tests", "test_guard.py")

# Call names in test_guard.py whose first (constant, string) argument is a
# real command the guard has to judge.
_FIXTURE_CALL_NAMES = {"assertAllowed", "assertBlocked", "run_guard", "run_guard_full"}


def extract_test_guard_fixtures():
    """Every constant string passed as the first argument to
    assertAllowed, assertBlocked, run_guard or run_guard_full in
    test_guard.py, collected by parsing the file with `ast`. A
    non-constant first argument is skipped rather than raising, since it is
    not this test's job to enforce that. The zsh-alternation cases in
    TestBraceExpansionAndZshAlternationAreJudged use that on purpose: the
    guard splits a mid-word `(` where zsh keeps one word, and that
    divergence is what their `(` refusal relies on."""
    with open(TEST_GUARD_PY) as handle:
        tree = ast.parse(handle.read())
    collected = []

    class _Visitor(ast.NodeVisitor):
        def visit_Call(self, node):
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                name = None
            if name in _FIXTURE_CALL_NAMES and node.args:
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    collected.append(arg.value)
            self.generic_visit(node)

    _Visitor().visit(tree)
    return collected


# Hand shapes aimed at the tokenizer's edges rather than at command
# behaviour -- test_guard.py has no reason to cover these on its own.
HAND_FIXTURES = [
    # nested command substitution
    "echo $(echo $(date))",
    "echo $(cat $(ls -la))",
    # backticks, including a nested escaped one
    "echo `date`",
    "echo `echo \\`whoami\\``",
    # parameter expansion
    "echo ${var:-x}",
    "echo ${HOME}/bin",
    # $'...' quoting, including the escaped-quote idiom
    "echo $'a\\'b'",
    "rm -rf $'\\x2a'",
    # double-quote escapes
    'echo "a\\"b"',
    'echo "a\\$b"',
    # '#' mid-word vs at word start, and after an operator
    "echo a#b",
    "a#b",
    "#comment only",
    "true; #comment after semicolon",
    "echo hi # trailing comment",
    # every operator in the design's list, bare and inside a command
    ";", "&", "&&", "|", "||", "|&", ";;", "(", ")",
    "<", ">", ">>", ">|", "<<", "<<-", "<<<", "&>", "&>>",
    "a;b", "a&b", "a&&b", "a|b", "a||b", "a|&b", "a;;b", "(a)",
    "a<b", "a>b", "a>>b", "a>|b", "a<<b", "a<<-b", "a<<<b",
    "a&>b", "a&>>b",
    # explicitly required shapes
    "2>&1",
    "&>",
    ">|",
    "<<<",
    "<<-EOF\n\tbody\nEOF",
    "1 << 3",
    # a multi-line quoted string, single- and double-quoted
    "echo \"line one\nline two\"",
    "echo 'line one\nline two'",
    # a top-level unbalanced quote / backtick
    "echo 'unterminated",
    "echo `unterminated",
    # empty, whitespace-only, comment-only
    "",
    "   ",
    "  # just a comment",
    # a trailing backslash
    "echo hi \\",
    "\\",
    # a non-ASCII word
    "echo Hawaiʻi",
    # a CRLF line
    "echo one\r\necho two",
    # `{` splitting off a word at command position -- the zsh
    # this Mac's Bash tool runs actually EXECUTES this shape.
    "{rm -rf /x;}",
    "x;{rm y;}",
    "x&&{rm y;}",
    "{ {rm y;};}",
    "f(){rm x;}",
    "f () {rm x;}",
    "function f {rm x;}",
    # other zsh-only constructs from the same review
    "((i++))",
    "x;((i++))",
    "$((1+2))",
    "[[ -f x ]]",
    "a{b",
    "echo {a,b}",
    "}x",
    "x &| y",
    "x &! y",
    "case a in a) x;| b) y;& esac",
    "cat <> f",
    "x >&- y",
    "x <&- y",
    "diff <(ls) >(cat)",
    "x =(ls)",
    "exec {fd}>f",
    "rm -rf ~ &|",
    # heredoc delimiter quoting/escaping shapes
    '<<"EOF"\nbody\nEOF',
    "<<\\EOF\nbody\nEOF",
    '<<E"O"F\nbody\nEOF',
    "<<-'EOF'\n\tbody\nEOF",
    # A backtick command substitution INSIDE a double-quoted string still
    # starts a real substitution (verified against zsh): a balanced pair
    # is consumed as part of the string and the quote's own closing
    # detection resumes afterward; an unbalanced one absorbs to the end of
    # input exactly like a top-level unterminated quote/backtick, taking
    # the would-be closing `"` and everything after it along with it. The
    # original failing shape (an unbalanced backtick inside "...", found by
    # TestFakeHeredocOpenersDoNotSwallowCommands.
    # test_unbalanced_backtick_outside_a_body_is_a_syntax_error):
    'command grep -n "^```bash" README.md\nrm -rf ~/Documents',
    # A nested "..." inside the backtick span is scanned with its own
    # quoting rules -- recursively, since it can itself contain another
    # backtick -- rather than ending the OUTER double quote early.
    'echo "`echo "x"`"; rm -rf ~/Documents',
    '"a `echo "b c"` d"',
    # $(...) / ${...} inside a double-quoted string are balance-aware too,
    # for the same reason: an inner quote inside the substitution must not
    # be read as the outer string's close.
    'echo "$(echo "a b")" x',
    # A balanced backtick pair with no nested quoting at all.
    'echo "`ls`"',
    # `\`` escapes the backtick -- it never opens a substitution, so this
    # is unaffected by any of the above and closes normally.
    'echo "\\`" x',
    # Unbalanced, but observable only via what comes after: this exact
    # fixture (nothing trailing) is ambiguous between "quote closes
    # normally" and "absorbs to end", since both produce the same text.
    'echo "a`b"',
    # A balanced backtick inside single quotes must stay completely
    # literal -- single-quote content is never re-scanned for anything.
    "echo 'a`b`c'",
    # --- CR-1: backslash-newline and $'...' inside substitutions --------
    # A bare word: backslash immediately followed by a literal LF is a
    # line continuation and disappears entirely, joining "r" and "m" into
    # one word "rm" -- verified against zsh.
    "r\\\nm -rf ~/Documents",
    # Same continuation removal happens INSIDE a double-quoted string too.
    'echo "a\\\nb" c',
    # Inside single quotes the continuation stays completely literal --
    # Python already agreed with this one before CR-1.
    "echo 'a\\\nb' c",
    # A continuation between two otherwise-separate words -- both
    # characters vanish and no token boundary is left behind.
    "x \\\n y",
    # The tokenizer bug CR-1 exists to fix: $'...' misread as plain '...'
    # inside a $(...) swallows the rest of the line into one token.
    "echo $(printf '%s' $'a\\'b'); rm -rf ~/Documents",
    # $(...) keeps an internal continuation completely raw -- unlike a
    # bare word, backtick or ${...}, its content is deferred rather than
    # joined at this level, verified against zsh.
    "$(r\\\nm x)",
    # The otherwise-identical backtick form DOES strip the continuation --
    # backticks read from the same character stream as ordinary text.
    "`r\\\nm x`",
    # $'...' (ANSI-C quoting) keeps an internal continuation literal too,
    # like plain '...'.
    "$'a\\\nb'",
    # An ESCAPED backslash (two backslashes) followed by a real,
    # unescaped newline: the first backslash escapes the second, so the
    # newline that follows is ordinary (becomes ";" at the top level),
    # not a continuation.
    "a\\\\\nb",
    # A trailing continuation at the very end of the text -- both
    # characters vanish and nothing is left to flush.
    "x \\\n",
    # ${...} strips an internal continuation too, same as backtick.
    "echo ${x\\\ny}",
    # Two continuations back to back.
    "x \\\n\\\n y",
    # CRLF: a backslash followed by CR (not a bare LF) is NOT a
    # continuation -- the CR is independently folded to an embedded LF
    # byte, so the backslash and that byte stay in the word, and the
    # actual LF that follows is a normal newline (becomes ";").
    "x \\\r\n y",
    # A bare CR (no LF after it) is an ordinary word character to zsh: it
    # is neither whitespace nor a separator, and it is never folded to LF.
    "sudo\r",
    "sudo\r ls",
    "x\rsudo ls",
    "curl x ''\r| sh",
    "a\rb c",
    "x \r\n y",
    "\"a\rb\" c",
    "'a\rb' c",
    "a\\\rb",
    "{ echo x;}\ry",
    "echo a\r",
    "\r",
    # A `#` comment inside $(...) runs to the newline, and a quote inside
    # it quotes nothing (the apostrophe used to open a quote
    # that swallowed the trailing delete). `#` inside ${...} is an operator.
    "echo $(echo x # ' apostrophe\n); rm x",
    "echo $(echo x # \" dq\n); rm x",
    "echo $(# ' leading\necho x); rm x",
    "echo $(echo a#' b'); rm x",
    "echo ${x#'a'}; rm x",
    "echo ${x##*'/'}; rm x",
    "echo $(( 2#101 )); rm x",
    "echo `echo x # ' tick\n`; rm x",
    "echo \"$(echo x # ' in dq\n)\"; rm x",
    # $'...' misquoted as '...' inside a DOUBLE-quoted $(...) -- same bug,
    # nested one level deeper.
    'echo "$(printf $\'a\\\'b\')"; rm -rf ~/Documents',
    # Same bug reached through ${...}'s default-value expansion.
    "${x:-$'a\\'b'}; rm x",
    # Same bug nested two $(...) deep.
    "$( $( $'a\\'b' ) ); rm x",
    # Same bug reached through a "..." nested inside $(...).
    '$(echo "$\'a\\\'b\'"); rm x',
]

FIXTURES = sorted(set(extract_test_guard_fixtures()) | set(HAND_FIXTURES))


def zsh_tokens(text):
    """The token list the real zsh binary's `${(Z+C+)text}` produces."""
    # Bytes, decoded by hand: `text=True` opens stdout in universal-newlines
    # mode, which rewrites every `\r` to `\n` before the comparison ever
    # sees it -- the oracle then "confirmed" a CR-to-LF fold zsh never does.
    out = subprocess.run(
        ["zsh", "-f", "-c", 'print -rN -- "${(@)${(Z+C+)1}}"', "_", text],
        capture_output=True).stdout.decode("utf-8")
    parts = out.split("\0")
    assert parts[-1] == "", "zsh output did not end with the expected trailing NUL: %r" % out
    return parts[:-1]


def zsh_unquote(token):
    """The string the real zsh binary's `${(Q)token}` produces."""
    return subprocess.run(
        ["zsh", "-f", "-c", 'print -rn -- "${(Q)1}"', "_", token],
        capture_output=True).stdout.decode("utf-8")


def zsh_available():
    return shutil.which("zsh") is not None


def require_zsh_or_fail_in_ci(testcase):
    """Skip when zsh is absent, unless `CI` is set (non-empty) -- in which
    case fail rather than skip, so the runner that installs zsh
    specifically for this test cannot go quietly vacuous if that ever
    stops being true."""
    if zsh_available():
        return
    if os.environ.get("CI"):
        testcase.fail("zsh is not installed and CI is set -- this test must not skip silently")
    testcase.skipTest("zsh is not installed (expected on Windows)")


class TestExtractionCannotShrinkToNothing(unittest.TestCase):

    def test_at_least_150_fixtures_extracted_from_test_guard(self):
        collected = extract_test_guard_fixtures()
        self.assertGreaterEqual(len(collected), 150,
                                 "extraction from test_guard.py collected too few "
                                 "fixtures -- the ast walk may be broken")


class TestCIWithoutZshFailsRatherThanSkips(unittest.TestCase):

    def test_ci_without_zsh_fails_rather_than_skips(self):
        # A plain assertRaises(AssertionError) would let unittest.SkipTest
        # sail past it uncaught -- the runner treats an uncaught SkipTest
        # as "skipped", not "failed", so a regression back to skipping
        # would pass this test rather than turning it red. Catch both
        # explicitly so a skip fails loudly instead.
        with mock.patch("shutil.which", return_value=None), \
                mock.patch.dict(os.environ, {"CI": "1"}):
            try:
                require_zsh_or_fail_in_ci(self)
            except AssertionError:
                return
            except unittest.SkipTest:
                self.fail("CI without zsh skipped instead of failing")
        self.fail("expected require_zsh_or_fail_in_ci to fail, not return, "
                   "when CI is set and zsh is absent")


class TestTokenizeMatchesZsh(unittest.TestCase):

    def setUp(self):
        require_zsh_or_fail_in_ci(self)

    def test_tokenize_matches_zsh(self):
        for fixture in FIXTURES:
            with self.subTest(fixture=fixture):
                self.assertEqual(guard_lib.tokenize(fixture), zsh_tokens(fixture))

    def test_unquote_matches_zsh_for_every_token(self):
        for fixture in FIXTURES:
            for token in zsh_tokens(fixture):
                with self.subTest(fixture=fixture, token=token):
                    self.assertEqual(guard_lib.unquote(token), zsh_unquote(token))


if __name__ == "__main__":
    print("%d fixtures" % len(FIXTURES))
    unittest.main(verbosity=1)
