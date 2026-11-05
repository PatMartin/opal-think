"""Opt-in live Ollama tool-calling smoke — real inference against an isolated EVENT.

Explicit opt-in: nothing live runs unless BOTH are set:

    OPAL_THINK_LIVE_OLLAMA   e.g. http://host:11434
    OPAL_THINK_LIVE_MODEL    e.g. llama3.1:8b — must advertise the ``tools`` capability

Optional knobs:

    OPAL_THINK_LIVE_TIMEOUT       task deadline in seconds (default 240)
    OPAL_THINK_LIVE_TOOL_TRIALS   tool-selection trials (default 1; raise for a rate)
    OPAL_THINK_LIVE_REPORT        evidence JSON path (default $TMP/opal_think_live_smoke.json)

EVENT is the in-process ``mock_http_event`` fixture serving one read-only
diagnostic action over ``httpx.MockTransport``: inference and the HTTP wire
shape are real; nothing executes on this machine.

When a model cannot produce tool calls (``/api/show`` lacks the ``tools``
capability) the tool tests *skip* with an explanation; a tool-capable model that
emits no tool call *fails* descriptively — that is a capability finding about the
model, not a suite defect.
"""

from __future__ import annotations

import json
import os
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
from opal_think.providers.ollama import OllamaAdapter
from tests.mock_http_event import MockEventApi

BASE = os.environ.get("OPAL_THINK_LIVE_OLLAMA")
MODEL = os.environ.get("OPAL_THINK_LIVE_MODEL")
TIMEOUT = int(os.environ.get("OPAL_THINK_LIVE_TIMEOUT", "240"))
TRIALS = int(os.environ.get("OPAL_THINK_LIVE_TOOL_TRIALS", "1"))
pytestmark = pytest.mark.skipif(not (BASE and MODEL), reason="OPAL_THINK_LIVE_OLLAMA + OPAL_THINK_LIVE_MODEL not set")

TASK_TERMINAL = {"completed", "failed", "cancelled", "timed_out"}

# Read-only diagnostic action: shell/exec shape per released opal_event.1.4.0,
# metadata per the self-describing-actions convention. The mock never executes it.
DIAGNOSTIC_ACTION = {
    "id": "read_gpu_temperature",
    "type": "shell",
    "enabled": True,
    "mode": "exec",
    "executable": "/opt/opal/bin/gpu-temp",
    "args": [],
    "stdin": "event_json",
    "timeout_seconds": 10,
    "description": "Read-only diagnostic: report the current GPU temperature.",
    "keywords": ["gpu", "temperature", "diagnostic"],
    "input_schema": {"type": "object"},
}
DIAGNOSTIC_TERMINAL = {
    "status": "succeeded",
    "exit_code": 0,
    "stdout": "GPU0 temp=63C",
    "stderr": "",
    "result": {"gpu_temp_c": 63},
}
TOOL_PROMPT = (
    "Use the read_gpu_temperature external action to check the GPU temperature, "
    "then report the measured temperature."
)


def _safe_endpoint(url: str) -> str:
    """Scheme://host[:port] only — never logs credentials or query material."""
    parts = urlsplit(url)
    host = parts.hostname or "unknown"
    return f"{parts.scheme}://{host}:{parts.port}" if parts.port else f"{parts.scheme}://{host}"


@pytest.fixture(scope="module", autouse=True)
def evidence(request):
    """Observable provenance (model tag, quantization, version, settings) + per-test
    outcomes, written as a JSON artifact so live runs are attributable in reports."""
    data: dict[str, Any] = {"endpoint": _safe_endpoint(BASE), "model": MODEL, "results": {}}
    try:
        version = httpx.get(f"{BASE}/api/version", timeout=5.0)
        data["ollama_version"] = version.json().get("version", "unknown") if version.status_code == 200 else "unknown"
    except httpx.HTTPError:
        data["ollama_version"] = "unreachable"
    try:
        show = httpx.post(f"{BASE}/api/show", json={"model": MODEL}, timeout=10.0)
        if show.status_code == 200:
            payload = show.json()
            details = payload.get("details") or {}
            info = payload.get("model_info") or {}
            data["model_details"] = {
                "parameter_size": details.get("parameter_size"),
                "quantization_level": details.get("quantization_level"),
                "families": details.get("families"),
                "format": details.get("format"),
                "capabilities": payload.get("capabilities"),
                "context_length": next((v for k, v in info.items() if k.endswith("context_length")), None),
                "parameters": (payload.get("parameters") or "")[:500] or None,
            }
    except httpx.HTTPError:
        data["model_details"] = "unavailable"
    yield data
    out = Path(os.environ.get("OPAL_THINK_LIVE_REPORT", Path(tempfile.gettempdir()) / "opal_think_live_smoke.json"))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    reporter = request.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"live smoke evidence -> {out}")


def _app(tmp_path: Path, event_api: MockEventApi | None) -> TestClient:
    app = create_app(ThinkConfig(db_path=tmp_path / "live.db"))
    app.state.settings.store.set_many(
        {"providers.ollama.base_url": BASE, "providers.ollama.enabled": True, "providers.fake.enabled": False}
    )
    if event_api is not None:
        app.state.event_client = event_api.client()
    return TestClient(app)


