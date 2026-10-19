"""The layout `gci tools` and `gci scripts` print: one block per entry, its
head line, a summary under it, and label/value rows in two columns. MCP
answers keep their own denser layout; this one is for a terminal."""

import shutil
import textwrap

from .text import check_filters, filter_text

# Wide enough for a usage line, narrow enough to read; a pipe gets this too.
MAX_WIDTH = 100
INDENT = "  "
ROW_INDENT = "    "
# A label past this pushes its value onto the next line rather than every row's.
MAX_LABEL = 24


def width() -> int:
    return min(shutil.get_terminal_size((MAX_WIDTH, 24)).columns, MAX_WIDTH)


def block(head: str, summary: str | None, rows: list[tuple[str, str]]) -> str:
    """One entry: `head`, wrapped with a hanging indent, then `summary` and
    each (label, value) row under it, the values in one column."""
    cols = width()
    lines = textwrap.wrap(head, cols, subsequent_indent=INDENT * 3) or [""]
    if summary:
        lines += textwrap.wrap(
            summary, cols, initial_indent=INDENT, subsequent_indent=INDENT
        )
    labels = [label for label, _ in rows if len(label) <= MAX_LABEL]
    pad = max(map(len, labels), default=0) + 2
    for label, value in rows:
        start = ROW_INDENT + label.ljust(pad)
        hang = " " * len(ROW_INDENT + " " * pad)
        if len(label) > MAX_LABEL:
            lines.append(ROW_INDENT + label)
            start = hang
        lines += textwrap.wrap(
            value, cols, initial_indent=start, subsequent_indent=hang
        ) or [start.rstrip()]
    return "\n".join(lines)


def filter_blocks(text: str, grep: str | None, tail: int | None) -> str:
    """A listing narrowed to the blocks holding a line that matches `grep`,
    whole, so a match keeps its head and rows; then `tail` lines of that."""
    rx, _, _ = check_filters(grep, tail)
    if rx is not None:
        blocks = text.split("\n\n")
        kept = [b for b in blocks if any(rx.search(ln) for ln in b.splitlines())]
        note = f"[grep `{grep}`: {len(kept)} of {len(blocks)} block(s) match]"
        text = "\n\n".join([note, *kept])
    return filter_text(text, None, tail)


def listing(blocks: list[str], notes: list[str] = ()) -> str:
    """Blocks a blank line apart, then any closing notes."""
    return "\n\n".join([*blocks, *notes])
