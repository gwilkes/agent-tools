"""`gci serve`: what each host runs, so other machines can sync to it and run
its .gci/settings.json scripts as jobs they follow."""

import hmac
import io
import json
import os
import shlex
import sys
import tarfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core
from .core import VERSION, Rules, ToolError, safe_config
from .procs import local_script
from .scripts import script_plan
from .sync import (
    file_hash,
    prune_empty_parents,
    sync_ignore,
    sync_walk,
    unpack_sources,
    wire_target,
)

KEY_HEADER = "X-GCI-Key"
VERSION_HEADER = "X-GCI-Version"
# Marks a job line that is for watching, not for the run's result.
LIVE_PREFIX = "##gci-live "
# Cloudflare's browser integrity check refuses a request that names no agent.
USER_AGENT = "agent-tools"
# How long a /job poll waits for a line before answering with none.
JOB_POLL = 25
# A job nobody has polled for this long is killed; its client is gone.
JOB_ABANDON = 600
# A finished job's output is kept this long for a client still catching up.
JOB_KEEP = 3600


class Job:
    """A script running on the server and every line it has printed, read by index.

    The caller follows it over many short requests rather than one long one,
    so a dropped connection loses nothing: the next read starts where the
    last one ended.
    """

    def __init__(self, label):
        self.id = os.urandom(8).hex()
        self.label = label
        self.lines = []
        self.code = None
        self.finished_at = None
        self.last_read = time.monotonic()
        self.stop_reason = None
        self._cond = threading.Condition()

    def append(self, line):
        with self._cond:
            self.lines.append(line)
            self._cond.notify_all()

    def finish(self, code):
        with self._cond:
            self.code = code
            self.finished_at = time.monotonic()
            self._cond.notify_all()

    def read(self, start, wait):
        """The lines from `start` on, waiting up to `wait` for one, and the
        exit code once there is one."""
        with self._cond:
            self.last_read = time.monotonic()
            self._cond.wait_for(
                lambda: len(self.lines) > start or self.code is not None, timeout=wait
            )
            self.last_read = time.monotonic()
            return self.lines[start:], self.code

    def stop(self, reason):
        self.stop_reason = self.stop_reason or reason

    def should_stop(self):
        """What the running step polls: a kill, or a caller that went away."""
        if self.stop_reason is None and time.monotonic() - self.last_read > JOB_ABANDON:
            self.stop(f"killed: nobody polled for {JOB_ABANDON}s")
        return self.stop_reason

    def expired(self, now):
        return self.finished_at is not None and now - self.finished_at > JOB_KEEP


def install_sources(sources):
    """Make the package directory hold exactly `sources`: every module written,
    and any module the new version no longer has removed."""
    for rel, data in sources.items():
        target = core.PKG_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    for rel in core.package_sources():
        if rel not in sources:
            (core.PKG_DIR / rel).unlink()


class CiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = USER_AGENT

    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        self.slog("error", fmt % args)

    def slog(self, verb, detail="", result=None, started=None):
        took = "" if started is None else f" {time.monotonic() - started:.1f}s"
        tail = "" if result is None else f"  {result}{took}"
        print(
            f"{time.strftime('%H:%M:%S')} {self.client_address[0]:<15} {verb:<9} {detail}{tail}",
            flush=True,
        )

    def _send(self, code, body, ctype):
        if code >= 400:
            self._drain()
            self.close_connection = True
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, payload):
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _body(self):
        self.body_read = True
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def _drain(self):
        """A refusal still has to consume the request body, or the next read on
        a kept-alive connection parses the leftover bytes as a new request."""
        if getattr(self, "body_read", False):
            return
        self.body_read = True
        remaining = int(self.headers.get("Content-Length", 0))
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            remaining -= len(chunk)

    def _refuse(self, code, error, verb, detail, started):
        self._json(code, {"error": error})
        self.slog(verb, detail, f"REFUSED {error}", started)

    def _dispatch(self, routes):
        started = time.monotonic()
        url = urllib.parse.urlsplit(self.path)
        if not hmac.compare_digest(
            self.headers.get(KEY_HEADER, "").encode(), self.server.key.encode()
        ):
            self._refuse(
                401, "missing or wrong API key", self.command, url.path, started
            )
            return
        route = routes.get(url.path)
        if route is None:
            self._refuse(404, "unknown endpoint", self.command, url.path, started)
            return
        handler, needs_parity = route
        theirs = self.headers.get(VERSION_HEADER, "")
        # Two versions can disagree about the sync domain, and a sync across
        # that disagreement deletes what the other side still wants.
        if needs_parity and theirs != VERSION:
            self._refuse(
                412,
                f"version skew: server runs {VERSION}, client {theirs or '(none)'}",
                url.path.lstrip("/"),
                "",
                started,
            )
            return
        try:
            handler(urllib.parse.parse_qs(url.query), started)
        except Exception as e:  # one bad request must not take the server down
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
            self.slog(
                url.path.lstrip("/"), f"{type(e).__name__}: {e}", "FAILED", started
            )

    def do_GET(self):
        self._dispatch(
            {"/info": (self.handle_info, False), "/job": (self.handle_job, False)}
        )

    def do_POST(self):
        # /self-update and /kill skip the parity check: converging is the one
        # cross-version operation, and a kill must reach a run of any version.
        self._dispatch(
            {
                "/self-update": (self.handle_self_update, False),
                "/kill": (self.handle_kill, False),
                "/manifest": (self.handle_manifest, True),
                "/files": (self.handle_files, True),
                "/script": (self.handle_script, True),
            }
        )

    def handle_info(self, query, started):
        running = self.server.running
        self._json(
            200,
            {
                "version": VERSION,
                "platform": sys.platform,
                "root": core.ROOT.name,
                "pid": os.getpid(),
                "running": running.label if running else None,
                "scripts": sorted(safe_config().get("scripts") or {}),
            },
        )

    def handle_job(self, query, started):
        job = self.server.jobs.get(query.get("id", [""])[0])
        if job is None:
            self._json(404, {"error": "no such job; the server may have restarted"})
            return
        try:
            start = max(0, int(query.get("from", ["0"])[0]))
        except ValueError:
            self._json(400, {"error": "from must be a line index"})
            return
        lines, code = job.read(start, JOB_POLL)
        self._json(200, {"lines": lines, "next": start + len(lines), "code": code})

    def _locked(self, verb, started, fn):
        """Run `fn` under the run lock, or refuse while a job holds it."""
        if not self.server.run_lock.acquire(blocking=False):
            running = self.server.running
            self._refuse(
                409,
                f"busy running {running.label if running else 'a job'}",
                verb,
                "",
                started,
            )
            return
        try:
            fn()
        finally:
            self.server.run_lock.release()

    def handle_self_update(self, query, started):
        def update():
            try:
                sources = unpack_sources(self._body())
            except ValueError as e:
                self._refuse(400, str(e), "update", "", started)
                return
            # A server that execs a broken package does not come back, and it
            # is on the far end of the network from whoever broke it.
            for rel, data in sources.items():
                try:
                    compile(data, str(core.PKG_DIR / rel), "exec")
                except SyntaxError as e:
                    self._refuse(
                        400,
                        f"refusing to install an unparsable {rel}: {e}",
                        "update",
                        "",
                        started,
                    )
                    return
            install_sources(sources)
            version = core.sources_version(sources)
            self._json(200, {"version": version, "restarting": True})
            self.slog(
                "update",
                f"{len(sources)} modules -> {version}, restarting",
                "OK",
                started,
            )
            self.server.restart_soon()

        self._locked("update", started, update)

    def handle_manifest(self, query, started):
        self._locked("manifest", started, lambda: self._manifest(started))

    def _manifest(self, started):
        payload = json.loads(self._body())
        theirs, dry = payload["files"], payload.get("dry", False)
        rules = Rules(safe_config())
        # The client's ignore lines, not this host's: they are the domain the
        # client walked, and the difference between the two is what gets deleted.
        mine, _ = sync_walk(rules, payload.get("ignore", sync_ignore({})))
        # This server's own modules are /self-update's to change, never a sync's.
        own = core.own_rel()
        need = []
        for rel, digest in theirs.items():
            target = wire_target(rel)
            if target is None or rules.denied(rel):
                self._refuse(400, f"bad path: {rel}", "manifest", "", started)
                return
            if not target.is_file() or file_hash(target) != digest:
                need.append(rel)
        deleted = []
        for rel in sorted(set(mine) - set(theirs)):
            if own is not None and rel.startswith(own + "/"):
                continue
            if not dry:
                (core.ROOT / rel).unlink()
                prune_empty_parents(core.ROOT / rel)
            deleted.append(rel)
        self._json(200, {"need": need, "deleted": deleted, "dry": dry})
        verb = "to delete" if dry else "deleted"
        self.slog(
            "manifest",
            f"{len(theirs)} offered, {len(need)} to send, {len(deleted)} {verb}",
            "dry-run" if dry else "OK",
            started,
        )

    def handle_files(self, query, started):
        self._locked("files", started, lambda: self._files(started))

    def _files(self, started):
        body = self._body()
        rules = Rules(safe_config())
        written = 0
        try:
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
                for member in tar:
                    if not member.isfile():
                        continue
                    target = wire_target(member.name)
                    if target is None or rules.denied(member.name):
                        self._refuse(
                            400, f"bad path: {member.name}", "files", "", started
                        )
                        return
                    # Written fresh, never with the archive's mtime: an old
                    # mtime hides the change from a build tool.
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(tar.extractfile(member).read())
                    if member.mode & 0o111 and os.name != "nt":
                        target.chmod(target.stat().st_mode | 0o111)
                    written += 1
        except tarfile.TarError as e:
            self._refuse(400, f"bad archive: {e}", "files", "", started)
            return
        self._json(200, {"written": written})
        self.slog("files", f"{written} written, {len(body) // 1024} KiB", "OK", started)

    def handle_script(self, query, started):
        req = json.loads(self._body())
        name, args = req.get("script"), [str(a) for a in req.get("args", [])]
        cfg = safe_config()
        try:
            script_plan(cfg, name, args)
        except ToolError as e:
            self._refuse(400, str(e), "script", str(name), started)
            return
        server = self.server
        label = shlex.join([name, *args])
        if not server.run_lock.acquire(blocking=False):
            running = server.running
            self._refuse(
                409,
                f"busy running {running.label if running else 'a job'}",
                "script",
                label,
                started,
            )
            return
        job = server.add_job(label)
        self.slog("script", f"{label} (job {job.id})")

        def emit(line):
            job.append(line)
            print(f"{'':25} {line}", flush=True)

        def quiet(line):
            job.append(LIVE_PREFIX + line)

        def run():
            code = 1
            try:
                code = local_script(cfg, name, args, emit, job.should_stop, quiet=quiet)
            except ToolError as e:
                emit(str(e))
                code = 127
            except Exception as e:
                emit(f"server error: {type(e).__name__}: {e}")
            finally:
                # Free the host before the caller hears the code, so its next request is not refused.
                server.running = None
                server.run_lock.release()
                job.finish(code)
                self.slog(
                    "script", label, "OK" if code == 0 else f"exit {code}", started
                )

        server.running = job
        threading.Thread(target=run, daemon=True).start()
        self._json(200, {"job": job.id})

    def handle_kill(self, query, started):
        body = self._body()
        wanted = (json.loads(body) if body else {}).get("job")
        job = self.server.running
        lines = []
        if job is None or wanted not in (None, job.id):
            lines.append("nothing running")
        else:
            job.stop(f"killed by gci kill from {self.client_address[0]}")
            lines.append(f"stopped {job.label}")
            if self.server.run_lock.acquire(timeout=30):
                self.server.run_lock.release()
                lines.append("the host is free")
            else:
                lines.append("the job has not let go of the host after 30s")
        self._json(200, {"lines": lines})
        self.slog("kill", "; ".join(lines), "OK", started)


