import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import title_lib
import toolkit_config  # on sys.path via title_lib's own lib insert, above


_LOG_PATCH = None


def setUpModule():
    # title_lib.log() appends to ~/.claude/title-hook.log; keep every test's
    # log line (a missing transcript, say) out of the real one.
    global _LOG_PATCH
    log_dir = tempfile.mkdtemp()
    _LOG_PATCH = mock.patch.object(title_lib, "LOG_PATH",
                                   os.path.join(log_dir, "title-hook.log"))
    _LOG_PATCH.start()


def tearDownModule():
    _LOG_PATCH.stop()


def write_transcript(lines):
    """Write a list of dicts as a .jsonl file, return its path."""
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant(text):
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def tool_use(name):
    return {"type": "assistant",
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "name": name, "input": {}}]}}


class TestExtractText(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(title_lib.extract_text("hello"), "hello")

    def test_content_block_list(self):
        blocks = [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]
        self.assertEqual(title_lib.extract_text(blocks), "one two")

    def test_ignores_non_text_blocks(self):
        blocks = [{"type": "tool_use", "name": "Bash"}, {"type": "text", "text": "kept"}]
        self.assertEqual(title_lib.extract_text(blocks), "kept")

    def test_none_returns_empty(self):
        self.assertEqual(title_lib.extract_text(None), "")


class TestIsJunk(unittest.TestCase):
    def test_system_reminder_is_junk(self):
        self.assertTrue(title_lib.is_junk("<system-reminder>stuff</system-reminder>"))

    def test_command_caveat_is_junk(self):
        self.assertTrue(title_lib.is_junk("<local-command-caveat>Caveat: blah"))

    def test_command_message_is_junk(self):
        self.assertTrue(title_lib.is_junk("<command-message>autonomy-permissions</command-message>"))

    def test_empty_is_junk(self):
        self.assertTrue(title_lib.is_junk("   "))

    def test_real_prompt_is_not_junk(self):
        self.assertFalse(title_lib.is_junk("build me a keyword list for the blog"))


class TestSampleTranscript(unittest.TestCase):
    def test_collects_first_three_user_messages(self):
        path = write_transcript([user("one"), user("two"), user("three"), user("four")])
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertEqual(sample["user_messages"], ["one", "two", "three"])

    def test_skips_junk_user_messages(self):
        path = write_transcript([
            user("<local-command-caveat>Caveat: ignore me"),
            user("real prompt here"),
        ])
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertEqual(sample["user_messages"], ["real prompt here"])
        self.assertEqual(sample["user_count"], 1)

    def test_skips_meta_messages(self):
        path = write_transcript([
            {"type": "user", "isMeta": True, "message": {"content": "meta thing"}},
            user("real one"),
        ])
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertEqual(sample["user_messages"], ["real one"])

    def test_keeps_last_assistant_text(self):
        path = write_transcript([user("hi"), assistant("first"), assistant("final answer")])
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertEqual(sample["last_assistant"], "final answer")

    def test_collects_tool_names_deduped(self):
        path = write_transcript([user("hi"), tool_use("Bash"), tool_use("Read"), tool_use("Bash")])
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertEqual(sorted(sample["tool_names"]), ["Bash", "Read"])

    def test_tolerates_malformed_lines(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            f.write("not json at all\n")
            f.write(json.dumps(user("survived")) + "\n")
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertEqual(sample["user_messages"], ["survived"])

    def test_skips_oversized_lines(self):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(user("x" * 300000)) + "\n")
            f.write(json.dumps(user("small one")) + "\n")
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertEqual(sample["user_messages"], ["small one"])

    def test_truncates_long_messages(self):
        path = write_transcript([user("y" * 5000)])
        sample = title_lib.sample_transcript(path, max_chars=1000)
        os.unlink(path)
        self.assertLessEqual(len(sample["user_messages"][0]), 1000)

    def test_missing_file_returns_empty_sample(self):
        sample = title_lib.sample_transcript("/nonexistent/path.jsonl")
        self.assertEqual(sample["user_messages"], [])
        self.assertEqual(sample["user_count"], 0)


class TestIsTitleworthy(unittest.TestCase):
    def test_two_real_messages_is_worthy(self):
        path = write_transcript([user("first real"), user("second real")])
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertTrue(title_lib.is_titleworthy(sample))

    def test_single_message_is_not_worthy(self):
        """Also the natural recursion backstop: a headless `claude -p`
        titling session has exactly one user message."""
        path = write_transcript([user("echo ok")])
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertFalse(title_lib.is_titleworthy(sample))

    def test_empty_is_not_worthy(self):
        path = write_transcript([])
        sample = title_lib.sample_transcript(path)
        os.unlink(path)
        self.assertFalse(title_lib.is_titleworthy(sample))


class TestBuildPrompt(unittest.TestCase):
    def _sample(self):
        return {"user_messages": ["build a keyword list for the blog"],
                "last_assistant": "Delivered 104 terms across 9 clusters.",
                "tool_names": ["Bash", "Write"],
                "user_count": 2}

    def test_includes_user_message(self):
        prompt = title_lib.build_prompt(self._sample(), "/Users/e/Work")
        self.assertIn("build a keyword list for the blog", prompt)

    def test_includes_last_assistant(self):
        prompt = title_lib.build_prompt(self._sample(), "/Users/e/Work")
        self.assertIn("104 terms", prompt)

    def test_includes_cwd(self):
        prompt = title_lib.build_prompt(self._sample(), "/Users/e/Work")
        self.assertIn("/Users/e/Work", prompt)

    def test_includes_tools(self):
        prompt = title_lib.build_prompt(self._sample(), "/x")
        self.assertIn("Bash, Write", prompt)

    def test_asks_for_a_plain_outcome_with_no_prefix(self):
        prompt = title_lib.build_prompt(self._sample(), "/x")
        self.assertIn("OUTCOME", prompt)
        self.assertIn("no project label, code, prefix", prompt)
        self.assertNotIn("—", prompt)  # no `LABEL — ...` separator to copy

    def test_lists_no_codes(self):
        """The prompt offers no list of codes to prefix a title with."""
        prompt = title_lib.build_prompt(self._sample(), "/x").lower()
        for word in ("client", "codes", "registry", "roster"):
            self.assertNotIn(word, prompt)

    def test_no_code_machinery_remains(self):
        for name in ("load_client_codes", "load_client_names", "expand_term",
                     "CLIENTS_PY_PATH", "roster"):
            self.assertFalse(hasattr(title_lib, name), name)


class TestCleanTitle(unittest.TestCase):
    def test_strips_whitespace_and_quotes(self):
        self.assertEqual(title_lib.clean_title('  "Drafted the keyword brief"  '),
                         "Drafted the keyword brief")

    def test_collapses_newlines(self):
        self.assertEqual(title_lib.clean_title("Drafted the brief\nand the outline"),
                         "Drafted the brief and the outline")

    def test_drops_one_trailing_period(self):
        self.assertEqual(title_lib.clean_title("Fixed the filtering bug."),
                         "Fixed the filtering bug")

    def test_accepts_exactly_max_len(self):
        title = "F" + "x" * 59
        self.assertEqual(title_lib.clean_title(title, max_len=60), title)

    def test_rejects_over_max_len_rather_than_truncating(self):
        """Truncation is how a refusal was once stored as a title: an answer
        longer than the limit is not a title the model was asked for."""
        self.assertIsNone(title_lib.clean_title("F" + "x" * 60, max_len=60))

    def test_rejects_refusal_prose(self):
        """Regression: a contentless transcript made the model answer in prose."""
        self.assertIsNone(title_lib.clean_title(
            "I don't have any information to title this session"))

    def test_rejects_first_person_and_apology_openers(self):
        for prose in ("I can't title this", "I’m not sure what happened",
                      "Sorry, nothing to title", "Unfortunately no content",
                      "There are no user messages", "No title available",
                      "As an AI I cannot see it"):
            self.assertIsNone(title_lib.clean_title(prose), prose)

    def test_rejects_apology_and_refusal_wording(self):
        """Gate finding: "Apologies, ..." passed the opener list and was stored."""
        for prose in ("Apologies, I cannot title this session",
                      "My apologies, nothing here", "Regrettably no transcript",
                      "Unable to title this session",
                      "This session cannot be titled",
                      "Can\u2019t determine what happened",
                      "Not enough information to title"):
            self.assertIsNone(title_lib.clean_title(prose), prose)

    def test_rejects_two_sentences(self):
        self.assertIsNone(title_lib.clean_title("Fixed the bug. Then added tests"))

    def test_accepts_words_that_only_start_like_an_opener(self):
        for title in ("IAM role cleanup", "Inbox triage script",
                      "Sorting fixes for the index", "Notes on the API"):
            self.assertEqual(title_lib.clean_title(title), title)

    def test_accepts_a_version_number(self):
        self.assertEqual(title_lib.clean_title("Bumped the parser to v2.1"),
                         "Bumped the parser to v2.1")

    def test_empty_returns_none(self):
        self.assertIsNone(title_lib.clean_title("   "))

    def test_none_returns_none(self):
        self.assertIsNone(title_lib.clean_title(None))

    def test_strips_leading_label(self):
        self.assertEqual(title_lib.clean_title("Title: Drafted the brief"),
                         "Drafted the brief")

    def test_strips_quotes_inside_a_leading_label(self):
        self.assertEqual(title_lib.clean_title('Title: "Drafted the brief"'),
                         "Drafted the brief")


class TestAppendTitleRecord(unittest.TestCase):
    def test_appends_valid_record(self):
        path = write_transcript([user("hello")])
        ok = title_lib.append_title_record(path, "sess-123", "Drafted the brief")
        with open(path) as f:
            last = json.loads(f.readlines()[-1])
        os.unlink(path)
        self.assertTrue(ok)
        self.assertEqual(last["type"], "custom-title")
        self.assertEqual(last["customTitle"], "Drafted the brief")
        self.assertEqual(last["sessionId"], "sess-123")

    def test_preserves_existing_lines(self):
        path = write_transcript([user("one"), user("two")])
        title_lib.append_title_record(path, "sess-123", "T")
        with open(path) as f:
            lines = f.readlines()
        os.unlink(path)
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["message"]["content"], "one")

    def test_missing_file_returns_false(self):
        self.assertFalse(
            title_lib.append_title_record("/nonexistent/dir/x.jsonl", "s", "T"))


