"""The tools for running things: the settings' scripts, here or on hosts,
following those runs, and looking after the hosts."""

from .. import runs
from ..core import ToolError, config, output_limit
from ..lanes import host_status, kill_lanes, named_hosts, sync_lanes
from ..progress import CURRENT as PROGRESS
from ..runs import RUNS, RUNS_LOCK, start_run
from ..scripts import script_defaults, script_listing, scripts_and_hosts
from ..text import check_filters, filter_text
from .registry import GREP, TAIL, cli_view, tool

# How long `job` with stop waits for the run to end. A host's /kill alone may take 30s.
STOP_WAIT = 40


def check_sync(sync):
    if not isinstance(sync, bool):
        raise ToolError(f"sync must be true or false, not {sync!r}")
    return sync


def wait_seconds(wait):
    if wait is not None and (isinstance(wait, bool) or not isinstance(wait, int)):
        raise ToolError(f"wait must be a number of seconds, not {wait!r}")
    return None if wait is None else max(0, wait)


AFTER = (
    "integer",
    "Lines to keep after each grep match, like `grep -A N`: an error's detail.",
    None,
)
HOSTS = (
    {"type": "array", "items": {"type": "string"}},
    "Host names, as the scripts tool lists them.",
)


@tool(
    """
    List the scripts run_script can run: each one's name, what it is for and
    the commands it runs, marked `[args...]` if it takes arguments and with the
    platforms it is limited to, if any. Then the hosts it can run them on.
    """,
    lines=True,
)
def scripts():
    return scripts_and_hosts(config())


@cli_view("scripts")
def scripts_view():
    return script_listing(config())


@tool(
    """
    Run one of the project's scripts (the scripts tool lists them): the
    build, the tests, anything the project needs a process for. Only those
    scripts run, and only the ones marked `[args...]` take arguments. Answers
    with the exit code, the time taken and the combined stdout/stderr. With `hosts`, it runs
    on each of them at once instead: this tree is synced to each host first,
    and the answer has a section per host.

    `grep` and `tail` narrow the output (each host's, with `hosts`) before it
    is cut to fit; the run's log keeps all of it, and its `log:` line is
    always kept, so grep or read that file to look again without rerunning.
    `after` keeps that many lines after each match, as an error's detail
    follows it. A script, or the settings for every script, can set `hosts`,
    `sync`, `wait`, `grep`, `after` and `tail` as defaults (the scripts tool
    shows them); passing one overrides it.

    Every run is a job. By default the call waits for it to end; with `wait`
    it answers after that many seconds if the run is still going (0: at once),
    with the output so far and the job id, and the run carries on: `job`
    reports on it, waits for it or stops it. So does a call the client gave
    up on.
    """,
    cli=("wait",),
    positional=("name", "args"),
    name=("string", "The script's name, as the scripts tool lists it."),
    args=(
        {"type": "array", "items": {"type": "string"}},
        "Arguments appended to its last command, for scripts that take them.",
        None,
    ),
    hosts=(
        HOSTS[0],
        HOSTS[1] + ' `local` among them runs it here too, and `["local"]` '
        "alone runs it only here; omit for the script's default, else here alone.",
        None,
    ),
    sync=(
        "boolean",
        "Sync this tree to each host before running; omit for the script's "
        "default, else true.",
        None,
    ),
    wait=(
        "integer",
        "Seconds to wait for the run to end before answering; omit to wait it "
        "out, or for the script's default.",
        None,
    ),
    grep=GREP,
    tail=TAIL,
    after=AFTER,
)
def run_script(
    name,
    args=None,
    hosts=None,
    sync=None,
    wait=None,
    grep=None,
    tail=None,
    after=None,
):
    cfg = config()
    # The script's defaults, for whatever the caller leaves out.
    defaults = script_defaults(cfg, name)
    hosts = defaults.get("hosts") if hosts is None else hosts
    named_hosts(cfg, hosts or [])
    sync = check_sync(defaults.get("sync", True) if sync is None else sync)
    wait = wait_seconds(defaults.get("wait") if wait is None else wait)
    if not runs.OUTLIVES_CALLS:
        # No later call could follow the run: this process ends with this one.
        wait = None
    grep = defaults.get("grep") if grep is None else grep
    tail = defaults.get("tail") if tail is None else tail
    after = defaults.get("after") if after is None else after
    check_filters(grep, tail, after)
    progress = PROGRESS.get()
    watchers = [progress] if progress else []
    run = start_run(cfg, name, list(args or []), hosts, sync, watchers)
    # What a later `job` shows of it, unless that call says otherwise.
    run.filters = {k: defaults.get(k) for k in ("grep", "tail", "after")}
    try:
        run.done.wait(wait)
    finally:
        # The call is answering; a later one shows the run live, or none does.
        for watcher in watchers:
            run.watchers.remove(watcher)
    if not run.done.is_set():
        run.wait_for_logs()
    return run.report(output_limit(cfg), grep, tail, after)


