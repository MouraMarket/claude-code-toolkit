#!/usr/bin/env python3
"""Permission-rule parsing and matching, shared by the scanner and the replay.

This module exists because both scripts used to model **Bash only**, against a
**single** settings file. Two blind spots followed from that, and the second one
is what let hand-enumerated MCP rules accumulate across three settings
files without anyone noticing:

  1. `scan_transcripts.py` filtered tool calls to `name == "Bash"` and discarded
     the rest, so MCP, WebFetch and file-tool calls were never counted.
  2. Both scripts read one `--settings` path, but a real session merges user,
     project and local scopes. Replaying against `~/.claude/settings.json` alone
     over-reports prompting for anything granted in the workspace files.

## The MCP matcher is a port, not a guess

`mcp_rule_matches` mirrors Claude Code's own matcher, read out of the 2.1.246
binary on 2026-08-29 rather than from the prose docs, which do not describe it:

    function me(e){let t=e.split("__"),[n,o,...s]=t;
      if(n!=="mcp"||!o)return null;
      let d=s.length>0?s.join("__"):void 0;
      return{serverName:o,toolName:d}}

    function Ea(e,t){let n=me(e),o=me(t);
      return n!==null&&o!==null&&n.serverName===o.serverName&&
        (n.toolName===void 0||n.toolName==="*"||
         o.toolName!==void 0&&fe(n.toolName)&&Qt(n.toolName,o.toolName))}

The consequence that matters: **a rule naming only a server matches every tool
on it.** `mcp__tracker` and `mcp__tracker__*` both grant the
whole server, because `toolName === undefined` and `toolName === "*"` each
short-circuit before any pattern comparison. That is the one-rule-per-server
form this skill recommends, and it is why enumerating every tool of one server was
never necessary.

**The partial-pattern branch: the validator accepts it, the matcher is
unread.** Two separate facts, and an earlier version of this file collapsed
them into one false claim.

- **Verified:** Claude Code's rule *validator* accepts a tool-position glob in
  an allow rule and offers one as an example. Its wildcard error reads
  *"Wildcard tool name ... is not supported in allow rules"* with
  `examples: ["mcp__puppeteer__*", "mcp__github__get_*"]` -- the second is a
  partial pattern. So `mcp__server__get_*` is a legal allow rule.
- **NOT verified:** whether `Ea`'s `fe(pattern) && Qt(pattern, tool)` branch
  globs the way `fnmatch` does. Those are minified scope-local names colliding
  with an unrelated bundled regexp parser (19 and 20 distinct definitions), so
  a `strings` read cannot say which pair is in scope.

This file previously asserted that the two whole-server forms "are the only two
Claude Code's own invalid-rule error text suggests." **That was false**, read
off the narrower *"MCP rules do not support patterns in parentheses"* error and
over-generalised; the binary's own examples contradict it. Corrected 2026-08-29
after a blinded review checked the primary source. It is recorded rather than
quietly deleted because the mistake was citing a document as evidence without
grepping it -- the failure mode that keeps recurring here.

`propose_server_rules` still never emits a partial pattern, now on a narrower
and honest ground: the whole-server form is simpler, covers the case, and does
not depend on unread matching semantics. A partial rule a human writes IS
modelled, so it is never falsely reported as prompting.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from typing import Iterable, NamedTuple

# Whether `Ea`'s `fe(pattern) && Qt(pattern, tool)` branch has been confirmed to
# glob the way this module's fnmatch does. It has NOT -- see the docstring.
# Distinct from whether such a rule is *legal*, which is verified: the binary's
# validator offers `mcp__github__get_*` as an allow-rule example.
PARTIAL_GLOB_MATCHING_VERIFIED = False
PARTIAL_GLOB_RULES_ARE_LEGAL = True  # verified against 2.1.246's validator


# --- Bash rule matching ------------------------------------------------------
#
# Ported from the Claude Code 2.1.292 binary on 2026-10-09 (functions read and
# executed under node; `tests/test_bash_rule_matcher.py` holds the native
# outputs as an oracle). `fnmatch` is NOT this matcher: only `*` is a wildcard
# (`?` and `[` are literal), `\*` is a literal star, `:*` makes a prefix rule,
# and a trailing ` *` also matches the bare head.
#
#     function HAe(e){return e.match(/^(.+):\*$/)?.[1]??null}
#     function Lpo(e){if(e.endsWith(":*"))return!1;for(let n=0;n<e.length;n++)
#       if(e[n]==="*"){let s=0,r=n-1;while(r>=0&&e[r]==="\\")s++,r--;
#       if(s%2===0)return!0}return!1}
#     function t_r(e){let n=e.trimEnd();if(!n.endsWith("*"))return!1;let s=0,
#       r=n.length-2;while(r>=0&&n[r]==="\\")s++,r--;return s%2===0}
#     function Z7(e,n,s=!1,r=!1){if(e.includes("\x00"))return!1;
#       let i=e.trim(),d=r?i.replace(/[ \t]+/g," "):i,
#       c=r?n.replace(/[ \t]+/g," "):n,p="",g=0;
#       while(g<d.length){let P=d[g];if(P==="\\"&&g+1<d.length){let K=d[g+1];
#       if(K==="*"){p+="\x00ESCAPED_STAR\x00",g+=2;continue}
#       else if(K==="\\"){p+="\x00ESCAPED_BACKSLASH\x00",g+=2;continue}}p+=P,g++}
#       let S=p.replace(/[.+?^${}()|[\]\\'"]/g,"\\$&")
#         .replace(em,"\x00GLOBSTAR\x00").replaceAll("*",".*")
#         .replace(tm,"/(?:.*/)?").replace(Zg,"\\*").replace(Qg,"\\\\"),
#       b=(p.match(/\*/g)||[]).length;
#       if(S.endsWith(" .*")&&b===1)S=S.slice(0,-3)+"( .*)?";
#       let w="s"+(s?"i":"");return new RegExp(`^${S}$`,w).test(c)}
#     function Cit(e){let n=HAe(e);if(n!==null)return{type:"prefix",prefix:n};
#       if(Lpo(e))return{type:"wildcard",pattern:e};
#       return{type:"exact",command:e}}
#     // em=/\/(?:\*\*\/)+/g ; OM(e,n)=Z7(e,n,false,true)
#     // "prefix" mode, one subcommand Se, bucket b:
#     switch(ke.type){case"exact":return ke.command===Se;
#       case"prefix":{let Te=ke.prefix.replace(/[ \t]+/g," "),
#         Ce=Se.replace(/[ \t]+/g," ");if(Ce===Te)return!0;
#         if(Ce.startsWith(Te+" "))return!0;let xe="xargs "+Te;
#         if(Ce===xe)return!0;return Ce.startsWith(xe+" ")}
#       case"wildcard":if(OM(ke.pattern,Se))return!0;
#         if(b!=="deny"&&b!=="ask"&&!t_r(ke.pattern))return!1;
#         return OM(`xargs ${ke.pattern}`,Se)}
#
# Escaped stars are protected first, then metacharacters are escaped, then
# `/**/` runs, then `*` -- the order matters.

_WS_RUN = re.compile(r"[ \t]+")
_STAR_SENTINEL = "\x00ESCAPED_STAR\x00"
_BACKSLASH_SENTINEL = "\x00ESCAPED_BACKSLASH\x00"
_GLOBSTAR_SENTINEL = "\x00GLOBSTAR\x00"
_REGEX_META = re.compile(r"[.+?^${}()|\[\]\\'\"]")
_GLOBSTAR_RUN = re.compile(r"/(?:\*\*/)+")


def _has_unescaped_star(text: str) -> bool:
    for i, ch in enumerate(text):
        if ch == "*":
            slashes, j = 0, i - 1
            while j >= 0 and text[j] == "\\":
                slashes += 1
                j -= 1
            if slashes % 2 == 0:
                return True
    return False


def _ends_in_unescaped_star(text: str) -> bool:
    trimmed = text.rstrip()
    if not trimmed.endswith("*"):
        return False
    slashes, j = 0, len(trimmed) - 2
    while j >= 0 and trimmed[j] == "\\":
        slashes += 1
        j -= 1
    return slashes % 2 == 0


def parse_bash_rule(content: str) -> tuple[str, str]:
    """Classify a Bash rule's content: ("prefix"|"wildcard"|"exact", payload)."""
    if content.endswith(":*") and len(content) > 2:
        return ("prefix", content[:-2])
    if _has_unescaped_star(content):
        return ("wildcard", content)
    return ("exact", content)


