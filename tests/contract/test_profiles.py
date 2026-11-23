"""Contract tests: /api/v1/profile CRUD + activate, v0.5.0 kernel v3 namespaced profiles.

Profiles are portable namespaced documents (opaque string ``id``, required ``shared``
and ``modules`` maps). The opal-think compartment is a closed empty schema, so
activation is a local selection change only. Foreign module compartments are stored
and returned verbatim on create/read; patches may only target ``opal-think``.
Profiles share the portable-configuration revision with settings and personas
("s<n>"). At most one profile is active; the active profile cannot be deleted.
"""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

PROFILE = {
    "name": "studio",
    "description": "high quality defaults",
    "modules": {"opal-say": {"voice_id": "en-gb-alan-low"}},
}


def _assert_error_shape(body: dict) -> None:
    error = body["error"]
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["request_id"], str) and error["request_id"]
    assert isinstance(error["transaction_id"], str) and error["transaction_id"]


def _config_etag(client: TestClient) -> str:
    return client.get("/api/v1/profile/").headers["ETag"]


def _create(client: TestClient, **overrides) -> object:
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
    assert body["id"] == "1"  # opaque string ids; auto-allocation continues the sequence
    assert response.headers["Location"].endswith("/api/v1/profile/1")
    assert body["name"] == "studio" and body["is_active"] is False
    assert body["shared"] == {} and body["modules"] == {"opal-say": {"voice_id": "en-gb-alan-low"}}
    assert body["created_at"] and body["updated_at"]
    # the mutation bumped the shared configuration revision
    assert client.get("/api/v1/setting/").headers["ETag"] != '"s0"'


def test_create_profile_client_id_preserved(client: TestClient) -> None:
    response = _create(client, id="workstation-42")
    assert response.status_code == 201
    assert response.json()["id"] == "workstation-42"
    assert client.get("/api/v1/profile/workstation-42").status_code == 200


def test_create_profile_requires_if_match(client: TestClient) -> None:
    assert client.post("/api/v1/profile/", json=PROFILE).status_code == 428
    assert client.post("/api/v1/profile/", json=PROFILE, headers={"If-Match": "s3"}).status_code == 422
    assert client.post("/api/v1/profile/", json=PROFILE, headers={"If-Match": '"s999"'}).status_code == 412


def test_create_profile_name_and_id_conflicts(client: TestClient) -> None:
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409  # duplicate name
    assert _create(client, name="other", id="1").status_code == 409  # duplicate id
    assert _create(client, name="other", id="p-7").status_code == 201


def test_create_profile_validation(client: TestClient) -> None:
    assert _create(client, name="").status_code == 422
    assert _create(client, name=None).status_code == 422
    assert _create(client, id="").status_code == 422


def test_create_profile_envelope_validation(client: TestClient) -> None:
    # 'shared' is a closed empty object
    assert _create(client, shared={"a": 1}).status_code == 422
    # compartments must be objects
    assert _create(client, modules={"opal-say": "nope"}).status_code == 422
    # own compartment is a closed empty schema
    assert _create(client, modules={"opal-think": {"x": 1}}).status_code == 422
    # foreign compartments accepted verbatim
    assert _create(client).status_code == 201


