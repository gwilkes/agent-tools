"""The `gci` command line, called in-process."""

import contextlib
import io
import json
import unittest
from unittest import mock

from gci import runs
from gci.cli import TRANSPORT_EXIT, cli_code, main, parser
from gci.lanes import Lane
from tests.helpers import ProjectCase, py


def run_cli(*argv):
    """(exit code, stdout, stderr) of `gci ARGV`."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class CliTest(ProjectCase):
    def test_scripts_lays_out_each_script_and_its_defaults(self):
        settings = self.settings()
        settings["defaults"] = {"tail": 40}
        self.write_settings(settings)
        code, out, _ = run_cli("scripts")
        self.assertEqual(code, 0)
        self.assertIn("hello [ARGS...]\n  say hi\n    $ ", out)
        self.assertRegex(out, r"chatty\n(    .*\n)*    grep +'\^keep'\n")
        self.assertRegex(out, r"    tail +40  \(from defaults\)")
        self.assertRegex(out, r"    timeout +600  \(built in\)")
        self.assertRegex(out, r"    timeout +1\n")
        self.assertIn("runs     hello, fail, two", out)
        self.assertTrue(out.rstrip().endswith("remote.hosts name them"))
        # The MCP answer keeps its own layout.
        self.assertIn("  hello: say hi -- ", self.ok("scripts"))

    def test_grep_keeps_whole_blocks_of_a_listing(self):
        code, out, _ = run_cli("scripts", "--grep", "say hi")
        self.assertEqual(code, 0)
        self.assertIn("1 of", out)
        self.assertIn("hello [ARGS...]\n  say hi\n    $ ", out)
        code, out, _ = run_cli("tools", "--grep", "ranges JSON", "--brief")
        self.assertIn("gci read [PATH]", out)
        self.assertNotIn("gci edit", out)

    def test_tools_lays_out_each_word_and_its_flags(self):
        code, out, _ = run_cli("tools", "read", "run_script", "job")
        self.assertEqual(code, 0)
        self.assertIn("gci read [PATH] [--offset N]", out)
        self.assertRegex(out, r"    --limit N +Most lines to show .*\(default 2000\)")
        self.assertIn(
            "gci run_script NAME [ARGS...] [--hosts HOSTS...] [--[no-]sync]", out
        )
        self.assertNotIn("--wait", out)
        self.assertIn("over MCP only: job", out)
        code, out, _ = run_cli("tools", "edit", "--brief")
        self.assertNotIn("--old OLD  ", out)
        self.assertNotIn("(default false)", run_cli("tools", "edit")[1])
        # The MCP answer keeps its signatures.
        self.assertIn("read(path?, offset=1", self.ok("tools", names=["read"]))

    def test_run_prints_live_and_answers_the_scripts_code(self):
        code, out, _ = run_cli("run", "hello", "there")
        self.assertEqual(code, 0)
        self.assertIn("hi there", out)
        self.assertEqual(run_cli("run", "fail")[0], 1)

    def test_a_refusal_goes_to_stderr(self):
        code, _, err = run_cli("run", "rm")
        self.assertEqual(code, 2)
        self.assertIn("no script 'rm'", err)
        code, _, err = run_cli("run_script", "rm")
        self.assertEqual(code, 2)
        self.assertIn("no script 'rm'", err)

    def test_hosts_takes_no_host_list(self):
        self.assertEqual(run_cli("hosts", "--hosts", "x")[0], 2)
        code, out, _ = run_cli("hosts")
        self.assertEqual((code, out.strip()), (0, "(none)"))

    def test_no_word_prints_help(self):
        code, out, _ = run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage: gci", out)


class ToolWordsTest(ProjectCase):
    def test_read_takes_its_params_as_flags(self):
        self.put("a.py", "x = 1\ndef f():\n    return 2\n")
        code, out, _ = run_cli("read", "a.py", "--at", "def f", "--limit", "5")
        self.assertEqual(code, 0)
        self.assertIn("return 2", out)
        self.assertNotIn("x = 1", out)
        code, out, _ = run_cli("read", "--ranges", '[{"path": "a.py", "end": 1}]')
        self.assertIn("x = 1", out)
        self.assertEqual(run_cli("read", "--ranges", "[oops")[0], 2)

    def test_edit_reads_a_whole_call_from_stdin(self):
        self.put("a.txt", "one\ntwo\n")
        call = {"path": "a.txt", "edits": [{"old": "two", "new": "2"}]}
        stdin = io.StringIO(json.dumps(call))
        with mock.patch("sys.stdin", stdin):
            code, out, _ = run_cli("edit", "--input", "-")
        self.assertEqual(code, 0)
        self.assertIn("+2", out)
        self.assertEqual(self.text_of("a.txt"), "one\n2\n")
        code, _, err = run_cli("edit", "a.txt", "--old", "nope", "--new", "x")
        self.assertEqual(code, 2)
        self.assertIn("old not found", err)

    def test_run_script_waits_and_answers_the_scripts_code(self):
        code, out, _ = run_cli("run_script", "hello", "there", "--tail", "5")
        self.assertEqual(code, 0)
        self.assertIn("hi there", out)
        self.assertNotIn("still running", out)
        self.assertEqual(run_cli("run_script", "fail")[0], 1)
        # A flag of the script's own goes after --, and reaches it as given.
        code, out, _ = run_cli("run_script", "hello", "--", "--loud")
        self.assertIn("hi --loud", out)

    def test_a_scripts_own_wait_is_waited_out(self):
        # Nothing could follow a run this process left going.
        settings = self.settings()
        settings["scripts"]["rest"] = {
            "command": py("import time; time.sleep(0.5); print('rested')"),
            "wait": 0,
        }
        self.write_settings(settings)
        with mock.patch.object(runs, "OUTLIVES_CALLS", False):
            code, out, _ = run_cli("run_script", "rest")
        self.assertEqual(code, 0)
        self.assertIn("rested", out)

    def test_server_only_tools_and_params_are_not_words(self):
        for argv in (["job"], ["restart"], ["run_script", "--wait", "1", "hello"]):
            self.assertEqual(run_cli(*argv)[0], 2, argv)

    def test_tools_json_is_what_tools_list_sends(self):
        code, out, _ = run_cli("tools", "read", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([t["name"] for t in json.loads(out)], ["read"])


class CodesTest(unittest.TestCase):
    def test_failure_beats_unreached_beats_success(self):
        def lanes(*codes):
            return [Lane(str(i), [], c, 0.0) for i, c in enumerate(codes)]

        self.assertEqual(cli_code(lanes(0, 0)), 0)
        self.assertEqual(cli_code(lanes(0, None)), TRANSPORT_EXIT)
        self.assertEqual(cli_code(lanes(None, 2)), 1)

    def test_every_word_parses(self):
        p = parser()
        for argv in (
            ["mcp"],
            ["scripts"],
            ["tools", "--json"],
            ["serve", "--port", "1"],
            ["sync", "--hosts", "a", "--dry-run"],
            ["kill", "--hosts", "a"],
            ["edit", "a", "--old", "x", "--new", "y", "--replace-all"],
        ):
            self.assertEqual(p.parse_args(argv).word, argv[0])


if __name__ == "__main__":
    unittest.main()
