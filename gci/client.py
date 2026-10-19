"""Reaching another host's `gci serve`: converging versions, syncing this tree,
and starting a script there and following it to its end."""

import http.client
import json
import ssl
import time
import urllib.parse
from collections.abc import Callable

from . import core
from .core import VERSION, Rules, ToolError
from .runlog import Sink, run_output
from .sync import file_hash, pack_files, pack_sources, sync_ignore, sync_walk
from .text import human_size
from .wire import (
    DEFAULT_PORT,
    FILES,
    INFO,
    JOB,
    JOB_POLL,
    KEY_HEADER,
    KILL,
    MANIFEST,
    SCRIPT,
    SELF_UPDATE,
    USER_AGENT,
    VERSION_HEADER,
    api_key,
    host_map,
    missing_key,
    remote_cfg,
)

# Cloudflare refuses a request body over 100 MB and cuts a request unanswered for 100 s.
UPLOAD_BATCH = 64 << 20
CONNECT_TIMEOUT = 10
UPLOAD_TIMEOUT = 600
# How long a client keeps retrying a host it lost, well inside the server's JOB_ABANDON.
JOB_RECONNECT = 480


class TransportError(Exception):
    """A host that could not be reached, or that refused."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Remote:
    """One host's server, as this client reaches it."""

    def __init__(self, label: str, url: str, key: str):
        self.label, self.url, self.key = label, url, key

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        ctype: str = "application/json",
        timeout: float = CONNECT_TIMEOUT,
    ) -> bytes:
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme == "https":
            conn = http.client.HTTPSConnection(
                parts.hostname,
                parts.port,
                timeout=CONNECT_TIMEOUT,
                context=ssl.create_default_context(),
            )
        else:
            conn = http.client.HTTPConnection(
                parts.hostname, parts.port, timeout=CONNECT_TIMEOUT
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
        # An HTTPException (a reply cut short by a proxy) fails the way there
        # as surely as a refused connection, so it is retried the same way.
        except (OSError, http.client.HTTPException) as e:
            raise TransportError(f"cannot reach {self.label} at {self.url}: {e}") from e
        finally:
            conn.close()
        if resp.status != 200:
            try:
                msg = json.loads(data)["error"]
            except (ValueError, KeyError, TypeError):
                msg = data.decode(errors="replace").strip() or f"HTTP {resp.status}"
            raise TransportError(f"{self.label}: {msg}", status=resp.status)
        return data

    def json_of(self, data: bytes, keys: tuple[str, ...] = ()) -> dict:
        """A reply as the object it must be, holding every one of `keys`, or a
        TransportError naming the host: an older server, or a proxy answering
        in its place, should not surface as a KeyError."""
        try:
            reply = json.loads(data)
        except ValueError:
            raise TransportError(f"{self.label}: the reply is not JSON") from None
        if not isinstance(reply, dict):
            raise TransportError(f"{self.label}: the reply is not a JSON object")
        missing = [key for key in keys if key not in reply]
        if missing:
            raise TransportError(f"{self.label}: the reply has no {', '.join(missing)}")
        return reply

    def get_json(self, path: str, keys: tuple[str, ...] = (), **kw) -> dict:
        return self.json_of(self.request("GET", path, **kw), keys)

    def post_json(
        self, path: str, payload: dict, keys: tuple[str, ...] = (), **kw
    ) -> dict:
        return self.json_of(
            self.request("POST", path, json.dumps(payload).encode(), **kw), keys
        )


def host_url(cfg: dict, name: str) -> str:
    addr = host_map(cfg).get(name, name)
    return (
        addr
        if "://" in addr
        else f"http://{addr}:{remote_cfg(cfg).get('port', DEFAULT_PORT)}"
    )


# What /info must answer with for a client to go on.
INFO_KEYS = ("version", "platform")


def reach(
    cfg: dict, name: str, emit: Sink, converge: bool = True
) -> tuple[Remote, dict]:
    """A host's Remote and its /info, running this package if `converge`."""
    key = api_key(cfg)
    if key is None:
        raise ToolError(missing_key(cfg))
    remote = Remote(name, host_url(cfg, name), key)
    info = remote.get_json(INFO, INFO_KEYS)
    if converge and info["version"] != VERSION:
        emit(
            f"{name} runs {info['version']}, this is {VERSION}; installing this one there"
        )
        info = converge_version(remote, emit)
    return remote, info


def converge_version(remote: Remote, emit: Sink, wait: float = 30) -> dict:
    """Install this package on a skewed host and wait up to `wait` seconds for
    its server to restart into it."""
    reply = remote.json_of(
        remote.request(
            "POST",
            SELF_UPDATE,
            pack_sources(core.SOURCES),
            ctype="application/gzip",
            timeout=UPLOAD_TIMEOUT,
        ),
        ("version",),
    )
    deadline = time.monotonic() + wait
    answering = None
    while time.monotonic() < deadline:
        time.sleep(min(1, wait))
        try:
            info = remote.get_json(INFO, INFO_KEYS)
        except TransportError:
            continue
        if info["version"] == VERSION:
            emit(f"{remote.label} restarted into {VERSION}")
            return info
        answering = info
    if answering is None:
        raise TransportError(
            f"{remote.label} took {reply['version']} but did not come back "
            f"within {wait}s; check the server there"
        )
    raise TransportError(
        f"{remote.label} still answers as {answering['version']} after {wait}s "
        f"(pid {answering.get('pid', '?')}); another server may hold the port"
    )


def upload_batches(sizes, cap: int) -> list[list[str]]:
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


def remote_sync(cfg: dict, remote: Remote, dry_run: bool, emit: Sink) -> None:
    """Make the host's tree match this one's sync domain."""
    root = core.ROOT
    extra = sync_ignore(cfg)
    files = sync_walk(Rules(cfg), extra)
    manifest = {rel: file_hash(root / rel) for rel in files}
    reply = remote.post_json(
        MANIFEST,
        {"files": manifest, "dry": dry_run, "ignore": extra},
        ("need", "deleted"),
        timeout=UPLOAD_TIMEOUT,
    )
    need, deleted = reply["need"], reply["deleted"]
    if dry_run:
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
        data = pack_files(batch)
        remote.request(
            "POST", FILES, data, ctype="application/gzip", timeout=UPLOAD_TIMEOUT
        )
        sent += len(data)
    gone = f", deleted {len(deleted)}" if deleted else ""
    if need:
        emit(
            f"synced {len(need)} of {len(manifest)} files "
            f"({human_size(sent)} compressed){gone}"
        )
    else:
        emit(f"in sync: {len(manifest)} files{gone}")


# Told of each job a remote run starts, so whoever owns the run can stop it.
JobSink = Callable[["Remote", str], object]


def remote_script(
    cfg: dict,
    host: str,
    name: str,
    args: list[str],
    emit: Sink,
    *,
    sync: bool = True,
    watch: Sink | None = None,
    quiet: Sink | None = None,
    on_job: JobSink | None = None,
) -> int:
    """Run a settings script on a host and follow it to its exit code.

    Logged here as it arrives, to tmp/gci/logs/HOST-NAME-*.log, the same way a
    local run is. `emit` gets what belongs in the result; `quiet` gets the
    output a gauntlet's result leaves out; `watch` and the log get both.
    `on_job(remote, job_id)` hears of the host's job once there is one.
    """
    with run_output(cfg, f"{host}-{name}", emit, watch, quiet) as (out, quiet_out):
        remote, _ = reach(cfg, host, out)
        if sync:
            remote_sync(cfg, remote, False, out)
        job_id = remote.post_json(SCRIPT, {"script": name, "args": args}, ("job",))[
            "job"
        ]
        if on_job:
            on_job(remote, job_id)
        return follow_job(remote, job_id, out, quiet_out)


def transient(e: TransportError) -> bool:
    """Whether a failed request is the way there failing rather than the server
    answering: no reply at all, or a proxy that could not reach it."""
    return e.status is None or e.status in (502, 503, 504) or 520 <= e.status <= 530


def follow_job(
    remote: Remote,
    job_id: str,
    emit: Sink,
    quiet: Sink | None = None,
    reconnect: float = JOB_RECONNECT,
    pause: float = 2,
) -> int:
    """A host's job, polled until it ends: its lines to `emit`, or `quiet`
    for the quiet ones, and its exit code. A host lost for less than
    `reconnect` seconds is waited for."""
    next_line, lost_at = 0, None
    while True:
        query = urllib.parse.urlencode({"id": job_id, "from": next_line})
        try:
            reply = remote.get_json(
                f"{JOB}?{query}",
                ("lines", "next", "code"),
                timeout=JOB_POLL + CONNECT_TIMEOUT,
            )
        except TransportError as e:
            if not transient(e):
                raise
            now = time.monotonic()
            if lost_at is None:
                lost_at = now
                emit(f"lost {remote.label} ({e}); retrying for {reconnect}s")
            elif now - lost_at > reconnect:
                raise TransportError(
                    f"{remote.label} unreachable for {reconnect}s; the job there is "
                    "killed once abandoned"
                ) from e
            time.sleep(pause)
            continue
        if lost_at is not None:
            emit(f"reconnected after {time.monotonic() - lost_at:.0f}s")
            lost_at = None
        for line, is_quiet in reply["lines"]:
            if not is_quiet:
                emit(line)
            elif quiet:
                quiet(line)
        next_line = reply["next"]
        if reply["code"] is not None:
            return reply["code"]


def kill_job(remote: Remote, job_id: str) -> str | None:
    """Stop one job on a host. None once stopped, else a line saying why not."""
    try:
        remote.post_json(KILL, {"job": job_id}, timeout=60)
    except TransportError as e:
        return f"could not stop the job on {remote.label}: {e}"
    return None
