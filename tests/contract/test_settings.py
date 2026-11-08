"""Contract tests: /api/v1/settings/ ETag + If-Match semantics, validation, secret refs."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _assert_error_shape(body: dict) -> None:
    error = body["error"]
    assert isinstance(error["code"], str) and error["code"]
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["request_id"], str) and error["request_id"]
    assert isinstance(error["transaction_id"], str) and error["transaction_id"]


def test_list_settings(client: TestClient) -> None:
    response = client.get("/api/v1/settings/")
    assert response.status_code == 200
    assert response.headers["ETag"].startswith('"s')
    body = response.json()
    # kernel OpalSettingsMap: flat dotted-key map + reserved pending_restart_fields
    assert body["pending_restart_fields"] == []
    keys = sorted(k for k in body if k != "pending_restart_fields")
    assert "task.max_concurrent" in keys
    assert "providers.ollama.base_url" in keys
    assert body["task.max_concurrent"] == 4


def test_show_setting(client: TestClient) -> None:
    response = client.get("/api/v1/settings/task.max_concurrent")
    assert response.status_code == 200
    assert response.headers["ETag"] == '"k0"'
    body = response.json()
    assert body == {
        "key": "task.max_concurrent",
        "value": 4,
        "default": 4,
        "is_default": True,
        "pending_restart": False,
    }


def test_show_unknown_setting(client: TestClient) -> None:
    response = client.get("/api/v1/settings/nope.nope")
    assert response.status_code == 404
    _assert_error_shape(response.json())


def test_malformed_setting_key(client: TestClient) -> None:
    assert client.get("/api/v1/settings/9bad").status_code == 422


def test_set_setting_roundtrip(client: TestClient) -> None:
    # missing If-Match -> 428
    assert client.put("/api/v1/settings/task.max_concurrent", json={"value": 8}).status_code == 428
    # stale If-Match -> 412
    assert (
        client.put("/api/v1/settings/task.max_concurrent", json={"value": 8}, headers={"If-Match": '"k9"'}).status_code
        == 412
    )
    # correct ETag -> 200 + new ETag
    etag = client.get("/api/v1/settings/task.max_concurrent").headers["ETag"]
    response = client.put("/api/v1/settings/task.max_concurrent", json={"value": 8}, headers={"If-Match": etag})
    assert response.status_code == 200
    assert response.headers["ETag"].startswith('"k')
    assert response.headers["ETag"] != '"k0"'
    body = response.json()
    assert body["value"] == 8 and body["is_default"] is False
    # collection ETag also advanced
    assert client.get("/api/v1/settings/").headers["ETag"] != '"s0"'


def test_set_setting_invalid_value(client: TestClient) -> None:
    etag = client.get("/api/v1/settings/task.max_concurrent").headers["ETag"]
    for bad in ("eight", 0, 1000, True):
        response = client.put("/api/v1/settings/task.max_concurrent", json={"value": bad}, headers={"If-Match": etag})
        assert response.status_code == 422, bad


def test_set_unknown_setting(client: TestClient) -> None:
    response = client.put("/api/v1/settings/nope.nope", json={"value": 1}, headers={"If-Match": '"k0"'})
    assert response.status_code == 404


def test_secret_reference_enforced(client: TestClient) -> None:
    etag = client.get("/api/v1/settings/event.credential_ref").headers["ETag"]
    raw = client.put(
        "/api/v1/settings/event.credential_ref", json={"value": "sk-live-secret-value"}, headers={"If-Match": etag}
    )
    assert raw.status_code == 422
    ok = client.put(
        "/api/v1/settings/event.credential_ref", json={"value": "env:OPAL_EVENT_TOKEN"}, headers={"If-Match": etag}
    )
    assert ok.status_code == 200
    assert ok.json()["value"] == "env:OPAL_EVENT_TOKEN"  # reference shown, never secret material


def test_remove_setting_override(client: TestClient) -> None:
    etag = client.get("/api/v1/settings/task.max_concurrent").headers["ETag"]
    client.put("/api/v1/settings/task.max_concurrent", json={"value": 8}, headers={"If-Match": etag})
    new_etag = client.get("/api/v1/settings/task.max_concurrent").headers["ETag"]
    assert client.delete("/api/v1/settings/task.max_concurrent").status_code == 428
    response = client.delete("/api/v1/settings/task.max_concurrent", headers={"If-Match": new_etag})
    assert response.status_code == 204
    assert "ETag" in response.headers
    body = client.get("/api/v1/settings/task.max_concurrent").json()
    assert body["is_default"] is True and body["value"] == 4


def test_patch_settings_atomic(client: TestClient) -> None:
    # missing If-Match -> 428
    assert client.patch("/api/v1/settings/", json={"task.max_concurrent": 6}).status_code == 428
    # stale -> 412
    assert (
        client.patch("/api/v1/settings/", json={"task.max_concurrent": 6}, headers={"If-Match": '"s99"'}).status_code
        == 412
    )
    etag = client.get("/api/v1/settings/").headers["ETag"]
    # reserved key is server-managed -> 422
    assert (
        client.patch("/api/v1/settings/", json={"pending_restart_fields": []}, headers={"If-Match": etag}).status_code
        == 422
    )
    # one bad key fails the whole patch (atomicity)
    bad = client.patch(
        "/api/v1/settings/",
        json={"task.max_concurrent": 6, "task.retention_days": "x"},
        headers={"If-Match": etag},
    )
    assert bad.status_code == 422
    assert client.get("/api/v1/settings/task.max_concurrent").json()["value"] == 4  # not applied
    good = client.patch(
        "/api/v1/settings/",
        json={"task.max_concurrent": 6, "task.retention_days": 7},
        headers={"If-Match": etag},
    )
    assert good.status_code == 200
    assert good.headers["ETag"].startswith('"s')
    assert good.json()["task.max_concurrent"] == 6 and good.json()["task.retention_days"] == 7


def test_dynamic_provider_key_lifecycle(client: TestClient) -> None:
    # new provider id fields exist by definition; read default
    shown = client.get("/api/v1/settings/providers.myprovider.enabled")
    assert shown.status_code == 200 and shown.json()["is_default"] is True
    keys = client.get("/api/v1/settings/").json()
    assert "providers.myprovider.enabled" not in keys  # not materialized yet
    etag = shown.headers["ETag"]
    created = client.put("/api/v1/settings/providers.myprovider.enabled", json={"value": True}, headers={"If-Match": etag})
    assert created.status_code == 200
    keys = client.get("/api/v1/settings/").json()
    assert "providers.myprovider.enabled" in keys  # now materialized
    # invalid provider field -> 404
    assert client.get("/api/v1/settings/providers.myprovider.bogus").status_code == 404
    # malformed key characters -> 422 (contract key pattern violation)
    assert client.get("/api/v1/settings/providers.BAD!.enabled").status_code == 422
    # well-formed but non-standard field shape -> 404
    assert client.get("/api/v1/settings/providers.U2PPER.enabled").status_code == 404


def test_settings_persist_across_restart(tmp_path) -> None:
    """Overrides survive a fresh app instance on the same db file."""
    from opal_think.app import create_app
    from opal_think.config import ThinkConfig

    with TestClient(create_app(ThinkConfig(db_path=tmp_path / "t.db"))) as c1:
        etag = c1.get("/api/v1/settings/task.max_concurrent").headers["ETag"]
        c1.put("/api/v1/settings/task.max_concurrent", json={"value": 9}, headers={"If-Match": etag})
    with TestClient(create_app(ThinkConfig(db_path=tmp_path / "t.db"))) as c2:
        assert c2.get("/api/v1/settings/task.max_concurrent").json()["value"] == 9


def test_duplicate_collection_etag_second_write_fails(client: TestClient) -> None:
    """Same collection ETag used twice: exactly one mutation succeeds (atomic check+write)."""
    etag = client.get("/api/v1/settings/").headers["etag"]
    ok = client.patch("/api/v1/settings/", json={"task.max_concurrent": 8}, headers={"If-Match": etag})
    assert ok.status_code == 200
    stale = client.patch("/api/v1/settings/", json={"task.max_concurrent": 9}, headers={"If-Match": etag})
    assert stale.status_code == 412


def test_malformed_if_match_rejected(client: TestClient) -> None:
    """Kernel IfMatch: exactly one strong quoted ETag; weak/list/*/unquoted -> 422."""
    for bad in ("s1", 'W/"s1"', "*", '"s1", "s2"', '""'):
        assert (
            client.put("/api/v1/settings/task.max_concurrent", json={"value": 8}, headers={"If-Match": bad}).status_code
            == 422
        ), bad
        assert (
            client.patch("/api/v1/settings/", json={"task.max_concurrent": 8}, headers={"If-Match": bad}).status_code
            == 422
        ), bad
        assert client.delete("/api/v1/settings/task.max_concurrent", headers={"If-Match": bad}).status_code == 422, bad


def test_invalid_patch_writes_nothing(client: TestClient) -> None:
    """A PATCH mixing a valid and an invalid key changes nothing (atomic validation)."""
    etag = client.get("/api/v1/settings/").headers["etag"]
    resp = client.patch(
        "/api/v1/settings/",
        json={"task.max_concurrent": 8, "bogus.key": 1},
        headers={"If-Match": etag},
    )
    assert resp.status_code == 422
    assert client.get("/api/v1/settings/task.max_concurrent").json()["is_default"] is True
