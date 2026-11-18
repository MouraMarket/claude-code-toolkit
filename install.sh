#!/usr/bin/env bash
#
# Install this Claude Code environment on a Mac.
#
# Idempotent: safe to re-run after every pull. It creates symlinks and enables the
# `toolkit-core` plugin and the selected profile plugins, which carry the hooks (each
# plugin's hooks/hooks.json: plugin/ for core, plugin-*/ for the rest) — it never
# overwrites your permissions, model, theme, or other plugins. Each run first takes
# a timestamped snapshot of the settings file if it changed since the last one
# (newest 10 kept, plus the first-install copy). Each apply records what it
# added in ~/.config/claude-code-toolkit/install-record.json, so removing the
# toolkit can take out only what it added (see --uninstall).
#
#   ./install.sh                apply
#   ./install.sh --check        report what it would do, change nothing
#   ./install.sh --links-only   report only which symlinks are absent
#   ./install.sh --verify-live  prove every selected profile plugin's hooks fired (run through Claude)
#   ./install.sh --autonomous   apply, plus the opt-in wide Bash grant (asks first)
#   ./install.sh --version      what is installed and running; read-only, no network
#   ./install.sh --update       fast-forward to origin's default branch, then run the new
#                               installer; prints --rollback if that install fails
#   ./install.sh --rollback     go back to the commit the last --update left, held there
#                               until the next --update
#   ./install.sh --uninstall    list what removing this install would take out; changes nothing
#   ./install.sh --uninstall --yes  remove it: snapshot settings.json first, then verify
#
set -euo pipefail
# Display paths as ~/... via ${path/#$HOME/$TILDE}. A literal `~` in that
# replacement is tilde-expanded back to $HOME by bash 4.3+ (Ubuntu, Git Bash)
# but not by macOS's bash 3.2; an expanded variable is never tilde-expanded.
TILDE='~'

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECK=0
SETUP=0
LINKS_ONLY=0
MANIFEST=0
VERIFY_LIVE=0
AUTONOMOUS=0
VERSION=0
UPDATE=0
ROLLBACK=0
UNINSTALL=0
YES=""
# These three modes take no other arguments, in any position, and `--yes`
# belongs to `--uninstall` alone, so this runs before the mode `case` (which
# would otherwise answer --help first).
for _arg in "$@"; do
  case "$_arg" in
    --version|--update|--rollback)
      if (( $# > 1 )); then
        echo "$_arg takes no other arguments" >&2
        exit 2
      fi ;;
    --yes)
      if [[ "$1" != "--uninstall" ]]; then
        echo "unknown option: --yes (only valid as: ./install.sh --uninstall --yes)" >&2
        exit 2
      fi ;;
  esac
done
case "${1:-}" in
  --version) VERSION=1 ;;
  --update) UPDATE=1 ;;
  --rollback) ROLLBACK=1 ;;
  --uninstall) UNINSTALL=1 ;;
  --check) CHECK=1 ;;
  --links-only) CHECK=1; LINKS_ONLY=1 ;;
  --manifest) CHECK=1; LINKS_ONLY=1; MANIFEST=1 ;;
  --verify-live) CHECK=1; VERIFY_LIVE=1 ;;
  --setup) SETUP=1 ;;
  --autonomous) AUTONOMOUS=1 ;;
  --help|-h)
    cat <<'USAGE'
usage: ./install.sh [--check | --links-only | --manifest | --verify-live | --setup | --autonomous]
       ./install.sh --uninstall [--yes]
       ./install.sh --version | --update | --rollback

  (no flag)      install: run first-time setup if there is no config yet, then
                 symlink everything into place, enable the toolkit-core plugin
                 (the hooks come from it) and merge the BASELINE permissions, and run the tests
  --check        report what would change and change nothing (never prompts)
  --links-only   report ONLY which symlinks are absent, then stop. Skips the
                 plugin step, the prune, and the test suite -- so it answers
                 "did a pull add something I never linked?" in about a second
                 instead of the several minutes a full --check spends on tests.
                 Exits 1 if any link is missing, 0 if every one is in place.
                 Run it from the PRIMARY checkout: from a linked worktree every
                 link compares against that worktree, so all of them read as
                 absent on a fully installed machine. It says so rather than
                 reporting a count you would act on.
  --manifest     print every destination the installer manages, tab-separated, then stop. Changes nothing.
  --verify-live  prove each selected profile plugin's hooks fired in this Claude Code session, on
                 its current hooks.json, and print its readiness (configured, installed,
                 runtime-proven, unverified); run it through Claude. Exit 0 when every selected
                 profile is proven (a Claude Code version other than the tested one reads as
                 unverified, not failed), 1 if any is not. Changes nothing.
  --setup        re-run the interactive configuration, then install
  --autonomous   ADVANCED. Install, and additionally grant Bash(*) under
                 defaultMode "auto", so almost nothing prompts. Auto mode drops
                 the blanket grant, so unmatched Bash goes to its classifier,
                 after the claude-danger-guard hook. Narrow allow rules skip the
                 classifier. Outside auto mode the guard is the only check.
                 Requires typing a confirmation. Read
                 claude/permissions.autonomous.json first.
                 A plain install NEVER does this.
  --uninstall    list what removing this install would take out, what it keeps and
                 what blocks it, and change nothing (exit 1 if anything blocks).
                 Only what the install record or an exact rule proves the installer
                 added is ever removed. Run it from the primary checkout.
  --uninstall --yes  do it: snapshot settings.json, remove, verify, then drop the record
  --version      what is installed and running: the commit, whether it is pinned, behind
                 origin as of the last fetch, and the last install. Read-only, no network.
  --update       fast-forward to origin's default branch, then run the new installer.
                 Prints --rollback if that install fails.
  --rollback     go back to the commit the last --update left, and stay there, with no
                 updates, until the next --update.

  CLAUDE_INSTALL_SKIP_SUITES=1  skip the Tests section's suite sweep (the
                 suites still ran elsewhere, e.g. ci/local-checks.sh, which
                 already covers them under its own two Python versions).
                 Any other value, or unset, runs the sweep as normal.

The baseline permissions only ever narrow what Claude may do: they add deny rules
for credential paths and force a prompt on pushing, PRs, releases and publishes.
Installing them cannot make your setup more permissive.
USAGE
    exit 0 ;;
  "") ;;
  *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
esac

