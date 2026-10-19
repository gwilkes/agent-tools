"""Runs: a script's work, here or on hosts, going on in a thread of its own so
a run_script call can answer before its run ends, and kept for `job` to
report on. A run is also what owns stopping it, for the MCP server and the
CLI alike: a lane here polls its stop reason, and each host's job is killed."""

import itertools
import shlex
import threading
import time
from collections.abc import Callable

from .client import Remote, kill_job
from .core import LOCAL
from .lanes import Lane, check_script, exit_text, lane_report, run_lane, run_lanes
from .procs import report_lines
from .runlog import Sink
from .text import log_of

# Finished runs `job` still reports on; older ones are forgotten, though not their logs.
KEEP_FINISHED = 50
# How long an answer given mid-run waits for each lane to name its log.
LOG_WAIT = 5
# Set by the MCP server, whose runs a later `job` call can follow. A CLI
# process ends with its one call, so its runs are waited out. Read as
# `runs.OUTLIVES_CALLS`, so the server's assignment is seen.
OUTLIVES_CALLS = False


class Run:
    """A run_script run, going on in a thread of this server. The call that
    started it waits on it as long as it was asked to, then answers; `job`
    reports on it from then on. Its lanes are the hosts it was asked for, or
    LOCAL alone, reported without a section header, for a run here."""

    def __init__(
        self,
        run_id: str,
        label: str,
        lanes: list[str],
        single: bool,
        watchers: list[Sink],
    ):
        self.id, self.label, self.single = run_id, label, single
        self.lines: dict[str, list[str]] = {lane: [] for lane in lanes}
        self.codes: dict[str, int | None] = {}
        self.seconds: dict[str, float] = {}
        self.began, self.ended = time.monotonic(), None
        self.stop_why: str | None = None
        self.remotes: list[tuple[Remote, str]] = []
        # Whoever is showing the run live: the progress reporter of a call waiting on it.
        self.watchers = list(watchers)
        # The script's own grep, tail and after, for a `job` call that gives none.
        self.filters: dict = {}
        self.done = threading.Event()

    def watch(self, lane: str) -> Sink:
        """A line sink handing every line of `lane` to the current watchers."""
        prefix = "" if self.single else f"[{lane}] "

        def send(line):
            for watcher in list(self.watchers):
                watcher(prefix + line)

        return send

    def stop_reason(self) -> str | None:
        return self.stop_why

    def add_remote_job(self, remote: Remote, job_id: str) -> None:
        self.remotes.append((remote, job_id))
        # A stop that came while this host was still syncing found no job to kill.
        if self.stop_why:
            kill_job(remote, job_id)

    def stop(self, reason: str = "killed by job(stop=true)") -> list[str]:
        """End every lane: a run here sees it within a second, and each host's
        job is killed there. The lines to report, for hosts that did not answer."""
        self.stop_why = self.stop_why or reason
        notes = [kill_job(r, i) for r, i in list(self.remotes)]
        return [n for n in notes if n]

    def finish_lane(self, lane: str, code: int | None) -> None:
        self.codes[lane] = code
        self.seconds[lane] = time.monotonic() - self.began

    def wait_for_logs(self) -> None:
        """Until every lane has named its log, the run ends, or LOG_WAIT passes."""
        deadline = time.monotonic() + LOG_WAIT
        while not all(log_of(lines) for lines in self.lines.values()):
            if time.monotonic() > deadline or self.done.wait(0.05):
                return

    def lanes(self) -> list[Lane]:
        now = time.monotonic() - self.began
        return [
            Lane(
                lane,
                list(lines),
                self.codes.get(lane),
                self.seconds.get(lane, now),
                running=lane not in self.codes,
            )
            for lane, lines in self.lines.items()
        ]

    def summary(self) -> str:
        if not self.done.is_set():
            return f"running for {time.monotonic() - self.began:.1f}s"
        took = f"{self.ended - self.began:.1f}s"
        if self.single:
            return f"{exit_text(self.codes.get(LOCAL))} in {took}"
        lanes = ", ".join(f"{h} {exit_text(self.codes.get(h))}" for h in self.lines)
        return f"ended in {took}: {lanes}"

    def report(
        self,
        limit: int,
        grep: str | None,
        tail: int | None,
        after: int | None = None,
    ) -> tuple[str, bool]:
        """How the run stands: its head, then its output so far (a section per
        host), with a pointer to `job` while it is still going. Returns (text, failed).
        """
        going = (
            ""
            if self.done.is_set()
            else f'\nstill running: job(id="{self.id}") reports on it, '
            "`wait` waits for it to end, `stop` kills it"
        )
        limit -= len(going)
        if self.single:
            (lane,) = self.lanes()
            status = "running for" if lane.running else f"{exit_text(lane.code)} in"
            head = f"{self.label}: {status} {lane.seconds:.1f}s (job {self.id})"
            body = report_lines(lane.lines, limit - len(head) - 1, grep, tail, after)
            failed = not lane.running and lane.code != 0
            return f"{head}\n{body}".rstrip() + going, failed
        head = f"{self.label}: {self.summary()} (job {self.id})"
        text, failed = lane_report(
            self.lanes(), limit - len(head) - 2, grep, tail, after
        )
        return f"{head}\n\n{text}{going}", failed


