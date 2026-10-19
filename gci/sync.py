"""The sync domain: what of this tree travels to a host, the gzipped tars
files travel in, and how a path from the wire is checked before anything is
written there."""

import hashlib
import io
import os
import re
import tarfile
from pathlib import Path

from . import core
from .core import ToolError
from .wire import remote_cfg


def _gitignore_regex(pat: str) -> str:
    """One gitignore pattern as a regex source: `*` and `?` stay within a
    name, `**/` spans directories."""
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return "".join(out)


class IgnoreRules:
    """One .gitignore's worth of patterns."""

    def __init__(self, lines):
        self.rules = []
        for raw in lines:
            line = raw.rstrip("\n").rstrip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:]
            dir_only = line.endswith("/")
            line = line.rstrip("/")
            anchored = line.startswith("/") or "/" in line
            line = line.lstrip("/")
            rx = _gitignore_regex(line)
            if not anchored:
                rx = r"(?:.*/)?" + rx
            self.rules.append((negated, dir_only, re.compile(rx + r"\Z")))

    def match(self, rel, is_dir):
        """True ignored, False re-included, None if no rule had an opinion."""
        state = None
        for negated, dir_only, rx in self.rules:
            if dir_only and not is_dir:
                continue
            if rx.match(rel):
                state = not negated
        return state


def gitignore_lines(directory: Path) -> list[str]:
    try:
        return (directory / ".gitignore").read_text().splitlines()
    except OSError:
        return []


def ignored_by(layers: list, rel: str, is_dir: bool) -> bool:
    """Last match wins, and a deeper .gitignore is consulted after a shallower one."""
    state = False
    for base, rules in layers:
        if rel.startswith(base):
            verdict = rules.match(rel[len(base) :], is_dir)
            if verdict is not None:
                state = verdict
    return state


def sync_ignore(cfg: dict) -> list[str]:
    """The lines read after the root .gitignore. The scratch directory never
    travels, .gitignore or not: its logs are each machine's own, and a host's
    would otherwise be deleted by the next sync. .gci/ is re-included last, so
    neither a `.*` rule nor a user's own `ignore` can keep the settings from a
    host: a host without them cannot run the scripts it is asked to."""
    return [
        ".git/",
        f"/{core.tmp_rel(cfg)}/",
        *remote_cfg(cfg).get("ignore", []),
        f"!/{core.CONFIG_DIR}/",
    ]


def sync_walk(rules: core.Rules, extra: list[str]) -> list[str]:
    """The files a sync carries.

    The tree as each .gitignore in it filters it, `extra` lines after the
    root's, without symlinks (a link's target is not the tree's to send) or
    anything denied.
    """
    root = core.ROOT
    layers_at = {"": [("", IgnoreRules(gitignore_lines(root) + list(extra)))]}
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_dir = here.relative_to(root).as_posix()
        prefix = "" if rel_dir == "." else rel_dir + "/"
        layers = layers_at.pop(prefix, None)
        if layers is None:
            dirnames[:] = []
            continue
        if prefix:
            nested = gitignore_lines(here)
            if nested:
                layers = layers + [(prefix, IgnoreRules(nested))]
        kept = []
        for name in sorted(dirnames):
            rel = prefix + name
            if (
                (here / name).is_symlink()
                or rules.denied(rel)
                or ignored_by(layers, rel, True)
            ):
                continue
            kept.append(name)
            layers_at[rel + "/"] = layers
        dirnames[:] = kept
        for name in sorted(filenames):
            rel = prefix + name
            if (
                (here / name).is_symlink()
                or rules.denied(rel)
                or ignored_by(layers, rel, False)
            ):
                continue
            files.append(rel)
    return files


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_parts(rel: object) -> bool:
    """Whether a relative posix path from the wire names only its own subtree."""
    return (
        isinstance(rel, str)
        and bool(rel)
        and not rel.startswith("/")
        and "\\" not in rel
        and ":" not in rel
        and all(p not in ("", ".", "..") for p in rel.split("/"))
    )


def wire_target(rel: str) -> Path | None:
    """The path a relative name from the wire means under ROOT, or None if it
    could mean anywhere else."""
    if not safe_parts(rel):
        return None
    target = (core.ROOT / rel).resolve()
    return target if target.is_relative_to(core.ROOT) else None


def pack(entries: list[tuple[str, bytes, bool]]) -> bytes:
    """(rel, bytes, executable) entries as the gzipped tar the wire carries."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel, data, executable in entries:
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = 0o755 if executable else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def unpack(data: bytes) -> list[tuple[str, bytes, bool]]:
    """The (rel, bytes, executable) files of a gzipped tar from the wire,
    every name checked with safe_parts before any is returned, or a
    ToolError saying what is wrong with it."""
    entries = []
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                if not safe_parts(member.name):
                    raise ToolError(f"bad path: {member.name}")
                entries.append(
                    (
                        member.name,
                        tar.extractfile(member).read(),
                        bool(member.mode & 0o111),
                    )
                )
    except tarfile.TarError as e:
        raise ToolError(f"bad archive: {e}") from None
    return entries


def pack_files(rels: list[str]) -> bytes:
    """Tree files, by rel, as a /files body, with whether each is executable."""
    return pack(
        [
            (
                rel,
                (core.ROOT / rel).read_bytes(),
                bool((core.ROOT / rel).stat().st_mode & 0o111),
            )
            for rel in rels
        ]
    )


def pack_sources(sources: dict[str, bytes]) -> bytes:
    """The package's modules ({rel: bytes}) as the gzipped tar /self-update takes."""
    return pack([(rel, data, False) for rel, data in sorted(sources.items())])


def unpack_sources(data: bytes) -> dict[str, bytes]:
    """{rel: bytes} from a /self-update body, or a ToolError saying what is wrong with it."""
    sources = {}
    for rel, module, _ in unpack(data):
        if not rel.endswith(".py"):
            raise ToolError(f"not a module path: {rel}")
        sources[rel] = module
    missing = {"__init__.py", "__main__.py"} - set(sources)
    if missing:
        raise ToolError(f"not this package: no {' or '.join(sorted(missing))}")
    return sources


def prune_empty_parents(path: Path) -> None:
    """Remove the directories a deleted file leaves empty, up to the root."""
    parent = path.parent
    while parent != core.ROOT and parent.is_dir() and not any(parent.iterdir()):
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent
