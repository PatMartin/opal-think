"""Contract tests: /api/v1/profile CRUD + activate, shared config ETag/If-Match semantics.

Profiles share the portable-configuration revision with settings and personas ("s<n>").
A profile's ``settings`` compartment is think's preset — applied as a write-only patch
into live settings on activation; foreign service compartments are opaque passthrough.
Exactly one profile is active at a time; the active profile cannot be deleted.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

PROFILE = {
    "name": "studio",
    "description": "high quality defaults",
    "settings": {"models.default_id": "fake:echo", "task.default_max_steps": 4},
}


def _assert_error_shape(body: dict) -> None:
    error = body["error"]
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["request_id"], str) and error["request_id"]
    assert isinstance(error["transaction_id"], str) and error["transaction_id"]


def _config_etag(client: TestClient) -> str:
    return client.get("/api/v1/profile/").headers["ETag"]


def _create(client: TestClient, **overrides) -> dict:
    body = {**PROFILE, **overrides}
    return client.post("/api/v1/profile/", json=body, headers={"If-Match": _config_etag(client)})


def _setting(client: TestClient, key: str):
    return client.get(f"/api/v1/setting/{key}").json()["value"]


def test_list_profiles_empty(client: TestClient) -> None:
    response = client.get("/api/v1/profile/")
    assert response.status_code == 200
    assert response.headers["ETag"].startswith('"s')
    assert response.json() == []


def test_create_profile(client: TestClient) -> None:
    response = _create(client)
    assert response.status_code == 201
    body = response.json()
    assert body["id"] == 1  # ids are auto-allocated integers
    assert response.headers["Location"].endswith(f"/api/v1/profile/{body['id']}")
    assert body["name"] == "studio" and body["is_active"] is False
    assert body["settings"]["models.default_id"] == "fake:echo"
    assert body["created_at"] and body["updated_at"]
    # the mutation bumped the shared configuration revision
    assert client.get("/api/v1/setting/").headers["ETag"] != '"s0"'


def test_create_profile_preserves_foreign_compartments(client: TestClient) -> None:
    response = _create(client, say={"voice_id": "en-gb-alan-low"}, look={"zones": ["front"]})
    assert response.status_code == 201
    body = response.json()
    assert body["say"] == {"voice_id": "en-gb-alan-low"}
    assert body["look"] == {"zones": ["front"]}


def test_create_profile_requires_if_match(client: TestClient) -> None:
    assert client.post("/api/v1/profile/", json=PROFILE).status_code == 428
    assert client.post("/api/v1/profile/", json=PROFILE, headers={"If-Match": "s3"}).status_code == 422
    assert client.post("/api/v1/profile/", json=PROFILE, headers={"If-Match": '"s999"'}).status_code == 412


def test_create_profile_name_and_id_conflicts(client: TestClient) -> None:
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409  # duplicate name
    assert _create(client, name="other", id=1).status_code == 409  # duplicate id
    assert _create(client, name="other", id=7).status_code == 201  # client-supplied id honored


def test_create_profile_validation(client: TestClient) -> None:
    assert _create(client, name="").status_code == 422
    assert _create(client, id=0).status_code == 422
    assert _create(client, name=None).status_code == 422


def test_create_profile_invalid_settings_compartment(client: TestClient) -> None:
    assert _create(client, settings={"unknown.key": 1}).status_code == 422
    assert _create(client, settings={"task.max_concurrent": "lots"}).status_code == 422
    assert _create(client, settings={"pending_restart_fields": []}).status_code == 422
    assert _create(client).status_code == 201


def test_get_profile(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    response = client.get(f"/api/v1/profile/{profile_id}")
    assert response.status_code == 200
    assert response.headers["ETag"].startswith('"s')
    assert response.json()["name"] == "studio"


def test_get_profile_not_found_and_bad_id(client: TestClient) -> None:
    assert client.get("/api/v1/profile/9").status_code == 404
    assert client.get("/api/v1/profile/abc").status_code == 422
    assert client.get("/api/v1/profile/0").status_code == 422


def test_patch_profile(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    response = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"name": "studio-2", "say": {"voice_id": "x"}, "settings": {"task.retention_days": 7}},
        headers={"If-Match": _config_etag(client)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "studio-2"
    assert body["settings"] == {"task.retention_days": 7}  # compartment replaced wholesale
    assert body["say"] == {"voice_id": "x"}
    assert body["description"] == "high quality defaults"  # untouched preserved
    # top-level merge: a later patch touching only the name leaves compartments alone
    response = client.patch(
        f"/api/v1/profile/{profile_id}", json={"name": "studio-3"}, headers={"If-Match": _config_etag(client)}
    )
    assert response.json()["say"] == {"voice_id": "x"}
    assert response.json()["settings"] == {"task.retention_days": 7}


def test_patch_profile_preconditions_and_conflicts(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    other_id = _create(client, name="other").json()["id"]
    assert client.patch(f"/api/v1/profile/{profile_id}", json={"name": "x"}).status_code == 428
    assert (
        client.patch(
            f"/api/v1/profile/{profile_id}", json={"name": "other"}, headers={"If-Match": _config_etag(client)}
        ).status_code
        == 409  # name collision with `other`
    )
    assert (
        client.patch(
            f"/api/v1/profile/{profile_id}", json={"id": other_id}, headers={"If-Match": _config_etag(client)}
        ).status_code
        == 409  # id is immutable
    )
    assert (
        client.patch("/api/v1/profile/9", json={"name": "x"}, headers={"If-Match": _config_etag(client)}).status_code
        == 404
    )
    # name may not be nulled out
    assert (
        client.patch(f"/api/v1/profile/{profile_id}", json={"name": None}, headers={"If-Match": _config_etag(client)})
        .status_code
        == 422
    )


def test_activate_profile_applies_preset(client: TestClient) -> None:
    first = _create(client).json()["id"]
    second = _create(client, name="lean", settings={"task.default_max_steps": 2}).json()["id"]
    etag = _config_etag(client)
    response = client.post(f"/api/v1/profile/{first}/activate", headers={"If-Match": etag})
    assert response.status_code == 200
    body = response.json()
    assert body["is_active"] is True
    # preset landed in live settings as ordinary overrides in the same write
    assert _setting(client, "models.default_id") == "fake:echo"
    assert _setting(client, "task.default_max_steps") == 4
    # activating the other profile is exclusive; its partial preset does not revert leftovers
    assert client.post(f"/api/v1/profile/{second}/activate", headers={"If-Match": _config_etag(client)}).status_code == 200
    profiles = {p["id"]: p["is_active"] for p in client.get("/api/v1/profile/").json()}
    assert profiles == {first: False, second: True}
    assert _setting(client, "task.default_max_steps") == 2
    assert _setting(client, "models.default_id") == "fake:echo"  # write-only preset: A's residue stays


def test_activate_profile_requires_if_match(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    assert client.post(f"/api/v1/profile/{profile_id}/activate").status_code == 428
    assert client.post(f"/api/v1/profile/{profile_id}/activate", headers={"If-Match": '"s999"'}).status_code == 412
    assert (
        client.post("/api/v1/profile/9/activate", headers={"If-Match": _config_etag(client)}).status_code == 404
    )


def test_create_active_profile_applies_preset(client: TestClient) -> None:
    response = _create(client, is_active=True)
    assert response.status_code == 201
    assert response.json()["is_active"] is True
    assert _setting(client, "task.default_max_steps") == 4


def test_patch_is_active_activates(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    response = client.patch(
        f"/api/v1/profile/{profile_id}", json={"is_active": True}, headers={"If-Match": _config_etag(client)}
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is True
    assert _setting(client, "models.default_id") == "fake:echo"


def test_delete_profile(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    assert client.delete(f"/api/v1/profile/{profile_id}").status_code == 428
    assert client.delete(f"/api/v1/profile/{profile_id}", headers={"If-Match": _config_etag(client)}).status_code == 204
    assert client.get(f"/api/v1/profile/{profile_id}").status_code == 404
    etag = _config_etag(client)
    assert client.delete("/api/v1/profile/9", headers={"If-Match": etag}).status_code == 404


def test_delete_active_profile_conflict(client: TestClient) -> None:
    profile_id = _create(client, is_active=True).json()["id"]
    response = client.delete(f"/api/v1/profile/{profile_id}", headers={"If-Match": _config_etag(client)})
    assert response.status_code == 409
    _assert_error_shape(response.json())


def test_settings_mutation_invalidates_profile_etag(client: TestClient) -> None:
    """Shared revision: a settings write stales a profile If-Match (and vice versa)."""
    profile_id = _create(client).json()["id"]
    profile_etag = client.get(f"/api/v1/profile/{profile_id}").headers["ETag"]
    key_etag = client.get("/api/v1/setting/task.max_concurrent").headers["ETag"]
    assert client.put(
        "/api/v1/setting/task.max_concurrent", json={"value": 8}, headers={"If-Match": key_etag}
    ).status_code == 200
    assert (
        client.patch(f"/api/v1/profile/{profile_id}", json={"description": "stale"}, headers={"If-Match": profile_etag})
        .status_code
        == 412
    )
