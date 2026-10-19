"""The settings' scripts: describing them, and turning an entry into a plan to run.

An entry is one of four kinds, by the key it has:

    "command":  [...]        one argv
    "commands": [[...], ...] argvs run in order; the first failure stops it
    "run":      ["a", ...]   a gauntlet: other scripts in order, OK/FAIL each
    "forbid":   [{...}, ...] regex rules no file in the tree may break

and may also carry `description`, `args` (whether callers may add
arguments), `cwd`, `env`, `path` and `platforms`, plus the DEFAULT_KEYS:
`timeout`, and run_script's own `hosts`, `sync`, `wait`, `grep`, `after` and
`tail`, for whatever a caller leaves out (a gauntlet's members use only their
timeout). The settings' `defaults` holds the same keys for every script; a
script's own key wins, null included, so a script can clear a shared default.
Settings-wide `env`, `path` and `vars` apply to every script, beneath its own.

`{name}` anywhere in an argument, cwd or env value is a var: the settings'
`vars` (a string, or a map from platform to string with an optional
"default"; a var may use built-ins and earlier vars), or a built-in --
python, exe (".exe" on Windows, else ""), tmp, root, platform, home. An
unknown name is left as it is, so JSON or a cargo `--config` in an argument
survives. An argument that is exactly `{args}` is where a caller's arguments
go; without one they follow the last command.
"""

import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Union

from . import core
from .core import (
    DEFAULT_TIMEOUT,
    Rules,
    ToolError,
    resolve,
    tmp_rel,
)
from .listing import block, listing
from .wire import describe_hosts, host_map

KINDS = ("command", "commands", "run", "forbid")
# Keys an older settings file used, and what a refusal tells its author.
RETIRED = {"steps": 'is now "commands"'}
# What a script, or the settings' `defaults` for every script, can set for
# the caller who leaves it out.
DEFAULT_KEYS = ("hosts", "sync", "wait", "grep", "after", "tail", "timeout")
# Every key a script may have, and the settings around it; settings.schema.json
# describes each, and a test holds the two together. A key outside these is a
# typo, refused rather than quietly ignored.
SCRIPT_KEYS = {
    *KINDS,
    *DEFAULT_KEYS,
    *("description", "args", "cwd", "env", "path", "platforms"),
    *("keep_going", "files", "exclude"),
}
SETTINGS_KEYS = {
    *("$schema", "deny", "protect", "skip", "tmp", "max_output", "edit_self"),
    *("vars", "env", "path", "defaults", "scripts", "remote"),
}
# A plan's kind for a `run` entry. The settings key stays "run", the word a
# person writes; inside, a gauntlet is kept apart from the many other runs.
GAUNTLET = "gauntlet"
ARGS_TOKEN = "{args}"
VAR = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Platform names a person writes, mapped onto sys.platform's.
PLATFORM_ALIASES = {
    "macos": "darwin",
    "mac": "darwin",
    "windows": "win32",
    "win": "win32",
}


def describe_script(entry: dict) -> str:
    """What a script does, in a few words: its commands, the scripts a
    gauntlet runs, or how many rules a check holds."""
    if not isinstance(entry, dict):
        return "(not an object)"

    def argv(command):
        return shlex.join(command) if isinstance(command, list) else repr(command)

    if "run" in entry:
        what = f"runs {', '.join(map(str, entry['run']))}"
    elif "forbid" in entry:
        what = f"checks {len(entry['forbid'])} rule(s) over the tree"
    elif isinstance(entry.get("commands"), list):
        what = " && ".join(f"`{argv(c)}`" for c in entry["commands"])
    else:
        what = f"`{argv(entry.get('command', []))}`"
    if entry.get("args"):
        what += " [args...]"
    if entry.get("platforms"):
        what += f" ({', '.join(entry['platforms'])} only)"
    defaults = describe_defaults(entry)
    if defaults:
        what += f" (default {defaults})"
    return what


def describe_defaults(given: dict) -> str:
    """The DEFAULT_KEYS `given` sets, as `wait 5, grep 'x'`; a null as `none`."""

    def shown(key, value):
        if value is None:
            return "none"
        if key == "grep":
            return repr(value)
        if key == "hosts" and isinstance(value, list):
            return ", ".join(map(str, value))
        return json.dumps(value) if isinstance(value, bool) else str(value)

    return ", ".join(
        f"{key} {shown(key, given[key])}" for key in DEFAULT_KEYS if key in given
    )


def describe_scripts(cfg: dict) -> str:
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


