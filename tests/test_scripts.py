"""Scripts: planning them, running them through run_script, and following runs with job."""

import os
import re
import time
import unittest

from gci.core import ToolError
from gci.scripts import WrongPlatform, script_plan, substitute
from gci.tools.run import wait_seconds
from tests.helpers import EM_DASH, GB_WORD, SETTINGS, ProjectCase, job_id, py, rpc


class PlanTest(ProjectCase):
    def test_args_follow_the_last_step_or_go_where_args_says(self):
        plan = script_plan(SETTINGS, "two", ["x"])
        self.assertEqual(plan.commands[-1][-1], "x")
        plan = script_plan(SETTINGS, "splice", ["a", "b"])
        self.assertEqual(plan.commands[0][-3:], ["a", "b", "tail"])

    def test_refusals(self):
        with self.assertRaisesRegex(ToolError, "no script 'rm'"):
            script_plan(SETTINGS, "rm", [])
        with self.assertRaisesRegex(ToolError, "takes no arguments"):
            script_plan(SETTINGS, "fail", ["x"])
        with self.assertRaises(WrongPlatform):
            script_plan(SETTINGS, "elsewhere", [])
        cfg = {
            "scripts": {
                "g": {"run": ["x"], "args": True},
                "late": {"command": ["x"], "timeout": "600"},
            }
        }
        with self.assertRaisesRegex(ToolError, "'g' takes no arguments"):
            script_plan(cfg, "g", ["a"])
        with self.assertRaisesRegex(ToolError, "timeout must be a number"):
            script_plan(cfg, "late", [])

    def test_vars_and_unknown_names(self):
        self.assertEqual(substitute("{a}-{b}", {"a": "1"}), "1-{b}")
        exe = ".exe" if os.name == "nt" else ""
        plan = script_plan(SETTINGS, "vars", [])
        self.assertEqual(plan.commands[0][-2:], [f"hello{exe}", "{unknown}"])
        self.assertEqual(plan.env["GCI_T"], f"hello{exe}-env")


class WaitTest(unittest.TestCase):
    def test_a_wait_is_whole_seconds_or_none(self):
        self.assertIsNone(wait_seconds(None))
        self.assertEqual(wait_seconds(-3), 0)
        for bad in (True, 1.5, "2"):
            with self.assertRaisesRegex(ToolError, "wait must be a number"):
                wait_seconds(bad)


