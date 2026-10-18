"""Reaching another host's `gci serve`: converging versions, syncing this tree,
starting a script there and following it, and running many hosts at once."""

import http.client
import io
import json
import ssl
import tarfile
import threading
import time
import urllib.parse

from . import core
from .core import (
    MAX_OUTPUT,
    VERSION,
    Rules,
    ToolError,
    clip_lines,
    host_map,
    human,
    log_of,
    remote_cfg,
)
from .procs import RunLog, tee
from .server import JOB_POLL, KEY_HEADER, LIVE_PREFIX, USER_AGENT, VERSION_HEADER
from .sync import file_hash, pack_sources, sync_ignore, sync_walk

# Cloudflare refuses a request body over 100 MB and cuts a request unanswered for 100 s.
UPLOAD_BATCH = 64 << 20
CONNECT_TIMEOUT = 10
# How long a first contact waits before the proxy is tried instead.
PROBE_TIMEOUT = 4
UPLOAD_TIMEOUT = 600
# How long a client keeps retrying a host it lost, well inside the server's JOB_ABANDON.
JOB_RECONNECT = 480


class Transport(Exception):
    """A host that could not be reached, or that refused."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Remote:
    """One host's server, as this client reaches it."""

    def __init__(self, label, url, key):
        self.label, self.url, self.key = label, url, key

    def request(
        self,
        method,
        path,
        body=None,
        ctype="application/json",
        timeout=CONNECT_TIMEOUT,
        connect=CONNECT_TIMEOUT,
    ):
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(
                parts.hostname,
                parts.port,
                timeout=connect,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(
                parts.hostname, parts.port, timeout=connect
            )
        headers = {
            KEY_HEADER: self.key,
            "User-Agent": USER_AGENT,
            VERSION_HEADER: VERSION,
        }
        if body is not None:
            headers["Content-Type"] = ctype
        try:
            conn.connect()
            conn.sock.settimeout(timeout)
            conn.request(
                method, parts.path.rstrip("/") + path, body=body, headers=headers
            )
            resp = conn.getresponse()
            data = resp.read()
        except OSError as e:
            raise Transport(f"cannot reach {self.label} at {self.url}: {e}") from e
        finally:
            conn.close()
        if resp.status != 200:
            try:
                msg = json.loads(data)["error"]
            except (ValueError, KeyError, TypeError):
                msg = data.decode(errors="replace").strip() or f"HTTP {resp.status}"
            raise Transport(f"{self.label}: {msg}", status=resp.status)
        return data

    def get_json(self, path, **kw):
        return json.loads(self.request("GET", path, **kw))

    def post_json(self, path, payload, **kw):
        return json.loads(
            self.request("POST", path, json.dumps(payload).encode(), **kw)
        )


def host_url(cfg, name, via_proxy):
    remote, hosts = remote_cfg(cfg), host_map(cfg)
    if via_proxy:
        proxy = remote.get("proxy")
        if not proxy:
            raise ToolError(f"no remote.proxy in {core.CONFIG}")
        if name not in hosts:
            raise ToolError(
                f"{name} is not in remote.hosts; the proxy routes only those"
            )
        return f"{proxy.rstrip('/')}/{name}"
    addr = hosts.get(name, name)
    return (
        addr
        if "://" in addr
        else f"http://{addr}:{remote.get('port', core.DEFAULT_PORT)}"
    )


def reach(cfg, name, proxy, emit, converge=True):
    """A host's Remote and its /info, through the proxy if it must be, running
    this package if `converge`. `proxy` None means direct, then the proxy."""
    key = core.api_key(cfg)
    if key is None:
        raise ToolError(core.missing_key(cfg))
    remote = Remote(name, host_url(cfg, name, bool(proxy)), key)
    try:
        info = remote.get_json("/info", connect=PROBE_TIMEOUT)
    except Transport as e:
        if not (
            proxy is None
            and e.status is None
            and remote_cfg(cfg).get("proxy")
            and name in host_map(cfg)
        ):
            raise
        emit(f"{name}: no answer directly ({e}); trying the proxy")
        remote = Remote(name, host_url(cfg, name, True), key)
        info = remote.get_json("/info")
    if converge and info["version"] != VERSION:
        emit(
            f"{name} runs {info['version']}, this is {VERSION}; installing this one there"
        )
        info = converge_version(remote, emit)
    return remote, info


def converge_version(remote, emit):
    """Install this package on a skewed host and wait for its server to restart into it."""
    reply = json.loads(
        remote.request(
            "POST",
            "/self-update",
            pack_sources(core.package_sources()),
            ctype="application/gzip",
            timeout=UPLOAD_TIMEOUT,
        )
    )
    deadline = time.monotonic() + 30
    answering = None
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            info = remote.get_json("/info")
        except Transport:
            continue
        if info["version"] == VERSION:
            emit(f"{remote.label} restarted into {VERSION}")
            return info
        answering = info
    if answering is None:
        raise Transport(
            f"{remote.label} took {reply['version']} but did not come back "
            "within 30s; check the server there"
        )
    raise Transport(
        f"{remote.label} still answers as {answering['version']} after 30s "
        f"(pid {answering.get('pid', '?')}); another server may hold the port"
    )


def upload_batches(sizes, cap):
    """Split (file, size) pairs into runs summing to at most `cap`; a file over it travels alone."""
    batches, current, total = [], [], 0
    for rel, size in sizes:
        if current and total + size > cap:
            batches.append(current)
            current, total = [], 0
        current.append(rel)
        total += size
    if current:
        batches.append(current)
    return batches


def remote_sync(cfg, remote, dry, emit):
    """Make the host's tree match this one's sync domain."""
    root = core.ROOT
    extra = sync_ignore(cfg)
    files, _ = sync_walk(Rules(cfg), extra)
    manifest = {rel: file_hash(root / rel) for rel in files}
    reply = remote.post_json(
        "/manifest",
        {"files": manifest, "dry": dry, "ignore": extra},
        timeout=UPLOAD_TIMEOUT,
    )
    need, deleted = reply["need"], reply["deleted"]
    if dry:
        emit(
            f"dry run: {len(manifest)} files offered, {len(need)} would be sent, "
            f"{len(deleted)} would be deleted"
        )
        for verb, rels in (("send  ", need), ("DELETE", deleted)):
            for rel in rels[:20]:
                emit(f"  {verb}  {rel}")
            if len(rels) > 20:
                emit(f"  ... {len(rels) - 20} more")
        return
    sent = 0
    for batch in upload_batches(
        [(rel, (root / rel).stat().st_size) for rel in need], UPLOAD_BATCH
    ):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for rel in batch:
                tar.add(root / rel, arcname=rel, recursive=False)
        data = buf.getvalue()
        remote.request(
            "POST", "/files", data, ctype="application/gzip", timeout=UPLOAD_TIMEOUT
        )
        sent += len(data)
    gone = f", deleted {len(deleted)}" if deleted else ""
    if need:
        emit(
            f"synced {len(need)} of {len(manifest)} files ({human(sent)} compressed){gone}"
        )
    else:
        emit(f"in sync: {len(manifest)} files{gone}")


# Jobs this client started and has not seen finish. Ctrl-C on the CLI kills them.
ACTIVE_JOBS = set()
ACTIVE_JOBS_LOCK = threading.Lock()


def remote_script(cfg, host, name, args, do_sync, proxy, emit, watch=None, quiet=None):
    """Run a settings script on a host and follow it to its exit code.

    Logged here as it arrives, to tmp/gci/logs/HOST-NAME-*.log, the same way a
    local run is. `emit` gets what belongs in the result; `quiet` gets the
    output a gauntlet's result leaves out; `watch` and the log get both.
    """
    with RunLog(cfg, f"{host}-{name}") as log:
        out = tee(log.write, watch, emit)
        hush = tee(log.write, watch, quiet)
        if log.rel:
            out(f"log: {log.rel}")
        remote, _ = reach(cfg, host, proxy, out)
        if do_sync:
            remote_sync(cfg, remote, False, out)
        job_id = remote.post_json("/script", {"script": name, "args": args})["job"]
        entry = (remote, job_id)
        with ACTIVE_JOBS_LOCK:
            ACTIVE_JOBS.add(entry)
        # Left registered if following fails, so Ctrl-C still reaches it.
        code = follow_job(remote, job_id, out, hush)
        with ACTIVE_JOBS_LOCK:
            ACTIVE_JOBS.discard(entry)
        return code


def transient(e):
    """Whether a failed request is the way there failing rather than the server
    answering: no reply at all, or a proxy that could not reach it."""
    return e.status is None or e.status in (502, 503, 504) or 520 <= e.status <= 530


def follow_job(remote, job_id, emit, quiet=None, reconnect=JOB_RECONNECT, pause=2):
    next_line, lost_at = 0, None
    while True:
        query = urllib.parse.urlencode({"id": job_id, "from": next_line})
        try:
            reply = remote.get_json(f"/job?{query}", timeout=JOB_POLL + CONNECT_TIMEOUT)
        except Transport as e:
            if not transient(e):
                raise
            now = time.monotonic()
            if lost_at is None:
                lost_at = now
                emit(f"lost {remote.label} ({e}); retrying for {reconnect}s")
            elif now - lost_at > reconnect:
                raise Transport(
                    f"{remote.label} unreachable for {reconnect}s; the job there is "
                    "killed once abandoned"
                ) from e
            time.sleep(pause)
            continue
        if lost_at is not None:
            emit(f"reconnected after {time.monotonic() - lost_at:.0f}s")
            lost_at = None
        for line in reply["lines"]:
            if line.startswith(LIVE_PREFIX):
                if quiet:
                    quiet(line[len(LIVE_PREFIX) :])
            else:
                emit(line)
        next_line = reply["next"]
        if reply["code"] is not None:
            return reply["code"]


def stop_active_jobs():
    """Kill every job this client started that has not finished. The lines to report."""
    with ACTIVE_JOBS_LOCK:
        jobs = list(ACTIVE_JOBS)
    lines = []
    for remote, job_id in jobs:
        try:
            reply = remote.post_json("/kill", {"job": job_id}, timeout=60)
            lines += [f"{remote.label}: {line}" for line in reply["lines"]]
        except Transport as e:
            lines.append(f"could not stop the job on {remote.label}: {e}")
    return lines


# ---- lanes ----


def run_lanes(names, work, echo=None):
    """`work(name, emit)` for every name at once, each a lane of its own.

    Returns [(name, lines, code, seconds)] in the order given; code is None for
    a lane that never ran (unreachable, refused, misconfigured). `echo(name,
    line)` sees each line as it lands, for a caller printing live.
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
        try:
            code = work(name, emit)
        except (Transport, ToolError) as e:
            emit(str(e))
            code = None
        results[i] = (name, lines, code, time.monotonic() - started)

    threads = [
        threading.Thread(target=lane, args=(i, n), daemon=True)
        for i, n in enumerate(names)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def lane_word(code):
    return "did not run" if code is None else f"exit {code}"


def lane_report(results, limit=MAX_OUTPUT):
    """One section per lane, each cut to its share of `limit` with a pointer
    to that lane's log. Returns (text, failed)."""
    if not results:
        raise ToolError("no hosts named")
    budget = limit // len(results) - 100
    parts = [
        f"=== {name}: {lane_word(code)} in {seconds:.1f}s ===\n"
        f"{clip_lines(lines, budget, see=log_of(lines))}"
        for name, lines, code, seconds in results
    ]
    return "\n\n".join(parts), any(code != 0 for _, _, code, _ in results)
