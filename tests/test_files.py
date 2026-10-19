"""read and edit, through the MCP handler."""

import os
import unittest

from gci.core import CONFIG, ToolError
from gci.tools.files import write_all
from gci.tools.read import block_end
from tests.helpers import ProjectCase

RUST = (
    "use x;\n\n/// Doc.\n#[test]\nfn alpha() {\n"
    "    let s = \"}\";\n    beta('{');\n}\n\n"
    "fn beta(c: char) -> u8 {\n    0\n}\nfn gamma();\n"
)
PYTHON = "def f():\n    x = 1\n\n    return x\ndef g():\n    pass\n"


class ReadTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.put("src/a.py", "x = 1\ny = 2\nx = 1\n")

    def test_pages_offsets_and_tail(self):
        self.ok("read", "     2\ty = 2", path="src/a.py")
        self.ok("read", "lines 2-2 of 3; offset=3", path="src/a.py", offset=2, limit=1)
        self.ok(
            "read",
            "[src/a.py: lines 3-3 of 3]\n     3\tx = 1",
            path="src/a.py",
            offset=-1,
        )
        self.ok("read", "[src/a.py: lines 2-3 of 3]", path="src/a.py", tail=2)
        self.refused("read", "tail must be a positive integer", path="src/a.py", tail=0)
        self.refused("read", "not both", path="src/a.py", tail=1, offset=2)
        self.refused("read", "no line 10 (it has 3)", path="src/a.py", offset=10)

    def test_denied_outside_and_binary_files(self):
        self.refused("read", "denied", path=".env")
        self.refused("read", "denied", path="secrets/key")
        self.refused("read", "outside the project root", path="../x")
        self.ok("read", "binary", path="blob.bin")

    def test_a_block_by_its_first_line(self):
        self.put("blocks/lib.rs", RUST)
        self.put("blocks/m.py", PYTHON)
        text = self.ok(
            "read",
            "[blocks/lib.rs: lines 3-8 of 13]",
            path="blocks/lib.rs",
            at="fn alpha",
        )
        self.assertIn("/// Doc.", text)
        self.assertNotIn("fn beta", text)
        self.ok("read", "lines 13-13 of 13", path="blocks/lib.rs", at="fn gamma")
        text = self.ok(
            "read", "[blocks/m.py: lines 1-4 of 6]", path="blocks/m.py", at="def f"
        )
        self.assertNotIn("def g", text)
        self.refused(
            "read", "no line contains 'fn nope'", path="blocks/lib.rs", at="fn nope"
        )
        self.refused(
            "read", "takes no offset or tail", path="blocks/lib.rs", at="fn a", tail=2
        )

    def test_several_slices_at_once(self):
        self.put("blocks/lib.rs", RUST)
        self.put("blocks/m.py", PYTHON)
        text = self.ok(
            "read",
            "[blocks/lib.rs: lines 1-1 of 13]",
            ranges=[
                {"path": "blocks/lib.rs", "start": 1, "end": 1},
                {"path": "blocks/m.py", "at": "def g"},
            ],
        )
        self.assertIn("[blocks/m.py: lines 5-6 of 6]", text)
        self.assertIn("     6\t    pass", text)
        self.refused(
            "read", "not both", path="blocks/lib.rs", ranges=[{"path": "blocks/m.py"}]
        )


class BlockEndTest(unittest.TestCase):
    def test_braces_strings_chars_and_declarations(self):
        lines = (
            "#[test]\nfn a() {\n    let s = \"}\";\n    if x { y('{') }\n}\nfn b();\n"
            "struct C { x: u8 }\nfn d<'a>(x: &'a str)\n    -> u8\n{\n    0 // }\n}"
        ).splitlines()
        self.assertEqual(block_end(lines, 1, False), 4)
        self.assertEqual(block_end(lines, 5, False), 5)
        self.assertEqual(block_end(lines, 6, False), 6)
        self.assertEqual(block_end(lines, 7, False), 11)

    def test_indentation(self):
        self.assertEqual(block_end(PYTHON.splitlines(), 0, True), 3)


