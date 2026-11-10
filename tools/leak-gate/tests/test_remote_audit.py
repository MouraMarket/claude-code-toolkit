"""The remote audit: advertised refs, pull-request cross-check, isolated fetch.

Hermetic: every "remote" is a local bare repository standing in for a host,
with `refs/pull/N/head` refs written the way GitHub advertises them. The
pull-request list comes from a JSON file, or from a stubbed GitHub call. No
network. Synthetic terms, names and identities only.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.realpath(__file__))
TOOL = os.path.dirname(HERE)
sys.path.insert(0, TOOL)
import history_lib as hist  # noqa: E402
import leak_gate_lib as lib  # noqa: E402
import remote_lib as remote  # noqa: E402
import selftest_lib as st  # noqa: E402

CLI = os.path.join(TOOL, "leak-gate")
SUB = st.SYNTHETIC_TERMS[0]["term"]


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="leakgate-remote-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.priv = os.path.join(self.base, "private")
        os.mkdir(self.priv)
        self.profile = self.write_json("profile.json", {
            "schema_version": 1, "modes": {m: "t" for m in lib.MODES},
            "terms": st.SYNTHETIC_TERMS})
        self.names = self.write_text("names", st.SYNTHETIC_NAMES)
        self.words = self.write_text("words", st.SYNTHETIC_WORDS)
        self.runner = st.Runner(os.path.join(self.base, "repos"), self.names, self.words)
        self.remote = os.path.join(self.base, "remote.git")
        subprocess.run(["git", "clone", "-q", "--bare", self.runner.clean.path, self.remote],
                       env=hist.git_env(), check=True)
        self.expected = self.freeze()
        self.pulls = self.write_json("pulls.json", {"pull_requests": []})

    def write_json(self, name, data):
        path = os.path.join(self.priv, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return path

    def write_text(self, name, text):
        path = os.path.join(self.priv, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def git_remote(self, *args):
        return subprocess.run(["git", "-C", self.remote] + list(args), env=hist.git_env(),
                              stdout=subprocess.PIPE, check=True).stdout.decode().strip()

    def freeze(self, name="expected.json"):
        """The remote's advertised refs as they stand: the reviewed publication."""
        refs = remote.advertised_refs(self.remote, hist.git_env())
        return self.write_json(name, {"schema_version": 1, "refs": refs})

    def push_branch(self, ref, plant=None, value=SUB):
        """Write a commit (optionally planted) onto the remote at `ref`."""
        fx = self.runner.fresh()
        if plant:
            plant(fx, value)
        else:
            fx.commit("work")
        head = fx.git("rev-parse", "HEAD").decode()
        fx.git("push", "-q", self.remote, "HEAD:%s" % ref)
        return head

    def audit(self, **kw):
        kw.setdefault("pulls_path", self.pulls)
        return remote.run_remote_audit(self.remote, kw.pop("expected", self.expected),
                                       self.profile, st.IDENTITY, names_path=self.names,
                                       words_path=self.words, **kw)

    def found(self, **kw):
        return sorted("%s/%s" % (f.cls, f.surface) for f in self.audit(**kw)[1])


# --------------------------------------------------------------------------


class TestCleanRemote(Base):
    def test_a_remote_matching_its_expected_refs_is_clean(self):
        code, findings, lines, summary = self.audit()
        self.assertEqual((code, findings, lines), (0, [], []))
        self.assertEqual(summary["access"], "anonymous")
        self.assertGreaterEqual(summary["history"]["commits"], 1)
        self.assertEqual(summary["advertised_refs"], 3)  # HEAD, main, tag t0

    def test_the_audit_repository_is_removed_afterwards(self):
        # A private temp dir, never the shared one: a concurrent audit elsewhere
        # on the machine would otherwise read as this audit's leftover.
        scratch = os.path.join(self.base, "tmp")
        os.mkdir(scratch)
        with mock.patch.object(tempfile, "tempdir", scratch):
            self.audit()
        self.assertEqual([d for d in os.listdir(scratch) if d.startswith("leak-gate-audit-")], [])