# `--uninstall` takes `--yes` and nothing else (the loop above refused a
# `--yes` anywhere else).
if (( UNINSTALL )); then
  if [[ "${2:-}" == "--yes" ]]; then
    YES=1
    EXTRA="${3:-}"
  else
    EXTRA="${2:-}"
  fi
  if (( $# > 2 )) || { [[ -z "$YES" ]] && (( $# > 1 )); }; then
    echo "unknown option: ${EXTRA:-(empty)} (usage: ./install.sh --uninstall [--yes])" >&2
    exit 2
  fi
elif (( $# > 1 )); then
  echo "$1 takes no other arguments" >&2
  exit 2
fi

ok()   { printf '  \033[32m%s\033[0m %s\n' "OK" "$*"; }
act()  { printf '  \033[33m%s\033[0m %s\n' "->" "$*"; }
warn() { printf '  \033[31m%s\033[0m %s\n' "!!" "$*"; }

# macOS only, and refused before anything is read or written. The hooks, the
# guards' path rules and the Mac profile all assume it; on another system an
# install would report success while protecting nothing. `--check` refuses too:
# a report that says what it would do on a host it cannot install on is not one
# to act on.
if [[ "$(uname -s 2>/dev/null)" != "Darwin" ]]; then
  echo "refusing to install: this toolkit supports macOS only (uname -s says '$(uname -s 2>/dev/null)')." >&2
  echo "Nothing was changed." >&2
  exit 1
fi

# A relocated CLAUDE_CONFIG_DIR is refused before anything is read or written.
# Everything this installer does goes to ~/.claude, but Claude Code reads its
# hooks, commands, agents, skills and settings from $CLAUDE_CONFIG_DIR when that
# is set, so a relocated install is inert and nothing else says so. Full support
# is deferred until someone actually relocates (about 90 hard-coded references
# across the tools). Compared after resolving symlinks, so a CLAUDE_CONFIG_DIR
# that merely points at ~/.claude stays quiet. Only a run that writes is
# refused; a read-only mode reports on stderr and carries on, and `--check`
# also fails on it (RELOCATED, below).
RELOCATED=0
config_dir="${CLAUDE_CONFIG_DIR:-}"
if [[ -n "$config_dir" ]]; then
  config_dir="${config_dir/#\~/$HOME}"
  resolved_config="$( (cd "$config_dir" 2>/dev/null && pwd -P) || printf '%s' "$config_dir")"
  resolved_home_claude="$( (cd "$HOME/.claude" 2>/dev/null && pwd -P) || printf '%s' "$HOME/.claude")"
  if [[ "${resolved_config%/}" != "${resolved_home_claude%/}" ]]; then
    RELOCATED=1
    if (( ! CHECK && ! VERSION )) && { (( ! UNINSTALL )) || [[ -n "$YES" ]]; }; then
      echo "refusing to install: CLAUDE_CONFIG_DIR is set to $CLAUDE_CONFIG_DIR, but this toolkit installs" >&2
      echo "into ~/.claude. Claude Code reads its hooks, commands, agents and skills from" >&2
      echo "CLAUDE_CONFIG_DIR, so none of this install would be active. Relocation is not" >&2
      echo "supported yet: unset CLAUDE_CONFIG_DIR. Nothing was changed." >&2
      exit 1
    fi
    {
      echo
      warn "CLAUDE_CONFIG_DIR is set to $CLAUDE_CONFIG_DIR, but this toolkit installs"
      warn "   into ~/.claude. Claude Code reads its hooks, commands, agents and"
      warn "   skills from CLAUDE_CONFIG_DIR, so none of this install is active."
      warn "   Relocation is not supported yet: unset CLAUDE_CONFIG_DIR."
    } >&2
  fi
fi

# --version reads state only, so it sits before the worktree refusal below and
# works from a linked worktree too.
if (( VERSION )); then
  exec python3 "$REPO/lib/toolkit_update.py" version "$REPO"
fi

# One timed snapshot per run, so every run is reversible and none overwrites
# another. Skipped when the newest one already holds these exact bytes (a re-run
# from converged settings adds nothing). Pruned to SNAPSHOT_KEEP, oldest first;
# the .pre-claude-code-toolkit.bak above is never counted or pruned.
SNAPSHOT_KEEP=10  # timed snapshots kept after a run
take_timed_snapshot() {
if (( ! CHECK )) && [[ -f "$SETTINGS" ]]; then
  NEW_SNAPSHOT=$(REPO="$REPO" SETTINGS="$SETTINGS" SNAPSHOT_KEEP="$SNAPSHOT_KEEP" python3 - <<'PY'
import os, re, sys, time

sys.path.insert(0, os.path.join(os.environ["REPO"], "lib"))
import settings_io

settings = os.environ["SETTINGS"]
keep = int(os.environ["SNAPSHOT_KEEP"])
# INSTALL_SNAPSHOT_STAMP is test-only: it pins the stamp so a same-second
# collision is reproducible. Anything that is not a UTC stamp is ignored.
stamp = os.environ.get("INSTALL_SNAPSHOT_STAMP", "")
if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", stamp):
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
directory, base = os.path.split(settings)
pattern = re.compile(r"^%s\.claude-code-toolkit-([0-9]{8}T[0-9]{6}Z)(?:-([0-9]+))?\.bak$"
                     % re.escape(base))


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


def snapshots():
    """Timed snapshots oldest first, by (stamp, counter) -- not by name:
    `Z-2.bak` sorts before `Z.bak` lexically."""
    found = []
    for name in os.listdir(directory):
        match = pattern.match(name)
        if match:
            found.append((match.group(1), int(match.group(2) or 1),
                          os.path.join(directory, name)))
    return sorted(found)


current = read(settings)
existing = snapshots()
created = None
if not existing or read(existing[-1][2]) != current:
    counter = 1
    while True:
        suffix = "" if counter == 1 else "-%d" % counter
        target = os.path.join(directory, "%s.claude-code-toolkit-%s%s.bak" % (base, stamp, suffix))
        if not os.path.exists(target) and settings_io.create_atomic(
                target, current, stat_from=settings):
            created = os.path.basename(target)
            break
        # Taken already (or lost a race to an identical stamp): same handling.
        if os.path.exists(target) and read(target) == current:
            target = None  # these exact bytes are already held under this stamp
            break
        counter += 1
remaining = snapshots()
for _, _, path in remaining[:max(0, len(remaining) - keep)]:
    os.remove(path)
if created:
    print(created)
PY
)
  if [[ -n "$NEW_SNAPSHOT" ]]; then
    act "snapshot of settings.json saved to $NEW_SNAPSHOT"
  fi
fi
}

# $REPO is derived from this script's own location. Run from a linked worktree,
# that derives every symlink target, hook path, and permission file from a
# directory that vanishes the moment `git worktree remove` cleans it up —
# silently breaking hooks and slash commands machine-wide, for everyone, not
# just the session that ran this. --check is exempt: it only reports, it changes
# nothing. A real apply from a worktree refuses outright;
# ALLOW_WORKTREE_INSTALL=1 is the deliberate override for the one legitimate
# case — testing install.sh itself from a branch that's still in review.
#
# Detection is git-based, NOT a path pattern. An earlier version matched the
# literal string `*/.claude/worktrees/*` — this repo's own convention and
# nothing else. Any worktree created anywhere else (`git worktree add ../foo`, a
# temp dir, a `wt`-style helper that uses its own layout) sailed straight
# through a real apply and repointed every ~/.local/bin symlink — including
# claude-danger-guard — at a directory that disappears on removal. A guard that
# only recognises the convention it was written beside is a guard you cannot
# rely on, and its failure is completely silent.
#
# The detection itself lives in lib/worktree_detect.sh, shared with
# scripts/setup-git.sh — which writes a global core.hooksPath symlink and needs
# the identical answer. Keeping two copies has already cost once: this guard was
# fixed here and left on the retired path pattern there.
#
# A missing helper fails CLOSED. A guard whose implementation has gone absent
# must not become an install that proceeds unguarded, and a checkout missing its
# own lib/ is not one to install from anyway.
if [[ ! -r "$REPO/lib/worktree_detect.sh" ]]; then
  echo "refusing to install: $REPO/lib/worktree_detect.sh is missing or unreadable." >&2
  echo "That file decides whether this checkout is a linked worktree, and" >&2
  echo "installing without that answer can repoint every ~/.local/bin symlink" >&2
  echo "at a directory that is about to be deleted. Check out the repo again." >&2
  exit 1
fi
# shellcheck source=lib/worktree_detect.sh
. "$REPO/lib/worktree_detect.sh"

if (( ! CHECK )) && is_linked_worktree "$REPO"; then
  if [[ "${ALLOW_WORKTREE_INSTALL:-}" != "1" ]]; then
    echo "refusing to install from a linked git worktree: $REPO" >&2
    echo "Every symlink and hook path this writes would point here, and this" >&2
    echo "directory disappears the moment the worktree is removed. Run" >&2
    echo "./install.sh from the primary checkout instead." >&2
    echo "If you genuinely mean to install from this worktree (e.g. testing" >&2
    echo "install.sh itself before merge), set ALLOW_WORKTREE_INSTALL=1." >&2
    exit 1
  fi
  warn "REPO is a linked worktree checkout ($REPO) — proceeding only because"
  warn "   ALLOW_WORKTREE_INSTALL=1 is set. Everything just linked will break"
  warn "   the moment this worktree is removed."
fi

# --uninstall: gather, plan, and (with --yes) apply in lib/uninstall.py. It is
# behind the worktree refusal above because a linked worktree's $REPO is not the
# checkout the install was made from. It is ahead of the declined-core refusal
# below: a config that declines core guards must not stop someone removing the
# toolkit. Nothing below this block ever runs on it.
if (( UNINSTALL )); then
  SETTINGS="$HOME/.claude/settings.json"
  # Snapshot only once the plan has no blockers: a refused run must not rotate
  # the timed snapshots. The --yes run below re-plans and refuses on its own.
  # Preflight exits 0 (no blockers: snapshot), 1 (blockers: the --yes run refuses
  # on its own) or anything else (it crashed): fail closed, since applying
  # without a snapshot is the one outcome this must never reach.
  if [[ -n "$YES" ]]; then
    preflight=0
    python3 "$REPO/lib/uninstall.py" "$REPO" --preflight || preflight=$?
    if (( preflight == 0 )); then
      take_timed_snapshot
    elif (( preflight != 1 )); then
      echo "uninstall preflight failed (exit $preflight); nothing was changed" >&2
      exit 1
    fi
  fi
  exec python3 "$REPO/lib/uninstall.py" "$REPO" ${YES:+--yes}
fi

CONFIG="$HOME/.config/claude-code-toolkit/config.json"

# A config that declines an always-on core guard stops a real run before it
# changes anything. Core cannot be installed without those guards, and
# installing them anyway would override a choice someone made; the remedy
# (`--update --accept-core`, or a skills-only install) is theirs to pick.
# `--check` changes nothing, so there it is a required failure, reported with
# everything else. Absent or unparseable config: nothing declined (setup and the
# Config section below handle those). A config that exists while
# lib/setup_config.py does not fails CLOSED, like the worktree helper above: a
# check that could not run must not read as "nothing declined".
#
# Prints the declined sections one per line; exit 0 = answered, 1 = could not.
core_conflicts() {
  REPO="$REPO" CONFIG="$CONFIG" python3 - <<'PY'
import json, os, sys

if not os.path.lexists(os.environ["CONFIG"]):
    sys.exit(0)
# Load the helper BEFORE parsing: an unparseable config must not short-circuit
# past a helper that is missing, or the double fault reads as "nothing declined".
sys.path.insert(0, os.path.join(os.environ["REPO"], "lib"))
try:
    import setup_config
except Exception as exc:  # any import failure is "could not check"
    sys.stderr.write("lib/setup_config.py could not be loaded: %s\n" % exc)
    sys.exit(1)
try:
    with open(os.environ["CONFIG"], encoding="utf-8") as handle:
        config = json.load(handle)
except (OSError, ValueError):
    sys.exit(0)
if not isinstance(config, dict):
    sys.exit(0)
for section in setup_config.core_conflicts(config):
    print(section)
PY
}

CORE_CONFLICTS=""
core_rc=0
CORE_CONFLICTS="$(core_conflicts)" || core_rc=$?
if (( core_rc != 0 )); then
  echo "refusing to install: could not check $CONFIG for declined core guards" >&2
  echo "(lib/setup_config.py is missing or failed). Nothing was changed." >&2
  exit 1
fi
if [[ -n "$CORE_CONFLICTS" ]] && (( ! CHECK )); then
  echo "refusing to install: your config declines guards this toolkit's core always installs:" >&2
  while IFS= read -r section; do
    echo "  - $section" >&2
  done <<< "$CORE_CONFLICTS"
  echo "Nothing was changed. Choose one, then re-run ./install.sh:" >&2
  echo "  accept them:  python3 \"$REPO/lib/setup_config.py\" \"$CONFIG\" \"$REPO\" --update --accept-core" >&2
  echo "  or install the skills only, without the toolkit's core." >&2
  exit 1
fi

# --update / --rollback: move the checkout in lib/toolkit_update.py, then run the
# checked-out installer as a CHILD process (a fresh bash reads the new file; this
# one keeps the file it opened). Behind the worktree refusal, and behind the
# declined-core refusal so a config the new installer would refuse stops this
# run before the checkout moves.
if (( UPDATE || ROLLBACK )); then
  mode=update; (( ROLLBACK )) && mode=rollback
  from=$(env -u GIT_DIR -u GIT_WORK_TREE git -C "$REPO" rev-parse HEAD)
  rc=0; python3 "$REPO/lib/toolkit_update.py" "$mode" "$REPO" || rc=$?
  case $rc in
    10) exit 0 ;;
    0|11) ;;
    *) exit 1 ;;
  esac
  to=$(env -u GIT_DIR -u GIT_WORK_TREE git -C "$REPO" rev-parse HEAD)
  install_rc=0; "$REPO/install.sh" || install_rc=$?
  if [[ "$mode" == update ]] && (( rc == 11 )); then
    # Already at origin: the checkout never moved, so "from -> to" would read
    # as an update and the rollback advice below would not apply.
    if (( install_rc == 0 )); then
      echo "Already at ${to:0:12}; ran the install it never had. Start a new Claude Code session (or run /reload-plugins) to load it."
      exit 0
    fi
    echo "Already at ${to:0:12}, so the checkout did not move, but the install failed (exit $install_rc)."
    exit "$install_rc"
  fi
  if [[ "$mode" == update ]]; then
    if (( install_rc == 0 )); then
      echo "Updated ${from:0:12} -> ${to:0:12}. Start a new Claude Code session (or run /reload-plugins) to load it."
      exit 0
    fi
    echo "The checkout moved from ${from:0:12} to ${to:0:12}, but the install failed (exit $install_rc)."
    echo "To go back:        ./install.sh --rollback"
    echo "If that fails too: git -C $REPO checkout --detach $from && $REPO/install.sh"
    exit "$install_rc"
  fi
  if (( install_rc == 0 )); then
    echo "Pinned at ${to:0:12}. You stay here, with no updates, until ./install.sh --update."
    exit 0
  fi
  echo "Rolled back to ${to:0:12}, but the install failed (exit $install_rc). Its output above names the failing step; ./install.sh --check reports it again without changing anything."
  exit "$install_rc"
fi

# Prints "<state><TAB><message>" for the session in $1; non-zero if the helper is
# missing or raises, so callers can say "unavailable" rather than crash.
live_verdict() {
  REPO="$REPO" SESSION_ID="$1" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO"], "lib"))
import hook_heartbeat
state, message = hook_heartbeat.verdict(
    os.environ["REPO"], os.path.expanduser("~"), os.environ["SESSION_ID"])
print("%s\t%s" % (state, message))
PY
}

# Prints the per-profile readiness block; its exit status is 0 (all selected
# profiles proven or unverified), 1 (one is configured or installed) or other
# (could not be computed).
readiness_report() {
  python3 "$REPO/lib/readiness.py" report "$REPO"
}

# Prints "<state><TAB><message>" from the cross-session hook-fires audit (states:
# flagged, incomplete, ok, no_era); non-zero if the helper is missing or raises.
guards_verdict() {
  REPO="$REPO" python3 - <<'PY'
import datetime, os, sys, time
sys.path.insert(0, os.path.join(os.environ["REPO"], "lib"))
import hook_heartbeat
result = hook_heartbeat.audit(
    os.path.expanduser("~"),
    exclude_session=os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
    deadline=time.monotonic() + 10)


def day(value):
    return value[:10] if value else "?"


if result["window_start"] is None:
    print("no_era\tno first-install time recorded yet; re-run ./install.sh")
    sys.exit(0)
since = datetime.datetime.fromtimestamp(
    result["window_start"], datetime.timezone.utc).strftime("%Y-%m-%d")
named = ["%s %s %s" % ((item["session"] or "?")[:8], item["project"], day(item["started_at"]))
         for item in result["flagged"]]
named += ["%s %s %s" % ((item["session"] or "?")[:8], item["project"], day(item["started_at"]))
          for item in result["unchecked"] if item["reason"] == "unreadable"]
late = [item for item in result["unchecked"] if item["reason"] == "deadline"]
if named:
    shown = ", ".join(named[:5])
    if len(named) > 5:
        shown += ", and %d more" % (len(named) - 5)
    print("flagged\t%d session(s) since %s ran shell commands with no toolkit hook "
          "firing or could not be read: %s" % (len(named), since, shown))
elif late:
    print("incomplete\tcould not finish checking %d session(s) since %s before the "
          "time limit; no hookless session found in the rest" % (len(late), since))
else:
    print("ok\thooks fired in every session started since %s that ran shell commands"
          % since)
PY
}

if (( VERIFY_LIVE )); then
  echo "Live hook check"
  if LIVE_OUT=$(live_verdict "${CLAUDE_CODE_SESSION_ID:-}" 2>/dev/null); then
    if [[ "${LIVE_OUT%%$'\t'*}" == "proven" ]]; then
      ok "${LIVE_OUT#*$'\t'}"
    else
      warn "${LIVE_OUT#*$'\t'}"
    fi
  else
    warn "live check unavailable (lib/hook_heartbeat.py is missing or failed)"
  fi
  # Every selected profile, core included: exit 0 only when readiness does.
  READINESS_RC=0
  READINESS_OUT=$(readiness_report 2>/dev/null) || READINESS_RC=$?
  if (( READINESS_RC == 0 || READINESS_RC == 1 )) && [[ -n "$READINESS_OUT" ]]; then
    echo "$READINESS_OUT"
    exit "$READINESS_RC"
  fi
  warn "readiness check unavailable (lib/readiness.py is missing or failed)"
  exit 1
fi

# Computed once, for the PATH check near the end of the run.
if [[ ":$PATH:" == *":$HOME/.local/bin:"* ]]; then
  ON_LOCAL_BIN_PATH=1
else
  ON_LOCAL_BIN_PATH=0
fi

# Every path through `link` that does not end in `ok` leaves that entry point
# NOT installed, so all of them count here. `--links-only` turns the count into
# an exit status; a plain run and a plain `--check` ignore it and are unchanged.
# `$(( x + 1 ))` rather than `(( x++ ))`: the latter evaluates to 0 on the first
# increment, which under `set -e` aborts the installer.
MISSING_LINKS=0

# What makes the run exit 1 on a REQUIRED component, one human-readable line
# each, printed together before the closing line. Separate from MISSING_LINKS,
# which also counts the things that stay warnings (a real file in the way is a
# first-class /onboard outcome). `--check` used to exit 0 over every one of
# these. LINKS_ABSENT counts links the `act` branch would create; only `--check`
# turns it into a failure, since an apply creates them.
REQUIRED_FAILURES=()
LINKS_ABSENT=0
# Set by the relocation check near the top; only a plain `--check` fails on it
# here (a writing run was refused there, the other read-only modes only print it).
if (( RELOCATED && CHECK && ! LINKS_ONLY && ! VERIFY_LIVE )); then
  REQUIRED_FAILURES+=("CLAUDE_CONFIG_DIR points away from ~/.claude, so none of this install is active")
fi
# The declined-core preflight above refused a real run; `--check` reports it.
if [[ -n "$CORE_CONFLICTS" ]]; then
  while IFS= read -r section; do
    REQUIRED_FAILURES+=("config declines $section, which core always installs — see Config health")
  done <<< "$CORE_CONFLICTS"
fi

# Every linkname this installer knows how to manage, recorded as it goes. The
# stale-link prune near the end reads it as an EXCLUSION: a dangling link whose
# name appears here is one we still manage and were merely unable to create
# from this checkout, so it is left alone; a dangling link whose name is absent
# belongs to something the repo no longer has at all, and that is the leftover
# the prune exists to clear.
#
# Recorded from `link` itself rather than from a parallel list, and that is the
# whole design. `EXECUTABLE_NAMES` was the obvious candidate and is wrong: it is
# hand-maintained, so it can drift out of step with what is actually linked,
# and one copy of this installer once did. An exclusion with
# a missing name DELETES A LIVE TOOL'S LINK, so "someone will remember to
# update the array" is not a property this branch may depend on. What the
# installer just tried to do cannot be forgotten.
#
# Note this is deliberately NOT conditional on the target existing. A target
# absent from THIS checkout is an older branch, a bisect or a rename in
# flight, and the answer there is to keep the link, exactly as `link` itself
# does. Only a name the installer no longer mentions anywhere is prunable: a
# tool that was added and then retired still gets its link cleared.
MANAGED_LINKS=""

note_managed_link() {
  MANAGED_LINKS="$MANAGED_LINKS$1"$'\n'
  if (( MANIFEST )) && [[ "${2:-}" != "from-link" ]]; then
    printf 'managed\t-\t%s\n' "$1"
  fi
}

# Exact whole-line match, never a substring: `claude-usage-line` must not be
# protected by an entry for `claude-usage-line-something`. The leading newline
# on the subject is what lets the first recorded entry match.
is_managed_link() {
  case $'\n'"$MANAGED_LINKS" in
    *$'\n'"$1"$'\n'*) return 0 ;;
  esac
  return 1
}

# link <target> <linkname>
link() {
  local target="$1" linkname="$2"
  # Before any early return. The `missing in repo` path below is the one the
  # prune must not contradict, so recording after it would fix nothing.
  note_managed_link "$linkname" from-link
  if (( MANIFEST )); then
    printf 'link\t%s\t%s\n' "$target" "$linkname"
    return
  fi
  if [[ ! -e "$target" ]]; then
    warn "missing in repo, skipped: ${target#$REPO/}"
    MISSING_LINKS=$(( MISSING_LINKS + 1 ))
    REQUIRED_FAILURES+=("link source missing from the repo: ${target#$REPO/}")
    return
  fi
  if [[ -L "$linkname" && "$(readlink "$linkname")" == "$target" ]]; then
    ok "${linkname/#$HOME/$TILDE}"
    return
  fi
  if [[ -e "$linkname" && ! -L "$linkname" ]]; then
    warn "${linkname/#$HOME/$TILDE} exists and is a real file — not touching it"
    MISSING_LINKS=$(( MISSING_LINKS + 1 ))
    return
  fi
  act "${linkname/#$HOME/$TILDE} -> ${target#$REPO/}"
  MISSING_LINKS=$(( MISSING_LINKS + 1 ))
  LINKS_ABSENT=$(( LINKS_ABSENT + 1 ))
  (( CHECK )) || ln -sfn "$target" "$linkname"
}

echo "claude-code-toolkit installer  (repo: $REPO)"
(( CHECK )) && echo "  --check: reporting only, nothing will change"

echo
echo "Directories"
# Only the two directories this installer still links or writes into. Commands,
# agents, skills and the build-lens files ship in their profile's plugin, so
# nothing here creates or links into `~/.claude/{commands,agents,skills,build-lens}`.
#
# Worth knowing if that ever changes: a `link` into a missing directory is NOT
# a soft skip. `ln` errors, `set -e` aborts, and the install stops partway with
# the prune and the plugin step never reached. The prune loop tolerates the
# directory being absent (`[[ -d ]] || continue`); `link` does not, and that
# asymmetry turns a missing mkdir into a silently partial install.
for d in "$HOME/.local/bin" "$HOME/.config/claude-code-toolkit"; do
  if [[ -d "$d" ]]; then ok "${d/#$HOME/$TILDE}"
  else act "mkdir ${d/#$HOME/$TILDE}"; (( CHECK )) || mkdir -p "$d"; fi
done

echo
echo "Config"
if [[ -f "$CONFIG" ]]; then
  if python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CONFIG" 2>/dev/null; then
    ws=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("workspace_root") or "")' "$CONFIG" 2>/dev/null)
    if [[ -n "$ws" ]]; then
      ok "${CONFIG/#$HOME/$TILDE} (workspace_root: $ws)"
    else
      warn "${CONFIG/#$HOME/$TILDE} has no workspace_root — the danger guard will"
      warn "   trust only ~/.claude, ~/bin and the temp dirs. That is the safe"
      warn "   default, but set it if deletions in your project tree are routine."
    fi
  else
    warn "${CONFIG/#$HOME/$TILDE} is not valid JSON — every tool will fall back to"
    warn "   its defaults until you fix it. Nothing will crash, but nothing will"
    warn "   read your settings either."
  fi
else
  act "no config yet — running first-time setup"
fi

# Interactive setup: on --setup always, and on a plain install when there is no
# config yet. Never on --check, which must not change anything.
if (( ! CHECK )) && { (( SETUP )) || [[ ! -f "$CONFIG" ]]; }; then
  set +e
  # --setup is the explicit "redo the questions"; a plain install only gets here
  # with no config, and setup_config refuses to overwrite one without --replace.
  replace_flag=()
  (( SETUP )) && replace_flag=(--replace)
  python3 "$REPO/lib/setup_config.py" "$CONFIG" "$REPO" ${replace_flag[@]+"${replace_flag[@]}"}
  rc=$?
  set -e
  if (( rc == 2 )); then
    # No TTY. Fall back to the example so the install can still finish, but be
    # loud: the example sets no workspace root, which is safe but not yours.
    if [[ ! -f "$CONFIG" ]]; then
      cp "$REPO/config.example.json" "$CONFIG"
      warn "copied config.example.json to ${CONFIG/#$HOME/$TILDE} unedited —"
      warn "   no workspace root set. Run ./install.sh --setup in a terminal,"
      warn "   or edit it by hand, before relying on the guard."
    fi
  elif (( rc != 0 )); then
    warn "setup aborted — nothing was written. Re-run ./install.sh --setup."
    exit 1
  fi
fi

# Judged AFTER setup, not before: `--setup` can repair an unparseable config,
# and a failure recorded before the repair would outlive it.
if [[ -f "$CONFIG" ]] && ! python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$CONFIG" 2>/dev/null; then
  REQUIRED_FAILURES+=("config.json is not valid JSON")
fi

echo
echo "Config health"
# `--report` never writes, so it runs in both modes. `set +e` around it (not
# `|| true`) is what lets a crash still be SEEN: a report that dies must warn
# with its exit code, never read as "nothing to report" the way a swallowed
# failure would.
set +e
CONFIG_REPORT_OUTPUT="$(python3 "$REPO/lib/setup_config.py" "$CONFIG" "$REPO" --report)"
rc=$?
set -e
if (( rc != 0 )); then
  warn "config report failed (exit $rc) — run it by hand to see why:"
  warn "   python3 \"$REPO/lib/setup_config.py\" \"$CONFIG\" \"$REPO\" --report"
elif [[ -n "$CONFIG_REPORT_OUTPUT" ]]; then
  while IFS= read -r line; do
    warn "$line"
  done <<< "$CONFIG_REPORT_OUTPUT"
else
  ok "nothing declined, retired or unread in ${CONFIG/#$HOME/$TILDE}"
fi

# Separate from the report above: an enabled tooling digest with no map file is
# not a config-SCHEMA problem `--report` can see, it is the runtime gap the
# digest hook resolves every SessionStart. This asks the hook's own question
# (digest_lib.missing_source) rather than re-deriving it, so the two cannot
# disagree. A check that crashes says so with its exit code.
set +e
DIGEST_MISSING_OUTPUT="$(CLAUDE_TOOLKIT_CONFIG="$CONFIG" python3 -c 'import sys; sys.path[:0] = [sys.argv[1] + "/tools/tooling-digest", sys.argv[1] + "/lib"]; import digest_lib; p = digest_lib.missing_source(); print(p or "")' "$REPO")"
rc=$?
set -e
if (( rc != 0 )); then
  warn "tooling-digest check failed (exit $rc)"
elif [[ "$DIGEST_MISSING_OUTPUT" == "<unconfigured>" ]]; then
  warn "tooling-digest is on but no file is configured (set workspace_root or tooling_digest.path) — it will inject nothing"
elif [[ -n "$DIGEST_MISSING_OUTPUT" ]]; then
  warn "tooling-digest is on but $DIGEST_MISSING_OUTPUT does not exist — it will inject nothing until you create it (TOOLING.md.template) or disable it"
fi

EXECUTABLE_NAMES=(claude-danger-guard claude-worktree-guard claude-model-guard claude-distill-write-guard \
  claude-tooling-digest claude-memory-routing claude-memory-lint claude-tool-rules \
  claude-index-distill \
  claude-context-growth claude-context-lint claude-context-budget \
  claude-session-claim claude-session-release claude-secret-scan \
  claude-usage-line claude-usage-watch claude-usage-subagent claude-usage-postmortem \
  claude-usage-anchor claude-usage-dispatch \
  claude-title-session claude-title-backfill csearch \
  claude-thread-watch claude-thread-deliver \
  claude-caffeinate claude-spec-archive claude-base-pin claude-toolkit-integrity \
  claude-wrap-up-survey claude-backup claude-build-cost claude-build-route claude-merge-brief claude-review-gate claude-codex-review claude-harness-check claude-prompt-optimizer claude-skill-trigger \
  claude-setup-audit git-tidy claude-repo-freshness claude-local-ci)
# Must list every executable the `link` calls below place in ~/.local/bin.
# Nothing reads it at runtime: the prunes below walk the directories instead,
# because a retired tool's name leaves a hand-kept list at exactly the moment
# its leftover link needs pruning. It is kept so
# `TestExecutableNamesCoversEveryLink` (lib/tests/test_install_links_inventory.py)
# can catch it drifting from the `link` calls, in both directions. The test is
# the reason this list can be trusted; this comment is not.
#
# Only tools this checkout ships are linked. A port row that adds a tool adds
# its `link` line (and its name above) in the same change; there is no list of
# tools still to come, because `link` treats a missing target as a required
# failure and must keep doing so.

echo
echo "Executables -> ~/.local/bin"
link "$REPO/tools/danger-guard/claude-danger-guard"    "$HOME/.local/bin/claude-danger-guard"
link "$REPO/tools/worktree-guard/claude-worktree-guard" "$HOME/.local/bin/claude-worktree-guard"
link "$REPO/tools/model-guard/claude-model-guard"      "$HOME/.local/bin/claude-model-guard"
link "$REPO/tools/distill-guard/claude-distill-write-guard" "$HOME/.local/bin/claude-distill-write-guard"
link "$REPO/tools/git-tidy/git-tidy"                   "$HOME/.local/bin/git-tidy"
link "$REPO/tools/repo-freshness/claude-repo-freshness"  "$HOME/.local/bin/claude-repo-freshness"
link "$REPO/tools/tooling-digest/claude-tooling-digest"  "$HOME/.local/bin/claude-tooling-digest"
link "$REPO/tools/memory-routing/claude-memory-routing"  "$HOME/.local/bin/claude-memory-routing"
link "$REPO/tools/context-growth/claude-context-growth" "$HOME/.local/bin/claude-context-growth"
link "$REPO/tools/memory-lint/claude-memory-lint"        "$HOME/.local/bin/claude-memory-lint"
link "$REPO/tools/tool-rules/claude-tool-rules"          "$HOME/.local/bin/claude-tool-rules"
link "$REPO/tools/index-distill/claude-index-distill"  "$HOME/.local/bin/claude-index-distill"
# Also the pre-commit chain's PATH fallback: tools/secret-scan/git-pre-commit
# reaches it beside itself first.
link "$REPO/tools/context-lint/claude-context-lint"      "$HOME/.local/bin/claude-context-lint"
# Report only — no hook entry, deliberately. Every surface it prints is already
# watched by the tool that owns it; a second session-start voice saying the same
# thing is notice fatigue, not extra safety. See the executable's docstring.
link "$REPO/tools/context-budget/claude-context-budget"  "$HOME/.local/bin/claude-context-budget"
link "$REPO/tools/session-claim/claude-session-claim"   "$HOME/.local/bin/claude-session-claim"
link "$REPO/tools/session-claim/claude-session-release" "$HOME/.local/bin/claude-session-release"
# The status line runs claude-usage-line (claude/settings.host.json names this
# link), and claude-usage-anchor is run by hand. The four hook executables run
# from the plugin; their links are the commands an older install registered.
link "$REPO/tools/usage-watch/claude-usage-line"       "$HOME/.local/bin/claude-usage-line"
link "$REPO/tools/usage-watch/claude-usage-watch"      "$HOME/.local/bin/claude-usage-watch"
link "$REPO/tools/usage-watch/claude-usage-subagent"   "$HOME/.local/bin/claude-usage-subagent"
link "$REPO/tools/usage-watch/claude-usage-postmortem" "$HOME/.local/bin/claude-usage-postmortem"
link "$REPO/tools/usage-watch/claude-usage-anchor"     "$HOME/.local/bin/claude-usage-anchor"
link "$REPO/tools/usage-watch/claude-usage-dispatch"   "$HOME/.local/bin/claude-usage-dispatch"
link "$REPO/tools/session-titler/claude-title-session" "$HOME/.local/bin/claude-title-session"
link "$REPO/tools/session-titler/claude-title-backfill" "$HOME/.local/bin/claude-title-backfill"
link "$REPO/tools/session-titler/csearch"              "$HOME/.local/bin/csearch"
link "$REPO/tools/thread-handover/claude-thread-watch"   "$HOME/.local/bin/claude-thread-watch"
link "$REPO/tools/thread-handover/claude-thread-deliver" "$HOME/.local/bin/claude-thread-deliver"
link "$REPO/tools/secret-scan/claude-secret-scan"      "$HOME/.local/bin/claude-secret-scan"
link "$REPO/tools/local-ci/claude-local-ci" "$HOME/.local/bin/claude-local-ci"
link "$REPO/tools/caffeinate/claude-caffeinate"        "$HOME/.local/bin/claude-caffeinate"
# Also registered as a SessionStart hook in plugin-build/hooks/hooks.json, run as
# `--hook`, and it is not report-only:
# `--hook` flips stale status: live specs and archives declared shipped/superseded
# ones (gated by spec_archive.enabled and spec_archive.specs_dir; the flip is off
# by default, spec_archive.stale_flip_days). It stays runnable by hand for
# --flip-stale/--report/--apply, which is why the link is unconditional here
# rather than only reachable through the hook.
link "$REPO/tools/spec-archive/claude-spec-archive"    "$HOME/.local/bin/claude-spec-archive"
link "$REPO/tools/base-pin/claude-base-pin"            "$HOME/.local/bin/claude-base-pin"
link "$REPO/tools/toolkit-integrity/claude-toolkit-integrity" "$HOME/.local/bin/claude-toolkit-integrity"
link "$REPO/tools/wrap-up/claude-wrap-up-survey"       "$HOME/.local/bin/claude-wrap-up-survey"
link "$REPO/tools/backup/claude-backup"                "$HOME/.local/bin/claude-backup"
# The manual report `/build` runs at Phase 5. Its recorder,
# claude-build-cost-record, runs from the toolkit-build plugin's SessionEnd hook
# and needs no link.
link "$REPO/tools/build-cost/claude-build-cost"        "$HOME/.local/bin/claude-build-cost"
link "$REPO/tools/build-route/claude-build-route"      "$HOME/.local/bin/claude-build-route"
# Builds the blinded brief every /build review gate hands its reviewer.
link "$REPO/tools/merge-brief/claude-merge-brief"      "$HOME/.local/bin/claude-merge-brief"
# Runs a /build review gate through the Agent tool where Workflow is absent.
link "$REPO/tools/build-lens/claude-review-gate"       "$HOME/.local/bin/claude-review-gate"
# The optional Codex reviewer `/build` dispatches under `full-power`.
link "$REPO/tools/codex-review/claude-codex-review"    "$HOME/.local/bin/claude-codex-review"
# Measures which skill a prompt routes to (skill authoring); run by hand.
link "$REPO/tools/skill-trigger/claude-skill-trigger"  "$HOME/.local/bin/claude-skill-trigger"
link "$REPO/tools/harness-check/claude-harness-check"  "$HOME/.local/bin/claude-harness-check"
# The reminder the build plugin's PreToolUse hook shows before any subagent
# dispatch; off unless prompt_optimizer.enabled is true, and silent without the
# skill.
link "$REPO/tools/prompt-optimizer/claude-prompt-optimizer" "$HOME/.local/bin/claude-prompt-optimizer"
# Run by hand and by /onboard, never a hook: it reads `install.sh --manifest`
# to report what this installer manages versus what is on the machine.
link "$REPO/tools/setup-audit/claude-setup-audit"      "$HOME/.local/bin/claude-setup-audit"

