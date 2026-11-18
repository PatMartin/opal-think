"""Contract tests: persona resolution for tasks and sessions.

Kernel ``x-opal-persona``: Think hosts the authoritative registry; the Think
binding (model/instructions/generation) lives in service-local
``persona.<id>.*`` settings; ``voice_id`` is the Say binding. Precedence is
per-field — explicit request field > persona binding > profile > service
default — and a request referencing an unknown or disabled persona is refused
422. The resolved selection + config revision persist on the task for
inspection and historical reproducibility.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

PERSONA = {"persona_id": "amy", "name": "Amy", "voice_id": "en-gb-alan-low", "enabled": True}


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    return client


def _config_etag(client: TestClient) -> str:
    return client.get("/api/v1/setting/").headers["ETag"]


def _persona_etag(client: TestClient) -> str:
    return client.get("/api/v1/persona/").headers["ETag"]


def _make_persona(client: TestClient, **overrides) -> dict:
    body = {**PERSONA, **overrides}
    response = client.post("/api/v1/persona/", json=body, headers={"If-Match": _config_etag(client)})
    assert response.status_code == 201, response.text
    return response.json()


def _update_persona(client: TestClient, persona_id: str, changes: dict) -> None:
    response = client.patch(
        f"/api/v1/persona/{persona_id}", json=changes, headers={"If-Match": _persona_etag(client)}
    )
    assert response.status_code == 200, response.text


def _wait_terminal(client: TestClient, task_id: str, seconds: float = 8.0) -> dict:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in {"completed", "failed", "cancelled", "timed_out"}:
            return task
        time.sleep(0.05)
    raise AssertionError(f"task {task_id} did not reach a terminal state")


def _fake(client: TestClient):
    return next(a for a in client.app.state.providers.adapters() if a.provider_id == "fake")


def test_unknown_persona_rejected(task_client: TestClient) -> None:
    response = task_client.post("/api/v1/task/", json={"prompt": "hi", "persona_id": "nobody"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_persona"


def test_disabled_persona_rejected_on_use(task_client: TestClient) -> None:
    _make_persona(task_client, persona_id="off", enabled=False)
    response = task_client.post("/api/v1/task/", json={"prompt": "hi", "persona_id": "off"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "persona_disabled"
    # The record itself stays readable — bindings remain inspectable.
    assert task_client.get("/api/v1/persona/off").status_code == 200


def test_persona_model_and_instructions_applied(task_client: TestClient) -> None:
    _make_persona(task_client)
    task_client.app.state.settings.store.set_many(
        {
            "persona.amy.model_id": "fake:script",
            "persona.amy.instructions": "Always answer like a drill sergeant.",
            "persona.amy.generation": {"max_steps": 3, "timeout_seconds": 90},
        }
    )
    from opal_think.providers import Generation

    _fake(task_client).set_script([Generation("YES SIR")])
    task = _wait_terminal(
        task_client, task_client.post("/api/v1/task/", json={"prompt": "report", "persona_id": "amy"}).json()["id"]
    )
    assert task["status"] == "completed" and task["answer"] == "YES SIR"
    assert task["model_id"] == "fake:script"  # binding drove resolution (no body model_id)
    assert task["max_steps"] == 3 and task["timeout_seconds"] == 90  # generation knobs
    persona = task["persona"]
    assert persona["persona_id"] == "amy" and persona["revision"] >= 1
    assert persona["snapshot"]["voice_id"] == "en-gb-alan-low"
    assert persona["snapshot"]["model_id"] == "fake:script"
    assert persona["snapshot"]["instructions"] == "Always answer like a drill sergeant."
    # The instructions actually reached the model as a system message.
    sent = _fake(task_client).calls[0]["messages"]
    system = [m["content"] for m in sent if m["role"] == "system"]
    assert any("drill sergeant" in content for content in system)


def test_explicit_task_fields_win_over_binding(task_client: TestClient) -> None:
    """Per-field precedence: request field > persona binding > service default."""
    _make_persona(task_client)
    task_client.app.state.settings.store.set_many(
        {"persona.amy.model_id": "fake:script", "persona.amy.generation": {"max_steps": 2}}
    )
    task_id = task_client.post(
        "/api/v1/task/", json={"prompt": "hi", "persona_id": "amy", "model_id": "fake:echo", "max_steps": 7}
    ).json()["id"]
    task = _wait_terminal(task_client, task_id)
    assert task["model_id"] == "fake:echo"  # explicit model_id beats the binding
    assert task["max_steps"] == 7  # explicit max_steps beats generation.max_steps
    assert task["answer"] == "echo: hi"


def test_session_default_persona_and_task_override(task_client: TestClient) -> None:
    _make_persona(task_client)
    _make_persona(task_client, persona_id="bob", name="Bob", voice_id=None)
    session = task_client.post("/api/v1/session/", json={"title": "t", "persona_id": "amy"}).json()
    assert session["persona_id"] == "amy"
    # Task inherits the session's default persona.
    inherited = _wait_terminal(
        task_client,
        task_client.post("/api/v1/task/", json={"prompt": "a", "session_id": session["id"]}).json()["id"],
    )
    assert inherited["persona"]["persona_id"] == "amy"
    # An explicit task persona overrides the session default.
    explicit = _wait_terminal(
        task_client,
        task_client.post(
            "/api/v1/task/", json={"prompt": "b", "session_id": session["id"], "persona_id": "bob"}
        ).json()["id"],
    )
    assert explicit["persona"]["persona_id"] == "bob"


def test_session_create_unknown_persona_422(task_client: TestClient) -> None:
    response = task_client.post("/api/v1/session/", json={"persona_id": "nobody"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_persona"


def test_historical_reproducibility_after_persona_edit(task_client: TestClient) -> None:
    """The task record keeps the snapshot+revision it ran under; later edits
    to the persona or its bindings do not rewrite history."""
    _make_persona(task_client)
    task_client.app.state.settings.store.set_many({"persona.amy.model_id": "fake:echo"})
    first = _wait_terminal(
        task_client, task_client.post("/api/v1/task/", json={"prompt": "a", "persona_id": "amy"}).json()["id"]
    )
    rev_before = first["persona"]["revision"]
    # Edit the persona (disable it) and change the binding afterwards.
    _update_persona(task_client, "amy", {"enabled": False})
    task_client.app.state.settings.store.set_many({"persona.amy.model_id": "fake:script"})
    again = task_client.get(f"/api/v1/task/{first['id']}").json()
    assert again["persona"]["revision"] == rev_before  # revision pinned at admission
    assert again["persona"]["snapshot"]["model_id"] == "fake:echo"  # binding it ran under
    # And a new selection now fails — the persona is disabled.
    assert (
        task_client.post("/api/v1/task/", json={"prompt": "b", "persona_id": "amy"}).status_code == 422
    )


def test_tasks_without_persona_have_null(task_client: TestClient) -> None:
    task = _wait_terminal(task_client, task_client.post("/api/v1/task/", json={"prompt": "x"}).json()["id"])
    assert task["persona"] is None


def test_persona_setting_validation(task_client: TestClient) -> None:
    settings = task_client.app.state.settings
    assert settings.validate_value("persona.amy.model_id", "fake:echo") is None
    assert settings.validate_value("persona.amy.model_id", 7) is not None
    assert settings.validate_value("persona.amy.generation", {"max_steps": 3}) is None
    assert settings.validate_value("persona.amy.generation", {"temperature": 0.5}) is not None
    assert settings.validate_value("persona.amy.generation", {"max_steps": 0}) is not None
    assert settings.validate_value("persona.unknown.field", "x") is not None
