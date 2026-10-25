"""End-to-end tests against a real n8n instance.

Skipped unless FLOWKEEPER_E2E_URL and FLOWKEEPER_E2E_API_KEY are set.
The instance should be disposable: the tests create workflows in it.

    FLOWKEEPER_E2E_URL=http://127.0.0.1:5678 \\
    FLOWKEEPER_E2E_API_KEY=$(python tests/e2e/bootstrap.py http://127.0.0.1:5678) \\
    python -m unittest discover -s tests -t tests -p "test_e2e.py"
"""

import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import helpers  # noqa: E402
from fake_n8n import connect, set_node, trigger_node  # noqa: E402
from flowkeeper.client import N8nClient  # noqa: E402
from flowkeeper.normalize import dumps, normalize_workflow  # noqa: E402

URL = os.environ.get("FLOWKEEPER_E2E_URL")
KEY = os.environ.get("FLOWKEEPER_E2E_API_KEY")


@unittest.skipUnless(URL and KEY, "set FLOWKEEPER_E2E_URL and FLOWKEEPER_E2E_API_KEY to run")
class RealN8nTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "workflows"
        self.client = N8nClient(URL, KEY)
        self.tag = "e2e-" + uuid.uuid4().hex[:6]

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, env=None):
        return helpers.run(
            [*args, "--dir", str(self.dir)],
            env={"N8N_URL": URL, "N8N_API_KEY": KEY, **(env or {})},
        )

    def files(self):
        return sorted(self.dir.glob("*.json"))

    def test_full_cycle(self):
        # A workflow made "in the UI": created, tagged and published through the API.
        name = f"E2E {self.tag}"
        created = self.client.create_workflow(
            {
                "name": name,
                "nodes": [trigger_node(), set_node()],
                "connections": connect("Schedule", "Set"),
                "settings": {"executionOrder": "v1", "timezone": "America/New_York"},
            }
        )
        tag = self.client.create_tag(self.tag)
        self.client.set_workflow_tags(created["id"], [tag["id"]])
        self.client.publish(created["id"])

        # Export only this test's workflows, then check Git and n8n agree.
        code, out, err = self.cli("export", "--tag", self.tag)
        self.assertEqual(code, 0, out + err)
        [path] = self.files()
        data = json.loads(path.read_text())
        self.assertEqual(data["name"], name)
        self.assertTrue(data["active"])
        self.assertEqual(data["tags"], [self.tag])
        code, out, err = self.cli("validate", "--strict")
        self.assertEqual(code, 0, out + err)

        code, out, err = self.cli("plan", "--detailed-exitcode", "-v")
        self.assertIn(f"= {name}", out)
        self.assertNotIn(f"~ {name}", out, out)

        # Change a node in Git and deploy: n8n must hold and publish the new version.
        data["nodes"][1]["parameters"]["assignments"]["assignments"][0]["value"] = "from git"
        path.write_text(dumps(normalize_workflow(data)))
        code, out, err = self.cli("deploy", env={"FLOWKEEPER_VERSION_NAME": "git e2e1234"})
        self.assertEqual(code, 0, out + err)
        self.assertIn("updated, published", out)
        remote = self.client.get_workflow(created["id"])
        value = next(n for n in remote["nodes"] if n["name"] == "Set")["parameters"]["assignments"]["assignments"][0]["value"]
        self.assertEqual(value, "from git")
        self.assertEqual(remote["activeVersionId"], remote["versionId"])

        code, out, err = self.cli("plan", "-v")
        self.assertIn(f"= {name}", out, out)

        # A new file is created, tagged and published.
        new_name = f"E2E new {self.tag}"
        (self.dir / "new.json").write_text(
            dumps(
                normalize_workflow(
                    {
                        "name": new_name,
                        "active": True,
                        "tags": [self.tag, self.tag + "-extra"],
                        "settings": {"executionOrder": "v1"},
                        "nodes": [trigger_node()],
                        "connections": {},
                    }
                )
            )
        )
        code, out, err = self.cli("deploy")
        self.assertEqual(code, 0, out + err)
        self.assertIn("+ " + new_name, out)

        # Unpublish through Git.
        data = json.loads(path.read_text())
        data["active"] = False
        path.write_text(dumps(normalize_workflow(data)))
        code, out, err = self.cli("deploy")
        self.assertEqual(code, 0, out + err)
        self.assertIn("unpublished", out)
        self.assertIsNone(self.client.get_workflow(created["id"]).get("activeVersionId"))

        # Everything converges; re-exporting picks up the new workflow's id.
        code, out, err = self.cli("export", "--tag", self.tag)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.files()), 2)
        code, out, err = self.cli("plan", "--detailed-exitcode")
        self.assertEqual(code, 0, out + err)
        self.assertIn("0 to create, 0 to update", out)


if __name__ == "__main__":
    unittest.main()
