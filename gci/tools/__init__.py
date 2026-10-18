"""The tool registry. A module registers its tools with @tool on import; the
MCP server lists and calls whatever is registered by the time it answers."""

import json
import textwrap

from ..core import CONFIG, ToolError

TOOLS = {}


class Tool:
    def __init__(self, fn, schema, enabled):
        self.fn, self.schema, self.enabled = fn, schema, enabled

    def available(self, cfg):
        return self.enabled is None or self.enabled(cfg)


def tool(description, enabled=None, **params):
    """Register a tool. Each param is (schema, description) when required, or
    (schema, description, default) when not; a schema is a type name or a dict.
    `enabled(cfg)`, if given, decides per call whether the tool is listed."""

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
        )
        return fn

    return wrap


def signature(schema):
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


def type_word(prop):
    if "enum" in prop:
        return "|".join(prop["enum"])
    if prop.get("type") == "array":
        return f"{prop.get('items', {}).get('type', 'object')}[]"
    return prop.get("type", "any")


def describe_tools(cfg, names=None, brief=False):
    """The registered tools laid out for a person, or a model, to read.

    Each tool is its signature, then (unless `brief`) its description and one
    line per parameter. Tools the settings switch off are named at the end
    rather than hidden, so a missing tool is never a mystery.
    """
    unknown = sorted(set(names or []) - set(TOOLS))
    if unknown:
        raise ToolError(
            f"no such tool: {', '.join(unknown)}; known: {', '.join(TOOLS)}"
        )
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
                f"    - {pname} ({type_word(prop)}{need}): {prop['description']}"
            )
        blocks.append("\n".join(lines))
    if off:
        blocks.append(f"switched off by {CONFIG}: {', '.join(off)}")
    return "\n\n".join(blocks)


PATH = ("string", "File or directory, relative to the project root.")
GLOB = (
    "string",
    "Only files matching this glob: `*.py` matches a name at any depth, "
    "`src/**` everything under src/.",
    None,
)
