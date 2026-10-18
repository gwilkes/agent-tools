"""`gci mcp`: the tools over newline-delimited JSON-RPC on stdio, and the
in-place restart that lets an edit to the package land without a reconnect."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path

from . import core
from .core import (
    CONFIG,
    MAX_OUTPUT,
    VERSION,
    ToolError,
    clip,
    describe_hosts,
    describe_scripts,
    output_limit,
    safe_config,
    tmp_rel,
)
from .procs import stream_command
from .progress import CURRENT as PROGRESS
from .progress import Progress
from .tools import TOOLS, describe_tools, tool

# Imported for what importing does: each module registers its tools, in this order.
from .tools import files as _files
from .tools import data as _data
from .tools import git as _git
from .tools import run as _run

# Echoed back when the client asks for a version this server predates.
PROTOCOL_VERSION = "2025-06-18"
# Across a restart's exec: the bytes read from stdin and not yet handled, and
# the word that the process is a restart rather than a fresh start.
PENDING_ENV = "GCI_MCP_PENDING"
RESTARTED_ENV = "GCI_MCP_RESTARTED"
# Set by the restart tool once its checks pass; serve_mcp execs after replying.
RESTART = threading.Event()
# tools/call requests running now, the restart's own included.
IN_FLIGHT = [0]
IN_FLIGHT_LOCK = threading.Lock()


@tool("""
    Restart this MCP server in place to pick up edits to its own code. The new
    code is started once first and the restart refused if it fails, so a broken
    edit leaves this server running. Refused while other calls are running,
    since their answers would be lost. The tool list is re-announced afterwards.
    """)
def restart():
    if os.name == "nt":
        raise ToolError(
            "not on Windows: exec there starts a new process, which the client "
            "sees as this server exiting; reconnect it instead"
        )
    with IN_FLIGHT_LOCK:
        others = IN_FLIGHT[0] - 1
    if others > 0:
        raise ToolError(
            f"{others} other call(s) still running; restart once they finish"
        )
    # Every module, not just the CLI's: a break in one this process imports
    # lazily would otherwise pass the check and take the server down on exec.
    _, env = core.self_command()
    modules = sorted(
        f"{core.PKG_NAME}." + rel[:-3].replace("/", ".").removesuffix(".__init__")
        for rel in core.package_sources()
        if rel != "__main__.py"
    )
    try:
        check = subprocess.run(
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
            + clip(check.stdout + check.stderr, 4000)
        )
    new = core.sources_version(core.package_sources())
    RESTART.set()
    if new == VERSION:
        return f"unchanged ({VERSION}); restarting anyway"
    return f"restarting into {new} (was {VERSION}); the tool list is re-announced after"


@tool(
    """
    Run the tools' own self-test: every tool against a scratch tree, a real
    `gci serve` over loopback, and a real MCP server restarted mid-session. Run
    it after editing this package, before `restart`, to check the edit. It runs
    in a fresh process, so it tests the code on disk rather than the code this
    server loaded, and touches nothing outside its scratch directories.
    """,
    timeout=("integer", "Seconds before it is killed.", 300),
)
def self_test(timeout=300):
    argv, env = core.self_command("self-test")
    started = time.monotonic()
    lines = []
    live = PROGRESS.get()

    def emit(line):
        lines.append(line)
        if live:
            live(line)

    code, why = stream_command(argv, core.ROOT, timeout, emit, env=env)
    head = f"self-test: {why or f'exit {code}'} in {time.monotonic() - started:.1f}s"
    return "\n".join([head, *lines]).rstrip(), why is not None or code != 0


@tool(
    """
    List every tool this server offers: its signature, what it does and its
    parameters, plus any the settings switch off. The same tools a client's
    tools/list sees, laid out for reading. Pass `names` for just those.
    """,
    names=({"type": "array", "items": {"type": "string"}}, "Only these tools.", None),
    brief=("boolean", "One line per tool: its signature and first sentence.", False),
)
def list_tools(names=None, brief=False):
    return describe_tools(safe_config(), names, brief)


def instructions():
    cfg = safe_config()
    return (
        textwrap.dedent(f"""
        File, search and process tools for {core.ROOT}, standing in for a shell.
        Paths are relative to that root and cannot leave it. Edit files with edit
        (exact replacement) or sub (regex across files), search with grep, list
        with ls, and run builds, tests and git through run_script, which runs only
        the scripts {CONFIG} defines. Pass run_script `hosts` to run a script on
        other machines at once (this tree is synced to each first); `local` is
        this one. Put temporary files under {tmp_rel(cfg)}/ and empty it with
        clean_tmp when done. Scripts now:
        {{scripts}}
        Hosts:
        {{hosts}}
        """)
        .strip()
        .replace("{scripts}", describe_scripts(cfg))
        .replace("{hosts}", describe_hosts(cfg))
    )


def result(text, error=False, limit=MAX_OUTPUT):
    """A tool result. Scripts cut their own output with a pointer to their log;
    this cut is the backstop for everything else."""
    return {"content": [{"type": "text", "text": clip(text, limit)}], "isError": error}


def call_tool(name, args, progress=None):
    cfg = safe_config()
    entry = TOOLS.get(name)
    if entry is None or not entry.available(cfg):
        return result(f"unknown tool: {name}", True)
    if not isinstance(args, dict):
        return result("arguments must be an object", True)
    # The tool runs in this thread, so it finds its reporter through the context.
    token = PROGRESS.set(progress)
    try:
        out = entry.fn(**args)
    except ToolError as e:
        return result(str(e), True)
    except TypeError as e:
        return result(f"bad arguments for {name}: {e}", True)
    except OSError as e:
        return result(f"{type(e).__name__}: {e}", True)
    except Exception as e:  # one bad call must not take the server down
        return result(f"internal error in {name}: {type(e).__name__}: {e}", True)
    finally:
        PROGRESS.reset(token)
    text, failed = out if isinstance(out, tuple) else (out, False)
    return result(text, failed, output_limit(cfg))


def handle(msg, notify=None):
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
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"},
        }
    return {"jsonrpc": "2.0", "id": mid, "result": reply}


def serve_mcp():
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
        if call:
            with IN_FLIGHT_LOCK:
                IN_FLIGHT[0] += 1
        try:
            reply = handle(msg, send)
        finally:
            if call:
                with IN_FLIGHT_LOCK:
                    IN_FLIGHT[0] -= 1
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
            os.execve(sys.executable, [sys.executable, *sys.orig_argv[1:]], env)

    pending = b""
    carried = os.environ.pop(PENDING_ENV, None)
    if carried:
        try:
            pending = Path(carried).read_bytes()
            os.unlink(carried)
        except OSError:
            pass
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
            if os.name != "nt" and not select.select([fd], [], [], 0.5)[0]:
                continue
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                return 0
            pending += chunk
            continue
        line, pending = pending.split(b"\n", 1)
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }
            )
            continue
        for one in msg if isinstance(msg, list) else [msg]:
            threading.Thread(target=answer, args=(one,), daemon=True).start()
