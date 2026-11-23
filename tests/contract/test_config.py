"""Contract tests: /api/v1/config save/load/validate (kernel config_* path items, v0.5.0).

Snapshots are the kernel ``OpalConfigSnapshot`` envelope — ``format_version``,
``module``, ``created_at``, flat ``settings`` overrides and ``resources`` sections —
never secrets or history. ``save`` captures (managed store or response body) without
mutating live config; ``load`` atomically replaces the whole portable configuration
under strong If-Match; ``validate`` is side-effect free.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

PERSONA = {"persona_id": "amy", "name": "Amy", "description": "ops assistant", "voice_id": None, "enabled": True}
PROFILE = {"name": "studio", "modules": {"opal-say": {"voice_id": "en-gb-alan-low"}}}


def _etag(client: TestClient) -> str:
    return client.get("/api/v1/setting/").headers["ETag"]


def _seed(client: TestClient) -> None:
    """One override + one persona + one profile in the portable config."""
    key_etag = client.get("/api/v1/setting/task.retention_days").headers["ETag"]
    client.put("/api/v1/setting/task.retention_days", json={"value": 7}, headers={"If-Match": key_etag})
    client.post("/api/v1/persona/", json=PERSONA, headers={"If-Match": _etag(client)})
    client.post("/api/v1/profile/", json=PROFILE, headers={"If-Match": _etag(client)})


def _save(client: TestClient, destination: str = "response") -> dict:
    response = client.post("/api/v1/config/save", json={"destination": destination})
    assert response.status_code == 200
    return response


# -- save -----------------------------------------------------------------------


def test_save_snapshot_shape(client: TestClient) -> None:
    _seed(client)
    response = _save(client)
    assert response.headers["ETag"].startswith('"s')
    body = response.json()
    assert body["managed"] is False
    snap = body["snapshot"]
    assert snap["format_version"] == "1.0.0" and snap["module"] == "opal-think" and snap["created_at"]
    # overrides only: an untouched setting is absent, the seeded one is present
    assert snap["settings"] == {"task.retention_days": 7}
    assert "pending_restart_fields" not in snap["settings"]
    assert [p["persona_id"] for p in snap["resources"]["personas"]] == ["amy"]
    assert [p["name"] for p in snap["resources"]["profiles"]] == ["studio"]
    assert snap["resources"]["profiles"][0]["modules"] == {"opal-say": {"voice_id": "en-gb-alan-low"}}


def test_save_is_not_a_mutation(client: TestClient) -> None:
    before = client.get("/api/v1/setting/").headers["ETag"]
    client.post("/api/v1/config/save", json={"destination": "managed"})
    assert client.get("/api/v1/setting/").headers["ETag"] == before


def test_save_managed_then_load_default(client: TestClient) -> None:
    _seed(client)
    saved = client.post("/api/v1/config/save", json={"destination": "managed"})
    assert saved.status_code == 200 and saved.json()["managed"] is True
    # mutate live config, then load the managed snapshot by omitting the body
    client.delete("/api/v1/persona/amy", headers={"If-Match": _etag(client)})
    loaded = client.post("/api/v1/config/load", headers={"If-Match": _etag(client)})
    assert loaded.status_code == 200 and loaded.json()["applied"] is True
    assert loaded.headers["ETag"].startswith('"s')
    assert client.get("/api/v1/persona/amy").status_code == 200
    assert client.get("/api/v1/setting/task.retention_days").json()["value"] == 7


def test_load_default_no_snapshot_404(client: TestClient) -> None:
    response = client.post("/api/v1/config/load", headers={"If-Match": _etag(client)})
    assert response.status_code == 404


# -- validate ---------------------------------------------------------------------


def test_validate_live_config(client: TestClient) -> None:
    _seed(client)
    body = client.post("/api/v1/config/validate", json={}).json()
    assert body["valid"] is True and body["errors"] == []


def test_validate_bad_snapshot(client: TestClient) -> None:
    snap = {
        "format_version": "1.0.0",
        "module": "opal-think",
        "created_at": "2026-01-01T00:00:00Z",
        "settings": {"unknown.key": 1, "task.max_concurrent": "lots", "pending_restart_fields": []},
        "resources": {
            "personas": [{"persona_id": "amy"}, {"persona_id": "amy", "name": "dupe"}],
            "profiles": [
                {"id": "1", "name": "a", "is_active": True},
                {"id": "1", "name": "b", "is_active": True, "shared": {"nope": 1}},
            ],
        },
    }
    body = client.post("/api/v1/config/validate", json={"snapshot": snap}).json()
    assert body["valid"] is False
    joined = "; ".join(e["message"] + e["path"] for e in body["errors"])
    for fragment in ("unknown.key", "task.max_concurrent", "pending_restart_fields", "duplicate persona id: amy",
                     "duplicate profile id: 1", "active profile", "closed empty object"):
        assert fragment in joined, fragment


def test_validate_wrong_module_and_version(client: TestClient) -> None:
    snap = {"format_version": "9.9", "module": "opal-other", "created_at": "t", "settings": {}, "resources": {}}
    body = client.post("/api/v1/config/validate", json={"snapshot": snap}).json()
    codes = {e["code"] for e in body["errors"]}
    assert {"wrong_module", "unsupported_format"} <= codes
    assert body["valid"] is False


def test_validate_unknown_section_and_unused(client: TestClient) -> None:
    snap = {
        "format_version": "1.0.0",
        "module": "opal-think",
        "created_at": "t",
        "settings": {},
        "resources": {"wakewords": [], "models": [{"id": "m1"}]},
    }
    body = client.post("/api/v1/config/validate", json={"snapshot": snap}).json()
    assert any(e["code"] == "unknown_section" for e in body["errors"])
    assert any(w["code"] == "unsupported_section" for w in body["warnings"])


def test_validate_explicit_null_snapshot(client: TestClient) -> None:
    assert client.post("/api/v1/config/validate", json={"snapshot": None}).status_code == 422


# -- load ---------------------------------------------------------------------


def test_load_requires_if_match(client: TestClient) -> None:
    snap = {"format_version": "1.0.0", "module": "opal-think", "created_at": "t", "settings": {}, "resources": {}}
    assert client.post("/api/v1/config/load", json={"snapshot": snap}).status_code == 428
    assert (
        client.post("/api/v1/config/load", json={"snapshot": snap}, headers={"If-Match": '"s999"'}).status_code == 412
    )


def test_load_replaces_portable_config(client: TestClient) -> None:
    _seed(client)
    snap = {
        "format_version": "1.0.0",
        "module": "opal-think",
        "created_at": "2026-01-01T00:00:00Z",
        "settings": {"task.default_timeout_seconds": 300},
        "resources": {
            "personas": [{"persona_id": "bob", "name": "Bob", "enabled": False}],
            "profiles": [{"id": "5", "name": "lean", "is_active": True, "modules": {"opal-say": {"voice_id": "en-gb"}}}],
        },
    }
    response = client.post("/api/v1/config/load", json={"snapshot": snap}, headers={"If-Match": _etag(client)})
    assert response.status_code == 200
    body = response.json()
    assert body["applied"] is True
    # wholesale replace: seeded override/persona/profile are gone, snapshot contents live
    assert client.get("/api/v1/setting/task.retention_days").json()["is_default"] is True
    assert client.get("/api/v1/setting/task.default_timeout_seconds").json()["value"] == 300
    assert client.get("/api/v1/persona/amy").status_code == 404
    assert client.get("/api/v1/persona/bob").json()["enabled"] is False
    profiles = client.get("/api/v1/profile/").json()
    assert [p["name"] for p in profiles] == ["lean"] and profiles[0]["is_active"] is True
    assert profiles[0]["id"] == "5" and profiles[0]["modules"]["opal-say"] == {"voice_id": "en-gb"}
    # activation is selection-only now: no settings side effect to check
    assert client.get("/api/v1/setting/task.max_concurrent").json()["value"] == 4


def test_load_round_trip(client: TestClient) -> None:
    """A live save -> mutate -> load cycle restores the captured configuration."""
    _seed(client)
    snap = _save(client).json()["snapshot"]
    client.delete("/api/v1/persona/amy", headers={"If-Match": _etag(client)})
    loaded = client.post("/api/v1/config/load", json={"snapshot": snap}, headers={"If-Match": _etag(client)})
    assert loaded.status_code == 200
    assert _save(client).json()["snapshot"]["resources"]["personas"] == snap["resources"]["personas"]


def test_load_invalid_snapshot_conflict(client: TestClient) -> None:
    snap = {"format_version": "1.0.0", "module": "opal-think", "created_at": "t",
            "settings": {"unknown.key": 1}, "resources": {}}
    response = client.post("/api/v1/config/load", json={"snapshot": snap}, headers={"If-Match": _etag(client)})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_snapshot"


def test_load_explicit_null_snapshot(client: TestClient) -> None:
    response = client.post("/api/v1/config/load", json={"snapshot": None}, headers={"If-Match": _etag(client)})
    assert response.status_code == 422


def test_retired_backup_routes_gone(client: TestClient) -> None:
    """v0.5.0: the backup route family is retired in favor of config save/load/validate."""
    for method, path in (
        ("get", "/api/v1/backup/list"),
        ("post", "/api/v1/backup/export"),
        ("post", "/api/v1/backup/import"),
        ("get", "/api/v1/backup/download/x.tar.gz"),
    ):
        assert getattr(client, method)(path).status_code == 404, path
