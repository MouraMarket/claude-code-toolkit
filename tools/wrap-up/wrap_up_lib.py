#!/usr/bin/env python3
"""Survey the local git state a wrap-up needs, as JSON.

Why this exists
---------------
`/wrap-up` reports whether it is safe to shut the machine down. Its whole value
is finding work that exists in exactly one place — uncommitted, unpushed, or
sitting on a branch with no remote. That is a **silent** failure: a wrap-up that
misses it produces a confident all-clear and the human stops looking.

The survey lives in a tool rather than in the command's prose because the git
here is easy to get wrong in ways that read as correct:

- **`git log -S` does not answer "which commits contain this".** It reports where
  an occurrence *count changed*, so a history search for a string can return a
  different, smaller set than `git grep` per rev. Under-reporting looks
  identical to a clean result.
- **A squash merge makes a merged branch look unmerged.** `git cherry main <b>`
  reports every commit as `+` because the squash commit is a different object.
  So this tool never *concludes* merged-ness; it reports the count and sets
  `pr_check_required`, and the caller resolves it through `gh`.
- **`git -C ""` is a silent no-op**, not an error. An empty path makes every
  call describe the process's own working directory, so a bad input produces a
  confident answer about a directory nobody asked about.

Network-free on purpose
-----------------------
Nothing here shells out to `gh` or fetches. That keeps the suite hermetic —
no token, no network, no rate limit — and keeps the one ambiguous call
(merged vs not) explicitly in the caller's hands rather than buried here.

`caffeinate` is likewise absent: `claude-caffeinate status` already owns that
and this repo does not keep a second copy of a live tool.
"""

import json
import os
import subprocess
import sys
from typing import Dict, List, Optional

# Same posture as `base-pin` and `lib/worktree_detect.sh`. A stray `GIT_DIR`
# makes calls resolve against a repo the caller never named, which here would
# silently attribute one checkout's dirty files to another. `GIT_COMMON_DIR`
# and `GIT_WORK_TREE` are cleared as cheap defence, not as tested guarantees.
_UNSET_GIT_VARS = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")

try:
    import toolkit_config as _config
except Exception:  # pragma: no cover - absent config must never raise
    _config = None


def _env() -> Dict[str, str]:
    env = dict(os.environ)
    for var in _UNSET_GIT_VARS:
        env.pop(var, None)
    return env


