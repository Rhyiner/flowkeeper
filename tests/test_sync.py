import json
import tempfile
import unittest
from pathlib import Path

import helpers
from fake_n8n import API_KEY, FakeN8n, connect, set_node, trigger_node
from flowkeeper.normalize import dumps, normalize_workflow


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "workflows"

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, n8n: FakeN8n, *args, env=None):
        return helpers.run(
            [*args, "--dir", str(self.dir)],
            env={"N8N_URL": n8n.url, "N8N_API_KEY": API_KEY, **(env or {})},
        )

    def files(self):
        return sorted(p.name for p in self.dir.glob("*.json"))

    def load(self, name):
        return json.loads((self.dir / name).read_text())

    def save(self, name, data):
        (self.dir / name).write_text(dumps(normalize_workflow(data)), encoding="utf-8")

    # -- export -------------------------------------------------------

    def test_export_writes_one_file_per_workflow_across_pages(self):
        with FakeN8n(page_size=2) as n8n:
            for i in range(5):
                n8n.add(f"Workflow {i}")
            code, out, err = self.cli(n8n, "export")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.files()), 5)
        self.assertIn("5 workflow(s) exported", out)
        data = self.load("workflow-0.json")
        self.assertNotIn("createdAt", data)
        self.assertNotIn("versionId", data)

    def test_export_is_idempotent_and_follows_renames(self):
        with FakeN8n() as n8n:
            wf = n8n.add("Old name")
            self.cli(n8n, "export")
            code, out, _ = self.cli(n8n, "export")
            self.assertIn("0 written", out)
            wf["name"] = "New name"
            code, out, _ = self.cli(n8n, "export")
        self.assertEqual(self.files(), ["new-name.json"])
        self.assertIn("renamed", out)

    def test_export_keeps_files_in_sub_folders(self):
        with FakeN8n() as n8n:
            wf = n8n.add("Nested")
            self.cli(n8n, "export")
            (self.dir / "ai").mkdir()
            (self.dir / "nested.json").rename(self.dir / "ai" / "nested.json")
            wf["name"] = "Nested renamed"
            code, out, err = self.cli(n8n, "export")
            self.assertEqual(code, 0, out + err)
        self.assertEqual([p.relative_to(self.dir).as_posix() for p in self.dir.rglob("*.json")], ["ai/nested-renamed.json"])

    def test_export_same_slug_gets_ids(self):
        with FakeN8n() as n8n:
            a = n8n.add("Report")
            b = n8n.add("report!")
            self.cli(n8n, "export")
        self.assertEqual(self.files(), sorted([f"report--{a['id']}.json", f"report--{b['id']}.json"]))

    def test_export_prune(self):
        with FakeN8n() as n8n:
            keep = n8n.add("Keep")
            gone = n8n.add("Gone")
            self.cli(n8n, "export")
            del n8n.workflows[gone["id"]]
            self.cli(n8n, "export")
            self.assertIn("gone.json", self.files())
            self.cli(n8n, "export", "--prune")
        self.assertEqual(self.files(), ["keep.json"])

    def test_export_filters_by_tag(self):
        with FakeN8n() as n8n:
            n8n.add("Tagged", tags=["gitops"])
            n8n.add("Other")
            self.cli(n8n, "export", "--tag", "gitops")
        self.assertEqual(self.files(), ["tagged.json"])

    def test_description_survives_round_trip(self):
        with FakeN8n() as n8n:
            n8n.add("Described", description="What it does")
            self.cli(n8n, "export")
            self.assertEqual(self.load("described.json")["description"], "What it does")
            code, out, _ = self.cli(n8n, "plan", "--detailed-exitcode")
        self.assertEqual(code, 0, out)

    def test_bad_api_key(self):
        with FakeN8n() as n8n:
            code, _, err = helpers.run(
                ["export", "--dir", str(self.dir)], env={"N8N_URL": n8n.url, "N8N_API_KEY": "wrong"}
            )
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401", err)

    # -- plan / deploy ------------------------------------------------

    def test_round_trip_has_no_changes(self):
        with FakeN8n() as n8n:
            n8n.add("A", active=True, tags=["prod"])
            n8n.add("B")
            self.cli(n8n, "export")
            code, out, err = self.cli(n8n, "plan", "--detailed-exitcode")
        self.assertEqual(code, 0, out + err)
        self.assertIn("0 to create, 0 to update, 2 unchanged", out)

    def test_update_is_deployed_and_published_with_commit_name(self):
        with FakeN8n() as n8n:
            wf = n8n.add("Daily", active=True, nodes=[trigger_node(), set_node()], connections=connect("Schedule", "Set"))
            self.cli(n8n, "export")
            data = self.load("daily.json")
            data["nodes"][1]["parameters"]["assignments"]["assignments"][0]["value"] = "changed"
            self.save("daily.json", data)

            code, out, _ = self.cli(n8n, "plan", "--diff", "--detailed-exitcode")
            self.assertEqual(code, 2)
            self.assertIn("~ Daily", out)
            self.assertIn("nodes: modified Set", out)
            self.assertIn('+', out)

            env = {"CI_COMMIT_SHORT_SHA": "a1b2c3d4", "CI_COMMIT_TITLE": "Change greeting"}
            code, out, err = self.cli(n8n, "deploy", env=env)
            self.assertEqual(code, 0, out + err)
            self.assertIn("updated, published", out)
            remote = n8n.workflows[wf["id"]]
            self.assertEqual(remote["nodes"][1]["parameters"]["assignments"]["assignments"][0]["value"], "changed")
            self.assertEqual(remote["activeVersionId"], remote["versionId"])
            self.assertEqual(n8n.published_versions[-1]["name"], "git a1b2c3d4")
            self.assertEqual(n8n.published_versions[-1]["description"], "Change greeting")

            code, out, _ = self.cli(n8n, "plan", "--detailed-exitcode")
            self.assertEqual(code, 0, out)

    def test_new_file_is_created_tagged_and_activated(self):
        with FakeN8n() as n8n:
            self.dir.mkdir()
            self.save("new.json", {
                "name": "Brand new",
                "active": True,
                "tags": ["gitops", "ai"],
                "settings": {"executionOrder": "v1"},
                "nodes": [trigger_node()],
                "connections": {},
            })
            code, out, err = self.cli(n8n, "deploy")
            self.assertEqual(code, 0, out + err)
            self.assertIn("+ Brand new", out)
            created = next(w for w in n8n.workflows.values() if w["name"] == "Brand new")
            self.assertTrue(created["activeVersionId"])
            self.assertEqual(sorted(t["name"] for t in created["tags"]), ["ai", "gitops"])
            # Matched by name next time, even though the file has no id yet.
            code, out, _ = self.cli(n8n, "plan", "--detailed-exitcode")
            self.assertEqual(code, 0, out)
            # Exporting adopts the hand-written file instead of duplicating it.
            self.cli(n8n, "export")
            self.assertEqual(self.files(), ["brand-new.json"])
            self.assertEqual(self.load("brand-new.json")["id"], created["id"])

    def test_deactivation(self):
        with FakeN8n() as n8n:
            wf = n8n.add("Stop me", active=True)
            self.cli(n8n, "export")
            data = self.load("stop-me.json")
            data["active"] = False
            self.save("stop-me.json", data)
            code, out, _ = self.cli(n8n, "deploy")
            self.assertEqual(code, 0, out)
            self.assertIn("unpublished", out)
            self.assertIsNone(n8n.workflows[wf["id"]]["activeVersionId"])

    def test_no_activation_flag(self):
        with FakeN8n() as n8n:
            wf = n8n.add("Leave me", active=True)
            self.cli(n8n, "export")
            data = self.load("leave-me.json")
            data["active"] = False
            self.save("leave-me.json", data)
            code, out, _ = self.cli(n8n, "deploy", "--no-activation")
            self.assertIn("Nothing to deploy", out)
            self.assertTrue(n8n.workflows[wf["id"]]["activeVersionId"])

    def test_unpublished_draft_in_n8n_is_replaced_by_git(self):
        with FakeN8n() as n8n:
            wf = n8n.add("Drafty", active=True)
            self.cli(n8n, "export")
            wf["versionId"] = "someone-edited-in-the-ui"
            code, out, _ = self.cli(n8n, "plan")
            self.assertIn("unpublished changes", out)
            code, out, _ = self.cli(n8n, "deploy")
            self.assertEqual(code, 0, out)
            self.assertEqual(wf["activeVersionId"], "someone-edited-in-the-ui")

    def test_unknown_settings_fall_back(self):
        with FakeN8n(settings={"executionOrder", "timezone"}) as n8n:
            wf = n8n.add("Settings", settings={"executionOrder": "v1"})
            self.cli(n8n, "export")
            data = self.load("settings.json")
            data["settings"]["availableInMCP"] = True
            data["settings"]["timezone"] = "America/New_York"
            self.save("settings.json", data)
            code, out, err = self.cli(n8n, "deploy")
            self.assertEqual(code, 0, out + err)
            self.assertEqual(n8n.workflows[wf["id"]]["settings"], {"executionOrder": "v1", "timezone": "America/New_York"})

    def test_n8n_1x_without_publish_endpoint(self):
        with FakeN8n(has_publish=False) as n8n:
            wf = n8n.add("Old n8n", active=False)
            self.cli(n8n, "export")
            data = self.load("old-n8n.json")
            data["active"] = True
            self.save("old-n8n.json", data)
            code, out, err = self.cli(n8n, "deploy")
            self.assertEqual(code, 0, out + err)
            self.assertTrue(wf["activeVersionId"])
            self.assertIn("POST /api/v1/workflows/%s/activate" % wf["id"], n8n.calls)

    def test_name_conflict_blocks_deploy(self):
        with FakeN8n() as n8n:
            n8n.add("Twin")
            n8n.add("Twin")
            self.dir.mkdir()
            self.save("twin.json", {"name": "Twin", "nodes": [trigger_node()], "connections": {}})
            code, out, err = self.cli(n8n, "deploy")
        self.assertEqual(code, 1)
        self.assertIn("2 workflows are named", out)
        self.assertIn("nothing was deployed", err)

    def test_invalid_files_are_never_deployed(self):
        with FakeN8n() as n8n:
            self.dir.mkdir()
            (self.dir / "broken.json").write_text("{")
            code, _, err = self.cli(n8n, "deploy")
            self.assertEqual(code, 1)
            self.assertIn("Fix the errors above first", err)
            self.assertFalse([c for c in n8n.calls if c.startswith(("POST", "PUT"))])

    def test_failed_publish_is_reported(self):
        with FakeN8n() as n8n:
            self.dir.mkdir()
            self.save("no-trigger.json", {"name": "No trigger", "active": True, "nodes": [set_node()], "connections": {}})
            code, out, err = self.cli(n8n, "deploy", "--no-secret-scan")
        self.assertEqual(code, 1)
        self.assertIn("created", out)
        self.assertIn("no node to start the workflow", out)

    def test_dry_run_changes_nothing(self):
        with FakeN8n() as n8n:
            self.dir.mkdir()
            self.save("dry.json", {"name": "Dry", "nodes": [trigger_node()], "connections": {}})
            code, out, _ = self.cli(n8n, "deploy", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("Dry run", out)
            self.assertEqual(n8n.workflows, {})


if __name__ == "__main__":
    unittest.main()
