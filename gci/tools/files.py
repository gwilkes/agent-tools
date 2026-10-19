"""Editing files: exact text, line ranges and marked regions in one or several
files, all or nothing, with the root and the deny/protect lists enforced."""

from ..core import Rules, ToolError, config
from ..text import open_text, unified_diff, write_bytes
from .registry import tool

EDIT_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["new"],
    "properties": {
        "old": {"type": "string", "description": "Exact text to replace."},
        "replace_all": {
            "type": "boolean",
            "description": "With old: every occurrence.",
        },
        "start": {
            "type": "integer",
            "description": "First line of a range to replace, 1-based.",
        },
        "end": {
            "type": "integer",
            "description": "Last line of the range, inclusive (default start).",
        },
        "expect": {
            "type": "string",
            "description": "With start: those lines exactly as they are now. Required.",
        },
        "after": {
            "type": "string",
            "description": "Text on the one line just before a region.",
        },
        "before": {"type": "string", "description": "Text on the first line after it."},
        "new": {
            "type": "string",
            "description": "What the text, range or region becomes.",
        },
    },
}
FILE_ITEM = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "edits"],
    "properties": {
        "path": {"type": "string"},
        "edits": {"type": "array", "items": EDIT_ITEM},
    },
}


def as_lines(new):
    """`new` as whole lines: a final newline added unless it is empty or has one."""
    return new if not new or new.endswith("\n") else new + "\n"


def apply_ranges(rel, text, ranges, label):
    """`text` with each (n, change) line-range edit made. The ranges are
    numbered as the file was before the call, the way the caller read it, so
    they apply bottom up and may not overlap."""
    lines = text.splitlines(keepends=True)
    spans = []
    for n, change in ranges:
        start, end = change["start"], change.get("end", change["start"])
        if not (
            isinstance(start, int)
            and isinstance(end, int)
            and 1 <= start <= end <= len(lines)
        ):
            raise ToolError(
                f"{label}edit {n}: lines {start}-{end} are not in {rel}, "
                f"which has {len(lines)}"
            )
        if "expect" not in change:
            raise ToolError(
                f"{label}edit {n}: a line range needs `expect`, the lines as they "
                "are now, so a stale line number cannot clobber the wrong lines"
            )
        now = "".join(lines[start - 1 : end])
        if now.rstrip("\n") != change["expect"].rstrip("\n"):
            raise ToolError(
                f"{label}edit {n}: lines {start}-{end} of {rel} are not what `expect` "
                f"says; read them again. They are now:\n{now}"
            )
        spans.append((start, end, change["new"]))
    spans.sort()
    for (_, end1, _), (start2, _, _) in zip(spans, spans[1:]):
        if start2 <= end1:
            raise ToolError(f"{label}two line ranges overlap at line {start2}")
    for start, end, new in reversed(spans):
        put = as_lines(new)
        # Replacing the last line of a file with no final newline keeps it that way.
        if (
            end == len(lines)
            and not lines[-1].endswith("\n")
            and not new.endswith("\n")
        ):
            put = new
        lines[start - 1 : end] = [put] if put else []
    return "".join(lines)


def replace_text(rel, text, change, tag):
    """`text` with an exact-text edit made: `old` must occur once, or replace_all."""
    find, put = change["old"], change["new"]
    if not find:
        raise ToolError(
            f"{tag}old is empty; to replace the whole file, write it instead"
        )
    if find == put:
        raise ToolError(f"{tag}old and new are the same")
    count = text.count(find)
    if count == 0:
        raise ToolError(f"{tag}old not found in {rel}; nothing written")
    if count > 1 and not change.get("replace_all"):
        raise ToolError(
            f"{tag}old occurs {count} times in {rel}; add context to make it "
            "unique or set replace_all; nothing written"
        )
    return text.replace(find, put)


def replace_region(rel, text, change, tag):
    """`text` with everything between the one line holding `after` and the
    next holding `before` replaced by `new`."""
    if not change.get("before"):
        raise ToolError(f"{tag}a region needs both after and before")
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if change["after"] in line]
    if len(hits) != 1:
        raise ToolError(
            f"{tag}`after` is on {len(hits)} lines of {rel}; it must be on exactly one"
        )
    first = hits[0]
    last = next(
        (i for i in range(first + 1, len(lines)) if change["before"] in lines[i]),
        None,
    )
    if last is None:
        raise ToolError(f"{tag}no line after line {first + 1} of {rel} has `before`")
    put = as_lines(change["new"])
    lines[first + 1 : last] = [put] if put else []
    return "".join(lines)