@tool(
    """
    Bring each host's tree level with this one, without running anything:
    changed files are sent and files the host has that this tree does not are
    deleted there. With dry_run, report what would be sent and deleted.
    `local` is skipped: there is nothing here to sync.
    """,
    hosts=HOSTS,
    dry_run=("boolean", "Report without sending or deleting.", False),
    grep=GREP,
    tail=TAIL,
)
def sync(hosts, dry_run=False, grep=None, tail=None):
    cfg = config()
    return sync_lanes(cfg, named_hosts(cfg, hosts), dry_run, grep, tail)


@tool(
    """
    Every host run_script can reach: whether it answers, its platform, whether
    it runs this version of the tools, and what it is running now.
    """,
    lines=True,
)
def hosts():
    return host_status(config())


@tool(
    """
    Stop whatever script each host is running, with its whole process tree,
    whoever started it. `local` is skipped: to stop one run this server
    started, here or on a host, use job with `stop` instead.
    """,
    hosts=HOSTS,
)
def kill(hosts):
    cfg = config()
    return kill_lanes(cfg, named_hosts(cfg, hosts))


@tool(
    """
    Follow a run_script run: how it stands and its output so far, laid out as
    run_script answers. Alone, an id answers at once with what there is.
    `wait` first waits up to that many seconds for the run to end; `stop`
    first kills it, with its whole process tree here or its job on each host.
    `grep`, `tail` and `after` default to the run's script's, as run_script's
    do. With no id, list the runs. They live in this server: a restart is
    refused while one is going, and forgets the finished ones (their logs
    stay).
    """,
    cli=False,
    id=("string", "The job id run_script answered with. Omit to list them.", None),
    wait=("integer", "Seconds to wait for the run to end before answering.", 0),
    stop=("boolean", "Kill the run, then report it.", False),
    grep=GREP,
    tail=TAIL,
    after=AFTER,
)
def job(id=None, wait=0, stop=False, grep=None, tail=None, after=None):
    wait = wait_seconds(wait)
    if id is None:
        check_filters(grep, tail)
        with RUNS_LOCK:
            runs = list(RUNS.values())
        rows = [f"{r.id}  {r.label}  {r.summary()}" for r in runs]
        return filter_text("\n".join(rows) or "no runs", grep, tail)
    with RUNS_LOCK:
        run = RUNS.get(str(id))
    if run is None:
        raise ToolError(f"no run {id}; job() lists them")
    given = {"grep": grep, "tail": tail, "after": after}
    grep, tail, after = (
        run.filters.get(k) if v is None else v for k, v in given.items()
    )
    check_filters(grep, tail, after)
    notes = []
    if stop:
        notes = run.stop()
        run.done.wait(STOP_WAIT)
    elif wait:
        run.done.wait(wait)
    text, failed = run.report(output_limit(config()), grep, tail, after)
    return "\n".join([*notes, text]), failed
