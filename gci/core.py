"""Configuration, the path rules every tool obeys, and the package's own identity.

ROOT is read as `core.ROOT` everywhere, never imported by name, so the
tests can point the whole package at a scratch tree by assigning it here.
"""

import fnmatch
import hashlib
import json
import os
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
# The var a script names for the interpreter running this package.
PYTHON_VAR = "{python}"
# The lane that means this checkout rather than a host.
LOCAL = "local"

# What one tool result may carry before its middle is cut. A result is read
# into the model's context whole, so a runaway log costs every later turn.
MAX_OUTPUT = 40_000


class ToolError(Exception):
    """A refusal or a failure the caller should read, not a crash."""


def package_sources(base: Path = PKG_DIR) -> dict[str, bytes]:
    """Every module of the package, as {package-relative posix path: bytes}."""
    return {
        path.relative_to(base).as_posix(): path.read_bytes()
        for path in sorted(base.rglob("*.py"))
        if "__pycache__" not in path.parts
    }


def sources_version(sources: dict[str, bytes]) -> str:
    """What a client and a server compare: every module's name and bytes, hashed."""
    digest = hashlib.sha256()
    for rel in sorted(sources):
        digest.update(rel.encode() + b"\0" + sources[rel] + b"\0")
    return digest.hexdigest()[:12]


# Snapshotted together, so a client always installs on a host exactly the
# version it then waits for, even after its own files change on disk.
SOURCES = package_sources()
VERSION = sources_version(SOURCES)


def self_command(*words: str) -> tuple[list[str], dict[str, str]]:
    """The argv and environment that run this package's CLI in a fresh process,
    from wherever it lives, installed or not."""
    env = {**os.environ}
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(PKG_DIR.parent), env.get("PYTHONPATH")])
    )
    return [sys.executable, "-m", PKG_NAME, *words], env


def own_rel() -> str | None:
    """The package's directory relative to ROOT, if it lives inside the tree."""
    return (
        PKG_DIR.relative_to(ROOT).as_posix() if PKG_DIR.is_relative_to(ROOT) else None
    )


def config() -> dict:
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


def safe_config() -> dict:
    try:
        return config()
    except ToolError:
        return {}


def glob_match(rel: str, pattern: str) -> bool:
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


def any_match(rel: str, patterns: list[str]) -> bool:
    return any(glob_match(rel, p) for p in patterns)


def rel_of(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


class Rules:
    """The deny, protect and skip lists for one call."""

    def __init__(self, cfg: dict):
        self.deny = list(cfg.get("deny", DEFAULT_DENY))
        self.skip = set(cfg.get("skip", DEFAULT_SKIP))
        protect = [*ALWAYS_PROTECTED, *cfg.get("protect", [])]
        # The server's own code, when it lives in the tree, unless the settings
        # (which only a person can change) say this tree is where it is developed.
        own = own_rel()
        if own is not None and not cfg.get("edit_self"):
            protect.append(f"{own}/**")
        self.protect = protect

    def denied(self, rel: str) -> bool:
        return rel != "." and any_match(rel, self.deny)

    def protected(self, rel: str) -> bool:
        # Any .git, at any depth (a submodule's too): its hooks and config are
        # code git runs, so writing there is running code.
        return ".git" in rel.split("/") or any_match(rel, self.protect)


def resolve(path: str, rules: Rules, write: bool = False) -> Path:
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


def output_limit(cfg: dict) -> int:
    """How many characters one tool result may hold: the settings' `max_output`,
    or MAX_OUTPUT. Never below 1000, which would cut every result to nothing."""
    try:
        return max(int(cfg.get("max_output", MAX_OUTPUT)), 1000)
    except (TypeError, ValueError):
        return MAX_OUTPUT


def tmp_rel(cfg: dict) -> str:
    return str(cfg.get("tmp") or DEFAULT_TMP).strip("/") or DEFAULT_TMP
