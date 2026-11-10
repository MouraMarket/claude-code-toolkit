"""Target resolution, receipts, optional status publication, and foreground local check execution."""

import collections
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

try:
    import fcntl
except ImportError:  # Windows: no flock, so no cap (see acquire_slot)
    fcntl = None

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "lib"))
try:
    import toolkit_config as _config
except Exception:  # pragma: no cover - config is optional by design
    _config = None


# Machine-wide cap on concurrent check runs (all repos share the CPU and RAM).
# More concurrent runs give diminishing throughput and can produce a false red
# under load. Override per machine with config key local_ci.max_concurrent.
DEFAULT_MAX_CONCURRENT = 2
SLOT_POLL_S = 5

# Must exceed ci/local-checks.sh's CLEANUP_GRACE_POLLS x 0.2 s plus margin, so
# the script's own SIGKILL of its check groups runs before we kill the shell.
CHILD_EXIT_TIMEOUT_S = 20

# ci/local-checks.sh ends on this line when the run could not produce a
# verdict (interrupted, disk full, no run directory). Posted as `error`.
ENVIRONMENT_RE = re.compile(r"local-checks: environment: (.+)")

# A full object name: 40 hex (SHA-1) or 64 (SHA-256), lowercase. Nothing is
# abbreviated or resolved from a ref name.
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# What a run is about. `pr`, `slug` and `base_name` are None for a local target.
Target = collections.namedtuple("Target", "kind head base pr slug base_name")

# Receipt locks stay open until the process exits (see lock_receipt).
_HELD_LOCKS = []


class RunnerError(Exception):
    """A precondition, command, or publication failed."""


