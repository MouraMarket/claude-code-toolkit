#!/usr/bin/env python3
"""Hermetic tests for actions/claude-pr-review/pr-review.sh.

`gh` is a stub: these tests prove the script's model of the GitHub API and its
own refusals, never GitHub's behaviour. Shared helpers sit at module level so
later subcommand classes reuse them."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = Path(os.path.realpath(__file__)).parents[4]
SCRIPT = REPO_ROOT / "actions" / "claude-pr-review" / "pr-review.sh"
REPO = "example-owner/fixture"
SHA = "a" * 40
TOKEN = "tok-secret-value-123"

GH_STUB = r'''#!/usr/bin/env python3
import json
import os
import sys

args = sys.argv[1:]
stub = os.environ["STUB_DIR"]
repo = os.environ["GH_REPO"]
with open(os.path.join(stub, "log.jsonl"), "a", encoding="utf-8") as out:
    out.write(json.dumps({"argv": args}) + "\n")


def count(kind):
    path = os.path.join(stub, "count-" + kind)
    try:
        with open(path, encoding="utf-8") as handle:
            number = int(handle.read() or "0")
    except OSError:
        number = 0
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(number + 1))
    return number + 1


def serve(prefix, kind):
    number = count(kind)
    chosen = None
    while number >= 1:
        candidate = os.path.join(stub, "%s-%d.json" % (prefix, number))
        if os.path.exists(candidate):
            chosen = candidate
            break
        number -= 1
    if chosen is None:
        sys.exit(98)
    with open(chosen, encoding="utf-8") as handle:
        sys.stdout.write(handle.read())


if args[:1] == ["api"]:
    paths = [a for a in args[1:] if not a.startswith("-")]
    path = paths[0] if paths else ""
    if not path.startswith("repos/" + repo + "/"):
        sys.exit(97)
    if "--paginate" in args and path.endswith("/reviews"):
        if os.path.exists(os.path.join(stub, "fail-reviews")):
            sys.exit(1)
        serve("reviews", "reviews")
        sys.exit(0)
    if "/pulls/" in path and path.count("/") == 4:
        if os.path.exists(os.path.join(stub, "fail-pull")):
            sys.exit(1)
        serve("pull", "pull")
        sys.exit(0)
    sys.exit(98)
if args[:1] == ["pr"]:
    if not any(args[i] == "--repo" and args[i + 1] == repo
               for i in range(len(args) - 1)):
        sys.exit(97)
    if args[1:2] == ["comment"]:
        try:
            with open(os.path.join(stub, "comment-exit"), encoding="utf-8") as handle:
                sys.exit(int(handle.read().strip() or "0"))
        except OSError:
            sys.exit(0)
sys.exit(98)
'''


def pull_json(**overrides):
    """A pull-request payload that passes validate; overrides replace keys."""
    data = {
        "state": "open",
        "head": {"sha": SHA, "repo": {"full_name": REPO}},
        "base": {"ref": "main"},
    }
    data.update(overrides)
    return data


class Harness:
    """A temp dir with a stub `gh`, a `python3` link, and a built-from-scratch env."""

    def __init__(self, testcase):
        self.root = Path(tempfile.mkdtemp(prefix="pr-review-test."))
        testcase.addCleanup(shutil.rmtree, str(self.root), True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.stub = self.root / "stub"
        self.stub.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.cwd = self.root / "cwd"
        self.cwd.mkdir()
        self.state = self.root / "state"
        # Stub scripts use `env python3`; run them on the suite's interpreter.
        (self.bin / "python3").symlink_to(sys.executable)
        gh = self.bin / "gh"
        gh.write_text(GH_STUB, encoding="utf-8")
        gh.chmod(0o755)

    def env(self, **overrides):
        env = {
            "PATH": "%s:/usr/bin:/bin" % self.bin,
            "HOME": str(self.home),
            "STUB_DIR": str(self.stub),
            "GH_REPO": REPO,
            "GH_TOKEN": "gh-token-value",
            "PR_NUMBER": "7",
            "MODEL": "opus",
            "OAUTH_TOKEN": TOKEN,
            "RUN_ID": "12345",
            "RUN_ATTEMPT": "1",
            "REVIEWER_LOGIN": "github-actions[bot]",
            "RUN_URL": "https://example.invalid/runs/12345",
            "STATE_DIR": str(self.state),
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def write_json(self, name, data):
        (self.stub / name).write_text(
            data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def run(self, sub, **overrides):
        args = [str(SCRIPT)] + ([sub] if sub is not None else [])
        return subprocess.run(
            args, env=self.env(**overrides), cwd=str(self.cwd),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)

    def calls(self):
        log = self.stub / "log.jsonl"
        if not log.exists():
            return []
        with open(str(log), encoding="utf-8") as handle:
            return [json.loads(line)["argv"] for line in handle if line.strip()]

    def read_state(self, name):
        with open(str(self.state / name), encoding="utf-8") as handle:
            return handle.read()


class HarnessTests(unittest.TestCase):
    def test_script_exists_and_is_executable(self):
        self.assertTrue(SCRIPT.exists(), str(SCRIPT))
        self.assertTrue(os.access(str(SCRIPT), os.X_OK))

    def test_stub_refuses_implicit_repository_calls(self):
        h = Harness(self)
        env = h.env()
        gh = str(h.bin / "gh")
        for argv in (["api", "repos/other/repo/pulls/7"],
                     ["api", "user"],
                     ["pr", "comment", "7", "--body", "x"],
                     ["pr", "comment", "7", "--repo", "other/repo", "--body", "x"]):
            result = subprocess.run([gh] + argv, env=env, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(result.returncode, 97, argv)
        # Positive control: the explicit repository is accepted.
        result = subprocess.run([gh, "pr", "comment", "7", "--repo", REPO, "--body", "x"],
                                env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.assertEqual(result.returncode, 0)


class UsageTests(unittest.TestCase):
    def test_unknown_subcommand_is_2(self):
        h = Harness(self)
        result = h.run("bogus")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("usage", result.stdout.lower())

    def test_missing_subcommand_is_2(self):
        h = Harness(self)
        result = h.run(None)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("usage", result.stdout.lower())


class ValidateTests(unittest.TestCase):
    def valid(self, **overrides):
        """Run validate against an open same-repo PR; return (harness, result)."""
        h = Harness(self)
        pull = overrides.pop("pull", None)
        h.write_json("pull-1.json", pull if pull is not None else pull_json())
        return h, h.run("validate", **overrides)

    def refused(self, expected, **overrides):
        h, result = self.valid(**overrides)
        self.assertEqual(result.returncode, expected, result.stdout)
        self.assertFalse((h.state / "pr.env").exists())
        return h, result

    def test_passes_and_writes_pr_env_exactly(self):
        h, result = self.valid()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(h.read_state("pr.env"),
                         "PR_NUMBER=7\nHEAD_SHA=%s\nBASE_REF=main\n" % SHA)
        self.assertEqual(h.calls(), [["api", "repos/%s/pulls/7" % REPO]])
        self.assertEqual([p.name for p in h.state.iterdir()], ["pr.env"])

    def test_creates_nested_state_dir(self):
        h2 = Harness(self)
        h2.write_json("pull-1.json", pull_json())
        nested = h2.root / "a" / "b" / "state"
        result = h2.run("validate", STATE_DIR=str(nested))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((nested / "pr.env").exists())

    def test_state_dir_unset_or_empty_is_2(self):
        for value in (None, ""):
            h = Harness(self)
            h.write_json("pull-1.json", pull_json())
            result = h.run("validate", STATE_DIR=value)
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertEqual(h.calls(), [])

    def test_bad_pr_number_is_2_with_no_gh_call(self):
        for value in ("abc", "0", "", "-3", "07", "1 2", "1;ls"):
            h, result = self.refused(2, PR_NUMBER=value)
            self.assertEqual(h.calls(), [], value)

    def test_large_pr_number_control(self):
        h, result = self.valid(PR_NUMBER="1234567")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PR_NUMBER=1234567\n", h.read_state("pr.env"))

    def test_unknown_model_is_2(self):
        self.refused(2, MODEL="opus-latest")
        self.refused(2, MODEL="")
        for model in ("opus", "sonnet", "haiku"):
            h, result = self.valid(MODEL=model)
            self.assertEqual(result.returncode, 0, model)

    def test_empty_token_is_2_and_value_never_printed(self):
        h, result = self.refused(2, OAUTH_TOKEN="")
        self.assertIn("CLAUDE_CODE_OAUTH_TOKEN", result.stdout)
        h, result = self.valid()
        self.assertNotIn(TOKEN, result.stdout)
        h, result = self.refused(2, MODEL="bad")
        self.assertNotIn(TOKEN, result.stdout)

    def test_bad_run_id_or_attempt_is_2(self):
        self.refused(2, RUN_ID="12a")
        self.refused(2, RUN_ID="")
        self.refused(2, RUN_ATTEMPT="x")
        self.refused(2, RUN_ATTEMPT="")
        h, result = self.valid(RUN_ID="9", RUN_ATTEMPT="3")
        self.assertEqual(result.returncode, 0)

    def test_bad_repo_is_2_with_no_gh_call(self):
        for value in ("", "noslash", "a/b/c", "a b/c", "a/b;c", "/b", "a/"):
            h, result = self.refused(2, GH_REPO=value)
            self.assertEqual(h.calls(), [], value)

    def test_closed_pr_is_1(self):
        self.refused(1, pull=pull_json(state="closed"))

    def test_fork_head_is_1_naming_both_repos(self):
        h, result = self.refused(1, pull=pull_json(
            head={"sha": SHA, "repo": {"full_name": "someone/fork"}}))
        self.assertIn("someone/fork", result.stdout)
        self.assertIn(REPO, result.stdout)

    def test_null_head_repo_is_1(self):
        self.refused(1, pull=pull_json(head={"sha": SHA, "repo": None}))

    def test_head_repo_compared_lowercase(self):
        h, result = self.valid(pull=pull_json(
            head={"sha": SHA, "repo": {"full_name": REPO.upper()}}))
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_bad_head_sha_is_2(self):
        for sha in ("a" * 39, "a" * 41, "A" * 40, "g" * 40, ""):
            self.refused(2, pull=pull_json(
                head={"sha": sha, "repo": {"full_name": REPO}}))

    def test_bad_base_ref_is_2(self):
        for ref in ("-main", "ma in", "main;x", ""):
            self.refused(2, pull=pull_json(base={"ref": ref}))
        h, result = self.valid(pull=pull_json(base={"ref": "release/1.x_y-z"}))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("BASE_REF=release/1.x_y-z\n", h.read_state("pr.env"))

    def test_gh_failure_is_2(self):
        h = Harness(self)
        h.write_json("pull-1.json", pull_json())
        (h.stub / "fail-pull").write_text("", encoding="utf-8")
        result = h.run("validate")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertFalse((h.state / "pr.env").exists())

    def test_non_json_response_is_2(self):
        h = Harness(self)
        h.write_json("pull-1.json", "not json")
        result = h.run("validate")
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_errors_use_error_annotation(self):
        h, result = self.refused(2, MODEL="bad")
        self.assertIn("::error::", result.stdout)

    def test_unusable_state_dir_is_2(self):
        h = Harness(self)
        h.write_json("pull-1.json", pull_json())
        blocker = h.root / "file"
        blocker.write_text("x", encoding="utf-8")
        result = h.run("validate", STATE_DIR=str(blocker))
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_pr_env_not_written_before_fork_check(self):
        # A refused fork leaves no pr.env even when a stale one is not present.
        h, result = self.refused(1, pull=pull_json(
            head={"sha": SHA, "repo": {"full_name": "someone/fork"}}))
        self.assertFalse(h.state.exists() and any(h.state.iterdir()))


class SnapshotTests(unittest.TestCase):
    def prepared(self):
        h = Harness(self)
        h.write_json("pull-1.json", pull_json())
        self.assertEqual(h.run("validate").returncode, 0)
        return h

    def test_snapshot_writes_sorted_ids_across_pages(self):
        h = self.prepared()
        h.write_json("reviews-1.json",
                     '[{"id": 30}, {"id": 4}][{"id": 100}]\n[{"id": 7}]')
        result = h.run("snapshot")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(h.read_state("reviews-before.txt"), "4\n7\n30\n100\n")
        self.assertIn(["api", "--paginate", "repos/%s/pulls/7/reviews" % REPO],
                      h.calls())

    def test_snapshot_empty_list_writes_empty_file(self):
        h = self.prepared()
        h.write_json("reviews-1.json", "[]")
        result = h.run("snapshot")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(h.read_state("reviews-before.txt"), "")

    def test_snapshot_requires_pr_env(self):
        h = Harness(self)
        h.write_json("reviews-1.json", "[]")
        h.state.mkdir()
        result = h.run("snapshot")
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("pr.env", result.stdout)
        self.assertEqual(h.calls(), [])

    def test_snapshot_refuses_pr_env_missing_a_key(self):
        h = self.prepared()
        h.write_json("reviews-1.json", "[]")
        good = h.read_state("pr.env").splitlines()
        for drop in range(3):
            kept = [line for i, line in enumerate(good) if i != drop]
            (h.state / "pr.env").write_text("\n".join(kept) + "\n", encoding="utf-8")
            result = h.run("snapshot")
            self.assertEqual(result.returncode, 2, drop)
            self.assertIn("validate", result.stdout)
        (h.state / "pr.env").write_text("\n".join(good) + "\n", encoding="utf-8")
        self.assertEqual(h.run("snapshot").returncode, 0)

    def test_snapshot_refuses_malformed_pr_env_values(self):
        h = self.prepared()
        h.write_json("reviews-1.json", "[]")
        for text in ("PR_NUMBER=0\nHEAD_SHA=%s\nBASE_REF=main\n" % SHA,
                     "PR_NUMBER=7\nHEAD_SHA=abc\nBASE_REF=main\n",
                     "PR_NUMBER=7\nHEAD_SHA=%s\nBASE_REF=-x\n" % SHA):
            (h.state / "pr.env").write_text(text, encoding="utf-8")
            self.assertEqual(h.run("snapshot").returncode, 2, text)

    def test_pr_env_is_parsed_not_sourced(self):
        h = self.prepared()
        h.write_json("reviews-1.json", "[]")
        marker = h.root / "pwned"
        (h.state / "pr.env").write_text(
            "PR_NUMBER=7\nHEAD_SHA=%s\nBASE_REF=main\ntouch %s\n" % (SHA, marker),
            encoding="utf-8")
        h.run("snapshot")
        self.assertFalse(marker.exists())

    def test_snapshot_gh_failure_is_2(self):
        h = self.prepared()
        (h.stub / "fail-reviews").write_text("", encoding="utf-8")
        result = h.run("snapshot")
        self.assertEqual(result.returncode, 2, result.stdout)

    def test_snapshot_state_dir_unset_is_2(self):
        h = self.prepared()
        self.assertEqual(h.run("snapshot", STATE_DIR=None).returncode, 2)


class ReportTests(unittest.TestCase):
    def comments(self, h):
        return [c for c in h.calls() if c[:2] == ["pr", "comment"]]

    def test_comments_once_without_verified(self):
        h = Harness(self)
        h.state.mkdir()
        result = h.run("report")
        self.assertEqual(result.returncode, 0, result.stdout)
        comments = self.comments(h)
        self.assertEqual(len(comments), 1)
        argv = comments[0]
        self.assertEqual(argv[:3], ["pr", "comment", "7"])
        self.assertIn("--repo", argv)
        self.assertEqual(argv[argv.index("--repo") + 1], REPO)
        body = argv[argv.index("--body") + 1]
        self.assertIn("**Claude PR review did not complete.**", body)
        self.assertIn("has not been reviewed", body)
        self.assertTrue(body.endswith("Run: https://example.invalid/runs/12345"))

    def test_silent_with_verified(self):
        h = Harness(self)
        h.state.mkdir()
        (h.state / "verified").write_text("", encoding="utf-8")
        result = h.run("report")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertEqual(h.calls(), [])

    def test_comments_when_state_dir_unset(self):
        h = Harness(self)
        result = h.run("report", STATE_DIR=None)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.comments(h)), 1)

    def test_comments_when_state_dir_is_a_regular_file(self):
        h = Harness(self)
        h.write_json("pull-1.json", pull_json())
        blocker = h.root / "file"
        blocker.write_text("x", encoding="utf-8")
        validated = h.run("validate", STATE_DIR=str(blocker))
        self.assertEqual(validated.returncode, 2, validated.stdout)
        result = h.run("report", STATE_DIR=str(blocker))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.comments(h)), 1)

    def test_comments_after_validate_refusal_with_no_state(self):
        h = Harness(self)
        h.write_json("pull-1.json", pull_json())
        self.assertEqual(h.run("validate", MODEL="bad").returncode, 2)
        result = h.run("report")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(self.comments(h)), 1)

    def test_no_gh_call_for_bad_pr_number(self):
        for value in ("abc", "0", "", "-1", "1;ls"):
            h = Harness(self)
            h.state.mkdir()
            result = h.run("report", PR_NUMBER=value)
            self.assertEqual(result.returncode, 0, value)
            self.assertEqual(h.calls(), [], value)
            self.assertIn("::warning::", result.stdout, value)

    def test_no_gh_call_for_bad_repo(self):
        h = Harness(self)
        result = h.run("report", GH_REPO="not a repo")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(h.calls(), [])
        self.assertIn("::warning::", result.stdout)

    def test_exits_0_when_comment_fails(self):
        h = Harness(self)
        h.state.mkdir()
        (h.stub / "comment-exit").write_text("1", encoding="utf-8")
        result = h.run("report")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.comments(h)), 1)
        self.assertIn("::warning::", result.stdout)


def git(repo, *args):
    """Run git in a fixture repository with a hermetic environment."""
    env = {"PATH": "/usr/bin:/bin", "HOME": str(repo), "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_CONFIG_NOSYSTEM": "1"}
    result = subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgsign=false", "-C", str(repo)] + list(args),
        env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
    if result.returncode != 0:
        raise AssertionError("git %s failed: %s" % (args, result.stdout))
    return result.stdout.strip()


class FixtureRepo:
    """base commit, a head commit on top, origin/main at the base, HEAD at the head."""

    def __init__(self, harness):
        self.path = harness.root / "workspace"
        self.path.mkdir()
        git(self.path, "init", "--quiet")
        (self.path / "a.txt").write_text("one\n", encoding="utf-8")
        git(self.path, "add", "a.txt")
        git(self.path, "commit", "--quiet", "-m", "base")
        self.base = git(self.path, "rev-parse", "HEAD")
        (self.path / "a.txt").write_text("two\n", encoding="utf-8")
        git(self.path, "commit", "--quiet", "-am", "head")
        self.head = git(self.path, "rev-parse", "HEAD")
        git(self.path, "update-ref", "refs/remotes/origin/main", self.base)
        git(self.path, "checkout", "--quiet", "--detach", self.head)


GIT_ENV = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}


class BaseTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self)
        self.repo = FixtureRepo(self.h)
        self.h.state.mkdir()

    def write_pr_env(self, head=None, ref="main"):
        (self.h.state / "pr.env").write_text(
            "PR_NUMBER=7\nHEAD_SHA=%s\nBASE_REF=%s\n" % (head or self.repo.head, ref),
            encoding="utf-8")

    def run_base(self, **overrides):
        env = dict(GIT_ENV, WORKSPACE=str(self.repo.path))
        env.update(overrides)
        return self.h.run("base", **env)

    def test_passes_leaves_head_at_base_and_writes_base_env(self):
        self.write_pr_env()
        result = self.run_base()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(git(self.repo.path, "rev-parse", "HEAD"), self.repo.base)
        self.assertEqual(self.h.read_state("base.env"), "BASE_SHA=%s\n" % self.repo.base)
        # The head commit stays reachable for git diff base...head.
        git(self.repo.path, "cat-file", "-e", self.repo.head + "^{commit}")

    def test_head_moved_is_1_and_changes_nothing(self):
        self.write_pr_env()
        git(self.repo.path, "checkout", "--quiet", "--detach", self.repo.base)
        result = self.run_base()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("moved", result.stdout)
        self.assertFalse((self.h.state / "base.env").exists())

    def test_absent_remote_ref_is_2(self):
        self.write_pr_env(ref="nonexistent")
        result = self.run_base()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("not fetched", result.stdout)
        self.assertEqual(git(self.repo.path, "rev-parse", "HEAD"), self.repo.head)

    def test_pr_env_absent_is_2(self):
        result = self.run_base()
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("pr.env", result.stdout)

    def test_workspace_unset_is_2(self):
        self.write_pr_env()
        result = self.h.run("base", **GIT_ENV)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("WORKSPACE", result.stdout)


SKILL_BODY = "Review pull request **$ARGUMENTS** now.\n\n---\n\nA horizontal rule above.\n"
AGENT_BODY = ("Agent rubric text.\n\n---\n\nAnother rule.\n\n"
              "**If there are no findings, say so in two lines and stop.**\n")
OVERRIDE = "does NOT apply here"
RANDOM_BYTES = bytes(range(16))
RANDOM_HEX = RANDOM_BYTES.hex()


def make_action_root(harness, pad=0, skill=True, agent=True):
    """A fixture ACTION_ROOT whose files carry frontmatter; pad adds body bytes."""
    root = harness.root / "action"
    filler = "Filler line to pad the file out.\n" * pad
    if skill:
        path = root / "skills" / "maintainer" / "review-pr-ci" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: review-pr-ci\n---\n" + SKILL_BODY + filler,
                        encoding="utf-8")
    if agent:
        path = root / "agents" / "code-reviewer.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: code-reviewer\n---\n" + AGENT_BODY + filler,
                        encoding="utf-8")
    return root


class PromptTests(unittest.TestCase):
    BASE_SHA = "b" * 40

    def setUp(self):
        self.h = Harness(self)
        self.h.state.mkdir()
        (self.h.state / "pr.env").write_text(
            "PR_NUMBER=7\nHEAD_SHA=%s\nBASE_REF=main\n" % SHA, encoding="utf-8")
        (self.h.state / "base.env").write_text(
            "BASE_SHA=%s\n" % self.BASE_SHA, encoding="utf-8")
        self.output = self.h.root / "github-output"
        self.random = self.h.root / "random"
        self.random.write_bytes(RANDOM_BYTES)

    def run_prompt(self, root, **overrides):
        env = {"ACTION_ROOT": str(root), "GITHUB_OUTPUT": str(self.output),
               "PR_REVIEW_RANDOM_SOURCE": str(self.random)}
        env.update(overrides)
        return self.h.run("prompt", **env)

    def prompt_text(self):
        return self.h.read_state("prompt.md")

    def constraints(self, text):
        return text[text.index("## Environment constraints (CI)"):]

    def test_padded_files_pass_and_short_prompt_is_2(self):
        short = self.run_prompt(make_action_root(self.h))
        self.assertEqual(short.returncode, 2, short.stdout)
        self.assertIn("bytes", short.stdout)
        self.h.state.joinpath("prompt.md").unlink()
        shutil.rmtree(str(self.h.root / "action"))
        padded = self.run_prompt(make_action_root(self.h, pad=80))
        self.assertEqual(padded.returncode, 0, padded.stdout)
        self.assertGreaterEqual(len(self.prompt_text().encode("utf-8")), 3000)

    def test_missing_rubric_or_skill_file_is_2(self):
        result = self.run_prompt(make_action_root(self.h, pad=80, agent=False))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("code-reviewer", result.stdout)
        shutil.rmtree(str(self.h.root / "action"))
        result = self.run_prompt(make_action_root(self.h, pad=80, skill=False))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("skill", result.stdout)

    def test_base_env_absent_is_2_naming_the_writer(self):
        (self.h.state / "base.env").unlink()
        result = self.run_prompt(make_action_root(self.h, pad=80))
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("base.env", result.stdout)

    def test_github_output_unset_is_2(self):
        result = self.run_prompt(make_action_root(self.h, pad=80), GITHUB_OUTPUT=None)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("GITHUB_OUTPUT", result.stdout)

    def test_frontmatter_stripped_but_body_rule_kept(self):
        self.assertEqual(
            self.run_prompt(make_action_root(self.h, pad=80)).returncode, 0)
        text = self.prompt_text()
        self.assertNotIn("name: review-pr-ci", text)
        self.assertNotIn("name: code-reviewer", text)
        self.assertTrue(text.startswith("Review pull request"), text[:60])
        self.assertIn("\n---\n\nA horizontal rule above.", text)
        self.assertIn("\n---\n\nAnother rule.", text)

    def test_arguments_replaced_and_identifiers_present(self):
        self.assertEqual(
            self.run_prompt(make_action_root(self.h, pad=80)).returncode, 0)
        text = self.prompt_text()
        self.assertNotIn("$ARGUMENTS", text)
        self.assertIn("Review pull request **7** now.", text)
        self.assertIn(SHA, text)
        self.assertIn(self.BASE_SHA, text)
        self.assertIn("<!-- claude-pr-review run=12345-1 head=%s -->" % SHA, text)
        self.assertEqual(
            text.rstrip("\n").splitlines()[-1],
            "<!-- claude-pr-review run=12345-1 head=%s -->" % SHA)

    def test_constraints_name_the_commands_with_values(self):
        self.assertEqual(
            self.run_prompt(make_action_root(self.h, pad=80)).returncode, 0)
        section = self.constraints(self.prompt_text())
        for command in ("gh pr view 7 --repo %s --json" % REPO,
                        "gh pr diff 7 --repo %s" % REPO,
                        "git diff %s...%s" % (self.BASE_SHA, SHA),
                        "git show %s:<path>" % SHA,
                        "gh pr review 7 --repo %s --comment --body" % REPO,
                        "gh pr review 7 --repo %s --request-changes --body" % REPO):
            self.assertIn(command, section)

    def test_constraints_read_the_pr_with_json_and_never_title_or_body(self):
        self.assertEqual(
            self.run_prompt(make_action_root(self.h, pad=80)).returncode, 0)
        text = self.prompt_text()
        section = self.constraints(text)
        view = "gh pr view 7 --repo %s --json files,commits,statusCheckRollup" % REPO
        self.assertIn(view, section)
        bare = [line for line in section.splitlines()
                if "gh pr view" in line and "--json" not in line
                and not line.startswith("- Bash allows only")]
        self.assertEqual(bare, [])
        sentence = ("Do not read the pull request's title or body: they are the "
                    "author's framing, and the rubric above says to skip the narrative.")
        heading = text.index("## Reviewer rubric (agents/code-reviewer.md)")
        self.assertIn(sentence, section)
        self.assertGreater(text.index(sentence), heading)

    @staticmethod
    def gh_lines_missing_repo(section):
        return [line for line in section.splitlines()
                if "gh " in line and "--repo %s" % REPO not in line]

    def test_every_gh_line_in_constraints_names_the_repo(self):
        self.assertEqual(
            self.run_prompt(make_action_root(self.h, pad=80)).returncode, 0)
        section = self.constraints(self.prompt_text())
        self.assertIn("gh pr diff", section)
        self.assertEqual(self.gh_lines_missing_repo(section), [])
        # Positive control: a line without --repo is detected.
        broken = section + "\n- `gh pr diff 7`\n"
        self.assertEqual(self.gh_lines_missing_repo(broken), ["- `gh pr diff 7`"])

    def test_override_sentence_sits_after_the_rubric_heading(self):
        self.assertEqual(
            self.run_prompt(make_action_root(self.h, pad=80)).returncode, 0)
        text = self.prompt_text()
        heading = text.index("## Reviewer rubric (agents/code-reviewer.md)")
        rubric_line = text.index("If there are no findings, say so in two lines and stop")
        override = text.index(OVERRIDE)
        self.assertGreater(override, heading)
        self.assertGreater(override, rubric_line)
        self.assertEqual(text.count(OVERRIDE), 1)
        self.assertIn("**Findings (0)**", text[override:override + 400])

    def test_github_output_wraps_the_prompt_bytes_exactly(self):
        self.assertEqual(
            self.run_prompt(make_action_root(self.h, pad=80)).returncode, 0)
        prompt = (self.h.state / "prompt.md").read_bytes()
        delimiter = "EOF_" + RANDOM_HEX
        expected = ("text<<%s\n" % delimiter).encode("utf-8") + prompt + \
            (delimiter + "\n").encode("utf-8")
        self.assertEqual(self.output.read_bytes(), expected)

    def test_delimiter_is_32_hex_from_the_default_source(self):
        env = self.h.env(ACTION_ROOT=str(make_action_root(self.h, pad=80)),
                         GITHUB_OUTPUT=str(self.output))
        result = subprocess.run(
            [str(SCRIPT), "prompt"], env=env, cwd=str(self.h.cwd),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, universal_newlines=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout)
        first = self.output.read_text(encoding="utf-8").splitlines()[0]
        self.assertRegex(first, r"^text<<EOF_[0-9a-f]{32}$")

    def test_delimiter_collision_is_2_and_control_passes(self):
        root = make_action_root(self.h, pad=80)
        skill = root / "skills" / "maintainer" / "review-pr-ci" / "SKILL.md"
        with open(str(skill), "a", encoding="utf-8") as handle:
            handle.write("EOF_%s\n" % RANDOM_HEX)
        result = self.run_prompt(root)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("delimiter", result.stdout)
        self.assertFalse(self.output.exists())
        # Positive control: a different random source passes on the same prompt.
        other = self.h.root / "random-other"
        other.write_bytes(bytes(range(16, 32)))
        again = self.run_prompt(root, PR_REVIEW_RANDOM_SOURCE=str(other))
        self.assertEqual(again.returncode, 0, again.stdout)

    def test_real_repository_root_passes_the_floor(self):
        result = self.run_prompt(REPO_ROOT)
        self.assertEqual(result.returncode, 0, result.stdout)
        text = self.prompt_text()
        self.assertGreater(len(text.encode("utf-8")), 2000)
        self.assertIn("**Recommendation:** REQUEST_CHANGES", text)
        self.assertEqual(self.gh_lines_missing_repo(self.constraints(text)), [])


SKILL_MD = REPO_ROOT / "skills" / "maintainer" / "review-pr-ci" / "SKILL.md"
RUN_MARKER = "<!-- claude-pr-review run=12345-1 head=%s -->" % SHA

GOOD_BODY = """**Recommendation:** {verdict}

