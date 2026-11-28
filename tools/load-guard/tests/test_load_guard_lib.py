#!/usr/bin/env python3
"""Tests for the load guard's pure library (shim set, memory parse, holder record)."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)

import load_guard_lib as lib  # noqa: E402


class TestHeavySet(unittest.TestCase):

    def test_builtin_set_is_this_literal_list(self):
        # a literal, so removing or adding a tool is a visible decision
        self.assertEqual(
            lib.BUILTIN_HEAVY,
            frozenset({"ffmpeg", "whisper-cli", "llama-server", "llama-cli",
                       "llama-bench"}))

    def test_light_args_are_this_literal_list(self):
        self.assertEqual(
            lib.LIGHT_ARGS,
            frozenset({"--version", "-version", "--help", "-h", "-help"}))

    def test_builtins_always_present(self):
        self.assertTrue(lib.BUILTIN_HEAVY <= lib.heavy_set(["x"]))
        self.assertEqual(lib.heavy_set(None), lib.BUILTIN_HEAVY)

    def test_extra_names_added_and_junk_ignored(self):
        got = lib.heavy_set(["reel_extract", "", "  ", 3, None])
        self.assertEqual(got, lib.BUILTIN_HEAVY | {"reel_extract"})

    def test_extra_path_reduced_to_basename(self):
        self.assertEqual(lib.heavy_set(["/opt/x/reel_extract", "dir/"]),
                         lib.BUILTIN_HEAVY | {"reel_extract"})

    def test_removed_parser_is_gone(self):
        for name in ("decide", "split_segments", "Verdict"):
            self.assertFalse(hasattr(lib, name), name)

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
