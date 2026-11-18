"""Decide whether an `Agent` dispatch names its model.

Split from the hook executable on purpose. Two mechanisms here produce the same
observable outcome — a dispatch is allowed either because the CALL named a model
or because the DEFINITION pins one — and this repo has already been bitten by a
pair like that hiding each other (`resolve_git_dirs` in `lib/worktree_detect.sh`
exists for the same reason). `decide` therefore reports WHICH of the two allowed
it, so a test can assert the path rather than infer it from a verdict.

Everything in this module is pure: no clock, no filesystem, no environment. The
one function that must touch disk, `read_pinned_model`, takes its paths from the
caller and is the only thing here that can raise on I/O.
"""
import os
import re
import sys
from collections import namedtuple

# The verdict, plus the reason it came out that way. `path` is what makes the
# two allow-routes distinguishable; see the module docstring.
Verdict = namedtuple("Verdict", "allowed path reason")

ALLOW_EXPLICIT = "explicit"      # the dispatch itself named a model
ALLOW_PINNED = "pinned"          # the agent definition names a concrete model
ALLOW_NOT_GUARDED = "not_guarded"  # not a tool this guard judges
DENY_INHERITS = "inherits"       # nothing names a model; the session's is used

# `inherit` is a REAL value in agent frontmatter and it is not a pin — it means
# "use the session model", which is precisely the case this guard exists to
# catch. Plugin agent definitions commonly declare it. Treating "has a `model:` key"
# as "pins a model" therefore does not fail loudly on an edge case; it
# silently disables the guard for many plugin agents. `test_inherit_is_not_a_pin` and its mutation
# are what hold this line.
NOT_A_PIN = frozenset({"inherit", "inherits", ""})

# Only `Agent` is judged. A `Workflow`'s per-agent models are arguments inside a
# JavaScript source string; parsing them out to decide a denial would be a guess,
# and a guard that denies on a guess is one that gets switched off. `Workflow` is
# deliberately absent from this set rather than special-cased below, so that
# adding a tool name is the whole change.
GUARDED_TOOLS = frozenset({"Agent"})

_FRONTMATTER = re.compile(r"\A---\s*?\n(.*?)\n---\s*?(?:\n|\Z)", re.DOTALL)
_MODEL_LINE = re.compile(r"^model:\s*(.*?)\s*$", re.MULTILINE)


def decide(tool_name, tool_input, definition_model):
    """Allow or deny one dispatch.

    `definition_model` is what the agent's own definition declares, already
    resolved by the caller: a string, or None when there is no definition file
    (every built-in type) or it declares nothing. None and `inherit` are the
    same answer to the only question being asked — "will this dispatch fall back
    to the session model?" — and both deny.
    """
    if tool_name not in GUARDED_TOOLS:
        return Verdict(True, ALLOW_NOT_GUARDED, None)

    if not isinstance(tool_input, dict):
        # A payload we cannot read is not evidence of a missing model. Allow;
        # the hook's own fail-open direction, kept here so the pure function
        # agrees with the executable rather than relying on it.
        return Verdict(True, ALLOW_NOT_GUARDED, None)

    if _names_a_model(tool_input.get("model")):
        return Verdict(True, ALLOW_EXPLICIT, None)

    if _names_a_model(definition_model):
        return Verdict(True, ALLOW_PINNED, None)

    return Verdict(False, DENY_INHERITS, _reason(tool_input.get("subagent_type")))


def _names_a_model(value):
    """True only for a string naming a concrete model.

    Rejects None, non-strings, whitespace, and `inherit`. Case- and
    whitespace-insensitive because frontmatter is hand-written.
    """
    if not isinstance(value, str):
        return False
    return value.strip().lower() not in NOT_A_PIN


