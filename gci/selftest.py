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
            assert {
                "edit",
                "rename",
                "json",
                "run_script",
                "self_test",
                "restart",
                "list_tools",
            } <= names and "python" not in names, names
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

            ok("write", "created", path="src/a.py", content="x = 1\ny = 2\nx = 1\n")
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

            ok("grep", "src/a.py:3:x = 0", pattern=r"x = \d")
            ok(
                "grep",
                "src/a.py-1-x = 0\nsrc/a.py:2:y = 3\nsrc/a.py-3-x = 0",
                pattern="y",
                context=1,
                path="src",
            )
            ok("grep", "src/a.py:2", pattern="x", mode="count")
            ok("grep", "src/a.py:1:x = 0", pattern=r"0\ny", multiline=True)
            text = ok("grep", "no matches", pattern="TOKEN")
            assert ".env" not in text and "secrets" not in text

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

            ok("mkdir", "out/deep/", path="out/deep")
            ok(
                "move",
                "copied src/a.py -> out/a.py",
                src="src/a.py",
                dst="out",
                copy=True,
            )
            ok("move", "moved out/a.py -> out/b.py", src="out/a.py", dst="out/b.py")
            ok("diff", "identical", a="src/a.py", b="out/b.py")
            ok("diff", "+tail", a="src/a.py", content="x = 5\ny = 3\nx = 5\ntail\n")
            text = ok("ls", "src/", depth=2)
            assert "a.py" in text and ".env" not in text and "node_modules" not in text
            ok("ls", "out/b.py", glob="*.py")
            refused("delete", "not empty", path="out")
            ok("delete", "deleted out", path="out", recursive=True)

            refused("read", "denied", path=".env")
            refused("read", "denied", path="secrets/key")
            refused("read", "outside the project root", path="../x")
            refused("write", "protected", path=CONFIG, content="{}")
            refused("move", "protected", src=CONFIG, dst="c.json")
            ok("read", "binary", path="blob.bin")

            ok("run_script", "hello: say hi")
            ok("run_script", "hi there", name="hello", args=["there"])
            refused("run_script", "exit 3", name="fail")
            refused("run_script", "takes no arguments", name="fail", args=["x"])
            refused("run_script", "timeout", name="slow")
            refused("run_script", "no script", name="rm")
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
            ok("run_script", "gate: runs hello, fail, two")

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
                "list_tools", "run_script(name?, args?, hosts?, sync=true, proxy?)"
            )
            assert (
                "    - pattern (string, required):" in text and "restart()" in text
            ), text
            assert "switched off" not in text, text
            ok("list_tools", "grep(pattern, ", names=["grep"], brief=True)
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

            (root / "r").mkdir()
            for name in ("one.jpeg", "two.jpeg", "x.txt"):
                (root / "r" / name).write_text(name)
            ok(
                "rename",
                "would rename 2",
                pattern=r"^(.*)\.jpeg$",
                replacement=r"\1.jpg",
                path="r",
                dry_run=True,
            )
            assert (root / "r" / "one.jpeg").exists(), "a dry run renamed"
            ok(
                "rename",
                "r/one.jpeg -> one.jpg",
                pattern=r"^(.*)\.jpeg$",
                replacement=r"\1.jpg",
                path="r",
            )
            assert (root / "r" / "two.jpg").read_text() == "two.jpeg"
            refused(
                "rename",
                "would both become",
                pattern=r"^.*\.jpg$",
                replacement="same.jpg",
                path="r",
            )
            refused(
                "rename", "exists", pattern=r"^x\.txt$", replacement="one.jpg", path="r"
            )
            assert sorted(p.name for p in (root / "r").iterdir()) == [
                "one.jpg",
                "two.jpg",
                "x.txt",
            ]
            checks += 2

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
            # A denied file deep in a directory must stop a recursive delete of it.
            (root / "d" / "e").mkdir(parents=True)
            (root / "d" / "e" / ".env").write_text("TOKEN=d\n")
            refused(
                "delete",
                "protected or denied files: d/e/.env",
                path="d",
                recursive=True,
            )
            assert (root / "d" / "e" / ".env").exists(), "a denied file was deleted"
            shutil.rmtree(root / "d")

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
    """The read-only git tools over a scratch repo, with a tracked denied file
    that must never show up, and a hook that must never run."""
    if shutil.which("git") is None:
        print("self-test: git is not on PATH; skipping the git tools")
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
    (root / "src" / "a.py").write_text("x = 5\ny = 3\nx = 5\nadded\n")
    (root / ".env").write_text("TOKEN=changed\n")

    ok("git_status", "## main")
    text = ok("git_status", " M src/a.py")
    assert ".env" not in text, f"a denied file in status:\n{text}"
    text = ok("git_diff", "+added")
    assert "TOKEN=" not in text, f"a denied file in a diff:\n{text}"
    ok("git_diff", "src/a.py", stat=True)
    ok("git_diff", "no differences", staged=True)
    ok("git_log", "self-test: first commit")
    # .env and secrets/key are tracked, but denied, so their contents never show.
    text = ok("git_show", "first commit")
    assert "TOKEN=" not in text and "node_modules/dep.js" in text, f"show:\n{text}"
    ok("git_blame", "(self-test", path="src/a.py", start=1, end=1)
    ok("git_branch", "* main")
    refused("git_show", "not a ref", ref="HEAD:.env")
    refused("git_log", "not a ref", ref="--output=leak.txt")
    refused("git_blame", "denied", path=".env")
    refused("write", "protected", path=".git/hooks/pre-commit", content="#!/bin/sh\n")
    refused(
        "edit",
        "protected",
        path=".git/config",
        old="[core]",
        new="[core]\n\tpager = sh",
    )
    assert not fired.exists(), "a git hook ran during a read-only tool"
    assert not (root / "leak.txt").exists(), "a ref became an option"
    # A tmp/ that git tracks is not scratch, and clean_tmp must not empty it.
    (root / "tmp" / "kept.txt").write_text("tracked\n")
    git("add", "tmp/kept.txt")
    git("commit", "-qm", "track a tmp file")
    refused("clean_tmp", "git tracks files in tmp/, so it is not scratch: tmp/kept.txt")
    assert (root / "tmp" / "kept.txt").exists()

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
