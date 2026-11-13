#!/usr/bin/env python3
"""Write and maintain ~/.config/claude-code-toolkit/config.json.

Five ways in, all validated:

    setup_config.py --list-tools                     the tool catalogue, as JSON
    setup_config.py CONFIG REPO [--replace]          interactive first run (a TTY)
    setup_config.py CONFIG REPO --write ...          first run without prompts
    setup_config.py CONFIG REPO --update ...         change an existing config
    setup_config.py CONFIG REPO --report             read-only health report

First-run setup never overwrites: with a config already in place, `--write` and
the interactive form refuse (exit 3) and point at `--update`. `--replace` is the
explicit "redo the questions" on the interactive form, which `install.sh --setup`
passes.

First-run setup asks a few plain questions and writes a MINIMAL config: only
the keys you actually chose, no commented-out cruft. config.example.json
remains the reference for everything else, and is also the TYPE CONTRACT that
`--update --set` checks a value against.

Validation is the point. Hand-editing JSON means a bad workspace root is only
discovered later, when a hook behaves oddly. Here, a path that does not exist is
rejected while the person who typed it is still looking at the screen.

A config written by an older personal copy of this toolkit is read as it is,
never rewritten behind your back: `--report` names what it carries that this
toolkit reads differently, and `--update --migrate` (or `--set`, or
`--accept-core`) applies a change only when you run it.

Exits 0 on success, 1 if the user aborts, 2 on a usage error or when it cannot
prompt (no TTY), 3 when a value is refused (nothing is written).
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import toolkit_config  # noqa: E402  (the one path expander; see expand_path)

HOME = os.path.expanduser("~")

# A workspace root inside one of these would make `rm` inside it look routine to
# the danger guard while actually propagating the delete to every other machine.
CLOUD_MARKERS = ("google drive", "library/cloudstorage", "dropbox",
                 "onedrive", "com~apple~clouddocs", "icloud")

# The tool catalogue: what /onboard explains and offers consent on, grouped
# the way it is presented. Each tier-2 group is capped at 4 tools because
# AskUserQuestion caps a single-select at 4 options. This is the single
# source of truth for the explanations — `--list-tools` dumps it as JSON so
# skills/core/onboard/SKILL.md and ONBOARDING.md read it rather than restating it,
# and the two cannot drift from each other or from this file.
#
# Plain language only: someone who has never used a terminal on purpose
# reads these. Never say "hook", "symlink", "config.json" or "~/.config" —
# say what the person gets, not what the software is.
TOOL_CATALOGUE = [
    {
        "tier": 1,
        "title": "Safety checks",
        "tools": [
            {
                "name": "danger-guard",
                "section": "danger_guard",
                "tier": 1,
                "explanation": (
                    "Checks every command before it runs and blocks the "
                    "genuinely dangerous ones — wiping a disk, running "
                    "something downloaded and unseen, and deleting files "
                    "outside your work folder."),
                "doc_keywords": ["safety net on file deletion"],
            },
            {
                "name": "worktree-guard",
                "section": None,
                "tier": 1,
                "explanation": (
                    "Makes sure Claude only changes files in the project "
                    "copy it is actually working in, even when more than "
                    "one copy of the same project is open at once."),
                "doc_keywords": ["which repo checkout work lands in"],
            },
            {
                "name": "secret-scan",
                "section": None,
                "tier": 1,
                "explanation": (
                    "Before a save to one of your code projects is "
                    "recorded, checks it for anything that looks like a "
                    "password or a secret key, and stops the save if it "
                    "finds one. Unlike the two above, it is off until the "
                    "git setup step runs later in onboarding."),
                "doc_keywords": ["stops passwords reaching github"],
            },
        ],
    },
    {
        "tier": 2,
        "title": "Session-start context",
        "tools": [
            {
                "name": "repo-freshness",
                "section": "repo_freshness",
                "tier": 2,
                "explanation": (
                    "Tells you at the start of a conversation if a project "
                    "you follow has newer work online than what's on your "
                    "machine, checked quietly in the background."),
                "doc_keywords": ["projects you follow get checked"],
            },
            {
                "name": "tooling-digest",
                "section": "tooling_digest",
                "tier": 2,
                "explanation": (
                    "Shows a short summary of what's already set up and "
                    "connected at the start of a conversation, so you "
                    "don't have to ask or remember."),
                "doc_keywords": ["reads that map instead of asking you"],
            },
            {
                "name": "memory-routing",
                "section": "memory_routing",
                "tier": 2,
                "explanation": (
                    "Claude keeps two sets of notes for you: one for how you "
                    "work, one for plain facts about your machine and "
                    "accounts. This points it out when a note lands in the "
                    "wrong set, so the two don't drift apart and disagree. "
                    "Only ever a suggestion — it never blocks anything."),
                "doc_keywords": ["nudge when a note lands in the wrong place"],
            },
            {
                "name": "build-cost",
                "section": "build_cost",
                "tier": 2,
                "explanation": (
                    "Keeps a running log of how much of your usage budget "
                    "each piece of work actually spends, so you can see "
                    "where it goes over time."),
                "doc_keywords": ["running record of what work actually costs"],
            },
        ],
    },
    {
        "tier": 2,
        "title": "Working in parallel",
        "tools": [
            {
                "name": "base-pin",
                "section": "base_pin",
                "tier": 2,
                "explanation": (
                    "Tells you mid-conversation when the shared version of a "
                    "project has moved ahead of the separate copy you are "
                    "working in, so you can catch up before sending your work "
                    "for review instead of after."),
                "doc_keywords": ["ahead of this worktree"],
            },
        ],
    },
    {
        "tier": 2,
        "title": "Session bookkeeping",
        "tools": [
            {
                "name": "session-claim",
                "section": "session_claim",
                "tier": 2,
                "explanation": (
                    "If you open two conversations on the same project "
                    "folder at once, moves the second one into its own "
                    "private copy first, so they can't overwrite each "
                    "other's work."),
                "doc_keywords": ["one session per folder"],
            },
            {
                "name": "session-titler",
                "section": "session_titler",
                "tier": 2,
                "explanation": (
                    "Gives a conversation a short title automatically "
                    "when it ends (one with at least two real messages, "
                    "best effort), so you can find old work later by "
                    "searching instead of scrolling."),
                "doc_keywords": ["titled automatically when it ends"],
            },
            {
                "name": "thread-handover",
                "section": "thread_handover",
                "tier": 2,
                "explanation": (
                    "Watches long conversations and writes a short "
                    "handover note before one runs out of room or ends "
                    "unexpectedly, so nothing gets lost."),
                "doc_keywords": ["suggest writing a handover note"],
            },
            {
                "name": "usage-watch",
                "section": "usage_watch",
                "tier": 2,
                "explanation": (
                    "Shows how much of your usage limit is left, warns "
                    "you before it runs low, and writes a note if a task "
                    "gets cut off partway through."),
                "doc_keywords": ["live look at your usage budget"],
            },
        ],
    },
    {
        "tier": 3,
        "title": "Optional extras",
        "tools": [
            {
                "name": "caffeinate",
                "section": "caffeinate",
                "tier": 3,
                "explanation": (
                    "Keeps your computer from falling asleep while Claude "
                    "is in the middle of a long task, so it doesn't get "
                    "interrupted by your screen locking or your machine "
                    "sleeping."),
                "doc_keywords": ["keeping your mac awake for a long build"],
            },
        ],
    },
]


def catalogue_lookup():
    """Map every normalized tool name and section name to its catalogue entry.

    Both spellings resolve to the same entry (`session-titler` and
    `session_titler`) because the catalogue exposes tool names but the
    caller (and the config itself) deals in section names.
    """
    lookup = {}
    for group in TOOL_CATALOGUE:
        for tool in group["tools"]:
            keys = {tool["name"].replace("-", "_")}
            if tool["section"]:
                keys.add(tool["section"])
            for key in keys:
                lookup[key] = tool
    return lookup


def settable_sections():
    """Section names a caller may actually toggle: tier 2/3, with a section.

    Excludes tier 1 (never optional) and anything with no config section
    (worktree-guard, secret-scan) — there is nothing there to toggle.
    """
    return sorted({tool["section"] for group in TOOL_CATALOGUE for tool in group["tools"]
                   if tool["section"] and tool["tier"] != 1})


def resolve_repo_root():
    """The repo root above `lib/`, resolved through symlinks.

    `realpath`, not `abspath` — per this repo's hard rule, a tool here can be
    reached through a `~/.local/bin` symlink, and `abspath` does not resolve
    one; it would land inside the symlink's directory instead of the repo.
    Returns None if resolution fails for any reason, which `tool_presence()`
    treats the same as "cannot be determined."
    """
    try:
        return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    except OSError:
        return None


def tool_presence(repo_root):
    """{tool-name: bool} — whether `tools/<name>/` exists under `repo_root`.

    `present` is descriptive metadata only. It must never gate what
    --enable/--disable accept: `--enable caffeinate` has to keep working on a
    checkout where `tools/caffeinate/` doesn't exist. Nothing here is wired
    into `resolve_toggle_sections()` or either write path for that reason.

    The one exception is not `present`: --enable of a tool whose *profile*
    the config does not select is refused (`refuse_unselected_profiles`).

    When `repo_root` is falsy, or `tools/` under it can't be listed, every
    tool is reported present. Failing toward *offering* a tool costs one
    inert config key; failing toward hiding one would silently drop real
    tools from consent on any layout this heuristic misreads.
    """
    names = [tool["name"] for group in TOOL_CATALOGUE for tool in group["tools"]]
    if not repo_root:
        return {name: True for name in names}
    try:
        os.listdir(os.path.join(repo_root, "tools"))
    except OSError:
        return {name: True for name in names}
    return {name: os.path.isdir(os.path.join(repo_root, "tools", name)) for name in names}


def _sibling(name):
    """The sibling lib module `name`, or None when it cannot be imported."""
    lib = os.path.dirname(os.path.realpath(__file__))
    if lib not in sys.path:
        sys.path.insert(0, lib)
    try:
        return __import__(name)
    except ImportError:
        return None


def tool_profiles(repo_root):
    """{tool-name: profile} — which profile's plugin ships each catalogue tool.

    A tool's profile is the `profile` of the hook rows registered under
    `tools/<name>/` (catalogue names equal their directory). A tool with no
    hook rows, or secret-scan and worktree-guard, count as `core`.

    When the repo, the registry or a manifest can't be read, every tool is
    reported `core`. Failing toward *offering* matches `tool_presence`: hiding
    a tool on a layout this misreads would silently drop it from consent.
    """
    names = [tool["name"] for group in TOOL_CATALOGUE for tool in group["tools"]]
    result = {name: "core" for name in names}
    manifest = _sibling("hook_manifest")
    if not repo_root or manifest is None:
        return result
    try:
        rows = manifest.plugin_registrations(repo_root)
    except (OSError, ValueError):
        return result
    for name in names:
        prefix = "tools/%s/" % name
        for row in rows:
            if row["relpath"].startswith(prefix):
                result[name] = row["profile"]
                break
    return result


def refuse_unselected_profiles(repo, config, enable_sections):
    """True when every enabled tool's profile is selected by `config`.

    Prints the reason and returns False otherwise. `config` is the dict being
    written ({} for a fresh --write). An unreadable registry or profile module
    passes, failing toward offering like `tool_profiles`; a bad `profiles` key
    in `config` is an error.
    """
    if not enable_sections:
        return True
    profiles = _sibling("profiles")
    if profiles is None:
        return True
    try:
        registry = profiles.load_registry(repo)
    except profiles.RegistryError:
        return True
    names, error, _ = profiles.resolve_selection(registry, config, None)
    if error:
        print(error, file=sys.stderr)
        return False
    lookup = catalogue_lookup()
    owners = tool_profiles(repo)
    for section in enable_sections:
        tool = lookup[section]["name"]
        owner = owners.get(tool, "core")
        if owner not in names:
            print('%s comes with the %s profile, which this config does not select '
                  '— add "%s" to "profiles" (keep "continuity" in the list), re-run '
                  './install.sh in the primary checkout, then enable it'
                  % (tool, owner, owner), file=sys.stderr)
            return False
    return True


def parse_profiles(repo, raw):
    """(names, error) for a `--profiles` value.

    `names` is core first, registry order, with the `requires` closure applied.
    Strict: a profile choice cannot be validated without the registry, so an
    unreadable registry or profile module is an error here, unlike
    `refuse_unselected_profiles`, which fails toward offering.
    """
    listed = [n.strip() for n in (raw or "").split(",") if n.strip()]
    if not listed:
        return None, ("--profiles needs at least one profile name "
                      "(core alone is --profiles core)")
    profiles = _sibling("profiles")
    if profiles is None:
        return None, "cannot validate --profiles: lib/profiles.py could not be imported"
    try:
        registry = profiles.load_registry(repo)
    except profiles.RegistryError as exc:
        return None, "cannot validate --profiles: %s" % exc
    names, error, _ = profiles.resolve_selection(registry, {"profiles": listed}, None)
    if error:
        return None, error
    return names, None


def apply_profiles(config, repo, names):
    """Write the chosen profiles into `config`, plus the Mac -> caffeinate rule.

    Choosing `mac` turns caffeinate on;
    anything else turns it off, because a `toolkit-mac` plugin that is already
    enabled stays enabled outside the selected profiles and caffeinate reads
    only its own flag. Other keys in the `caffeinate` section are kept.
    """
    config["profiles"] = names
    # An explicit --disable caffeinate still wins: the caller's disable loop
    # runs after this.
    config.setdefault("caffeinate", {})["enabled"] = "mac" in names
    profiles = _sibling("profiles")
    left_out = profiles.missing_defaults(profiles.load_registry(repo), names, False)
    if left_out:
        print('note: "profiles" leaves out %s; a list replaces the default set, '
              'so they will not be installed, and the next ./install.sh uninstalls '
              'any the last install put in place' % ", ".join(left_out), file=sys.stderr)


def resolve_toggle_sections(enable_arg, disable_arg):
    """Validate --enable/--disable against the catalogue.

    Returns (enable_sections, disable_sections) — concrete config section
    names — on success. On failure, prints the reason to stderr and returns
    None. Nothing is written on failure; the caller exits 3 without having
    touched the filesystem.
    """
    def split(raw):
        return [n.strip().replace("-", "_") for n in (raw or "").split(",") if n.strip()]

    enable_names = split(enable_arg)
    disable_names = split(disable_arg)

    overlap = sorted(set(enable_names) & set(disable_names))
    if overlap:
        print("cannot both --enable and --disable the same tool: %s"
              % ", ".join(overlap), file=sys.stderr)
        return None

    lookup = catalogue_lookup()
    valid = settable_sections()
    enable_sections, disable_sections = [], []
    for names, sections in ((enable_names, enable_sections),
                            (disable_names, disable_sections)):
        for name in names:
            tool = lookup.get(name)
            if tool is None:
                print("unknown tool %r — valid names: %s" % (name, ", ".join(valid)),
                      file=sys.stderr)
                return None
            if tool["tier"] == 1:
                print("%r is a tier-1 safety tool and cannot be enabled or "
                      "disabled — it is always on." % name, file=sys.stderr)
                return None
            sections.append(tool["section"])
    return enable_sections, disable_sections


def shorten(path):
    return path.replace(HOME, "~", 1) if path.startswith(HOME) else path


def ask(question, default=None, allow_empty=False):
    """Prompt until the answer is non-empty (or empty is explicitly allowed)."""
    suffix = " [%s]: " % shorten(default) if default else ": "
    while True:
        try:
            answer = input(question + suffix).strip()
        except EOFError:
            raise SystemExit(1)
        if not answer and default is not None:
            return default
        if answer or allow_empty:
            return answer
        print("  Please enter a value.")


def ask_yes_no(question, default=True):
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input("%s %s: " % (question, hint)).strip().lower()
        except EOFError:
            raise SystemExit(1)
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Please answer y or n.")


def holds_only_the_checkout(folder, repo):
    """True when `folder`'s only visible content is the toolkit checkout,
    directly or through a chain of single-entry folders (`~/Tools/repos/<it>`).

    Such a folder exists to hold the toolkit, not the person's work, so it is
    never offered as the work folder (row 73, 28b F-D1a): accepting it would
    put the toolkit's own files, guards included, inside the free-delete zone
    with nothing else there to justify it. Hidden entries (`.DS_Store`) do not
    count. An unreadable folder answers False: it is not shown to hold only
    the checkout, and the guess stays as before.
    """
    checkout = os.path.realpath(repo)
    current = os.path.realpath(folder)
    while True:
        try:
            entries = [e for e in os.scandir(current) if not e.name.startswith(".")]
        except OSError:
            return False
        if len(entries) != 1:
            return False
        only = os.path.realpath(entries[0].path)
        if only == checkout:
            return True
        if not entries[0].is_dir() or not checkout.startswith(only + os.sep):
            return False
        current = only


def guess_workspace_root(repo):
    """Guess from where this repo was cloned.

    A clone at ~/Work/repos/claude-code-toolkit strongly implies ~/Work is the
    workspace. Walk up past a conventional container directory and stop at the
    first plausible parent under $HOME. Never a folder that holds nothing but
    this checkout (`holds_only_the_checkout`); with no other guess, None.
    """
    parent = os.path.dirname(repo)
    if os.path.basename(parent) in ("repos", "repositories", "code", "src",
                                    "projects", "git"):
        parent = os.path.dirname(parent)
    if (parent and parent != HOME and parent.startswith(HOME + os.sep)
            and not holds_only_the_checkout(parent, repo)):
        return parent
    for candidate in ("Work", "Projects", "Code", "src", "dev"):
        path = os.path.join(HOME, candidate)
        if os.path.isdir(path) and not holds_only_the_checkout(path, repo):
            return path
    return None


def protect_the_checkout(protected, workspace_root, repo):
    """`protected` plus the toolkit checkout when it sits anywhere under the
    work folder and is not already listed (compared by real path).

    `derive_protected_paths` reaches only children and git repos one level
    below them, so a checkout nested deeper would be deletable wholesale as
    routine work. Added regardless of `--no-protected` or a declined list:
    the checkout is protected like the work folder itself, not as a derived
    suggestion (row 73, ruled 2026-11-09).
    """
    checkout = os.path.realpath(repo)
    root = os.path.realpath(workspace_root)
    if not checkout.startswith(root + os.sep):
        return list(protected)
    if any(os.path.realpath(p) == checkout for p in protected):
        return list(protected)
    return list(protected) + [checkout]


def validate_workspace_root(path):
    """Return (ok, message). Refuses the cases that would defeat the guard."""
    if not path:
        return False, "empty"
    real = os.path.realpath(os.path.expanduser(path))
    if real == "/" or real == os.path.realpath(HOME):
        return False, ("that is %s itself. The workspace root must be a "
                       "directory INSIDE your home, or the guard would treat "
                       "deletions anywhere as routine."
                       % ("your home directory" if real != "/" else "the filesystem root"))
    # The cloud check goes BEFORE the existence check on purpose. `~/Google Drive`
    # is a symlink into ~/Library/CloudStorage/..., so realpath moves the marker
    # around; and a refusal on safety grounds should not depend on whether the
    # directory happens to exist yet. Check both the typed path and the resolved
    # one, since either may carry the marker.
    lowered = (real + "\n" + os.path.expanduser(path)).lower()
    for marker in CLOUD_MARKERS:
        if marker in lowered:
            return False, ("that path looks cloud-synced (%s). A delete there "
                           "propagates to every other machine, so it must not "
                           "be a trusted workspace root." % marker)
    if not os.path.isdir(real):
        return False, "no such directory: %s" % shorten(real)
    return True, real


def derive_protected_paths(root):
    """Directories under `root` that should never be an `rm` target themselves.

    Its immediate children, plus any git repository one level below those. This
    reproduces by inspection what would otherwise be a hand-written list, and it
    is the part a person is most likely to get wrong or skip.
    """
    found = []
    try:
        children = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return found
    for child in children:
        if not child.is_dir() or child.name.startswith("."):
            continue
        found.append(child.path)
        try:
            for grandchild in sorted(os.scandir(child.path), key=lambda e: e.name):
                if not grandchild.is_dir() or grandchild.name.startswith("."):
                    continue
                if os.path.isdir(os.path.join(grandchild.path, ".git")):
                    found.append(grandchild.path)
        except OSError:
            continue
    return found


def validate_user_data_dir(path, repo):
    """Return (ok, shortened realpath | reason) for a directory the toolkit
    WRITES your files into: the handover directory and the specs directory.

    Your data lives outside the toolkit checkout, always. A directory inside
    it would put your handovers and plans where a `git clean`, a fresh clone
    or an update can take them, and where a commit can publish them. Also
    refused: your home directory or the filesystem root itself (the handover
    sweep moves files inside this directory), a path that is not absolute
    once expanded, one naming an unset variable, and an existing file. The
    directory need not exist yet: the tools that write there create it.
    """
    if not isinstance(path, str) or not path.strip():
        return False, "empty"
    expanded = toolkit_config.expand_path(path, strict=True)
    if expanded is None:
        return False, "%s names an unset variable" % path
    if not os.path.isabs(expanded):
        return False, "%s is not an absolute path once expanded" % path
    real = os.path.realpath(expanded)
    if real == "/" or real == os.path.realpath(HOME):
        return False, ("that is %s itself; give this its own directory"
                       % ("your home directory" if real != "/" else "the filesystem root"))
    if repo:
        checkout = os.path.realpath(repo)
        if real == checkout or real.startswith(checkout + os.sep):
            return False, ("that is inside the toolkit checkout (%s); your files "
                           "belong outside it, where an update or a commit "
                           "cannot reach them" % shorten(checkout))
    if os.path.exists(real) and not os.path.isdir(real):
        return False, "%s exists and is not a directory" % shorten(real)
    return True, shorten(real)


def default_handover_dir(workspace_root):
    """`<workspace_root>/handovers`: outside ~/.claude, where Claude Code asks
    before every write and a non-interactive session cannot write at all."""
    return os.path.join(workspace_root, "handovers")


def build_config(name, workspace_root, handover_dir, protected, specs_dir=None,
                 review_mode=None):
    """The config dict, from already-validated values.

    `spec_archive.specs_dir` is written only when the caller chose one. The
    tool that reads it has no compiled default and stays inert without it --
    deliberately, since it moves files -- so an unchosen specs directory is
    left unset rather than guessed from the workspace layout.

    `worktree_guard.canonical_root` is written as an explicit null: Rule C of
    the worktree guard is off until you name a root, and recording that as a
    decision is what tells `--report` this config was not inherited from an
    older copy that inferred one.

    `review_mode` is OMITTED entirely rather than written as an explicit
    "economical" when the caller has no opinion --
    `toolkit_config.build_review_mode()` already treats an absent `build` key
    as "economical". Only a caller that actually asked the question (the
    interactive prompt below, or `--review-mode`) writes it.
    """
    config = {
        "//": ("Written by claude-code-toolkit setup. See config.example.json in "
               "the repo for every available key and its default."),
        "owner_name": name,
        "workspace_root": shorten(workspace_root),
        "danger_guard": {
            "extra_safe_roots": [],
            "extra_protected_paths": [shorten(p) for p in protected],
        },
        "worktree_guard": {
            "canonical_root": None,
        },
        "thread_handover": {
            "handover_dir": shorten(handover_dir),
        },
    }
    if specs_dir:
        config["spec_archive"] = {"specs_dir": shorten(specs_dir)}
    if review_mode:
        config["build"] = {"review": review_mode}
    return config


def write_config(config_path, config, replace=True):
    """Write the config. With replace=False the file is created exclusively, so
    a config that appears between the up-front check and this write is never
    clobbered: FileExistsError propagates and the caller refuses."""
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    if replace:
        handle = open(config_path, "w", encoding="utf-8")
    else:
        fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        handle = os.fdopen(fd, "w", encoding="utf-8")
    with handle:
        json.dump(config, handle, indent=2)
        handle.write("\n")


def refuse_existing(config_path):
    """True, after saying why on stderr, when something already sits at the path.

    lexists, not exists: a dangling symlink is still something we must not write
    through.
    """
    if not os.path.lexists(config_path):
        return False
    print("config already exists at %s — change it with: python3 %s %s <repo> --update ..., "
          "or redo the questions with ./install.sh --setup"
          % (config_path, os.path.abspath(__file__), config_path),
          file=sys.stderr)
    return True


def non_interactive(config_path, repo, argv):
    """Write a config from flags, with the SAME validation as the prompts.

    This exists so Claude can drive onboarding conversationally: it asks the
    questions itself, then calls this. Routing through here rather than having
    the model hand-write JSON is the point — the workspace-root refusals
    (home directory, filesystem root, cloud-synced folders) are safety checks,
    and a caller that skips them writes a config that quietly defeats the guard.

    Exits 3 with the reason on a rejected value, so the caller can surface it and
    ask again rather than silently writing something wrong.
    """
    import argparse
    parser = argparse.ArgumentParser(prog="setup_config.py --write")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--name", required=True)
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--handover-dir", default=None,
                        help="where /handover writes; default <workspace-root>/handovers")
    parser.add_argument("--specs-dir", default=None,
                        help="where plans and specs go; omit to leave it unset "
                             "(the spec tools then ask)")
    parser.add_argument("--no-protected", action="store_true",
                        help="skip the derived protected-path list")
    parser.add_argument("--enable", default="",
                        help="comma-separated tools/sections to write {\"enabled\": true}")
    parser.add_argument("--disable", default="",
                        help="comma-separated tools/sections to write {\"enabled\": false}")
    parser.add_argument("--review-mode", choices=("economical", "full-power"), default=None,
                        help="which /build review mode to write under build.review "
                             "(omit to leave the key out entirely)")
    parser.add_argument("--profiles", default=None,
                        help="comma-separated profiles to write under \"profiles\" "
                             "(core and the requires closure are added)")
    args = parser.parse_args(argv)

    if refuse_existing(config_path):
        return 3

    # Validated before anything touches the filesystem: an unknown or
    # tier-1 name must leave the config file untouched, same contract as
    # the workspace_root rejection below.
    resolved = resolve_toggle_sections(args.enable, args.disable)
    if resolved is None:
        return 3
    enable_sections, disable_sections = resolved
    profile_names = None
    if args.profiles is not None:
        profile_names, error = parse_profiles(repo, args.profiles)
        if error:
            print(error, file=sys.stderr)
            return 3

    ok, result = validate_workspace_root(args.workspace_root)
    if not ok:
        print("workspace_root rejected: %s" % result, file=sys.stderr)
        return 3
    workspace_root = result

    ok, handover_dir = validate_user_data_dir(
        args.handover_dir or default_handover_dir(workspace_root), repo)
    if not ok:
        print("handover_dir rejected: %s" % handover_dir, file=sys.stderr)
        return 3
    specs_dir = None
    if args.specs_dir is not None:
        ok, specs_dir = validate_user_data_dir(args.specs_dir, repo)
        if not ok:
            print("specs_dir rejected: %s" % specs_dir, file=sys.stderr)
            return 3

    protected = [] if args.no_protected else derive_protected_paths(workspace_root)
    protected = protect_the_checkout(protected, workspace_root, repo)

    config = build_config(args.name, workspace_root, os.path.expanduser(handover_dir),
                          protected, specs_dir and os.path.expanduser(specs_dir),
                          args.review_mode)

    # Judged against the profiles about to be written, so one call can select
    # a profile and enable its tool.
    if not refuse_unselected_profiles(
            repo, {"profiles": profile_names} if profile_names else {}, enable_sections):
        return 3

    # Merge, never replace — thread_handover already carries handover_dir
    # from build_config above, and a section-level enable/disable must not
    # drop it.
    if profile_names:
        apply_profiles(config, repo, profile_names)
    for section in enable_sections:
        config.setdefault(section, {})["enabled"] = True
    for section in disable_sections:
        config.setdefault(section, {})["enabled"] = False

    try:
        write_config(config_path, config, replace=False)
    except FileExistsError:
        refuse_existing(config_path)
        return 3

    print("wrote %s" % shorten(config_path))
    print("  owner_name     : %s" % args.name)
    print("  workspace_root : %s" % shorten(workspace_root))
    print("  protected      : %d path(s)" % len(protected))
    for path in protected:
        print("      %s" % shorten(path))
    print("  handover_dir   : %s" % handover_dir)
    print("  specs_dir      : %s" % (specs_dir or "not set"))
    if enable_sections:
        print("  enabled        : %s" % ", ".join(enable_sections))
    if disable_sections:
        print("  disabled       : %s" % ", ".join(disable_sections))
    if args.review_mode:
        print("  review mode    : %s" % args.review_mode)
    if profile_names:
        print("  profiles       : %s" % ", ".join(profile_names))
    return 0


# --- Retired configuration ---------------------------------------------------
#
# A config can outlive the code that wrote it. The tables below say what an
# older copy of this toolkit recorded that this one reads differently, so that
# `--report` can name it and `--update --migrate` can carry it forward. Nothing
# here deletes a key: a migration copies, and the original stays recoverable.

RETIRED_SECTIONS = {
    # A retirement adds its row here, in the same change that stops reading
    # the section; the report's unknown-section warning below is what makes
    # a forgotten row loud -- an old section with nowhere mapped to go would
    # otherwise just silently stop being read.
    "workspace_sync": {
        "successor": "repo_freshness.sync",
        "keys": ("enabled", "repo_root", "gh_owner", "throttle_hours"),
    },
}

# The always-on safety tools. An older copy let you decline installing them;
# this toolkit ships them in its core profile with no off switch, so a
# recorded refusal cannot be honoured and has to become an explicit new choice.
CORE_GUARD_SECTIONS = ("danger_guard", "worktree_guard", "secret_scan")

# An older copy recorded install consent per tool as `<section>.installed`:
# true, false, or absent (no decision). This toolkit chooses tools by profile
# and turns optional ones off with `<section>.enabled: false`, and reads no
# `installed` key at all -- so a recorded "no" would silently become "on" if
# nothing carried it across. What each declined section becomes:
#   "core"        - an always-on guard: needs --accept-core or a skills-only
#                   install (see core_conflicts)
#   "enabled"     - on by default here: --migrate writes enabled: false
#   "default-off" - off unless enabled, so the refusal already holds
#   "manual"      - runs only when you invoke it; nothing runs in the background
#   "not-shipped" - not part of this toolkit
DECLINED_SUCCESSORS = {
    "danger_guard": "core",
    "worktree_guard": "core",
    "secret_scan": "core",
    "repo_freshness": "enabled",
    "tooling_digest": "enabled",
    "memory_routing": "enabled",
    "build_cost": "enabled",
    "session_claim": "enabled",
    "session_titler": "enabled",
    "thread_handover": "enabled",
    "usage_watch": "enabled",
    "base_pin": "enabled",          # newer than the old copy; mapped for completeness
    "caffeinate": "default-off",
    "prompt_optimizer": "default-off",
    "git_tidy": "manual",
    "wrap_up_survey": "manual",
    "toolkit_sync": "not-shipped",
}


def recorded_consent(config, section):
    """The older copy's install decision for `section`: True, False or None.

    Three states, and None is NOT a synonym for False: the section or key
    absent, JSON null and any non-boolean value all mean "no decision was
    recorded", which must never be read as a refusal.
    """
    section_config = config.get(section)
    if not isinstance(section_config, dict):
        return None
    value = section_config.get("installed")
    return value if isinstance(value, bool) else None


def core_conflicts(config):
    """Sorted always-on guard sections the config records as declined.

    The installer stops before changing anything while this is non-empty:
    core cannot be installed without them, and installing them anyway would
    override a choice someone made. `--update --accept-core` records the new
    choice; a skills-only install needs no core at all.
    """
    return sorted(section for section in CORE_GUARD_SECTIONS
                  if recorded_consent(config, section) is False)


def unhonoured_declines(config):
    """[(section, enabled value)] for every declined "enabled"-kind section
    whose `enabled` is not False -- the tools that would run although the
    config says they were declined."""
    found = []
    for section, kind in sorted(DECLINED_SUCCESSORS.items()):
        if kind != "enabled" or recorded_consent(config, section) is not False:
            continue
        enabled = config[section].get("enabled")
        if enabled is not False:
            found.append((section, enabled))
    return found


def inferred_canonical_root(config):
    """The canonical root an older copy's worktree guard would have INFERRED
    for this config, or None.

    That guard, with `worktree_guard.canonical_root` unset, fell back to
    `<workspace_root>/repos` when that directory existed and to the workspace
    root otherwise, so its Rule C was active on any machine that set a
    workspace root. This toolkit never infers the root (Rule C is off until
    you name one), so carrying such a config across would switch Rule C off
    silently. Only a config with no `canonical_root` key at all can have
    relied on the inference: an explicit value, null included, is a decision.
    Returns the shortened real path, or None when there was nothing to infer
    or the inferred directory does not exist (the old guard was inactive then
    too).
    """
    guard = config.get("worktree_guard")
    if isinstance(guard, dict) and "canonical_root" in guard:
        return None
    workspace = config.get("workspace_root")
    if not isinstance(workspace, str) or not workspace:
        return None
    root = toolkit_config.expand_path(workspace, strict=True)
    if not root or not os.path.isabs(root):
        return None
    nested = os.path.join(root, "repos")
    candidate = nested if os.path.isdir(nested) else root
    if not os.path.isdir(candidate):
        return None
    return shorten(os.path.realpath(candidate))


def inferred_sync_root(config):
    """The directory an older repo-freshness sync phase would have PULLED in
    for this config, or None.

    That sync phase, with `repo_freshness.sync.repo_root` unset, fell back to
    `<workspace_root>/repos` when that directory existed. This toolkit never
    derives a directory to write in, so carrying such a config across would
    switch the sync phase off silently. Only a config that turned sync on
    (a literal `true`, the only value that ever enabled it) and has no
    `repo_root` key can have relied on the fallback: an explicit value, null
    included, is a decision, and so is a retired `workspace_sync.repo_root`
    that `--migrate` will carry across. Returns the shortened real path, or
    None when the fallback directory does not exist (the old sync phase was
    inert then too).
    """
    freshness = config.get("repo_freshness")
    sync = freshness.get("sync") if isinstance(freshness, dict) else None
    if not isinstance(sync, dict) or sync.get("enabled") is not True:
        return None
    if "repo_root" in sync:
        return None
    retired = config.get("workspace_sync")
    if isinstance(retired, dict) and retired.get("repo_root") is not None:
        return None
    workspace = config.get("workspace_root")
    if not isinstance(workspace, str) or not workspace:
        return None
    root = toolkit_config.expand_path(workspace, strict=True)
    if not root or not os.path.isabs(root):
        return None
    candidate = os.path.join(root, "repos")
    if not os.path.isdir(candidate):
        return None
    return shorten(os.path.realpath(candidate))


# --- `--set`: one validated key ----------------------------------------------

def _load_example(repo_root):
    """config.example.json, parsed, from the root of `repo_root`."""
    with open(os.path.join(repo_root, "config.example.json"), encoding="utf-8") as handle:
        return json.load(handle)


def _resolve_example_leaf(example, segments):
    """Walk `example` by `segments`. Returns (value, error); error is None on
    success. A `//`-prefixed segment never reaches here: `_parse_set_arg`
    refuses one first.
    """
    node = example
    for index, seg in enumerate(segments):
        if not isinstance(node, dict) or seg not in node:
            return None, ("no such key in config.example.json: %s"
                          % ".".join(segments[:index + 1]))
        node = node[seg]
    if isinstance(node, dict):
        return None, ("%s names a section, not a value — set a key inside it"
                      % ".".join(segments))
    return node, None


# Keys whose example value is null but whose reader wants something other
# than a string. Without an entry, a null example accepts a string or null.
NULL_EXAMPLE_TYPES = {
    "context_budget.always_on_budget": int,
    "usage_watch.default_ratio": int,
}

# Keys config.example.json documents under a `//<section>` comment but leaves
# out of its live keys, because absent is the normal state. The value stands
# in for the example's, as the type contract. `--set` accepts them; nothing
# else changes: a copied example still leaves them absent.
ABSENT_EXAMPLE_KEYS = {
    "update.origin": "",  # toolkit_update.configured_origin; absent = the public repo
}


def _type_check(example_value, raw_value, null_type=None):
    """Return (ok, value_or_reason). `example_value` is config.example.json's
    own value at this path; its Python type is the contract `raw_value`
    (already `json.loads`-parsed, or the raw string when that failed) must
    match. `bool` is checked before `int` throughout, because
    `isinstance(True, int)` is True and a stray boolean must never pass as a
    number.
    """
    if example_value is None:
        if raw_value is None:
            return True, None
        if null_type is int:
            if isinstance(raw_value, int) and not isinstance(raw_value, bool):
                return True, raw_value
            return False, "expected an integer or null"
        if isinstance(raw_value, str):
            return True, raw_value
        return False, "expected a string or null"
    if isinstance(example_value, bool):
        if isinstance(raw_value, bool):
            return True, raw_value
        return False, "expected true or false"
    if isinstance(example_value, int):
        if isinstance(raw_value, int) and not isinstance(raw_value, bool):
            return True, raw_value
        return False, "expected an integer"
    if isinstance(example_value, str):
        if isinstance(raw_value, str):
            return True, raw_value
        return False, "expected a string"
    if isinstance(example_value, list):
        if isinstance(raw_value, list) and all(isinstance(v, str) for v in raw_value):
            return True, raw_value
        return False, "expected a list of strings"
    return False, "config.example.json's own value here has an unsupported type"


def _validate_extra_safe_roots(value, repo):
    """`danger_guard.extra_safe_roots`: each entry gets the same refusals as
    workspace_root itself, since a cloud-synced or home-directory entry here
    would just as quietly defeat the guard."""
    resolved = []
    for entry in value:
        ok, result = validate_workspace_root(entry)
        if not ok:
            return False, "%r: %s" % (entry, result)
        resolved.append(shorten(result))
    return True, resolved


def _existing_dir_or_null(value, repo):
    """null, or an existing directory, stored as its shortened real path. For
    readers that ignore anything else, so `--set` cannot report success on a
    value they would silently discard."""
    if value is None:
        return True, None
    expanded = toolkit_config.expand_path(value, strict=True)
    real = os.path.realpath(expanded) if expanded else None
    if not real or not os.path.isdir(real):
        return False, "no such directory: %s" % value
    return True, shorten(real)


def _non_empty_origin(value, repo):
    """`update.origin`: stored stripped, and never blank. Its reader refuses a
    blank one, so `--set` must not report success on it."""
    if not value.strip():
        return False, "expected a git URL or path, not a blank string"
    return True, value.strip()


def _user_data_dir_or_null(value, repo):
    """null, or a directory that passes `validate_user_data_dir`."""
    if value is None:
        return True, None
    return validate_user_data_dir(value, repo)


def _codex_model_or_null(value, repo):
    """null, or a usable `codex -m` argument: a non-empty string with no
    whitespace that does not start with `-` (codex would parse that as a
    flag). `bool` is not a `str`, so a stray boolean never passes."""
    if value is None:
        return True, None
    if (not isinstance(value, str) or value == ""
            or any(c.isspace() for c in value) or value.startswith("-")):
        return False, ("expected a model name: a non-empty string with no "
                       "whitespace, not starting with '-' (or null)")
    return True, value


def _int_in_range(lo, hi=None):
    """A validator for the common numeric shape: a reader that silently falls
    back to its own default for anything outside [lo, hi] (no hi = no
    ceiling). Assumes the value already passed the int-and-not-bool check."""
    def validate(value, repo):
        if value is None:
            return True, None
        if value < lo or (hi is not None and value > hi):
            if hi is None:
                return False, "expected an integer >= %d" % lo
            return False, "expected an integer between %d and %d" % (lo, hi)
        return True, value
    return validate


# Keys without an entry here are type-checked only, against
# config.example.json's own value. Add an entry when a key's reader silently
# ignores some type-valid value: each entry mirrors one reader's own fallback
# rule, so `--set` cannot report success on a value that reader discards.
KEY_VALIDATORS = {
    "update.origin": _non_empty_origin,
    "danger_guard.extra_safe_roots": _validate_extra_safe_roots,
    "worktree_guard.canonical_root": _existing_dir_or_null,
    "repo_freshness.sync.repo_root": _existing_dir_or_null,
    "thread_handover.handover_dir": _user_data_dir_or_null,
    "spec_archive.specs_dir": _user_data_dir_or_null,
    "codex_review.models.code": _codex_model_or_null,    # codex_review_lib.resolve_gate_model
    "codex_review.models.merge": _codex_model_or_null,   # codex_review_lib.resolve_gate_model
    "codex_review.models.plan": _codex_model_or_null,    # codex_review_lib.resolve_gate_model
    "codex_review.models.design": _codex_model_or_null,  # codex_review_lib.resolve_gate_model
    # Numeric keys: each mirrors its reader's own silent-fallback rule.
    "local_ci.max_concurrent": _int_in_range(1),                # local_ci_lib.max_concurrent
    "spec_archive.hook_deadline_seconds": _int_in_range(1),     # claude-spec-archive:253
    "repo_freshness.fetch_interval_seconds": _int_in_range(1),  # freshness_lib.py
    "repo_freshness.sync.throttle_hours": _int_in_range(1),     # sync_lib.py
    "tooling_digest.max_lines": _int_in_range(1),               # digest_lib.py
    "tooling_digest.stale_after_days": _int_in_range(0),        # digest_lib.py
    "memory_lint.max_hook_chars": _int_in_range(1),             # lint_lib.py
    "memory_lint.max_mean_hook_chars": _int_in_range(1),        # lint_lib.py
    # Clamped below the harness's own cut (25,000 chars, 200 lines): a value
    # at or above it would be stored but never honoured.
    "memory_lint.max_index_chars": _int_in_range(1, 24999),     # lint_lib.py
    "memory_lint.max_index_lines": _int_in_range(1, 199),       # lint_lib.py
    "context_lint.max_lines": _int_in_range(1),                 # context_lint_lib.py
    "context_lint.core_max_lines": _int_in_range(1),            # context_lint_lib.py
    "context_growth.memory_max_lines": _int_in_range(1),        # growth_lib.py
    "context_growth.rules_max_lines": _int_in_range(1),         # growth_lib.py
    "context_budget.always_on_budget": _int_in_range(1),        # budget_lib.py
    "caffeinate.battery_override_hours": _int_in_range(0),      # caffeinate_lib.py
    "caffeinate.battery_floor_percent": _int_in_range(0, 100),  # caffeinate_lib.py
    "caffeinate.settle_seconds": _int_in_range(1),              # caffeinate_lib.py
    "caffeinate.hold_seconds": _int_in_range(1),                # caffeinate_lib.py
    "usage_watch.stale_after_seconds": _int_in_range(1),        # usage_lib.py
    "usage_watch.unknown_reannounce_seconds": _int_in_range(1), # usage_lib.py
    "usage_watch.default_ratio": _int_in_range(1),              # estimate_lib.py
    "usage_watch.codex_low_gates": _int_in_range(0),            # claude-usage-watch:378
    "base_pin.fetch_interval_seconds": _int_in_range(1),        # base_pin_lib.py
    "base_pin.max_commits_listed": _int_in_range(0),            # base_pin_lib.py
    "backup.keep": _int_in_range(1),                            # claude-backup:50
    "backup.max_age_days": _int_in_range(1),                    # claude-backup:50
}

# The other side of the numeric table: keys whose reader accepts every int,
# so a range check here would be invented rather than mirrored. Each value
# names the reader that was checked. The suite requires every numeric leaf of
# config.example.json to be in exactly one of this dict and KEY_VALIDATORS.
TYPE_ONLY_KEYS = {
    "thread_handover.arm_cap": (
        "tools/thread-handover/watch_lib.py:117 -- feeds min(ARM_CAP, "
        "int(window * 0.35)) with no floor or ceiling; any int is a threshold."),
    "thread_handover.urgent_cap": "tools/thread-handover/watch_lib.py:118 -- as arm_cap.",
    "thread_handover.sweep_grace_sessions": (
        "tools/thread-handover/index_lib.py:117 -- compared as a count; 0 and "
        "negatives are used as-is (config.example.json says what 0 does)."),
    "thread_handover.sweep_grace_days": (
        "tools/thread-handover/index_lib.py:119 -- `elapsed >= GRACE_DAYS`; 0 "
        "disables the day gate rather than being discarded."),
    "spec_archive.min_age_days": (
        "tools/spec-archive/claude-spec-archive:345 -- compared as age > "
        "min_age_days; every int is a well-defined floor."),
    "spec_archive.stale_flip_days": (
        "tools/spec-archive/claude-spec-archive:245 -- <= 0 means the flip is "
        "off, which is the documented meaning of 0, not a discard."),
    "session_claim.max_blocks": (
        "tools/session-claim/breaker_lib.py:137 -- <= 0 disables the breaker, "
        "documented as permitted; any positive int trips after that many."),
    "usage_watch.notice_pct": (
        "tools/usage-watch/usage_lib.py:371 -- read with get_int and only "
        "sorted with the other two; no clamp, so any int is a boundary."),
    "usage_watch.gate_pct": "tools/usage-watch/usage_lib.py:372 -- as notice_pct.",
    "usage_watch.halt_pct": "tools/usage-watch/usage_lib.py:373 -- as notice_pct.",
}


def _parse_set_arg(raw):
    """Split one `--set` argument. Returns (path_str, segments, value) on a
    well-formed PATH=VALUE, or (None, None, reason) on a malformed one.
    """
    if "=" not in raw:
        return None, None, "malformed --set %r — expected PATH=VALUE" % raw
    path_str, _, value_str = raw.partition("=")
    segments = path_str.split(".")
    if len(segments) < 2 or any(not seg or seg.startswith("//") for seg in segments):
        return None, None, (
            "malformed --set path %r — dotted, at least two segments, no "
            "empty or // segment" % path_str)
    try:
        value = json.loads(value_str)
    except ValueError:
        value = value_str
    return path_str, segments, value


def resolve_set_operations(set_args, repo_root):
    """Validate every `--set PATH=VALUE`. Returns a list of (path_str,
    segments, value_to_store, is_guard) on success. On the first failure,
    prints the reason to stderr and returns None. Never opens the config:
    every check is against config.example.json and the value itself, which
    is what lets a refusal leave the file byte-identical.
    """
    if not set_args:
        return []
    example = _load_example(repo_root)
    ops = []
    for raw in set_args:
        path_str, segments, value = _parse_set_arg(raw)
        if path_str is None:
            print(value, file=sys.stderr)  # `value` holds the reason here
            return None

        if len(segments) == 2 and segments[-1] == "enabled":
            print("%r is set with --enable/--disable, not --set" % path_str,
                  file=sys.stderr)
            return None
        if segments[-1] == "installed":
            print("%r is not read by this toolkit: tools are chosen with "
                  "--profiles and turned off with --disable" % path_str,
                  file=sys.stderr)
            return None

        if path_str in ABSENT_EXAMPLE_KEYS:
            example_value, error = ABSENT_EXAMPLE_KEYS[path_str], None
        else:
            example_value, error = _resolve_example_leaf(example, segments)
        if error:
            print("--set %s: %s" % (path_str, error), file=sys.stderr)
            return None

        ok, result = _type_check(example_value, value, NULL_EXAMPLE_TYPES.get(path_str))
        if not ok:
            print("--set %s: %s (got %r)" % (path_str, result, value), file=sys.stderr)
            return None
        value = result

        validator = KEY_VALIDATORS.get(path_str)
        if validator:
            ok, result = validator(value, repo_root)
            if not ok:
                print("--set %s: %s" % (path_str, result), file=sys.stderr)
                return None
            value = result

        ops.append((path_str, segments, value, segments[0] in CORE_GUARD_SECTIONS))
    return ops


def _set_nested(config, segments, value):
    node = config
    for seg in segments[:-1]:
        nxt = node.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            node[seg] = nxt
        node = nxt
    node[segments[-1]] = value


def _get_nested(config, segments):
    node = config
    for seg in segments:
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


# --- `--migrate` ------------------------------------------------------------

def apply_migrate(config, repo_root):
    """Carry an older config forward, in place. Returns (changed, lines):
    `changed` is True only when something was written, and `lines` report
    every key examined (migrated / skipped / kept / disabled / needs a choice).

    Two things move, and nothing is ever removed:

    1. Every mapped, non-None `RETIRED_SECTIONS` value is copied into its
       successor, unless the successor already has a value (the successor
       wins and the line says "kept"; an explicit null counts as a value).
       The copy is refused, with a "skipped"
       line, when the value fails the successor's type contract or its
       validator: the same checks `--set` applies. The value copied is the
       original, byte for byte, never the validator's canonical form, so the
       report's successor/retired comparison clears after a migration.
    2. A declined install of a tool this toolkit runs by default becomes
       `<section>.enabled: false`, so a refusal stays a refusal. An explicit
       `enabled` already present is the newer decision and is kept (the
       report keeps naming the conflict until it is resolved). A declined
       always-on guard is never migrated here: see `core_conflicts`.
    """
    example = _load_example(repo_root)
    changed = False
    lines = []
    for old_name, info in RETIRED_SECTIONS.items():
        section = config.get(old_name)
        if not isinstance(section, dict):
            continue
        successor_segments = info["successor"].split(".")
        for key in info["keys"]:
            old_value = section.get(key)
            if old_value is None:
                continue
            successor_section = _get_nested(config, successor_segments)
            # Presence, not value: an explicit null in the successor is a
            # newer decision (e.g. gh_owner: null turns discovery off) and
            # wins like any other value.
            present = isinstance(successor_section, dict) and key in successor_section
            new_value = successor_section.get(key) if present else None
            full_path = ".".join(successor_segments + [key])
            if not present:
                example_value, _leaf_error = _resolve_example_leaf(
                    example, successor_segments + [key])
                type_ok, type_result = _type_check(
                    example_value, old_value, NULL_EXAMPLE_TYPES.get(full_path))
                if not type_ok:
                    lines.append("  skipped        : %s.%s (%s) — set it with --set"
                                 % (old_name, key, type_result))
                    continue
                validator = KEY_VALIDATORS.get(full_path)
                ok, reason = validator(old_value, repo_root) if validator else (True, None)
                if ok:
                    _set_nested(config, successor_segments + [key], old_value)
                    lines.append("  migrated       : %s.%s -> %s"
                                 % (old_name, key, full_path))
                    changed = True
                else:
                    lines.append("  skipped        : %s.%s (%s) — set it with --set"
                                 % (old_name, key, reason))
            elif new_value != old_value:
                lines.append("  kept           : %s (already %s; %s.%s was %s)"
                             % (full_path, json.dumps(new_value), old_name, key,
                                json.dumps(old_value)))

    for section, kind in sorted(DECLINED_SUCCESSORS.items()):
        if kind != "enabled" or recorded_consent(config, section) is not False:
            continue
        enabled = config[section].get("enabled")
        if enabled is None:
            config[section]["enabled"] = False
            lines.append("  disabled       : %s (declined in the older config)" % section)
            changed = True
        elif enabled is not False:
            lines.append("  kept           : %s.enabled = %s (set explicitly; the older "
                         "config declined it — --disable %s honours that)"
                         % (section, json.dumps(enabled), section))
    for section in core_conflicts(config):
        lines.append("  needs a choice : %s was declined, but core always installs "
                     "it — see --report" % section)
    return changed, lines


# --- `--report` -------------------------------------------------------------

def config_report(config, repo_root, config_path=None):
    """[warning, ...]: one line per issue found in `config` against
    config.example.json and the catalogue under `repo_root`. Empty means
    healthy. Never raises on a malformed `config`.

    - a retired section holding a value its successor does not have
      (`--update --migrate` fixes it);
    - a declined always-on guard (`--update --accept-core`, or a skills-only
      install);
    - a declined tool that would still run (`--update --migrate`, or
      `--disable`);
    - a canonical root an older worktree guard inferred, which this toolkit
      will not (`--set worktree_guard.canonical_root=...`, or `=null`);
    - a sync root an older repo-freshness inferred, which this toolkit will
      not (`--set repo_freshness.sync.repo_root=...`, or `=null`);
    - a top-level key nothing here reads: what a forgotten retirement or a
      typo looks like.
    """
    warnings = []
    example = _load_example(repo_root)
    # A `//name` comment documents `name` even when the example leaves the
    # key itself unset (owner_name, profiles).
    example_top_keys = {key[2:] if key.startswith("//") else key for key in example}
    example_top_keys.discard("")
    catalogue_sections = {tool["section"] for group in TOOL_CATALOGUE
                          for tool in group["tools"] if tool["section"]}
    command = "python3 %s/lib/setup_config.py %s %s --update" % (
        shorten(repo_root),
        # The real path when the caller has one, so the printed command runs
        # as pasted; a placeholder is advice nobody can follow.
        shorten(config_path) if config_path else "<config>",
        shorten(repo_root))

    for old_name, info in RETIRED_SECTIONS.items():
        section = config.get(old_name)
        if not isinstance(section, dict):
            continue
        successor_section = _get_nested(config, info["successor"].split("."))
        if not isinstance(successor_section, dict):
            successor_section = {}
        # Only what --migrate would copy: a key the successor already has,
        # null included, is a newer decision and not a pending migration.
        diffs = [key for key in info["keys"]
                 if section.get(key) is not None and key not in successor_section]
        if diffs:
            warnings.append(
                "config still carries %s (retired) with values %s does not have: "
                "%s — run: %s --migrate"
                % (old_name, info["successor"], ", ".join(diffs), command))

    for section in core_conflicts(config):
        warnings.append(
            "config declines %s, which this toolkit's core always installs — the "
            "installer stops until you choose: run %s --accept-core, or install "
            "skills only (no core)" % (section, command))

    for section, enabled in unhonoured_declines(config):
        if enabled is None:
            fix = "run: %s --migrate" % command
        else:
            fix = "it is enabled explicitly; run %s --disable %s to honour the refusal" % (
                command, section)
        warnings.append("config declined %s, but this toolkit would run it — %s"
                        % (section, fix))

    inferred = inferred_canonical_root(config)
    if inferred:
        warnings.append(
            "an older worktree guard inferred its canonical root from "
            "workspace_root (%s), so its Rule C was on; this toolkit never infers "
            "it, so Rule C is OFF here. Keep it on: %s --set "
            "worktree_guard.canonical_root=%s — leave it off: %s --set "
            "worktree_guard.canonical_root=null" % (inferred, command, inferred, command))

    sync_root = inferred_sync_root(config)
    if sync_root:
        warnings.append(
            "an older repo-freshness sync pulled every clean repo under %s, "
            "inferred from workspace_root; this toolkit never infers where to "
            "pull, so sync does nothing here. Keep it: %s --set "
            "repo_freshness.sync.repo_root=%s — leave it off: %s --set "
            "repo_freshness.sync.repo_root=null" % (sync_root, command, sync_root, command))

    known =example_top_keys | catalogue_sections | set(RETIRED_SECTIONS)
    for key in config:
        if key.startswith("//") or key in known:
            continue
        value = config[key]
        # An old section that only ever recorded an install decision is fully
        # accounted for by the declined-state handling above.
        if (key in DECLINED_SUCCESSORS and isinstance(value, dict)
                and set(value) <= {"installed"}):
            continue
        warnings.append(
            "config section %s is not read by any tool in this toolkit — if a "
            "tool was retired, its config migration did not ship "
            "(RETIRED_SECTIONS in lib/setup_config.py)" % key)
    return warnings


def apply_report(config_path, repo_root):
    """`--report`: print `config_report()`'s warnings, one per line, and
    always return 0 -- it informs, never fails the run reading it. An absent
    or unparseable config is never reported as silence: an empty report is
    what the installer reads as healthy, so silence has to be earned.
    """
    if not os.path.isfile(config_path):
        print("no config at %s — nothing to check" % shorten(config_path))
        return 0
    try:
        with open(config_path, encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        print("%s could not be parsed (%s) — every tool is reading defaults, so "
              "nothing here can be checked" % (shorten(config_path), exc))
        return 0
    for line in config_report(config, repo_root, config_path):
        print(line)
    return 0


# --- `--update` -------------------------------------------------------------

def load_existing_config(config_path):
    """Read config_path for `--update`. Never writes.

    Returns (True, config-dict) on success, or (False, message) on failure —
    file absent, not valid JSON, or not a JSON object. The caller exits 3 on
    failure without touching the file: an `--update` that creates a config on
    a missing path bypasses `validate_workspace_root`, and one that overwrites
    a file it could not parse destroys a person's config to fix a boolean.
    """
    if not os.path.isfile(config_path):
        return False, ("no config at %s — run --write first"
                       % shorten(config_path))
    try:
        # Explicit UTF-8: the ambient locale (ASCII under LC_ALL=C) would
        # raise on any non-ASCII value, and the except below would report a
        # VALID config as unparseable.
        with open(config_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        return False, "could not parse %s: %s" % (shorten(config_path), exc)
    if not isinstance(data, dict):
        return False, "%s is not a JSON object" % shorten(config_path)
    return True, data


def apply_update(config_path, repo, argv):
    """Change an existing config in place: flip `enabled` toggles, choose
    profiles, write validated keys (`--set`), carry an older config forward
    (`--migrate`) or accept the always-on guards (`--accept-core`).

    Unlike `non_interactive()`, this never calls `build_config()` -- it only
    merges into what is already on disk, so every key it did not touch
    survives unchanged, including `//` comments and anything
    `build_config()` doesn't know about.

    `--name`/`--workspace-root` are deliberately not registered: passing
    either makes argparse fail with "unrecognized arguments", which exits 2
    before anything is read or written. Every value is validated before the
    config is opened, so a refusal leaves the file byte-identical.
    """
    import argparse
    parser = argparse.ArgumentParser(prog="setup_config.py --update")
    parser.add_argument("--update", action="store_true")
    parser.add_argument("--enable", default="",
                        help="comma-separated tools/sections to write {\"enabled\": true}")
    parser.add_argument("--disable", default="",
                        help="comma-separated tools/sections to write {\"enabled\": false}")
    parser.add_argument("--profiles", default=None,
                        help="comma-separated profiles to write under \"profiles\" "
                             "(core and the requires closure are added)")
    parser.add_argument("--set", action="append", default=[],
                        help="PATH=VALUE, repeatable -- write one validated "
                             "config key, e.g. repo_freshness.sync.throttle_hours=12")
    parser.add_argument("--migrate", action="store_true",
                        help="carry an older config forward; never runs implicitly")
    parser.add_argument("--accept-core", action="store_true",
                        help="record that you accept the always-on guards an "
                             "older config declined")
    args = parser.parse_args(argv)

    if not (args.enable or args.disable or args.profiles is not None or args.set
            or args.migrate or args.accept_core):
        print("--update needs --enable, --disable, --profiles, --set, --migrate or "
              "--accept-core — a no-op rewrite would only lose whatever formatting "
              "is already there.", file=sys.stderr)
        return 2

    # Same catalogue validation as --write, before any file is touched.
    resolved = resolve_toggle_sections(args.enable, args.disable)
    if resolved is None:
        return 3
    enable_sections, disable_sections = resolved
    profile_names = None
    if args.profiles is not None:
        profile_names, error = parse_profiles(repo, args.profiles)
        if error:
            print(error, file=sys.stderr)
            return 3
    set_ops = resolve_set_operations(args.set, repo)
    if set_ops is None:
        return 3

    ok, result = load_existing_config(config_path)
    if not ok:
        print(result, file=sys.stderr)
        return 3
    config = result
    # --profiles rewrites caffeinate.enabled; a section that is not an
    # object cannot take the key, so refuse rather than crash mid-write.
    if profile_names and not isinstance(config.get("caffeinate", {}), dict):
        print("`caffeinate` in %s is not an object — fix it by hand, then re-run"
              % shorten(config_path), file=sys.stderr)
        return 3
    for section in enable_sections + disable_sections:
        if not isinstance(config.get(section, {}), dict):
            print("`%s` in %s is not an object — fix it by hand, then re-run"
                  % (section, shorten(config_path)), file=sys.stderr)
            return 3
    # The dict that will be written: --enable is judged against the NEW
    # profiles, not the ones on disk.
    if not refuse_unselected_profiles(
            repo, dict(config, profiles=profile_names) if profile_names else config,
            enable_sections):
        return 3

    if profile_names:
        apply_profiles(config, repo, profile_names)
    for section in enable_sections:
        config.setdefault(section, {})["enabled"] = True
    for section in disable_sections:
        config.setdefault(section, {})["enabled"] = False

    accepted = []
    if args.accept_core:
        for section in core_conflicts(config):
            config[section]["installed"] = True
            accepted.append(section)

    migrated, migrate_lines = (False, [])
    if args.migrate:
        migrated, migrate_lines = apply_migrate(config, repo)

    for path_str, segments, value, is_guard in set_ops:
        _set_nested(config, segments, value)

    # Nothing else was asked for, and the only actions found nothing to do:
    # say so and leave the file's bytes alone.
    only_maintenance = not (enable_sections or disable_sections or profile_names
                            or set_ops)
    if only_maintenance and not migrated and not accepted:
        for line in migrate_lines:
            print(line)
        if args.migrate:
            print("nothing to migrate")
        if args.accept_core:
            print("nothing to accept: no always-on guard is declined")
        return 0

    write_config(config_path, config)

    print("updated %s" % shorten(config_path))
    if enable_sections:
        print("  enabled        : %s" % ", ".join(enable_sections))
    if disable_sections:
        print("  disabled       : %s" % ", ".join(disable_sections))
    if profile_names:
        print("  profiles       : %s" % ", ".join(profile_names))
    for section in accepted:
        print("  core accepted  : %s" % section)
    for line in migrate_lines:
        print(line)
    for path_str, segments, value, is_guard in set_ops:
        label = "  guard setting  : " if is_guard else "  set            : "
        print("%s%s = %s" % (label, path_str, json.dumps(value)))
    return 0


def main():
    # Checked ahead of the positional-argument contract on purpose: the
    # catalogue is static repo data, not config, so listing it needs no
    # config-path or repo-root at all.
    if "--list-tools" in sys.argv[1:]:
        repo_root = resolve_repo_root()
        presence = tool_presence(repo_root)
        owners = tool_profiles(repo_root)
        # Strict like install.sh: a broken config or bad `profiles` is an
        # error, not a quiet fall back to the defaults.
        profiles = _sibling("profiles")
        chosen = None
        if profiles is not None and repo_root:
            chosen, error, _ = profiles.selected(repo_root)
            if error:
                print(error, file=sys.stderr)
                return 3
        groups = [dict(group, tools=[
                      dict(tool, present=presence.get(tool["name"], True),
                           profile=owners.get(tool["name"], "core"),
                           profile_selected=(chosen is None
                                             or owners.get(tool["name"], "core") in chosen))
                      for tool in group["tools"]])
                 for group in TOOL_CATALOGUE]
        print(json.dumps({"groups": groups}, indent=2))
        return 0

    if len(sys.argv) < 3:
        print("usage: setup_config.py <config-path> <repo-root> "
              "[--write ...|--update ...|--report]", file=sys.stderr)
        return 2
    config_path, repo = sys.argv[1], sys.argv[2]

    if "--report" in sys.argv[3:]:
        return apply_report(config_path, repo)

    if "--write" in sys.argv[3:]:
        return non_interactive(config_path, repo, sys.argv[3:])

    if "--update" in sys.argv[3:]:
        return apply_update(config_path, repo, sys.argv[3:])

    replace = "--replace" in sys.argv[3:]
    if not replace and refuse_existing(config_path):
        return 3

    if not sys.stdin.isatty():
        # Piped or CI. Say so rather than silently writing a guessed config.
        print("  no TTY — cannot prompt. Copy config.example.json to")
        print("  %s and edit it, or re-run in a terminal." % shorten(config_path))
        return 2

    print()
    print("  First-time setup. A few questions, then it writes")
    print("  %s." % shorten(config_path))
    print("  Press Enter to accept a [default]. Ctrl-C to abort.")
    print()

    # 1. Name — used when the guard tells Claude to hand a command back.
    default_name = ""
    for source in (os.environ.get("USER"), os.environ.get("LOGNAME")):
        if source:
            default_name = source.capitalize()
            break
    name = ask("  Your first name", default_name or None)

    # 2. Workspace root — the one answer that changes what the guard permits.
    print()
    print("  Your workspace root is the directory your work lives in. The")
    print("  danger guard treats deleting things inside it as ordinary, and")
    print("  deleting things outside it as suspect.")
    guess = guess_workspace_root(repo)
    while True:
        answer = ask("  Workspace root", guess)
        ok, result = validate_workspace_root(answer)
        if ok:
            workspace_root = result
            break
        print("  Rejected — %s" % result)

    # 3. Protected paths, derived rather than typed.
    protected = derive_protected_paths(workspace_root)
    print()
    if protected:
        print("  These are inside your workspace, so work happens in them, but")
        print("  deleting one *entirely* should never be a build step:")
        for path in protected:
            print("    - %s" % shorten(path))
        if not ask_yes_no("  Protect these from being deleted wholesale?", True):
            protected = []
    else:
        print("  No subdirectories found to protect yet. The workspace root")
        print("  itself is always protected regardless.")
    with_checkout = protect_the_checkout(protected, workspace_root, repo)
    if len(with_checkout) > len(protected):
        print("  This toolkit checkout is inside it, so it is protected too.")
    protected = with_checkout

    # 4. Handover directory.
    print()
    print("  /handover writes documents here, and the cleanup sweep moves")
    print("  files out of it — so give it a directory of its own.")
    while True:
        answer = ask("  Handover directory", default_handover_dir(workspace_root))
        ok, handover_dir = validate_user_data_dir(answer, repo)
        if ok:
            break
        print("  Rejected — %s" % handover_dir)

    # 5. Specs directory: optional, never guessed into place.
    print()
    print("  Plans and specs for bigger pieces of work are written to one")
    print("  directory, which the spec tools keep tidy. Leave it blank to skip;")
    print("  they will ask when they first need it.")
    specs_dir = None
    while True:
        answer = ask("  Specs directory", os.path.join(workspace_root, "specs"),
                     allow_empty=True)
        if not answer or answer == "-":
            break
        ok, result = validate_user_data_dir(answer, repo)
        if ok:
            specs_dir = result
            break
        print("  Rejected — %s (blank or - skips)" % result)

    # 6. /build review mode. Absent means "economical" (`build_review_mode()`'s
    # own fallback), so a typo or a skipped question can never silently select
    # the mode that needs Codex installed and authenticated.
    print()
    print("  /build's Code and Merge Review gates can run against Codex — more")
    print("  thorough, but needs `codex` installed and signed in — or stay")
    print("  entirely inside Claude, which needs nothing extra.")
    review_mode = "full-power" if ask_yes_no("  Use full-power (Codex) review mode?", False) else "economical"

    config = build_config(name, workspace_root, os.path.expanduser(handover_dir),
                          protected, specs_dir and os.path.expanduser(specs_dir),
                          review_mode)
    try:
        write_config(config_path, config, replace=replace)
    except FileExistsError:
        refuse_existing(config_path)
        return 3

    print()
    print("  Wrote %s" % shorten(config_path))
    print("    workspace root : %s" % shorten(workspace_root))
    print("    protected      : %d path(s)" % len(protected))
    print("    handover dir   : %s" % handover_dir)
    print("    specs dir      : %s" % (specs_dir or "not set"))
    print("    review mode    : %s" % review_mode)
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  aborted")
        sys.exit(1)