def _run_task(client: TestClient, prompt: str) -> dict:
    created = client.post(
        "/api/v1/tasks/", json={"prompt": prompt, "model_id": f"ollama:{MODEL}", "timeout_seconds": TIMEOUT}
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]
    deadline = time.monotonic() + TIMEOUT + 15
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in TASK_TERMINAL:
            return task
        time.sleep(1)
    raise AssertionError(f"live task {task_id} did not terminate within {TIMEOUT}s")


def _diagnostic_api() -> MockEventApi:
    api = MockEventApi(actions=[dict(DIAGNOSTIC_ACTION)])
    api.set_terminal("read_gpu_temperature", **DIAGNOSTIC_TERMINAL)
    return api


def test_live_discovery_and_capabilities(evidence) -> None:
    """Model is pulled and its capabilities/metadata are recorded for the report."""
    models = httpx.get(f"{BASE}/api/tags", timeout=10.0).json().get("models", [])
    names = {m.get("name") for m in models}
    assert MODEL in names, f"{MODEL} not pulled on {_safe_endpoint(BASE)} (have: {sorted(names)})"
    assert evidence["ollama_version"] not in {"unreachable"}, "ollama /api/version unreachable"
    evidence["results"]["discovery"] = {"model_pulled": True, "catalog_size": len(names)}


async def test_live_text_answer_no_tools_control(tmp_path, evidence) -> None:
    """Control: model-only deployment (no EVENT wired) answers a plain prompt."""
    with _app(tmp_path, event_api=None) as client:
        task = _run_task(client, "Reply with exactly one word: pong")
    assert task["status"] == "completed", f"control task failed: {task['error']}"
    assert task["answer"].strip(), "empty answer from live model"
    assert [s for s in task["steps"] if s["kind"] == "action"] == []  # no tools existed
    evidence["results"]["text_answer_control"] = {"status": "completed", "answer": task["answer"][:120]}


@pytest.mark.parametrize("trial", range(TRIALS), ids=lambda n: f"trial-{n + 1}")
def test_live_tool_selection_invocation_interpretation(tmp_path, evidence, trial) -> None:
    """Tool selection -> real wire invocation -> result interpretation, asserted separately."""
    key = f"tool_invocation_trial_{trial + 1}"
    # Capability gate: a model without /api/show "tools" can never emit tool calls —
    # skipping documents the finding instead of failing on an impossible ask.
    show = httpx.post(f"{BASE}/api/show", json={"model": MODEL}, timeout=10.0)
    capabilities = set(show.json().get("capabilities") or []) if show.status_code == 200 else set()
    if "tools" not in capabilities:
        evidence["results"][key] = {"status": "skipped", "reason": "model lacks tools capability"}
        pytest.skip(
            f"model '{MODEL}' does not advertise the 'tools' capability — tool-call smoke "
            "requires a tool-capable model (e.g. llama3.1, qwen2.5, mistral-nemo)"
        )
    api = _diagnostic_api()
    with _app(tmp_path, event_api=api) as client:
        task = _run_task(client, TOOL_PROMPT)
    if task["status"] != "completed":
        evidence["results"][key] = {"status": task["status"], "error": task["error"]}
        pytest.fail(
            f"tool task ended {task['status']} (error={task['error']}); if the model lacks the "
            f"tools capability this is a capability finding, not a suite bug"
        )
    # Actual invocation, proven at the wire — not inferred from answer text.
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    invoked = api.calls
    evidence["results"][key] = {
        "status": "completed",
        "action_steps": len(action_steps),
        "executions_admitted": len(invoked),
        "answer": task["answer"][:160],
    }
    assert action_steps, (
        f"model '{MODEL}' completed without emitting a tool call: it either lacks the "
        f"tools capability (see /api/show) or ignored the tool schemas — capability finding"
    )
    assert invoked and invoked[0]["action_id"] == "read_gpu_temperature", (
        f"no real execution reached EVENT (admitted: {[c['action_id'] for c in invoked]}) — "
        "an answer claiming execution here would be hallucinated, and is not accepted"
    )
    step = action_steps[0]
    assert step["status"] == "succeeded" and step["execution_id"] == invoked[0]["execution_id"]
    # Result interpretation: the terminal execution fields reached the final answer.
    assert task["answer"].strip(), "model produced no final answer after the tool result"
    assert "63" in task["answer"], (
        f"tool result (GPU0 temp=63C) not reflected in answer: {task['answer'][:160]!r}"
    )


async def test_live_model_tool_capability_documented(evidence) -> None:
    """Documented gate: explains (not silently passes) when a model can't emit tool calls."""
    adapter = OllamaAdapter("ollama", BASE)
    shown = await adapter.show_model(MODEL)
    assert shown is not None, f"{MODEL} not found via /api/show"
    evidence["tool_calling_advertised"] = shown.tool_calling
    evidence["context_window"] = shown.context_window
    if not shown.tool_calling:
        pytest.skip(
            f"model '{MODEL}' does not advertise the 'tools' capability — tool-call smoke "
            "requires a tool-capable model (e.g. llama3.1, qwen2.5, mistral-nemo)"
        )
    assert shown.context_window is None or shown.context_window > 0