class TestRefComparison(Base):
    def test_an_unexpected_branch_is_a_finding_and_is_scanned(self):
        self.push_branch("refs/heads/surprise", st.plant_file)
        found = self.found()
        self.assertIn("unexpected-ref/remote", found)
        self.assertIn("term/content", found)

    def test_a_missing_expected_ref_is_a_finding(self):
        self.git_remote("update-ref", "-d", "refs/tags/t0")
        self.assertEqual(self.found(), ["missing-ref/remote"])

    def test_a_moved_ref_is_a_finding(self):
        self.push_branch("refs/heads/main")
        self.assertIn("moved-ref/remote", self.found())

    def test_head_is_compared_like_any_ref(self):
        with open(self.expected) as fh:
            refs = json.load(fh)["refs"]
        del refs["HEAD"]
        exp = self.write_json("nohead.json", {"schema_version": 1, "refs": refs})
        self.assertEqual(self.found(expected=exp), ["unexpected-ref/remote"])


class TestPullRequests(Base):
    def add_pull(self, number, plant=None, identity=None):
        fx = self.runner.fresh()
        if plant:
            plant(fx, SUB)
        else:
            fx.commit("contribution", author=identity or st.IDENTITY,
                      committer=identity or st.IDENTITY)
        fx.git("push", "-q", self.remote, "HEAD:refs/pull/%d/head" % number)

    def test_a_pull_request_from_someone_else_is_scanned_and_blocks(self):
        self.add_pull(1, identity=st.OTHER_IDENTITY)
        pulls = self.write_json("p1.json", {"pull_requests": [1]})
        found = self.found(pulls_path=pulls)
        self.assertIn("unexpected-ref/remote", found)
        self.assertIn("identity/commit", found)

    def test_a_planted_term_in_a_pull_request_is_found(self):
        self.add_pull(1, plant=st.plant_deleted)
        pulls = self.write_json("p1.json", {"pull_requests": [1]})
        self.assertIn("term/content", self.found(pulls_path=pulls))

    def test_a_listed_pull_request_with_no_advertised_ref_refuses(self):
        pulls = self.write_json("p3.json", {"pull_requests": [3]})
        with self.assertRaises(lib.Refused) as ctx:
            self.audit(pulls_path=pulls)
        self.assertIn("no advertised head ref", str(ctx.exception))

    def test_an_advertised_pull_ref_the_list_omits_refuses(self):
        self.add_pull(4)
        with self.assertRaises(lib.Refused) as ctx:
            self.audit()  # the list is empty
        self.assertIn("list is incomplete", str(ctx.exception))

    def test_a_pull_request_with_only_a_merge_ref_refuses(self):
        head = self.git_remote("rev-parse", "refs/heads/main")
        self.git_remote("update-ref", "refs/pull/5/merge", head)
        pulls = self.write_json("p5.json", {"pull_requests": [5]})
        with self.assertRaises(lib.Refused) as ctx:
            self.audit(pulls_path=pulls)
        self.assertIn("no advertised head ref", str(ctx.exception))

    def test_a_pull_ref_of_an_unknown_shape_refuses(self):
        head = self.git_remote("rev-parse", "refs/heads/main")
        self.git_remote("update-ref", "refs/pull/x/head", head)
        with self.assertRaises(lib.Refused) as ctx:
            self.audit()
        self.assertIn("unknown shape", str(ctx.exception))

    def test_a_github_remote_takes_its_list_from_the_api_only(self):
        url = "https://github.com/example-owner/example-repo.git"
        self.assertEqual(remote.github_repo(url), ("example-owner", "example-repo"))
        for kw in ({"pulls_path": self.pulls}, {"no_pull_requests": True, "pulls_path": None}):
            with self.subTest(kw=kw), self.assertRaises(lib.Refused) as ctx:
                remote.run_remote_audit(url, self.expected, self.profile, st.IDENTITY,
                                        names_path=self.names, words_path=self.words, **kw)
            self.assertIn("GitHub API", str(ctx.exception))

    def test_a_github_remote_is_cross_checked_against_the_api(self):
        # Point the "GitHub" URL at the local bare remote, and stub the API.
        self.add_pull(1)
        url = "https://github.com/example-owner/example-repo"
        real_net = remote._net

        def local(args, env, what, cwd=None):
            return real_net([self.remote if a == url else a for a in args], env, what, cwd)
        with mock.patch.object(remote, "_net", local), \
                mock.patch.object(remote, "github_pulls", lambda o, r: set()):
            with self.assertRaises(lib.Refused):  # the API omits PR 1
                remote.run_remote_audit(url, self.expected, self.profile, st.IDENTITY,
                                        names_path=self.names, words_path=self.words)
        with mock.patch.object(remote, "_net", local), \
                mock.patch.object(remote, "github_pulls", lambda o, r: {1}):
            code, _f, _l, summary = remote.run_remote_audit(
                url, self.expected, self.profile, st.IDENTITY, names_path=self.names,
                words_path=self.words)
        self.assertEqual((code, summary["pull_request_source"]), (1, "github-api"))

    def test_github_url_shapes(self):
        cases = {
            "https://github.com/a-b/c.d": ("a-b", "c.d"),
            "git@github.com:a/b.git": ("a", "b"),
            "ssh://git@github.com/a/b": ("a", "b"),
            "https://github.com:443/a/b.git": ("a", "b"),
            "HTTPS://GitHub.COM/a/b/": ("a", "b"),
            "https://someone@github.com/a/b": ("a", "b"),
            "ssh://git@ssh.github.com:443/a/b.git": ("a", "b"),
            "http://www.github.com/a/b": ("a", "b"),
            "git@GitHub.com:a/b": ("a", "b"),
            "https://example.com/a/b": None,
            "/some/local/path.git": None,
        }
        for url, want in cases.items():
            with self.subTest(url=url):
                self.assertEqual(remote.github_repo(url), want)

    def test_an_unreadable_github_url_refuses_never_waives(self):
        for url in ("https://github.com/only-owner", "https://github.com/a/b/c",
                    "https://example.invalid/github.com/a/b"):
            with self.subTest(url=url), self.assertRaises(lib.Refused):
                remote.github_repo(url)

    def test_the_no_pull_requests_waiver_is_refused_for_a_github_port_url(self):
        with self.assertRaises(lib.Refused) as ctx:
            remote.run_remote_audit("https://github.com:443/a/b.git", self.expected,
                                    self.profile, st.IDENTITY, no_pull_requests=True,
                                    names_path=self.names, words_path=self.words)
        self.assertIn("GitHub API", str(ctx.exception))


