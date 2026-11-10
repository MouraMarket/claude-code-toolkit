#!/usr/bin/env python3
"""Print git-tidy's worktree section: what is safe to remove, and what is not.

Reports by default, and removes only when asked. Same asymmetry as the
no-upstream branch report this sits below -- a wrong delete is unrecoverable and
a wrong line costs one run -- so the suggestion is still printed every run and
`--prune` is the separate, deliberate act of running it.

`--prune` acts on CACHE verdicts identical to a merged PR and nothing else. It
reimplements no part of the classification: `worktree_lib` already proves a CACHE
branch is byte-identical to the commit its pull request squash-merged, and the two
cruder tests that look sufficient were tried against real worktrees and are
wrong (see that module's docstring). A worktree this removes holds nothing that
exists nowhere else, and `git worktree add` recreates it from a branch that
survives.
"""

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import worktree_lib as lib


# `None` is a meaningful value for merged PR data ("could not fetch"), so it
# cannot double as the "caller passed nothing" default.
_UNSET = object()


def _default_branch(repo):
    head = lib._git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    return head.split("/", 1)[1] if "/" in head else (head or "main")


def verdicts_for(repo, merged_prs=_UNSET):
    """Every worktree's verdict, minus the checkout you are standing in.

    Computed once and passed to both `report` and `prune`. Classifying twice
    would mean two `gh` calls and, worse, two chances for the answer to differ
    between what was printed and what was acted on. `merged_prs` lets `main`
    hand in the PR data it already fetched for the branch step.
    """
    if merged_prs is _UNSET:
        merged_prs = lib.fetch_merged_prs(repo)
    found = lib.classify(repo, merged_prs, lib.load_claims(), _default_branch(repo))

    # Never suggest removing the checkout you are standing in. `git worktree
    # remove` would refuse anyway, but printing the command reads as advice and
    # the refusal reads as a failure -- and running git-tidy from inside a
    # worktree is the normal case here, not the exception. Excluding it here
    # rather than in the printer means `--prune` inherits the same protection
    # instead of needing its own copy of it.
    here = os.path.realpath(lib._git(repo, "rev-parse", "--show-toplevel") or repo)
    return [v for v in found if os.path.realpath(v["path"]) != here]


def removable(verdicts):
    """The worktrees this tool acts on -- CACHE verdicts identical to a merged PR.

    One definition, called by both the printer and the pruner, so that what is
    printed as safe to remove and what `--prune` actually removes cannot drift
    apart. Two copies of this filter is the shape where the report reassures
    you about one set while the flag deletes another.

    A CACHE verdict with no merged PR (nothing ahead of the default branch) is
    excluded on purpose: a build's worktree looks exactly like that before its
    first commit or edit, and no LIVE verdict protects it because a session
    claim is only written after the first edit. `--prune-worktrees` once removed a
    peer's worktree minutes after its plan was written.
    """
    return [v for v in verdicts if v["state"] == lib.CACHE and v.get("merged") is True]


def report(repo, out=sys.stdout, verdicts=None):
    """Write the worktree section. Returns the number of removable worktrees."""
    if verdicts is None:
        verdicts = verdicts_for(repo)
    if not verdicts:
        return 0

    def say(line):
        out.write("git tidy: %s\n" % line)

    holds = [v for v in verdicts if v["state"] == lib.HOLDS]
    cache = removable(verdicts)
    unused = [v for v in verdicts
              if v["state"] == lib.CACHE and v.get("merged") is not True]
    live = [v for v in verdicts if v["state"] == lib.LIVE]
    unknown = [v for v in verdicts if v["state"] == lib.UNKNOWN]

    if holds:
        say("%d worktree(s) hold work, or were held deliberately:" % len(holds))
        for v in holds:
            out.write("  %-24s %s\n" % (os.path.basename(v["path"]), v["note"]))
            if v["warn"]:
                out.write("  %-24s   note: %s\n" % ("", v["warn"]))

    if live:
        say("%d worktree(s) in use by a live session - left alone" % len(live))

    if unused:
        say("%d worktree(s) unused - no PR and nothing ahead - left alone:"
            % len(unused))
        for v in unused:
            out.write("  %-24s %s\n" % (os.path.basename(v["path"]), v["note"]))
            if v["warn"]:
                out.write("  %-24s   note: %s\n" % ("", v["warn"]))
        say("  a build that has not made its first commit looks the same; "
            "remove one by hand if you know it is abandoned")

    if unknown:
        say("%d worktree(s) could not be classified:" % len(unknown))
        for v in unknown:
            out.write("  %-24s %s\n" % (os.path.basename(v["path"]), v["note"]))
        say("  each note says what could not be checked; none of these are "
            "suggested for removal")

    if cache:
        say("%d worktree(s) hold nothing - safe to remove:" % len(cache))
        for v in cache:
            out.write("  %-24s %s\n" % (os.path.basename(v["path"]), v["note"]))
            if v["warn"]:
                out.write("  %-24s   note: %s\n" % ("", v["warn"]))
        say("  the branch survives either way; 'git worktree add' recreates any "
            "of these")
        for v in cache:
            out.write("  git worktree remove %s\n" % v["path"])

    return len(cache)


