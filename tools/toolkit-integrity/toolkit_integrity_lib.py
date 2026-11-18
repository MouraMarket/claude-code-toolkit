#!/usr/bin/env python3
"""Say when the toolkit installed on this machine has stopped being intact.

Two independent questions, deliberately kept apart:

**Is anything the machine is configured to run unable to run?** Claude Code
skips a hook it cannot execute and says nothing about it, so a dangling
`~/.local/bin/claude-danger-guard` is indistinguishable from a guard that
simply had nothing to complain about. That is the worst failure shape this
repo has: a guard going dark with no signal.

**Has the toolkit moved underneath this session?** All the executables are
per-file symlinks into one checkout, and hooks re-exec per tool call, so a
`git tidy`, pull or merge in that checkout swaps `claude-danger-guard` and
`claude-session-claim` mid-flight for every open session. Nothing about a
session's own work breaks when that happens — only the tooling around it
changes — so this half is a notice, not a warning.

That last sentence held for CHANGED files and was printed for added ones too,
which is why the diff is now read with `--name-status`. A pull creates no
symlink, so a file that is new in the range has nothing pointing at it and is
inert until `install.sh` runs. Neither integrity check can see that: there is
no directory entry to enumerate and no `settings.json` command naming it, so
the machine looks clean. The notice is the only place it can be said, and
saying "already live" there was the opposite of true.

Three things here are load-bearing.

**The inventory is never the ground truth.** `install.sh`'s `EXECUTABLE_NAMES`
is hand-maintained and has already drifted. A check whose entire job is
catching reality diverging from a declaration cannot take that declaration as
its input. So the two integrity checks read, respectively, `~/.claude/
settings.json` (what Claude Code will actually try to run) and the install
directories themselves (what is actually on disk).

**Those two checks are separate functions with separately-assertable output,
and they overlap on purpose.** A dangling `~/.local/bin` symlink that a hook
names is found by both. This repo has been bitten by two mechanisms with
one observable outcome quietly covering for each other — `install.sh`'s
worktree detection, and the CACHE filter in `git tidy` — so `unrunnable_hooks`
and `broken_links` are asserted directly in the tests rather than through the
rendered notice, and neither is a fallback for the other.

**`settings.json` catches what directory enumeration structurally cannot:
a hook whose link was never created, or was deleted outright.** There is no
directory entry left to enumerate in that case, so a check built only on the
install directories reports a clean machine while the hook is gone. The design
this was built from specified enumeration alone; that hole is why both are
here.

**The drift notice is path-filtered.** On a checkout that moves often with
several sessions open, an unfiltered "the toolkit moved" fires constantly and
gets skimmed past — which is worse than no notice, because it teaches the
reader to skip the one that matters.
"""

# Design decisions about this file that look like accidental complexity are
# recorded in docs/decisions.md. Read the relevant one before simplifying
# anything here.

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from typing import Optional

try:
    import toolkit_config as _config           # type: ignore
except Exception:                              # pragma: no cover - no config
    _config = None

try:
    import hook_manifest as _manifest          # type: ignore
except Exception:                              # pragma: no cover - lib missing
    _manifest = None

try:
    import profiles as _profiles               # type: ignore
except Exception:                              # pragma: no cover - lib missing
    _profiles = None

# The git budget is one part of the run, and the whole run is bounded by the
# hook's watchdog (RUN_BUDGET_SECONDS in claude-toolkit-integrity), which must
# fit inside the `timeout` declared for this hook in plugin/hooks/hooks.json.
# If the harness kills a notice hook it discards all output, and no output is
# indistinguishable from "nothing to report" -- so going over budget fails
# SILENT, in exactly the direction this tool exists to prevent. The watchdog
# turns that into a stated "ran out of time". `TimeoutBudget` in the tests
# enforces the numbers and re-derives MAX_GIT_CALLS by counting call sites
# rather than trusting this comment. If you change one of these numbers, run it.
#
# Only the DRIFT half spends any of the git budget. The integrity half runs no
# subprocess at all, except the SessionStart canaries: one short subprocess per
# probe, bounded by the same deadline.
GIT_TIMEOUT_SECONDS = 4
MAX_GIT_CALLS = 2
GIT_KILL_MARGIN_SECONDS = 0.5
MIN_GIT_SECONDS = 0.5
# Monotonic time at which the hook's watchdog fires; None outside the hook.
_deadline = None
STATE_TTL_SECONDS = 7 * 24 * 60 * 60
DEFAULT_STATE_PATH = "~/.claude/toolkit-integrity-state.json"

# A stray GIT_DIR makes a worktree and a primary checkout compare equal, so
# every git call here runs with the three location variables removed. See
# lib/worktree_detect.sh, which paid for this lesson first.
_UNSET_GIT_VARS = ("GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE")

# Where install.sh links entry points, relative to $HOME. The four `.claude`
# dirs now hold only leftovers of the old per-file links, which install.sh
# removes; a dangling one left behind is still a defect, as silent as a
# dangling hook link, so they stay on the list.
LINK_DIRS = (".local/bin", ".claude/commands", ".claude/agents",
             ".claude/skills", ".claude/build-lens")

# Only ~/.local/bin holds things that must be executable. A markdown skill or
# agent is read, not run, so demanding +x there would report every one of them.
EXECUTABLE_DIR = ".local/bin"

# What counts as a change worth mentioning: a path a running session actually
# executes or imports. Docs, the README and the templates can move without
# changing what anything does.
#
# `lib/` is here because it is imported at RUN TIME, not only at install time.
# `lib/toolkit_config.py` is read by most live hooks, this one included, so
# a merge touching only `lib/` really does change hook behaviour mid-session.
# It was omitted at first on the theory that lib is build-time -- and the
# comment here said so, which would have made the next reader confident about
# a hole: the notice would have been silent on a lib-only change.
LIVE_PREFIXES = ("tools/", "agents/", "skills/", "lib/")
LIVE_FILES = ("plugin/hooks/hooks.json", "claude/profiles.json")
# Every plugin's manifest: `plugin/` for core, `plugin-*/` for the rest.
LIVE_MANIFEST_RE = re.compile(r"^plugin(-[a-z]+)?/hooks/hooks\.json$")

# A test file is never imported by a running hook, so `tools/x/tests/` and
# `lib/tests/` sit under a live prefix and are not live. Excluding them is the
# safe direction: including a test change costs a notice nobody can act on,
# and this filter's whole job is to stay worth reading.
DEAD_MARKERS = ("/tests/",)

# Live paths whose change needs more than a new session to take effect: core's
# manifest here, every other plugin's by LIVE_MANIFEST_RE, plus the registry.
PLUGIN_HOOKS = "plugin/hooks/hooks.json"

# The guards the plugin MUST register, and the tools each must cover. Written
# out here on purpose, NOT derived from hooks.json: that file is the thing
# being checked, and a check that reads its own expectations from its subject
# passes when the subject is emptied. A test pins this tuple against the
# shipped manifest so it cannot drift silently.
#
# The profile is the registry key of the plugin that must carry the guard. A
# guard is only required when its profile is selected or enabled on this machine.
REQUIRED_GUARDS = (
    # (profile, plugin-relative path, event, tools the matcher must cover)
    ("core", "tools/danger-guard/claude-danger-guard", "PreToolUse",
     ("Bash", "PowerShell", "Monitor")),
    ("core", "tools/worktree-guard/claude-worktree-guard", "PreToolUse",
     ("Bash", "Monitor", "Edit", "Write", "MultiEdit", "NotebookEdit")),
    ("core", "tools/distill-guard/claude-distill-write-guard", "PreToolUse",
     ("Write",)),
    ("builder", "tools/model-guard/claude-model-guard", "PreToolUse", ("Agent",)),
    # "*" means the registration itself must be unrestricted: session-claim keys on a
    # path so that a file-writing tool Claude Code ships later is covered the day it
    # ships (its own docstring); a named matcher would open that hole silently.
    ("builder", "tools/session-claim/claude-session-claim", "PreToolUse",
     ("*",)),
)