def scripts_and_hosts(cfg: dict) -> str:
    """The listing the scripts tool answers with."""
    shared = cfg.get("defaults")
    head = (
        f"defaults for every script: {describe_defaults(shared)}\n"
        if isinstance(shared, dict) and describe_defaults(shared)
        else ""
    )
    return f"{head}scripts:\n{describe_scripts(cfg)}\nhosts:\n{describe_hosts(cfg)}"


# A word shlex would quote only for its braces, which a var needs and a shell
# reader does not mind.
PLAIN_WORD = re.compile(r"[\w@%+=:,./{}-]+")


def shown_argv(argv: object) -> str:
    if not isinstance(argv, list):
        return repr(argv)
    return " ".join(
        a if isinstance(a, str) and PLAIN_WORD.fullmatch(a) else shlex.quote(str(a))
        for a in argv
    )


def script_rows(cfg: dict, name: str, entry: dict) -> list[tuple[str, str]]:
    """What a script runs, where, and every default it runs with, each
    marked with where it comes from when that is not the script itself."""
    rows = []
    if "run" in entry:
        rows.append(("runs", ", ".join(map(str, entry["run"]))))
        if entry.get("keep_going"):
            rows.append(("keep_going", "true: every member runs, failed or not"))
    elif "forbid" in entry:
        rules = entry["forbid"] if isinstance(entry["forbid"], list) else []
        rows.append(("checks", f"{len(rules)} rule(s) over the files a sync carries"))
        if entry.get("files"):
            rows.append(("files", ", ".join(entry["files"])))
        if entry.get("exclude"):
            rows.append(("exclude", ", ".join(entry["exclude"])))
    else:
        commands = entry.get("commands")
        if not isinstance(commands, list):
            commands = [entry.get("command")]
        rows += [("$", shown_argv(c)) for c in commands]
    for key in ("cwd", "platforms"):
        if entry.get(key):
            value = entry[key]
            rows.append((key, ", ".join(value) if isinstance(value, list) else value))
    try:
        defaults = script_defaults(cfg, name)
    except ToolError as e:
        return [*rows, ("defaults", f"unreadable: {e}")]
    defaults.setdefault("timeout", DEFAULT_TIMEOUT)
    for key in DEFAULT_KEYS:
        if key not in defaults:
            continue
        where = (
            ""
            if key in entry
            else (
                "  (from defaults)"
                if key in (cfg.get("defaults") or {})
                else "  (built in)"
            )
        )
        rows.append(
            (key, describe_defaults({key: defaults[key]}).split(" ", 1)[1] + where)
        )
    return rows


def script_listing(cfg: dict) -> str:
    """The scripts and hosts as `gci scripts` prints them: a block each."""
    blocks = []
    for name, entry in sorted((cfg.get("scripts") or {}).items()):
        if not isinstance(entry, dict):
            blocks.append(block(name, "(not an object)", []))
            continue
        head = f"{name} [ARGS...]" if entry.get("args") else name
        blocks.append(
            block(head, entry.get("description"), script_rows(cfg, name, entry))
        )
    if not blocks:
        blocks.append(block("(no scripts)", "the settings' scripts name them", []))
    hosts = host_map(cfg)
    rows = [(h, str(addr)) for h, addr in hosts.items()]
    blocks.append(
        block(
            "hosts",
            None if rows else "none: the settings' remote.hosts name them",
            rows,
        )
    )
    return listing(blocks)


class WrongPlatform(ToolError):
    """A script this host does not run. A gauntlet skips one; a direct call refuses."""


def on_platform(name: str) -> bool:
    return sys.platform.startswith(PLATFORM_ALIASES.get(name, name))


def var_values(cfg: dict) -> dict[str, str]:
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


def substitute(text: str, values: dict[str, str]) -> str:
    return VAR.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def argv_list(value: object, where: str) -> list[str]:
    if not (
        isinstance(value, list) and value and all(isinstance(a, str) for a in value)
    ):
        raise ToolError(f"{where} must be a non-empty list of strings (an argv)")
    return value


class Plan:
    """A script resolved for this host: what to run, where, with what.

    Every kind has a name, a kind and a timeout. The rest belongs to one kind
    each, and stays empty for the others: `commands`, `cwd` and `env` to
    commands; `members` and `keep_going` to a gauntlet; `rules`, `files`,
    `exclude` and `cfg` to forbid, which walks the tree as a sync would.
    """

    name: str
    # "commands" (a "command" entry too), GAUNTLET or "forbid".
    kind: str
    cfg: dict
    commands: list[list[str]]
    # (member name, its plan, or why it is skipped on this host).
    members: list[tuple[str, Union["Plan", str]]]
    # (pattern, the message for a match).
    rules: list[tuple[re.Pattern, str]]
    cwd: Path
    env: dict[str, str] | None
    timeout: float | None
    keep_going: bool
    files: list[str]
    exclude: list[str]

    def __init__(self, name: str, kind: str, cfg: dict):
        self.name, self.kind, self.cfg = name, kind, cfg
        self.commands, self.members, self.rules = [], [], []
        self.cwd, self.env, self.timeout = core.ROOT, None, DEFAULT_TIMEOUT
        self.keep_going = False
        self.files, self.exclude = [], []


