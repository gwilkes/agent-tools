"""The `gci` command line: one word per role, as the package docstring lists."""

import argparse
import json
import sys
import time

from . import __doc__ as PACKAGE_DOC
from .client import lane_word, remote_script, run_lanes, stop_active_jobs
from .core import (
    CONFIG,
    DEFAULT_PORT,
    LOCAL,
    ToolError,
    config,
    describe_hosts,
    describe_scripts,
    remote_cfg,
    safe_config,
)
from .procs import INTERRUPTED, local_script
from .scripts import WrongPlatform, script_plan

# A CLI run where some host was never reached.
TRANSPORT_EXIT = 3


def cli_code(results):
    """1 if a script failed anywhere, else 3 if a host was never reached, else 0."""
    codes = [code for _, _, code, _ in results]
    if any(code not in (0, None) for code in codes):
        return 1
    return TRANSPORT_EXIT if None in codes else 0


def cli_run(args):
    cfg = config()
    names = args.host or [LOCAL]
    try:
        script_plan(cfg, args.name, args.args)
    except WrongPlatform:
        if not args.host:
            raise
    many = len(names) > 1

    def echo(name, line):
        print(f"[{name}] {line}" if many else line, flush=True)

    # At a terminal a person is watching, so a gauntlet's members print too.
    def work(host, emit):
        if host == LOCAL:
            return local_script(cfg, args.name, args.args, emit, quiet=emit)
        return remote_script(
            cfg,
            host,
            args.name,
            args.args,
            not args.no_sync,
            args.proxy,
            emit,
            quiet=emit,
        )

    results = run_lanes(names, work, echo)
    if args.host:
        for name, _, code, seconds in results:
            print(f"{name}: {lane_word(code)} in {seconds:.1f}s")
    return cli_code(results)


def parser():
    head, usage = PACKAGE_DOC.split("\n\n")[:2]
    p = argparse.ArgumentParser(
        prog="gci",
        description=head,
        epilog=usage,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    words = p.add_subparsers(dest="word", metavar="WORD")

    def lane_flags(sub, required):
        sub.add_argument(
            "--host",
            action="append",
            default=[],
            required=required,
            help=f"a remote.hosts name, address or URL; repeatable; `{LOCAL}` is here",
        )
        sub.add_argument(
            "--proxy",
            dest="proxy",
            action="store_const",
            const=True,
            default=None,
            help="reach hosts through remote.proxy",
        )
        sub.add_argument(
            "--no-proxy",
            dest="proxy",
            action="store_const",
            const=False,
            help="never fall back to the proxy",
        )

    words.add_parser("mcp", help="serve the tools over stdio")
    words.add_parser("scripts", help=f"list the scripts and hosts {CONFIG} defines")
    words.add_parser("self-test", help="drive every tool once in scratch directories")
    sub = words.add_parser(
        "list-mcp", help="every MCP tool, its description and parameters"
    )
    sub.add_argument("names", nargs="*", help="only these tools")
    sub.add_argument("--brief", action="store_true", help="one line per tool")
    sub.add_argument(
        "--json",
        action="store_true",
        help="the schemas exactly as tools/list sends them",
    )
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
    sub = words.add_parser("run", help="run a script here or on hosts")
    lane_flags(sub, required=False)
    sub.add_argument(
        "--no-sync", action="store_true", help="run on the host's tree as it is"
    )
    sub.add_argument("name")
    sub.add_argument("args", nargs=argparse.REMAINDER)
    sub = words.add_parser("sync", help="bring hosts' trees level with this one")
    lane_flags(sub, required=True)
    sub.add_argument(
        "--dry-run", action="store_true", help="report without sending or deleting"
    )
    sub = words.add_parser("kill", help="stop what hosts are running")
    lane_flags(sub, required=True)
    sub = words.add_parser("hosts", help="which hosts answer")
    lane_flags(sub, required=False)
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        # Each word imports only what it runs, so `gci serve` on a host never
        # loads the MCP side.
        if args.word == "mcp":
            from .mcp import serve_mcp

            return serve_mcp()
        if args.word == "self-test":
            from .selftest import self_test

            return self_test()
        if args.word == "serve":
            from .server import serve

            port = (
                args.port
                if args.port is not None
                else remote_cfg(safe_config()).get("port", DEFAULT_PORT)
            )
            return serve(port, args.bind)
        if args.word == "list-mcp":
            from .mcp import TOOLS, describe_tools  # importing mcp registers every tool

            cfg = safe_config()
            if args.json:
                print(
                    json.dumps(
                        [
                            t.schema
                            for name, t in TOOLS.items()
                            if t.available(cfg)
                            and (not args.names or name in args.names)
                        ],
                        indent=2,
                    )
                )
            else:
                print(describe_tools(cfg, args.names, args.brief))
            return 0
        if args.word == "scripts":
            cfg = config()
            print(f"from {CONFIG}:")
            print(f"scripts:\n{describe_scripts(cfg)}\nhosts:\n{describe_hosts(cfg)}")
            return 0
        if args.word == "run":
            return cli_run(args)
        if args.word in ("hosts", "sync", "kill"):
            from .tools import run as tools

            if args.word == "hosts":
                print(tools.hosts(args.proxy))
                return 0
            text, failed = (
                tools.sync(args.host, args.dry_run, args.proxy)
                if args.word == "sync"
                else tools.kill(args.host, args.proxy)
            )
            print(text)
            return 1 if failed else 0
    except ToolError as e:
        print(e, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        INTERRUPTED.set()
        for line in stop_active_jobs():
            print(line, file=sys.stderr)
        # Local lanes notice within a second and take their process trees down.
        time.sleep(1.5)
        return 130
    p.print_help()
    return 2


def run():
    """The console-script entry point."""
    sys.exit(main())