def bash_wildcard_matches(pattern: str, command: str) -> bool:
    """Port of `Z7` with collapseWhitespace=True, case-sensitive."""
    if "\x00" in pattern:
        return False
    collapsed = _WS_RUN.sub(" ", pattern.strip())
    command = _WS_RUN.sub(" ", command)
    protected, i = "", 0
    while i < len(collapsed):
        ch = collapsed[i]
        if ch == "\\" and i + 1 < len(collapsed):
            nxt = collapsed[i + 1]
            if nxt == "*":
                protected += _STAR_SENTINEL
                i += 2
                continue
            if nxt == "\\":
                protected += _BACKSLASH_SENTINEL
                i += 2
                continue
        protected += ch
        i += 1
    body = _REGEX_META.sub(lambda m: "\\" + m.group(0), protected)
    body = _GLOBSTAR_RUN.sub(_GLOBSTAR_SENTINEL, body)
    body = body.replace("*", ".*")
    body = body.replace(_GLOBSTAR_SENTINEL, "/(?:.*/)?")
    body = body.replace(_STAR_SENTINEL, "\\*")
    body = body.replace(_BACKSLASH_SENTINEL, "\\\\")
    if body.endswith(" .*") and protected.count("*") == 1:
        body = body[:-3] + "( .*)?"
    return re.fullmatch(body, command, re.DOTALL) is not None