# Advice for a changed guard or manifest. Re-running install.sh restores
# neither, so the reinstall line the hooks/links sections carry would mislead.
_REPAIR_ADVICE = (
    "A guard or a plugin's hooks/hooks.json in the toolkit checkout has been "
    "changed. In the PRIMARY checkout, `git status` and `git diff` show what "
    "changed; restore it (or pull the latest main), then run /reload-plugins "
    "or start a new session. Re-running install.sh does not restore a changed "
    "file.")

# Behavioural canaries: does the guard EXECUTABLE still refuse? Two kinds. The
# guards whose verdict on a fixed payload is machine-independent (danger-guard,
# model-guard) run on fixed payloads, `cwd` filled in at run time (the user's
# home). The stateful ones (worktree-guard, distill-write-guard, session-claim)
# depend on a repo's worktrees or on claim files, so they run against a
# throwaway fixture built in a temp directory and removed afterwards.
CANARY_KEY = "canaries"

# A session that set one of these made a visible choice; it is not a disarmed
# guard. They are the escape hatches of every canaried guard.
CANARY_STRIPPED_ENV = ("DANGER_ALLOW", "MODEL_GUARD_ALLOW", "WORKTREE_ALLOW",
                       "WT_ALLOW_MAIN", "CLAIM_ALLOW")

CANARY_MAX_SECONDS = 2.0
CANARY_MARGIN_SECONDS = 0.5

# A canary record's `got` is "denied" or "allowed" (a verdict), "unidentified"
# (session-claim's, below), or one of two ways of having NO verdict, kept apart
# because they call for opposite advice. TIMED_OUT: the run's budget was spent
# before the probe started, or the probe hit its own cap -- a busy machine
# produces both, so it is no evidence the guard changed. UNCHECKED: the guard
# ran and returned something that is not a verdict (crashed, bad exit,
# unreadable output) -- that is evidence, and gets the repair advice.
TIMED_OUT = "timed-out"
UNCHECKED = "unchecked"

_CANARY_COMMON = {"session_id": "toolkit-integrity-canary",
                  "hook_event_name": "PreToolUse",
                  "transcript_path": "/dev/null"}


def _canary_payload(tool_name, tool_input):
    payload = dict(_CANARY_COMMON)
    payload["tool_name"] = tool_name
    payload["tool_input"] = tool_input
    return payload


# model-guard reads the user's agent definitions and rightly allows a no-model
# dispatch whose definition pins a model, so a real agent name would make the
# canary machine-dependent. An agent that cannot exist is always "no model".
_CANARY_AGENT = "toolkit-integrity-canary-no-such-agent"

CANARIES = (
    # (plugin-relative path, payload it must refuse, payload it must allow,
    #  config section whose `enabled` key can switch the guard off, or None)
    ("tools/danger-guard/claude-danger-guard",
     _canary_payload("Bash", {
         "command": "rm -rf /toolkit-canary-does-not-exist"}),
     _canary_payload("Bash", {"command": "echo toolkit-canary"}),
     None),
    ("tools/model-guard/claude-model-guard",
     _canary_payload("Agent", {"subagent_type": _CANARY_AGENT,
                               "prompt": "canary", "description": "canary"}),
     _canary_payload("Agent", {"subagent_type": _CANARY_AGENT,
                               "prompt": "canary", "description": "canary",
                               "model": "sonnet"}),
     "model_guard"),
)


def _env():
    env = dict(os.environ)
    for var in _UNSET_GIT_VARS:
        env.pop(var, None)
    return env


def set_deadline(monotonic_deadline):
    global _deadline
    _deadline = monotonic_deadline


def _git_timeout():
    # The watchdog's os._exit cannot reap a child, so a git still running when
    # it fires is orphaned; end each call (subprocess.run kills and reaps on its
    # own timeout) before the watchdog does.
    if _deadline is None:
        return GIT_TIMEOUT_SECONDS
    return min(GIT_TIMEOUT_SECONDS,
               _deadline - time.monotonic() - GIT_KILL_MARGIN_SECONDS)


def _git(cwd: str, *args: str) -> Optional[str]:
    """Read-only git in `cwd`, or None on any failure.

    An empty `cwd` returns None rather than running: `git -C ""` is a silent
    no-op that describes the process's actual working directory and exits 0,
    so without this the answer would be about a directory never examined.
    Three files in this repo have been bitten by it.
    """
    if not cwd:
        return None
    timeout = _git_timeout()
    if timeout < MIN_GIT_SECONDS:
        return None
    try:
        done = subprocess.run(
            ("git", "-C", cwd) + args,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=timeout, env=_env())
    except Exception:
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", "replace").strip()


# --------------------------------------------------------- check A, part 1 ---
# What the machine is configured to run, and whether it can.

def settings_path(home: str) -> str:
    return os.path.join(home, ".claude", "settings.json")


