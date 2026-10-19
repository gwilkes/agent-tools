"""The helpers every tool leans on: globs, path rules, output filtering and cutting."""

import unittest

from gci import core
from gci.client import upload_batches
from gci.core import Rules, ToolError, glob_match, output_limit, resolve
from gci.scripts import describe_script
from gci.text import (
    check_after,
    check_filters,
    check_tail,
    clip,
    clip_lines,
    compile_pattern,
    filter_lines,
    filter_text,
    human_size,
    log_of,
)
from gci.sync import (
    IgnoreRules,
    ignored_by,
    pack_sources,
    safe_parts,
    unpack_sources,
    wire_target,
)
from tests.helpers import ProjectCase


class GlobTest(unittest.TestCase):
    def test_a_bare_name_matches_at_any_depth(self):
        self.assertTrue(glob_match(".env", ".env"))
        self.assertTrue(glob_match("a/b/.env", ".env"))
        self.assertTrue(glob_match("src/x.py", "*.py"))

    def test_a_pattern_with_a_slash_matches_the_whole_path(self):
        self.assertTrue(glob_match("secrets/key", "secrets/**"))
        self.assertTrue(glob_match("secrets", "secrets/**"))
        self.assertFalse(glob_match("a/secrets/key", "secrets/**"))
        self.assertTrue(glob_match("a/b.rs", "**/b.rs"))
        self.assertTrue(glob_match("b.rs", "**/b.rs"))


class FilterTest(unittest.TestCase):
    LINES = ["log: tmp/x.log", "a 1", "b", "a 2", "c", "a 3"]

    def test_grep_keeps_matches_and_says_how_many(self):
        out, note = filter_lines(self.LINES, grep="^a")
        self.assertEqual(out, ["a 1", "a 2", "a 3"])
        self.assertEqual(note, "[grep `^a`: 3 of 6 line(s) match]")

    def test_tail_keeps_the_last_after_grep(self):
        out, note = filter_lines(self.LINES, grep="^a", tail=1)
        self.assertEqual(out, ["a 3"])
        self.assertIn("the last 1 of 3 shown", note)

    def test_a_pinned_line_survives_every_filter(self):
        out, _ = filter_lines(
            self.LINES, grep="^c", tail=1, pinned=lambda line: line.startswith("log: ")
        )
        self.assertEqual(out, ["log: tmp/x.log", "c"])

    def test_after_keeps_following_lines_and_marks_gaps(self):
        lines = ["error: one", "  at a", "  at b", "fine", "error: two", "  at c"]
        out, note = filter_lines(lines, grep="^error", after=1)
        self.assertEqual(out, ["error: one", "  at a", "--", "error: two", "  at c"])
        self.assertIn("with 1 line(s) after each", note)

    def test_tail_after_context_says_what_it_cut(self):
        lines = [f"l{i}" for i in range(30)]
        lines[10] = lines[20] = "ERR"
        out, note = filter_lines(lines, grep="ERR", tail=3, after=5)
        self.assertEqual(out, ["l23", "l24", "l25"])
        self.assertIn("the last 3 of 12 shown", note)

    def test_nothing_to_filter_answers_no_note(self):
        self.assertEqual(filter_lines(["x"]), (["x"], None))
        self.assertEqual(filter_text("a\nb"), "a\nb")

    def test_bad_counts_and_patterns_are_refused(self):
        for bad in (0, -1, True, "3"):
            with self.assertRaises(ToolError):
                check_tail(bad)
        with self.assertRaises(ToolError):
            check_after(-1)
        self.assertIsNone(check_after(0))
        with self.assertRaises(ToolError):
            compile_pattern("(")
        with self.assertRaisesRegex(ToolError, "bad pattern"):
            check_filters(grep="(")
        self.assertEqual(check_filters(None, 2, 0), (None, 2, None))


