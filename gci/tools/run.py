"""Running things: the settings' scripts, here or on hosts, and the tools that
look after those hosts."""

import itertools
import shlex
import threading
import time

from ..client import (
    RUNNING,
    Transport,
    lane_report,
    lane_word,
    reach,
    remote_script,
    remote_sync,
    run_lanes,
)
from ..core import (
    LOCAL,
    VERSION,
    ToolError,
    config,
    describe_hosts,
    describe_scripts,
    filter_lines,
    filter_text,
    host_map,
    log_of,
    output_limit,
)
from ..procs import local_script, report_lines, script_report
from ..progress import current as current_progress
from ..scripts import WrongPlatform, script_plan
from . import GREP, TAIL, tool

# Finished detached runs `job` still reports on; older ones are forgotten, though not their logs.
KEEP_FINISHED = 20
# How long a detached run's answer waits for each lane to name its log.
LOG_WAIT = 5
# How long `job` with stop waits for the run to end. A host's /kill alone may take 30s.
STOP_WAIT = 40


class Detached:
    """A run_script run that goes on in a thread of this server after the call
    has answered, for `job` to report on. Its lanes are the hosts, or the one
    lane None for a run here alone."""

    def __init__(self, job_id, label, lanes):
        self.id, self.label = job_id, label
        self.lines = {lane: [] for lane in lanes}
        self.codes, self.seconds = {}, {}
        self.began, self.ended = time.monotonic(), None
        self.stop_reason = None
        self.remotes = []
        self.done = threading.Event()

    def should_stop(self):
        return self.stop_reason

    def started(self, remote, job_id):
        self.remotes.append((remote, job_id))
        # A stop that came while this host was still syncing found no job to kill.
        if self.stop_reason:
            self.kill_remote(remote, job_id)

    def kill_remote(self, remote, job_id):
        try:
            remote.post_json("/kill", {"job": job_id}, timeout=60)
        except Transport as e:
            return f"could not stop the job on {remote.label}: {e}"
        return None

    def stop(self):
        """End every lane: a run here sees it within a second, and each host's
        job is killed there. The lines to report, for hosts that did not answer."""
        self.stop_reason = self.stop_reason or "killed by job(stop=true)"
        notes = [self.kill_remote(r, i) for r, i in list(self.remotes)]
        return [n for n in notes if n]

    def finish_lane(self, lane, code):
        self.codes[lane] = code
        self.seconds[lane] = time.monotonic() - self.began

    def logs(self):
        """Each lane's log, once every lane has named its own or LOG_WAIT has passed."""
        deadline = time.monotonic() + LOG_WAIT
        while not all(self.lines.values()) and time.monotonic() < deadline:
            if self.done.wait(0.05):
                break
        return {lane: log_of(lines) for lane, lines in self.lines.items()}

    def summary(self):
        if not self.done.is_set():
            return f"running for {time.monotonic() - self.began:.1f}s"
        took = f"{self.ended - self.began:.1f}s"
        if list(self.lines) == [None]:
            return f"{lane_word(self.codes.get(None))} in {took}"
        lanes = ", ".join(f"{h} {lane_word(self.codes.get(h))}" for h in self.lines)
        return f"ended in {took}: {lanes}"

    def report(self, limit, grep, tail):
        """How the run stands, laid out as run_script answers. Returns (text, failed)."""
        now = time.monotonic() - self.began
        if list(self.lines) == [None]:
            code = self.codes.get(None, RUNNING)
            took = self.seconds.get(None, now)
            word = "running for" if code == RUNNING else f"{lane_word(code)} in"
            head = f"{self.label}: {word} {took:.1f}s (job {self.id})"
            body = report_lines(
                list(self.lines[None]), limit - len(head) - 1, grep, tail
            )
            return f"{head}\n{body}".rstrip(), code not in (0, RUNNING)
        head = f"{self.label}: {self.summary()} (job {self.id})"
        results = [
            (
                lane,
                list(lines),
                self.codes.get(lane, RUNNING),
                self.seconds.get(lane, now),
            )
            for lane, lines in self.lines.items()
        ]
        text, failed = lane_report(results, limit - len(head) - 2, grep, tail)
        return f"{head}\n\n{text}", failed


DETACHED = {}
DETACHED_LOCK = threading.Lock()
DETACHED_IDS = itertools.count(1)


def detach(label, lanes, body):
    """Run `body(detached)` in a thread of its own; returns the Detached."""
    with DETACHED_LOCK:
        detached = Detached(str(next(DETACHED_IDS)), label, lanes)
        finished = [d for d in DETACHED.values() if d.done.is_set()]
        for old in finished[: max(0, len(finished) - KEEP_FINISHED + 1)]:
            del DETACHED[old.id]
        DETACHED[detached.id] = detached

    def go():
        try:
            body(detached)
        finally:
            detached.ended = time.monotonic()
            detached.done.set()

    threading.Thread(target=go, daemon=True).start()
    return detached


