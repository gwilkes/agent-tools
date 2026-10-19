"""Running a script's plan: its processes streamed, timed out and killed with
their whole tree, a gauntlet's members in turn, and a forbid check over the tree."""

import os
import shlex
import shutil
import signal
import subprocess  # nosec B404: argv lists only, never a shell
import threading
import time
from pathlib import Path

from . import core
from .core import Rules, ToolError, any_match
from .runlog import Reason, Sink, run_output
from .scripts import GAUNTLET, Plan
from .sync import sync_ignore, sync_walk
from .text import clip_lines, filter_lines, log_of, read_text

# What a plan answers when it was killed rather than exiting, as a shell would.
KILLED_EXIT = 137
# How long taskkill may take to end a process tree on Windows.
KILL_TIMEOUT = 30
# What a run answers when a step could not be started at all.
LAUNCH_FAILED = 127
# What a run answers when it failed inside this package rather than in the
# script (sysexits' EX_SOFTWARE), so it never reads as the script's own exit 1.
INTERNAL_ERROR = 70
# What stream_command answers for a step that outran its own deadline.
TIMED_OUT = "timed out"


def kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(  # nosec B603 B607: taskkill on our own child's pid
            [shutil.which("taskkill") or "taskkill", "/T", "/F", "/PID", str(proc.pid)],
            capture_output=True,
            check=False,
            timeout=KILL_TIMEOUT,
        )
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def stream_command(
    cmd: list[str],
    cwd: Path,
    timeout: float | None,
    emit: Sink,
    stop: Reason | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, str | None]:
    """Run `cmd`, handing `emit` each line of output as it lands.

    Ends at the command's own exit, its timeout, or the first time `stop()`
    answers a reason; either of the last two kills its whole process tree.
    Returns (exit code, why it was killed or None); TIMED_OUT for the timeout.
    """
    try:
        # A step is an argv from the settings, which only a person edits.
        proc = subprocess.Popen(  # nosec B603
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
            why = TIMED_OUT
        if why is not None:
            kill_tree(proc)
            proc.wait()
            break
    # A grandchild that escaped the kill can hold the pipe open; do not wait on it forever.
    reader.join(timeout=5)
    return proc.returncode, why


def elapsed(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def run_plan(
    plan: Plan, emit: Sink, stop: Reason | None = None, quiet: Sink | None = None
) -> int:
    """Run a plan, handing `emit` its output. Its exit code, or KILLED_EXIT.
    `quiet` gets the output a gauntlet's members print, which `emit` does not."""
    if plan.kind == GAUNTLET:
        return run_gauntlet(plan, emit, stop, quiet)
    if plan.kind == "forbid":
        return run_forbid(plan, emit)
    deadline = time.monotonic() + plan.timeout if plan.timeout else None
    for command in plan.commands:
        emit(f"$ {shlex.join(command)}")
        left = max(deadline - time.monotonic(), 1) if deadline else None
        code, why = stream_command(command, plan.cwd, left, emit, stop, env=plan.env)
        if why is not None:
            # A command's own deadline is what is left of the plan's; name the plan's.
            emit(f"killed after {plan.timeout}s timeout" if why is TIMED_OUT else why)
            return KILLED_EXIT
        if code != 0:
            return code
    return 0


def run_gauntlet(
    plan: Plan, emit: Sink, stop: Reason | None = None, quiet: Sink | None = None
) -> int:
    """Each member in order, one line apiece; a failure's own output under its
    line, a member that could not start included. A member's output goes to
    `quiet` as it lands, so it can be watched live."""
    started = time.monotonic()
    failed = 0
    ran = 0
    for member, sub in plan.members:
        if isinstance(sub, str):
            emit(f"  SKIP  {member:<16} {sub}")
            continue
        lines = []

        def member_out(line, lines=lines):
            lines.append(line)
            if quiet:
                quiet(line)

        if quiet:
            quiet(f"--- {member}")
        began = time.monotonic()
        try:
            code = run_plan(sub, member_out, stop)
        except ToolError as e:
            member_out(str(e))
            code = LAUNCH_FAILED
        status = "OK" if code == 0 else ("KILLED" if code == KILLED_EXIT else "FAIL")
        emit(f"  {status:<5} {member:<16} {elapsed(time.monotonic() - began)}")
        ran += 1
        if code == 0:
            continue
        # The tail, because the end of a failed build is where its error is.
        for line in lines[-150:]:
            emit(f"        {line}")
        failed = failed or code
        if code == KILLED_EXIT or not plan.keep_going:
            emit(f"FAILED at {member} after {elapsed(time.monotonic() - started)}")
            return code
    took = elapsed(time.monotonic() - started)
    emit(f"{'some FAILED' if failed else f'all {ran} OK'} in {took}")
    return failed


def run_forbid(plan: Plan, emit: Sink) -> int:
    """Every break of the plan's rules in the files a sync would carry."""
    rules = Rules(plan.cfg)
    files = sync_walk(rules, sync_ignore(plan.cfg))
    issues, scanned = 0, 0
    for rel in files:
        if plan.files and not any_match(rel, plan.files):
            continue
        if any_match(rel, plan.exclude):
            continue
        text = read_text(core.ROOT / rel)
        if text is None:
            continue
        scanned += 1
        found = []
        for rx, message in plan.rules:
            for match in rx.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                found.append((line, f"{message}: `{match.group(0).strip()[:60]}`"))
        for line, problem in sorted(found):
            emit(f"{rel}:{line}: {problem}")
        issues += len(found)
    emit(f"{issues} issue(s) in {scanned} file(s)")
    return 1 if issues else 0


def local_script(
    cfg: dict,
    plan: Plan,
    emit: Sink,
    *,
    stop: Reason | None = None,
    watch: Sink | None = None,
    quiet: Sink | None = None,
) -> int:
    """Run a script's plan here, streaming. Its exit code, KILLED_EXIT,
    LAUNCH_FAILED when a step could not start, or INTERNAL_ERROR. Never raises:
    a run's caller is often a thread with nobody above it to hear an
    exception, so a failure is a line of output and a code instead.

    `emit` gets what belongs in the result; `quiet` gets a gauntlet member's
    own output, which the result leaves out. `watch` and the log get both.
    """
    with run_output(cfg, plan.name, emit, watch, quiet) as (out, quiet_out):
        try:
            return run_plan(plan, out, stop, quiet_out)
        except ToolError as e:
            out(str(e))
            return LAUNCH_FAILED
        except Exception as e:
            out(f"internal error: {type(e).__name__}: {e}")
            return INTERNAL_ERROR


def report_lines(
    lines: list[str],
    limit: int,
    grep: str | None = None,
    tail: int | None = None,
    after: int | None = None,
) -> str:
    """A run's result lines through `grep` (with `after`) and `tail`, then cut
    to `limit`. The `log:` line survives both, so the whole of it can still be found."""
    see = log_of(lines)
    pointer = f"log: {see}"
    lines, note = filter_lines(
        lines, grep, tail, pinned=lambda line: line == pointer, after=after
    )
    if note:
        at = lines.index(pointer) + 1 if see else 0
        lines[at:at] = [note]
    return clip_lines(lines, limit, see=see)
