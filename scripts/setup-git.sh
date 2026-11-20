#!/usr/bin/env bash
#
# Git configuration this team relies on. Idempotent; safe to re-run.
#
#   ./scripts/setup-git.sh          apply
#   ./scripts/setup-git.sh --check  report only
#
# Only touches settings that are conventions rather than preferences, and never
# overwrites an existing value without saying so.
#
set -euo pipefail
TILDE='~'  # for ${x/#$HOME/$TILDE}; why in install.sh and lib/tests/test_tilde_display_lint.py

CHECK=0
[[ "${1:-}" == "--check" ]] && CHECK=1

ok()   { printf '  \033[32m%s\033[0m %s\n' "OK" "$*"; }
act()  { printf '  \033[33m%s\033[0m %s\n' "->" "$*"; }
warn() { printf '  \033[31m%s\033[0m %s\n' "!!" "$*"; }

# set_config <key> <value> <why>
set_config() {
  local key="$1" value="$2" why="$3" current
  current="$(git config --global --get "$key" 2>/dev/null || true)"
  if [[ "$current" == "$value" ]]; then
    ok "$key = $value"
  elif [[ -n "$current" ]]; then
    warn "$key is \"$current\", team convention is \"$value\" ($why)"
    warn "   leaving yours alone — change it with:"
    warn "   git config --global $key \"$value\""
  else
    act "$key = $value  ($why)"
    (( CHECK )) || git config --global "$key" "$value"
  fi
}

echo "git setup"
(( CHECK )) && echo "  --check: reporting only"
echo

# Resolved BEFORE anything is written. The worktree refusal below depends on it,
# and a guard that runs after the first side effect is not a guard — this script
# used to reach it only after setting init.defaultBranch, pull.rebase and
# core.excludesfile and appending to ~/.gitignore_global, so "refuses outright"
# was never quite true of it. pwd -P, not pwd, so the path handed to the
# detection is the real one rather than one reached through a symlink.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

# Same refusal install.sh makes, for the same reason, because this is a separate
# entry point that nothing in install.sh calls — README.md and skills/core/onboard/SKILL.md
# invoke it directly. The symlink written below points into THIS checkout. Written
# from a worktree, it dangles the moment the branch merges and the worktree is
# removed, and a dangling core.hooksPath/pre-commit is silent: git skips a hook it
# cannot execute, and core.hooksPath still REPLACES per-repo hooks, so the chaining
# in git-pre-commit never runs either. Net effect is secret scanning off AND every
# existing pre-commit hook on the machine off, with nothing said.
# --check is exempt: it reports and changes nothing.
#
# Detection is shared with install.sh via lib/worktree_detect.sh, and is
# git-based rather than a path pattern. This script kept matching the literal
# `*/.claude/worktrees/*` for a while after install.sh stopped, which meant a
# real apply from a worktree at any other path still wrote the dangling
# pre-commit symlink described above. Fails CLOSED if the helper is absent: the
# consequence of guessing wrong here is silent, so an unreadable guard must stop
# the run rather than wave it through.
if [[ ! -r "$REPO_ROOT/lib/worktree_detect.sh" ]]; then
  warn "refusing to install git hooks: lib/worktree_detect.sh is missing or"
  warn "   unreadable, so whether this checkout is a linked worktree cannot be"
  warn "   determined. Check out the repo again."
  exit 1
fi
# shellcheck source=../lib/worktree_detect.sh
. "$REPO_ROOT/lib/worktree_detect.sh"

if (( ! CHECK )) && is_linked_worktree "$REPO_ROOT"; then
  warn "refusing to install git hooks from a linked git worktree:"
  warn "   $REPO_ROOT"
  warn "   The pre-commit symlink would point here and dangle the moment this"
  warn "   worktree is removed, silently disabling secret scanning and every"
  warn "   other pre-commit hook on this machine. Re-run from the primary"
  warn "   checkout. (--check is safe from anywhere.)"
  exit 1
fi


set_config init.defaultBranch main \
  "new repos start on main, matching the org"
set_config pull.rebase false \
  "merge on pull; rebasing shared branches rewrites history others have"

echo
echo "Global gitignore"
IGNORE="$(git config --global --get core.excludesfile 2>/dev/null || true)"
if [[ -z "$IGNORE" ]]; then
  IGNORE="$HOME/.gitignore_global"
  act "core.excludesfile = ${IGNORE/#$HOME/$TILDE}"
  (( CHECK )) || git config --global core.excludesfile "$IGNORE"
else
  ok "core.excludesfile = ${IGNORE/#$HOME/$TILDE}"
