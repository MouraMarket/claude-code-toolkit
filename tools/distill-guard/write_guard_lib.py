"""Judge whether a Write over an existing, in-scope file is denied.

Denies a `Write` whose target exists and is in scope for distill-guard
(`growth_lib.in_scope`) -- it skips the verbatim archive
`distill_guard.py install` builds otherwise. Allows: a new-file Write,
Edit/MultiEdit, an out-of-scope target, malformed input, any exception
(fail open, per "the guard fails open"). Install-safe targets (memory
entries/index, CLAUDE.md family) get the reason's stamp/re-read/
rebuild/install route; anything else (rules/topic files, tooling map)
gets Edit only. A Bash rewrite (`cat >`, `mv`/`cp`) is NOT caught --
out of scope, named not built; every refusal tells the session not to
take that route instead (`BASH_ROUTE_LINE`).

Hermetic: no `growth_lib`/`toolkit_config` import -- the executable
injects the callables.
"""
import os
import shlex
from collections import namedtuple

Verdict = namedtuple("Verdict", "allowed reason")

# The one route a refused session is likeliest to take next, and nothing
# refuses it (a Bash guard was judged too rare to be worth
# building, and deferred). So the refusal closes it in words. Must never
# contain "stamp" or "install": the Edit-only reason is pinned to exclude both.
BASH_ROUTE_LINE = (
    "Do not route around this with Bash (`cat >`, `tee`, or `cp`/`mv` over "
    "the file): that skips the archive the same way, and nothing will "
    "refuse it.")


def _reason(path, guard_path, offer_install_route):
    """The deny reason. `offer_install_route` gates clauses 3-4 of §1 --
    stamp/re-read/rebuild/install -- so a non-install-safe target's reason
    names Edit only and never mentions `stamp`, `install`, or `guard_path`.
    """
    lines = [
        "Write refused: %s already exists and is in scope for "
        "distill-guard." % path,
        "Use a targeted Edit to change it -- that is the normal way.",
    ]
    if offer_install_route:
        lines += [
            "",
            "To rewrite the whole file instead:",
            "  1. python3 %s stamp %s"
            % (shlex.quote(guard_path), shlex.quote(path)),
            "  2. re-read %s fresh -- a peer may have changed it since your "
            "last read" % path,
            "  3. rebuild the staged content from that fresh read into a "
            "new file",
            "  4. python3 %s install %s <staged-file> <token>"
            % (shlex.quote(guard_path), shlex.quote(path)),
        ]
    lines += [
        "",
        "A whole-file Write skips the verbatim archive, which is why this "
        "is refused.",
        BASH_ROUTE_LINE,
    ]
    return "\n".join(lines)


def decide(tool_name, tool_input, exists, in_scope, install_safe, guard_path):
    """A `Verdict` for one `PreToolUse` call. Never raises.

    `exists` / `in_scope` / `install_safe`: callable(path) -> bool, injected
    by the caller. `guard_path`: absolute path string to `distill_guard.py`.
    """
    try:
        if tool_name != "Write":
            return Verdict(True, None)
        if not isinstance(tool_input, dict):
            return Verdict(True, None)

        path = tool_input.get("file_path")
        if not isinstance(path, str) or not path:
            return Verdict(True, None)

        real = os.path.realpath(path)
        if not (in_scope(path) or in_scope(real)):
            return Verdict(True, None)

        if not exists(path):
            return Verdict(True, None)

        # Judged on the REALPATH only, never the given path: `distill_guard.py
        # install` does `target = os.path.realpath(target)` and derives the
        # archive from THAT, so a given path that merely looks install-safe
        # by name (a memory-shaped symlink whose target is a rules file, say)
        # must not offer a route that would archive into the rules tree.
        safe = install_safe(real)
        return Verdict(False, _reason(path, guard_path, safe))
    except Exception:
        return Verdict(True, None)