def load_settings(path: str) -> Optional[dict]:
    """The settings object, or None when it cannot be read.

    None and `{}` are different and must not be collapsed: None is "cannot
    tell", which produces silence, while `{}` is a real settings file that
    declares no hooks -- a machine where nothing is installed, which is also
    silence but for a reason a future reader may want to act on differently.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def declared_commands(settings: Optional[dict]) -> list:
    """Every command string Claude Code is configured to execute.

    Returns [(where, command)]. `where` is the event name, or `statusLine` --
    which is included because install.sh merges it from the same file and a
    dead status line is as quiet as a dead hook.

    Every level is isinstance-guarded. A malformed settings file must produce
    a short list, never a raise inside a hook.
    """
    out = []
    if not isinstance(settings, dict):
        return out

    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event, matchers in hooks.items():
            if not isinstance(matchers, list):
                continue
            for matcher in matchers:
                if not isinstance(matcher, dict):
                    continue
                entries = matcher.get("hooks")
                if not isinstance(entries, list):
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    command = entry.get("command")
                    if isinstance(command, str) and command.strip():
                        out.append((str(event), command))

    line = settings.get("statusLine")
    if isinstance(line, dict):
        command = line.get("command")
        if isinstance(command, str) and command.strip():
            out.append(("statusLine", command))
    return out


def command_target(command: str) -> Optional[str]:
    """The executable a command string names, or None when it is not a path.

    `shlex` rather than `.split()`, because a quoted path containing a space
    is one token and splitting on whitespace would truncate it to a prefix
    that does not exist -- reporting a healthy hook as missing, which is the
    false-alarm direction that gets a notice disabled.

    Only ABSOLUTE paths are returned. A bare `pgrep`, or anything resolved
    through PATH, is not this tool's business: PATH differs between the shell
    a person tests in and the one Claude Code runs hooks in, so a verdict
    about it would be wrong as often as right.
    """
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        parts = shlex.split(command)
    except Exception:
        parts = command.split()
    if not parts:
        return None
    first = os.path.expanduser(parts[0])
    return first if os.path.isabs(first) else None


def unrunnable_hooks(home: str, settings: Optional[dict] = None,
                     repo: Optional[str] = None) -> list:
    """Commands the machine is configured to run and cannot.

    Each record is {"where", "command", "path", "target", "why"}. `why` is
    "missing" or "not executable" -- Claude Code's behaviour is identical for
    both (it skips the hook silently), but the remedies are not: one is a
    reinstall, the other a chmod.

    `target` is the symlink's destination when the path is a link, and empty
    otherwise. It is what tells a reinstall from a real problem: a link into a
    deleted worktree and a link into a renamed file look the same until you can
    see where it points.

    With `repo`, every registration in the toolkit-core plugin's manifest is
    checked too, as `plugin:<event>`; an unreadable manifest is one record
    with why "unreadable", never a raise (this runs on every prompt submit).
    """
    if settings is None:
        settings = load_settings(settings_path(home))
    problems = []
    for where, command in declared_commands(settings):
        path = command_target(command)
        if path is None:
            continue
        # `exists` follows symlinks, so a dangling link lands here as missing,
        # which is what it is from Claude Code's point of view.
        if not os.path.exists(path):
            why = "missing"
        elif not os.access(path, os.X_OK):
            why = "not executable"
        else:
            continue
        target = ""
        try:
            if os.path.islink(path):
                target = os.readlink(path)
        except Exception:
            target = ""
        problems.append({"where": where, "command": command, "path": path,
                         "target": target, "why": why})
    if repo:
        problems.extend(_plugin_problems(repo))
    return problems


def _plugin_problems(repo: str) -> list:
    path = os.path.join(repo, PLUGIN_HOOKS)
    if _manifest is None:
        return [{"where": "plugin", "command": "", "path": path,
                 "target": "", "why": "unreadable"}]
    try:
        rows = _manifest.plugin_registrations(repo)
        resolved = [_manifest.resolve(repo, row["relpath"], row["profile"])
                    for row in rows]
    except _manifest.ManifestError:
        return [{"where": "plugin", "command": "", "path": path,
                 "target": "", "why": "unreadable"}]
    problems = []
    for row, target_path in zip(rows, resolved):
        if not os.path.exists(target_path):
            why = "missing"
        elif not os.access(target_path, os.X_OK):
            why = "not executable"
        else:
            continue
        target = ""
        try:
            if os.path.islink(target_path):
                target = os.readlink(target_path)
        except Exception:
            target = ""
        prefix = ("plugin" if row["profile"] == "core"
                  else "plugin-" + row["profile"])
        problems.append({"where": prefix + ":" + row["event"],
                         "command": row["command"], "path": target_path,
                         "target": target, "why": why})
    return problems


def matcher_covers(matcher, tool: str) -> bool:
    """Does a registration's `matcher` fire for `tool`?

    A missing, empty or "*" matcher fires for everything. A wildcard
    REQUIREMENT ("*") is stricter: only an unrestricted registration meets it,
    never a named list. Otherwise the matcher is a regex anchored on both ends,
    as Claude Code treats it; a pattern that does not compile covers nothing.
    """
    if matcher is None or matcher in ("", "*"):
        return True
    if tool == "*":
        return False
    try:
        return re.fullmatch(matcher, tool) is not None
    except (re.error, TypeError):
        return False


def required_profiles(repo: str, home: str):
    """(names, problems): the profiles whose guards this machine must run.

    The union of the profiles the toolkit config selects and the plugins
    `~/.claude/settings.json` has enabled. Any error reading either is a
    `{"why": ...}` problem and a None set, which means "require every guard":
    the strict direction, because a selection nobody can read must not become a
    reason to stop checking a guard. A repo with no registry is the pre-profile
    layout and needs only core.
    """
    if _profiles is None or _manifest is None:
        return None, [{"why": "lib/profiles.py unavailable"}]
    if not os.path.exists(os.path.join(repo, _profiles.REGISTRY)):
        return {"core"}, []
    try:
        registry = _profiles.load_registry(repo)
    except _profiles.RegistryError as exc:
        return None, [{"why": str(exc)}]
    names, error, _defaulted = _profiles.selected(repo)
    if error:
        return None, [{"why": error}]
    # No settings file yet is "nothing enabled", not "unreadable": a fresh
    # machine has none until install.sh writes one, and that is not damage.
    path = settings_path(home)
    settings = load_settings(path) if os.path.lexists(path) else {}
    enabled, error = _profiles.enabled_profiles(registry, settings)
    if error:
        return None, [{"why": error}]
    return set(names) | enabled, []


def coverage_problems(repo: str, home: Optional[str] = None) -> list:
    """Required guards the manifest no longer registers on every tool they cover.

    Presence is not protection: a guard whose registration was removed or whose
    matcher was narrowed still exists, is still executable, and never runs.
    Registrations are unioned per guard and event, so a guard registered twice
    with split matchers counts. An unreadable manifest returns [] -- that is
    `_plugin_problems`' `unreadable`, and it is reported there.

    A guard is checked against its own profile's registrations, and only when
    that profile is required on this machine (see `required_profiles`). When
    that cannot be told, every guard is checked, but still only against its own
    profile's registrations: another profile registering the command does not
    protect the tools this one covers.
    """
    if _manifest is None:
        return []
    try:
        rows = _manifest.plugin_registrations(repo)
    except _manifest.ManifestError:
        return []
    required, _problems = required_profiles(
        repo, home or os.path.expanduser("~"))
    found = []
    for profile, relpath, event, tools in REQUIRED_GUARDS:
        if required is not None and profile not in required:
            continue
        mine = [r for r in rows
                if r["relpath"] == relpath and r["event"] == event
                and r["profile"] == profile]
        missing = [tool for tool in tools
                   if not any(matcher_covers(r["matcher"], tool) for r in mine)]
        if missing:
            found.append({"guard": relpath, "event": event,
                          "missing": missing,
                          "why": "matcher too narrow" if mine
                          else "unregistered"})
    return found


# --------------------------------------------------------- check A, part 2 ---
# What is actually on disk in the install directories.

def owns(repo: Optional[str], link: str, target: str) -> Optional[bool]:
    """Whether `install.sh` would recognise this link as its own, or None.

    The same test the installer's prune loop uses: the link's target sits under
    `$REPO`. It matters because the two cases have DIFFERENT remedies -- rerunning
    the installer fixes one and provably cannot touch the other, since that loop
    skips a foreign target as "not ours". Telling someone to run install.sh for
    a link it will not consider is advice that cannot work, repeated every new
    session, which is how a notice teaches people to skim past it.

    A relative target is resolved against the link's own directory first;
    `os.readlink` returns it exactly as written and a raw prefix compare would
    call every relative link foreign.

    None when the repo is unknown, and the caller then says nothing about
    ownership rather than guessing.
    """
    if not repo or not target:
        return None
    resolved = target
    if not os.path.isabs(resolved):
        resolved = os.path.normpath(os.path.join(os.path.dirname(link), resolved))
    repo = repo.rstrip(os.sep)
    return resolved == repo or resolved.startswith(repo + os.sep)


def broken_links(home: str, repo: Optional[str] = None) -> list:
    """Symlinks in the install directories that no longer resolve.

    Each record is {"link", "target", "why", "ours"}. Enumerating the
    filesystem rather than any declared list is the entire point -- see the
    module docstring.

    A link the toolkit does not own is reported too, and flagged. `install.sh`'s
    prune loop skips those deliberately, because it DELETES and must only delete
    what it put there; this only reports, and a dangling entry in `~/.local/bin`
    is a defect whoever made it. But the remedy differs, so the record carries
    which it is.
    """
    problems = []
    for relative in LINK_DIRS:
        directory = os.path.join(home, relative)
        try:
            entries = sorted(os.listdir(directory))
        except Exception:
            continue                            # absent dir is not a problem
        for name in entries:
            link = os.path.join(directory, name)
            try:
                if not os.path.islink(link):
                    continue
                target = os.readlink(link)
                if not os.path.exists(link):
                    why = "target is missing"
                elif relative == EXECUTABLE_DIR and not os.access(link, os.X_OK):
                    why = "target is not executable"
                else:
                    continue
            except Exception:
                continue
            problems.append({"link": link, "target": target, "why": why,
                             "ours": owns(repo, link, target)})
    return problems


def integrity_problems(home: str, settings: Optional[dict] = None,
                       repo: Optional[str] = None) -> dict:
    """Both checks, kept as separate lists.

    Returned as a dict of two lists rather than one merged list so a caller --
    and a test -- can still tell which check found what. Merging them here is
    how the two would start hiding each other.
    """
    return {"hooks": unrunnable_hooks(home, settings, repo),
            "links": broken_links(home, repo),
            "coverage": coverage_problems(repo, home) if repo else [],
            "profiles": required_profiles(repo, home)[1] if repo else []}


def problems_signature(problems: dict) -> str:
    """A stable key for "this exact set of problems".

    Deduping on this rather than on a bare boolean is what lets the check speak
    again when a NEW link breaks mid-session -- which is the case the whole
    mid-session half exists for. A boolean would say it once and then stay
    quiet through every subsequent break.
    """
    parts = []
    for record in (problems or {}).get("hooks", []):
        parts.append("hook\t%s\t%s\t%s" % (record.get("where"),
                                           record.get("path"),
                                           record.get("why")))
    for record in (problems or {}).get("links", []):
        parts.append("link\t%s\t%s" % (record.get("link"), record.get("why")))
    for record in (problems or {}).get("coverage", []):
        parts.append("coverage\t%s\t%s\t%s\t%s" % (
            record.get("guard"), record.get("event"), record.get("why"),
            ",".join(record.get("missing", []))))
    for record in (problems or {}).get("profiles", []):
        parts.append("profiles\t%s" % record.get("why"))
    if not parts:
        return ""
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()[:16]


_INTEGRITY_HEADER = "=== TOOLKIT INTEGRITY: SOMETHING INSTALLED CANNOT RUN ==="


def render_integrity(problems: dict, home: str = "") -> str:
    """The loud half. Empty string when everything resolves."""
    hooks = list((problems or {}).get("hooks", []))
    links = list((problems or {}).get("links", []))
    coverage = list((problems or {}).get("coverage", []))
    profiles = list((problems or {}).get("profiles", []))
    if not hooks and not links and not coverage and not profiles:
        return ""

    def short(path):
        return path.replace(home, "~", 1) if home and path.startswith(home) else path

    lines = [_INTEGRITY_HEADER, ""]

    if hooks:
        lines.append("Claude Code SKIPS a hook it cannot execute, and reports "
                     "nothing when it does.")
        lines.append("These are configured in ~/.claude/settings.json or the "
                     "toolkit-core plugin and will "
                     "not run:")
        lines.append("")
        for record in hooks:
            lines.append("  %-14s %s" % (record["where"] + ":",
                                         short(record["path"])))
            if record.get("target"):
                lines.append("  %-14s   -> %s (%s)"
                             % ("", record["target"], record["why"]))
            else:
                lines.append("  %-14s   %s" % ("", record["why"]))
        lines.append("")
        lines.append("If any of those is a guard, it is already dark - it is "
                     "not quiet because")
        lines.append("there is nothing to report.")
        lines.append("")

    named = {record["path"] for record in hooks}
    rest = [record for record in links if record["link"] not in named]
    if rest:
        lines.append("Broken symlinks in the install directories:")
        lines.append("")
        for record in rest:
            lines.append("  %s" % short(record["link"]))
            lines.append("      -> %s (%s)" % (record["target"], record["why"]))
            if record.get("ours") is False:
                lines.append("      NOT this toolkit's - install.sh will not "
                             "touch it")
        lines.append("")

    if hooks or links:
        lines.append("Fix: run ./install.sh from the PRIMARY checkout of "
                     "claude-code-toolkit")
        lines.append("(never from a worktree), then start a new session.")
    # Said only when it is true. install.sh's prune loop skips any link whose
    # target is not under its own $REPO, so for a foreign link the line above
    # is advice that provably cannot work -- and dedupe is per session, so it
    # would be repeated in every new session forever. A remedy that does
    # nothing is how a notice trains its reader to skip it, which is the same
    # failure the drift half is path-filtered to avoid.
    if any(record.get("ours") is False for record in rest):
        lines.append("")
        lines.append("The link(s) marked NOT this toolkit's are the "
                     "exception: install.sh only ever")
        lines.append("removes links pointing into its own checkout. Delete "
                     "those by hand, or fix")
        lines.append("whatever they point at.")
    if coverage:
        if hooks or links:
            lines.append("")
        lines.append("Guards the plugin no longer runs on every tool they "
                     "must cover:")
        lines.append("")
        for record in coverage:
            lines.append("  %s (%s): %s - missing %s" % (
                os.path.basename(record["guard"]), record["event"],
                record["why"], ", ".join(record["missing"])))
        lines.append("")
        lines.append(_REPAIR_ADVICE)
    if profiles:
        if hooks or links or coverage:
            lines.append("")
        lines.append("The toolkit's profile selection could not be read, so "
                     "every guard is being")
        lines.append("required as a precaution:")
        lines.append("")
        for record in profiles:
            lines.append("  %s" % record["why"])
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------- canaries (A, 3) ---
# Run the guard executables on harmless payloads. SessionStart only: this is
# the one part of the integrity half that spends a subprocess.

def _canary_env(extra_env=None) -> dict:
    """The environment a canary runs in: the hatches and every GIT_* variable
    (an inherited GIT_DIR points a fixture at another repository) removed, then
    the probe's own variables added."""
    env = {k: v for k, v in os.environ.items()
           if k not in CANARY_STRIPPED_ENV and not k.startswith("GIT_")}
    env.update(extra_env or {})
    return env


