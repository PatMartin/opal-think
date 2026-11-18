"""Contract tests: idempotent replay resolves before any provider access.

A retry carrying a previously-admitted ``Idempotency-Key`` must return the
stored admission even when every model provider is unreachable — replay is a
durable-store lookup, not a fresh admission. ``admit`` re-checks the key inside
its write transaction, so racing retries on the same key still produce exactly
one task.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from opal_think.engine import TaskEngine
from opal_think.providers.registry import ProviderRegistry
from opal_think.schemas import TaskCreate
from opal_think.settings_registry import SettingsRegistry, SettingsStore
from opal_think.store import ThinkStore


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    return client


def _offline(client: TestClient) -> None:
    """Make model resolution unreachable: no enabled provider remains."""
    client.app.state.settings.store.set_many({"providers.fake.enabled": False})


def test_same_key_replay_with_provider_offline(task_client: TestClient) -> None:
    """Baseline regression: replaying an admitted key must not need the provider."""
    body = {"prompt": "same"}
    first = task_client.post("/api/v1/task/", json=body, headers={"Idempotency-Key": "k-1"})
    assert first.status_code == 202
    _offline(task_client)
    # Prove the outage is real for fresh admissions...
    fresh = task_client.post("/api/v1/task/", json={"prompt": "fresh"})
    assert fresh.status_code == 503
    # ...while the stored retry still returns the original task.
    replay = task_client.post("/api/v1/task/", json=body, headers={"Idempotency-Key": "k-1"})
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == first.json()["id"]
    assert replay.headers["Location"].endswith(f"/api/v1/task/{first.json()['id']}")


def test_same_key_conflict_with_provider_offline(task_client: TestClient) -> None:
    """Key reuse with a different body is a 409 — also decidable without providers."""
    task_client.post(
        "/api/v1/task/", json={"prompt": "one"}, headers={"Idempotency-Key": "k-2"}
    ).raise_for_status()
    _offline(task_client)
    conflict = task_client.post(
        "/api/v1/task/", json={"prompt": "two"}, headers={"Idempotency-Key": "k-2"}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"


def test_concurrent_matching_requests_create_one_task(task_client: TestClient) -> None:
    """Two callers racing on the same key+body admit exactly one task."""
    fake = next(a for a in task_client.app.state.providers.adapters() if a.provider_id == "fake")
    fake.delay = 0.3  # widen the resolution window so the requests truly overlap
    body = {"prompt": "concurrent"}
    barrier = threading.Barrier(2)
    results: list = []

    def post():
        barrier.wait()
        results.append(
            task_client.post("/api/v1/task/", json=body, headers={"Idempotency-Key": "k-race"})
        )

    threads = [threading.Thread(target=post), threading.Thread(target=post)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert len(results) == 2
    assert all(r.status_code == 202 for r in results)
    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1, f"racing retries admitted {ids}"
    rows = task_client.app.state.store.list_tasks(50)
    matching = [r for r in rows if r["prompt"] == "concurrent"]
    assert len(matching) == 1


def test_engine_admit_race_single_winner(tmp_path) -> None:
    """The in-transaction re-check makes the double-admit race impossible even
    when both callers missed the pre-check."""
    settings = SettingsRegistry(SettingsStore(tmp_path / "s.db"))
    engine = TaskEngine(ThinkStore(tmp_path / "t.db"), settings, ProviderRegistry(settings))
    body = TaskCreate(prompt="raced")
    kwargs = dict(
        resolved_model_id="fake:echo",
        causation_id=None,
        causation_depth=0,
        opal_session_id=None,
        opal_transaction_id=None,
        idempotency_key="k-engine",
    )
    barrier = threading.Barrier(2)
    outcomes: list[tuple[dict, bool]] = []

    def admit():
        barrier.wait()
        outcomes.append(engine.admit(body, **kwargs))

    threads = [threading.Thread(target=admit), threading.Thread(target=admit)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert len(outcomes) == 2
    fresh = [o for o in outcomes if o[1] is False]
    replayed = [o for o in outcomes if o[1] is True]
    assert len(fresh) == 1 and len(replayed) == 1, f"expected one admit + one replay: {outcomes}"
    assert fresh[0][0]["id"] == replayed[0][0]["id"]


def test_replay_while_original_task_still_running(task_client: TestClient) -> None:
    """Replay is valid at any point in the task lifecycle, not only terminal."""
    fake = next(a for a in task_client.app.state.providers.adapters() if a.provider_id == "fake")
    fake.delay = 1.0
    body = {"prompt": "slow"}
    first = task_client.post("/api/v1/task/", json=body, headers={"Idempotency-Key": "k-3"})
    assert first.status_code == 202
    replay = task_client.post("/api/v1/task/", json=body, headers={"Idempotency-Key": "k-3"})
    assert replay.status_code == 202 and replay.json()["id"] == first.json()["id"]
    # wait out the slow generation so no active task leaks into later tests
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if task_client.get(f"/api/v1/task/{first.json()['id']}").json()["status"] not in {"queued", "running"}:
            break
        time.sleep(0.05)