def bash_rule_matches(content: str, command: str, bucket: str = "allow") -> bool:
    """Does one Bash rule match one subcommand? `bucket`: allow, ask or deny."""
    kind, payload = parse_bash_rule(content)
    if kind == "exact":
        return payload == command
    if kind == "prefix":
        prefix = _WS_RUN.sub(" ", payload)
        cmd = _WS_RUN.sub(" ", command)
        xargs = "xargs " + prefix
        return (cmd == prefix or cmd.startswith(prefix + " ")
                or cmd == xargs or cmd.startswith(xargs + " "))
    if bash_wildcard_matches(payload, command):
        return True
    if bucket not in ("deny", "ask") and not _ends_in_unescaped_star(payload):
        return False
    return bash_wildcard_matches("xargs " + payload, command)

# Settings scopes in precedence order, lowest first. A real session merges all
# of them; modelling one file is how a config looks worse than it is.
SETTINGS_SCOPES = (
    ("user", "~/.claude/settings.json"),
    ("project", "{workspace}/.claude/settings.json"),
    ("local", "{workspace}/.claude/settings.local.json"),
)

# Built-in tools believed to run without a prompt regardless of rules.
#
# **This list is NOT verified and must not be treated as one.** Claude Code
# declares per-tool exemption internally as `isReadOnly:()=>!0`, but those
# declarations are minified away from any adjacent tool name, so a `strings`
# read of the 2.1.246 binary could not enumerate them (attempted 2026-08-29).
# The list below is inference from observed behaviour.
#
# The consequence is deliberate: an uncovered call to one of these is reported
# under NOT MODELLED and never counted into the "would prompt" percentage that
# gates a config. Counting a guess into a gate number is how a threshold stops
# meaning anything -- an early cut of this change reported a config as
# prompting entirely on the strength of `SendMessage` and `Agent` calls that
# almost certainly never prompted.
#
# `KNOWN_PROMPTING_TOOLS` below is the other half of the same judgement, and the
# two must not be conflated: this set is "believed exempt, unverified", that one
# is "documented as prompting". Only genuine unknowns are excused.
ALWAYS_FREE_TOOLS = {
    "Read", "Glob", "Grep", "LS", "NotebookRead", "TodoWrite", "Task",
    "AskUserQuestion", "ExitPlanMode", "EnterPlanMode", "ListAgents",
    "ToolSearch", "BashOutput", "KillShell",
    "TaskOutput", "TaskStop", "Monitor",
}


