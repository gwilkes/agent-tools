"""A scratch project for each test, and the tools called the way a client calls them."""

import json
import re
import tempfile
import unittest
from pathlib import Path

from gci import core
from gci.core import CONFIG, PYTHON_VAR
from gci.mcp import handle
from gci import runs

# What the style test plants, built here rather than written out, so this
# tree itself passes the house-style check the test exercises.
EM_DASH = chr(0x2014)
GB_WORD = "colo" + "ur"


def py(code):
    """A script command running `code` in the interpreter running this package."""
    return [PYTHON_VAR, "-c", code]


SCRIPTS = {
    "hello": {
        "description": "say hi",
        "args": True,
        "command": py("import sys; print('hi', *sys.argv[1:])"),
    },
    "fail": {"command": py("raise SystemExit(3)")},
    "slow": {"command": py("import time; time.sleep(5)"), "timeout": 1},
    "chatty": {
        "command": py("print('keep 1'); print('drop'); print('keep 2')"),
        "grep": "^keep",
    },
    "errs": {
        "command": py(
            "print('error: one\\n  at a\\n  at b\\nfine\\nerror: two\\n  at c')"
        ),
        "grep": "^error",
        "after": 1,
    },
    "doze": {
        "command": py("import time; print('dozing', flush=True); time.sleep(60)"),
        "wait": 1,
    },
    "nap": {"command": py("import time; print('napping', flush=True); time.sleep(60)")},
    "two": {
        "args": True,
        "commands": [
            py("print('step one')"),
            py("import sys; print('step two', *sys.argv[1:])"),
        ],
    },
    "splice": {
        "args": True,
        "command": [*py("import sys; print(sys.argv[1:])"), "{args}", "tail"],
    },
    "vars": {
        "env": {"GCI_T": "{greet}-env"},
        "command": [
            *py("import os, sys; print(sys.argv[1], os.environ['GCI_T'])"),
            "{greet}",
            "{unknown}",
        ],
    },
    "elsewhere": {"platforms": ["plan9"], "command": [PYTHON_VAR, "-V"]},
    "gate": {"run": ["hello", "fail", "two"]},
    "gate-all": {"run": ["fail", "elsewhere", "hello"], "keep_going": True},
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
}

SETTINGS = {
    "deny": [".env", "secrets/**"],
    "scripts": SCRIPTS,
    "vars": {"greet": {"plan9": "wrong", "default": "hello{exe}"}},
}


def rpc(method, params=None, msg_id=1, notify=None):
    """One JSON-RPC request through the MCP handler; its result."""
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return handle(msg, notify)["result"]


def job_id(text):
    return re.search(r"\(job (\d+)\)", text).group(1)


class ProjectCase(unittest.TestCase):
    """A fresh scratch root with the test settings, a denied .env and secrets/,
    a skipped node_modules/ and a binary file. Runs still going at the end of a
    test are stopped, so none outlives it."""

    def setUp(self):
        self.saved_root = core.ROOT
        # A scratch git repo's objects are read-only, which Windows will not delete.
        self.scratch = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = core.ROOT = Path(self.scratch.name).resolve()
        self.write_settings(SETTINGS)
        self.put(".env", "TOKEN=x\n")
        self.put("secrets/key", "TOKEN=y\n")
        self.put("node_modules/dep.js", "TOKEN\n")
        (self.root / "blob.bin").write_bytes(b"TOKEN\0\1\2")

    def tearDown(self):
        runs.stop_runs()
        core.ROOT = self.saved_root
        self.scratch.cleanup()

    def write_settings(self, settings):
        self.put(CONFIG, json.dumps(settings))

    def settings(self):
        return json.loads((self.root / CONFIG).read_text())

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def text_of(self, rel):
        return (self.root / rel).read_text()

    # Positional-only, since `name` is also an argument run_script takes.
    def call(self, tool_name, /, **args):
        reply = rpc("tools/call", {"name": tool_name, "arguments": args})
        return reply["content"][0]["text"], reply["isError"]

    def ok(self, tool_name, expect=None, /, **args):
        text, err = self.call(tool_name, **args)
        self.assertFalse(err, f"{tool_name}{args} failed:\n{text}")
        if expect is not None:
            self.assertIn(expect, text)
        return text

    def refused(self, tool_name, expect, /, **args):
        text, err = self.call(tool_name, **args)
        self.assertTrue(err, f"{tool_name}{args} should have refused:\n{text}")
        self.assertIn(expect, text)
        return text
