"""The settings' scripts: turning an entry into a plan, and running a plan.

An entry is one of four kinds, by the key it has:

    "command": [...]        one argv
    "steps":   [[...], ...] argvs run in order; the first failure stops it
    "run":     ["a", ...]   a gauntlet: other scripts in order, OK/FAIL each
    "forbid":  [{...}, ...] regex rules no file in the tree may break

and may also carry `description`, `args` (whether callers may add
arguments), `timeout`, `cwd`, `env`, `path` and `platforms`. Settings-wide
`env`, `path`, `timeout` and `vars` apply to every script.

`{name}` anywhere in an argument, cwd or env value is a var: the settings'
`vars` (a string, or a map from platform to string with an optional
"default"; a var may use built-ins and earlier vars), or a built-in --
python, exe (".exe" on Windows, else ""), tmp, root, platform, home. An
unknown name is left as it is, so JSON or a cargo `--config` in an argument
survives. An argument that is exactly `{args}` is where a caller's arguments
go; without one they follow the last step.
"""

import os
import re
import shlex
import sys
import time
from pathlib import Path

from . import core
from .core import (
    DEFAULT_TIMEOUT,
    Rules,
    ToolError,
    describe_scripts,
    resolve,
    tmp_rel,
)
from .sync import sync_ignore, sync_walk

KINDS = ("command", "steps", "run", "forbid")
ARGS_TOKEN = "{args}"
VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Platform names a person writes, mapped onto sys.platform's.
PLATFORM_ALIASES = {
    "macos": "darwin",
    "mac": "darwin",
    "windows": "win32",
    "win": "win32",
}
# What a plan answers when it was killed rather than exiting, as a shell would.
KILLED_EXIT = 137


class WrongPlatform(ToolError):
    """A script this host does not run. A gauntlet skips one; a direct call refuses."""


def on_platform(name):
    return sys.platform.startswith(PLATFORM_ALIASES.get(name, name))


def var_values(cfg):
    values = {
        "python": sys.executable,
        "exe": ".exe" if os.name == "nt" else "",
        "tmp": tmp_rel(cfg),
        "root": str(core.ROOT),
        "platform": sys.platform,
        "home": str(Path.home()),
    }
    for name, value in (cfg.get("vars") or {}).items():
        if isinstance(value, dict):
            chosen = next(
                (v for p, v in value.items() if p != "default" and on_platform(p)),
                value.get("default"),
            )
            if chosen is None:
                raise ToolError(
                    f"var {name!r} has no value for {sys.platform} and no default"
                )
            value = chosen
        # A var may use the built-ins and any var before it: "concinnity{exe}".
        values[name] = substitute(str(value), values)
    return values


def substitute(text, values):
    return VAR.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def argv_list(value, where):
    if not (
        isinstance(value, list) and value and all(isinstance(a, str) for a in value)
    ):
        raise ToolError(f"{where} must be a non-empty list of strings (an argv)")
    return value


class Plan:
    """A script resolved for this host: what to run, where, with what."""

    def __init__(self, name, kind, cfg):
        self.name, self.kind, self.cfg = name, kind, cfg
        self.steps, self.members, self.rules = [], [], []
        self.cwd, self.env, self.timeout = core.ROOT, None, DEFAULT_TIMEOUT
        self.keep_going = False
        self.files, self.exclude = [], []


def script_plan(cfg, name, args, nested=False):
    """The Plan for script `name` with caller `args`, or a ToolError saying why not."""
    entry = (cfg.get("scripts") or {}).get(name)
    if not isinstance(entry, dict):
        raise ToolError(
            f"no script {name!r}; the scripts are:\n{describe_scripts(cfg)}"
        )
    platforms = entry.get("platforms")
    if platforms and not any(on_platform(p) for p in platforms):
        raise WrongPlatform(
            f"script {name!r} runs only on {', '.join(platforms)}; "
            f"this host is {sys.platform}"
        )
    kinds = [k for k in KINDS if k in entry]
    if len(kinds) != 1:
        raise ToolError(
            f"script {name!r} needs exactly one of {', '.join(KINDS)}; "
            f"it has {', '.join(kinds) or 'none'}"
        )
    args = list(args or [])
    if args and not entry.get("args"):
        raise ToolError(f"script {name!r} takes no arguments")
    plan = Plan(name, kinds[0], cfg)
    values = var_values(cfg)
    plan.timeout = entry.get("timeout", cfg.get("timeout", DEFAULT_TIMEOUT))

    if plan.kind == "run":
        if nested:
            raise ToolError(
                f"script {name!r} is a gauntlet; a gauntlet cannot run another"
            )
        plan.keep_going = bool(entry.get("keep_going"))
        for member in entry["run"]:
            try:
                plan.members.append((member, script_plan(cfg, member, [], nested=True)))
            except WrongPlatform as e:
                plan.members.append((member, str(e)))
        return plan

    if plan.kind == "forbid":
        for i, rule in enumerate(entry["forbid"]):
            if not isinstance(rule, dict) or not isinstance(rule.get("pattern"), str):
                raise ToolError(f'script {name!r}: forbid[{i}] needs a "pattern"')
            try:
                rx = re.compile(rule["pattern"], re.MULTILINE)
            except re.error as e:
                raise ToolError(
                    f"script {name!r}: forbid[{i}] is a bad pattern: {e}"
                ) from None
            plan.rules.append((rx, rule.get("message") or rule["pattern"]))
        plan.files = list(entry.get("files", []))
        plan.exclude = list(entry.get("exclude", []))
        return plan

    steps = [entry["command"]] if plan.kind == "command" else entry["steps"]
    if not (isinstance(steps, list) and steps):
        raise ToolError(f"script {name!r}: steps must be a non-empty list of argvs")
    spliced = False
    for i, step in enumerate(steps):
        out = []
        for arg in argv_list(step, f"script {name!r} step {i + 1}"):
            if arg == ARGS_TOKEN:
                out += args
                spliced = True
            else:
                out.append(substitute(arg, values))
        plan.steps.append(out)
    if not spliced:
        plan.steps[-1] += args
    plan.cwd = resolve(substitute(entry.get("cwd", "."), values), Rules(cfg))
    plan.env = script_env(cfg, entry, values)
    return plan