fi
IGNORE="${IGNORE/#\~/$HOME}"
for pattern in '.DS_Store' '__pycache__/' '*.pyc' '.venv/'; do
  if [[ -f "$IGNORE" ]] && grep -qxF "$pattern" "$IGNORE" 2>/dev/null; then
    ok "ignores $pattern"
  else
    act "add $pattern to ${IGNORE/#$HOME/$TILDE}"
    (( CHECK )) || { mkdir -p "$(dirname "$IGNORE")"; printf '%s\n' "$pattern" >> "$IGNORE"; }
  fi
done

echo
echo "Secret scanning (pre-commit)"
# core.hooksPath REPLACES per-repo .git/hooks rather than adding to them, which
# is why the hook we install chains to the repo's own pre-commit if it has one.
# Every other client-side hook type goes through tools/secret-scan/git-hook-forward.
# Same convention as every other setting here: an existing value that is not
# ours is the user's, and stays untouched.
HOOKS_DIR="$HOME/.config/claude-code-toolkit/git-hooks"
current_hooks="$(git config --global --get core.hooksPath 2>/dev/null || true)"
if [[ -n "$current_hooks" && "${current_hooks/#\~/$HOME}" != "$HOOKS_DIR" ]]; then
  warn "core.hooksPath is \"$current_hooks\" — leaving yours alone."
  warn "   Secret scanning is NOT active. To enable it, add this to that"
  warn "   directory's pre-commit hook:"
  warn "     \"\$HOME/.local/bin/claude-secret-scan\" || exit \$?"
