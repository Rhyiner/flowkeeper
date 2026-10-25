"""Minimal client for the n8n public REST API (``/api/v1``), standard library only."""

from __future__ import annotations

import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Iterator, List, Optional

from flowkeeper import __version__

# Workflow settings accepted by the public API (n8n 2.44). n8n rejects
# unknown settings keys ("request/body/settings must NOT have additional
# properties") and the list changes between versions, so payloads are
# retried with progressively smaller sets of settings.
API_SETTINGS = (
    "saveExecutionProgress",
    "saveManualExecutions",
    "saveDataErrorExecution",
    "saveDataSuccessExecution",
    "executionTimeout",
    "errorWorkflow",
    "timezone",
    "executionOrder",
    "binaryMode",
    "callerPolicy",
    "callerIds",
    "timeSavedMode",
    "timeSavedPerExecution",
    "redactionPolicy",
    "availableInMCP",
    "customTelemetryTags",
    "credentialResolverId",
)
# What every n8n 1.x and 2.x version accepts.
LEGACY_SETTINGS = (
    "saveExecutionProgress",
    "saveManualExecutions",
    "saveDataErrorExecution",
    "saveDataSuccessExecution",
    "executionTimeout",
    "errorWorkflow",
    "timezone",
    "executionOrder",
)

RETRY_STATUSES = {429, 502, 503, 504}
SCHEMA_ERROR = re.compile(r"additional properties|unrecognized key", re.IGNORECASE)


class N8nError(Exception):
    """Raised when n8n cannot be reached or answers with an error."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


def api_base_url(url: str) -> str:
    """Accept ``http://host:5678``, ``.../`` or ``.../api/v1`` and return the API root."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise N8nError("No n8n URL given (use --url or the N8N_URL variable).")
    if not urllib.parse.urlsplit(url).scheme:
        url = "http://" + url
    if not url.endswith("/api/v1"):
        url += "/api/v1"
    return url


