"""Reading and editing files, and emptying the scratch directory, each with
the root and the deny/protect lists enforced."""

import os
import re
import shutil
import subprocess

from .. import core
from ..core import (
    DEFAULT_TMP,
    MAX_LINE,
    Rules,
    ToolError,
    check_tail,
    compile_pattern,
    config,
    human,
    read_text,
    rel_of,
    resolve,
    tmp_rel,
    unified,
    walk,
    write_text,
)
from . import FILE, GLOB, PATH, tool


@tool(
    """
    Read a text file, numbered like `cat -n`. Long files come a page at a time:
    the header says which lines these are and the offset for the next page. A
    negative offset counts from the end: -50 is the last 50 lines, as is
    `tail` 50. To see only the lines matching a pattern, with these same line
    numbers, search it with `grep -n` instead.
    """,
    path=FILE,
    offset=("integer", "First line to show, 1-based; negative counts from the end.", 1),
    limit=("integer", "How many lines to show.", 2000),
    tail=("integer", "The last N lines, like `tail -n N`; replaces offset.", None),
)
def read(path, offset=1, limit=2000, tail=None):
    if check_tail(tail) is not None:
        offset, limit = -tail, tail
    rules = Rules(config())
    target = resolve(path, rules)
    if target.is_dir():
        raise ToolError(f"{rel_of(target)} is a directory; use ls")
    text = read_text(target)
    if text is None:
        return f"{rel_of(target)}: binary, {human(target.stat().st_size)}"
    lines = text.splitlines()
    if not lines:
        return f"{rel_of(target)}: empty"
    start = max(len(lines) + offset, 0) if offset < 0 else max(offset, 1) - 1
    page = lines[start : start + max(limit, 1)]
    body = "\n".join(
        f"{start + i + 1:>6}\t{line[:MAX_LINE]}"
        + (" [line cut]" if len(line) > MAX_LINE else "")
        for i, line in enumerate(page)
    )
    end = start + len(page)
    if start == 0 and end == len(lines):
        return body
    more = f"; offset={end + 1} for more" if end < len(lines) else ""
    return f"[{rel_of(target)}: lines {start + 1}-{end} of {len(lines)}{more}]\n{body}"


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


def apply_edits(rel, text, edits, label=""):
    """`text` with every edit made, or a ToolError naming the first that cannot be.

    Line ranges are numbered as the file was before the call, the way the
    caller read it: they apply first, bottom up, and may not overlap. Exact
    text and anchored regions then apply in order, each to the result of the
    one before.
    """
    kinds = []
    for n, change in enumerate(edits, 1):
        which = [k for k in ("old", "start", "after") if k in change]
        if len(which) != 1:
            raise ToolError(
                f"{label}edit {n}: give exactly one of old, start (a line range) "
                "or after/before (a region)"
            )
        kinds.append((n, which[0], change))

    lines = text.splitlines(keepends=True)
    spans = []
    for n, kind, change in kinds:
        if kind != "start":
            continue
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
    text = "".join(lines)

    for n, kind, change in kinds:
        tag = f"{label}edit {n}: " if len(edits) > 1 or label else ""
        if kind == "old":
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
            text = text.replace(find, put)
        elif kind == "after":
            if not change.get("before"):
                raise ToolError(f"{tag}a region needs both after and before")
            lines = text.splitlines(keepends=True)
            hits = [i for i, line in enumerate(lines) if change["after"] in line]
            if len(hits) != 1:
                raise ToolError(
                    f"{tag}`after` is on {len(hits)} lines of {rel}; it must be on "
                    "exactly one"
                )
            first = hits[0]
            last = next(
                (
                    i
                    for i in range(first + 1, len(lines))
                    if change["before"] in lines[i]
                ),
                None,
            )
            if last is None:
                raise ToolError(
                    f"{tag}no line after line {first + 1} of {rel} has `before`"
                )
            put = as_lines(change["new"])
            lines[first + 1 : last] = [put] if put else []
            text = "".join(lines)
    return text


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
        if any(x is not None for x in (path, old, new, edits)):
            raise ToolError("pass files alone, or path with old/new or edits")
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
        target = resolve(file_path, rules, write=True)
        rel = rel_of(target)
        if target in seen:
            raise ToolError(
                f"{rel} is listed twice; give each file once, with all its edits"
            )
        seen.add(target)
        if not target.is_file():
            raise ToolError(f"no such file: {rel}; edit changes existing files only")
        before = read_text(target)
        if before is None:
            raise ToolError(f"{rel} is binary")
        if not file_edits:
            raise ToolError(f"{rel}: no edits")
        label = f"{rel}: " if len(jobs) > 1 else ""
        planned.append(
            (target, rel, before, apply_edits(rel, before, file_edits, label))
        )
    written = []
    try:
        for target, rel, before, after in planned:
            if after != before:
                write_text(target, after)
                written.append((target, before))
    except OSError as e:
        for target, before in written:
            write_text(target, before)
        raise ToolError(
            f"writing failed ({e}); every file was put back as it was"
        ) from None
    out = [
        unified(rel, before, after) or f"{rel}: unchanged\n"
        for _, rel, before, after in planned
    ]
    if len(planned) > 1:
        out.append(
            f"edited {sum(b != a for *_, b, a in planned)} of {len(planned)} file(s)"
        )
    return "".join(out).rstrip("\n")