def run_canary(path: str, payload: dict, timeout: float,
               extra_env: Optional[dict] = None) -> str:
    """"denied", "allowed", TIMED_OUT or UNCHECKED for one payload."""
    env = _canary_env(extra_env)
    try:
        done = subprocess.run([path], input=json.dumps(payload),
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return TIMED_OUT
    except (subprocess.SubprocessError, OSError, ValueError):
        return "unchecked"
    if done.returncode == 2:
        return "denied"
    if done.returncode != 0:
        return "unchecked"
    out = (done.stdout or "").strip()
    if not out:
        return "allowed"
    try:
        data = json.loads(out)
    except ValueError:
        return "unchecked"
    if not isinstance(data, dict):
        return "unchecked"
    specific = data.get("hookSpecificOutput")
    decision = (specific.get("permissionDecision")
                if isinstance(specific, dict) else None)
    if decision == "deny":
        return "denied"
    # A crash report is a systemMessage with no decision: not an allow.
    if (decision is None and "permissionDecision" not in data
            and "systemMessage" not in data):
        return "allowed"
    return "unchecked"


def canary_problems(repo: str, deadline: float,
                    home: Optional[str] = None) -> list:
    """Probes whose verdict was not the expected one.

    `deadline` is a time.monotonic() value. Once too little of it remains the
    rest are recorded TIMED_OUT without being run. A guard that is missing or
    not executable is skipped: `_plugin_problems` already reports it. So is a
    guard whose profile this machine neither selects nor enables; when that
    cannot be told, every canary runs.
    """
    if _manifest is None:
        return []
    records = []
    home_dir = os.path.expanduser("~")
    required, _problems = required_profiles(repo, home or home_dir)
    guard_profile = {relpath: profile
                     for profile, relpath, _e, _t in reversed(REQUIRED_GUARDS)}
    for relpath, deny_payload, allow_payload, section in CANARIES:
        profile = guard_profile.get(relpath, "core")
        if required is not None and profile not in required:
            continue
        # A guard switched off in config is a visible choice, not checkout
        # damage (same reasoning as CANARY_STRIPPED_ENV). `is False`, exactly
        # as claude-model-guard reads it.
        if section is not None and _config is not None:
            try:
                if _config.get(section, "enabled", default=True) is False:
                    continue
            except Exception:
                pass
        try:
            path = _manifest.resolve(repo, relpath, profile)
        except _manifest.ManifestError:
            continue                # an unreadable registry is reported elsewhere
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        for probe, payload, expected in (("deny", deny_payload, "denied"),
                                         ("allow", allow_payload, "allowed")):
            timeout = min(CANARY_MAX_SECONDS,
                          deadline - time.monotonic() - CANARY_MARGIN_SECONDS)
            if timeout <= 0:
                got = TIMED_OUT
            else:
                got = run_canary(path, dict(payload, cwd=home_dir), timeout)
            if got != expected:
                records.append({"guard": os.path.basename(path),
                                "probe": probe, "got": got})
    return records


def _session_claim_canary_supported() -> bool:
    # session-claim identifies its caller by shelling out to `ps`, which never
    # resolves on native Windows: the guard is inert there by construction.
    return os.name != "nt"


STATEFUL_CANARY_GUARDS = (
    # (plugin-relative path, profile)
    ("tools/worktree-guard/claude-worktree-guard", "core"),
    ("tools/distill-guard/claude-distill-write-guard", "core"),
    ("tools/session-claim/claude-session-claim", "builder"),
)
_CLAIM_RELPATH = "tools/session-claim/claude-session-claim"


class _FixtureError(Exception):
    pass


class _FixtureTimeout(_FixtureError):
    """The fixture ran out of time: its probes are TIMED_OUT, not UNCHECKED."""


def _fixture_timeout(deadline: float) -> float:
    return min(CANARY_MAX_SECONDS,
               deadline - time.monotonic() - CANARY_MARGIN_SECONDS)


def _fixture_step(argv: list, deadline: float) -> None:
    """One fixture command; raises _FixtureError on any failure, and its
    _FixtureTimeout subclass when the budget is spent or the step hits it."""
    timeout = _fixture_timeout(deadline)
    if timeout <= 0:
        raise _FixtureTimeout("no time left")
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout,
                              env=_canary_env())
    except subprocess.TimeoutExpired as exc:
        raise _FixtureTimeout(str(exc))
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        raise _FixtureError(str(exc))
    if done.returncode != 0:
        raise _FixtureError("%s exited %d" % (argv[:2], done.returncode))


