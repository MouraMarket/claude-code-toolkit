#!/usr/bin/env python3
"""Replay real tool history against a settings.json and count what would prompt.

`scan_transcripts.py` answers "what does the user run?". This answers the
question that actually matters after you have written the config: **would the
config you just wrote have stopped the interruptions?**

It replays **every tool**, not just Bash. It modelled Bash alone until
2026-08-29, which meant a config could score a clean 0% while every call to a
newly-added MCP server still prompted -- the exact failure that sent this skill
back for revision. `--bash-only` restores the old behaviour for comparison.

It exists because that question was once answered by inspection, and the answer
was wrong. A mined allowlist looked comprehensive, covered every command
family in the scan, and still left the majority of real invocations prompting --
because the calls that prompt are disproportionately the ones that are not
command families at all (scripts invoked by path, exec wrappers, `cd` into a
new directory followed by `git`).

Run it before claiming a permissions change works, and again after:

    python3 replay_permissions.py                     # current settings
    python3 replay_permissions.py --settings /tmp/proposed.json
    python3 replay_permissions.py --verbose           # show the commands

Exit status is 0 if fewer than 5% of calls would prompt, 1 otherwise, so it can
gate a change. Calls matching an `ask` rule are reported apart (PROMPTS BY
DESIGN) and left out of that percentage; calls matching a `deny` rule count as
not prompting and are listed under DENIED.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from scan_transcripts import (  # noqa: E402
    ALWAYS_FREE, EXEC_WRAPPERS, HEREDOC, PREFIX_UNAPPROVABLE, STRIPPED_WRAPPERS,
    find_is_free, git_is_free, git_subcommand, load_events, output_redirects,
    strip_wrappers, subcommands,
)
import permission_rules as rules_lib  # noqa: E402

# `cd` alone is free and `git log` alone is free, but relying on the *built-in
# read-only set* for both is not: git executes hooks from the new directory, so
# Claude Code prompts when the directory actually changes.
#
# Verified 2026-07-30 against v2.1.215: this caveat governs only the no-rule
# case. An explicit allow rule that matches the git subcommand (`Bash(git *)`,
# or a blanket `Bash(*)`) permits it and no prompt occurs. So this penalty
# applies ONLY when the git segment was free by built-in rather than by rule --
# an earlier version of this script charged it unconditionally and reported
# phantom prompts against a config that was in fact clean.
CD_THEN_GIT = re.compile(r"\bcd\s+\S+[^\n]*(?:&&|;)\s*git\b")


def bash_patterns(settings: dict, bucket: str = "allow") -> list[str]:
    """Extract the Bash patterns from one rule bucket.

    `bucket` exists because the `ask` bucket needs the identical extraction and
    an earlier version only ever read `allow`. That left Bash `ask` rules
    counted as *no prompt* -- the exact defect this script's non-Bash half was
    written to fix, still live on the surface where this repo's own shipped ask
    rules are (`git push`, `gh pr create/merge`, `gh release`, `npm publish` in
    `claude/permissions.baseline.json`). Before the fix, calls to
    exactly those commands reported 0% prompting with no PROMPTS BY DESIGN
    section at all.
    """
    rules = (settings.get("permissions") or {}).get(bucket) or []
    pats = []
    for rule in rules:
        if rule == "Bash":
            return ["*"]
        if rule.startswith("Bash(") and rule.endswith(")"):
            pats.append(rule[5:-1])
    return pats


def would_prompt(segment: str, patterns: list[str]) -> str | None:
    """Return a reason string if this subcommand prompts, else None.

    The sentinel "" means "free, but only via the built-in read-only set" --
    the caller needs that to evaluate the cd-then-git caveat.
    """
    tokens = strip_wrappers(segment.split())
    if not tokens:
        return None

    normalised = " ".join(tokens)
    # What an ALLOW rule is matched against: only known-safe assignments are
    # skipped, so `Bash(npm test)` covers `NODE_ENV=test npm test` but not
    # `FOO=bar npm test`. `tokens` above strips every assignment, because the
    # head classification below needs the real command either way.
    rule_tokens = strip_wrappers(segment.split(), safe_env_only=True)
    rule_view = " ".join(rule_tokens)
    # A write through an output redirect is judged separately from the command:
    # no prefix rule covers it, only a tool-wide grant (bare `Bash` / `Bash(*)`).
    # Deliberately NOT gated on classify(): a bare `> f`, `: > f` and
    # `! echo x > f` all write a file while classify() returns None for them
    # (Code Review). The price is a line the splitter cut out of a multi-line
    # quoted argument (a regex holding `[^>]`) also counting as a prompt --
    # a small share of redirect hits, in the loud direction.
    writes = output_redirects(normalised)
    if (writes is None or writes) and "*" not in patterns:
        return "output redirect (no prefix rule covers the write)"
    # Same head canonicalisation as classify(), so the two cannot disagree.
    if tokens[0].strip("\"'()`") == "find":
        if find_is_free(tokens[1:]):
            return None
        # No prefix rule covers a mutating find; a tool-wide grant (bare `Bash`
        # or `Bash(*)`, both "*" here) or an exact rule does. Only `*` is a
        # wildcard in a Bash rule -- `?` and `[` are literal.
        if "*" in patterns or any(rules_lib.parse_bash_rule(p)[0] == "exact" and p == rule_view
                                  for p in patterns):
            return None
        return "find (no prefix rule covers it)"
    prefix_head = os.path.basename(tokens[0].strip("\"'()`")).strip("$")
    if prefix_head in PREFIX_UNAPPROVABLE:
        # Same shape as a mutating find: only a tool-wide grant or an exact rule.
        if "*" in patterns or any(rules_lib.parse_bash_rule(p)[0] == "exact" and p == rule_view
                                  for p in patterns):
            return None
        return f"{prefix_head} (no prefix rule covers it)"
    if any(rules_lib.bash_rule_matches(p, rule_view, "allow") for p in patterns):
        return None  # covered by an explicit rule

    raw = tokens[0].strip("\"'()`")
    head = os.path.basename(raw).strip("$")

    if raw.startswith("$("):
        return None  # command substitution, not a command being invoked
    if raw.startswith(("/", "./", "../", "~", "$")) or raw.startswith('"$'):
        return "path-invoked"
    if raw.startswith("-") and len(tokens) < len(segment.split()):
        # A wrapper's argument Claude Code leaves in place (`time -p x` is
        # matched as `-p x`): no prefix rule covers it, so it prompts. Not
        # quote bleed, which is what the check below would take it for.
        return f"{raw} (wrapper argument left as the command)"
    if not re.match(r"^[a-z0-9][a-z0-9._+-]*$", head, re.IGNORECASE):
        return None  # quote/heredoc bleed, not a real command
    if head == "git":
        # free only via the built-in set -- signal that with the sentinel
        if git_is_free(tokens[1:]):
            return ""
        sub, _ = git_subcommand(tokens[1:])
        return f"git {sub}".strip()
    if head in ALWAYS_FREE:
        return None
    if head in EXEC_WRAPPERS or head in STRIPPED_WRAPPERS:
        return f"{head} (exec wrapper)"
    return head


def _matches_any(segment: str, patterns: list[str], bucket: str = "allow") -> bool:
    """Does this subcommand match any of these Bash patterns?

    Mirrors `would_prompt`'s normalisation exactly -- env assignments and
    wrapper words stripped, then the native Bash rule matcher -- so an `ask`
    rule and an `allow` rule are judged against the identical string.
    """
    tokens = strip_wrappers(segment.split())
    if not tokens:
        return False
    normalised = " ".join(tokens)
    return any(rules_lib.bash_rule_matches(p, normalised, bucket) for p in patterns)


def _default_project_dir():
    """Shared with scan_transcripts.py — derive from the configured workspace."""
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from scan_transcripts import default_project_dir
    return default_project_dir()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--settings", default=None,
                    help="model ONE settings file instead of merging scopes")
    ap.add_argument("--workspace", default=None,
                    help="workspace root, for locating project/local scopes")
    ap.add_argument("--project", default=None)
    ap.add_argument("--limit", type=int, default=250)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--bash-only", action="store_true",
                    help="pre-2026-08-29 behaviour: ignore non-Bash tools")
    ap.add_argument("--threshold", type=float, default=5.0,
                    help="fail if more than this %% of calls would prompt")
    args = ap.parse_args()
    if args.project is None:
        args.project = _default_project_dir()
    if args.workspace is None:
        from scan_transcripts import _configured_workspace
        args.workspace = _configured_workspace()

    # A bare --settings models that file ALONE, deliberately: a proposal has to
    # stand on its own terms, not silently borrow the live config's grants.
    perm_rules = rules_lib.load_rules(workspace=args.workspace,
                                      settings_path=args.settings)
    if args.settings:
        with open(args.settings, encoding="utf-8") as fh:
            settings = json.load(fh)
    else:
        settings = {"permissions": {b: [r.raw for r in perm_rules[b]]
                                    for b in ("allow", "ask", "deny")}}
    patterns = bash_patterns(settings)
    ask_patterns = bash_patterns(settings, "ask")
    deny_patterns = bash_patterns(settings, "deny")

    events = load_events(args.project)
    calls = [i.get("command", "") for _, n, i in events if n == "Bash"]
    recent = calls[-args.limit:]
    # Refuse only when there is nothing to replay at all: an MCP-only history
    # is exactly the surface the non-Bash half below exists to audit.
    if not recent and (args.bash_only or not any(n != "Bash" for _, n, _ in events)):
        print("No %s calls found in transcripts."
              % ("Bash" if args.bash_only else "tool"), file=sys.stderr)
        return 1

    reasons: Counter = Counter()
    examples: dict[str, str] = {}
    by_design: Counter = Counter()
    denied: Counter = Counter()
    prompting = 0
    bash_total = 0
    for cmd in recent:
        # deny > ask > allow. A denied call is blocked, not prompted: it stays in
        # the total as not prompting, exactly as a denied non-Bash call does below.
        blocked = [s for s in subcommands(cmd)
                   if _matches_any(s, deny_patterns, "deny")]
        if blocked:
            bash_total += 1
            denied[f"Bash: {blocked[0][:60]}"] += 1
            continue
        # An `ask` rule outranks `allow`, so a Bash command matching one prompts
        # no matter what the allow list says. Checked BEFORE the allow replay.
        asked = [s for s in subcommands(cmd)
                 if _matches_any(s, ask_patterns, "ask")]
        if asked:
            by_design[f"Bash: {asked[0][:60]}"] += 1
            continue
        bash_total += 1
        verdicts = [would_prompt(s, patterns) for s in subcommands(cmd)]
        hits = {r for r in verdicts if r}
        # Only charge the cd-then-git caveat when git was free by built-in
        # (sentinel "") rather than covered by an explicit allow rule.
        if (not hits and "" in verdicts
                and CD_THEN_GIT.search(
                    HEREDOC.sub(lambda m: " " + m.group(2), cmd))):
            hits = {"cd + git (dir change, no git allow rule)"}
        if hits:
            prompting += 1
            for r in hits:
                reasons[r] += 1
                examples.setdefault(r, " ".join(cmd.split())[:96])

    # Non-Bash tools. Skipping these is what let a config score 0% while every
    # call to a new MCP server prompted -- so they are counted into the SAME
    # total, not reported alongside it. A separate number would have been read
    # as a footnote to a green headline.
    tool_prompting = 0
    tool_total = 0
    unmodelled: Counter = Counter()
    # `by_design` is shared with the Bash loop above -- do NOT re-initialise it
    # here. An `ask` rule prompts; that is its entire purpose. It is tracked
    # apart from the gate number because it is a prompt the user CHOSE, not
    # friction the config failed to remove. Folding it into either bucket is
    # wrong: into the gate number and every deliberate carve-out looks like a
    # regression; silently into "no prompt" and a config with 30 ask rules on
    # hot tools reports a serene 0% while interrupting constantly.
    if not args.bash_only:
        non_bash = [n for _, n, _ in events if n != "Bash"]
        recent_tools = non_bash[-args.limit:]
        for name in recent_tools:
            if name in rules_lib.ALWAYS_FREE_TOOLS:
                continue
            v = rules_lib.verdict(name, perm_rules)
            if v == "deny":
                denied[name] += 1
            if v == "ask":
                by_design[name] += 1
                continue
            if v == "prompt" and not rules_lib.is_modelled(name):
                # A built-in with no rule. Exemption is unverified, so this is
                # reported but NOT charged to the gate number.
                unmodelled[name] += 1
                continue
            tool_total += 1
            if v == "prompt":
                tool_prompting += 1
                # `is_modelled` used to mean "is MCP", so this line could
                # assume parse_mcp returned a ref. Widening it to cover known
                # built-ins made that assumption a crash on the first
                # uncovered Edit -- the caller a contract change silently
                # invalidates.
                ref = rules_lib.parse_mcp(name)
                label = f"mcp: {ref.server}" if ref else f"tool: {name}"
                reasons[label] += 1
                examples.setdefault(label, name)

    total = bash_total + tool_total
    prompting += tool_prompting
    pct = prompting * 100.0 / max(total, 1)
    print(f"Settings:  {args.settings or 'merged scopes'}"
          f"{'' if args.settings else ' (user + project + local)'}")
    print(f"Bash rules: {len(patterns)}"
          f"{'  (blanket Bash(*) grant)' if '*' in patterns else ''}")
    print(f"Replayed:  {bash_total} Bash calls"
          f"{'' if args.bash_only else f' + {tool_total} MCP/ruled tool calls'}\n")
    print(f"Would prompt: {prompting}/{total}  ({pct:.0f}%)")
    if not args.bash_only and tool_total:
        print(f"  of which non-Bash: {tool_prompting}/{tool_total}")

    if by_design:
        print(f"\nPROMPTS BY DESIGN — {sum(by_design.values())} calls matched an "
              f"`ask` rule.")
        print("  These DO interrupt you. They are excluded from the number above")
        print("  because you chose them, but a carve-out on a tool you use hourly")
        print("  is still friction — check the counts before keeping it:")
        for name, n in by_design.most_common(12):
            print(f"    {n:5d}  {name}")

    if denied:
        print(f"\nDENIED — {sum(denied.values())} calls matched a `deny` rule.")
        print("  These are blocked, not prompted: counted in the total above as")
        print("  not prompting. Check none of them is work you meant to allow:")
        for name, n in denied.most_common(12):
            print(f"    {n:5d}  {name}")

    if unmodelled:
        print(f"\nNOT MODELLED — {sum(unmodelled.values())} calls to built-in "
              f"tools with no explicit rule.")
        print("  Claude Code exempts many built-ins from prompting, but it")
        print("  declares that internally and the declaration could not be read")
        print("  out of the binary. These are EXCLUDED from the number above")
        print("  rather than guessed at in either direction:")
        for name, n in unmodelled.most_common(10):
            print(f"    {n:5d}  {name}")

    if reasons:
        print("\nWhy:")
        for reason, n in reasons.most_common(20):
            print(f"  {n:5d}  {reason}")
            if args.verbose:
                print(f"         e.g. {examples[reason]}")

    if pct > args.threshold:
        print(f"\nAbove the {args.threshold:.0f}% threshold. An enumerated allowlist")
        print("that leaves this much prompting will not feel autonomous -- check")
        print("whether the residue is path-invoked or exec-wrapper shaped, which")
        print("no prefix rule can fix, and prefer Bash(*) plus a PreToolUse hook.")
        return 1
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