def run(cmd, cwd):
    """Single subprocess boundary for every git and gh invocation."""
    return subprocess.run(cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace")


def checked(cmd, cwd):
    result = run(cmd, cwd)
    if result.returncode:
        raise RunnerError("%s failed: %s" % (" ".join(cmd), result.stderr.strip()))
    return result.stdout.strip()


def post_status(repo, slug, head, state, description):
    assert len(description) <= 140, "status description exceeds 140 characters"
    checked(["gh", "api", "-X", "POST", "repos/%s/statuses/%s" % (slug, head),
             "-f", "state=" + state, "-f", "context=local/tests",
             "-f", "description=" + description], repo)


def max_concurrent():
    value = _config.get("local_ci", "max_concurrent") if _config else None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
        return value
    return DEFAULT_MAX_CONCURRENT


def acquire_slot(slots, slot_dir, poll=SLOT_POLL_S):
    """Block until one of `slots` lock files is ours; return its open handle.

    Holding the handle IS holding the slot. flock is released by the kernel
    when the holder exits, however it exits, so a killed run never leaves a
    stale slot behind. Without fcntl (Windows) there is no cap: returns None.
    """
    if fcntl is None:
        return None
    slot_dir.mkdir(parents=True, exist_ok=True)
    announced = False
    while True:
        for index in range(slots):
            handle = open(slot_dir / ("slot-%d.lock" % index), "a", encoding="utf-8")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except OSError:
                handle.close()
        if not announced:
            print("claude-local-ci: all %d check slots on this machine are busy, "
                  "waiting (config: local_ci.max_concurrent)" % slots, flush=True)
            announced = True
        time.sleep(poll)


def stream_checks(worktree, base_sha, log_path):
    """Tee each line without a shell pipeline; preserve the child's exit code."""
    env = os.environ.copy()
    env["BASE_REF"] = base_sha
    # A stray export in the caller's shell must not make a real run skip the
    # script's self-test (the guard is for the script's own nested fixtures).
    env.pop("CLAUDE_LOCAL_CI_NESTED", None)
    # bash runs BASH_ENV (and sh-mode ENV) and imports exported functions BEFORE
    # the script's first line, so any of them could print a count and exit 0
    # without one check running. None of them belongs to the checks.
    for name in list(env):
        if name in ("BASH_ENV", "ENV") or name.startswith("BASH_FUNC_"):
            del env[name]
    count = None
    last = ""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("x", encoding="utf-8") as log:
        with subprocess.Popen(["bash", "ci/local-checks.sh"], cwd=str(worktree), env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace") as process:
            try:
                for line in process.stdout:
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    log.write(line)
                    log.flush()
                    match = re.fullmatch(r"(\d+) checks run", line.rstrip("\n"))
                    if match:
                        count = match.group(1)
                    if line.strip():
                        last = line.rstrip("\n")
                rc = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=CHILD_EXIT_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
    return rc, count, last


def environment_reason(rc, last):
    """The script's own "the environment broke this run" verdict, or None.

    Only from the LAST line and only on a non-zero exit: a failing suite's
    diagnostics can contain the same text mid-stream, and this must never be
    able to turn anything into a success, only a failure into an error."""
    match = ENVIRONMENT_RE.fullmatch(last)
    if rc == 0 or not match:
        return None
    return match.group(1)


def resolve_repo(repo):
    if repo is None:
        repo = checked(["git", "rev-parse", "--show-toplevel"], os.getcwd())
    return Path(repo).resolve()


def resolve_local(repo, head, base):
    """Both SHAs must name commits already in `repo`. No network, no gh."""
    for sha in (head, base):
        if run(["git", "cat-file", "-e", sha + "^{commit}"], repo).returncode:
            raise RunnerError("commit not found: " + sha)
    return Target("local", head, base, None, None, None)


def resolve_pr(repo, pr):
    """Run-mode steps 1-2: resolve the slug, PR metadata and the base tip."""
    slug = checked(["gh", "repo", "view", "--json", "nameWithOwner",
                    "-q", ".nameWithOwner"], repo)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", slug):
        raise RunnerError("invalid repository name from gh")
    result = run(["gh", "pr", "view", str(pr), "--json", "number,headRefOid,baseRefName"], repo)
    if result.returncode:
        raise RunnerError("no PR found")
    info = json.loads(result.stdout)
    head = info["headRefOid"]
    base_name = info["baseRefName"]
    number = info["number"]
    if (not isinstance(head, str) or not FULL_SHA_RE.fullmatch(head)
            or not isinstance(base_name, str) or not base_name or base_name.startswith("-")
            or not isinstance(number, int) or number < 1):
        raise RunnerError("invalid PR metadata from gh")
    if number != pr:
        raise RunnerError("gh returned PR %d, not the requested PR %d" % (number, pr))
    base = checked(["gh", "api", "repos/%s/commits/%s" % (slug, base_name),
                    "--jq", ".sha"], repo)
    if not FULL_SHA_RE.fullmatch(base):
        raise RunnerError("invalid base SHA from gh")
    return Target("pr", head, base, number, slug, base_name)


def runner_digest():
    """SHA-256 over the running tool's two files, each prefixed by its name, so an
    upgraded tool invalidates older receipts."""
    here = os.path.dirname(os.path.realpath(__file__))
    digest = hashlib.sha256()
    for name in ("claude-local-ci", "local_ci_lib.py"):
        with open(os.path.join(here, name), "rb") as handle:
            digest.update(name.encode() + b"\0" + handle.read() + b"\0")
    return "sha256:" + digest.hexdigest()


def lock_receipt(path):
    """Take the receipt's single-owner lock; the kernel drops it however we exit.

    Without fcntl (Windows) there is no lock: returns None."""
    path = str(path)
    directory = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(directory):
        raise RunnerError("receipt directory does not exist: " + directory)
    if fcntl is None:
        return None
    handle = open(path + ".lock", "a", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RunnerError("receipt in use by another run: " + path)
    return handle


def write_receipt(path, data):
    """Atomic: a reader sees the old receipt or the new one, never a torn file."""
    path = os.path.abspath(str(path))
    temporary = os.path.join(os.path.dirname(path),
                             ".%s.tmp-%d" % (os.path.basename(path), os.getpid()))
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def common_dir(repo):
    common = checked(["git", "rev-parse", "--git-common-dir"], repo)
    return os.path.realpath(os.path.join(str(repo), common))


def receipt_base(repo, target):
    return {
        "schema": 1,
        "tool": "claude-local-ci",
        "repo": common_dir(repo),
        "target": {"kind": target.kind, "pr": target.pr, "slug": target.slug},
        "head": target.head,
        "base": target.base,
        "merge_tree": None,
        "runner_digest": runner_digest(),
        "state": "running",
        "reason": "",
        "checks": None,
        "exit": None,
        "started_at": utc_now(),
        "finished_at": None,
        "log": None,
    }


def preflight_post(repo, slug):
    """Commit statuses need a login and push access; refuse before any check runs."""
    if not checked(["gh", "api", "user", "--jq", ".login"], repo):
        raise RunnerError("gh reports no login")
    push = checked(["gh", "api", "repos/%s" % slug, "--jq", ".permissions.push"], repo)
    if push != "true":
        raise RunnerError("no push access to " + slug)


def run_ci(repo, target, receipt_path, post=False):
    """`target` is ("local", head, base) or ("pr", number). Statuses are posted
    only when `post` is true (PR targets only)."""
    repo = resolve_repo(repo)
    lock = lock_receipt(receipt_path)
    if lock is not None:
        _HELD_LOCKS.append(lock)
    if target[0] == "local":
        resolved = resolve_local(repo, target[1], target[2])
    else:
        resolved = resolve_pr(repo, target[1])
    head, base_sha, base_name = resolved.head, resolved.base, resolved.base_name
    slug = resolved.slug
    receipt = receipt_base(repo, resolved)
    # No earlier result may survive at this path once the target is known.
    write_receipt(receipt_path, receipt)
    finished = False

    def finish(state, reason="", checks=None, exit_code=None):
        nonlocal finished
        receipt.update(state=state, reason=reason, checks=checks, exit=exit_code,
                       finished_at=utc_now())
        write_receipt(receipt_path, receipt)
        finished = True

    run_id = "%s-%d-%d" % (head[:8], os.getpid(), int(time.time()))
    worktree = repo / ".claude/worktrees" / ("local-ci-" + run_id)
    log_name = (slug.replace("/", "_") if slug else "local") + "-" + run_id + ".log"
    log_path = Path.home() / ".cache/claude-local-ci/logs" / log_name
    created = False
    posting_started = False
    started = time.monotonic()

    def post_state(state, description):
        if post:
            post_status(repo, slug, head, state, description)

    try:
        try:
            if post:
                try:
                    preflight_post(repo, slug)
                except RunnerError as exc:
                    finish("error", "post preflight: " + str(exc))
                    print("claude-local-ci: post preflight: " + str(exc), file=sys.stderr)
                    return 2
            if resolved.kind == "pr":
                checked(["git", "fetch", "origin", "pull/%d/head" % resolved.pr,
                         base_name], repo)
                for sha in (head, base_sha):
                    checked(["git", "cat-file", "-e", sha + "^{commit}"], repo)
            # Git accepts an existing empty directory; we must never adopt one.
            if os.path.lexists(worktree):
                raise RunnerError("worktree path already exists: " + str(worktree))
            checked(["git", "worktree", "add", "--detach", str(worktree), head], repo)
            created = True
            actual = checked(["git", "-C", str(worktree), "rev-parse", "HEAD"], repo)
            if actual != head:
                raise RunnerError("worktree HEAD does not match PR tip")
            posting_started = True
            post_state("pending", "running · base=" + base_sha)
            # The worktree is at the head and merges the base: head is "ours".
            # A disposable commit: never signed, so a signing setup cannot fail it.
            merged = run(["git", "-C", str(worktree), "-c", "user.name=local-ci",
                          "-c", "user.email=local-ci@localhost", "-c", "commit.gpgsign=false",
                          "merge", "--no-edit", base_sha], repo)
            if merged.returncode:
                unmerged = run(["git", "-C", str(worktree), "diff", "--name-only",
                                "--diff-filter=U"], repo).stdout.strip()
                run(["git", "-C", str(worktree), "merge", "--abort"], repo)
                if not unmerged:
                    # Not a conflict: git itself failed (a hook, a lock, the disk).
                    detail = (merged.stderr.strip() or merged.stdout.strip()).splitlines()
                    reason = "merge failed: " + (detail[-1] if detail else "git merge exited %d"
                                                 % merged.returncode)
                    finish("error", reason)
                    post_state("error", reason[:140 - len(" · base=" + base_sha)]
                               + " · base=" + base_sha)
                    print("claude-local-ci: " + reason, file=sys.stderr)
                    return 2
                finish("failure", "merge conflict with base")
                post_state("failure", "merge conflict with base · base=" + base_sha)
                return 1
            receipt["merge_tree"] = checked(
                ["git", "-C", str(worktree), "rev-parse", "HEAD^{tree}"], repo)
            write_receipt(receipt_path, receipt)
            if not (worktree / "ci/local-checks.sh").is_file():
                finish("error", "no ci/local-checks.sh")
                post_state("error", "no ci/local-checks.sh · base=" + base_sha)
                return 2
            waiting = time.monotonic()
            slot = acquire_slot(max_concurrent(),
                                Path.home() / ".cache/claude-local-ci/slots")
            started += time.monotonic() - waiting   # report check time, not queue time
            try:
                rc, count, last = stream_checks(worktree, base_sha, log_path)
            finally:
                if slot is not None:
                    slot.close()
                # Every failing suite's full output is in this log; a reader
                # who only sees the tail must be told where it is.
                if log_path.exists():
                    receipt["log"] = str(log_path)
                    print("claude-local-ci: full log: " + str(log_path),
                          file=sys.stderr, flush=True)
            reason = environment_reason(rc, last)
            if reason is not None:
                finish("error", "environment: " + reason,
                       int(count) if count else None, rc)
                # GitHub caps a status description at 140 characters.
                suffix = " · base=" + base_sha
                post_state("error", ("environment: " + reason)[:140 - len(suffix)] + suffix)
                return 2
            if count is None or int(count) < 1:
                finish("error", "no check count reported", None, rc)
                post_state("error", "no check count reported · base=" + base_sha)
                return 2
            finish("success" if rc == 0 else "failure", "", int(count), rc)
            minutes, seconds = divmod(int(time.monotonic() - started), 60)
            description = "%s checks %s · base=%s · %dm%02ds" % (
                count, "pass" if rc == 0 else "fail", base_sha, minutes, seconds)
            post_state("success" if rc == 0 else "failure", description)
            return 0 if rc == 0 else 1
        finally:
            if created:
                try:
                    checked(["git", "worktree", "remove", "--force", str(worktree)], repo)
                except (Exception, KeyboardInterrupt) as exc:
                    # Overrides an outcome already recorded: the run exits 2 here,
                    # and a receipt left at `success` would still verify. An
                    # interrupt during removal counts the same as a failure.
                    finish("error", "worktree removal failed: "
                           + (str(exc) or "runner interrupted"),
                           receipt["checks"], receipt["exit"])
                    raise
    except (Exception, KeyboardInterrupt) as exc:
        if not finished:
            try:
                finish("error", (str(exc) if isinstance(exc, Exception) else "")
                       or "runner interrupted", receipt["checks"], receipt["exit"])
            except (Exception, KeyboardInterrupt) as receipt_exc:
                print("claude-local-ci: could not write receipt: " + str(receipt_exc),
                      file=sys.stderr)
        if posting_started and post:
            try:
                post_state("error", "runner interrupted · base=" + base_sha)
            except (Exception, KeyboardInterrupt) as post_exc:
                print("claude-local-ci: could not post interruption: " + str(post_exc),
                      file=sys.stderr)
        raise


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# Design C4's receipt fields: name -> (accepts, description). None is allowed
# where the design says null.
RECEIPT_FIELDS = {
    "tool": (lambda v: isinstance(v, str), "a string"),
    "repo": (lambda v: isinstance(v, str), "a string"),
    "head": (lambda v: isinstance(v, str), "a string"),
    "base": (lambda v: isinstance(v, str), "a string"),
    "merge_tree": (lambda v: v is None or isinstance(v, str), "a string or null"),
    "runner_digest": (lambda v: isinstance(v, str), "a string"),
    "state": (lambda v: isinstance(v, str), "a string"),
    "reason": (lambda v: isinstance(v, str), "a string"),
    "checks": (lambda v: v is None or _is_int(v), "an integer or null"),
    "exit": (lambda v: v is None or _is_int(v), "an integer or null"),
    "started_at": (lambda v: isinstance(v, str), "a string"),
    "finished_at": (lambda v: v is None or isinstance(v, str), "a string or null"),
    "log": (lambda v: v is None or isinstance(v, str), "a string or null"),
}


def read_receipt(path):
    """The receipt at `path`, or RunnerError (exit 2) if it cannot be relied on."""
    try:
        with open(str(path), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise RunnerError("unusable receipt: %s" % exc)
    if not isinstance(data, dict):
        raise RunnerError("unusable receipt: not a JSON object")
    if not _is_int(data.get("schema")) or data["schema"] != 1:
        raise RunnerError("unusable receipt: schema is not 1")
    for name, (accepts, wanted) in RECEIPT_FIELDS.items():
        if name not in data:
            raise RunnerError("unusable receipt: %s is missing" % name)
        if not accepts(data[name]):
            raise RunnerError("unusable receipt: %s is not %s" % (name, wanted))
    target = data.get("target")
    if not isinstance(target, dict):
        raise RunnerError("unusable receipt: target is not an object")
    for name, accepts, wanted in (
            ("kind", lambda v: isinstance(v, str), "a string"),
            ("pr", lambda v: v is None or _is_int(v), "an integer or null"),
            ("slug", lambda v: v is None or isinstance(v, str), "a string or null")):
        if name not in target:
            raise RunnerError("unusable receipt: target.%s is missing" % name)
        if not accepts(target[name]):
            raise RunnerError("unusable receipt: target.%s is not %s" % (name, wanted))
    return data


def run_verify(repo, target_args, receipt_path):
    """`target_args` is ("local", head, base) or ("pr", number). Compares the receipt
    with what is true now; takes no lock, writes no file, posts nothing. Returns
    0 (verify: ok), 1 (the first mismatch, one stdout line), or raises RunnerError."""
    repo = resolve_repo(repo)
    receipt = read_receipt(receipt_path)
    if target_args[0] == "local":
        expected = resolve_local(repo, target_args[1], target_args[2])
    else:
        expected = resolve_pr(repo, target_args[1])
        for sha in (expected.head, expected.base):
            if run(["git", "cat-file", "-e", sha + "^{commit}"], repo).returncode:
                raise RunnerError("commit not found: %s; run claude-local-ci --pr %d first"
                                  % (sha, expected.pr))

    def mismatch(name, got, now):
        print("verify: %s: receipt %s, now %s" % (name, got, now))
        return 1

    if receipt["repo"] != common_dir(repo):
        return mismatch("repo", receipt["repo"], common_dir(repo))
    target = receipt["target"]
    if expected.kind == "pr":
        if target["kind"] != "pr" or target["pr"] != expected.pr:
            return mismatch("target", "%s %s" % (target["kind"], target["pr"]),
                            "pr %d" % expected.pr)
    elif target["kind"] != "local":
        return mismatch("target", "%s %s" % (target["kind"], target["pr"]), "local")
    if receipt["head"] != expected.head:
        return mismatch("head", receipt["head"], expected.head)
    if receipt["base"] != expected.base:
        return mismatch("base", receipt["base"], expected.base)
    if receipt["state"] != "success":
        print("verify: state: %s" % receipt["state"])
        return 1
    for field, holds in (("exit", receipt["exit"] == 0),
                         ("checks", receipt["checks"] is not None and receipt["checks"] >= 1),
                         ("finished_at", bool(receipt["finished_at"]))):
        if not holds:
            print("verify: state: success with %s %r" % (field, receipt[field]))
            return 1
    # Head first: the same "ours"/"theirs" order as the run's merge.
    # Head first, as run_ci's worktree merge (head is "ours"). `--attr-source`:
    # merge-tree otherwise takes merge attributes from THIS checkout, while the
    # run's worktree merge took them from the head it had checked out, so a head
    # that adds or changes a `merge=` attribute would verify against a different
    # tree (measured: plain merge-tree conflicts where the worktree merge did not).
    merged = run(["git", "--attr-source=" + expected.head, "merge-tree", "--write-tree",
                  expected.head, expected.base], repo)
    if merged.returncode == 1:
        print("verify: merge_tree: conflict now")
        return 1
    if merged.returncode:
        raise RunnerError("git merge-tree failed: " + merged.stderr.strip())
    tree = merged.stdout.splitlines()[0] if merged.stdout else ""
    if receipt["merge_tree"] != tree:
        return mismatch("merge_tree", receipt["merge_tree"], tree)
    if receipt["runner_digest"] != runner_digest():
        return mismatch("runner_digest", receipt["runner_digest"], runner_digest())
    print("verify: ok")
    return 0
