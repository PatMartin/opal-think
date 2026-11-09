"""Contract tests: /api/v1/persona CRUD + validate, shared config ETag/If-Match semantics.

Personas share the portable-configuration revision with settings ("s<n>"): both ETag
sources are interchangeable, and every persona mutation bumps the same counter.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

PERSONA = {"persona_id": "amy", "name": "Amy", "description": "ops assistant", "voice_id": "en-gb-alan-low"}


def _assert_error_shape(body: dict) -> None:
    error = body["error"]
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["request_id"], str) and error["request_id"]
    assert isinstance(error["transaction_id"], str) and error["transaction_id"]


def _config_etag(client: TestClient) -> str:
    return client.get("/api/v1/persona/").headers["ETag"]


def _create(client: TestClient, **overrides) -> dict:
    body = {**PERSONA, **overrides}
    return client.post("/api/v1/persona/", json=body, headers={"If-Match": _config_etag(client)})


def test_list_personas_empty(client: TestClient) -> None:
    response = client.get("/api/v1/persona/")
    assert response.status_code == 200
    assert response.headers["ETag"].startswith('"s')
    assert response.json() == []


def test_create_persona(client: TestClient) -> None:
    response = _create(client)
    assert response.status_code == 201
    assert response.headers["Location"].endswith("/api/v1/persona/amy")
    body = response.json()
    assert body["persona_id"] == "amy" and body["name"] == "Amy"
    assert body["description"] == "ops assistant" and body["voice_id"] == "en-gb-alan-low"
    assert body["enabled"] is True
    assert body["created_at"] and body["updated_at"]
    # the mutation bumped the shared configuration revision
    assert client.get("/api/v1/setting/").headers["ETag"] != '"s0"'


def test_create_persona_requires_if_match(client: TestClient) -> None:
    assert client.post("/api/v1/persona/", json=PERSONA).status_code == 428
    assert client.post("/api/v1/persona/", json=PERSONA, headers={"If-Match": "s3"}).status_code == 422
    assert client.post("/api/v1/persona/", json=PERSONA, headers={"If-Match": '"s999"'}).status_code == 412


def test_create_persona_duplicate_conflict(client: TestClient) -> None:
    assert _create(client).status_code == 201
    response = _create(client, name="Amy Two")
    assert response.status_code == 409
    _assert_error_shape(response.json())


def test_create_persona_validation(client: TestClient) -> None:
    # persona_id pattern: ^[A-Za-z0-9][A-Za-z0-9._-]*$
    assert _create(client, persona_id="-bad").status_code == 422
    assert _create(client, persona_id="").status_code == 422
    assert _create(client, name="").status_code == 422
    assert _create(client, unknown_field="x").status_code == 422


def test_get_persona(client: TestClient) -> None:
    assert _create(client).status_code == 201
    response = client.get("/api/v1/persona/amy")
    assert response.status_code == 200
    assert response.headers["ETag"].startswith('"s')
    assert response.json()["persona_id"] == "amy"


def test_get_persona_not_found(client: TestClient) -> None:
    response = client.get("/api/v1/persona/ghost")
    assert response.status_code == 404
    _assert_error_shape(response.json())


def test_get_persona_bad_id(client: TestClient) -> None:
    assert client.get("/api/v1/persona/bad id").status_code == 422


def test_list_personas_enabled_filter(client: TestClient) -> None:
    assert _create(client).status_code == 201
    assert _create(client, persona_id="bob", name="Bob", enabled=False).status_code == 201
    enabled = client.get("/api/v1/persona/", params={"enabled": "true"}).json()
    assert [p["persona_id"] for p in enabled] == ["amy"]
    disabled = client.get("/api/v1/persona/", params={"enabled": "false"}).json()
    assert [p["persona_id"] for p in disabled] == ["bob"]


def test_patch_persona(client: TestClient) -> None:
    assert _create(client).status_code == 201
    response = client.patch(
        "/api/v1/persona/amy",
        json={"name": "Amy Prime", "voice_id": None, "enabled": False},
        headers={"If-Match": _config_etag(client)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Amy Prime"
    assert body["voice_id"] is None  # explicit null clears the binding
    assert body["enabled"] is False
    assert body["description"] == "ops assistant"  # untouched fields preserved


def test_patch_persona_requires_body_and_if_match(client: TestClient) -> None:
    assert _create(client).status_code == 201
    assert client.patch("/api/v1/persona/amy", json={"enabled": False}).status_code == 428
    # minProperties: 1 — empty patch is a validation error
    assert (
        client.patch("/api/v1/persona/amy", json={}, headers={"If-Match": _config_etag(client)}).status_code == 422
    )
    # name may not be nulled out
    assert (
        client.patch("/api/v1/persona/amy", json={"name": None}, headers={"If-Match": _config_etag(client)}).status_code
        == 422
    )


def test_patch_persona_not_found_and_stale(client: TestClient) -> None:
    etag = _config_etag(client)
    assert client.patch("/api/v1/persona/ghost", json={"enabled": False}, headers={"If-Match": etag}).status_code == 404
    assert _create(client).status_code == 201  # bumps the shared revision
    assert (
        client.patch("/api/v1/persona/amy", json={"enabled": False}, headers={"If-Match": etag}).status_code == 412
    )


def test_delete_persona(client: TestClient) -> None:
    assert _create(client).status_code == 201
    assert client.delete("/api/v1/persona/amy").status_code == 428
    response = client.delete("/api/v1/persona/amy", headers={"If-Match": _config_etag(client)})
    assert response.status_code == 204
    assert client.get("/api/v1/persona/amy").status_code == 404


def test_delete_persona_not_found(client: TestClient) -> None:
    etag = _config_etag(client)
    assert client.delete("/api/v1/persona/ghost", headers={"If-Match": etag}).status_code == 404


def test_validate_persona(client: TestClient) -> None:
    assert _create(client).status_code == 201
    response = client.post("/api/v1/persona/amy/validate")
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is True
    assert body["errors"] == []
    # voice binding is resolved by OPAL Say, not verified locally
    assert any("en-gb-alan-low" in w for w in body["warnings"])


def test_validate_persona_disabled(client: TestClient) -> None:
    assert _create(client, persona_id="bob", name="Bob", enabled=False).status_code == 201
    body = client.post("/api/v1/persona/bob/validate").json()
    assert body["valid"] is False
    assert any("disabled" in e for e in body["errors"])


def test_validate_persona_not_found(client: TestClient) -> None:
    assert client.post("/api/v1/persona/ghost/validate").status_code == 404


def test_settings_mutation_invalidates_persona_etag(client: TestClient) -> None:
    """Shared revision: a settings write stales a persona If-Match (and vice versa)."""
    assert _create(client).status_code == 201
    persona_etag = client.get("/api/v1/persona/amy").headers["ETag"]
    key_etag = client.get("/api/v1/setting/task.max_concurrent").headers["ETag"]
    assert client.put(
        "/api/v1/setting/task.max_concurrent", json={"value": 8}, headers={"If-Match": key_etag}
    ).status_code == 200
    # the persona's view of the configuration moved on
    assert client.patch(
        "/api/v1/persona/amy", json={"enabled": False}, headers={"If-Match": persona_etag}
    ).status_code == 412
