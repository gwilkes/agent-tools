"""Live output during a tool call, as MCP progress notifications.

A tool result arrives whole when the call ends; MCP has no streamed result.
What it does have is `notifications/progress`: a client that sends a
`progressToken` in a call's `_meta` may be told, while the call runs, how it
is going. Whether and how a client shows those messages is the client's
business, so everything here is best-effort -- the run's log file under tmp/
is the dependable live view.

A cargo build prints thousands of lines a minute, so messages are rate
limited by a token bucket: bursts get through, a flood is thinned. A line
that reports a result (a gauntlet's OK/FAIL, a lane's header) always goes.
"""

import contextvars
import re
import threading
import time

# The reporter for the tool call running in this thread, or None.
CURRENT = contextvars.ContextVar("gci_progress", default=None)
# Lines that are never dropped, with or without a `[host] ` prefix.
ALWAYS = re.compile(
    r"^(?:\[[^\]]+\] )?(?:\s*(?:OK|FAIL|KILLED|SKIP)\s|FAILED at |all \d+ OK|"
    r"some FAILED|log: )"
)
BURST = 20
PER_SECOND = 4
MAX_MESSAGE = 500


class Progress:
    """Sends one tool call's progress notifications through `notify`."""

    def __init__(self, token, notify):
        self.token, self.notify = token, notify
        self.sent = 0
        self.tokens = float(BURST)
        self.stamp = time.monotonic()
        self.lock = threading.Lock()

    def __call__(self, line):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(BURST, self.tokens + (now - self.stamp) * PER_SECOND)
            self.stamp = now
            if self.tokens < 1 and not ALWAYS.match(line):
                return
            self.tokens = max(self.tokens - 1, 0)
            self.sent += 1
            count = self.sent
        self.notify(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {
                    "progressToken": self.token,
                    "progress": count,
                    "message": line[:MAX_MESSAGE],
                },
            }
        )


def current():
    """The reporter for this tool call, if its client asked for progress."""
    return CURRENT.get()
