"""Contract tests: task watchers always reach terminal state.

A saturated watcher mailbox can never strand the terminal event or the stream's
close: reserved delivery evicts the oldest *live* frame (contract allows no
replay guarantee for intermediate events) so the terminal frame and sentinel
always land. Watchers that miss a terminal frame reconcile from the durable
task record on the 15s idle check; reconnecting after termination yields the
terminal snapshot immediately.
"""

from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from opal_think.engine import _SUBSCRIBER_QUEUE_MAX


@pytest.fixture
def task_client(client: TestClient) -> TestClient:
    client.app.state.settings.store.set_many(
        {"providers.ollama.enabled": False, "providers.fake.type": "fake", "providers.fake.enabled": True}
    )
    return client


def _frames(response) -> list[dict]:
    frames, event = [], None
    for line in response.iter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            frames.append(json.loads(line[5:].strip()) | {"_event": event})
    return frames


def _set_delay(client: TestClient, seconds: float) -> None:
    fake = next(a for a in client.app.state.providers.adapters() if a.provider_id == "fake")
    fake.delay = seconds


def _engine(client: TestClient):
    return client.app.state.engine


def test_saturated_mailbox_reserves_terminal_and_close(task_client: TestClient) -> None:
    """>maxsize live frames then finish: the terminal frame AND the close
    sentinel both land — oldest live frames were evicted to make room."""
    engine = _engine(task_client)
    sub = engine.subscribe("task_sat")
    for i in range(_SUBSCRIBER_QUEUE_MAX + 100):
        engine._emit("task_sat", "task.progress", {"summary": f"step {i}"})
    engine._emit("task_sat", "task.completed", {"status": "completed"})
    engine._close_subscribers("task_sat")

    drained, terminal_seen = [], False
    while not sub.queue.empty():
        frame = sub.queue.get_nowait()
        drained.append(frame)
        if frame and frame["type"] == "task.completed":
            terminal_seen = True
    assert terminal_seen, "saturated mailbox lost the terminal event"
    assert drained[-1] is None, "close sentinel was not delivered after terminal"
    assert drained[-2]["type"] == "task.completed"
    assert sub.dropped > 0
    # Per-watcher resource bound held throughout.
    assert sub.queue.maxsize == _SUBSCRIBER_QUEUE_MAX


def test_saturated_mailbox_reserves_cancel_and_timeout(task_client: TestClient) -> None:
    """Every terminal type is reserved, not just completion."""
    engine = _engine(task_client)
    for event_type in ("task.cancelled", "task.timed_out"):
        sub = engine.subscribe("task_x")
        for _ in range(_SUBSCRIBER_QUEUE_MAX + 50):
            engine._emit("task_x", "task.output", {"text": "tok"})
        engine._emit("task_x", event_type, {"status": event_type.removeprefix("task.")})
        engine._close_subscribers("task_x")
        drained = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())]
        assert drained[-1] is None and drained[-2]["type"] == event_type
        engine.unsubscribe("task_x", sub)


def test_live_frames_bounded_but_terminal_guaranteed(task_client: TestClient) -> None:
    """Saturation drops *live* frames only: queue never exceeds its bound and
    at least the newest live frame + terminal + sentinel are retained."""
    engine = _engine(task_client)
    sub = engine.subscribe("task_bounded")
    for i in range(_SUBSCRIBER_QUEUE_MAX * 3):
        engine._emit("task_bounded", "task.output", {"text": str(i)})
    assert sub.queue.qsize() == _SUBSCRIBER_QUEUE_MAX  # bounded
    engine._emit("task_bounded", "task.failed", {"status": "failed"})
    engine._close_subscribers("task_bounded")
    assert sub.queue.qsize() == _SUBSCRIBER_QUEUE_MAX  # still bounded
    tail = [sub.queue.get_nowait() for _ in range(sub.queue.qsize())][-2:]
    assert tail[0]["type"] == "task.failed" and tail[1] is None


