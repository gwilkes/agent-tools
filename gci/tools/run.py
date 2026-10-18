"""Running things: the settings' scripts, here or on hosts, and the tools that
look after those hosts."""

from ..client import lane_report, reach, remote_script, remote_sync, run_lanes
from ..core import (
    CONFIG,
    LOCAL,
    VERSION,
    config,
    describe_hosts,
    describe_scripts,
    host_map,
    output_limit,
)
from ..procs import local_script, script_report
from ..progress import current as current_progress
from ..scripts import WrongPlatform, script_plan
from . import tool

HOSTS = (
    {"type": "array", "items": {"type": "string"}},
    f"Host names from {CONFIG}'s remote.hosts (or addresses/URLs); `local` is this checkout.",
)
PROXY = (
    "boolean",
    f"true: reach hosts through {CONFIG}'s remote.proxy; false: directly only. "
    "Unset: directly, falling back to the proxy.",
    None,
)


@tool(
    f"""
    Run a script defined in {CONFIG} -- the build, the tests, git, anything the
    project needs a process for. Only the scripts listed there run, and extra
    args are accepted only by entries that allow them. Answers with the exit
    code, the time taken and the combined stdout/stderr. With `hosts`, it runs
    on each of them at once instead: this tree is synced to each host first,
    and the answer has a section per host. Call with no name to list the
    scripts and hosts.
    """,
    name=("string", f"The script's key in {CONFIG}.", None),
    args=(
        {"type": "array", "items": {"type": "string"}},
        "Arguments appended to its command, for scripts that take them.",
        None,
    ),
    hosts=(HOSTS[0], HOSTS[1] + " Omit to run here alone.", None),
    sync=("boolean", "Sync this tree to each host before running.", True),
    proxy=PROXY,
)
def run_script(name=None, args=None, hosts=None, sync=True, proxy=None):
    cfg = config()
    if not name:
        return (
            f"scripts in {CONFIG}:\n{describe_scripts(cfg)}\n"
            f"hosts:\n{describe_hosts(cfg)}"
        )
    args = list(args or [])
    progress = current_progress()
    if not hosts:
        return script_report(cfg, name, args, watch=progress)
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

    return lane_report(run_lanes(hosts, work), output_limit(cfg))


@tool(
    """
    Bring each host's tree level with this one, without running anything:
    changed files are sent and files the host has that this tree does not are
    deleted there. With dry_run, report what would be sent and deleted.
    """,
    hosts=HOSTS,
    dry_run=("boolean", "Report without sending or deleting.", False),
    proxy=PROXY,
)
def sync(hosts, dry_run=False, proxy=None):
    cfg = config()

    def work(host, emit):
        remote, _ = reach(cfg, host, proxy, emit)
        remote_sync(cfg, remote, dry_run, emit)
        return 0

    return lane_report(run_lanes([h for h in hosts if h != LOCAL], work))


@tool(
    f"""
    Every host in {CONFIG}: whether it answers, its platform, whether it runs
    this version of the tools, and what it is running now.
    """,
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