class TestRefusals(Base):
    def refuses(self, **kw):
        with self.assertRaises(lib.Refused) as ctx:
            self.audit(**kw)
        return str(ctx.exception)

    def test_a_remote_with_no_refs_refuses(self):
        empty = os.path.join(self.base, "empty.git")
        subprocess.run(["git", "init", "-q", "--bare", empty], env=hist.git_env(), check=True)
        with self.assertRaises(lib.Refused) as ctx:
            remote.run_remote_audit(empty, self.expected, self.profile, st.IDENTITY,
                                    pulls_path=self.pulls, names_path=self.names,
                                    words_path=self.words)
        # The remote's own check, not the history gate's later empty-scope one.
        self.assertIn("advertises no refs", str(ctx.exception))

    def test_an_unreachable_remote_refuses(self):
        with self.assertRaises(lib.Refused):
            remote.run_remote_audit(os.path.join(self.base, "nowhere.git"), self.expected,
                                    self.profile, st.IDENTITY, pulls_path=self.pulls,
                                    names_path=self.names, words_path=self.words)

    def test_a_partial_fetch_refuses(self):
        real_net = remote._net

        def narrow(args, env, what, cwd=None):
            if "fetch" in args:
                args = [a if a != "+refs/*:refs/*" else "+refs/heads/*:refs/heads/*"
                        for a in args]
            return real_net(args, env, what, cwd)
        with mock.patch.object(remote, "_net", narrow):
            self.assertIn("partial fetch", self.refuses())

    def test_a_head_that_moves_during_the_fetch_refuses(self):
        # Both branches are expected and both are fetched; only HEAD moves.
        self.push_branch("refs/heads/other")
        self.expected = self.freeze("two.json")
        real_fetch = remote.fetch_all

        def switch_head(url, advertised, audit, env):
            real_fetch(url, advertised, audit, env)
            self.git_remote("symbolic-ref", "HEAD", "refs/heads/other")
        with mock.patch.object(remote, "fetch_all", switch_head):
            self.assertIn("changed during the audit", self.refuses())

    def test_no_pull_request_source_or_two_of_them_refuses(self):
        self.assertIn("exactly one", self.refuses(pulls_path=None))
        self.assertIn("exactly one", self.refuses(no_pull_requests=True))

    def test_an_empty_or_malformed_expectation_refuses(self):
        for name, data in (("e1.json", {"schema_version": 1, "refs": {}}),
                           ("e2.json", {"schema_version": 1, "refs": {"refs/heads/x": "abc"}}),
                           ("e3.json", {"schema_version": 1, "refs": {"main": "0" * 40}})):
            with self.subTest(name=name):
                self.refuses(expected=self.write_json(name, data))

    def test_a_bad_identity_refuses(self):
        with self.assertRaises(lib.Refused):
            remote.run_remote_audit(self.remote, self.expected, self.profile, "nobody",
                                    pulls_path=self.pulls)