def _run_git(repo, *args):
    """(returncode, combined output) for a git command that acts.

    `lib._git` is for reading and swallows failure into an empty string, which
    is the wrong shape here: a removal that git refused must be reported with
    git's own reason, and that reason is the whole value of leaning on the
    refusal in the first place.
    """
    try:
        done = subprocess.run(("git", "-C", repo) + args,
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
    except Exception as exc:
        return 1, str(exc) or exc.__class__.__name__
    text = ((done.stderr or "") + (done.stdout or "")).strip()
    return done.returncode, text


def prune(repo, verdicts=None, out=sys.stdout):
    """Remove every merged-PR CACHE-verdict worktree. Returns the number removed.

    Three things here are load-bearing and none of them are fussiness:

    - **`--force` is never passed.** Git refuses to remove a worktree that is
      dirty or carries untracked files, and that refusal is the last-line safety
      mechanism -- the same lean-on-git's-refusals pattern git-tidy already uses
      for `[gone]` branches and for the default-branch fetch. If the verdict and
      git ever disagree, git wins. Git does **not** protect ignored files: it
      deletes them. So `classify_one` checks them itself, and only the Python
      caches in `DISPOSABLE_IGNORED_DIRS` / `DISPOSABLE_IGNORED_SUFFIXES` are
      treated as disposable.
    - **`--prune` acts on CACHE verdicts identical to a merged PR and nothing
      else.** HOLDS, LIVE and UNKNOWN are all kept, and the report above has
      already named them. A CACHE verdict with no merged PR is also kept -- see
      `removable()` -- and UNKNOWN in particular means the pull request data
      could not be fetched, or that a claiming session's liveness could not be
      checked, which `worktree_lib` deliberately degrades to rather than
      guessing.
    - **The branch is left alone.** The `[gone]` branch cleanup deletes it on
      the next run, once the worktree stops holding it and it can prove the
      branch merged. Deleting it here would put that logic in a second place.
    """
    if verdicts is None:
        verdicts = verdicts_for(repo)
    cache = removable(verdicts)

    def say(line):
        out.write("git tidy: %s\n" % line)

    removed = 0
    for v in cache:
        code, why = _run_git(repo, "worktree", "remove", v["path"])
        if code == 0:
            removed += 1
            out.write("  removed %s\n" % v["path"])
        else:
            # Reported, and the loop continues: one worktree git refuses must
            # not cost the others their cleanup. Git's reason is quoted rather
            # than summarised -- it is the reason the refusal is trusted as the
            # safety mechanism, so paraphrasing it would be throwing away the
            # only evidence the person reading this has.
            first = why.splitlines()[0] if why else ""
            out.write("  KEPT %s - git refused: %s\n"
                      % (v["path"], first or "no reason given"))

    # Entries whose directory is already gone -- a hand-deleted worktree, or an
    # interrupted removal. Nothing above produces these; they accumulate on
    # their own and this is the one command that clears them. Reported rather
    # than done quietly: a flag that silently changes state it never mentioned
    # is how a tool stops being trusted.
    _, pruned = _run_git(repo, "worktree", "prune", "-v")
    stale = [l for l in pruned.splitlines() if l.strip()]

    if not cache:
        say("--prune-worktrees: no worktree was safe to remove; none was removed")
    else:
        say("--prune-worktrees: removed %d of %d; the branch(es) survive - the "
            "next 'git tidy' deletes any whose remote is gone and that it can "
            "prove merged" % (removed, len(cache)))
    if stale:
        say("  also cleared %d stale worktree entry(s) whose directory was "
            "already gone" % len(stale))
    return removed


def clean_gone_branches(repo, merged_prs, default_branch, out=sys.stdout,
                        err=sys.stderr):
    """Delete [gone] branches that can be proven merged. Returns the number deleted.

    A [gone] upstream only says the remote branch was deleted -- by a merge, by
    closing the PR unmerged, or by hand -- so each branch needs positive
    evidence (see `lib.gone_branch_evidence`) before `git branch -D`. The rest
    are kept and listed with the reason, so the person can decide.

    A branch another worktree holds is refused by git, and that refusal is
    CORRECT: it is one of the protections the worktree workflow leans on. The
    raw error reads like a failure when nothing failed, so those are counted
    and reported once.
    """
    deleted, held, kept = 0, 0, []
    for name in lib.gone_branches(repo):
        proven, why = lib.gone_branch_evidence(repo, name, merged_prs, default_branch)
        if not proven:
            kept.append((name, why))
            continue
        code, text = _run_git(repo, "branch", "-D", name)
        if code == 0:
            deleted += 1
            out.write(text + "\n")
        elif "used by worktree" in text or "checked out at" in text:
            held += 1
        else:
            first = text.splitlines()[0] if text else ""
            err.write("git tidy: WARNING - %s\n" % first)
    if held:
        out.write("git tidy: %d branch(es) skipped - held by worktrees\n" % held)
    if kept:
        out.write("git tidy: %d branch(es) whose remote is gone were kept - "
                  "nothing shows they merged:\n" % len(kept))
        for name, why in kept:
            out.write("  %-24s %s\n" % (name, why))
        out.write("git tidy:   check one with 'git log <name>', then delete it "
                  "with 'git branch -D <name>'\n")
    return deleted


def main(argv):
    flags = ("--prune", "--gone-branches")
    rest = [a for a in argv[1:] if a not in flags]
    wanted = "--prune" in argv[1:]
    gone = "--gone-branches" in argv[1:]
    repo = rest[0] if rest else "."
    # Fetched once: the branch step needs it to prove a squash merge and the
    # worktree step needs it for verdicts, so one `gh` call serves both.
    merged_prs = lib.fetch_merged_prs(repo)
    default_branch = _default_branch(repo)
    # The branch step runs FIRST: a branch a pruned worktree held is cleared on
    # the next run, never the same one (see git-tidy). The two steps have
    # separate guards so one failing does not skip the other, and each says so
    # -- a silent failure here would hide that gone branches stopped being
    # cleaned. `out`/`err` are passed explicitly, read at call time: a default
    # argument is bound at import, so a caller that swaps sys.stdout would
    # capture nothing.
    if gone:
        try:
            clean_gone_branches(repo, merged_prs, default_branch,
                                out=sys.stdout, err=sys.stderr)
        except Exception as exc:
            sys.stderr.write("git tidy: WARNING - branch cleanup stopped (%s) - "
                             "any branch it deleted is listed above\n"
                             % exc.__class__.__name__)
    try:
        found = verdicts_for(repo, merged_prs)
        report(repo, out=sys.stdout, verdicts=found)
        if wanted:
            prune(repo, verdicts=found, out=sys.stdout)
    except Exception as exc:
        # Same contract as every hook in this repo: a diagnostic that breaks
        # must never break the tool it is attached to. Each removal has its own
        # guard above, so a single refusal never reaches here.
        sys.stderr.write("git tidy: WARNING - worktree report failed (%s)\n"
                         % exc.__class__.__name__)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main(sys.argv))
