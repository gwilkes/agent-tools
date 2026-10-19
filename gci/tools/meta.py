"""Tools about this server: what it offers, and restarting it in place."""

import json
import os
import subprocess  # nosec B404: argv lists only, never a shell
import sys
import threading

from .. import core
from ..core import VERSION, ToolError, safe_config
from ..runs import running_runs
from ..text import clip
from ..listing import block, listing
from .registry import (
    TOOLS,
    check_names,
    cli_params,
    cli_view,
    describe_tools,
    summary_of,
    tool,
)

# Set by the restart tool once its checks pass; the stdio loop execs after replying.
RESTART = threading.Event()
# How much of a failed import check a refused restart quotes.
CHECK_OUTPUT = 4000
# tools/call requests running now, the restart's own included.
_in_flight = 0
_in_flight_lock = threading.Lock()


def begin_call() -> bool:
    """Count a tools/call in, unless a restart is under way. Whether it may run."""
    global _in_flight
    with _in_flight_lock:
        if RESTART.is_set():
            return False
        _in_flight += 1
        return True


def end_call() -> None:
    global _in_flight
    with _in_flight_lock:
        _in_flight -= 1


@tool(
    """
    List every tool this server offers: its signature, what it does and its
    parameters, plus any switched off for this project. The same tools a client's
    tools/list sees, laid out for reading. Pass `names` for just those, or
    grep: `^\\w` keeps the signatures alone, `tail\\?` the tools that take tail.
    """,
    lines=True,
    positional=("names",),
    names=({"type": "array", "items": {"type": "string"}}, "Only these tools.", None),
    brief=("boolean", "One line per tool: its signature and first sentence.", False),
)
def tools(names=None, brief=False):
    return describe_tools(safe_config(), names, brief)


@cli_view("tools")
def tools_view(names=None, brief=False):
    cfg = safe_config()
    check_names(names)
    blocks, mcp_only, off = [], [], []
    for name, entry in TOOLS.items():
        if names and name not in names:
            continue
        if not entry.available(cfg):
            off.append(name)
        elif entry.cli_skip is None:
            mcp_only.append(name)
        else:
            blocks.append(tool_block(name, entry, brief))
    notes = [f"over MCP only: {', '.join(mcp_only)}"] if mcp_only else []
    notes += [f"switched off for this project: {', '.join(off)}"] if off else []
    if blocks:
        notes.append("`gci WORD --help` gives a word's whole description.")
    return listing(blocks, notes)


def tool_block(name, entry, brief):
    """A tool as its CLI word: the usage, the description's first sentence,
    and a row per param with its default."""
    required = set(entry.schema["inputSchema"]["required"])
    usage = [f"gci {name}"]
    rows = []
    for pname, word, prop in cli_params(entry):
        usage.append(word if pname in required else f"[{word}]")
        # A switch that is off unless given needs no default spelled out.
        shown = "default" in prop and prop["default"] is not False
        default = f" (default {json.dumps(prop['default'])})" if shown else ""
        rows.append((word, prop["description"] + default))
    summary = summary_of(entry.schema) + "."
    return block(" ".join(usage), summary, [] if brief else rows)


@tool("Restart this MCP server in place, to load edits to its own code.", cli=False)
def restart():
    if os.name == "nt":
        raise ToolError(
            "not on Windows: exec there starts a new process, which the client "
            "sees as this server exiting; reconnect it instead"
        )
    with _in_flight_lock:
        others = _in_flight - 1
    if others > 0:
        raise ToolError(
            f"{others} other call(s) still running; restart once they finish"
        )
    # A run lives in a thread of this process, which the exec ends.
    runs = running_runs()
    if runs:
        raise ToolError(
            f"run(s) still going: {', '.join(r.id for r in runs)}; "
            "wait for them or stop them with job"
        )
    # Every module, not just the CLI's: a break in one this process imports
    # lazily would otherwise pass the check and take the server down on exec.
    _, env = core.self_command()
    sources = core.package_sources()
    modules = sorted(
        f"{core.PKG_NAME}." + rel[:-3].replace("/", ".").removesuffix(".__init__")
        for rel in sources
        if rel != "__main__.py"
    )
    try:
        # This interpreter, importing this package's own modules.
        check = subprocess.run(  # nosec B603
            [sys.executable, "-c", f"import {', '.join(modules)}"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=core.ROOT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(
            "the new code did not import within 30s; not restarting"
        ) from None
    if check.returncode != 0:
        raise ToolError(
            "the new code does not start; not restarting:\n"
            + clip(check.stdout + check.stderr, CHECK_OUTPUT)
        )
    new = core.sources_version(sources)
    # Checked again under the lock new calls take, so none starts between this
    # check and the exec that would end it.
    with _in_flight_lock:
        if _in_flight > 1 or running_runs():
            raise ToolError("a call or run started during the check; try again")
        RESTART.set()
    if new == VERSION:
        return f"unchanged ({VERSION}); restarting anyway"
    return f"restarting into {new} (was {VERSION}); the tool list is re-announced after"
