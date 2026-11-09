"""Opt-in live THINK<->EVENT interoperability — real endpoints, real inference.

Requires ALL of these (nothing live runs otherwise):

    OPAL_THINK_LIVE_EVENT        EVENT base URL of an isolated test instance
                                 configured with one read-only diagnostic action
                                 (e.g. read_gpu_temperature) and execute-scoped auth
    OPAL_THINK_LIVE_EVENT_KEY    name of the env var holding the EXECUTE-scoped
                                 EVENT token (credential reference, never logged)
    OPAL_THINK_LIVE_OLLAMA       Ollama base URL
    OPAL_THINK_LIVE_MODEL        tool-capable model id (e.g. llama3.1:8b)

Optional: OPAL_THINK_LIVE_TIMEOUT (default 240), OPAL_THINK_LIVE_EVENT_ACTION
(default read_gpu_temperature), OPAL_THINK_LIVE_REPORT (evidence JSON path;
default $TMP/opal_think_live_event_interop.json — carries git SHAs, model, and
redacted endpoints so live results are attributable).

Unlike the mock_http_event suite, EVENT here is a real service reached through
the production build_event_client wiring. Only read-only diagnostic actions are
exercised; nothing mutates EVENT configuration or the host.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from opal_think.app import create_app
from opal_think.config import ThinkConfig

EVENT = os.environ.get("OPAL_THINK_LIVE_EVENT")
EVENT_KEY_REF = os.environ.get("OPAL_THINK_LIVE_EVENT_KEY", "OPAL_EVENT_EXECUTE_TOKEN")
OLLAMA = os.environ.get("OPAL_THINK_LIVE_OLLAMA")
MODEL = os.environ.get("OPAL_THINK_LIVE_MODEL")
ACTION = os.environ.get("OPAL_THINK_LIVE_EVENT_ACTION", "read_gpu_temperature")
TIMEOUT = int(os.environ.get("OPAL_THINK_LIVE_TIMEOUT", "240"))
pytestmark = pytest.mark.skipif(
    not (EVENT and OLLAMA and MODEL and os.environ.get(EVENT_KEY_REF)),
    reason=f"requires OPAL_THINK_LIVE_EVENT/_OLLAMA/_MODEL and {EVENT_KEY_REF} populated",
)

TASK_TERMINAL = {"completed", "failed", "cancelled", "timed_out"}


def _safe(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}" if parts.port else f"{parts.scheme}://{parts.hostname}"


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
    except OSError:
        return "unknown"


@pytest.fixture(scope="module", autouse=True)
def evidence(request):
    data: dict[str, Any] = {
        "event_endpoint": _safe(EVENT),
        "ollama_endpoint": _safe(OLLAMA),
        "model": MODEL,
        "action": ACTION,
        "think_git_sha": _sha(),
        "event_credential_ref": f"env:{EVENT_KEY_REF}",
        "results": {},
    }
    yield data
    out = Path(os.environ.get("OPAL_THINK_LIVE_REPORT", Path(tempfile.gettempdir()) / "opal_think_live_event_interop.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    reporter = request.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"live interop evidence -> {out}")


def _app(tmp_path: Path, event_url: str | None = EVENT) -> TestClient:
    """THINK wired exactly as production: EVENT client built from settings."""
    app = create_app(ThinkConfig(db_path=tmp_path / "live.db"))
    settings = {"providers.ollama.base_url": OLLAMA, "providers.fake.enabled": False}
    if event_url:
        settings["event.base_url"] = event_url
        settings["event.credential_ref"] = f"env:{EVENT_KEY_REF}"
    app.state.settings.store.set_many(settings)
    return TestClient(app)


def _run_task(client: TestClient, prompt: str, headers: dict | None = None) -> dict:
    created = client.post(
        "/api/v1/task/", json={"prompt": prompt, "model_id": f"ollama:{MODEL}", "timeout_seconds": TIMEOUT},
        headers=headers or {},
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]
    deadline = time.monotonic() + TIMEOUT + 15
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in TASK_TERMINAL:
            return task
        time.sleep(1)
    raise AssertionError(f"task {task_id} did not terminate within {TIMEOUT}s")


def test_live_event_discovery_surface(tmp_path, evidence) -> None:
    """Execute-scoped token can list actions; the diagnostic action is discoverable."""
    headers = {"Authorization": f"Bearer {os.environ[EVENT_KEY_REF]}"}
    listed = httpx.get(f"{EVENT}/api/v1/action/", headers=headers, timeout=10.0)
    assert listed.status_code == 200, listed.text[:200]
    items = {a["id"]: a for a in listed.json()["items"]}
    assert ACTION in items, f"{ACTION} not in EVENT catalog: {sorted(items)}"
    entry = items[ACTION]
    evidence["results"]["discovery"] = {
        "action_enabled": entry.get("enabled"),
        "metadata_fields": sorted(k for k in ("description", "input_schema", "keywords", "suggested_prompts") if k in entry),
        "operational_fields_hidden": not any(
            k in entry for k in ("executable", "script_path", "url", "headers", "module", "function", "args")
        ),
    }
    assert entry.get("enabled") is True
    # execute-scope projection must not expose operational fields (step 07)
    leaked = [k for k in ("executable", "script_path", "url", "headers", "module", "function", "args") if k in entry]
    assert not leaked, f"execute-scope discovery leaked operational fields: {leaked}"


def test_live_full_interop_grounded_answer(tmp_path, evidence) -> None:
    """Task -> real tool selection -> EVENT execute -> poll terminal -> grounded answer."""
    prompt = f"Use the {ACTION} external action to check the GPU temperature, then report it."
    headers = {"X-Opal-Session-Id": "interop-sess", "X-Opal-Transaction-Id": "interop-txn"}
    with _app(tmp_path) as client:
        task = _run_task(client, prompt, headers=headers)
    evidence["results"]["interop_task"] = {
        "status": task["status"], "error": task["error"], "answer": (task["answer"] or "")[:200],
        "steps": [{"kind": s["kind"], "status": s["status"], "action_id": s.get("action_id"),
                   "execution_id": s.get("execution_id")} for s in task["steps"]],
    }
    assert task["status"] == "completed", f"task failed: {task['error']}"
    steps = [s for s in task["steps"] if s["kind"] == "action"]
    assert steps and steps[0]["action_id"] == ACTION, f"no real action step: {task['steps']}"
    assert steps[0]["status"] == "succeeded" and steps[0]["execution_id"], "action did not reach terminal success"
    assert steps[0]["action_input"]["event"]["data"] is not None, "action input not recorded"
    assert task["answer"].strip(), "no grounded final answer"
    # deadline honored: task finished within its timeout
    assert task["timeout_seconds"] == TIMEOUT


def test_live_schema_rejection_no_side_effect(tmp_path, evidence) -> None:
    """Invalid event.data against the action's declared schema -> 422, nothing admitted."""
    entry = httpx.get(
        f"{EVENT}/api/v1/action/{ACTION}", headers={"Authorization": f"Bearer {os.environ[EVENT_KEY_REF]}"}, timeout=10.0
    )
    schema = (entry.json().get("input_schema") if entry.status_code == 200 else None) or {}
    required = schema.get("required") or []
    if not required:
        pytest.skip(f"{ACTION} declares no required input_schema fields; cannot build an invalid call")
    headers = {"Authorization": f"Bearer {os.environ[EVENT_KEY_REF]}"}
    executions_before = httpx.get(f"{EVENT}/api/v1/execution/", headers=headers, timeout=10.0).json().get("items", [])
    bad = httpx.post(
        f"{EVENT}/api/v1/action/{ACTION}/execute",
        json={"event": {"topic": "interop.test", "data": {}}},  # missing required fields
        headers=headers, timeout=10.0,
    )
    assert bad.status_code == 422, f"expected 422 schema rejection, got {bad.status_code}: {bad.text[:200]}"
    executions_after = httpx.get(f"{EVENT}/api/v1/execution/", headers=headers, timeout=10.0).json().get("items", [])
    assert len(executions_after) == len(executions_before), "rejected input produced an execution record"
    evidence["results"]["schema_rejection"] = {"status": bad.status_code, "new_executions": 0}


