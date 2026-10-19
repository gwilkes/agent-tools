"""A run's output: the line sinks it goes to, and its log under tmp/, which
local and remote runs keep the same way."""

import itertools
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .core import Rules, ToolError, rel_of, resolve, tmp_rel

# Where each run's log goes, under the settings' tmp directory.
LOG_DIR = "gci/logs"

# A line of output goes to a sink; a stop callback answers why to stop, or None.
Sink = Callable[[str], object]
Reason = Callable[[], str | None]


def tee(*sinks: Sink | None) -> Sink:
    """One line sink that hands each line to every sink given (None ones skipped)."""
    sinks = [s for s in sinks if s is not None]

    def send(line):
        for sink in sinks:
            sink(line)

    return send


class RunLog:
    """A run's output as it lands, in a file under tmp/ a person can follow
    with `tail -f` or the read tool while the run is still going.

    Line-buffered, so each line is on disk as soon as it is written. If tmp/
    is not somewhere this tree may write, there is no log and the run goes on.
    """

    def __init__(self, cfg: dict, label: str):
        self.file, self.rel = None, None
        self._lock = threading.Lock()
        try:
            base = resolve(f"{tmp_rel(cfg)}/{LOG_DIR}", Rules(cfg), write=True)
            base.mkdir(parents=True, exist_ok=True)
        except (ToolError, OSError):
            return
        stem = f"{re.sub(r'[^A-Za-z0-9._-]+', '_', label)}-{time.strftime('%Y%m%d-%H%M%S')}"
        for n in itertools.count():
            path = base / (f"{stem}.log" if n == 0 else f"{stem}-{n}.log")
            try:
                self.file = open(path, "x", encoding="utf-8", buffering=1)
            except FileExistsError:
                continue
            except OSError:
                return
            self.rel = rel_of(path)
            return

    def write(self, line):
        # Under the lock close() takes: a reader thread that outlives its run
        # (a grandchild holding the pipe) drops its late lines, not raises.
        with self._lock:
            if self.file is not None:
                self.file.write(line + "\n")

    def close(self):
        with self._lock:
            if self.file is not None:
                self.file.close()
                self.file = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@contextmanager
def run_output(
    cfg: dict,
    label: str,
    emit: Sink,
    watch: Sink | None = None,
    quiet: Sink | None = None,
) -> Iterator[tuple[Sink, Sink]]:
    """A run's two line sinks, (out, quiet_out): `out` for what belongs in the
    result, `quiet_out` for the quiet output, which a gauntlet's result leaves
    out. Both reach the run's log and `watch`; the log is named first, on a
    `log:` line."""
    with RunLog(cfg, label) as log:
        out = tee(log.write, watch, emit)
        quiet_out = tee(log.write, watch, quiet)
        if log.rel:
            out(f"log: {log.rel}")
        yield out, quiet_out
