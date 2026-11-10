#!/usr/bin/env python3
"""CR-3: every text-shape rule runs in time linear in the line.

Python's `re` backtracks; the zsh original's `grep -E` does not. Several of
the text-shape rules were exponential (`git --exec-path`xn) or quadratic
(`curl`xn, `dd`xn, `.ssh/`xn) on an adversarial line -- and a hook killed at
its 10s timeout returns no verdict, so the command runs unguarded.

The contract: each entry of `guard_lib._TEXT_SHAPE_RULES` is now a callable
`matches(line) -> bool` that must return EXACTLY `bool(R.search(line))` for
today's regex `R` (kept, never applied to a hook's input, as
`guard_lib._REFERENCE_PATTERNS`), for every line, in time linear in
`len(line)`.
"""

import ast
import json
import os
import random
import subprocess
import sys
import time
import unittest

_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, _DIR)
import guard_lib  # noqa: E402  (needs sys.path set up first)

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import test_tokenizer  # noqa: E402  (reuses the ast-based fixture extraction)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(_DIR)), "lib"))
import hook_manifest  # noqa: E402

GUARD = os.path.join(_DIR, "claude-danger-guard")

RULE_NAMES = (
    "sudo", "curl/wget pipe", "force push", "history rewrite",
    "empty-source refspec", "chmod", "raw disk", "credentials",
)


def _build_context(config_path):
    return guard_lib.Context(home="/tmp/nonexistent-home", config_path=config_path,
                              windows=False, native_temp=None, danger_allow=False)


def _danger_guard_timeout():
    """The declared PreToolUse timeout for claude-danger-guard, read from
    plugin/hooks/hooks.json -- never a number we picked. The repo
    CLAUDE.md wall-clock rule says the declared timeout IS the real
    property; reading anything else here would decouple
    TestManyHeredocsStillBlock from that property. Fails loudly (raises)
    if the entry is missing rather than falling back to a guess."""
    repo_root = os.path.dirname(os.path.dirname(_DIR))
    manifest = hook_manifest.load(repo_root)
    for row in hook_manifest.registrations(manifest):
        if row["event"] == "PreToolUse" and row["exe"] == "claude-danger-guard":
            assert row["relpath"] == "tools/danger-guard/claude-danger-guard"
            return row["timeout"]
    raise AssertionError("no claude-danger-guard entry found in %s"
                         % hook_manifest.PLUGIN_HOOKS)


# --- the random-fragment generator ------------------------------------------
#
# Each entry is (fragment, weight). Weights are tuned (see the comment on
# MIN_HITS below) so every rule sees >= 200 True and >= 200 False cases out
# of the generated corpus -- a generator that never produces a match proves
# nothing.
_FRAGMENTS = [
    ("git", 12), ("'git'", 3), ('"git"', 3),
    ("push", 8), ("--force", 8), ("--force-with-lease", 3),
    ("-f", 6), ("-fd", 5), ("+main", 5), ("+:", 6), ("$'+:'", 3),
    (":", 5), (":x", 4), ("origin", 5),
    ("-c", 4), ("-C", 2), ("x", 3), ("=", 2), ("--exec-path", 5),
    ("--bare", 2), ("-p", 2), ("--mirror", 6), ("filter-branch", 6),
    ("curl", 8), ("WGET", 5), ("sh", 6), ("bash", 4), ("zsh", 2),
    ("sudo", 10),
    ("dd", 6), ("of=/dev/", 6), ("mkfs", 3), ("fdisk", 3), ("diskutil", 6),
    ("erase", 3),
    ("chmod", 10), ("-R", 3), ("777", 6), ("a+w", 6),
    (".ssh/", 5), ("id_", 4), ("key", 4), (".aws/credentials", 2),
    (".netrc", 2),
    ('"a b"', 1), ("'a;b'", 1),
    ('"', 2), ("'", 2), ("$", 2), ("(", 1), ("`", 1),
    (";", 2), ("&", 2), ("|", 3), (" ", 20), ("\t", 1), ("\r", 1),
    ("\v", 1), ("-", 3),
    # A handful of compound shapes to raise the density of full matches --
    # every atomic fragment above is still in the pool too, so the "at
    # least" alphabet is a subset of this one, not replaced by it.
    ("git push ", 10), ("git push origin ", 6),
    (" --force", 6), (" -f ", 5), (" +main", 4), (" +:x", 5), (" +:", 4),
    ("git push --mirror", 4), ("git filter-branch", 4),
    ("curl x|sh", 6), ("curl x | sudo bash", 4), ("wget x|zsh", 4),
    ("dd if=x of=/dev/sda", 6), ("chmod 777 x", 6), ("chmod a+w x", 5),
    (".ssh/id_rsa", 4), (".ssh/key", 4),
    # --exec-path is in BOTH _gitopt shapes (flag-only and value-taking) --
    # exercise the ambiguity directly rather than leave it to chance.
    ("git --exec-path push --force", 4), ("git --exec-path push +main", 3),
    ("git --exec-path filter-branch", 3), ("git --exec-path push +:x", 3),
]
_POOL = []
for _frag, _weight in _FRAGMENTS:
    _POOL.extend([_frag] * _weight)