class RunScriptTest(ProjectCase):
    def test_scripts(self):
        self.ok("scripts", "hello: say hi")
        self.ok("scripts", "hello: say hi", grep="^  hello")
        self.ok("scripts", "gate: runs hello, fail, two")
        self.ok("scripts", "(default grep '^keep')")
        self.ok("scripts", "(default wait 1)")
        self.ok("scripts", "(default grep '^error', after 1)")

    def test_runs_and_refusals(self):
        self.refused("run_script", "missing: name", args=["x"])
        self.ok("run_script", "hi there", name="hello", args=["there"])
        self.refused("run_script", "exit 3", name="fail")
        self.refused("run_script", "takes no arguments", name="fail", args=["x"])
        self.refused("run_script", "timeout", name="slow")
        self.refused("run_script", "no script", name="rm")
        self.refused("run_script", "bad pattern", name="hello", grep="(")
        self.refused("run_script", "runs only on plan9", name="elsewhere")
        self.refused(
            "run_script", "wait must be integer, not str", name="hello", wait="soon"
        )
        self.refused("job", "wait must be integer, not bool", wait=True)
        self.refused("run_script", "after must be a number", name="hello", after=-1)

    def test_command_and_commands_exclude_each_other(self):
        settings = self.settings()
        settings["scripts"]["both"] = {"command": ["x"], "commands": [["y"]]}
        settings["scripts"]["older"] = {"steps": [["x"]]}
        settings["scripts"]["empty"] = {"commands": []}
        settings["scripts"]["nested"] = {"command": [["x"]]}
        self.write_settings(settings)
        self.refused("run_script", "it has command, commands", name="both")
        self.refused("run_script", 'is now "commands"', name="older")
        self.refused("run_script", "a non-empty list of argvs", name="empty")
        self.refused("run_script", "must be a non-empty list of strings", name="nested")

    def test_commands_splice_and_vars(self):
        text = self.ok("run_script", "step two x", name="two", args=["x"])
        self.assertLess(text.index("step one"), text.index("step two"))
        self.ok("run_script", "['a', 'tail']", name="splice", args=["a"])
        exe = ".exe" if os.name == "nt" else ""
        text = self.ok("run_script", f"hello{exe} hello{exe}-env", name="vars")
        self.assertIn("{unknown}", text)

    def test_gauntlets(self):
        text, _ = self.call("run_script", name="gate")
        self.assertIn("  OK    hello", text)
        self.assertIn("  FAIL  fail", text)
        self.assertIn("FAILED at fail", text)
        self.assertNotIn("step one", text)
        text, _ = self.call("run_script", name="gate-all")
        self.assertIn("SKIP  elsewhere", text)
        self.assertIn("OK    hello", text)
        self.assertIn("some FAILED", text)

    def test_a_scripts_own_grep_and_after_are_defaults(self):
        text = self.ok("run_script", "keep 2", name="chatty")
        self.assertIn("keep 1", text)
        self.assertNotIn("drop", text)
        self.ok("run_script", "drop", name="chatty", grep=".")
        text = self.ok(
            "run_script", "error: one\n  at a\n--\nerror: two\n  at c", name="errs"
        )
        self.assertNotIn("fine", text)
        self.assertNotIn("at b", text)
        self.ok("run_script", "  at b\nfine", name="errs", after=3)

    def test_settings_defaults_beneath_each_scripts_own(self):
        settings = self.settings()
        settings["defaults"] = {"grep": "^drop", "tail": 5}
        settings["scripts"]["chatty-all"] = {**settings["scripts"]["chatty"]}
        settings["scripts"]["chatty-all"]["grep"] = None
        self.write_settings(settings)
        self.ok("scripts", "defaults for every script: grep '^drop', tail 5")
        self.ok("scripts", "(default grep none)")
        # The script's own grep wins over the shared one; the shared tail stays.
        text = self.ok("run_script", "keep 2", name="chatty")
        self.assertNotIn("drop", text)
        # A null in the script clears the shared grep.
        self.ok("run_script", "drop", name="chatty-all")
        # A script that sets none takes the shared grep.
        self.ok("run_script", "[grep `^drop`: 0 of 2", name="hello")
        # A caller's own still wins over both.
        self.ok("run_script", "keep 1", name="chatty", grep="1")

    def test_shared_defaults_are_checked_where_they_apply(self):
        settings = self.settings()
        settings["defaults"] = {"sync": "yes"}
        self.write_settings(settings)
        self.refused("run_script", "sync must be true or false", name="hello")
        settings["defaults"] = {"hosts": ["nowhere"]}
        self.write_settings(settings)
        self.refused("run_script", "unknown host(s): nowhere", name="hello")
        # `["local"]` alone overrides a default host list, answering as here.
        text = self.ok("run_script", name="hello", hosts=["local"])
        self.assertTrue(text.startswith("hello: exit 0"), text)
        settings["defaults"] = {"tail": 0}
        self.write_settings(settings)
        self.refused("run_script", "tail must be a positive integer", name="hello")
        settings["defaults"] = []
        self.write_settings(settings)
        self.refused("run_script", '"defaults" must be an object', name="hello")

    def test_a_mistyped_key_is_refused_not_ignored(self):
        settings = self.settings()
        settings["scripts"]["typo"] = {"command": ["x"], "tial": 5}
        self.write_settings(settings)
        self.refused("run_script", "script 'typo': unknown key(s) tial", name="typo")
        del settings["scripts"]["typo"]
        settings["defualts"] = {"tail": 5}
        self.write_settings(settings)
        self.refused(
            "run_script", "the settings: unknown key(s) defualts", name="hello"
        )
        settings["defaults"] = settings.pop("defualts") | {"tial": 1}
        self.write_settings(settings)
        self.refused("run_script", "unknown key(s) tial", name="hello")

    def test_the_timeout_default_moved_into_defaults(self):
        settings = self.settings()
        settings["timeout"] = 5
        self.write_settings(settings)
        self.refused("run_script", '"timeout" is now "defaults"', name="hello")
        del settings["timeout"]
        settings["defaults"] = {"timeout": 1}
        self.write_settings(settings)
        text, failed = self.call("run_script", name="nap")
        self.assertTrue(failed)
        self.assertIn("killed after 1s timeout", text)

    def test_job_shows_a_run_with_its_scripts_filters(self):
        text = self.ok("run_script", name="chatty", wait=0)
        run = job_id(text)
        text = self.ok("job", "keep 2", id=run, wait=10)
        self.assertNotIn("drop", text)
        self.ok("job", "drop", id=run, grep=".")

    def test_forbid_rules(self):
        self.put("notes/s.txt", f"fine\nit is {EM_DASH} or {GB_WORD}\n")
        self.put("notes/ok/skip.txt", f"{GB_WORD}\n")
        text, _ = self.call("run_script", name="style")
        self.assertIn("notes/s.txt:2: em dash", text)
        self.assertIn(f"notes/s.txt:2: en-GB spelling: `{GB_WORD}`", text)
        self.assertNotIn("skip.txt", text)
        self.assertIn("2 issue(s)", text)
        self.put("notes/s.txt", "fine\n")
        self.ok("run_script", "0 issue(s)", name="style")

    def test_long_output_is_cut_around_its_log(self):
        settings = self.settings()
        settings["max_output"] = 2000
        settings["scripts"]["spam"] = {
            "command": py("for i in range(3000): print('line', i)")
        }
        self.write_settings(settings)
        text = self.ok("run_script", "output truncated", name="spam")
        found = re.search(r"the full output is in `([^`]+)`", text)
        self.assertLessEqual(len(text), 2000)
        self.assertIn("line 2999", text)
        self.assertIn("line 1500\n", self.text_of(found.group(1)))
        # grep, then tail, before the cut; the log line outlives both. 3001
        # lines: the `$ command` echo counts too.
        text = self.ok(
            "run_script",
            "100 of 3001 line(s) match; the last 2 of 100 shown",
            name="spam",
            grep=r"^line 12\d\d$",
            tail=2,
        )
        self.assertTrue(text.splitlines()[1].startswith("log: "), text)
        self.assertTrue(text.endswith("line 1298\nline 1299"), text)
        self.assertNotIn("truncated", text)
        self.ok("read", "line 2999", path=found.group(1), offset=-1)

    def watched(self, script):
        """A run with a progress token: (result, live lines, its log)."""
        seen = []
        reply = rpc(
            "tools/call",
            {
                "name": "run_script",
                "arguments": {"name": script},
                "_meta": {"progressToken": "tok"},
            },
            notify=seen.append,
        )
        for msg in seen:
            self.assertEqual(msg["method"], "notifications/progress")
            self.assertEqual(msg["params"]["progressToken"], "tok")
        counts = [m["params"]["progress"] for m in seen]
        self.assertEqual(counts, sorted(set(counts)), "progress must rise")
        text = reply["content"][0]["text"]
        found = re.search(r"^log: (\S+)$", text, re.MULTILINE)
        self.assertTrue(found, text)
        return (
            text,
            [m["params"]["message"] for m in seen],
            self.text_of(found.group(1)),
        )

    def test_live_progress_and_the_log(self):
        _, live, log = self.watched("two")
        self.assertTrue(live[0].startswith("log: "), live)
        self.assertIn("step two", live)
        self.assertIn("step one\n", log)
        text, live, log = self.watched("gate")
        self.assertIn("--- hello", live)
        self.assertIn("  FAIL  fail", "\n".join(live))
        self.assertIn("\nhi\n", log)
        self.assertNotIn("--- hello", text, "a member's output reached the result")