**Summary:** Looks fine.

**Findings ({count})**
{items}
**Verified:** Read the diff.

""" + RUN_MARKER


def review(id, body, state, commit_id, login="github-actions[bot]"):
    return {"id": id, "body": body, "state": state, "commit_id": commit_id,
            "user": {"login": login}}


def good_body(verdict="COMMENT", count=1, items=None, marker=RUN_MARKER):
    if items is None:
        items = "- [ ] `a.py:1` - a problem\n" * count
    text = GOOD_BODY.format(verdict=verdict, count=count, items=items)
    return text.replace(RUN_MARKER, marker)


def skill_blocks():
    """The fenced markdown blocks of the real SKILL.md, keyed A and B."""
    text = SKILL_MD.read_text(encoding="utf-8")
    blocks = []
    inside = None
    for line in text.splitlines():
        if inside is None:
            if line.strip() == "```markdown":
                inside = []
        elif line.strip() == "```":
            blocks.append("\n".join(inside))
            inside = None
        else:
            inside.append(line)
    found = {}
    for block in blocks:
        if block.startswith("**Recommendation:** REQUEST_CHANGES"):
            found.setdefault("A", block)
        elif block.startswith("**Recommendation:** NOT REVIEWED"):
            found.setdefault("B", block)
    return found


class VerifyTests(unittest.TestCase):
    """Fixtures: pr.env and reviews-before.txt are written straight into
    STATE_DIR; the after-list is the stub's reviews-1.json; the current pull
    request is pull-1.json (verify reads each once)."""

    def prepared(self, reviews, before="1\n", head=SHA):
        h = Harness(self)
        h.state.mkdir()
        (h.state / "pr.env").write_text(
            "PR_NUMBER=7\nHEAD_SHA=%s\nBASE_REF=main\n" % SHA, encoding="utf-8")
        if before is not None:
            (h.state / "reviews-before.txt").write_text(before, encoding="utf-8")
        h.write_json("pull-1.json", pull_json(head={"sha": head, "repo": {"full_name": REPO}}))
        h.write_json("reviews-1.json", reviews)
        return h

    def old(self, **kw):
        return review(1, kw.pop("body", "old review"), "COMMENTED", SHA, **kw)

    def assertFails(self, h, phrase, code=1):
        result = h.run("verify")
        self.assertEqual(result.returncode, code, result.stdout)
        self.assertIn(phrase, result.stdout)
        self.assertIn("::error::", result.stdout)
        self.assertFalse((h.state / "verified").exists())

    def assertPasses(self, h):
        result = h.run("verify")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue((h.state / "verified").exists())
        return result

    def test_each_verdict_with_its_state_passes(self):
        for verdict, state in (("APPROVE", "COMMENTED"), ("COMMENT", "COMMENTED"),
                               ("REQUEST_CHANGES", "CHANGES_REQUESTED")):
            h = self.prepared([self.old(), review(2, good_body(verdict), state, SHA)])
            result = self.assertPasses(h)
            self.assertIn(verdict, result.stdout)
            self.assertIn(["api", "--paginate", "repos/%s/pulls/7/reviews" % REPO],
                          h.calls())
            self.assertIn(["api", "repos/%s/pulls/7" % REPO], h.calls())

    def test_paginated_reviews_are_read(self):
        h = self.prepared("[%s]\n[%s]" % (
            json.dumps(self.old()),
            json.dumps(review(2, good_body(), "COMMENTED", SHA))))
        self.assertPasses(h)

    def test_trailing_whitespace_and_blank_lines_are_tolerated(self):
        body = good_body().replace("\n", "  \n") + "\n\n"
        body = body.replace(RUN_MARKER + "  ", RUN_MARKER)
        self.assertPasses(self.prepared([review(2, body, "COMMENTED", SHA)]))

    def test_no_new_review_fails(self):
        self.assertFails(self.prepared([self.old()]), "no review from this run")

    def test_preexisting_review_with_marker_is_not_counted(self):
        old = review(1, good_body(), "COMMENTED", SHA)
        self.assertFails(self.prepared([old]), "no review from this run")

    def test_other_login_without_marker_fails(self):
        body = good_body(marker="<!-- something else -->")
        h = self.prepared([review(2, body, "COMMENTED", SHA, login="someone")])
        self.assertFails(h, "no review from this run")

    def test_marker_with_other_run_attempt_or_head_fails(self):
        for marker in ("<!-- claude-pr-review run=99999-1 head=%s -->" % SHA,
                       "<!-- claude-pr-review run=12345-2 head=%s -->" % SHA,
                       "<!-- claude-pr-review run=12345-1 head=%s -->" % ("b" * 40)):
            h = self.prepared([review(2, good_body(marker=marker), "COMMENTED", SHA)])
            self.assertFails(h, "no review from this run")

    def test_marker_not_on_the_last_line_is_not_counted(self):
        # A body that only quotes this run's marker, mid-body or first, was not
        # signed by this run: the marker counts only as the last non-empty line.
        signed_last = good_body()
        mid = signed_last.replace(RUN_MARKER, "Thanks.").replace(
            "**Verified:**", RUN_MARKER + "\n\n**Verified:**")
        first = RUN_MARKER + "\n\n" + signed_last.replace(RUN_MARKER, "Thanks.")
        for body in (mid, first):
            self.assertIn(RUN_MARKER, body)
            h = self.prepared([review(2, body, "COMMENTED", SHA)])
            self.assertFails(h, "no review from this run")
        self.assertPasses(self.prepared([review(2, signed_last, "COMMENTED", SHA)]))

    def test_unreadable_reviews_response_is_2(self):
        h = self.prepared([])
        h.write_json("reviews-1.json", "not json")
        self.assertFails(h, "could not be read", code=2)

    def test_wrong_commit_id_fails(self):
        h = self.prepared([review(2, good_body(), "COMMENTED", "c" * 40)])
        self.assertFails(h, "review is of another commit")

    def test_head_moved_fails(self):
        h = self.prepared([review(2, good_body(), "COMMENTED", SHA)], head="d" * 40)
        self.assertFails(h, "the pull request moved during the review")

    def test_two_counted_reviews_fail(self):
        h = self.prepared([review(2, good_body(), "COMMENTED", SHA),
                           review(3, good_body(), "COMMENTED", SHA)])
        self.assertFails(h, "more than one review from this run")

    def test_failure_body_with_verdict_line_and_marker_fails_shape(self):
        body = ("**Recommendation:** COMMENT\n\nCould not fetch the diff; no review "
                "performed.\n\n" + RUN_MARKER)
        self.assertFails(self.prepared([review(2, body, "COMMENTED", SHA)]),
                         "review is not in the required shape")

    def test_heading_only_body_fails_shape(self):
        body = "**Recommendation:** COMMENT\n\n" + RUN_MARKER
        self.assertFails(self.prepared([review(2, body, "COMMENTED", SHA)]),
                         "review is not in the required shape")

    def test_findings_count_above_items_fails(self):
        body = good_body(count=2, items="- [ ] `a.py:1` - one\n")
        self.assertFails(self.prepared([review(2, body, "COMMENTED", SHA)]),
                         "review is not in the required shape")

    def test_findings_zero_with_one_item_fails(self):
        body = good_body(count=0, items="- [ ] `a.py:1` - one\n")
        self.assertFails(self.prepared([review(2, body, "COMMENTED", SHA)]),
                         "review is not in the required shape")

    def test_findings_zero_with_no_items_passes(self):
        body = good_body(count=0, items="")
        self.assertPasses(self.prepared([review(2, body, "COMMENTED", SHA)]))

    def test_checkbox_inside_details_after_verified_is_not_counted(self):
        body = good_body().replace(
            RUN_MARKER,
            "<details>\n<summary>Reasoning</summary>\n\n- [ ] extra\n\n</details>\n\n"
            + RUN_MARKER)
        self.assertPasses(self.prepared([review(2, body, "COMMENTED", SHA)]))

    def test_state_mismatch_fails_both_ways(self):
        for verdict, state in (("COMMENT", "CHANGES_REQUESTED"),
                               ("APPROVE", "CHANGES_REQUESTED"),
                               ("REQUEST_CHANGES", "COMMENTED")):
            h = self.prepared([review(2, good_body(verdict), state, SHA)])
            self.assertFails(h, "verdict and review state disagree")

    def test_not_reviewed_alongside_valid_review_fails(self):
        failure = "**Recommendation:** NOT REVIEWED\n\nNo diff.\n\n" + RUN_MARKER
        h = self.prepared([review(2, good_body(), "COMMENTED", SHA),
                           review(3, failure, "COMMENTED", SHA)])
        self.assertFails(h, "the reviewer reported it could not review")

    def test_not_reviewed_without_marker_still_fails_first(self):
        failure = "**Recommendation:** NOT REVIEWED\n\nNo diff."
        h = self.prepared([review(2, failure, "COMMENTED", SHA)])
        self.assertFails(h, "the reviewer reported it could not review")

    def test_review_from_unrelated_login_is_not_counted(self):
        reviews = [review(2, good_body(), "COMMENTED", SHA, login="unrelated-human")]
        self.assertFails(self.prepared(reviews), "no review from this run")
        # Positive control: the same review from the expected login passes.
        self.assertPasses(self.prepared(
            [review(2, good_body(), "COMMENTED", SHA, login="github-actions[bot]")]))

    def test_not_reviewed_from_other_login_is_ignored(self):
        failure = "**Recommendation:** NOT REVIEWED\n\nNo diff.\n\n" + RUN_MARKER
        h = self.prepared([review(2, failure, "COMMENTED", SHA, login="other-bot"),
                           review(3, good_body(), "COMMENTED", SHA)])
        self.assertPasses(h)

    def test_not_reviewed_from_expected_login_still_fails(self):
        failure = "**Recommendation:** NOT REVIEWED\n\nNo diff.\n\n" + RUN_MARKER
        h = self.prepared([review(2, failure, "COMMENTED", SHA,
                                  login="github-actions[bot]"),
                           review(3, good_body(), "COMMENTED", SHA)])
        self.assertFails(h, "the reviewer reported it could not review")

    def test_reviewer_login_unset_or_invalid_is_2(self):
        for value in (None, "", "a b", "bad;login", "x[bot"):
            h = self.prepared([review(2, good_body(), "COMMENTED", SHA)])
            result = h.run("verify", REVIEWER_LOGIN=value)
            self.assertEqual(result.returncode, 2, (value, result.stdout))
            self.assertIn("REVIEWER_LOGIN", result.stdout)
            self.assertFalse((h.state / "verified").exists())

    def test_review_with_null_user_is_not_counted(self):
        orphan = review(2, good_body(), "COMMENTED", SHA)
        orphan["user"] = None
        self.assertFails(self.prepared([orphan]), "no review from this run")

    def test_gh_failure_is_2(self):
        for name in ("fail-reviews", "fail-pull"):
            h = self.prepared([review(2, good_body(), "COMMENTED", SHA)])
            (h.stub / name).write_text("", encoding="utf-8")
            self.assertFails(h, "Could not", code=2)

    def test_missing_reviews_before_is_2(self):
        h = self.prepared([review(2, good_body(), "COMMENTED", SHA)], before=None)
        self.assertFails(h, "reviews-before", code=2)

    def test_missing_pr_env_is_2(self):
        h = self.prepared([])
        (h.state / "pr.env").unlink()
        self.assertFails(h, "pr.env", code=2)

    def test_missing_run_id_is_2(self):
        h = self.prepared([review(2, good_body(), "COMMENTED", SHA)])
        result = h.run("verify", RUN_ID=None)
        self.assertEqual(result.returncode, 2, result.stdout)

    def skill_review(self, key, state):
        block = skill_blocks()[key]
        self.assertIn("run=RUN-ATTEMPT head=HEAD_SHA", block)
        body = block.replace("run=RUN-ATTEMPT head=HEAD_SHA", "run=12345-1 head=%s" % SHA)
        return self.prepared([review(2, body, state, SHA)])

    def test_skill_block_a_verifies(self):
        self.assertPasses(self.skill_review("A", "CHANGES_REQUESTED"))

    def test_skill_block_b_is_not_reviewed(self):
        self.assertFails(self.skill_review("B", "COMMENTED"),
                         "the reviewer reported it could not review")

    def test_skill_never_hands_the_reviewer_the_pr_title_or_body(self):
        text = SKILL_MD.read_text(encoding="utf-8")
        self.assertNotIn("PR title and body", text)
        self.assertIn("gh pr view $ARGUMENTS --json files,commits", text)

    def test_script_never_approves(self):
        for line in SCRIPT.read_text(encoding="utf-8").splitlines():
            if not line.lstrip().startswith("#"):
                self.assertNotIn("--approve", line)


import ast
import re
import shlex


class MiniYamlError(ValueError):
    pass


class MiniYaml:
    """Reads the YAML subset action.yml and workflow-template.yml use, so their
    wiring tests run on the standard library alone: block maps, `- ` lists
    (scalar or map items), `[a, b]` flow lists, quoted and plain scalars, and
    `>-` folded blocks. Plain scalars resolve as YAML 1.1 does (PyYAML), so
    `on` is True and `0` is 0. Anything else raises rather than misreads."""

    BOOLS = {"true": True, "yes": True, "on": True,
             "false": False, "no": False, "off": False}
    KEY = re.compile(r"[A-Za-z_][\w.-]*$")

    def __init__(self, text):
        self.lines = text.splitlines()
        self.pos = 0

    @classmethod
    def load(cls, text):
        reader = cls(text)
        value = reader.node(0)
        if reader.peek() is not None:
            reader.fail("unexpected dedent")
        return value

    def fail(self, why):
        raise MiniYamlError("line %d: %s" % (self.pos + 1, why))

    def peek(self):
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                indent = len(line) - len(line.lstrip(" "))
                if line[indent] == "\t":
                    self.fail("tab indent")
                return indent, stripped
            self.pos += 1
        return None

    def node(self, indent):
        peeked = self.peek()
        if peeked is None or peeked[0] != indent:
            self.fail("expected a block at indent %d" % indent)
        if peeked[1].startswith("-"):
            return self.sequence(indent)
        return self.mapping(indent, {})

    def mapping(self, indent, result):
        while True:
            peeked = self.peek()
            if peeked is None or peeked[0] < indent:
                return result
            if peeked[0] > indent or peeked[1].startswith("-"):
                self.fail("unexpected line in a map")
            self.pos += 1
            self.entry(peeked[1], indent, result)

    def entry(self, text, indent, result):
        key, sep, rest = text.partition(":")
        if not sep or not self.KEY.match(key) or rest[:1] not in ("", " "):
            self.fail("not a `key: value` line")
        key = self.BOOLS.get(key.lower(), key)
        if key in result:
            self.fail("duplicate key %r" % key)
        rest = rest.strip()
        if rest == ">-":
            result[key] = self.folded(indent)
        elif rest:
            result[key] = self.scalar(rest)
        else:
            peeked = self.peek()
            if peeked is None or peeked[0] <= indent:
                self.fail("empty value")
            result[key] = self.node(peeked[0])

    def sequence(self, indent):
        items = []
        while True:
            peeked = self.peek()
            if peeked is None or peeked[0] < indent:
                return items
            text = peeked[1]
            if peeked[0] > indent or not text.startswith("- ") or text[2:3] == " ":
                self.fail("unexpected line in a list")
            self.pos += 1
            body = text[2:]
            key = body.partition(":")[0]
            if self.KEY.match(key) and body[len(key):len(key) + 2] in (":", ": "):
                item = {}
                self.entry(body, indent + 2, item)
                items.append(self.mapping(indent + 2, item))
            else:
                items.append(self.scalar(body))

    def folded(self, indent):
        parts, block_indent = [], None
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            stripped = line.strip()
            here = len(line) - len(line.lstrip(" "))
            if not stripped or here <= indent:
                break
            if block_indent is None:
                block_indent = here
            elif here != block_indent:
                self.fail("uneven indent in a folded block")
            parts.append(stripped)
            self.pos += 1
        if not parts:
            self.fail("empty folded block")
        return " ".join(parts)

    def scalar(self, text):
        if text[0] == "'":
            inner = text[1:-1]
            if len(text) < 2 or text[-1] != "'" or "'" in inner.replace("''", ""):
                self.fail("bad single-quoted scalar")
            return inner.replace("''", "'")
        if text[0] == '"':
            inner = text[1:-1]
            if len(text) < 2 or text[-1] != '"' or '"' in inner or "\\" in inner:
                self.fail("unsupported double-quoted scalar")
            return inner
        if text[0] == "[":
            if text[-1] != "]":
                self.fail("bad flow list")
            return [self.plain(part.strip()) for part in text[1:-1].split(",")]
        return self.plain(text)

    def plain(self, text):
        if not text or text[0] in "[]{}&*!|>'\"%@`#," or " #" in text or ": " in text:
            self.fail("unsupported plain scalar %r" % text)
        if text.lower() in self.BOOLS:
            return self.BOOLS[text.lower()]
        if re.match(r"-?(0|[1-9][0-9]*)$", text):
            return int(text)
        if text[0] in "0123456789+-.~" or text.lower() == "null":
            self.fail("plain scalar %r would not resolve to a string" % text)
        return text

ACTION_YML = REPO_ROOT / "actions" / "claude-pr-review" / "action.yml"
TEMPLATE_YML = REPO_ROOT / "actions" / "claude-pr-review" / "workflow-template.yml"
STATE_ENV = "${{ runner.temp }}/claude-pr-review"
STEP_ORDER = ["validate", "checkout", "base", "prompt", "snapshot",
              "review", "verify", "report", "log"]
RUN_STEP_ENV = {
    "validate": {"PR_NUMBER", "MODEL", "OAUTH_TOKEN", "GH_TOKEN", "GH_REPO",
                 "RUN_ID", "RUN_ATTEMPT", "STATE_DIR"},
    "base": {"WORKSPACE", "STATE_DIR"},
    "prompt": {"ACTION_ROOT", "GH_REPO", "RUN_ID", "RUN_ATTEMPT", "STATE_DIR"},
    "snapshot": {"GH_TOKEN", "GH_REPO", "STATE_DIR"},
    "verify": {"GH_TOKEN", "GH_REPO", "RUN_ID", "RUN_ATTEMPT", "REVIEWER_LOGIN",
               "STATE_DIR"},
    "report": {"PR_NUMBER", "GH_TOKEN", "GH_REPO", "RUN_URL", "STATE_DIR"},
}
ALLOWED_TOOLS = {"Read", "Glob", "Grep", "Bash(gh pr view:*)",
                 "Bash(gh pr diff:*)", "Bash(gh pr review:*)",
                 "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"}


def public_origin_slug():
    """owner/repo from PUBLIC_ORIGIN, read with ast so lib is never imported."""
    tree = ast.parse((REPO_ROOT / "lib" / "toolkit_update.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "PUBLIC_ORIGIN" for t in node.targets):
            value = ast.literal_eval(node.value)
            prefix = "https://github.com/"
            assert value.startswith(prefix), value
            return value[len(prefix):]
    raise AssertionError("PUBLIC_ORIGIN not found")


def non_comment_lines(path):
    return [line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")]


class TemplatePinTests(unittest.TestCase):
    """Runs without PyYAML: reads the `uses:` line with a regex."""

    def test_uses_pins_public_origin_at_version_tag(self):
        version = (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        lines = [line for line in non_comment_lines(TEMPLATE_YML)
                 if re.match(r"\s*(-\s*)?uses:", line)]
        self.assertEqual(len(lines), 1)
        uses = re.match(r"\s*(?:-\s*)?uses:\s*(\S+)\s*$", lines[0]).group(1)
        self.assertEqual(
            uses, "%s/actions/claude-pr-review@v%s" % (public_origin_slug(), version))

    def test_header_says_green_cannot_prove_quality(self):
        # R1: a green check is a well-formed review by this run, not proof of a
        # good one; the user copying this file must be told so in it.
        text = TEMPLATE_YML.read_text(encoding="utf-8")
        header = [line for line in text.splitlines() if line.startswith("#")]
        self.assertIn("cannot prove the review was good", " ".join(
            line.lstrip("# ").strip() for line in header))

    def test_no_vars_or_federation_outside_comments(self):
        body = "\n".join(non_comment_lines(TEMPLATE_YML))
        for word in ("vars.", "id-token", "federation"):
            self.assertNotIn(word, body)

    def test_no_repo_workflow_references_the_action(self):
        workflows = REPO_ROOT / ".github" / "workflows"
        if not workflows.is_dir():
            return
        for path in workflows.rglob("*"):
            if path.is_file():
                self.assertNotIn("claude-pr-review",
                                 path.read_text(encoding="utf-8", errors="replace"),
                                 str(path))


class MiniYamlTests(unittest.TestCase):
    """The reader the wiring tests stand on: what it reads, and that it refuses
    what it does not support instead of misreading it."""

    def test_reads_the_supported_subset(self):
        text = ("on:\n  push:\n    types: [a, b]\n"
                "steps:\n  - id: x\n    run: 'say ''hi'''\n    n: 0\n"
                "  - plain\n"
                "args: >-\n  --one \"two\"\n  --three\n"
                "flag: false\n")
        self.assertEqual(MiniYaml.load(text), {
            True: {"push": {"types": ["a", "b"]}},
            "steps": [{"id": "x", "run": "say 'hi'", "n": 0}, "plain"],
            "args": '--one "two" --three',
            "flag": False})

    def test_refuses_unsupported_syntax(self):
        for text in ("a: &anchor x\n", "a: *anchor\n", "a: |\n  x\n", "a: {b: c}\n",
                     "a:\n\tb: c\n", "a: 1.5\n", "a: 2026-10-27\n", "a: x # note\n",
                     "a: b: c\n", "a: x\na: y\n", "a: >-\n    x\n  y\n", "a: null\n"):
            with self.subTest(text=text):
                with self.assertRaises(MiniYamlError):
                    MiniYaml.load(text)


class ActionWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.action = MiniYaml.load(ACTION_YML.read_text(encoding="utf-8"))
        cls.steps = cls.action["runs"]["steps"]
        cls.by_id = {step["id"]: step for step in cls.steps}
        cls.template = MiniYaml.load(TEMPLATE_YML.read_text(encoding="utf-8"))

    def test_step_ids_in_order(self):
        self.assertEqual([step["id"] for step in self.steps], STEP_ORDER)

    def test_composite_and_inputs(self):
        self.assertEqual(self.action["runs"]["using"], "composite")
        inputs = self.action["inputs"]
        self.assertEqual(set(inputs), {"pr_number", "github_token",
                                       "claude_code_oauth_token", "model",
                                       "reviewer_login"})
        for name in ("pr_number", "github_token", "claude_code_oauth_token"):
            self.assertTrue(inputs[name]["required"])
        self.assertEqual(inputs["model"]["default"], "opus")
        self.assertFalse(inputs["reviewer_login"]["required"])
        self.assertEqual(inputs["reviewer_login"]["default"], "github-actions[bot]")
        self.assertEqual(self.by_id["verify"]["env"]["REVIEWER_LOGIN"],
                         "${{ inputs.reviewer_login }}")

    def test_report_runs_always_and_log_only_on_failure(self):
        self.assertIn("always()", self.by_id["report"]["if"])
        self.assertEqual(self.by_id["log"]["if"].strip(), "failure()")

    def test_run_values_are_script_calls_without_expressions(self):
        for step in self.steps:
            if "run" in step:
                self.assertNotIn("${{", step["run"], step["id"])
                self.assertEqual(
                    step["run"],
                    '"$GITHUB_ACTION_PATH/pr-review.sh" %s' % step["id"])
                self.assertEqual(step["shell"], "bash")

    def test_run_step_env_keys_match_table(self):
        for step_id, keys in RUN_STEP_ENV.items():
            self.assertEqual(set(self.by_id[step_id]["env"]), keys, step_id)
            self.assertEqual(self.by_id[step_id]["env"]["STATE_DIR"], STATE_ENV)

    def test_every_run_step_is_in_the_table(self):
        run_ids = {step["id"] for step in self.steps if "run" in step}
        self.assertEqual(run_ids, set(RUN_STEP_ENV))

    def test_review_step_env_and_prompt(self):
        review = self.by_id["review"]
        self.assertEqual(review["uses"], "anthropics/claude-code-action@v1")
        self.assertEqual(review["with"]["prompt"], "${{ steps.prompt.outputs.text }}")
        self.assertEqual(set(review["with"]),
                         {"prompt", "claude_code_oauth_token", "github_token",
                          "claude_args"})
        self.assertEqual(set(review["env"]), {"GH_TOKEN", "GH_REPO"})

    def test_claude_args_exact(self):
        expression = "${{ inputs.model }}"
        raw = self.by_id["review"]["with"]["claude_args"]
        self.assertEqual(raw.count(expression), 1)
        # The expression holds spaces; swap in one token before shlex.split.
        tokens = shlex.split(raw.replace(expression, "MODEL_EXPR"))
        pairs = {}
        for i in range(0, len(tokens), 2):
            self.assertTrue(tokens[i].startswith("--"), tokens)
            self.assertNotIn(tokens[i], pairs)
            pairs[tokens[i]] = tokens[i + 1]
        self.assertEqual(len(tokens), 8)
        self.assertEqual(set(pairs), {"--model", "--tools", "--allowedTools",
                                      "--disallowedTools"})
        self.assertEqual(pairs["--model"], "MODEL_EXPR")
        self.assertEqual(pairs["--tools"], "Read,Glob,Grep,Bash")
        self.assertEqual(pairs["--disallowedTools"], "mcp__*")
        allowed = pairs["--allowedTools"].split(",")
        self.assertEqual(len(allowed), len(set(allowed)))
        self.assertEqual(set(allowed), ALLOWED_TOOLS)

    def test_checkout_inputs_and_position(self):
        checkout = self.by_id["checkout"]
        self.assertEqual(checkout["uses"], "actions/checkout@v7")
        self.assertNotIn("env", checkout)
        self.assertEqual(checkout["with"], {
            "ref": "refs/pull/${{ inputs.pr_number }}/head",
            "fetch-depth": 0,
            "persist-credentials": False})
        ids = [step["id"] for step in self.steps]
        self.assertLess(ids.index("checkout"), ids.index("base"))
        self.assertEqual(ids.index("validate"), 0)

    def test_log_upload_inputs(self):
        log = self.by_id["log"]
        self.assertEqual(log["uses"], "actions/upload-artifact@v7")
        self.assertEqual(log["with"], {
            "name": "claude-pr-review-log",
            "path": "${{ steps.review.outputs.execution_file }}",
            "if-no-files-found": "warn",
            "retention-days": 7})

    def test_report_env_values(self):
        env = self.by_id["report"]["env"]
        self.assertEqual(env["RUN_URL"],
                         "${{ github.server_url }}/${{ github.repository }}"
                         "/actions/runs/${{ github.run_id }}")

    def test_template_single_step_no_run(self):
        jobs = self.template["jobs"]
        self.assertEqual(list(jobs), ["review"])
        steps = jobs["review"]["steps"]
        self.assertEqual(len(steps), 1)
        self.assertNotIn("run", steps[0])
        self.assertEqual(steps[0]["with"]["model"], "opus")

    def test_template_triggers(self):
        # YAML 1.1 (PyYAML, and MiniYaml) reads the bare key `on` as True.
        triggers = self.template[True]
        self.assertEqual(triggers["pull_request"]["types"],
                         ["opened", "ready_for_review"])
        dispatch = triggers["workflow_dispatch"]["inputs"]["pr_number"]
        self.assertTrue(dispatch["required"])
        self.assertEqual(dispatch["type"], "number")

    def test_template_permissions_and_guard(self):
        self.assertEqual(self.template["permissions"], {
            "contents": "read", "pull-requests": "write",
            "checks": "read", "statuses": "read"})
        job = self.template["jobs"]["review"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["timeout-minutes"], 30)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository",
                      job["if"])
        self.assertIn("github.event.pull_request.draft == false", job["if"])
        self.assertIn("workflow_dispatch", job["if"])


if __name__ == "__main__":
    unittest.main()