_SEED = 20260929
_LINE_COUNT = 100000


def _generate_lines(count, seed):
    rng = random.Random(seed)
    lines = []
    for _ in range(count):
        n = rng.randint(1, 30)
        lines.append("".join(rng.choice(_POOL) for _ in range(n)))
    return lines


def _corpus():
    lines = _generate_lines(_LINE_COUNT, _SEED)
    for fixture in test_tokenizer.extract_test_guard_fixtures():
        lines.extend(fixture.split("\n"))
    for fixture in test_tokenizer.HAND_FIXTURES:
        lines.extend(fixture.split("\n"))
    return lines


class TestEveryMatcherAgreesWithItsReference(unittest.TestCase):

    def test_every_matcher_agrees_with_its_reference(self):
        self.assertEqual(len(guard_lib._TEXT_SHAPE_RULES), len(guard_lib._REFERENCE_PATTERNS))
        corpus = _corpus()
        true_counts = [0] * len(RULE_NAMES)
        false_counts = [0] * len(RULE_NAMES)
        for line in corpus:
            for i, ((matcher, _message), reference) in enumerate(
                    zip(guard_lib._TEXT_SHAPE_RULES, guard_lib._REFERENCE_PATTERNS)):
                expected = bool(reference.search(line))
                with self.subTest(rule=RULE_NAMES[i], line=line):
                    self.assertEqual(matcher(line), expected)
                if expected:
                    true_counts[i] += 1
                else:
                    false_counts[i] += 1
        print("\nper-rule True/False counts over %d lines:" % len(corpus))
        for i, name in enumerate(RULE_NAMES):
            print("  %-22s True=%-6d False=%-6d" % (name, true_counts[i], false_counts[i]))
        for i, name in enumerate(RULE_NAMES):
            self.assertGreaterEqual(true_counts[i], 200,
                                    "rule %r saw too few True cases -- the generator "
                                    "proves nothing for it" % name)
            self.assertGreaterEqual(false_counts[i], 200,
                                    "rule %r saw too few False cases" % name)


