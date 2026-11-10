#!/usr/bin/env python3
"""corpus_diff.py -- compare two guard builds against a real transcript
corpus (claude-danger-guard by default, or claude-worktree-guard when
`--tools` names the Edit family).

Walks one or more directories of Claude Code transcripts (`*.jsonl`,
recursively -- subagent transcripts live in their own nested files and are
walked the same as top-level ones), collects every tool_use payload a real
session issued for a tool named in `--tools` (Bash by default; the Edit
family -- `Edit Write MultiEdit NotebookEdit` -- is opt-in), and feeds each
one to a reference guard and a candidate guard under test. It reports where
they disagree, where the candidate crashes, and any exit code neither guard
is supposed to produce -- so a port can be checked against real usage rather
than only against hand-written fixtures.

No network I/O. It reads the transcript directories given on the command
line and writes only to stdout; it never writes a file itself.
"""

import argparse
import json
import os
import subprocess
import sys

CRASH_MARKER = "claude-danger-guard crashed"

DEFAULT_TOOLS = ("Bash",)

# An escape hatch leaking from corpus_diff's own invocation into the guard
# subprocesses it spawns would let a stub (or a real guard reading these
# vars) answer differently than it would standing alone -- stripped
# unconditionally so a disagreement reflects the guards' own logic, never
# the corpus_diff invocation's environment.
STRIPPED_ENV_VARS = ("DANGER_ALLOW", "WORKTREE_ALLOW", "WT_ALLOW_MAIN",
                     "WORKTREE_TEST_BUDGET", "WORKTREE_TEST_HARNESS")


def _iter_jsonl_files(dirs):
    """Yield every *.jsonl file under each directory, recursively and sorted
    for a deterministic run order. Subagent transcripts sit in nested
    subdirectories (`subagents/agent-<id>.jsonl`) and are walked the same as
    a top-level session file.
    """
    for root_dir in dirs:
        for dirpath, _dirnames, filenames in os.walk(root_dir):
            for name in sorted(filenames):
                if name.endswith(".jsonl"):
                    yield os.path.join(dirpath, name)


def _relevant_fields(name, tool_input):
    """The subset of `tool_input` that identifies this payload for dedup
    purposes: the command text for Bash, or whichever of `file_path` /
    `notebook_path` is set for the Edit family. Unrelated fields a real
    transcript's `input` may also carry never affect dedup.
    """
    if name in ("Bash", "Monitor"):
        return {"command": tool_input.get("command")}
    fields = {}
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path:
        fields["file_path"] = file_path
    notebook_path = tool_input.get("notebook_path")
    if isinstance(notebook_path, str) and notebook_path:
        fields["notebook_path"] = notebook_path
    return fields


def _tool_payloads_in_line(line, tools):
    """Parse one transcript line and yield (name, tool_input, cwd) for every
    tool_use block whose name is in `tools`.

    Never raises. A line that is not valid JSON, not a JSON object, missing
    a top-level `cwd`, or whose `message.content` is absent or a plain
    string (rather than the list of blocks a tool-calling turn carries) all
    yield nothing -- they are prose turns or the wrong shape, not an error.
    A Bash block needs a non-empty string `command`; an Edit-family block
    needs a non-empty string `file_path` or `notebook_path`.
    """
    try:
        record = json.loads(line)
    except ValueError:
        return
    if not isinstance(record, dict):
        return
    cwd = record.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return
    message = record.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        name = block.get("name")
        if name not in tools:
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        if name in ("Bash", "Monitor"):
            command = tool_input.get("command")
            if isinstance(command, str) and command:
                yield name, {"command": command}, cwd
        elif _relevant_fields(name, tool_input):
            yield name, tool_input, cwd


def collect_commands(dirs, tools=DEFAULT_TOOLS, limit=None):
    """Every (name, tool_input, cwd) triple from the transcripts under
    `dirs`, deduped on (name, the JSON of the relevant input fields, cwd)
    and kept in first-seen order.

    `limit`, when given, caps the deduped list at N entries -- it exists to
    bound a run against a very large corpus, not to change what counts as a
    duplicate, so it is applied after dedup rather than by reading fewer
    lines.
    """
    seen = set()
    commands = []
    for path in _iter_jsonl_files(dirs):
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                for name, tool_input, cwd in _tool_payloads_in_line(line, tools):
                    key = (name, json.dumps(_relevant_fields(name, tool_input),
                                            sort_keys=True), cwd)
                    if key in seen:
                        continue
                    seen.add(key)
                    commands.append((name, tool_input, cwd))
                    if limit is not None and len(commands) >= limit:
                        return commands
    return commands


