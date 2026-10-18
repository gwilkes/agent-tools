"""Running processes: streamed, timed out, and killed with their whole tree."""

import itertools
import os
import re
import signal
import subprocess
import threading
import time

from .core import (
    Rules,
    ToolError,
    clip_lines,
    filter_lines,
    log_of,
    output_limit,
    rel_of,
    resolve,
    tmp_rel,
)
from .scripts import run_plan, script_plan

# Where each run's log goes, under the settings' tmp directory.
LOG_DIR = "gci/logs"

# Set by the CLI's Ctrl-C. A step runs in a session of its own, so the
# terminal's SIGINT never reaches it; this is how it hears instead.
INTERRUPTED = threading.Event()


def interrupted():
    return "interrupted" if INTERRUPTED.is_set() else None


def kill_tree(proc):
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def stream_command(cmd, cwd, timeout, emit, stop=None, env=None):
    """Run `cmd`, handing `emit` each line of output as it lands.

    Ends at the command's own exit, its timeout, or the first time `stop()`
    answers a reason; either of the last two kills its whole process tree.
    Returns (exit code, why it was killed or None).
    """
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
        )
    except OSError as e:
        raise ToolError(f"failed to launch {cmd[0]}: {e}") from None

    def pump():
        with proc.stdout:
            for raw in proc.stdout:
                emit(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout if timeout else None
    why = None
    while True:
        try:
            proc.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            pass
        why = stop() if stop else None
        if why is None and deadline is not None and time.monotonic() > deadline:
            why = f"killed after {timeout}s timeout"
        if why is not None:
            kill_tree(proc)
            proc.wait()
            break
    # A grandchild that escaped the kill can hold the pipe open; do not wait on it forever.
    reader.join(timeout=5)
    return proc.returncode, why


def tee(*sinks):
    """One line sink that hands each line to every sink given (None ones skipped)."""
    sinks = [s for s in sinks if s is not None]

    def send(line):
        for sink in sinks:
            sink(line)

    return send


class RunLog:
    """A run's output as it lands, in a file under tmp/ a person can follow
    with `tail -f` or the read tool while the run is still going.

    Line-buffered, so each line is on disk as soon as it is written. If tmp/
    is not somewhere this tree may write, there is no log and the run goes on.
    """

    def __init__(self, cfg, label):
        self.file, self.rel = None, None
        self._lock = threading.Lock()
        try:
            base = resolve(f"{tmp_rel(cfg)}/{LOG_DIR}", Rules(cfg), write=True)
            base.mkdir(parents=True, exist_ok=True)
        except (ToolError, OSError):
            return
        stem = f"{re.sub(r'[^A-Za-z0-9._-]+', '_', label)}-{time.strftime('%Y%m%d-%H%M%S')}"
        for n in itertools.count():
            path = base / (f"{stem}.log" if n == 0 else f"{stem}-{n}.log")
            try:
                self.file = open(path, "x", encoding="utf-8", buffering=1)
            except FileExistsError:
                continue
            except OSError:
                return
            self.rel = rel_of(path)
            return

    def write(self, line):
        if self.file is not None:
            with self._lock:
                self.file.write(line + "\n")

    def close(self):
        if self.file is not None:
            self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def local_script(cfg, name, args, emit, stop=None, watch=None, quiet=None):
    """Run a settings script here, streaming. Its exit code, or KILLED_EXIT.

    `emit` gets what belongs in the result. A gauntlet keeps its members' own
    output out of that (only a failure's tail goes in), and hands it to
    `quiet` instead. Every line, either kind, goes to the run's log and to
    `watch`, which is how a caller shows the run live.
    """
    plan = script_plan(cfg, name, args)
    with RunLog(cfg, name) as log:
        out = tee(log.write, watch, emit)
        hush = tee(log.write, watch, quiet)
        if log.rel:
            out(f"log: {log.rel}")
        return run_plan(plan, out, stop or interrupted, hush)


def report_lines(lines, limit, grep=None, tail=None):
    """A run's result lines through `grep` and `tail`, then cut to `limit`.
    The `log:` line survives both, so the whole of it can still be found."""
    see = log_of(lines)
    pointer = f"log: {see}"
    lines, note = filter_lines(lines, grep, tail, pinned=lambda line: line == pointer)
    if note:
        at = lines.index(pointer) + 1 if see else 0
        lines[at:at] = [note]
    return clip_lines(lines, limit, see=see)


def script_report(cfg, name, args, watch=None, grep=None, tail=None):
    """Run a settings script here to its end. Returns (report, failed), the
    report cut to the settings' output limit with a pointer to the full log."""
    filter_lines([], grep, tail)
    started = time.monotonic()
    lines = []
    code = local_script(cfg, name, args, lines.append, watch=watch)
    head = f"{name}: exit {code} in {time.monotonic() - started:.1f}s"
    body = report_lines(lines, output_limit(cfg) - len(head) - 1, grep, tail)
    return f"{head}\n{body}".rstrip(), code != 0