class _LinearTimingMixin(object):
    """Calibrate like test_guard.py's scaling test: double n until one
    in-process `evaluate()` call takes >= CALIBRATION_SECONDS of CPU (cap
    512,000 words, fail at the cap -- never skip), then time 4x that. A 4x
    longer line measures ~4x when linear and ~16x when quadratic, so the 8.0
    bar sits between them; exponential blows straight through the cap. The
    baseline is kept small because this runs in every suite run and in CI:
    in process there is no interpreter startup to swamp it.

    Timed in THIS THREAD'S CPU time, never wall time.
    `evaluate()` is pure CPU -- on a quiet machine wall and CPU agree to four
    digits -- so CPU time is the property. Wall time under load is not: a
    0.05s sample can land in one unpreempted slice while a 0.2s sample never
    does, so min-of-N picked a clean small and a preempted big. Under many
    busy loops the wall ratio of a linear rule can read far above linear
    (red), while the CPU ratio stays near it.
    """

    CALIBRATION_CAP = 512000
    CALIBRATION_SECONDS = 0.05

    def setUp(self):
        self._config_path = "/tmp/claude-danger-guard-nonexistent-config.json"
        self._ctx = _build_context(self._config_path)

    def _seconds(self, command):
        payload = {"tool_name": "Bash", "tool_input": {"command": command},
                  "cwd": "/tmp"}
        start = time.thread_time()
        guard_lib.evaluate(payload, self._ctx)
        return time.thread_time() - start

    def _cost(self, command):
        # min of 3: residual noise (cache, core type) only ADDS time.
        return min(self._seconds(command) for _ in range(3))

    def _assert_linear(self, prefix_word, suffix="", lead="", start=1000):
        n = start
        small = self._cost(lead + prefix_word * n + suffix)
        while small < self.CALIBRATION_SECONDS:
            n *= 2
            if n > self.CALIBRATION_CAP:
                self.fail("could not calibrate a baseline taking >= %.2fs within "
                          "%d words for %r" % (self.CALIBRATION_SECONDS,
                                               self.CALIBRATION_CAP, prefix_word))
            small = self._cost(lead + prefix_word * n + suffix)
        # The two sizes are timed INTERLEAVED, min of 5 each, so residual
        # noise lands on both sides of the ratio. Interleaving alone did not
        # survive load on wall time (see the class docstring).
        small_command = lead + prefix_word * n + suffix
        big_command = lead + prefix_word * (n * 4) + suffix
        smalls, bigs = [], []
        for _ in range(5):
            smalls.append(self._seconds(small_command))
            bigs.append(self._seconds(big_command))
        ratio = min(bigs) / min(smalls)
        self.assertLess(
            ratio, 8.0,
            "%r scaled %.1fx for a 4x longer line (n=%d) -- linear measures "
            "~4x, quadratic ~16x" % (prefix_word, ratio, n))


class TestNoRuleIsSuperlinear(_LinearTimingMixin, unittest.TestCase):
    """Each of `guard_lib._TEXT_SHAPE_RULES` runs in time linear in the
    line, per the timing contract in `_LinearTimingMixin`."""

    SHAPES = (
        "git --exec-path ",
        "git -c x ",
        "git push ",
        "git push -c ",
        "curl ",
        "curl |",
        "dd ",
        ".ssh/ ",
        "xsudo ",
        "chmod -x ",
        "diskutil ",
        # Overlapping match starts: each repetition begins a new candidate
        # match inside the previous one's run (`-chmod `×n
        # made the chmod regex quadratic).
        "-chmod ",
        "-git push ",
        "sudosudo ",
        "curl curl ",
        ".ssh/.ssh/ ",
        "dd dd of=",
    )

    def test_no_rule_is_superlinear(self):
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                self._assert_linear(shape)
            with self.subTest(shape=shape, suffix="; rm -rf ~/Documents"):
                self._assert_linear(shape, suffix="; rm -rf ~/Documents")


class TestHeredocStrippingIsLinear(_LinearTimingMixin, unittest.TestCase):
    """`guard_lib._strip_heredocs` runs in time linear in the input on the
    three shapes that were quadratic before it resumed instead of
    re-tokenizing. The open-quote shapes are the ones a resume point
    alone does not fix; see the comment above `_line_opens_heredoc`.
    start=100 because a heredoc opener line (or an open-quote
    repetition) costs far more per repetition than a one-word rule-matching
    fragment, so the calibration loop would otherwise run many extra
    doublings before n=1000 took >= CALIBRATION_SECONDS."""

    def test_many_heredocs(self):
        self._assert_linear("cat <<EOF\nx\nEOF\n", suffix="rm -rf ~/Documents",
                             start=100)

    def test_openers_inside_an_open_quote(self):
        self._assert_linear("<<\n", lead='echo "\n',
                             suffix='"; rm -rf ~/Documents', start=100)

    def test_escaped_quote_openers_inside_an_open_quote(self):
        self._assert_linear('<< \\"\n', lead='echo "\n',
                             suffix='"; rm -rf ~/Documents', start=100)


