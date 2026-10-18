"""Configuration, the path rules every tool obeys, and the package's own identity.

ROOT is read as `core.ROOT` everywhere, never imported by name, so the
self-test can point the whole package at a scratch tree by assigning it here.
"""

import difflib
import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
# The import name, read rather than written out, so a rename of the package
# directory reaches `-m` and the restart's import check on its own.
PKG_NAME = __name__.rpartition(".")[0]
ROOT = Path(os.environ.get("GCI_ROOT") or os.getcwd()).resolve()
CONFIG_DIR = ".gci"
CONFIG = f"{CONFIG_DIR}/settings.json"

DEFAULT_DENY = [".env", ".env.*", "secrets/**"]
DEFAULT_SKIP = [
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
]
# Changing any of these changes what the server, or the agent, is allowed to do.
ALWAYS_PROTECTED = [f"{CONFIG_DIR}/**", ".mcp.json", ".claude/**"]
DEFAULT_TIMEOUT = 600
# The scratch directory: temporary files go here, and clean_tmp empties it.
DEFAULT_TMP = "tmp"
PYTHON_TOKEN = "{python}"
# The lane that means this checkout rather than a host.
LOCAL = "local"

DEFAULT_PORT = 9001
DEFAULT_KEY_ENV = "GCI_API_KEY"
DEFAULT_KEY_FILE = "~/.gci_key"
# What one tool result may carry before its middle is cut. A result is read
# into the model's context whole, so a runaway log costs every later turn.
MAX_OUTPUT = 40_000
MAX_LINE = 2000
BINARY_PROBE = 8192


class ToolError(Exception):
    """A refusal or a failure the caller should read, not a crash."""


# ---- the package itself ----


def package_sources(base=PKG_DIR):
    """Every module of the package, as {package-relative posix path: bytes}."""
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in sorted(base.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def sources_version(sources):
    """What a client and a server compare: every module's name and bytes, hashed."""
    digest = hashlib.sha256()
    for rel in sorted(sources):
        digest.update(rel.encode() + b"\0" + sources[rel] + b"\0")
    return digest.hexdigest()[:12]


VERSION = sources_version(package_sources())


def self_command(*words):
    """The argv and environment that run this package's CLI in a fresh process,
    from wherever it lives, installed or not."""
    env = {**os.environ}
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(PKG_DIR.parent), env.get("PYTHONPATH")])
    )
    return [sys.executable, "-m", PKG_NAME, *words], env


def own_rel():
    """The package's directory relative to ROOT, if it lives inside the tree."""
    return (
        PKG_DIR.relative_to(ROOT).as_posix() if PKG_DIR.is_relative_to(ROOT) else None
    )


# ---- configuration and paths ----


def config():
    path = ROOT / CONFIG
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise ToolError(f"{CONFIG} is unreadable: {e}") from None
    if not isinstance(data, dict):
        raise ToolError(f"{CONFIG} must be an object")
    return data


def safe_config():
    try:
        return config()
    except ToolError:
        return {}


def glob_match(rel, pattern):
    """`pattern` against a root-relative posix path: without a `/` it matches a
    name at any depth, with one the whole path, and `dir/**` everything under dir."""
    if pattern.endswith("/**"):
        stem = pattern[:-3]
        return rel == stem or rel.startswith(stem + "/")
    if "/" not in pattern:
        return fnmatch.fnmatch(rel.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatch(rel, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:])
    )


def any_match(rel, patterns):
    return any(glob_match(rel, p) for p in patterns)


def rel_of(path):
    return path.relative_to(ROOT).as_posix()


class Rules:
    """The deny, protect and skip lists for one call."""

    def __init__(self, cfg):
        self.deny = list(cfg.get("deny", DEFAULT_DENY))
        self.skip = set(cfg.get("skip", DEFAULT_SKIP))
        protect = [*ALWAYS_PROTECTED, *cfg.get("protect", [])]
        # The server's own code, when it lives in the tree, unless the settings
        # (which only a person can change) say this tree is where it is developed.
        own = own_rel()
        if own is not None and not cfg.get("edit_self"):
            protect.append(f"{own}/**")
        self.protect = protect

    def denied(self, rel):
        return rel != "." and any_match(rel, self.deny)

    def protected(self, rel):
        # Any .git, at any depth (a submodule's too): its hooks and config are
        # code git runs, so writing there is running code.
        return ".git" in rel.split("/") or any_match(rel, self.protect)