def script_entry(cfg: dict, name: str) -> dict | None:
    """Script `name`'s entry in the settings, or None if there is no such script."""
    entry = (cfg.get("scripts") or {}).get(name)
    return entry if isinstance(entry, dict) else None


def script_plan(cfg: dict, name: str, args: list[str], nested: bool = False) -> Plan:
    """The Plan for script `name` with caller `args`, or a ToolError saying why not."""
    entry = script_entry(cfg, name)
    if entry is None:
        raise ToolError(
            f"no script {name!r}; the scripts are:\n{describe_scripts(cfg)}"
        )
    refuse_unknown(f"script {name!r}", entry, SCRIPT_KEYS)
    platforms = entry.get("platforms")
    if platforms and not any(on_platform(p) for p in platforms):
        raise WrongPlatform(
            f"script {name!r} runs only on {', '.join(platforms)}; "
            f"this host is {sys.platform}"
        )
    retired = [k for k in RETIRED if k in entry]
    if retired:
        raise ToolError(f"script {name!r}: {retired[0]!r} {RETIRED[retired[0]]}")
    kinds = [k for k in KINDS if k in entry]
    if len(kinds) != 1:
        raise ToolError(
            f"script {name!r} needs exactly one of {', '.join(KINDS)}; "
            f"it has {', '.join(kinds) or 'none'}"
        )
    args = list(args or [])
    if args and (not entry.get("args") or kinds[0] in ("run", "forbid")):
        raise ToolError(f"script {name!r} takes no arguments")
    kind = {"run": GAUNTLET, "command": "commands"}.get(kinds[0], kinds[0])
    plan = Plan(name, kind, cfg)
    values = var_values(cfg)
    plan.timeout = script_timeout(name, cfg)

    if plan.kind == GAUNTLET:
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

    commands = [entry["command"]] if "command" in entry else entry["commands"]
    if not (isinstance(commands, list) and commands):
        raise ToolError(f"script {name!r}: commands must be a non-empty list of argvs")
    spliced = False
    for i, command in enumerate(commands):
        out = []
        for arg in argv_list(command, f"script {name!r} command {i + 1}"):
            if arg == ARGS_TOKEN:
                out += args
                spliced = True
            else:
                out.append(substitute(arg, values))
        plan.commands.append(out)
    if not spliced:
        plan.commands[-1] += args
    plan.cwd = resolve(substitute(entry.get("cwd", "."), values), Rules(cfg))
    plan.env = script_env(cfg, entry, values)
    return plan


def script_defaults(cfg: dict, name: str) -> dict:
    """The DEFAULT_KEYS script `name` sets: its own, else the settings'
    `defaults`. A key neither sets is absent, so a null that clears one is
    told apart from nothing said. Each is checked only where it is used."""
    if "timeout" in cfg:
        raise ToolError('the settings\' "timeout" is now "defaults": {"timeout": N}')
    refuse_unknown("the settings", cfg, SETTINGS_KEYS)
    shared = cfg.get("defaults", {})
    if not isinstance(shared, dict):
        raise ToolError('the settings\' "defaults" must be an object')
    refuse_unknown('the settings\' "defaults"', shared, DEFAULT_KEYS)
    entry = script_entry(cfg, name) or {}
    return {
        k: entry[k] if k in entry else shared[k]
        for k in DEFAULT_KEYS
        if k in entry or k in shared
    }


def refuse_unknown(where: str, given: dict, known) -> None:
    unknown = sorted(set(given) - set(known) - set(RETIRED))
    if unknown:
        raise ToolError(
            f"{where}: unknown key(s) {', '.join(unknown)}; "
            f"known: {', '.join(sorted(known))}"
        )


def script_timeout(name: str, cfg: dict) -> float | None:
    """The script's own timeout, else the settings' default, else
    DEFAULT_TIMEOUT; null or 0 for none."""
    timeout = script_defaults(cfg, name).get("timeout", DEFAULT_TIMEOUT)
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
    ):
        raise ToolError(
            f"script {name!r}: timeout must be a number of seconds, not {timeout!r}"
        )
    return timeout or None


def script_env(cfg: dict, entry: dict, values: dict[str, str]) -> dict[str, str]:
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
