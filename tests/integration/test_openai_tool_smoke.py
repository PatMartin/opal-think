"""Opt-in live OpenAI-compatible tool-call smoke — real inference, isolated EVENT.

Explicit opt-in: nothing live runs unless ALL are set:

    OPAL_THINK_LIVE_OPENAI         base URL, e.g. https://api.openai.com or a
                                 compatible gateway
    OPAL_THINK_LIVE_OPENAI_MODEL   model id, e.g. gpt-4o-mini
    OPAL_THINK_LIVE_OPENAI_KEY     name of the env var holding the API key
                                 (credential reference; default: OPENAI_API_KEY)

The key *value* is read only via ``env:`` reference — never logged, recorded, or
asserted. Cost note: each run performs a handful of real chat completions on the
configured endpoint; keep trials low on metered accounts.

Optional knobs:

    OPAL_THINK_LIVE_TIMEOUT       task deadline in seconds (default 240)
    OPAL_THINK_LIVE_TOOL_TRIALS   tool-selection trials (default 1)
    OPAL_THINK_LIVE_REPORT        evidence JSON path (default $TMP/opal_think_live_openai_smoke.json)

EVENT is the in-process ``mock_http_event`` fixture serving one read-only
diagnostic action over ``httpx.MockTransport``: inference and both HTTP wire
shapes are real; nothing executes on this machine and no arbitrary remote
action is invoked.

Capability vs outage: a listed model that cannot do chat completions/tools
fails the task with ``unsupported_capability`` (recorded + skipped here as a
capability finding); transport/auth/server failures surface as
``model_unavailable`` (outage — a real failure).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from opal_think.app import create_app
from opal_think.config import ThinkConfig
from opal_think.providers import CapabilityUnsupported, ProviderUnavailable
from opal_think.providers.openai import OpenAIAdapter
from tests.mock_http_event import MockEventApi

BASE = os.environ.get("OPAL_THINK_LIVE_OPENAI")
MODEL = os.environ.get("OPAL_THINK_LIVE_OPENAI_MODEL")
KEY_REF = os.environ.get("OPAL_THINK_LIVE_OPENAI_KEY", "OPENAI_API_KEY")
TIMEOUT = int(os.environ.get("OPAL_THINK_LIVE_TIMEOUT", "240"))
TRIALS = int(os.environ.get("OPAL_THINK_LIVE_TOOL_TRIALS", "1"))
pytestmark = pytest.mark.skipif(
    not (BASE and MODEL and os.environ.get(KEY_REF)),
    reason=f"OPAL_THINK_LIVE_OPENAI + _MODEL set and {KEY_REF} populated are all required",
)

TASK_TERMINAL = {"completed", "failed", "cancelled", "timed_out"}

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
    parts = urlsplit(url)
    host = parts.hostname or "unknown"
    return f"{parts.scheme}://{host}:{parts.port}" if parts.port else f"{parts.scheme}://{host}"


@pytest.fixture(scope="module", autouse=True)
def evidence(request):
    """Provenance (endpoint, model, credential ref NAME — never the key) + outcomes."""
    data: dict[str, Any] = {
        "endpoint": _safe_endpoint(BASE),
        "model": MODEL,
        "credential_ref": f"env:{KEY_REF}",
        "results": {},
    }
    yield data
    out = Path(
        os.environ.get("OPAL_THINK_LIVE_REPORT", Path(tempfile.gettempdir()) / "opal_think_live_openai_smoke.json")
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    reporter = request.config.pluginmanager.getplugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"live smoke evidence -> {out}")


def _app(tmp_path: Path, event_api: MockEventApi | None) -> TestClient:
    app = create_app(ThinkConfig(db_path=tmp_path / "live.db"))
    app.state.settings.store.set_many(
        {
            "providers.openai.type": "openai",
            "providers.openai.base_url": BASE,
            "providers.openai.enabled": True,
            "providers.openai.credential_ref": f"env:{KEY_REF}",
            "providers.ollama.enabled": False,
            "providers.fake.enabled": False,
        }
    )
    if event_api is not None:
        app.state.event_client = event_api.client()
    return TestClient(app)


def _run_task(client: TestClient, prompt: str) -> dict:
    created = client.post(
        "/api/v1/task/", json={"prompt": prompt, "model_id": f"openai:{MODEL}", "timeout_seconds": TIMEOUT}
    )
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]
    deadline = time.monotonic() + TIMEOUT + 15
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/task/{task_id}").json()
        if task["status"] in TASK_TERMINAL:
            return task
        time.sleep(1)
    raise AssertionError(f"live task {task_id} did not terminate within {TIMEOUT}s")


def _diagnostic_api() -> MockEventApi:
    api = MockEventApi(actions=[dict(DIAGNOSTIC_ACTION)])
    api.set_terminal("read_gpu_temperature", **DIAGNOSTIC_TERMINAL)
    return api


async def test_live_openai_discovery(evidence) -> None:
    """The configured model is listed by the endpoint's /v1/models."""
    adapter = OpenAIAdapter("openai", BASE, f"env:{KEY_REF}")
    models = await adapter.list_models()
    names = {m.name for m in models}
    assert MODEL in names, f"{MODEL} not listed on {_safe_endpoint(BASE)} (have: {sorted(names)[:20]})"
    evidence["results"]["discovery"] = {"model_listed": True, "catalog_size": len(names)}


