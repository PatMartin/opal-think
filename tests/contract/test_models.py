"""Contract tests: /api/v1/model/ catalog + /api/v1/model/{model_id}."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def fake_models(client: TestClient) -> TestClient:
    """Deterministic catalog: fake provider enabled, ollama pointed at a dead port."""
    client.app.state.settings.store.set_many(
        {
            "providers.ollama.base_url": "http://127.0.0.1:1",
            "providers.fake.type": "fake",
            "providers.fake.enabled": True,
        }
    )
    return client


def test_list_models(fake_models: TestClient) -> None:
    response = fake_models.get("/api/v1/model/")
    assert response.status_code == 200
    ids = [m["id"] for m in response.json()]
    assert ids == sorted(ids)
    assert {"fake:echo", "fake:script"} <= set(ids)
    for m in response.json():
        assert set(m) == {"id", "provider", "name", "available", "capabilities", "context_window", "description"}
        assert set(m["capabilities"]) == {"streaming", "tool_calling", "structured_output", "vision"}


def test_list_models_omits_unreachable_provider(fake_models: TestClient) -> None:
    """The dead-port ollama provider contributes nothing; the catalog still succeeds."""
    ids = [m["id"] for m in fake_models.get("/api/v1/model/").json()]
    assert not any(i.startswith("ollama:") for i in ids)


def test_list_models_503_when_all_providers_fail(client: TestClient) -> None:
    client.app.state.settings.store.set_many({"providers.ollama.base_url": "http://127.0.0.1:1"})
    response = client.get("/api/v1/model/")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "unavailable"


def test_show_model(fake_models: TestClient) -> None:
    response = fake_models.get("/api/v1/model/fake:echo")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "fake:echo" and body["provider"] == "fake"
    assert body["capabilities"]["tool_calling"] is True


def test_show_model_not_found(fake_models: TestClient) -> None:
    for model_id in ("fake:missing", "nada", "other:echo"):
        assert fake_models.get(f"/api/v1/model/{model_id}").status_code == 404, model_id


def test_show_model_provider_down_503(client: TestClient) -> None:
    client.app.state.settings.store.set_many({"providers.ollama.base_url": "http://127.0.0.1:1"})
    assert client.get("/api/v1/model/ollama:llama3.2").status_code == 503