class N8nClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        verify_tls: bool = True,
        retries: int = 3,
    ):
        if not api_key:
            raise N8nError("No API key given (use --api-key or the N8N_API_KEY variable).")
        self.base_url = api_base_url(url)
        self._api_key = api_key
        self.timeout = timeout
        self.retries = max(1, retries)
        self._ssl_context = None
        if not verify_tls:
            self._ssl_context = ssl.create_default_context()
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE
        self._payload_level = 0
        self._has_publish: Optional[bool] = None

    # -- low level -----------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        body: Any = None,
    ) -> Any:
        url = self.base_url + path
        if params:
            query = {k: v for k, v in params.items() if v is not None}
            if query:
                url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {
            "X-N8N-API-KEY": self._api_key,
            "Accept": "application/json",
            "User-Agent": f"flowkeeper/{__version__}",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        # Only idempotent requests are retried: retrying a POST could
        # create the same workflow twice.
        attempts = self.retries if method in ("GET", "PUT") else 1
        for attempt in range(1, attempts + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout, context=self._ssl_context) as resp:
                    payload = resp.read()
                    if not payload:
                        return None
                    return json.loads(payload.decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in RETRY_STATUSES and attempt < attempts:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise N8nError(self._describe_http_error(method, path, exc), exc.code) from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt < attempts:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                reason = getattr(exc, "reason", exc)
                raise N8nError(f"Cannot reach n8n at {self.base_url}: {reason}") from None
            except json.JSONDecodeError:
                raise N8nError(
                    f"{method} {path}: n8n answered with something that is not JSON. "
                    "Check that the URL points to n8n."
                ) from None
        raise N8nError(f"{method} {path}: giving up after {attempts} attempts")  # pragma: no cover

    @staticmethod
    def _describe_http_error(method: str, path: str, exc: urllib.error.HTTPError) -> str:
        detail = ""
        try:
            body = json.loads(exc.read().decode("utf-8"))
            detail = body.get("message") or body.get("error") or ""
        except Exception:  # noqa: BLE001 - the body is only used for the message
            pass
        hints = {
            401: "the API key is missing, wrong or expired",
            403: "the API key is not allowed to do this (check its scopes)",
            404: "not found",
        }
        text = f"{method} {path} failed with HTTP {exc.code}"
        if detail:
            text += f": {detail}"
        if exc.code in hints and hints[exc.code] not in text:
            text += f" ({hints[exc.code]})"
        return text

    # -- workflows -----------------------------------------------------

    def iter_workflows(
        self,
        *,
        tags: Optional[List[str]] = None,
        active: Optional[bool] = None,
        exclude_pin_data: bool = True,
    ) -> Iterator[Dict[str, Any]]:
        params: Dict[str, Any] = {"limit": 250}
        if exclude_pin_data:
            params["excludePinnedData"] = "true"
        if tags:
            params["tags"] = ",".join(tags)
        if active is not None:
            params["active"] = "true" if active else "false"
        cursor = None
        while True:
            page = self.request("GET", "/workflows", params={**params, "cursor": cursor})
            for workflow in page.get("data", []):
                yield workflow
            cursor = page.get("nextCursor")
            if not cursor:
                return

    def get_workflow(self, workflow_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self.request("GET", f"/workflows/{urllib.parse.quote(workflow_id)}")
        except N8nError as exc:
            if exc.status in (400, 404):
                return None
            raise

    def _payloads(self, workflow: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Request bodies to try, from most complete to most conservative.

        ``staticData`` is never sent: it holds runtime state (for example the
        last item a polling trigger saw) that a deployment must not reset.
        """
        base = {
            "name": workflow["name"],
            "nodes": workflow.get("nodes") or [],
            "connections": workflow.get("connections") or {},
        }
        settings = workflow.get("settings") or {}
        extras = {}
        for key in ("description", "nodeGroups", "pinData"):
            if workflow.get(key):
                extras[key] = workflow[key]
        full = dict(base, settings=dict(settings), **extras)
        filtered = dict(base, settings={k: v for k, v in settings.items() if k in API_SETTINGS}, **extras)
        legacy = dict(base, settings={k: v for k, v in settings.items() if k in LEGACY_SETTINGS})
        return [full, filtered, legacy]

    def _send_workflow(self, method: str, path: str, workflow: Dict[str, Any]) -> Dict[str, Any]:
        payloads = self._payloads(workflow)
        last_error: Optional[N8nError] = None
        for level in range(self._payload_level, len(payloads)):
            try:
                result = self.request(method, path, body=payloads[level])
                self._payload_level = level  # remember what this instance accepts
                return result
            except N8nError as exc:
                # n8n 1.x says "must NOT have additional properties",
                # n8n 2.x says "Unrecognized key(s) in object".
                schema_error = exc.status == 400 and bool(SCHEMA_ERROR.search(str(exc)))
                if not schema_error:
                    raise
                last_error = exc
        assert last_error is not None
        raise last_error

    def create_workflow(self, workflow: Dict[str, Any]) -> Dict[str, Any]:
        return self._send_workflow("POST", "/workflows", workflow)

    def update_workflow(self, workflow_id: str, workflow: Dict[str, Any]) -> Dict[str, Any]:
        return self._send_workflow("PUT", f"/workflows/{urllib.parse.quote(workflow_id)}", workflow)

    def publish(self, workflow_id: str, *, name: Optional[str] = None, description: Optional[str] = None) -> Any:
        """Publish the latest version (n8n 2.x); activate it on n8n 1.x.

        ``name`` and ``description`` label the version in n8n's history,
        which is how a deployment can be traced back to a Git commit.
        """
        quoted = urllib.parse.quote(workflow_id)
        if self._has_publish is not False:
            body = {}
            if name:
                body["name"] = name[:128]
            if description:
                body["description"] = description[:255]
            try:
                result = self.request("POST", f"/workflows/{quoted}/publish", body=body)
                self._has_publish = True
                return result
            except N8nError as exc:
                # The workflow was just read or written, so a 404 here means
                # this n8n has no /publish endpoint (1.x): fall back.
                if exc.status not in (404, 405) or self._has_publish:
                    raise
                self._has_publish = False
        return self.request("POST", f"/workflows/{quoted}/activate")

    def unpublish(self, workflow_id: str) -> Any:
        quoted = urllib.parse.quote(workflow_id)
        if self._has_publish is not False:
            try:
                result = self.request("POST", f"/workflows/{quoted}/unpublish")
                self._has_publish = True
                return result
            except N8nError as exc:
                if exc.status not in (404, 405) or self._has_publish:
                    raise
                self._has_publish = False
        return self.request("POST", f"/workflows/{quoted}/deactivate")

    # -- tags ----------------------------------------------------------

    def list_tags(self) -> List[Dict[str, Any]]:
        tags: List[Dict[str, Any]] = []
        cursor = None
        while True:
            page = self.request("GET", "/tags", params={"limit": 250, "cursor": cursor})
            tags.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return tags

    def create_tag(self, name: str) -> Dict[str, Any]:
        return self.request("POST", "/tags", body={"name": name})

    def set_workflow_tags(self, workflow_id: str, tag_ids: List[str]) -> Any:
        body = [{"id": tag_id} for tag_id in tag_ids]
        return self.request("PUT", f"/workflows/{urllib.parse.quote(workflow_id)}/tags", body=body)