class CiServer(ThreadingHTTPServer):
    """Windows' SO_REUSEADDR lets a second process bind a port that is still
    listening, so a restart could leave two servers answering; exclusive use
    makes the second bind fail instead."""

    allow_reuse_address = os.name != "nt"

    def __init__(self, address, key):
        super().__init__(address, CiHandler)
        self.key = key
        self.run_lock = threading.Lock()
        self.running = None
        self.jobs = {}
        self.restarting = False
        self._jobs_lock = threading.Lock()

    def server_bind(self):
        if os.name == "nt":
            import socket

            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request, client_address):
        # A caller that hung up mid-reply asks again on its next poll.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

    def add_job(self, label):
        now = time.monotonic()
        with self._jobs_lock:
            for job_id in [i for i, j in self.jobs.items() if j.expired(now)]:
                del self.jobs[job_id]
            job = Job(label)
            self.jobs[job.id] = job
        return job

    def restart_soon(self):
        # After the response has flushed. The exec happens on the main thread
        # once the socket is closed: on Windows `execv` starts a new process
        # rather than replacing this one, and it cannot bind the port while
        # this one still holds it.
        def go():
            time.sleep(0.5)
            self.restarting = True
            self.shutdown()

        threading.Thread(target=go, daemon=True).start()


def serve(port, bind):
    cfg = safe_config()
    key = core.api_key(cfg)
    if key is None:
        # This server runs what its settings say, and a sync can rewrite them.
        print(
            f"gci server: refusing to serve without a key; {core.missing_key(cfg)}",
            file=sys.stderr,
        )
        return 2
    try:
        server = CiServer((bind, port), key)
    except OSError as e:
        print(
            f"gci server: cannot bind port {port}: {e}; is another gci server still running?",
            file=sys.stderr,
        )
        return 1
    print(
        f"gci server: port {server.server_address[1]}, root {core.ROOT}, version {VERSION}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    if server.restarting:
        print(
            f"{time.strftime('%H:%M:%S')} restarting into the updated package",
            flush=True,
        )
        # orig_argv keeps `-m gci`, which a rebuilt argv from sys.argv would lose.
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])
    return 0
