"""Prepare a fresh n8n instance for the end-to-end tests.

Creates the owner account, then an API key with every scope, and prints
the key. It uses n8n's internal ``/rest`` endpoints (the ones the editor
uses), which may change between n8n versions: it is pinned to the version
tested in CI.

    python tests/e2e/bootstrap.py http://127.0.0.1:5678
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

EMAIL = "e2e@example.com"
PASSWORD = "Flowkeeper-e2e-1"


def main(base: str) -> str:
    base = base.rstrip("/")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def call(method: str, path: str, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "browser-id": "flowkeeper-e2e"},
        )
        with opener.open(req, timeout=30) as resp:
            payload = resp.read()
            return json.loads(payload) if payload else None

    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/healthz/readiness", timeout=5)
            break
        except (urllib.error.URLError, ConnectionError):
            time.sleep(2)

    try:
        call(
            "POST",
            "/rest/owner/setup",
            {"email": EMAIL, "firstName": "E2E", "lastName": "Tests", "password": PASSWORD},
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 400:  # 400: the owner already exists
            raise
    call("POST", "/rest/login", {"emailOrLdapLoginId": EMAIL, "password": PASSWORD})
    scopes = call("GET", "/rest/api-keys/scopes")["data"]
    created = call(
        "POST",
        "/rest/api-keys",
        {"label": f"flowkeeper-e2e-{int(time.time())}", "expiresAt": None, "scopes": scopes},
    )["data"]
    return created["rawApiKey"] if "rawApiKey" in created else created["apiKey"]


if __name__ == "__main__":
    print(main(sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5678"))
