"""claude-index-distill: manifest validation and dependency set.

A manifest moves rule text out of MEMORY.md's `## Always` into a path-scoped
rules page or a tool-rules page, demoting the evidence leaf. This module owns
what `check` verifies and what `apply` writes; Part B adds `prove`.
"""

import hashlib
import json
import ntpath
import os
import datetime
import posixpath
import re
import shutil
import sys
import tempfile
from pathlib import Path

# realpath, not abspath: this is reached through a ~/.local/bin symlink.
_HERE = os.path.dirname(os.path.realpath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_ROOT, "tools", "tool-rules"),
           os.path.join(_ROOT, "tools", "context-growth"),
           os.path.join(_ROOT, "tools", "memory-lint"),
           os.path.join(_ROOT, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import auto_memory  # noqa: E402
import distill_claim  # noqa: E402
import growth_lib  # noqa: E402
import lint_lib  # noqa: E402
import tool_rules_lib  # noqa: E402
import toolkit_config  # noqa: E402

TOP_KEYS = {"version", "pages", "moves", "behaviour"}
PAGE_KEYS = {"file", "bucket", "content", "base_sha256", "probe"}
MOVE_KEYS = {"memory", "page", "topic", "row", "base_sha256"}
BEHAVIOUR_KEYS = {"memory", "files", "prompt", "pass_if"}
MAX_BEHAVIOUR = 4
MEMDIR_REL = "mem"
PATH_PREFIX = ".claude/rules/"
TOOL_PREFIX = ".claude/tool-rules/"
NO_BASH_PROBLEM = ("tool page has no Bash(...) pattern; a rule that only matters "
                   "on MCP calls stays in the index")
DEMOTED_MARK = "> **Rule text lives in"

_TOPIC_RE = re.compile(r"^topics/[^/]+\.md$")
_TYPE_LINE_RE = re.compile(r"^\s*type:\s*\S+")
_HEADING_RE = re.compile(r"^#{1,6}\s+(\S.*?)\s*$")


class RootsError(Exception):
    pass


class PathError(Exception):
    pass


class ManifestError(Exception):
    pass


def resolve_roots(home=None):
    """(memory_dir, workspace); RootsError naming whichever is missing."""
    memory_dir = auto_memory.configured_dir(home or os.path.expanduser("~"))
    workspace = toolkit_config.workspace_root()
    missing = []
    if not memory_dir:
        missing.append("memory dir (no autoMemoryDirectory in ~/.claude/settings.json)")
    if not workspace:
        missing.append("workspace (no workspace_root in the toolkit config)")
    if missing:
        raise RootsError("cannot resolve: " + "; ".join(missing))
    return memory_dir, workspace


def sha256_file(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except (FileNotFoundError, IsADirectoryError):
        return None


def _read_text(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _components(rel):
    return [c for c in re.split(r"[\\/]", rel) if c]


def resolve_inside(root, rel):
    """Absolute, symlink-resolved Path of `rel` under `root`, or PathError."""
    if not isinstance(rel, str) or not rel:
        raise PathError("path must be a non-empty string: %r" % (rel,))
    if os.path.isabs(rel) or rel.startswith(("/", "\\")):
        raise PathError("path must be relative: %s" % rel)
    if ".." in _components(rel):
        raise PathError("path must not contain '..': %s" % rel)
    real_root = os.path.realpath(root)
    # realpath resolves the existing prefix (symlinks included) and keeps the
    # not-yet-existing remainder as written.
    resolved = os.path.realpath(os.path.join(real_root, rel))
    if os.path.commonpath([real_root, resolved]) != real_root:
        raise PathError("path resolves outside %s: %s" % (real_root, rel))
    return Path(resolved)


def bad_relpath(rel):
    """True unless `rel` is a plain relative path (MR3): no absolute or
    drive-qualified form (POSIX or NT), no backslash, no ':' in the first
    component, no '..' component."""
    if not isinstance(rel, str) or not rel:
        return True
    if posixpath.isabs(rel) or ntpath.isabs(rel) or ntpath.splitdrive(rel)[0]:
        return True
    if "\\" in rel:
        return True
    first = [c for c in rel.split("/") if c][:1]
    if first and ":" in first[0]:
        return True
    return ".." in _components(rel)


def is_reserved(rel, memdir_rel):
    # "." components mean nothing: "./mem/MEMORY.md" is "mem/MEMORY.md".
    # casefold: a case-insensitive volume reaches the same file by another spelling.
    parts = [p.casefold() for p in _components(rel) if p != "."]
    if any(p in ("claude.md", "claude.local.md") for p in parts):
        return True
    # `.claude` is reserved at any depth; `_proof` and the memdir only at the root.
    if ".claude" in parts or (parts and parts[0] == "_proof"):
        return True
    norm = "/".join(parts)
    memdir_rel = memdir_rel.casefold()
    return norm == memdir_rel or norm.startswith(memdir_rel + "/")


_PROBE_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:=@%+,-]+")
_INTERPRETERS = ("python3", "bash", "sh")


def probe_script(command):
    """The relative script path a tool probe runs, or None (G1, G8).

    A probe is one literal invocation: every token matches a small whitelist
    and the command is its tokens joined by single spaces, so the containment
    hook can allow the whole string knowing it can only run the stub.
    """
    if not isinstance(command, str):
        return None
    tokens = command.split(" ")
    if any(t == "" or not _PROBE_TOKEN_RE.fullmatch(t) for t in tokens):
        return None
    if tokens[0] in _INTERPRETERS and len(tokens) >= 2:
        candidate = tokens[1]
    elif "/" in tokens[0]:
        candidate = tokens[0]
    else:
        return None
    parts = [c for c in candidate.split("/") if c not in ("", ".")]
    normalised = "/".join(parts)
    if (not normalised or candidate.startswith("-") or bad_relpath(candidate)
            or is_reserved(normalised, MEMDIR_REL)):
        return None
    return normalised


def parse_manifest(raw):
    """The manifest dict from its bytes, or ManifestError."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ManifestError("manifest is not valid JSON: %s" % exc)
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    return data


def load_manifest(path):
    with open(path, "rb") as fh:
        return parse_manifest(fh.read())


def _page_path(page, workspace):
    """Absolute Path of a page's file; PathError on any violation."""
    bucket, file = page.get("bucket"), page.get("file")
    if bucket == "path":
        root, prefix = Path(workspace) / ".claude" / "rules", PATH_PREFIX
    elif bucket == "tool":
        root, prefix = tool_rules_lib.tool_rules_dir(workspace), TOOL_PREFIX
    else:
        raise PathError("bad bucket %r" % (bucket,))
    if not isinstance(file, str) or not file.startswith(prefix):
        raise PathError("%s page file must start with %s: %r" % (bucket, prefix, file))
    rest = file[len(prefix):]
    # The hook reads only the directory's own *.md, never a subdirectory.
    if bucket == "tool" and ("/" in rest or "\\" in rest):
        raise PathError("tool page must sit directly in %s, not a subdirectory: %r"
                        % (prefix, file))
    return resolve_inside(root, rest)


def _always_lines(index_text):
    section, lines = None, []
    for line in index_text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            section = "always" if re.match(r"always\b", heading.group(1), re.I) else "other"
            continue
        if section == "always":
            lines.append(line)
    return lines


def _check_leaf(memory, text, problems):
    lines = text.split("\n")
    close = None
    if lines and lines[0].rstrip() == "---":
        for i in range(1, len(lines)):
            if lines[i].rstrip() == "---":
                close = i
                break
    if close is None or not any(_TYPE_LINE_RE.match(l) for l in lines[1:close]):
        problems.append("leaf %s has no editable frontmatter type" % memory)
    if any(l.startswith(DEMOTED_MARK) for l in lines):
        problems.append("leaf %s is already demoted" % memory)


def _check_probe_path(label, probe, problems):
    if not (isinstance(probe, dict) and set(probe) == {"path"}
            and isinstance(probe["path"], str) and probe["path"]):
        problems.append('%s probe must be exactly {"path": <str>}' % label)
        return
    rel = probe["path"]
    if bad_relpath(rel):
        problems.append("%s probe path must be a plain relative path: %s" % (label, rel))
    elif is_reserved(rel, MEMDIR_REL):
        problems.append("%s probe path is reserved: %s" % (label, rel))


def _check_page(page, workspace, config, problems):
    if not isinstance(page, dict) or set(page) != PAGE_KEYS:
        problems.append("page keys must be exactly %s: %r" % (sorted(PAGE_KEYS), page))
        return None
    file = page["file"]
    label = "page %s" % (file,)
    if page["bucket"] not in ("path", "tool"):
        problems.append("%s: bucket must be 'path' or 'tool'" % label)
        return file
    if not isinstance(file, str) or not file.endswith(".md"):
        problems.append("%s: file must end .md" % label)
        return file
    try:
        live = _page_path(page, workspace)
    except PathError as exc:
        problems.append("%s: %s" % (label, exc))
        return file
    live_sha = sha256_file(live)
    if page["base_sha256"] != live_sha:
        problems.append("%s: base_sha256 is %s but the live file is %s"
                        % (label, page["base_sha256"], live_sha))
    text = page["content"]
    if text is None:
        if live_sha is None:
            problems.append("%s: content is null but the file does not exist" % label)
            text = None
        else:
            text = _read_text(live)
    elif not isinstance(text, str):
        problems.append("%s: content must be a string or null" % label)
        text = None
    patterns = []
    if text is not None:
        if page["bucket"] == "path":
            if not growth_lib.has_paths_frontmatter(text):
                problems.append("%s: path page needs paths: frontmatter" % label)
            limit = growth_lib.rules_max_lines(config)
            if len(text.splitlines()) > limit:
                problems.append("%s: over %d lines" % (label, limit))
        else:
            parsed = tool_rules_lib.parse_page(text)
            if parsed is None:
                problems.append("%s: tool page has no frontmatter tools: list" % label)
            else:
                patterns, body = parsed
                cap = tool_rules_lib.body_limit(tool_rules_lib.max_bytes(config))
                size = len(body.encode("utf-8"))
                if size > cap:
                    problems.append("%s: body is %d bytes, over the %d-byte deliverable limit (max_bytes minus the hook's reserve)"
                                    % (label, size, cap))
                # The hook's own pattern, so a malformed "Bash(" never counts.
                if not any(tool_rules_lib._BASH_PATTERN.match(p) for p in patterns):
                    problems.append(NO_BASH_PROBLEM)
    probe = page["probe"]
    if page["bucket"] == "tool":
        command = (probe.get("tool_input") or {}).get("command") \
            if isinstance(probe, dict) and isinstance(probe.get("tool_input"), dict) else None
        if not (isinstance(probe, dict) and set(probe) == {"tool_name", "tool_input"}
                and probe["tool_name"] == "Bash"
                and set(probe["tool_input"]) == {"command"}
                and isinstance(command, str) and command):
            problems.append('%s: probe must be exactly {"tool_name": "Bash", '
                            '"tool_input": {"command": <non-empty str>}}' % label)
        else:
            if patterns and not any(
                    tool_rules_lib.matches(p, "Bash", probe["tool_input"]) for p in patterns):
                problems.append("%s: probe command matches no pattern of the page" % label)
            if probe_script(command) is None:
                problems.append("%s: probe command must run a stub-able script: "
                                "'python3|bash|sh <relative path> \u2026' or '<relative path> \u2026' "
                                "(not reserved); widen the Bash() pattern with a leading * if needed"
                                % label)
    else:
        _check_probe_path(label, probe, problems)
    return file


def _check_move(move, memory_dir, page_files, index_text, problems):
    if not isinstance(move, dict) or set(move) != MOVE_KEYS:
        problems.append("move keys must be exactly %s: %r" % (sorted(MOVE_KEYS), move))
        return None
    memory = move["memory"]
    label = "move %s" % (memory,)
    if not (isinstance(memory, str) and re.match(r"^[^/\\]+\.md$", memory)
            and memory != "MEMORY.md" and memory not in (".", "..")):
        problems.append("%s: memory must be a bare *.md name" % label)
        return None
    leaf = os.path.join(memory_dir, memory)
    leaf_sha = sha256_file(leaf)
    if leaf_sha is None:
        problems.append("%s: leaf does not exist in the memory dir" % label)
    else:
        if move["base_sha256"] != leaf_sha:
            problems.append("%s: base_sha256 is %s but the live leaf is %s"
                            % (label, move["base_sha256"], leaf_sha))
        _check_leaf(memory, _read_text(leaf), problems)
    if move["page"] not in page_files:
        problems.append("%s: page %r is not in pages" % (label, move["page"]))
    topic = move["topic"]
    if not (isinstance(topic, str) and _TOPIC_RE.match(topic)):
        problems.append("%s: topic must match topics/<name>.md: %r" % (label, topic))
    else:
        if not os.path.isfile(os.path.join(memory_dir, topic)):
            problems.append("%s: topic %s does not exist" % (label, topic))
        nominated = any(r["section"] == "topics" and r["file"] == topic
                        for r in lint_lib.parse_sections(index_text))
        if not nominated:
            problems.append("%s: topic %s is not a row target in MEMORY.md's ## Topics"
                            % (label, topic))
    row = move["row"]
    if not isinstance(row, str) or not row:
        problems.append("%s: row must be a non-empty string" % label)
    else:
        count = sum(1 for l in _always_lines(index_text) if l == row)
        if count != 1:
            problems.append("%s: row must appear exactly once as a whole line in "
                            "## Always (found %d): %s" % (label, count, row))
        if "](%s)" % memory not in row:
            problems.append("%s: row's link target must be (%s)" % (label, memory))
    return memory


def _check_behaviour(entries, moved, problems):
    if len(entries) > MAX_BEHAVIOUR:
        problems.append("behaviour has %d entries; at most %d" % (len(entries), MAX_BEHAVIOUR))
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != BEHAVIOUR_KEYS:
            problems.append("behaviour keys must be exactly %s: %r"
                            % (sorted(BEHAVIOUR_KEYS), entry))
            continue
        label = "behaviour %s" % (entry["memory"],)
        if entry["memory"] not in moved:
            problems.append("%s: memory is not in any move" % label)
        for key in ("prompt", "pass_if"):
            if not (isinstance(entry[key], str) and entry[key].strip()):
                problems.append("%s: %s must be a non-empty string" % (label, key))
        files = entry["files"]
        if not isinstance(files, dict):
            problems.append("%s: files must be an object" % label)
            continue
        for key, value in files.items():
            if bad_relpath(key):
                problems.append("%s: file key must be a plain relative path: %s" % (label, key))
            elif is_reserved(key, MEMDIR_REL):
                problems.append("%s: file key is reserved: %s" % (label, key))
            if not isinstance(value, str):
                problems.append("%s: file %s value must be a string" % (label, key))


def _check_behaviour_pages(manifest, problems):
    """G7: behaviour runs have no Bash, so a tool rule could never reach them."""
    tool_files = set()
    for page in manifest["pages"]:
        if isinstance(page, dict) and page.get("bucket") == "tool":
            tool_files.add(page.get("file"))
    to_tool = set()
    for move in manifest["moves"]:
        if isinstance(move, dict) and move.get("page") in tool_files:
            to_tool.add(move.get("memory"))
    for entry in manifest["behaviour"]:
        if isinstance(entry, dict) and entry.get("memory") in to_tool:
            problems.append("behaviour %s: its rule moves to a tool page; "
                            "behaviour cases cover path pages only" % entry["memory"])


def validate(manifest, memory_dir, workspace, config=None):
    """Every violation as a string; empty when the manifest is valid."""
    problems = []
    if not isinstance(manifest, dict) or set(manifest) != TOP_KEYS:
        problems.append("manifest keys must be exactly %s" % sorted(TOP_KEYS))
        return problems
    if manifest["version"] != 1:
        problems.append("version must be 1")
    lists_ok = True
    for key in ("pages", "moves", "behaviour"):
        if not isinstance(manifest[key], list):
            problems.append("%s must be a list" % key)
            lists_ok = False
    result = lint_lib.scan(memory_dir)
    if not result["readable"]:
        problems.append("live memory not clean: memory dir is not readable")
    else:
        for finding in lint_lib.findings(result):
            problems.append("live memory not clean: %s" % (finding,))
    if not lists_ok:
        return problems

    files = []
    for page in manifest["pages"]:
        files.append(_check_page(page, workspace, config, problems))
    seen = set()
    for file in files:
        if file is not None and file in seen:
            problems.append("two pages share file %s" % file)
        seen.add(file)

    index_path = os.path.join(memory_dir, lint_lib.INDEX_NAME)
    index_text = _read_text(index_path) if os.path.isfile(index_path) else ""
    moved, seen_moves = [], set()
    for move in manifest["moves"]:
        memory = _check_move(move, memory_dir, set(f for f in files if f), index_text, problems)
        if memory is not None:
            if memory in seen_moves:
                problems.append("memory %s appears in two moves" % memory)
            seen_moves.add(memory)
            moved.append(memory)
    # _read_text above folds CRLF to LF, so line endings are judged on raw bytes.
    edited = [index_path] + [os.path.join(memory_dir, m) for m in moved]
    edited += sorted(set(os.path.join(memory_dir, m["topic"]) for m in manifest["moves"]
                         if isinstance(m, dict) and isinstance(m.get("topic"), str)
                         and _TOPIC_RE.match(m["topic"])))
    for path in edited:
        if os.path.isfile(path) and _mixed_eol(_read_raw(path)):
            problems.append("%s mixes Windows (CRLF) and Unix (LF) line endings; "
                            "convert it to one, then rerun" % _display(path))
    _check_behaviour(manifest["behaviour"], set(moved), problems)
    _check_behaviour_pages(manifest, problems)
    return problems


def dependency_set(manifest, memory_dir, workspace):
    """What the manifest depends on: files (sha), Always rows, topic files."""
    files, rows, topics = {}, [], {}
    index = os.path.join(memory_dir, lint_lib.INDEX_NAME)
    for page in manifest.get("pages", []):
        path = _page_path(page, workspace)
        files[str(path)] = sha256_file(path)
    for move in manifest.get("moves", []):
        leaf = os.path.join(memory_dir, move["memory"])
        files[leaf] = sha256_file(leaf)
        rows.append({"file": index, "row": move["row"]})
        topics[os.path.join(memory_dir, move["topic"])] = True
    return {"files": files, "rows": rows, "topics": topics}


# --- apply -------------------------------------------------------------------

_ATTEMPTS = 3
_LEAF_TYPE_RE = re.compile(r"^(\s*)type:\s*\S+")


class ApplyFailure(Exception):
    pass


def _display(path):
    """`path` with the home prefix shown as ~ (HOME read at call time)."""
    home = os.path.expanduser("~")
    path = str(path)
    if home and path.startswith(home.rstrip("/\\") + os.sep):
        return "~" + path[len(home.rstrip("/\\")):]
    return path


def _read_raw(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _atomic_write(path, text):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".distill-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sha_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _leaf_transform(display, today):
    note = ("> **Rule text lives in `%s`** (moved %s, index-distill). Correct the "
            "rule THERE; this file is the evidence behind it." % (display, today))

    def transform(text):
        lines = text.split("\n")
        close = next(i for i in range(1, len(lines)) if lines[i].rstrip() == "---")
        for i in range(1, close):
            if _LEAF_TYPE_RE.match(lines[i]):
                lines[i] = _LEAF_TYPE_RE.sub(r"\1type: reference", lines[i], count=1)
                break
        return "\n".join(lines[:close + 1] + ["", note] + lines[close + 1:])

    return transform


def operations(manifest, memory_dir, workspace, today):
    """Ordered (abs_path, kind, payload): pages, leaves, topics, then MEMORY.md.

    kind: page -> new text; leaf -> old-text-to-new-text function;
    topic -> [(heading, [rows])]; index -> [rows to remove from ## Always].
    """
    pages = {}
    for page in manifest["pages"]:
        pages[page["file"]] = _page_path(page, workspace)
    ops, topic_groups, index_rows = [], {}, []
    for page in manifest["pages"]:
        if page["content"] is not None:
            ops.append((str(pages[page["file"]]), "page", page["content"]))
    for move in manifest["moves"]:
        display = _display(pages[move["page"]])
        ops.append((str(resolve_inside(memory_dir, move["memory"])), "leaf",
                    _leaf_transform(display, today)))
        heading = "## Rules carried by %s \u2014 these are the evidence files" % display
        row = move["row"].replace("(%s)" % move["memory"], "(../%s)" % move["memory"])
        groups = topic_groups.setdefault(move["topic"], [])
        for group in groups:
            if group[0] == heading:
                group[1].append(row)
                break
        else:
            groups.append((heading, [row]))
        index_rows.append(move["row"])
    for topic, groups in topic_groups.items():
        ops.append((str(resolve_inside(memory_dir, topic)), "topic", groups))
    ops.append((os.path.join(memory_dir, lint_lib.INDEX_NAME), "index", index_rows))
    return ops


def _index_remove(text, rows):
    """`text` without each row, found as a whole line inside ## Always."""
    lines = text.split("\n")
    for row in rows:
        section, hit = None, None
        for i, line in enumerate(lines):
            heading = _HEADING_RE.match(line)
            if heading:
                section = "always" if re.match(r"always\b", heading.group(1), re.I) else "other"
            elif section == "always" and line == row:
                hit = i
                break
        if hit is None:
            raise ApplyFailure("row no longer in ## Always: %s" % row)
        del lines[hit]
    return "\n".join(lines)


def _section_bounds(lines, heading):
    """(heading index, section end index), or None when the heading is absent."""
    if heading not in lines:
        return None
    start = lines.index(heading)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if _HEADING_RE.match(lines[i]):
            end = i
            break
    return start, end


def _topic_append(text, groups):
    """(new text, info) with each group's rows added; info feeds the inverse."""
    lines = text.split("\n")
    info = []
    for heading, rows in groups:
        bounds = _section_bounds(lines, heading)
        if bounds is None:
            if lines and lines[-1] == "":
                lines.pop()
            lines.extend(["", heading, ""] + rows + [""])
            info.append({"heading": heading, "rows": list(rows), "created": True})
            continue
        start, end = bounds
        last = start
        for i in range(start + 1, end):
            if lines[i].startswith("- "):
                last = i
        if last == start and start + 1 < len(lines) and lines[start + 1] == "":
            last = start + 1
        lines[last + 1:last + 1] = rows
        info.append({"heading": heading, "rows": list(rows), "created": False})
    return "\n".join(lines), info


def _topic_remove(text, info):
    """Inverse of _topic_append: drop exactly the rows this run added."""
    lines = text.split("\n")
    for group in info:
        bounds = _section_bounds(lines, group["heading"])
        if bounds is None:
            raise ApplyFailure("heading gone from topic: %s" % group["heading"])
        for row in group["rows"]:
            start, end = _section_bounds(lines, group["heading"])
            hit = next((i for i in range(start + 1, end) if lines[i] == row), None)
            if hit is None:
                raise ApplyFailure("row no longer in topic: %s" % row)
            del lines[hit]
        start, end = _section_bounds(lines, group["heading"])
        if group["created"] and not any(l.strip() for l in lines[start + 1:end]):
            begin = start - 1 if start > 0 and lines[start - 1].strip() == "" else start
            lines[begin:end] = [""] if end == len(lines) else []
    return "\n".join(lines)


def _mixed_eol(text):
    return "\r\n" in text and "\n" in text.replace("\r\n", "")


def _as_lf(text):
    """(`text` with CRLF read as LF, a function giving a result back the file's ending).

    Every edit here splits on "\\n", so a Windows-edited file is edited as LF and
    written back as CRLF. validate() refuses a file that mixes the two."""
    if "\r\n" not in text:
        return text, lambda new: new
    return text.replace("\r\n", "\n"), lambda new: new.replace("\n", "\r\n")


def _rewrite(path, compute, hook=None):
    """Read t0, compute new, hook, re-read t1; replace only if t1 == t0.

    Returns whatever `compute` returned alongside the text (its second value).
    """
    for _ in range(_ATTEMPTS):
        t0 = _read_raw(path)
        lf, restore = _as_lf(t0)
        new, extra = compute(lf)
        new = restore(new)
        if hook:
            hook(path)
        if _read_raw(path) == t0:
            _atomic_write(path, new)
            return extra
    raise ApplyFailure("%s kept changing underneath apply (%d attempts)"
                       % (path, _ATTEMPTS))


def apply_operations(ops, expect=None, hook=None, done=None):
    """Apply `ops`; pages and leaves from prepared text, topics and MEMORY.md
    by re-applying their row operations to a fresh read, MEMORY.md last.

    `expect` maps path -> sha the target must still have before its replace.
    `done` collects ("file", path, sha_written) / ("topic", path, info).
    Raises ApplyFailure on any failure.
    """
    done = [] if done is None else done
    for kind in ("page", "leaf"):
        for path, op_kind, payload in ops:
            if op_kind != kind:
                continue
            if kind == "page":
                new = payload
            else:
                lf, restore = _as_lf(_read_raw(path))
                new = restore(payload(lf))
            if expect is not None and sha256_file(path) != expect.get(path):
                raise ApplyFailure("%s changed since the proof" % path)
            _atomic_write(path, new)
            done.append(("file", path, _sha_text(new)))
    for path, kind, payload in ops:
        if kind == "topic":
            info = _rewrite(path, lambda t, g=payload: _topic_append(t, g), hook)
            done.append(("topic", path, info))
    for path, kind, payload in ops:
        if kind == "index":
            _rewrite(path, lambda t, r=payload: (_index_remove(t, r), None), hook)
    return done


def apply_manifest(manifest, memory_dir, workspace, today):
    """operations + apply_operations: no proof, claim or backup."""
    return apply_operations(operations(manifest, memory_dir, workspace, today))


def _stamp_dir(home):
    base = os.path.join(home, ".claude", "index-distill", "backups")
    os.makedirs(base, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for n in range(1000):
        path = os.path.join(base, stamp if n == 0 else "%s-%d" % (stamp, n))
        try:
            os.mkdir(path)
            return path
        except FileExistsError:
            continue
    raise OSError("cannot create a backup directory under %s" % base)


def _backup(ops, memory_dir, workspace):
    backup = _stamp_dir(os.path.expanduser("~"))
    real_mem, real_ws = os.path.realpath(memory_dir), os.path.realpath(workspace)
    originals, rel_map = {}, {}
    for path, _kind, _payload in ops:
        real = os.path.realpath(path)
        if os.path.commonpath([real_mem, real]) == real_mem:
            rel = os.path.join("mem", os.path.relpath(real, real_mem))
        else:
            rel = os.path.join("ws", os.path.relpath(real, real_ws))
        if os.path.isfile(path):
            dest = os.path.join(backup, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(path, dest)
            originals[path] = "present"
        else:
            originals[path] = "absent"
        rel_map[path] = rel
    with open(os.path.join(backup, "originals.json"), "w", encoding="utf-8") as fh:
        json.dump(originals, fh, indent=1)
    return backup, rel_map


def _check_proof(manifest_path, manifest, memory_dir, workspace, session_id):
    """Step 1: every problem, nothing written."""
    proof_path = manifest_path + ".proof.json"
    try:
        with open(proof_path, encoding="utf-8") as fh:
            proof = json.load(fh)
        with open(manifest_path, "rb") as fh:
            manifest_sha = hashlib.sha256(fh.read()).hexdigest()
    except (OSError, ValueError) as exc:
        return ["no usable proof file %s: %s" % (proof_path, exc)]
    if not isinstance(proof, dict):
        return ["proof file is not a JSON object"]
    problems = []
    if proof.get("verdict") != "PASS":
        problems.append("proof verdict is %r, not PASS" % (proof.get("verdict"),))
    if proof.get("manifest_sha256") != manifest_sha:
        problems.append("manifest changed since the proof")
    roots = proof.get("roots") if isinstance(proof.get("roots"), dict) else {}
    for field, live in (("memory_dir", os.path.realpath(memory_dir)),
                        ("workspace", os.path.realpath(workspace))):
        if roots.get(field) != live:
            problems.append("roots changed since the proof: %s was %s, now %s"
                            % (field, roots.get(field), live))
    holder = distill_claim.read_live()
    # A half-written claim reads as session_id None: an unidentified caller
    # must not match it.
    if not session_id or not holder or holder.get("session_id") != session_id:
        problems.append("the distill claim is not held by this session")
    deps = proof.get("dependencies")
    try:
        now = dependency_set(manifest, memory_dir, workspace)
    except (PathError, KeyError, TypeError) as exc:
        return problems + ["cannot derive the dependency set: %s" % exc]
    if not (isinstance(deps, dict) and isinstance(deps.get("files"), dict)
            and isinstance(deps.get("rows"), list) and isinstance(deps.get("topics"), dict)):
        return problems + ["proof dependencies are malformed"]
    try:
        proof_rows = {(r["file"], r["row"]) for r in deps["rows"]}
    except (KeyError, TypeError):
        return problems + ["proof dependencies are malformed"]
    now_rows = {(r["file"], r["row"]) for r in now["rows"]}
    for label, a, b in (("files", set(deps["files"]), set(now["files"])),
                        ("rows", proof_rows, now_rows),
                        ("topics", set(deps["topics"]), set(now["topics"]))):
        if a != b:
            problems.append("proof dependencies differ from this manifest's (%s): "
                            "only in proof %s, only now %s"
                            % (label, sorted(map(str, a - b)), sorted(map(str, b - a))))
    if problems:
        return problems
    for path, sha in deps["files"].items():
        if sha256_file(path) != sha:
            problems.append("dependency changed since the proof: %s" % path)
    for file, row in proof_rows:
        try:
            count = sum(1 for l in _always_lines(_read_raw(file)) if l == row)
        except OSError:
            count = 0
        if count != 1:
            problems.append("row is not exactly once in ## Always (found %d): %s" % (count, row))
    for path in deps["topics"]:
        if not os.path.isfile(path):
            problems.append("topic is missing: %s" % path)
    return problems


def _roll_back(done, originals, backup, rel_map):
    """Step 5. (rolled_back_count, [(path, why)]) -- conflicts, never silent."""
    rolled, conflicts = 0, []
    for entry in reversed(done):
        kind, path = entry[0], entry[1]
        try:
            if kind == "file":
                if sha256_file(path) != entry[2]:
                    conflicts.append((path, "changed by someone else since this run wrote it"))
                    continue
                if originals.get(path) == "absent":
                    os.unlink(path)
                else:
                    shutil.copy2(os.path.join(backup, rel_map[path]), path)
            else:
                _rewrite(path, lambda t, i=entry[2]: (_topic_remove(t, i), None))
            rolled += 1
        except Exception as exc:
            conflicts.append((path, "%s: %s" % (type(exc).__name__, exc)))
    return rolled, conflicts


def apply(manifest_path, memory_dir, workspace, session_id, config=None,
          today=None, _hook=None):
    """Steps 1-6 of the write contract. Returns the CLI exit code."""
    today = today or datetime.date.today().isoformat()
    try:
        manifest = load_manifest(manifest_path)
        # The same checks `check` runs: apply never trusts a proof for a manifest
        # that is itself invalid (a leaf path escaping the memory dir, say).
        problems = validate(manifest, memory_dir, workspace, config)
        if not problems:
            problems = _check_proof(manifest_path, manifest, memory_dir, workspace,
                                    session_id)
    except (ManifestError, OSError) as exc:
        problems = [str(exc)]
    if problems:
        for problem in problems:
            print("- %s" % problem)
        return 1
    before = lint_lib.scan(memory_dir).get("index_chars", 0)
    try:
        ops = operations(manifest, memory_dir, workspace, today)
    except (PathError, KeyError, TypeError) as exc:
        print("- cannot resolve the manifest's paths: %s" % exc)
        return 1
    backup, rel_map = _backup(ops, memory_dir, workspace)
    print("backup: %s" % backup)
    with open(os.path.join(backup, "originals.json"), encoding="utf-8") as fh:
        originals = json.load(fh)
    # Leaves must still match their dependency sha; pages their base_sha256.
    with open(manifest_path + ".proof.json", encoding="utf-8") as fh:
        expect = dict(json.load(fh)["dependencies"]["files"])
    for page in manifest["pages"]:
        expect[str(_page_path(page, workspace))] = page["base_sha256"]

    done = []
    try:
        apply_operations(ops, expect=expect, hook=_hook, done=done)
    except Exception as exc:
        print("apply failed: %s" % exc)
        if not done:
            # Nothing was replaced, so this is a refusal: exit 1 promises an
            # untouched tree, and exit 5 would claim a partial write that never
            # happened.
            print("refused before any write; nothing written (backup at %s)" % backup)
            return 1
        rolled, conflicts = _roll_back(done, originals, backup, rel_map)
        if conflicts:
            print("ROLLBACK INCOMPLETE \u2014 resolve these by hand "
                  "(pre-apply copies in %s):" % backup)
            for path, why in conflicts:
                print("- %s: %s" % (path, why))
            return 6
        print("apply failed partway; rolled back %d file(s); backup at %s" % (rolled, backup))
        return 5

    result = lint_lib.scan(memory_dir)
    after = result.get("index_chars", 0)
    print("index chars: %d -> %d" % (before, after))
    found = lint_lib.findings(result)
    for finding in found:
        print("- %s" % (finding,))
    print("Restart any Claude session started before %s: it still holds the old layout."
          % datetime.datetime.now().strftime("%H:%M"))
    distill_claim.release(session_id)
    return 4 if found or lint_lib.distill_size(result) >= lint_lib.distill_at_chars(config) else 0
