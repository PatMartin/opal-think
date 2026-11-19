"""Live THINK<->EVENT boundary check against a real local EVENT instance.

Step 7 verification: boots the sibling ``opal-event`` service (1.7.1) on
loopback with REST auth enabled and a *named execute principal* narrowed to
one harmless action with ``own_receipts_only`` — the exact credential shape
THINK is meant to hold. EVENT is LIVE (real REST + SQLite + executor); only
the model is simulated (deterministic ``fake:script`` provider).

Auto-detected, skipped when no EVENT checkout is available:

    OPAL_EVENT_REPO   path to an opal-event checkout with .venv installed
                      (default: ../opal-event relative to this repo)

Verified: scoped authorization (narrowed catalog, hidden action 404, admin
route 403), retry-safe admission (one execution across a keyed retry, 409 on
key reuse with different payload, 422 at the chain-depth bound), a durable
execution receipt retrievable by the scoped token, and the terminal task
state observed by a live SSE watcher.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from opal_think.app import create_app
from opal_think.config import ThinkConfig
from opal_think.providers import Generation, ToolCallRequest

EVENT_PORT = 8090
EVENT = f"http://127.0.0.1:{EVENT_PORT}"
ACTION = "count_marker"
HIDDEN_ACTION = "admin_only_action"
TASK_TERMINAL = {"completed", "failed", "cancelled", "timed_out"}

_REPO_ROOT = Path(__file__).resolve().parents[2]
EVENT_REPO = Path(os.environ.get("OPAL_EVENT_REPO", _REPO_ROOT.parent / "opal-event"))


def _event_python() -> Path | None:
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        candidate = EVENT_REPO / rel
        if candidate.exists():
            return candidate
    return None


_EVENT_PY = _event_python()
pytestmark = pytest.mark.skipif(
    _EVENT_PY is None,
    reason=f"no opal-event checkout with .venv at {EVENT_REPO} (set OPAL_EVENT_REPO)",
)


@pytest.fixture(scope="module")
def monkeypatch_session():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def event_server(tmp_path_factory, monkeypatch_session):
    """Boot the real EVENT service with an execute-scoped Think principal."""
    if not (EVENT_REPO / "src/opal_event").is_dir():
        pytest.skip(f"opal-event sources not found at {EVENT_REPO}")
    tmp = tmp_path_factory.mktemp("event_live")
    config = {
        "version": 1,
        "server": {"host": "127.0.0.1", "port": EVENT_PORT},
        "zeromq": {
            "publishers": "tcp://127.0.0.1:15555",
            "subscribers": "tcp://127.0.0.1:15556",
            "max_event_bytes": 1048576,
            "curve": {"enabled": False},
            "publisher_queue_size": 1000,
        },
        "security": {
            "rest_auth": {
                "enabled": True,
                "token_env": "OPAL_EVENT_ADMIN_TOKEN",
                "principals": [
                    {
                        "name": "think",
                        "token_env": "OPAL_EVENT_THINK_TOKEN",
                        "scope": "execute",
                        "actions": [ACTION],
                        "own_receipts_only": True,
                    }
                ],
            },
            "max_chain_depth": 16,
        },
        "execution": {
            "max_workers": 2,
            "queue_size": 20,
            "default_timeout_seconds": 15,
            "overflow": "drop_and_log",
            "retry_enabled": False,
            "max_output_bytes": 65536,
        },
        "persistence": {
            "database": str(tmp / "opal_event.db"),
            "event_mode": "all",
            "retention_days": 7,
            "execution_history_enabled": True,
        },
        "actions": [
            {
                "id": ACTION,
                "type": "python",
                "enabled": True,
                "description": "Harmless counting marker for interop tests",
                "module": "opal_event.plugins.example",
                "function": "handle_event",
                "timeout_seconds": 10,
            },
            {
                "id": HIDDEN_ACTION,
                "type": "python",
                "enabled": True,
                "description": "Enabled but outside the think principal's action list",
                "module": "opal_event.plugins.example",
                "function": "handle_event",
                "timeout_seconds": 10,
            },
        ],
        "rules": [],
    }
    cfg_path = tmp / "opal_event.yaml"
    cfg_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    env = {
        **os.environ,
        "OPAL_EVENT_ADMIN_TOKEN": "admin-token-test",
        "OPAL_EVENT_THINK_TOKEN": "think-exec-token-test",
    }
    monkeypatch_session.setenv("OPAL_EVENT_ADMIN_TOKEN", "admin-token-test")
    monkeypatch_session.setenv("OPAL_EVENT_THINK_TOKEN", "think-exec-token-test")
    proc = subprocess.Popen(
        [str(_EVENT_PY), "-m", "opal_event", "--config", str(cfg_path), "--no-env-file", "--log-level", "warning"],
        cwd=tmp,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            pytest.fail(f"EVENT exited early (rc={proc.returncode}): {out[-2000:]}")
        try:
            if httpx.get(f"{EVENT}/api/v1/health/live", timeout=1.0).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.3)
    else:
        proc.terminate()
        pytest.fail("EVENT did not become live within 30s")
    yield {"admin": "admin-token-test", "think": "think-exec-token-test"}
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def think_client(tmp_path_factory, event_server):
    """THINK app wired exactly as production: fake model + real EVENT client."""
    tmp = tmp_path_factory.mktemp("think_live")
    app = create_app(ThinkConfig(db_path=tmp / "think.db"))
    app.state.settings.store.set_many(
        {
            "providers.ollama.enabled": False,
            "providers.fake.type": "fake",
            "providers.fake.enabled": True,
            "event.base_url": EVENT,
            "event.credential_ref": "env:OPAL_EVENT_THINK_TOKEN",
            "event.action_allowlist": [ACTION],
        }
    )
    with TestClient(app) as client:
        yield client


def _think_headers(event_server) -> dict:
    return {"Authorization": f"Bearer {event_server['think']}"}


def _admin_headers(event_server) -> dict:
    return {"Authorization": f"Bearer {event_server['admin']}"}


def _execute(action_id: str, token: str, body: dict, headers: dict | None = None) -> httpx.Response:
    return httpx.post(
        f"{EVENT}/api/v1/action/{action_id}/execute",
        json=body,
        headers={"Authorization": f"Bearer {token}", **(headers or {})},
        timeout=10.0,
    )


def test_scoped_authorization(event_server) -> None:
    """The think token is execute-scoped and narrowed to count_marker only."""
    headers = _think_headers(event_server)
    listed = httpx.get(f"{EVENT}/api/v1/action/", headers=headers, timeout=10.0)
    assert listed.status_code == 200, listed.text[:300]
    ids = [a["id"] for a in listed.json()]
    assert ids == [ACTION], f"narrowed principal should see only its action: {ids}"
    # Hidden action is invisible to the narrowed principal (404), but exists for admin.
    assert httpx.get(f"{EVENT}/api/v1/action/{HIDDEN_ACTION}", headers=headers, timeout=10.0).status_code == 404
    assert (
        httpx.get(f"{EVENT}/api/v1/action/{HIDDEN_ACTION}", headers=_admin_headers(event_server), timeout=10.0).status_code
        == 200
    )
    # Executing an out-of-scope action is refused.
    assert _execute(HIDDEN_ACTION, event_server["think"], {"event": {"topic": "t", "data": {}}}).status_code == 404
    # Admin-only surface is denied to the execute scope (publish route).
    denied = httpx.post(
        f"{EVENT}/api/v1/event/", json={"topic": "t", "data": {}}, headers=headers, timeout=10.0
    )
    assert denied.status_code == 403, denied.text[:200]
    # Execution reads are allowed (executions:read) and restricted to own receipts.
    assert httpx.get(f"{EVENT}/api/v1/execution/", headers=headers, timeout=10.0).status_code == 200


def test_keyed_retry_single_admission(event_server) -> None:
    """EVENT 1.7.1 retry-safe contract, live: same key + same payload returns the
    original receipt (one admission); same key + different payload is 409."""
    token = event_server["think"]
    body = {"event": {"topic": "interop.retry", "data": {"object": "once"}}}
    key = "interop-key-1"
    first = _execute(ACTION, token, body, {"Idempotency-Key": key})
    assert first.status_code == 202, first.text[:300]
    exec_id = first.json()["id"]
    second = _execute(ACTION, token, body, {"Idempotency-Key": key})
    assert second.status_code == 202 and second.json()["id"] == exec_id, "keyed retry admitted a second execution"
    conflict = _execute(ACTION, token, {"event": {"topic": "interop.retry", "data": {"object": "twice"}}},
                        {"Idempotency-Key": key})
    assert conflict.status_code == 409
    executions = httpx.get(f"{EVENT}/api/v1/execution/", headers=_think_headers(event_server), timeout=10.0).json()
    items = executions if isinstance(executions, list) else executions.get("items", [])
    assert len([e for e in items if e.get("action_id") == ACTION and e.get("id") == exec_id]) == 1


def test_chain_depth_bound_enforced(event_server) -> None:
    """X-Opal-Chain-Depth at the configured bound is refused live."""
    response = _execute(
        ACTION,
        event_server["think"],
        {"event": {"topic": "t", "data": {}}},
        {"X-Opal-Chain-Depth": "16"},
    )
    assert response.status_code == 422, response.text[:300]


def test_think_task_end_to_end_with_watcher(think_client: TestClient, event_server) -> None:
    """Scripted model requests count_marker -> live EVENT executes -> durable
    receipt -> task.completed observed by a live SSE watcher."""
    before = httpx.get(f"{EVENT}/api/v1/execution/", headers=_think_headers(event_server), timeout=10.0).json()
    before_items = before if isinstance(before, list) else before.get("items", [])
    fake = next(a for a in think_client.app.state.providers.adapters() if a.provider_id == "fake")
    fake.set_script(
        [
            Generation("", tool_calls=[ToolCallRequest(ACTION, {"topic": "interop.e2e", "data": {"object": "marker"}})]),
            Generation("action finished"),
        ]
    )
    created = think_client.post("/api/v1/task/", json={"prompt": "run the marker action", "model_id": "fake:script"})
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]

    frames: list[dict] = []

    def watch() -> None:
        with think_client.stream("GET", f"/api/v1/task/{task_id}/stream") as response:
            event = None
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    frames.append(json.loads(line[5:].strip()) | {"_event": event})

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    task = None
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        task = think_client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in TASK_TERMINAL:
            break
        time.sleep(0.5)
    watcher.join(timeout=10)
    assert task and task["status"] == "completed", f"task failed: {task and task['error']}"
    steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert steps and steps[0]["action_id"] == ACTION and steps[0]["status"] == "completed"
    execution_id = steps[0]["execution_id"]
    assert execution_id, "no durable receipt recorded"
    # Receipt is retrievable from live EVENT with the scoped token.
    receipt = httpx.get(f"{EVENT}/api/v1/execution/{execution_id}", headers=_think_headers(event_server), timeout=10.0)
    assert receipt.status_code == 200 and receipt.json()["status"] == "completed", receipt.text[:300]
    # Exactly one admission for this call.
    after = httpx.get(f"{EVENT}/api/v1/execution/", headers=_think_headers(event_server), timeout=10.0).json()
    after_items = after if isinstance(after, list) else after.get("items", [])
    assert len(after_items) == len(before_items) + 1, "task flow produced more than one execution"
    # Watcher observed the terminal state over SSE.
    assert frames and frames[-1]["type"] == "task.completed", f"watcher frames: {[f['type'] for f in frames]}"