RUNS = {}
RUNS_LOCK = threading.Lock()
RUN_IDS = itertools.count(1)


def launch(
    label: str,
    lanes: list[str],
    single: bool,
    watchers: list[Sink],
    body: Callable[[Run], object],
) -> Run:
    """Run `body(run)` in a thread of its own; returns the Run."""
    with RUNS_LOCK:
        run = Run(str(next(RUN_IDS)), label, lanes, single, watchers)
        finished = [r for r in RUNS.values() if r.done.is_set()]
        for old in finished[: max(0, len(finished) - KEEP_FINISHED + 1)]:
            del RUNS[old.id]
        RUNS[run.id] = run

    def go():
        try:
            body(run)
        finally:
            run.ended = time.monotonic()
            run.done.set()

    threading.Thread(target=go, daemon=True).start()
    return run


def running_runs() -> list[Run]:
    with RUNS_LOCK:
        return [r for r in RUNS.values() if not r.done.is_set()]


def stop_runs(reason: str = "killed: the server is exiting", wait: float = 5) -> None:
    """Stop every run still going, for a process about to exit: once it has,
    nothing could follow them or stop them."""
    runs = running_runs()
    for run in runs:
        run.stop(reason)
    deadline = time.monotonic() + wait
    for run in runs:
        run.done.wait(max(0, deadline - time.monotonic()))


def start_run(
    cfg: dict,
    name: str,
    args: list[str],
    hosts: list[str] | None,
    sync: bool,
    watchers: list[Sink],
) -> Run:
    """A run of script `name`, started; a bad script name or argument fails here.

    Each lane's lines and code are recorded as they land, a refusal's
    included, since run_lanes hands those to `echo` too. Watchers see
    everything: the result's lines through echo, and the quiet output a
    gauntlet's members print."""
    plan = check_script(cfg, name, args, hosts)
    lanes = list(dict.fromkeys(hosts or [LOCAL]))

    def body(run):
        def work(host, emit):
            code = None
            try:
                code = run_lane(
                    cfg,
                    host,
                    name,
                    args,
                    emit,
                    plan=plan,
                    sync=sync,
                    quiet=run.watch(host),
                    stop=run.stop_reason,
                    on_job=run.add_remote_job,
                )
                return code
            finally:
                run.finish_lane(host, code)

        def echo(host, line):
            run.lines[host].append(line)
            run.watch(host)(line)

        run_lanes(lanes, work, echo)

    # `["local"]` alone is how a caller overrides a script's default hosts, so
    # it answers as a run here does.
    single = lanes == [LOCAL]
    return launch(shlex.join([name, *args]), lanes, single, watchers, body)