def _fixture_repo(path: str, no_hooks: str, deadline: float,
                  commit: bool = False) -> None:
    # A local core.hooksPath keeps the user's global git hooks out of every
    # later git call on this repo, the guard's own `worktree add` included.
    base = ["git", "-c", "init.defaultBranch=main",
            "-c", "core.hooksPath=" + no_hooks]
    _fixture_step(base + ["init", "-q", path], deadline)
    _fixture_step(base + ["-C", path, "config", "core.hooksPath", no_hooks],
                  deadline)
    if commit:
        _fixture_step(base + ["-C", path,
                              "-c", "user.name=toolkit-canary",
                              "-c", "user.email=canary@invalid",
                              "-c", "commit.gpgsign=false",
                              "commit", "-q", "--allow-empty", "-m", "canary"],
                      deadline)


def _stateful_probes(wanted: set, root: str, deadline: float) -> dict:
    """relpath -> (deny payload, allow payload, extra env), built under `root`.

    Raises _FixtureError when any step fails."""
    no_hooks = os.path.join(root, "no-hooks")
    os.makedirs(no_hooks)
    probes = {}
    for relpath in wanted:
        if relpath == STATEFUL_CANARY_GUARDS[0][0]:
            one = os.path.join(root, "worktree-a")
            two = os.path.join(root, "worktree-b")
            _fixture_repo(one, no_hooks, deadline)
            _fixture_repo(two, no_hooks, deadline)
            probes[relpath] = (
                dict(_canary_payload("Bash", {"command":
                     "git -C %s commit --allow-empty -m canary"
                     % shlex.quote(two)}), cwd=one),
                dict(_canary_payload("Bash", {"command":
                     "git -C %s status" % shlex.quote(one)}), cwd=one),
                None)
        elif relpath == STATEFUL_CANARY_GUARDS[1][0]:
            folder = os.path.join(root, "distill")
            os.makedirs(folder)
            with open(os.path.join(folder, "CLAUDE.md"), "w",
                      encoding="utf-8") as handle:
                handle.write("toolkit canary\n")
            probes[relpath] = (
                dict(_canary_payload("Write", {
                    "file_path": os.path.join(folder, "CLAUDE.md"),
                    "content": "canary"}), cwd=folder),
                dict(_canary_payload("Write", {
                    "file_path": os.path.join(folder, "canary-new.txt"),
                    "content": "canary"}), cwd=folder),
                None)
        else:
            held = os.path.join(root, "claim-held")
            free = os.path.join(root, "claim-free")
            state = os.path.join(root, "claim-state")
            _fixture_repo(held, no_hooks, deadline, commit=True)
            _fixture_repo(free, no_hooks, deadline)
            os.makedirs(state)
            # The holder is this process, never the owner (the claude ancestor,
            # or CLAIM_TEST_OWNER_PID in tests), so the guard sees another
            # live session holding `held`.
            with open(os.path.join(state, "claims.json"), "w",
                      encoding="utf-8") as handle:
                json.dump([{"checkout": held, "pid": os.getpid(),
                            "proc_start": None,
                            "name": "toolkit-canary-holder",
                            "session_id": "toolkit-canary-holder",
                            "transcript_path": "/toolkit-canary/holder"}],
                          handle)
            probes[relpath] = (
                dict(_canary_payload("Write", {
                    "file_path": os.path.join(held, "canary.txt"),
                    "content": "canary"}), cwd=held),
                dict(_canary_payload("Write", {
                    "file_path": os.path.join(free, "canary.txt"),
                    "content": "canary"}), cwd=free),
                {"CLAIM_CLAIMS_PATH": os.path.join(state, "claims.json"),
                 "CLAIM_BREAKER_PATH": os.path.join(state, "breaker.json"),
                 "CLAIM_LOG_PATH": os.path.join(state, "decisions.log")})
    return probes


def _last_claim_decision(extra_env: dict) -> Optional[dict]:
    """The last line of the fixture's session-claim log, or None."""
    try:
        with open(extra_env["CLAIM_LOG_PATH"], encoding="utf-8",
                  errors="replace") as handle:
            lines = [ln for ln in handle.read().splitlines() if ln.strip()]
        data = json.loads(lines[-1])
    except (OSError, ValueError, IndexError):
        return None
    return data if isinstance(data, dict) else None


def stateful_canary_problems(repo: str, deadline: float,
                             home: Optional[str] = None,
                             tmp_parent: Optional[str] = None) -> list:
    """canary_problems for the guards whose verdict depends on machine state.

    worktree-guard, distill-write-guard and session-claim are probed against a
    throwaway fixture under `tmp_parent` (default: the system temp dir), removed
    on every path. Gated like canary_problems; the fixture is not built at all
    when no guard survives. If the fixture cannot be built, every probe that
    would have run is recorded and no guard runs: TIMED_OUT when it ran out of
    time, UNCHECKED when a step failed.
    """
    if _manifest is None:
        return []
    home_dir = os.path.expanduser("~")
    required, _problems = required_profiles(repo, home or home_dir)
    selected = []
    for relpath, profile in STATEFUL_CANARY_GUARDS:
        if required is not None and profile not in required:
            continue
        if relpath == _CLAIM_RELPATH:
            if not _session_claim_canary_supported():
                continue
            if _config is not None:
                try:
                    if _config.get("session_claim", "enabled",
                                   default=True) is False:
                        continue
                except Exception:
                    pass
        try:
            path = _manifest.resolve(repo, relpath, profile)
        except _manifest.ManifestError:
            continue                # an unreadable registry is reported elsewhere
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        selected.append((relpath, path))
    if not selected:
        return []

    def every_probe(got):
        # `cause`: no guard ran, so an UNCHECKED here is not a guard's crash.
        return [{"guard": os.path.basename(path), "probe": probe, "got": got,
                 "cause": "fixture"}
                for _relpath, path in selected for probe in ("deny", "allow")]

    try:
        root = tempfile.mkdtemp(prefix="toolkit-canary-", dir=tmp_parent)
    except OSError:
        return every_probe(UNCHECKED)
    try:
        # realpath: the claim guard compares a realpath-derived toplevel with
        # the record's string, and macOS $TMPDIR sits under a /var symlink.
        root = os.path.realpath(root)
        try:
            probes = _stateful_probes({r for r, _p in selected}, root, deadline)
        except _FixtureTimeout:
            return every_probe(TIMED_OUT)
        except (_FixtureError, OSError):
            return every_probe(UNCHECKED)
        records = []
        for relpath, path in selected:
            deny_payload, allow_payload, extra_env = probes[relpath]
            for probe, payload, expected in (
                    ("deny", deny_payload, "denied"),
                    ("allow", allow_payload, "allowed")):
                timeout = _fixture_timeout(deadline)
                if timeout <= 0:
                    got = TIMED_OUT
                else:
                    got = run_canary(path, payload, timeout,
                                     extra_env=extra_env)
                record = {"guard": os.path.basename(path), "probe": probe,
                          "got": got}
                if (relpath == _CLAIM_RELPATH and probe == "deny"
                        and got == "allowed"):
                    last = _last_claim_decision(extra_env)
                    if last is not None and last.get("decision") == "allow-uncertain":
                        record["got"] = "unidentified"
                        record["detail"] = str(last.get("reason") or "unknown")
                if record["got"] != expected:
                    records.append(record)
        return records
    finally:
        shutil.rmtree(root, ignore_errors=True)


