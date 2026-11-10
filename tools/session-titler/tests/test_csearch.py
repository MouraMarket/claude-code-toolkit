import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CSEARCH = os.path.join(ROOT, "csearch")

spec = importlib.util.spec_from_loader(
    "csearch", importlib.machinery.SourceFileLoader("csearch", CSEARCH))
csearch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(csearch)

ROWS = [
    "2026-08-06\tsess-a\t/Users/e/Work\tDrafted the keyword brief for the blog",
    "2026-08-05\tsess-b\t/Users/e/Work\tBooking funnel analysis for Acme Resorts",
    "2026-08-04\tsess-c\t/Users/e/Work/repos\tFixed the API filtering bug",
]


def index_file():
    fd, path = tempfile.mkstemp(suffix=".tsv")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(ROWS) + "\n")
    return path


class TestSearch(unittest.TestCase):
    def test_finds_by_title_substring(self):
        path = index_file()
        results = csearch.search(["keyword"], path)
        os.unlink(path)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["session_id"], "sess-a")

    def test_is_case_insensitive(self):
        path = index_file()
        results = csearch.search(["BOOKING"], path)
        os.unlink(path)
        self.assertEqual(len(results), 1)

    def test_multiple_terms_require_all(self):
        path = index_file()
        self.assertEqual(len(csearch.search(["api", "bug"], path)), 1)
        path2 = index_file()
        self.assertEqual(len(csearch.search(["api", "keyword"], path2)), 0)
        os.unlink(path)
        os.unlink(path2)

    def test_matches_cwd_field(self):
        path = index_file()
        results = csearch.search(["repos"], path)
        os.unlink(path)
        self.assertEqual(results[0]["session_id"], "sess-c")

    def test_no_terms_returns_all(self):
        path = index_file()
        results = csearch.search([], path)
        os.unlink(path)
        self.assertEqual(len(results), 3)

    def test_newest_first(self):
        path = index_file()
        results = csearch.search([], path)
        os.unlink(path)
        self.assertEqual(results[0]["date"], "2026-08-06")

    def test_missing_index_returns_empty(self):
        self.assertEqual(csearch.search(["x"], "/nonexistent/index.tsv"), [])

    def test_a_term_matches_only_itself(self):
        """No code or alias expansion: an abbreviation that appears nowhere in
        the index finds nothing, even when a row names what it abbreviates."""
        path = index_file()
        results = csearch.search(["acr"], path)
        os.unlink(path)
        self.assertEqual(results, [])

    def test_search_takes_no_name_map(self):
        """A term is matched as typed: there is no name map to expand it with."""
        path = index_file()
        with self.assertRaises(TypeError):
            csearch.search(["acme"], path, names={"ACR": "Acme Resorts"})
        os.unlink(path)

    def test_skips_malformed_rows(self):
        fd, path = tempfile.mkstemp(suffix=".tsv")
        with os.fdopen(fd, "w") as f:
            f.write("bad row without tabs\n")
            f.write(ROWS[0] + "\n")
        results = csearch.search([], path)
        os.unlink(path)
        self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
