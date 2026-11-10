#!/usr/bin/env python3
"""Guarded install for a distilled memory file.

memory/ has no version control and at most a periodic claude-backup copy (days
old at best), and several sessions can write to it concurrently, so a
whole-file rewrite can silently eat an append that landed between the read and
the write.

  stamp <abs-path>                      print a token describing the target
                                         RIGHT NOW, before you read it
  install <abs-path> <newfile> <token>  abort unless the target still matches
                                         <token>; otherwise snapshot the
                                         CURRENT content into history/ and
                                         move <newfile> into place

**The token is handed back by the caller, never looked up.** An earlier cut
kept stamps in a shared `~/.claude/memory-distill-stamps.json` keyed by target
path, which answered the wrong question: not "has this changed since *I* read
it" but "has it changed since the *last stamp by anybody*". Any concurrent
session stamping the same file silently re-armed every other session's pending
install, and the guard then reported `ok` while overwriting exactly the peer
write it exists to catch. The shared file also needed locking and an atomic
write it did not have. Passing the token through the caller removes all three
problems by removing the shared state: there is nothing for a peer to touch.

The check and the install happen in one process, so the race window is the
few milliseconds between them rather than the minutes a session takes to read
and rewrite a file.

The target is always an ABSOLUTE PATH ARGUMENT, resolved from exactly what the
caller passes -- never a bare name reconstructed against a hardcoded root.
A machine holds many different `memory/` directories (one per
project); a name-keyed lookup can stamp one project's file and silently write
over another's. `history/` is always the target's own sibling directory,
derived from the path actually given -- see `history_path_for`.

If the target does not exist, this refuses and says so rather than falling
back to any guessed location. Nor does it install where the archive would be a
file Claude Code loads -- see `loaded_archive_refusal`.

`install` also serialises cooperating installers per target: a lock held
across the token check, the snapshot, and the replace means a second install
with the same token waits, then fails the token check instead of overwriting
the first and losing its version. The replace itself is a temp file plus
`os.replace`, so an interrupted copy never leaves a half-written target. An
arbitrary external writer takes no lock; it is caught by the token, checked
once before the snapshot and again just before the rename.
"""
import datetime
import hashlib
import importlib.util
import os
import re
import shutil
import sys
import unicodedata

# How long an install waits for another install of the same target.
INSTALL_LOCK_TIMEOUT = 30.0

# The header written ONLY the first time a history file is created. Every
# later distillation appends a dated section below it and never rewrites
# this text -- see snapshot_history().
#
# Keyed on the target's FILENAME, not `os.path.splitext`'s stem --
# `splitext("CLAUDE.md.template")` gives stem "CLAUDE.md", which titled the
# template's own archive "# CLAUDE.md" (indistinguishable from CLAUDE.md's
# own archive title) and pointed its back-reference at the nonexistent
# `CLAUDE.md.md`. The header is write-once, so that was a permanent defect,
# not a cosmetic one. Using the filename means the back-reference already
# carries its own extension -- do not append ".md" to `{name}` below.
MEMORY_HEADER = """# {name} — incident history

Verbatim record of the incidents behind `../{name}`. **Deliberately not
loaded.** Append-only: nothing below has ever been overwritten.
"""

RATIONALE_HEADER = """# {name} — rationale history

Verbatim record of the reasoning displaced out of `{name}`. **Deliberately
not loaded** -- it exists so a cut is reversible and a challenged rule can be
traced back to why it exists. Append-only: nothing below has ever been
overwritten.
"""


def header_for(target):
    """The header for a target's archive, written ONCE on first creation.

    Keyed the same way as history_path_for: a memory archive sits one level
    down and records incidents; a sibling archive sits alongside and records
    displaced rationale. Getting the back-reference wrong is permanent -- the
    header is never rewritten (see snapshot_history).
    """
    directory, filename = os.path.split(target)
    template = (MEMORY_HEADER if _targets_memory_dir(directory)
                else RATIONALE_HEADER)
    return template.format(name=filename)

