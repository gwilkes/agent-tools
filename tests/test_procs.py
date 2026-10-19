"""Running plans: processes, timeouts, stops, gauntlets and a run's report."""

import sys
import time
import unittest

from gci import core
from gci.procs import (
    KILLED_EXIT,
    LAUNCH_FAILED,
    TIMED_OUT,
    elapsed,
    local_script,
    report_lines,
    stream_command,
)
from gci.progress import ALWAYS, BURST, Progress
from gci.runlog import RunLog, tee
from gci.scripts import script_plan
from tests.helpers import SETTINGS, ProjectCase


def python(code):
    return [sys.executable, "-c", code]


class StreamTest(unittest.TestCase):
    def test_each_line_reaches_emit_and_the_code_comes_back(self):
        lines = []
        code, why = stream_command(
            python("print('a'); print('b'); raise SystemExit(4)"),
            core.ROOT,
            30,
            lines.append,
        )
        self.assertEqual((code, why, lines), (4, None, ["a", "b"]))

    def test_a_timeout_kills_and_says_so_by_sentinel(self):
        began = time.monotonic()
        _, why = stream_command(
            python("import time; time.sleep(30)"), core.ROOT, 1, print
        )
        self.assertIs(why, TIMED_OUT)
        self.assertLess(time.monotonic() - began, 10)

    def test_a_stop_reason_kills(self):
        _, why = stream_command(
            python("import time; time.sleep(30)"),
            core.ROOT,
            None,
            print,
            stop=lambda: "enough",
        )
        self.assertEqual(why, "enough")

    def test_tee_skips_missing_sinks(self):
        a, b = [], []
        tee(a.append, None, b.append)("x")
        self.assertEqual((a, b), (["x"], ["x"]))
        self.assertEqual(elapsed(125), "2:05")


class LocalScriptTest(ProjectCase):
    def test_a_step_that_cannot_start_is_a_code_not_an_exception(self):
        settings = self.settings()
        settings["scripts"]["ghost"] = {"command": ["no-such-program-anywhere"]}
        self.write_settings(settings)
        lines = []
        code = local_script(settings, script_plan(settings, "ghost", []), lines.append)
        self.assertEqual(code, LAUNCH_FAILED)
        self.assertTrue(any("failed to launch" in line for line in lines), lines)

    def test_a_gauntlet_member_that_cannot_start_is_a_fail_line(self):
        settings = self.settings()
        settings["scripts"]["ghost"] = {"command": ["no-such-program-anywhere"]}
        settings["scripts"]["g"] = {"run": ["ghost", "hello"], "keep_going": True}
        self.write_settings(settings)
        lines = []
        code = local_script(settings, script_plan(settings, "g", []), lines.append)
        self.assertEqual(code, LAUNCH_FAILED)
        text = "\n".join(lines)
        self.assertRegex(text, r"FAIL\s+ghost")
        self.assertIn("failed to launch", text)
        self.assertRegex(text, r"OK\s+hello")
        self.assertIn("some FAILED", text)

    def test_a_log_drops_lines_written_after_it_closed(self):
        log = RunLog(self.settings(), "late")
        log.write("in time")
        log.close()
        log.write("too late")
        self.assertEqual((self.root / log.rel).read_text(), "in time\n")

    def test_a_timed_out_plan_names_its_own_timeout(self):
        lines = []
        code = local_script(SETTINGS, script_plan(SETTINGS, "slow", []), lines.append)
        self.assertEqual(code, KILLED_EXIT)
        self.assertIn("killed after 1s timeout", lines)
        self.assertTrue(lines[0].startswith("log: "), lines)

    def test_a_report_keeps_its_log_line_through_filters(self):
        text = report_lines(["log: tmp/a.log", "x", "y"], 1000, grep="y")
        self.assertEqual(text.splitlines()[0], "log: tmp/a.log")
        self.assertIn("y", text)
        self.assertNotIn("\nx", text)


class ProgressTest(unittest.TestCase):
    def test_a_flood_is_thinned_but_results_always_go(self):
        sent = []
        progress = Progress("tok", sent.append)
        for i in range(BURST * 3):
            progress(f"line {i}")
        progress("  FAIL  build  0:01")
        messages = [m["params"]["message"] for m in sent]
        self.assertLess(len(messages), BURST * 3)
        self.assertEqual(messages[-1], "  FAIL  build  0:01")
        self.assertEqual(
            [m["params"]["progress"] for m in sent], list(range(1, len(sent) + 1))
        )

    def test_what_always_goes(self):
        for line in ("[box] log: tmp/x", "all 3 OK in 0:01", "FAILED at test"):
            self.assertTrue(ALWAYS.match(line), line)
        self.assertFalse(ALWAYS.match("compiling foo"))


if __name__ == "__main__":
    unittest.main()