def script_env(cfg, entry, values):
    """The environment a script runs with: this one, its `path` entries put
    ahead of PATH, and its `env` set; the settings' own first, the script's after."""
    env = dict(os.environ)
    front = [
        str(Path(substitute(p, values)).expanduser())
        for p in [*entry.get("path", []), *cfg.get("path", [])]
    ]
    if front:
        env["PATH"] = os.pathsep.join([*front, env.get("PATH", "")])
    for source in (cfg.get("env") or {}, entry.get("env") or {}):
        for key, value in source.items():
            env[str(key)] = substitute(str(value), values)
    return env


# ---- running ----


def elapsed(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}:{seconds % 60:02d}"


def run_plan(plan, emit, stop, quiet=None):
    """Run a plan, handing `emit` its output. Its exit code, or KILLED_EXIT.
    `quiet` gets the output a gauntlet's members print, which `emit` does not."""
    if plan.kind == "run":
        return run_gauntlet(plan, emit, stop, quiet)
    if plan.kind == "forbid":
        return run_forbid(plan, emit)
    from .procs import stream_command  # here, since procs imports this module

    deadline = time.monotonic() + plan.timeout if plan.timeout else None
    for step in plan.steps:
        emit(f"$ {shlex.join(step)}")
        left = max(deadline - time.monotonic(), 1) if deadline else None
        code, why = stream_command(step, plan.cwd, left, emit, stop, env=plan.env)
        if why is not None:
            emit(
                why if "timeout" not in why else f"killed after {plan.timeout}s timeout"
            )
            return KILLED_EXIT
        if code != 0:
            return code
    return 0


def run_gauntlet(plan, emit, stop, quiet=None):
    """Each member in order, one line apiece; a failure's own output under its line.
    A member's output goes to `quiet` as it lands, so it can be watched live."""
    started = time.monotonic()
    failed = 0
    ran = 0
    for member, sub in plan.members:
        if isinstance(sub, str):
            emit(f"  SKIP  {member:<16} {sub}")
            continue
        lines = []

        def member_out(line, lines=lines):
            lines.append(line)
            if quiet:
                quiet(line)

        if quiet:
            quiet(f"--- {member}")
        began = time.monotonic()
        code = run_plan(sub, member_out, stop)
        word = "OK" if code == 0 else ("KILLED" if code == KILLED_EXIT else "FAIL")
        emit(f"  {word:<5} {member:<16} {elapsed(time.monotonic() - began)}")
        ran += 1
        if code == 0:
            continue
        # The tail, because the end of a failed build is where its error is.
        for line in lines[-150:]:
            emit(f"        {line}")
        failed = failed or code
        if code == KILLED_EXIT or not plan.keep_going:
            emit(f"FAILED at {member} after {elapsed(time.monotonic() - started)}")
            return code
    took = elapsed(time.monotonic() - started)
    emit(f"{'some FAILED' if failed else f'all {ran} OK'} in {took}")
    return failed


def run_forbid(plan, emit):
    """Every break of the plan's rules in the files a sync would carry."""
    rules = Rules(plan.cfg)
    files, _ = sync_walk(rules, sync_ignore(plan.cfg))
    issues, scanned = 0, 0
    for rel in files:
        if plan.files and not core.any_match(rel, plan.files):
            continue
        if core.any_match(rel, plan.exclude):
            continue
        text = core.read_text(core.ROOT / rel)
        if text is None:
            continue
        scanned += 1
        found = []
        for rx, message in plan.rules:
            for match in rx.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                found.append((line, f"{message}: `{match.group(0).strip()[:60]}`"))
        for line, problem in sorted(found):
            emit(f"{rel}:{line}: {problem}")
        issues += len(found)
    emit(f"{issues} issue(s) in {scanned} file(s)")
    return 1 if issues else 0