def canaries_signature(records: list) -> str:
    """Empty when there is nothing to report, else a stable hash of the records."""
    if not records:
        return ""
    lines = sorted("%s\t%s\t%s" % (r["guard"], r["probe"], r["got"])
                   for r in records)
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]


_CANARY_HEADER = "=== TOOLKIT INTEGRITY: A GUARD FAILED ITS CANARY ==="
# Only TIMED_OUT records: still printed, because silence must never read as a
# passed canary, but without the FAILED header or the repair advice, which
# would send someone hunting for a change that does not exist.
_TIMED_OUT_HEADER = ("=== TOOLKIT INTEGRITY: GUARD CANARIES NOT CHECKED "
                     "(OUT OF TIME) ===")
_TIMED_OUT_ADVICE = (
    "The machine was too busy to finish these canaries inside the hook's time "
    "budget. Nothing points to a changed guard, and they run again next "
    "session. If this repeats on an idle machine, a guard may be hanging: tell "
    "the toolkit maintainer.")


def render_canaries(records: list) -> str:
    if not records:
        return ""
    timed_out_only = all(r["got"] == TIMED_OUT for r in records)
    lines = [_TIMED_OUT_HEADER if timed_out_only else _CANARY_HEADER, ""]
    for record in records:
        guard, probe, got = record["guard"], record["probe"], record["got"]
        if got == "unidentified":
            lines.append("%s could not tell which session is calling it (%s), "
                         "so it allows every edit - it is NOT protecting this "
                         "machine's checkouts." % (guard, record["detail"]))
        elif got == TIMED_OUT:
            lines.append("%s's %s canary ran out of time and was not checked - "
                         "its behaviour is unverified this session."
                         % (guard, probe))
        elif got == UNCHECKED and record.get("cause") == "fixture":
            lines.append("%s's %s canary could not be checked (its throwaway "
                         "test fixture could not be built, so the guard never "
                         "ran) - its behaviour is unverified this session."
                         % (guard, probe))
        elif got == UNCHECKED:
            lines.append("%s's %s canary returned no verdict (it crashed or "
                         "printed something unreadable) - its behaviour is "
                         "unverified this session." % (guard, probe))
        elif probe == "deny":
            lines.append("%s let a harmless canary through that it must "
                         "refuse - it is NOT protecting this machine." % guard)
        else:
            lines.append("%s refused a harmless canary it must allow - it is "
                         "over-blocking." % guard)
    lines.append("")
    if timed_out_only:
        lines.append(_TIMED_OUT_ADVICE)
    elif any(r["got"] not in ("unidentified", TIMED_OUT) for r in records):
        lines.append(_REPAIR_ADVICE)
    else:
        lines.append("session-claim identifies a session from the process tree "
                     "and ~/.claude/sessions/. Restoring the checkout will not "
                     "fix this: until the reason above is resolved, "
                     "session-claim does not guard edits in the app this "
                     "session runs in (terminal, VS Code, desktop).")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ check B ---
# Has the toolkit moved underneath this session?