def _guard_argv(guard):
    """`[sys.executable, guard]` for a Python guard, detected from its own
    shebang -- the interpreter can run a `.py` file directly without an
    execute bit -- else `[guard]` for a script the OS execs itself.
    """
    with open(guard, "r", encoding="utf-8", errors="replace") as handle:
        first_line = handle.readline()
    if "python" in first_line:
        return [sys.executable, guard]
    return [guard]


def run_guard(argv, name, tool_input, cwd, env, crash_marker):
    """Feed one PreToolUse payload to a guard; return (exit_code, crashed),
    where `crashed` is whether stdout carried `crash_marker`.
    """
    payload = json.dumps({"tool_name": name, "tool_input": tool_input,
                          "cwd": cwd})
    done = subprocess.run(argv, input=payload, capture_output=True,
                          text=True, encoding="utf-8", env=env)
    crashed = crash_marker in done.stdout
    return done.returncode, crashed


def _truncate(text):
    """The text's first 300 characters, with newlines shown as a visible
    glyph so a multi-line value still reads on one report line.
    """
    return text[:300].replace("\n", "⏎")


def _describe_payload(name, tool_input):
    """A short, human-readable stand-in for a judged payload: the Bash
    command text itself, or `<ToolName> <path>` for the Edit family.
    """
    if name in ("Bash", "Monitor"):
        return tool_input.get("command", "")
    path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
    return "%s %s" % (name, path)


def _format_entry(ref_code, cand_code, cwd, name, tool_input):
    return "  reference=%s candidate=%s cwd=%s cmd=%s" % (
        ref_code, cand_code, cwd, _truncate(_describe_payload(name, tool_input)))


def diff(reference, candidate, dirs, tools=DEFAULT_TOOLS, limit=None,
        crash_marker=CRASH_MARKER, env=None):
    """Judge every payload under `dirs` with both guards.

    Returns (report_lines, exit_code). Exit code is 1 if there is any
    disagreement, any candidate crash, any exit code outside {0, 2} from
    either guard, or zero commands judged; else 0.
    """
    if env is None:
        env = os.environ
    env = dict(env)
    for var in STRIPPED_ENV_VARS:
        env.pop(var, None)

    ref_argv = _guard_argv(reference)
    cand_argv = _guard_argv(candidate)

    commands = collect_commands(dirs, tools=tools, limit=limit)

    disagreements = []
    crashes = []
    odd_exit_codes = []

    for name, tool_input, cwd in commands:
        ref_code, _ref_crashed = run_guard(ref_argv, name, tool_input, cwd,
                                           env, crash_marker)
        cand_code, cand_crashed = run_guard(cand_argv, name, tool_input, cwd,
                                            env, crash_marker)

        entry = (ref_code, cand_code, cwd, name, tool_input)
        if ref_code != cand_code:
            disagreements.append(entry)
        if cand_crashed:
            crashes.append(entry)
        if ref_code not in (0, 2) or cand_code not in (0, 2):
            odd_exit_codes.append(entry)

    lines = ["commands judged: %d" % len(commands)]

    lines.append("")
    lines.append("disagreements: %d" % len(disagreements))
    for entry in disagreements:
        lines.append(_format_entry(*entry))

    lines.append("")
    lines.append("candidate crashes: %d" % len(crashes))
    for entry in crashes:
        lines.append(_format_entry(*entry))

    lines.append("")
    lines.append("exit codes outside {0, 2}: %d" % len(odd_exit_codes))
    for entry in odd_exit_codes:
        lines.append(_format_entry(*entry))

    # Each disqualifying condition sets its own flag rather than being
    # folded into one boolean expression, so a mutation dropping any single
    # check is a one-line change with an obvious target (see the plan's
    # AC 3).
    ok = True
    if disagreements:
        ok = False
    if crashes:
        ok = False
    if odd_exit_codes:
        ok = False
    if not commands:
        ok = False

    return lines, (0 if ok else 1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", required=True,
                        help="path to the reference guard")
    parser.add_argument("--candidate", required=True,
                        help="path to the candidate guard")
    parser.add_argument("--tools", default=",".join(DEFAULT_TOOLS),
                        help="comma-separated tool_use names to collect "
                             "(Bash, Edit, Write, MultiEdit, NotebookEdit); "
                             "default Bash")
    parser.add_argument("--crash-marker", default=CRASH_MARKER,
                        help="text in stdout marking a candidate crash")
    parser.add_argument("--limit", type=int, default=None,
                        help="cap the number of deduped commands judged")
    parser.add_argument("dirs", nargs="+",
                        help="transcript directories to walk for *.jsonl")
    args = parser.parse_args(argv)

    tools = tuple(t.strip() for t in args.tools.split(",") if t.strip())

    lines, code = diff(args.reference, args.candidate, args.dirs,
                       tools=tools, limit=args.limit,
                       crash_marker=args.crash_marker)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
