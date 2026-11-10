"""The instruction meter's unit: sentences, `UNIT_VERSION` 2.

Pure functions, no file I/O. Unit v1 counted directive blocks (1 per
paragraph, list item and table body row, 0 for headings), and it was
gameable by layout alone: folding
bullets into paragraphs, or deleting the word `TRAP:` from the digest,
moved the v1 count. Unit v2 counts sentences instead: for
punctuated text, reflowing, folding bullets or moving text into a heading
cannot lower the count.

Two residuals are known and accepted, not closed:
(a) joining two *unterminated* blocks (no `.!?`) lowers the count by one --
    a layout effect, small in practice.
(b) joining sentences with `;` or an em dash, or an abbreviation like `etc.`
    ending a sentence, undercounts -- a text-editing effect, visible in a
    diff.

The rules are the functions below. It is a proxy, not an exact count, and its
job is a consistent series rather than a perfect one.

Changing any rule in this module bumps UNIT_VERSION; readings with different
versions are never compared (see `readings_lib.load`).
"""

import re

import lint_lib

UNIT_VERSION = 2

# A fence opens on `` ``` `` or `~~~` and closes on the next line whose
# stripped form starts with the SAME marker -- tracked by storing the
# 3-character prefix, never just "fenced: yes/no".
_HR_RE = re.compile(r"^(-{3,}|\*{3,}|_{3,})$")
_LIST_RE = re.compile(r"^\s*([-*+]|\d+[.)])\s+\S")
_TABLE_SEP_RE = re.compile(r"^\|?[\s:|-]+\|?$")
_IMPORT_RE = re.compile(r"^\s*@\S")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# _pieces' sentence terminators: one or more of .!? followed by any of the
# usual closing punctuation, then a word boundary (space or end of block).
_TERMINATOR_RE = re.compile(r"[.!?]+[)\]\"'*_\u201d\u2019\u00bb]*(?=\s|$)")
_LETTER_RE = re.compile(r"[^\W\d_]")
_ABBREV_RE = re.compile(r"\b(e\.g|i\.e|vs|etc|cf|approx|incl)\.", re.IGNORECASE)


def _strip_front_matter(text):
    """Remove a leading YAML front-matter block, only when it starts at byte 0.

    A `---` line elsewhere in the file is not front matter -- it is a
    horizontal rule, and left for `count_markdown` to classify as such.
    """
    if not text.startswith("---\n"):
        return text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i] == "---":
            return "\n".join(lines[i + 1:])
    return text


def _strip_blockquote(line):
    """Strip leading `>` markers, and one following space each."""
    while line.startswith(">"):
        line = line[1:]
        if line.startswith(" "):
            line = line[1:]
    return line


def _blocks(text):
    """Split text into blocks: paragraphs, list items, headings and table
    cells, each as its lines joined by a single space. See the plan's
    "Unit v2: the rules" table for the per-line classification.
    """
    text = _strip_front_matter(text or "")
    text = _COMMENT_RE.sub("", text)
    lines = text.split("\n")

    blocks = []
    current = []

    def end_block():
        if current:
            blocks.append(" ".join(current))
            del current[:]

    in_fence = False
    fence_marker = None

    for raw_line in lines:
        line = _strip_blockquote(raw_line)
        s = line.strip()

        if in_fence:
            if s.startswith(fence_marker):
                in_fence = False
                fence_marker = None
            continue

        if s.startswith("```") or s.startswith("~~~"):
            end_block()
            in_fence = True
            fence_marker = s[:3]
            continue

        if s == "" or _HR_RE.match(s):
            end_block()
            continue

        if s.startswith("|"):
            end_block()
            if _TABLE_SEP_RE.match(s):
                continue
            # `\|` is a literal pipe inside a cell (GFM), not a boundary.
            for cell in re.split(r"(?<!\\)\|", s.strip("|")):
                cell = cell.strip()
                if cell:
                    blocks.append(cell)
            continue

        if s.startswith("#"):
            end_block()
            blocks.append(re.sub(r"^#+\s*", "", s))
            continue

        if _LIST_RE.match(line):
            end_block()
            current.append(re.sub(r"^\s*([-*+]|\d+[.)])\s+", "", line).strip())
            continue

        current.append(s)

    end_block()
    return blocks


def _pieces(t):
    """Split one block into sentence-like pieces, in this order: neutralise
    code spans, link targets and URLs, drop dots from abbreviations and
    digit.digit sequences, then split on sentence terminators and keep only
    the pieces that contain a letter.
    """
    t = re.sub(r"(`+)(.+?)\1", " X ", t)
    t = re.sub(r"\]\([^)]*\)", "]", t)
    # `*` is never part of a URL here: it is closing emphasis, and letting the
    # mask take it swallowed the sentence's own terminator (`url.**`).
    t = re.sub(r"https?://[^\s)*]*[^\s).,!?*_\"'\u201d\u2019]", "U", t)
    t = _ABBREV_RE.sub(lambda m: m.group(1).replace(".", ""), t)
    t = re.sub(r"(?<=\d)\.(?=\d)", "", t)
    return [p.strip() for p in _TERMINATOR_RE.split(t) if _LETTER_RE.search(p)]


def count_markdown(text):
    return sum(len(_pieces(b)) for b in _blocks(text))


def has_import(text):
    lines = (text or "").split("\n")
    in_fence = False
    fence_marker = None
    for raw_line in lines:
        stripped = raw_line.strip()
        if in_fence:
            if stripped.startswith(fence_marker):
                in_fence = False
                fence_marker = None
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = True
            fence_marker = stripped[:3]
            continue
        if _IMPORT_RE.match(raw_line):
            return True
    return False


def count_digest(block):
    if not block:
        return 0
    lines = [ln for ln in block.split("\n") if not ln.strip().startswith("```")]
    return count_markdown("\n".join(lines))


def count_always_rows(index_text, layout):
    if layout == "two-level":
        return sum(
            1
            for entry in lint_lib.parse_sections(index_text)
            if entry["section"] == "always" and entry["parsed"] is True
        )
    return len(lint_lib.parse_index(index_text))