class JobTest(ProjectCase):
    def test_every_run_is_a_job(self):
        jid = job_id(self.ok("run_script", "two x: exit 0 in", name="two", args=["x"]))
        self.ok("job", "two x: exit 0 in", id=jid)
        self.ok("job", f"{jid}  two x  exit 0 in")
        self.refused("run_script", "no script", name="rm", wait=0)
        self.refused("job", "no run 999", id="999")

    def test_wait_answers_mid_run_and_stop_ends_it(self):
        text = self.ok("run_script", "nap: running for", name="nap", wait=1)
        self.assertIn('still running: job(id="', text)
        self.assertRegex(text, re.compile(r"^log: \S+$", re.MULTILINE))
        jid = job_id(text)
        # A slow machine may not have started the child within the second.
        deadline = time.monotonic() + 20
        while "napping" not in self.ok("job", "nap: running for", id=jid):
            self.assertLess(time.monotonic(), deadline, "the run never printed")
            time.sleep(0.2)
        if os.name != "nt":
            self.refused("restart", f"run(s) still going: {jid}")
        began = time.monotonic()
        self.refused("job", "killed by job(stop=true)", id=jid, stop=True)
        self.assertLess(
            time.monotonic() - began, 10, "a stop did not end the run promptly"
        )
        self.ok("job", f"{jid}  nap  exit 137")

    def test_wait_zero_answers_at_once(self):
        began = time.monotonic()
        jid = job_id(self.ok("run_script", "still running", name="nap", wait=0))
        self.assertLess(time.monotonic() - began, 5)
        self.refused("job", "killed by job(stop=true)", id=jid, stop=True)

    def test_a_scripts_own_wait(self):
        jid = job_id(self.ok("run_script", "still running", name="doze"))
        self.refused("job", "killed by job(stop=true)", id=jid, stop=True)


if __name__ == "__main__":
    unittest.main()