SUB_RULE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["pattern", "replacement"],
    "properties": {
        "pattern": {"type": "string"},
        "replacement": {"type": "string"},
        "literal": {"type": "boolean"},
        "ignore_case": {"type": "boolean"},
    },
}
# What counts as part of a word for `words`: an identifier's characters.
WORD_EDGE = "A-Za-z0-9_"


def substitutions(pattern, replacement, rules, words, ignore_case, literal, multiline):
    """The (regex, replacement) passes a sub makes, in order."""
    given = sum(
        [
            pattern is not None or replacement is not None,
            rules is not None,
            words is not None,
        ]
    )
    if given != 1:
        raise ToolError("pass pattern and replacement, or rules, or words: one of them")
    if words is not None:
        if not (
            isinstance(words, dict)
            and words
            and all(
                isinstance(k, str) and k and isinstance(v, str)
                for k, v in words.items()
            )
        ):
            raise ToolError("words must map non-empty strings to strings")
        # One pass over every word at once, longest first, so a replacement is
        # never itself replaced again (ci -> gci leaves an existing gci alone).
        keys = sorted(words, key=len, reverse=True)
        rx = re.compile(
            f"(?<![{WORD_EDGE}])(?:{'|'.join(map(re.escape, keys))})(?![{WORD_EDGE}])"
        )
        return [(rx, lambda m: words[m.group(0)], "words")]
    if rules is None:
        if pattern is None or replacement is None:
            raise ToolError("pattern needs a replacement")
        rules = [{"pattern": pattern, "replacement": replacement}]
    passes = []
    for rule in rules:
        plain = rule.get("literal", literal)
        rx = compile_pattern(
            rule["pattern"],
            rule.get("ignore_case", ignore_case),
            plain,
            multiline=multiline,
        )
        repl = rule["replacement"]
        passes.append((rx, (lambda _m, repl=repl: repl) if plain else repl, repl))
    return passes