def resolve(path, rules, write=False):
    """The absolute path for a tool's `path`, or a ToolError saying why not."""
    if not isinstance(path, str) or not path:
        raise ToolError("path must be a non-empty string")
    target = Path(path)
    target = (target if target.is_absolute() else ROOT / target).resolve()
    if not target.is_relative_to(ROOT):
        raise ToolError(f"outside the project root ({ROOT}): {path}")
    rel = rel_of(target)
    if rules.denied(rel):
        raise ToolError(f"denied, off limits to tools: {rel}")
    if write and rules.protected(rel):
        raise ToolError(f"protected, read-only to tools: {rel}")
    return target


def walk(base, rules, glob=None):
    """Every file under `base` a recursive tool may see, as (path, rel), in order."""
    if base.is_file():
        yield base, rel_of(base)
        return
    for dirpath, dirnames, filenames in os.walk(base):
        here = Path(dirpath)
        dirnames[:] = sorted(
            n
            for n in dirnames
            if n not in rules.skip and not rules.denied(rel_of(here / n))
        )
        for name in sorted(filenames):
            if name in rules.skip:
                continue
            path = here / name
            rel = rel_of(path)
            if rules.denied(rel) or (glob and not glob_match(rel, glob)):
                continue
            yield path, rel


# ---- text ----


def read_text(path):
    """A file's text, or None if it is binary."""
    data = path.read_bytes()
    if b"\0" in data[:BINARY_PROBE]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def write_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def clip(text, limit=MAX_OUTPUT, hint=None):
    if len(text) <= limit:
        return text
    half = limit // 2
    more = f"; {hint}" if hint else ""
    return (
        f"{text[:half]}\n... {len(text) - limit} characters cut{more} ...\n"
        f"{text[-half:]}"
    )


def filter_lines(lines, grep=None, tail=None, pinned=None):
    """`lines` as `| grep -E GREP | tail -n TAIL` would leave them. Returns
    (lines, note), the note saying what was dropped, or None if nothing was.

    A line `pinned(line)` holds is kept wherever it is and counts toward
    neither, so a run's `log:` pointer outlives any filter.
    """
    tail = check_tail(tail)
    rx = compile_pattern(grep) if grep is not None else None
    pin = pinned or (lambda _line: False)
    body = [i for i, line in enumerate(lines) if not pin(line)]
    keep = [i for i in body if rx is None or rx.search(lines[i])]
    matched = len(keep)
    if tail is not None:
        keep = keep[-tail:]
    kept = set(keep)
    out = [line for i, line in enumerate(lines) if i in kept or pin(line)]
    parts = []
    if rx is not None:
        parts.append(f"grep `{grep}`: {matched} of {len(body)} line(s) match")
    if len(keep) < matched:
        parts.append(f"the last {len(keep)} of {matched} shown")
    return out, f"[{'; '.join(parts)}]" if parts else None


def check_tail(tail):
    """`tail` as a count of lines, or None; a ToolError for anything else."""
    if tail is None:
        return None
    if isinstance(tail, bool) or not isinstance(tail, int) or tail < 1:
        raise ToolError(f"tail must be a positive integer, not {tail!r}")
    return tail


def filter_text(text, grep=None, tail=None):
    """`text` through filter_lines, with its note on a line of its own first."""
    lines, note = filter_lines(text.splitlines(), grep, tail)
    return text if note is None else "\n".join([note, *lines])


def output_limit(cfg):
    """How many characters one tool result may hold: the settings' `max_output`,
    or MAX_OUTPUT. Never below 1000, which would cut every result to nothing."""
    try:
        return max(int(cfg.get("max_output", MAX_OUTPUT)), 1000)
    except (TypeError, ValueError):
        return MAX_OUTPUT


def log_of(lines):
    """The log a run's output names on its first `log: PATH` line, if any."""
    return next((line[5:] for line in lines if line.startswith("log: ")), None)