# Built-in tools KNOWN to prompt when no rule covers them, from this repo's own
# artifacts: `references/permission-mechanics.md` documents `acceptEdits` as the
# mode that auto-accepts file edits (so `default` does not) and recommends
# `WebFetch(domain:...)` rules, and `claude/permissions.autonomous.json`
# allow-lists every name below.
#
# These exist because "is it MCP" was the wrong test for "is its status known".
# Using it meant an uncovered `Edit`, `Write` or `WebFetch` call landed in the
# not-modelled bucket and was struck from the gate number -- on a
# baseline-shaped config, every file and network call was excluded while the headline
# read 0%. That exclusion is one-directional and lands hardest on a
# user with only the baseline set applied, which is the person least able
# to notice it.
#
# `Skill` (and `SlashCommand`, its former name) sat in ALWAYS_FREE_TOOLS on the
# same unverified belief, and it was wrong: model-invoked skills prompt, and
# real transcripts hold rejected `Skill(...)` calls. Filed as exempt,
# those prompts were struck from every audit, so the fix was never proposed.
KNOWN_PROMPTING_TOOLS = {
    "Edit", "Write", "MultiEdit", "NotebookEdit", "WebFetch", "WebSearch",
    "Skill", "SlashCommand",
}


def is_modelled(tool_name: str) -> bool:
    """Can this script state a confident verdict for this tool?

    True for MCP (the matcher is a verified port) and for the built-ins whose
    prompting status this repo documents. False only for a tool of genuinely
    unknown status -- a new built-in, or one whose exemption is undocumented.
    """
    return (tool_name.startswith("mcp__")
            or tool_name in KNOWN_PROMPTING_TOOLS)

# MCP tool-name fragments that indicate an outward-facing or destructive call.
# Used only to SORT a proposal for human review -- never to auto-decide. The
# distinction matters: a keyword list is a prompt for judgement, not a
# classifier, and presenting it as the latter is how a `delete` rule gets
# skipped because the word happened not to appear.
DESTRUCTIVE_HINTS = (
    "delete", "trash", "remove", "archive", "void", "merge",
    "send", "publish", "share", "invite", "assign",
)


class ParsedRule(NamedTuple):
    """A permission rule split into the parts Claude Code matches on."""
    raw: str
    tool: str            # "Bash", "Read", "mcp__server__tool", ...
    content: str | None  # the text inside parentheses, if any
    scope: str           # which settings file it came from


class McpRef(NamedTuple):
    """The result of Claude Code's `me()` -- a parsed mcp__ identifier."""
    server: str
    tool: str | None     # None is the whole-server form


def parse_mcp(name: str) -> McpRef | None:
    """Port of `me()`. Returns None for anything that is not an mcp__ name.

    Mirrors the original's quirks deliberately: the server segment must be
    non-empty, and a name with no tool segment yields tool=None (the
    whole-server form) rather than an empty string.
    """
    parts = name.split("__")
    if len(parts) < 2 or parts[0] != "mcp" or not parts[1]:
        return None
    rest = parts[2:]
    return McpRef(parts[1], "__".join(rest) if rest else None)


def mcp_rule_matches(rule: str, tool_name: str) -> bool:
    """Port of `Ea()`. Does `rule` grant `tool_name`?

    The two verified forms -- a bare server, and a server with `__*` -- match
    every tool on that server. Anything else falls to the pattern branch that
    `PARTIAL_GLOBS_VERIFIED` disclaims.
    """
    r = parse_mcp(rule)
    t = parse_mcp(tool_name)
    if r is None or t is None or r.server != t.server:
        return False
    if r.tool is None or r.tool == "*":
        return True
    if t.tool is None:
        return False
    return fnmatch.fnmatchcase(t.tool, r.tool)


