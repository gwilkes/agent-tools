"""The `gci` command line: a word per tool, built from its schema, plus `run`,
`serve` and `mcp`, as the package docstring lists."""

import argparse
import json
import signal
import sys

from . import __doc__ as PACKAGE_DOC
from .core import LOCAL, ToolError, config, output_limit, safe_config
from .lanes import Lane, exit_text
from .runs import start_run, stop_runs
from .scripts import script_defaults
from .text import clip
from .wire import DEFAULT_PORT, remote_cfg

# A CLI run where some lane has no exit code: a host unreachable, refused or lost.
TRANSPORT_EXIT = 3
# What a run stopped by Ctrl-C answers, as a shell would.
INTERRUPTED_EXIT = 130
# How long Ctrl-C waits for the lanes to take their process trees down.
STOP_WAIT = 5


def cli_code(results: list[Lane]) -> int:
    """1 if a script failed anywhere, else TRANSPORT_EXIT if a lane has no exit code, else 0."""
    codes = [lane.code for lane in results]
    if any(code not in (0, None) for code in codes):
        return 1
    return TRANSPORT_EXIT if None in codes else 0


def run_word(args: argparse.Namespace) -> int:
    """`gci run`: the script here, or on each --host at once, printed live.
    At a terminal a person is watching, so a gauntlet's members print too.
    The script's default hosts and sync apply as they do to run_script."""
    cfg = config()
    defaults = script_defaults(cfg, args.name)
    hosts = args.host or defaults.get("hosts")
    sync = not args.no_sync and defaults.get("sync", True) is not False
    run = start_run(
        cfg,
        args.name,
        args.args,
        hosts,
        sync,
        [lambda line: print(line, flush=True)],
    )
    try:
        # A timed wait, so Ctrl-C lands here rather than in a blocked wait.
        while not run.done.wait(0.5):
            pass
    except KeyboardInterrupt:
        # A step runs in a session of its own, so the terminal's SIGINT never
        # reaches it; it hears of the stop the way an MCP run does.
        for line in run.stop("interrupted"):
            print(line, file=sys.stderr)
        run.done.wait(STOP_WAIT)
        return INTERRUPTED_EXIT
    results = run.lanes()
    if not run.single:
        for lane in results:
            print(f"{lane.name}: {exit_text(lane.code)} in {lane.seconds:.1f}s")
    return cli_code(results)


# The words that never load the tools: `gci serve` on a host must start even
# when a tool module the last self-update brought is broken.
TOOLLESS = {"serve"}


def json_arg(text: str) -> object:
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise argparse.ArgumentTypeError(f"not JSON: {e}") from None


def add_param(sub, name: str, prop: dict, positional: bool) -> None:
    """One tool param as an argparse argument. Each is SUPPRESSed when absent,
    so the tool's own default applies, as it does over MCP."""
    from .tools.registry import cli_flag, cli_metavar, is_word_list

    kind = prop.get("type")
    help = prop["description"].replace("%", "%%")
    opts = {"default": argparse.SUPPRESS, "help": help}
    meta = cli_metavar(name, prop)
    if kind == "integer":
        opts["type"] = int
    elif kind == "boolean":
        opts["action"] = argparse.BooleanOptionalAction
    elif is_word_list(prop):
        # Positional, a script's own flags go after `--`, so that a flag of
        # ours placed after them is still ours.
        opts["nargs"] = "*" if positional else "+"
    elif meta == "JSON":
        opts["type"] = json_arg
    if meta and not positional:
        opts["metavar"] = meta
    if positional:
        # Never required here: --input may carry it, and the tool's own check
        # names what is missing either way.
        opts.setdefault("nargs", "?")
        sub.add_argument(name, **opts)
    else:
        sub.add_argument(cli_flag(name), dest=name, **opts)