class TestAccess(Base):
    def test_anonymous_mode_reads_no_user_or_system_config(self):
        env = remote.network_env(False)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")

    def test_authenticated_mode_refuses_a_url_rewritten_by_config(self):
        home = os.path.join(self.base, "home")
        os.mkdir(home)
        with open(os.path.join(home, ".gitconfig"), "w") as fh:
            fh.write('[url "%s"]\n\tinsteadOf = %s\n'
                     % (self.remote, "https://example.invalid/rewritten.git"))
        with mock.patch.dict(os.environ, {"HOME": home, "XDG_CONFIG_HOME": home}):
            with self.assertRaises(lib.Refused) as ctx:
                remote.run_remote_audit("https://example.invalid/rewritten.git", self.expected,
                                        self.profile, st.IDENTITY, pulls_path=self.pulls,
                                        authenticated=True, names_path=self.names,
                                        words_path=self.words)
        self.assertIn("rewrites", str(ctx.exception))

    def test_authenticated_mode_audits_an_unrewritten_remote(self):
        home = os.path.join(self.base, "home")
        os.mkdir(home)
        with mock.patch.dict(os.environ, {"HOME": home, "XDG_CONFIG_HOME": home}):
            code, _f, _l, summary = self.audit(authenticated=True)
        self.assertEqual((code, summary["access"]), (0, "authenticated"))


class TestCli(Base):
    def run_cli(self, *args):
        proc = subprocess.run([sys.executable, CLI, "remote-audit"] + list(args),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"))
        return proc.returncode, proc.stdout.decode() + proc.stderr.decode()

    def args(self, *extra):
        return ["--remote", self.remote, "--expected", self.expected, "--profile",
                self.profile, "--identity", st.IDENTITY, "--pulls", self.pulls,
                "--names-file", self.names, "--words-file", self.words] + list(extra)

    def test_exit_codes_receipt_and_no_value_in_output(self):
        receipt = os.path.join(self.priv, "r.json")
        self.assertEqual(self.run_cli(*self.args("--receipt", receipt))[0], 0)
        self.push_branch("refs/heads/x-%s" % SUB)
        code, out = self.run_cli(*self.args("--receipt", receipt))
        self.assertEqual(code, 1)
        self.assertNotIn(SUB, out)
        with open(receipt) as fh:
            data = json.load(fh)
        self.assertEqual(data["exit"], 1)
        self.assertNotIn(SUB, json.dumps(data))
        os.remove(self.pulls)
        self.assertEqual(self.run_cli(*self.args("--receipt", receipt))[0], 2)
        with open(receipt) as fh:
            self.assertEqual(json.load(fh)["exit"], 2)


if __name__ == "__main__":
    unittest.main()
