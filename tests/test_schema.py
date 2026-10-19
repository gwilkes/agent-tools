"""settings.schema.json: that it holds the settings this repo, the README and
the tests use, refuses what the code refuses, and agrees with the code on
every key and default."""

import json
import re
import unittest
from pathlib import Path

from gci import core, wire
from gci.scripts import DEFAULT_KEYS, SCRIPT_KEYS, SETTINGS_KEYS
from tests.helpers import SETTINGS

REPO = Path(__file__).resolve().parent.parent
SCHEMA = json.loads((REPO / "gci" / "settings.schema.json").read_text())

TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


def is_type(value, name):
    # JSON has no int/bool split, and an integer is a number.
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    if name == "integer":
        return isinstance(value, int)
    if name == "number":
        return isinstance(value, (int, float))
    return isinstance(value, TYPES[name])


def problems(value, schema, where="settings"):
    """What is wrong with `value` against `schema`, for the parts of JSON
    Schema this one uses: $ref, type, properties, additionalProperties,
    required, items, minItems, minimum and oneOf."""
    if "$ref" in schema:
        name = schema["$ref"].removeprefix("#/$defs/")
        return problems(value, SCHEMA["$defs"][name], where)
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(is_type(value, k) for k in kinds):
            return [f"{where}: not {' or '.join(kinds)}"]
    found = []
    if "oneOf" in schema:
        passing = [s for s in schema["oneOf"] if not problems(value, s, where)]
        if len(passing) != 1:
            found.append(f"{where}: matches {len(passing)} of oneOf")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key in schema.get("required", []):
            if key not in value:
                found.append(f"{where}: missing {key}")
        for key, item in value.items():
            if key in props:
                found += problems(item, props[key], f"{where}.{key}")
            elif extra is False:
                found.append(f"{where}: unknown {key}")
            elif isinstance(extra, dict):
                found += problems(item, extra, f"{where}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            found.append(f"{where}: too few items")
        if "items" in schema:
            for i, item in enumerate(value):
                found += problems(item, schema["items"], f"{where}[{i}]")
    if is_type(value, "number") and value < schema.get("minimum", value):
        found.append(f"{where}: below {schema['minimum']}")
    return found


def readme_settings():
    readme = (REPO / "README.md").read_text()
    blocks = re.findall(r"```json\n(.*?)```", readme, re.DOTALL)
    return [json.loads(b) for b in blocks if '"scripts"' in b]


class SchemaHoldsTest(unittest.TestCase):
    def assertHolds(self, settings):
        self.assertEqual(problems(settings, SCHEMA), [])

    def test_this_repo_the_readme_and_the_tests(self):
        self.assertHolds(json.loads((REPO / core.CONFIG).read_text()))
        examples = readme_settings()
        self.assertTrue(examples)
        for example in examples:
            self.assertHolds(example)
        self.assertHolds(SETTINGS)

    def test_defaults_and_their_overrides(self):
        self.assertHolds(
            {
                "defaults": {"tail": 50, "wait": 120, "hosts": ["mac"], "timeout": 0},
                "scripts": {"x": {"command": ["x"], "grep": None, "sync": False}},
                "remote": {"hosts": {"mac": "10.0.0.2"}, "port": 9002},
            }
        )

    def test_what_the_code_refuses(self):
        bad = [
            {"scripts": {"x": {"command": ["x"], "tial": 5}}},
            {"scripts": {"x": {"command": ["x"], "commands": [["y"]]}}},
            {"scripts": {"x": {"description": "no kind"}}},
            {"scripts": {"x": {"command": [["nested"]]}}},
            {"scripts": {"x": {"commands": []}}},
            {"scripts": {"x": {"steps": [["x"]]}}},
            {"scripts": {"x": {"command": ["x"], "tail": 0}}},
            {"defaults": {"sync": "yes"}},
            {"defaults": {"tial": 5}},
            {"timeout": 5},
            {"max_output": 10},
        ]
        for settings in bad:
            self.assertNotEqual(problems(settings, SCHEMA), [], settings)


class SchemaAgreesTest(unittest.TestCase):
    def test_the_same_keys(self):
        defs = SCHEMA["$defs"]
        self.assertEqual(set(SCHEMA["properties"]), SETTINGS_KEYS)
        self.assertEqual(set(defs["script"]["properties"]), SCRIPT_KEYS)
        self.assertEqual(set(defs["runDefaults"]["properties"]), set(DEFAULT_KEYS))

    def test_the_same_defaults(self):
        props, defs = SCHEMA["properties"], SCHEMA["$defs"]
        remote = defs["remote"]["properties"]
        self.assertEqual(props["deny"]["default"], core.DEFAULT_DENY)
        self.assertEqual(props["skip"]["default"], core.DEFAULT_SKIP)
        self.assertEqual(props["tmp"]["default"], core.DEFAULT_TMP)
        self.assertEqual(props["max_output"]["default"], core.MAX_OUTPUT)
        self.assertEqual(defs["defaultTimeout"]["default"], core.DEFAULT_TIMEOUT)
        self.assertEqual(remote["port"]["default"], wire.DEFAULT_PORT)
        self.assertEqual(remote["key_env"]["default"], wire.DEFAULT_KEY_ENV)
        self.assertEqual(remote["key_file"]["default"], wire.DEFAULT_KEY_FILE)


if __name__ == "__main__":
    unittest.main()
