"""`gci serve`: what each host runs, so other machines can sync to it and run
its settings' scripts as jobs they follow.

A request that changes the tree or runs a script reads the settings strictly:
if they cannot be read, it is refused rather than run under default rules.
Only /info, which changes nothing, carries on without them."""

import hmac
import json
import os
import shlex
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import core
from .core import VERSION, Rules, ToolError, config, rel_of, safe_config
from .procs import INTERNAL_ERROR, local_script
from .scripts import script_plan
from .sync import (
    file_hash,
    prune_empty_parents,
    sync_ignore,
    sync_walk,
    unpack,
    unpack_sources,
    wire_target,
)
from .text import write_bytes
from .wire import (
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
    missing_key,
)

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

    def __init__(self, label: str):
        self.id = os.urandom(8).hex()
        self.label = label
        # [text, quiet] pairs, as /job hands them out.
        self.lines: list[list] = []
        self.code: int | None = None
        self.finished_at: float | None = None
        self.last_read = time.monotonic()
        self.stop_why: str | None = None
        self._cond = threading.Condition()

    def append(self, line: str, quiet: bool = False) -> None:
        """A line of the job's output; `quiet` for one only a watcher wants."""
        with self._cond:
            self.lines.append([line, quiet])
            self._cond.notify_all()

    def finish(self, code: int) -> None:
        with self._cond:
            self.code = code
            self.finished_at = time.monotonic()
            self._cond.notify_all()

    def read(self, start: int, wait: float) -> tuple[list[list], int | None]:
        """The lines from `start` on, waiting up to `wait` for one, and the
        exit code once there is one."""
        with self._cond:
            self.last_read = time.monotonic()
            self._cond.wait_for(
                lambda: len(self.lines) > start or self.code is not None, timeout=wait
            )
            self.last_read = time.monotonic()
            return self.lines[start:], self.code

    def stop(self, reason: str) -> None:
        self.stop_why = self.stop_why or reason

    def poll_stop(self) -> str | None:
        """What the running step polls: why to stop, a kill or a caller that
        went away, or None to carry on. Polling is what notices the caller
        going away, and stops the job then."""
        if self.stop_why is None and time.monotonic() - self.last_read > JOB_ABANDON:
            self.stop(f"killed: nobody polled for {JOB_ABANDON}s")
        return self.stop_why

    def expired(self, now: float) -> bool:
        return self.finished_at is not None and now - self.finished_at > JOB_KEEP


def own_module(own: str | None, rel: str) -> bool:
    """Whether `rel` is one of this server's own modules, which are
    /self-update's to change and never a sync's, whether writing or deleting."""
    return own is not None and rel.startswith(own + "/")


def install_sources(sources: dict[str, bytes]) -> None:
    """Make the package directory hold exactly `sources`: every module written,
    and any module the new version no longer has removed. All or nothing: a
    failure partway puts the old package back, since a server that execs a
    mix of two versions may not come back."""
    old = core.package_sources()
    try:
        for rel, data in sources.items():
            write_bytes(core.PKG_DIR / rel, data)
        for rel in old:
            if rel not in sources:
                (core.PKG_DIR / rel).unlink()
    except OSError as e:
        stuck = []
        for rel in sorted(set(old) | set(sources)):
            target = core.PKG_DIR / rel
            try:
                if rel in old:
                    write_bytes(target, old[rel])
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                stuck.append(rel)
        restored = (
            f"these could not be put back: {' '.join(stuck)}"
            if stuck
            else "the old package was put back"
        )
        raise ToolError(f"installing failed ({e}); {restored}") from None


def wire_path(rel: object, rules: Rules) -> Path:
    """The tree path a sync may write or compare for a name from the wire, or
    a ToolError. The deny list is checked against where the name leads too,
    so a host's symlink cannot carry a write onto a denied file."""
    target = wire_target(rel)
    if target is None or rules.denied(rel) or rules.denied(rel_of(target)):
        raise ToolError(f"bad path: {rel}")
    return target


class HostHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = USER_AGENT

    def log_message(self, fmt, *args):
        """Silent: slog writes one line per request, with its outcome."""

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

    def _json_body(self) -> dict:
        """The request body as a JSON object, or a ToolError, which answers 400."""
        try:
            payload = json.loads(self._body() or b"{}")
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            raise ToolError("the request body must be a JSON object")
        return payload

    def _dispatch(self, routes):
        """Each route is (handler, needs_parity, locked): whether the client
        must run this version, and whether the request holds the run lock."""
        started = time.monotonic()
        url = urllib.parse.urlsplit(self.path)
        verb = url.path.lstrip("/")
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
        handler, needs_parity, locked = route
        theirs = self.headers.get(VERSION_HEADER, "")
        # Two versions can disagree about the sync domain, and a sync across
        # that disagreement deletes what the other side still wants.
        if needs_parity and theirs != VERSION:
            self._refuse(
                412,
                f"version skew: server runs {VERSION}, client {theirs or '(none)'}",
                verb,
                "",
                started,
            )
            return
        if locked and not self._try_lock(verb, "", started):
            return
        try:
            handler(urllib.parse.parse_qs(url.query), started)
        except ToolError as e:
            self._refuse(400, str(e), verb, "", started)
        except Exception as e:  # one bad request must not take the server down
            self._json(500, {"error": f"{type(e).__name__}: {e}"})
            self.slog(verb, f"{type(e).__name__}: {e}", "FAILED", started)
        finally:
            if locked:
                self.server.run_lock.release()

    def do_GET(self):
        self._dispatch(
            {
                INFO: (self.handle_info, False, False),
                JOB: (self.handle_job, False, False),
            }
        )

    def do_POST(self):
        # /self-update and /kill skip the parity check: converging is the one
        # cross-version operation, and a kill must reach a run of any version.
        # /script takes the run lock itself, since its job holds it after the reply.
        self._dispatch(
            {
                SELF_UPDATE: (self.handle_self_update, False, True),
                KILL: (self.handle_kill, False, False),
                MANIFEST: (self.handle_manifest, True, True),
                FILES: (self.handle_files, True, True),
                SCRIPT: (self.handle_script, True, False),
            }
        )

    def handle_info(self, query, started):
        job = self.server.current_job
        self._json(
            200,
            {
                "version": VERSION,
                "platform": sys.platform,
                "root": core.ROOT.name,
                "pid": os.getpid(),
                "running": job.label if job else None,
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
            raise ToolError("from must be a line index") from None
        lines, code = job.read(start, JOB_POLL)
        self._json(200, {"lines": lines, "next": start + len(lines), "code": code})

    def _try_lock(self, verb, detail, started):
        """Take the run lock, or refuse while a job holds it. Whether it was taken."""
        if self.server.run_lock.acquire(blocking=False):
            return True
        job = self.server.current_job
        label = job.label if job else "a job"
        self._refuse(409, f"busy running {label}", verb, detail, started)
        return False

    def handle_self_update(self, query, started):
        sources = unpack_sources(self._body())
        # A server that execs a broken package does not come back, and it
        # is on the far end of the network from whoever broke it.
        for rel, data in sources.items():
            try:
                compile(data, str(core.PKG_DIR / rel), "exec")
            except SyntaxError as e:
                raise ToolError(
                    f"refusing to install an unparsable {rel}: {e}"
                ) from None
        install_sources(sources)
        version = core.sources_version(sources)
        self._json(200, {"version": version, "restarting": True})
        self.slog(
            "update", f"{len(sources)} modules -> {version}, restarting", "OK", started
        )
        self.server.restart_soon()

    def handle_manifest(self, query, started):
        payload = self._json_body()
        theirs, dry = payload.get("files"), bool(payload.get("dry", False))
        ignore = payload.get("ignore", sync_ignore({}))
        if not isinstance(theirs, dict) or not isinstance(ignore, list):
            raise ToolError("a manifest needs files, a map of path to hash")
        rules = Rules(config())
        # The client's ignore lines, not this host's: they are the domain the
        # client walked, and the difference between the two is what gets deleted.
        mine = sync_walk(rules, ignore)
        own = core.own_rel()
        need = []
        for rel, digest in theirs.items():
            target = wire_path(rel, rules)
            if own_module(own, rel):
                continue
            if not target.is_file() or file_hash(target) != digest:
                need.append(rel)
        deleted = []
        for rel in sorted(set(mine) - set(theirs)):
            if own_module(own, rel):
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
        body = self._body()
        rules = Rules(config())
        own = core.own_rel()
        # Every name is checked before any file is written, so a refused
        # batch leaves the tree as it was.
        files = []
        for rel, data, executable in unpack(body):
            target = wire_path(rel, rules)
            if own_module(own, rel) or own_module(own, rel_of(target)):
                raise ToolError(f"bad path: {rel}")
            files.append((target, data, executable))
        for target, data, executable in files:
            # Written fresh, never with the archive's mtime: an old mtime
            # hides the change from a build tool.
            write_bytes(target, data)
            if executable and os.name != "nt":
                target.chmod(target.stat().st_mode | 0o111)
        self._json(200, {"written": len(files)})
        self.slog(
            "files", f"{len(files)} written, {len(body) // 1024} KiB", "OK", started
        )

    def handle_script(self, query, started):
        req = self._json_body()
        name, args = req.get("script"), req.get("args", [])
        if not isinstance(name, str) or not isinstance(args, list):
            raise ToolError("a script request needs script, a name, and args, a list")
        args = [str(a) for a in args]
        cfg = config()
        plan = script_plan(cfg, name, args)
        server = self.server
        label = shlex.join([name, *args])
        if not self._try_lock("script", label, started):
            return
        job = server.add_job(label)
        self.slog("script", f"{label} (job {job.id})")

        def emit(line):
            job.append(line)
            print(f"{'':25} {line}", flush=True)

        def quiet(line):
            job.append(line, quiet=True)

        def run():
            code = INTERNAL_ERROR
            try:
                code = local_script(cfg, plan, emit, stop=job.poll_stop, quiet=quiet)
            finally:
                # Free the host before the caller hears the code, so its next request is not refused.
                server.current_job = None
                server.run_lock.release()
                job.finish(code)
                self.slog(
                    "script", label, "OK" if code == 0 else f"exit {code}", started
                )

        server.current_job = job
        threading.Thread(target=run, daemon=True).start()
        self._json(200, {"job": job.id})

    def handle_kill(self, query, started):
        wanted = self._json_body().get("job")
        job = self.server.current_job
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


class HostServer(ThreadingHTTPServer):
    """Windows' SO_REUSEADDR lets a second process bind a port that is still
    listening, so a restart could leave two servers answering; exclusive use
    makes the second bind fail instead."""

    allow_reuse_address = os.name != "nt"

    def __init__(self, address, key):
        super().__init__(address, HostHandler)
        self.key = key
        self.run_lock = threading.Lock()
        self.current_job: Job | None = None
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
    key = api_key(cfg)
    if key is None:
        # This server runs what its settings say, and a sync can rewrite them.
        print(
            f"gci server: refusing to serve without a key; {missing_key(cfg)}",
            file=sys.stderr,
        )
        return 2
    try:
        server = HostServer((bind, port), key)
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
        os.execv(sys.executable, [sys.executable, *sys.orig_argv[1:]])  # nosec B606
    return 0
