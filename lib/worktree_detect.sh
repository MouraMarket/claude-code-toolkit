#!/usr/bin/env bash
#
# is_linked_worktree <dir> — is <dir> a linked git worktree rather than a
# primary checkout?
#
# Sourced by BOTH `install.sh` and `scripts/setup-git.sh`. It lives here rather
# than being written twice because the duplication has already cost once: the
# guard was fixed in `install.sh` and left on the retired path pattern in
# `setup-git.sh`, which meant a real apply from a worktree still wrote a global
# `core.hooksPath` pre-commit symlink into a directory about to be deleted.
# Anything that grows a third caller belongs here too.
#
# Why both callers care: each writes symlinks derived from its own location. From
# a linked worktree those targets vanish when `git worktree remove` runs, which
# disarms `claude-danger-guard` and secret scanning machine-wide while reporting
# success.
#
# Detection is git-based, NOT a path pattern. An earlier version matched the
# literal string `*/.claude/worktrees/*` — this repo's own convention and nothing
# else — so a worktree at any other path (`git worktree add ../foo`, a temp dir,
# a helper with its own layout) sailed through a real apply.
#
# Returns 0 (is a linked worktree, refuse) / 1 (proceed).

# resolve_git_dirs <dir> — resolve the absolute --git-dir and --git-common-dir
# into RESOLVED_GIT_DIR and RESOLVED_COMMON_DIR. Returns 0 on success, 1 if
# either cannot be determined. Split out of is_linked_worktree so it can be
# tested on its own.
#
# It sets globals rather than printing two lines because a two-line stdout
# protocol is not safe for paths: a directory name may contain a newline, and
# splitting on the first one truncated the value and falsely refused a healthy
# primary checkout at such a path. There is no delimiter that is safe here —
# bash variables cannot hold NUL — so the values never share a channel.
#
# That split is not tidiness. When both this and the `.git`-file fallback below
# can refuse the same worktree, a break in EITHER is invisible: the fallback was
# briefly doing all the work for real worktrees because this step was silently
# failing on absolute paths, and every test still passed. Two mechanisms with one
# observable outcome need one test each.
resolve_git_dirs() {
  local repo="$1" git_dir common_dir
  RESOLVED_GIT_DIR=""
  RESOLVED_COMMON_DIR=""

  # An unusable argument is "cannot determine", never an answer about somewhere
  # else. `git -C ""` is a NO-OP rather than an error, so an empty argument makes
  # every command below silently describe the CURRENT WORKING DIRECTORY —
  # measured: `is_linked_worktree ""` returned "refuse" from inside a worktree
  # and "proceed" from a primary checkout, tracking the caller's cwd and not the
  # path it was asked about. A resolver that answers a question nobody asked is
  # worse than one that fails.
  [[ -n "$repo" && -d "$repo" ]] || return 1

  # GIT_DIR is the one that can actually defeat this, and it is the only one with
  # a test. Set in the environment it overrides `-C "$repo"` for repository
  # discovery outright, and — measured — from inside a linked worktree it makes
  # BOTH values succeed and resolve to the stray target, so they compare equal,
  # `resolve_git_dirs` returns success, and the caller reads "primary checkout".
  # The `.git`-file fallback below is never reached, because that only runs when
  # resolution FAILS. A stray GIT_DIR is therefore a silent miss, which is
  # exactly the catastrophe this guard exists to prevent.
  #
  # GIT_COMMON_DIR and GIT_WORK_TREE are unset too, but as cheap defence rather
  # than as fixes, and neither is tested — do not let a comment promote them:
  #   * GIT_COMMON_DIR does NOT cause equality. Measured: it makes `rev-parse`
  #     fail outright ("fatal: not a git repository"), so resolution returns 1
  #     and the fallback delivers a correct refusal. It fails SAFE. A test
  #     asserting "the refusal still fires with a stray GIT_COMMON_DIR" therefore
  #     passes with this `-u` deleted — it is the fallback answering, not this
  #     neutralisation. That test exists below under an honest name.
  #   * GIT_WORK_TREE: `-C` discovery wins over it, and no way to make it cause
  #     either a miss or a false refusal has been found.
  # Keep both anyway — they cost nothing and remove two things to reason about —
  # but do not delete `-u GIT_DIR` on the strength of their harmlessness.
  git_dir="$(env -u GIT_DIR -u GIT_COMMON_DIR -u GIT_WORK_TREE \
    git -C "$repo" rev-parse --git-dir 2>/dev/null)" || return 1
  common_dir="$(env -u GIT_DIR -u GIT_COMMON_DIR -u GIT_WORK_TREE \
    git -C "$repo" rev-parse --git-common-dir 2>/dev/null)" || return 1
  [[ -n "$git_dir" && -n "$common_dir" ]] || return 1

  # Either value can come back relative (".git", "../.git") depending on how git
  # discovered the repo, so a raw string comparison disagrees about two paths
  # that name the same directory and falsely refuses an ordinary checkout whose
  # install.sh sits below the git top level. Resolve both.
  #
  # `CDPATH=` guards a real hazard: with CDPATH set in the environment,
  # `cd somedir` can land somewhere else entirely AND echo the resolved path to
  # stdout, which lands inside this command substitution and corrupts the value.
  # Emptying it is sufficient — CDPATH only ever applies to relative operands.
  # Do NOT "harden" this by prefixing `./`: a worktree's --git-dir is ABSOLUTE,
  # `.//abs/path` is a relative path that does not exist, and the resulting
  # silent failure handed the whole decision to the fallback below.
  RESOLVED_GIT_DIR="$({ CDPATH= cd -- "$repo" && CDPATH= cd -- "$git_dir" && pwd -P; } 2>/dev/null)" || return 1
  RESOLVED_COMMON_DIR="$({ CDPATH= cd -- "$repo" && CDPATH= cd -- "$common_dir" && pwd -P; } 2>/dev/null)" || return 1
  [[ -n "$RESOLVED_GIT_DIR" && -n "$RESOLVED_COMMON_DIR" ]] || return 1
  return 0
}