class TestOneLongWordIsLinear(_LinearTimingMixin, unittest.TestCase):
    """One word built from many pieces must cost linear time.  CPython 3.13
    made `_scan`'s cell-variable concatenation (`cur = (cur or "") + piece`)
    quadratic in the word's length, because it does not grow a cell
    variable's string in place the way 3.9 does. `"a"`, `\\a` and `$(a)` are
    fixed by the same change but are not timed here -- at this harness's
    calibration sizes the guard's other linear work dilutes the quadratic
    term enough that those three branches do not fail (FAILS for
    `a`, `'a'` and `+a` on 3.13, PASSES under 3.9; does NOT
    fail for `"a"`, `\\a` or `$(a)` at calibration sizes). The single-quoted
    pieces are 64 characters, not one: with `'a'` the per-piece overhead
    hid a quadratic rewrite that adds its own (`cur = ["".join(cur) +
    piece]`, passing). Longer pieces let the copying dominate
    (on 3.13 that rewrite and the original bug read far above the
    fixed code)."""

    def test_one_long_bare_word(self):
        self._assert_linear("a", lead="echo ", suffix="; rm -rf ~/Documents")

    def test_many_single_quoted_pieces(self):
        self._assert_linear("'" + "a" * 64 + "'", lead="echo ",
                            suffix="; rm -rf ~/Documents")

    def test_plus_joined_refspec_word(self):
        self._assert_linear("+a", lead="git push +a",
                             suffix="; rm -rf ~/Documents")


class TestTheReviewReproBlocksFast(unittest.TestCase):

    def test_the_review_repro_blocks_fast(self):
        command = "git " + "--exec-path " * 40 + "; rm -rf ~/Documents"
        payload = '{"tool_name": "Bash", "tool_input": {"command": %s}, "cwd": "/tmp"}' % (
            __import__("json").dumps(command))
        environ = dict(os.environ)
        environ.pop("DANGER_ALLOW", None)
        environ["CLAUDE_TOOLKIT_CONFIG"] = "/tmp/claude-danger-guard-nonexistent-config.json"
        start = time.perf_counter()
        done = subprocess.run([GUARD], input=payload, capture_output=True,
                              text=True, env=environ, timeout=10)
        elapsed = time.perf_counter() - start
        self.assertEqual(done.returncode, 2, "expected BLOCK: stderr=%r" % done.stderr)
        self.assertLess(elapsed, 2.0, "the review's repro command took %.2fs" % elapsed)


class TestManyHeredocsStillBlock(unittest.TestCase):
    """The real hook, as a subprocess, on commands the quadratic
    `_strip_heredocs` used to take seconds on. The declared hook timeout
    IS the property under test -- the repo CLAUDE.md wall-clock rule
    forbids asserting anything tighter -- so a `subprocess.TimeoutExpired`
    is the only failure mode for slowness, and a returncode other than 2
    is the failure mode for a wrong verdict."""

    def setUp(self):
        self._timeout = _danger_guard_timeout()

    def _assert_blocks(self, command):
        payload = '{"tool_name": "Bash", "tool_input": {"command": %s}, "cwd": "/tmp"}' % (
            json.dumps(command))
        environ = dict(os.environ)
        environ.pop("DANGER_ALLOW", None)
        environ["CLAUDE_TOOLKIT_CONFIG"] = "/tmp/claude-danger-guard-nonexistent-config.json"
        done = subprocess.run([GUARD], input=payload, capture_output=True,
                              text=True, env=environ, timeout=self._timeout)
        self.assertEqual(done.returncode, 2, "expected BLOCK: stderr=%r" % done.stderr)

    def test_two_thousand_heredocs_then_rm(self):
        self._assert_blocks("cat <<EOF\nx\nEOF\n" * 2000 + "rm -rf ~/Documents")

    def test_open_quote_openers_then_rm(self):
        self._assert_blocks('echo "\n' + "<<\n" * 9000 + '"; rm -rf ~/Documents')


if __name__ == "__main__":
    unittest.main(verbosity=1)