# A YAML frontmatter line: a `key:` at any indent, a `- ` list item, or an
# indented continuation. Anything else -- a markdown heading, a sentence, a
# blockquote -- means the block we found is NOT frontmatter. See
# strip_frontmatter for why that distinction is load-bearing.
_YAML_LINE = re.compile(r"""
    ^[ \t]* (?:
        [A-Za-z_][A-Za-z0-9_.\-]* [ \t]* :      # key: or key:value
      | -[ \t]                                  # - list item
    )
    """, re.VERBOSE)


def fingerprint(path):
    """An opaque token for the target's current state.

    `st_mtime_ns` rather than `st_mtime`: an integer round-trips through the
    command line exactly, where a float's repr does not.
    """
    st = os.stat(path)
    return "%d:%d" % (st.st_mtime_ns, st.st_size)


def token_size(token):
    """The size a token records, or None if it is not one of ours."""
    try:
        return int(token.split(":")[1])
    except (AttributeError, IndexError, ValueError):
        return None


def _targets_memory_dir(directory):
    """True when a target's immediate parent is a `memory/` directory.

    The one place this question is asked. `history_path_for` and `header_for`
    both branch on it, and two copies would drift the moment the memory
    layout changes -- this repo forbids a second copy of a live predicate.
    Deliberately the IMMEDIATE parent: a file merely named `memory.md`, and a
    file nested deeper under `memory/`, are both NOT memory targets.
    """
    return os.path.basename(directory) == "memory"


def history_path_for(target):
    """The archive file for a target, derived from the path GIVEN -- never
    reconstructed from a bare name against some other root.

    Two shapes, keyed on what the target IS rather than on a maintained list
    of paths (guard-exclusions-key-on-behaviour):

    - Under a `memory/` directory -> `memory/history/<stem>.history.md`.
      memory/ holds many files, so they get their own subdirectory.
    - Anything else -> `<stem>.history.md`, a direct sibling. The CLAUDE.md
      family is one file per directory, and a `history/` folder in a workspace
      root and a shared repo root is clutter in two places people read often.
    """
    directory, filename = os.path.split(target)
    stem, _ext = os.path.splitext(filename)
    if _targets_memory_dir(directory):
        return os.path.join(directory, "history", stem + ".history.md")
    return os.path.join(directory, stem + ".history.md")