def _reason(subagent_type):
    name = subagent_type if isinstance(subagent_type, str) and subagent_type \
        else "this agent"
    return (
        # Named like claude-danger-guard's "BLOCKED by ..." so a user who
        # sees the refusal knows which hook to look up (row 76d, 28b F-D1d).
        "BLOCKED by claude-model-guard: "
        "Dispatching %s with no `model` inherits the session model — on an Opus "
        "session that pays Opus rates for work that is usually Sonnet's. "
        "Re-dispatch with an explicit `model` (e.g. \"sonnet\"), or pin one in "
        "the agent's own definition. `model: inherit` is not a pin. "
        # NOT "override for one call". A PreToolUse hook reads the environment
        # it inherited from the Claude Code process, so neither Claude nor the
        # user can set this for a single dispatch mid-session — it is a
        # launch-time, session-wide switch. `claude-danger-guard` words its own
        # escape hatch the same way, and this text is the actionable
        # instruction that reaches Claude on every denial, so overstating it
        # sends the reader after something they cannot do.
        "If you genuinely want this, re-run the session with "
        "MODEL_GUARD_ALLOW=1 set."
    ) % name


def read_pinned_model(subagent_type, agent_dirs):
    """The concrete model an agent definition pins, or None.

    Returns None for every case that means "no pin": no definition file, no
    `model:` line, or `model: inherit`. The caller cannot distinguish those and
    does not need to — `decide` treats them identically.

    `agent_dirs` is searched in order and the first hit wins, matching the
    harness's own precedence (project before user before plugin). A plugin type
    is namespaced `plugin:agent-name`; only the part after the colon is a
    filename.
    """
    if not isinstance(subagent_type, str) or not subagent_type:
        return None
    name = subagent_type.split(":")[-1]
    if not name:
        return None
    # NO SEPARATE TRAVERSAL GUARD. There was one — rejecting `..`, `/` and
    # `\` — and the exact-match check below made it UNREACHABLE: a traversing
    # name cannot be a member of `os.listdir`, which returns bare entry names.
    # Its mutation stopped failing, which this repo treats as a finding rather
    # than a curiosity, and it names the reason: an unreachable branch inside a
    # guard is worse than a constant, because it reads as protection while
    # nothing can exercise it. The traversal tests below are kept and now pass
    # because of the membership check, so the property is still pinned — by the
    # one mechanism that actually decides it.
    filename = name + ".md"
    for directory in agent_dirs:
        # LIST AND MATCH EXACTLY, rather than opening the joined path. macOS
        # APFS is case-INSENSITIVE, so `open(".../Explore.md")` happily returns
        # `explore.md` — an unrelated agent belonging to another plugin. That
        # is not a hypothetical: the built-in `Explore` resolved onto
        # claude-security's `explore.md`, read its `model: sonnet`, and the
        # guard ALLOWED a dispatch that inherits the session model. It failed
        # silently and selectively — `Plan` and `general-purpose` have no
        # colliding filename and denied correctly, so the guard looked like it
        # worked — and it was filesystem-dependent, giving a teammate on a
        # case-sensitive volume the opposite verdict from identical code.
        try:
            if filename not in os.listdir(directory):
                continue
        except OSError:
            continue
        try:
            with open(os.path.join(directory, filename), "r",
                      encoding="utf-8", errors="replace") as fh:
                # Frontmatter is at the top; a definition body can be long and
                # there is no reason to read it. 8 KiB is far past any real
                # frontmatter block and bounds a pathological file.
                head = fh.read(8192)
        except (OSError, ValueError):
            continue
        pinned = model_from_frontmatter(head)
        if pinned is not None:
            return pinned
        # A definition file that exists but pins nothing is a definitive "no
        # pin" — do NOT keep searching later directories for a same-named file.
        # The harness resolves the FIRST match and uses it whatever it says, so
        # continuing here would let a user-scope definition supply a pin the
        # dispatch will never actually get.
        return None
    return None


def model_from_frontmatter(text):
    """The `model:` value from a YAML frontmatter block, or None.

    Deliberately a narrow line match rather than a YAML parse: this runs in a
    `PreToolUse` hook on every dispatch, agent frontmatter is flat, and adding a
    parser dependency to a guard is how a guard acquires a new way to crash.
    Returns None when there is no frontmatter, no `model:` key, or the value is
    not a pin — the three cases the caller collapses anyway.
    """
    if not isinstance(text, str):
        return None
    block = _FRONTMATTER.match(text)
    if not block:
        return None
    found = _MODEL_LINE.search(block.group(1))
    if not found:
        return None
    # ORDER MATTERS, and the obvious order is wrong. Strip the comment FIRST:
    # on `model: "opus"  # why`, removing quotes first leaves `opus"  # why`
    # — the closing quote is no longer at either end — and the comment strip
    # then yields `opus"`, a model name nothing will ever match. Caught by
    # test_quotes_and_trailing_comments_are_stripped.
    value = found.group(1).strip()
    # Only whitespace-then-# opens a comment; a bare `#` inside a value does
    # not, which is why this is not a plain `split("#")`.
    value = re.split(r"\s+#", value, 1)[0].strip()
    value = value.strip("'\"").strip()
    if not _names_a_model(value):
        return None
    return value


