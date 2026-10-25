"""A small in-memory imitation of the n8n public API, for tests.

It reproduces the behaviours flowkeeper depends on: API key check,
cursor pagination, strict request bodies ("must NOT have additional
properties"), drafts versus published versions (n8n 2.x) and tags.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

API_KEY = "test-key"
WRITABLE = {"name", "nodes", "connections", "settings", "staticData", "description", "nodeGroups", "pinData", "parentFolderId"}
SETTINGS = {
    "saveExecutionProgress",
    "saveManualExecutions",
    "saveDataErrorExecution",
    "saveDataSuccessExecution",
    "executionTimeout",
    "errorWorkflow",
    "timezone",
    "executionOrder",
    "callerPolicy",
    "callerIds",
}


class FakeN8n:
    def __init__(self, *, page_size: int = 2, has_publish: bool = True, settings: Optional[set] = None):
        self.workflows: Dict[str, Dict[str, Any]] = {}
        self.tags: Dict[str, Dict[str, Any]] = {}
        self.page_size = page_size
        self.has_publish = has_publish
        self.settings = SETTINGS if settings is None else settings
        self.calls: List[str] = []
        self.published_versions: List[Dict[str, Any]] = []
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # -- lifecycle ------------------------------------------------------

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> "FakeN8n":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    # -- helpers for tests -----------------------------------------------

    def add(self, name: str, *, active: bool = False, tags: Optional[List[str]] = None, **fields: Any) -> Dict[str, Any]:
        wf_id = uuid.uuid4().hex[:16]
        version = uuid.uuid4().hex
        wf = {
            "id": wf_id,
            "name": name,
            "active": active,
            "isArchived": False,
            "createdAt": "2026-01-01T00:00:00.000Z",
            "updatedAt": "2026-01-01T00:00:00.000Z",
            "versionId": version,
            "activeVersionId": version if active else None,
            "triggerCount": 1 if active else 0,
            "nodes": fields.pop("nodes", [trigger_node()]),
            "connections": fields.pop("connections", {}),
            "settings": fields.pop("settings", {"executionOrder": "v1"}),
            "staticData": None,
            "meta": {"instanceId": "abc"},
            "pinData": {},
            "tags": [self._tag(name) for name in tags or []],
            "shared": [{"role": "workflow:owner"}],
        }
        wf.update(fields)
        self.workflows[wf_id] = wf
        return wf

    def _tag(self, name: str) -> Dict[str, Any]:
        for tag in self.tags.values():
            if tag["name"] == name:
                return tag
        tag = {"id": uuid.uuid4().hex[:8], "name": name, "createdAt": "x", "updatedAt": "x"}
        self.tags[tag["id"]] = tag
        return tag

    # -- HTTP -------------------------------------------------------------

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def _send(self, status: int, body: Any = None) -> None:
                data = json.dumps(body if body is not None else {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _body(self) -> Any:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length)) if length else None

            def _route(self, method: str) -> None:
                path, _, query = self.path.partition("?")
                params = {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}
                fake.calls.append(f"{method} {path}")
                if self.headers.get("X-N8N-API-KEY") != API_KEY:
                    return self._send(401, {"message": "unauthorized"})
                if not path.startswith("/api/v1/"):
                    return self._send(404, {"message": "not found"})
                route = path[len("/api/v1"):]
                try:
                    status, body = fake.dispatch(method, route, params, self._body())
                except BadRequest as exc:
                    status, body = 400, {"message": str(exc)}
                self._send(status, body)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_PUT(self):
                self._route("PUT")

        return Handler

    def dispatch(self, method: str, route: str, params: Dict[str, str], body: Any):
        if route == "/workflows" and method == "GET":
            items = [w for w in self.workflows.values()]
            if "tags" in params:
                wanted = set(params["tags"].split(","))
                items = [w for w in items if wanted & {t["name"] for t in w["tags"]}]
            if params.get("active") == "true":
                items = [w for w in items if w["activeVersionId"]]
            # Like n8n 2.x, the list leaves out the description.
            items = [{k: v for k, v in w.items() if k != "description"} for w in items]
            return 200, self._page(items, params)
        if route == "/workflows" and method == "POST":
            self._check_body(body)
            wf = self.add(body["name"], **{k: v for k, v in body.items() if k != "name"})
            return 200, wf
        if route == "/tags" and method == "GET":
            return 200, self._page(list(self.tags.values()), params)
        if route == "/tags" and method == "POST":
            return 201, self._tag(body["name"])

        match = re.fullmatch(r"/workflows/([^/]+)(?:/(\w+))?", route)
        if not match:
            return 404, {"message": "not found"}
        wf = self.workflows.get(match.group(1))
        action = match.group(2)
        if action in ("publish", "unpublish") and not self.has_publish:
            return 404, {"message": "not found"}
        if wf is None:
            return 404, {"message": "Not Found"}
        if action is None and method == "GET":
            return 200, wf
        if action is None and method == "PUT":
            self._check_body(body)
            wf.update(body)
            wf["versionId"] = uuid.uuid4().hex  # a draft: the published version does not change
            if not self.has_publish and wf["active"]:
                wf["activeVersionId"] = wf["versionId"]  # n8n 1.x re-activates on save
            return 200, wf
        if action in ("publish", "activate") and method == "POST":
            if not any(n["type"].lower().endswith("trigger") for n in wf["nodes"]):
                raise BadRequest("Workflow has no node to start the workflow")
            wf["active"] = True
            wf["activeVersionId"] = wf["versionId"]
            self.published_versions.append({"workflow": wf["id"], **(body or {})})
            return 200, wf
        if action in ("unpublish", "deactivate") and method == "POST":
            wf["active"] = False
            wf["activeVersionId"] = None
            return 200, wf
        if action == "tags" and method == "PUT":
            wf["tags"] = [self.tags[t["id"]] for t in body]
            return 200, wf["tags"]
        return 404, {"message": "not found"}

    def _check_body(self, body: Dict[str, Any]) -> None:
        extra = set(body) - WRITABLE
        if extra:
            raise BadRequest("request/body must NOT have additional properties")
        unknown = sorted(set(body.get("settings") or {}) - self.settings)
        if unknown:
            # The wording of n8n 2.x; 1.x says "must NOT have additional properties".
            keys = ", ".join(f"'{k}'" for k in unknown)
            raise BadRequest(f"request/body/settings Unrecognized key(s) in object: {keys}")
        for required in ("name", "nodes", "connections", "settings"):
            if required not in body:
                raise BadRequest(f"request/body must have required property '{required}'")

    def _page(self, items: List[Dict[str, Any]], params: Dict[str, str]) -> Dict[str, Any]:
        start = int(base64.b64decode(params["cursor"]).decode()) if params.get("cursor") else 0
        size = min(self.page_size, int(params.get("limit", 100)))
        chunk = items[start : start + size]
        nxt = start + size
        cursor = base64.b64encode(str(nxt).encode()).decode() if nxt < len(items) else None
        return {"data": chunk, "nextCursor": cursor}


class BadRequest(Exception):
    pass


def trigger_node(name: str = "Schedule", node_id: str = "n1") -> Dict[str, Any]:
    return {
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.scheduleTrigger",
        "typeVersion": 1.2,
        "position": [0, 0],
        "parameters": {"rule": {"interval": [{}]}},
    }


def set_node(name: str = "Set", node_id: str = "n2", value: str = "hello") -> Dict[str, Any]:
    return {
        "id": node_id,
        "name": name,
        "type": "n8n-nodes-base.set",
        "typeVersion": 3.4,
        "position": [220, 0],
        "parameters": {"assignments": {"assignments": [{"name": "msg", "value": value, "type": "string"}]}},
    }


def connect(source: str, target: str) -> Dict[str, Any]:
    return {source: {"main": [[{"node": target, "type": "main", "index": 0}]]}}
