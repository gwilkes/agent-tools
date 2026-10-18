"""Reading, editing, searching and moving files: the part of a shell an agent
uses most, each with the root and the deny/protect lists enforced."""

import difflib
import re
import shutil

from .. import core
from ..core import (
    DEFAULT_TMP,
    MAX_LINE,
    Rules,
    ToolError,
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
from . import GLOB, PATH, tool


@tool(
    """
    Read a text file, numbered like `cat -n`. Long files come a page at a time:
    the header says which lines these are and the offset for the next page. A
    negative offset counts from the end, like tail: -50 is the last 50 lines.
    """,
    path=PATH,
    offset=("integer", "First line to show, 1-based; negative counts from the end.", 1),
    limit=("integer", "How many lines to show.", 2000),
)
def read(path, offset=1, limit=2000):
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


@tool(
    """
    Create or replace a file with `content`, making its directories. Prefer edit
    for a change to an existing file: it shows what changed and cannot clobber
    the parts you did not mean to touch.
    """,
    path=PATH,
    content=("string", "The whole new file."),
)
def write(path, content):
    rules = Rules(config())
    target = resolve(path, rules, write=True)
    if target.is_dir():
        raise ToolError(f"{rel_of(target)} is a directory")
    existed = target.exists()
    write_text(target, content)
    return f"{'replaced' if existed else 'created'} {rel_of(target)} ({human(len(content.encode()))})"


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
                    f"{tag}old is empty; use write to replace the whole file"
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
            raise ToolError(f"no such file: {rel}; use write to create one")
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


@tool(
    """
    Search file contents with a Python regex, like grep -rn. Walks the tree
    skipping binaries, denied paths and the skip list (.git, node_modules...).
    mode `content` shows `path:line:text` (context lines use `-`), `files` only
    the paths, `count` matches per file.
    """,
    pattern=("string", "Regular expression (or plain text with literal)."),
    path=(*PATH, "."),
    glob=GLOB,
    mode=(
        {"type": "string", "enum": ["content", "files", "count"]},
        "What to report.",
        "content",
    ),
    ignore_case=("boolean", "Case-insensitive.", False),
    literal=("boolean", "Treat the pattern as plain text.", False),
    word=("boolean", "Match whole words only.", False),
    multiline=(
        "boolean",
        "Search whole files, so a pattern can span lines with \\n; "
        "^ and $ still match at each line.",
        False,
    ),
    context=("integer", "Lines of context around each match (content mode).", 0),
    max_results=("integer", "Stop after this many matches (or files).", 200),
)
def grep(
    pattern,
    path=".",
    glob=None,
    mode="content",
    ignore_case=False,
    literal=False,
    word=False,
    multiline=False,
    context=0,
    max_results=200,
):
    rules = Rules(config())
    rx = compile_pattern(pattern, ignore_case, literal, word, multiline)
    out, shown, more = [], 0, 0
    for file, rel in walk(resolve(path, rules), rules, glob):
        text = read_text(file)
        if text is None:
            continue
        lines = text.splitlines()
        if multiline:
            hits = sorted({text.count("\n", 0, m.start()) for m in rx.finditer(text)})
        else:
            hits = [i for i, line in enumerate(lines) if rx.search(line)]
        if not hits:
            continue
        if mode == "files":
            if shown < max_results:
                out.append(rel)
                shown += 1
            else:
                more += 1
            continue
        if mode == "count":
            out.append(f"{rel}:{len(hits)}")
            continue
        keep = hits[: max(max_results - shown, 0)]
        more += len(hits) - len(keep)
        shown += len(keep)
        matched, last = set(keep), -1
        for i in keep:
            lo, hi = max(i - context, 0), min(i + context, len(lines) - 1)
            if context and last >= 0 and lo > last + 1:
                out.append("--")
            for j in range(max(lo, last + 1), hi + 1):
                sep = ":" if j in matched else "-"
                out.append(f"{rel}{sep}{j + 1}{sep}{lines[j][:MAX_LINE]}")
            last = max(hi, last)
    if not out:
        return "no matches"
    if more:
        out.append(f"... {more} more not shown; narrow the search or raise max_results")
    return "\n".join(out)


@tool(
    """
    List a directory, like ls/find. Without a glob: its entries to `depth`
    levels, directories marked with `/`, files with their sizes. With a glob:
    every matching file under it, one path per line.
    """,
    path=(*PATH, "."),
    glob=GLOB,
    depth=("integer", "Levels to descend without a glob.", 1),
    max_results=("integer", "Stop after this many entries.", 500),
)
def ls(path=".", glob=None, depth=1, max_results=500):
    rules = Rules(config())
    base = resolve(path, rules)
    if not base.exists():
        raise ToolError(f"no such path: {path}")
    out = []
    if glob or base.is_file():
        for file, rel in walk(base, rules, glob):
            out.append(f"{rel}  {human(file.stat().st_size)}")
            if len(out) > max_results:
                break
    else:

        def visit(directory, level):
            try:
                entries = sorted(
                    directory.iterdir(), key=lambda p: (not p.is_dir(), p.name)
                )
            except OSError as e:
                out.append(f"{'  ' * level}[{e.strerror}]")
                return
            for entry in entries:
                if len(out) > max_results:
                    return
                if entry.name in rules.skip or rules.denied(rel_of(entry)):
                    continue
                pad = "  " * level
                if entry.is_dir():
                    out.append(f"{pad}{entry.name}/")
                    if level + 1 < depth:
                        visit(entry, level + 1)
                else:
                    out.append(f"{pad}{entry.name}  {human(entry.stat().st_size)}")

        visit(base, 0)
    if len(out) > max_results:
        out[max_results:] = ["... more not shown; raise max_results"]
    return "\n".join(out) or "(empty)"


@tool(
    """
    Move or rename a file or directory, like mv; with copy, copy it instead,
    like cp -r. A destination that is an existing directory receives the source
    inside it.
    """,
    src=("string", "What to move, relative to the project root."),
    dst=("string", "Where it goes."),
    copy=("boolean", "Copy rather than move.", False),
    overwrite=("boolean", "Replace a file already at the destination.", False),
)
def move(src, dst, copy=False, overwrite=False):
    rules = Rules(config())
    source = resolve(src, rules, write=not copy)
    if not source.exists():
        raise ToolError(f"no such path: {src}")
    dest = resolve(dst, rules, write=True)
    if dest.is_dir():
        dest = resolve(rel_of(dest / source.name), rules, write=True)
    if dest == source or dest.is_relative_to(source):
        raise ToolError(
            f"cannot {'copy' if copy else 'move'} {rel_of(source)} into itself"
        )
    if dest.exists():
        if not overwrite or dest.is_dir():
            raise ToolError(
                f"{rel_of(dest)} exists" + ("" if dest.is_dir() else "; set overwrite")
            )
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not copy:
        shutil.move(str(source), str(dest))
    elif source.is_dir():
        shutil.copytree(source, dest, symlinks=True)
    else:
        shutil.copy2(source, dest)
    return f"{'copied' if copy else 'moved'} {rel_of(source)} -> {rel_of(dest)}"


@tool(
    """
    Rename many files at once by a regex over their names, the loop of `mv`s a
    shell would write. Each file's name (not its directory) under `path` is
    matched; a match is renamed in place to the name the replacement makes.
    Refused as a whole if two files would land on one name, a name is taken,
    or any file involved is protected. Set dry_run to see the list first.
    """,
    pattern=("string", "Regular expression over file names, e.g. `^(.*)\\.jpeg$`."),
    replacement=("string", "The new name; takes \\1 and \\g<name>, e.g. `\\1.jpg`."),
    path=(*PATH, "."),
    glob=GLOB,
    dry_run=("boolean", "Report the renames without making them.", False),
)
def rename(pattern, replacement, path=".", glob=None, dry_run=False):
    rules = Rules(config())
    rx = compile_pattern(pattern)
    plan = []
    for file, rel in walk(resolve(path, rules), rules, glob):
        if not rx.search(file.name):
            continue
        try:
            name = rx.sub(replacement, file.name)
        except (re.error, IndexError) as e:
            raise ToolError(f"bad replacement {replacement!r}: {e}") from None
        if name == file.name:
            continue
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ToolError(
                f"{rel} would be renamed to {name!r}, which is not a file name"
            )
        plan.append((file, file.with_name(name)))
    if not plan:
        return "no names match"
    sources = {src for src, _ in plan}
    seen = {}
    for src, dst in plan:
        if dst in seen:
            raise ToolError(
                f"{rel_of(seen[dst])} and {rel_of(src)} would both become "
                f"{rel_of(dst)}; nothing renamed"
            )
        seen[dst] = src
        # A name freed by another rename in the same batch is not taken.
        if dst.exists() and dst not in sources:
            raise ToolError(f"{rel_of(dst)} exists; nothing renamed")
        for end in (src, dst):
            resolve(rel_of(end), rules, write=True)
    lines = [f"{rel_of(src)} -> {dst.name}" for src, dst in plan]
    if dry_run:
        return "\n".join([*lines, f"would rename {len(plan)} file(s)"])
    # Through temporary names, so a swap or a chain (a->b, b->c) cannot clobber.
    staged = []
    for i, (src, dst) in enumerate(plan):
        temp = src.with_name(f".gci-rename-{i}-{src.name}")
        src.rename(temp)
        staged.append((temp, dst))
    for temp, dst in staged:
        temp.rename(dst)
    return "\n".join([*lines, f"renamed {len(plan)} file(s)"])


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


@tool(
    """
    Delete a file, or a directory with recursive. Permanent: there is no trash.
    """,
    path=PATH,
    recursive=("boolean", "Delete a non-empty directory and everything in it.", False),
)
def delete(path, recursive=False):
    rules = Rules(config())
    target = resolve(path, rules, write=True)
    rel = rel_of(target)
    if target == core.ROOT:
        raise ToolError("refusing to delete the project root")
    if target.is_symlink() or target.is_file():
        target.unlink()
        return f"deleted {rel}"
    if not target.is_dir():
        raise ToolError(f"no such path: {rel}")
    inside = contents_or_refuse(target, rules)
    if any(target.iterdir()):
        if not recursive:
            raise ToolError(f"{rel} is not empty; set recursive")
        shutil.rmtree(target)
        return f"deleted {rel} ({len(inside)} file(s))"
    target.rmdir()
    return f"deleted {rel}"


@tool(
    f"""
    Empty the project's scratch directory, `{DEFAULT_TMP}/` unless the settings'
    `tmp` names another: the place for temporary files, kept out of git and out
    of every sync. Everything inside goes, the directory itself stays. Refused
    if git tracks anything in it, or it holds a protected or denied file. Set
    dry_run to see what it holds first.
    """,
    dry_run=("boolean", "Report what would be removed without removing it.", False),
)
def clean_tmp(dry_run=False):
    # Here rather than at the top, so the git tools register after these.
    from .git import run_git

    cfg = config()
    rules = Rules(cfg)
    target = resolve(tmp_rel(cfg), rules, write=True)
    rel = rel_of(target)
    if target == core.ROOT:
        raise ToolError("the settings' tmp is the project root; refusing to empty it")
    if not target.exists():
        return f"{rel}/ does not exist; nothing to clean"
    if not target.is_dir():
        raise ToolError(f"{rel} is a file, not a directory")
    try:
        tracked = run_git("ls-files", "--", rel).splitlines()
    except ToolError:
        tracked = []  # no git, or not a repo: nothing can be tracked
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


@tool("Make a directory and any missing parents, like mkdir -p.", path=PATH)
def mkdir(path):
    rules = Rules(config())
    target = resolve(path, rules, write=True)
    if target.exists() and not target.is_dir():
        raise ToolError(f"{rel_of(target)} exists and is a file")
    target.mkdir(parents=True, exist_ok=True)
    return f"{rel_of(target)}/"


@tool(
    """
    Unified diff of two files, or of a file against `content` (what it would
    become). Empty when they match.
    """,
    a=("string", "The first file."),
    b=("string", "The second file.", None),
    content=("string", "Text to compare `a` against, instead of b.", None),
    context=("integer", "Lines of context.", 3),
)
def diff(a, b=None, content=None, context=3):
    if (b is None) == (content is None):
        raise ToolError("pass exactly one of b or content")
    rules = Rules(config())
    left = resolve(a, rules)
    before = read_text(left) if left.is_file() else ""
    if before is None:
        raise ToolError(f"{rel_of(left)} is binary")
    if content is None:
        right = resolve(b, rules)
        after = read_text(right) if right.is_file() else ""
        if after is None:
            raise ToolError(f"{rel_of(right)} is binary")
    else:
        after = content
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=a,
        tofile=b or f"{a} (content)",
        n=context,
    )
    return (
        "".join(line if line.endswith("\n") else line + "\n" for line in lines)
        or "identical"
    )
