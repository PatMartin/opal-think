"""Contract tests: /api/v1/health/, /live, /ready — shapes, statuses, headers, auth."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from opal_think.app import create_app
from opal_think.config import AuthConfig, ThinkConfig


def _assert_error_shape(body: dict) -> None:
    error = body["error"]
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["request_id"], str) and error["request_id"]
    assert isinstance(error["transaction_id"], str) and error["transaction_id"]


def test_liveness(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["ready"] is True


def test_readiness_unavailable_without_models(client: TestClient) -> None:
    """503 with HealthProbe body when no usable model provider exists."""
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    assert body["ready"] is False
    assert body["details"]["models_available"] == 0


def test_readiness_ok_with_model(tmp_path) -> None:
    app = create_app(ThinkConfig(db_path=tmp_path / "t.db"))
    app.state.runtime.models_available = 1
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert body["status"] == "ok"


def test_health_summary_shape(client: TestClient) -> None:
    response = client.get("/api/v1/health/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded", "unavailable"}
    assert body["ready"] is False
    assert body["models_available"] == 0
    assert body["event"] in {"available", "degraded", "unavailable", "not_configured"}
    assert body["active_tasks"] == 0
    assert body["pending_restart_fields"] == []


def test_health_degraded_when_event_fails(tmp_path) -> None:
    app = create_app(ThinkConfig(db_path=tmp_path / "t.db"))
    app.state.runtime.models_available = 2
    app.state.runtime.event_status = "unavailable"
    with TestClient(app) as client:
        body = client.get("/api/v1/health/").json()
    assert body["status"] == "degraded"
    assert body["ready"] is True


def test_opal_headers_echoed_on_success(client: TestClient) -> None:
    response = client.get(
        "/api/v1/health/live",
        headers={
            "X-Opal-Session-Id": "sess-1",
            "X-Opal-Transaction-Id": "txn-1",
            "X-Opal-Causation-Id": "cause-1",
        },
    )
    assert response.status_code == 200
    assert response.headers["X-Opal-Session-Id"] == "sess-1"
    assert response.headers["X-Opal-Transaction-Id"] == "txn-1"
    assert response.headers["X-Opal-Causation-Id"] == "cause-1"
    assert response.headers["X-Opal-Request-Id"]


def test_opal_ids_generated_when_absent(client: TestClient) -> None:
    response = client.get("/api/v1/health/live")
    assert response.headers["X-Opal-Transaction-Id"]
    assert response.headers["X-Opal-Request-Id"]
    assert "X-Opal-Session-Id" not in response.headers


@pytest.mark.parametrize("header", ["X-Opal-Session-Id", "X-Opal-Transaction-Id", "X-Opal-Request-Id", "X-Opal-Causation-Id"])
def test_malformed_opal_header_rejected(client: TestClient, header: str) -> None:
    response = client.get("/api/v1/health/live", headers={header: "has whitespace"})
    assert response.status_code == 422
    body = response.json()
    _assert_error_shape(body)
    assert body["error"]["code"] == "invalid_header"
    assert body["error"]["details"][0]["header"] == header


def test_opal_headers_echoed_on_error(client: TestClient) -> None:
    response = client.get("/api/v1/health/", headers={"X-Opal-Transaction-Id": "txn-err"})
    assert response.status_code == 200
    assert response.headers["X-Opal-Transaction-Id"] == "txn-err"
    error_response = client.get("/api/v1/nope", headers={"X-Opal-Transaction-Id": "txn-err"})
    assert error_response.status_code == 404
    _assert_error_shape(error_response.json())
    assert error_response.headers["X-Opal-Transaction-Id"] == "txn-err"


def test_auth_disabled_on_loopback(client: TestClient) -> None:
    assert client.get("/api/v1/health/").status_code == 200


def test_auth_required_when_enabled(authed_client: TestClient) -> None:
    assert authed_client.get("/api/v1/health/").status_code == 401
    assert authed_client.get("/api/v1/health/", headers={"Authorization": "Bearer wrong"}).status_code == 401
    response = authed_client.get("/api/v1/health/", headers={"Authorization": "Bearer test-token-123"})
    assert response.status_code == 200


def test_auth_enabled_but_token_unset(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPAL_THINK_API_TOKEN", raising=False)
    config = ThinkConfig(db_path=tmp_path / "t.db", auth=AuthConfig(enabled=True))
    with TestClient(create_app(config)) as client:
        response = client.get("/api/v1/health/", headers={"Authorization": "Bearer whatever"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth_misconfigured"


def test_non_loopback_without_auth_refused(tmp_path) -> None:
    config = ThinkConfig(host="0.0.0.0", db_path=tmp_path / "t.db")
    with pytest.raises(ValueError, match="non-loopback"):
        create_app(config)