else
  # -x, not -L. -L is true for a symlink whose target is gone, so a machine whose
  # hook points at a deleted worktree — the exact failure the guard above now
  # prevents, and one that may already exist on a machine set up before it —
  # reported "pre-commit installed" while scanning nothing. -x follows the link
  # and is false unless something executable is actually there.
  #
  # Executable is not enough: the hook must resolve to THIS checkout's scanner,
  # the same test lib/secret_scan_status.py applies (read by install.sh --check
  # and /onboard verify). `-x` alone reported "pre-commit installed" for an old
  # fork's hook, which the apply below re-links anyway, so --check said "fine"
  # where the status probe said "not active" (H4 F5).
  pre_commit="$HOOKS_DIR/pre-commit"
  scanner="$REPO_ROOT/tools/secret-scan/git-pre-commit"
  real_path() { python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1" 2>/dev/null; }
  # Both resolutions must succeed: two failed lookups are two empty strings,
  # which compare equal, and set -e does not fire inside a condition.
  hook_real="$(real_path "$pre_commit" || true)"
  scanner_real="$(real_path "$scanner" || true)"
  if [[ -x "$pre_commit" && -n "$hook_real" && "$hook_real" == "$scanner_real" ]]; then
    ok "core.hooksPath = ${HOOKS_DIR/#$HOME/$TILDE} (pre-commit installed)"
  elif [[ -e "$pre_commit" && ( -z "$hook_real" || -z "$scanner_real" ) ]]; then
    warn "core.hooksPath = ${HOOKS_DIR/#$HOME/$TILDE} but whether its pre-commit is this"
    warn "   checkout's scanner could not be checked (python3 could not resolve the path)"
    act "re-link pre-commit to ${scanner/#$HOME/$TILDE}"
  elif [[ -L "$pre_commit" && ! -e "$pre_commit" ]]; then
    warn "core.hooksPath = ${HOOKS_DIR/#$HOME/$TILDE} but its pre-commit symlink is BROKEN"
    warn "   -> $(readlink "$pre_commit" 2>/dev/null || echo '?')"
    warn "   Nothing is being scanned. Re-run this from the primary checkout."
  elif [[ -e "$pre_commit" || -L "$pre_commit" ]]; then
    if [[ -L "$pre_commit" ]]; then
      foreign_target="-> $(readlink "$pre_commit" 2>/dev/null || echo '?')"
    else
      foreign_target="(a regular file, not a link)"
    fi
    warn "core.hooksPath = ${HOOKS_DIR/#$HOME/$TILDE} but its pre-commit is NOT this checkout's scanner"
    warn "   $foreign_target"
    act "re-link pre-commit to ${scanner/#$HOME/$TILDE}  (replaces the file above)"
  else
    act "core.hooksPath = ${HOOKS_DIR/#$HOME/$TILDE}  (blocks commits containing credentials)"
  fi

  # core.hooksPath replaces .git/hooks for EVERY hook, not just pre-commit, so
  # each client-side hook a repo may define is linked to a forwarder that hands
  # off to the repo's own hook (tools/secret-scan/git-hook-forward).
  # Not forwarded: pre-commit (stays git-pre-commit, which already chains); the
  # server-side pre-receive, update, post-receive and post-update (out of scope:
  # they only run in a repo RECEIVING a push, and this hooksPath still disables
  # them there); push-to-checkout (its mere presence replaces git's default
  # updateInstead behaviour), proc-receive (must speak a protocol once
  # receive.procReceiveRefs is set) and fsmonitor-watchman (git runs it only via
  # core.fsmonitor, never by name). Changing this list without the drift test in
  # lib/tests/test_setup_git_forwarding.py fails it.
  FORWARDED_HOOKS=(
    applypatch-msg pre-applypatch post-applypatch pre-merge-commit
    prepare-commit-msg commit-msg post-commit pre-rebase post-checkout
    post-merge pre-push reference-transaction pre-auto-gc post-rewrite
    sendemail-validate post-index-change p4-changelist p4-prepare-changelist
    p4-post-changelist p4-pre-submit
  )
  # Owned entries only: a name is ours when nothing is there (a dangling link
  # counts as something) or it is a symlink whose UNRESOLVED target is named
  # git-hook-forward — readlink, not -x/realpath, so a link left by an old,
  # deleted checkout is still recognised. Anything else (a regular file, a
  # directory, a link to some other target) is the user's own global hook and is
  # never touched. pre-commit is not covered by this: it is always relinked.
  is_ours_hook() {
    local path="$HOOKS_DIR/$1"
    if [[ -L "$path" ]]; then
      [[ "$(basename "$(readlink "$path")")" == "git-hook-forward" ]]
    else
      [[ ! -e "$path" ]]
    fi
  }
  stale_forwarders=0
  foreign_hooks=()
  for hook in "${FORWARDED_HOOKS[@]}"; do
    if is_ours_hook "$hook"; then
      [[ -x "$HOOKS_DIR/$hook" ]] || stale_forwarders=$((stale_forwarders + 1))
    else
      foreign_hooks+=("$hook")
    fi
  done
  if (( stale_forwarders == 0 && ${#foreign_hooks[@]} == 0 )); then
    ok "${#FORWARDED_HOOKS[@]} forwarded hooks resolve to an executable"
  fi
  if (( stale_forwarders > 0 )); then
    act "link $stale_forwarders of ${#FORWARDED_HOOKS[@]} forwarded hooks (a repo's own hooks keep running)"
  fi
  if (( ${#foreign_hooks[@]} > 0 )); then
    warn "kept your own ${foreign_hooks[*]} in ${HOOKS_DIR/#$HOME/$TILDE}: a repo's own hook of that name will not run"
  fi

  if (( ! CHECK )); then
    mkdir -p "$HOOKS_DIR"
    ln -sfn "$REPO_ROOT/tools/secret-scan/git-pre-commit" "$HOOKS_DIR/pre-commit"
    for hook in "${FORWARDED_HOOKS[@]}"; do
      if is_ours_hook "$hook"; then
        ln -sfn "$REPO_ROOT/tools/secret-scan/git-hook-forward" "$HOOKS_DIR/$hook"
      fi
    done
    git config --global core.hooksPath "$HOOKS_DIR"
  fi
fi

echo
echo "git tidy alias"
# This script used to INSTALL an alias.tidy here — that alias's first step,
# `git checkout "$def"`, silently yanks a checkout off whatever feature branch
# it was standing on. `git tidy` is now `tools/git-tidy/`, a real versioned
# tool (installed into ~/.local/bin by install.sh) that fast-forwards the
# default branch without ever checking it out. There is nothing left for this
# script to install — only the OLD, dangerous alias left over from a prior
# run to clean up. Only the EXACT alias this script used to write is ever
# touched; a different alias.tidy is the user's own and stays untouched, same
# convention as every other setting here.
OLD_TIDY='!f() { def=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed -e s@^origin/@@); [ -n "$def" ] || def=main; git checkout "$def" || { echo "git tidy: cannot switch to $def - commit or stash your changes first"; return 1; }; git fetch -p; git merge --ff-only "origin/$def" || echo "git tidy: WARNING - $def has diverged from origin/$def and was left unchanged - merge or rebase it yourself"; git for-each-ref --format='"'"'%(refname:short) %(upstream:track)'"'"' refs/heads | while read b t; do [ "$t" = '"'"'[gone]'"'"' ] && git branch -D "$b"; done; true; }; f'
current_tidy="$(git config --global --get alias.tidy 2>/dev/null || true)"
if [[ "$current_tidy" == "$OLD_TIDY" ]]; then
  act "removing alias.tidy — superseded by this toolkit's git-tidy, and its"
  act "   first step ('git checkout \"\$def\"') can clobber a shared checkout"
  (( CHECK )) || git config --global --unset alias.tidy
elif [[ -n "$current_tidy" ]]; then
  ok "alias.tidy already exists and differs — leaving yours alone"
else
  ok "no alias.tidy to remove"
fi

echo
if (( CHECK )); then
  echo "Check complete. Re-run without --check to apply."
else
  echo "Done. Run 'git tidy' in a repo to clean up merged branches."
fi
