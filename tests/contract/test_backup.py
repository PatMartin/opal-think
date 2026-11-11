"""Contract tests: /api/v1/backup export/validate/import + managed archives.

The bundle is the kernel OpalConfigExport shape — settings *overrides* (so is_default
flags survive round-trips), personas, profiles; never secrets or history. Import and
restore replace the whole portable configuration atomically under strong If-Match;
an empty import body is a self round-trip of the persisted state.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

PERSONA = {"persona_id": "amy", "name": "Amy", "description": "ops assistant", "voice_id": None, "enabled": True}
PROFILE = {"name": "studio", "settings": {"task.default_max_steps": 4}}


def _etag(client: TestClient) -> str:
    return client.get("/api/v1/setting/").headers["ETag"]


def _seed(client: TestClient) -> None:
    """One override + one persona + one profile in the portable config."""
    key_etag = client.get("/api/v1/setting/task.retention_days").headers["ETag"]
    client.put("/api/v1/setting/task.retention_days", json={"value": 7}, headers={"If-Match": key_etag})
    client.post("/api/v1/persona/", json=PERSONA, headers={"If-Match": _etag(client)})
    client.post("/api/v1/profile/", json=PROFILE, headers={"If-Match": _etag(client)})


def _export(client: TestClient) -> dict:
    response = client.get("/api/v1/backup/export")
    assert response.status_code == 200
    return response.json()


# -- export ---------------------------------------------------------------------


def test_export_shape(client: TestClient) -> None:
    _seed(client)
    bundle = _export(client)
    assert bundle["format_version"] and bundle["exported_at"]
    # overrides only: an untouched setting is absent, the seeded one is present
    assert bundle["settings"] == {"task.retention_days": 7}
    assert "pending_restart_fields" not in bundle["settings"]
    assert [p["persona_id"] for p in bundle["personas"]] == ["amy"]
    assert [p["name"] for p in bundle["profiles"]] == ["studio"]
    assert bundle["profiles"][0]["settings"] == {"task.default_max_steps": 4}


# -- validate --------------------------------------------------------------------


def test_validate_valid_bundle(client: TestClient) -> None:
    _seed(client)
    response = client.post("/api/v1/backup/validate", json=_export(client))
    assert response.status_code == 200
    assert response.json()["valid"] is True
    assert response.json()["errors"] == []


def test_validate_bad_bundle(client: TestClient) -> None:
    bundle = {
        "format_version": "1.0.0",
        "settings": {"unknown.key": 1, "task.max_concurrent": "lots", "pending_restart_fields": []},
        "personas": [{"persona_id": "amy"}, {"persona_id": "amy", "name": "dupe"}],
        "profiles": [
            {"id": 1, "name": "a", "is_active": True},
            {"id": 1, "name": "b", "is_active": True, "settings": {"nope.key": 1}},
        ],
    }
    body = client.post("/api/v1/backup/validate", json=bundle).json()
    assert body["valid"] is False
    joined = "; ".join(body["errors"])
    for fragment in ("unknown.key", "task.max_concurrent", "pending_restart_fields", "duplicate persona id: amy",
                     "duplicate profile id: 1", "active profile", "nope.key"):
        assert fragment in joined, fragment


def test_validate_foreign_sections_warn(client: TestClient) -> None:
    bundle = {"format_version": "1.0.0", "settings": {}, "actions": [{"id": "x"}], "wakewords": []}
    body = client.post("/api/v1/backup/validate", json=bundle).json()
    assert body["valid"] is True
    assert any("actions" in w for w in body["warnings"])


def test_validate_requires_bundle(client: TestClient) -> None:
    assert client.post("/api/v1/backup/validate", json={}).status_code == 422
    assert client.post("/api/v1/backup/validate", json={"settings": {}}).status_code == 422  # no format_version


# -- import ---------------------------------------------------------------------


def test_import_requires_if_match(client: TestClient) -> None:
    assert client.post("/api/v1/backup/import", json={"format_version": "1.0.0", "settings": {}}).status_code == 428
    assert (
        client.post(
            "/api/v1/backup/import", json={"format_version": "1.0.0", "settings": {}}, headers={"If-Match": '"s999"'}
        ).status_code
        == 412
    )


def test_import_replaces_portable_config(client: TestClient) -> None:
    _seed(client)
    bundle = {
        "format_version": "1.0.0",
        "settings": {"task.default_timeout_seconds": 300},
        "personas": [{"persona_id": "bob", "name": "Bob", "enabled": False}],
        "profiles": [{"id": 5, "name": "lean", "is_active": True, "settings": {"task.max_concurrent": 2}}],
    }
    response = client.post("/api/v1/backup/import", json=bundle, headers={"If-Match": _etag(client)})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "imported" and body["success"] is True
    assert body["counts"] == {"settings": 1, "personas": 1, "profiles": 1}
    # wholesale replace: seeded override/persona/profile are gone, bundle contents live
    assert client.get("/api/v1/setting/task.retention_days").json()["is_default"] is True
    assert client.get("/api/v1/setting/task.default_timeout_seconds").json()["value"] == 300
    assert client.get("/api/v1/persona/amy").status_code == 404
    assert client.get("/api/v1/persona/bob").json()["enabled"] is False
    profiles = client.get("/api/v1/profile/").json()
    assert [p["name"] for p in profiles] == ["lean"] and profiles[0]["is_active"] is True
    # imported presets are stored, not auto-applied: max_concurrent stays default until activated
    assert client.get("/api/v1/setting/task.max_concurrent").json()["value"] == 4


def test_import_empty_body_self_round_trip(client: TestClient) -> None:
    """Omitting the body re-validates and re-applies persisted state (think's 'config
    file set' is the SQLite store): succeeds, bumps revision, changes nothing."""
    _seed(client)
    before = _export(client)
    response = client.post("/api/v1/backup/import", headers={"If-Match": _etag(client)})
    assert response.status_code == 200
    assert response.json()["success"] is True
    after = _export(client)
    assert {k: v for k, v in after.items() if k != "exported_at"} == {
        k: v for k, v in before.items() if k != "exported_at"
    }


def test_import_invalid_bundle_conflict(client: TestClient) -> None:
    bundle = {"format_version": "1.0.0", "settings": {"unknown.key": 1}}
    response = client.post("/api/v1/backup/import", json=bundle, headers={"If-Match": _etag(client)})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"


# -- archives --------------------------------------------------------------------


def test_archive_round_trip(client: TestClient) -> None:
    _seed(client)
    created = client.post("/api/v1/backup/create")
    assert created.status_code == 201
    filename = created.json()["filename"]
    assert created.headers["Location"].endswith(f"/api/v1/backup/download/{filename}")

    listed = client.get("/api/v1/backup/list").json()
    assert [b["filename"] for b in listed] == [filename]
    assert listed[0]["size_bytes"] > 0 and listed[0]["created_at"]

    download = client.get(f"/api/v1/backup/download/{filename}")
    assert download.status_code == 200 and len(download.content) > 0

    # mutate the config, then restore the archive to bring the snapshot back
    client.delete("/api/v1/persona/amy", headers={"If-Match": _etag(client)})
    restore = client.post(f"/api/v1/backup/restore/{filename}", headers={"If-Match": _etag(client)})
    assert restore.status_code == 200 and restore.json()["status"] == "imported"
    assert client.get("/api/v1/persona/amy").status_code == 200
    assert client.get("/api/v1/setting/task.retention_days").json()["value"] == 7

    assert client.delete(f"/api/v1/backup/{filename}").status_code == 204
    assert client.get("/api/v1/backup/list").json() == []


def test_archive_missing_and_traversal(client: TestClient) -> None:
    assert client.get("/api/v1/backup/list").json() == []
    assert client.get("/api/v1/backup/download/nope.tar.gz").status_code == 404
    assert client.get("/api/v1/backup/download/..%2Fthink.db").status_code == 404
    assert client.delete("/api/v1/backup/nope.tar.gz").status_code == 404
    etag = _etag(client)
    assert client.post("/api/v1/backup/restore/nope.tar.gz", headers={"If-Match": etag}).status_code == 404


def test_restore_requires_if_match(client: TestClient) -> None:
    client.post("/api/v1/backup/create")
    filename = client.get("/api/v1/backup/list").json()[0]["filename"]
    assert client.post(f"/api/v1/backup/restore/{filename}").status_code == 428
    assert (
        client.post(f"/api/v1/backup/restore/{filename}", headers={"If-Match": '"s999"'}).status_code == 412
    )
