"""Contract tests: /api/v1/info/ service identity + /api/v1/info/ping liveness."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opal_think import API_VERSION, __version__


def test_info(client: TestClient) -> None:
    response = client.get("/api/v1/info/")
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "opal-think-svc"
    assert body["version"] == __version__
    assert body["api_version"] == API_VERSION == "1.7.1"


def test_info_ping(client: TestClient) -> None:
    response = client.get("/api/v1/info/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # kernel convention: correlation headers ride every response
    assert response.headers["X-Opal-Request-Id"]
    assert response.headers["X-Opal-Transaction-Id"]
