"""Reading files: a page at a time, the block a definition starts, or several
slices at once, with the root and the deny list enforced."""

from pathlib import Path

from ..core import Rules, ToolError, config, rel_of, resolve
from ..text import MAX_LINE, check_tail, human_size, read_text
from .registry import FILE, tool

# Files whose blocks are their indentation rather than their braces.
INDENTED = {".py", ".pyi", ".yaml", ".yml", ".toml", ".nim", ".coffee"}
# Lines that belong to the definition just below them.
LEADERS = ("#[", "#![", "//", "@", "/**", "* ", "*/")
# How far below an `at` line its opening brace may be (a long signature).
BRACE_REACH = 40

RANGE_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path"],
    "properties": {
        "path": {"type": "string", "description": "The file."},
        "start": {"type": "integer", "description": "First line, 1-based (default 1)."},
        "end": {"type": "integer", "description": "Last line, inclusive."},
        "at": {"type": "string", "description": "Instead: the block starting here."},
    },
}


def brace_depths(lines, start):
    """Each line's brace balance from `start` on, outside strings, char
    literals and comments, and whether the line opened a brace at all."""
    in_block = False
    for line in lines[start:]:
        balance, opened, i, quote = 0, False, 0, None
        while i < len(line):
            two = line[i : i + 2]
            if in_block:
                if two == "*/":
                    in_block, i = False, i + 2
                    continue
            elif quote:
                if line[i] == "\\":
                    i += 1
                elif line[i] == quote:
                    quote = None
            elif two == "//":
                break
            elif two == "/*":
                in_block, i = True, i + 2
                continue
            elif line[i] == '"':
                quote = '"'
            elif line[i] == "'" and line[i + 2 : i + 3] == "'":
                i += 2  # a char literal such as '{'; a Rust lifetime has no closing quote
            elif line[i] == "{":
                balance, opened = balance + 1, True
            elif line[i] == "}":
                balance -= 1
            i += 1
        yield balance, opened, line


def block_end(lines, start, indented):
    """The index of the last line of the block that starts at `start`."""
    if indented:
        base = len(lines[start]) - len(lines[start].lstrip())
        end = start
        for i in range(start + 1, len(lines)):
            text = lines[i]
            if not text.strip():
                continue
            if len(text) - len(text.lstrip()) <= base:
                break
            end = i
        return end
    depth, opened = 0, False
    for offset, (balance, saw, line) in enumerate(brace_depths(lines, start)):
        depth += balance
        opened = opened or saw
        if opened and depth <= 0:
            return start + offset
        # A declaration with no body: `fn f();`, `struct Unit;`, `use a::b;`.
        if not opened and line.rstrip().endswith(";"):
            return start + offset
        if not opened and offset >= BRACE_REACH:
            return start
    return len(lines) - 1


def block_slice(rel, lines, at, suffix):
    """(first, last, note) for the block at the first line containing `at`."""
    hits = [i for i, line in enumerate(lines) if at in line]
    if not hits:
        raise ToolError(f"{rel}: no line contains {at!r}")
    start = hits[0]
    end = block_end(lines, start, suffix in INDENTED)
    while start > 0 and lines[start - 1].lstrip().startswith(LEADERS):
        start -= 1
    more = len(hits) - 1
    note = (
        f"; {more} more line(s) contain it, the first at {hits[1] + 1}" if more else ""
    )
    return start, end, note


def numbered(lines, start, end):
    return "\n".join(
        f"{i + 1:>6}\t{lines[i][:MAX_LINE]}"
        + (" [line cut]" if len(lines[i]) > MAX_LINE else "")
        for i in range(start, end + 1)
    )


def load_lines(path, rules):
    """(rel, lines, note) for a file: its lines, or none and a note saying why
    there are none (binary, or empty)."""
    target = resolve(path, rules)
    rel = rel_of(target)
    if target.is_dir():
        raise ToolError(f"{rel} is a directory")
    if not target.exists():
        raise ToolError(f"no such file: {rel}")
    text = read_text(target)
    if text is None:
        return rel, [], f"{rel}: binary, {human_size(target.stat().st_size)}"
    lines = text.splitlines()
    return rel, lines, None if lines else f"{rel}: empty"


