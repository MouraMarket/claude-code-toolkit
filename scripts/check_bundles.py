#!/usr/bin/env python3
"""Check SKILL.md files for the mistakes that fail silently.

Each rule below fails without any error, so it is enforced here:

1. **A SKILL.md over 500 lines.** A long file is skimmed rather than
   truncated, and a step that was skimmed past is never performed. Nothing
   reports it.
2. **A description that cannot be read.** A skill with no readable
   `description` front-matter field never triggers: it is installed and
   silently unused.
3. **A description over 1,024 characters.** That is the Agent Skills
   specification's limit for the field; an over-long one is rejected or cut,
   so the skill stops routing as written.

Usage:
    check_bundles.py                  # every skill under skills/ and plugins/
    check_bundles.py PATH [PATH ...]  # a SKILL.md, a skill directory, or a
                                      # directory searched for skills

Skills are found by walking the disk, so new and untracked skills are
checked. A SKILL.md below another skill's directory is content (an example),
not a skill. Exit 0 when clean, 1 on any finding, 2 when a path does not exist
or no skill was found (a check that checked nothing must not pass).
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional, Sequence

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# The Agent Skills specification's limit for a skill's `description`.
DESCRIPTION_LIMIT = 1024

# Anthropic's documented bound: keep a SKILL.md under 500 lines. It FAILS
# rather than warns, because a long file's usual failure is being skimmed, not
# truncated. No exception table: the rule is to trim (move rare content to
# references/, compress), never to pin an over-cap file.
LINE_LIMIT = 500


def read_description(text: str) -> Optional[str]:
    """PURE. The `description` value from a SKILL.md, or None if absent.

    Hand-rolled rather than PyYAML on purpose: these suites are stdlib-only so
    they run on a bare interpreter, and adding a dependency to read one field would
    cost more than it buys. Handles the two shapes this repo actually uses —
    a folded/literal block scalar (`>-`, `>`, `|`, `|-`) and a plain or quoted
    inline scalar — and returns None for anything else rather than guessing,
    because a wrong length here either blocks a good PR or waves through a
    broken one.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    try:
        end = next(i for i, line in enumerate(lines[1:], 1)
                   if line.strip() in ("---", "..."))
    except StopIteration:
        return None

    for index in range(1, end):
        stripped = lines[index]
        if not stripped.startswith("description:"):
            continue
        value = stripped[len("description:"):].strip()

        if value[:1] in (">", "|"):
            # Block scalar. Collect the indented body, then fold per YAML:
            # a folded (`>`) scalar joins lines with a space and turns a blank
            # line into a newline; a literal (`|`) scalar keeps them as-is.
            folded = value.startswith(">")
            # Chomping indicator, and it is worth a full character: `-` strips
            # the trailing newline, a bare `>`/`|` CLIPS to exactly one, `+`
            # keeps every one. Getting this wrong costs one character in the
            # permissive direction, which is precisely the direction that lets a
            # 1,025-character description pass a 1,024 cap. Bare `>` is
            # common in real files and exposed the bug.
            chomp = "clip"
            if "-" in value[:3]:
                chomp = "strip"
            elif "+" in value[:3]:
                chomp = "keep"

            raw = []
            for line in lines[index + 1:end]:
                if line.strip() and not line.startswith((" ", "\t")):
                    break
                raw.append(line)
            # YAML keeps a content line's indentation beyond the block's own
            # (set by its first content line) and its trailing whitespace, and
            # stripping them would UNDER-count: the permissive direction. Such a
            # block cannot be measured exactly here, so it is unreadable.
            content = [line for line in raw if line.strip()]
            if content:
                indent = len(content[0]) - len(content[0].lstrip())
                for line in content:
                    if (len(line) - len(line.lstrip()) != indent
                            or line != line.rstrip()):
                        return None
            body = [line.strip() for line in raw]

            trailing = 0
            while body and not body[-1]:
                body.pop()
                trailing += 1

            out = "\n".join(body) if not folded else ""
            if folded:
                for chunk in body:
                    if not chunk:
                        out += "\n"
                    elif not out or out.endswith("\n"):
                        out += chunk
                    else:
                        out += " " + chunk
            if chomp == "clip" and body:
                out += "\n"
            elif chomp == "keep" and body:
                # +1 for the last content line's own terminator, which `trailing`
                # never counted because it only saw the blank lines after it.
                out += "\n" * (trailing + 1)
            return out

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            return value[1:-1]
        return value
    return None