# Skipped under --links-only: it reads ~/.gitconfig and reports on an alias, not
# on a symlink, and a flag named links-only that prints other findings is a flag
# whose output you have to re-read to trust.
if (( ! LINKS_ONLY )); then
echo
echo "Git tidy alias"
# An executable `git-tidy` on PATH takes precedence over a same-named
# `alias.tidy` — git ignores an alias that shadows an existing command, and a
# PATH `git-<cmd>` counts as one (confirmed empirically). So the moment
# ~/.local/bin/git-tidy is linked above AND ~/.local/bin is on PATH, `git tidy`
# runs it with no ~/.gitconfig edit needed — and any pre-existing alias.tidy
# becomes dead config: still present, never running. That supersession claim
# only holds if ~/.local/bin is actually on PATH, so it's gated on that check
# rather than asserted outright — a false "it's handled" is worse than no
# claim at all, because it is exactly what stops someone from investigating
# further. Report; don't touch ~/.gitconfig ourselves.
existing_alias="$(git config --global --get alias.tidy 2>/dev/null || true)"
if [[ -n "$existing_alias" ]]; then
  if (( ON_LOCAL_BIN_PATH )); then
    warn "~/.gitconfig has alias.tidy = '$existing_alias' — superseded now that"
    warn "   ~/.local/bin/git-tidy is linked, and will never run again. Remove it with:"
    warn "   git config --global --unset alias.tidy"
  else
    warn "~/.gitconfig has alias.tidy = '$existing_alias', and ~/.local/bin is NOT"
    warn "   on your PATH — so it does NOT supersede this alias yet. 'git tidy' will"
    warn "   still run that alias, not this tool. Fix your PATH (see below), or run"
    warn "   ~/.local/bin/git-tidy directly in the meantime."
  fi
else
  ok "no alias.tidy to supersede"
fi
fi

if (( MANIFEST )); then exit 0; fi

# `--check` reports an absent link as a failure, apart from `--links-only`
# (which has its own exit status below). From a linked worktree every link reads
# as absent whether or not it is, so there it is a note, never a failure.
if (( CHECK && ! LINKS_ONLY && LINKS_ABSENT > 0 )); then
  if is_linked_worktree "$REPO"; then
    echo
    warn "run from a LINKED WORKTREE ($REPO), so every link reads as absent"
    warn "   whether or not it is. The real links point at the primary checkout."
  else
    REQUIRED_FAILURES+=("$LINKS_ABSENT link(s) not in place — run ./install.sh")
  fi
fi

# --- end of the link calls -------------------------------------------------
#
# `--links-only` stops here, and the placement is the whole design. Everything
# above is the `link` calls themselves; everything below is the plugin step,
# the prune loops, the PATH advice and the test runner -- and that last one is
# the entire several minutes a full `--check` costs, of which the link work is
# under a second. No suite count here on purpose: this file discovers them with
# `find`, the documented loop searches fewer directories, and a number written
# beside either was already stale on the day it landed.
#
# This deliberately REUSES the link calls rather than reading a list of them.
# `toolkit_integrity_lib.py`'s docstring warns that "the inventory is never the
# ground truth", and that warning is about `EXECUTABLE_NAMES` -- a second,
# hand-maintained declaration that has already drifted from these calls. A
# checker that parsed or copied this list would recreate exactly that hazard.
# Running the calls in report mode cannot drift from them, because it is them.
#
# The exit status is the point: this is meant to be usable as a check, and a
# report nobody can branch on is a report nobody runs twice.
if (( LINKS_ONLY )); then
  echo
  if (( MISSING_LINKS )); then
    # From a linked worktree EVERY link reads as absent, because `link` compares
    # the existing symlink against this $REPO and the real ones point at the
    # primary checkout. On a fully installed machine that is a clean exit 1 with
    # every link listed — indistinguishable from a machine that never installed. The
    # `--check` equivalent of this noise is documented as "the symptom, not real
    # drift", but this flag turns it into an exit status, and this repo's own
    # rules push concurrent sessions into worktrees. So say it here rather than
    # relying on the reader knowing: a wrong verdict nobody can attribute is how
    # a one-second check earns a reputation for crying wolf.
    if is_linked_worktree "$REPO"; then
      warn "run from a LINKED WORKTREE ($REPO), so every link below reads as"
      warn "   absent whether or not it is. The real links point at the primary"
      warn "   checkout. Re-run this there — this result says nothing."
      exit 1
    fi
    warn "$MISSING_LINKS link(s) not in place — run ./install.sh from the"
    warn "   primary checkout to create them."
    exit 1
  fi
  ok "every link is in place"
  exit 0
fi

echo
echo "Secret scanning (pre-commit)"
# Report only: install.sh never sets core.hooksPath (scripts/setup-git.sh does,
# because it is a global git setting with machine-wide effects). What this
# section exists to prevent is reading the ~/.local/bin/claude-secret-scan link
# above as protection. That link is for running the scanner by hand; commits are
# scanned only when global core.hooksPath holds a pre-commit that resolves to
# this checkout's tools/secret-scan/git-pre-commit. Anything else is reported as
# not active, including a core.hooksPath that is yours: we cannot see whether
# your own hook calls the scanner, so we never claim it does.
SECRET_HOOKS_DIR="$HOME/.config/claude-code-toolkit/git-hooks"
secret_hooks="$(git config --global --get core.hooksPath 2>/dev/null || true)"
secret_hooks="${secret_hooks/#\~/$HOME}"
# The state comes from lib/secret_scan_status.py, the same probe /onboard's
# verify reads, so the installer and the onboarding summary never disagree.
secret_precommit_state="$(python3 "$REPO/lib/secret_scan_status.py" "$REPO")" \
  || secret_precommit_state="unknown"
case "$secret_precommit_state" in
  active)
    ok "active: commits on this machine are scanned (core.hooksPath -> this checkout's git-pre-commit)" ;;
  foreign)
    warn "NOT active from this toolkit: core.hooksPath is yours (${secret_hooks/#$HOME/$TILDE})."
    warn "   Commits are scanned only if your own pre-commit runs claude-secret-scan."
    warn "   \"$REPO/scripts/setup-git.sh\" leaves your hooks path alone and prints the line to add." ;;
  unset)
    warn "NOT active: commits are not scanned for credentials (no global core.hooksPath)."
    warn "   The claude-secret-scan link above is for running it by hand. To scan"
    warn "   every commit, run: \"$REPO/scripts/setup-git.sh\"" ;;
  *)
    warn "NOT active: ${SECRET_HOOKS_DIR/#$HOME/$TILDE}/pre-commit is missing, broken or"
    warn "   not this checkout's. Re-run \"$REPO/scripts/setup-git.sh\" from the primary checkout." ;;
esac

# An enabled reminder with no prompt-optimizer skill installed is silent by
# design, so this is the one place that state is visible. It only reports:
# it never adds to REQUIRED_FAILURES and never changes the exit status.
reminder_status="$(CLAUDE_TOOLKIT_CONFIG="$CONFIG" python3 "$REPO/tools/prompt-optimizer/prompt_optimizer_lib.py" status 2>/dev/null || true)"
case "$reminder_status" in
  on:*)    echo; echo "Prompt reminder"; ok "${reminder_status#on: }" ;;
  inert:*) echo; echo "Prompt reminder"; warn "${reminder_status#inert: }" ;;
esac

echo
echo "Hooks -> profile plugins"
# The hooks ship in each profile plugin (hooks/hooks.json in plugin/ for core,
# plugin-*/ for the rest; `toolkit-core` is core), which
# this repo's own checkout serves as a local directory marketplace. install.sh
# registers them through the `claude` CLI and removes ONLY the hook commands the
# old settings template ever wrote (claude/legacy-hook-commands.json, exact
# strings, never a pattern). Anything else in `hooks` is yours and stays.
SETTINGS="$HOME/.claude/settings.json"
HAVE_CLAUDE=1
command -v claude >/dev/null 2>&1 || HAVE_CLAUDE=0
if (( ! HAVE_CLAUDE )); then
  warn "claude CLI not on PATH — hooks NOT installed; your existing settings hooks were left as they are"
  # Exit now, not at the end: the ~/bin and dangling-link prunes below delete
  # links, and they must not run on a machine whose hooks we could not install.
  # --check changes nothing, so it carries on and reports what it can.
  (( CHECK )) || exit 1
fi
# Snapshot BEFORE any CLI call. `claude plugin marketplace add` writes
# extraKnownMarketplaces/enabledPlugins into settings.json straight away, so a
# snapshot taken later would capture the CLI's edits instead of the pre-run
# file. Immutable: written once, never overwritten by a later run.
SNAPSHOT="$SETTINGS.pre-claude-code-toolkit.bak"
if (( ! CHECK )) && [[ -f "$SETTINGS" && ! -e "$SNAPSHOT" ]]; then
  # Exit 3 = it appeared meanwhile (kept as is). Any other failure stops the
  # run, as the `cp -p` this replaces did under `set -e`.
  snapshot_rc=0
  REPO="$REPO" python3 -c '
import os, sys
sys.path.insert(0, os.path.join(os.environ["REPO"], "lib"))
import settings_io
settings, snapshot = sys.argv[1], sys.argv[2]
with open(settings, "rb") as handle:
    data = handle.read()
sys.exit(0 if settings_io.create_atomic(snapshot, data, stat_from=settings) else 3)
' "$SETTINGS" "$SNAPSHOT" || snapshot_rc=$?
  if (( snapshot_rc == 0 )); then
    act "snapshot of settings.json saved to $(basename "$SNAPSHOT")"
  elif (( snapshot_rc != 3 )); then
    warn "could not save the snapshot of settings.json (exit $snapshot_rc) — nothing was changed"
    exit 1
  fi
fi
take_timed_snapshot
UNPROTECTED=0
set +e
CHECK="$CHECK" HAVE_CLAUDE="$HAVE_CLAUDE" SETTINGS="$SETTINGS" REPO="$REPO" python3 - <<'PY'
import json, os, shutil, subprocess, sys

check = os.environ["CHECK"] == "1"
have_claude = os.environ["HAVE_CLAUDE"] == "1"
settings_path = os.environ["SETTINGS"]
repo = os.environ["REPO"]
home = os.path.expanduser("~")

sys.path.insert(0, os.path.join(repo, "lib"))
import hook_manifest
import profiles

OK = "  \033[32mOK\033[0m"
ACT = "  \033[33m->\033[0m"
BAD = "  \033[31m!!\033[0m"
repo_real = os.path.realpath(repo)

# The profile selection decides which plugins every step below handles. A broken
# one stops the run before any CLI call: falling back to a default would install
# a different plugin set than the one asked for.
selected_names, selection_error, defaulted = profiles.selected(repo)
if selection_error:
    print("%s profiles: %s" % (BAD, selection_error))
    sys.exit(1)
try:
    registry = profiles.load_registry(repo)
except profiles.RegistryError as exc:  # selected() just read it, so not expected
    print("%s profiles: %s" % (BAD, exc))
    sys.exit(1)
selected_ids = [profiles.plugin_id(registry[name]) for name in selected_names]
if defaulted:
    print("%s profiles: %s (default — set \"profiles\" in your toolkit config to choose)"
          % (ACT, ", ".join(selected_names)))
else:
    print("%s profiles: %s" % (OK, ", ".join(selected_names)))
    for left_out in profiles.missing_defaults(registry, selected_names, defaulted):
        print("%s profiles: your list leaves out %s, a default profile — a \"profiles\" "
              "list installs core plus only what it names (and what they require), so %s "
              "is not installed on a machine that lacks it, and one the last install put in "
              "place is uninstalled (one you enabled yourself is left as is). Add \"%s\" "
              "to keep it (%s)"
              % (ACT, left_out, left_out, left_out, registry[left_out]["summary"]))


def fail(message):
    print("%s %s" % (BAD, message))
    sys.exit(1)


def cli(*args):
    """Run `claude <args>`; return (ok, stdout). Never raises."""
    try:
        proc = subprocess.run(["claude"] + list(args), capture_output=True,
                              universal_newlines=True, stdin=subprocess.DEVNULL,
                              timeout=180)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "claude %s: %s" % (" ".join(args), exc)
    if proc.returncode != 0:
        return False, "claude %s exited %d" % (" ".join(args), proc.returncode)
    return True, proc.stdout


