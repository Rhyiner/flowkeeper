import json
import unittest

import helpers  # noqa: F401  (adds src/ to the path)
from fake_n8n import connect, set_node, trigger_node
from flowkeeper.normalize import comparable, dumps, normalize_workflow, slugify, tag_names


def raw_workflow(**overrides):
    wf = {
        "id": "abc123",
        "name": "Daily report",
        "active": True,
        "activeVersionId": "v2",
        "versionId": "v2",
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-02-01T00:00:00Z",
        "triggerCount": 1,
        "shared": [{"role": "workflow:owner"}],
        "meta": {"instanceId": "xyz"},
        "staticData": {"lastId": 4},
        "pinData": {"Set": [{"json": {"a": 1}}]},
        "tags": [{"id": "2", "name": "prod"}, {"id": "1", "name": "AI"}],
        "settings": {"timezone": "America/New_York", "executionOrder": "v1"},
        "nodes": [set_node(), trigger_node()],
        "connections": connect("Schedule", "Set"),
    }
    wf.update(overrides)
    return wf


class NormalizeTests(unittest.TestCase):
    def test_drops_bookkeeping_fields(self):
        wf = normalize_workflow(raw_workflow())
        for key in ("createdAt", "updatedAt", "versionId", "activeVersionId", "triggerCount", "shared", "meta"):
            self.assertNotIn(key, wf)
        self.assertNotIn("pinData", wf)
        self.assertNotIn("staticData", wf)

    def test_keeps_pin_and_static_data_on_request(self):
        wf = normalize_workflow(raw_workflow(), keep_pin_data=True, keep_static_data=True)
        self.assertEqual(wf["pinData"], {"Set": [{"json": {"a": 1}}]})
        self.assertEqual(wf["staticData"], {"lastId": 4})

    def test_top_level_order_and_sorted_nodes(self):
        wf = normalize_workflow(raw_workflow())
        self.assertEqual(list(wf)[:6], ["id", "name", "active", "tags", "settings", "nodes"])
        self.assertEqual([n["name"] for n in wf["nodes"]], ["Schedule", "Set"])
        self.assertEqual(list(wf["nodes"][0])[:4], ["id", "name", "type", "typeVersion"])

    def test_tags_become_sorted_names(self):
        self.assertEqual(normalize_workflow(raw_workflow())["tags"], ["AI", "prod"])
        self.assertEqual(tag_names(["b", {"name": "a"}, "b", " "]), ["a", "b"])

    def test_active_follows_active_version_id_on_n8n_2(self):
        self.assertFalse(normalize_workflow(raw_workflow(active=True, activeVersionId=None))["active"])
        self.assertTrue(normalize_workflow({"name": "x", "active": True})["active"])

    def test_output_is_stable(self):
        once = dumps(normalize_workflow(raw_workflow()))
        twice = dumps(normalize_workflow(json.loads(once)))
        self.assertEqual(once, twice)
        self.assertTrue(once.endswith("\n"))

    def test_keeps_node_groups(self):
        groups = [{"id": "g2", "name": "B", "nodeIds": ["n2"]}, {"id": "g1", "name": "A", "nodeIds": ["n1"]}]
        wf = normalize_workflow(raw_workflow(nodeGroups=groups))
        self.assertEqual([g["name"] for g in wf["nodeGroups"]], ["A", "B"])

    def test_slugify(self):
        self.assertEqual(slugify("Customer support (chat)"), "customer-support-chat")
        self.assertEqual(slugify("Mise à jour des données"), "mise-a-jour-des-donnees")
        self.assertEqual(slugify("???"), "workflow")

    def test_comparable_ignores_id_and_unlisted_settings(self):
        local = normalize_workflow(raw_workflow(settings={"timezone": "America/New_York"}))
        remote = normalize_workflow(raw_workflow(id="other", settings={"timezone": "America/New_York", "callerPolicy": "any"}))
        keys = set(local["settings"])
        self.assertEqual(comparable(local, settings_keys=keys), comparable(remote, settings_keys=keys))


if __name__ == "__main__":
    unittest.main()
