#!/usr/bin/env python3
"""Hermetic run-mode tests; Git is real, GitHub and checks are stubs.

The `gh` shim below is a stubbed API: these tests prove the adapter's model of
GitHub only, never GitHub's own behaviour."""

import contextlib
import functools
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


HERE = Path(os.path.realpath(__file__)).parent.parent
sys.path.insert(0, str(HERE))
import local_ci_lib  # noqa: E402
REPO_ROOT = HERE.parent.parent
TOOL = HERE / "claude-local-ci"
SLUG = "example-owner/fixture"

GH_SHIM = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
args = sys.argv[1:]
with open(os.environ["FIXTURE_CALLS"], "a") as out:
    out.write(json.dumps(args) + "\n")
if args == ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]:
    print("example-owner/fixture")
elif args in (["pr", "view", "--json", "number,headRefOid,baseRefName"],
              ["pr", "view", "7", "--json", "number,headRefOid,baseRefName"]):
    if os.environ.get("FIXTURE_NO_PR"):
        sys.exit(1)
    print(json.dumps({"number": int(os.environ.get("FIXTURE_PR_NUMBER", "7")),
                      "headRefOid": os.environ["FIXTURE_HEAD"],
                      "baseRefName": "main"}))
elif args == ["api", "repos/example-owner/fixture/commits/main", "--jq", ".sha"]:
    print(os.environ["FIXTURE_BASE"])
elif args[:3] == ["api", "-X", "POST"]:
    fields = dict(arg.split("=", 1) for arg in args if "=" in arg)
    if os.environ.get("FIXTURE_FAIL_POST") in ("all", fields.get("state")):
        sys.exit(1)
    print("{}")
elif args == ["api", "user", "--jq", ".login"]:
    if os.environ.get("FIXTURE_NO_LOGIN"):
        sys.exit(1)
    marker = os.environ.get("FIXTURE_SLOW_LOGIN")
    if marker:
        open(marker, "w").close()
        time.sleep(30)
    print("fixture-user")
elif args == ["api", "repos/example-owner/fixture", "--jq", ".permissions.push"]:
    print("false" if os.environ.get("FIXTURE_NO_PUSH") else "true")
else:
    print("unexpected gh arguments: " + repr(args), file=sys.stderr)
    sys.exit(99)