def agent_dirs(cwd, home):
    """Directories holding agent definitions, in resolution order.

    Project scope first, then user scope, then plugin marketplaces — the order
    the harness itself resolves in. Plugin directories are globbed by the caller
    only if the earlier two miss, but they are listed here so the whole search
    path is one readable thing.
    """
    dirs = []
    if isinstance(cwd, str) and cwd:
        dirs.append(os.path.join(cwd, ".claude", "agents"))
    if isinstance(home, str) and home:
        dirs.append(os.path.join(home, ".claude", "agents"))
    return dirs


def plugin_agent_dirs(home, plugin=None):
    """`agents/` directories of the installed plugins.

    SCOPED TO ONE PLUGIN when `plugin` is given, which is the only way this is
    called in production. A `subagent_type` for a plugin agent is namespaced
    (`plugin-dev:agent-creator`), so searching every plugin's `agents/` for a
    bare `helper.md` matches whichever plugin happens to ship that name — a
    definition the harness would never have consulted for that dispatch.
    A scoped lookup goes through `plugin_paths.plugin_root`, because a
    directory-source plugin (the `claude-code-toolkit` checkout) lives in its checkout,
    not under `marketplaces/*/plugins/`, and a glob there never finds it.
    `plugin=None` enumerates every hosted plugin and exists for tests.

    Returns [] on any I/O trouble rather than raising — a guard that cannot
    enumerate plugins should fall through to a denial, not crash.
    """
    if not isinstance(home, str) or not home:
        return []
    if plugin is not None:
        # Imported here, not at module top: a checkout with no `lib/` sibling
        # must still import this module so bare types keep their verdicts. A
        # namespaced lookup then finds nothing and falls through to a denial.
        lib = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.realpath(__file__)))), "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        try:
            import plugin_paths
        except ImportError:
            return []
        root = plugin_paths.plugin_root(home, plugin)
        if root is None:
            return []
        candidate = os.path.join(root, "agents")
        return [candidate] if os.path.isdir(candidate) else []
    root = os.path.join(home, ".claude", "plugins", "marketplaces")
    found = []
    try:
        for marketplace in sorted(os.listdir(root)):
            plugins = os.path.join(root, marketplace, "plugins")
            try:
                for name in sorted(os.listdir(plugins)):
                    candidate = os.path.join(plugins, name, "agents")
                    if os.path.isdir(candidate):
                        found.append(candidate)
            except OSError:
                continue
    except OSError:
        return []
    return found


def search_path(subagent_type, cwd, home):
    """The ONE ordered directory list to resolve `subagent_type` against.

    A single list, searched by a single `read_pinned_model` call, because the
    "first definition found decides" invariant that function documents can only
    hold if one call sees the whole path. Splitting the search into two calls
    breaks it: `read_pinned_model` returns None both for "no file here" and for
    "file found, pins nothing", so a second call cannot tell them apart and
    goes looking for a pin the dispatch will never receive. That is the
    wiring-layer defect this repo already recorded once from `base-pin` — the
    pure predicate was right and the code feeding it was not.

    A NAMESPACED type is resolved against its own plugin only. A BARE type is
    never resolved against plugin directories at all: built-ins and user- or
    project-scoped agents are bare, and reaching into plugins for one is how a
    built-in agent picks up an unrelated plugin agent's model.
    """
    if isinstance(subagent_type, str) and ":" in subagent_type:
        return plugin_agent_dirs(home, plugin=subagent_type.split(":", 1)[0])
    return agent_dirs(cwd, home)
