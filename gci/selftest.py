"""`gci self-test`: every tool through the MCP handler in a scratch root, a real
server over loopback, and a real MCP process restarted mid-session."""

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import core
from .client import Remote, Transport
from .core import CONFIG, DEFAULT_KEY_ENV, LOCAL, PYTHON_TOKEN, VERSION
from .mcp import handle
from .sync import pack_sources, unpack_sources

# What the style test plants, built here rather than written out, so this file
# itself passes the house-style check it tests.
EM_DASH = chr(0x2014)
GB_WORD = "colo" + "ur"


def self_test():
    saved = core.ROOT
    checks = 0

    # Positional-only, since `name` is also an argument run_script takes.
    def call(tool_name, /, **args):
        reply = handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": args},
            }
        )["result"]
        return reply["content"][0]["text"], reply["isError"]

    def ok(tool_name, expect=None, /, **args):
        nonlocal checks
        text, err = call(tool_name, **args)
        assert not err, f"{tool_name}{args} failed: {text}"
        assert (
            expect is None or expect in text
        ), f"{tool_name}{args}: {expect!r} not in:\n{text}"
        checks += 1
        return text

    def refused(tool_name, expect, /, **args):
        nonlocal checks
        text, err = call(tool_name, **args)
        assert (
            err and expect in text
        ), f"{tool_name}{args} should refuse with {expect!r}:\n{text}"
        checks += 1

    with tempfile.TemporaryDirectory() as scratch:
        core.ROOT = root = Path(scratch).resolve()
        try:
            (root / CONFIG).parent.mkdir(parents=True)
            (root / CONFIG).write_text(
                json.dumps(
                    {
                        "deny": [".env", "secrets/**"],
                        "scripts": {
                            "hello": {
                                "description": "say hi",
                                "args": True,
                                "command": [
                                    PYTHON_TOKEN,
                                    "-c",
                                    "import sys; print('hi', *sys.argv[1:])",
                                ],
                            },
                            "fail": {
                                "command": [PYTHON_TOKEN, "-c", "raise SystemExit(3)"]
                            },
                            "slow": {
                                "command": [
                                    PYTHON_TOKEN,
                                    "-c",
                                    "import time; time.sleep(5)",
                                ],
                                "timeout": 1,
                            },
                            "nap": {
                                "command": [
                                    PYTHON_TOKEN,
                                    "-c",
                                    "import time; print('napping', flush=True); time.sleep(60)",
                                ]
                            },
                            "two": {
                                "args": True,
                                "steps": [
                                    [PYTHON_TOKEN, "-c", "print('step one')"],
                                    [
                                        PYTHON_TOKEN,
                                        "-c",
                                        "import sys; print('step two', *sys.argv[1:])",
                                    ],
                                ],
                            },
                            "splice": {
                                "args": True,
                                "command": [
                                    PYTHON_TOKEN,
                                    "-c",
                                    "import sys; print(sys.argv[1:])",
                                    "{args}",
                                    "tail",
                                ],
                            },
                            "vars": {
                                "env": {"GCI_T": "{greet}-env"},
                                "command": [
                                    PYTHON_TOKEN,
                                    "-c",
                                    "import os, sys; print(sys.argv[1], os.environ['GCI_T'])",
                                    "{greet}",
                                    "{unknown}",
                                ],
                            },
                            "elsewhere": {
                                "platforms": ["plan9"],
                                "command": [PYTHON_TOKEN, "-V"],
                            },
                            "gate": {"run": ["hello", "fail", "two"]},
                            "gate-all": {
                                "run": ["fail", "elsewhere", "hello"],
                                "keep_going": True,
                            },
                            "style": {
                                "files": ["*.txt"],
                                "exclude": ["notes/ok/**"],
                                "forbid": [
                                    {"pattern": EM_DASH, "message": "em dash"},
                                    {
                                        "pattern": f"(?<![A-Za-z]){GB_WORD}(?![A-Za-z])",
                                        "message": "en-GB spelling",
                                    },
                                ],
                            },
                        },
                        "vars": {"greet": {"plan9": "wrong", "default": "hello{exe}"}},
                    }
                )
            )
            (root / ".env").write_text("TOKEN=x\n")
            (root / "secrets").mkdir()
            (root / "secrets" / "key").write_text("TOKEN=y\n")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "dep.js").write_text("TOKEN\n")
            (root / "blob.bin").write_bytes(b"TOKEN\0\1\2")

            init = handle(
                {
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-03-26"},
                }
            )["result"]
            assert (
                init["protocolVersion"] == "2025-03-26"
                and "hello" in init["instructions"]
            )
            names = {
                t["name"]
                for t in handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})[
                    "result"
                ]["tools"]
            }
            assert names == {
                "read",
                "edit",
                "sub",
                "clean_tmp",
                "json",
                "list_scripts",
                "run_script",
                "job",
                "sync",
                "hosts",
                "kill",
                "self_test",
                "restart",
                "list_tools",
            }, names
            # The settings file is the user's; an agent works from list_scripts.
            schemas = json.dumps(
                handle({"jsonrpc": "2.0", "id": 0, "method": "tools/list"})["result"]
            )
            assert (
                CONFIG not in schemas and CONFIG not in init["instructions"]
            ), "a tool description or the instructions name the settings file"
            assert (
                handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
                is None
            )
            assert (
                core.sources_version(
                    unpack_sources(pack_sources(core.package_sources()))
                )
                == VERSION
            ), "a self-update archive does not round-trip to this version"
            checks += 4

            (root / "src").mkdir()
            (root / "src" / "a.py").write_text("x = 1\ny = 2\nx = 1\n")
            ok("read", "     2\ty = 2", path="src/a.py")
            ok("read", "lines 2-2 of 3; offset=3", path="src/a.py", offset=2, limit=1)
            refused("edit", "occurs 2 times", path="src/a.py", old="x = 1", new="x = 9")
            ok("edit", "+y = 3", path="src/a.py", old="y = 2", new="y = 3")
            refused(
                "edit",
                "edit 2: old not found",
                path="src/a.py",
                edits=[{"old": "y = 3", "new": "y = 4"}, {"old": "nope", "new": "z"}],
            )
            ok("read", "y = 3", path="src/a.py")
            ok(
                "edit",
                "+x = 0",
                path="src/a.py",
                old="x = 1",
                new="x = 0",
                replace_all=True,
            )
            ok(
                "read",
                "[src/a.py: lines 3-3 of 3]\n     3\tx = 0",
                path="src/a.py",
                offset=-1,
            )
            ok("read", "[src/a.py: lines 2-3 of 3]", path="src/a.py", tail=2)
            refused("read", "tail must be a positive integer", path="src/a.py", tail=0)

            ok(
                "sub",
                "would replace 2",
                pattern="x",
                replacement="w",
                glob="*.py",
                dry_run=True,
            )
            ok("read", "x = 0", path="src/a.py")
            ok("sub", "replaced 2", pattern=r"(\w) = 0", replacement=r"\1 = 5")
            ok("read", "x = 5", path="src/a.py")

            (root / "w").mkdir()
            (root / "w" / "a.txt").write_text("ci gci ci_x .ci/ CI\n")
            ok("sub", "+gci gci ci_x .gci/ CI", words={"ci": "gci"}, path="w")
            (root / "w" / "b.txt").write_text("alpha beta\n")
            ok(
                "sub",
                "+ALPHA b",
                path="w/b.txt",
                rules=[
                    {"pattern": "alpha", "replacement": "ALPHA"},
                    {
                        "pattern": "ALPHA beta",
                        "replacement": "ALPHA b",
                        "literal": True,
                    },
                ],
            )
            refused(
                "sub", "one of them", pattern="x", replacement="y", words={"a": "b"}
            )

            (root / "e").mkdir()
            one, two = root / "e" / "one.txt", root / "e" / "two.txt"
            one.write_text("1\n2\n3\n4\n")
            two.write_text("head\n# BEGIN\nold\n# END\ntail")
            ok(
                "edit",
                "edited 2 of 2 file(s)",
                files=[
                    {
                        "path": "e/one.txt",
                        "edits": [
                            {
                                "start": 2,
                                "end": 3,
                                "expect": "2\n3",
                                "new": "two\nthree",
                            },
                            {"old": "4", "new": "four"},
                        ],
                    },
                    {
                        "path": "e/two.txt",
                        "edits": [
                            {"after": "# BEGIN", "before": "# END", "new": "generated"}
                        ],
                    },
                ],
            )
            assert one.read_text() == "1\ntwo\nthree\nfour\n", one.read_text()
            assert (
                two.read_text() == "head\n# BEGIN\ngenerated\n# END\ntail"
            ), two.read_text()
            refused(
                "edit",
                "not what `expect` says",
                path="e/one.txt",
                edits=[{"start": 1, "expect": "nope", "new": "x"}],
            )
            refused(
                "edit",
                "needs `expect`",
                path="e/one.txt",
                edits=[{"start": 1, "new": "x"}],
            )
            refused(
                "edit",
                "overlap",
                path="e/one.txt",
                edits=[
                    {"start": 1, "end": 2, "expect": "1\ntwo", "new": "a"},
                    {"start": 2, "expect": "two", "new": "b"},
                ],
            )
            refused(
                "edit",
                "listed twice",
                files=[
                    {"path": "e/one.txt", "edits": [{"old": "1", "new": "x"}]},
                    {"path": "e/one.txt", "edits": [{"old": "two", "new": "y"}]},
                ],
            )
            # All or nothing: the second file's failure leaves the first as it was.
            refused(
                "edit",
                "e/two.txt: edit 1: old not found",
                files=[
                    {"path": "e/one.txt", "edits": [{"old": "four", "new": "4"}]},
                    {"path": "e/two.txt", "edits": [{"old": "missing", "new": "x"}]},
                ],
            )
            assert (
                one.read_text() == "1\ntwo\nthree\nfour\n"
            ), "a failed multi-file edit wrote"

            refused("read", "denied", path=".env")
            refused("read", "denied", path="secrets/key")
            refused("read", "outside the project root", path="../x")
            refused("edit", "protected", path=CONFIG, old="{", new="[")
            refused("sub", "protected", pattern="scripts", replacement="x", path=CONFIG)
            ok("read", "binary", path="blob.bin")

            ok("list_scripts", "hello: say hi")
            refused("run_script", "required", args=["x"])
            ok("run_script", "hi there", name="hello", args=["there"])
            refused("run_script", "exit 3", name="fail")
            refused("run_script", "takes no arguments", name="fail", args=["x"])
            refused("run_script", "timeout", name="slow")
            refused("run_script", "no script", name="rm")
            refused("run_script", "bad pattern", name="hello", grep="(")
            ok("list_scripts", "hello: say hi", grep="^  hello")
            refused("python", "unknown tool", code="print(1)")

            text = ok("run_script", "step two x", name="two", args=["x"])
            assert text.index("step one") < text.index("step two"), text
            ok("run_script", "['a', 'tail']", name="splice", args=["a"])
            exe = ".exe" if os.name == "nt" else ""
            text = ok("run_script", f"hello{exe} hello{exe}-env", name="vars")
            assert (
                "{unknown}" in text
            ), f"an unknown var was not left as it was:\n{text}"
            refused("run_script", "runs only on plan9", name="elsewhere")
            text = call("run_script", name="gate")[0]
            assert "  OK    hello" in text and "  FAIL  fail" in text, text
            assert (
                "FAILED at fail" in text and "step one" not in text
            ), f"ran past a failure:\n{text}"
            text = call("run_script", name="gate-all")[0]
            assert "SKIP  elsewhere" in text and "OK    hello" in text, text
            assert "some FAILED" in text, text
            ok("list_scripts", "gate: runs hello, fail, two")

            # A detached run answers at once, naming its job and log; job follows it.
            def detached(expect, **args):
                text = ok("run_script", expect, detach=True, **args)
                return re.search(r"detached as job (\d+)", text).group(1), text

            jid, text = detached("detached as job", name="two", args=["x"])
            assert re.search(r"^log: \S+$", text, re.MULTILINE), text
            ok("job", "two x: exit 0 in", id=jid, wait=30)
            ok("job", f"{jid}  two x  exit 0 in")
            jid, _ = detached("fail: detached", name="fail")
            refused("job", "fail: exit 3", id=jid, wait=30)
            refused("run_script", "no script", name="rm", detach=True)
            refused("job", "no detached run 999", id="999")
            jid, _ = detached("nap: detached", name="nap")
            text = ok("job", "nap: running for", id=jid)
            if os.name != "nt":
                refused("restart", f"detached run(s) still going: {jid}")
            began = time.monotonic()
            refused("job", "killed by job(stop=true)", id=jid, stop=True)
            assert time.monotonic() - began < 10, "a stop did not end the run promptly"
            ok("job", f"{jid}  nap  exit 137")

            # Live output: progress notifications for a client that asks, and a log file.
            def watched(script):
                seen = []
                reply = handle(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {
                            "name": "run_script",
                            "arguments": {"name": script},
                            "_meta": {"progressToken": "tok"},
                        },
                    },
                    seen.append,
                )["result"]
                assert all(
                    m["method"] == "notifications/progress"
                    and m["params"]["progressToken"] == "tok"
                    for m in seen
                ), seen
                counts = [m["params"]["progress"] for m in seen]
                assert counts == sorted(set(counts)), f"progress must rise: {counts}"
                text = reply["content"][0]["text"]
                found = re.search(r"^log: (\S+)$", text, re.MULTILINE)
                assert found, f"no log line in the result:\n{text}"
                return (
                    text,
                    [m["params"]["message"] for m in seen],
                    (root / found.group(1)).read_text(),
                )

            text, live, log = watched("two")
            assert live[0].startswith("log: ") and "step two" in live, live
            assert "step one\n" in log and "step two\n" in log, log
            text, live, log = watched("gate")
            assert (
                "--- hello" in live
                and "hi" in live
                and "  FAIL  fail" in "\n".join(live)
            ), live
            assert "--- hello" in log and "\nhi\n" in log, log
            assert (
                "--- hello" not in text
            ), f"a member's output reached the result:\n{text}"
            checks += 2
            (root / "notes" / "ok").mkdir(parents=True)
            (root / "notes" / "s.txt").write_text(
                f"fine\nit is {EM_DASH} or {GB_WORD}\n"
            )
            (root / "notes" / "ok" / "skip.txt").write_text(f"{GB_WORD}\n")
            text = call("run_script", name="style")[0]
            assert (
                "notes/s.txt:2: em dash" in text
                and f"notes/s.txt:2: en-GB spelling: `{GB_WORD}`" in text
            ), text
            assert "skip.txt" not in text and "2 issue(s)" in text, text
            (root / "notes" / "s.txt").write_text("fine\n")
            ok("run_script", "0 issue(s)", name="style")
            checks += 4

            text = ok(
                "list_tools",
                "run_script(name, args?, hosts?, sync=true, proxy?, detach=false, grep?, tail?)",
            )
            listed = ok(
                "list_tools", "read(path, offset=1, limit=2000, tail?)", grep=r"^\w"
            )
            assert "    - " not in listed, listed
            assert (
                "    - name (string, required):" in text and "restart()" in text
            ), text
            assert "switched off for this project:" not in text, text
            ok("list_tools", "sub(pattern?, ", names=["sub"], brief=True)
            refused("list_tools", "no such tool: nope", names=["nope"])
            argv, env = core.self_command("list-mcp", "--brief")
            listed = subprocess.run(
                argv,
                cwd=root,
                env={**env, "GCI_ROOT": str(root)},
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert listed.returncode == 0 and "edit(path?, " in listed.stdout, listed
            argv, env = core.self_command("list-mcp", "--json", "read")
            listed = subprocess.run(
                argv,
                cwd=root,
                env={**env, "GCI_ROOT": str(root)},
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert [t["name"] for t in json.loads(listed.stdout)] == ["read"], listed
            checks += 2

            (root / "data.json").write_text(
                '{\n    "a": {"b": [1, 2]},\n    "k.d": true\n}\n'
            )
            ok("json", "true", path="data.json", query='["k.d"]')
            ok("json", "a\nk.d", path="data.json", keys=True)
            ok("json", '+        "c": 3', path="data.json", query="a.c", value="3")
            ok("json", '"x"', path="data.json", query="a.b[2]", value='"x"')
            ok("json", "3 item(s)", path="data.json", query="a.b", keys=True)
            ok(
                "json",
                '-    "k.d": true',
                path="data.json",
                query='["k.d"]',
                delete=True,
            )
            refused("json", "no key 'zz'", path="data.json", query="zz")
            refused("json", "out of range", path="data.json", query="a.b[9]", value="1")
            refused(
                "json", "not JSON text", path="data.json", query="a.c", value="bare"
            )
            refused("json", "protected", path=CONFIG, query="scripts", value="{}")
            assert json.loads((root / "data.json").read_text()) == {
                "a": {"b": [1, 2, "x"], "c": 3}
            }

            # Long output is cut to the settings' limit, naming the log that has all of it.
            saved_cfg = (root / CONFIG).read_text()
            cfg = json.loads(saved_cfg)
            cfg["max_output"] = 2000
            cfg["scripts"]["spam"] = {
                "command": [
                    PYTHON_TOKEN,
                    "-c",
                    "for i in range(3000): print('line', i)",
                ]
            }
            (root / CONFIG).write_text(json.dumps(cfg))
            text = ok("run_script", "output truncated", name="spam")
            found = re.search(r"the full output is in `([^`]+)`", text)
            assert len(text) <= 2000 and "line 2999" in text and found, text
            assert (
                "line 1500\n" in (root / found.group(1)).read_text()
            ), "the log is not whole"
            # grep, then tail, before the cut; the log line outlives both.
            # 3001 lines: the `$ command` echo counts too.
            text = ok(
                "run_script",
                "100 of 3001 line(s) match; the last 2 of 100 shown",
                name="spam",
                grep=r"^line 12\d\d$",
                tail=2,
            )
            assert text.splitlines()[1].startswith("log: "), text
            assert text.endswith("line 1298\nline 1299"), text
            assert "truncated" not in text, text
            # The tail of a run's log, without running it again.
            ok("read", "line 2999", path=found.group(1), offset=-1)
            # The backstop cut names the filters a tool takes.
            ok("list_tools", "pass grep or tail to narrow it")
            (root / CONFIG).write_text(saved_cfg)

            # Every run so far left its log in tmp/; start from an empty one.
            ok("clean_tmp", "removed")
            (root / "tmp" / "sub").mkdir(parents=True)
            (root / "tmp" / "a.log").write_text("a\n")
            (root / "tmp" / "sub" / "b.log").write_text("b\n")
            ok("clean_tmp", "would remove 2 file(s)", dry_run=True)
            assert (root / "tmp" / "a.log").exists(), "a dry run removed files"
            ok("clean_tmp", "removed 2 file(s)")
            assert (root / "tmp").is_dir() and not any(
                (root / "tmp").iterdir()
            ), "tmp/ not emptied"
            (root / "tmp" / ".env").write_text("TOKEN=t\n")
            refused("clean_tmp", "protected or denied")
            (root / "tmp" / ".env").unlink()
            # Not `checks += f()`: that reads `checks` before f runs, losing what
            # f's own ok() and refused() calls count.
            extra = self_test_git(root, ok, refused)
            checks += extra

            with tempfile.TemporaryDirectory() as far_dir:
                extra = self_test_remote(Path(far_dir).resolve(), ok, refused, call)
            checks += extra
            if os.name != "nt":
                checks += self_test_restart()
        finally:
            core.ROOT = saved
    print(f"self-test: {checks} checks OK")
    return 0


def self_test_git(root, ok, refused):
    """clean_tmp's git check over a scratch repo, with a hook that must never
    run, and .git/ protected from edits."""
    if shutil.which("git") is None:
        print("self-test: git is not on PATH; skipping the git checks")
        return 0

    def git(*args):
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=self-test",
                "-c",
                "user.email=self-test@example.com",
                "-c",
                "commit.gpgsign=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                *args,
            ],
            cwd=root,
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "first commit")
    # A hook git would run on an index refresh, armed after the commit made with hooks off.
    fired = root / "hook-fired"
    hooks = root / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    for name in ("post-index-change", "fsmonitor-watchman"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch '{fired}'\n")
        hook.chmod(0o755)
    refused(
        "edit",
        "protected",
        path=".git/config",
        old="[core]",
        new="[core]\n\tpager = sh",
    )
    # A tmp/ that git tracks is not scratch, and clean_tmp must not empty it.
    (root / "tmp" / "kept.txt").write_text("tracked\n")
    git("add", "tmp/kept.txt")
    git("commit", "-qm", "track a tmp file")
    refused("clean_tmp", "git tracks files in tmp/, so it is not scratch: tmp/kept.txt")
    assert (root / "tmp" / "kept.txt").exists()
    assert not fired.exists(), "a git hook ran during clean_tmp's check"

    # The scratch repo's history must not travel with the later sync tests.
    # Its objects are read-only, which Windows will not delete as they are.
    def force(func, path, _):
        os.chmod(path, 0o700)
        func(path)

    shutil.rmtree(root / ".git", onerror=force)
    return 2


def self_test_restart():
    """A real `gci mcp` restarted mid-session: the same process, on the same
    pipes, must answer before, announce its tools after, and keep answering."""
    argv, env = core.self_command("mcp")
    proc = subprocess.Popen(
        argv,
        cwd=core.ROOT,
        env={**env, "GCI_ROOT": str(core.ROOT)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    lines = queue.Queue()
    threading.Thread(
        target=lambda: [lines.put(json.loads(raw)) for raw in proc.stdout], daemon=True
    ).start()

    def send(*msgs):
        proc.stdin.write(b"".join(json.dumps(m).encode() + b"\n" for m in msgs))
        proc.stdin.flush()

    def expect(want, seen):
        """Read until `want(msg)` is true for some message; the messages seen go in `seen`."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                msg = lines.get(timeout=max(deadline - time.monotonic(), 0.1))
            except queue.Empty:
                break
            seen.append(msg)
            if want(msg):
                return msg
        raise AssertionError(
            f"no expected message from the restarted server; saw {seen}"
        )

    try:
        pid = proc.pid
        seen = []
        send(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "restart", "arguments": {}},
            },
        )
        reply = expect(lambda m: m.get("id") == 2, seen)
        assert not reply["result"]["isError"], reply
        # Sent while the old process may still hold the pipe: it must reach the new one.
        send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        expect(lambda m: m.get("method") == "notifications/tools/list_changed", seen)
        expect(lambda m: m.get("id") == 3, seen)
        send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "read", "arguments": {"path": CONFIG}},
            }
        )
        reply = expect(lambda m: m.get("id") == 4, seen)
        assert "scripts" in reply["result"]["content"][0]["text"], reply
        assert (
            proc.pid == pid and proc.poll() is None
        ), "the restart replaced the process"
        return 3
    finally:
        proc.kill()
        proc.wait()


def self_test_remote(far, ok, refused, call):
    """A real `gci serve` over loopback, serving `far`, driven through the tools.
    Returns the checks it made beyond those `ok` and `refused` count."""
    root = core.ROOT
    saved_key = os.environ.get(DEFAULT_KEY_ENV)
    os.environ[DEFAULT_KEY_ENV] = os.urandom(8).hex()
    argv, env = core.self_command("serve", "--port", "0", "--bind", "127.0.0.1")
    server = subprocess.Popen(
        argv,
        cwd=far,
        env={**env, "GCI_ROOT": str(far)},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        first = server.stdout.readline()
        found = re.search(r"port (\d+)", first)
        assert (
            found
        ), f"server did not start: {first}{server.stdout.read() if server.poll() else ''}"
        port = int(found.group(1))
        # Its log must go somewhere, or a full pipe stalls it mid-request.
        threading.Thread(target=server.stdout.read, daemon=True).start()

        cfg = json.loads((root / CONFIG).read_text())
        cfg["scripts"]["cat"] = {
            "args": True,
            "command": [
                PYTHON_TOKEN,
                "-c",
                "import pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text())",
            ],
        }
        cfg["remote"] = {
            "port": port,
            "hosts": {"box": "127.0.0.1", "dead": "http://127.0.0.1:9"},
        }
        (root / CONFIG).write_text(json.dumps(cfg))
        (root / "src" / "far.txt").write_text("remote hello\n")
        # A dotfile rule like this tree's own: .gci/ must travel regardless.
        (root / ".gitignore").write_text(".*\n")

        ok("hosts", "box: up at")
        ok("sync", "would be sent", hosts=["box"], dry_run=True)
        assert not (far / "src").exists(), "a dry run wrote files"
        ok(
            "run_script",
            "remote hello",
            name="cat",
            args=["src/far.txt"],
            hosts=["box"],
        )
        assert (far / "src" / "a.py").read_bytes() == (
            root / "src" / "a.py"
        ).read_bytes()
        assert (
            not (far / ".env").exists() and not (far / "secrets").exists()
        ), "denied files synced"
        assert (far / CONFIG).is_file(), "the settings did not travel past a `.*` rule"
        (root / "src" / "far.txt").unlink()
        ok("sync", "deleted 1", hosts=["box"])
        assert not (far / "src" / "far.txt").exists(), "a deletion did not travel"
        refused("run_script", "=== box: exit 3", name="fail", hosts=["box", LOCAL])
        refused("run_script", "=== local: exit 3", name="fail", hosts=["box", LOCAL])
        refused("run_script", "did not run", name="hello", hosts=["dead"], proxy=False)
        # A remote gauntlet's member output comes back for the log, not the result.
        text, _ = call("run_script", name="gate", hosts=["box"])
        assert "=== box: exit 3" in text and "--- hello" not in text, text
        logs = sorted((root / "tmp" / "gci" / "logs").glob("box-gate-*.log"))
        assert (
            logs and "--- hello" in logs[-1].read_text()
        ), "a remote gauntlet's output was lost"
        assert any(
            (far / "tmp" / "gci" / "logs").glob("gate-*.log")
        ), "the host kept no log"
        ok("kill", "nothing running", hosts=["box"])
        text = ok(
            "run_script",
            "box log: ",
            name="hello",
            args=["far"],
            hosts=["box"],
            detach=True,
        )
        jid = re.search(r"detached as job (\d+)", text).group(1)
        text = ok("job", "=== box: exit 0 in", id=jid, wait=60)
        assert "hi far" in text and "ended in" in text, text
        try:
            Remote("box", f"http://127.0.0.1:{port}", "wrong").get_json("/info")
            raise AssertionError("a wrong key was accepted")
        except Transport as e:
            assert e.status == 401, e
        return 5
    finally:
        server.kill()
        server.wait()
        if saved_key is None:
            os.environ.pop(DEFAULT_KEY_ENV, None)
        else:
            os.environ[DEFAULT_KEY_ENV] = saved_key


if __name__ == "__main__":
    sys.exit(self_test())
