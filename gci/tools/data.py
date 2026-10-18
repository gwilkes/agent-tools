"""Structured files: what `jq`, or a three-line Python script, is usually for."""

import json as jsonlib
import re

from ..core import (
    Rules,
    ToolError,
    config,
    read_text,
    rel_of,
    resolve,
    unified,
    write_text,
)
from . import FILE, tool

# One step of a query: `.name` (or a bare first name), `[3]`, or `["any key"]`.
STEP = re.compile(r'\.?([^.\[\]"]+)|\[(-?\d+)\]|\["((?:[^"\\]|\\.)*)"\]')


def parse_query(query):
    """`scripts["self-test"].command[0]` as ["scripts", "self-test", "command", 0]."""
    steps, pos = [], 0
    while pos < len(query):
        found = STEP.match(query, pos)
        if not found or found.end() == pos:
            raise ToolError(
                f"bad query {query!r} at {query[pos:]!r}: use a.b, a[0] or "
                'a["key with dots"]'
            )
        name, index, quoted = found.groups()
        if index is not None:
            steps.append(int(index))
        elif quoted is not None:
            steps.append(jsonlib.loads(f'"{quoted}"'))
        else:
            steps.append(name)
        pos = found.end()
    return steps


def kind(value):
    return {
        dict: "object",
        list: "array",
        str: "string",
        bool: "boolean",
        type(None): "null",
    }.get(type(value), "number")


def key_in(container, step, where):
    """`step` as the key `container` takes, or a ToolError saying why it cannot."""
    if isinstance(container, list):
        if isinstance(step, str) and re.fullmatch(r"-?\d+", step):
            step = int(step)
        if not isinstance(step, int):
            raise ToolError(
                f"{where or 'the document'} is an array; index it with [n], not {step!r}"
            )
        return step
    if isinstance(container, dict):
        return str(step)
    raise ToolError(
        f"{where or 'the document'} is a {kind(container)}; there is nothing inside it"
    )


def label(steps):
    out = ""
    for step in steps:
        out += (
            f"[{step}]"
            if isinstance(step, int)
            else (
                f".{step}"
                if re.fullmatch(r"[A-Za-z_][\w-]*", step)
                else f"[{jsonlib.dumps(step)}]"
            )
        )
    return out.lstrip(".")


def lookup(doc, steps):
    value = doc
    for i, step in enumerate(steps):
        where = label(steps[:i])
        key = key_in(value, step, where)
        if isinstance(value, list) and not -len(value) <= key < len(value):
            raise ToolError(
                f"{label(steps[:i + 1])}: out of range, {where or 'the document'} "
                f"has {len(value)} item(s)"
            )
        if isinstance(value, dict) and key not in value:
            known = ", ".join(map(repr, list(value)[:20])) or "none"
            raise ToolError(
                f"no key {key!r} in {where or 'the document'}; its keys: {known}"
            )
        value = value[key]
    return value


def indent_of(text):
    """The indent a file already uses, so a rewrite keeps its look; 2 if it has none."""
    found = re.search(r"\n([ \t]+)\S", text)
    return found.group(1) if found else 2


@tool(
    """
    Query or edit a JSON file, like jq. `query` is a path into it: `a.b`,
    `a[0]`, `a[-1]`, `a["key.with.dots"]`; none means the whole document.
    Without `value` or `delete`, answers with what is there (`keys` for just
    the keys of an object, or the length of an array). With `value` (JSON
    text: `"str"`, `3`, `{"k": [1]}`), sets it there, making missing objects
    on the way and appending at an index one past an array's end. With
    `delete`, removes it. An edit rewrites the file in standard layout, keeping
    its indent, and answers with the diff.
    """,
    lines=True,
    path=FILE,
    query=("string", "Where in the document; omit for the whole of it.", None),
    value=("string", "JSON text to set at the query.", None),
    delete=("boolean", "Remove what the query names.", False),
    keys=("boolean", "Answer with an object's keys or an array's length only.", False),
)
def json(path, query=None, value=None, delete=False, keys=False):
    if value is not None and delete:
        raise ToolError("pass value or delete, not both")
    editing = value is not None or delete
    rules = Rules(config())
    target = resolve(path, rules, write=editing)
    rel = rel_of(target)
    if not target.is_file():
        raise ToolError(f"no such file: {rel}")
    text = read_text(target)
    if text is None:
        raise ToolError(f"{rel} is binary")
    try:
        doc = jsonlib.loads(text)
    except ValueError as e:
        raise ToolError(f"{rel} is not JSON: {e}") from None
    steps = parse_query(query or "")

    if not editing:
        found = lookup(doc, steps)
        if keys:
            if isinstance(found, dict):
                return "\n".join(found) or "(no keys)"
            if isinstance(found, list):
                return f"{len(found)} item(s)"
            raise ToolError(
                f"{label(steps) or 'the document'} is a {kind(found)}, with no keys"
            )
        return jsonlib.dumps(found, indent=2, ensure_ascii=False)

    if not steps:
        if delete:
            raise ToolError(
                "refusing to delete the whole document; use delete on the file"
            )
        doc = parse_value(value)
    else:
        parent = doc
        for i, step in enumerate(steps[:-1]):
            key = key_in(parent, step, label(steps[:i]))
            if isinstance(parent, dict) and key not in parent:
                if delete:
                    raise ToolError(
                        f"no key {key!r} in {label(steps[:i]) or 'the document'}"
                    )
                parent[key] = {}
            parent = lookup(parent, [key])
        where = label(steps[:-1])
        key = key_in(parent, steps[-1], where)
        if delete:
            lookup(parent, [key])
            del parent[key]
        elif isinstance(parent, list) and key == len(parent):
            parent.append(parse_value(value))
        else:
            if isinstance(parent, list):
                lookup(parent, [key])
            parent[key] = parse_value(value)
    after = jsonlib.dumps(doc, indent=indent_of(text), ensure_ascii=False)
    if text.endswith("\n"):
        after += "\n"
    if after == text:
        return f"{rel}: unchanged"
    write_text(target, after)
    return unified(rel, text, after)


def parse_value(value):
    try:
        return jsonlib.loads(value)
    except ValueError as e:
        raise ToolError(
            f"value is not JSON text ({e}); a string needs its quotes: "
            f"{jsonlib.dumps(value)}"
        ) from None