def parse_rule(raw: str, scope: str = "?") -> ParsedRule:
    """Split `Tool(content)` into its parts. `Tool` alone has content None."""
    if raw.endswith(")") and "(" in raw:
        head, _, tail = raw.partition("(")
        return ParsedRule(raw, head, tail[:-1], scope)
    return ParsedRule(raw, raw, None, scope)


def load_rules(workspace: str | None = None,
               settings_path: str | None = None) -> dict[str, list[ParsedRule]]:
    """Merge allow/ask/deny across every settings scope that exists.

    Pass `settings_path` to model one specific file instead -- that is what
    `--settings /tmp/proposed.json` needs, and it deliberately does NOT merge,
    so a proposal can be evaluated on its own terms.
    """
    out: dict[str, list[ParsedRule]] = {"allow": [], "ask": [], "deny": []}
    if settings_path:
        sources = [("proposed", settings_path)]
    else:
        ws = workspace or os.path.expanduser("~")
        sources = [(name, tpl.format(workspace=ws))
                   for name, tpl in SETTINGS_SCOPES]
    for scope, path in sources:
        path = os.path.expanduser(path)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        perms = data.get("permissions") or {}
        for bucket in out:
            for raw in perms.get(bucket) or []:
                out[bucket].append(parse_rule(raw, scope))
    return out


def verdict(tool_name: str, rules: dict[str, list[ParsedRule]]) -> str:
    """Return "deny", "ask", "allow" or "prompt" for a non-Bash tool call.

    Precedence follows Claude Code: deny, then ask, then allow. "prompt" means
    no rule matched -- under `defaultMode: auto` the classifier then decides,
    which is a judgement call this script cannot model and does not pretend to.
    Bash is excluded because its matching is per-subcommand; `replay_permissions`
    owns that path.
    """
    for bucket in ("deny", "ask", "allow"):
        for rule in rules[bucket]:
            if _rule_covers(rule, tool_name):
                return bucket if bucket != "allow" else "allow"
    return "prompt"


def _rule_covers(rule: ParsedRule, tool_name: str) -> bool:
    if tool_name.startswith("mcp__"):
        return mcp_rule_matches(rule.raw, tool_name)
    if rule.tool != tool_name:
        return False
    # A bare `Write` grants the tool; `Write(path)` is a path rule this module
    # does not evaluate -- see references/permission-mechanics.md for why those
    # are accepted-but-never-consulted for some tools.
    return rule.content is None


def looks_destructive(tool_name: str) -> bool:
    """Heuristic ONLY, to order a proposal for review. Never an auto-decision."""
    ref = parse_mcp(tool_name)
    leaf = (ref.tool if ref and ref.tool else tool_name).lower()
    return any(h in leaf for h in DESTRUCTIVE_HINTS)


def propose_server_rules(
    called: Iterable[str],
    rules: dict[str, list[ParsedRule]],
) -> dict[str, dict]:
    """Group observed MCP calls by server and describe the consolidation.

    Returns, per server: the calls seen, which already have an exact rule, which
    would prompt, and the destructive-looking tools a whole-server grant would
    sweep in -- the last being the number a human actually needs to see before
    accepting one rule in place of forty.
    """
    servers: dict[str, dict] = {}
    for name in called:
        ref = parse_mcp(name)
        if ref is None:
            continue
        s = servers.setdefault(ref.server, {
            "calls": 0, "tools": set(), "prompting": set(),
            "server_rule_present": False,
        })
        s["calls"] += 1
        s["tools"].add(name)
        if verdict(name, rules) == "prompt":
            s["prompting"].add(name)
    for server, s in servers.items():
        s["server_rule_present"] = any(
            (r.raw == f"mcp__{server}" or r.raw == f"mcp__{server}__*")
            for r in rules["allow"])
        s["destructive_seen"] = sorted(t for t in s["tools"]
                                       if looks_destructive(t))
        s["proposed_rule"] = f"mcp__{server}"
    return servers
