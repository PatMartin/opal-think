"""Contract tests: /api/v1/session/ create, list (cursors), show, remove."""

from __future__ import annotations

from fastapi.testclient import TestClient

from opal_think.store import ThinkStore


def _assert_error_shape(body: dict) -> None:
    error = body["error"]
    assert error["code"] and error["message"] and error["request_id"] and error["transaction_id"]


def _add(client: TestClient, title: str | None = None) -> dict:
    response = client.post("/api/v1/session/", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def _seed_task(store: ThinkStore, session_id: str, status: str = "completed") -> str:
    task_id = ThinkStore.new_task_id()
    store.insert_task(
        {"id": task_id, "session_id": session_id, "status": status, "prompt": "p", "created_at": "2026-01-01T00:00:00+00:00"}
    )
    return task_id


def test_add_session(client: TestClient) -> None:
    response = client.post("/api/v1/session/", json={"title": "GPU work"})
    assert response.status_code == 201
    assert response.headers["Location"].endswith(response.json()["id"])
    assert "/api/v1/session/" in response.headers["Location"]
    body = response.json()
    assert body["id"].startswith("sess_")
    assert body["title"] == "GPU work"
    assert body["task_count"] == 0
    assert body["created_at"] and body["updated_at"]


def test_add_session_no_title(client: TestClient) -> None:
    assert _add(client)["title"] is None


def test_add_session_invalid(client: TestClient) -> None:
    assert client.post("/api/v1/session/", json={"title": ""}).status_code == 422
    assert client.post("/api/v1/session/", json={"unknown": 1}).status_code == 422


def test_show_session(client: TestClient) -> None:
    created = _add(client, "s1")
    shown = client.get(f"/api/v1/session/{created['id']}")
    assert shown.status_code == 200
    assert shown.json()["id"] == created["id"]
    assert client.get("/api/v1/session/sess_missing").status_code == 404


def test_list_sessions_pagination(client: TestClient) -> None:
    for i in range(5):
        _add(client, f"s{i}")
    first = client.get("/api/v1/session/?limit=2")
    assert first.status_code == 200
    page1 = first.json()
    assert page1["has_more"] is True
    assert len(page1["items"]) == 2 and page1["next_cursor"]
    page2 = client.get(f"/api/v1/session/?limit=2&cursor={page1['next_cursor']}").json()
    page3 = client.get(f"/api/v1/session/?limit=2&cursor={page2['next_cursor']}").json()
    ids = [i["id"] for i in page1["items"] + page2["items"] + page3["items"]]
    assert len(ids) == len(set(ids)) == 5
    assert page3["has_more"] is False and page3["next_cursor"] is None


def test_list_sessions_bad_cursor(client: TestClient) -> None:
    for cursor in ("bogus!!!", "aGVsbG8=", ""):
        response = client.get("/api/v1/session/", params={"cursor": cursor})
        assert response.status_code == 422, cursor
        _assert_error_shape(response.json())


def test_remove_session(client: TestClient) -> None:
    created = _add(client)
    assert client.delete(f"/api/v1/session/{created['id']}").status_code == 204
    assert client.get(f"/api/v1/session/{created['id']}").status_code == 404
    assert client.delete(f"/api/v1/session/{created['id']}").status_code == 404


def test_remove_session_cascades_terminal_tasks(client: TestClient) -> None:
    created = _add(client)
    _seed_task(client.app.state.store, created["id"], "completed")
    _seed_task(client.app.state.store, created["id"], "failed")
    assert client.get(f"/api/v1/session/{created['id']}").json()["task_count"] == 2
    assert client.delete(f"/api/v1/session/{created['id']}").status_code == 204


def test_remove_session_busy_conflict(client: TestClient) -> None:
    created = _add(client)
    for status in ("queued", "running"):
        task_id = _seed_task(client.app.state.store, created["id"], status)
        response = client.delete(f"/api/v1/session/{created['id']}")
        assert response.status_code == 409
        _assert_error_shape(response.json())
        # unblock by finishing the seeded task directly
        store = client.app.state.store
        with store._lock, store._connect() as conn:
            conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (task_id,))
    assert client.delete(f"/api/v1/session/{created['id']}").status_code == 204


def test_task_count_reflects_links(client: TestClient) -> None:
    created = _add(client)
    _seed_task(client.app.state.store, created["id"])
    shown = client.get(f"/api/v1/session/{created['id']}").json()
    assert shown["task_count"] == 1