def read_slice(rel, lines, suffix, start=None, end=None, at=None, limit=2000):
    """One slice, with its header: by line range, or the block `at` starts."""
    note = ""
    if at is not None:
        first, last, note = block_slice(rel, lines, at, suffix)
    else:
        first = max((start or 1) - 1, 0)
        last = len(lines) - 1 if end is None else min(end - 1, len(lines) - 1)
    if first > last or first >= len(lines):
        raise ToolError(f"{rel}: no lines {first + 1}-{last + 1} (it has {len(lines)})")
    shown = min(last, first + max(limit, 1) - 1)
    if shown < last:
        note += f"; cut at limit {limit}, offset={shown + 2} for more"
    head = f"[{rel}: lines {first + 1}-{shown + 1} of {len(lines)}{note}]"
    return f"{head}\n{numbered(lines, first, shown)}"


@tool(
    """
    Read a text file, numbered like `cat -n`. Long files come a page at a time:
    the header says which lines these are and the offset for the next page. A
    negative offset counts from the end: -50 is the last 50 lines, as is
    `tail` 50. To see only the lines matching a pattern, with these same line
    numbers, search it with `grep -n` instead.

    `at` reads one block instead: from the first line containing that text
    (`fn refresh_bounds`, `class Loader`) to the end of its body, by braces, or
    by indentation in Python and YAML, with the attributes and comments just
    above it. `ranges` reads several slices at once, from one file or many,
    each a line range or an `at` block under its own header.
    """,
    positional=("path",),
    path=(*FILE, None),
    offset=("integer", "First line to show, 1-based; negative counts from the end.", 1),
    limit=("integer", "Most lines to show (per slice, with ranges).", 2000),
    tail=("integer", "The last N lines, like `tail -n N`; replaces offset.", None),
    at=("string", "Text on the first line of the block to read.", None),
    ranges=(
        {"type": "array", "items": RANGE_ITEM},
        "Several slices, each {path, start?, end?} or {path, at}; replaces path.",
        None,
    ),
)
def read(path=None, offset=1, limit=2000, tail=None, at=None, ranges=None):
    rules = Rules(config())
    if ranges is not None:
        if path is not None or at is not None:
            raise ToolError("pass ranges, or path (and at), not both")
        if tail is not None or offset != 1:
            raise ToolError("ranges give their own lines; they take no offset or tail")
        if not ranges:
            raise ToolError("ranges is empty")
        out, loaded = [], {}
        for item in ranges:
            if item.get("at") is not None and (
                item.get("start") is not None or item.get("end") is not None
            ):
                raise ToolError(f"{item['path']}: give at, or start and end, not both")
            if item["path"] not in loaded:
                loaded[item["path"]] = load_lines(item["path"], rules)
            rel, lines, note = loaded[item["path"]]
            if note:
                out.append(f"[{note}]")
                continue
            suffix = Path(rel).suffix
            out.append(
                read_slice(
                    rel,
                    lines,
                    suffix,
                    item.get("start"),
                    item.get("end"),
                    item.get("at"),
                    limit,
                )
            )
        return "\n\n".join(out)
    if path is None:
        raise ToolError("pass path, or ranges")
    rel, lines, note = load_lines(path, rules)
    if note:
        return note
    if at is not None:
        if tail is not None or offset != 1:
            raise ToolError("at reads a block; it takes no offset or tail")
        return read_slice(rel, lines, Path(rel).suffix, at=at, limit=limit)
    if check_tail(tail) is not None:
        if offset != 1 or limit != 2000:
            raise ToolError("pass tail, or offset and limit, not both")
        offset, limit = -tail, tail
    start = max(len(lines) + offset, 0) if offset < 0 else max(offset, 1) - 1
    page = lines[start : start + max(limit, 1)]
    if not page:
        raise ToolError(f"{rel}: no line {start + 1} (it has {len(lines)})")
    end = start + len(page)
    body = numbered(lines, start, end - 1) if page else ""
    if start == 0 and end == len(lines):
        return body
    more = f"; offset={end + 1} for more" if end < len(lines) else ""
    return f"[{rel}: lines {start + 1}-{end} of {len(lines)}{more}]\n{body}"