def test_idle_reconcile_synthesizes_terminal_after_runner_loss(
    task_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runner dies without a terminal transition -> watchers get the stream's
    end (no fabricated event). A reconnected watcher on the still-"running"
    row keeps getting keep-alives until the idle reconcile sees the reaped
    failure and synthesizes the terminal frame from the durable record."""
    monkeypatch.setattr("opal_think.routers.tasks._KEEPALIVE_SECONDS", 0.05)
    engine = _engine(task_client)
    _set_delay(task_client, 30.0)  # stays running for the whole test
    task_id = task_client.post("/api/v1/task/", json={"prompt": "hi"}).json()["id"]

    def watch() -> list[dict]:
        with task_client.stream("GET", f"/api/v1/task/{task_id}/stream") as response:
            return _frames(response)

    frames1: list[dict] = []
    t1 = threading.Thread(target=lambda: frames1.extend(watch()), daemon=True)
    t1.start()
    deadline = time.monotonic() + 5
    while not engine._subscribers.get(task_id) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert engine._subscribers.get(task_id), "watcher never subscribed"
    # Kill the runner on its own loop: CancelledError -> finally -> close.
    task_client.portal.call(engine._runners[task_id].cancel)
    t1.join(timeout=5)
    assert not t1.is_alive(), "stream did not close after runner loss"
    assert all(f["type"] not in {"task.completed", "task.failed", "task.cancelled", "task.timed_out"} for f in frames1)

    # Reconnect: the row is still "running" — keep-alives until the reap lands.
    frames2: list[dict] = []
    t2 = threading.Thread(target=lambda: frames2.extend(watch()), daemon=True)
    t2.start()
    time.sleep(0.2)
    assert engine.reap_orphans() == 1  # marks the orphaned row failed
    t2.join(timeout=5)
    assert not t2.is_alive(), "idle reconcile never delivered the terminal frame"
    assert frames2[-1]["type"] == "task.failed" and frames2[-1]["data"]["status"] == "failed"


def test_reconnect_after_disconnect_sees_terminal(task_client: TestClient) -> None:
    """Watch, disconnect mid-run, reconnect: the durable snapshot delivers the
    terminal state (and, in-flight, reserved delivery still applies)."""
    _set_delay(task_client, 0.5)
    task_id = task_client.post("/api/v1/task/", json={"prompt": "hi"}).json()["id"]
    # First watcher connects then disconnects mid-run.
    with task_client.stream("GET", f"/api/v1/task/{task_id}/stream") as response:
        assert response.status_code == 200
        it = response.iter_lines()
        first = next(it)  # connect-time status frame
        assert first.startswith("event:")
    # Task completes while unwatched; reconnecting yields the terminal snapshot.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if task_client.get(f"/api/v1/task/{task_id}").json()["status"] == "completed":
            break
        time.sleep(0.05)
    with task_client.stream("GET", f"/api/v1/task/{task_id}/stream") as response:
        frames = _frames(response)
    assert frames[-1]["type"] == "task.completed" and frames[-1]["data"]["status"] == "completed"


def test_stream_observes_cancellation(task_client: TestClient) -> None:
    """A watcher on a cancelled task receives task.cancelled + close."""
    _set_delay(task_client, 3.0)
    task_id = task_client.post("/api/v1/task/", json={"prompt": "hi"}).json()["id"]
    frames: list[dict] = []

    def reader() -> None:
        with task_client.stream("GET", f"/api/v1/task/{task_id}/stream") as response:
            frames.extend(_frames(response))

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.3)  # watcher attached, task running
    assert task_client.post(f"/api/v1/task/{task_id}/cancel").status_code == 202
    thread.join(timeout=10)
    assert not thread.is_alive(), "stream did not close after cancellation"
    assert frames[-1]["type"] == "task.cancelled"


def test_stream_observes_timeout(task_client: TestClient) -> None:
    """A watcher on a deadline-exceeding task receives task.timed_out + close."""
    _set_delay(task_client, 5.0)
    task_id = task_client.post(
        "/api/v1/task/", json={"prompt": "hi", "timeout_seconds": 2}
    ).json()["id"]
    with task_client.stream("GET", f"/api/v1/task/{task_id}/stream") as response:
        frames = _frames(response)
    assert frames[-1]["type"] == "task.timed_out"
    assert task_client.get(f"/api/v1/task/{task_id}").json()["status"] == "timed_out"