def cli_list(*args):
    """A `--json` listing as a list of dicts, or None after reporting why."""
    ok, out = cli(*args)
    name = "claude " + " ".join(args)
    if not ok:
        print("%s %s" % (BAD, out))
        return None
    try:
        data = json.loads(out)
    except ValueError:
        print("%s %s printed unparseable JSON" % (BAD, name))
        return None
    if not isinstance(data, list):
        print("%s %s did not print a JSON array" % (BAD, name))
        return None
    return [entry for entry in data if isinstance(entry, dict)]


def market_entry(markets):
    for entry in markets or []:
        if entry.get("name") == hook_manifest.MARKETPLACE:
            return entry
    return None


def market_is_here(entry):
    return (entry is not None and entry.get("source") == "directory"
            and isinstance(entry.get("path"), str)
            and os.path.realpath(entry["path"]) == repo_real)


def plugin_entry(plugins, pid):
    # User scope only: `plugin list --json` also reports project-scoped installs,
    # and those put hooks in one project, not in every session on this machine.
    for entry in plugins or []:
        if (entry.get("id") == pid
                and entry.get("scope") == "user"):
            return entry
    return None


def run_or_fail(*args):
    ok, out = cli(*args)
    if not ok:
        fail(out)


def checkout_commit():
    """The checkout's HEAD as 12 hex characters, or None. For a plugin served
    from a directory marketplace with no manifest `version`, this is the version
    `claude plugin list` reports, read once when the plugin is installed."""
    env = dict(os.environ)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    try:
        proc = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True,
                              universal_newlines=True, env=env, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    head = proc.stdout.strip()
    if proc.returncode != 0 or not is_commit(head[:12]):
        return None
    return head[:12]


def is_commit(value):
    return (isinstance(value, str) and len(value) == 12
            and all(char in "0123456789abcdef" for char in value))


head12 = checkout_commit()


def stale_version(entry):
    """The commit `entry` shows when it is not the checkout's, else None.

    The hooks always run from the checkout itself, but `claude plugin list`
    keeps showing the commit read at install time until `claude plugin update`
    refreshes it, so after an --update or --rollback the obvious check says the
    old version is still installed. Only a 12-hex version is compared: a CLI
    that reports no version, or a manifest version, says nothing about the
    checkout."""
    shown = entry.get("version") if entry else None
    if head12 is not None and is_commit(shown) and shown != head12:
        return shown
    return None


# What the deselected-profile step removed this run, for the end-state check.
uninstalled_now = []
# The plugins this run refreshed, for the end-state check.
refreshed_now = []


def refresh_if_stale(entry, pid):
    """Refresh one installed plugin whose listed version is not the checkout's.
    A refresh that fails is reported and never fails the run: the hooks already
    run from the checkout, so only the version `claude plugin list` shows is wrong."""
    shown = stale_version(entry)
    if shown is None:
        return
    if check:
        print("%s plugin %s shows %s, the checkout is at %s (would refresh)"
              % (ACT, pid, shown, head12))
        return
    print("%s refreshing plugin %s (shows %s, the checkout is at %s)" % (ACT, pid, shown, head12))
    ok, out = cli("plugin", "update", pid, "--scope", "user")
    if ok:
        refreshed_now.append(pid)
    else:
        print("%s %s — the hooks still run from the checkout, but `claude plugin list` "
              "shows %s until you run: claude plugin update %s --scope user"
              % (BAD, out, shown, pid))


def uninstall_deselected(plugins, markets):
    """Uninstall the plugin of each profile the LAST install selected (its record)
    and this one does not. A plugin no record selected was enabled by hand and is
    never touched; one that something staying installed still needs is kept.
    Returns the plugin ids this step handled, so the caller does not also report
    them as enabled by hand. A refused uninstall fails the run before the record
    is rewritten, so the next run retries it."""
    import install_record
    import plugin_deps
    record = install_record.load(install_record.record_path(home))
    previous = record.get("profiles") if record else None
    if not isinstance(previous, list):
        return set()
    for name in previous:
        if name not in registry:
            print("%s profile %s from your last install no longer exists; its plugin, "
                  "if any, was left as is" % (OK, name))
    dropped = {}
    for name in registry:
        if name in previous and name not in selected_names:
            pid = profiles.plugin_id(registry[name])
            if plugin_entry(plugins, pid) is not None:
                dropped[pid] = name
            elif any(e.get("id") == pid for e in plugins):
                print("%s plugin %s: profile %s is no longer selected, but it is installed "
                      "at project scope only — left as is" % (OK, pid, name))
    if not dropped:
        return set()

    marketplace = hook_manifest.MARKETPLACE
    errors = []
    try:
        toolkit_deps = plugin_deps.toolkit_dependencies(repo)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        toolkit_deps = {}
        errors.append("cannot read the toolkit's plugin manifests: %s" % exc)
    other_deps, other_errors = plugin_deps.installed_dependencies(home, plugins, markets, marketplace)
    errors.extend(other_errors)

    def deps_of(key):
        if key.endswith("@" + marketplace):
            return toolkit_deps.get(key.partition("@")[0], [])
        return other_deps.get(key, [])

    kept = {}
    if errors:
        # Unproven is not safe: a plugin we could not read might need any of them.
        for pid in dropped:
            kept[pid] = errors[0]
        order = []
    else:
        # `plugins` was listed before this run installed anything, so the
        # selected plugins join explicitly: one installed just now may need a
        # plugin the last install selected.
        stays = {e["id"] for e in plugins
                 if isinstance(e.get("id"), str) and e["id"] not in dropped} | set(selected_ids)
        order, kept = plugin_deps.plan_removal(dropped, stays, deps_of)
    for pid in sorted(kept):
        print("%s kept %s: %s" % (OK, pid, kept[pid]))

    for pid in order:
        if check:
            print("%s plugin %s: profile %s is no longer selected (would uninstall)"
                  % (ACT, pid, dropped[pid]))
        else:
            print("%s uninstalling %s (profile %s is no longer selected)" % (ACT, pid, dropped[pid]))
            run_or_fail("plugin", "uninstall", pid, "--scope", "user")
            uninstalled_now.append(pid)
    return set(dropped)


def report_prerequisites(plugins):
    """Say whether each selected profile's outside prerequisite is enabled at
    user scope. A report only: never installs, enables or changes the exit status."""
    for pid, why, needing in profiles.prerequisites(registry, selected_names):
        who = ", ".join(needing)
        found = plugin_entry(plugins, pid)
        if found is not None and found.get("enabled") is True:
            print("%s prerequisite %s (for %s)" % (OK, pid, who))
        elif found is None:
            print("%s %s needs %s (%s), which is not installed. Install it: "
                  "claude plugin install %s --scope user" % (BAD, who, pid, why, pid))
        else:
            print("%s %s needs %s (%s), which is disabled. Enable it: "
                  "claude plugin enable %s --scope user" % (BAD, who, pid, why, pid))


# --- Steps 2-4: register and enable the plugin through the CLI -------------
if have_claude:
    markets = cli_list("plugin", "marketplace", "list", "--json")
    if markets is None:
        if not check:
            sys.exit(1)
    else:
        entry = market_entry(markets)
        if entry is None:
            if check:
                print("%s marketplace %s not registered (would add %s)"
                      % (ACT, hook_manifest.MARKETPLACE, repo))
            else:
                print("%s registering marketplace %s -> %s"
                      % (ACT, hook_manifest.MARKETPLACE, repo))
                run_or_fail("plugin", "marketplace", "add", repo)
        elif not market_is_here(entry):
            where = "%s (%s)" % (entry.get("path"), entry.get("source"))
            if check:
                print("%s marketplace %s points at %s, not %s (would re-register)"
                      % (BAD, hook_manifest.MARKETPLACE, where, repo))
            else:
                print("%s marketplace %s points at %s, not %s — re-registering"
                      % (ACT, hook_manifest.MARKETPLACE, where, repo))
                run_or_fail("plugin", "marketplace", "remove", hook_manifest.MARKETPLACE)
                run_or_fail("plugin", "marketplace", "add", repo)
        else:
            print("%s marketplace %s" % (OK, hook_manifest.MARKETPLACE))

    plugins = cli_list("plugin", "list", "--json")
    if plugins is None:
        if not check:
            sys.exit(1)
    else:
        for pid in selected_ids:
            entry = plugin_entry(plugins, pid)
            if entry is None:
                if check:
                    print("%s plugin %s not installed (would install)" % (ACT, pid))
                else:
                    print("%s installing plugin %s" % (ACT, pid))
                    run_or_fail("plugin", "install", pid, "--scope", "user")
            elif entry.get("enabled") is not True:
                if check:
                    print("%s plugin %s is disabled (would enable)" % (BAD, pid))
                else:
                    print("%s enabling plugin %s" % (ACT, pid))
                    run_or_fail("plugin", "enable", pid, "--scope", "user")
                refresh_if_stale(entry, pid)
            else:
                print("%s plugin %s enabled" % (OK, pid))
                refresh_if_stale(entry, pid)
        deselected_handled = uninstall_deselected(plugins, markets or [])
        # Another toolkit plugin the user enabled by hand is theirs: say so, change nothing.
        for entry in plugins:
            other = entry.get("id")
            if (isinstance(other, str) and other.endswith("@" + hook_manifest.MARKETPLACE)
                    and entry.get("scope") == "user" and entry.get("enabled") is True
                    and other not in selected_ids and other not in deselected_handled):
                print("%s %s enabled outside your profiles — left as is" % (OK, other))
        if check:
            report_prerequisites(plugins)

    if not check:
        # Trust the CLI's own report of the end state, not its exit code: a CLI
        # that prints success and changes nothing leaves hooks nowhere.
        markets = cli_list("plugin", "marketplace", "list", "--json")
        plugins = cli_list("plugin", "list", "--json")
        if markets is None or plugins is None:
            sys.exit(1)
        problems = []
        if not market_is_here(market_entry(markets)):
            problems.append("marketplace %s is not a directory marketplace at %s"
                            % (hook_manifest.MARKETPLACE, repo))
        for pid in selected_ids:
            final = plugin_entry(plugins, pid)
            if final is None or final.get("enabled") is not True:
                problems.append("plugin %s is not installed and enabled" % pid)
        for pid in uninstalled_now:
            if plugin_entry(plugins, pid) is not None:
                problems.append("plugin %s is still installed after uninstalling it" % pid)
        if problems:
            for problem in problems:
                print("%s %s" % (BAD, problem))
            fail("hooks NOT installed; your existing settings hooks were left as they are")
        # A refresh the CLI reported as done but that left the old version
        # showing: say so, as for a refused one (the hooks are fine either way).
        for pid in refreshed_now:
            shown = stale_version(plugin_entry(plugins, pid))
            if shown is not None:
                print("%s plugin %s still shows %s after refreshing it, not the checkout's %s; "
                      "the hooks run from the checkout either way" % (BAD, pid, shown, head12))
        report_prerequisites(plugins)

# --- Step 5: remove only the hook commands the old template wrote -----------
try:
    with open(settings_path) as handle:
        settings = json.load(handle)
    existed = True
except FileNotFoundError:
    settings, existed = {}, False
except json.JSONDecodeError as exc:
    fail("%s is not valid JSON (%s) — refusing to touch it" % (settings_path, exc))
if not isinstance(settings, dict):
    fail("%s is not a JSON object — refusing to touch it" % settings_path)

try:
    legacy = hook_manifest.legacy_commands(repo, home)
except (OSError, ValueError, KeyError) as exc:
    fail("cannot read claude/legacy-hook-commands.json: %s" % exc)

changed = False
current = settings.get("hooks")
if isinstance(current, dict):
    new_hooks = {}
    any_removed = False
    for event, blocks in current.items():
        if not isinstance(blocks, list):
            new_hooks[event] = blocks
            continue
        new_blocks, event_removed, event_foreign = [], [], []
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("hooks"), list):
                new_blocks.append(block)    # malformed: kept, never "fixed"
                continue
            kept, dropped = [], []
            for handler in block["hooks"]:
                command = handler.get("command") if isinstance(handler, dict) else None
                if isinstance(command, str) and command in legacy:
                    dropped.append(command)
                else:
                    kept.append(handler)
            event_removed.extend(dropped)
            event_foreign.extend(
                h["command"] if isinstance(h, dict) and isinstance(h.get("command"), str)
                else json.dumps(h) for h in kept)
            if not dropped:
                new_blocks.append(block)
            elif kept:
                replaced = dict(block)
                replaced["hooks"] = kept
                new_blocks.append(replaced)
        for command in event_removed:
            print("%s %s %s" % (ACT, "would remove" if check else "removed", command))
        if event_removed:
            any_removed = True
            for command in event_foreign:
                print("%s kept %s: %s" % (OK, event, command))
            if new_blocks:
                new_hooks[event] = new_blocks
        else:
            new_hooks[event] = blocks
    if any_removed:
        changed = True
        if new_hooks:
            settings["hooks"] = new_hooks
        else:
            del settings["hooks"]
if not changed:
    print("%s hooks: nothing to remove" % OK)

# --- Step 6: statusLine, never clobbering one that is already yours ---------
try:
    wanted_status_line = hook_manifest.wanted_status_line(repo, home)
except (OSError, ValueError) as exc:
    fail("cannot read claude/settings.host.json: %s" % exc)
current_status_line = settings.get("statusLine")
if wanted_status_line is None:
    pass
elif current_status_line == wanted_status_line:
    print("%s statusLine" % OK)
elif current_status_line is None:
    print("%s statusLine (adding)" % ACT)
    if not check:
        settings["statusLine"] = wanted_status_line
        changed = True
else:
    print("%s statusLine is yours and was left alone — usage-watch needs its own\n"
          "     status line to write the budget cache. Until your statusLine command\n"
          "     also passes its stdin to ~/.local/bin/claude-usage-line, the usage hooks\n"
          "     will report the budget as UNKNOWN once per session rather than going quiet."
          % BAD)

