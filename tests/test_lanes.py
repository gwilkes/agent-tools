"""Lanes and runs: fan-out, reports, host names, and runs kept for job."""

import unittest

from gci.core import LOCAL, ToolError
from gci.lanes import Lane, exit_text, lane_report, named_hosts, run_lanes
from gci.runs import RUNS, launch, running_runs
from gci.wire import describe_hosts


class LanesTest(unittest.TestCase):
    def test_every_name_gets_a_row_whatever_work_raises(self):
        def work(name, emit):
            emit(f"in {name}")
            if name == "a":
                raise ToolError("refused")
            if name == "b":
                raise KeyError("bug")
            return 0

        lanes = run_lanes(["a", "b", "c", "a"], work)
        self.assertEqual([lane.name for lane in lanes], ["a", "b", "c"])
        self.assertEqual([lane.code for lane in lanes], [None, None, 0])
        self.assertIn("refused", lanes[0].lines)
        self.assertIn("internal error: KeyError: 'bug'", lanes[1].lines)

    def test_a_report_has_a_section_per_lane(self):
        lanes = [
            Lane("box", ["ok"], 0, 1.0),
            Lane("pc", ["..."], None, 2.0, running=True),
        ]
        text, failed = lane_report(lanes)
        self.assertIn("=== box: exit 0 in 1.0s ===", text)
        self.assertIn("=== pc: running for 2.0s ===", text)
        self.assertFalse(failed)
        self.assertEqual(exit_text(None), "no exit code")

    def test_agents_name_hosts_from_the_settings(self):
        cfg = {"remote": {"hosts": {"box": "10.0.0.2"}}}
        self.assertEqual(named_hosts(cfg, ["box", LOCAL]), ["box", LOCAL])
        with self.assertRaisesRegex(ToolError, "unknown host"):
            named_hosts(cfg, ["10.0.0.3"])
        self.assertIn("box: 10.0.0.2", describe_hosts(cfg))


class RunsTest(unittest.TestCase):
    def test_a_launched_run_is_kept_and_ends(self):
        def body(run):
            run.lines[LOCAL].append("done")
            run.finish_lane(LOCAL, 0)

        run = launch("x", [LOCAL], True, [], body)
        self.assertTrue(run.done.wait(5))
        self.assertIs(RUNS[run.id], run)
        self.assertNotIn(run, running_runs())
        text, failed = run.report(1000, None, None)
        self.assertIn("x: exit 0 in", text)
        self.assertFalse(failed)

    def test_a_stopped_run_names_its_reason(self):
        def body(run):
            while not run.stop_reason():
                run.done.wait(0.01)
            run.lines[LOCAL].append(run.stop_reason())
            run.finish_lane(LOCAL, 137)

        run = launch("y", [LOCAL], True, [], body)
        self.assertEqual(run.stop("interrupted"), [])
        self.assertTrue(run.done.wait(5))
        self.assertEqual(run.lanes()[0].lines, ["interrupted"])


if __name__ == "__main__":
    unittest.main()