def scan_description(path: str, text: str) -> List[str]:
    """PURE. Reasons this SKILL.md's description is over the limit."""
    if not path.replace("\\", "/").endswith("SKILL.md"):
        return []
    description = read_description(text)
    if description is None:
        return []
    if len(description) <= DESCRIPTION_LIMIT:
        return []
    return ["has a description of {0} characters, over the {1}-character "
            "limit of the Agent Skills specification. Cut {2} "
            "character(s)".format(
                len(description), DESCRIPTION_LIMIT,
                len(description) - DESCRIPTION_LIMIT)]


def scan_size(path: str, text: str) -> List[str]:
    """PURE. Failures for a SKILL.md's line count; empty for anything else.

    Lines are counted as `splitlines()` sees them, so a missing final newline
    does not buy a line.
    """
    if os.path.basename(path.replace("\\", "/")) != "SKILL.md":
        return []
    lines = len(text.splitlines())
    if lines <= LINE_LIMIT:
        return []
    return ["is {0} lines, over the {1}-line SKILL.md limit. Trim {2}+ "
            "line(s): move rare material to references/ and compress. Never "
            "cut a rule a review or an incident put there".format(
                lines, LINE_LIMIT, lines - LINE_LIMIT)]


def find_skills(paths: Sequence[str]) -> List[str]:
    """Sorted SKILL.md paths under `paths`.

    A file named SKILL.md is itself. A directory is walked; a directory that
    holds a SKILL.md is a skill, and nothing below it is searched (a SKILL.md
    there is content). Raises ValueError for a path that does not exist.
    """
    found = set()
    for path in paths:
        if os.path.isfile(path):
            if os.path.basename(path) == "SKILL.md":
                found.add(os.path.normpath(path))
        elif os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames.sort()
                if "SKILL.md" in filenames:
                    found.add(os.path.normpath(os.path.join(dirpath, "SKILL.md")))
                    dirnames[:] = []
        else:
            raise ValueError("path does not exist: {0}".format(path))
    return sorted(found)


def check_skill(path: str, text: str) -> List[str]:
    """PURE. Every failure for one SKILL.md."""
    failures = scan_size(path, text)
    if read_description(text) is None:
        failures.append("has no readable description: a skill with no "
                        "description never triggers (a description block "
                        "with uneven indentation or trailing spaces cannot "
                        "be measured here; even it out)")
    failures.extend(scan_description(path, text))
    return failures


def _display(path: str) -> str:
    """The path relative to the cwd when it sits inside it."""
    relative = os.path.relpath(path)
    return path if relative.startswith("..") else relative


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        paths = args
    else:
        paths = [p for p in (os.path.join(REPO_ROOT, "skills"),
                             os.path.join(REPO_ROOT, "plugins"))
                 if os.path.isdir(p)]
    try:
        skills = find_skills(paths)
    except ValueError as error:
        print(error)
        return 2
    if not skills:
        print("no SKILL.md found under {0}: nothing was checked".format(
            ", ".join(_display(p) for p in paths) or "(no paths)"))
        return 2

    print("Checked {0} skill(s).".format(len(skills)))
    problems = 0
    for skill in skills:
        with open(skill, encoding="utf-8") as handle:
            text = handle.read()
        for failure in check_skill(skill, text):
            problems += 1
            print("FAIL {0}: {1}".format(_display(skill), failure))
    if problems:
        print("{0} problem(s).".format(problems))
        return 1
    print("OK: every skill is within its limits.")
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
