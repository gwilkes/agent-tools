"""The MCP side: the handshake, the tool list, the CLI's view of it, and restart."""

import json
import os
import queue
import subprocess
import threading
import time
import unittest

from gci import core
from gci.core import CONFIG
from gci.core import ToolError
from gci.mcp import handle
from gci.tools import meta
from gci.tools.registry import check_arguments
from tests.helpers import ProjectCase, rpc

TOOLS = {
    "read",
    "edit",
    "scripts",
    "run_script",
    "job",
    "sync",
    "hosts",
    "kill",
    "restart",
    "tools",
}


class ProtocolTest(ProjectCase):
    def test_initialize_echoes_the_version_and_names_the_scripts(self):
        init = rpc("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(init["protocolVersion"], "2025-03-26")
        self.assertIn("hello", init["instructions"])

    def test_the_tool_list_is_exactly_these(self):
        names = {t["name"] for t in rpc("tools/list")["tools"]}
        self.assertEqual(names, TOOLS)

    def test_nothing_an_agent_reads_names_the_settings_file(self):
        # The settings file is the user's; an agent works from scripts.
        self.assertNotIn(CONFIG, json.dumps(rpc("tools/list")))
        self.assertNotIn(CONFIG, rpc("initialize", {})["instructions"])

    def test_notifications_and_unknowns(self):
        self.assertIsNone(
            handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        reply = handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})
        self.assertEqual(reply["error"]["code"], -32601)
        self.refused("python", "unknown tool", code="print(1)")

    def test_arguments_are_checked_against_the_schema(self):
        self.refused("read", "unknown: nope", path="x", nope=1)
        self.refused("read", "path must be string, not int", path=3)
        self.refused("edit", "edits must be array, not str", path="x", edits="a")
        self.refused("run_script", "args must hold strings", name="hello", args=[1])


class CheckArgumentsTest(unittest.TestCase):
    SCHEMA = {
        "name": "t",
        "inputSchema": {
            "properties": {
                "n": {"type": "integer"},
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["k"],
                        "properties": {"k": {"type": "string"}},
                    },
                },
            },
            "required": ["n"],
            "additionalProperties": False,
        },
    }

    def problems(self, args):
        try:
            check_arguments(self.SCHEMA, args)
        except ToolError as e:
            return str(e)
        return None

    def test_good_arguments_pass(self):
        self.assertIsNone(self.problems({"n": 1, "items": [{"k": "x"}]}))

    def test_every_problem_is_named_with_its_path(self):
        text = self.problems({"n": True, "items": [{"k": 1}, {"z": "y"}], "q": 0})
        self.assertIn("unknown: q", text)
        self.assertIn("n must be integer, not bool", text)
        self.assertIn("items[0].k must be string, not int", text)
        self.assertIn("items[1] unknown: z", text)
        self.assertIn("items[1] missing: k", text)


class CallCountTest(unittest.TestCase):
    def test_calls_are_counted_until_a_restart_begins(self):
        self.assertTrue(meta.begin_call())
        meta.end_call()
        meta.RESTART.set()
        try:
            self.assertFalse(meta.begin_call())
        finally:
            meta.RESTART.clear()


class ListToolsTest(ProjectCase):
    def test_signatures_descriptions_and_filters(self):
        text = self.ok(
            "tools",
            "run_script(name, args?, hosts?, sync?, wait?, grep?, tail?, after?)",
        )
        self.assertIn("    - name (string, required):", text)
        self.assertIn("restart()", text)
        self.assertNotIn("switched off for this project:", text)
        listed = self.ok(
            "tools",
            "read(path?, offset=1, limit=2000, tail?, at?, ranges?)",
            grep=r"^\w",
        )
        self.assertNotIn("    - ", listed)
        self.ok("tools", "edit(path?, ", names=["edit"], brief=True)
        self.refused("tools", "no such tool: nope", names=["nope"])

    def test_a_long_answer_names_the_filters_that_narrow_it(self):
        settings = self.settings()
        settings["max_output"] = 2000
        self.write_settings(settings)
        self.ok("tools", "pass grep or tail to narrow it")

    def cli(self, *words):
        argv, env = core.self_command(*words)
        return subprocess.run(
            argv,
            cwd=self.root,
            env={**env, "GCI_ROOT": str(self.root)},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_the_cli_shows_the_same_tools(self):
        listed = self.cli("tools", "--brief")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("gci edit [PATH] ", listed.stdout)
        listed = self.cli("tools", "--json", "read")
        self.assertEqual([t["name"] for t in json.loads(listed.stdout)], ["read"])


@unittest.skipIf(os.name == "nt", "exec starts a new process on Windows")
class RestartTest(ProjectCase):
    """A real `gci mcp` restarted mid-session: the same process, on the same
    pipes, must answer before, announce its tools after, and keep answering."""

    def setUp(self):
        super().setUp()
        argv, env = core.self_command("mcp")
        self.proc = subprocess.Popen(
            argv,
            cwd=self.root,
            env={**env, "GCI_ROOT": str(self.root)},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self.lines = queue.Queue()
        threading.Thread(
            target=lambda: [
                self.lines.put(json.loads(raw)) for raw in self.proc.stdout
            ],
            daemon=True,
        ).start()
        self.seen = []

    def tearDown(self):
        self.proc.kill()
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()
        super().tearDown()

    def send(self, *msgs):
        self.proc.stdin.write(b"".join(json.dumps(m).encode() + b"\n" for m in msgs))
        self.proc.stdin.flush()

    def expect(self, want):
        """Read until `want(msg)` holds for some message, within 30s."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                msg = self.lines.get(timeout=max(deadline - time.monotonic(), 0.1))
            except queue.Empty:
                break
            self.seen.append(msg)
            if want(msg):
                return msg
        self.fail(f"no expected message from the server; saw {self.seen}")

    def test_a_restart_keeps_the_process_and_its_pipes(self):
        pid = self.proc.pid
        self.send(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "restart", "arguments": {}},
            },
        )
        reply = self.expect(lambda m: m.get("id") == 2)
        self.assertFalse(reply["result"]["isError"], reply)
        # Sent while the old process may still hold the pipe: it must reach the new one.
        self.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.expect(lambda m: m.get("method") == "notifications/tools/list_changed")
        self.expect(lambda m: m.get("id") == 3)
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "read", "arguments": {"path": CONFIG}},
            }
        )
        reply = self.expect(lambda m: m.get("id") == 4)
        self.assertIn("scripts", reply["result"]["content"][0]["text"])
        self.assertEqual(self.proc.pid, pid)
        self.assertIsNone(self.proc.poll(), "the restart ended the process")


if __name__ == "__main__":
    unittest.main()