def loaded_archive_refusal(hist_path):
    """A refusal message when `hist_path` is a file Claude Code would LOAD, else
    None.

    The archive is a sibling of its target, so a target in `.claude/rules/`
    gets an archive in `.claude/rules/` -- loaded like every rules file, with
    its frontmatter stripped so it carries no `paths:` and loads in EVERY
    session -- and a topic file gets one in `memory/topics/`, where it reads as
    a topic. Neither says anything: the context cost just appears. So the
    question is asked of the ARCHIVE, the file that causes the cost, which also
    covers a non-`.md` target in the rules tree (`CLAUDE.md.template`).

    The predicates are `growth_lib`'s, never a copy -- the same ones
    `claude-distill-write-guard` uses to withhold this route. If `growth_lib`
    cannot be imported the answer is a refusal: this is a hand-run writer, so
    failing closed costs one loud error, and guessing costs a loaded file.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    for path in (os.path.join(root, "lib"),
                 os.path.join(root, "tools", "context-growth")):
        if path not in sys.path:
            sys.path.insert(0, path)
    try:
        import growth_lib
    except Exception as exc:
        return ("ABORT: cannot check whether %s would be loaded (growth_lib "
                "import failed: %s) -- nothing installed" % (hist_path, exc))
    if growth_lib.is_rules_file(hist_path) or growth_lib.is_topic_file(hist_path):
        return ("ABORT: the archive would be %s, which Claude Code loads as a "
                "rules or topic file -- nothing installed. Rules and topic "
                "files have no archive route; change them with a targeted "
                "Edit." % hist_path)
    return None


def strip_frontmatter(text):
    """Drop a leading '---' YAML frontmatter block, if there is one.

    History holds the incident body, not the fact file's own frontmatter
    (name/type/metadata).

    **This is a data-loss surface, so it strips only what it can positively
    recognise as frontmatter.** The obvious implementation -- find the next
    `\\n---\\n` and cut -- silently ate real content in two shapes, both of
    which occur in hand-written prose:

      - a file with NO frontmatter whose first line is a `---` horizontal
        rule: the cut then lands on the *next* rule further down and
        everything between them is gone;
      - a closing fence carrying trailing whitespace, so the exact-match
        search skips it and finds a later `---` in the body instead.

    What comes back from here is what gets archived into `history/`, and the
    fact file is overwritten in the same call -- on a directory with no version
    control and at most a days-old claude-backup copy. So the block must both
    close on a real fence line and contain nothing but YAML-shaped lines.
    Anything else is passed through WHOLE: archiving a few lines of frontmatter is harmless, archiving a
    truncated body is not recoverable.
    """
    if not text.startswith("---\n"):
        return text

    lines = text.split("\n")
    for index in range(1, len(lines)):
        line = lines[index]
        if line.rstrip() == "---":                       # candidate closing fence
            block = lines[1:index]
            if all(not ln.strip() or _YAML_LINE.match(ln) for ln in block):
                return "\n".join(lines[index + 1:])
            return text                                  # fenced, but not YAML
        if line.strip() and not _YAML_LINE.match(line):
            return text                                  # prose before any fence
    return text                                          # never closed


def cmd_stamp(target):
    target = os.path.realpath(target)
    if not os.path.isfile(target):
        sys.exit("ABORT: %s is not an existing file -- refusing to stamp a "
                  "guessed path" % target)
    token = fingerprint(target)
    print("stamped %s (size=%d)" % (target, token_size(token)), file=sys.stderr)
    print(token)


def snapshot_history(target, hist_path):
    """Append the target's CURRENT content to history/ as a new dated section.

    G1 -- never truncate an existing history file. The append is the ONLY
    write path that touches one that already exists: a first snapshot
    creates the file (mode "w", nothing to clobber yet); every later one
    opens the existing file in append mode and nothing else. If growing the
    file is somehow impossible, abort rather than risk the archive --
    losing this distillation is recoverable, losing the archive is not.

    **Every caller must validate its inputs BEFORE calling this.** The append
    is a side effect on the one file that cannot be reconstructed, so a
    failure *after* it leaves a snapshot section behind for an install that
    never happened -- and a retry appends a second copy, under a header that
    claims each section records a real distillation. cmd_install checks the
    staged file up front for exactly this reason.
    """
    original = open(target, encoding="utf-8").read()
    body = strip_frontmatter(original).rstrip("\n")
    stamp = datetime.date.today().isoformat()
    section = "\n## Snapshot -- %s\n\n---\n%s\n" % (stamp, body)

    try:
        if os.path.exists(hist_path):
            before = os.path.getsize(hist_path)
            with open(hist_path, "a", encoding="utf-8") as handle:
                handle.write(section)
            if os.path.getsize(hist_path) <= before:
                sys.exit("ABORT: appending to %s did not grow the file -- "
                          "nothing installed, the archive is unchanged" %
                          hist_path)
        else:
            os.makedirs(os.path.dirname(hist_path), exist_ok=True)
            with open(hist_path, "w", encoding="utf-8") as handle:
                handle.write(header_for(target) + section)
    except OSError as exc:
        sys.exit("ABORT: could not write history for %s (%s) -- nothing "
                  "installed" % (target, exc))


def _lock_path(target):
    """The install lock for a target, keyed on its directory and folded name.

    Not the target's resolved path: on a case- or normalisation-insensitive
    filesystem (macOS default) `realpath` does not canonicalise case, so
    `example.md` and `EXAMPLE.md` would hash to two locks for one file. The
    key is the parent directory's identity (device and inode -- a directory
    is not replaced by an install) plus the NFC-normalised, casefolded
    filename. The target's own inode is deliberately not used: os.replace
    changes it. On a case-sensitive filesystem two files differing only in
    case now share a lock, which is harmless over-serialisation.

    A lock holds no state, so the "no shared state" rule above (about stamps)
    does not apply. It lives under ~/.claude rather than $TMPDIR because
    sessions can have different temp dirs, which would not serialise them;
    never beside the target, which sits in a repo or memory dir people read.
    """
    st = os.stat(os.path.dirname(target))
    name = unicodedata.normalize("NFC", os.path.basename(target)).casefold()
    material = "%d:%d:%s" % (st.st_dev, st.st_ino, name)
    key = hashlib.sha256(material.encode("utf-8", "surrogateescape")).hexdigest()
    return os.path.join(os.path.expanduser("~/.claude"), "distill-guard",
                        "locks", key[:16] + ".lock")


def _load_portable():
    """lib/portable.py loaded by file path, so no sys.path is touched.

    Called from cmd_install only: `stamp` and the Write-hook import path must
    never depend on it.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    path = os.path.join(here, "..", "..", "lib", "portable.py")
    spec = importlib.util.spec_from_file_location("portable", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_install(target, newfile, token):
    target = os.path.realpath(target)
    if not os.path.isfile(target):
        sys.exit("ABORT: %s is not an existing file -- refusing to install "
                  "against a guessed path" % target)
    hist_path = history_path_for(target)
    # Before the history append, not after: see snapshot_history's docstring.
    refusal = loaded_archive_refusal(hist_path)
    if refusal:
        sys.exit(refusal)
    if not os.path.isfile(newfile):
        sys.exit("ABORT: %s is not an existing file -- nothing installed, "
                  "the target and the archive are unchanged" % newfile)
    # The success path removes newfile; if it were the target, that would
    # delete the freshly installed file.
    if os.path.samefile(newfile, target):
        sys.exit("ABORT: the staged file IS the target -- nothing installed")
    then = token_size(token)
    if then is None:
        sys.exit("ABORT: %r is not a token from `stamp` -- stamp the target "
                  "before reading it and pass that token back here" % token)

    try:
        portable = _load_portable()
    except Exception as exc:
        sys.exit("ABORT: could not load the install lock (%s) -- nothing "
                  "installed, the target and the archive are unchanged" % exc)
    lock_path = _lock_path(target)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with portable.exclusive_lock(lock_path,
                                 timeout=INSTALL_LOCK_TIMEOUT) as acquired:
        if not acquired:
            sys.exit("ABORT: another distill install of %s is still running "
                      "-- nothing installed; retry when it finishes" % target)
        now = fingerprint(target)
        if now != token:
            what = []
            if token_size(now) != then:
                what.append("size %d -> %d" % (then, token_size(now)))
            else:
                what.append("mtime moved (content may be identical)")
            sys.exit(
                "ABORT: %s changed since it was read -- %s. A peer wrote to it. "
                "Nothing installed; re-read and redo this one." %
                (target, "; ".join(what)))

        snapshot_history(target, hist_path)

        # Copy beside the target, then rename over it: the target is either
        # the old file or the whole new one, never a partial copy.
        tmp = target + ".distill-tmp-%d" % os.getpid()
        try:
            shutil.copyfile(newfile, tmp)
            shutil.copymode(newfile, tmp)
            # Re-check right before the rename: a writer that takes no lock (an
            # editor, a script) may have changed the target during the snapshot
            # or the copy, and renaming over it would lose that edit unarchived.
            # What remains is the instant between this check and the rename,
            # which only a cooperating writer can close.
            if fingerprint(target) != token:
                os.remove(tmp)
                sys.exit("ABORT: %s changed during the install -- nothing "
                          "installed, but a snapshot section was already "
                          "appended to %s. Delete that section, re-read and "
                          "redo this one." % (target, hist_path))
            os.replace(tmp, target)
        except (OSError, shutil.Error) as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            sys.exit("ABORT: could not move %s into place (%s) -- the target is "
                      "unchanged, but a snapshot section was already appended to "
                      "%s. Delete that section before retrying." %
                      (newfile, exc, hist_path))
    # Best effort: the install already succeeded.
    try:
        os.remove(newfile)
    except OSError:
        pass
    new_size = os.path.getsize(target)
    print("ok %s: %d -> %d B, history %d B" %
          (target, then, new_size, os.path.getsize(hist_path)))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) < 2:
        sys.exit(__doc__)
    command, target = argv[0], argv[1]
    if command == "stamp":
        cmd_stamp(target)
    elif command == "install":
        if len(argv) < 4:
            sys.exit(__doc__)
        cmd_install(target, argv[2], argv[3])
    else:
        sys.exit("unknown command %r" % command)


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors=_stream.errors)
        except (AttributeError, ValueError):
            pass
    main()