# Write only when something changed. The pre-run backup was taken in the shell
# before any CLI call, so no second backup here.
if changed and not check:
    import settings_io
    settings_io.write_json_atomic(settings_path, settings, ensure_ascii=True)
    print("%s updated %s" % (ACT, os.path.basename(settings_path)))

# --- Step 7 ------------------------------------------------------------------
print("  Running sessions keep their old hooks until /reload-plugins or a new session.")

# --- Step 8: the ONE "protected" verdict -------------------------------------
# Used by --check and by the end of a real install, and it re-reads everything
# from scratch (CLI state, settings on disk, managed files) rather than trusting
# what the steps above believe they did. Every failing condition prints its own
# red line; none of them writes anything. The toolkit never sets
# disableAllHooks or allowManagedHooksOnly: both are somebody else's decision.
def managed_files():
    base = os.environ.get("CLAUDE_TOOLKIT_MANAGED_DIR") or "/Library/Application Support/ClaudeCode"
    paths = [os.path.join(base, "managed-settings.json")]
    drop_in = os.path.join(base, "managed-settings.d")
    if os.path.isdir(drop_in):
        paths += sorted(os.path.join(drop_in, n) for n in os.listdir(drop_in)
                        if n.endswith(".json"))
    found = []
    for path in paths:
        try:
            with open(path) as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            found.append((path, data))
    return found


def protected():
    """True when this machine's hooks will actually run. Prints each reason not."""
    problems = []

    def bad(message):
        problems.append(message)
        print("%s %s" % (BAD, message))

    # 1-3: the CLI and what it reports.
    if not shutil.which("claude"):
        bad("claude CLI not on PATH: the toolkit-core plugin cannot be checked or installed")
    else:
        markets = cli_list("plugin", "marketplace", "list", "--json")
        plugins = cli_list("plugin", "list", "--json")
        if markets is None or plugins is None:
            bad("could not read the plugin state from the claude CLI")
        else:
            if not market_is_here(market_entry(markets)):
                bad("marketplace %s is not a directory marketplace at %s"
                    % (hook_manifest.MARKETPLACE, repo))
            for pid in selected_ids:
                found = plugin_entry(plugins, pid)
                if found is None or found.get("enabled") is not True:
                    bad("plugin %s is not installed and enabled" % pid)

    # 4 / 4b: settings that suppress hooks. Reported, never changed.
    try:
        with open(settings_path) as handle:
            user_settings = json.load(handle)
    except (OSError, ValueError):
        user_settings = {}
    if isinstance(user_settings, dict) and user_settings.get("disableAllHooks") is True:
        bad("disableAllHooks is true in %s: every hook, this plugin's included, is "
            "suppressed. The toolkit will not change that setting; remove it yourself."
            % settings_path)
    managed = managed_files()
    for path, data in managed:
        if data.get("disableAllHooks") is True:
            bad("disableAllHooks is true in managed settings %s: every hook is "
                "suppressed. The toolkit will not change that setting." % path)
    if any(data.get("allowManagedHooksOnly") is True for _, data in managed):
        for pid in selected_ids:
            managed_plugin = any(
                isinstance(data.get("enabledPlugins"), dict)
                and data["enabledPlugins"].get(pid) is True
                for _, data in managed)
            if not managed_plugin:
                bad("allowManagedHooksOnly is true in managed settings, and no managed "
                    "file enables %s, so user-installed plugin hooks are ignored. The "
                    "toolkit will not change that setting." % pid)
    print("%s a project-level disableAllHooks cannot be checked from here" % ACT)

    # 5: handlers from the old template still in settings.json.
    try:
        legacy_set = hook_manifest.legacy_commands(repo, home)
    except (OSError, ValueError, KeyError) as exc:
        legacy_set = set()
        bad("cannot read claude/legacy-hook-commands.json: %s" % exc)
    hooks = user_settings.get("hooks") if isinstance(user_settings, dict) else None
    for blocks in (hooks.values() if isinstance(hooks, dict) else []):
        for block in blocks if isinstance(blocks, list) else []:
            inner = block.get("hooks") if isinstance(block, dict) else None
            for handler in inner if isinstance(inner, list) else []:
                command = handler.get("command") if isinstance(handler, dict) else None
                if isinstance(command, str) and command in legacy_set:
                    bad("legacy hook still in %s: %s" % (settings_path, command))

    # 6: the plugin's own manifest and what it points at.
    # resolve() re-reads the registry per call and can raise, so the whole loop
    # is inside the try.
    try:
        for row in hook_manifest.plugin_registrations(repo):
            if row["profile"] not in selected_names:
                continue
            target = hook_manifest.resolve(repo, row["relpath"], row["profile"])
            if not os.path.exists(target):
                bad("%s hook %s is missing: %s" % (row["event"], row["exe"], target))
            elif not os.access(target, os.X_OK):
                bad("%s hook %s is not executable: %s" % (row["event"], row["exe"], target))
    except hook_manifest.ManifestError as exc:
        bad("hook manifest: %s" % exc)
    return not problems


if protected():
    print("%s hooks are protected" % OK)
else:
    sys.exit(3)    # 3, not 1: bash tells "not protected" from an abort above
PY
UNPROTECTED_RC=$?
set -e
if (( UNPROTECTED_RC == 3 )); then
  UNPROTECTED=1
elif (( UNPROTECTED_RC != 0 )); then
  exit "$UNPROTECTED_RC"
fi

# Live hook line: informational, never changes the exit status.
if [[ -n "${CLAUDE_CODE_SESSION_ID:-}" ]]; then
  if LIVE_OUT=$(live_verdict "$CLAUDE_CODE_SESSION_ID" 2>/dev/null); then
    if [[ "${LIVE_OUT%%$'\t'*}" == "proven" ]]; then
      ok "Live: ${LIVE_OUT#*$'\t'}"
    else
      act "Live: ${LIVE_OUT#*$'\t'}"
    fi
  else
    act "Live: live check unavailable (lib/hook_heartbeat.py is missing or failed)"
  fi
else
  act "Live: not checked from a terminal — ask Claude to run ./install.sh --verify-live"
fi

# Guards line: informational, never changes the exit status.
if GUARDS_OUT=$(guards_verdict 2>/dev/null); then
  if [[ "${GUARDS_OUT%%$'\t'*}" == "ok" ]]; then
    ok "Guards: ${GUARDS_OUT#*$'\t'}"
  else
    act "Guards: ${GUARDS_OUT#*$'\t'}"
  fi
else
  act "Guards: check unavailable (lib/hook_heartbeat.py is missing or failed)"
fi

# Readiness block: informational, never changes the exit status.
READINESS_RC=0
READINESS_OUT=$(readiness_report 2>/dev/null) || READINESS_RC=$?
if (( READINESS_RC == 0 || READINESS_RC == 1 )) && [[ -n "$READINESS_OUT" ]]; then
  echo "$READINESS_OUT"
else
  act "Readiness: check unavailable (lib/readiness.py is missing or failed)"
fi

# Both prune loops below (this one and "Links to files this repo no longer
# has") test `readlink ... == "$REPO"/*` to decide what they own, and both
# delete. An empty $REPO turns that pattern into `/*`, which matches every
# absolute path, so either loop would delete symlinks it does not own.
# Nothing in either loop enforces that $REPO is set. Today that is closed by
# `set -e` aborting on the failed `cd` where REPO is computed — a safety
# property living at a distance, in another mechanism's error handling, in a
# line nobody maintaining either loop will read. That is too thin a thread
# for two branches that delete.
#
# Refuse rather than skip: a prune that quietly does nothing is
# indistinguishable from a prune that found nothing.
#
# The same latent hole was confirmed the same day in `lib/worktree_detect.sh`,
# where an empty $REPO makes the detector approve an install instead. That
# guard validates its own argument for the same reason. Deliberately NOT
# fixed once at install.sh:14 — `scripts/setup-git.sh` computes its own root
# and would not be covered, while an assertion there would look like it was.
#
# Checked here, once, before BOTH loops rather than only before the second —
# the first loop below has the identical ownership test and the identical
# failure mode, so guarding only the second would leave the first open.
if [[ -z "${REPO:-}" ]]; then
  echo "refusing to prune stale links: \$REPO is empty." >&2
  echo "The ownership test would then match every absolute path, and these" >&2
  echo "loops would delete symlinks they do not own. This is a bug in how" >&2
  echo "REPO is resolved, not something to work around here." >&2
  exit 1
fi

