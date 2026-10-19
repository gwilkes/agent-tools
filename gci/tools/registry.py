"""The tool registry: @tool, the argument checks every call goes through, and
the parameters tools share. The package imports every tool module, so TOOLS
is whole wherever it is read."""

import json
import re
import textwrap

from ..core import ToolError
from ..text import filter_text

TOOLS = {}


# A schema type name, as the Python type an argument must be.
TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}


def type_problem(name: str, value: object, prop: dict) -> str | None:
    want = TYPES.get(prop.get("type"))
    if want and (
        not isinstance(value, want) or (want is int and isinstance(value, bool))
    ):
        return f"{name} must be {prop['type']}, not {type(value).__name__}"
    return None


def object_problems(where: str, obj: dict, spec: dict) -> list[str]:
    """What is wrong with an object against its schema: keys it may not
    have, keys it lacks, and each value in turn."""
    props = spec.get("properties", {})
    label = f"{where} " if where else ""
    unknown = (
        sorted(set(obj) - set(props))
        if spec.get("additionalProperties") is False
        else []
    )
    missing = [key for key in spec.get("required", []) if key not in obj]
    problems = [f"{label}unknown: {', '.join(unknown)}"] if unknown else []
    problems += [f"{label}missing: {', '.join(missing)}"] if missing else []
    for key, value in obj.items():
        if key in props and value is not None:
            name = f"{where}.{key}" if where else key
            problems += value_problems(name, value, props[key])
    return problems


def value_problems(name: str, value: object, prop: dict) -> list[str]:
    problem = type_problem(name, value, prop)
    if problem:
        return [problem]
    problems = []
    if "enum" in prop and value not in prop["enum"]:
        problems.append(f"{name} must be one of {', '.join(prop['enum'])}")
    items = prop.get("items")
    if isinstance(value, list) and items:
        if "properties" in items:
            for i, item in enumerate(value):
                problems += value_problems(f"{name}[{i}]", item, items)
        elif any(type_problem(name, v, items) for v in value):
            problems.append(f"{name} must hold {items['type']}s")
        elif "enum" in items and any(v not in items["enum"] for v in value):
            problems.append(f"{name} may hold only {', '.join(items['enum'])}")
    if isinstance(value, dict) and "properties" in prop:
        problems += object_problems(name, value, prop)
    return problems


def check_arguments(schema: dict, args: dict) -> None:
    """A ToolError for any argument the schema does not allow: unknown, missing,
    or of the wrong type, down into the objects an array holds. JSON has no
    int/bool split, so true is not an integer."""
    problems = object_problems("", args, schema["inputSchema"])
    if problems:
        raise ToolError(f"bad arguments for {schema['name']}: {'; '.join(problems)}")


class Tool:
    """A registered tool: its schema, and `call`, which checks the arguments
    against it and always answers (text, failed)."""

    def __init__(self, fn, schema, enabled, lines, cli_skip, positional):
        self.fn, self.schema, self.enabled, self.lines = fn, schema, enabled, lines
        # The params the CLI leaves out, or None for a tool it leaves out.
        self.cli_skip, self.positional = cli_skip, positional
        # What the CLI word prints instead of fn's answer, if set by @cli_view.
        self.cli_view = None

    def available(self, cfg):
        return self.enabled is None or self.enabled(cfg)

    def call(self, args, cli: bool = False):
        """The tool's answer to `args`, or its CLI view's when `cli`."""
        check_arguments(self.schema, args)
        if self.lines:
            args = dict(args)
            grep, tail = args.pop("grep", None), args.pop("tail", None)
        view = cli and self.cli_view is not None
        out = (self.cli_view if view else self.fn)(**args)
        text, failed = out if isinstance(out, tuple) else (out, False)
        if self.lines and view:
            from ..listing import filter_blocks

            text = filter_blocks(text, grep, tail)
        elif self.lines:
            text = filter_text(text, grep, tail)
        return text, failed


GREP = (
    "string",
    "Only the lines of the answer matching this Python regex, like `| grep -E`; "
    "`(?i)` at its start ignores case.",
    None,
)
TAIL = (
    "integer",
    "Only the last N lines of the answer (after grep), like `| tail -n N`.",
    None,
)


def tool(
    description: str,
    enabled=None,
    lines: bool = False,
    cli: bool | tuple = True,
    positional: tuple = (),
    **params,
):
    """Register a tool. Each param is (schema, description) when required, or
    (schema, description, default) when not; a schema is a type name or a dict.
    `enabled(cfg)`, if given, decides per call whether the tool is listed.

    The CLI gives every tool a word of its own, with a flag per param and
    `positional` taken in order without one. `cli` False leaves the tool out
    of the CLI, and a tuple names the params it leaves out: the ones that only
    mean something to a server that outlives the call.

    A tool answers with its text, or (text, failed) when the answer reports a
    failure without being a refusal (a script that exited non-zero); a
    refusal raises ToolError. `lines` gives the tool `grep` and `tail`,
    applied to whatever it answers. A tool whose answer is already cut (from
    the head, or around a log pointer) takes them as params of its own
    instead, to filter before the cut. The function itself is returned
    unwrapped, for the CLI to call."""
    if lines:
        params = {**params, "grep": GREP, "tail": TAIL}

    def wrap(fn):
        props, required = {}, []
        for name, (schema, desc, *default) in params.items():
            schema = {"type": schema} if isinstance(schema, str) else dict(schema)
            schema["description"] = desc
            if not default:
                required.append(name)
            elif default[0] is not None:
                schema["default"] = default[0]
            props[name] = schema
        TOOLS[fn.__name__] = Tool(
            fn,
            {
                "name": fn.__name__,
                "description": textwrap.dedent(description).strip(),
                "inputSchema": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                    "additionalProperties": False,
                },
            },
            enabled,
            lines,
            {True: (), False: None}.get(cli, cli),
            positional,
        )
        return fn

    return wrap