def test_get_profile(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    response = client.get(f"/api/v1/profile/{profile_id}")
    assert response.status_code == 200
    assert response.headers["ETag"].startswith('"s')
    assert response.json()["name"] == "studio"


def test_get_profile_not_found(client: TestClient) -> None:
    assert client.get("/api/v1/profile/9").status_code == 404
    assert client.get("/api/v1/profile/anything-opaque").status_code == 404


def test_patch_profile_merges_own_compartment(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    response = client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"name": "studio-2", "modules": {"opal-think": {}}},
        headers={"If-Match": _config_etag(client)},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "studio-2"
    assert body["description"] == "high quality defaults"  # untouched preserved
    assert body["modules"] == {"opal-say": {"voice_id": "en-gb-alan-low"}, "opal-think": {}}
    # a later patch touching only the name leaves compartments alone
    response = client.patch(
        f"/api/v1/profile/{profile_id}", json={"name": "studio-3"}, headers={"If-Match": _config_etag(client)}
    )
    assert response.json()["modules"]["opal-say"] == {"voice_id": "en-gb-alan-low"}


def test_patch_profile_rejects_foreign_compartment(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    assert client.patch(
        f"/api/v1/profile/{profile_id}",
        json={"modules": {"opal-say": {"voice_id": "other"}}},
        headers={"If-Match": _config_etag(client)},
    ).status_code == 422  # foreign compartments are read-only on patch
    assert client.get(f"/api/v1/profile/{profile_id}").json()["modules"]["opal-say"] == {
        "voice_id": "en-gb-alan-low"
    }  # stored contents preserved


def test_patch_profile_preconditions_and_conflicts(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    other_id = _create(client, name="other").json()["id"]
    assert client.patch(f"/api/v1/profile/{profile_id}", json={"name": "x"}).status_code == 428
    assert client.patch(
        f"/api/v1/profile/{profile_id}", json={"name": "other"}, headers={"If-Match": _config_etag(client)}
    ).status_code == 409  # name collision with `other`
    assert client.patch(
        f"/api/v1/profile/{profile_id}", json={"id": other_id}, headers={"If-Match": _config_etag(client)}
    ).status_code == 422  # 'id' is not a patchable field
    assert client.patch(
        "/api/v1/profile/9", json={"name": "x"}, headers={"If-Match": _config_etag(client)}
    ).status_code == 404
    # name may not be nulled out
    assert client.patch(
        f"/api/v1/profile/{profile_id}", json={"name": None}, headers={"If-Match": _config_etag(client)}
    ).status_code == 422


def test_activate_profile_is_selection_only(client: TestClient) -> None:
    first = _create(client).json()["id"]
    second = _create(client, name="lean").json()["id"]
    before = client.get("/api/v1/setting/").json()
    response = client.post(f"/api/v1/profile/{first}/activate", headers={"If-Match": _config_etag(client)})
    assert response.status_code == 200
    assert response.json()["is_active"] is True
    # empty opal-think compartment: activation changes no live settings
    assert client.get("/api/v1/setting/").json() == before
    assert client.post(
        f"/api/v1/profile/{second}/activate", headers={"If-Match": _config_etag(client)}
    ).status_code == 200
    profiles = {p["id"]: p["is_active"] for p in client.get("/api/v1/profile/").json()}
    assert profiles == {first: False, second: True}


def test_activate_profile_requires_if_match(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    assert client.post(f"/api/v1/profile/{profile_id}/activate").status_code == 428
    assert client.post(f"/api/v1/profile/{profile_id}/activate", headers={"If-Match": '"s999"'}).status_code == 412
    assert client.post("/api/v1/profile/9/activate", headers={"If-Match": _config_etag(client)}).status_code == 404


def test_create_active_profile_is_exclusive(client: TestClient) -> None:
    assert _create(client, is_active=True).status_code == 201
    assert client.get("/api/v1/profile/1").json()["is_active"] is True


def test_patch_is_active_activates(client: TestClient) -> None:
    profile_id = _create(client).json()["id"]
    response = client.patch(
        f"/api/v1/profile/{profile_id}", json={"is_active": True}, headers={"If-Match": _config_etag(client)}
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is True


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


def test_v2_storage_upgrades_on_reopen(client, tmp_path, monkeypatch) -> None:
    """A v1.7.1-era database (INTEGER profile ids, flat ``settings`` compartment,
    top-level foreign compartments) reopens under v3: ids stringify, ``settings``
    is dropped, foreign keys fold under ``modules``."""
    db_path = tmp_path / "v2.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE profiles (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,"
        " is_active INTEGER NOT NULL DEFAULT 0, record TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO profiles VALUES (?,?,?,?)",
        (
            7,
            "legacy",
            1,
            json.dumps(
                {
                    "description": "d",
                    "settings": {"task.default_max_steps": 4},
                    "say": {"voice_id": "en-gb"},
                    "created_at": "2025-01-01T00:00:00+00:00",
                    "updated_at": "2025-01-01T00:00:00+00:00",
                }
            ),
        ),
    )
    conn.commit()
    conn.close()

    # repoint the app's settings registry at the legacy database; construction upgrades it
    from opal_think.settings_registry import SettingsRegistry, SettingsStore

    client.app.state.settings = SettingsRegistry(SettingsStore(db_path))
    cols = {r[1]: r[2] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(profiles)").fetchall()}
    assert cols["id"].upper() == "TEXT"  # table rebuilt with string ids

    response = client.get("/api/v1/profile/")
    assert response.status_code == 200
    [profile] = response.json()
    assert profile["id"] == "7" and profile["name"] == "legacy" and profile["is_active"] is True
    assert "settings" not in profile
    assert profile["shared"] == {}
    assert profile["modules"] == {"say": {"voice_id": "en-gb"}}
    # reopen is idempotent: a second store construction on the now-v3 table is a no-op
    client.app.state.settings = SettingsRegistry(SettingsStore(db_path))
    assert client.get("/api/v1/profile/").json() == [profile]
