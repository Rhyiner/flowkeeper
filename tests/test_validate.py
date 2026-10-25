import json
import tempfile
import unittest
from pathlib import Path

import helpers
from fake_n8n import connect, set_node, trigger_node
from flowkeeper.normalize import dumps, normalize_workflow


def write(directory: Path, name: str, data, raw: bool = False) -> Path:
    path = directory / name
    path.write_text(data if raw else dumps(normalize_workflow(data, keep_pin_data=True)), encoding="utf-8")
    return path


def good(name="Good", **extra):
    wf = {
        "id": "w1",
        "name": name,
        "active": True,
        "nodes": [trigger_node(), set_node()],
        "connections": connect("Schedule", "Set"),
        "settings": {},
    }
    wf.update(extra)
    return wf


class ValidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def validate(self, *extra):
        return helpers.run(["validate", "--dir", str(self.dir), *extra])

    def test_valid_directory(self):
        write(self.dir, "good.json", good())
        code, out, err = self.validate()
        self.assertEqual(code, 0, err)
        self.assertIn("0 error(s), 0 warning(s)", out)

    def test_invalid_json(self):
        write(self.dir, "broken.json", "{not json", raw=True)
        code, _, err = self.validate()
        self.assertEqual(code, 1)
        self.assertIn("invalid JSON", err)

    def test_structure_errors(self):
        wf = good()
        wf["nodes"].append(dict(set_node(), id="n3"))  # duplicate name "Set"
        wf["nodes"][0]["position"] = "here"
        wf["connections"].update(connect("Set", "Ghost"))
        write(self.dir, "bad.json", json.dumps(wf), raw=True)
        code, _, err = self.validate()
        self.assertEqual(code, 1)
        self.assertIn("two nodes have this name", err)
        self.assertIn('"position" must be [x, y]', err)
        self.assertIn('unknown node "Ghost"', err)

    def test_duplicate_names_and_ids_across_files(self):
        write(self.dir, "a.json", good("Same"))
        write(self.dir, "b.json", good("Same"))
        code, _, err = self.validate()
        self.assertEqual(code, 1)
        self.assertIn('id "w1" is used by several files', err)
        self.assertIn('workflow name "Same" is used by several files', err)

    def test_detects_known_secret(self):
        node = set_node(value="ghp_" + "a" * 36)
        write(self.dir, "secret.json", good(nodes=[trigger_node(), node]))
        code, _, err = self.validate()
        self.assertEqual(code, 1)
        self.assertIn("looks like a GitHub token", err)

    def test_secret_like_header_is_a_warning(self):
        http = {
            "id": "n2",
            "name": "Call API",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [200, 0],
            "parameters": {"headerParameters": {"parameters": [{"name": "X-Api-Key", "value": "supersecretvalue"}]}},
        }
        write(self.dir, "header.json", good(nodes=[trigger_node(), http], connections=connect("Schedule", "Call API")))
        code, _, err = self.validate()
        self.assertEqual(code, 0)
        self.assertIn('"X-Api-Key" looks like a hard-coded secret', err)
        code, _, _ = self.validate("--strict")
        self.assertEqual(code, 1)

    def test_expressions_are_not_secrets(self):
        node = set_node(value="={{ $env.GITHUB_TOKEN }}")
        node["parameters"]["token"] = "={{ $json.token }}"
        write(self.dir, "expr.json", good(nodes=[trigger_node(), node]))
        code, _, err = self.validate("--strict")
        self.assertEqual(code, 0, err)

    def test_active_without_trigger_warns(self):
        write(self.dir, "notrigger.json", good(nodes=[set_node()], connections={}))
        _, _, err = self.validate()
        self.assertIn("no enabled trigger node", err)

    def test_not_canonical_warns_and_format_fixes(self):
        write(self.dir, "messy.json", json.dumps(good(createdAt="x")), raw=True)
        _, _, err = self.validate()
        self.assertIn("not in canonical form", err)
        code, out, _ = helpers.run(["format", "--check", "--dir", str(self.dir)])
        self.assertEqual(code, 1)
        self.assertIn("would reformat", out)
        code, _, _ = helpers.run(["format", "--dir", str(self.dir)])
        self.assertEqual(code, 0)
        code, _, err = self.validate("--strict")
        self.assertEqual(code, 0, err)


if __name__ == "__main__":
    unittest.main()