def _git(cwd: str, *args: str) -> Optional[str]:
    """Run a read-only git command in `cwd`; None on any failure.

    An empty `cwd` returns None rather than running — see the module docstring.
    None always means "could not tell", never "no".
    """
    if not cwd:
        return None
    try:
        proc = subprocess.run(
            ("git", "-C", cwd) + args,
            capture_output=True,
            text=True, encoding="utf-8",
            env=_env(),
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def is_git_repo(path: str) -> bool:
    """True for a checkout with a working tree.

    A **bare** repo is excluded deliberately. It has no working tree, so every
    question this tool asks of it — uncommitted files, what branch you are on —
    is meaningless, and answering `dirty_files: 0` for one would contribute a
    reassuring row about a thing that cannot hold unsaved work.
    """
    if _git(path, "rev-parse", "--git-dir") is None:
        return False
    return _git(path, "rev-parse", "--is-bare-repository") == "false"


def default_branch(repo: str) -> Optional[str]:
    """The repo's default branch name, or None.

    Prefers `origin/HEAD`. Falls back to `main`/`master` only if one of them
    actually exists — guessing a name that does not resolve would make every
    downstream count silently empty.
    """
    head = _git(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if head:
        return head.rsplit("/", 1)[-1]
    for candidate in ("main", "master"):
        if _git(repo, "rev-parse", "--verify", "--quiet", candidate) is not None:
            return candidate
    return None


def upstream_state(repo: str) -> str:
    """One of: 'ok', 'gone', 'none'.

    'gone' and 'none' are different problems and must not be merged. A 'gone'
    upstream means the remote branch was deleted — usually a merged PR, and
    `git tidy` deletes it once it can prove the branch merged. 'none' means the branch was never pushed,
    so the commits exist on this disk and nowhere else.
    """
    # The branch header is the ONLY source that distinguishes all three states.
    # `rev-parse @{u}` cannot: once the remote branch is deleted and pruned,
    # `@{u}` fails to resolve exactly as it does for a branch that never had an
    # upstream, so consulting it first collapses 'gone' into 'none'. That is
    # the dangerous direction — 'gone' means `git tidy` may delete the
    # branch, which is a warning, and 'none' is merely a fact.
    #
    #   ## main...origin/main            -> ok
    #   ## main...origin/main [ahead 1]  -> ok
    #   ## feat...origin/feat [gone]     -> gone
    #   ## scratch                       -> none
    #   ## HEAD (no branch)              -> none
    status = _git(repo, "status", "--porcelain=v1", "-b")
    if status is None:
        return "none"
    lines = status.splitlines()
    first = lines[0] if lines else ""
    if "[gone]" in first:
        return "gone"
    return "ok" if "..." in first else "none"


def _count(repo: str, *args: str) -> Optional[int]:
    out = _git(repo, "rev-list", "--count", *args)
    if out is None:
        return None
    try:
        return int(out)
    except ValueError:
        return None


def survey_checkout(path: str) -> Dict:
    """Git facts for one checkout (a primary clone or a linked worktree)."""
    result: Dict = {"path": path}
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    result["branch"] = branch
    result["head"] = _git(path, "rev-parse", "--short", "HEAD")

    status = _git(path, "status", "--porcelain=v1")
    # None (git failed) must not read as a clean tree.
    result["dirty_files"] = None if status is None else len([
        line for line in status.splitlines() if line.strip()
    ])

    state = upstream_state(path)
    result["upstream"] = state

    if state == "ok":
        result["unpushed"] = _count(path, "@{u}..HEAD")
    else:
        result["unpushed"] = None

    base = default_branch(path)
    result["default_branch"] = base
    ahead = None
    if base and branch and branch != base:
        ahead = _count(path, f"{base}..HEAD")
    result["commits_not_in_default"] = ahead

    # The squash trap. A positive count is NOT evidence of unmerged work; it is
    # a question for `gh`. Only a branch with no upstream at all AND commits
    # ahead is unambiguously local-only, because it was never pushed, so no PR
    # can exist for it.
    result["pr_check_required"] = bool(ahead) and state != "none"
    result["local_only"] = bool(ahead) and state == "none"
    return result


def list_worktrees(repo: str) -> Optional[List[str]]:
    """Linked worktree paths for `repo`, excluding the primary checkout.

    Returns None when git could not list them ("could not tell"), which is not
    the same answer as [] ("no linked worktrees").
    """
    out = _git(repo, "worktree", "list", "--porcelain")
    if out is None:
        return None
    paths = [
        line.split(" ", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("worktree ")
    ]
    # The primary checkout is always listed first.
    return paths[1:]


def find_repos(root: str) -> List[str]:
    """Immediate subdirectories of `root` that are git repos, sorted.

    A directory with a `.git` entry that git cannot open is still included:
    it is a checkout the tool could not read, not a non-repo, and dropping it
    would leave it out of the survey silently. A bare repo has no `.git`
    child, so it stays excluded.
    """
    if not root or not os.path.isdir(root):
        return []
    found = []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if is_git_repo(path) or (
            os.path.lexists(os.path.join(path, ".git"))
            and _git(path, "rev-parse", "--git-dir") is None
        ):
            found.append(path)
    return found


def repos_root() -> Optional[str]:
    """The configured repos root (`toolkit_config.repos_root`), or None.

    None rather than a guessed path: the caller then says it could not
    survey, instead of reporting a confident empty result.
    """
    if _config is None:
        return None
    try:
        return _config.repos_root()
    except Exception:
        return None


def _unresolved_reason() -> str:
    """Why `repos_root()` gave None: an unusable setting or no workspace root."""
    try:
        raw = _config.get("wrap_up", "repos_root") if _config is not None else None
    except Exception:
        raw = None
    if raw is not None and raw != "":
        return ("wrap_up.repos_root is not an absolute path after expansion: %r"
                % (raw,))
    return "no workspace_root configured; cannot locate repos"


def survey(root: Optional[str] = None) -> Dict:
    """Full survey. `root` overrides the configured repos root (for tests)."""
    resolved = root if root is not None else repos_root()
    if not resolved:
        return {
            "repos_root": None,
            "surveyed": False,
            "reason": _unresolved_reason(),
            "repos": [],
        }
    # A configured root that is not there must not read as "looked, found
    # nothing". `find_repos` returns [] for a missing directory, and an empty
    # `repos` with `surveyed: true` is exactly the confident all-clear this
    # tool exists to prevent — a typo'd path would certify a machine as clean.
    if not os.path.isdir(resolved):
        return {
            "repos_root": resolved,
            "surveyed": False,
            "reason": f"repos root does not exist: {resolved}",
            "repos": [],
        }
    repos = []
    for repo in find_repos(resolved):
        entry = survey_checkout(repo)
        entry["name"] = os.path.basename(repo)
        wts = list_worktrees(repo)
        entry["worktrees_listed"] = wts is not None
        entry["worktrees"] = [survey_checkout(w) for w in (wts or [])]
        repos.append(entry)
    return {"repos_root": resolved, "surveyed": True, "repos": repos}


def attention(result: Dict) -> List[Dict]:
    """Checkouts a human must decide about, flattened across repos.

    Deliberately conservative: `None` (could not tell) is included, because the
    alternative is dropping a checkout the tool failed to read and calling the
    result clean.
    """
    def plural(n: int, noun: str) -> str:
        # This text is read by a human in a closeout, not parsed. "1 commits"
        # reads as a bug in the tool and quietly costs it credibility on the
        # one line that matters most.
        return f"{n} {noun}" if n == 1 else f"{n} {noun}s"

    items = []
    for repo in result.get("repos", []):
        for checkout in [repo] + list(repo.get("worktrees") or []):
            reasons = []
            if checkout.get("dirty_files") is None:
                reasons.append("could not read status")
            elif checkout["dirty_files"] > 0:
                reasons.append(plural(checkout["dirty_files"], "uncommitted file"))
            if checkout.get("unpushed"):
                reasons.append(plural(checkout["unpushed"], "unpushed commit"))
            upstream = checkout.get("upstream")
            if upstream == "ok" and checkout.get("unpushed") is None:
                reasons.append("could not count unpushed commits")
            base = checkout.get("default_branch")
            branch = checkout.get("branch")
            if (base and branch and branch != base
                    and checkout.get("commits_not_in_default") is None):
                reasons.append(f"could not count commits ahead of {base}")
            # A failed worktree listing hides every linked worktree; the
            # primary checkout carries the warning.
            if checkout is repo and repo.get("worktrees_listed") is False:
                reasons.append("could not list worktrees — a dirty worktree could be hidden")
            if checkout.get("local_only"):
                reasons.append(
                    plural(checkout["commits_not_in_default"], "commit")
                    + ", never pushed, no PR possible"
                )
            elif upstream == "none":
                # Keyed on the upstream state DIRECTLY, not via `local_only`.
                # `local_only` needs a commit count ahead of the default
                # branch, and that count is None whenever HEAD *is* the
                # default branch (see `survey_checkout`'s `branch != base`
                # guard) or the default cannot be resolved. Both zeroed it
                # out, so a repo with no remote at all — `git init`, two
                # commits, never pushed — surveyed as `attention: []` with
                # `surveyed: true`, which the command file tells the reader
                # is a real all-clear. That is the exact silent failure this
                # tool exists to prevent, and it was reachable for anyone
                # with a not-yet-pushed repo under their repos root.
                reasons.append("no upstream — nothing on this branch is pushed anywhere")
            elif checkout.get("pr_check_required"):
                reasons.append(
                    plural(checkout["commits_not_in_default"], "commit")
                    + " ahead — resolve via gh, not git cherry"
                )
            if upstream == "gone":
                reasons.append("upstream gone — git tidy deletes it only if it can prove the branch merged")
            # An unresolvable default branch makes every commit count None.
            # Those are explicit could-not-tells and must not pass silently.
            if checkout.get("default_branch") is None:
                reasons.append("could not resolve a default branch — commit counts unavailable")
            if reasons:
                items.append({
                    "repo": repo.get("name"),
                    "path": checkout.get("path"),
                    "branch": checkout.get("branch"),
                    "reasons": reasons,
                })
    return items


def main(argv: List[str]) -> int:
    root = None
    for i, arg in enumerate(argv):
        if arg == "--root":
            # A trailing `--root` with no value used to fall through and
            # survey the CONFIGURED root instead — answering a question about
            # a directory the caller never named, and looking authoritative.
            if i + 1 >= len(argv):
                print("--root needs a directory", file=sys.stderr)
                return 3
            root = argv[i + 1]
    result = survey(root)
    result["attention"] = attention(result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