echo
echo "Old ~/bin symlinks (superseded by ~/.local/bin)"
# ~/bin is superseded by ~/.local/bin: an older installer linked into it, and
# ~/bin is often not on PATH, so those tools could not run by name. Remove ONLY
# a stale link that points into
# THIS repo AND that we plausibly created — never a ~/bin entry that points
# somewhere else, and never one a human made by hand; neither is ours to
# touch. This MUST run after the hooks step above,
# never before it: settings.json can still name an old ~/bin/... path right
# up until that step rewrites it, and deleting the symlink first — even
# briefly, even mid-script — would leave it pointing at nothing, silently
# unhooking the guard until the next run.
#
# Walks the directory instead of iterating EXECUTABLE_NAMES (the same fix as
# the loop below). Retiring a tool used to leave its ~/bin
# symlink behind forever: the array-keyed version could only prune a name
# still IN EXECUTABLE_NAMES, and retiring a tool is exactly when a name
# leaves that array — so the one moment cleanup was needed was the one
# moment it stopped happening.
#
# Deliberately NOT excluding `is_managed_link`, unlike the loop below, and
# deliberately NOT checking whether the target still resolves. Those two
# together are what make this loop different in kind, not just in
# implementation: the loop below prunes a DANGLING link, protecting one a
# `link` call is still trying to manage (an older branch, a bisect, a rename
# in flight). This loop prunes by LOCATION — every symlink into $REPO found
# in ~/bin is stale, live target or not, because ~/bin itself is superseded
# and nothing links into it any more. A ~/bin entry for a tool that is very
# much alive in ~/.local/bin is exactly the leftover this loop exists to
# remove, not a case to protect — so consulting `is_managed_link` here would
# guard a branch that can never fire (nothing anywhere records a
# `$HOME/bin/...` linkname), which is untestable by construction and
# indistinguishable from no guard at all. If a future `link` call ever
# targets `~/bin` again, that contradicts this loop's whole premise and
# should be resolved deliberately, not papered over with a silent exclusion.
#
# THE BASENAME TEST IS WHAT KEEPS "one we created" TRUE, and dropping it is
# how the walk stops being a like-for-like replacement for the array. The
# array-keyed version could only ever match `$HOME/bin/<tool-name>`, so it
# could not touch a symlink a HUMAN put in ~/bin pointing into this repo —
# `~/bin/dg -> $REPO/tools/danger-guard/claude-danger-guard`, say, or
# `~/bin/toolkit-install -> $REPO/install.sh`. A bare walk deletes those,
# because everything past the 30-odd tool names is by construction a link
# this installer never made. Every link it DID make named the file it
# pointed at, so basename equality readmits exactly the old loop's set
# without readmitting the array: it is keyed on the filesystem, so a retired
# tool — the case this walk exists for — still matches and is still
# pruned, while a hand-made alias never does.
#
# Do not "simplify" this to the PATH argument that used to sit at the top of
# this comment. `~/bin` not being on PATH is a fact about ONE machine, and
# install.sh runs on other people's machines; the file's only PATH checks are
# about ~/.local/bin and never mentioned ~/bin at all, so that reasoning did
# not even hold here.
found_stale=0
if [[ -d "$HOME/bin" ]]; then
  for old in "$HOME/bin"/*; do
    [[ -L "$old" ]] || continue
    target="$(readlink "$old")"
    [[ "$target" == "$REPO"/* ]] || continue    # not ours
    [[ "${old##*/}" == "${target##*/}" ]] || continue   # a hand-made alias, not ours
    found_stale=1
    act "removing ${old/#$HOME/$TILDE} -> $target"
    (( CHECK )) || rm -f "$old"
  done
fi
(( found_stale )) || ok "no stale ~/bin symlinks from this repo"

echo
echo "Links to files this repo no longer has"
# Retiring a tool used to leave its symlink behind forever. The loop above
# USED TO be able to prune only a name still in EXECUTABLE_NAMES (fixed for
# it too) — and retiring a tool is exactly when a name leaves that list,
# so the one moment cleanup was needed was the one moment it stopped
# happening. A tool added and then reverted exposed it: anyone who installed
# in that window kept a dangling ~/.local/bin link and a dangling
# ~/.claude/commands file, and `--check` still reported clean. A
# tombstone list would have the same defect one step later, because it also
# depends on someone remembering.
#
# So this asks the filesystem instead of a list: a symlink that points into THIS
# repo is one we created, and if its target no longer exists the file it named is
# gone. Both conditions are required. A link pointing somewhere else is not ours
# to touch, and a link whose target exists is live.
#
# THREE conditions, not two. Those two were not enough, and the gap was
# measured rather than reasoned about: they also hold for a tool that is live and
# merely absent from the checkout `install.sh` happens to be running from — an
# older branch, a bisect, a rename in flight. `link` calls that same state
# "missing in repo, skipped" and deliberately leaves the link; this loop deleted
# it 200 lines later, so one run printed both messages about one tool and a real
# apply removed a working tool's entry point. So a dangling link is pruned only
# if its name is NOT in `MANAGED_LINKS` (declared beside `link`, which records
# every name it is handed).
#
# That is not the tombstone list rejected two paragraphs up, and the direction is
# the whole difference. A tombstone list must be ADDED TO when a tool retires —
# forgetting leaves a leftover forever, which is the bug. `MANAGED_LINKS` must be
# REMOVED FROM, and removal is not a separate act of memory: retiring a tool
# deletes its `link` call, and the name stops being recorded because the line
# that recorded it is gone. Keying cleanup ON a hand-maintained array was the
# original defect; using what the installer just DID as an exclusion inverts the
# dependency instead of relocating it.
#
# Same ordering constraint as the block above: this MUST run after the hooks
# step, never before it.
#
# It PARTLY self-heals the worktree-install accident that the "never run
# ./install.sh from a worktree" rule exists to prevent: links repointed at a
# deleted worktree UNDER `.claude/worktrees/` are dangling and inside $REPO, so
# a later install from the primary checkout clears them. It does nothing for a
# worktree created OUTSIDE the repo — those targets fail the `$REPO/*` test and
# are correctly treated as not ours, so they are stranded permanently. Do not
# read this as covering that rule; the rule is still the protection.
#
# The ownership test below is the ONLY thing between this loop and every
# dangling symlink in ~/.local/bin, and it fails OPEN on an empty $REPO:
# the pattern becomes `/*`, which matches every absolute path. The refusal for
# that now lives once, above the ~/bin loop, and covers this loop too — both
# have the identical ownership test and the identical failure mode, so one
# guard early in the file is what closes it for both rather than a second
# copy here.
found_dangling=0
for dir in "$HOME/.local/bin"; do
  [[ -d "$dir" ]] || continue
  for entry in "$dir"/*; do
    [[ -L "$entry" ]] || continue
    target="$(readlink "$entry")"
    [[ "$target" == "$REPO"/* ]] || continue     # not ours
    [[ -e "$entry" ]] && continue                # target exists: live
    # Still managed: `link` above tried this exact linkname and reported the
    # target "missing in repo, skipped", deliberately leaving the link. Deleting
    # it here would contradict that decision 200 lines later, and one run would
    # print both messages about one tool. Nothing is echoed on this branch --
    # `link` has already said the useful thing, and a second line about the same
    # name is what made the original defect hard to read.
    is_managed_link "$entry" && continue
    found_dangling=1
    act "removing ${entry/#$HOME/$TILDE} -> $target (target no longer in the repo)"
    (( CHECK )) || rm -f "$entry"
  done
done
(( found_dangling )) || ok "no links to files this repo no longer has"

echo
echo "Plugin components"
# Commands, agents, skills and the build-lens files ship in the plugin, so this
# installer links none of them into `~/.claude/`. An earlier install did, and a
# link left there still runs the old copy instead of the plugin's, so the ones
# that point into THIS checkout are removed. `plugin_paths.py` decides
# ownership by realpath plus component containment, so a `<repo>/../x` target
# and a `<repo>-old/x` sibling are never ours. A link that is not ours is left
# alone and said so.
legacy_rc=0
legacy_out="$(python3 "$REPO/lib/plugin_paths.py" legacy "$HOME" "$REPO")" || legacy_rc=$?
if (( legacy_rc != 0 )); then
  warn "plugin_paths.py legacy exited $legacy_rc — leftover ~/.claude links were not checked"
  REQUIRED_FAILURES+=("plugin legacy-link check did not run (plugin_paths.py legacy exited $legacy_rc)")
else
  while IFS=$'\t' read -r legacy_kind legacy_path; do
    [[ -n "$legacy_kind" ]] || continue
    legacy_shown="${legacy_path/#$HOME/$TILDE}"
    if [[ "$legacy_kind" == "owned" ]]; then
      act "removing $legacy_shown (now ships in the toolkit-core plugin)"
      if (( CHECK )); then
        REQUIRED_FAILURES+=("owned legacy link still present: $legacy_shown — run ./install.sh")
      else
        rm -f "$legacy_path"
      fi
    else
      ok "$legacy_shown kept (not this checkout's)"
    fi
  done <<< "$legacy_out"
  # Empty only once the removal above took the last link; a directory holding
  # anything else is the human's and stays.
  (( CHECK )) || rmdir "$HOME/.claude/build-lens" 2>/dev/null || true
fi

components_rc=0
components_out="$(python3 "$REPO/lib/profiles.py" components "$REPO" 2>/dev/null)" || components_rc=$?
if (( components_rc == 0 || components_rc == 1 )); then
  while IFS=$'\t' read -r component_state component_path; do
    [[ -n "$component_state" ]] || continue
    if [[ "$component_state" == "ok" ]]; then
      ok "$component_path"
    else
      warn "$component_path does not resolve — the plugin ships no skills or agents"
      REQUIRED_FAILURES+=("$component_path does not resolve — the plugin ships no skills or agents")
    fi
  done <<< "$components_out"
else
  warn "plugin component check did not run (profiles.py components exited $components_rc)"
  REQUIRED_FAILURES+=("plugin component check did not run (profiles.py components exited $components_rc)")
fi

# A bare name another copy also defines is a silent wrong answer: `/<name>` runs
# whichever loaded first. Fail strict: an inventory that could not be read is
# reported, never read as "no clashes".
clash_rc=0
clash_out="$(python3 "$REPO/lib/plugin_paths.py" clashes "$HOME" "$REPO")" || clash_rc=$?
if (( clash_rc != 0 )); then
  warn "plugin clash check did not run (plugin_paths.py clashes exited $clash_rc)"
  REQUIRED_FAILURES+=("plugin clash check did not run (plugin_paths.py clashes exited $clash_rc)")
elif [[ -n "$clash_out" ]]; then
  while IFS= read -r clash_line; do
    warn "$clash_line"
    REQUIRED_FAILURES+=("$clash_line")
  done <<< "$clash_out"
else
  ok "no bare-name clashes"
fi

echo
echo "Permissions -> ~/.claude/settings.json"
if (( AUTONOMOUS )) && (( ! CHECK )); then
  echo
  warn "--autonomous grants Bash(*) with defaultMode \"auto\". Almost nothing"
  warn "   will prompt. claude-danger-guard runs first as a deterministic check."
  warn "   It blocks a specific set of destructive shapes and ALLOWS everything"
  warn "   it does not recognise as dangerous."
  warn ""
  warn "   Under auto mode Claude Code drops the blanket Bash(*) grant, so a Bash"
  warn "   command no surviving rule matches goes to its classifier. That is a"
  warn "   second layer, but a command a narrow allow rule covers (including any"
  warn "   saved by \"don't ask again\") gets no semantic review. Outside auto mode"
  warn "   (default, acceptEdits) Bash(*) stays live and the guard is the ONLY"
  warn "   shape check on shell access."
  warn ""
  warn "   This is only safe with the decision-gate contract in the toolkit policy"
  warn "   loaded (policy/CLAUDE.md, \"Gate at decisions\"; --check reports"
  warn "   \"toolkit policy: ok\"). Applying it without that is the actual mistake."
  warn ""
  warn "   Read claude/permissions.autonomous.json in full before continuing."
  echo
  if [[ -t 0 ]]; then
    printf '  Type "I understand" to apply it, anything else to skip: '
    read -r reply
    if [[ "$reply" != "I understand" ]]; then
      act "skipped the autonomous grant — baseline permissions only"
      AUTONOMOUS=0
    fi
  else
    warn "no TTY — refusing to apply the autonomous grant unattended"
    AUTONOMOUS=0
  fi
fi
CHECK="$CHECK" AUTONOMOUS="$AUTONOMOUS" SETTINGS="$SETTINGS" REPO="$REPO" python3 - <<'PY'
import json, os, shutil, sys

check = os.environ["CHECK"] == "1"
autonomous = os.environ["AUTONOMOUS"] == "1"
settings_path = os.environ["SETTINGS"]
repo = os.environ["REPO"]

def load(name):
    with open(os.path.join(repo, "claude", name)) as handle:
        return json.load(handle).get("permissions") or {}

wanted = load("permissions.baseline.json")

# Personal opt-out: exact `ask` rule strings in the toolkit config that this
# run must not re-add. Applied to the baseline BEFORE the autonomous overlay.
# A stub fixture repo without lib/toolkit_config.py means "no opt-out" and
# stays quiet. `deny` rules are never opt-out-able; they guard secrets.
sys.path.insert(0, os.path.join(repo, "lib"))
try:
    import toolkit_config
    opt_out = toolkit_config.get("permissions", "baseline_opt_out")
except ImportError:
    opt_out = None
if opt_out is not None:
    if not isinstance(opt_out, list):
        print("  \033[33m!!\033[0m permissions.baseline_opt_out must be a list"
              " — ignored, all baseline rules apply")
    else:
        seen = set()
        for rule in opt_out:
            if not isinstance(rule, str):
                continue
            if rule in seen:
                print("  \033[33m!!\033[0m permissions.baseline_opt_out lists"
                      " %s twice — the repeat is ignored" % rule)
                continue
            seen.add(rule)
            if rule in wanted.get("ask", []):
                wanted["ask"] = [r for r in wanted["ask"] if r != rule]
                print("  \033[2m--\033[0m opted out: ask %s" % rule)
            elif rule in wanted.get("deny", []):
                print("  \033[33m!!\033[0m permissions.baseline_opt_out cannot"
                      " remove deny rule %s — applied anyway" % rule)
            else:
                print("  \033[33m!!\033[0m permissions.baseline_opt_out entry"
                      " %s matches no baseline rule — ignored" % rule)

if autonomous:
    extra = load("permissions.autonomous.json")
    for key in ("allow", "deny", "ask"):
        if extra.get(key):
            wanted.setdefault(key, [])
            wanted[key] = wanted[key] + extra[key]
    if extra.get("defaultMode"):
        wanted["defaultMode"] = extra["defaultMode"]

try:
    with open(settings_path) as handle:
        settings = json.load(handle)
    existed = True
except FileNotFoundError:
    settings, existed = {}, False
except json.JSONDecodeError as exc:
    print("  \033[31m!!\033[0m %s is not valid JSON (%s) — refusing to touch it"
          % (settings_path, exc))
    sys.exit(1)

current = settings.get("permissions") or {}
merged = {k: list(v) if isinstance(v, list) else v for k, v in current.items()}
added = {}

# UNION only. An entry already present is left alone, and nothing is ever
# removed: this must not be able to revoke a rule the user added themselves.
for key in ("allow", "deny", "ask"):
    for rule in wanted.get(key, []):
        bucket = merged.setdefault(key, [])
        if rule not in bucket:
            bucket.append(rule)
            added.setdefault(key, []).append(rule)

mode_change = None
if wanted.get("defaultMode") and current.get("defaultMode") != wanted["defaultMode"]:
    mode_change = wanted["defaultMode"]
    merged["defaultMode"] = mode_change

if not added and mode_change is None:
    print("  \033[32mOK\033[0m permissions already cover the %s set"
          % ("autonomous" if autonomous else "baseline"))
    sys.exit(0)

for key, rules in sorted(added.items()):
    for rule in rules:
        print("  \033[33m->\033[0m %s += %s" % (key, rule))
if mode_change:
    print("  \033[33m->\033[0m defaultMode = %s" % mode_change)

if check:
    sys.exit(0)

# Record first: a rule written without a record is one a removal can never
# prove the installer added, so a record failure stops the write.
try:
    import install_record
    install_record.add_permissions(
        install_record.record_path(os.path.expanduser("~")), added,
        mode_set=mode_change,
        mode_previous=current.get("defaultMode") if mode_change else None)
except (ImportError, OSError) as exc:
    print("  \033[31m!!\033[0m cannot write the install record (%s) — permissions NOT written" % exc)
    sys.exit(1)

settings["permissions"] = merged
import settings_io
settings_io.write_json_atomic(settings_path, settings, ensure_ascii=True)
print("  \033[33m->\033[0m wrote permissions (snapshot taken at the start of this run)")
PY

echo
echo "Auto memory -> ~/.claude/settings.json"
CHECK="$CHECK" SETTINGS="$SETTINGS" REPO="$REPO" python3 - <<'PY'
import json, os, shutil, sys

check = os.environ["CHECK"] == "1"
settings_path = os.environ["SETTINGS"]
repo = os.environ["REPO"]

# A real checkout always has lib/toolkit_config.py and lib/auto_memory.py; this
# only fails on a stub fixture repo (this repo's own minimal install fixtures,
# e.g. test_install_worktree.py, ship lib/worktree_detect.sh alone). Since it
# prints when it happens, the failure is never silent -- it just never fires
# on a real machine.
sys.path.insert(0, os.path.join(repo, "lib"))
try:
    import toolkit_config
    import auto_memory
except ImportError as exc:
    print("  \033[31m!!\033[0m auto memory skipped: toolkit lib not importable (%s)"
          % exc)
    sys.exit(0)

home = os.path.expanduser("~")
unify = auto_memory.unify_enabled(toolkit_config)
# Raw config value, unexpanded -- managed_target() does its own ~ expansion
# against home, so tests stay hermetic against a fixture home.
target, reason = auto_memory.managed_target(toolkit_config.get("workspace_root"), home)

try:
    with open(settings_path) as handle:
        settings = json.load(handle)
    existed = True
except FileNotFoundError:
    settings, existed = {}, False
except json.JSONDecodeError as exc:
    print("  \033[31m!!\033[0m %s is not valid JSON (%s) — refusing to touch it"
          % (settings_path, exc))
    sys.exit(1)

# The sentinel keeps a present JSON `null` classed as foreign rather than as
# absent -- auto_memory.plan() relies on the distinction.
current = settings.get("autoMemoryDirectory", auto_memory.ABSENT)
action, detail = auto_memory.plan(current, unify, target)

if action == "ok":
    if detail is None:
        print("  \033[32mOK\033[0m auto memory not managed (auto_memory.unify is false)")
    else:
        print("  \033[32mOK\033[0m autoMemoryDirectory = %s" % detail)
    sys.exit(0)

if action == "skip":
    print("  \033[31m!!\033[0m autoMemoryDirectory not set: %s" % reason)
    print("     sessions started in a repo or worktree will load an empty memory folder")
    sys.exit(0)

if action == "foreign":
    unavailable = target if target is not None else ("unavailable: %s" % reason)
    print("  \033[31m!!\033[0m autoMemoryDirectory is %r, not the toolkit's value (%s) — leaving it alone"
          % (current, unavailable))
    sys.exit(0)

if action == "set":
    print("  \033[33m->\033[0m autoMemoryDirectory = %s" % detail)
else:
    print("  \033[33m->\033[0m removing autoMemoryDirectory (%s)" % detail)

if check:
    sys.exit(0)

if action == "set":
    settings["autoMemoryDirectory"] = detail
else:
    settings.pop("autoMemoryDirectory", None)

import settings_io
settings_io.write_json_atomic(settings_path, settings, ensure_ascii=True)
print("  \033[33m->\033[0m wrote autoMemoryDirectory (snapshot taken at the start of this run)")
PY

echo
echo "Workflows -> ~/.claude/settings.json"
CHECK="$CHECK" SETTINGS="$SETTINGS" REPO="$REPO" python3 - <<'PY'
import json, os, sys

check = os.environ["CHECK"] == "1"
settings_path = os.environ["SETTINGS"]
repo = os.environ["REPO"]

# A real checkout always has these modules; this only fails on a stub fixture
# repo (see the Auto memory section above). It prints when it happens, so the
# failure is never silent.
sys.path.insert(0, os.path.join(repo, "lib"))
try:
    import toolkit_config
    import profiles
    import install_record
    import workflows_setting
except ImportError as exc:
    print("  \033[31m!!\033[0m enableWorkflows skipped: toolkit lib not importable (%s)"
          % exc)
    sys.exit(0)

home = os.path.expanduser("~")
record_path = install_record.record_path(home)

try:
    with open(settings_path) as handle:
        settings = json.load(handle)
except FileNotFoundError:
    settings = {}
except json.JSONDecodeError as exc:
    print("  \033[31m!!\033[0m %s is not valid JSON (%s) — refusing to touch it"
          % (settings_path, exc))
    sys.exit(1)
if not isinstance(settings, dict):
    print("  \033[31m!!\033[0m %s is not a JSON object — refusing to touch it"
          % settings_path)
    sys.exit(1)

# A selection that cannot be read is not "builder is off": removing the key on
# a transient profiles error would undo something the user asked for.
names, error, _defaulted = profiles.selected(repo)
if error or names is None:
    print("  \033[31m!!\033[0m enableWorkflows left as is: cannot tell the selected profiles (%s)"
          % error)
    sys.exit(0)

wanted = workflows_setting.wanted(
    toolkit_config.get(*workflows_setting.CONFIG_PATH), names)
owned = workflows_setting.owned(install_record.load(record_path))
# The sentinel keeps a present JSON `null` classed as a value the user set.
current = settings.get(workflows_setting.KEY, workflows_setting.ABSENT)
action = workflows_setting.plan(current, wanted, owned)

if action == "ok":
    if owned:
        print("  \033[32mOK\033[0m enableWorkflows = true (set by the toolkit)")
    else:
        print("  \033[32mOK\033[0m enableWorkflows not managed (build.enable_workflows is off or builder is not selected)")
elif action == "foreign":
    print("  \033[32mOK\033[0m enableWorkflows is yours (%s) — left as is"
          % json.dumps(current))
elif action == "set":
    print("  \033[33m->\033[0m enableWorkflows = true")
elif action == "remove":
    print("  \033[33m->\033[0m removing enableWorkflows (no longer wanted)")
else:
    print("  \033[31m!!\033[0m enableWorkflows was changed since the toolkit set it — left as you set it; no longer managed")

if check:
    sys.exit(0)

def set_owned(owned_now, unwritten):
    try:
        install_record.set_workflows_owned(record_path, owned_now)
    except (ImportError, OSError) as exc:
        print("  \033[31m!!\033[0m cannot write the install record (%s)%s" % (exc, unwritten))
        sys.exit(1)

import settings_io
if action == "set":
    # Record first: a key written without a record is one a removal can never
    # prove the installer added, so a record failure stops the write.
    set_owned(True, " — enableWorkflows NOT written")
    settings[workflows_setting.KEY] = True
    settings_io.write_json_atomic(settings_path, settings, ensure_ascii=True)
elif action == "remove":
    del settings[workflows_setting.KEY]
    settings_io.write_json_atomic(settings_path, settings, ensure_ascii=True)
    set_owned(False, " — enableWorkflows was removed but is still recorded as ours")
elif action == "edited":
    set_owned(False, "")
PY

echo
echo "CLAUDE.md excludes -> ~/.claude/settings.json"
CHECK="$CHECK" SETTINGS="$SETTINGS" REPO="$REPO" python3 - <<'PY'
import json, os, shutil, sys

# A worktree under <repo>/.claude/worktrees/ sits INSIDE the primary checkout,
# so a session started there loads the repo's CLAUDE.md twice: the worktree's
# copy and the primary's, found walking up (the CLAUDE.md is loaded twice
# in one executor). This skips the worktree's copy; the primary's still
# loads (verified live). Trade-off: a branch that edits CLAUDE.md is read
# from the primary checkout, not the branch, in its own worktree session.
# Only ever ADDS the pattern -- a user's own entries are kept, and a value
# that is not a list of strings is left alone with a warning.
PATTERN = "**/.claude/worktrees/*/CLAUDE.md"
check = os.environ["CHECK"] == "1"
settings_path = os.environ["SETTINGS"]

try:
    with open(settings_path) as handle:
        settings = json.load(handle)
    existed = True
except FileNotFoundError:
    settings, existed = {}, False
except json.JSONDecodeError as exc:
    print("  \033[31m!!\033[0m %s is not valid JSON (%s) — refusing to touch it"
          % (settings_path, exc))
    sys.exit(1)

current = settings.get("claudeMdExcludes")
if "claudeMdExcludes" not in settings:
    wanted = [PATTERN]
elif isinstance(current, list) and all(isinstance(p, str) for p in current):
    if PATTERN in current:
        print("  \033[32mOK\033[0m claudeMdExcludes has %s" % PATTERN)
        sys.exit(0)
    wanted = current + [PATTERN]
else:
    print("  \033[31m!!\033[0m claudeMdExcludes is %r, not a list of patterns — leaving it alone"
          % (current,))
    print("     worktree sessions will load each repo's CLAUDE.md twice")
    sys.exit(0)

print("  \033[33m->\033[0m claudeMdExcludes += %s" % PATTERN)
if check:
    sys.exit(0)

# Record first, same reason as the permissions step.
try:
    sys.path.insert(0, os.path.join(os.environ["REPO"], "lib"))
    import install_record
    import settings_io
    install_record.add_exclude(
        install_record.record_path(os.path.expanduser("~")), PATTERN,
        created_key=("claudeMdExcludes" not in settings))
except (ImportError, OSError) as exc:
    print("  \033[31m!!\033[0m cannot write the install record (%s) — claudeMdExcludes NOT written" % exc)
    sys.exit(1)

settings["claudeMdExcludes"] = wanted
settings_io.write_json_atomic(settings_path, settings, ensure_ascii=True)
print("  \033[33m->\033[0m wrote claudeMdExcludes (snapshot taken at the start of this run)")
PY

echo
echo "Your CLAUDE.md"
WS="$(python3 -c 'import json,os,sys
try: print(json.load(open(sys.argv[1])).get("workspace_root") or "")
except Exception: print("")' "$CONFIG" 2>/dev/null || true)"
WS="${WS/#\~/$HOME}"
if [[ -z "$WS" ]]; then
  warn "no workspace_root configured — skipping the CLAUDE.md check"
elif [[ -f "$WS/CLAUDE.md" ]]; then
  if grep -q "^\*\*TODO" "$WS/CLAUDE.md" 2>/dev/null || grep -q "TODO — " "$WS/CLAUDE.md" 2>/dev/null; then
    warn "${WS/#$HOME/$TILDE}/CLAUDE.md still has TODO sections — fill them in. The"
    warn "   'About me' and tone parts are what make Claude useful here."
  else
    ok "${WS/#$HOME/$TILDE}/CLAUDE.md"
  fi
  if grep -qF -e "~/.claude/build-lens" -e "~/.claude/commands" "$WS/CLAUDE.md" 2>/dev/null; then
    warn "${WS/#$HOME/$TILDE}/CLAUDE.md still points at ~/.claude/build-lens or ~/.claude/commands, which this toolkit no longer installs."
    warn "   Those files now live in the toolkit checkout: tools/build-lens/<file> and skills/<group>/<name>/SKILL.md. Update those lines (ask Claude to)."
  fi
else
  warn "no CLAUDE.md at ${WS/#$HOME/$TILDE} — this is the highest-leverage file you own."
  warn "   Copy the template and fill in its TODO sections:"
  warn "   cp -n \"$REPO/CLAUDE.md.template\" \"$WS/CLAUDE.md\""
fi
# The toolkit policy loads through one import line in ~/.claude/CLAUDE.md.
# Read-only; warns, never changes the exit code. A stub fixture repo has no
# lib/team_policy.py, and says so rather than going quiet.
tp_json=""
if [[ -r "$REPO/lib/team_policy.py" ]]; then
  tp_json="$(python3 "$REPO/lib/team_policy.py" --status "$REPO" 2>/dev/null)" || tp_json=""
fi
tp_state="$(printf '%s' "$tp_json" | python3 -c 'import json,sys
try: d=json.load(sys.stdin); print(d["state"] + "\t" + (d.get("detail") or ""))
except Exception: print("")' 2>/dev/null || true)"
tp_detail="${tp_state#*$'\t'}"; tp_state="${tp_state%%$'\t'*}"
case "$tp_state" in
  ok)          ok "toolkit policy: ok — $tp_detail" ;;
  opted-out)   ok "toolkit policy: opted-out — $tp_detail" ;;
  missing|dangling)
    warn "toolkit policy: $tp_state — $tp_detail"
    warn "   Sessions run without the toolkit policy. Rerun /onboard, or: python3 \"$REPO/lib/team_policy.py\" ensure \"$REPO\""
    warn "   (To opt out on purpose, set team_policy.import to false in your toolkit config.)" ;;
  legacy-copy)
    warn "toolkit policy: legacy-copy — $tp_detail"
    warn "   Those sections now load from policy/CLAUDE.md, so your copy loads twice and can go stale. Ask Claude to trim them from your workspace CLAUDE.md." ;;
  error) warn "toolkit policy: error — $tp_detail" ;;
  *)     warn "toolkit policy: not checked (lib/team_policy.py missing or failed)" ;;
esac

echo
if (( ON_LOCAL_BIN_PATH )); then
  ok "~/.local/bin is on PATH"
else
  warn "~/.local/bin is not on your PATH — the hooks use absolute paths so they"
  warn "   still work, but the toolkit's commands will not be callable by name"
  warn "   until you add it."
fi

echo
echo "Tests"
failed=0
if [[ "${CLAUDE_INSTALL_SKIP_SUITES:-}" == "1" ]]; then
  echo "  Tests SKIPPED (CLAUDE_INSTALL_SKIP_SUITES=1) — not a pass"
else
# find, not a glob: the suites live under tools/*/tests, lib/tests and
# skills/*/scripts/tests, and a brace-glob that silently matches nothing would
# report success having run none. `skills` was added later -- until then a
# skill could ship its own suite and never turn a run red, which is worse than
# having no suite because it looks covered.
# discovered/completed: a sweep that found nothing, or stopped before the end of
# its queue, is red. The suites are collected into an array BEFORE any runs, so
# `discovered` is fixed up front and no suite can touch the queue (a suite that
# reads stdin used to swallow the rest of a stdin-fed loop). `completed` counts
# suites whose subshell returned (pass or fail), so a sweep that stopped early
# shows up as completed != discovered. bash 3.2 (macOS): no mapfile, so a
# read loop that only appends.
suites=()
while IFS= read -r suite; do
  suites+=("$suite")
done < <(find "$REPO/tools" "$REPO/lib" "$REPO/skills" "$REPO/evals" "$REPO/scripts" -path '*/tests/test_*.py' -type f 2>/dev/null | sort)
discovered=${#suites[@]}
completed=0
for suite in ${suites[@]+"${suites[@]}"}; do
  base="$(dirname "$(dirname "$suite")")"
  label="$(basename "$base")/$(basename "$suite")"
  # </dev/null stays as a second line of defence: the collection loop above
  # reads the suite list on stdin, and a suite that read stdin in a stdin-fed
  # loop once swallowed the queue (only part of the suite list ran,
  # all reported OK, exit 0). Pinned by lib/tests/test_install_checks_every_suite.py.
  # env -u: suites re-point HOME at fixtures, and these two would follow the
  # user's real setup past it. CLAUDE_CONFIG_DIR=~/.claude is allowed here but
  # makes an installer run inside a suite refuse (the fixture's ~ differs);
  # GIT_CONFIG_GLOBAL overrides the fixture HOME's git config.
  if ( cd "$base" && env -u CLAUDE_CONFIG_DIR -u GIT_CONFIG_GLOBAL python3 "tests/$(basename "$suite")" >/dev/null 2>&1 </dev/null ); then
    ok "$label"
  else
    warn "$label FAILED"
    failed=1
  fi
  completed=$((completed+1))
done
if (( discovered == 0 )); then
  warn "no test suites found under tools/ lib/ skills/"
  failed=1
elif (( completed != discovered )); then
  warn "only $completed of $discovered test suites completed"
  failed=1
fi
fi

echo
if (( UNPROTECTED )); then
  warn "Hooks are NOT protected (see the lines above) — fix those before relying on them."
fi
if (( failed )); then
  warn "Some suites failed — investigate before relying on the hooks."
fi
# The record's only creation point on a converged machine, so a failure fails
# the run. Only on an otherwise successful apply. A required failure also
# leaves `commit` at the last fully successful install, which updating relies on.
if (( ! CHECK )) && (( ! UNPROTECTED )) && (( ! failed )) && (( ${#REQUIRED_FAILURES[@]} == 0 )); then
  if ! python3 "$REPO/lib/install_record.py" finish "$REPO"; then
    REQUIRED_FAILURES+=("install record could not be written")
  fi
fi
if (( ${#REQUIRED_FAILURES[@]} > 0 )); then
  echo
  warn "Required components failed:"
  for entry in ${REQUIRED_FAILURES[@]+"${REQUIRED_FAILURES[@]}"}; do
    warn "   - $entry"
  done
fi
if (( UNPROTECTED || failed || ${#REQUIRED_FAILURES[@]} > 0 )); then
  exit 1
fi
if (( CHECK )); then
  echo "Check complete. Re-run without --check to apply."
else
  echo "Done. Run /reload-plugins in any open session, or start a new one, to load the hooks."
fi
