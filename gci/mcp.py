"""`gci mcp`: the tools over newline-delimited JSON-RPC on stdio, and the
in-place restart that lets an edit to the package land without a reconnect."""

import json
import os
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

from . import core, runs
from .core import MAX_OUTPUT, VERSION, ToolError, output_limit, safe_config, tmp_rel
from .progress import CURRENT as PROGRESS
from .progress import Progress
from .runs import stop_runs
from .scripts import describe_scripts
from .text import clip
from .tools import TOOLS
from .tools.meta import RESTART, begin_call, end_call
from .wire import describe_hosts

# Whatever answers through this module is a server, there for the next call.
runs.OUTLIVES_CALLS = True

# What this server speaks when a client names no version; one that names one
# is answered with its own.
PROTOCOL_VERSION = "2025-06-18"
# Across a restart's exec: the bytes read from stdin and not yet handled, and
# the word that the process is a restart rather than a fresh start.
PENDING_ENV = "GCI_MCP_PENDING"
RESTARTED_ENV = "GCI_MCP_RESTARTED"
# How often the stdin loop wakes to see a restart, and how much it reads at once.
RESTART_POLL = 0.5
READ_CHUNK = 1 << 16


def instructions() -> str:
    cfg = safe_config()
    return (
        textwrap.dedent(f"""
        Script and file tools for {core.ROOT}; paths are relative to that root
        and cannot leave it. Run builds and tests through run_script, which
        runs only the project's scripts (the scripts tool lists them, as does
        the end of this note). Pass it `hosts` to run a script on other machines at once
        (this tree is synced to each first; `local` is this one), or `wait` to
        answer after that many seconds and follow the run with job. Read a
        definition by name, or several slices at once, with read, and edit
        files with edit (exact text, line ranges, several files at once). Put
        temporary files under {tmp_rel(cfg)}/. Scripts:
        {{scripts}}
        Hosts:
        {{hosts}}
        """)
        .strip()
        .replace("{scripts}", describe_scripts(cfg))
        .replace("{hosts}", describe_hosts(cfg))
    )


def rpc_error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def result(
    text: str, error: bool = False, limit: int = MAX_OUTPUT, hint: str | None = None
) -> dict:
    """A tool result. Scripts cut their own output with a pointer to their log;
    this cut is the backstop for everything else."""
    return {
        "content": [{"type": "text", "text": clip(text, limit, hint)}],
        "isError": error,
    }


def call_tool(name: str, args: dict, progress: Progress | None = None) -> dict:
    cfg = safe_config()
    entry = TOOLS.get(name)
    if entry is None or not entry.available(cfg):
        return result(f"unknown tool: {name}", True)
    if not isinstance(args, dict):
        return result("arguments must be an object", True)
    # The tool runs in this thread, so it finds its reporter through the context.
    token = PROGRESS.set(progress)
    try:
        text, failed = entry.call(args)
    except ToolError as e:
        return result(str(e), True)
    except OSError as e:
        return result(f"{type(e).__name__}: {e}", True)
    except Exception as e:  # one bad call must not take the server down
        return result(f"internal error in {name}: {type(e).__name__}: {e}", True)
    finally:
        PROGRESS.reset(token)
    narrows = {"grep", "tail"} & set(entry.schema["inputSchema"]["properties"])
    hint = f"pass {' or '.join(sorted(narrows))} to narrow it" if narrows else None
    return result(text, failed, output_limit(cfg), hint)


