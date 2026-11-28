#!/usr/bin/env python3
"""Tests for the load guard's pure library.

`path` is asserted on every verdict, never inferred from `allowed`: several
routes allow, and a test that only checks the verdict cannot tell them apart.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)

import load_guard_lib as lib  # noqa: E402

HEAVY = lib.heavy_set([])


def judge(command, extra=()):
    return lib.decide(command, lib.heavy_set(list(extra)))


class TestDecide(unittest.TestCase):

    def assertDeny(self, command, matched, extra=()):
        v = judge(command, extra)
        self.assertIs(v.allowed, False, command)
        self.assertEqual(v.path, lib.DENY_HEAVY, command)
        self.assertEqual(v.matched, matched, command)
        return v

    def assertAllow(self, command, path, extra=()):
        v = judge(command, extra)
        self.assertIs(v.allowed, True, command)
        self.assertEqual(v.path, path, command)
        self.assertIsNone(v.matched, command)

    def test_bare_heavy_tool_denies(self):
        v = self.assertDeny("ffmpeg -i a b", "ffmpeg")
        self.assertIn("claude-heavy-run ffmpeg", v.reason)
        self.assertIn("LOAD_GUARD_ALLOW=1", v.reason)

    def test_every_builtin_denies(self):
        for name in sorted(lib.BUILTIN_HEAVY):
            self.assertDeny(name + " x", name)

    def test_wrapped_allows_by_the_wrapped_route(self):
        self.assertAllow("claude-heavy-run ffmpeg -i a b", lib.ALLOW_WRAPPED)
        self.assertAllow("claude-heavy-run -- ffmpeg -i a b", lib.ALLOW_WRAPPED)
        self.assertAllow("/x/bin/claude-heavy-run ffmpeg a", lib.ALLOW_WRAPPED)

    def test_wrapper_behind_a_prefix_is_wrapped(self):
        self.assertAllow("nohup claude-heavy-run ffmpeg a", lib.ALLOW_WRAPPED)

    def test_mentions_are_not_runs(self):
        self.assertAllow("brew info ffmpeg", lib.ALLOW_NOT_HEAVY)
        self.assertAllow("which whisper-cli", lib.ALLOW_NOT_HEAVY)
        self.assertAllow("grep ffmpeg log", lib.ALLOW_NOT_HEAVY)
        self.assertAllow("echo ffmpeg", lib.ALLOW_NOT_HEAVY)
        self.assertAllow("command -v ffmpeg", lib.ALLOW_NOT_HEAVY)

    def test_after_a_separator_denies(self):
        self.assertDeny("cd x && ffmpeg -i a b", "ffmpeg")
        self.assertDeny("ls; ffmpeg a", "ffmpeg")
        self.assertDeny("false || yt-dlp URL", "yt-dlp")
        self.assertDeny("cat f | ffmpeg -i - o", "ffmpeg")
        self.assertDeny("sleep 1 & ffmpeg a", "ffmpeg")
        self.assertDeny("ls\nffmpeg a", "ffmpeg")

    def test_assignment_and_prefixes_are_skipped(self):
        self.assertDeny("FOO=1 nohup yt-dlp URL", "yt-dlp")
        self.assertDeny("env A=1 ffmpeg a", "ffmpeg")
        self.assertDeny("env -u X ffmpeg a", "ffmpeg")
        self.assertDeny("time ffmpeg a", "ffmpeg")
        self.assertDeny("nice -n 10 ffmpeg a", "ffmpeg")
        self.assertDeny("caffeinate -i ffmpeg a", "ffmpeg")
        self.assertDeny("caffeinate -t 60 nohup ffmpeg a", "ffmpeg")
        self.assertDeny("exec ffmpeg a", "ffmpeg")
        self.assertDeny("sudo -u root ffmpeg a", "ffmpeg")
        self.assertDeny("command ffmpeg a", "ffmpeg")

    def test_absolute_path_matches_by_basename(self):
        self.assertDeny("/opt/homebrew/bin/llama-server -m m", "llama-server")

    def test_interpreter_script_denies_only_when_listed(self):
        self.assertAllow("python3 reel_extract.py", lib.ALLOW_NOT_HEAVY)
        self.assertDeny("python3 reel_extract.py", "reel_extract",
                        extra=["reel_extract"])
        self.assertDeny("python3 -u /a/b/reel_extract.py x", "reel_extract",
                        extra=["reel_extract"])
        self.assertDeny("bash run.sh", "run", extra=["run"])
        self.assertDeny("python3 -m mlx_lm.generate --model m",
                        "mlx_lm.generate")

    def test_interpreter_with_an_unrelated_script_allows(self):
        self.assertAllow("python3 analyze.py ffmpeg", lib.ALLOW_NOT_HEAVY)

    def test_version_probe_allows_by_its_own_route(self):
        for probe in ("--version", "-version", "--help", "-h"):
            self.assertAllow("ffmpeg " + probe, lib.ALLOW_VERSION_PROBE)

    def test_probe_with_extra_arguments_still_denies(self):
        self.assertDeny("ffmpeg -version -i a", "ffmpeg")
        self.assertDeny("ffmpeg -i a -h", "ffmpeg")

    def test_unbalanced_quote_fails_open(self):
        self.assertAllow('ffmpeg -i "a b', lib.ALLOW_UNPARSEABLE)

    def test_a_second_bare_call_denies_despite_a_wrapped_one(self):
        v = self.assertDeny("ls; claude-heavy-run ffmpeg x; ffmpeg y", "ffmpeg")
        self.assertEqual(v.path, lib.DENY_HEAVY)

    def test_quoted_separator_does_not_split(self):
        self.assertAllow('echo ";" ffmpeg', lib.ALLOW_NOT_HEAVY)
        self.assertAllow("echo 'a && ffmpeg b'", lib.ALLOW_NOT_HEAVY)

    def test_stderr_redirect_does_not_split(self):
        self.assertDeny("ffmpeg -i a b 2>&1", "ffmpeg")
        self.assertAllow("claude-heavy-run ffmpeg a 2>&1", lib.ALLOW_WRAPPED)

    def test_bash_c_is_a_documented_limit(self):
        self.assertAllow('bash -c "ffmpeg a"', lib.ALLOW_NOT_HEAVY)

    def test_non_string_command_allows(self):
        self.assertAllow(None, lib.ALLOW_NOT_HEAVY)

    def test_empty_command_allows(self):
        self.assertAllow("", lib.ALLOW_NOT_HEAVY)


class TestHeavySet(unittest.TestCase):

    def test_builtins_always_present(self):
        self.assertTrue(lib.BUILTIN_HEAVY <= lib.heavy_set(["x"]))
        self.assertEqual(lib.heavy_set(None), lib.BUILTIN_HEAVY)

    def test_extra_names_added_and_junk_ignored(self):
        got = lib.heavy_set(["reel_extract", "", "  ", 3, None])
        self.assertEqual(got, lib.BUILTIN_HEAVY | {"reel_extract"})

    def test_extra_path_reduced_to_basename(self):
        self.assertEqual(lib.heavy_set(["/opt/x/reel_extract", "dir/"]),
                         lib.BUILTIN_HEAVY | {"reel_extract"})
        v = judge("python3 reel_extract.py", ["/opt/x/reel_extract"])
        self.assertEqual((v.allowed, v.path), (False, lib.DENY_HEAVY))

    def test_non_list_extra_ignored(self):
        self.assertEqual(lib.heavy_set("ffmpeg2"), lib.BUILTIN_HEAVY)


class TestParseLevel(unittest.TestCase):

    def test_integers(self):
        self.assertEqual(lib.parse_level("52\n"), 52)
        self.assertEqual(lib.parse_level("0"), 0)
        self.assertEqual(lib.parse_level(" 100 "), 100)

    def test_garbage_is_none(self):
        for text in ("garbage", "", "  ", "5 2", "-3", "4.5", "101", "1000",
                     None, 52):
            self.assertIsNone(lib.parse_level(text), repr(text))


class TestHolder(unittest.TestCase):

    def test_round_trip(self):
        text = lib.format_holder(123, ["ffmpeg", "-i", "a b"], 1700000000.5)
        self.assertEqual(lib.parse_holder(text), {
            "pid": 123, "argv": ["ffmpeg", "-i", "a b"],
            "started": 1700000000.5})

    def test_empty_and_foreign_are_none(self):
        for text in ("", "\n", "not json", "[]", "{}", None,
                     '{"pid": "1", "argv": [], "started": 1}',
                     '{"pid": 1, "argv": [1], "started": 1}',
                     '{"pid": 1, "argv": [], "started": "now"}',
                     '{"pid": true, "argv": [], "started": 1}'):
            self.assertIsNone(lib.parse_holder(text), repr(text))


if __name__ == "__main__":
    unittest.main()