'''


class Fixture:
    def __init__(self, root, default_branch, script, conflict=False, missing=False):
        self.root = Path(root)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # The CLI's shebang is `env python3`; run it on the suite's own interpreter.
        (self.bin / "python3").symlink_to(sys.executable)
        self.calls = self.root / "calls.jsonl"
        self.receipt = self.root / "receipt.json"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith("GIT_") and not key.startswith("FIXTURE_")}
        self.env.update(HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / ".config"),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@localhost",
                        GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@localhost",
                        FIXTURE_CALLS=str(self.calls))
        shim = self.bin / "gh"
        shim.write_text(GH_SHIM)
        shim.chmod(0o755)
        self.origin = self.root / "origin.git"
        self.seed = self.root / "seed"
        self.repo = self.root / "work"
        self.git(self.root, "-c", "init.defaultBranch=" + default_branch,
                 "init", "--bare", str(self.origin))
        self.git(self.origin, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(self.root, "clone", str(self.origin), str(self.seed))
        self.git(self.seed, "symbolic-ref", "HEAD", "refs/heads/main")
        (self.seed / "ci").mkdir()
        (self.seed / "ci/local-checks.sh").write_text(script)
        (self.seed / "shared.txt").write_text("ancestor\n")
        self.git(self.seed, "add", ".")
        initial = self.snapshot("main")
        if missing:
            (self.seed / "ci/local-checks.sh").unlink()
        (self.seed / "feature.txt").write_text("feature\n")
        if conflict:
            (self.seed / "shared.txt").write_text("feature change\n")
        self.git(self.seed, "add", "-A")
        self.head = self.snapshot("feature", initial)
        self.git(self.seed, "read-tree", initial)
        if conflict:
            (self.seed / "shared.txt").write_text("base change\n")
            self.git(self.seed, "add", "shared.txt")
        else:
            (self.seed / "base-only.txt").write_text("live base\n")
            self.git(self.seed, "add", "base-only.txt")
        self.base = self.snapshot("main", initial)
        self.git(self.seed, "push", "origin", "refs/heads/main", "refs/heads/feature")
        self.git(self.origin, "update-ref", "refs/pull/7/head", self.head)
        self.git(self.root, "-c", "init.defaultBranch=" + default_branch,
                 "clone", str(self.origin), str(self.repo))
        actual = self.git(self.repo, "symbolic-ref", "--short", "HEAD")
        if actual != "main":
            raise AssertionError("fixture clone branch is " + actual)
        self.env.update(FIXTURE_HEAD=self.head, FIXTURE_BASE=self.base)

    def git(self, cwd, *args):
        # Never inherit a GIT_* setting from the caller (especially GIT_DIR).
        env = {key: value for key, value in self.env.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                   GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL="fixture@localhost",
                   GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL="fixture@localhost")
        result = subprocess.run(["git"] + list(args), cwd=str(cwd), env=env,
                                input="", text=True, capture_output=True)
        if result.returncode:
            raise AssertionError("git %r: %s" % (args, result.stderr))
        return result.stdout.strip()

    def snapshot(self, branch, parent=None):
        tree = self.git(self.seed, "write-tree")
        args = ["commit-tree", tree, "-m", "fixture"]
        if parent:
            args += ["-p", parent]
        sha = self.git(self.seed, *args)
        self.git(self.seed, "update-ref", "refs/heads/" + branch, sha)
        return sha

    def invoke(self, *args, frozen=False, explicit_repo=True, receipt=None):
        if not TOOL.is_file():
            raise AssertionError("missing executable under test: " + str(TOOL))
        cmd = [str(TOOL)]
        if frozen:
            cmd = [sys.executable, "-c",
                   "import runpy,sys; from unittest.mock import patch; "
                   "p=patch('time.time',return_value=1800000000); p.start(); "
                   "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')",
                   str(TOOL)]
        if explicit_repo:
            cmd += ["--repo", str(self.repo)]
        cmd += ["--receipt", str(self.receipt if receipt is None else receipt)]
        return subprocess.run(cmd + list(args), cwd=str(self.repo), env=self.env,
                              input="", text=True, capture_output=True)

    def read_receipt(self, path=None):
        return json.loads(Path(self.receipt if path is None else path).read_text())

    def calls_made(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def worktrees(self):
        return self.git(self.repo, "worktree", "list", "--porcelain").count("worktree ")

    def posts(self):
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        return [call for call in calls if call[:3] == ["api", "-X", "POST"]]

    def log_files(self):
        return sorted((self.home / ".cache/claude-local-ci/logs").glob("*.log"))


# How long an interrupt test waits for the runner. It must OUTLAST the runner's
# own kill path (CHILD_EXIT_TIMEOUT_S, then cleanup): at exactly 20 s, a
# correct-but-slow run and a stub that ignored TERM both died as a bare
# TimeoutExpired (3.9, PR #271), so neither cause could be told apart.
# Detection does not ride on this number: a stub that had to be KILLed never
# writes its TERM-trap marker, and the marker assertion fails by name.
RUNNER_EXIT_TIMEOUT_S = local_ci_lib.CHILD_EXIT_TIMEOUT_S + 30


def stub(output="3 checks run", rc=0):
    return ("printf 'WORKTREE=%s\\n' \"$PWD\"\n"
            "printf 'BASE_REF=%s\\n' \"$BASE_REF\"\n"
            "printf 'HEAD=%s\\n' \"$(git rev-parse HEAD)\"\n"
            "printf 'TREE=%s\\n' \"$(git rev-parse 'HEAD^{tree}')\"\n"
            "printf 'stderr line\\n' >&2\n"
            "printf '%s\\n' '" + output + "'\nexit " + str(rc) + "\n")


def both_defaults(method):
    @functools.wraps(method)
    def wrapped(self):
        for branch in ("main", "trunk"):
            with self.subTest(default_branch=branch):
                self.default_branch = branch
                method(self)
    return wrapped


class TestRunMode(unittest.TestCase):
    def fixtures(self, script=None, **kwargs):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            yield Fixture(root, self.default_branch, stub() if script is None else script, **kwargs)

    # The receipt's final state for each process exit (design C4's outcome table).
    RECEIPT_STATE = {0: "success", 1: "failure", 2: "error"}

    def assert_result(self, fixture, result, rc, states, receipt_state=None):
        self.assertEqual(result.returncode, rc, result.stdout + result.stderr)
        receipt = fixture.read_receipt()
        self.assertEqual(receipt["state"], receipt_state or self.RECEIPT_STATE[rc], receipt)
        self.assertEqual(receipt["head"], fixture.head)
        self.assertEqual(receipt["base"], fixture.base)
        self.assertIsNotNone(receipt["finished_at"])
        posts = fixture.posts()
        fields = [dict(arg.split("=", 1) for arg in post if "=" in arg) for post in posts]
        self.assertEqual([field["state"] for field in fields], states)
        for post, field in zip(posts, fields):
            self.assertEqual(post[3], "repos/" + SLUG + "/statuses/" + fixture.head)
            self.assertEqual(field["context"], "local/tests")
            self.assertIn("base=" + fixture.base, field["description"])
            self.assertLessEqual(len(field["description"]), 140)
        entries = fixture.git(fixture.repo, "worktree", "list", "--porcelain")
        self.assertEqual(entries.count("worktree "), 1, entries)
        scratch = fixture.repo / ".claude/worktrees"
        self.assertEqual(list(scratch.glob("local-ci-*")), [])
        return fields

    @both_defaults
    def test_passing_stub_streams_and_logs_last_count(self):
        for f in self.fixtures(stub("2 checks run\n3 checks run")):
            result = f.invoke("--pr", "7", "--post", explicit_repo=False)
            fields = self.assert_result(f, result, 0, ["pending", "success"])
            self.assertEqual(fields[0]["description"], "running · base=" + f.base)
            self.assertRegex(fields[-1]["description"],
                             r"^3 checks pass · base=[0-9a-f]{40} · \d+m\d{2}s$")
            self.assertIn("BASE_REF=" + f.base, result.stdout)
            self.assertIn("stderr line\n", result.stdout)
            logs = f.log_files()
            self.assertEqual(len(logs), 1)
            self.assertRegex(logs[0].name, r"^example-owner_fixture-" + f.head[:8]
                             + r"-\d+-\d+\.log$")
            self.assertEqual(logs[0].read_text(), result.stdout)
            self.assertIn("claude-local-ci: full log: " + str(logs[0]) + "\n", result.stderr)

    @both_defaults
    def test_failing_run_names_its_log(self):
        # A failing suite's traceback can sit far above the tail; the run must
        # say where the full output was kept.
        for f in self.fixtures(stub(rc=1)):
            result = f.invoke("--pr", "7", "--post")
            self.assert_result(f, result, 1, ["pending", "failure"])
            logs = f.log_files()
            self.assertEqual(len(logs), 1)
            self.assertIn("claude-local-ci: full log: " + str(logs[0]) + "\n", result.stderr)

    @both_defaults
    def test_runner_scrubs_the_nesting_guard_from_the_checks_environment(self):
        # A stray CLAUDE_LOCAL_CI_NESTED=1 in the caller's shell must never reach
        # ci/local-checks.sh, or a real run would skip its self-test and post green.
        script = 'printf \'%s\\n\' "${CLAUDE_LOCAL_CI_NESTED:-unset}" > "$MARK"\n' + stub()
        for f in self.fixtures(script):
            marker = f.root / "nested-marker"
            f.env["MARK"] = str(marker)
            f.env["CLAUDE_LOCAL_CI_NESTED"] = "1"
            result = f.invoke("--pr", "7", "--post")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(marker.read_text().strip(), "unset")

    @both_defaults
    def test_failed_checks(self):
        for f in self.fixtures(stub(rc=1)):
            fields = self.assert_result(f, f.invoke("--pr", "7", "--post"), 1, ["pending", "failure"])
            self.assertRegex(fields[-1]["description"],
                             r"^3 checks fail · base=[0-9a-f]{40} · \d+m\d{2}s$")

    @both_defaults
    def test_interrupt_terminates_checks_before_removing_worktree(self):
        script = ('trap \'echo terminated > "$MARK"; exit 143\' TERM\n'
                  'echo ready\n'
                  'sleep 60 & wait\n')
        for f in self.fixtures(script):
            marker = f.root / "terminated"
            f.env["MARK"] = str(marker)
            output_path = f.root / "runner-output"
            with output_path.open("w") as output:
                process = subprocess.Popen(
                    [str(TOOL), "--repo", str(f.repo), "--pr", "7", "--post",
                     "--receipt", str(f.receipt)], cwd=str(f.repo), env=f.env,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        # Ignore an incomplete final line while the shim appends.
                        calls = ([json.loads(line) for line in
                                  f.calls.read_text().split("\n")[:-1]]
                                 if f.calls.exists() else [])
                        pending = any(call[:3] == ["api", "-X", "POST"]
                                      and "state=pending" in call for call in calls)
                        if pending and "ready" in output_path.read_text().splitlines():
                            break
                        self.assertIsNone(process.poll(), output_path.read_text())
                        time.sleep(0.05)
                    else:
                        self.fail("runner did not post pending and stream ready within 10s")

                    os.kill(process.pid, signal.SIGINT)
                    rc = process.wait(timeout=RUNNER_EXIT_TIMEOUT_S)
                    result = subprocess.CompletedProcess(
                        process.args, rc, output_path.read_text(), "")
                    fields = self.assert_result(f, result, 2, ["pending", "error"])
                    self.assertEqual(fields[-1]["description"],
                                     "runner interrupted · base=" + f.base)
                    self.assertTrue(marker.exists(), "TERM trap marker is absent")
                    self.assertEqual(marker.read_text().strip(), "terminated")
                finally:
                    # Reap the stub's sleep even when the regression assertion fails.
                    # Group signalling is teardown only; the interrupt targets the runner.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

    def test_child_exit_timeout_exceeds_script_grace(self):
        # The runner must outwait ci/local-checks.sh's cleanup grace, or it
        # SIGKILLs the shell before the script KILLs its own check groups.
        text = (REPO_ROOT / "ci/local-checks.sh").read_text()
        polls = re.search(r"^CLEANUP_GRACE_POLLS=(\d+)", text, re.M)
        self.assertIsNotNone(polls, "CLEANUP_GRACE_POLLS not found")
        loop = re.search(r"for \(\( waited = 1; waited <= CLEANUP_GRACE_POLLS; waited\+\+ \)\); do.*?\n  done", text, re.S)
        self.assertIsNotNone(loop, "grace poll loop not found")
        step = re.search(r"\bsleep ([0-9.]+)", loop.group(0))
        self.assertIsNotNone(step, "poll step not found")
        self.assertEqual(float(step.group(1)), 0.2)
        grace = int(polls.group(1)) * float(step.group(1))
        self.assertGreaterEqual(local_ci_lib.CHILD_EXIT_TIMEOUT_S, grace + 5)

    def test_interrupt_leaves_no_sigterm_ignoring_suite_running(self):
        self.default_branch = "main"
        grace = int(re.search(r"^CLEANUP_GRACE_POLLS=(\d+)",
                              (REPO_ROOT / "ci/local-checks.sh").read_text(), re.M).group(1)) * 0.2
        script = ('set -m\n'
                  '(trap \'\' TERM; while :; do date +%s >> "$BEAT"; sleep 0.2; done) &\n'
                  'PID=$!\n'
                  'cleanup() {\n'
                  '  kill -TERM -- "-$PID" 2>/dev/null\n'
                  '  sleep "$GRACE"\n'
                  '  kill -KILL -- "-$PID" 2>/dev/null\n'
                  '}\n'
                  'trap cleanup EXIT INT TERM\n'
                  'echo ready\n'
                  'wait\n')
        for f in self.fixtures(script):
            beat = f.root / "heartbeat"
            f.env["BEAT"] = str(beat)
            f.env["GRACE"] = str(grace)
            output_path = f.root / "runner-output"
            with output_path.open("w") as output:
                process = subprocess.Popen(
                    [str(TOOL), "--repo", str(f.repo), "--pr", "7", "--post",
                     "--receipt", str(f.receipt)], cwd=str(f.repo), env=f.env,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
                try:
                    deadline = time.monotonic() + 10
                    while not (beat.exists() and beat.stat().st_size > 0):
                        self.assertIsNone(process.poll(), output_path.read_text())
                        self.assertLess(time.monotonic(), deadline, "no heartbeat within 10s")
                        time.sleep(0.05)
                    # Positive control: the heartbeat is growing before the interrupt.
                    before = beat.stat().st_size
                    time.sleep(1)
                    self.assertGreater(beat.stat().st_size, before)

                    os.kill(process.pid, signal.SIGINT)
                    process.wait(timeout=40)
                    settled = beat.stat().st_size
                    time.sleep(2)
                    self.assertEqual(beat.stat().st_size, settled,
                                     "SIGTERM-ignoring check survived the runner")
                finally:
                    # Group signalling is teardown only; reap survivors of the stub.
                    subprocess.run(["pkill", "-KILL", "-f", str(beat)])
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

    @both_defaults
    def test_sigterm_terminates_checks_before_removing_worktree(self):
        # Fix 3: SIGTERM's default disposition kills the process outright,
        # skipping run_ci's cleanup entirely — the checks child survives,
        # the worktree stays registered, and the status stays "pending". The
        # CLI must route SIGTERM through the same KeyboardInterrupt path as
        # Ctrl-C. Mirrors test_interrupt_terminates_checks_before_removing_
        # worktree above, signalling the runner process ONLY (no preexec_fn
        # reset — unlike SIGINT, a non-interactive shell does not start
        # background jobs with SIGTERM ignored).
        script = ('trap \'echo terminated > "$MARK"; exit 143\' TERM\n'
                  'echo ready\n'
                  'sleep 60 & wait\n')
        for f in self.fixtures(script):
            marker = f.root / "terminated"
            f.env["MARK"] = str(marker)
            output_path = f.root / "runner-output"
            with output_path.open("w") as output:
                process = subprocess.Popen(
                    [str(TOOL), "--repo", str(f.repo), "--pr", "7", "--post",
                     "--receipt", str(f.receipt)], cwd=str(f.repo), env=f.env,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True)
                try:
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        # Ignore an incomplete final line while the shim appends.
                        calls = ([json.loads(line) for line in
                                  f.calls.read_text().split("\n")[:-1]]
                                 if f.calls.exists() else [])
                        pending = any(call[:3] == ["api", "-X", "POST"]
                                      and "state=pending" in call for call in calls)
                        if pending and "ready" in output_path.read_text().splitlines():
                            break
                        self.assertIsNone(process.poll(), output_path.read_text())
                        time.sleep(0.05)
                    else:
                        self.fail("runner did not post pending and stream ready within 10s")

                    os.kill(process.pid, signal.SIGTERM)
                    rc = process.wait(timeout=RUNNER_EXIT_TIMEOUT_S)
                    result = subprocess.CompletedProcess(
                        process.args, rc, output_path.read_text(), "")
                    fields = self.assert_result(f, result, 2, ["pending", "error"])
                    self.assertEqual(fields[-1]["description"],
                                     "runner interrupted · base=" + f.base)
                    self.assertTrue(marker.exists(), "TERM trap marker is absent")
                    self.assertEqual(marker.read_text().strip(), "terminated")
                finally:
                    # Reap the stub's sleep even when the regression assertion fails.
                    # Group signalling is teardown only; the signal targets the runner.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

    @both_defaults
    def test_no_count_is_error(self):
        for f in self.fixtures(stub("not a count")):
            fields = self.assert_result(f, f.invoke("--pr", "7", "--post"), 2, ["pending", "error"])
            self.assertEqual(fields[-1]["description"], "no check count reported · base=" + f.base)

    @both_defaults
    def test_zero_count_is_error(self):
        # Fix 2: "0 checks run" matches the count regex and the stub exits 0
        # — a below-1 count must be treated exactly like a missing one, not
        # posted as success.
        for f in self.fixtures(stub("0 checks run", rc=0)):
            fields = self.assert_result(f, f.invoke("--pr", "7", "--post"), 2, ["pending", "error"])
            self.assertEqual(fields[-1]["description"], "no check count reported · base=" + f.base)

    @both_defaults
    def test_environment_last_line_is_error_not_failure(self):
        # Rows 11/13 (codex-gate-models): an interrupted or disk-full run is
        # not a test result. With and without a count line before it.
        env = "local-checks: environment: disk full (No space left on device)"
        for output, rc in ((env, 143), ("6 checks run\n" + env, 2)):
            with self.subTest(output=output):
                for f in self.fixtures(stub(output, rc=rc)):
                    fields = self.assert_result(f, f.invoke("--pr", "7", "--post"), 2, ["pending", "error"])
                    self.assertEqual(
                        fields[-1]["description"],
                        "environment: disk full (No space left on device) · base=" + f.base)

    @both_defaults
    def test_environment_reason_is_cut_to_fit_the_description(self):
        for f in self.fixtures(stub("local-checks: environment: " + "x" * 200, rc=2)):
            fields = self.assert_result(f, f.invoke("--pr", "7", "--post"), 2, ["pending", "error"])
            self.assertTrue(fields[-1]["description"].startswith("environment: xxx"))
            self.assertTrue(fields[-1]["description"].endswith(" · base=" + f.base))

    @both_defaults
    def test_environment_text_mid_stream_stays_a_failure(self):
        # A failing suite's diagnostics can carry the same text; only the
        # script's own LAST line counts.
        for f in self.fixtures(stub(
                "local-checks: environment: interrupted by SIGTERM\n6 checks run", rc=1)):
            fields = self.assert_result(f, f.invoke("--pr", "7", "--post"), 1, ["pending", "failure"])
            self.assertRegex(fields[-1]["description"], r"^6 checks fail · base=")

    @both_defaults
    def test_environment_line_cannot_override_a_zero_exit(self):
        for f in self.fixtures(stub(
                "6 checks run\nlocal-checks: environment: interrupted by SIGTERM", rc=0)):
            fields = self.assert_result(f, f.invoke("--pr", "7", "--post"), 0, ["pending", "success"])
            self.assertRegex(fields[-1]["description"], r"^6 checks pass · base=")

    @both_defaults
    def test_missing_script(self):
        for f in self.fixtures(missing=True):
            fields = self.assert_result(f, f.invoke("--pr", "7", "--post"), 2, ["pending", "error"])
            self.assertEqual(fields[-1]["description"], "no ci/local-checks.sh · base=" + f.base)

    @both_defaults
    def test_merge_conflict(self):
        for f in self.fixtures(conflict=True):
            fields = self.assert_result(f, f.invoke("--pr", "7", "--post"), 1, ["pending", "failure"])
            self.assertEqual(fields[-1]["description"], "merge conflict with base · base=" + f.base)

    @both_defaults
    def test_no_post_mirrors_checks(self):
        for rc in (0, 1):
            with self.subTest(stub_rc=rc):
                for f in self.fixtures(stub(rc=rc)):
                    self.assert_result(f, f.invoke("--pr", "7", "--no-post"), rc, [])

    @both_defaults
    def test_no_pr(self):
        for f in self.fixtures():
            f.env["FIXTURE_NO_PR"] = "1"
            result = f.invoke("--pr", "7")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(f.posts(), [])
            self.assertEqual(f.worktrees(), 1)
            self.assertFalse(f.receipt.exists(), "a failed resolution must not write a receipt")
            self.assertIn("no PR found", result.stderr)

    @both_defaults
    def test_failed_post(self):
        for state in ("all", "success"):
            with self.subTest(failed_state=state):
                for f in self.fixtures():
                    f.env["FIXTURE_FAIL_POST"] = state
                    result = f.invoke("--pr", "7", "--post")
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertTrue(f.posts())
                    # A failed final POST leaves the local outcome in the receipt; a
                    # failed pending POST stops the run before any outcome exists.
                    self.assertEqual(f.read_receipt()["state"],
                                     "success" if state == "success" else "error")
                    self.assertEqual(f.git(f.repo, "worktree", "list", "--porcelain").count("worktree "), 1)
                    self.assertEqual(list((f.repo / ".claude/worktrees").glob("local-ci-*")), [])

    @both_defaults
    def test_posts_target_pr_tip_not_merge(self):
        for f in self.fixtures():
            result = f.invoke("--pr", "7", "--post")
            self.assert_result(f, result, 0, ["pending", "success"])
            merged = next(line[5:] for line in result.stdout.splitlines() if line.startswith("HEAD="))
            self.assertNotEqual(merged, f.head)
            self.assertNotEqual(merged, f.base)
            self.assertTrue(all(post[3].endswith("/" + f.head) for post in f.posts()))

    @both_defaults
    def test_two_runs_preserve_existing_worktree_path(self):
        for f in self.fixtures():
            first = f.invoke("--pr", "7", "--post", frozen=True)
            self.assert_result(f, first, 0, ["pending", "success"])
            first_path = Path(next(line[9:] for line in first.stdout.splitlines()
                                   if line.startswith("WORKTREE=")))
            first_path.mkdir()
            sentinel = first_path / "belongs-to-another-run"
            sentinel.write_text("preserve me")
            second = f.invoke("--pr", "7", "--post", frozen=True)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            second_path = Path(next(line[9:] for line in second.stdout.splitlines()
                                    if line.startswith("WORKTREE=")))
            self.assertNotEqual(first_path, second_path)
            self.assertEqual(sentinel.read_text(), "preserve me")
            self.assertFalse(second_path.exists())
            self.assertEqual(len(f.log_files()), 2)
            self.assertEqual(f.git(f.repo, "worktree", "list", "--porcelain").count("worktree "), 1)


HOLDER = ("import fcntl, sys, time\n"
          "handle = open(sys.argv[1], 'a')\n"
          "fcntl.flock(handle, fcntl.LOCK_EX)\n"
          "print('held', flush=True)\n"
          "time.sleep(120)\n")


@unittest.skipIf(local_ci_lib.fcntl is None, "no flock on this platform: no cap by design")
class TestCheckSlots(unittest.TestCase):
    """The machine-wide cap on concurrent check runs (row 4, 2026-10-05)."""

    def hold(self, path):
        """Hold `path`'s flock from ANOTHER process, the way a peer run would."""
        holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)],
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    def test_a_free_slot_is_taken_without_waiting(self):
        with tempfile.TemporaryDirectory() as root:
            started = time.monotonic()
            handle = local_ci_lib.acquire_slot(2, Path(root), poll=30)
            self.addCleanup(handle.close)
            self.assertLess(time.monotonic() - started, 5)

    def test_a_full_machine_waits_until_a_holder_dies(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            root.mkdir(exist_ok=True)
            first = self.hold(root / "slot-0.lock")
            self.hold(root / "slot-1.lock")
            got = []
            worker = threading.Thread(
                target=lambda: got.append(local_ci_lib.acquire_slot(2, root, poll=0.05)),
                daemon=True)
            worker.start()
            worker.join(1.0)
            self.assertTrue(worker.is_alive(), "took a slot while both were held")
            first.kill()          # killed, never unlocked: the kernel must release it
            first.wait()
            worker.join(10)
            self.assertFalse(worker.is_alive(), "never took the slot its holder died with")
            self.addCleanup(got[0].close)

    def test_max_concurrent_falls_back_to_the_default(self):
        for value in (None, 0, -1, "3", True, 2.5):
            with self.subTest(value=value), \
                    mock.patch.object(local_ci_lib, "_config") as config:
                config.get.return_value = value
                self.assertEqual(local_ci_lib.max_concurrent(),
                                 local_ci_lib.DEFAULT_MAX_CONCURRENT)

    def test_max_concurrent_reads_the_config(self):
        with mock.patch.object(local_ci_lib, "_config") as config:
            config.get.return_value = 3
            self.assertEqual(local_ci_lib.max_concurrent(), 3)
            config.get.assert_called_with("local_ci", "max_concurrent")

    def test_the_runner_waits_for_a_slot_before_running_checks(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            fixture = Fixture(root, "main", stub())
            slots = fixture.home / ".cache/claude-local-ci/slots"
            slots.mkdir(parents=True)
            first = self.hold(slots / "slot-0.lock")
            self.hold(slots / "slot-1.lock")
            process = subprocess.Popen(
                [str(TOOL), "--repo", str(fixture.repo), "--head", fixture.head,
                 "--base", fixture.base, "--receipt", str(fixture.root / "slots-receipt.json")],
                cwd=str(fixture.repo), env=fixture.env, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
            before = []
            for line in process.stdout:     # EOF ends this if no wait is announced
                before.append(line)
                if "slots on this machine are busy" in line:
                    break
            self.assertIn("busy", "".join(before), "never announced a wait")
            self.assertNotIn("WORKTREE=", "".join(before), "checks ran before a slot freed")
            first.kill()
            rest, _ = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, "".join(before) + rest)
            self.assertIn("WORKTREE=", rest)

    def test_the_slot_is_held_while_the_checks_run(self):
        # The checks script itself probes slot-0 (the one a free machine hands
        # out): a runner that let go of it before streaming would report "free".
        probe = ("python3 -c \"import fcntl, os, sys\n"
                 "h = open(os.path.expanduser('~/.cache/claude-local-ci/slots/slot-0.lock'), 'a')\n"
                 "try:\n    fcntl.flock(h, fcntl.LOCK_EX | fcntl.LOCK_NB); print('SLOT0=free')\n"
                 "except OSError:\n    print('SLOT0=held')\"\n"
                 "echo '1 checks run'\n")
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            fixture = Fixture(root, "main", probe)
            result = fixture.invoke("--head", fixture.head, "--base", fixture.base,
                                    receipt=fixture.root / "slots-receipt.json")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("SLOT0=held", result.stdout)


class TestInterpreter(unittest.TestCase):
    def test_cli_runs_on_the_suites_interpreter(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            result = subprocess.run(
                ["python3", "-c", "import sys; print(tuple(sys.version_info[:2]))"],
                env=f.env, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), str(tuple(sys.version_info[:2])), result.stderr)


class TestLocalMode(unittest.TestCase):
    """`--head SHA --base SHA`: same run, no network, no gh, no posting."""

    def fixtures(self, script=None, **kwargs):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            yield Fixture(root, self.default_branch, stub() if script is None else script, **kwargs)

    def run_local(self, f, **kwargs):
        return f.invoke("--head", f.head, "--base", f.base, **kwargs)

    def assert_finished(self, f, result, rc, state):
        """Shared outcome checks: exit code, final receipt shape, no gh, no worktree."""
        self.assertEqual(result.returncode, rc, result.stdout + result.stderr)
        receipt = f.read_receipt()
        self.assertEqual(receipt["state"], state, receipt)
        self.assertEqual(receipt["schema"], 1)
        self.assertEqual(receipt["tool"], "claude-local-ci")
        self.assertEqual(receipt["target"], {"kind": "local", "pr": None, "slug": None})
        self.assertEqual((receipt["head"], receipt["base"]), (f.head, f.base))
        self.assertEqual(receipt["repo"],
                         os.path.realpath(os.path.join(str(f.repo), ".git")))
        self.assertEqual(receipt["runner_digest"], local_ci_lib.runner_digest())
        self.assertRegex(receipt["started_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertRegex(receipt["finished_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(f.calls_made(), [], "a local run must never call gh")
        self.assertEqual(f.worktrees(), 1)
        self.assertEqual(list((f.repo / ".claude/worktrees").glob("local-ci-*")), [])
        return receipt

    @both_defaults
    def test_pass(self):
        for f in self.fixtures(stub("2 checks run\n3 checks run")):
            result = self.run_local(f)
            receipt = self.assert_finished(f, result, 0, "success")
            self.assertEqual((receipt["checks"], receipt["exit"], receipt["reason"]), (3, 0, ""))
            tree = next(line[5:] for line in result.stdout.splitlines()
                        if line.startswith("TREE="))
            self.assertEqual(receipt["merge_tree"], tree)
            logs = f.log_files()
            self.assertEqual(len(logs), 1)
            self.assertRegex(logs[0].name, r"^local-" + f.head[:8] + r"-\d+-\d+\.log$")
            self.assertEqual(receipt["log"], str(logs[0]))

    @both_defaults
    def test_fail(self):
        for f in self.fixtures(stub(rc=1)):
            receipt = self.assert_finished(f, self.run_local(f), 1, "failure")
            self.assertEqual((receipt["checks"], receipt["exit"]), (3, 1))
            self.assertIsNotNone(receipt["merge_tree"])

    @both_defaults
    def test_conflict(self):
        for f in self.fixtures(conflict=True):
            receipt = self.assert_finished(f, self.run_local(f), 1, "failure")
            self.assertEqual(receipt["reason"], "merge conflict with base")
            self.assertIsNone(receipt["checks"])
            self.assertIsNone(receipt["exit"])
            self.assertIsNone(receipt["merge_tree"])

    @both_defaults
    def test_missing_script(self):
        for f in self.fixtures(missing=True):
            receipt = self.assert_finished(f, self.run_local(f), 2, "error")
            self.assertEqual(receipt["reason"], "no ci/local-checks.sh")
            self.assertIsNone(receipt["checks"])
            self.assertIsNone(receipt["exit"])

    @both_defaults
    def test_no_count(self):
        for f in self.fixtures(stub("not a count")):
            receipt = self.assert_finished(f, self.run_local(f), 2, "error")
            self.assertEqual(receipt["reason"], "no check count reported")
            self.assertIsNone(receipt["checks"])
            self.assertEqual(receipt["exit"], 0)

    @both_defaults
    def test_environment_line(self):
        env = "local-checks: environment: disk full (No space left on device)"
        for f in self.fixtures(stub("6 checks run\n" + env, rc=2)):
            receipt = self.assert_finished(f, self.run_local(f), 2, "error")
            self.assertEqual(receipt["reason"], "environment: disk full (No space left on device)")
            self.assertEqual((receipt["checks"], receipt["exit"]), (6, 2))

    @both_defaults
    def test_interrupt_is_an_error_receipt_and_removes_the_worktree(self):
        script = ('trap \'echo terminated > "$MARK"; exit 143\' TERM\n'
                  'echo ready\n'
                  'sleep 60 & wait\n')
        for f in self.fixtures(script):
            marker = f.root / "terminated"
            f.env["MARK"] = str(marker)
            output_path = f.root / "runner-output"
            with output_path.open("w") as output:
                process = subprocess.Popen(
                    [str(TOOL), "--repo", str(f.repo), "--head", f.head, "--base", f.base,
                     "--receipt", str(f.receipt)], cwd=str(f.repo), env=f.env,
                    stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    start_new_session=True,
                    preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
                try:
                    deadline = time.monotonic() + 10
                    while "ready" not in output_path.read_text().splitlines():
                        self.assertIsNone(process.poll(), output_path.read_text())
                        self.assertLess(time.monotonic(), deadline, "checks never started")
                        time.sleep(0.05)
                    os.kill(process.pid, signal.SIGINT)
                    rc = process.wait(timeout=RUNNER_EXIT_TIMEOUT_S)
                    result = subprocess.CompletedProcess(
                        process.args, rc, output_path.read_text(), "")
                    receipt = self.assert_finished(f, result, 2, "error")
                    self.assertEqual(receipt["reason"], "runner interrupted")
                    self.assertTrue(marker.exists(), "TERM trap marker is absent")
                finally:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)

    def test_runs_with_no_gh_on_the_path(self):
        self.default_branch = "main"
        for f in self.fixtures():
            # The shim dir also holds the python3 link the CLI's shebang needs, so
            # it is replaced by a dir with only that link.
            only_python = f.root / "only-python"
            only_python.mkdir()
            (only_python / "python3").symlink_to(sys.executable)
            kept = [d for d in os.environ["PATH"].split(os.pathsep)
                    if d and not os.access(os.path.join(d, "gh"), os.X_OK)]
            f.env["PATH"] = os.pathsep.join([str(only_python)] + kept)
            self.assertIsNone(shutil.which("gh", path=f.env["PATH"]), "gh is still reachable")
            self.assertIsNotNone(shutil.which("git", path=f.env["PATH"]), "git was dropped too")
            result = self.run_local(f)
            self.assert_finished(f, result, 0, "success")


class TestArguments(unittest.TestCase):
    """Every refusal exits 2 before any git or gh call and never touches the receipt."""

    SEED = b"seeded receipt, must survive a refusal\n"

    def refuse(self, build_args, reason, lock_untouched=True):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            f.receipt.write_bytes(self.SEED)
            result = f.invoke(*build_args(f))
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertRegex(result.stderr, r"^claude-local-ci: .*(?:" + reason + ")")
            self.assertEqual(f.calls_made(), [], "a refusal must make no gh call")
            self.assertEqual(f.worktrees(), 1)
            self.assertFalse((f.repo / ".claude/worktrees").exists()
                             and list((f.repo / ".claude/worktrees").glob("local-ci-*")))
            self.assertEqual(f.receipt.read_bytes(), self.SEED)
            if lock_untouched:
                self.assertFalse(Path(str(f.receipt) + ".lock").exists(),
                                 "a usage error must not take the receipt's lock")
            self.assertEqual(f.log_files(), [])

    def test_abbreviated_sha(self):
        self.refuse(lambda f: ["--head", f.head[:12], "--base", f.base], "40|64|hex|full")

    def test_ref_name(self):
        self.refuse(lambda f: ["--head", "main", "--base", f.base], "40|64|hex|full")

    def test_uppercase_sha(self):
        self.refuse(lambda f: ["--head", f.head.upper(), "--base", f.base], "40|64|hex|full")

    def test_missing_commit(self):
        # Resolution happens after the run owns the receipt, so the lock file may
        # exist; the receipt's bytes must still be exactly the seed.
        self.refuse(lambda f: ["--head", "1" * 40, "--base", f.base],
                    "commit not found: " + "1" * 40, lock_untouched=False)

    def test_head_alone(self):
        self.refuse(lambda f: ["--head", f.head], "--head|--base|target")

    def test_base_alone(self):
        self.refuse(lambda f: ["--base", f.base], "--head|--base|target")

    def test_no_target(self):
        self.refuse(lambda f: [], "target|--pr|--head")

    def test_both_targets(self):
        self.refuse(lambda f: ["--head", f.head, "--base", f.base, "--pr", "7"],
                    "target|--pr|one")

    def test_post_without_pr(self):
        self.refuse(lambda f: ["--head", f.head, "--base", f.base, "--post"], "--post")

    def test_post_with_no_post(self):
        self.refuse(lambda f: ["--pr", "7", "--post", "--no-post"], "--post|--no-post")

    def test_post_with_verify(self):
        self.refuse(lambda f: ["--verify", "--pr", "7", "--post"], "--post|--verify")

    def test_receipt_is_required(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            result = subprocess.run(
                [str(TOOL), "--repo", str(f.repo), "--head", f.head, "--base", f.base],
                cwd=str(f.repo), env=f.env, input="", text=True, capture_output=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(f.calls_made(), [])


class TestReceiptOwnership(unittest.TestCase):
    """One owner per receipt path; an earlier result never outlives a newer run."""

    def commit_child(self, f, script):
        """A child of f.head whose ci/local-checks.sh is `script`, made in a scratch worktree."""
        scratch = f.root / "child-worktree"
        f.git(f.repo, "worktree", "add", "--detach", str(scratch), f.head)
        (scratch / "ci/local-checks.sh").write_text(script)
        f.git(scratch, "add", "ci/local-checks.sh")
        f.git(scratch, "commit", "-m", "child")
        child = f.git(scratch, "rev-parse", "HEAD")
        f.git(f.repo, "worktree", "remove", "--force", str(scratch))
        return child

    def test_the_second_run_replaces_the_first_at_one_path(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            first = f.invoke("--head", f.head, "--base", f.base)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(f.read_receipt()["state"], "success")
            child = self.commit_child(f, stub(rc=1))
            second = f.invoke("--head", child, "--base", f.base)
            self.assertEqual(second.returncode, 1, second.stdout + second.stderr)
            receipt = f.read_receipt()
            self.assertEqual((receipt["state"], receipt["head"]), ("failure", child))
            self.assertEqual(f.worktrees(), 1)

    def test_a_run_killed_before_the_merge_never_leaves_the_old_success(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            first = f.invoke("--pr", "7")
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(f.read_receipt()["state"], "success")
            started = f.root / "login-started"
            f.env["FIXTURE_SLOW_LOGIN"] = str(started)
            process = subprocess.Popen(
                [str(TOOL), "--repo", str(f.repo), "--pr", "7", "--post",
                 "--receipt", str(f.receipt)], cwd=str(f.repo), env=f.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            try:
                deadline = time.monotonic() + 15
                while not started.exists():
                    self.assertIsNone(process.poll(), "runner exited before the preflight")
                    self.assertLess(time.monotonic(), deadline, "preflight never started")
                    time.sleep(0.05)
                os.kill(process.pid, signal.SIGKILL)
                process.wait(timeout=10)
            finally:
                # Reap the shim's 30 s sleep; the kill above targeted the runner only.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=10)
            receipt = f.read_receipt()
            self.assertEqual(receipt["state"], "running", receipt)
            self.assertEqual(receipt["head"], f.head)
            self.assertIsNone(receipt["merge_tree"])
            self.assertEqual(f.worktrees(), 1)
            self.assertEqual(list((f.repo / ".claude/worktrees").glob("local-ci-*")), [])

    @unittest.skipIf(local_ci_lib.fcntl is None, "no flock on this platform")
    def test_a_held_lock_refuses_and_touches_nothing(self):
        import fcntl
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            seed = b"seeded receipt\n"
            f.receipt.write_bytes(seed)
            with open(str(f.receipt) + ".lock", "a") as holder:
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                result = f.invoke("--head", f.head, "--base", f.base)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("receipt in use", result.stderr)
            self.assertEqual(f.receipt.read_bytes(), seed)
            self.assertEqual(f.worktrees(), 1)
            self.assertEqual(f.log_files(), [])

    def preflight_refused(self, variable):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            f.env[variable] = "1"
            result = f.invoke("--pr", "7", "--post")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertEqual(f.posts(), [])
            self.assertEqual(f.worktrees(), 1)
            self.assertFalse((f.repo / ".claude/worktrees").exists()
                             and list((f.repo / ".claude/worktrees").glob("local-ci-*")))
            receipt = f.read_receipt()
            self.assertEqual(receipt["state"], "error", receipt)
            self.assertTrue(receipt["reason"].startswith("post preflight"), receipt)
            self.assertIsNotNone(receipt["finished_at"])

    def test_preflight_without_a_login_is_refused(self):
        self.preflight_refused("FIXTURE_NO_LOGIN")

    def test_preflight_without_push_access_is_refused(self):
        self.preflight_refused("FIXTURE_NO_PUSH")


class TestShellAndMergeEnvironment(unittest.TestCase):
    @both_defaults
    def test_bash_startup_cannot_stand_in_for_the_checks(self):
        # BASH_ENV, ENV and exported functions run before ci/local-checks.sh's
        # first line; a failing script must still fail with all three set.
        for f in self.fixtures(stub(rc=1)):
            startup = f.root / "startup.sh"
            startup.write_text("echo '6 checks run'\nexit 0\n")
            f.env.update(BASH_ENV=str(startup), ENV=str(startup),
                         **{"BASH_FUNC_printf%%": "() { echo '6 checks run'; exit 0; }"})
            result = f.invoke("--head", f.head, "--base", f.base)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertEqual(f.read_receipt()["state"], "failure")

    @both_defaults
    def test_a_signing_setup_does_not_fail_the_merge(self):
        for f in self.fixtures():
            f.git(f.repo, "config", "commit.gpgsign", "true")
            f.git(f.repo, "config", "gpg.program", str(f.root / "no-such-signer"))
            result = f.invoke("--head", f.head, "--base", f.base)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(f.read_receipt()["state"], "success")

    @both_defaults
    def test_a_merge_that_fails_without_a_conflict_is_an_error(self):
        for f in self.fixtures():
            hook = f.repo / ".git" / "hooks" / "pre-merge-commit"
            hook.parent.mkdir(exist_ok=True)
            hook.write_text("#!/bin/sh\necho 'hook refused the merge' >&2\nexit 1\n")
            hook.chmod(0o755)
            result = f.invoke("--head", f.head, "--base", f.base)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            receipt = f.read_receipt()
            self.assertEqual(receipt["state"], "error")
            self.assertTrue(receipt["reason"].startswith("merge failed: "), receipt)
            self.assertIn("merge failed: ", result.stderr)

    def fixtures(self, script=None, **kwargs):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            yield Fixture(root, self.default_branch, stub() if script is None else script, **kwargs)


class TestCleanupFailure(unittest.TestCase):
    def test_an_interrupt_during_worktree_removal_leaves_an_error_receipt(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            original = local_ci_lib.checked

            def checked(cmd, cwd):
                if cmd[:4] == ["git", "worktree", "remove", "--force"]:
                    raise KeyboardInterrupt()
                return original(cmd, cwd)

            try:
                with mock.patch.object(local_ci_lib, "checked", side_effect=checked), \
                        mock.patch.dict(os.environ, f.env, clear=True), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(KeyboardInterrupt):
                        local_ci_lib.run_ci(f.repo, ("local", f.head, f.base), f.receipt)
            finally:
                while local_ci_lib._HELD_LOCKS:
                    local_ci_lib._HELD_LOCKS.pop().close()
            receipt = f.read_receipt()
            self.assertEqual(receipt["state"], "error")
            self.assertEqual(receipt["reason"], "worktree removal failed: runner interrupted")
            verify = f.invoke("--verify", "--head", f.head, "--base", f.base)
            self.assertEqual(verify.returncode, 1, verify.stdout + verify.stderr)
            f.git(f.repo, "worktree", "prune")

    def test_a_failed_worktree_removal_leaves_an_error_receipt(self):
        # In-process so only `git worktree remove` can be made to fail.
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            original = local_ci_lib.checked

            def checked(cmd, cwd):
                if cmd[:4] == ["git", "worktree", "remove", "--force"]:
                    raise local_ci_lib.RunnerError("simulated removal failure")
                return original(cmd, cwd)

            try:
                with mock.patch.object(local_ci_lib, "checked", side_effect=checked), \
                        mock.patch.dict(os.environ, f.env, clear=True), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(local_ci_lib.RunnerError):
                        local_ci_lib.run_ci(f.repo, ("local", f.head, f.base), f.receipt)
            finally:
                # run_ci holds its receipt lock for the process's life; release it here.
                while local_ci_lib._HELD_LOCKS:
                    local_ci_lib._HELD_LOCKS.pop().close()
            receipt = f.read_receipt()
            self.assertEqual(receipt["state"], "error")
            self.assertTrue(receipt["reason"].startswith("worktree removal failed"), receipt)
            verify = f.invoke("--verify", "--head", f.head, "--base", f.base)
            self.assertEqual(verify.returncode, 1, verify.stdout + verify.stderr)
            # Leave no registered worktree behind for the TemporaryDirectory cleanup.
            f.git(f.repo, "worktree", "prune")


class TestVerifyMode(unittest.TestCase):
    """`--verify`: a receipt is read and compared, never produced. Every case below
    moves exactly one input from a passing control; the rest still hold."""

    def local_run(self, f):
        result = f.invoke("--head", f.head, "--base", f.base)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def pr_run(self, f):
        result = f.invoke("--pr", "7")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def verify_local(self, f, head=None, base=None, **kwargs):
        return f.invoke("--verify", "--head", head or f.head, "--base", base or f.base,
                        **kwargs)

    def verify_pr(self, f, number="7", **kwargs):
        return f.invoke("--verify", "--pr", number, **kwargs)

    def edit_receipt(self, f, **changes):
        receipt = f.read_receipt()
        receipt.update(changes)
        f.receipt.write_text(json.dumps(receipt))

    def assert_fails(self, result, name, rc=1):
        self.assertEqual(result.returncode, rc, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(len(lines), 1, result.stdout)
        self.assertTrue(lines[0].startswith("verify: %s: " % name), lines[0])

    def assert_ok(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout, "verify: ok\n")

    def test_local_control_passes(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.local_run(f)
            self.assert_ok(self.verify_local(f))

    def test_pr_control_passes(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.pr_run(f)
            self.assert_ok(self.verify_pr(f))

    def test_repo_is_another_checkout(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root, \
                tempfile.TemporaryDirectory(prefix="local-ci-test-") as other_root:
            f = Fixture(root, "main", stub())
            other = Fixture(other_root, "main", stub())
            self.local_run(f)
            copy = f.root / "copy.json"
            receipt = f.read_receipt()
            receipt["repo"] = os.path.realpath(os.path.join(str(other.repo), ".git"))
            copy.write_text(json.dumps(receipt))
            self.assert_fails(self.verify_local(f, receipt=copy), "repo")

    def test_target_local_receipt_verified_as_pr(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.local_run(f)
            self.assert_fails(self.verify_pr(f), "target")

    def test_target_pr_number_differs(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.pr_run(f)
            receipt = f.read_receipt()
            receipt["target"]["pr"] = 8
            f.receipt.write_text(json.dumps(receipt))
            self.assert_fails(self.verify_pr(f), "target")

    def test_target_pr_receipt_verified_as_local(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.pr_run(f)
            self.assert_fails(self.verify_local(f), "target")

    def test_gh_naming_another_pr_is_refused(self):
        # The gh shim is a stubbed API: this proves the adapter's model only.
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.pr_run(f)
            f.env["FIXTURE_PR_NUMBER"] = "8"
            before = f.receipt.read_bytes()
            for result in (self.verify_pr(f), f.invoke("--pr", "7")):
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("gh returned PR 8, not the requested PR 7", result.stderr)
            self.assertEqual(f.receipt.read_bytes(), before)

    def test_merge_conflicting_now_fails(self):
        # A receipt claiming success for a pair that conflicts now (another git,
        # changed attributes) must not verify, whatever merge_tree it carries.
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub(), conflict=True)
            result = f.invoke("--head", f.head, "--base", f.base)
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.edit_receipt(f, state="success", exit=0, checks=3, reason="",
                              merge_tree="0" * 40)
            self.assert_fails(self.verify_local(f), "merge_tree")

    def test_head_is_another_commit(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.local_run(f)
            parent = f.git(f.repo, "rev-parse", f.head + "^")
            self.assert_fails(self.verify_local(f, head=parent), "head")

    def test_base_is_another_commit(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.local_run(f)
            parent = f.git(f.repo, "rev-parse", f.base + "^")
            self.assert_fails(self.verify_local(f, base=parent), "base")

    def state_case(self, **changes):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.local_run(f)
            self.edit_receipt(f, **changes)
            self.assert_fails(self.verify_local(f), "state")

    def test_state_running(self):
        self.state_case(state="running")

    def test_state_failure(self):
        self.state_case(state="failure")

    def test_state_error(self):
        self.state_case(state="error")

    def test_exit_nonzero_with_success(self):
        self.state_case(exit=1)

    def test_zero_checks(self):
        self.state_case(checks=0)

    def test_finished_at_null(self):
        self.state_case(finished_at=None)

    def test_merge_tree_differs(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.local_run(f)
            self.edit_receipt(f, merge_tree=f.git(f.repo, "rev-parse", f.head + "^{tree}"))
            self.assert_fails(self.verify_local(f), "merge_tree")

    def test_runner_digest_differs(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.local_run(f)
            self.edit_receipt(f, runner_digest="sha256:" + "0" * 64)
            self.assert_fails(self.verify_local(f), "runner_digest")

    def test_changing_either_runner_file_invalidates(self):
        for name in ("claude-local-ci", "local_ci_lib.py"):
            with self.subTest(changed=name), \
                    tempfile.TemporaryDirectory(prefix="local-ci-test-") as root, \
                    tempfile.TemporaryDirectory(prefix="local-ci-copy-") as copy_root:
                f = Fixture(root, "main", stub())
                tool_dir = Path(copy_root) / "local-ci"
                shutil.copytree(str(HERE), str(tool_dir),
                                ignore=shutil.ignore_patterns("tests", "__pycache__"))

                def invoke_copy(*args):
                    return subprocess.run(
                        [str(tool_dir / "claude-local-ci"), "--repo", str(f.repo)]
                        + list(args) + ["--receipt", str(f.receipt)],
                        cwd=str(f.repo), env=f.env, input="", text=True,
                        capture_output=True)

                produced = invoke_copy("--head", f.head, "--base", f.base)
                self.assertEqual(produced.returncode, 0, produced.stdout + produced.stderr)
                intact = invoke_copy("--verify", "--head", f.head, "--base", f.base)
                self.assert_ok(intact)
                with open(str(tool_dir / name), "a", encoding="utf-8") as handle:
                    handle.write("# changed\n")
                changed = invoke_copy("--verify", "--head", f.head, "--base", f.base)
                self.assert_fails(changed, "runner_digest")

    def test_pr_head_moved(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.pr_run(f)
            f.env["FIXTURE_HEAD"] = f.git(f.repo, "rev-parse", f.head + "^")
            self.assert_fails(self.verify_pr(f), "head")

    def test_pr_commit_missing_locally_refuses(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.pr_run(f)
            f.env["FIXTURE_HEAD"] = "1" * 40
            result = self.verify_pr(f)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("claude-local-ci --pr 7 first", result.stderr)

    def test_merge_order_is_head_first(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            children = {}
            for side, parent in (("head", f.head), ("base", f.base)):
                scratch = f.root / ("scratch-" + side)
                f.git(f.repo, "worktree", "add", "--detach", str(scratch), parent)
                (scratch / ".gitattributes").write_text("shared.txt merge=union\n")
                (scratch / "shared.txt").write_text("ancestor\n%s line\n" % side)
                f.git(scratch, "add", ".gitattributes", "shared.txt")
                f.git(scratch, "commit", "-m", side)
                children[side] = f.git(scratch, "rev-parse", "HEAD")
                f.git(f.repo, "worktree", "remove", "--force", str(scratch))
            head, base = children["head"], children["base"]
            # The checkout under test does NOT carry the attribute: only the two
            # commits do. Plain `git merge-tree` reads attributes from the checkout
            # and conflicts here; verify must take them from the head (--attr-source).
            self.assertFalse((f.repo / ".gitattributes").exists())
            result = f.invoke("--head", head, "--base", base)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            receipt = f.read_receipt()
            reversed_tree = f.git(f.repo, "--attr-source=" + head, "merge-tree",
                                  "--write-tree", base, head)
            self.assertNotEqual(reversed_tree, receipt["merge_tree"],
                                "fixture is not order-sensitive")
            self.assert_ok(self.verify_local(f, head=head, base=base))

    def test_unusable_receipts_refuse(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.local_run(f)
            good = f.read_receipt()
            cases = {"missing": None, "not json": "{", "not an object": "[]",
                     "schema 2": json.dumps(dict(good, schema=2)),
                     "head an int": json.dumps(dict(good, head=7))}
            for label, text in cases.items():
                with self.subTest(case=label):
                    path = f.root / "bad.json"
                    if path.exists():
                        path.unlink()
                    if text is not None:
                        path.write_text(text)
                    result = self.verify_local(f, receipt=path)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn("claude-local-ci: unusable receipt: ", result.stderr)
                    self.assertEqual(result.stdout, "")

    def test_verify_changes_nothing_and_posts_nothing(self):
        with tempfile.TemporaryDirectory(prefix="local-ci-test-") as root:
            f = Fixture(root, "main", stub())
            self.pr_run(f)
            before = (f.receipt.read_bytes(), f.git(f.repo, "for-each-ref"),
                      f.git(f.repo, "worktree", "list", "--porcelain"))
            calls_before = len(f.calls_made())
            self.assert_ok(self.verify_pr(f))
            f.env["FIXTURE_HEAD"] = f.head
            self.assert_ok(f.invoke("--verify", "--pr", "7"))
            after = (f.receipt.read_bytes(), f.git(f.repo, "for-each-ref"),
                     f.git(f.repo, "worktree", "list", "--porcelain"))
            self.assertEqual(before, after)
            new_calls = f.calls_made()[calls_before:]
            self.assertTrue(new_calls)
            self.assertEqual([c for c in new_calls if c[:3] == ["api", "-X", "POST"]], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