def handle(msg: object, notify=None) -> dict | None:
    """One JSON-RPC message in, its reply out (None for a notification).
    `notify` sends a message to the client mid-call, for progress."""
    if not isinstance(msg, dict) or "id" not in msg or "method" not in msg:
        return None
    mid, method, params = msg["id"], msg["method"], msg.get("params") or {}
    try:
        if method == "initialize":
            reply = {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": "agent-tools", "version": VERSION},
                "instructions": instructions(),
            }
        elif method == "ping":
            reply = {}
        elif method == "tools/list":
            cfg = safe_config()
            reply = {"tools": [t.schema for t in TOOLS.values() if t.available(cfg)]}
        elif method == "tools/call":
            token = (params.get("_meta") or {}).get("progressToken")
            progress = Progress(token, notify) if token is not None and notify else None
            reply = call_tool(
                params.get("name"), params.get("arguments") or {}, progress
            )
        else:
            return rpc_error(mid, -32601, f"unknown method: {method}")
    except Exception as e:
        return rpc_error(mid, -32603, f"{type(e).__name__}: {e}")
    return {"jsonrpc": "2.0", "id": mid, "result": reply}


def serve_mcp() -> int:
    """Newline-delimited JSON-RPC on stdin/stdout.

    stdout is the protocol, so anything else that prints goes to stderr. Each
    call runs on a thread of its own: a ten-minute build must not hold up a
    grep the client sent beside it.

    stdin is read with os.read into a buffer of our own, never through
    sys.stdin, so that a restart knows every byte it has taken off the pipe:
    the unhandled ones travel to the exec'd process in a file, and the rest are
    still in the pipe, which the exec keeps along with the pid.
    """
    wire, sys.stdout = sys.stdout, sys.stderr
    lock = threading.Lock()
    wake = threading.Event()

    def send(msg):
        with lock:
            wire.write(json.dumps(msg) + "\n")
            wire.flush()

    def answer(msg):
        call = isinstance(msg, dict) and msg.get("method") == "tools/call"
        if call and not begin_call():
            send(rpc_error(msg.get("id"), -32603, "restarting; call again"))
            return
        try:
            reply = handle(msg, send)
        finally:
            if call:
                end_call()
        if reply is not None:
            send(reply)
        if RESTART.is_set() and call and msg.get("params", {}).get("name") == "restart":
            wake.set()

    def exec_restart(pending):
        with lock:
            wire.flush()
            env = {**os.environ, RESTARTED_ENV: "1"}
            if pending:
                fd, path = tempfile.mkstemp(prefix="gci-mcp-", suffix=".pending")
                with os.fdopen(fd, "wb") as f:
                    f.write(pending)
                env[PENDING_ENV] = path
            print("gci mcp: restarting in place", flush=True)
            # orig_argv keeps `-m gci`, which a rebuilt argv from sys.argv would lose.
            # The same interpreter and argv: the restart is this process, again.
            os.execve(
                sys.executable, [sys.executable, *sys.orig_argv[1:]], env
            )  # nosec B606

    pending = b""
    carried = os.environ.pop(PENDING_ENV, None)
    if carried:
        try:
            pending = Path(carried).read_bytes()
            os.unlink(carried)
        except OSError as e:
            # The requests in it are lost; their client is left waiting, so say so.
            print(
                f"gci mcp: lost the requests carried over the restart: {e}", flush=True
            )
    if os.environ.pop(RESTARTED_ENV, None):
        send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    print(f"gci mcp: root {core.ROOT}, version {VERSION}", flush=True)

    fd = sys.stdin.fileno()
    if os.name != "nt":
        import select  # posix only: Windows pipes cannot be selected
    while True:
        if RESTART.is_set():
            # Nothing more is dispatched: what is buffered goes to the new
            # process whole, once the restart's own reply is out.
            wake.wait(timeout=10)
            exec_restart(pending)
        if b"\n" not in pending:
            # Wake twice a second to see a restart; a read would block until the next message.
            if os.name != "nt" and not select.select([fd], [], [], RESTART_POLL)[0]:
                continue
            chunk = os.read(fd, READ_CHUNK)
            if not chunk:
                stop_runs()
                return 0
            pending += chunk
            continue
        line, pending = pending.split(b"\n", 1)
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send(rpc_error(None, -32700, "parse error"))
            continue
        for one in msg if isinstance(msg, list) else [msg]:
            threading.Thread(target=answer, args=(one,), daemon=True).start()