def cli_view(name: str):
    """Register what `gci NAME` prints in place of tool NAME's own answer:
    the same arguments, laid out for a terminal rather than a model."""

    def wrap(fn):
        TOOLS[name].cli_view = fn
        return fn

    return wrap


def summary_of(schema: dict) -> str:
    """A tool's description cut to its first clause, for a one-line listing."""
    first = schema["description"].split("\n\n")[0].replace("\n", " ")
    return re.split(r"\. |: |; ", first)[0].rstrip(".")


def is_word_list(prop: dict) -> bool:
    items = prop.get("items", {})
    return (
        prop.get("type") == "array"
        and items.get("type") == "string"
        and "properties" not in items
    )


def cli_flag(name: str) -> str:
    return f"--{name.replace('_', '-')}"


def cli_metavar(name: str, prop: dict) -> str | None:
    """What a param's value is called on the command line; None for a switch."""
    kind = prop.get("type")
    if kind == "boolean":
        return None
    if kind == "integer":
        return "N"
    if kind not in ("string", "array") or (kind == "array" and not is_word_list(prop)):
        return "JSON"
    return name.upper()


def cli_params(entry: Tool) -> list[tuple[str, str, dict]]:
    """A tool's params as its CLI word takes them, positionals first:
    (param name, how it is written, its schema)."""
    props = entry.schema["inputSchema"]["properties"]
    out = []
    for name in entry.positional:
        word = name.upper() + ("..." if is_word_list(props[name]) else "")
        out.append((name, word, props[name]))
    for name, prop in props.items():
        if name in entry.positional or name in entry.cli_skip:
            continue
        meta = cli_metavar(name, prop)
        many = "..." if is_word_list(prop) else ""
        if meta is None and prop.get("default") is not False:
            # A switch that may be on already is worth writing off.
            out.append((name, f"--[no-]{cli_flag(name)[2:]}", prop))
            continue
        out.append(
            (name, f"{cli_flag(name)} {meta}{many}" if meta else cli_flag(name), prop)
        )
    return out


def signature(schema: dict) -> str:
    """`name(required, optional=default, other?)`, from a tool's input schema."""
    spec = schema["inputSchema"]
    required = set(spec["required"])
    parts = []
    for name, prop in spec["properties"].items():
        if name in required:
            parts.append(name)
        elif "default" in prop:
            parts.append(f"{name}={json.dumps(prop['default'])}")
        else:
            parts.append(f"{name}?")
    return f"{schema['name']}({', '.join(parts)})"


def type_label(prop: dict) -> str:
    if "enum" in prop:
        return "|".join(prop["enum"])
    if prop.get("type") == "array":
        return f"{prop.get('items', {}).get('type', 'object')}[]"
    return prop.get("type", "any")


def check_names(names: list[str] | None) -> None:
    unknown = sorted(set(names or []) - set(TOOLS))
    if unknown:
        raise ToolError(
            f"no such tool: {', '.join(unknown)}; known: {', '.join(TOOLS)}"
        )


def describe_tools(
    cfg: dict, names: list[str] | None = None, brief: bool = False
) -> str:
    """The registered tools laid out for a person, or a model, to read.

    Each tool is its signature, then (unless `brief`) its description and one
    line per parameter. Tools switched off for this project are named at the end
    rather than hidden, so a missing tool is never a mystery.
    """
    check_names(names)
    blocks, off = [], []
    for name, entry in TOOLS.items():
        if names and name not in names:
            continue
        if not entry.available(cfg):
            off.append(name)
            continue
        schema = entry.schema
        if brief:
            first = (
                schema["description"].split("\n\n")[0].replace("\n", " ").split(". ")[0]
            )
            blocks.append(f"{signature(schema)}\n    {first.rstrip('.')}.")
            continue
        spec = schema["inputSchema"]
        lines = [signature(schema)]
        lines += [
            f"    {line}" if line else "" for line in schema["description"].splitlines()
        ]
        for pname, prop in spec["properties"].items():
            need = ", required" if pname in spec["required"] else ""
            lines.append(
                f"    - {pname} ({type_label(prop)}{need}): {prop['description']}"
            )
        blocks.append("\n".join(lines))
    if off:
        blocks.append(f"switched off for this project: {', '.join(off)}")
    return "\n\n".join(blocks)


FILE = ("string", "The file, relative to the project root.")