@tool(
    """
    Search and replace across files, like `sed -i`. Python `re` syntax; a
    replacement takes \\1 and \\g<name>. Three forms: `pattern` with
    `replacement`; `rules`, several {pattern, replacement} applied in order,
    each to the result of the last; or `words`, a map of whole words to their
    new names applied in one pass ({"old_name": "new_name", ...}), so a
    renamed word is never renamed again. A word is bounded by anything that is
    not a letter, digit or underscore. Set dry_run to see the diff first.
    Nothing is written if any file that would change is protected.
    """,
    lines=True,
    pattern=("string", "Regular expression (or plain text with literal).", None),
    replacement=("string", "What each match becomes.", None),
    rules=(
        {"type": "array", "items": SUB_RULE},
        "Several rules, applied in order.",
        None,
    ),
    words=(
        {"type": "object", "additionalProperties": {"type": "string"}},
        "Whole words to rename: {old: new}.",
        None,
    ),
    path=(*PATH, "."),
    glob=GLOB,
    ignore_case=("boolean", "Case-insensitive.", False),
    literal=("boolean", "Treat pattern and replacement as plain text.", False),
    multiline=(
        "boolean",
        "^ and $ match at every line; the pattern may span lines with \\n.",
        False,
    ),
    dry_run=("boolean", "Report the diff without writing.", False),
)
def sub(
    pattern=None,
    replacement=None,
    rules=None,
    words=None,
    path=".",
    glob=None,
    ignore_case=False,
    literal=False,
    multiline=False,
    dry_run=False,
):
    passes = substitutions(
        pattern, replacement, rules, words, ignore_case, literal, multiline
    )
    rules = Rules(config())
    changes = []
    for file, rel in walk(resolve(path, rules), rules, glob):
        text = read_text(file)
        if text is None:
            continue
        after, count = text, 0
        for rx, repl, shown in passes:
            try:
                after, n = rx.subn(repl, after)
            except (re.error, IndexError) as e:
                raise ToolError(f"bad replacement {shown!r}: {e}") from None
            count += n
        if count and after != text:
            changes.append((file, rel, text, after, count))
    if not changes:
        return "no matches"
    locked = [rel for _, rel, *_ in changes if rules.protected(rel)]
    if locked and not dry_run:
        raise ToolError(
            f"protected, read-only to tools: {' '.join(locked)}; nothing written"
        )
    out = []
    for file, rel, before, after, _ in changes:
        if not dry_run:
            write_text(file, after)
        out.append(unified(rel, before, after, context=1))
    total = sum(c[4] for c in changes)
    verb = "would replace" if dry_run else "replaced"
    out.append(
        f"{verb} {total} match{'es' if total != 1 else ''} in {len(changes)} file(s)"
    )
    return "".join(out)


def contents_or_refuse(directory, rules):
    """Every file under `directory`, or a ToolError if one is protected or denied.

    The walk itself skips and denies nothing: a walk that honored the deny
    list would never see the denied files this exists to find.
    """
    inside = [r for _, r in walk(directory, Rules({"deny": [], "skip": []}))]
    locked = [r for r in inside if rules.protected(r) or rules.denied(r)]
    if locked:
        raise ToolError(
            f"{rel_of(directory)} holds protected or denied files: "
            f"{' '.join(locked[:10])}"
        )
    return inside


# Read-only git can still run code: a hook, or fsmonitor.
SAFE_GIT = ["-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}"]


def git_tracked(rel):
    """The files git tracks under `rel`; none if there is no git or no repo."""
    try:
        proc = subprocess.run(
            ["git", *SAFE_GIT, "ls-files", "--", rel],
            cwd=core.ROOT,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    return proc.stdout.decode("utf-8", errors="replace").splitlines()


@tool(
    f"""
    Empty the project's scratch directory, `{DEFAULT_TMP}/` unless the server's
    instructions name another: the place for temporary files, kept out of git and out
    of every sync. Everything inside goes, the directory itself stays. Refused
    if git tracks anything in it, or it holds a protected or denied file. Set
    dry_run to see what it holds first.
    """,
    dry_run=("boolean", "Report what would be removed without removing it.", False),
)
def clean_tmp(dry_run=False):
    cfg = config()
    rules = Rules(cfg)
    target = resolve(tmp_rel(cfg), rules, write=True)
    rel = rel_of(target)
    if target == core.ROOT:
        raise ToolError(
            "the scratch directory is the project root; refusing to empty it"
        )
    if not target.exists():
        return f"{rel}/ does not exist; nothing to clean"
    if not target.is_dir():
        raise ToolError(f"{rel} is a file, not a directory")
    tracked = git_tracked(rel)
    if tracked:
        raise ToolError(
            f"git tracks files in {rel}/, so it is not scratch: "
            f"{' '.join(tracked[:10])}"
        )
    inside = contents_or_refuse(target, rules)
    size = sum((core.ROOT / r).lstat().st_size for r in inside)
    summary = f"{len(inside)} file(s), {human(size)}, from {rel}/"
    if dry_run:
        return "\n".join([*inside[:50], f"would remove {summary}"])
    for entry in target.iterdir():
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    return f"removed {summary}"
