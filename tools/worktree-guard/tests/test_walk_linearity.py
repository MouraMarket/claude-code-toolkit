#!/usr/bin/env python3
"""Every text-shape rule claude-danger-guard's own test_rule_linearity.py
polices is a regex; the worktree guard's command walk is a loop over tokens,
resolving a target directory per git invocation. The analogous hazard is a
walk or resolver that is quadratic (or worse) in the number of statements,
`cd`s, subshells, or git global options on one command line -- a hook killed
at its declared timeout returns no verdict, so the command runs unjudged.

This times `worktree_lib.evaluate` in process, with a `Context` whose `git` is
an instant fake (a fixed answer per git subcommand, no subprocess at all), so
only the walk and its matching are measured -- not real git's own latency.
`load_config` is patched to return a fixed canonical_root without touching the
filesystem, since the real function realpaths an existing directory and the
test has none.
"""

import os
import sys
import time
import unittest
import unittest.mock as mock

_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.insert(0, _DIR)
import worktree_lib  # noqa: E402  (needs sys.path set up first)


def _fake_git(target_dir, args):
    """One fixed answer per git subcommand -- same toplevel for every
    target, so cwd and every resolved target always match (Rule A never
    fires; nothing here times git resolution itself, only the walk around
    it) -- and a branch/worktree-count shape that keeps Rule C ACTIVE, so a
    push shape's verb-matching code actually runs rather than returning
    early from on_canonical_default_branch."""
    if args == ["rev-parse", "--show-toplevel"]:
        return worktree_lib.GitResult("ok", "/r/x")
    if args == ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]:
        return worktree_lib.GitResult("ok", "origin/main")
    if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
        return worktree_lib.GitResult("ok", "main")
    if args == ["worktree", "list", "--porcelain"]:
        return worktree_lib.GitResult("ok", "worktree /r/x\n")
    return worktree_lib.GitResult("ok", "")


def _make_context():
    return worktree_lib.Context(
        home="/r", config_path="/nonexistent.json", windows=False, allow=False,
        git=_fake_git, clock=time.monotonic, msys_path=None)


# --- the shapes ---------------------------------------------------------
#
# Each is a callable n -> command string. Three are "push shapes" -- they
# must actually reach `check_rule_c_git`'s push-verb matching, not merely be
# ALLOWED by some earlier, cheaper exit -- and are reach-asserted before
# being timed.

def _shape_status_chain(n):
    return "git status ; " * n


def _shape_dash_c_then_push(n):
    # ONE git, n `-c a=b` global options, then the subcommand -- not n
    # separate `git -c a=b` statements, which would never reach a push
    # subcommand at all (the option-consuming loop stops at the first
    # token that doesn't start with "-", which would be the NEXT "git").
    return "git " + "-c a=b " * n + "push"


def _shape_cd_chain(n):
    return "cd x && " * n + "git status"


def _shape_parenthesized(n):
    return "( " * n + "git status" + " )" * n


def _shape_echo_chain(n):
    return "echo x " * n + "; git status"


def _shape_plus_refspec_overlap(n):
    # One giant token, `+a` repeated with no separating whitespace -- each
    # repetition begins a new candidate match for _PLUS_REFSPEC_RE inside
    # the previous one's run, the same overlapping-match-start shape the
    # danger guard's own linearity suite exercises for its text rules.
    return "git push +a" + "+a" * n


def _shape_dash_f_overlap(n):
    # Same overlapping-match-start shape against _FORCE_PUSH_RE, via `-f`.
    return "git push -f" * n


SHAPES = (
    ("'git status ; ' * n", _shape_status_chain, False),
    ("'git ' + '-c a=b ' * n + 'push'", _shape_dash_c_then_push, True),
    ("'cd x && ' * n + 'git status'", _shape_cd_chain, False),
    ("'( ' * n + 'git status' + ' )' * n", _shape_parenthesized, False),
    ("'echo x ' * n + '; git status'", _shape_echo_chain, False),
    ("'git push +a' + '+a' * n", _shape_plus_refspec_overlap, True),
    ("'git push -f' * n", _shape_dash_f_overlap, True),
)


class TestWalkIsLinear(unittest.TestCase):
    """Calibrated and timed exactly like claude-danger-guard's
    test_rule_linearity.py: double n from 1000 until one call takes >= 0.05s
    (cap 512,000, fail at the cap -- never skip), then time n and 4n,
    interleaved min of 5 each. A 4x longer line measures ~4x when linear and
    ~16x when quadratic, so 8.0 sits between them. Timed in this thread's CPU
    time, not wall time, for the reason in that file's `_LinearTimingMixin`:
    under load, min-of-N on wall time favours the short sample.
    """

    CALIBRATION_CAP = 512000
    CALIBRATION_SECONDS = 0.05

    def setUp(self):
        self._ctx = _make_context()
        self._patcher = mock.patch("worktree_lib.load_config",
                                   return_value=("", "/r"))
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def _seconds(self, command):
        payload = {"tool_name": "Bash", "tool_input": {"command": command},
                  "cwd": "/r/x"}
        start = time.thread_time()
        worktree_lib.evaluate(payload, self._ctx)
        return time.thread_time() - start

    def _cost(self, command):
        # min of 3: residual noise (cache, core type) only ADDS time.
        return min(self._seconds(command) for _ in range(3))

    def _assert_reaches_check_rule_c_git(self, build):
        """A shape that exits early times nothing -- before calibrating or
        timing a push shape, confirm `check_rule_c_git` is actually called
        for it, at a small n, via a counting wrapper patch."""
        calls = []
        original = worktree_lib.check_rule_c_git

        def _counting(*args, **kwargs):
            calls.append(1)
            return original(*args, **kwargs)

        with mock.patch("worktree_lib.check_rule_c_git", side_effect=_counting):
            self._seconds(build(10))
        self.assertGreater(
            len(calls), 0,
            "this shape never reached check_rule_c_git -- timing it would "
            "measure an early exit, not the walk")

    def _assert_linear(self, build):
        n = 1000
        small = self._cost(build(n))
        while small < self.CALIBRATION_SECONDS:
            n *= 2
            if n > self.CALIBRATION_CAP:
                self.fail("could not calibrate a baseline taking >= %.2fs "
                         "within %d words" % (self.CALIBRATION_SECONDS,
                                              self.CALIBRATION_CAP))
            small = self._cost(build(n))
        # Interleaved, min of 5 each, so residual noise lands on both sides
        # of the ratio.
        small_command = build(n)
        big_command = build(n * 4)
        smalls, bigs = [], []
        for _ in range(5):
            smalls.append(self._seconds(small_command))
            bigs.append(self._seconds(big_command))
        ratio = min(bigs) / min(smalls)
        self.assertLess(
            ratio, 8.0,
            "scaled %.1fx for a 4x longer command (n=%d) -- linear measures "
            "~4x, quadratic ~16x" % (ratio, n))

    def test_no_walk_shape_is_superlinear(self):
        for name, build, is_push_shape in SHAPES:
            with self.subTest(shape=name):
                if is_push_shape:
                    self._assert_reaches_check_rule_c_git(build)
                self._assert_linear(build)


if __name__ == "__main__":
    unittest.main(verbosity=1)