def running_detached():
    with DETACHED_LOCK:
        return [d for d in DETACHED.values() if not d.done.is_set()]


def stop_detached(wait=5):
    """Stop every detached run still going, for a server about to exit: once
    it has, nothing could follow them or stop them."""
    runs = running_detached()
    for detached in runs:
        detached.stop()
    deadline = time.monotonic() + wait
    for detached in runs:
        detached.done.wait(max(0, deadline - time.monotonic()))


def start_detached(cfg, name, args, hosts, sync, proxy):
    """run_script's `detach`: the run started in the background, and the answer
    naming its job and its logs."""
    label = shlex.join([name, *args])
    if not hosts:
        script_plan(cfg, name, args)

        def body(detached):
            out, code = detached.lines[None].append, 1
            try:
                code = local_script(cfg, name, args, out, stop=detached.should_stop)
            except ToolError as e:
                out(str(e))
                code = 127
            except Exception as e:  # the thread's end is where this would vanish
                out(f"internal error: {type(e).__name__}: {e}")
            finally:
                detached.finish_lane(None, code)

        lanes = [None]
    else:
        try:
            script_plan(cfg, name, args)
        except WrongPlatform:
            pass  # a script for another platform may still suit the hosts asked

        def body(detached):
            def work(host, emit):
                code = None
                try:
                    if host == LOCAL:
                        code = local_script(
                            cfg, name, args, emit, stop=detached.should_stop
                        )
                    else:
                        code = remote_script(
                            cfg,
                            host,
                            name,
                            args,
                            sync,
                            proxy,
                            emit,
                            started=detached.started,
                        )
                    return code
                finally:
                    detached.finish_lane(host, code)

            def echo(host, line):
                detached.lines[host].append(line)

            # run_lanes' own record is the whole of each lane, a refusal included.
            for host, lines, code, seconds in run_lanes(hosts, work, echo):
                detached.lines[host] = lines
                detached.codes[host], detached.seconds[host] = code, seconds

        lanes = list(dict.fromkeys(hosts))
    detached = detach(label, lanes, body)
    out = [f"{label}: detached as job {detached.id}"]
    for lane, log in detached.logs().items():
        if log:
            out.append(f"log: {log}" if lane is None else f"{lane} log: {log}")
    out.append(
        f'job(id="{detached.id}") reports on it: `wait` waits for it to end, '
        "`stop` kills it"
    )
    return "\n".join(out)


HOSTS = (
    {"type": "array", "items": {"type": "string"}},
    "Host names, as list_scripts lists them (or addresses/URLs); `local` is this checkout.",
)
PROXY = (
    "boolean",
    "true: reach hosts through the project's proxy; false: directly only. "
    "Unset: directly, falling back to the proxy.",
    None,
)


@tool(
    """
    List the scripts run_script can run: each one's name, what it is for and
    the command it runs, marked `[args...]` if it takes arguments and with the
    platforms it is limited to, if any. Then the hosts it can run them on.
    """,
    lines=True,
)
def list_scripts():
    cfg = config()
    return f"scripts:\n{describe_scripts(cfg)}\nhosts:\n{describe_hosts(cfg)}"


@tool(
    """
    Run one of the project's scripts (list_scripts lists them): the build, the
    tests, anything the project needs a process for. Only those scripts run,
    and only the ones marked `[args...]` take arguments. Answers with the exit
    code, the time taken and the combined stdout/stderr. With `hosts`, it runs
    on each of them at once instead: this tree is synced to each host first,
    and the answer has a section per host.

    `grep` and `tail` narrow the output (each host's, with `hosts`) before it
    is cut to fit; the run's log keeps all of it, and its `log:` line is
    always kept, so grep or read that file to look again without rerunning.

    With `detach`, it answers at once with a job id and the run's log (each
    host's, with `hosts`), and the run goes on in the background; `job`
    reports on it, waits for it or stops it.
    """,
    name=("string", "The script's name, as list_scripts lists it."),
    args=(
        {"type": "array", "items": {"type": "string"}},
        "Arguments appended to its command, for scripts that take them.",
        None,
    ),
    hosts=(HOSTS[0], HOSTS[1] + " Omit to run here alone.", None),
    sync=("boolean", "Sync this tree to each host before running.", True),
    proxy=PROXY,
    detach=("boolean", "Answer at once and let the run go on; see `job`.", False),
    grep=GREP,
    tail=TAIL,
)
def run_script(
    name,
    args=None,
    hosts=None,
    sync=True,
    proxy=None,
    detach=False,
    grep=None,
    tail=None,
):
    cfg = config()
    # A bad pattern fails before the run, not after it.
    filter_lines([], grep, tail)
    args = list(args or [])
    if detach:
        return start_detached(cfg, name, args, hosts, sync, proxy)
    progress = current_progress()
    if not hosts:
        return script_report(cfg, name, args, progress, grep, tail)
    # Here as well as on each host, so a typo fails once rather than per host.
    try:
        script_plan(cfg, name, args)
    except WrongPlatform:
        pass  # a script for another platform may still suit the hosts asked

    def work(host, emit):
        watch = (lambda line: progress(f"[{host}] {line}")) if progress else None
        if host == LOCAL:
            return local_script(cfg, name, args, emit, watch=watch)
        return remote_script(cfg, host, name, args, sync, proxy, emit, watch=watch)

    return lane_report(run_lanes(hosts, work), output_limit(cfg), grep, tail)