def test_live_event_unavailable_degrades(tmp_path, evidence) -> None:
    """EVENT unreachable at discovery -> model-only degradation, not a crash."""
    with _app(tmp_path, event_url="http://127.0.0.1:1") as client:  # closed port
        task = _run_task(client, "Reply with exactly one word: pong")
    evidence["results"]["event_down"] = {"status": task["status"], "error": task["error"]}
    assert task["status"] in {"completed", "failed"}, task["error"]
    if task["status"] == "failed":
        assert (task["error"] or {}).get("code") in {"unavailable", "event_unavailable"}


def test_live_disabled_action_not_invoked(tmp_path, evidence) -> None:
    """A disabled action is rejected at the EVENT boundary — never admitted."""
    headers = {"Authorization": f"Bearer {os.environ[EVENT_KEY_REF]}"}
    items = httpx.get(f"{EVENT}/api/v1/action/", headers=headers, timeout=10.0).json().get("items", [])
    disabled = [a for a in items if not a.get("enabled")]
    if not disabled:
        pytest.skip("no disabled action in the test EVENT catalog")
    action_id = disabled[0]["id"]
    resp = httpx.post(
        f"{EVENT}/api/v1/action/{action_id}/execute", json={"event": {"topic": "t", "data": {}}}, headers=headers, timeout=10.0
    )
    assert resp.status_code in {403, 404, 422}, f"disabled action admitted: {resp.status_code}"
    evidence["results"]["disabled_action"] = {"action_id": action_id, "status": resp.status_code}