class CutTest(unittest.TestCase):
    def test_clip_keeps_both_ends_and_names_the_hint(self):
        text = clip("x" * 100, limit=20, hint="pass grep")
        self.assertTrue(text.startswith("x" * 10) and text.endswith("x" * 10))
        self.assertIn("80 characters cut; pass grep", text)

    def test_clip_lines_keeps_more_of_the_end_and_names_the_log(self):
        lines = [f"line {i}" for i in range(500)]
        text = clip_lines(lines, 1000, see="tmp/run.log")
        self.assertLessEqual(len(text), 1100)
        self.assertIn("line 499", text)
        self.assertIn("the full output is in `tmp/run.log`", text)
        self.assertEqual(clip_lines(["a", "b"], 100), "a\nb")

    def test_output_limit_has_a_floor(self):
        self.assertEqual(output_limit({"max_output": 10}), 1000)
        self.assertEqual(output_limit({"max_output": "bad"}), core.MAX_OUTPUT)

    def test_small_helpers(self):
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(2048), "2.0 KiB")
        self.assertEqual(log_of(["$ x", "log: a.log"]), "a.log")
        self.assertIsNone(log_of(["$ x"]))


class DescribeScriptTest(unittest.TestCase):
    def test_each_kind_and_its_defaults(self):
        self.assertEqual(
            describe_script({"command": ["cargo", "build"]}), "`cargo build`"
        )
        self.assertIn("&&", describe_script({"commands": [["a"], ["b"]]}))
        self.assertEqual(describe_script({"run": ["a", "b"]}), "runs a, b")
        self.assertIn("[args...]", describe_script({"command": ["x"], "args": True}))
        self.assertIn(
            "(default wait 5, grep 'e')",
            describe_script({"command": ["x"], "wait": 5, "grep": "e"}),
        )


class SyncPathsTest(unittest.TestCase):
    def test_only_names_inside_their_subtree_are_safe(self):
        for good in ("a", "a/b.py"):
            self.assertTrue(safe_parts(good))
        for bad in ("", "/a", "../a", "a/../b", "a\\b", "c:a", "a//b"):
            self.assertFalse(safe_parts(bad), bad)
        self.assertIsNone(wire_target("../x"))

    def test_a_self_update_archive_round_trips(self):
        sources = core.package_sources()
        self.assertEqual(unpack_sources(pack_sources(sources)), sources)
        self.assertEqual(core.sources_version(sources), core.VERSION)
        with self.assertRaises(ToolError):
            unpack_sources(pack_sources({"x.py": b""}))

    def test_upload_batches_respect_the_cap(self):
        sizes = [("a", 4), ("b", 4), ("c", 10), ("d", 1)]
        self.assertEqual(upload_batches(sizes, 8), [["a", "b"], ["c"], ["d"]])


class IgnoreRulesTest(unittest.TestCase):
    CASES = [
        # (patterns, rel, is_dir, ignored)
        (["build/"], "build", True, True),
        (["build/"], "build", False, None),
        (["/top"], "top", False, True),
        (["/top"], "a/top", False, None),
        (["top"], "a/top", False, True),
        (["a/*.py"], "a/b.py", False, True),
        (["a/*.py"], "a/b/c.py", False, None),
        (["**/gen"], "x/y/gen", True, True),
        (["**/gen"], "gen", True, True),
        (["*.log", "!keep.log"], "keep.log", False, False),
        (["*.log", "!keep.log"], "other.log", False, True),
        (["# a comment", ""], "x", False, None),
    ]

    def test_each_rule_shape(self):
        for patterns, rel, is_dir, ignored in self.CASES:
            self.assertEqual(
                IgnoreRules(patterns).match(rel, is_dir),
                ignored,
                (patterns, rel, is_dir),
            )

    def test_a_deeper_gitignore_overrides_a_shallower_one(self):
        layers = [("", IgnoreRules(["*.log"])), ("sub/", IgnoreRules(["!x.log"]))]
        self.assertFalse(ignored_by(layers, "sub/x.log", False))
        self.assertTrue(ignored_by(layers, "sub/y.log", False))
        self.assertTrue(ignored_by(layers, "x.log", False))


class RulesTest(ProjectCase):
    def test_denied_protected_and_outside_paths_are_refused(self):
        rules = Rules(self.settings())
        self.assertTrue(rules.denied(".env"))
        self.assertTrue(rules.denied("secrets/key"))
        self.assertTrue(rules.protected(".gci/settings.json"))
        self.assertTrue(rules.protected("sub/.git/config"))
        with self.assertRaisesRegex(ToolError, "outside the project root"):
            resolve("../x", rules)
        with self.assertRaisesRegex(ToolError, "denied"):
            resolve(".env", rules)
        with self.assertRaisesRegex(ToolError, "protected"):
            resolve(".mcp.json", rules, write=True)
        self.assertEqual(resolve("src/a.py", rules), self.root / "src" / "a.py")


if __name__ == "__main__":
    unittest.main()