def apply_edits(rel, text, edits, label=""):
    """`text` with every edit made, or a ToolError naming the first that cannot be.
    Line ranges apply first; exact text and regions then apply in order, each
    to the result of the one before."""
    kinds = []
    for n, change in enumerate(edits, 1):
        which = [k for k in ("old", "start", "after") if k in change]
        if len(which) != 1:
            raise ToolError(
                f"{label}edit {n}: give exactly one of old, start (a line range) "
                "or after/before (a region)"
            )
        kinds.append((n, which[0], change))
    text = apply_ranges(
        rel, text, [(n, c) for n, kind, c in kinds if kind == "start"], label
    )
    for n, kind, change in kinds:
        tag = f"{label}edit {n}: " if len(edits) > 1 or label else ""
        if kind == "old":
            text = replace_text(rel, text, change, tag)
        elif kind == "after":
            text = replace_region(rel, text, change, tag)
    return text


def write_all(changes):
    """Write each (target, rel, before, after) that changes, all or nothing.

    Every text is encoded before the first write, so one that cannot be
    (a lone surrogate) fails with nothing written. If a write fails, every
    file written so far, the failing one included, is put back, and the
    ToolError names any that could not be."""
    pending = []
    for target, rel, before, after in changes:
        if after == before:
            continue
        try:
            pending.append((target, rel, before.encode("utf-8"), after.encode("utf-8")))
        except UnicodeEncodeError as e:
            raise ToolError(
                f"{rel}: not writable as UTF-8 ({e}); nothing written"
            ) from None
    written = []
    try:
        for target, rel, before, after in pending:
            written.append((target, rel, before))
            write_bytes(target, after)
    except OSError as e:
        stuck = []
        for target, rel, before in written:
            try:
                # The file whose write failed may be untouched, and read-only.
                if target.read_bytes() != before:
                    write_bytes(target, before)
            except OSError:
                stuck.append(rel)
        restored = (
            f"these could not be put back: {' '.join(stuck)}"
            if stuck
            else "every file was put back as it was"
        )
        raise ToolError(f"writing failed ({e}); {restored}") from None


@tool(
    """
    Change files by exact text, by line range, or between marker lines; one
    file or several, all or nothing. `old`/`new` replaces text that must occur
    once (or set replace_all). `edits` makes several changes to `path`. `files`
    makes changes to several files, each {path, edits}: if any edit in any file
    fails, no file is written.

    An edit is one of: {old, new, replace_all?}; {start, end?, expect, new}, a
    line range as you read it, where `expect` must be those lines exactly as
    they are now; or {after, before, new}, replacing everything between the one
    line containing `after` and the next containing `before` (a generated
    block, a section between markers). Line ranges apply first, numbered as the
    file was before the call; the rest apply in order. Answers with the diffs.
    """,
    positional=("path",),
    path=("string", "The file, for one-file edits.", None),
    old=("string", "Text to find, exactly, whitespace included.", None),
    new=("string", "Text to put in its place.", None),
    replace_all=(
        "boolean",
        "Replace every occurrence instead of requiring one.",
        False,
    ),
    edits=({"type": "array", "items": EDIT_ITEM}, "Several edits to `path`.", None),
    files=(
        {"type": "array", "items": FILE_ITEM},
        "Edits to several files, all or nothing.",
        None,
    ),
)
def edit(path=None, old=None, new=None, replace_all=False, edits=None, files=None):
    if files is not None:
        if any(x is not None for x in (path, old, new, edits)) or replace_all:
            raise ToolError(
                "pass files alone (replace_all goes in each edit), "
                "or path with old/new or edits"
            )
        jobs = [(f.get("path"), f.get("edits") or []) for f in files]
    else:
        if path is None:
            raise ToolError("pass path, or files")
        if edits is None:
            if old is None or new is None:
                raise ToolError("pass old and new, or edits")
            edits = [{"old": old, "new": new, "replace_all": replace_all}]
        elif old is not None or new is not None:
            raise ToolError("pass old and new, or edits, not both")
        jobs = [(path, edits)]
    if not jobs:
        raise ToolError("no files to edit")
    rules = Rules(config())
    planned, seen = [], set()
    for file_path, file_edits in jobs:
        target, rel, before = open_text(file_path, rules, write=True)
        if target in seen:
            raise ToolError(
                f"{rel} is listed twice; give each file once, with all its edits"
            )
        seen.add(target)
        if not file_edits:
            raise ToolError(f"{rel}: no edits")
        label = f"{rel}: " if len(jobs) > 1 else ""
        planned.append(
            (target, rel, before, apply_edits(rel, before, file_edits, label))
        )
    write_all(planned)
    out = [
        unified_diff(rel, before, after) or f"{rel}: unchanged\n"
        for _, rel, before, after in planned
    ]
    if len(planned) > 1:
        out.append(
            f"edited {sum(b != a for *_, b, a in planned)} of {len(planned)} file(s)"
        )
    return "".join(out).rstrip("\n")