def clip_lines(lines, limit, see=None):
    """`lines` joined, or as much of the start and of the end as fits in
    `limit` characters, with a line in between saying what was cut and where
    the whole of it is. More of the end is kept: a failed build says why last."""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    head_budget = limit // 5
    tail_budget = limit - head_budget - 200
    head, used = [], 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail, used = [], 0
    for line in reversed(lines[len(head) :]):
        if used + len(line) + 1 > tail_budget:
            break
        tail.append(line)
        used += len(line) + 1
    tail.reverse()
    cut = len(lines) - len(head) - len(tail)
    where = (
        f"; the full output is in `{see}` (grep it, or read it with a negative offset)"
        if see
        else ""
    )
    return "\n".join(
        [*head, f"... output truncated: {cut} line(s) cut{where} ...", *tail]
    )


def unified(rel, before, after, context=2):
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{rel}",
        tofile=f"b/{rel}",
        n=context,
    )
    return "".join(
        line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        for line in lines
    )


def human(size):
    for unit in ("B", "KiB", "MiB"):
        if size < 1024:
            return f"{size} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GiB"


def compile_pattern(
    pattern, ignore_case=False, literal=False, word=False, multiline=False
):
    source = re.escape(pattern) if literal else pattern
    if word:
        source = rf"\b(?:{source})\b"
    flags = (re.IGNORECASE if ignore_case else 0) | (re.MULTILINE if multiline else 0)
    try:
        return re.compile(source, flags)
    except re.error as e:
        raise ToolError(f"bad pattern {pattern!r}: {e}") from None


# ---- scripts and hosts ----


def describe_script(entry):
    """What a script does, in a few words: its command, its steps, the
    scripts a gauntlet runs, or how many rules a check holds."""
    if not isinstance(entry, dict):
        return "(not an object)"

    def argv(step):
        return shlex.join(step) if isinstance(step, list) else repr(step)

    if "run" in entry:
        what = f"runs {', '.join(map(str, entry['run']))}"
    elif "forbid" in entry:
        what = f"checks {len(entry['forbid'])} rule(s) over the tree"
    elif "steps" in entry:
        what = " && ".join(f"`{argv(s)}`" for s in entry["steps"])
    else:
        what = f"`{argv(entry.get('command', []))}`"
    if entry.get("args"):
        what += " [args...]"
    if entry.get("platforms"):
        what += f" ({', '.join(entry['platforms'])} only)"
    return what


def describe_scripts(cfg):
    scripts = cfg.get("scripts") or {}
    if not scripts:
        return "  (none)"
    rows = []
    for name, entry in sorted(scripts.items()):
        desc = entry.get("description") if isinstance(entry, dict) else ""
        rows.append(
            f"  {name}: {desc + ' -- ' if desc else ''}{describe_script(entry)}"
        )
    return "\n".join(rows)


def tmp_rel(cfg):
    return str(cfg.get("tmp") or DEFAULT_TMP).strip("/") or DEFAULT_TMP


def remote_cfg(cfg):
    remote = cfg.get("remote") or {}
    return remote if isinstance(remote, dict) else {}


def host_map(cfg):
    return remote_cfg(cfg).get("hosts") or {}


def describe_hosts(cfg):
    hosts = host_map(cfg)
    if not hosts:
        return "  (none)"
    rows = [f"  {name}: {addr}" for name, addr in hosts.items()]
    proxy = remote_cfg(cfg).get("proxy")
    if proxy:
        rows.append(f"  (off the LAN, each by {proxy.rstrip('/')}/<name>)")
    return "\n".join(rows)


def api_key(cfg):
    remote = remote_cfg(cfg)
    key = os.environ.get(remote.get("key_env", DEFAULT_KEY_ENV), "").strip()
    if not key:
        try:
            key = (
                Path(remote.get("key_file", DEFAULT_KEY_FILE))
                .expanduser()
                .read_text()
                .strip()
            )
        except OSError:
            key = ""
    return key or None


def missing_key(cfg):
    remote = remote_cfg(cfg)
    return (
        f"no API key: set {remote.get('key_env', DEFAULT_KEY_ENV)} or write it to "
        f"{remote.get('key_file', DEFAULT_KEY_FILE)}"
    )
