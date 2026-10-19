"""Text in and out: reading and writing files as UTF-8, and shaping what a
tool answers with: filtered like grep and tail, cut to fit, diffed."""

import contextlib
import difflib
import errno
import os
import re
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from .core import MAX_OUTPUT, Rules, ToolError, rel_of, resolve

MAX_LINE = 2000
KIB = 1024
BINARY_PROBE = 8192
# Read once, while the process is still single-threaded: os.umask can only be
# read by setting it.
UMASK = os.umask(0)
os.umask(UMASK)


def read_text(path: Path) -> str | None:
    """A file's text, or None if it is binary."""
    data = path.read_bytes()
    if b"\0" in data[:BINARY_PROBE]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def open_text(path: str, rules: Rules, write: bool = False) -> tuple[Path, str, str]:
    """A tool's `path` as a text file: (target, rel, text), or a ToolError
    saying why it cannot be read as one."""
    target = resolve(path, rules, write)
    rel = rel_of(target)
    if not target.is_file():
        raise ToolError(f"no such file: {rel}")
    text = read_text(target)
    if text is None:
        raise ToolError(f"{rel} is binary")
    return target, rel, text


def write_bytes(path: Path, data: bytes) -> None:
    """`data` into `path` atomically: written beside it and renamed over it, so
    a reader, or a crash mid-write, never sees half a file. An existing file
    keeps its mode, and one this process may not write is refused rather than
    replaced, as an open-and-write would refuse it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not os.access(path, os.W_OK):
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        mode = 0o666 & ~UMASK
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def clip(text: str, limit: int = MAX_OUTPUT, hint: str | None = None) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    more = f"; {hint}" if hint else ""
    return (
        f"{text[:half]}\n... {len(text) - limit} characters cut{more} ...\n"
        f"{text[-half:]}"
    )


def filter_lines(
    lines: list[str],
    grep: str | None = None,
    tail: int | None = None,
    pinned: Callable[[str], bool] | None = None,
    after: int | None = None,
) -> tuple[list[str], str | None]:
    """`lines` as `| grep -E GREP -A AFTER | tail -n TAIL` would leave them.
    Returns (lines, note), the note saying what was dropped, or None if
    nothing was. With `after`, a `--` line marks each gap, as grep's does.

    A line `pinned(line)` holds is kept wherever it is and counts toward
    neither, so a run's `log:` pointer outlives any filter.
    """
    rx, tail, after = check_filters(grep, tail, after)
    pin = pinned or (lambda _line: False)
    body = [i for i, line in enumerate(lines) if not pin(line)]
    at = {i: n for n, i in enumerate(body)}
    keep = [i for i in body if rx is None or rx.search(lines[i])]
    matched = len(keep)
    if after and rx is not None:
        extra = {j for i in keep for j in body[at[i] + 1 : at[i] + 1 + after]}
        keep = sorted(set(keep) | extra)
    shown_before_tail = len(keep)
    if tail is not None:
        keep = keep[-tail:]
    kept = set(keep)
    out, last = [], None
    for i, line in enumerate(lines):
        if pin(line):
            out.append(line)
        elif i in kept:
            if after and last is not None and at[i] != at[last] + 1:
                out.append("--")
            out.append(line)
            last = i
    parts = []
    if rx is not None:
        trailing = f", with {after} line(s) after each" if after else ""
        parts.append(f"grep `{grep}`: {matched} of {len(body)} line(s) match{trailing}")
    if len(keep) < shown_before_tail:
        parts.append(f"the last {len(keep)} of {shown_before_tail} shown")
    return out, f"[{'; '.join(parts)}]" if parts else None


def check_filters(
    grep: str | None = None, tail: int | None = None, after: int | None = None
) -> tuple[re.Pattern | None, int | None, int | None]:
    """grep, tail and after checked before any output is filtered with them, so
    a bad one is refused before a run rather than after it. Returns them as
    filter_lines uses them: grep compiled, a zero `after` as None."""
    return (
        compile_pattern(grep) if grep is not None else None,
        check_tail(tail),
        check_after(after),
    )


def check_after(after: int | None) -> int | None:
    """`after` as a count of lines, or None; a ToolError for anything else."""
    if after is None or after == 0:
        return None
    if isinstance(after, bool) or not isinstance(after, int) or after < 0:
        raise ToolError(f"after must be a number of lines, not {after!r}")
    return after


def check_tail(tail: int | None) -> int | None:
    """`tail` as a count of lines, or None; a ToolError for anything else."""
    if tail is None:
        return None
    if isinstance(tail, bool) or not isinstance(tail, int) or tail < 1:
        raise ToolError(f"tail must be a positive integer, not {tail!r}")
    return tail


def filter_text(text: str, grep: str | None = None, tail: int | None = None) -> str:
    """`text` through filter_lines, with its note on a line of its own first."""
    lines, note = filter_lines(text.splitlines(), grep, tail)
    return text if note is None else "\n".join([note, *lines])


def log_of(lines: list[str]) -> str | None:
    """The log a run's output names on its first `log: PATH` line, if any."""
    return next((line[5:] for line in lines if line.startswith("log: ")), None)


def clip_lines(lines: list[str], limit: int, see: str | None = None) -> str:
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


def unified_diff(rel: str, before: str, after: str, context: int = 2) -> str:
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


def human_size(size: float) -> str:
    for unit in ("B", "KiB", "MiB"):
        if size < KIB:
            return f"{size} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= KIB
    return f"{size:.1f} GiB"


def compile_pattern(
    pattern: str,
    ignore_case: bool = False,
    literal: bool = False,
    multiline: bool = False,
) -> re.Pattern:
    source = re.escape(pattern) if literal else pattern
    flags = (re.IGNORECASE if ignore_case else 0) | (re.MULTILINE if multiline else 0)
    try:
        return re.compile(source, flags)
    except re.error as e:
        raise ToolError(f"bad pattern {pattern!r}: {e}") from None