@tool(
    """
    Bring each host's tree level with this one, without running anything:
    changed files are sent and files the host has that this tree does not are
    deleted there. With dry_run, report what would be sent and deleted.
    """,
    hosts=HOSTS,
    dry_run=("boolean", "Report without sending or deleting.", False),
    proxy=PROXY,
    grep=GREP,
    tail=TAIL,
)
def sync(hosts, dry_run=False, proxy=None, grep=None, tail=None):
    cfg = config()
    filter_lines([], grep, tail)

    def work(host, emit):
        remote, _ = reach(cfg, host, proxy, emit)
        remote_sync(cfg, remote, dry_run, emit)
        return 0

    lanes = run_lanes([h for h in hosts if h != LOCAL], work)
    return lane_report(lanes, output_limit(cfg), grep, tail)


@tool(
    """
    Every host run_script can reach: whether it answers, its platform, whether
    it runs this version of the tools, and what it is running now.
    """,
    lines=True,
    proxy=PROXY,
)
def hosts(proxy=None):
    cfg = config()
    names = list(host_map(cfg))
    if not names:
        return describe_hosts(cfg)

    def work(host, emit):
        remote, info = reach(cfg, host, proxy, emit, converge=False)
        same = (
            "this version"
            if info["version"] == VERSION
            else f"version {info['version']} (this is {VERSION})"
        )
        running = f", running {info['running']}" if info.get("running") else ", idle"
        emit(f"up at {remote.url}: {info['platform']}, {same}{running}")
        return 0

    rows = [
        f"{host}: {' / '.join(lines)}" for host, lines, _, _ in run_lanes(names, work)
    ]
    return "\n".join(rows)


@tool(
    """
    Stop whatever script each host is running, with its whole process tree.
    """,
    hosts=HOSTS,
    proxy=PROXY,
)
def kill(hosts, proxy=None):
    cfg = config()

    def work(host, emit):
        remote, _ = reach(cfg, host, proxy, emit, converge=False)
        for line in remote.post_json("/kill", {}, timeout=60)["lines"]:
            emit(line)
        return 0

    return lane_report(run_lanes([h for h in hosts if h != LOCAL], work))


@tool(
    """
    Follow a run that run_script started with `detach`: how it stands and its
    output so far, laid out as run_script answers once a run ends. Alone, an
    id answers at once with what there is. `wait` first waits up to that many
    seconds for the run to end; `stop` first kills it,
    with its whole process tree here or its job on each host. With no id, list
    the detached runs. They live in this server: a restart is refused while
    one is going, and forgets the finished ones (their logs stay).
    """,
    id=("string", "The job id run_script answered with. Omit to list them.", None),
    wait=("integer", "Seconds to wait for the run to end before answering.", 0),
    stop=("boolean", "Kill the run, then report it.", False),
    grep=GREP,
    tail=TAIL,
)
def job(id=None, wait=0, stop=False, grep=None, tail=None):
    filter_lines([], grep, tail)
    if id is None:
        with DETACHED_LOCK:
            runs = list(DETACHED.values())
        rows = [f"{d.id}  {d.label}  {d.summary()}" for d in runs]
        return filter_text("\n".join(rows) or "no detached runs", grep, tail)
    with DETACHED_LOCK:
        detached = DETACHED.get(str(id))
    if detached is None:
        raise ToolError(f"no detached run {id}; job() lists them")
    notes = []
    if stop:
        notes = detached.stop()
        detached.done.wait(STOP_WAIT)
    elif wait:
        detached.done.wait(max(0, wait))
    text, failed = detached.report(output_limit(config()), grep, tail)
    return "\n".join([*notes, text]), failed