class TestHasTitle(unittest.TestCase):
    def test_false_when_untitled(self):
        path = write_transcript([user("hello")])
        result = title_lib.has_title(path)
        os.unlink(path)
        self.assertFalse(result)

    def test_true_after_titling(self):
        path = write_transcript([user("hello")])
        title_lib.append_title_record(path, "s", "T")
        result = title_lib.has_title(path)
        os.unlink(path)
        self.assertTrue(result)

    def test_missing_file_is_false(self):
        self.assertFalse(title_lib.has_title("/nonexistent/x.jsonl"))


class TestAppendIndexRow(unittest.TestCase):
    def test_writes_tab_separated_row(self):
        fd, path = tempfile.mkstemp(suffix=".tsv")
        os.close(fd)
        title_lib.append_index_row("sess-1", "/Users/e/Work", "Drafted the brief",
                                   index_path=path, date_str="2026-08-07")
        with open(path) as f:
            row = f.read().strip()
        os.unlink(path)
        self.assertEqual(row.split("\t"),
                         ["2026-08-07", "sess-1", "/Users/e/Work", "Drafted the brief"])

    def test_strips_tabs_from_title(self):
        fd, path = tempfile.mkstemp(suffix=".tsv")
        os.close(fd)
        title_lib.append_index_row("s", "/c", "has\ttab", index_path=path,
                                   date_str="2026-08-07")
        with open(path) as f:
            fields = f.read().strip().split("\t")
        os.unlink(path)
        self.assertEqual(len(fields), 4)

    def test_appends_rather_than_overwrites(self):
        fd, path = tempfile.mkstemp(suffix=".tsv")
        os.close(fd)
        title_lib.append_index_row("s1", "/c", "one", index_path=path, date_str="2026-08-07")
        title_lib.append_index_row("s2", "/c", "two", index_path=path, date_str="2026-08-07")
        with open(path) as f:
            lines = [x for x in f.read().strip().split("\n") if x]
        os.unlink(path)
        self.assertEqual(len(lines), 2)

    def test_creates_parent_directory(self):
        base = tempfile.mkdtemp()
        target = os.path.join(base, "nested", "deeper", "index.tsv")
        ok = title_lib.append_index_row("s", "/c", "t", index_path=target,
                                        date_str="2026-08-07")
        self.assertTrue(ok)
        self.assertTrue(os.path.exists(target))