def test_live_text_answer_no_tools_control(tmp_path, evidence) -> None:
    """Control: model-only deployment (no EVENT wired) answers a plain prompt."""
    with _app(tmp_path, event_api=None) as client:
        task = _run_task(client, "Reply with exactly one word: pong")
    if task["status"] == "failed" and (task["error"] or {}).get("code") == "unsupported_capability":
        evidence["results"]["text_answer_control"] = {"status": "skipped", "error": task["error"]}
        pytest.skip(f"model '{MODEL}' does not support chat completions (capability finding)")
    assert task["status"] == "completed", f"control task failed: {task['error']}"
    assert task["answer"].strip(), "empty answer from live model"
    assert [s for s in task["steps"] if s["kind"] == "action"] == []
    evidence["results"]["text_answer_control"] = {"status": "completed", "answer": task["answer"][:120]}


@pytest.mark.parametrize("trial", range(TRIALS), ids=lambda n: f"trial-{n + 1}")
def test_live_tool_two_turn_invocation(tmp_path, evidence, trial) -> None:
    """Two-turn tool calling: request -> assistant tool_calls -> tool result -> answer."""
    api = _diagnostic_api()
    with _app(tmp_path, event_api=api) as client:
        task = _run_task(client, TOOL_PROMPT)
    key = f"tool_invocation_trial_{trial + 1}"
    error = (task["error"] or {}).get("code") if task["status"] == "failed" else None
    evidence["results"][key] = {"status": task["status"], "error": task["error"]}
    if error == "unsupported_capability":
        pytest.skip(f"model '{MODEL}' is listed but does not support chat/tools (capability finding)")
    assert task["status"] == "completed", f"tool task ended {task['status']} (error={task['error']})"
    # Actual invocation proven at the EVENT wire — never inferred from answer text.
    action_steps = [s for s in task["steps"] if s["kind"] == "action"]
    invoked = api.calls
    evidence["results"][key] = {
        "status": "completed",
        "action_steps": len(action_steps),
        "executions_admitted": len(invoked),
        "answer": task["answer"][:160],
    }
    assert action_steps, (
        f"model '{MODEL}' completed without emitting a tool call (capability finding, not a pass)"
    )
    assert invoked and invoked[0]["action_id"] == "read_gpu_temperature", (
        "no real execution reached EVENT — a 'done' answer here would be hallucinated"
    )
    assert action_steps[0]["status"] == "succeeded" and action_steps[0]["execution_id"] == invoked[0]["execution_id"]
    assert task["answer"].strip() and "63" in task["answer"], (
        f"tool result (GPU0 temp=63C) not reflected in answer: {task['answer'][:160]!r}"
    )


async def test_live_openai_capability_probe(evidence) -> None:
    """Single paid probe: records finish_reason/usage/tool_calls from a real generation."""
    adapter = OpenAIAdapter("openai", BASE, f"env:{KEY_REF}")
    tools = [{"type": "function", "function": {"name": "noop", "parameters": {"type": "object"}}}]
    try:
        gen = await adapter.generate(MODEL, [{"role": "user", "content": "Say ok"}], tools=tools)
    except CapabilityUnsupported as exc:
        evidence["results"]["capability_probe"] = {"status": "skipped", "reason": str(exc)[:200]}
        pytest.skip(f"model '{MODEL}' does not support chat completions/tools: {exc}")
    except ProviderUnavailable as exc:
        evidence["results"]["capability_probe"] = {"status": "outage", "reason": str(exc)[:200]}
        pytest.fail(f"openai-compatible endpoint unavailable: {exc}")
    evidence["results"]["capability_probe"] = {
        "status": "completed",
        "finish_reason": gen.finish_reason,
        "tool_calls": len(gen.tool_calls),
        "usage": gen.usage,
    }
    assert gen.text or gen.tool_calls, "empty generation from live endpoint"