def root_and_head(start: str) -> Optional[tuple]:
    """(checkout, HEAD sha) for the tree this tool is installed from, or None.

    `start` comes from this file's own realpath -- never abspath, which does
    not follow the `~/.local/bin` symlink and would resolve into `~/.local/bin`
    rather than into the repo. `git rev-parse` confirms the tree rather than
    trusting directory arithmetic: an unpacked tarball has the identical layout
    and no git at all, and check B must be silent there rather than wrong.

    ONE git call, asking for both, rather than a call each. Not for the speed:
    two calls can straddle another session's merge and return a toplevel and a
    HEAD from either side of it, and this runs on every prompt submit in a repo
    several sessions push to. A single `rev-parse` cannot disagree with itself.

    Returns None if either half is missing -- including a repo with no commits
    yet, where `HEAD` does not resolve and the whole command fails.

    There is deliberately NO `if not start` guard here. `_git` already refuses
    an empty path, and a second guard producing the identical answer is the
    two-mechanisms-one-outcome shape this repo keeps paying for: with both in
    place, a mutation deleting `_git`'s guard -- the one that protects every
    caller -- left this function's test green. One guard, at the boundary,
    exercised by the tests that reach it.
    """
    out = _git(start, "rev-parse", "--show-toplevel", "HEAD")
    if not out:
        return None
    lines = [line for line in out.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    root, head = lines[0].strip(), lines[1].strip()
    return (root, head) if root and head else None


# A path with one of these statuses did not exist at its current location in
# `old`, so nothing on this machine can be pointing at it unless the installer
# has run since. `R`ename and `C`opy are here with `A`dd on purpose: a renamed
# file's DESTINATION is as unlinked as a brand-new one, and only its source half
# leaves a dangling link for `broken_links` to find.
ADDED_STATUSES = ("A", "C", "R")


def is_added(status: str) -> bool:
    """Whether a `--name-status` letter means "this path is new here".

    Scores off the first character alone because git suffixes similarity to
    rename and copy statuses -- `R100`, `C075` -- and an exact-match test would
    silently classify every rename as an ordinary edit.
    """
    return isinstance(status, str) and status[:1] in ADDED_STATUSES


def changed_paths(repo: str, old: str, new: str) -> Optional[list]:
    """(status, path) pairs between two commits, or None when git cannot say.

    None rather than [] on failure. They mean opposite things -- "could not
    look" against "looked, nothing changed" -- and the caller reports the first
    and stays silent on the second. An `old` sha that has been garbage
    collected or force-pushed away is the realistic way to reach None.

    `--name-status`, not `--name-only`, for one reason: the drift notice used to
    tell every reader that the code was "already live", which is true of a file
    behind an existing symlink and FALSE of one that was added. A pull creates
    no symlink, so an added tool is inert until `install.sh` runs, and the
    notice was reassuring people in exactly that case. Same single git call
    either way -- this spends nothing against MAX_GIT_CALLS.

    A rename prints `R100\\told\\tnew`, so the path is the LAST field, not the
    second. Taking `parts[1]` would report the vanished source path as the thing
    that needs linking.
    """
    if not old or not new:
        return None
    out = _git(repo, "diff", "--name-status", "%s..%s" % (old, new))
    if out is None:
        return None
    pairs = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue                        # a status with no path is not one
        status, path = parts[0].strip(), parts[-1].strip()
        if status and path:
            pairs.append((status, path))
    return pairs


def live_changes(pairs) -> list:
    """The (status, path) subset that changes what a running session executes.

    Without this filter the notice fires on every docs merge, and a notice that
    fires constantly is one that gets skimmed past -- which costs more than not
    having it, because the reader stops looking at the time it matters.

    Takes and returns pairs rather than bare paths so the added/changed
    distinction survives the filter. Dropping the status here and re-deriving it
    later would need a second git call, which the hook's timeout budget does not
    have.
    """
    if not pairs:
        return []
    keep = []
    for pair in pairs:
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            continue
        status, path = pair
        if not isinstance(path, str) or not isinstance(status, str):
            continue
        if any(marker in path for marker in DEAD_MARKERS):
            continue
        if (path in LIVE_FILES or LIVE_MANIFEST_RE.match(path)
                or path.startswith(LIVE_PREFIXES)):
            keep.append((status, path))
    return keep


_DRIFT_HEADER = "=== THE TOOLKIT MOVED UNDER THIS SESSION ==="


def render_drift(old: str, new: str, live, undiffable: bool = False) -> str:
    """The quiet half. Empty string when nothing running changed."""
    if undiffable:
        return ("%s\n\n"
                "%s -> %s. What changed could not be listed, so whether\n"
                "anything you are running moved is unknown.\n\n"
                % (_DRIFT_HEADER, (old or "?")[:12], (new or "?")[:12]))
    live = list(live or [])
    if not live:
        return ""
    added = [path for status, path in live if is_added(status)]
    paths = [path for _, path in live]
    lines = [_DRIFT_HEADER, ""]
    lines.append("%s -> %s. These paths changed, and the hooks, skills and "
                 "agents" % (old[:12], new[:12]))
    lines.append("on this machine load from that checkout through the "
                 "toolkit's plugins:")
    lines.append("")
    for status, path in live:
        lines.append("  %s%s" % (path, "   (new)" if is_added(status) else ""))
    lines.append("")
    # The scope of this sentence is the whole point of carrying the status this
    # far. It says "changed", not "these", because the added ones below are the
    # case where it is false -- and a blanket version of it is what this notice
    # once said, reassuring the reader precisely when a newly added
    # tool was sitting in the checkout with nothing pointing at it.
    lines.append("A hook re-execs per tool call, so CHANGED files are already "
                 "live. Nothing")
    lines.append("about your own branch or edits changed - only the tooling "
                 "around them.")
    if added:
        lines.append("")
        # This half knows a path is NEW, not whether it is one of the entry
        # points install.sh links -- and the gap is not academic. `lib/` is in
        # LIVE_PREFIXES and has ZERO link calls, so a new lib module renders
        # (new) while being live the moment it is imported; most files under
        # tools/ are the same, reached through an already-linked script. So the
        # text states the CONDITION and hands off, rather than asserting a
        # machine state it cannot support. Closing that gap here would mean
        # duplicating install.sh's inventory, which is the hazard the module
        # docstring names -- and an unconditional "this is inert" would be a
        # fresh false claim inside the very block that exists to remove one.
        lines.append("The paths marked (new) did not exist before, and a pull "
                     "creates no symlink.")
        lines.append("A new skill or agent needs /reload-plugins or a new "
                     "session. A new")
        lines.append("~/.local/bin entry point needs the installer to link "
                     "it; this checks it in")
        lines.append("about a second, and changes nothing. A library reached "
                     "through an")
        lines.append("already-linked script needs nothing:")
        lines.append("")
        lines.append("  ./install.sh --links-only        # from the PRIMARY "
                     "checkout")
    for path in paths:
        if LIVE_MANIFEST_RE.match(path) or path == "claude/profiles.json":
            lines.append("")
            lines.append("%s changed: run /reload-plugins or start a new"
                         % path)
            lines.append("session before it takes effect.")
    lines.append("")
    return "\n".join(lines)


# -------------------------------------------------------------------- config --

def is_enabled() -> bool:
    """False only when explicitly disabled. Absent config means on."""
    if _config is None:
        return True
    try:
        return _config.get("toolkit_integrity", "enabled",
                           default=True) is not False
    except Exception:
        return True


def drift_enabled() -> bool:
    """The quiet half can be turned off on its own.

    Separate from `is_enabled` because the two halves have different value:
    check A catches a dark guard and should be very hard to lose, check B is a
    convenience. Someone who finds the drift notice noisy should be able to
    silence it without also silencing the one that matters.
    """
    if _config is None:
        return True
    try:
        return _config.get("toolkit_integrity", "drift_notice",
                           default=True) is not False
    except Exception:
        return True


# --------------------------------------------------------------------- state --

def state_path() -> str:
    if _config is not None:
        try:
            configured = _config.get("toolkit_integrity", "state_path",
                                     default=None)
            if configured:
                return os.path.expanduser(configured)
        except Exception:
            pass
    return os.path.expanduser(DEFAULT_STATE_PATH)


def load_state(path: Optional[str] = None) -> dict:
    try:
        with open(path or state_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def prune_state(state: dict, now: Optional[float] = None) -> dict:
    """Drop records past the TTL, and anything malformed.

    Malformed records are dropped rather than kept: this file is only ever a
    dedupe cache, so discarding one costs a repeated notice while carrying an
    unexpected shape risks a raise inside a hook.
    """
    if now is None:
        now = time.time()
    clean = {}
    for session, keys in (state or {}).items():
        if not isinstance(keys, dict):
            continue
        kept = {}
        for key, record in keys.items():
            if not isinstance(record, dict):
                continue
            stamp = record.get("at")
            if not isinstance(stamp, (int, float)):
                continue
            if now - stamp > STATE_TTL_SECONDS:
                continue
            if not isinstance(record.get("value"), str):
                continue
            kept[key] = {"value": record["value"], "at": stamp}
        if kept:
            clean[session] = kept
    return clean


def last_reported(state: dict, session: str, key: str) -> Optional[str]:
    """The value last announced for this key, or None if there is no record.

    The ONLY place that knows the on-disk shape. Drift needs all three
    answers -- no record, a different record, the same record -- because "no
    record" is the session's first invocation, where the correct behaviour is
    to record a baseline and say nothing rather than to report. A boolean
    cannot carry that, so the hook used to reach into `state[...][...]`
    directly and the shape was known in two places.
    """
    try:
        value = state[session][key]["value"]
    except Exception:
        return None
    return value if isinstance(value, str) else None


def already_reported(state: dict, session: str, key: str, value: str) -> bool:
    """True when this exact value was already announced for this key.

    Keyed on the value rather than a boolean so each check re-fires when its
    subject changes -- a new sha for drift, a new set of problems for
    integrity -- and never repeats on an unchanged one.

    Defined in terms of `last_reported` rather than reading the state itself,
    so there is one reader of the format.
    """
    return last_reported(state, session, key) == value


def record_reported(state: dict, session: str, key: str, value: str,
                    now: Optional[float] = None) -> dict:
    if now is None:
        now = time.time()
    state.setdefault(session, {})[key] = {"value": value, "at": now}
    return state


def save_state(state: dict, path: Optional[str] = None) -> bool:
    """Write the dedupe table. False on any failure; the caller carries on.

    Temp file plus rename, because several sessions write this on their own
    prompt submits and a torn read by the next one would look like corruption
    rather than a race. A lost write costs one repeated notice, which is why
    there is no lock.
    """
    target = path or state_path()
    tmp = target + ".tmp.%d" % os.getpid()
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp, target)
        return True
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        return False


# ---------------------------------------------------------------- hook-fires audit

AUDIT_STATE = os.path.join(".claude", "claude-code-toolkit", "heartbeat-audit.json")
AUDIT_SECONDS = 1.0
AUDIT_STRIKES = 3
AUDIT_REPORTED_DAYS = 30
# A separate file: fires_audit writes its state dict whole, so a shared file
# would lose the other audit's keys.
TOOL_RULES_AUDIT_STATE = os.path.join(".claude", "claude-code-toolkit",
                                      "tool-rules-audit.json")


def _audit_seconds():
    # Test seam, same rule as _run_budget: it can only SHORTEN the budget.
    try:
        wanted = float(os.environ.get("TOOLKIT_INTEGRITY_AUDIT_SECONDS", ""))
    except ValueError:
        return AUDIT_SECONDS
    if wanted >= 0:
        return min(wanted, AUDIT_SECONDS)
    return AUDIT_SECONDS


def _load_audit_state(home, name=AUDIT_STATE):
    empty = {"judged": {}, "reported": {}, "strikes": {}}
    try:
        with open(os.path.join(home, name), encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, ValueError):
        return empty
    if not isinstance(loaded, dict):
        return empty
    for key in empty:
        if not isinstance(loaded.get(key), dict):
            loaded[key] = {}
    return loaded


def save_fires_audit_state(home, state, name=AUDIT_STATE):
    """Atomic write (temp file in the same dir + os.replace); creates the dir."""
    path = os.path.join(home, name)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    handle, temp = tempfile.mkstemp(dir=folder, prefix=".heartbeat-audit.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(state, out)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except OSError:
            pass
        raise


def fires_audit(home, session, deadline, repo, now=None):
    """Earlier sessions that ran shell commands with no toolkit hook heartbeat.

    Returns (notice, new_state). `notice` is "" when nothing is new; `new_state`
    is None only when no state should be written (no install era). Never writes
    the state itself: the hook prints the notice first, then saves.

    Test seam: TOOLKIT_INTEGRITY_AUDIT_CRASH=1 raises RuntimeError, so the hook's
    crash announcement can be exercised.
    """
    if os.environ.get("TOOLKIT_INTEGRITY_AUDIT_CRASH") == "1":
        raise RuntimeError("TOOLKIT_INTEGRITY_AUDIT_CRASH")
    import hook_heartbeat

    now = time.time() if now is None else now
    state = _load_audit_state(home)
    result = hook_heartbeat.audit(
        home, exclude_session=session,
        deadline=min(deadline, time.monotonic() + _audit_seconds()),
        cache=state["judged"], skip=set(state["reported"]), now=now)
    if result["window_start"] is None:
        return "", None

    reported = state["reported"]
    strikes = {}
    items = []     # (key, session, project, text) -- shell commands ran unguarded
    unchecked = []  # same shape -- the audit could not check these
    interrupted = False

    def key_of(entry):
        return entry["session"] or "dir:" + entry["project"]

    for entry in result["flagged"]:
        if entry["session"] in reported:
            continue
        started = (entry["started_at"] or "?")[:10]
        items.append((entry["session"], entry["session"], entry["project"],
                      "started %s  %d shell command(s) ran with no toolkit guard "
                      "check" % (started, entry["ran"])))
    for entry in result["unchecked"]:
        key = key_of(entry)
        if entry["reason"] == "unreadable":
            if key not in reported:
                unchecked.append((key, entry["session"], entry["project"],
                                  "could not be read"))
        elif entry["reason"] == "deadline":
            # Only the FIRST deadline entry was being worked on when time ran
            # out; the rest were merely queued behind it (smallest first), so
            # they keep their count rather than striking out for waiting.
            previous = int(state["strikes"].get(key, 0))
            count = previous if interrupted else previous + 1
            interrupted = True
            if count:
                strikes[key] = count
            if count >= AUDIT_STRIKES and key not in reported:
                size = entry["size"]
                megabytes = "?" if size is None else "%.1f" % (size / 1e6)
                unchecked.append((key, entry["session"], entry["project"],
                              "could not be checked in %d tries (%s MB) -- run "
                              "%s/install.sh --check"
                              % (AUDIT_STRIKES, megabytes, shlex.quote(repo))))

    notice = ""
    if items or unchecked:
        lines = []
        for header, group in (
                ("Toolkit hooks did not fire in %d earlier session(s)", items),
                ("The hook-fires audit could not check %d earlier session(s)",
                 unchecked)):
            if group:
                lines.append(header % len(group))
                for _, sid, project, text in group:
                    lines.append("  %s  %s  %s" % ((sid or "(dir)")[:8], project, text))
        lines.append("Run /toolkit-core:onboard verify in a fresh session. If you ran a session "
                     "without the toolkit on purpose, ignore it.")
        notice = "\n".join(lines) + "\n"

    new_reported = {k: v for k, v in reported.items()
                    if isinstance(v, (int, float))
                    and now - v < AUDIT_REPORTED_DAYS * 86400}
    for key, _, _, _ in items + unchecked:
        new_reported[key] = now
    return notice, {"judged": result["judged"], "reported": new_reported,
                    "strikes": strikes}


# ------------------------------------------------------------ tool-rules audit

def _import_tool_rules_lib():
    """`tool_rules_lib`, found beside this file's realpath -- never via `repo`,
    which is a test seam that may point at a fixture checkout."""
    import sys
    folder = os.path.join(os.path.dirname(os.path.dirname(
        os.path.realpath(__file__))), "tool-rules")
    if folder not in sys.path:
        sys.path.insert(0, folder)
    import tool_rules_lib
    return tool_rules_lib


def tool_rules_era(state, default):
    """The stored era (epoch) from a tool-rules audit state, else `default`."""
    era = state.get("era")
    if isinstance(era, (int, float)) and not isinstance(era, bool):
        return era
    return default


def tool_rules_audit(home, session, deadline, repo, now=None):
    """Earlier sessions where a live tool-rules page should have loaded and left
    no `tool-rules-state` record.

    Returns (notice, new_state), same contract as `fires_audit`; `new_state` is
    None when the audit is inactive (no live pages). Never writes the state.

    Test seam: TOOLKIT_INTEGRITY_TOOL_RULES_AUDIT_CRASH=1 raises RuntimeError.
    """
    if os.environ.get("TOOLKIT_INTEGRITY_TOOL_RULES_AUDIT_CRASH") == "1":
        raise RuntimeError("TOOLKIT_INTEGRITY_TOOL_RULES_AUDIT_CRASH")
    tool_rules_lib = _import_tool_rules_lib()
    import toolkit_config

    now = time.time() if now is None else now
    workspace_root = toolkit_config.workspace_root()
    state = _load_audit_state(home, TOOL_RULES_AUDIT_STATE)
    # The era: this audit's first active run. The hook's durable state ships in
    # the same checkout, so a call before it may predate that -- never judged.
    era = tool_rules_era(state, now)
    result = tool_rules_lib.audit(
        home, workspace_root=workspace_root, exclude_session=session,
        deadline=min(deadline, time.monotonic() + _audit_seconds()),
        cache=state["judged"], skip=set(state["reported"]), now=now, since=era)
    if not result["active"]:
        return "", None

    reported = state["reported"]
    strikes = {}
    items = []     # (key, session, project, text) -- a page should have loaded
    unchecked = []  # same shape -- the audit could not check these
    interrupted = False

    def key_of(entry):
        return entry["session"] or "dir:" + entry["project"]

    for entry in result["flagged"]:
        if entry["session"] in reported:
            continue
        started = (entry["started_at"] or "?")[:10]
        items.append((entry["session"], entry["session"], entry["project"],
                      "started %s  %d matching call(s), no tool-rules record "
                      "(%s)" % (started, entry["calls"],
                                ", ".join(entry["pages"]))))
    for entry in result["unchecked"]:
        key = key_of(entry)
        if entry["reason"] == "unreadable":
            if key not in reported:
                unchecked.append((key, entry["session"], entry["project"],
                                  "could not be read"))
        elif entry["reason"] == "deadline":
            # Only the FIRST deadline entry was being worked on when time ran
            # out; the rest were queued behind it, so they keep their count.
            previous = int(state["strikes"].get(key, 0))
            count = previous if interrupted else previous + 1
            interrupted = True
            if count:
                strikes[key] = count
            if count >= AUDIT_STRIKES and key not in reported:
                size = entry["size"]
                megabytes = "?" if size is None else "%.1f" % (size / 1e6)
                unchecked.append((key, entry["session"], entry["project"],
                                  "could not be checked in %d tries (%s MB) -- "
                                  "run %s/tools/toolkit-integrity/"
                                  "claude-toolkit-integrity --tool-rules-audit"
                                  % (AUDIT_STRIKES, megabytes,
                                     shlex.quote(repo))))

    notice = ""
    if items or unchecked:
        lines = []
        for header, group in (
                ("Tool-rules pages did not load in %d earlier session(s)", items),
                ("The tool-rules audit could not check %d earlier session(s)",
                 unchecked)):
            if group:
                lines.append(header % len(group))
                for _, sid, project, text in group:
                    lines.append("  %s  %s  %s" % ((sid or "(dir)")[:8], project, text))
        pages_dir = tool_rules_lib.tool_rules_dir(workspace_root)
        lines.append("Check that the toolkit-continuity plugin is enabled (/plugin), "
                     "then start a fresh session. Pages: %s." % pages_dir)
        notice = "\n".join(lines) + "\n"

    new_reported = {k: v for k, v in reported.items()
                    if isinstance(v, (int, float))
                    and now - v < AUDIT_REPORTED_DAYS * 86400}
    for key, _, _, _ in items + unchecked:
        new_reported[key] = now
    return notice, {"judged": result["judged"], "reported": new_reported,
                    "strikes": strikes, "era": era}