class EditTest(ProjectCase):
    def setUp(self):
        super().setUp()
        self.put("src/a.py", "x = 1\ny = 2\nx = 1\n")

    def test_exact_text(self):
        self.refused(
            "edit", "occurs 2 times", path="src/a.py", old="x = 1", new="x = 9"
        )
        self.ok("edit", "+y = 3", path="src/a.py", old="y = 2", new="y = 3")
        self.refused(
            "edit",
            "edit 2: old not found",
            path="src/a.py",
            edits=[{"old": "y = 3", "new": "y = 4"}, {"old": "nope", "new": "z"}],
        )
        self.assertEqual(self.text_of("src/a.py"), "x = 1\ny = 3\nx = 1\n")
        self.ok(
            "edit",
            "+x = 0",
            path="src/a.py",
            old="x = 1",
            new="x = 0",
            replace_all=True,
        )

    def test_ranges_regions_and_several_files(self):
        one = self.put("e/one.txt", "1\n2\n3\n4\n")
        two = self.put("e/two.txt", "head\n# BEGIN\nold\n# END\ntail")
        self.ok(
            "edit",
            "edited 2 of 2 file(s)",
            files=[
                {
                    "path": "e/one.txt",
                    "edits": [
                        {"start": 2, "end": 3, "expect": "2\n3", "new": "two\nthree"},
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
        self.assertEqual(one.read_text(), "1\ntwo\nthree\nfour\n")
        self.assertEqual(two.read_text(), "head\n# BEGIN\ngenerated\n# END\ntail")
        self.refused(
            "edit",
            "not what `expect` says",
            path="e/one.txt",
            edits=[{"start": 1, "expect": "nope", "new": "x"}],
        )
        self.refused(
            "edit", "needs `expect`", path="e/one.txt", edits=[{"start": 1, "new": "x"}]
        )
        self.refused(
            "edit",
            "overlap",
            path="e/one.txt",
            edits=[
                {"start": 1, "end": 2, "expect": "1\ntwo", "new": "a"},
                {"start": 2, "expect": "two", "new": "b"},
            ],
        )
        self.refused(
            "edit",
            "listed twice",
            files=[
                {"path": "e/one.txt", "edits": [{"old": "1", "new": "x"}]},
                {"path": "e/one.txt", "edits": [{"old": "two", "new": "y"}]},
            ],
        )

    def test_all_or_nothing_across_files(self):
        one = self.put("e/one.txt", "four\n")
        self.put("e/two.txt", "x\n")
        self.refused(
            "edit",
            "e/two.txt: edit 1: old not found",
            files=[
                {"path": "e/one.txt", "edits": [{"old": "four", "new": "4"}]},
                {"path": "e/two.txt", "edits": [{"old": "missing", "new": "x"}]},
            ],
        )
        self.assertEqual(one.read_text(), "four\n")

    @unittest.skipIf(os.name == "nt" or os.geteuid() == 0, "needs a write to fail")
    def test_a_failed_write_puts_every_file_back(self):
        one = self.put("e/one.txt", "a\n")
        two = self.put("e/two.txt", "a\n")
        two.chmod(0o444)
        try:
            self.refused(
                "edit",
                "every file was put back",
                files=[
                    {"path": "e/one.txt", "edits": [{"old": "a", "new": "b"}]},
                    {"path": "e/two.txt", "edits": [{"old": "a", "new": "b"}]},
                ],
            )
            self.assertEqual(one.read_text(), "a\n")
        finally:
            two.chmod(0o644)

    def test_batch_modes_refuse_what_they_would_ignore(self):
        self.put("e/one.txt", "a\n")
        self.refused(
            "edit",
            "replace_all goes in each edit",
            replace_all=True,
            files=[{"path": "e/one.txt", "edits": [{"old": "a", "new": "b"}]}],
        )
        self.refused(
            "read", "take no offset or tail", ranges=[{"path": "e/one.txt"}], tail=1
        )

    def test_malformed_edits_are_refused_by_the_schema(self):
        self.put("e/one.txt", "a\n")
        self.refused(
            "edit",
            "edits[0] missing: new",
            path="e/one.txt",
            edits=[{"start": 1, "expect": "a"}],
        )
        self.refused(
            "edit",
            "edits[0] unknown: replace_al",
            path="e/one.txt",
            edits=[{"old": "a", "new": "q", "replace_al": True}],
        )
        self.refused(
            "edit",
            "files[0].edits[0].start must be integer",
            files=[{"path": "e/one.txt", "edits": [{"start": "1", "new": "x"}]}],
        )
        self.refused("read", "ranges[0] missing: path", ranges=[{"start": 1}])
        self.assertEqual(self.text_of("e/one.txt"), "a\n")

    def test_text_that_cannot_be_written_writes_nothing(self):
        one = self.put("e/one.txt", "a\n")
        two = self.put("e/two.txt", "b\n")
        with self.assertRaisesRegex(ToolError, "e/two.txt: not writable as UTF-8"):
            write_all(
                [
                    (one, "e/one.txt", "a\n", "A\n"),
                    (two, "e/two.txt", "b\n", "\ud800\n"),
                ]
            )
        self.assertEqual(one.read_text(), "a\n")

    def test_protected_files(self):
        self.refused("edit", "protected", path=CONFIG, old="{", new="[")


if __name__ == "__main__":
    unittest.main()