class TestIsEnabled(unittest.TestCase):
    """`is_enabled()` gates claude-title-session's main() -- see test_hook.py
    for the subprocess-level proof that the gate actually stops a fork."""

    def setUp(self):
        self._orig_config_path = toolkit_config.CONFIG_PATH
        self.addCleanup(self._restore)

    def _restore(self):
        toolkit_config.CONFIG_PATH = self._orig_config_path
        toolkit_config.load(refresh=True)

    def _write_config(self, blob):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(blob, handle)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        toolkit_config.CONFIG_PATH = handle.name
        toolkit_config.load(refresh=True)

    def test_no_config_file_at_all_defaults_on(self):
        toolkit_config.CONFIG_PATH = "/nonexistent-on-purpose.json"
        toolkit_config.load(refresh=True)
        self.assertTrue(title_lib.is_enabled())

    def test_key_absent_defaults_on(self):
        self._write_config({"session_titler": {"index_path": "/tmp/x.tsv"}})
        self.assertTrue(title_lib.is_enabled())

    def test_explicitly_disabled(self):
        self._write_config({"session_titler": {"enabled": False}})
        self.assertFalse(title_lib.is_enabled())

    def test_a_non_boolean_value_leaves_it_on(self):
        # "no" is not `is not False` -- a garbage value must not read as off.
        self._write_config({"session_titler": {"enabled": "no"}})
        self.assertTrue(title_lib.is_enabled())


if __name__ == "__main__":
    unittest.main()
