"""Lanes: work done for several places at once, here (the LOCAL lane) or on
hosts, each with its own output and exit code, and reported a section each.
Running a script, syncing, checking and stopping hosts are all lanes."""

import threading
import time
from collections.abc import Callable
from typing import NamedTuple

from .client import JobSink, TransportError, reach, remote_script, remote_sync
from .core import LOCAL, MAX_OUTPUT, VERSION, ToolError, output_limit
from .procs import local_script, report_lines
from .runlog import Reason, Sink
from .scripts import Plan, WrongPlatform, script_plan
from .text import check_filters
from .wire import KILL, describe_hosts, host_map


def check_script(
    cfg: dict, name: str, args: list[str], hosts: list[str] | None
) -> Plan | None:
    """The script's plan for this machine, built once so a typo fails before
    any lane starts. None when it is for another platform but hosts were
    named, since it may still suit them; a local lane then refuses on its own."""
    try:
        return script_plan(cfg, name, args)
    except WrongPlatform:
        if not hosts:
            raise
        return None


def run_lane(
    cfg: dict,
    host: str,
    name: str,
    args: list[str],
    emit: Sink,
    *,
    plan: Plan | None = None,
    sync: bool = True,
    watch: Sink | None = None,
    quiet: Sink | None = None,
    stop: Reason | None = None,
    on_job: JobSink | None = None,
) -> int:
    """One lane of a run: the script here when `host` is LOCAL, else on that
    host. A run here polls `stop`; one on a host tells `on_job` of its job,
    which is how it is stopped."""
    if host == LOCAL:
        plan = plan or script_plan(cfg, name, args)
        return local_script(cfg, plan, emit, stop=stop, watch=watch, quiet=quiet)
    return remote_script(
        cfg, host, name, args, emit, sync=sync, watch=watch, quiet=quiet, on_job=on_job
    )


class Lane(NamedTuple):
    """One lane's record: its output, how long it took, and its exit code,
    which is None when there is none: the host was unreachable, refused, or
    was lost mid-run. `running` marks a record taken while it is still going."""

    name: str
    lines: list
    code: int | None
    seconds: float
    running: bool = False


def run_lanes(
    names: list[str],
    work: Callable[[str, Sink], int],
    echo: Callable[[str, str], object] | None = None,
) -> list[Lane]:
    """`work(name, emit)` for every name at once, each a lane of its own.
    Returns a Lane per name, in the order given, whatever `work` raised.
    `echo(name, line)` sees each line as it lands, for a caller printing live.
    """
    names = list(dict.fromkeys(names))
    results = [None] * len(names)

    def lane(i, name):
        lines = []

        def emit(line):
            lines.append(line)
            if echo:
                echo(name, line)

        started = time.monotonic()
        code = None
        try:
            code = work(name, emit)
        except (TransportError, ToolError) as e:
            emit(str(e))
        except Exception as e:  # a lane's thread is where this would vanish
            emit(f"internal error: {type(e).__name__}: {e}")
        finally:
            results[i] = Lane(name, lines, code, time.monotonic() - started)

    threads = [
        threading.Thread(target=lane, args=(i, n), daemon=True)
        for i, n in enumerate(names)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def exit_text(code: int | None) -> str:
    return "no exit code" if code is None else f"exit {code}"


def lane_report(
    results: list[Lane],
    limit: int = MAX_OUTPUT,
    grep: str | None = None,
    tail: int | None = None,
    after: int | None = None,
) -> tuple[str, bool]:
    """One section per lane, each through `grep` and `tail` and cut to its
    share of `limit`, with a pointer to that lane's log. Returns (text, failed)."""
    if not results:
        raise ToolError("no hosts to report on")
    budget = limit // len(results) - 100
    parts = [
        f"=== {lane.name}: "
        f"{'running for' if lane.running else exit_text(lane.code) + ' in'} "
        f"{lane.seconds:.1f}s ===\n"
        f"{report_lines(lane.lines, budget, grep, tail, after)}"
        for lane in results
    ]
    return "\n\n".join(parts), any(
        not lane.running and lane.code != 0 for lane in results
    )


def named_hosts(cfg: dict, hosts: list[str], local: bool = True) -> list[str]:
    """`hosts`, each a name the settings give (or LOCAL, where it means
    something). An agent reaches only the hosts a person listed: a raw address
    would be handed the API key and this tree."""
    known = set(host_map(cfg))
    unknown = [h for h in hosts if h not in known and not (local and h == LOCAL)]
    if unknown:
        raise ToolError(
            f"unknown host(s): {', '.join(unknown)}; the hosts are:\n"
            f"{describe_hosts(cfg)}"
        )
    return hosts


def sync_lanes(
    cfg: dict,
    hosts: list[str],
    dry_run: bool,
    grep: str | None = None,
    tail: int | None = None,
) -> tuple[str, bool]:
    """sync's work, for any hosts or addresses: a person's CLI may name either."""
    check_filters(grep, tail)
    hosts = [h for h in hosts if h != LOCAL]
    if not hosts:
        raise ToolError(f"only {LOCAL} was named; there is nothing here to sync")

    def work(host, emit):
        remote, _ = reach(cfg, host, emit)
        remote_sync(cfg, remote, dry_run, emit)
        return 0

    return lane_report(run_lanes(hosts, work), output_limit(cfg), grep, tail)


def host_status(cfg: dict) -> tuple[str, bool]:
    """hosts' work: one line per host. Returns (text, failed)."""
    names = list(host_map(cfg))
    if not names:
        return describe_hosts(cfg), False

    def work(host, emit):
        remote, info = reach(cfg, host, emit, converge=False)
        same = (
            "this version"
            if info["version"] == VERSION
            else f"version {info['version']} (this is {VERSION})"
        )
        running = f", running {info['running']}" if info.get("running") else ", idle"
        emit(f"up at {remote.url}: {info['platform']}, {same}{running}")
        return 0

    lanes = run_lanes(names, work)
    rows = [f"{lane.name}: {' / '.join(lane.lines)}" for lane in lanes]
    return "\n".join(rows), any(lane.code is None for lane in lanes)


def kill_lanes(cfg: dict, hosts: list[str]) -> tuple[str, bool]:
    """kill's work, for any hosts or addresses."""
    hosts = [h for h in hosts if h != LOCAL]
    if not hosts:
        raise ToolError(f"only {LOCAL} was named; to stop a run here, use job")

    def work(host, emit):
        remote, _ = reach(cfg, host, emit, converge=False)
        for line in remote.post_json(KILL, {}, ("lines",), timeout=60)["lines"]:
            emit(line)
        return 0

    return lane_report(run_lanes(hosts, work), output_limit(cfg))
