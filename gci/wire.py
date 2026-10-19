"""What a client and a host agree on: the HTTP protocol between them, and the
settings' `remote` section, which says where the hosts are and how the key
they share is found.

Every request carries the key, and all but /info, /job, /kill and
/self-update the client's version, which must match the host's. The bodies
are JSON unless named otherwise:

    GET  /info              -> {version, platform, root, pid, running, scripts}
    GET  /job?id=&from=     -> {lines, next, code}; lines are [text, quiet]
                               pairs, quiet for output only a watcher wants
    POST /self-update       gzipped tar of the package -> {version, restarting}
    POST /kill {job?}       -> {lines}
    POST /manifest {files: {rel: sha256}, dry, ignore} -> {need, deleted, dry}
    POST /files             gzipped tar of tree files -> {written}
    POST /script {script, args} -> {job}
"""

import os
from pathlib import Path

from .core import ToolError

KEY_HEADER = "X-GCI-Key"
VERSION_HEADER = "X-GCI-Version"
# Cloudflare's browser integrity check refuses a request that names no agent.
USER_AGENT = "agent-tools"
# How long a /job poll waits for a line before answering with none.
JOB_POLL = 25

INFO = "/info"
JOB = "/job"
SELF_UPDATE = "/self-update"
KILL = "/kill"
MANIFEST = "/manifest"
FILES = "/files"
SCRIPT = "/script"

DEFAULT_PORT = 9001
DEFAULT_KEY_ENV = "GCI_API_KEY"
DEFAULT_KEY_FILE = "~/.gci_key"


def remote_cfg(cfg: dict) -> dict:
    remote = cfg.get("remote") or {}
    return remote if isinstance(remote, dict) else {}


def host_map(cfg: dict) -> dict[str, str]:
    return remote_cfg(cfg).get("hosts") or {}


def describe_hosts(cfg: dict) -> str:
    hosts = host_map(cfg)
    if not hosts:
        return "  (none)"
    return "\n".join(f"  {name}: {addr}" for name, addr in hosts.items())


def api_key(cfg: dict) -> str | None:
    """The shared key from the environment, else the key file; None when
    neither has one. A key file that exists but cannot be read is a ToolError,
    since the advice for a missing key would be wrong."""
    remote = remote_cfg(cfg)
    key = os.environ.get(remote.get("key_env", DEFAULT_KEY_ENV), "").strip()
    if not key:
        path = Path(remote.get("key_file", DEFAULT_KEY_FILE)).expanduser()
        try:
            key = path.read_text().strip()
        except FileNotFoundError:
            key = ""
        except (OSError, UnicodeDecodeError) as e:
            raise ToolError(f"{path} is unreadable: {e}") from None
    return key or None


def missing_key(cfg: dict) -> str:
    remote = remote_cfg(cfg)
    return (
        f"no API key: set {remote.get('key_env', DEFAULT_KEY_ENV)} or write it to "
        f"{remote.get('key_file', DEFAULT_KEY_FILE)}"
    )