def add_tool_words(words) -> None:
    from .tools import TOOLS
    from .tools.registry import summary_of

    for name, entry in TOOLS.items():
        if entry.cli_skip is None:
            continue
        schema = entry.schema
        sub = words.add_parser(
            name,
            help=summary_of(schema).replace("%", "%%"),
            description=schema["description"],
            epilog=(
                f"Not here: {', '.join(entry.cli_skip)}, which only a server "
                "outliving the call can honor; a run is waited out."
                if entry.cli_skip
                else None
            ),
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        sub.set_defaults(_tool=name)
        sub.add_argument(
            "--input",
            dest="_input",
            metavar="FILE",
            help="the arguments as one JSON object, as an MCP call sends them; "
            "`-` reads stdin; flags given beside it win",
        )
        props = schema["inputSchema"]["properties"]
        for pname in entry.positional:
            add_param(sub, pname, props[pname], True)
        for pname, prop in props.items():
            if pname not in entry.positional and pname not in entry.cli_skip:
                add_param(sub, pname, prop, False)
        if name == "tools":
            sub.add_argument(
                "--json",
                dest="_json",
                action="store_true",
                help="the schemas exactly as tools/list sends them",
            )


def parser(with_tools: bool = True) -> argparse.ArgumentParser:
    head, usage = PACKAGE_DOC.split("\n\n")[:2]
    p = argparse.ArgumentParser(
        prog="gci",
        description=head,
        epilog=usage,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    words = p.add_subparsers(dest="word", metavar="WORD")
    if with_tools:
        add_tool_words(words)
    sub = words.add_parser("run", help="run a script, its output printed live")
    sub.add_argument(
        "--host",
        action="append",
        default=[],
        help=f"a remote.hosts name, address or URL; repeatable; `{LOCAL}` is here",
    )
    sub.add_argument(
        "--no-sync", action="store_true", help="run on the host's tree as it is"
    )
    sub.add_argument("name")
    sub.add_argument("args", nargs=argparse.REMAINDER)
    sub = words.add_parser("serve", help="run scripts for remote callers")
    sub.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"default remote.port, else {DEFAULT_PORT}",
    )
    sub.add_argument(
        "--bind", default="", help="the address to listen on (default all)"
    )
    words.add_parser("mcp", help="serve the tools over stdio, for an MCP client")
    return p


def read_input(source: str) -> dict:
    try:
        text = sys.stdin.read() if source == "-" else open(source).read()
        call = json.loads(text)
    except (OSError, json.JSONDecodeError) as e:
        raise ToolError(f"--input: {e}") from None
    if not isinstance(call, dict):
        raise ToolError("--input must hold one JSON object")
    return call


def tool_word(args: argparse.Namespace) -> int:
    """A tool run from the command line: the same checks and answer as an MCP
    call, printed. Exit 1 when the answer reports a failure."""
    from .tools import TOOLS

    cfg = safe_config()
    entry = TOOLS[args._tool]
    if not entry.available(cfg):
        raise ToolError(f"{args._tool} is switched off for this project")
    call = read_input(args._input) if args._input else {}
    props = entry.schema["inputSchema"]["properties"]
    call.update({k: v for k, v in vars(args).items() if k in props})
    text, failed = entry.call(call, cli=True)
    print(clip(text, output_limit(cfg)))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    p = parser(with_tools=not argv or argv[0] not in TOOLLESS)
    args = p.parse_args(argv)
    # A Bash tool's timeout sends SIGTERM; a run's process tree, in a session
    # of its own, would outlive us unless that ends the way Ctrl-C does.
    signal.signal(signal.SIGTERM, interrupt)
    try:
        # Each word imports only what it runs, so `gci serve` on a host never
        # loads the MCP side.
        if args.word == "mcp":
            from .mcp import serve_mcp

            return serve_mcp()
        if args.word == "serve":
            from .server import serve

            port = (
                args.port
                if args.port is not None
                else remote_cfg(safe_config()).get("port", DEFAULT_PORT)
            )
            return serve(port, args.bind)
        if args.word == "run":
            return run_word(args)
        if getattr(args, "_json", False):
            from .tools import TOOLS

            cfg = safe_config()
            names = getattr(args, "names", None)
            print(
                json.dumps(
                    [
                        t.schema
                        for name, t in TOOLS.items()
                        if t.available(cfg) and (not names or name in names)
                    ],
                    indent=2,
                )
            )
            return 0
        if hasattr(args, "_tool"):
            return tool_word(args)
    except ToolError as e:
        print(e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        stop_runs("interrupted")
        return INTERRUPTED_EXIT
    p.print_help()
    return 2


def interrupt(signum, frame):
    raise KeyboardInterrupt


def run() -> None:
    """The console-script entry point."""
    sys.exit(main())