is_linked_worktree() {
  local repo="$1" gitdir_target

  # Fail CLOSED on an argument this cannot be a verdict about. Both callers derive
  # their path from `$(cd ... && pwd)` under `set -euo pipefail`, so an empty
  # value aborts the script before reaching here — which means this branch is
  # currently unreachable *for a reason that has nothing to do with this guard*.
  # That is precisely why it belongs here: the check lives on an unrelated line in
  # two other files, nobody editing those would know they were preserving it, and
  # this is now a SHARED library whose next caller may not have that property at
  # all. `scripts/setup-git.sh` already computes its own root independently, so
  # "assert it where $REPO is set" would have covered one caller while reading as
  # if it covered both.
  #
  # Refusing rather than proceeding, because this function's answer authorises a
  # write: a guard that cannot identify its subject must not say yes.
  if [[ -z "$repo" || ! -d "$repo" ]]; then
    return 0
  fi

  if resolve_git_dirs "$repo"; then
    [[ "$RESOLVED_GIT_DIR" != "$RESOLVED_COMMON_DIR" ]]
    return
  fi

  # rev-parse failed, or the paths would not resolve. Do NOT read that as "not a
  # worktree": the largest case here is a linked worktree whose admin directory
  # is GONE — the primary clone was moved, renamed or re-cloned, or
  # `.git/worktrees/<name>` was deleted. `rev-parse` then fails with
  # "fatal: not a git repository", and treating that as a plain directory hands a
  # real apply the most doomed target there is. Measured: with the admin dir
  # removed, install.sh exited 0, reported success, and pointed
  # ~/.local/bin/claude-danger-guard into the dead worktree. The retired path
  # pattern refused that case, so reading the failure as "proceed" was a
  # REGRESSION rather than a fresh gap.
  #
  # A linked worktree is still identifiable with no working git at all: its
  # `.git` is a regular FILE whose `gitdir:` names a path under `worktrees/`.
  # That separates it from every legitimate layout that also uses a `.git` file
  # — a submodule points at `.git/modules/<name>`, and `--separate-git-dir`
  # points anywhere else.
  if [[ -f "$repo/.git" ]]; then
    gitdir_target="$(sed -n 's/^gitdir:[[:space:]]*//p' "$repo/.git" 2>/dev/null | head -n 1)"
    if [[ "$gitdir_target" == */worktrees/* ]]; then
      return 0
    fi
  fi

  # Everything left really is "no worktree to be linked": a plain directory, an
  # unpacked tarball, a primary checkout whose `.git` is a directory, a
  # submodule, a `--separate-git-dir` checkout. Proceeding is correct — failing
  # closed here would refuse a legitimate install from a copy of the repo.
  return 1
}
