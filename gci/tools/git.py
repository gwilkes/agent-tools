"""Reading git: status, diffs, history, blame and branches. Nothing here
changes the repository, the index, or anything outside the machine.

Read-only git can still run code, so every call shuts the ways off: no
fsmonitor, no hooks (a status that refreshes the index can fire one), no
external diff or textconv filter, no pager, and optional locks off so status
never writes the index at all. A ref is checked before it reaches git, so it
cannot be an option (`--output=FILE` writes a file) or a `REV:path` read of a
denied file, and every pathspec excludes what the settings deny, so a tracked
.env never shows up in a diff.
"""

import os
import re
import shutil
import subprocess

from .. import core
from ..core import Rules, ToolError, config, rel_of, resolve
from . import tool

GIT_TIMEOUT = 60
# Ahead of every subcommand: what turns git's ways of running code off.
SAFE_CONFIG = [
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.hooksPath={os.devnull}",
    "--no-pager",
]
# Flags for anything that prints a diff.
SAFE_DIFF = ["--no-ext-diff", "--no-textconv", "--no-color"]
REF = re.compile(r"[^\s:\\]+")


def git_env():
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
    }
    env.pop("GIT_EXTERNAL_DIFF", None)
    return env


def check_ref(ref, what="ref"):
    """A revision, a range (`main..HEAD`) or a ref name, never an option or a path read."""
    if (
        not isinstance(ref, str)
        or not ref
        or ref.startswith("-")
        or ":" in ref
        or not REF.fullmatch(ref)
    ):
        raise ToolError(
            f"not a {what}: {ref!r} (a branch, tag, commit, HEAD~2 or a..b; "
            "`REV:path` and options are refused)"
        )
    return ref


def deny_pathspecs(rules):
    """The settings' deny globs as git exclude pathspecs."""
    out = []
    for pattern in rules.deny:
        glob = pattern if "/" in pattern else f"**/{pattern}"
        out.append(f":(exclude,glob){glob}")
    return out


def pathspecs(rules, paths):
    """`paths` checked against the root and the deny list, then the deny excludes."""
    own = [rel_of(resolve(p, rules)) for p in (paths or [])]
    return [*own, *deny_pathspecs(rules)]


def run_git(*args):
    if shutil.which("git") is None:
        raise ToolError("git is not on PATH")
    try:
        proc = subprocess.run(
            ["git", *SAFE_CONFIG, *args],
            cwd=core.ROOT,
            env=git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise ToolError(f"git {args[0]} took longer than {GIT_TIMEOUT}s") from None
    out = proc.stdout.decode("utf-8", errors="replace").rstrip()
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise ToolError(f"git {args[0]} failed (exit {proc.returncode}): {err or out}")
    return out


PATHS = (
    {"type": "array", "items": {"type": "string"}},
    "Only these files or directories, relative to the project root.",
    None,
)


@tool(
    """
    The working tree's state, like `git status --short --branch`: the branch and
    how far it is from its upstream, then one line per changed, staged or
    untracked file (XY codes: first column staged, second unstaged).
    """,
    paths=PATHS,
)
def git_status(paths=None):
    rules = Rules(config())
    return (
        run_git("status", "--short", "--branch", "--", *pathspecs(rules, paths))
        or "clean"
    )


@tool(
    """
    A diff, like `git diff`. Unstaged changes by default; `staged` for what is
    in the index; `base` (and `target`) to compare commits instead, e.g. base
    `main` for everything since main, or base `HEAD~3` target `HEAD`. `stat`
    for the per-file summary alone.
    """,
    staged=(
        "boolean",
        "Diff the index against HEAD (or `base`) instead of the work tree.",
        False,
    ),
    base=("string", "Commit to diff from.", None),
    target=("string", "Commit to diff to; needs base.", None),
    paths=PATHS,
    stat=("boolean", "Only the --stat summary.", False),
    context=("integer", "Lines of context.", 3),
)
def git_diff(staged=False, base=None, target=None, paths=None, stat=False, context=3):
    if target is not None and base is None:
        raise ToolError("target needs base")
    if target is not None and staged:
        raise ToolError("staged compares the index; it cannot take a target too")
    rules = Rules(config())
    args = ["diff", *SAFE_DIFF, f"-U{max(int(context), 0)}"]
    if staged:
        args.append("--cached")
    if stat:
        args.append("--stat")
    revs = [check_ref(r) for r in (base, target) if r is not None]
    return (
        run_git(*args, "--end-of-options", *revs, "--", *pathspecs(rules, paths))
        or "no differences"
    )


@tool(
    """
    Commit history, like `git log`. One line per commit (hash, date, author,
    subject) unless `full` (whole messages) or `patch` (each commit's diff).
    `ref` is where to start, or a range like `main..HEAD`; `paths` keeps
    commits that touched them.
    """,
    ref=("string", "Branch, commit or range; HEAD if omitted.", None),
    paths=PATHS,
    max_count=("integer", "At most this many commits.", 20),
    full=("boolean", "Whole commit messages.", False),
    patch=("boolean", "Each commit's diff as well.", False),
)
def git_log(ref=None, paths=None, max_count=20, full=False, patch=False):
    rules = Rules(config())
    args = [
        "log",
        "--no-color",
        f"--max-count={max(int(max_count), 1)}",
        "--date=short",
    ]
    if not full:
        args.append("--format=%h %ad %an: %s")
    if patch:
        args += ["-p", *SAFE_DIFF]
    revs = [check_ref(ref)] if ref else []
    return (
        run_git(*args, "--end-of-options", *revs, "--", *pathspecs(rules, paths))
        or "no commits"
    )


@tool(
    """
    One commit, like `git show`: its message and its diff (or `stat` for the
    summary only). Files the settings deny are left out of the diff.
    """,
    ref=("string", "The commit; HEAD if omitted.", "HEAD"),
    paths=PATHS,
    stat=("boolean", "The --stat summary instead of the diff.", False),
)
def git_show(ref="HEAD", paths=None, stat=False):
    rules = Rules(config())
    args = ["show", *SAFE_DIFF, "--date=iso"]
    if stat:
        args.append("--stat")
    return run_git(
        *args, "--end-of-options", check_ref(ref), "--", *pathspecs(rules, paths)
    )


@tool(
    """
    Who last changed each line of a file, and in which commit, like `git
    blame`. `start` and `end` limit it to a range of lines.
    """,
    path=("string", "The file, relative to the project root."),
    start=("integer", "First line, 1-based.", None),
    end=("integer", "Last line.", None),
    ref=("string", "Blame as of this commit instead of the work tree.", None),
)
def git_blame(path, start=None, end=None, ref=None):
    rules = Rules(config())
    rel = rel_of(resolve(path, rules))
    # No --no-color: blame has none (git reads it as an ambiguous abbreviation)
    # and colors only on request.
    args = ["blame", "--no-textconv", "--date=short"]
    if start is not None or end is not None:
        args.append(f"-L{max(int(start or 1), 1)},{'' if end is None else int(end)}")
    revs = [check_ref(ref)] if ref else []
    return run_git(*args, "--end-of-options", *revs, "--", rel)


@tool(
    """
    Branches, like `git branch -vv`: the current one starred, each with its
    last commit and the upstream it tracks. `all` adds remote-tracking
    branches (as last fetched; nothing is fetched).
    """,
    all=("boolean", "Include remote-tracking branches.", False),
)
def git_branch(all=False):
    args = ["branch", "--no-color", "-vv"]
    if all:
        args.append("--all")
    return run_git(*args) or "no branches"
